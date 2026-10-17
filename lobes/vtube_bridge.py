"""Puente WebSocket con VTube Studio: el cuerpo visible de Amadeus.

Qué hace (§10.20, revisión 2):
- Se conecta a la API pública de VTube Studio (``ws://localhost:8001`` por defecto) y se
  autentica con el token de plugin guardado en ``data/vts_token.txt`` (permisos 600). Si no hay
  token o VTS lo rechaza, lo pide con ``AuthenticationTokenRequest``: el usuario acepta el
  plugin «RAMS BioCore» en la ventana de VTS (120 s de plazo).
- Hace inventario de hotkeys y archivos de expresión del modelo cargado y resuelve el
  vocabulario cerrado ``[EXP]``/``[MOTION]`` (§4.3.1) con ``data/vts_mapeo.json`` o con los
  candidatos de ``MAPEO_EXP_DEFECTO``/``MAPEO_MOTION_DEFECTO``.
- Expresiones **por archivo** ``.exp3.json`` (``ExpressionActivationRequest``): antes de
  cambiar lee el estado real (``ExpressionStateRequest``) y apaga las expresiones del plugin
  que sigan activas. **Nunca** desactiva una expresión re-disparando su hotkey *toggle*, así
  no se desincroniza aunque el usuario toque VTS a mano.
- Movimientos con ``HotkeyTriggerRequest`` (mínimo 250 ms entre disparos: límite de VTS).
- Boca (lip-sync) a 30 Hz con ``InjectParameterDataRequest`` mientras la voz la alimente.
- Expresión de reposo según el estado homeostático, como mucho cada 15 s.
- ``bucle_reconexion``: reconexión con *backoff* exponencial y *keepalive* ``APIStateRequest``
  cada 30 s (no se confía en los pings de WebSocket: ``ping_interval=None``); si cambia el
  modelo (``modelID``) refresca el inventario.

Dependencias internas: ``core.config``, ``core.bus``, ``core.contracts``, ``core.etiquetas``.
Dependencia opcional: ``websockets`` ≥ 14 (API ``websockets.asyncio``).

Degradación: sin ``websockets`` → ``OFF ("websockets no instalado")``; ``VTS_ENABLED=0`` →
``OFF ("deshabilitado")``; VTS cerrado → ``SIN CONEXIÓN`` y reintentos en segundo plano.
Todos los métodos públicos son *no-op* si ``en_linea`` es ``False`` y nunca propagan
excepciones del periférico al orquestador. En Arch Linux, VTube Studio corre bajo Proton.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import time
import unicodedata
import urllib.parse
import uuid
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, NoReturn

from core.bus import BusEventos
from core.config import Settings
from core.contracts import (
    EXP_VALIDOS,
    MOTION_VALIDOS,
    DiagnosticoModulo,
    EstadoModulo,
    Etiquetas,
    HomeostaticState,
)
from core.etiquetas import normalizar_valor, sugerir_etiquetas

try:  # opcional: sin websockets el puente queda en OFF
    from websockets.asyncio.client import connect as _ws_connect
    from websockets.exceptions import ConnectionClosed, WebSocketException
except ImportError:  # sin websockets (o versión < 13 sin la API asyncio): VTube Studio desactivado
    _ws_connect = None

    class WebSocketException(Exception):  # type: ignore[no-redef]
        """Sustituto local cuando ``websockets`` no está instalado (nunca se lanza)."""

    class ConnectionClosed(WebSocketException):  # type: ignore[no-redef]
        """Sustituto local cuando ``websockets`` no está instalado (nunca se lanza)."""


__all__ = [
    "MAPEO_EXP_DEFECTO", "MAPEO_MOTION_DEFECTO", "VTSError", "VTubeBridge", "WEBSOCKETS_DISPONIBLE",
]

log = logging.getLogger("rams.vts")

#: Flag de capacidad: ``True`` si se pudo importar ``websockets.asyncio``.
WEBSOCKETS_DISPONIBLE: bool = _ws_connect is not None

# ---------------------------------------------------------------------------
# Constantes del protocolo y del comportamiento (§10.20)
# ---------------------------------------------------------------------------

API_NOMBRE: Final = "VTubeStudioPublicAPI"
API_VERSION: Final = "1.0"

TIMEOUT_APERTURA_S: Final = 3.0          # open_timeout de la conexión WebSocket
TIMEOUT_CIERRE_S: Final = 2.0            # close_timeout (cierre ordenado)
TIMEOUT_PETICION_S: Final = 5.0          # espera de la respuesta con el mismo requestID
TIMEOUT_TOKEN_S: Final = 120.0           # el usuario tiene que aceptar el plugin en VTS
TIMEOUT_ETIQUETAS_S: Final = 2.5         # presupuesto de aplicar_etiquetas (no retrasar la voz)
INTERVALO_KEEPALIVE_S: Final = 30.0      # APIStateRequest como ping
BLOQUEO_DENEGADO_S: Final = 600.0        # tras el error 50 no se reintenta la autenticación en 10 min
INTERVALO_ESTADO_S: Final = 15.0         # mínimo entre expresiones de reposo (aplicar_estado)
MIN_ENTRE_HOTKEYS_S: Final = 0.25        # VTS: 5 frames por hotkey y cola de 32
FADE_EXPRESION_S: Final = 0.25           # fadeTime de ExpressionActivationRequest
HZ_BOCA: Final = 30.0                    # reenvío del parámetro de boca (VTS lo suelta tras 1 s)
VENTANA_BOCA_S: Final = 0.5              # la boca se inyecta mientras haya boca() más reciente que esto
MAX_ERRORES_BOCA: Final = 10             # errores seguidos que pausan la inyección de la boca
TIMEOUTS_PARA_DESCONECTAR: Final = 3     # timeouts seguidos que dan la conexión por perdida
PARAMETRO_BOCA_DEFECTO: Final = "MouthOpen"
SUFIJO_EXPRESION: Final = ".exp3.json"

# Valores de ErrorID.cs (repositorio DenchiSoft/VTubeStudio). Para la boca también se mira el
# texto del mensaje, por si los números cambiaran en una versión futura.
ERROR_REQUIERE_AUTENTICACION: Final = 8
ERROR_ACCESO_DENEGADO: Final = 50
ERROR_TOKEN_EN_CURSO: Final = 51
ERROR_PARAMETRO_NO_ENCONTRADO: Final = 453
ERROR_PARAMETRO_OCUPADO: Final = 454

DETALLE_DENEGADO: Final = "acceso denegado por el usuario"
DETALLE_SIN_RESPUESTA: Final = "VTube Studio no responde"
SEP_REINTENTO: Final = " · reintento"     # sufijo del detalle: "<motivo> · reintento en N s"

# Tipos de hotkey que se pueden disparar automáticamente como gesto (candidatos por defecto).
# Los demás (cambiar de modelo, mover, recargar, toggles de expresión…) solo si el usuario los
# nombra explícitamente en vts_mapeo.json.
_TIPOS_GESTO: Final = frozenset({"TriggerAnimation"})
_TIPO_TOGGLE: Final = "ToggleExpression"

_RE_TOKEN: Final = re.compile(r"^[\x21-\x7e]{1,512}$")
_RE_PARAMETRO: Final = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_RE_SEPARADOR_RUTA: Final = re.compile(r"[\\/]")

MAPEO_EXP_DEFECTO: Final[dict[str, list[str]]] = {
    "neutral": [], "smug": ["smug", "engreida", "presumida"], "sonrojo_tsundere": ["sonrojo", "blush", "tsundere"],
    "molesta": ["molesta", "angry", "enojo"], "pensativa": ["pensativa", "thinking", "think"],
    "sorprendida": ["sorprendida", "surprised", "shock"], "triste": ["triste", "sad"],
    "feliz": ["feliz", "happy", "smile"], "avergonzada": ["avergonzada", "embarrassed", "shy"],
    "seria": ["seria", "serious"], "curiosa": ["curiosa", "curious"], "aburrida": ["aburrida", "bored"],
    "somnolienta": ["somnolienta", "sleepy", "tired"],
}
MAPEO_MOTION_DEFECTO: Final[dict[str, list[str]]] = {
    "idle": [], "asentir": ["asentir", "nod"], "negar": ["negar", "shake"], "ladear_cabeza": ["ladear", "tilt"],
    "brazos_cruzados": ["brazos", "cross"], "mirar_a_otro_lado": ["mirar_lado", "look_away", "lookaway"],
    "facepalm": ["facepalm"], "senalar": ["senalar", "point"], "suspirar": ["suspirar", "sigh"],
    "bostezar": ["bostezar", "yawn"], "estirarse": ["estirarse", "stretch"],
}


class VTSError(RuntimeError):
    """Respuesta ``APIError`` de VTube Studio. Se captura siempre dentro de este módulo."""

    error_id: int

    def __init__(self, error_id: int, mensaje: str = "") -> None:
        try:
            codigo = int(error_id)
        except (TypeError, ValueError):
            codigo = -1
        self.error_id = codigo
        self.mensaje = _texto_corto(mensaje, 200)
        super().__init__(f"VTS APIError {codigo}: {self.mensaje}")


# ---------------------------------------------------------------------------
# Utilidades puras (sin E/S de red)
# ---------------------------------------------------------------------------


def _texto_corto(valor: Any, largo: int = 60) -> str:
    """Texto seguro para logs y detalles: sin caracteres de control y truncado."""
    texto = "" if valor is None else str(valor)
    texto = "".join(c for c in texto if unicodedata.category(c)[0] != "C").strip()
    return texto if len(texto) <= largo else texto[: largo - 1] + "…"


def _puerto_de(url: str) -> int | None:
    """Puerto de la URL de VTS (``None`` si no se puede saber)."""
    try:
        return urllib.parse.urlsplit(url).port
    except ValueError:
        return None


def _nombre_archivo(archivo: str) -> str:
    """Nombre base de un archivo de expresión sin carpeta ni sufijo ``.exp3.json``."""
    base = _RE_SEPARADOR_RUTA.split(archivo)[-1]
    if base.casefold().endswith(SUFIJO_EXPRESION):
        base = base[: -len(SUFIJO_EXPRESION)]
    return base


def _es_archivo_expresion(valor: Any) -> bool:
    return isinstance(valor, str) and valor.casefold().endswith(SUFIJO_EXPRESION)


def _opciones_conexion() -> dict[str, Any]:
    """Argumentos de ``websockets.asyncio.client.connect`` (§10.20 punto 2)."""
    opciones: dict[str, Any] = {
        "open_timeout": TIMEOUT_APERTURA_S,
        "ping_interval": None,            # el keepalive es APIStateRequest (no se confía en los pings)
        "close_timeout": TIMEOUT_CIERRE_S,
        "compression": None,              # mensajes JSON pequeños a 30 Hz: sin deflate
        "max_size": 4 * 1024 * 1024,      # inventarios de modelos con muchas hotkeys
    }
    try:
        parametros = inspect.signature(_ws_connect).parameters if _ws_connect is not None else {}
    except (TypeError, ValueError):
        parametros = {}
    if "proxy" in parametros:            # websockets ≥ 15: VTS es local, nunca a través de un proxy
        opciones["proxy"] = None
    # Se pasa a loop.create_connection: con "localhost" prueba IPv4 sin esperar a que falle ::1.
    opciones["happy_eyeballs_delay"] = 0.25
    return opciones


@dataclass(frozen=True)
class _Hotkey:
    """Hotkey del modelo actual (``HotkeysInCurrentModelRequest``)."""

    nombre: str
    tipo: str
    id: str
    archivo: str

    @property
    def nombre_normalizado(self) -> str:
        return normalizar_valor(self.nombre)


def _hotkeys_validas(lista: Any) -> list[_Hotkey]:
    """Convierte ``availableHotkeys`` en ``_Hotkey`` y descarta entradas mal formadas."""
    hotkeys: list[_Hotkey] = []
    if not isinstance(lista, list):
        return hotkeys
    for item in lista:
        if not isinstance(item, Mapping):
            continue
        id_hotkey = item.get("hotkeyID")
        nombre = item.get("name")
        if not isinstance(id_hotkey, str) or not id_hotkey:
            continue
        archivo = item.get("file")
        hotkeys.append(_Hotkey(
            nombre=nombre if isinstance(nombre, str) else "",
            tipo=str(item.get("type") or ""),
            id=id_hotkey,
            archivo=archivo if isinstance(archivo, str) else "",
        ))
    return hotkeys


def _archivos_expresion(lista: Any) -> list[str]:
    """Archivos ``.exp3.json`` de una respuesta de ``ExpressionStateRequest``."""
    archivos: list[str] = []
    if not isinstance(lista, list):
        return archivos
    for item in lista:
        if isinstance(item, Mapping) and _es_archivo_expresion(item.get("file")):
            archivos.append(item["file"])
    return archivos


def _huella_hotkeys(hotkeys: list[_Hotkey]) -> tuple[tuple[str, str, str, str], ...]:
    """Huella del inventario para detectar cambios de hotkeys dentro del mismo modelo."""
    return tuple(sorted((h.id, h.nombre, h.tipo, h.archivo) for h in hotkeys))


@dataclass(frozen=True)
class _Mapeo:
    """Contenido validado de ``data/vts_mapeo.json``."""

    exp: dict[str, tuple[str, ...]] = field(default_factory=dict)
    motion: dict[str, tuple[str, ...]] = field(default_factory=dict)
    parametro_boca: str | None = None
    avisos: tuple[str, ...] = ()


def _valores_mapeo(valor: Any) -> tuple[str, ...]:
    """Un valor del mapeo puede ser una cadena o una lista de cadenas (en orden de preferencia)."""
    if isinstance(valor, str):
        valores = [valor]
    elif isinstance(valor, list):
        valores = [v for v in valor if isinstance(v, str)]
    else:
        return ()
    return tuple(v.strip() for v in valores if v.strip() and len(v.strip()) <= 128)


def _seccion_mapeo(crudo: Any, validos: tuple[str, ...], nombre: str,
                   avisos: list[str]) -> dict[str, tuple[str, ...]]:
    """Valida la sección ``exp`` o ``motion`` del mapeo."""
    resultado: dict[str, tuple[str, ...]] = {}
    if crudo is None:
        return resultado
    if not isinstance(crudo, Mapping):
        avisos.append(f"vts_mapeo.json: «{nombre}» debe ser un objeto; se ignora")
        return resultado
    for clave, valor in crudo.items():
        id_vocab = normalizar_valor(str(clave))
        if id_vocab not in validos:
            avisos.append(f"vts_mapeo.json: {nombre.upper()} «{_texto_corto(clave, 40)}» no existe "
                          "en el vocabulario; se ignora")
            continue
        valores = _valores_mapeo(valor)
        if not valores:
            avisos.append(f"vts_mapeo.json: valor inválido para {nombre.upper()} «{id_vocab}»; se ignora")
            continue
        resultado[id_vocab] = valores
    return resultado


def _leer_mapeo(ruta: Path) -> _Mapeo:
    """Lee y valida ``vts_mapeo.json``. Nunca lanza: si falta o es inválido, mapeo vacío + aviso."""
    try:
        if not ruta.is_file():
            return _Mapeo()
        crudo = json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return _Mapeo(avisos=(f"no pude leer {ruta.name} ({exc.__class__.__name__}); "
                              "uso los candidatos por defecto",))
    if not isinstance(crudo, Mapping):
        return _Mapeo(avisos=(f"{ruta.name} debe contener un objeto JSON; uso los candidatos por defecto",))
    avisos: list[str] = []
    exp = _seccion_mapeo(crudo.get("exp"), EXP_VALIDOS, "exp", avisos)
    motion = _seccion_mapeo(crudo.get("motion"), MOTION_VALIDOS, "motion", avisos)
    parametro = crudo.get("parametro_boca")
    if parametro is not None and not (isinstance(parametro, str) and _RE_PARAMETRO.match(parametro)):
        avisos.append(f"vts_mapeo.json: parametro_boca inválido; uso «{PARAMETRO_BOCA_DEFECTO}»")
        parametro = None
    return _Mapeo(exp=exp, motion=motion, parametro_boca=parametro, avisos=tuple(avisos))


@dataclass
class _Resolucion:
    """Vocabulario resuelto contra el modelo actual."""

    exp_archivo: dict[str, str] = field(default_factory=dict)   # EXP → archivo .exp3.json
    exp_hotkey: dict[str, str] = field(default_factory=dict)    # EXP → hotkeyID (no toggle), sin archivo
    motion_hotkey: dict[str, str] = field(default_factory=dict) # MOTION → hotkeyID
    parametro_boca: str = PARAMETRO_BOCA_DEFECTO
    avisos: list[str] = field(default_factory=list)


def _buscar_archivo(nombre: str, archivos: list[str]) -> str | None:
    """Archivo del modelo cuyo nombre base coincide (sin distinguir mayúsculas) con ``nombre``."""
    buscado = _RE_SEPARADOR_RUTA.split(nombre)[-1].casefold()
    for archivo in archivos:
        if _RE_SEPARADOR_RUTA.split(archivo)[-1].casefold() == buscado:
            return archivo
    return None


def _buscar_hotkey(valor: str, hotkeys: list[_Hotkey]) -> _Hotkey | None:
    """Hotkey por ID exacto o por nombre sin distinguir mayúsculas (como hace VTS)."""
    for hotkey in hotkeys:
        if hotkey.id == valor:
            return hotkey
    plegado = valor.casefold()
    for hotkey in hotkeys:
        if hotkey.nombre.casefold() == plegado:
            return hotkey
    return None


def _resolver_exp_explicito(valor: str, hotkeys: list[_Hotkey],
                            archivos: list[str]) -> tuple[str, str] | None:
    """Resuelve un valor del mapeo de EXP: archivo ``.exp3.json`` o nombre/ID de hotkey."""
    if _es_archivo_expresion(valor):
        encontrado = _buscar_archivo(valor, archivos)
        if encontrado is not None:
            return ("archivo", encontrado)
        if not archivos and not hotkeys:      # inventario no disponible: se confía en el usuario
            return ("archivo", valor)
        return None
    hotkey = _buscar_hotkey(valor, hotkeys)
    if hotkey is None:
        return None
    if _es_archivo_expresion(hotkey.archivo):
        return ("archivo", hotkey.archivo)
    if hotkey.tipo == _TIPO_TOGGLE:           # toggle sin archivo: no se puede gobernar por estado
        return None
    return ("hotkey", hotkey.id)


def _resolver_exp_candidatos(exp: str, hotkeys: list[_Hotkey],
                             archivos: list[str]) -> tuple[str, str] | None:
    """Candidatos por defecto: toggle con archivo > archivo de expresión > animación."""
    candidatos = [normalizar_valor(c) for c in MAPEO_EXP_DEFECTO.get(exp, ())]
    for candidato in candidatos:
        for hotkey in hotkeys:
            if (hotkey.tipo == _TIPO_TOGGLE and _es_archivo_expresion(hotkey.archivo)
                    and candidato in hotkey.nombre_normalizado):
                return ("archivo", hotkey.archivo)
    for candidato in candidatos:
        for archivo in archivos:
            if candidato in normalizar_valor(_nombre_archivo(archivo)):
                return ("archivo", archivo)
    for candidato in candidatos:
        for hotkey in hotkeys:
            if hotkey.tipo in _TIPOS_GESTO and candidato in hotkey.nombre_normalizado:
                return ("hotkey", hotkey.id)
    return None


def _resolver_motion_explicito(valor: str, hotkeys: list[_Hotkey]) -> str | None:
    """Resuelve un valor del mapeo de MOTION (nombre o ID de hotkey; nunca un toggle de expresión)."""
    hotkey = _buscar_hotkey(valor, hotkeys)
    if hotkey is None or hotkey.tipo == _TIPO_TOGGLE:
        return None
    return hotkey.id


def _resolver_motion_candidatos(motion: str, hotkeys: list[_Hotkey]) -> str | None:
    """Candidatos por defecto para MOTION: solo hotkeys de tipo ``TriggerAnimation``."""
    for candidato in (normalizar_valor(c) for c in MAPEO_MOTION_DEFECTO.get(motion, ())):
        for hotkey in hotkeys:
            if hotkey.tipo in _TIPOS_GESTO and candidato in hotkey.nombre_normalizado:
                return hotkey.id
    return None


def _resolver(hotkeys: list[_Hotkey], archivos_estado: list[str], mapeo: _Mapeo) -> _Resolucion:
    """Resuelve todo el vocabulario contra el inventario (§10.20 punto 4).

    ``avisos`` solo recoge las entradas del mapeo que no sirven en este modelo; los errores de
    formato del archivo ya vienen en ``mapeo.avisos``.
    """
    resolucion = _Resolucion()
    archivos: list[str] = []
    vistos: set[str] = set()
    for archivo in [*archivos_estado, *(h.archivo for h in hotkeys if _es_archivo_expresion(h.archivo))]:
        if archivo.casefold() not in vistos:
            vistos.add(archivo.casefold())
            archivos.append(archivo)

    for exp in EXP_VALIDOS:
        if exp == "neutral":
            continue
        resultado: tuple[str, str] | None = None
        explicitos = mapeo.exp.get(exp, ())
        for valor in explicitos:
            resultado = _resolver_exp_explicito(valor, hotkeys, archivos)
            if resultado is not None:
                break
        if explicitos and resultado is None:
            resolucion.avisos.append(
                f"vts_mapeo.json: «{_texto_corto(', '.join(explicitos), 80)}» no sirve en el modelo "
                f"actual para EXP «{exp}»; uso los candidatos por defecto")
        if resultado is None:
            resultado = _resolver_exp_candidatos(exp, hotkeys, archivos)
        if resultado is not None:
            tipo, valor = resultado
            (resolucion.exp_archivo if tipo == "archivo" else resolucion.exp_hotkey)[exp] = valor

    for motion in MOTION_VALIDOS:
        if motion == "idle":
            continue
        id_hotkey: str | None = None
        explicitos = mapeo.motion.get(motion, ())
        for valor in explicitos:
            id_hotkey = _resolver_motion_explicito(valor, hotkeys)
            if id_hotkey is not None:
                break
        if explicitos and id_hotkey is None:
            resolucion.avisos.append(
                f"vts_mapeo.json: «{_texto_corto(', '.join(explicitos), 80)}» no sirve en el modelo "
                f"actual para MOTION «{motion}»; uso los candidatos por defecto")
        if id_hotkey is None:
            id_hotkey = _resolver_motion_candidatos(motion, hotkeys)
        if id_hotkey is not None:
            resolucion.motion_hotkey[motion] = id_hotkey

    if mapeo.parametro_boca:
        resolucion.parametro_boca = mapeo.parametro_boca
    return resolucion


# ---------------------------------------------------------------------------
# E/S de archivos de estado (se ejecutan con asyncio.to_thread: operaciones < 1 s)
# ---------------------------------------------------------------------------


def _chmod_privado(ruta: Path) -> None:
    """``chmod 0o600`` (en Windows no tiene efecto; los fallos solo se registran)."""
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:
        log.debug("No se pudo aplicar chmod 600 a %s: %s", ruta.name, exc)


def _leer_token(ruta: Path) -> str | None:
    """Token de plugin guardado, o ``None`` si no hay uno válido. Nunca lanza ni registra el token."""
    try:
        if not ruta.is_file():
            return None
        texto = ruta.read_text(encoding="utf-8").strip()
        if os.name == "posix" and ruta.stat().st_mode & 0o077:
            log.info("El token de VTube Studio era legible por otros usuarios; aplico chmod 600")
            _chmod_privado(ruta)
    except (OSError, UnicodeDecodeError) as exc:
        log.info("No pude leer el token de VTube Studio (%s); pediré uno nuevo", exc.__class__.__name__)
        return None
    if not _RE_TOKEN.match(texto):
        log.info("El token guardado de VTube Studio tiene un formato inválido; pediré uno nuevo")
        return None
    return texto


def _escribir_token(ruta: Path, token: str) -> None:
    """Guarda el token de forma atómica y con permisos 600 desde su creación. Lanza ``OSError``."""
    ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
            archivo.write(token + "\n")
        os.replace(temporal, ruta)
    except OSError:
        try:
            os.unlink(temporal)
        except OSError as exc_borrado:
            log.debug("No se pudo borrar el temporal del token: %s", exc_borrado)
        raise
    _chmod_privado(ruta)


def _abortar_ws(ws: Any) -> None:
    """Corta la conexión sin esperas (para conexiones rotas o tareas canceladas)."""
    if ws is None:
        return
    transporte = getattr(ws, "transport", None)
    if transporte is None:
        return
    try:
        transporte.abort()
    except Exception as exc:  # transporte ya cerrado o de otro loop
        log.debug("No se pudo abortar el transporte de VTS: %s", exc)


async def _esperar_evento(evento: asyncio.Event, segundos: float) -> bool:
    """Espera a ``evento`` como mucho ``segundos``. Devuelve ``True`` si se activó."""
    if segundos <= 0:
        return evento.is_set()
    try:
        await asyncio.wait_for(evento.wait(), timeout=segundos)
    except TimeoutError:
        return False
    return True


# Errores esperables al hablar con VTS (se capturan siempre dentro del módulo).
_ERRORES_VTS: Final[tuple[type[BaseException], ...]] = (
    VTSError, WebSocketException, TimeoutError, OSError, ValueError,
)


# ---------------------------------------------------------------------------
# Puente
# ---------------------------------------------------------------------------


class VTubeBridge:
    """Puente con VTube Studio (implementa ``SalidaAvatar``, §4.3.7).

    ``en_linea`` es ``True`` solo con la conexión abierta, autenticada y con inventario. Todos
    los métodos públicos son *no-op* (y nunca lanzan) cuando es ``False``.
    """

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 ruta_token: Path | None = None, ruta_mapeo: Path | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.ruta_token = Path(ruta_token) if ruta_token is not None else settings.data_dir / "vts_token.txt"
        self.ruta_mapeo = Path(ruta_mapeo) if ruta_mapeo is not None else settings.data_dir / "vts_mapeo.json"
        self.en_linea: bool = False

        # Parámetros de tiempo (atributos para poder acortarlos en pruebas).
        self.timeout_peticion_s = TIMEOUT_PETICION_S
        self.timeout_token_s = TIMEOUT_TOKEN_S
        self.timeout_etiquetas_s = TIMEOUT_ETIQUETAS_S
        self.intervalo_keepalive_s = INTERVALO_KEEPALIVE_S
        self.intervalo_estado_s = INTERVALO_ESTADO_S
        self.bloqueo_denegado_s = BLOQUEO_DENEGADO_S

        puerto = _puerto_de(settings.vts_url)
        self._nombre_diag = f"Puente VTube Studio :{puerto}" if puerto else "Puente VTube Studio"
        if not WEBSOCKETS_DISPONIBLE:
            estado, detalle = EstadoModulo.OFF, "websockets no instalado"
        elif not settings.vts_habilitado:
            estado, detalle = EstadoModulo.OFF, "deshabilitado"
        else:
            estado, detalle = EstadoModulo.EN_ESPERA, "conectando en segundo plano"
        self._estado: EstadoModulo = estado
        self._detalle: str = detalle
        self._latencia_ms: int | None = None

        self._ws: Any = None
        self._cerrado = False
        self._loop_locks: asyncio.AbstractEventLoop | None = None
        self._lock_protocolo: asyncio.Lock | None = None
        self._lock_conexion: asyncio.Lock | None = None
        self._lock_expresion: asyncio.Lock | None = None
        self._lock_hotkey: asyncio.Lock | None = None
        self._timeouts_seguidos = 0
        # Hora del loop en que vence el plazo del primer envío sin ningún mensaje de VTS detrás.
        self._limite_silencio: float | None = None
        self._bloqueo_auth_hasta = 0.0
        self._fallos_conexion = 0

        # Inventario resuelto.
        self._modelo_id: str | None = None
        self._modelo_nombre: str = ""
        self._modelo_cargado = False
        self._huella: tuple[tuple[str, str, str, str], ...] = ()
        self._exp_archivo: dict[str, str] = {}
        self._exp_hotkey: dict[str, str] = {}
        self._motion_hotkey: dict[str, str] = {}
        self._gestionados_cf: frozenset[str] = frozenset()
        self._parametro_boca = PARAMETRO_BOCA_DEFECTO
        self._avisados: set[tuple[str, str]] = set()
        self._avisos_mapeo: set[str] = set()

        # Expresiones, movimientos y reposo.
        self._exp_actual: str | None = None
        self._ultimo_disparo = 0.0
        self._ultimo_estado_ts: float | None = None

        # Boca.
        self._boca_objetivo = 0.0
        self._boca_ultimo = float("-inf")
        self._tarea_boca: asyncio.Task[None] | None = None
        self._boca_desactivada = False
        self._boca_motivo: str | None = None
        self._boca_pausada = False              # demasiados errores seguidos: hasta cerrar_boca()

    # ------------------------------------------------------------------
    # Estado y diagnóstico
    # ------------------------------------------------------------------

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico del puente (clave ``"vts"``) para el arranque y el HUD."""
        return DiagnosticoModulo(clave="vts", nombre=self._nombre_diag, estado=self._estado,
                                 detalle=self._detalle, latencia_ms=self._latencia_ms)

    def _cambiar_estado(self, estado: EstadoModulo, detalle: str) -> None:
        """Actualiza el estado y publica ``sistema.modulo`` solo si cambió (§5)."""
        cambio = (estado, detalle) != (self._estado, self._detalle)
        self._estado, self._detalle = estado, detalle
        if cambio and self.bus is not None:
            try:
                self.bus.publicar("sistema.modulo", self.diagnostico().a_dict(), origen="vts")
            except Exception as exc:  # el bus nunca debería lanzar; se registra por si acaso
                log.debug("No se pudo publicar sistema.modulo: %s", exc)

    def _detalle_en_linea(self) -> str:
        """Detalle del estado OK: modelo y cuánto vocabulario quedó resuelto."""
        if not self._modelo_cargado:
            return "sin modelo cargado"
        total_exp = len(EXP_VALIDOS) - 1
        total_motion = len(MOTION_VALIDOS) - 1
        n_exp = len(self._exp_archivo) + len(self._exp_hotkey)
        n_motion = len(self._motion_hotkey)
        nombre = f"modelo «{self._modelo_nombre}»" if self._modelo_nombre else "modelo cargado"
        return f"{nombre} · EXP {n_exp}/{total_exp} · MOTION {n_motion}/{total_motion}"

    # ------------------------------------------------------------------
    # Protocolo
    # ------------------------------------------------------------------

    def _asegurar_locks(self) -> None:
        """Crea los ``asyncio.Lock`` del loop en ejecución (y los recrea si cambió de loop)."""
        loop = asyncio.get_running_loop()
        if self._loop_locks is not loop or self._lock_protocolo is None:
            self._loop_locks = loop
            self._lock_protocolo = asyncio.Lock()
            self._lock_conexion = asyncio.Lock()
            self._lock_expresion = asyncio.Lock()
            self._lock_hotkey = asyncio.Lock()

    async def _llamar(self, tipo: str, datos: Mapping[str, Any] | None = None, *,
                      timeout: float | None = None) -> dict[str, Any]:
        """Envía una petición y espera la respuesta con su ``requestID`` (§10.20 punto 1).

        Lanza ``VTSError`` ante ``APIError``, ``TimeoutError`` si no hay respuesta en el plazo
        y las excepciones de conexión de ``websockets``. Quien llama las captura.

        Detección de VTS colgado: ``_limite_silencio`` es el momento en que vence el plazo del
        primer envío que no tiene ningún mensaje de VTS detrás; cualquier mensaje lo borra. Si
        vence (aunque quien hizo esa petición la abandonara antes, como riko con sus
        presupuestos de 1-2 s), la conexión se da por perdida en ese momento o en la siguiente
        petición, sin esperar al *keepalive*. Si VTS sí envía mensajes pero no la respuesta,
        cuentan los ``TIMEOUTS_PARA_DESCONECTAR`` *timeouts* seguidos.
        """
        self._asegurar_locks()
        assert self._lock_protocolo is not None
        if self._ws is None:
            raise ConnectionError("VTube Studio no está conectado")
        plazo = self.timeout_peticion_s if timeout is None else float(timeout)
        id_peticion = uuid.uuid4().hex[:16]
        mensaje = json.dumps({
            "apiName": API_NOMBRE,
            "apiVersion": API_VERSION,
            "requestID": id_peticion,
            "messageType": tipo,
            "data": dict(datos or {}),
        }, ensure_ascii=False)
        loop = asyncio.get_running_loop()
        async with self._lock_protocolo:
            # Se lee dentro del lock: mientras se esperaba, otra petición pudo dar la conexión por perdida.
            ws = self._ws
            if ws is None:
                raise ConnectionError("VTube Studio no está conectado")
            ahora = loop.time()
            if self._limite_silencio is not None and ahora >= self._limite_silencio:
                # Una petición anterior (abandonada por quien la hizo) lleva un plazo entero sin que
                # VTS envíe nada: está colgado aunque el TCP siga abierto. Se falla sin esperar.
                self._vts_mudo(tipo)
            limite = ahora + plazo
            try:
                await asyncio.wait_for(ws.send(mensaje), timeout=max(0.05, plazo))
            except TimeoutError:
                # VTS no lee lo que se le envía (búfer lleno): conexión colgada.
                self._perder_conexion(DETALLE_SIN_RESPUESTA)
                raise TimeoutError(f"VTube Studio no aceptó {tipo} en {plazo:.0f} s") from None
            if self._limite_silencio is None:
                self._limite_silencio = limite
            while True:
                silencio = self._limite_silencio
                tope = limite if silencio is None else min(limite, silencio)
                try:
                    restante = tope - loop.time()
                    if restante <= 0:
                        raise TimeoutError
                    crudo = await asyncio.wait_for(ws.recv(), timeout=restante)
                except TimeoutError:
                    if self._limite_silencio is not None:
                        self._vts_mudo(tipo)       # ningún mensaje desde un envío durante un plazo entero
                    self._timeouts_seguidos += 1   # VTS envía cosas, pero no la respuesta a esta petición
                    raise TimeoutError(f"VTube Studio no respondió a {tipo} en {plazo:.0f} s") from None
                self._limite_silencio = None       # cualquier mensaje prueba que VTS sigue vivo
                respuesta = self._decodificar(crudo)
                if respuesta is None:
                    continue
                if respuesta.get("requestID") != id_peticion:
                    log.debug("Descarto un mensaje de VTube Studio que no es la respuesta esperada (%s)",
                              _texto_corto(respuesta.get("messageType"), 60))
                    continue
                break
        self._timeouts_seguidos = 0
        datos_respuesta = respuesta.get("data")
        if not isinstance(datos_respuesta, dict):
            datos_respuesta = {}
        if respuesta.get("messageType") == "APIError":
            raise VTSError(datos_respuesta.get("errorID", -1), datos_respuesta.get("message", ""))
        return datos_respuesta

    def _vts_mudo(self, tipo: str) -> NoReturn:
        """VTS lleva un plazo de petición entero sin enviar nada tras un envío: conexión perdida.

        Cubre el caso de VTS colgado con el TCP abierto aunque quien llama (riko, con
        presupuestos < 5 s) abandone cada petición antes de su *timeout*: la siguiente petición
        lo detecta en cuanto vence el plazo de la primera que quedó sin respuesta (§10.20 punto 1).
        """
        self._perder_conexion(DETALLE_SIN_RESPUESTA)
        raise TimeoutError(f"VTube Studio no responde ({tipo}): conexión dada por perdida")

    @staticmethod
    def _decodificar(crudo: Any) -> dict[str, Any] | None:
        """JSON de un mensaje de VTS, o ``None`` si no es un objeto válido."""
        try:
            if isinstance(crudo, (bytes, bytearray)):
                crudo = bytes(crudo).decode("utf-8")
            respuesta = json.loads(crudo)
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            log.debug("Mensaje de VTube Studio ilegible: %s", exc)
            return None
        return respuesta if isinstance(respuesta, dict) else None

    def _manejar_error(self, exc: BaseException, que: str) -> None:
        """Registra un error del periférico y decide si la conexión quedó inservible."""
        if not self.en_linea:
            # Otra petición ya dio la conexión por perdida (y publicó el motivo): no se pisa.
            log.debug("Error de VTube Studio en %s con la conexión ya perdida: %s", que, exc)
            return
        if isinstance(exc, VTSError):
            if exc.error_id == ERROR_REQUIERE_AUTENTICACION:
                self._perder_conexion("sesión no autenticada")
            else:
                log.info("VTube Studio rechazó %s: %s", que, exc)
            return
        if isinstance(exc, ConnectionClosed):
            self._perder_conexion("VTube Studio cerró la conexión")
            return
        if isinstance(exc, TimeoutError):
            if self._timeouts_seguidos >= TIMEOUTS_PARA_DESCONECTAR:
                self._perder_conexion(DETALLE_SIN_RESPUESTA)
            else:
                log.info("VTube Studio tardó demasiado en responder a %s", que)
            return
        if isinstance(exc, (OSError, WebSocketException)):
            log.info("Error de comunicación con VTube Studio en %s: %s", que, exc)
            self._perder_conexion("error de comunicación")
            return
        log.info("No se pudo completar %s en VTube Studio: %s", que, exc)

    def _perder_conexion(self, motivo: str) -> None:
        """Marca el puente fuera de línea, corta el socket y publica ``SIN CONEXIÓN``.

        Solo publica si estaba en línea: si ya se había perdido, el primer motivo se conserva
        (y durante ``conectar()`` el estado lo fija el propio arranque).
        """
        estaba = self.en_linea
        self.en_linea = False
        ws, self._ws = self._ws, None
        self._limite_silencio = None
        self._exp_actual = None
        _abortar_ws(ws)
        if estaba:
            log.info("Conexión con VTube Studio perdida (%s); reintentaré en segundo plano", motivo)
            if not self._cerrado:
                self._marcar_sin_conexion(motivo)

    def _marcar_sin_conexion(self, motivo: str) -> None:
        """``SIN CONEXIÓN`` con ``motivo``, sin republicar si ya está así (§5: "por cambio").

        ``bucle_reconexion`` añade " · reintento en N s" al detalle; un reintento que falla por el
        mismo motivo no lo quita, así no se publican dos eventos por ciclo con VTS cerrado.
        """
        if (self._estado == EstadoModulo.SIN_CONEXION
                and self._detalle.split(SEP_REINTENTO, 1)[0] == motivo):
            return
        self._cambiar_estado(EstadoModulo.SIN_CONEXION, motivo)

    def _detalle_con_reintento(self, segundos: float) -> str:
        """Detalle actual (sin un reintento anterior) más " · reintento en N s"."""
        motivo = self._detalle.split(SEP_REINTENTO, 1)[0]
        return f"{motivo}{SEP_REINTENTO} en {segundos:.0f} s"

    async def _cerrar_ws(self, ws: Any) -> None:
        """Cierre ordenado de la conexión con tope de tiempo (si no, se aborta)."""
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=TIMEOUT_CIERRE_S + 0.5)
        except asyncio.CancelledError:
            _abortar_ws(ws)
            raise
        except Exception as exc:  # timeout o conexión ya rota: se corta en seco
            log.debug("Cierre de la conexión con VTS sin handshake: %s", exc)
            _abortar_ws(ws)

    # ------------------------------------------------------------------
    # Conexión y autenticación
    # ------------------------------------------------------------------

    async def conectar(self) -> bool:
        """Conecta, autentica y hace inventario. Devuelve ``True`` si queda en línea. Nunca lanza."""
        if not WEBSOCKETS_DISPONIBLE:
            self._cambiar_estado(EstadoModulo.OFF, "websockets no instalado")
            return False
        if not self.settings.vts_habilitado:
            self._cambiar_estado(EstadoModulo.OFF, "deshabilitado")
            return False
        if self._cerrado:
            return False
        self._asegurar_locks()
        assert self._lock_conexion is not None
        async with self._lock_conexion:
            if self.en_linea and self._ws is not None:
                return True
            if self._bloqueo_auth_hasta > time.monotonic():
                self._marcar_sin_conexion(DETALLE_DENEGADO)
                log.debug("Autenticación con VTube Studio bloqueada tras la denegación del usuario")
                return False
            try:
                return await self._conectar_sin_lock()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # garantía "nunca lanza": fallo inesperado del cliente
                log.warning("Fallo inesperado al conectar con VTube Studio: %s", exc, exc_info=True)
                ws, self._ws = self._ws, None
                _abortar_ws(ws)
                self.en_linea = False
                self._marcar_sin_conexion("error interno del puente")
                return False

    async def _conectar_sin_lock(self) -> bool:
        """Cuerpo de :meth:`conectar` (con el lock de conexión tomado)."""
        assert _ws_connect is not None
        url = self.settings.vts_url
        inicio = time.monotonic()
        try:
            ws = await _ws_connect(url, **_opciones_conexion())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # OSError, TimeoutError, InvalidURI, InvalidHandshake…
            self._fallos_conexion += 1
            if self._fallos_conexion == 1:
                log.info("VTube Studio no disponible en %s (%s); sigo sin avatar",
                         url, _texto_corto(exc, 120) or exc.__class__.__name__)
            else:
                log.debug("VTube Studio sigue sin responder (%s)", exc.__class__.__name__)
            self._marcar_sin_conexion(DETALLE_SIN_RESPUESTA)
            return False

        self._ws = ws
        self._timeouts_seguidos = 0
        self._limite_silencio = None
        try:
            if not await self._autenticar():
                self._ws = None
                await self._cerrar_ws(ws)
                return False
            inicio_ping = time.monotonic()
            estado_api = await self._llamar("APIStateRequest")
            self._latencia_ms = int((time.monotonic() - inicio_ping) * 1000)
            await self._refrescar_inventario()
        except asyncio.CancelledError:
            self._ws = None
            _abortar_ws(ws)
            raise
        except _ERRORES_VTS as exc:
            self._ws = None
            await self._cerrar_ws(ws)
            if isinstance(exc, VTSError):
                log.warning("VTube Studio devolvió un error al iniciar la sesión: %s", exc)
                detalle = f"error de la API ({exc.error_id})"
            elif isinstance(exc, ConnectionClosed):
                log.info("VTube Studio cerró la conexión durante el arranque del puente")
                detalle = "VTube Studio cerró la conexión"
            else:
                log.info("VTube Studio no respondió durante el arranque del puente (%s)",
                         exc.__class__.__name__)
                detalle = DETALLE_SIN_RESPUESTA
            self._marcar_sin_conexion(detalle)
            return False

        self._fallos_conexion = 0
        self._exp_actual = None
        self._boca_pausada = False
        self.en_linea = True
        version = _texto_corto(estado_api.get("vTubeStudioVersion"), 20) or "versión desconocida"
        log.info("Conectado a VTube Studio %s en %.0f ms (%s)", version,
                 (time.monotonic() - inicio) * 1000, self._detalle_en_linea())
        self._cambiar_estado(EstadoModulo.OK, self._detalle_en_linea())
        return True

    def _denegar(self) -> None:
        """Error 50: el usuario denegó el plugin. No se reintenta la autenticación en 10 min."""
        self._bloqueo_auth_hasta = time.monotonic() + self.bloqueo_denegado_s
        log.warning("VTube Studio denegó el acceso al plugin «%s»; no lo vuelvo a pedir en %.0f min",
                    self.settings.vts_plugin_nombre, self.bloqueo_denegado_s / 60)
        self._marcar_sin_conexion(DETALLE_DENEGADO)

    async def _autenticar(self) -> bool:
        """Autenticación por token (§10.20 punto 3). ``False`` si no se pudo autenticar."""
        nombre = self.settings.vts_plugin_nombre
        plugin = {"pluginName": nombre, "pluginDeveloper": self.settings.vts_plugin_desarrollador}
        token = await asyncio.to_thread(_leer_token, self.ruta_token)
        if token:
            try:
                respuesta = await self._llamar("AuthenticationRequest",
                                               {**plugin, "authenticationToken": token})
            except VTSError as exc:
                if exc.error_id == ERROR_ACCESO_DENEGADO:
                    self._denegar()
                    return False
                log.info("VTube Studio rechazó el token guardado (error %d); pido uno nuevo", exc.error_id)
            else:
                if respuesta.get("authenticated") is True:
                    log.debug("Autenticado en VTube Studio con el token guardado")
                    return True
                log.info("VTube Studio no aceptó el token guardado (%s); pido uno nuevo",
                         _texto_corto(respuesta.get("reason"), 80) or "sin motivo")

        self._cambiar_estado(EstadoModulo.EN_ESPERA, f"acepta el plugin «{nombre}» en VTube Studio")
        log.info("Pidiendo acceso a VTube Studio: acepta el plugin «%s» en la ventana de VTS (%.0f s)",
                 nombre, self.timeout_token_s)
        try:
            respuesta = await self._llamar("AuthenticationTokenRequest", plugin,
                                           timeout=self.timeout_token_s)
        except VTSError as exc:
            if exc.error_id == ERROR_ACCESO_DENEGADO:
                self._denegar()
                return False
            if exc.error_id == ERROR_TOKEN_EN_CURSO:
                log.info("Ya hay una solicitud de acceso abierta en VTube Studio; espero a que la respondas")
                self._cambiar_estado(EstadoModulo.EN_ESPERA,
                                     f"acepta el plugin «{nombre}» en VTube Studio")
                return False
            log.warning("VTube Studio rechazó la solicitud de token: %s", exc)
            self._marcar_sin_conexion(f"error de la API ({exc.error_id})")
            return False
        except TimeoutError:
            log.info("Nadie aceptó el plugin en VTube Studio en %.0f s; lo intentaré más tarde",
                     self.timeout_token_s)
            self._marcar_sin_conexion("sin respuesta a la solicitud de acceso")
            return False

        nuevo = respuesta.get("authenticationToken")
        if not isinstance(nuevo, str) or not _RE_TOKEN.match(nuevo):
            log.warning("VTube Studio devolvió un token con formato inesperado; no se guarda")
            self._marcar_sin_conexion("token inválido")
            return False
        try:
            await asyncio.to_thread(_escribir_token, self.ruta_token, nuevo)
            log.info("Token de VTube Studio guardado en %s (permisos 600)", self.ruta_token.name)
        except OSError as exc:
            log.warning("No pude guardar el token de VTube Studio (%s); solo valdrá en esta sesión",
                        exc.__class__.__name__)

        respuesta = await self._llamar("AuthenticationRequest", {**plugin, "authenticationToken": nuevo})
        if respuesta.get("authenticated") is not True:
            log.warning("VTube Studio rechazó la autenticación con el token recién emitido (%s)",
                        _texto_corto(respuesta.get("reason"), 80) or "sin motivo")
            self._marcar_sin_conexion("autenticación rechazada")
            return False
        log.info("Plugin «%s» autorizado en VTube Studio", nombre)
        return True

    # ------------------------------------------------------------------
    # Inventario
    # ------------------------------------------------------------------

    async def _refrescar_inventario(self, respuesta_hotkeys: Mapping[str, Any] | None = None) -> None:
        """Lee hotkeys y expresiones del modelo y resuelve el vocabulario (§10.20 punto 4)."""
        if respuesta_hotkeys is None:
            try:
                respuesta_hotkeys = await self._llamar("HotkeysInCurrentModelRequest")
            except VTSError as exc:
                log.info("VTube Studio no entregó las hotkeys del modelo: %s", exc)
                respuesta_hotkeys = {}
        hotkeys = _hotkeys_validas(respuesta_hotkeys.get("availableHotkeys"))
        modelo_id = respuesta_hotkeys.get("modelID")
        modelo_id = modelo_id if isinstance(modelo_id, str) and modelo_id else None
        cargado = respuesta_hotkeys.get("modelLoaded")
        cargado = bool(cargado) if isinstance(cargado, bool) else bool(hotkeys or modelo_id)

        archivos: list[str] = []
        if cargado:
            try:
                estado = await self._llamar("ExpressionStateRequest", {"details": False})
                archivos = _archivos_expresion(estado.get("expressions"))
            except VTSError as exc:
                log.debug("ExpressionStateRequest falló durante el inventario: %s", exc)

        mapeo = await asyncio.to_thread(_leer_mapeo, self.ruta_mapeo)
        resolucion = _resolver(hotkeys, archivos, mapeo)
        # Errores de formato del archivo: AVISO (el usuario debe corregirlo). Entradas que no
        # existen en el modelo actual: INFO (pueden ser de otro modelo). Cada uno, una sola vez.
        for nivel, aviso in [*((logging.WARNING, a) for a in mapeo.avisos),
                             *((logging.INFO, a) for a in resolucion.avisos)]:
            if aviso not in self._avisos_mapeo:
                self._avisos_mapeo.add(aviso)
                log.log(nivel, "%s", aviso)

        cambio_modelo = modelo_id != self._modelo_id
        self._modelo_id = modelo_id
        self._modelo_nombre = _texto_corto(respuesta_hotkeys.get("modelName"), 40)
        self._modelo_cargado = cargado
        self._huella = _huella_hotkeys(hotkeys)
        self._exp_archivo = resolucion.exp_archivo
        self._exp_hotkey = resolucion.exp_hotkey
        self._motion_hotkey = resolucion.motion_hotkey
        self._gestionados_cf = frozenset(a.casefold() for a in resolucion.exp_archivo.values())
        if resolucion.parametro_boca != self._parametro_boca:
            self._parametro_boca = resolucion.parametro_boca
            if self._boca_motivo == "desconocido":
                self._boca_desactivada, self._boca_motivo = False, None
        if cambio_modelo:
            self._avisados.clear()
            self._exp_actual = None
            if self._boca_motivo == "desconocido":
                self._boca_desactivada, self._boca_motivo = False, None
        if not cargado:
            log.info("VTube Studio no tiene ningún modelo cargado; expresiones y movimientos en pausa")
        else:
            log.info("Inventario de VTube Studio: %s", self._detalle_en_linea())

    # ------------------------------------------------------------------
    # Reconexión y keepalive
    # ------------------------------------------------------------------

    async def bucle_reconexion(self, parar: asyncio.Event, *, base_s: float = 5.0,
                               max_s: float = 60.0) -> None:
        """Mantiene la conexión viva hasta ``parar`` (§10.20 punto 9).

        Sin conexión: ``conectar()`` y, si falla, *backoff* exponencial de ``base_s`` a ``max_s``.
        Con conexión: cada 30 s ``APIStateRequest`` como *ping*; si cambió ``modelID`` (o las
        hotkeys), refresca el inventario. ``ConnectionClosed`` → ``en_linea=False`` y
        ``SIN CONEXIÓN`` con "reintento en N s".
        """
        if not WEBSOCKETS_DISPONIBLE:
            self._cambiar_estado(EstadoModulo.OFF, "websockets no instalado")
            log.info("websockets no está instalado: VTube Studio queda desactivado")
            return
        if not self.settings.vts_habilitado:
            self._cambiar_estado(EstadoModulo.OFF, "deshabilitado")
            return
        base = max(0.05, float(base_s))
        tope = max(base, float(max_s))
        espera = base
        conectado_desde: float | None = None
        while not parar.is_set() and not self._cerrado:
            try:
                if not self.en_linea:
                    if conectado_desde is not None:
                        duracion = time.monotonic() - conectado_desde
                        conectado_desde = None
                        if duracion < 2 * base:
                            # Conexión efímera (VTS acepta y corta): no reconectar en bucle cerrado.
                            self._cambiar_estado(EstadoModulo.SIN_CONEXION,
                                                 self._detalle_con_reintento(espera))
                            await _esperar_evento(parar, espera)
                            espera = min(espera * 2, tope)
                            continue
                        espera = base
                    conectado = await self._con_parada(self.conectar(), parar)
                    if parar.is_set() or self._cerrado:
                        break
                    if conectado:
                        conectado_desde = time.monotonic()
                        continue
                    bloqueo = self._bloqueo_auth_hasta - time.monotonic()
                    if bloqueo > 0:
                        pausa = bloqueo
                    else:
                        pausa = espera
                        if self._estado == EstadoModulo.SIN_CONEXION:
                            # Un solo evento por cambio: conectar() conserva el sufijo si el
                            # motivo no cambió, y aquí solo cambia si cambia N (backoff).
                            self._cambiar_estado(EstadoModulo.SIN_CONEXION,
                                                 self._detalle_con_reintento(pausa))
                        espera = min(espera * 2, tope)
                    await _esperar_evento(parar, pausa)
                    continue
                motivo_espera = await self._esperar_en_linea(parar, self.intervalo_keepalive_s)
                if motivo_espera == "parar":
                    break
                if motivo_espera == "cerrado":
                    if self.en_linea:
                        self._perder_conexion("VTube Studio cerró la conexión")
                    continue
                if self.en_linea:
                    await self._keepalive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # el bucle no debe morir por un fallo inesperado
                log.warning("Fallo inesperado en el bucle de VTube Studio: %s; reintento en %.0f s",
                            exc, espera, exc_info=True)
                await _esperar_evento(parar, espera)
                espera = min(espera * 2, tope)
        log.debug("Bucle de reconexión de VTube Studio terminado")

    async def _con_parada(self, corrutina: Awaitable[bool], parar: asyncio.Event) -> bool:
        """Ejecuta ``corrutina`` y la cancela si se activa ``parar`` antes de que termine."""
        tarea = asyncio.ensure_future(corrutina)
        espera_parar = asyncio.ensure_future(parar.wait())
        try:
            await asyncio.wait({tarea, espera_parar}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pendiente in (tarea, espera_parar):
                if not pendiente.done():
                    pendiente.cancel()
            await asyncio.wait({tarea, espera_parar})
        if tarea.cancelled():
            return False
        excepcion = tarea.exception()
        if excepcion is not None:
            log.warning("conectar() terminó con un error inesperado: %s", excepcion)
            return False
        return bool(tarea.result())

    async def _esperar_en_linea(self, parar: asyncio.Event, segundos: float) -> str:
        """Espera ``segundos`` o hasta ``parar`` o el cierre del socket: "parar"/"cerrado"/"tiempo"."""
        esperas: list[asyncio.Future[Any]] = [asyncio.ensure_future(parar.wait())]
        cierre: asyncio.Future[Any] | None = None
        esperar_cierre = getattr(self._ws, "wait_closed", None)
        if callable(esperar_cierre):
            cierre = asyncio.ensure_future(esperar_cierre())
            esperas.append(cierre)
        try:
            hechas, _pendientes = await asyncio.wait(esperas, timeout=max(0.0, segundos),
                                                     return_when=asyncio.FIRST_COMPLETED)
        finally:
            for futuro in esperas:
                if not futuro.done():
                    futuro.cancel()
            await asyncio.gather(*esperas, return_exceptions=True)
        if parar.is_set():
            return "parar"
        if (cierre is not None and cierre in hechas) or not self.en_linea:
            return "cerrado"
        return "tiempo"

    async def _keepalive(self) -> None:
        """Ping con ``APIStateRequest`` y refresco del inventario si cambió el modelo."""
        try:
            inicio = time.monotonic()
            estado_api = await self._llamar("APIStateRequest")
            self._latencia_ms = int((time.monotonic() - inicio) * 1000)
            if estado_api.get("currentSessionAuthenticated") is False:
                log.warning("VTube Studio ya no reconoce la sesión del plugin (¿token revocado?); reconecto")
                self._perder_conexion("sesión no autenticada")
                return
            hotkeys = await self._llamar("HotkeysInCurrentModelRequest")
            modelo_id = hotkeys.get("modelID")
            modelo_id = modelo_id if isinstance(modelo_id, str) and modelo_id else None
            huella = _huella_hotkeys(_hotkeys_validas(hotkeys.get("availableHotkeys")))
            if modelo_id != self._modelo_id or huella != self._huella:
                log.info("Cambió el modelo (o sus hotkeys) en VTube Studio; actualizo el inventario")
                await self._refrescar_inventario(hotkeys)
                self._cambiar_estado(EstadoModulo.OK, self._detalle_en_linea())
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._perder_conexion("VTube Studio no responde al keepalive")
        except _ERRORES_VTS as exc:
            self._manejar_error(exc, "el keepalive")

    # ------------------------------------------------------------------
    # Expresiones
    # ------------------------------------------------------------------

    def _avisar_sin_resolver(self, tipo: str, valor: str) -> None:
        """Registra una sola vez por modelo que un ID del vocabulario no tiene hotkey."""
        clave = (tipo, valor)
        if clave not in self._avisados:
            self._avisados.add(clave)
            log.info("sin hotkey para %s «%s»; ignorando", tipo, valor)

    async def expresion(self, exp: str) -> bool:
        """Pone la expresión ``exp`` (por archivo ``.exp3.json``). ``True`` si quedó aplicada.

        Una EXP nueva lee antes qué expresiones están realmente activas, apaga las del plugin
        con ``active: false`` y activa la nueva con ``fadeTime 0.25``. ``neutral`` solo apaga. La
        misma EXP no hace nada salvo que VTS diga que ya no está activa (entonces se reactiva).
        """
        if not self.en_linea:
            return False
        valor = normalizar_valor(exp)
        if valor not in EXP_VALIDOS:
            log.debug("EXP desconocida «%s»; ignorando", _texto_corto(valor, 40))
            return False
        self._asegurar_locks()
        assert self._lock_expresion is not None
        async with self._lock_expresion:
            if not self.en_linea:
                return False
            try:
                return await self._cambiar_expresion(valor)
            except asyncio.CancelledError:
                raise
            except _ERRORES_VTS as exc:
                self._manejar_error(exc, f"la expresión «{valor}»")
                return False
            except Exception as exc:  # garantía de no propagar: fallo inesperado
                log.warning("Fallo inesperado al aplicar la expresión «%s»: %s", valor, exc, exc_info=True)
                return False

    async def _cambiar_expresion(self, exp: str) -> bool:
        """Cuerpo de :meth:`expresion` (con el lock de expresiones tomado)."""
        archivo = self._exp_archivo.get(exp)
        hotkey = self._exp_hotkey.get(exp)
        if exp != "neutral" and archivo is None and hotkey is None:
            self._avisar_sin_resolver("EXP", exp)
            return False

        if exp == self._exp_actual:
            if archivo is None:          # neutral, o EXP por hotkey: no hay estado que vigilar
                return True
            activos = await self._leer_activos()
            if activos is None or archivo.casefold() in activos:
                return True
            log.debug("La expresión «%s» ya no está activa en VTS; la reactivo", exp)
            await self._activar_archivo(archivo)
            return True

        activos = await self._leer_activos()
        await self._apagar_gestionadas(activos, excepto=archivo)
        if archivo is not None:
            if activos is None or archivo.casefold() not in activos:
                await self._activar_archivo(archivo)
        elif hotkey is not None:
            await self._disparar_hotkey(hotkey)
        self._exp_actual = exp
        return True

    async def _leer_activos(self) -> dict[str, str] | None:
        """Expresiones activas según VTS (``casefold`` → nombre real), o ``None`` si no se pudo leer."""
        try:
            respuesta = await self._llamar("ExpressionStateRequest", {"details": False})
        except VTSError as exc:
            log.debug("No pude leer el estado de las expresiones: %s", exc)
            return None
        lista = respuesta.get("expressions")
        if not isinstance(lista, list):
            return None
        activos: dict[str, str] = {}
        for item in lista:
            if isinstance(item, Mapping) and item.get("active") is True and _es_archivo_expresion(item.get("file")):
                activos[item["file"].casefold()] = item["file"]
        return activos

    async def _apagar_gestionadas(self, activos: dict[str, str] | None, *, excepto: str | None) -> None:
        """Apaga con ``active: false`` las expresiones del plugin que siguen activas."""
        excepto_cf = excepto.casefold() if excepto else None
        if activos is None:
            # Sin lectura de estado: se apaga solo la última que puso el plugin.
            previo = self._exp_archivo.get(self._exp_actual or "")
            objetivos = [previo] if previo and previo.casefold() != excepto_cf else []
        else:
            objetivos = [real for plegado, real in activos.items()
                         if plegado in self._gestionados_cf and plegado != excepto_cf]
        for archivo in objetivos:
            try:
                await self._llamar("ExpressionActivationRequest", {"expressionFile": archivo, "active": False})
            except VTSError as exc:
                log.debug("VTS no apagó la expresión %s: %s", _texto_corto(archivo, 60), exc)

    async def _activar_archivo(self, archivo: str) -> None:
        """Activa una expresión por archivo con el *fade* de 0,25 s."""
        await self._llamar("ExpressionActivationRequest",
                           {"expressionFile": archivo, "fadeTime": FADE_EXPRESION_S, "active": True})

    # ------------------------------------------------------------------
    # Movimientos
    # ------------------------------------------------------------------

    async def _disparar_hotkey(self, id_hotkey: str) -> None:
        """``HotkeyTriggerRequest`` respetando 250 ms entre disparos (límite de VTS)."""
        self._asegurar_locks()
        assert self._lock_hotkey is not None
        async with self._lock_hotkey:
            espera = self._ultimo_disparo + MIN_ENTRE_HOTKEYS_S - time.monotonic()
            if espera > 0:
                await asyncio.sleep(espera)
            try:
                await self._llamar("HotkeyTriggerRequest", {"hotkeyID": id_hotkey})
            finally:
                self._ultimo_disparo = time.monotonic()

    async def movimiento(self, motion: str) -> bool:
        """Dispara el gesto ``motion`` con su hotkey. ``idle`` no hace nada (devuelve ``True``)."""
        if not self.en_linea:
            return False
        valor = normalizar_valor(motion)
        if valor not in MOTION_VALIDOS:
            log.debug("MOTION desconocido «%s»; ignorando", _texto_corto(valor, 40))
            return False
        if valor == "idle":
            return True
        id_hotkey = self._motion_hotkey.get(valor)
        if id_hotkey is None:
            self._avisar_sin_resolver("MOTION", valor)
            return False
        try:
            await self._disparar_hotkey(id_hotkey)
            return True
        except asyncio.CancelledError:
            raise
        except _ERRORES_VTS as exc:
            self._manejar_error(exc, f"el movimiento «{valor}»")
            return False
        except Exception as exc:  # garantía de no propagar: fallo inesperado
            log.warning("Fallo inesperado al disparar el movimiento «%s»: %s", valor, exc, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # SalidaAvatar: etiquetas, estado y boca
    # ------------------------------------------------------------------

    async def aplicar_etiquetas(self, etiquetas: Etiquetas) -> None:
        """Aplica ``[EXP]`` y ``[MOTION]`` de un segmento (lo llama riko antes de hablar).

        Tiene un presupuesto de 2,5 s para no retrasar la voz si VTS va lento. No cuenta para
        el intervalo de :meth:`aplicar_estado` (§10.20 punto 8: ≥ 15 s desde la última llamada
        efectiva *de aplicar_estado*), así que al terminar de hablar la expresión de reposo
        vuelve en cuanto ese intervalo lo permite.
        """
        if not self.en_linea:
            return
        exp = getattr(etiquetas, "exp", None)
        motion = getattr(etiquetas, "motion", None)
        try:
            await asyncio.wait_for(self._aplicar(exp, motion), timeout=self.timeout_etiquetas_s)
        except TimeoutError:
            log.info("VTube Studio tardó demasiado en aplicar las etiquetas; sigo sin esperar")

    async def _aplicar(self, exp: Any, motion: Any) -> None:
        """Expresión y luego gesto (cada uno ya captura sus errores)."""
        if isinstance(exp, str):
            await self.expresion(exp)
        if isinstance(motion, str):
            await self.movimiento(motion)

    async def aplicar_estado(self, estado: HomeostaticState, *, forzar: bool = False) -> None:
        """Expresión de reposo ``sugerir_etiquetas(estado).exp`` como mucho cada 15 s.

        El intervalo cuenta desde la última llamada efectiva (no saltada) de este mismo método;
        :meth:`aplicar_etiquetas` no lo reinicia. La llama main solo cuando riko no está
        hablando. ``forzar`` ignora el intervalo.
        """
        if not self.en_linea:
            return
        ahora = time.monotonic()
        if (not forzar and self._ultimo_estado_ts is not None
                and ahora - self._ultimo_estado_ts < self.intervalo_estado_s):
            return
        self._ultimo_estado_ts = ahora
        try:
            exp = sugerir_etiquetas(estado).exp
        except Exception as exc:  # estado malformado: no se toca el avatar
            log.debug("No pude sugerir la expresión de reposo: %s", exc)
            return
        await self.expresion(exp)

    async def boca(self, valor: float) -> None:
        """Fija la apertura de la boca (0..1) sin bloquear; el envío lo hace la tarea a 30 Hz.

        Si la inyección se pausó por errores seguidos, no hace nada hasta :meth:`cerrar_boca`
        (fin de la frase) o hasta una reconexión.
        """
        if not self.en_linea or self._boca_desactivada or self._boca_pausada or self._cerrado:
            return
        try:
            v = float(valor)
        except (TypeError, ValueError):
            return
        if not math.isfinite(v):
            v = 0.0
        self._boca_objetivo = min(1.0, max(0.0, v))
        self._boca_ultimo = time.monotonic()
        tarea = self._tarea_boca
        if tarea is None or tarea.done():
            nueva = asyncio.get_running_loop().create_task(self._bucle_boca(), name="rams-vts-boca")
            nueva.add_done_callback(self._fin_tarea_boca)
            self._tarea_boca = nueva

    @staticmethod
    def _fin_tarea_boca(tarea: asyncio.Task[None]) -> None:
        """Recoge el resultado de la tarea de la boca (no deja excepciones sin leer)."""
        if tarea.cancelled():
            return
        excepcion = tarea.exception()
        if excepcion is not None:
            log.warning("La tarea de la boca de VTube Studio terminó con error: %s", excepcion)

    async def _bucle_boca(self) -> None:
        """Reenvía el parámetro de boca a 30 Hz mientras haya ``boca()`` de hace < 0,5 s."""
        loop = asyncio.get_running_loop()
        periodo = 1.0 / HZ_BOCA
        proximo = loop.time()
        errores = 0
        while self.en_linea and not self._boca_desactivada and not self._boca_pausada and not self._cerrado:
            if time.monotonic() - self._boca_ultimo >= VENTANA_BOCA_S:
                break
            if await self._inyectar_boca(self._boca_objetivo, cara=True):
                errores = 0
            else:
                errores += 1
                if errores >= MAX_ERRORES_BOCA:
                    if time.monotonic() - self._boca_ultimo < VENTANA_BOCA_S:
                        # La frase sigue: la marca impide que el siguiente boca() (33 ms después)
                        # lance otra tarea. Si cerrar_boca() ya la terminó, no se pausa la próxima.
                        self._boca_pausada = True
                        log.info("La boca no responde en VTube Studio; pauso la inyección hasta "
                                 "la próxima frase")
                    break
            proximo += periodo
            espera = proximo - loop.time()
            if espera < 0:               # atrasado: no se recupera el atraso
                proximo = loop.time()
                espera = 0.0
            await asyncio.sleep(espera)

    async def _inyectar_boca(self, valor: float, *, cara: bool) -> bool:
        """Un ``InjectParameterDataRequest`` del parámetro de boca. ``True`` si VTS lo aceptó."""
        datos = {
            "faceFound": bool(cara),
            "mode": "set",
            "parameterValues": [{"id": self._parametro_boca, "value": round(float(valor), 4)}],
        }
        try:
            await self._llamar("InjectParameterDataRequest", datos)
            return True
        except asyncio.CancelledError:
            raise
        except VTSError as exc:
            texto = exc.mensaje.casefold()
            if exc.error_id == ERROR_REQUIERE_AUTENTICACION:
                self._manejar_error(exc, "la boca")      # token revocado: reconectar ya
            elif exc.error_id == ERROR_PARAMETRO_OCUPADO or "other plugin" in texto or "controlled by" in texto:
                self._desactivar_boca("ocupado")
            elif exc.error_id == ERROR_PARAMETRO_NO_ENCONTRADO or "not found" in texto:
                self._desactivar_boca("desconocido")
            else:
                log.debug("VTube Studio rechazó la inyección de la boca: %s", exc)
            return False
        except _ERRORES_VTS as exc:
            self._manejar_error(exc, "la boca")
            return False

    def _desactivar_boca(self, motivo: str) -> None:
        """Desactiva la boca durante la sesión con un único aviso."""
        if self._boca_desactivada:
            return
        self._boca_desactivada = True
        self._boca_motivo = motivo
        if motivo == "ocupado":
            log.warning("Otro plugin controla el parámetro «%s» en VTube Studio; desactivo la boca "
                        "de Amadeus durante esta sesión", self._parametro_boca)
        else:
            log.warning("El modelo de VTube Studio no tiene el parámetro «%s»; desactivo la boca "
                        "(revisa parametro_boca en vts_mapeo.json)", self._parametro_boca)

    async def cerrar_boca(self) -> None:
        """Para la tarea de la boca y envía una vez ``value 0`` con ``faceFound: false``.

        Es el fin de la frase: levanta la pausa por errores, así la siguiente vuelve a intentarlo.
        """
        self._boca_ultimo = float("-inf")       # la tarea sale en su próxima vuelta
        self._boca_pausada = False
        tarea, self._tarea_boca = self._tarea_boca, None
        if tarea is not None and not tarea.done():
            _hechas, pendientes = await asyncio.wait({tarea}, timeout=0.3)
            if pendientes:
                tarea.cancel()
                await asyncio.wait({tarea}, timeout=0.5)
        if not self.en_linea or self._boca_desactivada:
            return
        await self._inyectar_boca(0.0, cara=False)

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------

    async def cerrar(self) -> None:
        """Cierra la boca, devuelve el avatar a ``neutral`` y cierra la conexión (idempotente)."""
        if self._cerrado:
            return
        try:
            if self.en_linea:
                # Presupuesto corto: vtube.cerrar() es el primero del apagado (§6.6, 15 s en total).
                await asyncio.wait_for(self._despedida_avatar(), timeout=1.5)
        except TimeoutError:
            log.debug("VTube Studio no confirmó la vuelta a reposo antes de cerrar")
        finally:
            self._cerrado = True
            self.en_linea = False
            tarea, self._tarea_boca = self._tarea_boca, None
            if tarea is not None and not tarea.done():
                tarea.cancel()
                await asyncio.wait({tarea}, timeout=0.5)
            ws, self._ws = self._ws, None
            await self._cerrar_ws(ws)
            self._cambiar_estado(EstadoModulo.OFF, "desconectado")
            log.debug("Puente de VTube Studio cerrado")

    async def _despedida_avatar(self) -> None:
        """Boca cerrada y expresiones del plugin apagadas (el avatar queda en reposo)."""
        await self.cerrar_boca()
        await self.expresion("neutral")
