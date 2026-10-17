"""Tests de core/etiquetas.py: gramática [EXP][MOTION][TONE] y tablas de voz (grupo config-bus, §12.3)."""

from __future__ import annotations

import math
import re

import pytest

from core.contracts import (
    EXP_VALIDOS,
    MOTION_VALIDOS,
    TONE_VALIDOS,
    Etiquetas,
    HomeostaticState,
    Segmento,
)
from core.etiquetas import (
    AJUSTES_VOZ,
    EXP_SOCIALES,
    PROSODIA_EDGE,
    PROTOTIPOS_EXP,
    TAG_AUDIO_TONE,
    TAG_SUELTA_RE,
    TAG_TRIPLE_RE,
    TAGS_AUDIO_CONOCIDOS,
    ajustes_voz,
    distancia_a_prototipo,
    es_valida,
    limpiar_para_telegram,
    limpiar_para_voz,
    limpiar_texto,
    normalizar_valor,
    parsear_respuesta,
    prosodia_edge,
    recortar_segmentos,
    sugerir_etiquetas,
    tag_audio,
    tipo_evento,
    validar_contra_estado,
)

SUGERIDAS = Etiquetas("seria", "asentir", "analitico")


def _frase(n: int, palabra: str = "dato") -> str:
    return " ".join(f"{palabra}{i}" for i in range(n))


def _palabras(segmentos: list[Segmento]) -> int:
    return sum(len(s.texto.split()) for s in segmentos)


# ---------------------------------------------------------------------------
# Vocabulario y normalización
# ---------------------------------------------------------------------------


def test_vocabulario_y_prototipos_coherentes() -> None:
    assert set(PROTOTIPOS_EXP) == set(EXP_VALIDOS)
    assert EXP_SOCIALES == {"sonrojo_tsundere", "feliz", "avergonzada"}
    assert set(TAG_AUDIO_TONE) == set(TONE_VALIDOS) == set(AJUSTES_VOZ) == set(PROSODIA_EDGE)
    assert {"sarcastic", "warmly", "nervous", "short pause", "laughs harder"} <= TAGS_AUDIO_CONOCIDOS


def test_normalizar_valor() -> None:
    assert normalizar_valor("  Señalar ") == "senalar"
    assert normalizar_valor("Mirar a otro-lado") == "mirar_a_otro_lado"
    assert normalizar_valor("SARCÁSTICO") == "sarcastico"
    assert normalizar_valor("Sonrojo_Tsundere") == "sonrojo_tsundere"


def test_es_valida_y_distancia() -> None:
    assert es_valida(Etiquetas())
    assert not es_valida(Etiquetas("bailarina", "idle", "analitico"))
    estado = HomeostaticState(arousal=0.4, focus=0.5, boredom=0.3, fatiga=0.3)
    assert distancia_a_prototipo("neutral", estado) == pytest.approx(0.0)
    assert distancia_a_prototipo("inventada", estado) == math.inf
    assert tipo_evento("apodo:christina") == "apodo"
    assert tipo_evento(None) is None


def test_regex_exportadas() -> None:
    m = TAG_TRIPLE_RE.search("x [ EXP : smug ][MOTION:asentir] [TONE:calido] y")
    assert m and (m.group("exp"), m.group("motion"), m.group("tone")) == ("smug", "asentir", "calido")
    sueltas = [m.group("tipo").upper() for m in TAG_SUELTA_RE.finditer("[lang:en] [INTENT: abrir]")]
    assert sueltas == ["LANG", "INTENT"]


# ---------------------------------------------------------------------------
# parsear_respuesta
# ---------------------------------------------------------------------------


def test_parsear_triple_valido() -> None:
    segs, corregido = parsear_respuesta("[EXP:smug][MOTION:brazos_cruzados][TONE:sarcastico] Hola.",
                                        SUGERIDAS)
    assert corregido is False
    assert segs == [Segmento(Etiquetas("smug", "brazos_cruzados", "sarcastico"), "Hola.", "es")]


def test_parsear_sin_etiquetas_usa_sugeridas() -> None:
    segs, corregido = parsear_respuesta("Solo texto, sin formato.", SUGERIDAS)
    assert corregido is True
    assert len(segs) == 1
    assert segs[0].etiquetas == SUGERIDAS and segs[0].texto == "Solo texto, sin formato."


def test_parsear_espacios_en_etiquetas() -> None:
    segs, corregido = parsear_respuesta("[EXP: smug] [MOTION : asentir ][ TONE:Calido ]  ¿Y bien?",
                                        SUGERIDAS)
    assert corregido is False
    assert segs[0].etiquetas == Etiquetas("smug", "asentir", "calido")
    assert segs[0].texto == "¿Y bien?"


def test_parsear_etiquetas_sueltas() -> None:
    texto = ("[EXP:pensativa][MOTION:idle][TONE:analitico] [LANG:en] Hello [TONE:calido] there. "
             "[INTENT:abrir_app] [EXP:smug] Fin.")
    segs, corregido = parsear_respuesta(texto, SUGERIDAS)
    assert corregido is True
    assert len(segs) == 1
    assert segs[0].lang == "en"
    assert segs[0].etiquetas == Etiquetas("smug", "idle", "calido")
    assert segs[0].texto == "Hello there. Fin."
    solo_lang, corregido_lang = parsear_respuesta(
        "[EXP:seria][MOTION:asentir][TONE:serio][LANG:ja] Konnichiwa.", SUGERIDAS)
    assert solo_lang[0].lang == "ja" and corregido_lang is False


def test_parsear_mas_de_dos_segmentos_se_fusionan() -> None:
    texto = ("[EXP:seria][MOTION:asentir][TONE:analitico] Uno. "
             "[EXP:sonrojo_tsundere][MOTION:mirar_a_otro_lado][TONE:nervioso] Dos. "
             "[EXP:smug][MOTION:asentir][TONE:sarcastico] Tres.")
    segs, corregido = parsear_respuesta(texto, SUGERIDAS)
    assert corregido is True
    assert [s.texto for s in segs] == ["Uno.", "Dos. Tres."]
    assert segs[1].etiquetas.exp == "sonrojo_tsundere"
    uno, _ = parsear_respuesta(texto, SUGERIDAS, max_segmentos=1)
    assert [s.texto for s in uno] == ["Uno. Dos. Tres."]


def test_parsear_valores_invalidos() -> None:
    segs, corregido = parsear_respuesta("[EXP:bailarina][MOTION:volar][TONE:epico] Texto.", SUGERIDAS)
    assert corregido is True
    assert segs[0].etiquetas == SUGERIDAS
    parcial, corregido2 = parsear_respuesta("[EXP:smug][MOTION:volar][TONE:calido] Ok.", SUGERIDAS)
    assert corregido2 is True
    assert parcial[0].etiquetas == Etiquetas("smug", "asentir", "calido")


def test_parsear_elimina_bloques_de_codigo() -> None:
    texto = "[EXP:seria][MOTION:asentir][TONE:analitico] Mira:\n```python\nimport os\n```\nListo."
    segs, corregido = parsear_respuesta(texto, SUGERIDAS)
    assert corregido is True
    assert "import" not in segs[0].texto and segs[0].texto == "Mira: Listo."
    truncado, corregido_t = parsear_respuesta(
        "[EXP:seria][MOTION:asentir][TONE:analitico] Antes ```bash\nrm -rf", SUGERIDAS)
    assert corregido_t is True and truncado[0].texto == "Antes"


def test_parsear_texto_previo_y_vacios() -> None:
    segs, corregido = parsear_respuesta("Preámbulo. [EXP:smug][MOTION:asentir][TONE:sarcastico] Resto.",
                                        SUGERIDAS)
    assert corregido is True
    assert [(s.etiquetas.exp, s.texto) for s in segs] == [("seria", "Preámbulo."), ("smug", "Resto.")]
    vacio, _ = parsear_respuesta("[EXP:smug][MOTION:asentir][TONE:sarcastico]   ", SUGERIDAS)
    assert len(vacio) == 1 and vacio[0].texto == "" and vacio[0].etiquetas.exp == "smug"
    assert parsear_respuesta("", SUGERIDAS)[0][0].texto == ""


# ---------------------------------------------------------------------------
# sugerir_etiquetas (una prueba por regla)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("estado", "kwargs", "esperado"), [
    # 1 alerta crítica (sorprendida si arousal ≥ 0,8; si no, seria)
    (HomeostaticState(arousal=0.9), {"alerta_critica": True}, ("sorprendida", "senalar", "serio")),
    (HomeostaticState(arousal=0.5), {"alerta_critica": True, "evento_social": "tristeza"},
     ("seria", "senalar", "serio")),
    # 2 tristeza (antes que la fatiga)
    (HomeostaticState(fatiga=0.9), {"evento_social": "tristeza"}, ("triste", "asentir", "calido")),
    # 3 fatiga > 0,75
    (HomeostaticState(fatiga=0.8), {"evento_social": "cumplido", "tsundere_ok": True},
     ("somnolienta", "bostezar", "somnoliento")),
    # 4 error propio
    (HomeostaticState(), {"evento_social": "error_propio"},
     ("avergonzada", "mirar_a_otro_lado", "nervioso")),
    # 5a tsundere permitido (también con apodo:chuunibyou)
    (HomeostaticState(), {"evento_social": "cumplido", "tsundere_ok": True},
     ("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso")),
    (HomeostaticState(), {"evento_social": "apodo:chuunibyou", "tsundere_ok": True},
     ("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso")),
    # 5b apodo / acusación sin presupuesto tsundere
    (HomeostaticState(), {"evento_social": "apodo:christina"}, ("molesta", "brazos_cruzados", "sarcastico")),
    (HomeostaticState(), {"evento_social": "apodo:chuunibyou"}, ("molesta", "brazos_cruzados", "sarcastico")),
    (HomeostaticState(), {"evento_social": "acusacion_tsundere"}, ("molesta", "brazos_cruzados", "sarcastico")),
    # 5c cumplido / agradecimiento sin presupuesto tsundere
    (HomeostaticState(), {"evento_social": "agradecimiento"}, ("smug", "asentir", "calido")),
    # 6 regreso
    (HomeostaticState(), {"evento_social": "regreso"}, ("feliz", "asentir", "calido")),
    # 7 sobresalto: arousal > 0,80 y focus < 0,40
    (HomeostaticState(arousal=0.85, focus=0.3), {}, ("sorprendida", "ladear_cabeza", "nervioso")),
    # 8 foco alto (seria si arousal ≥ 0,5; si no, pensativa)
    (HomeostaticState(arousal=0.6, focus=0.8), {}, ("seria", "asentir", "analitico")),
    (HomeostaticState(arousal=0.4, focus=0.8), {}, ("pensativa", "asentir", "analitico")),
    # 9a aburrida pero curiosa
    (HomeostaticState(arousal=0.5, focus=0.5, boredom=0.8, curiosidad=0.6), {},
     ("curiosa", "ladear_cabeza", "jugueton")),
    # 9b aburrida (sarcástica con valencia negativa; juguetona si no)
    (HomeostaticState(arousal=0.5, focus=0.5, boredom=0.8, curiosidad=0.2, valencia=-0.3), {},
     ("aburrida", "estirarse", "sarcastico")),
    (HomeostaticState(arousal=0.5, focus=0.5, boredom=0.8, curiosidad=0.2, valencia=0.1), {},
     ("aburrida", "estirarse", "jugueton")),
    # 10 saludo / despedida
    (HomeostaticState(), {"evento_social": "saludo"}, ("neutral", "asentir", "calido")),
    (HomeostaticState(), {"evento_social": "despedida"}, ("neutral", "asentir", "calido")),
    # 11 buen ánimo
    (HomeostaticState(arousal=0.6, focus=0.5, valencia=0.4), {}, ("smug", "asentir", "sarcastico")),
    # 12 por defecto
    (HomeostaticState(), {}, ("neutral", "idle", "analitico")),
])
def test_sugerir_etiquetas_reglas(estado: HomeostaticState, kwargs: dict, esperado: tuple) -> None:
    etq = sugerir_etiquetas(estado, **kwargs)
    assert (etq.exp, etq.motion, etq.tone) == esperado
    assert es_valida(etq)


# ---------------------------------------------------------------------------
# validar_contra_estado
# ---------------------------------------------------------------------------


def test_validar_alerta_manda_sugeridas() -> None:
    sugeridas = Etiquetas("seria", "senalar", "serio")
    final, corregido = validar_contra_estado(Etiquetas("smug", "asentir", "jugueton"), sugeridas,
                                             HomeostaticState(), alerta_critica=True)
    assert final == sugeridas and corregido is True
    igual, corregido2 = validar_contra_estado(sugeridas, sugeridas, HomeostaticState(),
                                              alerta_critica=True)
    assert igual == sugeridas and corregido2 is False


def test_validar_sin_cambios() -> None:
    estado = HomeostaticState()
    etq = Etiquetas("seria", "asentir", "analitico")
    assert validar_contra_estado(etq, Etiquetas(), estado) == (etq, False)


def test_validar_exp_social_sin_evento() -> None:
    final, corregido = validar_contra_estado(Etiquetas("feliz", "asentir", "calido"),
                                             Etiquetas("neutral", "idle", "analitico"),
                                             HomeostaticState())
    assert final.exp == "neutral" and corregido is True
    con_evento, _ = validar_contra_estado(Etiquetas("feliz", "asentir", "calido"),
                                          Etiquetas("feliz", "asentir", "calido"),
                                          HomeostaticState(), evento_social="regreso")
    assert con_evento.exp == "feliz"


def test_validar_tsundere_no_permitido() -> None:
    sugeridas = Etiquetas("smug", "asentir", "calido")
    final, corregido = validar_contra_estado(
        Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"), sugeridas,
        HomeostaticState(), evento_social="cumplido", tsundere_ok=False)
    assert corregido is True
    assert final == Etiquetas("smug", "mirar_a_otro_lado", "calido")
    permitido, corregido_ok = validar_contra_estado(
        Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"),
        Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"),
        HomeostaticState(), evento_social="cumplido", tsundere_ok=True)
    assert permitido.exp == "sonrojo_tsundere" and corregido_ok is False
    # Si las sugeridas también son tsundere (sin presupuesto), se cae a neutral.
    neutral, _ = validar_contra_estado(
        Etiquetas("sonrojo_tsundere", "idle", "analitico"),
        Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"),
        HomeostaticState(), evento_social="apodo:christina", tsundere_ok=False)
    assert neutral.exp == "neutral"


def test_validar_distancia_mayor_que_umbral() -> None:
    estado = HomeostaticState()   # (0,55; 0,70; 0,20; 0,25)
    lejos, corregido = validar_contra_estado(Etiquetas("aburrida", "estirarse", "jugueton"),
                                             SUGERIDAS, estado)
    assert distancia_a_prototipo("aburrida", estado) > 0.55
    assert lejos.exp == SUGERIDAS.exp and corregido is True
    cerca, corregido_cerca = validar_contra_estado(Etiquetas("pensativa", "asentir", "analitico"),
                                                   Etiquetas(), estado)
    assert distancia_a_prototipo("pensativa", estado) <= 0.55
    assert cerca.exp == "pensativa" and corregido_cerca is False
    estricto, _ = validar_contra_estado(Etiquetas("pensativa", "asentir", "analitico"),
                                        Etiquetas(), estado, umbral=0.05)
    assert estricto.exp == "neutral"


def test_validar_fatiga_fuerza_somnoliento() -> None:
    estado = HomeostaticState(arousal=0.1, focus=0.2, boredom=0.4, fatiga=0.9)
    final, corregido = validar_contra_estado(Etiquetas("somnolienta", "bostezar", "sarcastico"),
                                             Etiquetas("somnolienta", "bostezar", "somnoliento"), estado)
    assert final.tone == "somnoliento" and corregido is True
    for tono in ("calido", "serio", "somnoliento"):
        conservado, _ = validar_contra_estado(Etiquetas("somnolienta", "bostezar", tono),
                                              Etiquetas("somnolienta", "bostezar", "somnoliento"), estado)
        assert conservado.tone == tono


def test_validar_valores_fuera_de_vocabulario() -> None:
    final, corregido = validar_contra_estado(Etiquetas("Pensativa", "volar", "épico"), SUGERIDAS,
                                             HomeostaticState())
    assert final == Etiquetas("pensativa", "asentir", "analitico")
    assert corregido is True
    assert es_valida(final)


# ---------------------------------------------------------------------------
# Limpieza
# ---------------------------------------------------------------------------


def test_limpiar_texto() -> None:
    texto = "[EXP:smug][MOTION:idle][TONE:calido] Hola [laughs] ref [1] [sic] [short pause]. [clears throat] ok"
    assert limpiar_texto(texto) == "Hola ref [1] [sic]. ok"
    assert limpiar_texto("  varias\n\nlíneas   aquí ") == "varias líneas aquí"
    assert limpiar_texto("[Hououin Kyouma] dijo") == "[Hououin Kyouma] dijo"
    assert limpiar_texto("") == ""


@pytest.mark.parametrize("texto", [
    "Revisa tu archivo .env y el script ./run.sh",
    "Copia .env.example a .env",
    "Valor de .5 o 0.5",
    "Ejecuta find . -type f y luego git add .",
    "Hola .",                                   # §4.4.3: solo colapsar espacios y strip
])
def test_limpiar_conserva_puntuacion_del_texto(texto: str) -> None:
    assert limpiar_texto(texto) == texto
    assert limpiar_para_telegram(texto) == texto
    assert limpiar_para_voz(texto) == texto


def test_limpiar_cierra_solo_el_hueco_de_la_etiqueta() -> None:
    assert limpiar_texto("Hola [sighs].") == "Hola."
    assert limpiar_texto("Hola [EXP:smug] [sighs]. Bueno [laughs], vale [laughs]!!") == \
        "Hola. Bueno, vale!!"
    assert limpiar_texto("Ejecuta [sighs] ./run.sh") == "Ejecuta ./run.sh"
    assert limpiar_texto("[EXP:seria][MOTION:asentir][TONE:analitico] .env es la clave") == \
        ".env es la clave"
    assert limpiar_para_telegram("Línea [laughs].\nCopia .env") == "Línea.\nCopia .env"
    assert limpiar_para_voz("Bien 😀. Mira .env") == "Bien. Mira .env"
    assert "\x00" not in limpiar_texto("Hola\x00mundo [sighs].")
    segs, corregido = parsear_respuesta(
        "[EXP:seria][MOTION:asentir][TONE:analitico] Crea el archivo .env con chmod 600 .env",
        SUGERIDAS)
    assert corregido is False and segs[0].texto == "Crea el archivo .env con chmod 600 .env"
    sueltas, _ = parsear_respuesta(
        "[EXP:seria][MOTION:asentir][TONE:analitico] Listo [TONE:calido]. Fin [INTENT:x]!", SUGERIDAS)
    assert sueltas[0].texto == "Listo. Fin!"


def test_limpiar_para_voz() -> None:
    texto = ("# Informe\n> cita importante\n- **Primero**: mira https://example.com/a?b=1 😀\n"
             "2) `pip install` y __listo__ 👍🏽\n```bash\nrm -rf /\n```\n[docs](https://x.org) ✨")
    limpio = limpiar_para_voz(texto)
    assert "http" not in limpio and "el enlace" in limpio
    for prohibido in ("**", "__", "#", ">", "`", "rm -rf", "😀", "👍", "✨", "\n", "- "):
        assert prohibido not in limpio
    assert limpio.startswith("Informe cita importante Primero: mira el enlace")
    assert "pip install y listo" in limpio and limpio.endswith("docs")
    assert limpiar_para_voz("[EXP:seria][MOTION:asentir][TONE:serio] Visita https://a.b") == \
        "Visita el enlace"


def test_limpiar_para_telegram_conserva_saltos() -> None:
    texto = "[EXP:smug][MOTION:idle][TONE:calido] Línea   uno [laughs]\n\n\n\nLínea <b>dos</b>  "
    assert limpiar_para_telegram(texto) == "Línea uno\n\nLínea <b>dos</b>"


# ---------------------------------------------------------------------------
# recortar_segmentos
# ---------------------------------------------------------------------------


def test_recortar_voz_dos_oraciones() -> None:
    segs = [Segmento(Etiquetas(), "Uno. Dos. Tres.")]
    recortados, cambio = recortar_segmentos(segs, "voz")
    assert cambio is True and [s.texto for s in recortados] == ["Uno. Dos."]
    dos = [Segmento(Etiquetas("seria", "asentir", "analitico"), "Uno."),
           Segmento(Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"), "¡Dos! ¿Tres?")]
    recortados2, cambio2 = recortar_segmentos(dos, "voz")
    assert cambio2 is True
    assert [(s.etiquetas.exp, s.texto) for s in recortados2] == [("seria", "Uno."),
                                                                  ("sonrojo_tsundere", "¡Dos!")]
    cabe, sin_cambio = recortar_segmentos([Segmento(Etiquetas(), "Hola. Adiós.")], "voz")
    assert sin_cambio is False and cabe[0].texto == "Hola. Adiós."


def test_recortar_voz_45_palabras_corte_duro() -> None:
    segs = [Segmento(Etiquetas(), _frase(60))]
    recortados, cambio = recortar_segmentos(segs, "voz")
    assert cambio is True
    assert recortados[0].texto.endswith("…")
    assert _palabras(recortados) == 45


def test_recortar_voz_se_recorta_primero_el_ultimo_segmento() -> None:
    segs = [Segmento(Etiquetas("seria", "asentir", "analitico"), _frase(30) + "."),
            Segmento(Etiquetas("smug", "asentir", "sarcastico"), _frase(30, "otro") + ".")]
    recortados, cambio = recortar_segmentos(segs, "voz")
    assert cambio is True
    assert len(recortados) == 1 and recortados[0].etiquetas.exp == "seria"
    assert _palabras(recortados) == 30


def test_recortar_nota_voz_50_palabras() -> None:
    texto = " ".join(_frase(20, f"p{i}_") + "." for i in range(3))
    recortados, cambio = recortar_segmentos([Segmento(Etiquetas(), texto)], "nota_voz")
    assert cambio is True
    assert _palabras(recortados) <= 50
    assert len(re.split(r"(?<=[.!?…])\s+", recortados[0].texto)) <= 3
    cuatro = "Uno. Dos. Tres. Cuatro."
    assert recortar_segmentos([Segmento(Etiquetas(), cuatro)], "nota_voz")[0][0].texto == "Uno. Dos. Tres."


def test_recortar_modo_desconocido_es_terminal() -> None:
    texto = " ".join(_frase(10, f"o{i}_") + "." for i in range(13))   # 130 palabras
    segs = [Segmento(Etiquetas(), texto)]
    terminal, cambio_t = recortar_segmentos(segs, "terminal")
    desconocido, cambio_d = recortar_segmentos(segs, "holograma")
    assert cambio_t is cambio_d is True
    assert desconocido == terminal
    assert _palabras(terminal) == 120
    largo, cambio_l = recortar_segmentos(segs, "terminal_largo")
    assert cambio_l is False and largo == segs


def test_recortar_telegram_lineas_y_caracteres() -> None:
    lineas, cambio = recortar_segmentos([Segmento(Etiquetas(), "Uno\nDos\nTres\nCuatro")], "telegram")
    assert cambio is True and lineas[0].texto == "Uno\nDos\nTres"
    largo, cambio2 = recortar_segmentos([Segmento(Etiquetas(), "abcdefghi " * 70)], "telegram")
    assert cambio2 is True
    assert len(largo[0].texto) <= 600 and largo[0].texto.endswith("…")
    assert recortar_segmentos([], "telegram") == ([], False)


# ---------------------------------------------------------------------------
# Tablas de voz (Anexo E)
# ---------------------------------------------------------------------------


def test_tag_audio() -> None:
    assert tag_audio("sarcastico", "eleven_v4_turbo") == "[sarcastic]"
    assert tag_audio("calido", "eleven_v4") == "[warmly]"
    assert tag_audio("sarcastico", "eleven_v3") == "[sarcastic]"
    assert tag_audio("calido", "eleven_v3") == ""            # [warmly] no está verificado en v3
    assert tag_audio("sarcastico", "eleven_flash_v2_5") == ""
    assert tag_audio("sarcastico", "eleven_multilingual_v2") == ""
    assert tag_audio("inventado", "eleven_v4_turbo") == ""


def test_ajustes_voz() -> None:
    v4 = ajustes_voz("sarcastico", "eleven_v4_turbo")
    assert set(v4) == {"stability", "similarity_boost", "style", "speed", "use_speaker_boost"}
    assert v4["stability"] in (0.0, 0.5, 1.0)
    assert v4["use_speaker_boost"] is True
    assert v4["similarity_boost"] == 0.80 and v4["style"] == 0.55 and v4["speed"] == 1.00
    for tono in TONE_VALIDOS:
        assert ajustes_voz(tono, "eleven_v4")["stability"] in (0.0, 0.5, 1.0)
        assert ajustes_voz(tono, "eleven_v3_conversational")["stability"] in (0.0, 0.5, 1.0)
    assert ajustes_voz("serio", "eleven_v4_turbo")["stability"] == 1.0
    flash = ajustes_voz("sarcastico", "eleven_flash_v2_5")
    assert flash["stability"] == 0.45
    assert ajustes_voz("desconocido", "eleven_flash_v2_5")["stability"] == 0.70   # analitico
    ajustes_voz("sarcastico", "eleven_v4")["speed"] = 9.9
    assert AJUSTES_VOZ["sarcastico"]["speed"] == 1.00                            # devuelve copias


def test_prosodia_edge() -> None:
    for tono in TONE_VALIDOS:
        rate, pitch = prosodia_edge(tono)
        assert re.fullmatch(r"[+-]\d+%", rate) and re.fullmatch(r"[+-]\d+Hz", pitch)
    assert prosodia_edge("sarcastico") == ("+6%", "+0Hz")
    assert prosodia_edge("calido") == ("-2%", "-4Hz")
    assert prosodia_edge("serio") == ("+0%", "-6Hz")
    assert prosodia_edge("somnoliento") == ("-12%", "-8Hz")
    assert prosodia_edge("??") == prosodia_edge("analitico")


def test_sugeridas_siempre_validas_en_vocabulario() -> None:
    for exp in EXP_VALIDOS:
        assert exp in PROTOTIPOS_EXP
    for motion in ("senalar", "bostezar", "estirarse", "ladear_cabeza"):
        assert motion in MOTION_VALIDOS
