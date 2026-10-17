"""Enlace WebSocket ``/escritorio`` entre R.A.M.S. y Amadeus Desktop o el chat del HUD (§C2.4).

Qué hace (``docs/COMPANION.md`` §C1.5, §C2.3 y §C2.4):
- Vive dentro del mismo servidor que el HUD (``interface/hud_server.py`` lo compone): mismo puerto,
  ``127.0.0.1`` y nunca ``0.0.0.0``.
- **Token propio** (``secrets.token_urlsafe(16)``, distinto del del HUD y nuevo en cada arranque),
  escrito de forma atómica (temporal en el mismo directorio + ``os.replace``) y con 0o600 en
  ``settings.escritorio_token_file`` como ``{"url", "token", "pid"}``. Al cerrar se borra solo si
  el archivo sigue teniendo el token y el pid propios. El token nunca va a logs, ``repr``,
  diagnósticos ni al ``hola``.
- **Handshake:** ``?t=<token>`` comparado con ``hmac.compare_digest``. Sin ``Origin`` = Amadeus
  Desktop (el ``WebSocket`` global de Node no lo envía; uno nuevo **reemplaza** al anterior, que
  se cierra con 4001). Con ``Origin`` del HUD (``http://127.0.0.1|localhost:<puerto>``, solo con
  ``hud_chat``) = panel de chat del HUD, hasta 2 clientes. Cualquier otro caso ⇒ 403.
- **Entrantes** (≤ 8 KiB, JSON inválido o tipo desconocido ⇒ se ignora con ``log.debug``; ritmo
  por cliente **y por clase**, §C2.4.5: ``chat`` y lo no reconocido ≤ 5/s; ``caja`` y
  ``evento{toque, soltada, cursor_encima}`` ≤ 5/s; ``hola_app``, ``control``, ``confirmacion`` y
  ``evento{mostrada, ocultada, chat_abierto}`` ≤ 20/s con ``log.info`` al descartar, y de
  ``mostrada``/``ocultada`` nunca se pierde la última: se publica al vencer la ventana).
  ``hola_app`` → ``escritorio.app``; ``chat`` → ``escritorio.chat`` (**nunca**
  ``usuario.entrada``: el texto solo lo consume main y nadie lo registra); ``evento`` →
  ``escritorio.evento``; ``control`` → ``escritorio.control`` (``silenciar`` → ``hud.control``);
  ``confirmacion`` → ``escritorio.confirmacion``; ``caja`` → ``escritorio.caja`` (≤ 2 Hz y solo si
  cambia). ``hola_app``, ``evento`` y ``caja`` solo se aceptan del cliente sin ``Origin``. Publica
  ``escritorio.conectado`` al conectar y al desconectar.
- **Salientes:** ``hola``, ``subtitulo``, ``voz``, ``permiso`` (con ``vista_previa``), ``sistema``,
  ``travesuras`` y ``no_molestar`` (de ``pc.no_molestar``: ``{activo, motivo, usuario}``) a todos;
  ``orden``, ``estado`` (≤ 1 Hz) y ``vision`` (``{camara: bool}`` = OR de ``vision.camara`` por
  ``fuente``, solo al cambiar: el ojo del overlay, §C2.13) solo a Amadeus Desktop. Al conectar,
  cada cliente recibe ``hola`` y la instantánea: permiso pendiente, travesuras, ``no_molestar``
  (siempre; sin ``pc.no_molestar`` previo = todo ``false``) y, para Electron, ``vision``
  (siempre) y el último ``estado``.

Reglas del bus (§C2.3): nadie se suscribe a ``"*"`` ni a ``"escritorio.*"``; este módulo publica
``escritorio.chat`` pero no lo escucha ni lo registra.

Dependencias opcionales: ``websockets`` (≥ 14, API ``websockets.asyncio``). Sin ella el módulo se
importa igual y el servidor del HUD queda en ``OFF``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from core.bus import BusEventos, Evento
from core.config import Settings
from core.contracts import VERSION, DiagnosticoModulo, EstadoModulo

try:  # contratos v0.2 (grupo base); respaldo literal de §C2.2 por si faltaran
    from core.contracts import ACCIONES_ORDEN_ESCRITORIO, CONTROLES_ESCRITORIO, EVENTOS_ESCRITORIO
except ImportError:  # pragma: no cover - solo con un core/contracts.py anterior a la v0.2
    ACCIONES_ORDEN_ESCRITORIO = ("aparecer", "esconderse", "abrir_chat", "notificar")
    EVENTOS_ESCRITORIO = ("toque", "soltada", "cursor_encima", "mostrada", "ocultada", "chat_abierto")
    CONTROLES_ESCRITORIO = ("no_molestar_on", "no_molestar_off", "basta_travesuras", "pausar_vision")

try:  # opcional: sin websockets no hay servidor
    from websockets.asyncio.server import broadcast as _ws_broadcast
    from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
except ImportError:  # sin websockets: el enlace nunca se activa
    _ws_broadcast = None

    class _WsConnectionClosed(Exception):  # type: ignore[no-redef]
        """Sustituto para poder escribir ``except`` sin websockets instalado."""


__all__ = [
    "CODIGO_REEMPLAZADO",
    "EVENTOS_SUSCRITOS_ESCRITORIO",
    "EnlaceEscritorio",
    "RUTA_ESCRITORIO",
    "escribir_archivo_token",
    "borrar_archivo_token_si_propio",
]

log = logging.getLogger("rams.escritorio")

# ---------------------------------------------------------------------------
# Constantes (§C2.4)
# ---------------------------------------------------------------------------

RUTA_ESCRITORIO: Final = "/escritorio"
HOST: Final = "127.0.0.1"  # nunca 0.0.0.0

MAX_MENSAJES_CLIENTE_S: Final = 5       # clase "general": chat y lo no reconocido
MAX_RUIDO_CLIENTE_S: Final = 5          # clase "ruido": caja, toque, soltada, cursor_encima
MAX_ESTADO_CLIENTE_S: Final = 20        # clase "estado": hola_app, control, confirmacion, visibilidad
MAX_BYTES_MENSAJE: Final = 8 * 1024
MAX_TEXTO_CHAT: Final = 2000
MAX_CLIENTES_HUD: Final = 2
MAX_BYTES_DATOS_EVENTO: Final = 1024
MAX_CLAVES_DATOS_EVENTO: Final = 16
MAX_TEXTO_SUBTITULO: Final = 4000
MAX_TEXTO_SISTEMA: Final = 4000
MAX_TEXTO_NOTIFICAR: Final = 300
MAX_URL: Final = 300
MAX_VISTA_PREVIA: Final = 500
MAX_LADO_CAJA: Final = 2000
MAX_COORD_CAJA: Final = 100_000      # píxeles físicos globales: holgura para varios monitores
MAX_PID: Final = 2 ** 32
MAX_BUFFER_CLIENTE: Final = 1024 * 1024  # bytes sin escribir antes de soltar a un cliente lento

INTERVALO_CAJA_S: Final = 0.5            # ≤ 2 Hz
INTERVALO_ESTADO_S: Final = 1.0          # ≤ 1 Hz
INTERVALO_CURSOR_ENCIMA_S: Final = 10.0  # cursor_encima ≤ 1 cada 10 s

CODIGO_REEMPLAZADO: Final = 4001          # cierre del cliente de Electron reemplazado por otro
CODIGO_DEMASIADOS: Final = 1008           # carrera entre handshakes del HUD (política)

ID_CONFIRMACION_RE: Final = re.compile(r"[0-9a-f]{8}")             # se usa con fullmatch
_URL_HTTPS_RE: Final = re.compile(r"https://[^\s\x00-\x1f\x7f<>\"'`\\]+")  # fullmatch
_TIPO_SEGURO_RE: Final = re.compile(r"[a-z_]{1,24}")                # solo para registrar el tipo
_VERSION_SEGURA_RE: Final = re.compile(r"[0-9A-Za-z.+\-]{1,40}")

# Eventos del bus que reenvía /escritorio (nunca "*" ni "escritorio.*", §C2.3 regla a).
EVENTOS_SUSCRITOS_ESCRITORIO: Final = (
    "salida.subtitulo",
    "voz.inicio",
    "voz.fin",
    "escritorio.orden",
    "escritorio.sistema",
    "permiso.solicitado",
    "permiso.resuelto",
    "homeostasis.estado",
    "motor.travesuras",
    "pc.no_molestar",
    "vision.camara",
)

# ``vision.camara.fuente`` (§C2.3): sin ``fuente`` = "vistazo". El ojo del overlay es el OR (§C2.13).
FUENTES_CAMARA: Final = ("vistazo", "continua")

# ``pc.no_molestar.motivo`` (§C2.3); "usuario" = el "no molestar" que el usuario puso a mano.
MOTIVOS_NO_MOLESTAR: Final = ("usuario", "presentacion", "pantalla_completa", "ocupado", "juego", "pantalla", "")
NO_MOLESTAR_INICIAL: Final[dict[str, Any]] = {"activo": False, "motivo": "", "usuario": False}

# Destinos de un mensaje saliente.
_TODOS: Final = "todos"
_APP: Final = "app"

# Clases de ritmo de los entrantes (§C2.4.5): cada una con su propio presupuesto por cliente.
_GENERAL: Final = "general"
_RUIDO: Final = "ruido"
_ESTADO: Final = "estado"
_PRESUPUESTOS: Final[dict[str, int]] = {
    _GENERAL: MAX_MENSAJES_CLIENTE_S,
    _RUIDO: MAX_RUIDO_CLIENTE_S,
    _ESTADO: MAX_ESTADO_CLIENTE_S,
}
_EVENTOS_RUIDO: Final = frozenset({"toque", "soltada", "cursor_encima"})
_EVENTOS_VISIBILIDAD: Final = frozenset({"mostrada", "ocultada"})
_TIPOS_ESTADO: Final = frozenset({"hola_app", "control", "confirmacion"})


# ---------------------------------------------------------------------------
# Saneado (los payloads del bus y de los clientes se tratan con cautela)
# ---------------------------------------------------------------------------


def _texto(valor: Any, maximo: int) -> str:
    """Texto recortado a ``maximo`` caracteres (con «…»); ``None`` ⇒ ``""``."""
    if valor is None:
        return ""
    texto = valor if isinstance(valor, str) else str(valor)
    if len(texto) > maximo:
        return texto[: max(0, maximo - 1)] + "…"
    return texto


def _real(valor: Any, defecto: float | None = None) -> float | None:
    """Número real finito (``bool`` no cuenta) o ``defecto``."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return defecto
    numero = float(valor)
    return numero if math.isfinite(numero) else defecto


def _acotar(valor: Any, minimo: float, maximo: float, defecto: float) -> float:
    """Real acotado a ``[minimo, maximo]`` (``defecto`` si no es un número finito)."""
    numero = _real(valor)
    if numero is None:
        return defecto
    return min(maximo, max(minimo, numero))


def _es_entero(valor: Any) -> bool:
    """``True`` si es un ``int`` de verdad (``bool`` no cuenta)."""
    return isinstance(valor, int) and not isinstance(valor, bool)


def _url_https_valida(url: Any) -> bool:
    """``True`` si ``url`` es ``https://`` con host, sin espacios ni controles y ≤ ``MAX_URL``."""
    if not isinstance(url, str) or len(url) > MAX_URL or not _URL_HTTPS_RE.fullmatch(url):
        return False
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _por_defecto_json(valor: Any) -> Any:
    """``default`` de ``json.dumps``: tipos no estándar a algo representable."""
    if isinstance(valor, Enum):
        return valor.value
    if isinstance(valor, (set, frozenset, tuple)):
        return list(valor)
    if isinstance(valor, Mapping):
        return dict(valor)
    return str(valor)


def _serializar(tipo: str, datos: Mapping[str, Any]) -> str:
    """Mensaje ``{"tipo", "datos"}`` como JSON estricto y compacto (UTF-8 sin escapar)."""
    return json.dumps({"tipo": tipo, "datos": dict(datos)}, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False, default=_por_defecto_json)


def _datos_evento_validos(datos: Any) -> dict[str, Any] | None:
    """``datos`` de un ``evento`` de Electron: dict de escalares, ≤ 16 claves y ≤ 1 KiB en JSON."""
    if datos is None:
        return {}
    if not isinstance(datos, dict) or len(datos) > MAX_CLAVES_DATOS_EVENTO:
        return None
    limpio: dict[str, Any] = {}
    for clave, valor in datos.items():
        if not isinstance(clave, str) or not 0 < len(clave) <= 40:
            return None
        if valor is None or isinstance(valor, (bool, str)):
            limpio[clave] = valor
        elif isinstance(valor, (int, float)):
            if not math.isfinite(float(valor)):
                return None
            limpio[clave] = valor
        else:
            return None  # listas, dicts y demás: solo escalares
    try:
        tamano = len(json.dumps(limpio, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                     .encode("utf-8"))
    except (TypeError, ValueError):
        return None
    return limpio if tamano <= MAX_BYTES_DATOS_EVENTO else None


def _orden_saneada(datos: Mapping[str, Any]) -> dict[str, Any] | None:
    """Payload de ``escritorio.orden`` saneado según §C2.3 (o ``None`` si no es válido)."""
    accion = datos.get("accion")
    if not isinstance(accion, str) or accion not in ACCIONES_ORDEN_ESCRITORIO:
        return None
    salida: dict[str, Any] = {"accion": accion}
    if accion == "aparecer":
        borde = datos.get("borde")
        if borde in ("left", "right"):
            salida["borde"] = borde
        modo = datos.get("modo")
        if modo in ("entrar", "asomarse"):
            salida["modo"] = modo
    elif accion == "notificar":
        texto = _texto(datos.get("texto"), MAX_TEXTO_NOTIFICAR).strip()
        if not texto:
            return None
        salida["texto"] = texto
        url = datos.get("url")
        if _url_https_valida(url):
            salida["url"] = url
    return salida


# ---------------------------------------------------------------------------
# Archivo del token (§C2.4.3): escritura atómica y borrado seguro
# ---------------------------------------------------------------------------


def _reemplazar(origen: str, destino: Path, intentos: int = 3) -> None:
    """``os.replace`` con reintentos breves: en Windows falla si otro proceso tiene el destino abierto
    en ese instante (p. ej. Amadeus Desktop leyendo el token). Bloqueante: solo dentro de un hilo."""
    for intento in range(intentos):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if intento == intentos - 1:
                raise
            time.sleep(0.05)


def escribir_archivo_token(ruta: Path, contenido: Mapping[str, Any]) -> None:
    """Escribe ``contenido`` en ``ruta`` de forma atómica y privada (0o600). Bloqueante: usar en un hilo.

    Crea un temporal en el **mismo** directorio (``mkstemp``: 0o600 en POSIX), lo vuelca, hace
    ``fsync`` y lo mueve con ``os.replace`` (atómico también en Windows). Si falla, borra el
    temporal y relanza ``OSError``.
    """
    ruta = Path(ruta)
    ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    datos = json.dumps(dict(contenido), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd, temporal = tempfile.mkstemp(prefix=".escritorio-", suffix=".tmp", dir=ruta.parent)
    try:
        with os.fdopen(fd, "wb") as archivo:
            archivo.write(datos)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.chmod(temporal, 0o600)
        _reemplazar(temporal, ruta)
    except BaseException:
        try:
            os.unlink(temporal)
        except OSError as exc:
            log.debug("No se pudo borrar el temporal del token de escritorio: %s", type(exc).__name__)
        raise
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:  # p. ej. un sistema de archivos sin permisos POSIX
        log.debug("No se pudo fijar 0o600 en el token de escritorio: %s", type(exc).__name__)


def borrar_archivo_token_si_propio(ruta: Path, token: str, pid: int) -> bool:
    """Borra ``ruta`` solo si su contenido tiene **este** token y **este** pid. Bloqueante.

    Devuelve ``True`` si lo borró. Un archivo ajeno (otra instancia de R.A.M.S. lo reescribió),
    ilegible o demasiado grande se deja como está.
    """
    ruta = Path(ruta)
    try:
        if ruta.stat().st_size > 64 * 1024:
            return False
        crudo = ruta.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.debug("No se pudo leer el token de escritorio al cerrar: %s", type(exc).__name__)
        return False
    try:
        datos = json.loads(crudo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(datos, dict):
        return False
    guardado = datos.get("token")
    if not isinstance(guardado, str) or not _es_entero(datos.get("pid")) or datos.get("pid") != pid:
        return False
    if not hmac.compare_digest(guardado.encode("utf-8"), token.encode("utf-8")):
        return False
    try:
        ruta.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("No se pudo borrar el token de escritorio: %s", type(exc).__name__)
        return False
    return True


# ---------------------------------------------------------------------------
# Estado por conexión y límites con coalescencia
# ---------------------------------------------------------------------------


@dataclass
class _Ventana:
    """Ventana de 1 s de una clase de ritmo (§C2.4.5)."""

    inicio: float = -math.inf
    n: int = 0
    avisado: bool = False              # ya se registró un descarte en esta ventana


@dataclass
class _Cliente:
    """Estado de una conexión de ``/escritorio`` (solo se toca desde el hilo del loop)."""

    conexion: Any
    es_app: bool                       # True = Amadeus Desktop (sin Origin); False = chat del HUD
    ventanas: dict[str, _Ventana] = field(default_factory=dict)
    hola_app: bool = False
    cursor_encima_ts: float = -math.inf
    caja_publicada: tuple[int, int, int, int] | None = None
    caja_pendiente: tuple[int, int, int, int] | None = None
    caja_ts: float = -math.inf
    caja_temporizador: asyncio.TimerHandle | None = None
    # mostrada/ocultada que no cupo en su ventana: se publica al vencerla (gana la última).
    visibilidad_pendiente: tuple[str, dict[str, Any]] | None = None
    visibilidad_temporizador: asyncio.TimerHandle | None = None
    descartados: int = 0


@dataclass
class _Limite:
    """Límite de frecuencia con coalescencia: si llega otro dato dentro del intervalo, gana el último."""

    intervalo: float
    ultimo_envio: float = -math.inf
    pendiente: dict[str, Any] | None = None
    temporizador: asyncio.TimerHandle | None = None


@dataclass
class _Rechazos:
    """Registro de rechazos sin inundar el log (una línea cada 10 s)."""

    ultimo_log: float = -math.inf
    sin_log: int = 0


# ---------------------------------------------------------------------------
# Enlace
# ---------------------------------------------------------------------------


class EnlaceEscritorio:
    """Endpoint ``/escritorio`` del servidor del HUD (§C2.4). Lo compone :class:`ServidorHUD`.

    ``__init__`` es barato (sin sockets ni archivos) y se suscribe ya al bus para tener el permiso
    pendiente y el estado de las travesuras cuando conecte el primer cliente.
    """

    def __init__(self, settings: Settings, bus: BusEventos, *, evitar_token: str | None = None,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._bus = bus
        self._reloj = reloj
        token = secrets.token_urlsafe(16)
        while evitar_token is not None and hmac.compare_digest(token, evitar_token):
            token = secrets.token_urlsafe(16)  # distinto del token del HUD (§C2.4.3)
        self._token: str = token
        self._pid = os.getpid()

        ruta = getattr(settings, "escritorio_token_file", None)
        self._ruta_token: Path | None = Path(ruta) if ruta else None
        self._puerto: int | None = None
        self._activo = False
        self._archivo_escrito = False
        self._error_archivo: str | None = None

        # Estado observado del bus (el bus puede llamar desde otro hilo).
        self._lock = threading.Lock()
        self._permiso_pendiente: dict[str, Any] | None = None
        self._travesuras: dict[str, Any] | None = None
        self._estado: dict[str, Any] | None = None
        self._no_molestar: dict[str, Any] | None = None
        self._camara_por_fuente: dict[str, bool] = {}   # vision.camara.activa por fuente
        self._camara_enviada = False                      # último OR enviado a Electron

        # Conexiones (solo desde el hilo del loop).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hilo_loop: int | None = None
        self._app: _Cliente | None = None
        self._huds: dict[int, _Cliente] = {}
        self._limite_estado = _Limite(INTERVALO_ESTADO_S)
        self._tareas: set[asyncio.Task[Any]] = set()
        self._rechazos = _Rechazos()
        self.descartados = 0

        self._desuscripciones: list[Callable[[], None]] = []
        self._suscribir()

    # ------------------------------------------------------------------
    # Propiedades públicas (nunca exponen el token)
    # ------------------------------------------------------------------

    @property
    def activo(self) -> bool:
        """``True`` mientras el servidor escucha y ``/escritorio`` acepta clientes."""
        return self._activo

    @property
    def url(self) -> str | None:
        """``ws://127.0.0.1:<puerto>/escritorio`` **sin** token (``None`` si no está activo)."""
        if not self._activo or self._puerto is None:
            return None
        return f"ws://{HOST}:{self._puerto}{RUTA_ESCRITORIO}"

    @property
    def overlay_conectado(self) -> bool:
        """``True`` si Amadeus Desktop (cliente sin Origin) está conectado."""
        return self._app is not None

    @property
    def n_hud(self) -> int:
        """Número de paneles de chat del HUD conectados."""
        return len(self._huds)

    @property
    def n_clientes(self) -> int:
        """Clientes de ``/escritorio`` conectados (Electron + HUD)."""
        return (1 if self._app is not None else 0) + len(self._huds)

    def __repr__(self) -> str:  # sin token
        return (f"EnlaceEscritorio(puerto={self._puerto!r}, activo={self._activo}, "
                f"overlay={self.overlay_conectado}, hud={self.n_hud})")

    def datos_para_hud(self) -> dict[str, str] | None:
        """Mensaje ``escritorio`` de ``/ws`` (§C2.4.6): ``{"token", "url": "/escritorio"}`` o ``None``.

        Solo si el enlace está activo, el HUD está habilitado y ``hud_chat`` lo permite. El llamador
        lo envía únicamente por ``/ws`` (token del HUD + Origin local ya validados).
        """
        if not self._activo or not self._chat_hud_permitido():
            return None
        return {"token": self._token, "url": RUTA_ESCRITORIO}

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def activar(self, puerto: int | None) -> None:
        """Marca el enlace como activo en ``puerto`` y escribe el archivo del token (en un hilo)."""
        if puerto is None:
            log.warning("Enlace de escritorio sin puerto: no se activa")
            return
        if not self._desuscripciones:  # reinicio tras cerrar()
            self._suscribir()
        self._loop = asyncio.get_running_loop()
        self._hilo_loop = threading.get_ident()
        self._puerto = int(puerto)
        self._activo = True
        await self._escribir_token()

    async def cerrar(self) -> None:
        """Suelta el bus, cierra los clientes y borra el token si sigue siendo propio. Idempotente."""
        for desuscribir in self._desuscripciones:
            try:
                desuscribir()
            except Exception as exc:
                log.debug("Error al desuscribir el enlace de escritorio del bus: %s", exc)
        self._desuscripciones = []
        self._activo = False
        limite = self._limite_estado
        if limite.temporizador is not None:
            limite.temporizador.cancel()
        limite.temporizador = None
        limite.pendiente = None

        clientes = ([self._app] if self._app is not None else []) + list(self._huds.values())
        self._app = None
        self._huds.clear()
        for cliente in clientes:
            self._cancelar_temporizadores(cliente)
            try:
                await asyncio.wait_for(cliente.conexion.close(1001, "cierre"), timeout=2.0)
            except (TimeoutError, _WsConnectionClosed):
                pass
            except Exception as exc:
                log.debug("Error al cerrar un cliente de /escritorio: %s", type(exc).__name__)
        tareas = [t for t in self._tareas if not t.done()]
        if tareas:
            _hechas, pendientes = await asyncio.wait(tareas, timeout=2.5)
            for tarea in pendientes:
                tarea.cancel()
        self._tareas.clear()

        if self._archivo_escrito and self._ruta_token is not None:
            try:
                borrado = await asyncio.to_thread(borrar_archivo_token_si_propio, self._ruta_token,
                                                  self._token, self._pid)
            except Exception as exc:
                log.debug("Error al borrar el token de escritorio: %s", type(exc).__name__)
                borrado = False
            if borrado:
                log.debug("Token de escritorio borrado")
            else:
                log.info("El archivo del token de escritorio ya no es de esta instancia: no se borra")
            self._archivo_escrito = False

    def diagnostico(self) -> DiagnosticoModulo:
        """``OK`` con la URL sin token, ``DEGRADADO`` si no se pudo escribir el token, ``EN ESPERA`` si no escucha."""
        url = self.url
        if url is None:
            return DiagnosticoModulo("escritorio", "Amadeus Desktop (/escritorio)", EstadoModulo.EN_ESPERA,
                                     "sin iniciar")
        if self._error_archivo is not None:
            return DiagnosticoModulo("escritorio", "Amadeus Desktop (/escritorio)", EstadoModulo.DEGRADADO,
                                     f"{self._error_archivo} · {url}")
        return DiagnosticoModulo("escritorio", "Amadeus Desktop (/escritorio)", EstadoModulo.OK, url)

    async def _escribir_token(self) -> None:
        """Escribe ``{url, token, pid}`` en ``escritorio_token_file`` (atómico y 0o600)."""
        self._error_archivo = None
        if self._ruta_token is None:
            self._error_archivo = "sin ruta para el token"
            log.warning("Sin RAMS_ESCRITORIO_TOKEN_FILE: Amadeus Desktop no podrá conectarse")
            return
        contenido = {"url": self.url, "token": self._token, "pid": self._pid}
        try:
            await asyncio.to_thread(escribir_archivo_token, self._ruta_token, contenido)
        except OSError as exc:
            self._error_archivo = "no se pudo escribir el token"
            log.warning("No se pudo escribir el token de escritorio en %s (%s)", self._ruta_token.name,
                        exc.strerror or type(exc).__name__)
            return
        self._archivo_escrito = True
        log.info("Token de /escritorio listo en %s", self._ruta_token.name)

    # ------------------------------------------------------------------
    # Handshake (process_request del servidor del HUD)
    # ------------------------------------------------------------------

    def validar_handshake(self, conexion: Any, solicitud: Any, consulta: str) -> Any:
        """Valida token, ``Origin`` y límites. ``None`` sigue el handshake; si no, responde 403/404."""
        if not self._activo:
            return conexion.respond(HTTPStatus.NOT_FOUND, "No encontrado\n")
        valores = parse_qs(consulta, keep_blank_values=True).get("t") or [""]
        token = valores[0]
        if not hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8")):
            self._registrar_rechazo("token inválido")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        try:
            origenes = list(solicitud.headers.get_all("Origin"))
        except Exception as exc:
            log.debug("Cabeceras Origin ilegibles en /escritorio: %s", type(exc).__name__)
            origenes = ["?", "?"]
        if len(origenes) > 1:
            self._registrar_rechazo("varias cabeceras Origin")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        if not origenes:
            return None  # Amadeus Desktop: uno nuevo reemplaza al anterior en manejar()
        origen = origenes[0]
        if not self._chat_hud_permitido() or origen not in self._origenes_hud():
            log.debug("Origin rechazado en /escritorio: %r", _texto(origen, 80))
            self._registrar_rechazo("Origin no permitido")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        if len(self._huds) >= MAX_CLIENTES_HUD:
            self._registrar_rechazo("demasiados paneles del HUD")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        return None

    def _chat_hud_permitido(self) -> bool:
        """El panel de chat del HUD solo existe con el HUD habilitado y ``hud_chat``."""
        return bool(getattr(self._settings, "hud_habilitado", False)) and bool(
            getattr(self._settings, "hud_chat", True))

    def _origenes_hud(self) -> frozenset[str]:
        """``{http://127.0.0.1:<p>, http://localhost:<p>}`` con el puerto real."""
        if self._puerto is None:
            return frozenset()
        return frozenset({f"http://127.0.0.1:{self._puerto}", f"http://localhost:{self._puerto}"})

    def _registrar_rechazo(self, motivo: str) -> None:
        """Registra conexiones rechazadas sin inundar el log (como mucho una línea cada 10 s)."""
        ahora = time.monotonic()
        r = self._rechazos
        if ahora - r.ultimo_log >= 10.0:
            extra = f" (+{r.sin_log} más)" if r.sin_log else ""
            log.info("Conexión a /escritorio rechazada: %s%s", motivo, extra)
            r.ultimo_log = ahora
            r.sin_log = 0
        else:
            r.sin_log += 1

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------

    @staticmethod
    def _origen_de(conexion: Any) -> str | None:
        """``Origin`` de la petición de la conexión (``None`` si no lo trae)."""
        try:
            origenes = conexion.request.headers.get_all("Origin")
        except Exception:
            return "?"  # no se puede leer: se trata como un Origin cualquiera (nunca como Electron)
        return origenes[0] if origenes else None

    async def manejar(self, conexion: Any) -> None:
        """Ciclo de vida de un cliente de ``/escritorio``: alta, mensajes iniciales, entrantes y baja."""
        if not self._activo or _ws_broadcast is None:
            return
        es_app = self._origen_de(conexion) is None
        cliente = _Cliente(conexion, es_app)
        if not es_app and len(self._huds) >= MAX_CLIENTES_HUD:  # carrera entre dos handshakes
            await self._cerrar_cliente(conexion, CODIGO_DEMASIADOS, "demasiados clientes")
            return
        try:
            for texto in self._mensajes_iniciales(es_app):  # síncrono y en orden: hola primero
                _ws_broadcast([conexion], texto, raise_exceptions=True)
        except Exception as exc:
            log.debug("No se pudo enviar el saludo a un cliente de /escritorio: %s", type(exc).__name__)
            return

        if es_app:
            anterior = self._app
            self._app = cliente
            if anterior is not None:
                log.info("Nueva conexión de Amadeus Desktop: se reemplaza la anterior")
                self._cancelar_temporizadores(anterior)
                self._crear_tarea(self._cerrar_cliente(anterior.conexion, CODIGO_REEMPLAZADO, "reemplazado"))
            else:
                log.info("Amadeus Desktop conectado a /escritorio")
        else:
            self._huds[id(conexion)] = cliente
            log.debug("Chat del HUD conectado a /escritorio (%d)", len(self._huds))
        self._publicar_conectado()

        try:
            async for crudo in conexion:
                self._procesar(cliente, crudo)  # valida, aplica el ritmo de su clase y publica
        except _WsConnectionClosed:
            log.debug("Cliente de /escritorio desconectado")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Error con un cliente de /escritorio: %s", type(exc).__name__)
        finally:
            self._cancelar_temporizadores(cliente)
            if es_app:
                if self._app is cliente:
                    self._app = None
                    log.info("Amadeus Desktop desconectado de /escritorio")
            else:
                self._huds.pop(id(conexion), None)
            if cliente.descartados:
                log.debug("Cliente de /escritorio: %d mensajes descartados por ritmo", cliente.descartados)
            if self._activo:
                self._publicar_conectado()

    async def _cerrar_cliente(self, conexion: Any, codigo: int, motivo: str) -> None:
        """Cierra una conexión con ``codigo`` (sin propagar errores)."""
        try:
            await conexion.close(codigo, motivo)
        except _WsConnectionClosed:
            pass
        except Exception as exc:
            log.debug("Error al cerrar un cliente de /escritorio: %s", type(exc).__name__)

    def _crear_tarea(self, corrutina: Any) -> None:
        """Lanza una tarea guardando la referencia (que no la recolecte el GC)."""
        try:
            tarea = asyncio.get_running_loop().create_task(corrutina, name="rams-escritorio")
        except RuntimeError:
            corrutina.close()
            return
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)

    def _publicar_conectado(self) -> None:
        """``escritorio.conectado{clientes, overlay, hud}`` con el estado actual."""
        self._bus.publicar("escritorio.conectado", {
            "clientes": self.n_clientes,
            "overlay": self._app is not None,
            "hud": len(self._huds),
        }, origen="escritorio")

    def _mensajes_iniciales(self, es_app: bool) -> list[str]:
        """``hola`` y la instantánea: permiso pendiente (con vista previa), travesuras, ``no_molestar``
        (siempre) y, para Electron, el estado."""
        mensajes = [_serializar("hola", {
            "version": VERSION,
            "nombre_ia": _texto(getattr(self._settings, "nombre_ia", "Amadeus"), 40),
            "usuario": _texto(getattr(self._settings, "trato_usuario", ""), 60),
        })]
        with self._lock:
            permiso = dict(self._permiso_pendiente) if self._permiso_pendiente else None
            travesuras = dict(self._travesuras) if self._travesuras else None
            estado = dict(self._estado) if self._estado else None
            no_molestar = dict(self._no_molestar) if self._no_molestar else None
            camara = any(self._camara_por_fuente.values())
            sin_camara_vista = not self._camara_por_fuente
        if sin_camara_vista:
            ultimo = self._bus.ultimo("vision.camara")   # publicado antes de crear el enlace
            camara = bool(ultimo is not None and isinstance(ultimo.datos, Mapping)
                          and ultimo.datos.get("activa") is True)
        if travesuras is None:
            ultimo = self._bus.ultimo("motor.travesuras")
            if ultimo is not None and isinstance(ultimo.datos, Mapping):
                travesuras = self._travesuras_saneadas(ultimo.datos)
        if no_molestar is None:
            # main solo publica pc.no_molestar al cambiar (el "sin no molestar" inicial no se
            # publica): sin ningún evento previo, el estado real es todo false.
            ultimo = self._bus.ultimo("pc.no_molestar")
            no_molestar = (self._no_molestar_saneado(ultimo.datos)
                           if ultimo is not None and isinstance(ultimo.datos, Mapping)
                           else dict(NO_MOLESTAR_INICIAL))
        if permiso is not None:
            mensajes.append(_serializar("permiso", permiso))
        if travesuras is not None:
            mensajes.append(_serializar("travesuras", travesuras))
        mensajes.append(_serializar("no_molestar", no_molestar))
        if es_app:
            # Ojo del overlay (§C2.13): siempre, para que Electron no conserve un "abierta" viejo.
            mensajes.append(_serializar("vision", {"camara": camara}))
        if es_app and estado is not None:
            mensajes.append(_serializar("estado", estado))
        return mensajes

    # ------------------------------------------------------------------
    # Entrantes
    # ------------------------------------------------------------------

    def _admitir(self, cliente: _Cliente, clase: str) -> bool:
        """Presupuesto por cliente y por clase en ventanas de 1 s (§C2.4.5); el exceso se descarta."""
        ventana = cliente.ventanas.get(clase)
        if ventana is None:
            ventana = cliente.ventanas[clase] = _Ventana()
        ahora = self._reloj()
        if ahora - ventana.inicio >= 1.0:
            ventana.inicio = ahora
            ventana.n = 0
            ventana.avisado = False
        ventana.n += 1
        if ventana.n > _PRESUPUESTOS[clase]:
            cliente.descartados += 1
            self.descartados += 1
            return False
        return True

    @staticmethod
    def _clase(tipo: str, mensaje: Mapping[str, Any]) -> str:
        """Clase de ritmo de un mensaje con ``tipo`` conocido (§C2.4.5)."""
        if tipo in _TIPOS_ESTADO:
            return _ESTADO
        if tipo == "caja":
            return _RUIDO
        if tipo == "evento":
            nombre = mensaje.get("nombre")
            if isinstance(nombre, str) and nombre in EVENTOS_ESCRITORIO and nombre not in _EVENTOS_RUIDO:
                return _ESTADO  # mostrada, ocultada, chat_abierto
            return _RUIDO
        return _GENERAL     # chat

    @staticmethod
    def _decodificar(crudo: Any) -> tuple[dict[str, Any] | None, str]:
        """``(mensaje, "")`` si ``crudo`` es un objeto JSON de ≤ 8 KiB; si no, ``(None, motivo)``."""
        if not isinstance(crudo, str):
            return None, "mensaje binario"
        if len(crudo) > MAX_BYTES_MENSAJE or len(crudo.encode("utf-8")) > MAX_BYTES_MENSAJE:
            return None, "mensaje de más de 8 KiB"
        try:
            mensaje = json.loads(crudo)
        except (ValueError, RecursionError):
            return None, "JSON inválido"
        if not isinstance(mensaje, dict):
            return None, "mensaje que no es un objeto"
        return mensaje, ""

    def _procesar(self, cliente: _Cliente, crudo: Any) -> None:
        """Valida un mensaje entrante, aplica el ritmo de su clase y publica lo que toque (nunca lanza)."""
        mensaje, motivo = self._decodificar(crudo)
        if mensaje is None:
            if self._admitir(cliente, _GENERAL):  # la basura gasta el cupo general (y no inunda el log)
                log.debug("/escritorio: %s ignorado", motivo)
            return
        tipo = mensaje.get("tipo")
        manejador = self._ENTRANTES.get(tipo) if isinstance(tipo, str) else None
        if manejador is None:
            if self._admitir(cliente, _GENERAL):
                nombre = tipo if isinstance(tipo, str) and _TIPO_SEGURO_RE.fullmatch(tipo) else "<no válido>"
                log.debug("/escritorio: tipo desconocido ignorado (%s)", nombre)
            return
        try:
            clase = self._clase(tipo, mensaje)
            if not self._admitir(cliente, clase):
                if clase == _ESTADO:
                    self._descartar_estado(cliente, tipo, mensaje)
                return
            manejador(self, cliente, mensaje)
        except Exception as exc:  # un mensaje raro nunca tumba la conexión
            log.debug("/escritorio: error al procesar «%s»: %s", tipo, type(exc).__name__)

    def _descartar_estado(self, cliente: _Cliente, tipo: str, mensaje: Mapping[str, Any]) -> None:
        """Un mensaje de estado no cupo en su ventana: ``mostrada``/``ocultada`` se pospone (gana la
        última); el resto se descarta con ``log.info`` (una línea por ventana)."""
        ventana = cliente.ventanas[_ESTADO]
        nombre = mensaje.get("nombre") if tipo == "evento" else None
        if isinstance(nombre, str) and nombre in _EVENTOS_VISIBILIDAD:
            if self._diferir_visibilidad(cliente, nombre, mensaje):
                if not ventana.avisado:
                    ventana.avisado = True
                    log.info("/escritorio: demasiados mensajes de estado; «%s» se publica al vencer la ventana",
                             nombre)
            return
        if not ventana.avisado:
            ventana.avisado = True
            log.info("/escritorio: demasiados mensajes de estado; se descarta «%s»", tipo)

    def _diferir_visibilidad(self, cliente: _Cliente, nombre: str, mensaje: Mapping[str, Any]) -> bool:
        """Guarda ``mostrada``/``ocultada`` para publicarla al vencer la ventana (``True`` si se guardó)."""
        if not cliente.es_app:
            return False
        datos = _datos_evento_validos(mensaje.get("datos"))
        loop = self._loop
        if datos is None or loop is None:
            return False
        cliente.visibilidad_pendiente = (nombre, datos)
        if cliente.visibilidad_temporizador is None:
            ventana = cliente.ventanas[_ESTADO]
            espera = min(1.0, max(0.0, 1.0 - (self._reloj() - ventana.inicio)))
            cliente.visibilidad_temporizador = loop.call_later(espera, self._vaciar_visibilidad, cliente)
        return True

    def _vaciar_visibilidad(self, cliente: _Cliente) -> None:
        """Publica la visibilidad pospuesta más reciente (si sigue siendo el cliente activo)."""
        cliente.visibilidad_temporizador = None
        pendiente = cliente.visibilidad_pendiente
        cliente.visibilidad_pendiente = None
        if pendiente is None or not self._activo or self._app is not cliente:
            return
        nombre, datos = pendiente
        self._bus.publicar("escritorio.evento", {"nombre": nombre, "datos": datos}, origen="escritorio")

    def _en_hola_app(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``hola_app{pid, version}`` (solo Electron, una vez por conexión) → ``escritorio.app{pid}``."""
        if not cliente.es_app:
            log.debug("/escritorio: hola_app desde el HUD ignorado")
            return
        if cliente.hola_app:
            return
        pid = mensaje.get("pid")
        if not _es_entero(pid) or not 0 < pid <= MAX_PID:
            log.debug("/escritorio: hola_app con pid inválido ignorado")
            return
        cliente.hola_app = True
        version = mensaje.get("version")
        texto_version = version if isinstance(version, str) and _VERSION_SEGURA_RE.fullmatch(version) else "?"
        log.info("Amadeus Desktop identificado (versión %s, pid %d)", texto_version, pid)
        self._bus.publicar("escritorio.app", {"pid": pid}, origen="escritorio")

    def _en_chat(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``chat{texto}`` → ``escritorio.chat{texto}`` (1..2000). Nunca ``usuario.entrada``; nunca se registra."""
        texto = mensaje.get("texto")
        if not isinstance(texto, str) or len(texto) > MAX_TEXTO_CHAT:
            log.debug("/escritorio: chat con texto inválido ignorado")
            return
        texto = texto.replace("\x00", "").strip()
        if not texto:
            return
        self._bus.publicar("escritorio.chat", {"texto": texto}, origen="escritorio")

    def _en_evento(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``evento{nombre, datos}`` (solo Electron) → ``escritorio.evento`` (escalares ≤ 1 KiB)."""
        if not cliente.es_app:
            log.debug("/escritorio: evento desde el HUD ignorado")
            return
        nombre = mensaje.get("nombre")
        if not isinstance(nombre, str) or nombre not in EVENTOS_ESCRITORIO:
            log.debug("/escritorio: evento desconocido ignorado")
            return
        datos = _datos_evento_validos(mensaje.get("datos"))
        if datos is None:
            log.debug("/escritorio: evento %s con datos inválidos ignorado", nombre)
            return
        if nombre == "cursor_encima":
            ahora = self._reloj()
            if ahora - cliente.cursor_encima_ts < INTERVALO_CURSOR_ENCIMA_S:
                return
            cliente.cursor_encima_ts = ahora
        elif nombre == "soltada":
            datos["altura"] = _acotar(datos.get("altura"), 0.0, 1.0, 0.0)
        elif nombre in _EVENTOS_VISIBILIDAD:
            self._cancelar_visibilidad(cliente)  # una visibilidad más nueva gana a la pospuesta
        self._bus.publicar("escritorio.evento", {"nombre": nombre, "datos": datos}, origen="escritorio")

    def _en_control(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``control{accion}``: ``CONTROLES_ESCRITORIO`` → ``escritorio.control``; ``silenciar`` → ``hud.control``."""
        accion = mensaje.get("accion")
        if not isinstance(accion, str):
            return
        if accion in CONTROLES_ESCRITORIO:
            log.info("Control desde el escritorio: %s", accion)
            self._bus.publicar("escritorio.control", {"accion": accion}, origen="escritorio")
        elif accion == "silenciar":
            log.info("Control desde el escritorio: silenciar")
            self._bus.publicar("hud.control", {"accion": "silenciar"}, origen="escritorio")
        else:
            log.debug("/escritorio: control no permitido ignorado")

    def _en_confirmacion(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``confirmacion{id, aprobado}`` (id ``^[0-9a-f]{8}$``, booleano) → ``escritorio.confirmacion``."""
        ident = mensaje.get("id")
        aprobado = mensaje.get("aprobado")
        if not isinstance(ident, str) or not ID_CONFIRMACION_RE.fullmatch(ident) or not isinstance(aprobado, bool):
            log.debug("/escritorio: confirmación inválida ignorada")
            return
        log.info("Confirmación desde el escritorio: %s", "aprobada" if aprobado else "rechazada")
        self._bus.publicar("escritorio.confirmacion", {"id": ident, "aprobado": aprobado}, origen="escritorio")

    def _en_caja(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``caja{x,y,w,h}`` (solo Electron, enteros) → ``escritorio.caja`` a ≤ 2 Hz y solo si cambia."""
        if not cliente.es_app:
            log.debug("/escritorio: caja desde el HUD ignorada")
            return
        valores = [mensaje.get(k) for k in ("x", "y", "w", "h")]
        if not all(_es_entero(v) for v in valores):
            log.debug("/escritorio: caja con valores no enteros ignorada")
            return
        x, y, w, h = valores
        if not (0 < w <= MAX_LADO_CAJA and 0 < h <= MAX_LADO_CAJA
                and abs(x) <= MAX_COORD_CAJA and abs(y) <= MAX_COORD_CAJA):
            log.debug("/escritorio: caja fuera de rango ignorada")
            return
        caja = (x, y, w, h)
        if cliente.caja_temporizador is not None:  # ya hay una publicación diferida: gana la última
            cliente.caja_pendiente = caja
            return
        if caja == cliente.caja_publicada:
            return
        ahora = self._reloj()
        espera = INTERVALO_CAJA_S - (ahora - cliente.caja_ts)
        if espera <= 0:
            self._publicar_caja(cliente, caja)
            return
        loop = self._loop
        if loop is None:
            return
        cliente.caja_pendiente = caja
        cliente.caja_temporizador = loop.call_later(espera, self._vaciar_caja, cliente)

    def _vaciar_caja(self, cliente: _Cliente) -> None:
        """Publica la caja diferida más reciente (si sigue siendo el cliente activo y cambió)."""
        cliente.caja_temporizador = None
        caja = cliente.caja_pendiente
        cliente.caja_pendiente = None
        if caja is None or not self._activo or self._app is not cliente or caja == cliente.caja_publicada:
            return
        self._publicar_caja(cliente, caja)

    def _publicar_caja(self, cliente: _Cliente, caja: tuple[int, int, int, int]) -> None:
        cliente.caja_publicada = caja
        cliente.caja_ts = self._reloj()
        x, y, w, h = caja
        self._bus.publicar("escritorio.caja", {"x": x, "y": y, "w": w, "h": h}, origen="escritorio")

    @staticmethod
    def _cancelar_caja(cliente: _Cliente) -> None:
        if cliente.caja_temporizador is not None:
            cliente.caja_temporizador.cancel()
            cliente.caja_temporizador = None
        cliente.caja_pendiente = None

    @staticmethod
    def _cancelar_visibilidad(cliente: _Cliente) -> None:
        if cliente.visibilidad_temporizador is not None:
            cliente.visibilidad_temporizador.cancel()
            cliente.visibilidad_temporizador = None
        cliente.visibilidad_pendiente = None

    def _cancelar_temporizadores(self, cliente: _Cliente) -> None:
        """Cancela las publicaciones diferidas (caja y visibilidad) de un cliente que se va."""
        self._cancelar_caja(cliente)
        self._cancelar_visibilidad(cliente)

    _ENTRANTES: Final[dict[str, Callable[[EnlaceEscritorio, _Cliente, Mapping[str, Any]], None]]] = {
        "hola_app": _en_hola_app,
        "chat": _en_chat,
        "evento": _en_evento,
        "control": _en_control,
        "confirmacion": _en_confirmacion,
        "caja": _en_caja,
    }

    # ------------------------------------------------------------------
    # Bus → salientes
    # ------------------------------------------------------------------

    def _suscribir(self) -> None:
        """Se suscribe a los eventos que reenvía ``/escritorio`` (nombres exactos)."""
        self._desuscripciones = [self._bus.suscribir(nombre, self._al_evento)
                                 for nombre in EVENTOS_SUSCRITOS_ESCRITORIO]

    @staticmethod
    def _travesuras_saneadas(datos: Mapping[str, Any]) -> dict[str, Any]:
        return {"activas": bool(datos.get("activas", False)), "hasta_ts": _real(datos.get("hasta_ts"))}

    @staticmethod
    def _no_molestar_saneado(datos: Mapping[str, Any]) -> dict[str, Any]:
        """``pc.no_molestar`` → ``{activo, motivo, usuario}``; ``usuario`` = el que el usuario puso a mano
        (``activo and motivo == "usuario"``), que es el estado del botón «No molestar» de los chats."""
        activo = datos.get("activo") is True
        motivo = datos.get("motivo")
        if not activo or not isinstance(motivo, str) or motivo not in MOTIVOS_NO_MOLESTAR:
            motivo = ""
        return {"activo": activo, "motivo": motivo, "usuario": activo and motivo == "usuario"}

    def _al_evento(self, evento: Evento) -> None:
        """Manejador síncrono del bus: guarda el estado y programa el mensaje saliente."""
        try:
            nombre = evento.nombre
            datos: Mapping[str, Any] = evento.datos if isinstance(evento.datos, Mapping) else {}
            salida = self._traducir(nombre, datos)
            if salida is None or not self._activo:
                return
            destino, tipo, mensaje = salida
            self._programar(destino, tipo, mensaje)
        except Exception as exc:  # nunca propaga al bus
            log.debug("Error al procesar %s para /escritorio: %s", getattr(evento, "nombre", "?"),
                      type(exc).__name__)

    def _traducir(self, nombre: str, datos: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        """Evento del bus → ``(destino, tipo, datos)`` de ``/escritorio`` (y guarda el estado)."""
        if nombre == "salida.subtitulo":
            return _TODOS, "subtitulo", {
                "texto": _texto(datos.get("texto"), MAX_TEXTO_SUBTITULO),
                "exp": _texto(datos.get("exp"), 40),
                "canal": _texto(datos.get("canal"), 20),
                "origen": _texto(datos.get("origen"), 20),
            }
        if nombre in ("voz.inicio", "voz.fin"):
            return _TODOS, "voz", {"hablando": nombre == "voz.inicio"}
        if nombre == "escritorio.orden":
            orden = _orden_saneada(datos)
            if orden is None:
                log.debug("escritorio.orden inválida: no se reenvía")
                return None
            return _APP, "orden", orden
        if nombre == "escritorio.sistema":
            texto = _texto(datos.get("texto"), MAX_TEXTO_SISTEMA)
            return (_TODOS, "sistema", {"texto": texto}) if texto else None
        if nombre == "permiso.solicitado":
            permiso: dict[str, Any] = {
                "id": _texto(datos.get("id"), 64),
                "descripcion": _texto(datos.get("descripcion"), 300),
                "destructiva": bool(datos.get("destructiva", False)),
                "pendiente": True,
            }
            vista = datos.get("vista_previa")
            if isinstance(vista, str) and vista:
                permiso["vista_previa"] = _texto(vista, MAX_VISTA_PREVIA)
            with self._lock:
                self._permiso_pendiente = dict(permiso)
            return _TODOS, "permiso", permiso
        if nombre == "permiso.resuelto":
            ident = _texto(datos.get("id"), 64)
            with self._lock:
                pendiente = self._permiso_pendiente
                mismo = pendiente is not None and pendiente.get("id") in ("", ident)
                if pendiente is None or mismo:
                    self._permiso_pendiente = None
            return _TODOS, "permiso", {
                "id": ident,
                "descripcion": pendiente.get("descripcion", "") if (pendiente and mismo) else "",
                "destructiva": bool(pendiente.get("destructiva", False)) if (pendiente and mismo) else False,
                "pendiente": False,
                "aprobado": bool(datos.get("aprobado", False)),
            }
        if nombre == "homeostasis.estado":
            estado = datos.get("estado")
            if not isinstance(estado, Mapping):
                return None
            resumen = {
                "aburrimiento": _acotar(estado.get("boredom"), 0.0, 1.0, 0.0),
                "fatiga": _acotar(estado.get("fatiga"), 0.0, 1.0, 0.0),
                "valencia": _acotar(estado.get("valencia"), -1.0, 1.0, 0.0),
            }
            with self._lock:
                self._estado = dict(resumen)
            return _APP, "estado", resumen
        if nombre == "motor.travesuras":
            travesuras = self._travesuras_saneadas(datos)
            with self._lock:
                self._travesuras = dict(travesuras)
            return _TODOS, "travesuras", travesuras
        if nombre == "pc.no_molestar":
            no_molestar = self._no_molestar_saneado(datos)
            with self._lock:
                self._no_molestar = dict(no_molestar)
            return _TODOS, "no_molestar", no_molestar
        if nombre == "vision.camara":
            # OR de las fuentes (la continua se repite cada 5 s): solo se envía si cambia.
            fuente = datos.get("fuente")
            fuente = fuente if fuente in FUENTES_CAMARA else "vistazo"
            with self._lock:
                self._camara_por_fuente[fuente] = datos.get("activa") is True
                camara = any(self._camara_por_fuente.values())
                if camara == self._camara_enviada:
                    return None
                self._camara_enviada = camara
            return _APP, "vision", {"camara": camara}
        return None

    def _programar(self, destino: str, tipo: str, datos: dict[str, Any]) -> None:
        """Envía en el hilo del loop (delegando con ``call_soon_threadsafe`` si hace falta)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if threading.get_ident() == self._hilo_loop:
            self._enviar(destino, tipo, datos)
            return
        try:
            loop.call_soon_threadsafe(self._enviar, destino, tipo, datos)
        except RuntimeError as exc:
            log.debug("Loop de /escritorio cerrado; se descarta %s (%s)", tipo, exc)

    def _enviar(self, destino: str, tipo: str, datos: dict[str, Any]) -> None:
        """Aplica el límite de ``estado`` (≤ 1 Hz, con coalescencia) y difunde."""
        if not self._activo:
            return
        if tipo != "estado":
            self._difundir(destino, _serializar(tipo, datos))
            return
        if self._app is None:
            return
        limite = self._limite_estado
        if limite.temporizador is not None:
            limite.pendiente = datos
            return
        ahora = self._reloj()
        espera = limite.intervalo - (ahora - limite.ultimo_envio)
        if espera <= 0:
            limite.ultimo_envio = ahora
            self._difundir(destino, _serializar(tipo, datos))
            return
        loop = self._loop
        if loop is None:
            return
        limite.pendiente = datos
        limite.temporizador = loop.call_later(espera, self._vaciar_estado)

    def _vaciar_estado(self) -> None:
        """Envía el ``estado`` diferido más reciente."""
        limite = self._limite_estado
        datos = limite.pendiente
        limite.pendiente = None
        limite.temporizador = None
        if datos is None or not self._activo or self._app is None:
            return
        limite.ultimo_envio = self._reloj()
        self._difundir(_APP, _serializar("estado", datos))

    def _difundir(self, destino: str, texto: str) -> None:
        """Escribe ``texto`` en los clientes del destino (sin esperar; suelta a los saturados)."""
        if _ws_broadcast is None:
            return
        clientes = [self._app] if self._app is not None else []
        if destino == _TODOS:
            clientes.extend(self._huds.values())
        conexiones = []
        for cliente in clientes:
            transporte = getattr(cliente.conexion, "transport", None)
            try:
                pendiente = transporte.get_write_buffer_size() if transporte is not None else 0
            except Exception:
                pendiente = 0
            if pendiente > MAX_BUFFER_CLIENTE:
                log.info("Cliente de /escritorio saturado (%d bytes sin enviar); se desconecta", pendiente)
                try:
                    transporte.abort()
                except Exception as exc:
                    log.debug("Error al abortar un cliente saturado: %s", type(exc).__name__)
                continue
            conexiones.append(cliente.conexion)
        if not conexiones:
            return
        try:
            _ws_broadcast(conexiones, texto, raise_exceptions=True)
        except Exception as exc:  # incluye ExceptionGroup: algún cliente no pudo recibir
            log.debug("Difusión parcial de /escritorio: %s", type(exc).__name__)
