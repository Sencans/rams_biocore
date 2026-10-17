"""Servidor del HUD «videollamada Amadeus» (HTTP + WebSocket en 127.0.0.1:8765).

Qué hace:
- Sirve ``interface/amadeus_hud.html`` en ``/`` (y ``/index.html``) con cabeceras de seguridad
  y CSP fijas, y un WebSocket en ``/ws`` protegido por un token aleatorio por sesión
  (``?t=``, comparado con ``hmac.compare_digest``) **y** por la cabecera ``Origin``
  (solo ``http://127.0.0.1:<puerto>`` y ``http://localhost:<puerto>``).
- Observa el bus de eventos (§5) desde su construcción: guarda los diagnósticos
  (``sistema.modulo``) y los últimos eventos aunque el servidor aún no escuche, y los reenvía a
  los clientes con los límites de frecuencia de §10.23 (estado 2 Hz, telemetría 1 Hz,
  boca 15 Hz, log 10/s).
- Al conectar, cada cliente recibe ``hola`` y una instantánea (último estado homeostático,
  telemetría, presencia, cámara, diagnósticos, permiso pendiente y apagado si lo hubo).
- Del cliente solo acepta ``{"tipo": "control", "accion": x}`` con ``x`` en
  ``CONTROLES_PERMITIDOS`` (máximo 5 mensajes/s por cliente): publica ``hud.control`` y responde
  ``control_ack``. El HUD **no** es un canal de texto ni de confirmaciones.

Dependencias opcionales: ``websockets`` (≥ 14, API ``websockets.asyncio``). Sin ella el módulo
se importa igual y ``iniciar()`` devuelve ``OFF ("websockets no instalado")``.

Degradación: puerto ocupado ⇒ ``SIN CONEXIÓN ("puerto ocupado")``; sin ``amadeus_hud.html`` ⇒
``DEGRADADO`` y una página mínima de aviso. Nunca escucha en ``0.0.0.0`` y nunca registra la URL
con token (solo la muestra la consola de ``main.py``).
"""

from __future__ import annotations

import asyncio
import email.utils
import errno
import hmac
import json
import logging
import math
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from core.bus import BusEventos, Evento
from core.config import Settings
from core.contracts import VERSION, DiagnosticoModulo, EstadoModulo

try:  # opcional: sin websockets el HUD queda en OFF
    from websockets.asyncio.server import broadcast as _ws_broadcast
    from websockets.asyncio.server import serve as _ws_serve
    from websockets.datastructures import Headers as _WsHeaders
    from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
    from websockets.http11 import Response as _WsResponse
except ImportError:  # sin websockets: HUD desactivado
    _ws_broadcast = None
    _ws_serve = None
    _WsHeaders = None
    _WsResponse = None

    class _WsConnectionClosed(Exception):  # type: ignore[no-redef]
        """Sustituto para poder escribir ``except`` sin websockets instalado."""

    WEBSOCKETS_DISPONIBLE = False
else:
    WEBSOCKETS_DISPONIBLE = True

__all__ = [
    "CONTROLES_PERMITIDOS",
    "CSP",
    "EVENTOS_SUSCRITOS",
    "ServidorHUD",
    "WEBSOCKETS_DISPONIBLE",
    "traducir_evento",
]

log = logging.getLogger("rams.hud")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CONTROLES_PERMITIDOS: Final = frozenset({"silenciar", "camara", "colgar"})

NOMBRE_HTML: Final = "amadeus_hud.html"
HOST: Final = "127.0.0.1"  # nunca 0.0.0.0 (§9.4)

CSP: Final = (
    "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; img-src 'self' data:"
)

# Eventos del bus que observa el HUD (tabla de §10.23 punto 4, más sistema.arranque para hola).
EVENTOS_SUSCRITOS: Final = (
    "homeostasis.estado",
    "homeostasis.cambio_linea",
    "pc.telemetria",
    "salida.subtitulo",
    "voz.inicio",
    "voz.fin",
    "voz.nivel_boca",
    "vision.camara",
    "usuario.presencia",
    "sistema.modulo",
    "sistema.apagado",
    "sistema.arranque",
    "telegram.enviado",
    "telegram.respuesta_llamada",
    "memoria.consolidada",
    "permiso.solicitado",
    "permiso.resuelto",
    "log",
)

# Instantánea para clientes nuevos: (evento del bus, tipo de mensaje del HUD).
INSTANTANEA: Final = (
    ("homeostasis.estado", "estado"),
    ("pc.telemetria", "telemetria"),
    ("usuario.presencia", "presencia"),
    ("vision.camara", "camara"),
)
_NOMBRES_INSTANTANEA: Final = frozenset(nombre for nombre, _tipo in INSTANTANEA)

# Límites de frecuencia hacia los clientes: tipo → intervalo mínimo en segundos. Se aplican con
# coalescencia: si llega otro mensaje dentro del intervalo se envía el más reciente al vencer
# (así nunca se pierde el último estado, solo los intermedios).
INTERVALOS_MIN_S: Final[dict[str, float]] = {
    "estado": 0.5,        # 2 Hz
    "telemetria": 1.0,    # 1 Hz
    "boca": 1.0 / 15.0,   # 15 Hz
}
MAX_LOG_POR_S: Final = 10
MAX_MENSAJES_CLIENTE_S: Final = 5
MAX_CLIENTES: Final = 8
MAX_TAMANO_MENSAJE: Final = 65536
MAX_BUFFER_CLIENTE: Final = 2 * 1024 * 1024  # bytes pendientes de escribir antes de soltar a un cliente lento
ESPERA_CIERRE_S: Final = 3.0
# Lanzamiento del navegador: cuánto se espera a que ``setsid --fork`` salga (lo hace al instante)
# y a que un navegador lanzado con ``Popen`` falle de inmediato (si sigue vivo, se deja así).
ESPERA_SETSID_S: Final = 5.0
ESPERA_NAVEGADOR_S: Final = 2.0

# telegram.enviado.tipo → llamada.tipo (§10.23)
_TIPO_LLAMADA: Final[dict[str, str]] = {
    "llamada": "perdida",
    "recordatorio": "perdida",
    "megumin": "megumin",
    "critico": "critico",
    "respuesta": "mensaje",
}

# Navegadores con modo aplicación (Linux), en orden de preferencia.
_NAVEGADORES_APP: Final = ("chromium", "chromium-browser", "google-chrome")

_PAGINA_SIN_HTML: Final = (
    "<!doctype html>\n<html lang=\"es\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>Amadeus</title><style>body{background:#070b14;color:#d6e6ff;"
    "font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;"
    "padding:16px;box-sizing:border-box;text-align:center}</style></head><body>"
    "<main><h1>AMADEUS</h1><p>Falta el archivo <code>interface/amadeus_hud.html</code>.</p>"
    "<p>Reinstala el grupo HUD de R.A.M.S. BIO-CORE y reinicia el sistema.</p></main>"
    "</body></html>\n"
).encode("utf-8")


# ---------------------------------------------------------------------------
# Utilidades de saneado (los payloads del bus vienen de otros módulos: se tratan con cautela)
# ---------------------------------------------------------------------------


def _texto(valor: Any, maximo: int = 300) -> str:
    """Convierte a texto y recorta a ``maximo`` caracteres (con «…»)."""
    if valor is None:
        return ""
    texto = valor if isinstance(valor, str) else str(valor)
    if len(texto) > maximo:
        return texto[: max(0, maximo - 1)] + "…"
    return texto


def _real(valor: Any, defecto: float | None = None) -> float | None:
    """Número real finito o ``defecto``."""
    if isinstance(valor, bool):
        return float(valor)
    if isinstance(valor, (int, float)):
        numero = float(valor)
        return numero if math.isfinite(numero) else defecto
    if isinstance(valor, str):
        try:
            numero = float(valor)
        except ValueError:
            return defecto
        return numero if math.isfinite(numero) else defecto
    return defecto


def _entero(valor: Any) -> int | None:
    """Entero o ``None``."""
    numero = _real(valor)
    if numero is None:
        return None
    return int(numero)


def _acotar01(valor: Any) -> float:
    """Real acotado a [0, 1] (0 si no es un número)."""
    numero = _real(valor, 0.0) or 0.0
    return min(1.0, max(0.0, numero))


def _json_seguro(valor: Any, profundidad: int = 0) -> Any:
    """Convierte ``valor`` en algo serializable a JSON estricto (sin NaN ni tipos raros)."""
    if profundidad > 8:
        return None
    if valor is None or isinstance(valor, (bool, int, str)):
        return valor
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, Enum):
        return _json_seguro(valor.value, profundidad + 1)
    if isinstance(valor, Mapping):
        return {str(k): _json_seguro(v, profundidad + 1) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set, frozenset)):
        return [_json_seguro(v, profundidad + 1) for v in valor]
    return str(valor)


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
    mensaje = {"tipo": tipo, "datos": datos}
    try:
        return json.dumps(mensaje, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                          default=_por_defecto_json)
    except (TypeError, ValueError) as exc:
        log.debug("Payload %s no serializable directamente (%s); se sanea", tipo, exc)
        return json.dumps(_json_seguro(mensaje), ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False)


def traducir_evento(nombre: str, datos: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Traduce un evento del bus al mensaje del HUD ``(tipo, datos)`` de §10.23 (o ``None``).

    ``sistema.arranque`` no se traduce aquí (genera un ``hola`` con datos del servidor).
    """
    if not isinstance(datos, Mapping):
        datos = {}
    if nombre == "homeostasis.estado":
        estado = datos.get("estado")
        if not isinstance(estado, Mapping):
            return None
        bump = datos.get("bump")
        if not isinstance(bump, (list, tuple)):
            bump = estado.get("bump")
        if not isinstance(bump, (list, tuple)):
            bump = ()
        return "estado", {"estado": dict(estado), "bump": [_acotar01(b) for b in list(bump)[:16]]}
    if nombre == "homeostasis.cambio_linea":
        return "linea_mundo", {
            "antes": _texto(datos.get("antes"), 32),
            "despues": _texto(datos.get("despues"), 32),
            "divergencia": _real(datos.get("divergencia")),
        }
    if nombre == "pc.telemetria":
        return "telemetria", dict(datos)
    if nombre == "salida.subtitulo":
        return "subtitulo", {
            "texto": _texto(datos.get("texto"), 1200),
            "exp": _texto(datos.get("exp"), 40),
            "motion": _texto(datos.get("motion"), 40),
            "tone": _texto(datos.get("tone"), 40),
            "canal": _texto(datos.get("canal"), 20),
            "origen": _texto(datos.get("origen"), 20),
        }
    if nombre in ("voz.inicio", "voz.fin"):
        return "voz", {"hablando": nombre == "voz.inicio", "motor": _texto(datos.get("motor"), 20)}
    if nombre == "voz.nivel_boca":
        return "boca", {"valor": _acotar01(datos.get("valor"))}
    if nombre == "vision.camara":
        return "camara", {
            "activa": bool(datos.get("activa", False)),
            "habilitada": bool(datos.get("habilitada", False)),
            "duracion_ms": _entero(datos.get("duracion_ms")) or 0,
        }
    if nombre == "usuario.presencia":
        return "presencia", dict(datos)
    if nombre == "sistema.modulo":
        return "modulo", _normalizar_modulo(datos)
    if nombre == "sistema.apagado":
        return "apagado", {"motivo": _texto(datos.get("motivo") or "usuario", 40)}
    if nombre == "telegram.enviado":
        tipo = _TIPO_LLAMADA.get(str(datos.get("tipo", "")), "mensaje")
        return "llamada", {"tipo": tipo, "texto": _texto(datos.get("texto"), 200)}
    if nombre == "telegram.respuesta_llamada":
        tipo = "devuelta" if bool(datos.get("contestada", False)) else "rechazada"
        return "llamada", {"tipo": tipo, "texto": ""}
    if nombre == "memoria.consolidada":
        return "consolidacion", {
            "kv_id": _texto(datos.get("kv_id"), 20),
            "n_turnos": _entero(datos.get("n_turnos")) or 0,
            "metodo": _texto(datos.get("metodo"), 20),
        }
    if nombre == "permiso.solicitado":
        return "permiso", {
            "id": _texto(datos.get("id"), 64),
            "descripcion": _texto(datos.get("descripcion"), 300),
            "destructiva": bool(datos.get("destructiva", False)),
            "pendiente": True,
        }
    if nombre == "permiso.resuelto":
        return "permiso", {
            "id": _texto(datos.get("id"), 64),
            "aprobado": bool(datos.get("aprobado", False)),
            "motivo": _texto(datos.get("motivo"), 200),
            "pendiente": False,
        }
    if nombre == "log":
        return "log", {
            "nivel": _texto(datos.get("nivel"), 16),
            "modulo": _texto(datos.get("modulo"), 60),
            "mensaje": _texto(datos.get("mensaje"), 500),
            "ts": _real(datos.get("ts"), time.time()),
        }
    return None


def _normalizar_modulo(datos: Mapping[str, Any]) -> dict[str, Any]:
    """Payload de ``sistema.modulo`` saneado: ``{"clave","nombre","estado","detalle","latencia_ms"}``."""
    estado = datos.get("estado", "")
    if isinstance(estado, Enum):
        estado = estado.value
    return {
        "clave": _texto(datos.get("clave"), 40),
        "nombre": _texto(datos.get("nombre"), 80),
        "estado": _texto(estado, 20),
        "detalle": _texto(datos.get("detalle"), 300),
        "latencia_ms": _entero(datos.get("latencia_ms")),
    }


def _plataforma() -> str:
    """Plataforma actual (separada para poder simularla en los tests)."""
    return sys.platform


def _lanzar_popen_desacoplado(argv: list[str]) -> int | None:
    """Lanza ``argv`` con ``Popen`` en una sesión nueva (sin shell); se ejecuta en un hilo.

    Devuelve el código de salida si el proceso termina en ``ESPERA_NAVEGADOR_S`` o ``None`` si sigue
    vivo. El objeto ``Popen`` no se guarda: al recolectarse solo se apunta para recoger su estado más
    tarde y, a diferencia del transporte de asyncio, **no** mata al hijo.
    """
    proceso = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return proceso.wait(timeout=ESPERA_NAVEGADOR_S)
    except subprocess.TimeoutExpired:
        return None


def _es_puerto_ocupado(exc: OSError) -> bool:
    """``True`` si el error de ``bind`` indica que el puerto ya está en uso."""
    codigos = {errno.EADDRINUSE, 10048}  # 10048 = WSAEADDRINUSE (Windows)
    return exc.errno in codigos or getattr(exc, "winerror", None) == 10048


def _es_puerto_prohibido(exc: OSError) -> bool:
    """``True`` si el error de ``bind`` indica un puerto no permitido o reservado."""
    codigos = {errno.EACCES, 10013}  # 10013 = WSAEACCES (puerto reservado en Windows)
    return exc.errno in codigos or getattr(exc, "winerror", None) == 10013


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------


@dataclass
class _Limite:
    """Estado de un límite de frecuencia con coalescencia (solo se usa en el hilo del loop)."""

    intervalo: float
    ultimo_envio: float = -math.inf
    pendiente: dict[str, Any] | None = None
    temporizador: asyncio.TimerHandle | None = None


class ServidorHUD:
    """Servidor HTTP + WebSocket del HUD «videollamada Amadeus» (§10.23).

    ``__init__`` es barato (sin sockets) y **se suscribe ya** al bus: main lo construye antes que
    los demás módulos para no perder ningún ``sistema.modulo`` del arranque.
    """

    def __init__(self, settings: Settings, bus: BusEventos, *, ruta_html: Path | None = None) -> None:
        self._settings = settings
        self._bus = bus
        self._ruta_html = Path(ruta_html) if ruta_html is not None else Path(__file__).resolve().parent / NOMBRE_HTML
        self.token: str = secrets.token_urlsafe(16)
        self.puerto_real: int | None = None
        self._creado_ts = time.time()

        # Estado observado del bus (protegido por _lock: el bus puede llamar desde otro hilo).
        self._lock = threading.Lock()
        self._diagnosticos: dict[str, dict[str, Any]] = {}
        self._ultimos: dict[str, Evento] = {}
        self._inicio_ts_evento: float | None = None
        self._permiso_pendiente: dict[str, Any] | None = None
        self._apagado: dict[str, Any] | None = None

        # Servidor (solo se toca desde el hilo del loop).
        self._servidor: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hilo_loop: int | None = None
        self._clientes: set[Any] = set()
        self._html: bytes | None = None
        self._limites: dict[str, _Limite] = {t: _Limite(i) for t, i in INTERVALOS_MIN_S.items()}
        self._log_ventana_inicio = 0.0
        self._log_en_ventana = 0
        self._apagado_enviado = False
        self._tareas_navegador: set[asyncio.Task[Any]] = set()
        self._navegador_abierto_ts: float | None = None
        self._rechazos_ultimo_log = -math.inf
        self._rechazos_sin_log = 0
        self.descartados_cliente = 0
        self.descartados_log = 0

        self._desuscripciones: list[Callable[[], None]] = []
        self._suscribir()

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    @property
    def url(self) -> str | None:
        """URL del HUD **con** token (solo para la consola local; nunca se registra)."""
        if self.puerto_real is None or self._servidor is None:
            return None
        return f"http://{HOST}:{self.puerto_real}/?t={self.token}"

    @property
    def n_clientes(self) -> int:
        """Número de clientes WebSocket conectados."""
        return len(self._clientes)

    def __repr__(self) -> str:  # sin token
        return f"ServidorHUD(puerto={self.puerto_real!r}, clientes={self.n_clientes})"

    async def iniciar(self) -> DiagnosticoModulo:
        """Abre el servidor en ``127.0.0.1:hud_puerto`` y devuelve el diagnóstico (clave ``"hud"``).

        ``OFF`` si ``hud_habilitado`` es ``False`` o falta websockets; ``SIN CONEXIÓN`` si el puerto
        está ocupado; ``DEGRADADO`` si falta ``amadeus_hud.html``; ``OK`` con la URL **sin** token.
        """
        if not self._settings.hud_habilitado:
            return self._registrar_propio(EstadoModulo.OFF, "deshabilitado")
        if not WEBSOCKETS_DISPONIBLE or _ws_serve is None:
            log.info("HUD desactivado: websockets no está instalado")
            return self._registrar_propio(EstadoModulo.OFF, "websockets no instalado")
        if self._servidor is not None:
            return self._diagnostico_activo()

        if not self._desuscripciones:  # reinicio tras cerrar()
            self._suscribir()
        self._loop = asyncio.get_running_loop()
        self._hilo_loop = threading.get_ident()
        self._apagado_enviado = False
        self._html = await asyncio.to_thread(self._leer_html)

        try:
            servidor = await _ws_serve(
                self._manejar,
                HOST,
                int(self._settings.hud_puerto),
                process_request=self._http,
                max_size=MAX_TAMANO_MENSAJE,
                server_header=None,
                close_timeout=2,
            )
        except OSError as exc:
            if _es_puerto_ocupado(exc):
                log.warning("HUD sin conexión: el puerto %d está ocupado", self._settings.hud_puerto)
                return self._registrar_propio(EstadoModulo.SIN_CONEXION, "puerto ocupado")
            if _es_puerto_prohibido(exc):
                log.warning("HUD sin conexión: el puerto %d no está permitido", self._settings.hud_puerto)
                return self._registrar_propio(EstadoModulo.SIN_CONEXION, "puerto no permitido")
            log.warning("HUD sin conexión: no se pudo escuchar en %s:%d (%s)", HOST,
                        self._settings.hud_puerto, exc.strerror or type(exc).__name__)
            return self._registrar_propio(EstadoModulo.SIN_CONEXION,
                                          f"no se pudo escuchar ({exc.strerror or type(exc).__name__})")
        except Exception as exc:  # error inesperado de la librería: el HUD no tumba el sistema
            log.warning("HUD sin conexión: error al iniciar el servidor (%s)", type(exc).__name__)
            return self._registrar_propio(EstadoModulo.SIN_CONEXION, f"error al iniciar ({type(exc).__name__})")

        self._servidor = servidor
        self.puerto_real = self._puerto_de(servidor)
        log.info("HUD escuchando en %s:%s", HOST, self.puerto_real)
        # CONTRATO: con RAMS_HUD_OPEN=1 es main quien llama a abrir_navegador() tras el arranque.
        return self._diagnostico_activo()

    async def cerrar(self) -> None:
        """Avisa a los clientes (``apagado``), cierra el servidor y suelta la suscripción al bus.

        Idempotente. Espera como máximo unos segundos al cierre de las conexiones.
        """
        for desuscribir in self._desuscripciones:
            try:
                desuscribir()
            except Exception as exc:
                log.debug("Error al desuscribir el HUD del bus: %s", exc)
        self._desuscripciones = []
        for limite in self._limites.values():
            if limite.temporizador is not None:
                limite.temporizador.cancel()
                limite.temporizador = None
            limite.pendiente = None

        servidor = self._servidor
        self._servidor = None
        if servidor is not None:
            if self._clientes and not self._apagado_enviado:
                with self._lock:
                    motivo = (self._apagado or {}).get("motivo", "cierre")
                self._difundir(_serializar("apagado", {"motivo": motivo}))
                self._apagado_enviado = True
            try:
                servidor.close(close_connections=True)
            except Exception as exc:
                log.debug("Error al cerrar el servidor del HUD: %s", exc)
            try:
                await asyncio.wait_for(servidor.wait_closed(), timeout=ESPERA_CIERRE_S)
            except TimeoutError:
                log.debug("El cierre de las conexiones del HUD tardó más de %.0f s", ESPERA_CIERRE_S)
            except Exception as exc:
                log.debug("Error al esperar el cierre del HUD: %s", exc)
            log.info("HUD cerrado")

        # El lanzamiento del navegador NO se cancela: el navegador ya va desacoplado de RAMS
        # (setsid --fork o Popen en sesión nueva) y solo queda comprobar cómo salió el lanzador.
        # Se espera de forma acotada y, si algo se quedara colgado, se cancela la espera (nunca el
        # navegador: ningún objeto de asyncio lo tiene como hijo).
        tareas = [t for t in self._tareas_navegador if not t.done()]
        if tareas:
            _hechas, pendientes = await asyncio.wait(tareas, timeout=ESPERA_NAVEGADOR_S + 1.0)
            for tarea in pendientes:
                tarea.cancel()
        self._tareas_navegador.clear()
        self._clientes.clear()
        self.puerto_real = None

    def abrir_navegador(self) -> bool:
        """Abre el HUD en el navegador. Devuelve ``True`` si se lanzó (o ya se acaba de lanzar).

        Linux: ``chromium --app=URL --class=amadeus-hud`` (o ``chromium-browser``/``google-chrome``)
        si existe, por ``create_subprocess_exec`` y sin shell; si no, ``xdg-open URL``. En ambos
        casos el navegador se lanza desacoplado de RAMS (ver :meth:`_lanzar_navegador`): sobrevive
        al apagado y nunca se mata. Otros sistemas: ``webbrowser.open(url)``. Nunca registra la URL
        con token.
        """
        url = self.url
        if url is None:
            log.info("El HUD no está escuchando: no se abre el navegador")
            return False
        ahora = time.monotonic()
        # Dos llamadas seguidas (p. ej. RAMS_HUD_OPEN=1 y un reintento) no abren dos ventanas.
        if self._navegador_abierto_ts is not None and ahora - self._navegador_abierto_ts < 10.0:
            log.debug("El navegador del HUD se abrió hace menos de 10 s; no se repite")
            return True

        if _plataforma().startswith("linux"):
            argv = self._argv_navegador_linux(url)
            if argv is None:
                log.info("No encontré chromium, chromium-browser, google-chrome ni xdg-open para abrir el HUD")
                return False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                log.info("No se abre el navegador del HUD: hace falta el loop asyncio en ejecución")
                return False
            tarea = loop.create_task(self._lanzar_navegador(argv), name="rams-hud-navegador")
            self._tareas_navegador.add(tarea)
            tarea.add_done_callback(self._tareas_navegador.discard)
            self._navegador_abierto_ts = ahora
            return True

        try:
            import webbrowser  # stdlib; se importa aquí para mantener barato el import del módulo

            abierto = bool(webbrowser.open(url, new=1))
        except Exception as exc:  # el mensaje podría contener la URL: solo se registra el tipo
            log.info("No se pudo abrir el navegador para el HUD (%s)", type(exc).__name__)
            return False
        if abierto:
            self._navegador_abierto_ts = ahora
            log.info("HUD abierto en el navegador predeterminado")
        else:
            log.info("El sistema no tiene un navegador disponible para abrir el HUD")
        return abierto

    # ------------------------------------------------------------------
    # Bus → estado interno → clientes
    # ------------------------------------------------------------------

    def _suscribir(self) -> None:
        """Se suscribe a los eventos que observa el HUD (síncrono y barato)."""
        self._desuscripciones = [self._bus.suscribir(nombre, self._al_evento) for nombre in EVENTOS_SUSCRITOS]

    def _al_evento(self, evento: Evento) -> None:
        """Manejador síncrono del bus: guarda el estado y reenvía el mensaje a los clientes."""
        try:
            nombre = evento.nombre
            datos: Mapping[str, Any] = evento.datos if isinstance(evento.datos, Mapping) else {}
            with self._lock:
                if nombre == "sistema.modulo":
                    modulo = _normalizar_modulo(datos)
                    if modulo["clave"]:
                        self._diagnosticos[modulo["clave"]] = modulo
                elif nombre == "sistema.arranque":
                    inicio = _real(datos.get("inicio_ts"))
                    if inicio is not None:
                        self._inicio_ts_evento = inicio
                elif nombre == "sistema.apagado":
                    self._apagado = {"motivo": _texto(datos.get("motivo") or "usuario", 40)}
                elif nombre == "permiso.solicitado":
                    traducido = traducir_evento(nombre, datos)
                    self._permiso_pendiente = traducido[1] if traducido else None
                elif nombre == "permiso.resuelto":
                    pendiente = self._permiso_pendiente
                    if pendiente is None or pendiente.get("id") in ("", _texto(datos.get("id"), 64)):
                        self._permiso_pendiente = None
                if nombre in _NOMBRES_INSTANTANEA:
                    self._ultimos[nombre] = evento

            if self._servidor is None or not self._clientes:
                return  # nadie mirando: basta con el estado guardado para la instantánea
            if nombre == "sistema.arranque":
                mensaje: tuple[str, dict[str, Any]] | None = ("hola", self._datos_hola())
            else:
                mensaje = traducir_evento(nombre, datos)
            if mensaje is not None:
                self._programar_envio(mensaje[0], mensaje[1], nombre)
        except Exception as exc:  # nunca propaga al bus
            log.debug("Error al procesar %s para el HUD: %s", getattr(evento, "nombre", "?"), exc)

    def _programar_envio(self, tipo: str, datos: dict[str, Any], nombre: str) -> None:
        """Envía en el hilo del loop del servidor (delegando con ``call_soon_threadsafe`` si hace falta)."""
        loop = self._loop
        if loop is None or self._servidor is None or loop.is_closed():
            return
        if threading.get_ident() == self._hilo_loop:
            self._enviar(tipo, datos, nombre)
            return
        try:
            loop.call_soon_threadsafe(self._enviar, tipo, datos, nombre)
        except RuntimeError as exc:
            log.debug("Loop del HUD cerrado; se descarta %s (%s)", tipo, exc)

    def _enviar(self, tipo: str, datos: dict[str, Any], nombre: str = "") -> None:
        """Aplica los límites de frecuencia y difunde el mensaje a todos los clientes."""
        if self._servidor is None:
            return
        if nombre == "voz.fin":  # que una boca diferida no reabra la onda tras terminar de hablar
            limite_boca = self._limites.get("boca")
            if limite_boca is not None and limite_boca.temporizador is not None:
                limite_boca.temporizador.cancel()
                limite_boca.temporizador = None
                limite_boca.pendiente = None
        if not self._clientes:
            return
        if tipo == "log":
            ahora = time.monotonic()
            if ahora - self._log_ventana_inicio >= 1.0:
                self._log_ventana_inicio = ahora
                self._log_en_ventana = 0
            if self._log_en_ventana >= MAX_LOG_POR_S:
                self.descartados_log += 1
                return
            self._log_en_ventana += 1
        if tipo == "apagado":
            self._apagado_enviado = True
        limite = self._limites.get(tipo)
        if limite is None:
            self._difundir(_serializar(tipo, datos))
            return
        if limite.temporizador is not None:  # ya hay un envío diferido: gana el dato más reciente
            limite.pendiente = datos
            return
        ahora = time.monotonic()
        espera = limite.intervalo - (ahora - limite.ultimo_envio)
        if espera <= 0:
            limite.ultimo_envio = ahora
            self._difundir(_serializar(tipo, datos))
            return
        loop = self._loop
        if loop is None:
            return
        limite.pendiente = datos
        limite.temporizador = loop.call_later(espera, self._vaciar_limite, tipo)

    def _vaciar_limite(self, tipo: str) -> None:
        """Envía el mensaje diferido más reciente de un tipo limitado."""
        limite = self._limites.get(tipo)
        if limite is None:
            return
        datos = limite.pendiente
        limite.pendiente = None
        limite.temporizador = None
        if datos is None or self._servidor is None or not self._clientes:
            return
        limite.ultimo_envio = time.monotonic()
        self._difundir(_serializar(tipo, datos))

    def _difundir(self, texto: str) -> None:
        """Escribe ``texto`` en todos los clientes abiertos (sin esperar; descarta a los saturados)."""
        if not self._clientes or _ws_broadcast is None:
            return
        destino = []
        for conexion in list(self._clientes):
            transporte = getattr(conexion, "transport", None)
            try:
                pendiente = transporte.get_write_buffer_size() if transporte is not None else 0
            except Exception as exc:
                log.debug("No se pudo leer el búfer de un cliente del HUD: %s", exc)
                pendiente = 0
            if pendiente > MAX_BUFFER_CLIENTE:
                log.info("Cliente del HUD saturado (%d bytes sin enviar); se desconecta", pendiente)
                self._clientes.discard(conexion)
                try:
                    transporte.abort()
                except Exception as exc:
                    log.debug("Error al abortar un cliente saturado: %s", exc)
                continue
            destino.append(conexion)
        if not destino:
            return
        try:
            _ws_broadcast(destino, texto, raise_exceptions=True)
        except Exception as exc:  # incluye ExceptionGroup: algún cliente no pudo recibir
            log.debug("Difusión parcial del HUD: %s", exc)

    # ------------------------------------------------------------------
    # HTTP y WebSocket
    # ------------------------------------------------------------------

    def _http(self, conexion: Any, solicitud: Any) -> Any:
        """``process_request``: sirve el HTML, valida ``/ws`` y responde 204/404 al resto."""
        try:
            partes = urlsplit(solicitud.path)
            ruta = partes.path
            if ruta in ("/", "/index.html"):
                return self._respuesta_html()
            if ruta == "/ws":
                return self._validar_ws(conexion, solicitud, partes.query)
            if ruta == "/favicon.ico":
                return self._respuesta_vacia(HTTPStatus.NO_CONTENT)
            return conexion.respond(HTTPStatus.NOT_FOUND, "No encontrado\n")
        except Exception as exc:
            log.debug("Error al procesar una petición HTTP del HUD: %s", type(exc).__name__)
            return conexion.respond(HTTPStatus.INTERNAL_SERVER_ERROR, "Error interno\n")

    def _validar_ws(self, conexion: Any, solicitud: Any, consulta: str) -> Any:
        """Token (``hmac.compare_digest``) y ``Origin`` permitidos; ``None`` sigue el *handshake*."""
        valores = parse_qs(consulta, keep_blank_values=True).get("t") or [""]
        token = valores[0]
        if not hmac.compare_digest(token.encode("utf-8"), self.token.encode("utf-8")):
            self._registrar_rechazo("token inválido")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        try:
            origen = solicitud.headers.get("Origin")
        except Exception as exc:  # p. ej. varias cabeceras Origin
            log.debug("Cabecera Origin inválida en el HUD: %s", type(exc).__name__)
            origen = None
        if origen is None or origen not in self._origenes_permitidos():
            log.debug("Origin rechazado en el HUD: %r", _texto(origen, 80))
            self._registrar_rechazo("Origin no permitido")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        if len(self._clientes) >= MAX_CLIENTES:
            self._registrar_rechazo("demasiados clientes")
            return conexion.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Demasiados clientes\n")
        return None

    def _origenes_permitidos(self) -> frozenset[str]:
        """``{http://127.0.0.1:<p>, http://localhost:<p>}`` con el puerto real."""
        puerto = self.puerto_real
        if puerto is None:
            return frozenset()
        return frozenset({f"http://127.0.0.1:{puerto}", f"http://localhost:{puerto}"})

    def _registrar_rechazo(self, motivo: str) -> None:
        """Registra conexiones rechazadas sin inundar el log (como mucho una línea cada 10 s)."""
        ahora = time.monotonic()
        if ahora - self._rechazos_ultimo_log >= 10.0:
            extra = f" (+{self._rechazos_sin_log} más)" if self._rechazos_sin_log else ""
            log.info("Conexión al HUD rechazada: %s%s", motivo, extra)
            self._rechazos_ultimo_log = ahora
            self._rechazos_sin_log = 0
        else:
            self._rechazos_sin_log += 1

    def _cabeceras_base(self, longitud: int | None) -> list[tuple[str, str]]:
        """Cabeceras de transporte comunes (fecha, cierre y longitud)."""
        cabeceras = [("Date", email.utils.formatdate(usegmt=True)), ("Connection", "close")]
        if longitud is not None:
            cabeceras.append(("Content-Length", str(longitud)))
        return cabeceras

    def _respuesta_html(self) -> Any:
        """200 con el HTML del HUD y las cabeceras de seguridad exactas de §10.23."""
        cuerpo = self._html if self._html is not None else _PAGINA_SIN_HTML
        cabeceras = self._cabeceras_base(len(cuerpo)) + [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", CSP),
        ]
        return _WsResponse(HTTPStatus.OK.value, HTTPStatus.OK.phrase, _WsHeaders(cabeceras), cuerpo)

    def _respuesta_vacia(self, estado: HTTPStatus) -> Any:
        """Respuesta sin cuerpo (204 para el favicon)."""
        return _WsResponse(estado.value, estado.phrase, _WsHeaders(self._cabeceras_base(None)), b"")

    async def _manejar(self, conexion: Any) -> None:
        """Ciclo de vida de un cliente: ``hola`` + instantánea, y luego solo controles permitidos."""
        if self._servidor is None:
            return
        iniciales = self._mensajes_iniciales()
        if _ws_broadcast is None:
            return
        try:
            for texto in iniciales:  # escritura síncrona y en orden: nada se cuela antes del hola
                _ws_broadcast([conexion], texto, raise_exceptions=True)
        except Exception as exc:
            log.debug("No se pudo enviar la instantánea a un cliente del HUD: %s", exc)
            return
        self._clientes.add(conexion)
        log.debug("Cliente del HUD conectado (%d en total)", len(self._clientes))
        ventana_inicio = time.monotonic()
        en_ventana = 0
        try:
            async for crudo in conexion:
                ahora = time.monotonic()
                if ahora - ventana_inicio >= 1.0:
                    ventana_inicio = ahora
                    en_ventana = 0
                en_ventana += 1
                if en_ventana > MAX_MENSAJES_CLIENTE_S:
                    self.descartados_cliente += 1
                    continue
                accion = self._interpretar_control(crudo)
                if accion is None:
                    continue
                log.info("Control desde el HUD: %s", accion)
                self._bus.publicar("hud.control", {"accion": accion}, origen="hud")
                await conexion.send(_serializar("control_ack", {"accion": accion, "ok": True}))
        except _WsConnectionClosed:
            log.debug("Cliente del HUD desconectado")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Error con un cliente del HUD: %s", exc)
        finally:
            self._clientes.discard(conexion)

    @staticmethod
    def _interpretar_control(crudo: Any) -> str | None:
        """Devuelve la acción si el mensaje es exactamente un control permitido; si no, ``None``."""
        if not isinstance(crudo, str) or len(crudo) > 1024:
            return None
        try:
            mensaje = json.loads(crudo)
        except ValueError:
            return None
        if not isinstance(mensaje, dict) or mensaje.get("tipo") != "control":
            return None
        accion = mensaje.get("accion")
        if isinstance(accion, str) and accion in CONTROLES_PERMITIDOS:
            return accion
        return None

    def _mensajes_iniciales(self) -> list[str]:
        """``hola``, instantánea de los últimos eventos, diagnósticos, permiso pendiente y apagado."""
        mensajes = [_serializar("hola", self._datos_hola())]
        with self._lock:
            ultimos = dict(self._ultimos)
            diagnosticos = [dict(d) for d in self._diagnosticos.values()]
            permiso = dict(self._permiso_pendiente) if self._permiso_pendiente else None
            apagado = dict(self._apagado) if self._apagado else None
        for nombre, _tipo in INSTANTANEA:
            evento = self._bus.ultimo(nombre) or ultimos.get(nombre)
            if evento is None:
                continue
            traducido = traducir_evento(nombre, evento.datos)
            if traducido is not None:
                mensajes.append(_serializar(*traducido))
        for diagnostico in diagnosticos:
            mensajes.append(_serializar("modulo", diagnostico))
        if permiso is not None:
            mensajes.append(_serializar("permiso", permiso))
        if apagado is not None:
            mensajes.append(_serializar("apagado", apagado))
        return mensajes

    def _datos_hola(self) -> dict[str, Any]:
        """Datos del mensaje ``hola``: versión, nombre de la IA, trato del usuario e inicio."""
        return {
            "version": VERSION,
            "nombre_ia": self._settings.nombre_ia,
            "usuario": self._settings.trato_usuario,
            "inicio_ts": self._inicio_ts(),
        }

    def _inicio_ts(self) -> float:
        """Inicio de la sesión: ``sistema.arranque`` del bus o, si aún no existe, la construcción del HUD."""
        evento = self._bus.ultimo("sistema.arranque")
        if evento is not None and isinstance(evento.datos, Mapping):
            inicio = _real(evento.datos.get("inicio_ts"))
            if inicio is not None:
                return inicio
        with self._lock:
            if self._inicio_ts_evento is not None:
                return self._inicio_ts_evento
        return self._creado_ts

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _leer_html(self) -> bytes | None:
        """Lee ``amadeus_hud.html`` (acotado; < 1 s). ``None`` si no existe o no se puede leer."""
        try:
            return self._ruta_html.read_bytes()
        except FileNotFoundError:
            log.warning("No existe %s: el HUD mostrará una página de aviso", self._ruta_html.name)
        except OSError as exc:
            log.warning("No se pudo leer %s: %s", self._ruta_html.name, exc)
        return None

    @staticmethod
    def _puerto_de(servidor: Any) -> int | None:
        """Puerto real en el que escucha el servidor (útil con ``hud_puerto=0``)."""
        for sock in getattr(servidor, "sockets", None) or ():
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError, ValueError) as exc:
                log.debug("No se pudo leer el puerto del HUD: %s", exc)
        return None

    def _diagnostico_activo(self) -> DiagnosticoModulo:
        """Diagnóstico del servidor en marcha: ``OK`` (o ``DEGRADADO`` sin HTML) con la URL sin token."""
        url_publica = f"http://{HOST}:{self.puerto_real}/"
        if self._html is None:
            return self._registrar_propio(EstadoModulo.DEGRADADO, f"falta {NOMBRE_HTML} · {url_publica}")
        return self._registrar_propio(EstadoModulo.OK, url_publica)

    def _registrar_propio(self, estado: EstadoModulo, detalle: str) -> DiagnosticoModulo:
        """Crea el diagnóstico del HUD y lo guarda para la instantánea (main es quien lo publica)."""
        diagnostico = DiagnosticoModulo("hud", "HUD", estado, detalle)
        with self._lock:
            self._diagnosticos["hud"] = _normalizar_modulo(diagnostico.a_dict())
        return diagnostico

    def _argv_navegador_linux(self, url: str) -> list[str] | None:
        """``argv`` para abrir el HUD en Linux (sin shell), o ``None`` si no hay navegador."""
        for nombre in _NAVEGADORES_APP:
            ruta = shutil.which(nombre)
            if ruta:
                return [ruta, f"--app={url}", "--class=amadeus-hud"]
        ruta = shutil.which("xdg-open")
        if ruta:
            return [ruta, url]
        return None

    async def _lanzar_navegador(self, argv: Iterable[str]) -> None:
        """Lanza el navegador **desacoplado** de RAMS (sin shell) y registra el resultado.

        Si no había otra instancia, la ventana ``--app`` es la primera de Chromium y en ella acaban
        las ventanas que el usuario abra después. Por eso el navegador nunca queda como hijo directo
        dentro de un transporte de asyncio: ``BaseSubprocessTransport.close()`` (al que también llama
        su ``__del__`` cuando se recolecta) mata con ``SIGKILL`` al hijo que siga vivo, es decir, a
        todo el navegador del usuario al apagar RAMS.

        1. Con ``setsid`` (util-linux): ``create_subprocess_exec(setsid, "--fork", *argv)``. El hijo
           directo sale al instante y el navegador queda en su propia sesión, adoptado por init.
        2. Sin ``setsid``, o si no admite ``--fork`` (p. ej. busybox): ``subprocess.Popen`` con
           ``start_new_session=True`` en un hilo; ``Popen`` no mata al hijo al recolectarse.
        """
        argumentos = list(argv)
        nombre = Path(argumentos[0]).name
        setsid = shutil.which("setsid")
        if setsid and await self._lanzar_con_setsid([setsid, "--fork", *argumentos]):
            log.info("HUD abierto con %s", nombre)
            return
        try:
            codigo = await asyncio.to_thread(_lanzar_popen_desacoplado, argumentos)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.info("No se pudo lanzar %s para el HUD (%s)", nombre, type(exc).__name__)
            self._navegador_abierto_ts = None
            return
        if codigo:
            log.info("%s terminó con código %d al abrir el HUD", nombre, codigo)
            return
        log.info("HUD abierto con %s", nombre)

    @staticmethod
    async def _lanzar_con_setsid(argv: list[str]) -> bool:
        """Ejecuta ``setsid --fork …`` por ``create_subprocess_exec`` (sin shell).

        ``True`` si el lanzador salió con 0 (el navegador ya corre fuera de RAMS) o si, de forma
        anómala, sigue vivo (no se reintenta para no abrir dos ventanas). ``False`` si ``setsid`` no
        se pudo ejecutar o no admite ``--fork``: entonces se usa ``Popen`` desacoplado.
        """
        try:
            proceso = await asyncio.create_subprocess_exec(
                *argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            log.debug("No se pudo ejecutar setsid para el HUD (%s)", type(exc).__name__)
            return False
        try:
            codigo = await asyncio.wait_for(proceso.wait(), timeout=ESPERA_SETSID_S)
        except TimeoutError:
            # Con --fork el navegador es un nieto en otra sesión: aunque el transporte matara
            # algún día a este lanzador, el navegador no se ve afectado.
            log.debug("setsid no terminó en %.0f s; se da el HUD por lanzado", ESPERA_SETSID_S)
            return True
        if codigo != 0:
            log.debug("setsid --fork salió con código %d; se lanza con Popen desacoplado", codigo)
            return False
        return True
