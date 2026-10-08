"""opencad-modelo: fachada simple para dibujar en OpenCADStudio via MCP.

Expone la clase :class:`Modelo` (lineas, polilineas, textos, capas y
edicion) y el cliente MCP minimo (:class:`OcsMcp`, :func:`elegir_sesion`).

Uso:
    from opencad_modelo import Modelo

    with Modelo() as m:
        m.creaCapa("capa1", 4, "DASHDOT")
        h = m.linea([0, 0], [100, 0], "capa1")
        m.texto("Hola", [10, 45, 0], capa="capa1")
"""

from .modelo import Modelo
from .ocs import OcsMcp, OcsSessionError, elegir_sesion

__version__ = "0.4.3"
__all__ = ["Modelo", "OcsMcp", "OcsSessionError", "elegir_sesion", "__version__"]
