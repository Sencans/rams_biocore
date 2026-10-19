"""Despachador de salida de R.A.M.S. BIO-CORE: voz + avatar (presente) y Telegram (ausente) (§10.19).

Qué hace:
- Mantiene **una única cola** de ``Utterance`` (``asyncio.PriorityQueue``) con un consumidor
  (``bucle``) que, al apagar, **drena** la cola; ``vaciar`` la consume por sí misma si no hay
  consumidor activo, así que la despedida siempre se entrega.
- **Usuario presente:** voz neuronal (ElevenLabs en streaming PCM con la voz
  ``p7sm2zw5Us66lZr2brDW``; *fallback* a edge-tts), boca y expresiones del avatar (solo vía el
  ``Protocol`` ``SalidaAvatar``), subtítulos en la terminal y en el HUD.
- **Usuario ausente:** "llamadas" de Amadeus por Telegram (llamada perdida + nota de voz +
  transcripción, con botones *Devolver llamada* / *Ahora no*), insistencia de **Megumin**
  (nivel 2, solo texto) y avisos críticos con su "Explosión" diaria (nivel 3, persistida con
  ``meta_get``/``meta_set``).
- Compuerta de proactividad sin LLM (§8): Telegram = pausa + horario silencioso + cuota diaria;
  voz/terminal = pausa + máximo de interrupciones por hora y ≥ 10 min entre interrupciones
  (``descanso`` siempre se permite).
- Entrada de Telegram por *long polling* con filtro estricto de remitente (§9.3).
- Multimedia de Telegram (v0.2.1): ``enviar_foto_telegram`` (``sendPhoto`` de un JPEG en memoria,
  ≤ 10 MB), ``enviar_voz_telegram`` (respuesta como nota de voz MP3, ElevenLabs o edge-tts) y
  notas de voz/audios entrantes (≤ 2 MB y ≤ 3 min): ``getFile`` → descarga en memoria →
  transcriptor inyectado con ``set_transcriptor`` → ``manejador(texto, voz=True)``. Nada de eso
  toca el disco ni el log (del audio y de la foto solo se registran tamaños).

Dependencias opcionales: ``httpx`` (ElevenLabs y Telegram), ``edge_tts`` (voz de respaldo),
``numpy`` (RMS del lip-sync; si falta, ``array('h')`` + ``math``; **nunca** ``audioop``),
``sounddevice`` (reproducción en Windows, importado solo al reproducir), ``soundfile`` (decodifica
el MP3 de edge-tts sin ffmpeg; su wheel trae libsndfile con MP3; importado solo en el executor de
audio). Binarios opcionales: ``pw-play``/``paplay``/``aplay`` (PCM), ``ffmpeg`` (MP3 → PCM),
``mpv``/``ffplay`` (MP3), ``notify-send``.

Caminos de edge-tts (MP3 24 kHz), por orden de preferencia:
1. ``ffmpeg`` en streaming → PCM → reproductor PCM, con lip-sync RMS.
2. ``soundfile`` (sin ffmpeg): MP3 completo → PCM decodificado en el executor de audio →
   reproductor PCM por bloques de 100 ms, con el mismo lip-sync RMS. Es el camino de Windows
   (``soundfile`` + ``sounddevice``), donde no suele haber ffmpeg. ``iniciar`` comprueba que
   soundfile carga y que su libsndfile lee MP3 (≥ 1.1); si no, se descarta antes de hablar. El MP3
   del segmento siguiente se descarga mientras suena el actual.
3. ``mpv``/``ffplay`` leyendo el MP3, con la boca simulada.

Sincronía con el avatar: el subtítulo (``salida.subtitulo``, canal ``voz``) de cada segmento
hablado se publica cuando su audio empieza de verdad (al abrir el reproductor), porque el puente
de VTuber AI Studio mueve los labios del visor en cuanto lo recibe.

Degradación: sin httpx no hay ElevenLabs ni Telegram; sin edge_tts no hay *fallback* de voz;
sin reproductor solo hay texto; sin avatar, voz sin boca; sin memoria, la cuota, las llamadas y
la Explosión se cuentan solo en RAM y no se registran turnos. **Nunca** se pierde el texto:
siempre llega a la terminal.

Memoria y avatar se usan **solo** vía los ``Protocol`` ``RegistroLlamadas`` y ``SalidaAvatar``
(§4.3.7); este módulo no importa ``VTubeBridge`` ni ``MemoryEngine``.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import functools
import html
import importlib.util
import inspect
import io
import itertools
import json
import logging
import math
import os
import random
import re
import shutil
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Final

from core.bus import BusEventos
from core.config import Settings, en_horario_silencioso, zona_local
from core.contracts import (
    INICIATIVAS,
    LANG_VALIDOS,
    BrocaReply,
    DiagnosticoModulo,
    EstadoModulo,
    EstadoPresencia,
    Etiquetas,
    RegistroLlamadas,
    SalidaAvatar,
    Segmento,
    Utterance,
)
from core.etiquetas import (
    ajustes_voz,
    limpiar_para_telegram,
    limpiar_para_voz,
    prosodia_edge,
    recortar_segmentos,
    tag_audio,
)

try:  # opcional: ElevenLabs y Telegram
    import httpx
except ImportError:  # sin httpx: solo voz local con edge-tts y terminal
    httpx = None

try:  # opcional: RMS vectorizado para el lip-sync
    import numpy as np
except ImportError:  # sin numpy: RMS con array('h') + math
    np = None

try:  # opcional: voz de respaldo gratuita
    import edge_tts
except ImportError:  # sin edge-tts: sin fallback de voz
    edge_tts = None


def _modulo_instalado(nombre: str) -> bool:
    """``True`` si el módulo existe, **sin importarlo** (sounddevice inicializa PortAudio al importarse
    y soundfile carga libsndfile por cffi)."""
    try:
        return importlib.util.find_spec(nombre) is not None
    except (ImportError, ValueError):
        return False


SOUNDDEVICE_INSTALADO: Final = _modulo_instalado("sounddevice")
SOUNDFILE_INSTALADO: Final = _modulo_instalado("soundfile")

__all__ = [
    "RikoMeguminIO",
    "INTERVALO_MIN_INTERRUPCION_S",
    "INTERVALO_MIN_ENVIO_S",
    "ESCALERA_REFRACTARIO_CRITICO_S",
    "MEGUMIN_DEFECTO",
    "ORIGENES_PROACTIVOS",
]

log = logging.getLogger("rams.salida")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Compuerta de proactividad (§10.19 paso 2)
INTERVALO_MIN_INTERRUPCION_S: Final = 600          # ≥ 10 min entre interrupciones presenciales
VENTANA_INTERRUPCIONES_S: Final = 3600.0           # "por hora"
ORIGENES_PROACTIVOS: Final = frozenset({"iniciativa", "agenda", "alerta", "nexus"})
ORIGENES_COMPUERTA: Final = frozenset({"iniciativa", "nexus"})   # "alerta" y "agenda" siempre pasan

# Refractario de avisos críticos por Telegram (nivel 3): 10 → 30 → 60 min por clave.
ESCALERA_REFRACTARIO_CRITICO_S: Final = (600.0, 1800.0, 3600.0)
REINICIO_REFRACTARIO_S: Final = 6 * 3600.0

# Telegram
URL_TELEGRAM: Final = "https://api.telegram.org"
# Mínimo entre envíos (sendMessage/sendVoice/sendPhoto). Variable de módulo para que los tests la anulen.
INTERVALO_MIN_ENVIO_S: float = 1.0
METODOS_ENVIO: Final = frozenset({"sendMessage", "sendVoice", "sendPhoto"})
ACTUALIZACIONES_PERMITIDAS: Final = ("message", "callback_query")
LIMITE_MENSAJE: Final = 4000           # margen bajo el límite de 4096 caracteres
LIMITE_LEYENDA: Final = 1000           # caption de sendVoice/sendPhoto (≤ 1024)
# Multimedia (Bot API): sendPhoto ≤ 10 MB; sendVoice ≤ 50 MB (OGG/Opus, MP3 o M4A); getFile
# descarga ≤ 20 MB, pero aquí solo se aceptan notas de voz/audios de ≤ 2 MB y ≤ 3 min.
LIMITE_FOTO_BYTES: Final = 10 * 1024 * 1024
LIMITE_VOZ_BYTES: Final = 50 * 1024 * 1024
LIMITE_AUDIO_ENTRANTE_BYTES: Final = 2 * 1024 * 1024
LIMITE_AUDIO_ENTRANTE_S: Final = 180
LIMITE_TEXTO_VOZ: Final = 1500         # caracteres que se sintetizan en una respuesta por voz
TIMEOUT_DESCARGA_S: Final = 30.0       # descarga de una nota de voz (getFile + /file/bot…)
# Transcripción de una nota de voz (Gemini). Variable de módulo para que los tests la reduzcan.
TIMEOUT_TRANSCRIPCION_S: float = 60.0
MIME_AUDIO_DEFECTO: Final = {"voice": "audio/ogg", "audio": "audio/mpeg"}
_RE_MIME_AUDIO: Final = re.compile(r"^audio/[a-z0-9][a-z0-9.+\-]{0,40}$")
_RE_RUTA_ARCHIVO: Final = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-./]{0,255}$")
MENSAJE_AUDIO_SIN_TRANSCRIPTOR: Final = ("Ahora mismo no puedo escuchar notas de voz. "
                                         "¿Me lo escribes, por favor?")
MENSAJE_AUDIO_FALLIDO: Final = "No pude escuchar tu nota de voz. ¿Me la repites o me lo escribes?"
MENSAJE_AUDIO_LARGO: Final = ("Tu audio es demasiado largo para mí (máximo 3 minutos). "
                              "¿Me lo resumes en uno más corto o me lo escribes?")
MAX_REINTENTOS_429: Final = 2
ESPERA_ERROR_TELEGRAM_S: Final = 5.0
LONG_POLL_S: Final = 50
_RE_CALLBACK: Final = re.compile(r"^(dev|no):(-?\d{1,15})$")
MENSAJE_409: Final = ("Otro proceso usa getUpdates con este bot (¿Nexus IDE?). "
                      "Configura un bot distinto para R.A.M.S.")

# ElevenLabs
URL_ELEVENLABS: Final = "https://api.elevenlabs.io"
FORMATO_NOTA_VOZ: Final = "mp3_44100_64"

# edge-tts
VOCES_EDGE_POR_LANG: Final = {"en": "en-US-AriaNeural", "ja": "ja-JP-NanamiNeural"}
SR_EDGE: Final = 24000
TIMEOUT_EDGE_MP3_S: Final = 45.0          # MP3 completo de edge-tts (soundfile, mpv/ffplay y notas de voz)
TIMEOUT_DECODIFICAR_S: Final = 20.0       # decodificación con soundfile en el executor de audio
BLOQUE_PCM_MS: Final = 100                # bloques del PCM decodificado hacia _reproducir_pcm
INTERVALO_CORTE_S: Final = 0.1            # cada cuánto se atiende un corte mientras se descarga el MP3
MAX_FALLOS_SOUNDFILE: Final = 3           # decodificaciones fallidas seguidas antes de descartar soundfile
# Comprobación de soundfile en ``iniciar`` (importarlo carga libsndfile y numpy: ~0,3 s en frío). Va
# en paralelo con el sondeo de ElevenLabs y por debajo de TIMEOUT_DIAGNOSTICO_S. Variable de módulo.
TIMEOUT_VALIDAR_SOUNDFILE_S: float = 2.0

# Lip-sync (informe 04 §10.2)
TRAMA_MS: Final = 20
VENTAJA_S: Final = 0.12
FRECUENCIA_BOCA_HZ: Final = 30.0
MAX_NIVEL_BOCA_HZ: Final = 15.0
ATAQUE: Final = 0.6
LIBERACION: Final = 0.25
COMPUERTA_RUIDO: Final = 0.015
GANANCIA: Final = 5.0
PAUSA_ENTRE_SEGMENTOS_S: Final = 0.15
PALABRAS_POR_SEGUNDO: Final = 2.6

# Cola
ESPERA_COLA_S: Final = 0.25
TIMEOUT_MEMORIA_S: Final = 5.0

# Diagnósticos de arranque. CONTRATO: main acota TODO ``iniciar()`` con ``wait_for(..., 4)`` (§6.4),
# así que cada sondeo (GET de la voz en ElevenLabs, ``getMe``) tiene 3 s y el conjunto ≤ 3,5 s: un
# servicio lento devuelve su propio DEGRADADO/SIN CONEXIÓN sin arrastrar al otro. Variables de
# módulo (no ``Final``) para que los tests las reduzcan.
TIMEOUT_DIAGNOSTICO_S: float = 3.0
MARGEN_DIAGNOSTICO_S: float = 0.5
TIMEOUT_CUOTA_ARRANQUE_S: float = 1.0      # lectura de la cuota en paralelo con los sondeos

# Meta de la memoria con las iniciativas registradas que no llegaron a Telegram (no gastan cuota).
META_NO_ENTREGADAS: Final = "llamadas_no_entregadas"

# Megumin (Anexo C.3): plantillas por defecto si persona/frases.json falta o está incompleto.
MEGUMIN_DEFECTO: Final[dict[str, tuple[dict[str, str], ...]]] = {
    # "nivel1" solo se usa con RAMS_MENSAJERO=megumin (CONTRATO de cierre): Megumin entrega la
    # primera llamada perdida en lugar de Amadeus.
    "nivel1": ({
        "apertura": "¡Atención, {usuario}! Amadeus te llamó a las {hora} y no contestaste, así que yo, "
                    "Megumin, mensajera carmesí, traigo su mensaje:",
        "cierre": "Mensaje entregado. Puedes devolverle la llamada con el botón.",
    },),
    "nivel2": ({
        "apertura": "¡Escucha bien, {usuario}! Amadeus se rindió de llamarte y me envió a mí, "
                    "Megumin, mensajera carmesí:",
        "cierre": "…Un mensaje sin explosiones. Qué desperdicio de talento.",
    },),
    "nivel3": ({
        "apertura": "¡EXPLOSIÓN! …de urgencia, quiero decir:",
        "cierre": "Esa fue mi Explosión de hoy. Ahora me desplomo.",
    },),
    "nivel3_agotada": ({
        "apertura": "Ya usé mi Explosión de hoy, así que esto va sin efectos especiales:",
        "cierre": "Atiéndelo, ¿sí?",
    },),
}
_RE_PLACEHOLDER: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_ERRORES_RED: tuple[type[BaseException], ...] = (OSError, asyncio.TimeoutError)
if httpx is not None:
    _ERRORES_RED = _ERRORES_RED + (httpx.HTTPError,)


# ---------------------------------------------------------------------------
# Errores internos (nunca salen del módulo)
# ---------------------------------------------------------------------------


class _ErrorEleven(Exception):
    """Respuesta no 200 de ElevenLabs (código HTTP y ``detail.code``)."""

    def __init__(self, status: int, codigo: str = "") -> None:
        super().__init__(f"ElevenLabs {status} {codigo}".strip())
        self.status = int(status)
        self.codigo = codigo


class _ErrorTelegram(Exception):
    """Fallo de la Bot API. ``codigo`` ``None`` = error de red. Nunca contiene la URL."""

    def __init__(self, codigo: int | None, descripcion: str = "") -> None:
        texto = "red" if codigo is None else str(codigo)
        super().__init__(f"Telegram {texto}: {descripcion}".strip())
        self.codigo = codigo
        self.descripcion = descripcion


class _ErrorReproductor(Exception):
    """El reproductor de audio no se pudo abrir o dejó de aceptar datos."""


class _ArchivoGrande(Exception):
    """El archivo de Telegram supera el límite que se acepta (se corta la descarga)."""


# Transcriptor de notas de voz: ``(audio, mime) -> texto | None`` (main inyecta el de Broca/Gemini).
Transcriptor = Callable[[bytes, str], Awaitable["str | None"]]


_PARADO: Final = object()   # centinela: se activó ``parar`` mientras se esperaba


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _imprimir_defecto(texto: str, etiquetas: Etiquetas | None = None) -> None:
    """Impresión por defecto: ``print(f"[Amadeus] › {texto}")`` (tolerante a consolas sin UTF-8)."""
    linea = f"[Amadeus] › {texto}"
    try:
        print(linea, flush=True)
    except UnicodeEncodeError:
        codificacion = getattr(sys.stdout, "encoding", None) or "ascii"
        print(linea.encode(codificacion, errors="replace").decode(codificacion, errors="replace"),
              flush=True)
    except (OSError, ValueError) as exc:  # stdout cerrado (servicio, apagado)
        log.debug("No se pudo escribir en stdout: %s", type(exc).__name__)


def _nivel_trama(trama: bytes) -> float:
    """Nivel de boca (0..1) de una trama PCM s16le: ``min(1, max(0, (rms − 0,015)·5))``.

    Con numpy si está; si no, ``array("h")`` (con ``byteswap`` en máquinas big-endian) + ``math``.
    """
    n = len(trama) // 2
    if n <= 0:
        return 0.0
    datos = trama[: n * 2]
    if np is not None:
        x = np.frombuffer(datos, dtype="<i2").astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
    else:
        muestras = array.array("h")
        muestras.frombytes(datos)
        if sys.byteorder == "big":
            muestras.byteswap()
        rms = math.sqrt(sum(m * m for m in muestras) / n) / 32768.0
    return min(1.0, max(0.0, (rms - COMPUERTA_RUIDO) * GANANCIA))


def _codigo_error_eleven(crudo: bytes) -> str:
    """Extrae ``detail.code`` (o ``status``) de un cuerpo de error de ElevenLabs."""
    try:
        cuerpo = json.loads(crudo.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError):
        return ""
    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    if isinstance(detalle, dict):
        return str(detalle.get("code") or detalle.get("status") or "")[:60]
    if isinstance(detalle, list):
        return "validacion"
    return ""


def _rellenar(plantilla: str, valores: Mapping[str, str]) -> str:
    """Sustituye ``{clave}``; un placeholder desconocido se borra (nunca llega un ``{x}`` a Telegram)."""
    def _sustituir(m: re.Match[str]) -> str:
        clave = m.group(1)
        if clave in valores:
            return str(valores[clave])
        log.debug("Placeholder desconocido en plantilla de Megumin: %s", clave)
        return ""
    return _RE_PLACEHOLDER.sub(_sustituir, plantilla)


def _una_linea(texto: str, maximo: int = 300) -> str:
    """Colapsa a una línea (Megumin: máximo 2 líneas de adorno) y acota la longitud."""
    limpio = " ".join(str(texto or "").split())
    if len(limpio) > maximo:
        limpio = limpio[: maximo - 1].rstrip() + "…"
    return limpio


def _acotar_caracteres(texto: str, maximo: int) -> str:
    """Recorta a ``maximo`` caracteres (con "…") sin escapar; el escape va después."""
    if len(texto) <= maximo:
        return texto
    return texto[: max(0, maximo - 1)].rstrip() + "…"


def _partir_texto(texto: str, maximo: int = LIMITE_MENSAJE) -> list[str]:
    """Parte un texto largo en trozos ≤ ``maximo`` caracteres, preferentemente por líneas."""
    if len(texto) <= maximo:
        return [texto]
    partes: list[str] = []
    actual = ""
    for linea in texto.split("\n"):
        while len(linea) > maximo:
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:maximo])
            linea = linea[maximo:]
        candidato = f"{actual}\n{linea}" if actual else linea
        if len(candidato) > maximo:
            partes.append(actual)
            actual = linea
        else:
            actual = candidato
    if actual:
        partes.append(actual)
    return [p for p in partes if p.strip()]


def _texto_telegram(segmentos: list[Segmento]) -> str:
    """Une los textos de los segmentos limpios para Telegram (conserva saltos de línea)."""
    textos = (limpiar_para_telegram(s.texto) for s in segmentos)
    return " ".join(t for t in textos if t).strip()


def _teclado_llamada(llamada_id: int) -> dict[str, Any]:
    """``reply_markup`` de una llamada: *Devolver llamada* (``dev:``) y *Ahora no* (``no:``)."""
    return {"inline_keyboard": [[
        {"text": "Devolver llamada", "callback_data": f"dev:{llamada_id}"},
        {"text": "Ahora no", "callback_data": f"no:{llamada_id}"},
    ]]}


def _entero_no_negativo(valor: Any) -> int | None:
    """Entero ≥ 0 de un campo de la Bot API (``duration``, ``file_size``), o ``None``."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    return valor if valor >= 0 else None


def _mime_audio(valor: Any, tipo: str) -> str:
    """MIME de la nota de voz/audio declarado por Telegram (saneado) o el habitual del tipo."""
    texto = str(valor or "").split(";", 1)[0].strip().lower()
    if _RE_MIME_AUDIO.match(texto):
        return texto
    return MIME_AUDIO_DEFECTO.get(tipo, "audio/ogg")


def _texto_para_voz(texto: str, maximo: int = LIMITE_TEXTO_VOZ) -> str:
    """Acota el texto de una respuesta por voz cortando en el último fin de oración que quepa."""
    if len(texto) <= maximo:
        return texto
    corte = texto[:maximo]
    fin = max(corte.rfind(". "), corte.rfind("? "), corte.rfind("! "), corte.rfind("… "))
    if fin >= maximo // 3:
        return corte[:fin + 1].strip()
    return corte.rstrip() + "…"


def _acepta_voz(manejador: Callable[..., Any]) -> bool | None:
    """¿El manejador acepta ``voz=``? ``None`` si su firma no se puede leer."""
    try:
        parametros = inspect.signature(manejador).parameters.values()
    except (TypeError, ValueError):
        return None
    return any((p.name == "voz" and p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD))
               or p.kind is p.VAR_KEYWORD for p in parametros)


# ---------------------------------------------------------------------------
# Reproductores de audio
# ---------------------------------------------------------------------------


class _Reproductor:
    """Reproductor PCM s16le mono: ``abrir(sr)``, ``escribir(pcm)``, ``cerrar()`` y ``abortar()``."""

    nombre: str = "?"
    latencia_s: float = 0.1

    async def abrir(self, sr: int) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError

    async def escribir(self, pcm: bytes) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError

    async def cerrar(self) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError

    async def abortar(self) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError


def _argv_reproductor(nombre: str, ruta: str, sr: int) -> list[str]:
    """Línea de órdenes de cada reproductor PCM de Linux (§10.19)."""
    if nombre == "pw-play":
        return [ruta, "--raw", "--rate", str(sr), "--channels", "1", "--format", "s16",
                "--latency", "40ms", "-"]
    if nombre == "paplay":
        return [ruta, "--raw", f"--rate={sr}", "--channels=1", "--format=s16le"]
    return [ruta, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(sr), "-c", "1"]   # aplay


class _ReproductorProceso(_Reproductor):
    """Reproductor por subproceso (``pw-play``, ``paplay`` o ``aplay``) que lee de stdin."""

    def __init__(self, nombre: str) -> None:
        self.nombre = nombre
        self.latencia_s = 0.04 if nombre == "pw-play" else 0.1
        self._proc: asyncio.subprocess.Process | None = None

    async def abrir(self, sr: int) -> None:
        ruta = shutil.which(self.nombre)
        if not ruta:
            raise _ErrorReproductor(f"{self.nombre} no está instalado")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *_argv_reproductor(self.nombre, ruta, sr),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            raise _ErrorReproductor(f"{self.nombre}: {type(exc).__name__}") from None

    async def escribir(self, pcm: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise _ErrorReproductor("reproductor no abierto")
        if proc.returncode is not None:
            raise _ErrorReproductor(f"{self.nombre} terminó (código {proc.returncode})")
        try:
            proc.stdin.write(pcm)
            await asyncio.wait_for(proc.stdin.drain(), 3.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise _ErrorReproductor(f"{self.nombre}: {type(exc).__name__}") from None

    async def cerrar(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
                await asyncio.wait_for(proc.stdin.wait_closed(), 2.0)
            except (OSError, asyncio.TimeoutError) as exc:
                log.debug("Cierre de stdin de %s: %s", self.nombre, type(exc).__name__)
        try:
            await asyncio.wait_for(proc.wait(), 5.0)
        except asyncio.TimeoutError:
            log.debug("%s no terminó a tiempo; se fuerza el cierre.", self.nombre)
            await self._matar(proc)

    async def abortar(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            await self._matar(proc)

    async def _matar(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            except OSError as exc:
                log.debug("No se pudo matar %s: %s", self.nombre, type(exc).__name__)
        try:
            await asyncio.wait_for(proc.wait(), 2.0)
        except asyncio.TimeoutError:
            log.debug("%s sigue vivo tras kill().", self.nombre)


class _ReproductorSoundDevice(_Reproductor):
    """Reproductor con ``sounddevice.RawOutputStream`` en el executor propio ``rams-audio``.

    Escribe por bloques de 100 ms. ``sounddevice`` se importa aquí (no al importar el módulo),
    porque al importarse inicializa PortAudio.
    """

    nombre = "sounddevice"

    def __init__(self, ejecutor: ThreadPoolExecutor) -> None:
        self._ejecutor = ejecutor
        self._flujo: Any = None
        self._bloque = 4800
        self._pendiente = bytearray()
        self.latencia_s = 0.1

    async def _en_hilo(self, funcion: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._ejecutor, funcion, *args)

    async def abrir(self, sr: int) -> None:
        def _abrir() -> Any:
            import sounddevice as sd  # opcional; importación diferida a propósito
            flujo = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16")
            flujo.start()
            return flujo
        try:
            self._flujo = await self._en_hilo(_abrir)
        except Exception as exc:  # ImportError, OSError (sin PortAudio), PortAudioError
            raise _ErrorReproductor(f"sounddevice: {type(exc).__name__}") from None
        self._bloque = max(2, (sr // 10) * 2)   # 100 ms de s16 mono
        latencia = getattr(self._flujo, "latency", None)
        if isinstance(latencia, (int, float)) and 0 <= latencia < 1:
            self.latencia_s = float(latencia)

    async def escribir(self, pcm: bytes) -> None:
        if self._flujo is None:
            raise _ErrorReproductor("sounddevice no abierto")
        self._pendiente += pcm
        while len(self._pendiente) >= self._bloque:
            bloque = bytes(self._pendiente[: self._bloque])
            del self._pendiente[: self._bloque]
            await self._escribir_bloque(bloque)

    async def _escribir_bloque(self, bloque: bytes) -> None:
        try:
            await self._en_hilo(self._flujo.write, bloque)
        except Exception as exc:  # PortAudioError, RuntimeError del executor cerrado
            raise _ErrorReproductor(f"sounddevice: {type(exc).__name__}") from None

    async def cerrar(self) -> None:
        flujo = self._flujo
        if flujo is None:
            return
        try:
            if self._pendiente:
                bloque = bytes(self._pendiente)
                self._pendiente.clear()
                await self._escribir_bloque(bloque)
        except _ErrorReproductor as exc:
            log.debug("Último bloque de audio perdido: %s", exc)
        self._flujo = None

        def _cerrar() -> None:
            flujo.stop()      # espera a que suene lo que quede en el búfer
            flujo.close()
        try:
            await self._en_hilo(_cerrar)
        except Exception as exc:
            log.debug("Cierre de sounddevice: %s", type(exc).__name__)

    async def abortar(self) -> None:
        flujo = self._flujo
        self._flujo = None
        self._pendiente.clear()
        if flujo is None:
            return

        def _abortar() -> None:
            flujo.abort()
            flujo.close()
        try:
            await self._en_hilo(_abortar)
        except Exception as exc:
            log.debug("Aborto de sounddevice: %s", type(exc).__name__)


def _detectar_reproductor_pcm(excluir: frozenset[str] = frozenset()) -> str | None:
    """Nombre del primer reproductor PCM disponible (§10.19), o ``None``.

    Linux (y otros Unix): ``pw-play`` → ``paplay`` → ``aplay`` y, si no hay ninguno,
    ``sounddevice`` (PortAudio). Windows y macOS: ``sounddevice``.
    """
    if sys.platform != "win32":
        for nombre in ("pw-play", "paplay", "aplay"):
            if nombre not in excluir and shutil.which(nombre):
                return nombre
    if SOUNDDEVICE_INSTALADO and "sounddevice" not in excluir:
        return "sounddevice"
    return None


def _argv_reproductor_mp3() -> list[str] | None:
    """``mpv`` o ``ffplay`` leyendo MP3 de stdin (sin ffmpeg para decodificar), o ``None``."""
    ruta = shutil.which("mpv")
    if ruta:
        return [ruta, "--no-video", "--really-quiet", "-"]
    ruta = shutil.which("ffplay")
    if ruta:
        return [ruta, "-nodisp", "-autoexit", "-loglevel", "error", "-i", "-"]
    return None


def _nombre_programa(ruta: str) -> str:
    """Ruta de un ejecutable → nombre corto para el diagnóstico (``…/ffplay.EXE`` → ``"ffplay"``)."""
    return os.path.splitext(os.path.basename(ruta))[0].lower() or ruta


def _soundfile_con_mp3() -> Any:
    """Importa ``soundfile`` y comprueba que su libsndfile decodifica MP3. Devuelve el módulo.

    **Bloqueante** (executor de audio). La wheel de soundfile trae libsndfile ≥ 1.1 con MP3, pero
    donde no hay librería empaquetada (armv7, musl, paquete de la distro, sdist) soundfile carga la
    libsndfile del sistema, que puede ser anterior a 1.1 y no leer MP3: cada ``sf.read`` fallaría
    con «Format not recognised». Eso se trata como un soundfile que no carga: ``ImportError``.
    Lanza ``ImportError``/``OSError`` si soundfile o libsndfile no se pueden cargar.
    """
    import soundfile as sf  # opcional; importación diferida a propósito

    if "MP3" not in sf.available_formats():
        version = getattr(sf, "__libsndfile_version__", "?")
        raise ImportError(f"libsndfile {version} no decodifica MP3 (hace falta 1.1 o posterior)")
    return sf


def _mp3_a_pcm_soundfile(mp3: bytes) -> tuple[bytes, int]:
    """MP3 completo → ``(PCM s16le mono, frecuencia real)`` con soundfile (libsndfile ≥ 1.1).

    **Bloqueante**: corre en el executor de audio, nunca en el event loop. ``soundfile`` se importa
    aquí a propósito (al importarse carga libsndfile por cffi y numpy). Si el audio trae varios
    canales se mezclan a mono promediándolos. Lanza ``ImportError``/``OSError`` si soundfile o
    libsndfile no se pueden cargar o no decodifican MP3 (``_soundfile_con_mp3``) y
    ``RuntimeError`` (``LibsndfileError``) si el MP3 es ilegible.
    """
    sf = _soundfile_con_mp3()
    datos, sr = sf.read(io.BytesIO(mp3), dtype="int16", always_2d=True)
    if datos.shape[1] > 1:
        mono = datos.mean(axis=1).round()      # la media de int16 siempre cabe en int16
    else:
        mono = datos[:, 0]
    return mono.astype("<i2").tobytes(), int(sr)


async def _bloques_pcm(pcm: bytes, sr: int, ms: int = BLOQUE_PCM_MS) -> AsyncIterator[bytes]:
    """Fuente asíncrona de PCM s16le mono en bloques de ``ms`` (≈ 100 ms) para ``_reproducir_pcm``."""
    tam = max(2, (sr * ms // 1000) * 2)
    for inicio in range(0, len(pcm), tam):
        yield pcm[inicio:inicio + tam]


def _motivo_excepcion(exc: BaseException) -> str:
    """``"Tipo: mensaje"`` (≤ 200 caracteres) para explicar en el log por qué se descarta algo."""
    mensaje = str(exc).strip()
    return f"{type(exc).__name__}: {mensaje}"[:200] if mensaje else type(exc).__name__


def _recoger_resultado(tarea: asyncio.Future[Any]) -> None:
    """*Done callback* de una descarga anticipada que quizá nadie espere: recoge su excepción para
    que el loop no avise de «Task exception was never retrieved»."""
    if not tarea.cancelled():
        tarea.exception()


def _fabricar_reproductor(nombre: str, obtener_ejecutor: Callable[[], ThreadPoolExecutor]) -> _Reproductor:
    """Crea el reproductor PCM ``nombre`` (punto de inyección para los tests)."""
    if nombre == "sounddevice":
        return _ReproductorSoundDevice(obtener_ejecutor())
    return _ReproductorProceso(nombre)


class _ProgresoAudio:
    """Estado compartido entre la escritura de audio y el bucle de la boca."""

    __slots__ = ("t0", "escrito_s", "fin", "abortar", "latencia")

    def __init__(self) -> None:
        self.t0: float | None = None
        self.escrito_s = 0.0
        self.fin = False
        self.abortar = False
        self.latencia = 0.1


# ---------------------------------------------------------------------------
# Despachador
# ---------------------------------------------------------------------------


class RikoMeguminIO:
    """Despachador de salida (§10.19): voz + avatar, terminal, HUD y Telegram."""

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 avatar: SalidaAvatar | None = None, memoria: RegistroLlamadas | None = None,
                 presencia: Callable[[], EstadoPresencia] | None = None,
                 cliente: "httpx.AsyncClient | None" = None,
                 imprimir: Callable[[str, Etiquetas | None], None] | None = None,
                 reloj: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._bus = bus
        self._avatar = avatar
        self._memoria = memoria
        self._presencia = presencia
        self._cliente = cliente
        self._cliente_propio = False
        self._imprimir = imprimir if imprimir is not None else _imprimir_defecto
        self._reloj = reloj

        # Cola única y consumo
        self._cola: asyncio.PriorityQueue[tuple[int, float, str, int, Utterance]] = asyncio.PriorityQueue()
        self._secuencia = itertools.count()
        self._senal: asyncio.Event | None = None
        self._senal_loop: asyncio.AbstractEventLoop | None = None
        self._lock_proceso: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._consumidores = 0
        self._en_curso = 0

        # Estado general
        self._iniciado = False
        self._cerrado = False
        self._silenciado = False
        self._hablando = False
        self._cortar_voz = False
        self._voz_forzada: bool | None = None
        self._ultimo_impreso: str | None = None

        # Compuerta de proactividad
        self._pausa_hasta = 0.0
        self._interrupciones: deque[float] = deque()
        self._proactivos_mem: int | None = None
        self._proactivos_mem_fecha: str | None = None
        self._proactivos_ram: dict[str, int] = {}          # fecha local → iniciativas entregadas
        self._no_entregadas: dict[str, set[int]] = {}      # fecha local → ids registrados sin entregar
        self._no_entregadas_leidas = False
        self._refractario: dict[str, tuple[float, int]] = {}
        self._explosion_fecha_ram: str | None = None
        self._llamadas_ram: dict[int, list[Any]] = {}     # id → [ts, contestada]
        self._siguiente_llamada_ram = -1                  # ids negativos: nunca chocan con SQLite

        # Voz
        self._eleven_desactivada = False
        self._eleven_bloqueada_hasta = 0.0
        self._omitir_eleven_utterance: str | None = None
        self._reproductor_pcm: str | None = None
        self._reproductores_rotos: set[str] = set()
        self._reproductor_mp3: list[str] | None = None
        self._ffmpeg: str | None = None
        self._soundfile = False      # soundfile instalado y utilizable (se descarta si no carga)
        self._fallos_soundfile = 0   # decodificaciones fallidas seguidas (MAX_FALLOS_SOUNDFILE)
        # MP3 de edge-tts del segmento siguiente, descargado mientras suena el actual (texto, tono, lang).
        self._mp3_anticipados: dict[tuple[str, str, str], asyncio.Task[bytes]] = {}
        self._notify_send: str | None = None
        self._reproductor_activo: _Reproductor | None = None
        self._proc_mp3_activo: asyncio.subprocess.Process | None = None
        self._ejecutor_audio: ThreadPoolExecutor | None = None
        self._ultimo_diag_voz: tuple[str, str] | None = None

        # Telegram
        self._telegram_desactivado = False
        self._entrada_telegram_desactivada = False
        self._proximo_envio = 0.0
        self._megumin: dict[str, list[dict[str, str]]] | None = None
        self._transcriptor: Transcriptor | None = None

    # ------------------------------------------------------------------
    # Propiedades públicas
    # ------------------------------------------------------------------

    @property
    def hablando(self) -> bool:
        """``True`` mientras se reproduce audio."""
        return self._hablando

    @property
    def silenciado(self) -> bool:
        """``True`` si la voz está silenciada (``/silencio`` o el botón del HUD)."""
        return self._silenciado

    @property
    def voz_disponible(self) -> bool:
        """Hay motor de voz y reproductor, no está silenciado y ``settings.voz_habilitada``.

        Motor: ElevenLabs activo en la sesión o edge-tts importable con ``tiene_edge_tts``.
        Reproductor: PCM (pw-play/paplay/aplay/sounddevice) o, para MP3, mpv/ffplay. edge-tts
        (MP3) necesita además un decodificador para ir por el reproductor PCM: ffmpeg o soundfile.
        Antes de ``iniciar()`` siempre es ``False``.
        """
        if self._voz_forzada is not None:
            return self._voz_forzada
        if not self._iniciado or self._cerrado or self._silenciado:
            return False
        if not self._settings.voz_habilitada:
            return False
        hay_pcm = self._nombre_reproductor_pcm() is not None
        hay_mp3 = self._reproductor_mp3 is not None
        if self._eleven_activo() and (hay_pcm or hay_mp3):
            return True
        return self._edge_disponible() and self._edge_reproducible()

    @voz_disponible.setter
    def voz_disponible(self, valor: bool | None) -> None:
        """Gancho de pruebas e integración: fuerza el valor (``None`` vuelve al cálculo real).

        Permite ``monkeypatch.setattr(riko, "voz_disponible", True)`` (§10.21, test 8 de main).
        Si la voz forzada no puede sonar, el texto igual llega a la terminal.
        """
        self._voz_forzada = None if valor is None else bool(valor)

    # ------------------------------------------------------------------
    # Arranque y cierre
    # ------------------------------------------------------------------

    async def iniciar(self) -> list[DiagnosticoModulo]:
        """Detecta reproductores y motores, carga Megumin, lee la cuota y diagnostica voz y Telegram.

        Devuelve los diagnósticos con claves ``"voz"`` y ``"telegram"``. La cuota y los dos sondeos
        corren en paralelo y cada uno va acotado, así que el total queda por debajo de los 4 s que
        main concede a ``iniciar`` (§6.4).
        """
        self._detectar_capacidades()
        self._cargar_megumin()
        self._iniciado = True
        _, voz, telegram = await asyncio.gather(
            self._actualizar_cuota_acotada(TIMEOUT_CUOTA_ARRANQUE_S),
            self._diagnostico_acotado(self._diagnostico_voz(), "voz", "Salida de voz"),
            self._diagnostico_acotado(self._diagnostico_telegram(), "telegram",
                                      "Mensajería (Telegram/RINE)"),
        )
        self._ultimo_diag_voz = (voz.estado.value, voz.detalle)
        for diagnostico in (voz, telegram):
            self._publicar("sistema.modulo", diagnostico.a_dict())
        log.info("Salida lista: voz %s (%s), Telegram %s (%s).", voz.estado.value, voz.detalle,
                 telegram.estado.value, telegram.detalle)
        return [voz, telegram]

    async def cerrar(self) -> None:
        """Corta la voz en curso, cierra el cliente HTTP propio y apaga el executor de audio."""
        if self._cerrado:
            return
        self._cerrado = True
        self._cortar_voz = True
        self._cancelar_anticipados()
        reproductor = self._reproductor_activo
        if reproductor is not None:
            try:
                await reproductor.abortar()
            except Exception as exc:
                log.debug("Aborto del reproductor al cerrar: %s", type(exc).__name__)
        proc = self._proc_mp3_activo
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError) as exc:
                log.debug("Cierre del reproductor MP3: %s", type(exc).__name__)
        if self._cliente_propio and self._cliente is not None:
            try:
                await self._cliente.aclose()
            except Exception as exc:
                log.debug("Cierre del cliente HTTP propio: %s", type(exc).__name__)
            self._cliente = None
            self._cliente_propio = False
        if self._ejecutor_audio is not None:
            self._ejecutor_audio.shutdown(wait=False, cancel_futures=True)
            self._ejecutor_audio = None
        pendientes = self._cola.qsize()
        if pendientes:
            log.info("Salida cerrada con %d mensajes sin entregar en la cola.", pendientes)

    # ------------------------------------------------------------------
    # Cola: encolar, bucle y vaciar
    # ------------------------------------------------------------------

    async def encolar(self, u: Utterance) -> None:
        """Pone una ``Utterance`` en la cola (clave ``(-prioridad, creado_ts, id)``)."""
        if not isinstance(u, Utterance):
            log.warning("Se ignoró un elemento de salida que no es una Utterance (%s).",
                        type(u).__name__)
            return
        self._cola.put_nowait((-u.prioridad, u.creado_ts, u.id, next(self._secuencia), u))
        self._senal_actual().set()
        log.debug("Encolada salida %s (origen %s, canal %s, prioridad %d, %d caracteres).",
                  u.id, u.origen, u.canal, u.prioridad, len(u.texto_plano))

    async def decir(self, reply: BrocaReply, *, origen: str = "respuesta", prioridad: int = 0,
                    canal: str = "auto", motivo: str = "", nivel: int | None = None,
                    tipo_iniciativa: str | None = None) -> None:
        """Convierte una ``BrocaReply`` en ``Utterance`` y la encola."""
        if reply is None:
            return
        if isinstance(reply, str):
            reply = BrocaReply.de_texto(reply)
        if not isinstance(reply, BrocaReply):
            log.warning("decir() recibió un %s en lugar de BrocaReply; se ignora.", type(reply).__name__)
            return
        if tipo_iniciativa is None and origen == "iniciativa" and motivo in INICIATIVAS:
            tipo_iniciativa = motivo
        u = Utterance.desde_reply(reply, origen=origen, prioridad=prioridad, canal=canal,
                                  nivel_mensajeria=nivel, motivo=motivo,
                                  tipo_iniciativa=tipo_iniciativa)
        await self.encolar(u)

    async def bucle(self, parar: asyncio.Event, *, drenar_al_parar: bool = True) -> None:
        """Consumidor único de la cola.

        Mientras ``parar`` no está activo, espera elementos (con esperas cortas para ver
        ``parar``). Al activarse, con ``drenar_al_parar`` procesa lo que quede y retorna; sin él,
        retorna de inmediato.
        """
        self._consumidores += 1
        try:
            while True:
                if parar.is_set():
                    if drenar_al_parar:
                        await self._drenar()
                    return
                item = self._sacar()
                if item is None:
                    senal = self._senal_actual()
                    senal.clear()
                    try:
                        await asyncio.wait_for(senal.wait(), ESPERA_COLA_S)
                    except asyncio.TimeoutError:
                        pass  # espera corta normal: vuelve a mirar ``parar``
                    continue
                await self._procesar(item)
        finally:
            self._consumidores -= 1

    async def vaciar(self, timeout: float = 6.0) -> bool:
        """Espera a que la cola quede vacía y sin nada en curso; ``False`` si vence ``timeout``.

        Si no hay ningún ``bucle`` activo, procesa la cola **por sí mismo** con la misma rutina.
        """
        limite = time.monotonic() + max(0.0, float(timeout))
        while True:
            if self._cola.empty() and self._en_curso == 0:
                return True
            restante = limite - time.monotonic()
            if restante <= 0:
                log.warning("La cola de salida no se vació en %.1f s.", timeout)
                return False
            if self._consumidores == 0 and self._en_curso == 0:
                item = self._sacar()
                if item is None:
                    continue
                try:
                    await asyncio.wait_for(self._procesar(item), restante)
                except asyncio.TimeoutError:
                    log.warning("La cola de salida no se vació en %.1f s.", timeout)
                    return False
                continue
            await asyncio.sleep(min(0.02, restante))

    def _sacar(self) -> Utterance | None:
        try:
            return self._cola.get_nowait()[-1]
        except asyncio.QueueEmpty:
            return None

    async def _drenar(self) -> None:
        while True:
            item = self._sacar()
            if item is None:
                return
            await self._procesar(item)

    def _senal_actual(self) -> asyncio.Event:
        """Evento "hay elementos" ligado al loop en curso (se recrea si cambia el loop)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._senal is None or (loop is not None and self._senal_loop is not loop):
            self._senal = asyncio.Event()
            self._senal_loop = loop
        return self._senal

    def _lock_actual(self) -> asyncio.Lock:
        """Cerrojo de procesado ligado al loop en curso: una sola Utterance a la vez."""
        loop = asyncio.get_running_loop()
        if self._lock_proceso is None or self._lock_loop is not loop:
            self._lock_proceso = asyncio.Lock()
            self._lock_loop = loop
        return self._lock_proceso

    # ------------------------------------------------------------------
    # Controles
    # ------------------------------------------------------------------

    def set_silencio(self, silenciado: bool) -> None:
        """Silencia o reactiva la voz. Silenciar corta la frase en curso (el texto ya está en la terminal)."""
        nuevo = bool(silenciado)
        if nuevo == self._silenciado:
            return
        self._silenciado = nuevo
        if nuevo and self._hablando:
            self._cortar_voz = True
        log.info("Voz %s.", "silenciada" if nuevo else "reactivada")

    def pausar_proactividad(self, minutos: float = 30.0) -> None:
        """Pausa la proactividad (``/colgar``, botón *Colgar*). ``minutos ≤ 0`` la reanuda."""
        try:
            minutos = float(minutos)
        except (TypeError, ValueError):
            minutos = 30.0
        if not math.isfinite(minutos) or minutos <= 0:
            self._pausa_hasta = 0.0
            log.info("Proactividad reanudada.")
            return
        self._pausa_hasta = self._ahora() + minutos * 60.0
        log.info("Proactividad en pausa durante %s min.", f"{minutos:g}")

    def puede_proactivo(self, *, canal: str, critico: bool = False,
                        tipo: str | None = None) -> tuple[bool, str]:
        """Compuerta de proactividad (sin LLM). Devuelve ``(permitido, motivo)`` para el canal efectivo.

        - Pausa activa ⇒ no (salvo crítico).
        - ``telegram``: horario silencioso (salvo crítico) o cuota diaria agotada ⇒ no. La cuota
          aplica también a los críticos (§10.19 paso 2 solo los exime de la pausa y del horario).
        - ``voz``/``terminal``: ``descanso`` siempre sí; si no, máximo
          ``max_interrupciones_hora`` en la última hora y ≥ 10 min desde la última (también
          para los críticos).
        """
        ahora = self._ahora()
        canal = self._resolver_canal_compuerta(canal)
        if not critico and ahora < self._pausa_hasta:
            minutos = max(1, int(math.ceil((self._pausa_hasta - ahora) / 60.0)))
            return False, f"proactividad en pausa ({minutos} min restantes)"
        if canal == "telegram":
            if not critico and en_horario_silencioso(self._settings, self._momento_local(ahora)):
                return False, "horario silencioso de Telegram"
            maximo = max(0, int(self._settings.max_proactivos_dia))
            hechos = self._proactivos_hoy(ahora)
            if hechos >= maximo:
                return False, f"cuota diaria de Telegram agotada ({hechos}/{maximo})"
            return True, "aviso crítico" if critico else "permitido"
        # voz o terminal: usuario presente
        if tipo == "descanso":
            return True, "descanso: siempre permitido"
        self._podar_interrupciones(ahora)
        maximo = max(0, int(self._settings.max_interrupciones_hora))
        if len(self._interrupciones) >= maximo:
            return False, f"límite de {maximo} interrupciones por hora"
        if self._interrupciones and ahora - self._interrupciones[-1] < INTERVALO_MIN_INTERRUPCION_S:
            return False, "la última interrupción fue hace menos de 10 min"
        return True, "permitido"

    async def actualizar_cuota(self) -> None:
        """Relee ``contar_proactivos_hoy()`` de la memoria (solo cuenta Telegram).

        Las iniciativas registradas que no llegaron a enviarse (red caída, Telegram rechazó el
        envío) se descuentan en :meth:`_proactivos_hoy`: un corte de red no agota la cuota del día.
        """
        if self._memoria is None:
            return
        if not self._no_entregadas_leidas:
            await self._leer_no_entregadas()
        valor = await self._mem("contar_proactivos_hoy")
        if valor is None:
            return
        try:
            self._proactivos_mem = max(0, int(valor))
        except (TypeError, ValueError):
            log.debug("contar_proactivos_hoy devolvió un valor no numérico.")
            return
        self._proactivos_mem_fecha = self._fecha_local()

    async def _actualizar_cuota_acotada(self, limite: float) -> None:
        """``actualizar_cuota`` con tope de tiempo (arranque): si vence, se relee antes de cada envío."""
        try:
            await asyncio.wait_for(self.actualizar_cuota(), limite)
        except asyncio.TimeoutError:
            log.info("La cuota de Telegram no se leyó en %.1f s; se releerá antes de cada envío.", limite)

    # ------------------------------------------------------------------
    # Procesado de una Utterance
    # ------------------------------------------------------------------

    async def _procesar(self, u: Utterance) -> None:
        """Procesa una Utterance en exclusiva (nunca dos a la vez, aunque coincidan bucle y vaciar)."""
        self._en_curso += 1
        try:
            async with self._lock_actual():
                await self._procesar_una(u)
        finally:
            self._en_curso -= 1

    async def _procesar_una(self, u: Utterance) -> None:
        """Canal efectivo → compuerta → entrega → interrupciones → registro de turno."""
        segs: list[Segmento] = []
        try:
            segs = [s for s in u.segmentos if isinstance(s, Segmento) and s.texto and s.texto.strip()]
            if not segs:
                log.debug("Salida %s sin texto; nada que entregar.", u.id)
                return
            canal = self._canal_efectivo(u)
            if u.origen in ORIGENES_COMPUERTA:
                if canal == "telegram":
                    await self.actualizar_cuota()   # la cuota pudo cambiar desde que main preguntó
                permitido, motivo = self.puede_proactivo(canal=canal, critico=u.prioridad >= 2,
                                                         tipo=u.tipo_iniciativa)
                if not permitido:
                    log.info("Iniciativa descartada: %s", motivo)
                    return
            if canal == "voz":
                canal_usado = await self._entregar_voz(u, segs)
            elif canal == "telegram":
                if u.origen in ORIGENES_PROACTIVOS:
                    enviado = await self._entregar_telegram_proactivo(u, segs)
                else:
                    self._imprimir_utterance(u, segs, "telegram", prefijo="[→ Telegram] ")
                    enviado = await self.enviar_telegram_texto(u.texto_plano)
                    if not enviado:
                        log.warning("No se pudo enviar la respuesta por Telegram; quedó en la terminal.")
                canal_usado = "telegram" if enviado else "terminal"
            else:
                self._imprimir_utterance(u, segs, "terminal")
                canal_usado = "terminal"
            if u.origen in ORIGENES_COMPUERTA and canal in ("voz", "terminal"):
                self._interrupciones.append(self._ahora())
            if u.origen != "respuesta":
                await self._registrar_turno(u, segs, canal_usado)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error al entregar la salida %s; el texto va a la terminal.", u.id)
            if segs and self._ultimo_impreso != u.id:   # nunca se pierde el texto
                self._imprimir_utterance(u, segs, "terminal")

    def _canal_efectivo(self, u: Utterance) -> str:
        """Tabla de canal efectivo de §10.19 (main siempre pasa un canal explícito)."""
        canal = u.canal
        if canal == "auto":
            if self._usuario_presente():
                canal = "voz"
            elif u.origen in ORIGENES_PROACTIVOS and self._telegram_disponible():
                return "telegram"
            else:
                return "terminal"
        if canal == "voz":
            return "voz" if self.voz_disponible else "terminal"
        if canal == "telegram":
            return "telegram" if self._telegram_disponible() else "terminal"
        return "terminal"

    def _resolver_canal_compuerta(self, canal: str) -> str:
        """Canal para la compuerta: ``auto`` se resuelve con la presencia; desconocido = terminal."""
        canal = str(canal or "").strip().lower()
        if canal == "auto":
            if self._usuario_presente():
                return "voz" if self.voz_disponible else "terminal"
            return "telegram" if self._telegram_disponible() else "terminal"
        return canal if canal in ("voz", "terminal", "telegram") else "terminal"

    def _imprimir_utterance(self, u: Utterance, segs: list[Segmento], canal: str, *,
                            prefijo: str = "", subtitulos: bool = True) -> None:
        """Imprime el texto completo en la terminal y publica ``salida.subtitulo`` por segmento."""
        texto = _texto_telegram(segs)
        self._ultimo_impreso = u.id
        if texto:
            linea = prefijo + texto
            try:
                self._imprimir(linea, segs[0].etiquetas)
            except Exception as exc:  # una consola rota nunca debe perder el texto
                log.warning("La impresión de la consola falló (%s); uso stdout.", type(exc).__name__)
                _imprimir_defecto(linea, segs[0].etiquetas)
        log.info("Salida por %s: %d caracteres (origen %s).", canal, len(texto), u.origen)
        if subtitulos:
            for seg in segs:
                self._publicar_subtitulo(seg, canal, u.origen)

    def _publicar_subtitulo(self, seg: Segmento, canal: str, origen: str) -> None:
        self._publicar("salida.subtitulo", {
            "texto": _acotar_caracteres(limpiar_para_telegram(seg.texto), 2000),
            "exp": seg.etiquetas.exp, "motion": seg.etiquetas.motion, "tone": seg.etiquetas.tone,
            "canal": canal, "origen": origen,
        })

    async def _registrar_turno(self, u: Utterance, segs: list[Segmento], canal: str) -> None:
        """Guarda la salida no conversacional como turno de ``amadeus`` (historial de Gemini)."""
        if self._memoria is None:
            return
        await self._mem("registrar_turno", "amadeus", u.texto_plano, canal=canal,
                        etiquetas=segs[0].etiquetas)

    # ------------------------------------------------------------------
    # Voz
    # ------------------------------------------------------------------

    async def _entregar_voz(self, u: Utterance, segs: list[Segmento]) -> str:
        """Imprime el texto completo y habla la parte que cabe en voz (2 oraciones, 45 palabras).

        Los subtítulos del HUD llevan **todos** los segmentos completos. Cada segmento hablado se
        publica cuando su audio empieza de verdad (al abrir el reproductor), no antes de
        sintetizarlo: el puente de VTuber AI Studio mueve los labios del visor en cuanto recibe un
        subtítulo con ``canal == "voz"``, y publicarlo antes adelantaría la boca a la voz (con
        edge-tts sin ffmpeg, todo lo que tarda en llegar el MP3 completo). Al terminar se publican
        los que no se hablaron (recorte de voz, corte por silencio o voz que no sonó); nunca se
        pierden en el HUD. Si no sonó nada y nadie cortó la voz, esos subtítulos salen con
        ``canal == "terminal"``, el canal real de la entrega (el mismo que se guarda en el turno).

        Con edge-tts por MP3 completo (soundfile o mpv/ffplay), el MP3 del segmento siguiente se
        descarga mientras suena el actual, para no dejar entre ellos la espera de la síntesis.
        """
        self._imprimir_utterance(u, segs, "voz", subtitulos=False)
        recortados, recortado = recortar_segmentos(segs, "voz")
        recortados = [s for s in recortados if s.texto.strip()] or segs[:1]
        if recortado:
            log.debug("Texto recortado para voz; el completo quedó en la terminal.")
        if u.origen == "iniciativa":
            await self._notificar_llamada_entrante()
        self._cortar_voz = False
        self._hablando = True
        motor_inicial = ("elevenlabs" if self._eleven_activo()
                         else "edge" if self._edge_disponible() else "texto")
        self._publicar("voz.inicio", {"motor": motor_inicial, "tone": recortados[0].etiquetas.tone,
                                      "utterance_id": u.id})
        inicio = time.monotonic()
        motor = "texto"
        publicados = 0      # segmentos originales ya publicados como subtítulo (en orden)

        def al_empezar(indice: int) -> None:
            """El audio del segmento hablado ``indice`` acaba de empezar (idempotente).

            El recorte conserva un prefijo: el segmento hablado i procede del original i, así que
            se publican en orden los originales hasta ``indice`` (incluidos los anteriores que no
            sonaron). Después se anticipa el MP3 del siguiente segmento.
            """
            nonlocal publicados
            while publicados <= indice and publicados < len(segs):
                self._publicar_subtitulo(segs[publicados], "voz", u.origen)
                publicados += 1
            if indice + 1 < len(recortados):
                self._anticipar_mp3_edge(u, recortados[indice + 1])

        cancelado = False
        try:
            for indice, seg in enumerate(recortados):
                if self._debe_cortar():
                    break
                if indice > 0:
                    await asyncio.sleep(PAUSA_ENTRE_SEGMENTOS_S)
                await self._aplicar_etiquetas_avatar(seg.etiquetas)
                motor_segmento = await self._hablar_segmento(
                    u, seg, al_empezar=functools.partial(al_empezar, indice))
                if motor_segmento != "texto":
                    motor = motor_segmento
        except asyncio.CancelledError:
            cancelado = True
            raise
        finally:
            self._hablando = False
            self._cancelar_anticipados()
            self._publicar("voz.fin", {"utterance_id": u.id,
                                       "duracion_s": round(time.monotonic() - inicio, 3),
                                       "motor": motor})
            # Lo que no se habló también llega al HUD. Si no sonó nada (y no fue un corte ni una
            # cancelación), la entrega real fue por la terminal: así el visor 3D puede leerlo con
            # su propia voz.
            cortado = cancelado or self._debe_cortar()
            canal_resto = "voz" if motor != "texto" or cortado else "terminal"
            for seg in segs[publicados:]:
                self._publicar_subtitulo(seg, canal_resto, u.origen)
        return "voz" if motor != "texto" else "terminal"

    async def _hablar_segmento(self, u: Utterance, seg: Segmento, *,
                               al_empezar: Callable[[], None] | None = None) -> str:
        """Sintetiza y reproduce un segmento. Devuelve el motor usado (``"texto"`` si no sonó).

        ``al_empezar`` se llama cuando el audio empieza de verdad (al abrir el reproductor PCM o
        al lanzar mpv/ffplay); puede llamarse más de una vez si ElevenLabs reintenta.
        """
        texto_voz = limpiar_para_voz(seg.texto)
        if not texto_voz:
            return "texto"
        try:
            if self._eleven_activo() and self._omitir_eleven_utterance != u.id:
                if await self._hablar_eleven(u, seg, texto_voz, al_empezar=al_empezar):
                    return "elevenlabs"
            if self._debe_cortar():
                return "texto"
            if self._edge_disponible():
                duracion = await self._hablar_edge(texto_voz, seg, al_empezar=al_empezar)
                if duracion:
                    return "edge"
        except _ErrorReproductor as exc:
            log.warning("El reproductor de audio falló (%s); el texto quedó en la terminal.", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # un periférico de voz nunca tumba la salida
            log.warning("La voz falló (%s); el texto quedó en la terminal.", type(exc).__name__)
            log.debug("Detalle del fallo de voz.", exc_info=True)
        return "texto"

    async def _hablar_eleven(self, u: Utterance, seg: Segmento, texto_voz: str, *,
                             al_empezar: Callable[[], None] | None = None) -> bool:
        """ElevenLabs: streaming PCM con lip-sync o, sin reproductor PCM, MP3 por mpv/ffplay."""
        tone, lang = seg.etiquetas.tone, seg.lang
        sr = int(self._settings.elevenlabs_sample_rate)
        if self._nombre_reproductor_pcm() is not None:
            async def accion(extras: bool) -> float:
                texto = self._texto_eleven([(tone, texto_voz)], extras)
                return await self._reproducir_pcm(self._eleven_chunks(texto, tone, lang, extras), sr,
                                                  al_empezar=al_empezar)
        elif self._reproductor_mp3 is not None:
            async def accion(extras: bool) -> float:
                texto = self._texto_eleven([(tone, texto_voz)], extras)
                mp3 = await self._eleven_mp3(texto, tone, lang, extras)
                return await self._reproducir_mp3(mp3, len(texto_voz.split()), al_empezar=al_empezar)
        else:
            return False
        resultado = await self._intentar_eleven(accion, utterance_id=u.id)
        return bool(resultado)

    async def _hablar_edge(self, texto_voz: str, seg: Segmento, *,
                           al_empezar: Callable[[], None] | None = None) -> float | None:
        """edge-tts por el mejor camino disponible. Devuelve los segundos sonados o ``None``.

        Orden: ``ffmpeg`` en streaming → PCM (lip-sync RMS) > ``soundfile``: MP3 completo →
        PCM decodificado en el executor de audio → ``_reproducir_pcm`` por bloques de 100 ms
        (mismo lip-sync RMS, ``voz.nivel_boca`` y boca del avatar) > MP3 directo por mpv/ffplay
        (boca simulada). Si soundfile no puede decodificar ese MP3, se reproduce con mpv/ffplay
        cuando existen, sin volver a sintetizar. El MP3 completo puede venir ya descargado
        (``_anticipar_mp3_edge``) y su descarga atiende los cortes. ``al_empezar`` se llama
        cuando el audio empieza de verdad, nunca antes de tener el MP3 y decodificarlo.
        """
        tone, lang = seg.etiquetas.tone, seg.lang
        hay_pcm = self._nombre_reproductor_pcm() is not None
        decodificador = self._decodificador_edge() if hay_pcm else None
        try:
            if decodificador == "ffmpeg":
                duracion = await self._reproducir_pcm(
                    self._decodificar_mp3(self._edge_stream(texto_voz, tone, lang)), SR_EDGE,
                    al_empezar=al_empezar)
                return duracion or None
            if decodificador is None and self._reproductor_mp3 is None:
                return None
            mp3 = await self._esperar_mp3_edge(texto_voz, tone, lang)
            if not mp3 or self._debe_cortar():
                return None
            if decodificador == "soundfile":
                decodificado = await self._decodificar_soundfile(mp3)
                if decodificado is not None:
                    pcm, sr = decodificado
                    return await self._reproducir_pcm(_bloques_pcm(pcm, sr), sr,
                                                      al_empezar=al_empezar) or None
                if self._reproductor_mp3 is None or self._debe_cortar():
                    return None
            return await self._reproducir_mp3(mp3, len(texto_voz.split()),
                                              al_empezar=al_empezar) or None
        except (_ErrorReproductor, asyncio.CancelledError):
            raise
        except Exception as exc:  # NoAudioReceived, WebSocketError, aiohttp, ffmpeg…
            log.info("edge-tts no pudo sintetizar (%s); el texto quedó en la terminal.",
                     type(exc).__name__)
        return None

    async def _notificar_llamada_entrante(self) -> None:
        """``notify-send -a Amadeus "Llamada entrante: Amadeus"`` si existe (iniciativas en presencia)."""
        if not self._notify_send:
            return
        nombre = self._settings.nombre_ia
        try:
            proc = await asyncio.create_subprocess_exec(
                self._notify_send, "-a", nombre, f"Llamada entrante: {nombre}",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            log.debug("notify-send no se pudo lanzar: %s", type(exc).__name__)
            return
        try:
            await asyncio.wait_for(proc.wait(), 3.0)
        except asyncio.TimeoutError:
            log.debug("notify-send tardó demasiado; se cierra.")
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()

    async def _aplicar_etiquetas_avatar(self, etiquetas: Etiquetas) -> None:
        avatar = self._avatar
        if avatar is None or not getattr(avatar, "en_linea", False):
            return
        try:
            await asyncio.wait_for(avatar.aplicar_etiquetas(etiquetas), 2.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("El avatar no aplicó las etiquetas: %s", type(exc).__name__)

    async def _mover_boca(self, valor: float) -> None:
        avatar = self._avatar
        if avatar is None or not getattr(avatar, "en_linea", False):
            return
        try:
            await avatar.boca(valor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("El avatar no movió la boca: %s", type(exc).__name__)

    async def _cerrar_boca_avatar(self) -> None:
        avatar = self._avatar
        if avatar is None or not getattr(avatar, "en_linea", False):
            return
        try:
            await asyncio.wait_for(avatar.cerrar_boca(), 1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("El avatar no cerró la boca: %s", type(exc).__name__)

    def _debe_cortar(self) -> bool:
        return self._cortar_voz or self._silenciado or self._cerrado

    # --- Reproducción PCM con lip-sync -------------------------------

    @staticmethod
    def _avisar_inicio(al_empezar: Callable[[], None] | None) -> None:
        """El audio acaba de empezar: llama a ``al_empezar``. Un fallo suyo nunca corta la voz."""
        if al_empezar is None:
            return
        try:
            al_empezar()
        except Exception:
            log.debug("Fallo del aviso de inicio del audio.", exc_info=True)

    async def _reproducir_pcm(self, fuente: AsyncIterator[bytes], sr: int, *,
                              al_empezar: Callable[[], None] | None = None) -> float:
        """Reproduce PCM s16le mono al ritmo del reloj (tramas de 20 ms, ventaja de 120 ms).

        Abre el reproductor con el primer fragmento (si la fuente falla antes de sonar, la
        excepción sube y el llamador cae al siguiente motor) y en ese momento llama a
        ``al_empezar`` (los subtítulos de voz salen cuando el audio empieza de verdad). Mueve la
        boca a 30 Hz con *attack* 0,6 / *release* 0,25 y publica ``voz.nivel_boca`` a ≤ 15 Hz.
        Devuelve los segundos de audio escritos.
        """
        bytes_trama = max(2, (sr * TRAMA_MS // 1000) * 2)
        niveles: list[float] = []
        progreso = _ProgresoAudio()
        reproductor: _Reproductor | None = None
        tarea_boca: asyncio.Task[None] | None = None
        pendiente = bytearray()
        cortado = False
        try:
            async with contextlib.aclosing(fuente):   # type: ignore[type-var]
                try:
                    async for parte in fuente:
                        if self._debe_cortar():
                            cortado = True
                            break
                        if not parte:
                            continue
                        if reproductor is None:
                            reproductor = await self._abrir_reproductor_pcm(sr)
                            progreso.t0 = time.monotonic()
                            progreso.latencia = reproductor.latencia_s
                            tarea_boca = asyncio.create_task(self._bucle_boca(niveles, progreso))
                            self._avisar_inicio(al_empezar)
                        pendiente += parte
                        while len(pendiente) >= bytes_trama:
                            trama = bytes(pendiente[:bytes_trama])
                            del pendiente[:bytes_trama]
                            await self._escribir_trama(reproductor, trama, niveles, progreso, sr)
                            if self._debe_cortar():
                                cortado = True
                                break
                        if cortado:
                            break
                    if pendiente and reproductor is not None and not cortado:
                        resto = bytes(pendiente[: len(pendiente) - (len(pendiente) % 2)])
                        if resto:
                            await self._escribir_trama(reproductor, resto, niveles, progreso, sr)
                except (_ErrorReproductor, asyncio.CancelledError):
                    cortado = True
                    raise
                except Exception as exc:
                    if progreso.escrito_s <= 0:
                        raise
                    log.warning("La voz se interrumpió a mitad de frase (%s).", type(exc).__name__)
        except _ErrorReproductor:
            if reproductor is not None:
                self._marcar_reproductor_roto(reproductor.nombre)
            raise
        finally:
            progreso.fin = True
            if cortado:
                progreso.abortar = True
            if reproductor is not None:
                try:
                    if cortado:
                        await reproductor.abortar()
                    else:
                        await reproductor.cerrar()
                except Exception as exc:
                    log.debug("Cierre del reproductor: %s", type(exc).__name__)
                if self._reproductor_activo is reproductor:
                    self._reproductor_activo = None
            if tarea_boca is not None:
                restante = max(0.0, progreso.escrito_s - (time.monotonic() - (progreso.t0 or 0.0)))
                try:
                    await asyncio.wait_for(asyncio.shield(tarea_boca), restante + 1.0)
                except asyncio.TimeoutError:
                    progreso.abortar = True
                    try:
                        await asyncio.wait_for(tarea_boca, 1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        tarea_boca.cancel()
                except asyncio.CancelledError:
                    progreso.abortar = True
                    tarea_boca.cancel()
                    raise
        return progreso.escrito_s

    async def _escribir_trama(self, reproductor: _Reproductor, trama: bytes, niveles: list[float],
                              progreso: _ProgresoAudio, sr: int) -> None:
        """Escribe una trama sin adelantarse más de ``VENTAJA_S`` al reloj y anota su nivel."""
        if progreso.t0 is None:
            progreso.t0 = time.monotonic()
        adelanto = progreso.escrito_s - (time.monotonic() - progreso.t0)
        if adelanto > VENTAJA_S:
            await asyncio.sleep(adelanto - VENTAJA_S)
        niveles.append(_nivel_trama(trama))
        await reproductor.escribir(trama)
        progreso.escrito_s += len(trama) / (2.0 * sr)

    async def _abrir_reproductor_pcm(self, sr: int) -> _Reproductor:
        nombre = self._nombre_reproductor_pcm()
        if nombre is None:
            raise _ErrorReproductor("no hay reproductor PCM")
        reproductor = _fabricar_reproductor(nombre, self._obtener_ejecutor_audio)
        try:
            await reproductor.abrir(sr)
        except _ErrorReproductor:
            self._marcar_reproductor_roto(nombre)
            raise
        except Exception as exc:
            self._marcar_reproductor_roto(nombre)
            raise _ErrorReproductor(f"{nombre}: {type(exc).__name__}") from None
        self._reproductor_activo = reproductor
        return reproductor

    async def _bucle_boca(self, niveles: list[float], progreso: _ProgresoAudio) -> None:
        """Boca a 30 Hz siguiendo la envolvente RMS; al terminar, ``cerrar_boca``."""
        suave = 0.0
        ultimo_pub = 0.0
        periodo = 1.0 / FRECUENCIA_BOCA_HZ
        try:
            while not progreso.abortar:
                ahora = time.monotonic()
                posicion = ahora - (progreso.t0 or ahora) - progreso.latencia
                if progreso.fin and posicion > progreso.escrito_s + 0.3:
                    break
                indice = int(posicion * 1000 // TRAMA_MS)
                objetivo = niveles[indice] if 0 <= indice < len(niveles) else 0.0
                alfa = ATAQUE if objetivo > suave else LIBERACION
                suave += alfa * (objetivo - suave)
                await self._mover_boca(suave)
                if ahora - ultimo_pub >= 1.0 / MAX_NIVEL_BOCA_HZ:
                    self._publicar("voz.nivel_boca", {"valor": round(min(1.0, max(0.0, suave)), 3)})
                    ultimo_pub = ahora
                await asyncio.sleep(periodo)
        finally:
            await self._cerrar_boca_avatar()
            self._publicar("voz.nivel_boca", {"valor": 0.0})

    async def _boca_simulada(self, duracion: float, progreso: _ProgresoAudio) -> None:
        """Boca simulada (MP3 por mpv/ffplay): senoide de 4 Hz entre 0,2 y 0,7 durante ``duracion`` s."""
        inicio = time.monotonic()
        ultimo_pub = 0.0
        try:
            while not progreso.abortar:
                t = time.monotonic() - inicio
                if t >= duracion:
                    break
                valor = 0.45 + 0.25 * math.sin(2.0 * math.pi * 4.0 * t)
                await self._mover_boca(valor)
                ahora = time.monotonic()
                if ahora - ultimo_pub >= 1.0 / MAX_NIVEL_BOCA_HZ:
                    self._publicar("voz.nivel_boca", {"valor": round(valor, 3)})
                    ultimo_pub = ahora
                await asyncio.sleep(1.0 / FRECUENCIA_BOCA_HZ)
        finally:
            await self._cerrar_boca_avatar()
            self._publicar("voz.nivel_boca", {"valor": 0.0})

    async def _reproducir_mp3(self, mp3: bytes, palabras: int, *,
                              al_empezar: Callable[[], None] | None = None) -> float:
        """Reproduce un MP3 con mpv/ffplay por stdin y simula la boca ``palabras/2,6`` s.

        ``al_empezar`` se llama en cuanto el reproductor arranca, con el MP3 ya completo.
        """
        argv = self._reproductor_mp3
        if not argv:
            raise _ErrorReproductor("no hay reproductor MP3")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
        except (OSError, ValueError) as exc:
            self._reproductor_mp3 = None
            raise _ErrorReproductor(f"reproductor MP3: {type(exc).__name__}") from None
        self._proc_mp3_activo = proc
        duracion = max(0.6, palabras / PALABRAS_POR_SEGUNDO)
        progreso = _ProgresoAudio()
        boca = asyncio.create_task(self._boca_simulada(duracion, progreso))
        inicio = time.monotonic()
        self._avisar_inicio(al_empezar)
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(mp3)
                    await asyncio.wait_for(proc.stdin.drain(), 10.0)
                    proc.stdin.close()
                except (OSError, asyncio.TimeoutError) as exc:
                    raise _ErrorReproductor(f"reproductor MP3: {type(exc).__name__}") from None
            while proc.returncode is None:
                if self._debe_cortar() or time.monotonic() - inicio > duracion + 20.0:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
                    break
                try:
                    await asyncio.wait_for(proc.wait(), 0.1)
                except asyncio.TimeoutError:
                    continue
        finally:
            progreso.abortar = True
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), 2.0)
            self._proc_mp3_activo = None
            try:
                await asyncio.wait_for(boca, 1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                boca.cancel()
        return time.monotonic() - inicio

    def _nombre_reproductor_pcm(self) -> str | None:
        nombre = self._reproductor_pcm
        if nombre is None or nombre in self._reproductores_rotos:
            return None
        return nombre

    def _marcar_reproductor_roto(self, nombre: str) -> None:
        """Descarta un reproductor que falló y busca el siguiente candidato."""
        if nombre in self._reproductores_rotos:
            return
        self._reproductores_rotos.add(nombre)
        self._reproductor_pcm = _detectar_reproductor_pcm(frozenset(self._reproductores_rotos))
        log.warning("El reproductor %s falló; %s.", nombre,
                    f"pruebo con {self._reproductor_pcm}" if self._reproductor_pcm
                    else "la voz queda solo en texto")
        self._publicar_diagnostico_voz()

    def _obtener_ejecutor_audio(self) -> ThreadPoolExecutor:
        if self._ejecutor_audio is None:
            self._ejecutor_audio = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rams-audio")
        return self._ejecutor_audio

    # --- ElevenLabs -----------------------------------------------------

    def _eleven_activo(self) -> bool:
        """ElevenLabs configurado, con httpx, no desactivado en la sesión ni bloqueado hasta medianoche."""
        return (self._settings.tiene_elevenlabs and httpx is not None and not self._cerrado
                and not self._eleven_desactivada and self._ahora() >= self._eleven_bloqueada_hasta)

    def _cabeceras_eleven(self) -> dict[str, str]:
        return {"xi-api-key": str(self._settings.elevenlabs_api_key or "")}

    def _texto_eleven(self, partes: list[tuple[str, str]], extras: bool) -> str:
        """Texto para ElevenLabs con el tag de audio de cada tono (solo si el modelo los admite)."""
        modelo = self._settings.elevenlabs_model
        trozos: list[str] = []
        for tone, texto in partes:
            tag = tag_audio(tone, modelo) if extras else ""
            trozos.append(f"{tag} {texto}" if tag else texto)
        return " ".join(t for t in trozos if t).strip()

    def _cuerpo_eleven(self, texto: str, tone: str, lang: str, extras: bool) -> dict[str, Any]:
        modelo = self._settings.elevenlabs_model
        ajustes = dict(ajustes_voz(tone, modelo))
        if not extras:
            ajustes.pop("speed", None)
        cuerpo: dict[str, Any] = {"text": texto, "model_id": modelo, "voice_settings": ajustes}
        if modelo == "eleven_flash_v2_5":
            cuerpo["language_code"] = lang if lang in LANG_VALIDOS else "es"
        return cuerpo

    async def _eleven_chunks(self, texto: str, tone: str, lang: str, extras: bool) -> AsyncIterator[bytes]:
        """``POST …/stream?output_format=pcm_{sr}``: fragmentos PCM sin partir muestras de 16 bits."""
        cliente = self._obtener_cliente()
        if cliente is None:
            raise _ErrorEleven(0, "sin_cliente")
        sr = int(self._settings.elevenlabs_sample_rate)
        url = f"{URL_ELEVENLABS}/v1/text-to-speech/{self._settings.elevenlabs_voice_id}/stream"
        async with cliente.stream("POST", url, params={"output_format": f"pcm_{sr}"},
                                  json=self._cuerpo_eleven(texto, tone, lang, extras),
                                  headers=self._cabeceras_eleven(),
                                  timeout=httpx.Timeout(10.0, read=30.0)) as respuesta:
            if respuesta.status_code != 200:
                crudo = await respuesta.aread()
                raise _ErrorEleven(respuesta.status_code, _codigo_error_eleven(crudo))
            resto = b""
            async for parte in respuesta.aiter_bytes():
                if not parte:
                    continue
                datos = resto + parte
                corte = len(datos) - (len(datos) % 2)
                resto = datos[corte:]
                if corte:
                    yield datos[:corte]

    async def _eleven_mp3(self, texto: str, tone: str, lang: str, extras: bool) -> bytes:
        """``POST …/text-to-speech/{voice_id}?output_format=mp3_44100_64`` sin streaming."""
        cliente = self._obtener_cliente()
        if cliente is None:
            raise _ErrorEleven(0, "sin_cliente")
        url = f"{URL_ELEVENLABS}/v1/text-to-speech/{self._settings.elevenlabs_voice_id}"
        respuesta = await cliente.post(url, params={"output_format": FORMATO_NOTA_VOZ},
                                       json=self._cuerpo_eleven(texto, tone, lang, extras),
                                       headers=self._cabeceras_eleven(),
                                       timeout=httpx.Timeout(10.0, read=60.0))
        if respuesta.status_code != 200:
            raise _ErrorEleven(respuesta.status_code, _codigo_error_eleven(respuesta.content))
        return respuesta.content

    async def _intentar_eleven(self, accion: Callable[[bool], Awaitable[Any]], *,
                               utterance_id: str | None = None) -> Any:
        """Ejecuta ``accion(extras)`` aplicando la tabla de errores de ElevenLabs (§10.19).

        Devuelve el resultado, o ``None`` si hay que caer a edge-tts.
        """
        extras = True
        reintentado_429 = False
        reintentado_400 = False
        while True:
            if not self._eleven_activo():
                return None
            try:
                return await accion(extras)
            except _ErrorEleven as exc:
                estado = exc.status
                if estado in (401, 402, 403, 404):
                    self._aplicar_error_eleven(estado, exc.codigo)
                    return None
                if estado == 429:
                    if not reintentado_429:
                        reintentado_429 = True
                        log.info("ElevenLabs pide esperar (429); reintento en 1 s.")
                        await asyncio.sleep(1.0)
                        continue
                    log.info("ElevenLabs sigue limitando (429); uso edge-tts para esta frase.")
                    if utterance_id is not None:
                        self._omitir_eleven_utterance = utterance_id
                    return None
                if estado in (400, 422) and not reintentado_400:
                    reintentado_400 = True
                    extras = False
                    log.info("ElevenLabs rechazó la petición (%d); reintento sin speed ni tag de audio.",
                             estado)
                    continue
                log.info("ElevenLabs respondió %d; uso edge-tts.", estado)
                return None
            except _ERRORES_RED as exc:
                log.info("ElevenLabs sin conexión (%s); uso edge-tts.", type(exc).__name__)
                return None

    def _aplicar_error_eleven(self, estado: int, codigo: str = "", *, publicar: bool = True) -> None:
        """401/403/404 ⇒ desactivado en la sesión; 402 ⇒ desactivado hasta la medianoche local."""
        if estado == 401:
            self._eleven_desactivada = True
            log.warning("ElevenLabs rechazó la clave (401); desactivado durante la sesión, uso edge-tts.")
        elif estado == 402:
            momento = self._momento_local()
            medianoche = (momento + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            self._eleven_bloqueada_hasta = medianoche.timestamp()
            log.warning("ElevenLabs sin créditos (402); desactivado hasta la medianoche, uso edge-tts.")
        elif estado == 403:
            self._eleven_desactivada = True
            log.warning("ElevenLabs denegó el acceso (403: plan sin acceso al modelo o a la voz); "
                        "uso edge-tts durante la sesión.")
        elif estado == 404:
            self._eleven_desactivada = True
            log.error("ElevenLabs no encuentra la voz configurada (404 %s); ElevenLabs desactivado.",
                      codigo or "voice_not_found")
        if publicar:
            self._publicar_diagnostico_voz()

    # --- edge-tts -------------------------------------------------------

    def _edge_disponible(self) -> bool:
        return edge_tts is not None and self._settings.tiene_edge_tts and not self._cerrado

    def _decodificador_edge(self) -> str | None:
        """Decodificador MP3 → PCM para edge-tts: ``"ffmpeg"`` (streaming) > ``"soundfile"``, o ``None``."""
        if self._ffmpeg is not None:
            return "ffmpeg"
        if self._soundfile:
            return "soundfile"
        return None

    def _edge_reproducible(self) -> bool:
        """edge-tts puede sonar: decodificador + reproductor PCM, o el MP3 directo por mpv/ffplay."""
        hay_pcm = self._nombre_reproductor_pcm() is not None
        return (hay_pcm and self._decodificador_edge() is not None) or self._reproductor_mp3 is not None

    def _detalle_edge(self) -> str:
        """Detalle del diagnóstico con edge-tts: voz y camino del audio.

        P. ej. ``"edge-tts es-CO-SalomeNeural · soundfile + sounddevice"``,
        ``"edge-tts es-CO-SalomeNeural · ffmpeg + pw-play"`` o ``"edge-tts es-CO-SalomeNeural · mpv"``.
        """
        pcm = self._nombre_reproductor_pcm()
        decodificador = self._decodificador_edge() if pcm is not None else None
        if decodificador is not None:
            camino = f"{decodificador} + {pcm}"
        elif self._reproductor_mp3:
            camino = _nombre_programa(self._reproductor_mp3[0])
        else:
            camino = "sin reproductor"
        return f"edge-tts {self._settings.edge_tts_voz} · {camino}"

    def _edge_por_mp3_completo(self) -> bool:
        """edge-tts sonará desde el MP3 entero (soundfile o mpv/ffplay), no en streaming por ffmpeg."""
        if not self._edge_disponible() or self._debe_cortar():
            return False
        decodificador = self._decodificador_edge() if self._nombre_reproductor_pcm() is not None else None
        if decodificador == "ffmpeg":
            return False
        return decodificador == "soundfile" or self._reproductor_mp3 is not None

    def _tarea_mp3_edge(self, texto: str, tone: str, lang: str) -> asyncio.Task[bytes]:
        """Descarga del MP3 completo de edge-tts en su propia tarea (acotada a ``TIMEOUT_EDGE_MP3_S``)."""
        tarea = asyncio.create_task(
            asyncio.wait_for(self._edge_mp3(texto, tone, lang), TIMEOUT_EDGE_MP3_S))
        tarea.add_done_callback(_recoger_resultado)
        return tarea

    def _anticipar_mp3_edge(self, u: Utterance, seg: Segmento) -> None:
        """Empieza a descargar el MP3 de ``seg`` mientras suena el segmento actual.

        Solo si edge-tts usará el MP3 completo (soundfile o mpv/ffplay) y el segmento no irá por
        ElevenLabs. Lo recoge ``_esperar_mp3_edge`` y lo que sobra se cancela al acabar la utterance.
        """
        if not self._edge_por_mp3_completo():
            return
        if self._eleven_activo() and self._omitir_eleven_utterance != u.id:
            return
        texto_voz = limpiar_para_voz(seg.texto)
        if not texto_voz:
            return
        clave = (texto_voz, seg.etiquetas.tone, seg.lang)
        if clave not in self._mp3_anticipados:
            self._mp3_anticipados[clave] = self._tarea_mp3_edge(*clave)

    def _cancelar_anticipados(self) -> None:
        """Cancela las descargas anticipadas que nadie recogió (fin de la utterance o cierre)."""
        anticipados, self._mp3_anticipados = self._mp3_anticipados, {}
        for tarea in anticipados.values():
            tarea.cancel()

    async def _esperar_mp3_edge(self, texto: str, tone: str, lang: str) -> bytes | None:
        """MP3 completo de edge-tts (el anticipado, si lo hay). ``None`` si la voz se corta antes.

        La descarga corre en su propia tarea y aquí se atiende un corte (silencio, cierre) cada
        ``INTERVALO_CORTE_S``, sin esperar a que termine la síntesis. Los errores de edge-tts y el
        ``TimeoutError`` de ``TIMEOUT_EDGE_MP3_S`` se propagan al llamador.
        """
        tarea = self._mp3_anticipados.pop((texto, tone, lang), None)
        if tarea is None:
            tarea = self._tarea_mp3_edge(texto, tone, lang)
        try:
            while not tarea.done():
                if self._debe_cortar():
                    tarea.cancel()
                    return None
                await asyncio.wait({tarea}, timeout=INTERVALO_CORTE_S)
        except asyncio.CancelledError:
            tarea.cancel()
            raise
        if tarea.cancelled():
            return None
        return tarea.result()

    def _descartar_soundfile(self, motivo: str, *, publicar: bool = True) -> None:
        """soundfile no sirve en esta sesión: edge-tts pasa a ffmpeg o mpv/ffplay, o a solo texto.

        Con ``publicar`` se publica el diagnóstico nuevo (``voz_disponible`` cambia con él).
        """
        if not self._soundfile:
            return
        self._soundfile = False
        self._fallos_soundfile = 0
        log.warning("soundfile no sirve para la voz (%s); edge-tts %s.", motivo,
                    "usará mpv/ffplay" if self._reproductor_mp3 is not None
                    else "necesita ffmpeg, mpv o ffplay para sonar")
        if publicar:
            self._publicar_diagnostico_voz()

    async def _validar_soundfile(self) -> None:
        """En ``iniciar``: comprueba una vez, en el executor de audio, que soundfile carga y lee MP3.

        Solo cuando soundfile sería el camino de edge-tts (edge-tts disponible, reproductor PCM y
        sin ffmpeg). Si no sirve se descarta antes de la primera frase, así ``voz_disponible`` y el
        diagnóstico de arranque dicen la verdad y ninguna frase se sintetiza para nada. Si tarda
        más de ``TIMEOUT_VALIDAR_SOUNDFILE_S`` queda para la primera decodificación, que repite la
        misma comprobación (``_soundfile_con_mp3``).
        """
        if not (self._soundfile and self._ffmpeg is None and self._edge_disponible()
                and self._nombre_reproductor_pcm() is not None):
            return
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(self._obtener_ejecutor_audio(), _soundfile_con_mp3),
                TIMEOUT_VALIDAR_SOUNDFILE_S)
        except asyncio.TimeoutError:     # antes que OSError: en 3.11+ TimeoutError hereda de OSError
            log.info("soundfile tardó más de %g s en cargar; se comprobará en la primera frase.",
                     TIMEOUT_VALIDAR_SOUNDFILE_S)
        except Exception as exc:  # ImportError, OSError, cffi…: no sirve
            self._descartar_soundfile(_motivo_excepcion(exc), publicar=False)

    async def _decodificar_soundfile(self, mp3: bytes) -> tuple[bytes, int] | None:
        """MP3 completo → ``(PCM s16le mono, sr)`` con soundfile en el executor de audio (no en el loop).

        Si soundfile o libsndfile no cargan o no decodifican MP3 (``ImportError``/``OSError``), se
        descarta durante la sesión y se publica el diagnóstico nuevo. Un MP3 ilegible o una
        decodificación que no termina en ``TIMEOUT_DECODIFICAR_S`` solo pierden ese segmento, pero
        ``MAX_FALLOS_SOUNDFILE`` fallos seguidos lo descartan igual (un decodificador que nunca
        decodifica no es una voz). En esos casos devuelve ``None`` y el llamador cae a mpv/ffplay
        si existen.
        """
        loop = asyncio.get_running_loop()
        try:
            resultado = await asyncio.wait_for(
                loop.run_in_executor(self._obtener_ejecutor_audio(), _mp3_a_pcm_soundfile, mp3),
                TIMEOUT_DECODIFICAR_S)
        except asyncio.TimeoutError:     # antes que OSError: en 3.11+ TimeoutError hereda de OSError
            log.info("soundfile tardó más de %g s en decodificar el MP3 de edge-tts.",
                     TIMEOUT_DECODIFICAR_S)
            self._registrar_fallo_soundfile()
        except (ImportError, OSError) as exc:
            self._descartar_soundfile(_motivo_excepcion(exc))
        except Exception as exc:  # LibsndfileError (RuntimeError): MP3 truncado o ilegible
            log.info("soundfile no pudo decodificar el MP3 de edge-tts (%s).", type(exc).__name__)
            self._registrar_fallo_soundfile()
        else:
            self._fallos_soundfile = 0
            return resultado
        return None

    def _registrar_fallo_soundfile(self) -> None:
        """Cuenta una decodificación fallida; ``MAX_FALLOS_SOUNDFILE`` seguidas descartan soundfile."""
        self._fallos_soundfile += 1
        if self._fallos_soundfile >= MAX_FALLOS_SOUNDFILE:
            self._descartar_soundfile(f"{self._fallos_soundfile} decodificaciones fallidas seguidas")

    def _voz_edge(self, lang: str) -> str:
        return VOCES_EDGE_POR_LANG.get(lang, self._settings.edge_tts_voz)

    async def _edge_stream(self, texto: str, tone: str, lang: str) -> AsyncIterator[bytes]:
        """Fragmentos MP3 (24 kHz) de edge-tts con la prosodia del tono."""
        if edge_tts is None:
            raise RuntimeError("edge-tts no está instalado")
        rate, pitch = prosodia_edge(tone)
        comunicador = edge_tts.Communicate(texto, self._voz_edge(lang), rate=rate, pitch=pitch)
        async for parte in comunicador.stream():
            if isinstance(parte, dict) and parte.get("type") == "audio" and parte.get("data"):
                yield parte["data"]

    async def _edge_mp3(self, texto: str, tone: str, lang: str) -> bytes:
        datos = bytearray()
        async with contextlib.aclosing(self._edge_stream(texto, tone, lang)) as flujo:   # type: ignore[type-var]
            async for parte in flujo:
                datos += parte
        return bytes(datos)

    async def _decodificar_mp3(self, fuente: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """MP3 → PCM s16le mono 24 kHz con ffmpeg; escritura y lectura en tareas concurrentes."""
        if self._ffmpeg is None:
            raise _ErrorReproductor("ffmpeg no disponible")
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
            "-ar", str(SR_EDGE), "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        errores: list[BaseException] = []

        async def _escribir() -> None:
            try:
                async with contextlib.aclosing(fuente):   # type: ignore[type-var]
                    async for parte in fuente:
                        if proc.stdin is None:
                            break
                        proc.stdin.write(parte)
                        await proc.stdin.drain()
            except Exception as exc:  # error de edge-tts o de la tubería: se re-lanza si no hubo audio
                errores.append(exc)
            finally:
                if proc.stdin is not None:
                    with contextlib.suppress(OSError):
                        proc.stdin.close()

        escritor = asyncio.create_task(_escribir())
        producido = 0
        try:
            while proc.stdout is not None:
                datos = await asyncio.wait_for(proc.stdout.read(8192), 30.0)
                if not datos:
                    break
                producido += len(datos)
                yield datos
            await asyncio.wait_for(asyncio.shield(escritor), 5.0)
            if errores and producido == 0:
                raise errores[0]
        finally:
            if not escritor.done():
                escritor.cancel()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(escritor, 2.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 2.0)

    # --- Diagnóstico de voz --------------------------------------------

    async def _diagnostico_voz(self) -> DiagnosticoModulo:
        """``OK (ElevenLabs)`` tras ``GET /v1/voices/{id}`` → 200; si no, edge-tts, sin reproductor u OFF.

        La comprobación de soundfile (``_validar_soundfile``) corre en paralelo con el sondeo de
        ElevenLabs; cada una tiene su tope y ambas caben en el de ``_diagnostico_acotado``.
        """
        nombre = "Salida de voz"
        s = self._settings
        if s.offline:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.OFF, "--offline")
        if not s.voz_habilitada:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.OFF, "deshabilitado")
        hay_pcm = self._nombre_reproductor_pcm() is not None
        hay_mp3 = self._reproductor_mp3 is not None
        if not hay_pcm and not hay_mp3:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.DEGRADADO, "sin reproductor de audio")
        validacion = asyncio.create_task(self._validar_soundfile())
        try:
            sondeo = await self._sondear_elevenlabs(nombre)
            await validacion
        finally:
            if not validacion.done():
                validacion.cancel()
        return sondeo or self._diagnostico_voz_local(sin_verificar=True)

    async def _sondear_elevenlabs(self, nombre: str) -> DiagnosticoModulo | None:
        """``GET /v1/voices/{id}`` (sin coste): ``OK (ElevenLabs)`` si responde 200; si no, ``None``.

        Aplica los errores definitivos (401/402/403/404) sin publicar: el diagnóstico de arranque
        lo publica ``iniciar``.
        """
        s = self._settings
        if self._eleven_activo():
            cliente = self._obtener_cliente()
            inicio = time.monotonic()
            try:
                # El timeout de httpx es por fase (conexión, lectura…): wait_for acota el total.
                respuesta = await asyncio.wait_for(cliente.get(
                    f"{URL_ELEVENLABS}/v1/voices/{s.elevenlabs_voice_id}",
                    headers=self._cabeceras_eleven(), timeout=TIMEOUT_DIAGNOSTICO_S),
                    TIMEOUT_DIAGNOSTICO_S)
                latencia = int((time.monotonic() - inicio) * 1000)
                if respuesta.status_code == 200:
                    return DiagnosticoModulo("voz", nombre, EstadoModulo.OK, "ElevenLabs", latencia)
                log.info("Diagnóstico de ElevenLabs: respuesta %d.", respuesta.status_code)
                codigo = _codigo_error_eleven(respuesta.content)
                if respuesta.status_code in (401, 403) and "permission" in codigo.lower():
                    # CONTRATO: una clave con permisos acotados (sin voices_read) puede sintetizar
                    # aunque no pueda leer la voz; no se desactiva por el sondeo de arranque.
                    log.info("La clave de ElevenLabs no puede leer la voz (%s); se probará al hablar.",
                             codigo)
                elif respuesta.status_code in (401, 402, 403, 404):
                    self._aplicar_error_eleven(respuesta.status_code, codigo, publicar=False)
            except _ERRORES_RED as exc:
                log.info("ElevenLabs no respondió al diagnóstico (%s).", type(exc).__name__)
        return None

    def _diagnostico_voz_local(self, *, sin_verificar: bool = False) -> DiagnosticoModulo:
        """Diagnóstico de voz sin red (para los cambios de estado en tiempo de ejecución)."""
        nombre = "Salida de voz"
        s = self._settings
        if s.offline:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.OFF, "--offline")
        if not s.voz_habilitada:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.OFF, "deshabilitado")
        hay_pcm = self._nombre_reproductor_pcm() is not None
        hay_mp3 = self._reproductor_mp3 is not None
        if not hay_pcm and not hay_mp3:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.DEGRADADO, "sin reproductor de audio")
        if self._eleven_activo() and not sin_verificar:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.OK, "ElevenLabs")
        if self._edge_disponible() and self._edge_reproducible():
            return DiagnosticoModulo("voz", nombre, EstadoModulo.DEGRADADO, self._detalle_edge())
        if self._eleven_activo():
            return DiagnosticoModulo("voz", nombre, EstadoModulo.DEGRADADO, "ElevenLabs sin verificar")
        if edge_tts is not None and s.tiene_edge_tts:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.DEGRADADO,
                                     "edge-tts necesita soundfile, ffmpeg, mpv o ffplay")
        if s.tiene_elevenlabs:
            return DiagnosticoModulo("voz", nombre, EstadoModulo.OFF,
                                     "ElevenLabs desactivado y sin edge-tts")
        return DiagnosticoModulo("voz", nombre, EstadoModulo.OFF, "sin clave")

    def _publicar_diagnostico_voz(self) -> None:
        """Publica ``sistema.modulo`` de la voz si cambió (solo tras ``iniciar``)."""
        if not self._iniciado:
            return
        diagnostico = self._diagnostico_voz_local()
        clave = (diagnostico.estado.value, diagnostico.detalle)
        if clave != self._ultimo_diag_voz:
            self._ultimo_diag_voz = clave
            self._publicar("sistema.modulo", diagnostico.a_dict())

    def _detectar_capacidades(self) -> None:
        """Reproductores PCM/MP3, ffmpeg, soundfile y notify-send, sin abrir dispositivos.

        Binarios con ``shutil.which``; soundfile con ``SOUNDFILE_INSTALADO`` (``find_spec``, sin
        importarlo: se importa y se comprueba que lee MP3 en el executor de audio, durante el
        diagnóstico de ``iniciar`` y en cada decodificación).
        """
        self._fallos_soundfile = 0
        self._reproductor_pcm = _detectar_reproductor_pcm(frozenset(self._reproductores_rotos))
        self._reproductor_mp3 = _argv_reproductor_mp3()
        self._ffmpeg = shutil.which("ffmpeg")
        self._soundfile = bool(SOUNDFILE_INSTALADO)
        self._notify_send = shutil.which("notify-send")
        log.debug("Audio: PCM=%s, MP3=%s, ffmpeg=%s, soundfile=%s.", self._reproductor_pcm,
                  "sí" if self._reproductor_mp3 else "no", "sí" if self._ffmpeg else "no",
                  "sí" if self._soundfile else "no")

    async def _diagnostico_acotado(self, corrutina: Awaitable[DiagnosticoModulo], clave: str,
                                   nombre: str) -> DiagnosticoModulo:
        """Red de seguridad: cada diagnóstico termina en ``TIMEOUT + MARGEN`` (< 4 s de main)."""
        limite = TIMEOUT_DIAGNOSTICO_S + MARGEN_DIAGNOSTICO_S
        try:
            return await asyncio.wait_for(corrutina, limite)
        except asyncio.TimeoutError:
            return DiagnosticoModulo(clave, nombre, EstadoModulo.SIN_CONEXION,
                                     f"sin respuesta en {TIMEOUT_DIAGNOSTICO_S:g} s")
        except Exception as exc:
            log.warning("Diagnóstico de %s fallido: %s", clave, type(exc).__name__)
            return DiagnosticoModulo(clave, nombre, EstadoModulo.DEGRADADO, "error en el diagnóstico")

    # ------------------------------------------------------------------
    # Telegram: envío
    # ------------------------------------------------------------------

    def _telegram_disponible(self) -> bool:
        return (self._settings.tiene_telegram and httpx is not None and not self._cerrado
                and not self._telegram_desactivado)

    def _chat_id_api(self) -> int | str:
        chat = str(self._settings.telegram_chat_id or "")
        try:
            return int(chat)
        except ValueError:
            return chat

    async def _tg(self, metodo: str, *, json_: Mapping[str, Any] | None = None,
                  data: Mapping[str, str] | None = None, files: Mapping[str, Any] | None = None,
                  timeout: Any = 20.0) -> Any:
        """``POST https://api.telegram.org/bot{token}/{metodo}``. Devuelve ``result`` o lanza ``_ErrorTelegram``.

        Métodos sin archivo con ``json=``; con archivo, multipart (``data=`` + ``files=``).
        429 ⇒ espera ``retry_after + 1`` s y reintenta (2 veces como máximo). La URL **nunca** se
        registra ni viaja en las excepciones.
        """
        cliente = self._obtener_cliente()
        if cliente is None:
            raise _ErrorTelegram(None, "httpx no disponible")
        url = f"{URL_TELEGRAM}/bot{self._settings.telegram_bot_token}/{metodo}"
        for intento in range(MAX_REINTENTOS_429 + 1):
            if metodo in METODOS_ENVIO:
                await self._esperar_turno_envio()
            try:
                if files is not None:
                    respuesta = await cliente.post(url, data=dict(data or {}), files=dict(files),
                                                   timeout=timeout)
                else:
                    respuesta = await cliente.post(url, json=dict(json_ or {}), timeout=timeout)
            except _ERRORES_RED as exc:
                raise _ErrorTelegram(None, type(exc).__name__) from None
            try:
                cuerpo = respuesta.json()
            except ValueError:
                cuerpo = None
            if isinstance(cuerpo, dict) and cuerpo.get("ok") is True:
                return cuerpo.get("result")
            codigo = respuesta.status_code
            descripcion = ""
            parametros: Mapping[str, Any] = {}
            if isinstance(cuerpo, dict):
                if isinstance(cuerpo.get("error_code"), int):
                    codigo = cuerpo["error_code"]
                descripcion = str(cuerpo.get("description") or "")[:200]
                if isinstance(cuerpo.get("parameters"), dict):
                    parametros = cuerpo["parameters"]
            if codigo == 429 and intento < MAX_REINTENTOS_429:
                try:
                    espera = int(parametros.get("retry_after", 1))
                except (TypeError, ValueError):
                    espera = 1
                espera = min(max(espera, 0), 300)
                log.warning("Telegram pide esperar %d s (429) en %s; reintento.", espera, metodo)
                await asyncio.sleep(espera + 1)
                continue
            raise _ErrorTelegram(codigo, descripcion)
        raise _ErrorTelegram(429, "demasiadas solicitudes")

    async def _esperar_turno_envio(self) -> None:
        """Mínimo ``INTERVALO_MIN_ENVIO_S`` entre envíos (reserva sin *locks*: el loop es único)."""
        ahora = time.monotonic()
        turno = max(ahora, self._proximo_envio)
        self._proximo_envio = turno + max(0.0, float(INTERVALO_MIN_ENVIO_S))
        if turno > ahora:
            await asyncio.sleep(turno - ahora)

    async def _accion_chat(self, accion: str) -> None:
        """``sendChatAction`` (``typing`` / ``record_voice``); su fallo no impide el envío."""
        try:
            await self._tg("sendChatAction", json_={"chat_id": self._chat_id_api(), "action": accion},
                           timeout=10.0)
        except _ErrorTelegram as exc:
            log.debug("sendChatAction falló: %s", exc)

    async def _enviar_mensaje(self, texto_html: str, *, silencioso: bool = False,
                              teclado: Mapping[str, Any] | None = None) -> bool:
        """``sendMessage`` en HTML (el texto ya va escapado)."""
        cuerpo: dict[str, Any] = {
            "chat_id": self._chat_id_api(), "text": texto_html, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if silencioso:
            cuerpo["disable_notification"] = True
        if teclado is not None:
            cuerpo["reply_markup"] = dict(teclado)
        try:
            await self._tg("sendMessage", json_=cuerpo)
            return True
        except _ErrorTelegram as exc:
            self._registrar_error_telegram("sendMessage", exc)
            return False

    async def _enviar_voz(self, audio: bytes, *, leyenda_html: str = "",
                          silencioso: bool = False) -> bool:
        """``sendVoice`` en multipart con ``files={"voice": ("amadeus.mp3", bytes, "audio/mpeg")}``."""
        await self._accion_chat("record_voice")
        datos = {"chat_id": str(self._settings.telegram_chat_id)}
        if leyenda_html:
            datos["caption"] = leyenda_html
            datos["parse_mode"] = "HTML"
        if silencioso:
            datos["disable_notification"] = "true"
        try:
            await self._tg("sendVoice", data=datos,
                           files={"voice": ("amadeus.mp3", audio, "audio/mpeg")},
                           timeout=httpx.Timeout(15.0, read=60.0, write=60.0))
            return True
        except _ErrorTelegram as exc:
            self._registrar_error_telegram("sendVoice", exc)
            return False

    def _registrar_error_telegram(self, metodo: str, exc: _ErrorTelegram) -> None:
        if exc.codigo in (401, 404):
            if not self._telegram_desactivado:
                log.error("Telegram rechazó el token del bot (%s); Telegram desactivado en la sesión.",
                          exc.codigo)
            self._telegram_desactivado = True
            self._publicar("sistema.modulo", DiagnosticoModulo(
                "telegram", "Mensajería (Telegram/RINE)", EstadoModulo.SIN_CONEXION,
                "token rechazado").a_dict())
        elif exc.codigo is None:
            log.warning("Telegram sin conexión en %s (%s).", metodo, exc.descripcion)
        else:
            log.warning("Telegram %s falló (%s).", metodo, exc)

    async def enviar_telegram_texto(self, texto: str) -> bool:
        """Respuesta conversacional por Telegram (sin Megumin). ``True`` si se envió completa."""
        if not self._telegram_disponible():
            return False
        limpio = limpiar_para_telegram(str(texto or ""))
        if not limpio:
            return False
        await self._accion_chat("typing")
        for parte in _partir_texto(limpio, LIMITE_MENSAJE):
            if not await self._enviar_mensaje(html.escape(parte)):
                return False
        self._publicar("telegram.enviado", {"tipo": "respuesta", "nivel": 0, "texto": limpio[:200]})
        return True

    # --- Multimedia: fotos y notas de voz -------------------------------

    async def enviar_foto_telegram(self, jpeg: bytes, *, leyenda: str = "") -> bool:
        """``sendPhoto`` (multipart) de un JPEG en memoria al ``CHAT_ID``. ``True`` si se envió.

        Mismos límites que el resto de envíos: ritmo mínimo entre envíos, reintento ante 429 y
        desactivación si Telegram rechaza el token. La foto debe pesar ≤ 10 MB (límite de la Bot
        API); la leyenda se limpia y se escapa como un ``sendMessage`` (≤ 1000 caracteres). Los
        bytes nunca se escriben en disco ni en el log.
        """
        if not self._telegram_disponible():
            return False
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg:
            log.info("No hay foto que enviar por Telegram.")
            return False
        if len(jpeg) > LIMITE_FOTO_BYTES:
            log.warning("La foto pesa %d KB (> 10 MB); no se envía por Telegram.", len(jpeg) // 1024)
            return False
        limpio = limpiar_para_telegram(str(leyenda or ""))
        await self._accion_chat("upload_photo")
        datos = {"chat_id": str(self._settings.telegram_chat_id)}
        if limpio:
            datos["caption"] = html.escape(_acotar_caracteres(limpio, LIMITE_LEYENDA))
            datos["parse_mode"] = "HTML"
        try:
            await self._tg("sendPhoto", data=datos,
                           files={"photo": ("amadeus.jpg", bytes(jpeg), "image/jpeg")},
                           timeout=httpx.Timeout(15.0, read=60.0, write=60.0))
        except _ErrorTelegram as exc:
            self._registrar_error_telegram("sendPhoto", exc)
            return False
        self._publicar("telegram.enviado", {"tipo": "foto", "nivel": 0, "texto": limpio[:200]})
        log.info("Telegram: foto enviada (%d KB).", len(jpeg) // 1024)
        return True

    async def enviar_voz_telegram(self, texto: str, *, tone: str = "calido",
                                  transcripcion: bool = True) -> bool:
        """Nota de voz con ``texto`` (ElevenLabs o edge-tts, MP3) por ``sendVoice``. ``True`` si se envió.

        Se sintetiza como las notas de voz de las llamadas (``mp3_44100_64`` de ElevenLabs o el MP3
        de edge-tts), con el texto limpio para voz y acotado a ``LIMITE_TEXTO_VOZ`` caracteres
        (cortando en un fin de oración). Con ``transcripcion`` (por defecto) la nota lleva el texto
        como leyenda, igual que las llamadas; pásalo en ``False`` si el texto ya va en otro mensaje.
        Si la respuesta es larga (la voz se recortó o el texto no cabe en la leyenda), la nota va
        sin leyenda y el texto **completo** sigue en mensajes aparte: nada se pierde en silencio.
        Sin motor de voz o si la síntesis falla devuelve ``False`` (quien llama envía el texto);
        también si la nota salió pero el texto completo no (quien llama lo reintenta).
        """
        if not self._telegram_disponible():
            return False
        limpio = limpiar_para_telegram(str(texto or ""))
        voz_completa = limpiar_para_voz(str(texto or ""))
        texto_voz = _texto_para_voz(voz_completa)
        if not texto_voz:
            return False
        texto_aparte = bool(transcripcion and limpio) and (
            len(voz_completa) > LIMITE_TEXTO_VOZ or len(limpio) > LIMITE_LEYENDA)
        audio = await self._sintetizar_mp3(texto_voz, str(tone or "calido"), "es")
        if not audio:
            log.info("No se pudo sintetizar la nota de voz para Telegram; va solo el texto.")
            return False
        if len(audio) > LIMITE_VOZ_BYTES:
            log.warning("La nota de voz pesa demasiado (%d KB); no se envía.", len(audio) // 1024)
            return False
        leyenda = ""
        if transcripcion and limpio and not texto_aparte:
            leyenda = html.escape(_acotar_caracteres(limpio, LIMITE_LEYENDA))
        if not await self._enviar_voz(audio, leyenda_html=leyenda):
            return False
        self._publicar("telegram.enviado", {"tipo": "audio", "nivel": 0, "texto": limpio[:200]})
        log.info("Telegram: nota de voz enviada (%d KB, %d caracteres).", len(audio) // 1024, len(texto_voz))
        if texto_aparte and not await self.enviar_telegram_texto(limpio):
            log.warning("La nota de voz salió, pero el texto completo de la respuesta no.")
            return False
        return True

    async def _sintetizar_mp3(self, texto_voz: str, tone: str, lang: str) -> bytes | None:
        """MP3 de ``texto_voz``: ElevenLabs (``mp3_44100_64``) o, si no, edge-tts. ``None`` si no hay voz."""
        if self._eleven_activo():
            async def accion(extras: bool) -> bytes:
                return await self._eleven_mp3(self._texto_eleven([(tone, texto_voz)], extras), tone, lang,
                                              extras)
            try:
                audio = await self._intentar_eleven(accion)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # la voz es opcional: el texto va igual
                log.info("ElevenLabs no pudo generar la nota de voz (%s).", type(exc).__name__)
                audio = None
            if audio:
                return bytes(audio)
        if self._edge_disponible():
            try:
                audio = await asyncio.wait_for(self._edge_mp3(texto_voz, tone, lang), TIMEOUT_EDGE_MP3_S)
                if audio:
                    return audio
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("edge-tts no pudo generar la nota de voz (%s).", type(exc).__name__)
        return None

    def set_transcriptor(self, fn: Transcriptor | None) -> None:
        """Función que transcribe las notas de voz entrantes: ``await fn(audio, mime) -> texto | None``.

        ``None`` la quita: entonces a una nota de voz se le responde por texto que no se puede
        escuchar. main inyecta ``broca.transcribir_audio`` si hay Gemini y ``telegram_transcribir``.
        """
        if fn is not None and not callable(fn):
            log.warning("set_transcriptor recibió un %s que no es invocable; se ignora.", type(fn).__name__)
            return
        self._transcriptor = fn
        log.info("Transcripción de notas de voz de Telegram %s.", "activa" if fn is not None else "desactivada")

    # --- Ruta proactiva por niveles ------------------------------------

    async def _entregar_telegram_proactivo(self, u: Utterance, segs: list[Segmento]) -> bool:
        """Llamada de Amadeus (1), insistencia de Megumin (2) o aviso crítico con Explosión (3)."""
        nivel = await self._calcular_nivel(u)
        clave_refractario = u.motivo or "critico"
        if nivel == 3 and not self._refractario_permite(clave_refractario):
            self._imprimir_utterance(u, segs, "terminal")
            log.info("Aviso crítico «%s» no enviado por Telegram: refractario activo (quedó en la terminal).",
                     clave_refractario)
            return False
        self._imprimir_utterance(u, segs, "telegram", prefijo="[→ Telegram] ")
        momento = self._momento_local()
        silencioso = nivel != 3 and en_horario_silencioso(self._settings, momento)
        recorte_tg, _ = recortar_segmentos(segs, "telegram")
        transcripcion = _texto_telegram(recorte_tg) or _texto_telegram(segs)
        llamada_id = await self._registrar_llamada(u, nivel, momento.timestamp())
        await self.actualizar_cuota()   # §10.19 paso 2: la cuota se refresca tras cada registro
        # RAMS_MENSAJERO (CONTRATO de cierre): "hibrido" = tabla de §10.19; "megumin" = Megumin
        # también entrega el nivel 1; "amadeus" = Megumin nunca aparece (todo es llamada de Amadeus).
        mensajero = getattr(self._settings, "mensajero", "hibrido")
        tipo_llamada = "recordatorio" if u.origen == "agenda" else "llamada"
        if nivel == 3 and mensajero == "amadeus":
            aviso = f"Aviso crítico: {self._settings.nombre_ia} ({momento.strftime('%H:%M')})"
            enviado = await self._enviar_nivel1(segs, transcripcion, llamada_id, momento, False, aviso=aviso)
            tipo = "critico"
        elif nivel == 3:
            enviado = await self._enviar_nivel3(segs, transcripcion, llamada_id, momento)
            tipo = "critico"
        elif nivel == 1 and mensajero == "megumin":
            enviado = await self._enviar_megumin("nivel1", transcripcion, llamada_id, silencioso)
            tipo = "megumin"
        elif nivel == 2 and mensajero != "amadeus":
            enviado = await self._enviar_megumin("nivel2", transcripcion, llamada_id, silencioso)
            tipo = "megumin"
        else:
            enviado = await self._enviar_nivel1(segs, transcripcion, llamada_id, momento, silencioso)
            tipo = tipo_llamada
        if not enviado:
            log.warning("No se pudo entregar la llamada por Telegram; el texto quedó en la terminal.")
            await self._anular_llamada(llamada_id, origen=u.origen, fecha=momento.date().isoformat())
            return False
        if nivel == 3:
            self._refractario_anotar(clave_refractario)
        if u.origen == "iniciativa":
            # Lo mismo que cuenta contar_proactivos_hoy() (§10.8): solo iniciativas por Telegram.
            fecha = momento.date().isoformat()
            self._proactivos_ram = {fecha: self._proactivos_ram.get(fecha, 0) + 1}
        self._publicar("telegram.enviado", {"tipo": tipo, "nivel": nivel, "texto": transcripcion[:200]})
        log.info("Telegram: %s de nivel %d enviada (%d caracteres).", tipo, nivel, len(transcripcion))
        return True

    async def _calcular_nivel(self, u: Utterance) -> int:
        """Nivel forzado, o 3 si es crítico, 2 con ≥ 2 llamadas ignoradas, 1 en otro caso."""
        if u.nivel_mensajeria in (1, 2, 3):
            return int(u.nivel_mensajeria)
        if u.prioridad >= 2:
            return 3
        return 2 if await self._llamadas_ignoradas() >= 2 else 1

    async def _enviar_nivel1(self, segs: list[Segmento], transcripcion: str, llamada_id: int,
                             momento: datetime, silencioso: bool, *, aviso: str | None = None) -> bool:
        """Llamada perdida + botones, nota de voz con transcripción (o transcripción como texto)."""
        if aviso is None:
            aviso = f"Llamada perdida: {self._settings.nombre_ia} ({momento.strftime('%H:%M')})"
        await self._accion_chat("typing")
        if not await self._enviar_mensaje(html.escape(aviso), silencioso=silencioso,
                                          teclado=_teclado_llamada(llamada_id)):
            return False
        audio = await self._sintetizar_nota_voz(segs)
        leyenda = html.escape(_acotar_caracteres(transcripcion, LIMITE_LEYENDA))
        if audio and await self._enviar_voz(audio, leyenda_html=leyenda, silencioso=silencioso):
            return True
        if transcripcion:
            await self._accion_chat("typing")
            if not await self._enviar_mensaje(html.escape(_acotar_caracteres(transcripcion, LIMITE_MENSAJE)),
                                              silencioso=silencioso):
                log.warning("La transcripción de la llamada no se pudo enviar.")
        return True

    async def _enviar_megumin(self, categoria: str, mensaje: str, llamada_id: int,
                              silencioso: bool) -> bool:
        """Megumin (solo texto): ``"{apertura}\\n<blockquote>Amadeus: {mensaje}</blockquote>\\n{cierre}"``."""
        plantilla = self._plantilla_megumin(categoria)
        valores = self._valores_plantilla(mensaje)
        apertura = _una_linea(_rellenar(plantilla.get("apertura", ""), valores))
        cierre = _una_linea(_rellenar(plantilla.get("cierre", ""), valores))
        cita = (f"<blockquote>{html.escape(self._settings.nombre_ia)}: "
                f"{html.escape(_acotar_caracteres(mensaje, 3000))}</blockquote>")
        lineas = [html.escape(apertura)] if apertura else []
        lineas.append(cita)
        if cierre:
            lineas.append(html.escape(cierre))
        await self._accion_chat("typing")
        return await self._enviar_mensaje("\n".join(lineas), silencioso=silencioso,
                                          teclado=_teclado_llamada(llamada_id))

    async def _enviar_nivel3(self, segs: list[Segmento], transcripcion: str, llamada_id: int,
                             momento: datetime) -> bool:
        """Crítico: Explosión del día (plantilla ``nivel3`` + nota de voz) o ``nivel3_agotada``."""
        hoy = momento.date().isoformat()
        usada = await self._explosion_usada(hoy)
        categoria = "nivel3_agotada" if usada else "nivel3"
        if not await self._enviar_megumin(categoria, transcripcion, llamada_id, silencioso=False):
            return False
        if not usada:
            await self._marcar_explosion(hoy)
            audio = await self._sintetizar_nota_voz(segs)
            if audio and not await self._enviar_voz(audio):
                log.info("La nota de voz del aviso crítico no se pudo enviar.")
        return True

    async def _sintetizar_nota_voz(self, segs: list[Segmento]) -> bytes | None:
        """MP3 de la nota de voz (≤ 50 palabras): ElevenLabs ``mp3_44100_64`` o edge-tts."""
        if not self._settings.voz_habilitada:
            return None
        nota, _ = recortar_segmentos(segs, "nota_voz")
        partes = [(s, limpiar_para_voz(s.texto)) for s in nota]
        partes = [(s, t) for s, t in partes if t]
        if not partes:
            return None
        tone, lang = partes[0][0].etiquetas.tone, partes[0][0].lang
        if self._eleven_activo():
            tonos = [(s.etiquetas.tone, t) for s, t in partes]

            async def accion(extras: bool) -> bytes:
                return await self._eleven_mp3(self._texto_eleven(tonos, extras), tone, lang, extras)
            try:
                audio = await self._intentar_eleven(accion)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # la nota de voz es opcional: la transcripción va igual
                log.info("ElevenLabs no pudo generar la nota de voz (%s).", type(exc).__name__)
                audio = None
            if audio:
                return bytes(audio)
        if self._edge_disponible():
            try:
                audio = await asyncio.wait_for(
                    self._edge_mp3(" ".join(t for _, t in partes), tone, lang), TIMEOUT_EDGE_MP3_S)
                if audio:
                    return audio
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("edge-tts no pudo generar la nota de voz (%s).", type(exc).__name__)
        return None

    # --- Plantillas de Megumin ------------------------------------------

    def _cargar_megumin(self) -> None:
        """Lee ``persona/frases.json["megumin"]``; lo que falte o sea inválido usa ``MEGUMIN_DEFECTO``."""
        plantillas: dict[str, list[dict[str, str]]] = {
            categoria: [dict(e) for e in entradas] for categoria, entradas in MEGUMIN_DEFECTO.items()}
        ruta = self._settings.persona_dir / "frases.json"
        try:
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("No existe persona/frases.json; Megumin usa sus plantillas por defecto.")
            datos = None
        except (OSError, ValueError) as exc:
            log.warning("persona/frases.json ilegible (%s); Megumin usa sus plantillas por defecto.",
                        type(exc).__name__)
            datos = None
        seccion = datos.get("megumin") if isinstance(datos, dict) else None
        if isinstance(seccion, dict):
            for categoria in MEGUMIN_DEFECTO:
                entradas = seccion.get(categoria)
                if not isinstance(entradas, list):
                    continue
                validas = [{"apertura": str(e.get("apertura", "")), "cierre": str(e.get("cierre", ""))}
                           for e in entradas
                           if isinstance(e, dict) and (e.get("apertura") or e.get("cierre"))]
                if validas:
                    plantillas[categoria] = validas
        self._megumin = plantillas

    def _plantilla_megumin(self, categoria: str) -> dict[str, str]:
        if self._megumin is None:
            self._cargar_megumin()
        entradas = (self._megumin or {}).get(categoria) or list(MEGUMIN_DEFECTO[categoria])
        return dict(random.choice(entradas))

    def _valores_plantilla(self, mensaje: str) -> dict[str, str]:
        s = self._settings
        return {"usuario": s.trato_usuario, "rango": s.rango_lab, "nombre_ia": s.nombre_ia,
                "hora": self._momento_local().strftime("%H:%M"), "mensaje": mensaje}

    # --- Llamadas, cuota, Explosión y refractario ------------------------

    async def _registrar_llamada(self, u: Utterance, nivel: int, ts: float) -> int:
        """``memoria.registrar_llamada(...)``; sin memoria (o si falla), id negativo en RAM.

        ``MemoryEngine`` devuelve 0 si no pudo guardar la fila: ese caso también usa un id de RAM
        (un botón ``dev:0`` no marcaría nada).
        """
        if self._memoria is not None:
            resultado = await self._mem("registrar_llamada", origen=u.origen, canal="telegram",
                                        nivel=nivel, motivo=u.motivo, texto=u.texto_plano)
            if isinstance(resultado, int) and not isinstance(resultado, bool) and resultado > 0:
                return resultado
            log.debug("La memoria no registró la llamada; se usa un id en RAM.")
        llamada_id = self._siguiente_llamada_ram
        self._siguiente_llamada_ram -= 1
        self._llamadas_ram[llamada_id] = [ts, None]
        return llamada_id

    async def _anular_llamada(self, llamada_id: int, *, origen: str, fecha: str) -> None:
        """Una llamada que no llegó a enviarse no cuenta como "ignorada" ni gasta la cuota del día.

        CONTRATO: ``RegistroLlamadas`` (§4.3.7) no permite borrar la fila, así que se marca
        ``contestada=True`` (fuera de ``llamadas_ignoradas``) y, si es una iniciativa, su id se
        anota en la meta ``llamadas_no_entregadas`` para descontarla de ``contar_proactivos_hoy``
        (también tras reiniciar el programa el mismo día).
        """
        if llamada_id < 0 or self._memoria is None:
            self._llamadas_ram.pop(llamada_id, None)
            return
        await self._mem("marcar_llamada", llamada_id, True)
        log.debug("Llamada %d no entregada: se marca para no contarla como ignorada.", llamada_id)
        if origen == "iniciativa":
            await self._anotar_no_entregada(llamada_id, fecha)

    async def _leer_no_entregadas(self) -> None:
        """Carga de la meta las iniciativas de hoy que no llegaron a entregarse."""
        crudo = await self._mem("meta_get", META_NO_ENTREGADAS)
        self._no_entregadas_leidas = True
        if not isinstance(crudo, str) or not crudo.strip():
            return
        try:
            datos = json.loads(crudo)
            fecha = str(datos["fecha"])
            ids = {int(i) for i in datos["ids"]}
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            log.debug("Meta %s ilegible (%s); se ignora.", META_NO_ENTREGADAS, type(exc).__name__)
            return
        if fecha == self._fecha_local():
            self._no_entregadas.setdefault(fecha, set()).update(i for i in ids if i > 0)

    async def _anotar_no_entregada(self, llamada_id: int, fecha: str) -> None:
        """Anota (en RAM y en la meta) una iniciativa registrada que no salió por Telegram."""
        if not self._no_entregadas_leidas:
            await self._leer_no_entregadas()
        ids = self._no_entregadas.get(fecha, set())
        ids.add(int(llamada_id))
        self._no_entregadas = {fecha: ids}          # solo interesa el día en curso
        await self._mem("meta_set", META_NO_ENTREGADAS,
                        json.dumps({"fecha": fecha, "ids": sorted(ids)}))

    async def _marcar_llamada(self, llamada_id: int, contestada: bool) -> None:
        if llamada_id < 0 or self._memoria is None:
            if llamada_id in self._llamadas_ram:
                self._llamadas_ram[llamada_id][1] = contestada
            return
        await self._mem("marcar_llamada", llamada_id, contestada)

    async def _llamadas_ignoradas(self) -> int:
        if self._memoria is not None:
            valor = await self._mem("llamadas_ignoradas")
            if valor is not None:
                try:
                    return max(0, int(valor))
                except (TypeError, ValueError):
                    log.debug("llamadas_ignoradas devolvió un valor no numérico.")
        ahora = self._ahora()
        total = 0
        for ts, contestada in self._llamadas_ram.values():
            if ahora - ts > 24 * 3600:
                continue
            if contestada is False or (contestada is None and ahora - ts > 120 * 60):
                total += 1
        return total

    def _proactivos_hoy(self, ahora: float | None = None) -> int:
        """Iniciativas entregadas hoy por Telegram: cuenta de la memoria menos las no entregadas.

        Sin memoria (o antes de leerla) vale el contador de RAM, que solo suma entregas reales.
        """
        hoy = self._fecha_local(ahora)
        memoria = 0
        if self._proactivos_mem is not None and self._proactivos_mem_fecha == hoy:
            memoria = max(0, self._proactivos_mem - len(self._no_entregadas.get(hoy, ())))
        return max(memoria, self._proactivos_ram.get(hoy, 0))

    async def _explosion_usada(self, hoy: str) -> bool:
        if self._explosion_fecha_ram == hoy:
            return True
        if self._memoria is not None:
            valor = await self._mem("meta_get", "explosion_fecha")
            if isinstance(valor, str) and valor.strip() == hoy:
                self._explosion_fecha_ram = hoy
                return True
        return False

    async def _marcar_explosion(self, hoy: str) -> None:
        self._explosion_fecha_ram = hoy
        if self._memoria is not None:
            await self._mem("meta_set", "explosion_fecha", hoy)

    def _refractario_permite(self, clave: str) -> bool:
        """Escalera 10 → 30 → 60 min desde el envío anterior de la misma clave (se reinicia tras 6 h)."""
        registro = self._refractario.get(clave)
        if registro is None:
            return True
        ultimo, envios = registro
        transcurrido = self._ahora() - ultimo
        if transcurrido >= REINICIO_REFRACTARIO_S:
            return True
        indice = min(max(envios, 1) - 1, len(ESCALERA_REFRACTARIO_CRITICO_S) - 1)
        return transcurrido >= ESCALERA_REFRACTARIO_CRITICO_S[indice]

    def _refractario_anotar(self, clave: str) -> None:
        ahora = self._ahora()
        registro = self._refractario.get(clave)
        if registro is None or ahora - registro[0] >= REINICIO_REFRACTARIO_S:
            self._refractario[clave] = (ahora, 1)
        else:
            self._refractario[clave] = (ahora, registro[1] + 1)

    def _podar_interrupciones(self, ahora: float) -> None:
        while self._interrupciones and ahora - self._interrupciones[0] >= VENTANA_INTERRUPCIONES_S:
            self._interrupciones.popleft()

    # ------------------------------------------------------------------
    # Telegram: entrada
    # ------------------------------------------------------------------

    async def bucle_telegram_entrada(self, parar: asyncio.Event,
                                     manejador: Callable[..., Awaitable[None]]) -> None:
        """*Long polling* de ``getUpdates`` con filtro estricto de remitente (§9.3).

        1. ``getUpdates(offset=-1)`` para saltar el *backlog*; ``offset = último + 1``.
        2. ``getUpdates(timeout=50)`` con ``allowed_updates=["message","callback_query"]``.
        3. Solo chats privados cuyo ``chat.id`` y ``from.id`` sean ``CHAT_ID``.
        4. Texto → ``manejador(texto)``; nota de voz o audio (≤ 2 MB y ≤ 3 min) → ``getFile``,
           descarga en memoria, transcriptor y ``manejador(texto, voz=True)`` (sin transcriptor o
           si falla, se responde por texto que no se pudo escuchar); botones ``dev:``/``no:`` →
           marcar la llamada.
        5. 409 → ``log.error`` y termina; otros errores → espera 5 s y reintenta.

        CONTRATO: ``manejador(texto: str, *, voz: bool = False)``. Un manejador antiguo sin ``voz``
        sigue funcionando (recibe solo el texto).
        """
        if not self._telegram_disponible():
            log.info("Entrada de Telegram desactivada (sin configuración, offline o sin httpx).")
            return
        if self._entrada_telegram_desactivada:
            return
        chat_id = str(self._settings.telegram_chat_id)
        offset: int | None = None
        while not parar.is_set():
            try:
                resultado = await self._esperar_o_parar(
                    self._tg("getUpdates", json_={"offset": -1, "timeout": 0,
                                                  "allowed_updates": list(ACTUALIZACIONES_PERMITIDAS)},
                             timeout=20.0), parar)
            except _ErrorTelegram as exc:
                if exc.codigo == 409:
                    self._conflicto_409()
                    return
                self._registrar_error_telegram("getUpdates", exc)
                if self._telegram_desactivado:
                    return
                await self._dormir(parar, ESPERA_ERROR_TELEGRAM_S)
                continue
            if resultado is _PARADO:
                return
            offset = self._siguiente_offset(resultado, 0)
            break
        log.info("Escuchando Telegram (long polling).")
        while not parar.is_set():
            try:
                resultado = await self._esperar_o_parar(
                    self._tg("getUpdates", json_={"offset": offset, "timeout": LONG_POLL_S,
                                                  "allowed_updates": list(ACTUALIZACIONES_PERMITIDAS)},
                             timeout=httpx.Timeout(15.0, read=LONG_POLL_S + 10.0)), parar)
            except _ErrorTelegram as exc:
                if exc.codigo == 409:
                    self._conflicto_409()
                    return
                self._registrar_error_telegram("getUpdates", exc)
                if self._telegram_desactivado:
                    return
                await self._dormir(parar, ESPERA_ERROR_TELEGRAM_S)
                continue
            if resultado is _PARADO:
                return
            if not isinstance(resultado, list):
                continue
            offset = self._siguiente_offset(resultado, offset or 0)
            for actualizacion in resultado:
                if isinstance(actualizacion, dict):
                    await self._procesar_actualizacion(actualizacion, manejador, chat_id, parar=parar)

    @staticmethod
    def _siguiente_offset(actualizaciones: Any, actual: int) -> int:
        """``max(update_id) + 1`` del lote (o ``actual`` si no hay)."""
        siguiente = actual
        if isinstance(actualizaciones, list):
            for actualizacion in actualizaciones:
                uid = actualizacion.get("update_id") if isinstance(actualizacion, dict) else None
                if isinstance(uid, int) and not isinstance(uid, bool) and uid + 1 > siguiente:
                    siguiente = uid + 1
        return siguiente

    async def _procesar_actualizacion(self, actualizacion: Mapping[str, Any],
                                      manejador: Callable[..., Awaitable[None]], chat_id: str, *,
                                      parar: asyncio.Event | None = None) -> None:
        """Aplica el filtro de remitente y despacha mensajes de texto, notas de voz y botones."""
        mensaje = actualizacion.get("message")
        if isinstance(mensaje, dict):
            chat = mensaje.get("chat") if isinstance(mensaje.get("chat"), dict) else {}
            remitente = mensaje.get("from") if isinstance(mensaje.get("from"), dict) else {}
            if (chat.get("type") == "private" and str(chat.get("id")) == chat_id
                    and str(remitente.get("id")) == chat_id):
                texto = mensaje.get("text")
                if isinstance(texto, str) and texto.strip():
                    log.info("Mensaje de Telegram recibido (%d caracteres).", len(texto))
                    await self._llamar_manejador(manejador, texto)
                elif isinstance(mensaje.get("voice"), dict) or isinstance(mensaje.get("audio"), dict):
                    await self._procesar_audio_entrante(mensaje, manejador, parar)
                else:
                    log.info("Mensaje de Telegram sin texto ignorado.")
            else:
                log.info("Mensaje de Telegram ignorado: chat o remitente no autorizado.")
        consulta = actualizacion.get("callback_query")
        if isinstance(consulta, dict):
            remitente = consulta.get("from") if isinstance(consulta.get("from"), dict) else {}
            mensaje_cb = consulta.get("message") if isinstance(consulta.get("message"), dict) else {}
            chat_cb = mensaje_cb.get("chat") if isinstance(mensaje_cb.get("chat"), dict) else {}
            if str(remitente.get("id")) == chat_id and str(chat_cb.get("id")) == chat_id:
                await self._procesar_boton(consulta, manejador)
            else:
                log.info("Botón de Telegram ignorado: remitente no autorizado.")

    async def _procesar_audio_entrante(self, mensaje: Mapping[str, Any],
                                       manejador: Callable[..., Awaitable[None]],
                                       parar: asyncio.Event | None) -> None:
        """Nota de voz (``voice``) o audio (``audio``) del usuario → texto → ``manejador(texto, voz=True)``.

        Límites: ≤ 2 MB y ≤ 180 s (lo declarado por Telegram y lo que llega de verdad). El audio
        se descarga en memoria, se pasa al transcriptor y se suelta; nunca toca el disco ni el log
        (del texto transcrito solo se registra la longitud). Si no hay transcriptor, el audio es
        demasiado largo o algo falla, se responde por texto en el mismo chat.
        """
        tipo = "voice" if isinstance(mensaje.get("voice"), dict) else "audio"
        datos: Mapping[str, Any] = mensaje[tipo]
        duracion = _entero_no_negativo(datos.get("duration"))
        tam = _entero_no_negativo(datos.get("file_size"))
        log.info("%s de Telegram recibida (%s s, %s KB).",
                 "Nota de voz" if tipo == "voice" else "Nota de audio",
                 "?" if duracion is None else duracion, "?" if tam is None else tam // 1024)
        if self._transcriptor is None:
            await self.enviar_telegram_texto(MENSAJE_AUDIO_SIN_TRANSCRIPTOR)
            return
        if ((duracion is not None and duracion > LIMITE_AUDIO_ENTRANTE_S)
                or (tam is not None and tam > LIMITE_AUDIO_ENTRANTE_BYTES)):
            log.info("Nota de voz demasiado larga; no se descarga.")
            await self.enviar_telegram_texto(MENSAJE_AUDIO_LARGO)
            return
        file_id = datos.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip() or len(file_id) > 256:
            await self.enviar_telegram_texto(MENSAJE_AUDIO_FALLIDO)
            return
        await self._accion_chat("typing")
        trabajo = self._escuchar_audio(file_id, _mime_audio(datos.get("mime_type"), tipo))
        if parar is not None:
            resultado = await self._esperar_o_parar(trabajo, parar)
            if resultado is _PARADO:
                return
        else:
            resultado = await trabajo
        estado, texto = resultado
        if estado == "ok" and texto:
            log.info("Nota de voz transcrita (%d caracteres).", len(texto))
            await self._llamar_manejador(manejador, texto, voz=True)
        elif estado == "largo":
            await self.enviar_telegram_texto(MENSAJE_AUDIO_LARGO)
        elif estado == "sin_transcriptor":
            await self.enviar_telegram_texto(MENSAJE_AUDIO_SIN_TRANSCRIPTOR)
        else:
            await self.enviar_telegram_texto(MENSAJE_AUDIO_FALLIDO)

    async def _escuchar_audio(self, file_id: str, mime: str) -> tuple[str, str | None]:
        """``getFile`` → descarga en memoria → transcriptor. ``(estado, texto)``.

        ``estado`` ∈ {``"ok"``, ``"largo"``, ``"fallo"``, ``"sin_transcriptor"``}. Nunca lanza
        (salvo la cancelación).
        """
        try:
            info = await self._tg("getFile", json_={"file_id": file_id}, timeout=15.0)
        except _ErrorTelegram as exc:
            self._registrar_error_telegram("getFile", exc)
            if exc.codigo == 400 and "too big" in exc.descripcion.lower():
                return "largo", None
            return "fallo", None
        if not isinstance(info, dict):
            return "fallo", None
        tam = _entero_no_negativo(info.get("file_size"))
        if tam is not None and tam > LIMITE_AUDIO_ENTRANTE_BYTES:
            return "largo", None
        ruta = info.get("file_path")
        if not isinstance(ruta, str) or not _RE_RUTA_ARCHIVO.match(ruta) or ".." in ruta:
            log.warning("getFile devolvió una ruta de archivo no válida; se descarta la nota de voz.")
            return "fallo", None
        try:
            audio = await asyncio.wait_for(self._descargar_archivo(ruta, LIMITE_AUDIO_ENTRANTE_BYTES),
                                           TIMEOUT_DESCARGA_S + 5.0)
        except _ArchivoGrande:
            return "largo", None
        except asyncio.TimeoutError:
            log.warning("La descarga de la nota de voz tardó demasiado.")
            return "fallo", None
        except _ErrorTelegram as exc:
            log.warning("No se pudo descargar la nota de voz (%s).", exc)
            return "fallo", None
        if not audio:
            return "fallo", None
        transcriptor = self._transcriptor
        if transcriptor is None:
            return "sin_transcriptor", None
        try:
            texto = await asyncio.wait_for(transcriptor(audio, mime), TIMEOUT_TRANSCRIPCION_S)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("La transcripción de la nota de voz tardó más de %g s.", TIMEOUT_TRANSCRIPCION_S)
            texto = None
        except Exception as exc:  # el transcriptor es de otro módulo: nunca tumba la escucha
            log.warning("El transcriptor de notas de voz falló (%s).", type(exc).__name__)
            texto = None
        finally:
            del audio
        if not isinstance(texto, str) or not texto.strip():
            log.info("La nota de voz no se pudo transcribir.")
            return "fallo", None
        return "ok", texto.strip()

    async def _descargar_archivo(self, ruta: str, limite: int) -> bytes:
        """``GET https://api.telegram.org/file/bot{token}/{ruta}`` en memoria, cortando al pasar ``limite``.

        Lanza ``_ArchivoGrande`` o ``_ErrorTelegram`` (sin la URL: lleva el token).
        """
        cliente = self._obtener_cliente()
        if cliente is None:
            raise _ErrorTelegram(None, "httpx no disponible")
        url = f"{URL_TELEGRAM}/file/bot{self._settings.telegram_bot_token}/{ruta}"
        datos = bytearray()
        try:
            async with cliente.stream("GET", url, timeout=httpx.Timeout(10.0, read=TIMEOUT_DESCARGA_S)) as respuesta:
                if respuesta.status_code != 200:
                    raise _ErrorTelegram(respuesta.status_code, "descarga del archivo")
                declarado = str(respuesta.headers.get("content-length") or "")
                if declarado.isdigit() and int(declarado) > limite:
                    raise _ArchivoGrande()
                async for parte in respuesta.aiter_bytes():
                    datos += parte
                    if len(datos) > limite:
                        raise _ArchivoGrande()
        except _ERRORES_RED as exc:
            raise _ErrorTelegram(None, type(exc).__name__) from None
        return bytes(datos)

    async def _procesar_boton(self, consulta: Mapping[str, Any],
                              manejador: Callable[..., Awaitable[None]]) -> None:
        """``dev:<id>`` devuelve la llamada; ``no:<id>`` la anota como rechazada."""
        id_consulta = consulta.get("id")
        coincidencia = _RE_CALLBACK.match(str(consulta.get("data") or ""))
        if coincidencia is None:
            await self._responder_boton(id_consulta, None)
            log.info("Botón de Telegram desconocido ignorado.")
            return
        accion, llamada_id = coincidencia.group(1), int(coincidencia.group(2))
        contestada = accion == "dev"
        await self._marcar_llamada(llamada_id, contestada)
        await self._responder_boton(id_consulta, "Devolviendo la llamada…" if contestada else "Anotado.")
        self._publicar("telegram.respuesta_llamada", {"llamada_id": llamada_id, "contestada": contestada})
        log.info("Llamada %d %s por Telegram.", llamada_id, "devuelta" if contestada else "rechazada")
        if contestada:
            await self._llamar_manejador(manejador, "/devolver_llamada")

    async def _responder_boton(self, id_consulta: Any, texto: str | None) -> None:
        if id_consulta is None:
            return
        cuerpo: dict[str, Any] = {"callback_query_id": str(id_consulta)}
        if texto:
            cuerpo["text"] = texto
        try:
            await self._tg("answerCallbackQuery", json_=cuerpo, timeout=10.0)
        except _ErrorTelegram as exc:
            log.debug("answerCallbackQuery falló: %s", exc)

    async def _llamar_manejador(self, manejador: Callable[..., Awaitable[None]], texto: str, *,
                                voz: bool = False) -> None:
        """``manejador(texto)`` o, para una nota de voz, ``manejador(texto, voz=True)``.

        Un manejador que no admite ``voz`` (firma antigua) recibe solo el texto.
        """
        try:
            if voz:
                acepta = _acepta_voz(manejador)
                if acepta is None:
                    try:
                        corrutina = manejador(texto, voz=True)
                    except TypeError:
                        corrutina = manejador(texto)
                    await corrutina
                elif acepta:
                    await manejador(texto, voz=True)
                else:
                    await manejador(texto)
            else:
                await manejador(texto)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # un turno fallido nunca detiene la escucha
            log.warning("El manejador de Telegram falló (%s).", type(exc).__name__)
            log.debug("Detalle del fallo del manejador de Telegram.", exc_info=True)

    def _conflicto_409(self) -> None:
        log.error(MENSAJE_409)
        self._entrada_telegram_desactivada = True
        self._publicar("sistema.modulo", DiagnosticoModulo(
            "telegram", "Mensajería (Telegram/RINE)", EstadoModulo.DEGRADADO,
            "entrada desactivada: otro proceso usa el bot (409)").a_dict())

    async def _esperar_o_parar(self, corrutina: Awaitable[Any], parar: asyncio.Event) -> Any:
        """Espera ``corrutina`` o ``parar`` (lo primero); si gana ``parar``, cancela y devuelve ``_PARADO``."""
        tarea = asyncio.ensure_future(corrutina)
        espera = asyncio.ensure_future(parar.wait())
        try:
            await asyncio.wait({tarea, espera}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            tarea.cancel()
            espera.cancel()
            raise
        espera.cancel()
        if tarea.done():
            return tarea.result()
        tarea.cancel()
        await asyncio.wait({tarea}, timeout=2.0)
        if tarea.done() and not tarea.cancelled() and tarea.exception() is not None:
            log.debug("Petición a Telegram abandonada al parar: %s", type(tarea.exception()).__name__)
        return _PARADO

    @staticmethod
    async def _dormir(parar: asyncio.Event, segundos: float) -> None:
        try:
            await asyncio.wait_for(parar.wait(), segundos)
        except asyncio.TimeoutError:
            pass  # pasó el tiempo de espera sin que se pidiera parar

    # --- Diagnóstico de Telegram ---------------------------------------

    async def _diagnostico_telegram(self) -> DiagnosticoModulo:
        """``OK ("@bot")`` tras ``getMe``; ``OFF ("sin clave")``; ``SIN CONEXIÓN`` ante errores de red."""
        nombre = "Mensajería (Telegram/RINE)"
        s = self._settings
        if s.offline:
            return DiagnosticoModulo("telegram", nombre, EstadoModulo.OFF, "--offline")
        if not s.telegram_bot_token or not s.telegram_chat_id:
            return DiagnosticoModulo("telegram", nombre, EstadoModulo.OFF, "sin clave")
        if httpx is None:
            return DiagnosticoModulo("telegram", nombre, EstadoModulo.OFF, "httpx no instalado")
        inicio = time.monotonic()
        try:
            resultado = await asyncio.wait_for(
                self._tg("getMe", json_={}, timeout=TIMEOUT_DIAGNOSTICO_S), TIMEOUT_DIAGNOSTICO_S)
        except asyncio.TimeoutError:
            return DiagnosticoModulo("telegram", nombre, EstadoModulo.SIN_CONEXION,
                                     f"sin respuesta en {TIMEOUT_DIAGNOSTICO_S:g} s")
        except _ErrorTelegram as exc:
            if exc.codigo is None:
                return DiagnosticoModulo("telegram", nombre, EstadoModulo.SIN_CONEXION, "error de red")
            if exc.codigo in (401, 404):
                self._telegram_desactivado = True
                log.error("Telegram rechazó el token del bot (%s); Telegram desactivado en la sesión.",
                          exc.codigo)
                return DiagnosticoModulo("telegram", nombre, EstadoModulo.SIN_CONEXION, "token rechazado")
            return DiagnosticoModulo("telegram", nombre, EstadoModulo.SIN_CONEXION, f"error {exc.codigo}")
        latencia = int((time.monotonic() - inicio) * 1000)
        usuario = resultado.get("username") if isinstance(resultado, dict) else None
        detalle = f"@{usuario}" if usuario else "bot activo"
        return DiagnosticoModulo("telegram", nombre, EstadoModulo.OK, detalle, latencia)

    # ------------------------------------------------------------------
    # Auxiliares generales
    # ------------------------------------------------------------------

    def _obtener_cliente(self) -> "httpx.AsyncClient | None":
        """Cliente compartido; si no se recibió, crea uno propio (se cierra en ``cerrar``)."""
        if self._cliente is None and httpx is not None and not self._cerrado:
            self._cliente = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0),
                                              headers={"User-Agent": "RAMS-BioCore/0.1"})
            self._cliente_propio = True
        return self._cliente

    async def _mem(self, metodo: str, *args: Any, **kwargs: Any) -> Any:
        """Llama a ``memoria.<metodo>`` con timeout; cualquier fallo ⇒ ``None`` con log."""
        if self._memoria is None:
            return None
        funcion = getattr(self._memoria, metodo, None)
        if not callable(funcion):
            # CONTRATO: RegistroLlamadas (§4.3.7) define este método; una memoria incompleta se tolera.
            log.debug("La memoria no implementa %s.", metodo)
            return None
        try:
            return await asyncio.wait_for(funcion(*args, **kwargs), TIMEOUT_MEMORIA_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Memoria: %s falló (%s).", metodo, type(exc).__name__)
            return None

    def _usuario_presente(self) -> bool:
        if self._presencia is None:
            return EstadoPresencia.desconocido().presente
        try:
            return bool(self._presencia().presente)
        except Exception as exc:
            log.debug("Presencia no disponible (%s); se asume presente.", type(exc).__name__)
            return True

    def _ahora(self) -> float:
        try:
            valor = float(self._reloj())
        except Exception as exc:
            log.debug("Reloj inválido (%s); uso time.time().", type(exc).__name__)
            return time.time()
        return valor if math.isfinite(valor) else time.time()

    def _momento_local(self, ahora: float | None = None) -> datetime:
        """Fecha y hora local (America/Bogota) del reloj inyectado."""
        return datetime.fromtimestamp(self._ahora() if ahora is None else ahora,
                                      zona_local(self._settings))

    def _fecha_local(self, ahora: float | None = None) -> str:
        return self._momento_local(ahora).date().isoformat()

    def _publicar(self, nombre: str, datos: Mapping[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publicar(nombre, datos, origen="riko")
        except Exception as exc:  # el bus nunca debe tumbar la salida
            log.debug("No se pudo publicar %s: %s", nombre, type(exc).__name__)
