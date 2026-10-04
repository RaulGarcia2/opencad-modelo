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
    def tool(self, name, arguments, permitir_stale=False):
        """Llama a una tool y devuelve su structuredContent (o lanza error)."""
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        sc = res.get("structuredContent", {})
        if res.get("isError") or sc.get("ok") is False:
            if permitir_stale and sc.get("code") == "stale_state":
                return sc
            raise RuntimeError(f"{name} fallo: {json.dumps(sc)}")
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
        print(f"Servidor: {init['serverInfo']['name']} {init['serverInfo']['version']}")
        self._notify("notifications/initialized", {})
        tools = self._rpc("tools/list", {})
        nombres = sorted(t["name"] for t in tools["tools"])
        print(f"Tools: {', '.join(nombres)}")
        assert set(nombres) == {
            "ocs_sessions",
            "ocs_read",
            "ocs_execute",
            "ocs_capture",
        }, nombres

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
        if sc.get("code") == "stale_state" and reintentos_stale > 0:
            # Otro cliente toco el documento entre medias: refrescar y
            # reintentar UNA vez con un request_id nuevo.
            self.tool("ocs_read", {"ocs_session_id": session_id, "op": "state"})
            request = dict(request, request_id=self.nuevo_id("ej"))
            return self.execute(session_id, request, detail, wait, reintentos_stale - 1)
        estado = sc.get("status")
        if estado in ("accepted", "running"):
            sc = self._esperar_operacion(session_id, request["request_id"])
        if sc.get("ok") is False or sc.get("status") in ("failed", "cancelled"):
            raise RuntimeError(f"Operacion fallo: {json.dumps(sc)}")
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
