"""Tests puros (sin OCS) de puntos_curva: ejecutar con
PYTHONPATH=../src python3 test_puntos_curva.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from opencad_modelo.modelo import (  # noqa: E402
    _arco_estaciones,
    _catmull_rom_muestrear,
    _circulo_estaciones,
    _elipse_estaciones,
    _fuente_spline,
    _nurbs_muestrear,
    _remuestrear,
)


def cerca(a, b, tol=1e-9):
    assert abs(a - b) <= tol, (a, b)


def test_remuestrear():
    pts = _remuestrear([(0.0, 0.0), (10.0, 0.0)], 3.0)
    assert [p[0] for p in pts] == [0.0, 2.5, 5.0, 7.5, 10.0]
    assert [p[3] for p in pts] == [0.0, 2.5, 5.0, 7.5, 10.0]
    for a, b in zip(pts, pts[1:]):
        assert math.hypot(b[0] - a[0], b[1] - a[1]) <= 3.0 + 1e-9
    anillo = _remuestrear([(0.0, 0.0), (4.0, 0.0), (4.0, 3.0)], 2.0,
                          cerrada=True)
    assert anillo[0][:2] == anillo[-1][:2]
    assert abs(anillo[-1][3] - 12.0) < 1e-9
    try:
        _remuestrear([(0.0, 0.0)], 1.0)
        raise SystemExit("falta error <2 puntos")
    except ValueError:
        pass
    try:
        _remuestrear([(0.0, 0.0), (1.0, 0.0)], 0.0)
        raise SystemExit("falta error paso 0")
    except ValueError:
        pass


def test_nurbs_cuarto_circulo():
    sq = math.sqrt(2) / 2
    pts = _nurbs_muestrear([(1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)],
                           2, [0, 0, 0, 1, 1, 1], [1.0, sq, 1.0])
    assert len(pts) >= 32
    for x, y, z in pts:
        cerca(math.hypot(x, y), 1.0, 1e-9)
    cerca(pts[0][0], 1.0)
    cerca(pts[-1][1], 1.0)
    try:
        _nurbs_muestrear([(0.0, 0.0, 0.0)], 2, [0, 0, 0], None)
        raise SystemExit("falta error degree")
    except ValueError:
        pass
    try:
        _nurbs_muestrear([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)], 1,
                         [0, 1, 0.5, 1], None)
        raise SystemExit("falta error knots")
    except ValueError:
        pass


def test_catmull_rom():
    fit = [(0.0, 0.0, 0.0), (10.0, 10.0, 1.0), (20.0, 0.0, 0.0)]
    densa = _catmull_rom_muestrear(fit, por_tramo=8)
    assert densa[0] == fit[0] and densa[-1] == fit[-1]
    for f in fit[1:-1]:
        assert min(math.dist(f, p) for p in densa) < 1e-9


def test_fuente_spline():
    modo, _ = _fuente_spline({"control_points": [[0, 0, 0], [1, 0, 0]],
                              "degree": 1, "knots": [0, 0, 1, 1],
                              "flags": {}})
    assert modo == "nurbs"
    modo, _ = _fuente_spline({"fit_points": [{"x": 0, "y": 0, "z": 0},
                                              {"x": 1, "y": 0, "z": 0}],
                              "flags": {}})
    assert modo == "catmull"
    try:
        _fuente_spline({"control_points": [[0, 0, 0], [1, 0, 0]],
                        "degree": 1, "knots": [0, 0, 1, 1],
                        "flags": {"periodic": True}})
        raise SystemExit("falta error periodica")
    except ValueError:
        pass
    try:
        _fuente_spline({"flags": {}})
        raise SystemExit("falta error vacia")
    except ValueError:
        pass


def test_circulo_arco():
    est = _circulo_estaciones([5.0, 5.0, 2.0], 10.0, 1.0)
    assert est[0][:3] == est[-1][:3]
    for x, y, z, _pk in est:
        cerca(math.hypot(x - 5.0, y - 5.0), 10.0, 1e-9)
        cerca(z, 2.0)
    cerca(est[-1][3], 2 * math.pi * 10.0, 1e-6)
    arc = _arco_estaciones([0.0, 0.0, 0.0], 10.0, 0.0, math.pi / 2, 0.5)
    cerca(arc[0][0], 10.0)
    cerca(arc[-1][1], 10.0)
    cerca(arc[-1][3], math.pi * 10.0 / 2, 1e-6)
    try:
        _circulo_estaciones([0, 0, 0], -1.0, 1.0)
        raise SystemExit("falta error radio")
    except ValueError:
        pass


def test_elipse():
    est = _elipse_estaciones([0.0, 0.0, 0.0], [10.0, 0.0, 0.0], 0.5,
                             0.0, 0.0, 0.5)
    assert est[0][:3] == est[-1][:3]
    a, b = 10.0, 5.0
    ramanujan = math.pi * (3 * (a + b) - math.sqrt((3 * a + b) * (a + 3 * b)))
    cerca(est[-1][3], ramanujan, 0.01 * ramanujan)
    try:
        _elipse_estaciones([0, 0, 0], [10, 0, 0], 1.5, 0.0, 0.0, 0.5)
        raise SystemExit("falta error ratio")
    except ValueError:
        pass


if __name__ == "__main__":
    test_remuestrear()
    test_nurbs_cuarto_circulo()
    test_catmull_rom()
    test_fuente_spline()
    test_circulo_arco()
    test_elipse()
    print("tests paquete OK")
