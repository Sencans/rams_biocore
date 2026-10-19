"""Visión continua local de R.A.M.S. BIO-CORE v0.2 (docs/COMPANION.md §C1.9, §C2.9, §C2.13).

Amadeus "ve" en tiempo real sin gastar tokens: todo se calcula aquí, en local, y al bus solo
salen **escalares** (nunca cuadros, miniaturas ni bytes).

- **Cámara** (opt-in de dos niveles: ``RAMS_CAMERA=1`` **y** ``RAMS_VISION_CONTINUA=1``): un hilo
  daemon ``rams-vision-cam`` abre la webcam una vez, pide 10–15 fps al driver y analiza a
  ``vision_fps`` (1–5) con YuNet (``cv2.FaceDetectorYN``) o, de respaldo, Haar. De cada cuadro
  saca rostro, posición normalizada (u, v), distancia aproximada, orientación (yaw/pitch),
  movimiento y brillo; el cuadro no sale del hilo salvo una referencia al **último** (solo en
  memoria, sobrescrita en cada lectura y soltada al pausar o cerrar) que únicamente usa
  ``instantanea_jpeg``: la foto que el usuario pide ("mándame una foto"), codificada en el executor
  por ``lobes.capturas`` y nunca escrita en disco. Pausar = el hilo sale de su bucle y hace
  ``release()`` en su ``finally`` (el LED se apaga de verdad).
- **Pantalla** (``RAMS_PANTALLA_MODO``): ``metadatos`` (app, título corto, pantalla completa) o
  ``pixeles`` (además, una miniatura de 64×36 que vive solo en memoria para medir cambio, cortes,
  vídeo por región, brillo y tono). Windows: ctypes + ``mss`` en un hilo daemon; Hyprland: socket
  de Hyprland + ``grim -s 0.1 -t ppm -`` como subproceso asíncrono (sin tocar el disco); X11: OFF
  (sin metadatos de ventanas). Una ventana cuyo título casa con ``settings.pantalla_excluir_re``
  no se captura y se publica con ``privada:true`` y ``titulo:""``; sin metadatos válidos (consulta
  fallida, título y clase vacíos) tampoco hay píxeles: la privacidad falla cerrada. La caja del
  avatar (``escritorio.caja``) se enmascara para que Amadeus no se vea a sí misma.
- **Integrador** asyncio a 2 Hz: flancos con histéresis escalada al ritmo real (``llegada`` tras
  1 s de rostro estable y con refractario ≥ 120 s; ``salida`` tras 20 s sin rostro), estímulos
  fásicos con refractario (movimiento, acercamiento, luz), tónico de atención (EMA), mirada del
  avatar (con latido cada 2 s), autopausa (ausencia ≥ 0,8 que la cámara no contradiga, apps de
  llamada, batería < 20 % sin cargador, juego a pantalla completa, sesión bloqueada) y
  degradación en escalera (CPU, temperatura, coste por cuadro, batería).
- ``como_vistazo`` sirve los vistazos de aura desde la cámara continua y, mientras el hilo de
  cámara exista (abriendo o liberando), la pausa del usuario o la autopausa por llamada, bloqueo
  o juego, devuelve un resultado con ``error`` para que aura **no** abra el dispositivo.

Eventos (§C2.3): ``vision.rostro``, ``vision.mirada``, ``vision.estimulo``, ``vision.atencion``,
``vision.estado``, ``vision.camara`` (``fuente:"continua"``, repetido cada 5 s mientras la cámara
está abierta), ``pantalla.estado`` y ``pantalla.estimulo``. Con ``n ≥ 2`` caras solo se publica
el número de caras. La presencia **no** se escribe aquí (su dueño es main): ``presencia`` solo se
lee para la autopausa por ausencia. Este módulo no se suscribe a ningún evento.

Dependencias opcionales: ``cv2``, ``numpy``, ``mss`` y ``psutil`` (sin ellas la cámara o la
pantalla quedan OFF o en metadatos). Ningún método público lanza excepciones (la cancelación de
tareas sí se propaga, como debe ser).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("rams.vision.continua")

try:  # opcional: cámara (captura, YuNet/Haar) y operaciones de imagen rápidas
    import cv2
except ImportError:  # sin OpenCV: cámara OFF (salvo una fábrica de captura inyectada)
    cv2 = None
except Exception as _exc_cv2:  # OpenCV roto (p. ej. ABI de numpy incompatible): se trata como ausente
    log.debug("OpenCV no se pudo importar (%s); cámara continua OFF", type(_exc_cv2).__name__)
    cv2 = None

try:  # opcional: todo el análisis numérico (sin numpy no hay análisis)
    import numpy as np
except ImportError:
    np = None

try:  # opcional: píxeles de pantalla en Windows (y X11)
    import mss as _mss
except ImportError:
    _mss = None
except Exception as _exc_mss:
    log.debug("mss no se pudo importar (%s); pantalla solo con metadatos", type(_exc_mss).__name__)
    _mss = None

try:  # opcional: CPU, batería y temperatura para la degradación y la autopausa
    import psutil
except ImportError:
    psutil = None

from core.bus import BusEventos
from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo, ResultadoVistazo, formatear_decimal

try:  # CONTRATO (grupo base v0.2): forma a la que se aplica pantalla_excluir_re
    from core.config import normalizar_titulo
except ImportError:  # respaldo equivalente (minúsculas, sin tildes, espacios colapsados)
    import unicodedata

    def normalizar_titulo(titulo: Any) -> str:
        if titulo is None:
            return ""
        descompuesto = unicodedata.normalize("NFKD", str(titulo))
        return " ".join("".join(c for c in descompuesto if not unicodedata.combining(c)).lower().split())

try:  # CONTRATO (grupo base v0.2): respaldo si Settings no trae pantalla_excluir_re
    from core.config import titulo_sensible as _titulo_sensible
except ImportError:
    _titulo_sensible = None

try:  # CONTRATO (grupo base v0.2, §C2.2)
    from core.contracts import ESTADOS_VISION, MODOS_PANTALLA
except ImportError:
    ESTADOS_VISION = ("off", "continua", "pausada", "ocupada", "error", "degradada")
    MODOS_PANTALLA = ("off", "metadatos", "pixeles")

__all__ = [
    "MuestraCamara",
    "MuestraPantalla",
    "EstadoVision",
    "Integrador",
    "EscaleraDegradacion",
    "MetricasPantalla",
    "VisionContinua",
    "analizar",
    "punto_mirada",
    "mascara_caja",
    "titulo_privado",
    "ruta_modelo_yunet",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ORIGEN_BUS: Final = "vision_continua"
FUENTE_BUS: Final = "continua"

# Cámara.
TAM_ANALISIS: Final = (320, 240)              # (ancho, alto) de análisis (YuNet ≈ 2–3 ms)
TAM_ANALISIS_REDUCIDO: Final = (160, 120)     # nivel de degradación 2
TAM_MINI_CAMARA: Final = (80, 60)             # miniatura en gris para el movimiento (solo memoria)
ANCHO_CARA_M: Final = 0.15                    # ancho típico de la caja de una cara adulta
CALENTAMIENTO_S: Final = 0.4                  # autoexposición tras abrir (en DSHOW el primer cuadro sale oscuro)
UMBRAL_MOV_PIXEL: Final = 18                  # diferencia de gris que cuenta como píxel en movimiento
FPS_HAAR_MAX: Final = 2.0
FPS_CAPTURA_MIN: Final = 10                   # CAP_PROP_FPS que se pide al driver (10–15)
FPS_CAPTURA_MAX: Final = 15
UMBRAL_YAW_FRONTAL: Final = 0.25
UMBRAL_PITCH_FRONTAL: Final = 0.30
FALLOS_LECTURA_MAX: Final = 5
REINTENTOS_APERTURA_S: Final = (30.0, 60.0, 120.0, 300.0)   # cámara ocupada o ausente
REINTENTO_ERROR_S: Final = 60.0
REINTENTO_PANTALLA_S: Final = 30.0            # el hilo de pantalla terminó solo (sin fuente o roto)
FRESCURA_VISTAZO_S: Final = 1.0               # como_vistazo: muestra de menos de 1 s
FRESCURA_INSTANTANEA_S: Final = 2.0           # instantanea_jpeg: cuadro de menos de 2 s (o 2,5 periodos)
TIMEOUT_INSTANTANEA_S: Final = 5.0            # codificación del JPEG en el executor
JOIN_HILO_S: Final = 2.0
_FOURCC_MJPG: Final = sum(ord(c) << (8 * i) for i, c in enumerate("MJPG"))
_PREFIJO_YUNET: Final = "face_detection_yunet"
_XML_HAAR: Final = "haarcascade_frontalface_default.xml"
_DIRS_HAAR_SISTEMA: Final = (
    "/usr/share/opencv4/haarcascades", "/usr/local/share/opencv4/haarcascades",
    "/usr/share/opencv5/haarcascades", "/usr/local/share/opencv5/haarcascades",
)
# Valores de las propiedades de VideoCapture por si cv2 no las expone (fábricas falsas).
_PROPS_CAPTURA: Final = {"CAP_PROP_FRAME_WIDTH": 3, "CAP_PROP_FRAME_HEIGHT": 4, "CAP_PROP_FPS": 5,
                         "CAP_PROP_FOURCC": 6, "CAP_PROP_BUFFERSIZE": 38}

# Integrador (2 Hz).
PERIODO_INTEGRADOR_S: Final = 0.5
LLEGADA_ESTABLE_S: Final = 1.0                # rostro estable antes de la llegada
HUECO_ROSTRO_S: Final = 1.0                   # huecos más cortos no rompen la racha (histéresis)…
FACTOR_HUECO: Final = 2.5                     # …escalada al ritmo real: ≥ 2,5 periodos entre muestras
HUECO_MAX_S: Final = 5.0                      # (a 1 fps, 2,5 s; nunca más de 5 s)
ALFA_INTERVALO: Final = 0.3                   # EMA del intervalo real entre muestras
SALIDA_S: Final = 20.0                        # sin rostro durante 20 s ⇒ salida
REFRACTARIO_LLEGADA_S: Final = 120.0
ASENTAMIENTO_S: Final = 2.0                   # sin estímulos de luz/movimiento justo tras abrir
REFRACTARIOS_S: Final = {"movimiento": 8.0, "acercamiento": 30.0, "luz": 60.0}
UMBRAL_MOV_MIN: Final = 0.08
FACTOR_MOV: Final = 4.0
ALFA_MOV: Final = 0.1
MUESTRAS_MIN_MOV: Final = 5
VENTANA_LUZ_S: Final = 2.0
UMBRAL_LUZ: Final = 0.35
VENTANA_ACERCAMIENTO_S: Final = 2.0
UMBRAL_ACERCAMIENTO: Final = 0.25
TAU_ATENCION_S: Final = 5.0
INTERVALO_ATENCION_S: Final = 2.0
DELTA_ATENCION: Final = 0.1
INTERVALO_ROSTRO_S: Final = 0.5               # vision.rostro ≤ 2 Hz
INTERVALO_MIRADA_S: Final = 0.2               # vision.mirada ≤ 5 Hz
ZONA_MUERTA_MIRADA_M: Final = 0.04
LATIDO_MIRADA_S: Final = 2.0                  # con el usuario quieto, la mirada se repite cada 2 s
                                              # (la corteza la da por caducada a los 5 s)
ALFA_MIRADA: Final = 0.45
INTERVALO_LATIDO_CAMARA_S: Final = 5.0        # vision.camara{activa:true} mientras está abierta
CAM_VISOR: Final = (0.0, 1.3, 3.6)            # cámara por defecto del visor (m)
Z_MIRADA: Final = 3.0
LIMITE_X: Final = (-3.0, 3.0)
LIMITE_Y: Final = (0.3, 2.2)

# Pantalla.
TAM_MINI_PANTALLA: Final = (64, 36)
PANTALLA_FPS_MIN: Final = 0.2
PANTALLA_FPS_MAX: Final = 2.0
INTERVALO_PANTALLA_MIN_S: Final = 1.0         # pantalla.estado ≤ 1 Hz…
LATIDO_PANTALLA_S: Final = 5.0                # …y al menos cada 5 s (main la da por vieja a los 10 s)
INTERVALO_ESTIMULO_PANTALLA_S: Final = 5.0
TITULO_MAX: Final = 60
ANCHO_PRESUBMUESTREO: Final = 256             # la captura se submuestrea a ≤ 256 px de ancho
TIMEOUT_GRIM_S: Final = 3.0
TIMEOUT_HYPR_S: Final = 0.25
MAX_RESPUESTA_HYPR: Final = 1 << 20           # JSON del socket de Hyprland (j/clients puede pesar decenas de KB)
VENTANAS_ENUMERADAS_MAX: Final = 4096         # EnumWindows: más que esto ⇒ falla cerrada
ESPERA_SALIDA_CANCELADO_S: Final = 1.0
_RE_SALIDA_MONITOR: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_RE_CLASE: Final = re.compile(r"[^a-z0-9_.+\-]+")
_RE_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]+")

# Entorno, autopausa y degradación.
INTERVALO_ENTORNO_S: Final = 5.0
TIMEOUT_ENTORNO_S: Final = 3.0
CONFIANZA_AUSENCIA: Final = 0.8
BATERIA_MIN_PCT: Final = 20.0
LLAMADA_PEGAJOSA_S: Final = 300.0             # tras ver una app de llamada en primer plano
CPU_ALTA_PCT: Final = 85.0
CPU_SOSTENIDA_S: Final = 10.0
TEMP_ALTA_C: Final = 85.0
PRESUPUESTO_CAMARA_MS: Final = 15.0
PRESUPUESTO_PANTALLA_MS: Final = 40.0
SALIDA_NIVEL_S: Final = 60.0
NIVEL2_TRAS_S: Final = 120.0
ALFA_MS: Final = 0.2
_PROCESOS_BLOQUEO: Final = frozenset({"hyprlock", "swaylock", "gtklock", "waylock"})
_EXES_BLOQUEO: Final = frozenset({"lockapp", "logonui"})


# ---------------------------------------------------------------------------
# Tipos (solo escalares)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MuestraCamara:
    """Lo que queda de un cuadro de cámara tras analizarlo (solo escalares)."""

    ts: float
    rostro: bool
    n: int
    u: float | None
    v: float | None
    dist_m: float | None
    yaw_r: float | None
    pitch_r: float | None
    frontal: bool | None
    mov: float
    brillo: float
    score: float | None
    ms_proc: float


@dataclass(frozen=True)
class MuestraPantalla:
    """Lo que queda de una mirada a la pantalla (solo escalares; ``titulo`` ≤ 60, ``""`` si privada)."""

    ts: float
    cambio: float
    brillo: float
    tono: int | None
    corte: bool
    video: bool
    pantalla_completa: bool | None
    reproduciendo: bool | None
    clase: str
    titulo: str
    privada: bool
    ms_proc: float


@dataclass(frozen=True)
class EstadoVision:
    """Estado público de la visión continua (payload de ``vision.estado``)."""

    camara: str          # ∈ ESTADOS_VISION
    pantalla: str        # "off" | "metadatos" | "pixeles" | "pausada"
    fps_camara: float
    fps_pantalla: float
    nivel: int
    detector: str | None
    presente: bool | None
    atencion: float

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable con las 8 claves del contrato."""
        return {
            "camara": str(self.camara),
            "pantalla": str(self.pantalla),
            "fps_camara": float(self.fps_camara),
            "fps_pantalla": float(self.fps_pantalla),
            "nivel": int(self.nivel),
            "detector": self.detector,
            "presente": self.presente,
            "atencion": float(self.atencion),
        }


# ---------------------------------------------------------------------------
# Utilidades de imagen (numpy; cv2 si está, para ir más rápido)
# ---------------------------------------------------------------------------


def _acotar(valor: float, minimo: float, maximo: float) -> float:
    return minimo if valor < minimo else maximo if valor > maximo else valor


def _reducir(imagen: Any, tam: tuple[int, int]) -> Any:
    """Redimensiona a ``tam`` = (ancho, alto) con promedio por áreas (``INTER_AREA`` si hay cv2)."""
    ancho, alto = int(tam[0]), int(tam[1])
    img = np.asarray(imagen)
    h, w = int(img.shape[0]), int(img.shape[1])
    if (w, h) == (ancho, alto):
        return img
    if cv2 is not None:
        try:
            interp = cv2.INTER_AREA if (w >= ancho and h >= alto) else cv2.INTER_LINEAR
            return cv2.resize(np.ascontiguousarray(img), (ancho, alto), interpolation=interp)
        except Exception as exc:  # cv2 roto o tipo raro: respaldo en numpy
            log.debug("cv2.resize falló (%s); uso numpy", type(exc).__name__)
    if w < ancho or h < alto:   # ampliar (no debería pasar): vecino más cercano
        ys = np.minimum((np.arange(alto) * h) // alto, h - 1)
        xs = np.minimum((np.arange(ancho) * w) // ancho, w - 1)
        return img[ys][:, xs]
    datos = img.astype(np.float64)
    integral = np.zeros((h + 1, w + 1) + datos.shape[2:], np.float64)
    integral[1:, 1:] = datos.cumsum(0).cumsum(1)
    ys = (np.arange(alto + 1) * h) // alto
    xs = (np.arange(ancho + 1) * w) // ancho
    total = (integral[ys[1:]][:, xs[1:]] - integral[ys[:-1]][:, xs[1:]]
             - integral[ys[1:]][:, xs[:-1]] + integral[ys[:-1]][:, xs[:-1]])
    area = ((ys[1:] - ys[:-1])[:, None] * (xs[1:] - xs[:-1])[None, :]).astype(np.float64)
    if datos.ndim == 3:
        area = area[..., None]
    salida = total / area
    if img.dtype == np.uint8:
        return np.clip(salida + 0.5, 0, 255).astype(np.uint8)
    return salida.astype(img.dtype)


def _a_bgr(imagen: Any) -> Any:
    """BGR de 3 canales (desde gris, BGR o BGRA)."""
    img = np.asarray(imagen)
    if img.ndim == 2:
        return np.repeat(img[..., None], 3, axis=2)
    if img.ndim == 3 and img.shape[2] >= 4:
        return img[..., :3]
    return img


def _a_gris(imagen: Any) -> Any:
    """Gris ``uint8`` (BT.601) desde BGR o gris."""
    img = np.asarray(imagen)
    if img.ndim == 2:
        return img
    img = img[..., :3]
    if cv2 is not None and img.dtype == np.uint8:
        try:
            return cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_BGR2GRAY)
        except Exception as exc:
            log.debug("cv2.cvtColor falló (%s); uso numpy", type(exc).__name__)
    datos = img.astype(np.float32)
    gris = 0.114 * datos[..., 0] + 0.587 * datos[..., 1] + 0.299 * datos[..., 2]
    return np.clip(gris + 0.5, 0, 255).astype(np.uint8)


def _filas_caras(detector: Any, imagen_bgr: Any) -> list[list[float]]:
    """Filas ``[x, y, w, h, …]`` del detector (YuNet: 15 columnas; Haar: 4). Nunca lanza."""
    if detector is None:
        return []
    try:
        resultado = detector.detect(imagen_bgr)
    except Exception as exc:
        log.debug("El detector de rostros falló (%s)", type(exc).__name__)
        return []
    caras = resultado[1] if isinstance(resultado, tuple) and len(resultado) == 2 else resultado
    if caras is None:
        return []
    try:
        matriz = np.asarray(caras, dtype=np.float64)
    except (TypeError, ValueError):
        return []
    if matriz.size == 0:
        return []
    if matriz.ndim == 1:
        matriz = matriz.reshape(1, -1)
    if matriz.ndim != 2 or matriz.shape[1] < 4:
        return []
    return [[float(x) for x in fila] for fila in matriz if fila[2] > 0 and fila[3] > 0]


def analizar(cuadro: Any, previo: Any, detector: Any = None, *, hfov_deg: float = 70.0,
             tam: tuple[int, int] = TAM_ANALISIS, base_yaw: float = 0.0, base_pitch: float | None = None,
             ts: float | None = None) -> tuple[MuestraCamara, Any]:
    """Analiza un cuadro y devuelve ``(MuestraCamara, mini)``; el cuadro no sale de aquí.

    ``detector`` sigue la API de ``cv2.FaceDetectorYN.detect(img) -> (retval, caras)``: filas de
    15 columnas (caja, 5 puntos y puntuación) o de 4 (Haar, solo la caja). ``mini`` es la
    miniatura 80×60 en gris para el movimiento del cuadro siguiente (solo memoria).
    """
    inicio = time.perf_counter()
    if np is None:
        raise RuntimeError("numpy no instalado")
    ancho, alto = int(tam[0]), int(tam[1])
    peq = np.ascontiguousarray(_reducir(_a_bgr(cuadro), (ancho, alto)))
    gris = _a_gris(peq)
    brillo = float(gris.mean()) / 255.0
    mini = _reducir(gris, TAM_MINI_CAMARA)
    if cv2 is not None:
        with contextlib.suppress(Exception):
            mini = cv2.GaussianBlur(mini, (3, 3), 0)
    mov = 0.0
    if previo is not None and getattr(previo, "shape", None) == mini.shape:
        diferencia = np.abs(mini.astype(np.int16) - np.asarray(previo).astype(np.int16))
        mov = float(np.count_nonzero(diferencia > UMBRAL_MOV_PIXEL)) / float(diferencia.size)
    filas = _filas_caras(detector, peq)
    del peq, gris
    rostro = bool(filas)
    n = len(filas)
    u = v = dist = yaw = pitch = score = None
    frontal: bool | None = None
    if filas:
        fila = max(filas, key=lambda f: f[2])            # la cara más grande = la más cercana
        x, y, w, h = fila[0], fila[1], fila[2], fila[3]
        focal = (ancho / 2.0) / math.tan(math.radians(_acotar(float(hfov_deg), 10.0, 170.0)) / 2.0)
        u = _acotar((x + w / 2.0) / ancho, 0.0, 1.0)
        v = _acotar((y + h / 2.0) / alto, 0.0, 1.0)
        dist = focal * ANCHO_CARA_M / max(w, 1.0)
        if len(fila) >= 15:
            ojo_d = (fila[4], fila[5])
            ojo_i = (fila[6], fila[7])
            nariz = (fila[8], fila[9])
            ipd = math.hypot(ojo_d[0] - ojo_i[0], ojo_d[1] - ojo_i[1])
            if ipd > 1e-6:
                medio = ((ojo_d[0] + ojo_i[0]) / 2.0, (ojo_d[1] + ojo_i[1]) / 2.0)
                yaw = (nariz[0] - medio[0]) / ipd
                pitch = (nariz[1] - medio[1]) / ipd
            score = _acotar(fila[14], 0.0, 1.0)
        if yaw is not None:
            frontal = abs(yaw - base_yaw) < UMBRAL_YAW_FRONTAL and (
                base_pitch is None or pitch is None or abs(pitch - base_pitch) < UMBRAL_PITCH_FRONTAL)
        else:
            frontal = True        # Haar frontalface: si la detecta, está de frente
    muestra = MuestraCamara(
        ts=float(ts) if ts is not None else time.monotonic(),
        rostro=rostro, n=int(n),
        u=None if u is None else float(u), v=None if v is None else float(v),
        dist_m=None if dist is None else float(dist),
        yaw_r=None if yaw is None else float(yaw), pitch_r=None if pitch is None else float(pitch),
        frontal=None if frontal is None else bool(frontal),
        mov=float(mov), brillo=float(brillo), score=None if score is None else float(score),
        ms_proc=float((time.perf_counter() - inicio) * 1000.0),
    )
    return muestra, mini


def punto_mirada(u: float, v: float, dist_m: float | None, *, hfov_deg: float = 70.0, espejo: bool = False,
                 aspecto: float = 0.75, cam: tuple[float, float, float] = CAM_VISOR,
                 ganancia: float = 1.0) -> tuple[float, float, float]:
    """Punto del mundo del visor (m) al que el avatar debe mirar para mirar al usuario.

    Modelo estenopeico: +x del mundo = derecha de la pantalla. En un cuadro sin espejo, quien
    se mueve a **su** derecha aparece a la izquierda del cuadro (``u < 0,5``) ⇒ ``x > 0``;
    ``espejo=True`` invierte el eje. ``v > 0,5`` (más abajo en el cuadro) ⇒ ``y`` menor.
    Límites del saneador de la corteza: x ∈ [−3, 3], y ∈ [0,3; 2,2]; z = 3.
    """
    d = 0.6 if dist_m is None or not math.isfinite(dist_m) or dist_m <= 0 else float(dist_m)
    tan_h = math.tan(math.radians(_acotar(float(hfov_deg), 10.0, 170.0)) / 2.0)
    tx = (float(u) - 0.5) * 2.0 * tan_h
    ty = (float(v) - 0.5) * 2.0 * tan_h * float(aspecto)
    x = (tx if espejo else -tx) * d
    y = -ty * d
    return (
        round(_acotar(cam[0] + ganancia * x, *LIMITE_X), 4),
        round(_acotar(cam[1] + ganancia * y, *LIMITE_Y), 4),
        Z_MIRADA,
    )


def mascara_caja(caja: tuple[int, int, int, int] | None, geometria: tuple[float, float, float, float] | None,
                 tam: tuple[int, int] = TAM_MINI_PANTALLA) -> Any:
    """Máscara booleana (alto×ancho de ``tam``) con ``True`` en la caja del avatar, o ``None``.

    ``caja`` = (x, y, w, h) en píxeles físicos globales; ``geometria`` = (izquierda, arriba,
    ancho, alto) del monitor capturado en las mismas coordenadas.
    """
    if np is None or caja is None or geometria is None:
        return None
    try:
        x, y, w, h = (float(c) for c in caja)
        izq, arr, ancho_m, alto_m = (float(g) for g in geometria)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or ancho_m <= 0 or alto_m <= 0:
        return None
    ancho, alto = int(tam[0]), int(tam[1])
    x0 = int(math.floor((x - izq) / ancho_m * ancho))
    x1 = int(math.ceil((x + w - izq) / ancho_m * ancho))
    y0 = int(math.floor((y - arr) / alto_m * alto))
    y1 = int(math.ceil((y + h - arr) / alto_m * alto))
    x0, x1 = max(0, x0), min(ancho, x1)
    y0, y1 = max(0, y0), min(alto, y1)
    mascara = np.zeros((alto, ancho), dtype=bool)
    if x1 > x0 and y1 > y0:
        mascara[y0:y1, x0:x1] = True
    return mascara


def _correlacion(a: Any, b: Any) -> float:
    """Correlación de dos histogramas (como ``HISTCMP_CORREL``)."""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    da, db = a - a.mean(), b - b.mean()
    denominador = math.sqrt(float((da * da).sum()) * float((db * db).sum()))
    if denominador <= 1e-12:
        return 1.0 if np.allclose(a, b) else 0.0
    return float((da * db).sum()) / denominador


class MetricasPantalla:
    """Métricas escalares de una miniatura de pantalla 64×36 (BGR); nada sale de aquí salvo escalares.

    - ``brillo`` (0..1), ``tono`` dominante en 12 cubos de 30° (0 = rojo, 4 = verde, 8 = azul;
      ``None`` si la pantalla es casi gris), ``cambio`` (fracción de celdas que cambian),
      ``corte`` (histograma de grises poco correlado con el anterior) y ``video`` (una región
      > 4 % que cambia sin parar con el resto quieto).
    - ``mascara`` (``True`` = excluir, p. ej. la caja del avatar) se ignora en todas las métricas.
    """

    def __init__(self, tam: tuple[int, int] = TAM_MINI_PANTALLA) -> None:
        self.tam = (int(tam[0]), int(tam[1]))
        self._prev: Any = None
        self._hist_prev: Any = None
        self.mapa: Any = np.zeros((self.tam[1], self.tam[0]), np.float32) if np is not None else None

    def reiniciar(self) -> None:
        """Olvida el cuadro anterior (p. ej. tras una ventana privada)."""
        self._prev = None
        self._hist_prev = None
        if self.mapa is not None:
            self.mapa[:] = 0.0

    def actualizar(self, mini_bgr: Any, mascara: Any = None) -> dict[str, Any]:
        """Métricas del cuadro ``mini_bgr`` (se reduce a 64×36 si hace falta)."""
        neutro: dict[str, Any] = {"brillo": 0.0, "tono": None, "cambio": 0.0, "corte": False, "video": False}
        if np is None:
            return neutro
        ancho, alto = self.tam
        img = _a_bgr(mini_bgr)
        if img.shape[0] != alto or img.shape[1] != ancho:
            img = _reducir(img, (ancho, alto))
        valida = np.ones((alto, ancho), dtype=bool)
        if mascara is not None and getattr(mascara, "shape", None) == (alto, ancho):
            valida = ~np.asarray(mascara, dtype=bool)
        n_validas = int(valida.sum())
        if n_validas == 0:
            return neutro
        datos = img.astype(np.float32)
        b, g, r = datos[..., 0], datos[..., 1], datos[..., 2]
        gris = 0.114 * b + 0.587 * g + 0.299 * r
        brillo = float(gris[valida].mean()) / 255.0
        maximo = np.maximum(np.maximum(r, g), b)
        minimo = np.minimum(np.minimum(r, g), b)
        delta = maximo - minimo
        saturacion = np.where(maximo > 0, delta / np.maximum(maximo, 1e-6), 0.0)
        saturados = (saturacion > 60.0 / 255.0) & (maximo > 40.0) & valida
        tono: int | None = None
        if int(saturados.sum()) > 0.05 * n_validas:
            seguro = np.maximum(delta, 1e-6)
            matiz = np.where(maximo == r, np.mod((g - b) / seguro, 6.0),
                             np.where(maximo == g, (b - r) / seguro + 2.0, (r - g) / seguro + 4.0)) * 60.0
            cubos = (np.floor(matiz[saturados] / 30.0).astype(np.int64)) % 12
            tono = int(np.bincount(cubos, minlength=12).argmax())
        hist = np.bincount(np.clip(gris[valida] / 16.0, 0, 15).astype(np.int64), minlength=16).astype(np.float64)
        resultado = {"brillo": brillo, "tono": tono, "cambio": 0.0, "corte": False, "video": False}
        if self._prev is not None and self._prev.shape == gris.shape:
            dif = (np.abs(gris - self._prev) > 10.0) & valida
            resultado["cambio"] = float(dif.sum()) / float(n_validas)
            resultado["corte"] = bool(_correlacion(hist, self._hist_prev) < 0.5)
            self.mapa = (0.8 * self.mapa + 0.2 * dif.astype(np.float32)).astype(np.float32)
            self.mapa[~valida] = 0.0
            activa = (self.mapa > 0.5) & valida
            fraccion = float(activa.sum()) / float(n_validas)
            resto = valida & ~activa
            quieto = float(self.mapa[resto].mean()) if bool(resto.any()) else 1.0
            resultado["video"] = bool(fraccion > 0.04 and quieto < 0.05)
        self._prev = gris
        self._hist_prev = hist
        return resultado


def _parsear_ppm(datos: bytes) -> Any:
    """``P6`` (RGB de 8 bits) → vista BGR de numpy; ``None`` si no es válido. Solo memoria."""
    if np is None or not isinstance(datos, (bytes, bytearray)) or not datos.startswith(b"P6"):
        return None
    pos = 2
    fichas: list[bytes] = []
    limite = min(len(datos), 512)
    while len(fichas) < 3 and pos < limite:
        caracter = datos[pos:pos + 1]
        if caracter.isspace():
            pos += 1
            continue
        if caracter == b"#":
            fin = datos.find(b"\n", pos)
            pos = limite if fin < 0 else fin + 1
            continue
        inicio = pos
        while pos < limite and not datos[pos:pos + 1].isspace():
            pos += 1
        fichas.append(bytes(datos[inicio:pos]))
    if len(fichas) < 3:
        return None
    try:
        ancho, alto, maximo = (int(f) for f in fichas)
    except ValueError:
        return None
    if not (0 < ancho <= 8192 and 0 < alto <= 8192) or maximo != 255:
        return None
    pos += 1                                   # un único blanco tras el valor máximo
    necesario = ancho * alto * 3
    if len(datos) - pos < necesario:
        return None
    rgb = np.frombuffer(datos, dtype=np.uint8, count=necesario, offset=pos).reshape(alto, ancho, 3)
    return rgb[..., ::-1]


def _sanear_clase(texto: Any) -> str:
    """Clase o ejecutable en minúsculas, sin ``.exe`` y con caracteres seguros (≤ 64)."""
    valor = str(texto or "").strip().lower()
    if valor.endswith(".exe"):
        valor = valor[:-4]
    return _RE_CLASE.sub("", valor)[:64]


def _recortar_titulo(titulo: Any) -> str:
    """Título sin controles, con espacios colapsados y ≤ 60 caracteres."""
    texto = " ".join(_RE_CONTROL.sub(" ", str(titulo or "")).split())
    return texto[:TITULO_MAX]


def titulo_privado(settings: Settings, titulo: Any, clase: Any = "") -> bool:
    """``True`` si el título (o la clase) casa con ``settings.pantalla_excluir_re``. Falla cerrado."""
    try:
        patron = getattr(settings, "pantalla_excluir_re", None)
        for texto in (titulo, clase):
            normal = normalizar_titulo(texto)
            if not normal:
                continue
            if patron is not None:
                if patron.search(normal):
                    return True
            elif _titulo_sensible is not None and _titulo_sensible(normal):
                return True
        return False
    except Exception as exc:  # ante la duda, privada
        log.debug("titulo_privado: error (%s); se trata como privada", type(exc).__name__)
        return True


def _coincide_app(app: str, apps: Any) -> bool:
    """``app`` (proceso o clase) está en la lista ``apps`` (nombre exacto o una de sus partes)."""
    nombre = _sanear_clase(app)
    if not nombre:
        return False
    partes = {p for p in re.split(r"[.\-_+ ]+", nombre) if p}
    try:
        lista = [_sanear_clase(a) for a in (apps or ())]
    except TypeError:
        return False
    return any(a and (a == nombre or a in partes) for a in lista)


def _fmt_fps(valor: float) -> str:
    return str(int(valor)) if float(valor).is_integer() else formatear_decimal(valor, 1)


def _plataforma_actual() -> str:
    """``"win"``, ``"hypr"`` (Hyprland), ``"x11"`` u ``"otro"``."""
    if sys.platform == "win32":
        return "win"
    if sys.platform.startswith("linux"):
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip():
            return "hypr"
        if os.environ.get("DISPLAY", "").strip() and not os.environ.get("WAYLAND_DISPLAY", "").strip():
            return "x11"
    return "otro"


# ---------------------------------------------------------------------------
# Integrador (sin E/S: flancos, refractarios, tónicos y mirada)
# ---------------------------------------------------------------------------

Evento = tuple[str, dict[str, Any]]


class Integrador:
    """Convierte muestras de cámara en eventos del bus con histéresis y refractarios.

    No hace E/S ni conoce el reloj: ``muestra(m)`` procesa una muestra (``m.ts`` en el reloj del
    módulo) y devuelve los eventos fásicos y la mirada; ``tick(ahora)`` (2 Hz) evalúa los flancos
    de llegada/salida y publica ``vision.rostro`` y ``vision.atencion``. ``presente`` es el estado
    del flanco (``None`` si la cámara no está abierta, no hay detector o aún no se sabe).

    La histéresis de rostro (``hueco_s``) se escala al ritmo real: ``periodo_s`` es el periodo
    objetivo (lo fija ``VisionContinua`` con ``1 / vision_fps``) y además se mide el intervalo
    real entre muestras; a 1 fps un cuadro por segundo no rompe la racha ni hace parpadear
    ``vision.rostro.presente``.
    """

    def __init__(self, *, hfov_deg: float = 70.0, espejo: bool = False, periodo_s: float = 1.0 / 3.0) -> None:
        self.hfov_deg = float(hfov_deg)
        self.espejo = bool(espejo)
        self.periodo_s = float(periodo_s)
        self.detector_disponible = True
        self.flanco: str | None = None          # None | "presente" | "ausente"
        self.atencion = 0.0
        self.base_yaw = 0.0
        self.base_pitch: float | None = None
        self._pitch_acum: float | None = None
        self._n_calibracion = 0
        self._ultima_llegada: float | None = None
        self._activo_ts: float | None = None    # última muestra o tick con la cámara abierta
        self._abierta = False
        self._reiniciar_sesion(None)

    def _reiniciar_sesion(self, ahora: float | None) -> None:
        self._apertura = ahora
        self._racha_desde: float | None = None
        self._ultimo_rostro: float | None = None
        self._ts_muestra_prev: float | None = None
        self._intervalo: float | None = None
        self._ultima: MuestraCamara | None = None
        self._ultima_con_rostro: MuestraCamara | None = None
        self._ema_mov: float | None = None
        self._n_mov = 0
        self._brillos: deque[tuple[float, float]] = deque(maxlen=64)
        self._distancias: deque[tuple[float, float]] = deque(maxlen=64)
        self._refractario: dict[str, float] = {}
        self._mirada: tuple[float, float, float] | None = None
        self._mirada_pub: tuple[float, float, float] | None = None
        self._mirada_pub_ts: float | None = None
        self._rostro_pub: tuple[Any, ...] | None = None
        self._rostro_pub_ts: float | None = None
        self._atencion_pub: float | None = None
        self._atencion_pub_ts: float | None = None
        self._tick_ts: float | None = None

    # -- estado ---------------------------------------------------------------

    @property
    def abierta(self) -> bool:
        return self._abierta

    @property
    def presente(self) -> bool | None:
        if not self._abierta or not self.detector_disponible or self.flanco is None:
            return None
        return self.flanco == "presente"

    @property
    def ultima(self) -> MuestraCamara | None:
        return self._ultima

    @property
    def hueco_s(self) -> float:
        """Hueco máximo entre caras que no rompe la racha: ≥ 1 s y ≥ 2,5 periodos reales (≤ 5 s)."""
        periodo = max(float(self.periodo_s), self._intervalo or 0.0)
        return min(HUECO_MAX_S, max(HUECO_ROSTRO_S, FACTOR_HUECO * periodo))

    def vio_rostro(self, ahora: float, ventana_s: float) -> bool:
        """``True`` si en esta sesión hubo una cara hace ``ventana_s`` segundos o menos."""
        return self._abierta and self._ultimo_rostro is not None and ahora - self._ultimo_rostro <= ventana_s

    def abrir(self, ahora: float) -> None:
        """La cámara se acaba de abrir: nueva sesión.

        El refractario de llegada se conserva siempre; el flanco solo si la cámara estuvo
        cerrada menos de ``SALIDA_S`` (un reintento o un cambio de nivel). Tras una pausa más
        larga el flanco viejo ya no dice nada: vuelve a ``None`` hasta ver (o no ver) una cara.
        """
        if self._activo_ts is None or ahora - self._activo_ts > SALIDA_S:
            self.flanco = None
        self._abierta = True
        self._reiniciar_sesion(ahora)
        self._activo_ts = ahora

    def cerrar(self) -> list[Evento]:
        """La cámara se cerró: no hay muestras nuevas ni flancos hasta reabrir.

        La atención vuelve a 0 y, si lo último publicado decía otra cosa, devuelve
        ``vision.rostro{presente:false, n:0}`` y ``vision.atencion{nivel:0}`` para que nadie
        siga usando un estado de antes de la pausa.
        """
        eventos: list[Evento] = []
        if self._rostro_pub is not None and self._rostro_pub[0]:
            eventos.append(("vision.rostro", {"presente": False, "n": 0, "dist_m": None, "frontal": None,
                                              "atencion": 0.0}))
        if self._atencion_pub is not None and self._atencion_pub > 0.0:
            eventos.append(("vision.atencion", {"nivel": 0.0}))
        self._abierta = False
        self.atencion = 0.0
        self._ultima = None
        self._ultima_con_rostro = None
        self._racha_desde = None
        self._mirada = None
        self._rostro_pub = (False, 0, None, None, 0.0)
        self._atencion_pub = 0.0
        return eventos

    def calibrar(self, ahora: float) -> bool:
        """Línea base de "mira la pantalla" con la última muestra (el usuario está tecleando)."""
        m = self._ultima_con_rostro
        if m is None or m.n != 1 or m.yaw_r is None or ahora - m.ts > 2.0:
            return False
        if self._n_calibracion == 0:
            self.base_yaw = m.yaw_r
        else:
            self.base_yaw += 0.3 * (m.yaw_r - self.base_yaw)
        self.base_yaw = _acotar(self.base_yaw, -0.4, 0.4)
        if m.pitch_r is not None:
            self._pitch_acum = m.pitch_r if self._pitch_acum is None else (
                self._pitch_acum + 0.3 * (m.pitch_r - self._pitch_acum))
        self._n_calibracion += 1
        if self._n_calibracion >= 3 and self._pitch_acum is not None:
            self.base_pitch = self._pitch_acum
        return True

    # -- utilidades -----------------------------------------------------------

    def _libre(self, tipo: str, ahora: float) -> bool:
        ultimo = self._refractario.get(tipo)
        if ultimo is not None and ahora - ultimo < REFRACTARIOS_S.get(tipo, 0.0):
            return False
        self._refractario[tipo] = ahora
        return True

    @staticmethod
    def _estimulo(tipo: str, magnitud: float, firma: str) -> Evento:
        return ("vision.estimulo", {"fuente": "camara", "tipo": tipo,
                                    "magnitud": round(_acotar(float(magnitud), 0.0, 1.0), 3), "firma": firma})

    # -- muestras -------------------------------------------------------------

    def muestra(self, m: MuestraCamara) -> list[Evento]:
        """Procesa una muestra: racha de rostro, estímulos fásicos y mirada."""
        eventos: list[Evento] = []
        if not self._abierta:
            return eventos
        ahora = float(m.ts)
        self._ultima = m
        self._activo_ts = ahora
        # Intervalo real entre muestras (EMA, acotado): la histéresis se escala con él.
        if self._ts_muestra_prev is not None:
            dt = min(max(0.0, ahora - self._ts_muestra_prev), HUECO_MAX_S / FACTOR_HUECO)
            self._intervalo = dt if self._intervalo is None else (
                (1.0 - ALFA_INTERVALO) * self._intervalo + ALFA_INTERVALO * dt)
        self._ts_muestra_prev = ahora
        if m.rostro and m.n > 0:
            if (self._racha_desde is None or self._ultimo_rostro is None
                    or ahora - self._ultimo_rostro > self.hueco_s):
                self._racha_desde = ahora
            self._ultimo_rostro = ahora
            self._ultima_con_rostro = m
        apertura = self._apertura if self._apertura is not None else ahora
        asentada = ahora - apertura >= ASENTAMIENTO_S

        # Movimiento brusco: muy por encima de su media reciente.
        mov = max(0.0, float(m.mov))
        if (asentada and self._ema_mov is not None and self._n_mov >= MUESTRAS_MIN_MOV
                and mov > max(UMBRAL_MOV_MIN, FACTOR_MOV * self._ema_mov) and self._libre("movimiento", ahora)):
            eventos.append(self._estimulo("movimiento", mov * 2.5, "camara:movimiento"))
        self._ema_mov = mov if self._ema_mov is None else (1.0 - ALFA_MOV) * self._ema_mov + ALFA_MOV * mov
        self._n_mov += 1

        # Luz: el brillo cambia > 35 % en menos de 2 s.
        while self._brillos and ahora - self._brillos[0][0] > VENTANA_LUZ_S:
            self._brillos.popleft()
        if asentada:
            if self._brillos:
                referencia = max(self._brillos, key=lambda tb: abs(m.brillo - tb[1]) / max(tb[1], 0.05))
                relativo = (m.brillo - referencia[1]) / max(referencia[1], 0.05)
                if abs(relativo) > UMBRAL_LUZ and self._libre("luz", ahora):
                    eventos.append(self._estimulo("luz", abs(relativo),
                                                  "camara:luz:" + ("sube" if relativo > 0 else "baja")))
            self._brillos.append((ahora, float(m.brillo)))

        # Acercamiento: la distancia cae > 25 % en menos de 2 s (solo con una cara).
        while self._distancias and ahora - self._distancias[0][0] > VENTANA_ACERCAMIENTO_S:
            self._distancias.popleft()
        if m.rostro and m.n == 1 and m.dist_m is not None and m.dist_m > 0:
            if self._distancias:
                maxima = max(d for _, d in self._distancias)
                caida = (maxima - m.dist_m) / maxima if maxima > 0 else 0.0
                if caida > UMBRAL_ACERCAMIENTO and self._libre("acercamiento", ahora):
                    eventos.append(self._estimulo("acercamiento", caida * 2.0, "camara:acercamiento"))
            self._distancias.append((ahora, float(m.dist_m)))

        # Mirada del avatar (≤ 5 Hz, zona muerta de 4 cm y latido cada 2 s con el usuario quieto,
        # para que la corteza no la dé por caducada). Con terceros (n ≥ 2), nada.
        if m.rostro and m.n == 1 and m.u is not None and m.v is not None:
            punto = punto_mirada(m.u, m.v, m.dist_m, hfov_deg=self.hfov_deg, espejo=self.espejo)
            if self._mirada is None:
                filtrado = punto
            else:
                filtrado = tuple(a + ALFA_MIRADA * (b - a) for a, b in zip(self._mirada, punto))  # type: ignore[assignment]
            self._mirada = filtrado  # type: ignore[assignment]
            confianza = m.score if m.score is not None else 0.7
            if m.frontal is False:
                confianza *= 0.85
            listo = self._mirada_pub_ts is None or ahora - self._mirada_pub_ts >= INTERVALO_MIRADA_S - 1e-9
            latido = self._mirada_pub_ts is not None and ahora - self._mirada_pub_ts >= LATIDO_MIRADA_S - 1e-9
            if listo and (self._mirada_pub is None or latido
                          or math.dist(self._mirada_pub, filtrado) > ZONA_MUERTA_MIRADA_M):
                self._mirada_pub = filtrado  # type: ignore[assignment]
                self._mirada_pub_ts = ahora
                eventos.append(("vision.mirada", {
                    "x": round(_acotar(filtrado[0], *LIMITE_X), 3),
                    "y": round(_acotar(filtrado[1], *LIMITE_Y), 3),
                    "z": round(_acotar(filtrado[2], -3.0, 3.0), 3),
                    "confianza": round(_acotar(confianza, 0.0, 1.0), 3),
                }))
        return eventos

    # -- tick (2 Hz) ----------------------------------------------------------

    def _atencion_instantanea(self, reciente: bool) -> float:
        m = self._ultima_con_rostro if reciente else None
        if m is None or m.n != 1:      # con terceros (n ≥ 2) no se mide la atención de nadie
            return 0.0
        factor = 1.0 if m.frontal is True else 0.3 if m.frontal is False else 0.6
        proximidad = 0.8 if m.dist_m is None else _acotar(1.2 - 0.5 * m.dist_m, 0.3, 1.0)
        return factor * proximidad

    def tick(self, ahora: float) -> list[Evento]:
        """Flancos de llegada/salida, tónico de atención, ``vision.rostro`` y ``vision.atencion``."""
        eventos: list[Evento] = []
        dt = PERIODO_INTEGRADOR_S if self._tick_ts is None else max(0.0, ahora - self._tick_ts)
        self._tick_ts = ahora
        if not self._abierta:
            return eventos
        self._activo_ts = ahora
        if not self.detector_disponible:
            return eventos
        reciente = self._ultimo_rostro is not None and ahora - self._ultimo_rostro <= self.hueco_s

        # Llegada: rostro estable ≥ 1 s; el evento tiene refractario de 120 s (dentro de él el
        # flanco cambia en silencio, para que el estado no mienta).
        if (self.flanco != "presente" and reciente and self._racha_desde is not None
                and self._ultimo_rostro is not None
                and self._ultimo_rostro - self._racha_desde >= LLEGADA_ESTABLE_S - 1e-6):
            self.flanco = "presente"
            if self._ultima_llegada is None or ahora - self._ultima_llegada >= REFRACTARIO_LLEGADA_S:
                self._ultima_llegada = ahora
                n = self._ultima_con_rostro.n if self._ultima_con_rostro is not None else 1
                eventos.append(self._estimulo("llegada", 1.0, f"camara:llegada:{min(max(n, 1), 9)}"))

        # Salida: 20 s sin rostro con la cámara abierta. Solo es flanco si venía de "presente".
        referencia = max(self._ultimo_rostro if self._ultimo_rostro is not None else -math.inf,
                         self._apertura if self._apertura is not None else -math.inf)
        if self.flanco != "ausente" and not reciente and ahora - referencia >= SALIDA_S:
            anterior = self.flanco
            self.flanco = "ausente"
            if anterior == "presente":
                eventos.append(self._estimulo("salida", 1.0, "camara:salida"))

        # Tónico de atención: EMA (τ = 5 s) de "de frente × cerca".
        objetivo = self._atencion_instantanea(reciente)
        alfa = 1.0 - math.exp(-dt / TAU_ATENCION_S) if dt > 0 else 0.0
        self.atencion = _acotar(self.atencion + alfa * (objetivo - self.atencion), 0.0, 1.0)

        # vision.rostro (≤ 2 Hz y solo si cambia). Con terceros solo el número de caras.
        m = self._ultima_con_rostro if reciente else None
        n = m.n if m is not None else 0
        if m is not None and n == 1:
            dist = None if m.dist_m is None else round(m.dist_m, 2)
            frontal = m.frontal
        else:
            dist = None
            frontal = None
        atencion = round(self.atencion, 3)
        clave = (n > 0, n, None if dist is None else round(dist, 1), frontal, round(self.atencion, 1))
        if clave != self._rostro_pub and (
                self._rostro_pub_ts is None or ahora - self._rostro_pub_ts >= INTERVALO_ROSTRO_S - 1e-6):
            self._rostro_pub = clave
            self._rostro_pub_ts = ahora
            eventos.append(("vision.rostro", {"presente": n > 0, "n": int(n), "dist_m": dist,
                                              "frontal": frontal, "atencion": atencion}))

        # vision.atencion (cada 2 s o si cambia > 0,1).
        if (self._atencion_pub is None or self._atencion_pub_ts is None
                or ahora - self._atencion_pub_ts >= INTERVALO_ATENCION_S - 1e-6
                or abs(atencion - self._atencion_pub) > DELTA_ATENCION):
            self._atencion_pub = atencion
            self._atencion_pub_ts = ahora
            eventos.append(("vision.atencion", {"nivel": atencion}))
        return eventos


# ---------------------------------------------------------------------------
# Degradación en escalera
# ---------------------------------------------------------------------------


class EscaleraDegradacion:
    """Niveles 0–2 con histéresis (el 3 = cámara liberada lo pone la autopausa).

    - Sube a 1 con presión: CPU > 85 % sostenida 10 s, temperatura > 85 °C, coste medio por
      cuadro > 15 ms (cámara) o > 40 ms (pantalla), o portátil sin cargador.
    - Sube a 2 si sigue en 1 con presión más de 2 min.
    - Baja un nivel por cada 60 s seguidos sin presión.
    """

    def __init__(self) -> None:
        self.nivel = 0
        self._cpu_alta_desde: float | None = None
        self._sin_presion_desde: float | None = None
        self._nivel1_desde: float | None = None

    def evaluar(self, ahora: float, *, cpu_pct: float | None = None, temp_c: float | None = None,
                ms_camara: float | None = None, ms_pantalla: float | None = None,
                cargando: bool | None = None) -> int:
        if cpu_pct is not None and cpu_pct > CPU_ALTA_PCT:
            if self._cpu_alta_desde is None:
                self._cpu_alta_desde = ahora
        else:
            self._cpu_alta_desde = None
        presion = (
            (self._cpu_alta_desde is not None and ahora - self._cpu_alta_desde >= CPU_SOSTENIDA_S)
            or (temp_c is not None and temp_c > TEMP_ALTA_C)
            or (ms_camara is not None and ms_camara > PRESUPUESTO_CAMARA_MS)
            or (ms_pantalla is not None and ms_pantalla > PRESUPUESTO_PANTALLA_MS)
            or cargando is False
        )
        if presion:
            self._sin_presion_desde = None
            if self.nivel == 0:
                self.nivel = 1
                self._nivel1_desde = ahora
            elif self.nivel == 1 and self._nivel1_desde is not None and ahora - self._nivel1_desde >= NIVEL2_TRAS_S:
                self.nivel = 2
        elif self.nivel > 0:
            if self._sin_presion_desde is None:
                self._sin_presion_desde = ahora
            elif ahora - self._sin_presion_desde >= SALIDA_NIVEL_S:
                self.nivel -= 1
                self._sin_presion_desde = ahora
                if self.nivel == 1:
                    self._nivel1_desde = ahora
        return self.nivel


# ---------------------------------------------------------------------------
# Detectores de rostros (se crean y usan solo en el hilo de cámara)
# ---------------------------------------------------------------------------


class _DetectorYuNet:
    """``cv2.FaceDetectorYN`` que ajusta su tamaño de entrada al de la imagen."""

    tipo = "yunet"

    def __init__(self, detector: Any, tam: tuple[int, int]) -> None:
        self._detector = detector
        self._tam = (int(tam[0]), int(tam[1]))

    def detect(self, imagen: Any) -> Any:
        tam = (int(imagen.shape[1]), int(imagen.shape[0]))
        if tam != self._tam:
            self._detector.setInputSize(tam)
            self._tam = tam
        return self._detector.detect(imagen)


class _DetectorHaar:
    """Cascada Haar ``frontalface`` con la misma interfaz que YuNet (filas de 4 columnas)."""

    tipo = "haar"

    def __init__(self, cascada: Any) -> None:
        self._cascada = cascada

    def detect(self, imagen: Any) -> Any:
        gris = _a_gris(imagen)
        # Haar cuesta ~4× menos a media resolución; el tamaño mínimo de cara efectivo no cambia
        # (20 px a 160×120 = 40 px a 320×240) y las cajas se devuelven en la escala de entrada.
        factor = 1.0
        if int(gris.shape[1]) > 200:
            factor = 2.0
            gris = _reducir(gris, (int(gris.shape[1]) // 2, int(gris.shape[0]) // 2))
        if cv2 is not None:
            with contextlib.suppress(Exception):
                gris = cv2.equalizeHist(gris)
        lado = max(20, int(gris.shape[1]) // 8)
        caras = self._cascada.detectMultiScale(gris, scaleFactor=1.2, minNeighbors=5, minSize=(lado, lado))
        if caras is None or len(caras) == 0:
            return 0, None
        return len(caras), np.asarray(caras, dtype=np.float64).reshape(-1, 4) * factor


def _clase_cascada() -> Any:
    """``CascadeClassifier`` del núcleo (OpenCV 4) o de ``xobjdetect`` (OpenCV 5 con contrib)."""
    if cv2 is None:
        return None
    clase = getattr(cv2, "CascadeClassifier", None)
    if clase is None:
        clase = getattr(getattr(cv2, "xobjdetect", None), "CascadeClassifier", None)
    return clase


def _ruta_haar() -> Path | None:
    """XML de Haar de ``cv2.data`` o de las rutas del sistema (``opencv4`` y ``opencv5``)."""
    if cv2 is None:
        return None
    candidatos: list[Path] = []
    directorio = getattr(getattr(cv2, "data", None), "haarcascades", None)
    if directorio:
        candidatos.append(Path(str(directorio)) / _XML_HAAR)
    candidatos.extend(Path(d) / _XML_HAAR for d in _DIRS_HAAR_SISTEMA)
    for candidato in candidatos:
        try:
            if candidato.is_file():
                return candidato
        except OSError:
            continue
    return None


def _version_mayor_cv2() -> int:
    try:
        return int(str(getattr(cv2, "__version__", "4")).split(".", 1)[0])
    except ValueError:
        return 4


def ruta_modelo_yunet(data_dir: Any, *, explicita: Any = None, mayor: int | None = None) -> Path | None:
    """Modelo YuNet a usar (búsqueda compartida con ``aura_vision_pc``; nunca lanza).

    ``explicita`` (``settings.vision_modelo``) manda si se da: esa ruta o ``None`` si no existe.
    Si no, ``data_dir/models/face_detection_yunet*.onnx`` (el ``…_2023mar.onnx`` de la guía, el
    nombre corto de la v0.1 u otro), prefiriendo el que casa con la versión de OpenCV
    (``mayor``, por defecto la del ``cv2`` importado aquí) y el no cuantizado (sin ``int8``).
    """
    if explicita:
        try:
            ruta = Path(explicita)
            return ruta if ruta.is_file() else None
        except (OSError, TypeError, ValueError):
            return None
    if data_dir is None:
        return None
    try:
        directorio = Path(data_dir) / "models"
        candidatos = [p for p in directorio.glob(_PREFIJO_YUNET + "*.onnx") if p.is_file()]
    except (OSError, TypeError, ValueError):
        return None
    if not candidatos:
        return None
    version = _version_mayor_cv2() if mayor is None else int(mayor)

    def puntuacion(ruta: Path) -> tuple[int, str]:
        nombre = ruta.name.lower()
        p = 0
        if "int8" in nombre:
            p += 10
        if version >= 5:
            p += 0 if "2026" in nombre else 2
        else:
            p += 0 if "2023mar" in nombre else (1 if nombre == _PREFIJO_YUNET + ".onnx" else 3)
        return p, nombre

    return sorted(candidatos, key=puntuacion)[0]


# ---------------------------------------------------------------------------
# Windows (ctypes, solo lectura: ventana activa, proceso y estado de notificaciones)
# ---------------------------------------------------------------------------


class _Win32:
    """Lecturas de Win32 sin efectos (ventana en primer plano, ventanas visibles, proceso,
    rectángulos, QUNS)."""

    _PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
    _MONITOR_DEFAULTTONEAREST: Final = 2
    _CLASES_ESCRITORIO: Final = frozenset({"progman", "workerw", "shell_traywnd"})
    _DWMWA_EXTENDED_FRAME_BOUNDS: Final = 9      # rectángulo que se ve (sin los bordes invisibles)
    _DWMWA_CLOAKED: Final = 14                   # ≠ 0: oculta por DWM (otro escritorio virtual, UWP suspendida)

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD)]

        self._RECT = RECT
        self._MONITORINFO = MONITORINFO
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        s = ctypes.WinDLL("shell32", use_last_error=True)
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = wintypes.HWND
        for nombre in ("GetWindowTextW", "GetClassNameW"):
            funcion = getattr(u, nombre)
            funcion.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            funcion.restype = ctypes.c_int
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HANDLE
        u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        s.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        s.SHQueryUserNotificationState.restype = ctypes.c_long
        # Enumeración de ventanas de nivel superior (capturas bajo pedido: ventanas visibles).
        self._WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self._WNDENUMPROC, wintypes.LPARAM]
        u.EnumWindows.restype = wintypes.BOOL
        for nombre in ("IsWindowVisible", "IsIconic"):
            funcion = getattr(u, nombre)
            funcion.argtypes = [wintypes.HWND]
            funcion.restype = wintypes.BOOL
        d: Any = None
        try:  # opcional: sin DWM se usa GetWindowRect y ninguna ventana cuenta como oculta
            d = ctypes.WinDLL("dwmapi", use_last_error=True)
            d.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            d.DwmGetWindowAttribute.restype = ctypes.c_long
        except (OSError, AttributeError):
            d = None
        self._u, self._k, self._s, self._d = u, k, s, d

    def quns(self) -> int | None:
        """``SHQueryUserNotificationState`` (1 bloqueada, 2 ocupada, 3 D3D pantalla completa, 4 presentación, 5 normal)."""
        valor = self._ct.c_int(0)
        try:
            if self._s.SHQueryUserNotificationState(self._ct.byref(valor)) != 0:
                return None
        except OSError:
            return None
        return int(valor.value)

    def _texto(self, funcion: Any, hwnd: Any) -> str:
        buffer = self._ct.create_unicode_buffer(512)
        try:
            funcion(hwnd, buffer, 512)
        except OSError:
            return ""
        return buffer.value

    def _exe(self, hwnd: Any, cache: dict[int, str] | None = None) -> str:
        pid = self._wt.DWORD(0)
        self._u.GetWindowThreadProcessId(hwnd, self._ct.byref(pid))
        if not pid.value:
            return ""
        if cache is not None and int(pid.value) in cache:
            return cache[int(pid.value)]
        nombre = ""
        manejador = self._k.OpenProcess(self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if manejador:
            try:
                buffer = self._ct.create_unicode_buffer(1024)
                tam = self._wt.DWORD(1024)
                if self._k.QueryFullProcessImageNameW(manejador, 0, buffer, self._ct.byref(tam)):
                    nombre = _sanear_clase(buffer.value.replace("\\", "/").rsplit("/", 1)[-1])
            finally:
                self._k.CloseHandle(manejador)
        if not nombre and psutil is not None:
            with contextlib.suppress(Exception):
                nombre = _sanear_clase(psutil.Process(int(pid.value)).name())
        if cache is not None:
            cache[int(pid.value)] = nombre
        return nombre

    def _oculta_por_dwm(self, hwnd: Any) -> bool:
        """``True`` si DWM la oculta (otro escritorio virtual, app UWP suspendida): no se ve."""
        if self._d is None:
            return False
        valor = self._wt.DWORD(0)
        try:
            resultado = self._d.DwmGetWindowAttribute(hwnd, self._DWMWA_CLOAKED, self._ct.byref(valor),
                                                      self._ct.sizeof(valor))
        except OSError:
            return False
        return resultado == 0 and valor.value != 0

    def _rect_visible(self, hwnd: Any) -> tuple[int, int, int, int] | None:
        """``(izq, arriba, der, abajo)`` de lo que se ve de la ventana (DWM; si no, ``GetWindowRect``).

        ``GetWindowRect`` incluye los bordes invisibles de redimensionado (~8 px): una ventana
        maximizada en el monitor de al lado "tocaría" este. DWM da el marco que de verdad se ve.
        """
        rect = self._RECT()
        if self._d is not None:
            try:
                if self._d.DwmGetWindowAttribute(hwnd, self._DWMWA_EXTENDED_FRAME_BOUNDS, self._ct.byref(rect),
                                                 self._ct.sizeof(rect)) == 0:
                    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            except OSError:
                pass
        try:
            if self._u.GetWindowRect(hwnd, self._ct.byref(rect)):
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except OSError:
            pass
        return None

    def ventanas_visibles(self, region: Mapping[str, Any], *,
                          maximo: int = VENTANAS_ENUMERADAS_MAX) -> list[dict[str, str]] | None:
        """``[{app, titulo, clase}]`` de las ventanas de nivel superior que se ven en ``region``.

        Cuenta toda ventana visible (``IsWindowVisible``), sin minimizar, no oculta por DWM y cuyo
        marco toca ``region`` (``{left, top, width, height}``), en orden Z. No calcula la oclusión:
        una ventana tapada por otra cuenta como visible (ante la duda, se mira). ``None`` si la
        región no es válida, ``EnumWindows`` falla o hay más de ``maximo`` ventanas: quien llama
        falla cerrada. Solo lectura; los títulos nunca se registran.
        """
        try:
            izq, arriba = int(region["left"]), int(region["top"])
            der, abajo = izq + int(region["width"]), arriba + int(region["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if der <= izq or abajo <= arriba:
            return None
        hwnds: list[Any] = []
        desbordada = False

        def recolectar(hwnd: Any, _lparam: Any) -> bool:
            nonlocal desbordada
            if len(hwnds) >= maximo:
                desbordada = True
                return False
            hwnds.append(hwnd)
            return True

        retrollamada = self._WNDENUMPROC(recolectar)
        try:
            completa = bool(self._u.EnumWindows(retrollamada, 0))
        except OSError:
            return None
        if desbordada or not completa:
            return None
        cache: dict[int, str] = {}
        visibles: list[dict[str, str]] = []
        for hwnd in hwnds:
            if not hwnd:
                continue
            try:
                if not self._u.IsWindowVisible(hwnd) or self._u.IsIconic(hwnd) or self._oculta_por_dwm(hwnd):
                    continue
                rect = self._rect_visible(hwnd)
                if rect is not None:
                    r_izq, r_arriba, r_der, r_abajo = rect
                    if r_der <= r_izq or r_abajo <= r_arriba:
                        continue                  # sin área: no se ve
                    if r_der <= izq or r_izq >= der or r_abajo <= arriba or r_arriba >= abajo:
                        continue                  # en otro monitor
                visibles.append({"app": self._exe(hwnd, cache),
                                 "titulo": self._texto(self._u.GetWindowTextW, hwnd),
                                 "clase": self._texto(self._u.GetClassNameW, hwnd)})
            except OSError:
                return None
        return visibles

    def ventana_activa(self, *, con_titulo: bool) -> dict[str, Any]:
        """``{app, titulo, pantalla_completa, monitor}`` de la ventana en primer plano."""
        hwnd = self._u.GetForegroundWindow()
        if not hwnd:
            return {"app": "", "titulo": "", "pantalla_completa": None, "monitor": None}
        app = self._exe(hwnd)
        titulo = self._texto(self._u.GetWindowTextW, hwnd) if con_titulo else ""
        clase_ventana = self._texto(self._u.GetClassNameW, hwnd).lower()
        rect = self._RECT()
        info = self._MONITORINFO()
        info.cbSize = self._ct.sizeof(self._MONITORINFO)
        completa: bool | None = None
        monitor: dict[str, int] | None = None
        try:
            monitor_h = self._u.MonitorFromWindow(hwnd, self._MONITOR_DEFAULTTONEAREST)
            if monitor_h and self._u.GetMonitorInfoW(monitor_h, self._ct.byref(info)):
                m = info.rcMonitor
                monitor = {"left": int(m.left), "top": int(m.top),
                           "width": int(m.right - m.left), "height": int(m.bottom - m.top)}
                if self._u.GetWindowRect(hwnd, self._ct.byref(rect)):
                    completa = (clase_ventana not in self._CLASES_ESCRITORIO
                                and rect.left <= m.left and rect.top <= m.top
                                and rect.right >= m.right and rect.bottom >= m.bottom)
        except OSError:
            completa = None
        return {"app": app, "titulo": titulo, "pantalla_completa": completa, "monitor": monitor}


def _crear_win32() -> _Win32 | None:
    if sys.platform != "win32":
        return None
    try:
        return _Win32()
    except Exception as exc:
        log.debug("Win32 no disponible (%s)", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Fuentes de pantalla por defecto (hilo de pantalla)
# ---------------------------------------------------------------------------


def _mss_a_array(captura: Any) -> Any:
    """Vista BGRA de una captura de mss (sin copiar si se puede)."""
    try:
        return np.asarray(captura)
    except Exception:
        alto, ancho = int(captura.height), int(captura.width)
        return np.frombuffer(captura.bgra, dtype=np.uint8).reshape(alto, ancho, 4)


class _FuenteMss:
    """Píxeles con ``mss`` del monitor indicado o del principal (base de ``_FuenteWindows``).

    Sola no da metadatos (título y clase vacíos), así que nunca se usa sola: sin metadatos la
    privacidad falla cerrada y no se capturan píxeles.
    """

    def __init__(self) -> None:
        self._sct: Any = None
        self.monitor: dict[str, int] | None = None
        self._geometria: tuple[float, float, float, float] | None = None

    def metadatos(self) -> dict[str, Any]:
        return {"clase": "", "titulo": "", "pantalla_completa": None, "reproduciendo": None}

    def capturar(self) -> Any:
        if _mss is None or np is None:
            return None
        if self._sct is None:
            fabrica = getattr(_mss, "MSS", None) or getattr(_mss, "mss")
            self._sct = fabrica()
        region = self.monitor
        if region is None:
            region = getattr(self._sct, "primary_monitor", None)
            if not isinstance(region, Mapping):
                monitores = getattr(self._sct, "monitors", [])
                region = monitores[1] if len(monitores) > 1 else (monitores[0] if monitores else None)
        if not isinstance(region, Mapping):
            return None
        region = {k: int(region[k]) for k in ("left", "top", "width", "height")}
        captura = self._sct.grab(region)
        matriz = _mss_a_array(captura)
        paso = max(1, int(matriz.shape[1]) // ANCHO_PRESUBMUESTREO)
        reducida = np.ascontiguousarray(matriz[::paso, ::paso, :3])
        del matriz, captura
        self._geometria = (region["left"], region["top"], region["width"], region["height"])
        return reducida

    def geometria(self) -> tuple[float, float, float, float] | None:
        return self._geometria

    def cerrar(self) -> None:
        sct, self._sct = self._sct, None
        if sct is not None:
            with contextlib.suppress(Exception):
                sct.close()


class _FuenteWindows(_FuenteMss):
    """Windows: metadatos con ctypes y píxeles con mss del monitor de la ventana activa."""

    def __init__(self) -> None:
        super().__init__()
        self._win = _crear_win32()

    def metadatos(self) -> dict[str, Any]:
        if self._win is None:
            return super().metadatos()
        info = self._win.ventana_activa(con_titulo=True)
        quns = self._win.quns()
        completa = info.get("pantalla_completa")
        if quns in (2, 3, 4):
            completa = True
        self.monitor = info.get("monitor")
        return {"clase": info.get("app", ""), "titulo": info.get("titulo", ""),
                "pantalla_completa": completa, "reproduciendo": None, "juego": quns == 3}


# ---------------------------------------------------------------------------
# Hyprland y subprocesos (asíncronos, sin shell)
# ---------------------------------------------------------------------------


def _ruta_socket_hypr() -> Path | None:
    his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
    if not his or "/" in his or his in (".", ".."):
        return None
    candidatos = []
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        candidatos.append(Path(xdg) / "hypr" / his / ".socket.sock")
    candidatos.append(Path("/tmp/hypr") / his / ".socket.sock")
    for candidato in candidatos:
        with contextlib.suppress(OSError):
            if candidato.exists():
                return candidato
    return None


async def _consulta_hypr_socket(comando: str, timeout: float = TIMEOUT_HYPR_S) -> Any:
    """``j/activewindow``, ``j/monitors`` o ``j/clients`` por el socket de Hyprland → JSON (``None`` si falla).

    Lee hasta que Hyprland cierra la conexión (una respuesta grande llega en varios trozos) y
    como mucho ``MAX_RESPUESTA_HYPR`` bytes; una respuesta que no es JSON ("Invalid") da ``None``.
    """
    if (comando not in ("j/activewindow", "j/monitors", "j/clients")
            or not hasattr(asyncio, "open_unix_connection")):
        return None
    ruta = _ruta_socket_hypr()
    if ruta is None:
        return None
    escritor: asyncio.StreamWriter | None = None
    try:
        lector, escritor = await asyncio.wait_for(asyncio.open_unix_connection(str(ruta)), timeout=timeout)
        escritor.write(comando.encode("ascii"))
        await asyncio.wait_for(escritor.drain(), timeout=timeout)
        partes: list[bytes] = []
        total = 0
        while True:
            trozo = await asyncio.wait_for(lector.read(65536), timeout=timeout)
            if not trozo:
                break
            partes.append(trozo)
            total += len(trozo)
            if total > MAX_RESPUESTA_HYPR:
                raise ValueError("respuesta de Hyprland demasiado grande")
        datos = b"".join(partes)
        return json.loads(datos.decode("utf-8", "replace")) if datos else None
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, asyncio.TimeoutError) as exc:
        log.debug("Consulta %s a Hyprland fallida (%s)", comando, type(exc).__name__)
        return None
    finally:
        if escritor is not None:
            escritor.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(escritor.wait_closed(), timeout=timeout)


def _meta_hypr(ventana: Any) -> dict[str, Any]:
    """Metadatos de ``activewindow`` de Hyprland (clase, título, pantalla completa, vídeo, juego)."""
    if not isinstance(ventana, Mapping):
        return {"clase": "", "titulo": "", "pantalla_completa": None, "reproduciendo": None,
                "video": None, "juego": None}
    completa_bruta = ventana.get("fullscreen")
    if isinstance(completa_bruta, bool):
        completa: bool | None = completa_bruta
    elif isinstance(completa_bruta, int):
        completa = completa_bruta >= 2          # 1 = maximizada; 2 = pantalla completa
    else:
        completa = None
    tipo = str(ventana.get("contentType") or "").lower()
    inhibe = ventana.get("inhibitingIdle")
    if tipo == "video" or inhibe is True:
        reproduciendo: bool | None = True
    elif tipo or isinstance(inhibe, bool):
        reproduciendo = False
    else:
        reproduciendo = None
    return {"clase": str(ventana.get("class") or ""), "titulo": str(ventana.get("title") or ""),
            "pantalla_completa": completa, "reproduciendo": reproduciendo,
            "video": tipo == "video", "juego": tipo == "game" and completa is True}


def _geometria_monitor_hypr(monitor: Any) -> tuple[float, float, float, float] | None:
    """(izquierda, arriba, ancho, alto) de un monitor de ``j/monitors`` en coordenadas de layout.

    ``width``/``height`` son los del modo (píxeles físicos, sin rotar): se dividen por la escala
    y, con ``transform`` impar (90°/270°, también volteados), se intercambian, como hace Amadeus
    Desktop al calcular la caja del avatar. ``None`` si faltan datos.
    """
    if not isinstance(monitor, Mapping):
        return None
    try:
        escala = float(monitor.get("scale") or 1.0) or 1.0
        ancho = float(monitor["width"]) / escala
        alto = float(monitor["height"]) / escala
        transformacion = int(monitor.get("transform") or 0)
        if transformacion % 2 == 1:
            ancho, alto = alto, ancho
        geometria = (float(monitor["x"]), float(monitor["y"]), ancho, alto)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not all(math.isfinite(g) for g in geometria) or geometria[2] <= 0 or geometria[3] <= 0:
        return None
    return geometria


def _matar(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()


async def _ejecutar(argv: list[str], *, timeout: float, procesos: set[asyncio.subprocess.Process]) -> bytes | None:
    """Ejecuta ``argv`` sin shell; stdout si termina bien a tiempo, ``None`` si no."""
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
    except (OSError, ValueError) as exc:
        log.debug("No se pudo lanzar %s (%s)", Path(argv[0]).name if argv else "?", type(exc).__name__)
        return None
    procesos.add(proc)
    try:
        salida, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _matar(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        return None
    except asyncio.CancelledError:
        _matar(proc)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=ESPERA_SALIDA_CANCELADO_S)
        raise
    finally:
        procesos.discard(proc)
    if proc.returncode != 0:
        return None
    return salida


# ---------------------------------------------------------------------------
# Entorno (CPU, batería, temperatura, ventana activa, bloqueo): en un executor propio
# ---------------------------------------------------------------------------


def _leer_entorno_sistema(plataforma: str, win: _Win32 | None) -> dict[str, Any]:
    """Lectura síncrona y acotada del entorno para la autopausa y la degradación (sin títulos)."""
    datos: dict[str, Any] = {}
    if psutil is not None:
        with contextlib.suppress(Exception):
            datos["cpu_pct"] = float(psutil.cpu_percent(None))
        lector_bateria = getattr(psutil, "sensors_battery", None)
        if lector_bateria is not None:
            try:
                bateria = lector_bateria()
            except Exception as exc:
                log.debug("sensors_battery falló (%s)", type(exc).__name__)
                bateria = None
            if bateria is not None:
                porcentaje = getattr(bateria, "percent", None)
                enchufado = getattr(bateria, "power_plugged", None)
                datos["bateria_pct"] = float(porcentaje) if isinstance(porcentaje, (int, float)) else None
                datos["cargando"] = None if enchufado is None else bool(enchufado)
        lector_temp = getattr(psutil, "sensors_temperatures", None)
        if lector_temp is not None and plataforma != "win":
            try:
                sensores = lector_temp() or {}
                valores = [float(e.current) for chip, entradas in sensores.items()
                           if str(chip).lower().startswith(("k10temp", "coretemp", "zenpower", "cpu"))
                           for e in (entradas or []) if isinstance(getattr(e, "current", None), (int, float))]
                if valores:
                    datos["temp_c"] = max(valores)
            except Exception as exc:
                log.debug("sensors_temperatures falló (%s)", type(exc).__name__)
        if plataforma == "hypr":
            try:
                nombres = {str(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
                datos["bloqueada"] = bool(nombres & _PROCESOS_BLOQUEO)
            except Exception as exc:
                log.debug("process_iter falló (%s)", type(exc).__name__)
    if plataforma == "win" and win is not None:
        try:
            info = win.ventana_activa(con_titulo=False)
            quns = win.quns()
            app = str(info.get("app") or "")
            datos["app"] = app
            datos["quns"] = quns
            datos["bloqueada"] = quns == 1 or app in _EXES_BLOQUEO
            datos["juego"] = quns == 3
            datos["pantalla_completa"] = bool(info.get("pantalla_completa")) or quns in (2, 3, 4)
        except Exception as exc:
            log.debug("Lectura de la ventana activa fallida (%s)", type(exc).__name__)
    return datos


# ---------------------------------------------------------------------------
# Lóbulo
# ---------------------------------------------------------------------------


class VisionContinua:
    """Visión continua local (cámara + pantalla). API de §C2.9; ningún método público lanza.

    ``fabrica_captura(indice) -> captura | None`` sustituye a ``cv2.VideoCapture`` (tests) y
    ``fabrica_pantalla() -> fuente`` a la fuente de pantalla del sistema; una fuente ofrece
    ``metadatos() -> dict``, ``capturar() -> ndarray | None``, ``geometria()`` y ``cerrar()``
    (todas opcionales) y se usa desde el hilo daemon ``rams-vision-pantalla``.
    """

    def __init__(self, settings: Settings, bus: BusEventos | None, *, presencia: Any | None = None,
                 reloj: Callable[[], float] = time.monotonic,
                 fabrica_captura: Callable[[int], Any] | None = None,
                 fabrica_pantalla: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self._presencia = presencia          # SOLO LECTURA (autopausa por ausencia ≥ 0,8)
        self._reloj = reloj
        self._fabrica_captura = fabrica_captura
        self._fabrica_pantalla = fabrica_pantalla
        self._plataforma = _plataforma_actual()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._iniciado = False
        self._cerrado = False
        self._tareas: list[asyncio.Task[Any]] = []
        self._ejecutor: ThreadPoolExecutor | None = None
        self._procesos: set[asyncio.subprocess.Process] = set()

        # Interruptores.
        self._camara_runtime = True
        modo = str(getattr(settings, "pantalla_modo", "off") or "off").strip().lower()
        self._modo_pantalla = modo if modo in MODOS_PANTALLA else "off"
        self._pausa_hasta: float | None = None     # pausa del usuario (math.inf = hasta reanudar)
        self._pausa_motivo = ""
        self._autopausa = ""                        # motivo de la autopausa ("" = ninguna)
        self._llamada_hasta = -math.inf

        # Cámara.
        self._hilo_cam: threading.Thread | None = None
        self._parar_cam: threading.Event | None = None
        self._gen_cam = 0
        self._cam_abierta = False
        self._cam_abierta_ts = 0.0
        self._latido_ts = 0.0
        self._error_cam = ""                        # "" | "ocupada" | "error"
        self._fallos_apertura = 0
        self._reintento_ts = -math.inf
        self._detector_tipo: str | None = None
        self._ms_cam: float | None = None
        self._ultima_cam: MuestraCamara | None = None
        # Último cuadro de la cámara (solo memoria; lo escribe el hilo y solo lo lee instantanea_jpeg).
        self._cerrojo_cuadro = threading.Lock()
        self._cuadro: Any = None
        self._cuadro_ts = 0.0                       # time.monotonic() de la lectura
        self._cuadro_gen = 0
        self._integrador = Integrador(hfov_deg=float(getattr(settings, "vision_hfov", 70.0) or 70.0),
                                      espejo=bool(getattr(settings, "vision_espejo", False)))

        # Pantalla.
        self._hilo_pant: threading.Thread | None = None
        self._tarea_pant: asyncio.Task[Any] | None = None
        self._parar_pant: threading.Event | None = None
        self._gen_pant = 0
        self._pant_modo_actual = "off"              # lo lee el hilo de pantalla
        self._metricas = MetricasPantalla()
        self._caja: tuple[int, int, int, int] | None = None
        self._ultima_pant: MuestraPantalla | None = None
        self._ms_pant: float | None = None
        self._pant_pub: tuple[Any, ...] | None = None
        self._pant_pub_ts = -math.inf
        self._firma_pant: str | None = None
        self._clase_pant: str | None = None
        self._estimulo_pant_ts = -math.inf
        self._grim: str | None | bool = False       # False = aún no se buscó
        self._reintento_pant_ts = -math.inf

        # Entorno y degradación.
        self._escalera = EscaleraDegradacion()
        self._nivel = 0
        self._entorno: dict[str, Any] = {}
        self._win_entorno: _Win32 | None | bool = False
        self._estado_pub: dict[str, Any] | None = None

        # Puntos de inyección privados (tests): lectura del entorno, consultas a Hyprland y detector.
        self._leer_entorno: Callable[[], dict[str, Any]] = self._leer_entorno_defecto
        self._consulta_hypr: Callable[[str], Any] = _consulta_hypr_socket
        self._crear_detector: Callable[[tuple[int, int]], tuple[Any, str | None]] = self._crear_detector_defecto
        self._detector_tipo = self._detector_previsto()

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def _publicar(self, nombre: str, datos: Mapping[str, Any]) -> None:
        """Publica en el bus (si hay); nunca lanza."""
        if self.bus is None:
            return
        try:
            self.bus.publicar(nombre, dict(datos), origen=ORIGEN_BUS)
        except Exception as exc:
            log.debug("No se pudo publicar %s (%s)", nombre, type(exc).__name__)

    @staticmethod
    def _llamar_loop(loop: asyncio.AbstractEventLoop | None, funcion: Callable[..., Any], *args: Any) -> None:
        """``loop.call_soon_threadsafe`` tolerante a un loop ya cerrado (desde los hilos)."""
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(funcion, *args)
        except RuntimeError:
            log.debug("El loop se cerró antes de entregar un resultado de visión")

    def _programar_tick(self) -> None:
        """Pide un tick lo antes posible (seguro desde cualquier hilo; no bloquea)."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            self._llamar_loop(loop, self._tick_seguro)

    def _tick_seguro(self) -> None:
        try:
            self._tick(self._reloj())
        except Exception as exc:
            log.warning("Tick de visión fallido (%s)", type(exc).__name__)

    def _permitida(self) -> bool:
        """Opt-in de dos niveles: ``RAMS_CAMERA`` y ``RAMS_VISION_CONTINUA``."""
        valor = getattr(self.settings, "tiene_vision_continua", None)
        if isinstance(valor, bool):
            return valor
        return bool(getattr(self.settings, "vision_continua", False)) and bool(
            getattr(self.settings, "camara_habilitada", False))

    def _camara_habilitada(self) -> bool:
        return self._permitida() and self._camara_runtime and not self._cerrado

    def _deps_camara(self) -> bool:
        return np is not None and (cv2 is not None or self._fabrica_captura is not None)

    def _pausa_usuario_activa(self, ahora: float) -> bool:
        hasta = self._pausa_hasta
        if hasta is None:
            return False
        if ahora >= hasta:
            self._pausa_hasta = None
            log.info("Visión continua: termina la pausa")
            return False
        return True

    def _fps_camara_objetivo(self) -> float:
        fps = _acotar(float(getattr(self.settings, "vision_fps", 3.0) or 3.0), 1.0, 5.0)
        if self._detector_tipo == "haar":
            fps = min(fps, FPS_HAAR_MAX)
        if self._nivel >= 2:
            return 1.0
        if self._nivel == 1:
            return min(fps, 2.0)
        return fps

    def _fps_pantalla_objetivo(self) -> float:
        fps = _acotar(float(getattr(self.settings, "pantalla_fps", 1.0) or 1.0), PANTALLA_FPS_MIN, PANTALLA_FPS_MAX)
        return min(fps, 0.5) if self._nivel >= 1 else fps

    def _ruta_grim(self) -> str | None:
        if self._grim is False:
            self._grim = shutil.which("grim")
        return self._grim or None  # type: ignore[return-value]

    def _metadatos_posibles(self) -> bool:
        return self._fabrica_pantalla is not None or self._plataforma in ("win", "hypr")

    def _pixeles_posibles(self) -> bool:
        """Píxeles solo donde hay metadatos de la ventana activa para comprobar su privacidad.

        En X11 ``mss`` captura, pero sin título ni clase no se puede aplicar
        ``pantalla_excluir_re``: ahí la pantalla queda OFF (falla cerrada).
        """
        if np is None:
            return False
        if self._fabrica_pantalla is not None:
            return True
        if self._plataforma == "win":
            return _mss is not None
        if self._plataforma == "hypr":
            return self._ruta_grim() is not None
        return False

    def _pantalla_efectiva(self, ahora: float) -> str:
        """``off``/``metadatos``/``pixeles``/``pausada`` según modo, pausas, nivel y dependencias."""
        modo = self._modo_pantalla
        if modo == "off" or self._cerrado:
            return "off"
        if not (self._metadatos_posibles() or self._pixeles_posibles()):
            return "off"
        if self._pausa_usuario_activa(ahora) or self._autopausa == "bloqueada":
            return "pausada"
        if modo == "pixeles" and (self._autopausa or self._nivel >= 2 or not self._pixeles_posibles()):
            return "metadatos"
        return modo

    def _estado_camara(self, ahora: float) -> str:
        if not self._camara_habilitada() or not self._deps_camara():
            return "off"
        if self._pausa_usuario_activa(ahora) or self._autopausa:
            return "pausada"
        if self._error_cam in ("ocupada", "error"):
            return self._error_cam
        if self._cam_abierta and (self._nivel >= 1 or self._detector_tipo is None):
            return "degradada"
        return "continua"

    def _ausente_seguro(self, ahora: float) -> bool:
        """Ausencia con confianza ≥ 0,8 que la propia cámara no contradice.

        La presencia puede dar "ausente" solo por inactividad (10 min sin teclado ni ratón, p. ej.
        viendo un vídeo). Si la cámara continua está abierta con detector y vio una cara hace
        menos de ``SALIDA_S`` (o su flanco sigue en ``presente``), manda lo que ve: no se pausa.
        La autopausa por ausencia llega tras su propio flanco de salida (20 s sin cara).
        """
        presencia = self._presencia
        if presencia is None:
            return False
        try:
            estado = presencia.estado()
            ausente = (getattr(estado, "presente", True) is False
                       and float(getattr(estado, "confianza", 0.0)) >= CONFIANZA_AUSENCIA)
        except Exception as exc:
            log.debug("No se pudo leer la presencia (%s)", type(exc).__name__)
            return False
        if not ausente:
            return False
        integrador = self._integrador
        if self._cam_abierta and integrador.detector_disponible and (
                integrador.flanco == "presente" or integrador.vio_rostro(ahora, SALIDA_S)):
            return False
        return True

    # ------------------------------------------------------------------
    # Detector (rutas y creación)
    # ------------------------------------------------------------------

    def _ruta_yunet(self) -> Path | None:
        """``settings.vision_modelo`` o ``data_dir/models/face_detection_yunet*.onnx`` (según OpenCV)."""
        return ruta_modelo_yunet(getattr(self.settings, "data_dir", None),
                                 explicita=getattr(self.settings, "vision_modelo", None))

    def _orden_detectores(self) -> tuple[str, str]:
        preferido = str(getattr(self.settings, "vision_detector", "auto") or "auto").strip().lower()
        return ("haar", "yunet") if preferido == "haar" else ("yunet", "haar")

    def _detector_previsto(self) -> str | None:
        """Qué detector se usará (sin crearlo): para el diagnóstico y el estado."""
        if cv2 is None:
            return None
        for tipo in self._orden_detectores():
            if tipo == "yunet" and hasattr(cv2, "FaceDetectorYN") and self._ruta_yunet() is not None:
                return "yunet"
            if tipo == "haar" and _clase_cascada() is not None and _ruta_haar() is not None:
                return "haar"
        return None

    def _crear_detector_defecto(self, tam: tuple[int, int]) -> tuple[Any, str | None]:
        """Crea el detector en el hilo de cámara: YuNet preferido, Haar de respaldo, o ninguno."""
        if cv2 is None:
            return None, None
        for tipo in self._orden_detectores():
            try:
                if tipo == "yunet":
                    ruta = self._ruta_yunet()
                    clase = getattr(cv2, "FaceDetectorYN", None)
                    crear = getattr(clase, "create", None) or getattr(cv2, "FaceDetectorYN_create", None)
                    if ruta is not None and crear is not None:
                        detector = crear(str(ruta), "", (int(tam[0]), int(tam[1])), 0.8, 0.3, 50)
                        if detector is not None:
                            return _DetectorYuNet(detector, tam), "yunet"
                else:
                    ruta = _ruta_haar()
                    clase = _clase_cascada()
                    if ruta is not None and clase is not None:
                        cascada = clase(str(ruta))
                        if not cascada.empty():
                            return _DetectorHaar(cascada), "haar"
            except Exception as exc:
                log.warning("No se pudo crear el detector %s (%s)", tipo, type(exc).__name__)
        return None, None

    # ------------------------------------------------------------------
    # Cámara: hilo
    # ------------------------------------------------------------------

    def _abrir_captura(self) -> Any:
        """Captura abierta (fábrica o ``cv2.VideoCapture`` con los backends del SO) o ``None``."""
        indice = int(getattr(self.settings, "camara_indice", 0) or 0)
        if self._fabrica_captura is not None:
            captura = self._fabrica_captura(indice)
            if captura is None:
                return None
            abierta = getattr(captura, "isOpened", None)
            if callable(abierta) and not abierta():
                with contextlib.suppress(Exception):
                    captura.release()
                return None
            return captura
        if cv2 is None:
            return None
        if sys.platform.startswith("linux"):
            backends = (getattr(cv2, "CAP_V4L2", 200),)
        elif sys.platform == "win32":
            backends = (getattr(cv2, "CAP_DSHOW", 700), getattr(cv2, "CAP_MSMF", 1400))
        else:
            backends = (getattr(cv2, "CAP_ANY", 0),)
        for backend in backends:
            if backend == getattr(cv2, "CAP_MSMF", 1400):
                os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
            captura = None
            try:
                captura = cv2.VideoCapture(indice, backend)
                if captura is not None and captura.isOpened():
                    return captura
            except Exception as exc:
                log.debug("VideoCapture con el backend %s falló (%s)", backend, type(exc).__name__)
            if captura is not None:
                with contextlib.suppress(Exception):
                    captura.release()
        return None

    def _configurar_captura(self, captura: Any) -> None:
        fps_pedidos = int(_acotar(math.ceil(self._fps_camara_objetivo() * 3), FPS_CAPTURA_MIN, FPS_CAPTURA_MAX))
        ajustes = (("CAP_PROP_FOURCC", _FOURCC_MJPG), ("CAP_PROP_FRAME_WIDTH", 640),
                   ("CAP_PROP_FRAME_HEIGHT", 480), ("CAP_PROP_FPS", fps_pedidos), ("CAP_PROP_BUFFERSIZE", 1))
        for nombre, valor in ajustes:
            propiedad = getattr(cv2, nombre, _PROPS_CAPTURA[nombre]) if cv2 is not None else _PROPS_CAPTURA[nombre]
            with contextlib.suppress(Exception):
                captura.set(propiedad, valor)

    @staticmethod
    def _leer_fresco(captura: Any, es_dshow: bool) -> tuple[bool, Any]:
        """Un cuadro reciente: DSHOW lee dos veces (el primero es viejo); V4L2/MSMF drenan la cola."""
        agarrar = getattr(captura, "grab", None)
        recuperar = getattr(captura, "retrieve", None)
        if es_dshow or not callable(agarrar) or not callable(recuperar):
            if es_dshow:
                captura.read()
            return captura.read()
        for _ in range(5):
            inicio = time.monotonic()
            if not agarrar():
                return False, None
            if time.monotonic() - inicio > 0.015:     # esperó a un cuadro nuevo: la cola está vacía
                break
        return recuperar()

    def _hilo_camara(self, gen: int, parar: threading.Event, loop: asyncio.AbstractEventLoop | None) -> None:
        """Cuerpo del hilo daemon ``rams-vision-cam``. Al bus solo llegan escalares (por el loop)."""
        captura = None
        abierta = False
        error = ""
        inicio = time.monotonic()
        try:
            captura = self._abrir_captura()
            if captura is None:
                error = "ocupada"
                return
            abierta = True
            self._configurar_captura(captura)
            tam = TAM_ANALISIS_REDUCIDO if self._nivel >= 2 else TAM_ANALISIS
            detector, tipo = self._crear_detector(tam)
            self._llamar_loop(loop, self._al_camara_abierta, gen, tipo)
            try:
                es_dshow = str(captura.getBackendName()).upper() == "DSHOW"
            except Exception:
                es_dshow = False
            previo = None
            proximo = time.monotonic() + CALENTAMIENTO_S
            fallos = 0
            while not parar.is_set():
                espera = proximo - time.monotonic()
                if espera > 0:
                    if parar.wait(min(espera, 0.25)):
                        break
                    continue
                tam = TAM_ANALISIS_REDUCIDO if self._nivel >= 2 else TAM_ANALISIS
                ok, cuadro = self._leer_fresco(captura, es_dshow)
                if parar.is_set():
                    break
                if not ok or cuadro is None:
                    fallos += 1
                    if fallos >= FALLOS_LECTURA_MAX:
                        error = "error"
                        log.warning("La cámara dejó de entregar cuadros; se libera")
                        break
                    parar.wait(0.2)
                    continue
                fallos = 0
                self._guardar_cuadro(gen, cuadro)   # solo para instantanea_jpeg (se sobrescribe)
                # Cadencia fija: el periodo cuenta desde la cita anterior, no desde que terminó la
                # lectura (en DSHOW la 2.ª lectura espera al cuadro siguiente); con retraso no
                # se recupera a ráfagas: la siguiente lectura va en cuanto se pueda.
                proximo = max(proximo + 1.0 / max(0.5, self._fps_camara_objetivo()), time.monotonic())
                integrador = self._integrador
                try:
                    muestra, previo = analizar(cuadro, previo, detector, hfov_deg=integrador.hfov_deg, tam=tam,
                                               base_yaw=integrador.base_yaw, base_pitch=integrador.base_pitch)
                finally:
                    del cuadro                       # solo queda la referencia de _guardar_cuadro
                self._llamar_loop(loop, self._al_muestra_camara, gen, muestra)
        except Exception as exc:
            error = "error"
            log.warning("Fallo en el hilo de la cámara continua (%s)", type(exc).__name__)
        finally:
            self._soltar_cuadro(gen)
            if captura is not None:
                try:
                    captura.release()                # SIEMPRE, y desde este hilo: apaga el LED
                except Exception as exc:
                    log.debug("release() de la cámara falló (%s)", type(exc).__name__)
            ms = int((time.monotonic() - inicio) * 1000)
            self._llamar_loop(loop, self._al_camara_cerrada, gen, abierta, error, ms)

    def _arrancar_camara(self) -> None:
        loop = self._loop
        if loop is None or self._hilo_cam is not None:
            return
        self._gen_cam += 1
        gen = self._gen_cam
        parar = threading.Event()
        hilo = threading.Thread(target=self._hilo_camara, args=(gen, parar, loop),
                                name="rams-vision-cam", daemon=True)
        self._cam_abierta = False
        self._cerrar_integrador()
        self._parar_cam = parar
        self._hilo_cam = hilo
        try:
            hilo.start()
        except RuntimeError as exc:
            log.warning("No se pudo crear el hilo de la cámara (%s)", exc)
            self._hilo_cam = None
            self._parar_cam = None
            self._error_cam = "error"
            self._reintento_ts = self._reloj() + REINTENTO_ERROR_S

    def _detener_camara(self) -> None:
        """Solo señala el ``threading.Event``: el hilo libera la cámara y avisa (vuelve en µs)."""
        parar = self._parar_cam
        if parar is not None:
            parar.set()

    def _guardar_cuadro(self, gen: int, cuadro: Any) -> None:
        """Hilo de cámara: guarda (sobrescribe) la referencia al último cuadro, solo en memoria."""
        with self._cerrojo_cuadro:
            self._cuadro = cuadro
            self._cuadro_ts = time.monotonic()
            self._cuadro_gen = gen

    def _soltar_cuadro(self, gen: int | None = None) -> None:
        """Olvida el último cuadro (pausa, apagado, cierre o fin del hilo de la generación ``gen``)."""
        with self._cerrojo_cuadro:
            if gen is None or self._cuadro_gen == gen:
                self._cuadro = None
                self._cuadro_ts = 0.0

    # -- callbacks en el loop -----------------------------------------------

    def _al_camara_abierta(self, gen: int, tipo: str | None) -> None:
        if gen != self._gen_cam or self._cerrado:
            return
        ahora = self._reloj()
        self._cam_abierta = True
        self._cam_abierta_ts = ahora
        self._latido_ts = ahora
        self._detector_tipo = tipo
        self._error_cam = ""
        self._fallos_apertura = 0
        self._ms_cam = None
        self._integrador.detector_disponible = tipo is not None
        self._integrador.periodo_s = 1.0 / max(0.5, self._fps_camara_objetivo())
        self._integrador.abrir(ahora)
        self._publicar("vision.camara", self._carga_camara(True, 0))
        log.info("Visión continua: cámara abierta (%s)", tipo or "sin detector de rostros")
        self._publicar_estado()

    def _al_muestra_camara(self, gen: int, muestra: MuestraCamara) -> None:
        if gen != self._gen_cam or not self._cam_abierta:
            return
        try:
            m = replace(muestra, ts=self._reloj())
            primera = self._ultima_cam is None
            self._ultima_cam = m
            if not primera:   # la primera muestra incluye el calentamiento del detector (hasta segundos)
                coste = min(float(m.ms_proc), 1000.0)
                self._ms_cam = coste if self._ms_cam is None else (1 - ALFA_MS) * self._ms_cam + ALFA_MS * coste
            for nombre, datos in self._integrador.muestra(m):
                self._publicar(nombre, datos)
        except Exception as exc:
            log.debug("Muestra de cámara descartada (%s)", type(exc).__name__)

    def _al_camara_cerrada(self, gen: int, abierta: bool, error: str, ms: int) -> None:
        if gen == self._gen_cam:
            self._hilo_cam = None
            self._parar_cam = None
            self._cam_abierta = False
            self._ultima_cam = None
            self._cerrar_integrador()
            if error:
                self._error_cam = error
                if error == "ocupada":
                    self._fallos_apertura += 1
                    espera = REINTENTOS_APERTURA_S[min(self._fallos_apertura, len(REINTENTOS_APERTURA_S)) - 1]
                    log.info("Visión continua: la cámara no abre (¿ocupada?); reintento en %s s", _fmt_fps(espera))
                else:
                    espera = REINTENTO_ERROR_S
                self._reintento_ts = self._reloj() + espera
        if abierta:   # el LED se apaga también si el hilo era de una generación anterior
            self._publicar("vision.camara", {"activa": False, "habilitada": self._camara_habilitada(),
                                             "duracion_ms": max(0, int(ms)), "fuente": FUENTE_BUS})
            log.info("Visión continua: cámara liberada")
        self._publicar_estado()

    def _cerrar_integrador(self) -> None:
        """Cierra la sesión del integrador y publica su "ya no veo a nadie" (si hacía falta)."""
        for nombre, datos in self._integrador.cerrar():
            self._publicar(nombre, datos)

    def _carga_camara(self, activa: bool, duracion_ms: int) -> dict[str, Any]:
        return {"activa": bool(activa), "habilitada": self._camara_habilitada(),
                "duracion_ms": max(0, int(duracion_ms)), "fuente": FUENTE_BUS}

    # ------------------------------------------------------------------
    # Pantalla
    # ------------------------------------------------------------------

    def _es_privada(self, meta: Mapping[str, Any]) -> bool:
        return titulo_privado(self.settings, meta.get("titulo"), meta.get("clase"))

    @staticmethod
    def _meta_valida(meta: Any) -> bool:
        """``True`` si los metadatos dicen qué ventana es (título o clase no vacíos).

        Sin ellos no se puede comprobar ``pantalla_excluir_re``: la privacidad falla cerrada y
        esa mirada se queda en metadatos (sin píxeles).
        """
        if not isinstance(meta, Mapping):
            return False
        return bool(str(meta.get("titulo") or "").strip() or str(meta.get("clase") or "").strip())

    def _procesar_pantalla(self, meta: Mapping[str, Any], cuadro: Any, geometria: Any,
                           privada: bool, *, olvidar: bool = False) -> MuestraPantalla:
        """Métricas escalares de una mirada a la pantalla (el cuadro no sale de aquí).

        ``ms_proc`` mide solo el análisis local, no la captura del sistema (BitBlt o ``grim``):
        una captura lenta no debe degradar la cámara por la escalera. ``olvidar`` (en modo
        píxeles sin cuadro) borra el cuadro anterior para no inventar cambios ni cortes después.
        """
        inicio = time.perf_counter()
        clase = _sanear_clase(meta.get("clase"))
        titulo = "" if privada else _recortar_titulo(meta.get("titulo"))
        if cuadro is not None and not privada and np is not None:
            mini = _reducir(_a_bgr(cuadro), TAM_MINI_PANTALLA)
            mascara = None
            if bool(getattr(self.settings, "pantalla_excluir_avatar", True)):
                mascara = mascara_caja(self._caja, geometria)
            metricas = self._metricas.actualizar(mini, mascara)
            del mini
        else:
            if privada or olvidar:
                self._metricas.reiniciar()
            metricas = {"brillo": 0.0, "tono": None, "cambio": 0.0, "corte": False, "video": False}
        completa = meta.get("pantalla_completa")
        reproduciendo = meta.get("reproduciendo")
        return MuestraPantalla(
            ts=time.monotonic(),
            cambio=round(float(metricas["cambio"]), 4),
            brillo=round(float(metricas["brillo"]), 4),
            tono=None if metricas["tono"] is None else int(metricas["tono"]),
            corte=bool(metricas["corte"]),
            video=bool(metricas["video"]) or meta.get("video") is True,
            pantalla_completa=None if completa is None else bool(completa),
            reproduciendo=None if reproduciendo is None else bool(reproduciendo),
            clase=clase, titulo=titulo, privada=bool(privada),
            ms_proc=round((time.perf_counter() - inicio) * 1000.0, 3),
        )

    @staticmethod
    def _meta_de(fuente: Any) -> dict[str, Any]:
        lector = getattr(fuente, "metadatos", None)
        if not callable(lector):
            return {}
        datos = lector()
        return dict(datos) if isinstance(datos, Mapping) else {}

    def _muestrear_pantalla_sync(self, fuente: Any, modo: str) -> MuestraPantalla:
        """Una mirada desde el hilo de pantalla: metadatos, privacidad y (si toca) píxeles.

        Falla cerrada: sin metadatos válidos (título y clase vacíos) antes **y** después de la
        captura no hay píxeles, porque no se puede comprobar si la ventana es privada.
        """
        meta = self._meta_de(fuente)
        privada = self._es_privada(meta)
        cuadro = None
        geometria = None
        if modo == "pixeles" and not privada and np is not None and self._meta_valida(meta):
            capturar = getattr(fuente, "capturar", None)
            if callable(capturar):
                cuadro = capturar()
            if cuadro is not None:
                # La ventana pudo cambiar durante la captura: si ahora es privada (o ya no se
                # sabe cuál es), el cuadro se descarta.
                despues = self._meta_de(fuente)
                if not self._meta_valida(despues):
                    cuadro = None
                elif self._es_privada(despues):
                    cuadro = None
                    meta, privada = despues, True
                else:
                    lector_geo = getattr(fuente, "geometria", None)
                    geometria = lector_geo() if callable(lector_geo) else None
        try:
            return self._procesar_pantalla(meta, cuadro, geometria, privada,
                                           olvidar=modo == "pixeles" and cuadro is None)
        finally:
            del cuadro

    def _crear_fuente_pantalla(self) -> Any:
        if self._fabrica_pantalla is not None:
            return self._fabrica_pantalla()
        if self._plataforma == "win":
            return _FuenteWindows()
        return None       # X11 sin metadatos de ventanas: sin fuente (falla cerrada)

    def _hilo_pantalla(self, gen: int, parar: threading.Event, loop: asyncio.AbstractEventLoop | None) -> None:
        """Cuerpo del hilo daemon ``rams-vision-pantalla`` (Windows, X11 o fuente inyectada)."""
        fuente = None
        try:
            fuente = self._crear_fuente_pantalla()
            if fuente is None:
                return
            while not parar.is_set():
                inicio = time.monotonic()
                modo = self._pant_modo_actual
                if modo in ("metadatos", "pixeles"):
                    try:
                        muestra = self._muestrear_pantalla_sync(fuente, modo)
                        if not parar.is_set():
                            self._llamar_loop(loop, self._al_muestra_pantalla, gen, muestra)
                    except Exception as exc:
                        log.debug("Muestra de pantalla fallida (%s)", type(exc).__name__)
                periodo = 1.0 / max(PANTALLA_FPS_MIN, self._fps_pantalla_objetivo())
                parar.wait(max(0.05, periodo - (time.monotonic() - inicio)))
        except Exception as exc:
            log.warning("Fallo en el hilo de pantalla (%s)", type(exc).__name__)
        finally:
            if fuente is not None:
                cerrar = getattr(fuente, "cerrar", None)
                if callable(cerrar):
                    with contextlib.suppress(Exception):
                        cerrar()
            self._llamar_loop(loop, self._al_pantalla_terminada, gen, parar.is_set())

    async def _muestrear_pantalla_hypr(self, modo: str) -> MuestraPantalla:
        """Una mirada en Hyprland: ``activewindow`` por el socket y, si toca, ``grim`` a PPM.

        Falla cerrada: si la consulta falla (socket caído o lento) o no dice qué ventana es, no
        se lanza ``grim``; si la re-consulta tras la captura falla, el cuadro se descarta.
        """
        meta = _meta_hypr(await self._consulta_hypr("j/activewindow"))
        privada = self._es_privada(meta)
        cuadro = None
        geometria = None
        grim = self._ruta_grim()
        if modo == "pixeles" and not privada and grim and np is not None and self._meta_valida(meta):
            monitores = await self._consulta_hypr("j/monitors")
            monitor = None
            if isinstance(monitores, list):
                monitor = next((m for m in monitores if isinstance(m, Mapping) and m.get("focused")), None)
            argv = [grim, "-s", "0.1", "-t", "ppm"]
            nombre = str(monitor.get("name") or "") if monitor is not None else ""
            if _RE_SALIDA_MONITOR.fullmatch(nombre):
                argv += ["-o", nombre]
            argv.append("-")
            salida = await _ejecutar(argv, timeout=TIMEOUT_GRIM_S, procesos=self._procesos)
            if salida:
                cuadro = _parsear_ppm(salida)
            del salida
            if cuadro is not None:
                despues = _meta_hypr(await self._consulta_hypr("j/activewindow"))
                if not self._meta_valida(despues):
                    cuadro = None
                elif self._es_privada(despues):
                    cuadro = None
                    meta, privada = despues, True
                elif monitor is not None:
                    geometria = _geometria_monitor_hypr(monitor)
        try:
            return self._procesar_pantalla(meta, cuadro, geometria, privada,
                                           olvidar=modo == "pixeles" and cuadro is None)
        finally:
            del cuadro

    async def _bucle_pantalla_hypr(self, gen: int, parar: threading.Event) -> None:
        try:
            while not parar.is_set() and not self._cerrado:
                inicio = time.monotonic()
                modo = self._pant_modo_actual
                if modo in ("metadatos", "pixeles"):
                    try:
                        self._al_muestra_pantalla(gen, await self._muestrear_pantalla_hypr(modo))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.debug("Muestra de pantalla (Hyprland) fallida (%s)", type(exc).__name__)
                periodo = 1.0 / max(PANTALLA_FPS_MIN, self._fps_pantalla_objetivo())
                restante = max(0.05, periodo - (time.monotonic() - inicio))
                fin = time.monotonic() + restante
                while not parar.is_set() and time.monotonic() < fin:
                    await asyncio.sleep(min(0.25, max(0.0, fin - time.monotonic())))
        finally:
            self._al_pantalla_terminada(gen, parar.is_set() or self._cerrado)

    def _arrancar_pantalla(self) -> None:
        loop = self._loop
        if loop is None or self._hilo_pant is not None or self._tarea_pant is not None:
            return
        self._gen_pant += 1
        gen = self._gen_pant
        parar = threading.Event()
        self._parar_pant = parar
        if self._plataforma == "hypr" and self._fabrica_pantalla is None:
            self._tarea_pant = loop.create_task(self._bucle_pantalla_hypr(gen, parar), name="rams-vision-pantalla")
            return
        hilo = threading.Thread(target=self._hilo_pantalla, args=(gen, parar, loop),
                                name="rams-vision-pantalla", daemon=True)
        self._hilo_pant = hilo
        try:
            hilo.start()
        except RuntimeError as exc:
            log.warning("No se pudo crear el hilo de pantalla (%s)", exc)
            self._hilo_pant = None
            self._parar_pant = None

    def _detener_pantalla(self) -> None:
        parar = self._parar_pant
        if parar is not None:
            parar.set()

    def _al_pantalla_terminada(self, gen: int, pedida: bool = True) -> None:
        if gen == self._gen_pant:
            self._hilo_pant = None
            self._tarea_pant = None
            self._parar_pant = None
            if not pedida:     # terminó sola (sin fuente o rota): no se relanza en cada tick
                self._reintento_pant_ts = self._reloj() + REINTENTO_PANTALLA_S

    def _carga_pantalla(self, m: MuestraPantalla) -> dict[str, Any]:
        """Payload de ``pantalla.estado`` (``titulo`` vacío si la ventana es privada).

        ``no_molestar`` sale solo de señales del sistema: pantalla completa (en Windows incluye
        QUNS 2/3/4: ocupado, juego D3D y presentación), juego y app de llamada. El ``video`` de
        la heurística de píxeles (una región que cambia con el resto quieto: también una terminal
        con logs o un GIF) es percepción y veto de travesuras, nunca "no molestar".
        """
        llamada = self._reloj() < self._llamada_hasta
        no_molestar = bool(m.pantalla_completa is True or bool(self._entorno.get("juego")) or llamada)
        return {
            "cambio": float(m.cambio), "brillo": float(m.brillo), "tono": m.tono, "video": bool(m.video),
            "pantalla_completa": m.pantalla_completa, "reproduciendo": m.reproduciendo,
            "no_molestar": no_molestar, "privada": bool(m.privada), "clase": m.clase,
            "titulo": "" if m.privada else m.titulo[:TITULO_MAX],
        }

    def _al_muestra_pantalla(self, gen: int, muestra: MuestraPantalla) -> None:
        if gen != self._gen_pant or self._cerrado:
            return
        try:
            ahora = self._reloj()
            m = replace(muestra, ts=ahora)
            self._ultima_pant = m
            if m.ms_proc > 0 and self._pant_modo_actual == "pixeles":
                coste = min(float(m.ms_proc), 1000.0)
                self._ms_pant = coste if self._ms_pant is None else (
                    (1 - ALFA_MS) * self._ms_pant + ALFA_MS * coste)
            carga = self._carga_pantalla(m)
            clave = (round(m.cambio, 2), round(m.brillo, 2), m.tono, m.video, m.pantalla_completa,
                     m.reproduciendo, carga["no_molestar"], m.privada, m.clase, carga["titulo"])
            transcurrido = ahora - self._pant_pub_ts
            if (clave != self._pant_pub and transcurrido >= INTERVALO_PANTALLA_MIN_S - 1e-6) \
                    or transcurrido >= LATIDO_PANTALLA_S - 1e-6:
                self._pant_pub = clave
                self._pant_pub_ts = ahora
                self._publicar("pantalla.estado", carga)
            self._estimulo_pantalla(m, ahora)
        except Exception as exc:
            log.debug("Muestra de pantalla descartada (%s)", type(exc).__name__)

    def _estimulo_pantalla(self, m: MuestraPantalla, ahora: float) -> None:
        """``pantalla.estimulo`` (≤ 1 cada 5 s) con un corte de escena o una firma nueva."""
        if m.privada:
            firma = "pantalla:privada"
        elif self._pant_modo_actual == "pixeles":
            tono = "n" if m.tono is None else str(m.tono)
            firma = f"pantalla:{m.clase or '-'}:{tono}:{min(4, int(m.brillo * 5))}"
        else:
            firma = f"pantalla:{m.clase or '-'}"
        clase_anterior = self._clase_pant
        if self._firma_pant is None:            # la primera mirada solo fija la referencia
            self._firma_pant = firma
            self._clase_pant = m.clase
            return
        if not (m.corte or firma != self._firma_pant):
            return
        if ahora - self._estimulo_pant_ts < INTERVALO_ESTIMULO_PANTALLA_S - 1e-6:
            return
        if m.corte:
            magnitud = max(0.5, m.cambio)
        elif m.clase != clase_anterior:
            magnitud = 0.6
        else:
            magnitud = 0.3
        self._firma_pant = firma
        self._clase_pant = m.clase
        self._estimulo_pant_ts = ahora
        self._publicar("pantalla.estimulo", {"firma": firma[:96], "magnitud": round(_acotar(magnitud, 0.0, 1.0), 3)})

    # ------------------------------------------------------------------
    # Entorno: autopausa y degradación
    # ------------------------------------------------------------------

    def _leer_entorno_defecto(self) -> dict[str, Any]:
        """Lectura síncrona (executor ``rams-vision``)."""
        if self._win_entorno is False:
            self._win_entorno = _crear_win32() if self._plataforma == "win" else None
        win = self._win_entorno if isinstance(self._win_entorno, _Win32) else None
        return _leer_entorno_sistema(self._plataforma, win)

    def _aplicar_entorno(self, ahora: float, datos: Mapping[str, Any]) -> None:
        """Aplica una lectura del entorno: app de llamada, nivel de degradación y autopausa."""
        self._entorno = dict(datos)
        app = _sanear_clase(datos.get("app") or "")
        if app and _coincide_app(app, getattr(self.settings, "vision_apps_pausa", ())):
            self._llamada_hasta = ahora + LLAMADA_PEGAJOSA_S
        anterior = self._nivel
        self._nivel = self._escalera.evaluar(
            ahora,
            cpu_pct=datos.get("cpu_pct"),
            temp_c=datos.get("temp_c"),
            ms_camara=self._ms_cam if self._cam_abierta else None,
            ms_pantalla=self._ms_pant if self._pant_modo_actual == "pixeles" else None,
            cargando=datos.get("cargando"),
        )
        if self._nivel != anterior:
            log.info("Visión continua: nivel de degradación %d → %d", anterior, self._nivel)
        self._evaluar_autopausa(ahora)

    def _evaluar_autopausa(self, ahora: float) -> str:
        e = self._entorno
        bateria = e.get("bateria_pct")
        if e.get("bloqueada"):
            motivo = "bloqueada"
        elif self._ausente_seguro(ahora):
            motivo = "ausente"
        elif ahora < self._llamada_hasta:
            motivo = "llamada"
        elif isinstance(bateria, (int, float)) and bateria < BATERIA_MIN_PCT and e.get("cargando") is False:
            motivo = "bateria"
        elif e.get("juego"):
            motivo = "juego"
        else:
            motivo = ""
        if motivo != self._autopausa:
            if motivo:
                log.info("Visión continua: autopausa (%s)", motivo)
            else:
                log.info("Visión continua: fin de la autopausa (%s)", self._autopausa)
            self._autopausa = motivo
        return motivo

    async def _leer_entorno_async(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        datos: dict[str, Any] = {}
        ejecutor = self._ejecutor
        if ejecutor is not None:
            try:
                leido = await asyncio.wait_for(loop.run_in_executor(ejecutor, self._leer_entorno),
                                               timeout=TIMEOUT_ENTORNO_S)
                if isinstance(leido, Mapping):
                    datos.update(leido)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Lectura del entorno fallida (%s)", type(exc).__name__)
        if self._plataforma == "hypr":
            meta = _meta_hypr(await self._consulta_hypr("j/activewindow"))
            datos.setdefault("app", meta.get("clase") or "")
            datos["juego"] = bool(meta.get("juego"))
            datos["pantalla_completa"] = meta.get("pantalla_completa")
        return datos

    # ------------------------------------------------------------------
    # Integrador (2 Hz)
    # ------------------------------------------------------------------

    def _quiere_camara(self, ahora: float) -> bool:
        return (self._camara_habilitada() and self._deps_camara()
                and not self._pausa_usuario_activa(ahora) and not self._autopausa)

    def _reconciliar(self, ahora: float) -> None:
        """Arranca o para los hilos según interruptores, pausas, autopausa y reintentos."""
        self._evaluar_autopausa(ahora)
        if self._quiere_camara(ahora):
            if self._hilo_cam is None and ahora >= self._reintento_ts and self._loop is not None:
                self._arrancar_camara()
        elif self._hilo_cam is not None:
            self._detener_camara()
            self._soltar_cuadro()
        modo = self._pantalla_efectiva(ahora)
        self._pant_modo_actual = modo
        if modo in ("metadatos", "pixeles"):
            if (self._hilo_pant is None and self._tarea_pant is None and self._loop is not None
                    and ahora >= self._reintento_pant_ts):
                self._arrancar_pantalla()
        elif self._hilo_pant is not None or self._tarea_pant is not None:
            self._detener_pantalla()

    def _tick(self, ahora: float) -> None:
        """Un paso del integrador: reconciliar, flancos, latido de ``vision.camara`` y estado."""
        if self._cerrado:
            return
        self._reconciliar(ahora)
        self._integrador.periodo_s = 1.0 / max(0.5, self._fps_camara_objetivo())
        for nombre, datos in self._integrador.tick(ahora):
            self._publicar(nombre, datos)
        if self._cam_abierta and ahora - self._latido_ts >= INTERVALO_LATIDO_CAMARA_S - 1e-6:
            self._latido_ts = ahora
            self._publicar("vision.camara", self._carga_camara(True, int((ahora - self._cam_abierta_ts) * 1000)))
        self._publicar_estado()

    def _publicar_estado(self) -> None:
        """``vision.estado`` solo si cambió (la atención se compara con un decimal)."""
        datos = self.estado().a_dict()
        clave = dict(datos, atencion=round(datos["atencion"], 1))
        if clave != self._estado_pub:
            self._estado_pub = clave
            self._publicar("vision.estado", datos)

    async def _bucle_integrador(self) -> None:
        while not self._cerrado:
            try:
                self._tick(self._reloj())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Iteración del integrador de visión fallida (%s)", type(exc).__name__)
            await asyncio.sleep(PERIODO_INTEGRADOR_S)

    async def _bucle_entorno(self) -> None:
        while not self._cerrado:
            try:
                datos = await self._leer_entorno_async()
                if not self._cerrado:
                    self._aplicar_entorno(self._reloj(), datos)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Iteración del entorno de visión fallida (%s)", type(exc).__name__)
            await asyncio.sleep(INTERVALO_ENTORNO_S)

    # ------------------------------------------------------------------
    # Diagnóstico
    # ------------------------------------------------------------------

    def _diagnostico_camara(self) -> DiagnosticoModulo:
        nombre = "Visión continua (cámara)"
        if not bool(getattr(self.settings, "camara_habilitada", False)):
            return DiagnosticoModulo("vision_camara", nombre, EstadoModulo.OFF, "cámara deshabilitada (RAMS_CAMERA=0)")
        if not bool(getattr(self.settings, "vision_continua", False)):
            return DiagnosticoModulo("vision_camara", nombre, EstadoModulo.OFF, "desactivada (RAMS_VISION_CONTINUA=0)")
        if np is None:
            return DiagnosticoModulo("vision_camara", nombre, EstadoModulo.OFF, "numpy no instalado")
        if cv2 is None and self._fabrica_captura is None:
            return DiagnosticoModulo("vision_camara", nombre, EstadoModulo.OFF, "OpenCV no instalado")
        detector = self._detector_tipo
        fps = _fmt_fps(self._fps_camara_objetivo())
        if detector is None:
            return DiagnosticoModulo("vision_camara", nombre, EstadoModulo.DEGRADADO,
                                     f"sin detector de rostros: solo movimiento y luz a {fps} fps")
        return DiagnosticoModulo("vision_camara", nombre, EstadoModulo.OK,
                                 f"{'YuNet' if detector == 'yunet' else 'Haar'} a {fps} fps (solo escalares)")

    def _diagnostico_pantalla(self) -> DiagnosticoModulo:
        nombre = "Visión continua (pantalla)"
        modo = self._modo_pantalla
        if modo == "off":
            return DiagnosticoModulo("vision_pantalla", nombre, EstadoModulo.OFF, "RAMS_PANTALLA_MODO=off")
        fps = _fmt_fps(self._fps_pantalla_objetivo())
        if modo == "pixeles":
            if self._pixeles_posibles():
                return DiagnosticoModulo("vision_pantalla", nombre, EstadoModulo.OK,
                                         f"píxeles a {fps} fps (64×36 en memoria, sin guardar)")
            if np is None:
                motivo = "numpy no instalado"
            elif self._plataforma == "hypr":
                motivo = "grim no instalado"
            elif self._plataforma == "win":
                motivo = "mss no instalado"
            elif self._plataforma == "x11":
                motivo = "X11 sin metadatos de ventanas (sin ellos no hay píxeles)"
            else:
                motivo = "sin captura en este sistema"
            if self._metadatos_posibles():
                return DiagnosticoModulo("vision_pantalla", nombre, EstadoModulo.DEGRADADO,
                                         f"{motivo}: solo metadatos")
            return DiagnosticoModulo("vision_pantalla", nombre, EstadoModulo.OFF, motivo)
        if self._metadatos_posibles():
            return DiagnosticoModulo("vision_pantalla", nombre, EstadoModulo.OK,
                                     f"metadatos de la ventana activa a {fps} fps")
        return DiagnosticoModulo("vision_pantalla", nombre, EstadoModulo.OFF,
                                 "sin metadatos de ventanas en este sistema")

    # ------------------------------------------------------------------
    # API pública (§C2.9)
    # ------------------------------------------------------------------

    async def iniciar(self) -> list[DiagnosticoModulo]:
        """Diagnósticos ``vision_camara`` y ``vision_pantalla``; arranca integrador, cámara y pantalla.

        No espera a que la cámara abra (eso ocurre en su hilo): vuelve enseguida. Nunca lanza.
        """
        try:
            self._loop = asyncio.get_running_loop()
            diagnosticos = [self._diagnostico_camara(), self._diagnostico_pantalla()]
            if not self._cerrado and not self._iniciado:
                self._iniciado = True
                self._ejecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rams-vision")
                self._tareas = [
                    asyncio.create_task(self._bucle_integrador(), name="rams-vision-integrador"),
                    asyncio.create_task(self._bucle_entorno(), name="rams-vision-entorno"),
                ]
                self._tick(self._reloj())
            for diagnostico in diagnosticos:
                log.info("Diagnóstico %s: %s%s", diagnostico.clave, diagnostico.estado.value,
                         f" ({diagnostico.detalle})" if diagnostico.detalle else "")
            return diagnosticos
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No se pudo iniciar la visión continua (%s)", type(exc).__name__)
            return [
                DiagnosticoModulo("vision_camara", "Visión continua (cámara)", EstadoModulo.DEGRADADO,
                                  "error al iniciar"),
                DiagnosticoModulo("vision_pantalla", "Visión continua (pantalla)", EstadoModulo.DEGRADADO,
                                  "error al iniciar"),
            ]

    async def cerrar(self) -> None:
        """Señala los hilos, cancela las tareas y espera (≤ 2 s por hilo, fuera del loop)."""
        if self._cerrado:
            return
        self._cerrado = True
        try:
            self._detener_camara()
            self._soltar_cuadro()
            self._detener_pantalla()
            tareas = [t for t in (*self._tareas, self._tarea_pant) if t is not None and not t.done()]
            for tarea in tareas:
                tarea.cancel()
            if tareas:
                await asyncio.wait(tareas, timeout=2.0)
            for proc in list(self._procesos):
                _matar(proc)
            for hilo in (self._hilo_cam, self._hilo_pant):
                if hilo is not None and hilo.is_alive():
                    await asyncio.to_thread(hilo.join, JOIN_HILO_S)
                    if hilo.is_alive():
                        log.warning("El hilo %s no terminó en %s s; sigue como daemon", hilo.name,
                                    _fmt_fps(JOIN_HILO_S))
            await asyncio.sleep(0)       # deja correr los avisos pendientes de los hilos (LED apagado)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Error al cerrar la visión continua (%s)", type(exc).__name__)
        finally:
            if self._ejecutor is not None:
                self._ejecutor.shutdown(wait=False, cancel_futures=True)
            log.debug("Visión continua cerrada")

    def set_camara(self, activa: bool) -> str:
        """Interruptor en tiempo de ejecución (``/vision on|off``, ``/camara off``, HUD). Devuelve el estado.

        Respeta el opt-in (``RAMS_CAMERA`` y ``RAMS_VISION_CONTINUA``): no puede encenderla si el
        maestro está apagado. Apagar solo señala el hilo, que libera la cámara.
        """
        try:
            if activa:
                if not self._permitida():
                    log.info("RAMS_CAMERA=0 o RAMS_VISION_CONTINUA=0 impiden activar la visión continua")
                self._camara_runtime = True
                self._pausa_hasta = None
                self._error_cam = ""
                self._fallos_apertura = 0
                self._reintento_ts = -math.inf
                self._programar_tick()
            else:
                self._camara_runtime = False
                self._detener_camara()
                self._soltar_cuadro()
                self._programar_tick()
            return self._estado_camara(self._reloj())
        except Exception as exc:
            log.warning("set_camara falló (%s)", type(exc).__name__)
            return "error"

    def set_pantalla(self, modo: str) -> str:
        """``off``/``metadatos``/``pixeles`` (``pixeles`` es un permiso aparte). Devuelve el modo efectivo."""
        try:
            nuevo = str(modo or "").strip().lower()
            if nuevo not in MODOS_PANTALLA:
                log.info("Modo de pantalla no válido: %r", str(modo)[:20])
            else:
                if nuevo != self._modo_pantalla:
                    log.info("Visión de pantalla: %s → %s", self._modo_pantalla, nuevo)
                self._modo_pantalla = nuevo
                if nuevo == "off":
                    self._detener_pantalla()
                self._pant_modo_actual = self._pantalla_efectiva(self._reloj())
                self._programar_tick()
            return self._pantalla_efectiva(self._reloj())
        except Exception as exc:
            log.warning("set_pantalla falló (%s)", type(exc).__name__)
            return "off"

    def pausar(self, segundos: float | None = None, motivo: str = "usuario") -> None:
        """Pausa cámara y pantalla (``None`` = hasta ``reanudar``). Solo señala los hilos: vuelve en µs."""
        try:
            ahora = self._reloj()
            duracion = None
            if segundos is not None:
                with contextlib.suppress(TypeError, ValueError):
                    duracion = float(segundos)
            if duracion is None or not math.isfinite(duracion) or duracion <= 0:
                self._pausa_hasta = math.inf
            else:
                self._pausa_hasta = ahora + duracion
            self._pausa_motivo = str(motivo or "usuario")[:32]
            self._pant_modo_actual = "pausada"
            self._detener_camara()
            self._soltar_cuadro()
            self._detener_pantalla()
            self._programar_tick()
        except Exception as exc:  # defensa: nunca lanza
            log.debug("pausar falló (%s)", type(exc).__name__)

    def reanudar(self) -> None:
        """Quita la pausa del usuario (la autopausa sigue mandando mientras dure su motivo)."""
        try:
            self._pausa_hasta = None
            self._pausa_motivo = ""
            self._programar_tick()
        except Exception as exc:
            log.debug("reanudar falló (%s)", type(exc).__name__)

    def estado(self) -> EstadoVision:
        """Estado actual (lo mismo que publica ``vision.estado``)."""
        try:
            ahora = self._reloj()
            pantalla = self._pantalla_efectiva(ahora)
            return EstadoVision(
                camara=self._estado_camara(ahora),
                pantalla=pantalla,
                fps_camara=round(self._fps_camara_objetivo(), 2) if self._cam_abierta else 0.0,
                fps_pantalla=round(self._fps_pantalla_objetivo(), 2) if pantalla in ("metadatos", "pixeles") else 0.0,
                nivel=3 if self._autopausa else int(self._nivel),
                detector=self._detector_tipo,
                presente=self._integrador.presente,
                atencion=round(float(self._integrador.atencion), 3),
            )
        except Exception as exc:
            log.debug("estado() falló (%s)", type(exc).__name__)
            return EstadoVision("error", "off", 0.0, 0.0, 0, None, None, 0.0)

    def pausada_por_usuario(self) -> bool:
        """``True`` mientras dura la pausa que pidió el usuario ("deja de mirarme", ``pausar``).

        ``estado().camara == "pausada"`` no basta para saberlo: también lo da la autopausa
        (sesión bloqueada, usuario ausente, juego, batería, llamada), que no es una orden del
        usuario. Una foto que el usuario pide ahora (p. ej. por Telegram) solo debe negarse por
        esta pausa (y por la llamada, que ``como_vistazo`` ya da como ``ocupada``). Nunca lanza.
        """
        try:
            return self._pausa_usuario_activa(self._reloj())
        except Exception as exc:
            log.debug("pausada_por_usuario falló (%s)", type(exc).__name__)
            return True              # ante la duda, se respeta la privacidad

    def calibrar_atencion(self) -> None:
        """Línea base de "mira la pantalla" (main la llama en cada turno local: el usuario teclea)."""
        try:
            self._integrador.calibrar(self._reloj())
        except Exception as exc:
            log.debug("calibrar_atencion falló (%s)", type(exc).__name__)

    def como_vistazo(self, motivo: str) -> ResultadoVistazo | None:
        """Un "vistazo" servido desde la cámara continua, sin abrir nada ni contar cupo.

        - App de llamada en primer plano (o hace < 5 min) ⇒ ``error="ocupada"``: la cámara es
          de la llamada y aura no debe abrirla, tampoco a petición del usuario.
        - Pausa del usuario ("deja de mirarme") o autopausa por sesión bloqueada o juego ⇒
          ``error="denegada"`` para los vistazos que no pidió el usuario (curiosidad,
          presencia). Un ``motivo == "usuario"`` (lo pide ahora, explícitamente) sigue.
        - Hilo de cámara vivo pero sin muestra reciente (abriendo, calentando o liberando) ⇒
          ``error="ocupada"``: el dispositivo lo tiene este hilo y nadie más debe abrirlo.
        - Abierta con una muestra reciente (< 1 s, o 1,5 periodos si el ritmo es menor) ⇒
          ``ResultadoVistazo`` de esa muestra (``rostro=None`` si no hay detector).
        - Sin hilo de cámara y sin veto ⇒ ``None`` (aura puede parpadear como en la v0.1).
        """
        try:
            ahora = self._reloj()
            texto = str(motivo or "")
            veto = self._veto_vistazo(texto, ahora)
            if veto is not None:
                return ResultadoVistazo(None, motivo=texto, error=veto)
            if not self._cam_abierta:
                hilo = self._hilo_cam
                if hilo is not None and hilo.is_alive():   # abriendo o liberando: el dispositivo es suyo
                    return ResultadoVistazo(None, motivo=texto, error="ocupada")
                return None
            m = self._ultima_cam
            frescura = max(FRESCURA_VISTAZO_S, 1.5 / max(0.5, self._fps_camara_objetivo()))
            if m is None or ahora - m.ts > frescura:
                return ResultadoVistazo(None, motivo=texto, error="ocupada")
            if self._detector_tipo is None:
                return ResultadoVistazo(None, n_rostros=0, duracion_ms=0, brillo=m.brillo, motivo=texto)
            return ResultadoVistazo(bool(m.rostro), n_rostros=int(m.n) if m.rostro else 0, duracion_ms=0,
                                    brillo=m.brillo, motivo=texto)
        except Exception as exc:
            log.debug("como_vistazo falló (%s)", type(exc).__name__)
            return None

    def _instantanea_permitida(self) -> bool:
        """Cámara continua abierta y sin pausa (del usuario ni autopausa por llamada, bloqueo…)."""
        if self._cerrado or not self._cam_abierta:
            return False
        return self._estado_camara(self._reloj()) in ("continua", "degradada")

    async def instantanea_jpeg(self, *, calidad: int = 85, max_lado: int = 1600) -> bytes | None:
        """JPEG del último cuadro de la cámara continua (solo en memoria), o ``None``.

        Solo si la cámara continua está abierta, no está en pausa (del usuario o autopausa) y el
        cuadro es reciente (< 2 s, o 2,5 periodos si el ritmo es menor). La codificación va al
        executor (``lobes.capturas.codificar_jpeg``) y el resultado se descarta si mientras tanto
        llegó una pausa. No abre ningún dispositivo ni publica nada. Nunca lanza.
        """
        try:
            if not self._instantanea_permitida():
                return None
            with self._cerrojo_cuadro:
                cuadro, ts, gen = self._cuadro, self._cuadro_ts, self._cuadro_gen
            frescura = max(FRESCURA_INSTANTANEA_S, 2.5 / max(0.5, self._fps_camara_objetivo()))
            if cuadro is None or gen != self._gen_cam or time.monotonic() - ts > frescura:
                return None
            from lobes.capturas import codificar_jpeg  # diferido: capturas importa este módulo

            loop = asyncio.get_running_loop()
            ejecutor = self._ejecutor if self._ejecutor is not None and not self._cerrado else None
            tarea = loop.run_in_executor(ejecutor, functools.partial(
                codificar_jpeg, cuadro, calidad=calidad, max_lado=max_lado))
            del cuadro
            resultado = await asyncio.wait_for(tarea, TIMEOUT_INSTANTANEA_S)
            if not resultado or not self._instantanea_permitida():
                return None
            return bytes(resultado[0])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("instantanea_jpeg falló (%s)", type(exc).__name__)
            return None

    def _veto_vistazo(self, motivo: str, ahora: float) -> str | None:
        """Error con el que ``como_vistazo`` impide que aura abra la cámara, o ``None``."""
        if ahora < self._llamada_hasta:
            return "ocupada"
        if motivo == "usuario":
            return None
        if (self._pausa_usuario_activa(ahora) or bool(self._entorno.get("bloqueada"))
                or bool(self._entorno.get("juego"))):
            return "denegada"
        return None

    def set_caja_avatar(self, caja: tuple[int, int, int, int] | None) -> None:
        """Caja del avatar en píxeles físicos globales (máscara de la pantalla); ``None`` la quita."""
        try:
            if caja is None:
                self._caja = None
                return
            x, y, w, h = (int(c) for c in caja)
            if w <= 0 or h <= 0:
                log.debug("Caja del avatar ignorada (tamaño no válido)")
                return
            self._caja = (x, y, w, h)
        except (TypeError, ValueError) as exc:
            log.debug("Caja del avatar ignorada (%s)", type(exc).__name__)
