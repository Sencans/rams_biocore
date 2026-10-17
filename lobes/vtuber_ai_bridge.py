"""Puente con **VTuber AI Studio**: avatar 3D (VRM) controlado por HTTP en ``localhost:3939``.

Es una alternativa (o un complemento) a VTube Studio. A diferencia de ``VTubeBridge``, este
puente **no** se inyecta en el despachador de salida: escucha el bus y traduce lo que ya
publican los demás módulos (§5) a comandos ``POST /api/command`` de la interfaz:

- ``salida.subtitulo``  → expresión mezclada + gesto/pose + ``speak`` (globo y labios). Si R.A.M.S.
  ya está hablando con su propia voz (``canal == "voz"``) el visor anima los labios en silencio;
  si la salida es solo texto (``canal == "terminal"``), el visor puede hablar con la voz del
  navegador (``VTUBER_AI_TTS=1``). Los segmentos que llegan juntos se agrupan en una ``sequence``,
  porque en el visor cada ``speak`` interrumpe al anterior.
- ``homeostasis.estado`` → expresión de reposo (``sugerir_etiquetas``) cuando no está hablando.
- ``sistema.apagado``   → reverencia de despedida.

Nunca bloquea el bus: los manejadores solo encolan; un *worker* propio envía en orden. Si el
servidor no corre o no hay ningún visor abierto, el módulo queda ``EN ESPERA`` y descarta los
comandos sin ruido. Solo habla con ``127.0.0.1``/``localhost``.
"""

from __future__ import annotations

import asyncio
import logging
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

VENTANA_AGRUPAR_S: Final = 0.25        # segmentos de una misma respuesta llegan casi juntos
INTERVALO_EN_LINEA_S: Final = 15.0
INTERVALO_FUERA_S: Final = 5.0
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


class VTuberAIBridge:
    """Traduce los eventos del bus de R.A.M.S. a comandos HTTP de VTuber AI Studio."""

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *, cliente: Any = None) -> None:
        self.settings = settings
        self.bus = bus
        self.url = str(getattr(settings, "vtuber_ai_url", "http://127.0.0.1:3939")).rstrip("/")
        self.tts_navegador = bool(getattr(settings, "vtuber_ai_tts", True))
        self.lang = str(getattr(settings, "vtuber_ai_lang", "es-MX"))
        self.en_linea = False
        self.visores = 0
        self.modelo: str | None = None
        self._disponibles: frozenset[str] | None = None
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
            r = await cliente.get(f"{self.url}/api/state", timeout=3.0)
            datos = r.json() if r.status_code == 200 else {}
        except Exception as exc:  # conexión rechazada, timeout, JSON inválido…
            log.debug("VTuber AI Studio no responde: %s", type(exc).__name__)
            self._cambiar_estado(EstadoModulo.EN_ESPERA, "VTuber AI Studio no está corriendo")
            return False
        if not isinstance(datos, Mapping):
            datos = {}
        self.visores = int(datos.get("viewers") or 0)
        self.modelo = datos.get("model") if isinstance(datos.get("model"), str) else None
        exprs = datos.get("expressionsAvailable")
        if isinstance(exprs, list):
            self._disponibles = frozenset(str(e) for e in exprs)
        if self.visores <= 0:
            self._cambiar_estado(EstadoModulo.EN_ESPERA, f"servidor OK, abre el visor en {self.url}")
            return False
        if not datos.get("ready", True):
            self._cambiar_estado(EstadoModulo.EN_ESPERA, "el visor está cargando el modelo")
            return False
        modelo = f" · {self.modelo}" if self.modelo else ""
        self._cambiar_estado(EstadoModulo.OK, f"{self.visores} visor(es){modelo}")
        return True

    async def enviar(self, comando: Mapping[str, Any] | list[Any]) -> bool:
        """``POST /api/command``. Devuelve ``True`` si el visor lo aceptó."""
        cliente = self._http()
        if cliente is None:
            return False
        try:
            r = await cliente.post(f"{self.url}/api/command", json=comando,
                                   headers={"X-Source": "rams"}, timeout=5.0)
        except Exception as exc:
            log.debug("No se pudo enviar al avatar 3D: %s", type(exc).__name__)
            self._cambiar_estado(EstadoModulo.EN_ESPERA, "VTuber AI Studio no está corriendo")
            return False
        if r.status_code == 409:  # sin visores abiertos
            self._cambiar_estado(EstadoModulo.EN_ESPERA, f"servidor OK, abre el visor en {self.url}")
            return False
        try:
            datos = r.json()
        except ValueError:
            datos = {}
        ok = r.status_code == 200 and bool(datos.get("ok", True))
        if not ok:
            log.debug("El avatar 3D rechazó el comando: %s", str(datos.get("error") or datos)[:200])
        return ok

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

    def _encolar(self, comando: Any) -> None:
        if not self._asegurar_worker() or self._cola is None:
            return
        if self._cola.full():  # descarta el más antiguo (igual que el bus)
            try:
                self._cola.get_nowait()
                self._cola.task_done()
            except asyncio.QueueEmpty:
                pass
        self._cola.put_nowait(comando)

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
            pasos.append({"type": "speak", "text": texto, "lang": self.lang, "rate": rate, "pitch": pitch,
                          "tts": bool(self.tts_navegador and not seg.get("voz_propia"))})
            duracion += duracion_estimada(texto, rate)
            for paso in deshacer:
                if paso not in deshacer_total:
                    deshacer_total.append(paso)
        pasos.extend({**p, "await": False} for p in deshacer_total)
        self._hablando_hasta = max(self._hablando_hasta, time.monotonic()) + duracion
        self._exp_reposo = None  # tras hablar se vuelve a calcular la expresión de reposo
        return {"type": "sequence", "steps": pasos}

    def _al_homeostasis(self, evento: Evento) -> None:
        if sugerir_etiquetas is None or not self.en_linea:
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
        """Sondea el servidor (cada 5 s sin conexión, cada 15 s con ella) hasta ``parar``."""
        if self._estado == EstadoModulo.OFF:
            return
        self._asegurar_worker()
        while not parar.is_set():
            await self.consultar_estado()
            espera = INTERVALO_EN_LINEA_S if self.en_linea else INTERVALO_FUERA_S
            try:
                await asyncio.wait_for(parar.wait(), timeout=espera)
            except asyncio.TimeoutError:
                pass

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
            except Exception:
                pass
        self._desuscribir = []
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        if self._cliente_propio and self._cliente is not None:
            try:
                await self._cliente.aclose()
            except Exception:
                pass
            self._cliente = None
