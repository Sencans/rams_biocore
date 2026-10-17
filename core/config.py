"""Configuración de R.A.M.S. BIO-CORE: ``Settings`` desde ``.env``, logging y hora local.

Qué hace:
- Carga un ``Settings`` inmutable (``dataclass(frozen=True)``, *hashable*) desde ``.env`` y
  ``os.environ`` (o desde un mapeo explícito, para los tests), con validación de formatos y
  la **regla única de ausencia** (§4.1.1): ausente, vacío, solo espacios o empieza por ``#``
  ⇒ valor por defecto del campo.
- Configura el logging en español (niveles traducidos, archivo rotativo 0o600 y consola) con
  un filtro que redacta secretos en el mensaje, en la traza (``exc_text``) y en ``stack_info``.
- Ofrece la hora local (``America/Bogota``) y el cálculo del horario silencioso.
- v0.2 "Amadeus Companion" (``docs/COMPANION.md`` §C2.1): ajustes de Amadeus Desktop, del
  control del PC y travesuras, y de la visión continua; y las funciones puras
  :func:`parece_secreto` y :func:`titulo_sensible` (base de la regla anti-contraseñas, §C2.12).

Dependencias: solo la biblioteca estándar y ``core.contracts`` (que no importa este módulo: no
hay ciclo). ``python-dotenv`` es opcional: si falta, se usa un parser mínimo propio que imita a
dotenv.

Degradación: ``cargar_settings`` nunca lanza por valores de entorno (valor inválido ⇒ valor
por defecto y ``log.warning`` sin el valor recibido). Sin ``tzdata`` la zona horaria cae a
UTC−5 ("COT"), registrado una sola vez.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import logging.handlers
import math
import os
import re
import string
import sys
import threading
import types
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.contracts import DESTINOS_BASE, MODOS_PANTALLA, PATRON_TITULO_SENSIBLE, SAFE_ALIAS_RE

try:  # opcional: si falta, se usa el parser mínimo propio
    from dotenv import dotenv_values
except ImportError:  # sin python-dotenv: parser mínimo
    dotenv_values = None

__all__ = [
    "VERSION",
    "BASE_DIR_DEFECTO",
    "APPS_DEFECTO",
    "NOMBRES_NIVEL",
    "MENSAJEROS",
    "VISION_APPS_PAUSA_DEFECTO",
    "VISION_DETECTORES",
    "VTUBER_AI_TEMAS",
    "Settings",
    "cargar_settings",
    "leer_env_archivo",
    "parsear_env_texto",
    "configurar_logging",
    "FiltroSecretos",
    "redactar",
    "parece_secreto",
    "titulo_sensible",
    "normalizar_titulo",
    "nombre_nivel",
    "zona_local",
    "ahora_local",
    "en_horario_silencioso",
]

log = logging.getLogger("rams.config")

VERSION: Final = "0.1.0"
BASE_DIR_DEFECTO: Final[Path] = Path(__file__).resolve().parent.parent
APPS_DEFECTO: Final[tuple[tuple[str, str], ...]] = (
    ("terminal", "kitty"),
    ("navegador", "firefox"),
    ("nexus", "nexus-ide"),
)

# Nombres de nivel en español (§3.3).
NOMBRES_NIVEL: Final[dict[int, str]] = {
    logging.DEBUG: "DEPURACIÓN",
    logging.INFO: "INFO",
    logging.WARNING: "AVISO",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRÍTICO",
}

# Alias aceptados en RAMS_LOG_LEVEL → nombre canónico de ``logging``.
_ALIAS_NIVEL: Final[dict[str, str]] = {
    "DEBUG": "DEBUG",
    "DEPURACION": "DEBUG",
    "DEPURACIÓN": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "WARN": "WARNING",
    "AVISO": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
    "CRITICO": "CRITICAL",
    "CRÍTICO": "CRITICAL",
}

_VERDADEROS: Final = frozenset({"1", "true", "t", "si", "sí", "s", "yes", "y", "on"})
_FALSOS: Final = frozenset({"0", "false", "f", "no", "n", "off"})

# Validación de formatos de secretos (§4.1.1).
_RE_TOKEN_TELEGRAM: Final = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
_RE_CHAT_ID: Final = re.compile(r"^-?\d+$")
_RE_CLAVE: Final = re.compile(r"^[^\s#]{8,}$")

# RAMS_APPS (§4.1.1).
_RE_ALIAS_APP: Final = re.compile(r"^[a-z0-9_]{1,24}$")
_RE_CMD_APP: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Otros formatos (valores no secretos).
_RE_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_RE_MODELO: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_RE_VOZ_ID: Final = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
_RE_VOZ_EDGE: Final = re.compile(r"^[A-Za-z0-9\-]{3,80}$")
_RE_ZONA: Final = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$")
_RE_REGION_WEB: Final = re.compile(r"^[a-z]{2,3}-[a-z]{2,3}$")
_RE_WS_URL: Final = re.compile(r"^wss?://\S+$")
_RE_URL_LOCAL: Final = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?/?$")
_RE_LANG_BCP47: Final = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
_RE_VOZ_NAVEGADOR: Final = re.compile(r"^[A-Za-z0-9 ,_\-]{1,120}$")
_RE_HOST: Final = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
_RE_HHMM: Final = re.compile(r"^(\d{1,2}):(\d{2})$")
_RE_SALIDA_11L: Final = re.compile(r"^(?:pcm_)?(\d+)$", re.IGNORECASE)
_RE_XDG: Final = re.compile(r"\$\{XDG_RUNTIME_DIR\}|\$XDG_RUNTIME_DIR(?![A-Za-z0-9_])")

_FRECUENCIAS_11L: Final = frozenset({16000, 22050, 24000})
_NIVELES_THINKING: Final = ("minimal", "low", "medium", "high")
_MODOS_HYPR: Final = ("auto", "lua", "legacy")
_FUENTES_CONECTOMA: Final = ("reducido", "stub", "flywire")
# RAMS_MENSAJERO: quién entrega los mensajes proactivos por Telegram (CONTRATO de cierre, §10.19).
MENSAJEROS: Final = ("hibrido", "megumin", "amadeus")

# v0.2 (§C2.1): catálogos de opciones y valores por defecto de los ajustes nuevos.
VTUBER_AI_TEMAS: Final = ("amadeus", "none")
VISION_DETECTORES: Final = ("auto", "yunet", "haar")
# Procesos que autopausan la cámara continua y vetan las travesuras (nombre sin ".exe", minúsculas).
VISION_APPS_PAUSA_DEFECTO: Final[tuple[str, ...]] = ("zoom", "teams", "ms-teams", "discord", "obs64", "obs")
_RE_NOMBRE_PROCESO: Final = re.compile(r"^[a-z0-9_.+\-]{1,64}$")
_PANTALLA_EXCLUIR_MAX: Final = 32          # patrones extra como mucho
_PANTALLA_EXCLUIR_LARGO: Final = 60        # caracteres por patrón extra

# Campos que nunca se muestran ni se registran.
_CAMPOS_SECRETOS: Final = (
    "gemini_api_key",
    "elevenlabs_api_key",
    "telegram_bot_token",
    "telegram_chat_id",
    "brave_api_key",
    "nexus_colab_token",
)


def _ruta_token_ctl_defecto() -> Path:
    """Ruta por defecto del token del socket de control (``~/.config/rams_biocore/ctl_token``)."""
    try:
        return Path.home() / ".config" / "rams_biocore" / "ctl_token"
    except (RuntimeError, KeyError, OSError) as exc:  # sin HOME determinable
        log.debug("No se pudo determinar el directorio personal (%s); uso base_dir", type(exc).__name__)
        return BASE_DIR_DEFECTO / ".config" / "rams_biocore" / "ctl_token"


def _valor_entorno(obtener: Callable[[str], Any], var: str) -> str | None:
    """Valor útil de una variable (regla única de ausencia) a partir de un ``get``; nunca lanza."""
    try:
        valor = obtener(var)
    except (TypeError, AttributeError, KeyError) as exc:
        log.debug("Entorno no legible para %s (%s)", var, type(exc).__name__)
        return None
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.startswith("#") or "\x00" in texto:
        return None
    return texto


def _ruta_token_escritorio(obtener: Callable[[str], Any], data_dir: Path, *,
                           plataforma: str | None = None) -> Path:
    """Ruta por defecto del token de ``/escritorio`` (§C2.1).

    - Windows: ``%LOCALAPPDATA%\\rams\\escritorio.json``.
    - Linux y demás: ``$XDG_RUNTIME_DIR/rams/escritorio.json``.
    - Sin ``LOCALAPPDATA``/``XDG_RUNTIME_DIR`` (o con un valor relativo): ``data_dir/escritorio.json``
      (Amadeus Desktop entonces solo conecta si ``RAMS_ESCRITORIO_TOKEN_FILE`` está definida, §C2.7.6).

    ``obtener`` es el ``get`` del entorno que se está leyendo (con ``entorno`` explícito, el de
    ese mapeo: los tests nunca tocan el ``%LOCALAPPDATA%`` real).
    """
    sistema = sys.platform if plataforma is None else plataforma
    var = "LOCALAPPDATA" if sistema == "win32" else "XDG_RUNTIME_DIR"
    base = _valor_entorno(obtener, var)
    if base is not None:
        raiz = Path(base)
        if raiz.anchor:  # mismo criterio que _Lector.expandir_ruta: nunca relativa a os.getcwd()
            return raiz / "rams" / "escritorio.json"
        log.debug("%s es una ruta relativa; el token de escritorio va a data_dir", var)
    return data_dir / "escritorio.json"


def _ruta_token_escritorio_defecto() -> Path:
    """Defecto de ``Settings.escritorio_token_file`` cuando se construye ``Settings`` a mano."""
    return _ruta_token_escritorio(os.environ.get, BASE_DIR_DEFECTO / "data")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Configuración inmutable y *hashable* del sistema (§4.1.1).

    Se construye con :func:`cargar_settings`. Los secretos llevan ``repr=False`` para que
    ``repr(settings)`` nunca los muestre. Para cambiar valores (tests, ``--offline``) se usa
    :meth:`con`, que devuelve una copia.
    """

    base_dir: Path = BASE_DIR_DEFECTO
    data_dir: Path = BASE_DIR_DEFECTO / "data"
    memoria_dir: Path = BASE_DIR_DEFECTO / "data" / "john"
    db_path: Path = BASE_DIR_DEFECTO / "data" / "john" / "rams_memory.db"
    perfil_path: Path = BASE_DIR_DEFECTO / "data" / "john" / "perfil_conductual.json"
    persona_dir: Path = BASE_DIR_DEFECTO / "persona"
    log_dir: Path = BASE_DIR_DEFECTO / "data" / "logs"
    nombre_ia: str = "Amadeus"
    nombre_usuario: str | None = None
    rango_lab: str = "Lab Mem 001"
    zona_horaria: str = "America/Bogota"
    offline: bool = False
    log_nivel: str = "INFO"
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_models: tuple[str, ...] = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
    gemini_thinking_level: str = "low"
    gemini_max_output_tokens: int = 1024
    gemini_timeout_s: float = 30.0
    gemini_max_llamadas_dia: int = 300
    compresion_llm: bool = False
    elevenlabs_api_key: str | None = field(default=None, repr=False)
    elevenlabs_voice_id: str = "p7sm2zw5Us66lZr2brDW"
    elevenlabs_model: str = "eleven_v4_turbo"
    elevenlabs_sample_rate: int = 24000
    edge_tts_voz: str = "es-CO-SalomeNeural"
    voz_habilitada: bool = True
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)
    telegram_polling: bool = True
    brave_api_key: str | None = field(default=None, repr=False)
    web_region: str = "co-es"
    vts_habilitado: bool = True
    vts_url: str = "ws://localhost:8001"
    vts_plugin_nombre: str = "RAMS BioCore"
    vts_plugin_desarrollador: str = "Sencans"
    # Avatar 3D VRM en VTuber AI Studio (HTTP local; lobes/vtuber_ai_bridge.py).
    vtuber_ai_habilitado: bool = True
    vtuber_ai_url: str = "http://127.0.0.1:3939"
    vtuber_ai_tts: bool = True          # sin voz propia, habla la voz del navegador
    vtuber_ai_lang: str = "es-MX"
    vtuber_ai_voz: str = "female"        # voz del navegador: nombre(s) "Sabina,Helena" o "female"
    # Autonomía corporal del avatar 3D (lobes/corteza_motora.py): el conectoma decide cuándo y cómo
    # se mueve, gesticula y cambia de cara, sin LLM.
    avatar_autonomo: bool = True
    avatar_actividad: float = 1.0        # multiplicador de la frecuencia de conductas (0 = quieta)
    avatar_caminar: bool = True          # puede desplazarse por el escenario
    avatar_camara: bool = False          # puede cambiar el encuadre de la cámara
    camara_habilitada: bool = False
    camara_indice: int = 0
    camara_intervalo_min_s: float = 90.0
    camara_max_dia: int = 40
    hypr_modo_dispatch: str = "auto"
    idle_flag_path: Path | None = None
    idle_timeout_s: float = 180.0
    ausencia_min: float = 10.0
    apps_permitidas_items: tuple[tuple[str, str], ...] = APPS_DEFECTO
    nexus_colab_token: str | None = field(default=None, repr=False)
    nexus_colab_host: str = "127.0.0.1"
    nexus_colab_puerto: int = 3737
    nexus_workspace: Path | None = None
    mcp_memoria: bool = False
    ctl_habilitado: bool = True
    ctl_puerto: int = 8766
    ctl_token_file: Path = field(default_factory=_ruta_token_ctl_defecto)
    hud_habilitado: bool = True
    hud_puerto: int = 8765
    hud_abrir: bool = False
    conectoma_fuente: str = "reducido"
    conectoma_semilla: int | None = None
    conectoma_escala_tiempo: float = 1.0
    flywire_dir: Path = BASE_DIR_DEFECTO / "data" / "flywire"
    perfil_en_prompt: bool = True
    max_proactivos_dia: int = 3
    max_interrupciones_hora: int = 4
    silencio_inicio: str = "23:00"
    silencio_fin: str = "07:00"
    # CONTRATO de cierre: quién da la cara en Telegram cuando no estás (§10.19, niveles).
    # "hibrido" (defecto, fiel a S;G0): Amadeus llama y Megumin insiste; "megumin": Megumin
    # entrega todos los mensajes; "amadeus": Megumin nunca aparece.
    mensajero: str = "hibrido"
    # --- v0.2 Amadeus Companion (docs/COMPANION.md §C2.1) ---
    # Amadeus Desktop / escritorio: endpoint /escritorio del HUD y efectores de escritorio.
    escritorio_habilitado: bool = True
    escritorio_token_file: Path = field(default_factory=_ruta_token_escritorio_defecto)  # {url, token, pid}, 0o600
    escritorio_voz: bool = False         # True = las respuestas al chat siguen la regla de voz de la v0.1
    hud_chat: bool = True                # False = el HUD no recibe el token de /escritorio
    # Control del PC y travesuras (interruptor maestro del cursor real: OFF por defecto).
    travesuras_habilitadas: bool = False
    travesura_amplitud_max_px: int = 40  # 5..80
    destinos_items: tuple[tuple[str, str], ...] = ()   # (sinónimo, clave de DESTINOS_BASE); se SUMAN
    vtuber_ai_tema: str = "amadeus"      # "amadeus" | "none" (con "none" el puente nunca envía theme)
    # Visión continua (opt-in; además exige camara_habilitada) y pantalla.
    vision_continua: bool = False
    vision_fps: float = 3.0              # 1..5
    vision_detector: str = "auto"        # "auto" | "yunet" | "haar"
    vision_modelo: Path | None = None    # YuNet; None = data_dir/models/face_detection_yunet*.onnx
    vision_hfov: float = 70.0            # grados, 30..120
    vision_espejo: bool = False          # la webcam entrega la imagen en espejo
    vision_apps_pausa: tuple[str, ...] = VISION_APPS_PAUSA_DEFECTO
    pantalla_modo: str = "off"           # ∈ MODOS_PANTALLA ("pixeles" es un permiso aparte)
    pantalla_fps: float = 1.0            # 0,2..2
    pantalla_excluir: tuple[str, ...] = ()   # patrones EXTRA; PATRON_TITULO_SENSIBLE se aplica siempre
    pantalla_excluir_avatar: bool = True     # enmascara la caja del avatar (escritorio.caja)

    # --- Capacidades (§4.1.2) ---

    @property
    def tiene_gemini(self) -> bool:
        """Hay clave de Gemini y no se está en modo offline."""
        return bool(self.gemini_api_key) and not self.offline

    @property
    def tiene_elevenlabs(self) -> bool:
        """Hay clave de ElevenLabs, no es offline y la voz está habilitada."""
        return bool(self.elevenlabs_api_key) and not self.offline and self.voz_habilitada

    @property
    def tiene_edge_tts(self) -> bool:
        """edge-tts es utilizable por configuración (que el paquete exista lo comprueba riko)."""
        return not self.offline and self.voz_habilitada

    @property
    def tiene_telegram(self) -> bool:
        """Hay token y chat_id de Telegram y no es offline."""
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id) and not self.offline

    @property
    def tiene_brave(self) -> bool:
        """Hay clave de Brave Search y no es offline."""
        return bool(self.brave_api_key) and not self.offline

    @property
    def tiene_web(self) -> bool:
        """La búsqueda web es posible (DuckDuckGo no necesita clave)."""
        return not self.offline

    @property
    def apps_permitidas(self) -> Mapping[str, str]:
        """Lista permitida de apps (alias → comando), de solo lectura."""
        return types.MappingProxyType(dict(self.apps_permitidas_items))

    @property
    def trato_usuario(self) -> str:
        """Cómo llama Amadeus al usuario: su nombre de pila o, si no hay, su rango."""
        return self.nombre_usuario if self.nombre_usuario else self.rango_lab

    # --- Capacidades v0.2 (§C2.1): NINGUNA depende de offline (todo es local) ---

    @property
    def tiene_escritorio(self) -> bool:
        """Endpoint ``/escritorio`` y efectores de escritorio activos (no depende de ``offline``)."""
        return bool(self.escritorio_habilitado)

    @property
    def tiene_vision_continua(self) -> bool:
        """Visión continua pedida **y** cámara habilitada (opt-in de dos niveles; no depende de ``offline``)."""
        return bool(self.vision_continua) and bool(self.camara_habilitada)

    @property
    def destinos_permitidos(self) -> Mapping[str, str]:
        """Alias de destino → clave de ``DESTINOS_BASE``, de solo lectura.

        ``{clave: clave}`` para cada destino del catálogo más los sinónimos de ``RAMS_DESTINOS``
        (``destinos_items``). Un sinónimo nunca reemplaza a una clave del catálogo.
        """
        return _destinos_permitidos(self.destinos_items)

    @property
    def pantalla_excluir_re(self) -> re.Pattern[str]:
        """``PATRON_TITULO_SENSIBLE`` | patrones extra de ``pantalla_excluir`` (escapados).

        Se aplica con ``search`` al título en minúsculas y sin tildes (:func:`normalizar_titulo`);
        se compila sin distinguir mayúsculas como red de seguridad. Compilado una vez por
        conjunto de extras (caché).
        """
        return _compilar_excluir(self.pantalla_excluir)

    def secretos(self) -> tuple[str, ...]:
        """Valores no vacíos de las claves (Gemini, ElevenLabs, token y chat de Telegram, Brave)
        y del token de colaboración de Nexus. Se pasan a :class:`FiltroSecretos`."""
        valores = (getattr(self, nombre) for nombre in _CAMPOS_SECRETOS)
        return tuple(v for v in valores if v)

    def asegurar_directorios(self) -> None:
        """Crea ``data_dir``, ``memoria_dir``, ``log_dir`` y ``data_dir/models`` con permisos 0o700.

        También aplica ``chmod 0o700`` a los que ya existían. Los errores de E/S se registran
        y no se propagan.

        v0.2 (§C2.1): crea también el directorio padre de ``escritorio_token_file`` con 0o700.
        Un padre que ya existía solo recibe ``chmod`` si es el directorio propio ``rams`` (nunca
        se cambian los permisos de un directorio ajeno como ``/tmp``).
        """
        for ruta in (self.data_dir, self.memoria_dir, self.log_dir, self.data_dir / "models"):
            try:
                ruta.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("No se pudo crear el directorio %s: %s", ruta, exc)
                continue
            _chmod_seguro(ruta, 0o700)
        padre = Path(self.escritorio_token_file).parent
        try:
            existia = padre.is_dir()
            padre.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("No se pudo crear el directorio del token de escritorio %s: %s", padre, exc)
            return
        if not existia or padre.name == "rams":
            _chmod_seguro(padre, 0o700)

    def resumen_seguro(self) -> dict[str, object]:
        """Diccionario JSON-serializable de la configuración, sin secretos.

        Las claves aparecen como ``"configurada"`` o ``"ausente"``.
        """
        resumen: dict[str, object] = {}
        for campo in dataclasses.fields(self):
            valor = getattr(self, campo.name)
            if campo.name in _CAMPOS_SECRETOS:
                resumen[campo.name] = "configurada" if valor else "ausente"
            elif isinstance(valor, Path):
                resumen[campo.name] = str(valor)
            elif isinstance(valor, tuple):
                resumen[campo.name] = [list(x) if isinstance(x, tuple) else x for x in valor]
            else:
                resumen[campo.name] = valor
        for nombre in ("tiene_gemini", "tiene_elevenlabs", "tiene_edge_tts", "tiene_telegram",
                       "tiene_brave", "tiene_web", "tiene_escritorio", "tiene_vision_continua"):
            resumen[nombre] = getattr(self, nombre)
        return resumen

    def con(self, **cambios: Any) -> Settings:
        """Copia con cambios (``dataclasses.replace``), para tests y ``--offline``."""
        return dataclasses.replace(self, **cambios)


# ---------------------------------------------------------------------------
# Lectura de .env
# ---------------------------------------------------------------------------

_RE_LINEA_ENV: Final = re.compile(
    r"^\s*(?:export\s+)?(?P<clave>[A-Za-z_][A-Za-z0-9_.\-]*)\s*=(?P<resto>.*)$"
)
_RE_LINEA_SIN_IGUAL: Final = re.compile(
    r"^\s*(?:export\s+)?(?P<clave>[A-Za-z_][A-Za-z0-9_.\-]*)\s*(?:#.*)?$"
)
_RE_COMENTARIO_EN_LINEA: Final = re.compile(r"\s+#.*$")
_ESCAPES_DOBLES: Final = {
    "\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
}
_RE_ESCAPE_DOBLE: Final = re.compile(r"\\([\\'\"abfnrtv])")
_RE_ESCAPE_SIMPLE: Final = re.compile(r"\\([\\'])")


def _buscar_cierre(cuerpo: str, comilla: str) -> int | None:
    """Índice de la comilla de cierre no escapada en ``cuerpo`` (o ``None``)."""
    i = 0
    while i < len(cuerpo):
        caracter = cuerpo[i]
        if caracter == "\\":
            i += 2
            continue
        if caracter == comilla:
            return i
        i += 1
    return None


def _desescapar(valor: str, comilla: str) -> str:
    """Procesa los escapes como dotenv: completos con comillas dobles; ``\\'`` y ``\\\\`` con simples."""
    if comilla == '"':
        return _RE_ESCAPE_DOBLE.sub(lambda m: _ESCAPES_DOBLES[m.group(1)], valor)
    return _RE_ESCAPE_SIMPLE.sub(lambda m: m.group(1), valor)


def parsear_env_texto(texto: str) -> dict[str, str]:
    """Parser mínimo de ``.env`` que imita a python-dotenv (§4.1).

    - Ignora líneas vacías y las que empiezan por ``#``; acepta el prefijo ``export ``.
    - Valor entre comillas simples o dobles: se toma lo de dentro (puede ocupar varias
      líneas), sin procesar comentarios.
    - Valor sin comillas: ``strip`` y se elimina un comentario en línea ``\\s+#.*$``; si el
      resultado empieza por ``#``, es la cadena vacía (cubre ``CLAVE=   # comentario``).
    - Una clave sin ``=`` queda con valor vacío. No expande variables.

    Nunca lanza.
    """
    resultado: dict[str, str] = {}
    lineas = str(texto).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lineas):
        linea = lineas[i]
        i += 1
        limpia = linea.strip().lstrip("﻿")
        if not limpia or limpia.startswith("#"):
            continue
        coincidencia = _RE_LINEA_ENV.match(linea.lstrip("﻿"))
        if coincidencia is None:
            sin_igual = _RE_LINEA_SIN_IGUAL.match(linea.lstrip("﻿"))
            if sin_igual is not None:
                resultado[sin_igual.group("clave")] = ""
            else:
                log.debug("Línea %d del .env no reconocida; se ignora", i)
            continue
        clave = coincidencia.group("clave")
        resto = coincidencia.group("resto").lstrip()
        if resto[:1] in ("'", '"'):
            comilla = resto[0]
            cuerpo = resto[1:]
            fin = _buscar_cierre(cuerpo, comilla)
            while fin is None and i < len(lineas):
                cuerpo += "\n" + lineas[i]
                i += 1
                fin = _buscar_cierre(cuerpo, comilla)
            if fin is None:
                log.debug("Comillas sin cerrar en el .env (variable %s); se ignora", clave)
                continue
            resultado[clave] = _desescapar(cuerpo[:fin], comilla)
        else:
            valor = _RE_COMENTARIO_EN_LINEA.sub("", resto).strip()
            if valor.startswith("#"):
                valor = ""
            resultado[clave] = valor
    return resultado


def leer_env_archivo(ruta: str | Path) -> dict[str, str]:
    """Devuelve el contenido de un archivo ``.env`` como dict (§4.1.3).

    Usa ``dotenv.dotenv_values`` (sin interpolar) si python-dotenv está instalado y, si no,
    :func:`parsear_env_texto`. Archivo inexistente o ilegible ⇒ ``{}`` con ``log.debug``.
    Nunca lanza.
    """
    try:
        archivo = Path(ruta)
        if not archivo.is_file():
            log.debug("No existe el archivo de entorno %s", archivo)
            return {}
    except (TypeError, ValueError, OSError) as exc:
        log.debug("Ruta de .env no válida (%s)", type(exc).__name__)
        return {}
    if dotenv_values is not None:
        try:
            crudo = dotenv_values(archivo, interpolate=False, encoding="utf-8")
            return {
                str(k).lstrip("﻿"): ("" if v is None else str(v))
                for k, v in crudo.items()
                if k
            }
        except Exception as exc:  # python-dotenv puede lanzar errores variados al parsear
            log.debug("python-dotenv no pudo leer %s (%s); uso el parser mínimo",
                      archivo, type(exc).__name__)
    try:
        texto = archivo.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        log.debug("No se pudo leer %s (%s)", archivo, type(exc).__name__)
        return {}
    return parsear_env_texto(texto)


# ---------------------------------------------------------------------------
# Conversión de valores de entorno
# ---------------------------------------------------------------------------


class _Lector:
    """Lee y convierte variables de entorno aplicando la regla única de ausencia (§4.1.1).

    Cada conversión inválida devuelve el valor por defecto y registra
    ``log.warning("Valor inválido para %s; uso %r", var, defecto)`` sin el valor recibido.
    """

    def __init__(self, entorno: Mapping[str, str], base_dir: Path, home: Path) -> None:
        self.entorno = entorno
        self.base_dir = base_dir
        self.home = home

    # --- primitivas ---

    def crudo(self, var: str) -> str | None:
        """Valor útil de ``var`` o ``None`` si está ausente, vacío, en blanco o empieza por ``#``."""
        try:
            valor = self.entorno.get(var)
        except (TypeError, AttributeError) as exc:
            log.debug("Entorno no legible para %s (%s); uso el valor por defecto",
                      var, type(exc).__name__)
            return None
        if valor is None:
            return None
        texto = str(valor).strip()
        if not texto or texto.startswith("#"):
            return None
        return texto

    @staticmethod
    def invalido(var: str, defecto: Any) -> Any:
        """Registra el aviso de valor inválido (sin el valor) y devuelve el defecto."""
        log.warning("Valor inválido para %s; uso %r", var, defecto)
        return defecto

    def texto(self, var: str, defecto: str, *, patron: re.Pattern[str] | None = None,
              opciones: Iterable[str] | None = None, minusculas: bool = False,
              largo: tuple[int, int] = (1, 200)) -> str:
        """Cadena validada (sin caracteres de control; con patrón, opciones y largo opcionales)."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        if minusculas:
            valor = valor.lower()
        if _RE_CONTROL.search(valor) or not (largo[0] <= len(valor) <= largo[1]):
            return self.invalido(var, defecto)
        if patron is not None and not patron.match(valor):
            return self.invalido(var, defecto)
        if opciones is not None and valor not in tuple(opciones):
            return self.invalido(var, defecto)
        return valor

    def texto_opcional(self, var: str, *, largo: tuple[int, int] = (1, 60)) -> str | None:
        """Cadena opcional (``None`` si falta o es inválida)."""
        valor = self.crudo(var)
        if valor is None:
            return None
        if _RE_CONTROL.search(valor) or not (largo[0] <= len(valor) <= largo[1]):
            return self.invalido(var, None)
        return valor

    def entero(self, var: str, defecto: int, *, minimo: int | None = None,
               maximo: int | None = None) -> int:
        """Entero en base 10 dentro de ``[minimo, maximo]``."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        try:
            numero = int(valor, 10)
        except ValueError:
            return self.invalido(var, defecto)
        if (minimo is not None and numero < minimo) or (maximo is not None and numero > maximo):
            return self.invalido(var, defecto)
        return numero

    def entero_opcional(self, var: str, *, minimo: int = 0, maximo: int = 2**63 - 1) -> int | None:
        """Entero opcional (``None`` si falta o es inválido)."""
        valor = self.crudo(var)
        if valor is None:
            return None
        try:
            numero = int(valor, 10)
        except ValueError:
            return self.invalido(var, None)
        if not (minimo <= numero <= maximo):
            return self.invalido(var, None)
        return numero

    def real(self, var: str, defecto: float, *, minimo: float | None = None,
             maximo: float | None = None, minimo_exclusivo: bool = False) -> float:
        """Float finito (acepta coma decimal: ``"90,5"``) dentro del rango indicado."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        normalizado = valor
        if "," in normalizado and "." not in normalizado and normalizado.count(",") == 1:
            normalizado = normalizado.replace(",", ".")
        try:
            numero = float(normalizado)
        except ValueError:
            return self.invalido(var, defecto)
        if not math.isfinite(numero):
            return self.invalido(var, defecto)
        if minimo is not None and (numero < minimo or (minimo_exclusivo and numero == minimo)):
            return self.invalido(var, defecto)
        if maximo is not None and numero > maximo:
            return self.invalido(var, defecto)
        return numero

    def booleano(self, var: str, defecto: bool) -> bool:
        """Booleano según los conjuntos de §4.1.1, sin distinguir mayúsculas."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        normalizado = unicodedata.normalize("NFC", valor).lower()
        if normalizado in _VERDADEROS:
            return True
        if normalizado in _FALSOS:
            return False
        return self.invalido(var, defecto)

    def expandir_ruta(self, valor: str) -> Path | None:
        """Expande ``${XDG_RUNTIME_DIR}``/``$XDG_RUNTIME_DIR`` y ``~``; resuelve relativas a ``base_dir``.

        Devuelve ``None`` si la ruta no es utilizable (por ejemplo, usa ``XDG_RUNTIME_DIR``
        y esa variable no está definida en el entorno leído).
        """
        if "\x00" in valor:
            return None
        if _RE_XDG.search(valor):
            xdg = self.crudo("XDG_RUNTIME_DIR")
            if xdg is None:
                return None
            valor = _RE_XDG.sub(lambda _m: xdg, valor)
        if valor == "~":
            ruta = self.home
        elif valor.startswith("~/") or valor.startswith("~\\"):
            ruta = self.home / valor[2:]
        else:
            ruta = Path(valor)
        if not ruta.anchor:  # relativa: nunca contra os.getcwd() (§3.4)
            ruta = self.base_dir / ruta
        return ruta

    def ruta(self, var: str, defecto: Path) -> Path:
        """Ruta (``Path``) con expansión; inválida ⇒ defecto."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        ruta = self.expandir_ruta(valor)
        if ruta is None:
            return self.invalido(var, defecto)
        return ruta

    def ruta_opcional(self, var: str, defecto: Path | None = None) -> Path | None:
        """Ruta opcional; ausente ⇒ ``defecto`` (que puede ser ``None``)."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        ruta = self.expandir_ruta(valor)
        if ruta is None:
            return self.invalido(var, defecto)
        return ruta

    def csv_modelos(self, var: str, defecto: tuple[str, ...]) -> tuple[str, ...]:
        """Lista CSV de nombres de modelo, sin duplicados ni vacíos."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        modelos: list[str] = []
        for parte in valor.split(","):
            nombre = parte.strip()
            if not nombre:
                continue
            if not _RE_MODELO.match(nombre):
                return self.invalido(var, defecto)
            if nombre not in modelos:
                modelos.append(nombre)
        if not modelos:
            return self.invalido(var, defecto)
        return tuple(modelos)

    def apps(self, var: str, defecto: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        """``alias=cmd,alias=cmd``; se descartan los pares que no cumplen los formatos."""
        valor = self.crudo(var)
        if valor is None:
            return defecto
        pares: dict[str, str] = {}
        descartados = 0
        for parte in valor.split(","):
            parte = parte.strip()
            if not parte:
                continue
            alias, igual, comando = parte.partition("=")
            alias, comando = alias.strip(), comando.strip()
            if not igual or not _RE_ALIAS_APP.match(alias) or not _RE_CMD_APP.match(comando):
                descartados += 1
                continue
            pares.pop(alias, None)  # el último gana, conservando el orden de aparición
            pares[alias] = comando
        if descartados:
            log.warning("Se descartaron %d entradas inválidas de %s", descartados, var)
        if not pares:
            return self.invalido(var, defecto)
        return tuple(pares.items())

    def destinos(self, var: str) -> tuple[tuple[str, str], ...]:
        """``sinonimo=clave,…`` (§C2.1): sinónimos que se SUMAN a ``DESTINOS_BASE``.

        El sinónimo se normaliza (minúsculas y sin tildes, como el texto que enruta
        ``core/intenciones.py``) y debe cumplir ``SAFE_ALIAS_RE``; la clave (en minúsculas) debe
        estar en ``DESTINOS_BASE``. Se descartan, con un único ``log.warning`` sin los valores, los
        pares mal formados, las claves desconocidas, los sinónimos inválidos y los que
        coinciden con otra clave del catálogo (un sinónimo nunca la reemplaza). Si un sinónimo se
        repite, gana el último. Ausente ⇒ ``()``.
        """
        valor = self.crudo(var)
        if valor is None:
            return ()
        pares: dict[str, str] = {}
        descartados = 0
        for parte in valor.split(","):
            parte = parte.strip()
            if not parte:
                continue
            sinonimo, igual, clave = parte.partition("=")
            sinonimo = normalizar_titulo(sinonimo).strip()
            clave = clave.strip().lower()
            if (not igual or not SAFE_ALIAS_RE.match(sinonimo) or clave not in DESTINOS_BASE
                    or (sinonimo in DESTINOS_BASE and sinonimo != clave)):
                descartados += 1
                continue
            if sinonimo == clave:  # redundante: la clave ya es su propio alias
                continue
            pares.pop(sinonimo, None)  # el último gana, conservando el orden de aparición
            pares[sinonimo] = clave
        if descartados:
            log.warning("Se descartaron %d entradas inválidas de %s", descartados, var)
        return tuple(pares.items())

    def csv_procesos(self, var: str, defecto: tuple[str, ...]) -> tuple[str, ...]:
        """CSV de nombres de proceso en minúsculas, sin ``.exe`` y sin duplicados.

        Las entradas que no cumplen ``^[a-z0-9_.+-]{1,64}$`` se descartan con un aviso; si no
        queda ninguna, valor inválido ⇒ ``defecto``.
        """
        valor = self.crudo(var)
        if valor is None:
            return defecto
        nombres: list[str] = []
        descartados = 0
        for parte in valor.split(","):
            nombre = parte.strip().lower()
            if not nombre:
                continue
            if nombre.endswith(".exe"):
                nombre = nombre[:-4]
            if not _RE_NOMBRE_PROCESO.match(nombre):
                descartados += 1
                continue
            if nombre not in nombres:
                nombres.append(nombre)
        if descartados:
            log.warning("Se descartaron %d entradas inválidas de %s", descartados, var)
        if not nombres:
            return self.invalido(var, defecto)
        return tuple(nombres)

    def csv_patrones(self, var: str) -> tuple[str, ...]:
        """CSV de patrones literales de título (minúsculas y sin tildes, ≤ 60 caracteres, ≤ 32).

        Se comparan con títulos normalizados igual (:func:`normalizar_titulo`). Las entradas con
        caracteres de control, demasiado largas o que sobran se descartan con un aviso.
        Ausente ⇒ ``()``.
        """
        valor = self.crudo(var)
        if valor is None:
            return ()
        patrones: list[str] = []
        descartados = 0
        for parte in valor.split(","):
            if _RE_CONTROL.search(parte):
                descartados += 1
                continue
            patron = normalizar_titulo(parte)
            if not patron:
                continue
            if len(patron) > _PANTALLA_EXCLUIR_LARGO or len(patrones) >= _PANTALLA_EXCLUIR_MAX:
                descartados += 1
                continue
            if patron not in patrones:
                patrones.append(patron)
        if descartados:
            log.warning("Se descartaron %d entradas inválidas de %s", descartados, var)
        return tuple(patrones)

    def secreto(self, var: str, patron: re.Pattern[str]) -> str | None:
        """Secreto validado por formato; inválido ⇒ ``None`` con aviso sin el valor."""
        valor = self.crudo(var)
        if valor is None:
            return None
        if not patron.match(valor):
            log.warning("Formato inválido para %s; se ignora", var)
            return None
        return valor


def _home_para(entorno: Mapping[str, str] | None) -> Path:
    """Directorio personal para expandir ``~``.

    # CONTRATO: §4.1.3 dice que ``~`` se expande con ``Path.home()`` y §12.2 que, con
    # ``entorno``, también ``HOME`` sale del mapeo. Se respetan ambos: ``HOME`` del mapeo
    # si está y no está vacía; si no, ``Path.home()``.
    """
    if entorno is not None:
        home = str(entorno.get("HOME") or "").strip()
        if home and not home.startswith("#"):
            return Path(home)
    try:
        return Path.home()
    except (RuntimeError, KeyError, OSError) as exc:
        log.debug("No se pudo determinar el directorio personal (%s); uso base_dir", type(exc).__name__)
        return BASE_DIR_DEFECTO


def _avisar_permisos_env(ruta: Path) -> None:
    """En POSIX, avisa si el ``.env`` es legible por el grupo u otros usuarios."""
    if os.name != "posix":
        return
    try:
        modo = ruta.stat().st_mode
    except OSError as exc:
        log.debug("No se pudo comprobar los permisos de %s: %s", ruta, exc)
        return
    if modo & 0o077:
        log.warning("El archivo .env es legible por otros usuarios; ejecuta: chmod 600 .env")


def cargar_settings(
    env_file: str | Path | None = None,
    *,
    offline: bool | None = None,
    entorno: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
) -> Settings:
    """Construye un :class:`Settings` validado (§4.1.3).

    - Sin ``entorno``: lee ``env_file`` o ``base_dir/.env`` (si existe) y lo combina con
      ``os.environ`` **sin sobrescribir** las variables ya presentes. No modifica
      ``os.environ``.
    - Con ``entorno``: todas las lecturas salen de ese mapeo (también ``XDG_RUNTIME_DIR`` y
      ``HOME``). Es lo que usan los tests.
    - ``offline`` distinto de ``None`` manda sobre ``RAMS_OFFLINE``.

    Nunca lanza excepciones por valores de entorno.
    """
    try:
        return _cargar_settings(env_file, offline=offline, entorno=entorno, base_dir=base_dir)
    except Exception:  # red de seguridad final: la configuración nunca tumba el arranque
        log.error("Error inesperado al cargar la configuración; uso los valores por defecto",
                  exc_info=True)
        base = Path(base_dir) if base_dir is not None else BASE_DIR_DEFECTO
        return _settings_por_defecto(base, offline=bool(offline))


def _settings_por_defecto(base: Path, *, offline: bool) -> Settings:
    """Settings con todos los valores por defecto relativos a ``base``."""
    data = base / "data"
    memoria = data / "john"
    return Settings(
        base_dir=base, data_dir=data, memoria_dir=memoria, db_path=memoria / "rams_memory.db",
        perfil_path=memoria / "perfil_conductual.json", persona_dir=base / "persona",
        log_dir=data / "logs", flywire_dir=data / "flywire", offline=offline,
        escritorio_token_file=_ruta_token_escritorio(os.environ.get, data),
    )


def _cargar_settings(
    env_file: str | Path | None,
    *,
    offline: bool | None,
    entorno: Mapping[str, str] | None,
    base_dir: Path | None,
) -> Settings:
    """Implementación de :func:`cargar_settings` (puede lanzar solo por errores de programación)."""
    if entorno is not None:
        env: Mapping[str, str] = entorno
    else:
        base_env = None
        if base_dir is None:
            crudo_base = str(os.environ.get("RAMS_BASE_DIR") or "").strip()
            if crudo_base and not crudo_base.startswith("#"):
                base_env = Path(crudo_base).expanduser()
        base_para_env = Path(base_dir) if base_dir is not None else (base_env or BASE_DIR_DEFECTO)
        ruta_env = Path(env_file) if env_file is not None else base_para_env / ".env"
        del_archivo: dict[str, str] = {}
        try:
            existe = ruta_env.is_file()
        except OSError as exc:  # p. ej. PermissionError en el directorio padre
            log.debug("No se pudo comprobar %s (%s)", ruta_env, type(exc).__name__)
            existe = False
        if existe:
            del_archivo = leer_env_archivo(ruta_env)
            _avisar_permisos_env(ruta_env)
            log.debug("Variables leídas de %s: %d", ruta_env, len(del_archivo))
        elif env_file is not None:
            log.warning("No existe el archivo de entorno %s; uso solo las variables del sistema",
                        ruta_env)
        # os.environ manda (equivalente a override=False); os.environ no se modifica.
        env = {**del_archivo, **dict(os.environ)}

    home = _home_para(entorno)
    lector_inicial = _Lector(env, BASE_DIR_DEFECTO, home)

    # --- Rutas ---
    if base_dir is not None:
        base = Path(base_dir).expanduser()
        if not base.anchor:
            base = BASE_DIR_DEFECTO / base
    else:
        base = lector_inicial.ruta("RAMS_BASE_DIR", BASE_DIR_DEFECTO)
    lector = _Lector(env, base, home)

    data_dir = lector.ruta("RAMS_DATA_DIR", base / "data")
    memoria_dir = data_dir / "john"
    db_path = lector.ruta("RAMS_DB_PATH", memoria_dir / "rams_memory.db")
    perfil_path = lector.ruta("RAMS_PERFIL_PATH", memoria_dir / "perfil_conductual.json")
    persona_dir = lector.ruta("RAMS_PERSONA_DIR", base / "persona")
    log_dir = data_dir / "logs"
    flywire_dir = lector.ruta("RAMS_FLYWIRE_DIR", data_dir / "flywire")

    xdg = lector.crudo("XDG_RUNTIME_DIR")
    idle_defecto = Path(xdg) / "rams_idle" if xdg else None
    idle_flag_path = lector.ruta_opcional("RAMS_IDLE_FLAG", idle_defecto)

    ctl_defecto = home / ".config" / "rams_biocore" / "ctl_token"
    ctl_token_file = lector.ruta("RAMS_CTL_TOKEN_FILE", ctl_defecto)
    nexus_workspace = lector.ruta_opcional("NEXUS_WORKSPACE", None)

    # v0.2 (§C2.1): token de /escritorio (LOCALAPPDATA / XDG_RUNTIME_DIR salen del entorno leído).
    escritorio_token_file = lector.ruta("RAMS_ESCRITORIO_TOKEN_FILE",
                                        _ruta_token_escritorio(lector.crudo, data_dir))
    vision_modelo = lector.ruta_opcional("RAMS_VISION_MODELO", None)

    # --- Nivel de log ---
    nivel_crudo = lector.crudo("RAMS_LOG_LEVEL")
    log_nivel = "INFO"
    if nivel_crudo is not None:
        canonico = _ALIAS_NIVEL.get(nivel_crudo.upper())
        log_nivel = canonico if canonico else lector.invalido("RAMS_LOG_LEVEL", "INFO")

    # --- Telegram (CHAT_ID con alias TELEGRAM_CHAT_ID) ---
    var_chat = "CHAT_ID" if lector.crudo("CHAT_ID") is not None else "TELEGRAM_CHAT_ID"

    # --- Frecuencia de ElevenLabs ---
    sample_rate = 24000
    salida = lector.crudo("ELEVENLABS_OUTPUT")
    if salida is not None:
        coincidencia = _RE_SALIDA_11L.match(salida)
        if coincidencia and int(coincidencia.group(1)) in _FRECUENCIAS_11L:
            sample_rate = int(coincidencia.group(1))
        else:
            sample_rate = lector.invalido("ELEVENLABS_OUTPUT", 24000)

    # --- Horario silencioso ---
    silencio_inicio = _hhmm(lector, "RAMS_QUIET_START", "23:00")
    silencio_fin = _hhmm(lector, "RAMS_QUIET_END", "07:00")

    if lector.crudo("RAMS_CONNECTOME_PROCESO") is not None:
        log.info("RAMS_CONNECTOME_PROCESO se ignora en v0.1")

    modo_offline = bool(offline) if offline is not None else lector.booleano("RAMS_OFFLINE", False)

    return Settings(
        base_dir=base,
        data_dir=data_dir,
        memoria_dir=memoria_dir,
        db_path=db_path,
        perfil_path=perfil_path,
        persona_dir=persona_dir,
        log_dir=log_dir,
        nombre_ia=lector.texto("RAMS_AI_NAME", "Amadeus", largo=(1, 40)),
        nombre_usuario=lector.texto_opcional("RAMS_USER_NAME", largo=(1, 40)),
        rango_lab=lector.texto("RAMS_LAB_RANK", "Lab Mem 001", largo=(1, 40)),
        zona_horaria=lector.texto("RAMS_TZ", "America/Bogota", patron=_RE_ZONA, largo=(1, 64)),
        offline=modo_offline,
        log_nivel=log_nivel,
        gemini_api_key=lector.secreto("GEMINI_API_KEY", _RE_CLAVE),
        gemini_model=lector.texto("GEMINI_MODEL", "gemini-3.8-flash", patron=_RE_MODELO),
        gemini_fallback_models=lector.csv_modelos(
            "GEMINI_FALLBACK_MODELS", ("gemini-3.6-flash", "gemini-3.5-flash-lite")),
        gemini_thinking_level=lector.texto("GEMINI_THINKING_LEVEL", "low",
                                           opciones=_NIVELES_THINKING, minusculas=True),
        gemini_max_output_tokens=lector.entero("GEMINI_MAX_OUTPUT_TOKENS", 1024,
                                               minimo=1, maximo=1_000_000),
        gemini_timeout_s=lector.real("GEMINI_TIMEOUT_S", 30.0, minimo=0.0, maximo=600.0,
                                     minimo_exclusivo=True),
        gemini_max_llamadas_dia=lector.entero("GEMINI_MAX_CALLS_DAY", 300, minimo=0),
        compresion_llm=lector.booleano("RAMS_COMPRESION_LLM", False),
        elevenlabs_api_key=lector.secreto("ELEVENLABS_API_KEY", _RE_CLAVE),
        elevenlabs_voice_id=lector.texto("ELEVENLABS_VOICE_ID", "p7sm2zw5Us66lZr2brDW",
                                         patron=_RE_VOZ_ID),
        elevenlabs_model=lector.texto("ELEVENLABS_MODEL", "eleven_v4_turbo", patron=_RE_MODELO),
        elevenlabs_sample_rate=sample_rate,
        edge_tts_voz=lector.texto("EDGE_TTS_VOICE", "es-CO-SalomeNeural", patron=_RE_VOZ_EDGE),
        voz_habilitada=lector.booleano("RAMS_VOICE", True),
        telegram_bot_token=lector.secreto("TELEGRAM_BOT_TOKEN", _RE_TOKEN_TELEGRAM),
        telegram_chat_id=lector.secreto(var_chat, _RE_CHAT_ID),
        telegram_polling=lector.booleano("RAMS_TELEGRAM_POLL", True),
        brave_api_key=lector.secreto("BRAVE_API_KEY", _RE_CLAVE),
        web_region=lector.texto("RAMS_WEB_REGION", "co-es", patron=_RE_REGION_WEB,
                                minusculas=True),
        vts_habilitado=lector.booleano("VTS_ENABLED", True),
        vts_url=lector.texto("VTS_URL", "ws://localhost:8001", patron=_RE_WS_URL),
        vts_plugin_nombre=lector.texto("VTS_PLUGIN_NAME", "RAMS BioCore", largo=(3, 32)),
        vts_plugin_desarrollador=lector.texto("VTS_PLUGIN_DEVELOPER", "Sencans", largo=(3, 32)),
        vtuber_ai_habilitado=lector.booleano("VTUBER_AI_ENABLED", True),
        vtuber_ai_url=lector.texto("VTUBER_AI_URL", "http://127.0.0.1:3939", patron=_RE_URL_LOCAL),
        vtuber_ai_tts=lector.booleano("VTUBER_AI_TTS", True),
        vtuber_ai_lang=lector.texto("VTUBER_AI_LANG", "es-MX", patron=_RE_LANG_BCP47),
        vtuber_ai_voz=lector.texto("VTUBER_AI_VOICE", "female", patron=_RE_VOZ_NAVEGADOR),
        avatar_autonomo=lector.booleano("RAMS_AVATAR_AUTONOMO", True),
        avatar_actividad=lector.real("RAMS_AVATAR_ACTIVIDAD", 1.0, minimo=0.0, maximo=3.0),
        avatar_caminar=lector.booleano("RAMS_AVATAR_CAMINAR", True),
        avatar_camara=lector.booleano("RAMS_AVATAR_CAMARA", False),
        camara_habilitada=lector.booleano("RAMS_CAMERA", False),
        camara_indice=lector.entero("RAMS_CAMERA_INDEX", 0, minimo=0, maximo=63),
        camara_intervalo_min_s=lector.real("RAMS_CAMERA_MIN_INTERVAL_S", 90.0, minimo=0.0),
        camara_max_dia=lector.entero("RAMS_CAMERA_MAX_DIA", 40, minimo=0),
        hypr_modo_dispatch=lector.texto("HYPR_DISPATCH_MODE", "auto", opciones=_MODOS_HYPR,
                                        minusculas=True),
        idle_flag_path=idle_flag_path,
        idle_timeout_s=lector.real("RAMS_IDLE_TIMEOUT_S", 180.0, minimo=0.0,
                                   minimo_exclusivo=True),
        ausencia_min=lector.real("RAMS_AWAY_MINUTES", 10.0, minimo=0.0, minimo_exclusivo=True),
        apps_permitidas_items=lector.apps("RAMS_APPS", APPS_DEFECTO),
        nexus_colab_token=lector.secreto("NEXUS_COLLAB_TOKEN", _RE_CLAVE),
        nexus_colab_host=lector.texto("NEXUS_COLLAB_HOST", "127.0.0.1", patron=_RE_HOST),
        nexus_colab_puerto=lector.entero("NEXUS_COLLAB_PORT", 3737, minimo=1, maximo=65535),
        nexus_workspace=nexus_workspace,
        mcp_memoria=lector.booleano("RAMS_MCP_MEMORIA", False),
        ctl_habilitado=lector.booleano("RAMS_CTL_ENABLED", True),
        ctl_puerto=lector.entero("RAMS_CTL_PORT", 8766, minimo=0, maximo=65535),
        ctl_token_file=ctl_token_file,
        hud_habilitado=lector.booleano("RAMS_HUD", True),
        hud_puerto=lector.entero("RAMS_HUD_PORT", 8765, minimo=0, maximo=65535),
        hud_abrir=lector.booleano("RAMS_HUD_OPEN", False),
        conectoma_fuente=lector.texto("RAMS_CONNECTOME", "reducido", opciones=_FUENTES_CONECTOMA,
                                      minusculas=True),
        conectoma_semilla=lector.entero_opcional("RAMS_CONNECTOME_SEED", minimo=0,
                                                 maximo=2**63 - 1),
        conectoma_escala_tiempo=lector.real("RAMS_CONNECTOME_TIME_SCALE", 1.0, minimo=0.0,
                                            maximo=10_000.0, minimo_exclusivo=True),
        flywire_dir=flywire_dir,
        perfil_en_prompt=lector.booleano("RAMS_PERFIL_EN_PROMPT", True),
        max_proactivos_dia=lector.entero("RAMS_MAX_PROACTIVE", 3, minimo=0),
        max_interrupciones_hora=lector.entero("RAMS_MAX_INTERRUPCIONES_HORA", 4, minimo=0),
        silencio_inicio=silencio_inicio,
        silencio_fin=silencio_fin,
        mensajero=_mensajero(lector),
        # --- v0.2 Amadeus Companion (§C2.1) ---
        escritorio_habilitado=lector.booleano("RAMS_ESCRITORIO", True),
        escritorio_token_file=escritorio_token_file,
        escritorio_voz=lector.booleano("RAMS_ESCRITORIO_VOZ", False),
        hud_chat=lector.booleano("RAMS_HUD_CHAT", True),
        travesuras_habilitadas=lector.booleano("RAMS_TRAVESURAS", False),
        travesura_amplitud_max_px=lector.entero("RAMS_TRAVESURA_AMPLITUD", 40, minimo=5, maximo=80),
        destinos_items=lector.destinos("RAMS_DESTINOS"),
        vtuber_ai_tema=lector.texto("VTUBER_AI_TEMA", "amadeus", opciones=VTUBER_AI_TEMAS,
                                    minusculas=True),
        vision_continua=lector.booleano("RAMS_VISION_CONTINUA", False),
        vision_fps=lector.real("RAMS_VISION_FPS", 3.0, minimo=1.0, maximo=5.0),
        vision_detector=lector.texto("RAMS_VISION_DETECTOR", "auto", opciones=VISION_DETECTORES,
                                     minusculas=True),
        vision_modelo=vision_modelo,
        vision_hfov=lector.real("RAMS_VISION_HFOV", 70.0, minimo=30.0, maximo=120.0),
        vision_espejo=lector.booleano("RAMS_VISION_ESPEJO", False),
        vision_apps_pausa=lector.csv_procesos("RAMS_VISION_APPS_PAUSA", VISION_APPS_PAUSA_DEFECTO),
        pantalla_modo=lector.texto("RAMS_PANTALLA_MODO", "off", opciones=MODOS_PANTALLA,
                                   minusculas=True),
        pantalla_fps=lector.real("RAMS_PANTALLA_FPS", 1.0, minimo=0.2, maximo=2.0),
        pantalla_excluir=lector.csv_patrones("RAMS_PANTALLA_EXCLUIR"),
        pantalla_excluir_avatar=lector.booleano("RAMS_PANTALLA_EXCLUIR_AVATAR", True),
    )


def _mensajero(lector: _Lector) -> str:
    """``RAMS_MENSAJERO`` (``hibrido`` | ``megumin`` | ``amadeus``); acepta también «híbrido»."""
    valor = lector.crudo("RAMS_MENSAJERO")
    if valor is None:
        return "hibrido"
    canonico = valor.strip().lower().replace("í", "i")
    return canonico if canonico in MENSAJEROS else lector.invalido("RAMS_MENSAJERO", "hibrido")


def _hhmm(lector: _Lector, var: str, defecto: str) -> str:
    """Hora ``"HH:MM"`` normalizada con ceros a la izquierda."""
    valor = lector.crudo(var)
    if valor is None:
        return defecto
    coincidencia = _RE_HHMM.match(valor)
    if not coincidencia:
        return lector.invalido(var, defecto)
    horas, minutos = int(coincidencia.group(1)), int(coincidencia.group(2))
    if horas > 23 or minutos > 59:
        return lector.invalido(var, defecto)
    return f"{horas:02d}:{minutos:02d}"


# ---------------------------------------------------------------------------
# Redacción de secretos
# ---------------------------------------------------------------------------

_RE_BOT_TOKEN: Final = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
_RE_CLAVE_GOOGLE: Final = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
_RE_CLAVE_ELEVEN: Final = re.compile(r"sk_[0-9a-fA-F]{32,}")
# CONTRATO: regla 5 de §4.1.3 ampliada (superconjunto estricto): si el valor empieza por un
# esquema de autenticación ("Authorization: Bearer xyz"), se oculta también la credencial que
# lo sigue. Con la regex literal, "\S+" consumía solo "Bearer" y la regla 6 ya no veía el token.
_RE_ASIGNACION: Final = re.compile(
    r"(?i)\b(xi-api-key|x-goog-api-key|x-subscription-token|authorization|api[_-]?key|token"
    r"|password|secret)(\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?\S+"
)
_RE_ARGUMENTO: Final = re.compile(
    r"(?i)(\bbearer|--api[_-]?key|--token|--password|--secret)(\s+|=)\S+"
)
_RE_PARAMETRO_URL: Final = re.compile(r"([?&](?:key|token|t)=)[^&\s]+")
_RE_ALTA_ENTROPIA: Final = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")


def redactar(texto: str, secretos: Iterable[str] = (), *, alta_entropia: bool = False) -> str:
    """Oculta secretos en ``texto`` aplicando, en orden, las reglas de §4.1.3.

    1. Cada secreto literal de 6 o más caracteres → ``«secreto»``.
    2. Token de bot de Telegram en URL → ``bot«token»``.
    3. Claves de Google (``AIza…``) → ``«clave-google»``.
    4. Claves de ElevenLabs (``sk_…``) → ``«clave-elevenlabs»``.
    5. ``clave: valor`` / ``clave=valor`` de cabeceras y parámetros sensibles → ``«secreto»``
       (si el valor empieza por ``Bearer``/``Basic``/``Token``, también la credencial siguiente).
    6. ``Bearer x``, ``--token x``, ``--api-key=x``… → ``«secreto»``.
    7. Parámetros de URL ``?key=``, ``&token=``, ``?t=`` → ``«secreto»``.
    8. Solo con ``alta_entropia``: secuencias de 24+ caracteres tipo clave → ``«oculto»``.

    Es idempotente y no lanza.
    """
    if not isinstance(texto, str):
        texto = str(texto)
    if not texto:
        return texto
    literales: list[str] = []
    try:
        for secreto in secretos or ():
            if secreto is None:
                continue
            valor = str(secreto)
            if len(valor) >= 6 and valor not in literales:
                literales.append(valor)
    except TypeError:  # ``secretos`` no iterable: se ignora la regla 1
        log.debug("redactar recibió secretos no iterables; se omite la regla literal")
    for valor in sorted(literales, key=len, reverse=True):
        texto = texto.replace(valor, "«secreto»")
    texto = _RE_BOT_TOKEN.sub("bot«token»", texto)
    texto = _RE_CLAVE_GOOGLE.sub("«clave-google»", texto)
    texto = _RE_CLAVE_ELEVEN.sub("«clave-elevenlabs»", texto)
    texto = _RE_ASIGNACION.sub(r"\1\2«secreto»", texto)
    texto = _RE_ARGUMENTO.sub(r"\1\2«secreto»", texto)
    texto = _RE_PARAMETRO_URL.sub(r"\1«secreto»", texto)
    if alta_entropia:
        texto = _RE_ALTA_ENTROPIA.sub("«oculto»", texto)
    return texto


# ---------------------------------------------------------------------------
# v0.2: textos y títulos sensibles (§C2.12 "Anti-contraseñas", §C2.13)
# ---------------------------------------------------------------------------

_RE_TITULO_SENSIBLE: Final = re.compile(PATRON_TITULO_SENSIBLE, re.IGNORECASE)
_RE_OTP: Final = re.compile(r"^\s*\d{4,8}\s*$")
# Rachas de 13+ dígitos con separadores sueltos (espacio o guion) y rachas puras de 13–19.
_RE_RACHA_DIGITOS: Final = re.compile(r"[0-9](?:[ \-]?[0-9]){12,}")
_RE_RACHA_PURA: Final = re.compile(r"(?<![0-9])[0-9]{13,19}(?![0-9])")
_RE_SEPARADOR_DIGITOS: Final = re.compile(r"[ \-]")
# Se aplica al texto en minúsculas y sin tildes: cubre "contraseña"/"contrasena", "código"/"codigo".
_RE_ASIGNA_CLAVE: Final = re.compile(r"(?:contrasena|clave|password|\bpin|codigo)\s*[:=]\s*\S+")
# Puntuación de borde que no cuenta como parte de una palabra ("¡Felicidades!" → "Felicidades").
_BORDE_PALABRA: Final = (string.punctuation.replace("#", "").replace("@", "")
                         + "¡¿«»“”‘’…–—·")
# Separadores habituales dentro de palabras normales (no cuentan como clase "símbolo").
_SEPARADORES_PALABRA: Final = frozenset("-.'’/:_")
_RE_URL_PALABRA: Final = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)
_RE_EMAIL_PALABRA: Final = re.compile(r"^[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+$")
_RE_ETIQUETA_SOCIAL: Final = re.compile(r"^[#@]\w+$")   # #hashtag o @mención


def normalizar_titulo(titulo: Any) -> str:
    """Texto en minúsculas, sin tildes (NFKD sin marcas) y con los espacios colapsados.

    Es la forma a la que se aplican ``PATRON_TITULO_SENSIBLE`` y ``Settings.pantalla_excluir_re``
    (``"Contraseña  - Bancolombia"`` → ``"contrasena - bancolombia"``). ``None`` ⇒ ``""``.
    """
    if titulo is None:
        return ""
    descompuesto = unicodedata.normalize("NFKD", str(titulo))
    sin_marcas = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return " ".join(sin_marcas.lower().split())


def titulo_sensible(titulo: Any) -> bool:
    """``True`` si el título (en minúsculas y sin tildes) casa con ``PATRON_TITULO_SENSIBLE``.

    Ej.: ``"Bancolombia - Sucursal Virtual"``, ``"Iniciar sesión"``, ``"Bitwarden"``. Nunca lanza.
    """
    try:
        return _RE_TITULO_SENSIBLE.search(normalizar_titulo(titulo)) is not None
    except Exception as exc:  # título exótico (str() roto): se trata como sensible
        log.debug("titulo_sensible: título no legible (%s); se trata como sensible", type(exc).__name__)
        return True


def _luhn(digitos: str) -> bool:
    """Suma de verificación de Luhn sobre una cadena de dígitos ASCII."""
    total = 0
    for indice, caracter in enumerate(reversed(digitos)):
        valor = ord(caracter) - 48
        if indice % 2 == 1:
            valor *= 2
            if valor > 9:
                valor -= 9
        total += valor
    return total % 10 == 0


def _hay_tarjeta(texto: str) -> bool:
    """13–19 dígitos (seguidos o con espacios/guiones sueltos) que pasan Luhn."""
    for coincidencia in _RE_RACHA_DIGITOS.finditer(texto):
        digitos = _RE_SEPARADOR_DIGITOS.sub("", coincidencia.group())
        if 13 <= len(digitos) <= 19 and _luhn(digitos):
            return True
    return any(_luhn(c.group()) for c in _RE_RACHA_PURA.finditer(texto))


def _parece_clave_compleja(palabra: str) -> bool:
    """Palabra de ≥ 12 caracteres con ≥ 3 clases (minúscula, mayúscula, dígito, símbolo).

    No cuentan como contraseña las URL, los correos, los #hashtags/@menciones ni las palabras
    normales con puntuación de borde o separadores (``"¡Felicidades!"``, ``"Bogotá-Medellín"``).
    """
    if len(palabra) < 12:
        return False
    limpia = palabra.strip(_BORDE_PALABRA)
    if not limpia:
        return False
    if (_RE_URL_PALABRA.match(limpia) or _RE_EMAIL_PALABRA.match(limpia)
            or _RE_ETIQUETA_SOCIAL.match(limpia)):
        return False
    clases = 0
    if any(c.islower() for c in limpia):
        clases += 1
    if any(c.isupper() for c in limpia):
        clases += 1
    if any(c.isdigit() for c in limpia):
        clases += 1
    if any(not c.isalnum() and not c.isspace() and c not in _SEPARADORES_PALABRA for c in limpia):
        clases += 1
    return clases >= 3


def parece_secreto(texto: Any, secretos: Iterable[str] = ()) -> bool:
    """``True`` si el texto parece un secreto (reglas "Texto" de §C2.12). Basta una:

    1. ``redactar(texto, secretos, alta_entropia=True) != texto`` (claves, tokens, asignaciones
       sensibles, secuencias de 24+ caracteres tipo clave, secretos literales).
    2. ``^\\s*\\d{4,8}\\s*$``: el texto entero es un código OTP o un PIN.
    3. 13–19 dígitos (seguidos o agrupados con espacios/guiones) que pasan Luhn (tarjeta).
    4. ``(contraseña|clave|password|pin|código)\\s*[:=]\\s*\\S+`` (sobre minúsculas sin tildes).
    5. Una palabra de ≥ 12 caracteres con ≥ 3 clases de caracteres (ver
       :func:`_parece_clave_compleja`).

    Función pura que comparten ``core/permisos.py`` y ``lobes/control_pc.py``. Nunca lanza: ante
    un error interno responde ``True`` (lo prudente es no escribir el texto).
    """
    try:
        cadena = "" if texto is None else str(texto)
        if not cadena.strip():
            return False
        if redactar(cadena, secretos, alta_entropia=True) != cadena:
            return True
        if _RE_OTP.match(cadena):
            return True
        if _hay_tarjeta(cadena):
            return True
        if _RE_ASIGNA_CLAVE.search(normalizar_titulo(cadena)):
            return True
        return any(_parece_clave_compleja(palabra) for palabra in cadena.split())
    except Exception as exc:  # contrato: nunca lanza; ante la duda, se trata como secreto
        log.debug("parece_secreto: texto no evaluable (%s); se trata como secreto", type(exc).__name__)
        return True


def _como_tupla(valor: Any) -> tuple[Any, ...]:
    """Tupla *hashable* de un campo de secuencia (tolera listas y una cadena suelta de ``con()``)."""
    if valor is None:
        return ()
    if isinstance(valor, str):
        return (valor,)
    try:
        return tuple(tuple(v) if isinstance(v, list) else v for v in valor)
    except TypeError as exc:
        log.debug("Secuencia de configuración no iterable (%s); se ignora", type(exc).__name__)
        return ()


@functools.lru_cache(maxsize=16)
def _compilar_excluir_cache(extras: tuple[Any, ...]) -> re.Pattern[str]:
    """``PATRON_TITULO_SENSIBLE`` | extras escapados (normalizados), sin distinguir mayúsculas."""
    partes = [PATRON_TITULO_SENSIBLE]
    for extra in extras:
        normalizado = normalizar_titulo(extra)
        if normalizado and re.escape(normalizado) not in partes:
            partes.append(re.escape(normalizado))
    return re.compile("|".join(partes), re.IGNORECASE)


def _compilar_excluir(extras: Any) -> re.Pattern[str]:
    """Patrón de títulos excluidos (ver :meth:`Settings.pantalla_excluir_re`); nunca lanza."""
    try:
        return _compilar_excluir_cache(_como_tupla(extras))
    except TypeError as exc:  # elementos no hashables: sin extras (el patrón base siempre se aplica)
        log.debug("pantalla_excluir no válido (%s); uso solo el patrón base", type(exc).__name__)
        return _compilar_excluir_cache(())


@functools.lru_cache(maxsize=16)
def _destinos_permitidos_cache(items: tuple[Any, ...]) -> Mapping[str, str]:
    """``{clave: clave}`` de ``DESTINOS_BASE`` ∪ ``{sinonimo: clave}`` válidos, de solo lectura."""
    destinos = {clave: clave for clave in DESTINOS_BASE}
    for par in items:
        try:
            sinonimo, clave = par
        except (TypeError, ValueError) as exc:
            log.debug("destinos_items: entrada no válida (%s); se ignora", type(exc).__name__)
            continue
        if (isinstance(sinonimo, str) and isinstance(clave, str) and clave in DESTINOS_BASE
                and SAFE_ALIAS_RE.match(sinonimo) and sinonimo not in DESTINOS_BASE):
            destinos[sinonimo] = clave
    return types.MappingProxyType(destinos)


def _destinos_permitidos(items: Any) -> Mapping[str, str]:
    """Alias de destino → clave (ver :attr:`Settings.destinos_permitidos`); nunca lanza."""
    try:
        return _destinos_permitidos_cache(_como_tupla(items))
    except TypeError as exc:  # elementos no hashables: solo el catálogo base
        log.debug("destinos_items no válido (%s); uso solo DESTINOS_BASE", type(exc).__name__)
        return _destinos_permitidos_cache(())


class FiltroSecretos(logging.Filter):
    """Filtro de logging que redacta secretos en mensaje, traza y ``stack_info`` (§4.1.3).

    Es idempotente: el mismo ``record`` puede pasar por varios handlers.
    """

    def __init__(self, secretos: Iterable[str] = ()) -> None:
        super().__init__()
        try:
            self.secretos: tuple[str, ...] = tuple(str(s) for s in (secretos or ()) if s)
        except TypeError as exc:  # se registra al construir, nunca dentro de filter (recursión)
            log.debug("FiltroSecretos recibió secretos no iterables (%s); solo se aplican las "
                      "reglas por patrón", type(exc).__name__)
            self.secretos = ()

    def filter(self, record: logging.LogRecord) -> bool:
        """Redacta el registro en el sitio y devuelve siempre ``True``."""
        try:
            mensaje = record.getMessage()
        except Exception as exc:  # argumentos que no casan con el formato
            mensaje = f"{record.msg!s} (argumentos de log inválidos: {type(exc).__name__})"
        record.msg = redactar(mensaje, self.secretos)
        record.args = None
        if record.exc_info:
            try:
                traza = logging.Formatter().formatException(record.exc_info)
            except Exception as exc:  # traza imposible de formatear
                traza = f"(traza no disponible: {type(exc).__name__})"
            record.exc_text = redactar(traza, self.secretos)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redactar(record.exc_text, self.secretos)
        if record.stack_info:
            record.stack_info = redactar(record.stack_info, self.secretos)
        return True


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_FORMATO_ARCHIVO: Final = "%(asctime)s %(levelname)-11s %(name)s: %(message)s"
_FORMATO_CONSOLA: Final = "%(levelname)s %(name)s: %(message)s"
_LOGGERS_SILENCIADOS: Final = ("httpx", "httpcore", "websockets", "asyncio")


def nombre_nivel(nivel: int) -> str:
    """Nombre del nivel de log en español (``AVISO``, ``DEPURACIÓN``…)."""
    if nivel in NOMBRES_NIVEL:
        return NOMBRES_NIVEL[nivel]
    nombre = logging.getLevelName(nivel)
    return nombre if isinstance(nombre, str) else str(nivel)


def _chmod_seguro(ruta: str | Path, modo: int) -> None:
    """``chmod`` en POSIX dentro de ``try``; en Windows no hace nada (§3.4)."""
    if os.name != "posix":
        return
    try:
        os.chmod(ruta, modo)
    except OSError as exc:
        log.debug("No se pudo aplicar chmod %o a %s: %s", modo, ruta, exc)


class _ArchivoRotativoPrivado(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` que deja el log (y cada archivo tras rotar) en 0o600."""

    def _open(self) -> Any:  # noqa: D401 - firma heredada
        """Crea el archivo con 0o600 si no existe y lo abre."""
        if os.name == "posix":
            try:
                descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.close(descriptor)
            except OSError as exc:
                log.debug("No se pudo crear %s con permisos 0o600: %s", self.baseFilename, exc)
        flujo = super()._open()
        _chmod_seguro(self.baseFilename, 0o600)
        return flujo

    def doRollover(self) -> None:  # noqa: N802 - nombre heredado de logging
        """Rota y reaplica 0o600 al archivo nuevo y a las copias."""
        super().doRollover()
        _chmod_seguro(self.baseFilename, 0o600)
        for indice in range(1, max(0, int(self.backupCount)) + 1):
            copia = f"{self.baseFilename}.{indice}"
            if os.path.exists(copia):
                _chmod_seguro(copia, 0o600)


def _quitar_handlers_rams(raiz: logging.Logger, *, incluir_bus: bool = False) -> None:
    """Quita (y cierra) los handlers propios marcados con ``_rams = True``.

    Los handlers del bus (``_rams_tipo == "bus"``, de ``core.bus.instalar_manejador_log``)
    solo se quitan con ``incluir_bus=True``: reconfigurar el logging no debe desconectar el HUD.
    """
    for handler in list(raiz.handlers):
        if not getattr(handler, "_rams", False):
            continue
        if getattr(handler, "_rams_tipo", "") == "bus" and not incluir_bus:
            continue
        raiz.removeHandler(handler)
        try:
            handler.close()
        except Exception as exc:  # cerrar un handler roto no debe impedir reconfigurar
            log.debug("Error al cerrar un handler de log: %s", exc)


def _nivel_valido(nivel: str | int, defecto: int) -> int:
    """Convierte un nivel (nombre o número) en entero; inválido ⇒ ``defecto``."""
    if isinstance(nivel, int):
        return nivel
    canonico = _ALIAS_NIVEL.get(str(nivel).strip().upper())
    if canonico is None:
        return defecto
    return int(logging.getLevelName(canonico))


def configurar_logging(settings: Settings, *, consola_nivel: str = "WARNING",
                       archivo: bool = True) -> None:
    """Configura el logging del sistema (§4.1.3). Es idempotente.

    - Handlers en el logger raíz (marcados ``_rams = True``): archivo rotativo
      ``log_dir/rams.log`` (1 MB × 3, 0o600, nivel ``settings.log_nivel``) y consola en
      ``stderr`` (nivel ``consola_nivel``), ambos con :class:`FiltroSecretos`.
    - Niveles: raíz ``WARNING``; ``rams`` ``settings.log_nivel``; ``httpx``, ``httpcore``,
      ``websockets`` y ``asyncio`` en ``WARNING`` (httpx registra la URL de Telegram, con token).
    - Nombres de nivel en español.
    """
    for nivel, nombre in NOMBRES_NIVEL.items():
        logging.addLevelName(nivel, nombre)

    raiz = logging.getLogger()
    _quitar_handlers_rams(raiz)
    raiz.setLevel(logging.WARNING)

    nivel_rams = _nivel_valido(settings.log_nivel, logging.INFO)
    logging.getLogger("rams").setLevel(nivel_rams)
    for nombre in _LOGGERS_SILENCIADOS:
        logging.getLogger(nombre).setLevel(logging.WARNING)

    secretos = settings.secretos()

    consola = logging.StreamHandler(sys.stderr)
    consola.setLevel(_nivel_valido(consola_nivel, logging.WARNING))
    consola.setFormatter(logging.Formatter(_FORMATO_CONSOLA))
    consola.addFilter(FiltroSecretos(secretos))
    consola._rams = True  # type: ignore[attr-defined]
    consola._rams_tipo = "consola"  # type: ignore[attr-defined]
    raiz.addHandler(consola)

    if archivo:
        ruta_log = settings.log_dir / "rams.log"
        try:
            settings.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            manejador = _ArchivoRotativoPrivado(ruta_log, maxBytes=1_000_000, backupCount=3,
                                                encoding="utf-8")
        except OSError as exc:
            log.warning("No se pudo abrir el log en %s (%s); solo consola", ruta_log, exc)
        else:
            manejador.setLevel(nivel_rams)
            manejador.setFormatter(logging.Formatter(_FORMATO_ARCHIVO))
            manejador.addFilter(FiltroSecretos(secretos))
            manejador._rams = True  # type: ignore[attr-defined]
            manejador._rams_tipo = "archivo"  # type: ignore[attr-defined]
            raiz.addHandler(manejador)
    log.debug("Logging configurado (nivel %s, consola %s, archivo %s)",
              settings.log_nivel, consola_nivel, "sí" if archivo else "no")


# ---------------------------------------------------------------------------
# Hora local
# ---------------------------------------------------------------------------

_ZONAS: dict[str, tzinfo] = {}
_ZONAS_LOCK = threading.Lock()
_ZONA_RESPALDO: Final = timezone(timedelta(hours=-5), "COT")


def zona_local(settings: Settings) -> tzinfo:
    """Zona horaria del usuario (``ZoneInfo(settings.zona_horaria)``).

    Sin base de zonas (Windows sin ``tzdata``) cae a UTC−5 ("COT") y lo registra una sola vez.
    """
    nombre = settings.zona_horaria
    zona = _ZONAS.get(nombre)
    if zona is not None:
        return zona
    with _ZONAS_LOCK:
        zona = _ZONAS.get(nombre)
        if zona is not None:
            return zona
        try:
            zona = ZoneInfo(nombre)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            log.warning("Zona horaria %s no disponible (%s); uso UTC−5 (COT). Instala tzdata.",
                        nombre, type(exc).__name__)
            zona = _ZONA_RESPALDO
        _ZONAS[nombre] = zona
        return zona


def ahora_local(settings: Settings) -> datetime:
    """Fecha y hora actuales, *aware*, en la zona del usuario."""
    return datetime.now(zona_local(settings))


def _minutos(hhmm: str, defecto: int) -> int:
    """Minutos desde medianoche de ``"HH:MM"``; inválido ⇒ ``defecto``."""
    coincidencia = _RE_HHMM.match(str(hhmm).strip())
    if not coincidencia:
        return defecto
    horas, minutos = int(coincidencia.group(1)), int(coincidencia.group(2))
    if horas > 23 or minutos > 59:
        return defecto
    return horas * 60 + minutos


def en_horario_silencioso(settings: Settings, momento: datetime | None = None) -> bool:
    """``True`` si ``momento`` (o ahora) cae en el horario silencioso (solo afecta a Telegram).

    El rango puede cruzar la medianoche: con ``23:00–07:00``, las 23:30 y las 06:59 están
    dentro y las 07:00 está fuera. Un ``momento`` *naive* se interpreta como hora local; uno
    *aware* se convierte a la zona del usuario. Inicio igual a fin ⇒ sin horario silencioso.
    """
    zona = zona_local(settings)
    if momento is None:
        local = datetime.now(zona)
    elif momento.tzinfo is None or momento.tzinfo.utcoffset(momento) is None:
        local = momento
    else:
        local = momento.astimezone(zona)
    inicio = _minutos(settings.silencio_inicio, 23 * 60)
    fin = _minutos(settings.silencio_fin, 7 * 60)
    actual = local.hour * 60 + local.minute
    if inicio == fin:
        return False
    if inicio < fin:
        return inicio <= actual < fin
    return actual >= inicio or actual < fin
