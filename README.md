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
pip install "git+https://github.com/RaulGarcia2/opencad-modelo.git@v0.5.0"
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
- Un dibujo abierto en el editor (`--mcp` se conecta al editor que ya está
  en marcha; no abre uno nuevo).

## Sin plugin y sin recompilar nada

La librería habla con el servidor MCP que trae OpenCADStudio de serie: **no
usa el plugin `opencad-python`** (el que ejecuta `PY_RUN` dentro del editor).
Da igual que el editor arranque con `plugins: []` o que ese plugin se haya
quedado desfasado tras una actualización: para dibujar desde Python aquí no
hay que compilar ni instalar nada más.

Solo hace falta la librería estándar de Python (no tiene dependencias) y un
OpenCADStudio **2026.44** con sus tools MCP. La librería únicamente exige
`ocs_sessions`, `ocs_read` y `ocs_execute`: si el servidor amplía su catálogo
(2026.44 expone 20 tools) se ignora el resto. Si la versión del servidor no es
2026.44 se avisa y se continúa.

## Dónde está el ejecutable

`Modelo()` lo busca solo, en este orden:

1. `Modelo(bin="/ruta/al/binario")`.
2. La variable de entorno `OCS_BIN`.
3. El `PATH` (`shutil.which`).
4. Las rutas habituales: `/usr/local/bin`, `/usr/bin`, `/opt/opencad-studio`,
   `~/.local/bin`, `~/bin`; en Windows `%LOCALAPPDATA%\Programs\OpenCADStudio`
   y `Program Files`.

Si no encuentra nada, el error lista todas las rutas probadas. `bin=` y
`OCS_BIN` valen también para un comando con argumentos, p. ej.
`OCS_BIN="flatpak run org.opencad.Studio"`.

## Licencia

GPL-3.0 (ver `LICENSE`).
