# opencad-modelo

Fachada simple en Python para dibujar en **OpenCADStudio** a través de su
servidor **MCP** (sin dependencias externas; solo la librería estándar).

## Instalación

```sh
pip install -e .
```

## Uso

```python
from opencad_modelo import Modelo

with Modelo() as m:                       # conecta con el dibujo en pantalla
    m.creaCapa("capaRuta", 4, "DASHDOT")
    h = m.linea([0, 0], [100, 0], "capaRuta")
    m.polilinea([[0, 0], [50, 50], [100, 0]], "capaRuta")
    m.texto("Hola MCP", [10, 45, 0], altura=5, capa="capaRuta")
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
