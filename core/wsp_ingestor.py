"""Ingestor de chats exportados de WhatsApp → ``perfil_conductual.json`` (§10.9).

Qué hace: parsea exportaciones ``.txt`` de WhatsApp (Android es-CO e iOS, con sus espacios
invisibles y el AM/PM "a. m."/"p. m."), agrupa ráfagas y extrae **solo patrones propios** del
usuario: estilo, muletillas (n-gramas con *lift* frente a la línea base ajena), horario, temas,
saludos y despedidas. El resultado es el **recuerdo heredado** de Amadeus: conoce al usuario
por datos, no por haberlo vivido (como Amadeus Kurisu con Okabe).

Muletillas ≠ temas: una muletilla es un **marcador discursivo**, interjección, relleno o risa
(léxico curado del español coloquial de Colombia en :data:`MARCADORES_ES`, alargamientos como
"hmm"/"siii"/"nooo", risas y "teclazos" tipo ``kakfkak``). Las palabras de contenido
("dinero", "precios"…) solo pueden salir en ``temas``. Los teclazos se agrupan en una única
entrada legible (``"risa de teclazo (kakfkak)"``) y se cuentan en ``estilo["risa_teclazo"]``
(fuera de ``estilo.risa``, que conserva sus cuatro claves de §10.9).

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
import functools
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
    "MARCADORES_ES",
    "EXPRESIONES_COLOQUIALES",
    "CLAVE_RISA_TECLAZO",
    "ETIQUETA_TECLAZO",
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
# Muletillas: las cifras también son tokens (nunca admisibles) para que "ps5" no deje un "ps"
# suelto ni "10 mm" un "mm", y para que ningún n-grama salte por encima de un número.
_RE_TOKEN_MULETILLA: Final = re.compile(r"[a-záéíóúñü0-9]+")
_RE_PALABRA: Final = re.compile(r"[^\W\d_]+")            # palabra con su forma original (mayúsculas)
_RE_NOMBRE_AUTOR: Final = re.compile(r"[^\W_]+")          # tokens (letras o dígitos) de un nombre
_RE_MENCION: Final = re.compile(r"@(\w+)")
_RE_ESPACIOS: Final = re.compile(r"\s+")
_RE_DIGITOS_LARGOS: Final = re.compile(r"\d{7,}")
_RE_SEPARADOR_DIGITOS: Final = re.compile(r"(?<=\d)[\s.\-()/]+(?=\d)")
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

# Risas (§10.9: ja(ja)+ → jaja, je(je)+ → jeje, (js)+ → jsjs, x+d+ → xd); se tolera una j final
# y, en "jaja", una "a" inicial ("ajajaja"). Se comparan sin tildes ("jajajá").
_RISAS_RE: Final = (
    (re.compile(r"a?ja(?:ja)+j?"), "jaja"),
    (re.compile(r"je(?:je)+j?"), "jeje"),
    (re.compile(r"(?:js)+j?"), "jsjs"),
    (re.compile(r"x+d+"), "xd"),
)
RISAS: Final = ("jaja", "jeje", "jsjs", "xd")

# Otras risas escritas: cuentan como muletilla, pero no tienen clave propia en ``estilo.risa``.
_RISAS_EXTRA_RE: Final = (
    (re.compile(r"ha(?:ha)+h?"), "haha"),
    (re.compile(r"he(?:he)+h?"), "hehe"),
    (re.compile(r"ji(?:ji)+j?"), "jiji"),
    (re.compile(r"jo(?:jo)+j?"), "jojo"),
    (re.compile(r"ju(?:ju)+j?"), "juju"),
    (re.compile(r"(?:rs){2,}r?"), "rsrs"),
    (re.compile(r"lo+l"), "lol"),
    (re.compile(r"lmf?ao+"), "lmao"),
)
_RISAS_EXTRA: Final[frozenset[str]] = frozenset(canonico for _, canonico in _RISAS_EXTRA_RE)

# Teclazo (keysmash): risa escrita aporreando el teclado ("kakfkak", "asdfgh", "skjsks").
# Su conteo va en ``estilo["risa_teclazo"]``, fuera de ``estilo.risa`` (§10.9: solo jaja/jeje/
# jsjs/xd): quien toma la clave mayor de ``risa`` como "risa escrita más habitual" diría si no
# que el usuario escribe «teclazo».
CLAVE_RISA_TECLAZO: Final = "risa_teclazo"       # clave de ``estilo`` con el conteo de teclazos
_CLAVE_TECLAZO_EN_RISA: Final = "teclazo"        # ubicación antigua (dentro de ``estilo.risa``)
ETIQUETA_TECLAZO: Final = "risa de teclazo"      # entrada agrupada en ``muletillas``
_CLAVE_TECLAZO: Final = "#teclazo"               # clave interna (nunca coincide con un token)
_MAX_EJEMPLO_TECLAZO: Final = 12                 # letras máximas del ejemplo en la etiqueta
_MIN_VECES_EJEMPLO_TECLAZO: Final = 2            # privacidad: solo variantes repetidas
_MIN_TECLAZOS_RESUMEN: Final = 2
_RE_ETIQUETA_TECLAZO: Final = re.compile(r"risa de teclazo(?: \((?P<ejemplo>[a-z]{1,20})\))?")
_RE_RACHA: Final = re.compile(r"(.)\1+")
_RE_RACHA_LARGA: Final = re.compile(r"(.)\1{2,}")
# Fronteras de cláusula para decidir si un marcador ambiguo ("este", "tipo", "ya") va suelto.
_RE_CORTE_CLAUSULA: Final = re.compile(r"[\n.!?¿¡,;:…()\[\]\"«»“”]+")

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

# Marcadores discursivos, interjecciones, rellenos y vocativos coloquiales (es-CO) que cuentan
# como muletilla en cualquier posición. Normalizados (minúsculas, sin tildes) y sin letras
# repetidas: "hmm" → "hm", "uff" → "uf"; los alargamientos ("buenooo") caen en su base.
MARCADORES_ES: Final[frozenset[str]] = frozenset(
    m for m in (_RE_RACHA.sub(r"\1", p) for p in (INTERJECCIONES | frozenset("""
bueno claro osea pues pue pos dale listo okey okay ok oki okis oka okas na nah nel nop nope sip sipi
sisas sisa aja aha ajam obvio obviamente literal literalmente exacto exactamente efectivamente
parce parcero parcera parcerito marica mk mka marik bro broder brother manito mijo mija mijito
mijita pana bacano bacana chevere melo genial perfecto excelente tranqui hagale hagamosle quiubo
quiubos quihubo porfa porfis porfi plis oye oiga ey hey ay uy ah eh oh uh uf hm pf ps pst aw agh
argh ugh ups wow guau uau ole jum bah puf uish ush ufa uju yuju upa epa erda ombe nojoda juemadre
jueputa hijueputa ojala
""".split()))) if len(m) >= 2)

# Marcadores ambiguos: también son palabras corrientes ("este precio", "ya llegué", "tipo de
# cambio", "cuánto vale"). Solo cuentan como muletilla sueltos en su cláusula ("este... no sé",
# "tipo, ni idea", "ya ya", "bueno ya"; no en "bueno, ya llegué") o alargados ("esteee"), y lo
# mismo dentro de un n-grama.
_MARCADORES_AMBIGUOS: Final[frozenset[str]] = frozenset("""
este tipo nada ya va vale mira mire vea venga verdad cierto ojo pilas fijo fresco super severo
brutal chimba hermano hermana mano socio viejo llave buena hp entonces igual total
""".split())
# Ambiguos que también son marcador al abrir cláusula ("entonces qué", "igual no importa",
# "total que no fui"), salvo si los sigue una comparación o una cantidad ("igual que ayer",
# "igual de caro", "total a pagar", "total de la cuenta") o un número ("total: 50 mil").
_MARCADORES_DE_INICIO: Final[frozenset[str]] = frozenset({"entonces", "igual", "total"})
_NO_SIGUEN_AL_MARCADOR_DE_INICIO: Final[Mapping[str, frozenset[str]]] = {
    "igual": frozenset({"que", "a", "al", "de", "del"}),
    "total": frozenset({"a", "al", "de", "del"}),
}
# Tras un artículo o una preposición, un marcador funciona como sustantivo o adjetivo ("el ups
# del pc", "llegó por ups", "el pos", "dame el ok", "lo bueno es que"); tras un número, como
# unidad ("10 mm"). Ahí no es muletilla.
_ANTES_DE_USO_NOMINAL: Final[frozenset[str]] = frozenset(
    "el la los las lo un una unos unas del al de en por con sin para pa".split())
# Interjecciones que solo lo son alargadas ("grrr", "brrr", "shhh"): "gr" suelto es "gramos".
_INTERJ_SOLO_ALARGADAS: Final[frozenset[str]] = frozenset({"gr", "br", "sh"})
# Palabras que se alargan con solo una letra repetida ("sii", "noo", "yaa"); el resto necesita
# una racha de 3 ("queee") para no confundir "lee" con un "le" alargado.
_ALARGABLES_CORTOS: Final[frozenset[str]] = (_MARCADORES_AMBIGUOS | _INTERJ_SOLO_ALARGADAS
                                             | frozenset({"si", "no"}))
# Variantes de la duda ("hmm", "mmm", "ehm", "ammm") agrupadas en una sola muletilla.
_ALIAS_DUDA: Final[frozenset[str]] = frozenset({"m", "mh", "hm", "hum", "uhm", "ehm"})
_ALIAS_DUDA_ALARGADA: Final[frozenset[str]] = frozenset({"am", "em", "um"})   # solo "amm", "emm"…
# Palabras que pueden repetirse como muletilla aunque solas sean funcionales ("sí sí", "no no").
_REPETIBLES: Final[frozenset[str]] = frozenset({"si", "no"})

# n-gramas que en es-CO funcionan como muletilla aunque lleven palabras funcionales o de
# contenido ("o sea", "la verdad", "no sé", "qué pena"). Normalizados (sin tildes).
EXPRESIONES_COLOQUIALES: Final[frozenset[str]] = frozenset({
    "de una", "que mas", "no mas", "nada mas", "para nada", "por eso", "asi es", "eso si",
    "de una vez", "con todo", "si o que", "si o no",
    "o sea", "o sea que", "osea que", "la verdad", "de verdad", "en serio", "es que", "no se",
    "yo que se", "ni idea", "ni modo", "a ver", "vamos a ver", "en fin", "mejor dicho", "de pronto",
    "de malas", "a lo bien", "todo bien", "a la orden", "con gusto", "mas o menos", "por fa",
    "que pena", "que pereza", "que chimba", "que bacano", "que chevere", "que nota", "que oso",
    "que boleta", "que mamera", "que jartera", "que vaina", "que bien", "que mas pues", "que hubo",
    "y nada", "y bueno", "y pues", "pues si", "pues no", "no pues", "ay no", "ay si", "uy no",
    "uy si", "ah si", "ah no", "si claro", "claro que si", "claro que no", "si senor", "no senor",
    "como asi", "como que", "nada que ver", "no pasa nada", "no hay problema", "no joda", "eso es",
    "ahi vamos",
})

# Expresiones que también son secuencias corrientes ("no se puede", "voy a ver la serie", "la casa
# de una amiga", "lo que más me gusta"): solo cuentan en su contexto de muletilla. Cada regla es
# ``(antes, después)``: las palabras admitidas justo antes y justo después dentro de la cláusula,
# además del borde de la cláusula y de otros marcadores, risas o alargamientos; ``None`` = cualquiera.
_INTERROGATIVOS: Final[frozenset[str]] = frozenset(
    "que si como cuando donde quien quienes cual cuales cuanto cuanta cuantos cuantas porque pa para".split())
_ABREN_CLAUSULA: Final[frozenset[str]] = frozenset({"y", "pero"})
_CONTEXTO_EXPRESIONES: Final[Mapping[str, tuple[frozenset[str] | None, frozenset[str] | None]]] = {
    # "no sé (qué/si/cómo…)", sí; "no se puede", "no se dio", "no se me ocurre" (pronombre "se"), no.
    "no se": (None, _INTERROGATIVOS | {"nada", "bien", "ni", "mucho", "nunca"}),
    # "a ver, ¿qué pasó?" abre la cláusula; "voy a ver la serie", "fui a ver el partido", no.
    "a ver": (_ABREN_CLAUSULA, None),
    "vamos a ver": (_ABREN_CLAUSULA, _INTERROGATIVOS),
    # "de una" (claro, enseguida) cierra la cláusula o va antes de un verbo; "de una amiga", no.
    "de una": (None, frozenset({"me", "te", "se", "nos", "le", "les", "lo", "y", "vamos", "voy"})),
    # "¿qué más?" (saludo), sí; "lo que más me gusta", "qué más quieres", no.
    "que mas": (_ABREN_CLAUSULA | {"hola", "buenas", "buenos", "ey"}, frozenset({"como", "y"})),
    # "es que…", "la verdad…" al abrir; "lo malo es que", "dime la verdad", no.
    "es que": (_ABREN_CLAUSULA | {"no", "si", "que"}, None),
    "la verdad": (_ABREN_CLAUSULA | {"no", "si", "que"}, None),
    # Respuestas que cierran la cláusula ("así es", "eso sí que no"); "así es la vida", no.
    "asi es": (None, frozenset()),
    "eso es": (None, frozenset()),
    "eso si": (None, frozenset({"que"})),
    "ahi vamos": (None, frozenset()),
    "con todo": (None, frozenset()),
}
# Formas con tilde que deshacen la ambigüedad: "no sé" siempre es la muletilla.
_EXPRESIONES_CON_TILDE: Final[Mapping[str, tuple[str, ...]]] = {"no se": ("no", "sé")}

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
    """La risa canónica de :data:`RISAS` si ``token`` lo es (sin mirar tildes); si no, ``token``."""
    base = _sin_tildes(token)
    for patron, canonico in _RISAS_RE:
        if patron.fullmatch(base):
            return canonico
    return token


# --- Teclazos (keysmash) ---------------------------------------------------
# Una palabra es "pronunciable" si se puede partir en sílabas (ataque + núcleo + coda) con
# grupos consonánticos del español o del inglés de uso corriente (para no tomar "whatsapp",
# "flash", "snacks" o "graph" por teclazos). "kakfkak" no lo es: ni "kf" ni "fk" son coda o
# ataque posibles. El silabeo es una programación dinámica por posición (O(n·k), sin la
# vuelta atrás exponencial de una regex con cuantificadores anidados).
_ATAQUES: Final[frozenset[str]] = frozenset("""
scr skr spr spl squ str shr thr chr sch bl br cl cr fl fr gl gr kl kr pl pr dr tr dw gw kw tw
ch ll rr qu gu th sh ph wh wr kn gn ps ts tz tl kh gh dh bh zh dj sc sk sm sn sp st sw sl
b c d f g h j k l m n p q r s t v w x y z
""".split())
_CODAS: Final[frozenset[str]] = frozenset([
    *"ght tch nch rch nds nts nks rts rds rks sts rst rld ngth".split(),
    *"ck ch sh th kh ph gn ng nk mp mb ft pt ct kt st sk sp xt pp ss ll ff tt zz cc dd gg bb mm nn rr".split(),
    *(a + b for a in "lnr" for b in "bcdfgklmnpstvz"),               # "ld", "lk", "nt", "rk"…
    *(a + b for a in "lnr" for b in ("sh", "ch", "th", "ph")),        # "harsh", "health", "morph"
    *(a + "s" for a in "bcdfgkmpt"),
    *"bcdfghjklmnprstxz",
])
_LARGOS_ATAQUE: Final = tuple(sorted({len(a) for a in _ATAQUES}))
_LARGOS_CODA: Final = tuple(sorted({len(c) for c in _CODAS}))
_VOCALES: Final[frozenset[str]] = frozenset("aeiou")
_VOCALES_FUERA_DE_FILA: Final[frozenset[str]] = frozenset("eiou")   # la fila central solo tiene la "a"
_FILA_CENTRAL: Final[frozenset[str]] = frozenset("asdfghjkl")       # donde caen los dedos al reír
_FILAS_SEGUIDAS: Final = ("asdfghjkl", "lkjhgfdsa")                  # "asdf", "jkl", "lkjh"…
_RE_TECLAZO_CORTO: Final = re.compile(r"([sdfghjkl])(?!\1)([sdfghjkl])\1\2")   # "ksks", "kdkd"
_ABREVIATURAS: Final[frozenset[str]] = frozenset("""
slds sldos nmms ntp tqm tkm pls plz msj msjs bss dnd xq pq tmb tbn bn hdp alv dlv ptm omg wtf lol
lmao lmfao btw idk brb nvm thx pfvr xfa xfavor grax grx kpsa qhubo
""".split())


def _finales_de_silaba(palabra: str, inicio: int) -> set[int]:
    """Posiciones donde puede acabar una sílaba (ataque? + núcleo + coda?) que empieza en ``inicio``.

    Núcleo: una o más vocales con una ``w``/``y`` final opcional ("aw", "ay"), o una ``y`` sola.
    Al final de la palabra se admite una ``s`` de plural tras la coda ("snacks", "calls", "fields").
    """
    n = len(palabra)
    finales: set[int] = set()
    ataques = [inicio] + [inicio + k for k in _LARGOS_ATAQUE
                          if inicio + k <= n and palabra[inicio:inicio + k] in _ATAQUES]
    for tras_ataque in ataques:
        nucleos: list[int] = []
        if tras_ataque < n and palabra[tras_ataque] == "y":
            nucleos.append(tras_ataque + 1)
        posicion = tras_ataque
        while posicion < n and palabra[posicion] in _VOCALES:
            posicion += 1
            nucleos.append(posicion)
            if posicion < n and palabra[posicion] in "wy":
                nucleos.append(posicion + 1)
        for tras_nucleo in nucleos:
            finales.add(tras_nucleo)
            for k in _LARGOS_CODA:
                if tras_nucleo + k <= n and palabra[tras_nucleo:tras_nucleo + k] in _CODAS:
                    finales.add(tras_nucleo + k)
                    if tras_nucleo + k == n - 1 and palabra[-1] == "s":
                        finales.add(n)
    return finales


def _silabeable(palabra: str) -> bool:
    """``True`` si ``palabra`` se puede partir entera en sílabas (programación dinámica de derecha
    a izquierda: ``alcanzable[i]`` dice si ``palabra[i:]`` se puede silabear)."""
    n = len(palabra)
    alcanzable = [False] * (n + 1)
    alcanzable[n] = True
    for i in range(n - 1, -1, -1):
        alcanzable[i] = any(alcanzable[fin] for fin in _finales_de_silaba(palabra, i))
    return alcanzable[0]


@functools.lru_cache(maxsize=16384)
def _es_teclazo(token: str) -> bool:
    """``True`` si ``token`` (normalizado) es un teclazo: risa de letras sin estructura silábica.

    Exige ≥ 5 letras (o 4 en patrón alterno, "ksks", o seguidas en la fila central, "asdf"),
    ninguna vocal distinta de la "a" (la única de la fila central: "gmail", "skills" o "fields"
    son palabras), mayoría de letras de la fila central del teclado (o muy pocas letras
    distintas, como "wkwkwk") y que no se pueda silabear. Así "kakfkak", "asdfgh" o "skjsks"
    son teclazos, pero "salsa", "kafka", "flash", "whatsapp", "snacks" o "calls" no.
    """
    n = len(token)
    if n < 4 or not token.isascii() or not token.isalpha() or token in _ABREVIATURAS:
        return False
    if _VOCALES_FUERA_DE_FILA.intersection(token):
        return False
    if n == 4:
        return (_RE_TECLAZO_CORTO.fullmatch(token) is not None
                or any(token in fila for fila in _FILAS_SEGUIDAS))
    muestra = token[:40]
    fila = sum(c in _FILA_CENTRAL for c in muestra) / len(muestra)
    if fila < 0.6 and not (n >= 6 and len(set(muestra)) / len(muestra) <= 0.4):
        return False
    return not _silabeable(muestra)


def _sin_alargar(token: str, doble_final: bool) -> str:
    """``token`` sin alargamientos: cada racha de 3 o más letras queda en una ("peeero" → "pero")
    y, con ``doble_final``, también la letra doble del final ("sii" → "si"). Una doble letra en
    mitad de palabra no es alargamiento ("valle", "pillas", "ssh")."""
    base = _RE_RACHA_LARGA.sub(r"\1", token)
    if doble_final and len(base) >= 2 and base[-1] == base[-2]:
        base = base[:-1]
    return base


@functools.lru_cache(maxsize=16384)
def _clave_base(token: str) -> str:
    """Clave de muletilla de un token normalizado (minúsculas, sin tildes).

    - Risas → su forma canónica ("jajajaj" → "jaja", "hahaha" → "haha").
    - Teclazos → una clave común (todas las variantes cuentan juntas).
    - Dudas ("hmm", "mmm", "ehm", "ammm") → ``"hm"``.
    - Marcadores (con o sin alargar: "buenooo") → su forma de :data:`MARCADORES_ES`.
    - Palabras funcionales o marcadores ambiguos alargados ("siii", "nooo", "esteee") → base + ``"~"``.
      Solo es alargamiento una racha de 3 letras o, en :data:`_ALARGABLES_CORTOS`, la letra final
      doblada ("sii", "estee"); "valle", "pillas" o "ssh" son palabras, no "vale"/"pilas"/"sh".
    - Cualquier otra palabra → ella misma.
    """
    if len(token) < 2:
        return token
    for patron, canonico in _RISAS_RE + _RISAS_EXTRA_RE:
        if patron.fullmatch(token):
            return canonico
    colapsado = _RE_RACHA.sub(r"\1", token)
    alargado = colapsado != token
    if colapsado in _ALIAS_DUDA or (alargado and colapsado in _ALIAS_DUDA_ALARGADA):
        return "hm"
    if colapsado in MARCADORES_ES:
        return colapsado
    if (alargado and (colapsado in STOPWORDS_ES or colapsado in _ALARGABLES_CORTOS)
            and _sin_alargar(token, colapsado in _ALARGABLES_CORTOS) == colapsado):
        return colapsado + "~"
    if _es_teclazo(token):
        return _CLAVE_TECLAZO
    return token


def _clase_clave(clave: str) -> str:
    """Clase de una clave: ``T`` teclazo, ``R`` risa, ``A`` alargamiento, ``M`` marcador,
    ``m`` marcador ambiguo, ``F`` palabra funcional, ``C`` contenido (o ambiguo fuera de contexto)."""
    if clave == _CLAVE_TECLAZO:
        return "T"
    if clave in RISAS or clave in _RISAS_EXTRA:
        return "R"
    if clave.endswith("~"):
        return "A"
    if clave in MARCADORES_ES:
        return "M"
    if clave in _MARCADORES_AMBIGUOS:
        return "m"
    if clave in STOPWORDS_ES:
        return "F"
    return "C"


def _es_muletilla_lexica(token: str) -> bool:
    """``True`` si el token normalizado es risa, teclazo, alargamiento o marcador inequívoco."""
    return _clase_clave(_clave_base(token)) in ("T", "R", "A", "M")


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
    except (TypeError, ValueError, OverflowError):   # OverflowError: un entero enorme en un perfil roto
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


def _tiene_digito(token: str) -> bool:
    return any(c.isdigit() for c in token)


def _es_marcador_o_risa(clave: str) -> bool:
    """Risa, teclazo, alargamiento o marcador (también ambiguo): vecino neutro de una muletilla."""
    return _clase_clave(clave) in ("T", "R", "A", "M", "m")


def _marcador_en_contexto(claves: Sequence[str], i: int, siguiente: str | None) -> bool:
    """¿Funciona como muletilla el marcador ``claves[i]`` de esta cláusula?

    - Nunca en uso nominal: tras un artículo o una preposición ("el ups", "lo bueno", "por el
      pos") o tras un número ("10 mm").
    - Los ambiguos ("este", "tipo", "ya"…), solo sueltos en su cláusula (junto a otros
      marcadores, risas o a sí mismos) o, "entonces"/"igual"/"total", abriéndola sin una
      comparación o cantidad detrás. Si el siguiente token del mensaje (aunque esté tras una
      coma o dos puntos) es un número, "igual"/"total" son cantidad: "total: 50 mil".
    """
    clave = claves[i]
    if i > 0 and (_tiene_digito(claves[i - 1]) or (
            claves[i - 1] in _ANTES_DE_USO_NOMINAL
            and not any(" ".join(claves[i - k:i]) in EXPRESIONES_COLOQUIALES for k in (2, 3) if i >= k))):
        return False                     # "el ups", "10 mm"; pero "de una parce" ("una" no es artículo)
    if clave not in _MARCADORES_AMBIGUOS:
        return True
    if clave in _MARCADORES_DE_INICIO and siguiente is not None and _tiene_digito(siguiente):
        return False
    if (i == 0 and clave in _MARCADORES_DE_INICIO
            and (len(claves) == 1 or claves[1] not in _NO_SIGUEN_AL_MARCADOR_DE_INICIO.get(clave, ()))):
        return True
    return all(otra == clave or _clase_clave(otra) in ("T", "R", "A", "M")
               for j, otra in enumerate(claves) if j != i)


def _clausulas_muletilla(texto: str, excluir: frozenset[str] | set[str]) -> list[list[tuple[str, str, bool]]]:
    """Cláusulas del mensaje, cada una con ``(superficie, clave, en_contexto)`` por token.

    ``superficie`` es el token en minúsculas (con la risa normalizada); ``clave``, la de
    :func:`_clave_base` (los tokens de ``excluir`` y los que llevan cifras se quedan tal cual:
    "ps5" nunca es "ps"); ``en_contexto`` dice si un marcador funciona aquí como muletilla (ver
    :func:`_marcador_en_contexto`). Para risas, alargamientos y palabras corrientes es ``True``.
    """
    limpio = _texto_tokenizable(str(texto)).lower()
    crudas = [[_normalizar_risa(t) for t in _RE_TOKEN_MULETILLA.findall(c)]
              for c in _RE_CORTE_CLAUSULA.split(limpio)]
    crudas = [c for c in crudas if c]
    todas = [t for c in crudas for t in c]
    salida: list[list[tuple[str, str, bool]]] = []
    posicion = 0                                 # índice global del primer token de la cláusula
    for superficies in crudas:
        claves: list[str] = []
        for superficie in superficies:
            normal = _sin_tildes(superficie)
            claves.append(normal if normal in excluir or _tiene_digito(normal) else _clave_base(normal))
        clausula: list[tuple[str, str, bool]] = []
        for i, (superficie, clave) in enumerate(zip(superficies, claves)):
            contexto = True
            if _clase_clave(clave) in ("M", "m"):
                siguiente = todas[posicion + i + 1] if posicion + i + 1 < len(todas) else None
                contexto = _marcador_en_contexto(claves, i, siguiente)
            clausula.append((superficie, clave, contexto))
        salida.append(clausula)
        posicion += len(superficies)
    return salida


def _expresion_en_contexto(expresion: str, clausula: Sequence[tuple[str, str, bool]], i: int, n: int) -> bool:
    """¿Es muletilla la expresión que ocupa ``clausula[i:i+n]``? Ver :data:`_CONTEXTO_EXPRESIONES`.

    Un vecino vale si es borde de cláusula, una palabra admitida por la regla o un marcador,
    risa o alargamiento **en su contexto** ("lo bueno es que" no abre "es que": ahí "bueno" es
    adjetivo).
    """
    regla = _CONTEXTO_EXPRESIONES.get(expresion)
    if regla is None or tuple(t[0] for t in clausula[i:i + n]) == _EXPRESIONES_CON_TILDE.get(expresion):
        return True
    antes, despues = regla
    for posicion, admitidas in ((i - 1, antes), (i + n, despues)):
        if admitidas is None or not 0 <= posicion < len(clausula):
            continue
        _, vecino, contexto = clausula[posicion]
        if vecino not in admitidas and not (contexto and _es_marcador_o_risa(vecino)):
            return False
    return True


def _contar_ngramas(textos: Sequence[str], excluir: frozenset[str] | set[str] = frozenset(), *,
                    con_formas: bool = True) -> tuple[Counter[tuple[str, ...]], dict[tuple[str, ...], Counter[str]]]:
    """Conteo de n-gramas (1–3) por clave de muletilla y, si se piden, sus formas de superficie.

    Los n-gramas no cruzan fronteras de cláusula (``.,;:!?``…): "bueno, ya llegué" no forma
    "bueno ya". Un marcador fuera de contexto lleva la clave ``"=palabra"`` (nunca admisible),
    para que "este precio" no sume a la muletilla "este". Las :data:`EXPRESIONES_COLOQUIALES`
    ("la verdad", "y nada") se cuentan con sus claves normales cuando están en su contexto (ver
    :data:`_CONTEXTO_EXPRESIONES`); fuera de él ("no se puede"), con ``"="`` delante.
    """
    conteo: Counter[tuple[str, ...]] = Counter()
    formas: dict[tuple[str, ...], Counter[str]] = {}
    for texto in textos:
        for clausula in _clausulas_muletilla(texto, excluir):
            superficies = [t[0] for t in clausula]
            claves = [t[1] for t in clausula]
            en_contexto = [clave if contexto else "=" + clave for _, clave, contexto in clausula]
            for n in (1, 2, 3):
                for i in range(len(clausula) - n + 1):
                    plana = tuple(claves[i:i + n])
                    expresion = " ".join(plana) if n > 1 else ""
                    if expresion in EXPRESIONES_COLOQUIALES:
                        clave_n = (plana if _expresion_en_contexto(expresion, clausula, i, n)
                                   else ("=" + plana[0], *plana[1:]))
                    else:
                        clave_n = tuple(en_contexto[i:i + n])
                    conteo[clave_n] += 1
                    if con_formas:
                        formas.setdefault(clave_n, Counter())[" ".join(superficies[i:i + n])] += 1
    return conteo, formas


def _ngrama_admisible(clave: tuple[str, ...], excluir: frozenset[str] | set[str]) -> bool:
    """¿Puede ser muletilla este n-grama? Solo marcadores discursivos, interjecciones o risas.

    - Nunca con un token de ``excluir`` o con dígitos.
    - Unigramas: risa, teclazo, alargamiento o marcador (los ambiguos, solo en contexto).
    - n-gramas (2–3): las :data:`EXPRESIONES_COLOQUIALES`; la repetición de un marcador
      ("bueno bueno", "ya ya", "sí sí"); o solo marcadores, alargamientos y risas con al menos
      un marcador o alargamiento inequívoco ("bueno pues", "ah ok"). Nunca con palabras de
      contenido ("estaba ocupado"), solo con funcionales ("de la") ni con teclazos.
    """
    if any(t in excluir or any(c.isdigit() for c in t) for t in clave):
        return False
    if len(clave) == 1:
        return len(clave[0]) >= 2 and _clase_clave(clave[0]) in ("T", "R", "A", "M", "m")
    if " ".join(clave) in EXPRESIONES_COLOQUIALES:
        return True
    if any(len(t) < 2 for t in clave):
        return False
    clases = [_clase_clave(t) for t in clave]
    if "T" in clases or all(c == "R" for c in clases):
        return False
    if len(set(clave)) == 1:
        return clases[0] in ("A", "M", "m") or clave[0] in _REPETIBLES
    return all(c in ("R", "A", "M", "m") for c in clases) and any(c in ("A", "M") for c in clases)


def _etiqueta_teclazo(variantes: Counter[str] | None) -> str:
    """``"risa de teclazo (kakfkak)"`` con la variante más frecuente si se repite (privacidad:
    una cadena rara escrita una sola vez podría ser una clave o un código); si no, sin ejemplo."""
    if variantes:
        ejemplo, veces = variantes.most_common(1)[0]
        ejemplo = _sin_tildes(ejemplo)[:_MAX_EJEMPLO_TECLAZO]
        if veces >= _MIN_VECES_EJEMPLO_TECLAZO and ejemplo.isascii() and ejemplo.isalpha():
            return f"{ETIQUETA_TECLAZO} ({ejemplo})"
    return ETIQUETA_TECLAZO


def extraer_muletillas(propios: Sequence[str], ajenos: Sequence[str], *, excluir: Iterable[str] = (),
                       top: int = 25, min_freq: int = 5) -> list[dict[str, Any]]:
    """Muletillas propias: n-gramas (1–3) frecuentes con *lift* ≥ 1,5 frente a los mensajes ajenos.

    Solo cuentan marcadores discursivos, interjecciones, rellenos, alargamientos y risas (ver
    :func:`_ngrama_admisible`); las palabras de contenido quedan para ``temas``. Las variantes
    de un mismo marcador se agrupan ("hmm"/"mmm", "bueno"/"buenooo") y la superficie es la más
    usada; todos los teclazos forman una sola entrada (``"risa de teclazo (kakfkak)"``) con la
    suma de sus frecuencias.

    ``lift = ((c_propio+1)/(N_propio+V)) / ((c_ajeno+1)/(N_ajeno+V))``. Orden por
    ``freq·log(1+lift)``. Sin mensajes ajenos, ``lift = 1.0`` y se ordena por frecuencia.
    Devuelve ``[{"ngrama", "freq", "lift"}]``.
    """
    excluidos = {_sin_tildes(str(e)) for e in excluir if str(e).strip()}
    c_propio, formas = _contar_ngramas(list(propios), excluidos)
    hay_ajenos = any(str(t).strip() for t in ajenos)
    c_ajeno, _ = _contar_ngramas(list(ajenos), excluidos, con_formas=False) if hay_ajenos else (Counter(), {})
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
        if clave == (_CLAVE_TECLAZO,):
            superficie = _etiqueta_teclazo(formas.get(clave))
        else:
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


# Muletillas que también pueden ser nombres, apellidos o apodos ("Melo", "Jojo"): al abrir
# mensaje con mayúscula siguen pasando por la detección de nombres (privacidad, §9.6).
_MULETILLAS_O_NOMBRES: Final[frozenset[str]] = frozenset({"melo", "nel", "ole", "severo", "jojo", "juju",
                                                          "jiji"})

# Palabras comunes que suelen abrir mensajes (el teclado del móvil las pone con mayúscula): nunca
# se toman por nombres aunque casi no aparezcan en mitad de frase.
_COMUNES_AL_INICIO: Final[frozenset[str]] = (
    STOPWORDS_ES | INTERJECCIONES | _NO_TEMAS | frozenset(RISAS)
    | frozenset(t for frase in SALUDOS_BASE + DESPEDIDAS_BASE for t in _sin_tildes(frase).split())
    | frozenset({"oye", "mira", "porfa", "perdon", "obvio", "exacto", "sisas", "nono", "amor", "bro",
                 "mano", "hermano", "hermana", "pana", "socio", "venga", "espera", "jefe", "okey"})
    | ((MARCADORES_ES | _MARCADORES_AMBIGUOS) - _MULETILLAS_O_NOMBRES))


def _es_comun_al_inicio(clave: str) -> bool:
    """Palabra común al abrir mensaje: las de :data:`_COMUNES_AL_INICIO` y las muletillas
    léxicas ("Buenooo", "Hmmm", "Hahaha"), salvo las que pueden ser nombres ("Melo", "Jojo").
    De los teclazos solo los de risa (≤ 4 letras distintas, como "Kakfkak"): uno más variado
    sigue pasando por la detección de nombres."""
    if clave in _COMUNES_AL_INICIO:
        return True
    if clave in _MULETILLAS_O_NOMBRES or not _es_muletilla_lexica(clave):
        return False
    return _clave_base(clave) != _CLAVE_TECLAZO or len(set(clave)) <= 4


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
        if n < 2 or _es_comun_al_inicio(clave) or mayus[clave] / n <= 0.4:
            continue
        minus = minuscula_en_medio[clave]
        if minus >= 2 and minus / en_medio[clave] >= 0.6:
            continue                     # palabra corriente que solo va con mayúscula al abrir frase
        sospechosos.add(clave)
    return sospechosos


def _tokens_de_nombre(nombre: str) -> set[str]:
    return {_sin_tildes(t) for t in _RE_NOMBRE_AUTOR.findall(nombre)}


def _temas(textos: Sequence[str], excluir: frozenset[str] | set[str]) -> list[dict[str, Any]]:
    """Los 15 unigramas propios más frecuentes (≥ 4 letras) escritos en minúscula ≥ 60 % de las veces.

    Las muletillas léxicas (marcadores, risas, alargamientos y teclazos) nunca son temas.
    """
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
                or clave in INTERJECCIONES or clave in _NO_TEMAS or clave in excluir
                or _es_muletilla_lexica(clave)):
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
    teclazos = 0
    for texto in textos_propios:
        for token in _tokens(texto):
            if token in RISAS:
                risa[token] += 1
            else:
                normal = _sin_tildes(token)
                if normal not in excluir_f and _clave_base(normal) == _CLAVE_TECLAZO:
                    teclazos += 1

    histograma = [0] * 24
    for mensaje in propios:
        histograma[mensaje.ts.hour] += 1
    maximo = max(histograma)
    hora_pico = histograma.index(maximo) if maximo else 0

    saludos, despedidas = _saludos_y_despedidas(propios)
    muletillas = [m for m in extraer_muletillas(textos_propios, textos_ajenos, excluir=excluir_f)
                  if _es_texto_seguro(m["ngrama"], excluir_f)]
    # Una palabra que ya forma parte de una muletilla ("la verdad", "tipo") no es un tema.
    en_muletillas = {_sin_tildes(t) for m in muletillas if not _RE_ETIQUETA_TECLAZO.fullmatch(m["ngrama"])
                     for t in _RE_TOKEN.findall(m["ngrama"].lower())}
    temas = [t for t in _temas(textos_propios, excluir_f | en_muletillas)
             if _es_texto_seguro(t["palabra"], excluir_f)]
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
            CLAVE_RISA_TECLAZO: teclazos,
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
    _migrar_teclazo(datos)
    return datos


def _migrar_teclazo(perfil: dict[str, Any]) -> None:
    """Mueve ``estilo.risa["teclazo"]`` (perfiles de una versión anterior) a ``estilo["risa_teclazo"]``.

    Así ningún consumidor que tome la clave mayor de ``estilo.risa`` como la risa más habitual
    (``memory_engine.hechos_desde_perfil``) afirma que el usuario escribe «teclazo».
    """
    estilo = perfil.get("estilo")
    risa = estilo.get("risa") if isinstance(estilo, dict) else None
    if not isinstance(risa, dict) or _CLAVE_TECLAZO_EN_RISA not in risa:
        return
    antiguo = risa.pop(_CLAVE_TECLAZO_EN_RISA)
    if CLAVE_RISA_TECLAZO not in estilo:
        estilo[CLAVE_RISA_TECLAZO] = antiguo
    log.debug("Perfil heredado: conteo de teclazos movido fuera de estilo.risa")


def _item(valor: Any, maximo: int = 40) -> str:
    """Cadena del perfil saneada para el prompt (sin control, comillas angulares ni datos de contacto)."""
    if not isinstance(valor, str):
        return ""
    texto = _RE_CONTROL.sub("", valor).replace("«", "").replace("»", "").replace("<", "").replace(">", "")
    texto = _RE_ESPACIOS.sub(" ", texto).strip()
    # Los separadores entre dígitos no esconden un teléfono ni una cédula (300-123-4567, 1.036.123.456).
    if (not texto or _RE_DIGITOS_LARGOS.search(_RE_SEPARADOR_DIGITOS.sub("", texto)) or "@" in texto
            or _RE_URL.search(texto)):
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


def _frase_risa(estilo: Mapping[str, Any], muletillas: Any) -> tuple[str, set[str]]:
    """Frase de risa del resumen y las muletillas (normalizadas) que ya cubre.

    ``"Ríe con «jaja» y a teclazos («kakfkak»)."``; si los teclazos son más que la risa
    principal, van primero. El conteo de teclazos es ``estilo["risa_teclazo"]`` (o, en
    perfiles de una versión anterior, ``estilo.risa["teclazo"]``) y el ejemplo sale de la
    entrada agrupada de ``muletillas`` (``"risa de teclazo (kakfkak)"``). Los perfiles sin
    teclazos siguen funcionando como antes.
    """
    risa = estilo.get("risa") if isinstance(estilo.get("risa"), Mapping) else {}
    conteos = [(k, _real(v) or 0.0) for k, v in risa.items()
               if isinstance(k, str) and k != _CLAVE_TECLAZO_EN_RISA and _item(k)]
    conteos = [par for par in conteos if par[1] > 0]
    principal = max(conteos, key=lambda par: par[1]) if conteos else None
    teclazos = _real(estilo.get(CLAVE_RISA_TECLAZO))
    if teclazos is None:
        teclazos = _real(risa.get(_CLAVE_TECLAZO_EN_RISA))
    teclazos = teclazos or 0.0
    cubiertas: set[str] = set()
    con_risa = f"con «{_item(principal[0])}»" if principal else ""
    if principal:
        cubiertas.add(_sin_tildes(_item(principal[0])))
    a_teclazos = ""
    if teclazos >= _MIN_TECLAZOS_RESUMEN:
        ejemplo = ""
        for muletilla in _items(muletillas, "ngrama", 25):
            coincidencia = _RE_ETIQUETA_TECLAZO.fullmatch(muletilla)
            if coincidencia:
                cubiertas.add(_sin_tildes(muletilla))
                ejemplo = ejemplo or (coincidencia.group("ejemplo") or "")
        a_teclazos = f"a teclazos («{ejemplo}»)" if ejemplo else "a teclazos"
    if con_risa and a_teclazos:
        primero, segundo = ((a_teclazos, con_risa) if principal and teclazos > principal[1]
                            else (con_risa, a_teclazos))
        return f"Ríe {primero} y {segundo}.", cubiertas
    if con_risa or a_teclazos:
        return f"Ríe {con_risa or a_teclazos}.", cubiertas
    return "", set()


def resumen_perfil(perfil: Mapping[str, Any], max_chars: int = 600) -> str:
    """Resumen en español del perfil (≤ ``max_chars``) para ``<perfil_usuario>`` del system prompt.

    Ejemplo: ``"Escribe en ráfagas cortas (~7 palabras; 2,4 mensajes por ráfaga). Muletillas:
    «de una», «parce», «bueno bueno». Ríe con «jaja» y a teclazos («kakfkak»). Más activo hacia
    las 22 h (noche). Emojis en el 18 % de sus mensajes. Temas: código, música, fútbol."``

    La risa va en su propia frase, justo detrás de las muletillas; las risas que esa frase ya
    menciona (la principal y el teclazo agrupado) no se repiten en la lista de muletillas.
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

    frase_risa, cubiertas = _frase_risa(estilo, perfil.get("muletillas"))
    muletillas = [m for m in _items(perfil.get("muletillas"), "ngrama", 25)
                  if _sin_tildes(m) not in cubiertas][:5]
    if muletillas:
        frases.append("Muletillas: " + ", ".join(f"«{m}»" for m in muletillas) + ".")
    if frase_risa:
        frases.append(frase_risa)

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
