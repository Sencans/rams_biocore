"""Compuerta de permisos para toda acción sobre el PC (§9.1, §10.10; v0.2: §C2.11 y §C2.12).

Qué hace: decide, con código determinista y sin LLM, si una :class:`AccionPC` se puede
ejecutar. Implementa la tabla normativa de §9.1 ampliada en la v0.2 (se evalúa de arriba abajo
y gana la primera fila que aplique) y la mecánica de confirmación: publica
``permiso.solicitado``, pregunta en la terminal local con ``⚠ Confirmación requerida: …
¿Procedo? [s/N]``, espera la respuesta (la siguiente línea de la terminal con :meth:`resolver`
o un botón del chat de escritorio con :meth:`resolver_por_id`; el timeout equivale a "no") y
publica ``permiso.resuelto``.

Reglas de seguridad que aplica:
- Las acciones nunca nacen del texto del LLM ni de Telegram: ``origen`` ``llm`` o
  ``telegram`` ⇒ denegada siempre.
- Por iniciativa propia solo hay ``vistazo`` y ``travesura_cursor`` (v0.2).
- Solo hay **una** confirmación pendiente a la vez; otra solicitud se deniega.
- ``cerrar_ventana`` exige una dirección de Hyprland válida y un ``pid`` que no sea de
  R.A.M.S. (ni su terminal ni sus ancestros) y **solo** se confirma desde la terminal.
- v0.2: ``abrir_destino``/``buscar_en`` solo con claves del catálogo ``DESTINOS_BASE``;
  ``prerrellenar``/``escribir_texto`` piden confirmación, nunca con textos que parezcan
  secretos (``config.parece_secreto``) ni contra ventanas propias (``pids_propios`` ∪
  ``pids_extra()``); ``travesura_cursor`` exige el interruptor maestro ``RAMS_TRAVESURAS`` **y**
  el estado efectivo de main (``travesuras_activas()``).
- La ``descripcion`` de una solicitud **nunca** lleva el texto que se va a escribir: el texto
  viaja aparte como ``vista_previa`` en ``permiso.solicitado`` (lo muestra el chat de escritorio
  y la consola local; ningún ``log`` lo registra).
- v0.2.1 (órdenes desde Telegram, apps del menú Inicio y fotos):
  * El origen ``telegram`` sigue denegado siempre. Lo que el USUARIO pide por Telegram llega
    con ``origen="usuario"`` y ``args["canal"] = "telegram"``; desde ese canal solo valen, y solo
    con ``RAMS_TELEGRAM_ACCIONES=1``, ``abrir_destino``, ``buscar_en``, ``media``, ``lanzar_app``
    de apps YA permitidas y ``enviar_foto``. Por Telegram nada pide confirmación: lo que la
    necesitaría se deniega (nadie en el PC debe aprobar lo que pidió otro).
  * ``lanzar_app`` con ``args["local"] is True`` (app del menú Inicio): el nivel lo da el índice
    de ``ControlPC`` (callback ``nivel_app``): ``permitida`` → sin preguntar; ``descubierta`` →
    confirmar (main recuerda la aprobación); ``sensible`` (consolas y herramientas del sistema) →
    confirmar cada vez; desconocida → denegada.
  * ``enviar_foto`` (``fuente`` cámara o pantalla, a Telegram del usuario): solo ``origen``
    usuario (nunca iniciativa ni LLM), por terminal, escritorio o Telegram (este último con
    ``RAMS_TELEGRAM_FOTOS=1``), como mucho 10 por hora.
- v0.2.2 (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §5): tipos extra de Google, correo, Drive y taller.
  * ``conectar_google``, ``drive_descargar`` y ``drive_crear_carpeta``: solo el usuario en un canal
    local y con ``tiene_google``. ``desconectar_google``: confirmación clásica (60 s).
  * **Tarjetas aprobables** (``TIPOS_ACCION_TARJETA``: ``enviar_correo``, ``drive_subir``,
    ``drive_papelera``, ``drive_compartir``, ``ejecutar_script``): solo con el botón de SU tarjeta en el
    canal ``escritorio`` (``args.aprobacion == "tarjeta"``), con la tarjeta vigente, del mismo tipo y con
    la MISMA huella (``hmac.compare_digest``). La compuerta guarda el registro de tarjetas (una por
    clase, en memoria) y ``autorizar`` consume la tarjeta dentro del lock: un doble clic no ejecuta dos
    veces. ``enviar_correo`` además reserva su envío en las ventanas de tasa (10 min y 24 h); main
    devuelve la reserva con :meth:`liberar_envio_correo` si nada pudo salir.
  * Compartir en público exige doble confirmación (≤ 120 s entre clics) y nunca con edición; un
    script de riesgo crítico nunca se ejecuta. La ``descripcion`` de estas acciones nunca lleva
    contenido (cuerpo, direcciones, nombres de archivo ni código).

Dependencias opcionales: ``psutil`` (para calcular los procesos propios: el propio PID y sus
ancestros). Degradación: sin psutil, solo ``os.getpid()``; sin bus no publica eventos; sin
callback ``preguntar`` usa ``print``.

La compuerta se limita a la mecánica: Amadeus confirma «qué, dónde, cuántos y qué se pierde»
sin sarcasmo, pero eso lo verbalizan main y Broca.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import logging
import math
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

try:  # opcional: ancestros del proceso para no cerrar la terminal de R.A.M.S.
    import psutil
except ImportError:  # sin psutil: solo el PID propio
    psutil = None

from core.bus import BusEventos
from core.config import Settings, redactar
from core.contracts import (
    ORIGENES_ACCION,
    TIPOS_ACCION,
    AccionPC,
    DecisionPermiso,
    formatear_decimal,
)

try:  # v0.2 (grupo base): catálogo de destinos y patrones de travesura
    from core.contracts import DESTINOS_BASE, PATRONES_TRAVESURA, SAFE_ALIAS_RE
except ImportError:  # contratos v0.1: sin destinos ni travesuras (las filas v0.2 deniegan)
    DESTINOS_BASE = {}  # type: ignore[assignment]
    PATRONES_TRAVESURA = ()  # type: ignore[assignment]
    SAFE_ALIAS_RE = re.compile(r"^[a-z0-9_]{1,24}$")  # type: ignore[misc]

try:  # v0.2 (grupo base): heurística anti-contraseñas compartida con lobes/control_pc.py
    from core.config import parece_secreto
except ImportError:  # sin la función compartida: lo prudente es tratar todo texto como secreto
    def parece_secreto(texto: Any, secretos: Any = ()) -> bool:  # type: ignore[misc]
        return True

try:  # v0.2.2 (grupo base): catálogos de Drive, tarjetas y taller
    from core.contracts import (
        DESTINOS_DESCARGA,
        FORMATOS_DRIVE,
        HUELLA_RE,
        ID_TARJETA_RE,
        NIVELES_EJECUTABLES,
        ROLES_DRIVE,
        TTL_DOBLE_CONFIRMACION_S,
    )
except ImportError:  # contratos v0.2.1: los valores de la spec (§3.1)
    DESTINOS_DESCARGA = ("descargas", "documentos", "escritorio")  # type: ignore[misc]
    FORMATOS_DRIVE = ("pdf", "texto", "original")  # type: ignore[misc]
    ROLES_DRIVE = ("reader", "commenter", "writer")  # type: ignore[misc]
    NIVELES_EJECUTABLES = frozenset({"bajo", "medio", "alto"})  # type: ignore[misc]
    TTL_DOBLE_CONFIRMACION_S = 120.0  # type: ignore[misc]
    ID_TARJETA_RE = re.compile(r"^[0-9a-f]{8}$")  # type: ignore[misc]
    HUELLA_RE = re.compile(r"^[0-9a-f]{64}$")  # type: ignore[misc]

__all__ = [
    "CLASE_SEGURA_RE",
    "DIRECCION_RE",
    "DIRECCION_V02_RE",
    "RESPUESTAS_SI",
    "MOTIVOS_VISTAZO",
    "ORDENES_MEDIA",
    "TIPOS_INICIATIVA",
    "NOMBRES_DESTINO",
    "MAX_TEXTO_ACCION",
    "MAX_CONSULTA_ACCION",
    "MAX_SEGUNDOS_TRAVESURA",
    "TIPOS_ACCION_EXTRA",
    "ACCIONES_POR_TELEGRAM",
    "FUENTES_FOTO",
    "MAX_FOTOS_HORA",
    "NIVELES_APP",
    "SAFE_CLAVE_APP_RE",
    "TIPOS_ACCION_TARJETA",
    "CLASE_DE_ACCION",
    "VENTANA_CORREO_CORTA_S",
    "VENTANA_CORREO_DIA_S",
    "CANALES_ACCION",
    "SolicitudPermiso",
    "TarjetaAprobable",
    "calcular_pids_propios",
    "nombre_destino",
    "sanear_texto_accion",
    "CompuertaPermisos",
]

log = logging.getLogger("rams.permisos")

# ---------------------------------------------------------------------------
# Constantes (§10.10)
# ---------------------------------------------------------------------------

CLASE_SEGURA_RE: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
DIRECCION_RE: Final = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
RESPUESTAS_SI: Final = frozenset({"s", "si", "sí", "y", "yes", "procede", "dale"})

MOTIVOS_VISTAZO: Final = frozenset({"usuario", "curiosidad", "presencia"})
ORIGENES_VISTAZO: Final = frozenset({"usuario", "iniciativa"})
ORDENES_MEDIA: Final = frozenset({"play_pause", "siguiente", "anterior"})

# v0.2 (§C2.11): lo único que se puede hacer por iniciativa propia.
TIPOS_INICIATIVA: Final = frozenset({"vistazo", "travesura_cursor"})
# Dirección de ventana de Hyprland en el objetivo de escribir_texto (minúsculas, §C2.11).
DIRECCION_V02_RE: Final = re.compile(r"^0x[0-9a-f]{1,16}$")
ID_SOLICITUD_RE: Final = re.compile(r"^[0-9a-f]{8}$")
MAX_TEXTO_ACCION: Final = 500           # prerrellenar / escribir_texto (§C2.12 "Longitudes")
MAX_CONSULTA_ACCION: Final = 120        # buscar_en
MAX_SEGUNDOS_TRAVESURA: Final = 0.6
ORIGENES_TRAVESURA: Final = frozenset({"usuario", "iniciativa"})

# v0.2.1: tipos que aún no están en contracts.TIPOS_ACCION (los evalúa solo esta compuerta).
# v0.2.2 (§5.1): Google, correo, Drive y taller. TIPOS_ACCION (13) no cambia.
TIPOS_ACCION_EXTRA: Final = frozenset({
    "enviar_foto",
    "conectar_google", "desconectar_google",
    "enviar_correo",
    "drive_descargar", "drive_crear_carpeta", "drive_subir", "drive_papelera", "drive_compartir",
    "ejecutar_script",
})
# Acciones que solo se aprueban con el botón de su tarjeta (§5.3) y la clase de tarjeta de cada una.
TIPOS_ACCION_TARJETA: Final = frozenset({"enviar_correo", "drive_subir", "drive_papelera", "drive_compartir",
                                         "ejecutar_script"})
CLASE_DE_ACCION: Final[Mapping[str, str]] = {"enviar_correo": "correo", "drive_subir": "drive",
                                             "drive_papelera": "drive", "drive_compartir": "drive",
                                             "ejecutar_script": "script"}
VENTANA_CORREO_CORTA_S: Final = 600.0
VENTANA_CORREO_DIA_S: Final = 86400.0
MAX_TTL_TARJETA_S: Final = 86400.0
MAX_IDS_RETIRADAS: Final = 32
CANALES_ORIGEN_TARJETA: Final = frozenset({"terminal", "escritorio", "telegram"})
_CANALES_LOCALES_ACCION: Final = frozenset({"terminal", "escritorio"})
_TIPOS_GOOGLE_LOCALES: Final = frozenset({"conectar_google", "desconectar_google", "drive_descargar",
                                          "drive_crear_carpeta"})
_RE_NOMBRE_CARPETA_PROHIBIDO: Final = re.compile(r"[/\\\x00-\x1f\x7f]")
DESCRIPCION_DESCONECTAR_GOOGLE: Final = "Desconectar tu cuenta de Google (revoca el permiso de Gmail y Drive)"
# Lo único que se puede pedir por Telegram (con RAMS_TELEGRAM_ACCIONES=1; las fotos además con
# RAMS_TELEGRAM_FOTOS=1). Mismo conjunto que core.intenciones.ACCIONES_TELEGRAM + enviar_foto.
ACCIONES_POR_TELEGRAM: Final = frozenset({"abrir_destino", "buscar_en", "lanzar_app", "media", "enviar_foto"})
CANALES_ACCION: Final = frozenset({"terminal", "escritorio", "telegram"})
FUENTES_FOTO: Final = frozenset({"camara", "pantalla"})
MAX_FOTOS_HORA: Final = 10
VENTANA_FOTOS_S: Final = 3600.0
# Apps del menú Inicio (clave del índice de ControlPC) y sus niveles.
SAFE_CLAVE_APP_RE: Final = re.compile(r"^[a-z0-9_]{1,40}$")
NIVELES_APP: Final = ("permitida", "descubierta", "sensible")

# Nombre legible de cada clave de DESTINOS_BASE (para descripciones y frases; nunca para abrir).
NOMBRES_DESTINO: Final[Mapping[str, str]] = {
    "whatsapp": "WhatsApp", "telegram": "Telegram", "x": "X", "gmail": "Gmail", "correo": "el correo",
    "instagram": "Instagram", "tiktok": "TikTok", "facebook": "Facebook", "discord": "Discord",
    "linkedin": "LinkedIn", "youtube": "YouTube", "spotify": "Spotify", "navegador": "el navegador",
    "archivos": "el explorador de archivos",
}

_ORIGENES_PROHIBIDOS: Final = frozenset({"llm", "telegram"})
# Solo dígitos ASCII: con \d (Unicode) se aceptarían '٣' o '０５０', y el ejecutor
# (aura_vision_pc._entero_arg) exige isascii(); compuerta y ejecutor deben coincidir.
_RE_ENTERO: Final = re.compile(r"^[0-9]{1,4}$")
_RE_SALTOS: Final = re.compile(r"[\r\n\t]+")
_RE_EXE_SEGURO: Final = re.compile(r"[^A-Za-z0-9_.\- ]+")

# Motivos de la confirmación (§9.1 "Confirmación").
MOTIVO_CONFIRMADA: Final = "confirmada por el usuario"
MOTIVO_RECHAZADA: Final = "rechazada por el usuario"
MOTIVO_CANCELADA: Final = "confirmación cancelada"
MOTIVO_OTRA_EN_CURSO: Final = "otra confirmación en curso"
MOTIVO_CONFIRMADA_BOTON: Final = "confirmada con el botón del chat"
MOTIVO_RECHAZADA_BOTON: Final = "rechazada con el botón del chat"


def nombre_destino(alias: Any) -> str:
    """Nombre legible de una clave de ``DESTINOS_BASE`` (``"whatsapp"`` → ``"WhatsApp"``)."""
    clave = str(alias or "")
    return NOMBRES_DESTINO.get(clave, clave or "esa app")


def _saneador_control_pc() -> Callable[[Any, int], str | None] | None:
    """``lobes.control_pc._sanear`` (import perezoso: ``control_pc`` importa este módulo) o ``None``."""
    try:
        from lobes.control_pc import _sanear
    except Exception as exc:  # control_pc ausente o roto: queda el saneado mínimo de abajo
        log.debug("Sin el saneado de ControlPC (%s); uso el mínimo", type(exc).__name__)
        return None
    return _sanear if callable(_sanear) else None


def sanear_texto_accion(texto: Any) -> str:
    """Texto de ``prerrellenar``/``escribir_texto`` saneado **igual que lo escribe** ``ControlPC``.

    Reutiliza ``control_pc._sanear`` (NFC, controles → espacio, invisibles borrados, espacios
    colapsados) para que la vista previa y el recuento de la confirmación coincidan con lo que
    se teclea o prerrellena. Sin ``control_pc``: ``\\r\\n\\t`` → espacio y ``strip``. Nunca hay
    saltos de línea: en un chat equivalen a pulsar Enter, y Amadeus nunca envía nada.
    """
    if not isinstance(texto, str):
        return ""
    sanear = _saneador_control_pc()
    if sanear is not None:
        try:
            limpio = sanear(texto, sys.maxsize)
        except Exception as exc:  # defensa: nunca rompe la compuerta
            log.debug("Saneado de ControlPC fallido (%s); uso el mínimo", type(exc).__name__)
        else:
            return limpio if isinstance(limpio, str) else ""
    return _RE_SALTOS.sub(" ", texto).strip()


def _sin_tildes(texto: str) -> str:
    """Quita los diacríticos (NFD sin la categoría Mn)."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


_SI_NORMALIZADAS: Final = frozenset(_sin_tildes(r) for r in RESPUESTAS_SI)


def _es_si(respuesta: str) -> bool:
    """``True`` si la respuesta (``strip().lower()`` y sin tildes) es exactamente un "sí"."""
    if not isinstance(respuesta, str):
        return False
    return _sin_tildes(respuesta.strip().lower()) in _SI_NORMALIZADAS


def _entero(valor: Any) -> int | None:
    """Entero estricto: ``int`` (no ``bool``) o texto de dígitos; cualquier otra cosa ⇒ ``None``."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and _RE_ENTERO.fullmatch(valor.strip()):
        return int(valor.strip())
    return None


def _real(valor: Any) -> float | None:
    """Real finito estricto: ``int``/``float`` (no ``bool``); cualquier otra cosa ⇒ ``None``."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    numero = float(valor)
    return numero if math.isfinite(numero) else None


def _texto_corto(valor: Any, maximo: int = 40) -> str:
    """Texto de un argumento para un motivo legible (truncado y sin saltos de línea)."""
    texto = " ".join(str(valor).split())
    return texto if len(texto) <= maximo else texto[: maximo - 1] + "…"


def _segundos(valor: float) -> str:
    """Duración legible para el usuario: ``60.0 → "60"``, ``0.05 → "0,05"``."""
    if valor >= 1:
        return formatear_decimal(valor, 0)
    return formatear_decimal(valor, 2)


def calcular_pids_propios() -> frozenset[int]:
    """PIDs de R.A.M.S.: ``{os.getpid()}`` ∪ sus ancestros (terminal, shell…) según psutil.

    Sin psutil, o si psutil falla, devuelve solo ``{os.getpid()}``. Nunca lanza.
    """
    pids = {os.getpid()}
    if psutil is None:
        log.debug("psutil no está instalado: los procesos propios son solo el PID actual")
        return frozenset(pids)
    try:
        for padre in psutil.Process().parents():
            pids.add(int(padre.pid))
    except Exception as exc:  # psutil.Error, OSError, permisos…
        log.debug("No se pudieron leer los procesos ancestros (%s)", type(exc).__name__)
    return frozenset(pids)


@dataclass(frozen=True)
class SolicitudPermiso:
    """Confirmación pendiente de una acción destructiva."""

    id: str                 # uuid4().hex[:8]
    accion: AccionPC
    creada_ts: float


@dataclass(frozen=True)
class TarjetaAprobable:
    """Tarjeta aprobable registrada en la compuerta (v0.2.2, §5.4). Solo metadatos: nunca el contenido."""

    id: str                  # ^[0-9a-f]{8}$ (lo genera el módulo dueño del contenido)
    clase: str               # CLASE_DE_ACCION[tipo_accion]
    tipo_accion: str         # ∈ TIPOS_ACCION_TARJETA
    huella: str              # ^[0-9a-f]{64}$
    version: int             # 1, 2, … (sube con cada edición o reapertura)
    creada: float            # reloj() (monotónico inyectable)
    caduca: float            # reloj() + ttl_s
    canal_origen: str        # "terminal" | "escritorio" | "telegram"
    doble: bool = False      # compartir en público: exige dos clics
    doble_ok_hasta: float | None = None   # 1.er clic dado: vale hasta este reloj()


class CompuertaPermisos:
    """Compuerta única de permisos para el control del PC (§9.1 y §C2.11).

    ``evaluar`` es pura (tabla de §9.1 ampliada); ``autorizar`` añade la confirmación
    interactiva. La siguiente línea que escriba el usuario en la terminal local se pasa a
    :meth:`resolver`; los botones del chat de escritorio llegan a :meth:`resolver_por_id`.

    v0.2: ``pids_extra`` devuelve los PIDs de Amadeus Desktop (y descendientes) que tampoco
    son un objetivo válido de ``escribir_texto``; ``travesuras_activas`` es el estado efectivo
    de las travesuras que lleva main (§C2.14.4). Por defecto: ``frozenset()`` y ``False``.

    v0.2.1: ``nivel_app(clave) -> (nivel, nombre) | None`` es el índice de apps del menú Inicio
    de ``ControlPC`` (sin él, toda app local se deniega); ``reloj`` mide el tope de fotos por hora.
    """

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 preguntar: Callable[[str], None] | None = None, timeout_s: float = 60.0,
                 pids_propios: frozenset[int] | None = None,
                 pids_extra: Callable[[], frozenset[int]] | None = None,
                 travesuras_activas: Callable[[], bool] | None = None,
                 nivel_app: Callable[[str], tuple[str, str] | None] | None = None,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.bus = bus
        self._nivel_app: Callable[[str], tuple[str, str] | None] | None = nivel_app
        self._reloj: Callable[[], float] = reloj
        self._fotos: deque[float] = deque()
        self._preguntar: Callable[[str], None] | None = preguntar
        try:
            self.timeout_s = max(0.0, float(timeout_s))
        except (TypeError, ValueError):
            log.warning("timeout_s inválido para la compuerta; uso 60 s")
            self.timeout_s = 60.0
        if pids_propios is None:
            self.pids_propios: frozenset[int] = calcular_pids_propios()
        else:
            self.pids_propios = frozenset(int(p) for p in pids_propios)
        self._pids_extra: Callable[[], frozenset[int]] | None = pids_extra
        self._travesuras_activas: Callable[[], bool] | None = travesuras_activas
        # Reentrante (v0.2.2): ``autorizar`` evalúa y consume una tarjeta dentro del MISMO lock, y la
        # evaluación vuelve a leer el registro (y las ventanas de tasa) con él.
        self._lock = threading.RLock()
        self._pendiente: SolicitudPermiso | None = None
        self._futuro: asyncio.Future[tuple[bool, str]] | None = None
        # v0.2.2 (§5.4): registro de tarjetas aprobables (solo en memoria) y reservas de envío de correo.
        self._tarjetas: dict[str, TarjetaAprobable] = {}
        self._versiones_retiradas: dict[str, int] = {}      # id → última version (≤ 32, orden de inserción)
        self._correos: deque[tuple[float, str]] = deque()   # (reloj(), id de la tarjeta)

    # ------------------------------------------------------------------
    # Estado inyectado (v0.2)
    # ------------------------------------------------------------------

    def pids_extra(self) -> frozenset[int]:
        """PIDs extra que no son objetivo válido (``frozenset()`` si no hay callback o falla)."""
        fuente = self._pids_extra
        if fuente is None:
            return frozenset()
        try:
            valor = fuente()
            return frozenset(int(p) for p in (valor or ()) if not isinstance(p, bool))
        except Exception as exc:  # un callback roto nunca abre la compuerta: se trata como vacío
            log.debug("pids_extra no disponible (%s)", type(exc).__name__)
            return frozenset()

    def travesuras_activas(self) -> bool:
        """Estado efectivo de las travesuras según main (``False`` si no hay callback o falla)."""
        fuente = self._travesuras_activas
        if fuente is None:
            return False
        try:
            return bool(fuente())
        except Exception as exc:  # ante la duda, sin travesuras
            log.debug("travesuras_activas no disponible (%s)", type(exc).__name__)
            return False

    def nivel_app(self, clave: str) -> tuple[str, str] | None:
        """``(nivel, nombre)`` de una app del menú Inicio según ControlPC, o ``None``.

        Un callback ausente, roto o que devuelve algo mal formado cuenta como "no la conozco"
        (se deniega): nunca abre la compuerta.
        """
        fuente = self._nivel_app
        if fuente is None:
            return None
        try:
            valor = fuente(clave)
        except Exception as exc:
            log.debug("nivel_app no disponible (%s)", type(exc).__name__)
            return None
        if (isinstance(valor, tuple) and len(valor) == 2 and valor[0] in NIVELES_APP
                and isinstance(valor[1], str)):
            return str(valor[0]), _texto_corto(valor[1], 60) or clave
        return None

    def set_nivel_app(self, nivel_app: Callable[[str], tuple[str, str] | None] | None) -> None:
        """Cambia el índice de apps locales (``ControlPC.nivel_app_local``)."""
        self._nivel_app = nivel_app

    def fotos_ultima_hora(self) -> int:
        """Fotos autorizadas en la última hora (ventana deslizante con ``reloj``)."""
        ahora = self._reloj()
        with self._lock:
            while self._fotos and ahora - self._fotos[0] >= VENTANA_FOTOS_S:
                self._fotos.popleft()
            return len(self._fotos)

    def _anotar_foto(self) -> None:
        with self._lock:
            self._fotos.append(self._reloj())

    # ------------------------------------------------------------------
    # v0.2.2: registro de tarjetas aprobables (§5.4)
    # ------------------------------------------------------------------

    def reloj(self) -> float:
        """El reloj de la compuerta (monotónico, inyectable): main lo usa para traducir ``caduca`` a
        ``time.time()`` en la tarjeta (``caduca_ts``)."""
        return self._reloj()

    def registrar_tarjeta(self, id: str, *, tipo_accion: str, huella: str, ttl_s: float,  # noqa: A002
                          canal_origen: str, doble: bool = False
                          ) -> tuple[TarjetaAprobable | None, TarjetaAprobable | None]:
        """``(nueva, reemplazada)``. Valida los formatos (``(None, None)`` si algo no cumple).

        Hay una tarjeta por clase: una nueva reemplaza a la de su clase (que se devuelve para que main
        la cierre como ``reemplazado``). Con una id ya vista (vigente o retirada: reapertura tras un
        fallo o tras ejecutar) sigue la numeración (``version`` = última + 1) y, como no reemplaza a
        otra, ``reemplazada`` es ``None``. Recuerda la última versión de como mucho 32 ids retiradas.
        ``ttl_s`` ∈ (0, 86400]. Thread-safe.
        """
        if not (isinstance(id, str) and ID_TARJETA_RE.fullmatch(id)):
            return None, None
        if tipo_accion not in TIPOS_ACCION_TARJETA:
            return None, None
        if not (isinstance(huella, str) and HUELLA_RE.fullmatch(huella)):
            return None, None
        ttl = _real(ttl_s)
        if ttl is None or not 0.0 < ttl <= MAX_TTL_TARJETA_S:
            return None, None
        if canal_origen not in CANALES_ORIGEN_TARJETA or not isinstance(doble, bool):
            return None, None
        clase = CLASE_DE_ACCION[tipo_accion]
        ahora = self._reloj()
        with self._lock:
            previa = self._tarjetas.get(id)
            if previa is not None:
                version = previa.version + 1
            else:
                version = self._versiones_retiradas.pop(id, 0) + 1
            reemplazada: TarjetaAprobable | None = None
            for otra in list(self._tarjetas.values()):
                if otra.clase == clase and otra.id != id:
                    reemplazada = self._retirar_sin_lock(otra.id)
            nueva = TarjetaAprobable(id=id, clase=clase, tipo_accion=tipo_accion, huella=huella, version=version,
                                     creada=ahora, caduca=ahora + ttl, canal_origen=canal_origen, doble=doble)
            self._tarjetas[id] = nueva
        log.debug("Tarjeta %s registrada (versión %d)", clase, version)
        return nueva, reemplazada

    def actualizar_tarjeta(self, id: str, huella: str) -> TarjetaAprobable | None:  # noqa: A002
        """Edición: nueva huella y ``version+1``; renueva la caducidad (misma ttl) y borra el 1.er clic."""
        if not (isinstance(huella, str) and HUELLA_RE.fullmatch(huella)):
            return None
        ahora = self._reloj()
        with self._lock:
            t = self._vigente_sin_lock(id, ahora)
            if t is None:
                return None
            ttl = max(0.0, t.caduca - t.creada)
            nueva = dataclasses.replace(t, huella=huella, version=t.version + 1, creada=ahora, caduca=ahora + ttl,
                                        doble_ok_hasta=None)
            self._tarjetas[t.id] = nueva
            return nueva

    def marcar_primera_confirmacion(self, id: str, huella: str) -> TarjetaAprobable | None:  # noqa: A002
        """Paso 1 de "compartir en público": solo si ``t.doble`` y la huella coincide; el 2.º clic vale
        ``TTL_DOBLE_CONFIRMACION_S`` (120 s) y la tarjeta sube de versión."""
        if not isinstance(huella, str):
            return None
        ahora = self._reloj()
        with self._lock:
            t = self._vigente_sin_lock(id, ahora)
            if t is None or not t.doble:
                return None
            if not hmac.compare_digest(t.huella.encode("ascii"), huella.encode("ascii", "replace")):
                return None
            nueva = dataclasses.replace(t, version=t.version + 1,
                                        doble_ok_hasta=ahora + float(TTL_DOBLE_CONFIRMACION_S))
            self._tarjetas[t.id] = nueva
            return nueva

    def tarjeta(self, id: str) -> TarjetaAprobable | None:  # noqa: A002
        """La tarjeta ``id`` si está registrada y vigente; si no, ``None``."""
        with self._lock:
            return self._vigente_sin_lock(id, self._reloj())

    def tarjetas(self) -> tuple[TarjetaAprobable, ...]:
        """Las tarjetas vigentes, de la más vieja a la más nueva."""
        ahora = self._reloj()
        with self._lock:
            vigentes = [t for t in self._tarjetas.values() if ahora < t.caduca]
        return tuple(sorted(vigentes, key=lambda t: t.creada))

    def retirar_tarjeta(self, id: str) -> TarjetaAprobable | None:  # noqa: A002
        """Quita la tarjeta ``id`` (vigente o caducada) y la devuelve; recuerda su versión."""
        if not isinstance(id, str):
            return None
        with self._lock:
            return self._retirar_sin_lock(id)

    def purgar_caducadas(self) -> tuple[TarjetaAprobable, ...]:
        """Quita y devuelve las tarjetas vencidas (main las cierra como ``caducado``)."""
        ahora = self._reloj()
        with self._lock:
            vencidas = [t for t in self._tarjetas.values() if ahora >= t.caduca]
            for t in vencidas:
                self._retirar_sin_lock(t.id)
        return tuple(vencidas)

    def correos_recientes(self) -> tuple[int, int]:
        """Envíos de correo reservados en los últimos 600 s y en las últimas 86 400 s."""
        ahora = self._reloj()
        with self._lock:
            while self._correos and ahora - self._correos[0][0] >= VENTANA_CORREO_DIA_S:
                self._correos.popleft()
            return self._conteo_correos_sin_lock(ahora)

    def liberar_envio_correo(self, id: str) -> bool:  # noqa: A002
        """Quita la reserva de tasa que ``autorizar()`` hizo para la tarjeta ``id`` (la más reciente con
        esa id dentro de la ventana de 86 400 s). ``True`` si había una. Idempotente y thread-safe: main
        la llama solo cuando el envío falló sin que nada pudiera salir (``ResultadoEnvio.pudo_salir``)."""
        if not isinstance(id, str):
            return False
        ahora = self._reloj()
        with self._lock:
            for indice in range(len(self._correos) - 1, -1, -1):
                ts, ident = self._correos[indice]
                if ahora - ts >= VENTANA_CORREO_DIA_S:
                    break
                if ident == id:
                    del self._correos[indice]
                    log.debug("Reserva de envío de correo liberada")
                    return True
        return False

    def _vigente_sin_lock(self, id: Any, ahora: float) -> TarjetaAprobable | None:  # noqa: A002
        if not isinstance(id, str):
            return None
        t = self._tarjetas.get(id)
        return t if t is not None and ahora < t.caduca else None

    def _retirar_sin_lock(self, id: str) -> TarjetaAprobable | None:  # noqa: A002
        t = self._tarjetas.pop(id, None)
        if t is not None:
            self._versiones_retiradas.pop(id, None)
            self._versiones_retiradas[id] = t.version
            while len(self._versiones_retiradas) > MAX_IDS_RETIRADAS:
                self._versiones_retiradas.pop(next(iter(self._versiones_retiradas)))
        return t

    def _conteo_correos_sin_lock(self, ahora: float) -> tuple[int, int]:
        corta = sum(1 for ts, _id in self._correos if ahora - ts < VENTANA_CORREO_CORTA_S)
        dia = sum(1 for ts, _id in self._correos if ahora - ts < VENTANA_CORREO_DIA_S)
        return corta, dia

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------

    def set_preguntar(self, preguntar: Callable[[str], None]) -> None:
        """Cambia el callback que muestra la pregunta de confirmación (p. ej. la consola)."""
        self._preguntar = preguntar

    # ------------------------------------------------------------------
    # Evaluación pura (tabla de §9.1)
    # ------------------------------------------------------------------

    def evaluar(self, accion: AccionPC) -> DecisionPermiso:
        """Aplica la tabla de §9.1 en orden. Pura: no publica, no pregunta y no ejecuta.

        ``aprobada`` es ``True`` solo cuando el veredicto es ``"permitida"``; un veredicto
        ``"confirmar"`` necesita :meth:`autorizar`. Nunca lanza: una acción mal formada se
        deniega.
        """
        try:
            return self._evaluar(accion)
        except Exception as exc:  # defensa: una acción exótica nunca rompe la compuerta
            log.warning("Error al evaluar una acción (%s); se deniega", type(exc).__name__)
            return DecisionPermiso("denegada", "no pude evaluar la acción", aprobada=False)

    def _evaluar(self, accion: AccionPC) -> DecisionPermiso:
        if not isinstance(accion, AccionPC):
            return self._denegada("acción mal formada")
        tipo = accion.tipo
        origen = accion.origen
        args: Mapping[str, Any] = accion.args if isinstance(accion.args, Mapping) else {}

        # 1. Origen: nunca del LLM ni de Telegram; orígenes desconocidos tampoco.
        if origen == "llm":
            return self._denegada("las acciones nunca nacen del texto del modelo de lenguaje")
        if origen == "telegram":
            return self._denegada("desde Telegram no se puede actuar sobre el computador")
        if origen not in ORIGENES_ACCION or origen in _ORIGENES_PROHIBIDOS:
            return self._denegada("origen de la acción desconocido")

        # 2. Tipo soportado (v0.2.1: más los de TIPOS_ACCION_EXTRA).
        if tipo not in TIPOS_ACCION and tipo not in TIPOS_ACCION_EXTRA:
            return self._denegada("tipo de acción no soportado")

        # 3-4. Restricciones por origen (v0.2: por iniciativa, solo vistazo y travesura_cursor).
        if origen == "iniciativa" and tipo not in TIPOS_INICIATIVA:
            return self._denegada("por iniciativa propia solo puedo echar un vistazo por la cámara "
                                  "o hacer una travesura con el cursor")
        if origen == "nexus" and tipo != "enfocar_nexus":
            return self._denegada("desde Nexus IDE solo se permite traer su ventana al frente")

        # 4b. v0.2.1: canal desde el que el usuario lo pidió (Telegram solo lo de bajo riesgo).
        canal = args.get("canal")
        if canal is not None and canal not in CANALES_ACCION:
            return self._denegada("canal de la acción desconocido")
        remota = canal == "telegram"
        if remota:
            if tipo not in ACCIONES_POR_TELEGRAM:
                return self._denegada("desde Telegram solo puedo abrir apps y webs, buscar, pausar o pasar la "
                                      "música y mandarte fotos; lo demás pídemelo en el computador")
            if tipo != "enviar_foto" and not getattr(self.settings, "telegram_acciones", False):
                return self._denegada("las acciones desde Telegram están desactivadas (RAMS_TELEGRAM_ACCIONES=1 "
                                      "para activarlas)")

        # 4c. v0.2.1: fotos de la cámara o de la pantalla a Telegram.
        if tipo == "enviar_foto":
            return self._evaluar_foto(origen, args, remota)

        # 4d. v0.2.2 (§5.2-§5.3): cuenta de Google, Drive local y acciones con tarjeta. Ninguna pasa
        # por Telegram, iniciativa, Nexus ni el LLM (las filas 1-4b ya las denegaron).
        if tipo in TIPOS_ACCION_TARJETA:
            return self._evaluar_tarjeta(tipo, origen, args)
        if tipo in _TIPOS_GOOGLE_LOCALES:
            return self._evaluar_google(tipo, origen, args)

        # 5. Filas nuevas de la v0.2 (§C2.11), en orden.
        if tipo in ("abrir_destino", "buscar_en", "prerrellenar", "escribir_texto", "travesura_cursor"):
            return self._evaluar_v02(tipo, origen, args)

        # 5+. Por tipo.
        if tipo == "vistazo":
            motivo = args.get("motivo")
            if motivo not in MOTIVOS_VISTAZO:
                return self._denegada("motivo de vistazo no válido")
            if origen not in ORIGENES_VISTAZO:
                return self._denegada("ese origen no puede pedir un vistazo de cámara")
            return self._permitida("vistazo de cámara permitido")

        if tipo == "lanzar_app" and args.get("local") is True:
            return self._evaluar_app_local(origen, args, remota)

        if tipo == "lanzar_app":
            alias = args.get("alias")
            if isinstance(alias, str) and alias in self.settings.apps_permitidas:
                return self._permitida(f"«{alias}» está en la lista permitida")
            return self._denegada(f"«{_texto_corto(alias)}» no está en la lista permitida RAMS_APPS")

        if tipo == "enfocar_nexus":
            return self._permitida("traer Nexus IDE al frente está permitido")

        if tipo == "ir_workspace":
            n = _entero(args.get("n"))
            if n is not None and 1 <= n <= 10:
                return self._permitida(f"espacio de trabajo {n} permitido")
            return self._denegada("el espacio de trabajo debe ser un entero entre 1 y 10")

        if tipo == "volumen":
            nivel = _entero(args.get("nivel"))
            if nivel is not None and 0 <= nivel <= 100:
                return self._permitida(f"volumen al {nivel} % permitido")
            return self._denegada("el volumen debe ser un entero entre 0 y 100")

        if tipo == "media":
            orden = args.get("orden")
            if orden in ORDENES_MEDIA:
                return self._permitida("control multimedia permitido")
            return self._denegada("orden multimedia no válida")

        if tipo == "captura_pantalla":
            if origen == "usuario":
                return self._permitida("captura de pantalla pedida por el usuario")
            return self._denegada("la captura de pantalla solo la puede pedir el usuario")

        if tipo == "cerrar_ventana":
            direccion = args.get("direccion")
            if not (isinstance(direccion, str) and DIRECCION_RE.fullmatch(direccion)):
                return self._denegada("no sé qué ventana cerrar", destructiva=True)
            pid = args.get("pid")
            if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
                return self._denegada("identificador de proceso no válido", destructiva=True)
            if pid is not None and pid in self.pids_propios:
                return self._denegada("esa ventana es la terminal de R.A.M.S.", destructiva=True)
            return DecisionPermiso("confirmar", "cerrar una ventana requiere confirmación",
                                   destructiva=True, aprobada=False)

        # Inalcanzable mientras TIPOS_ACCION y esta tabla estén alineados.
        return self._denegada("tipo de acción no soportado")

    def _evaluar_foto(self, origen: str, args: Mapping[str, Any], remota: bool) -> DecisionPermiso:
        """Fila ``enviar_foto`` (v0.2.1): el usuario, una fuente válida, opt-in por Telegram y ≤ 10/h."""
        if origen != "usuario":
            return self._denegada("solo tú puedes pedirme una foto (nunca por mi cuenta)")
        fuente = args.get("fuente")
        if fuente not in FUENTES_FOTO:
            return self._denegada("solo puedo mandar una foto de la cámara o de la pantalla")
        if remota and not getattr(self.settings, "telegram_fotos", False):
            return self._denegada("las fotos por Telegram están desactivadas (RAMS_TELEGRAM_FOTOS=1 para "
                                  "activarlas)")
        if self.fotos_ultima_hora() >= MAX_FOTOS_HORA:
            return self._denegada(f"ya te mandé {MAX_FOTOS_HORA} fotos en la última hora; espera un poco")
        que = "de la cámara" if fuente == "camara" else "de la pantalla"
        return self._permitida(f"foto {que} pedida por el usuario")

    def _tiene_google(self) -> bool:
        return bool(getattr(self.settings, "tiene_google", False))

    def _evaluar_google(self, tipo: str, origen: str, args: Mapping[str, Any]) -> DecisionPermiso:
        """Filas ``conectar_google``, ``desconectar_google``, ``drive_descargar`` y ``drive_crear_carpeta``."""
        if origen != "usuario":
            return self._denegada("solo tú puedes pedirme eso")
        if args.get("canal") not in _CANALES_LOCALES_ACCION:
            return self._denegada("eso solo se pide desde el computador")
        if tipo == "desconectar_google":
            return DecisionPermiso("confirmar", "desconectar tu cuenta de Google requiere confirmación",
                                   destructiva=True, aprobada=False)
        if not self._tiene_google():
            return self._denegada("la conexión con Google está apagada (RAMS_GOOGLE=1 para activarla) o estoy "
                                  "en modo sin red")
        if tipo == "conectar_google":
            return self._permitida("conectar tu cuenta de Google: la pantalla de consentimiento de Google es la "
                                   "confirmación")
        if tipo == "drive_descargar":
            if args.get("destino") not in DESTINOS_DESCARGA:
                return self._denegada("solo descargo en Descargas, Documentos o el Escritorio")
            formato = args.get("formato")
            if formato is not None and formato not in FORMATOS_DRIVE:
                return self._denegada("formato de descarga no válido (pdf, texto u original)")
            return self._permitida("descargar de tu Drive está permitido")
        nombre = args.get("nombre")
        if (not isinstance(nombre, str) or not nombre.strip() or len(nombre) > 120
                or _RE_NOMBRE_CARPETA_PROHIBIDO.search(nombre)):
            return self._denegada("el nombre de la carpeta debe tener entre 1 y 120 caracteres, sin / ni \\")
        return self._permitida("crear una carpeta en tu Drive está permitido")

    def _evaluar_tarjeta(self, tipo: str, origen: str, args: Mapping[str, Any]) -> DecisionPermiso:
        """Fila común de tarjeta (§5.3) y los requisitos propios de cada tipo (§5.2). Pura."""
        if origen != "usuario":
            return self._denegada("solo tú puedes aprobar eso, con el botón de su tarjeta")
        if args.get("canal") != "escritorio":
            return self._denegada("eso solo se aprueba con el botón de la tarjeta en el computador")
        if args.get("aprobacion") != "tarjeta":
            return self._denegada("eso solo se aprueba con el botón de su tarjeta")
        ident, sha = args.get("tarjeta"), args.get("sha256")
        if not (isinstance(ident, str) and ID_TARJETA_RE.fullmatch(ident)
                and isinstance(sha, str) and HUELLA_RE.fullmatch(sha)):
            return self._denegada("la aprobación no trae una tarjeta válida")
        t = self.tarjeta(ident)
        if t is None:
            return self._denegada("esa tarjeta ya no está vigente")
        if t.tipo_accion != tipo:
            return self._denegada("esa tarjeta es de otra acción")
        if not hmac.compare_digest(t.huella.encode("ascii"), sha.encode("ascii")):
            return self._denegada("el contenido cambió desde que lo viste")
        if tipo == "ejecutar_script":
            if not getattr(self.settings, "tiene_taller", False):
                return self._denegada("el taller de scripts está apagado (RAMS_TALLER=1 para activarlo)")
            if args.get("nivel") not in NIVELES_EJECUTABLES:
                return self._denegada("riesgo crítico: no lo ejecuto desde la app")
            return self._permitida("script aprobado con la tarjeta")
        if not self._tiene_google():
            return self._denegada("la conexión con Google está apagada (RAMS_GOOGLE=1 para activarla) o estoy "
                                  "en modo sin red")
        if tipo == "enviar_correo":
            n = args.get("n_destinatarios")
            maximo = int(getattr(self.settings, "correo_max_destinatarios", 10) or 0)
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= maximo:
                return self._denegada(f"un correo va a entre 1 y {maximo} destinatarios")
            with self._lock:
                corta, dia = self._conteo_correos_sin_lock(self._reloj())
            max_corta = int(getattr(self.settings, "correo_max_10min", 5) or 0)
            max_dia = int(getattr(self.settings, "correo_max_dia", 30) or 0)
            if corta >= max_corta:
                return self._denegada(f"ya salieron {max_corta} correos en 10 minutos: espera un poco")
            if dia >= max_dia:
                return self._denegada(f"ya salieron {max_dia} correos hoy: es el tope diario")
            return self._permitida("correo aprobado con la tarjeta")
        if tipo in ("drive_subir", "drive_papelera"):
            return self._permitida("operación de Drive aprobada con la tarjeta")
        # drive_compartir
        rol, publico = args.get("rol"), args.get("publico")
        if rol not in ROLES_DRIVE:
            return self._denegada("permiso de Drive no válido")
        if not isinstance(publico, bool):
            return self._denegada("no sé si es para compartir en público o con una persona")
        if publico and rol != "reader":
            return self._denegada("público y editable nunca: un enlace público solo puede ser de lectura")
        if publico != t.doble:
            return self._denegada("lo que se aprueba no coincide con la tarjeta")
        if publico:
            if args.get("doble_confirmacion") is not True:
                return self._denegada("compartir en público necesita la segunda confirmación")
            if t.doble_ok_hasta is None or self._reloj() >= t.doble_ok_hasta:
                return self._denegada("la primera confirmación venció: vuelve a pulsar Compartir")
        return self._permitida("compartir aprobado con la tarjeta")

    def _evaluar_app_local(self, origen: str, args: Mapping[str, Any], remota: bool) -> DecisionPermiso:
        """Fila ``lanzar_app`` local (app del menú Inicio, v0.2.1), según el nivel del índice."""
        if origen != "usuario":
            return self._denegada("solo el usuario puede pedirme que abra una app")
        clave = args.get("alias")
        if not (isinstance(clave, str) and SAFE_CLAVE_APP_RE.fullmatch(clave)):
            return self._denegada("no sé qué app abrir")
        nivel = self.nivel_app(clave)
        if nivel is None:
            return self._denegada("no encuentro esa app en el menú Inicio")
        grado, nombre = nivel
        if grado == "permitida":
            return self._permitida(f"«{nombre}» ya está aprobada")
        if remota:
            if grado == "sensible":
                return self._denegada(f"«{nombre}» es una consola o una herramienta del sistema: solo la abro "
                                      "desde el computador y con tu confirmación")
            return self._denegada(f"«{nombre}» aún no está aprobada: ábrela una vez desde el computador (te "
                                  "pediré confirmación) y luego podrás pedírmela desde aquí")
        if grado == "sensible":
            return DecisionPermiso("confirmar", f"«{nombre}» es una herramienta del sistema: requiere confirmación",
                                   destructiva=False, aprobada=False)
        return DecisionPermiso("confirmar", f"abrir «{nombre}» por primera vez requiere confirmación",
                               destructiva=False, aprobada=False)

    def _evaluar_v02(self, tipo: str, origen: str, args: Mapping[str, Any]) -> DecisionPermiso:
        """Filas de la v0.2 (§C2.11): destinos, prerrelleno, tecleo y travesuras."""
        if tipo == "travesura_cursor":
            if origen not in ORIGENES_TRAVESURA:
                return self._denegada("ese origen no puede pedir una travesura")
            if not getattr(self.settings, "travesuras_habilitadas", False):
                return self._denegada("las travesuras con el cursor están desactivadas en la configuración "
                                      "(RAMS_TRAVESURAS=1)")
            if not self.travesuras_activas():
                return self._denegada("las travesuras están apagadas ahora mismo")
            if args.get("patron") not in PATRONES_TRAVESURA:
                return self._denegada("patrón de travesura no válido")
            amplitud = _entero(args.get("amplitud_px"))
            maximo = int(getattr(self.settings, "travesura_amplitud_max_px", 40) or 0)
            if amplitud is None or not 1 <= amplitud <= maximo:
                return self._denegada(f"la amplitud de la travesura debe estar entre 1 y {maximo} px")
            segundos = _real(args.get("segundos"))
            if segundos is None or not 0.0 < segundos <= MAX_SEGUNDOS_TRAVESURA:
                return self._denegada("una travesura dura como mucho 0,6 s")
            return self._permitida("travesura de cursor permitida (guardas de estado en ControlPC)")

        # El resto de filas v0.2 solo las puede pedir el usuario (canal terminal o escritorio).
        if origen != "usuario":
            return self._denegada("solo el usuario puede pedir esa acción")

        if tipo == "escribir_texto":
            return self._evaluar_escribir(args)

        alias = args.get("alias")
        if not (isinstance(alias, str) and SAFE_ALIAS_RE.fullmatch(alias) and alias in DESTINOS_BASE):
            return self._denegada(f"«{_texto_corto(alias, 24)}» no está en el catálogo de destinos")
        meta = DESTINOS_BASE[alias]
        nombre = nombre_destino(alias)

        if tipo == "abrir_destino":
            return self._permitida(f"abrir {nombre} está permitido")

        if tipo == "buscar_en":
            if not meta.get("busqueda"):
                return self._denegada(f"en {nombre} no puedo buscar directamente")
            consulta = args.get("consulta")
            if not isinstance(consulta, str) or not 1 <= len(consulta.strip()) <= MAX_CONSULTA_ACCION:
                return self._denegada(f"la búsqueda debe tener entre 1 y {MAX_CONSULTA_ACCION} caracteres")
            if redactar(consulta, self.settings.secretos(), alta_entropia=True) != consulta:
                return self._denegada("la búsqueda parece contener una clave o un secreto")
            return self._permitida(f"buscar en {nombre} está permitido")

        # prerrellenar
        if not meta.get("prerrelleno"):
            return self._denegada(f"{nombre} no admite borradores prerrellenados")
        texto = sanear_texto_accion(args.get("texto"))
        if not 1 <= len(texto) <= MAX_TEXTO_ACCION:
            return self._denegada(f"el borrador debe tener entre 1 y {MAX_TEXTO_ACCION} caracteres")
        if parece_secreto(texto, self.settings.secretos()):
            return self._denegada("ese texto parece una contraseña, un código o una clave; no lo escribo")
        return DecisionPermiso("confirmar", "un borrador prerrellenado requiere confirmación",
                               destructiva=False, aprobada=False)

    def _evaluar_escribir(self, args: Mapping[str, Any]) -> DecisionPermiso:
        """Fila ``escribir_texto``: objetivo ajeno y válido, texto corto y que no parezca un secreto."""
        pid = args.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return self._denegada("no sé en qué ventana escribir (falta el proceso de destino)")
        if pid in self.pids_propios or pid in self.pids_extra():
            return self._denegada("esa ventana es de Amadeus o de R.A.M.S.; no escribo ahí")
        hwnd = args.get("hwnd")
        direccion = args.get("direccion")
        hwnd_ok = isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd > 0
        direccion_ok = isinstance(direccion, str) and bool(DIRECCION_V02_RE.fullmatch(direccion))
        if not (hwnd_ok or direccion_ok):
            return self._denegada("no sé en qué ventana escribir (falta el identificador de la ventana)")
        texto = sanear_texto_accion(args.get("texto"))
        if not 1 <= len(texto) <= MAX_TEXTO_ACCION:
            return self._denegada(f"el texto debe tener entre 1 y {MAX_TEXTO_ACCION} caracteres")
        if parece_secreto(texto, self.settings.secretos()):
            return self._denegada("ese texto parece una contraseña, un código o una clave; no lo escribo")
        return DecisionPermiso("confirmar", "escribir en otra ventana requiere confirmación",
                               destructiva=False, aprobada=False)

    @staticmethod
    def _permitida(motivo: str) -> DecisionPermiso:
        return DecisionPermiso("permitida", motivo, destructiva=False, aprobada=True)

    @staticmethod
    def _denegada(motivo: str, *, destructiva: bool = False) -> DecisionPermiso:
        return DecisionPermiso("denegada", motivo, destructiva=destructiva, aprobada=False)

    # ------------------------------------------------------------------
    # Autorización con confirmación
    # ------------------------------------------------------------------

    async def autorizar(self, accion: AccionPC) -> DecisionPermiso:
        """Decisión final (``.aprobada``) para ``accion``; pregunta si es destructiva.

        - ``permitida`` ⇒ ``aprobada=True``.
        - ``denegada`` ⇒ ``log.info`` y ``aprobada=False``.
        - ``confirmar`` ⇒ publica ``permiso.solicitado``, llama a ``preguntar``, espera a
          :meth:`resolver` (como mucho ``timeout_s``; el silencio equivale a "no"), publica
          ``permiso.resuelto`` y devuelve ``DecisionPermiso("confirmar", motivo,
          destructiva=True, aprobada=<respuesta>)``.

        Si ya hay otra confirmación pendiente, la nueva se deniega ("otra confirmación en
        curso"). La cancelación de la tarea que espera se propaga, limpia la pendiente y
        publica ``permiso.resuelto`` con ``aprobado=False`` y motivo "confirmación cancelada".

        v0.2: para ``prerrellenar``/``escribir_texto`` la descripción la escribe la compuerta
        (nunca lleva el texto) y ``permiso.solicitado`` añade ``vista_previa`` (el texto, ≤ 500);
        la consola local la muestra en pantalla, pero ningún ``log`` la registra.

        v0.2.2: las acciones con tarjeta (``TIPOS_ACCION_TARJETA``) se evalúan y, si se permiten, su
        tarjeta se RETIRA dentro del lock (un solo uso: un doble clic no ejecuta dos veces); con
        ``enviar_correo`` se reserva además el envío en las ventanas de tasa. Nunca publican
        ``permiso.solicitado``: la tarjeta ya fue la confirmación.
        """
        tipo = getattr(accion, "tipo", "?")
        if tipo in TIPOS_ACCION_TARJETA:
            return self._autorizar_tarjeta(accion)
        decision = self.evaluar(accion)
        if decision.veredicto == "permitida":
            log.debug("Acción %s permitida: %s", tipo, decision.motivo)
            if tipo == "enviar_foto":
                self._anotar_foto()
            return decision
        if decision.veredicto != "confirmar":
            log.info("Acción %s denegada: %s", tipo, decision.motivo)
            return decision
        args_accion = accion.args if isinstance(accion.args, Mapping) else {}
        if args_accion.get("canal") == "telegram":
            # Defensa en profundidad: lo pedido por Telegram nunca se confirma en el PC.
            log.info("Acción %s denegada: pedida por Telegram y requiere confirmación", tipo)
            return DecisionPermiso("denegada", "eso necesita tu confirmación en el computador",
                                   destructiva=decision.destructiva, aprobada=False)

        loop = asyncio.get_running_loop()
        futuro: asyncio.Future[tuple[bool, str]] = loop.create_future()
        solicitud = SolicitudPermiso(id=uuid.uuid4().hex[:8], accion=accion, creada_ts=time.time())
        with self._lock:
            if self._hay_pendiente_sin_lock():
                ocupada = True
            else:
                ocupada = False
                self._pendiente = solicitud
                self._futuro = futuro
        if ocupada:
            log.info("Acción %s denegada: %s", tipo, MOTIVO_OTRA_EN_CURSO)
            return DecisionPermiso("denegada", MOTIVO_OTRA_EN_CURSO, destructiva=True, aprobada=False)

        vista_previa = self._vista_previa(accion)
        if vista_previa is not None:
            # Con texto del usuario la descripción la fija la compuerta: nunca contiene el texto.
            descripcion = self._descripcion_v02(accion, len(vista_previa))
        elif accion.tipo == "lanzar_app" and args_accion.get("local") is True:
            descripcion = self._descripcion_app(str(args_accion.get("alias") or ""))
        elif accion.tipo == "desconectar_google":
            descripcion = DESCRIPCION_DESCONECTAR_GOOGLE   # v0.2.2: fija, nunca con datos de la cuenta
        else:
            descripcion = accion.descripcion or self._descripcion_por_defecto(accion)
        destructiva = bool(decision.destructiva)
        # Valor si la espera no termina normalmente (tarea cancelada, p. ej. en el apagado):
        # aun así se publica permiso.resuelto para que el HUD cierre el banner (§5).
        aprobada, motivo = False, MOTIVO_CANCELADA
        try:
            datos: dict[str, Any] = {
                "id": solicitud.id,
                "descripcion": descripcion,
                "destructiva": destructiva,
                "tipo": tipo,
            }
            if vista_previa is not None:
                datos["vista_previa"] = vista_previa
            self._publicar("permiso.solicitado", datos)
            pregunta = f"⚠ Confirmación requerida: {descripcion}. ¿Procedo? [s/N]"
            if vista_previa is not None:
                # Solo en la pantalla local (callback de la consola), nunca en el log.
                pregunta += f"\n   Texto: «{vista_previa}»"
            self._mostrar(pregunta)
            log.info("Confirmación solicitada (%s) para la acción %s", solicitud.id, tipo)
            try:
                aprobada, motivo = await asyncio.wait_for(futuro, timeout=self.timeout_s)
            except asyncio.TimeoutError:
                aprobada, motivo = False, f"sin respuesta en {_segundos(self.timeout_s)} s"
        finally:
            with self._lock:
                if self._pendiente is solicitud:
                    self._pendiente = None
                    self._futuro = None
            if not futuro.done():
                futuro.cancel()
            # En el finally: cada permiso.solicitado tiene su permiso.resuelto, también
            # cuando la cancelación de la tarea se propaga desde aquí.
            self._publicar("permiso.resuelto", {"id": solicitud.id, "aprobado": bool(aprobada),
                                                 "motivo": motivo})
            log.info("Confirmación %s resuelta: %s", solicitud.id, motivo)

        return DecisionPermiso("confirmar", motivo, destructiva=destructiva, aprobada=bool(aprobada))

    def _autorizar_tarjeta(self, accion: AccionPC) -> DecisionPermiso:
        """Evalúa una acción con tarjeta y, si se permite, consume la tarjeta (y reserva el correo)."""
        tipo = accion.tipo
        with self._lock:
            decision = self.evaluar(accion)
            if decision.veredicto == "permitida":
                ident = str(accion.args.get("tarjeta"))
                self._retirar_sin_lock(ident)
                if tipo == "enviar_correo":
                    self._correos.append((self._reloj(), ident))
        if decision.veredicto == "permitida":
            log.debug("Acción %s permitida con su tarjeta", tipo)
        else:
            log.info("Acción %s denegada: %s", tipo, decision.motivo)
        return decision

    @staticmethod
    def _descripcion_por_defecto(accion: AccionPC) -> str:
        """Descripción mínima si la intención no trajo una."""
        if accion.tipo == "cerrar_ventana":
            return f"cerrar la ventana {accion.args.get('direccion', '?')}"
        return accion.tipo.replace("_", " ")

    def _descripcion_app(self, clave: str) -> str:
        """Descripción de abrir una app del menú Inicio (nombre del índice, nunca del texto del usuario)."""
        nivel = self.nivel_app(clave)
        nombre = nivel[1] if nivel is not None else clave
        if nivel is not None and nivel[0] == "sensible":
            return f"Abrir «{nombre}» (herramienta del sistema: te lo preguntaré cada vez)"
        return f"Abrir «{nombre}» (app del menú Inicio; si dices que sí, no te lo vuelvo a preguntar)"

    @staticmethod
    def _vista_previa(accion: AccionPC) -> str | None:
        """Texto saneado de ``prerrellenar``/``escribir_texto`` (≤ 500) o ``None`` para el resto."""
        if accion.tipo not in ("prerrellenar", "escribir_texto"):
            return None
        return sanear_texto_accion(accion.args.get("texto"))[:MAX_TEXTO_ACCION]

    @staticmethod
    def _descripcion_v02(accion: AccionPC, n: int) -> str:
        """Descripción de una acción con texto: qué, dónde y cuántos caracteres (sin el texto)."""
        caracteres = "1 carácter" if n == 1 else f"{n} caracteres"
        if accion.tipo == "prerrellenar":
            return f"Abrir {nombre_destino(accion.args.get('alias'))} con un borrador de {caracteres} (no se enviará)"
        exe = _RE_EXE_SEGURO.sub("", str(accion.args.get("exe") or accion.args.get("clase") or "")).strip()[:40]
        return f"Escribir {caracteres} en {exe or 'la ventana anterior'} (sin enviar)"

    # ------------------------------------------------------------------
    # Estado de la confirmación pendiente
    # ------------------------------------------------------------------

    def _hay_pendiente_sin_lock(self) -> bool:
        futuro = self._futuro
        return self._pendiente is not None and futuro is not None and not futuro.done()

    def hay_pendiente(self) -> bool:
        """``True`` si hay una confirmación esperando la respuesta del usuario."""
        with self._lock:
            return self._hay_pendiente_sin_lock()

    def pendiente(self) -> SolicitudPermiso | None:
        """La solicitud pendiente (o ``None``)."""
        with self._lock:
            return self._pendiente if self._hay_pendiente_sin_lock() else None

    def resolver(self, respuesta: str) -> bool:
        """Interpreta ``respuesta`` como la contestación a la solicitud pendiente.

        "Sí" es exactamente ``s``, ``si``, ``sí``, ``y``, ``yes``, ``procede`` o ``dale`` (sin
        distinguir mayúsculas ni tildes); cualquier otra cosa es "no". Devuelve ``True`` si
        consumió una solicitud pendiente; si no había ninguna, ``False`` y no hace nada.
        """
        si = _es_si(respuesta)
        return self._terminar(si, MOTIVO_CONFIRMADA if si else MOTIVO_RECHAZADA)

    def resolver_por_id(self, id: str, aprobado: bool) -> bool:  # noqa: A002 - nombre del contrato
        """Resuelve la solicitud pendiente con un botón del chat de escritorio (§C2.11).

        Síncrono y thread-safe. Devuelve ``True`` solo si hay una pendiente, ``id`` coincide
        con el suyo (``hmac.compare_digest``) y la acción **no** es ``cerrar_ventana`` (cerrar
        ventanas solo se confirma en la terminal). ``aprobado`` debe ser un ``bool``. Un id
        viejo, mal formado o de otra solicitud no resuelve nada. Nunca toca locks de main.
        """
        if not isinstance(aprobado, bool) or not isinstance(id, str) or not ID_SOLICITUD_RE.fullmatch(id):
            return False
        motivo = MOTIVO_CONFIRMADA_BOTON if aprobado else MOTIVO_RECHAZADA_BOTON

        def coincide(solicitud: SolicitudPermiso) -> bool:
            if not hmac.compare_digest(id.encode("ascii"), str(solicitud.id).encode("ascii", "replace")):
                return False
            return getattr(solicitud.accion, "tipo", "") != "cerrar_ventana"

        return self._terminar(aprobado, motivo, condicion=coincide)

    def cancelar_pendiente(self) -> None:
        """Resuelve la solicitud pendiente como "no" (p. ej. durante el apagado)."""
        if self._terminar(False, MOTIVO_CANCELADA):
            log.info("Confirmación pendiente cancelada")

    def _terminar(self, aprobada: bool, motivo: str, *,
                  condicion: Callable[[SolicitudPermiso], bool] | None = None) -> bool:
        """Entrega el resultado al future pendiente (thread-safe). ``True`` si había uno.

        Con ``condicion``, solo se resuelve si la solicitud pendiente la cumple (comprobado
        dentro del lock: no hay carrera entre comprobar y resolver).
        """
        with self._lock:
            if not self._hay_pendiente_sin_lock():
                return False
            if condicion is not None:
                assert self._pendiente is not None
                if not condicion(self._pendiente):
                    return False
            futuro = self._futuro
            # La solicitud deja de estar pendiente YA: la siguiente línea no se consume aquí.
            self._pendiente = None
            self._futuro = None
        assert futuro is not None
        loop = futuro.get_loop()
        try:
            en_su_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            en_su_loop = False
        if en_su_loop:
            _entregar(futuro, (aprobada, motivo))
        else:
            try:
                loop.call_soon_threadsafe(_entregar, futuro, (aprobada, motivo))
            except RuntimeError as exc:  # el loop ya se cerró
                log.debug("No se pudo entregar la respuesta de confirmación: %s", exc)
                return False
        return True

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def _mostrar(self, texto: str) -> None:
        """Muestra la pregunta con el callback (o ``print``); un fallo no rompe la compuerta."""
        preguntar = self._preguntar
        try:
            if preguntar is not None:
                preguntar(texto)
            else:
                print(texto, flush=True)
        except Exception as exc:
            log.warning("No se pudo mostrar la pregunta de confirmación (%s)", type(exc).__name__)

    def _publicar(self, nombre: str, datos: dict[str, Any]) -> None:
        """Publica en el bus si existe; nunca lanza."""
        if self.bus is None:
            return
        try:
            self.bus.publicar(nombre, datos, origen="permisos")
        except Exception as exc:
            log.debug("No se pudo publicar %s: %s", nombre, exc)


def _entregar(futuro: asyncio.Future[tuple[bool, str]], valor: tuple[bool, str]) -> None:
    """``set_result`` si el future sigue vivo (pudo vencer el timeout entretanto)."""
    if not futuro.done():
        futuro.set_result(valor)
