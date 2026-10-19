"""Tests de lobes/capturas.py (grupo telegram: fotos de cámara y pantalla bajo pedido).

Sin hardware: la webcam real NUNCA se abre (``_abrir_camara`` se sustituye por capturas falsas),
la pantalla real nunca se captura (``mss`` y ``grim`` falsos) y las ventanas (Win32 y el socket
de Hyprland) también son falsas. Nada se escribe en disco. Sin pytest-asyncio: ``asyncio.run``
(fixture ``correr``).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

import lobes.capturas as cap  # noqa: E402
from core.config import Settings  # noqa: E402
from core.contracts import ResultadoVistazo  # noqa: E402
from lobes.capturas import ResultadoCaptura, capturar_jpeg, codificar_jpeg, dimensiones_jpeg  # noqa: E402

MONITOR_1 = {"left": 0, "top": 0, "width": 1920, "height": 1080}
MONITOR_2 = {"left": 1920, "top": 0, "width": 2560, "height": 1440}
VSCODE = {"app": "code", "titulo": "Visual Studio Code", "clase": "Chrome_WidgetWin_1"}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sin_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ninguna prueba abre la cámara real, captura la pantalla real ni lee las ventanas reales.

    Por defecto la plataforma es Windows con ``mss`` y Win32 prohibidos: cada prueba de pantalla
    pone los suyos falsos (``_win_falso`` y ``_mss_falso``).
    """
    def prohibido(*_args: Any, **_kw: Any) -> Any:
        raise AssertionError("se intentó usar hardware real")

    async def prohibido_async(*_args: Any, **_kw: Any) -> Any:
        raise AssertionError("se intentó usar el sistema real")

    monkeypatch.setattr(cap, "_abrir_camara", prohibido)
    monkeypatch.setattr(cap, "_mss", types.SimpleNamespace(MSS=prohibido))
    monkeypatch.setattr(cap._vc, "_crear_win32", prohibido)
    monkeypatch.setattr(cap._vc, "_plataforma_actual", lambda: "win")
    monkeypatch.setattr(cap._vc, "_consulta_hypr_socket", prohibido_async)
    monkeypatch.setattr(cap._vc, "_ejecutar", prohibido_async)
    monkeypatch.setattr(cap, "CALENTAMIENTO_MIN_S", 0.0)


@pytest.fixture
def settings_cam(settings_prueba: Settings) -> Settings:
    return settings_prueba.con(camara_habilitada=True)


def _imagen(alto: int = 480, ancho: int = 640, canales: int = 3) -> Any:
    img = np.zeros((alto, ancho, canales), dtype=np.uint8)
    img[:, : ancho // 2] = 200
    img[alto // 3:, :, 0] = 90
    return img


class CapturaFalsa:
    """Webcam falsa: cuenta los grabs y registra el hilo de ``release()``."""

    def __init__(self, *, lee: bool = True, espera_s: float = 0.0) -> None:
        self.grabs = 0
        self.lecturas = 0
        self.ajustes: list[tuple[int, Any]] = []
        self.hilo_release: str | None = None
        self.liberada = False
        self.lee = lee
        self.espera_s = espera_s

    def isOpened(self) -> bool:  # noqa: N802 - API de OpenCV
        return True

    def set(self, propiedad: int, valor: Any) -> bool:
        self.ajustes.append((propiedad, valor))
        return True

    def grab(self) -> bool:
        if self.espera_s:
            time.sleep(self.espera_s)
        self.grabs += 1
        return True

    def read(self) -> tuple[bool, Any]:
        self.lecturas += 1
        return (True, _imagen(720, 1280)) if self.lee else (False, None)

    def release(self) -> None:
        self.hilo_release = threading.current_thread().name
        self.liberada = True


class VisionFalsa:
    """Lo que ``capturas`` usa de ``VisionContinua``: ``instantanea_jpeg`` y ``como_vistazo``."""

    def __init__(self, jpeg: bytes | None, vistazo: ResultadoVistazo | None = None) -> None:
        self.jpeg = jpeg
        self.vistazo = vistazo
        self.pedidos: list[dict[str, Any]] = []
        self.vistazos: list[str] = []

    async def instantanea_jpeg(self, *, calidad: int = 85, max_lado: int = 1600) -> bytes | None:
        self.pedidos.append({"calidad": calidad, "max_lado": max_lado})
        return self.jpeg

    def como_vistazo(self, motivo: str) -> ResultadoVistazo | None:
        self.vistazos.append(motivo)
        return self.vistazo


def _jpeg(ancho: int = 320, alto: int = 240) -> bytes:
    ok, buffer = cv2.imencode(".jpg", _imagen(alto, ancho))
    assert ok
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# Fuente: sin disco, sin shell, UTF-8 + LF
# ---------------------------------------------------------------------------


def test_fuente_sin_disco_ni_shell() -> None:
    crudo = Path(inspect.getsourcefile(cap) or "").read_bytes()
    fuente = crudo.decode("utf-8")
    for prohibido in ("imwrite", "VideoWriter", "tempfile", "NamedTemporaryFile", "shell=True", "base64",
                      ".save(", "write_bytes"):
        assert prohibido not in fuente, prohibido
    assert re.search(r"open\([^)]*['\"][rwax+]*b", fuente) is None
    assert b"\r" not in crudo


def test_resultado_sin_bytes_en_el_repr() -> None:
    resultado = ResultadoCaptura(True, b"\xff\xd8secreto", 10, 20, "camara", None)
    assert "secreto" not in repr(resultado)
    assert resultado.ok and resultado.motivo is None
    with pytest.raises(Exception):
        resultado.ok = False  # type: ignore[misc]  # frozen
    assert cap.MOTIVOS == frozenset({"deshabilitada", "sin_camara", "camara_ocupada", "ventana_sensible",
                                     "sin_backend", "error"})
    firma = inspect.signature(capturar_jpeg)
    assert inspect.iscoroutinefunction(capturar_jpeg)
    for nombre in ("settings", "vision", "ventana_titulo", "calidad", "max_lado"):
        assert firma.parameters[nombre].kind is inspect.Parameter.KEYWORD_ONLY
    assert firma.parameters["calidad"].default == 85 and firma.parameters["max_lado"].default == 1600


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------


def test_codificar_jpeg_reduce_y_acepta_bgra_y_gris() -> None:
    jpeg, ancho, alto = codificar_jpeg(_imagen(1080, 1920), calidad=80, max_lado=1600)
    assert jpeg.startswith(b"\xff\xd8") and (ancho, alto) == (1600, 900)
    assert dimensiones_jpeg(jpeg) == (1600, 900)
    jpeg_bgra, ancho, alto = codificar_jpeg(_imagen(300, 400, 4), max_lado=1600)
    assert (ancho, alto) == (400, 300) and dimensiones_jpeg(jpeg_bgra) == (400, 300)
    gris = np.full((50, 70), 128, dtype=np.uint8)
    assert codificar_jpeg(gris)[1:] == (70, 50)
    assert codificar_jpeg(np.zeros((10, 10, 3), dtype=np.float32)) is None
    assert codificar_jpeg(None) is None
    # calidad y lado se acotan (30–95, 64–4096)
    _, ancho, alto = codificar_jpeg(_imagen(1000, 1000), calidad=500, max_lado=1)
    assert (ancho, alto) == (64, 64)


def test_dimensiones_jpeg_tolera_basura() -> None:
    assert dimensiones_jpeg(b"") is None
    assert dimensiones_jpeg(b"\x89PNG\r\n") is None
    assert dimensiones_jpeg(b"\xff\xd8\xff\xe0\x00") is None
    assert dimensiones_jpeg(_jpeg(123, 45)) == (123, 45)


# ---------------------------------------------------------------------------
# Cámara
# ---------------------------------------------------------------------------


def test_camara_deshabilitada(settings_prueba: Settings, correr) -> None:
    vision = VisionFalsa(_jpeg())
    resultado = correr(capturar_jpeg("camara", settings=settings_prueba, vision=vision))
    assert (resultado.ok, resultado.motivo, resultado.jpeg) == (False, "deshabilitada", None)
    assert vision.pedidos == []


def test_camara_desde_la_vision_continua_sin_abrir_el_dispositivo(settings_cam: Settings, correr) -> None:
    vision = VisionFalsa(_jpeg(640, 480))
    resultado = correr(capturar_jpeg("cámara", settings=settings_cam, vision=vision, calidad=70, max_lado=800))
    assert resultado.ok and resultado.fuente == "camara" and resultado.motivo is None
    assert (resultado.ancho, resultado.alto) == (640, 480)
    assert resultado.jpeg == vision.jpeg
    assert vision.pedidos == [{"calidad": 70, "max_lado": 800}]


def test_camara_de_la_vision_ocupada_no_se_abre(settings_cam: Settings, correr) -> None:
    for vistazo in (ResultadoVistazo(None, motivo="usuario", error="ocupada"),
                    ResultadoVistazo(True, n_rostros=1, motivo="usuario")):
        vision = VisionFalsa(None, vistazo)
        resultado = correr(capturar_jpeg("camara", settings=settings_cam, vision=vision))
        assert (resultado.ok, resultado.motivo) == (False, "camara_ocupada")
        assert vision.vistazos == ["usuario"]


def test_camara_un_instante_descarta_exposicion_y_libera(settings_cam: Settings, correr,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    capturas: list[CapturaFalsa] = []

    def abrir(indice: int) -> CapturaFalsa:
        capturas.append(CapturaFalsa())
        return capturas[-1]

    monkeypatch.setattr(cap, "_abrir_camara", abrir)
    vision = VisionFalsa(None, None)          # visión sin cámara abierta y sin veto
    resultado = correr(capturar_jpeg("camara", settings=settings_cam, vision=vision, max_lado=640))
    assert resultado.ok and resultado.motivo is None
    assert (resultado.ancho, resultado.alto) == (640, 360)
    assert dimensiones_jpeg(resultado.jpeg) == (640, 360)
    c = capturas[0]
    assert c.grabs >= cap.CUADROS_EXPOSICION >= 3 and c.lecturas == 1
    assert c.liberada and c.hilo_release != threading.main_thread().name   # release() en el executor
    assert not cap._CERROJO_CAMARA.locked()


def test_camara_sin_vision_y_fallos(settings_cam: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap, "_abrir_camara", lambda indice: None)
    assert correr(capturar_jpeg("camara", settings=settings_cam)).motivo == "sin_camara"
    sin_cuadros = CapturaFalsa(lee=False)
    monkeypatch.setattr(cap, "_abrir_camara", lambda indice: sin_cuadros)
    resultado = correr(capturar_jpeg("camara", settings=settings_cam))
    assert (resultado.ok, resultado.motivo) == (False, "camara_ocupada")
    assert sin_cuadros.liberada
    assert not cap._CERROJO_CAMARA.locked()
    monkeypatch.setattr(cap, "cv2", None)
    assert correr(capturar_jpeg("camara", settings=settings_cam)).motivo == "sin_backend"


def test_camara_un_pedido_a_la_vez(settings_cam: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap, "_abrir_camara", lambda indice: CapturaFalsa())
    assert cap._CERROJO_CAMARA.acquire(blocking=False)
    try:
        assert correr(capturar_jpeg("camara", settings=settings_cam)).motivo == "camara_ocupada"
    finally:
        cap._CERROJO_CAMARA.release()
    assert correr(capturar_jpeg("camara", settings=settings_cam)).ok


def test_camara_que_no_responde_no_bloquea_el_loop(settings_cam: Settings, correr,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    lenta = CapturaFalsa(espera_s=0.05)
    monkeypatch.setattr(cap, "_abrir_camara", lambda indice: lenta)
    monkeypatch.setattr(cap, "TIMEOUT_CAMARA_S", 0.05)
    monkeypatch.setattr(cap, "CALENTAMIENTO_MIN_S", 5.0)

    async def escenario() -> tuple[ResultadoCaptura, int]:
        latidos = 0

        async def latir() -> None:
            nonlocal latidos
            while True:
                await asyncio.sleep(0.005)
                latidos += 1

        tarea = asyncio.create_task(latir())
        resultado = await capturar_jpeg("camara", settings=settings_cam)
        tarea.cancel()
        return resultado, latidos

    resultado, latidos = correr(escenario())
    assert resultado.motivo == "error" and latidos >= 3
    fin = time.monotonic() + 3.0
    while cap._CERROJO_CAMARA.locked() and time.monotonic() < fin:   # el hilo atiende la cancelación
        time.sleep(0.01)
    assert not cap._CERROJO_CAMARA.locked() and lenta.liberada


class BusFalso:
    """Lo que ``capturas`` usa del bus: ``publicar(nombre, datos)`` (desde cualquier hilo)."""

    def __init__(self, registro: list[tuple[str, Any]] | None = None) -> None:
        self.registro = registro if registro is not None else []

    def publicar(self, nombre: str, datos: Any = None) -> None:
        self.registro.append((nombre, dict(datos or {})))


def test_camara_publica_el_indicador_mientras_esta_abierta(settings_cam: Settings, correr,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """§C2.13: con la cámara abierta por una foto, vision.camara{activa} enciende LED, consola y overlay."""
    registro: list[tuple[str, Any]] = []

    class Registrada(CapturaFalsa):
        def release(self) -> None:
            registro.append(("release", None))
            super().release()

    def abrir(indice: int) -> CapturaFalsa:
        registro.append(("abrir", None))
        return Registrada()

    monkeypatch.setattr(cap, "_abrir_camara", abrir)
    resultado = correr(capturar_jpeg("camara", settings=settings_cam, bus=BusFalso(registro)))
    assert resultado.ok
    nombres = [nombre for nombre, _ in registro]
    assert nombres == ["vision.camara", "abrir", "release", "vision.camara"]   # encendido antes, apagado después
    encendido, apagado = registro[0][1], registro[-1][1]
    assert encendido == {"activa": True, "habilitada": True, "duracion_ms": 0, "fuente": "vistazo"}
    assert apagado["activa"] is False and apagado["fuente"] == "vistazo" and apagado["duracion_ms"] >= 0
    # Sin cámara (no abre) también se apaga; desde la visión continua no se publica nada (ya lo hace ella).
    registro.clear()
    monkeypatch.setattr(cap, "_abrir_camara", lambda indice: None)
    assert correr(capturar_jpeg("camara", settings=settings_cam, bus=BusFalso(registro))).motivo == "sin_camara"
    assert [d["activa"] for _, d in registro] == [True, False]
    registro.clear()
    assert correr(capturar_jpeg("camara", settings=settings_cam, vision=VisionFalsa(_jpeg()),
                                bus=BusFalso(registro))).ok
    assert registro == []


def test_camara_tras_el_plazo_el_indicador_se_apaga_al_liberar(settings_cam: Settings, correr,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    lenta = CapturaFalsa(espera_s=0.05)
    registro: list[tuple[str, Any]] = []
    monkeypatch.setattr(cap, "_abrir_camara", lambda indice: lenta)
    monkeypatch.setattr(cap, "TIMEOUT_CAMARA_S", 0.05)
    monkeypatch.setattr(cap, "CALENTAMIENTO_MIN_S", 5.0)
    assert correr(capturar_jpeg("camara", settings=settings_cam, bus=BusFalso(registro))).motivo == "error"
    fin = time.monotonic() + 3.0
    while cap._CERROJO_CAMARA.locked() and time.monotonic() < fin:
        time.sleep(0.01)
    assert lenta.liberada and [d["activa"] for _, d in registro] == [True, False]


def test_camara_pedido_abandonado_no_abre_el_dispositivo(settings_cam: Settings, correr,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Si el plazo vence mientras el trabajo espera turno en el executor, la cámara ya no se enciende."""
    aperturas: list[float] = []
    registro: list[tuple[str, Any]] = []

    def abrir(indice: int) -> CapturaFalsa:
        aperturas.append(time.monotonic())
        return CapturaFalsa()

    monkeypatch.setattr(cap, "_abrir_camara", abrir)
    # 1) Directo: con la cancelación ya puesta no abre nada, no publica nada y suelta el cerrojo.
    assert cap._CERROJO_CAMARA.acquire(blocking=False)
    cancelar = threading.Event()
    cancelar.set()
    resultado = cap._foto_camara_sync(0, 85, 1600, cancelar, cap._indicador_camara(BusFalso(registro)))
    assert (resultado.ok, resultado.motivo) == (False, "error")
    assert aperturas == [] and registro == [] and not cap._CERROJO_CAMARA.locked()

    # 2) Executor saturado: el pedido vence en la cola y, al llegar su turno, ya no abre la cámara.
    monkeypatch.setattr(cap, "TIMEOUT_CAMARA_S", 0.1)

    async def escenario() -> ResultadoCaptura:
        loop = asyncio.get_running_loop()
        ejecutor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(ejecutor)
        ocupado = loop.run_in_executor(None, time.sleep, 0.4)
        resultado = await capturar_jpeg("camara", settings=settings_cam, bus=BusFalso(registro))
        await ocupado
        await asyncio.sleep(0.1)          # el trabajo de la cámara ya corrió (y salió sin abrir)
        return resultado

    assert correr(escenario()).motivo == "error"
    fin = time.monotonic() + 3.0
    while cap._CERROJO_CAMARA.locked() and time.monotonic() < fin:
        time.sleep(0.01)
    assert aperturas == [] and registro == [] and not cap._CERROJO_CAMARA.locked()


# ---------------------------------------------------------------------------
# Pantalla
# ---------------------------------------------------------------------------


class SctFalso:
    """``mss.MSS`` falso: devuelve una imagen del tamaño de la región pedida y la anota."""

    def __init__(self, al_capturar: Any = None) -> None:
        self.regiones: list[dict[str, int]] = []
        self.cerrado = False
        self.al_capturar = al_capturar
        self.primary_monitor = dict(MONITOR_1)

    def grab(self, region: dict[str, int]) -> Any:
        self.regiones.append(dict(region))
        if self.al_capturar is not None:
            self.al_capturar()
        return _imagen(region["height"], region["width"], 4)

    def close(self) -> None:
        self.cerrado = True


def _mss_falso(monkeypatch: pytest.MonkeyPatch, al_capturar: Any = None) -> list[SctFalso]:
    creados: list[SctFalso] = []

    def fabrica() -> SctFalso:
        creados.append(SctFalso(al_capturar))
        return creados[-1]

    monkeypatch.setattr(cap, "_mss", types.SimpleNamespace(MSS=fabrica))
    return creados


_DEFECTO: Any = object()


class WinFalso:
    """``_Win32`` falso: ventana en primer plano y ventanas visibles (las regiones pedidas se anotan)."""

    def __init__(self, activa: Any = _DEFECTO, visibles: Any = _DEFECTO) -> None:
        self.activa: Any = activa if activa is not _DEFECTO else {
            "app": "code", "titulo": "Visual Studio Code", "pantalla_completa": False, "monitor": dict(MONITOR_1)}
        self.visibles: Any = visibles if visibles is not _DEFECTO else [dict(VSCODE)]   # None = EnumWindows falló
        self.regiones: list[dict[str, int]] = []
        self.lecturas = 0

    def ventana_activa(self, *, con_titulo: bool) -> dict[str, Any]:
        assert con_titulo
        self.lecturas += 1
        if isinstance(self.activa, Exception):
            raise self.activa
        return dict(self.activa)

    def ventanas_visibles(self, region: dict[str, int]) -> list[dict[str, str]] | None:
        self.regiones.append(dict(region))
        return None if self.visibles is None else [dict(v) for v in self.visibles]


def _win_falso(monkeypatch: pytest.MonkeyPatch, win: WinFalso | None) -> WinFalso | None:
    monkeypatch.setattr(cap._vc, "_crear_win32", lambda: win)
    return win


def test_pantalla_windows_captura_el_monitor_de_la_ventana_activa(settings_prueba: Settings, correr,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Dos monitores y la ventana activa en el secundario: se comprueba y se captura ESE monitor."""
    creados = _mss_falso(monkeypatch)
    win = _win_falso(monkeypatch, WinFalso({"app": "code", "titulo": "Visual Studio Code",
                                            "pantalla_completa": False, "monitor": dict(MONITOR_2)}))
    resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba, max_lado=1600))
    assert resultado.ok and resultado.fuente == "pantalla" and resultado.motivo is None
    assert (resultado.ancho, resultado.alto) == (1600, 900)
    assert dimensiones_jpeg(resultado.jpeg) == (1600, 900)
    assert creados[0].regiones == [MONITOR_2]              # no el monitor principal
    assert win.regiones == [MONITOR_2, MONITOR_2]          # ventanas visibles de esa región, antes y después
    assert win.lecturas == 2 and creados[0].cerrado


def test_pantalla_rechaza_ventanas_sensibles(settings_prueba: Settings, correr,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    creados = _mss_falso(monkeypatch)
    win = _win_falso(monkeypatch, WinFalso())
    # 1) El título que pasa quien llama.
    resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba,
                                     ventana_titulo="Bancolombia - Sucursal Virtual"))
    assert (resultado.ok, resultado.motivo, resultado.jpeg) == (False, "ventana_sensible", None)

    # 2) La ventana que de verdad está en primer plano (aunque quien llama pase otra).
    win.activa = {"app": "bitwarden", "titulo": "Iniciar sesión - Bitwarden", "monitor": dict(MONITOR_1)}
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba, ventana_titulo="Notas")).motivo == \
        "ventana_sensible"

    # 3) Patrones extra del usuario (pantalla_excluir).
    win.activa = {"app": "obsidian", "titulo": "Mi Diario Íntimo - Obsidian", "monitor": dict(MONITOR_1)}
    win.visibles = [{"app": "obsidian", "titulo": "Mi Diario Íntimo - Obsidian", "clase": "Chrome_WidgetWin_1"}]
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).ok
    extra = settings_prueba.con(pantalla_excluir=("diario intimo",))
    assert correr(capturar_jpeg("pantalla", settings=extra)).motivo == "ventana_sensible"
    assert sum(len(c.regiones) for c in creados) == 1      # solo el caso permitido llegó a capturar


def test_pantalla_rechaza_ventana_sensible_visible_sin_foco(settings_prueba: Settings, correr,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Lado a lado: el foco en VS Code y el banco (o el gestor de claves) visible al lado."""
    creados = _mss_falso(monkeypatch)
    for visible in ({"app": "msedge", "titulo": "Bancolombia - Sucursal Virtual Personas", "clase": "x"},
                    {"app": "keepassxc", "titulo": "Base.kdbx", "clase": "Qt5QWindowIcon"},
                    {"app": "", "titulo": "", "clase": "KeePassXC"}):
        _win_falso(monkeypatch, WinFalso(visibles=[dict(VSCODE), visible]))
        resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba))
        assert (resultado.ok, resultado.motivo, resultado.jpeg) == (False, "ventana_sensible", None), visible
    assert creados == [] or all(c.regiones == [] for c in creados)


def test_pantalla_windows_falla_cerrada(settings_prueba: Settings, correr,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    creados = _mss_falso(monkeypatch)
    casos: list[tuple[WinFalso | None, str]] = [
        # GetForegroundWindow() == 0 (cambiando de ventana, escritorio seguro…): título y app vacíos.
        (WinFalso({"app": "", "titulo": "", "pantalla_completa": None, "monitor": None}), "ventana_sensible"),
        (WinFalso({"app": " ", "titulo": "  ", "monitor": dict(MONITOR_1)}), "ventana_sensible"),
        (None, "ventana_sensible"),                                      # sin Win32
        (WinFalso(visibles=None), "ventana_sensible"),                   # no se pudieron enumerar
        (WinFalso({"app": "code", "titulo": "Visual Studio Code", "monitor": None}), "error"),
        (WinFalso(OSError("Win32 roto")), "error"),                      # type: ignore[arg-type]
    ]
    for win, motivo in casos:
        _win_falso(monkeypatch, win)
        resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba))
        assert (resultado.ok, resultado.motivo, resultado.jpeg) == (False, motivo, None)
    assert all(c.regiones == [] for c in creados)          # en ningún caso se llegó a capturar
    assert all(c.cerrado for c in creados)


def test_pantalla_se_descarta_si_las_ventanas_cambian_durante_la_captura(
        settings_prueba: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    win = WinFalso()
    _win_falso(monkeypatch, win)
    cambios = [
        lambda: setattr(win, "activa", {"app": "bitwarden", "titulo": "Bitwarden", "monitor": dict(MONITOR_1)}),
        lambda: setattr(win, "activa", {"app": "", "titulo": "", "monitor": None}),
        lambda: setattr(win, "visibles", [dict(VSCODE), {"app": "chrome", "titulo": "Nequi - Pago PSE", "clase": ""}]),
        lambda: setattr(win, "visibles", None),
        lambda: setattr(win, "activa", {"app": "code", "titulo": "Visual Studio Code", "monitor": dict(MONITOR_2)}),
    ]
    for cambio in cambios:
        win.activa = {"app": "code", "titulo": "Visual Studio Code", "monitor": dict(MONITOR_1)}
        win.visibles = [dict(VSCODE)]
        creados = _mss_falso(monkeypatch, cambio)
        resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba))
        assert (resultado.ok, resultado.jpeg) == (False, None)
        assert resultado.motivo == "ventana_sensible"
        assert creados[0].regiones == [MONITOR_1] and creados[0].cerrado


def test_pantalla_sin_forma_de_comprobar_las_ventanas_no_captura(settings_prueba: Settings, correr,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """X11, otros Wayland y macOS: no se sabe qué ventanas se ven ⇒ sin_backend (como la visión continua)."""
    creados = _mss_falso(monkeypatch)
    monkeypatch.setattr(cap.shutil, "which", lambda nombre: "/usr/bin/grim")
    for plataforma in ("x11", "otro"):
        monkeypatch.setattr(cap._vc, "_plataforma_actual", lambda p=plataforma: p)
        assert cap._backend_pantalla() == ("", None)
        resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba, ventana_titulo="Notas"))
        assert (resultado.ok, resultado.motivo) == (False, "sin_backend")
    assert creados == []
    # Windows sin mss/OpenCV, y Hyprland sin grim, tampoco.
    monkeypatch.setattr(cap._vc, "_plataforma_actual", lambda: "win")
    assert cap._backend_pantalla() == ("mss", None)
    monkeypatch.setattr(cap, "_mss", None)
    assert cap._backend_pantalla() == ("", None)
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).motivo == "sin_backend"
    monkeypatch.setattr(cap._vc, "_plataforma_actual", lambda: "hypr")
    assert cap._backend_pantalla() == ("grim", "/usr/bin/grim")
    monkeypatch.setattr(cap.shutil, "which", lambda nombre: None)
    assert cap._backend_pantalla() == ("", None)


# --- Hyprland -----------------------------------------------------------------


class HyprFalso:
    """Socket de Hyprland falso (``j/activewindow``, ``j/monitors``, ``j/clients``) y grim falso."""

    def __init__(self) -> None:
        self.activa: Any = {"class": "code", "title": "Visual Studio Code"}
        self.monitores: Any = [
            {"id": 0, "name": "HDMI-A-1", "focused": False, "activeWorkspace": {"id": 1},
             "specialWorkspace": {"id": 0}},
            {"id": 1, "name": "DP-1", "focused": True, "activeWorkspace": {"id": 2},
             "specialWorkspace": {"id": -98}},
        ]
        self.clientes: Any = [
            {"class": "code", "title": "Visual Studio Code", "mapped": True, "hidden": False,
             "workspace": {"id": 2}},
        ]
        self.comandos: list[str] = []
        self.llamadas: list[list[str]] = []
        self.al_capturar: Any = None
        self.salida = _jpeg(3840, 1080)

    async def consulta(self, comando: str, timeout: float = 0.25) -> Any:
        self.comandos.append(comando)
        return {"j/activewindow": self.activa, "j/monitors": self.monitores,
                "j/clients": self.clientes}[comando]

    async def ejecutar(self, argv: list[str], *, timeout: float, procesos: set[Any]) -> bytes | None:
        self.llamadas.append(list(argv))
        if self.al_capturar is not None:
            self.al_capturar()
        return self.salida


def _hypr_falso(monkeypatch: pytest.MonkeyPatch) -> HyprFalso:
    hypr = HyprFalso()
    monkeypatch.setattr(cap._vc, "_plataforma_actual", lambda: "hypr")
    monkeypatch.setattr(cap.shutil, "which", lambda nombre: "/usr/bin/grim" if nombre == "grim" else None)
    monkeypatch.setattr(cap._vc, "_consulta_hypr_socket", hypr.consulta)
    monkeypatch.setattr(cap._vc, "_ejecutar", hypr.ejecutar)
    return hypr


def test_pantalla_hyprland_grim_del_monitor_enfocado_sin_shell(settings_prueba: Settings, correr,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    hypr = _hypr_falso(monkeypatch)
    resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba, calidad=80, max_lado=1600))
    assert resultado.ok and (resultado.ancho, resultado.alto) == (1600, 450)
    assert hypr.llamadas == [["/usr/bin/grim", "-o", "DP-1", "-t", "jpeg", "-q", "80", "-"]]   # stdout: nunca disco
    assert hypr.comandos == ["j/activewindow", "j/monitors", "j/clients"] * 2      # antes y después
    # Sin monitor enfocado válido, grim junta todas las salidas: se miran los espacios de todos.
    hypr.monitores[1]["focused"] = False
    hypr.llamadas.clear()
    hypr.clientes.append({"class": "firefox", "title": "Bancolombia", "mapped": True, "workspace": {"id": 1}})
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).motivo == "ventana_sensible"
    hypr.clientes.pop()
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).ok
    assert hypr.llamadas[-1][:2] == ["/usr/bin/grim", "-t"]


def test_pantalla_hyprland_mira_los_clientes_visibles(settings_prueba: Settings, correr,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    hypr = _hypr_falso(monkeypatch)
    banco = {"class": "firefox", "title": "Bancolombia - Sucursal Virtual", "mapped": True, "hidden": False}
    # Visible en el espacio activo del monitor enfocado, en su espacio especial abierto o fijada: se rechaza.
    for espacio, extra in (({"id": 2}, {}), ({"id": -98}, {}), ({"id": 7}, {"pinned": True}), (None, {})):
        hypr.clientes = [{"class": "code", "title": "VS Code", "workspace": {"id": 2}},
                         {**banco, "workspace": espacio, **extra}]
        assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).motivo == "ventana_sensible", espacio
    # En otro espacio de trabajo (o en el del otro monitor), oculta o sin mapear: no se ve.
    for cliente in ({**banco, "workspace": {"id": 5}}, {**banco, "workspace": {"id": 1}},
                    {**banco, "workspace": {"id": 2}, "hidden": True},
                    {**banco, "workspace": {"id": 2}, "mapped": False}):
        hypr.clientes = [{"class": "code", "title": "VS Code", "workspace": {"id": 2}}, cliente]
        assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).ok, cliente
    assert all(llamada[1:3] == ["-o", "DP-1"] for llamada in hypr.llamadas)


def test_pantalla_hyprland_falla_cerrada(settings_prueba: Settings, correr,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    for atributo, valor in (("activa", None), ("activa", {"class": "", "title": ""}),
                            ("activa", {"class": "keepassxc", "title": "Base.kdbx"}),
                            ("monitores", None), ("monitores", []), ("clientes", None)):
        estado = _hypr_falso(monkeypatch)
        setattr(estado, atributo, valor)
        resultado = correr(capturar_jpeg("pantalla", settings=settings_prueba))
        assert (resultado.ok, resultado.motivo) == (False, "ventana_sensible"), (atributo, valor)
        assert estado.llamadas == []                       # grim ni se lanza
    # Cambio durante la captura: la imagen se descarta.
    hypr = _hypr_falso(monkeypatch)
    hypr.al_capturar = lambda: setattr(hypr, "activa", {"class": "firefox", "title": "Nequi - Pago PSE"})
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).motivo == "ventana_sensible"
    hypr = _hypr_falso(monkeypatch)
    hypr.al_capturar = lambda: hypr.monitores[1].update(name="DP-2")   # cambió el monitor enfocado
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).motivo == "ventana_sensible"
    hypr = _hypr_falso(monkeypatch)
    hypr.salida = None
    assert correr(capturar_jpeg("pantalla", settings=settings_prueba)).motivo == "error"


def test_fuente_desconocida(settings_prueba: Settings, correr) -> None:
    resultado = correr(capturar_jpeg("microondas", settings=settings_prueba))
    assert (resultado.ok, resultado.motivo) == (False, "error")
