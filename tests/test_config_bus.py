"""Tests de core/config.py, core/bus.py y core/contracts.py (grupo config-bus, §12.3).

Sin red ni hardware. Sin pytest-asyncio: las pruebas asíncronas usan ``asyncio.run`` (fixture
``correr`` de tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import os
import subprocess
import sys
import threading
import time
import types
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core
import core.config as config
from core.bus import BusEventos, Evento, instalar_manejador_log
from core.config import (
    FiltroSecretos,
    Settings,
    ahora_local,
    cargar_settings,
    carpetas_usuario,
    configurar_logging,
    en_horario_silencioso,
    leer_env_archivo,
    normalizar_titulo,
    parece_secreto,
    parsear_env_texto,
    redactar,
    titulo_sensible,
    zona_local,
)
from core.contracts import (
    ACCIONES_ORDEN_ESCRITORIO,
    BOTONES_TARJETA,
    CANALES_ENTRADA,
    CANALES_LOCALES,
    CANALES_SALIDA,
    CARPETAS_USUARIO,
    CATEGORIAS_RIESGO,
    CLASES_TARJETA,
    CONDUCTAS_SOLICITUD,
    CONTROLES_ESCRITORIO,
    CORREO_RE,
    DECISIONES_TARJETA,
    DESTINOS_BASE,
    DESTINOS_DESCARGA,
    DIVERGENCIA_STEINS_GATE,
    ESTADOS_TARJETA,
    ESTADOS_VISION,
    EVENTOS_ESCRITORIO,
    FORMATOS_DRIVE,
    HUELLA_RE,
    ID_TARJETA_RE,
    INICIATIVAS,
    LENGUAJES_SCRIPT,
    MAX_ADJUNTOS_CORREO,
    MAX_ASUNTO_CORREO,
    MAX_BYTES_DECISION_TARJETA,
    MAX_BYTES_TARJETA,
    MAX_CODIGO_SCRIPT,
    MAX_CUERPO_CORREO,
    MAX_HALLAZGOS_TARJETA,
    MAX_SALIDA_TARJETA,
    MAX_TARJETAS_VIVAS,
    MODOS_PANTALLA,
    MODOS_SALIDA,
    NIVELES_EJECUTABLES,
    NIVELES_RIESGO,
    OPERACIONES_DRIVE,
    ORIGENES_ACCION,
    PATRON_ARCHIVO_SENSIBLE,
    PATRON_TITULO_SENSIBLE,
    PATRONES_TRAVESURA,
    RESULTADOS_TARJETA,
    ROLES_DRIVE,
    SAFE_ALIAS_RE,
    SERVICIOS_GOOGLE,
    TIPOS_ACCION,
    TIPOS_ACCION_V02,
    TTL_DOBLE_CONFIRMACION_S,
    TTL_TARJETA_S,
    TTL_TARJETA_TELEGRAM_S,
    BrocaReply,
    ContextoBroca,
    DiagnosticoModulo,
    EstadoModulo,
    EstadoPresencia,
    Etiquetas,
    HomeostaticState,
    ProveedorHypr,
    RegistroLlamadas,
    Segmento,
    SalidaAvatar,
    Telemetria,
    Utterance,
    banda,
    bytes_json,
    formatear_decimal,
    huella_contenido,
    largo_utf16,
    resumen_nexus,
)

TOKEN_TG = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
URL_TG = f"POST https://api.telegram.org/bot{TOKEN_TG}/sendMessage"


def _avisos_config(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "rams.config" and r.levelno >= logging.WARNING]


def _quitar_handlers(raiz: logging.Logger, tipos: tuple[str, ...]) -> None:
    for handler in list(raiz.handlers):
        if getattr(handler, "_rams", False) and getattr(handler, "_rams_tipo", "") in tipos:
            raiz.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------------------------
# core/__init__.py
# ---------------------------------------------------------------------------


def test_paquete_core_version() -> None:
    assert core.__version__ == "0.1.0"
    assert core.NOMBRE == "R.A.M.S. BIO-CORE"
    assert config.VERSION == core.__version__


# ---------------------------------------------------------------------------
# Settings: valores por defecto, tipos y regla única de ausencia
# ---------------------------------------------------------------------------


def test_valores_por_defecto() -> None:
    s = cargar_settings(entorno={})
    assert s.elevenlabs_voice_id == "p7sm2zw5Us66lZr2brDW"
    assert s.gemini_model == "gemini-3.8-flash"
    assert s.gemini_fallback_models == ("gemini-3.6-flash", "gemini-3.5-flash-lite")
    assert s.camara_habilitada is False
    assert s.rango_lab == "Lab Mem 001"
    assert s.nombre_usuario is None
    assert s.trato_usuario == "Lab Mem 001"
    assert s.nombre_ia == "Amadeus"
    assert s.zona_horaria == "America/Bogota"
    assert s.hypr_modo_dispatch == "auto"
    assert s.idle_flag_path is None
    assert s.elevenlabs_sample_rate == 24000
    assert s.conectoma_fuente == "reducido"
    assert s.conectoma_semilla is None
    assert s.max_proactivos_dia == 3 and s.max_interrupciones_hora == 4
    assert s.camara_max_dia == 40 and s.camara_intervalo_min_s == 90.0
    assert s.silencio_inicio == "23:00" and s.silencio_fin == "07:00"
    assert s.mensajero == "hibrido"   # RAMS_MENSAJERO (CONTRATO de cierre): Amadeus llama, Megumin insiste
    assert s.perfil_en_prompt is True and s.mcp_memoria is False
    assert s.apps_permitidas_items == (("terminal", "kitty"), ("navegador", "firefox"),
                                       ("nexus", "nexus-ide"))
    assert s.memoria_dir == s.data_dir / "john"
    assert s.db_path == s.memoria_dir / "rams_memory.db"
    assert s.log_dir == s.data_dir / "logs"
    assert s.base_dir == Path(config.__file__).resolve().parent.parent


def test_fixture_settings_prueba(settings_prueba: Settings, tmp_path: Path) -> None:
    assert settings_prueba.offline is True
    assert settings_prueba.hud_puerto == 0 and settings_prueba.ctl_puerto == 0
    assert settings_prueba.conectoma_semilla == 7
    assert settings_prueba.data_dir == tmp_path / "data"
    assert settings_prueba.memoria_dir.is_dir() and settings_prueba.log_dir.is_dir()
    assert (settings_prueba.data_dir / "models").is_dir()


@pytest.mark.parametrize("valor", ["1", "true", "T", "si", "Sí", "s", "YES", "y", "on"])
def test_booleanos_verdaderos(valor: str) -> None:
    assert cargar_settings(entorno={"RAMS_CAMERA": valor}).camara_habilitada is True


@pytest.mark.parametrize("valor", ["0", "false", "F", "no", "N", "off", "OFF"])
def test_booleanos_falsos(valor: str) -> None:
    assert cargar_settings(entorno={"RAMS_VOICE": valor}).voz_habilitada is False


def test_booleano_invalido_usa_defecto_y_avisa(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={"RAMS_VOICE": "quizá-mañana"})
    assert s.voz_habilitada is True
    avisos = _avisos_config(caplog)
    assert any("RAMS_VOICE" in r.getMessage() for r in avisos)
    assert all("quizá-mañana" not in r.getMessage() for r in avisos)


def test_entero_invalido_no_lanza(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={"RAMS_HUD_PORT": "ochenta", "RAMS_CAMERA_INDEX": "-3",
                                     "RAMS_CTL_PORT": "70000", "GEMINI_TIMEOUT_S": "nan",
                                     "RAMS_CAMERA_MIN_INTERVAL_S": "45,5",
                                     "RAMS_CONNECTOME_SEED": "semilla"})
    assert s.hud_puerto == 8765
    assert s.camara_indice == 0
    assert s.ctl_puerto == 8766
    assert s.gemini_timeout_s == 30.0
    assert s.camara_intervalo_min_s == 45.5          # coma decimal aceptada
    assert s.conectoma_semilla is None
    mensajes = " ".join(r.getMessage() for r in _avisos_config(caplog))
    assert "RAMS_HUD_PORT" in mensajes and "ochenta" not in mensajes


def test_otros_formatos_validados() -> None:
    s = cargar_settings(entorno={
        "ELEVENLABS_OUTPUT": "pcm_16000", "RAMS_QUIET_START": "7:05", "GEMINI_THINKING_LEVEL": "HIGH",
        "HYPR_DISPATCH_MODE": "Lua", "RAMS_LOG_LEVEL": "debug", "RAMS_CONNECTOME": "stub",
        "GEMINI_FALLBACK_MODELS": "gemini-x, ,gemini-y,gemini-x", "RAMS_USER_NAME": "Rintaro",
    })
    assert s.elevenlabs_sample_rate == 16000
    assert s.silencio_inicio == "07:05"
    assert s.gemini_thinking_level == "high"
    assert s.hypr_modo_dispatch == "lua"
    assert s.log_nivel == "DEBUG"
    assert s.conectoma_fuente == "stub"
    assert s.gemini_fallback_models == ("gemini-x", "gemini-y")
    assert s.trato_usuario == "Rintaro"
    malo = cargar_settings(entorno={"ELEVENLABS_OUTPUT": "mp3_44100_128", "RAMS_QUIET_END": "25:00",
                                    "HYPR_DISPATCH_MODE": "wayland", "VTS_PLUGIN_NAME": "ab"})
    assert malo.elevenlabs_sample_rate == 24000
    assert malo.silencio_fin == "07:00"
    assert malo.hypr_modo_dispatch == "auto"
    assert malo.vts_plugin_nombre == "RAMS BioCore"


def test_regla_unica_de_ausencia() -> None:
    s = cargar_settings(entorno={"RAMS_LAB_RANK": "   ", "RAMS_AI_NAME": "# sin nombre",
                                 "HYPR_DISPATCH_MODE": "", "RAMS_HUD_PORT": "#8000",
                                 "RAMS_USER_NAME": "", "RAMS_CAMERA": "  # comentario"})
    assert s.rango_lab == "Lab Mem 001"
    assert s.nombre_ia == "Amadeus"
    assert s.hypr_modo_dispatch == "auto"
    assert s.hud_puerto == 8765
    assert s.nombre_usuario is None
    assert s.camara_habilitada is False


def test_alias_telegram_chat_id() -> None:
    token = "123456789:" + "b" * 35
    s = cargar_settings(entorno={"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "-1001234"})
    assert s.telegram_chat_id == "-1001234"
    assert s.tiene_telegram is True
    ambos = cargar_settings(entorno={"TELEGRAM_BOT_TOKEN": token, "CHAT_ID": "42",
                                     "TELEGRAM_CHAT_ID": "99"})
    assert ambos.telegram_chat_id == "42"   # CHAT_ID manda; el alias solo cubre su ausencia


def test_offline_desactiva_capacidades(settings_con_claves: Settings) -> None:
    s = settings_con_claves
    assert s.tiene_gemini and s.tiene_elevenlabs and s.tiene_edge_tts
    assert s.tiene_telegram and s.tiene_brave and s.tiene_web
    off = s.con(offline=True)
    assert not any((off.tiene_gemini, off.tiene_elevenlabs, off.tiene_edge_tts,
                    off.tiene_telegram, off.tiene_brave, off.tiene_web))
    por_entorno = cargar_settings(entorno={"GEMINI_API_KEY": "clave-larga-123", "RAMS_OFFLINE": "1"})
    assert por_entorno.offline is True and por_entorno.tiene_gemini is False
    forzado = cargar_settings(entorno={"RAMS_OFFLINE": "1"}, offline=False)
    assert forzado.offline is False
    sin_voz = s.con(voz_habilitada=False)
    assert sin_voz.tiene_elevenlabs is False and sin_voz.tiene_edge_tts is False


TEXTO_ENV = "TELEGRAM_BOT_TOKEN=            # crea un bot\nCHAT_ID=   # tu chat\nRAMS_IDLE_FLAG=\n"


@pytest.mark.parametrize("lector", ["dotenv", "minimo"])
def test_parser_comentarios_tras_igual(lector: str, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch,
                                       caplog: pytest.LogCaptureFixture) -> None:
    if lector == "dotenv":
        pytest.importorskip("dotenv")
        assert config.dotenv_values is not None
    else:
        monkeypatch.setattr(config, "dotenv_values", None)
    ruta = tmp_path / ".env"
    ruta.write_text(TEXTO_ENV, encoding="utf-8")
    leido = leer_env_archivo(ruta)
    assert set(leido) == {"TELEGRAM_BOT_TOKEN", "CHAT_ID", "RAMS_IDLE_FLAG"}
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={**leido, "XDG_RUNTIME_DIR": "/run/user/1000"})
    assert s.tiene_telegram is False
    assert s.telegram_bot_token is None and s.telegram_chat_id is None
    assert s.idle_flag_path == Path("/run/user/1000/rams_idle")
    assert s.hypr_modo_dispatch == "auto"
    assert _avisos_config(caplog) == []


def test_parser_minimo_imita_a_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "dotenv_values", None)
    texto = (
        "# comentario\n"
        "\n"
        "export RAMS_AI_NAME=Amadeus  # con comentario\n"
        "RAMS_LAB_RANK=Lab Mem 001\n"
        "COMILLAS_DOBLES=\"valor # no es comentario\"  # este sí\n"
        "COMILLAS_SIMPLES='a\\'b'\n"
        "ESCAPES=\"l1\\nl2\"\n"
        "SIN_ESPACIO=valor#pegado\n"
        "MULTILINEA=\"linea1\n"
        "linea2\"\n"
        "  ESPACIADA = hola  \n"
        "SOLO_CLAVE\n"
        "VACIA=\n"
    )
    ruta = tmp_path / ".env"
    ruta.write_text(texto, encoding="utf-8")
    leido = leer_env_archivo(ruta)
    assert leido["RAMS_AI_NAME"] == "Amadeus"
    assert leido["RAMS_LAB_RANK"] == "Lab Mem 001"
    assert leido["COMILLAS_DOBLES"] == "valor # no es comentario"
    assert leido["COMILLAS_SIMPLES"] == "a'b"
    assert leido["ESCAPES"] == "l1\nl2"
    assert leido["SIN_ESPACIO"] == "valor#pegado"
    assert leido["MULTILINEA"] == "linea1\nlinea2"
    assert leido["ESPACIADA"] == "hola"
    assert leido["SOLO_CLAVE"] == "" and leido["VACIA"] == ""
    assert parsear_env_texto(texto) == leido


def test_leer_env_inexistente_devuelve_vacio(tmp_path: Path) -> None:
    assert leer_env_archivo(tmp_path / "no_existe.env") == {}
    assert leer_env_archivo(tmp_path) == {}


def test_cargar_settings_desde_archivo_sin_sobrescribir_entorno(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("RAMS_LAB_RANK=Desde Archivo\nRAMS_AI_NAME=Kurisu\n",
                                   encoding="utf-8")
    monkeypatch.setenv("RAMS_AI_NAME", "Amadeus Entorno")
    monkeypatch.delenv("RAMS_LAB_RANK", raising=False)
    antes = dict(os.environ)
    s = cargar_settings(base_dir=tmp_path)
    assert s.rango_lab == "Desde Archivo"
    assert s.nombre_ia == "Amadeus Entorno"        # os.environ manda (override=False)
    assert dict(os.environ) == antes               # no modifica os.environ
    assert s.base_dir == tmp_path


def test_validacion_formatos_de_secretos(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={"TELEGRAM_BOT_TOKEN": "abc", "CHAT_ID": "12a",
                                     "GEMINI_API_KEY": "corta", "BRAVE_API_KEY": "con espacio dentro"})
    assert s.telegram_bot_token is None
    assert s.telegram_chat_id is None
    assert s.gemini_api_key is None and s.brave_api_key is None
    mensajes = [r.getMessage() for r in _avisos_config(caplog)]
    assert any("TELEGRAM_BOT_TOKEN" in m for m in mensajes)
    assert any("CHAT_ID" in m for m in mensajes)
    assert all("abc" not in m and "12a" not in m and "corta" not in m for m in mensajes)
    bueno = cargar_settings(entorno={"TELEGRAM_BOT_TOKEN": "1234567:" + "x" * 30, "CHAT_ID": "-77"})
    assert bueno.telegram_bot_token is not None and bueno.telegram_chat_id == "-77"


def test_settings_hashable_e_inmutable() -> None:
    s = cargar_settings(entorno={})
    assert isinstance(hash(s), int)
    assert hash(s) == hash(cargar_settings(entorno={}))
    assert {s: "ok"}[cargar_settings(entorno={})] == "ok"
    with pytest.raises(AttributeError):
        s.offline = True  # type: ignore[misc]


def test_repr_y_resumen_sin_secretos(settings_con_claves: Settings) -> None:
    s = settings_con_claves
    texto = repr(s)
    for secreto in s.secretos():
        if len(secreto) >= 6:
            assert secreto not in texto
    assert "test-gemini-key" not in texto and "TEST_token" not in texto
    resumen = s.resumen_seguro()
    volcado = json.dumps(resumen, ensure_ascii=False)
    assert "test-gemini-key" not in volcado and "TEST_token" not in volcado
    assert resumen["gemini_api_key"] == "configurada"
    assert resumen["nexus_colab_token"] == "ausente"
    assert resumen["tiene_telegram"] is True


def test_secretos_apps_y_con(settings_con_claves: Settings) -> None:
    s = settings_con_claves
    assert set(s.secretos()) == {"test-gemini-key-000000", "test-eleven-key-000000",
                                 "123456789:TEST_token_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "42",
                                 "test-brave-key-000000"}
    apps = s.apps_permitidas
    assert isinstance(apps, types.MappingProxyType)
    assert apps["terminal"] == "kitty"
    with pytest.raises(TypeError):
        apps["rm"] = "rm"  # type: ignore[index]
    otro = s.con(apps_permitidas_items=(("terminal", "foot"),))
    assert dict(otro.apps_permitidas) == {"terminal": "foot"}
    assert s.apps_permitidas["terminal"] == "kitty"


def test_rams_apps_descarta_invalidos(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={"RAMS_APPS": "editor=nvim,MALO=x,rm=rm -rf /,musica=mpv,editor=code"})
    assert dict(s.apps_permitidas) == {"musica": "mpv", "editor": "code"}
    todo_malo = cargar_settings(entorno={"RAMS_APPS": "X=;y"})
    assert todo_malo.apps_permitidas_items == config.APPS_DEFECTO


def test_rutas_expansion_y_relativas(tmp_path: Path) -> None:
    hogar = tmp_path / "hogar"
    s = cargar_settings(entorno={
        "XDG_RUNTIME_DIR": "/run/user/1000", "RAMS_IDLE_FLAG": "${XDG_RUNTIME_DIR}/otra_bandera",
        "HOME": str(hogar), "RAMS_CTL_TOKEN_FILE": "~/tokens/ctl", "RAMS_DATA_DIR": "datos",
        "NEXUS_WORKSPACE": "",
    }, base_dir=tmp_path)
    assert s.idle_flag_path == Path("/run/user/1000/otra_bandera")
    assert s.ctl_token_file == hogar / "tokens" / "ctl"
    assert s.data_dir == tmp_path / "datos"            # relativa a base_dir, nunca a os.getcwd()
    assert s.memoria_dir == tmp_path / "datos" / "john"
    assert s.nexus_workspace is None
    por_defecto_ctl = cargar_settings(entorno={"HOME": str(hogar)})
    assert por_defecto_ctl.ctl_token_file == hogar / ".config" / "rams_biocore" / "ctl_token"
    sin_xdg = cargar_settings(entorno={"RAMS_IDLE_FLAG": "$XDG_RUNTIME_DIR/rams_idle"})
    assert sin_xdg.idle_flag_path is None


def test_connectome_proceso_se_ignora(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="rams.config"):
        s = cargar_settings(entorno={"RAMS_CONNECTOME_PROCESO": "1"})
    assert not hasattr(s, "conectoma_proceso")
    assert any("RAMS_CONNECTOME_PROCESO se ignora en v0.1" in r.getMessage()
               for r in caplog.records if r.levelno == logging.INFO)


def test_asegurar_directorios(tmp_path: Path) -> None:
    s = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path / "d")})
    s.asegurar_directorios()
    s.asegurar_directorios()  # idempotente
    for ruta in (s.data_dir, s.memoria_dir, s.log_dir, s.data_dir / "models"):
        assert ruta.is_dir()
        if os.name == "posix":
            assert (ruta.stat().st_mode & 0o777) == 0o700


# ---------------------------------------------------------------------------
# Redacción de secretos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("texto", "prohibido", "esperado"), [
    ("clave mi-secreto-largo en texto", "mi-secreto-largo", "«secreto»"),                       # 1
    (URL_TG, "123456789:AAAA", "bot«token»"),                                                     # 2
    ("key AIza" + "B" * 35 + " fin", "AIzaBBB", "«clave-google»"),                                # 3
    ("xi sk_" + "a1" * 20 + " fin", "sk_a1a1", "«clave-elevenlabs»"),                             # 4
    ("X-Goog-Api-Key: abcdef123", "abcdef123", "X-Goog-Api-Key: «secreto»"),                      # 5
    ("password=hunter2", "hunter2", "password=«secreto»"),                                        # 5
    ("Authorization: Bearer abc.def", "abc.def", "Authorization: «secreto»"),                     # 5
    ("--api-key sk_live_123 x", "sk_live_123", "--api-key «secreto» x"),                          # 6
    ("cmd --token=zzz999 --otro", "zzz999", "--token=«secreto»"),                                 # 6
    ("Bearer eyJabc.def", "eyJabc", "Bearer «secreto»"),                                          # 6
    ("https://h/?key=XYZ123&a=1", "XYZ123", "?key=«secreto»&a=1"),                                # 7
    ("http://127.0.0.1:8765/?t=tok3n", "tok3n", "?t=«secreto»"),                                  # 7
])
def test_redactar_patrones(texto: str, prohibido: str, esperado: str) -> None:
    redactado = redactar(texto, ["mi-secreto-largo"])
    assert prohibido not in redactado
    assert esperado in redactado
    assert redactar(redactado, ["mi-secreto-largo"]) == redactado   # idempotente


def test_redactar_alta_entropia_y_sin_falsos_positivos() -> None:
    assert redactar("x" * 30, alta_entropia=True) == "«oculto»"
    assert redactar("x" * 30) == "x" * 30                          # la regla 8 solo con alta_entropia
    assert "sk_live_123" not in redactar("--api-key sk_live_123 x")
    for frase in ("token inválido", "el password es largo", "La clave de Telegram falta",
                  "secreto revelado", "Bearer", "botón 123", "?t es una variable"):
        assert redactar(frase) == frase
    assert redactar("abc", ["abc"]) == "abc"                       # secretos < 6 caracteres no se tocan
    assert redactar("") == ""


def test_filtro_secretos_log_exception_a_archivo(tmp_path: Path) -> None:
    ruta = tmp_path / "prueba.log"
    logger = logging.getLogger("rams.prueba.filtro_archivo")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(ruta, encoding="utf-8")
    handler.addFilter(FiltroSecretos(["otro-secreto-123"]))
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError(URL_TG)
        except RuntimeError as exc:
            logger.exception("Fallo al enviar a Telegram: %s", exc)
        logger.warning("Clave %s cargada", "otro-secreto-123", stack_info=True)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.propagate = True
    contenido = ruta.read_text(encoding="utf-8")
    assert "123456789:AAAA" not in contenido
    assert "otro-secreto-123" not in contenido
    assert "bot«token»" in contenido
    assert "Traceback" in contenido and "RuntimeError" in contenido   # la traza sigue ahí, redactada


def test_filtro_secretos_redacta_exc_text_preexistente() -> None:
    registro = logging.LogRecord("rams.x", logging.ERROR, __file__, 1, "msg %s", ("a",), None)
    registro.exc_text = f"Traceback...\nRuntimeError: {URL_TG}"
    filtro = FiltroSecretos()
    assert filtro.filter(registro) is True
    assert "123456789:AAAA" not in registro.exc_text
    assert registro.args is None and registro.getMessage() == "msg a"
    assert filtro.filter(registro) is True                     # idempotente
    assert registro.getMessage() == "msg a"


def test_configurar_logging_idempotente_y_redactado(settings_con_claves: Settings,
                                                    capsys: pytest.CaptureFixture[str],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    raiz = logging.getLogger()
    nivel_previo = raiz.level
    # configurar_logging toca estado global de logging: se restaura todo al terminar para no
    # crear dependencias de orden con los tests que corran después.
    # 1) Nombres de nivel en español (addLevelName escribe en estos dos dicts del módulo).
    monkeypatch.setattr(logging, "_levelToName", dict(logging._levelToName))
    monkeypatch.setattr(logging, "_nameToLevel", dict(logging._nameToLevel))
    # 2) Niveles de los loggers que ajusta (rams y los silenciados).
    niveles_previos = {nombre: logging.getLogger(nombre).level
                       for nombre in ("rams", "httpx", "httpcore", "websockets", "asyncio")}
    try:
        configurar_logging(settings_con_claves, consola_nivel="WARNING")
        configurar_logging(settings_con_claves, consola_nivel="WARNING")
        propios = [h for h in raiz.handlers if getattr(h, "_rams", False)]
        assert sorted(getattr(h, "_rams_tipo", "") for h in propios) == ["archivo", "consola"]
        assert raiz.level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("rams").level == logging.INFO
        log = logging.getLogger("rams.prueba.configurar")
        log.info("Arranque normal")
        log.warning("Clave %s y URL %s", "test-gemini-key-000000", URL_TG)
        for h in propios:
            h.flush()
        contenido = (settings_con_claves.log_dir / "rams.log").read_text(encoding="utf-8")
        assert "Arranque normal" in contenido
        assert "AVISO" in contenido
        assert "test-gemini-key-000000" not in contenido and "123456789:AAAA" not in contenido
        if os.name == "posix":
            modo = (settings_con_claves.log_dir / "rams.log").stat().st_mode & 0o777
            assert modo == 0o600
        consola = capsys.readouterr().err
        assert "test-gemini-key-000000" not in consola
    finally:
        _quitar_handlers(raiz, ("archivo", "consola"))
        raiz.setLevel(nivel_previo)
        for nombre, nivel in niveles_previos.items():
            logging.getLogger(nombre).setLevel(nivel)


# ---------------------------------------------------------------------------
# Hora local
# ---------------------------------------------------------------------------


def test_hora_local_aware() -> None:
    s = cargar_settings(entorno={})
    ahora = ahora_local(s)
    assert ahora.tzinfo is not None
    assert ahora.utcoffset() == timedelta(hours=-5)        # Bogotá (o su respaldo COT) no tiene DST
    assert zona_local(s) is zona_local(s)


def test_zona_invalida_cae_a_cot(caplog: pytest.LogCaptureFixture) -> None:
    s = cargar_settings(entorno={"RAMS_TZ": "Marte/Base_Alfa"})
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        zona = zona_local(s)
        zona_local(s)
    assert zona.utcoffset(None) == timedelta(hours=-5)
    assert len([r for r in caplog.records if "Marte/Base_Alfa" in r.getMessage()]) <= 1


def test_en_horario_silencioso_cruza_medianoche() -> None:
    s = cargar_settings(entorno={})
    zona = zona_local(s)
    dia = datetime(2026, 10, 16, tzinfo=zona)
    assert en_horario_silencioso(s, dia.replace(hour=23, minute=30))
    assert en_horario_silencioso(s, dia.replace(hour=6, minute=59))
    assert en_horario_silencioso(s, dia.replace(hour=0, minute=0))
    assert not en_horario_silencioso(s, dia.replace(hour=7, minute=0))
    assert not en_horario_silencioso(s, dia.replace(hour=12, minute=0))
    assert not en_horario_silencioso(s, dia.replace(hour=22, minute=59))
    # Un momento aware en UTC se convierte a la hora local (04:30 UTC = 23:30 en Bogotá).
    assert en_horario_silencioso(s, datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc))
    # Naive: se interpreta como hora local.
    assert en_horario_silencioso(s, datetime(2026, 10, 16, 23, 45))
    tarde = s.con(silencio_inicio="13:00", silencio_fin="15:00")
    assert en_horario_silencioso(tarde, dia.replace(hour=14))
    assert not en_horario_silencioso(tarde, dia.replace(hour=15))
    assert isinstance(en_horario_silencioso(s), bool)


# ---------------------------------------------------------------------------
# Bus de eventos
# ---------------------------------------------------------------------------


def test_bus_patron_exacto_prefijo_y_comodin(bus: BusEventos) -> None:
    exactos: list[str] = []
    prefijo: list[str] = []
    todos: list[str] = []
    bus.suscribir("pc.telemetria", lambda e: exactos.append(e.nombre))
    bus.suscribir("pc.*", lambda e: prefijo.append(e.nombre))
    bus.suscribir("*", lambda e: todos.append(e.nombre))
    bus.publicar("pc.telemetria", {"cpu_pct": 1.0}, origen="aura")
    bus.publicar("pc.alerta", {"tipo": "cpu"})
    bus.publicar("pcx.otro")
    bus.publicar("homeostasis.estado", {"estado": {}})
    assert exactos == ["pc.telemetria"]
    assert prefijo == ["pc.telemetria", "pc.alerta"]
    assert todos == ["pc.telemetria", "pc.alerta", "pcx.otro", "homeostasis.estado"]
    stats = bus.estadisticas()
    assert stats["publicados"] == 4 and stats["suscriptores"] == 3
    assert stats["entregados"] == 7


def test_bus_evento_y_ultimo(bus: BusEventos) -> None:
    assert bus.ultimo("sistema.arranque") is None
    bus.publicar("sistema.arranque", {"version": "0.1.0", "offline": True, "inicio_ts": 1.0},
                 origen="main")
    bus.publicar("sistema.arranque", {"version": "0.1.0", "offline": False, "inicio_ts": 2.0},
                 origen="main")
    ultimo = bus.ultimo("sistema.arranque")
    assert isinstance(ultimo, Evento)
    assert ultimo.datos["inicio_ts"] == 2.0 and ultimo.origen == "main"
    assert isinstance(ultimo.ts, float) and ultimo.ts <= time.time()
    with pytest.raises(AttributeError):
        ultimo.nombre = "otro"  # type: ignore[misc]


def test_bus_manejador_asincrono(bus: BusEventos, correr) -> None:
    recibidos: list[dict] = []

    async def manejador(evento: Evento) -> None:
        await asyncio.sleep(0)
        recibidos.append(evento.datos)

    async def principal() -> None:
        bus.vincular_loop()
        bus.suscribir("voz.inicio", manejador)
        bus.publicar("voz.inicio", {"motor": "texto", "tone": "calido", "utterance_id": "u1"})
        await asyncio.sleep(0.05)
        await bus.emitir("voz.fin", {"utterance_id": "u1", "duracion_s": 0.0, "motor": "texto"})
        await bus.emitir("voz.inicio", {"motor": "edge", "tone": "serio", "utterance_id": "u2"})
        assert [r["utterance_id"] for r in recibidos] == ["u1", "u2"]   # emitir espera al manejador
        await bus.cerrar()

    correr(principal())


def test_bus_cola_descarta_el_mas_antiguo(bus: BusEventos, correr) -> None:
    async def principal() -> list[int]:
        cola = bus.cola("voz.nivel_boca", maxsize=3)
        for i in range(5):
            bus.publicar("voz.nivel_boca", {"valor": i / 10, "i": i})
        valores = []
        while not cola.empty():
            valores.append((await cola.get()).datos["i"])
        bus.soltar_cola(cola)
        bus.publicar("voz.nivel_boca", {"valor": 0.9, "i": 9})
        assert cola.empty()
        return valores

    assert correr(principal()) == [2, 3, 4]
    assert bus.estadisticas()["descartados"] == 2


def test_bus_publicar_desde_otro_hilo(bus: BusEventos, correr) -> None:
    async def principal() -> tuple[int, int, int]:
        bus.vincular_loop()
        hilo_loop = threading.get_ident()
        llegada = asyncio.Event()
        hilos: list[int] = []
        cola = bus.cola("pc.*")

        def manejador(evento: Evento) -> None:
            hilos.append(threading.get_ident())
            llegada.set()

        bus.suscribir("pc.ventana", manejador)
        hilo = threading.Thread(target=bus.publicar,
                                args=("pc.ventana", {"clase": "kitty", "titulo": "nvim",
                                                     "direccion": None, "pid": 1}),
                                kwargs={"origen": "aura"})
        hilo.start()
        hilo.join(timeout=5)
        await asyncio.wait_for(llegada.wait(), timeout=5)
        evento = await asyncio.wait_for(cola.get(), timeout=5)
        assert evento.datos["clase"] == "kitty"
        return hilo_loop, hilos[0], hilo.ident or 0

    hilo_loop, hilo_manejador, hilo_publicador = correr(principal())
    assert hilo_manejador == hilo_loop != hilo_publicador


def test_bus_loop_vinculado_sin_arrancar_que_corre_en_otro_hilo(bus: BusEventos) -> None:
    """vincular_loop(loop) antes de arrancarlo no ata el loop al hilo que lo vincula (§4.2.3)."""
    loop = asyncio.new_event_loop()
    bus.vincular_loop(loop)                      # se vincula desde este hilo, sin arrancar
    hilos: list[int] = []
    llegada = threading.Event()
    arrancado = threading.Event()

    def manejador(evento: Evento) -> None:
        hilos.append(threading.get_ident())
        llegada.set()

    bus.suscribir("pc.ventana", manejador)
    hilo_loop = threading.Thread(target=loop.run_forever, name="rams-test-loop", daemon=True)
    try:
        hilo_loop.start()
        loop.call_soon_threadsafe(arrancado.set)
        assert arrancado.wait(timeout=5)
        bus.publicar("pc.ventana", {"clase": "kitty", "titulo": "nvim", "direccion": None, "pid": 1})
        assert llegada.wait(timeout=5)
        assert hilos == [hilo_loop.ident]          # se despachó en el hilo del loop, no en este
    finally:
        loop.call_soon_threadsafe(loop.stop)
        hilo_loop.join(timeout=5)
        loop.close()


def test_bus_excepcion_del_manejador_aislada(bus: BusEventos, correr,
                                             caplog: pytest.LogCaptureFixture) -> None:
    recibidos: list[str] = []

    def roto(evento: Evento) -> None:
        raise ValueError("fallo provocado")

    async def roto_async(evento: Evento) -> None:
        raise RuntimeError("fallo asíncrono provocado")

    bus.suscribir("usuario.entrada", roto)
    bus.suscribir("usuario.entrada", roto_async)
    bus.suscribir("usuario.entrada", lambda e: recibidos.append(e.datos["canal"]))

    async def principal() -> None:
        with caplog.at_level(logging.ERROR, logger="rams.bus"):
            bus.publicar("usuario.entrada", {"texto": "hola", "canal": "terminal"})
            await asyncio.sleep(0.05)

    correr(principal())
    assert recibidos == ["terminal"]
    assert bus.estadisticas()["errores"] == 2
    assert any("fallo provocado" in r.getMessage() for r in caplog.records)
    assert any("fallo asíncrono provocado" in r.getMessage() for r in caplog.records)


def test_bus_nombre_invalido(bus: BusEventos, caplog: pytest.LogCaptureFixture) -> None:
    recibidos: list[Evento] = []
    bus.suscribir("*", recibidos.append)
    with caplog.at_level(logging.ERROR, logger="rams.bus"):
        for nombre in ("Sistema.Arranque", "pc..alerta", "pc.alerta.", "voz-inicio", "", "pc.*",
                       "log\n", "pc.alerta\n"):   # el "$" de match casaría antes del "\n" final
            bus.publicar(nombre, {"x": 1})
        bus.publicar("pc.alerta", ["no", "es", "dict"])  # type: ignore[arg-type]
    assert recibidos == []
    assert bus.estadisticas()["publicados"] == 0 and bus.ultimo("log\n") is None
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 9
    for patron in ("PC.*", "pc.*\n", "log\n"):            # patrones inválidos: no se suscriben
        desuscribir = bus.suscribir(patron, recibidos.append)
        desuscribir()
    assert bus.estadisticas()["suscriptores"] == 1


def test_bus_publicar_dentro_de_manejador_y_desuscribir(bus: BusEventos) -> None:
    orden: list[str] = []

    def reenviar(evento: Evento) -> None:
        orden.append(evento.nombre)
        bus.publicar("homeostasis.cambio_linea",
                     {"antes": "alfa", "despues": "steins_gate", "divergencia": 1.048596})

    bus.suscribir("homeostasis.iniciativa", reenviar)
    quitar = bus.suscribir("homeostasis.cambio_linea", lambda e: orden.append(e.nombre))
    bus.publicar("homeostasis.iniciativa", {"tipo": "cambio_linea"})
    assert orden == ["homeostasis.iniciativa", "homeostasis.cambio_linea"]
    quitar()
    quitar()  # idempotente
    bus.publicar("homeostasis.cambio_linea", {})
    assert orden == ["homeostasis.iniciativa", "homeostasis.cambio_linea"]
    assert bus.ultimo("homeostasis.cambio_linea").datos == {}


def test_bus_sin_loop_descarta_asincronos(bus: BusEventos) -> None:
    sincronos: list[str] = []

    async def asincrono(evento: Evento) -> None:  # pragma: no cover - nunca se ejecuta
        sincronos.append("async")

    bus.suscribir("sistema.modulo", asincrono)
    bus.suscribir("sistema.modulo", lambda e: sincronos.append("sync"))
    bus.publicar("sistema.modulo", {"clave": "voz"})
    assert sincronos == ["sync"]


def test_bus_cerrar_cancela_pendientes(bus: BusEventos, correr) -> None:
    async def lento(evento: Evento) -> None:
        await asyncio.sleep(30)

    async def principal() -> float:
        bus.suscribir("sistema.apagado", lento)
        bus.publicar("sistema.apagado", {"motivo": "usuario"})
        inicio = time.monotonic()
        await bus.cerrar(timeout=0.1)
        duracion = time.monotonic() - inicio
        bus.publicar("sistema.apagado", {"motivo": "usuario"})   # cerrado: se descarta sin lanzar
        return duracion

    assert correr(principal()) < 2.0
    assert bus.estadisticas()["suscriptores"] == 0


def test_instalar_manejador_log_redacta_y_es_idempotente(bus: BusEventos) -> None:
    eventos: list[Evento] = []
    bus.suscribir("log", eventos.append)
    raiz = logging.getLogger()
    manejador = instalar_manejador_log(bus, secretos=["otro-secreto-123"])
    manejador2 = instalar_manejador_log(bus, secretos=["otro-secreto-123"])
    try:
        propios = [h for h in raiz.handlers if getattr(h, "_rams_tipo", "") == "bus"
                   and getattr(h, "_rams_bus", None) is bus]
        assert propios == [manejador2] and manejador not in raiz.handlers
        assert getattr(manejador2, "_rams", False) is True
        log = logging.getLogger("rams.prueba.bus_log")
        try:
            raise RuntimeError(URL_TG)
        except RuntimeError as exc:
            log.exception("Fallo al enviar: %s (clave otro-secreto-123)", exc)
        log.info("informativo: no debe publicarse")
        logging.getLogger("rams.bus").error("registro del propio bus: se ignora")
    finally:
        raiz.removeHandler(manejador2)
        manejador2.close()
    assert len(eventos) == 1
    datos = eventos[0].datos
    assert set(datos) == {"nivel", "modulo", "mensaje", "ts"}
    assert datos["nivel"] == "ERROR" and datos["modulo"] == "rams.prueba.bus_log"
    assert "123456789:AAAA" not in datos["mensaje"]
    assert "otro-secreto-123" not in datos["mensaje"]
    assert "Traceback" not in datos["mensaje"]
    json.dumps(datos)


# ---------------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------------


def test_homeostatic_state_acotado() -> None:
    e = HomeostaticState(arousal=1.7, focus=-0.2, boredom=2, fatiga=-5, curiosidad=9,
                         valencia=-3, divergencia=12.5, presion_sueno=-1, rumbo_deg=725.0,
                         campo="omega", modo="hibernando", iniciativa="bailar",
                         linea_antes="omega", linea_despues="beta")
    assert (e.arousal, e.focus, e.boredom, e.fatiga, e.curiosidad) == (1.0, 0.0, 1.0, 0.0, 1.0)
    assert e.valencia == -1.0 and e.presion_sueno == 0.0
    assert e.divergencia == 3.999999
    assert e.rumbo_deg == pytest.approx(5.0)
    assert e.campo == "anomalo" and e.modo == "vigilia"
    assert e.iniciativa is None
    assert e.linea_antes is None and e.linea_despues == "beta"


def test_homeostatic_state_nan_e_inf() -> None:
    e = HomeostaticState(arousal=float("nan"), focus=float("inf"), valencia=float("-inf"),
                         divergencia=float("nan"), rumbo_deg=float("nan"),
                         factor_tiempo_real=float("nan"))
    assert e.arousal == 0.55 and e.focus == 0.70 and e.valencia == 0.0
    assert e.divergencia == DIVERGENCIA_STEINS_GATE
    assert e.rumbo_deg == 0.0 and e.factor_tiempo_real == 1.0


def test_homeostatic_state_bump_acotado() -> None:
    e = HomeostaticState(bump=[float("nan"), 1.5, -0.3, 0.5] + [0.1] * 20)
    assert len(e.bump) == 16
    assert e.bump[:4] == (0.0, 1.0, 0.0, 0.5)
    assert isinstance(e.bump, tuple)
    assert HomeostaticState().bump == ()


def test_homeostatic_state_ida_y_vuelta() -> None:
    e = HomeostaticState(arousal=0.62, focus=0.81, divergencia=1.123581, campo="beta",
                         iniciativa="cambio_linea", motivo_iniciativa="salto de línea",
                         linea_antes="alfa", linea_despues="beta", fuente="reducido",
                         tasas={"EPG": 12.5, "KC": 3.0}, bump=[0.1] * 16, ts=1234.5)
    d = e.a_dict()
    json.dumps(d)
    assert isinstance(d["bump"], list) and isinstance(d["tasas"], dict)
    assert d["divergencia"] == 1.123581
    assert HomeostaticState.desde_dict(d) == e
    parcial = HomeostaticState.desde_dict({"arousal": 0.9, "bump": [0.2, 0.4], "clave_rara": 1})
    assert parcial.arousal == 0.9 and parcial.bump == (0.2, 0.4)
    assert parcial.focus == 0.70 and parcial.campo == "steins_gate"
    assert HomeostaticState.desde_dict({"arousal": "mucho", "tasas": [1, 2]}).arousal == 0.55
    assert HomeostaticState.desde_dict(None).campo == "steins_gate"  # type: ignore[arg-type]
    neutral = HomeostaticState.neutral()
    assert neutral == HomeostaticState(ts=neutral.ts)


def test_homeostatic_state_textos() -> None:
    e = HomeostaticState(arousal=0.62, focus=0.81, boredom=0.10, fatiga=0.35, curiosidad=0.20,
                         valencia=0.4)
    assert e.resumen_textual() == ("arousal=medio, focus=alto, boredom=bajo, fatiga=medio, "
                                   "curiosidad=bajo, valencia=positiva")
    assert HomeostaticState(valencia=-0.5).resumen_textual().endswith("valencia=negativa")
    assert HomeostaticState(valencia=0.2).resumen_textual().endswith("valencia=neutra")
    assert HomeostaticState().divergencia_str() == "1.048596"
    assert HomeostaticState(divergencia=0.571024).divergencia_str() == "0.571024"
    assert e.vector() == (0.62, 0.81, 0.10, 0.35)


def test_formatear_decimal_y_banda() -> None:
    assert formatear_decimal(9.43) == "9,4"
    assert formatear_decimal(9.43, 2) == "9,43"
    assert formatear_decimal(59.4, 0) == "59"
    assert formatear_decimal(-0.01) == "0,0"
    assert formatear_decimal(float("nan")) == "—"
    assert banda(0.2) == "bajo" and banda(0.34) == "medio" and banda(0.66) == "medio"
    assert banda(0.67) == "alto"


def test_resumen_nexus() -> None:
    assert resumen_nexus(None) is None
    assert resumen_nexus({"corriendo": False, "workspace": "/home/u/x"}) is None
    assert resumen_nexus({}) is None
    completo = resumen_nexus({"corriendo": True, "enfocado": True,
                              "workspace": "/home/u/proyectos/rams_biocore/",
                              "trabajos": [{"cmd": "python3 train.py"}], "pids": [1, 2]})
    assert completo == {"corriendo": True, "enfocado": True, "workspace": "rams_biocore",
                        "trabajos": 1}
    windows = resumen_nexus({"corriendo": True, "workspace": "C:\\Users\\u\\Proyecto X"})
    assert windows == {"corriendo": True, "enfocado": False, "workspace": "Proyecto X",
                       "trabajos": 0}
    assert resumen_nexus({"corriendo": True}) == {"corriendo": True, "enfocado": False,
                                                  "workspace": None, "trabajos": 0}
    assert resumen_nexus({"corriendo": True, "trabajos": object()})["trabajos"] == 0
    assert resumen_nexus("no es un dict") is None  # type: ignore[arg-type]


def test_resumen_nexus_payload_roto_se_registra(caplog: pytest.LogCaptureFixture) -> None:
    """§3.1: la captura que garantiza 'nunca lanza' deja rastro (solo el tipo, sin el payload)."""

    class _MappingRoto(dict):
        def get(self, clave, defecto=None):  # noqa: ANN001 - firma de dict.get
            raise RuntimeError("/home/u/secreto/ruta")

    with caplog.at_level(logging.DEBUG, logger="rams.contracts"):
        assert resumen_nexus(_MappingRoto(corriendo=True)) is None
    registros = [r for r in caplog.records if r.name == "rams.contracts"]
    assert registros and "RuntimeError" in registros[-1].getMessage()
    assert "/home/u/secreto" not in registros[-1].getMessage()


def test_constantes_contrato() -> None:
    assert "terminal_largo" in MODOS_SALIDA
    assert CANALES_ENTRADA == ("terminal", "telegram", "nexus", "escritorio")
    assert "comando" not in TIPOS_ACCION and len(TIPOS_ACCION) == 13


def test_broca_reply_y_utterance() -> None:
    reply = BrocaReply.de_texto("  Hola, Christina no.  ", Etiquetas("molesta", "brazos_cruzados", "sarcastico"))
    assert reply.texto_plano == "Hola, Christina no."
    assert reply.etiquetas.exp == "molesta" and reply.fuente == "local"
    d = reply.a_dict()
    assert set(d) == {"segmentos", "fuente", "modelo", "latencia_ms", "corregido", "tsundere",
                      "finish", "texto_plano"}
    assert d["segmentos"][0] == {"exp": "molesta", "motion": "brazos_cruzados",
                                 "tone": "sarcastico", "texto": "Hola, Christina no.", "lang": "es"}
    json.dumps(d)
    assert BrocaReply(segmentos=[]).etiquetas == Etiquetas()
    u = Utterance.desde_reply(reply, origen="iniciativa", prioridad=1, canal="voz",
                              tipo_iniciativa="descanso")
    assert u.segmentos == reply.segmentos and u.texto_plano == reply.texto_plano
    assert len(u.id) == 12 and u.canal == "voz" and u.tipo_iniciativa == "descanso"
    assert Utterance(segmentos=(), prioridad=7, canal="fax").prioridad == 2
    assert Etiquetas().como_prefijo() == "[EXP:neutral][MOTION:idle][TONE:analitico]"
    assert Segmento(Etiquetas(), "x", lang="fr").lang == "es"


def test_diagnostico_presencia_y_telemetria() -> None:
    diag = DiagnosticoModulo("voz", "Salida de voz", EstadoModulo.DEGRADADO, "edge-tts", 12)
    assert diag.a_dict() == {"clave": "voz", "nombre": "Salida de voz", "estado": "DEGRADADO",
                             "detalle": "edge-tts", "latencia_ms": 12}
    assert DiagnosticoModulo("x", "X", "SIN CONEXIÓN").estado is EstadoModulo.SIN_CONEXION  # type: ignore[arg-type]
    assert f"{EstadoModulo.EN_ESPERA}" == "EN ESPERA"
    p = EstadoPresencia.desconocido()
    assert (p.presente, p.confianza, p.motivo, p.ambiguo) == (True, 0.3, "sin_datos", True)
    json.dumps(p.a_dict())
    t = Telemetria(cpu_pct=23.2, ram_usada_gb=9.43, ram_total_gb=16.0, ram_pct=58.9,
                   disco_pct=78.0, temp_cpu=61.2)
    assert t.resumen_textual() == ("CPU 23 %, RAM 9,4 de 16,0 GB (59 %), disco 78 %, CPU 61 °C, "
                                   "sin lectura de GPU")
    con_gpu = Telemetria(temp_gpu=54.0, gpu_pct=35.0, gpu_nombre="RTX", ram_total_gb=0.0,
                         proceso_top=("firefox", 42, 12.4))
    texto = con_gpu.resumen_textual()
    assert "GPU RTX 35 %, 54 °C" in texto and "sin lectura de RAM" in texto
    assert "sin lectura de temperatura de CPU" in texto and "firefox (12 %)" in texto
    assert con_gpu.a_dict()["proceso_top"] == {"nombre": "firefox", "pid": 42, "cpu": 12.4}


def test_protocolos_runtime_checkable() -> None:
    class AvatarFalso:
        en_linea = True

        async def aplicar_etiquetas(self, etiquetas: Etiquetas) -> None:
            return None

        async def boca(self, valor: float) -> None:
            return None

        async def cerrar_boca(self) -> None:
            return None

    class HyprFalso:
        disponible = False

        async def consultar(self, que: str):
            return None

        async def enfocar_direccion(self, direccion: str) -> bool:
            return False

    assert isinstance(AvatarFalso(), SalidaAvatar)
    assert isinstance(HyprFalso(), ProveedorHypr)
    assert not isinstance(HyprFalso(), RegistroLlamadas)
    assert not isinstance(object(), SalidaAvatar)
    assert math.isfinite(HomeostaticState().ts)


# ---------------------------------------------------------------------------
# v0.2 "Amadeus Companion" (docs/COMPANION.md §C2.1, §C2.2, §C3 "Grupo base")
# ---------------------------------------------------------------------------

RAIZ_PROYECTO = Path(__file__).resolve().parents[1]

# Defectos exactos de §C2.1 (salvo la ruta del token, que depende del sistema).
DEFECTOS_V02 = {
    "escritorio_habilitado": True,
    "escritorio_voz": False,
    "hud_chat": True,
    "travesuras_habilitadas": False,
    "travesura_amplitud_max_px": 40,
    "destinos_items": (),
    "vtuber_ai_tema": "amadeus",
    "vision_continua": False,
    "vision_fps": 3.0,
    "vision_detector": "auto",
    "vision_modelo": None,
    "vision_hfov": 70.0,
    "vision_espejo": False,
    "vision_apps_pausa": ("zoom", "teams", "ms-teams", "discord", "obs64", "obs"),
    "pantalla_modo": "off",
    "pantalla_fps": 1.0,
    "pantalla_excluir": (),
    "pantalla_excluir_avatar": True,
}


def test_v02_valores_por_defecto() -> None:
    s = cargar_settings(entorno={})
    for campo, esperado in DEFECTOS_V02.items():
        assert getattr(s, campo) == esperado, campo
    assert isinstance(s.travesura_amplitud_max_px, int) and isinstance(s.vision_fps, float)
    # Con un entorno explícito sin LOCALAPPDATA ni XDG_RUNTIME_DIR: data_dir/escritorio.json.
    assert s.escritorio_token_file == s.data_dir / "escritorio.json"
    assert s.tiene_escritorio is True and s.tiene_vision_continua is False
    directo = Settings()
    for campo, esperado in DEFECTOS_V02.items():
        assert getattr(directo, campo) == esperado, campo
    assert directo.escritorio_token_file.name == "escritorio.json"


def test_v02_fixture_settings_prueba_aislada(settings_prueba: Settings, tmp_path: Path) -> None:
    assert settings_prueba.escritorio_token_file == tmp_path / "escritorio.json"
    assert settings_prueba.travesuras_habilitadas is False
    assert settings_prueba.vision_continua is False and settings_prueba.tiene_vision_continua is False
    assert settings_prueba.pantalla_modo == "off"


def test_v02_ruta_token_escritorio_por_plataforma(tmp_path: Path) -> None:
    ruta = config._ruta_token_escritorio
    datos = tmp_path / "data"
    local = str(tmp_path / "AppData" / "Local")
    assert ruta({"LOCALAPPDATA": local}.get, datos, plataforma="win32") == \
        Path(local) / "rams" / "escritorio.json"
    assert ruta({}.get, datos, plataforma="win32") == datos / "escritorio.json"
    assert ruta({"XDG_RUNTIME_DIR": "/run/user/1000"}.get, datos, plataforma="linux") == \
        Path("/run/user/1000") / "rams" / "escritorio.json"
    assert ruta({"XDG_RUNTIME_DIR": "  "}.get, datos, plataforma="linux") == datos / "escritorio.json"
    assert ruta({"XDG_RUNTIME_DIR": "relativa/x"}.get, datos, plataforma="linux") == \
        datos / "escritorio.json"                                    # nunca relativa a os.getcwd()
    # En Linux no se mira LOCALAPPDATA ni en Windows XDG_RUNTIME_DIR.
    assert ruta({"LOCALAPPDATA": local}.get, datos, plataforma="linux") == datos / "escritorio.json"
    assert ruta({"XDG_RUNTIME_DIR": "/run/user/1000"}.get, datos, plataforma="win32") == \
        datos / "escritorio.json"
    # cargar_settings lee la variable del sistema actual desde el MISMO entorno explícito.
    var = "LOCALAPPDATA" if sys.platform == "win32" else "XDG_RUNTIME_DIR"
    base_sistema = local if sys.platform == "win32" else "/run/user/1000"
    s = cargar_settings(entorno={var: base_sistema, "RAMS_DATA_DIR": str(datos)})
    assert s.escritorio_token_file == Path(base_sistema) / "rams" / "escritorio.json"
    hogar = tmp_path / "hogar"
    explicita = cargar_settings(entorno={"HOME": str(hogar),
                                         "RAMS_ESCRITORIO_TOKEN_FILE": "~/rams/escritorio.json"})
    assert explicita.escritorio_token_file == hogar / "rams" / "escritorio.json"


def test_v02_rangos_y_opciones(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        malo = cargar_settings(entorno={
            "RAMS_PANTALLA_FPS": "0.1", "RAMS_VISION_FPS": "6", "RAMS_TRAVESURA_AMPLITUD": "81",
            "RAMS_VISION_HFOV": "29", "RAMS_PANTALLA_MODO": "todo", "RAMS_VISION_DETECTOR": "dlib",
            "VTUBER_AI_TEMA": "otro-tema-raro", "RAMS_TRAVESURAS": "a-veces", "RAMS_HUD_CHAT": "tal-vez",
        })
    assert malo.pantalla_fps == 1.0 and malo.vision_fps == 3.0
    assert malo.travesura_amplitud_max_px == 40 and malo.vision_hfov == 70.0
    assert malo.pantalla_modo == "off" and malo.vision_detector == "auto"
    assert malo.vtuber_ai_tema == "amadeus"
    assert malo.travesuras_habilitadas is False and malo.hud_chat is True
    mensajes = " ".join(r.getMessage() for r in _avisos_config(caplog))
    for var in ("RAMS_PANTALLA_FPS", "RAMS_VISION_FPS", "RAMS_TRAVESURA_AMPLITUD", "RAMS_VISION_HFOV",
                "RAMS_PANTALLA_MODO", "RAMS_VISION_DETECTOR", "VTUBER_AI_TEMA", "RAMS_TRAVESURAS"):
        assert var in mensajes
    assert "otro-tema-raro" not in mensajes and "a-veces" not in mensajes   # nunca el valor recibido
    assert cargar_settings(entorno={"RAMS_PANTALLA_FPS": "3"}).pantalla_fps == 1.0
    bueno = cargar_settings(entorno={
        "RAMS_PANTALLA_FPS": "0,2", "RAMS_VISION_FPS": "5", "RAMS_TRAVESURA_AMPLITUD": "80",
        "RAMS_VISION_HFOV": "120", "RAMS_PANTALLA_MODO": "Metadatos", "RAMS_VISION_DETECTOR": "YUNET",
        "VTUBER_AI_TEMA": "None", "RAMS_TRAVESURAS": "1", "RAMS_ESCRITORIO": "0",
        "RAMS_ESCRITORIO_VOZ": "sí", "RAMS_HUD_CHAT": "0", "RAMS_VISION_ESPEJO": "1",
        "RAMS_PANTALLA_EXCLUIR_AVATAR": "0", "RAMS_VISION_MODELO": "modelos/yunet.onnx",
    }, base_dir=RAIZ_PROYECTO)
    assert bueno.pantalla_fps == 0.2 and bueno.vision_fps == 5.0
    assert bueno.travesura_amplitud_max_px == 80 and bueno.vision_hfov == 120.0
    assert bueno.pantalla_modo == "metadatos" and bueno.vision_detector == "yunet"
    assert bueno.vtuber_ai_tema == "none" and bueno.travesuras_habilitadas is True
    assert bueno.escritorio_habilitado is False and bueno.tiene_escritorio is False
    assert bueno.escritorio_voz is True and bueno.hud_chat is False
    assert bueno.vision_espejo is True and bueno.pantalla_excluir_avatar is False
    assert bueno.vision_modelo == RAIZ_PROYECTO / "modelos" / "yunet.onnx"   # relativa a base_dir
    assert cargar_settings(entorno={"RAMS_TRAVESURA_AMPLITUD": "5"}).travesura_amplitud_max_px == 5
    assert cargar_settings(entorno={"RAMS_PANTALLA_MODO": "pixeles"}).pantalla_modo == "pixeles"


def test_v02_csv_procesos_y_patrones(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={
            "RAMS_VISION_APPS_PAUSA": "Zoom.exe, slack ,zoom,,mal nombre,OBS64",
            "RAMS_PANTALLA_EXCLUIR": "Mi Banco Ñ, Diario  Íntimo,,mi banco ñ," + "x" * 61,
        })
    assert s.vision_apps_pausa == ("zoom", "slack", "obs64")
    assert s.pantalla_excluir == ("mi banco n", "diario intimo")
    avisos = [r.getMessage() for r in _avisos_config(caplog)]
    assert any("RAMS_VISION_APPS_PAUSA" in m for m in avisos)
    assert any("RAMS_PANTALLA_EXCLUIR" in m for m in avisos)
    assert all("mal nombre" not in m for m in avisos)
    todo_malo = cargar_settings(entorno={"RAMS_VISION_APPS_PAUSA": "???, a b"})
    assert todo_malo.vision_apps_pausa == DEFECTOS_V02["vision_apps_pausa"]
    muchos = cargar_settings(entorno={"RAMS_PANTALLA_EXCLUIR": ",".join(f"p{i}" for i in range(40))})
    assert len(muchos.pantalla_excluir) == 32


def test_v02_rams_destinos_suma_sinonimos_y_descarta(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={
            "RAMS_DESTINOS": "wasap=whatsapp, Twitter=X,malo=noexiste,x=whatsapp,Música=spotify,"
                             "sin igual,con espacio=x,face=facebook,face=instagram,youtube=youtube"})
    assert s.destinos_items == (("wasap", "whatsapp"), ("twitter", "x"), ("musica", "spotify"),
                                ("face", "instagram"))
    avisos = [r.getMessage() for r in _avisos_config(caplog)]
    assert len(avisos) == 1 and "RAMS_DESTINOS" in avisos[0] and "4" in avisos[0]
    assert "noexiste" not in avisos[0]
    destinos = s.destinos_permitidos
    assert isinstance(destinos, types.MappingProxyType)
    for clave in DESTINOS_BASE:
        assert destinos[clave] == clave                      # el catálogo siempre está
    assert destinos["wasap"] == "whatsapp" and destinos["twitter"] == "x"
    assert destinos["musica"] == "spotify" and destinos["face"] == "instagram"
    assert destinos["x"] == "x"                              # un sinónimo nunca pisa una clave
    assert set(destinos) == set(DESTINOS_BASE) | {"wasap", "twitter", "musica", "face"}
    with pytest.raises(TypeError):
        destinos["rm"] = "archivos"  # type: ignore[index]
    sin_destinos = cargar_settings(entorno={})
    assert dict(sin_destinos.destinos_permitidos) == {clave: clave for clave in DESTINOS_BASE}
    # Robusto ante una copia con datos raros (con()), sin lanzar.
    raro = sin_destinos.con(destinos_items=[["chat", "telegram"], ("MAL", "x"), ("y",)])
    assert dict(raro.destinos_permitidos) == {**{c: c for c in DESTINOS_BASE}, "chat": "telegram"}


def test_v02_capacidades_no_dependen_de_offline() -> None:
    entorno = {"RAMS_CAMERA": "1", "RAMS_VISION_CONTINUA": "1"}
    for offline in (True, False):
        s = cargar_settings(entorno=entorno, offline=offline)
        assert s.offline is offline
        assert s.tiene_escritorio is True
        assert s.tiene_vision_continua is True
    s = cargar_settings(entorno=entorno, offline=True)
    assert s.con(camara_habilitada=False).tiene_vision_continua is False      # opt-in de dos niveles
    assert s.con(vision_continua=False).tiene_vision_continua is False
    assert cargar_settings(entorno={"RAMS_VISION_CONTINUA": "1"}).tiene_vision_continua is False
    assert s.con(escritorio_habilitado=False).tiene_escritorio is False


def test_v02_pantalla_excluir_re() -> None:
    s = cargar_settings(entorno={"RAMS_PANTALLA_EXCLUIR": "Diario Íntimo, a.b(c"})
    patron = s.pantalla_excluir_re
    assert patron is s.pantalla_excluir_re                    # compilado una vez (caché)
    for titulo in ("Bitwarden", "Nueva pestaña InPrivate", "Iniciar sesión - Cuenta",
                   "Bancolombia - Sucursal Virtual", "Mi diario íntimo - Notas", "Pagar con PSE",
                   "KeePassXC", "Navegación privada - Firefox", "Archivo a.b(c).txt"):
        assert patron.search(normalizar_titulo(titulo)), titulo
    assert patron.search("Bitwarden")                         # sin distinguir mayúsculas
    for titulo in ("YouTube - Google Chrome", "main.py - Visual Studio Code", "Archivo axb(c",
                   "Steins;Gate 0 - Episodio 1"):
        assert patron.search(normalizar_titulo(titulo)) is None, titulo
    base = cargar_settings(entorno={}).pantalla_excluir_re
    assert base.pattern == PATRON_TITULO_SENSIBLE             # sin extras: exactamente el patrón base
    assert base.search(normalizar_titulo("Mi diario íntimo")) is None


def test_parece_secreto() -> None:
    for texto in ("482915", "  1234 ", "4111 1111 1111 1111", "4111-1111-1111-1111",
                  "mi tarjeta es 4111111111111111 ok", "AIza" + "B" * 35, "password: x",
                  "Contraseña: hunter2", "mi pin: 1234", "el código=998877", "Kj8#mP2$qL9!",
                  "Password123!", "token=abc", "x" * 30):
        assert parece_secreto(texto) is True, texto
    assert parece_secreto("te paso mi-secreto-largo", ["mi-secreto-largo"]) is True
    for texto in ("hola, ¿vienes mañana?", "¡Felicidades, mamá! #SteinsGate0 @kurisu_makise",
                  "nos vemos en Bogotá-Medellín a las 3:00pm-5:00pm", "llámame al 300 123 4567",
                  "4111111111111112", "", "   ", "El Psy Kongroo.", "escríbeme a juan.perez@gmail.com",
                  "mira https://wa.me/573001234567", "La reunión es el 17/10/2026 a las 10:30"):
        assert parece_secreto(texto) is False, texto
    assert parece_secreto(None) is False  # type: ignore[arg-type]


def test_titulo_sensible_y_normalizar_titulo() -> None:
    assert titulo_sensible("Bancolombia - Sucursal Virtual") is True
    assert titulo_sensible("Iniciar sesión - Google Chrome") is True
    assert titulo_sensible("Verificación en dos pasos") is True
    assert titulo_sensible("KeePassXC") is True
    assert titulo_sensible("Nueva pestaña InPrivate - Microsoft Edge") is True
    assert titulo_sensible("Nequi") is True and titulo_sensible("Escribe tu PIN") is True
    assert titulo_sensible("YouTube - Google Chrome") is False
    assert titulo_sensible("Spinning - Música") is False      # \bpin\b no casa dentro de palabras
    assert titulo_sensible("") is False and titulo_sensible(None) is False
    assert normalizar_titulo("  Contraseña   ÑANDÚ ") == "contrasena nandu"
    assert normalizar_titulo(None) == ""


def test_v02_constantes_de_contrato() -> None:
    assert "curiosidad_web" in INICIATIVAS and INICIATIVAS[:4] == (
        "conversar", "curiosidad_visual", "descanso", "cambio_linea")
    assert CANALES_LOCALES == frozenset({"terminal", "escritorio"})
    assert "escritorio" in CANALES_LOCALES and set(CANALES_LOCALES) <= set(CANALES_ENTRADA)
    assert CANALES_SALIDA == ("voz", "terminal", "telegram")            # NO cambia
    assert ORIGENES_ACCION == ("usuario", "iniciativa", "nexus", "telegram", "llm")   # NO cambia
    assert TIPOS_ACCION[:8] == ("lanzar_app", "enfocar_nexus", "ir_workspace", "cerrar_ventana",
                                "volumen", "media", "captura_pantalla", "vistazo")
    assert TIPOS_ACCION_V02 == frozenset({"abrir_destino", "buscar_en", "prerrellenar",
                                          "escribir_texto", "travesura_cursor"})
    assert TIPOS_ACCION_V02 <= set(TIPOS_ACCION) and len(set(TIPOS_ACCION)) == 13
    assert PATRONES_TRAVESURA == ("empujon", "circulo", "temblor", "hacia_avatar")
    assert MODOS_PANTALLA == ("off", "metadatos", "pixeles")
    assert ESTADOS_VISION == ("off", "continua", "pausada", "ocupada", "error", "degradada")
    assert ACCIONES_ORDEN_ESCRITORIO == ("aparecer", "esconderse", "abrir_chat", "notificar")
    assert EVENTOS_ESCRITORIO == ("toque", "soltada", "cursor_encima", "mostrada", "ocultada",
                                  "chat_abierto")
    assert CONTROLES_ESCRITORIO == ("no_molestar_on", "no_molestar_off", "basta_travesuras",
                                    "pausar_vision")
    assert CONDUCTAS_SOLICITUD == ("buscar_usuario", "asomarse", "esconderse", "pasear")
    assert SAFE_ALIAS_RE.match("whatsapp_2") and not SAFE_ALIAS_RE.match("WhatsApp")
    assert not SAFE_ALIAS_RE.match("a" * 25) and not SAFE_ALIAS_RE.match("")
    assert list(DESTINOS_BASE) == ["whatsapp", "telegram", "x", "gmail", "correo", "instagram",
                                   "tiktok", "facebook", "discord", "linkedin", "youtube",
                                   "spotify", "navegador", "archivos"]
    prerrelleno = {k for k, v in DESTINOS_BASE.items() if v["prerrelleno"]}
    busqueda = {k for k, v in DESTINOS_BASE.items() if v["busqueda"]}
    assert prerrelleno == {"whatsapp", "telegram", "x", "gmail", "correo"}
    assert busqueda == {"x", "youtube", "spotify", "navegador"}
    assert isinstance(DESTINOS_BASE, types.MappingProxyType)
    assert all(isinstance(v, types.MappingProxyType) and set(v) == {"prerrelleno", "busqueda"}
               for v in DESTINOS_BASE.values())
    with pytest.raises(TypeError):
        DESTINOS_BASE["whatsapp"]["prerrelleno"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        DESTINOS_BASE["nuevo"] = {}  # type: ignore[index]
    assert "bitwarden" in PATRON_TITULO_SENSIBLE and r"\bpin\b" in PATRON_TITULO_SENSIBLE
    import core.contracts as contratos
    for nombre in ("TIPOS_ACCION_V02", "PATRONES_TRAVESURA", "MODOS_PANTALLA", "ESTADOS_VISION",
                   "ACCIONES_ORDEN_ESCRITORIO", "EVENTOS_ESCRITORIO", "CONTROLES_ESCRITORIO",
                   "CONDUCTAS_SOLICITUD", "SAFE_ALIAS_RE", "DESTINOS_BASE", "PATRON_TITULO_SENSIBLE"):
        assert nombre in contratos.__all__, nombre
    for nombre in ("parece_secreto", "titulo_sensible"):
        assert nombre in config.__all__
    # Los contratos que validan contra estas constantes aceptan los valores nuevos.
    assert HomeostaticState(iniciativa="curiosidad_web").iniciativa == "curiosidad_web"
    assert Utterance(segmentos=(), tipo_iniciativa="curiosidad_web").tipo_iniciativa == "curiosidad_web"
    ctx = ContextoBroca(estado=HomeostaticState(), canal_entrada="escritorio")
    assert ctx.canal_entrada == "escritorio"
    iniciativa = ContextoBroca(estado=HomeostaticState(), canal_entrada="iniciativa",
                               iniciativa="curiosidad_web")
    assert iniciativa.iniciativa == "curiosidad_web"


def test_contexto_broca_percepcion_filtrada() -> None:
    estado = HomeostaticState()
    assert ContextoBroca(estado=estado).percepcion is None
    ctx = ContextoBroca(estado=estado, percepcion={"app": "chrome", "x": 1})
    assert ctx.percepcion == {"app": "chrome"}
    assert isinstance(ctx.percepcion, types.MappingProxyType)
    with pytest.raises(TypeError):
        ctx.percepcion["app"] = "otra"  # type: ignore[index]
    assert isinstance(hash(ctx), int)                        # percepcion no entra en el hash
    completa = ContextoBroca(estado=estado, percepcion={
        "app": None, "titulo_corto": "T" * 100 + "\n", "video": 1, "pantalla_completa": float("nan"),
        "rostro": "sí", "atencion": 3, "dist_m": -2.0, "imagen": b"\x00\x01", "titulo": "completo"})
    assert dict(completa.percepcion) == {"app": "", "titulo_corto": "T" * 60, "video": True,
                                         "pantalla_completa": None, "rostro": None,
                                         "atencion": 1.0, "dist_m": 0.0}
    normal = ContextoBroca(estado=estado, percepcion={"app": "code", "titulo_corto": "main.py",
                                                      "video": False, "pantalla_completa": None,
                                                      "rostro": True, "atencion": 0.4, "dist_m": 0.7})
    assert dict(normal.percepcion) == {"app": "code", "titulo_corto": "main.py", "video": False,
                                       "pantalla_completa": None, "rostro": True, "atencion": 0.4,
                                       "dist_m": 0.7}
    assert ContextoBroca(estado=estado, percepcion=["app", "chrome"]).percepcion is None  # type: ignore[arg-type]
    assert ContextoBroca(estado=estado, percepcion={}).percepcion == {}
    json.dumps(dict(normal.percepcion))


def test_v02_settings_hashable_y_resumen() -> None:
    entorno = {"RAMS_DESTINOS": "wasap=whatsapp", "RAMS_PANTALLA_EXCLUIR": "diario",
               "RAMS_VISION_MODELO": "/opt/yunet.onnx", "RAMS_VISION_APPS_PAUSA": "zoom,slack"}
    s = cargar_settings(entorno=entorno)
    assert isinstance(hash(s), int)
    assert hash(s) == hash(cargar_settings(entorno=entorno))
    assert {s: "ok"}[cargar_settings(entorno=entorno)] == "ok"
    resumen = s.resumen_seguro()
    json.dumps(resumen, ensure_ascii=False)
    assert resumen["tiene_escritorio"] is True and resumen["tiene_vision_continua"] is False
    assert resumen["destinos_items"] == [["wasap", "whatsapp"]]
    assert str(resumen["escritorio_token_file"]).endswith("escritorio.json")


def test_env_example_v02_da_los_defectos_sin_avisos(caplog: pytest.LogCaptureFixture) -> None:
    ruta = RAIZ_PROYECTO / ".env.example"
    texto = ruta.read_bytes().decode("utf-8")
    assert "\r" not in texto
    for seccion in ("# --- Amadeus Desktop / escritorio ---", "# --- Control del PC y travesuras ---",
                    "# --- Visión continua (cámara + pantalla) ---"):
        assert seccion in texto
    for var in ("RAMS_ESCRITORIO", "RAMS_ESCRITORIO_TOKEN_FILE", "RAMS_ESCRITORIO_VOZ", "RAMS_HUD_CHAT",
                "RAMS_TRAVESURAS", "RAMS_TRAVESURA_AMPLITUD", "RAMS_DESTINOS", "VTUBER_AI_TEMA",
                "RAMS_VISION_CONTINUA", "RAMS_VISION_FPS", "RAMS_VISION_DETECTOR", "RAMS_VISION_MODELO",
                "RAMS_VISION_HFOV", "RAMS_VISION_ESPEJO", "RAMS_VISION_APPS_PAUSA", "RAMS_PANTALLA_MODO",
                "RAMS_PANTALLA_FPS", "RAMS_PANTALLA_EXCLUIR", "RAMS_PANTALLA_EXCLUIR_AVATAR"):
        assert f"{var}=" in texto, var
    leido = leer_env_archivo(ruta)
    with caplog.at_level(logging.DEBUG, logger="rams.config"):
        s = cargar_settings(entorno=leido)
    assert _avisos_config(caplog) == [], [r.getMessage() for r in _avisos_config(caplog)]
    for campo, esperado in DEFECTOS_V02.items():
        assert getattr(s, campo) == esperado, campo
    assert s.escritorio_token_file == cargar_settings(entorno={}).escritorio_token_file


def test_asegurar_directorios_crea_el_padre_del_token(tmp_path: Path,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    padre = tmp_path / "run" / "rams"
    s = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path / "d"),
                                 "RAMS_ESCRITORIO_TOKEN_FILE": str(padre / "escritorio.json")})
    s.asegurar_directorios()
    s.asegurar_directorios()  # idempotente
    assert padre.is_dir() and not (padre / "escritorio.json").exists()   # el archivo lo escribe el HUD
    ajeno = tmp_path / "compartido"
    ajeno.mkdir()
    if os.name == "posix":
        assert (padre.stat().st_mode & 0o777) == 0o700
        os.chmod(ajeno, 0o755)
    otro = s.con(escritorio_token_file=ajeno / "escritorio.json")
    otro.asegurar_directorios()
    if os.name == "posix":   # un directorio ajeno que ya existía no cambia de permisos
        assert (ajeno.stat().st_mode & 0o777) == 0o755
    (tmp_path / "archivo.txt").write_text("no soy un directorio", encoding="utf-8")
    roto = s.con(escritorio_token_file=tmp_path / "archivo.txt" / "escritorio.json")
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        roto.asegurar_directorios()   # OSError registrado, no propagado
    assert any("token de escritorio" in r.getMessage() for r in _avisos_config(caplog))


# ---------------------------------------------------------------------------
# Fixture sin_efectos_pc (tests/conftest.py)
# ---------------------------------------------------------------------------


def _marcada(objeto: object) -> bool:
    """El sustituto lleva la marca de la fixture (si no, NO se llama: sería el efecto real)."""
    return getattr(objeto, "_rams_sin_efectos", False) is True


def test_sin_efectos_pc_falla_ante_xdg_open_por_asyncio(correr) -> None:
    assert _marcada(asyncio.create_subprocess_exec)
    assert _marcada(asyncio.subprocess.create_subprocess_exec)
    with pytest.raises(pytest.fail.Exception, match="xdg-open"):
        asyncio.create_subprocess_exec("xdg-open", "https://example.com")

    async def principal() -> None:
        await asyncio.create_subprocess_exec("/usr/bin/hyprctl", "dispatch", "exec", "kitty")

    with pytest.raises(pytest.fail.Exception, match="hyprctl"):
        correr(principal())


def test_sin_efectos_pc_popen_vigilado() -> None:
    assert _marcada(subprocess.Popen)
    for argumentos in (["wtype", "hola"], ["C:\\Windows\\explorer.exe", "https://example.com"],
                       "cmd /c start https://example.com", ["powershell.exe", "-c", "x"],
                       [Path("/usr/bin/gtk-launch"), "kitty"], [b"ydotool", b"type", b"x"]):
        with pytest.raises(pytest.fail.Exception):
            subprocess.Popen(argumentos)
    with pytest.raises(pytest.fail.Exception):
        subprocess.run(["start", "https://example.com"], check=False)
    # Lo demás pasa (también un iterador, que no se consume al revisarlo).
    resultado = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True,
                               timeout=60, check=False)
    assert resultado.returncode == 0 and resultado.stdout.strip() == "ok"
    proceso = subprocess.Popen(iter([sys.executable, "-c", "pass"]))
    assert isinstance(proceso, subprocess.Popen)
    assert proceso.wait(timeout=60) == 0


def test_sin_efectos_pc_navegador_y_win32() -> None:
    import webbrowser

    for nombre in ("open", "open_new", "open_new_tab"):
        funcion = getattr(webbrowser, nombre)
        assert _marcada(funcion), nombre
        with pytest.raises(pytest.fail.Exception):
            funcion("https://example.com")
    if hasattr(os, "startfile"):
        assert _marcada(os.startfile)
        with pytest.raises(pytest.fail.Exception):
            os.startfile("https://example.com")
    if sys.platform == "win32":
        import ctypes

        user32 = ctypes.windll.user32
        for nombre in ("SendInput", "SetCursorPos", "keybd_event", "mouse_event", "SetForegroundWindow"):
            funcion = getattr(user32, nombre)
            assert _marcada(funcion), nombre
            with pytest.raises(pytest.fail.Exception):
                funcion(0, 0)
        assert _marcada(ctypes.windll.shell32.ShellExecuteW)


def test_sin_efectos_pc_un_monkeypatch_del_test_tiene_prioridad(monkeypatch: pytest.MonkeyPatch,
                                                               correr) -> None:
    lanzados: list[tuple[str, ...]] = []

    async def exec_falso(*argv: str, **kwargs: object) -> str:
        lanzados.append(argv)
        return "proceso-falso"

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_falso)
    assert correr(asyncio.create_subprocess_exec("xdg-open", "https://example.com")) == "proceso-falso"
    assert lanzados == [("xdg-open", "https://example.com")]
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: True)
    assert webbrowser.open("https://example.com") is True


# ---------------------------------------------------------------------------
# v0.2 Respaldo Puter (RAMS_PUTER, PUTER_MODEL, RAMS_PUTER_PRIMERO)
# ---------------------------------------------------------------------------


def test_puter_valores_por_defecto() -> None:
    for s in (cargar_settings(entorno={}), Settings()):
        assert s.puter_habilitado is True and s.puter_modelo == "" and s.puter_primero is False
        assert s.puter_max_llamadas_dia == 300 and s.tiene_puter is True
    resumen = cargar_settings(entorno={}).resumen_seguro()
    assert resumen["puter_habilitado"] is True and resumen["puter_modelo"] == ""
    assert resumen["puter_primero"] is False and resumen["tiene_puter"] is True
    assert resumen["puter_max_llamadas_dia"] == 300
    json.dumps(resumen, ensure_ascii=False)


def test_puter_lectura_y_validacion(caplog: pytest.LogCaptureFixture) -> None:
    s = cargar_settings(entorno={"RAMS_PUTER": "0", "PUTER_MODEL": "openrouter:proveedor/modelo-1.5_x",
                                 "RAMS_PUTER_PRIMERO": "sí"})
    assert s.puter_habilitado is False and s.tiene_puter is False
    assert s.puter_modelo == "openrouter:proveedor/modelo-1.5_x" and s.puter_primero is True
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        malo = cargar_settings(entorno={"RAMS_PUTER": "quizas", "PUTER_MODEL": "modelo con espacios",
                                        "RAMS_PUTER_PRIMERO": "talvez"})
    assert malo.puter_habilitado is True and malo.puter_modelo == "" and malo.puter_primero is False
    mensajes = " ".join(r.getMessage() for r in _avisos_config(caplog))
    for var in ("RAMS_PUTER", "PUTER_MODEL", "RAMS_PUTER_PRIMERO"):
        assert var in mensajes
    assert "modelo con espacios" not in mensajes and "quizas" not in mensajes   # nunca el valor recibido
    assert cargar_settings(entorno={"PUTER_MODEL": "x" * 101}).puter_modelo == ""
    assert cargar_settings(entorno={"PUTER_MODEL": "   "}).puter_modelo == ""      # regla de ausencia


def test_puter_capacidad_depende_de_offline_y_escritorio() -> None:
    s = cargar_settings(entorno={})
    assert s.tiene_puter is True
    assert s.con(offline=True).tiene_puter is False                # Puter necesita red
    assert cargar_settings(entorno={}, offline=True).tiene_puter is False
    assert cargar_settings(entorno={"RAMS_OFFLINE": "1"}).tiene_puter is False
    assert s.con(escritorio_habilitado=False).tiene_puter is False  # corre en Amadeus Desktop
    assert s.con(puter_habilitado=False).tiene_puter is False
    assert isinstance(hash(s.con(puter_modelo="m", puter_primero=True)), int)


def test_env_example_documenta_puter_sin_avisos(caplog: pytest.LogCaptureFixture) -> None:
    ruta = RAIZ_PROYECTO / ".env.example"
    texto = ruta.read_bytes().decode("utf-8")
    assert "\r" not in texto
    assert "# --- Respaldo Puter (IA de respaldo del Área de Broca) ---" in texto
    for var in ("RAMS_PUTER", "PUTER_MODEL", "RAMS_PUTER_PRIMERO", "PUTER_MAX_CALLS_DAY"):
        assert f"\n{var}=" in texto, var
    with caplog.at_level(logging.DEBUG, logger="rams.config"):
        s = cargar_settings(entorno=leer_env_archivo(ruta))
    assert _avisos_config(caplog) == []
    assert s.puter_habilitado is True and s.puter_modelo == "" and s.puter_primero is False
    assert s.puter_max_llamadas_dia == 300


def test_puter_model_mismo_patron_que_el_enlace(caplog: pytest.LogCaptureFixture) -> None:
    # Empieza por letra o dígito y tiene como mucho 100 caracteres: todo lo que acepta la
    # configuración lo acepta también interface/escritorio_link.py (y no lo descarta en silencio).
    from interface.escritorio_link import _MODELO_PUTER_RE

    buenos = ("gpt-5-nano", "claude-sonnet-4-5", "openrouter:meta-llama/llama-3.1-8b-instruct",
              "x-ai/grok-4", "9modelo", "a" * 100)
    for valor in buenos:
        assert cargar_settings(entorno={"PUTER_MODEL": valor}).puter_modelo == valor, valor
        assert _MODELO_PUTER_RE.fullmatch(valor), valor
    for valor in ("/modelo", "-x", ".x", ":x", "_x", "a" * 101, "a" * 110, "modelo?x", "ñandú"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rams.config"):
            assert cargar_settings(entorno={"PUTER_MODEL": valor}).puter_modelo == "", valor
        mensajes = " ".join(r.getMessage() for r in _avisos_config(caplog))
        assert "PUTER_MODEL" in mensajes and valor not in mensajes


def test_puter_max_calls_day(caplog: pytest.LogCaptureFixture) -> None:
    s = cargar_settings(entorno={"PUTER_MAX_CALLS_DAY": "50"})
    assert s.puter_max_llamadas_dia == 50 and s.tiene_puter is True
    cero = cargar_settings(entorno={"PUTER_MAX_CALLS_DAY": "0"})
    assert cero.puter_max_llamadas_dia == 0 and cero.tiene_puter is False     # 0 = nunca usar Puter
    assert cargar_settings(entorno={}).con(puter_max_llamadas_dia=0).tiene_puter is False
    for malo in ("-1", "muchos", "3.5"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rams.config"):
            assert cargar_settings(entorno={"PUTER_MAX_CALLS_DAY": malo}).puter_max_llamadas_dia == 300
        mensajes = " ".join(r.getMessage() for r in _avisos_config(caplog))
        assert "PUTER_MAX_CALLS_DAY" in mensajes and malo not in mensajes


# ---------------------------------------------------------------------------
# v0.2.2 "Correo, Drive y taller de scripts" (docs/V022_CORREO_DRIVE_SCRIPTS.md §2, §3 y §3.4)
# ---------------------------------------------------------------------------

# Defectos exactos de §2.1 (las rutas cuelgan de data_dir y se comprueban aparte).
DEFECTOS_V022 = {
    "google_habilitado": False,
    "google_servicios": ("correo", "drive"),
    "drive_completo": False,
    "drive_descargas_dir": None,
    "drive_max_descarga_mb": 200,
    "drive_max_subida_mb": 100,
    "correo_contactos_items": (),
    "correo_max_destinatarios": 10,
    "correo_max_10min": 5,
    "correo_max_dia": 30,
    "correo_max_adjuntos_mb": 20,
    "telegram_correo": True,
    "telegram_drive": False,
    "taller_habilitado": False,
    "taller_ofrecer": True,
    "taller_timeout_s": 30.0,
    "taller_max_procesos": 16,
    "taller_max_memoria_mb": 512,
    "taller_max_salida": 16384,
}

# Variables documentadas en .env.example (§2.3). RAMS_GOOGLE_TOKEN y RAMS_TALLER_DIR son avanzadas.
VARIABLES_ENV_V022 = (
    "RAMS_GOOGLE", "RAMS_GOOGLE_SERVICIOS", "RAMS_GOOGLE_CLIENTE", "RAMS_DRIVE_COMPLETO",
    "RAMS_DRIVE_DESCARGAS", "RAMS_DRIVE_MAX_DESCARGA_MB", "RAMS_DRIVE_MAX_SUBIDA_MB",
    "RAMS_CORREO_CONTACTOS", "RAMS_CORREO_MAX_DESTINATARIOS", "RAMS_CORREO_MAX_10MIN",
    "RAMS_CORREO_MAX_DIA", "RAMS_CORREO_MAX_ADJUNTOS_MB", "RAMS_TELEGRAM_CORREO", "RAMS_TELEGRAM_DRIVE",
    "RAMS_TALLER", "RAMS_TALLER_OFRECER", "RAMS_TALLER_TIMEOUT_S", "RAMS_TALLER_MAX_PROC",
    "RAMS_TALLER_MAX_MEM_MB", "RAMS_TALLER_MAX_SALIDA",
)


def test_v022_valores_por_defecto(tmp_path: Path) -> None:
    s = cargar_settings(entorno={})
    for campo, esperado in DEFECTOS_V022.items():
        assert getattr(s, campo) == esperado, campo
    assert isinstance(s.taller_timeout_s, float) and isinstance(s.taller_max_salida, int)
    assert s.google_cliente_path == s.data_dir / "google_cliente.json"
    assert s.google_token_path == s.data_dir / "google_token.json"
    assert s.taller_dir == s.data_dir / "scripts"
    assert s.tiene_google is False and s.tiene_taller is False
    assert dict(s.correo_contactos) == {}
    directo = Settings()
    for campo, esperado in DEFECTOS_V022.items():
        assert getattr(directo, campo) == esperado, campo
    assert directo.google_cliente_path == directo.data_dir / "google_cliente.json"
    assert directo.google_token_path == directo.data_dir / "google_token.json"
    assert directo.taller_dir == directo.data_dir / "scripts"
    # Las rutas por defecto siguen a RAMS_DATA_DIR (y al respaldo de cargar_settings).
    datos = tmp_path / "datos"
    movido = cargar_settings(entorno={"RAMS_DATA_DIR": str(datos)})
    assert movido.google_cliente_path == datos / "google_cliente.json"
    assert movido.google_token_path == datos / "google_token.json"
    assert movido.taller_dir == datos / "scripts"
    respaldo = config._settings_por_defecto(tmp_path, offline=True)
    assert respaldo.google_cliente_path == tmp_path / "data" / "google_cliente.json"
    assert respaldo.google_token_path == tmp_path / "data" / "google_token.json"
    assert respaldo.taller_dir == tmp_path / "data" / "scripts"
    # asegurar_directorios NO crea taller_dir: lo crea el taller, bajo demanda, con 0o700 (§2.2).
    movido.asegurar_directorios()
    assert datos.is_dir() and not movido.taller_dir.exists()


def test_v022_fixture_settings_prueba_aislada(settings_prueba: Settings, tmp_path: Path) -> None:
    assert settings_prueba.google_habilitado is False and settings_prueba.tiene_google is False
    assert settings_prueba.taller_habilitado is False and settings_prueba.tiene_taller is False
    assert settings_prueba.google_cliente_path == tmp_path / "data" / "google_cliente.json"
    assert settings_prueba.google_token_path == tmp_path / "data" / "google_token.json"
    assert settings_prueba.taller_dir == tmp_path / "data" / "scripts"
    activado = settings_prueba.con(google_habilitado=True, taller_habilitado=True)
    assert activado.tiene_google is False                      # settings_prueba es offline
    assert activado.con(offline=False).tiene_google is True
    assert activado.tiene_taller is config._taller_soportado(sys.platform)


def test_v022_parseo_valido(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    hogar = tmp_path / "hogar"
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={
            "HOME": str(hogar),
            "RAMS_GOOGLE": "1", "RAMS_GOOGLE_SERVICIOS": " Drive, CORREO,drive,, ",
            "RAMS_GOOGLE_CLIENTE": "secretos/cliente.json", "RAMS_GOOGLE_TOKEN": "~/tok/google.json",
            "RAMS_DRIVE_COMPLETO": "sí", "RAMS_DRIVE_DESCARGAS": str(tmp_path / "bajadas"),
            "RAMS_DRIVE_MAX_DESCARGA_MB": "2048", "RAMS_DRIVE_MAX_SUBIDA_MB": "1",
            "RAMS_CORREO_MAX_DESTINATARIOS": "50", "RAMS_CORREO_MAX_10MIN": "1",
            "RAMS_CORREO_MAX_DIA": "200", "RAMS_CORREO_MAX_ADJUNTOS_MB": "24",
            "RAMS_TELEGRAM_CORREO": "0", "RAMS_TELEGRAM_DRIVE": "1",
            "RAMS_TALLER": "1", "RAMS_TALLER_OFRECER": "no", "RAMS_TALLER_TIMEOUT_S": "12,5",
            "RAMS_TALLER_MAX_PROC": "64", "RAMS_TALLER_MAX_MEM_MB": "64", "RAMS_TALLER_MAX_SALIDA": "262144",
            "RAMS_TALLER_DIR": str(tmp_path / "taller"),
        }, base_dir=tmp_path)
    assert _avisos_config(caplog) == [], [r.getMessage() for r in _avisos_config(caplog)]
    assert s.google_habilitado is True and s.google_servicios == ("correo", "drive")
    assert s.google_cliente_path == tmp_path / "secretos" / "cliente.json"   # relativa a base_dir
    assert s.google_token_path == hogar / "tok" / "google.json"
    assert s.drive_completo is True and s.drive_descargas_dir == tmp_path / "bajadas"
    assert s.drive_max_descarga_mb == 2048 and s.drive_max_subida_mb == 1
    assert s.correo_max_destinatarios == 50 and s.correo_max_10min == 1
    assert s.correo_max_dia == 200 and s.correo_max_adjuntos_mb == 24
    assert s.telegram_correo is False and s.telegram_drive is True
    assert s.taller_habilitado is True and s.taller_ofrecer is False and s.taller_timeout_s == 12.5
    assert s.taller_max_procesos == 64 and s.taller_max_memoria_mb == 64 and s.taller_max_salida == 262144
    assert s.taller_dir == tmp_path / "taller"
    assert s.tiene_google is True and s.tiene_taller is config._taller_soportado(sys.platform)
    for valor, esperado in (("drive", ("drive",)), ("correo", ("correo",)), ("DRIVE,correo", ("correo", "drive")),
                            ("correo,correo", ("correo",))):
        assert cargar_settings(entorno={"RAMS_GOOGLE_SERVICIOS": valor}).google_servicios == esperado, valor
    for valor, esperado in (("5", 5.0), ("300", 300.0), ("45.5", 45.5)):
        assert cargar_settings(entorno={"RAMS_TALLER_TIMEOUT_S": valor}).taller_timeout_s == esperado
    minimos = cargar_settings(entorno={
        "RAMS_DRIVE_MAX_DESCARGA_MB": "1", "RAMS_DRIVE_MAX_SUBIDA_MB": "2048", "RAMS_CORREO_MAX_DESTINATARIOS": "1",
        "RAMS_CORREO_MAX_10MIN": "20", "RAMS_CORREO_MAX_DIA": "1", "RAMS_CORREO_MAX_ADJUNTOS_MB": "1",
        "RAMS_TALLER_MAX_PROC": "1", "RAMS_TALLER_MAX_MEM_MB": "8192", "RAMS_TALLER_MAX_SALIDA": "1024"})
    assert (minimos.drive_max_descarga_mb, minimos.drive_max_subida_mb, minimos.correo_max_destinatarios,
            minimos.correo_max_10min, minimos.correo_max_dia, minimos.correo_max_adjuntos_mb,
            minimos.taller_max_procesos, minimos.taller_max_memoria_mb, minimos.taller_max_salida) == \
        (1, 2048, 1, 20, 1, 1, 1, 8192, 1024)
    # Regla única de ausencia: vacías o en blanco ⇒ defecto, sin aviso; RAMS_DRIVE_DESCARGAS vacía ⇒ None.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        vacias = cargar_settings(entorno={var: "  " for var in VARIABLES_ENV_V022})
    assert _avisos_config(caplog) == []
    for campo, esperado in DEFECTOS_V022.items():
        assert getattr(vacias, campo) == esperado, campo
    assert vacias.drive_descargas_dir is None


def test_v022_invalidos_usan_defecto_y_avisan_sin_valor(caplog: pytest.LogCaptureFixture) -> None:
    malos = {
        "RAMS_GOOGLE": "quizas-raro", "RAMS_GOOGLE_SERVICIOS": "correo,fotos-raras",
        "RAMS_DRIVE_COMPLETO": "talvez-raro", "RAMS_DRIVE_MAX_DESCARGA_MB": "0",
        "RAMS_DRIVE_MAX_SUBIDA_MB": "2049", "RAMS_CORREO_MAX_DESTINATARIOS": "51",
        "RAMS_CORREO_MAX_10MIN": "21", "RAMS_CORREO_MAX_DIA": "201", "RAMS_CORREO_MAX_ADJUNTOS_MB": "25",
        "RAMS_TELEGRAM_CORREO": "a-veces-raro", "RAMS_TELEGRAM_DRIVE": "x-raro", "RAMS_TALLER": "puede-raro",
        "RAMS_TALLER_OFRECER": "nunca-jamas-raro", "RAMS_TALLER_TIMEOUT_S": "0",
        "RAMS_TALLER_MAX_PROC": "65", "RAMS_TALLER_MAX_MEM_MB": "63", "RAMS_TALLER_MAX_SALIDA": "1023",
    }
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno=malos)
    for campo, esperado in DEFECTOS_V022.items():
        assert getattr(s, campo) == esperado, campo
    mensajes = " ".join(r.getMessage() for r in _avisos_config(caplog))
    for var in malos:
        assert f"para {var};" in mensajes, var
    for valor in malos.values():
        if "rar" in valor:
            assert valor not in mensajes                            # nunca el valor recibido
    assert "fotos" not in mensajes
    for var, valor, campo in (
        ("RAMS_GOOGLE_SERVICIOS", "fotos", "google_servicios"),
        ("RAMS_GOOGLE_SERVICIOS", " , ,", "google_servicios"),
        ("RAMS_GOOGLE_SERVICIOS", "correo;drive", "google_servicios"),
        ("RAMS_TALLER_TIMEOUT_S", "301", "taller_timeout_s"),
        ("RAMS_TALLER_TIMEOUT_S", "4.9", "taller_timeout_s"),
        ("RAMS_TALLER_TIMEOUT_S", "nan", "taller_timeout_s"),
        ("RAMS_TALLER_TIMEOUT_S", "inf", "taller_timeout_s"),
        ("RAMS_TALLER_TIMEOUT_S", "treinta", "taller_timeout_s"),
        ("RAMS_DRIVE_MAX_DESCARGA_MB", "200.5", "drive_max_descarga_mb"),
        ("RAMS_CORREO_MAX_DIA", "-1", "correo_max_dia"),
    ):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rams.config"):
            assert getattr(cargar_settings(entorno={var: valor}), campo) == DEFECTOS_V022[campo], (var, valor)
        avisos = [r.getMessage() for r in _avisos_config(caplog)]
        assert len(avisos) == 1 and f"para {var};" in avisos[0], (var, valor)


def test_v022_correo_contactos(caplog: pytest.LogCaptureFixture) -> None:
    crudo = ("Mamá=mama@gmail.com, Jefe   Grande = jefe@empresa.com,malo-sin-igual,x=no-es-correo,"
             "=solo@correo.co,  =  ,mama=otra.mama@gmail.com,"
             "alias_demasiado_largo_para_el_limite_31=largo@correo.co")
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={"RAMS_CORREO_CONTACTOS": crudo})
    # Alias normalizado (minúsculas, sin tildes, espacios colapsados); si se repite, gana el último.
    assert s.correo_contactos_items == (("jefe grande", "jefe@empresa.com"), ("mama", "otra.mama@gmail.com"))
    avisos = [r.getMessage() for r in _avisos_config(caplog)]
    assert len(avisos) == 1, avisos                                  # UN solo aviso por los pares malos
    assert "RAMS_CORREO_CONTACTOS" in avisos[0] and "5" in avisos[0]
    for privado in ("mama", "gmail", "no-es-correo", "malo-sin-igual", "@", "solo"):
        assert privado not in avisos[0], privado
    contactos = s.correo_contactos
    assert isinstance(contactos, types.MappingProxyType)
    assert contactos is s.correo_contactos                         # cacheado
    assert dict(contactos) == {"jefe grande": "jefe@empresa.com", "mama": "otra.mama@gmail.com"}
    with pytest.raises(TypeError):
        contactos["tia"] = "tia@correo.co"  # type: ignore[index]
    # Las direcciones (datos privados, §15.11) no salen en repr ni en resumen_seguro.
    assert "jefe@empresa.com" not in repr(s) and "otra.mama" not in repr(s)
    resumen = s.resumen_seguro()
    assert resumen["correo_contactos_items"] == 2
    assert "jefe@empresa.com" not in json.dumps(resumen, ensure_ascii=False)
    # Todos malos ⇒ () con un único aviso; vacía o ausente ⇒ () sin aviso.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        todo_malo = cargar_settings(entorno={"RAMS_CORREO_CONTACTOS": "malo,x=y,Ñ=ñ@x.co"})
    assert todo_malo.correo_contactos_items == () and dict(todo_malo.correo_contactos) == {}
    assert len(_avisos_config(caplog)) == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        assert cargar_settings(entorno={"RAMS_CORREO_CONTACTOS": ""}).correo_contactos_items == ()
        assert cargar_settings(entorno={}).correo_contactos_items == ()
    assert _avisos_config(caplog) == []
    assert cargar_settings(entorno={"RAMS_CORREO_CONTACTOS": "Ñoño_1=a@b.co"}).correo_contactos_items == \
        (("nono_1", "a@b.co"),)
    # Robusto ante una copia con datos raros (con()), sin lanzar.
    raro = s.con(correo_contactos_items=[["tia", "tia@correo.co"], ("MAL", "x@y.co"), ("z",),
                                         ("p", "no-correo"), 7])
    assert dict(raro.correo_contactos) == {"tia": "tia@correo.co"}


def test_v022_propiedades() -> None:
    entorno = {"RAMS_GOOGLE": "1", "RAMS_TALLER": "1"}
    s = cargar_settings(entorno=entorno)
    assert s.tiene_google is True
    assert s.con(offline=True).tiene_google is False
    assert cargar_settings(entorno=entorno, offline=True).tiene_google is False
    assert cargar_settings(entorno={**entorno, "RAMS_OFFLINE": "1"}).tiene_google is False
    assert s.con(google_habilitado=False).tiene_google is False
    for plataforma, esperado in (("win32", True), ("linux", True), ("linux2", True), ("darwin", False),
                                 ("cygwin", False), ("freebsd14", False)):
        assert config._taller_soportado(plataforma) is esperado, plataforma
    assert s.tiene_taller is config._taller_soportado(sys.platform)
    assert s.con(offline=True).tiene_taller is s.tiene_taller          # no depende de offline
    assert s.con(taller_habilitado=False).tiene_taller is False
    resumen = s.resumen_seguro()
    assert resumen["tiene_google"] is True and resumen["tiene_taller"] is s.tiene_taller
    assert resumen["google_servicios"] == ["correo", "drive"]
    json.dumps(resumen, ensure_ascii=False)
    assert isinstance(hash(s), int) and hash(s) == hash(cargar_settings(entorno=entorno))
    copia = s.con(google_servicios=("drive",), taller_timeout_s=60.0, drive_descargas_dir=Path("x"))
    assert copia.google_servicios == ("drive",) and copia.taller_timeout_s == 60.0
    assert isinstance(hash(copia), int) and s.google_servicios == ("correo", "drive")
    with pytest.raises(AttributeError):
        s.google_habilitado = False  # type: ignore[misc]


def test_env_example_v022_sin_avisos(caplog: pytest.LogCaptureFixture) -> None:
    ruta = RAIZ_PROYECTO / ".env.example"
    texto = ruta.read_bytes().decode("utf-8")
    assert "\r" not in texto and not texto.startswith("\ufeff")
    seccion = "# --- v0.2.2: Google (Gmail y Drive) y taller de scripts ---"
    assert seccion in texto
    assert all(texto.index(seccion) > texto.index(otra) for otra in (
        "# --- Telegram multimedia ---", "# --- Respaldo Puter", "# --- Visión continua"))   # va al final
    lineas = texto.split("\n")
    for var in VARIABLES_ENV_V022:
        indices = [i for i, linea in enumerate(lineas) if linea.startswith(f"{var}=")]
        assert len(indices) == 1, var
        assert lineas[indices[0] - 1].startswith("# "), var      # una línea de comentario en español
    for var in ("RAMS_GOOGLE_TOKEN", "RAMS_TALLER_DIR"):         # avanzadas: no se listan
        assert var not in texto, var
    assert "# mama=mama@gmail.com,jefe=jefe@empresa.com" in texto
    assert "data/google_cliente.json" in texto and "§17" in texto
    leido = leer_env_archivo(ruta)
    for var in ("RAMS_GOOGLE_CLIENTE", "RAMS_DRIVE_DESCARGAS", "RAMS_CORREO_CONTACTOS"):
        assert leido[var] == "", var                              # vacías = defecto, sin aviso
    with caplog.at_level(logging.DEBUG, logger="rams.config"):
        s = cargar_settings(entorno=leido)
    assert _avisos_config(caplog) == [], [r.getMessage() for r in _avisos_config(caplog)]
    for campo, esperado in DEFECTOS_V022.items():
        assert getattr(s, campo) == esperado, campo
    base = cargar_settings(entorno={})
    for campo in ("google_cliente_path", "google_token_path", "taller_dir", "drive_descargas_dir"):
        assert getattr(s, campo) == getattr(base, campo), campo


def test_v022_constantes_de_contrato() -> None:
    assert SERVICIOS_GOOGLE == ("correo", "drive")
    assert CARPETAS_USUARIO == ("descargas", "documentos", "escritorio", "imagenes", "musica", "videos")
    assert DESTINOS_DESCARGA == ("descargas", "documentos", "escritorio")
    assert set(DESTINOS_DESCARGA) <= set(CARPETAS_USUARIO)
    assert OPERACIONES_DRIVE == ("buscar", "info", "descargar", "leer", "crear_carpeta", "subir", "papelera",
                                 "compartir")
    assert FORMATOS_DRIVE == ("pdf", "texto", "original")
    assert ROLES_DRIVE == ("reader", "commenter", "writer")
    assert CLASES_TARJETA == ("correo", "drive", "script", "salida")
    assert ESTADOS_TARJETA == ("pendiente", "info", "cerrada")
    assert BOTONES_TARJETA == ("aprobar", "aprobar_publico", "editar", "guardar", "copiar", "descartar")
    assert DECISIONES_TARJETA == ("aprobar", "aprobar_publico", "editar", "guardar", "descartar")
    assert set(DECISIONES_TARJETA) == set(BOTONES_TARJETA) - {"copiar"}      # "copiar" es local
    assert RESULTADOS_TARJETA == ("enviado", "hecho", "ejecutado", "descartado", "caducado", "reemplazado",
                                  "denegado", "fallo")
    assert LENGUAJES_SCRIPT == ("powershell", "python", "bash")
    assert NIVELES_RIESGO == ("bajo", "medio", "alto", "critico")
    assert NIVELES_EJECUTABLES == frozenset({"bajo", "medio", "alto"})
    assert isinstance(NIVELES_EJECUTABLES, frozenset) and "critico" not in NIVELES_EJECUTABLES
    assert CATEGORIAS_RIESGO == ("borrado", "sobrescritura", "registro", "servicios", "red", "descarga_ejecucion",
                                 "credenciales", "elevacion", "apagado", "seguridad", "persistencia",
                                 "ofuscacion", "dinamico", "fuera_de_carpeta", "subproceso", "winapi",
                                 "interactivo", "sintaxis", "tamano", "analisis_degradado")
    assert len(set(CATEGORIAS_RIESGO)) == 20
    assert (TTL_TARJETA_S, TTL_TARJETA_TELEGRAM_S, TTL_DOBLE_CONFIRMACION_S) == (1800.0, 43200.0, 120.0)
    assert all(isinstance(v, float) for v in (TTL_TARJETA_S, TTL_TARJETA_TELEGRAM_S, TTL_DOBLE_CONFIRMACION_S))
    assert MAX_TARJETAS_VIVAS == 4 == len(CLASES_TARJETA)
    assert MAX_CODIGO_SCRIPT == 24_576                       # ya NO es 32 768 (§21.1, hallazgo 7)
    assert MAX_SALIDA_TARJETA == 8_192 and MAX_HALLAZGOS_TARJETA == 20
    assert MAX_BYTES_TARJETA == MAX_BYTES_DECISION_TARJETA == 61_440
    assert MAX_BYTES_TARJETA < 65_536 and MAX_BYTES_DECISION_TARJETA < 65_536
    assert 2 * MAX_CODIGO_SCRIPT < MAX_BYTES_TARJETA          # el código escapado (peor caso) cabe
    assert (MAX_CUERPO_CORREO, MAX_ASUNTO_CORREO, MAX_ADJUNTOS_CORREO) == (10_000, 200, 5)
    # Lo que NO cambia en la v0.2.2.
    assert len(TIPOS_ACCION) == 13 and ORIGENES_ACCION == ("usuario", "iniciativa", "nexus", "telegram", "llm")
    assert CANALES_ENTRADA == ("terminal", "telegram", "nexus", "escritorio")
    import core.contracts as contratos

    assert contratos.VERSION == "0.1.0"
    for nombre in ("SERVICIOS_GOOGLE", "CARPETAS_USUARIO", "DESTINOS_DESCARGA", "OPERACIONES_DRIVE",
                   "FORMATOS_DRIVE", "ROLES_DRIVE", "CLASES_TARJETA", "ESTADOS_TARJETA", "BOTONES_TARJETA",
                   "DECISIONES_TARJETA", "RESULTADOS_TARJETA", "LENGUAJES_SCRIPT", "NIVELES_RIESGO",
                   "NIVELES_EJECUTABLES", "CATEGORIAS_RIESGO", "TTL_TARJETA_S", "TTL_TARJETA_TELEGRAM_S",
                   "TTL_DOBLE_CONFIRMACION_S", "MAX_TARJETAS_VIVAS", "MAX_CODIGO_SCRIPT", "MAX_SALIDA_TARJETA",
                   "MAX_HALLAZGOS_TARJETA", "MAX_BYTES_TARJETA", "MAX_BYTES_DECISION_TARJETA",
                   "MAX_CUERPO_CORREO", "MAX_ASUNTO_CORREO", "MAX_ADJUNTOS_CORREO", "ID_TARJETA_RE",
                   "HUELLA_RE", "CORREO_RE", "PATRON_ARCHIVO_SENSIBLE", "huella_contenido", "largo_utf16",
                   "bytes_json"):
        assert nombre in contratos.__all__ and hasattr(contratos, nombre), nombre
    assert "carpetas_usuario" in config.__all__
    # Regex de contrato (los consumidores usan fullmatch: "$" admitiría un "\n" final).
    assert ID_TARJETA_RE.fullmatch("0a1b2c3d") and HUELLA_RE.fullmatch("ab" * 32)
    for malo in ("0A1B2C3D", "0a1b2c3", "0a1b2c3d9", "0a1b2c3g", ""):
        assert ID_TARJETA_RE.fullmatch(malo) is None, malo
    for malo in ("AB" * 32, "ab" * 31, "ab" * 33, "g" * 64):
        assert HUELLA_RE.fullmatch(malo) is None, malo
    for bueno in ("juan.perez+x@mail.example.co", "a@b.co", "MAMA_1@Gmail.COM", "x%y@sub-dominio.empresa.travel"):
        assert CORREO_RE.fullmatch(bueno), bueno
    for malo in ("a@b", "a b@c.co", "@x.co", "a@.co", "a@b.c", "a@@b.co", "a" * 65 + "@b.co", "ñ@b.co",
                 "a@b.c0m", "a@b.co\n", "<a@b.co>"):
        assert CORREO_RE.fullmatch(malo) is None, malo
    sensible = re.compile(PATRON_ARCHIVO_SENSIBLE)
    for nombre in (".env", ".env.local", "proyecto/.env", "c:/users/yo/.ssh/id_rsa", "id_ed25519.pub",
                   "claves.kdbx", "cert.pem", "privada.key", "vpn.ovpn", "login data", "cookies", "web data",
                   "key4.db", "logins.json", "ctl_token", "escritorio.json", "google_cliente.json",
                   "data/google_token.json", "client_secret_123.apps.googleusercontent.com.json"):
        assert sensible.search(nombre), nombre
    for nombre in ("informe.pdf", "fotos/playa.jpg", "notas.env.txt", "mi.env", "keys.txt", "cookies.txt",
                   "client_secret.txt", "escritorio.json.bak"):
        assert sensible.search(nombre) is None, nombre


def test_huella_contenido(caplog: pytest.LogCaptureFixture) -> None:
    base = {"clase": "correo", "para": ["a@b.co"], "asunto": "Hola", "cuerpo": "Línea 1\nLínea 2", "n": 3,
            "listo": True, "nada": None}
    huella = huella_contenido(base)
    assert HUELLA_RE.fullmatch(huella)
    assert huella == huella_contenido(dict(base)) == huella_contenido(base)            # determinista
    canonico = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert huella == hashlib.sha256(canonico.encode("utf-8")).hexdigest()
    assert huella_contenido(dict(reversed(list(base.items())))) == huella              # orden de claves
    assert huella_contenido(types.MappingProxyType(base)) == huella
    assert huella_contenido({**base, "para": ("a@b.co",)}) == huella                   # tupla = lista
    assert huella_contenido({**base, "cuerpo": "Línea 1\r\nLínea 2"}) == huella        # CRLF = LF
    assert huella_contenido({**base, "cuerpo": "Línea 1\rLínea 2"}) == huella          # CR = LF
    descompuesta = unicodedata.normalize("NFD", base["cuerpo"])
    assert descompuesta != base["cuerpo"]
    assert huella_contenido({**base, "cuerpo": descompuesta}) == huella                # NFC
    assert huella_contenido({"a": {"b": [("x\r\n", {"c": "e\u0301"})]}}) == \
        huella_contenido({"a": {"b": [["x\n", {"c": "é"}]]}})                            # recursivo
    assert huella_contenido({}) == hashlib.sha256(b"{}").hexdigest()
    for campo, otro in (("para", ["c@d.co"]), ("asunto", "Hola."), ("cuerpo", "Línea 1"), ("n", 4),
                        ("listo", False), ("nada", 0), ("clase", "drive")):
        assert huella_contenido({**base, campo: otro}) != huella, campo                 # sensible a cada campo
    assert huella_contenido({**base, "extra": 1}) != huella
    # No serializable ⇒ "" (fail-closed), nunca lanza.
    circular: dict[str, object] = {}
    circular["yo"] = circular
    for malo in ({"x": b"bytes"}, {"x": float("nan")}, {"x": float("inf")}, {"x": object()}, {"x": {1, 2}},
                 {"x": "\ud800"}, {"\udfff": 1}, {1: "a", "1": "b"}, {"e\u0301": 1, "é": 2}, circular,
                 ["no", "es", "mapping"], "texto", None):
        assert huella_contenido(malo) == "", repr(malo)[:40]  # type: ignore[arg-type]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="rams.contracts"):
        assert huella_contenido({"clave": b"clave-super-privada"}) == ""
    assert caplog.records and all("clave-super-privada" not in r.getMessage() for r in caplog.records)


def test_largo_utf16_y_bytes_json() -> None:
    assert largo_utf16("hola") == 4 == len("hola")                 # ASCII = len
    assert largo_utf16("é") == 1 and largo_utf16("e\u0301") == 2
    assert largo_utf16("😀") == 2 and largo_utf16("a😀b") == 4     # fuera del BMP cuenta 2
    assert largo_utf16("") == 0 and largo_utf16("\ud800") == 1     # sustituto suelto: 1 unidad, sin lanzar
    assert largo_utf16("x" * 10_000 + "😀") == 10_002
    for no_str in (None, 5, b"abc", ["a"], 3.5):
        assert largo_utf16(no_str) == 0, no_str  # type: ignore[arg-type]
    for valor in ({"tipo": "tarjeta", "datos": {"titulo": "Café ☕", "n": [1, 2.5, None, True]}}, "ñandú😀",
                  [], {}, 0, ("a", "b"), "línea\ncon\ttab \"comillas\" y \\"):
        assert bytes_json(valor) == len(json.dumps(valor, ensure_ascii=False, separators=(",", ":")).encode()), \
            valor
    assert bytes_json("a\nb") - bytes_json("ab") == 2              # "\n" escapado ocupa 2 bytes
    assert bytes_json("\n") == 4 and bytes_json("\t") == 4 and bytes_json('"') == 4 and bytes_json("\\") == 4
    assert bytes_json("é") == 4 and bytes_json("😀") == 6          # 2 comillas + bytes UTF-8
    assert bytes_json((1, 2)) == bytes_json([1, 2])
    for malo in ("\ud800", {"x": "\udfff"}, float("nan"), float("inf"), {"x": float("-inf")}, b"x", object(),
                 {1, 2}):
        assert bytes_json(malo) == sys.maxsize, repr(malo)


@pytest.mark.parametrize(("texto", "prohibido", "esperado"), [
    ("token de acceso ya29." + "a0AfB_x" * 4 + " fin", "ya29.a0AfB", "«token-google»"),             # 3b
    ("Authorization: Bearer ya29." + "a0AfB_x" * 4, "ya29.a0AfB", "Authorization: «secreto»"),     # 3b + 5
    ('{"refresh_token": "1//0g' + "Ab-_9" * 6 + '"}', "1//0gAb", '"«token-google»"'),               # 3b
    ("secreto GOCSPX-" + "AbCdEf_-123" + " fin", "GOCSPX-AbCd", "«secreto-cliente»"),               # 3b
    ("client_secret=mi-valor-del-cliente y más", "mi-valor-del-cliente", "client_secret=«secreto»"),  # 5
    ("client_secret: GOCSPX-" + "AbCdEf_-123", "GOCSPX-", "client_secret: «secreto»"),              # 3b + 5
    ("refresh_token: r3fr3sc0-valor", "r3fr3sc0", "refresh_token: «secreto»"),                       # 5
    ("access_token=tok-acceso-1", "tok-acceso-1", "access_token=«secreto»"),                         # 5
    ("id_token: eyJhbGciOi.x.y", "eyJhbGciOi", "id_token: «secreto»"),                               # 5
    ("code_verifier=verif-123-abc", "verif-123-abc", "code_verifier=«secreto»"),                     # 5
    ("http://127.0.0.1:5555/?code=4/0AbcDEF&state=xyz789", "4/0AbcDEF", "?code=«secreto»&state=«secreto»"),
    ("GET /?state=st4t3XYZ&code=4/0Ab-c&scope=gmail.send HTTP/1.1", "st4t3XYZ",
     "?state=«secreto»&code=«secreto»&scope=gmail.send"),                                            # 7
    ("https://oauth2.googleapis.com/revoke?x=1&refresh_token=r3fr3sc0", "r3fr3sc0", "&refresh_token=«secreto»"),
    # La regla 5 (asignación) se come el resto hasta el espacio: también los parámetros siguientes.
    ("https://h/cb?access_token=abc&code_verifier=v3r1f", "v3r1f", "?access_token=«secreto»"),
    ("https://h/cb?x=1&code_verifier=v3r1f&state=st", "v3r1f", "&code_verifier=«secreto»"),
])
def test_redactar_google_oauth(texto: str, prohibido: str, esperado: str) -> None:
    redactado = redactar(texto)
    assert prohibido not in redactado
    assert esperado in redactado
    assert redactar(redactado) == redactado                         # idempotente


def test_redactar_oauth_solo_en_parametros_de_url() -> None:
    # code y state solo se tapan como parámetro de URL ([?&]x=), nunca en texto suelto.
    for frase in ("el code=1234 del ejemplo", "state=ok en una frase", "el código de estado", "1//corto",
                  "ya29.corto", "GOCSPX-corto", "client secret faltante", "refresh token caducado",
                  "consulta de la API: código 200, estado ok"):
        assert redactar(frase) == frase, frase
    assert parece_secreto("el estado del correo es enviado") is False
    registro = logging.LogRecord("rams.google", logging.INFO, __file__, 1, "token %s y %s",
                                 ("ya29." + "x" * 30, "GOCSPX-" + "y" * 20), None)
    FiltroSecretos().filter(registro)
    assert "ya29" not in registro.getMessage() and "GOCSPX" not in registro.getMessage()


class _EntornoVigilado(dict):
    """Entorno falso que anota qué variables se leen (carpetas_usuario solo puede leer tres)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.leidas: set[str] = set()

    def get(self, clave: str, defecto: object = None) -> object:  # type: ignore[override]
        self.leidas.add(clave)
        return super().get(clave, defecto)

    def __getitem__(self, clave: str) -> object:
        self.leidas.add(clave)
        return super().__getitem__(clave)


_VARIABLES_CARPETAS = {"USERPROFILE", "HOME", "XDG_CONFIG_HOME"}


def test_carpetas_usuario_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_carpetas_windows_api",
                        lambda: pytest.fail("en Linux nunca se llama a la API de Windows"))
    hogar = tmp_path / "hogar"
    musica = tmp_path / "otra" / "Musica"
    for ruta in (hogar / "Descargas", hogar / "Downloads", hogar / "Documentos", hogar / "Desktop",
                 hogar / "Pictures", musica):
        ruta.mkdir(parents=True)
    (hogar / ".config").mkdir()
    (hogar / ".config" / "user-dirs.dirs").write_text(
        "# Generado por xdg-user-dirs-update\n"
        'XDG_DOWNLOAD_DIR="$HOME/Descargas"\n'
        'XDG_DOCUMENTS_DIR="${HOME}/Documentos"\n'
        'XDG_DESKTOP_DIR="$HOME/"\n'                 # desactivada: nunca el propio $HOME (→ ~/Desktop)
        'XDG_PICTURES_DIR="relativa/Imagenes"\n'     # relativa: se ignora (→ ~/Pictures)
        f'XDG_MUSIC_DIR="{musica}"\n'                # absoluta
        'XDG_VIDEOS_DIR="$HOME/NoExiste"\n'          # no existe (y ~/Videos tampoco): no aparece
        'XDG_TEMPLATES_DIR="$HOME/Plantillas"\n'
        "línea que no se entiende\n", encoding="utf-8")
    entorno = _EntornoVigilado({"HOME": str(hogar), "USERPROFILE": "C:\\no\\se\\usa", "PATH": "x",
                                "GEMINI_API_KEY": "nunca-se-lee", "XDG_DOWNLOAD_DIR": "/tampoco"})
    carpetas = carpetas_usuario(entorno=entorno, plataforma="linux", usar_api=True)
    assert carpetas == {"descargas": hogar / "Descargas", "documentos": hogar / "Documentos",
                        "escritorio": hogar / "Desktop", "imagenes": hogar / "Pictures", "musica": musica}
    assert list(carpetas) == [c for c in CARPETAS_USUARIO if c in carpetas]     # orden del catálogo
    assert entorno.leidas <= _VARIABLES_CARPETAS, entorno.leidas
    # XDG_CONFIG_HOME absoluto manda; nunca una carpeta que contenga al $HOME.
    config_xdg = tmp_path / "cfg"
    config_xdg.mkdir()
    (hogar / "Bajadas").mkdir()
    (config_xdg / "user-dirs.dirs").write_text(
        f'XDG_DOWNLOAD_DIR="$HOME/Bajadas"\nXDG_MUSIC_DIR="{tmp_path}"\nXDG_VIDEOS_DIR="$OTRA/Videos"\n',
        encoding="utf-8")
    otro = carpetas_usuario(entorno={"HOME": str(hogar), "XDG_CONFIG_HOME": str(config_xdg)},
                            plataforma="linux", usar_api=False)
    assert otro == {"descargas": hogar / "Bajadas", "escritorio": hogar / "Desktop",
                    "imagenes": hogar / "Pictures"}
    # Un XDG_CONFIG_HOME relativo se ignora (se usa ~/.config).
    assert carpetas_usuario(entorno={"HOME": str(hogar), "XDG_CONFIG_HOME": "relativa/cfg"},
                            plataforma="linux", usar_api=False) == carpetas
    # Sin user-dirs.dirs: los nombres por defecto bajo $HOME (también en otros sistemas).
    sin_archivo = tmp_path / "otro_hogar"
    for nombre in ("Downloads", "Videos"):
        (sin_archivo / nombre).mkdir(parents=True)
    (sin_archivo / "Music").write_text("no soy una carpeta", encoding="utf-8")
    esperado = {"descargas": sin_archivo / "Downloads", "videos": sin_archivo / "Videos"}
    assert carpetas_usuario(entorno={"HOME": str(sin_archivo)}, plataforma="linux", usar_api=False) == esperado
    assert carpetas_usuario(entorno={"HOME": str(sin_archivo)}, plataforma="darwin") == esperado
    # Un user-dirs.dirs basura o enorme se ignora sin lanzar.
    raro = tmp_path / "hogar_raro"
    (raro / ".config").mkdir(parents=True)
    (raro / "Downloads").mkdir()
    (raro / ".config" / "user-dirs.dirs").write_bytes(b"\xff\xfe\x00basura\n" + b"x" * 70_000)
    assert carpetas_usuario(entorno={"HOME": str(raro)}, plataforma="linux", usar_api=False) == \
        {"descargas": raro / "Downloads"}


def test_carpetas_usuario_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    perfil = tmp_path / "perfil"
    for nombre in ("Downloads", "Documents", "Videos"):
        (perfil / nombre).mkdir(parents=True)
    (perfil / "Music").write_text("no soy una carpeta", encoding="utf-8")
    monkeypatch.setattr(config, "_carpetas_windows_api",
                        lambda: pytest.fail("usar_api=False no llama a SHGetKnownFolderPath"))
    entorno = _EntornoVigilado({"USERPROFILE": str(perfil), "HOME": str(tmp_path / "otro"),
                                "APPDATA": "nunca", "LOCALAPPDATA": "nunca", "GEMINI_API_KEY": "nunca-se-lee"})
    esperado = {"descargas": perfil / "Downloads", "documentos": perfil / "Documents", "videos": perfil / "Videos"}
    assert carpetas_usuario(entorno=entorno, plataforma="win32", usar_api=False) == esperado
    assert entorno.leidas <= _VARIABLES_CARPETAS, entorno.leidas
    assert carpetas_usuario(entorno={"HOME": str(perfil)}, plataforma="win32", usar_api=False) == esperado
    # Con un entorno explícito sin perfil (o con uno relativo) nunca se mira el perfil real.
    assert carpetas_usuario(entorno={}, plataforma="win32", usar_api=False) == {}
    assert carpetas_usuario(entorno={}, plataforma="linux", usar_api=False) == {}
    assert carpetas_usuario(entorno={"USERPROFILE": "relativa"}, plataforma="win32", usar_api=False) == {}
    # Con la API (doble): manda la carpeta conocida (OneDrive, carpetas movidas); si no existe, o es
    # el propio perfil, se usa el nombre bajo %USERPROFILE%.
    onedrive = tmp_path / "OneDrive" / "Escritorio"
    onedrive.mkdir(parents=True)
    (perfil / "Desktop").mkdir()
    llamadas: list[int] = []

    def api_falsa() -> dict[str, Path]:
        llamadas.append(1)
        return {"escritorio": onedrive, "documentos": tmp_path / "no_existe", "imagenes": perfil}

    monkeypatch.setattr(config, "_carpetas_windows_api", api_falsa)
    assert carpetas_usuario(entorno={"USERPROFILE": str(perfil)}, plataforma="win32") == {
        "descargas": perfil / "Downloads", "documentos": perfil / "Documents", "escritorio": onedrive,
        "videos": perfil / "Videos"}
    assert llamadas == [1]
    carpetas_usuario(entorno={"HOME": str(perfil)}, plataforma="linux")
    assert llamadas == [1]                                       # en Linux nunca


def test_carpetas_usuario_nunca_lanza(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class EntornoRoto(dict):
        def get(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
            raise RuntimeError("entorno roto")

    assert carpetas_usuario(entorno=EntornoRoto(), plataforma="linux", usar_api=False) == {}
    assert carpetas_usuario(entorno=object(), plataforma="win32", usar_api=False) == {}  # type: ignore[arg-type]
    assert carpetas_usuario(entorno={"HOME": "a\x00b"}, plataforma="linux", usar_api=False) == {}

    def api_rota() -> dict[str, Path]:
        raise RuntimeError("API rota")

    api_original = config._carpetas_windows_api
    monkeypatch.setattr(config, "_carpetas_windows_api", api_rota)
    (tmp_path / "Downloads").mkdir()
    assert carpetas_usuario(entorno={"USERPROFILE": str(tmp_path)}, plataforma="win32") == \
        {"descargas": tmp_path / "Downloads"}
    if sys.platform != "win32":   # fuera de Windows la API no existe: {} sin lanzar (nunca las carpetas reales)
        assert api_original() == {}


def test_sin_efectos_pc_v022_shells_y_limites(correr) -> None:
    # §3.5: ningún test lanza una shell real ni crea unidades o límites reales; sys.executable sí.
    for argumentos in (["bash", "-c", "echo hola"], ["C:\\Program Files\\Git\\bin\\bash.exe", "-lc", "x"],
                       ["/bin/sh", "-c", "x"], ["zsh"], ["dash", "-c", "x"], ["pwsh", "-NoProfile"],
                       ["pwsh.exe", "-c", "x"], ["systemd-run", "--user", "--scope", "x"],
                       ["/usr/bin/systemctl", "--user", "show"], ["prlimit", "--as=1", "x"]):
        with pytest.raises(pytest.fail.Exception):
            subprocess.Popen(argumentos)

    async def principal() -> None:
        await asyncio.create_subprocess_exec("systemd-run", "--user", "--scope", "true")

    with pytest.raises(pytest.fail.Exception, match="systemd-run"):
        correr(principal())
    resultado = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True,
                               timeout=60, check=False)
    assert resultado.returncode == 0 and resultado.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Perfil heredado de Google Takeout (core/takeout_ingestor.py)
# ---------------------------------------------------------------------------


def test_perfil_takeout_defectos_y_entorno(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    s = cargar_settings(entorno={})
    assert s.perfil_takeout_path == s.memoria_dir / "perfil_takeout.json"
    assert s.perfil_takeout_path.parent == s.perfil_path.parent
    assert s.perfil_takeout_en_prompt is True
    otro = tmp_path / "otro" / "takeout.json"
    s = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path / "d"), "RAMS_PERFIL_TAKEOUT_EN_PROMPT": "0",
                                 "RAMS_PERFIL_TAKEOUT_PATH": str(otro)})
    assert s.perfil_takeout_en_prompt is False and s.perfil_takeout_path == otro
    assert cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path / "d")}).perfil_takeout_path == \
        tmp_path / "d" / "john" / "perfil_takeout.json"
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        s = cargar_settings(entorno={"RAMS_PERFIL_TAKEOUT_EN_PROMPT": "talvez-secreto"})
    assert s.perfil_takeout_en_prompt is True
    avisos = _avisos_config(caplog)
    assert any("RAMS_PERFIL_TAKEOUT_EN_PROMPT" in r.getMessage() for r in avisos)
    assert all("talvez-secreto" not in r.getMessage() for r in avisos)
    assert config._settings_por_defecto(tmp_path, offline=True).perfil_takeout_path == \
        tmp_path / "data" / "john" / "perfil_takeout.json"


def test_env_example_documenta_el_perfil_takeout(caplog: pytest.LogCaptureFixture) -> None:
    ruta = RAIZ_PROYECTO / ".env.example"
    lineas = ruta.read_bytes().decode("utf-8").split("\n")
    indices = [i for i, linea in enumerate(lineas) if linea.startswith("RAMS_PERFIL_TAKEOUT_EN_PROMPT=")]
    assert len(indices) == 1 and lineas[indices[0]] == "RAMS_PERFIL_TAKEOUT_EN_PROMPT=1"
    assert lineas[indices[0] - 1].startswith("# ")                    # comentario en español justo encima
    assert indices[0] > lineas.index("RAMS_PERFIL_EN_PROMPT=1")        # junto al perfil de WhatsApp
    assert not any(linea.startswith("RAMS_PERFIL_TAKEOUT_PATH") for linea in lineas)   # avanzada: no se lista
    with caplog.at_level(logging.DEBUG, logger="rams.config"):
        s = cargar_settings(entorno=leer_env_archivo(ruta))
    assert _avisos_config(caplog) == []
    assert s.perfil_takeout_en_prompt is True
