"""Tests puros (sin OCS) del sobre de respuesta MCP de 2026.44.

Cubre el transporte: como se lee el payload cuando structuredContent
viene vacio, la deteccion de stale_state dentro de results, el
handshake con un catalogo mas amplio que el esperado y los mensajes de
error. Ejecutar con:

    PYTHONPATH=../src python3 test_ocs_sobre.py
"""
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from opencad_modelo.modelo import Modelo
from opencad_modelo.ocs import TOOLS_REQUERIDAS, OcsMcp, detalle_fallo

TOOLS_2026_44 = [
    "ocs_audit", "ocs_cancel", "ocs_capture", "ocs_commands", "ocs_execute",
    "ocs_find_text", "ocs_inspect", "ocs_measure", "ocs_new", "ocs_open",
    "ocs_query", "ocs_read", "ocs_redo", "ocs_render", "ocs_run", "ocs_save",
    "ocs_save_verified", "ocs_select", "ocs_sessions", "ocs_undo",
]


def sobre_ok(payload):
    """Respuesta correcta: structuredContent poblado (2026.44)."""
    return {
        "isError": False,
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }


def sobre_fallo(payload):
    """Respuesta fallida 2026.44: structuredContent VACIO y el JSON
    solo en content[0].text."""
    return {
        "isError": True,
        "structuredContent": {},
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }


def cliente(respuestas):
    """OcsMcp sin subproceso: _rpc/_notify responden de una cola."""
    m = OcsMcp.__new__(OcsMcp)
    m._id = 0
    m._req = 0
    cola = list(respuestas)

    def _rpc(method, params):
        if not cola:
            raise AssertionError(f"se pidio de mas: {method} {params}")
        return cola.pop(0)

    m._rpc = _rpc
    m._notify = lambda method, params: None
    return m


def test_desempaquetar():
    # structuredContent vacio -> se lee content[0].text (2026.44).
    payload = {"ok": False, "code": "invalid_command",
               "error": "Comando desconocido"}
    assert OcsMcp._desempaquetar("ocs_execute", sobre_fallo(payload)) == payload
    # Si structuredContent viene poblado, manda el suyo.
    bueno = {"ok": True, "status": "completed"}
    res = {"isError": False, "structuredContent": bueno,
           "content": [{"type": "text", "text": '{"ok": false}'}]}
    assert OcsMcp._desempaquetar("ocs_execute", res) == bueno
    # Texto ilegible: no se puede construir un payload, pero se avisa.
    roto = {"isError": True, "structuredContent": {},
            "content": [{"type": "text", "text": "no es json"}]}
    malo = OcsMcp._desempaquetar("ocs_read", roto)
    assert malo["ok"] is False and "no es json" in malo["error"]


def test_tool_error_claro():
    payload = {"ok": False, "code": "invalid_command",
               "error": "Comando desconocido: FOO"}
    m = cliente([sobre_fallo(payload)])
    try:
        m.tool("ocs_execute", {"op": "run"})
        raise SystemExit("falta error de tool")
    except RuntimeError as e:
        texto = str(e)
        assert "invalid_command" in texto, texto
        assert "Comando desconocido" in texto, texto
        assert "{}" not in texto, texto


def test_tool_stale():
    # stale en el nivel superior: se devuelve sin lanzar.
    top = {"ok": False, "code": "stale_state", "error": "Refresh state"}
    m = cliente([sobre_fallo(top)])
    assert m.tool("ocs_execute", {}, permitir_stale=True)["code"] == "stale_state"
    # ... pero sin permiso explota con mensaje legible.
    m = cliente([sobre_fallo(top)])
    try:
        m.tool("ocs_execute", {})
        raise SystemExit("falta error de stale")
    except RuntimeError as e:
        assert "stale_state" in str(e), e


def test_es_stale_batch():
    # stale dentro de results[].code (no en el nivel superior).
    batch = {"ok": False, "status": "failed",
             "results": [{"step": 0, "op": "run", "ok": False,
                          "code": "stale_state",
                          "error": "Refresh state before editing"}]}
    assert OcsMcp._es_stale(batch) is True
    # Solo cuenta si TODOS los fallidos son stale: si alguno avanzo,
    # reintentar duplicaria el trabajo ya hecho.
    mixto = {"ok": False, "status": "failed",
             "results": [{"step": 0, "ok": False, "code": "stale_state"},
                         {"step": 1, "ok": False,
                          "code": "invalid_command", "error": "FOO"}]}
    assert OcsMcp._es_stale(mixto) is False
    limpio = {"ok": True, "status": "completed", "completed_steps": 2,
              "results": [{"step": 0, "ok": True, "status": "completed"},
                          {"step": 1, "ok": True, "status": "completed"}]}
    assert OcsMcp._es_stale(limpio) is False


def test_handshake():
    # El catalogo de referencia contiene todas las tools requeridas.
    assert TOOLS_REQUERIDAS <= set(TOOLS_2026_44)
    # Un catalogo mas amplio que el de siempre (2026.44: 20 tools) vale,
    # mientras esten las que esta libreria usa.
    m = cliente([
        {"serverInfo": {"name": "OpenCADStudio", "version": "2026.44"}},
        {"tools": [{"name": n} for n in TOOLS_2026_44]},
    ])
    m.handshake()
    assert m.server["version"] == "2026.44"
    # Si falta una tool requerida, no se conecta y se dice cual falta.
    incompleto = [n for n in TOOLS_2026_44 if n != "ocs_execute"]
    m = cliente([
        {"serverInfo": {"name": "OpenCADStudio", "version": "2026.44"}},
        {"tools": [{"name": n} for n in incompleto]},
    ])
    try:
        m.handshake()
        raise SystemExit("falta error de tools requeridas")
    except RuntimeError as e:
        assert "ocs_execute" in str(e), e


def test_handshake_version_distinta():
    # Otra version no corta: avisa y sigue (lo que manda es TOOLS_REQUERIDAS).
    m = cliente([
        {"serverInfo": {"name": "OpenCADStudio", "version": "2026.40"},
         "protocolVersion": "2025-11-25"},
        {"tools": [{"name": n} for n in TOOLS_2026_44]},
    ])
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        m.handshake()
    assert m.server["version"] == "2026.40"
    texto = salida.getvalue()
    assert "AVISO" in texto, texto
    assert "2026.40" in texto, texto


def test_detalle_fallo():
    sc = {"ok": False, "status": "failed",
          "results": [{"step": 0, "op": "run", "ok": False,
                       "status": "waiting_input", "code": None, "error": None}],
          "state": {"modal": "Rename", "command": None}}
    texto = detalle_fallo(sc)
    assert "paso 0" in texto, texto
    assert "modal abierto: Rename" in texto, texto


def test_exigir_batch():
    # waiting_input por un modal abierto: error legible, no el diccionario.
    resp = {"ok": True, "status": "waiting_input", "completed_steps": 1,
            "state": {"modal": "Rename"}}
    try:
        Modelo._exigir_batch(resp, 2, "crear la capa 'x'")
        raise SystemExit("falta error de batch")
    except RuntimeError as e:
        assert "crear la capa 'x'" in str(e), e
        assert "modal abierto: Rename" in str(e), e
    # Batch completo: pasa.
    ok = {"ok": True, "status": "completed", "completed_steps": 2,
          "results": [{"step": 0, "status": "completed"},
                      {"step": 1, "status": "completed"}]}
    assert Modelo._exigir_batch(ok, 2, "x") is ok


def test_exigir_ok():
    ok = {"status": "completed"}
    assert Modelo._exigir_ok(ok, "x") is ok
    try:
        Modelo._exigir_ok({"status": "waiting_input"}, "activar la pestana")
        raise SystemExit("falta error de op simple")
    except RuntimeError as e:
        assert "activar la pestana" in str(e), e


def test_execute_reintenta_stale():
    # stale en results -> refresca estado -> reintenta con request_id nuevo.
    batch_stale = {"ok": False, "status": "failed", "results": [
        {"step": 0, "op": "run", "ok": False, "code": "stale_state",
         "error": "Refresh state before editing"}]}
    hecho = {"ok": True, "status": "completed", "completed_steps": 1,
             "results": [{"step": 0, "ok": True, "status": "completed"}]}
    m = cliente([
        sobre_fallo(batch_stale),   # primer intento: stale
        sobre_ok({"document_id": 5}),  # refresh de estado
        sobre_ok(hecho),            # reintento
    ])
    resp = m.execute("sid", {"op": "batch", "request_id": "r-1", "steps": []})
    assert resp["status"] == "completed"
    # Sin reintentos agotados, el stale se convierte en error claro.
    m = cliente([sobre_fallo(batch_stale), sobre_ok({"document_id": 5}),
                 sobre_fallo(batch_stale)])
    try:
        m.execute("sid", {"op": "batch", "request_id": "r-2", "steps": []},
                  reintentos_stale=1)
        raise SystemExit("falta error de stale agotado")
    except RuntimeError as e:
        assert "stale_state" in str(e), e


if __name__ == "__main__":
    test_desempaquetar()
    test_tool_error_claro()
    test_tool_stale()
    test_es_stale_batch()
    test_handshake()
    test_handshake_version_distinta()
    test_detalle_fallo()
    test_exigir_batch()
    test_exigir_ok()
    test_execute_reintenta_stale()
    print("tests ocs_sobre OK")
