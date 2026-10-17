"""Enrutador local de intenciones y detector de eventos sociales de R.A.M.S. BIO-CORE (§10.12).

Qué hace (sin LLM, núcleo del principio "No-LLM centric", §8):
- ``enrutar`` decide con reglas y expresiones regulares qué herramienta o acción local
  corresponde a un texto del usuario (buscar en la web, recordar, programar, reporte del
  sistema, vistazo de cámara, acciones sobre el PC…). Las ``AccionPC`` nacen SOLO aquí, a
  partir del texto del usuario, siempre con ``origen="usuario"`` (§9.1).
- Aplica las **capacidades por canal**: desde Telegram o Nexus no se puede mirar, escuchar ni
  actuar sobre el PC; esas intenciones salen como ``bloqueada_canal`` (§9.3).
- ``parsear_cuando`` interpreta expresiones de tiempo en español ("a las 6 pm", "en 20
  minutos", "mañana a las 8", "el 2026-12-24") y ``detectar_evento_social`` reconoce apodos,
  cumplidos, saludos, despedidas, tristeza… (§4.4.4).

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

__all__ = [
    "Intencion",
    "TIPOS_INTENCION",
    "COMANDOS",
    "INTENCIONES_POR_CANAL",
    "COMANDOS_POR_CANAL",
    "MENSAJE_SOLO_LOCAL",
    "normalizar",
    "parsear_comando",
    "parsear_cuando",
    "enrutar",
    "permitida_en_canal",
    "detectar_evento_social",
    "completar_programar",
    "quiere_cancelar",
]

log = logging.getLogger("rams.intenciones")

# ---------------------------------------------------------------------------
# Tipos y constantes (§10.12)
# ---------------------------------------------------------------------------

TIPOS_INTENCION: Final = (
    "ninguna", "comando", "buscar_web", "reporte_sistema", "recordar", "programar",
    "programar_incompleto", "mirar_pantalla", "vistazo", "accion_pc", "memoria_consulta",
    "bloqueada_canal",
)


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
                   "/devolver_llamada", "/hud", "/logro")

# Capacidades por canal de entrada (normativo). "terminal" puede todo.
INTENCIONES_POR_CANAL: Final[dict[str, frozenset[str]]] = {
    "telegram": frozenset({"ninguna", "comando", "buscar_web", "recordar", "programar", "programar_incompleto",
                           "memoria_consulta", "reporte_sistema"}),
    "nexus": frozenset({"ninguna"}),
}
COMANDOS_POR_CANAL: Final[dict[str, frozenset[str]]] = {
    "telegram": frozenset({"/ayuda", "/estado", "/divergencia", "/memoria", "/recordar", "/buscar", "/agenda",
                           "/reporte", "/bitacora", "/colgar", "/devolver_llamada"}),
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
    r"^(busca|buscar|investiga|averigua|googlea|consulta en (internet|la red))\b[:,]?\s*(?P<q>.+)")
_RE_BUSCAR_DEBIL: Final = re.compile(
    r"\b(ultimas noticias|que paso con|precio (actual )?de|cuando (sale|salio)|version (estable )?de)\b")
_RE_REPORTE: Final = re.compile(
    r"\b(reporte|estado del (sistema|pc|computador)|como esta (el|mi) (pc|computador|sistema)|temperatura"
    r"|uso de (cpu|ram|memoria))\b")
_RE_PANTALLA: Final = re.compile(r"\b(mira|analiza|revisa|que hay en|que ves en)\s+(mi\s+)?pantalla\b")
_RE_VISTAZO: Final = re.compile(r"\b(me ves|mirame|puedes verme)\b|estoy aqui\?")
_RE_LANZAR: Final = re.compile(r"^(abre|lanza|abrir|ejecuta)\s+(el |la )?(?P<app>[a-z0-9_]+)[.!]*$")
_RE_WORKSPACE: Final = re.compile(
    r"\b(ve|cambia|pasa|llevame)\s+al?\s+(espacio|workspace|escritorio)\s+(?P<n>\d{1,2})\b")
_RE_NEXUS: Final = re.compile(r"\b(enfoca|trae|muestrame)\s+(nexus|el ide)\b")
_RE_CERRAR: Final = re.compile(r"^cierra\s+(esta |la )?ventana\b")
_RE_MEDIA: Final = re.compile(
    r"^(?P<orden>pausa|pon pausa|reanuda|siguiente cancion|cancion anterior)( (la )?(musica|cancion))?[.!]*$")
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


def _accion(tipo: str, args: Mapping[str, Any], descripcion: str, tecnica: bool) -> Intencion:
    """Intención ``accion_pc`` con su ``AccionPC`` (siempre ``origen="usuario"``)."""
    return Intencion("accion_pc", {"accion": AccionPC(tipo, dict(args), descripcion, origen="usuario")},
                     explicacion_tecnica=tecnica)


def _clasificar(original: str, originales: list[str], normalizadas: list[str], ahora: datetime,
                apps: Mapping[str, str], tecnica: bool) -> Intencion:
    """Aplica la tabla de reglas de §10.12 en orden; gana la primera."""
    texto = " ".join(normalizadas)

    comando = parsear_comando(original)
    if comando is not None:
        return Intencion("comando", {"comando": comando[0], "resto": comando[1]}, explicacion_tecnica=tecnica)

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

    m = _RE_BUSCAR.match(texto)
    if m:
        consulta = _limpiar_fragmento(_original_desde(originales, normalizadas, m.start("q")))
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

    m = _RE_LANZAR.match(texto)
    if m and m.group("app") in apps:
        app = m.group("app")
        return _accion("lanzar_app", {"alias": app}, f"abrir {app}", tecnica)

    m = _RE_WORKSPACE.search(texto)
    if m:
        n = int(m.group("n"))
        return _accion("ir_workspace", {"n": n}, f"cambiar al espacio {n}", tecnica)

    if _RE_NEXUS.search(texto):
        return _accion("enfocar_nexus", {}, "traer Nexus IDE al frente", tecnica)

    if _RE_CERRAR.match(texto):
        # Sin objetivo: main lo fija con la última ventana ajena (§10.21); si no puede, la compuerta deniega.
        return _accion("cerrar_ventana", {}, "cerrar la ventana", tecnica)

    m = _RE_MEDIA.match(texto)
    if m:
        orden, descripcion = _ORDEN_MEDIA[m.group("orden")]
        return _accion("media", {"orden": orden}, descripcion, tecnica)

    m = _RE_VOLUMEN.search(texto)
    if m:
        nivel = int(m.group("n"))
        return _accion("volumen", {"nivel": nivel}, f"poner el volumen al {nivel} %", tecnica)

    m = _RE_MEMORIA.search(texto)
    if m:
        consulta = _limpiar_fragmento(_original_desde(originales, normalizadas, m.end()).rstrip("?¿!¡."))
        return Intencion("memoria_consulta", {"consulta": consulta or original.strip()},
                         explicacion_tecnica=tecnica)

    return Intencion("ninguna", {}, explicacion_tecnica=tecnica)


def enrutar(texto: str, *, ahora: datetime, apps: Mapping[str, str] | None = None,
            canal: str = "terminal") -> Intencion:
    """Decide la intención de ``texto`` con reglas locales y aplica las capacidades del canal.

    - Las reglas se evalúan sobre ``normalizar(texto)`` en el orden de §10.12; los argumentos
      que citan al usuario (consulta, contenido, mensaje) salen del texto **original**.
    - ``explicacion_tecnica`` es ``True`` si el texto pide una explicación y tiene más de 8 palabras.
    - Si ``canal`` no es local y la intención no está permitida allí, devuelve
      ``Intencion("bloqueada_canal", {"tipo_original": t, "comando": c | None})``. Un canal
      desconocido se trata como ``nexus``.

    Nunca lanza: ante un error inesperado devuelve ``ninguna`` (bloqueada si el canal no la admite).
    """
    original = str(texto or "").strip()
    originales, normalizadas = _palabras(original)
    texto_norm = " ".join(normalizadas)
    tecnica = bool(_RE_TECNICA.search(texto_norm)) and len(normalizadas) > 8
    try:
        intencion = _clasificar(original, originales, normalizadas, ahora, apps or {}, tecnica)
    except Exception as exc:  # defensa: el enrutador nunca tumba un turno
        log.warning("Error inesperado al enrutar (%s); uso la intención 'ninguna'", type(exc).__name__)
        intencion = Intencion("ninguna", {}, explicacion_tecnica=tecnica)
    if canal not in CANALES_LOCALES and not permitida_en_canal(intencion, canal):
        comando = intencion.args.get("comando") if intencion.tipo == "comando" else None
        intencion = Intencion("bloqueada_canal", {"tipo_original": intencion.tipo, "comando": comando},
                              explicacion_tecnica=tecnica)
    log.debug("Intención %s (canal %s, %d palabras)", intencion.tipo, canal, len(normalizadas))
    return intencion


def permitida_en_canal(intencion: Intencion, canal: str) -> bool:
    """``True`` si ``intencion`` se puede atender desde ``canal`` (misma regla que ``enrutar``).

    ``terminal`` (canal local) lo puede todo; un canal desconocido se trata como ``nexus``.
    """
    if canal in CANALES_LOCALES:
        return True
    clave = canal if canal in INTENCIONES_POR_CANAL else "nexus"
    if intencion.tipo not in INTENCIONES_POR_CANAL[clave]:
        return False
    if intencion.tipo == "comando":
        return str(intencion.args.get("comando", "")) in COMANDOS_POR_CANAL[clave]
    return True


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
