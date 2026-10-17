"""Tests de core/config.py, core/bus.py y core/contracts.py (grupo config-bus, §12.3).

Sin red ni hardware. Sin pytest-asyncio: las pruebas asíncronas usan ``asyncio.run`` (fixture
``correr`` de tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
import types
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
    configurar_logging,
    en_horario_silencioso,
    leer_env_archivo,
    parsear_env_texto,
    redactar,
    zona_local,
)
from core.contracts import (
    CANALES_ENTRADA,
    DIVERGENCIA_STEINS_GATE,
    MODOS_SALIDA,
    TIPOS_ACCION,
    BrocaReply,
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
    formatear_decimal,
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
    assert CANALES_ENTRADA == ("terminal", "telegram", "nexus")
    assert "comando" not in TIPOS_ACCION and len(TIPOS_ACCION) == 8


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
