"""Corteza motora de Amadeus: autonomía corporal del avatar 3D (VTuber AI Studio), sin LLM.

Qué hace:
- Decide **cuándo** y **cómo** se mueve el avatar VRM: locomoción por el escenario, mirada
  (sacádicas, seguir el ratón, mirar alrededor, mirada esquiva), posturas (poses del visor y
  micro-posturas procedurales con ``bones`` de ángulos pequeños), gestos, manos, cara de reposo y
  microexpresiones, intensidad del *idle* y, solo si ``settings.avatar_camara``, el encuadre.
- Quien decide es el estado del conectoma (``homeostasis.estado``), nunca el LLM: el arousal fija
  la tasa de conductas y la intensidad del reposo; el aburrimiento empuja a explorar y a encogerse
  de hombros; la fatiga, a estirarse, bostezar y dormitar (y baja la actividad); el foco alto la
  deja quieta, pensativa o con los brazos cruzados mirando a cámara; la curiosidad, a mirar
  alrededor y ladear la cabeza; la valencia, a la alegría contenida o al enfado. El ``bump`` del
  anillo E-PG del Complejo Central (que en la mosca codifica el rumbo) hace de **brújula** por
  integración de trayectoria: su deriva gira el rumbo de marcha que ella recuerda, su fase lo
  atrae suavemente y el aburrimiento y la curiosidad añaden ruido de exploración. Camina desde
  donde está, una distancia mínima, acotada al escenario y rebotando en los bordes.
- Personalidad Amadeus/Kurisu en el cuerpo: contenida (las conductas efusivas son raras),
  científica, brazos cruzados cuando está molesta o concentrada, pose pensativa con foco alto,
  mirada esquiva al "sonrojarse" y reacción "Reading Steiner" a ``homeostasis.cambio_linea``
  (sorpresa, mano a la sien, mirar alrededor).

Cómo:
- ``decidir(estado, ahora, contexto, memoria)`` es el núcleo puro de la decisión: sin E/S y sin
  tocar el estado de la corteza, devuelve una ``Decision`` (el ``Plan``, con pasos con desfase
  temporal y comandos del visor ya saneados, o ``None``, y la ``MemoriaMotora`` siguiente:
  enfriamientos, última conducta, próxima decisión y rumbo de marcha). El azar sale de
  ``random.Random`` sembrado con (semilla, nº de decisión) de la memoria: las mismas entradas dan
  siempre la misma decisión.
- ``tick()`` hace la E/S: guarda la memoria nueva, envía con ``avatar.encolar`` (no bloquea) los
  pasos que vencen, mantiene la cara de reposo y la intensidad del *idle*, atiende al usuario y
  respeta el habla. ``bucle(parar)`` llama a ``tick`` cada 250 ms.
- Nunca envía ``sequence``, ``speak``, ``reset`` ni ``stop`` de la secuencia o del habla: en el
  visor una ``sequence`` nueva cancela la anterior y cortaría el habla del puente. Las únicas
  excepciones son ``{"type": "stop", "what": "gestures"}`` al empezar el habla, que corta un gesto
  largo suyo sin tocar la ``sequence``, y ``{"type": "desktop", "action": "stop"}``, que solo detiene
  su locomoción por el escritorio. Las conductas de varios pasos se agendan aquí y cada paso sale
  como un comando (o una lista paralela) independiente; los ``desktop`` siempre salen solos.
- Mientras está activa, ``avatar.reposo_externo = True`` (ella gobierna la cara en reposo); al
  cerrar lo devuelve a ``False``.

Límites: posiciones dentro de un escenario seguro que depende del encuadre, ángulos de ``bones``
acotados por hueso, solo huesos y expresiones que el modelo tiene, enfriamiento por conducta, sin
repetir la misma conducta dos veces seguidas, tope global de comandos por minuto y quietud
mientras habla. Al empezar el habla corta la conducta y devuelve el cuerpo **en ese momento**
(de frente, pose ``relaxed``, mirada a cámara, boca y párpados cerrados, huesos e *idle*
restaurados), salvo lo que gobierne el ``MOTION`` del propio habla; solo la cara de reposo espera
a que termine.

Modo escritorio (v0.2, ``docs/COMPANION.md`` §C1.4, §C1.11 y §C2.10; habilitado con
``settings.tiene_escritorio``). La corteza decide la vida de Amadeus en el escritorio, sin LLM:

- **Visibilidad:** la sigue por ``escritorio.evento`` (``mostrada``/``ocultada``) y la pierde con
  ``escritorio.conectado{overlay: False}``. Solo con la ventana visible **y** un overlay cargado
  (``avatar.visores_escritorio_total > 0``) emite conductas de escritorio (``avatar.desktop(...)``:
  ``pasear_escritorio`` → ``wander``/``walk_to`` con la brújula E-PG, ``asomarse`` → ``peek``,
  ``perseguir_cursor`` → ``chase_cursor``, ``buscar_usuario`` → ``seek_user``). Con la ventana oculta
  sigue con las conductas de escenario de la v0.1 (para el panel). Con la ventana **visible** el cuerpo
  está en el overlay aunque no pueda emitir ``desktop`` ("no molestar", saliendo, el puente aún sin
  overlay): no hay escenario, así que nunca ``move`` con x/z ni ``walk``, ni paseos de la v0.1, y la
  atención es la de escritorio; mientras Electron la hace salir o entrar por el borde (saliendo y los
  primeros ``GRACIA_ENTRADA_S`` tras ``mostrada``) ni giros ni conductas nuevas. Los comandos ``desktop``
  salen siempre solos, nunca en una lista paralela con otros tipos.
- **Solicitudes de main** (``motor.solicitud``): ``buscar_usuario`` → ``seek_user``, ``asomarse`` →
  ``peek{edge}``, ``pasear`` → ``wander{seconds ≤ 15}``, ``esconderse`` → ``escritorio.orden``. Guarda
  la última hasta ``ttl_s`` esperando visibilidad y silencio; al caducar la descarta (``log.debug``).
- **Esconderse sola:** con ``pc.no_molestar.activo`` (una vez por activación) o con fatiga > 0,8
  sostenida 10 min **a la vista** (el tiempo oculta no cuenta: al llamarla no se va enseguida) publica
  ``escritorio.orden{accion: "esconderse"}``. **Nunca** publica ``aparecer``: solo reaparece si la
  llaman o por una iniciativa de main.
- **Mirada:** sigue al usuario con ``vision.mirada`` (confianza > 0,6, reciente) con un ``look`` a un
  punto como mucho 1 por segundo, solo si el punto se movió > 0,15 m y con su propio sub-tope
  (``MIRADA_MAX_MIN``/min, dentro de ``LIMITE_COMANDOS_MIN``); ``vision.rostro`` frontal →
  mira al usuario; ``escritorio.evento{toque|soltada}`` → sobresalto o sonrojo breve; la atención por
  el chat (canal ``escritorio``) la gira de frente y la hace mirar al ratón, sin acercarse.
- **Travesuras:** publica ``motor.travesura`` (main → compuerta → ControlPC) solo con
  ``motor.travesuras.activas``, la ventana visible, sin habla, sin "no molestar", aburrimiento > 0,6,
  como mucho 1 cada 15 min y 6 al día. **Nunca** llama a ControlPC.

Degradación: sin avatar en línea no hace nada (``EN ESPERA``); con ``avatar_actividad = 0`` queda
quieta (``OFF``) y devuelve la cara de reposo al puente. Nunca lanza desde los manejadores del bus.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from core.bus import BusEventos, Evento
from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo, HomeostaticState, formatear_decimal
from lobes.vtuber_ai_bridge import (
    ACCIONES_DESKTOP,
    BORDES_DESKTOP,
    CAMARAS_VISOR,
    DESKTOP_SEGUNDOS_MAX,
    GESTOS_VISOR,
    MANOS_VISOR,
    MIRADAS_VISOR,
    POSES_VISOR,
    comando_expresion,
    mezcla_para,
    pasos_motion,
    sanear_desktop,
)

try:
    from core.etiquetas import sugerir_etiquetas
except ImportError:  # pragma: no cover - core.etiquetas es P0
    sugerir_etiquetas = None  # type: ignore[assignment]

try:  # catálogos cerrados de la v0.2 (§C2.2); fallback si core.contracts aún no los tiene
    from core.contracts import CONDUCTAS_SOLICITUD, EVENTOS_ESCRITORIO, PATRONES_TRAVESURA
except ImportError:  # pragma: no cover - depende de la versión de core.contracts
    PATRONES_TRAVESURA = ("empujon", "circulo", "temblor", "hacia_avatar")  # type: ignore[misc]
    EVENTOS_ESCRITORIO = ("toque", "soltada", "cursor_encima", "mostrada", "ocultada",  # type: ignore[misc]
                          "chat_abierto")
    CONDUCTAS_SOLICITUD = ("buscar_usuario", "asomarse", "esconderse", "pasear")  # type: ignore[misc]

log = logging.getLogger("rams.motor")

CLAVE: Final = "motor"
NOMBRE: Final = "Corteza motora (avatar 3D)"

# --- Ritmo -----------------------------------------------------------------------------------
TICK_S: Final = 0.25
LIMITE_COMANDOS_MIN: Final = 40      # tope duro de comandos por minuto (todas las fuentes)
RESERVA_PRIORITARIA: Final = 8       # parte del tope que solo usan la atención y el Reading Steiner
INTERVALO_BASE_S: Final = 7.0        # intervalo medio entre conductas con actividad 1 y arousal ≈ 0,6
INTERVALO_MIN_S: Final = 2.5
INTERVALO_MAX_S: Final = 60.0
MIN_ENTRE_REPOSO_S: Final = 8.0      # la cara de reposo cambia como mucho cada 8 s
MIN_ENTRE_IDLE_S: Final = 15.0
ATENCION_S: Final = 20.0             # tras una entrada del usuario: atención, sin pasear
HABLA_INMINENTE_S: Final = 1.2       # un subtítulo anuncia habla (el puente agrupa 0,25 s)
VOZ_MAX_S: Final = 90.0              # voz.inicio sin voz.fin caduca
CALMA_TRAS_HABLAR_S: Final = 2.5
REFRACTARIO_STEINER_S: Final = 30.0
CADUCIDAD_STEINER_S: Final = 20.0

# --- Escenario (coordenadas del visor: x + = derecha del espectador = izquierda de ella;
#     z + = hacia la cámara; rotation + = gira hacia SU izquierda) ------------------------------
LIMITE_X_ABS: Final = 1.6
LIMITE_Z_ABS: Final = (-1.0, 0.6)
# Encuadre → (|x| máx, z mín, z máx) por donde puede caminar; None = no camina (se saldría del plano).
ESCENARIO_POR_CAMARA: Final[dict[str, tuple[float, float, float] | None]] = {
    "full": (1.0, -0.7, 0.4),
    "wide": (1.5, -1.0, 0.6),
    "bust": None,
    "face": None,
}
Z_ATENCION: Final = 0.25             # se acerca un poco a la cámara cuando le escriben
DIST_MIN_PASEO: Final = 0.3          # por debajo no merece la pena caminar
PASEO_MIN: Final = 0.45              # longitud de un paseo (m): de PASEO_MIN a PASEO_MAX según el impulso
PASEO_MAX: Final = 1.3
MARGEN_BORDE: Final = 0.05           # no camina pegada al borde del escenario
VEL_MIN: Final = 0.45                # m/s con arousal 0
VEL_MAX: Final = 0.85                # m/s con arousal 1
GIRO_MAX_QUIETA: Final = 35.0        # giro sin caminar: siempre de frente al público
GIRO_MIN_QUIETA: Final = 10.0        # un giro en el sitio menor no se nota

# --- Brújula (integración de trayectoria sobre el bump E-PG) ---------------------------------
GANANCIA_DERIVA: Final = 1.0         # el rumbo de marcha gira lo mismo que el bump desde la última marcha
GANANCIA_FASE: Final = 0.2           # atracción suave hacia la fase del bump ("dirección de atención")
RUIDO_RUMBO_BASE: Final = 10.0       # desviación típica (°) del ruido de exploración…
RUIDO_RUMBO_ABURRIMIENTO: Final = 60.0   # …que crece con el aburrimiento…
RUIDO_RUMBO_CURIOSIDAD: Final = 40.0     # …con la curiosidad…
RUIDO_RUMBO_INCOHERENCIA: Final = 25.0   # …y con un bump difuso (1 − coherencia)

# --- Entradas del usuario que piden atención (por Telegram no está frente a la cámara) --------
# "escritorio" (chat de Amadeus Desktop o del HUD): atención de escritorio, sin acercarse.
CANALES_ATENCION: Final = frozenset({"terminal", "nexus", "escritorio"})
ATENCION_ESCRITORIO_S: Final = 6.0   # mira al ratón (donde está el chat) y luego vuelve a mirar al usuario

# Único ``stop`` que envía: corta sus gestos largos al empezar el habla sin cancelar la sequence.
PARAR_GESTOS: Final[dict[str, str]] = {"type": "stop", "what": "gestures"}
# Detiene la locomoción de escritorio (paseo, persecución…) al empezar el habla o una reacción.
PARAR_DESKTOP: Final[dict[str, str]] = {"type": "desktop", "action": "stop"}

# --- Modo escritorio (Amadeus Desktop; docs/COMPANION.md §C1.4, §C1.11, §C2.10) ----------------
# Conductas autónomas de escritorio (solo con la ventana visible y un overlay cargado) y su enfriamiento.
ENFRIAMIENTO_ESCRITORIO_S: Final[dict[str, float]] = {
    "pasear_escritorio": 25.0, "asomarse": 60.0, "perseguir_cursor": 90.0, "buscar_usuario": 150.0,
}
CONDUCTAS_ESCRITORIO: Final = tuple(ENFRIAMIENTO_ESCRITORIO_S)
# Duración estimada (s) de cada acción ``desktop`` sin ``seconds`` (para el fin del plan).
DURACION_DESKTOP: Final[dict[str, float]] = {"walk_to": 3.5, "peek": 4.0, "chase_cursor": 6.0, "seek_user": 4.5,
                                             "wander": 8.0, "stop": 0.0}
# Solicitud de main → acción desktop ("esconderse" no es del visor: es una escritorio.orden).
SOLICITUD_A_DESKTOP: Final[dict[str, str]] = {"buscar_usuario": "seek_user", "asomarse": "peek", "pasear": "wander"}
SOLICITUD_TTL_DEFECTO_S: Final = 20.0
SOLICITUD_TTL_MAX_S: Final = 30.0
USUARIO_OLVIDADO_S: Final = 300.0    # sin entradas del usuario en 5 min, puede ir a buscarlo por su cuenta
# Esconderse sola por cansancio: fatiga > 0,8 sostenida 10 min CON ELLA A LA VISTA (oculta no cuenta).
FATIGA_ESCONDERSE: Final = 0.8
FATIGA_SOSTENIDA_S: Final = 600.0
# Tras "mostrada" el main de Electron la hace entrar caminando por el borde: ni giros ni conductas nuevas.
GRACIA_ENTRADA_S: Final = 3.0
# Mirada al usuario con la visión continua (vision.mirada / vision.rostro).
MIRADA_MIN_ENTRE_S: Final = 1.0      # como mucho un look al usuario por segundo…
MIRADA_UMBRAL_M: Final = 0.15        # …y solo si el punto se movió más que esto…
MIRADA_MAX_MIN: Final = 12           # …con su propio sub-tope (12 look/min de los 40: no ahoga las conductas)…
MIRADA_RAFAGA: Final = 4             # …que admite ráfagas cortas (cubo de fichas)
MIRADA_CONFIANZA_MIN: Final = 0.6
MIRADA_RECIENTE_S: Final = 5.0       # vision.mirada solo se publica si cambia: una muestra vale 5 s
MIRADA_CABEZA: Final = 0.3
ROSTRO_MIN_ENTRE_S: Final = 10.0     # vision.rostro frontal → mirar al usuario, como mucho cada 10 s
ROSTRO_CADUCIDAD_S: Final = 2.0
# Reacciones a toques y arrastres sobre la caja del avatar.
REACCION_MIN_ENTRE_S: Final = 3.0
REACCION_CADUCIDAD_S: Final = 2.0
SOLTADA_ALTA: Final = 0.3            # soltada desde más arriba: sobresalto y enfado
# Travesuras de cursor (las ejecuta main → compuerta → ControlPC; aquí solo se proponen).
TRAVESURA_MIN_ENTRE_S: Final = 900.0         # ≤ 1 cada 15 min…
TRAVESURA_MARGEN_S: Final = 600.0            # …más un margen aleatorio para que no sea un reloj
TRAVESURAS_MAX_DIA: Final = 6                # ≤ 6 en cualquier ventana de 24 h
TRAVESURA_VENTANA_S: Final = 86400.0
TRAVESURA_ABURRIMIENTO: Final = 0.6
TRAVESURA_SEGUNDOS_MAX: Final = 0.6
TRAVESURA_AMPLITUD_RANGO: Final = (5, 80)

# --- Vocabulario permitido (subconjunto del protocolo del visor) ------------------------------
TIPOS_PERMITIDOS: Final = frozenset({"expression", "pose", "gesture", "bones", "hand", "look", "move",
                                     "camera", "idle", "desktop"})
# Kurisu no baila, no salta, no aplaude ni hace reverencias por su cuenta (bow es la despedida del puente).
GESTOS_PERMITIDOS: Final = frozenset({"nod", "shake_head", "tilt_head", "shrug", "look_around", "think",
                                      "surprise", "laugh", "cheer", "point", "wave"}) & GESTOS_VISOR
POSES_PERMITIDAS: Final = frozenset({"relaxed", "arms_up", "hands_on_hips", "arms_crossed", "thinking",
                                     "shy"}) & POSES_VISOR
MIRADAS_PERMITIDAS: Final = frozenset(MIRADAS_VISOR) - {"none"}
# Expresiones estándar de VRM: se usan si todavía no se conocen las del modelo.
EXPRESIONES_ESTANDAR: Final = frozenset({"happy", "angry", "sad", "relaxed", "surprised", "aa", "ih", "ou",
                                         "ee", "oh", "blink"})
DURACION_GESTO: Final[dict[str, float]] = {
    "wave": 2.6, "nod": 1.2, "shake_head": 1.4, "bow": 2.2, "tilt_head": 1.8, "jump": 1.1, "clap": 2.0,
    "shrug": 1.6, "cheer": 2.4, "point": 2.5, "dance": 6.0, "laugh": 2.2, "look_around": 3.2, "think": 3.5,
    "surprise": 1.5,
}

# Rango (mín, máx) en grados por eje (x, y, z) de cada hueso que la corteza puede tocar con ``bones``.
# Las micro-posturas usan una fracción pequeña; los brazos solo existen para "mano a la sien".
LIMITES_HUESO: Final[dict[str, tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]] = {
    "hips": ((-3.0, 3.0), (-8.0, 8.0), (-5.0, 5.0)),
    "spine": ((-5.0, 8.0), (-6.0, 6.0), (-5.0, 5.0)),
    "chest": ((-5.0, 6.0), (-5.0, 5.0), (-4.0, 4.0)),
    "upperChest": ((-4.0, 5.0), (-4.0, 4.0), (-3.0, 3.0)),
    "neck": ((-8.0, 8.0), (-10.0, 10.0), (-6.0, 6.0)),
    "head": ((-14.0, 14.0), (-18.0, 18.0), (-12.0, 12.0)),
    "leftShoulder": ((0.0, 0.0), (-5.0, 5.0), (-8.0, 8.0)),
    "rightShoulder": ((0.0, 0.0), (-5.0, 5.0), (-8.0, 8.0)),
    "leftHand": ((-20.0, 20.0), (-45.0, 45.0), (-25.0, 25.0)),
    "rightHand": ((-20.0, 20.0), (-45.0, 45.0), (-25.0, 25.0)),
    "rightUpperArm": ((-90.0, 0.0), (0.0, 40.0), (0.0, 80.0)),
    "rightLowerArm": ((0.0, 0.0), (0.0, 150.0), (0.0, 0.0)),
}
_EJES: Final = ("x", "y", "z")

# Mano derecha a la sien (cinemática directa con proporciones humanoides medias; Reading Steiner).
MANO_A_LA_SIEN: Final[dict[str, dict[str, float]]] = {
    "rightUpperArm": {"x": -90.0, "y": 20.0, "z": 70.0},
    "rightLowerArm": {"y": 140.0},
    "rightHand": {"x": 10.0, "y": -40.0, "z": 10.0},
}

# Huesos no braquiales que fija cada pose del visor (lib/protocol.js): base de las micro-posturas.
BASE_POSE: Final[dict[str, dict[str, dict[str, float]]]] = {
    "relaxed": {},
    "arms_up": {"head": {"x": -8.0}},
    "hands_on_hips": {"spine": {"z": 2.0}},
    "arms_crossed": {"head": {"x": -4.0, "z": 3.0}},
    "thinking": {"head": {"x": 4.0, "z": -7.0}},
    "shy": {"head": {"x": 12.0, "z": -8.0}, "spine": {"x": 4.0}},
}
MANO_POSE: Final[dict[str, str]] = {"arms_up": "open", "tpose": "open", "apose": "open"}

# Enfriamiento (s) de cada conducta: no se repite antes aunque el estado la pida.
ENFRIAMIENTO_S: Final[dict[str, float]] = {
    "mirada": 5.0, "seguir_raton": 30.0, "microexpresion": 8.0, "micro_postura": 10.0, "manos": 15.0,
    "pasear": 18.0, "girar": 12.0, "volver_al_centro": 10.0, "mirar_alrededor": 25.0, "ladear_cabeza": 12.0,
    "pensativa": 25.0, "brazos_cruzados": 25.0, "estirarse": 50.0, "bostezar": 45.0, "dormitar": 30.0,
    "encogerse": 30.0, "asentir": 40.0, "alegria": 45.0, "tsundere": 35.0, "postura": 40.0, "camara": 60.0,
}
CONDUCTAS: Final = tuple(ENFRIAMIENTO_S)
_SIN_ATENCION: Final = frozenset({"pasear", "girar", "estirarse", "encogerse", "camara", "mirar_alrededor",
                                  "dormitar", "bostezar"})
_EN_REPOSO: Final = frozenset({"dormitar", "bostezar", "mirada", "micro_postura", "microexpresion"})


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rumbo:
    """Dirección que marca el bump E-PG: 0° = hacia la cámara, 90° = derecha del espectador
    (izquierda de ella), 180° = fondo del escenario. ``coherencia`` 0..1 (longitud del vector)."""

    angulo: float
    coherencia: float


@dataclass(frozen=True)
class Brujula:
    """Rumbo de marcha propuesto por integración de trayectoria (mismo convenio de ángulos que ``Rumbo``).

    ``angulo`` = rumbo recordado + ``deriva`` del bump desde que se fijó + atracción hacia la fase
    del bump + ruido de exploración. ``bump`` es la fase cruda del bump (la referencia que se guarda
    al fijar un rumbo nuevo) y ``coherencia`` su nitidez (0..1).
    """

    angulo: float
    coherencia: float
    bump: float
    deriva: float = 0.0


@dataclass(frozen=True)
class Paso:
    """Comandos que salen juntos (lista paralela del visor) ``t`` segundos después de empezar.

    ``final`` marca los pasos de cierre (volver a la pose, mirar a cámara, girar de frente…): si la
    conducta se interrumpe, no se descartan, se aplazan. ``reposo`` = EXP si el paso es la cara de reposo.
    """

    t: float
    comandos: tuple[dict[str, Any], ...]
    final: bool = False
    reposo: str | None = None


@dataclass(frozen=True)
class Plan:
    """Una conducta motora ya saneada: pasos con desfase temporal.

    ``rumbo`` es la dirección (grados, convenio de ``Rumbo``) en la que camina, si camina: al
    ejecutarse pasa a ser el rumbo de marcha recordado (integración de trayectoria).
    """

    conducta: str
    pasos: tuple[Paso, ...]
    duracion: float = 0.0
    rumbo: float | None = None

    def comandos(self) -> list[dict[str, Any]]:
        """Todos los comandos del plan, en orden."""
        return [c for p in self.pasos for c in p.comandos]

    def tipos(self) -> list[str]:
        """Tipos de comando del plan, en orden (útil para logs y tests)."""
        return [str(c.get("type")) for c in self.comandos()]


@dataclass(frozen=True)
class ContextoMotor:
    """Lo que la decisión necesita saber del cuerpo y del entorno (lo arma ``tick``)."""

    hablando: bool = False
    en_linea: bool = True
    huesos: frozenset[str] | None = None
    expresiones: frozenset[str] | None = None
    posicion: tuple[float, float] = (0.0, 0.0)
    rotacion: float = 0.0
    pose: str = "relaxed"
    camara: str = "full"
    desde_usuario_s: float = math.inf
    presupuesto: int = LIMITE_COMANDOS_MIN         # comandos que aún caben en el último minuto
    escritorio: bool = False                       # puede emitir conductas desktop (visible, overlay, sin "no molestar")
    usuario: tuple[float, float, float] | None = None   # punto del usuario (vision.mirada útil) o None
    atencion_escritorio: bool = False              # la última entrada vino del chat de escritorio (sin acercarse)
    en_escritorio: bool = False                    # el cuerpo está en el overlay visible (aunque no pueda emitir desktop)

    @property
    def sin_escenario(self) -> bool:
        """En el escritorio no hay escenario (el visor ignora ``move`` x/z): nada de locomoción de la v0.1,
        tanto si puede emitir ``desktop`` (``escritorio``) como si no ("no molestar", saliendo, el puente
        aún sin overlay: ``en_escritorio``)."""
        return self.escritorio or self.en_escritorio


@dataclass(frozen=True)
class MemoriaMotora:
    """Lo que la decisión recuerda entre una llamada y la siguiente (inmutable).

    ``decidir`` no la modifica: devuelve la siguiente dentro de la ``Decision``. ``semilla`` y ``n``
    (decisiones tomadas) fijan el generador aleatorio de cada decisión, así que la misma memoria con
    las mismas entradas decide siempre lo mismo.
    """

    semilla: int = 0
    n: int = 0
    ultima: str | None = None
    usos: tuple[tuple[str, float], ...] = ()       # (conducta, instante del último uso)
    proxima: float = -math.inf                     # no decide antes de este instante
    plan_hasta: float = -math.inf                  # fin estimado del plan en curso
    rumbo_marcha: float | None = None              # rumbo de marcha recordado (grados)
    bump_ref: float | None = None                  # fase del bump cuando se fijó ese rumbo

    def uso(self, conducta: str) -> float:
        """Instante del último uso de la conducta (``-inf`` si nunca)."""
        for nombre, instante in self.usos:
            if nombre == conducta:
                return instante
        return -math.inf

    def con_uso(self, conducta: str, ahora: float) -> MemoriaMotora:
        """Copia con el uso de ``conducta`` registrado en ``ahora``."""
        usos = dict(self.usos)
        usos[conducta] = ahora
        return replace(self, usos=tuple(sorted(usos.items())))

    def azar(self) -> random.Random:
        """Generador de esta decisión: depende solo de (semilla, n)."""
        return random.Random(f"rams.motor:{self.semilla}:{self.n}")


@dataclass(frozen=True)
class Decision:
    """Salida del núcleo puro: el plan elegido (o ``None``) y la memoria siguiente."""

    plan: Plan | None
    memoria: MemoriaMotora


@dataclass
class _Pendiente:
    t_abs: float
    paso: Paso
    conducta: str


@dataclass(frozen=True)
class SolicitudMotor:
    """Última ``motor.solicitud`` de main, retenida hasta ``vence`` (reloj de la corteza)."""

    conducta: str
    motivo: str
    borde: str | None
    vence: float


@dataclass(frozen=True)
class MiradaUsuario:
    """Último punto ``vision.mirada`` (coordenadas del visor, metros) y cuándo llegó."""

    x: float
    y: float
    z: float
    confianza: float
    t: float


# ---------------------------------------------------------------------------
# Funciones puras
# ---------------------------------------------------------------------------


def _acotar(valor: float, minimo: float, maximo: float) -> float:
    return minimo if valor < minimo else maximo if valor > maximo else valor


def _num(valor: Any, defecto: float | None = None) -> float | None:
    """Número finito o ``defecto`` (``bool`` no cuenta como número)."""
    if isinstance(valor, bool):
        return defecto
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if math.isfinite(numero) else defecto


def envolver_180(angulo: float) -> float:
    """Ángulo equivalente en [-180, 180)."""
    return (angulo + 180.0) % 360.0 - 180.0


def rumbo_del_bump(bump: Sequence[Any], defecto_deg: float = 0.0) -> Rumbo:
    """Vector poblacional del anillo E-PG: la columna ``k`` de ``n`` vale ``2πk/n`` (como el conectoma).

    Devuelve el ángulo (0..360) y la coherencia (``|Σ b·e^{iθ}| / Σ b``). Sin bump útil devuelve
    ``defecto_deg`` con coherencia 0.
    """
    valores: list[float] = []
    for v in bump or ():
        numero = _num(v, 0.0) or 0.0
        valores.append(max(0.0, numero))
    n = len(valores)
    total = sum(valores)
    if n < 3 or total <= 1e-9:
        return Rumbo((_num(defecto_deg, 0.0) or 0.0) % 360.0, 0.0)
    zx = sum(v * math.cos(2.0 * math.pi * k / n) for k, v in enumerate(valores))
    zy = sum(v * math.sin(2.0 * math.pi * k / n) for k, v in enumerate(valores))
    angulo = math.degrees(math.atan2(zy, zx)) % 360.0
    coherencia = _acotar(math.hypot(zx, zy) / total, 0.0, 1.0)
    return Rumbo(angulo, coherencia)


def limites_escenario(camara: str | None) -> tuple[float, float, float] | None:
    """(|x| máx, z mín, z máx) donde puede caminar con ese encuadre; ``None`` = no debe caminar."""
    return ESCENARIO_POR_CAMARA.get(camara or "full", ESCENARIO_POR_CAMARA["full"])


def _alcance(x0: float, z0: float, ux: float, uz: float, caja: tuple[float, float, float, float]) -> float:
    """Distancia que se puede recorrer desde (x0, z0) en la dirección (ux, uz) sin salir de la caja."""
    x_min, x_max, z_min, z_max = caja
    t = math.inf
    if ux > 1e-9:
        t = min(t, (x_max - x0) / ux)
    elif ux < -1e-9:
        t = min(t, (x_min - x0) / ux)
    if uz > 1e-9:
        t = min(t, (z_max - z0) / uz)
    elif uz < -1e-9:
        t = min(t, (z_min - z0) / uz)
    return max(0.0, t)


def tramo_en_escenario(origen: tuple[float, float], rumbo_deg: float, distancia: float,
                       limites: tuple[float, float, float], *, margen: float = MARGEN_BORDE,
                       minimo: float = DIST_MIN_PASEO + 0.05) -> tuple[tuple[float, float], float] | None:
    """Tramo recto desde ``origen`` en la dirección ``rumbo_deg`` (convenio de ``Rumbo``).

    Si hay sitio para al menos la mitad del paseo, camina hasta el borde como mucho; si no, rebota
    (reflexión especular, como una bola de billar): primero en la pared que alcanzaría antes, luego en
    la otra y, en una esquina, en las dos. Devuelve ``(destino, rumbo_final)`` o ``None`` si no caben
    ``minimo`` metros en ninguna de esas direcciones.
    """
    x_lim, z_min, z_max = limites
    caja = (-x_lim + margen, x_lim - margen, z_min + margen, z_max - margen)
    x0, z0 = _acotar(origen[0], caja[0], caja[1]), _acotar(origen[1], caja[2], caja[3])
    angulo = math.radians(rumbo_deg)
    ux, uz = math.sin(angulo), math.cos(angulo)
    suficiente = max(minimo, 0.5 * distancia)
    t_x = _alcance(x0, z0, ux, 0.0, caja) if abs(ux) > 1e-9 else math.inf     # hasta el lateral
    t_z = _alcance(x0, z0, 0.0, uz, caja) if abs(uz) > 1e-9 else math.inf     # hasta el frente o el fondo
    rebote_x, rebote_z = (-ux, uz), (ux, -uz)
    candidatos = [(ux, uz)] + ([rebote_x, rebote_z] if t_x <= t_z else [rebote_z, rebote_x]) + [(-ux, -uz)]
    alcances = [_alcance(x0, z0, cx, cz, caja) for cx, cz in candidatos]
    elegido = next((i for i, a in enumerate(alcances) if a >= suficiente), None)
    if elegido is None:
        elegido = max(range(len(candidatos)), key=lambda i: alcances[i])
    tramo = min(distancia, alcances[elegido])
    if tramo < minimo:
        return None
    ux, uz = candidatos[elegido]
    destino = (round(x0 + tramo * ux, 3), round(z0 + tramo * uz, 3))
    return destino, math.degrees(math.atan2(ux, uz)) % 360.0


def sanear_comando(cmd: Any, *, huesos: frozenset[str] | None = None,
                   expresiones: frozenset[str] | None = None, caminar: bool = True,
                   camara: bool = False) -> dict[str, Any] | None:
    """Valida un comando contra el protocolo del visor y lo acota. ``None`` si no es seguro o útil.

    - Solo tipos de ``TIPOS_PERMITIDOS`` (nunca ``speak``, ``sequence``, ``stop`` ni ``reset``).
    - Nombres de gestos, poses, miradas, manos y encuadres del protocolo (y de la personalidad).
    - Expresiones solo del modelo cargado (o estándar VRM si aún no se conocen).
    - ``bones`` solo con huesos de ``LIMITES_HUESO`` presentes en ``huesos`` y ángulos acotados;
      siempre absolutos (nunca ``relative``, que acumularía deriva).
    - ``move`` absoluto, dentro del escenario; sin ``caminar`` solo gira (``walk: false``).
    - ``desktop`` con su saneador propio (``sanear_desktop``): rechaza acciones fuera del protocolo
      (``show``/``hide``…) y campos fuera de rango (``seconds`` > 15, ``x`` fuera de [0, 1]…).
    """
    if not isinstance(cmd, Mapping):
        return None
    tipo = cmd.get("type")
    if tipo not in TIPOS_PERMITIDOS:
        return None
    if tipo == "expression":
        return _sanear_expresion(cmd, expresiones)
    if tipo == "pose":
        nombre = cmd.get("name")
        if nombre not in POSES_PERMITIDAS:
            return None
        return {"type": "pose", "name": nombre, "duration": round(_acotar(_num(cmd.get("duration"), 0.6) or 0.6,
                                                                          0.2, 3.0), 2)}
    if tipo == "gesture":
        nombre = cmd.get("name")
        if nombre not in GESTOS_PERMITIDOS:
            return None
        salida: dict[str, Any] = {"type": "gesture", "name": nombre}
        if cmd.get("side") in ("left", "right"):
            salida["side"] = cmd["side"]
        duracion = _num(cmd.get("duration"))
        if duracion is not None:
            salida["duration"] = round(_acotar(duracion, 0.5, 8.0), 2)
        return salida
    if tipo == "bones":
        return _sanear_huesos(cmd, huesos)
    if tipo == "hand":
        lado = cmd.get("side", "both")
        forma = cmd.get("shape")
        if lado not in ("left", "right", "both") or forma not in MANOS_VISOR:
            return None
        return {"type": "hand", "side": lado, "shape": forma}
    if tipo == "look":
        return _sanear_mirada(cmd)
    if tipo == "move":
        return _sanear_movimiento(cmd, caminar)
    if tipo == "camera":
        preset = cmd.get("preset")
        if not camara or preset not in CAMARAS_VISOR:
            return None
        return {"type": "camera", "preset": preset,
                "duration": round(_acotar(_num(cmd.get("duration"), 1.2) or 1.2, 0.3, 3.0), 2)}
    if tipo == "desktop":
        # Saneador propio (protocolo del visor): action ∈ {walk_to, peek, chase_cursor, seek_user, wander,
        # stop}; x ∈ [0, 1] (o unit "px" con 0 ≤ x ≤ 10000); edge left|right; seconds ≤ 15; speed 0,3..3.
        # Fuera de rango se rechaza (no se acota). Su disponibilidad (ventana visible) se decide al enviar.
        return sanear_desktop(cmd)
    # idle: nunca se apaga desde aquí
    intensidad = _num(cmd.get("intensity"), 1.0)
    return {"type": "idle", "enabled": True, "intensity": round(_acotar(intensidad or 0.0, 0.0, 2.0), 2),
            "blink": True}


def _sanear_expresion(cmd: Mapping[str, Any], expresiones: frozenset[str] | None) -> dict[str, Any] | None:
    validas = expresiones if expresiones is not None else EXPRESIONES_ESTANDAR
    salida: dict[str, Any] = {"type": "expression",
                              "duration": round(_acotar(_num(cmd.get("duration"), 0.4) or 0.0, 0.0, 5.0), 2)}
    exclusiva = cmd.get("exclusive") is not False
    if not exclusiva:
        salida["exclusive"] = False
    retener = _num(cmd.get("hold"))
    if retener is not None and retener > 0:
        salida["hold"] = round(_acotar(retener, 0.0, 10.0), 2)
    valores = cmd.get("values")
    if isinstance(valores, Mapping):
        filtrados = {str(n): round(_acotar(_num(v, 0.0) or 0.0, 0.0, 1.0), 3)
                     for n, v in valores.items() if str(n) in validas}
        if filtrados:
            salida["values"] = filtrados
            return salida
        if not exclusiva:
            return None
        salida.pop("hold", None)
        salida["name"] = "neutral"
        return salida
    nombre = str(cmd.get("name") or "")
    if nombre == "neutral":
        salida["name"] = "neutral"
        return salida
    if nombre not in validas:
        return None
    salida["name"] = nombre
    salida["value"] = round(_acotar(_num(cmd.get("value"), 1.0) or 0.0, 0.0, 1.0), 3)
    return salida


def _sanear_huesos(cmd: Mapping[str, Any], huesos: frozenset[str] | None) -> dict[str, Any] | None:
    rotaciones = cmd.get("rotations")
    if not isinstance(rotaciones, Mapping) or not huesos:
        return None
    limpias: dict[str, dict[str, float]] = {}
    for hueso, rot in rotaciones.items():
        limites = LIMITES_HUESO.get(str(hueso))
        if limites is None or str(hueso) not in huesos or not isinstance(rot, Mapping):
            continue
        ejes: dict[str, float] = {}
        for eje, (minimo, maximo) in zip(_EJES, limites):
            valor = _num(rot.get(eje))
            if valor is not None:
                ejes[eje] = round(_acotar(valor, minimo, maximo), 1)
        if ejes:
            limpias[str(hueso)] = ejes
    if not limpias:
        return None
    return {"type": "bones", "rotations": limpias,
            "duration": round(_acotar(_num(cmd.get("duration"), 0.8) or 0.8, 0.1, 3.0), 2)}


def _sanear_mirada(cmd: Mapping[str, Any]) -> dict[str, Any] | None:
    destino = cmd.get("at")
    if destino not in MIRADAS_PERMITIDAS:
        return None
    salida: dict[str, Any] = {"type": "look", "at": destino}
    cabeza = _num(cmd.get("head"))
    if cabeza is not None:
        salida["head"] = round(_acotar(cabeza, 0.0, 1.0), 2)
    if destino == "point":
        x, y, z = _num(cmd.get("x")), _num(cmd.get("y")), _num(cmd.get("z"))
        if x is None or y is None or z is None:
            return None
        salida.update(x=round(_acotar(x, -3.0, 3.0), 2), y=round(_acotar(y, 0.3, 2.2), 2),
                      z=round(_acotar(z, -3.0, 3.0), 2))
    return salida


def _sanear_movimiento(cmd: Mapping[str, Any], caminar: bool) -> dict[str, Any] | None:
    salida: dict[str, Any] = {"type": "move"}
    x, z, giro = _num(cmd.get("x")), _num(cmd.get("z")), _num(cmd.get("rotation"))
    desplaza = caminar and (x is not None or z is not None)
    if desplaza:
        if x is not None:
            salida["x"] = round(_acotar(x, -LIMITE_X_ABS, LIMITE_X_ABS), 3)
        if z is not None:
            salida["z"] = round(_acotar(z, LIMITE_Z_ABS[0], LIMITE_Z_ABS[1]), 3)
    if giro is not None:
        salida["rotation"] = round(_acotar(giro, -180.0, 180.0), 1)
    if len(salida) == 1:
        return None
    salida["walk"] = bool(desplaza and cmd.get("walk"))
    salida["duration"] = round(_acotar(_num(cmd.get("duration"), 1.0) or 1.0, 0.3, 8.0), 2)
    return salida


def _duracion_comando(cmd: Mapping[str, Any]) -> float:
    """Segundos que tarda el visor en completar un comando (estimado, para el fin del plan)."""
    tipo = cmd.get("type")
    if tipo == "gesture":
        return float(cmd.get("duration") or DURACION_GESTO.get(str(cmd.get("name")), 1.5))
    if tipo == "expression":
        return float(cmd.get("duration") or 0.0) + float(cmd.get("hold") or 0.0)
    if tipo in ("move", "pose", "bones", "camera"):
        return float(cmd.get("duration") or 0.6)
    if tipo == "desktop":
        return float(cmd.get("seconds") or DURACION_DESKTOP.get(str(cmd.get("action")), 3.0))
    return 0.0


def _enfriamiento(conducta: str) -> float:
    """Enfriamiento (s) de una conducta de escenario o de escritorio (10 s si no figura)."""
    if conducta in ENFRIAMIENTO_S:
        return ENFRIAMIENTO_S[conducta]
    return ENFRIAMIENTO_ESCRITORIO_S.get(conducta, 10.0)


def _desktop(accion: str, **campos: Any) -> dict[str, Any]:
    return {"type": "desktop", "action": accion, **campos}


def _rumbo_de_marcha(pasos: Sequence[Paso], origen: tuple[float, float]) -> float | None:
    """Dirección (grados, convenio de ``Rumbo``) del primer ``move`` que camina, desde ``origen``."""
    for paso in pasos:
        for cmd in paso.comandos:
            if cmd.get("type") != "move" or not cmd.get("walk"):
                continue
            dx = float(cmd.get("x", origen[0])) - origen[0]
            dz = float(cmd.get("z", origen[1])) - origen[1]
            if math.hypot(dx, dz) < 0.05:
                return None
            return round(math.degrees(math.atan2(dx, dz)) % 360.0, 3)
    return None


def _p(t: float, *comandos: dict[str, Any] | None, final: bool = False, reposo: str | None = None) -> Paso:
    return Paso(round(max(0.0, t), 3), tuple(c for c in comandos if c), final, reposo)


def _mirar(destino: str, cabeza: float = 0.35) -> dict[str, Any]:
    return {"type": "look", "at": destino, "head": round(cabeza, 2)}


def _mirar_punto(punto: tuple[float, float, float], cabeza: float = MIRADA_CABEZA) -> dict[str, Any]:
    return {"type": "look", "at": "point", "x": round(punto[0], 2), "y": round(punto[1], 2),
            "z": round(punto[2], 2), "head": round(cabeza, 2)}


def _mirar_al_usuario(ctx: ContextoMotor, cabeza: float = MIRADA_CABEZA) -> dict[str, Any]:
    """``look`` al usuario: a su punto (visión continua) si se conoce; si no, a cámara."""
    return _mirar("camera", cabeza) if ctx.usuario is None else _mirar_punto(ctx.usuario, cabeza)


def _girar(rotacion: float, duracion: float = 0.8) -> dict[str, Any]:
    return {"type": "move", "rotation": round(rotacion, 1), "walk": False, "duration": round(duracion, 2)}


def _pose(nombre: str, duracion: float = 0.7) -> dict[str, Any]:
    return {"type": "pose", "name": nombre, "duration": round(duracion, 2)}


def _gesto(nombre: str, lado: str | None = None) -> dict[str, Any]:
    cmd: dict[str, Any] = {"type": "gesture", "name": nombre}
    if lado:
        cmd["side"] = lado
    return cmd


def _huesos(rotaciones: Mapping[str, Mapping[str, float]], duracion: float = 0.8) -> dict[str, Any]:
    return {"type": "bones", "rotations": {b: {e: round(v, 1) for e, v in r.items()} for b, r in rotaciones.items()},
            "duration": round(duracion, 2)}


# ---------------------------------------------------------------------------
# Corteza motora
# ---------------------------------------------------------------------------


class CortezaMotora:
    """El conectoma gobierna el cuerpo del avatar 3D: cuándo y cómo se mueve, gesticula y pone caras."""

    def __init__(self, settings: Settings, bus: BusEventos | None, avatar: Any, *, semilla: int | None = None,
                 reloj: Callable[[], float] | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.avatar = avatar
        self._rng = random.Random(semilla)
        self._reloj: Callable[[], float] = reloj or time.monotonic
        self.actividad = _acotar(_num(getattr(settings, "avatar_actividad", 1.0), 1.0) or 0.0, 0.0, 3.0)
        self.caminar = bool(getattr(settings, "avatar_caminar", True))
        self.camara_permitida = bool(getattr(settings, "avatar_camara", False))
        self._estado = HomeostaticState.neutral()
        # Memoria de decisión (inmutable; solo la reemplazan tick y la E/S)
        self._memoria = MemoriaMotora(semilla=semilla if semilla is not None else self._rng.getrandbits(48))
        self._plan_actual: str | None = None
        self._plan_cara = False
        self._agenda: list[_Pendiente] = []
        self._diferidos: list[Paso] = []
        self._enviados: deque[float] = deque()
        self._envio_tras_mov: float | None = None    # primer envío después del último movimiento
        # Modelo del cuerpo (lo que la corteza cree que está haciendo el avatar)
        self._pos: tuple[float, float] = (0.0, 0.0)
        self._mov: tuple[float, float, tuple[float, float], tuple[float, float]] | None = None
        self._rot = 0.0
        self._fin_ultimo_mov = -math.inf
        self._pose = "relaxed"
        self._camara: str | None = None
        self._t_camara = -math.inf
        # Cara e idle
        self._exp_reposo: str | None = None
        self._t_reposo = -math.inf
        self._forzar_reposo = True
        self._idle: float | None = None
        self._t_idle = -math.inf
        # Eventos
        self._ultimo_usuario = -math.inf
        self._atencion_pendiente = False
        self._habla_inminente_hasta = -math.inf
        self._voz_desde: float | None = None
        self._hablaba = False
        self._habla_gobierna: frozenset[str] = frozenset()   # tipos que trae el MOTION del habla en curso
        self._gesto_hasta = -math.inf                         # fin estimado del último gesto suyo
        self._steiner_hasta = -math.inf
        self._ultimo_steiner = -math.inf
        self._apagando = False
        self._cerrado = False
        self._era_en_linea = False
        # Modo escritorio (v0.2): visibilidad, solicitudes de main, no molestar y travesuras
        self.escritorio_habilitado = bool(getattr(settings, "tiene_escritorio", False))
        self._escritorio_visible = False
        self._overlay_conectado: bool | None = None   # None = aún no se sabe
        self._saliendo = False                         # pidió esconderse y aún no llegó "ocultada"
        self._t_mostrada = -math.inf                   # última vez que pasó a visible (entrada por el borde)
        self._ceder_cuerpo = False                     # entra o sale por el borde: el tic corta la conducta
        self._atencion_escritorio = False              # la atención pendiente viene del chat de escritorio
        self._desktop_hasta = -math.inf                # fin estimado de la locomoción de escritorio en curso
        self._solicitud: SolicitudMotor | None = None
        self._no_molestar = False
        self._fatiga_alta_desde: float | None = None
        self._escondida_por_fatiga = False
        self._reaccion: tuple[str, dict[str, Any], float] | None = None
        self._t_reaccion = -math.inf
        self._travesuras_activas = False
        self._travesuras: deque[float] = deque()       # instantes de las travesuras propuestas (24 h)
        self._travesura_proxima = -math.inf
        # Mirada al usuario (visión continua)
        self._mirada: MiradaUsuario | None = None
        self._punto_mirada: tuple[float, float, float] | None = None   # último look point enviado
        self._t_mirada_usuario = -math.inf
        self._fichas_mirada = float(MIRADA_RAFAGA)    # cubo de fichas del sub-tope de la mirada
        self._t_fichas_mirada: float | None = None
        self._rostro_frontal = False
        self._mirar_usuario_hasta = -math.inf
        self._t_mirar_usuario = -math.inf
        # Diagnóstico
        self._diag: tuple[EstadoModulo, str] | None = None
        self._desuscribir: list[Callable[[], None]] = []
        if not self.activa:
            return
        self._poner_reposo_externo(True)
        if bus is not None:
            self._desuscribir = [
                bus.suscribir("homeostasis.estado", self._al_homeostasis),
                bus.suscribir("homeostasis.cambio_linea", self._al_cambio_linea),
                bus.suscribir("usuario.entrada", self._al_usuario),
                bus.suscribir("salida.subtitulo", self._al_subtitulo),
                bus.suscribir("voz.inicio", self._al_voz_inicio),
                bus.suscribir("voz.fin", self._al_voz_fin),
                bus.suscribir("sistema.apagado", self._al_apagado),
                # Visión continua (si está apagada nadie publica estos eventos).
                bus.suscribir("vision.rostro", self._al_vision_rostro),
                bus.suscribir("vision.mirada", self._al_vision_mirada),
            ]
            if self.escritorio_habilitado:   # nunca "escritorio.*" (regla del bus v0.2): uno a uno
                self._desuscribir += [
                    bus.suscribir("escritorio.conectado", self._al_escritorio_conectado),
                    bus.suscribir("escritorio.evento", self._al_escritorio_evento),
                    bus.suscribir("motor.solicitud", self._al_solicitud),
                    bus.suscribir("motor.travesuras", self._al_travesuras),
                    bus.suscribir("pc.no_molestar", self._al_no_molestar),
                ]
                self._estado_inicial_escritorio(bus)

    # ------------------------------------------------------------------ propiedades
    @property
    def activa(self) -> bool:
        """``True`` si gobierna el cuerpo (hay avatar, actividad > 0 y no se cerró)."""
        return self.avatar is not None and self.actividad > 0.0 and not self._cerrado

    @property
    def estado_homeostatico(self) -> HomeostaticState:
        """Último estado recibido del conectoma (neutral hasta el primero)."""
        return self._estado

    @property
    def plan_actual(self) -> str | None:
        """Conducta en curso (``None`` si ninguna)."""
        return self._plan_actual

    @property
    def memoria(self) -> MemoriaMotora:
        """Memoria de decisión actual (inmutable)."""
        return self._memoria

    def _aplazar(self, hasta: float) -> None:
        """No decide otra conducta antes de ``hasta``."""
        if hasta > self._memoria.proxima:
            self._memoria = replace(self._memoria, proxima=hasta)

    def _poner_reposo_externo(self, valor: bool) -> None:
        try:
            self.avatar.reposo_externo = valor
        except Exception as exc:  # avatar sin ese atributo (o de solo lectura)
            log.debug("No se pudo fijar reposo_externo=%s: %s", valor, type(exc).__name__)

    # ------------------------------------------------------------------ diagnóstico
    def _estado_diag(self) -> tuple[EstadoModulo, str]:
        if self.actividad <= 0.0:
            return EstadoModulo.OFF, "actividad 0 (quieta)"
        if self._cerrado or self.avatar is None:
            return EstadoModulo.OFF, "cerrada"
        if not self._avatar_en_linea():
            return EstadoModulo.EN_ESPERA, "esperando al avatar 3D"
        partes = [f"autónoma · actividad ×{formatear_decimal(self.actividad, 1)}",
                  "camina" if self.caminar else "sin caminar"]
        if self.camara_permitida:
            partes.append("cámara")
        if self.escritorio_habilitado:
            partes.append("escritorio")
        return EstadoModulo.OK, " · ".join(partes)

    def diagnostico(self) -> DiagnosticoModulo:
        """Estado actual para el arranque y ``sistema.modulo`` (clave ``"motor"``)."""
        estado, detalle = self._estado_diag()
        return DiagnosticoModulo(CLAVE, NOMBRE, estado, detalle)

    def _publicar_diag(self) -> None:
        actual = self._estado_diag()
        if actual == self._diag:
            return
        self._diag = actual
        if self.bus is not None:
            try:
                self.bus.publicar("sistema.modulo", self.diagnostico().a_dict(), origen="corteza_motora")
            except Exception as exc:
                log.debug("No se pudo publicar el estado de la corteza motora: %s", type(exc).__name__)
        log.info("Corteza motora: %s (%s)", actual[0].value, actual[1])

    def _avatar_en_linea(self) -> bool:
        try:
            return bool(getattr(self.avatar, "en_linea", False))
        except Exception as exc:
            log.debug("Avatar sin en_linea legible: %s", type(exc).__name__)
            return False

    # ------------------------------------------------------------------ ritmo
    def intervalo_medio(self, estado: HomeostaticState) -> float:
        """Segundos medios entre conductas: ∝ 1 / (actividad × arousal); la fatiga, el foco alto y el
        modo reposo lo alargan. ``inf`` con actividad 0."""
        if self.actividad <= 0.0:
            return math.inf
        base = INTERVALO_BASE_S / (self.actividad * (0.3 + 1.2 * estado.arousal))
        base *= 1.0 + 0.8 * estado.fatiga
        base *= 1.0 + max(0.0, estado.focus - 0.7)
        if estado.modo == "reposo":
            base *= 2.5
        return _acotar(base, INTERVALO_MIN_S, INTERVALO_MAX_S)

    def tasa_conductas(self, estado: HomeostaticState) -> float:
        """Conductas por minuto esperadas para ese estado (0 con actividad 0)."""
        intervalo = self.intervalo_medio(estado)
        return 0.0 if math.isinf(intervalo) else 60.0 / intervalo

    def intensidad_idle(self, estado: HomeostaticState) -> float:
        """Intensidad del reposo del visor (respiración y balanceo) según arousal y fatiga."""
        if estado.modo == "reposo":
            return 0.3
        return round(_acotar(0.35 + 1.1 * estado.arousal - 0.35 * estado.fatiga, 0.2, 1.6), 1)

    def exp_reposo(self, estado: HomeostaticState) -> str:
        """EXP de la cara en reposo (las mismas reglas que las etiquetas sugeridas, §4.4.3)."""
        if estado.modo == "reposo":
            return "somnolienta"
        if sugerir_etiquetas is None:
            return "neutral"
        try:
            return sugerir_etiquetas(estado).exp
        except Exception as exc:
            log.debug("sugerir_etiquetas falló: %s", type(exc).__name__)
            return "neutral"

    @staticmethod
    def rumbo(estado: HomeostaticState) -> Rumbo:
        """Lectura del bump E-PG (sin ruido). Sin bump usa ``rumbo_deg`` con coherencia = focus."""
        rumbo = rumbo_del_bump(estado.bump, estado.rumbo_deg)
        if not estado.bump:
            return Rumbo(rumbo.angulo, _acotar(estado.focus, 0.0, 1.0))
        return rumbo

    def brujula(self, estado: HomeostaticState, memoria: MemoriaMotora | None = None,
                rng: random.Random | None = None) -> Brujula:
        """Rumbo de marcha propuesto por integración de trayectoria (no modifica la memoria).

        El bump se usa como rumbo **relativo**: lo que gira desde que se fijó el último rumbo de
        marcha (``deriva``) lo gira a ella igual; su fase la atrae un poco (``GANANCIA_FASE`` ×
        coherencia) y el ruido de exploración crece con el aburrimiento, la curiosidad y la
        incoherencia del bump. Sin rumbo recordado parte de la fase del bump.
        """
        mem = self._memoria if memoria is None else memoria
        r = self._rng if rng is None else rng
        lectura = self.rumbo(estado)
        if mem.rumbo_marcha is None:
            base, deriva, atraccion = lectura.angulo, 0.0, 0.0
        else:
            base = mem.rumbo_marcha
            deriva = 0.0 if mem.bump_ref is None else GANANCIA_DERIVA * envolver_180(lectura.angulo - mem.bump_ref)
            atraccion = GANANCIA_FASE * lectura.coherencia * envolver_180(lectura.angulo - (base + deriva))
        sigma = (RUIDO_RUMBO_BASE + RUIDO_RUMBO_ABURRIMIENTO * estado.boredom
                 + RUIDO_RUMBO_CURIOSIDAD * estado.curiosidad + RUIDO_RUMBO_INCOHERENCIA * (1.0 - lectura.coherencia))
        angulo = (base + deriva + atraccion + r.gauss(0.0, sigma)) % 360.0
        return Brujula(round(angulo, 3), lectura.coherencia, lectura.angulo, round(deriva, 3))

    # ------------------------------------------------------------------ pesos de las conductas
    def pesos_base(self, estado: HomeostaticState, ctx: ContextoMotor) -> dict[str, float]:
        """Peso de cada conducta para el estado (puro: sin enfriamientos ni azar). Solo pesos > 0."""
        a, f, b, t = estado.arousal, estado.focus, estado.boredom, estado.fatiga
        c, v = estado.curiosidad, estado.valencia
        reposo = estado.modo == "reposo"
        atencion = ctx.desde_usuario_s < ATENCION_S
        andar = self.caminar and limites_escenario(ctx.camara) is not None
        dist = math.hypot(*ctx.posicion)
        p: dict[str, float] = {
            "mirada": 0.35 + 0.4 * c + 0.2 * b,
            "seguir_raton": (0.15 + 0.35 * a + 0.3 * max(v, 0.0)) * (1.6 if ctx.desde_usuario_s < 180.0 else 1.0),
            "microexpresion": 0.3 + 0.2 * a,
            "micro_postura": (0.3 + 0.25 * b) if ctx.huesos else 0.0,
            "manos": 0.15 + 0.15 * a,
            "pasear": (0.15 + 1.5 * b + 0.7 * c + 0.25 * a - 2.0 * max(0.0, f - 0.6)
                       - 1.0 * max(0.0, t - 0.4)) if andar else 0.0,
            "girar": 0.2 + 0.4 * c + 0.3 * b + (0.0 if andar else 0.4) - max(0.0, f - 0.7),
            "volver_al_centro": (1.5 * max(0.0, dist - 0.6) + (2.0 if f > 0.75 and dist > 0.45 else 0.0))
            if andar else 0.0,
            "mirar_alrededor": 0.05 + 1.6 * c + 0.3 * b - 0.5 * max(0.0, f - 0.7),
            "ladear_cabeza": 0.1 + 1.1 * c + 0.2 * max(v, 0.0),
            "pensativa": (3.0 * max(0.0, f - 0.6) + 0.15 * c) * (0.5 if a > 0.9 else 1.0),
            "brazos_cruzados": 0.05 + 1.8 * max(0.0, -v - 0.1) + 1.5 * max(0.0, f - 0.7) + 0.3 * max(0.0, a - 0.6),
            "estirarse": 2.2 * max(0.0, t - 0.45) + 0.4 * b * t,
            "bostezar": 2.4 * max(0.0, t - 0.5) + (1.0 if reposo else 0.0),
            "dormitar": 2.5 if reposo else 1.6 * max(0.0, t - 0.75),
            "encogerse": 1.4 * max(0.0, b - 0.4) + 0.4 * max(0.0, -v) * b,
            "asentir": 0.05 + 0.4 * max(0.0, v) * f,
            "alegria": 4.5 * max(0.0, v - 0.3) * max(0.0, a - 0.4),
            "tsundere": 1.2 * max(0.0, v - 0.15) * a + (0.6 * max(0.0, v) if ctx.desde_usuario_s < 60.0 else 0.0),
            "postura": 0.05 + 0.6 * max(0.0, v) * a,
            "camara": 0.2 if self.camara_permitida else 0.0,
        }
        if ctx.sin_escenario:
            # En el escritorio el visor ignora move x/z (y un paseo de escenario la dejaría andando en el sitio
            # o de espaldas): la locomoción va por "desktop" (siempre solo) o no hay locomoción.
            p["pasear"] = p["volver_al_centro"] = 0.0
        if ctx.escritorio:
            p.update({
                "pasear_escritorio": 0.15 + 1.4 * b + 0.6 * c + 0.25 * a - 2.0 * max(0.0, f - 0.6)
                - 1.0 * max(0.0, t - 0.4),
                "asomarse": 0.1 + 0.6 * c + 0.3 * b - 0.5 * max(0.0, t - 0.6),
                "perseguir_cursor": 1.6 * max(0.0, b - 0.5) + 0.4 * max(0.0, v) * a - 1.0 * max(0.0, t - 0.5),
                "buscar_usuario": (1.2 * max(0.0, b - 0.55) + 0.3 * c)
                if ctx.desde_usuario_s > USUARIO_OLVIDADO_S else 0.0,
            })
        if atencion:
            for nombre in _SIN_ATENCION | set(CONDUCTAS_ESCRITORIO):
                p[nombre] = 0.0
        if reposo:
            for nombre in p:
                if nombre not in _EN_REPOSO:
                    p[nombre] *= 0.15
        return {n: round(w, 4) for n, w in p.items() if w > 1e-6}

    def preferidas(self, estado: HomeostaticState, ctx: ContextoMotor | None = None, n: int = 3) -> list[str]:
        """Las ``n`` conductas de más peso para ese estado (sin enfriamientos)."""
        pesos = self.pesos_base(estado, ctx or ContextoMotor())
        return [nombre for nombre, _ in sorted(pesos.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

    def pesos(self, estado: HomeostaticState, ctx: ContextoMotor, ahora: float,
              memoria: MemoriaMotora | None = None) -> dict[str, float]:
        """``pesos_base`` sin las conductas en enfriamiento ni la última elegida (sin repeticiones)."""
        mem = self._memoria if memoria is None else memoria
        salida = {}
        for nombre, peso in self.pesos_base(estado, ctx).items():
            if nombre == mem.ultima:
                continue
            if ahora - mem.uso(nombre) < _enfriamiento(nombre):
                continue
            salida[nombre] = peso
        return salida

    def _elegir(self, pesos: Mapping[str, float], rng: random.Random | None = None) -> str:
        r = self._rng if rng is None else rng
        total = sum(pesos.values())
        umbral = r.random() * total
        acumulado = 0.0
        nombre = ""
        for nombre, peso in pesos.items():
            acumulado += peso
            if umbral < acumulado:
                return nombre
        return nombre

    # ------------------------------------------------------------------ decisión (núcleo puro)
    def decidir(self, estado: HomeostaticState, ahora: float, ctx: ContextoMotor,
                memoria: MemoriaMotora | None = None) -> Decision:
        """Núcleo puro de la decisión: (estado, reloj, contexto, memoria) → ``Decision``.

        No hace E/S, no lee el avatar ni modifica la corteza: solo usa sus ajustes fijos
        (actividad, caminar, cámara) y lo que recibe; ``memoria`` por defecto es la actual, que se
        lee pero no se cambia. Las mismas entradas dan siempre la misma ``Decision`` (el azar sale
        de ``memoria.azar()``). Respeta: actividad 0, avatar fuera de línea, habla en curso, el plan
        anterior, el intervalo según arousal × actividad, enfriamientos, no repetir y el tope de
        comandos (``ctx.presupuesto`` menos la reserva prioritaria). La memoria devuelta registra la
        decisión (enfriamiento, última conducta, próxima decisión y, si camina, el rumbo de marcha).
        """
        mem = self._memoria if memoria is None else memoria
        if self.actividad <= 0.0 or not ctx.en_linea or ctx.hablando:
            return Decision(None, mem)
        if ahora < mem.proxima or ahora < mem.plan_hasta:
            return Decision(None, mem)
        r = mem.azar()
        siguiente = replace(mem, n=mem.n + 1)
        brujula = self.brujula(estado, mem, r)
        plan: Plan | None = None
        if self._debe_volver(estado, ctx, ahora, mem):
            plan = self.construir("volver_al_centro", estado, ctx, brujula=brujula, rng=r)
        if plan is None:
            candidatos = self.pesos(estado, ctx, ahora, mem)
            while candidatos and plan is None:
                nombre = self._elegir(candidatos, r)
                plan = self.construir(nombre, estado, ctx, brujula=brujula, rng=r)
                if plan is None:
                    candidatos.pop(nombre, None)
        if plan is None:
            espera = self.intervalo_medio(estado) * r.uniform(0.6, 1.4)
            return Decision(None, replace(siguiente, proxima=ahora + espera))
        if len(plan.comandos()) > ctx.presupuesto - RESERVA_PRIORITARIA:
            return Decision(None, replace(siguiente, proxima=ahora + 2.0))
        siguiente = replace(siguiente.con_uso(plan.conducta, ahora), ultima=plan.conducta,
                            proxima=ahora + plan.duracion + self.intervalo_medio(estado) * r.uniform(0.6, 1.4),
                            plan_hasta=ahora + plan.duracion)
        if plan.rumbo is not None:
            siguiente = replace(siguiente, rumbo_marcha=plan.rumbo, bump_ref=brujula.bump)
        return Decision(plan, siguiente)

    def _debe_volver(self, estado: HomeostaticState, ctx: ContextoMotor, ahora: float,
                     memoria: MemoriaMotora) -> bool:
        if ctx.sin_escenario or not self.caminar or limites_escenario(ctx.camara) is None:
            return False
        if math.hypot(*ctx.posicion) <= 0.45:
            return False
        if ahora - memoria.uso("volver_al_centro") < ENFRIAMIENTO_S["volver_al_centro"]:
            return False
        # Si le escriben por el chat de escritorio la atención es sin acercarse: no vuelve al centro por eso.
        return estado.focus >= 0.75 or (ctx.desde_usuario_s < ATENCION_S and not ctx.atencion_escritorio)

    def construir(self, conducta: str, estado: HomeostaticState, ctx: ContextoMotor, *,
                  brujula: Brujula | None = None, rng: random.Random | None = None) -> Plan | None:
        """Construye (y sanea) el plan de una conducta concreta; ``None`` si no aplica ahora.

        ``rng`` es el generador de la decisión (por defecto, el de la corteza) y ``brujula`` el rumbo
        de marcha propuesto (por defecto, el de la memoria actual)."""
        constructor = getattr(self, f"_b_{conducta}", None)
        if constructor is None:
            return None
        r = self._rng if rng is None else rng
        try:
            pasos = constructor(estado, ctx, brujula or self.brujula(estado, rng=r), r)
        except Exception as exc:
            log.debug("La conducta %s falló al construirse: %s", conducta, type(exc).__name__, exc_info=True)
            return None
        return self._plan(conducta, pasos, ctx)

    def _plan(self, conducta: str, pasos: Sequence[Paso], ctx: ContextoMotor) -> Plan | None:
        limpios: list[Paso] = []
        for paso in sorted(pasos, key=lambda p: p.t):
            comandos = []
            for cmd in paso.comandos:
                limpio = sanear_comando(cmd, huesos=ctx.huesos, expresiones=ctx.expresiones,
                                        caminar=self.caminar, camara=self.camara_permitida)
                if limpio is not None and limpio not in comandos:
                    comandos.append(limpio)
            if comandos:
                limpios.append(Paso(paso.t, tuple(comandos), paso.final, paso.reposo))
        if not any(not p.final for p in limpios):
            return None
        duracion = max(p.t + max((_duracion_comando(c) for c in p.comandos), default=0.0) for p in limpios)
        return Plan(conducta, tuple(limpios), round(duracion, 3), _rumbo_de_marcha(limpios, ctx.posicion))

    # ------------------------------------------------------------------ ayudas de construcción
    def _mezcla(self, exp: str, ctx: ContextoMotor, escala: float = 1.0,
                duracion: float = 0.3) -> dict[str, Any] | None:
        disponibles = ctx.expresiones if ctx.expresiones is not None else EXPRESIONES_ESTANDAR
        mezcla = mezcla_para(exp, disponibles)
        if not mezcla:
            return None
        return {"type": "expression", "values": {k: round(v * escala, 3) for k, v in mezcla.items()},
                "duration": duracion}

    def _paso_reposo(self, t: float, estado: HomeostaticState, ctx: ContextoMotor) -> Paso:
        exp = self.exp_reposo(estado)
        return _p(t, comando_expresion(exp, ctx.expresiones, duracion=0.8), final=True, reposo=exp)

    @staticmethod
    def _lado(lateral: float, rng: random.Random, umbral: float = 0.15) -> str:
        """``left``/``right`` del avatar (su izquierda = x + del escenario) según la componente lateral."""
        if lateral > umbral:
            return "left"
        if lateral < -umbral:
            return "right"
        return rng.choice(("left", "right"))

    def _base_hueso(self, pose: str, hueso: str, eje: str) -> float:
        return float(BASE_POSE.get(pose, {}).get(hueso, {}).get(eje, 0.0))

    def _micro_huesos(self, delta: Mapping[str, Mapping[str, float]], ctx: ContextoMotor, retener: float,
                      duracion: float = 0.9, vuelta: float = 0.9) -> list[Paso]:
        """Micro-postura sobre la base de la pose actual y su restauración (paso final)."""
        objetivo = {h: {e: self._base_hueso(ctx.pose, h, e) + d for e, d in ejes.items()} for h, ejes in delta.items()}
        restaurar = {h: {e: self._base_hueso(ctx.pose, h, e) for e in ejes} for h, ejes in delta.items()}
        return [_p(0.0, _huesos(objetivo, duracion)), _p(retener, _huesos(restaurar, vuelta), final=True)]

    def _caminar_a(self, destino: tuple[float, float], estado: HomeostaticState, ctx: ContextoMotor, *,
                   giro_final: float = 0.0) -> list[Paso]:
        """Gira hacia el destino, camina (los ojos se adelantan) y vuelve a quedar de frente."""
        x0, z0 = ctx.posicion
        dx, dz = destino[0] - x0, destino[1] - z0
        distancia = math.hypot(dx, dz)
        if distancia < DIST_MIN_PASEO:
            return []
        rumbo = math.degrees(math.atan2(dx, dz))
        t_giro = 0.35 + abs(envolver_180(rumbo - ctx.rotacion)) / 360.0
        velocidad = VEL_MIN + (VEL_MAX - VEL_MIN) * estado.arousal
        t_camino = _acotar(distancia / velocidad, 0.6, 6.0)
        lado = "left" if dx > 0 else "right"
        llegada = 0.15 + t_giro + t_camino
        return [
            _p(0.0, _mirar(lado, 0.3)),
            _p(0.15, _girar(rumbo, t_giro)),
            _p(0.15 + t_giro, {"type": "move", "x": round(destino[0], 3), "z": round(destino[1], 3), "walk": True,
                               "duration": round(t_camino, 2)}, _mirar("forward", 0.2)),
            _p(llegada + 0.3, _girar(giro_final, 0.6), final=True),
            _p(llegada + 0.6, _mirar("camera", 0.35), final=True),
        ]

    # ------------------------------------------------------------------ repertorio
    def _b_mirada(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        lateral = math.sin(math.radians(brujula.angulo))
        if r.random() < 0.3:
            x0, z0 = ctx.posicion
            cmd = {"type": "look", "at": "point", "x": round(x0 + 1.6 * lateral + r.uniform(-0.4, 0.4), 2),
                   "y": round(r.uniform(1.0, 1.8) - 0.4 * e.fatiga, 2), "z": round(z0 + r.uniform(1.0, 2.2), 2),
                   "head": round(r.uniform(0.15, 0.4), 2)}
        else:
            lado = self._lado(lateral, r)
            if e.fatiga > 0.6 or e.valencia < -0.3:
                vertical = "down_"
            elif e.curiosidad > 0.5 or r.random() < 0.3:
                vertical = "up_"
            else:
                vertical = ""
            cmd = _mirar(vertical + lado, r.uniform(0.2, 0.5))
        retener = r.uniform(0.8, 2.6) * (1.0 + 0.5 * e.curiosidad)
        return [_p(0.0, cmd), _p(retener, _mirar("camera"), final=True)]

    def _b_seguir_raton(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                        r: random.Random) -> list[Paso]:
        return [_p(0.0, _mirar("mouse", 0.45)), _p(r.uniform(4.0, 10.0), _mirar("camera"), final=True)]

    def _b_microexpresion(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                          r: random.Random) -> list[Paso]:
        candidatas: list[str] = []
        if e.valencia > 0.25:
            candidatas += ["smug", "feliz"]
        if e.valencia < -0.25:
            candidatas += ["molesta", "seria"]
        if e.curiosidad > 0.45:
            candidatas.append("curiosa")
        if e.arousal > 0.8 and e.focus < 0.5:
            candidatas.append("sorprendida")
        if e.focus > 0.7:
            candidatas += ["pensativa", "seria"]
        if e.boredom > 0.6:
            candidatas.append("aburrida")
        if not candidatas:
            candidatas = ["smug", "pensativa", "curiosa"]
        cara = self._mezcla(r.choice(candidatas), ctx, escala=r.uniform(0.45, 0.8), duracion=0.25)
        if cara is None:
            return []
        return [_p(0.0, cara), self._paso_reposo(r.uniform(0.7, 1.6), e, ctx)]

    def _b_micro_postura(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                         r: random.Random) -> list[Paso]:
        h = ctx.huesos or frozenset()
        variantes: list[tuple[str, float]] = []
        if {"hips", "spine"} <= h:
            variantes.append(("peso", 1.0 + e.boredom))
        if "spine" in h:
            variantes.append(("torso", 0.6 + 0.6 * e.focus))
        if "head" in h:
            variantes.append(("cabeza", 1.0 + e.curiosidad))
        if {"leftShoulder", "rightShoulder"} <= h:
            variantes.append(("hombros", 0.4))
        if "head" in h and (e.fatiga > 0.5 or e.valencia < -0.3):
            variantes.append(("cabeza_baja", 1.0 + e.fatiga))
        if not variantes:
            return []
        variante = self._elegir(dict(variantes), r)
        s = r.choice((-1.0, 1.0))
        delta: dict[str, dict[str, float]]
        if variante == "peso":          # apoyar el peso en una pierna
            delta = {"hips": {"z": s * r.uniform(2.0, 4.0)}, "spine": {"z": -s * r.uniform(1.5, 3.0)}}
        elif variante == "torso":       # inclinarse (foco) o erguirse (orgullo)
            delta = {"spine": {"x": r.uniform(2.0, 5.0) if (e.focus > 0.6 or r.random() < 0.5)
                               else -r.uniform(2.0, 4.0)}}
        elif variante == "cabeza":      # ladear apenas la cabeza
            delta = {"head": {"z": s * r.uniform(3.0, 7.0)}}
            if "neck" in h:
                delta["neck"] = {"z": s * r.uniform(1.0, 3.0)}
        elif variante == "hombros":     # hombros un poco arriba (tensión contenida)
            delta = {"leftShoulder": {"z": r.uniform(2.0, 5.0)}, "rightShoulder": {"z": -r.uniform(2.0, 5.0)}}
        else:                           # cabeza baja (cansada o molesta)
            delta = {"head": {"x": r.uniform(4.0, 9.0)}}
        return self._micro_huesos(delta, ctx, r.uniform(3.0, 9.0), duracion=r.uniform(0.6, 1.2))

    def _b_manos(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        lado = r.choice(("left", "right"))
        if e.valencia < -0.3 and e.arousal > 0.6:
            forma = "fist"
        elif e.valencia > 0.6 and r.random() < 0.05:
            forma = r.choice(("peace", "thumbs_up"))       # muy raro: es Kurisu
        else:
            forma = r.choice(("open", "open", "relaxed", "fist"))
        base = MANO_POSE.get(ctx.pose, "relaxed")
        if forma == base:
            forma = "open" if base != "open" else "relaxed"
        return [_p(0.0, {"type": "hand", "side": lado, "shape": forma}),
                _p(r.uniform(1.5, 3.0), {"type": "hand", "side": lado, "shape": base}, final=True)]

    def _b_pasear(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        """Paseo por integración de trayectoria: avanza **desde donde está** en el rumbo de marcha
        propuesto, una distancia que crece con el aburrimiento, la curiosidad y el arousal, sin salir
        del escenario (rebota en los bordes). Si está arrinconada, sale hacia el centro."""
        limites = limites_escenario(ctx.camara)
        if not self.caminar or limites is None:
            return []
        impulso = _acotar(0.25 + 0.5 * e.boredom + 0.3 * e.curiosidad + 0.2 * e.arousal + r.uniform(-0.2, 0.2),
                          0.0, 1.0)
        distancia = PASEO_MIN + (PASEO_MAX - PASEO_MIN) * impulso
        tramo = tramo_en_escenario(ctx.posicion, brujula.angulo, distancia, limites)
        if tramo is None:
            x0, z0 = ctx.posicion
            al_centro = math.degrees(math.atan2(-x0, (limites[1] + limites[2]) / 2.0 - z0)) + r.uniform(-30.0, 30.0)
            tramo = tramo_en_escenario(ctx.posicion, al_centro, distancia, limites)
        if tramo is None:
            return []
        destino, rumbo = tramo
        giro_final = 0.0
        if (e.curiosidad > 0.5 or e.boredom > 0.6) and r.random() < 0.35:
            giro_final = _acotar(envolver_180(rumbo) * 0.25, -25.0, 25.0)   # queda algo girada hacia donde iba
        return self._caminar_a(destino, e, ctx, giro_final=giro_final)

    def _b_volver_al_centro(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                            r: random.Random) -> list[Paso]:
        if not self.caminar or limites_escenario(ctx.camara) is None:
            return []
        return self._caminar_a((0.0, 0.0), e, ctx, giro_final=0.0)

    def _b_girar(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        """Gira en el sitio hacia el rumbo de marcha propuesto (siempre de frente al público, ±35°)."""
        objetivo = _acotar(0.6 * envolver_180(brujula.angulo), -GIRO_MAX_QUIETA, GIRO_MAX_QUIETA)
        if abs(objetivo - ctx.rotacion) < GIRO_MIN_QUIETA:      # ya mira hacia ahí: sigue girando un poco más
            if objetivo != ctx.rotacion:
                signo = 1.0 if objetivo > ctx.rotacion else -1.0
            else:
                signo = r.choice((-1.0, 1.0))
            objetivo = ctx.rotacion + signo * r.uniform(12.0, 30.0)
            if abs(objetivo) > GIRO_MAX_QUIETA:
                objetivo = ctx.rotacion - signo * r.uniform(12.0, 30.0)
            objetivo = _acotar(objetivo, -GIRO_MAX_QUIETA, GIRO_MAX_QUIETA)
        if abs(objetivo - ctx.rotacion) < 5.0:
            return []
        retener = r.uniform(2.5, 6.0)
        lado = "left" if objetivo > ctx.rotacion else "right"
        return [_p(0.0, _mirar(lado, 0.3)), _p(0.2, _girar(objetivo, 0.9)),
                _p(1.1 + retener, _girar(0.0, 0.8), final=True), _p(1.3 + retener, _mirar("camera"), final=True)]

    def _b_mirar_alrededor(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                           r: random.Random) -> list[Paso]:
        pasos = [_p(0.0, _gesto("look_around"))]
        if e.curiosidad > 0.4:
            cara = self._mezcla("curiosa", ctx, escala=0.7)
            if cara is not None:
                pasos += [_p(0.0, cara), self._paso_reposo(3.4, e, ctx)]
        pasos.append(_p(3.4, _mirar("camera"), final=True))
        return pasos

    def _b_ladear_cabeza(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                         r: random.Random) -> list[Paso]:
        lado = self._lado(math.sin(math.radians(brujula.angulo)), r, 0.1)
        if ctx.huesos and "head" in ctx.huesos and r.random() < 0.45:
            signo = 1.0 if lado == "right" else -1.0           # z + = ladear hacia SU derecha
            delta: dict[str, dict[str, float]] = {"head": {"z": signo * r.uniform(5.0, 10.0)}}
            if "neck" in ctx.huesos:
                delta["neck"] = {"z": signo * r.uniform(1.0, 3.0)}
            return self._micro_huesos(delta, ctx, r.uniform(2.0, 5.0), duracion=0.6)
        return [_p(0.0, _gesto("tilt_head", lado))]

    def _b_pensativa(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        arriba = "up_left" if r.random() < 0.5 else "up_right"
        if r.random() < 0.3:
            return [_p(0.0, _gesto("think")), _p(0.2, _mirar(arriba, 0.4)), _p(3.6, _mirar("camera"), final=True)]
        retener = r.uniform(4.0, 8.0) * (0.7 + 0.6 * e.focus)
        pasos = [_p(0.0, _pose("thinking", 0.8)), _p(0.4, _mirar(arriba, 0.35))]
        if r.random() < 0.5:
            pasos.append(_p(0.4 + retener * 0.5, _mirar("camera", 0.3)))     # mira a cámara sin soltar la pose
        pasos += [_p(retener, _mirar("camera"), final=True), _p(retener + 0.3, _pose("relaxed", 0.8), final=True)]
        return pasos

    def _b_brazos_cruzados(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                           r: random.Random) -> list[Paso]:
        retener = r.uniform(5.0, 10.0)
        pasos = [_p(0.0, _pose("arms_crossed", 0.7))]
        if e.valencia < -0.3:
            cara = self._mezcla("molesta", ctx, escala=0.6)
            if cara is not None:
                pasos += [_p(0.3, cara), self._paso_reposo(retener, e, ctx)]
        if r.random() < 0.4:                                   # aparta la mirada un momento (tsundere)
            otro = "right" if r.random() < 0.5 else "left"
            t_mirada = 0.8 + r.uniform(1.2, 2.5)
            pasos += [_p(0.8, _mirar(otro, 0.45)), _p(t_mirada, _mirar("camera"), final=True)]
        pasos.append(_p(retener, _pose("relaxed", 0.8), final=True))
        return pasos

    def _b_estirarse(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        pasos = [_p(0.0, _pose("arms_up", 1.1)), _p(0.3, _mirar("up", 0.3))]
        cara = self._mezcla("somnolienta", ctx, escala=0.8)
        if cara is not None:
            pasos += [_p(0.2, cara), self._paso_reposo(3.0, e, ctx)]
        pasos += [_p(2.6, _pose("relaxed", 1.2), final=True), _p(2.8, _mirar("camera"), final=True)]
        return pasos

    def _b_bostezar(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        disponibles = ctx.expresiones if ctx.expresiones is not None else EXPRESIONES_ESTANDAR
        boca = {n: v for n, v in (("aa", 0.65), ("blink", 0.4)) if n in disponibles}
        pasos: list[Paso] = []
        if ctx.huesos and "head" in ctx.huesos:
            delta: dict[str, dict[str, float]] = {"head": {"x": -10.0}}
            if "neck" in ctx.huesos:
                delta["neck"] = {"x": -5.0}
            micro = self._micro_huesos(delta, ctx, 2.1, duracion=0.7)
            pasos += micro
        else:
            pasos += [_p(0.0, _mirar("up", 0.5)), _p(2.1, _mirar("camera"), final=True)]
        if boca:
            pasos += [_p(0.2, {"type": "expression", "values": boca, "exclusive": False, "duration": 0.6}),
                      _p(2.0, {"type": "expression", "values": {n: 0.0 for n in boca}, "exclusive": False,
                               "duration": 0.5}, final=True)]
        cara = self._mezcla("somnolienta", ctx, escala=0.7)
        if cara is not None:
            pasos += [_p(0.1, cara), self._paso_reposo(2.4, e, ctx)]
        return pasos

    def _b_dormitar(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        retener = r.uniform(4.0, 9.0)
        disponibles = ctx.expresiones if ctx.expresiones is not None else EXPRESIONES_ESTANDAR
        pasos = [_p(0.0, {"type": "idle", "intensity": 0.3}),
                 _p(retener + 0.3, {"type": "idle", "intensity": self.intensidad_idle(e)}, final=True)]
        cara = self._mezcla("somnolienta", ctx, escala=0.9, duracion=1.5)
        if cara is not None:
            pasos += [_p(0.0, cara), self._paso_reposo(retener + 0.2, e, ctx)]
        if "blink" in disponibles:                             # párpados a media asta
            pasos += [_p(0.3, {"type": "expression", "values": {"blink": 0.55}, "exclusive": False, "duration": 1.5}),
                      _p(retener + 0.1, {"type": "expression", "values": {"blink": 0.0}, "exclusive": False,
                                         "duration": 0.3}, final=True)]
        if ctx.huesos and "head" in ctx.huesos:                # cabecea y se despierta de golpe
            delta: dict[str, dict[str, float]] = {"head": {"x": 10.0}}
            if "neck" in ctx.huesos:
                delta["neck"] = {"x": 5.0}
            pasos += self._micro_huesos(delta, ctx, retener, duracion=2.0, vuelta=0.3)
        return pasos

    def _b_encogerse(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        pasos = [_p(0.0, _gesto("shrug"))]
        if e.boredom > 0.6 and r.random() < 0.6:
            cara = self._mezcla("aburrida", ctx, escala=0.6)
            if cara is not None:
                pasos += [_p(0.0, cara), self._paso_reposo(1.9, e, ctx)]
        if r.random() < 0.4:
            pasos += [_p(0.4, _mirar(r.choice(("down_left", "down_right")), 0.3)),
                      _p(1.8, _mirar("camera"), final=True)]
        return pasos

    def _b_asentir(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        return [_p(0.0, _gesto("nod"))]

    def _b_alegria(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        """Alegría contenida: sonrisa, ladeo o asentimiento; reír o celebrar es raro, y suele
        rematar apartando la mirada con un leve sonrojo (tsundere)."""
        pasos: list[Paso] = []
        disponibles = ctx.expresiones if ctx.expresiones is not None else EXPRESIONES_ESTANDAR
        sonrisa = {n: v for n, v in (("happy", r.uniform(0.4, 0.6)), ("relaxed", 0.3)) if n in disponibles}
        if sonrisa:
            pasos.append(_p(0.0, {"type": "expression", "values": sonrisa, "duration": 0.3}))
        opciones = {"tilt_head": 0.6, "nod": 0.25}
        if e.arousal > 0.6:
            opciones["laugh"] = 0.12
        if e.valencia > 0.7 and e.arousal > 0.8:
            opciones["cheer"] = 0.03
        gesto = self._elegir(opciones, r)
        lado = r.choice(("left", "right")) if gesto == "tilt_head" else None
        pasos.append(_p(0.2, _gesto(gesto, lado)))
        fin = DURACION_GESTO.get(gesto, 1.5) + 0.3
        if r.random() < 0.6:
            sonrojo = self._mezcla("sonrojo_tsundere", ctx, escala=0.8)
            pasos += [_p(fin, _mirar(r.choice(("right", "left", "down_right")), 0.55), sonrojo),
                      _p(fin + r.uniform(1.2, 2.2), _mirar("camera"), final=True)]
            fin += 2.4
        pasos.append(self._paso_reposo(fin, e, ctx))
        return pasos

    def _b_tsundere(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        """Sonrojo y mirada esquiva; a veces se cruza de brazos o se encoge (shy)."""
        retener = r.uniform(1.8, 4.0)
        pasos = [_p(0.1, _mirar(r.choice(("right", "left", "down_right", "down_left")), 0.6))]
        sonrojo = self._mezcla("sonrojo_tsundere", ctx, escala=r.uniform(0.6, 0.9))
        if sonrojo is not None:
            pasos += [_p(0.0, sonrojo), self._paso_reposo(retener + 0.2, e, ctx)]
        tirada = r.random()
        if tirada < 0.25:
            pasos += [_p(0.2, _pose("shy", 0.6)), _p(retener + 0.4, _pose("relaxed", 0.8), final=True)]
        elif tirada < 0.5:
            pasos += [_p(0.2, _pose("arms_crossed", 0.6)), _p(retener + 0.4, _pose("relaxed", 0.8), final=True)]
        pasos.append(_p(retener, _mirar("camera"), final=True))
        return pasos

    def _b_postura(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        """Manos en la cadera (segura de sí, smug)."""
        retener = r.uniform(4.0, 8.0)
        pasos = [_p(0.0, _pose("hands_on_hips", 0.8)), _p(retener, _pose("relaxed", 0.8), final=True)]
        if e.valencia > 0.2:
            cara = self._mezcla("smug", ctx, escala=0.7)
            if cara is not None:
                pasos += [_p(0.2, cara), self._paso_reposo(retener, e, ctx)]
        return pasos

    def _b_camara(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula, r: random.Random) -> list[Paso]:
        if not self.camara_permitida:
            return []
        if e.focus > 0.75:
            preset = "bust"
        elif (e.boredom > 0.6 or e.curiosidad > 0.6) and self.caminar:
            preset = "wide" if r.random() < 0.3 else "full"
        else:
            preset = "full"
        if preset == ctx.camara:
            return []
        return [_p(0.0, {"type": "camera", "preset": preset, "duration": 1.2})]

    # ------------------------------------------------------------------ repertorio de escritorio
    @staticmethod
    def _velocidad_escritorio(e: HomeostaticState, base: float = 0.6, ganancia: float = 0.9) -> float:
        """Velocidad de marcha por la pantalla (0,3..3) según arousal y fatiga."""
        return round(_acotar(base + ganancia * e.arousal - 0.3 * e.fatiga, 0.3, 3.0), 2)

    def _b_pasear_escritorio(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                             r: random.Random) -> list[Paso]:
        """Pasea por la pantalla con la brújula E-PG: con un rumbo nítido va a un sitio concreto
        (``walk_to``, la componente lateral del rumbo elige el lado de la pantalla); si no, deambula
        (``wander``) un rato que crece con el aburrimiento y la curiosidad."""
        if not ctx.escritorio:
            return []
        velocidad = self._velocidad_escritorio(e)
        lateral = math.sin(math.radians(brujula.angulo))
        if r.random() < 0.35 + 0.4 * brujula.coherencia:
            x = _acotar(0.5 + 0.42 * lateral + r.uniform(-0.08, 0.08), 0.05, 0.95)
            cmd = _desktop("walk_to", x=round(x, 3), speed=velocidad)
            duracion = DURACION_DESKTOP["walk_to"] / max(velocidad, 0.3)
        else:
            segundos = round(_acotar(4.0 + 8.0 * e.boredom + 3.0 * e.curiosidad + r.uniform(-1.5, 1.5),
                                     3.0, DESKTOP_SEGUNDOS_MAX), 1)
            cmd = _desktop("wander", seconds=segundos, speed=velocidad)
            duracion = segundos
        return [_p(0.0, _mirar("forward", 0.2)), _p(0.1, cmd),
                _p(0.1 + duracion + 0.3, _mirar("camera", 0.35), final=True)]

    def _b_asomarse(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                    r: random.Random) -> list[Paso]:
        """Se asoma por el borde hacia el que apunta el rumbo, con cara de curiosidad."""
        if not ctx.escritorio:
            return []
        lateral = math.sin(math.radians(brujula.angulo))
        borde = "right" if lateral > 0.1 else "left" if lateral < -0.1 else r.choice(BORDES_DESKTOP)
        return self._pasos_asomarse(e, ctx, borde)

    def _pasos_asomarse(self, e: HomeostaticState, ctx: ContextoMotor, borde: str) -> list[Paso]:
        pasos = [_p(0.0, _desktop("peek", edge=borde)), _p(0.6, _mirar_al_usuario(ctx, 0.4))]
        cara = self._mezcla("curiosa", ctx, escala=0.7)
        if cara is not None:
            pasos += [_p(0.3, cara), self._paso_reposo(DURACION_DESKTOP["peek"], e, ctx)]
        return pasos

    def _b_perseguir_cursor(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                            r: random.Random) -> list[Paso]:
        """Persigue el cursor un rato (solo visual: nunca toca el ratón real), con cara de suficiencia."""
        if not ctx.escritorio:
            return []
        segundos = round(_acotar(4.0 + 8.0 * e.boredom + r.uniform(-1.0, 1.0), 3.0, 12.0), 1)
        velocidad = self._velocidad_escritorio(e, base=0.8, ganancia=1.0)
        pasos = [_p(0.0, _mirar("mouse", 0.5)), _p(0.1, _desktop("chase_cursor", seconds=segundos, speed=velocidad))]
        cara = self._mezcla("smug", ctx, escala=0.7)
        if cara is not None:
            pasos += [_p(0.2, cara), self._paso_reposo(segundos + 0.4, e, ctx)]
        pasos.append(_p(segundos + 0.3, _mirar("camera"), final=True))
        return pasos

    def _b_buscar_usuario(self, e: HomeostaticState, ctx: ContextoMotor, brujula: Brujula,
                          r: random.Random) -> list[Paso]:
        """Hace rato que no le hablan: va a buscar al usuario (camina hacia él y lo mira)."""
        if not ctx.escritorio:
            return []
        return self._pasos_buscar_usuario(e, ctx)

    def _pasos_buscar_usuario(self, e: HomeostaticState, ctx: ContextoMotor) -> list[Paso]:
        pasos = [_p(0.0, _desktop("seek_user")), _p(0.5, _mirar_al_usuario(ctx, 0.45))]
        cara = self._mezcla("curiosa" if e.valencia >= -0.2 else "molesta", ctx, escala=0.6)
        if cara is not None:
            pasos += [_p(0.4, cara), self._paso_reposo(DURACION_DESKTOP["seek_user"] + 0.5, e, ctx)]
        return pasos

    def _mirada_util(self, ahora: float) -> MiradaUsuario | None:
        """Último punto de ``vision.mirada`` si es reciente y fiable (confianza > 0,6)."""
        mirada = self._mirada
        if mirada is None or mirada.confianza <= MIRADA_CONFIANZA_MIN:
            return None
        return mirada if 0.0 <= ahora - mirada.t <= MIRADA_RECIENTE_S else None

    # ------------------------------------------------------------------ planes prioritarios
    def plan_atencion(self, estado: HomeostaticState, ctx: ContextoMotor) -> Plan | None:
        """El usuario escribió: mirar a cámara, girar de frente y (si camina) acercarse un poco. En el
        escritorio no hay escenario al que acercarse: atención de escritorio (``plan_atencion_escritorio``)."""
        if ctx.sin_escenario:
            return self.plan_atencion_escritorio(estado, ctx)
        pasos = [_p(0.0, _mirar("camera", 0.45))]
        if ctx.pose in ("thinking", "arms_up", "shy", "hands_on_hips"):
            pasos.append(_p(0.0, _pose("relaxed", 0.6)))
        limites = limites_escenario(ctx.camara)
        x0, z0 = ctx.posicion
        destino = (x0 * 0.35, Z_ATENCION)
        if limites is not None:
            destino = (_acotar(destino[0], -limites[0], limites[0]), _acotar(destino[1], limites[1], limites[2]))
        distancia = math.hypot(destino[0] - x0, destino[1] - z0)
        if self.caminar and limites is not None and distancia > 0.25:
            duracion = _acotar(distancia / 0.7, 0.6, 4.0)
            pasos += [_p(0.0, {"type": "move", "x": round(destino[0], 3), "z": round(destino[1], 3),
                               "rotation": 0.0, "walk": True, "duration": round(duracion, 2)}),
                      _p(duracion + 0.2, _mirar("camera", 0.4), final=True)]
        elif abs(ctx.rotacion) > 3.0:
            pasos.append(_p(0.0, _girar(0.0, 0.5)))
        return self._plan("atencion", pasos, ctx)

    def plan_reading_steiner(self, estado: HomeostaticState, ctx: ContextoMotor) -> Plan | None:
        """Cambio de línea de mundo: sorpresa, mano a la sien, mirar alrededor, inquietud y vuelta."""
        disponibles = ctx.expresiones if ctx.expresiones is not None else EXPRESIONES_ESTANDAR
        pasos = [_p(0.0, self._mezcla("sorprendida", ctx, duracion=0.2), _gesto("surprise"), _mirar("camera", 0.5))]
        if ctx.huesos and {"rightUpperArm", "rightLowerArm", "rightHand"} <= ctx.huesos:
            cabeza = {"head": {"x": 6.0, "z": -4.0}} if "head" in ctx.huesos else {}
            pasos += [_p(0.9, _huesos({**MANO_A_LA_SIEN, **cabeza}, 0.7),
                         {"type": "hand", "side": "right", "shape": "relaxed"})]
        else:
            pasos.append(_p(0.9, _pose("thinking", 0.7)))
        pasos.append(_p(1.4, _gesto("look_around")))
        inquieta = {n: v for n, v in (("surprised", 0.35), ("sad", 0.25)) if n in disponibles}
        if inquieta:
            pasos.append(_p(3.0, {"type": "expression", "values": inquieta, "duration": 0.6}))
        pasos += [_p(4.9, _pose("relaxed", 1.0), final=True), _p(5.0, _mirar("camera"), final=True),
                  self._paso_reposo(5.2, estado, ctx)]
        return self._plan("reading_steiner", pasos, ctx)

    def plan_atencion_escritorio(self, estado: HomeostaticState, ctx: ContextoMotor) -> Plan | None:
        """El usuario escribió en el chat de escritorio: se gira de frente y mira al ratón (donde está
        el chat), **sin acercarse** (ningún ``move`` con x/z); al rato vuelve a mirar al usuario."""
        pasos = [_p(0.0, _mirar("mouse", 0.45))]
        if ctx.pose in ("thinking", "arms_up", "shy", "hands_on_hips"):
            pasos.append(_p(0.0, _pose("relaxed", 0.6)))
        if abs(ctx.rotacion) > 3.0:
            pasos.append(_p(0.0, _girar(0.0, 0.5)))
        pasos.append(_p(ATENCION_ESCRITORIO_S, _mirar_al_usuario(ctx, 0.35), final=True))
        return self._plan("atencion_escritorio", pasos, ctx)

    def plan_solicitud(self, solicitud: SolicitudMotor, estado: HomeostaticState,
                       ctx: ContextoMotor) -> Plan | None:
        """Plan de una ``motor.solicitud`` de main (buscar al usuario, asomarse o pasear por la pantalla)."""
        if solicitud.conducta == "buscar_usuario":
            pasos = self._pasos_buscar_usuario(estado, ctx)
        elif solicitud.conducta == "asomarse":
            pasos = self._pasos_asomarse(estado, ctx, solicitud.borde or "right")
        elif solicitud.conducta == "pasear":
            segundos = round(_acotar(6.0 + 6.0 * estado.boredom, 3.0, DESKTOP_SEGUNDOS_MAX), 1)
            pasos = [_p(0.0, _desktop("wander", seconds=segundos, speed=self._velocidad_escritorio(estado))),
                     _p(segundos + 0.4, _mirar_al_usuario(ctx, 0.35), final=True)]
        else:
            return None
        return self._plan(f"solicitud_{solicitud.conducta}", pasos, ctx)

    def plan_reaccion(self, nombre: str, datos: Mapping[str, Any], estado: HomeostaticState,
                      ctx: ContextoMotor) -> Plan | None:
        """Toque o arrastre sobre la caja del avatar: sobresalto y sonrojo breves (o enfado si la
        soltaron desde arriba)."""
        if nombre == "toque":
            pasos = [_p(0.0, self._mezcla("sorprendida", ctx, escala=0.6, duracion=0.15), _mirar("camera", 0.5)),
                     _p(0.7, self._mezcla("sonrojo_tsundere", ctx, escala=0.85),
                        _mirar(self._rng.choice(("right", "down_right", "left")), 0.5)),
                     _p(2.2, _mirar("camera"), final=True), self._paso_reposo(2.4, estado, ctx)]
        elif nombre == "soltada":
            altura = _acotar(_num(datos.get("altura"), 0.0) or 0.0, 0.0, 1.0)
            alta = altura > SOLTADA_ALTA
            pasos = [_p(0.0, self._mezcla("sorprendida", ctx, escala=0.9, duracion=0.15),
                        _gesto("surprise") if alta else None, _mirar("camera", 0.5)),
                     _p(1.2, self._mezcla("molesta" if alta else "sonrojo_tsundere", ctx, escala=0.7)),
                     _p(2.6, _mirar("camera"), final=True), self._paso_reposo(2.8, estado, ctx)]
        else:
            return None
        return self._plan(f"reaccion_{nombre}", pasos, ctx)

    # ------------------------------------------------------------------ manejadores del bus (nunca lanzan)
    def _al_homeostasis(self, evento: Evento) -> None:
        try:
            datos = evento.datos
            estado = HomeostaticState.desde_dict(datos.get("estado") or {})
            if not estado.bump and isinstance(datos.get("bump"), (list, tuple)):
                estado = replace(estado, bump=tuple(datos["bump"]))
            self._estado = estado
        except Exception as exc:
            log.debug("Estado homeostático inválido para la corteza motora: %s", type(exc).__name__)

    def _al_cambio_linea(self, _evento: Evento) -> None:
        try:
            ahora = self._reloj()
            if ahora - self._ultimo_steiner >= REFRACTARIO_STEINER_S:
                self._steiner_hasta = ahora + CADUCIDAD_STEINER_S
        except Exception as exc:
            log.debug("Reading Steiner no registrado: %s", type(exc).__name__)

    def _al_usuario(self, evento: Evento) -> None:
        """Atención solo si el usuario está frente al avatar (terminal, Nexus o el chat de escritorio);
        por Telegram no. Por el chat de escritorio, atención de escritorio (sin acercarse)."""
        try:
            canal = evento.datos.get("canal", "terminal")
            if canal not in CANALES_ATENCION:
                return
            self._ultimo_usuario = self._reloj()
            self._atencion_pendiente = True
            self._atencion_escritorio = canal == "escritorio"
        except Exception as exc:
            log.debug("Entrada del usuario no registrada: %s", type(exc).__name__)

    def _al_subtitulo(self, evento: Evento) -> None:
        try:
            datos = evento.datos
            if datos.get("canal") == "telegram" or not str(datos.get("texto") or "").strip():
                return
            self._habla_inminente_hasta = self._reloj() + HABLA_INMINENTE_S
            durante, _deshacer = pasos_motion(str(datos.get("motion") or "idle"))
            # Lo que trae el MOTION del habla (pose, mirada, gesto) lo gobierna el puente: al cortar la
            # conducta no se pisa, y el puente lo deshace al terminar.
            self._habla_gobierna |= frozenset(str(p.get("type")) for p in durante)
            if any(p.get("type") == "pose" for p in durante):
                self._pose = "relaxed"                         # el puente la deshace al terminar
        except Exception as exc:
            log.debug("Subtítulo no registrado por la corteza motora: %s", type(exc).__name__)

    def _al_voz_inicio(self, _evento: Evento) -> None:
        try:
            self._voz_desde = self._reloj()
        except Exception as exc:
            log.debug("voz.inicio no registrado: %s", type(exc).__name__)

    def _al_voz_fin(self, _evento: Evento) -> None:
        self._voz_desde = None

    def _al_apagado(self, _evento: Evento) -> None:
        self._apagando = True

    # --- visión continua ---------------------------------------------------------------------
    def _al_vision_mirada(self, evento: Evento) -> None:
        try:
            datos = evento.datos
            valores = [_num(datos.get(k)) for k in ("x", "y", "z", "confianza")]
            if any(v is None for v in valores):
                return
            x, y, z, confianza = (float(v) for v in valores)   # type: ignore[arg-type]
            self._mirada = MiradaUsuario(_acotar(x, -3.0, 3.0), _acotar(y, 0.3, 2.2), _acotar(z, -3.0, 3.0),
                                         _acotar(confianza, 0.0, 1.0), self._reloj())
        except Exception as exc:
            log.debug("vision.mirada no registrada: %s", type(exc).__name__)

    def _al_vision_rostro(self, evento: Evento) -> None:
        """Flanco de "rostro de frente" → mirar al usuario (como mucho cada ``ROSTRO_MIN_ENTRE_S``)."""
        try:
            datos = evento.datos
            frontal = datos.get("presente") is True and datos.get("frontal") is True
            if frontal and not self._rostro_frontal:
                ahora = self._reloj()
                if ahora - self._t_mirar_usuario >= ROSTRO_MIN_ENTRE_S:
                    self._mirar_usuario_hasta = ahora + ROSTRO_CADUCIDAD_S
            self._rostro_frontal = frontal
        except Exception as exc:
            log.debug("vision.rostro no registrado: %s", type(exc).__name__)

    # --- escritorio (Amadeus Desktop) -------------------------------------------------------------
    def _estado_inicial_escritorio(self, bus: Any) -> None:
        """Toma el último estado ya publicado (``motor.travesuras``, ``pc.no_molestar``,
        ``escritorio.conectado``) por si la corteza se construye después; no publica nada."""
        ultimo = getattr(bus, "ultimo", None)
        if not callable(ultimo):
            return
        try:
            travesuras = ultimo("motor.travesuras")
            if travesuras is not None:
                self._travesuras_activas = travesuras.datos.get("activas") is True
            no_molestar = ultimo("pc.no_molestar")
            if no_molestar is not None:
                self._no_molestar = no_molestar.datos.get("activo") is True
            conectado = ultimo("escritorio.conectado")
            if conectado is not None and isinstance(conectado.datos.get("overlay"), bool):
                self._overlay_conectado = conectado.datos["overlay"]
        except Exception as exc:
            log.debug("Estado inicial de escritorio ilegible: %s", type(exc).__name__)

    def _al_escritorio_conectado(self, evento: Evento) -> None:
        try:
            overlay = evento.datos.get("overlay")
            if overlay is False:                               # sin Electron no hay ventana que ver
                self._overlay_conectado = False
                self._escritorio_visible = self._saliendo = False
                self._desktop_hasta = -math.inf
            elif overlay is True:
                self._overlay_conectado = True
                self._refrescar_avatar()
        except Exception as exc:
            log.debug("escritorio.conectado no registrado: %s", type(exc).__name__)

    def _al_escritorio_evento(self, evento: Evento) -> None:
        try:
            nombre = evento.datos.get("nombre")
            if nombre not in EVENTOS_ESCRITORIO:
                return
            datos = evento.datos.get("datos")
            datos = dict(datos) if isinstance(datos, Mapping) else {}
            if nombre == "mostrada":
                if not self._escritorio_visible or self._saliendo:   # entra por el borde (Electron la guía)
                    self._t_mostrada, self._ceder_cuerpo = self._reloj(), True
                self._escritorio_visible, self._saliendo, self._overlay_conectado = True, False, True
                self._refrescar_avatar()
            elif nombre == "ocultada":
                self._escritorio_visible = self._saliendo = False
                self._desktop_hasta = -math.inf
            elif nombre in ("toque", "soltada"):
                ahora = self._reloj()
                if ahora - self._t_reaccion >= REACCION_MIN_ENTRE_S:
                    self._reaccion = (nombre, datos, ahora)
            elif nombre == "chat_abierto":                     # el usuario abrió el chat: va a escribirle
                self._ultimo_usuario = self._reloj()
                self._atencion_pendiente = self._atencion_escritorio = True
        except Exception as exc:
            log.debug("escritorio.evento no registrado: %s", type(exc).__name__)

    def _al_solicitud(self, evento: Evento) -> None:
        """``motor.solicitud`` de main: se guarda la última hasta ``ttl_s`` (``esconderse``: en el acto)."""
        try:
            datos = evento.datos
            conducta = datos.get("conducta")
            if conducta not in CONDUCTAS_SOLICITUD:
                log.debug("Solicitud motora desconocida: %s", str(conducta)[:40])
                return
            if conducta == "esconderse":
                self._esconderse("solicitud de main")
                return
            if conducta not in SOLICITUD_A_DESKTOP:
                return
            ttl = _acotar(_num(datos.get("ttl_s"), SOLICITUD_TTL_DEFECTO_S) or 0.0, 0.0, SOLICITUD_TTL_MAX_S)
            borde = datos.get("borde") if datos.get("borde") in BORDES_DESKTOP else None
            self._solicitud = SolicitudMotor(str(conducta), str(datos.get("motivo") or "")[:80], borde,
                                             self._reloj() + ttl)
            self._refrescar_avatar()
        except Exception as exc:
            log.debug("motor.solicitud no registrada: %s", type(exc).__name__)

    def _al_travesuras(self, evento: Evento) -> None:
        try:
            self._travesuras_activas = evento.datos.get("activas") is True
        except Exception as exc:
            log.debug("motor.travesuras no registrado: %s", type(exc).__name__)

    def _al_no_molestar(self, evento: Evento) -> None:
        """``pc.no_molestar``: al activarse se esconde (una vez por activación) y olvida lo pendiente."""
        try:
            activo = evento.datos.get("activo") is True
            antes, self._no_molestar = self._no_molestar, activo
            if activo and not antes:
                self._solicitud = None
                self._esconderse(f"no molestar ({str(evento.datos.get('motivo') or 'sistema')[:24]})")
        except Exception as exc:
            log.debug("pc.no_molestar no registrado: %s", type(exc).__name__)

    def _esconderse(self, motivo: str) -> bool:
        """Publica ``escritorio.orden{accion: "esconderse"}`` (la ÚNICA orden de escritorio de la corteza:
        nunca ``aparecer``). La visibilidad la gobierna el main de Electron."""
        self._solicitud = None
        if self.bus is None or not self.escritorio_habilitado:
            return False
        try:
            self.bus.publicar("escritorio.orden", {"accion": "esconderse"}, origen="corteza_motora")
        except Exception as exc:
            log.debug("No se pudo publicar escritorio.orden: %s", type(exc).__name__)
            return False
        if self._escritorio_visible and not self._saliendo:   # sale caminando: el tic corta la conducta
            self._saliendo = self._ceder_cuerpo = True
        log.info("Amadeus se esconde del escritorio (%s)", motivo)
        return True

    def _refrescar_avatar(self) -> None:
        """Si el puente aún no conoce el overlay, le pide un sondeo inmediato de ``/api/state``."""
        if self._visores_escritorio_total() > 0:
            return
        refrescar = getattr(self.avatar, "refrescar", None)
        if callable(refrescar):
            try:
                refrescar()
            except Exception as exc:
                log.debug("El avatar no pudo refrescar su estado: %s", type(exc).__name__)

    # ------------------------------------------------------------------ E/S
    def _esta_hablando(self, ahora: float) -> bool:
        try:
            hablando = bool(getattr(self.avatar, "hablando", False))
        except Exception as exc:
            log.debug("Avatar sin 'hablando' legible: %s", type(exc).__name__)
            hablando = False
        if self._voz_desde is not None:
            if ahora - self._voz_desde > VOZ_MAX_S:
                self._voz_desde = None
            else:
                hablando = True
        return hablando or ahora < self._habla_inminente_hasta

    def _posicion_actual(self, ahora: float) -> tuple[float, float]:
        if self._mov is not None:
            t0, duracion, desde, hasta = self._mov
            k = _acotar((ahora - t0) / max(duracion, 1e-3), 0.0, 1.0)
            if k >= 1.0:
                self._pos, self._mov = hasta, None
                return hasta
            e = k * k * (3.0 - 2.0 * k)                        # la misma curva que el visor al caminar
            return (desde[0] + (hasta[0] - desde[0]) * e, desde[1] + (hasta[1] - desde[1]) * e)
        return self._pos

    def _camara_actual(self) -> str:
        visor = None
        ts = None
        try:
            estado = getattr(self.avatar, "estado_visor", None)
            ts = _num(getattr(self.avatar, "ts_estado", None))
            if isinstance(estado, Mapping) and estado.get("camera") in CAMARAS_VISOR:
                visor = str(estado["camera"])
        except Exception as exc:
            log.debug("Estado del visor ilegible: %s", type(exc).__name__)
        if self._camara is not None and (visor is None or ts is None or self._t_camara >= ts):
            return self._camara
        return visor or "full"

    def _sincronizar_posicion(self) -> None:
        """Adopta la posición que informa el visor (p. ej. si el usuario la movió a mano)."""
        if self._mov is not None:
            return
        try:
            pos = getattr(self.avatar, "posicion", None)
            ts = _num(getattr(self.avatar, "ts_estado", None))
        except Exception as exc:
            log.debug("Posición del visor ilegible: %s", type(exc).__name__)
            return
        if ts is None or not isinstance(pos, tuple) or len(pos) != 3:
            return
        # El visor solo publica su estado tras ejecutar un comando: la foto es fiable si es posterior al
        # primer comando enviado después de que terminara el último movimiento.
        if self._envio_tras_mov is None or ts <= self._envio_tras_mov + 0.6:
            return
        x, z, giro = (_num(v) for v in pos)
        if x is None or z is None or giro is None:
            return
        if math.hypot(x - self._pos[0], z - self._pos[1]) > 0.05 or abs(envolver_180(giro - self._rot)) > 3.0:
            self._pos = (_acotar(x, -LIMITE_X_ABS, LIMITE_X_ABS), _acotar(z, LIMITE_Z_ABS[0], LIMITE_Z_ABS[1]))
            self._rot = envolver_180(giro)

    def contexto(self, ahora: float) -> ContextoMotor:
        """Contexto actual para ``decidir`` (lee el avatar sin bloquear)."""
        try:
            huesos = getattr(self.avatar, "huesos_disponibles", None)
            expresiones = getattr(self.avatar, "expresiones_disponibles", None)
        except Exception as exc:
            log.debug("Inventario del avatar ilegible: %s", type(exc).__name__)
            huesos = expresiones = None
        mirada = self._mirada_util(ahora)
        return ContextoMotor(
            hablando=self._esta_hablando(ahora), en_linea=self._avatar_en_linea(),
            huesos=frozenset(huesos) if huesos else None,
            expresiones=frozenset(expresiones) if expresiones else None,
            posicion=self._posicion_actual(ahora), rotacion=self._rot, pose=self._pose,
            camara=self._camara_actual(), desde_usuario_s=ahora - self._ultimo_usuario,
            presupuesto=self.presupuesto(ahora), escritorio=self.escritorio_disponible(),
            usuario=(mirada.x, mirada.y, mirada.z) if mirada is not None else None,
            atencion_escritorio=self._atencion_escritorio, en_escritorio=self.cuerpo_en_escritorio())

    # ------------------------------------------------------------------ escritorio: disponibilidad
    def _visores_escritorio_total(self) -> int:
        """Overlays de escritorio cargados según el puente (0 si el avatar no lo sabe)."""
        try:
            valor = getattr(self.avatar, "visores_escritorio_total", 0)
        except Exception as exc:
            log.debug("Avatar sin visores_escritorio_total legible: %s", type(exc).__name__)
            return 0
        numero = _num(valor, 0.0) or 0.0
        return int(numero) if numero > 0 else 0

    @property
    def escritorio_visible(self) -> bool:
        """Último ``escritorio.evento`` de visibilidad (``mostrada`` → True, ``ocultada`` → False)."""
        return self._escritorio_visible

    @property
    def solicitud(self) -> SolicitudMotor | None:
        """Solicitud de main retenida (o ``None``)."""
        return self._solicitud

    def escritorio_disponible(self, *, parar: bool = False) -> bool:
        """``True`` si puede emitir conductas de escritorio: habilitado, ventana visible (y sin estar
        saliendo), overlay cargado (``avatar.visores_escritorio_total > 0``) y sin "no molestar"
        (``parar=True`` ignora el "no molestar": detener la locomoción siempre se permite).

        Solo decide los comandos ``desktop``; dónde está el cuerpo lo dice ``cuerpo_en_escritorio()``."""
        if not self.escritorio_habilitado or not self._escritorio_visible or self._saliendo:
            return False
        if self._visores_escritorio_total() <= 0:
            return False
        return parar or not self._no_molestar

    def cuerpo_en_escritorio(self) -> bool:
        """``True`` si el cuerpo está en el overlay de escritorio: la ventana está visible, aunque no pueda
        emitir ``desktop`` ("no molestar", saliendo por el borde, el puente aún sin overlay). Entonces no
        hay escenario: ni ``move`` con x/z ni ``walk``, ni paseos de la v0.1, y la atención es la de
        escritorio (sin acercarse)."""
        return self.escritorio_habilitado and self._escritorio_visible

    def _cuerpo_de_electron(self, ahora: float) -> bool:
        """``True`` mientras el main de Electron la hace salir o entrar caminando por el borde (saliendo,
        o los primeros ``GRACIA_ENTRADA_S`` tras "mostrada"): ni giros ni conductas nuevas, que pisarían
        la orientación de esa caminata."""
        return self.cuerpo_en_escritorio() and (self._saliendo or ahora < self._t_mostrada + GRACIA_ENTRADA_S)

    def _filtrar_cuerpo(self, comandos: list[dict[str, Any]], ahora: float) -> list[dict[str, Any]]:
        """Con el cuerpo en el escritorio, ningún ``move`` de escenario: sin x/z ni ``walk`` (solo giro en el
        sitio) y, mientras Electron la hace entrar o salir, ni siquiera giro. Fuera del escritorio, intactos."""
        if not self.cuerpo_en_escritorio():
            return comandos
        sin_giros = self._cuerpo_de_electron(ahora)
        salida: list[dict[str, Any]] = []
        for cmd in comandos:
            if cmd.get("type") == "move":
                if sin_giros or "rotation" not in cmd:
                    continue
                if "x" in cmd or "z" in cmd or cmd.get("walk"):
                    cmd = {"type": "move", "rotation": cmd["rotation"], "walk": False,
                           "duration": cmd.get("duration", 0.6)}
            salida.append(cmd)
        return salida

    def presupuesto(self, ahora: float) -> int:
        """Comandos que aún caben en el último minuto (descontando los ya agendados o aplazados)."""
        while self._enviados and self._enviados[0] <= ahora - 60.0:
            self._enviados.popleft()
        pendientes = sum(len(p.paso.comandos) for p in self._agenda) + sum(len(p.comandos) for p in self._diferidos)
        return LIMITE_COMANDOS_MIN - len(self._enviados) - pendientes

    def _enviar(self, comandos: Sequence[dict[str, Any]], ahora: float, *, reservado: bool) -> int:
        """Encola en el avatar (lista = en paralelo, en orden). ``reservado`` = ya contaba como pendiente.

        Los ``desktop`` salen aparte y de uno en uno con ``avatar.desktop(...)`` (el servidor rechaza un
        lote que los mezcle con otros tipos) y solo si el escritorio está disponible; si no, se descartan.
        """
        comandos = self._filtrar_cuerpo([c for c in comandos if c], ahora)
        if not comandos:
            return 0
        if not reservado and len(comandos) > self.presupuesto(ahora):
            return 0
        cuerpo = [c for c in comandos if c.get("type") != "desktop"]
        enviados: list[dict[str, Any]] = []
        if cuerpo:
            try:
                self.avatar.encolar(dict(cuerpo[0]) if len(cuerpo) == 1 else [dict(c) for c in cuerpo])
            except Exception as exc:
                log.debug("No se pudo encolar en el avatar 3D: %s", type(exc).__name__)
            else:
                enviados.extend(cuerpo)
        enviados.extend(c for c in comandos if c.get("type") == "desktop" and self._enviar_desktop(c))
        for cmd in enviados:
            self._enviados.append(ahora)
            self._aplicar_modelo(cmd, ahora)
        if enviados and self._envio_tras_mov is None and ahora > self._fin_ultimo_mov:
            self._envio_tras_mov = ahora
        return len(enviados)

    def _enviar_desktop(self, cmd: Mapping[str, Any]) -> bool:
        """``avatar.desktop(action, **campos)`` si el escritorio está disponible. ``True`` si lo encoló."""
        if not self.escritorio_disponible(parar=cmd.get("action") == "stop"):
            return False
        desktop = getattr(self.avatar, "desktop", None)
        if not callable(desktop):
            return False
        campos = {k: v for k, v in cmd.items() if k not in ("type", "action")}
        try:
            return bool(desktop(str(cmd.get("action")), **campos))
        except Exception as exc:
            log.debug("No se pudo encolar el comando de escritorio: %s", type(exc).__name__)
            return False

    def _aplicar_modelo(self, cmd: Mapping[str, Any], ahora: float) -> None:
        """Actualiza lo que la corteza cree del cuerpo tras enviar un comando."""
        tipo = cmd.get("type")
        if tipo == "move":
            actual = self._posicion_actual(ahora)
            if "x" in cmd or "z" in cmd:
                destino = (float(cmd.get("x", actual[0])), float(cmd.get("z", actual[1])))
                duracion = float(cmd.get("duration") or 1.0)
                self._mov = (ahora, duracion, actual, destino)
                self._fin_ultimo_mov = ahora + duracion
                self._envio_tras_mov = None
            else:                                              # solo giro: el visor se detiene donde está
                self._pos, self._mov = actual, None
                self._fin_ultimo_mov = max(self._fin_ultimo_mov, ahora + float(cmd.get("duration") or 0.6))
                self._envio_tras_mov = None
            if "rotation" in cmd:
                self._rot = float(cmd["rotation"])
        elif tipo == "pose":
            self._pose = str(cmd.get("name"))
        elif tipo == "gesture":
            self._gesto_hasta = max(self._gesto_hasta, ahora + _duracion_comando(cmd))
        elif tipo == "stop":
            self._gesto_hasta = -math.inf
        elif tipo == "camera":
            self._camara, self._t_camara = str(cmd.get("preset")), ahora
        elif tipo == "idle":
            self._idle, self._t_idle = float(cmd.get("intensity") or 0.0), ahora
        elif tipo == "look":                                   # recuerda si mira a un punto (el del usuario)
            punto = tuple(_num(cmd.get(eje)) for eje in _EJES)
            self._punto_mirada = (punto if cmd.get("at") == "point" and None not in punto  # type: ignore[assignment]
                                  else None)
        elif tipo == "desktop":
            self._desktop_hasta = -math.inf if cmd.get("action") == "stop" else ahora + _duracion_comando(cmd)

    def ejecutar(self, plan: Plan, ahora: float | None = None) -> int:
        """Agenda un plan ya decidido (o construido a mano) y envía sus pasos vencidos. Devuelve cuántos
        comandos encoló. Sin presupuesto o sin avatar en línea no hace nada."""
        if not self.activa or not self._avatar_en_linea():
            return 0
        ahora = self._reloj() if ahora is None else ahora
        if len(plan.comandos()) > self.presupuesto(ahora):
            return 0
        self._programar(plan, ahora)
        return self._despachar_agenda(ahora)

    def _programar(self, plan: Plan, ahora: float) -> None:
        for paso in plan.pasos:
            self._agenda.append(_Pendiente(ahora + paso.t, paso, plan.conducta))
        self._agenda.sort(key=lambda p: p.t_abs)
        self._plan_actual = plan.conducta
        memoria = replace(self._memoria, plan_hasta=ahora + plan.duracion)
        if plan.rumbo is not None:                             # integración de trayectoria: nuevo rumbo de marcha
            memoria = replace(memoria, rumbo_marcha=plan.rumbo, bump_ref=self.rumbo(self._estado).angulo)
        self._memoria = memoria
        self._plan_cara = any(c.get("type") == "expression" for c in plan.comandos())
        log.debug("Conducta motora: %s (%d comandos, %.1f s)", plan.conducta, len(plan.comandos()), plan.duracion)

    def _despachar_agenda(self, ahora: float) -> int:
        vencidos = [p for p in self._agenda if p.t_abs <= ahora]
        if not vencidos:
            return 0
        self._agenda = [p for p in self._agenda if p.t_abs > ahora]
        comandos = [c for p in vencidos for c in p.paso.comandos]
        enviados = self._enviar(comandos, ahora, reservado=True)
        if enviados:
            for p in vencidos:
                if p.paso.reposo:
                    self._exp_reposo, self._t_reposo, self._forzar_reposo = p.paso.reposo, ahora, False
        if not self._agenda:
            self._plan_actual = None
        return enviados

    def _interrumpir(self, ahora: float, *, de_frente: bool = False,
                     parar_desktop: bool = True) -> list[dict[str, Any]]:
        """Corta la conducta en curso: descarta los pasos pendientes no finales, aplaza los finales y
        devuelve el giro final (``move``) para enviarlo ya: detiene el paseo y la deja girada como
        terminaba la conducta (``de_frente`` = exactamente de frente, para hablar o reaccionar; también
        si estaba algo girada sin conducta en curso). Si camina por el escritorio (y ``parar_desktop``),
        añade ``desktop stop`` (va primero: es lo que más se nota).

        Lo devuelto ya no cuenta como pendiente: quien lo envíe lo hace con presupuesto normal."""
        finales = [p.paso for p in self._agenda if p.paso.final]
        self._agenda = []
        self._plan_actual = None
        if self._memoria.plan_hasta > ahora:
            self._memoria = replace(self._memoria, plan_hasta=ahora)
        movimientos: list[dict[str, Any]] = []
        for paso in finales:
            mov = tuple(c for c in paso.comandos if c.get("type") == "move")
            resto = tuple(c for c in paso.comandos if c.get("type") not in ("move", "desktop"))
            movimientos.extend(mov)
            if resto:
                self._diferidos.append(Paso(0.0, resto, True, paso.reposo))
        parar = ([dict(PARAR_DESKTOP)] if parar_desktop and ahora < self._desktop_hasta
                 and self.escritorio_disponible(parar=True) else [])
        if self._mov is not None and not movimientos:          # paseo sin giro final: se detiene igual
            movimientos.append(_girar(0.0 if de_frente else self._rot, 0.6 if de_frente else 0.4))
        if de_frente and (movimientos or abs(self._rot) > 3.0):
            return parar + [_girar(0.0, 0.6)]
        return parar + movimientos[-1:]                        # basta el último giro

    def _cortar_por_habla(self, ahora: float) -> int:
        """Empieza el habla: corta la conducta y devuelve el cuerpo **ya**, no al terminar de hablar.

        Envía en una lista paralela el giro de frente (detiene el paseo), los pasos finales de la
        conducta (pose ``relaxed``, mirada a cámara, boca ``aa`` y párpados ``blink`` a 0, huesos,
        manos e *idle* restaurados) y, si queda un gesto largo suyo en curso, ``stop`` de gestos (no
        cancela la ``sequence`` del puente). Si caminaba por el escritorio, ``desktop stop`` (aparte:
        nunca en la lista paralela). No pisa lo que gobierna el ``MOTION`` del habla: sin pose
        ni huesos si trae pose (la pose del visor reinicia los huesos tocados), sin mirada si trae
        mirada y sin ``stop`` si trae gesto. Solo la cara de reposo espera a que termine el habla.
        """
        giro = self._interrumpir(ahora, de_frente=True)
        gobierna = self._habla_gobierna
        restaurar: list[dict[str, Any]] = []
        quedan: list[Paso] = []
        for paso in self._diferidos:
            if paso.reposo:                                    # la cara la pone el habla; tras ella, reposo nuevo
                quedan.append(paso)
                continue
            for cmd in paso.comandos:
                tipo = cmd.get("type")
                if tipo in gobierna or (tipo == "bones" and "pose" in gobierna) or cmd in restaurar:
                    continue
                restaurar.append(cmd)
        self._diferidos = quedan
        extras = list(giro)                                    # desktop stop (si caminaba) va primero
        if ahora < self._gesto_hasta and "gesture" not in gobierna:
            extras.append(dict(PARAR_GESTOS))
        # Los finales ya contaban como pendientes; los extras solo salen si caben en el tope.
        libres = max(0, self.presupuesto(ahora) - len(restaurar))
        return self._enviar(restaurar + extras[:libres], ahora, reservado=True)

    def _enviar_diferidos(self, ahora: float) -> int:
        """Envía los finales aplazados; la cara de reposo no se reenvía vieja: se fuerza una nueva."""
        pasos, self._diferidos = self._diferidos, []
        comandos: list[dict[str, Any]] = []
        for paso in pasos:
            if paso.reposo:
                self._forzar_reposo = True
                continue
            for cmd in paso.comandos:
                if cmd not in comandos:
                    comandos.append(cmd)
        return self._enviar(comandos, ahora, reservado=True)

    def _mantenimiento(self, estado: HomeostaticState, ctx: ContextoMotor, ahora: float) -> int:
        """Cara de reposo (EXP sugerida por el estado) e intensidad del idle (según arousal)."""
        enviados = 0
        cara_ocupada = (self._plan_cara and ahora < self._memoria.plan_hasta) or any(
            c.get("type") == "expression" for p in self._agenda for c in p.paso.comandos)
        if not cara_ocupada:
            exp = self.exp_reposo(estado)
            if self._forzar_reposo or (exp != self._exp_reposo and ahora - self._t_reposo >= MIN_ENTRE_REPOSO_S):
                cmd = sanear_comando(comando_expresion(exp, ctx.expresiones, duracion=1.2),
                                     expresiones=ctx.expresiones)
                if cmd is not None and self._enviar([cmd], ahora, reservado=False):
                    self._exp_reposo, self._t_reposo, self._forzar_reposo = exp, ahora, False
                    enviados += 1
        intensidad = self.intensidad_idle(estado)
        if self._agenda and any(c.get("type") == "idle" for p in self._agenda for c in p.paso.comandos):
            return enviados
        if (self._idle is None or abs(intensidad - self._idle) >= 0.15) and ahora - self._t_idle >= MIN_ENTRE_IDLE_S:
            cmd = sanear_comando({"type": "idle", "intensity": intensidad})
            if cmd is not None:
                enviados += self._enviar([cmd], ahora, reservado=False)
        return enviados

    def _ejecutar_prioritario(self, plan: Plan | None, ahora: float, previos: Sequence[dict[str, Any]]) -> int:
        """Lanza un plan prioritario (atención, Reading Steiner) con el tope completo de comandos."""
        if plan is None:
            return self._enviar(list(previos), ahora, reservado=False)
        if len(plan.comandos()) + len(previos) > self.presupuesto(ahora):
            log.debug("Sin presupuesto de comandos para %s", plan.conducta)
            return self._enviar(list(previos), ahora, reservado=False)
        # Los finales aplazados (salvo mirada/giro, que el plan ya decide) salen con el primer paso.
        extra = [c for p in self._diferidos for c in p.comandos if c.get("type") not in ("look", "move")]
        self._diferidos = []
        primero = plan.pasos[0]
        pasos = (Paso(primero.t, tuple(list(previos) + extra + list(primero.comandos)), primero.final,
                      primero.reposo),) + plan.pasos[1:]
        self._programar(replace(plan, pasos=pasos), ahora)
        return self._despachar_agenda(ahora)

    def tick(self, ahora: float | None = None) -> int:
        """Un paso de control (no bloquea). Devuelve cuántos comandos encoló."""
        if not self.activa or self._cerrado:
            return 0
        ahora = self._reloj() if ahora is None else ahora
        self._publicar_diag()
        if not self._avatar_en_linea():
            if self._era_en_linea:
                self._agenda, self._diferidos, self._mov = [], [], None
                self._plan_actual = None
                self._memoria = replace(self._memoria, plan_hasta=-math.inf)
            self._era_en_linea = False
            return 0
        if not self._era_en_linea:                             # (re)conexión: cara e idle desde cero
            self._era_en_linea = True
            self._forzar_reposo, self._idle = True, None
            self._aplazar(ahora + 1.5)
            try:                                               # un visor recargado vuelve a "relaxed"
                visor = getattr(self.avatar, "estado_visor", None)
                pose = visor.get("pose") if isinstance(visor, Mapping) else None
            except Exception as exc:
                log.debug("Estado del visor ilegible al reconectar: %s", type(exc).__name__)
                pose = None
            self._pose = pose if pose in POSES_VISOR else "relaxed"
        if self._apagando:
            if self._agenda:
                movimientos = self._interrumpir(ahora, de_frente=True)
                if movimientos:
                    self._diferidos.append(Paso(0.0, tuple(movimientos), True))
            return 0
        self._sincronizar_posicion()
        enviados = 0
        if self._esta_hablando(ahora):
            if not self._hablaba:                              # empieza a hablar: corta y devuelve el cuerpo ya
                self._hablaba = True
                enviados += self._cortar_por_habla(ahora)
            self._atencion_pendiente = False                   # el habla ya es la respuesta
            return enviados
        if self._hablaba:                                      # terminó de hablar
            self._hablaba = False
            self._forzar_reposo = True                         # el habla dejó su propia cara
            self._aplazar(ahora + CALMA_TRAS_HABLAR_S)
        self._habla_gobierna = frozenset()
        if self._ceder_cuerpo:                                 # entra o sale por el borde: corta la conducta
            self._ceder_cuerpo = False
            self._ceder_cuerpo_a_electron(ahora)
        if self._diferidos:                                    # finales aplazados: la pose no queda colgada
            enviados += self._enviar_diferidos(ahora)
        estado = self._estado
        self._caducar_pendientes(ahora)
        self._vigilar_fatiga(estado, ahora)
        if ahora <= self._steiner_hasta:
            self._steiner_hasta = -math.inf
            self._ultimo_steiner = ahora
            self._atencion_pendiente = False
            previos = self._interrumpir(ahora, de_frente=True)
            enviados += self._ejecutar_prioritario(self.plan_reading_steiner(estado, self.contexto(ahora)), ahora,
                                                   previos)
            self._aplazar(self._memoria.plan_hasta + 1.0)
        elif self._reaccion is not None:
            enviados += self._ejecutar_reaccion(estado, ahora)
        elif self._atencion_pendiente:
            self._atencion_pendiente = False
            if self._atencion_escritorio or self.cuerpo_en_escritorio():    # escritorio: sin acercarse
                previos = [c for c in self._interrumpir(ahora) if c.get("type") == "desktop"]
                plan = self.plan_atencion_escritorio(estado, self.contexto(ahora))
            else:
                self._interrumpir(ahora)                       # la atención decide su propio giro
                previos, plan = [], self.plan_atencion(estado, self.contexto(ahora))
            enviados += self._ejecutar_prioritario(plan, ahora, previos)
            self._aplazar(ahora + 3.0)
        elif self._solicitud is not None and self.escritorio_disponible():
            enviados += self._ejecutar_solicitud(estado, ahora)
        enviados += self._despachar_agenda(ahora)
        enviados += self._mirar_al_usuario(ahora)
        ctx = self.contexto(ahora)
        enviados += self._mantenimiento(estado, ctx, ahora)
        enviados += self._quizas_travesura(estado, ctx, ahora)
        if self._cuerpo_de_electron(ahora):                    # entra o sale por el borde: nada nuevo
            return enviados
        decision = self.decidir(estado, ahora, replace(ctx, presupuesto=self.presupuesto(ahora)), self._memoria)
        self._memoria = decision.memoria
        if decision.plan is not None:
            self._programar(decision.plan, ahora)
            enviados += self._despachar_agenda(ahora)
        return enviados

    # ------------------------------------------------------------------ escritorio: E/S del tic
    def _caducar_pendientes(self, ahora: float) -> None:
        """Descarta la solicitud de main y la reacción que caducaron sin poder ejecutarse."""
        if self._solicitud is not None and ahora > self._solicitud.vence:
            log.debug("Solicitud motora %s caducada sin ventana visible ni silencio; se descarta",
                      self._solicitud.conducta)
            self._solicitud = None
        if self._reaccion is not None and ahora - self._reaccion[2] > REACCION_CADUCIDAD_S:
            self._reaccion = None

    def _ceder_cuerpo_a_electron(self, ahora: float) -> None:
        """Pasó a visible (``mostrada``) o empezó a salir (``esconderse``): el main de Electron la lleva
        caminando por el borde. Corta la conducta en curso (decidida para el escenario o para quedarse):
        sus giros se descartan (pisarían la orientación de esa caminata) y los demás finales (pose,
        mirada, cara) se aplazan y salen ya. Al entrar, sin conductas nuevas durante ``GRACIA_ENTRADA_S``;
        al salir, ninguna hasta ``ocultada`` (lo vigila ``_cuerpo_de_electron``)."""
        self._interrumpir(ahora, parar_desktop=False)
        self._aplazar(self._t_mostrada + GRACIA_ENTRADA_S)

    def _vigilar_fatiga(self, estado: HomeostaticState, ahora: float) -> None:
        """Fatiga > 0,8 sostenida 10 min **con ella a la vista** → se esconde (una vez por episodio).

        Solo cuenta el tiempo visible: oculta no hay nada que esconder, y el cansancio acumulado a
        escondidas no puede echarla en cuanto la llaman o sale por una iniciativa de main (§C1.11:
        "solo reaparece si la llaman"). Cada vez que se muestra, el reloj empieza de cero."""
        if estado.fatiga <= FATIGA_ESCONDERSE:
            self._fatiga_alta_desde, self._escondida_por_fatiga = None, False
            return
        if not self._escritorio_visible or self._saliendo:
            self._fatiga_alta_desde = None
            return
        if self._fatiga_alta_desde is None:
            self._fatiga_alta_desde = ahora
            return
        if not self._escondida_por_fatiga and ahora - self._fatiga_alta_desde >= FATIGA_SOSTENIDA_S:
            self._escondida_por_fatiga = True
            self._esconderse("cansancio")

    def _presupuesto_tras_interrumpir(self, ahora: float) -> int:
        """Presupuesto que quedaría al cortar la conducta en curso (sus pasos no finales se liberan)."""
        return self.presupuesto(ahora) + sum(len(p.paso.comandos) for p in self._agenda if not p.paso.final)

    def _ejecutar_solicitud(self, estado: HomeostaticState, ahora: float) -> int:
        """Ejecuta la solicitud retenida (ventana visible y silencio). Sin presupuesto, sigue esperando."""
        solicitud = self._solicitud
        if solicitud is None:
            return 0
        plan = self.plan_solicitud(solicitud, estado, self.contexto(ahora))
        if plan is None:
            self._solicitud = None
            return 0
        if len(plan.comandos()) + 2 > self._presupuesto_tras_interrumpir(ahora):
            return 0                                           # sin tope ahora: lo reintenta (hasta ttl_s)
        self._solicitud = None
        self._atencion_pendiente = False
        # La acción desktop nueva sustituye a la locomoción en curso: no hace falta un stop antes.
        previos = self._interrumpir(ahora, parar_desktop=False)
        enviados = self._ejecutar_prioritario(plan, ahora, previos)
        self._aplazar(self._memoria.plan_hasta + 1.0)
        log.debug("Solicitud motora ejecutada: %s (%s)", solicitud.conducta, solicitud.motivo or "sin motivo")
        return enviados

    def _ejecutar_reaccion(self, estado: HomeostaticState, ahora: float) -> int:
        """Sobresalto o sonrojo por un toque o un arrastre sobre la caja del avatar."""
        reaccion, self._reaccion = self._reaccion, None
        if reaccion is None:
            return 0
        nombre, datos, _t = reaccion
        self._t_reaccion = ahora
        previos = self._interrumpir(ahora, de_frente=True)
        enviados = self._ejecutar_prioritario(self.plan_reaccion(nombre, datos, estado, self.contexto(ahora)),
                                              ahora, previos)
        self._aplazar(self._memoria.plan_hasta + 1.0)
        return enviados

    def _agenda_mira(self) -> bool:
        """``True`` si la conducta en curso todavía tiene que mover la mirada (no se le pisa)."""
        return any(c.get("type") == "look" for p in self._agenda for c in p.paso.comandos)

    def _mirar_al_usuario(self, ahora: float) -> int:
        """Sigue al usuario con la mirada (``vision.mirada``) o lo mira si su rostro se puso de frente.

        Un ``look`` a su punto como mucho cada ``MIRADA_MIN_ENTRE_S`` y solo si se movió más de
        ``MIRADA_UMBRAL_M`` respecto al último punto que miró; nunca pisa la mirada de una conducta en
        curso y deja libre la reserva prioritaria del tope de comandos (cuenta en ``LIMITE_COMANDOS_MIN``).
        Además tiene su propio sub-tope (cubo de fichas: ``MIRADA_MAX_MIN`` por minuto con ráfagas de
        ``MIRADA_RAFAGA``): con un usuario que se mueve mucho lo sigue a vistazos, sin gastarse el tope que
        necesitan las conductas autónomas (pasear, asomarse, buscarlo…). El flanco de rostro frontal (ya
        limitado a uno cada ``ROSTRO_MIN_ENTRE_S``) no espera a tener ficha.
        """
        rostro = ahora <= self._mirar_usuario_hasta
        if self._agenda_mira() or self.presupuesto(ahora) <= RESERVA_PRIORITARIA:
            return 0
        fichas = self._recargar_fichas_mirada(ahora)
        mirada = self._mirada_util(ahora)
        cmd: dict[str, Any] | None = None
        if mirada is not None and ahora - self._t_mirada_usuario >= MIRADA_MIN_ENTRE_S and (rostro or fichas >= 1.0):
            punto = (mirada.x, mirada.y, mirada.z)
            anterior = self._punto_mirada
            if rostro or anterior is None or math.dist(punto, anterior) > MIRADA_UMBRAL_M:
                cmd = sanear_comando(_mirar_punto(punto))
        elif mirada is None and rostro:
            cmd = _mirar("camera", 0.35)                       # rostro de frente sin punto: a cámara
        if cmd is None:
            return 0
        enviados = self._enviar([cmd], ahora, reservado=False)
        if enviados:
            self._t_mirada_usuario = ahora
            self._fichas_mirada = max(0.0, self._fichas_mirada - 1.0)
            if rostro:
                self._mirar_usuario_hasta, self._t_mirar_usuario = -math.inf, ahora
        return enviados

    def _recargar_fichas_mirada(self, ahora: float) -> float:
        """Recarga el cubo de fichas de la mirada (``MIRADA_MAX_MIN``/min, máx. ``MIRADA_RAFAGA``)."""
        if self._t_fichas_mirada is not None and ahora > self._t_fichas_mirada:
            self._fichas_mirada = min(float(MIRADA_RAFAGA), self._fichas_mirada
                                      + (ahora - self._t_fichas_mirada) * MIRADA_MAX_MIN / 60.0)
        if self._t_fichas_mirada is None or ahora > self._t_fichas_mirada:
            self._t_fichas_mirada = ahora
        return self._fichas_mirada

    def patron_travesura(self, estado: HomeostaticState, rng: random.Random | None = None) -> str:
        """Patrón de la travesura según el ánimo: empujón (lo normal), hacia ella (lo "agarra"),
        círculo (juguetona) o temblor (molesta)."""
        r = self._rng if rng is None else rng
        pesos = {"empujon": 1.0, "hacia_avatar": 0.8, "circulo": 0.3 + max(0.0, estado.valencia),
                 "temblor": 0.2 + max(0.0, -estado.valencia)}
        return self._elegir({p: w for p, w in pesos.items() if p in PATRONES_TRAVESURA}, r)

    def _quizas_travesura(self, estado: HomeostaticState, ctx: ContextoMotor, ahora: float) -> int:
        """Propone una travesura de cursor publicando ``motor.travesura`` (la ejecuta main tras la compuerta
        y ControlPC; aquí nunca se toca el ratón). Solo con travesuras activas, la ventana visible, sin
        habla (el tic ya calló), sin "no molestar", aburrimiento > 0,6, ≤ 1 cada 15 min y ≤ 6 en 24 h.
        Devuelve los comandos de cuerpo que la acompañan (mirar al ratón con suficiencia)."""
        if (self.bus is None or not self.escritorio_habilitado or not self._travesuras_activas
                or not self.escritorio_disponible() or ctx.hablando
                or estado.boredom <= TRAVESURA_ABURRIMIENTO or ctx.desde_usuario_s < ATENCION_S
                or ahora < self._travesura_proxima):
            return 0
        while self._travesuras and self._travesuras[0] <= ahora - TRAVESURA_VENTANA_S:
            self._travesuras.popleft()
        if len(self._travesuras) >= TRAVESURAS_MAX_DIA:
            return 0
        maximo = int(_acotar(_num(getattr(self.settings, "travesura_amplitud_max_px", 40), 40.0) or 40.0,
                             *TRAVESURA_AMPLITUD_RANGO))
        datos = {
            "patron": self.patron_travesura(estado),
            "amplitud_px": int(round(_acotar(maximo * (0.5 + 0.5 * estado.boredom), TRAVESURA_AMPLITUD_RANGO[0],
                                             maximo))),
            "segundos": round(_acotar(0.3 + 0.3 * estado.arousal, 0.2, TRAVESURA_SEGUNDOS_MAX), 2),
        }
        try:
            self.bus.publicar("motor.travesura", datos, origen="corteza_motora")
        except Exception as exc:
            log.debug("No se pudo publicar motor.travesura: %s", type(exc).__name__)
            return 0
        self._travesuras.append(ahora)
        self._travesura_proxima = ahora + TRAVESURA_MIN_ENTRE_S + self._rng.uniform(0.0, TRAVESURA_MARGEN_S)
        log.debug("Travesura propuesta: %s (%d px)", datos["patron"], datos["amplitud_px"])
        if self._agenda:                                       # no corta una conducta en curso
            return 0
        pasos = [_p(0.0, _mirar("mouse", 0.5), self._mezcla("smug", ctx, escala=0.7)),
                 _p(2.5, _mirar_al_usuario(ctx, 0.35), final=True), self._paso_reposo(2.7, estado, ctx)]
        plan = self._plan("travesura", pasos, ctx)
        if plan is None or len(plan.comandos()) > self.presupuesto(ahora) - RESERVA_PRIORITARIA:
            return 0
        self._programar(plan, ahora)
        return self._despachar_agenda(ahora)

    async def bucle(self, parar: asyncio.Event) -> None:
        """Llama a ``tick`` cada ``TICK_S`` hasta ``parar`` (cancelable; un tic que falla no la detiene)."""
        if not self.activa:
            self._publicar_diag()
            await parar.wait()
            return
        self._poner_reposo_externo(True)
        while not parar.is_set() and not self._cerrado:
            try:
                self.tick()
            except Exception as exc:
                log.debug("Fallo en el tic de la corteza motora: %s", type(exc).__name__, exc_info=True)
            try:
                await asyncio.wait_for(parar.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass

    async def cerrar(self) -> None:
        """Deja el cuerpo en reposo (finales pendientes), devuelve la cara al puente y se desuscribe.
        Idempotente."""
        if self._cerrado:
            return
        ahora = self._reloj()
        try:
            if self.activa and self._avatar_en_linea():
                movimientos = self._interrumpir(ahora, de_frente=True)
                comandos: list[dict[str, Any]] = []
                for cmd in [c for p in self._diferidos for c in p.comandos if not p.reposo] + movimientos:
                    if cmd not in comandos:                    # p. ej. un desktop stop ya aplazado al apagar
                        comandos.append(cmd)
                self._diferidos = []
                self._enviar(comandos, ahora, reservado=True)  # al cerrar el tope ya no importa
        except Exception as exc:
            log.debug("No se pudo dejar el avatar en reposo al cerrar: %s", type(exc).__name__)
        self._cerrado = True
        self._agenda, self._diferidos = [], []
        for desuscribir in self._desuscribir:
            try:
                desuscribir()
            except Exception as exc:
                log.debug("Error al desuscribir la corteza motora: %s", type(exc).__name__)
        self._desuscribir = []
        if self.avatar is not None:
            self._poner_reposo_externo(False)
