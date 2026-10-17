"""Presencia del usuario (presente / ausente) de R.A.M.S. BIO-CORE (§7, §10.11).

Qué hace:
- ``leer_inactividad(settings)`` lee la inactividad del sistema: en Linux con el archivo-bandera
  que crea hypridle (``RAMS_IDLE_FLAG``) y, si no existe, comprobando con psutil que hypridle
  esté corriendo; en Windows con ``GetLastInputInfo`` (ctypes). En otros sistemas no hay fuente.
- ``RastreadorPresencia`` fusiona la última entrada local (terminal o chat de escritorio), el
  último rostro (vistazo de cámara o visión continua, que registra main) y la inactividad del
  sistema con la tabla de 7 reglas de §7. Es lógica pura y determinista: **no** abre la cámara
  ni hace E/S salvo la lectura de inactividad.
- v0.2 (``docs/COMPANION.md`` §C3 "Grupo orquestacion"): el canal ``escritorio`` cuenta como
  entrada local; la inactividad de Windows puede venir de ``ControlPC.inactividad_s`` (callable
  inyectado) y un reinicio de la inactividad a ±1 s de ``ControlPC.ts_ultima_inyeccion()`` (el
  cursor o el teclado los movió Amadeus, no el usuario) se ignora. Un rostro visto hace < 60 s
  (si fue el último registro) gana a la inactividad: main lo refresca cada 30 s mientras la
  visión continua ve al usuario, y quien mira un vídeo sin tocar nada no está ausente.

Dependencias opcionales: ``psutil`` (detección del proceso hypridle; sin él no hay esa
comprobación). ``ctypes`` es de la biblioteca estándar y solo se usa en Windows.

Degradación: sin fuente de inactividad se aplican las reglas 6–7 (confianza baja, ``ambiguo``).
Ninguna función de este módulo lanza excepciones por fallos del sistema operativo.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Final

from core.config import Settings
from core.contracts import CANALES_LOCALES, EstadoPresencia

try:  # opcional: detección del proceso hypridle
    import psutil
except ImportError:  # sin psutil: no se comprueba si hypridle corre
    psutil = None

__all__ = [
    "leer_inactividad",
    "RastreadorPresencia",
    "VENTANA_TERMINAL_RECIENTE_S",
    "VENTANA_ROSTRO_S",
    "INACTIVIDAD_MAX_CON_ROSTRO_S",
    "VENTANA_ROSTRO_FRESCO_S",
    "INACTIVIDAD_MIN_SIN_ROSTRO_S",
    "UMBRAL_AMBIGUO",
    "CACHE_HYPRIDLE_S",
]

log = logging.getLogger("rams.presencia")

# --- Umbrales de la tabla de decisión (§7) ---
VENTANA_TERMINAL_RECIENTE_S: Final = 120.0     # regla 1
VENTANA_ROSTRO_S: Final = 300.0                # reglas 2 y 3
INACTIVIDAD_MAX_CON_ROSTRO_S: Final = 600.0    # regla 2
# Regla 2 (v0.2): un rostro visto hace < 60 s, si fue el último registro, gana a la inactividad.
# La visión continua (main) lo refresca cada 30 s mientras ve al usuario: ver un vídeo largo sin
# tocar el teclado no es estar ausente (si no, la regla 5 mandaría las iniciativas a Telegram).
VENTANA_ROSTRO_FRESCO_S: Final = 60.0
INACTIVIDAD_MIN_SIN_ROSTRO_S: Final = 180.0    # regla 3
UMBRAL_AMBIGUO: Final = 0.6                    # ambiguo = confianza < 0,6
CACHE_HYPRIDLE_S: Final = 30.0                 # caché de la comprobación del proceso hypridle
ESPERA_PRIMER_RECORRIDO_S: Final = 0.25        # espera máxima del primer recorrido (sin caché)
TOLERANCIA_INYECCION_S: Final = 1.0            # ±1 s alrededor de una entrada inyectada por Amadeus
UMBRAL_REINICIO_S: Final = 0.5                 # un "último input" que avanza más que esto es un input nuevo

_FUENTES_VALIDAS: Final = frozenset({"hypridle", "win32", "ninguna"})

# Caché de la comprobación "¿corre hypridle?": (instante monotónico, resultado).
_cache_hypridle: tuple[float, bool] | None = None
_cache_lock = threading.Lock()
# Recorrido de procesos en curso (uno a la vez), fuera del event loop (§3.1).
_hilo_hypridle: threading.Thread | None = None


class _LASTINPUTINFO(ctypes.Structure):
    """Estructura ``LASTINPUTINFO`` de Win32 (``cbSize``, ``dwTime``)."""

    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _reiniciar_cache_hypridle() -> None:
    """Olvida el resultado cacheado de la detección de hypridle (lo usan los tests).

    Espera (acotado) a un recorrido en curso para que no escriba un resultado viejo después.
    """
    global _cache_hypridle
    with _cache_lock:
        hilo = _hilo_hypridle
    if hilo is not None and hilo.is_alive():
        hilo.join(1.0)
    with _cache_lock:
        _cache_hypridle = None


def _buscar_hypridle() -> bool:
    """Recorre los procesos con psutil buscando ``hypridle`` (sin caché)."""
    if psutil is None:
        return False
    try:
        for proceso in psutil.process_iter(["name"]):
            try:
                nombre = (proceso.info.get("name") or "").lower()
            except (AttributeError, TypeError):
                continue
            # Cubre también envoltorios como ".hypridle-wrapped" (NixOS).
            if nombre == "hypridle" or "hypridle" in nombre:
                return True
    except Exception as exc:  # psutil.Error u OSError al enumerar /proc
        log.debug("No se pudo enumerar los procesos para buscar hypridle: %s", type(exc).__name__)
    return False


def _recorrer_y_cachear() -> None:
    """Cuerpo del hilo de recorrido: busca hypridle y guarda el resultado en la caché."""
    global _cache_hypridle
    resultado = _buscar_hypridle()
    with _cache_lock:
        _cache_hypridle = (time.monotonic(), resultado)


def _lanzar_recorrido() -> threading.Thread | None:
    """Lanza el recorrido de procesos en un hilo propio, o devuelve el que ya está en curso.

    CONTRATO: §3.1 pide sacar "psutil sobre todos los procesos" del event loop a un worker propio
    del módulo. Se usa un hilo *daemon* (uno a la vez) en lugar de un ``ThreadPoolExecutor``:
    la API de presencia (§10.11) no tiene ``cerrar()`` y los hilos de un executor se esperan al
    salir del intérprete, así que un recorrido colgado retendría el apagado.
    """
    global _hilo_hypridle
    with _cache_lock:
        hilo = _hilo_hypridle
        if hilo is not None and hilo.is_alive():
            return hilo
        hilo = threading.Thread(target=_recorrer_y_cachear, name="rams-presencia-psutil", daemon=True)
        try:
            hilo.start()
        except RuntimeError as exc:  # sin hilos disponibles: se reintenta en la próxima lectura
            log.debug("No se pudo lanzar la búsqueda de hypridle: %s", type(exc).__name__)
            return None
        _hilo_hypridle = hilo
        return hilo


def _hypridle_corriendo() -> bool:
    """``True`` si hay un proceso hypridle vivo. El resultado se cachea ``CACHE_HYPRIDLE_S`` s.

    Nunca bloquea al llamador (``RastreadorPresencia.actualizar`` corre en el event loop): al
    caducar la caché, el recorrido de procesos se relanza en un hilo propio y mientras tanto se
    devuelve el último resultado. Solo la primera lectura (sin resultado previo) espera, como
    mucho ``ESPERA_PRIMER_RECORRIDO_S``; si el recorrido no terminó, se asume que no corre.
    """
    if psutil is None:
        return False
    ahora = time.monotonic()
    with _cache_lock:
        cache = _cache_hypridle
    if cache is not None and ahora - cache[0] < CACHE_HYPRIDLE_S:
        return cache[1]
    hilo = _lanzar_recorrido()
    if cache is not None:
        return cache[1]          # valor anterior mientras se refresca en segundo plano
    if hilo is not None:
        hilo.join(ESPERA_PRIMER_RECORRIDO_S)
    with _cache_lock:
        cache = _cache_hypridle
    return cache[1] if cache is not None else False


def _inactividad_linux(settings: Settings) -> tuple[float | None, str]:
    """Inactividad en Linux a partir del archivo-bandera de hypridle (§7)."""
    ruta = settings.idle_flag_path
    if ruta is None:
        # Sin ruta no se puede distinguir "activo" de "mal configurado".
        return None, "ninguna"
    try:
        mtime = os.stat(ruta).st_mtime
    except FileNotFoundError:
        mtime = None
    except OSError as exc:
        log.debug("No se pudo leer el archivo de inactividad: %s", type(exc).__name__)
        return None, "ninguna"
    if mtime is not None:
        transcurrido = max(0.0, time.time() - mtime)
        return transcurrido + float(settings.idle_timeout_s), "hypridle"
    if _hypridle_corriendo():
        # hypridle está vivo y aún no disparó su timeout: el usuario está activo.
        return 0.0, "hypridle"
    return None, "ninguna"


def _inactividad_win32() -> tuple[float | None, str]:
    """Inactividad en Windows con ``GetLastInputInfo`` (falla sin sesión de escritorio)."""
    try:
        windll = ctypes.windll  # type: ignore[attr-defined]
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not windll.user32.GetLastInputInfo(ctypes.byref(info)):
            log.debug("GetLastInputInfo devolvió error (¿sin sesión de escritorio?).")
            return None, "ninguna"
        ticks = int(windll.kernel32.GetTickCount())
        milisegundos = (ticks - int(info.dwTime)) & 0xFFFFFFFF
        return milisegundos / 1000.0, "win32"
    except (AttributeError, OSError, ValueError, TypeError) as exc:
        log.debug("Sin lectura de inactividad en Windows: %s", type(exc).__name__)
        return None, "ninguna"


def leer_inactividad(settings: Settings) -> tuple[float | None, str]:
    """Segundos de inactividad del sistema y su fuente (``"hypridle"``, ``"win32"`` o ``"ninguna"``).

    - **Linux:** ``idle_flag_path`` ``None`` ⇒ ``(None, "ninguna")``. Si el archivo existe:
      ``time.time() − mtime + idle_timeout_s``. Si no existe pero hypridle corre (psutil, caché de
      30 s; el recorrido de procesos va en un hilo propio y no bloquea al llamador) ⇒
      ``(0.0, "hypridle")``. En otro caso ``(None, "ninguna")``.
    - **Windows:** ``GetLastInputInfo`` vía ctypes, fuente ``"win32"``.
    - **Otros sistemas:** ``(None, "ninguna")``.

    Nunca lanza excepciones.
    """
    try:
        plataforma = sys.platform
        if plataforma.startswith("linux"):
            return _inactividad_linux(settings)
        if plataforma == "win32":
            return _inactividad_win32()
    except Exception as exc:  # defensa en profundidad: la presencia nunca tumba el sistema
        log.debug("Lectura de inactividad fallida: %s", type(exc).__name__)
    return None, "ninguna"


def _segundos_validos(valor: object) -> float | None:
    """Convierte la lectura de inactividad a ``float ≥ 0`` finito, o ``None`` si no es válida."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        segundos = float(valor)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(segundos):
        return None
    return max(0.0, segundos)


class RastreadorPresencia:
    """Fusión de presencia por reglas (§7). Lógica pura: no abre la cámara.

    ``reloj`` (por defecto ``time.time``) y ``fuente_inactividad`` (por defecto
    ``leer_inactividad(settings)``) se inyectan para poder probar las reglas sin tocar el sistema.

    v0.2 (todos opcionales, también con :meth:`conectar_control_pc`):

    - ``inactividad_pc``: ``ControlPC.inactividad_s`` (Windows: ``GetLastInputInfo``). Si
      devuelve un número válido, manda sobre ``fuente_inactividad`` (fuente ``"win32"``); si
      devuelve ``None`` (Hyprland) se usa la fuente normal (archivo-bandera de hypridle).
    - ``ts_ultima_inyeccion``: ``ControlPC.ts_ultima_inyeccion`` en el reloj ``reloj_inyeccion``
      (por defecto ``time.monotonic``, el de ``ControlPC``). Un reinicio de la inactividad cuyo
      instante cae a ±1 s de la última inyección propia no cuenta como actividad del usuario.
    """

    def __init__(self, settings: Settings, *, reloj: Callable[[], float] = time.time,
                 fuente_inactividad: Callable[[], tuple[float | None, str]] | None = None,
                 inactividad_pc: Callable[[], float | None] | None = None,
                 ts_ultima_inyeccion: Callable[[], float | None] | None = None,
                 reloj_inyeccion: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._reloj = reloj
        if fuente_inactividad is None:
            def _fuente_defecto() -> tuple[float | None, str]:
                return leer_inactividad(settings)
            fuente_inactividad = _fuente_defecto
        self._fuente = fuente_inactividad
        self._inactividad_pc: Callable[[], float | None] | None = inactividad_pc
        self._ts_inyeccion: Callable[[], float | None] | None = ts_ultima_inyeccion
        self._reloj_inyeccion = reloj_inyeccion
        # Instante (en reloj_inyeccion) del último input REAL del usuario según la fuente de
        # inactividad; None hasta la primera lectura. Lo usa el filtro de inyecciones propias.
        self._input_real: float | None = None
        self._lock = threading.Lock()
        self._ultima_entrada_local: float | None = None
        self.ultima_entrada_remota_ts: float | None = None
        self._ultimo_vistazo_ts: float | None = None
        self._ultimo_vistazo_rostro: bool | None = None
        self._ultimo_rostro_ts: float | None = None
        self._estado: EstadoPresencia = EstadoPresencia.desconocido()

    def conectar_control_pc(self, *, inactividad_pc: Callable[[], float | None] | None = None,
                            ts_ultima_inyeccion: Callable[[], float | None] | None = None,
                            reloj_inyeccion: Callable[[], float] | None = None) -> None:
        """Conecta (o desconecta con ``None``) las fuentes de ``ControlPC`` tras construir el rastreador."""
        with self._lock:
            self._inactividad_pc = inactividad_pc
            self._ts_inyeccion = ts_ultima_inyeccion
            if reloj_inyeccion is not None:
                self._reloj_inyeccion = reloj_inyeccion
            self._input_real = None

    # ------------------------------------------------------------------
    # Entradas
    # ------------------------------------------------------------------

    def registrar_entrada(self, canal: str) -> None:
        """Anota una entrada del usuario.

        Solo los canales de ``CANALES_LOCALES`` (``terminal`` y, desde la v0.2, ``escritorio``:
        el chat de Amadeus Desktop o del HUD) cuentan como presencia.
        ``telegram`` fija ``ultima_entrada_remota_ts`` (indica más bien que el usuario está
        lejos). Cualquier otro canal (``nexus``) se ignora.
        """
        canal = str(canal or "").strip().lower()
        ahora = self._ahora()
        with self._lock:
            if canal in CANALES_LOCALES:
                self._ultima_entrada_local = ahora
                return
            if canal == "telegram":
                self.ultima_entrada_remota_ts = ahora
                return
        log.debug("Entrada por el canal «%s»: no cuenta para la presencia.", canal)

    def registrar_rostro(self, presente: bool | None) -> None:
        """Anota el resultado del último vistazo de cámara (``None`` no altera nada)."""
        if presente is None:
            return
        ahora = self._ahora()
        with self._lock:
            self._ultimo_vistazo_ts = ahora
            self._ultimo_vistazo_rostro = bool(presente)
            if presente:
                self._ultimo_rostro_ts = ahora

    # ------------------------------------------------------------------
    # Decisión
    # ------------------------------------------------------------------

    def actualizar(self) -> EstadoPresencia:
        """Recalcula la presencia con la tabla de 7 reglas de §7 (gana la primera que aplique)."""
        ahora = self._ahora()
        inactividad, fuente = self._leer_fuente()
        umbral_ausencia = max(0.0, float(self._settings.ausencia_min)) * 60.0

        with self._lock:
            ultima_entrada = self._ultima_entrada_local
            ultimo_vistazo = self._ultimo_vistazo_ts
            vistazo_con_rostro = self._ultimo_vistazo_rostro
            ultimo_rostro = self._ultimo_rostro_ts
            anterior = self._estado

        desde_entrada = None if ultima_entrada is None else max(0.0, ahora - ultima_entrada)
        desde_rostro = None if ultimo_rostro is None else max(0.0, ahora - ultimo_rostro)
        desde_vistazo = None if ultimo_vistazo is None else max(0.0, ahora - ultimo_vistazo)

        rostro_fresco = (desde_rostro is not None and desde_rostro < VENTANA_ROSTRO_FRESCO_S
                         and vistazo_con_rostro is True)
        if desde_entrada is not None and desde_entrada < VENTANA_TERMINAL_RECIENTE_S:
            presente, confianza, motivo = True, 0.95, "terminal_reciente"            # regla 1
        elif rostro_fresco or (desde_rostro is not None and desde_rostro < VENTANA_ROSTRO_S
                               and (inactividad is None or inactividad < INACTIVIDAD_MAX_CON_ROSTRO_S)):
            presente, confianza, motivo = True, 0.90, "rostro"                       # regla 2
        elif (desde_vistazo is not None and desde_vistazo < VENTANA_ROSTRO_S
              and vistazo_con_rostro is False
              and (inactividad is None or inactividad >= INACTIVIDAD_MIN_SIN_ROSTRO_S)):
            presente, confianza, motivo = False, 0.85, "sin_rostro"                  # regla 3
        elif inactividad is not None and inactividad < umbral_ausencia:
            presente, confianza, motivo = True, 0.70, "sistema_activo"               # regla 4
        elif inactividad is not None:
            presente, confianza, motivo = False, 0.80, "inactividad"                 # regla 5
        elif desde_entrada is not None and desde_entrada < umbral_ausencia:
            presente, confianza, motivo = True, 0.50, "sin_datos"                    # regla 6
        else:
            presente, confianza, motivo = False, 0.40, "sin_datos"                   # regla 7

        estado = EstadoPresencia(
            presente=presente,
            confianza=confianza,
            motivo=motivo,
            ambiguo=confianza < UMBRAL_AMBIGUO,
            segundos_inactivo=inactividad,
            fuente_inactividad=fuente,
            ultima_entrada_ts=ultima_entrada,
            ultimo_rostro_ts=ultimo_rostro,
            ts=ahora,
        )
        with self._lock:
            self._estado = estado
        if anterior.presente != estado.presente or anterior.motivo != estado.motivo:
            log.debug("Presencia: %s (%s, confianza %.2f).",
                      "presente" if presente else "ausente", motivo, confianza)
        return estado

    def estado(self) -> EstadoPresencia:
        """El último estado calculado (``EstadoPresencia.desconocido()`` antes del primer cálculo)."""
        with self._lock:
            return self._estado

    def segundos_desde_entrada_local(self) -> float | None:
        """Segundos desde la última entrada local, o ``None`` si aún no hubo ninguna."""
        with self._lock:
            ultima = self._ultima_entrada_local
        if ultima is None:
            return None
        return max(0.0, self._ahora() - ultima)

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _ahora(self) -> float:
        """Marca de tiempo del reloj inyectado (``time.time()`` si el reloj falla)."""
        try:
            valor = float(self._reloj())
        except Exception as exc:  # un reloj de pruebas defectuoso nunca tumba la presencia
            log.debug("Reloj de presencia inválido (%s); uso time.time().", type(exc).__name__)
            return time.time()
        return valor if math.isfinite(valor) else time.time()

    def _leer_fuente(self) -> tuple[float | None, str]:
        """Lee y sanea la fuente de inactividad; ante cualquier fallo, ``(None, "ninguna")``.

        Con ``inactividad_pc`` conectada (ControlPC en Windows) se usa esa lectura; después se
        aplica el filtro de inyecciones propias (:meth:`_filtrar_inyeccion`).
        """
        segundos: float | None = None
        fuente = "ninguna"
        externa = self._inactividad_pc
        if externa is not None:
            try:
                segundos = _segundos_validos(externa())
            except Exception as exc:
                log.debug("Inactividad de ControlPC fallida: %s", type(exc).__name__)
                segundos = None
            if segundos is not None:
                fuente = "win32"
        if segundos is None:
            try:
                resultado = self._fuente()
                segundos_crudos, fuente_cruda = resultado
            except Exception as exc:
                log.debug("Fuente de inactividad fallida: %s", type(exc).__name__)
                return None, "ninguna"
            segundos = _segundos_validos(segundos_crudos)
            if segundos is None:
                return None, "ninguna"
            fuente = str(fuente_cruda) if str(fuente_cruda) in _FUENTES_VALIDAS else "ninguna"
        return self._filtrar_inyeccion(segundos), fuente

    def _filtrar_inyeccion(self, segundos: float) -> float:
        """Ignora los reinicios de inactividad causados por Amadeus (travesura o tecleo).

        Se lleva el instante del último input real (``reloj_inyeccion() − segundos``). Si una
        lectura indica un input nuevo (el instante avanza más de ``UMBRAL_REINICIO_S``) y ese
        instante cae a ±``TOLERANCIA_INYECCION_S`` de ``ts_ultima_inyeccion()``, se ignora y la
        inactividad sigue contando desde el input real anterior. Sin ``ts_ultima_inyeccion``
        devuelve ``segundos`` tal cual. Nunca lanza.
        """
        fuente_ts = self._ts_inyeccion
        if fuente_ts is None:
            return segundos
        try:
            ahora = float(self._reloj_inyeccion())
            crudo = fuente_ts()
            inyeccion = None if crudo is None or isinstance(crudo, bool) else float(crudo)
        except Exception as exc:
            log.debug("Filtro de inyecciones no disponible (%s)", type(exc).__name__)
            return segundos
        if not math.isfinite(ahora):
            return segundos
        momento = ahora - segundos
        with self._lock:
            previo = self._input_real
            if previo is None or momento <= previo + UMBRAL_REINICIO_S:
                # Primera lectura o sin input nuevo: se adopta la lectura (corrige derivas).
                if previo is None or momento > previo:
                    self._input_real = momento
                return segundos if previo is None else max(segundos, ahora - self._input_real)
            if (inyeccion is not None and math.isfinite(inyeccion)
                    and abs(momento - inyeccion) <= TOLERANCIA_INYECCION_S):
                log.debug("Reinicio de inactividad causado por una inyección propia; se ignora")
                return max(0.0, ahora - previo)
            self._input_real = momento
            return segundos
