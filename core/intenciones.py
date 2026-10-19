"""Enrutador local de intenciones y detector de eventos sociales de R.A.M.S. BIO-CORE (§10.12).

Qué hace (sin LLM, núcleo del principio "No-LLM centric", §8):
- ``enrutar`` decide con reglas y expresiones regulares qué herramienta o acción local
  corresponde a un texto del usuario (buscar en la web, recordar, programar, reporte del
  sistema, vistazo de cámara, acciones sobre el PC…). Las ``AccionPC`` nacen SOLO aquí, a
  partir del texto del usuario, siempre con ``origen="usuario"`` (§9.1).
- Aplica las **capacidades por canal**: desde Telegram o Nexus no se puede mirar, escuchar ni
  actuar sobre el PC; esas intenciones salen como ``bloqueada_canal`` (§9.3). El chat de
  escritorio (canal ``escritorio``, v0.2) es local: hereda todo lo de la terminal.
- v0.2 (``docs/COMPANION.md`` §C3 "Grupo orquestacion"): abrir apps y webs del catálogo
  ``DESTINOS_BASE`` (``abrir_destino``), buscar dentro de ellas (``buscar_en``), dejar un
  borrador prerrellenado (``prerrellenar``) o teclear en la ventana anterior
  (``escribir_texto``), publicar el último borrador de Amadeus (``publicar_borrador``),
  "búscame en 10 minutos" (``recordatorio_personal``), llamar o despedir a Amadeus del
  escritorio (``escritorio_venir``/``escritorio_vete``), cortar las travesuras
  (``basta_travesuras``) y pausar la cámara (``vision_pausa``). Las frases cortas ("ven",
  "basta"…) solo cuentan como **mensaje completo** (reglas ancladas). ``args["alias"]`` es
  siempre la **clave** del catálogo (los sinónimos de ``RAMS_DESTINOS`` se traducen aquí).
- ``parsear_cuando`` interpreta expresiones de tiempo en español ("a las 6 pm", "en 20
  minutos", "mañana a las 8", "el 2026-12-24") y ``detectar_evento_social`` reconoce apodos,
  cumplidos, saludos, despedidas, tristeza… (§4.4.4).
- v0.2.1 (órdenes naturales, apps locales y Telegram multimedia):
  * Las órdenes se entienden con vocativos y cortesías ("asistente, abre steam porfavor",
    "Kurisu ábreme YouTube pls"), en forma de petición ("¿puedes abrir YouTube?", "me abres
    Spotify", "quiero que abras Steam") y con "en el pc" al final. Las preguntas teóricas
    ("¿puedes abrir archivos?") no ejecutan nada: las explica Broca. El texto que el usuario
    DICTA (borradores, recordatorios) nunca se recorta por el final.
  * ``apps_locales`` (Windows): "abre <nombre de varias palabras>" da
    ``accion_pc`` con ``AccionPC("lanzar_app", {"nombre", "local": True, "explicito"[, "tentativa"]})``;
    main lo resuelve contra el menú Inicio (``ControlPC.resolver_app_local``) antes de la compuerta.
    La consulta de "busca …" cita al usuario: no pierde los vocativos del final ("busca videos de
    amadeus"), solo las cortesías.
  * Intenciones nuevas, en Telegram y en local: ``enviar_foto{fuente: camara|pantalla}``
    ("mándame una foto", "mándame una captura", "qué hay en mi pantalla" por Telegram, "cómo me
    veo" por Telegram) y ``pedir_audio`` ("mándame un audio", "háblame", "respóndeme por
    audio"); :func:`quiere_audio` dice si un mensaje con contenido pide la respuesta en audio.
  * ``telegram_acciones``: desde Telegram se admiten SOLO las acciones de bajo riesgo de
    ``ACCIONES_TELEGRAM`` (abrir destinos y apps, buscar en un destino y pausar/pasar la
    música); la ``AccionPC`` lleva ``args["canal"] = "telegram"`` y la compuerta remata.
- v0.2.2 (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §4): intenciones ``google`` (conectar,
  desconectar, estado), ``correo`` (preparar un borrador: destinatarios, asunto, cuerpo o tema y
  adjuntos), ``drive`` (buscar, info, descargar, leer, crear carpetas, subir, papelera y compartir)
  y ``taller`` (escribir un script, listar, ejecutar u olvidar habilidades). Ninguna crea una
  ``AccionPC``: las acciones con tarjeta las construye ``main`` al pulsar el botón (§0.2). Por
  Telegram solo valen las operaciones de ``OPS_POR_CANAL`` (el taller nunca). Además,
  :func:`pide_tarea_pc` (¿el mensaje pide una tarea sobre el PC?) y :func:`acepta_oferta` (¿acepta
  la oferta del taller?), puras y sin E/S.

Es lógica pura: sin E/S, sin red y sin estado global mutable. Solo depende de la biblioteca
estándar y de ``core.contracts``. No se degrada porque no tiene dependencias opcionales.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any, Final

from core.contracts import CANALES_LOCALES, AccionPC

try:  # v0.2 (grupo base): catálogo cerrado de destinos
    from core.contracts import DESTINOS_BASE
except ImportError:  # contratos v0.1: sin catálogo (las reglas de destinos no casan nunca)
    DESTINOS_BASE = {}  # type: ignore[assignment]

try:  # v0.2.2 (grupo base): catálogos de Drive y forma de una dirección de correo
    from core.contracts import CORREO_RE, DESTINOS_DESCARGA, FORMATOS_DRIVE, OPERACIONES_DRIVE, ROLES_DRIVE
except ImportError:  # contratos v0.2.1: los mismos valores de la spec (§3.1)
    OPERACIONES_DRIVE = ("buscar", "info", "descargar", "leer", "crear_carpeta", "subir", "papelera",  # type: ignore[misc]
                         "compartir")
    DESTINOS_DESCARGA = ("descargas", "documentos", "escritorio")  # type: ignore[misc]
    FORMATOS_DRIVE = ("pdf", "texto", "original")  # type: ignore[misc]
    ROLES_DRIVE = ("reader", "commenter", "writer")  # type: ignore[misc]
    CORREO_RE = re.compile(  # type: ignore[misc]
        r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}$")

__all__ = [
    "Intencion",
    "TIPOS_INTENCION",
    "OPS_INTENCION",
    "COMANDOS",
    "INTENCIONES_POR_CANAL",
    "OPS_POR_CANAL",
    "COMANDOS_POR_CANAL",
    "MENSAJE_SOLO_LOCAL",
    "ACCIONES_TELEGRAM",
    "FUENTES_FOTO",
    "normalizar",
    "quiere_audio",
    "parsear_comando",
    "parsear_cuando",
    "enrutar",
    "permitida_en_canal",
    "detectar_evento_social",
    "completar_programar",
    "quiere_cancelar",
    "pide_tarea_pc",
    "acepta_oferta",
]

log = logging.getLogger("rams.intenciones")

# ---------------------------------------------------------------------------
# Tipos y constantes (§10.12)
# ---------------------------------------------------------------------------

TIPOS_INTENCION: Final = (
    "ninguna", "comando", "buscar_web", "reporte_sistema", "recordar", "programar",
    "programar_incompleto", "mirar_pantalla", "vistazo", "accion_pc", "memoria_consulta",
    "bloqueada_canal",
    # v0.2
    "publicar_borrador", "recordatorio_personal", "escritorio_venir", "escritorio_vete",
    "basta_travesuras", "vision_pausa",
    # v0.2.1: fotos de la cámara o de la pantalla (a Telegram) y respuestas en audio
    "enviar_foto", "pedir_audio",
    # v0.2.2: cuenta de Google, borradores de correo, Drive y taller de scripts
    "google", "correo", "drive", "taller",
)

# v0.2.2 (§4.1): operaciones de cada tipo nuevo (``args["op"]``).
OPS_INTENCION: Final[dict[str, tuple[str, ...]]] = {
    "google": ("conectar", "desconectar", "estado"),
    "correo": ("preparar",),
    "drive": tuple(OPERACIONES_DRIVE),
    "taller": ("crear", "habilidades", "ejecutar_habilidad", "olvidar_habilidad"),
}

# Fuentes de ``enviar_foto`` (contrato con lobes/capturas.py).
FUENTES_FOTO: Final = ("camara", "pantalla")

# Mensaje de un recordatorio personal sin texto ("búscame en 10 minutos").
MENSAJE_BUSCAR_DEFECTO: Final = "Me pediste que te buscara"


@dataclass(frozen=True)
class Intencion:
    """Intención decidida por el enrutador local (nunca por el LLM)."""

    tipo: str        # ∈ TIPOS_INTENCION
    args: Mapping[str, Any] = field(default_factory=dict, hash=False)
    confianza: float = 1.0
    explicacion_tecnica: bool = False   # sugiere modo_salida "terminal_largo"

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", dict(self.args) if isinstance(self.args, Mapping) else {})
        try:
            confianza = float(self.confianza)
        except (TypeError, ValueError):
            confianza = 1.0
        object.__setattr__(self, "confianza", min(1.0, max(0.0, confianza)))
        object.__setattr__(self, "explicacion_tecnica", bool(self.explicacion_tecnica))


COMANDOS: Final = ("/ayuda", "/salir", "/estado", "/divergencia", "/memoria", "/recordar", "/olvidar",
                   "/agenda", "/camara", "/silencio", "/colgar", "/buscar", "/reporte", "/bitacora",
                   "/devolver_llamada", "/hud", "/logro",
                   # v0.2: solo canales locales (terminal y chat de escritorio)
                   "/vision", "/pantalla", "/travesuras",
                   # v0.2.2: cuenta de Google (por Telegram, solo el estado), taller y habilidades
                   "/google", "/script", "/habilidades")

# Capacidades por canal de entrada (normativo). Los canales locales (terminal y escritorio)
# pueden todo. Telegram no ganó capacidades en la v0.2 (§C2.12): tampoco el recordatorio
# personal ("llámame en 10 minutos"), porque al vencer mueve el avatar en el escritorio
# (aparecer + buscar_usuario). Por Telegram sigue valiendo "recuérdame en …".
# v0.2.1: Telegram puede pedir fotos y audios (main exige ``RAMS_TELEGRAM_FOTOS`` para las fotos)
# y, con ``telegram_acciones``, las acciones de bajo riesgo de ``ACCIONES_TELEGRAM``.
# v0.2.2: Telegram gana google (solo el estado), correo (solo preparar: se aprueba en el PC) y drive
# (solo buscar e info); el taller nunca. Los interruptores (RAMS_GOOGLE, RAMS_TELEGRAM_CORREO,
# RAMS_TELEGRAM_DRIVE, RAMS_TALLER) no los mira el enrutador: los mira main.
INTENCIONES_POR_CANAL: Final[dict[str, frozenset[str]]] = {
    "telegram": frozenset({"ninguna", "comando", "buscar_web", "recordar", "programar", "programar_incompleto",
                           "memoria_consulta", "reporte_sistema", "enviar_foto", "pedir_audio",
                           "google", "correo", "drive"}),
    "nexus": frozenset({"ninguna"}),
}
# v0.2.2 (§4.3): operaciones permitidas por canal; una op no listada → bloqueada_canal.
OPS_POR_CANAL: Final[dict[str, dict[str, frozenset[str]]]] = {
    "telegram": {"google": frozenset({"estado"}), "correo": frozenset({"preparar"}),
                 "drive": frozenset({"buscar", "info"})},
}
# Acciones sobre el PC que se pueden pedir desde Telegram con ``telegram_acciones`` (bajo riesgo:
# abrir, buscar y la música). Teclear, prerrellenar, cerrar ventanas, travesuras, volumen,
# espacios de trabajo o la cámara local siguen siendo solo locales.
ACCIONES_TELEGRAM: Final = frozenset({"abrir_destino", "buscar_en", "lanzar_app", "media"})
COMANDOS_POR_CANAL: Final[dict[str, frozenset[str]]] = {
    "telegram": frozenset({"/ayuda", "/estado", "/divergencia", "/memoria", "/recordar", "/buscar", "/agenda",
                           "/reporte", "/bitacora", "/colgar", "/devolver_llamada",
                           # v0.2.2: main solo admite el estado por Telegram
                           "/google"}),
    "nexus": frozenset(),
}
MENSAJE_SOLO_LOCAL: Final = "Eso solo se puede pedir desde el computador."

# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

_RE_ESPACIOS: Final = re.compile(r"\s+")
_QUITAR_APERTURA: Final = str.maketrans("", "", "¿¡")
_RE_PUNTUACION_FINAL: Final = re.compile(r"[\s.,;:!?…]+$")
_RE_TIENE_LETRA_O_DIGITO: Final = re.compile(r"\w")


def _sin_tildes(texto: str) -> str:
    """Quita los diacríticos (NFD sin la categoría Mn). ``ñ`` → ``n``, ``ü`` → ``u``."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return unicodedata.normalize("NFC", "".join(c for c in descompuesto if unicodedata.category(c) != "Mn"))


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, sin ``¿``/``¡``, espacios colapsados y ``strip``.

    Conserva el resto de la puntuación (``?`` hace falta para "estoy aquí?").
    """
    if not isinstance(texto, str):
        texto = "" if texto is None else str(texto)
    resultado = _sin_tildes(texto.lower()).translate(_QUITAR_APERTURA)
    return _RE_ESPACIOS.sub(" ", resultado).strip()


def _palabras(texto: str) -> tuple[list[str], list[str]]:
    """Palabras originales y normalizadas **alineadas por índice**.

    Se normaliza palabra a palabra y se descartan a la vez (en ambas listas) las que quedan
    vacías (p. ej. un ``¿`` suelto). Así un corte por índice de palabra sobre el texto
    normalizado se traduce exactamente al texto original (algoritmo de cortes, §10.12).
    """
    originales: list[str] = []
    normalizadas: list[str] = []
    for palabra in str(texto or "").split():
        norm = normalizar(palabra)
        if norm:
            originales.append(palabra)
            normalizadas.append(norm)
    return originales, normalizadas


def _original_desde(originales: list[str], normalizadas: list[str], inicio: int) -> str:
    """Texto original a partir del carácter ``inicio`` del texto normalizado (unido por espacios).

    Si ``inicio`` cae a mitad de una palabra (p. ej. ``"busca:precio"``), devuelve el tramo
    normalizado: nunca se pierde contenido.
    """
    texto_norm = " ".join(normalizadas)
    while 0 <= inicio < len(texto_norm) and texto_norm[inicio] == " ":
        inicio += 1
    if inicio <= 0:
        indice = 0
    elif inicio <= len(texto_norm) and texto_norm[inicio - 1] == " ":
        indice = texto_norm[:inicio].count(" ")
    else:
        return texto_norm[inicio:].strip()
    return " ".join(originales[indice:]).strip()


def _limpiar_fragmento(texto: str) -> str:
    """Quita puntuación sobrante en los extremos de un fragmento extraído del texto original."""
    limpio = re.sub(r"^[\s,;:.\-–—]+", "", texto)
    limpio = re.sub(r"[\s,;:\-–—]+$", "", limpio)
    return limpio.strip()


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def parsear_comando(texto: str) -> tuple[str, str] | None:
    """``("/buscar", "resto")`` si el texto empieza por un comando conocido; si no, ``None``.

    El comando se compara sin mayúsculas ni tildes (``/Bitácora`` → ``/bitacora``) y admite
    el sufijo ``@nombre_bot`` que añaden algunos clientes de Telegram. ``resto`` conserva el
    texto original.
    """
    limpio = str(texto or "").strip()
    if not limpio.startswith("/"):
        return None
    partes = limpio.split(maxsplit=1)
    cabeza = normalizar(partes[0]).split("@", 1)[0]
    if cabeza not in COMANDOS:
        return None
    resto = partes[1].strip() if len(partes) > 1 else ""
    return cabeza, resto


# ---------------------------------------------------------------------------
# Expresiones de tiempo
# ---------------------------------------------------------------------------

# "a las 6", "a la 1", "a las 6:30", "a las 6 pm", "a las 6 p. m.", "a las 8 de la noche".
_HORA: Final = (r"a las? (?P<h>\d{1,2})(?:[:.](?P<mi>\d{2}))?"
                r"(?: ?(?P<ampm>am|pm|a\.? ?m\.?|p\.? ?m\.?|de la (?:manana|tarde|noche|madrugada)))?")
_RE_SOLO_HORA: Final = re.compile(_HORA)
_RE_RELATIVO: Final = re.compile(r"en (?P<n>\d{1,5}|un|una|media) (?P<u>minutos?|min|horas?|h)")
_RE_DIA_RELATIVO: Final = re.compile(rf"(?P<dia>pasado manana|manana)(?: {_HORA})?")
_RE_FECHA_ISO: Final = re.compile(rf"el (?P<y>\d{{4}})-(?P<mo>\d{{1,2}})-(?P<d>\d{{1,2}})(?: {_HORA})?")
_RE_FECHA_DM: Final = re.compile(rf"el (?P<d>\d{{1,2}})/(?P<mo>\d{{1,2}})(?:/(?P<y>\d{{2}}|\d{{4}}))?(?: {_HORA})?")

_HORA_POR_DEFECTO: Final = (9, 0)   # "mañana" sin hora ⇒ 09:00 (también para fechas sin hora)


def _hora_de(coincidencia: re.Match[str]) -> tuple[int, int] | None:
    """(hora, minuto) en 24 h a partir de los grupos ``h``, ``mi`` y ``ampm``; inválida ⇒ ``None``."""
    if coincidencia.group("h") is None:
        return None
    hora = int(coincidencia.group("h"))
    minuto = int(coincidencia.group("mi") or 0)
    if minuto > 59:
        return None
    sufijo = coincidencia.group("ampm")
    if sufijo:
        clave = sufijo.replace(".", "").replace(" ", "")
        if not 1 <= hora <= 12:
            return None
        if clave == "am" or clave.endswith("manana") or clave.endswith("madrugada"):
            if hora == 12:
                hora = 0
        elif clave.endswith("noche") and hora == 12:
            hora = 0                      # "las 12 de la noche" = medianoche
        elif hora != 12:                  # pm, "de la tarde", "de la noche"
            hora += 12
    elif hora > 23:
        return None
    return hora, minuto


def _combinar(dia: date, hora: int, minuto: int, ahora: datetime) -> datetime:
    """``datetime`` en la zona de ``ahora`` (aware si ``ahora`` lo es)."""
    return datetime.combine(dia, dtime(hora, minuto), tzinfo=ahora.tzinfo)


def parsear_cuando(texto: str, ahora: datetime) -> datetime | None:
    """Interpreta una expresión de tiempo que debe ocupar el texto **entero** (``fullmatch``).

    Formas admitidas (tras normalizar y quitar la puntuación final):
    ``a las HH(:MM)? (am|pm|a. m.|p. m.|de la tarde…)?``, ``en (N|un|una|media)
    (minutos?|min|horas?|h)``, ``mañana( a las …)?`` (09:00 si no se da hora), ``pasado
    mañana( a las …)?``, ``el AAAA-MM-DD( a las …)?`` y ``el DD/MM(/AAAA)?( a las …)?``.

    Si la hora ya pasó hoy y no se indicó día, se programa para mañana. Una fecha ``DD/MM``
    sin año cuyo **día** ya pasó se lleva al año siguiente; si el día es hoy y la hora (o las
    09:00 por defecto) ya pasó, devuelve ``None`` igual que una fecha explícita pasada: así
    acaba en ``programar_incompleto`` y se pregunta la hora en lugar de programar para dentro
    de un año. Devuelve un ``datetime`` en la zona de ``ahora`` o ``None``.
    """
    return _analizar_cuando(texto, ahora)[1]


def _analizar_cuando(texto: str, ahora: datetime) -> tuple[bool, datetime | None]:
    """``(reconocida, vence)``: ``reconocida`` indica que el texto entero tiene la forma de una
    expresión de tiempo aunque no sea válida o ya haya pasado (``vence`` es entonces ``None``).

    El algoritmo de cortes lo usa para no buscar un "cuándo" más corto dentro de una fecha
    reconocida pero vencida ("pagar el 16/10 a las 9" no es "mañana a las 9").
    """
    if not isinstance(ahora, datetime):
        return False, None
    limpio = _RE_PUNTUACION_FINAL.sub("", normalizar(texto))
    if not limpio:
        return False, None

    relativo = _RE_RELATIVO.fullmatch(limpio)
    if relativo:
        bruto = relativo.group("n")
        cantidad = {"un": 1.0, "una": 1.0, "media": 0.5}.get(bruto)
        if cantidad is None:
            cantidad = float(int(bruto))
        if cantidad <= 0:
            return True, None
        minutos = cantidad * (60.0 if relativo.group("u").startswith("h") else 1.0)
        return True, ahora + timedelta(minutes=minutos)

    solo_hora = _RE_SOLO_HORA.fullmatch(limpio)
    if solo_hora:
        hm = _hora_de(solo_hora)
        if hm is None:
            return True, None
        candidato = _combinar(ahora.date(), hm[0], hm[1], ahora)
        if candidato <= ahora:
            candidato = _combinar(ahora.date() + timedelta(days=1), hm[0], hm[1], ahora)
        return True, candidato

    dia_relativo = _RE_DIA_RELATIVO.fullmatch(limpio)
    if dia_relativo:
        hm = _hora_de(dia_relativo) if dia_relativo.group("h") else _HORA_POR_DEFECTO
        if hm is None:
            return True, None
        dias = 2 if dia_relativo.group("dia") == "pasado manana" else 1
        return True, _combinar(ahora.date() + timedelta(days=dias), hm[0], hm[1], ahora)

    iso = _RE_FECHA_ISO.fullmatch(limpio)
    if iso:
        hm = _hora_de(iso) if iso.group("h") else _HORA_POR_DEFECTO
        if hm is None:
            return True, None
        try:
            dia = date(int(iso.group("y")), int(iso.group("mo")), int(iso.group("d")))
        except ValueError:
            return True, None
        candidato = _combinar(dia, hm[0], hm[1], ahora)
        return True, (candidato if candidato > ahora else None)

    dm = _RE_FECHA_DM.fullmatch(limpio)
    if dm:
        hm = _hora_de(dm) if dm.group("h") else _HORA_POR_DEFECTO
        if hm is None:
            return True, None
        anio_txt = dm.group("y")
        anio = ahora.year if anio_txt is None else int(anio_txt) + (2000 if len(anio_txt) == 2 else 0)
        try:
            dia = date(anio, int(dm.group("mo")), int(dm.group("d")))
        except ValueError:
            return True, None
        candidato = _combinar(dia, hm[0], hm[1], ahora)
        if candidato <= ahora:
            # Solo un día ya pasado (sin año) se lleva al año siguiente; hoy con la hora vencida
            # no es "el año que viene" sino una hora que ya pasó (como en la forma ISO).
            if anio_txt is not None or dia >= ahora.date():
                return True, None
            try:
                candidato = _combinar(dia.replace(year=anio + 1), hm[0], hm[1], ahora)
            except ValueError:   # 29/02 en un año no bisiesto
                return True, None
        return True, candidato
    return False, None


# ---------------------------------------------------------------------------
# Algoritmo de cortes de "programar" (normativo, §10.12)
# ---------------------------------------------------------------------------

_RE_CONECTOR_RECORDATORIO: Final = re.compile(r"(a las?|en|manana|pasado manana|el)\b")
_RE_CONECTOR_CAPSULA: Final = re.compile(r"(para|el)\b")
_RE_QUE_INICIAL: Final = re.compile(r"que[,:]?")   # "recuérdame QUE mañana a las 8 llame al banco"


def _limpiar_mensaje(palabras: list[str]) -> str:
    """Mensaje de un recordatorio a partir de palabras originales (sin "que" inicial ni comas sueltas)."""
    mensaje = _limpiar_fragmento(" ".join(palabras))
    if normalizar(mensaje).startswith("que "):
        mensaje = mensaje.split(maxsplit=1)[1]
    return _limpiar_fragmento(mensaje)


def _cuando_de(palabras: list[str], ahora: datetime, capsula: bool) -> tuple[bool, datetime | None]:
    """``_analizar_cuando`` de unas palabras normalizadas (en cápsulas descarta el conector ``para`` inicial)."""
    if capsula and palabras and palabras[0] == "para":
        palabras = palabras[1:]
    return _analizar_cuando(" ".join(palabras), ahora) if palabras else (False, None)


def _cortar(originales: list[str], normalizadas: list[str], ahora: datetime,
            capsula: bool) -> tuple[str, datetime] | None:
    """Separa ``resto`` en (mensaje, vencimiento) con cortes por índice de palabra.

    0. Si ``resto`` **entero** es una expresión de tiempo ("recuérdame a las 6 pm"), devuelve
       ``("", vence)``: hay hora pero falta el mensaje. Nunca se parte la propia hora para
       inventar un mensaje ("pm", "pasado", "el 24/12").
    1. **Sufijo:** de izquierda a derecha, en cada posición (salvo la 0) donde empieza un
       conector, prueba ``parsear_cuando`` con el resto; gana el primer corte que se pueda
       parsear entero (el conector ``para`` de las cápsulas se descarta del "cuándo"). Se
       aplica la coincidencia más larga: el corte se extiende a la izquierda mientras el
       "cuándo" siga parseando ("pasado | mañana a las 7" ⇒ "pasado mañana a las 7").
    2. **Prefijo:** prefijos de más largo a más corto (al menos una palabra de mensaje detrás).
       El primero que parsea es el más largo, así que la palabra siguiente nunca forma parte
       de la hora ("a las 6 | pm" no se acepta porque "a las 6 pm" también parsea).

    Si el corte ganador deja el mensaje vacío (solo puntuación o "que"), devuelve
    ``("", vence)`` en lugar de probar cortes más cortos que partirían la hora. Si el "cuándo"
    ganador tiene la forma de una expresión de tiempo pero no es válido o ya pasó ("el 16/10
    a las 9" dicho el 16/10 a las 10:00), devuelve ``None`` (falta una hora válida) en lugar de
    quedarse con un trozo de él ("a las 9" ⇒ mañana).
    """
    total = len(normalizadas)
    reconocida, entero = _cuando_de(normalizadas, ahora, capsula)
    if reconocida:
        return ("", entero) if entero is not None else None
    conector = _RE_CONECTOR_CAPSULA if capsula else _RE_CONECTOR_RECORDATORIO
    for pos in range(1, total):
        cola = normalizadas[pos:]
        if not conector.match(" ".join(cola)):
            continue
        reconocida, vence = _cuando_de(cola, ahora, capsula)
        if not reconocida:
            continue
        inicio = pos
        while vence is not None and inicio > 1:
            mas_larga, vence_mas_largo = _cuando_de(normalizadas[inicio - 1:], ahora, capsula)
            if not mas_larga:
                break
            inicio, vence = inicio - 1, vence_mas_largo
        if vence is None:
            return None
        return _limpiar_mensaje(originales[:inicio]), vence
    for fin in range(total - 1, 0, -1):
        reconocida, vence = _cuando_de(normalizadas[:fin], ahora, capsula)
        if reconocida:
            return (_limpiar_mensaje(originales[fin:]), vence) if vence is not None else None
    return None


def _sin_relleno_inicial(originales: list[str], normalizadas: list[str]) -> tuple[list[str], list[str]]:
    """Descarta las palabras iniciales sin letras ni dígitos (en ambas listas a la vez)."""
    while normalizadas and not _RE_TIENE_LETRA_O_DIGITO.search(normalizadas[0]):
        originales, normalizadas = originales[1:], normalizadas[1:]
    return originales, normalizadas


def _programar(originales: list[str], normalizadas: list[str], ahora: datetime, *,
               capsula: bool, tecnica: bool) -> Intencion:
    """``programar`` con (mensaje, vence) o ``programar_incompleto`` (nunca ``ninguna``).

    ``programar_incompleto`` lleva ``{"mensaje", "tipo"}`` cuando falta la hora (§10.12) y,
    además, ``"vence"`` y ``"falta": "mensaje"`` cuando hay hora pero no mensaje
    ("recuérdame en 20 minutos"): main pregunta entonces qué recordar, no para cuándo.
    Un "que" inicial ("recuérdame que mañana a las 8 llame al banco") no forma parte ni de la
    hora ni del mensaje y se descarta antes de cortar.
    """
    originales, normalizadas = _sin_relleno_inicial(originales, normalizadas)
    if len(normalizadas) > 1 and _RE_QUE_INICIAL.fullmatch(normalizadas[0]):
        originales, normalizadas = _sin_relleno_inicial(originales[1:], normalizadas[1:])
    tipo = "capsula" if capsula else "recordatorio"
    corte = _cortar(originales, normalizadas, ahora, capsula) if normalizadas else None
    if corte is not None:
        mensaje, vence = corte
        if mensaje:
            return Intencion("programar", {"mensaje": mensaje, "vence": vence, "tipo": tipo},
                             explicacion_tecnica=tecnica)
        return Intencion("programar_incompleto", {"mensaje": "", "tipo": tipo, "vence": vence, "falta": "mensaje"},
                         explicacion_tecnica=tecnica)
    return Intencion("programar_incompleto", {"mensaje": _limpiar_fragmento(" ".join(originales)), "tipo": tipo},
                     explicacion_tecnica=tecnica)


# ---------------------------------------------------------------------------
# Recordatorio a medias: la respuesta del turno siguiente (CONTRATO de cierre, §10.12)
# ---------------------------------------------------------------------------
# Tras un ``programar_incompleto`` Amadeus pregunta lo que falta (la hora o el mensaje). main
# guarda el pendiente y, en el turno siguiente del mismo canal, llama a ``completar_programar``
# con la línea del usuario. Sigue siendo una decisión local y sin LLM.

# Palabras que pueden rodear una hora sin cambiarla ("para mañana a las 8", "a las 6 pm, por favor").
_RELLENO_INICIAL: Final = frozenset({
    "pues", "ok", "okay", "vale", "bueno", "si", "listo", "entonces", "mejor", "eh", "ah", "mmm", "hmm",
    "este", "para", "que", "sea", "hazlo", "ponlo", "ponmelo", "programalo", "recuerdamelo", "avisame",
    "avisamelo", "dale", "claro",
})
_RELLENO_FINAL: Final = (("por", "favor"), ("porfa",), ("gracias",), ("please",), ("si",), ("vale",), ("ok",),
                         ("entonces",), ("mejor",))
_RE_PUNTUACION_PALABRA: Final = re.compile(r"[.,;:!?…]+$")

# Cancelación explícita ("no, olvídalo", "déjalo así", "mejor no"): todas las palabras deben salir de
# este vocabulario y al menos una debe ser "fuerte".
_CANCELAR_FUERTES: Final = frozenset({
    "no", "nada", "olvidalo", "olvidala", "olvida", "cancela", "cancelalo", "cancelala", "cancelar",
    "dejalo", "dejala", "deja", "nah", "ninguno", "ninguna",
})
_CANCELAR_VOCABULARIO: Final = _CANCELAR_FUERTES | frozenset({
    "eso", "ya", "mejor", "gracias", "hace", "falta", "igual", "da", "importa", "asi", "lo", "por", "favor",
    "porfa", "bueno", "entonces", "mas", "tarde", "despues", "luego", "el", "recordatorio", "la", "capsula",
})


def _palabras_limpias(normalizadas: list[str]) -> list[str]:
    """Palabras normalizadas sin la puntuación pegada al final (``"no,"`` → ``"no"``)."""
    limpias = (_RE_PUNTUACION_PALABRA.sub("", p) for p in normalizadas)
    return [p for p in limpias if p and _RE_TIENE_LETRA_O_DIGITO.search(p)]


def quiere_cancelar(texto: str) -> bool:
    """``True`` si la línea solo pide cancelar ("no", "olvídalo", "mejor no", "déjalo así")."""
    palabras = _palabras_limpias(normalizar(texto).split())
    if not palabras or len(palabras) > 6:
        return False
    return (all(p in _CANCELAR_VOCABULARIO for p in palabras)
            and any(p in _CANCELAR_FUERTES for p in palabras))


def _cuando_respuesta(normalizadas: list[str], ahora: datetime) -> tuple[bool, datetime | None]:
    """``_analizar_cuando`` de una respuesta suelta, tolerando relleno al principio y al final."""
    palabras = _palabras_limpias(normalizadas)
    cambiado = True
    while cambiado and palabras:
        cambiado = False
        for cola in _RELLENO_FINAL:
            if len(palabras) > len(cola) and tuple(palabras[-len(cola):]) == cola:
                palabras = palabras[:-len(cola)]
                cambiado = True
    while len(palabras) > 1 and palabras[0] in _RELLENO_INICIAL:
        palabras = palabras[1:]
    # "para las 8" / "las 6 pm" ⇒ "a las 8" / "a las 6 pm".
    if len(palabras) > 1 and palabras[0] in ("las", "la") and palabras[1][:1].isdigit():
        palabras = ["a", *palabras]
    return _analizar_cuando(" ".join(palabras), ahora) if palabras else (False, None)


def completar_programar(texto: str, pendiente: Mapping[str, Any], *, ahora: datetime,
                        intencion: Intencion | None = None) -> Intencion | None:
    """Completa un recordatorio a medias con la respuesta del usuario (sin LLM).

    ``pendiente`` es lo que main guardó tras un ``programar_incompleto``: ``{"falta": "hora" |
    "mensaje", "tipo": "recordatorio" | "capsula", "mensaje": str, "vence": datetime | None}``.
    ``intencion`` es lo que ``enrutar`` dijo de la misma línea (``None`` equivale a ``ninguna``).

    - Falta la hora: la línea debe ser una expresión de tiempo (se toleran rellenos como "para",
      "por favor" o "que sea"); si no, se aplica el algoritmo de cortes y se usa solo el "cuándo"
      (el mensaje sigue siendo el original). Si la hora tiene forma válida pero ya pasó, devuelve
      ``programar_incompleto`` para volver a preguntar.
    - Falta el mensaje: la línea entera es el mensaje (sin un "que" inicial).
    - Si la línea es a su vez un ``programar_incompleto`` que aporta justo lo que faltaba
      ("recuérdame a las 6" cuando faltaba la hora), se combinan.

    Devuelve ``programar`` / ``programar_incompleto``, o ``None`` si la línea no completa nada
    (main cancela entonces el pendiente y lo dice). Nunca lanza.
    """
    try:
        return _completar_programar(texto, pendiente, ahora, intencion)
    except Exception as exc:  # defensa: nunca tumba un turno
        log.warning("Error inesperado al completar un recordatorio (%s)", type(exc).__name__)
        return None


def _completar_programar(texto: str, pendiente: Mapping[str, Any], ahora: datetime,
                         intencion: Intencion | None) -> Intencion | None:
    if not isinstance(ahora, datetime) or not isinstance(pendiente, Mapping):
        return None
    falta = pendiente.get("falta")
    tipo = "capsula" if pendiente.get("tipo") == "capsula" else "recordatorio"
    mensaje_pendiente = str(pendiente.get("mensaje") or "").strip()
    vence_pendiente = pendiente.get("vence")
    tecnica = bool(intencion.explicacion_tecnica) if intencion is not None else False

    def programar(mensaje: str, vence: datetime) -> Intencion:
        return Intencion("programar", {"mensaje": mensaje, "vence": vence, "tipo": tipo},
                         explicacion_tecnica=tecnica)

    if intencion is not None and intencion.tipo == "programar_incompleto":
        args = intencion.args
        nuevo_vence = args.get("vence")
        nuevo_mensaje = str(args.get("mensaje") or "").strip()
        if falta == "hora" and mensaje_pendiente and isinstance(nuevo_vence, datetime):
            return programar(mensaje_pendiente, nuevo_vence)
        if falta == "mensaje" and isinstance(vence_pendiente, datetime) and nuevo_mensaje \
                and args.get("falta") != "mensaje":
            return programar(nuevo_mensaje, vence_pendiente)
        return intencion
    if intencion is not None and intencion.tipo != "ninguna":
        return None
    if quiere_cancelar(texto):
        return None
    originales, normalizadas = _palabras(str(texto or "").strip())
    originales, normalizadas = _sin_relleno_inicial(originales, normalizadas)
    if not normalizadas:
        return None

    if falta == "mensaje":
        if not isinstance(vence_pendiente, datetime):
            return None
        mensaje = _limpiar_mensaje(originales).rstrip(".!?…").strip()
        return programar(mensaje, vence_pendiente) if mensaje else None

    if falta != "hora" or not mensaje_pendiente:
        return None
    reconocida, vence = _cuando_respuesta(normalizadas, ahora)
    if reconocida:
        if vence is None:   # forma de hora válida pero vencida o imposible: se vuelve a preguntar
            return Intencion("programar_incompleto", {"mensaje": mensaje_pendiente, "tipo": tipo},
                             explicacion_tecnica=tecnica)
        return programar(mensaje_pendiente, vence)
    corte = _cortar(originales, normalizadas, ahora, tipo == "capsula")
    if corte is not None:
        return programar(mensaje_pendiente, corte[1])
    return None


# ---------------------------------------------------------------------------
# Tabla de reglas de enrutar (§10.12), en orden
# ---------------------------------------------------------------------------

_RE_PROGRAMAR: Final = re.compile(r"^(recuerdame|avisame)[,:]?\s+(?P<resto>.+)$")
_RE_CAPSULA: Final = re.compile(r"^(capsula del tiempo|guarda para el futuro)[:,]?\s+(?P<resto>.+)$")
_RE_RECORDAR: Final = re.compile(r"^(recuerda que|anota que|no olvides que|guarda que)\s+(?P<x>.+)")
_RE_BUSCAR: Final = re.compile(
    r"^(busca|buscame|buscar|investiga|averigua|googlea|consulta en (internet|la red))\b[:,]?\s*(?P<q>.+)")
_RE_BUSCAR_DEBIL: Final = re.compile(
    r"\b(ultimas noticias|que paso con|precio (actual )?de|cuando (sale|salio)|version (estable )?de)\b")
_RE_REPORTE: Final = re.compile(
    r"\b(reporte|estado del (sistema|pc|computador)|como esta (el|mi) (pc|computador|sistema)|temperatura"
    r"|uso de (cpu|ram|memoria))\b")
_RE_PANTALLA: Final = re.compile(r"\b(mira|analiza|revisa|que hay en|que ves en)\s+(mi\s+)?pantalla\b")
_RE_VISTAZO: Final = re.compile(r"\b(me ves|mirame|puedes verme)\b|estoy aqui\?")
_RE_LANZAR: Final = re.compile(r"^(abre|lanza|abrir|ejecuta)\s+(el |la )?(?P<app>[a-z0-9_]+)[.!?]*$")
_RE_WORKSPACE: Final = re.compile(
    r"\b(ve|cambia|pasa|llevame)\s+al?\s+(espacio|workspace|escritorio)\s+(?P<n>\d{1,2})\b")
_RE_NEXUS: Final = re.compile(r"\b(enfoca|trae|muestrame)\s+(nexus|el ide)\b")
_RE_CERRAR: Final = re.compile(r"^cierra\s+(esta |la )?ventana\b")
_RE_MEDIA: Final = re.compile(
    r"^(?P<orden>pausa|pon pausa|reanuda|siguiente cancion|cancion anterior)( (la )?(musica|cancion))?[.!?]*$")
_RE_VOLUMEN: Final = re.compile(r"\bvolumen (al? )?(?P<n>\d{1,3})\s*%?")
_RE_MEMORIA: Final = re.compile(r"\b(que recuerdas|te acuerdas|que sabes) (de|sobre)\b")
# "explica" admite también "explícame/explícanos/explicarme" (extensión natural del patrón normativo).
_RE_TECNICA: Final = re.compile(r"\b(explica(?:me|nos|rme)?|como funciona|por que|diferencia entre)\b")

_ORDEN_MEDIA: Final = {
    "pausa": ("play_pause", "pausar o reanudar la reproducción"),
    "pon pausa": ("play_pause", "pausar o reanudar la reproducción"),
    "reanuda": ("play_pause", "pausar o reanudar la reproducción"),
    "siguiente cancion": ("siguiente", "pasar a la siguiente canción"),
    "cancion anterior": ("anterior", "volver a la canción anterior"),
}

# ---------------------------------------------------------------------------
# Reglas v0.2 (docs/COMPANION.md §C3 "Grupo orquestacion"), sobre texto normalizado
# ---------------------------------------------------------------------------

_AMADEUS: Final = r"(?:amadeus|kurisu|asistente)"
_VOCATIVO_INICIO: Final = rf"(?:(?:oye|hey|ey|hola),? )?(?:{_AMADEUS},? )?"
_VOCATIVO_FIN: Final = rf"(?:,? {_AMADEUS})?(?:,? (?:por favor|porfa))?[.!]*"
# Frases cortas: solo valen como mensaje COMPLETO (anclas ^…$).
_RE_VENIR: Final = re.compile(
    rf"^{_VOCATIVO_INICIO}(?:ven|ven aca|ven aqui|veni|aparece|aparecete|sal|sal ya|asomate)"
    rf"{_VOCATIVO_FIN}$")
_RE_VETE: Final = re.compile(
    rf"^{_VOCATIVO_INICIO}(?:vete|vete ya|escondete|ocultate|desaparece)(?: un rato)?{_VOCATIVO_FIN}$")
_RE_BASTA: Final = re.compile(
    rf"^{_VOCATIVO_INICIO}(?:basta|basta ya|ya basta|quieta|quieta ya|quieta ahi|para ya|ya para"
    r"|deja (?:el|mi) (?:mouse|raton|cursor)|suelta (?:el|mi) (?:mouse|raton|cursor)"
    rf"|deja (?:el|mi) (?:mouse|raton|cursor) en paz){_VOCATIVO_FIN}$")
_RE_VISION_PAUSA: Final = re.compile(
    rf"^{_VOCATIVO_INICIO}(?:deja de mirarme|deja de verme|no me mires|no me veas|cierra los ojos)"
    r"(?: (?:por|durante) (?P<n>\d{1,4}|un|una|media) ?(?P<u>minutos?|min|horas?|h))?"
    rf"{_VOCATIVO_FIN}$")
# "búscame / llámame en 10 minutos (para …)".
_RE_BUSCAME: Final = re.compile(
    r"^(?:buscame|llamame|ven a buscarme|ven por mi|vuelve a buscarme)[,:]?\s+(?P<resto>.+)$")
# "ponlo / pásalo / publícalo en <alias>": el último borrador de Amadeus.
_RE_PUBLICAR: Final = re.compile(
    r"^(?:ponlo|ponla|pasalo|pasala|publicalo|publicala|mandalo|mandala|copialo|pegalo|subelo|postealo)"
    r"\s+(?:en|a|al|por)\s+(?:el |la |mi )?(?P<alias>[a-z0-9_]+)[.!?]*$")
# "abre whatsapp y escríbele a mamá: …" (una sola línea).
_RE_ABRIR_Y_ESCRIBIR: Final = re.compile(
    r"^(?:abre|abreme|lanza|pon)\s+(?:el |la |mi )?(?P<alias>[a-z0-9_]+)\s+y\s+"
    r"(?:escribele|escribeles|escribe|mandale|enviale|ponle|pon|publica)(?:\s+a\s+(?P<dest>[^:,]+?))?"
    r"(?:\s*[:,]\s*|\s+que\s+|\s+diciendo\s+)(?P<texto>.+)$")
# "escríbele a X por <alias>: …", "mándale un mensaje a X por <alias> que …".
_RE_PRERRELLENAR: Final = re.compile(
    r"^(?:escribele|escribeles|mandale|enviale|manda|envia|escribe)"
    r"(?:\s+un\s+(?:mensaje|correo|mail|whatsapp|tweet|post|dm))?(?:\s+a\s+(?P<dest>[^:,]+?))?"
    r"\s+(?:por|en|al)\s+(?:el |la |mi )?(?P<alias>[a-z0-9_]+)"
    r"(?:\s*[:,]\s*|\s+que\s+|\s+diciendo\s+)(?P<texto>.+)$")
# "pon en <alias>: …".
_RE_PONER_EN: Final = re.compile(
    r"^(?:pon|publica|postea|escribe)\s+en\s+(?:el |la |mi )?(?P<alias>[a-z0-9_]+)\s*[:,]\s*(?P<texto>.+)$")
# "escribe ahí / aquí: …" (ventana previa).
_RE_ESCRIBIR_AQUI: Final = re.compile(
    r"^(?:escribe|escribelo|teclea|pon|ponlo|pega)\s+(?:ahi|aqui|alli|aca)"
    r"(?:\s*[:,]\s*|\s+que\s+|\s+)(?P<texto>.+)$")
# "escríbele (a X): …" sin destino: tras "abre whatsapp" (mensajes consecutivos) o en la ventana previa.
_RE_ESCRIBELE: Final = re.compile(
    r"^(?:escribele|escribeles|mandale|enviale|respondele|contestale)(?:\s+a\s+(?P<dest>[^:,]+?))?"
    r"(?:\s*[:,]\s*|\s+que\s+|\s+diciendo\s+)(?P<texto>.+)$")
# "busca <consulta> en <alias>", "pon <canción> en spotify".
_RE_BUSCAR_EN: Final = re.compile(
    r"^(?:busca|buscame|buscar|pon|ponme|reproduce)\s+(?P<q>.+?)\s+(?:en|por)\s+(?:el |la |mi )?"
    r"(?P<alias>[a-z0-9_]+)[.!?]*$")
# "abre / pon / lanza <alias>" (sustituye a la regla _RE_LANZAR de la v0.1: catálogo → RAMS_APPS).
_RE_ABRIR: Final = re.compile(
    r"^(?:abre|abrir|abreme|lanza|ejecuta|pon|ponme|inicia|arranca)\s+(?:el |la |mi |un |una )?"
    r"(?P<alias>[a-z0-9_]+)[.!?]*$")
# v0.2.1: "abre <nombre de varias palabras>" (apps del menú Inicio de Windows). Se evalúa DESPUÉS
# del catálogo, de RAMS_APPS, de la música y del volumen. Con "pon/ponme" el verbo no es explícito:
# main solo abre si la coincidencia es única (para no chocar con "pon música en spotify").
_RE_ABRIR_APP: Final = re.compile(
    r"^(?P<verbo>abre|abreme|abrir|lanza|lanzame|ejecuta|ejecutame|inicia|iniciame|arranca|pon|ponme)\s+"
    r"(?P<articulo>el |la |los |las |mi |un |una )?(?P<nombre>[a-z0-9][a-z0-9 +._'&\-]{0,59}?)[\s.!?]*$")
_VERBOS_APP_NO_EXPLICITOS: Final = frozenset({"pon", "ponme"})

# Sinónimos de serie (se suman a los de RAMS_DESTINOS, que tienen prioridad). Nunca nombres de
# navegadores concretos: "abre firefox" sigue siendo una app de RAMS_APPS.
SINONIMOS_DESTINO: Final[Mapping[str, str]] = {
    "twitter": "x", "wasap": "whatsapp", "wasa": "whatsapp", "whats": "whatsapp", "wpp": "whatsapp",
    "insta": "instagram", "ig": "instagram", "face": "facebook", "fb": "facebook", "yt": "youtube",
    "mail": "correo", "email": "correo", "explorador": "archivos",
    # v0.2.1: nombres de varias palabras (los dice la regla de apps locales antes del menú Inicio)
    "explorador de archivos": "archivos", "mis archivos": "archivos", "correo electronico": "correo",
    "whatsapp web": "whatsapp", "telegram web": "telegram",
}
NOTA_ESCRIBIR_DESPUES: Final = "escribir_despues"

# ---------------------------------------------------------------------------
# v0.2.1: órdenes naturales (vocativos, cortesías y peticiones)
# ---------------------------------------------------------------------------

# Vocativos que se quitan al principio y al final de una ORDEN ("asistente, abre steam porfavor").
_VOCATIVOS: Final = frozenset({"asistente", "assistant", "amadeus", "kurisu", "christina", "cristina", "kristina",
                               "makise"})
# Cortesías de una palabra, también pegadas, estiradas o con erratas: "porfavor", "porfavorr",
# "pofavor", "porfa", "porfaa", "porfis", "pls", "plis", "pliss", "plz", "please", "xfa", "xfis".
_RE_CORTESIA: Final = re.compile(
    r"p+o+r*f+a+(?:v+o+r*)?s*|p+o+r*f+i+s*|p+l+i+[sz]+|p+l+[sz]+|p+l+e+a+s+e*|x+f+a+(?:v+o+r*)?|x+f+i+s*")
# Segunda palabra de "por favor" / "por fa" / "x favor" / "por favorr".
_RE_FAVOR: Final = re.compile(r"f+a+(?:v+o+r*)?")
_PRIMERAS_POR_FAVOR: Final = frozenset({"por", "x", "pr"})
# Lo que puede ir delante de una orden sin cambiarla ("oye", "hola", "bueno", "a ver"…).
_RELLENOS_INICIO_ORDEN: Final = _VOCATIVOS | frozenset({
    "oye", "oiga", "hey", "ey", "eh", "ehh", "ehm", "hola", "bueno", "ok", "okay", "vale", "dale", "listo",
})
_PARES_INICIO_ORDEN: Final = (("a", "ver"), ("hola", "hola"))
# Por el FINAL solo se recorta en las órdenes (nunca en un texto dictado: borradores, recordatorios).
_FINALES_ORDEN: Final = _VOCATIVOS | frozenset({"gracias"})
_PARES_FINAL_ORDEN: Final = (("muchas", "gracias"), ("si", "puedes"), ("ahora", "mismo"))
_RE_NUCLEO: Final = re.compile(r"^[^\w]+|[^\w]+$")
_RE_COMA_FINAL: Final = re.compile(r"[,;:]+$")

# Peticiones que equivalen a una orden: "¿puedes abrir X?", "me abres X", "quiero que abras X".
_RE_PETICION_INFINITIVO: Final = re.compile(
    r"^(?P<me1>me\s+)?(?P<modal>puedes|podrias|podes|pudieras|quieres|quisieras|podria)\s+(?:por favor\s+)?"
    r"(?P<me2>me\s+)?"
    r"(?P<verbo>abrir(?:me)?|lanzar(?:me)?|ejecutar(?:me)?|iniciar(?:me)?|arrancar(?:me)?|poner(?:me)?"
    r"|buscar(?:me)?|pausar|reproducir(?:me)?|mandar(?:me)?|enviar(?:me)?|tomar(?:me)?|sacar(?:me)?)\b")
_MODALES_DE_DESEO: Final = frozenset({"quieres", "quisieras"})
_RE_PETICION_ME: Final = re.compile(
    r"^me\s+(?P<verbo>abres|abririas|pones|pondrias|buscas|buscarias|lanzas|mandas|mandarias|envias|enviarias"
    r"|tomas|tomarias|sacas|sacarias)\b")
_RE_PETICION_QUE: Final = re.compile(
    r"^(?:quiero|necesito|quisiera)\s+que\s+(?:me\s+)?(?P<verbo>abras|pongas|busques|lances|ejecutes|inicies"
    r"|mandes|envies|tomes|saques)\b")
_IMPERATIVOS: Final[Mapping[str, str]] = {
    "abrir": "abre", "lanzar": "lanza", "ejecutar": "ejecuta", "iniciar": "inicia", "arrancar": "arranca",
    "poner": "pon", "buscar": "busca", "pausar": "pausa", "reproducir": "reproduce", "mandar": "mandame",
    "enviar": "enviame", "tomar": "tomame", "sacar": "sacame",
    "abres": "abre", "abririas": "abre", "pones": "pon", "pondrias": "pon", "buscas": "busca",
    "buscarias": "busca", "lanzas": "lanza", "mandas": "mandame", "mandarias": "mandame", "envias": "enviame",
    "enviarias": "enviame", "tomas": "tomame", "tomarias": "tomame", "sacas": "sacame", "sacarias": "sacame",
    "abras": "abre", "pongas": "pon", "busques": "busca", "lances": "lanza", "ejecutes": "ejecuta",
    "inicies": "inicia", "mandes": "mandame", "envies": "enviame", "tomes": "tomame", "saques": "sacame",
}
# Con "me" la orden pasa a la forma pronominal ("me puedes abrir steam" → "ábreme steam").
_IMPERATIVOS_ME: Final[Mapping[str, str]] = {"abre": "abreme", "pon": "ponme", "busca": "buscame",
                                              "lanza": "lanzame", "reproduce": "reproduceme"}
# Objetos genéricos: "¿puedes abrir archivos/apps/programas?" es una pregunta teórica, no una orden.
_OBJETOS_GENERICOS: Final = frozenset({
    "archivos", "apps", "aplicaciones", "programas", "cosas", "paginas", "sitios", "webs", "ventanas", "carpetas",
    "documentos", "enlaces", "links", "pestanas", "juegos", "algo", "cualquier", "cualquiera", "otra", "otras",
    "otro", "otros", "mas", "nada", "eso", "esto", "aplicacion", "app", "programa",
})
_ARTICULOS: Final = frozenset({"el", "la", "los", "las", "mi", "mis", "un", "una", "unos", "unas"})
# "abre la app de Steam" y "abre steam ya": lo que sobra alrededor del nombre de una app.
_PREFIJOS_APP: Final = frozenset({"app", "aplicacion", "programa", "juego"})
_COLAS_APP: Final = frozenset({"ya", "ahora", "ahorita", "rapido", "rapidito"})
# "abre youtube en el navegador / en chrome": el sufijo solo sobra si lo que queda es un destino web.
_RE_EN_NAVEGADOR: Final = re.compile(
    r"^(?P<base>.+?)\s+(?:en|con|desde|por)\s+(?:el |la |mi )?(?:navegador|browser|internet|la web|web"
    r"|google chrome|chrome|microsoft edge|edge|mozilla firefox|firefox|brave|opera)$")
# Verbos que también se usan fuera de las apps ("lanza una moneda", "arranca el carro", "ejecuta el
# plan"): si el nombre no se parece a ninguna app del menú Inicio, main lo trata como conversación.
_VERBOS_APP_TENTATIVOS: Final = frozenset({"lanza", "lanzame", "ejecuta", "ejecutame", "inicia", "iniciame",
                                           "arranca"})
# "abre X en el pc / en mi computador": el sufijo sobra en una orden de abrir.
_RE_EN_EL_PC: Final = re.compile(
    r"^(?:abre|abreme|abrir|lanza|lanzame|ejecuta|inicia|arranca|pon|ponme)\b.*?"
    r"(?P<sufijo>\s+(?:en|del|desde)\s+(?:el |la |mi |tu )?(?:pc|computador|computadora|compu|ordenador|portatil"
    r"|laptop))[\s.!?]*$")
# Nombres que nunca son una app ("abre tu mente", "abre la ventana", "abre los ojos").
_NO_ES_APP_INICIO: Final = frozenset({"tu", "tus", "su", "sus", "nuestro", "nuestra", "nuestros", "nuestras",
                                      "vuestro", "vuestra", "mis", "esa", "ese", "eso", "esta", "este", "esto",
                                      "bien", "mas", "paso", "que"})
_NO_ES_APP: Final = frozenset({
    "mente", "corazon", "ojos", "ojo", "boca", "alma", "puerta", "puertas", "regalo", "regalos", "caja", "ventana",
    "ventanas", "nevera", "llave", "grifo", "botella", "paquete", "sobre", "mano", "manos", "brazos", "piernas",
    "camino", "debate", "discusion", "conversacion", "tema", "hilo", "chat", "sesion", "cuenta", "mensaje",
    "carta", "cortinas", "cortina", "cajon", "pestana", "pausa", "musica", "cancion", "volumen",
})

# ---------------------------------------------------------------------------
# v0.2.1: fotos de la cámara o de la pantalla y respuestas en audio (sobre la ORDEN limpia)
# ---------------------------------------------------------------------------

# Verbos que piden la imagen YA tomada o por tomar ("mándame", "tómame", "sácame", "hazme una foto").
_VERBOS_FOTO_FUERTES: Final = r"mandame|enviame|pasame|tomame|toma|saca|sacame|hazme|haz|tirame"
_VERBO_FOTO_FUERTE: Final = rf"(?:{_VERBOS_FOTO_FUERTES})"
# Verbos que no bastan por sí solos ("dame / muéstrame / enséñame / quiero una imagen" puede ser
# "genérame" o "enséñame una cualquiera"): solo cuentan con "de la cámara / webcam".
_VERBO_FOTO: Final = rf"(?:{_VERBOS_FOTO_FUERTES}|dame|muestrame|ensename|quiero)"
_PC: Final = r"(?:\s+(?:del|de mi|de la|desde el|desde mi)\s+(?:pc|computador|computadora|compu|ordenador))?"
_DE_CAMARA: Final = r"\s+(?:de|con|desde)\s+(?:la|tu|mi)\s+(?:camara|webcam)"
# La webcam solo se usa con una petición clara: "mándame una foto", "tómame una foto (con la
# cámara)", "foto de la cámara (del pc)", "muéstrame una imagen de la webcam". "imagen" a secas
# ("hazme una imagen", "¿me mandas una imagen?") no es la cámara: suele pedir que se genere una.
_RE_FOTO_CAMARA: Final = re.compile(
    rf"^(?:{_VERBO_FOTO_FUERTE}\s+(?:una?\s+)?(?:foto|fotico|fotito|fotografia)(?:{_DE_CAMARA})?"
    rf"|{_VERBO_FOTO}\s+(?:una?\s+)?(?:foto|fotico|fotito|fotografia|imagen){_DE_CAMARA}"
    rf"|(?:una?\s+)?(?:foto|fotografia|imagen){_DE_CAMARA})"
    rf"{_PC}(?:\s+(?:ahora|ya|ahorita))?[\s.!?]*$")
# "mándame una captura (de pantalla)", "hazme un pantallazo", "foto de la pantalla", "captura de pantalla".
_RE_CAPTURA: Final = re.compile(
    rf"^(?:{_VERBO_FOTO}\s+)?(?:una?\s+|la\s+)?"
    r"(?:captura(?:\s+de\s+(?:la\s+|mi\s+|tu\s+)?pantalla)?|screenshot|pantallazo"
    r"|(?:foto|imagen|fotografia)\s+de\s+(?:la|mi|tu)\s+pantalla)"
    rf"{_PC}(?:\s+(?:ahora|ya|ahorita))?[\s.!?]*$")
# "mándame la pantalla", "muéstrame mi pantalla" (fuera de la terminal: la manda a Telegram).
_RE_MANDAME_PANTALLA: Final = re.compile(
    rf"^(?:mandame|enviame|pasame|muestrame|ensename)\s+(?:la|mi|tu)\s+pantalla{_PC}[\s.!?]*$")
# Solo por Telegram: "¿qué hay en mi pantalla?" (en local sigue siendo mirar_pantalla).
_RE_QUE_HAY_PANTALLA: Final = re.compile(rf"^(?:que hay|que tengo|que se ve) en (?:la|mi) pantalla{_PC}[\s.!?]*$")
# Solo por Telegram: "¿cómo me veo?" (en local es un vistazo de la cámara).
_RE_COMO_ME_VEO: Final = re.compile(r"^como me veo(?:\s+(?:hoy|ahora|ahorita))?[\s.!?]*$")
# Pedir la respuesta en audio (mensaje completo): "mándame un audio", "háblame", "respóndeme por audio".
_DESTINO_AUDIO: Final = r"(?:\s+(?:al|a mi|por|en mi|en el)\s+(?P<dest>telegram|celular|cel|movil|telefono))?"
_RE_PEDIR_AUDIO: Final = re.compile(
    r"^(?:(?:mandame|enviame|pasame|dejame|grabame|hazme)\s+(?:una?\s+)?"
    r"(?:audio|audiito|audito|nota de voz|notita de voz|mensaje de voz|vocal)"
    r"|hablame|hablame un (?:rato|poco)|quiero (?:escucharte|oirte|oir tu voz|escuchar tu voz)"
    r"|(?:respondeme|contestame|dimelo|dime|hablame)\s+(?:por|en|con)\s+(?:un\s+|una\s+)?(?:audio|voz|nota de voz)"
    r"|(?:respondeme|contestame|dimelo|hablame)\s+con tu voz)"
    rf"{_DESTINO_AUDIO}[\s.!?]*$")
# Un mensaje con contenido que además pide la respuesta en audio ("cuéntame un chiste por audio").
# Se evalúa sobre la ORDEN limpia (sin cortesías ni puntuación al final). "audio" como TEMA del
# mensaje ("videos con audio", "tengo un problema con audio", "exportar como audio") no cuenta:
# - "por/en/vía (un) audio | nota de voz" solo AL FINAL del mensaje;
# - "con tu voz" al final y detrás de un verbo con pronombre ("cuéntame…", "dímelo…");
# - un verbo de respuesta junto a la forma ("respóndeme por audio", "mándamelo como nota de voz");
# - "mándame un audio" (el audio es el objeto pedido).
_AUDIO: Final = r"(?:audio|audiito|audito|nota de voz|notita de voz|mensaje de voz)"
_RE_QUIERE_AUDIO: Final = re.compile(
    rf"(?:^|\s)(?:por|en|via)\s+(?:un\s+|una\s+)?{_AUDIO}$"
    r"|\b\w+(?:me|melo|mela|nos|noslo)\b.*\bcon tu (?:propia\s+)?voz$"
    r"|\b(?:mandame|enviame|grabame|dejame|hazme)\s+(?:un|una)\s+(?:audio|audiito|nota de voz|mensaje de voz)\b"
    r"|\b(?:respondeme|contestame|dimelo|dime|cuentamelo|explicamelo|mandamelo|enviamelo|pasamelo|leemelo)"
    rf"\s+(?:por|en|con|como|via)\s+(?:un\s+|una\s+)?(?:voz|{_AUDIO})\b")


def _destino(alias: str, destinos: Mapping[str, str]) -> str | None:
    """Clave de ``DESTINOS_BASE`` para ``alias`` (sinónimo de ``destinos`` o de serie), o ``None``."""
    if not alias:
        return None
    clave = destinos.get(alias)
    if clave is None:
        clave = SINONIMOS_DESTINO.get(alias)
    return clave if isinstance(clave, str) and clave in DESTINOS_BASE else None


def _admite(clave: str, capacidad: str) -> bool:
    """``DESTINOS_BASE[clave][capacidad]`` sin lanzar."""
    meta = DESTINOS_BASE.get(clave)
    return bool(meta.get(capacidad)) if isinstance(meta, Mapping) else False


def _nombre_destino(clave: str) -> str:
    """Nombre para la descripción de la acción (la clave; nunca lleva el texto del usuario).

    La descripción que ve el usuario en una confirmación la escribe la compuerta de permisos.
    """
    return clave


def _tramo_original(originales: list[str], normalizadas: list[str], inicio: int, fin: int) -> str:
    """Palabras originales del tramo ``[inicio, fin)`` del texto normalizado (cortes por palabra).

    Si un extremo cae a mitad de palabra, devuelve el tramo normalizado (nunca pierde contenido).
    """
    texto_norm = " ".join(normalizadas)
    if not (0 <= inicio < fin <= len(texto_norm)):
        return ""
    en_borde_inicio = inicio == 0 or texto_norm[inicio - 1] == " "
    en_borde_fin = fin == len(texto_norm) or texto_norm[fin] == " "
    if not (en_borde_inicio and en_borde_fin):
        return texto_norm[inicio:fin].strip()
    primera = texto_norm[:inicio].count(" ")
    ultima = texto_norm[:fin].count(" ") + 1
    return " ".join(originales[primera:ultima]).strip()


def _texto_de(originales: list[str], normalizadas: list[str], inicio: int) -> str:
    """Texto que dicta el usuario a partir de ``inicio`` (original, sin comillas que lo envuelvan)."""
    texto = _limpiar_fragmento(_original_desde(originales, normalizadas, inicio))
    if len(texto) >= 2 and texto[0] in "\"'«“" and texto[-1] in "\"'»”":
        texto = texto[1:-1].strip()
    return texto


def _minutos_pausa(m: re.Match[str]) -> float | None:
    """Minutos de "no me mires por N minutos" (``None`` = sin plazo)."""
    bruto = m.group("n")
    if bruto is None:
        return None
    cantidad = {"un": 1.0, "una": 1.0, "media": 0.5}.get(bruto)
    if cantidad is None:
        cantidad = float(int(bruto))
    unidad = m.group("u") or "min"
    return cantidad * (60.0 if unidad.startswith("h") else 1.0)


def _recordatorio_personal(originales: list[str], normalizadas: list[str], ahora: datetime,
                           tecnica: bool) -> Intencion | None:
    """"búscame en 10 minutos (para …)" → ``recordatorio_personal{vence, mensaje}`` o ``None``."""
    originales, normalizadas = _sin_relleno_inicial(originales, normalizadas)
    if not normalizadas:
        return None
    reconocida, vence = _analizar_cuando(" ".join(normalizadas), ahora)
    mensaje = ""
    if not reconocida:
        for fin in range(len(normalizadas) - 1, 0, -1):
            reconocida, vence = _analizar_cuando(" ".join(normalizadas[:fin]), ahora)
            if reconocida:
                resto = originales[fin:]
                if resto and normalizar(resto[0]).strip(",:") in ("para", "que", "y", "a", "porque"):
                    resto = resto[1:]
                mensaje = _limpiar_mensaje(resto).rstrip(".!?…").strip()
                break
    if not reconocida or vence is None:
        return None
    return Intencion("recordatorio_personal", {"vence": vence, "mensaje": mensaje or MENSAJE_BUSCAR_DEFECTO,
                                               "con_mensaje": bool(mensaje)},
                     explicacion_tecnica=tecnica)


def _nucleo(palabra: str) -> str:
    """Palabra normalizada sin la puntuación de los extremos (``"asistente,"`` → ``"asistente"``)."""
    return _RE_NUCLEO.sub("", palabra)


def _quitar_coma_final(originales: list[str], normalizadas: list[str]) -> tuple[list[str], list[str]]:
    """Quita ``,;:`` pegados a la última palabra (lo que deja un vocativo final recortado)."""
    if not normalizadas:
        return originales, normalizadas
    ultima_norm = _RE_COMA_FINAL.sub("", normalizadas[-1])
    ultima_orig = _RE_COMA_FINAL.sub("", originales[-1])
    if not ultima_norm or not ultima_orig:
        return originales, normalizadas
    return [*originales[:-1], ultima_orig], [*normalizadas[:-1], ultima_norm]


def _limpiar_inicio(originales: list[str], normalizadas: list[str]) -> tuple[list[str], list[str]]:
    """Quita vocativos, saludos y cortesías del PRINCIPIO ("asistente, …", "oye, …", "por favor …").

    Nunca deja la lista vacía: "hola amadeus" se queda como está. Sirve también para los textos
    dictados (lo dictado va al final y no se toca).
    """
    while len(normalizadas) > 1:
        primera = _nucleo(normalizadas[0])
        par = (primera, _nucleo(normalizadas[1]))
        if (par in _PARES_INICIO_ORDEN or _es_por_favor(*par)) and len(normalizadas) > 2:
            originales, normalizadas = originales[2:], normalizadas[2:]
        elif primera in _RELLENOS_INICIO_ORDEN or _es_cortesia(primera) or not primera:
            originales, normalizadas = originales[1:], normalizadas[1:]
        else:
            break
    return originales, normalizadas


def _es_cortesia(palabra: str, *, ingles: bool = True) -> bool:
    """``True`` si ``palabra`` (núcleo normalizado) es una cortesía ("porfa", "pls", "porfavorr").

    ``ingles=False`` deja fuera "please", que también puede ser contenido ("la letra de please").
    """
    if not palabra or _RE_CORTESIA.fullmatch(palabra) is None:
        return False
    return ingles or not palabra.startswith("ple")


def _es_por_favor(primera: str, segunda: str) -> bool:
    """``True`` para "por favor", "por fa", "x favor", "x fa", "por favorr" (núcleos normalizados)."""
    return primera in _PRIMERAS_POR_FAVOR and _RE_FAVOR.fullmatch(segunda) is not None


def _recortar_final(originales: list[str], normalizadas: list[str], *,
                    cita: bool) -> tuple[list[str], list[str]]:
    """Quita vocativos y cortesías del FINAL (nunca deja la lista vacía).

    ``cita=False`` (órdenes): vocativos, "gracias", "please" y todas las cortesías. ``cita=True``
    (la consulta de una búsqueda, que cita al usuario): solo las cortesías españolas, que nunca son
    contenido ("porfa", "pls", "por favor"); un vocativo, "gracias" o "please" solo si van
    separados por una coma ("busca el clima, amadeus"): "busca videos de amadeus" busca "amadeus".
    """
    cambiado = True
    while cambiado and len(normalizadas) > 1:
        cambiado = False
        ultima = _nucleo(normalizadas[-1])
        penultima = _nucleo(normalizadas[-2])
        tras_coma = bool(_RE_COMA_FINAL.search(normalizadas[-2]))
        if len(normalizadas) > 2 and (_es_por_favor(penultima, ultima)
                                      or (not cita and (penultima, ultima) in _PARES_FINAL_ORDEN)):
            originales, normalizadas = originales[:-2], normalizadas[:-2]
            cambiado = True
        elif (not ultima or _es_cortesia(ultima, ingles=not cita)
              or ((not cita or tras_coma) and (ultima in _FINALES_ORDEN or _es_cortesia(ultima)))):
            originales, normalizadas = originales[:-1], normalizadas[:-1]
            cambiado = True
        if cambiado:
            originales, normalizadas = _quitar_coma_final(originales, normalizadas)
    return originales, normalizadas


def _limpiar_cita(originales: list[str], normalizadas: list[str]) -> tuple[list[str], list[str]]:
    """Versión del mensaje para las reglas que CITAN al usuario (la consulta de "busca …").

    Sin rellenos al principio y solo sin cortesías puras al final (ver :func:`_recortar_final`):
    los nombres del personaje ("busca información sobre makise kurisu") son contenido.
    """
    originales, normalizadas = _limpiar_inicio(originales, normalizadas)
    return _recortar_final(originales, normalizadas, cita=True)


def _limpiar_orden(originales: list[str], normalizadas: list[str]) -> tuple[list[str], list[str]]:
    """Versión "orden" del mensaje: sin vocativos ni cortesías al principio NI al final.

    "asistente abre steam porfavor" → "abre steam"; "Puedes abrir YouTube en el PC pls" pasa
    después por :func:`_peticion_a_orden`. Nunca deja la lista vacía.
    """
    originales, normalizadas = _limpiar_inicio(originales, normalizadas)
    originales, normalizadas = _recortar_final(originales, normalizadas, cita=False)
    # La puntuación final sobra en una orden ("¿me abres Steam?"): así el nombre sale del texto original.
    while normalizadas:
        ultima_norm = _RE_PUNTUACION_FINAL.sub("", normalizadas[-1])
        ultima_orig = _RE_PUNTUACION_FINAL.sub("", originales[-1]).rstrip("¿¡")
        if ultima_norm and ultima_orig:
            originales, normalizadas = [*originales[:-1], ultima_orig], [*normalizadas[:-1], ultima_norm]
            break
        if len(normalizadas) == 1:
            break
        originales, normalizadas = originales[:-1], normalizadas[:-1]
    return originales, normalizadas


def _es_objeto_generico(normalizadas: list[str]) -> bool:
    """``True`` si el objeto de "¿puedes abrir …?" es genérico ("archivos", "apps", "algo")."""
    palabras = [_nucleo(p) for p in normalizadas]
    palabras = [p for p in palabras if p]
    while palabras and palabras[0] in _ARTICULOS:
        palabras = palabras[1:]
    return not palabras or palabras[0] in _OBJETOS_GENERICOS


def _peticion_a_orden(originales: list[str], normalizadas: list[str]) -> tuple[list[str], list[str]]:
    """"¿puedes abrir X?" / "me abres X" / "quiero que abras X" → "abre X" (y "abre X en el pc" → "abre X").

    Las preguntas teóricas con objeto genérico ("¿puedes abrir archivos?", "¿podrías abrir apps?")
    no se convierten: no son una orden y las explica Broca. Las palabras que siguen al verbo
    conservan el texto original (consultas y nombres).
    """
    texto = " ".join(normalizadas)
    for patron in (_RE_PETICION_INFINITIVO, _RE_PETICION_ME, _RE_PETICION_QUE):
        m = patron.match(texto)
        if m is None:
            continue
        verbo = m.group("verbo")
        base = verbo[:-2] if verbo.endswith("me") and verbo[:-2] in _IMPERATIVOS else verbo
        imperativo = _IMPERATIVOS.get(base)
        if imperativo is None:
            break
        con_me = verbo != base or patron is _RE_PETICION_ME or bool(
            m.groupdict().get("me1") or m.groupdict().get("me2")) or " que me " in f" {texto[:m.end()]} "
        if base == "buscar" and m.groupdict().get("modal") in _MODALES_DE_DESEO and not con_me:
            # "¿quieres buscar problemas / pareja?" pregunta por un deseo: no es "busca problemas".
            break
        if con_me:
            imperativo = _IMPERATIVOS_ME.get(imperativo, imperativo)
        indice = texto[:m.end()].count(" ") + 1     # palabras que ocupa la petición
        resto_orig, resto_norm = originales[indice:], normalizadas[indice:]
        if not resto_norm or _es_objeto_generico(resto_norm):
            return originales, normalizadas
        originales, normalizadas = [imperativo, *resto_orig], [imperativo, *resto_norm]
        break
    m = _RE_EN_EL_PC.match(" ".join(normalizadas))
    if m is not None:
        quitar = m.group("sufijo").strip().count(" ") + 1
        if len(normalizadas) - quitar >= 2:
            originales, normalizadas = _quitar_coma_final(originales[:-quitar], normalizadas[:-quitar])
    return originales, normalizadas


def quiere_audio(texto: str) -> bool:
    """``True`` si el mensaje pide la respuesta en audio ("cuéntame un chiste por audio", "mándame un
    audio", "dímelo con tu voz"). main lo usa por Telegram con ``RAMS_TELEGRAM_AUDIO=auto``."""
    if not normalizar(texto):
        return False
    orden = " ".join(_nucleo(p) or p for p in _limpiar_orden(*_palabras(texto))[1])
    return _RE_QUIERE_AUDIO.search(orden) is not None or _RE_PEDIR_AUDIO.match(orden) is not None


def _clasificar_multimedia(texto_orden: str, canal: str, tecnica: bool) -> Intencion | None:
    """Fotos (cámara o pantalla) y peticiones de audio, sobre la orden limpia (mensaje completo)."""
    if _RE_CAPTURA.match(texto_orden) or _RE_MANDAME_PANTALLA.match(texto_orden):
        return Intencion("enviar_foto", {"fuente": "pantalla"}, explicacion_tecnica=tecnica)
    if _RE_FOTO_CAMARA.match(texto_orden):
        return Intencion("enviar_foto", {"fuente": "camara"}, explicacion_tecnica=tecnica)
    if canal == "telegram":
        # Por Telegram no hay pantalla ni cámara delante: "¿qué hay en mi pantalla?" y "¿cómo me
        # veo?" se contestan con la imagen. En local siguen siendo mirar_pantalla y vistazo.
        if _RE_QUE_HAY_PANTALLA.match(texto_orden):
            return Intencion("enviar_foto", {"fuente": "pantalla"}, explicacion_tecnica=tecnica)
        if _RE_COMO_ME_VEO.match(texto_orden):
            return Intencion("enviar_foto", {"fuente": "camara"}, explicacion_tecnica=tecnica)
    elif _RE_COMO_ME_VEO.match(texto_orden):
        return Intencion("vistazo", {}, explicacion_tecnica=tecnica)
    m = _RE_PEDIR_AUDIO.match(texto_orden)
    if m:
        args = {"destino": "telegram"} if m.group("dest") else {}
        return Intencion("pedir_audio", args, explicacion_tecnica=tecnica)
    return None


def _clasificar_app_local(originales: list[str], normalizadas: list[str], destinos: Mapping[str, str],
                          tecnica: bool) -> Intencion | None:
    """"abre steam", "ábreme el bloc de notas", "pon visual studio code" → app del menú Inicio.

    Primero mira si el nombre es un destino del catálogo dicho con varias palabras ("explorador de
    archivos") o seguido del navegador ("abre youtube en chrome" → YouTube). Descarta lo que nunca
    es una app ("abre tu mente", "abre los ojos", "abre la ventana") y los objetos genéricos
    ("abre programas").

    ``args``: ``explicito`` (``False`` con "pon/ponme": main solo abre si la app es única) y
    ``tentativa`` (solo si es ``True``: verbo que también se usa fuera de las apps — "lanza una
    moneda", "arranca el carro" — o nombre con artículo indefinido — "abre un archivo" —; si nada
    del menú Inicio se le parece, main lo trata como conversación en vez de "No encuentro…").
    """
    texto = " ".join(normalizadas)
    m = _RE_ABRIR_APP.match(texto)
    if m is None:
        return None
    tramo = _tramo_original(originales, normalizadas, m.start("nombre"), m.end("nombre"))
    pares = [(orig, _nucleo(norm)) for orig, norm in zip(tramo.split(), m.group("nombre").split())]
    pares = [(orig, norm) for orig, norm in pares if norm]
    # "abre la app de Steam" → "Steam"; "abre steam ya" → "steam"; "abre steam pliss" → "steam".
    if len(pares) > 2 and pares[0][1] in _PREFIJOS_APP and pares[1][1] in ("de", "del"):
        pares = pares[2:]
    while len(pares) > 1:
        if len(pares) > 2 and _es_por_favor(pares[-2][1], pares[-1][1]):
            pares = pares[:-2]
        elif pares[-1][1] in _COLAS_APP or _es_cortesia(pares[-1][1]):
            pares = pares[:-1]
        else:
            break
    palabras = [norm for _orig, norm in pares]
    nombre_norm = " ".join(palabras)
    if not palabras or palabras[0] in _NO_ES_APP_INICIO or any(p in _NO_ES_APP for p in palabras):
        return None
    if _es_objeto_generico(palabras):
        return None
    clave = _destino(nombre_norm, destinos)
    if clave is not None:
        return _accion("abrir_destino", {"alias": clave}, f"abrir {_nombre_destino(clave)}", tecnica)
    en_navegador = _RE_EN_NAVEGADOR.match(nombre_norm)
    if en_navegador is not None:
        # "abre youtube en el navegador": el destino se abre en el navegador predeterminado. Si lo
        # que queda no es un destino web ("abre steam en chrome"), no es una orden de app.
        clave = _destino(en_navegador.group("base"), destinos)
        if clave is None:
            return None
        return _accion("abrir_destino", {"alias": clave}, f"abrir {_nombre_destino(clave)}", tecnica)
    nombre = _limpiar_fragmento(" ".join(orig for orig, _norm in pares))
    nombre = nombre.strip(" .!?¿¡\"'«»")[:60] or nombre_norm[:60]
    verbo = m.group("verbo")
    args: dict[str, Any] = {"nombre": nombre, "local": True, "explicito": verbo not in _VERBOS_APP_NO_EXPLICITOS}
    if verbo in _VERBOS_APP_TENTATIVOS or m.group("articulo") in ("un ", "una "):
        args["tentativa"] = True
    return _accion("lanzar_app", args, f"abrir {nombre}", tecnica)


def _clasificar_v02(original: str, originales: list[str], normalizadas: list[str], ahora: datetime,
                    destinos: Mapping[str, str], tecnica: bool,
                    orden: tuple[list[str], list[str]] | None = None) -> Intencion | None:
    """Reglas de la v0.2 que van antes de las búsquedas web (gana la primera que casa).

    ``orden`` (v0.2.1) es la versión limpia del mensaje (sin vocativos ni cortesías al final) para
    las reglas que son órdenes (publicar, buscar en un destino); las que citan un texto dictado
    usan ``originales``/``normalizadas`` (solo limpias por el principio).
    """
    texto = " ".join(normalizadas)
    orden_orig, orden_norm = orden if orden is not None else (originales, normalizadas)
    texto_orden = " ".join(orden_norm)

    m = _RE_PUBLICAR.match(texto_orden)
    if m:
        clave = _destino(m.group("alias"), destinos)
        if clave is not None:
            return Intencion("publicar_borrador", {"alias": clave}, explicacion_tecnica=tecnica)

    m = _RE_ABRIR_Y_ESCRIBIR.match(texto)
    if m:
        clave = _destino(m.group("alias"), destinos)
        texto_usuario = _texto_de(originales, normalizadas, m.start("texto"))
        if clave is not None and texto_usuario:
            nombre = _nombre_destino(clave)
            if _admite(clave, "prerrelleno"):
                return _accion("prerrellenar", {"alias": clave, "texto": texto_usuario}, f"borrador en {nombre}",
                               tecnica)
            # Sin prerrelleno no se teclea a ciegas en una app que aún se está abriendo: se abre y
            # Amadeus pide el "escribe ahí: …" cuando el usuario tenga la caja de texto delante.
            intencion = _accion("abrir_destino", {"alias": clave}, f"abrir {nombre}", tecnica)
            return Intencion("accion_pc", {**intencion.args, "nota": NOTA_ESCRIBIR_DESPUES},
                             explicacion_tecnica=tecnica)

    for patron in (_RE_PRERRELLENAR, _RE_PONER_EN):
        m = patron.match(texto)
        if not m:
            continue
        clave = _destino(m.group("alias"), destinos)
        texto_usuario = _texto_de(originales, normalizadas, m.start("texto"))
        if clave is None or not texto_usuario:
            continue
        nombre = _nombre_destino(clave)
        if _admite(clave, "prerrelleno"):
            return _accion("prerrellenar", {"alias": clave, "texto": texto_usuario}, f"borrador en {nombre}",
                           tecnica)
        # Sin enlace de prerrelleno (Instagram, TikTok, Discord…): teclear en la ventana previa.
        intencion = _accion("escribir_texto", {"texto": texto_usuario}, f"escribir en {nombre}", tecnica)
        return Intencion("accion_pc", {**intencion.args, "destino": clave}, explicacion_tecnica=tecnica)

    m = _RE_ESCRIBIR_AQUI.match(texto)
    if m:
        texto_usuario = _texto_de(originales, normalizadas, m.start("texto"))
        if texto_usuario:
            return _accion("escribir_texto", {"texto": texto_usuario}, "escribir en la ventana anterior", tecnica)

    m = _RE_ESCRIBELE.match(texto)
    if m:
        texto_usuario = _texto_de(originales, normalizadas, m.start("texto"))
        if texto_usuario:
            intencion = _accion("escribir_texto", {"texto": texto_usuario}, "escribir en la ventana anterior",
                                tecnica)
            # main puede convertirlo en prerrelleno si el usuario acaba de abrir una app que lo admite.
            return Intencion("accion_pc", {**intencion.args, "sin_alias": True}, explicacion_tecnica=tecnica)

    m = _RE_BUSCAR_EN.match(texto_orden)
    if m:
        clave = _destino(m.group("alias"), destinos)
        if clave is not None and _admite(clave, "busqueda"):
            consulta = _limpiar_fragmento(_tramo_original(orden_orig, orden_norm, m.start("q"), m.end("q")))
            if consulta:
                return _accion("buscar_en", {"alias": clave, "consulta": consulta},
                               f"buscar en {_nombre_destino(clave)}", tecnica)
    return None


# ---------------------------------------------------------------------------
# v0.2.2: Google, correo, Drive y taller de scripts (docs/V022_CORREO_DRIVE_SCRIPTS.md §4)
# ---------------------------------------------------------------------------
# Ninguna de estas reglas crea una AccionPC: el borrador, la operación de Drive o el script se
# preparan en main y la acción nace al pulsar el botón de su tarjeta (§0.2). Todas las regex van
# sobre texto normalizado (sin tildes); los textos que citan al usuario salen del ORIGINAL.

MAX_TAREA_TALLER: Final = 500
MAX_NOMBRE_HABILIDAD: Final = 40
MAX_DESTINATARIOS_INTENCION: Final = 10
MAX_ADJUNTOS_INTENCION: Final = 5
MAX_REF_DRIVE: Final = 25
MAX_NOMBRE_DRIVE: Final = 200

# 4. Taller explícito: «hazme un script que …», «escribe un script para …» (la tarea es dictada).
_RE_TALLER: Final = re.compile(
    r"^(?:hazme|haz|escribe|escribeme|crea|creame|genera|generame|prepara|preparame|arma|armame)\s+"
    r"(?:un|el)\s+script\b[\s,:]*(?:(?:que|para|de)\b[\s,:]*)?(?P<tarea>.*)$")
# Habilidades guardadas (sobre la orden limpia).
_RE_HABILIDADES: Final = re.compile(
    r"^(?:(?:que|cuales)\s+(?:son\s+)?(?:tus\s+|mis\s+|las\s+)?(?:habilidades|scripts\s+guardados)"
    r"(?:\s+(?:tienes|hay|guardaste|tengo|guardadas|guardados))?"
    r"|(?:lista|listame|muestra|muestrame|ensename|dime)\s+(?:tus|mis|las)\s+(?:habilidades|scripts\s+guardados)"
    r"|(?:tus|mis)\s+habilidades)$")
_RE_HABILIDAD: Final = re.compile(
    r"^(?P<verbo>ejecuta|ejecutame|usa|usame|corre|correme|aplica|olvida|olvidate\s+de|borra|borrame|elimina"
    r"|quita)\s+(?:la|mi|tu)\s+habilidad\s+(?:de\s+|llamada\s+)?(?P<nombre>.+)$")
_VERBOS_OLVIDAR_HABILIDAD: Final = frozenset({"olvida", "borra", "borrame", "elimina", "quita"})

# 6. Cuenta de Google.
_GOOGLE_OBJ: Final = (r"(?:(?:mi|la|el|tu)\s+)?(?:cuenta\s+(?:de\s+)?)?"
                      r"(?:google(?:\s+drive)?|gmail|drive|correo\s+de\s+google)")
_RE_GOOGLE_CONECTAR: Final = re.compile(
    r"^(?:(?:puedes|podrias|me\s+puedes)\s+)?"
    r"(?:conecta|conectame|conectar|conectarme|vincula|vinculame|vincular|enlaza|enlazame|enlazar|autoriza"
    rf"|autorizar)\s+{_GOOGLE_OBJ}(?:\s+(?:y|e|con)\s+{_GOOGLE_OBJ})*"
    r"(?:\s+(?:contigo|a\s+amadeus|con\s+amadeus))?$")
_RE_GOOGLE_DESCONECTAR: Final = re.compile(
    r"^(?:desconecta|desconectame|desconectar|desvincula|desvinculame|desvincular|desenlaza|revoca|quita"
    rf"(?:le)?\s+el\s+acceso\s+a)\s+{_GOOGLE_OBJ}(?:\s+(?:y|e)\s+{_GOOGLE_OBJ})*$")
_RE_GOOGLE_ESTADO: Final = re.compile(
    rf"^(?:estado\s+de\s+{_GOOGLE_OBJ}"
    rf"|(?:esta|estas|sigue|sigues)\s+conectad[oa]\s+(?:a\s+|con\s+)?{_GOOGLE_OBJ}"
    rf"|{_GOOGLE_OBJ}\s+(?:esta|sigue)\s+conectad[oa]"
    rf"|tengo\s+{_GOOGLE_OBJ}\s+conectad[oa])$")

# 7. Correo: «(manda|envía|escribe|redacta)(le)? (un)? correo a <destinatarios> …».
_RE_CORREO: Final = re.compile(
    r"^(?P<verbo>manda|mandale|mandales|envia|enviale|enviales|escribe|escribele|escribeles|redacta|redactale"
    r"|redactales|redactame)\s+(?:un\s+|una\s+|el\s+|otro\s+)?"
    r"(?:correo(?:\s+electronico)?|email|e-mail|mail|emilio)\s+(?:a|para|al)\s+(?P<resto>\S.*)$")
# Peticiones corteses de correo o de script (sobre las normalizadas, tras _limpiar_inicio): «¿puedes
# mandarle…?», «me puedes escribir…», «quiero que le mandes…», «¿le envías…?», «¿me haces un script…?».
# Cada forma solo con su tiempo verbal (infinitivo tras el modal, subjuntivo tras «que», presente o
# condicional a secas); la palabra resultante es el imperativo que esperan _RE_CORREO / _RE_TALLER.
_RE_PETICION_MODAL_TAREA: Final = re.compile(
    r"^(?:me\s+)?(?:puedes|podrias|podes|pudieras|podria|quieres|quisieras)\s+(?:por\s+favor,?\s+)?"
    r"(?:(?:me|le|les)\s+)?(?P<verbo>[a-z]+?)(?:me|le|les)?\s")
_RE_PETICION_QUE_TAREA: Final = re.compile(
    r"^(?:quiero|necesito|quisiera)\s+que\s+(?:(?:me|le|les)\s+)?(?P<verbo>[a-z]+?)(?:me|le|les)?\s")
_RE_PETICION_DIRECTA_TAREA: Final = re.compile(r"^(?:(?:me|le|les)\s+)?(?P<verbo>[a-z]+?)(?:me|le|les)?\s")
_PETICION_CORREO: Final[Mapping[str, Mapping[str, str]]] = {
    "modal": {"mandar": "manda", "enviar": "envia", "escribir": "escribe", "redactar": "redacta"},
    "que": {"mandes": "manda", "envies": "envia", "escribas": "escribe", "redactes": "redacta"},
    "directa": {"mandas": "manda", "envias": "envia", "escribes": "escribe", "redactas": "redacta",
                "mandarias": "manda", "enviarias": "envia", "escribirias": "escribe", "redactarias": "redacta"},
}
_PETICION_TALLER: Final[Mapping[str, Mapping[str, str]]] = {
    "modal": {"hacer": "haz", "escribir": "escribe", "crear": "crea", "generar": "genera", "preparar": "prepara",
              "armar": "arma"},
    "que": {"hagas": "haz", "escribas": "escribe", "crees": "crea", "generes": "genera", "prepares": "prepara",
            "armes": "arma"},
    "directa": {"haces": "haz", "escribes": "escribe", "creas": "crea", "generas": "genera", "preparas": "prepara",
                "armas": "arma", "harias": "haz", "escribirias": "escribe", "crearias": "crea",
                "generarias": "genera", "prepararias": "prepara", "armarias": "arma"},
}
_GERUNDIO_RE: Final = re.compile(r"[a-z]{2,}(?:ando|iendo|yendo)(?:le|les|lo|la|los|las|me|nos|selo|sela)?")
_INFINITIVO_RE: Final = re.compile(r"[a-z]{2,}(?:ar|er|ir)(?:le|les|lo|la|los|las|me|nos|selo|sela)?")
_DICIENDO: Final = frozenset({"diciendo", "diciendole", "diciendoles"})
_SEPARADORES_LISTA: Final = frozenset({"y", "e", "a", "al", "para"})
_PALABRAS_ADJUNTA: Final = frozenset({"adjunta", "adjuntale", "adjuntales", "adjuntame", "adjuntando",
                                      "adjuntandole", "adjuntas", "adjuntes", "adjuntar", "adjuntarle"})
_NOMBRES_ADJUNTO: Final = frozenset({"archivo", "archivos", "adjunto", "adjuntos", "documento", "documentos",
                                     "fichero", "ficheros"})
_ARTICULOS_ADJUNTO: Final = _NOMBRES_ADJUNTO | frozenset({"el", "la", "los", "las", "mi", "mis"})
_RE_UNIDAD_WINDOWS: Final = re.compile(r"^[A-Za-z]:")

# 8. Drive. «borra» solo con «de (mi) drive»; sin mencionar Drive, solo referencias numéricas a la
# última lista («descarga el 2») o con sustantivo de documento («léeme el doc de presupuesto»).
_DRIVE_RE: Final = r"(?:(?:mi|el|tu|la)\s+)?(?:google\s+)?drive"
_EN_DRIVE: Final = rf"(?:de|del|en|desde)\s+{_DRIVE_RE}"
_RE_DRIVE_BUSCAR_EN: Final = re.compile(
    rf"^(?:busca|buscame|buscar|encuentra|encuentrame)\s+en\s+{_DRIVE_RE}"
    r"(?:\s+(?:sobre|acerca\s+de|por))?(?:\s+(?P<q>\S.*))?$")
_RE_DRIVE_BUSCAR_FIN: Final = re.compile(
    rf"^(?:busca|buscame|buscar|encuentra|encuentrame)\s+(?P<q>.+?)\s+en\s+{_DRIVE_RE}$")
_RE_DRIVE_QUE_TENGO: Final = re.compile(
    rf"^que\s+(?:tengo|hay|guarde)\s+en\s+{_DRIVE_RE}"
    r"(?:\s+(?:sobre|acerca\s+de|relacionado\s+con|con|del?)\s+(?P<q>\S.*))?$")
_RE_DRIVE_LISTA: Final = re.compile(
    r"^(?:(?:lista|listame|muestra|muestrame|ensename|dame)\s+(?:(?:los\s+|mis\s+)?(?:archivos|documentos)"
    rf"(?:\s+recientes)?\s+(?:de|en|del)\s+)?{_DRIVE_RE}(?:\s+recientes?)?"
    rf"|(?:(?:los|mis)\s+)?(?:archivos|documentos)\s+recientes\s+(?:de|en|del)\s+{_DRIVE_RE}"
    rf"|(?:lo\s+)?(?:mas\s+)?reciente\s+(?:de|en|del)\s+{_DRIVE_RE})$")
_RE_DRIVE_INFO: Final = re.compile(
    r"^(?:(?:dame|muestrame|dime|ensename)\s+(?:los\s+|la\s+)?)?(?:detalles|detalle|info|informacion|datos"
    r"|propiedades)\s+(?:del?|sobre(?:\s+el|\s+la)?|de\s+la)\s+(?P<resto>.+)$")
_RE_DRIVE_DESCARGAR: Final = re.compile(r"^(?:descarga|descargame|descargar|baja|bajame|bajar)\s+(?P<resto>.+)$")
_RE_DRIVE_LEER: Final = re.compile(r"^(?:resume|resumeme|resumir|resumelo|lee|leeme|leer)\s+(?P<resto>.+)$")
_RE_DRIVE_CARPETA: Final = re.compile(
    r"^(?:crea|creame|crear|haz|hazme)\s+(?:una\s+)?(?:nueva\s+)?carpeta\s+(?P<resto>.+)$")
_RE_DRIVE_SUBIR: Final = re.compile(r"^(?:sube|subeme|subir|carga|cargame|cargar)\s+(?P<resto>.+)$")
_RE_DRIVE_PAPELERA: Final = re.compile(
    r"^(?:manda|mueve|tira|envia|echa|pasa|lleva)\s+(?:(?P<ref1>.+?)\s+a\s+la\s+papelera"
    rf"|a\s+la\s+papelera\s+(?P<ref2>.+?))(?P<dd>\s+{_EN_DRIVE})?$")
_RE_DRIVE_BORRA: Final = re.compile(
    r"^(?:borra|borrame|elimina|eliminame)\s+(?P<ref>.+?)\s+de\s+(?:mi\s+)?(?:google\s+)?drive$")
_RE_DRIVE_PUBLICO: Final = re.compile(
    r"^(?:(?:haz|hazme|vuelve|pon|deja)\s+(?:publico|publica)\s+(?P<ref1>.+?)"
    r"|(?:haz|vuelve|pon|deja)\s+(?P<ref2>.+?)\s+(?:publico|publica)"
    r"|comparte\s+(?:el\s+)?(?:enlace|link|vinculo)\s+(?:del?|de\s+la)\s+(?P<ref3>.+?)\s+con\s+(?:cualquiera"
    r"|todos|todo\s+el\s+mundo|cualquier\s+persona)"
    r"|comparte\s+(?P<ref4>.+?)\s+(?:con\s+(?:cualquiera|todos|todo\s+el\s+mundo|cualquier\s+persona)"
    r"|publicamente|en\s+publico|para\s+todos))"
    rf"(?P<dd>\s+{_EN_DRIVE})?"
    r"(?:\s+(?P<editable>para\s+(?:que\s+)?(?:puedan\s+)?editar(?:lo|la)?|editable|como\s+editora?"
    r"|con\s+edicion|con\s+permiso\s+de\s+edicion))?$")
_RE_DRIVE_COMPARTIR: Final = re.compile(
    r"^(?:comparte|compartele|compartir)\s+(?P<ref>.+?)\s+con\s+(?P<correo>[^\s@]+@[^\s@]+\.[a-z]{2,24})"
    r"(?:\s+(?:como|de)\s+(?P<rol>lectora?|comentarista|comentadora?|editora?)"
    r"|\s+(?:para\s+que|que)\s+(?:lo\s+|la\s+)?pueda\s+(?P<verbo>ver|leer|comentar|editar))?$")
_SUF_FORMATO: Final = re.compile(
    r"\s+(?:como|en\s+formato|en|a|formato)\s+(?:un\s+|una\s+)?(?P<f>pdf|texto(?:\s+plano)?|txt|csv|markdown"
    r"|original|word|excel|docx|xlsx|pptx|powerpoint)$")
_SUF_DESTINO: Final = re.compile(
    r"\s+(?:en|a|al)\s+(?:la\s+carpeta\s+(?:de\s+)?)?(?:mis\s+|el\s+|la\s+|mi\s+)?"
    r"(?P<d>descargas|documentos|escritorio)$")
_SUF_DRIVE: Final = re.compile(rf"\s+{_EN_DRIVE}$")
_FORMATOS_INTENCION: Final[Mapping[str, str]] = {
    "pdf": "pdf", "texto": "texto", "texto plano": "texto", "txt": "texto", "csv": "texto", "markdown": "texto",
    "original": "original", "word": "original", "excel": "original", "docx": "original", "xlsx": "original",
    "pptx": "original", "powerpoint": "original",
}
_SUSTANTIVOS_DOC: Final = frozenset({"documento", "documentos", "doc", "docs", "archivo", "archivos", "hoja",
                                     "hojas", "presentacion", "pdf", "carpeta", "fichero"})
_DETERMINANTES_REF: Final = frozenset({"el", "la", "los", "las", "un", "una", "mi", "mis", "este", "esta", "ese",
                                       "esa", "aquel", "aquella"})
_ENLACE_REF: Final = frozenset({"de", "del", "llamado", "llamada", "que", "se", "llama", "numero", "num", "no", "n",
                                "nro"})
_ORDINALES: Final[Mapping[str, int]] = {
    "primero": 1, "primer": 1, "primera": 1, "segundo": 2, "segunda": 2, "tercero": 3, "tercer": 3,
    "tercera": 3, "cuarto": 4, "cuarta": 4, "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6, "septimo": 7,
    "septima": 7, "octavo": 8, "octava": 8, "noveno": 9, "novena": 9, "decimo": 10, "decima": 10,
}
_RE_NUMERO_REF: Final = re.compile(r"#?(?P<n>\d{1,2})(?:ro|ra|do|da|to|ta|vo|va|no|na|mo|ma|º|°|o|a)?")
_PREFIJOS_NOMBRE_CARPETA: Final = (("que", "se", "llame"), ("con", "el", "nombre"), ("de", "nombre"),
                                   ("llamada",), ("llamado",), ("nombrada",))


def _forma_de_orden(normalizadas: list[str],
                    tabla: Mapping[str, Mapping[str, str]]) -> tuple[str, int, bool] | None:
    """Petición cortés → ``(imperativo, palabras que ocupa la petición, es_pregunta)`` o ``None``.

    «puedes mandarle un correo …» → ``("manda", 2, True)``; «quiero que le mandes …» → ``("manda", 4,
    False)``. Solo cambia el PRINCIPIO: el resto conserva sus índices (los cortes por palabra sobre el
    texto original siguen valiendo). ``es_pregunta``: forma interrogativa (modal o presente), cuyo «?»
    final es de la petición.
    """
    texto = " ".join(normalizadas) + " "
    for clave, patron in (("modal", _RE_PETICION_MODAL_TAREA), ("que", _RE_PETICION_QUE_TAREA),
                          ("directa", _RE_PETICION_DIRECTA_TAREA)):
        m = patron.match(texto)
        if m is None:
            continue
        imperativo = tabla[clave].get(m.group("verbo"))
        if imperativo is not None:
            return imperativo, texto[:m.end()].count(" "), clave != "que"
    return None


def _sin_interrogacion_de_peticion(texto: str) -> str:
    """Quita el «?» final que pertenece a la petición («¿me haces un script que liste mis fotos?»)."""
    if texto.endswith("?") and "¿" not in texto:
        return texto.rstrip("?").rstrip()
    return texto


def _palabras_de_span(texto: str, inicio: int, fin: int) -> tuple[int, int]:
    """Índices de palabra ``[i0, i1)`` del tramo ``[inicio, fin)`` de ``texto`` (palabras unidas por espacios)."""
    if fin <= inicio:
        return texto[:inicio].count(" "), texto[:inicio].count(" ")
    return texto[:inicio].count(" "), texto[:fin].count(" ") + 1


def _sufijo_original(original: str, n_palabras: int) -> str:
    """Las últimas ``n_palabras`` palabras (con contenido) de ``original`` con su espaciado y saltos de línea.

    El texto que el usuario DICTA para un correo (cuerpo o tema) se corta del original, no de las
    palabras unidas por espacios: así conserva los saltos de línea. Mismo filtro que :func:`_palabras`.
    """
    posiciones = [m.start() for m in re.finditer(r"\S+", original) if normalizar(m.group())]
    if n_palabras <= 0 or n_palabras > len(posiciones):
        return ""
    return original[posiciones[-n_palabras]:].strip()


def _sin_comillas(texto: str) -> str:
    """Quita comillas que envuelven todo el texto y la puntuación suelta de los extremos.

    Solo para NOMBRES (alias, carpetas, referencias, habilidades). El texto que el usuario dicta
    para un correo (cuerpo, asunto, tema) usa :func:`_sin_comillas_envolventes`, que nunca deja
    una comilla sin pareja.
    """
    limpio = _limpiar_fragmento(texto)
    if len(limpio) >= 2 and limpio[0] in "\"'«“" and limpio[-1] in "\"'»”":
        limpio = limpio[1:-1].strip()
    return limpio.strip(" \t\"'«»“”")


_PAREJAS_COMILLAS: Final[Mapping[str, str]] = {"\"": "\"", "'": "'", "«": "»", "“": "”"}


def _sin_comillas_envolventes(texto: str) -> str:
    """Texto dictado sin las comillas que lo envuelven ENTERO (una pareja que se corresponde).

    «"hola"» → «hola»; «le diga a papá “te quiero”», «'casa123'» al final o «"Hola", dijo él»
    quedan intactos: una comilla suelta de un extremo es contenido del usuario (va a la tarjeta).
    """
    limpio = _limpiar_fragmento(texto)
    while len(limpio) >= 2:
        cierre = _PAREJAS_COMILLAS.get(limpio[0])
        interior = limpio[1:-1]
        if cierre is None or limpio[-1] != cierre or limpio[0] in interior or cierre in interior:
            break
        limpio = _limpiar_fragmento(interior)
    return limpio


def _mayuscula_inicial(texto: str) -> str:
    """Primera letra en mayúscula (tras signos de apertura o comillas)."""
    m = re.match(r"^([¿¡«\"'“(]*)(\w)", texto)
    if m is None or not m.group(2).islower():
        return texto
    return texto[:m.start(2)] + m.group(2).upper() + texto[m.end(2):]


def _es_que_conjuncion(palabra: str) -> bool:
    """``True`` solo para la conjunción «que» tal como se escribió (sin tilde ni ¿/¡).

    «qué», «¿qué» o «¡qué» son interrogativos o exclamativos: son contenido del correo, no el
    separador («diciendo que llegué» frente a «diciendo ¿qué hora es?»).
    """
    return palabra.strip(",:;").lower() == "que"


def _texto_dictado(original: str, n_palabras: int, *, quitar_que: bool, peticion: bool = False) -> str:
    """Cuerpo o tema dictado: sufijo del original, sin comillas envolventes, sin la conjunción «que»
    inicial y con mayúscula. ``peticion``: la orden era una pregunta («¿puedes mandarle…?»), así que un
    «?» final sin «¿» en lo dictado es de la petición, no del correo."""
    texto = _sin_comillas_envolventes(_sufijo_original(original, n_palabras))
    if quitar_que:
        partes = texto.split(maxsplit=1)
        if partes and _es_que_conjuncion(partes[0]):
            texto = _sin_comillas_envolventes(partes[1]) if len(partes) > 1 else ""
    if peticion:
        texto = _sin_comillas_envolventes(_sin_interrogacion_de_peticion(texto))
    return _mayuscula_inicial(texto)


def _items_lista(indices: list[int], orig: list[str], nuc: list[str], crudas: list[str]) -> list[str]:
    """Elementos de una lista dictada («ana@x.com y a pedro@y.com», «mamá, papá»), del texto original."""
    items: list[str] = []
    actual: list[str] = []

    def cerrar() -> None:
        if actual:
            for parte in re.split(r"[,;]", " ".join(actual)):
                limpio = parte.strip().strip(".:!?¿¡\"'«»“”").strip()
                if limpio:
                    items.append(limpio)
            actual.clear()

    for k in indices:
        if nuc[k] in ("y", "e") or (nuc[k] in _SEPARADORES_LISTA and not actual):
            cerrar()
            continue
        if not nuc[k]:
            if "," in crudas[k] or ";" in crudas[k]:
                cerrar()
            continue
        actual.append(orig[k])
        if crudas[k].endswith((",", ";")):
            cerrar()
    cerrar()
    return items


def _adjunto_valido(nombre: str) -> str | None:
    """Nombre o ruta relativa ("carpeta/archivo.ext") de un adjunto, o ``None`` si no es aceptable."""
    ruta = nombre.replace("\\", "/").strip()
    if not ruta or ruta.startswith("/") or _RE_UNIDAD_WINDOWS.match(ruta) or "~" in ruta:
        return None
    if any(parte in ("..", ".") for parte in ruta.split("/")) or "//" in ruta:
        return None
    return ruta[:255]


def _limpiar_adjunto(texto: str) -> str:
    """«el archivo informe.pdf» → «informe.pdf» (quita artículos y sustantivos si queda algo detrás)."""
    palabras = texto.split()
    while len(palabras) > 1 and normalizar(palabras[0]) in _ARTICULOS_ADJUNTO:
        palabras = palabras[1:]
    return " ".join(palabras)


def _correo(original: str, originales: list[str], normalizadas: list[str], inicio: int, verbo: str,
            tecnica: bool, *, peticion: bool = False) -> Intencion:
    """``correo{op:"preparar", …}`` con las palabras que siguen a «… un correo a» (desde ``inicio``).

    Destinatarios (Para) hasta un marcador: «con copia (oculta) a», «cc», «cco» → CC/CCO; «con
    asunto», «asunto:» → asunto (hasta «:» o «diciendo»); «adjunta…», «con el archivo» → adjuntos.
    El cuerpo va tras «diciendo», «que dice/diga», «que» o «:» (sin el «que» inicial y con la
    primera letra en mayúscula). Con «redacta…», o si tras el destinatario viene un gerundio
    («pidiendo vacaciones»), «sobre …» o «para <verbo> …», el resto es el TEMA y ``redactar=True``.
    Solo la conjunción «que» escrita así separa: «qué», «¿qué» o «¡qué» abren el cuerpo y se quedan.
    Nada se redacta aquí: los textos van crudos (§14.2). Lo que no cabe en el contrato (más de 10
    destinatarios por campo, más de 5 adjuntos o rutas absolutas o con «..») no se pierde en silencio:
    ``args["descartados"] = {"destinatarios": n, "adjuntos": m}`` (solo si algo se quitó), y main lo
    avisa en la tarjeta y en el turno.
    """
    orig = originales[inicio:]
    crudas = normalizadas[inicio:]
    nuc = [_nucleo(p) for p in crudas]
    n = len(nuc)
    secciones: dict[str, list[int]] = {"para": [], "cc": [], "cco": [], "asunto": [], "adjuntos": []}
    actual = "para"
    redactar = verbo.startswith("redact")
    desde: int | None = None        # primera palabra (relativa) del cuerpo o del tema
    quitar_que = False
    es_tema = redactar
    i = 0
    while i < n:
        w = nuc[i]
        sig = nuc[i + 1] if i + 1 < n else ""
        if not w:
            if ":" in crudas[i]:
                desde = i + 1
                break
            i += 1
            continue
        if w in _DICIENDO:
            desde, quitar_que = i + 1, True
            break
        if w == "que" and sig in ("dice", "diga", "diria", "diciendo"):
            # «que dice …» separa; «¿qué dice el informe?» ya ES el cuerpo (interrogativo).
            desde, quitar_que = (i + 2, True) if _es_que_conjuncion(orig[i]) else (i, False)
            break
        if w == "que" and actual != "asunto" and (secciones[actual] or actual != "para"):
            desde = i + 1 if _es_que_conjuncion(orig[i]) else i
            break
        if (w == "con" and sig == "copia") or (w == "copia" and actual == "para" and secciones["para"]):
            j = i + 2 if w == "con" else i + 1
            if j < n and nuc[j] == "oculta":
                actual, j = "cco", j + 1
            else:
                actual = "cc"
            if j < n and nuc[j] in ("a", "para", "al"):
                j += 1
            i = j
            continue
        if w in ("cc", "cco") and secciones["para"]:
            actual = w
            i += 2 if sig in ("a", "para", "al") else 1
            continue
        if w == "asunto" or (w == "con" and sig == "asunto") or (w in ("con", "de") and sig == "el"
                                                                 and i + 2 < n and nuc[i + 2] == "asunto"):
            if w == "asunto":
                i += 1
            elif sig == "asunto":
                i += 2
            else:
                i += 3
            actual = "asunto"
            continue
        if w in _PALABRAS_ADJUNTA or (w == "con" and sig in ("el", "los", "la", "las") and i + 2 < n
                                      and nuc[i + 2] in _NOMBRES_ADJUNTO):
            actual = "adjuntos"
            i += 1 if w in _PALABRAS_ADJUNTA else 2
            continue
        en_lista = actual in ("para", "cc", "cco")
        if en_lista and secciones[actual]:
            ultimo = secciones[actual][-1]
            tras_separador = nuc[ultimo] in _SEPARADORES_LISTA or crudas[ultimo].endswith((",", ";"))
            if not tras_separador:
                if w in ("sobre", "acerca"):
                    desde = i + (2 if w == "acerca" and sig == "de" else 1)
                    es_tema = True
                    break
                if _GERUNDIO_RE.fullmatch(w) and w not in _DICIENDO:
                    desde, es_tema = i, True
                    break
                if w == "para" and _INFINITIVO_RE.fullmatch(sig):
                    desde, es_tema = i, True
                    break
        secciones[actual].append(i)
        if crudas[i].endswith(":"):
            desde = i + 1
            break
        i += 1

    listas = {campo: _items_lista(secciones[campo], orig, nuc, crudas) for campo in ("para", "cc", "cco")}
    para, cc, cco = (listas[campo][:MAX_DESTINATARIOS_INTENCION] for campo in ("para", "cc", "cco"))
    destinatarios_fuera = sum(max(0, len(lista) - MAX_DESTINATARIOS_INTENCION) for lista in listas.values())
    adjuntos: list[str] = []
    adjuntos_fuera = 0
    for item in _items_lista(secciones["adjuntos"], orig, nuc, crudas):
        valido = _adjunto_valido(_limpiar_adjunto(item))
        if valido is None:
            adjuntos_fuera += 1                  # ruta absoluta, con «..» o «~»: el módulo nunca la vería
        elif valido not in adjuntos:
            adjuntos.append(valido)
    adjuntos_fuera += max(0, len(adjuntos) - MAX_ADJUNTOS_INTENCION)
    adjuntos = adjuntos[:MAX_ADJUNTOS_INTENCION]
    asunto = _sin_comillas_envolventes(" ".join(orig[k] for k in secciones["asunto"])) or None
    dictado = ""
    if desde is not None and desde < n:
        dictado = _texto_dictado(original, n - desde, quitar_que=quitar_que, peticion=peticion)
    cuerpo: str | None = None
    tema: str | None = None
    if es_tema:
        tema = dictado or None
    else:
        cuerpo = dictado or None
    args: dict[str, Any] = {"op": "preparar", "para": para, "cc": cc, "cco": cco, "asunto": asunto,
                            "cuerpo": cuerpo, "tema": tema, "redactar": bool(es_tema), "adjuntos": adjuntos}
    if destinatarios_fuera or adjuntos_fuera:
        args["descartados"] = {"destinatarios": destinatarios_fuera, "adjuntos": adjuntos_fuera}
    return Intencion("correo", args, explicacion_tecnica=tecnica)


def _ref_drive(orig: list[str], nuc: list[str]) -> tuple[str, bool, bool, bool] | None:
    """Referencia a un archivo de Drive: ``(ref, numerica, con_sustantivo, de_drive)`` o ``None``.

    «el 2», «el segundo», «el documento 1», «el 2 de drive» → ``"2"``; «el doc de presupuesto» →
    ``"presupuesto"`` (del original). Los números van de 1 a 25 (la última lista).
    """
    orig, nuc = list(orig), list(nuc)
    de_drive = False
    m = _SUF_DRIVE.search(" " + " ".join(nuc))
    if m is not None:
        quitar = (" " + " ".join(nuc))[m.start():].strip().count(" ") + 1
        if len(nuc) > quitar:
            orig, nuc = orig[:-quitar], nuc[:-quitar]
            de_drive = True
    sustantivo = False
    k = 0
    while k < len(nuc) - 1:
        if nuc[k] in _DETERMINANTES_REF or nuc[k] in _ENLACE_REF:
            k += 1
        elif nuc[k] in _SUSTANTIVOS_DOC:
            sustantivo = True
            k += 1
        else:
            break
    if k < len(nuc) and nuc[k] in _SUSTANTIVOS_DOC and k == len(nuc) - 1:
        return None                                   # «el archivo» a secas: sin referencia
    resto_nuc, resto_orig = nuc[k:], orig[k:]
    if not resto_nuc:
        return None
    if len(resto_nuc) == 1:
        palabra = resto_nuc[0]
        numero = _ORDINALES.get(palabra)
        m_num = _RE_NUMERO_REF.fullmatch(palabra)
        if numero is None and m_num is not None:
            numero = int(m_num.group("n"))
        if numero is not None:
            if 1 <= numero <= MAX_REF_DRIVE:
                return str(numero), True, sustantivo, de_drive
            return None
    nombre = _sin_comillas(" ".join(resto_orig))[:MAX_NOMBRE_DRIVE]
    return (nombre, False, sustantivo, de_drive) if nombre else None


def _sin_articulos(orig: list[str], nuc: list[str]) -> str:
    """Texto original sin artículos ni «sobre» al principio (consulta de Drive)."""
    k = 0
    while k < len(nuc) - 1 and nuc[k] in (_DETERMINANTES_REF | {"sobre", "acerca", "de"}):
        k += 1
    return _sin_comillas(" ".join(orig[k:]))


def _cortar_sufijos(resto: str) -> tuple[str, dict[str, str], bool]:
    """Quita del final «como pdf», «en documentos» y «de drive» (en cualquier orden)."""
    extra: dict[str, str] = {}
    de_drive = False
    texto = " " + resto
    for _ in range(4):
        m = _SUF_FORMATO.search(texto)
        if m is not None and "formato" not in extra:
            extra["formato"] = _FORMATOS_INTENCION.get(re.sub(r"\s+", " ", m.group("f")), "pdf")
            texto = texto[:m.start()]
            continue
        m = _SUF_DESTINO.search(texto)
        if m is not None and "destino" not in extra:
            extra["destino"] = m.group("d")
            texto = texto[:m.start()]
            continue
        m = _SUF_DRIVE.search(texto)
        if m is not None and not de_drive:
            de_drive = True
            texto = texto[:m.start()]
            continue
        break
    return texto.strip(), extra, de_drive


def _buscar_frase_drive(nuc: list[str], preposiciones: frozenset[str]) -> tuple[int, int] | None:
    """Índices ``[i0, i1)`` de «en (mi) (google) drive» dentro de ``nuc`` (o ``None``)."""
    for i, palabra in enumerate(nuc):
        if palabra not in preposiciones:
            continue
        j = i + 1
        if j < len(nuc) and nuc[j] in ("mi", "el", "tu", "la"):
            j += 1
        if j < len(nuc) and nuc[j] == "google":
            j += 1
        if j < len(nuc) and nuc[j] == "drive":
            return i, j + 1
    return None


def _nombre_de_carpeta(orig: list[str], nuc: list[str]) -> str:
    """Nombre dictado de una carpeta («llamada Facturas 2026» → «Facturas 2026»)."""
    for prefijo in _PREFIJOS_NOMBRE_CARPETA:
        if tuple(nuc[:len(prefijo)]) == prefijo and len(nuc) > len(prefijo):
            orig, nuc = orig[len(prefijo):], nuc[len(prefijo):]
            break
    return _sin_comillas(" ".join(orig))[:120]


def _partir_dentro_de(orig: list[str], nuc: list[str]) -> tuple[tuple[list[str], list[str]], str | None]:
    """Separa «X dentro de (la carpeta) Y» → ((X), «Y»)."""
    for i in range(len(nuc) - 1):
        if nuc[i] == "dentro" and nuc[i + 1] in ("de", "del"):
            j = i + 2
            if j + 1 < len(nuc) and nuc[j] == "la" and nuc[j + 1] == "carpeta":
                j += 2
            elif j < len(nuc) and nuc[j] == "carpeta":
                j += 1
            carpeta = _sin_comillas(" ".join(orig[j:]))[:MAX_NOMBRE_DRIVE] or None
            return (orig[:i], nuc[:i]), carpeta
    return (orig, nuc), None


def _drive_crear_carpeta(orig: list[str], nuc: list[str], tecnica: bool) -> Intencion | None:
    """«crea una carpeta Facturas 2026 en drive (dentro de Trabajo)»."""
    frase = _buscar_frase_drive(nuc, frozenset({"en", "de", "a"}))
    if frase is None:
        return None
    segmentos = [(orig[:frase[0]], nuc[:frase[0]]), (orig[frase[1]:], nuc[frase[1]:])]
    nombre = ""
    carpeta: str | None = None
    for seg_orig, seg_nuc in segmentos:
        (base_orig, base_nuc), dentro = _partir_dentro_de(seg_orig, seg_nuc)
        if dentro and carpeta is None:
            carpeta = dentro
        if base_nuc and not nombre:
            nombre = _nombre_de_carpeta(base_orig, base_nuc)
    if not nombre:
        return None
    args: dict[str, Any] = {"op": "crear_carpeta", "nombre": nombre}
    if carpeta:
        args["carpeta"] = carpeta
    return Intencion("drive", args, explicacion_tecnica=tecnica)


def _drive_subir(orig: list[str], nuc: list[str], tecnica: bool) -> Intencion | None:
    """«sube informe.pdf a drive», «sube docs/plan.xlsx a la carpeta Trabajo de drive»."""
    frase = _buscar_frase_drive(nuc, frozenset({"a", "al", "en", "de"}))
    if frase is None:
        return None
    carpeta_ini: int | None = None
    for i in range(len(nuc) - 2):
        if nuc[i] in ("a", "en") and nuc[i + 1] == "la" and nuc[i + 2] == "carpeta":
            carpeta_ini = i
            break
    fin_nombre = min(x for x in (frase[0], carpeta_ini) if x is not None)
    nombre = _sin_comillas(_limpiar_adjunto(" ".join(orig[:fin_nombre])))
    if not nombre:
        return None
    args: dict[str, Any] = {"op": "subir", "nombre": nombre[:255]}
    if carpeta_ini is not None:
        fin = frase[0] if frase[0] > carpeta_ini else len(nuc)
        carpeta = _sin_comillas(" ".join(orig[carpeta_ini + 3:fin]))
        if carpeta:
            args["carpeta"] = carpeta[:MAX_NOMBRE_DRIVE]
    return Intencion("drive", args, explicacion_tecnica=tecnica)


def _drive_ref(op: str, orig: list[str], nuc: list[str], tecnica: bool, *, de_drive: bool,
               exigir: str, extra: Mapping[str, Any] | None = None) -> Intencion | None:
    """``drive{op, ref, …}`` si la referencia cumple ``exigir`` (sin mencionar Drive).

    ``exigir``: ``"numero"`` (solo la última lista), ``"documento"`` (número o sustantivo de
    documento) o ``"nada"`` (cualquier referencia).
    """
    ref = _ref_drive(orig, nuc)
    if ref is None:
        return None
    texto_ref, numerica, sustantivo, ref_de_drive = ref
    mencion = de_drive or ref_de_drive
    if not mencion:
        if exigir == "numero" and not numerica:
            return None
        if exigir == "documento" and not (numerica or sustantivo):
            return None
    args: dict[str, Any] = {"op": op, "ref": texto_ref}
    args.update(extra or {})
    return Intencion("drive", args, explicacion_tecnica=tecnica)


def _clasificar_drive(orden_orig: list[str], orden_norm: list[str], tecnica: bool) -> Intencion | None:
    """Regla 8 (§4.2): Drive, sobre la orden limpia. Va antes de ``_clasificar_v02``."""
    texto = " ".join(orden_norm)
    nuc = [_nucleo(p) or p for p in orden_norm]

    def tramo(m: re.Match[str], grupo: str) -> tuple[list[str], list[str]]:
        i0, i1 = _palabras_de_span(texto, m.start(grupo), m.end(grupo))
        return orden_orig[i0:i1], nuc[i0:i1]

    for patron in (_RE_DRIVE_BUSCAR_EN, _RE_DRIVE_BUSCAR_FIN, _RE_DRIVE_QUE_TENGO):
        m = patron.match(texto)
        if m:
            consulta = _sin_articulos(*tramo(m, "q")) if m.group("q") else ""
            return Intencion("drive", {"op": "buscar", "consulta": consulta or None}, explicacion_tecnica=tecnica)
    if _RE_DRIVE_LISTA.match(texto):
        return Intencion("drive", {"op": "buscar", "consulta": None}, explicacion_tecnica=tecnica)

    m = _RE_DRIVE_INFO.match(texto)
    if m:
        intencion = _drive_ref("info", *tramo(m, "resto"), tecnica, de_drive=False, exigir="documento")
        if intencion is not None:
            return intencion

    m = _RE_DRIVE_DESCARGAR.match(texto)
    if m:
        resto, extra, de_drive = _cortar_sufijos(m.group("resto"))
        if resto:
            inicio = m.start("resto")
            i0, i1 = _palabras_de_span(texto, inicio, inicio + len(resto))
            intencion = _drive_ref("descargar", orden_orig[i0:i1], nuc[i0:i1], tecnica, de_drive=de_drive,
                                   exigir="documento", extra=extra)
            if intencion is not None:
                return intencion

    m = _RE_DRIVE_LEER.match(texto)
    if m:
        intencion = _drive_ref("leer", *tramo(m, "resto"), tecnica, de_drive=False, exigir="documento")
        if intencion is not None:
            return intencion

    m = _RE_DRIVE_CARPETA.match(texto)
    if m:
        intencion = _drive_crear_carpeta(*tramo(m, "resto"), tecnica)
        if intencion is not None:
            return intencion

    m = _RE_DRIVE_SUBIR.match(texto)
    if m:
        intencion = _drive_subir(*tramo(m, "resto"), tecnica)
        if intencion is not None:
            return intencion

    m = _RE_DRIVE_PAPELERA.match(texto)
    if m:
        grupo = "ref1" if m.group("ref1") is not None else "ref2"
        intencion = _drive_ref("papelera", *tramo(m, grupo), tecnica, de_drive=bool(m.group("dd")),
                               exigir="numero")
        if intencion is not None:
            return intencion
    m = _RE_DRIVE_BORRA.match(texto)
    if m:
        intencion = _drive_ref("papelera", *tramo(m, "ref"), tecnica, de_drive=True, exigir="nada")
        if intencion is not None:
            return intencion

    m = _RE_DRIVE_PUBLICO.match(texto)
    if m:
        grupo = next(g for g in ("ref1", "ref2", "ref3", "ref4") if m.group(g) is not None)
        rol = "writer" if m.group("editable") else "reader"
        intencion = _drive_ref("compartir", *tramo(m, grupo), tecnica, de_drive=bool(m.group("dd")),
                               exigir="documento", extra={"correo": None, "rol": rol, "publico": True})
        if intencion is not None:
            return intencion
    m = _RE_DRIVE_COMPARTIR.match(texto)
    if m:
        correo_orig, _ = tramo(m, "correo")
        correo = " ".join(correo_orig).strip(" .,;:«»\"'")
        clave_rol = m.group("rol") or m.group("verbo") or ""
        if clave_rol.startswith("edit"):
            rol = "writer"
        elif clave_rol.startswith("coment"):
            rol = "commenter"
        else:
            rol = "reader"
        intencion = _drive_ref("compartir", *tramo(m, "ref"), tecnica, de_drive=False, exigir="nada",
                               extra={"correo": correo, "rol": rol, "publico": False})
        if intencion is not None:
            return intencion
    return None


def _clasificar_taller(texto: str, originales: list[str], normalizadas: list[str], orden_orig: list[str],
                       orden_norm: list[str], tecnica: bool) -> Intencion | None:
    """Regla 4 (§4.2): taller explícito y habilidades (tras multimedia).

    También en forma de petición: «¿me puedes hacer un script que…?», «quiero que me escribas un
    script para…», «¿me haces un script…?» (:func:`_forma_de_orden`).
    """
    m = _RE_TALLER.match(texto)
    pregunta = False
    if m is None:
        forma = _forma_de_orden(normalizadas, _PETICION_TALLER)
        if forma is not None:
            imperativo, n_peticion, pregunta = forma
            originales = [imperativo, *originales[n_peticion:]]
            normalizadas = [imperativo, *normalizadas[n_peticion:]]
            m = _RE_TALLER.match(" ".join(normalizadas))
    if m:
        tarea = _texto_de(originales, normalizadas, m.start("tarea")) if m.group("tarea") else ""
        if pregunta:
            tarea = _sin_interrogacion_de_peticion(tarea)
        return Intencion("taller", {"op": "crear", "tarea": tarea[:MAX_TAREA_TALLER]}, explicacion_tecnica=tecnica)
    texto_orden = " ".join(orden_norm)
    if _RE_HABILIDADES.match(texto_orden):
        return Intencion("taller", {"op": "habilidades"}, explicacion_tecnica=tecnica)
    m = _RE_HABILIDAD.match(texto_orden)
    if m:
        nombre = _sin_comillas(_tramo_original(orden_orig, orden_norm, m.start("nombre"), m.end("nombre")))
        if nombre:
            verbo = m.group("verbo").split()[0]
            op = "olvidar_habilidad" if verbo in _VERBOS_OLVIDAR_HABILIDAD or verbo == "olvidate" \
                else "ejecutar_habilidad"
            return Intencion("taller", {"op": op, "nombre": nombre[:MAX_NOMBRE_HABILIDAD]},
                             explicacion_tecnica=tecnica)
    return None


def _clasificar_google_correo(original: str, originales: list[str], normalizadas: list[str],
                              texto_orden: str, destinos: Mapping[str, str], tecnica: bool) -> Intencion | None:
    """Reglas 6 y 7 (§4.2): cuenta de Google y correo (tras recordar, antes de Drive)."""
    if _RE_GOOGLE_CONECTAR.match(texto_orden):
        return Intencion("google", {"op": "conectar"}, explicacion_tecnica=tecnica)
    if _RE_GOOGLE_DESCONECTAR.match(texto_orden):
        return Intencion("google", {"op": "desconectar"}, explicacion_tecnica=tecnica)
    if _RE_GOOGLE_ESTADO.match(texto_orden):
        return Intencion("google", {"op": "estado"}, explicacion_tecnica=tecnica)
    texto = " ".join(normalizadas)
    m = _RE_CORREO.match(texto)
    desplazamiento = 0          # palabras de la petición cortés que ocupa el imperativo equivalente
    pregunta = False
    if m is None:
        # «¿puedes mandarle un correo a…?», «me puedes escribir un correo a…», «quiero que le mandes…».
        forma = _forma_de_orden(normalizadas, _PETICION_CORREO)
        if forma is None:
            return None
        imperativo, n_peticion, pregunta = forma
        texto = " ".join([imperativo, *normalizadas[n_peticion:]])
        m = _RE_CORREO.match(texto)
        if m is None:
            return None
        desplazamiento = n_peticion - 1
    # Excepción: «escríbele un correo a juan por gmail: …» sigue siendo el prerrelleno de siempre.
    prerrelleno = _RE_PRERRELLENAR.match(texto)
    if prerrelleno is not None and _destino(prerrelleno.group("alias"), destinos) is not None:
        return None
    inicio = texto[:m.start("resto")].count(" ") + desplazamiento
    return _correo(original, originales, normalizadas, inicio, m.group("verbo"), tecnica, peticion=pregunta)


def _accion(tipo: str, args: Mapping[str, Any], descripcion: str, tecnica: bool) -> Intencion:
    """Intención ``accion_pc`` con su ``AccionPC`` (siempre ``origen="usuario"``)."""
    return Intencion("accion_pc", {"accion": AccionPC(tipo, dict(args), descripcion, origen="usuario")},
                     explicacion_tecnica=tecnica)


def _lanzar_app_permitido(plataforma: str | None) -> bool:
    """Los alias de ``RAMS_APPS`` (``lanzar_app``) solo se ejecutan en Linux (Hyprland).

    ``plataforma=None`` (llamada sin contexto, p. ej. el autodiagnóstico) no restringe.
    """
    return plataforma is None or str(plataforma).startswith("linux")


def _clasificar(original: str, originales: list[str], normalizadas: list[str], ahora: datetime,
                apps: Mapping[str, str], tecnica: bool, destinos: Mapping[str, str] | None = None,
                plataforma: str | None = None, *, canal: str = "terminal",
                apps_locales: bool = False) -> Intencion:
    """Aplica la tabla de reglas de §10.12 (ampliada en la v0.2 y la v0.2.1) en orden; gana la primera.

    v0.2.1: todas las reglas ven el mensaje sin vocativos ni saludos al principio; las que son
    ÓRDENES (frases cortas, fotos/audio, abrir, buscar en un destino, música, cerrar) ven además la
    versión sin cortesías ni vocativos al final y con las peticiones convertidas en orden.
    """
    if destinos is None:
        destinos = {clave: clave for clave in DESTINOS_BASE}

    comando = parsear_comando(original)
    if comando is not None:
        return Intencion("comando", {"comando": comando[0], "resto": comando[1]}, explicacion_tecnica=tecnica)

    originales, normalizadas = _limpiar_inicio(originales, normalizadas)
    texto = " ".join(normalizadas)
    orden_orig, orden_norm = _peticion_a_orden(*_limpiar_orden(originales, normalizadas))
    texto_orden = " ".join(orden_norm)

    # v0.2: frases cortas ancladas (mensaje completo).
    if _RE_BASTA.match(texto_orden):
        return Intencion("basta_travesuras", {}, explicacion_tecnica=tecnica)
    if _RE_VENIR.match(texto_orden):
        return Intencion("escritorio_venir", {}, explicacion_tecnica=tecnica)
    if _RE_VETE.match(texto_orden):
        return Intencion("escritorio_vete", {}, explicacion_tecnica=tecnica)
    m = _RE_VISION_PAUSA.match(texto_orden)
    if m:
        return Intencion("vision_pausa", {"minutos": _minutos_pausa(m)}, explicacion_tecnica=tecnica)

    # v0.2.1: fotos de la cámara o de la pantalla y respuestas en audio (mensaje completo).
    multimedia = _clasificar_multimedia(texto_orden, canal, tecnica)
    if multimedia is not None:
        return multimedia

    # v0.2.2 (§4.2, regla 4): taller explícito y habilidades.
    taller = _clasificar_taller(texto, originales, normalizadas, orden_orig, orden_norm, tecnica)
    if taller is not None:
        return taller

    m = _RE_BUSCAME.match(texto)
    if m:
        indice = texto[:m.start("resto")].count(" ")
        personal = _recordatorio_personal(originales[indice:], normalizadas[indice:], ahora, tecnica)
        if personal is not None:
            return personal

    m = _RE_PROGRAMAR.match(texto)
    if m:
        indice = texto[:m.start("resto")].count(" ")
        return _programar(originales[indice:], normalizadas[indice:], ahora, capsula=False, tecnica=tecnica)

    m = _RE_CAPSULA.match(texto)
    if m:
        indice = texto[:m.start("resto")].count(" ")
        return _programar(originales[indice:], normalizadas[indice:], ahora, capsula=True, tecnica=tecnica)

    m = _RE_RECORDAR.match(texto)
    if m:
        contenido = _limpiar_fragmento(_original_desde(originales, normalizadas, m.start("x")))
        if contenido:
            return Intencion("recordar", {"contenido": contenido}, explicacion_tecnica=tecnica)

    # v0.2.2 (§4.2, reglas 6-8): cuenta de Google, correo (cede ante el prerrelleno con alias) y
    # Drive. Antes de _clasificar_v02: si no, «busca X en drive» sería una búsqueda web.
    google_correo = _clasificar_google_correo(original, originales, normalizadas, texto_orden, destinos, tecnica)
    if google_correo is not None:
        return google_correo
    drive = _clasificar_drive(orden_orig, orden_norm, tecnica)
    if drive is not None:
        return drive

    # v0.2: destinos, borradores y tecleo (antes de la búsqueda web: "busca X en YouTube").
    v02 = _clasificar_v02(original, originales, normalizadas, ahora, destinos, tecnica,
                          orden=(orden_orig, orden_norm))
    if v02 is not None:
        return v02

    # La consulta CITA al usuario: sin cortesías al final, pero conservando los nombres del
    # personaje ("busca información sobre makise kurisu", "busca videos de amadeus").
    base_orig, base_norm = _limpiar_cita(originales, normalizadas)
    cita_orig, cita_norm = _peticion_a_orden(base_orig, base_norm)
    m = _RE_BUSCAR.match(" ".join(cita_norm))
    if m:
        consulta = _limpiar_fragmento(_original_desde(cita_orig, cita_norm, m.start("q")))
        if cita_norm[:1] != base_norm[:1]:
            # "¿podrías buscar la verdad?": el signo es de la petición, no de la consulta.
            consulta = _limpiar_fragmento(consulta.rstrip(" ?!.…"))
        if consulta:
            return Intencion("buscar_web", {"consulta": consulta}, explicacion_tecnica=tecnica)

    if _RE_BUSCAR_DEBIL.search(texto):
        return Intencion("buscar_web", {"consulta": original.strip()}, confianza=0.6, explicacion_tecnica=tecnica)

    if _RE_REPORTE.search(texto):
        return Intencion("reporte_sistema", {}, explicacion_tecnica=tecnica)

    if _RE_PANTALLA.search(texto):
        return Intencion("mirar_pantalla", {}, explicacion_tecnica=tecnica)

    if _RE_VISTAZO.search(texto):
        return Intencion("vistazo", {}, explicacion_tecnica=tecnica)

    # v0.2: "abre <alias>": primero el catálogo de destinos; si no, un alias de RAMS_APPS
    # (lanzar_app, solo Linux). Si no es ninguno de los dos, siguen las demás reglas ("pon pausa").
    m = _RE_ABRIR.match(texto_orden)
    if m:
        alias = m.group("alias")
        clave = _destino(alias, destinos)
        if clave is not None:
            return _accion("abrir_destino", {"alias": clave}, f"abrir {_nombre_destino(clave)}", tecnica)
    m = _RE_LANZAR.match(texto_orden)
    if m and m.group("app") in apps and _lanzar_app_permitido(plataforma):
        app = m.group("app")
        return _accion("lanzar_app", {"alias": app}, f"abrir {app}", tecnica)

    m = _RE_WORKSPACE.search(texto)
    if m:
        n = int(m.group("n"))
        return _accion("ir_workspace", {"n": n}, f"cambiar al espacio {n}", tecnica)

    if _RE_NEXUS.search(texto):
        return _accion("enfocar_nexus", {}, "traer Nexus IDE al frente", tecnica)

    if _RE_CERRAR.match(texto_orden):
        # Sin objetivo: main lo fija con la última ventana ajena (§10.21); si no puede, la compuerta deniega.
        return _accion("cerrar_ventana", {}, "cerrar la ventana", tecnica)

    m = _RE_MEDIA.match(texto_orden)
    if m:
        orden, descripcion = _ORDEN_MEDIA[m.group("orden")]
        return _accion("media", {"orden": orden}, descripcion, tecnica)

    m = _RE_VOLUMEN.search(texto)
    if m:
        nivel = int(m.group("n"))
        return _accion("volumen", {"nivel": nivel}, f"poner el volumen al {nivel} %", tecnica)

    # v0.2.1: "abre <nombre de varias palabras>" → app del menú Inicio (Windows; R18 del diseño v0.3).
    if apps_locales:
        local = _clasificar_app_local(orden_orig, orden_norm, destinos, tecnica)
        if local is not None:
            return local

    m = _RE_MEMORIA.search(texto)
    if m:
        consulta = _limpiar_fragmento(_original_desde(originales, normalizadas, m.end()).rstrip("?¿!¡."))
        return Intencion("memoria_consulta", {"consulta": consulta or original.strip()},
                         explicacion_tecnica=tecnica)

    return Intencion("ninguna", {}, explicacion_tecnica=tecnica)


def enrutar(texto: str, *, ahora: datetime, apps: Mapping[str, str] | None = None,
            canal: str = "terminal", destinos: Mapping[str, str] | None = None,
            plataforma: str | None = None, apps_locales: bool = False,
            telegram_acciones: bool = False) -> Intencion:
    """Decide la intención de ``texto`` con reglas locales y aplica las capacidades del canal.

    - Las reglas se evalúan sobre ``normalizar(texto)`` en el orden de §10.12; los argumentos
      que citan al usuario (consulta, contenido, mensaje, texto a escribir) salen del texto
      **original**.
    - ``explicacion_tecnica`` es ``True`` si el texto pide una explicación y tiene más de 8 palabras.
    - ``destinos`` (v0.2): ``Settings.destinos_permitidos`` (sinónimo → clave de ``DESTINOS_BASE``);
      ``None`` = solo las claves del catálogo (más los sinónimos de serie). ``plataforma``
      (``sys.platform``): los alias de ``RAMS_APPS`` solo dan ``lanzar_app`` en Linux; ``None`` no
      restringe.
    - ``apps_locales`` (v0.2.1): activa "abre <app del menú Inicio>" (``lanzar_app`` con
      ``local=True``). Por defecto no; main lo activa en Windows con ``Settings.apps_menu_inicio``.
    - ``telegram_acciones`` (v0.2.1, ``RAMS_TELEGRAM_ACCIONES``): por Telegram admite las acciones
      de ``ACCIONES_TELEGRAM`` y les añade ``args["canal"] = "telegram"`` (la compuerta remata).
    - Si ``canal`` no es local y la intención no está permitida allí, devuelve
      ``Intencion("bloqueada_canal", {"tipo_original": t, "comando": c | None})``. Un canal
      desconocido se trata como ``nexus``. ``escritorio`` es local (hereda lo de la terminal).

    Nunca lanza: ante un error inesperado devuelve ``ninguna`` (bloqueada si el canal no la admite).
    """
    original = str(texto or "").strip()
    originales, normalizadas = _palabras(original)
    texto_norm = " ".join(normalizadas)
    tecnica = bool(_RE_TECNICA.search(texto_norm)) and len(normalizadas) > 8
    try:
        intencion = _clasificar(original, originales, normalizadas, ahora, apps or {}, tecnica,
                                destinos=destinos, plataforma=plataforma, canal=canal,
                                apps_locales=bool(apps_locales))
    except Exception as exc:  # defensa: el enrutador nunca tumba un turno
        log.warning("Error inesperado al enrutar (%s); uso la intención 'ninguna'", type(exc).__name__)
        intencion = Intencion("ninguna", {}, explicacion_tecnica=tecnica)
    if canal not in CANALES_LOCALES:
        if not permitida_en_canal(intencion, canal, telegram_acciones=telegram_acciones):
            comando = intencion.args.get("comando") if intencion.tipo == "comando" else None
            intencion = Intencion("bloqueada_canal", {"tipo_original": intencion.tipo, "comando": comando},
                                  explicacion_tecnica=tecnica)
        elif intencion.tipo == "accion_pc":
            intencion = _con_canal(intencion, canal)
    log.debug("Intención %s (canal %s, %d palabras)", intencion.tipo, canal, len(normalizadas))
    return intencion


def _con_canal(intencion: Intencion, canal: str) -> Intencion:
    """La misma ``accion_pc`` con ``args["canal"]`` en su ``AccionPC`` (la compuerta lo lee)."""
    accion = intencion.args.get("accion")
    if not isinstance(accion, AccionPC):
        return intencion
    nueva = AccionPC(accion.tipo, {**accion.args, "canal": canal}, accion.descripcion, origen=accion.origen)
    return Intencion("accion_pc", {**intencion.args, "accion": nueva}, intencion.confianza,
                     intencion.explicacion_tecnica)


def _accion_remota_permitida(intencion: Intencion) -> bool:
    """Acción de bajo riesgo pedible por Telegram (``ACCIONES_TELEGRAM``), sin pasos locales detrás."""
    accion = intencion.args.get("accion")
    if not isinstance(accion, AccionPC) or accion.origen != "usuario":
        return False
    if accion.tipo not in ACCIONES_TELEGRAM:
        return False
    # "abre whatsapp y escríbele…" abre y luego pide teclear ("escribe ahí"): eso es solo local.
    return not any(clave in intencion.args for clave in ("nota", "destino", "sin_alias"))


def permitida_en_canal(intencion: Intencion, canal: str, *, telegram_acciones: bool = False) -> bool:
    """``True`` si ``intencion`` se puede atender desde ``canal`` (misma regla que ``enrutar``).

    ``terminal`` (canal local) lo puede todo; un canal desconocido se trata como ``nexus``. Con
    ``telegram_acciones`` (v0.2.1), Telegram admite además las acciones de ``ACCIONES_TELEGRAM``.
    v0.2.2: las intenciones con operaciones (``google``, ``correo``, ``drive``) solo pasan si su
    ``args["op"]`` está en ``OPS_POR_CANAL`` (p. ej. por Telegram solo el estado de Google).
    """
    if canal in CANALES_LOCALES:
        return True
    clave = canal if canal in INTENCIONES_POR_CANAL else "nexus"
    if clave == "telegram" and telegram_acciones and intencion.tipo == "accion_pc":
        return _accion_remota_permitida(intencion)
    if intencion.tipo not in INTENCIONES_POR_CANAL[clave]:
        return False
    if intencion.tipo == "comando":
        return str(intencion.args.get("comando", "")) in COMANDOS_POR_CANAL[clave]
    ops = OPS_POR_CANAL.get(clave, {}).get(intencion.tipo)
    if ops is not None:
        return intencion.args.get("op") in ops
    return True


# ---------------------------------------------------------------------------
# v0.2.2: oferta del taller (§4.4, funciones puras y sin E/S)
# ---------------------------------------------------------------------------

_PREFIJO_TAREA: Final = (
    r"(?:(?:oye|oiga|hey|ey|hola|bueno|vale|ok|okay|ya|y|e|entonces|luego|despues|ahora|a ver|amadeus|kurisu"
    r"|christina|cristina|kristina|makise|asistente|assistant|por favor|porfavor|porfa|porfas|porfis|porfi|pls"
    r"|plis|plz|please|xfa|xfavor|xfis)\s+)*")
_PETICION_TAREA: Final = (
    r"(?:(?:me |nos )?(?:puedes|podrias|podras|pudieras|podes|quieres|quisieras|vas a|sabes)\s+(?:me\s+)?"
    r"|(?:necesito|quiero|quisiera) que (?:me )?|(?:hazme|me haces) (?:el|un) favor de |me |ayudame a )?")
_CLITICOS: Final = r"(?:me|te|lo|la|los|las|le|les|melo|mela|melos|melas|selo|sela|selos|selas|nos)?"
# Verbos de tarea en -ar: imperativo (-a) e infinitivo (-ar), con clíticos, y el subjuntivo de «quiero que …».
_VERBOS_TAREA_AR: Final = ("renombr", "organiz", "orden", "copi", "borr", "elimin", "limpi", "cre", "gener",
                           "list", "calcul", "cambi", "configur", "instal", "desinstal", "apag", "reinici",
                           "automatiz", "descarg", "respald", "sincroniz", "redimension", "recort", "junt",
                           "separ")
_SUBJUNTIVOS_TAREA: Final = ("renombres", "organices", "ordenes", "copies", "borres", "elimines", "limpies",
                             "crees", "generes", "listes", "calcules", "cambies", "configures", "instales",
                             "desinstales", "apagues", "reinicies", "automatices", "descargues", "respaldes",
                             "sincronices", "redimensiones", "recortes", "juntes", "separes", "cuentes",
                             "muevas", "conviertas", "comprimas", "descomprimas", "extraigas", "suspendas",
                             "unas", "dividas", "hagas")
_VERBO_TAREA: Final = (
    rf"(?:(?:{'|'.join(_VERBOS_TAREA_AR)})(?:a|ar){_CLITICOS}"
    rf"|(?:cuenta|contar){_CLITICOS}"
    rf"|(?:muev|conviert|comprim|descomprim|extra|suspend|un|divid)e{_CLITICOS}"
    rf"|(?:mover|convertir|comprimir|descomprimir|extraer|suspender|unir|dividir){_CLITICOS}"
    rf"|haz{_CLITICOS}|hacer{_CLITICOS}"
    rf"|{'|'.join(_SUBJUNTIVOS_TAREA)})")
_RE_TAREA_PC: Final = re.compile(rf"^{_PREFIJO_TAREA}{_PETICION_TAREA}{_VERBO_TAREA}\b")
_RE_OBJETO_PC: Final = re.compile(
    r"\b(?:archivos?|carpetas?|fotos?|fotografias?|imagen|imagenes|pdfs?|documentos?|descargas|escritorio|disco"
    r"|programas?|apps?|aplicacion|aplicaciones|pantalla|fondo|wifi|bluetooth|brillo|resolucion|procesos?"
    r"|servicios?|ventanas?|zip|rar|mp3|mp4|videos?|musica|excel|csv|txt|word|iconos?|accesos?\s+directos?"
    r"|papelera|temporales|cache)\b")
# Horarios y fechas: «¿a qué hora abre el banco?» habla de un local, no del PC (como broca._pide_accion).
_RE_HORARIO_TAREA: Final = re.compile(
    r"\b(?:a|hasta|desde) que hora\b|\bhorarios?\b|\b(?:a|hasta|desde) las \d|\bhoy\b|\bmanana\b"
    r"|\b(?:los|el) (?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?|festivos|feriados)\b"
    r"|\bfines? de semana\b")
_RE_CORTE_CLAUSULA_TAREA: Final = re.compile(r"[.!?;:,\n]+")

_ACEPTA_FUERTES: Final = frozenset({"si", "dale", "hazlo", "ok", "okay", "va", "listo", "hagale", "escribelo",
                                    "intentalo", "claro", "adelante", "procede", "vale", "de una"})
_ACEPTA_SUAVES: Final = frozenset({"por favor", "porfa", "porfavor", "porfis", "pls", "plis", "please", "bueno",
                                   "amadeus", "kurisu", "asistente", "entonces", "ya", "pues", "pruebalo",
                                   "pruebe", "con un script", "con el script", "con script"})
_RE_ACEPTA_TOKEN: Final = re.compile(
    r"de una|por favor|con (?:un |el )?script|[a-z]+")


def pide_tarea_pc(texto: str) -> bool:
    """True si alguna cláusula (texto normalizado, tras vocativos y cortesías) empieza por un verbo de tarea
    sobre el PC y nombra un objeto del PC.

    Verbos: renombra, organiza, ordena, mueve, copia, convierte, comprime, descomprime, extrae, borra,
    elimina, limpia, crea, genera, cuenta, lista, calcula, cambia, configura, instala, desinstala, apaga,
    reinicia, suspende, automatiza, descarga, respalda, sincroniza, redimensiona, recorta, une, junta,
    separa, divide (y sus formas -ame/-alos/-me, "hazme", peticiones "¿puedes …?", "quiero que …").
    Objetos: archivo(s), carpeta(s), foto(s), imagen(es), pdf(s), documento(s), descargas, escritorio, disco,
    programa(s), app(s), pantalla, fondo, wifi, bluetooth, brillo, resolucion, proceso(s), servicio(s),
    ventana(s), zip, rar, mp3, mp4, video(s), musica, excel, csv, txt, word, icono(s), acceso(s) directo(s),
    papelera, temporales, cache. No cuenta si habla de horarios (como broca._pide_accion).
    "organiza tus ideas" → False; "organiza mis descargas por tipo" → True. Nunca lanza.
    """
    try:
        for clausula in _RE_CORTE_CLAUSULA_TAREA.split(normalizar(texto)):
            limpia = re.sub(r"^[^a-z0-9]+", "", clausula).strip()
            if not limpia or not _RE_TAREA_PC.match(limpia):
                continue
            if _RE_OBJETO_PC.search(limpia) and not _RE_HORARIO_TAREA.search(limpia):
                return True
    except Exception as exc:  # defensa: nunca tumba un turno
        log.debug("pide_tarea_pc falló (%s)", type(exc).__name__)
    return False


def acepta_oferta(texto: str) -> bool:
    """True si el mensaje COMPLETO es una aceptación corta: si | sí | dale | hazlo | ok | okay | va | listo |
    de una | hagale | hágale | escribelo | escríbelo | intentalo | inténtalo | por favor, con "con un/el script"
    opcional y puntuación final. "sí, pero mañana" → False. Nunca lanza."""
    try:
        limpio = re.sub(r"[.,;:!?¡¿…\"'«»()\-]+", " ", normalizar(texto))
        limpio = _RE_ESPACIOS.sub(" ", limpio).strip()
        if not limpio or len(limpio) > 80:
            return False
        tokens: list[str] = []
        posicion = 0
        for m in _RE_ACEPTA_TOKEN.finditer(limpio):
            if limpio[posicion:m.start()].strip():
                return False                      # algo que no es una palabra (números, símbolos)
            tokens.append(_RE_ESPACIOS.sub(" ", m.group()))
            posicion = m.end()
        if limpio[posicion:].strip() or not tokens or len(tokens) > 8:
            return False
        if any(t not in _ACEPTA_FUERTES and t not in _ACEPTA_SUAVES for t in tokens):
            return False
        return any(t in _ACEPTA_FUERTES for t in tokens)
    except Exception as exc:  # defensa: nunca tumba un turno
        log.debug("acepta_oferta falló (%s)", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Eventos sociales (§4.4.4)
# ---------------------------------------------------------------------------

_RE_TRISTEZA: Final = re.compile(
    r"\b(estoy (muy )?(triste|mal|deprimid[oa]|agotad[oa] de todo)|me siento (mal|sol[oa]|triste|vacio)"
    r"|quiero llorar|fallecio|murio)\b")
_APODOS: Final = (
    ("christina", re.compile(r"\b(christina|cristina|kristina)\b")),
    ("asistente", re.compile(r"\basistente\b")),
    ("zombi", re.compile(r"\bzombie?\b")),
    ("celeb17", re.compile(r"\bceleb\s*(17|seventeen)\b")),
    ("chuunibyou", re.compile(r"\b(hou?oin\s+kyou?ma|kyou?ma|cientifico loco|mad scientist|la organizacion)\b")),
)
_RE_ACUSACION: Final = re.compile(r"\btsundere\b")
_RE_CUMPLIDO: Final = re.compile(
    r"\b(eres (muy |tan )?(inteligente|lista|genial|brillante|increible|la mejor)|que lista|buen trabajo"
    r"|bien hecho|te luciste)\b")
_RE_AGRADECIMIENTO: Final = re.compile(r"\b(gracias|mil gracias|te lo agradezco)\b")
_RE_DESPEDIDA: Final = re.compile(
    r"\b(adios|chao|nos vemos|hasta (manana|luego|pronto)|me voy a dormir|me despido)\b")
_RE_SALUDO: Final = re.compile(r"^(hola|buenas|buenos dias|buenas tardes|buenas noches|quiubo|hey)\b")


def detectar_evento_social(texto: str) -> str | None:
    """Evento social del texto (``"tipo"`` o ``"tipo:detalle"``) o ``None``.

    Prioridad: tristeza > apodo > acusacion_tsundere > cumplido > agradecimiento >
    despedida > saludo. ``regreso`` y ``error_propio`` los fija main.py, no este detector.
    """
    norm = normalizar(texto)
    if not norm:
        return None
    if _RE_TRISTEZA.search(norm):
        return "tristeza"
    for detalle, patron in _APODOS:
        if patron.search(norm):
            return f"apodo:{detalle}"
    if _RE_ACUSACION.search(norm):
        return "acusacion_tsundere"
    if _RE_CUMPLIDO.search(norm):
        return "cumplido"
    if _RE_AGRADECIMIENTO.search(norm):
        return "agradecimiento"
    if _RE_DESPEDIDA.search(norm):
        return "despedida"
    if _RE_SALUDO.search(norm):
        return "saludo"
    return None
