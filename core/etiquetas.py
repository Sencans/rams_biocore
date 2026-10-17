"""Gramática ``[EXP][MOTION][TONE]`` de Amadeus: regex, parser, sugerencias y tablas de voz.

Qué hace (§4.4 y Anexo E):
- Define las expresiones regulares de las etiquetas (único lugar del proyecto con ellas).
- ``sugerir_etiquetas``: expresión base decidida por código a partir del estado homeostático
  y del evento social (sin LLM, §8).
- ``validar_contra_estado`` y ``parsear_respuesta``: corrigen en local lo que escriba el LLM
  (nunca se reintenta contra el LLM por formato).
- ``recortar_segmentos`` y las funciones ``limpiar_*``: límites por modo de salida y
  limpieza para terminal, voz y Telegram.
- Tablas de voz (TONE → tags de audio y ajustes de ElevenLabs, prosodia de edge-tts).

Dependencias: solo la biblioteca estándar y ``core.contracts``. Sin E/S: no se degrada.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Final

from core.contracts import (
    EXP_VALIDOS,
    LANG_VALIDOS,
    MOTION_VALIDOS,
    TONE_VALIDOS,
    Etiquetas,
    HomeostaticState,
    Segmento,
)

__all__ = [
    "TAG_TRIPLE_RE", "TAG_SUELTA_RE", "TAG_AUDIO_RE",
    "PROTOTIPOS_EXP", "EXP_SOCIALES",
    "MODELOS_CON_TAGS", "TAG_AUDIO_TONE", "TAGS_V3_VERIFICADOS", "TAGS_AUDIO_CONOCIDOS",
    "PALABRAS_TAGS_AUDIO", "AJUSTES_VOZ", "PROSODIA_EDGE",
    "TIPOS_EVENTO_SOCIAL", "DETALLES_APODO", "LIMITES_SALIDA",
    "tipo_evento", "normalizar_valor", "es_valida", "distancia_a_prototipo",
    "sugerir_etiquetas", "validar_contra_estado", "parsear_respuesta", "recortar_segmentos",
    "limpiar_texto", "limpiar_para_voz", "limpiar_para_telegram",
    "tag_audio", "ajustes_voz", "prosodia_edge",
]

# ---------------------------------------------------------------------------
# §4.4.1 Expresiones regulares (exactas)
# ---------------------------------------------------------------------------

TAG_TRIPLE_RE = re.compile(
    r"\[\s*EXP\s*:\s*(?P<exp>[A-Za-z_]+)\s*\]\s*"
    r"\[\s*MOTION\s*:\s*(?P<motion>[A-Za-z_]+)\s*\]\s*"
    r"\[\s*TONE\s*:\s*(?P<tone>[A-Za-z_]+)\s*\]"
)
TAG_SUELTA_RE = re.compile(
    r"\[\s*(?P<tipo>EXP|MOTION|TONE|LANG|INTENT)\s*:\s*(?P<valor>[^\]\n]{0,40})\]",
    re.IGNORECASE,
)
TAG_AUDIO_RE = re.compile(r"\[(?P<contenido>[A-Za-z][A-Za-z ,'\-]{0,38})\]")   # tags estilo ElevenLabs escritos por error

# ---------------------------------------------------------------------------
# §4.4.2 Vocabulario y prototipos
# ---------------------------------------------------------------------------

PROTOTIPOS_EXP: Final[dict[str, tuple[float, float, float, float]]] = {
    "neutral": (0.40, 0.50, 0.30, 0.30), "smug": (0.60, 0.60, 0.30, 0.20),
    "sonrojo_tsundere": (0.80, 0.40, 0.10, 0.20), "molesta": (0.70, 0.50, 0.30, 0.40),
    "pensativa": (0.40, 0.80, 0.10, 0.30), "sorprendida": (0.90, 0.30, 0.10, 0.10),
    "triste": (0.30, 0.40, 0.40, 0.50), "feliz": (0.70, 0.50, 0.10, 0.20),
    "avergonzada": (0.85, 0.30, 0.10, 0.20), "seria": (0.50, 0.90, 0.10, 0.30),
    "curiosa": (0.65, 0.60, 0.50, 0.20), "aburrida": (0.20, 0.20, 0.90, 0.40),
    "somnolienta": (0.10, 0.20, 0.40, 0.90),
}
EXP_SOCIALES: Final = frozenset({"sonrojo_tsundere", "feliz", "avergonzada"})   # solo con evento social

# §4.4.4 Eventos sociales ("<tipo>" o "<tipo>:<detalle>", los produce core.intenciones).
TIPOS_EVENTO_SOCIAL: Final = ("apodo", "cumplido", "agradecimiento", "acusacion_tsundere", "regreso",
                              "error_propio", "tristeza", "saludo", "despedida")
DETALLES_APODO: Final = ("christina", "asistente", "zombi", "celeb17", "chuunibyou", "otro")

_TIPOS_TSUNDERE: Final = frozenset({"cumplido", "apodo", "agradecimiento", "acusacion_tsundere"})
_TONOS_FATIGA: Final = frozenset({"somnoliento", "calido", "serio"})

# ---------------------------------------------------------------------------
# Anexo E. Tablas de voz
# ---------------------------------------------------------------------------

MODELOS_CON_TAGS: Final = frozenset({"eleven_v4", "eleven_v4_turbo", "eleven_v3", "eleven_v3_conversational"})
TAG_AUDIO_TONE: Final = {
    "sarcastico": "[sarcastic]", "analitico": "[thoughtful]", "calido": "[warmly]",
    "nervioso": "[nervous]", "serio": "[professional]", "jugueton": "[mischievously]",
    "somnoliento": "[sighs]",
}
TAGS_V3_VERIFICADOS: Final = frozenset({"[sarcastic]", "[thoughtful]", "[mischievously]", "[sighs]", "[professional]",
                                        "[curious]", "[excited]", "[whispers]", "[laughs]", "[annoyed]",
                                        "[exhales]", "[surprised]"})
TAGS_AUDIO_CONOCIDOS: Final = frozenset(
    {t.strip("[]") for t in TAG_AUDIO_TONE.values()}
    | {t.strip("[]") for t in TAGS_V3_VERIFICADOS}
    | {"laughs harder", "starts laughing", "wheezing", "crying", "snorts", "dismissive",
       "short pause", "long pause", "warmly", "nervous"}
)
# Palabras inglesas típicas de los tags de audio: un corchete de 1–2 palabras, todas de esta
# lista, se considera un tag de audio escrito por error y se elimina (limpiar_texto, paso 2).
PALABRAS_TAGS_AUDIO: Final = frozenset(
    {palabra for tag in TAGS_AUDIO_CONOCIDOS for palabra in tag.split()}
    | {"laugh", "laughing", "chuckles", "chuckle", "giggles", "giggle", "gasps", "gasp", "sigh",
       "sighing", "whisper", "whispering", "shouting", "shouts", "softly", "quietly", "loudly",
       "sadly", "happily", "angrily", "excitedly", "sarcastically", "nervously", "cheerfully",
       "playfully", "curiously", "seriously", "thoughtfully", "clears", "throat", "pause",
       "sobbing", "sniffs", "groans", "yawns", "yawning", "hesitates", "stammers", "coughs",
       "mumbles", "inhales", "breathes", "deeply", "smirks", "scoffs", "hums", "humming", "gulps",
       "dramatically", "pauses", "sighs", "exhale", "inhale"}
)

AJUSTES_VOZ: Final[dict[str, dict[str, float]]] = {
    "sarcastico": {"stability": 0.45, "similarity_boost": 0.80, "style": 0.55, "speed": 1.00},
    "analitico": {"stability": 0.70, "similarity_boost": 0.85, "style": 0.20, "speed": 1.05},
    "calido": {"stability": 0.55, "similarity_boost": 0.80, "style": 0.40, "speed": 0.95},
    "nervioso": {"stability": 0.30, "similarity_boost": 0.75, "style": 0.60, "speed": 1.10},
    "serio": {"stability": 0.75, "similarity_boost": 0.85, "style": 0.15, "speed": 1.00},
    "jugueton": {"stability": 0.35, "similarity_boost": 0.75, "style": 0.65, "speed": 1.05},
    "somnoliento": {"stability": 0.60, "similarity_boost": 0.80, "style": 0.25, "speed": 0.88},
}
_ESTABILIDADES_DISCRETAS: Final = (0.0, 0.5, 1.0)

# (rate, pitch) en el formato exacto de edge-tts.
PROSODIA_EDGE: Final[dict[str, tuple[str, str]]] = {
    "sarcastico": ("+6%", "+0Hz"),
    "analitico": ("+8%", "+0Hz"),
    "calido": ("-2%", "-4Hz"),
    "nervioso": ("+12%", "+8Hz"),
    "serio": ("+0%", "-6Hz"),
    "jugueton": ("+8%", "+6Hz"),
    "somnoliento": ("-12%", "-8Hz"),
}

# Límites de recortar_segmentos por modo de salida (§4.4.3).
LIMITES_SALIDA: Final[dict[str, dict[str, int]]] = {
    "voz": {"oraciones": 2, "palabras": 45},
    "terminal": {"palabras": 120},
    "terminal_largo": {"palabras": 300},
    "telegram": {"lineas": 3, "caracteres": 600},
    "nota_voz": {"oraciones": 3, "palabras": 50},   # ~20 s de audio (nota de voz de Telegram)
}

# ---------------------------------------------------------------------------
# Regex auxiliares
# ---------------------------------------------------------------------------

_RE_BLOQUE_CODIGO: Final = re.compile(r"```.*?```", re.DOTALL)
_RE_CODIGO_EN_LINEA: Final = re.compile(r"`([^`\n]*)`")
_RE_ESPACIOS: Final = re.compile(r"\s+")
_RE_ESPACIOS_LINEA: Final = re.compile(r"[ \t\f\v]+")
# Marca interna que sustituye a cada etiqueta, tag de audio o emoji eliminado. Sirve para cerrar
# SOLO el hueco que deja esa eliminación ante la puntuación de cierre ("Hola [sighs]." → "Hola.")
# sin tocar los espacios que escribió el propio texto: ".env", "./run.sh", ".5" o "find . -name"
# se conservan tal cual (§4.4.3 solo pide colapsar espacios y aplicar strip). Nunca sale de este
# módulo: _cerrar_huecos la resuelve siempre (un NUL del propio texto se trata igual).
_MARCA: Final = "\x00"
_RE_HUECO_ANTE_PUNTUACION: Final = re.compile(r"[ \t\x00]*\x00(?=[,.;:!?…]+(?![^\s\x00]))")
_RE_NEGRITA: Final = re.compile(r"\*\*|__")
_RE_ASTERISCO: Final = re.compile(r"\*")
_RE_ENCABEZADO: Final = re.compile(r"^[ \t]*#+[ \t]*", re.MULTILINE)
_RE_CITA: Final = re.compile(r"^[ \t]*>+[ \t]*", re.MULTILINE)
_RE_VINETA: Final = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.MULTILINE)
_RE_ENLACE_MD: Final = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_RE_URL: Final = re.compile(r"https?://\S+")
_RE_EMOJI: Final = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_RE_SALTOS_MULTIPLES: Final = re.compile(r"\n{3,}")
_RE_FIN_ORACION: Final = re.compile(r"(?<=[.!?…])\s+")
_RE_FIN_ORACION_O_LINEA: Final = re.compile(r"(?<=[.!?…])\s+|\s*\n\s*")
_RE_TOKENS: Final = re.compile(r"\S+|\s+")


# ---------------------------------------------------------------------------
# §4.4.3 Funciones
# ---------------------------------------------------------------------------


def tipo_evento(evento_social: str | None) -> str | None:
    """Tipo de un evento social (``"apodo:christina"`` → ``"apodo"``), normalizado."""
    if not evento_social:
        return None
    tipo = str(evento_social).split(":")[0].strip().lower()
    return tipo or None


def normalizar_valor(valor: str) -> str:
    """Minúsculas, sin tildes (NFD sin categoría ``Mn``), espacios y ``-`` → ``_``, ``strip``."""
    texto = unicodedata.normalize("NFD", "" if valor is None else str(valor))
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = texto.strip().lower()
    return re.sub(r"[\s\-]+", "_", texto)


def es_valida(etq: Etiquetas) -> bool:
    """``True`` si las tres etiquetas están en el vocabulario cerrado."""
    return (getattr(etq, "exp", None) in EXP_VALIDOS
            and getattr(etq, "motion", None) in MOTION_VALIDOS
            and getattr(etq, "tone", None) in TONE_VALIDOS)


def distancia_a_prototipo(exp: str, estado: HomeostaticState) -> float:
    """Distancia euclídea 4D entre el prototipo de ``exp`` y ``estado.vector()``; desconocida ⇒ ``inf``."""
    prototipo = PROTOTIPOS_EXP.get(normalizar_valor(exp))
    if prototipo is None:
        return math.inf
    vector = estado.vector()
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(prototipo, vector)))


def sugerir_etiquetas(estado: HomeostaticState, *, alerta_critica: bool = False,
                      evento_social: str | None = None, tsundere_ok: bool = False) -> Etiquetas:
    """Expresión base decidida por código (§4.4.3). Gana la primera regla que aplique.

    Reglas: 1 alerta · 2 tristeza · 3 fatiga · 4 error propio · 5a/5b/5c tsundere, apodo y
    cumplidos · 6 regreso · 7 sobresalto · 8 foco · 9a/9b aburrimiento · 10 saludo/despedida ·
    11 ánimo · 12 por defecto.
    """
    if estado is None:
        estado = HomeostaticState.neutral()
    tipo = tipo_evento(evento_social)
    arousal, focus, boredom, fatiga = estado.vector()
    curiosidad, valencia = estado.curiosidad, estado.valencia

    if alerta_critica:                                                       # 1
        return Etiquetas("sorprendida" if arousal >= 0.8 else "seria", "senalar", "serio")
    if tipo == "tristeza":                                                   # 2
        return Etiquetas("triste", "asentir", "calido")
    if fatiga > 0.75:                                                        # 3
        return Etiquetas("somnolienta", "bostezar", "somnoliento")
    if tipo == "error_propio":                                               # 4
        return Etiquetas("avergonzada", "mirar_a_otro_lado", "nervioso")
    if tipo in _TIPOS_TSUNDERE and tsundere_ok:                              # 5a
        return Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso")
    if tipo in ("apodo", "acusacion_tsundere"):                              # 5b
        return Etiquetas("molesta", "brazos_cruzados", "sarcastico")
    if tipo in ("cumplido", "agradecimiento"):                               # 5c
        return Etiquetas("smug", "asentir", "calido")
    if tipo == "regreso":                                                    # 6
        return Etiquetas("feliz", "asentir", "calido")
    if arousal > 0.80 and focus < 0.40:                                      # 7
        return Etiquetas("sorprendida", "ladear_cabeza", "nervioso")
    if focus > 0.70:                                                         # 8
        return Etiquetas("seria" if arousal >= 0.5 else "pensativa", "asentir", "analitico")
    if boredom > 0.70 and curiosidad >= 0.5:                                 # 9a
        return Etiquetas("curiosa", "ladear_cabeza", "jugueton")
    if boredom > 0.70:                                                       # 9b
        return Etiquetas("aburrida", "estirarse", "sarcastico" if valencia < 0 else "jugueton")
    if tipo in ("saludo", "despedida"):                                      # 10
        return Etiquetas("neutral", "asentir", "calido")
    if valencia > 0.2 and arousal > 0.5:                                     # 11
        return Etiquetas("smug", "asentir", "sarcastico")
    return Etiquetas("neutral", "idle", "analitico")                         # 12


def validar_contra_estado(etq: Etiquetas, sugeridas: Etiquetas, estado: HomeostaticState, *,
                          evento_social: str | None = None, alerta_critica: bool = False,
                          tsundere_ok: bool = False, umbral: float = 0.55) -> tuple[Etiquetas, bool]:
    """Corrige las etiquetas del LLM contra el estado (§4.4.3). Devuelve ``(finales, corregido)``.

    1. Con alerta crítica manda ``sugeridas``.
    2. Valores fuera del vocabulario ⇒ los de ``sugeridas``.
    3. EXP social sin evento social ⇒ ``sugeridas.exp``.
    4. ``sonrojo_tsundere`` sin presupuesto ⇒ ``sugeridas.exp`` (o ``neutral``) y fuera ``nervioso``.
    5. EXP no social demasiado lejos de su prototipo ⇒ ``sugeridas.exp``.
    6. Fatiga alta ⇒ tono ``somnoliento`` salvo ``calido`` o ``serio``.
    """
    if estado is None:
        estado = HomeostaticState.neutral()
    if alerta_critica:                                                       # 1
        return sugeridas, etq != sugeridas

    exp = normalizar_valor(getattr(etq, "exp", ""))                          # 2
    motion = normalizar_valor(getattr(etq, "motion", ""))
    tone = normalizar_valor(getattr(etq, "tone", ""))
    if exp not in EXP_VALIDOS:
        exp = sugeridas.exp
    if motion not in MOTION_VALIDOS:
        motion = sugeridas.motion
    if tone not in TONE_VALIDOS:
        tone = sugeridas.tone

    if exp in EXP_SOCIALES and evento_social is None and exp != sugeridas.exp:   # 3
        exp = sugeridas.exp

    if exp == "sonrojo_tsundere" and not tsundere_ok:                        # 4
        exp = sugeridas.exp if sugeridas.exp != "sonrojo_tsundere" else "neutral"
        if tone == "nervioso":
            tone = sugeridas.tone

    if exp not in EXP_SOCIALES and distancia_a_prototipo(exp, estado) > umbral:  # 5
        exp = sugeridas.exp

    if estado.fatiga > 0.75 and tone not in _TONOS_FATIGA:                   # 6
        tone = "somnoliento"

    finales = Etiquetas(exp, motion, tone)
    return finales, finales != etq


def _valor_vocabulario(valor: str, validos: tuple[str, ...], defecto: str) -> tuple[str, bool]:
    """Valor normalizado si está en ``validos``; si no, ``defecto`` y ``True`` (corregido)."""
    normalizado = normalizar_valor(valor)
    if normalizado in validos:
        return normalizado, False
    return defecto, True


def parsear_respuesta(texto: str, sugeridas: Etiquetas, *,
                      max_segmentos: int = 2) -> tuple[list[Segmento], bool]:
    """Convierte el texto del LLM en segmentos válidos (§4.4.3). Devuelve ``(segmentos, corregido)``.

    1. Elimina bloques de código. 2. Sin triples ⇒ un segmento con ``sugeridas``. 3. Texto previo
    al primer triple ⇒ segmento con ``sugeridas``. 4. Etiquetas sueltas (``LANG``, ``EXP``,
    ``MOTION``, ``TONE``; ``INTENT`` se ignora). 5. Valores fuera del vocabulario ⇒ ``sugeridas``.
    6. ``limpiar_texto`` y fuera los vacíos. 7. Más de ``max_segmentos`` ⇒ se fusionan en el último.
    """
    corregido = False
    original = "" if texto is None else str(texto)
    max_segmentos = max(1, int(max_segmentos))

    sin_codigo, n_bloques = _RE_BLOQUE_CODIGO.subn(" ", original)            # 1
    if n_bloques:
        corregido = True
    abierto = sin_codigo.find("```")
    if abierto != -1:  # bloque sin cerrar (p. ej. respuesta truncada por MAX_TOKENS)
        sin_codigo = sin_codigo[:abierto]
        corregido = True

    crudos: list[tuple[Etiquetas, str]] = []
    triples = list(TAG_TRIPLE_RE.finditer(sin_codigo))
    if not triples:                                                          # 2
        crudos.append((sugeridas, sin_codigo))
        corregido = True
    else:
        previo = sin_codigo[:triples[0].start()]                             # 3
        if limpiar_texto(previo):
            crudos.append((sugeridas, previo))
            corregido = True
        for i, coincidencia in enumerate(triples):
            fin = triples[i + 1].start() if i + 1 < len(triples) else len(sin_codigo)
            exp, c1 = _valor_vocabulario(coincidencia.group("exp"), EXP_VALIDOS, sugeridas.exp)    # 5
            motion, c2 = _valor_vocabulario(coincidencia.group("motion"), MOTION_VALIDOS, sugeridas.motion)
            tone, c3 = _valor_vocabulario(coincidencia.group("tone"), TONE_VALIDOS, sugeridas.tone)
            if c1 or c2 or c3:
                corregido = True
            crudos.append((Etiquetas(exp, motion, tone), sin_codigo[coincidencia.end():fin]))

    segmentos: list[Segmento] = []
    for etiquetas, cuerpo in crudos:                                         # 4
        estado = {"exp": etiquetas.exp, "motion": etiquetas.motion, "tone": etiquetas.tone,
                  "lang": "es", "corregido": False}

        def _suelta(m: re.Match[str], estado: dict[str, object] = estado) -> str:
            tipo = m.group("tipo").upper()
            valor = normalizar_valor(m.group("valor"))
            if tipo == "LANG":
                if valor in LANG_VALIDOS:
                    estado["lang"] = valor
            elif tipo == "EXP":
                if valor in EXP_VALIDOS:
                    estado["exp"] = valor
                estado["corregido"] = True
            elif tipo == "MOTION":
                if valor in MOTION_VALIDOS:
                    estado["motion"] = valor
                estado["corregido"] = True
            elif tipo == "TONE":
                if valor in TONE_VALIDOS:
                    estado["tone"] = valor
                estado["corregido"] = True
            # INTENT: se elimina y se ignora (las intenciones las decide core/intenciones.py).
            return _MARCA  # limpiar_texto (paso 6) la resuelve: cierra el hueco ante la puntuación

        cuerpo = TAG_SUELTA_RE.sub(_suelta, cuerpo)
        if estado["corregido"]:
            corregido = True
        limpio = limpiar_texto(cuerpo)                                       # 6
        if limpio:
            segmentos.append(Segmento(
                Etiquetas(str(estado["exp"]), str(estado["motion"]), str(estado["tone"])),
                limpio, str(estado["lang"])))

    if not segmentos:
        primeras = crudos[0][0] if crudos else sugeridas
        return [Segmento(primeras, "", "es")], corregido

    if len(segmentos) > max_segmentos:                                       # 7
        ultimo = segmentos[max_segmentos - 1]
        fusion = " ".join(s.texto for s in segmentos[max_segmentos - 1:] if s.texto)
        segmentos = segmentos[:max_segmentos - 1] + [Segmento(ultimo.etiquetas, fusion, ultimo.lang)]
        corregido = True
    return segmentos, corregido


def _partir_oraciones(texto: str, por_lineas: bool) -> list[tuple[str, str]]:
    """Divide ``texto`` en ``(oración, separador_siguiente)``; con ``por_lineas`` también corta en ``\\n``."""
    patron = _RE_FIN_ORACION_O_LINEA if por_lineas else _RE_FIN_ORACION
    unidades: list[tuple[str, str]] = []
    posicion = 0
    for coincidencia in patron.finditer(texto):
        if coincidencia.end() == coincidencia.start():
            continue
        frase = texto[posicion:coincidencia.start()]
        separador = coincidencia.group()
        if frase.strip():
            unidades.append((frase, separador))
        elif unidades:
            unidades[-1] = (unidades[-1][0], unidades[-1][1] + frase + separador)
        posicion = coincidencia.end()
    resto = texto[posicion:]
    if resto.strip():
        unidades.append((resto, ""))
    return unidades


def _medidas(textos: list[str], n_oraciones: int) -> dict[str, int]:
    """Palabras, oraciones, caracteres y líneas del conjunto de textos (un segmento por línea al menos)."""
    vivos = [t for t in textos if t]
    return {
        "palabras": sum(len(t.split()) for t in vivos),
        "oraciones": n_oraciones,
        "caracteres": sum(len(t) for t in vivos) + max(0, len(vivos) - 1),
        "lineas": sum(t.count("\n") + 1 for t in vivos),
    }


def _cabe(medidas: dict[str, int], limites: dict[str, int]) -> bool:
    """``True`` si todas las medidas respetan los límites."""
    return all(medidas.get(clave, 0) <= tope for clave, tope in limites.items())


def _corte_duro(texto: str, limites: dict[str, int]) -> str:
    """Corte por palabras que respeta los límites y termina en ``"…"``."""
    tope_palabras = limites.get("palabras")
    tope_caracteres = limites.get("caracteres")
    tope_lineas = limites.get("lineas")
    acumulado = ""
    palabras = 0
    for token in _RE_TOKENS.findall(texto):
        if token.isspace():
            acumulado += token
            continue
        candidato = acumulado + token
        if tope_palabras is not None and palabras + 1 > tope_palabras:
            break
        if tope_caracteres is not None and len(candidato.rstrip()) + 1 > tope_caracteres:
            break
        if tope_lineas is not None and candidato.count("\n") + 1 > tope_lineas:
            break
        acumulado = candidato
        palabras += 1
    recortado = acumulado.rstrip().rstrip(",;:—-").rstrip()
    if not recortado:
        largo = (tope_caracteres - 1) if tope_caracteres else 40
        recortado = texto.strip()[:max(1, largo)].rstrip()
    return recortado + "…"


def recortar_segmentos(segmentos: list[Segmento], modo_salida: str) -> tuple[list[Segmento], bool]:
    """Aplica el límite del modo de salida sobre el texto total (§4.4.3). Devuelve ``(segmentos, recortado)``.

    Se recorta primero el último segmento, cortando en el último fin de oración que quepa; si no
    cabe ni una oración, corte duro por palabras con ``"…"``. Modo desconocido = ``terminal``.
    En ``telegram`` los saltos de línea también cuentan como cortes y cada segmento ocupa al
    menos una línea.
    """
    segs = [s for s in (segmentos or []) if isinstance(s, Segmento)]
    if not segs:
        return list(segmentos or []), False
    limites = LIMITES_SALIDA.get(modo_salida) or LIMITES_SALIDA["terminal"]
    por_lineas = "lineas" in limites

    unidades: list[tuple[int, str, str]] = []
    for indice, segmento in enumerate(segs):
        for frase, separador in _partir_oraciones(segmento.texto, por_lineas):
            unidades.append((indice, frase, separador))

    if _cabe(_medidas([s.texto for s in segs], len(unidades)), limites):
        return segs, False

    def reconstruir(elegidas: list[tuple[int, str, str]]) -> dict[int, str]:
        textos: dict[int, str] = {}
        for posicion, (indice, frase, separador) in enumerate(elegidas):
            siguiente_mismo = posicion + 1 < len(elegidas) and elegidas[posicion + 1][0] == indice
            textos[indice] = textos.get(indice, "") + frase + (separador if siguiente_mismo else "")
        return {i: t.strip() for i, t in textos.items()}

    elegidas: list[tuple[int, str, str]] = []
    for unidad in unidades:
        prueba = elegidas + [unidad]
        if _cabe(_medidas(list(reconstruir(prueba).values()), len(prueba)), limites):
            elegidas = prueba
        else:
            break

    if elegidas:
        textos = reconstruir(elegidas)
        resultado = [Segmento(segs[i].etiquetas, textos[i], segs[i].lang)
                     for i in sorted(textos) if textos[i]]
        return resultado, True

    primero = next((s for s in segs if s.texto.strip()), segs[0])
    return [Segmento(primero.etiquetas, _corte_duro(primero.texto, limites), primero.lang)], True


def _es_tag_audio(contenido: str) -> bool:
    """``True`` si el contenido de un corchete es un tag de audio (conocido o 1–2 palabras de la lista)."""
    normalizado = " ".join(contenido.strip().lower().split())
    if normalizado in TAGS_AUDIO_CONOCIDOS:
        return True
    palabras = normalizado.replace(",", " ").split()
    return 1 <= len(palabras) <= 2 and all(p.strip("'-") in PALABRAS_TAGS_AUDIO for p in palabras)


def _cerrar_huecos(texto: str) -> str:
    """Resuelve las marcas de :data:`_MARCA`.

    Una marca pegada a puntuación de cierre (seguida de espacio, otra marca o el final) se
    elimina junto con los espacios y marcas que la preceden; el resto de marcas pasan a ser
    espacios. Los espacios que no rodean una marca no se tocan.
    """
    if _MARCA not in texto:
        return texto
    return _RE_HUECO_ANTE_PUNTUACION.sub("", texto).replace(_MARCA, " ")


def _quitar_etiquetas(texto: str) -> str:
    """Quita triples, etiquetas sueltas y tags de audio conocidos (conserva los saltos de línea).

    Acepta texto con marcas ya puestas (``parsear_respuesta`` marca así las etiquetas sueltas)
    y devuelve el texto sin ninguna marca.
    """
    resultado = TAG_TRIPLE_RE.sub(_MARCA, texto)
    resultado = TAG_SUELTA_RE.sub(_MARCA, resultado)
    resultado = TAG_AUDIO_RE.sub(
        lambda m: _MARCA if _es_tag_audio(m.group("contenido")) else m.group(0), resultado)
    return _cerrar_huecos(resultado)


def limpiar_texto(texto: str) -> str:
    """Elimina etiquetas y tags de audio, colapsa los espacios y aplica ``strip`` (§4.4.3).

    # CONTRATO: no se toca la puntuación del texto. Solo se cierra el hueco que deja una
    # etiqueta eliminada justo antes de un signo de cierre ("Hola [sighs]." → "Hola."); un
    # espacio escrito antes de un punto (".env", "find . -name") se conserva.
    """
    if not texto:
        return ""
    resultado = _quitar_etiquetas(str(texto))
    resultado = _RE_ESPACIOS.sub(" ", resultado)
    return resultado.strip()


def limpiar_para_voz(texto: str) -> str:
    """Texto apto para TTS (§4.4.3): sin etiquetas, código, markdown, URLs ni emojis, en una línea.

    # CONTRATO: el paso 1 ("aplica limpiar_texto") se hace conservando los saltos de línea,
    # porque los pasos 2–3 (bloques de código, encabezados, citas y viñetas) son por línea y el
    # paso 6 es el que convierte los saltos en espacios.
    """
    if not texto:
        return ""
    resultado = _quitar_etiquetas(str(texto))                                # 1
    resultado = _RE_BLOQUE_CODIGO.sub(" ", resultado)                        # 2
    resultado = _RE_CODIGO_EN_LINEA.sub(r"\1", resultado)
    resultado = resultado.replace("`", "")
    resultado = _RE_ENLACE_MD.sub(r"\1", resultado)                          # 3
    resultado = _RE_NEGRITA.sub("", resultado)
    resultado = _RE_ENCABEZADO.sub("", resultado)
    resultado = _RE_CITA.sub("", resultado)
    resultado = _RE_VINETA.sub("", resultado)
    resultado = _RE_ASTERISCO.sub("", resultado)
    resultado = _RE_URL.sub("el enlace", resultado)                          # 4
    resultado = _cerrar_huecos(_RE_EMOJI.sub(_MARCA, resultado))             # 5
    resultado = _RE_ESPACIOS.sub(" ", resultado)                             # 6
    return resultado.strip()


def limpiar_para_telegram(texto: str) -> str:
    """Como :func:`limpiar_texto`, pero conservando los saltos de línea. No escapa HTML."""
    if not texto:
        return ""
    resultado = _quitar_etiquetas(str(texto)).replace("\r\n", "\n").replace("\r", "\n")
    lineas = []
    for linea in resultado.split("\n"):
        linea = _RE_ESPACIOS_LINEA.sub(" ", linea)
        lineas.append(linea.strip())
    resultado = "\n".join(lineas)
    resultado = _RE_SALTOS_MULTIPLES.sub("\n\n", resultado)
    return resultado.strip()


def tag_audio(tone: str, modelo: str) -> str:
    """Tag de audio de ElevenLabs para ``tone`` (Anexo E); ``""`` si el modelo no los admite.

    v4 → ``TAG_AUDIO_TONE[tone]``; v3 → solo si está en ``TAGS_V3_VERIFICADOS``; otro → ``""``
    (si se enviara, el tag se leería en voz alta).
    """
    modelo = str(modelo or "")
    if modelo not in MODELOS_CON_TAGS:
        return ""
    tag = TAG_AUDIO_TONE.get(normalizar_valor(tone), "")
    if modelo.startswith("eleven_v4"):
        return tag
    if modelo.startswith("eleven_v3"):
        return tag if tag in TAGS_V3_VERIFICADOS else ""
    return ""


def ajustes_voz(tone: str, modelo: str) -> dict[str, float | bool]:
    """``voice_settings`` de ElevenLabs para ``tone`` (Anexo E), con ``use_speaker_boost=True``.

    En ``eleven_v3*`` y ``eleven_v4*``, ``stability`` se ajusta al valor más cercano de
    {0.0, 0.5, 1.0} (en empate, el más estable). Tono desconocido ⇒ ``analitico``.
    """
    base = AJUSTES_VOZ.get(normalizar_valor(tone)) or AJUSTES_VOZ["analitico"]
    ajustes: dict[str, float | bool] = {k: float(v) for k, v in base.items()}
    modelo = str(modelo or "")
    if modelo.startswith("eleven_v3") or modelo.startswith("eleven_v4"):
        estabilidad = float(base["stability"])
        ajustes["stability"] = min(_ESTABILIDADES_DISCRETAS,
                                   key=lambda v: (round(abs(v - estabilidad), 9), -v))
    ajustes["use_speaker_boost"] = True
    return ajustes


def prosodia_edge(tone: str) -> tuple[str, str]:
    """``(rate, pitch)`` para edge-tts en formato exacto (``"+6%"``, ``"-4Hz"``); desconocido ⇒ ``analitico``."""
    return PROSODIA_EDGE.get(normalizar_valor(tone), PROSODIA_EDGE["analitico"])
