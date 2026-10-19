"""Capturas bajo pedido de R.A.M.S. BIO-CORE: una foto JPEG de la cámara o de la pantalla.

Sirve para que Amadeus te mande por Telegram lo que ve **cuando tú se lo pides** ("mándame una
foto", "mándame captura"). Todo ocurre en memoria: la imagen nunca se escribe en disco, nunca se
registra en el log y al bus no sale ningún byte (quien llama decide a dónde va el JPEG).

- **Cámara**: si la visión continua (``VisionContinua``) tiene la cámara abierta, se usa su
  último cuadro (``instantanea_jpeg``) y no se toca el dispositivo. Si no, se abre la cámara un
  instante en el executor (como el parpadeo de aura: ``CAP_DSHOW``/``CAP_MSMF`` en Windows,
  ``CAP_V4L2`` en Linux), se descartan los primeros cuadros de exposición, se lee uno y se libera
  **siempre** (``release()`` en un ``finally``: el LED se apaga). Exige ``settings.camara_habilitada``.
  Mientras el dispositivo está abierto se publica ``vision.camara{activa}`` (``fuente:"vistazo"``)
  en el ``bus`` que pase quien llama: LED del HUD, ``● CÁMARA ACTIVA`` y ojo del overlay
  (§C2.13). Un pedido abandonado (plazo vencido o cancelado) antes de que el hilo empiece ya no
  abre la cámara. Una app de llamada en primer plano, o el hilo de la visión continua abriendo o
  soltando la cámara, la dejan ``camara_ocupada``: nadie compite por el dispositivo.
- **Pantalla**: se captura el monitor de la ventana en primer plano, y solo si **todo lo que se
  ve en él** es publicable. Se rechaza con ``ventana_sensible`` si el título que pasa quien
  llama, la ventana en primer plano o **cualquier ventana visible en ese monitor** (aunque no
  tenga el foco) casa con :func:`core.config.titulo_sensible` o con ``settings.pantalla_excluir_re``
  (bancos, contraseñas, gestores de claves…). La privacidad falla cerrada: sin ventana en primer
  plano (título y app vacíos), si la consulta falla o si no se pueden enumerar las ventanas,
  tampoco hay captura; y tras capturar se vuelve a mirar (si algo cambió a sensible o a
  desconocido, la imagen se descarta). Windows: ``mss`` del monitor de la ventana activa,
  ventanas por ``EnumWindows``; Hyprland: ``grim -o <monitor enfocado> -t jpeg -`` a stdout (sin
  shell, sin disco), ventanas por ``j/clients`` de los espacios de trabajo visibles. En X11, otros
  Wayland y macOS no hay forma fiable de saber qué ventanas se ven: ``sin_backend`` (como la
  visión continua, que ahí deja la pantalla en OFF).
- El JPEG se codifica con ``cv2.imencode`` en el executor, reducido para que el lado mayor no
  pase de ``max_lado``.

Nada bloqueante corre en el event loop: la cámara, la captura, las consultas de Win32 y la
codificación van al executor por defecto; ``grim`` es un subproceso asíncrono.

Motivos de fallo (``ResultadoCaptura.motivo``): ``deshabilitada`` (cámara apagada en la
configuración), ``sin_camara`` (no abre ninguna cámara), ``camara_ocupada`` (la usa una llamada,
la visión continua u otra app), ``ventana_sensible``, ``sin_backend`` (falta OpenCV, numpy, mss o
grim) y ``error``. ``capturar_jpeg`` nunca lanza (salvo la cancelación).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from core.config import Settings, titulo_sensible
from lobes import vision_continua as _vc

log = logging.getLogger("rams.capturas")

try:  # opcional: cámara y codificación JPEG
    import cv2
except ImportError:  # sin OpenCV: sin cámara ni JPEG propios
    cv2 = None
except Exception as _exc_cv2:  # OpenCV roto (p. ej. ABI de numpy incompatible): se trata como ausente
    log.debug("OpenCV no se pudo importar (%s); capturas sin backend", type(_exc_cv2).__name__)
    cv2 = None

try:  # opcional: matrices de imagen
    import numpy as np
except ImportError:
    np = None

try:  # opcional: captura de pantalla en Windows, X11 y macOS
    import mss as _mss
except ImportError:
    _mss = None
except Exception as _exc_mss:
    log.debug("mss no se pudo importar (%s); sin captura de pantalla con mss", type(_exc_mss).__name__)
    _mss = None

__all__ = [
    "ResultadoCaptura",
    "capturar_jpeg",
    "codificar_jpeg",
    "dimensiones_jpeg",
    "FUENTES",
    "MOTIVOS",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

FUENTES: Final = ("camara", "pantalla")
MOTIVOS: Final = frozenset({"deshabilitada", "sin_camara", "camara_ocupada", "ventana_sensible",
                            "sin_backend", "error"})
_SINONIMOS_FUENTE: Final = {
    "camara": "camara", "cámara": "camara", "camera": "camara", "webcam": "camara",
    "pantalla": "pantalla", "screen": "pantalla", "captura": "pantalla", "screenshot": "pantalla",
}

CALIDAD_MIN: Final = 30
CALIDAD_MAX: Final = 95
LADO_MIN: Final = 64
LADO_MAX: Final = 4096

# Cámara (un instante, como el parpadeo de aura). Variables de módulo para que los tests las reduzcan.
CUADROS_EXPOSICION: int = 5            # cuadros que se descartan mientras ajusta la exposición (3–5)
CALENTAMIENTO_MIN_S: float = 0.4       # en DSHOW el primer cuadro sale oscuro: al menos este tiempo
CUADROS_CALENTAMIENTO_MAX: Final = 30  # tope de grabs del calentamiento
TIMEOUT_CAMARA_S: float = 8.0
ANCHO_CAMARA: Final = 1280             # se pide 1280×720 (MJPG); el driver da lo que puede
ALTO_CAMARA: Final = 720
_FOURCC_MJPG: Final = sum(ord(c) << (8 * i) for i, c in enumerate("MJPG"))
# ``vision.camara`` de una apertura breve (§C2.3: "vistazo" = la cámara se abre un instante;
# "continua" es solo de la visión continua). El LED, la consola y el overlay son el OR de fuentes.
FUENTE_INDICADOR: Final = "vistazo"

# Pantalla.
TIMEOUT_PANTALLA_S: float = 6.0       # Windows: comprobación + captura + comprobación + JPEG
TIMEOUT_GRIM_S: float = 5.0
TIMEOUT_CODIFICAR_S: float = 6.0

# Una sola apertura de cámara a la vez (dos pedidos seguidos no compiten por el dispositivo).
_CERROJO_CAMARA: Final = threading.Lock()


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultadoCaptura:
    """Resultado de :func:`capturar_jpeg`. ``jpeg`` solo vive en memoria (y no sale en el ``repr``)."""

    ok: bool
    jpeg: bytes | None = field(repr=False)
    ancho: int
    alto: int
    fuente: str
    motivo: str | None = None


def _fallo(fuente: str, motivo: str) -> ResultadoCaptura:
    return ResultadoCaptura(False, None, 0, 0, fuente, motivo if motivo in MOTIVOS else "error")


# ---------------------------------------------------------------------------
# JPEG (bloqueante: siempre en el executor)
# ---------------------------------------------------------------------------


def codificar_jpeg(imagen: Any, *, calidad: int = 85, max_lado: int = 1600) -> tuple[bytes, int, int] | None:
    """Matriz BGR, BGRA o gris (``uint8``) → ``(jpeg, ancho, alto)`` con ``cv2.imencode``, o ``None``.

    **Bloqueante**: córrela en el executor. Reduce con ``INTER_AREA`` para que el lado mayor no
    pase de ``max_lado``. Solo memoria: nunca escribe en disco.
    """
    if cv2 is None or np is None or imagen is None:
        return None
    calidad = int(min(max(int(calidad), CALIDAD_MIN), CALIDAD_MAX))
    max_lado = int(min(max(int(max_lado), LADO_MIN), LADO_MAX))
    img = np.asarray(imagen)
    if img.dtype != np.uint8:
        return None
    if img.ndim == 3 and img.shape[2] >= 4:
        img = img[..., :3]
    elif img.ndim == 3 and img.shape[2] == 1:
        img = img[..., 0]
    if img.ndim not in (2, 3) or (img.ndim == 3 and img.shape[2] != 3):
        return None
    alto, ancho = int(img.shape[0]), int(img.shape[1])
    if alto <= 0 or ancho <= 0:
        return None
    lado = max(alto, ancho)
    if lado > max_lado:
        escala = max_lado / float(lado)
        ancho, alto = max(1, int(round(ancho * escala))), max(1, int(round(alto * escala)))
        img = cv2.resize(np.ascontiguousarray(img), (ancho, alto), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", np.ascontiguousarray(img),
                              [int(getattr(cv2, "IMWRITE_JPEG_QUALITY", 1)), calidad])
    if not ok or buffer is None:
        return None
    datos = bytes(buffer.tobytes())
    return (datos, ancho, alto) if datos else None


def dimensiones_jpeg(datos: bytes) -> tuple[int, int] | None:
    """``(ancho, alto)`` leído del marcador SOF de un JPEG, sin decodificarlo; ``None`` si no se halla."""
    if not isinstance(datos, (bytes, bytearray)) or not datos.startswith(b"\xff\xd8"):
        return None
    i, n = 2, len(datos)
    while i + 4 <= n:
        if datos[i] != 0xFF:
            return None
        marcador = datos[i + 1]
        if marcador == 0xFF:                      # relleno
            i += 1
            continue
        if marcador in (0x01, 0xD8) or 0xD0 <= marcador <= 0xD7:
            i += 2
            continue
        largo = int.from_bytes(datos[i + 2:i + 4], "big")
        if largo < 2:
            return None
        if 0xC0 <= marcador <= 0xCF and marcador not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            alto = int.from_bytes(datos[i + 5:i + 7], "big")
            ancho = int.from_bytes(datos[i + 7:i + 9], "big")
            return (ancho, alto) if ancho > 0 and alto > 0 else None
        if marcador == 0xDA:                      # empieza el barrido: ya no hay SOF
            return None
        i += 2 + largo
    return None


def _reducir_jpeg(jpeg: bytes, calidad: int, max_lado: int) -> tuple[bytes, int, int] | None:
    """JPEG ajeno (grim) → reducido a ``max_lado`` y recodificado; sin OpenCV, tal cual. Bloqueante."""
    if cv2 is None or np is None:
        dims = dimensiones_jpeg(jpeg)
        return (bytes(jpeg), dims[0], dims[1]) if dims else (bytes(jpeg), 0, 0)
    matriz = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), int(getattr(cv2, "IMREAD_COLOR", 1)))
    if matriz is None:
        return None
    try:
        return codificar_jpeg(matriz, calidad=calidad, max_lado=max_lado)
    finally:
        del matriz


# ---------------------------------------------------------------------------
# Cámara
# ---------------------------------------------------------------------------


def _backends_camara() -> tuple[int, ...]:
    if cv2 is None:
        return ()
    if sys.platform.startswith("linux"):
        return (getattr(cv2, "CAP_V4L2", 200),)
    if sys.platform == "win32":
        return (getattr(cv2, "CAP_DSHOW", 700), getattr(cv2, "CAP_MSMF", 1400))
    return (getattr(cv2, "CAP_ANY", 0),)


def _abrir_camara(indice: int) -> Any:
    """``cv2.VideoCapture`` abierta con el primer backend que funcione, o ``None``. Bloqueante."""
    for backend in _backends_camara():
        if backend == getattr(cv2, "CAP_MSMF", 1400):
            os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
        captura = None
        try:
            captura = cv2.VideoCapture(int(indice), backend)
            if captura is not None and captura.isOpened():
                return captura
        except Exception as exc:
            log.debug("VideoCapture con el backend %s falló (%s)", backend, type(exc).__name__)
        if captura is not None:
            with contextlib.suppress(Exception):
                captura.release()
    return None


Indicador = Callable[[bool, int], None]


def _indicador_camara(bus: Any) -> Indicador | None:
    """``aviso(activa, duracion_ms)`` que publica ``vision.camara`` en ``bus`` (o ``None`` sin bus).

    ``BusEventos.publicar`` es seguro desde cualquier hilo: el aviso se da desde el hilo que abre
    y libera el dispositivo, así el indicador refleja la cámara real (también tras un plazo vencido).
    """
    publicar = getattr(bus, "publicar", None) if bus is not None else None
    if not callable(publicar):
        return None

    def aviso(activa: bool, duracion_ms: int) -> None:
        try:
            publicar("vision.camara", {"activa": bool(activa), "habilitada": True,
                                       "duracion_ms": max(0, int(duracion_ms)), "fuente": FUENTE_INDICADOR})
        except Exception as exc:  # el indicador nunca tumba la foto
            log.debug("No se pudo publicar vision.camara (%s)", type(exc).__name__)

    return aviso


def _foto_camara_sync(indice: int, calidad: int, max_lado: int, cancelar: threading.Event,
                      aviso: Indicador | None = None) -> ResultadoCaptura:
    """Abre la cámara, descarta los cuadros de exposición, lee uno y la libera. Corre en el executor.

    Suelta ``_CERROJO_CAMARA`` al terminar (lo tomó quien la programó). Si el pedido ya se
    abandonó mientras esperaba turno en el executor (``cancelar``), no abre nada. ``aviso``
    publica el indicador de cámara: encendido justo antes de abrir y apagado tras ``release()``.
    El cuadro es una variable local que se descarta tras codificarlo: nunca se escribe en disco
    ni sale de aquí.
    """
    captura = None
    cuadro = None
    abierta_desde: float | None = None
    try:
        if cancelar.is_set():                     # a quien pidió ya se le respondió: no se enciende nada
            return _fallo("camara", "error")
        if aviso is not None:
            aviso(True, 0)
        abierta_desde = time.monotonic()
        captura = _abrir_camara(indice)
        if captura is None:
            return _fallo("camara", "sin_camara")
        for nombre, valor in (("CAP_PROP_FOURCC", _FOURCC_MJPG), ("CAP_PROP_FRAME_WIDTH", ANCHO_CAMARA),
                              ("CAP_PROP_FRAME_HEIGHT", ALTO_CAMARA), ("CAP_PROP_BUFFERSIZE", 1)):
            propiedad = getattr(cv2, nombre, None)
            if propiedad is not None:
                with contextlib.suppress(Exception):
                    captura.set(propiedad, valor)
        inicio = time.monotonic()
        descartados = 0
        while descartados < CUADROS_CALENTAMIENTO_MAX and (
                descartados < CUADROS_EXPOSICION or time.monotonic() - inicio < CALENTAMIENTO_MIN_S):
            if cancelar.is_set():
                return _fallo("camara", "error")
            captura.grab()
            descartados += 1
        if cancelar.is_set():
            return _fallo("camara", "error")
        ok, cuadro = captura.read()
        if not ok or cuadro is None:
            return _fallo("camara", "camara_ocupada")    # abre pero no entrega cuadros: la usa otra app
    except Exception as exc:
        log.warning("Foto de cámara fallida (%s)", type(exc).__name__)
        return _fallo("camara", "error")
    finally:
        if captura is not None:
            try:
                captura.release()                 # SIEMPRE: apaga el LED de la cámara
            except Exception as exc:
                log.debug("release() de la cámara falló (%s)", type(exc).__name__)
        if aviso is not None and abierta_desde is not None:
            aviso(False, int((time.monotonic() - abierta_desde) * 1000))
        with contextlib.suppress(RuntimeError):
            _CERROJO_CAMARA.release()
    try:
        codificado = codificar_jpeg(cuadro, calidad=calidad, max_lado=max_lado)
    finally:
        del cuadro                                # se descarta: nunca sale de esta función
    if codificado is None:
        return _fallo("camara", "error")
    jpeg, ancho, alto = codificado
    return ResultadoCaptura(True, jpeg, ancho, alto, "camara", None)


def _vision_ocupa_la_camara(vision: Any) -> bool:
    """``True`` si la visión continua (o una llamada) tiene la cámara: no se debe abrir aquí.

    ``como_vistazo("usuario")`` devuelve ``None`` solo cuando no hay hilo de cámara ni veto; un
    resultado (con o sin error) significa que el dispositivo es suyo o de la app de llamada.
    """
    como = getattr(vision, "como_vistazo", None)
    if not callable(como):
        return False
    try:
        return como("usuario") is not None
    except Exception as exc:
        log.debug("como_vistazo falló (%s); se asume la cámara ocupada", type(exc).__name__)
        return True


async def _capturar_camara(settings: Settings, vision: Any, calidad: int, max_lado: int,
                           bus: Any = None) -> ResultadoCaptura:
    if not bool(getattr(settings, "camara_habilitada", False)):
        return _fallo("camara", "deshabilitada")
    if vision is not None:
        instantanea = getattr(vision, "instantanea_jpeg", None)
        if callable(instantanea):
            try:
                jpeg = await instantanea(calidad=calidad, max_lado=max_lado)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("instantanea_jpeg falló (%s)", type(exc).__name__)
                jpeg = None
            if isinstance(jpeg, (bytes, bytearray)) and jpeg:
                dims = dimensiones_jpeg(jpeg) or (0, 0)
                log.info("Foto de cámara desde la visión continua (%d KB)", len(jpeg) // 1024)
                return ResultadoCaptura(True, bytes(jpeg), dims[0], dims[1], "camara", None)
        if _vision_ocupa_la_camara(vision):
            log.info("Foto de cámara: el dispositivo lo tiene la visión continua o una llamada")
            return _fallo("camara", "camara_ocupada")
    if cv2 is None or np is None:
        return _fallo("camara", "sin_backend")
    if not _CERROJO_CAMARA.acquire(blocking=False):
        return _fallo("camara", "camara_ocupada")
    cancelar = threading.Event()
    loop = asyncio.get_running_loop()
    indice = int(getattr(settings, "camara_indice", 0) or 0)
    try:
        futuro = loop.run_in_executor(None, _foto_camara_sync, indice, calidad, max_lado, cancelar,
                                      _indicador_camara(bus))
    except Exception as exc:  # executor cerrado: el cerrojo no llegó al hilo
        _CERROJO_CAMARA.release()
        log.warning("No se pudo programar la foto de cámara (%s)", type(exc).__name__)
        return _fallo("camara", "error")
    try:
        resultado = await asyncio.wait_for(asyncio.shield(futuro), TIMEOUT_CAMARA_S)
    except asyncio.TimeoutError:
        cancelar.set()
        log.warning("La cámara no respondió en %.0f s; se liberará cuando el hilo termine", TIMEOUT_CAMARA_S)
        return _fallo("camara", "error")
    except asyncio.CancelledError:
        cancelar.set()
        raise
    if resultado.ok:
        log.info("Foto de cámara tomada (%d×%d, %d KB)", resultado.ancho, resultado.alto,
                 len(resultado.jpeg or b"") // 1024)
    else:
        log.info("Foto de cámara fallida (%s)", resultado.motivo)
    return resultado


# ---------------------------------------------------------------------------
# Pantalla
# ---------------------------------------------------------------------------


def _es_sensible(settings: Settings, *textos: Any) -> bool:
    """Algún título, app o clase que casa con ``titulo_sensible`` o con ``settings.pantalla_excluir_re``."""
    for texto in textos:
        if texto and titulo_sensible(texto):
            return True
    return any(_vc.titulo_privado(settings, texto or "") for texto in textos if texto)


def _region(valor: Any) -> dict[str, int] | None:
    """``{left, top, width, height}`` enteros con área, o ``None``."""
    if not isinstance(valor, Mapping):
        return None
    try:
        region = {k: int(valor[k]) for k in ("left", "top", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    return region if region["width"] > 0 and region["height"] > 0 else None


# --- Windows (mss + Win32, todo en el executor) ------------------------------


def _revisar_windows(win: Any, settings: Settings) -> tuple[str | None, dict[str, int] | None]:
    """``(motivo, monitor)``. ``motivo is None`` ⇒ se puede capturar ``monitor`` (el de la ventana activa).

    Falla cerrada (``ventana_sensible``): sin ventana en primer plano (título y app vacíos), con
    una ventana sensible en primer plano o **visible en ese monitor** aunque no tenga el foco, o
    si las ventanas no se pueden enumerar. Sin el monitor de la ventana activa: ``error``.
    Bloqueante (ctypes).
    """
    info = win.ventana_activa(con_titulo=True)
    if not isinstance(info, Mapping):
        return "ventana_sensible", None
    titulo, app = str(info.get("titulo") or ""), str(info.get("app") or "")
    if not (titulo.strip() or app.strip()) or _es_sensible(settings, titulo, app):
        return "ventana_sensible", None
    monitor = _region(info.get("monitor"))
    if monitor is None:
        return "error", None
    visibles = win.ventanas_visibles(monitor)
    if not isinstance(visibles, list):
        return "ventana_sensible", None
    for ventana in visibles:
        if not isinstance(ventana, Mapping) or _es_sensible(
                settings, ventana.get("titulo"), ventana.get("app"), ventana.get("clase")):
            return "ventana_sensible", None
    return None, monitor


def _foto_pantalla_windows(settings: Settings, calidad: int, max_lado: int) -> ResultadoCaptura:
    """Monitor de la ventana activa con ``mss`` → JPEG, mirando las ventanas antes y después.

    Bloqueante (executor). Solo memoria. Como la visión continua (``_FuenteWindows``), se
    captura el monitor de la ventana en primer plano, no el principal: lo comprobado y lo
    capturado son la misma región.
    """
    fabrica = getattr(_mss, "MSS", None) or getattr(_mss, "mss")
    # mss va primero: activa el DPI por monitor del proceso, así los rectángulos de Win32 salen
    # en los mismos píxeles físicos que usa la captura.
    sct = fabrica()
    try:
        win = _vc._crear_win32()
        if win is None:                           # sin Win32 no se sabe qué hay en pantalla
            return _fallo("pantalla", "ventana_sensible")
        motivo, monitor = _revisar_windows(win, settings)
        if motivo is not None or monitor is None:
            return _fallo("pantalla", motivo or "error")
        captura = sct.grab(monitor)
        try:
            # Las ventanas pudieron cambiar durante la captura: si ahora hay algo sensible, no se
            # sabe qué hay o la ventana activa pasó a otro monitor, la imagen se descarta.
            motivo, despues = _revisar_windows(win, settings)
            if motivo is not None or despues != monitor:
                return _fallo("pantalla", motivo or "ventana_sensible")
            codificado = codificar_jpeg(_vc._mss_a_array(captura), calidad=calidad, max_lado=max_lado)
        finally:
            del captura
    finally:
        with contextlib.suppress(Exception):
            sct.close()
    if codificado is None:
        return _fallo("pantalla", "error")
    jpeg, ancho, alto = codificado
    return ResultadoCaptura(True, jpeg, ancho, alto, "pantalla", None)


# --- Hyprland (socket + grim, asíncrono) --------------------------------------


def _espacios_visibles(monitores: list[Any]) -> set[int]:
    """Ids de los espacios de trabajo que se ven en ``monitores`` (activo y especial abierto)."""
    espacios: set[int] = set()
    for monitor in monitores:
        if not isinstance(monitor, Mapping):
            continue
        for clave in ("activeWorkspace", "specialWorkspace"):
            espacio = monitor.get(clave)
            ident = espacio.get("id") if isinstance(espacio, Mapping) else None
            if isinstance(ident, int) and not isinstance(ident, bool) and ident != 0:
                espacios.add(ident)
    return espacios


async def _revisar_hypr(settings: Settings) -> tuple[str | None, str | None]:
    """``(motivo, salida)``. ``motivo is None`` ⇒ se puede capturar ``salida`` (``None`` = todas).

    Falla cerrada (``ventana_sensible``): si el socket no responde, no hay ventana activa con
    título o clase, o algún cliente visible (mapeado, no oculto, en un espacio de trabajo que
    se ve en lo capturado, o fijado) casa con los patrones sensibles.
    """
    meta = _vc._meta_hypr(await _vc._consulta_hypr_socket("j/activewindow"))
    titulo, clase = str(meta.get("titulo") or ""), str(meta.get("clase") or "")
    if not (titulo.strip() or clase.strip()) or _es_sensible(settings, titulo, clase):
        return "ventana_sensible", None
    monitores = await _vc._consulta_hypr_socket("j/monitors")
    if not isinstance(monitores, list) or not monitores:
        return "ventana_sensible", None
    enfocado = next((m for m in monitores if isinstance(m, Mapping) and m.get("focused") is True), None)
    nombre = str(enfocado.get("name") or "") if enfocado is not None else ""
    salida = nombre if _vc._RE_SALIDA_MONITOR.match(nombre) else None
    espacios = _espacios_visibles([enfocado] if salida else monitores)   # sin -o, grim junta todos
    clientes = await _vc._consulta_hypr_socket("j/clients")
    if not isinstance(clientes, list):
        return "ventana_sensible", None
    for cliente in clientes:
        if not isinstance(cliente, Mapping):
            continue
        if cliente.get("mapped") is False or cliente.get("hidden") is True:
            continue
        espacio = cliente.get("workspace")
        ident = espacio.get("id") if isinstance(espacio, Mapping) else None
        if isinstance(ident, int) and ident not in espacios and cliente.get("pinned") is not True:
            continue                              # en un espacio de trabajo que no se ve
        if _es_sensible(settings, cliente.get("title"), cliente.get("class")):
            return "ventana_sensible", None
    return None, salida


async def _foto_pantalla_grim(settings: Settings, ruta: str, calidad: int, max_lado: int) -> ResultadoCaptura:
    """``grim [-o salida] -t jpeg -q <calidad> -`` a stdout (subproceso asíncrono, sin shell ni disco)."""
    motivo, salida = await _revisar_hypr(settings)
    if motivo is not None:
        return _fallo("pantalla", motivo)
    argv = [ruta] + (["-o", salida] if salida else []) + ["-t", "jpeg", "-q", str(calidad), "-"]
    datos = await _vc._ejecutar(argv, timeout=TIMEOUT_GRIM_S, procesos=set())
    if not datos:
        log.info("grim no devolvió la captura")
        return _fallo("pantalla", "error")
    try:
        # Las ventanas pudieron cambiar durante la captura: se vuelve a mirar antes de usarla.
        motivo, despues = await _revisar_hypr(settings)
        if motivo is not None or despues != salida:
            return _fallo("pantalla", motivo or "ventana_sensible")
        loop = asyncio.get_running_loop()
        codificado = await asyncio.wait_for(
            loop.run_in_executor(None, _reducir_jpeg, datos, calidad, max_lado), TIMEOUT_CODIFICAR_S)
    finally:
        del datos
    if codificado is None:
        return _fallo("pantalla", "error")
    jpeg, ancho, alto = codificado
    return ResultadoCaptura(True, jpeg, ancho, alto, "pantalla", None)


def _backend_pantalla() -> tuple[str, str | None]:
    """``("mss", None)`` en Windows, ``("grim", ruta)`` en Hyprland o ``("", None)``.

    Solo donde se puede saber qué ventanas se ven (Win32, socket de Hyprland). En X11, otros
    Wayland y macOS no hay forma fiable de comprobar la privacidad: sin captura (falla cerrada,
    igual que la visión continua deja ahí la pantalla en OFF).
    """
    plataforma = _vc._plataforma_actual()
    if plataforma == "win":
        return ("mss", None) if _mss is not None and np is not None and cv2 is not None else ("", None)
    if plataforma == "hypr":
        ruta = shutil.which("grim")
        return ("grim", ruta) if ruta else ("", None)
    return "", None


async def _capturar_pantalla(settings: Settings, ventana_titulo: str | None, calidad: int,
                             max_lado: int) -> ResultadoCaptura:
    if ventana_titulo is not None and _es_sensible(settings, ventana_titulo):
        log.info("Captura de pantalla rechazada: ventana sensible")
        return _fallo("pantalla", "ventana_sensible")
    backend, ruta = _backend_pantalla()
    if backend == "mss":
        loop = asyncio.get_running_loop()
        resultado = await asyncio.wait_for(
            loop.run_in_executor(None, _foto_pantalla_windows, settings, calidad, max_lado), TIMEOUT_PANTALLA_S)
    elif backend == "grim" and ruta:
        resultado = await _foto_pantalla_grim(settings, ruta, calidad, max_lado)
    else:
        log.info("Captura de pantalla no disponible: no se puede comprobar qué ventanas se ven aquí")
        return _fallo("pantalla", "sin_backend")
    if resultado.ok:
        log.info("Captura de pantalla tomada (%d×%d, %d KB)", resultado.ancho, resultado.alto,
                 len(resultado.jpeg or b"") // 1024)
    elif resultado.motivo == "ventana_sensible":
        log.info("Captura de pantalla rechazada: hay una ventana sensible o no se pudo comprobar")
    return resultado


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


async def capturar_jpeg(fuente: str, *, settings: Settings, vision: Any = None,
                        ventana_titulo: str | None = None, calidad: int = 85,
                        max_lado: int = 1600, bus: Any = None) -> ResultadoCaptura:
    """Foto JPEG (en memoria) de la ``"camara"`` o de la ``"pantalla"``. Nunca lanza (salvo cancelación).

    ``vision`` es la ``VisionContinua`` (opcional): si tiene la cámara abierta se usa su último
    cuadro. ``ventana_titulo`` es el título que conoce quien llama; además se miran la ventana en
    primer plano y todas las visibles en el monitor que se captura. ``bus`` (``BusEventos``,
    opcional) recibe ``vision.camara{activa}`` mientras esta función tiene la cámara abierta
    (indicador obligatorio de §C2.13: pásalo siempre que exista). ``calidad`` (30–95) y
    ``max_lado`` (64–4096 px) se acotan.
    """
    nombre = _SINONIMOS_FUENTE.get(str(fuente or "").strip().lower(), "")
    try:
        calidad = int(min(max(int(calidad), CALIDAD_MIN), CALIDAD_MAX))
        max_lado = int(min(max(int(max_lado), LADO_MIN), LADO_MAX))
    except (TypeError, ValueError):
        calidad, max_lado = 85, 1600
    if not nombre:
        log.info("Fuente de captura desconocida")
        return _fallo(str(fuente or "")[:16], "error")
    try:
        if nombre == "camara":
            return await _capturar_camara(settings, vision, calidad, max_lado, bus)
        return await _capturar_pantalla(settings, ventana_titulo, calidad, max_lado)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        log.warning("La captura de %s tardó demasiado", nombre)
        return _fallo(nombre, "error")
    except Exception as exc:
        log.warning("Captura de %s fallida (%s)", nombre, type(exc).__name__)
        return _fallo(nombre, "error")
