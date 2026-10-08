# opencad-modelo

Fachada simple en Python para dibujar en **OpenCADStudio** a través de su
servidor **MCP** (sin dependencias externas; solo la librería estándar).

## Instalación

```sh
pip install -e .
```

O directamente desde GitHub (sin clonar):

```sh
pip install "git+https://github.com/RaulGarcia2/opencad-modelo.git"
# fijando versión:
pip install "git+https://github.com/RaulGarcia2/opencad-modelo.git@v0.3.3"
```

## Uso

```python
from opencad_modelo import Modelo

with Modelo() as m:                       # conecta con el dibujo en pantalla
    m.creaCapa("capaRuta", 4, "DASHDOT", grosor=0.5)
    h = m.linea([0, 0], [100, 0], "capaRuta")
    m.polilinea([[0, 0], [50, 50], [100, 0]], "capaRuta",
                color=1, grosor=0.5, estilo="DASHED")  # None = hereda la capa
    m.texto("Hola MCP", [10, 45, 0], altura=5, capa="capaRuta")
    m.textos([("a", [0, 0]), ("b", [0, 10])], "capaRuta", color=2)
    h2 = m.cambiar_propiedades(h, color=1, grosor=0.5, estilo="DASHED")
    hs = m.crear_lote([("pline", [[0, 0], [10, 0], [10, 10]], True),
                       ("circulo", [5, 5], 1)], "capaRuta")  # lote rapido
    m.agrupar(hs, "grupo1"); m.borrar(hs)
    m.paralela(h, 3)                      # paralela a la derecha (+dcha / -izda)
    m.circulo([50, 25], 20, "capaRuta")   # circulo por centro y radio
    m.arco_3p([0, 0], [50, 20], [100, 0]) # arco por 3 puntos
    n, h = m.bloque_desde_fichero("plano.dwg", [0, 0, 0])  # bloque externo
    sel = m.seleccionar_coordenadas()     # pincha en pantalla y devuelve puntos
```

## Requisitos

- OpenCADStudio instalado con su MCP (`opencad-studio --mcp`).
- Un dibujo abierto en el editor.

La clase `Modelo` usa por defecto el ejecutable `/usr/local/bin/opencad-studio`;
se puede cambiar con `Modelo(bin="/ruta/al/binario")`.

## Licencia

GPL-3.0 (ver `LICENSE`).
