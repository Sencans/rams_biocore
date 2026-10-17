"""Ingestor de chats exportados de WhatsApp → ``perfil_conductual.json`` (§10.9).

Qué hace: parsea exportaciones ``.txt`` de WhatsApp (Android es-CO e iOS, con sus espacios
invisibles y el AM/PM "a. m."/"p. m."), agrupa ráfagas y extrae **solo patrones propios** del
usuario: estilo, muletillas (n-gramas con *lift* frente a la línea base ajena), horario, temas,
saludos y despedidas. El resultado es el **recuerdo heredado** de Amadeus: conoce al usuario
por datos, no por haberlo vivido (como Amadeus Kurisu con Okabe).

Privacidad (§9.6): todo se procesa en local. El perfil **no** guarda mensajes, nombres de
archivos, nombres de participantes (tampoco el del propio usuario), nombres de personas
mencionadas, números de 7 o más dígitos, correos ni URLs. Los mensajes ajenos solo sirven como
línea base de frecuencias en memoria y luego se descartan. El chat crudo nunca va al LLM.

Uso (CLI)::

    python -m core.wsp_ingestor chats/*.txt --yo "Tu Nombre" [--salida RUTA] [--gap 120]
                                [--orden dmy|mdy] [--excluir "Pedro,Ana"]

Dependencias: solo la biblioteca estándar. ``core.config`` se importa únicamente dentro de
:func:`main` (para la ruta por defecto del perfil).

Degradación: las líneas ilegibles se saltan con un contador; los archivos que no son UTF-8 se
reintentan con ``utf-8-sig`` y luego con ``latin-1``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "MensajeWsp",
    "ANDROID_RE",
    "IOS_RE",
    "STOPWORDS_ES",
    "NOMBRES_COMUNES_ES",
    "normalizar_linea",
    "parsear_lineas",
    "parsear_archivo",
    "detectar_autores",
    "agrupar_rafagas",
    "extraer_muletillas",
    "detectar_nombres_propios",
    "construir_perfil",
    "guardar_perfil",
    "cargar_perfil",
    "resumen_perfil",
    "main",
]

log = logging.getLogger("rams.wsp")

VERSION_PERFIL: Final = 1

# ---------------------------------------------------------------------------
# Expresiones regulares (§10.9, exactas)
# ---------------------------------------------------------------------------

ANDROID_RE: Final = re.compile(
    r"^(?P<fecha>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<hora>\d{1,2}:\d{2}(?::\d{2})?)\s*"
    r"(?P<ampm>[ap]\.?\s?m\.?)?\s+-\s+(?P<resto>.*)$", re.I)
IOS_RE: Final = re.compile(
    r"^\[(?P<fecha>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<hora>\d{1,2}:\d{2}(?::\d{2})?)\s*"
    r"(?P<ampm>[ap]\.?\s?m\.?)?\]\s+(?P<resto>.*)$", re.I)

_RE_INVISIBLES: Final = re.compile("[‎‏‪-‮﻿]")
_RE_EDITADO: Final = re.compile(r"\s*<\s*(?:se edit[óo] este mensaje|this message was edited)\.?\s*>\s*$", re.I)
_RE_TOKEN: Final = re.compile(r"[a-záéíóúñü]+")
_RE_PALABRA: Final = re.compile(r"[^\W\d_]+")            # palabra con su forma original (mayúsculas)
_RE_NOMBRE_AUTOR: Final = re.compile(r"[^\W_]+")          # tokens (letras o dígitos) de un nombre
_RE_MENCION: Final = re.compile(r"@(\w+)")
_RE_ESPACIOS: Final = re.compile(r"\s+")
_RE_DIGITOS_LARGOS: Final = re.compile(r"\d{7,}")
_RE_CORREO: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_URL: Final = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
# Limpieza previa a la tokenización (§9.6): de una URL o un correo no deben quedar palabras sueltas.
_RE_ESQUEMA_URL: Final = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S*")
_RE_ARROBA: Final = re.compile(r"\S*@\S*")                 # correos, menciones y handles con @
_RE_DOMINIO: Final = re.compile(
    r"(?i)\b(?:[\w-]+\.)+(?:com|co|net|org|io|me|edu|gov|info|biz|app|dev|ly|gl|gg|be|tv|ai|xyz|"
    r"us|uk|es|mx|ar|cl|pe|ec|ve|br)\b(?:[/?#:]\S*)?")
_RE_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RE_EMOJI: Final = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF\U0001F900-\U0001F9FF"
    "\U0001FA70-\U0001FAFF☀-⛿✀-➿⭐⭕‼⁉]")
_MODIFICADORES_EMOJI: Final = frozenset(chr(c) for c in range(0x1F3FB, 0x1F400))

# Risas (§10.9: ja(ja)+ → jaja, je(je)+ → jeje, (js)+ → jsjs, x+d+ → xd); se tolera una j final.
_RISAS_RE: Final = (
    (re.compile(r"ja(?:ja)+j?"), "jaja"),
    (re.compile(r"je(?:je)+j?"), "jeje"),
    (re.compile(r"(?:js)+j?"), "jsjs"),
    (re.compile(r"x+d+"), "xd"),
)
RISAS: Final = ("jaja", "jeje", "jsjs", "xd")

MARCADORES_MULTIMEDIA: Final = (
    "<multimedia omitido>", "<media omitted>", "imagen omitida", "audio omitido", "video omitido",
    "sticker omitido", "gif omitido", "documento omitido", "se eliminó este mensaje",
    "eliminaste este mensaje", "this message was deleted",
)

# Avisos del sistema que iOS exporta con el nombre del contacto como "autor".
_TEXTOS_SISTEMA: Final = (
    "los mensajes y las llamadas están cifrados de extremo a extremo",
    "los mensajes y llamadas están cifrados de extremo a extremo",
    "messages and calls are end-to-end encrypted",
    "cambió tu código de seguridad",
    "your security code with",
)

SALUDOS_BASE: Final = ("hola", "buenas", "quiubo", "qué más", "hey", "buenos días", "buenas tardes",
                       "buenas noches")
DESPEDIDAS_BASE: Final = ("chao", "listo", "nos vemos", "bueno", "bye", "hablamos", "buenas noches")

# Palabras de relleno conversacional que nunca son "temas" (saludos, despedidas, asentimientos).
_NO_TEMAS: Final[frozenset[str]] = frozenset(
    {"gracias", "dale", "claro", "vale", "okay", "bueno", "buena", "buenas", "buenos", "listo", "lista",
     "perfecto", "genial", "parce", "parcero", "hola", "chao", "quiubo", "hablamos", "vemos", "tranqui"})

LATENCIA_MAX_S: Final = 6 * 3600
MIN_LIFT: Final = 1.5
MIN_FREQ_TEMA: Final = 2
MAX_TEMAS: Final = 15

# ---------------------------------------------------------------------------
# Vocabulario (normalizado: minúsculas y sin tildes)
# ---------------------------------------------------------------------------

STOPWORDS_ES: Final[frozenset[str]] = frozenset("""
a aca ahi ahora al algo alguien algun alguna algunas alguno algunos alla alli ante antes aqui asi aun
aunque bien cada casi como con contra cual cuales cualquier cuando cuanto de del desde despues donde
dos durante e el ella ellas ello ellos en entonces entre era eramos eran eras eres es esa esas ese eso
esos esta estaba estaban estado estamos estan estar estas este esto estos estoy estuve fue fueron fui
ha habia han has hasta hay he hemos hice hizo hacer hace igual la las le les lo los luego mas me menos
mi mia mias mio mios mis mismo misma mucho muchos mucha muchas muy nada ni no nos nosotros nuestra
nuestro nunca o os otra otras otro otros para pero poco por porque pues puede pueden puedo que quien
quienes se sea segun ser si sido siempre sin sino sobre solo son soy su sus suya suyo tal tambien tan
tanto te tenemos tener tengo tenia ti tiene tienen toda todas todo todos tu tus tuyo u un una unas uno
unos usted ustedes va vamos van vas ver vez veces voy vos y ya yo dice dijo decir cosa cosas hoy ayer
manana dia dias pa pal xq pq porq tb tmb ok oki sea ese esa mas aja eh
""".split())

INTERJECCIONES: Final[frozenset[str]] = frozenset("""
ay ayy uy uyy ah oh eh uf uff ufff ups wow ole aja mmm hmm uish ush upa epa hey bah puf ojala jum
""".split())

# n-gramas formados solo por stopwords que en es-CO sí funcionan como muletillas.
EXPRESIONES_COLOQUIALES: Final[frozenset[str]] = frozenset({
    "de una", "que mas", "no mas", "nada mas", "para nada", "por eso", "asi es", "eso si", "ya que",
    "de una vez", "con todo", "si o que", "si o no",
})

NOMBRES_COMUNES_ES: Final[frozenset[str]] = frozenset("""
juan jose luis carlos jorge andres diego david daniel alejandro santiago sebastian felipe camilo julian
mateo nicolas samuel gabriel miguel angel pedro pablo manuel francisco fernando ricardo eduardo javier
sergio oscar mauricio hernan hector jaime alberto alvaro antonio arturo cesar cristian christian edwin
esteban fabian gustavo guillermo harold ivan jairo jhon john johan jonathan kevin leonardo mario martin
nelson orlando rafael ramiro raul roberto rodrigo ruben victor william wilson yeison brayan bryan
emilio enrique ernesto federico gerardo german gonzalo hugo ignacio joaquin lorenzo marco marcos nestor
omar rene rigoberto rolando saul ulises wilmer alexander alex alonso armando benjamin bernardo dario
elkin emanuel ezequiel fredy freddy gilberto horacio isaac jesus jhonatan leandro lucas matias
maximiliano agustin alfredo anderson duvan efrain elias emerson faber fabio franco gerson giovanni
hamilton humberto jaider jefferson jhonny jimmy jonatan josue julio leonel luciano mariano mateus
milton moises nelson norberto octavio orlando osvaldo patricio reinaldo rogelio rosendo silvio
teodoro valentin wilmar yesid yovanny maria ana laura andrea carolina paola natalia daniela valentina
camila sofia isabella mariana gabriela juliana alejandra catalina diana sandra claudia patricia monica
adriana angela lina luisa paula juana sara salome manuela luciana antonella martina emilia victoria
valeria ximena jimena tatiana viviana yesenia yolanda gloria marta martha rosa luz carmen teresa beatriz
lucia elena isabel margarita esperanza blanca cecilia clara dora elsa fanny gladys ines irene johana
jenny jennifer jessica karen katherine kelly leidy lorena lucero marcela maritza milena nancy nora
olga pilar rocio ruth silvia sonia stella susana veronica yaneth yuliana yurany zulma alba amparo
consuelo dolores edith estefania fernanda florencia gisela ingrid ivonne jacqueline josefina julieta
karina liliana mercedes mileidy nathalia noemi pamela raquel rebeca regina renata rosario sabrina
samantha tania vanessa wendy yamile yenny zoe abril agustina emma eva alicia amelia aurora carla celeste
cristina elisa estela eugenia fatima guadalupe helena hilda ivana jazmin lidia lilia mabel magdalena
marisol matilde miriam nataly olivia rosalba selena soledad tamara vilma yadira angelica antonia
berta brenda dayana deisy erika francy gina heidy jhoana kimberly leydi lizeth luzmila maribel
marleny nubia piedad rubiela shirley stefany yeimy yudy
""".split())


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MensajeWsp:
    """Un mensaje del chat exportado."""

    ts: datetime          # naive, hora local del export
    autor: str            # "" en mensajes de sistema
    texto: str
    multimedia: bool = False
    sistema: bool = False


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _sin_tildes(texto: str) -> str:
    """Minúsculas y sin tildes (NFD sin marcas ``Mn``)."""
    descompuesto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


def _normalizar_risa(token: str) -> str:
    for patron, canonico in _RISAS_RE:
        if patron.fullmatch(token):
            return canonico
    return token


def _texto_tokenizable(texto: str) -> str:
    """El texto sin URLs, dominios sueltos, correos, menciones ni números largos (cada uno → espacio).

    Se aplica **antes** de tokenizar (§9.6): si no, de ``zuluaga.restrepo@empresa.co`` o de
    ``https://www.instagram.com/usuario`` quedarían palabras sueltas ("restrepo", "instagram")
    que llegarían a muletillas y temas, donde ya no se reconocen como correo o URL.
    """
    for patron in (_RE_ESQUEMA_URL, _RE_URL, _RE_ARROBA, _RE_DOMINIO, _RE_DIGITOS_LARGOS):
        texto = patron.sub(" ", texto)
    return texto


def _tokens(texto: str) -> list[str]:
    """Tokens ``[a-záéíóúñü]+`` en minúsculas (sin URLs, correos ni menciones), con las risas normalizadas."""
    return [_normalizar_risa(t) for t in _RE_TOKEN.findall(_texto_tokenizable(texto).lower())]


def _decimal(valor: float, decimales: int = 1) -> str:
    """Coma decimal para el usuario (§3.5), sin depender de ``core``."""
    texto = f"{valor:.{decimales}f}"
    if texto.startswith("-") and float(texto) == 0.0:
        texto = texto[1:]
    return texto.replace(".", ",")


def _real(valor: Any) -> float | None:
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _es_texto_seguro(texto: str, excluir: frozenset[str] | set[str]) -> bool:
    """``False`` si el texto contiene un token excluido, dígitos largos, correos, URLs o ``@``.

    Es solo una segunda barrera sobre el n-grama ya formado: las URLs, correos y menciones se
    quitan antes de tokenizar (:func:`_texto_tokenizable`).
    """
    if not texto or "@" in texto or _RE_DIGITOS_LARGOS.search(texto) or _RE_URL.search(texto):
        return False
    if _RE_CORREO.search(texto):
        return False
    return not any(_sin_tildes(t) in excluir for t in _RE_NOMBRE_AUTOR.findall(texto))


# ---------------------------------------------------------------------------
# Parseo
# ---------------------------------------------------------------------------


def normalizar_linea(linea: str) -> str:
    """Quita ``U+200E``, ``U+200F``, ``U+202A``–``U+202E`` y ``U+FEFF``; ``U+202F``/``U+00A0`` → espacio; sin ``\\r``."""
    linea = _RE_INVISIBLES.sub("", linea)
    return linea.replace(" ", " ").replace(" ", " ").replace("\r", "")


def _detectar_orden(fechas: Iterable[str]) -> str:
    """``dmy`` si algún primer campo es > 12; ``mdy`` si algún segundo campo lo es; si no, ``dmy``."""
    primero_mayor = segundo_mayor = False
    for fecha in fechas:
        partes = fecha.split("/")
        if len(partes) != 3:
            continue
        if int(partes[0]) > 12:
            primero_mayor = True
        if int(partes[1]) > 12:
            segundo_mayor = True
    if primero_mayor:
        return "dmy"
    if segundo_mayor:
        return "mdy"
    return "dmy"


def _construir_fecha(fecha: str, hora: str, ampm: str | None, orden: str) -> datetime | None:
    """``datetime`` *naive* desde la cabecera; ``None`` si la fecha u hora no existen."""
    try:
        a, b, anio_txt = fecha.split("/")
        dia, mes = (int(a), int(b)) if orden == "dmy" else (int(b), int(a))
        anio = int(anio_txt)
        if len(anio_txt) <= 2:
            anio += 2000
        partes = [int(p) for p in hora.split(":")]
        horas, minutos = partes[0], partes[1]
        segundos = partes[2] if len(partes) > 2 else 0
        if ampm:
            marca = ampm.strip().lower()[:1]
            if marca == "p" and 1 <= horas <= 11:
                horas += 12
            elif marca == "a" and horas == 12:
                horas = 0
        return datetime(anio, mes, dia, horas, minutos, segundos)
    except (ValueError, IndexError):
        return None


def _es_multimedia(texto: str) -> bool:
    minus = texto.lower()
    return any(marcador in minus for marcador in MARCADORES_MULTIMEDIA)


def _es_aviso_sistema(texto: str) -> bool:
    minus = texto.strip().lower()
    return any(minus.startswith(aviso) for aviso in _TEXTOS_SISTEMA)


def _construir_mensaje(ts: datetime, resto: str, extra: Sequence[str]) -> MensajeWsp:
    autor = ""
    sistema = False
    if ": " in resto:
        autor, texto = resto.split(": ", 1)
    else:
        texto = resto
        sistema = True
    if extra:
        texto = texto + "\n" + "\n".join(extra)
    texto = _RE_EDITADO.sub("", texto.rstrip()).rstrip()
    autor = autor.strip()
    if sistema or not autor or _es_aviso_sistema(texto):
        return MensajeWsp(ts=ts, autor="", texto=texto.strip(), sistema=True)
    return MensajeWsp(ts=ts, autor=autor, texto=texto, multimedia=_es_multimedia(texto))


def parsear_lineas(lineas: Iterable[str], *, orden_fecha: str | None = None) -> list[MensajeWsp]:
    """Convierte las líneas de un export (Android o iOS) en mensajes.

    - Una línea sin cabecera se une con ``\\n`` al mensaje anterior (multilínea).
    - ``resto`` se separa en autor y texto en el primer ``": "``; sin él es un mensaje de sistema.
    - ``orden_fecha``: ``"dmy"``, ``"mdy"`` o ``None`` (detección automática; Colombia = ``dmy``).
    - Las líneas ilegibles (antes de la primera cabecera o con fecha imposible) se saltan con
      un contador.
    """
    if orden_fecha not in (None, "dmy", "mdy"):
        log.warning("Orden de fecha desconocido (%r); uso la detección automática", orden_fecha)
        orden_fecha = None
    registros: list[tuple[str, str, str | None, str, list[str]]] = []
    ilegibles = 0
    for cruda in lineas:
        linea = normalizar_linea(str(cruda)).rstrip("\n")
        coincidencia = ANDROID_RE.match(linea) or IOS_RE.match(linea)
        if coincidencia:
            registros.append((coincidencia.group("fecha"), coincidencia.group("hora"),
                              coincidencia.group("ampm"), coincidencia.group("resto"), []))
        elif registros:
            registros[-1][4].append(linea)
        elif linea.strip():
            ilegibles += 1
    orden = orden_fecha or _detectar_orden(r[0] for r in registros)
    mensajes: list[MensajeWsp] = []
    for fecha, hora, ampm, resto, extra in registros:
        ts = _construir_fecha(fecha, hora, ampm, orden)
        if ts is None:
            ilegibles += 1 + len(extra)
            continue
        mensajes.append(_construir_mensaje(ts, resto, extra))
    mensajes = _recuperar_cabeceras_vacias(mensajes)
    if ilegibles:
        log.info("Se saltaron %d líneas ilegibles del chat", ilegibles)
    log.debug("Chat parseado: %d mensajes (orden %s)", len(mensajes), orden)
    return mensajes


def _recuperar_cabeceras_vacias(mensajes: list[MensajeWsp]) -> list[MensajeWsp]:
    """Repara ``"Autor:"`` sin espacio final (editores que recortan espacios): si la primera línea
    de un "mensaje de sistema" es exactamente ``<autor conocido>:``, es un mensaje de ese autor
    cuyo texto empieza en la línea siguiente."""
    conocidos = {m.autor for m in mensajes if not m.sistema and m.autor}
    if not conocidos:
        return mensajes
    reparados: list[MensajeWsp] = []
    for mensaje in mensajes:
        primera, _, resto = mensaje.texto.partition("\n")
        candidato = primera.strip()[:-1].strip() if primera.strip().endswith(":") else ""
        if mensaje.sistema and candidato in conocidos:
            texto = resto.strip("\n")
            reparados.append(MensajeWsp(ts=mensaje.ts, autor=candidato, texto=texto,
                                        multimedia=_es_multimedia(texto)))
        else:
            reparados.append(mensaje)
    return reparados


def _leer_texto(ruta: Path) -> str:
    """Lee el archivo probando ``utf-8``, luego ``utf-8-sig`` y por último ``latin-1``."""
    datos = Path(ruta).read_bytes()
    for codificacion in ("utf-8", "utf-8-sig"):
        try:
            return datos.decode(codificacion)
        except UnicodeDecodeError:
            log.debug("El chat no está en %s; pruebo otra codificación", codificacion)
    return datos.decode("latin-1")


def parsear_archivo(ruta: Path, *, orden_fecha: str | None = None) -> list[MensajeWsp]:
    """Parsea un ``.txt`` exportado. Lanza ``OSError`` si no se puede leer (la CLI devuelve 1)."""
    return parsear_lineas(_leer_texto(Path(ruta)).splitlines(), orden_fecha=orden_fecha)


def detectar_autores(mensajes: Sequence[MensajeWsp]) -> Counter[str]:
    """Conteo de mensajes por autor (sin los de sistema)."""
    return Counter(m.autor for m in mensajes if not m.sistema and m.autor)


def agrupar_rafagas(mensajes: Sequence[MensajeWsp], autor: str, gap_s: float = 120.0) -> list[list[MensajeWsp]]:
    """Ráfagas de ``autor``: mensajes consecutivos suyos con huecos ≤ ``gap_s`` segundos.

    Un mensaje de otra persona corta la ráfaga; los mensajes de sistema se ignoran.
    """
    rafagas: list[list[MensajeWsp]] = []
    actual: list[MensajeWsp] = []
    anterior: MensajeWsp | None = None
    for mensaje in mensajes:
        if mensaje.sistema:
            continue
        if mensaje.autor == autor:
            hueco = (mensaje.ts - anterior.ts).total_seconds() if anterior is not None else None
            if actual and anterior is not None and anterior.autor == autor and hueco is not None \
                    and 0 <= hueco <= gap_s:
                actual.append(mensaje)
            else:
                if actual:
                    rafagas.append(actual)
                actual = [mensaje]
        elif actual:
            rafagas.append(actual)
            actual = []
        anterior = mensaje
    if actual:
        rafagas.append(actual)
    return rafagas


# ---------------------------------------------------------------------------
# Muletillas, nombres propios y temas
# ---------------------------------------------------------------------------


def _contar_ngramas(textos: Sequence[str]) -> tuple[Counter[tuple[str, ...]], dict[tuple[str, ...], Counter[str]]]:
    """Conteo de n-gramas (1–3) por clave normalizada y sus formas de superficie."""
    conteo: Counter[tuple[str, ...]] = Counter()
    formas: dict[tuple[str, ...], Counter[str]] = {}
    for texto in textos:
        tokens = _tokens(texto)
        normales = [_sin_tildes(t) for t in tokens]
        for n in (1, 2, 3):
            for i in range(len(tokens) - n + 1):
                clave = tuple(normales[i:i + n])
                conteo[clave] += 1
                formas.setdefault(clave, Counter())[" ".join(tokens[i:i + n])] += 1
    return conteo, formas


def _ngrama_admisible(clave: tuple[str, ...], excluir: frozenset[str] | set[str]) -> bool:
    """Filtros de §10.9 (excluidos, dígitos, tokens < 2, solo stopwords) para un n-grama."""
    if any(t in excluir or len(t) < 2 or any(c.isdigit() for c in t) for t in clave):
        return False
    if len(clave) == 1:
        token = clave[0]
        return token not in STOPWORDS_ES or token in RISAS or token in INTERJECCIONES
    if all(t in STOPWORDS_ES for t in clave):
        return " ".join(clave) in EXPRESIONES_COLOQUIALES
    return True


def extraer_muletillas(propios: Sequence[str], ajenos: Sequence[str], *, excluir: Iterable[str] = (),
                       top: int = 25, min_freq: int = 5) -> list[dict[str, Any]]:
    """Muletillas propias: n-gramas (1–3) frecuentes con *lift* ≥ 1,5 frente a los mensajes ajenos.

    ``lift = ((c_propio+1)/(N_propio+V)) / ((c_ajeno+1)/(N_ajeno+V))``. Orden por
    ``freq·log(1+lift)``. Sin mensajes ajenos, ``lift = 1.0`` y se ordena por frecuencia.
    Devuelve ``[{"ngrama", "freq", "lift"}]``.
    """
    excluidos = {_sin_tildes(str(e)) for e in excluir if str(e).strip()}
    c_propio, formas = _contar_ngramas(list(propios))
    hay_ajenos = any(str(t).strip() for t in ajenos)
    c_ajeno, _ = _contar_ngramas(list(ajenos)) if hay_ajenos else (Counter(), {})
    n_propio = sum(c_propio.values())
    n_ajeno = sum(c_ajeno.values())
    vocabulario = len(set(c_propio) | set(c_ajeno)) or 1
    candidatos: list[tuple[float, int, str, float]] = []
    for clave, freq in c_propio.items():
        if freq < min_freq or not _ngrama_admisible(clave, excluidos):
            continue
        if hay_ajenos:
            p_propio = (freq + 1) / (n_propio + vocabulario)
            p_ajeno = (c_ajeno.get(clave, 0) + 1) / (n_ajeno + vocabulario)
            lift = p_propio / p_ajeno
            if lift < MIN_LIFT:
                continue
            orden = freq * math.log(1 + lift)
        else:
            lift = 1.0
            orden = float(freq)
        superficie = formas[clave].most_common(1)[0][0]
        candidatos.append((orden, freq, superficie, lift))
    candidatos.sort(key=lambda c: (-c[0], -c[1], c[2]))
    return [{"ngrama": superficie, "freq": freq, "lift": round(lift, 2)}
            for _, freq, superficie, lift in candidatos[:max(0, top)]]


_ENVOLTURAS: Final = frozenset(" \t\"'«»“”‘’([{-—–*_~")
_FIN_DE_FRASE: Final = frozenset(".!?¿¡…")


def _inicio_de_frase(texto: str, inicio: int) -> bool:
    """``True`` si la palabra que empieza en ``inicio`` abre frase (inicio, ``.!?¿¡`` o salto de línea)."""
    j = inicio - 1
    while j >= 0 and (texto[j] in _ENVOLTURAS or texto[j] == "\n"):
        if texto[j] == "\n":
            return True
        j -= 1
    return j < 0 or texto[j] in _FIN_DE_FRASE


def detectar_nombres_propios(textos: Sequence[str], *, umbral: float = 0.6) -> set[str]:
    """Tokens (normalizados) que van con mayúscula **fuera del inicio de frase** en ≥ ``umbral``
    de esas apariciones, con al menos 2. Las palabras en MAYÚSCULAS sostenidas (gritos, siglas)
    no cuentan.
    """
    total: Counter[str] = Counter()
    mayus: Counter[str] = Counter()
    for texto in textos:
        texto = str(texto)
        for coincidencia in _RE_PALABRA.finditer(texto):
            palabra = coincidencia.group()
            if len(palabra) < 2 or palabra.isupper():
                continue
            if _inicio_de_frase(texto, coincidencia.start()):
                continue
            clave = _sin_tildes(palabra)
            total[clave] += 1
            if palabra[0].isupper():
                mayus[clave] += 1
    return {clave for clave, n in total.items() if n >= 2 and mayus[clave] / n >= umbral}


# Palabras comunes que suelen abrir mensajes (el teclado del móvil las pone con mayúscula): nunca
# se toman por nombres aunque casi no aparezcan en mitad de frase.
_COMUNES_AL_INICIO: Final[frozenset[str]] = (
    STOPWORDS_ES | INTERJECCIONES | _NO_TEMAS | frozenset(RISAS)
    | frozenset(t for frase in SALUDOS_BASE + DESPEDIDAS_BASE for t in _sin_tildes(frase).split())
    | frozenset({"oye", "mira", "porfa", "perdon", "obvio", "exacto", "sisas", "nono", "amor", "bro",
                 "mano", "hermano", "hermana", "pana", "socio", "venga", "espera", "jefe", "okey"}))


def _nombres_al_inicio(textos: Sequence[str]) -> set[str]:
    """Nombres propios que casi solo aparecen al inicio de frase (complemento de
    :func:`detectar_nombres_propios`, que descarta esas apariciones).

    Un token de 3 letras o más que no sea palabra común (*stopword*, risa, interjección, saludo…),
    con al menos 2 apariciones y mayúscula inicial en más del 40 % de ellas (cuente o no el inicio
    de frase), se trata como nombre salvo que haya pruebas de que es una palabra corriente: al
    menos 2 apariciones en minúscula en mitad de frase y ≥ 60 % de sus apariciones en mitad de
    frase. Así "Juancho ya llegó" se excluye, pero "Bacano" (autocorrector) con "qué bacano" no.
    Las palabras en MAYÚSCULAS sostenidas no cuentan.
    """
    total: Counter[str] = Counter()
    mayus: Counter[str] = Counter()
    en_medio: Counter[str] = Counter()
    minuscula_en_medio: Counter[str] = Counter()
    for texto in textos:
        texto = _texto_tokenizable(str(texto))
        for coincidencia in _RE_PALABRA.finditer(texto):
            palabra = coincidencia.group()
            if len(palabra) < 3 or palabra.isupper():
                continue
            clave = _sin_tildes(palabra)
            total[clave] += 1
            if palabra[0].isupper():
                mayus[clave] += 1
            if not _inicio_de_frase(texto, coincidencia.start()):
                en_medio[clave] += 1
                if palabra == palabra.lower():
                    minuscula_en_medio[clave] += 1
    sospechosos: set[str] = set()
    for clave, n in total.items():
        if n < 2 or clave in _COMUNES_AL_INICIO or mayus[clave] / n <= 0.4:
            continue
        minus = minuscula_en_medio[clave]
        if minus >= 2 and minus / en_medio[clave] >= 0.6:
            continue                     # palabra corriente que solo va con mayúscula al abrir frase
        sospechosos.add(clave)
    return sospechosos


def _tokens_de_nombre(nombre: str) -> set[str]:
    return {_sin_tildes(t) for t in _RE_NOMBRE_AUTOR.findall(nombre)}


def _temas(textos: Sequence[str], excluir: frozenset[str] | set[str]) -> list[dict[str, Any]]:
    """Los 15 unigramas propios más frecuentes (≥ 4 letras) escritos en minúscula ≥ 60 % de las veces."""
    total_original: Counter[str] = Counter()
    minuscula: Counter[str] = Counter()
    for texto in textos:
        for palabra in _RE_PALABRA.findall(_texto_tokenizable(texto)):
            clave = _sin_tildes(palabra)
            total_original[clave] += 1
            if palabra == palabra.lower():
                minuscula[clave] += 1
    frecuencia: Counter[str] = Counter()
    formas: dict[str, Counter[str]] = {}
    for texto in textos:
        for token in _tokens(texto):
            clave = _sin_tildes(token)
            frecuencia[clave] += 1
            formas.setdefault(clave, Counter())[token] += 1
    temas: list[tuple[int, str]] = []
    for clave, freq in frecuencia.items():
        if (freq < MIN_FREQ_TEMA or len(clave) < 4 or clave in STOPWORDS_ES or clave in RISAS
                or clave in INTERJECCIONES or clave in _NO_TEMAS or clave in excluir):
            continue
        apariciones = total_original.get(clave, 0)
        if apariciones and minuscula[clave] / apariciones < 0.6:
            continue
        temas.append((freq, formas[clave].most_common(1)[0][0]))
    temas.sort(key=lambda t: (-t[0], t[1]))
    return [{"palabra": palabra, "freq": freq} for freq, palabra in temas[:MAX_TEMAS]]


def _frases_en(texto: str, frases: Sequence[str]) -> list[str]:
    """Frases base presentes en el texto (palabra completa), sin las contenidas en otra encontrada."""
    normal = _sin_tildes(texto)
    halladas = [f for f in frases if re.search(r"\b" + re.escape(_sin_tildes(f)) + r"\b", normal)]
    return [f for f in halladas
            if not any(f != otra and _sin_tildes(f) in _sin_tildes(otra) for otra in halladas)]


def _saludos_y_despedidas(propios: Sequence[MensajeWsp]) -> tuple[list[str], list[str]]:
    """Los 3 saludos (primer mensaje propio del día) y despedidas (último) más frecuentes."""
    por_dia: dict[Any, list[MensajeWsp]] = {}
    for mensaje in propios:
        if mensaje.multimedia or not mensaje.texto.strip():
            continue
        por_dia.setdefault(mensaje.ts.date(), []).append(mensaje)
    saludos: Counter[str] = Counter()
    despedidas: Counter[str] = Counter()
    for mensajes_dia in por_dia.values():
        mensajes_dia.sort(key=lambda m: m.ts)
        saludos.update(_frases_en(mensajes_dia[0].texto, SALUDOS_BASE))
        despedidas.update(_frases_en(mensajes_dia[-1].texto, DESPEDIDAS_BASE))

    def top(conteo: Counter[str], base: Sequence[str]) -> list[str]:
        return [f for f, _ in sorted(conteo.items(), key=lambda par: (-par[1], base.index(par[0])))[:3]]

    return top(saludos, SALUDOS_BASE), top(despedidas, DESPEDIDAS_BASE)


def _empieza_con_mayuscula(texto: str) -> bool:
    primera = _RE_PALABRA.search(texto)
    return bool(primera) and primera.group()[0].isupper()


def _emojis(texto: str) -> list[str]:
    return [c for c in _RE_EMOJI.findall(texto) if c not in _MODIFICADORES_EMOJI]


def _resolver_autor(yo: str, autores: Mapping[str, int]) -> str | None:
    """Autor del chat que corresponde a ``yo`` (exacto, o sin distinguir mayúsculas/tildes/espacios)."""
    if yo in autores:
        return yo
    buscado = _RE_ESPACIOS.sub(" ", _sin_tildes(yo)).strip()
    for autor in autores:
        if _RE_ESPACIOS.sub(" ", _sin_tildes(autor)).strip() == buscado:
            return autor
    return None


def _franja(hora: int) -> str:
    if 5 <= hora <= 11:
        return "manana"
    if 12 <= hora <= 18:
        return "tarde"
    if 19 <= hora <= 23:
        return "noche"
    return "madrugada"


def _segmentar(mensajes: Sequence[MensajeWsp], cortes: Iterable[int]) -> list[Sequence[MensajeWsp]]:
    """Parte ``mensajes`` en los índices de ``cortes`` (inicio de cada chat); sin trozos vacíos."""
    indices = sorted({int(c) for c in cortes if 0 < int(c) < len(mensajes)})
    limites = [0, *indices, len(mensajes)]
    return [mensajes[a:b] for a, b in zip(limites, limites[1:]) if b > a]


def construir_perfil(mensajes: Sequence[MensajeWsp], yo: str, *, gap_s: float = 120.0,
                     excluir_extra: Iterable[str] = (), cortes: Iterable[int] = ()) -> dict[str, Any]:
    """Construye el perfil conductual de ``yo`` (esquema JSON exacto de §10.9).

    Solo los mensajes de ``yo`` alimentan las estadísticas; los ajenos son línea base de
    frecuencias. Lanza ``ValueError`` si ``yo`` no aparece entre los autores.

    ``cortes`` (opcional): índices de ``mensajes`` donde empieza otro chat (varios archivos
    concatenados). Las ráfagas y las latencias de respuesta nunca emparejan mensajes de chats
    distintos.
    """
    autores = detectar_autores(mensajes)
    yo_real = _resolver_autor(str(yo), autores)
    if yo_real is None:
        raise ValueError("el autor indicado con --yo no aparece en el chat")
    no_sistema = [m for m in mensajes if not m.sistema]
    propios = [m for m in no_sistema if m.autor == yo_real]
    ajenos = [m for m in no_sistema if m.autor != yo_real]
    textos_propios = [m.texto for m in propios if not m.multimedia and m.texto.strip()]
    textos_ajenos = [m.texto for m in ajenos if not m.multimedia and m.texto.strip()]

    # Conjunto excluir (§10.9): autores, menciones, nombres propios detectados (también los que
    # casi solo aparecen al abrir frase, p. ej. un apodo), nombres comunes y extra.
    excluir: set[str] = set(NOMBRES_COMUNES_ES)
    for autor in autores:
        excluir |= _tokens_de_nombre(autor)
    for texto in textos_propios + textos_ajenos:
        for mencion in _RE_MENCION.findall(texto):
            excluir |= _tokens_de_nombre(mencion)
    excluir |= detectar_nombres_propios(textos_propios + textos_ajenos)
    excluir |= _nombres_al_inicio(textos_propios + textos_ajenos)
    for extra in excluir_extra:
        excluir |= _tokens_de_nombre(str(extra))
    excluir_f = frozenset(excluir)

    n_textos = len(textos_propios)

    def proporcion(condicion: Any) -> float:
        return round(sum(1 for t in textos_propios if condicion(t)) / n_textos, 2) if n_textos else 0.0

    palabras = [len(t.split()) for t in textos_propios]
    # Ráfagas y latencias por chat: el último mensaje de un archivo y el primero del siguiente
    # no son una conversación.
    segmentos = _segmentar(mensajes, cortes)
    rafagas = [r for segmento in segmentos for r in agrupar_rafagas(segmento, yo_real, gap_s)]
    duraciones = [(r[-1].ts - r[0].ts).total_seconds() for r in rafagas if len(r) > 1]
    latencias: list[float] = []
    for segmento in segmentos:
        conversacion = [m for m in segmento if not m.sistema]
        for anterior, actual in zip(conversacion, conversacion[1:]):
            if anterior.autor != yo_real and actual.autor == yo_real:
                diferencia = (actual.ts - anterior.ts).total_seconds()
                if 0 <= diferencia < LATENCIA_MAX_S:
                    latencias.append(diferencia)

    emojis: Counter[str] = Counter()
    for texto in textos_propios:
        emojis.update(c for c in _emojis(texto) if not 0x1F1E6 <= ord(c) <= 0x1F1FF)
    risa: Counter[str] = Counter()
    for texto in textos_propios:
        risa.update(t for t in _tokens(texto) if t in RISAS)

    histograma = [0] * 24
    for mensaje in propios:
        histograma[mensaje.ts.hour] += 1
    maximo = max(histograma)
    hora_pico = histograma.index(maximo) if maximo else 0

    saludos, despedidas = _saludos_y_despedidas(propios)
    muletillas = [m for m in extraer_muletillas(textos_propios, textos_ajenos, excluir=excluir_f)
                  if _es_texto_seguro(m["ngrama"], excluir_f)]
    temas = [t for t in _temas(textos_propios, excluir_f) if _es_texto_seguro(t["palabra"], excluir_f)]
    fechas = sorted(m.ts.date() for m in propios)

    perfil: dict[str, Any] = {
        "version": VERSION_PERFIL,
        "generado": datetime.now(UTC).isoformat(timespec="seconds"),
        "fuente": {
            "archivos": len(segmentos),
            "mensajes_propios": len(propios),
            "rango": [fechas[0].isoformat(), fechas[-1].isoformat()] if fechas else [],
        },
        "estilo": {
            "longitud_media_palabras": round(statistics.fmean(palabras), 1) if palabras else 0.0,
            "mensajes_por_rafaga": round(statistics.fmean(len(r) for r in rafagas), 1) if rafagas else 0.0,
            "rafaga_mediana_s": int(round(statistics.median(duraciones))) if duraciones else 0,
            "latencia_respuesta_mediana_s": int(round(statistics.median(latencias))) if latencias else None,
            "pct_con_emoji": proporcion(lambda t: bool(_emojis(t))),
            "emojis_top": [e for e, _ in emojis.most_common(3)],
            "pct_preguntas": proporcion(lambda t: "?" in t),
            "pct_signo_apertura": proporcion(lambda t: "¿" in t or "¡" in t),
            "pct_mayuscula_inicial": proporcion(_empieza_con_mayuscula),
            "risa": {clave: risa.get(clave, 0) for clave in RISAS},
        },
        "muletillas": muletillas,
        "horario": {
            "histograma": [round(v / maximo, 3) if maximo else 0.0 for v in histograma],
            "hora_pico": hora_pico,
            "franja": _franja(hora_pico),
        },
        "temas": temas,
        "saludos": saludos,
        "despedidas": despedidas,
    }
    log.info("Perfil conductual construido: %d mensajes propios, %d muletillas, %d temas",
             len(propios), len(muletillas), len(temas))
    return perfil


# ---------------------------------------------------------------------------
# Persistencia y resumen
# ---------------------------------------------------------------------------


def _chmod_privado(ruta: Path) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:
        log.debug("No se pudo aplicar 0o600 al perfil: %s", exc)


def guardar_perfil(perfil: Mapping[str, Any], ruta: Path) -> None:
    """Escribe el perfil en JSON (UTF-8) de forma atómica (``.tmp`` + ``os.replace``) con 0o600.

    Lanza ``OSError`` si no se puede escribir (la CLI lo convierte en el código 1).
    """
    ruta = Path(ruta)
    try:
        ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("No se pudo crear la carpeta del perfil: %s", exc)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    datos = json.dumps(dict(perfil), ensure_ascii=False, indent=2) + "\n"
    banderas = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporal, banderas, 0o600)
    try:
        try:
            archivo = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with archivo:
            archivo.write(datos.encode("utf-8"))
            archivo.flush()
            os.fsync(archivo.fileno())
        _chmod_privado(temporal)
        os.replace(temporal, ruta)
    except BaseException:
        try:
            temporal.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("No se pudo borrar el temporal del perfil: %s", exc)
        raise
    _chmod_privado(ruta)
    log.info("Perfil conductual guardado (%d bytes)", len(datos.encode("utf-8")))


def cargar_perfil(ruta: Path) -> dict[str, Any] | None:
    """Lee ``perfil_conductual.json``. Ausente, ilegible o inválido ⇒ ``None`` (nunca lanza)."""
    try:
        texto = Path(ruta).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        log.debug("No hay perfil heredado todavía")
        return None
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("No se pudo leer el perfil heredado: %s", type(exc).__name__)
        return None
    try:
        datos = json.loads(texto)
    except ValueError as exc:
        log.warning("El perfil heredado no es JSON válido (%s); se ignora", exc)
        return None
    if not isinstance(datos, dict):
        log.warning("El perfil heredado no es un objeto JSON; se ignora")
        return None
    version = datos.get("version")
    if isinstance(version, int) and version > VERSION_PERFIL:
        log.warning("Perfil heredado v%d más nuevo que este programa (v%d); se usa lo que se entienda",
                    version, VERSION_PERFIL)
    return datos


def _item(valor: Any, maximo: int = 40) -> str:
    """Cadena del perfil saneada para el prompt (sin control, comillas angulares ni datos de contacto)."""
    if not isinstance(valor, str):
        return ""
    texto = _RE_CONTROL.sub("", valor).replace("«", "").replace("»", "").replace("<", "").replace(">", "")
    texto = _RE_ESPACIOS.sub(" ", texto).strip()
    if not texto or _RE_DIGITOS_LARGOS.search(texto) or "@" in texto or _RE_URL.search(texto):
        return ""
    return texto[:maximo].strip()


def _items(valores: Any, clave: str | None, maximo: int) -> list[str]:
    if not isinstance(valores, (list, tuple)):
        return []
    salida: list[str] = []
    for valor in valores:
        crudo = valor.get(clave) if (clave and isinstance(valor, Mapping)) else valor
        item = _item(crudo)
        if item and item not in salida:
            salida.append(item)
        if len(salida) >= maximo:
            break
    return salida


_FRANJA_RESUMEN: Final = {"madrugada": "madrugada", "manana": "mañana", "tarde": "tarde", "noche": "noche"}


def resumen_perfil(perfil: Mapping[str, Any], max_chars: int = 600) -> str:
    """Resumen en español del perfil (≤ ``max_chars``) para ``<perfil_usuario>`` del system prompt.

    Ejemplo: ``"Escribe en ráfagas cortas (~7 palabras; 2,4 mensajes por ráfaga). Muletillas:
    «de una», «parce», «jaja». Más activo hacia las 22 h (noche). Emojis en el 18 % de sus
    mensajes. Temas: código, música, fútbol."``
    """
    limite = max(1, int(max_chars))
    if not isinstance(perfil, Mapping):
        return ""
    estilo = perfil.get("estilo") if isinstance(perfil.get("estilo"), Mapping) else {}
    horario = perfil.get("horario") if isinstance(perfil.get("horario"), Mapping) else {}
    frases: list[str] = []

    longitud = _real(estilo.get("longitud_media_palabras"))
    if longitud is not None and longitud > 0:
        detalle = f"~{max(1, round(longitud))} palabras"
        rafaga = _real(estilo.get("mensajes_por_rafaga"))
        if rafaga is not None and rafaga > 0:
            detalle += f"; {_decimal(rafaga)} mensajes por ráfaga"
        if longitud <= 12:
            frases.append(f"Escribe en ráfagas cortas ({detalle}).")
        elif longitud <= 25:
            frases.append(f"Escribe mensajes de longitud media ({detalle}).")
        else:
            frases.append(f"Escribe mensajes largos ({detalle}).")

    muletillas = _items(perfil.get("muletillas"), "ngrama", 5)
    if muletillas:
        frases.append("Muletillas: " + ", ".join(f"«{m}»" for m in muletillas) + ".")

    hora = horario.get("hora_pico")
    if isinstance(hora, (int, float)) and not isinstance(hora, bool) and 0 <= int(hora) <= 23:
        franja = horario.get("franja") if horario.get("franja") in _FRANJA_RESUMEN else _franja(int(hora))
        frases.append(f"Más activo hacia las {int(hora)} h ({_FRANJA_RESUMEN[franja]}).")

    pct_emoji = _real(estilo.get("pct_con_emoji"))
    if pct_emoji is not None and pct_emoji > 0:
        frases.append(f"Emojis en el {round(min(pct_emoji, 1.0) * 100)} % de sus mensajes.")

    temas = _items(perfil.get("temas"), "palabra", 5)
    if temas:
        frases.append("Temas: " + ", ".join(temas) + ".")

    saludos = _items(perfil.get("saludos"), None, 2)
    despedidas = _items(perfil.get("despedidas"), None, 2)
    if saludos or despedidas:
        partes = []
        if saludos:
            partes.append("saluda con " + " o ".join(f"«{s}»" for s in saludos))
        if despedidas:
            partes.append("se despide con " + " o ".join(f"«{d}»" for d in despedidas))
        texto = "; ".join(partes)
        frases.append(texto[0].upper() + texto[1:] + ".")

    risa = estilo.get("risa")
    if isinstance(risa, Mapping):
        conteos = [(k, _real(v) or 0.0) for k, v in risa.items() if isinstance(k, str) and _item(k)]
        conteos = [par for par in conteos if par[1] > 0]
        if conteos:
            frases.append(f"Ríe con «{_item(max(conteos, key=lambda par: par[1])[0])}».")

    pct_preg = _real(estilo.get("pct_preguntas"))
    if pct_preg is not None and pct_preg > 0:
        frases.append(f"Pregunta en el {round(min(pct_preg, 1.0) * 100)} % de sus mensajes.")

    latencia = _real(estilo.get("latencia_respuesta_mediana_s"))
    if latencia is not None and latencia > 0:
        frases.append("Responde en menos de un minuto." if latencia < 60
                      else f"Responde en ~{round(latencia / 60)} min (mediana).")

    if not frases:
        frases.append("Perfil heredado con pocos datos.")
    resumen = ""
    for frase in frases:
        candidato = f"{resumen} {frase}".strip()
        if len(candidato) > limite:
            break
        resumen = candidato
    if not resumen:
        resumen = frases[0][: max(0, limite - 1)].rstrip() + "…" if limite > 1 else frases[0][:limite]
    return resumen[:limite]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _imprimir(texto: str, *, error: bool = False) -> None:
    """``print`` tolerante a consolas que no soportan todos los caracteres."""
    flujo = sys.stderr if error else sys.stdout
    try:
        print(texto, file=flujo)
    except UnicodeEncodeError:
        codificacion = getattr(flujo, "encoding", None) or "ascii"
        print(texto.encode(codificacion, errors="replace").decode(codificacion, errors="replace"), file=flujo)


def _ruta_perfil_defecto() -> Path:
    """``cargar_settings().perfil_path`` (``core.config`` se importa solo aquí)."""
    try:
        from core.config import cargar_settings
    except ImportError as exc:  # ejecución fuera del proyecto
        log.debug("core.config no disponible (%s); uso la ruta por defecto del proyecto", exc)
        return Path(__file__).resolve().parent.parent / "data" / "john" / "perfil_conductual.json"
    return cargar_settings().perfil_path


def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.wsp_ingestor",
        description="Convierte chats exportados de WhatsApp (.txt) en perfil_conductual.json "
                    "(solo patrones propios; nada sale de tu equipo).")
    parser.add_argument("archivos", nargs="+", metavar="ARCHIVO", help="chats exportados (.txt)")
    parser.add_argument("--yo", metavar="NOMBRE", help="tu nombre tal como aparece en el chat")
    parser.add_argument("--salida", metavar="RUTA", help="ruta del perfil (por defecto, la de Settings)")
    parser.add_argument("--gap", type=float, default=120.0, metavar="S",
                        help="segundos máximos entre mensajes de una misma ráfaga (120)")
    parser.add_argument("--orden", choices=("dmy", "mdy"), help="orden de la fecha (auto por defecto)")
    parser.add_argument("--excluir", metavar="N1,N2", default="",
                        help="nombres adicionales que nunca deben aparecer en el perfil")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI. Códigos: 0 OK · 1 error de lectura/escritura · 2 falta ``--yo`` o no aparece en el chat."""
    parser = _construir_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as salida:  # argparse: --help (0) o uso incorrecto (2)
        return salida.code if isinstance(salida.code, int) else 2
    if not math.isfinite(args.gap) or args.gap <= 0:
        _imprimir("--gap debe ser un número de segundos mayor que 0.", error=True)
        return 2

    mensajes: list[MensajeWsp] = []
    cortes: list[int] = []                 # inicio de cada archivo: no se mezclan conversaciones
    for archivo in args.archivos:
        cortes.append(len(mensajes))
        try:
            mensajes.extend(parsear_archivo(Path(archivo), orden_fecha=args.orden))
        except OSError as exc:
            _imprimir(f"No se pudo leer {archivo}: {exc.strerror or exc}", error=True)
            return 1
    autores = detectar_autores(mensajes)
    if not autores:
        _imprimir("No se encontraron mensajes de WhatsApp reconocibles en los archivos indicados.", error=True)
        return 1

    def listar_autores() -> None:
        _imprimir("Autores detectados:")
        for autor, n in autores.most_common():
            _imprimir(f"  - {autor}: {n} mensajes")

    if not args.yo:
        listar_autores()
        _imprimir('Indica cuál eres tú con --yo "NOMBRE".')
        return 2
    if _resolver_autor(args.yo, autores) is None:
        _imprimir("El nombre indicado con --yo no aparece en el chat.", error=True)
        listar_autores()
        return 2

    excluir_extra = [n.strip() for n in str(args.excluir or "").split(",") if n.strip()]
    perfil = construir_perfil(mensajes, args.yo, gap_s=args.gap, excluir_extra=excluir_extra, cortes=cortes)
    perfil["fuente"]["archivos"] = len(args.archivos)
    ruta = Path(args.salida).expanduser() if args.salida else _ruta_perfil_defecto()
    try:
        guardar_perfil(perfil, ruta)
    except OSError as exc:
        _imprimir(f"No se pudo guardar el perfil en {ruta}: {exc.strerror or exc}", error=True)
        return 1
    _imprimir(f"Perfil generado: {perfil['fuente']['mensajes_propios']} mensajes propios, "
              f"{len(perfil['muletillas'])} muletillas. Guardado en {ruta}. "
              "Recomendación: borra los .txt exportados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
