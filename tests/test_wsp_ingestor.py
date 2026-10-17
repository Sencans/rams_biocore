"""Tests de core/wsp_ingestor.py con chats sintéticos (grupo memory, §10.9 y §12.3).

Los chats de ``tests/fixtures`` son 100 % sintéticos ("Yo Prueba" y "Ana Ficticia", con el
tercero "Pedro" mencionado por "Yo Prueba"). ``chat_muletillas.txt`` (también inventado)
reproduce el fallo de las palabras de tema tomadas por muletillas y los teclazos ("kakfkak")
sueltos. Sin red ni hardware.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from core.wsp_ingestor import (
    CLAVE_RISA_TECLAZO,
    ETIQUETA_TECLAZO,
    MensajeWsp,
    agrupar_rafagas,
    cargar_perfil,
    construir_perfil,
    detectar_autores,
    detectar_nombres_propios,
    extraer_muletillas,
    guardar_perfil,
    main,
    normalizar_linea,
    parsear_archivo,
    parsear_lineas,
    resumen_perfil,
)

try:
    from conftest import RAIZ  # contrato §12.2
except ImportError:  # CONTRATO: modo de importación sin tests/ en sys.path; misma definición que conftest
    RAIZ = Path(__file__).resolve().parents[1]

FIXTURES = RAIZ / "tests" / "fixtures"
ANDROID = FIXTURES / "chat_android.txt"
IOS = FIXTURES / "chat_ios.txt"
MULETILLAS = FIXTURES / "chat_muletillas.txt"
YO = "Yo Prueba"


def _no_sistema(mensajes: list[MensajeWsp]) -> list[MensajeWsp]:
    return [m for m in mensajes if not m.sistema]


def _todos_los_ngramas_y_temas(perfil: dict) -> list[str]:
    return [m["ngrama"] for m in perfil["muletillas"]] + [t["palabra"] for t in perfil["temas"]]


# ---------------------------------------------------------------------------
# Los 6 casos de §10.9
# ---------------------------------------------------------------------------


def test_android_e_ios_mismo_numero_de_mensajes() -> None:
    android = parsear_archivo(ANDROID)
    ios = parsear_archivo(IOS)
    assert len(_no_sistema(android)) == len(_no_sistema(ios)) > 60
    assert sum(m.sistema for m in android) == sum(m.sistema for m in ios) == 1
    assert detectar_autores(android) == detectar_autores(ios)
    assert set(detectar_autores(android)) == {YO, "Ana Ficticia"}
    assert sum(m.multimedia for m in android) == sum(m.multimedia for m in ios) == 2


def test_multilinea_unido() -> None:
    for ruta in (ANDROID, IOS):
        multilinea = [m for m in parsear_archivo(ruta) if "\n" in m.texto]
        assert len(multilinea) == 1
        assert multilinea[0].autor == YO
        assert multilinea[0].texto == "Lista para mañana:\n- revisar el código\n- comprar café"


def test_muletillas_propias_sin_nombres_de_participantes() -> None:
    for ruta in (ANDROID, IOS):
        perfil = construir_perfil(parsear_archivo(ruta), YO)
        ngramas = [m["ngrama"] for m in perfil["muletillas"]]
        assert "de una" in ngramas
        for texto in _todos_los_ngramas_y_temas(perfil):
            tokens = texto.split()
            assert "ana" not in tokens and "ficticia" not in tokens


def test_main_sin_yo_devuelve_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(ANDROID)]) == 2
    salida = capsys.readouterr().out
    assert "Yo Prueba" in salida and "--yo" in salida


def test_perfil_serializado_sin_nombres_como_palabra_completa() -> None:
    for ruta in (ANDROID, IOS):
        serializado = json.dumps(construir_perfil(parsear_archivo(ruta), YO), ensure_ascii=False)
        for prohibido in ("ana", "ficticia", "yo prueba", "pedro"):
            assert re.search(rf"\b{prohibido}\b", serializado, re.I) is None, prohibido


def test_detectar_nombres_propios_pedro() -> None:
    assert "pedro" in detectar_nombres_propios(["dile a Pedro que sí", "hablé con Pedro"])


# ---------------------------------------------------------------------------
# Casos adicionales
# ---------------------------------------------------------------------------


def test_normalizar_linea() -> None:
    cruda = "﻿‎[14/10/26, 8:02:00 a. m.] Yo: hola‏\r"
    assert normalizar_linea(cruda) == "[14/10/26, 8:02:00 a. m.] Yo: hola"


def test_ampm_orden_y_sistema() -> None:
    mensajes = parsear_lineas([
        "12/10/26, 12:30 a. m. - Yo: medianoche y media",
        "12/10/26, 3:15 p. m. - Yo: tarde",
        "12/10/26, 12:05 p. m. - Ana: mediodía",
        "12/10/26, 21:40 - Ana: formato de 24 horas",
        "12/10/26, 9:00 a. m. - Yo creó el grupo",
    ])
    assert [m.ts.hour for m in mensajes] == [0, 15, 12, 21, 9]
    assert mensajes[0].ts == datetime(2026, 10, 12, 0, 30)    # dmy por defecto (Colombia)
    assert mensajes[-1].sistema and mensajes[-1].autor == ""
    mdy = parsear_lineas(["10/13/26, 8:00 a. m. - Yo: hola"])
    assert mdy[0].ts == datetime(2026, 10, 13, 8, 0)
    forzado = parsear_lineas(["01/02/26, 8:00 a. m. - Yo: hola"], orden_fecha="mdy")
    assert forzado[0].ts == datetime(2026, 1, 2, 8, 0)


def test_lineas_ilegibles_se_saltan() -> None:
    mensajes = parsear_lineas([
        "basura antes de la primera cabecera",
        "31/02/26, 8:00 a. m. - Yo: fecha imposible",
        "continuación de la fecha imposible",
        "14/10/26, 8:00 a. m. - Yo: válido <Se editó este mensaje.>",
    ])
    assert len(mensajes) == 1 and mensajes[0].texto == "válido"


def test_agrupar_rafagas() -> None:
    mensajes = parsear_lineas([
        "14/10/26, 8:00 a. m. - Yo: uno",
        "14/10/26, 8:01 a. m. - Yo: dos",
        "14/10/26, 8:10 a. m. - Yo: tres (otra ráfaga por el hueco)",
        "14/10/26, 8:11 a. m. - Ana: corte",
        "14/10/26, 8:12 a. m. - Yo: cuatro",
    ])
    assert [len(r) for r in agrupar_rafagas(mensajes, "Yo", gap_s=120)] == [2, 1, 1]


def test_extraer_muletillas_sin_ajenos_ordena_por_frecuencia() -> None:
    propios = ["parce de una"] * 6 + ["jajaja parce"] * 2
    muletillas = extraer_muletillas(propios, [], min_freq=5)
    assert muletillas[0] == {"ngrama": "parce", "freq": 8, "lift": 1.0}
    assert {"ngrama": "de una", "freq": 6, "lift": 1.0} in muletillas
    assert all(m["ngrama"] != "una" for m in muletillas)        # stopword sola


def test_main_completo_guarda_perfil_privado(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    salida = tmp_path / "john" / "perfil_conductual.json"
    codigo = main([str(ANDROID), str(IOS), "--yo", "yo prueba", "--salida", str(salida),
                   "--excluir", "Bastante"])
    assert codigo == 0
    impreso = capsys.readouterr().out
    assert impreso.strip().startswith("Perfil generado: 80 mensajes propios,")
    assert "Recomendación: borra los .txt exportados." in impreso
    perfil = cargar_perfil(salida)
    assert perfil is not None and perfil["version"] == 1 and perfil["fuente"]["archivos"] == 2
    assert not salida.with_suffix(".json.tmp").exists()
    texto = salida.read_text(encoding="utf-8")
    assert "chat_android" not in texto and "bastante" not in texto.lower()
    assert set(perfil) == {"version", "generado", "fuente", "estilo", "muletillas", "horario", "temas",
                           "saludos", "despedidas"}
    assert len(perfil["horario"]["histograma"]) == 24 and max(perfil["horario"]["histograma"]) == 1.0


def test_main_errores(tmp_path: Path) -> None:
    assert main([str(ANDROID), "--yo", "Okabe Rintaro", "--salida", str(tmp_path / "p.json")]) == 2
    assert main([str(tmp_path / "no_existe.txt"), "--yo", YO]) == 1
    assert not (tmp_path / "p.json").exists()


def test_parsear_archivo_latin1(tmp_path: Path) -> None:
    ruta = tmp_path / "latin1.txt"
    ruta.write_bytes("14/10/26, 8:00 a. m. - Yo: café y canción\n".encode("latin-1"))
    mensajes = parsear_archivo(ruta)
    assert mensajes[0].texto == "café y canción"


def test_guardar_y_cargar_perfil(tmp_path: Path) -> None:
    ruta = tmp_path / "perfil.json"
    guardar_perfil({"version": 1, "temas": [{"palabra": "código", "freq": 3}]}, ruta)
    assert cargar_perfil(ruta) == {"version": 1, "temas": [{"palabra": "código", "freq": 3}]}
    assert "código" in ruta.read_text(encoding="utf-8")       # ensure_ascii=False
    (tmp_path / "roto.json").write_text("{no es json", encoding="utf-8")
    assert cargar_perfil(tmp_path / "roto.json") is None
    assert cargar_perfil(tmp_path / "inexistente.json") is None


def _mensajes(*filas: tuple[str, int, str]) -> list[MensajeWsp]:
    """``(autor, minuto, texto)`` → mensajes del 1/10/2026 desde las 9:00."""
    return [MensajeWsp(ts=datetime(2026, 10, 1, 9 + minuto // 60, minuto % 60), autor=autor, texto=texto)
            for autor, minuto, texto in filas]


def test_urls_y_correos_no_dejan_fragmentos_en_el_perfil() -> None:
    filas: list[tuple[str, int, str]] = []
    for i in range(6):
        filas += [
            (YO, 10 * i, "mándale el informe a zuluaga.restrepo@empresa.co porfa"),
            (YO, 10 * i + 1, "mira https://www.instagram.com/kurisu_makise_fan/ jaja"),
            (YO, 10 * i + 2, "y el otro está en lab.futuro.com/okabe y @daru_hacker lo sabe"),
            ("Ana Ficticia", 10 * i + 5, "bueno, ya lo reviso más tarde"),
        ]
    perfil = construir_perfil(_mensajes(*filas), YO)
    serializado = json.dumps(perfil, ensure_ascii=False) + resumen_perfil(perfil)
    for prohibido in ("zuluaga", "restrepo", "empresa", "co", "com", "https", "www", "instagram",
                      "kurisu", "makise", "fan", "lab", "futuro", "okabe", "daru", "hacker"):
        assert re.search(rf"\b{prohibido}\b", serializado, re.I) is None, prohibido
    # El resto del texto sí cuenta: "informe" es palabra de contenido (tema, no muletilla).
    assert "informe" in [t["palabra"] for t in perfil["temas"]]
    assert "informe" not in [m["ngrama"] for m in perfil["muletillas"]]
    assert "porfa" in [m["ngrama"] for m in perfil["muletillas"]]


def test_apodo_al_inicio_de_mensaje_no_es_muletilla() -> None:
    filas: list[tuple[str, int, str]] = []
    for i in range(8):
        filas += [
            (YO, 10 * i, f"Juancho ya llegó a la casa número {i}"),
            (YO, 10 * i + 1, "Bacano, de una"),                          # autocorrector al abrir frase
            ("Ana Ficticia", 10 * i + 3, f"ok, todo bien por acá {i}"),
        ]
    filas += [(YO, 100 + i, "qué bacano todo") for i in range(3)]        # en minúscula en mitad de frase
    perfil = construir_perfil(_mensajes(*filas), YO)
    serializado = json.dumps(perfil, ensure_ascii=False) + resumen_perfil(perfil)
    assert re.search(r"\bjuancho\b", serializado, re.I) is None
    assert "bacano" in [m["ngrama"].lower() for m in perfil["muletillas"]]


def test_varios_chats_no_mezclan_latencias_ni_rafagas(tmp_path: Path) -> None:
    chat_a = _mensajes(("Ana Ficticia", 0, "hola"), (YO, 10, "quiubo"), ("Ana Ficticia", 20, "chao, me voy"))
    chat_b = _mensajes((YO, 21, "uno"), (YO, 21, "dos"), ("Carlos", 30, "tres"))
    separados = construir_perfil(chat_a + chat_b, YO, cortes=[len(chat_a)])
    assert separados["estilo"]["latencia_respuesta_mediana_s"] == 600      # solo la de dentro del chat A
    assert separados["fuente"]["archivos"] == 2
    unidos = construir_perfil(chat_a + chat_b, YO)                         # sin cortes: un solo chat
    assert unidos["estilo"]["latencia_respuesta_mediana_s"] == 330

    chat_c = _mensajes(("Ana Ficticia", 0, "hola"), (YO, 1, "uno"))
    chat_d = _mensajes((YO, 2, "dos"), ("Carlos", 3, "tres"))
    rafagas = construir_perfil(chat_c + chat_d, YO, cortes=[len(chat_c)])["estilo"]
    assert rafagas["mensajes_por_rafaga"] == 1.0 and rafagas["rafaga_mediana_s"] == 0

    archivo_c = tmp_path / "c.txt"
    archivo_c.write_text("01/10/26, 9:00 a. m. - Ana Ficticia: hola\n"
                         "01/10/26, 9:01 a. m. - Yo Prueba: uno\n", encoding="utf-8")
    archivo_d = tmp_path / "d.txt"
    archivo_d.write_text("01/10/26, 9:02 a. m. - Yo Prueba: dos\n"
                         "01/10/26, 9:03 a. m. - Carlos: tres\n", encoding="utf-8")
    salida = tmp_path / "perfil.json"
    assert main([str(archivo_c), str(archivo_d), "--yo", YO, "--salida", str(salida)]) == 0
    perfil = cargar_perfil(salida)
    assert perfil is not None and perfil["estilo"]["mensajes_por_rafaga"] == 1.0
    assert perfil["fuente"]["archivos"] == 2


def test_resumen_perfil() -> None:
    perfil = construir_perfil(parsear_archivo(ANDROID), YO)
    resumen = resumen_perfil(perfil)
    assert 0 < len(resumen) <= 600
    assert resumen.startswith("Escribe en ráfagas cortas (~")
    assert "«de una»" in resumen
    assert re.search(r"\bpedro\b", resumen, re.I) is None
    assert len(resumen_perfil(perfil, max_chars=80)) <= 80
    assert resumen_perfil({}) == "Perfil heredado con pocos datos."


# ---------------------------------------------------------------------------
# Muletillas = marcadores discursivos (no palabras de tema) y teclazos agrupados
# ---------------------------------------------------------------------------

_PALABRAS_DE_TEMA = ("dinero", "precios", "economía", "intercambio", "bienes", "propiedad", "ocupado")
_VARIANTES_TECLAZO = ("kakfkak", "kakgkak", "kakfka")


def test_palabras_de_tema_no_son_muletillas_y_quedan_en_temas() -> None:
    perfil = construir_perfil(parsear_archivo(MULETILLAS), YO)
    ngramas = [m["ngrama"] for m in perfil["muletillas"]]
    for tema in _PALABRAS_DE_TEMA:                       # frecuentes y con lift alto, pero de contenido
        assert all(tema not in ngrama.split() for ngrama in ngramas), tema
    assert "estaba ocupado" not in ngramas and "la propiedad" not in ngramas
    assert {"bueno", "bueno bueno", "hmm", "na", "claro", "jaja", "o sea", "la verdad", "tipo"} <= set(ngramas)
    assert "este" not in ngramas                         # demostrativo ("este precio"), no muletilla
    temas = {t["palabra"] for t in perfil["temas"]}
    assert set(_PALABRAS_DE_TEMA) <= temas
    assert not temas & {"bueno", "tipo", "verdad", "hmm", "hmmm", *_VARIANTES_TECLAZO}


def test_teclazos_agrupados_en_una_entrada_y_en_estilo_risa() -> None:
    perfil = construir_perfil(parsear_archivo(MULETILLAS), YO)
    teclazos = [m for m in perfil["muletillas"] if m["ngrama"].startswith(ETIQUETA_TECLAZO)]
    assert [(m["ngrama"], m["freq"]) for m in teclazos] == [("risa de teclazo (kakfkak)", 6)]   # 3 + 2 + 1
    assert teclazos[0]["lift"] >= 1.5
    # estilo.risa conserva sus cuatro claves (§10.9); el conteo de teclazos va aparte.
    assert perfil["estilo"]["risa"] == {"jaja": 6, "jeje": 0, "jsjs": 0, "xd": 0}
    assert perfil["estilo"][CLAVE_RISA_TECLAZO] == 6
    for variante in _VARIANTES_TECLAZO:                  # ninguna variante suelta ni dentro de un n-grama
        assert all(variante not in m["ngrama"].split() for m in perfil["muletillas"])

    # Un token excluido a mano nunca cuenta como teclazo ni aparece en el perfil.
    excluido = construir_perfil(parsear_archivo(MULETILLAS), YO, excluir_extra=["Kakfkak"])
    assert excluido["estilo"][CLAVE_RISA_TECLAZO] == 3                            # 2 + 1, por debajo de 5
    assert not [m for m in excluido["muletillas"] if m["ngrama"].startswith(ETIQUETA_TECLAZO)]
    assert "kakfkak" not in json.dumps(excluido, ensure_ascii=False)


def test_extraer_muletillas_agrupa_teclazos_y_alargamientos() -> None:
    propios = (["kakfkak"] * 3 + ["jaja kakgkak"] * 2 + ["asdfgh"] + ["hmm"] * 3 + ["mmm"] * 2 + ["hmmm"]
               + ["siii"] * 3 + ["sii"] * 2 + ["buenooo"] * 2 + ["bueno"] * 3 + ["salsa kafka"] * 6
               + ["lee el informe"] * 5)
    muletillas = {m["ngrama"]: m["freq"] for m in extraer_muletillas(propios, [], min_freq=5)}
    assert muletillas == {"risa de teclazo (kakfkak)": 6, "hmm": 6, "siii": 5, "bueno": 5}


def test_ejemplo_de_teclazo_solo_si_la_variante_se_repite() -> None:
    propios = ["kakfkak", "kakgkak", "asdfgh", "skjsks", "ajsjaj"]      # todas distintas: sin ejemplo
    assert extraer_muletillas(propios, [], min_freq=5) == [{"ngrama": ETIQUETA_TECLAZO, "freq": 5, "lift": 1.0}]


_PRESTAMOS_DEL_INGLES = ("gmail", "tasks", "skills", "calls", "snacks", "packs", "hacks", "fields", "health",
                         "assign", "graph", "graphs", "falls", "walls", "balls", "folks", "stacks", "decks",
                         "locks", "cells", "mills", "salts", "harsh", "flasks", "drafts", "apps", "halls",
                         "lambda")


def test_es_teclazo_distingue_palabras_reales() -> None:
    from core.wsp_ingestor import _es_teclazo
    for teclazo in ("kakfkak", "kakgkak", "kakfka", "asdfgh", "skjsks", "ajsjaj", "jajsjajs", "ksks", "asdf",
                    "jaaaaaaaaaksjd", "wkwkwk"):
        assert _es_teclazo(teclazo), teclazo
    for palabra in ("salsa", "falda", "kafka", "alaska", "hadas", "flash", "hashtag", "whatsapp", "nvidia",
                    "drivers", "precios", "propiedad", "slds", "jaja", *_PRESTAMOS_DEL_INGLES):
        assert not _es_teclazo(palabra), palabra


def test_prestamos_del_ingles_quedan_en_temas_y_no_son_risa() -> None:
    filas: list[tuple[str, int, str]] = []
    for i in range(5):
        filas += [(YO, 10 * i, "te mando el link al gmail"), (YO, 10 * i + 1, "las tasks del sprint"),
                  (YO, 10 * i + 2, "hagamos las calls mañana"), (YO, 10 * i + 3, "compré snacks jaja"),
                  ("Ana Ficticia", 10 * i + 5, "dale, de acuerdo")]
    perfil = construir_perfil(_mensajes(*filas), YO)
    assert not [m for m in perfil["muletillas"] if m["ngrama"].startswith(ETIQUETA_TECLAZO)]
    assert perfil["estilo"][CLAVE_RISA_TECLAZO] == 0
    assert {"gmail", "tasks", "calls", "snacks"} <= {t["palabra"] for t in perfil["temas"]}
    assert "teclazo" not in resumen_perfil(perfil)


def test_es_teclazo_sin_vuelta_atras_exponencial() -> None:
    """El silabeo es lineal: un grito más un teclazo ("JAAAA…KSJD") no cuelga el ingestor."""
    import time

    from core.wsp_ingestor import _es_teclazo
    for token in ("j" + "a" * 30 + "ksjd", "a" * 26 + "hdk", "jaaaaaaaaaaaaaaaaaaaaaaaaaaksjd",
                  "a" * 21 + "hdjsk", "a" * 39 + "h", "asdfghjkl" * 5):
        _es_teclazo.cache_clear()
        inicio = time.perf_counter()
        _es_teclazo(token)
        assert time.perf_counter() - inicio < 0.05, token
    # De punta a punta, aunque el grito lo escriba la otra persona (los ajenos también se tokenizan).
    _es_teclazo.cache_clear()
    inicio = time.perf_counter()
    construir_perfil(_mensajes((YO, 0, "hola"), ("Ana Ficticia", 1, "JAAAAAAAAAAAAAAAAAAAAAAAAAKSJD"),
                               (YO, 2, "jajaja")), YO)
    assert time.perf_counter() - inicio < 1.0


def test_ngramas_solo_de_marcadores() -> None:
    propios = (["o sea, no sé"] * 5 + ["la verdad, bueno bueno"] * 5 + ["sí sí, estaba ocupado"] * 5
               + ["bueno, el dinero no alcanza"] * 5 + ["bueno, ya llegué"] * 5)
    ngramas = {m["ngrama"] for m in extraer_muletillas(propios, [], min_freq=5)}
    assert {"o sea", "no sé", "la verdad", "bueno bueno", "sí sí", "bueno"} <= ngramas
    for intruso in ("estaba ocupado", "ocupado", "dinero", "el dinero", "alcanza", "verdad bueno",
                    "bueno ya", "ya", "sí estaba", "sea no"):
        assert intruso not in ngramas, intruso


def test_marcadores_ambiguos_solo_cuentan_sueltos() -> None:
    propios = (["este precio está caro"] * 6 + ["ya llegué a la casa"] * 6 + ["es igual de caro"] * 6
               + ["este... no sé"] * 5 + ["ya ya"] * 3 + ["listo, ya"] * 2 + ["tipo, ni idea"] * 5
               + ["entonces qué"] * 5)
    muletillas = {m["ngrama"]: m["freq"] for m in extraer_muletillas(propios, [], min_freq=5)}
    assert muletillas["este"] == 5 and muletillas["ya"] == 8
    assert muletillas["tipo"] == 5 and muletillas["entonces"] == 5
    assert not {"igual", "precio", "caro", "casa", "llegué"} & set(muletillas)


def test_alargamiento_solo_con_la_letra_final_o_racha_larga() -> None:
    from core.wsp_ingestor import _clave_base
    for alargado, base in (("sii", "si~"), ("noo", "no~"), ("yaa", "ya~"), ("estee", "este~"), ("tipoo", "tipo~"),
                           ("shh", "sh~"), ("shhh", "sh~"), ("grrr", "gr~"), ("queee", "que~"), ("peeero", "pero~")):
        assert _clave_base(alargado) == base, alargado
    for palabra in ("valle", "vallee", "pillas", "ssh", "mirra", "lee", "quee"):    # palabras, no alargamientos
        assert not _clave_base(palabra).endswith("~"), palabra
    propios = (["vivo en el valle", "el valle es bonito"] * 3 + ["me pillas?"] * 5
               + ["conéctate por ssh al server", "la llave ssh no sirve", "el ssh se cayó"] * 2)
    assert extraer_muletillas(propios, [], min_freq=5) == []


def test_expresiones_coloquiales_solo_en_su_contexto() -> None:
    falsos = (["el precio de una casa", "la mamá de una amiga"] * 3 + ["no se puede", "no se dio"] * 3
              + ["voy a ver la serie", "fui a ver el partido"] * 3 + ["lo que más me gusta"] * 5
              + ["dime la verdad", "lo malo es que llueve", "así es la vida"] * 2)
    assert extraer_muletillas(falsos, [], min_freq=5) == []
    reales = (["no sé", "no se si ir", "no se que hacer"] * 2 + ["a ver, qué pasó", "bueno a ver"] * 3
              + ["de una parce", "sí, de una", "de una me voy"] * 2 + ["hola que más", "qué más pues"] * 3
              + ["la verdad no sé"] * 2 + ["pero la verdad sí"] * 3 + ["es que no puedo", "así es"] * 5)
    ngramas = {m["ngrama"] for m in extraer_muletillas(reales, [], min_freq=5)}
    assert {"no sé", "a ver", "de una", "que más", "la verdad", "es que", "así es"} <= ngramas


def test_siglas_unidades_y_uso_nominal_no_son_muletillas() -> None:
    falsos = (["tornillo de 10 mm"] * 5 + ["juego en la ps5", "la PS4 está barata"] * 3
              + ["llegó por ups", "el ups del pc"] * 3 + ["pagué con el pos", "el pos no sirve"] * 3
              + ["lo bueno es que llegó"] * 5)
    assert extraer_muletillas(falsos, [], min_freq=5) == []
    reales = ["ps sí"] * 5 + ["mm no sé"] * 5 + ["ups, perdón"] * 5 + ["pos sí"] * 5
    ngramas = {m["ngrama"] for m in extraer_muletillas(reales, [], min_freq=5)}
    assert {"ps", "mm", "ups", "pos"} <= ngramas


def test_igual_y_total_con_cantidad_o_comparacion_no_son_muletillas() -> None:
    falsos = ["total: 50 mil", "total 80 mil", "total a pagar 30", "total, 20 mil", "total de la cuenta",
              "igual que ayer", "igual a la otra", "igual de caro", "igual que siempre", "igual a mi casa"]
    assert extraer_muletillas(falsos, [], min_freq=5) == []
    reales = ["igual no importa", "total que no fui", "igual, ya fue", "total, no fui"] * 3
    muletillas = {m["ngrama"]: m["freq"] for m in extraer_muletillas(reales, [], min_freq=5)}
    assert muletillas["igual"] == 6 and muletillas["total"] == 6


def test_muletillas_mantienen_el_lift_frente_a_ajenos() -> None:
    propios = ["bueno, listo"] * 6 + ["claro"] * 6
    ajenos = ["bueno, listo"] * 6 + ["vale"] * 6
    ngramas = {m["ngrama"] for m in extraer_muletillas(propios, ajenos, min_freq=5)}
    assert "claro" in ngramas
    assert not {"bueno", "listo", "bueno listo"} & ngramas              # también los usa la otra persona


def test_resumen_perfil_con_risa_de_teclazo() -> None:
    perfil = construir_perfil(parsear_archivo(MULETILLAS), YO)
    resumen = resumen_perfil(perfil)
    assert "Ríe con «jaja» y a teclazos («kakfkak»)." in resumen
    lista = resumen.split("Muletillas: ", 1)[1].split(".", 1)[0]
    assert "«bueno»" in lista and "teclazo" not in lista and "«jaja»" not in lista
    assert re.search(r"\b(dinero|precios|ocupado)\b", lista) is None

    sintetico = {"estilo": {"risa": {"jaja": 3, "jeje": 0, "jsjs": 0, "xd": 1}, CLAVE_RISA_TECLAZO: 9},
                 "muletillas": [{"ngrama": "risa de teclazo (asdfgh)", "freq": 9, "lift": 2.0},
                                {"ngrama": "parce", "freq": 6, "lift": 2.0}]}
    assert resumen_perfil(sintetico) == "Muletillas: «parce». Ríe a teclazos («asdfgh») y con «jaja»."
    sintetico["estilo"] = {"risa": {"jaja": 0}, CLAVE_RISA_TECLAZO: 4}
    sintetico["muletillas"] = [{"ngrama": "risa de teclazo", "freq": 4, "lift": 2.0}]
    assert resumen_perfil(sintetico) == "Ríe a teclazos."
    suelto = {"estilo": {"risa": {"jaja": 5}, CLAVE_RISA_TECLAZO: 1}}         # un teclazo suelto no basta
    assert resumen_perfil(suelto) == "Ríe con «jaja»."
    antiguo = {"estilo": {"risa": {"jaja": 5, "jeje": 0, "jsjs": 0, "xd": 2}}}  # perfil sin teclazos
    assert resumen_perfil(antiguo) == "Ríe con «jaja»."
    intermedio = {"estilo": {"risa": {"jaja": 3, "teclazo": 9}}}                # teclazos dentro de estilo.risa
    assert resumen_perfil(intermedio) == "Ríe a teclazos y con «jaja»."


def test_consumidores_no_toman_el_teclazo_por_una_risa_escrita(tmp_path: Path, settings_prueba) -> None:
    from core.memory_engine import hechos_desde_perfil
    from lobes.broca_llm import BrocaLLM

    ruta = tmp_path / "perfil_conductual.json"
    guardar_perfil(construir_perfil(parsear_archivo(MULETILLAS), YO), ruta)
    perfil = cargar_perfil(ruta)
    assert perfil is not None and perfil["estilo"][CLAVE_RISA_TECLAZO] == 6
    hechos = hechos_desde_perfil(perfil, "Okabe")
    assert 3 <= len(hechos) <= 6
    assert any("«risa de teclazo (kakfkak)»" in h for h in hechos)
    assert all("dinero" not in h for h in hechos if h.startswith("Muletillas"))
    prompt = BrocaLLM(settings_prueba, perfil_resumen=resumen_perfil(perfil)).construir_system_prompt()
    assert "Ríe con «jaja» y a teclazos («kakfkak»)." in prompt

    # Con más teclazos que jajas, la "risa escrita más habitual" sigue siendo una risa real.
    perfil["estilo"][CLAVE_RISA_TECLAZO] = 30
    hechos = hechos_desde_perfil(perfil, "Okabe")
    assert not any("teclazo»" in h for h in hechos)
    assert any("Su risa escrita más habitual es «jaja»." == h for h in hechos)

    # Un perfil de la versión anterior (teclazos dentro de estilo.risa) se migra al cargarlo.
    antiguo = tmp_path / "perfil_antiguo.json"
    antiguo.write_text(json.dumps({"version": 1, "estilo": {"risa": {"jaja": 1, "jeje": 0, "jsjs": 0, "xd": 0,
                                                                     "teclazo": 10}}}), encoding="utf-8")
    migrado = cargar_perfil(antiguo)
    assert migrado is not None
    assert migrado["estilo"] == {"risa": {"jaja": 1, "jeje": 0, "jsjs": 0, "xd": 0}, CLAVE_RISA_TECLAZO: 10}
    assert "Su risa escrita más habitual es «jaja»." in hechos_desde_perfil(migrado, "Okabe")
    assert resumen_perfil(migrado) == "Ríe a teclazos y con «jaja»."
