import argparse
import json
import subprocess
import time


# --------------------------------------------------------------------------
# Cliente MCP minimo (JSON-RPC por stdio, una linea por mensaje).
# --------------------------------------------------------------------------
class OcsSessionError(RuntimeError):
    """No hay sesion de OpenCADStudio con un dibujo abierto.

    Se lanza en lugar de SystemExit para que el programa que importa
    esta libreria pueda capturarlo (except Exception) y decidir.
    """


# Tools del servidor MCP que esta libreria usa de verdad. El resto del
# catalogo (ocs_query, ocs_render, ocs_measure...) es optativo y puede
# aparecer o desaparecer entre versiones sin que nos afecte.
TOOLS_REQUERIDAS = frozenset(
    {"ocs_sessions", "ocs_read", "ocs_execute"}
)


def detalle_fallo(sc):
    """Mensaje de error legible a partir del payload de un fallo.

    En vez de escupir el diccionario entero, resume el ``code`` y el
    ``error`` del conjunto, el paso de batch que fallo (si lo hay) y lo
    que haya quedado abierto en el estado (modal, comando, editores).
    """
    partes = []
    codigo = sc.get("code") or ""
    texto = sc.get("error") or ""
    if codigo or texto:
        partes.append(f"{codigo} {texto}".strip())
    for r in sc.get("results") or []:
        if r.get("ok") is not False and r.get("status") in (None, "completed"):
            continue
        motivo = f"{r.get('code') or ''} {r.get('error') or ''}".strip()
        partes.append(
            f"paso {r.get('step')} ({r.get('op')}): {motivo or r.get('status')}"
        )
    estado = sc.get("state") or {}
    for clave, etiqueta in (
        ("modal", "modal abierto"),
        ("command", "comando activo"),
        ("text_editor", "editor de texto inline"),
        ("mtext_editor", "editor MTEXT"),
        ("block_editor", "editor de bloque"),
    ):
        if estado.get(clave):
            partes.append(f"{etiqueta}: {estado[clave]}")
    if not partes:
        partes.append(json.dumps(sc, ensure_ascii=False)[:400])
    return "; ".join(partes)


class OcsMcp:
    def __init__(self, exe):
        try:
            self.proc = subprocess.Popen(
                [exe, "--mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"No encuentro el ejecutable: {exe}")
        assert self.proc.stdin and self.proc.stdout
        self._id = 0
        self._req = 0

    # -- transporte ------------------------------------------------------
    def _rpc(self, method, params):
        self._id += 1
        self.proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            )
            + "\n"
        )
        self.proc.stdin.flush()
        linea = self.proc.stdout.readline()
        if not linea:
            raise RuntimeError("El servidor MCP cerro la conexion")
        resp = json.loads(linea)
        if "error" in resp:
            raise RuntimeError(f"Error RPC: {resp['error']}")
        return resp["result"]

    def _notify(self, method, params):
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        )
        self.proc.stdin.flush()

    # -- herramientas ----------------------------------------------------
    @staticmethod
    def _desempaquetar(name, res):
        """Devuelve el payload de una tools/call normalizado.

        Desde 2026.44 una respuesta fallida trae ``structuredContent``
        vacio y el JSON real en ``content[0].text``: aqui se lee siempre
        el que haya, para no perder el codigo y el mensaje de error.
        """
        sc = res.get("structuredContent") or {}
        if sc:
            return sc
        texto = "".join(c.get("text", "") for c in res.get("content") or [])
        try:
            cargado = json.loads(texto)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "code": "respuesta_invalida",
                "error": (texto.strip() or "sin contenido")[:400],
            }
        if isinstance(cargado, dict):
            return cargado
        # Una lista u otro escalar no es un payload utilizable.
        return {"ok": False, "code": "respuesta_invalida", "raw": cargado}

    @staticmethod
    def _es_stale(sc):
        """True si la operacion fallo por estado desactualizado.

        En ``op: "batch"`` el codigo viene dentro de ``results[].code``
        y no en el nivel superior. Solo cuenta si TODOS los pasos
        fallidos son ``stale_state``: si alguno avanzo, reintentar
        duplicaria el trabajo ya hecho.
        """
        if sc.get("code") == "stale_state":
            return True
        resultados = sc.get("results") or []
        fallas = [r for r in resultados if r.get("ok") is False]
        return bool(fallas) and all(
            r.get("code") == "stale_state" for r in fallas
        )

    def tool(self, name, arguments, permitir_stale=False):
        """Llama a una tool y devuelve su payload (o lanza error)."""
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        sc = self._desempaquetar(name, res)
        if permitir_stale and self._es_stale(sc):
            return sc
        if sc.get("ok") is False or (
            res.get("isError") and sc.get("ok") is not True
        ):
            raise RuntimeError(f"{name} fallo: {detalle_fallo(sc)}")
        return sc

    def handshake(self):
        init = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "ocs-ejemplo", "version": "1"},
            },
        )
        self.server = init["serverInfo"]
        print(f"Servidor: {self.server['name']} {self.server['version']}")
        self._notify("notifications/initialized", {})
        tools = self._rpc("tools/list", {})
        nombres = {t["name"] for t in tools["tools"]}
        # El servidor amplia su catalogo con el tiempo (2026.44 expone 20
        # tools): basta con que esten las que esta libreria usa.
        faltan = sorted(TOOLS_REQUERIDAS - nombres)
        if faltan:
            raise RuntimeError(
                f"El servidor no expone las tools necesarias: {faltan} "
                f"(servidor {self.server['version']}, {len(nombres)} tools)"
            )
        usadas = ", ".join(sorted(TOOLS_REQUERIDAS & nombres))
        print(f"Tools: {len(nombres)} disponibles (uso: {usadas})")

    def execute(
        self, session_id, request, detail="compact", wait=30.0, reintentos_stale=1
    ):
        """ocs_execute con request_id unico y reintento ante stale_state."""
        args = {
            "ocs_session_id": session_id,
            "request": request,
            "response_detail": detail,
            "wait_seconds": wait,
        }
        sc = self.tool("ocs_execute", args, permitir_stale=True)
        if self._es_stale(sc) and reintentos_stale > 0:
            # Otro cliente toco el documento entre medias: refrescar y
            # reintentar UNA vez con un request_id nuevo.
            self.tool("ocs_read", {"ocs_session_id": session_id, "op": "state"})
            request = dict(request, request_id=self.nuevo_id("ej"))
            return self.execute(session_id, request, detail, wait, reintentos_stale - 1)
        estado = sc.get("status")
        if estado in ("accepted", "running"):
            sc = self._esperar_operacion(session_id, request["request_id"])
        if sc.get("ok") is False or sc.get("status") in ("failed", "cancelled"):
            raise RuntimeError(f"Operacion fallo: {detalle_fallo(sc)}")
        return sc

    def _esperar_operacion(self, session_id, request_id, tope=30.0):
        t0 = time.time()
        while time.time() - t0 < tope:
            time.sleep(0.5)
            sc = self.tool(
                "ocs_read",
                {
                    "ocs_session_id": session_id,
                    "op": "operation",
                    "parameters": {"request_id": request_id},
                },
            )
            if sc.get("status") not in ("accepted", "running"):
                return sc
        raise RuntimeError("Operacion sin terminar tras esperar")

    def nuevo_id(self, prefijo):
        self._req += 1
        return f"{prefijo}-{int(time.time())}-{self._req}"

    def cerrar(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def elegir_sesion(mcp):
    """Localiza la sesion con un dibujo abierto (no la pestana Start)."""
    sesiones = mcp.tool("ocs_sessions", {"launch_if_none": False}).get("result", [])
    for s in sesiones:
        dibujos = [d for d in s.get("documents", []) if not d.get("start")]
        if not dibujos:
            continue
        activa = s.get("document_id")
        if any(d["id"] == activa for d in dibujos):
            return s["session_id"], activa
        # Hay dibujo pero en otra pestana: la activaremos despues.
        return s["session_id"], dibujos[0]["id"]
    raise OcsSessionError(
        "No hay ninguna sesion con un dibujo abierto.\n"
        "Abre OpenCADStudio con un dibujo (NEW u OPEN) y vuelve a intentarlo."
    )
