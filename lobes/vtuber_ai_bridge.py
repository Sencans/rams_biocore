"""Puente con **VTuber AI Studio**: avatar 3D (VRM) controlado por HTTP en ``localhost:3939``.

Es una alternativa (o un complemento) a VTube Studio. A diferencia de ``VTubeBridge``, este
puente **no** se inyecta en el despachador de salida: escucha el bus y traduce lo que ya
publican los demás módulos (§5) a comandos ``POST /api/command`` de la interfaz:

- ``salida.subtitulo``  → expresión mezclada + gesto/pose + ``speak`` (globo y labios). Si R.A.M.S.
  ya está hablando con su propia voz (``canal == "voz"``) el visor anima los labios en silencio;
  si la salida es solo texto (``canal == "terminal"``), el visor puede hablar con la voz del
  navegador (``VTUBER_AI_TTS=1``). Los segmentos que llegan juntos se agrupan en una ``sequence``,
  porque en el visor cada ``speak`` interrumpe al anterior.
- ``homeostasis.estado`` → expresión de reposo (``sugerir_etiquetas``) cuando no está hablando, salvo
  que otro módulo gobierne la cara (``reposo_externo = True``, lo pone ``lobes/corteza_motora.py``).
- ``sistema.apagado``   → reverencia de despedida.

API para otros módulos (la usa la corteza motora): ``en_linea``, ``hablando``,
``expresiones_disponibles``, ``huesos_disponibles``, ``estado_visor``/``posicion``/``ts_estado``
(lo último que informó ``/api/state``) y ``encolar(comando)``, que no bloquea y respeta el orden.

Modo escritorio (v0.2, ``docs/COMPANION.md`` §C2.5 y §C2.10):

- ``visores_escritorio`` (``desktopViewers``: visores ``?desktop=1`` con la ventana visible),
  ``visores_escritorio_total`` (``desktopViewersTotal``: cargados, visibles u ocultos) y
  ``viewer_seq`` (``viewerSeq``: sube con cada conexión SSE), leídos de ``GET /api/state``.
- ``desktop(accion, **campos) -> bool`` encola ``{"type": "desktop", "action": …}`` (siempre **solo**:
  el servidor rechaza un lote que mezcle ``desktop`` con otros tipos). ``False`` si el puente no está
  en línea, no hay overlay de escritorio o la acción no es válida (``sanear_desktop``).
- ``aplicar_tema(...)`` encola ``{"type": "theme", …}``; con ``VTUBER_AI_TEMA=amadeus`` el puente lo
  llama cada vez que cambia ``viewerSeq`` (nuevo visor o recarga; el visor es idempotente).
- ``refrescar()`` pide un sondeo de ``/api/state`` cuanto antes (la corteza lo usa cuando el overlay
  se muestra y el puente aún no lo conoce).
- ``409 {code: "sin_overlay"}`` es un fallo de ese comando (sin cambiar de estado ni los contadores):
  pide un sondeo inmediato y, durante ``VENTANA_SONDEO_RAPIDO_S``, sondea cada ``SONDEO_RAPIDO_S`` hasta
  volver a ver el overlay (suele ser un hueco breve: el overlay reconecta su SSE o se recarga). ``409
  {code: "sin_visores"}`` (o sin ``code``) deja el módulo ``EN ESPERA`` como en la v0.1.

Nunca bloquea el bus: los manejadores solo encolan; un *worker* propio envía en orden. Si el
servidor no corre o no hay ningún visor abierto, el módulo queda ``EN ESPERA`` y descarta los
comandos sin ruido. Solo habla con ``127.0.0.1``/``localhost`` y todas las peticiones llevan
``X-Source: rams`` (los POST, además, ``Content-Type: application/json``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Any, Final, Mapping

try:  # dependencia opcional (§0.5)
    import httpx
except ImportError:  # pragma: no cover - depende del entorno
    httpx = None  # type: ignore[assignment]

from core.bus import BusEventos, Evento
from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo, HomeostaticState

try:
    from core.etiquetas import sugerir_etiquetas
except ImportError:  # pragma: no cover - core.etiquetas es P0
    sugerir_etiquetas = None  # type: ignore[assignment]

log = logging.getLogger("rams.vtuber_ai")

CLAVE: Final = "avatar3d"
NOMBRE: Final = "Avatar 3D (VTuber AI Studio)"

# Vocabulario del visor (lib/protocol.js de VTuber AI Studio).
EMOCIONES_VISOR: Final = ("happy", "angry", "sad", "relaxed", "surprised")
GESTOS_VISOR: Final = frozenset({"wave", "nod", "shake_head", "bow", "tilt_head", "jump", "clap", "shrug",
                                 "cheer", "point", "dance", "laugh", "look_around", "think", "surprise"})
POSES_VISOR: Final = frozenset({"relaxed", "tpose", "apose", "arms_up", "hands_on_hips", "arms_crossed",
                                "thinking", "shy"})
MIRADAS_VISOR: Final = frozenset({"camera", "mouse", "forward", "left", "right", "up", "down", "up_left",
                                  "up_right", "down_left", "down_right", "point", "none"})
MANOS_VISOR: Final = frozenset({"open", "relaxed", "fist", "point", "peace", "thumbs_up"})
CAMARAS_VISOR: Final = frozenset({"face", "bust", "full", "wide"})

# EXP de Amadeus → mezcla de expresiones VRM (pesos 0..1). Vacío = neutral.
EXP_A_MEZCLA: Final[dict[str, dict[str, float]]] = {
    "neutral": {},
    "smug": {"happy": 0.35, "relaxed": 0.45},
    "sonrojo_tsundere": {"angry": 0.35, "happy": 0.30},
    "molesta": {"angry": 0.75},
    "pensativa": {"relaxed": 0.35},
    "sorprendida": {"surprised": 0.90},
    "triste": {"sad": 0.75},
    "feliz": {"happy": 0.85},
    "avergonzada": {"sad": 0.25, "happy": 0.30, "surprised": 0.15},
    "seria": {"angry": 0.20},
    "curiosa": {"surprised": 0.35, "happy": 0.20},
    "aburrida": {"relaxed": 0.60, "sad": 0.20},
    "somnolienta": {"relaxed": 0.90},
}

# MOTION de Amadeus → pasos del visor que acompañan al habla (no se esperan: await=false).
MOTION_A_PASOS: Final[dict[str, tuple[dict[str, Any], ...]]] = {
    "idle": (),
    "asentir": ({"type": "gesture", "name": "nod"},),
    "negar": ({"type": "gesture", "name": "shake_head"},),
    "ladear_cabeza": ({"type": "gesture", "name": "tilt_head", "side": "right"},),
    "brazos_cruzados": ({"type": "pose", "name": "arms_crossed"},),
    "mirar_a_otro_lado": ({"type": "look", "at": "right", "head": 0.6},),
    "facepalm": ({"type": "pose", "name": "thinking"}, {"type": "gesture", "name": "shake_head"}),
    "senalar": ({"type": "gesture", "name": "point", "side": "right"},),
    "suspirar": ({"type": "gesture", "name": "shrug"},),
    "bostezar": ({"type": "look", "at": "up", "head": 0.5},),
    "estirarse": ({"type": "pose", "name": "arms_up"},),
}

# TONE → prosodia de la voz del navegador (rate 0.5–2, pitch 0–2).
TONO_A_PROSODIA: Final[dict[str, tuple[float, float]]] = {
    "sarcastico": (1.00, 1.00), "analitico": (1.05, 1.00), "calido": (0.95, 1.10),
    "nervioso": (1.15, 1.20), "serio": (0.95, 0.90), "jugueton": (1.10, 1.15),
    "somnoliento": (0.85, 0.90),
}

# Modo escritorio (lib/protocol.js, comando "desktop"): show/hide/enter/leave NO existen; la
# visibilidad y la entrada/salida por el borde pertenecen al main de Electron (§C2.5.3).
ACCIONES_DESKTOP: Final = ("walk_to", "peek", "chase_cursor", "seek_user", "wander", "stop")
BORDES_DESKTOP: Final = ("left", "right")
DESKTOP_SEGUNDOS_MAX: Final = 15.0
DESKTOP_VELOCIDAD: Final = (0.3, 3.0)
DESKTOP_PX_MAX: Final = 10000.0
# Tema visual (comando "theme"): "amadeus" = look de Kurisu; "none" lo quita.
TEMAS_VISOR: Final = ("amadeus", "none")
TEMA_SCANLINES_MAX: Final = 0.2
CABECERAS: Final[dict[str, str]] = {"X-Source": "rams"}

VENTANA_AGRUPAR_S: Final = 0.25        # segmentos de una misma respuesta llegan casi juntos
INTERVALO_EN_LINEA_S: Final = 15.0
INTERVALO_FUERA_S: Final = 5.0
MIN_ENTRE_SONDEOS_S: Final = 1.0      # refrescar() no sondea más de una vez por segundo
SONDEO_RAPIDO_S: Final = 2.0          # tras un 409 sin_overlay sondea cada 2 s…
VENTANA_SONDEO_RAPIDO_S: Final = 20.0  # …durante 20 s o hasta volver a ver el overlay
MIN_ENTRE_REPOSO_S: Final = 8.0       # expresión de reposo como mucho cada 8 s
CPS_VISOR: Final = 14.0               # caracteres/segundo que estima el visor para los labios
MAX_COLA: Final = 32
MAX_TEXTO: Final = 1900               # el visor rechaza > 2000 caracteres
_HOSTS_LOCALES: Final = ("127.0.0.1", "localhost", "[::1]")
_RE_URL_LOCAL: Final = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?/?$")


def url_es_local(url: str) -> bool:
    """``True`` si la URL apunta a esta máquina (el visor solo acepta Host local)."""
    return bool(_RE_URL_LOCAL.match((url or "").strip()))


def mezcla_para(exp: str, disponibles: frozenset[str] | None = None) -> dict[str, float]:
    """Mezcla de expresiones para un EXP, filtrada a las que tiene el modelo cargado."""
    mezcla = dict(EXP_A_MEZCLA.get(exp, {}))
    if disponibles:
        mezcla = {n: v for n, v in mezcla.items() if n in disponibles}
    return mezcla


def comando_expresion(exp: str, disponibles: frozenset[str] | None = None, *,
                      duracion: float = 0.4) -> dict[str, Any]:
    """Comando ``expression`` del visor para un EXP de Amadeus (neutral si no hay mezcla posible)."""
    mezcla = mezcla_para(exp, disponibles)
    if not mezcla:
        return {"type": "expression", "name": "neutral", "duration": duracion}
    return {"type": "expression", "values": mezcla, "duration": duracion}


def pasos_motion(motion: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(pasos durante el habla, pasos para deshacer después) de un MOTION."""
    durante: list[dict[str, Any]] = []
    deshacer: list[dict[str, Any]] = []
    for paso in MOTION_A_PASOS.get(motion, ()):
        tipo, nombre = paso.get("type"), paso.get("name")
        if tipo == "gesture" and nombre not in GESTOS_VISOR:
            continue
        if tipo == "pose" and nombre not in POSES_VISOR:
            continue
        durante.append({**paso, "await": False})
        if tipo == "pose" and {"type": "pose", "name": "relaxed"} not in deshacer:
            deshacer.append({"type": "pose", "name": "relaxed"})
        if tipo == "look" and {"type": "look", "at": "camera"} not in deshacer:
            deshacer.append({"type": "look", "at": "camera"})
    return durante, deshacer


def duracion_estimada(texto: str, rate: float = 1.0) -> float:
    """Segundos que el visor tardará en "decir" el texto (misma fórmula que su lip-sync)."""
    return max(1.0, len(texto) / (CPS_VISOR * max(0.5, rate)) + 0.3)


def _numero(valor: Any) -> float | None:
    """Número finito o ``None`` (``bool`` no cuenta como número)."""
    if isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _entero_no_negativo(valor: Any) -> int:
    """Entero ≥ 0 a partir de un campo de ``/api/state`` (0 si falta o no es numérico)."""
    numero = _numero(valor)
    return int(numero) if numero is not None and numero > 0 else 0


def sanear_desktop(cmd: Any) -> dict[str, Any] | None:
    """Valida un comando ``desktop`` contra el protocolo del visor; ``None`` si no es válido.

    - ``action`` ∈ ``ACCIONES_DESKTOP`` (``show``/``hide``/``enter``/``leave`` no existen).
    - ``x``: fracción del ancho en [0, 1], o píxeles en [0, 10000] con ``unit: "px"`` (otra
      ``unit`` invalida el comando); obligatoria en ``walk_to``. ``unit`` sin ``x`` se descarta.
    - ``edge`` ∈ {left, right}; ``seconds`` en (0, 15]; ``speed`` en [0,3; 3].

    Un campo conocido fuera de rango invalida el comando (no se acota: es un error del emisor);
    las claves desconocidas se descartan. El resultado es ``{"type": "desktop", "action": …}`` más
    los campos válidos, en ese orden.
    """
    if not isinstance(cmd, Mapping) or cmd.get("type", "desktop") != "desktop":
        return None
    accion = cmd.get("action")
    if accion not in ACCIONES_DESKTOP:
        return None
    salida: dict[str, Any] = {"type": "desktop", "action": accion}
    if "x" in cmd:
        x = _numero(cmd.get("x"))
        unidad = cmd.get("unit")
        if x is None or unidad not in (None, "px"):
            return None
        maximo = DESKTOP_PX_MAX if unidad == "px" else 1.0
        if not 0.0 <= x <= maximo:
            return None
        salida["x"] = round(x, 1) if unidad == "px" else round(x, 4)
        if unidad == "px":
            salida["unit"] = "px"
    elif accion == "walk_to":
        return None
    if "edge" in cmd:
        if cmd.get("edge") not in BORDES_DESKTOP:
            return None
        salida["edge"] = cmd["edge"]
    if "seconds" in cmd:
        segundos = _numero(cmd.get("seconds"))
        if segundos is None or not 0.0 < segundos <= DESKTOP_SEGUNDOS_MAX:
            return None
        salida["seconds"] = round(segundos, 2)
    if "speed" in cmd:
        velocidad = _numero(cmd.get("speed"))
        if velocidad is None or not DESKTOP_VELOCIDAD[0] <= velocidad <= DESKTOP_VELOCIDAD[1]:
            return None
        salida["speed"] = round(velocidad, 2)
    return salida


def comando_tema(nombre: str = "amadeus", *, rim: bool = True, scanlines: float = 0.07) -> dict[str, Any] | None:
    """Comando ``theme`` del visor (``None`` si el tema no existe); ``scanlines`` se acota a [0, 0,2]."""
    if nombre not in TEMAS_VISOR:
        return None
    lineas = _numero(scanlines)
    lineas = 0.07 if lineas is None else min(max(lineas, 0.0), TEMA_SCANLINES_MAX)
    return {"type": "theme", "name": nombre, "rim": bool(rim), "scanlines": round(lineas, 3)}


class VTuberAIBridge:
    """Traduce los eventos del bus de R.A.M.S. a comandos HTTP de VTuber AI Studio."""

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *, cliente: Any = None) -> None:
        self.settings = settings
        self.bus = bus
        self.url = str(getattr(settings, "vtuber_ai_url", "http://127.0.0.1:3939")).rstrip("/")
        self.tts_navegador = bool(getattr(settings, "vtuber_ai_tts", True))
        self.lang = str(getattr(settings, "vtuber_ai_lang", "es-MX"))
        self.voz = str(getattr(settings, "vtuber_ai_voz", "female"))
        # Si otro módulo (lobes/corteza_motora.py) gobierna la cara en reposo, el puente no la toca.
        self.reposo_externo = False
        self.en_linea = False
        self.visores = 0
        self.tema = str(getattr(settings, "vtuber_ai_tema", "amadeus"))
        self._visores_escritorio = 0
        self._visores_escritorio_total = 0
        self._viewer_seq: int | None = None           # None = el servidor no informa viewerSeq
        self._seq_tema: int | None = None             # viewerSeq para el que ya se envió el tema
        self._despertar: asyncio.Event | None = None  # refrescar() → sondeo inmediato (lo crea bucle)
        self._sondeo_rapido_hasta = 0.0               # monotonic: sondeo rápido tras un 409 sin_overlay
        self.modelo: str | None = None
        self._disponibles: frozenset[str] | None = None
        self._huesos: frozenset[str] | None = None
        self._actual: dict[str, Any] | None = None   # "current" de /api/state (pose, posición, cámara…)
        self._ts_estado: float | None = None          # monotonic del último /api/state leído
        self._cliente = cliente
        self._cliente_propio = False
        self._estado = EstadoModulo.EN_ESPERA
        self._detalle = "esperando a VTuber AI Studio"
        self._cola: asyncio.Queue[Any] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._pendientes: list[dict[str, Any]] = []
        self._flush: asyncio.TimerHandle | None = None
        self._hablando_hasta = 0.0
        self._exp_reposo: str | None = None
        self._ultimo_reposo = 0.0
        self._cerrado = False
        self._desuscribir: list[Any] = []
        if not url_es_local(self.url):
            self._estado, self._detalle = EstadoModulo.OFF, "URL no local (solo se permite 127.0.0.1/localhost)"
            log.warning("VTUBER_AI_URL debe apuntar a esta máquina; puente desactivado")
            return
        if bus is not None:
            self._desuscribir = [
                bus.suscribir("salida.subtitulo", self._al_subtitulo),
                bus.suscribir("homeostasis.estado", self._al_homeostasis),
                bus.suscribir("sistema.apagado", self._al_apagado),
            ]

    # ------------------------------------------------------------------ diagnóstico
    def diagnostico(self) -> DiagnosticoModulo:
        """Estado actual para el arranque y ``sistema.modulo``."""
        return DiagnosticoModulo(CLAVE, NOMBRE, self._estado, self._detalle)

    def _cambiar_estado(self, estado: EstadoModulo, detalle: str) -> None:
        if (estado, detalle) == (self._estado, self._detalle):
            return
        self._estado, self._detalle = estado, detalle
        self.en_linea = estado == EstadoModulo.OK
        if self.bus is not None:
            self.bus.publicar("sistema.modulo", self.diagnostico().a_dict(), origen="vtuber_ai")
        log.info("Avatar 3D: %s (%s)", estado.value, detalle)

    # ------------------------------------------------------------------ HTTP
    def _http(self) -> Any:
        if httpx is None:
            return None
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=1.5))
            self._cliente_propio = True
        return self._cliente

    async def consultar_estado(self) -> bool:
        """``GET /api/state``: actualiza visores, modelo y expresiones disponibles."""
        cliente = self._http()
        if cliente is None:
            self._cambiar_estado(EstadoModulo.OFF, "falta httpx")
            return False
        try:
            r = await cliente.get(f"{self.url}/api/state", headers=CABECERAS, timeout=3.0)
            datos = r.json() if r.status_code == 200 else {}
        except Exception as exc:  # conexión rechazada, timeout, JSON inválido…
            log.debug("VTuber AI Studio no responde: %s", type(exc).__name__)
            self._cambiar_estado(EstadoModulo.EN_ESPERA, "VTuber AI Studio no está corriendo")
            return False
        if not isinstance(datos, Mapping):
            datos = {}
        self.visores = _entero_no_negativo(datos.get("viewers"))
        # Modo escritorio (§C2.5.2): un servidor sin estos campos es de la v0.1 (sin overlay ni tema).
        self._visores_escritorio = _entero_no_negativo(datos.get("desktopViewers"))
        self._visores_escritorio_total = _entero_no_negativo(datos.get("desktopViewersTotal"))
        if self._visores_escritorio_total > 0:                # el overlay está: vuelve el ritmo normal
            self._sondeo_rapido_hasta = 0.0
        self._viewer_seq = _entero_no_negativo(datos["viewerSeq"]) if datos.get("viewerSeq") is not None else None
        self.modelo = datos.get("model") if isinstance(datos.get("model"), str) else None
        exprs = datos.get("expressionsAvailable")
        if isinstance(exprs, list):
            self._disponibles = frozenset(str(e) for e in exprs)
        huesos = datos.get("bonesAvailable")
        if isinstance(huesos, list):
            self._huesos = frozenset(str(h) for h in huesos)
        actual = datos.get("current")
        if isinstance(actual, Mapping):
            self._actual = dict(actual)
            self._ts_estado = time.monotonic()
        if self.visores <= 0:
            self._cambiar_estado(EstadoModulo.EN_ESPERA, f"servidor OK, abre el visor en {self.url}")
            return False
        if not datos.get("ready", True):
            self._cambiar_estado(EstadoModulo.EN_ESPERA, "el visor está cargando el modelo")
            return False
        self._quizas_tema()
        modelo = f" · {self.modelo}" if self.modelo else ""
        escritorio = " · overlay de escritorio" if self._visores_escritorio_total else ""
        self._cambiar_estado(EstadoModulo.OK, f"{self.visores} visor(es){modelo}{escritorio}")
        return True

    def _quizas_tema(self) -> None:
        """Con ``VTUBER_AI_TEMA=amadeus``, envía el tema cuando ``viewerSeq`` cambia entre dos consultas
        (nuevo visor o recarga). La primera consulta con visores cuenta como cambio (de desconocido a
        conocido): así un panel abierto antes de arrancar R.A.M.S. también recibe el tema; el visor es
        idempotente. Con ``none``, o si el servidor no informa ``viewerSeq`` (v0.1), nunca."""
        if self.tema != "amadeus" or self._viewer_seq is None or self._viewer_seq == self._seq_tema:
            return
        self._seq_tema = self._viewer_seq
        self.aplicar_tema()

    async def enviar(self, comando: Mapping[str, Any] | list[Any]) -> bool:
        """``POST /api/command``. Devuelve ``True`` si el visor lo aceptó."""
        cliente = self._http()
        if cliente is None:
            return False
        try:
            # json= fija Content-Type: application/json (el servidor lo exige en todo POST).
            r = await cliente.post(f"{self.url}/api/command", json=comando, headers=CABECERAS, timeout=5.0)
        except Exception as exc:
            log.debug("No se pudo enviar al avatar 3D: %s", type(exc).__name__)
            self._cambiar_estado(EstadoModulo.EN_ESPERA, "VTuber AI Studio no está corriendo")
            return False
        try:
            datos = r.json()
        except ValueError:
            datos = {}
        if not isinstance(datos, Mapping):
            datos = {}
        if r.status_code == 409:
            if datos.get("code") == "sin_overlay":
                # Lote desktop sin overlay de escritorio: falla SOLO este comando (el puente sigue en línea
                # y los contadores no se tocan). Lo decide el próximo GET /api/state, que se pide ya: si
                # el overlay está reconectando su SSE, el sondeo rápido lo vuelve a ver en ≤ 2 s.
                self._sondeo_rapido_hasta = time.monotonic() + VENTANA_SONDEO_RAPIDO_S
                self.refrescar()
                log.debug("Sin overlay de escritorio abierto: comando desktop descartado")
                return False
            # "sin_visores" (o un servidor de la v0.1 sin code): no hay ningún visor abierto.
            self._cambiar_estado(EstadoModulo.EN_ESPERA, f"servidor OK, abre el visor en {self.url}")
            return False
        ok = r.status_code == 200 and bool(datos.get("ok", True))
        if not ok:
            log.debug("El avatar 3D rechazó el comando (%s%s): %s", r.status_code,
                      f", {datos.get('code')}" if datos.get("code") else "", str(datos.get("error") or datos)[:200])
        return ok

    # ------------------------------------------------------------------ API para otros módulos
    @property
    def hablando(self) -> bool:
        """``True`` mientras el visor sigue "diciendo" la última respuesta (estimado)."""
        return time.monotonic() < self._hablando_hasta

    @property
    def expresiones_disponibles(self) -> frozenset[str] | None:
        """Expresiones del modelo cargado (``None`` hasta el primer sondeo)."""
        return self._disponibles

    @property
    def huesos_disponibles(self) -> frozenset[str] | None:
        """Huesos humanoides del modelo cargado (``None`` hasta el primer sondeo)."""
        return self._huesos

    @property
    def estado_visor(self) -> dict[str, Any] | None:
        """Copia del ``current`` del último ``/api/state`` (pose, mirada, posición, cámara…) o ``None``."""
        return dict(self._actual) if self._actual is not None else None

    @property
    def ts_estado(self) -> float | None:
        """``time.monotonic()`` del último ``/api/state`` con ``current`` (``None`` si nunca llegó)."""
        return self._ts_estado

    @property
    def posicion(self) -> tuple[float, float, float] | None:
        """``(x, z, rotación en grados)`` del avatar según el último sondeo; ``None`` si no se sabe."""
        actual = self._actual or {}
        pos = actual.get("position")
        if not isinstance(pos, Mapping):
            return None
        try:
            valores = (float(pos.get("x", 0.0)), float(pos.get("z", 0.0)), float(actual.get("rotation", 0.0)))
        except (TypeError, ValueError):
            log.debug("Posición del visor no numérica")
            return None
        return valores if all(math.isfinite(v) for v in valores) else None

    @property
    def visores_escritorio(self) -> int:
        """Visores de escritorio (``?desktop=1``) con la ventana **visible** (``desktopViewers``)."""
        return self._visores_escritorio

    @property
    def visores_escritorio_total(self) -> int:
        """Visores de escritorio cargados, visibles u ocultos (``desktopViewersTotal``)."""
        return self._visores_escritorio_total

    @property
    def viewer_seq(self) -> int:
        """Contador de conexiones SSE del servidor (``viewerSeq``; 0 si el servidor no lo informa)."""
        return self._viewer_seq or 0

    def encolar(self, comando: Mapping[str, Any] | list[Any]) -> None:
        """Encola un comando (o lista) del visor; se envía en orden por el worker. No bloquea."""
        if self._estado != EstadoModulo.OFF:
            self._encolar(comando)

    def desktop(self, accion: str, **campos: Any) -> bool:
        """Encola ``{"type": "desktop", "action": accion, **campos}`` (solo, nunca en un lote mixto).

        ``True`` si lo encoló; ``False`` si el puente no está en línea, no hay ningún overlay de
        escritorio (``visores_escritorio_total == 0``) o la acción/los campos no son válidos
        (``sanear_desktop``). No bloquea. Con la ventana oculta el visor responde con un ack fallido
        (``error: "oculta"``): la visibilidad la sigue la corteza por ``escritorio.evento``.
        """
        if self._estado == EstadoModulo.OFF or not self.en_linea or self._visores_escritorio_total <= 0:
            return False
        comando = sanear_desktop({**campos, "type": "desktop", "action": accion})
        if comando is None:
            log.debug("Comando desktop inválido descartado: %s", str(accion)[:40])
            return False
        return self._encolar(comando)

    def aplicar_tema(self, nombre: str = "amadeus", *, rim: bool = True, scanlines: float = 0.07) -> None:
        """Encola ``{"type": "theme", "name", "rim", "scanlines"}`` (el visor recarga el modelo solo si
        el tema cambia). Un nombre desconocido se descarta con ``log.debug``."""
        comando = comando_tema(nombre, rim=rim, scanlines=scanlines)
        if comando is None:
            log.debug("Tema desconocido para el avatar 3D: %s", str(nombre)[:40])
            return
        self.encolar(comando)

    def refrescar(self) -> None:
        """Pide un sondeo de ``/api/state`` cuanto antes (como mucho uno por segundo). No bloquea;
        sin ``bucle`` en marcha no hace nada (el siguiente sondeo normal lo recogerá)."""
        if self._despertar is not None:
            self._despertar.set()

    # ------------------------------------------------------------------ cola y worker
    def _asegurar_worker(self) -> bool:
        if self._cerrado:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._cola is None:
            self._cola = asyncio.Queue(maxsize=MAX_COLA)
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._trabajar(), name="vtuber_ai.worker")
        return True

    def _encolar(self, comando: Any) -> bool:
        if not self._asegurar_worker() or self._cola is None:
            return False
        if self._cola.full():  # descarta el más antiguo (igual que el bus)
            try:
                self._cola.get_nowait()
                self._cola.task_done()
            except asyncio.QueueEmpty:
                pass
        self._cola.put_nowait(comando)
        return True

    async def _trabajar(self) -> None:
        assert self._cola is not None
        while True:
            comando = await self._cola.get()
            try:
                if self.en_linea or await self.consultar_estado():
                    await self.enviar(comando)
            except Exception:
                log.debug("Fallo enviando al avatar 3D", exc_info=True)
            finally:
                self._cola.task_done()

    # ------------------------------------------------------------------ eventos
    def _al_subtitulo(self, evento: Evento) -> None:
        datos = evento.datos
        canal = datos.get("canal")
        texto = str(datos.get("texto") or "").strip()
        if canal == "telegram" or not texto or self._estado == EstadoModulo.OFF:
            return
        self._pendientes.append({
            "texto": texto[:MAX_TEXTO], "exp": str(datos.get("exp") or "neutral"),
            "motion": str(datos.get("motion") or "idle"), "tone": str(datos.get("tone") or "analitico"),
            "voz_propia": canal == "voz",
        })
        if canal == "voz":  # riko publica cada segmento cuando empieza su audio: enviarlo ya
            self._vaciar_pendientes()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._flush is None:
            self._flush = loop.call_later(VENTANA_AGRUPAR_S, self._vaciar_pendientes)

    def _vaciar_pendientes(self) -> None:
        self._flush = None
        segmentos, self._pendientes = self._pendientes, []
        if segmentos:
            self._encolar(self.secuencia_para(segmentos))

    def secuencia_para(self, segmentos: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Construye la ``sequence`` del visor para uno o varios segmentos de una respuesta."""
        pasos: list[dict[str, Any]] = []
        deshacer_total: list[dict[str, Any]] = []
        duracion = 0.0
        for seg in segmentos:
            rate, pitch = TONO_A_PROSODIA.get(str(seg.get("tone")), (1.0, 1.0))
            durante, deshacer = pasos_motion(str(seg.get("motion")))
            pasos.append({**comando_expresion(str(seg.get("exp")), self._disponibles), "await": False})
            pasos.extend(durante)
            texto = str(seg.get("texto"))
            pasos.append({"type": "speak", "text": texto, "lang": self.lang, "voice": self.voz, "rate": rate,
                          "pitch": pitch, "tts": bool(self.tts_navegador and not seg.get("voz_propia"))})
            duracion += duracion_estimada(texto, rate)
            for paso in deshacer:
                if paso not in deshacer_total:
                    deshacer_total.append(paso)
        pasos.extend({**p, "await": False} for p in deshacer_total)
        self._hablando_hasta = max(self._hablando_hasta, time.monotonic()) + duracion
        self._exp_reposo = None  # tras hablar se vuelve a calcular la expresión de reposo
        return {"type": "sequence", "steps": pasos}

    def _al_homeostasis(self, evento: Evento) -> None:
        if sugerir_etiquetas is None or not self.en_linea or self.reposo_externo:
            return
        ahora = time.monotonic()
        if ahora < self._hablando_hasta or ahora - self._ultimo_reposo < MIN_ENTRE_REPOSO_S:
            return
        try:
            estado = HomeostaticState.desde_dict(evento.datos.get("estado") or {})
            exp = sugerir_etiquetas(estado).exp
        except Exception:
            log.debug("Estado homeostático inválido para el avatar 3D", exc_info=True)
            return
        if exp == self._exp_reposo:
            return
        self._exp_reposo, self._ultimo_reposo = exp, ahora
        self._encolar(comando_expresion(exp, self._disponibles, duracion=1.2))

    def _al_apagado(self, _evento: Evento) -> None:
        if self.en_linea:
            self._encolar({"type": "gesture", "name": "bow"})

    # ------------------------------------------------------------------ ciclo de vida
    async def iniciar(self) -> DiagnosticoModulo:
        """Primer sondeo (para el arranque); nunca lanza."""
        if self._estado != EstadoModulo.OFF:
            await self.consultar_estado()
        return self.diagnostico()

    async def bucle(self, parar: asyncio.Event) -> None:
        """Sondea el servidor (cada 5 s sin conexión, cada 15 s con ella, cada 2 s tras un ``409
        sin_overlay`` hasta volver a ver el overlay o pasar 20 s, o antes si alguien llama a
        ``refrescar()``, como mucho una vez por segundo) hasta ``parar``."""
        if self._estado == EstadoModulo.OFF:
            return
        self._asegurar_worker()
        despertar = self._despertar = asyncio.Event()
        try:
            while not parar.is_set():
                despertar.clear()
                inicio = time.monotonic()
                await self.consultar_estado()
                espera = INTERVALO_EN_LINEA_S if self.en_linea else INTERVALO_FUERA_S
                if time.monotonic() < self._sondeo_rapido_hasta:
                    espera = min(espera, SONDEO_RAPIDO_S)
                await self._esperar(parar, despertar, espera)
                resto = MIN_ENTRE_SONDEOS_S - (time.monotonic() - inicio)
                if resto > 0 and not parar.is_set():
                    try:
                        await asyncio.wait_for(parar.wait(), timeout=resto)
                    except asyncio.TimeoutError:
                        pass
        finally:
            if self._despertar is despertar:
                self._despertar = None

    @staticmethod
    async def _esperar(parar: asyncio.Event, despertar: asyncio.Event, espera: float) -> None:
        """Espera ``espera`` segundos, o hasta ``parar`` o ``despertar`` (lo primero que ocurra)."""
        tareas = {asyncio.ensure_future(parar.wait()), asyncio.ensure_future(despertar.wait())}
        try:
            await asyncio.wait(tareas, timeout=espera, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for tarea in tareas:
                if not tarea.done():
                    tarea.cancel()

    async def cerrar(self, timeout: float = 2.5) -> None:
        """Entrega lo pendiente (p. ej. la despedida), detiene el worker y suelta el cliente propio."""
        if self._flush is not None:
            self._flush.cancel()
        self._vaciar_pendientes()
        if self._cola is not None and self._worker is not None and not self._worker.done():
            try:
                await asyncio.wait_for(self._cola.join(), timeout=timeout)
            except asyncio.TimeoutError:
                log.debug("El avatar 3D no confirmó a tiempo los últimos comandos")
        self._cerrado = True
        for desuscribir in self._desuscribir:
            try:
                desuscribir()
            except Exception as exc:
                log.debug("Error al desuscribir el puente del avatar 3D: %s", type(exc).__name__)
        self._desuscribir = []
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                log.debug("Worker del avatar 3D cancelado")
            except Exception as exc:
                log.debug("El worker del avatar 3D terminó con error: %s", type(exc).__name__)
        if self._cliente_propio and self._cliente is not None:
            try:
                await self._cliente.aclose()
            except Exception as exc:
                log.debug("Error al cerrar el cliente HTTP del avatar 3D: %s", type(exc).__name__)
            self._cliente = None
