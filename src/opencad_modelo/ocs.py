import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections import deque


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

# Version del servidor contra la que esta probada la libreria. Si aparece
# otra se avisa y se sigue: lo que manda es TOOLS_REQUERIDAS.
VERSION_PROBADA = "2026.44"
PROTOCOLO = "2025-11-25"

# Ruta tipica en una instalacion del sistema (Windows usa 'win32').
BIN_POR_DEFECTO = "/usr/local/bin/opencad-studio"

# Rutas habituales, en orden, para cuando ni el PATH ni OCS_BIN lo traen.
# Asi funciona con instalaciones en otro prefijo (/usr/local, /usr, /opt)
# o de usuario (~/.local, %LOCALAPPDATA%) sin tocar nada.
_CANDIDATAS = {
    "linux": [
        BIN_POR_DEFECTO,
        "/usr/bin/opencad-studio",
        "/opt/opencad-studio/opencad-studio",
        "~/.local/bin/opencad-studio",
        "~/bin/opencad-studio",
    ],
    "win32": [
        r"%LOCALAPPDATA%\Programs\OpenCADStudio\opencad-studio.exe",
        r"C:\Program Files\OpenCADStudio\opencad-studio.exe",
        r"C:\Program Files (x86)\OpenCADStudio\opencad-studio.exe",
    ],
}


def _a_argv(valor):
    """Convierte un binario (ruta o linea de comando) en lista argv.

    Acepta una lista ya preparada o un texto; el texto se parte como lo
    haria el shell para permitir cosas como
    ``OCS_BIN="flatpak run org.opencad.Studio"`` o una AppImage.
    """
    if isinstance(valor, (list, tuple)):
        return [str(v) for v in valor]
    texto = str(valor).strip()
    if not texto:
        return []
    # posix=False (Windows) conserva las comillas: se quitan aqui.
    return [
        parte[1:-1]
        if len(parte) > 1 and parte[0] == parte[-1] and parte[0] in "\"'"
        else parte
        for parte in shlex.split(texto, posix=os.name != "nt")
    ]


def candidatas(plataforma=None, entorno=None):
    """Rutas habituales del binario para la plataforma indicada.

    ``entorno`` permite probar las rutas de Windows sin estar en
    Windows (por defecto, las variables del proceso).
    """
    actual = plataforma if plataforma is not None else sys.platform
    claves = ["win32"] if actual.startswith("win") else ["linux"]
    variables = entorno if entorno is not None else os.environ
    rutas = []
    for clave in claves:
        rutas.extend(_CANDIDATAS[clave])
    return [_expandir(ruta, variables) for ruta in rutas]


def _expandir(ruta, entorno):
    """Expande ``~`` y ``%VAR%`` (estilo Windows) con ese entorno."""
    if ruta.startswith("~"):
        ruta = os.path.expanduser(ruta)

    def sustituir(aparicion):
        return entorno.get(aparicion.group(1), aparicion.group(0))

    return re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", sustituir, ruta)


def _resolver(argv):
    """Devuelve el argv con el ejecutable resuelto, o None si no existe."""
    if not argv:
        return None
    exe = argv[0]
    ruta = exe if os.path.sep in exe else shutil.which(exe)
    if ruta and os.path.isfile(ruta) and os.access(ruta, os.X_OK):
        return [ruta, *argv[1:]]
    return None


def encontrar_binario(explicito=None):
    """Localiza el ejecutable de OpenCADStudio y devuelve su argv.

    Orden: lo pedido explicitamente (``Modelo(bin=...)``), la variable
    ``OCS_BIN``, el ``PATH`` y por ultimo las rutas habituales del
    sistema. Lo explicito manda: si no existe, se avisa en vez de
    caer a otra instalacion por sorpresa.
    """
    for origen, valor in (
        ("bin=", explicito),
        ("OCS_BIN", os.environ.get("OCS_BIN")),
    ):
        if not valor:
            continue
        argv = _resolver(_a_argv(valor))
        if argv is None:
            raise FileNotFoundError(
                f"No encuentro el ejecutable de OpenCADStudio "
                f"({origen}{valor!r}). Revisa la ruta o quita {origen} "
                f"para que se busque sola."
            )
        return argv

    probadas = []
    for nombre in ("opencad-studio", "opencad-studio.exe"):
        ruta = shutil.which(nombre)
        if ruta:
            return [ruta]
        probadas.append(f"PATH: {nombre}")
    for ruta in candidatas():
        if os.path.isfile(ruta) and os.access(ruta, os.X_OK):
            return [ruta]
        probadas.append(ruta)
    raise FileNotFoundError(
        "No encuentro el ejecutable de OpenCADStudio. Instalalo o define "
        "OCS_BIN=/ruta/al/binario (o Modelo(bin=...)). Rutas probadas: "
        + ", ".join(probadas)
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
    def __init__(self, exe=None):
        argv = encontrar_binario(exe)
        try:
            self.proc = subprocess.Popen(
                [*argv, "--mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"No encuentro el ejecutable: {argv[0]}")
        assert self.proc.stdin and self.proc.stdout
        self._id = 0
        self._req = 0
        # El stderr se guarda (antes iba a DEVNULL) para poder explicar
        # POR QUE no arranco: un binario sin --mcp o un editor que no
        # esta abierto fallan justo ahi y sin mensaje parecia un cuelgue.
        # Un hilo lo vacia para que el pipe no se llene y bloquee.
        self._stderr = deque(maxlen=40)
        threading.Thread(target=self._vaciar_stderr, daemon=True).start()

    def _vaciar_stderr(self):
        stderr = self.proc.stderr
        if stderr is None:
            return
        for linea in stderr:
            self._stderr.append(linea.rstrip())

    def _motivo_salida(self):
        """Resumen de como termino el proceso y lo que dijo por stderr."""
        partes = []
        codigo = self.proc.poll()
        if codigo is not None:
            partes.append(f"salio con codigo {codigo}")
        salida = [linea for linea in self._stderr if linea.strip()]
        if salida:
            partes.append("stderr: " + " | ".join(salida[-8:]))
        return "; ".join(partes)

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
            motivo = self._motivo_salida()
            raise RuntimeError(
                "El servidor MCP cerro la conexion"
                + (f" ({motivo})" if motivo else "")
            )
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
                "protocolVersion": PROTOCOLO,
                "capabilities": {},
                "clientInfo": {"name": "ocs-ejemplo", "version": "1"},
            },
        )
        self.server = init["serverInfo"]
        print(f"Servidor: {self.server['name']} {self.server['version']}")
        if self.server.get("version") != VERSION_PROBADA:
            print(
                f"AVISO: servidor {self.server.get('version')}; esta libreria "
                f"esta probada con {VERSION_PROBADA}. Sigo, pero si algo "
                f"falla empieza por aqui."
            )
        protocolo = init.get("protocolVersion")
        if protocolo and protocolo != PROTOCOLO:
            print(f"AVISO: el servidor habla MCP {protocolo} (enviamos {PROTOCOLO})")
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
