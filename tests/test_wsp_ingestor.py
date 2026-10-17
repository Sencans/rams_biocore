"""Tests de core/wsp_ingestor.py con chats sintéticos (grupo memory, §10.9 y §12.3).

Los chats de ``tests/fixtures`` son 100 % sintéticos ("Yo Prueba" y "Ana Ficticia", con el
tercero "Pedro" mencionado por "Yo Prueba"). Sin red ni hardware.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from core.wsp_ingestor import (
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
    assert "informe" in [m["ngrama"] for m in perfil["muletillas"]]      # el resto del texto sí cuenta


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
