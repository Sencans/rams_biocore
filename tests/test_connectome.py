"""Tests del conectoma (core/sincan_connectome.py) y del cargador FlyWire (core/flywire_loader.py).

Cubren los 8 casos de §10.6 y los de §10.7 (§12.3). Sin red ni hardware y sin pytest-asyncio:
las pruebas asíncronas usan ``asyncio.run``. Los casos que necesitan numpy usan
``pytest.importorskip("numpy")``.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.contracts import CAMPOS, DIVERGENCIA_STEINS_GATE, EstadoModulo, HomeostaticState
from core.flywire_loader import POBLACIONES, cargar_red_flywire, estadisticas, main
from core.sincan_connectome import (
    VERSION_RED,
    X_EQ_DEFECTO,
    MedidorDivergencia,
    MotorIniciativa,
    SincanConnectome,
    ciclo_circadiano,
    patron_pn,
)

SG = DIVERGENCIA_STEINS_GATE
DT = 0.05
BOGOTA = timezone(timedelta(hours=-5))


def _reloj_fijo(hora: int = 10) -> float:
    """Marca de tiempo fija (America/Bogota) para que los procesos S y C sean deterministas."""
    return datetime(2026, 10, 16, hora, 0, tzinfo=BOGOTA).timestamp()


def _estado_con_m(m: float) -> tuple[float, float, float, float]:
    """Estado (A, Fo, B, F) a distancia normalizada ``m`` de X_EQ_DEFECTO, del lado β (solo fatiga)."""
    a, fo, b, f = X_EQ_DEFECTO
    return a, fo, b, f + m * MedidorDivergencia.M_MAX / 1.2


def _rangos_validos(e: HomeostaticState) -> None:
    for valor in (e.arousal, e.focus, e.boredom, e.fatiga, e.curiosidad, e.presion_sueno):
        assert 0.0 <= valor <= 1.0
    assert -1.0 <= e.valencia <= 1.0
    assert 0.0 <= e.divergencia <= 3.999999
    assert e.campo in CAMPOS
    assert 0.0 <= e.rumbo_deg < 360.0
    assert e.modo in ("vigilia", "reposo")
    assert all(0.0 <= v <= 1.0 for v in e.bump)


# ---------------------------------------------------------------------------
# 1. MedidorDivergencia (Python puro)
# ---------------------------------------------------------------------------


def test_medidor_equilibrio_exacto() -> None:
    medidor = MedidorDivergencia()
    for _ in range(70):   # 3,5 s simulados
        d, campo = medidor.actualizar(*X_EQ_DEFECTO, DT)
    assert d == 1.048596
    assert campo == "steins_gate"
    assert medidor.bloqueado


def test_medidor_agotado_confirma_beta_con_histeresis() -> None:
    medidor = MedidorDivergencia()
    agotado = (0.20, 0.40, 0.30, 0.90)
    campos = []
    for _ in range(120):   # 6 s simulados
        d, campo = medidor.actualizar(*agotado, DT)
        campos.append(campo)
    assert campos[39] == "steins_gate"          # a los 2 s todavía no se confirma
    assert medidor.campo_crudo == "beta"
    assert campos[-1] == "beta"
    assert d > SG


def test_medidor_aburrido_confirma_alfa() -> None:
    medidor = MedidorDivergencia()
    for _ in range(120):
        d, campo = medidor.actualizar(0.25, 0.40, 0.90, 0.30, DT)
    assert campo == "alfa"
    assert d < 1.0


def test_medidor_jitter_nunca_entra_en_la_banda_sg() -> None:
    m_bordes = [0.0715 + k * 0.0002 for k in range(12)] + [0.08, 0.12, 0.2, 0.5]
    for m in m_bordes:
        for lado in (+1, -1):
            a, fo, b, f = X_EQ_DEFECTO
            delta = m * MedidorDivergencia.M_MAX / 1.2
            x = (a, fo, b, f + delta) if lado > 0 else (a, fo, b + delta, f)
            for semilla in range(0, 2001, 7):
                medidor = MedidorDivergencia()
                d, _ = medidor.actualizar(*x, DT, semilla_jitter=semilla)
                assert d == SG or abs(d - SG) >= 0.003 - 1e-9, (m, lado, semilla, d)
                assert d == SG or (d - SG) * lado > 0, (m, lado, semilla, d)   # nunca cruza la banda
    # Lejos de la banda el jitter sí hace temblar los últimos dígitos.
    valores = {MedidorDivergencia().actualizar(*_estado_con_m(0.3), DT, semilla_jitter=s)[0]
               for s in (0, 500, 1500, 2000)}
    assert len(valores) > 1


def test_medidor_converge_a_linea_canonica() -> None:
    # D = SG − 0,9·tanh(2,2·(m − 0,07)) a 0,001 de 0,571024 (lado α: solo aburrimiento).
    objetivo = 0.571024 + 0.001
    m = 0.07 + math.atanh((SG - objetivo) / 0.9) / 2.2
    a, fo, b, f = X_EQ_DEFECTO
    x = (a, fo, b + m * MedidorDivergencia.M_MAX / 1.2, f)
    for semilla in (0, 999, 2000):
        d, _ = MedidorDivergencia().actualizar(*x, DT, semilla_jitter=semilla)
        assert d == 0.571024


def test_medidor_antirrebote() -> None:
    # m oscilando entre 0,06 y 0,11 cada paso: como mucho un cambio confirmado en 70 pasos.
    medidor = MedidorDivergencia()
    bajo, alto = _estado_con_m(0.06), _estado_con_m(0.11)
    campos = [medidor.actualizar(*(bajo if i % 2 == 0 else alto), DT)[1] for i in range(70)]
    cambios = sum(1 for x, y in zip(campos, campos[1:]) if x != y)
    assert cambios <= 1

    # Dos cambios forzados separados por 10 s: el segundo no se confirma antes de 60 s.
    medidor = MedidorDivergencia()
    t = 0.0
    t_beta = None
    for _ in range(200):          # 10 s en el estado agotado
        t += DT
        _, campo = medidor.actualizar(0.20, 0.40, 0.30, 0.90, DT)
        if campo == "beta" and t_beta is None:
            t_beta = t
    assert t_beta is not None
    t_alfa = None
    for _ in range(1400):         # 70 s en el estado aburrido
        t += DT
        _, campo = medidor.actualizar(0.25, 0.40, 0.90, 0.30, DT)
        if campo == "alfa" and t_alfa is None:
            t_alfa = t
    assert t_alfa is not None
    assert t_alfa - t_beta >= 60.0 - 1e-6


def test_medidor_anomalo_inmediato_y_salida_con_histeresis() -> None:
    medidor = MedidorDivergencia()
    d, campo = medidor.actualizar(*X_EQ_DEFECTO, DT, anomalo=True)
    assert campo == "anomalo"
    assert 2.0 <= d <= 3.999999
    d, campo = medidor.actualizar(float("nan"), 0.7, 0.2, 0.25, DT)   # NaN ⇒ anómalo
    assert campo == "anomalo"
    for _ in range(200):           # 10 s en equilibrio: aún dentro del refractario de 60 s
        _, campo = medidor.actualizar(*X_EQ_DEFECTO, DT)
    assert campo == "anomalo"


# ---------------------------------------------------------------------------
# Motor de iniciativas
# ---------------------------------------------------------------------------


class _Reloj:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_motor_iniciativa_flancos_prioridad_y_refractario() -> None:
    reloj = _Reloj()
    motor = MotorIniciativa(escala_tiempo=1.0, reloj=reloj)
    base = dict(modo="vigilia", campo_antes="steins_gate", campo_despues="steins_gate")
    # curiosidad_visual gana a conversar en el mismo chunk.
    ini, motivo, antes, despues = motor.evaluar(curiosidad=0.7, fatiga=0.3, boredom=0.8, arousal=0.6, **base)
    assert ini == "curiosidad_visual" and motivo and antes is None and despues is None
    # conversar quedó armada y sale en el siguiente chunk.
    ini, motivo, _, _ = motor.evaluar(curiosidad=0.7, fatiga=0.3, boredom=0.8, arousal=0.6, **base)
    assert ini == "conversar"
    assert "aburrimiento 0,80" in motivo
    # Sin rearme no se repite aunque pase el refractario.
    reloj.t += 3600.0
    assert motor.evaluar(curiosidad=0.7, fatiga=0.3, boredom=0.8, arousal=0.6, **base)[0] is None
    motor.evaluar(curiosidad=0.3, fatiga=0.3, boredom=0.8, arousal=0.6, **base)   # rearme
    assert motor.evaluar(curiosidad=0.7, fatiga=0.3, boredom=0.8, arousal=0.6, **base)[0] == "curiosidad_visual"
    # cambio_linea devuelve antes/después y respeta el refractario de 10 min.
    ini, _, antes, despues = motor.evaluar(curiosidad=0.1, fatiga=0.3, boredom=0.3, arousal=0.5, modo="vigilia",
                                           campo_antes="steins_gate", campo_despues="alfa")
    assert (ini, antes, despues) == ("cambio_linea", "steins_gate", "alfa")
    ini, _, _, _ = motor.evaluar(curiosidad=0.1, fatiga=0.3, boredom=0.3, arousal=0.5, modo="vigilia",
                                 campo_antes="alfa", campo_despues="beta")
    assert ini is None


def test_patron_pn_y_ciclo_circadiano() -> None:
    for texto in ("hola amadeus", "ok", "", "¿cómo va el build?", "a" * 500):
        p = patron_pn(texto)
        assert len(p) == 12 and len(set(p)) == 12
        assert all(0 <= i < 64 for i in p)
        assert p == patron_pn(texto)
    assert ciclo_circadiano(4.0) > ciclo_circadiano(16.0)
    assert all(0.0 <= ciclo_circadiano(h / 2) <= 1.0 for h in range(48))


# ---------------------------------------------------------------------------
# 2. Stub (Python puro)
# ---------------------------------------------------------------------------


def test_stub_avanza_con_rangos_validos() -> None:
    c = SincanConnectome(None, fuente="stub", semilla=1)
    e = c.avanzar(50)
    assert isinstance(e, HomeostaticState)
    assert e.fuente == "stub"
    assert len(e.bump) == 16
    _rangos_validos(e)
    assert c.n_neuronas == 0
    diag = c.diagnostico()
    assert diag.clave == "conectoma" and diag.estado is EstadoModulo.DEGRADADO
    assert "modo simple" in diag.detalle
    hud = c.snapshot_hud()
    assert set(hud) == {"bump", "divergencia", "campo", "fuente", "tasas"}
    assert hud["tasas"] == {} and len(hud["divergencia"]) == 8
    c.inyectar("usuario_escribe", texto="hola amadeus")
    c.inyectar("ventana_cambia", clase="kitty", titulo="nvim main.py")
    for _ in range(20):
        _rangos_validos(c.avanzar(50))
    asyncio.run(c.cerrar())


def test_stub_iniciar_calibra_equilibrio() -> None:
    c = SincanConnectome(None, fuente="stub", semilla=1, reloj=lambda: _reloj_fijo(10))
    t0 = time.perf_counter()
    diag = asyncio.run(c.iniciar(timeout_s=4))
    assert time.perf_counter() - t0 < 2.0
    assert diag.clave == "conectoma"
    x_eq = c.medidor.x_eq
    assert x_eq != X_EQ_DEFECTO and all(0.0 <= v <= 1.0 for v in x_eq)
    assert c.estado().divergencia_str() == "1.048596"
    asyncio.run(c.cerrar())


# ---------------------------------------------------------------------------
# 3. Modelo reducido (numpy)
# ---------------------------------------------------------------------------


def test_reducido_rendimiento_y_rangos(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7)
    assert c.fuente_activa == "reducido" and c.modo_degradacion == "normal"
    assert c.n_neuronas > 1000
    t0 = time.perf_counter()
    for _ in range(100):
        e = c.avanzar(50)
    assert time.perf_counter() - t0 < 2.5
    _rangos_validos(e)
    assert e.fuente == "reducido" and len(e.bump) == 16
    assert {"EPG", "KC", "MBON_a3", "PAM", "PPL1", "dFB"} <= set(e.tasas)
    diag = c.diagnostico()
    assert diag.estado is EstadoModulo.OK
    assert "neuronas · dt=1 ms" in diag.detalle
    interno = c.diagnostico_interno()
    assert set(interno) == {"novedad", "kc_activas_pct", "rho", "tasa_mbon_a3", "ms_por_chunk",
                            "factor_tiempo_real"}
    asyncio.run(c.cerrar())


def test_reducido_novedad_decrece_con_la_repeticion(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7,
                         reloj=lambda: _reloj_fijo(10))
    for _ in range(20):
        c.avanzar(50)
    novedades = []
    for _ in range(4):
        c.inyectar("usuario_escribe", texto="hola amadeus")
        picos = []
        for _ in range(8):           # respuesta de inicio (400 ms)
            c.avanzar(50)
            picos.append(c.diagnostico_interno()["novedad"])
        novedades.append(max(picos))
        for _ in range(12):          # hasta completar 1 000 ms entre presentaciones
            c.avanzar(50)
    assert novedades[0] > novedades[3]
    assert c._red.familiaridad() > 0.0      # la plasticidad KC→MBON-α'3 aprendió el patrón
    asyncio.run(c.cerrar())


def test_reducido_codificacion_dispersa_kc(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7,
                         reloj=lambda: _reloj_fijo(10))
    for _ in range(10):
        c.avanzar(50)
    c.inyectar("usuario_escribe", texto="hola amadeus, ¿cómo va el build?")
    for _ in range(6):               # 300 ms simulados
        c.avanzar(50)
    pct = c.diagnostico_interno()["kc_activas_pct"]
    assert 6.0 <= pct <= 12.0, pct
    asyncio.run(c.cerrar())


@pytest.mark.parametrize(("semilla", "hora"), [(7, 10), (21, 15), (33, 3)])
def test_reducido_bump_persistente_y_steins_gate(settings_prueba, semilla, hora) -> None:
    pytest.importorskip("numpy")
    # Reloj fijo: la hora local (proceso C) y el proceso S quedan deterministas entre ejecuciones. Varias
    # combinaciones semilla/hora: el X_EQ calibrado debe representar el reposo, no solo con una semilla.
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=semilla, semilla_red=semilla,
                         reloj=lambda: _reloj_fijo(hora))
    diag = asyncio.run(c.iniciar(timeout_s=30))
    assert diag.estado is EstadoModulo.OK
    lento = os.environ.get("RAMS_TESTS_LENTOS") == "1"
    campos = []
    for i in range((60 if lento else 30) * 20):
        e = c.avanzar(50)
        campos.append(e.campo)
        if i == 30 * 20 - 1:
            assert c.diagnostico_interno()["rho"] > 0.5
            assert e.focus > 0.2
    assert set(campos[:30 * 20]) == {"steins_gate"}       # 30 s sin salir de SG (ni un Reading Steiner)
    assert c.diagnostico_interno()["rho"] > 0.5
    if lento and e.campo != "steins_gate":
        # Hacia el minuto sin estímulos el aburrimiento (A.5, ν = 0) ya suma m ≈ 0,07: cualquier
        # fluctuación normal del foco desbloquea SG y la línea pasa a α. Es la deriva prevista
        # ("aburrido ⇒ α"); nunca β ni un salto que no explique el aburrimiento.
        assert e.campo == "alfa"
        assert e.boredom - c.medidor.x_eq[2] > 0.08
    asyncio.run(c.cerrar())


def test_reducido_reposo_sin_novedad_y_conversar(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7,
                         reloj=lambda: _reloj_fijo(15))
    asyncio.run(c.iniciar(timeout_s=30))
    novedades = []
    for _ in range(200):              # 10 s de reposo: el ruido de MBON-α'3 no es novedad
        c.avanzar(50)
        novedades.append(c.diagnostico_interno()["novedad"])
    assert sum(novedades) / len(novedades) < 1e-3
    # Cerca del umbral, el aburrimiento sigue creciendo (sin el término −B·ν/3 del ruido rectificado,
    # que lo estancaba en ≈ 0,5) y sale "conversar".
    c._lector.B = 0.745
    conversar = None
    for _ in range(600):              # hasta 30 s
        e = c.avanzar(50)
        if e.iniciativa == "conversar":
            conversar = e
            break
    assert conversar is not None
    assert conversar.boredom > 0.75 and conversar.motivo_iniciativa.startswith("aburrimiento")
    asyncio.run(c.cerrar())


@pytest.mark.skipif(os.environ.get("RAMS_TESTS_LENTOS") != "1", reason="reposo de 9 min (RAMS_TESTS_LENTOS=1)")
def test_reducido_reposo_largo_dispara_conversar(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7,
                         reloj=lambda: _reloj_fijo(15))
    asyncio.run(c.iniciar(timeout_s=30))
    iniciativas = []
    for _ in range(9 * 60 * 20):
        e = c.avanzar(50)
        if e.iniciativa:
            iniciativas.append(e.iniciativa)
    assert "conversar" in iniciativas
    assert e.boredom > 0.75
    asyncio.run(c.cerrar())


@pytest.mark.parametrize("fuente", ["stub", "reducido"])
def test_curiosidad_visual_tras_aburrimiento_y_estimulo_nuevo(settings_prueba, fuente) -> None:
    if fuente == "reducido":
        pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente=fuente, semilla=7, semilla_red=7, reloj=lambda: _reloj_fijo(15))
    asyncio.run(c.iniciar(timeout_s=30))
    modelo = c._lector if fuente == "reducido" else c._simple
    modelo.B = 0.9                    # un buen rato sin estímulos
    for _ in range(100):
        c.avanzar(50)
    assert c.estado().curiosidad < 0.65            # el aburrimiento solo no basta para el vistazo
    c.inyectar("usuario_escribe", texto="¿viste el eclipse de anoche sobre Akihabara?")
    estados = [c.avanzar(50) for _ in range(60)]
    vistazos = [e for e in estados if e.iniciativa == "curiosidad_visual"]
    assert vistazos and vistazos[0].disparar_vision
    assert vistazos[0].curiosidad < 0.65           # saciedad: ×0,3 tras el vistazo
    asyncio.run(c.cerrar())


def _distancia_deg(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def test_reducido_rostro_desplaza_el_rumbo_hacia_el_usuario() -> None:
    pytest.importorskip("numpy")
    from core.sincan_connectome import _h

    col_usuario = _h("usuario", 16)
    col_ventana = (col_usuario + 9) % 16           # ventana activa a 157,5° del usuario
    c = SincanConnectome(None, fuente="reducido", semilla=7, semilla_red=7, reloj=lambda: _reloj_fijo(10))
    c._rumbo_asignado_col = col_ventana
    c._aplicar_tonicos()
    for _ in range(60):
        c.avanzar(50)
    assert _distancia_deg(c.estado().rumbo_deg, col_ventana * 22.5) < 45.0
    c.inyectar("rostro", presente=True)
    rumbos = [c.avanzar(50).rumbo_deg for _ in range(16)]           # 800 ms
    assert min(_distancia_deg(r, col_usuario * 22.5) for r in rumbos) < 30.0
    for _ in range(60):               # al terminar el transitorio el tónico devuelve el rumbo a la ventana
        c.avanzar(50)
    assert _distancia_deg(c.estado().rumbo_deg, col_ventana * 22.5) < 60.0
    asyncio.run(c.cerrar())


def test_degradar_a_stub_en_ejecucion_fija_un_equilibrio_propio(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7,
                         reloj=lambda: _reloj_fijo(10))
    asyncio.run(c.iniciar(timeout_s=30))
    for _ in range(200):
        c.avanzar(50)
    while c.modo_degradacion != "stub":
        c._bajar_escalon("prueba")
    a_eq, fo_eq, _, _ = c.medidor.x_eq
    assert fo_eq == pytest.approx(0.70, abs=0.01)                    # equilibrio del modo simple (A.6)
    assert a_eq == pytest.approx(0.40 + 0.2 * (1.0 - ciclo_circadiano(10.0)), abs=0.01)
    for _ in range(400):              # 20 s
        e = c.avanzar(50)
    assert e.fuente == "stub" and e.campo == "steins_gate"
    assert c.medidor.m < 0.08
    asyncio.run(c.cerrar())


@pytest.mark.parametrize("llamadas_hasta_abortar", [40, 150])   # antes y después de congelar la línea base
def test_calibracion_abortada_restaura_el_conjunto_guardado(settings_prueba, llamadas_hasta_abortar) -> None:
    pytest.importorskip("numpy")
    ruta = settings_prueba.data_dir / "connectome_estado.json"
    c1 = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7,
                          reloj=lambda: _reloj_fijo(15))
    asyncio.run(c1.iniciar(timeout_s=30))
    asyncio.run(c1.cerrar())
    guardado = json.loads(ruta.read_text(encoding="utf-8"))

    c2 = SincanConnectome(settings_prueba, fuente="reducido", semilla=8, semilla_red=7,
                          reloj=lambda: _reloj_fijo(15))
    llamadas = 0

    def vencido(_plazo: float) -> bool:
        nonlocal llamadas
        llamadas += 1
        return llamadas > llamadas_hasta_abortar

    c2._vencido = vencido  # type: ignore[method-assign]
    asyncio.run(c2.iniciar(timeout_s=30))
    assert c2._cal_offsets == pytest.approx(guardado["ajuste_sesgo"])
    assert c2._lector.r_base == pytest.approx(guardado["r_base"])
    assert c2._lector.a_base == pytest.approx(guardado["a_base"])
    assert c2.medidor.x_eq == pytest.approx(tuple(guardado["x_eq"]))
    asyncio.run(c2.cerrar())
    assert json.loads(ruta.read_text(encoding="utf-8"))["ajuste_sesgo"] == pytest.approx(guardado["ajuste_sesgo"])


def test_benchmark_no_degrada_por_un_pico_pasajero(settings_prueba, monkeypatch) -> None:
    pytest.importorskip("numpy")
    import core.sincan_connectome as modulo

    original = modulo._RedReducida.simular
    picos = []

    def simular(self, pasos, *, plasticidad=True):
        if not plasticidad and pasos >= 200 and not picos:
            picos.append(pasos)
            time.sleep(0.2)           # una sola tanda lenta (≈ 1 ms por ms simulado)
        return original(self, pasos, plasticidad=plasticidad)

    monkeypatch.setattr(modulo._RedReducida, "simular", simular)
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7)
    asyncio.run(c.iniciar(timeout_s=10))
    assert picos and c.modo_degradacion == "normal"
    asyncio.run(c.cerrar())


def test_bump_usa_la_ventana_de_200_ms() -> None:
    np = pytest.importorskip("numpy")
    from core.sincan_connectome import _LectorHomeostatico, _ProcesoSueno, _ResultadoChunk

    lector = _LectorHomeostatico({}, 0.05, 35.0)
    sueno = _ProcesoSueno(0.3)

    def chunk(cols: dict[int, float]) -> _ResultadoChunk:
        cols_epg = [float(cols.get(k, 0.0)) for k in range(16)]
        return _ResultadoChunk(50.0, np.zeros(18, dtype=np.int64), cols_epg, 0.05, 0.0, 0, True)

    for _ in range(3):
        lector.actualizar(chunk({2: 9}), 0.05, c=0.5, sueno=sueno, dt_sueno_s=0.0, reposo=False, k_act=1.0)
    previo = lector.bump[9]
    lector.actualizar(chunk({9: 10}), 0.05, c=0.5, sueno=sueno, dt_sueno_s=0.0, reposo=False, k_act=1.0)
    alfa = 1.0 - math.exp(-0.05 / 0.3)
    # N_k de la ventana de 200 ms: columna 2 = 27 spikes, columna 9 = 10 ⇒ objetivo 10/27 (no 1).
    assert lector.bump[9] == pytest.approx(previo + alfa * (10.0 / 27.0 - previo))
    assert lector.bump[2] > lector.bump[9]


def test_iniciar_con_plazo_agotado_no_bloquea(settings_prueba) -> None:
    pytest.importorskip("numpy")
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7)
    t0 = time.perf_counter()
    diag = asyncio.run(c.iniciar(timeout_s=0.05))
    assert time.perf_counter() - t0 < 3.0
    assert diag.clave == "conectoma"
    assert c.medidor.x_eq == X_EQ_DEFECTO        # sin calibración: equilibrio por defecto
    _rangos_validos(c.avanzar(50))
    asyncio.run(c.cerrar())


def test_cascada_de_degradacion_hasta_stub(settings_prueba, monkeypatch) -> None:
    pytest.importorskip("numpy")
    import core.sincan_connectome as modulo

    monkeypatch.setattr(modulo, "_UMBRAL_BENCH_S_POR_MS", 0.0)   # ninguna máquina pasa el benchmark
    c = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7)
    diag = asyncio.run(c.iniciar(timeout_s=10))
    assert c.fuente_activa == "stub" and c.modo_degradacion == "stub"
    assert diag.estado is EstadoModulo.DEGRADADO and "modo simple" in diag.detalle
    e = c.avanzar(50)
    assert e.fuente == "stub" and len(e.bump) == 16
    asyncio.run(c.cerrar())


# ---------------------------------------------------------------------------
# 4. inyectar nunca lanza
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fuente", ["stub", "reducido"])
def test_inyectar_eventos_raros_no_lanza(fuente) -> None:
    if fuente == "reducido":
        pytest.importorskip("numpy")
    c = SincanConnectome(None, fuente=fuente, semilla=3, semilla_red=3)
    c.inyectar("evento_inventado", x=1)
    c.inyectar("usuario_escribe")
    c.inyectar("carga_sistema", cpu="mucho", ram=None, temp=float("nan"))
    c.inyectar("error", severidad="grave")
    c.inyectar("ventana_cambia")
    c.inyectar("rostro", presente=True, n="dos")
    c.inyectar("busqueda_completa", consulta=None, n=-1, exito=True)
    c.inyectar("recompensa", magnitud=float("inf"))
    c.inyectar("nexus_foco", enfocado=True)
    c.inyectar("usuario_presencia", presente=False)
    c.inyectar(None)  # type: ignore[arg-type]
    for _ in range(4):
        _rangos_validos(c.avanzar(50))
    asyncio.run(c.cerrar())


# ---------------------------------------------------------------------------
# 5. latir
# ---------------------------------------------------------------------------


class _MotorFalso:
    """Motor de iniciativas falso: devuelve un cambio de línea a partir de la llamada ``desde``."""

    def __init__(self, desde: int = 3) -> None:
        self.llamadas = 0
        self.desde = desde

    def evaluar(self, **_: object) -> tuple[str | None, str, str | None, str | None]:
        self.llamadas += 1
        if self.llamadas >= self.desde:
            return "cambio_linea", "x", "alfa", "beta"
        return None, "", None, None


def test_latir_publica_periodicamente_y_al_haber_iniciativa() -> None:
    c = SincanConnectome(None, fuente="stub", semilla=1)
    c.motor_iniciativa = _MotorFalso(desde=3)  # type: ignore[assignment]
    publicados: list[HomeostaticState] = []

    async def escenario() -> None:
        parar = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, parar.set)
        await c.latir(periodo_ms=50, publicar=publicados.append, parar=parar, publicar_cada_ms=60_000)

    asyncio.run(escenario())
    assert len(publicados) >= 2
    assert publicados[0].iniciativa is None            # primera publicación por tiempo
    con_iniciativa = [e for e in publicados if e.iniciativa is not None]
    assert con_iniciativa, "las iniciativas deben publicarse de inmediato"
    e = con_iniciativa[0]
    assert e.iniciativa == "cambio_linea"
    assert e.linea_antes == "alfa" and e.linea_despues == "beta"
    assert e.motivo_iniciativa == "x"
    asyncio.run(c.cerrar())
    asyncio.run(c.cerrar())


def test_latir_publica_al_menos_una_vez_y_guarda_al_terminar(settings_prueba) -> None:
    c = SincanConnectome(settings_prueba, fuente="stub", semilla=1)
    publicados: list[HomeostaticState] = []

    async def escenario() -> None:
        parar = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, parar.set)
        await c.latir(publicar=publicados.append, parar=parar)

    asyncio.run(escenario())
    assert len(publicados) >= 1
    assert all(isinstance(e.factor_tiempo_real, float) for e in publicados)
    assert (settings_prueba.data_dir / "connectome_estado.json").is_file()
    asyncio.run(c.cerrar())


# ---------------------------------------------------------------------------
# 6–7. Persistencia
# ---------------------------------------------------------------------------


def test_guardar_y_cargar_estado_conserva_semilla_red(settings_prueba) -> None:
    ruta = settings_prueba.data_dir / "connectome_estado.json"
    c1 = SincanConnectome(settings_prueba, fuente="stub", semilla=1)
    for _ in range(10):
        c1.avanzar(50)
    c1.guardar_estado()
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    assert datos["version"] == 2
    assert {"ts", "presion_sueno", "boredom", "fatiga", "r_novel", "rumbo_deg", "x_eq", "r_base", "red"} <= set(datos)
    assert len(datos["x_eq"]) == 4
    assert datos["red"]["version_red"] == VERSION_RED
    assert datos["red"]["semilla_red"] == c1._semilla_red
    assert {"n_kc", "n_pn", "k_pn", "modo"} <= set(datos["red"])
    if os.name == "posix":
        assert (ruta.stat().st_mode & 0o777) == 0o600

    c2 = SincanConnectome(settings_prueba, fuente="stub", semilla=1)
    assert c2._semilla_red == c1._semilla_red
    assert c2.cargar_estado() is True
    asyncio.run(c1.cerrar())
    asyncio.run(c2.cerrar())


def test_misma_red_entre_arranques(settings_prueba) -> None:
    np = pytest.importorskip("numpy")
    c1 = SincanConnectome(settings_prueba, fuente="reducido", semilla=7)
    c1.guardar_estado()
    c2 = SincanConnectome(settings_prueba, fuente="reducido", semilla=7)
    assert c2._semilla_red == c1._semilla_red
    assert np.array_equal(c1._red.garras, c2._red.garras)
    assert np.array_equal(c1._red.w, c2._red.w)
    asyncio.run(c1.cerrar())
    asyncio.run(c2.cerrar())


def test_estado_corrupto_y_presion_de_sueno_al_cargar(settings_prueba) -> None:
    ruta = settings_prueba.data_dir / "connectome_estado.json"
    ruta.write_text("{esto no es json", encoding="utf-8")
    c = SincanConnectome(settings_prueba, fuente="stub", semilla=1)
    assert c.cargar_estado() is False
    _rangos_validos(c.avanzar(50))

    # P ← P_guardado·exp(−t_apagado/τ_s) y B ← 0,5·B_guardado.
    ahora = _reloj_fijo(10)
    ruta.write_text(json.dumps({"version": 2, "ts": ahora - 4.2 * 3600, "presion_sueno": 0.8, "boredom": 0.6,
                                "fatiga": 0.4, "r_novel": 30.0, "rumbo_deg": 90.0,
                                "x_eq": [0.5, 0.6, 0.3, 0.3], "r_base": {"PAM": 3.0},
                                "red": {"version_red": 2, "semilla_red": 99, "n_kc": 0, "n_pn": 64,
                                        "k_pn": 12, "modo": "stub"}}), encoding="utf-8")
    c2 = SincanConnectome(settings_prueba, fuente="stub", semilla=1, reloj=lambda: ahora)
    assert c2._semilla_red == 99
    assert c2._sueno.p == pytest.approx(0.8 * math.exp(-1.0), rel=1e-6)
    assert c2._simple.B == pytest.approx(0.3)
    assert c2.medidor.x_eq == (0.5, 0.6, 0.3, 0.3)
    asyncio.run(c.cerrar())
    asyncio.run(c2.cerrar())


def test_guardar_normal_y_cargar_ligero_usa_w0(settings_prueba) -> None:
    np = pytest.importorskip("numpy")
    c1 = SincanConnectome(settings_prueba, fuente="reducido", semilla=7, semilla_red=7)
    c1._red.W_a3 *= 0.5                       # familiaridad aprendida
    c1.guardar_estado()
    assert (settings_prueba.data_dir / "connectome_plastico.npz").is_file()

    c2 = SincanConnectome(settings_prueba, fuente="reducido", semilla=7)
    assert c2._red.familiaridad() > 0.4      # misma red: los pesos guardados se recuperan
    assert c2._forzar_modo("ligero") is True
    assert c2.modo_degradacion == "ligero"
    assert c2.cargar_estado() is True        # no lanza
    assert np.array_equal(c2._red.W_a3, c2._red.W0_a3)
    assert np.array_equal(c2._red.W_avd, c2._red.W0_avd)
    assert np.array_equal(c2._red.W_app, c2._red.W0_app)
    _rangos_validos(c2.avanzar(50))
    asyncio.run(c1.cerrar())
    asyncio.run(c2.cerrar())


# ---------------------------------------------------------------------------
# 8. cerrar
# ---------------------------------------------------------------------------


def test_cerrar_dos_veces_no_lanza(settings_prueba) -> None:
    c = SincanConnectome(settings_prueba, fuente="stub", semilla=1)
    c.avanzar(50)
    asyncio.run(c.cerrar())
    asyncio.run(c.cerrar())
    assert (settings_prueba.data_dir / "connectome_estado.json").is_file()
    _rangos_validos(c.estado())


# ---------------------------------------------------------------------------
# FlyWire (§10.7)
# ---------------------------------------------------------------------------


def test_flywire_sin_archivos(tmp_path) -> None:
    assert cargar_red_flywire(tmp_path) is None
    assert main(["--stats", str(tmp_path)]) == 1


def _gz(ruta: Path, filas: list[list[object]]) -> None:
    with gzip.open(ruta, "wt", encoding="utf-8", newline="") as f:
        for fila in filas:
            f.write(",".join(str(x) for x in fila) + "\n")


def test_flywire_red_sintetica_y_cache(tmp_path, capsys) -> None:
    pytest.importorskip("numpy")
    base = 720575940600000000
    tipos = [["root_id", "primary_type", "additional_type(s)"]]
    neuronas = [["root_id", "nt_type"]]
    ids: dict[str, list[int]] = {}
    plantilla = [("EPG", "EPG", "ACH"), ("D7", "Delta7", "GLUT"), ("KC_g", "KCg-m", "ACH"),
                 ("MBON_a3", "MBON16", "ACH"), ("PAM", "PAM01", "DA"), ("PPL1", "PPL101", "DA"),
                 ("dFB", "FB6A", "GABA")]
    k = 0
    for pob, tipo, nt in plantilla:
        for _ in range(3):
            k += 1
            rid = base + k
            ids.setdefault(pob, []).append(rid)
            tipos.append([rid, tipo, ""])
            neuronas.append([rid, nt])
    tipos.append([base + 999, "AVLP001", ""])                    # fuera de CX+MB
    conexiones = [["pre_root_id", "post_root_id", "neuropil", "syn_count", "nt_type"]]
    conexiones += [[ids["EPG"][0], ids["D7"][0], "PB_L1", 10, "ACH"],
                   [ids["EPG"][0], ids["D7"][0], "PB_L2", 5, "ACH"],   # mismo par: se suman
                   [ids["D7"][1], ids["EPG"][1], "PB_L1", 7, "GLUT"],
                   [ids["KC_g"][0], ids["MBON_a3"][0], "MB_VL_L", 20, "ACH"],
                   [base + 999, ids["EPG"][2], "AVLP_L", 30, "ACH"]]
    _gz(tmp_path / "consolidated_cell_types.csv.gz", tipos)
    _gz(tmp_path / "neurons.csv.gz", neuronas)
    _gz(tmp_path / "connections.csv.gz", conexiones)

    red = cargar_red_flywire(tmp_path)
    assert red is not None
    stats = estadisticas(red)
    assert set(stats) == set(POBLACIONES)
    assert stats["EPG"] == {"neuronas": 3, "sinapsis_salida": 15}
    assert stats["D7"]["sinapsis_salida"] == 7
    assert len(red.root_ids) == 21 and int(red.root_ids.max()) == base + 21
    assert len(red.w) == 3
    pesos = sorted(float(w) for w in red.w)
    assert pesos[0] == pytest.approx(-7 * 0.275)          # Δ7 glutamatérgica ⇒ inhibitoria
    assert pesos[-1] == pytest.approx(20 * 0.275)
    assert (tmp_path / "flywire_cxmb_783.npz").is_file()
    assert red.meta["md5"] and red.meta["conteos"]["PAM"]["neuronas"] == 3

    otra = cargar_red_flywire(tmp_path)                    # desde la caché
    assert otra is not None and otra.meta["md5"] == red.meta["md5"]
    assert main(["--stats", str(tmp_path)]) == 0
    salida = capsys.readouterr().out
    assert "EPG" in salida and "Red válida" in salida


def test_flywire_faltan_poblaciones_obligatorias(tmp_path) -> None:
    pytest.importorskip("numpy")
    base = 720575940600000000
    _gz(tmp_path / "consolidated_cell_types.csv.gz",
        [["root_id", "primary_type"], [base + 1, "EPG"], [base + 2, "EPG"]])
    _gz(tmp_path / "connections.csv.gz",
        [["pre_root_id", "post_root_id", "syn_count"], [base + 1, base + 2, 9]])
    assert cargar_red_flywire(tmp_path) is not None
    assert main(["--stats", str(tmp_path)]) == 1


def test_sin_numpy_degrada_a_modo_simple_y_flywire_none(tmp_path) -> None:
    """Sin numpy (bloqueado en un subproceso) ambos módulos se importan y degradan con elegancia."""
    import subprocess

    raiz = Path(__file__).resolve().parents[1]
    codigo = chr(10).join([
        "import sys; sys.modules['numpy'] = None",
        'import core.sincan_connectome as m, core.flywire_loader as f',
        "c = m.SincanConnectome(None, fuente='reducido', semilla=1)",
        "assert c.fuente_activa == 'stub', c.fuente_activa",
        "assert c.diagnostico().detalle == 'modo simple (sin numpy)'",
        "e = c.avanzar(50); assert e.fuente == 'stub' and len(e.bump) == 16",
        'assert f.cargar_red_flywire(sys.argv[1]) is None',
        "assert f.main(['--stats', sys.argv[1]]) == 1",
        "assert 'pandas' not in sys.modules",
        "print('ok')",
    ])
    entorno = dict(os.environ, PYTHONPATH=str(raiz))
    resultado = subprocess.run([sys.executable, "-c", codigo, str(tmp_path)], cwd=raiz, env=entorno,
                               capture_output=True, text=True, timeout=60)
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip().endswith("ok")
