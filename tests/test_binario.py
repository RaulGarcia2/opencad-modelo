"""Tests puros (sin OCS) de la localizacion del binario de OpenCADStudio.

Comprueba que la libreria encuentra el ejecutable en cualquier
instalacion (``bin=``, ``OCS_BIN``, ``PATH`` y rutas habituales) y que
no depende del plugin ``opencad-python``: para dibujar no hay que
recompilar nada. Ejecutar con:

    PYTHONPATH=../src python3 test_binario.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from opencad_modelo.ocs import (
    BIN_POR_DEFECTO,
    OcsMcp,
    _a_argv,
    candidatas,
    encontrar_binario,
)

AQUI = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(AQUI, "..", "src", "opencad_modelo")


def test_candidatas():
    linux = candidatas("linux")
    assert BIN_POR_DEFECTO in linux, linux
    assert all(ruta.endswith("opencad-studio") for ruta in linux), linux
    windows = candidatas("win32", {"LOCALAPPDATA": r"C:\Users\raul\AppData\Local"})
    assert windows and all(ruta.endswith(".exe") for ruta in windows), windows
    assert not any("%" in ruta for ruta in windows), windows
    assert windows[0].startswith("C:\\Users\\raul\\AppData"), windows
    # Sin la variable definida la ruta se queda como esta (no revienta).
    assert "%LOCALAPPDATA%" in candidatas("win32", {})[0]
    # Las del sistema actual existen como lista aunque falten los ficheros.
    assert candidatas()


def test_argv():
    assert _a_argv("/usr/bin/opencad-studio") == ["/usr/bin/opencad-studio"]
    assert _a_argv(["a", "b"]) == ["a", "b"]
    # OCS_BIN puede llevar argumentos (flatpak, AppImage...).
    assert _a_argv("flatpak run org.opencad.Studio") == [
        "flatpak", "run", "org.opencad.Studio"]
    assert _a_argv('"/ruta/con espacios/opencad-studio"') == [
        "/ruta/con espacios/opencad-studio"]
    assert _a_argv("   ") == []


def test_explicito():
    assert encontrar_binario(sys.executable) == [sys.executable]
    assert encontrar_binario(f"{sys.executable} --mcp") == [
        sys.executable, "--mcp"]
    try:
        encontrar_binario("/no/existe/opencad-studio")
        raise SystemExit("falta error de bin= inexistente")
    except FileNotFoundError as e:
        texto = str(e)
        assert "bin=" in texto, texto
        assert "/no/existe/opencad-studio" in texto, texto


def test_ocs_bin():
    previo = os.environ.get("OCS_BIN")
    try:
        os.environ["OCS_BIN"] = sys.executable
        assert encontrar_binario() == [sys.executable]
        os.environ["OCS_BIN"] = f"{sys.executable} --mcp"
        assert encontrar_binario() == [sys.executable, "--mcp"]
        # bin= manda sobre OCS_BIN.
        assert encontrar_binario(sys.executable) == [sys.executable]
        # Un OCS_BIN invalido avisa: no cae a otra instalacion por sorpresa.
        os.environ["OCS_BIN"] = "/no/existe/opencad-studio"
        try:
            encontrar_binario()
            raise SystemExit("falta error de OCS_BIN inexistente")
        except FileNotFoundError as e:
            assert "OCS_BIN" in str(e), str(e)
    finally:
        if previo is None:
            os.environ.pop("OCS_BIN", None)
        else:
            os.environ["OCS_BIN"] = previo


def test_path():
    if os.name == "nt":
        print("  (test_path: sin prueba de PATH en Windows)")
        return
    previo_path = os.environ.get("PATH", "")
    previo_bin = os.environ.pop("OCS_BIN", None)
    tmp = tempfile.mkdtemp(prefix="ocs-bin-")
    try:
        exe = os.path.join(tmp, "opencad-studio")
        with open(exe, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        os.environ["PATH"] = tmp + os.pathsep + previo_path
        assert encontrar_binario() == [exe]
    finally:
        os.environ["PATH"] = previo_path
        if previo_bin is not None:
            os.environ["OCS_BIN"] = previo_bin
        shutil.rmtree(tmp, ignore_errors=True)


def test_sin_plugin():
    """Dibujar no depende del plugin opencad-python: cero recompilaciones."""
    prohibidos = ("PY_RUN", "PY_EVAL", "opencad-python", "plugin.toml")
    for fichero in sorted(os.listdir(SRC)):
        if not fichero.endswith(".py"):
            continue
        with open(os.path.join(SRC, fichero), encoding="utf-8") as f:
            texto = f.read()
        for palabra in prohibidos:
            assert palabra not in texto, f"{fichero} menciona {palabra}"


def test_motivo_salida():
    """Si el proceso muere, el error dice por que (stderr + codigo)."""
    m = OcsMcp.__new__(OcsMcp)
    m.proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.stderr.write('no hay editor en ejecucion\\n'); sys.exit(3)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    m._stderr = deque(maxlen=10)
    m.proc.wait()
    m._stderr.extend(linea.rstrip() for linea in m.proc.stderr)
    texto = m._motivo_salida()
    assert "codigo 3" in texto, texto
    assert "no hay editor" in texto, texto
    # Proceso vivo y sin stderr: no inventa motivo.
    m2 = OcsMcp.__new__(OcsMcp)
    m2.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    m2._stderr = deque(maxlen=10)
    try:
        assert m2._motivo_salida() == ""
    finally:
        m2.proc.kill()
        m2.proc.wait()


if __name__ == "__main__":
    test_candidatas()
    test_argv()
    test_explicito()
    test_ocs_bin()
    test_path()
    test_sin_plugin()
    test_motivo_salida()
    print("tests binario OK")
