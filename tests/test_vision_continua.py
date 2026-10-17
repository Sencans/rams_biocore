"""Tests de la visión continua local (grupo vision, docs/COMPANION.md §C2.9, §C2.13, §C3 "Grupo vision").

Sin hardware: la webcam NUNCA se abre (fábricas de captura falsas), la pantalla real nunca se
captura (fuentes falsas) y los relojes son falsos donde importa el tiempo. Las pruebas
asíncronas usan ``asyncio.run`` (fixture ``correr`` de tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

import lobes.vision_continua as vc_mod
from core.bus import BusEventos
from core.config import Settings
from core.contracts import ESTADOS_VISION, DiagnosticoModulo, EstadoModulo, ResultadoVistazo
from lobes.vision_continua import (
    EscaleraDegradacion,
    EstadoVision,
    Integrador,
    MetricasPantalla,
    MuestraCamara,
    MuestraPantalla,
    VisionContinua,
    analizar,
    mascara_caja,
    punto_mirada,
)

np = pytest.importorskip("numpy")


# ---------------------------------------------------------------------------
# Utilidades de prueba
# ---------------------------------------------------------------------------


class Reloj:
    """Reloj monotónico controlable."""

    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _eventos(bus: BusEventos, *patrones: str) -> list[tuple[str, dict[str, Any]]]:
    registro: list[tuple[str, dict[str, Any]]] = []
    for patron in patrones:
        bus.suscribir(patron, lambda ev: registro.append((ev.nombre, dict(ev.datos))))
    return registro


def _solo_escalares(valor: Any) -> bool:
    """``True`` si el payload solo tiene escalares JSON (ni bytes ni arrays de numpy)."""
    if valor is None or isinstance(valor, (bool, int, float, str)):
        return not isinstance(valor, (bytes, bytearray)) and type(valor).__module__ == "builtins"
    if isinstance(valor, dict):
        return all(isinstance(k, str) and _solo_escalares(v) for k, v in valor.items())
    if isinstance(valor, (list, tuple)):
        return all(_solo_escalares(v) for v in valor)
    return False


def _verificar_payloads(eventos: list[tuple[str, dict[str, Any]]]) -> None:
    for nombre, datos in eventos:
        assert _solo_escalares(datos), (nombre, datos)
        json.dumps(datos)


def _m(ts: float, *, rostro: bool = True, n: int = 1, u: float = 0.5, v: float = 0.5, dist: float | None = 0.7,
       yaw: float | None = 0.0, pitch: float | None = 0.5, frontal: bool | None = True, mov: float = 0.01,
       brillo: float = 0.5, score: float | None = 0.9) -> MuestraCamara:
    if not rostro:
        return MuestraCamara(ts=ts, rostro=False, n=0, u=None, v=None, dist_m=None, yaw_r=None, pitch_r=None,
                             frontal=None, mov=mov, brillo=brillo, score=None, ms_proc=2.0)
    return MuestraCamara(ts=ts, rostro=True, n=n, u=u, v=v, dist_m=dist, yaw_r=yaw, pitch_r=pitch,
                         frontal=frontal, mov=mov, brillo=brillo, score=score, ms_proc=2.0)


def _simular(integ: Integrador, desde: float, hasta: float, rostro_en: Any, *,
             eventos: list[tuple[float, str, dict[str, Any]]]) -> None:
    """Muestras a 3 fps y ticks a 2 Hz (en sextos de segundo, sin deriva) entre ``desde`` y ``hasta``."""
    inicio = round(desde * 6)
    fin = round(hasta * 6)
    for k in range(inicio, fin + 1):
        t = k / 6.0
        if k % 2 == 0:
            m = _m(t) if rostro_en(t) else _m(t, rostro=False)
            for nombre, datos in integ.muestra(m):
                eventos.append((t, nombre, datos))
        if k % 3 == 0:
            for nombre, datos in integ.tick(t):
                eventos.append((t, nombre, datos))


def _tipos(eventos: list[tuple[float, str, dict[str, Any]]], tipo: str) -> list[float]:
    return [t for t, nombre, d in eventos if nombre == "vision.estimulo" and d["tipo"] == tipo]


@pytest.fixture
def settings_vision(settings_prueba: Settings) -> Settings:
    return settings_prueba.con(camara_habilitada=True, vision_continua=True)


def _vision(settings: Settings, bus: BusEventos | None = None, **kwargs: Any) -> VisionContinua:
    """``VisionContinua`` aislada: sin leer el entorno real, sin plataforma y con una fábrica de
    captura que nunca abre nada (salvo que el test pase la suya)."""
    kwargs.setdefault("fabrica_captura", lambda indice: None)
    vision = VisionContinua(settings, bus, **kwargs)
    vision._leer_entorno = lambda: {}  # type: ignore[method-assign]
    vision._plataforma = "otro"
    return vision


# ---------------------------------------------------------------------------
# Privacidad del código fuente y API
# ---------------------------------------------------------------------------


def test_fuente_sin_persistencia_ni_red() -> None:
    crudo = Path(inspect.getsourcefile(vc_mod) or "").read_bytes()
    fuente = crudo.decode("utf-8")
    for prohibido in ("imwrite", "imencode", "VideoWriter", "ImageGrab", "pytesseract", "base64", "httpx",
                      "shell=True"):
        assert prohibido not in fuente, prohibido
    assert re.search(r"open\([^)]*['\"][rwax+]*b", fuente) is None   # nada de open(..., "wb")
    assert b"\r" not in crudo                                          # UTF-8 + LF


def test_api_exacta() -> None:
    firma = inspect.signature(VisionContinua.__init__)
    assert list(firma.parameters)[1:3] == ["settings", "bus"]
    for nombre in ("presencia", "reloj", "fabrica_captura", "fabrica_pantalla"):
        assert firma.parameters[nombre].kind is inspect.Parameter.KEYWORD_ONLY
    assert firma.parameters["presencia"].default is None
    assert inspect.iscoroutinefunction(VisionContinua.iniciar)
    assert inspect.iscoroutinefunction(VisionContinua.cerrar)
    for nombre in ("set_camara", "set_pantalla", "pausar", "reanudar", "estado", "calibrar_atencion",
                   "como_vistazo", "set_caja_avatar"):
        assert not inspect.iscoroutinefunction(getattr(VisionContinua, nombre)), nombre
    pausar = inspect.signature(VisionContinua.pausar)
    assert pausar.parameters["segundos"].default is None and pausar.parameters["motivo"].default == "usuario"
    estado = EstadoVision("continua", "off", 3.0, 0.0, 0, "yunet", True, 0.5)
    assert set(estado.a_dict()) == {"camara", "pantalla", "fps_camara", "fps_pantalla", "nivel", "detector",
                                    "presente", "atencion"}
    assert {f for f in MuestraCamara.__dataclass_fields__} == {
        "ts", "rostro", "n", "u", "v", "dist_m", "yaw_r", "pitch_r", "frontal", "mov", "brillo", "score", "ms_proc"}
    assert {f for f in MuestraPantalla.__dataclass_fields__} == {
        "ts", "cambio", "brillo", "tono", "corte", "video", "pantalla_completa", "reproduciendo", "clase",
        "titulo", "privada", "ms_proc"}


# ---------------------------------------------------------------------------
# analizar (detector falso) y utilidades de imagen
# ---------------------------------------------------------------------------


class DetectorFalso:
    """Imita ``cv2.FaceDetectorYN.detect``: devuelve filas fijas y anota el tamaño de la imagen."""

    def __init__(self, filas: list[list[float]] | None) -> None:
        self.filas = filas
        self.formas: list[tuple[int, ...]] = []

    def detect(self, imagen: Any) -> tuple[int, Any]:
        self.formas.append(tuple(imagen.shape))
        if not self.filas:
            return 0, None
        return len(self.filas), np.asarray(self.filas, dtype=np.float32)


# Cara de 40×50 centrada horizontalmente, ojos a la misma altura y nariz en medio (de frente).
FILA_FRONTAL = [140, 90, 40, 50, 150, 105, 170, 105, 160, 115, 152, 125, 168, 125, 0.95]


def test_analizar_con_detector_falso_de_15_columnas() -> None:
    cuadro = np.full((480, 640, 3), 120, dtype=np.uint8)
    detector = DetectorFalso([FILA_FRONTAL])
    m, mini = analizar(cuadro, None, detector, hfov_deg=70.0, ts=5.0)
    assert detector.formas == [(240, 320, 3)]            # se analiza a 320×240
    assert m.ts == 5.0 and m.rostro is True and m.n == 1
    assert m.u == pytest.approx(0.5) and m.v == pytest.approx(115 / 240)
    focal = 160 / math.tan(math.radians(35))
    assert m.dist_m == pytest.approx(focal * 0.15 / 40, rel=1e-6)
    assert m.yaw_r == pytest.approx(0.0) and m.pitch_r == pytest.approx(0.5)
    assert m.frontal is True and m.score == pytest.approx(0.95, abs=1e-6)
    assert m.brillo == pytest.approx(120 / 255, abs=0.01) and m.mov == 0.0
    assert mini.shape == (60, 80)
    # Cabeza girada: la nariz se desplaza respecto al centro de los ojos.
    girada = list(FILA_FRONTAL)
    girada[8] = 170
    assert analizar(cuadro, None, DetectorFalso([girada]))[0].frontal is False
    # La línea base calibrada cambia lo que cuenta como "de frente".
    assert analizar(cuadro, None, DetectorFalso([girada]), base_yaw=0.45)[0].frontal is True
    for valor in m.__dict__.values():                    # solo escalares de Python
        assert valor is None or type(valor) in (bool, int, float), valor


def test_analizar_sin_cara_sin_detector_y_varias_caras() -> None:
    cuadro = np.full((480, 640, 3), 60, dtype=np.uint8)
    for detector in (DetectorFalso(None), DetectorFalso([]), None):
        m, _ = analizar(cuadro, None, detector)
        assert m.rostro is False and m.n == 0 and m.u is None and m.dist_m is None and m.frontal is None
    pequena = [10, 10, 20, 25, 15, 18, 25, 18, 20, 22, 16, 28, 24, 28, 0.85]
    m, _ = analizar(cuadro, None, DetectorFalso([pequena, FILA_FRONTAL]))
    assert m.n == 2 and m.u == pytest.approx(0.5)        # se elige la cara más grande
    # Haar: filas de 4 columnas (sin puntos ni puntuación) ⇒ de frente, sin yaw.
    m, _ = analizar(cuadro, None, DetectorFalso([[100, 80, 60, 60]]))
    assert m.rostro is True and m.yaw_r is None and m.score is None and m.frontal is True


def test_analizar_movimiento_y_brillo() -> None:
    oscuro = np.full((480, 640, 3), 30, dtype=np.uint8)
    claro = oscuro.copy()
    claro[:, :320] = 200                                   # media imagen cambia
    _, mini = analizar(oscuro, None, None)
    m, _ = analizar(claro, mini, None)
    assert 0.4 < m.mov < 0.6
    assert m.brillo > 0.3


def test_analizar_y_reducir_sin_opencv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vc_mod, "cv2", None)
    img = np.arange(16, dtype=np.uint8).reshape(4, 4)
    assert vc_mod._reducir(img, (2, 2)).tolist() == [[3, 5], [11, 13]]   # promedio por bloques
    cuadro = np.full((480, 640, 3), 100, dtype=np.uint8)
    m, mini = analizar(cuadro, None, DetectorFalso([FILA_FRONTAL]))
    assert m.rostro is True and mini.shape == (60, 80) and m.brillo == pytest.approx(100 / 255, abs=0.01)


def test_punto_mirada_signos_y_limites() -> None:
    assert punto_mirada(0.5, 0.5, 0.7) == (0.0, 1.3, 3.0)
    # Sin espejo: el usuario a SU derecha aparece a la izquierda del cuadro ⇒ +x del mundo.
    x, y, z = punto_mirada(0.3, 0.5, 0.7)
    assert x > 0 and y == pytest.approx(1.3) and z == 3.0
    assert punto_mirada(0.3, 0.5, 0.7, espejo=True)[0] == pytest.approx(-x)
    assert punto_mirada(0.5, 0.8, 0.7)[1] < 1.3            # más abajo en el cuadro ⇒ y menor
    assert punto_mirada(0.5, 0.2, 0.7)[1] > 1.3
    assert punto_mirada(0.0, 0.5, 10.0)[0] == 3.0          # límites del saneador
    assert punto_mirada(1.0, 0.5, 10.0)[0] == -3.0
    assert punto_mirada(0.5, 1.0, 10.0)[1] == 0.3
    assert punto_mirada(0.5, 0.0, 10.0)[1] == 2.2
    assert punto_mirada(0.4, 0.5, None)[0] > 0             # sin distancia: 0,6 m por defecto


# ---------------------------------------------------------------------------
# Métricas de pantalla y máscara de la caja del avatar
# ---------------------------------------------------------------------------


def test_metricas_pantalla_corte_y_tono() -> None:
    metricas = MetricasPantalla()
    oscura = np.full((36, 64, 3), 20, dtype=np.uint8)
    r = metricas.actualizar(oscura)
    assert r["cambio"] == 0.0 and r["corte"] is False and r["tono"] is None
    clara = np.full((36, 64, 3), 230, dtype=np.uint8)
    r = metricas.actualizar(clara)
    assert r["corte"] is True and r["cambio"] == pytest.approx(1.0) and r["brillo"] > 0.85
    for bgr, tono in (((0, 0, 255), 0), ((0, 255, 0), 4), ((255, 0, 0), 8)):
        assert MetricasPantalla().actualizar(np.full((36, 64, 3), bgr, dtype=np.uint8))["tono"] == tono
    # Una captura más grande se reduce sola a 64×36.
    assert MetricasPantalla().actualizar(np.full((108, 192, 3), 128, dtype=np.uint8))["brillo"] == pytest.approx(
        128 / 255, abs=0.01)


def test_metricas_pantalla_video_por_region() -> None:
    rng = np.random.default_rng(7)
    fondo = rng.integers(0, 255, size=(36, 64, 3), dtype=np.uint8)
    metricas = MetricasPantalla()
    resultados = []
    for _ in range(8):
        cuadro = fondo.copy()
        cuadro[10:26, 20:36] = rng.integers(0, 255, size=(16, 16, 3), dtype=np.uint8)   # 11 % de la pantalla
        resultados.append(metricas.actualizar(cuadro))
    assert resultados[-1]["video"] is True
    assert resultados[0]["video"] is False
    # Toda la pantalla cambiando sin parar no es "vídeo en una región".
    metricas = MetricasPantalla()
    for _ in range(8):
        r = metricas.actualizar(rng.integers(0, 255, size=(36, 64, 3), dtype=np.uint8))
    assert r["video"] is False


def test_metricas_pantalla_ignoran_la_caja_del_avatar() -> None:
    caja = (1280, 360, 320, 540)                           # px físicos en un monitor 1920×1080
    mascara = mascara_caja(caja, (0, 0, 1920, 1080))
    assert mascara is not None and mascara.shape == (36, 64)
    assert mascara[18, 48] and not mascara[18, 10]
    rng = np.random.default_rng(3)
    fondo = np.full((36, 64, 3), 90, dtype=np.uint8)
    metricas = MetricasPantalla()
    for _ in range(8):
        cuadro = fondo.copy()
        cuadro[mascara] = rng.integers(0, 255, size=(int(mascara.sum()), 3), dtype=np.uint8)  # el avatar se mueve
        r = metricas.actualizar(cuadro, mascara)
        assert r["cambio"] == 0.0 and r["corte"] is False and r["video"] is False
    assert r["brillo"] == pytest.approx(90 / 255, abs=0.01)
    # Sin máscara, el mismo movimiento sí se vería (y realimentaría la novedad).
    metricas = MetricasPantalla()
    for _ in range(8):
        cuadro = fondo.copy()
        cuadro[mascara] = rng.integers(0, 255, size=(int(mascara.sum()), 3), dtype=np.uint8)
        r = metricas.actualizar(cuadro)
    assert r["cambio"] > 0.05


def test_mascara_caja_geometria() -> None:
    mascara = mascara_caja((960, 540, 192, 108), (0, 0, 1920, 1080))
    filas, columnas = np.nonzero(mascara)
    assert (filas.min(), filas.max(), columnas.min(), columnas.max()) == (18, 21, 32, 38)
    # Monitor secundario con origen negativo.
    otra = mascara_caja((-1920, 0, 960, 1080), (-1920, 0, 1920, 1080))
    assert otra[:, :32].all() and not otra[:, 32:].any()
    assert not mascara_caja((5000, 5000, 100, 100), (0, 0, 1920, 1080)).any()   # fuera del monitor
    assert mascara_caja(None, (0, 0, 1920, 1080)) is None
    assert mascara_caja((0, 0, 100, 100), None) is None
    assert mascara_caja((0, 0, 0, 100), (0, 0, 1920, 1080)) is None


def test_parsear_ppm() -> None:
    datos = b"P6\n# grim\n2 1\n255\n" + bytes([255, 0, 0, 0, 0, 255])   # rojo, azul (RGB)
    bgr = vc_mod._parsear_ppm(datos)
    assert bgr.shape == (1, 2, 3)
    assert bgr[0, 0].tolist() == [0, 0, 255] and bgr[0, 1].tolist() == [255, 0, 0]
    assert vc_mod._parsear_ppm(b"P6\n2 1\n255\n" + b"\x00" * 3) is None      # truncado
    assert vc_mod._parsear_ppm(b"\x89PNG....") is None
    assert vc_mod._parsear_ppm(b"P6\n2 1\n65535\n" + b"\x00" * 12) is None   # 16 bits: no


# ---------------------------------------------------------------------------
# Integrador con reloj falso
# ---------------------------------------------------------------------------


def test_llegada_tras_un_segundo_y_salida_tras_veinte() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    eventos: list[tuple[float, str, dict[str, Any]]] = []
    _simular(integ, 0.0, 40.0, lambda t: t <= 10.0, eventos=eventos)
    assert _tipos(eventos, "llegada") == [pytest.approx(1.0)]
    assert _tipos(eventos, "salida") == [pytest.approx(30.0)]       # 20 s después de la última cara
    llegada = next(d for _, n, d in eventos if n == "vision.estimulo" and d["tipo"] == "llegada")
    assert llegada == {"fuente": "camara", "tipo": "llegada", "magnitud": 1.0, "firma": "camara:llegada:1"}
    assert integ.presente is False and integ.flanco == "ausente"


def test_tres_fps_durante_300_s_un_solo_flanco_de_llegada() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    eventos: list[tuple[float, str, dict[str, Any]]] = []
    # Caras a 3 fps con un fallo de detección cada 10 cuadros (histéresis).
    _simular(integ, 0.0, 300.0, lambda t: round(t * 3) % 10 != 7, eventos=eventos)
    assert len(_tipos(eventos, "llegada")) == 1
    assert _tipos(eventos, "salida") == []
    assert integ.presente is True
    rostros = [(t, d) for t, n, d in eventos if n == "vision.rostro"]
    tiempos = [t for t, _ in rostros]
    assert all(b - a >= 0.5 - 1e-9 for a, b in zip(tiempos, tiempos[1:]))     # ≤ 2 Hz
    assert all(set(d) == {"presente", "n", "dist_m", "frontal", "atencion"} for _, d in rostros)
    atenciones = [t for t, n, _ in eventos if n == "vision.atencion"]
    assert 100 <= len(atenciones) <= 300                                       # ~cada 2 s
    assert integ.atencion > 0.6


def test_refractario_de_llegada_y_flanco_silencioso() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    eventos: list[tuple[float, str, dict[str, Any]]] = []
    presente = lambda t: t <= 10.0 or 40.0 <= t <= 50.0 or t >= 200.0  # noqa: E731
    _simular(integ, 0.0, 220.0, presente, eventos=eventos)
    assert _tipos(eventos, "llegada") == [pytest.approx(1.0), pytest.approx(201.0)]
    assert _tipos(eventos, "salida") == [pytest.approx(30.0), pytest.approx(70.0)]
    # A los 41 s vuelve dentro del refractario: el estado cambia sin evento.
    integ2 = Integrador()
    integ2.abrir(0.0)
    _simular(integ2, 0.0, 42.0, lambda t: t <= 10.0 or t >= 40.0, eventos=[])
    assert integ2.presente is True


def test_estimulos_movimiento_luz_y_acercamiento() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    salidas: list[tuple[str, dict[str, Any]]] = []
    for k in range(30):                                       # 3 s a 10 fps, quieto
        salidas += integ.muestra(_m(k / 10, mov=0.01, brillo=0.5, dist=0.9))
    assert [d for n, d in salidas if n == "vision.estimulo"] == []
    salidas = integ.muestra(_m(3.0, mov=0.5, brillo=0.5, dist=0.9))
    assert [d["tipo"] for n, d in salidas if n == "vision.estimulo"] == ["movimiento"]
    refractario = [d for n, d in integ.muestra(_m(4.0, mov=0.6, brillo=0.5, dist=0.9)) if n == "vision.estimulo"]
    assert refractario == []                                  # refractario de 8 s
    luz = [d for n, d in integ.muestra(_m(4.5, brillo=0.2, dist=0.9)) if n == "vision.estimulo"]
    assert [(d["tipo"], d["firma"]) for d in luz] == [("luz", "camara:luz:baja")]
    acerca = [d for n, d in integ.muestra(_m(5.0, brillo=0.2, dist=0.6)) if n == "vision.estimulo"]
    assert [d["tipo"] for d in acerca] == ["acercamiento"]
    assert all(0.0 <= d["magnitud"] <= 1.0 for d in luz + acerca)


def test_sin_estimulos_de_luz_durante_el_asentamiento() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    eventos = []
    for k, brillo in enumerate((0.1, 0.3, 0.6, 0.8)):           # autoexposición al abrir
        eventos += integ.muestra(_m(k * 0.3, brillo=brillo))
    assert [d for n, d in eventos if n == "vision.estimulo"] == []


def test_mirada_como_mucho_5_hz_y_nunca_con_terceros() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    miradas = []
    for k in range(60):                                       # 2 s a 30 fps, moviéndose de lado a lado
        u = 0.3 if k % 2 else 0.7
        miradas += [(k / 30, d) for n, d in integ.muestra(_m(k / 30, u=u)) if n == "vision.mirada"]
    assert 1 <= len(miradas) <= 11
    tiempos = [t for t, _ in miradas]
    assert all(b - a >= 0.2 - 1e-9 for a, b in zip(tiempos, tiempos[1:]))
    for _, d in miradas:
        assert set(d) == {"x", "y", "z", "confianza"}
        assert -3 <= d["x"] <= 3 and 0.3 <= d["y"] <= 2.2 and -3 <= d["z"] <= 3 and 0 <= d["confianza"] <= 1
    # Con dos caras: ninguna mirada, y vision.rostro solo con el número de caras.
    integ = Integrador()
    integ.abrir(0.0)
    eventos = []
    for k in range(12):
        eventos += integ.muestra(_m(k / 3, n=2, u=0.2 + 0.05 * k))
        eventos += integ.tick(k / 3)
    assert [n for n, _ in eventos if n == "vision.mirada"] == []
    rostro = [d for n, d in eventos if n == "vision.rostro" and d["n"] == 2][-1]
    assert rostro["presente"] is True and rostro["dist_m"] is None and rostro["frontal"] is None


def test_calibrar_atencion_ajusta_la_linea_base() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    assert integ.calibrar(0.0) is False                       # sin muestra
    for k in range(3):
        integ.muestra(_m(k * 0.5, yaw=0.3, pitch=0.6))
        assert integ.calibrar(k * 0.5) is True
    assert integ.base_yaw == pytest.approx(0.3) and integ.base_pitch == pytest.approx(0.6)
    integ.muestra(_m(10.0, n=2))
    assert integ.calibrar(10.0) is False                      # con terceros no se calibra


# ---------------------------------------------------------------------------
# Degradación en escalera (telemetría falsa) y autopausa
# ---------------------------------------------------------------------------


def test_escalera_de_degradacion_con_telemetria_falsa() -> None:
    escalera = EscaleraDegradacion()
    assert escalera.evaluar(0.0, cpu_pct=95.0) == 0          # aún no sostenida
    assert escalera.evaluar(5.0, cpu_pct=95.0) == 0
    assert escalera.evaluar(10.0, cpu_pct=95.0) == 1          # 10 s por encima del 85 %
    t = 10.0
    while t < 125.0:
        t += 5.0
        assert escalera.evaluar(t, cpu_pct=95.0) == 1
    assert escalera.evaluar(130.0, cpu_pct=95.0) == 2         # > 2 min en el nivel 1
    assert escalera.evaluar(135.0, cpu_pct=10.0) == 2
    assert escalera.evaluar(190.0, cpu_pct=10.0) == 2
    assert escalera.evaluar(195.0, cpu_pct=10.0) == 1         # 60 s sin presión: baja un nivel
    assert escalera.evaluar(255.0, cpu_pct=10.0) == 0
    for kwargs in ({"temp_c": 90.0}, {"ms_camara": 20.0}, {"ms_pantalla": 50.0}, {"cargando": False}):
        assert EscaleraDegradacion().evaluar(0.0, **kwargs) == 1, kwargs
    assert EscaleraDegradacion().evaluar(0.0, cpu_pct=50.0, temp_c=60.0, cargando=True) == 0


def test_degradacion_cambia_fps_y_pantalla(settings_vision: Settings) -> None:
    reloj = Reloj(0.0)
    ajustes = settings_vision.con(vision_fps=5.0, pantalla_modo="pixeles", pantalla_fps=2.0)
    vision = _vision(ajustes, reloj=reloj, fabrica_pantalla=lambda: None)
    assert vision._fps_camara_objetivo() == 5.0 and vision._fps_pantalla_objetivo() == 2.0
    for t in range(0, 15, 5):
        reloj.t = float(t)
        vision._aplicar_entorno(reloj.t, {"cpu_pct": 99.0})
    assert vision._nivel == 1 and vision._fps_camara_objetivo() == 2.0 and vision._fps_pantalla_objetivo() == 0.5
    assert vision.estado().nivel == 1 and vision.estado().pantalla == "pixeles"
    for t in range(15, 140, 5):
        reloj.t = float(t)
        vision._aplicar_entorno(reloj.t, {"cpu_pct": 99.0})
    assert vision._nivel == 2 and vision._fps_camara_objetivo() == 1.0
    assert vision.estado().pantalla == "metadatos"            # nivel 2: sin píxeles
    vision._detector_tipo = "haar"
    vision._nivel = 0
    assert vision._fps_camara_objetivo() == 2.0               # Haar: como mucho 2 fps


def test_autopausa_por_entorno_y_presencia(settings_vision: Settings) -> None:
    reloj = Reloj(0.0)
    presencia_estado = types.SimpleNamespace(presente=True, confianza=0.9)
    presencia = types.SimpleNamespace(estado=lambda: presencia_estado)
    vision = _vision(settings_vision.con(pantalla_modo="metadatos"), reloj=reloj, presencia=presencia,
                     fabrica_pantalla=lambda: None)
    assert vision.estado().camara == "continua" and vision.estado().nivel == 0
    vision._aplicar_entorno(0.0, {"bateria_pct": 15.0, "cargando": False})
    assert vision.estado().camara == "pausada" and vision.estado().nivel == 3 and vision._autopausa == "bateria"
    vision._aplicar_entorno(5.0, {"bateria_pct": 15.0, "cargando": True})
    assert vision.estado().camara == "continua" and vision._autopausa == ""
    vision._aplicar_entorno(10.0, {"bloqueada": True})
    assert vision._autopausa == "bloqueada" and vision.estado().pantalla == "pausada"
    vision._aplicar_entorno(15.0, {"juego": True})
    assert vision._autopausa == "juego"
    vision._aplicar_entorno(20.0, {"app": "Zoom.exe"})       # app de llamada: pegajosa 5 min
    assert vision._autopausa == "llamada"
    reloj.t = 200.0
    vision._aplicar_entorno(200.0, {"app": "chrome"})
    assert vision._autopausa == "llamada"
    reloj.t = 321.0
    vision._aplicar_entorno(321.0, {"app": "chrome"})
    assert vision._autopausa == ""
    vision._aplicar_entorno(325.0, {"app": "ms-teams"})
    assert vision._autopausa == "llamada"
    vision._llamada_hasta = -math.inf
    presencia_estado.presente = False                         # ausente con confianza 0,9
    assert vision._evaluar_autopausa(330.0) == "ausente"
    presencia_estado.confianza = 0.5                          # poca confianza: no pausa
    assert vision._evaluar_autopausa(331.0) == ""
    assert vc_mod._coincide_app("obsidian", ("obs", "obs64")) is False


# ---------------------------------------------------------------------------
# Cámara: hilo, pausa y release desde el hilo (captura falsa)
# ---------------------------------------------------------------------------


class CapturaFalsa:
    """Webcam falsa: entrega cuadros sintéticos y registra desde qué hilo se libera."""

    def __init__(self) -> None:
        self.lecturas = 0
        self.ajustes: list[tuple[int, Any]] = []
        self.liberada_en: float | None = None
        self.hilo_release: str | None = None

    def isOpened(self) -> bool:  # noqa: N802 - API de OpenCV
        return True

    def set(self, propiedad: int, valor: Any) -> bool:
        self.ajustes.append((propiedad, valor))
        return True

    def getBackendName(self) -> str:  # noqa: N802
        return "FALSO"

    def grab(self) -> bool:
        time.sleep(0.002)
        return True

    def retrieve(self) -> tuple[bool, Any]:
        self.lecturas += 1
        return True, np.full((480, 640, 3), 100 + self.lecturas % 5, dtype=np.uint8)

    def read(self) -> tuple[bool, Any]:
        return self.retrieve()

    def release(self) -> None:
        self.hilo_release = threading.current_thread().name
        self.liberada_en = time.monotonic()


async def _esperar(condicion: Any, limite_s: float = 5.0) -> bool:
    fin = time.monotonic() + limite_s
    while time.monotonic() < fin:
        if condicion():
            return True
        await asyncio.sleep(0.02)
    return bool(condicion())


def test_pausar_libera_desde_el_hilo_y_reanudar(settings_vision: Settings, bus: BusEventos, correr) -> None:
    eventos = _eventos(bus, "vision.*", "pantalla.*")
    capturas: list[CapturaFalsa] = []

    def fabrica(indice: int) -> CapturaFalsa:
        capturas.append(CapturaFalsa())
        return capturas[-1]

    vision = _vision(settings_vision.con(vision_fps=5.0), bus, fabrica_captura=fabrica)
    vision._crear_detector = lambda tam: (None, None)  # type: ignore[method-assign]

    async def principal() -> dict[str, Any]:
        datos: dict[str, Any] = {}
        diagnosticos = await vision.iniciar()
        datos["claves"] = [d.clave for d in diagnosticos]
        assert await _esperar(lambda: vision._ultima_cam is not None)
        datos["vistazo"] = vision.como_vistazo("usuario")
        datos["estado_abierta"] = vision.estado()
        inicio = time.perf_counter()
        vision.pausar()
        datos["pausar_s"] = time.perf_counter() - inicio
        t_pausa = time.monotonic()
        assert await _esperar(lambda: capturas[0].liberada_en is not None, 1.0)
        datos["release_s"] = capturas[0].liberada_en - t_pausa
        assert await _esperar(lambda: vision._hilo_cam is None)
        datos["estado_pausada"] = vision.estado()
        datos["vistazo_pausada"] = vision.como_vistazo("usuario")
        vision.reanudar()
        assert await _esperar(lambda: len(capturas) == 2 and vision._cam_abierta)
        await vision.cerrar()
        datos["estado_final"] = vision.estado()
        return datos

    datos = correr(principal())
    assert datos["claves"] == ["vision_camara", "vision_pantalla"]
    assert datos["pausar_s"] < 0.005                        # pausar() solo señala el Event
    assert datos["release_s"] < 1.0
    assert all(c.hilo_release == "rams-vision-cam" for c in capturas)   # release() en el hilo, nunca en el loop
    assert all(c.liberada_en is not None for c in capturas)
    assert (5, 15) in capturas[0].ajustes                   # CAP_PROP_FPS pedido entre 10 y 15
    vistazo = datos["vistazo"]
    assert isinstance(vistazo, ResultadoVistazo) and vistazo.error is None
    assert vistazo.rostro is None and vistazo.motivo == "usuario"      # sin detector: no se sabe
    assert vistazo.brillo == pytest.approx(0.4, abs=0.02)
    assert datos["estado_abierta"].camara == "degradada" and datos["estado_abierta"].fps_camara == 5.0
    assert datos["estado_pausada"].camara == "pausada" and datos["vistazo_pausada"] is None
    assert datos["estado_final"].camara == "off"
    camara = [d for n, d in eventos if n == "vision.camara"]
    assert [d["activa"] for d in camara] == [True, False, True, False]
    assert all(d["fuente"] == "continua" and set(d) == {"activa", "habilitada", "duracion_ms", "fuente"}
               for d in camara)
    assert any(n == "vision.estado" and d["camara"] == "pausada" for n, d in eventos)
    assert all(d["camara"] in ESTADOS_VISION for n, d in eventos if n == "vision.estado")
    _verificar_payloads(eventos)


def test_camara_que_no_abre_queda_ocupada_y_reintenta(settings_vision: Settings, bus: BusEventos, correr) -> None:
    vision = _vision(settings_vision, bus, fabrica_captura=lambda indice: None)

    async def principal() -> tuple[str, float]:
        await vision.iniciar()
        assert await _esperar(lambda: vision._error_cam == "ocupada")
        estado = vision.estado().camara
        espera = vision._reintento_ts - vision._reloj()
        await vision.cerrar()
        return estado, espera

    estado, espera = correr(principal())
    assert estado == "ocupada"
    assert 25.0 < espera <= 30.0


def test_latido_de_vision_camara_cada_5_s(settings_vision: Settings, bus: BusEventos) -> None:
    camara = _eventos(bus, "vision.camara")
    reloj = Reloj(100.0)
    vision = _vision(settings_vision, bus, reloj=reloj)
    vision._al_camara_abierta(vision._gen_cam, "yunet")
    for k in range(1, 41):
        reloj.t = 100.0 + k * 0.5
        vision._tick(reloj.t)
    activas = [d for _, d in camara if d["activa"]]
    assert [d["duracion_ms"] for d in activas] == [0, 5000, 10000, 15000, 20000]
    assert all(d["fuente"] == "continua" and d["habilitada"] is True for d in activas)
    vision._al_camara_cerrada(vision._gen_cam, True, "", 20000)
    for k in range(41, 70):
        reloj.t = 100.0 + k * 0.5
        vision._tick(reloj.t)
    assert [d["activa"] for _, d in camara] == [True] * 5 + [False]
    assert camara[-1][1] == {"activa": False, "habilitada": True, "duracion_ms": 20000, "fuente": "continua"}


def test_como_vistazo(settings_vision: Settings) -> None:
    reloj = Reloj(10.0)
    vision = _vision(settings_vision, reloj=reloj)
    assert vision.como_vistazo("curiosidad") is None                 # cámara continua cerrada
    vision._al_camara_abierta(vision._gen_cam, "yunet")
    ocupada = vision.como_vistazo("curiosidad")                      # abierta, sin muestra aún
    assert ocupada is not None and ocupada.error == "ocupada" and ocupada.rostro is None
    vision._al_muestra_camara(vision._gen_cam, _m(0.0, n=2, brillo=0.42))
    reloj.t += 0.5
    r = vision.como_vistazo("curiosidad")
    assert r is not None and r.error is None and r.rostro is True and r.n_rostros == 2
    assert r.motivo == "curiosidad" and r.duracion_ms == 0 and r.brillo == pytest.approx(0.42)
    reloj.t += 1.0                                                   # muestra de hace 1,5 s
    assert vision.como_vistazo("curiosidad").error == "ocupada"
    vision._al_muestra_camara(vision._gen_cam, _m(0.0, rostro=False))
    assert vision.como_vistazo("presencia").rostro is False
    vision._detector_tipo = None
    assert vision.como_vistazo("presencia").rostro is None           # sin detector no se afirma nada


def test_set_camara_respeta_el_opt_in(settings_prueba: Settings, settings_vision: Settings) -> None:
    sin_camara = _vision(settings_prueba)
    assert sin_camara.set_camara(True) == "off"
    sin_continua = _vision(settings_prueba.con(camara_habilitada=True))
    assert sin_continua.set_camara(True) == "off"
    vision = _vision(settings_vision)
    assert vision.set_camara(False) == "off"
    assert vision.estado().camara == "off"
    assert vision.set_camara(True) == "continua"
    vision.pausar(60)
    assert vision.estado().camara == "pausada"
    assert vision.set_camara(True) == "continua"                     # encender quita la pausa


def test_pausa_temporal_vence_con_el_reloj(settings_vision: Settings) -> None:
    reloj = Reloj(0.0)
    vision = _vision(settings_vision, reloj=reloj)
    vision.pausar(30, motivo="usuario")
    assert vision.estado().camara == "pausada"
    reloj.t = 29.0
    assert vision.estado().camara == "pausada"
    reloj.t = 30.0
    assert vision.estado().camara == "continua"
    vision.pausar(None)
    reloj.t = 10_000.0
    assert vision.estado().camara == "pausada"
    vision.reanudar()
    assert vision.estado().camara == "continua"
    vision.pausar("no es un número")                               # nunca lanza: indefinida
    assert vision.estado().camara == "pausada"


# ---------------------------------------------------------------------------
# Pantalla: privacidad, payloads, ritmo y Hyprland (fuentes falsas)
# ---------------------------------------------------------------------------


class FuentePantalla:
    def __init__(self, titulo: str, clase: str = "code", *, titulo_despues: str | None = None,
                 completa: bool | None = False) -> None:
        self.titulos = [titulo] if titulo_despues is None else [titulo, titulo_despues]
        self.clase = clase
        self.completa = completa
        self.capturas = 0
        self.cerrada = False

    def metadatos(self) -> dict[str, Any]:
        titulo = self.titulos[0] if len(self.titulos) == 1 else self.titulos.pop(0)
        return {"clase": self.clase, "titulo": titulo, "pantalla_completa": self.completa, "reproduciendo": None}

    def capturar(self) -> Any:
        self.capturas += 1
        return np.full((108, 192, 3), 128, dtype=np.uint8)

    def geometria(self) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080)

    def cerrar(self) -> None:
        self.cerrada = True


def test_ventana_privada_sin_pixeles_y_sin_titulo(settings_vision: Settings) -> None:
    vision = _vision(settings_vision.con(pantalla_modo="pixeles"), fabrica_pantalla=lambda: None)
    for titulo, clase in (("KeePassXC - Base.kdbx", "keepassxc"), ("Bancolombia - Sucursal Virtual", "chrome"),
                          ("Nueva pestaña InPrivate - Microsoft Edge", "msedge")):
        fuente = FuentePantalla(titulo, clase)
        m = vision._muestrear_pantalla_sync(fuente, "pixeles")
        assert m.privada is True and m.titulo == "" and fuente.capturas == 0, titulo
        assert m.cambio == 0.0 and m.brillo == 0.0
    normal = FuentePantalla("main.py - rams_biocore - Visual Studio Code " + "x" * 80)
    m = vision._muestrear_pantalla_sync(normal, "pixeles")
    assert m.privada is False and normal.capturas == 1 and len(m.titulo) == 60
    assert m.titulo.startswith("main.py - rams_biocore") and m.brillo == pytest.approx(128 / 255, abs=0.01)
    # Solo metadatos: nunca se captura.
    solo = FuentePantalla("YouTube - Google Chrome", "chrome.exe")
    m = vision._muestrear_pantalla_sync(solo, "metadatos")
    assert solo.capturas == 0 and m.clase == "chrome" and m.titulo == "YouTube - Google Chrome"
    # Si durante la captura la ventana pasa a ser privada, el cuadro se descarta.
    cambia = FuentePantalla("Documento - Word", "winword", titulo_despues="Iniciar sesión - Bitwarden")
    m = vision._muestrear_pantalla_sync(cambia, "pixeles")
    assert cambia.capturas == 1 and m.privada is True and m.titulo == "" and m.brillo == 0.0


def test_pantalla_estado_payload_ritmo_y_estimulo(settings_vision: Settings, bus: BusEventos) -> None:
    eventos = _eventos(bus, "pantalla.*")
    reloj = Reloj(0.0)
    vision = _vision(settings_vision.con(pantalla_modo="pixeles"), bus, reloj=reloj, fabrica_pantalla=lambda: None)
    vision._pant_modo_actual = "pixeles"

    def muestra(clase: str = "code", titulo: str = "main.py", brillo: float = 0.5, **kw: Any) -> MuestraPantalla:
        base = {"ts": 0.0, "cambio": 0.0, "brillo": brillo, "tono": None, "corte": False, "video": False,
                "pantalla_completa": False, "reproduciendo": None, "clase": clase, "titulo": titulo,
                "privada": False, "ms_proc": 1.0}
        base.update(kw)
        return MuestraPantalla(**base)

    for k in range(25):                                       # 12 s iguales a 2 fps ⇒ latido cada 5 s
        reloj.t = k * 0.5
        vision._al_muestra_pantalla(vision._gen_pant, muestra())
    estados = [(d) for n, d in eventos if n == "pantalla.estado"]
    assert len(estados) == 3
    assert set(estados[0]) == {"cambio", "brillo", "tono", "video", "pantalla_completa", "reproduciendo",
                               "no_molestar", "privada", "clase", "titulo"}
    assert [n for n, _ in eventos if n == "pantalla.estimulo"] == []          # misma firma: nada
    eventos.clear()
    for k in range(24):                                       # cambia de app cada medio segundo
        reloj.t = 20.0 + k * 0.5
        vision._al_muestra_pantalla(vision._gen_pant, muestra(clase=f"app{k % 3}", brillo=0.1 * (k % 5)))
    estados = [t for t, (n, _) in enumerate(eventos) if n == "pantalla.estado"]
    assert 1 <= len(estados) <= 13                            # ≤ 1 Hz
    estimulos = [d for n, d in eventos if n == "pantalla.estimulo"]
    assert 1 <= len(estimulos) <= 3                           # ≤ 1 cada 5 s
    assert all(set(d) == {"firma", "magnitud"} and d["firma"].startswith("pantalla:") for d in estimulos)
    # Privada: título vacío en el payload; pantalla completa ⇒ no molestar.
    eventos.clear()
    reloj.t = 100.0
    vision._al_muestra_pantalla(vision._gen_pant, muestra(clase="keepassxc", titulo="", privada=True,
                                                         pantalla_completa=True))
    estado = [d for n, d in eventos if n == "pantalla.estado"][-1]
    assert estado["privada"] is True and estado["titulo"] == "" and estado["no_molestar"] is True
    _verificar_payloads(eventos)


def test_hilo_de_pantalla_con_fuente_falsa(settings_prueba: Settings, bus: BusEventos, correr) -> None:
    eventos = _eventos(bus, "pantalla.*", "vision.estado")
    fuentes: list[FuentePantalla] = []

    def fabrica() -> FuentePantalla:
        fuentes.append(FuentePantalla("KeePassXC", "keepassxc"))
        return fuentes[-1]

    ajustes = settings_prueba.con(pantalla_modo="pixeles", pantalla_fps=2.0)   # cámara apagada
    vision = _vision(ajustes, bus, fabrica_pantalla=fabrica)

    async def principal() -> list[DiagnosticoModulo]:
        diagnosticos = await vision.iniciar()
        assert await _esperar(lambda: any(n == "pantalla.estado" for n, _ in eventos))
        vision.pausar()
        assert await _esperar(lambda: vision._hilo_pant is None)
        await vision.cerrar()
        return diagnosticos

    diagnosticos = correr(principal())
    assert [d.estado for d in diagnosticos] == [EstadoModulo.OFF, EstadoModulo.OK]
    estado = next(d for n, d in eventos if n == "pantalla.estado")
    assert estado["privada"] is True and estado["titulo"] == "" and estado["clase"] == "keepassxc"
    assert fuentes and fuentes[0].capturas == 0 and fuentes[0].cerrada is True
    assert any(n == "vision.estado" and d["pantalla"] == "pixeles" for n, d in eventos)
    assert any(n == "vision.estado" and d["pantalla"] == "pausada" for n, d in eventos)
    _verificar_payloads(eventos)


def test_pantalla_en_hyprland_con_grim_falso(settings_vision: Settings, correr,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    vision = _vision(settings_vision.con(pantalla_modo="pixeles"))
    vision._plataforma = "hypr"
    vision._grim = "/usr/bin/grim"
    ventana = {"class": "firefox", "title": "YouTube - Mozilla Firefox", "fullscreen": 0, "contentType": "video"}
    monitores = [{"name": "DP-2", "focused": False, "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0},
                 {"name": "DP-1", "focused": True, "x": 1920, "y": 0, "width": 2560, "height": 1440, "scale": 1.25}]

    async def consulta(comando: str) -> Any:
        return dict(ventana) if comando == "j/activewindow" else monitores

    llamadas: list[list[str]] = []
    ppm = b"P6\n192 108\n255\n" + bytes([200, 100, 50]) * (192 * 108)

    async def ejecutar(argv: list[str], *, timeout: float, procesos: set[Any]) -> bytes:
        llamadas.append(list(argv))
        return ppm

    vision._consulta_hypr = consulta  # type: ignore[method-assign]
    monkeypatch.setattr(vc_mod, "_ejecutar", ejecutar)
    m = correr(vision._muestrear_pantalla_hypr("pixeles"))
    assert llamadas == [["/usr/bin/grim", "-s", "0.1", "-t", "ppm", "-o", "DP-1", "-"]]
    assert m.clase == "firefox" and m.titulo == "YouTube - Mozilla Firefox" and m.privada is False
    assert m.video is True and m.reproduciendo is True and m.pantalla_completa is False
    assert m.brillo == pytest.approx((0.299 * 200 + 0.587 * 100 + 0.114 * 50) / 255, abs=0.01)
    assert m.tono is not None
    # Ventana privada: ni grim ni título.
    ventana.update({"class": "Bitwarden", "title": "Bitwarden"})
    m = correr(vision._muestrear_pantalla_hypr("pixeles"))
    assert len(llamadas) == 1 and m.privada is True and m.titulo == ""
    # Metadatos de Hyprland: pantalla completa (modo 2) y juego.
    meta = vc_mod._meta_hypr({"class": "steam_app_1", "title": "Juego", "fullscreen": 2, "contentType": "game"})
    assert meta["pantalla_completa"] is True and meta["juego"] is True and meta["reproduciendo"] is False
    assert vc_mod._meta_hypr({"fullscreen": 1})["pantalla_completa"] is False   # maximizada
    assert vc_mod._meta_hypr(None)["clase"] == ""


def test_set_pantalla_y_caja(settings_vision: Settings) -> None:
    vision = _vision(settings_vision, fabrica_pantalla=lambda: None)
    assert vision.estado().pantalla == "off"
    assert vision.set_pantalla("metadatos") == "metadatos"
    assert vision.set_pantalla("pixeles") == "pixeles"
    assert vision.set_pantalla("todo") == "pixeles"                  # inválido: no cambia
    assert vision.set_pantalla("off") == "off"
    sin_fuente = _vision(settings_vision)                            # plataforma "otro" sin fábrica
    assert sin_fuente.set_pantalla("pixeles") == "off"
    vision.set_caja_avatar((10, 20, 340, 600))
    assert vision._caja == (10, 20, 340, 600)
    vision.set_caja_avatar((10, 20, 0, 600))                         # inválida: se ignora
    vision.set_caja_avatar("basura")  # type: ignore[arg-type]
    assert vision._caja == (10, 20, 340, 600)
    vision.set_caja_avatar(None)
    assert vision._caja is None


# ---------------------------------------------------------------------------
# Diagnósticos, detector y estado
# ---------------------------------------------------------------------------


def test_diagnosticos(settings_prueba: Settings, settings_vision: Settings, correr) -> None:
    vision = _vision(settings_prueba)

    async def principal() -> list[DiagnosticoModulo]:
        diagnosticos = await vision.iniciar()
        await vision.cerrar()
        return diagnosticos

    camara, pantalla = correr(principal())
    assert (camara.clave, camara.estado, camara.detalle) == ("vision_camara", EstadoModulo.OFF,
                                                             "cámara deshabilitada (RAMS_CAMERA=0)")
    assert (pantalla.clave, pantalla.estado) == ("vision_pantalla", EstadoModulo.OFF)
    sin_continua = _vision(settings_prueba.con(camara_habilitada=True))
    assert sin_continua._diagnostico_camara().detalle == "desactivada (RAMS_VISION_CONTINUA=0)"
    vision = _vision(settings_vision, fabrica_captura=lambda indice: None)
    vision._detector_tipo = None
    assert vision._diagnostico_camara().estado == EstadoModulo.DEGRADADO
    vision._detector_tipo = "yunet"
    diag = vision._diagnostico_camara()
    assert diag.estado == EstadoModulo.OK and diag.detalle.startswith("YuNet a 3 fps")
    metadatos = _vision(settings_vision.con(pantalla_modo="metadatos"))
    assert metadatos._diagnostico_pantalla().estado == EstadoModulo.OFF            # sin fuente en "otro"
    metadatos._plataforma = "win"
    assert metadatos._diagnostico_pantalla().estado == EstadoModulo.OK
    pixeles = _vision(settings_vision.con(pantalla_modo="pixeles"), fabrica_pantalla=lambda: None)
    assert pixeles._diagnostico_pantalla().estado == EstadoModulo.OK


def test_seleccion_del_modelo_yunet(settings_vision: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    modelos = settings_vision.data_dir / "models"
    modelos.mkdir(parents=True, exist_ok=True)
    for nombre in ("face_detection_yunet_2023mar.onnx", "face_detection_yunet_2023mar_int8.onnx",
                   "face_detection_yunet_2026may.onnx"):
        (modelos / nombre).write_bytes(b"")
    vision = _vision(settings_vision)
    monkeypatch.setattr(vc_mod, "_version_mayor_cv2", lambda: 5)
    assert vision._ruta_yunet().name == "face_detection_yunet_2026may.onnx"
    monkeypatch.setattr(vc_mod, "_version_mayor_cv2", lambda: 4)
    assert vision._ruta_yunet().name == "face_detection_yunet_2023mar.onnx"
    explicito = modelos / "mi_modelo.onnx"
    explicito.write_bytes(b"")
    assert _vision(settings_vision.con(vision_modelo=explicito))._ruta_yunet() == explicito
    assert _vision(settings_vision.con(vision_modelo=modelos / "no_existe.onnx"))._ruta_yunet() is None
    # Crear el detector con un modelo inválido no lanza (cae a Haar o a ninguno).
    detector, tipo = vision._crear_detector_defecto((320, 240))
    assert tipo in (None, "haar")
    assert vc_mod._DIRS_HAAR_SISTEMA[-2:] == ("/usr/share/opencv5/haarcascades",
                                              "/usr/local/share/opencv5/haarcascades")


def test_vision_estado_solo_al_cambiar(settings_vision: Settings, bus: BusEventos) -> None:
    eventos = _eventos(bus, "vision.estado")
    reloj = Reloj(0.0)
    vision = _vision(settings_vision, bus, reloj=reloj)
    for k in range(10):
        reloj.t = k * 0.5
        vision._tick(reloj.t)
    assert len(eventos) == 1
    vision.pausar()
    vision._tick(reloj.t)
    assert len(eventos) == 2 and eventos[-1][1]["camara"] == "pausada"
    assert set(eventos[-1][1]) == {"camara", "pantalla", "fps_camara", "fps_pantalla", "nivel", "detector",
                                   "presente", "atencion"}
    _verificar_payloads(eventos)


def test_integracion_de_muestras_publica_solo_escalares(settings_vision: Settings, bus: BusEventos) -> None:
    eventos = _eventos(bus, "vision.*", "pantalla.*")
    reloj = Reloj(0.0)
    vision = _vision(settings_vision, bus, reloj=reloj)
    vision._al_camara_abierta(vision._gen_cam, "yunet")
    for k in range(90):                                        # 30 s: llegada, mirada, atención
        reloj.t = k / 3
        cuadro_muestra = _m(0.0, u=0.4 + 0.01 * (k % 7), dist=0.6 + 0.01 * (k % 3))
        vision._al_muestra_camara(vision._gen_cam, cuadro_muestra)
        if k % 2 == 0:
            vision._tick(reloj.t)
    nombres = {n for n, _ in eventos}
    assert {"vision.camara", "vision.estimulo", "vision.mirada", "vision.rostro", "vision.atencion",
            "vision.estado"} <= nombres
    assert vision.estado().presente is True
    vision.calibrar_atencion()
    _verificar_payloads(eventos)


# ---------------------------------------------------------------------------
# Regresiones de la revisión adversarial (v0.2)
# ---------------------------------------------------------------------------


def _alternar(integ: Integrador, hasta: float, cada: float, rostro_en: Any) -> list[tuple[float, str, dict[str, Any]]]:
    """Muestras cada ``cada`` segundos y ticks a 2 Hz hasta ``hasta`` (reloj falso)."""
    eventos: list[tuple[float, str, dict[str, Any]]] = []
    t_m = 0.0
    for paso in range(int(hasta / 0.5) + 1):
        t = paso * 0.5
        while t_m <= t:
            m = _m(t_m) if rostro_en(t_m) else _m(t_m, rostro=False)
            eventos += [(t_m, n, d) for n, d in integ.muestra(m)]
            t_m += cada
        eventos += [(t, n, d) for n, d in integ.tick(t)]
    return eventos


def test_histeresis_escalada_al_ritmo_real_a_1_fps() -> None:
    """A 1 fps (``RAMS_VISION_FPS=1`` o nivel 2) cada cuadro llega a ~1,05 s: hay llegada y no hay parpadeo."""
    for integ in (Integrador(periodo_s=1.0), Integrador()):     # con el periodo objetivo o solo midiendo
        integ.abrir(0.0)
        eventos = _alternar(integ, 60.0, 1.05, lambda t: True)
        assert len(_tipos(eventos, "llegada")) == 1
        presentes = [d["presente"] for _, n, d in eventos if n == "vision.rostro"]
        assert presentes and all(presentes)                    # nunca parpadea a False
        assert integ.presente is True and 2.5 <= integ.hueco_s <= vc_mod.HUECO_MAX_S
    # La salida sigue llegando 20 s después de la última cara.
    integ = Integrador(periodo_s=1.0)
    integ.abrir(0.0)
    eventos = _alternar(integ, 45.0, 1.05, lambda t: t <= 10.0)
    ultima_cara = max(k * 1.05 for k in range(20) if k * 1.05 <= 10.0)
    assert _tipos(eventos, "salida") == [pytest.approx(math.ceil((ultima_cara + 20.0) * 2) / 2)]
    # A 3 fps la histéresis no cambia (1 s).
    integ = Integrador()
    integ.abrir(0.0)
    _simular(integ, 0.0, 5.0, lambda t: True, eventos=[])
    assert integ.hueco_s == vc_mod.HUECO_ROSTRO_S


class CapturaLenta(CapturaFalsa):
    """Cada lectura tarda 60 ms (como la 2.ª lectura de DSHOW esperando al cuadro siguiente)."""

    def grab(self) -> bool:
        time.sleep(0.06)
        return True


class DetectorCronometrado:
    """Detector falso que siempre ve una cara y anota cuándo se le llama (una vez por cuadro)."""

    def __init__(self) -> None:
        self.tiempos: list[float] = []

    def detect(self, imagen: Any) -> Any:
        self.tiempos.append(time.monotonic())
        return 1, np.asarray([[140, 90, 40, 50, 150, 105, 170, 105, 160, 115, 152, 125, 168, 125, 0.95]],
                             dtype=np.float32)


def test_hilo_a_1_fps_cadencia_fija_y_llegada(settings_vision: Settings, bus: BusEventos, correr) -> None:
    eventos = _eventos(bus, "vision.estimulo", "vision.rostro")
    detector = DetectorCronometrado()
    vision = _vision(settings_vision.con(vision_fps=1.0), bus, fabrica_captura=lambda indice: CapturaLenta())
    vision._crear_detector = lambda tam: (detector, "yunet")  # type: ignore[method-assign]

    async def principal() -> None:
        await vision.iniciar()
        assert await _esperar(lambda: len(detector.tiempos) >= 4, 8.0)
        await vision.cerrar()

    correr(principal())
    intervalos = [b - a for a, b in zip(detector.tiempos, detector.tiempos[1:])]
    assert intervalos and sum(intervalos) / len(intervalos) < 1.04     # la lectura de 60 ms no se suma
    assert [d["tipo"] for n, d in eventos if n == "vision.estimulo"].count("llegada") == 1
    presentes = [d["presente"] for n, d in eventos if n == "vision.rostro"]
    assert presentes and presentes[0] is True and False not in presentes[:-1]   # el último puede ser el cierre


def test_como_vistazo_respeta_pausas_y_no_compite_por_el_dispositivo(settings_vision: Settings) -> None:
    reloj = Reloj(10.0)
    vision = _vision(settings_vision, reloj=reloj)
    # El hilo continuo vive pero aún no abrió (DSHOW/MSMF tardan 1–3 s) o está liberando: es suyo.
    soltar = threading.Event()
    vision._hilo_cam = threading.Thread(target=soltar.wait, args=(5.0,), daemon=True)
    vision._hilo_cam.start()
    try:
        abriendo = vision.como_vistazo("presencia")
        assert abriendo is not None and abriendo.error == "ocupada" and abriendo.rostro is None
    finally:
        soltar.set()
        vision._hilo_cam.join(1.0)
    assert vision.como_vistazo("presencia") is None                  # el hilo ya soltó: aura puede parpadear
    vision._hilo_cam = None
    assert vision.como_vistazo("presencia") is None                  # sin hilo: aura puede parpadear
    # "Deja de mirarme": ni curiosidad ni presencia; lo que el usuario pide ahora, sí.
    vision.pausar(None, "usuario")
    for motivo in ("curiosidad", "presencia"):
        r = vision.como_vistazo(motivo)
        assert r is not None and r.error == "denegada" and r.motivo == motivo
    assert vision.como_vistazo("usuario") is None
    vision.reanudar()
    assert vision.como_vistazo("curiosidad") is None
    # Autopausa por sesión bloqueada o por juego: tampoco.
    for entorno in ({"bloqueada": True}, {"juego": True}):
        vision._aplicar_entorno(reloj.t, entorno)
        assert vision.como_vistazo("presencia").error == "denegada", entorno
    vision._aplicar_entorno(reloj.t, {})
    assert vision.como_vistazo("presencia") is None
    # App de llamada (pegajosa 5 min): la cámara es de la llamada, ni siquiera a petición del usuario.
    vision._aplicar_entorno(reloj.t, {"app": "Zoom.exe"})
    for motivo in ("usuario", "curiosidad", "presencia"):
        assert vision.como_vistazo(motivo).error == "ocupada", motivo
    reloj.t += vc_mod.LLAMADA_PEGAJOSA_S + 1.0
    vision._aplicar_entorno(reloj.t, {"app": "code"})
    assert vision.como_vistazo("curiosidad") is None


def test_ausencia_por_inactividad_no_apaga_la_camara_que_ve_al_usuario(settings_vision: Settings) -> None:
    """Viendo un vídeo 10 min sin teclado ni ratón la presencia dice "ausente 0,8"; la cámara la ve."""
    reloj = Reloj(0.0)
    presencia_estado = types.SimpleNamespace(presente=False, confianza=0.8)
    presencia = types.SimpleNamespace(estado=lambda: presencia_estado)
    vision = _vision(settings_vision, reloj=reloj, presencia=presencia)
    vision._al_camara_abierta(vision._gen_cam, "yunet")
    for k in range(12):
        reloj.t = k / 3
        vision._al_muestra_camara(vision._gen_cam, _m(0.0))
        if k % 2 == 0:
            vision._tick(reloj.t)
    assert vision.estado().presente is True
    assert vision._evaluar_autopausa(reloj.t) == "" and vision.estado().camara == "continua"
    # Se va: tras su propio flanco de salida (20 s sin cara), ahora sí, autopausa por ausencia.
    fin = reloj.t + 21.0
    while reloj.t < fin:
        reloj.t += 0.5
        vision._al_muestra_camara(vision._gen_cam, _m(0.0, rostro=False))
        vision._tick(reloj.t)
    assert vision._autopausa == "ausente" and vision.estado().camara == "pausada"
    # Sin detector la cámara no puede contradecir a la presencia.
    sin_detector = _vision(settings_vision, reloj=reloj, presencia=presencia)
    sin_detector._al_camara_abierta(sin_detector._gen_cam, None)
    assert sin_detector._evaluar_autopausa(reloj.t) == "ausente"


def _terminal_con_scroll(k: int) -> Any:
    """Pantalla 64×36: escritorio quieto y una terminal (25×14 celdas) con líneas que suben."""
    img = np.full((36, 64, 3), 40, dtype=np.uint8)
    lineas = np.random.default_rng(1234).integers(0, 2, size=(200, 25)).astype(np.uint8) * 180 + 20
    for fila in range(14):
        img[4 + fila, 30:55, :] = lineas[(fila + k) % 200][:, None]
    return img


def test_no_molestar_solo_con_senales_del_sistema(settings_vision: Settings, bus: BusEventos) -> None:
    eventos = _eventos(bus, "pantalla.estado")
    reloj = Reloj(0.0)
    vision = _vision(settings_vision.con(pantalla_modo="pixeles"), bus, reloj=reloj, fabrica_pantalla=lambda: None)
    vision._pant_modo_actual = "pixeles"

    class Terminal(FuentePantalla):
        def capturar(self) -> Any:
            self.capturas += 1
            return _terminal_con_scroll(self.capturas)

    terminal = Terminal("python main.py", "windowsterminal")
    for k in range(12):                                       # la propia consola de R.A.M.S. con logs
        reloj.t = float(k)
        vision._al_muestra_pantalla(vision._gen_pant, vision._muestrear_pantalla_sync(terminal, "pixeles"))
    assert eventos[-1][1]["video"] is True and eventos[-1][1]["no_molestar"] is False

    def publicar(**kw: Any) -> dict[str, Any]:
        base = {"ts": 0.0, "cambio": 0.1, "brillo": 0.5, "tono": None, "corte": False, "video": False,
                "pantalla_completa": False, "reproduciendo": None, "clase": "chrome", "titulo": "YouTube",
                "privada": False, "ms_proc": 1.0}
        base.update(kw)
        reloj.t += vc_mod.LATIDO_PANTALLA_S
        vision._al_muestra_pantalla(vision._gen_pant, MuestraPantalla(**base))
        return eventos[-1][1]

    assert publicar(video=True, reproduciendo=True)["no_molestar"] is False   # "¿Otra vez YouTube?"
    assert publicar(video=True, pantalla_completa=True)["no_molestar"] is True
    vision._entorno = {"juego": True}
    assert publicar()["no_molestar"] is True
    vision._entorno = {}
    vision._llamada_hasta = reloj.t + 100.0
    assert publicar()["no_molestar"] is True
    vision._llamada_hasta = -math.inf
    assert publicar()["no_molestar"] is False


def test_pantalla_sin_metadatos_falla_cerrada(settings_vision: Settings, correr,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    vision = _vision(settings_vision.con(pantalla_modo="pixeles"), fabrica_pantalla=lambda: None)
    # Sin título ni clase (Win32 roto, sin ventana en primer plano): no se sabe si es privada ⇒ sin píxeles.
    vacia = FuentePantalla("", "")
    m = vision._muestrear_pantalla_sync(vacia, "pixeles")
    assert vacia.capturas == 0 and m.brillo == 0.0 and m.titulo == "" and m.clase == ""
    assert vision._muestrear_pantalla_sync(FuentePantalla("  ", " "), "pixeles").brillo == 0.0

    class Secuencia(FuentePantalla):
        def __init__(self, metas: list[dict[str, Any]]) -> None:
            super().__init__("x")
            self.metas = metas

        def metadatos(self) -> dict[str, Any]:
            return self.metas.pop(0) if len(self.metas) > 1 else self.metas[0]

    # Metadatos válidos antes de capturar pero no después: el cuadro se descarta.
    cambia = Secuencia([{"clase": "code", "titulo": "main.py"}, {}])
    m = vision._muestrear_pantalla_sync(cambia, "pixeles")
    assert cambia.capturas == 1 and m.brillo == 0.0 and m.clase == "code" and m.titulo == "main.py"
    # X11: mss captura, pero sin metadatos de ventanas no hay píxeles (ni pantalla).
    x11 = _vision(settings_vision.con(pantalla_modo="pixeles"))
    x11._plataforma = "x11"
    assert x11._pixeles_posibles() is False and x11._crear_fuente_pantalla() is None
    assert x11.estado().pantalla == "off"
    diagnostico = x11._diagnostico_pantalla()
    assert diagnostico.estado == EstadoModulo.OFF and "X11" in diagnostico.detalle
    # Hyprland con el socket caído o lento (consulta None): ni grim ni píxeles.
    hypr = _vision(settings_vision.con(pantalla_modo="pixeles"))
    hypr._plataforma = "hypr"
    hypr._grim = "/usr/bin/grim"
    ventanas: list[Any] = [None]
    monitores = [{"name": "DP-1", "focused": True, "x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1.0}]

    async def consulta(comando: str) -> Any:
        if comando == "j/activewindow":
            return ventanas.pop(0) if len(ventanas) > 1 else ventanas[0]
        return monitores

    llamadas: list[list[str]] = []
    ppm = b"P6\n192 108\n255\n" + bytes([10, 20, 30]) * (192 * 108)

    async def ejecutar(argv: list[str], *, timeout: float, procesos: set[Any]) -> bytes:
        llamadas.append(list(argv))
        return ppm

    hypr._consulta_hypr = consulta  # type: ignore[method-assign]
    monkeypatch.setattr(vc_mod, "_ejecutar", ejecutar)
    m = correr(hypr._muestrear_pantalla_hypr("pixeles"))
    assert llamadas == [] and m.privada is False and m.brillo == 0.0
    ventanas[:] = [{}]                                        # sin ventana activa: tampoco
    assert correr(hypr._muestrear_pantalla_hypr("pixeles")).brillo == 0.0 and llamadas == []
    # La re-consulta tras la captura falla: el cuadro se descarta.
    ventanas[:] = [{"class": "code", "title": "main.py"}, None]
    m = correr(hypr._muestrear_pantalla_hypr("pixeles"))
    assert len(llamadas) == 1 and m.brillo == 0.0 and m.clase == "code"
    ventanas[:] = [{"class": "code", "title": "main.py"}]
    m = correr(hypr._muestrear_pantalla_hypr("pixeles"))
    assert len(llamadas) == 2 and m.brillo > 0.0


def test_geometria_de_monitor_hyprland_rotado() -> None:
    geometria = vc_mod._geometria_monitor_hypr
    base = {"x": 0, "y": 0, "width": 2560, "height": 1440, "scale": 1.0}
    assert geometria({**base, "x": 1920, "scale": 1.25}) == (1920.0, 0.0, 2048.0, 1152.0)
    for transformacion in (1, 3, 5, 7):                       # 90°/270° (también volteados)
        assert geometria({**base, "transform": transformacion}) == (0.0, 0.0, 1440.0, 2560.0)
    for transformacion in (0, 2, 4, 6):
        assert geometria({**base, "transform": transformacion}) == (0.0, 0.0, 2560.0, 1440.0)
    assert geometria({"x": 0}) is None and geometria(None) is None
    assert geometria({**base, "width": 0}) is None
    # En un monitor vertical la máscara cubre al avatar (con la geometría sin rotar quedaba fuera).
    caja = (0, 1500, 340, 600)
    assert mascara_caja(caja, geometria({**base, "transform": 1})).any()
    assert not mascara_caja(caja, (0.0, 0.0, 2560.0, 1440.0)).any()


def test_mirada_con_latido_si_el_usuario_esta_quieto() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    rng = np.random.default_rng(0)
    tiempos: list[float] = []
    for k in range(90):                                       # 30 s a 3 fps con ±2 px de ruido
        u = 0.45 + float(rng.normal(0, 2 / 320))
        v = 0.48 + float(rng.normal(0, 2 / 240))
        tiempos += [k / 3 for n, _ in integ.muestra(_m(k / 3, u=u, v=v)) if n == "vision.mirada"]
    huecos = [b - a for a, b in zip(tiempos, tiempos[1:])]
    assert tiempos[0] == 0.0 and tiempos[-1] >= 27.0
    assert max(huecos) <= vc_mod.LATIDO_MIRADA_S + 1 / 3 + 1e-6      # la corteza la caduca a los 5 s
    assert min(huecos) >= vc_mod.INTERVALO_MIRADA_S - 1e-9


def test_reabrir_tras_una_pausa_larga_olvida_el_flanco() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    _simular(integ, 0.0, 4.0, lambda t: True, eventos=[])
    assert integ.presente is True and integ.atencion > 0.0
    cierre = integ.cerrar()
    assert cierre == [("vision.rostro", {"presente": False, "n": 0, "dist_m": None, "frontal": None,
                                         "atencion": 0.0}),
                      ("vision.atencion", {"nivel": 0.0})]
    assert integ.cerrar() == [] and integ.atencion == 0.0       # un segundo cierre no repite nada
    integ.abrir(10.0)                                         # reintento rápido (< 20 s): se conserva
    assert integ.presente is True
    integ.cerrar()
    integ.abrir(36_000.0)                                     # 10 h después (pausa nocturna)
    integ.tick(36_000.5)
    assert integ.presente is None
    eventos: list[tuple[float, str, dict[str, Any]]] = []
    _simular(integ, 36_000.5, 36_003.0, lambda t: True, eventos=eventos)
    assert len(_tipos(eventos, "llegada")) == 1 and integ.presente is True


def test_pausa_publica_que_ya_no_ve_a_nadie(settings_vision: Settings, bus: BusEventos) -> None:
    eventos = _eventos(bus, "vision.rostro", "vision.atencion")
    reloj = Reloj(0.0)
    vision = _vision(settings_vision, bus, reloj=reloj)
    vision._al_camara_abierta(vision._gen_cam, "yunet")
    for k in range(12):
        reloj.t = k / 3
        vision._al_muestra_camara(vision._gen_cam, _m(0.0))
        if k % 2 == 0:
            vision._tick(reloj.t)
    assert vision.estado().presente is True and vision.estado().atencion > 0.0
    vision.pausar(None, "usuario")
    vision._al_camara_cerrada(vision._gen_cam, True, "", 4000)
    assert eventos[-2:] == [("vision.rostro", {"presente": False, "n": 0, "dist_m": None, "frontal": None,
                                               "atencion": 0.0}),
                            ("vision.atencion", {"nivel": 0.0})]
    assert vision.estado().presente is None and vision.estado().atencion == 0.0
    _verificar_payloads(eventos)


def test_con_terceros_no_se_mide_la_atencion() -> None:
    integ = Integrador()
    integ.abrir(0.0)
    eventos: list[tuple[str, dict[str, Any]]] = []
    for k in range(30):                                       # 10 s con dos caras de frente y cerca
        eventos += integ.muestra(_m(k / 3, n=2, dist=0.5))
        eventos += integ.tick(k / 3)
    assert all(d["atencion"] == 0.0 for n, d in eventos if n == "vision.rostro")
    assert all(d["nivel"] == 0.0 for n, d in eventos if n == "vision.atencion")
    for k in range(30, 60):                                   # se queda sola: la atención sube
        integ.muestra(_m(k / 3, dist=0.5))
        integ.tick(k / 3)
    sola = integ.atencion
    assert sola > 0.5
    for k in range(60, 75):                                   # llega alguien más: baja, no se mide al otro
        integ.muestra(_m(k / 3, n=2, dist=0.5))
        integ.tick(k / 3)
    assert integ.atencion < sola
