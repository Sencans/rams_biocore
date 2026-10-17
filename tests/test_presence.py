"""Tests de core/presence.py (grupo io-dispatch, §10.11 y §12.3).

Recorren las 7 reglas de §7 con un reloj y una fuente de inactividad falsos (sin tocar el
sistema), comprueban que ``leer_inactividad`` no lanza en la plataforma actual, que Telegram no
cuenta como presencia y el caso de Linux simulado con ``idle_flag_path=None`` y hypridle vivo.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import core.presence as presence
from core.config import Settings, cargar_settings
from core.contracts import EstadoPresencia
from core.presence import RastreadorPresencia, leer_inactividad

RAIZ = Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0


def _settings(tmp_path: Path, **extra: str) -> Settings:
    """Settings offline y aislados (mismo contrato que ``settings_prueba`` de conftest)."""
    entorno = {
        "RAMS_DATA_DIR": str(tmp_path / "data"), "RAMS_HUD_PORT": "0", "RAMS_CTL_PORT": "0",
        "RAMS_CTL_TOKEN_FILE": str(tmp_path / "ctl_token"), "VTS_ENABLED": "0", "RAMS_CAMERA": "0",
        "RAMS_TELEGRAM_POLL": "0", "RAMS_CONNECTOME_SEED": "7",
    }
    entorno.update(extra)
    return cargar_settings(entorno=entorno, offline=True, base_dir=RAIZ)


class Reloj:
    """Reloj controlable."""

    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def avanzar(self, segundos: float) -> None:
        self.t += segundos


class Fuente:
    """Fuente de inactividad controlable."""

    def __init__(self, segundos: float | None = None, fuente: str = "ninguna") -> None:
        self.valor: tuple[float | None, str] = (segundos, fuente)

    def __call__(self) -> tuple[float | None, str]:
        return self.valor


def _rastreador(tmp_path: Path, segundos: float | None = None,
                fuente: str = "ninguna") -> tuple[RastreadorPresencia, Reloj, Fuente]:
    reloj = Reloj()
    fuente_falsa = Fuente(segundos, fuente)
    return (RastreadorPresencia(_settings(tmp_path), reloj=reloj, fuente_inactividad=fuente_falsa),
            reloj, fuente_falsa)


# ---------------------------------------------------------------------------
# Estado inicial
# ---------------------------------------------------------------------------


def test_estado_inicial_desconocido(tmp_path: Path) -> None:
    rastreador, _, _ = _rastreador(tmp_path)
    estado = rastreador.estado()
    assert estado.presente is True
    assert estado.motivo == "sin_datos"
    assert estado.ambiguo is True
    assert estado.confianza == pytest.approx(EstadoPresencia.desconocido().confianza)
    assert rastreador.segundos_desde_entrada_local() is None
    assert rastreador.ultima_entrada_remota_ts is None


# ---------------------------------------------------------------------------
# Reglas 1–7
# ---------------------------------------------------------------------------


def test_regla_1_terminal_reciente(tmp_path: Path) -> None:
    rastreador, reloj, fuente = _rastreador(tmp_path, 99_999.0, "win32")
    rastreador.registrar_entrada("terminal")
    reloj.avanzar(60)
    estado = rastreador.actualizar()
    assert (estado.presente, estado.confianza, estado.motivo) == (True, 0.95, "terminal_reciente")
    assert estado.ambiguo is False
    assert estado.ultima_entrada_ts == pytest.approx(T0)
    assert rastreador.segundos_desde_entrada_local() == pytest.approx(60.0)
    assert rastreador.estado() is estado
    # A los 120 s deja de ser "reciente": con inactividad enorme manda la regla 5.
    reloj.avanzar(60)
    assert rastreador.actualizar().motivo == "inactividad"


def test_regla_2_rostro(tmp_path: Path) -> None:
    rastreador, reloj, fuente = _rastreador(tmp_path)
    rastreador.registrar_rostro(True)
    reloj.avanzar(100)
    estado = rastreador.actualizar()          # inactividad desconocida
    assert (estado.presente, estado.confianza, estado.motivo) == (True, 0.90, "rostro")
    assert estado.ultimo_rostro_ts == pytest.approx(T0)
    fuente.valor = (599.0, "hypridle")        # inactividad < 600 s
    assert rastreador.actualizar().motivo == "rostro"
    fuente.valor = (700.0, "hypridle")        # inactividad ≥ 600 s: ya no vale el rostro
    estado = rastreador.actualizar()
    assert estado.motivo == "inactividad" and estado.presente is False
    fuente.valor = (None, "ninguna")
    reloj.avanzar(250)                        # rostro hace 350 s: caducado
    assert rastreador.actualizar().motivo == "sin_datos"


def test_regla_2_rostro_fresco_gana_a_la_inactividad(tmp_path: Path) -> None:
    """Visión continua (v0.2): main refresca el rostro cada 30 s; un vídeo largo sin tocar nada
    no es ausencia. Una salida posterior (rostro False) anula el rostro fresco."""
    rastreador, reloj, fuente = _rastreador(tmp_path, 1800.0, "win32")   # 30 min sin input
    for _ in range(4):                         # 2 min de refrescos cada 30 s
        rastreador.registrar_rostro(True)
        reloj.avanzar(30)
        estado = rastreador.actualizar()
        assert (estado.presente, estado.confianza, estado.motivo) == (True, 0.90, "rostro")
    reloj.avanzar(31)                          # último rostro hace 61 s: vuelve a mandar la inactividad
    assert rastreador.actualizar().motivo == "inactividad"
    rastreador.registrar_rostro(True)
    reloj.avanzar(5)
    rastreador.registrar_rostro(False)         # vision.estimulo{salida}
    estado = rastreador.actualizar()
    assert estado.presente is False and estado.motivo == "sin_rostro"


def test_regla_3_sin_rostro(tmp_path: Path) -> None:
    rastreador, reloj, fuente = _rastreador(tmp_path, 200.0, "hypridle")
    rastreador.registrar_rostro(False)
    reloj.avanzar(30)
    estado = rastreador.actualizar()
    assert (estado.presente, estado.confianza, estado.motivo) == (False, 0.85, "sin_rostro")
    assert estado.ambiguo is False
    fuente.valor = (None, "ninguna")          # inactividad desconocida: también aplica
    assert rastreador.actualizar().motivo == "sin_rostro"
    fuente.valor = (100.0, "hypridle")        # inactividad < 180 s: no aplica → regla 4
    assert rastreador.actualizar().motivo == "sistema_activo"


def test_registrar_rostro_none_no_altera(tmp_path: Path) -> None:
    rastreador, reloj, _ = _rastreador(tmp_path, 200.0, "hypridle")
    rastreador.registrar_rostro(False)
    rastreador.registrar_rostro(None)
    reloj.avanzar(10)
    assert rastreador.actualizar().motivo == "sin_rostro"


def test_regla_4_sistema_activo(tmp_path: Path) -> None:
    rastreador, _, _ = _rastreador(tmp_path, 30.0, "win32")
    estado = rastreador.actualizar()
    assert (estado.presente, estado.confianza, estado.motivo) == (True, 0.70, "sistema_activo")
    assert estado.segundos_inactivo == pytest.approx(30.0)
    assert estado.fuente_inactividad == "win32"
    assert estado.ambiguo is False


def test_regla_5_inactividad(tmp_path: Path) -> None:
    rastreador, _, fuente = _rastreador(tmp_path, 600.0, "hypridle")   # A = 10 min = 600 s
    estado = rastreador.actualizar()
    assert (estado.presente, estado.confianza, estado.motivo) == (False, 0.80, "inactividad")
    assert estado.ambiguo is False
    fuente.valor = (599.9, "hypridle")
    assert rastreador.actualizar().motivo == "sistema_activo"


def test_regla_6_sin_fuente_y_entrada_local(tmp_path: Path) -> None:
    rastreador, reloj, _ = _rastreador(tmp_path)
    rastreador.registrar_entrada("terminal")
    reloj.avanzar(300)                        # > 120 s y < A
    estado = rastreador.actualizar()
    assert (estado.presente, estado.confianza, estado.motivo) == (True, 0.50, "sin_datos")
    assert estado.ambiguo is True
    assert estado.fuente_inactividad == "ninguna"


def test_regla_7_resto(tmp_path: Path) -> None:
    rastreador, reloj, _ = _rastreador(tmp_path)
    estado = rastreador.actualizar()          # sin entrada ni fuente
    assert (estado.presente, estado.confianza, estado.motivo) == (False, 0.40, "sin_datos")
    assert estado.ambiguo is True
    rastreador.registrar_entrada("terminal")
    reloj.avanzar(601)                        # entrada local más vieja que A
    estado = rastreador.actualizar()
    assert (estado.presente, estado.motivo) == (False, "sin_datos")


def test_fuente_que_falla_no_lanza(tmp_path: Path) -> None:
    def fuente_rota() -> tuple[float | None, str]:
        raise OSError("sin sesión")

    rastreador = RastreadorPresencia(_settings(tmp_path), reloj=Reloj(), fuente_inactividad=fuente_rota)
    estado = rastreador.actualizar()
    assert estado.motivo == "sin_datos" and estado.fuente_inactividad == "ninguna"

    rastreador = RastreadorPresencia(_settings(tmp_path), reloj=Reloj(),
                                     fuente_inactividad=lambda: (math.nan, "hypridle"))
    assert rastreador.actualizar().segundos_inactivo is None


def test_ausencia_min_configurable(tmp_path: Path) -> None:
    settings = _settings(tmp_path, RAMS_AWAY_MINUTES="2")
    rastreador = RastreadorPresencia(settings, reloj=Reloj(), fuente_inactividad=lambda: (150.0, "win32"))
    assert rastreador.actualizar().motivo == "inactividad"     # 150 s ≥ 2 min


# ---------------------------------------------------------------------------
# Canales
# ---------------------------------------------------------------------------


def test_telegram_no_cuenta_como_presencia(tmp_path: Path) -> None:
    rastreador, reloj, _ = _rastreador(tmp_path)
    rastreador.registrar_entrada("telegram")
    assert rastreador.ultima_entrada_remota_ts == pytest.approx(T0)
    assert rastreador.segundos_desde_entrada_local() is None
    reloj.avanzar(5)
    estado = rastreador.actualizar()
    assert estado.presente is False and estado.motivo == "sin_datos"
    assert estado.ultima_entrada_ts is None


def test_nexus_y_canales_desconocidos_se_ignoran(tmp_path: Path) -> None:
    rastreador, _, _ = _rastreador(tmp_path)
    rastreador.registrar_entrada("nexus")
    rastreador.registrar_entrada("hud")
    assert rastreador.segundos_desde_entrada_local() is None
    assert rastreador.ultima_entrada_remota_ts is None


# ---------------------------------------------------------------------------
# leer_inactividad
# ---------------------------------------------------------------------------


def test_leer_inactividad_no_lanza_en_la_plataforma_actual(tmp_path: Path) -> None:
    segundos, fuente = leer_inactividad(_settings(tmp_path))
    assert fuente in {"hypridle", "win32", "ninguna"}
    assert segundos is None or (isinstance(segundos, float) and segundos >= 0.0)
    if fuente == "ninguna":
        assert segundos is None


def test_rastreador_por_defecto_usa_el_sistema(tmp_path: Path) -> None:
    estado = RastreadorPresencia(_settings(tmp_path)).actualizar()
    assert isinstance(estado, EstadoPresencia)
    assert estado.fuente_inactividad in {"hypridle", "win32", "ninguna"}


class _ProcesoFalso:
    def __init__(self, nombre: str) -> None:
        self.info = {"name": nombre}


class _PsutilFalso:
    """Simula psutil con una lista de nombres de proceso."""

    def __init__(self, nombres: list[str]) -> None:
        self.nombres = nombres
        self.llamadas = 0

    def process_iter(self, atributos: list[str] | None = None):  # noqa: ANN201 - imita psutil
        self.llamadas += 1
        return [_ProcesoFalso(n) for n in self.nombres]


@pytest.fixture
def linux_simulado(monkeypatch: pytest.MonkeyPatch) -> _PsutilFalso:
    """``sys.platform == "linux"`` y psutil falso con hypridle corriendo (caché reiniciada)."""
    falso = _PsutilFalso(["systemd", "hypridle", "kitty"])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(presence, "psutil", falso)
    presence._reiniciar_cache_hypridle()
    yield falso
    presence._reiniciar_cache_hypridle()


def test_linux_sin_ruta_de_bandera_aunque_corra_hypridle(tmp_path: Path,
                                                         linux_simulado: _PsutilFalso) -> None:
    settings = _settings(tmp_path)
    assert settings.idle_flag_path is None
    assert leer_inactividad(settings) == (None, "ninguna")


def test_linux_con_bandera_existente(tmp_path: Path, linux_simulado: _PsutilFalso) -> None:
    bandera = tmp_path / "rams_idle"
    bandera.write_text("", encoding="utf-8")
    hace = time.time() - 40.0
    os.utime(bandera, (hace, hace))
    settings = _settings(tmp_path).con(idle_flag_path=bandera)
    segundos, fuente = leer_inactividad(settings)
    assert fuente == "hypridle"
    assert segundos == pytest.approx(40.0 + settings.idle_timeout_s, abs=5.0)


def test_linux_sin_bandera_con_hypridle_vivo(tmp_path: Path, linux_simulado: _PsutilFalso) -> None:
    settings = _settings(tmp_path).con(idle_flag_path=tmp_path / "no_existe")
    assert leer_inactividad(settings) == (0.0, "hypridle")
    # La comprobación del proceso se cachea 30 s.
    assert leer_inactividad(settings) == (0.0, "hypridle")
    assert linux_simulado.llamadas == 1


def test_linux_sin_bandera_ni_hypridle(tmp_path: Path, linux_simulado: _PsutilFalso) -> None:
    linux_simulado.nombres = ["systemd", "kitty"]
    settings = _settings(tmp_path).con(idle_flag_path=tmp_path / "no_existe")
    assert leer_inactividad(settings) == (None, "ninguna")


class _PsutilBloqueante(_PsutilFalso):
    """``process_iter`` se queda colgado hasta que se suelta ``liberar`` (simula un /proc lento)."""

    def __init__(self, nombres: list[str]) -> None:
        super().__init__(nombres)
        self.liberar = threading.Event()

    def process_iter(self, atributos: list[str] | None = None):  # noqa: ANN201 - imita psutil
        self.liberar.wait(5.0)
        return super().process_iter(atributos)


def test_cache_caducada_no_bloquea_y_refresca_en_segundo_plano(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.1: el recorrido de procesos nunca corre en el hilo del llamador (el event loop de main)."""
    lento = _PsutilBloqueante(["systemd", "kitty"])          # hypridle ya NO corre
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(presence, "psutil", lento)
    presence._reiniciar_cache_hypridle()
    try:
        settings = _settings(tmp_path).con(idle_flag_path=tmp_path / "no_existe")
        # Caché caducada que decía "corre": se devuelve ese valor al instante y se relanza.
        monkeypatch.setattr(presence, "_cache_hypridle",
                            (time.monotonic() - presence.CACHE_HYPRIDLE_S - 1.0, True))
        inicio = time.monotonic()
        assert leer_inactividad(settings) == (0.0, "hypridle")
        assert leer_inactividad(settings) == (0.0, "hypridle")       # un solo recorrido a la vez
        assert time.monotonic() - inicio < 0.2
        hilo = presence._hilo_hypridle
        assert hilo is not None and hilo.is_alive() and hilo.daemon
        lento.liberar.set()
        hilo.join(2.0)
        assert lento.llamadas == 1
        assert leer_inactividad(settings) == (None, "ninguna")       # ya refrescada
    finally:
        lento.liberar.set()
        presence._reiniciar_cache_hypridle()


def test_primer_recorrido_lento_espera_acotado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lento = _PsutilBloqueante(["hypridle"])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(presence, "psutil", lento)
    presence._reiniciar_cache_hypridle()
    try:
        settings = _settings(tmp_path).con(idle_flag_path=tmp_path / "no_existe")
        inicio = time.monotonic()
        assert leer_inactividad(settings) == (None, "ninguna")       # aún sin resultado
        assert time.monotonic() - inicio < presence.ESPERA_PRIMER_RECORRIDO_S + 0.3
        lento.liberar.set()
        presence._hilo_hypridle.join(2.0)
        assert leer_inactividad(settings) == (0.0, "hypridle")
    finally:
        lento.liberar.set()
        presence._reiniciar_cache_hypridle()


def test_linux_sin_psutil(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(presence, "psutil", None)
    presence._reiniciar_cache_hypridle()
    settings = _settings(tmp_path).con(idle_flag_path=tmp_path / "no_existe")
    assert leer_inactividad(settings) == (None, "ninguna")


def test_otro_sistema_sin_fuente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert leer_inactividad(_settings(tmp_path)) == (None, "ninguna")


@pytest.mark.skipif(sys.platform != "win32", reason="GetLastInputInfo solo existe en Windows")
def test_windows_get_last_input_info(tmp_path: Path) -> None:
    segundos, fuente = leer_inactividad(_settings(tmp_path))
    if fuente == "win32":
        assert segundos is not None and segundos >= 0.0
    else:   # sesión sin escritorio (servicio/CI)
        assert (segundos, fuente) == (None, "ninguna")


# ---------------------------------------------------------------------------
# v0.2 (docs/COMPANION.md §C3 "Grupo orquestacion"): escritorio, ControlPC e inyecciones
# ---------------------------------------------------------------------------


class PCFalso:
    """``ControlPC`` mínimo: inactividad de Windows e instante de la última inyección propia."""

    def __init__(self, reloj: Reloj) -> None:
        self.reloj = reloj
        self.ultimo_input: float | None = reloj.t     # instante del último input (cualquiera)
        self.inyeccion: float | None = None

    def inactividad_s(self) -> float | None:
        return None if self.ultimo_input is None else max(0.0, self.reloj.t - self.ultimo_input)

    def ts_ultima_inyeccion(self) -> float | None:
        return self.inyeccion

    def inyectar(self) -> None:          # Amadeus mueve el cursor o teclea: Windows reinicia la inactividad
        self.inyeccion = self.reloj.t
        self.ultimo_input = self.reloj.t

    def usuario(self) -> None:           # el usuario de verdad toca el teclado o el ratón
        self.ultimo_input = self.reloj.t


def test_escritorio_cuenta_como_entrada_local(tmp_path: Path) -> None:
    rastreador, reloj, _ = _rastreador(tmp_path)
    rastreador.registrar_entrada("escritorio")
    assert rastreador.segundos_desde_entrada_local() == pytest.approx(0.0)
    reloj.avanzar(30)
    estado = rastreador.actualizar()
    assert estado.presente is True and estado.motivo == "terminal_reciente"
    assert rastreador.ultima_entrada_remota_ts is None


def test_inactividad_de_control_pc_manda_sobre_la_fuente(tmp_path: Path) -> None:
    reloj = Reloj()
    fuente = Fuente(9999.0, "win32")
    pc = PCFalso(reloj)
    rastreador = RastreadorPresencia(_settings(tmp_path), reloj=reloj, fuente_inactividad=fuente,
                                     inactividad_pc=pc.inactividad_s)
    reloj.avanzar(42)
    estado = rastreador.actualizar()
    assert estado.segundos_inactivo == pytest.approx(42.0) and estado.fuente_inactividad == "win32"
    assert estado.presente is True and estado.motivo == "sistema_activo"
    # Hyprland: ControlPC no sabe la inactividad (None) → la fuente normal (archivo-bandera de hypridle).
    pc.ultimo_input = None
    fuente.valor = (12.0, "hypridle")
    estado = rastreador.actualizar()
    assert estado.segundos_inactivo == pytest.approx(12.0) and estado.fuente_inactividad == "hypridle"


def test_conectar_control_pc_despues_y_callbacks_rotos(tmp_path: Path) -> None:
    rastreador, reloj, fuente = _rastreador(tmp_path, 7.0, "hypridle")

    def roto() -> float:
        raise OSError("sin sesión de escritorio")

    rastreador.conectar_control_pc(inactividad_pc=roto, ts_ultima_inyeccion=roto)
    estado = rastreador.actualizar()                       # nunca lanza: usa la fuente normal
    assert estado.segundos_inactivo == pytest.approx(7.0) and estado.fuente_inactividad == "hypridle"
    pc = PCFalso(reloj)
    rastreador.conectar_control_pc(inactividad_pc=pc.inactividad_s, ts_ultima_inyeccion=pc.ts_ultima_inyeccion,
                                   reloj_inyeccion=reloj)
    reloj.avanzar(3)
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(3.0)
    rastreador.conectar_control_pc()                        # desconectar
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(7.0)


def test_reinicio_por_inyeccion_propia_se_ignora(tmp_path: Path) -> None:
    reloj = Reloj()
    pc = PCFalso(reloj)
    ajustes = _settings(tmp_path, RAMS_AWAY_MINUTES="10")
    rastreador = RastreadorPresencia(ajustes, reloj=reloj, fuente_inactividad=Fuente(None),
                                     inactividad_pc=pc.inactividad_s, ts_ultima_inyeccion=pc.ts_ultima_inyeccion,
                                     reloj_inyeccion=reloj)
    reloj.avanzar(500)
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(500.0)
    pc.inyectar()                                          # travesura: Windows marca actividad
    reloj.avanzar(0.5)
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(500.5)
    reloj.avanzar(200)
    estado = rastreador.actualizar()
    assert estado.segundos_inactivo == pytest.approx(700.5)      # sigue contando desde el input real
    assert estado.presente is False and estado.motivo == "inactividad"
    # El usuario mueve el ratón de verdad (lejos de la inyección): actividad real.
    reloj.avanzar(10)
    pc.usuario()
    estado = rastreador.actualizar()
    assert estado.segundos_inactivo == pytest.approx(0.0) and estado.presente is True


def test_input_real_cerca_de_la_inyeccion_tambien_se_ignora(tmp_path: Path) -> None:
    reloj = Reloj()
    pc = PCFalso(reloj)
    rastreador = RastreadorPresencia(_settings(tmp_path), reloj=reloj, fuente_inactividad=Fuente(None),
                                     inactividad_pc=pc.inactividad_s, ts_ultima_inyeccion=pc.ts_ultima_inyeccion,
                                     reloj_inyeccion=reloj)
    reloj.avanzar(60)
    rastreador.actualizar()
    pc.inyectar()
    reloj.avanzar(0.8)
    pc.usuario()                                           # a ±1 s de la inyección: no se distingue
    reloj.avanzar(1)
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(61.8)
    reloj.avanzar(5)
    pc.usuario()                                           # 6,8 s después: es del usuario
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(0.0)


def test_sin_ts_de_inyeccion_no_filtra(tmp_path: Path) -> None:
    reloj = Reloj()
    pc = PCFalso(reloj)
    rastreador = RastreadorPresencia(_settings(tmp_path), reloj=reloj, fuente_inactividad=Fuente(None),
                                     inactividad_pc=pc.inactividad_s)
    reloj.avanzar(100)
    rastreador.actualizar()
    pc.inyectar()
    reloj.avanzar(1)
    assert rastreador.actualizar().segundos_inactivo == pytest.approx(1.0)
