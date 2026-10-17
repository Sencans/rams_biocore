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

Dependencias opcionales: ``psutil`` (para calcular los procesos propios: el propio PID y sus
ancestros). Degradación: sin psutil, solo ``os.getpid()``; sin bus no publica eventos; sin
callback ``preguntar`` usa ``print``.

La compuerta se limita a la mecánica: Amadeus confirma «qué, dónde, cuántos y qué se pierde»
sin sarcasmo, pero eso lo verbalizan main y Broca.
"""

from __future__ import annotations

import asyncio
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
    "SolicitudPermiso",
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


class CompuertaPermisos:
    """Compuerta única de permisos para el control del PC (§9.1 y §C2.11).

    ``evaluar`` es pura (tabla de §9.1 ampliada); ``autorizar`` añade la confirmación
    interactiva. La siguiente línea que escriba el usuario en la terminal local se pasa a
    :meth:`resolver`; los botones del chat de escritorio llegan a :meth:`resolver_por_id`.

    v0.2: ``pids_extra`` devuelve los PIDs de Amadeus Desktop (y descendientes) que tampoco
    son un objetivo válido de ``escribir_texto``; ``travesuras_activas`` es el estado efectivo
    de las travesuras que lleva main (§C2.14.4). Por defecto: ``frozenset()`` y ``False``.
    """

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 preguntar: Callable[[str], None] | None = None, timeout_s: float = 60.0,
                 pids_propios: frozenset[int] | None = None,
                 pids_extra: Callable[[], frozenset[int]] | None = None,
                 travesuras_activas: Callable[[], bool] | None = None) -> None:
        self.settings = settings
        self.bus = bus
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
        self._lock = threading.Lock()
        self._pendiente: SolicitudPermiso | None = None
        self._futuro: asyncio.Future[tuple[bool, str]] | None = None

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

        # 2. Tipo soportado.
        if tipo not in TIPOS_ACCION:
            return self._denegada("tipo de acción no soportado")

        # 3-4. Restricciones por origen (v0.2: por iniciativa, solo vistazo y travesura_cursor).
        if origen == "iniciativa" and tipo not in TIPOS_INICIATIVA:
            return self._denegada("por iniciativa propia solo puedo echar un vistazo por la cámara "
                                  "o hacer una travesura con el cursor")
        if origen == "nexus" and tipo != "enfocar_nexus":
            return self._denegada("desde Nexus IDE solo se permite traer su ventana al frente")

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
        """
        decision = self.evaluar(accion)
        tipo = getattr(accion, "tipo", "?")
        if decision.veredicto == "permitida":
            log.debug("Acción %s permitida: %s", tipo, decision.motivo)
            return decision
        if decision.veredicto != "confirmar":
            log.info("Acción %s denegada: %s", tipo, decision.motivo)
            return decision

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

    @staticmethod
    def _descripcion_por_defecto(accion: AccionPC) -> str:
        """Descripción mínima si la intención no trajo una."""
        if accion.tipo == "cerrar_ventana":
            return f"cerrar la ventana {accion.args.get('direccion', '?')}"
        return accion.tipo.replace("_", " ")

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
