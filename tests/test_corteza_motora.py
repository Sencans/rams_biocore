"""Tests de lobes/corteza_motora.py (autonomía corporal del avatar 3D, sin LLM).

Sin red, sin visor real y sin mover el avatar: el avatar es un objeto falso que guarda lo que se le
encola, y el tiempo es un reloj falso que el test avanza a mano. Las corrutinas se ejecutan con
``asyncio.run`` (fixture ``correr``).
"""

from __future__ import annotations

import asyncio
import io
import math
import random
from collections import Counter
from dataclasses import replace
from typing import Any

import pytest

import lobes.corteza_motora as cm
from core.contracts import DiagnosticoModulo, EstadoModulo, HomeostaticState
from lobes.corteza_motora import (
    ContextoMotor,
    CortezaMotora,
    MemoriaMotora,
    Plan,
    rumbo_del_bump,
    sanear_comando,
    tramo_en_escenario,
)
from lobes.vtuber_ai_bridge import sanear_desktop

HUESOS = frozenset({"hips", "spine", "chest", "upperChest", "neck", "head", "leftShoulder", "rightShoulder",
                    "leftUpperArm", "rightUpperArm", "leftLowerArm", "rightLowerArm", "leftHand", "rightHand",
                    "leftUpperLeg", "rightUpperLeg"})
EXPRESIONES = frozenset({"aa", "ih", "ou", "ee", "oh", "blink", "happy", "angry", "sad", "relaxed", "surprised"})
CTX = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES)

ABURRIDA = HomeostaticState(arousal=0.5, focus=0.3, boredom=0.9, fatiga=0.2, curiosidad=0.2, valencia=0.0)
FATIGADA = HomeostaticState(arousal=0.3, focus=0.4, boredom=0.3, fatiga=0.9, curiosidad=0.05)
FRESCA = HomeostaticState(arousal=0.7, focus=0.5, boredom=0.3, fatiga=0.1, curiosidad=0.2)
FOCO = HomeostaticState(arousal=0.5, focus=0.92, boredom=0.1, fatiga=0.2, curiosidad=0.1)
CURIOSA = HomeostaticState(arousal=0.6, focus=0.4, boredom=0.4, fatiga=0.2, curiosidad=0.9, valencia=0.1)
ALEGRE = HomeostaticState(arousal=0.85, focus=0.5, boredom=0.1, fatiga=0.1, curiosidad=0.2, valencia=0.8)
EUFORICA = HomeostaticState(arousal=1.0, focus=0.3, boredom=0.8, fatiga=0.0, curiosidad=0.8, valencia=0.6)


class Reloj:
    """Reloj monotónico falso."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> None:
        self.t += segundos


class AvatarFalso:
    """Imita la API de VTuberAIBridge que usa la corteza (sin red)."""

    def __init__(self, reloj: Reloj | None = None, *, en_linea: bool = True,
                 huesos: frozenset[str] | None = HUESOS, expresiones: frozenset[str] | None = EXPRESIONES) -> None:
        self.reloj = reloj or Reloj()
        self.en_linea = en_linea
        self.hablando = False
        self.huesos_disponibles = huesos
        self.expresiones_disponibles = expresiones
        self.reposo_externo = False
        self.envios: list[tuple[float, Any]] = []

    def encolar(self, comando: Any) -> None:
        self.envios.append((self.reloj(), comando))

    def comandos(self) -> list[dict[str, Any]]:
        salida: list[dict[str, Any]] = []
        for _t, envio in self.envios:
            salida.extend(envio if isinstance(envio, list) else [envio])
        return salida

    def limpiar(self) -> None:
        self.envios.clear()


# Huesos de las poses del visor (lib/protocol.js; solo los que importan aquí). Los que no aparecen vuelven a 0.
POSES_VISOR_FALSAS: dict[str, dict[str, dict[str, float]]] = {
    "relaxed": {"leftUpperArm": {"z": -72}, "rightUpperArm": {"z": 72}, "leftLowerArm": {"y": -14},
                "rightLowerArm": {"y": 14}, "leftHand": {"z": -6}, "rightHand": {"z": 6}},
    "arms_up": {"leftUpperArm": {"z": 62}, "rightUpperArm": {"z": -62}, "head": {"x": -8}},
    "hands_on_hips": {"leftUpperArm": {"z": -48}, "rightUpperArm": {"z": 48}, "spine": {"z": 2}},
    "arms_crossed": {"leftUpperArm": {"z": -62}, "rightUpperArm": {"z": 62}, "head": {"x": -4, "z": 3}},
    "thinking": {"rightLowerArm": {"y": 140}, "head": {"x": 4, "z": -7}},
    "shy": {"head": {"x": 12, "z": -8}, "spine": {"x": 4}},
}
VISEMAS = {"aa", "ih", "ou", "ee", "oh"}


class VisorLogico:
    """Estado lógico del visor (misma semántica que public/app.js para lo que usa la corteza).

    ``pose`` reinicia los huesos tocados; ``bones`` mezcla ejes y deja la pose en ``custom``;
    ``expression`` exclusiva limpia emociones pero **no** visemas ni ``blink``; ``stop gestures``
    corta los gestos en curso; ``move`` solo con ``rotation`` detiene el paseo.
    """

    def __init__(self) -> None:
        self.pose = "relaxed"
        self.huesos: dict[str, dict[str, float]] = {}
        self.mirada = "camera"
        self.expr: dict[str, float] = {}
        self.idle = 1.0
        self.rot = 0.0
        self.paseo_hasta = -math.inf                # fin del paseo en curso
        self.gestos: dict[str, float] = {}          # gesto → fin
        self.aplicar({"type": "pose", "name": "relaxed"}, 0.0)

    def aplicar(self, c: dict[str, Any], t: float) -> None:
        tipo = c["type"]
        if tipo == "pose":
            base = POSES_VISOR_FALSAS[c["name"]]
            for hueso in set(self.huesos) | set(base):
                self.huesos[hueso] = {"x": 0.0, "y": 0.0, "z": 0.0, **base.get(hueso, {})}
            self.pose = c["name"]
        elif tipo == "bones":
            for hueso, rot in c["rotations"].items():
                self.huesos.setdefault(hueso, {"x": 0.0, "y": 0.0, "z": 0.0}).update(rot)
            self.pose = "custom"
        elif tipo == "look":
            self.mirada = c["at"]
        elif tipo == "expression":
            exclusiva = c.get("exclusive") is not False
            if exclusiva:
                self.expr = {n: v for n, v in self.expr.items() if n in VISEMAS or n.startswith("blink")}
            if "values" in c:
                self.expr.update(c["values"])
            elif c.get("name") != "neutral":
                self.expr[c["name"]] = c.get("value", 1.0)
        elif tipo == "idle":
            self.idle = c.get("intensity", self.idle)
        elif tipo == "move":
            self.rot = c.get("rotation", self.rot)
            if c.get("walk") and ("x" in c or "z" in c):
                self.paseo_hasta = t + c.get("duration", 1.0)
            else:                                   # solo giro: se detiene donde está
                self.paseo_hasta = min(self.paseo_hasta, t)
        elif tipo == "gesture":
            self.gestos[c["name"]] = t + cm.DURACION_GESTO.get(c["name"], 1.5)
        elif tipo == "stop" and c.get("what") in ("gestures", "all"):
            self.gestos.clear()

    def camina(self, t: float) -> bool:
        return t < self.paseo_hasta

    def gestos_en_curso(self, t: float) -> set[str]:
        return {nombre for nombre, fin in self.gestos.items() if fin > t}

    def huesos_en_reposo(self) -> bool:
        """``True`` si todos los huesos están como en la pose ``relaxed`` (sin micro-postura ni brazo)."""
        base = POSES_VISOR_FALSAS["relaxed"]
        return all(abs(rot[eje] - base.get(hueso, {}).get(eje, 0.0)) < 1e-6
                   for hueso, rot in self.huesos.items() for eje in "xyz")


class AvatarConVisor(AvatarFalso):
    """Avatar falso que además aplica cada comando a un ``VisorLogico``."""

    def __init__(self, reloj: Reloj | None = None, **kwargs: Any) -> None:
        super().__init__(reloj, **kwargs)
        self.visor = VisorLogico()

    def encolar(self, comando: Any) -> None:
        super().encolar(comando)
        for c in comando if isinstance(comando, list) else [comando]:
            self.visor.aplicar(c, self.reloj())


def _corteza(settings: Any, *, bus: Any = None, avatar: Any = None, semilla: int = 7,
             reloj: Reloj | None = None, **ajustes: Any) -> CortezaMotora:
    s = settings.con(**ajustes) if ajustes else settings
    reloj = reloj or Reloj()
    return CortezaMotora(s, bus, avatar if avatar is not None else AvatarFalso(reloj), semilla=semilla, reloj=reloj)


def _primeras_decisiones(settings: Any, estado: HomeostaticState, ctx: ContextoMotor = CTX,
                         n: int = 150, **ajustes: Any) -> Counter[str | None]:
    """Primera conducta elegida por ``n`` cortezas con semillas distintas (Monte Carlo determinista)."""
    cuenta: Counter[str | None] = Counter()
    for semilla in range(n):
        plan = _corteza(settings, semilla=semilla, **ajustes).decidir(estado, 100.0, ctx).plan
        cuenta[plan.conducta if plan else None] += 1
    return cuenta


def _fraccion(cuenta: Counter[str | None], nombres: set[str]) -> float:
    return sum(v for k, v in cuenta.items() if k in nombres) / max(1, sum(cuenta.values()))


def _todos_los_planes(settings: Any, **ajustes: Any) -> list[Plan]:
    """Muchos planes de todas las conductas, estados, semillas y posiciones extremas."""
    planes: list[Plan] = []
    estados = (ABURRIDA, FATIGADA, FOCO, CURIOSA, ALEGRE, EUFORICA, HomeostaticState(valencia=-0.8, arousal=0.9),
               HomeostaticState(modo="reposo", fatiga=0.95))
    posiciones = ((0.0, 0.0), (1.6, 0.6), (-1.6, -1.0), (0.9, -0.7))
    for semilla in range(6):
        corteza = _corteza(settings, semilla=semilla, **ajustes)
        for estado in estados:
            for pos in posiciones:
                ctx = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES, posicion=pos, rotacion=30.0)
                for conducta in cm.CONDUCTAS:
                    plan = corteza.construir(conducta, estado, ctx)
                    if plan is not None:
                        planes.append(plan)
                for plan in (corteza.plan_atencion(estado, ctx), corteza.plan_reading_steiner(estado, ctx)):
                    if plan is not None:
                        planes.append(plan)
    return planes


# ---------------------------------------------------------------------------
# Brújula: rumbo del bump E-PG
# ---------------------------------------------------------------------------


def _bump_pico(columna: int, n: int = 16) -> list[float]:
    return [1.0 if k == columna else 0.0 for k in range(n)]


def test_rumbo_del_bump_vector_poblacional() -> None:
    assert rumbo_del_bump(_bump_pico(4)).angulo == pytest.approx(90.0)
    assert rumbo_del_bump(_bump_pico(12)).angulo == pytest.approx(270.0)
    assert rumbo_del_bump(_bump_pico(0)).coherencia == pytest.approx(1.0)
    # Bump con forma de campana alrededor de la columna 8 (180°): coherente, sin ser un delta.
    campana = [math.exp(-((k - 8) ** 2) / 4.0) for k in range(16)]
    rumbo = rumbo_del_bump(campana)
    assert rumbo.angulo == pytest.approx(180.0, abs=1e-6) and 0.5 < rumbo.coherencia < 1.0
    # Plano o vacío: sin dirección fiable.
    assert rumbo_del_bump([0.5] * 16).coherencia == pytest.approx(0.0, abs=1e-9)
    assert rumbo_del_bump([], defecto_deg=45.0) == cm.Rumbo(45.0, 0.0)
    assert rumbo_del_bump(["x", None, float("nan")]).coherencia == 0.0


def _media_circular(angulos: list[float]) -> tuple[float, float]:
    """(ángulo medio en grados 0..360, concentración 0..1)."""
    x = sum(math.cos(math.radians(a)) for a in angulos)
    y = sum(math.sin(math.radians(a)) for a in angulos)
    return math.degrees(math.atan2(y, x)) % 360.0, math.hypot(x, y) / len(angulos)


def test_brujula_rumbo_relativo_por_integracion_de_trayectoria(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    calma = {"boredom": 0.0, "curiosidad": 0.0}

    def angulos(estado: HomeostaticState, memoria: MemoriaMotora, n: int = 300) -> list[float]:
        return [corteza.brujula(estado, memoria, random.Random(i)).angulo for i in range(n)]

    # Sin rumbo recordado parte de la fase del bump.
    media, _ = _media_circular(angulos(HomeostaticState(bump=tuple(_bump_pico(4)), **calma), MemoriaMotora()))
    assert media == pytest.approx(90.0, abs=4.0)
    # El bump gira 90° (de 180° a 270°) desde que fijó un rumbo de 200°: ella gira 90° (≈ 290°, apenas
    # atraída hacia la fase 270°). Un rumbo absoluto daría 270°.
    memoria = MemoriaMotora(rumbo_marcha=200.0, bump_ref=180.0)
    media, _ = _media_circular(angulos(HomeostaticState(bump=tuple(_bump_pico(12)), **calma), memoria))
    assert 280.0 < media < 292.0
    # Bump quieto (como el del conectoma en Windows): conserva su rumbo, con una atracción suave a la fase.
    memoria = MemoriaMotora(rumbo_marcha=120.0, bump_ref=0.0)
    media, _ = _media_circular(angulos(HomeostaticState(bump=tuple(_bump_pico(0)), **calma), memoria))
    assert 88.0 < media < 104.0
    # El ruido de exploración crece con el aburrimiento y la curiosidad.
    _m, quieta = _media_circular(angulos(HomeostaticState(bump=tuple(_bump_pico(0)), **calma), memoria))
    _m, inquieta = _media_circular(angulos(HomeostaticState(bump=tuple(_bump_pico(0)), boredom=0.9,
                                                            curiosidad=0.8), memoria))
    assert inquieta < quieta - 0.3
    assert corteza.memoria == MemoriaMotora(semilla=7)            # proponer no cambia la memoria


def test_tramo_en_escenario_parte_de_donde_esta_y_rebota() -> None:
    full = cm.limites_escenario("full")
    assert full == (1.0, -0.7, 0.4)
    destino, rumbo = tramo_en_escenario((0.0, 0.0), 90.0, 0.8, full)
    assert destino == pytest.approx((0.8, 0.0), abs=1e-6) and rumbo == pytest.approx(90.0)
    # Hay sitio para más de medio paseo: camina hasta el borde (con margen).
    destino, rumbo = tramo_en_escenario((0.0, 0.0), 90.0, 1.3, full)
    assert destino == pytest.approx((0.95, 0.0), abs=1e-6) and rumbo == pytest.approx(90.0)
    # Pegada al lateral derecho del espectador: rebota hacia la izquierda.
    destino, rumbo = tramo_en_escenario((0.9, 0.0), 90.0, 0.8, full)
    assert destino == pytest.approx((0.1, 0.0), abs=1e-6) and rumbo == pytest.approx(270.0)
    # Pegada al frente mirando a cámara: rebota hacia el fondo.
    destino, rumbo = tramo_en_escenario((0.0, 0.3), 0.0, 0.8, full)
    assert destino == pytest.approx((0.0, -0.5), abs=1e-6) and rumbo == pytest.approx(180.0)
    # En la esquina: rebota en las dos paredes.
    _destino, rumbo = tramo_en_escenario((0.9, 0.3), 45.0, 0.8, full)
    assert rumbo == pytest.approx(225.0)
    # Sin sitio en ninguna dirección: no hay tramo.
    assert tramo_en_escenario((0.0, 0.0), 0.0, 0.8, (0.2, -0.2, 0.2)) is None
    # Siempre dentro del escenario, desde cualquier sitio y rumbo.
    for x0 in (-1.0, -0.5, 0.0, 0.7, 1.0):
        for z0 in (-0.7, 0.0, 0.4):
            for rumbo in range(0, 360, 15):
                tramo = tramo_en_escenario((x0, z0), float(rumbo), 1.3, full)
                assert tramo is not None
                (x, z), _r = tramo
                assert abs(x) <= 0.95 + 1e-9 and -0.65 - 1e-9 <= z <= 0.35 + 1e-9
                assert math.hypot(x - cm._acotar(x0, -0.95, 0.95), z - cm._acotar(z0, -0.65, 0.35)) >= 0.35 - 1e-9


def test_el_rumbo_de_marcha_decide_hacia_donde_camina_y_gira(settings_prueba: Any) -> None:
    # El bump gira 90° desde el último rumbo (0°): ella camina hacia la derecha del espectador, desde donde está.
    memoria = MemoriaMotora(rumbo_marcha=0.0, bump_ref=0.0)
    estado = HomeostaticState(boredom=0.1, curiosidad=0.1, bump=tuple(_bump_pico(4)))
    origen = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES, posicion=(-0.6, -0.2))
    xs = []
    for semilla in range(20):
        corteza = _corteza(settings_prueba, semilla=semilla)
        plan = corteza.construir("pasear", estado, origen, brujula=corteza.brujula(estado, memoria))
        assert plan is not None and plan.rumbo is not None
        camino = next(c for c in plan.comandos() if c["type"] == "move" and c.get("walk"))
        giro = next(c for c in plan.comandos() if c["type"] == "move" and "x" not in c)
        assert math.hypot(camino["x"] + 0.6, camino["z"] + 0.2) >= cm.DIST_MIN_PASEO
        assert giro["rotation"] == pytest.approx(cm.envolver_180(plan.rumbo), abs=0.2)
        xs.append(camino["x"] + 0.6)
    assert sum(1 for dx in xs if dx > 0.3) >= 17
    # Al ejecutarlo, ese rumbo queda como rumbo de marcha recordado (con la fase del bump de referencia).
    reloj = Reloj()
    corteza = _corteza(settings_prueba, reloj=reloj)
    corteza._estado = estado
    plan = corteza.construir("pasear", estado, CTX)
    assert plan is not None and corteza.ejecutar(plan) > 0
    assert corteza.memoria.rumbo_marcha == plan.rumbo and corteza.memoria.bump_ref == pytest.approx(90.0)
    # Sin caminar, el rumbo solo la gira (siempre de frente al público, ±35°).
    quieta = _corteza(settings_prueba, avatar_caminar=False)
    plan = quieta.construir("girar", HomeostaticState(bump=tuple(_bump_pico(4))), CTX)
    assert plan is not None
    giros = [c["rotation"] for c in plan.comandos() if c["type"] == "move"]
    assert giros[0] > 0 and all(abs(g) <= cm.GIRO_MAX_QUIETA for g in giros) and giros[-1] == 0.0
    extremos = []
    for semilla in range(40):
        plan = _corteza(settings_prueba, semilla=semilla, avatar_caminar=False).construir(
            "girar", HomeostaticState(boredom=0.6, bump=tuple(_bump_pico(0))), CTX)
        if plan is not None:
            extremos.append(plan.comandos()[1]["rotation"])
    assert min(extremos) < -20.0 and max(extremos) > 20.0           # con el bump quieto gira a los dos lados


@pytest.mark.parametrize("fuente", ["stub", "reducido"])
def test_camina_libremente_con_el_bump_real_del_conectoma(settings_prueba: Any, fuente: str) -> None:
    """Con el bump del conectoma de verdad (sin Hyprland ni eventos: casi quieto en ≈ 0°) recorre el
    escenario: varias caminatas a destinos distintos en 30 minutos simulados (no un péndulo)."""
    if fuente == "reducido":
        pytest.importorskip("numpy")
    from datetime import datetime

    from core.config import zona_local
    from core.sincan_connectome import SincanConnectome

    # El proceso C (circadiano) depende de la hora local: a las 4 de la mañana la fatiga la deja casi
    # quieta. Reloj de pared fijo a media tarde para que el test no dependa de cuándo se ejecuta.
    tarde = datetime(2026, 10, 14, 15, 0, tzinfo=zona_local(settings_prueba)).timestamp()
    conectoma = SincanConnectome(settings_prueba, fuente=fuente, semilla=7, semilla_red=7, reloj=lambda: tarde)
    try:
        estados = [conectoma.avanzar(500) for _ in range(20)]
    finally:
        asyncio.run(conectoma.cerrar())
    assert all(len(e.bump) == 16 for e in estados)
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, avatar=avatar, reloj=reloj, semilla=1)
    for i in range(int(30 * 60 / cm.TICK_S)):
        reloj.avanzar(cm.TICK_S)
        corteza._estado = estados[(i // 2) % len(estados)]            # homeostasis.estado a 2 Hz
        corteza.tick()
    destinos = [(c["x"], c["z"]) for c in avatar.comandos() if c["type"] == "move" and c.get("walk")]
    assert len(destinos) >= 4
    assert len({(round(x, 1), round(z, 1)) for x, z in destinos}) >= 3
    xs = [x for x, _z in destinos]
    assert max(xs) - min(xs) >= 0.5                                  # no oscila entre el centro y un punto
    giros = [c["rotation"] for c in avatar.comandos() if c["type"] == "move" and "x" not in c]
    assert max(abs(g) for g in giros) > 20.0


# ---------------------------------------------------------------------------
# Decisiones por estado
# ---------------------------------------------------------------------------


def test_aburrida_explora_o_se_encoge(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    preferidas = corteza.preferidas(ABURRIDA, CTX, 3)
    assert preferidas[0] == "pasear" and "encogerse" in preferidas
    cuenta = _primeras_decisiones(settings_prueba, ABURRIDA)
    assert _fraccion(cuenta, {"pasear", "girar", "mirar_alrededor", "encogerse"}) >= 0.45
    assert cuenta.most_common(1)[0][0] == "pasear"


def test_fatiga_estira_bosteza_y_baja_la_actividad(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    assert set(corteza.preferidas(FATIGADA, CTX, 2)) == {"estirarse", "bostezar"}
    assert corteza.intervalo_medio(FATIGADA) > 1.5 * corteza.intervalo_medio(FRESCA)
    assert corteza.tasa_conductas(FATIGADA) < corteza.tasa_conductas(FRESCA)
    assert corteza.pesos_base(FATIGADA, CTX).get("pasear", 0.0) < corteza.pesos_base(FRESCA, CTX)["pasear"]
    assert corteza.intensidad_idle(FATIGADA) < corteza.intensidad_idle(FRESCA)
    cuenta = _primeras_decisiones(settings_prueba, FATIGADA)
    assert _fraccion(cuenta, {"estirarse", "bostezar", "dormitar"}) >= 0.4
    plan = corteza.construir("bostezar", FATIGADA, CTX)
    assert plan is not None
    # La boca y los párpados del bostezo se cierran explícitamente (clearEmotions no toca aa/blink).
    finales = [c for p in plan.pasos if p.final for c in p.comandos if c["type"] == "expression"]
    assert any(c.get("values", {}).get("aa") == 0.0 for c in finales)


def test_foco_alto_pensativa_quieta_y_mirando_a_camara(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    pesos = corteza.pesos_base(FOCO, CTX)
    assert corteza.preferidas(FOCO, CTX, 1) == ["pensativa"]
    assert "pasear" not in pesos and "brazos_cruzados" in corteza.preferidas(FOCO, CTX, 4)
    assert corteza.intervalo_medio(FOCO) > corteza.intervalo_medio(HomeostaticState(focus=0.5))
    cuenta = _primeras_decisiones(settings_prueba, FOCO)
    assert _fraccion(cuenta, {"pensativa", "brazos_cruzados"}) >= 0.35
    for semilla in range(10):
        plan = _corteza(settings_prueba, semilla=semilla).construir("pensativa", FOCO, CTX)
        assert plan is not None
        assert plan.pasos[-1].final and any(c == {"type": "look", "at": "camera", "head": 0.35}
                                            for p in plan.pasos if p.final for c in p.comandos)
    # Lejos del centro y con foco alto: vuelve al centro y queda de frente mirando a cámara.
    lejos = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES, posicion=(0.9, -0.6), rotacion=20.0)
    plan = corteza.decidir(FOCO, 100.0, lejos).plan
    assert plan is not None and plan.conducta == "volver_al_centro"
    camino = next(c for c in plan.comandos() if c["type"] == "move" and c.get("walk"))
    assert (camino["x"], camino["z"]) == (0.0, 0.0)
    assert plan.comandos()[-2] == {"type": "move", "rotation": 0.0, "walk": False, "duration": 0.6}
    assert plan.comandos()[-1]["at"] == "camera"


def test_curiosidad_mira_alrededor_o_ladea(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    preferidas = corteza.preferidas(CURIOSA, CTX, 3)
    assert preferidas[0] == "mirar_alrededor" and "ladear_cabeza" in preferidas
    cuenta = _primeras_decisiones(settings_prueba, CURIOSA)
    assert _fraccion(cuenta, {"mirar_alrededor", "ladear_cabeza", "mirada", "pasear", "girar"}) >= 0.6


def test_alegria_contenida_de_kurisu(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    assert corteza.preferidas(ALEGRE, CTX, 1) == ["alegria"]
    assert "tsundere" in corteza.preferidas(ALEGRE, CTX, 4)
    gestos: Counter[str] = Counter()
    for semilla in range(300):
        plan = _corteza(settings_prueba, semilla=semilla).construir("alegria", EUFORICA, CTX)
        assert plan is not None
        gestos.update(c["name"] for c in plan.comandos() if c["type"] == "gesture")
    total = sum(gestos.values())
    assert not {"dance", "jump", "clap"} & set(gestos)
    assert (gestos["laugh"] + gestos["cheer"]) / total < 0.2 and gestos["cheer"] / total < 0.06
    assert (gestos["tilt_head"] + gestos["nod"]) / total > 0.75


def test_reading_steiner_sorpresa_mano_a_la_sien_y_mirar_alrededor(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    plan = corteza.plan_reading_steiner(HomeostaticState(), CTX)
    assert plan is not None
    gestos = [c["name"] for c in plan.comandos() if c["type"] == "gesture"]
    assert gestos == ["surprise", "look_around"]
    huesos = next(c for c in plan.comandos() if c["type"] == "bones")
    assert {"rightUpperArm", "rightLowerArm", "rightHand"} <= set(huesos["rotations"])
    assert plan.comandos()[0]["values"] == {"surprised": 0.9}
    finales = [c for p in plan.pasos if p.final for c in p.comandos]
    assert {"type": "pose", "name": "relaxed", "duration": 1.0} in finales
    # Sin huesos de brazo: la mano va al mentón con la pose del visor.
    sin_brazos = ContextoMotor(huesos=frozenset({"head"}), expresiones=EXPRESIONES)
    plan = corteza.plan_reading_steiner(HomeostaticState(), sin_brazos)
    assert plan is not None and {"type": "pose", "name": "thinking", "duration": 0.7} in plan.comandos()


def test_decision_determinista_con_semilla(settings_prueba: Any) -> None:
    def secuencia(semilla: int) -> list[Any]:
        corteza = _corteza(settings_prueba, semilla=semilla)
        memoria = corteza.memoria
        salida = []
        t = 100.0
        for estado in (ABURRIDA, CURIOSA, FOCO, ALEGRE, FATIGADA) * 3:
            t += 30.0
            decision = corteza.decidir(estado, t, CTX, memoria)
            memoria = decision.memoria
            salida.append(decision.plan.comandos() if decision.plan else None)
        return salida

    assert secuencia(3) == secuencia(3)
    assert secuencia(3) != secuencia(4)


def test_decidir_es_un_nucleo_puro(settings_prueba: Any) -> None:
    """estado + reloj + contexto + memoria → Decision, sin E/S ni efectos sobre la corteza."""
    corteza = _corteza(settings_prueba, semilla=4)
    memoria = corteza.memoria
    a = corteza.decidir(ABURRIDA, 100.0, CTX, memoria)
    b = corteza.decidir(ABURRIDA, 100.0, CTX, memoria)
    assert a.plan is not None and a == b                             # misma entrada → misma salida
    assert corteza.memoria == memoria                                # no toca la corteza
    assert corteza.decidir(ABURRIDA, 100.0, CTX) == a                # por defecto lee la memoria actual
    assert a.memoria.n == memoria.n + 1 and a.memoria.ultima == a.plan.conducta
    assert a.memoria.uso(a.plan.conducta) == 100.0 and a.memoria.plan_hasta == pytest.approx(100.0 + a.plan.duracion)
    assert memoria.uso(a.plan.conducta) == -math.inf                  # la memoria de entrada sigue intacta
    # No necesita avatar: sin avatar decide exactamente igual (no hace E/S).
    assert CortezaMotora(settings_prueba, None, None, semilla=4).decidir(ABURRIDA, 100.0, CTX, memoria) == a
    # Con la memoria siguiente no repite la conducta y respeta la próxima decisión.
    assert corteza.decidir(ABURRIDA, 100.5, CTX, a.memoria).plan is None
    siguiente = corteza.decidir(ABURRIDA, a.memoria.proxima, CTX, a.memoria)
    assert siguiente.plan is None or siguiente.plan.conducta != a.plan.conducta
    # El tope de comandos llega en el contexto: sin presupuesto no elige y lo reintenta en 2 s.
    sin_tope = corteza.decidir(ABURRIDA, 100.0, replace(CTX, presupuesto=cm.RESERVA_PRIORITARIA), memoria)
    assert sin_tope.plan is None and sin_tope.memoria.proxima == 102.0
    # Habla en curso, avatar fuera de línea o actividad 0: nada y la memoria no cambia.
    assert corteza.decidir(ABURRIDA, 100.0, replace(CTX, hablando=True), memoria) == cm.Decision(None, memoria)
    assert corteza.decidir(ABURRIDA, 100.0, replace(CTX, en_linea=False), memoria) == cm.Decision(None, memoria)


def test_sin_repeticiones_seguidas_y_enfriamientos(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    memoria = corteza.memoria
    t, anterior, usos = 100.0, None, {}
    for _ in range(200):
        t += 1.0
        decision = corteza.decidir(EUFORICA, t, CTX, memoria)
        memoria, plan = decision.memoria, decision.plan
        if plan is None:
            continue
        assert plan.conducta != anterior
        if plan.conducta in usos:
            assert t - usos[plan.conducta] >= cm.ENFRIAMIENTO_S[plan.conducta]
        anterior, usos[plan.conducta] = plan.conducta, t


# ---------------------------------------------------------------------------
# Límites
# ---------------------------------------------------------------------------


def test_planes_acotados_y_validos(settings_prueba: Any) -> None:
    planes = _todos_los_planes(settings_prueba, avatar_camara=True)
    assert len(planes) > 500
    conductas = {p.conducta for p in planes}
    assert set(cm.CONDUCTAS) - {"camara"} <= conductas | {"volver_al_centro"}
    for plan in planes:
        for c in plan.comandos():
            tipo = c["type"]
            assert tipo in cm.TIPOS_PERMITIDOS, c
            if tipo == "move":
                assert "relative" not in c
                assert abs(c.get("x", 0.0)) <= 1.0 + 1e-9      # escenario del encuadre "full"
                assert -0.7 - 1e-9 <= c.get("z", 0.0) <= 0.6
                assert abs(c.get("rotation", 0.0)) <= 180.0
            elif tipo == "bones":
                for hueso, ejes in c["rotations"].items():
                    assert hueso in HUESOS and hueso in cm.LIMITES_HUESO
                    for eje, (minimo, maximo) in zip("xyz", cm.LIMITES_HUESO[hueso]):
                        if eje in ejes:
                            assert minimo <= ejes[eje] <= maximo, (hueso, ejes)
                    if hueso not in cm.MANO_A_LA_SIEN:         # micro-posturas: ángulos pequeños
                        assert all(abs(v) <= 16.0 for v in ejes.values()), (hueso, ejes)
            elif tipo == "expression":
                assert set(c.get("values", {})) <= EXPRESIONES and c.get("name", "neutral") in EXPRESIONES | {"neutral"}
            elif tipo == "gesture":
                assert c["name"] in cm.GESTOS_PERMITIDOS and c["name"] not in {"dance", "jump", "clap", "bow"}
            elif tipo == "pose":
                assert c["name"] in cm.POSES_PERMITIDAS and c["name"] not in {"tpose", "apose"}
            elif tipo == "look":
                assert c["at"] in cm.MIRADAS_PERMITIDAS
                if c["at"] == "point":
                    assert abs(c["x"]) <= 3.0 and 0.3 <= c["y"] <= 2.2 and abs(c["z"]) <= 3.0


def test_solo_huesos_y_expresiones_del_modelo(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    solo_cabeza = ContextoMotor(huesos=frozenset({"head"}), expresiones=frozenset({"happy"}))
    for semilla in range(30):
        corteza = _corteza(settings_prueba, semilla=semilla)
        for conducta in ("micro_postura", "ladear_cabeza", "bostezar", "dormitar", "microexpresion", "alegria",
                         "tsundere"):
            plan = corteza.construir(conducta, ALEGRE, solo_cabeza)
            for c in plan.comandos() if plan else []:
                if c["type"] == "bones":
                    assert set(c["rotations"]) == {"head"}
                if c["type"] == "expression":
                    assert set(c.get("values", {})) <= {"happy"} and c.get("name", "neutral") in {"neutral", "happy"}
    # Sin inventario de huesos (visor aún no sondeado): nada de bones.
    sin_huesos = ContextoMotor(huesos=None, expresiones=EXPRESIONES)
    assert "micro_postura" not in corteza.pesos_base(ABURRIDA, sin_huesos)
    assert corteza.construir("micro_postura", ABURRIDA, sin_huesos) is None


def test_sanear_comando() -> None:
    mov = sanear_comando({"type": "move", "x": 9.0, "z": -9.0, "rotation": 400.0, "relative": True,
                          "walk": True, "duration": 99})
    assert mov == {"type": "move", "x": cm.LIMITE_X_ABS, "z": cm.LIMITE_Z_ABS[0], "rotation": 180.0, "walk": True,
                   "duration": 8.0}
    sin_andar = sanear_comando({"type": "move", "x": 0.5, "rotation": 20, "walk": True}, caminar=False)
    assert sin_andar == {"type": "move", "rotation": 20.0, "walk": False, "duration": 1.0}
    assert sanear_comando({"type": "move", "x": 0.5, "walk": True}, caminar=False) is None
    huesos = sanear_comando({"type": "bones", "rotations": {"head": {"x": 90, "q": 3}, "hips": {"z": -50},
                                                            "leftUpperLeg": {"x": 30}, "tail": {"x": 1}},
                             "relative": True}, huesos=frozenset({"head", "hips", "leftUpperLeg"}))
    assert huesos == {"type": "bones", "rotations": {"head": {"x": 14.0}, "hips": {"z": -5.0}}, "duration": 0.8}
    assert sanear_comando({"type": "bones", "rotations": {"head": {"x": 5}}}, huesos=None) is None
    for prohibido in ("speak", "sequence", "stop", "reset", "load_model", "background", "wait", "state"):
        assert sanear_comando({"type": prohibido, "text": "hola"}) is None
    assert sanear_comando({"type": "gesture", "name": "dance"}) is None
    assert sanear_comando({"type": "gesture", "name": "wave", "side": "arriba", "loop": True}) == \
        {"type": "gesture", "name": "wave"}
    assert sanear_comando({"type": "pose", "name": "tpose"}) is None
    assert sanear_comando({"type": "camera", "preset": "face"}) is None
    assert sanear_comando({"type": "camera", "preset": "face"}, camara=True)["preset"] == "face"
    assert sanear_comando({"type": "look", "at": "point", "x": 1}) is None
    assert sanear_comando({"type": "look", "at": "none"}) is None
    assert sanear_comando({"type": "expression", "name": "sonrisa"}, expresiones=EXPRESIONES) is None
    assert sanear_comando({"type": "expression", "values": {"x": 1}}, expresiones=EXPRESIONES)["name"] == "neutral"
    assert sanear_comando({"type": "idle", "enabled": False, "intensity": 7}) == \
        {"type": "idle", "enabled": True, "intensity": 2.0, "blink": True}
    assert sanear_comando("basura") is None


def test_caminar_deshabilitado_no_desplaza(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba, avatar_caminar=False)
    pesos = corteza.pesos_base(ABURRIDA, CTX)
    assert "pasear" not in pesos and "volver_al_centro" not in pesos and pesos["girar"] > 0
    for plan in _todos_los_planes(settings_prueba, avatar_caminar=False):
        for c in plan.comandos():
            if c["type"] == "move":
                assert not c["walk"] and "x" not in c and "z" not in c
    lejos = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES, posicion=(1.0, -0.5), rotacion=25.0)
    atencion = corteza.plan_atencion(FOCO, lejos)
    assert atencion is not None
    assert [c for c in atencion.comandos() if c["type"] == "move"] == \
        [{"type": "move", "rotation": 0.0, "walk": False, "duration": 0.5}]


def test_encuadre_cerrado_no_camina(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    busto = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES, camara="bust")
    assert "pasear" not in corteza.pesos_base(ABURRIDA, busto)
    assert corteza.construir("pasear", ABURRIDA, busto) is None
    assert corteza.construir("camara", FOCO, CTX) is None      # avatar_camara=False: nunca toca la cámara
    con_camara = _corteza(settings_prueba, avatar_camara=True)
    plan = con_camara.construir("camara", FOCO, CTX)
    assert plan is not None and plan.comandos() == [{"type": "camera", "preset": "bust", "duration": 1.2}]


def test_actividad_cero_queda_quieta(settings_prueba: Any) -> None:
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, avatar=avatar, reloj=reloj, avatar_actividad=0.0)
    assert not corteza.activa and avatar.reposo_externo is False
    assert corteza.decidir(EUFORICA, 100.0, CTX).plan is None
    assert corteza.tasa_conductas(EUFORICA) == 0.0
    for _ in range(400):
        reloj.avanzar(0.25)
        assert corteza.tick() == 0
    assert avatar.envios == []
    diag = corteza.diagnostico()
    assert (diag.clave, diag.nombre, diag.estado) == ("motor", "Corteza motora (avatar 3D)", EstadoModulo.OFF)


# ---------------------------------------------------------------------------
# E/S con avatar falso: tasa, habla, atención, bus
# ---------------------------------------------------------------------------


def test_tasa_maxima_de_comandos(settings_prueba: Any, bus: Any) -> None:
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj, avatar_actividad=3.0,
                       avatar_camara=True)
    corteza._estado = EUFORICA
    planes: set[str] = set()
    for i in range(int(15 * 60 / cm.TICK_S)):
        reloj.avanzar(cm.TICK_S)
        if i % 240 == 0:                                       # el usuario escribe cada minuto
            bus.publicar("usuario.entrada", {"texto": "hola", "canal": "terminal"}, origen="main")
        if i % 400 == 0:
            bus.publicar("homeostasis.cambio_linea", {"antes": "alfa", "despues": "beta", "divergencia": 0.5},
                         origen="main")
        corteza.tick()
        if corteza.plan_actual:
            planes.add(corteza.plan_actual)
    tiempos = [(t, len(c) if isinstance(c, list) else 1) for t, c in avatar.envios]
    assert len(tiempos) > 100 and len(planes) >= 10
    for t_fin, _n in tiempos:
        en_ventana = sum(n for t, n in tiempos if t_fin - 60.0 < t <= t_fin)
        assert en_ventana <= cm.LIMITE_COMANDOS_MIN, (t_fin, en_ventana)


def test_silencio_mientras_habla_y_cierre_de_la_conducta(settings_prueba: Any) -> None:
    reloj = Reloj()
    avatar = AvatarConVisor(reloj)
    corteza = _corteza(settings_prueba, avatar=avatar, reloj=reloj)
    assert corteza.decidir(EUFORICA, reloj(), ContextoMotor(hablando=True)).plan is None
    corteza.tick()                                             # cara de reposo e idle iniciales
    reloj.avanzar(1.0)
    plan = corteza.construir("pasear", HomeostaticState(boredom=0.9, bump=tuple(_bump_pico(4))),
                             corteza.contexto(reloj()))
    assert plan is not None
    avatar.limpiar()
    assert corteza.ejecutar(plan) > 0                          # mira hacia donde va
    for _ in range(4):                                         # gira (≈0,6 s) y empieza a caminar
        reloj.avanzar(0.25)
        corteza.tick()
    assert any(c["type"] == "move" and c.get("walk") for c in avatar.comandos()) and avatar.visor.camina(reloj())
    avatar.limpiar()
    avatar.hablando = True
    corteza.tick()                                             # empieza el habla: se detiene de frente y mira YA
    assert len(avatar.envios) == 1                             # una sola lista paralela
    corte = avatar.comandos()
    assert {"type": "move", "rotation": 0.0, "walk": False, "duration": 0.6} in corte
    assert {"type": "look", "at": "camera", "head": 0.35} in corte
    assert not any(c["type"] == "expression" for c in corte)   # la cara la pone el habla
    assert not avatar.visor.camina(reloj()) and avatar.visor.rot == 0.0 and avatar.visor.mirada == "camera"
    assert corteza.plan_actual is None
    avatar.limpiar()
    for _ in range(80):                                        # 20 s hablando: silencio total
        reloj.avanzar(0.25)
        corteza.tick()
    assert avatar.envios == []
    avatar.hablando = False
    reloj.avanzar(0.25)
    corteza.tick()                                             # fin del habla: solo la cara de reposo nueva
    comandos = avatar.comandos()
    assert any(c["type"] == "expression" for c in comandos)
    assert not any(c["type"] in ("look", "move", "pose") for c in comandos)
    # Tras hablar espera un poco antes de volver a moverse por su cuenta.
    avatar.limpiar()
    corteza._estado = EUFORICA
    for _ in range(int(cm.CALMA_TRAS_HABLAR_S / 0.25) - 1):
        reloj.avanzar(0.25)
        corteza.tick()
    assert not any(c["type"] in ("gesture", "pose", "move", "bones") for c in avatar.comandos())


@pytest.mark.parametrize(("conducta", "estado", "t_habla"), [
    ("estirarse", FATIGADA, 1.0), ("bostezar", FATIGADA, 0.8), ("dormitar", FATIGADA, 3.0),
    ("pensativa", FOCO, 2.0), ("seguir_raton", CURIOSA, 1.0), ("mirada", CURIOSA, 0.5),
    ("tsundere", ALEGRE, 0.8), ("micro_postura", ABURRIDA, 1.0), ("girar", CURIOSA, 1.5),
    ("pasear", ABURRIDA, 1.2), ("mirar_alrededor", CURIOSA, 0.5), ("brazos_cruzados", FOCO, 1.5),
    ("manos", FRESCA, 0.5), ("reading_steiner", CURIOSA, 2.0),
])
def test_al_empezar_el_habla_el_cuerpo_vuelve_ya(settings_prueba: Any, conducta: str, estado: HomeostaticState,
                                                 t_habla: float) -> None:
    """Si el habla corta una conducta a medias, el cuerpo no queda congelado durante la respuesta."""
    reloj = Reloj()
    avatar = AvatarConVisor(reloj)
    corteza = _corteza(settings_prueba, avatar=avatar, reloj=reloj, semilla=3)
    corteza._estado = estado
    corteza.tick()
    reloj.avanzar(0.5)
    ctx = corteza.contexto(reloj())
    if conducta == "reading_steiner":
        plan = corteza.plan_reading_steiner(estado, ctx)
    else:
        plan = corteza.construir(conducta, estado, ctx)
    assert plan is not None and corteza.ejecutar(plan) > 0
    t = 0.0
    while t < t_habla:
        reloj.avanzar(0.25)
        t += 0.25
        corteza.tick()
    n = len(avatar.envios)
    avatar.hablando = True
    reloj.avanzar(0.25)
    corteza.tick()
    corte = [c for _t, envio in avatar.envios[n:] for c in (envio if isinstance(envio, list) else [envio])]
    assert len(avatar.envios) <= n + 1                          # todo en una sola lista paralela
    assert not any(c["type"] == "expression" and c.get("exclusive", True) for c in corte)   # la cara espera
    v = avatar.visor
    assert v.gestos_en_curso(reloj()) == set()                 # sus gestos largos se cortan (stop gestures)
    for _ in range(60):                                        # 15 s hablando: quieta y sin enviar nada
        reloj.avanzar(0.25)
        corteza.tick()
    assert len(avatar.envios) <= n + 1
    assert v.mirada == "camera" and v.rot == 0.0 and not v.camina(reloj())
    assert v.pose in ("relaxed", "custom") and v.huesos_en_reposo(), (v.pose, v.huesos)
    assert v.expr.get("aa", 0.0) == 0.0 and v.expr.get("blink", 0.0) == 0.0
    assert v.idle == pytest.approx(corteza.intensidad_idle(estado))
    m = len(avatar.comandos())
    avatar.hablando = False
    reloj.avanzar(0.25)
    corteza.tick()
    assert any(c["type"] == "expression" for c in avatar.comandos()[m:])   # tras el habla, cara de reposo nueva


def test_al_cortar_no_pisa_el_motion_del_habla(settings_prueba: Any, bus: Any) -> None:
    """Lo que trae el MOTION del habla (pose, mirada, gesto) lo gobierna el puente."""

    def cortar(motion: str, conducta: str, estado: HomeostaticState, t_habla: float) -> list[dict[str, Any]]:
        reloj = Reloj()
        avatar = AvatarFalso(reloj)
        corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj, semilla=3)
        corteza._estado = estado
        corteza.tick()
        reloj.avanzar(0.5)
        ctx = corteza.contexto(reloj())
        plan = (corteza.plan_reading_steiner(estado, ctx) if conducta == "reading_steiner"
                else corteza.construir(conducta, estado, ctx))
        assert plan is not None and corteza.ejecutar(plan) > 0
        for _ in range(int(t_habla / 0.25)):
            reloj.avanzar(0.25)
            corteza.tick()
        avatar.limpiar()
        bus.publicar("salida.subtitulo", {"texto": "Hmph.", "exp": "smug", "motion": motion, "tone": "sarcastico",
                                          "canal": "voz", "origen": "respuesta"}, origen="riko")
        reloj.avanzar(0.25)
        corteza.tick()
        comandos = avatar.comandos()
        asyncio.run(corteza.cerrar())
        return comandos

    sin_motion = cortar("idle", "estirarse", FATIGADA, 1.0)
    assert {"type": "pose", "name": "relaxed", "duration": 1.2} in sin_motion
    con_pose = cortar("brazos_cruzados", "estirarse", FATIGADA, 1.0)        # el habla trae arms_crossed
    assert not any(c["type"] in ("pose", "bones") for c in con_pose)
    assert {"type": "look", "at": "camera", "head": 0.35} in con_pose
    con_pose = cortar("facepalm", "micro_postura", ABURRIDA, 1.0)           # pose thinking + shake_head
    assert not any(c["type"] in ("pose", "bones", "stop") for c in con_pose)
    con_mirada = cortar("mirar_a_otro_lado", "estirarse", FATIGADA, 1.0)
    assert not any(c["type"] == "look" for c in con_mirada) and any(c["type"] == "pose" for c in con_mirada)
    assert cm.PARAR_GESTOS in cortar("idle", "reading_steiner", CURIOSA, 2.0)
    assert cm.PARAR_GESTOS not in cortar("asentir", "reading_steiner", CURIOSA, 2.0)   # el habla trae su gesto


def test_responde_mirando_a_camara_tras_la_latencia_del_llm(settings_prueba: Any, bus: Any) -> None:
    """El usuario escribe, la respuesta llega 3–6 s después: siempre habla de frente y mirando a cámara."""
    for latencia in (3.0, 4.5, 6.0):
        for semilla in range(12):
            reloj = Reloj()
            avatar = AvatarConVisor(reloj)
            corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj, semilla=semilla)
            corteza._estado = HomeostaticState(arousal=0.7, focus=0.75, curiosidad=0.3, valencia=0.3)
            for _ in range(int(40 / 0.25)):
                reloj.avanzar(0.25)
                corteza.tick()
            bus.publicar("usuario.entrada", {"texto": "hola", "canal": "terminal"}, origen="main")
            for _ in range(int(latencia / 0.25)):
                reloj.avanzar(0.25)
                corteza.tick()
            avatar.hablando = True
            for _ in range(20):
                reloj.avanzar(0.25)
                corteza.tick()
            v = avatar.visor
            assert v.mirada == "camera" and v.rot == 0.0 and v.huesos_en_reposo(), (latencia, semilla, v.mirada)
            assert v.pose in ("relaxed", "custom") and not v.camina(reloj())
            asyncio.run(corteza.cerrar())


def test_subtitulo_anuncia_habla_antes_que_el_puente(settings_prueba: Any, bus: Any) -> None:
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj, avatar_actividad=3.0)
    corteza._estado = EUFORICA
    corteza.tick()
    avatar.limpiar()
    bus.publicar("salida.subtitulo", {"texto": "Hmph.", "exp": "smug", "motion": "brazos_cruzados",
                                      "tone": "sarcastico", "canal": "terminal", "origen": "respuesta"}, origen="riko")
    for _ in range(4):                                         # 1 s: el puente aún agrupa, ella ya calla
        reloj.avanzar(0.25)
        corteza.tick()
    assert avatar.envios == []
    bus.publicar("voz.inicio", {"motor": "edge", "tone": "calido", "utterance_id": "u1"}, origen="riko")
    for _ in range(40):
        reloj.avanzar(0.25)
        corteza.tick()
    assert avatar.envios == []
    bus.publicar("voz.fin", {"utterance_id": "u1", "duracion_s": 10.0, "motor": "edge"}, origen="riko")
    for _ in range(40):
        reloj.avanzar(0.25)
        corteza.tick()
    assert avatar.envios


def test_atencion_al_usuario(settings_prueba: Any, bus: Any) -> None:
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj)
    corteza._pos, corteza._rot = (0.9, -0.6), 30.0             # quedó lejos tras un paseo
    corteza.tick()
    avatar.limpiar()
    bus.publicar("usuario.entrada", {"texto": "Kurisu, ¿estás?", "canal": "terminal"}, origen="main")
    reloj.avanzar(0.25)
    corteza.tick()
    comandos = avatar.comandos()
    assert comandos[0] == {"type": "look", "at": "camera", "head": 0.45}
    acercarse = next(c for c in comandos if c["type"] == "move")
    assert acercarse["walk"] and acercarse["rotation"] == 0.0 and acercarse["z"] == cm.Z_ATENCION
    assert abs(acercarse["x"]) < 0.9
    # Durante la atención no se va de paseo.
    ctx = corteza.contexto(reloj())
    assert ctx.desde_usuario_s < cm.ATENCION_S
    assert not {"pasear", "girar", "estirarse"} & set(corteza.pesos_base(ABURRIDA, ctx))


@pytest.mark.parametrize(("canal", "atiende"), [("telegram", False), ("nexus", True), ("terminal", True)])
def test_atencion_solo_si_esta_frente_al_avatar(settings_prueba: Any, bus: Any, canal: str, atiende: bool) -> None:
    """Por Telegram el usuario no está frente a la cámara: ni mira, ni se acerca, ni deja de pasear."""
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj)
    corteza._pos, corteza._rot = (0.9, -0.5), 20.0
    corteza.tick()
    avatar.limpiar()
    bus.publicar("usuario.entrada", {"texto": "hola desde el móvil", "canal": canal}, origen="main")
    reloj.avanzar(0.25)
    corteza.tick()
    mira = {"type": "look", "at": "camera", "head": 0.45} in avatar.comandos()
    assert mira is atiende
    ctx = corteza.contexto(reloj())
    assert (ctx.desde_usuario_s < cm.ATENCION_S) is atiende
    assert ("pasear" in corteza.pesos_base(ABURRIDA, ctx)) is not atiende


def test_adopta_la_posicion_que_informa_el_visor(settings_prueba: Any) -> None:
    """Si el usuario la movió a mano, la corteza se entera por /api/state (vía el puente)."""
    reloj = Reloj()
    avatar = AvatarFalso(reloj)
    avatar.posicion = (0.8, -0.3, 20.0)
    avatar.ts_estado = reloj() - 5.0                       # foto anterior a cualquier comando: no se usa
    avatar.estado_visor = {"pose": "arms_crossed", "camera": "bust"}
    corteza = _corteza(settings_prueba, avatar=avatar, reloj=reloj)
    corteza.tick()                                         # primer envío (cara de reposo)
    assert corteza.contexto(reloj()).posicion == (0.0, 0.0)
    ctx = corteza.contexto(reloj())
    assert ctx.pose == "arms_crossed" and ctx.camara == "bust"
    assert "pasear" not in corteza.pesos_base(ABURRIDA, ctx)   # con el encuadre de busto no camina
    avatar.estado_visor = {"pose": "relaxed", "camera": "full"}
    reloj.avanzar(2.0)
    avatar.ts_estado = reloj()                             # foto posterior a ese envío
    corteza.tick()
    ctx = corteza.contexto(reloj())
    assert ctx.posicion == (0.8, -0.3) and ctx.rotacion == 20.0
    # Mientras camina, manda su propio modelo (la foto del visor puede ser de antes del paseo).
    plan = corteza.construir("volver_al_centro", FOCO, ctx)
    assert plan is not None and corteza.ejecutar(plan) > 0
    for _ in range(12):
        reloj.avanzar(0.25)
        corteza.tick()
    avatar.ts_estado = reloj()
    corteza.tick()
    assert corteza.contexto(reloj()).posicion != (0.8, -0.3)


def test_integracion_con_bus_y_avatar(settings_prueba: Any, bus: Any, correr: Any) -> None:
    reloj = Reloj()
    avatar = AvatarFalso(reloj, en_linea=False)
    modulos: list[dict[str, Any]] = []
    bus.suscribir("sistema.modulo", lambda e: modulos.append(dict(e.datos)))

    async def escenario() -> None:
        corteza = _corteza(settings_prueba, bus=bus, avatar=avatar, reloj=reloj)
        assert avatar.reposo_externo is True                   # ella gobierna la cara en reposo
        assert corteza.tick() == 0 and avatar.envios == []     # visor cerrado: nada
        assert corteza.diagnostico().estado == EstadoModulo.EN_ESPERA
        avatar.en_linea = True
        estado = HomeostaticState(arousal=0.8, valencia=0.5, boredom=0.6)
        datos = estado.a_dict()
        datos.pop("bump")
        bus.publicar("homeostasis.estado", {"estado": datos, "bump": _bump_pico(4)}, origen="main")
        assert corteza.estado_homeostatico.arousal == 0.8
        assert corteza.estado_homeostatico.bump == tuple(_bump_pico(4))
        reloj.avanzar(0.25)
        corteza.tick()
        assert corteza.diagnostico().estado == EstadoModulo.OK
        tipos = {c["type"] for c in avatar.comandos()}
        assert {"expression", "idle"} <= tipos                 # cara de reposo + intensidad del idle
        # Reading Steiner por el bus.
        avatar.limpiar()
        bus.publicar("homeostasis.cambio_linea", {"antes": "steins_gate", "despues": "beta",
                                                  "divergencia": 0.571046}, origen="main")
        reloj.avanzar(0.25)
        corteza.tick()
        assert {"type": "gesture", "name": "surprise"} in avatar.comandos()
        for _ in range(30):
            reloj.avanzar(0.25)
            corteza.tick()
        assert {"type": "gesture", "name": "look_around"} in avatar.comandos()
        assert {"type": "pose", "name": "relaxed", "duration": 1.0} in avatar.comandos()
        # Payloads basura: los manejadores nunca lanzan.
        errores = bus.estadisticas()["errores"]
        bus.publicar("homeostasis.estado", {"estado": "basura", "bump": "x"}, origen="main")
        bus.publicar("salida.subtitulo", {"texto": None, "motion": 3}, origen="riko")
        bus.publicar("usuario.entrada", {}, origen="main")
        assert bus.estadisticas()["errores"] == errores
        suscritos = bus.estadisticas()["suscriptores"]
        await corteza.cerrar()
        await corteza.cerrar()                                 # idempotente
        assert avatar.reposo_externo is False
        # 7 de la v0.1 + 2 de la visión continua + 5 del escritorio (settings_prueba: tiene_escritorio).
        assert bus.estadisticas()["suscriptores"] == suscritos - 14
        assert corteza.tick() == 0

    correr(escenario())
    motor = [m for m in modulos if m["clave"] == "motor"]
    assert [m["estado"] for m in motor][:2] == ["EN ESPERA", "OK"]
    assert motor[0]["nombre"] == "Corteza motora (avatar 3D)"


def test_bucle_cancelable(settings_prueba: Any, correr: Any) -> None:
    avatar = AvatarFalso()

    async def escenario() -> None:
        corteza = CortezaMotora(settings_prueba, None, avatar, semilla=1)
        parar = asyncio.Event()
        tarea = asyncio.create_task(corteza.bucle(parar))
        await asyncio.sleep(0.6)
        parar.set()
        await asyncio.wait_for(tarea, timeout=2.0)
        tarea = asyncio.create_task(corteza.bucle(asyncio.Event()))
        await asyncio.sleep(0.1)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        await corteza.cerrar()

    correr(escenario())
    assert any(c["type"] == "expression" for c in avatar.comandos())


def test_puente_real_con_avatar_falso_de_red(settings_prueba: Any, bus: Any, correr: Any) -> None:
    """La corteza sobre el VTuberAIBridge de verdad (servidor simulado con httpx.MockTransport)."""
    httpx = pytest.importorskip("httpx")
    import json

    from lobes.vtuber_ai_bridge import VTuberAIBridge

    recibidos: list[Any] = []

    def servidor(request: Any) -> Any:
        if request.url.path == "/api/state":
            return httpx.Response(200, json={"viewers": 1, "ready": True, "model": "Kurisu.vrm",
                                             "expressionsAvailable": sorted(EXPRESIONES),
                                             "bonesAvailable": sorted(HUESOS),
                                             "current": {"position": {"x": 0.0, "z": 0.0}, "rotation": 0,
                                                         "camera": "full", "pose": "relaxed"}})
        recibidos.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "delivered": 1, "results": []})

    async def escenario() -> None:
        cliente = httpx.AsyncClient(transport=httpx.MockTransport(servidor))
        puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True), bus, cliente=cliente)
        await puente.iniciar()
        corteza = CortezaMotora(settings_prueba.con(avatar_actividad=3.0), bus, puente, semilla=5)
        assert puente.reposo_externo is True
        corteza._estado = EUFORICA
        for _ in range(12):
            corteza.tick()
            await asyncio.sleep(0.05)
        await corteza.cerrar()
        assert puente.reposo_externo is False
        await puente.cerrar()
        await cliente.aclose()

    correr(escenario())
    assert recibidos
    for envio in recibidos:
        for c in envio if isinstance(envio, list) else [envio]:
            assert c["type"] in cm.TIPOS_PERMITIDOS


# ---------------------------------------------------------------------------
# Integración con main.py (sin VTuber AI Studio real: puente falso)
# ---------------------------------------------------------------------------


class _PuenteFalso:
    """Sustituye a VTuberAIBridge en main: nunca abre conexiones."""

    cierres: list[str] = []

    def __init__(self, settings: Any, bus: Any = None, *, cliente: Any = None) -> None:
        self.en_linea = False
        self.hablando = False
        self.expresiones_disponibles = None
        self.huesos_disponibles = None
        self.reposo_externo = False
        self.encolados: list[Any] = []

    def encolar(self, comando: Any) -> None:
        self.encolados.append(comando)

    def diagnostico(self) -> DiagnosticoModulo:
        return DiagnosticoModulo("avatar3d", "Avatar 3D (VTuber AI Studio)", EstadoModulo.EN_ESPERA, "simulado")

    async def iniciar(self) -> DiagnosticoModulo:
        return self.diagnostico()

    async def bucle(self, parar: asyncio.Event) -> None:
        await parar.wait()

    async def cerrar(self) -> None:
        _PuenteFalso.cierres.append("avatar3d")


def _orquestador(settings: Any) -> tuple[Any, io.StringIO]:
    import main

    salida = io.StringIO()
    return main.Orquestador(settings, consola=main.Consola(settings, salida=salida, usar_prompt_toolkit=False)), salida


def test_main_construye_y_cierra_la_corteza(settings_prueba: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import lobes.vtuber_ai_bridge as puente_mod

    monkeypatch.setattr(puente_mod, "VTuberAIBridge", _PuenteFalso)
    _PuenteFalso.cierres = []
    cerrar_original = CortezaMotora.cerrar

    async def cerrar_espia(self: CortezaMotora) -> None:
        _PuenteFalso.cierres.append("motor")
        await cerrar_original(self)

    monkeypatch.setattr(CortezaMotora, "cerrar", cerrar_espia)
    ajustes = settings_prueba.con(conectoma_fuente="stub", vtuber_ai_habilitado=True)

    async def principal() -> tuple[str, list[Any], Any]:
        orq, salida = _orquestador(ajustes)
        try:
            diags = await orq.iniciar(lanzar_tareas=False, imprimir_arranque=True)
            corteza = orq.corteza
            assert isinstance(corteza, CortezaMotora) and orq.avatar3d.reposo_externo is True
            return salida.getvalue(), diags, corteza
        finally:
            await orq.apagar()

    texto, diags, corteza = asyncio.run(principal())
    motor = next(d for d in diags if d.clave == "motor")
    assert motor.estado == EstadoModulo.EN_ESPERA and motor.nombre == "Corteza motora (avatar 3D)"
    lineas = texto.splitlines()
    i_avatar = next(i for i, linea in enumerate(lineas) if "Avatar 3D VTuber AI Studio" in linea)
    assert "Corteza motora (avatar 3D)" in lineas[i_avatar + 1] and "EN ESPERA" in lineas[i_avatar + 1]
    assert _PuenteFalso.cierres == ["motor", "avatar3d"]          # la corteza se cierra antes que el avatar
    assert corteza._cerrado


def test_main_tarea_supervisada_motor(settings_prueba: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import lobes.vtuber_ai_bridge as puente_mod

    monkeypatch.setattr(puente_mod, "VTuberAIBridge", _PuenteFalso)
    ajustes = settings_prueba.con(conectoma_fuente="stub", vtuber_ai_habilitado=True)

    async def principal() -> list[str]:
        orq, _salida = _orquestador(ajustes)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            orq._lanzar_tareas()
            await asyncio.sleep(0.3)
            return sorted(orq._tareas)
        finally:
            await orq.apagar()

    tareas = asyncio.run(principal())
    assert "motor" in tareas and "avatar3d" in tareas


@pytest.mark.parametrize(("ajustes", "detalle"), [
    ({"vtuber_ai_habilitado": True, "avatar_autonomo": False}, "deshabilitado"),
    ({"vtuber_ai_habilitado": False}, "sin avatar 3D"),
])
def test_main_corteza_apagada(settings_prueba: Any, monkeypatch: pytest.MonkeyPatch, ajustes: dict[str, Any],
                              detalle: str) -> None:
    import lobes.vtuber_ai_bridge as puente_mod

    monkeypatch.setattr(puente_mod, "VTuberAIBridge", _PuenteFalso)
    s = settings_prueba.con(conectoma_fuente="stub", **ajustes)

    async def principal() -> tuple[list[Any], Any]:
        orq, _salida = _orquestador(s)
        try:
            return await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False), orq.corteza
        finally:
            await orq.apagar()

    diags, corteza = asyncio.run(principal())
    motor = next(d for d in diags if d.clave == "motor")
    assert corteza is None and motor.estado == EstadoModulo.OFF and motor.detalle == detalle


# ---------------------------------------------------------------------------
# Modo escritorio (v0.2, docs/COMPANION.md §C1.4, §C1.11, §C2.10; grupo motor-escritorio)
# ---------------------------------------------------------------------------


class AvatarEscritorio(AvatarFalso):
    """AvatarFalso con la API de escritorio del puente: ``desktop()``, ``visores_escritorio_total`` y
    ``refrescar()``. Los ``desktop`` se guardan aparte (en el puente real viajan solos)."""

    def __init__(self, reloj: Reloj | None = None, *, overlays: int = 1, **kwargs: Any) -> None:
        super().__init__(reloj, **kwargs)
        self.visores_escritorio_total = overlays
        self.desktops: list[tuple[float, dict[str, Any]]] = []
        self.refrescos = 0

    def desktop(self, accion: str, **campos: Any) -> bool:
        if not self.en_linea or self.visores_escritorio_total <= 0:
            return False
        comando = sanear_desktop({**campos, "type": "desktop", "action": accion})
        if comando is None:
            return False
        self.desktops.append((self.reloj(), comando))
        return True

    def refrescar(self) -> None:
        self.refrescos += 1

    def acciones(self) -> list[str]:
        return [c["action"] for _t, c in self.desktops]

    def limpiar(self) -> None:
        super().limpiar()
        self.desktops.clear()


ACCIONES_AUTONOMAS = {"walk_to", "wander", "peek", "chase_cursor", "seek_user"}


def _capturar(bus: Any, nombre: str) -> list[dict[str, Any]]:
    eventos: list[dict[str, Any]] = []
    bus.suscribir(nombre, lambda e: eventos.append(dict(e.datos)))
    return eventos


def _evento_escritorio(bus: Any, nombre: str, **datos: Any) -> None:
    bus.publicar("escritorio.evento", {"nombre": nombre, "datos": datos}, origen="escritorio")


def _escritorio(settings: Any, bus: Any, *, semilla: int = 7, overlays: int = 1,
                **ajustes: Any) -> tuple[CortezaMotora, AvatarEscritorio, Reloj]:
    reloj = Reloj()
    avatar = AvatarEscritorio(reloj, overlays=overlays)
    return _corteza(settings, bus=bus, avatar=avatar, reloj=reloj, semilla=semilla, **ajustes), avatar, reloj


def _correr(corteza: CortezaMotora, reloj: Reloj, segundos: float, paso: float = cm.TICK_S) -> None:
    for _ in range(int(round(segundos / paso))):
        reloj.avanzar(paso)
        corteza.tick()


def test_saneador_de_desktop() -> None:
    assert "desktop" in cm.TIPOS_PERMITIDOS and "escritorio" in cm.CANALES_ATENCION
    assert sanear_comando({"type": "desktop", "action": "walk_to", "x": 0.5}) == \
        {"type": "desktop", "action": "walk_to", "x": 0.5}
    assert sanear_comando({"type": "desktop", "action": "peek", "edge": "left", "loop": True}) == \
        {"type": "desktop", "action": "peek", "edge": "left"}
    assert sanear_comando({"type": "desktop", "action": "walk_to", "x": 800, "unit": "px", "speed": 2}) == \
        {"type": "desktop", "action": "walk_to", "x": 800.0, "unit": "px", "speed": 2.0}
    # La visibilidad y la entrada/salida por el borde son del main de Electron: no son acciones desktop.
    for accion in ("show", "hide", "enter", "leave", "aparecer"):
        assert sanear_comando({"type": "desktop", "action": accion}) is None
    for malo in ({"action": "wander", "seconds": 60}, {"action": "wander", "seconds": -1},
                 {"action": "walk_to", "x": 1.2}, {"action": "walk_to"}, {"action": "peek", "edge": "arriba"},
                 {"action": "chase_cursor", "speed": 3.5}, {"action": "walk_to", "x": 0.4, "unit": "cm"}):
        assert sanear_comando({"type": "desktop", **malo}) is None, malo
    assert sanear_comando({"type": "desktop", "action": "wander", "seconds": 15})["seconds"] == 15.0


def test_con_overlay_visible_usa_desktop_y_oculto_no(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = ABURRIDA
    _correr(corteza, reloj, 1.0)
    _evento_escritorio(bus, "mostrada")
    assert corteza.escritorio_visible and corteza.contexto(reloj()).escritorio
    _correr(corteza, reloj, 600.0)
    acciones = avatar.acciones()
    assert len(acciones) >= 6 and set(acciones) <= ACCIONES_AUTONOMAS | {"stop"}
    assert {"wander", "walk_to"} & set(acciones) and len(set(acciones) - {"stop"}) >= 2
    # En el escritorio el visor ignora move x/z: nada de paseos de escenario, y desktop nunca va en un lote.
    assert not any(c["type"] == "move" and ("x" in c or "z" in c) for c in avatar.comandos())
    assert not any(c["type"] == "desktop" for c in avatar.comandos())
    assert all(c["x"] <= 1.0 for _t, c in avatar.desktops if "x" in c)
    assert all(c.get("seconds", 0) <= 15.0 for _t, c in avatar.desktops)
    # Oculta: conductas de escenario de la v0.1 (para el panel), ningún desktop.
    _evento_escritorio(bus, "ocultada")
    avatar.limpiar()
    _correr(corteza, reloj, 600.0)
    assert avatar.desktops == []
    assert any(c["type"] == "move" and c.get("walk") for c in avatar.comandos())


def test_conductas_de_escritorio_solo_con_overlay_cargado(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus, overlays=0)
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    assert avatar.refrescos == 1                                # pide al puente un sondeo inmediato
    assert not corteza.contexto(reloj()).escritorio
    _correr(corteza, reloj, 300.0)
    assert avatar.desktops == []
    # Sin la API de escritorio en el avatar (puente de la v0.1) tampoco: nunca lanza.
    viejo = AvatarFalso(reloj)
    corteza = _corteza(settings_prueba, bus=bus, avatar=viejo, reloj=reloj)
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 120.0)
    assert not any(c["type"] == "desktop" for c in viejo.comandos())


def test_escritorio_conectado_sin_overlay_pierde_la_visibilidad(settings_prueba: Any, bus: Any) -> None:
    corteza, _avatar, _reloj = _escritorio(settings_prueba, bus)
    _evento_escritorio(bus, "mostrada")
    assert corteza.escritorio_visible
    bus.publicar("escritorio.conectado", {"clientes": 1, "overlay": True, "hud": 1}, origen="escritorio")
    assert corteza.escritorio_visible                           # conectar no la muestra ni la oculta
    bus.publicar("escritorio.conectado", {"clientes": 0, "overlay": False, "hud": 0}, origen="escritorio")
    assert not corteza.escritorio_visible
    _evento_escritorio(bus, "mostrada")
    _evento_escritorio(bus, "ocultada")
    assert not corteza.escritorio_visible
    _evento_escritorio(bus, "inventado")                        # fuera de EVENTOS_ESCRITORIO: se ignora
    assert not corteza.escritorio_visible


def test_solicitud_retenida_hasta_mostrada(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = FRESCA
    _correr(corteza, reloj, 1.0)
    bus.publicar("motor.solicitud", {"conducta": "buscar_usuario", "motivo": "aburrida", "borde": None,
                                     "ttl_s": 20.0}, origen="main")
    assert corteza.solicitud is not None and avatar.refrescos == 0     # el puente ya conoce el overlay
    _correr(corteza, reloj, 5.0)                                # overlay oculto: espera
    assert avatar.desktops == [] and corteza.solicitud is not None
    _evento_escritorio(bus, "mostrada")                         # main la sacó: ahora sí
    reloj.avanzar(cm.TICK_S)
    corteza.tick()
    assert avatar.acciones() == ["seek_user"] and corteza.solicitud is None
    assert {"type": "look", "at": "camera", "head": 0.45} in [c for _t, e in avatar.envios
                                                               for c in (e if isinstance(e, list) else [e])] or \
        any(c["type"] == "look" for c in avatar.comandos())


def test_solicitud_caduca_con_log_debug(settings_prueba: Any, bus: Any, caplog: pytest.LogCaptureFixture) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza.tick()
    bus.publicar("motor.solicitud", {"conducta": "asomarse", "motivo": "curiosidad", "borde": "left",
                                     "ttl_s": 3.0}, origen="main")
    with caplog.at_level("DEBUG", logger="rams.motor"):
        _correr(corteza, reloj, 4.0)
    assert corteza.solicitud is None and "caducada" in caplog.text
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 1.0)
    assert "peek" not in avatar.acciones()
    # ttl_s fuera de rango se acota a 30 s.
    bus.publicar("motor.solicitud", {"conducta": "pasear", "motivo": "", "borde": None, "ttl_s": 999}, origen="main")
    assert corteza.solicitud is not None and corteza.solicitud.vence <= reloj() + cm.SOLICITUD_TTL_MAX_S


@pytest.mark.parametrize(("conducta", "borde", "esperado"), [
    ("asomarse", "left", {"action": "peek", "edge": "left"}),
    ("asomarse", None, {"action": "peek", "edge": "right"}),
    ("buscar_usuario", None, {"action": "seek_user"}),
    ("pasear", None, {"action": "wander"}),
])
def test_solicitudes_de_main(settings_prueba: Any, bus: Any, conducta: str, borde: str | None,
                             esperado: dict[str, Any]) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    corteza.tick()
    avatar.limpiar()
    bus.publicar("motor.solicitud", {"conducta": conducta, "motivo": "iniciativa", "borde": borde, "ttl_s": 20},
                 origen="main")
    reloj.avanzar(cm.TICK_S)
    corteza.tick()
    primero = avatar.desktops[0][1]
    assert all(primero.get(k) == v for k, v in esperado.items()), primero
    if conducta == "pasear":
        assert 0 < primero["seconds"] <= 15.0


def test_solicitud_esconderse_publica_la_orden(settings_prueba: Any, bus: Any) -> None:
    ordenes = _capturar(bus, "escritorio.orden")
    corteza, _avatar, reloj = _escritorio(settings_prueba, bus)
    _evento_escritorio(bus, "mostrada")
    bus.publicar("motor.solicitud", {"conducta": "esconderse", "motivo": "usuario", "borde": None, "ttl_s": 5},
                 origen="main")
    assert ordenes == [{"accion": "esconderse"}]
    assert not corteza.contexto(reloj()).escritorio              # saliendo: nada de escritorio hasta "ocultada"
    bus.publicar("motor.solicitud", {"conducta": "bailar", "motivo": "", "borde": None, "ttl_s": 5}, origen="main")
    assert ordenes == [{"accion": "esconderse"}] and corteza.solicitud is None


def test_solicitud_espera_al_silencio(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    _evento_escritorio(bus, "mostrada")
    corteza.tick()
    avatar.hablando = True
    bus.publicar("motor.solicitud", {"conducta": "buscar_usuario", "motivo": "x", "borde": None, "ttl_s": 20},
                 origen="main")
    _correr(corteza, reloj, 6.0)
    assert "seek_user" not in avatar.acciones()
    avatar.hablando = False
    _correr(corteza, reloj, 0.5)
    assert "seek_user" in avatar.acciones()


def test_no_molestar_se_esconde_una_vez_y_sin_travesuras(settings_prueba: Any, bus: Any) -> None:
    ordenes = _capturar(bus, "escritorio.orden")
    travesuras = _capturar(bus, "motor.travesura")
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = replace(ABURRIDA, boredom=0.95)
    bus.publicar("motor.travesuras", {"activas": True, "hasta_ts": None}, origen="main")
    _evento_escritorio(bus, "mostrada")
    bus.publicar("pc.no_molestar", {"activo": True, "motivo": "pantalla_completa"}, origen="main")
    assert ordenes == [{"accion": "esconderse"}]
    bus.publicar("pc.no_molestar", {"activo": True, "motivo": "pantalla_completa"}, origen="main")
    _evento_escritorio(bus, "ocultada")
    _evento_escritorio(bus, "mostrada")                         # el usuario la llamó: no se vuelve a esconder
    avatar.limpiar()
    _correr(corteza, reloj, 1800.0, paso=1.0)
    assert ordenes == [{"accion": "esconderse"}]                # una vez por activación
    assert travesuras == []
    assert set(avatar.acciones()) <= {"stop"}                    # ninguna conducta de escritorio
    # Nueva activación → otra vez.
    bus.publicar("pc.no_molestar", {"activo": False, "motivo": ""}, origen="main")
    bus.publicar("pc.no_molestar", {"activo": True, "motivo": "juego"}, origen="main")
    assert ordenes == [{"accion": "esconderse"}] * 2            # nunca "aparecer"


def test_se_esconde_por_fatiga_sostenida(settings_prueba: Any, bus: Any) -> None:
    ordenes = _capturar(bus, "escritorio.orden")
    corteza, _avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = HomeostaticState(arousal=0.3, fatiga=0.9, boredom=0.3)
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 9 * 60.0, paso=1.0)
    assert ordenes == []
    _correr(corteza, reloj, 70.0, paso=1.0)
    assert ordenes == [{"accion": "esconderse"}]
    _evento_escritorio(bus, "ocultada")
    _evento_escritorio(bus, "mostrada")                         # la llaman otra vez, aún cansada
    _correr(corteza, reloj, 15 * 60.0, paso=1.0)
    assert len(ordenes) == 1                                    # una vez por episodio
    corteza._estado = HomeostaticState(arousal=0.5, fatiga=0.4)  # descansa…
    _correr(corteza, reloj, 5.0, paso=1.0)
    corteza._estado = HomeostaticState(arousal=0.3, fatiga=0.85)  # …y vuelve a cansarse 10 min
    _correr(corteza, reloj, 10 * 60.0 + 5.0, paso=1.0)
    assert ordenes == [{"accion": "esconderse"}] * 2
    # Con la ventana oculta no publica nada (no hay nada que esconder).
    oculta, _a, r2 = _escritorio(settings_prueba, bus)
    oculta._estado = HomeostaticState(fatiga=0.95)
    n = len(ordenes)
    _correr(oculta, r2, 15 * 60.0, paso=1.0)
    assert len(ordenes) == n


def test_cansancio_acumulado_oculta_no_la_echa_al_llamarla(settings_prueba: Any, bus: Any) -> None:
    """Solo cuenta la fatiga a la vista (§C1.11: "solo reaparece si la llaman"): tras 15 min cansada y
    oculta, main la saca para buscar al usuario (iniciativa o recordatorio "búscame") → lo busca y se queda;
    se esconde solo tras otros 10 min a la vista, aún cansada."""
    ordenes = _capturar(bus, "escritorio.orden")
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = HomeostaticState(arousal=0.3, fatiga=0.85, boredom=0.3)
    _correr(corteza, reloj, 15 * 60.0, paso=1.0)               # oculta y cansada (de noche)
    assert ordenes == []
    # main (_salir_a_buscar): aparecer (lo hace Electron) + motor.solicitud; Electron responde "mostrada".
    bus.publicar("motor.solicitud", {"conducta": "buscar_usuario", "motivo": "recordatorio", "borde": "left",
                                     "ttl_s": 20.0}, origen="main")
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 5 * 60.0, paso=1.0)
    assert ordenes == [] and avatar.acciones()[0] == "seek_user" and corteza.solicitud is None
    _correr(corteza, reloj, 5 * 60.0 + 5.0, paso=1.0)          # 10 min a la vista, aún cansada
    assert ordenes == [{"accion": "esconderse"}]
    # Si la ocultan antes de los 10 min, el reloj vuelve a empezar al mostrarse.
    _evento_escritorio(bus, "ocultada")
    corteza._estado = HomeostaticState(arousal=0.5, fatiga=0.4)
    _correr(corteza, reloj, 2.0, paso=1.0)                     # descansa: nuevo episodio
    corteza._estado = HomeostaticState(arousal=0.3, fatiga=0.9)
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 8 * 60.0, paso=1.0)
    _evento_escritorio(bus, "ocultada")
    _correr(corteza, reloj, 5 * 60.0, paso=1.0)
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 8 * 60.0, paso=1.0)                # 16 min a la vista en dos tramos de 8
    assert len(ordenes) == 1
    _correr(corteza, reloj, 2 * 60.0 + 5.0, paso=1.0)          # 10 min seguidos
    assert len(ordenes) == 2


def test_travesuras_solo_con_permiso_y_con_tope(settings_prueba: Any, bus: Any) -> None:
    travesuras = _capturar(bus, "motor.travesura")
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = replace(ABURRIDA, boredom=0.9, valencia=0.2)
    _evento_escritorio(bus, "mostrada")
    _correr(corteza, reloj, 3600.0, paso=5.0)                   # travesuras apagadas (defecto): nada
    assert travesuras == []
    bus.publicar("motor.travesuras", {"activas": True, "hasta_ts": None}, origen="main")
    instantes: list[float] = []
    bus.suscribir("motor.travesura", lambda _e: instantes.append(reloj()))
    _correr(corteza, reloj, 24 * 3600.0, paso=10.0)
    assert 1 <= len(travesuras) <= cm.TRAVESURAS_MAX_DIA
    assert all(b - a >= cm.TRAVESURA_MIN_ENTRE_S for a, b in zip(instantes, instantes[1:]))
    for t in travesuras:
        assert set(t) == {"patron", "amplitud_px", "segundos"}
        assert t["patron"] in cm.PATRONES_TRAVESURA
        assert isinstance(t["amplitud_px"], int) and 5 <= t["amplitud_px"] <= 40
        assert 0 < t["segundos"] <= 0.6
    # La acompaña con el cuerpo (mira al ratón), sin tocarlo.
    assert {"type": "look", "at": "mouse", "head": 0.5} in avatar.comandos()


@pytest.mark.parametrize("veto", ["aburrimiento", "hablando", "oculta", "no_molestar", "desactivadas", "usuario"])
def test_travesuras_vetadas(settings_prueba: Any, bus: Any, veto: str) -> None:
    travesuras = _capturar(bus, "motor.travesura")
    corteza, avatar, reloj = _escritorio(settings_prueba, bus, travesura_amplitud_max_px=20)
    corteza._estado = replace(ABURRIDA, boredom=0.5 if veto == "aburrimiento" else 0.9)
    bus.publicar("motor.travesuras", {"activas": veto != "desactivadas", "hasta_ts": None}, origen="main")
    if veto != "oculta":
        _evento_escritorio(bus, "mostrada")
    if veto == "no_molestar":
        bus.publicar("pc.no_molestar", {"activo": True, "motivo": "usuario"}, origen="main")
        _evento_escritorio(bus, "mostrada")
    avatar.hablando = veto == "hablando"
    for _ in range(120):
        if veto == "usuario":                                   # el usuario está escribiendo en el chat
            bus.publicar("usuario.entrada", {"texto": "hola", "canal": "escritorio"}, origen="main")
        reloj.avanzar(5.0)
        corteza.tick()
    assert travesuras == []
    asyncio.run(corteza.cerrar())
    # Control: sin el veto sí la propone (y respeta la amplitud máxima de la configuración).
    control, _avatar, r2 = _escritorio(settings_prueba, bus, travesura_amplitud_max_px=20)
    control._estado = replace(ABURRIDA, boredom=0.9)
    bus.publicar("motor.travesuras", {"activas": True, "hasta_ts": None}, origen="main")
    bus.publicar("pc.no_molestar", {"activo": False, "motivo": ""}, origen="main")
    _evento_escritorio(bus, "mostrada")
    _correr(control, r2, 2.0)
    assert travesuras and all(t["amplitud_px"] <= 20 for t in travesuras)


def test_mirada_al_usuario_como_mucho_una_por_segundo(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza.tick()
    corteza._aplazar(1e9)                                       # sin conductas propias: solo la mirada
    avatar.limpiar()
    for i in range(50):                                         # 5 muestras/s durante 10 s, moviéndose
        bus.publicar("vision.mirada", {"x": 0.4 * (i % 2) - 0.2, "y": 1.4, "z": 2.0 + 0.01 * i,
                                       "confianza": 0.9}, origen="vision")
        reloj.avanzar(0.2)
        corteza.tick()
    puntos = [c for c in avatar.comandos() if c["type"] == "look" and c["at"] == "point"]
    assert 5 <= len(puntos) <= 10
    assert all(c["head"] == cm.MIRADA_CABEZA and -3 <= c["x"] <= 3 and 0.3 <= c["y"] <= 2.2 for c in puntos)
    # Punto casi quieto (< 0,15 m): no repite el look.
    avatar.limpiar()
    for i in range(25):
        bus.publicar("vision.mirada", {"x": 0.2 + 0.001 * i, "y": 1.4, "z": 2.5, "confianza": 0.9}, origen="vision")
        reloj.avanzar(0.2)
        corteza.tick()
    assert len([c for c in avatar.comandos() if c["type"] == "look"]) <= 1
    # Confianza baja o muestra vieja: nada.
    avatar.limpiar()
    for i in range(25):
        bus.publicar("vision.mirada", {"x": -1.0 + 0.3 * (i % 2), "y": 1.2, "z": 2.0, "confianza": 0.5},
                     origen="vision")
        reloj.avanzar(0.2)
        corteza.tick()
    assert not any(c["type"] == "look" for c in avatar.comandos())
    bus.publicar("vision.mirada", {"x": 1.5, "y": 1.2, "z": 2.0, "confianza": 0.95}, origen="vision")
    reloj.avanzar(cm.MIRADA_RECIENTE_S + 1.0)
    corteza.tick()
    assert not any(c["type"] == "look" for c in avatar.comandos())
    errores = bus.estadisticas()["errores"]
    bus.publicar("vision.mirada", {"x": "basura", "y": 1.2, "z": 2.0, "confianza": 0.95}, origen="vision")
    bus.publicar("vision.rostro", {"presente": "sí"}, origen="vision")
    assert bus.estadisticas()["errores"] == errores


def test_mirada_respeta_el_tope_de_comandos(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus, avatar_actividad=3.0)
    corteza._estado = EUFORICA
    for i in range(int(5 * 60 / 0.2)):                          # 5 min de mirada a 5 Hz con conductas
        bus.publicar("vision.mirada", {"x": 1.0 * (i % 2) - 0.5, "y": 1.4, "z": 2.0, "confianza": 0.9},
                     origen="vision")
        reloj.avanzar(0.2)
        corteza.tick()
    tiempos = [(t, len(c) if isinstance(c, list) else 1) for t, c in avatar.envios]
    assert any(c["type"] == "look" and c["at"] == "point" for c in avatar.comandos())
    for t_fin, _n in tiempos:
        assert sum(n for t, n in tiempos if t_fin - 60.0 < t <= t_fin) <= cm.LIMITE_COMANDOS_MIN


def test_mirada_con_sub_tope_no_ahoga_las_conductas_de_escritorio(settings_prueba: Any, bus: Any) -> None:
    """Con la visión continua y un usuario que se mueve mucho (> 0,15 m cada segundo), la mirada usa su propio
    sub-tope (``MIRADA_MAX_MIN``/min): lo sigue a vistazos y las conductas autónomas de escritorio (salir a
    caminar por la pantalla, asomarse…) no se apagan (≥ 50 % de las que hace sin visión)."""
    def simular(con_vision: bool, semilla: int) -> tuple[list[float], int]:
        corteza, avatar, reloj = _escritorio(settings_prueba, bus, semilla=semilla)
        corteza._estado = ABURRIDA
        _evento_escritorio(bus, "mostrada")
        for i in range(int(600 / 0.2)):
            reloj.avanzar(0.2)
            if con_vision:
                bus.publicar("vision.mirada", {"x": 0.4 if (i // 5) % 2 else -0.4, "y": 1.2, "z": 3.0,
                                               "confianza": 0.9}, origen="vision")
            corteza.tick()
        miradas = [t for t, envio in avatar.envios for c in (envio if isinstance(envio, list) else [envio])
                   if c["type"] == "look" and c.get("at") == "point"]
        conductas = sum(1 for a in avatar.acciones() if a != "stop")
        asyncio.run(corteza.cerrar())
        return miradas, conductas

    for semilla in (3, 7):
        _sin, base = simular(False, semilla)
        miradas, conductas = simular(True, semilla)
        assert base >= 6 and conductas >= 0.5 * base, (semilla, base, conductas)
        # La sigue (casi todo el sub-tope) sin pasarse: las de las conductas (asomarse, buscarlo) aparte.
        assert 0.8 * 10 * cm.MIRADA_MAX_MIN <= len(miradas) <= 10 * cm.MIRADA_MAX_MIN + cm.MIRADA_RAFAGA + conductas
        for t_fin in miradas:
            assert sum(1 for t in miradas if t_fin - 60.0 < t <= t_fin) <= cm.MIRADA_MAX_MIN + cm.MIRADA_RAFAGA + 3


def test_rostro_frontal_mira_al_usuario(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza.tick()
    corteza._aplazar(1e9)
    avatar.limpiar()
    rostro = {"presente": True, "n": 1, "dist_m": 0.6, "frontal": True, "atencion": 0.8}
    bus.publicar("vision.rostro", rostro, origen="vision")
    reloj.avanzar(cm.TICK_S)
    corteza.tick()
    assert {"type": "look", "at": "camera", "head": 0.35} in avatar.comandos()
    avatar.limpiar()
    bus.publicar("vision.rostro", {**rostro, "frontal": False}, origen="vision")
    bus.publicar("vision.rostro", rostro, origen="vision")      # otro flanco antes de 10 s: nada
    _correr(corteza, reloj, 1.0)
    assert not any(c["type"] == "look" for c in avatar.comandos())


def test_atencion_por_chat_de_escritorio_sin_acercarse(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._pos, corteza._rot = (0.9, -0.5), 20.0
    corteza._estado = ABURRIDA
    corteza.tick()
    avatar.limpiar()
    bus.publicar("usuario.entrada", {"texto": "hola Kurisu", "canal": "escritorio"}, origen="main")
    reloj.avanzar(cm.TICK_S)
    corteza.tick()
    comandos = avatar.comandos()
    assert comandos[0] == {"type": "look", "at": "mouse", "head": 0.45}
    assert {"type": "move", "rotation": 0.0, "walk": False, "duration": 0.5} in comandos   # de frente
    _correr(corteza, reloj, 15.0)
    moves = [c for c in avatar.comandos() if c["type"] == "move"]
    assert moves and not any("x" in c or "z" in c for c in moves)                       # sin acercarse
    ctx = corteza.contexto(reloj())
    assert ctx.desde_usuario_s < cm.ATENCION_S and "pasear" not in corteza.pesos_base(ABURRIDA, ctx)
    # Con el overlay visible, una entrada por la terminal tampoco la hace caminar (x/z) ni salir de paseo.
    _evento_escritorio(bus, "mostrada")
    avatar.limpiar()
    bus.publicar("usuario.entrada", {"texto": "hola", "canal": "terminal"}, origen="main")
    _correr(corteza, reloj, 15.0)
    assert not any(c["type"] == "move" and ("x" in c or "z" in c) for c in avatar.comandos())
    assert set(avatar.acciones()) <= {"stop"}


def _moves_de_escenario(avatar: AvatarFalso) -> list[dict[str, Any]]:
    return [c for c in avatar.comandos() if c["type"] == "move" and ("x" in c or "z" in c or c.get("walk"))]


@pytest.mark.parametrize("caso", ["no_molestar", "puente_sin_overlay"])
def test_visible_sin_desktop_no_camina_por_el_escenario(settings_prueba: Any, bus: Any, caso: str) -> None:
    """Ventana visible pero sin poder emitir ``desktop`` (la llamaron con "no molestar" activo, o el puente
    aún no conoce el overlay): el cuerpo está en el overlay, que ignora x/z pero no ``walk`` → nada de paseos
    de escenario (andaría en el sitio), ni con la atención por la terminal; sigue viva en el sitio."""
    corteza, avatar, reloj = _escritorio(settings_prueba, bus, overlays=0 if caso == "puente_sin_overlay" else 1)
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    if caso == "no_molestar":
        bus.publicar("pc.no_molestar", {"activo": True, "motivo": "pantalla_completa"}, origen="main")
        _evento_escritorio(bus, "ocultada")
        _evento_escritorio(bus, "mostrada")                     # el usuario la vuelve a llamar
    ctx = corteza.contexto(reloj())
    assert ctx.en_escritorio and not ctx.escritorio and ctx.sin_escenario
    assert not {"pasear", "volver_al_centro"} & set(corteza.pesos_base(ABURRIDA, replace(ctx, posicion=(0.9, 0.3))))
    assert corteza.plan_atencion(ABURRIDA, replace(ctx, posicion=(0.9, -0.5))).conducta == "atencion_escritorio"
    avatar.limpiar()
    for minuto in range(10):
        if minuto % 3 == 0:
            bus.publicar("usuario.entrada", {"texto": "hola", "canal": "terminal"}, origen="main")
        _correr(corteza, reloj, 60.0)
    assert _moves_de_escenario(avatar) == []
    # Tampoco el giro hacia el rumbo de un paseo (de espaldas): solo giros en el sitio, de frente al público.
    assert all(abs(c["rotation"]) <= cm.GIRO_MAX_QUIETA for c in avatar.comandos() if c["type"] == "move")
    assert set(avatar.acciones()) <= {"stop"}
    assert len({c["type"] for c in avatar.comandos()} - {"move"}) >= 2   # miradas, caras, posturas en el sitio
    # Oculta, vuelve el escenario de la v0.1 (para el panel).
    _evento_escritorio(bus, "ocultada")
    avatar.limpiar()
    _correr(corteza, reloj, 600.0)
    assert any(c["type"] == "move" and c.get("walk") for c in avatar.comandos())


def test_al_salir_y_al_entrar_por_el_borde_no_gira_ni_decide(settings_prueba: Any, bus: Any) -> None:
    """Mientras Electron la hace salir (``esconderse`` → ``ocultada``) o entrar (``mostrada``) caminando por el
    borde, ningún ``move`` (pisaría la orientación de esa caminata: el visor gira el cuerpo con el mismo
    ``moveAvatar``) ni conducta nueva; la conducta en curso se corta. La mirada y la cara, sí."""
    for semilla in range(40):
        corteza, avatar, reloj = _escritorio(settings_prueba, bus, semilla=semilla)
        corteza._estado = ABURRIDA
        _evento_escritorio(bus, "mostrada")
        _correr(corteza, reloj, 30.0)
        assert corteza._esconderse("prueba")
        avatar.limpiar()
        _correr(corteza, reloj, 4.0)
        assert corteza.plan_actual is None, semilla               # cortada y sin conductas nuevas
        bus.publicar("usuario.entrada", {"texto": "espera", "canal": "terminal"}, origen="main")
        avatar.hablando = semilla % 2 == 0                      # o empieza a hablar mientras sale
        _correr(corteza, reloj, 6.0)
        avatar.hablando = False
        _correr(corteza, reloj, 4.0)
        assert not any(c["type"] == "move" for c in avatar.comandos()), (semilla, avatar.comandos())
        assert set(avatar.acciones()) == set(), semilla          # ni desktop stop: la salida es de Electron
        _evento_escritorio(bus, "ocultada")
        asyncio.run(corteza.cerrar())
    # Entrada: con un paseo de escenario en curso (para el panel) la vuelven a mostrar.
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = ABURRIDA
    corteza.tick()
    corteza._aplazar(reloj() + 1e9)
    plan = corteza.construir("pasear", ABURRIDA, corteza.contexto(reloj()))
    assert plan is not None and corteza.ejecutar(plan) > 0
    _correr(corteza, reloj, 1.0)
    assert any(c["type"] == "move" and c.get("walk") for c in avatar.comandos())   # ya camina
    corteza._memoria = replace(corteza.memoria, proxima=-math.inf)
    _evento_escritorio(bus, "mostrada")
    avatar.limpiar()
    _correr(corteza, reloj, cm.GRACIA_ENTRADA_S - 0.25)
    assert corteza.plan_actual is None and not any(c["type"] == "move" for c in avatar.comandos())
    assert {"type": "look", "at": "camera", "head": 0.35} in avatar.comandos()     # finales sin giro: sí
    _correr(corteza, reloj, 120.0)
    assert _moves_de_escenario(avatar) == [] and avatar.acciones()                # ya decide (en el escritorio)


def test_chat_abierto_es_atencion_de_escritorio(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    corteza.tick()
    avatar.limpiar()
    _evento_escritorio(bus, "chat_abierto")
    reloj.avanzar(cm.TICK_S)
    corteza.tick()
    assert avatar.comandos()[0] == {"type": "look", "at": "mouse", "head": 0.45}
    assert corteza.contexto(reloj()).desde_usuario_s < cm.ATENCION_S
    _correr(corteza, reloj, 10.0)
    assert set(avatar.acciones()) <= {"stop"}                    # mientras le escribe no se va de paseo


@pytest.mark.parametrize(("nombre", "datos", "esperado"), [
    ("toque", {}, {"surprised"}),
    ("soltada", {"altura": 0.8}, {"surprised", "angry"}),
    ("soltada", {"altura": 0.1}, {"surprised"}),
])
def test_toque_y_soltada_reaccionan(settings_prueba: Any, bus: Any, nombre: str, datos: dict[str, Any],
                                    esperado: set[str]) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    _evento_escritorio(bus, "mostrada")
    corteza.tick()
    corteza._aplazar(1e9)                                       # sin conductas propias: solo la reacción
    avatar.limpiar()
    _evento_escritorio(bus, nombre, **datos)
    _evento_escritorio(bus, nombre, **datos)                    # segundo toque inmediato: una sola reacción
    _correr(corteza, reloj, 3.5)
    emociones = {n for c in avatar.comandos() if c["type"] == "expression" for n in c.get("values", {})}
    assert esperado <= emociones
    sobresaltos = [c for c in avatar.comandos() if c["type"] == "expression" and "surprised" in c.get("values", {})]
    assert len(sobresaltos) == 1
    gestos = [c["name"] for c in avatar.comandos() if c["type"] == "gesture"]
    assert gestos == (["surprise"] if datos.get("altura", 0) > cm.SOLTADA_ALTA else [])
    assert {"type": "look", "at": "camera", "head": 0.35} in avatar.comandos()   # termina mirando al usuario


def test_habla_detiene_la_locomocion_de_escritorio_y_calla(settings_prueba: Any, bus: Any) -> None:
    corteza, avatar, reloj = _escritorio(settings_prueba, bus)
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    corteza.tick()
    corteza._aplazar(reloj() + 60.0)                            # que no elija otra conducta entretanto
    ctx = corteza.contexto(reloj())
    plan = corteza.construir("perseguir_cursor", ABURRIDA, ctx)
    assert plan is not None and corteza.ejecutar(plan) > 0
    _correr(corteza, reloj, 1.0)
    assert avatar.acciones() == ["chase_cursor"]
    avatar.limpiar()
    avatar.hablando = True
    reloj.avanzar(cm.TICK_S)
    corteza.tick()                                              # empieza el habla: se detiene ya
    assert avatar.acciones() == ["stop"]
    assert {"type": "look", "at": "camera", "head": 0.35} in avatar.comandos()
    avatar.limpiar()
    bus.publicar("motor.solicitud", {"conducta": "pasear", "motivo": "", "borde": None, "ttl_s": 30},
                 origen="main")
    _correr(corteza, reloj, 20.0)                               # 20 s hablando: silencio total
    assert avatar.envios == [] and avatar.desktops == []
    avatar.hablando = False
    _correr(corteza, reloj, 1.0)
    assert "wander" in avatar.acciones()                        # la solicitud retenida sale tras el habla


def test_conductas_de_escritorio_construidas_y_acotadas(settings_prueba: Any) -> None:
    corteza = _corteza(settings_prueba)
    sin = ContextoMotor(huesos=HUESOS, expresiones=EXPRESIONES)
    con = replace(sin, escritorio=True)
    for conducta in cm.CONDUCTAS_ESCRITORIO:
        assert corteza.construir(conducta, ABURRIDA, sin) is None   # sin ventana visible no aplican
    assert not set(cm.CONDUCTAS_ESCRITORIO) & set(corteza.pesos_base(ABURRIDA, sin))
    pesos = corteza.pesos_base(ABURRIDA, replace(con, desde_usuario_s=600.0))
    assert {"pasear_escritorio", "perseguir_cursor", "buscar_usuario"} <= set(pesos)
    assert "pasear" not in pesos and "volver_al_centro" not in pesos
    assert corteza.preferidas(ABURRIDA, replace(con, desde_usuario_s=600.0), 1) == ["pasear_escritorio"]
    acciones: Counter[str] = Counter()
    for semilla in range(40):
        c = _corteza(settings_prueba, semilla=semilla)
        for conducta in cm.CONDUCTAS_ESCRITORIO:
            for estado in (ABURRIDA, CURIOSA, FATIGADA, EUFORICA):
                plan = c.construir(conducta, estado, con)
                assert plan is not None
                desktop = [x for x in plan.comandos() if x["type"] == "desktop"]
                assert len(desktop) == 1 and sanear_desktop(desktop[0]) == desktop[0]
                acciones[desktop[0]["action"]] += 1
                assert all(x["type"] in cm.TIPOS_PERMITIDOS for x in plan.comandos())
    assert set(acciones) == ACCIONES_AUTONOMAS
    # La brújula E-PG elige el lado de la pantalla del walk_to: rumbo 90° → derecha, 270° → izquierda.
    xs: dict[float, list[float]] = {90.0: [], 270.0: []}
    for semilla in range(60):
        c = _corteza(settings_prueba, semilla=semilla)
        for angulo, lista in xs.items():
            plan = c.construir("pasear_escritorio", FRESCA, con, brujula=cm.Brujula(angulo, 1.0, angulo))
            lista += [x["x"] for x in (plan.comandos() if plan else []) if x.get("action") == "walk_to"]
    assert xs[90.0] and xs[270.0] and min(xs[90.0]) > 0.5 > max(xs[270.0])


def test_sin_escritorio_no_se_suscribe_ni_usa_desktop(settings_prueba: Any, bus: Any) -> None:
    ordenes = _capturar(bus, "escritorio.orden")
    antes = bus.estadisticas()["suscriptores"]
    corteza, avatar, reloj = _escritorio(settings_prueba, bus, escritorio_habilitado=False)
    assert bus.estadisticas()["suscriptores"] - antes == 9      # 7 de la v0.1 + 2 de la visión
    corteza._estado = ABURRIDA
    _evento_escritorio(bus, "mostrada")
    bus.publicar("motor.solicitud", {"conducta": "buscar_usuario", "motivo": "", "borde": None, "ttl_s": 20},
                 origen="main")
    bus.publicar("pc.no_molestar", {"activo": True, "motivo": "juego"}, origen="main")
    _correr(corteza, reloj, 300.0)
    assert avatar.desktops == [] and ordenes == [] and not corteza.escritorio_visible
    assert "escritorio" not in corteza.diagnostico().detalle


def test_estado_inicial_desde_el_bus(settings_prueba: Any, bus: Any) -> None:
    bus.publicar("motor.travesuras", {"activas": True, "hasta_ts": None}, origen="main")
    bus.publicar("pc.no_molestar", {"activo": True, "motivo": "usuario"}, origen="main")
    ordenes = _capturar(bus, "escritorio.orden")
    corteza, _avatar, _reloj = _escritorio(settings_prueba, bus)
    assert corteza._travesuras_activas and corteza._no_molestar
    assert ordenes == []                                        # construirse no publica nada


def test_nunca_llama_a_control_pc_ni_publica_aparecer() -> None:
    """La corteza solo PROPONE (motor.travesura) y solo pide esconderse: nunca toca el PC."""
    import ast
    import inspect

    arbol = ast.parse(inspect.getsource(cm))
    prohibidos = {"empujon_cursor", "escribir_texto", "abrir_destino", "buscar_en", "prerrellenar",
                  "puede_travesura", "SetCursorPos", "SendInput"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Import, ast.ImportFrom)):
            modulos = [getattr(nodo, "module", None) or ""] + [a.name for a in nodo.names]
            assert not any("control_pc" in m or m in ("ctypes", "ControlPC") for m in modulos), modulos
        if isinstance(nodo, ast.Attribute):
            assert nodo.attr not in prohibidos, nodo.attr
        if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
            assert nodo.value not in ("aparecer", "abrir_chat", "notificar")
    # Las únicas publicaciones de la corteza: diagnóstico, esconderse y travesura.
    publicados = {n.args[0].value for n in ast.walk(arbol) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "publicar"
                  and n.args and isinstance(n.args[0], ast.Constant)}
    assert publicados == {"sistema.modulo", "escritorio.orden", "motor.travesura"}


def test_puente_real_desktop_siempre_solo(settings_prueba: Any, bus: Any, correr: Any) -> None:
    """Con el VTuberAIBridge de verdad (httpx.MockTransport): los desktop salen solos, nunca en un lote."""
    httpx = pytest.importorskip("httpx")
    import json

    from lobes.vtuber_ai_bridge import VTuberAIBridge

    recibidos: list[Any] = []

    def servidor(request: Any) -> Any:
        if request.url.path == "/api/state":
            return httpx.Response(200, json={"viewers": 2, "ready": True, "model": "Kurisu.vrm",
                                             "expressionsAvailable": sorted(EXPRESIONES),
                                             "bonesAvailable": sorted(HUESOS), "current": {"camera": "full"},
                                             "desktopViewers": 1, "desktopViewersTotal": 1, "viewerSeq": 2})
        cuerpo = json.loads(request.content)
        recibidos.append(cuerpo)
        lote = cuerpo if isinstance(cuerpo, list) else [cuerpo]
        if len(lote) > 1 and any(c.get("type") == "desktop" for c in lote):
            return httpx.Response(400, json={"ok": False, "error": "lote mixto"})
        return httpx.Response(200, json={"ok": True, "delivered": 2, "results": []})

    async def escenario() -> None:
        cliente = httpx.AsyncClient(transport=httpx.MockTransport(servidor))
        puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True), bus, cliente=cliente)
        await puente.iniciar()
        reloj = Reloj()
        corteza = CortezaMotora(settings_prueba.con(avatar_actividad=3.0), bus, puente, semilla=5, reloj=reloj)
        corteza._estado = ABURRIDA
        _evento_escritorio(bus, "mostrada")
        bus.publicar("motor.solicitud", {"conducta": "buscar_usuario", "motivo": "x", "borde": None, "ttl_s": 20},
                     origen="main")
        for _ in range(int(240 / cm.TICK_S)):
            reloj.avanzar(cm.TICK_S)
            corteza.tick()
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        await corteza.cerrar()
        await puente.cerrar()
        await cliente.aclose()

    correr(escenario())
    desktops = [c for c in recibidos if isinstance(c, dict) and c.get("type") == "desktop"]
    assert desktops and desktops[0] == {"type": "desktop", "action": "seek_user"}
    assert len(desktops) >= 3
    for envio in recibidos:
        if isinstance(envio, list):
            assert not any(c["type"] == "desktop" for c in envio)
        for c in envio if isinstance(envio, list) else [envio]:
            assert c["type"] in cm.TIPOS_PERMITIDOS | {"theme"}
