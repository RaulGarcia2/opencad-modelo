"""Modelo: fachada simple para dibujar en OpenCADStudio via MCP.

Uso tipico:
    from opencad_modelo import Modelo

    with Modelo() as m:                # conecta con el dibujo en pantalla
        m.creaCapa("capaRuta", 4, "DASHDOT")
        h1 = m.linea([0, 0], [100, 0], "capaRuta")
        h2 = m.texto("Hola", [10, 45, 0], capa="capaRuta")

Solo libreria estandar. El documento queda modificado SIN guardar.
"""

import json
import math
import os
import time
from decimal import Decimal

from .ocs import OcsMcp, OcsSessionError, elegir_sesion

BIN_POR_DEFECTO = "/usr/local/bin/opencad-studio"


def _hs(handles):
    """Normaliza a lista: acepta un handle suelto o una lista."""
    if isinstance(handles, str):
        return [handles]
    return list(handles)


def _xy(p):
    """[x, y] de un punto que puede ser dict {x,y,z} o lista [x,y,...]."""
    if isinstance(p, dict):
        return [float(p.get("x", 0.0)), float(p.get("y", 0.0))]
    return [float(p[0]), float(p[1])]


def _offset_xy(pts, d):
    """Paralela geometrica: desplaza cada vertice por la normal local.

    d > 0 -> derecha del sentido de la polilinea; d < 0 -> izquierda.
    La normal de cada vertice es la media de las normales derechas de
    los tramos adyacentes. Deterministico (no depende del kernel OFFSET).
    """
    n = len(pts)
    out = []
    for i in range(n):
        dirs = []
        if i > 0:
            dirs.append((pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
        if i < n - 1:
            dirs.append((pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]))
        nx = ny = 0.0
        for dx, dy in dirs:
            largo = (dx * dx + dy * dy) ** 0.5
            if largo < 1e-12:
                continue
            nx += dy / largo  # normal derecha = (dy, -dx)
            ny += -dx / largo
        largo = (nx * nx + ny * ny) ** 0.5
        z = pts[i][2] if len(pts[i]) > 2 else 0.0
        if largo < 1e-12:
            out.append([pts[i][0], pts[i][1], z])
            continue
        out.append([pts[i][0] + nx / largo * d, pts[i][1] + ny / largo * d, z])
    return out


def _estacionar(vertices, paso, cerrada=False):
    """Puntos cada `paso` a lo largo de una polilinea con arcos.

    vertices: [(x, y, bulge), ...] (bulge del tramo i->i+1 en cada vertice;
    bulge = tan(angulo/4), positivo = antihorario). Devuelve [(x, y, pk)].
    Siempre incluye el inicio (pk 0) y el final.
    """
    if paso is None or paso != paso or paso in (float("inf"), float("-inf")):
        raise ValueError(f"paso {paso!r}: debe ser finito")
    if paso <= 0:
        raise ValueError(f"paso {paso!r}: debe ser > 0")
    pts = [(float(x), float(y)) for x, y, *_ in vertices]
    bulges = [
        float(b) for _, _, b, *_ in [list(v) + [0.0] * (3 - len(v)) for v in vertices]
    ]
    if len(pts) < 2:
        raise ValueError("se necesitan al menos 2 vertices")
    n = len(pts)
    segs = []
    for i in range(n if cerrada else n - 1):
        a = pts[i]
        b = pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        c = math.hypot(dx, dy)
        bb = bulges[i]
        theta = 4.0 * math.atan(bb)
        if abs(bb) < 1e-12 or c < 1e-12 or abs(theta) < 1e-12:
            segs.append({"kind": "recta", "a": a, "b": b, "lon": c})
            continue
        r = c / (2.0 * math.sin(theta / 2.0))
        mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
        ux, uy = dx / c, dy / c
        h = abs(r) * math.cos(theta / 2.0)
        sgn = 1.0 if theta > 0 else -1.0
        cx, cy = mx + (-uy) * h * sgn, my + ux * h * sgn
        segs.append(
            {
                "kind": "arco",
                "a": a,
                "cx": cx,
                "cy": cy,
                "r": abs(r),
                "a0": math.atan2(a[1] - cy, a[0] - cx),
                "sgn": sgn,
                "lon": abs(r * theta),
            }
        )
    total = sum(s["lon"] for s in segs)
    if total <= 1e-12:
        return [(pts[0][0], pts[0][1], 0.0)]

    def punto_en(s_acum, objetivo):
        for s in segs:
            if objetivo <= s_acum + s["lon"] + 1e-9 or s is segs[-1]:
                t = min(max(objetivo - s_acum, 0.0), s["lon"])
                if s["kind"] == "recta":
                    f = 0.0 if s["lon"] < 1e-12 else t / s["lon"]
                    x = s["a"][0] + (s["b"][0] - s["a"][0]) * f
                    y = s["a"][1] + (s["b"][1] - s["a"][1]) * f
                else:
                    ang = s["a0"] + s["sgn"] * (t / s["r"])
                    x = s["cx"] + s["r"] * math.cos(ang)
                    y = s["cy"] + s["r"] * math.sin(ang)
                return x, y, s_acum
            s_acum += s["lon"]
        s = segs[-1]
        if s["kind"] == "recta":
            return s["b"][0], s["b"][1], s_acum
        ang = s["a0"] + s["sgn"] * (s["lon"] / s["r"])
        return (
            s["cx"] + s["r"] * math.cos(ang),
            s["cy"] + s["r"] * math.sin(ang),
            s_acum,
        )

    out, k, s_acum = [], 0, 0.0
    while True:
        objetivo = k * paso
        if objetivo > total - 1e-9:
            break
        x, y, _ = punto_en(0.0, objetivo)
        out.append((x, y, objetivo))
        k += 1
    x, y, _ = punto_en(0.0, total)
    if not out or abs(out[-1][2] - total) > 1e-9:
        out.append((x, y, total))
    return out


def _fmt_num(v):
    """float -> notacion decimal posicional, sin exponente ni recortes.

    `:g` limita a 6 cifras significativas y salta a cientifica (p. ej.
    4569586.3297 -> '4.56959e+06'), que OCS interpreta mal. Aqui va la
    representacion exacta del double.
    """
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"Numero {v!r}: debe ser finito")
    s = format(Decimal(repr(f)), "f")
    if s.startswith("-") and Decimal(s) == 0:
        s = "0"
    return s


def _fmt_punto(p):
    """[x, y] o [x, y, z] -> 'x,y' o 'x,y,z' (valida numeros finitos)."""
    if len(p) not in (2, 3):
        raise ValueError(f"Punto {p!r}: se esperan 2 o 3 coordenadas")
    return ",".join(_fmt_num(v) for v in p)


def _color_valor(color):
    """Normaliza color a valor MCP: int -> ACI, tupla/lista -> {rgb:[r,g,b]}."""
    if isinstance(color, bool):
        raise ValueError(f"Color {color!r} no valido")
    if isinstance(color, int):
        if not 0 <= color <= 256:
            raise ValueError(f"Color ACI {color!r}: debe estar entre 0 y 256")
        return color
    if isinstance(color, (tuple, list)) and len(color) == 3:
        rgb = [int(c) for c in color]
        if any(not 0 <= c <= 255 for c in rgb):
            raise ValueError(f"Color RGB {color!r}: componentes 0-255")
        return {"rgb": rgb}
    raise ValueError(f"Color {color!r} no valido: usa indice ACI (int) o tupla RGB")


class Modelo:
    """Sesion de dibujo sobre el documento abierto en OpenCADStudio."""

    def __init__(self, bin=BIN_POR_DEFECTO):
        self.mcp = OcsMcp(bin)
        try:
            self.mcp.handshake()
            self.sid, self.doc_id = elegir_sesion(self.mcp)
            estado = self.mcp.tool(
                "ocs_read", {"ocs_session_id": self.sid, "op": "state"}
            )
            if estado["document_id"] != self.doc_id:
                self.mcp.execute(
                    self.sid,
                    {
                        "op": "activate",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "document_id": self.doc_id,
                    },
                )
                print(f"Pestana activada: documento {self.doc_id}")
                estado = self.mcp.tool(
                    "ocs_read", {"ocs_session_id": self.sid, "op": "state"}
                )
            self._limpiar_restos(estado)
        except OcsSessionError:
            # Sin dibujo abierto: cerrar el subproceso MCP y propagar para
            # que el programa que llama decida (except OcsSessionError).
            # Tragársela aquí dejaría un Modelo con sid=None que fallaría
            # después de forma confusa.
            self.mcp.cerrar()
            raise
        except Exception:
            self.mcp.cerrar()
            raise

    def textos_existentes(self):
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {"type": "Text", "detail": "summary", "limit": 1000},
            },
        )
        return {e["handle"] for e in q.get("entities", [])}

    def _limpiar_restos(self, estado):
        """Cierra un editor/comando dejado por una ejecucion interrumpida.

        Si el proceso anterior murio a mitad de un TEXT (SIGTERM/SIGKILL no
        ejecutan el finally), el editor inline queda abierto y OCS reporta
        waiting_input en TODA operacion posterior.
        """
        if estado.get("text_editor"):
            print(
                "AVISO: editor de texto inline abierto de una ejecucion "
                "anterior; lo cierro"
            )
            self.mcp.execute(
                self.sid,
                {
                    "op": "action",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "name": "text_commit",
                },
            )
            estado = self.mcp.tool(
                "ocs_read", {"ocs_session_id": self.sid, "op": "state"}
            )
        if estado.get("command"):
            print("AVISO: comando activo de una ejecucion anterior; lo cancelo")
            try:
                self.mcp.execute(
                    self.sid,
                    {
                        "op": "cancel",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "document_id": self.doc_id,
                    },
                )
            except RuntimeError:
                pass  # cancel devuelve status "cancelled" (no es fallo real)

    def cerrar(self):
        self.mcp.cerrar()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cerrar()
        return False

    @property
    def fichero(self) -> str | None:
        """Ruta completa del dibujo activo, o None si aun no se guardo.

        Se lee del estado en cada acceso (el usuario puede cambiar de
        pestana o hacer Guardar como en cualquier momento).
        """
        estado = self.mcp.tool("ocs_read", {"ocs_session_id": self.sid, "op": "state"})
        for doc in estado.get("documents", []):
            if doc.get("id") == estado.get("document_id"):
                return doc.get("path")
        return None

    # -- capas ---------------------------------------------------------
    def creaCapa(self, nombre: str, color: int = 2, tipo: str = "CONTINUOUS"):
        """Crea la capa (color ACI + tipo de linea) o la reutiliza.
        Si la capa ya existe NO toca ni su color ni su linetype.
        """
        capas = self.mcp.tool(
            "ocs_read", {"ocs_session_id": self.sid, "op": "layers", "parameters": {}}
        )
        if any(c["name"] == nombre for c in capas.get("layers", [])):
            print(f"La capa '{nombre}' ya existe: la reutilizo sin tocarla")
            return

        if tipo.upper() != "CONTINUOUS":
            self._exigir_tipo_cargado(tipo)
        pasos = [
            {"op": "run", "document_id": self.doc_id, "cmd": f"LAYER NEW {nombre}"},
            {
                "op": "run",
                "document_id": self.doc_id,
                "cmd": f"LAYER COLOR {nombre} {color}",
            },
        ]
        resp = self.mcp.execute(
            self.sid,
            {"op": "batch", "request_id": self.mcp.nuevo_id("ej"), "steps": pasos},
        )
        assert resp["status"] == "completed", resp
        assert resp["completed_steps"] == len(pasos), resp
        print(f"Capa '{nombre}' creada (color ACI {color})")
        if tipo.upper() != "CONTINUOUS":
            self._fijar_tipo_linea_capa(nombre, tipo.upper())
        self._refrescar_menu_capas(nombre)

    def _refrescar_menu_capas(self, nombre: str):
        """Hace visible la capa en los menus de OCS.

        LAYER NEW no reconstruye el espejo del panel/dropdown (a
        diferencia de RENAME); un doble renombrado la refresca via MCP.
        """
        if any(ch.isspace() for ch in nombre):
            print(f"AVISO: '{nombre}' con espacios: OCS la mostrara al reabrir")
            return
        tmp = f"{nombre}_tmp"
        capas = self.mcp.tool(
            "ocs_read", {"ocs_session_id": self.sid, "op": "layers", "parameters": {}}
        )
        if any(c["name"] == tmp for c in capas.get("layers", [])):
            raise RuntimeError(f"Existe '{tmp}'; borrala o cambia el nombre")
        for viejo, nuevo in ((nombre, tmp), (tmp, nombre)):
            resp = self.mcp.execute(
                self.sid,
                {
                    "op": "run",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "cmd": f"RENAME LAYER {viejo} {nuevo}",
                },
            )
            assert resp.get("status") == "completed", resp
        rec = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "records",
                "parameters": {"collection": "layers"},
            },
        )
        assert any(r.get("name") == nombre for r in rec.get("records", [])), nombre
        print(f"Capa '{nombre}' visible en menus")

    def _exigir_tipo_cargado(self, tipo: str):
        """Falla claro si el linetype no esta cargado (no hay LOAD por MCP)."""
        self.mcp.execute(
            self.sid,
            {
                "op": "run",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "cmd": "LINETYPE LIST",
            },
        )
        hist = self.mcp.tool(
            "ocs_read", {"ocs_session_id": self.sid, "op": "history", "parameters": {}}
        )
        textos = "\n".join(e.get("text") or "" for e in hist.get("entries", []))
        import re

        # Formato: "Tipos de línea: Continuous (Solid line), ByLayer (), ...,
        #   DASHDOT (Dash dot __ . __ ...)". Todo en una o pocas lineas.
        cargados = set(re.findall(r"([A-Za-z0-9_]+)\s*\(", textos))
        cargados |= {"CONTINUOUS", "BYLAYER", "BYBLOCK"}
        if tipo.upper() not in {c.upper() for c in cargados}:
            raise RuntimeError(
                f"Linetype '{tipo}' no cargado. Cargalo en el GUI "
                f"(panel de capas) o usa uno de: {sorted(cargados)}"
            )

    def _fijar_tipo_linea_capa(self, nombre: str, tipo: str):
        """Fija el tipo de linea de la capa via records (set_properties)."""
        rec = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "records",
                "parameters": {"collection": "layers"},
            },
        )
        capa = next(
            (r for r in rec.get("records", []) if r.get("name") == nombre), None
        )
        if capa is None:
            raise RuntimeError(f"No existe el record de capa '{nombre}'")
        actual = capa["properties"].get("line_type")
        if actual == tipo:
            print(f"Capa '{nombre}': ya es {tipo}")
            return
        resp = self.mcp.execute(
            self.sid,
            {
                "op": "set_properties",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "collection": "layers",
                "handle": capa["handle"],
                "updates": [{"path": "/line_type", "value": tipo, "expected": actual}],
            },
        )
        assert resp.get("status") == "completed", resp
        print(f"Capa '{nombre}': tipo de linea {actual} -> {tipo}")

    def asignaCapa(self, handles, capa: str):
        """Mueve entidades a la capa: select + property (campo 'layer')."""
        self.mcp.execute(
            self.sid,
            {
                "op": "select",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "handles": _hs(handles),
            },
        )
        props = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "properties",
                "parameters": {"document_id": self.doc_id},
            },
        )
        campo = next(
            (
                p["id"]
                for s in props.get("sections", [])
                for p in s.get("properties", [])
                if p.get("kind") == "layer"
            ),
            None,
        )
        if campo is None:
            raise RuntimeError("La seleccion no expone propiedad de capa")
        self.mcp.execute(
            self.sid,
            {
                "op": "property",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "field": campo,
                "value": capa,
            },
        )
        print(f"{len(_hs(handles))} entidades movidas a '{capa}'")

    # -- modificar -----------------------------------------------------
    def _todos_handles(self):
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {"detail": "summary", "limit": 10000},
            },
        )
        return {e["handle"] for e in q.get("entities", [])}

    def _modificar(self, handles, pasos):
        """select + pasos run/input. Devuelve la respuesta del batch."""
        if isinstance(handles, str):
            handles = [handles]
        hs = [h.strip() for h in _hs(handles)]
        if not hs:
            raise ValueError("Se necesita al menos 1 handle")
        pasos = [{"op": "select", "document_id": self.doc_id, "handles": hs}, *pasos]
        resp = self.mcp.execute(
            self.sid,
            {"op": "batch", "request_id": self.mcp.nuevo_id("ej"), "steps": pasos},
        )
        assert resp["status"] == "completed", resp
        assert resp["completed_steps"] == len(pasos), resp
        return resp

    def _creados_despues(self, antes):
        nuevos = self._todos_handles() - set(antes)
        assert nuevos, "El comando no creo entidades"
        return self._ordenar_handles(nuevos)

    def mover(self, handles, p1: list):
        """Desplaza las entidades de p1 a p2. Devuelve los mismos handles."""
        p0 = [0, 0, 0]
        self._modificar(
            handles,
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"MOVE {_fmt_punto(p0)} {_fmt_punto(p1)}",
                }
            ],
        )
        print(f"{len(_hs(handles))} entidades movidas")
        return _hs(handles)

    def copiar(self, handles, p1: list):
        """Copia las entidades de p0 a p1. Devuelve los NUEVOS handles."""
        p0 = [0, 0, 0]
        antes = self._todos_handles()
        # COPY con base+destino inline coloca la copia y termina solo.
        self._modificar(
            handles,
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"COPY {_fmt_punto(p0)} {_fmt_punto(p1)}",
                }
            ],
        )
        nuevos = self._creados_despues(antes)
        assert len(nuevos) == len(_hs(handles)), (nuevos, handles)
        print(f"{len(nuevos)} copias creadas: {nuevos}")
        return nuevos

    def ampliar(self, handles, base: list, factor: float):
        """Escala respecto a base por factor (> 0). Devuelve los handles."""
        f = float(factor)
        if not (f > 0.0 and f not in (float("inf"), float("-inf"))):
            raise ValueError(f"Factor {factor!r}: debe ser > 0")
        self._modificar(
            handles,
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"SCALE {_fmt_punto(base)} {_fmt_num(f)}",
                }
            ],
        )
        print(f"{len(_hs(handles))} entidades escaladas x{f:g}")
        return _hs(handles)

    def rotar(self, handles, centro: list, angulo: float):
        """Rota respecto a centro los grados dados. Devuelve los handles."""
        g = float(angulo)
        if g != g or g in (float("inf"), float("-inf")):
            raise ValueError(f"Angulo {angulo!r}: debe ser finito")
        self._modificar(
            handles,
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"ROTATE {_fmt_punto(centro)} {_fmt_num(g)}",
                }
            ],
        )
        print(f"{len(_hs(handles))} entidades rotadas {g:g}°")
        return _hs(handles)

    def simetria(self, handles, p1: list, p2: list, borrar_origen: bool = False):
        """Simetria respecto al eje p1-p2 (los puntos van inline).

        Nota: `run` confirma con Enter al final, que en AskErase equivale
        a No (= conservar origen). Para borrar el origen se usa el flujo
        guiado start + input, que no anade ese Enter.
        Conservar origen -> devuelve los NUEVOS handles; borrarlo ->
        devuelve los mismos (girados en su sitio).
        """
        hs = [h.strip() for h in _hs(handles)]
        if not hs:
            raise ValueError("Se necesita al menos 1 handle")
        antes = self._todos_handles()
        if borrar_origen:
            base = {"document_id": self.doc_id}
            self.mcp.execute(
                self.sid,
                dict(
                    {
                        "op": "select",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "handles": hs,
                    },
                    **base,
                ),
            )
            self.mcp.execute(
                self.sid,
                dict(
                    {
                        "op": "start",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "cmd": "MIRROR",
                    },
                    **base,
                ),
            )
            for pt in (p1, p2):
                self.mcp.execute(
                    self.sid,
                    dict(
                        {
                            "op": "input",
                            "request_id": self.mcp.nuevo_id("ej"),
                            "kind": "point",
                            "point": [
                                float(pt[0]),
                                float(pt[1]),
                                float(pt[2] if len(pt) > 2 else 0),
                            ],
                        },
                        **base,
                    ),
                )
            self.mcp.execute(
                self.sid,
                dict(
                    {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "kind": "token",
                        "text": "Y",
                    },
                    **base,
                ),
            )
            print(f"{len(hs)} entidades reflejadas (origen borrado)")
            return hs
        self._modificar(
            hs,
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"MIRROR {_fmt_punto(p1)} {_fmt_punto(p2)}",
                }
            ],
        )
        nuevos = self._creados_despues(antes)
        assert len(nuevos) == len(hs), (nuevos, hs)
        print(f"{len(nuevos)} reflejos creados: {nuevos}")
        return nuevos

    def splinefit(self, handles):
        """Convierte una polilinea en una bspline (comando SPLINEFIT).

        La bspline cubica pasa por TODOS los vertices de la polilinea
        (quedan como fit-points) suavizando las tangentes. SPLINEFIT
        borra la polilinea original (reversible con Undo) y crea una
        entidad Spline; aqui se devuelve a su capa original.
        Devuelve el handle de la spline nueva.
        """
        hs = [h.strip() for h in _hs(handles)]
        if len(hs) != 1:
            raise ValueError("splinefit necesita exactamente 1 polilinea")
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {"handles": hs, "detail": "summary", "limit": 1},
            },
        )
        ents = q.get("entities", [])
        if not ents or ents[0].get("type") != "Polyline":
            raise ValueError(f"splinefit: {hs[0]} no es una polilinea")
        capa = ents[0].get("layer") or ""

        antes = self._todos_handles()
        # SPLINEFIT exige la polilinea pre-seleccionada (pickfirst).
        self._modificar(
            hs,
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": "SPLINEFIT",
                }
            ],
        )
        nuevos = self._todos_handles() - antes
        assert len(nuevos) == 1, nuevos
        nuevo = self._ordenar_handles(nuevos)[0]

        # Verificar que es una Spline.
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {"handles": [nuevo], "detail": "summary", "limit": 1},
            },
        )
        tipo = (q.get("entities") or [{}])[0].get("type")
        assert tipo == "Spline", (tipo, q)

        if capa and capa != "0":
            self.asignaCapa([nuevo], capa)
        print(f"Polilinea {hs[0]} -> bspline {nuevo} (capa {capa})")
        return nuevo

    def _puntos_de(self, h):
        """Lista de [x, y, z] de Line, Polyline o Spline."""
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {"handles": [h], "detail": "geometry", "limit": 1},
            },
        )
        ents = q.get("entities", [])
        if not ents:
            raise ValueError(f"paralela: la entidad {h} no existe")
        e = ents[0]
        t = e.get("type")
        if t == "Line":
            s, en = e["start"], e["end"]
            return [
                [float(s[0]), float(s[1]), float(s[2]) if len(s) > 2 else 0.0],
                [float(en[0]), float(en[1]), float(en[2]) if len(en) > 2 else 0.0],
            ]
        if t == "Polyline":
            rec = self.mcp.tool(
                "ocs_read",
                {
                    "ocs_session_id": self.sid,
                    "op": "records",
                    "parameters": {"collection": "entities", "handles": [h]},
                },
            )
            elev = rec["records"][0]["properties"].get("elevation") or 0.0
            vs = [[p[0], p[1], elev] for p in (_xy(v) for v in e.get("vertices", []))]
            if len(vs) < 2:
                raise ValueError(f"paralela: la polilinea {h} tiene <2 vertices")
            return vs
        if t == "Spline":
            rec = self.mcp.tool(
                "ocs_read",
                {
                    "ocs_session_id": self.sid,
                    "op": "records",
                    "parameters": {"collection": "entities", "handles": [h]},
                },
            )
            props = rec["records"][0]["properties"]
            pts = props.get("fit_points") or props.get("control_points") or []
            out = []
            for p in pts:
                if isinstance(p, dict):
                    out.append(
                        [
                            float(p.get("x", 0)),
                            float(p.get("y", 0)),
                            float(p.get("z", 0)),
                        ]
                    )
                else:
                    out.append(
                        [float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0]
                    )
            if len(out) < 2:
                raise ValueError(f"paralela: la spline {h} no tiene puntos")
            return out
        raise ValueError(f"paralela: tipo {t} no soportado (Line, Polyline, Spline)")

    def paralela(self, handle, distancia: float, capa: str = ""):
        """Crea una paralela geometrica a Line, Polyline o Spline.

        distancia > 0 -> a la DERECHA del sentido de la entidad;
        distancia < 0 -> a la IZQUIERDA.
        Desplaza cada vertice por la normal local (deterministico y con
        el signo garantizado; no depende del kernel OFFSET). Devuelve la
        lista con el handle de la polilinea nueva.
        """
        d = float(distancia)
        if d != d or d in (float("inf"), float("-inf")):
            raise ValueError(f"distancia {distancia!r}: debe ser finita")
        if abs(d) < 1e-9:
            raise ValueError("distancia no puede ser 0")
        h = _hs(handle)[0]
        pts = self._puntos_de(h)
        off = _offset_xy(pts, d)
        nuevo = self.polilinea(off, capa)
        lado = "derecha" if d > 0 else "izquierda"
        print(f"Paralela de {h} a {lado} {abs(d):g} -> {nuevo}")
        return [nuevo]

    def puntos_polilinea(self, handle, paso: float):
        """Puntos cada `paso` a lo largo de una polilinea (rectas y arcos).

        Lee vertices + bulge + cierre + elevacion via records y calcula
        en local (sin tocar el dibujo). Devuelve [[x, y, z], ...] con
        pk 0, paso, 2*paso... mas siempre el punto final.
        """
        h = _hs(handle)[0]
        rec = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "records",
                "parameters": {"collection": "entities", "handles": [h]},
            },
        )
        recs = rec.get("records", [])
        if not recs:
            raise ValueError(f"puntos_polilinea: la entidad {h} no existe")
        props = recs[0].get("properties", {})
        tipo = str(recs[0].get("record_type", "") or recs[0].get("type", ""))
        if "POLYLINE" not in tipo.upper():
            raise ValueError(
                f"puntos_polilinea: {h} es {tipo or 'desconocido'}, "
                "se necesita una polilinea"
            )
        verts = []
        for v in props.get("vertices", []):
            loc = v.get("location", {})
            verts.append(
                (
                    float(loc.get("x", 0.0)),
                    float(loc.get("y", 0.0)),
                    float(v.get("bulge", 0.0) or 0.0),
                )
            )
        if len(verts) < 2:
            raise ValueError(f"puntos_polilinea: {h} tiene <2 vertices")
        z = props.get("elevation", 0.0)
        try:
            z = float(z)
        except (TypeError, ValueError):
            z = 0.0
        est = _estacionar(verts, paso, bool(props.get("is_closed", False)))
        print(
            f"Polilinea {h}: {len(est)} puntos cada {paso:g} "
            f"(longitud {est[-1][2]:.3f})"
        )
        return [[x, y, z] for x, y, _ in est]

    def _tipo_de(self, h):
        """Tipo (ui_name) de una entidad, o None si no existe."""
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {"handles": [h], "detail": "summary", "limit": 1},
            },
        )
        ents = q.get("entities", [])
        return ents[0].get("type") if ents else None

    def seleccionar_coordenadas(
        self, tipos=("Line", "Polyline"), timeout: float = 120.0
    ):
        """Espera a que pinches una linea/polilinea y da sus coordenadas.

        Pincha en el visor (un clic). Sondea state.selection hasta que
        haya exactamente 1 entidad de los tipos pedidos. Devuelve
        {handle, tipo, capa, puntos} con puntos=[[x, y, z], ...].
        """
        if isinstance(tipos, str):
            tipos = [tipos]
        aceptados = {str(t).strip().lower() for t in tipos}
        if "polyline" in aceptados:
            aceptados |= {"lwpolyline", "polyline2d", "polyline3d"}
        etiqueta = "/".join(sorted(aceptados))
        print(
            f"Pincha una {etiqueta} en OpenCADStudio "
            f"(espero hasta {timeout:.0f} s, Ctrl+C para salir)..."
        )
        t0 = time.time()
        avisado = None
        while time.time() - t0 < timeout:
            sel = self.mcp.tool(
                "ocs_read", {"ocs_session_id": self.sid, "op": "state"}
            ).get("selection", [])
            if len(sel) == 1:
                tipo = self._tipo_de(sel[0])
                if tipo and tipo.lower() in aceptados:
                    h = sel[0]
                    q = self.mcp.tool(
                        "ocs_read",
                        {
                            "ocs_session_id": self.sid,
                            "op": "query",
                            "parameters": {
                                "handles": [h],
                                "detail": "summary",
                                "limit": 1,
                            },
                        },
                    )
                    capa = (q.get("entities") or [{}])[0].get("layer", "")
                    puntos = self._puntos_de(h)
                    try:  # deseleccionar
                        self.mcp.execute(
                            self.sid,
                            {
                                "op": "select",
                                "request_id": self.mcp.nuevo_id("ej"),
                                "document_id": self.doc_id,
                                "clear": True,
                            },
                        )
                    except Exception:
                        pass
                    print(
                        f"Seleccionado {tipo} {h} ({len(puntos)} puntos, capa {capa})"
                    )
                    return {"handle": h, "tipo": tipo, "capa": capa, "puntos": puntos}
                clave = ("tipo", tipo)
                if clave != avisado:
                    print(f"  Eso es un '{tipo}': necesito {etiqueta}")
                    avisado = clave
            elif len(sel) > 1:
                clave = ("varias", len(sel))
                if clave != avisado:
                    print(f"  Hay {len(sel)} seleccionados: deja solo uno")
                    avisado = clave
            time.sleep(1.0)
        raise TimeoutError("Sin seleccion tras el timeout.")

    # -- dibujo --------------------------------------------------------
    def _corre_batch(self, pasos, detalle="changed_entities"):
        resp = self.mcp.execute(
            self.sid,
            {"op": "batch", "request_id": self.mcp.nuevo_id("ej"), "steps": pasos},
            detail=detalle,
        )
        assert resp["status"] == "completed", resp
        assert resp["completed_steps"] == len(pasos), resp
        handles = [e["handle"] for e in resp.get("changed_entities", [])]
        for e in resp.get("changed_entities", []):
            print(f"  - {e['type']} handle={e['handle']} capa={e['layer']}")
        if not handles:
            raise RuntimeError(f"El batch no creo entidades: {resp}")
        return handles

    def linea(self, p1: list, p2: list, capa: str = ""):
        """Dibuja LINE p1->p2 (puntos [x, y] o [x, y, z]). Devuelve el handle."""
        handles = self._corre_batch(
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"LINE {_fmt_punto(p1)} {_fmt_punto(p2)}",
                }
            ]
        )
        if capa:
            self.asignaCapa(handles, capa)
        return handles[0]

    def polilinea(self, puntos: list, capa: str = ""):
        """Dibuja PLINE por los puntos. Devuelve el handle.

        Con >~200 vertices el diario de geometria (cap 256) expulsa la
        epoca inicial y changed_entities viene vacio aunque la entidad
        SI se crea: en ese caso se localiza por diff antes/despues.
        """
        if len(puntos) < 2:
            raise ValueError("Polilinea necesita al menos 2 puntos")
        cmd = "PLINE " + " ".join(_fmt_punto(p) for p in puntos)
        antes = self._todos_handles()
        resp = self.mcp.execute(
            self.sid,
            {
                "op": "batch",
                "request_id": self.mcp.nuevo_id("ej"),
                "steps": [
                    {
                        "op": "run",
                        "document_id": self.doc_id,
                        "cmd": cmd,
                    }
                ],
            },
            detail="changed_entities",
        )
        assert resp["status"] == "completed", resp
        assert resp["completed_steps"] == 1, resp
        handles = [e["handle"] for e in resp.get("changed_entities", [])]
        for e in resp.get("changed_entities", []):
            print(f"  - {e['type']} handle={e['handle']} capa={e['layer']}")
        if not handles:
            # Sin reporte (diario expulsado): localizar por diff.
            nuevos = self._todos_handles() - antes
            assert len(nuevos) == 1, nuevos
            handles = self._ordenar_handles(nuevos)
            print(f"  - Polyline handle={handles[0]} (localizada por diff)")
        if capa:
            self.asignaCapa(handles, capa)
        return handles[0]

    def poligono_relleno(self, puntos: list, capa: str, color):
        """Dibuja un poligono con relleno solido.

        puntos: lista de [x, y] o [x, y, z] (>=3). Se cierra repitiendo
            el primero. color: indice ACI (int) o tupla RGB.
        Si la capa no existe se crea basica. El contorno se conserva.
        Devuelve {"contorno": handle, "relleno": handle}.
        """
        if len(puntos) < 3:
            raise ValueError("Poligono necesita al menos 3 puntos")
        anillo = [_fmt_punto(p) for p in puntos]
        valor_color = _color_valor(color)

        capas = self.mcp.tool(
            "ocs_read", {"ocs_session_id": self.sid, "op": "layers", "parameters": {}}
        )
        if not any(c["name"] == capa for c in capas.get("layers", [])):
            self.mcp.execute(
                self.sid,
                {
                    "op": "run",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "cmd": f"LAYER NEW {capa}",
                },
            )
            print(f"Capa '{capa}' creada")

        antes = self._todos_handles()
        h_contorno = self.polilinea(puntos + [puntos[0]], capa)
        resp = self.mcp.execute(
            self.sid,
            {
                "op": "batch",
                "request_id": self.mcp.nuevo_id("ej"),
                "steps": [
                    {
                        "op": "select",
                        "document_id": self.doc_id,
                        "handles": [h_contorno],
                    },
                    {"op": "start", "document_id": self.doc_id, "cmd": "HATCH"},
                    {
                        "op": "input",
                        "document_id": self.doc_id,
                        "kind": "token",
                        "text": "P SOLID",
                    },
                    {"op": "input", "document_id": self.doc_id, "kind": "enter"},
                ],
            },
            detail="changed_entities",
        )
        if resp.get("status") != "completed":
            try:
                self.mcp.execute(
                    self.sid,
                    {
                        "op": "cancel",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "document_id": self.doc_id,
                    },
                )
            except Exception:
                pass
            raise RuntimeError(
                "HATCH no termino (¿poligono abierto o autointersecado?). "
                f"{json.dumps(resp)[:300]}"
            )
        nuevos = self._todos_handles() - antes - {h_contorno}
        assert len(nuevos) == 1, nuevos
        h_relleno = self._ordenar_handles(nuevos)[0]
        # Verificar tipos por query.
        q = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "query",
                "parameters": {
                    "handles": [h_contorno, h_relleno],
                    "detail": "summary",
                    "limit": 2,
                },
            },
        )
        tipos = {e["handle"]: e["type"] for e in q.get("entities", [])}
        assert tipos.get(h_contorno) == "Polyline", tipos
        assert tipos.get(h_relleno) == "Hatch", tipos

        self.asignaCapa([h_contorno, h_relleno], capa)
        self.mcp.execute(
            self.sid,
            {
                "op": "select",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "handles": [h_relleno],
            },
        )
        props = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "properties",
                "parameters": {"document_id": self.doc_id},
            },
        )
        campo = next(
            (
                p["id"]
                for s in props.get("sections", [])
                for p in s.get("properties", [])
                if p.get("kind") == "color"
            ),
            None,
        )
        if campo is None:
            raise RuntimeError("El hatch no expone propiedad de color")
        self.mcp.execute(
            self.sid,
            {
                "op": "property",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "field": campo,
                "value": valor_color,
            },
        )
        print(f"Poligono con relleno {valor_color} en '{capa}': {h_relleno}")
        return {"contorno": h_contorno, "relleno": h_relleno}

    JUSTIFICACIONES = {
        "L": "L",
        "LEFT": "L",
        "IZQUIERDA": "L",
        "C": "C",
        "CENTER": "C",
        "CENTRO": "C",
        "R": "R",
        "RIGHT": "R",
        "DERECHA": "R",
        "A": "A",
        "ALIGNED": "A",
        "ALINEADO": "A",
        "M": "M",
        "MIDDLE": "M",
        "MEDIO": "M",
        "F": "F",
        "FIT": "F",
        "AJUSTE": "F",
        "TL": "TL",
        "TC": "TC",
        "TR": "TR",
        "ML": "ML",
        "MC": "MC",
        "MR": "MR",
        "BL": "BL",
        "BC": "BC",
        "BR": "BR",
    }

    def bloque_desde_fichero(
        self, ruta, punto=None, escala=1.0, rotacion=0.0, recrear=False, insertar=False
    ):
        """Crea un bloque a partir de un DWG/DXF externo (via -XREF Attach).

        ruta: fichero .dwg o .dxf existente (se usa su ruta absoluta; el
            bloque queda como referencia externa vinculada: no borres ni
            muevas el fichero o la referencia quedara rota).
        punto: insercion [x, y] o [x, y, z] (por defecto origen).
        escala: factor != 0. rotacion: grados.
        ligar: si True, incrusta el contenido (Attach -> Explode del
            inserto -> block_define -> Detach): no exige dibujo guardado
            y el fichero externo deja de ser necesario. Si False (defecto),
            queda como referencia externa vinculada.
        recrear: si True y el bloque ya existe, lo elimina (Detach) y lo
            vuelve a crear. Si False (defecto) y existe, reutiliza la
            definicion e inserta una nueva referencia en punto/escala/
            rotacion.
        insertar: si False, solo define el bloque (borra el inserto
            automatico) y devuelve (nombre, None). La insercion queda
            para otro metodo.
        Devuelve (nombre_bloque, handle_insert o None). Reversible con Undo.
        """
        ligar = True
        if punto is None:
            punto = [0, 0, 0]
        if not isinstance(ruta, str) or not ruta.strip():
            raise ValueError("ruta debe ser un texto no vacio")
        abspath = os.path.abspath(ruta.strip())
        if not os.path.isfile(abspath):
            raise ValueError(f"No existe el fichero: {abspath}")
        if os.path.splitext(abspath)[1].lower() not in (".dwg", ".dxf"):
            raise ValueError(f"Extension no soportada (solo .dwg/.dxf): {abspath}")
        esc = float(escala)
        if esc != esc or esc in (float("inf"), float("-inf")) or esc == 0.0:
            raise ValueError(f"Escala {escala!r}: debe ser finita y != 0")
        rot = float(rotacion)
        if rot != rot or rot in (float("inf"), float("-inf")):
            raise ValueError(f"Rotacion {rotacion!r}: debe ser finita")
        stem = os.path.splitext(os.path.basename(abspath))[0]
        existentes = self._nombres_bloques()
        real = next((b for b in existentes if b.lower() == stem.lower()), None)
        if real is not None and not recrear:
            if not insertar:
                return real, None
            return self._insertar_bloque(real, punto, esc, rot)
        if real is not None:
            try:  # Detach best-effort para recrear limpio
                self.mcp.execute(
                    self.sid,
                    {
                        "op": "run",
                        "request_id": self.mcp.nuevo_id("ej"),
                        "document_id": self.doc_id,
                        "cmd": f"-XREF Detach {real}",
                    },
                    detail="compact",
                )
            except Exception:
                pass
            existentes = self._nombres_bloques()
        rot_txt = _fmt_num(rot)  # "0" -> "0.0": el 0 escueto lo rechaza XREF
        bloques_antes = self._nombres_bloques()
        antes = self._todos_handles()
        base = self.mcp.tool(
            "ocs_execute",
            {
                "ocs_session_id": self.sid,
                "request": {
                    "op": "run",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "cmd": f"-XREF Attach {abspath} {_fmt_punto(punto)} {_fmt_num(esc)}",
                },
                "response_detail": "compact",
                "wait_seconds": 5,
            },
        )
        if base.get("status") != "completed":
            # Se espera waiting_input en el angulo de rotacion (el run
            # con la rotacion inline la rechaza si vale 0): se completa
            # con input guiado, que si acepta el "0.0".
            estado = self.mcp.tool(
                "ocs_read", {"ocs_session_id": self.sid, "op": "state"}
            )
            cmd = estado.get("command") or {}
            if "token" not in cmd.get("accepts", []):
                raise RuntimeError(f"XREF no arranco: {base}")
            r2 = self.mcp.execute(
                self.sid,
                {
                    "op": "input",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "kind": "token",
                    "text": rot_txt,
                },
                detail="compact",
            )
            assert r2["status"] == "completed", r2
        nuevos_bloques = self._nombres_bloques() - bloques_antes
        assert nuevos_bloques, f"XREF no creo bloque: {base}"
        nombre = sorted(nuevos_bloques)[0]
        nuevos = self._todos_handles() - antes
        tipos_ref = ("Insert", "Block Reference")
        inserts = [h for h in nuevos if self._tipo_de(h) in tipos_ref]
        if not inserts:
            # Localizar la referencia por diff aunque el diario no la reporte.
            q = self.mcp.tool(
                "ocs_read",
                {
                    "ocs_session_id": self.sid,
                    "op": "query",
                    "parameters": {
                        "handles": sorted(nuevos),
                        "detail": "summary",
                        "limit": len(nuevos) or 1,
                    },
                },
            )
            inserts = [
                e["handle"] for e in q.get("entities", []) if e.get("type") in tipos_ref
            ]
        assert inserts, (nombre, sorted(nuevos))
        handle = self._ordenar_handles(inserts)[-1]
        print(f"Bloque '{nombre}' desde {abspath} -> inserto {handle}")
        if ligar:
            return self._ligar_xref(nombre, handle, insertar)
        if not insertar:
            self.mcp.execute(
                self.sid,
                {
                    "op": "entities_delete",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "handles": [handle],
                },
                detail="compact",
            )
            print(f"Bloque '{nombre}' definido (sin insertar).")
            return nombre, None
        return nombre, handle

    def _ligar_xref(self, nombre, handle, insertar=True):
        """Incrusta un xref: Explode -> block_define -> Detach.

        El contenido ya quedo transformado (punto/escala/rotacion del
        Attach), asi que se redefine con ese mismo nombre y el inserto
        conserva la colocacion. Con insertar=False se borra el inserto
        automatico. Devuelve (nombre, handle|None).
        """
        antes = self._todos_handles()
        resp = self.mcp.execute(
            self.sid,
            {
                "op": "batch",
                "request_id": self.mcp.nuevo_id("ej"),
                "steps": [
                    {"op": "select", "document_id": self.doc_id, "handles": [handle]},
                    {"op": "run", "document_id": self.doc_id, "cmd": "EXPLODE"},
                ],
            },
            detail="changed_entities",
        )
        assert resp["status"] == "completed", resp
        assert resp["completed_steps"] == 2, resp
        despues = self._todos_handles()
        explotados = sorted(h for h in despues - antes if h in despues)
        assert explotados, f"EXPLODE no produjo entidades: {resp}"
        r2 = self.mcp.execute(
            self.sid,
            {
                "op": "run",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "cmd": f"-XREF Detach {nombre}",
            },
            detail="compact",
        )
        assert r2["status"] == "completed", r2
        quedan = [h for h in explotados if h in self._todos_handles()]
        assert quedan, "Detach se llevo las entidades explotadas"
        r3 = self.mcp.execute(
            self.sid,
            {
                "op": "block_define",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "name": nombre,
                "base": [0, 0, 0],
                "handles": quedan,
            },
            detail="compact",
        )
        assert r3.get("status") == "completed", r3
        nuevo = (r3.get("result") or {}).get("insert")
        assert nuevo, r3
        if not insertar:
            self.mcp.execute(
                self.sid,
                {
                    "op": "entities_delete",
                    "request_id": self.mcp.nuevo_id("ej"),
                    "document_id": self.doc_id,
                    "handles": [nuevo],
                },
                detail="compact",
            )
            print(f"Bloque '{nombre}' ligado (incrustado, sin insertar).")
            return nombre, None
        print(f"Bloque '{nombre}' ligado (incrustado) -> inserto {nuevo}")
        return nombre, nuevo

    def insertar_bloque(self, nombre, punto=None, escala=1.0, rotacion=0.0):
        """Inserta una referencia a un bloque existente del dibujo.

        nombre: exacto de block_records (insensible a mayusculas).
        punto/escala/rotacion como en bloque_desde_fichero (la escala y
        la rotacion se aplican despues con SCALE/ROTATE).
        Devuelve (nombre, handle_insert). Reversible con Undo.
        """
        if punto is None:
            punto = [0, 0, 0]
        if not isinstance(nombre, str) or not nombre.strip():
            raise ValueError("nombre debe ser un texto no vacio")
        esc = float(escala)
        if esc != esc or esc in (float("inf"), float("-inf")) or esc == 0.0:
            raise ValueError(f"Escala {escala!r}: debe ser finita y != 0")
        rot = float(rotacion)
        if rot != rot or rot in (float("inf"), float("-inf")):
            raise ValueError(f"Rotacion {rotacion!r}: debe ser finita")
        existentes = self._nombres_bloques()
        real = next(
            (b for b in existentes if b.lower() == nombre.strip().lower()), None
        )
        if real is None:
            raise ValueError(f"No existe el bloque '{nombre}' en el dibujo")
        return self._insertar_bloque(real, punto, esc, rot)

    def _insertar_bloque(self, nombre, punto, esc, rot):
        """Inserta una referencia a un bloque existente (flujo guiado).

        nombre: exacto de block_records. punto/escala/rotacion ya
            validados. La escala/rotacion se aplican despues con
            SCALE/ROTATE (evita los tokens de opcion del prompt).
        Devuelve (nombre, handle_insert).
        """
        antes = self._todos_handles()
        resp = self.mcp.execute(
            self.sid,
            {
                "op": "start",
                "request_id": self.mcp.nuevo_id("ej"),
                "document_id": self.doc_id,
                "cmd": "INSERT",
            },
            detail="full",
        )
        cmd = (resp.get("state") or {}).get("command") or {}
        if "token" not in cmd.get("accepts", []):
            raise RuntimeError(f"INSERT no arranco: {resp}")
        for peticion in (
            {"op": "input", "kind": "token", "text": nombre},
            {
                "op": "input",
                "kind": "point",
                "point": [
                    float(punto[0]),
                    float(punto[1]),
                    float(punto[2] if len(punto) > 2 else 0),
                ],
                "space": "wcs",
            },
        ):
            resp = self.mcp.execute(
                self.sid,
                dict(
                    peticion,
                    request_id=self.mcp.nuevo_id("ej"),
                    document_id=self.doc_id,
                ),
                detail="full",
            )
        assert resp.get("status") == "completed", resp
        nuevos = self._todos_handles() - antes
        tipos_ref = ("Insert", "Block Reference")
        inserts = [h for h in nuevos if self._tipo_de(h) in tipos_ref]
        assert inserts, sorted(nuevos)
        handle = self._ordenar_handles(inserts)[-1]
        if esc != 1.0:
            self.ampliar(handle, list(punto), esc)
        if rot != 0.0:
            self.rotar(handle, list(punto), rot)
        print(f"Insertado '{nombre}' en {punto} -> {handle}")
        return nombre, handle

    def _nombres_bloques(self):
        """Nombres de block_records (para diff antes/despues)."""
        rec = self.mcp.tool(
            "ocs_read",
            {
                "ocs_session_id": self.sid,
                "op": "records",
                "parameters": {"collection": "block_records"},
            },
        )
        return {r.get("name") for r in rec.get("records", []) if r.get("name")}

    def _asegurar_capa(self, nombre: str, color: int = 2):
        """Nombre real de la capa (insensible a mayusculas) o la crea.

        Evita el choque LAYER NEW cuando existe con otras mayusculas
        (p. ej. 'margen' frente a 'Margen'): reutiliza sin tocar color.
        """
        capas = self.mcp.tool(
            "ocs_read", {"ocs_session_id": self.sid, "op": "layers", "parameters": {}}
        )
        for c in capas.get("layers", []):
            if str(c.get("name", "")).lower() == nombre.lower():
                return c["name"]
        self.creaCapa(nombre, color)
        return nombre

    def circulo(self, centro: list, radio: float, capa: str = ""):
        """Dibuja CIRCLE centro + radio. Devuelve el handle."""
        r = float(radio)
        if r != r or r in (float("inf"), float("-inf")) or r <= 0.0:
            raise ValueError(f"Radio {radio!r}: debe ser finito y > 0")
        handles = self._corre_batch(
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"CIRCLE {_fmt_punto(centro)} {_fmt_num(r)}",
                }
            ]
        )
        if capa:
            self.asignaCapa(handles, self._asegurar_capa(capa))
        return handles[0]

    def arco_3p(self, p1: list, p2: list, p3: list, capa: str = ""):
        """Dibuja ARC por 3 puntos (inicial, intermedio, final).

        Rechaza puntos colineales (no definen un arco unico).
        Devuelve el handle.
        """
        pts = []
        for p in (p1, p2, p3):
            _fmt_punto(p)  # valida 2 o 3 coordenadas finitas
            pts.append([float(p[0]), float(p[1])])
        (x1, y1), (x2, y2), (x3, y3) = pts
        if abs((x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)) < 1e-9:
            raise ValueError(f"Puntos colineales: {p1!r} {p2!r} {p3!r}")
        handles = self._corre_batch(
            [
                {
                    "op": "run",
                    "document_id": self.doc_id,
                    "cmd": f"ARC {_fmt_punto(p1)} {_fmt_punto(p2)} {_fmt_punto(p3)}",
                }
            ]
        )
        if capa:
            self.asignaCapa(handles, self._asegurar_capa(capa))
        return handles[0]

    def texto(
        self,
        cadena,
        punto: list,
        rotacion: float = 0,
        altura: float = 1,
        capa: str = "",
        justificacion: str = "",
        punto2: list | None = None,
    ):
        """Crea TEXT guiado (justificacion/punto/altura/rotacion/editor).

        cadena: una linea (str) o varias (lista, o str con "\\n"). Cada
            linea crea una entidad TEXT debajo de la anterior (paso
            altura x 1.666). Las lineas vacias se rechazan: un commit
            vacio TERMINA el comando en vez de crear linea.
        justificacion: token CAD ("L","C","R","A","M","F","TL","TC",
            "TR","ML","MC","MR","BL","BC","BR") o nombre EN/ES
            ("centro", "derecha", ...). "" = justificar a la izquierda.
            "A"/"F" necesitan punto2 (alineacion en dos puntos).
        punto2: segundo punto para justificaciones Aligned/Fit.
        Devuelve el handle (una linea) o la lista de handles (varias).
        """
        if isinstance(cadena, str):
            lineas = cadena.splitlines()
        else:
            lineas = list(cadena)
        if not lineas or any(not ln.strip() for ln in lineas):
            raise ValueError("texto necesita 1+ lineas no vacias")
        multiple = len(lineas) > 1 or not isinstance(cadena, str)
        just = self.JUSTIFICACIONES.get(justificacion.strip().upper(), "")
        if justificacion and not just:
            raise ValueError(
                f"Justificacion {justificacion!r} no valida. Usa: "
                "L C R A M F TL TC TR ML MC MR BL BC BR"
            )
        if just in ("A", "F") and punto2 is None:
            raise ValueError("Justificacion Aligned/Fit necesita punto2")
        if len(punto) == 2:
            punto = [punto[0], punto[1], 0]
        if len(punto) != 3:
            raise ValueError(f"Punto {punto!r}: se esperan 2 o 3 coordenadas")
        if punto2 is not None:
            if len(punto2) == 2:
                punto2 = [punto2[0], punto2[1], 0]
            if len(punto2) != 3:
                raise ValueError(f"Punto2 {punto2!r}: 2 o 3 coordenadas")
        previos = self.textos_existentes()

        def enviar(peticion, detail="full"):
            return self.mcp.execute(
                self.sid, dict(peticion, document_id=self.doc_id), detail=detail
            )

        def cancelar():
            try:
                enviar({"op": "cancel", "request_id": self.mcp.nuevo_id("ej-t")})
            except Exception:
                pass

        resp = enviar(
            {"op": "start", "request_id": self.mcp.nuevo_id("ej-text"), "cmd": "TEXT"}
        )
        punto_enviado = False
        punto2_enviado = False
        fase_just = 0  # 0=nada, 1=opcion J enviada, 2=justificacion fijada
        pendientes = list(lineas)
        try:
            for _ in range(10 + 2 * len(lineas)):  # cota de seguridad
                estado = resp.get("state", {})
                if estado.get("text_editor"):
                    if pendientes:
                        # Editor abierto: escribir la siguiente linea y
                        # confirmar. TEXT reabre abajo para la proxima.
                        linea = pendientes.pop(0)
                        print(f"  editor inline -> text_input {linea!r} + commit")
                        enviar(
                            {
                                "op": "action",
                                "request_id": self.mcp.nuevo_id("ej-t"),
                                "name": "text_input",
                                "value": linea,
                            }
                        )
                        resp = enviar(
                            {
                                "op": "action",
                                "request_id": self.mcp.nuevo_id("ej-t"),
                                "name": "text_commit",
                            }
                        )
                        continue
                    # Sin lineas pendientes: commit en vacio equivale a Enter
                    # y termina el comando (on_editor_closed(false) -> Cancel).
                    print("  editor vacio -> text_commit para terminar")
                    resp = enviar(
                        {
                            "op": "action",
                            "request_id": self.mcp.nuevo_id("ej-t"),
                            "name": "text_commit",
                        }
                    )
                    if resp.get("status") == "completed":
                        nuevos = self.textos_existentes() - previos
                        assert len(nuevos) == len(lineas), (nuevos, lineas)
                        print(f"Texto creado ({len(lineas)} lineas).")
                        handles = self._ordenar_handles(nuevos)
                        if capa:
                            self.asignaCapa(handles, capa)
                        return handles if multiple else handles[0]
                    continue
                cmd = estado.get("command")
                if not cmd:
                    if resp.get("status") == "completed":
                        print("Comando terminado.")
                        return None
                    raise RuntimeError(f"Estado inesperado: {json.dumps(resp)}")
                acepta = cmd.get("accepts", [])
                bajo = cmd.get("prompt", "").lower()
                print(
                    f"  prompt: {cmd.get('prompt', '').splitlines()[-1]!r} "
                    f"acepta={acepta}"
                )
                if (
                    just
                    and fase_just == 0
                    and "token" in acepta
                    and ("justif" in bajo or "justify" in bajo)
                ):
                    pet = {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej-t"),
                        "kind": "token",
                        "text": "J",
                    }
                    fase_just = 1
                elif just and fase_just == 1 and "token" in acepta:
                    pet = {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej-t"),
                        "kind": "token",
                        "text": just,
                    }
                    fase_just = 2
                elif "point" in acepta and not punto_enviado:
                    pet = {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej-t"),
                        "kind": "point",
                        "point": [float(punto[0]), float(punto[1]), float(punto[2])],
                        "space": "wcs",
                    }
                    punto_enviado = True
                elif (
                    "point" in acepta
                    and punto_enviado
                    and punto2 is not None
                    and not punto2_enviado
                ):
                    # Segundo punto (justificaciones Aligned/Fit).
                    pet = {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej-t"),
                        "kind": "point",
                        "point": [float(punto2[0]), float(punto2[1]), float(punto2[2])],
                        "space": "wcs",
                    }
                    punto2_enviado = True
                elif "token" in acepta and ("height" in bajo or "altura" in bajo):
                    pet = {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej-t"),
                        "kind": "token",
                        "text": str(altura),
                    }
                elif "token" in acepta and (
                    "rot" in bajo or "angle" in bajo or "ngulo" in bajo
                ):
                    pet = {
                        "op": "input",
                        "request_id": self.mcp.nuevo_id("ej-t"),
                        "kind": "token",
                        "text": str(rotacion),
                    }
                else:
                    raise RuntimeError(f"Paso no previsto: {json.dumps(cmd)}")
                resp = enviar(pet)
        finally:
            cancelar()
        raise RuntimeError("TEXT no termino en los pasos previstos.")

    # -- textos masivos (rapido) --------------------------------------
    def textos(
        self,
        items,
        capa: str = "",
        altura: float = 1,
        justificacion: str = "",
        verbose: bool = False,
    ):
        """Crea muchos TEXT de una linea de forma rapida.

        items: iterable de (cadena, punto) o (cadena, punto, rotacion).
            punto es [x, y] o [x, y, z].
        capa: si se indica, mueve TODOS los textos a esa capa al final
            (una sola operacion, no una por texto).
        Devuelve la lista de handles en orden de creacion.

        Cada texto cuesta 3 llamadas MCP (run + text_input + [commit,
        cancel]) frente a ~9 de texto(); pensado para miles de textos.
        """
        items = list(items)
        if not items:
            return []
        just = self.JUSTIFICACIONES.get(justificacion.strip().upper(), "")
        if justificacion and not just:
            raise ValueError(
                f"Justificacion {justificacion!r} no valida. Usa: "
                "L C R A M F TL TC TR ML MC MR BL BC BR"
            )
        for it in items:
            if not str(it[0]).strip():
                raise ValueError("textos: cadena vacia no permitida")
        # Una sola sonda: el comando TEXT pide altura salvo en estilos de
        # altura fija (entonces se omite ese token).
        pide_altura = self._texto_pide_altura(items[0][1])
        handles = []
        for i, it in enumerate(items):
            cadena, punto = it[0], it[1]
            rot = it[2] if len(it) > 2 else 0
            handles.append(
                self._texto_rapido(cadena, punto, rot, altura, just, pide_altura)
            )
            if verbose and (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(items)} textos...")
        if capa:
            self.asignaCapa(handles, capa)
        return handles

    def _texto_pide_altura(self, punto) -> bool:
        """Sonda (una vez): True si TEXT pide altura con el estilo actual."""
        base = {"document_id": self.doc_id}
        self.mcp.execute(
            self.sid,
            dict(
                {"op": "start", "request_id": self.mcp.nuevo_id("t"), "cmd": "TEXT"},
                **base,
            ),
        )
        r = self.mcp.execute(
            self.sid,
            dict(
                {
                    "op": "input",
                    "request_id": self.mcp.nuevo_id("t"),
                    "kind": "point",
                    "point": [
                        float(punto[0]),
                        float(punto[1]),
                        float(punto[2] if len(punto) > 2 else 0),
                    ],
                    "space": "wcs",
                },
                **base,
            ),
        )
        cmd = (r.get("state") or {}).get("command") or {}
        bajo = (cmd.get("prompt") or "").lower()
        try:
            self.mcp.execute(
                self.sid,
                dict({"op": "cancel", "request_id": self.mcp.nuevo_id("t")}, **base),
            )
        except RuntimeError:
            pass  # cancel devuelve status "cancelled"
        return "height" in bajo or "altura" in bajo

    def _texto_rapido(self, cadena, punto, rotacion, altura, just, pide_altura):
        """Un TEXT de una linea en 3 llamadas. Devuelve el handle."""
        base = {"document_id": self.doc_id}
        partes = ["TEXT"]
        if just and just != "L":  # L es la justificacion por defecto
            partes += ["J", just]
        partes.append(_fmt_punto(punto))
        if pide_altura:
            partes.append(_fmt_num(float(altura)))
        partes.append(_fmt_num(float(rotacion)))
        # 1) run: define el texto y abre el editor inline.
        self.mcp.execute(
            self.sid,
            dict(
                {
                    "op": "run",
                    "request_id": self.mcp.nuevo_id("t"),
                    "cmd": " ".join(partes),
                },
                **base,
            ),
        )
        # 2) contenido.
        self.mcp.execute(
            self.sid,
            dict(
                {
                    "op": "action",
                    "request_id": self.mcp.nuevo_id("t"),
                    "name": "text_input",
                    "value": str(cadena),
                },
                **base,
            ),
        )
        # 3) commit + cancel: el commit crea el texto y reabre el editor;
        #    el cancel lo cierra. El batch queda "cancelled" a proposito,
        #    por eso se llama al RPC crudo y no a execute().
        res = self.mcp._rpc(
            "tools/call",
            {
                "name": "ocs_execute",
                "arguments": {
                    "ocs_session_id": self.sid,
                    "request": {
                        "op": "batch",
                        "request_id": self.mcp.nuevo_id("t"),
                        "steps": [
                            {
                                "op": "action",
                                "document_id": self.doc_id,
                                "name": "text_commit",
                            },
                            {"op": "cancel", "document_id": self.doc_id},
                        ],
                    },
                    "response_detail": "changed_entities",
                },
            },
        )
        sc = res.get("structuredContent", {})
        nuevos = [e["handle"] for e in sc.get("changed_entities", [])]
        if not nuevos:
            raise RuntimeError(f"texto no creado: {json.dumps(sc)[:300]}")
        return nuevos[-1]

    @staticmethod
    def _ordenar_handles(handles):
        """Ordena handles hex por valor numerico (= orden de creacion)."""
        return sorted(handles, key=lambda h: int(h, 16))
