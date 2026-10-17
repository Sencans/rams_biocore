"""Conectoma LIF de *Drosophila* (Complejo Central + Cuerpo Fungiforme): el "tronco encefálico" sin LLM.

Qué hace
--------
Simula en tiempo real una red *Leaky Integrate-and-Fire* reducida (≈1 262 neuronas LIF más
72 entradas Poisson) inspirada en el Complejo Central (CX: anillo E-PG/P-EN/P-EG/Δ7/ER y el
circuito de sueño ER5→dFB↔ExR1) y en el Cuerpo Fungiforme (MB: KC con inhibición APL,
MBON-α'3 de novedad y DAN PAM/PPL1 con plasticidad KC→MBON). De su actividad salen, sin gastar
un solo token, el estado homeostático de Amadeus (arousal, focus, boredom, fatiga, curiosidad,
valencia), el valor de divergencia con su campo de atracción (α, Steins Gate, β) y las
**iniciativas** (conversar, mirar por la cámara, sugerir un descanso, comentar un cambio de
línea de mundo). Es la fuente de las "emociones" que Amadeus cita como prueba ("mi arousal
subió a 0,71 cuando volviste").

Base científica (citas)
-----------------------
- Shiu, P. K. et al. (2024). *A Drosophila computational brain model reveals sensorimotor
  processing.* Nature 634:210–219. Neurona LIF y parámetros: V_rest = −52 mV, V_th = −45 mV,
  τ_m = 20 ms, τ_syn = 5 ms, refractario 2,2 ms, retardo 1,8 ms, 0,275 mV por sinapsis.
- Dorkenwald, S. et al. (2024). *Neuronal wiring diagram of an adult brain.* Nature 634:124–138
  (conectoma FlyWire; ver ``core/flywire_loader.py``, herramienta offline en v0.1).
- Hulse, B. K. et al. (2021). *A connectome of the Drosophila central complex reveals network
  motifs suitable for flexible navigation and context-dependent action selection.* eLife
  10:e66039 (anillo E-PG/P-EN/Δ7/ER, ER5 y dFB).
- Hattori, D. et al. (2017). *Representations of novelty and familiarity in a mushroom body
  compartment.* Cell 169:956–969 (MBON-α'3: novedad y habituación dopaminérgica).
- Borbély, A. A. (1982); Daan, Beersma & Borbély (1984). Modelo de dos procesos del sueño
  (proceso S homeostático con τ_w = 18,2 h y τ_s = 4,2 h, y proceso C circadiano).

Arquitectura (Anexo A y §10.6 de docs/ARQUITECTURA.md, revisión 2)
------------------------------------------------------------------
- ``_RedReducida``: poblaciones y conectividad (A.3) con ``numpy.random.default_rng(semilla_red)``
  para la topología y un generador **separado** para el ruido OU y las entradas Poisson; núcleo
  LIF con Euler exponencial, CSR manual y ``np.bincount`` (A.1–A.2), APL graduada, plasticidad
  KC→MBON en matrices densas y un planificador de estímulos con decaimiento (A.4).
- ``_LectorHomeostatico``: tasas como EMA (τ = 1 s; 0,3 s para MBON-α'3), *z*-scores y lecturas
  homeostáticas (A.5).
- ``_ProcesoSueno``: proceso S con tiempo de reloj × ``escala_tiempo``.
- ``_ModeloSimple``: modo *stub* en Python puro (A.6) cuando falta numpy o la máquina no da.
- ``MedidorDivergencia`` y ``MotorIniciativa``: Python puro, compartidos por todas las fuentes.

Concurrencia (§6.1–6.2)
-----------------------
``avanzar``, el *benchmark*, la calibración y ``guardar_estado`` corren en un
``ThreadPoolExecutor(1, "rams-conectoma")`` propio. ``inyectar`` solo encola en un ``deque``
(thread-safe) y nunca lanza. ``estado()``, ``snapshot_hud()`` y ``diagnostico_interno()`` leen
objetos inmutables o copias publicadas al final de cada ``avanzar``: son seguros desde el loop.

Desviaciones deliberadas del Anexo A (medidas y justificadas junto a cada constante)
------------------------------------------------------------------------------------
- Landmark tónico de 8 mV (A.4: 1,5 mV); el landmark del rostro cancela el tónico mientras dura.
- Zona muerta de 2,5 Hz en ν (``_ZONA_MUERTA_NOVEDAD_HZ``): sin ella el ruido de MBON-α'3 estanca B en ≈ 0,5.
- ω compara fases separadas 0,6 s (``_VENTANA_OMEGA_S``) en vez de chunks contiguos.
- Calibración: ``r_base``/``ā_base`` = media de [3; 5 s) y A reproducida con esa base (A.5: EMA al
  final); la componente Fo de X_EQ se promedia en [2,5; 10 s) (A.5: últimos 5 s).
- Curiosidad con EMA asimétrica: sube con τ = 0,5 s y baja con los 2 s de A.5.

Dependencias y degradación
--------------------------
numpy es opcional: sin él (o si el *benchmark* agota la cascada normal → ligero → dt2) se usa el
modo simple, que produce un estado igual de válido y se reporta como ``DEGRADADO``. Ninguna API
pública propaga excepciones: ante un fallo interno se registra y se devuelve el último estado
válido. FlyWire no es una fuente de tiempo real en v0.1.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import secrets
import threading
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Final

from core.config import Settings, zona_local
from core.contracts import (
    DIVERGENCIA_STEINS_GATE,
    LINEAS_CANONICAS,
    DiagnosticoModulo,
    EstadoModulo,
    HomeostaticState,
    formatear_decimal,
)

try:
    import numpy as np  # opcional: sin numpy el conectoma usa el modo simple
except ImportError:  # sin numpy: modo stub (Anexo A.6)
    np = None  # type: ignore[assignment]

__all__ = [
    "X_EQ_DEFECTO",
    "VERSION_RED",
    "SincanConnectome",
    "MedidorDivergencia",
    "MotorIniciativa",
    "patron_pn",
    "ciclo_circadiano",
]

log = logging.getLogger("rams.conectoma")

# ---------------------------------------------------------------------------
# Constantes públicas
# ---------------------------------------------------------------------------

X_EQ_DEFECTO: Final = (0.55, 0.70, 0.20, 0.25)    # (arousal, focus, boredom, fatiga) si no hay calibración
VERSION_RED: Final = 2                            # cambia si cambia la construcción de la red

# ---------------------------------------------------------------------------
# Constantes internas (Anexo A)
# ---------------------------------------------------------------------------

_SG: Final = DIVERGENCIA_STEINS_GATE
_VERSION_ESTADO: Final = 2
_NOMBRE_DIAGNOSTICO: Final = "Conectoma CX+MB (LIF)"

_N_COL: Final = 16                       # columnas angulares del elipsoide
_N_PN_TEXTO: Final = 64                  # PN de texto (entradas Poisson)
_K_PN: Final = 12                        # PN activas por estímulo (A.3: 64/12)
_N_PN_INTERO: Final = 8                  # PN interoceptivas (población aparte, índices 64–71)
_N_ENTRADAS: Final = _N_PN_TEXTO + _N_PN_INTERO

# Neurona LIF (A.1), en mV relativos al reposo: u = v − V_rest.
_THETA_MV: Final = 7.0
_TAU_M_MS: Final = 20.0
_TAU_S_MS: Final = 5.0
_TAU_A_MS: Final = 200.0
_T_REF_MS: Final = 2.2
_T_RET_MS: Final = 1.8
_W_SYN_MV: Final = 0.275
_DELTA_A_MV: Final = 2.0
_SIGMA_MV: Final = 1.5
_SIGMA_KC_MV: Final = 1.0

# Tasas de fondo y de estímulo de las PN (A.3).
_PN_FONDO_HZ: Final = 2.0
_PN_ACTIVA_HZ: Final = 50.0

# Plasticidad KC→MBON (A.3).
_TAU_TRAZA_MS: Final = 500.0
_ETA_POR_MS: Final = 3e-4
_TAU_RECUPERACION_MS: Final = 30.0 * 60.0 * 1000.0
_BLOQUE_PASOS_MS: Final = 10.0           # trazas, W y estímulos se actualizan cada 10 ms

# Lecturas (A.5).
_TAU_TASA_S: Final = 1.0
_TAU_TASA_A3_S: Final = 0.3
_R_NOVEL_DEFECTO: Final = 35.0
_R_NOVEL_MIN: Final = 20.0
_VENTANA_FOCUS_S: Final = 0.2           # ventana de spikes E-PG para ρ y φ
# Separación entre las fases que se comparan para ω. Con 0,2 s, ω mide sobre todo el temblor del bump
# anclado (≈ 1,25 rad/s en reposo, con modulaciones lentas de ±0,4) y focus en reposo queda en 0,57 ± 0,06
# con medias de 5 s que varían ±0,04: el X_EQ calibrado salía poco representativo. Con 0,6 s ω mide la
# deriva del rumbo: focus en reposo ≈ 0,71 (como X_EQ_DEFECTO y el modo simple), menos ruido y una caída
# mayor ante un cambio de ventana real (relación señal/ruido 2,1 frente a 1,6).
_VENTANA_OMEGA_S: Final = 0.6
# EMA de la curiosidad: A.5 da τ = 2 s. La sorpresa de un mensaje nuevo dura < 1 s (patrón PN de
# 300–800 ms) y a la vez la novedad vacía el aburrimiento (−B·ν/3), así que con τ = 2 s C se queda en
# 0,47–0,63 incluso con ν = 1 tras un rato de aburrimiento y ``curiosidad_visual`` (> 0,65) casi nunca
# dispara (3 de 16 casos). Como el arousal (2 s / 8 s), C sube rápido (τ = 0,5 s: 11 de 16) y baja con los
# 2 s de A.5; el punto fijo no cambia (en reposo con B = 0,99, C ≤ 0,63: no hay vistazos espontáneos).
# Desviación deliberada de A.5.
_TAU_C_S: Final = 2.0
_TAU_C_SUBIDA_S: Final = 0.5
_TAU_W_S: Final = 18.2 * 3600.0          # proceso S en vigilia
_TAU_S_SUENO_S: Final = 4.2 * 3600.0     # proceso S en reposo

# Zona muerta de la novedad (A.5). ν se calcula sobre la EMA de MBON-α'3 (τ = 0,3 s; solo 4 neuronas a
# ~1 Hz): en reposo esa EMA es ruido de disparo de ±0,6 Hz con picos de 2–4 Hz. Sin zona muerta el
# ``clip(…, 0, 1)`` rectifica ese ruido (ν medio ≈ 0,008) y el término −B·ν/3 equilibra el crecimiento
# del aburrimiento: B se estanca en ≈ 0,5 y ``conversar`` (B > 0,75) nunca dispara. Las respuestas a lo
# nuevo son de 7–45 Hz, así que restar 2,5 Hz no las altera: ν = clip((r − r_base − δ)/(r_novel − r_base − δ)).
# Desviación deliberada de A.5.
_ZONA_MUERTA_NOVEDAD_HZ: Final = 2.5

# Calibración y rendimiento (§10.6).
_CALIBRACION_S: Final = 10.0
_CALIBRACION_CHUNK_MS: Final = 50
# Reparto de los 10 s (A.5): ajuste de sesgos [0; 2,5 s), asentamiento con los sesgos ya congelados y línea
# base = MEDIA de la EMA en [3; 5 s) (la EMA instantánea de PPL1 o MBON-α'3 tiene un ruido del 20–50 %), y
# X_EQ = media de (A, Fo, B, F) en los últimos 5 s.
_CAL_AJUSTE_S: Final = 2.5
_CAL_BASE_S: Final = 2.0
_UMBRAL_BENCH_S_POR_MS: Final = 300e-6   # 300 µs por paso de 1 ms (= 15 ms por chunk de 50 ms)
_TANDAS_BENCH: Final = 3                 # se baja de escalón solo si TODAS las tandas superan el umbral
_MODOS: Final = ("normal", "ligero", "dt2", "stub")
_FACTOR_LENTO: Final = 0.8
_T_LENTO_S: Final = 30.0
_GUARDADO_CADA_S: Final = 300.0
_TAU_FACTOR_TR_S: Final = 5.0

# Landmarks (A.4). El tónico mantiene el bump E-PG en el último rumbo sin estímulos. A.4 fija 1,5 mV
# (y 2 mV para Nexus), pero con la red de A.3 ese valor solo sostiene el bump unos segundos: en
# simulaciones de 30–60 s el bump se suelta del landmark varias veces por minuto (ω ≈ 3 rad/s, focus
# 0,3 ± 0,09) y el bloqueo SG del medidor se rompe en reposo. Con 8 mV (misma razón 4:3 para Nexus) el
# anillo conserva sus tasas de A.3 y el focus en reposo queda en 0,56 ± 0,06. Desviación deliberada de A.4.
_LANDMARK_TONICO_MV: Final = 8.0
_LANDMARK_NEXUS_MV: Final = 10.5
_LANDMARK_EVENTO_MV: Final = 6.0         # ventana_cambia y rostro: 6 mV con perfil cos², 600 ms
# Consecuencia del tónico de 8 mV: un landmark transitorio de 6 mV en OTRA columna (``rostro``) ya no
# desplaza el bump. Mientras dura el landmark del rostro, el tónico se cancela con un estímulo negativo de
# la misma duración y decaimiento (vuelve solo al terminar): el rumbo gira hacia el usuario y luego regresa
# a la ventana activa, como el transitorio que describe A.4.
_DURACION_LANDMARK_MS: Final = 600.0

# Reposo (§10.6).
_AUSENCIA_REPOSO_S: Final = 600.0
_RECORTE_SENSORIAL_REPOSO: Final = 0.3

_EVENTOS: Final = frozenset({
    "usuario_escribe", "ventana_cambia", "carga_sistema", "rostro", "busqueda_completa",
    "error", "recompensa", "nexus_foco", "usuario_presencia",
})

# Poblaciones del modelo reducido, en orden de índice (A.3). Los tamaños de KC dependen del modo.
_ORDEN_POBLACIONES: Final = (
    "EPG", "PEN1", "PEN2", "PEG", "D7", "ER_vis", "ER5", "ExR1", "dFB",
    "KC_g", "KC_ab", "KC_apbp", "MBON_a3", "MBON_app", "MBON_avd", "MBON_otr", "PAM", "PPL1",
)
_TAM_FIJO: Final = {
    "EPG": 48, "PEN1": 32, "PEN2": 32, "PEG": 16, "D7": 32, "ER_vis": 48, "ER5": 16, "ExR1": 8,
    "dFB": 24, "MBON_a3": 4, "MBON_app": 12, "MBON_avd": 12, "MBON_otr": 6, "PAM": 60, "PPL1": 12,
}
_TAM_KC_NORMAL: Final = {"KC_g": 300, "KC_ab": 450, "KC_apbp": 150}
_BIAS_MV: Final = {
    "EPG": 3.5, "PEN1": 4.0, "PEN2": 4.0, "PEG": 4.5, "D7": 3.0, "ER_vis": 4.0,
    "ER5": 0.0, "dFB": 0.0,            # dinámicos: ER5 = 2 + 4·P; dFB = 1,5 + 2·(c(h) − 0,5) (+3 en reposo)
    "ExR1": 3.0, "KC_g": 0.0, "KC_ab": 0.0, "KC_apbp": 0.0,
    "MBON_a3": 3.0, "MBON_app": 3.0, "MBON_avd": 3.0, "MBON_otr": 3.0, "PAM": 2.5, "PPL1": 2.5,
}
_ANILLO: Final = frozenset({"EPG", "PEN1", "PEN2", "PEG", "D7", "ER_vis"})   # sin SFA (A.1)
# MBON-α'3 tampoco lleva SFA: su respuesta de novedad (30–45 Hz, A.3) se calibró [P] sin adaptación
# y con 2 mV por spike quedaría por debajo de 10 Hz (la novedad, el aburrimiento y la curiosidad
# perderían la señal).
_SIN_SFA: Final = _ANILLO | frozenset({"MBON_a3"})
_KC: Final = ("KC_g", "KC_ab", "KC_apbp")
# Los sesgos [P] de A.3 se calibraron sin SFA. Con SFA (Δa = 2 mV, τ_a = 200 ms) la adaptación media
# a una tasa r resta Δa·r·τ_a mV; se compensa en el sesgo para que la red arranque cerca de su tasa
# objetivo incluso sin calibración (la calibración de iniciar() afina el resto).
_COMPENSACION_SFA_MV: Final = {
    "ER5": 0.8, "ExR1": 1.4, "dFB": 0.5, "MBON_app": 1.4, "MBON_avd": 1.4, "MBON_otr": 1.4,
    "PAM": 1.2, "PPL1": 0.8,
}

# Claves de tasas (EMA) que se leen y se publican, y sus objetivos de A.3 (punto medio del rango).
_CLAVES_TASA: Final = (
    "EPG", "PEN1", "PEN2", "PEG", "D7", "ER_vis", "ER5", "ExR1", "dFB", "KC", "MBON_a3", "MBON",
    "PAM", "PPL1", "glob",
)
_TASAS_OBJETIVO: Final = {
    "EPG": 10.0, "PEN1": 6.5, "PEN2": 6.5, "PEG": 7.5, "D7": 12.5, "ER_vis": 4.5, "ER5": 2.0,
    "ExR1": 3.5, "dFB": 1.25, "KC": 0.05, "MBON_a3": 0.75, "MBON": 3.5, "PAM": 3.0, "PPL1": 2.0,
    "glob": 5.5,
}
# Poblaciones que la calibración adapta y tope del ajuste de sesgo, en mV. Quedan fuera dFB y ER5 (A.5),
# las KC (A.3 no les da tasa basal: su escasez la fija la APL) y el anillo E-PG/P-EN/P-EG/Δ7/ER: sus
# tasas son una propiedad emergente del bump (ráfagas de 0 a 60 Hz por columna) y ajustar sus sesgos
# con la EMA lo apaga (EPG cae a ~4 Hz y el foco se pierde). Los valores [P] del anillo ya caen dentro
# de sus rangos sin calibrar.
_CALIBRABLES: Final = {
    "ExR1": 2.0, "MBON_a3": 2.0, "MBON": 2.0, "PAM": 2.0, "PPL1": 2.0,
}
# Rangos de tasa objetivo de A.3 (Hz). La calibración solo corrige lo que cae FUERA del rango (hacia
# el extremo más cercano): el anillo es un atractor delicado y empujar sus poblaciones hacia el punto
# medio (p. ej. más Δ7 o más ER) apaga el bump aunque ya estuviera dentro de su rango.
_RANGO_OBJETIVO: Final = {
    "EPG": (8.0, 12.0), "PEN1": (5.0, 8.0), "PEN2": (5.0, 8.0), "PEG": (5.0, 10.0), "D7": (10.0, 15.0),
    "ER_vis": (3.0, 6.0), "ExR1": (2.0, 5.0), "MBON_a3": (0.5, 1.0), "MBON": (2.0, 5.0), "PAM": (2.0, 4.0),
    "PPL1": (1.0, 3.0),
}
_GANANCIA_CALIBRACION: Final = 0.3       # mV por Hz de error y por segundo (τ ≈ 2 s con ~1,5 Hz/mV)

# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _h(s: str, n: int) -> int:
    """Hash determinista (blake2b, 4 bytes) de ``s`` módulo ``n`` (Anexo A.4)."""
    datos = s.encode("utf-8", "surrogatepass")
    return int.from_bytes(hashlib.blake2b(datos, digest_size=4).digest(), "little") % n


def patron_pn(texto: str, n_pn: int = _N_PN_TEXTO, k: int = _K_PN) -> list[int]:
    """Devuelve SIEMPRE ``k`` índices distintos de PN de texto (0..n_pn-1), deterministas para el mismo texto.

    Codificación "olfativa" de símbolos (Anexo A.4): los trigramas de caracteres votan por PN;
    textos parecidos comparten PN (la familiaridad generaliza) y el mismo texto da el mismo
    patrón (se habitúa). Los textos cortos se completan con bigramas de palabras y, si aún
    faltan, con una expansión determinista con sal.
    """
    n_pn = max(1, int(n_pn))
    k = max(0, min(int(k), n_pn))
    if not isinstance(texto, str):
        texto = "" if texto is None else str(texto)
    t = " " + " ".join(texto.lower().split()) + " "
    votos = collections.Counter(_h(t[i:i + 3], n_pn) for i in range(max(1, len(t) - 2)))
    elegidos = [p for p, _ in votos.most_common(k)]
    # Relleno 1: bigramas de palabras hasheados (textos cortos: "ok", "gracias").
    palabras = t.split()
    for a, b in zip(palabras, palabras[1:]):
        if len(elegidos) >= k:
            break
        p = _h(f"{a}_{b}", n_pn)
        if p not in elegidos:
            elegidos.append(p)
    # Relleno 2: expansión determinista con sal.
    j = 0
    while len(elegidos) < k:
        p = _h(f"{t}#{j}", n_pn)
        j += 1
        if p not in elegidos:
            elegidos.append(p)
    return elegidos


def ciclo_circadiano(h: float) -> float:
    """Proceso C (Anexo A.5): presión circadiana de sueño para la hora local decimal ``h``.

    ``c(h) = clip(0,5 + 0,4·cos(2π(h − 4)/24) + 0,08·exp(−((h − 14,5)/1,2)²), 0, 1)``: máxima
    hacia las 04:00, mínima hacia las 16:00, con un pequeño bache de siesta a las 14:30.
    """
    try:
        h = float(h) % 24.0
    except (TypeError, ValueError):
        h = 12.0
    if not math.isfinite(h):
        h = 12.0
    valor = 0.5 + 0.4 * math.cos(2.0 * math.pi * (h - 4.0) / 24.0) + 0.08 * math.exp(-((h - 14.5) / 1.2) ** 2)
    return _clip(valor, 0.0, 1.0)


def _clip(x: float, lo: float, hi: float) -> float:
    """Acota ``x`` a ``[lo, hi]`` (NaN ⇒ ``lo``)."""
    if x != x:  # NaN
        return lo
    return lo if x < lo else hi if x > hi else x


def _sigmoide(x: float) -> float:
    """Sigmoide logística estable numéricamente."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-min(x, 60.0)))
    e = math.exp(max(x, -60.0))
    return e / (1.0 + e)


def _ema(previo: float, x: float, dt: float, tau: float) -> float:
    """``ema(x̄, x, Δ, τ) = x̄ + (1 − e^{−Δ/τ})(x − x̄)`` (Anexo A.5)."""
    if tau <= 0.0:
        return x
    return previo + (1.0 - math.exp(-dt / tau)) * (x - previo)


def _envolver(angulo: float) -> float:
    """Ángulo a ``[−π, π)``."""
    return (angulo + math.pi) % (2.0 * math.pi) - math.pi


def _real(valor: Any, defecto: float) -> float:
    """Float finito o ``defecto``."""
    try:
        x = float(valor)
    except (TypeError, ValueError, OverflowError):
        return defecto
    return x if math.isfinite(x) else defecto


def _texto(valor: Any) -> str:
    """Texto seguro para codificar (``None`` ⇒ cadena vacía; recorta a 4 000 caracteres)."""
    if valor is None:
        return ""
    if not isinstance(valor, str):
        valor = str(valor)
    return valor[:4000]


def _trigramas(texto: str) -> frozenset[str]:
    """Conjunto de trigramas del texto normalizado (para la similitud de Jaccard del modo simple)."""
    t = " " + " ".join(texto.lower().split()) + " "
    return frozenset(t[i:i + 3] for i in range(max(1, len(t) - 2)))


_ZONA_DEFECTO: tzinfo | None = None


def _zona_defecto() -> tzinfo:
    """Zona America/Bogota sin ``Settings`` (con respaldo UTC−5 si falta la base de zonas)."""
    global _ZONA_DEFECTO
    if _ZONA_DEFECTO is None:
        try:
            from zoneinfo import ZoneInfo
            _ZONA_DEFECTO = ZoneInfo("America/Bogota")
        except Exception as exc:  # ZoneInfoNotFoundError, ImportError…
            log.debug("Zona America/Bogota no disponible (%s); uso UTC−5", type(exc).__name__)
            _ZONA_DEFECTO = timezone(timedelta(hours=-5), "COT")
    return _ZONA_DEFECTO


def _escribir_atomico(ruta: Path, contenido: bytes) -> None:
    """Escribe ``contenido`` en ``ruta`` de forma atómica (``.tmp`` + ``os.replace``) con permisos 0o600."""
    ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(contenido)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError as exc:  # Windows o sistemas de archivos sin permisos POSIX
        log.debug("No se pudo aplicar chmod 600 a %s: %s", tmp.name, exc)
    os.replace(tmp, ruta)
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:
        log.debug("No se pudo aplicar chmod 600 a %s: %s", ruta.name, exc)


def _nombre_campo(campo: str | None) -> str:
    """Nombre legible de un campo de atracción para los motivos en español."""
    return {"alfa": "α", "beta": "β", "steins_gate": "Steins Gate", "anomalo": "anómalo"}.get(str(campo), str(campo))


# ---------------------------------------------------------------------------
# Medidor de divergencia (Python puro)
# ---------------------------------------------------------------------------


class MedidorDivergencia:
    """Medidor de divergencia estilo Steins;Gate con campo confirmado por histéresis temporal (§10.6).

    Convierte el vector homeostático ``x = (A, Fo, B, F)`` en el valor nixie de 6 decimales y en
    un campo de atracción. En equilibrio marca exactamente ``1.048596`` (Steins Gate); la
    sobrecarga o la fatiga llevan al campo β (> 1) y la subestimulación o el aburrimiento al α
    (< 1). Los valores cercanos a líneas canónicas de la serie convergen a ellas. El campo
    **confirmado** (el que se publica) exige 5 s estables en el nuevo campo y 60 s desde el último
    cambio confirmado; la entrada en ``anomalo`` es inmediata. Python puro, sin numpy.
    """

    PESOS: Final = (1.0, 1.0, 1.2, 1.2)
    M_MAX: Final = 1.5887
    UMBRAL_BLOQUEO: Final = 0.07
    UMBRAL_DESBLOQUEO: Final = 0.10
    T_BLOQUEO_S: Final = 3.0
    BANDA_SG: Final = 0.003
    HISTERESIS_SIGNO: Final = 0.05
    T_CONFIRMACION_S: Final = 5.0
    T_REFRACTARIO_S: Final = 60.0
    _EPS: Final = 1e-6

    def __init__(self, x_eq: tuple[float, float, float, float] = X_EQ_DEFECTO) -> None:
        self._x_eq: tuple[float, float, float, float] = tuple(X_EQ_DEFECTO)  # type: ignore[assignment]
        self.set_equilibrio(x_eq)
        self._signo = 1
        self._t_bajo = 0.0
        self._bloqueado = False
        self.campo_crudo: str = "steins_gate"
        self._confirmado = "steins_gate"
        self._crudo_previo = "steins_gate"
        self._t_en_crudo = 0.0
        self._t_sim = 0.0
        self._t_ult_cambio = -math.inf
        self.m: float = 0.0
        self.divergencia: float = _SG

    @property
    def x_eq(self) -> tuple[float, float, float, float]:
        """Equilibrio homeostático vigente (A, Fo, B, F)."""
        return self._x_eq

    @property
    def campo(self) -> str:
        """Campo confirmado actual."""
        return self._confirmado

    @property
    def bloqueado(self) -> bool:
        """``True`` si el bloqueo Steins Gate está activo (m < 0,07 durante ≥ 3 s acumulados)."""
        return self._bloqueado

    @property
    def t_sim(self) -> float:
        """Tiempo simulado acumulado (s), suma de los ``dt_s`` recibidos."""
        return self._t_sim

    def set_equilibrio(self, x_eq: tuple[float, float, float, float]) -> None:
        """Fija el equilibrio calibrado ``X_EQ``. Valores inválidos se ignoran con ``log.debug``."""
        try:
            valores = tuple(float(v) for v in x_eq)
        except (TypeError, ValueError):
            log.debug("Equilibrio de divergencia inválido; se conserva el anterior")
            return
        if len(valores) != 4 or not all(math.isfinite(v) for v in valores):
            log.debug("Equilibrio de divergencia inválido; se conserva el anterior")
            return
        self._x_eq = tuple(_clip(v, 0.0, 1.0) for v in valores)  # type: ignore[assignment]

    def actualizar(self, arousal: float, focus: float, boredom: float, fatiga: float, dt_s: float,
                   *, anomalo: bool = False, semilla_jitter: int = 0) -> tuple[float, str]:
        """Avanza ``dt_s`` segundos simulados y devuelve ``(D instantánea, campo CONFIRMADO)``."""
        dt = _real(dt_s, 0.0)
        dt = max(0.0, dt)
        self._t_sim += dt
        crudo_x = (arousal, focus, boredom, fatiga)
        x: list[float] = []
        for valor, eq in zip(crudo_x, self._x_eq):
            v = _real(valor, math.nan)
            if not math.isfinite(v):
                anomalo = True
                v = eq
            x.append(v)
        a, fo, b, f = x
        ea, efo, eb, ef = self._x_eq
        dif = (a - ea, fo - efo, b - eb, f - ef)
        norma = math.sqrt(sum((w * d) ** 2 for w, d in zip(self.PESOS, dif)))
        m = min(1.0, norma / self.M_MAX)
        self.m = m
        s = 1.0 * (a - ea) + 1.2 * (f - ef) - 1.2 * (b - eb) - 0.4 * (fo - efo)
        if s > self.HISTERESIS_SIGNO:
            self._signo = 1
        elif s < -self.HISTERESIS_SIGNO:
            self._signo = -1

        # Bloqueo SG: m < 0,07 durante ≥ 3 s acumulados; se desbloquea con m > 0,10.
        if m < self.UMBRAL_BLOQUEO:
            self._t_bajo += dt
            if self._t_bajo >= self.T_BLOQUEO_S - self._EPS:
                self._bloqueado = True
        elif m > self.UMBRAL_DESBLOQUEO:
            self._t_bajo = 0.0
            self._bloqueado = False

        # 1) Valor instantáneo y campo crudo.
        if anomalo:
            d = round(2.0 + min(1.999999, 2.0 * m), 6)
            crudo = "anomalo"
        elif self._bloqueado:
            d = _SG
            crudo = "steins_gate"
        else:
            d = _SG + self._signo * 0.9 * math.tanh(2.2 * max(0.0, m - self.UMBRAL_BLOQUEO))
            if abs(d - _SG) < self.BANDA_SG:
                d = _SG  # banda muerta SG (sin jitter)
                crudo = "steins_gate"
            else:
                # Convergencia: la primera línea canónica (en el orden de LINEAS_CANONICAS) a menos de
                # 0,003. Así todo valor a menos de 0,003 de 0.571024 converge a 0.571024 aunque 0.571046
                # esté más cerca. Interpretación: un valor convergido queda exacto (como la banda SG),
                # de modo que el jitter solo "tiembla" fuera de las líneas canónicas.
                convergida = next((linea for linea in LINEAS_CANONICAS if abs(d - linea) < self.BANDA_SG), None)
                if convergida is not None:
                    d = convergida
                else:
                    jitter = ((int(semilla_jitter) % 2001) - 1000) * 1e-6 * min(1.0, m / 0.2)
                    if (d + jitter - _SG) * self._signo > self.BANDA_SG:
                        d += jitter  # el jitter nunca entra en la banda SG ni la cruza
                d = round(d, 6)
                crudo = "beta" if self._signo > 0 else "alfa"
        self.campo_crudo = crudo
        self.divergencia = d

        # 2) Histéresis temporal (campo CONFIRMADO).
        if crudo == "anomalo":
            if self._confirmado != "anomalo":
                self._confirmado = "anomalo"
                self._t_ult_cambio = self._t_sim
            self._t_en_crudo = 0.0
        elif crudo != self._confirmado:
            if crudo != self._crudo_previo:
                self._t_en_crudo = 0.0
            self._t_en_crudo += dt
            if (self._t_en_crudo >= self.T_CONFIRMACION_S - self._EPS
                    and (self._t_sim - self._t_ult_cambio) >= self.T_REFRACTARIO_S - self._EPS):
                self._confirmado = crudo
                self._t_ult_cambio = self._t_sim
                self._t_en_crudo = 0.0
        else:
            self._t_en_crudo = 0.0
        self._crudo_previo = crudo
        return d, self._confirmado


# ---------------------------------------------------------------------------
# Motor de iniciativas (Python puro)
# ---------------------------------------------------------------------------


class MotorIniciativa:
    """Decide las iniciativas por flancos, con rearme y refractario (§10.6). Python puro.

    | Iniciativa | Dispara si | Rearme | Refractario |
    |---|---|---|---|
    | ``curiosidad_visual`` | curiosidad > 0,65, fatiga < 0,85 y vigilia | curiosidad < 0,45 | 90 s |
    | ``descanso`` | fatiga > 0,80 | fatiga < 0,65 | 60 min |
    | ``conversar`` | boredom > 0,75, arousal > 0,40 y fatiga < 0,80 | boredom < 0,50 | 20 min |
    | ``cambio_linea`` | el campo confirmado cambia entre α, SG y β | — | 10 min |

    Los refractarios se dividen por ``escala_tiempo``. Si varias disparan en el mismo chunk gana
    la de mayor prioridad (en ese orden) y las demás quedan armadas para el siguiente.
    """

    PRIORIDAD: Final = ("curiosidad_visual", "descanso", "conversar", "cambio_linea")
    REFRACTARIO_S: Final = {"curiosidad_visual": 90.0, "descanso": 3600.0, "conversar": 1200.0,
                            "cambio_linea": 600.0}
    _CAMPOS_LINEA: Final = frozenset({"alfa", "steins_gate", "beta"})

    def __init__(self, escala_tiempo: float = 1.0, reloj: Callable[[], float] = time.monotonic) -> None:
        escala = _real(escala_tiempo, 1.0)
        self._escala = escala if escala > 0 else 1.0
        self._reloj = reloj
        self._armada = {nombre: True for nombre in self.PRIORIDAD}
        self._ultimo = {nombre: -math.inf for nombre in self.PRIORIDAD}
        self._cambio_pendiente: tuple[str, str] | None = None
        self._ultimo_estimulo = self._ahora()

    def _ahora(self) -> float:
        try:
            return float(self._reloj())
        except Exception as exc:  # un reloj inyectado roto no debe tumbar el conectoma
            log.debug("Reloj del motor de iniciativas falló (%s); uso time.monotonic", exc)
            return time.monotonic()

    def registrar_estimulo(self) -> None:
        """Marca la llegada de un estímulo nuevo (para el motivo de ``conversar``)."""
        self._ultimo_estimulo = self._ahora()

    def _libre(self, nombre: str, ahora: float) -> bool:
        return (ahora - self._ultimo[nombre]) >= self.REFRACTARIO_S[nombre] / self._escala

    def evaluar(self, *, curiosidad: float, fatiga: float, boredom: float, arousal: float, modo: str,
                campo_antes: str, campo_despues: str) -> tuple[str | None, str, str | None, str | None]:
        """Evalúa los flancos de este chunk.

        Devuelve ``(iniciativa, motivo, antes, despues)``; ``antes``/``despues`` solo con
        ``"cambio_linea"`` (si no, ``None, None``). ``campo_antes``/``campo_despues`` son campos
        CONFIRMADOS.
        """
        ahora = self._ahora()
        c = _real(curiosidad, 0.0)
        f = _real(fatiga, 0.0)
        b = _real(boredom, 0.0)
        a = _real(arousal, 0.0)

        # Rearme por histéresis.
        if c < 0.45:
            self._armada["curiosidad_visual"] = True
        if f < 0.65:
            self._armada["descanso"] = True
        if b < 0.50:
            self._armada["conversar"] = True

        # Cambio de línea: se recuerda hasta que pueda emitirse (o se descarta en refractario).
        if (campo_antes != campo_despues and campo_antes in self._CAMPOS_LINEA
                and campo_despues in self._CAMPOS_LINEA):
            if self._libre("cambio_linea", ahora):
                if self._cambio_pendiente is not None:
                    antes = self._cambio_pendiente[0]
                    self._cambio_pendiente = None if antes == campo_despues else (antes, campo_despues)
                else:
                    self._cambio_pendiente = (campo_antes, campo_despues)
            else:
                log.debug("Cambio de línea %s → %s dentro del refractario; no genera iniciativa",
                          campo_antes, campo_despues)

        candidatas: dict[str, str] = {}
        if (self._armada["curiosidad_visual"] and c > 0.65 and f < 0.85 and modo == "vigilia"
                and self._libre("curiosidad_visual", ahora)):
            candidatas["curiosidad_visual"] = (
                f"curiosidad {formatear_decimal(c, 2)} (aburrimiento {formatear_decimal(b, 2)}, "
                f"arousal {formatear_decimal(a, 2)}): quiero echar un vistazo")
        if self._armada["descanso"] and f > 0.80 and self._libre("descanso", ahora):
            candidatas["descanso"] = (
                f"fatiga {formatear_decimal(f, 2)}: la presión de sueño es alta y conviene un descanso")
        if (self._armada["conversar"] and b > 0.75 and a > 0.40 and f < 0.80
                and self._libre("conversar", ahora)):
            minutos = max(0, int((ahora - self._ultimo_estimulo) // 60))
            candidatas["conversar"] = (
                f"aburrimiento {formatear_decimal(b, 2)} sin estímulos nuevos en {minutos} min")
        if self._cambio_pendiente is not None and self._libre("cambio_linea", ahora):
            antes, despues = self._cambio_pendiente
            candidatas["cambio_linea"] = (
                f"la línea de mundo cambió del campo {_nombre_campo(antes)} al {_nombre_campo(despues)}")

        for nombre in self.PRIORIDAD:
            if nombre in candidatas:
                self._ultimo[nombre] = ahora
                if nombre == "cambio_linea":
                    antes, despues = self._cambio_pendiente  # type: ignore[misc]
                    self._cambio_pendiente = None
                    return nombre, candidatas[nombre], antes, despues
                self._armada[nombre] = False
                return nombre, candidatas[nombre], None, None
        return None, "", None, None


# ---------------------------------------------------------------------------
# Proceso S (Borbély)
# ---------------------------------------------------------------------------


class _ProcesoSueno:
    """Presión de sueño P (proceso S) integrada con tiempo de reloj × ``escala_tiempo``."""

    def __init__(self, p: float = 0.30) -> None:
        self.p = _clip(_real(p, 0.30), 0.0, 1.0)

    def avanzar(self, dt_s: float, *, reposo: bool, z_glob: float = 0.0, k_act: float = 1.0) -> None:
        """Integra ``dt_s`` segundos (ya escalados) de forma exacta para la parte lineal."""
        if dt_s <= 0.0:
            return
        if reposo:
            self.p *= math.exp(-dt_s / _TAU_S_SUENO_S)
        else:
            tasa = (1.0 + 0.5 * max(0.0, z_glob)) * max(0.0, k_act) / _TAU_W_S
            self.p = 1.0 - (1.0 - self.p) * math.exp(-dt_s * tasa)
        self.p = _clip(self.p, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Red reducida (numpy)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Estimulo:
    """Estímulo programado: ``destino`` ∈ {"ext", "pn", "dan"} con decaimiento τ = duración/2 al terminar."""

    destino: str
    indices: Any                 # np.ndarray[int]
    magnitud: Any                # float o np.ndarray (mV para "ext"; Hz para "pn"/"dan")
    inicio_ms: float
    duracion_ms: float

    def factor(self, t_ms: float) -> float | None:
        """Factor de intensidad en ``t_ms`` (0 antes del inicio); ``None`` si ya se extinguió."""
        if t_ms < self.inicio_ms:
            return 0.0
        fin = self.inicio_ms + self.duracion_ms
        if t_ms < fin:
            return 1.0
        tau = max(1.0, self.duracion_ms / 2.0)
        transcurrido = t_ms - fin
        if transcurrido > 8.0 * tau:
            return None
        return math.exp(-transcurrido / tau)


class _ResultadoChunk:
    """Resumen de un chunk simulado (lo consume ``_LectorHomeostatico``)."""

    __slots__ = ("ms", "cnt", "cols_epg", "a_media", "kc_frac", "crc", "finito")

    def __init__(self, ms: float, cnt: Any, cols_epg: list[float], a_media: float, kc_frac: float,
                 crc: int, finito: bool) -> None:
        self.ms = ms
        self.cnt = cnt
        self.cols_epg = cols_epg
        self.a_media = a_media
        self.kc_frac = kc_frac
        self.crc = crc
        self.finito = finito


class _RedReducida:
    """Red LIF reducida CX+MB (Anexo A.3) con núcleo numpy (A.1–A.2). Solo se usa en el executor.

    Unidades: u, g, a y corrientes en mV relativos al reposo; tiempos en ms; tasas en Hz.
    """

    def __init__(self, *, semilla_red: int, rng: Any, modo: str) -> None:
        if np is None:  # pragma: no cover - el llamador lo comprueba
            raise RuntimeError("numpy no disponible")
        self.modo = modo
        self.semilla_red = int(semilla_red)
        self.rng = rng
        self.dt = 2.0 if modo == "dt2" else 1.0
        factor_kc = 1.0 if modo == "normal" else 0.5
        tam = dict(_TAM_FIJO)
        for nombre, n in _TAM_KC_NORMAL.items():
            tam[nombre] = int(n * factor_kc)
        self.tam = tam
        self.sl: dict[str, slice] = {}
        inicio = 0
        for nombre in _ORDEN_POBLACIONES:
            self.sl[nombre] = slice(inicio, inicio + tam[nombre])
            inicio += tam[nombre]
        self.N = inicio
        self.n_kc = sum(tam[k] for k in _KC)
        self.tam_vec = np.array([tam[p] for p in _ORDEN_POBLACIONES], dtype=np.int64)
        self.pop_id = np.empty(self.N, dtype=np.int16)
        for i, nombre in enumerate(_ORDEN_POBLACIONES):
            self.pop_id[self.sl[nombre]] = i
        self.kc_lo = self.sl["KC_g"].start
        self.kc_hi = self.sl["KC_apbp"].stop
        self.kc_lims = np.array([self.kc_lo, self.sl["KC_g"].stop, self.sl["KC_ab"].stop, self.kc_hi],
                                dtype=np.int64)
        self.dan_lo = self.sl["PAM"].start
        self.dan_hi = self.sl["PPL1"].stop
        ppl1 = self.sl["PPL1"].start
        # Subpoblaciones PPL1 (A.3): α'3 (2), γ1pedc (2), →dFB (2), otros (6).
        self.ppl1_a3 = np.arange(ppl1, ppl1 + 2)
        self.ppl1_g1 = np.arange(ppl1 + 2, ppl1 + 4)
        self.ppl1_dfb = np.arange(ppl1 + 4, ppl1 + 6)
        self.ppl1_otros = np.arange(ppl1 + 6, ppl1 + 12)
        # Índices locales del bloque DAN (PAM 0..59, PPL1 60..71) para estímulos "dan".
        self.dan_pam = np.arange(0, tam["PAM"])
        self.dan_ppl1 = np.arange(tam["PAM"], tam["PAM"] + tam["PPL1"])
        # Compartimento de cada DAN local: 0 = α'3 (PPL1-α'3), 1 = avd (PAM), 2 = app (PPL1 γ1pedc + otros).
        comp = np.full(tam["PAM"] + tam["PPL1"], -1, dtype=np.int64)
        comp[self.dan_pam] = 1
        comp[self.ppl1_a3 - self.dan_lo] = 0
        comp[self.ppl1_g1 - self.dan_lo] = 2
        comp[self.ppl1_otros - self.dan_lo] = 2
        self.dan_comp = comp
        # Normalización de las trazas dopaminérgicas a "compartimento de 2 DAN".
        self.dan_norm = np.array([2.0 / 2.0, 2.0 / tam["PAM"], 2.0 / 8.0])

        self.anillo = np.zeros(self.N, dtype=bool)
        for nombre in _ANILLO:
            self.anillo[self.sl[nombre]] = True
        self.no_anillo = np.flatnonzero(~self.anillo)

        rng_red = np.random.default_rng(self.semilla_red)
        self._construir_conectividad(rng_red)
        self._construir_estado()

    # -- construcción -----------------------------------------------------------

    def _indices(self, nombre: str) -> Any:
        s = self.sl[nombre]
        return np.arange(s.start, s.stop, dtype=np.int64)

    def _construir_conectividad(self, rng_red: Any) -> None:
        """Aristas del Anexo A.3 → CSR por presináptica (LIF + 72 entradas) y matrices plásticas."""
        pres: list[Any] = []
        posts: list[Any] = []
        pesos: list[Any] = []

        def agregar(pre: Any, post: Any, s: Any, signo: float) -> None:
            pre = np.asarray(pre, dtype=np.int64).ravel()
            post = np.asarray(post, dtype=np.int64).ravel()
            s_arr = np.broadcast_to(np.asarray(s, dtype=np.float64), pre.shape).ravel()
            m = (s_arr > 1e-9) & (pre != post)
            pres.append(pre[m])
            posts.append(post[m])
            pesos.append((signo * _W_SYN_MV * s_arr[m]).astype(np.float32))

        def todos(pre: Any, post: Any) -> tuple[Any, Any]:
            a, b = np.meshgrid(pre, post, indexing="ij")
            return a.ravel(), b.ravel()

        def probabilistico(pre: Any, post: Any, p: float, s: float, signo: float) -> None:
            a, b = todos(pre, post)
            m = rng_red.random(a.shape[0]) < p
            agregar(a[m], b[m], s, signo)

        theta = 2.0 * np.pi * np.arange(_N_COL) / _N_COL
        epg = self._indices("EPG")
        col_epg = (epg - epg[0]) // 3
        pen1 = self._indices("PEN1")
        pen2 = self._indices("PEN2")
        pen1_r, pen1_l = pen1[:_N_COL], pen1[_N_COL:]
        pen2_r, pen2_l = pen2[:_N_COL], pen2[_N_COL:]
        peg = self._indices("PEG")
        d7 = self._indices("D7")
        col_d7 = (d7 - d7[0]) // 2
        er = self._indices("ER_vis")
        phi_er = 2.0 * np.pi * (er - er[0]) / len(er)
        self.phi_er = phi_er.astype(np.float64)
        self.idx_er = er
        self.idx_epg = epg
        self.idx_pen_r = np.concatenate((pen1_r, pen2_r))
        self.idx_pen_l = np.concatenate((pen1_l, pen2_l))

        # --- Anillo CX [P] ---
        a, b = todos(epg, epg)
        agregar(a, b, np.where(col_epg[a - epg[0]] == col_epg[b - epg[0]], 6.0, 0.0), +1.0)
        for destino in (pen1_r, pen1_l, pen2_r, pen2_l, peg):
            agregar(epg, destino[col_epg], 30.0, +1.0)
        for destino in (pen2_r, pen2_l):
            agregar(peg, destino, 30.0, +1.0)
        cols = np.arange(_N_COL)
        for fuente_r in (pen1_r, pen2_r):
            pre = np.repeat(fuente_r, 3)
            post = epg[0] + 3 * np.repeat((cols + 1) % _N_COL, 3) + np.tile(np.arange(3), _N_COL)
            agregar(pre, post, 75.0, +1.0)
        for fuente_l in (pen1_l, pen2_l):
            pre = np.repeat(fuente_l, 3)
            post = epg[0] + 3 * np.repeat((cols - 1) % _N_COL, 3) + np.tile(np.arange(3), _N_COL)
            agregar(pre, post, 75.0, +1.0)
        a, b = todos(epg, d7)
        agregar(a, b, 6.0 * (1.0 + np.cos(theta[col_epg[a - epg[0]]] - theta[col_d7[b - d7[0]]])) / 2.0, +1.0)
        destinos_d7 = [(epg, col_epg), (pen1_r, cols), (pen1_l, cols), (pen2_r, cols), (pen2_l, cols), (peg, cols)]
        for destino, col_destino in destinos_d7:
            a, b = todos(np.arange(len(d7)), np.arange(len(destino)))
            s = 15.0 * (1.0 - np.cos(theta[col_destino[b]] - theta[col_d7[a]])) / 2.0
            agregar(d7[a], destino[b], s, -1.0)
        a, b = todos(np.arange(len(er)), np.arange(len(epg)))
        s = 0.67 + 12.0 * (1.0 - np.cos(phi_er[a] - theta[col_epg[b]])) / 2.0
        agregar(er[a], epg[b], s, -1.0)
        a, b = todos(er, er)
        agregar(a, b, 1.3, -1.0)

        # --- Sueño [D] ---
        exr1 = self._indices("ExR1")
        er5 = self._indices("ER5")
        dfb = self._indices("dFB")
        probabilistico(exr1, er5, 1.0, 10.0, +1.0)
        probabilistico(er5, dfb, 0.5, 8.0, +1.0)
        probabilistico(er5, epg, 0.3, 1.0, -1.0)
        probabilistico(dfb, exr1, 1.0, 12.0, -1.0)
        probabilistico(dfb, er, 0.5, 3.0, -1.0)
        probabilistico(self.ppl1_dfb, dfb, 1.0, 15.0, -1.0)

        # --- Cuerpo Fungiforme ---
        kc_g = self._indices("KC_g")
        kc_ab = self._indices("KC_ab")
        kc_apbp = self._indices("KC_apbp")
        kc = np.concatenate((kc_g, kc_ab, kc_apbp))
        # PN de texto → KC: 6 garras distintas por KC (de las 64).
        garras = np.argsort(rng_red.random((len(kc), _N_PN_TEXTO)), axis=1)[:, :6]
        self.garras = garras.astype(np.int16)
        agregar(self.N + garras.ravel(), np.repeat(kc, 6), 32.0, +1.0)
        # PN interoceptivas → 1 garra en el 10 % de los KC_ab.
        sel = rng_red.random(len(kc_ab)) < 0.10
        intero = rng_red.integers(0, _N_PN_INTERO, size=len(kc_ab))
        agregar(self.N + _N_PN_TEXTO + intero[sel], kc_ab[sel], 16.0, +1.0)
        mbon_otr = self._indices("MBON_otr")
        probabilistico(np.concatenate((kc_ab, kc_apbp)), mbon_otr, 0.3, 15.0, +1.0)
        pam = self._indices("PAM")
        probabilistico(kc_g, np.concatenate((pam, self.ppl1_g1)), 0.3, 4.0, +1.0)
        probabilistico(kc_ab, np.concatenate((pam, self.ppl1_otros)), 0.3, 4.0, +1.0)
        probabilistico(kc_apbp, self.ppl1_a3, 0.3, 4.0, +1.0)
        mbon_avd = self._indices("MBON_avd")
        mbon_app = self._indices("MBON_app")
        probabilistico(pam, mbon_avd, 0.5, 2.0, +1.0)
        probabilistico(np.concatenate((self.ppl1_g1, self.ppl1_otros)), mbon_app, 0.5, 2.0, +1.0)
        probabilistico(mbon_app, pam, 0.3, 6.0, +1.0)

        # Plasticidad KC→MBON (matrices densas por compartimento, fuera del CSR).
        n_g, n_ab, n_apbp = len(kc_g), len(kc_ab), len(kc_apbp)
        self.W0_a3 = np.full((n_apbp, self.tam["MBON_a3"]), 40.0 * _W_SYN_MV, dtype=np.float32)
        mascara_avd = rng_red.random((n_g, self.tam["MBON_avd"])) < 0.5
        self.W0_avd = np.where(mascara_avd, 20.0 * _W_SYN_MV, 0.0).astype(np.float32)
        mascara_app = rng_red.random((n_g + n_ab, self.tam["MBON_app"])) < 0.5
        self.W0_app = np.where(mascara_app, 20.0 * _W_SYN_MV, 0.0).astype(np.float32)
        self.W_a3 = self.W0_a3.copy()
        self.W_avd = self.W0_avd.copy()
        self.W_app = self.W0_app.copy()
        self.sl_a3 = self.sl["MBON_a3"]
        self.sl_avd = self.sl["MBON_avd"]
        self.sl_app = self.sl["MBON_app"]
        self.apl_w = np.float32(8.0 * _W_SYN_MV)

        pre = np.concatenate(pres)
        post = np.concatenate(posts)
        w = np.concatenate(pesos)
        orden = np.argsort(pre, kind="stable")
        pre, post, w = pre[orden], post[orden], w[orden]
        n_pre = self.N + _N_ENTRADAS
        self.indptr = np.zeros(n_pre + 1, dtype=np.int64)
        self.indptr[1:] = np.cumsum(np.bincount(pre, minlength=n_pre))
        self.post = post.astype(np.int64)
        self.w = w.astype(np.float32)
        self.n_aristas = int(len(self.w))

    def _construir_estado(self) -> None:
        """Variables de estado, sesgos y constantes de integración (A.1)."""
        dt = self.dt
        self.dm = float(math.exp(-dt / _TAU_M_MS))
        self.ds = float(math.exp(-dt / _TAU_S_MS))
        self.da = float(math.exp(-dt / _TAU_A_MS))
        self.c1 = float(1.0 - self.dm)
        self.sq = float(math.sqrt(1.0 - self.dm ** 2))
        self.ref_pasos = max(1, int(round(_T_REF_MS / dt)))
        self.D = max(1, int(round(_T_RET_MS / dt)))
        self.bloque = max(1, int(round(_BLOQUE_PASOS_MS / dt)))
        N = self.N
        self.bias_base = np.zeros(N, dtype=np.float32)
        self.sigma = np.full(N, _SIGMA_MV, dtype=np.float32)
        self.delta_a = np.full(N, _DELTA_A_MV, dtype=np.float32)
        for nombre in _ORDEN_POBLACIONES:
            self.bias_base[self.sl[nombre]] = _BIAS_MV[nombre] + _COMPENSACION_SFA_MV.get(nombre, 0.0)
            if nombre in _KC:
                self.sigma[self.sl[nombre]] = _SIGMA_KC_MV
            if nombre in _SIN_SFA:
                self.delta_a[self.sl[nombre]] = 0.0
        self.cal = np.zeros(N, dtype=np.float32)
        self.dyn = np.zeros(N, dtype=np.float32)
        self.bias_total = self.bias_base.copy()
        self.ext_tonico = np.zeros(N, dtype=np.float32)
        self.sig_extra = np.zeros(N, dtype=np.float32)
        self.sig_sq = (self.sigma * self.sq).astype(np.float32)
        self.drive = self.bias_total.copy()
        self._ext = np.zeros(N, dtype=np.float32)
        self._tmp = np.zeros(N, dtype=np.float32)
        self._mascara = np.zeros(N, dtype=bool)
        self.pn_base = np.full(_N_ENTRADAS, _PN_FONDO_HZ, dtype=np.float32)
        self.dan_tonico = np.zeros(self.dan_hi - self.dan_lo, dtype=np.float32)
        self.pn_prob = self.pn_base * np.float32(self.dt / 1000.0)
        self.dan_prob = np.zeros_like(self.dan_tonico)
        self.dan_activo = False
        self.estimulos: list[_Estimulo] = []
        self._sueno_cache: tuple[float, float, bool] | None = None
        self.reiniciar_dinamica()
        self.e_kc = np.zeros(self.n_kc, dtype=np.float32)
        self.d_traza = np.zeros(3, dtype=np.float64)

    def reiniciar_dinamica(self) -> None:
        """Reinicia u, g, a, el búfer de retardo y la ventana de actividad KC (arranque o NaN/inf)."""
        N = self.N
        self.u = self.rng.uniform(0.0, 3.0, N).astype(np.float32)
        self.g = np.zeros(N, dtype=np.float32)
        self.a = np.zeros(N, dtype=np.float32)
        self.buf = np.zeros((self.D + 1, N), dtype=np.float32)
        self.ref_hist: collections.deque[Any] = collections.deque(maxlen=self.ref_pasos)
        self.kc_ultimo = np.full(self.n_kc, -1e12, dtype=np.float64)
        if not hasattr(self, "t"):
            self.t = 0
            self.t_ms = 0.0

    # -- parámetros en caliente ---------------------------------------------------

    def _recalcular_bias(self) -> None:
        np.add(self.bias_base, self.cal, out=self.bias_total)
        self.bias_total += self.dyn

    def set_calibracion(self, offsets: Mapping[str, float]) -> None:
        """Aplica los ajustes de sesgo de la calibración por población (claves de ``_CLAVES_TASA``)."""
        self.cal.fill(0.0)
        for clave, valor in offsets.items():
            v = _real(valor, 0.0)
            if clave == "MBON":
                for nombre in ("MBON_app", "MBON_avd", "MBON_otr"):
                    self.cal[self.sl[nombre]] = v
            elif clave in self.sl:
                self.cal[self.sl[clave]] = v
        self._recalcular_bias()

    def set_sueno(self, p: float, c: float, reposo: bool) -> None:
        """Sesgos dinámicos: ER5 = 2 + 4·P; dFB = 1,5 + 2·(c − 0,5) (+3 mV en reposo); DPM ∝ P sobre KC α'/β'."""
        clave = (round(p, 4), round(c, 4), bool(reposo))
        if clave == self._sueno_cache:
            return
        self._sueno_cache = clave
        self.dyn[self.sl["ER5"]] = 2.0 + 4.0 * p
        self.dyn[self.sl["dFB"]] = 1.5 + 2.0 * (c - 0.5) + (3.0 if reposo else 0.0)
        self.dyn[self.sl["KC_apbp"]] = -1.0 * p      # DPM graduada (inhibición ∝ presión de sueño)
        self._recalcular_bias()

    def set_landmark_tonico(self, columna: int | None, mv: float) -> None:
        """Corriente tónica en ER_vis con perfil cos² centrado en ``columna`` (Anexo A.4)."""
        self.ext_tonico[self.sl["ER_vis"]] = 0.0
        if columna is None or mv <= 0.0:
            return
        psi = 2.0 * math.pi * (int(columna) % _N_COL) / _N_COL
        perfil = np.maximum(0.0, np.cos(self.phi_er - psi)) ** 2
        self.ext_tonico[self.sl["ER_vis"]] = (mv * perfil).astype(np.float32)

    def set_intero(self, hz: float) -> None:
        """Tasa de las 8 PN interoceptivas (2–40 Hz ∝ carga del sistema)."""
        self.pn_base[_N_PN_TEXTO:] = _clip(_real(hz, _PN_FONDO_HZ), 0.0, 100.0)

    def set_ppl1_tonico(self, hz: float) -> None:
        """Tasa tónica añadida a PPL1 (Hz), p. ej. +8 Hz con el sistema sobrecargado."""
        self.dan_tonico[self.dan_ppl1] = max(0.0, _real(hz, 0.0))

    def set_ruido_er(self, delta_mv: float) -> None:
        """Ajuste de σ del ruido de ER_vis (mV): +0,5 con carga alta, −0,3 con Nexus enfocado."""
        self.sig_extra.fill(0.0)
        self.sig_extra[self.sl["ER_vis"]] = _real(delta_mv, 0.0)
        sigma = np.maximum(0.05, self.sigma + self.sig_extra)
        self.sig_sq = (sigma * self.sq).astype(np.float32)

    def programar(self, destino: str, indices: Any, magnitud: Any, duracion_ms: float) -> None:
        """Programa un estímulo que empieza ahora (tiempo simulado de la red)."""
        idx = np.asarray(indices, dtype=np.int64)
        if idx.size == 0:
            return
        if isinstance(magnitud, (int, float)):
            mag: Any = float(magnitud)
        else:
            mag = np.asarray(magnitud, dtype=np.float32)
        self.estimulos.append(_Estimulo(destino, idx, mag, self.t_ms, max(1.0, float(duracion_ms))))
        if len(self.estimulos) > 64:  # tope defensivo ante ráfagas de eventos
            self.estimulos = self.estimulos[-64:]

    def _componer(self) -> None:
        """Compone corriente externa, tasas PN y tasas DAN del bloque actual (tónico + transitorio)."""
        t = self.t_ms
        np.copyto(self._ext, self.ext_tonico)
        pn = self.pn_base.copy()
        dan = self.dan_tonico.copy()
        vivos: list[_Estimulo] = []
        for est in self.estimulos:
            f = est.factor(t)
            if f is None:
                continue
            vivos.append(est)
            if f <= 0.0:
                continue
            if est.destino == "ext":
                self._ext[est.indices] += est.magnitud * f
            elif est.destino == "pn":
                pn[est.indices] += est.magnitud * f
            else:
                dan[est.indices] += est.magnitud * f
        self.estimulos = vivos
        np.add(self.bias_total, self._ext, out=self.drive)
        escala = np.float32(self.dt / 1000.0)
        self.pn_prob = np.clip(pn, 0.0, 200.0) * escala
        self.dan_prob = np.clip(dan, 0.0, 200.0) * escala
        self.dan_activo = bool(dan.any())

    # -- simulación -------------------------------------------------------------

    def _propagar(self, pre: Any, destino: Any) -> None:
        """Suma en ``destino`` los pesos de salida de las presinápticas ``pre`` (CSR + bincount)."""
        ini = self.indptr[pre]
        cnt = self.indptr[pre + 1] - ini
        tot = int(cnt.sum())
        if tot:
            offs = np.repeat(ini - np.cumsum(cnt) + cnt, cnt) + np.arange(tot)
            destino += np.bincount(self.post[offs], weights=self.w[offs], minlength=self.N)

    def simular(self, pasos: int, *, plasticidad: bool = True) -> _ResultadoChunk:
        """Simula ``pasos`` pasos de ``dt`` ms y devuelve conteos por población y lecturas crudas."""
        N = self.N
        D1 = self.D + 1
        u, g, a, buf = self.u, self.g, self.a, self.buf
        tmp, mascara = self._tmp, self._mascara
        ds, da, dm, c1, theta = self.ds, self.da, self.dm, self.c1, _THETA_MV
        rng = self.rng
        ruido = rng.standard_normal((pasos, N), dtype=np.float32)
        ruido *= self.sig_sq
        kc_lo, kc_hi, lims = self.kc_lo, self.kc_hi, self.kc_lims
        apbp_lo = int(lims[2])
        dan_lo, dan_hi = self.dan_lo, self.dan_hi
        ref_hist = self.ref_hist
        todas: list[Any] = []
        vacio = np.empty(0, dtype=np.int64)
        hecho = 0
        while hecho < pasos:
            nb = min(self.bloque, pasos - hecho)
            self._componer()
            drive = self.drive
            pn_blk = rng.random((nb, _N_ENTRADAS), dtype=np.float32) < self.pn_prob
            dan_blk = (rng.random((nb, dan_hi - dan_lo), dtype=np.float32) < self.dan_prob
                       if self.dan_activo else None)
            kc_blk: list[Any] = []
            spk_blk: list[Any] = []
            for k in range(nb):
                t = self.t
                llegada = buf[t % D1]
                g *= ds
                g += llegada
                llegada.fill(0.0)
                a *= da
                np.add(g, drive, out=tmp)
                tmp -= a
                tmp *= c1
                u *= dm
                u += tmp
                u += ruido[hecho + k]
                for r in ref_hist:   # refractario: los que dispararon en los últimos ref_pasos pasos
                    if r.size:
                        u[r] = 0.0
                np.greater_equal(u, theta, out=mascara)
                if dan_blk is not None:
                    mascara[dan_lo:dan_hi] |= dan_blk[k]
                spk = np.flatnonzero(mascara)
                if spk.size:
                    u[spk] = 0.0
                    a[spk] += self.delta_a[spk]
                ref_hist.append(spk)
                pn = np.flatnonzero(pn_blk[k])
                destino = buf[(t + self.D) % D1]
                if pn.size:
                    self._propagar(np.concatenate((spk, pn + N)), destino)
                elif spk.size:
                    self._propagar(spk, destino)
                if spk.size:
                    li = np.searchsorted(spk, lims)
                    if li[3] > li[0]:
                        n_kc = int(li[3] - li[0])
                        destino[kc_lo:kc_hi] -= self.apl_w * n_kc      # APL graduada
                        kcs = spk[li[0]:li[3]] - kc_lo
                        self.kc_ultimo[kcs] = self.t_ms
                        kc_blk.append(kcs)
                        if li[3] > li[2]:
                            destino[self.sl_a3] += self.W_a3[spk[li[2]:li[3]] - apbp_lo].sum(axis=0)
                        if li[1] > li[0]:
                            destino[self.sl_avd] += self.W_avd[spk[li[0]:li[1]] - kc_lo].sum(axis=0)
                        if li[2] > li[0]:
                            destino[self.sl_app] += self.W_app[spk[li[0]:li[2]] - kc_lo].sum(axis=0)
                    spk_blk.append(spk)
                self.t += 1
                self.t_ms += self.dt
            hecho += nb
            bloque_spk = np.concatenate(spk_blk) if spk_blk else vacio
            if plasticidad:
                self._plasticidad(nb * self.dt, kc_blk, bloque_spk)
            if spk_blk:
                todas.append(bloque_spk)
        if plasticidad:
            self._recuperar_pesos(pasos * self.dt)
        spikes = np.concatenate(todas) if todas else vacio
        cnt = np.bincount(self.pop_id[spikes], minlength=len(_ORDEN_POBLACIONES))
        epg = spikes[spikes < self.sl["EPG"].stop]
        cols = np.bincount(epg // 3, minlength=_N_COL).astype(np.float64).tolist()
        a_media = float(a[self.no_anillo].mean())
        kc_frac = float(np.count_nonzero(self.kc_ultimo >= self.t_ms - 300.0)) / max(1, self.n_kc)
        crc = zlib.crc32(cnt.astype(np.int32).tobytes())
        finito = math.isfinite(float(u.sum()) + float(g.sum()) + float(a.sum()))
        return _ResultadoChunk(pasos * self.dt, cnt, cols, a_media, kc_frac, crc, finito)

    def _plasticidad(self, delta_ms: float, kc_blk: list[Any], spikes: Any) -> None:
        """Trazas de elegibilidad y dopaminérgicas y depresión KC→MBON (cada 10 ms, A.3)."""
        f = math.exp(-delta_ms / _TAU_TRAZA_MS)
        self.e_kc *= f
        if kc_blk:
            kcs = np.concatenate(kc_blk)
            self.e_kc += np.bincount(kcs, minlength=self.n_kc).astype(np.float32)
        dan = spikes[(spikes >= self.dan_lo) & (spikes < self.dan_hi)] - self.dan_lo
        cnt_dan = np.zeros(3)
        if dan.size:
            comp = self.dan_comp[dan]
            comp = comp[comp >= 0]
            cnt_dan = np.bincount(comp, minlength=3).astype(np.float64)
        self.d_traza = self.d_traza * f + cnt_dan * self.dan_norm
        eta = _ETA_POR_MS * delta_ms
        if float(self.e_kc.max(initial=0.0)) < 1e-3:
            return
        g_lo, ab_lo, apbp_lo = 0, int(self.kc_lims[1] - self.kc_lo), int(self.kc_lims[2] - self.kc_lo)
        for matriz, traza, d in (
            (self.W_a3, self.e_kc[apbp_lo:], self.d_traza[0]),
            (self.W_avd, self.e_kc[g_lo:ab_lo], self.d_traza[1]),
            (self.W_app, self.e_kc[g_lo:apbp_lo], self.d_traza[2]),
        ):
            if d <= 1e-6:
                continue
            fac = np.clip(eta * d * traza, 0.0, 0.9).astype(np.float32)
            matriz *= (1.0 - fac)[:, None]

    def _recuperar_pesos(self, delta_ms: float) -> None:
        """Recuperación lenta de la familiaridad: ``W ← W + (W0 − W)·Δ/30 min``."""
        k = np.float32(min(1.0, delta_ms / _TAU_RECUPERACION_MS))
        for matriz, base in ((self.W_a3, self.W0_a3), (self.W_avd, self.W0_avd), (self.W_app, self.W0_app)):
            matriz += (base - matriz) * k

    # -- persistencia de pesos ----------------------------------------------------

    def firma(self) -> dict[str, Any]:
        """Identidad de la red para validar los pesos plásticos guardados."""
        return {"version_red": VERSION_RED, "semilla_red": self.semilla_red, "n_kc": self.n_kc,
                "n_pn": _N_PN_TEXTO, "k_pn": _K_PN, "modo": self.modo}

    def pesos_plasticos(self) -> dict[str, Any]:
        """Copias de las matrices plásticas KC→MBON."""
        return {"W_a3": self.W_a3.copy(), "W_avd": self.W_avd.copy(), "W_app": self.W_app.copy()}

    def reiniciar_pesos(self) -> None:
        """W = W0 (sin familiaridad aprendida)."""
        self.W_a3 = self.W0_a3.copy()
        self.W_avd = self.W0_avd.copy()
        self.W_app = self.W0_app.copy()

    def cargar_pesos(self, pesos: Mapping[str, Any], factor: float) -> bool:
        """``W ← W0 + (W_guardado − W0)·factor`` si todas las formas coinciden; si no, no toca nada."""
        nuevos: dict[str, Any] = {}
        for clave, base in (("W_a3", self.W0_a3), ("W_avd", self.W0_avd), ("W_app", self.W0_app)):
            m = pesos.get(clave)
            if m is None:
                return False
            m = np.asarray(m, dtype=np.float32)
            if m.shape != base.shape or not np.isfinite(m).all():
                return False
            m = np.clip(m, 0.0, None)
            nuevos[clave] = (base + (m - base) * np.float32(_clip(factor, 0.0, 1.0))) * (base > 0)
        self.W_a3 = nuevos["W_a3"].astype(np.float32)
        self.W_avd = nuevos["W_avd"].astype(np.float32)
        self.W_app = nuevos["W_app"].astype(np.float32)
        return True

    def familiaridad(self) -> float:
        """Índice de familiaridad global ``H = 1 − mean(W_α'3)/W0`` (0 = todo nuevo)."""
        return float(1.0 - self.W_a3.mean() / self.W0_a3.mean())


# ---------------------------------------------------------------------------
# Lecturas homeostáticas del modelo reducido (A.5)
# ---------------------------------------------------------------------------


class _LectorHomeostatico:
    """Tasas EMA, z-scores y lecturas homeostáticas (arousal, focus, boredom, fatiga, curiosidad, valencia)."""

    def __init__(self, r_base: Mapping[str, float], a_base: float, r_novel: float) -> None:
        self.r_base = {k: max(0.0, _real(r_base.get(k), _TASAS_OBJETIVO[k])) for k in _CLAVES_TASA}
        self.r_ema = dict(self.r_base)
        self.z = {k: 0.0 for k in _CLAVES_TASA}
        self.a_base = max(1e-3, _real(a_base, 0.05))
        self.a_media = self.a_base
        self.r_novel = max(_R_NOVEL_MIN, _real(r_novel, _R_NOVEL_DEFECTO))
        self.A, self.Fo, self.B, self.F, self.C, self.V = X_EQ_DEFECTO[0], X_EQ_DEFECTO[1], 0.20, 0.25, 0.10, 0.0
        self.nu = 0.0
        self.nu_s = 0.0
        self.rho = 0.0
        self.phi = 0.0
        self.omega = 0.0
        self.bump = [0.0] * _N_COL
        self._ventana: collections.deque[tuple[float, list[float]]] = collections.deque()
        self._fases: collections.deque[tuple[float, float]] = collections.deque()
        self._t_s = 0.0
        self.iniciar_A = True
        self.iniciar_F = True
        self.tam: dict[str, float] = {}
        self._cos = [math.cos(2.0 * math.pi * k / _N_COL) for k in range(_N_COL)]
        self._sin = [math.sin(2.0 * math.pi * k / _N_COL) for k in range(_N_COL)]
        # Historial de la calibración: (dt, r_ema, c, P, ā, reposo) por chunk. ``None`` fuera de ella.
        self.historial: list[tuple[float, dict[str, float], float, float, float, bool]] | None = None

    # -- objetivos (A.5), compartidos por la lectura y la fijación de la línea base ----------------

    @staticmethod
    def _objetivo_A(z: Mapping[str, float], c: float, reposo: bool) -> float:
        """``a* = σ(1,2·z_PAM + 0,8·z_PPL1 + 1,0·z_glob − 1,0·z_dFB·[reposo] − 0,4·(c − 0,5) − 0,2)``."""
        return _sigmoide(1.2 * z["PAM"] + 0.8 * z["PPL1"] + 1.0 * z["glob"]
                         - (1.0 * z["dFB"] if reposo else 0.0) - 0.4 * (c - 0.5) - 0.2)

    def _objetivo_F(self, p: float, z_dfb: float, c: float, a_media: float) -> float:
        """``F* = σ(3,0·(P − 0,5) + 0,8·z_dFB + 1,2·(c − 0,5) + 0,5·(ā/ā_base − 1))``."""
        sfa = _clip(a_media / max(self.a_base, 1e-3) - 1.0, -1.0, 2.0)
        return _sigmoide(3.0 * (p - 0.5) + 0.8 * z_dfb + 1.2 * (c - 0.5) + 0.5 * sfa)

    def _z(self, r_ema: Mapping[str, float]) -> dict[str, float]:
        return {k: math.log2((r_ema[k] + 0.5) / (self.r_base[k] + 0.5)) for k in _CLAVES_TASA}

    def fijar_linea_base(self, n_base: int, *, reiniciar_F: bool) -> None:
        """Congela la línea base con la MEDIA de los últimos ``n_base`` chunks del historial (A.5).

        ``r_base`` es la media de la EMA de cada población y ``ā_base`` la media de la adaptación: la EMA
        de un instante (lo que dice A.5 al pie de la letra) tiene un ruido del 20–50 % en PPL1, dFB o
        MBON-α'3 y sesgaba todos los *z* posteriores. Con la base nueva se **reproduce** la EMA asimétrica
        de A sobre esa misma ventana (en vez de reiniciar A en su objetivo instantáneo, que lo dejaba
        lejos de su régimen y en plena subida durante la ventana de X_EQ) y, si no hay una fatiga
        guardada reciente, F parte de la media de su objetivo en la ventana. Vacía el historial.
        """
        historial = self.historial or []
        self.historial = None
        ventana = historial[-max(1, int(n_base)):]
        if not ventana:
            return
        n = float(len(ventana))
        self.r_base = {k: max(0.0, sum(h[1][k] for h in ventana) / n) for k in _CLAVES_TASA}
        self.a_base = max(1e-3, sum(h[4] for h in ventana) / n)
        A: float | None = None
        suma_f = 0.0
        for dt, r_ema, c, p, a_media, reposo in ventana:
            z = self._z(r_ema)
            a_obj = self._objetivo_A(z, c, reposo)
            A = a_obj if A is None else _ema(A, a_obj, dt, 2.0 if a_obj > A else 8.0)
            suma_f += self._objetivo_F(p, z["dFB"], c, a_media)
        if A is not None:
            self.A = A
        self.iniciar_A = False
        if reiniciar_F:
            self.F = suma_f / n
            self.iniciar_F = False
        self.z = self._z(self.r_ema)

    def configurar(self, red: _RedReducida) -> None:
        """Tamaños de población de la red activa (cambian con el modo de degradación)."""
        t = red.tam
        n_kc = sum(t[k] for k in _KC)
        n_mbon = t["MBON_app"] + t["MBON_avd"] + t["MBON_otr"]
        self.tam = {
            "EPG": t["EPG"], "PEN1": t["PEN1"], "PEN2": t["PEN2"], "PEG": t["PEG"], "D7": t["D7"],
            "ER_vis": t["ER_vis"], "ER5": t["ER5"], "ExR1": t["ExR1"], "dFB": t["dFB"], "KC": n_kc,
            "MBON_a3": t["MBON_a3"], "MBON": n_mbon, "PAM": t["PAM"], "PPL1": t["PPL1"],
            "glob": red.N - n_kc,
        }

    def tasas_publicas(self) -> dict[str, float]:
        """Tasas EMA por población (Hz, 2 decimales) para ``HomeostaticState.tasas``."""
        return {k: round(v, 2) for k, v in self.r_ema.items() if k != "glob"}

    def actualizar(self, res: _ResultadoChunk, dt_s: float, *, c: float, sueno: _ProcesoSueno,
                   dt_sueno_s: float, reposo: bool, k_act: float) -> None:
        """Una lectura completa a partir del resumen del chunk (A.5)."""
        cnt = res.cnt.tolist()
        (c_epg, c_pen1, c_pen2, c_peg, c_d7, c_er, c_er5, c_exr1, c_dfb, c_kg, c_kab, c_kapbp,
         c_a3, c_app, c_avd, c_otr, c_pam, c_ppl1) = cnt
        c_kc = c_kg + c_kab + c_kapbp
        conteos = {
            "EPG": c_epg, "PEN1": c_pen1, "PEN2": c_pen2, "PEG": c_peg, "D7": c_d7, "ER_vis": c_er,
            "ER5": c_er5, "ExR1": c_exr1, "dFB": c_dfb, "KC": c_kc, "MBON_a3": c_a3,
            "MBON": c_app + c_avd + c_otr, "PAM": c_pam, "PPL1": c_ppl1, "glob": sum(cnt) - c_kc,
        }
        dt = max(1e-4, dt_s)
        for k in _CLAVES_TASA:
            n = max(1.0, float(self.tam.get(k, 1)))
            r_inst = conteos[k] / (n * dt)
            tau = _TAU_TASA_A3_S if k == "MBON_a3" else _TAU_TASA_S
            self.r_ema[k] = _ema(self.r_ema[k], r_inst, dt, tau)
            self.z[k] = math.log2((self.r_ema[k] + 0.5) / (self.r_base[k] + 0.5))
        z = self.z

        # Proceso S (tiempo de reloj × escala) antes de la fatiga.
        sueno.avanzar(dt_sueno_s, reposo=reposo, z_glob=z["glob"], k_act=k_act)
        p = sueno.p
        if self.historial is not None:
            self.historial.append((dt, dict(self.r_ema), c, p, res.a_media, reposo))

        # Arousal: τ = 2 s al subir y 8 s al bajar; el dFB solo en reposo.
        a_obj = self._objetivo_A(z, c, reposo)
        if self.iniciar_A:
            self.A = a_obj
            self.iniciar_A = False
        else:
            self.A = _ema(self.A, a_obj, dt, 2.0 if a_obj > self.A else 8.0)

        # Valencia (PAM − PPL1).
        self.V = math.tanh(0.8 * (z["PAM"] - z["PPL1"]))

        # Focus: coherencia PPC del bump E-PG en una ventana de 200 ms.
        self._ventana.append((dt, list(res.cols_epg)))
        total = sum(d for d, _ in self._ventana)
        while len(self._ventana) > 1 and total - self._ventana[0][0] >= _VENTANA_FOCUS_S - 1e-9:
            total -= self._ventana.popleft()[0]
        n_k = [0.0] * _N_COL
        for _, cols in self._ventana:
            for i, v in enumerate(cols):
                n_k[i] += v
        s_tot = sum(n_k)
        q = sum(v * v for v in n_k)
        zx = sum(v * cc for v, cc in zip(n_k, self._cos))
        zy = sum(v * ss for v, ss in zip(n_k, self._sin))
        self._t_s += dt
        if s_tot >= 2.0:
            den = s_tot * s_tot - q
            if den > 1e-9:
                self.rho = math.sqrt(max(0.0, (zx * zx + zy * zy - q) / den))
            else:
                self.rho = 1.0  # todas las spikes en una sola columna
            self.phi = math.atan2(zy, zx)
            self._fases.append((self._t_s, self.phi))
        else:
            self.rho = 0.0
        # ω: |Δφ| entre estimaciones separadas una ventana completa (200 ms). Diferenciar ventanas
        # solapadas cada 50 ms mediría sobre todo el ruido del estimador y no el giro del bump.
        while len(self._fases) > 1 and self._t_s - self._fases[1][0] >= _VENTANA_OMEGA_S - 1e-9:
            self._fases.popleft()
        if len(self._fases) > 1 and self._t_s - self._fases[0][0] >= _VENTANA_OMEGA_S - 1e-9:
            t0, phi0 = self._fases[0]
            vel = abs(_envolver(self.phi - phi0)) / max(1e-3, self._t_s - t0)
        else:
            vel = 0.0 if s_tot >= 2.0 else self.omega
        self.omega = _ema(self.omega, vel, dt, 0.5)
        pico = max(n_k) / (3.0 * max(total, 1e-3)) if n_k else 0.0    # Hz por neurona E-PG de la columna pico
        amp = 1.0 - math.exp(-pico / 20.0)
        fo_obj = _clip((self.rho - 0.30) / 0.55, 0.0, 1.0) * math.exp(-self.omega / math.pi) * amp
        self.Fo = _ema(self.Fo, fo_obj, dt, 1.5)
        # bump[k] = N_k/max(N) con N_k de la MISMA ventana de 200 ms que ρ y φ, suavizado con τ = 0,3 s.
        maximo = max(n_k)
        objetivo_bump = [v / maximo for v in n_k] if maximo > 0 else [0.0] * _N_COL
        self.bump = [_ema(b, o, dt, 0.3) for b, o in zip(self.bump, objetivo_bump)]

        # Novedad y aburrimiento (MBON-α'3), con la zona muerta del ruido de reposo.
        umbral = self.r_base["MBON_a3"] + _ZONA_MUERTA_NOVEDAD_HZ
        rango = max(1.0, self.r_novel - umbral)
        self.nu = _clip((self.r_ema["MBON_a3"] - umbral) / rango, 0.0, 1.0)
        self.B = _clip(self.B + ((1.0 - self.B) * (1.0 - self.nu) * (1.0 - self.F) / 300.0
                                 - self.B * self.nu / 3.0) * dt, 0.0, 1.0)

        # Fatiga: proceso S + dFB + proceso C + SFA.
        self.a_media = res.a_media
        f_obj = self._objetivo_F(p, z["dFB"], c, self.a_media)
        if self.iniciar_F:
            self.F = f_obj
            self.iniciar_F = False
        else:
            self.F = _ema(self.F, f_obj, dt, 60.0)

        # Curiosidad: sorpresa + B·A·(1 − F).
        nu_s_prev = self.nu_s
        self.nu_s = _ema(self.nu_s, self.nu, dt, 1.0)
        sorpresa = max(0.0, (self.nu_s - nu_s_prev) / dt) * 1.0
        c_obj = _clip(1.5 * sorpresa + 1.6 * self.B * self.A * (1.0 - self.F), 0.0, 1.0)
        self.C = _ema(self.C, c_obj, dt, _TAU_C_SUBIDA_S if c_obj > self.C else _TAU_C_S)

    @property
    def rumbo_deg(self) -> float:
        """Fase del bump en grados (0..360)."""
        return math.degrees(self.phi) % 360.0


# ---------------------------------------------------------------------------
# Modo simple (A.6): Python puro
# ---------------------------------------------------------------------------


class _ModeloSimple:
    """Modelo homeostático en Python puro (sin numpy), con la misma semántica de variables (Anexo A.6)."""

    def __init__(self, *, A: float = X_EQ_DEFECTO[0], Fo: float = X_EQ_DEFECTO[1], B: float = 0.20,
                 F: float = 0.25, C: float = 0.10, V: float = 0.0, rumbo_deg: float = 0.0) -> None:
        self.A = _clip(A, 0.0, 1.0)
        self.Fo = _clip(Fo, 0.0, 1.0)
        self.B = _clip(B, 0.0, 1.0)
        self.F = _clip(F, 0.0, 1.0)
        self.C = _clip(C, 0.0, 1.0)
        self.V = _clip(V, -1.0, 1.0)
        self.nu = 0.0
        self.nu_s = 0.0
        self.rumbo_deg = rumbo_deg % 360.0
        self.objetivo_fo = 0.70
        self.iniciar_F = True
        self._textos: collections.deque[frozenset[str]] = collections.deque(maxlen=20)
        self._clases: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._ultima_ventana_ts: float | None = None
        self.bump: list[float] = self._calcular_bump()

    def evento(self, nombre: str, datos: Mapping[str, Any], ts: float) -> None:
        """Efecto de un evento sobre las variables (tabla de A.6)."""
        if nombre == "usuario_escribe":
            tri = _trigramas(_texto(datos.get("texto")))
            sim = 0.0
            for previo in self._textos:
                union = len(tri | previo)
                if union:
                    sim = max(sim, len(tri & previo) / union)
            self._textos.append(tri)
            self.nu = max(self.nu, 1.0 - sim)
            self.A += 0.15 * (1.0 - self.A)
            self.V += 0.05
        elif nombre == "ventana_cambia":
            clase = _texto(datos.get("clase"))
            if self._ultima_ventana_ts is not None and ts - self._ultima_ventana_ts < 3.0:
                self.Fo -= 0.15
            else:
                self.Fo -= 0.05
            self._ultima_ventana_ts = ts
            if clase not in self._clases:
                self.nu = max(self.nu, 0.3)
            self._clases[clase] = None
            self._clases.move_to_end(clase)
            while len(self._clases) > 256:
                self._clases.popitem(last=False)
            self.rumbo_deg = 360.0 * _h(clase, _N_COL) / _N_COL
        elif nombre == "carga_sistema":
            cpu = _real(datos.get("cpu"), 0.0)
            temp = datos.get("temp")
            temp_v = _real(temp, -math.inf) if temp is not None else -math.inf
            if cpu > 85.0 or temp_v > 80.0:
                self.A += 0.05
                self.V -= 0.05
        elif nombre == "rostro":
            if bool(datos.get("presente", False)):
                self.A += 0.1
                self.V += 0.1
                self.nu = max(self.nu, 0.5)
        elif nombre == "busqueda_completa":
            if bool(datos.get("exito", False)):
                self.V += 0.1
                self.nu = max(self.nu, 0.4)
            else:
                self.V -= 0.1
        elif nombre == "error":
            sev = _clip(_real(datos.get("severidad"), 0.5), 0.0, 1.0)
            self.A += 0.3 * sev
            self.V -= 0.3 * sev
        elif nombre == "recompensa":
            mag = _clip(_real(datos.get("magnitud"), 0.5), 0.0, 1.0)
            self.V += 0.4 * mag
            self.A += 0.1 * mag
        elif nombre == "nexus_foco":
            self.objetivo_fo = 0.85 if bool(datos.get("enfocado", False)) else 0.70
        self._acotar()

    def _acotar(self) -> None:
        self.A = _clip(self.A, 0.0, 1.0)
        self.Fo = _clip(self.Fo, 0.0, 1.0)
        self.B = _clip(self.B, 0.0, 1.0)
        self.F = _clip(self.F, 0.0, 1.0)
        self.C = _clip(self.C, 0.0, 1.0)
        self.V = _clip(self.V, -1.0, 1.0)
        self.nu = _clip(self.nu, 0.0, 1.0)

    def _calcular_bump(self) -> list[float]:
        centro = (self.rumbo_deg / 360.0) * _N_COL
        ancho = 2.0 + 4.0 * (1.0 - self.Fo)
        bump = []
        for k in range(_N_COL):
            d = abs(k - centro) % _N_COL
            d = min(d, _N_COL - d)
            bump.append(math.exp(-0.5 * (d / ancho) ** 2))
        return bump

    def avanzar(self, dt_s: float, *, c: float, sueno: _ProcesoSueno, dt_sueno_s: float, reposo: bool,
                k_act: float) -> None:
        """Dinámica continua de A.6 durante ``dt_s`` segundos simulados."""
        dt = max(1e-4, dt_s)
        self.A = _ema(self.A, 0.40 + 0.2 * (1.0 - c), dt, 60.0)
        self.Fo = _ema(self.Fo, self.objetivo_fo, dt, 30.0)
        self.nu *= math.exp(-dt / 1.0)
        self.B = _clip(self.B + ((1.0 - self.B) * (1.0 - self.nu) * (1.0 - self.F) / 300.0
                                 - self.B * self.nu / 3.0) * dt, 0.0, 1.0)
        sueno.avanzar(dt_sueno_s, reposo=reposo, z_glob=0.0, k_act=k_act)
        f_obj = _sigmoide(3.0 * (sueno.p - 0.5) + 1.2 * (c - 0.5))
        if self.iniciar_F:
            self.F = f_obj
            self.iniciar_F = False
        else:
            self.F = _ema(self.F, f_obj, dt, 60.0)
        nu_s_prev = self.nu_s
        self.nu_s = _ema(self.nu_s, self.nu, dt, 1.0)
        sorpresa = max(0.0, (self.nu_s - nu_s_prev) / dt) * 1.0
        c_obj = _clip(1.5 * sorpresa + 1.6 * self.B * self.A * (1.0 - self.F), 0.0, 1.0)
        self.C = _ema(self.C, c_obj, dt, _TAU_C_SUBIDA_S if c_obj > self.C else _TAU_C_S)
        self.V = _ema(self.V, 0.0, dt, 120.0)
        self._acotar()
        self.bump = self._calcular_bump()


# ---------------------------------------------------------------------------
# Fachada pública
# ---------------------------------------------------------------------------


class SincanConnectome:
    """Conectoma CX+MB de Amadeus: homeostasis, divergencia e iniciativas sin LLM (§10.6).

    ``__init__`` es barato (< 200 ms): construye la red y carga el estado guardado, sin
    *benchmark* ni calibración (eso lo hace ``await iniciar()``). Toda la simulación corre en
    un executor propio de un hilo; ``inyectar`` y ``estado`` son seguros desde cualquier hilo.
    """

    def __init__(self, settings: Settings | None = None, *, fuente: str | None = None,
                 semilla: int | None = None, semilla_red: int | None = None,
                 ruta_estado: Path | None = None, reloj: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._reloj = reloj
        escala = _real(getattr(settings, "conectoma_escala_tiempo", 1.0), 1.0) if settings is not None else 1.0
        self._escala = escala if escala > 0 else 1.0
        self._lock = threading.RLock()
        self._cola: collections.deque[tuple[str, dict[str, Any], float]] = collections.deque(maxlen=1024)
        self._ejecutor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rams-conectoma")
        self._abortar_inicio = threading.Event()
        self._tarea_inicio: asyncio.Future[Any] | None = None
        self._iniciado = False
        self._cerrado = False
        self._guardado_final = False
        self._fallos = 0
        self._ultimo_aviso_fallo = -math.inf
        self._ultimo_aviso_nan = -math.inf

        # Semilla del ruido: argumento, RAMS_CONNECTOME_SEED o aleatoria.
        if semilla is None and settings is not None:
            semilla = getattr(settings, "conectoma_semilla", None)
        self._semilla_ruido: int | None = self._entero_o_none(semilla, "semilla")

        # Rutas de persistencia.
        if ruta_estado is not None:
            self._ruta_estado: Path | None = Path(ruta_estado)
        elif settings is not None:
            self._ruta_estado = Path(settings.data_dir) / "connectome_estado.json"
        else:
            self._ruta_estado = None
        self._ruta_npz: Path | None = None
        if self._ruta_estado is not None:
            if self._ruta_estado.name == "connectome_estado.json":
                self._ruta_npz = self._ruta_estado.with_name("connectome_plastico.npz")
            else:
                self._ruta_npz = self._ruta_estado.with_suffix(".npz")

        # Selección de fuente.
        if fuente is None:
            fuente = getattr(settings, "conectoma_fuente", "reducido") if settings is not None else "stub"
        fuente = str(fuente).strip().lower()
        self._motivo_stub = ""
        if fuente == "flywire":
            log.info("El modo flywire es una herramienta offline en v0.1; uso el modelo reducido")
            fuente = "reducido"
        elif fuente not in ("reducido", "stub"):
            log.warning("Fuente de conectoma desconocida (%s); uso el modelo reducido", fuente[:40])
            fuente = "reducido"
        if fuente == "reducido" and np is None:
            log.info("numpy no está instalado: el conectoma usa el modo simple")
            fuente = "stub"
            self._motivo_stub = "sin numpy"
        elif fuente == "stub":
            self._motivo_stub = "configurado" if settings is not None else "modo de pruebas"
        self.fuente_activa: str = fuente
        self.modo_degradacion: str = "normal" if fuente == "reducido" else "stub"
        self.n_neuronas: int = 0

        # Estado persistido (lectura barata, antes de construir la red: aporta semilla_red).
        datos = self._leer_json()
        semilla_red_ok = self._entero_o_none(semilla_red, "semilla_red")
        if semilla_red_ok is None and datos is not None:
            semilla_red_ok = self._semilla_guardada(datos)
        if semilla_red_ok is None:
            semilla_red_ok = secrets.randbits(32)   # una sola vez: se persiste en el siguiente guardar_estado
        self._semilla_red: int = abs(semilla_red_ok) & 0xFFFFFFFFFFFFFFFF

        # Variables compartidas por todas las fuentes.
        self._sueno = _ProcesoSueno(0.30)
        self._x_eq: tuple[float, float, float, float] = tuple(X_EQ_DEFECTO)  # type: ignore[assignment]
        self._r_base: dict[str, float] = dict(_TASAS_OBJETIVO)
        self._a_base = 0.05
        self._r_novel = _R_NOVEL_DEFECTO
        self._respuestas_novedad: collections.deque[tuple[float, float]] = collections.deque(maxlen=512)
        self._ventanas_novedad: list[list[float]] = []      # [ms restantes, pico]
        self._rumbo_asignado_col = 0
        self._nexus_enfocado = False
        self._carga_alta = False
        self._intero_hz = _PN_FONDO_HZ
        self._cal_offsets: dict[str, float] = {}
        self._modo = "vigilia"
        self._ausente_desde: float | None = None
        self._k_act_hasta = -math.inf
        self._anomalo_hasta_ms = -math.inf
        self._t_ms = 0.0
        self._factor_tr = 1.0
        self._ms_chunk = 0.0
        self._ultimo_reloj: float | None = None
        self._campo_previo = "steins_gate"
        self._red_guardada: Mapping[str, Any] | None = None
        self._fatiga_guardada_reciente = False

        self.medidor = MedidorDivergencia(X_EQ_DEFECTO)
        self.motor_iniciativa = MotorIniciativa(escala_tiempo=self._escala)
        self._red: _RedReducida | None = None
        self._lector: _LectorHomeostatico | None = None
        self._simple: _ModeloSimple | None = None

        if self.fuente_activa == "reducido":
            try:
                self._construir_reducido("normal")
            except Exception as exc:  # cualquier fallo de construcción ⇒ modo simple
                log.warning("No se pudo construir la red reducida (%s); uso el modo simple", exc)
                self._pasar_a_stub("error al construir la red")
        if self.fuente_activa == "stub":
            self._simple = _ModeloSimple()

        self._estado: HomeostaticState = HomeostaticState(fuente=self.fuente_activa, ts=self._ahora())
        self._diag: dict[str, float] = self._diag_vacio()
        if datos is not None:
            try:
                self._aplicar_estado(datos)
            except Exception as exc:
                log.warning("Estado del conectoma inválido (%s); lo ignoro", exc)
        self._estado = HomeostaticState(fuente=self.fuente_activa, presion_sueno=self._sueno.p,
                                        ts=self._ahora())

    # -- utilidades internas -------------------------------------------------------

    def _ahora(self) -> float:
        try:
            v = float(self._reloj())
            return v if math.isfinite(v) else time.time()
        except Exception as exc:
            log.debug("Reloj del conectoma falló (%s); uso time.time", exc)
            return time.time()

    def _hora_decimal(self, ts: float) -> float:
        try:
            zona = zona_local(self._settings) if self._settings is not None else _zona_defecto()
            momento = datetime.fromtimestamp(ts, zona)
            return momento.hour + momento.minute / 60.0 + momento.second / 3600.0
        except (OverflowError, OSError, ValueError) as exc:
            log.debug("Hora local no disponible (%s); uso mediodía", exc)
            return 12.0

    def _diag_vacio(self) -> dict[str, float]:
        return {"novedad": 0.0, "kc_activas_pct": 0.0, "rho": 0.0, "tasa_mbon_a3": 0.0,
                "ms_por_chunk": 0.0, "factor_tiempo_real": 1.0}

    @staticmethod
    def _entero_o_none(valor: Any, nombre: str) -> int | None:
        if valor is None:
            return None
        if isinstance(valor, bool):
            log.warning("Valor inválido para %s del conectoma; uso uno aleatorio", nombre)
            return None
        try:
            return int(valor)
        except (TypeError, ValueError, OverflowError):
            log.warning("Valor inválido para %s del conectoma; uso uno aleatorio", nombre)
            return None

    @staticmethod
    def _semilla_guardada(datos: Mapping[str, Any]) -> int | None:
        red = datos.get("red")
        if isinstance(red, Mapping):
            valor = red.get("semilla_red")
            if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
                return valor
        return None

    def _nueva_rng(self, nivel: int) -> Any:
        if self._semilla_ruido is None:
            return np.random.default_rng()
        return np.random.default_rng([abs(self._semilla_ruido), nivel])

    def _construir_reducido(self, modo: str) -> None:
        """Construye (o reconstruye) la red en ``modo`` con la misma ``semilla_red``."""
        anterior = self._red
        nivel = _MODOS.index(modo)
        red = _RedReducida(semilla_red=self._semilla_red, rng=self._nueva_rng(nivel), modo=modo)
        if anterior is not None and anterior.n_kc == red.n_kc:
            red.cargar_pesos(anterior.pesos_plasticos(), 1.0)
        elif anterior is not None:
            log.info("Pesos plásticos de otra red; empiezo con W0")
        if anterior is not None:
            red.t_ms = anterior.t_ms
        self._red = red
        if self._lector is None:
            self._lector = _LectorHomeostatico(self._r_base, self._a_base, self._r_novel)
        self._lector.configurar(red)
        self._aplicar_tonicos()
        self.fuente_activa = "reducido"
        self.modo_degradacion = modo
        self.n_neuronas = red.N

    def _pasar_a_stub(self, motivo: str) -> None:
        """Abandona la red numpy y continúa con el modelo simple, conservando las variables."""
        lector = self._lector
        if lector is not None:
            # Fo del modelo simple no se mide (no hay bump): es un objetivo homeostático propio (0,70, o
            # 0,85 con Nexus enfocado). Arrancar desde el Fo medido de la red solo crearía una deriva de
            # τ = 30 s ajena al estado de Amadeus.
            objetivo_fo = 0.85 if self._nexus_enfocado else 0.70
            self._simple = _ModeloSimple(A=lector.A, Fo=objetivo_fo, B=lector.B, F=lector.F, C=lector.C,
                                         V=lector.V, rumbo_deg=lector.rumbo_deg)
            self._simple.objetivo_fo = objetivo_fo
            self._simple.iniciar_F = False
        elif self._simple is None:
            self._simple = _ModeloSimple()
        self._red = None
        self.fuente_activa = "stub"
        self.modo_degradacion = "stub"
        self.n_neuronas = 0
        self._motivo_stub = motivo

    def _aplicar_tonicos(self) -> None:
        """Reaplica a la red los estímulos tónicos vigentes (landmark, interocepción, PPL1, ruido ER, calibración)."""
        red = self._red
        if red is None:
            return
        if self._nexus_enfocado:
            red.set_landmark_tonico(_h("nexus", _N_COL), _LANDMARK_NEXUS_MV)
        else:
            red.set_landmark_tonico(self._rumbo_asignado_col, _LANDMARK_TONICO_MV)
        red.set_intero(self._intero_hz)
        red.set_ppl1_tonico(8.0 if self._carga_alta else 0.0)
        red.set_ruido_er((0.5 if self._carga_alta else 0.0) + (-0.3 if self._nexus_enfocado else 0.0))
        red.set_calibracion(self._cal_offsets)

    def _bajar_escalon(self, motivo: str = "rendimiento insuficiente") -> None:
        """Baja un escalón de la cascada normal → ligero → dt2 → stub (en el executor)."""
        with self._lock:
            actual = self.modo_degradacion
            if actual == "stub":
                return
            siguiente = _MODOS[_MODOS.index(actual) + 1]
            if siguiente == "stub":
                self._pasar_a_stub(motivo)
            else:
                try:
                    self._construir_reducido(siguiente)
                except Exception as exc:
                    log.warning("No se pudo reconstruir la red en modo %s (%s); paso al modo simple", siguiente, exc)
                    self._pasar_a_stub(motivo)
            log.warning("Conectoma: bajo del modo %s al modo %s (%s)", actual, self.modo_degradacion, motivo)
            # En ejecución (tras iniciar) el modo simple necesita su propio equilibrio. Durante iniciar no
            # hace falta: la calibración viene justo después. Los escalones ligero y dt2 conservan la
            # topología y la base calibrada (deriva medida m ≈ 0,04) y no se recalibran: repetir 10 s
            # simulados en una máquina que va lenta pararía el bucle varios segundos.
            if self.modo_degradacion == "stub" and self._iniciado:
                try:
                    self._calibrar_stub_tras_degradar()
                except Exception as exc:
                    log.warning("No se pudo recalibrar el modo simple (%s); conservo X_EQ", exc)

    def _forzar_modo(self, modo: str) -> bool:
        """Reconstruye la red en ``modo`` (solo pruebas y diagnóstico, con la simulación parada)."""
        if modo not in _MODOS:
            return False
        with self._lock:
            if modo == "stub":
                self._pasar_a_stub("forzado")
                return True
            if np is None:
                return False
            self._construir_reducido(modo)
            return True

    # -- API pública: eventos --------------------------------------------------------

    def inyectar(self, evento: str, **datos: Any) -> None:
        """Encola un evento sensorial (thread-safe; NUNCA lanza). Los desconocidos se ignoran con log.debug."""
        try:
            if not isinstance(evento, str) or evento not in _EVENTOS:
                log.debug("Evento de conectoma desconocido: %r", str(evento)[:60])
                return
            self._cola.append((evento, dict(datos), self._ahora()))
        except Exception as exc:  # contrato: inyectar nunca lanza
            log.debug("No se pudo encolar el evento %r: %s", str(evento)[:60], exc)

    def _drenar(self) -> list[tuple[str, dict[str, Any], float]]:
        eventos = []
        while True:
            try:
                eventos.append(self._cola.popleft())
            except IndexError:
                return eventos

    def _escala_sensorial(self) -> float:
        return _RECORTE_SENSORIAL_REPOSO if self._modo == "reposo" else 1.0

    def _despertar(self, motivo: str) -> None:
        if self._modo == "reposo":
            self._modo = "vigilia"
            log.info("Conectoma: salgo del modo reposo (%s)", motivo)

    def _abrir_ventana_novedad(self) -> None:
        self._ventanas_novedad.append([500.0, 0.0])
        if len(self._ventanas_novedad) > 8:
            self._ventanas_novedad = self._ventanas_novedad[-8:]
        try:
            registrar = getattr(self.motor_iniciativa, "registrar_estimulo", None)
            if callable(registrar):
                registrar()
        except Exception as exc:
            log.debug("registrar_estimulo del motor falló: %s", exc)

    def _procesar_evento(self, nombre: str, datos: Mapping[str, Any], ts: float) -> None:
        """Aplica un evento (tabla de §10.6 y Anexo A.4) a la red o al modelo simple."""
        # Contabilidad común (presencia, actividad, reposo).
        if nombre == "usuario_escribe":
            self._k_act_hasta = ts + 60.0
            self._ausente_desde = None
            self._despertar("el usuario escribió")
        elif nombre == "rostro" and bool(datos.get("presente", False)):
            self._ausente_desde = None
            self._despertar("rostro detectado")
        elif nombre == "usuario_presencia":
            if bool(datos.get("presente", True)):
                self._ausente_desde = None
            elif self._ausente_desde is None:
                self._ausente_desde = ts
            return
        if nombre in ("usuario_escribe", "ventana_cambia", "busqueda_completa") or (
                nombre == "rostro" and bool(datos.get("presente", False))):
            self._abrir_ventana_novedad()
        if nombre == "ventana_cambia":
            self._rumbo_asignado_col = _h(_texto(datos.get("clase")), _N_COL)
        elif nombre == "nexus_foco":
            self._nexus_enfocado = bool(datos.get("enfocado", False))
        elif nombre == "carga_sistema":
            cpu = _clip(_real(datos.get("cpu"), 0.0), 0.0, 100.0)
            temp = datos.get("temp")
            temp_v = _real(temp, -math.inf) if temp is not None else -math.inf
            self._intero_hz = 2.0 + 38.0 * cpu / 100.0
            self._carga_alta = cpu > 85.0 or temp_v > 80.0

        if self._red is None:
            if self._simple is not None:
                self._simple.evento(nombre, datos, ts)
            return
        red = self._red
        esc = self._escala_sensorial()
        extra_pn = _PN_ACTIVA_HZ - _PN_FONDO_HZ
        if nombre == "usuario_escribe":
            texto = _texto(datos.get("texto"))
            red.programar("pn", patron_pn(texto), extra_pn * esc, min(800.0, 300.0 + 5.0 * len(texto)))
            red.programar("dan", red.dan_pam, 10.0, 500.0)
        elif nombre == "ventana_cambia":
            columna = self._rumbo_asignado_col
            psi = 2.0 * math.pi * columna / _N_COL
            perfil = _LANDMARK_EVENTO_MV * np.maximum(0.0, np.cos(red.phi_er - psi)) ** 2
            red.programar("ext", red.idx_er, perfil * esc, _DURACION_LANDMARK_MS)
            fase = self._lector.phi if self._lector is not None else 0.0
            dif = _envolver(psi - fase)
            if abs(dif) > math.pi / _N_COL:
                pen = red.idx_pen_r if dif > 0 else red.idx_pen_l
                red.programar("ext", pen, (3.0 + 2.0 * min(1.0, abs(dif) / math.pi)) * esc, 150.0)
            titulo = _texto(datos.get("titulo"))
            red.programar("pn", patron_pn(titulo), extra_pn * esc, 500.0)
            self._aplicar_tonicos()
        elif nombre == "carga_sistema":
            self._aplicar_tonicos()
        elif nombre == "rostro":
            if bool(datos.get("presente", False)):
                red.programar("pn", patron_pn("<rostro>"), extra_pn * esc, 500.0)
                red.programar("dan", red.dan_pam, 15.0, 800.0)
                psi = 2.0 * math.pi * _h("usuario", _N_COL) / _N_COL
                perfil = _LANDMARK_EVENTO_MV * np.maximum(0.0, np.cos(red.phi_er - psi)) ** 2
                # Landmark "usuario" y, mientras dura (mismo decaimiento), el tónico cancelado: con el
                # tónico de 8 mV intacto el bump no se movería hacia el usuario.
                tonico = red.ext_tonico[red.idx_er]
                red.programar("ext", red.idx_er, (perfil - tonico) * esc, _DURACION_LANDMARK_MS)
        elif nombre == "busqueda_completa":
            red.programar("pn", patron_pn(_texto(datos.get("consulta"))), extra_pn * esc, 500.0)
            if bool(datos.get("exito", False)):
                red.programar("dan", red.dan_pam, 12.0, 600.0)
            else:
                red.programar("dan", red.dan_ppl1, 8.0, 400.0)
        elif nombre == "error":
            sev = _clip(_real(datos.get("severidad"), 0.5), 0.0, 1.0)
            red.programar("dan", red.dan_ppl1, 5.0 + 10.0 * sev, 300.0 + 500.0 * sev)
        elif nombre == "recompensa":
            mag = _clip(_real(datos.get("magnitud"), 0.5), 0.0, 1.0)
            if mag > 0.0:
                red.programar("dan", red.dan_pam, 15.0 * mag, 1000.0)
        elif nombre == "nexus_foco":
            self._aplicar_tonicos()

    # -- API pública: simulación -------------------------------------------------------

    def avanzar(self, ms: int = 50) -> HomeostaticState:
        """Simula ``ms`` milisegundos y devuelve el nuevo estado (síncrono; solo desde el executor o tests).

        Nunca lanza: ante un fallo interno lo registra y devuelve el último estado válido.
        """
        try:
            ms_ok = int(_clip(_real(ms, 50.0), 1.0, 1000.0))
        except (TypeError, ValueError):
            ms_ok = 50
        try:
            with self._lock:
                t0 = time.perf_counter()
                estado, diag = self._avanzar_interno(ms_ok)
                duracion_ms = (time.perf_counter() - t0) * 1000.0
                self._ms_chunk = duracion_ms if self._ms_chunk <= 0.0 else _ema(self._ms_chunk, duracion_ms, 1.0, 10.0)
                diag["ms_por_chunk"] = round(self._ms_chunk, 3)
                diag["factor_tiempo_real"] = round(self._factor_tr, 4)
                self._estado = estado
                self._diag = diag
                self._fallos = 0
                return estado
        except Exception as exc:
            self._fallos += 1
            ahora = time.monotonic()
            if ahora - self._ultimo_aviso_fallo >= 60.0:
                self._ultimo_aviso_fallo = ahora
                log.warning("Fallo interno del conectoma (%s: %s); conservo el último estado",
                            type(exc).__name__, exc)
                log.debug("Traza del fallo del conectoma", exc_info=True)
            return self._estado

    def _avanzar_interno(self, ms: int) -> tuple[HomeostaticState, dict[str, float]]:
        ahora = self._ahora()
        if self._ultimo_reloj is None:
            dt_wall = 0.0
        else:
            dt_wall = _clip(ahora - self._ultimo_reloj, 0.0, 60.0)
        self._ultimo_reloj = ahora
        for nombre, datos, ts in self._drenar():
            try:
                self._procesar_evento(nombre, datos, ts)
            except Exception as exc:
                log.debug("Evento %s ignorado por error: %s", nombre, exc)

        lect = self._simular_y_leer(ms, dt_wall * self._escala, ahora, calibrando=False)
        dt_s = lect["dt_s"]
        A, Fo, B, F, C, V = lect["A"], lect["Fo"], lect["B"], lect["F"], lect["C"], lect["V"]

        # Modo reposo: entra con fatiga > 0,85 y el usuario ausente > 10 min; sale con P < 0,2.
        ausente_s = (ahora - self._ausente_desde) if self._ausente_desde is not None else 0.0
        if self._modo == "vigilia" and F > 0.85 and ausente_s > _AUSENCIA_REPOSO_S:
            self._modo = "reposo"
            log.info("Conectoma: entro en modo reposo (fatiga %s, ausencia %d min)",
                     formatear_decimal(F, 2), int(ausente_s // 60))
        elif self._modo == "reposo" and self._sueno.p < 0.2:
            self._despertar("presión de sueño baja")

        anomalo = self._t_ms < self._anomalo_hasta_ms or self._factor_tr < 0.2
        d, campo = self.medidor.actualizar(A, Fo, B, F, dt_s, anomalo=anomalo,
                                           semilla_jitter=int(lect["jitter"]))
        campo_antes = self._campo_previo
        self._campo_previo = campo
        iniciativa, motivo, antes, despues = self._evaluar_iniciativa(C, F, B, A, campo_antes, campo)
        if iniciativa == "curiosidad_visual":
            self._saciar_curiosidad()
            C *= 0.3
        if iniciativa is not None:
            log.info("Iniciativa del conectoma: %s (%s)", iniciativa, motivo)
        estado = HomeostaticState(
            arousal=A, focus=Fo, boredom=B, fatiga=F, curiosidad=C, valencia=V,
            divergencia=d, campo=campo, presion_sueno=self._sueno.p, rumbo_deg=lect["rumbo"],
            modo=self._modo, disparar_vision=(iniciativa == "curiosidad_visual"),
            iniciativa=iniciativa, motivo_iniciativa=motivo if iniciativa else "",
            linea_antes=antes if iniciativa == "cambio_linea" else None,
            linea_despues=despues if iniciativa == "cambio_linea" else None,
            factor_tiempo_real=self._factor_tr, fuente=self.fuente_activa, tasas=lect["tasas"],
            bump=tuple(lect["bump"]), ts=ahora,
        )
        diag = {"novedad": round(lect["nu"], 4), "kc_activas_pct": round(lect["kc_pct"], 3),
                "rho": round(lect["rho"], 4), "tasa_mbon_a3": round(lect["a3"], 3)}
        return estado, diag

    def _evaluar_iniciativa(self, c: float, f: float, b: float, a: float, campo_antes: str,
                            campo: str) -> tuple[str | None, str, str | None, str | None]:
        try:
            resultado = self.motor_iniciativa.evaluar(curiosidad=c, fatiga=f, boredom=b, arousal=a,
                                                      modo=self._modo, campo_antes=campo_antes,
                                                      campo_despues=campo)
            iniciativa, motivo, antes, despues = resultado
        except Exception as exc:
            log.debug("El motor de iniciativas falló (%s); sin iniciativa en este chunk", exc)
            return None, "", None, None
        if iniciativa is not None and not isinstance(iniciativa, str):
            iniciativa = None
        return iniciativa, "" if motivo is None else str(motivo), antes, despues

    def _saciar_curiosidad(self) -> None:
        if self._lector is not None and self._red is not None:
            self._lector.C *= 0.3
        elif self._simple is not None:
            self._simple.C *= 0.3

    def _simular_y_leer(self, ms: int, dt_sueno_s: float, ahora: float, *, calibrando: bool) -> dict[str, Any]:
        """Simula un chunk con la fuente activa y devuelve las lecturas (sin medidor ni iniciativas)."""
        c = ciclo_circadiano(self._hora_decimal(ahora))
        reposo = self._modo == "reposo"
        k_act = 1.1 if ahora < self._k_act_hasta else 1.0
        red = self._red
        if red is not None and self._lector is not None:
            lector = self._lector
            red.set_sueno(self._sueno.p, c, reposo)
            pasos = max(1, int(round(ms / red.dt)))
            res = red.simular(pasos)
            self._t_ms += res.ms
            if not res.finito:
                red.reiniciar_dinamica()
                self._anomalo_hasta_ms = self._t_ms + 5000.0
                ahora_m = time.monotonic()
                if ahora_m - self._ultimo_aviso_nan >= 60.0:
                    self._ultimo_aviso_nan = ahora_m
                    log.warning("Conectoma: valores no finitos (NaN/inf) en la membrana; reinicio u y g")
            dt_s = res.ms / 1000.0
            lector.actualizar(res, dt_s, c=c, sueno=self._sueno, dt_sueno_s=dt_sueno_s, reposo=reposo,
                              k_act=k_act)
            if not calibrando:
                self._seguir_novedad(res.ms, lector.r_ema["MBON_a3"], ahora)
            return {
                "dt_s": dt_s, "A": lector.A, "Fo": lector.Fo, "B": lector.B, "F": lector.F, "C": lector.C,
                "V": lector.V, "rumbo": lector.rumbo_deg, "bump": lector.bump, "tasas": lector.tasas_publicas(),
                "jitter": res.crc, "nu": lector.nu, "kc_pct": 100.0 * res.kc_frac, "rho": lector.rho,
                "a3": lector.r_ema["MBON_a3"], "a_media": res.a_media,
            }
        simple = self._simple if self._simple is not None else _ModeloSimple()
        self._simple = simple
        dt_s = ms / 1000.0
        simple.avanzar(dt_s, c=c, sueno=self._sueno, dt_sueno_s=dt_sueno_s, reposo=reposo, k_act=k_act)
        self._t_ms += ms
        return {
            "dt_s": dt_s, "A": simple.A, "Fo": simple.Fo, "B": simple.B, "F": simple.F, "C": simple.C,
            "V": simple.V, "rumbo": simple.rumbo_deg, "bump": simple.bump, "tasas": {},
            "jitter": int(self._ahora() * 1000) & 0x7FFFFFFF, "nu": simple.nu, "kc_pct": 0.0, "rho": 0.0,
            "a3": 0.0, "a_media": 0.0,
        }

    def _seguir_novedad(self, ms: float, r_a3: float, ahora: float) -> None:
        """Registra el pico de MBON-α'3 tras cada estímulo (respuestas de inicio para ``r_novel``)."""
        if not self._ventanas_novedad:
            return
        vivas = []
        for ventana in self._ventanas_novedad:
            ventana[0] -= ms
            ventana[1] = max(ventana[1], r_a3)
            if ventana[0] > 0.0:
                vivas.append(ventana)
            else:
                self._respuestas_novedad.append((ahora, ventana[1]))
        self._ventanas_novedad = vivas
        while self._respuestas_novedad and ahora - self._respuestas_novedad[0][0] > 86400.0:
            self._respuestas_novedad.popleft()
        if len(self._respuestas_novedad) >= 5:
            picos = sorted(p for _, p in self._respuestas_novedad)
            p90 = picos[min(len(picos) - 1, int(math.ceil(0.9 * len(picos))) - 1)]
            self._r_novel = max(_R_NOVEL_MIN, p90)
            if self._lector is not None:
                self._lector.r_novel = self._r_novel

    def estado(self) -> HomeostaticState:
        """Último estado publicado (objeto inmutable): seguro desde cualquier hilo."""
        return self._estado

    def snapshot_hud(self) -> dict[str, Any]:
        """Instantánea para el HUD construida SOLO a partir de ``estado()`` (seguro desde el loop)."""
        e = self._estado
        return {"bump": list(e.bump), "divergencia": e.divergencia_str(), "campo": e.campo,
                "fuente": e.fuente, "tasas": dict(e.tasas)}

    def diagnostico_interno(self) -> dict[str, float]:
        """Copia de ``{"novedad", "kc_activas_pct", "rho", "tasa_mbon_a3", "ms_por_chunk", "factor_tiempo_real"}``."""
        return dict(self._diag)

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico para el arranque y ``sistema.modulo`` (clave ``"conectoma"``)."""
        if self._fallos >= 3:
            return DiagnosticoModulo("conectoma", _NOMBRE_DIAGNOSTICO, EstadoModulo.DEGRADADO,
                                     f"fallos internos repetidos ({self._fallos}); estado congelado")
        if self.fuente_activa == "stub":
            motivo = self._motivo_stub or "sin numpy"
            return DiagnosticoModulo("conectoma", _NOMBRE_DIAGNOSTICO, EstadoModulo.DEGRADADO,
                                     f"modo simple ({motivo})")
        red = self._red
        dt = red.dt if red is not None else 1.0
        n = f"{self.n_neuronas:,}".replace(",", " ")
        detalle = f"{n} neuronas · dt={dt:g} ms"
        if self.modo_degradacion != "normal":
            detalle += f" · modo {self.modo_degradacion}"
            return DiagnosticoModulo("conectoma", _NOMBRE_DIAGNOSTICO, EstadoModulo.DEGRADADO, detalle)
        return DiagnosticoModulo("conectoma", _NOMBRE_DIAGNOSTICO, EstadoModulo.OK, detalle)

    # -- arranque: benchmark + calibración ----------------------------------------------

    async def iniciar(self, *, timeout_s: float = 4.0) -> DiagnosticoModulo:
        """Benchmark de 200 pasos (con degradación en cascada) y calibración de 10 s simulados.

        Corre en el executor propio con tope ``timeout_s``. Mientras tanto ``estado()`` es neutral
        (con la fuente real). Si se agota el tiempo se usan la línea base y el equilibrio guardados
        (o los objetivos de A.3 y ``X_EQ_DEFECTO``). Idempotente. Devuelve ``diagnostico()``.
        """
        if self._cerrado or self._iniciado:
            return self.diagnostico()
        loop = asyncio.get_running_loop()
        tarea = self._tarea_inicio
        if tarea is None or tarea.done() or tarea.get_loop() is not loop:
            tarea = loop.create_task(self._iniciar_async(timeout_s))
            self._tarea_inicio = tarea
        try:
            await asyncio.shield(tarea)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La calibración del conectoma falló (%s); sigo con la línea base guardada", exc)
        return self.diagnostico()

    async def _iniciar_async(self, timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        tope = max(0.1, _real(timeout_s, 4.0))
        plazo = time.monotonic() + tope
        self._abortar_inicio.clear()
        self._estado = HomeostaticState(fuente=self.fuente_activa, presion_sueno=self._sueno.p, ts=self._ahora())
        try:
            fut = loop.run_in_executor(self._ejecutor, self._iniciar_sync, plazo)
            await asyncio.wait_for(asyncio.shield(fut), timeout=tope + 0.5)
        except TimeoutError:
            self._abortar_inicio.set()
            log.info("La calibración del conectoma no terminó en %.1f s; uso la línea base guardada", tope)
        except RuntimeError as exc:  # executor ya apagado
            log.debug("No se pudo calibrar el conectoma: %s", exc)
        finally:
            self._iniciado = True

    def _vencido(self, plazo: float) -> bool:
        return self._abortar_inicio.is_set() or self._cerrado or time.monotonic() > plazo

    def _iniciar_sync(self, plazo: float) -> None:
        """Benchmark y calibración (solo en el executor)."""
        with self._lock:
            try:
                if self._red is not None:
                    self._benchmark(plazo)
                ok = self._calibrar(plazo)
                if not ok:
                    log.info("Calibración del conectoma incompleta; uso la línea base guardada")
                self._publicar_tras_calibrar()
            except Exception as exc:
                log.warning("Fallo durante la calibración del conectoma (%s); sigo sin calibrar", exc)
                log.debug("Traza de la calibración", exc_info=True)
            finally:
                if self._lector is not None:
                    self._lector.historial = None   # el historial solo vive durante la calibración

    def _benchmark(self, plazo: float) -> None:
        """Microbenchmark de 200 pasos de 1 ms; si cada ms simulado cuesta > 300 µs, baja un escalón.

        Una sola muestra confunde un pico pasajero (planificador del SO, GIL disputado con los demás
        diagnósticos del arranque) con una máquina lenta, y la degradación dura toda la sesión (ligero
        además pierde la familiaridad aprendida). Se miden hasta ``_TANDAS_BENCH`` tandas y se baja de
        escalón solo si TODAS superan el umbral (se toma la mejor).
        """
        while self._red is not None and not self._vencido(plazo):
            red = self._red
            red.simular(max(1, int(round(10.0 / red.dt))), plasticidad=False)   # calentamiento
            pasos = max(1, int(round(200.0 / red.dt)))
            por_ms = math.inf
            tandas = 0
            for _ in range(_TANDAS_BENCH):
                t0 = time.perf_counter()
                red.simular(pasos, plasticidad=False)
                por_ms = min(por_ms, (time.perf_counter() - t0) / (pasos * red.dt))
                tandas += 1
                if por_ms <= _UMBRAL_BENCH_S_POR_MS or self._vencido(plazo):
                    break
            log.debug("Benchmark del conectoma (modo %s): %.0f µs por ms simulado", red.modo, por_ms * 1e6)
            if por_ms <= _UMBRAL_BENCH_S_POR_MS or tandas < _TANDAS_BENCH:
                return   # rápido, o plazo agotado antes de confirmar la lentitud (lo vigila latir)
            log.info("Conectoma: %.0f µs por ms simulado en modo %s (> 300 µs); bajo un escalón",
                     por_ms * 1e6, red.modo)
            self._bajar_escalon("rendimiento insuficiente")

    def _calibrar(self, plazo: float) -> bool:
        """Calibración de 10 s simulados (Anexo A.5).

        Primera mitad (5 s): durante ``_CAL_AJUSTE_S`` cada población calibrable adapta su sesgo hacia su
        rango de tasa objetivo (τ ≈ 2 s); después los sesgos quedan congelados y, tras un asentamiento,
        ``r_base`` y ``ā_base`` se fijan con la MEDIA de los últimos ``_CAL_BASE_S`` (no con la EMA de un
        instante) y A se reproduce sobre esa ventana con la base nueva (``fijar_linea_base``). Segunda
        mitad: ``X_EQ`` = media de (A, Fo, B, F), que se pasa al medidor. Devuelve ``False`` si se agotó
        el plazo: entonces vuelve el conjunto guardado COMPLETO (ajustes de sesgo, ``r_base``, ``ā_base``
        y ``X_EQ``), nunca una mezcla de piezas nuevas y viejas.
        """
        chunk = _CALIBRACION_CHUNK_MS
        n_chunks = int(round(_CALIBRACION_S * 1000.0 / chunk))
        mitad = n_chunks // 2
        n_ajuste = min(mitad, int(round(_CAL_AJUSTE_S * 1000.0 / chunk)))
        n_base = max(1, min(mitad - n_ajuste, int(round(_CAL_BASE_S * 1000.0 / chunk))))
        suma = [0.0, 0.0, 0.0, 0.0]
        medidos = 0
        # Fo solo depende del anillo, que la calibración no toca: su componente de X_EQ se promedia desde
        # el fin del ajuste (7,5 s en vez de 5 s), porque sus fluctuaciones lentas hacen poco
        # representativa una ventana de 5 s.
        suma_fo = 0.0
        medidos_fo = 0
        offsets_guardados = dict(self._cal_offsets)
        offsets = dict(offsets_guardados)
        if self._lector is not None and self._red is not None:
            self._lector.historial = []
        for i in range(n_chunks):
            if self._vencido(plazo):
                self._restaurar_calibracion(offsets_guardados)
                return False
            ahora = self._ahora()
            lect = self._simular_y_leer(chunk, 0.0, ahora, calibrando=True)
            lector = self._lector if self._red is not None else None
            if i < mitad:
                if lector is None:
                    continue
                if i < n_ajuste:
                    dt = lect["dt_s"]
                    for clave, tope in _CALIBRABLES.items():
                        bajo, alto = _RANGO_OBJETIVO[clave]
                        r = lector.r_ema[clave]
                        error = bajo - r if r < bajo else alto - r if r > alto else 0.0
                        offsets[clave] = _clip(offsets.get(clave, 0.0) + _GANANCIA_CALIBRACION * error * dt,
                                               -tope, tope)
                    self._cal_offsets = dict(offsets)
                    if self._red is not None:
                        self._red.set_calibracion(self._cal_offsets)
                else:
                    suma_fo += lect["Fo"]
                    medidos_fo += 1
                if i == mitad - 1:
                    # Sesgos congelados desde n_ajuste: línea base = media de la ventana final y A
                    # reproducida con ella (sin saltos de A al empezar la ventana de X_EQ).
                    lector.fijar_linea_base(n_base, reiniciar_F=not self._fatiga_guardada_reciente)
            else:
                suma[0] += lect["A"]
                suma[1] += lect["Fo"]
                suma[2] += lect["B"]
                suma[3] += lect["F"]
                medidos += 1
                suma_fo += lect["Fo"]
                medidos_fo += 1
        if medidos == 0:
            self._restaurar_calibracion(offsets_guardados)
            return False
        suma[1] = suma_fo * medidos / max(1, medidos_fo)
        x_eq = tuple(_clip(s / medidos, 0.0, 1.0) for s in suma)
        self._x_eq = x_eq  # type: ignore[assignment]
        self.medidor.set_equilibrio(x_eq)
        if self._red is not None and self._lector is not None:
            self._r_base = dict(self._lector.r_base)
            self._a_base = self._lector.a_base
        log.info("Conectoma calibrado (%s): X_EQ = (%s)", self.modo_degradacion,
                 ", ".join(formatear_decimal(v, 3) for v in x_eq))
        return True

    def _restaurar_calibracion(self, offsets_guardados: Mapping[str, float]) -> None:
        """Calibración incompleta: vuelve el conjunto guardado completo (sesgos, línea base y X_EQ)."""
        self._cal_offsets = dict(offsets_guardados)
        if self._red is not None:
            self._red.set_calibracion(self._cal_offsets)
        if self._lector is not None:
            self._lector.historial = None
            self._lector.r_base = {k: self._r_base.get(k, _TASAS_OBJETIVO[k]) for k in _CLAVES_TASA}
            self._lector.a_base = self._a_base
        self.medidor.set_equilibrio(self._x_eq)

    def _calibrar_stub_tras_degradar(self) -> None:
        """Nuevo ``X_EQ`` del modo simple al bajar a ``stub`` en ejecución (A.6, en el executor).

        El equilibrio del modelo simple no es el de la red (su Fo tiende a 0,70, su A a 0,40 + 0,2·(1 − c)):
        con el ``X_EQ`` de la red la divergencia quedaría fuera de la banda SG para siempre. Se simulan
        10 s sin entradas (< 50 ms) de una COPIA en reposo, que parte de esos objetivos y de las
        componentes B y F del equilibrio vigente (el aburrimiento y la fatiga siguen midiéndose respecto
        al arranque), y ``X_EQ`` = media de los últimos 5 s. El estado vivo no se toca.
        """
        simple = self._simple
        if simple is None:
            return
        ahora = self._ahora()
        c = ciclo_circadiano(self._hora_decimal(ahora))
        _, _, b_eq, f_eq = self.medidor.x_eq
        copia = _ModeloSimple(A=0.40 + 0.2 * (1.0 - c), Fo=simple.objetivo_fo, B=b_eq, F=f_eq, C=0.0, V=0.0,
                              rumbo_deg=simple.rumbo_deg)
        copia.objetivo_fo = simple.objetivo_fo
        copia.iniciar_F = False
        sueno = _ProcesoSueno(self._sueno.p)
        dt_s = _CALIBRACION_CHUNK_MS / 1000.0
        n_chunks = int(round(_CALIBRACION_S / dt_s))
        suma = [0.0, 0.0, 0.0, 0.0]
        medidos = 0
        for i in range(n_chunks):
            copia.avanzar(dt_s, c=c, sueno=sueno, dt_sueno_s=0.0, reposo=False, k_act=1.0)
            if i >= n_chunks // 2:
                for j, v in enumerate((copia.A, copia.Fo, copia.B, copia.F)):
                    suma[j] += v
                medidos += 1
        x_eq = tuple(_clip(s / max(1, medidos), 0.0, 1.0) for s in suma)
        self._x_eq = x_eq  # type: ignore[assignment]
        self.medidor.set_equilibrio(x_eq)
        log.info("Conectoma: nuevo equilibrio del modo simple X_EQ = (%s)",
                 ", ".join(formatear_decimal(v, 3) for v in x_eq))

    def _publicar_tras_calibrar(self) -> None:
        """Publica un estado real al terminar la calibración (sin avanzar el tiempo del medidor)."""
        if self._red is not None and self._lector is not None:
            m = self._lector
            vals = (m.A, m.Fo, m.B, m.F, m.C, m.V, m.rumbo_deg, m.bump, m.tasas_publicas())
        else:
            s = self._simple if self._simple is not None else _ModeloSimple()
            vals = (s.A, s.Fo, s.B, s.F, s.C, s.V, s.rumbo_deg, s.bump, {})
        A, Fo, B, F, C, V, rumbo, bump, tasas = vals
        d, campo = self.medidor.actualizar(A, Fo, B, F, 0.0, anomalo=False, semilla_jitter=1000)
        self._campo_previo = campo
        self._estado = HomeostaticState(
            arousal=A, focus=Fo, boredom=B, fatiga=F, curiosidad=C, valencia=V, divergencia=d, campo=campo,
            presion_sueno=self._sueno.p, rumbo_deg=rumbo, modo=self._modo, factor_tiempo_real=self._factor_tr,
            fuente=self.fuente_activa, tasas=tasas, bump=tuple(bump), ts=self._ahora())

    # -- bucle homeostático ---------------------------------------------------------------

    async def latir(self, *, periodo_ms: int = 50, publicar: Callable[[HomeostaticState], None] | None = None,
                    parar: asyncio.Event | None = None, publicar_cada_ms: int = 500) -> None:
        """Bucle homeostático (§6.2): avanza un chunk por periodo con dilatación temporal y publica.

        Publica cada ``publicar_cada_ms`` y de inmediato ante una iniciativa o un cambio de campo
        confirmado. Si ``factor_tiempo_real`` < 0,8 durante 30 s seguidos baja un escalón de la
        cascada. Guarda el estado cada 5 min y al terminar, siempre en el executor propio.
        """
        if self._cerrado:
            return
        if not self._iniciado:
            await self.iniciar()
        loop = asyncio.get_running_loop()
        periodo = int(_clip(_real(periodo_ms, 50.0), 1.0, 1000.0))
        periodo_s = periodo / 1000.0
        cada_s = max(0.0, _real(publicar_cada_ms, 500.0)) / 1000.0
        if parar is None:
            parar = asyncio.Event()
        ultimo_pub = -math.inf
        campo_pub: str | None = None
        ultimo_guardado = time.monotonic()
        lento_desde: float | None = None
        proximo = time.monotonic()
        t_prev: float | None = None
        cancelado = False
        self._guardado_final = False
        try:
            while not parar.is_set() and not self._cerrado:
                inicio = time.monotonic()
                if t_prev is not None:
                    real = inicio - t_prev
                    if real > 0.0:
                        razon = periodo_s / real
                        self._factor_tr = _clip(_ema(self._factor_tr, razon, real, _TAU_FACTOR_TR_S), 0.0, 10.0)
                t_prev = inicio
                try:
                    estado = await loop.run_in_executor(self._ejecutor, self.avanzar, periodo)
                except RuntimeError as exc:  # executor apagado (cerrar concurrente)
                    log.debug("El bucle del conectoma se detiene: %s", exc)
                    break
                ahora = time.monotonic()
                if publicar is not None and (ahora - ultimo_pub >= cada_s or estado.iniciativa is not None
                                             or estado.campo != campo_pub):
                    try:
                        publicar(estado)
                    except Exception as exc:
                        log.warning("El callback de publicación del conectoma falló: %s", exc)
                    ultimo_pub = ahora
                    campo_pub = estado.campo
                if self._factor_tr < _FACTOR_LENTO and self.modo_degradacion != "stub":
                    if lento_desde is None:
                        lento_desde = ahora
                    elif ahora - lento_desde >= _T_LENTO_S:
                        log.warning("Conectoma: factor de tiempo real %s durante 30 s",
                                    formatear_decimal(self._factor_tr, 2))
                        try:
                            await loop.run_in_executor(self._ejecutor, self._bajar_escalon,
                                                       "tiempo real insuficiente")
                        except RuntimeError as exc:
                            log.debug("No se pudo bajar de escalón: %s", exc)
                            break
                        lento_desde = None
                        self._factor_tr = 1.0
                else:
                    lento_desde = None
                if ahora - ultimo_guardado >= _GUARDADO_CADA_S:
                    ultimo_guardado = ahora
                    try:
                        await loop.run_in_executor(self._ejecutor, self.guardar_estado)
                    except RuntimeError as exc:
                        log.debug("Guardado periódico omitido: %s", exc)
                proximo += periodo_s
                ahora = time.monotonic()
                if proximo < ahora:
                    proximo = ahora   # dilatación temporal: no se recupera el atraso
                espera = proximo - ahora
                if espera > 0.0:
                    try:
                        await asyncio.wait_for(parar.wait(), timeout=espera)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            cancelado = True
            raise
        finally:
            if not cancelado and not self._cerrado:
                try:
                    await loop.run_in_executor(self._ejecutor, self.guardar_estado)
                    self._guardado_final = True
                except Exception as exc:
                    log.debug("Guardado final del bucle omitido: %s", exc)

    # -- persistencia ---------------------------------------------------------------------

    def _leer_json(self) -> dict[str, Any] | None:
        ruta = self._ruta_estado
        if ruta is None:
            return None
        try:
            texto = ruta.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("No se pudo leer el estado del conectoma: %s", exc)
            return None
        try:
            datos = json.loads(texto)
        except ValueError:
            log.warning("El estado del conectoma está corrupto; lo ignoro")
            return None
        if not isinstance(datos, dict):
            log.warning("El estado del conectoma no tiene el formato esperado; lo ignoro")
            return None
        return datos

    def cargar_estado(self) -> bool:
        """Carga ``connectome_estado.json`` (y los pesos plásticos si la red coincide). Nunca lanza."""
        try:
            with self._lock:
                datos = self._leer_json()
                if datos is None:
                    return False
                self._aplicar_estado(datos)
                return True
        except Exception as exc:
            log.warning("Estado del conectoma inválido (%s); lo ignoro", exc)
            return False

    def _aplicar_estado(self, datos: Mapping[str, Any]) -> None:
        ahora = self._ahora()
        ts = _real(datos.get("ts"), ahora)
        t_off = max(0.0, ahora - ts)
        p = _clip(_real(datos.get("presion_sueno"), 0.30), 0.0, 1.0)
        self._sueno.p = _clip(p * math.exp(-t_off * self._escala / _TAU_S_SUENO_S), 0.0, 1.0)
        b = 0.5 * _clip(_real(datos.get("boredom"), 0.20), 0.0, 1.0)
        f = _clip(_real(datos.get("fatiga"), 0.25), 0.0, 1.0)
        f_reciente = t_off <= 300.0
        self._fatiga_guardada_reciente = f_reciente
        self._r_novel = max(_R_NOVEL_MIN, _real(datos.get("r_novel"), _R_NOVEL_DEFECTO))
        rumbo = _real(datos.get("rumbo_deg"), 0.0) % 360.0
        self._rumbo_asignado_col = int(round(rumbo / (360.0 / _N_COL))) % _N_COL
        x_eq = datos.get("x_eq")
        if isinstance(x_eq, (list, tuple)) and len(x_eq) == 4:
            valores = tuple(_real(v, math.nan) for v in x_eq)
            if all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in valores):
                self._x_eq = valores  # type: ignore[assignment]
                self.medidor.set_equilibrio(valores)  # type: ignore[arg-type]
        r_base = datos.get("r_base")
        if isinstance(r_base, Mapping):
            for k in _CLAVES_TASA:
                v = _real(r_base.get(k), math.nan)
                if math.isfinite(v) and v >= 0.0:
                    self._r_base[k] = v
        self._a_base = max(1e-3, _real(datos.get("a_base"), self._a_base))
        ajuste = datos.get("ajuste_sesgo")
        if isinstance(ajuste, Mapping):
            self._cal_offsets = {k: _clip(_real(ajuste.get(k), 0.0), -tope, tope)
                                 for k, tope in _CALIBRABLES.items() if k in ajuste}
        red_guardada = datos.get("red")
        self._red_guardada = red_guardada if isinstance(red_guardada, Mapping) else None

        if self._lector is not None:
            lector = self._lector
            lector.B = b
            if f_reciente:
                lector.F = f
                lector.iniciar_F = False
            lector.r_base = {k: self._r_base.get(k, _TASAS_OBJETIVO[k]) for k in _CLAVES_TASA}
            lector.r_ema = dict(lector.r_base)
            lector.a_base = self._a_base
            lector.r_novel = self._r_novel
        if self._simple is not None:
            self._simple.B = b
            if f_reciente:
                self._simple.F = f
                self._simple.iniciar_F = False
            self._simple.rumbo_deg = self._rumbo_asignado_col * 360.0 / _N_COL
            self._simple.bump = self._simple._calcular_bump()
        if self._red is not None:
            self._aplicar_tonicos()
            self._cargar_pesos(t_off)

    def _red_coincide(self, guardada: Mapping[str, Any] | None) -> bool:
        red = self._red
        if red is None or guardada is None:
            return False
        actual = red.firma()
        return all(guardada.get(k) == actual[k] for k in ("version_red", "semilla_red", "n_kc", "n_pn", "k_pn"))

    def _cargar_pesos(self, t_off: float) -> None:
        red = self._red
        if red is None or np is None or self._ruta_npz is None:
            return
        if not self._red_coincide(self._red_guardada):
            red.reiniciar_pesos()
            if self._ruta_npz.exists():
                log.info("Pesos plásticos de otra red; empiezo con W0")
            return
        try:
            with np.load(self._ruta_npz, allow_pickle=False) as z:
                pesos = {k: z[k] for k in ("W_a3", "W_avd", "W_app") if k in z.files}
        except FileNotFoundError:
            log.debug("No hay pesos plásticos guardados; empiezo con W0")
            red.reiniciar_pesos()
            return
        except Exception as exc:
            log.warning("No se pudieron leer los pesos plásticos (%s); empiezo con W0", exc)
            red.reiniciar_pesos()
            return
        factor = math.exp(-t_off / (_TAU_RECUPERACION_MS / 1000.0))
        if not red.cargar_pesos(pesos, factor):
            red.reiniciar_pesos()
            log.info("Pesos plásticos de otra red; empiezo con W0")

    def _volcar_estado(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if self._red is not None and self._lector is not None:
            b, f = self._lector.B, self._lector.F
        elif self._simple is not None:
            b, f = self._simple.B, self._simple.F
        else:
            b, f = 0.20, 0.25
        red = self._red
        datos = {
            "version": _VERSION_ESTADO,
            "ts": round(self._ahora(), 3),
            "presion_sueno": round(self._sueno.p, 6),
            "boredom": round(b, 6),
            "fatiga": round(f, 6),
            "r_novel": round(self._r_novel, 3),
            "rumbo_deg": round(self._rumbo_asignado_col * 360.0 / _N_COL, 3),
            "x_eq": [round(v, 6) for v in self.medidor.x_eq],
            "r_base": {k: round(v, 4) for k, v in self._r_base.items()},
            "a_base": round(self._a_base, 6),
            "ajuste_sesgo": {k: round(v, 4) for k, v in self._cal_offsets.items()},
            "red": {
                "version_red": VERSION_RED, "semilla_red": self._semilla_red,
                "n_kc": red.n_kc if red is not None else 0, "n_pn": _N_PN_TEXTO, "k_pn": _K_PN,
                "modo": self.modo_degradacion,
            },
        }
        pesos = red.pesos_plasticos() if red is not None else None
        return datos, pesos

    def guardar_estado(self) -> None:
        """Guarda el estado JSON (atómico, 0o600) y los pesos ``.npz`` (solo desde el executor o en parada).

        Nunca lanza: los errores de E/S se registran con ``log.warning``.
        """
        if self._ruta_estado is None:
            log.debug("Conectoma sin ruta de estado: no se guarda")
            return
        try:
            with self._lock:
                datos, pesos = self._volcar_estado()
            contenido = json.dumps(datos, ensure_ascii=False, allow_nan=False, indent=1).encode("utf-8")
            _escribir_atomico(self._ruta_estado, contenido)
            if pesos is not None and np is not None and self._ruta_npz is not None:
                import io
                memoria = io.BytesIO()
                np.savez(memoria, **pesos)
                _escribir_atomico(self._ruta_npz, memoria.getvalue())
            log.debug("Estado del conectoma guardado")
        except Exception as exc:
            log.warning("No se pudo guardar el estado del conectoma: %s", exc)

    # -- cierre -----------------------------------------------------------------------------

    async def cerrar(self, timeout: float = 2.0) -> None:
        """Guarda el estado final en el executor (si ``latir`` no lo hizo, con tope ``timeout``) y lo apaga.

        Idempotente: una segunda llamada no hace nada.
        """
        if self._cerrado:
            return
        self._cerrado = True
        self._abortar_inicio.set()
        if not self._guardado_final:
            try:
                loop = asyncio.get_running_loop()
                fut = loop.run_in_executor(self._ejecutor, self.guardar_estado)
                await asyncio.wait_for(fut, timeout=max(0.1, _real(timeout, 2.0)))
                self._guardado_final = True
            except TimeoutError:
                log.warning("El guardado final del conectoma superó %.1f s; lo abandono", _real(timeout, 2.0))
            except Exception as exc:
                log.debug("Guardado final del conectoma omitido: %s", exc)
        try:
            self._ejecutor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            log.debug("Error al apagar el executor del conectoma: %s", exc)
