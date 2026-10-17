"""Tests del lóbulo visión/PC (grupo vision-pc, §10.17, §9.1, §9.2).

Sin hardware ni red: la webcam NUNCA se abre (``cv2`` se simula y ``_parpadeo`` se
parchea), ``hyprctl`` y demás binarios no se invocan (``_hyprctl`` y ``_correr`` se
parchean) y ninguna acción toca el PC real. Las pruebas asíncronas usan ``asyncio.run``
(fixture ``correr`` de tests/conftest.py).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import struct
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

import lobes.aura_vision_pc as aura_mod
from core.bus import BusEventos
from core.config import Settings
from core.contracts import (
    AccionPC,
    DiagnosticoModulo,
    EstadoModulo,
    ProveedorHypr,
    ResultadoAccion,
    ResultadoVistazo,
    Telemetria,
)
from core.permisos import CompuertaPermisos
from lobes.aura_vision_pc import (
    DIRECCION_RE,
    SAFE_CLASS_RE,
    AuraVisionPC,
    ControlHyprland,
    SalidaHyprctl,
)

PID_AJENO = 999_999


# ---------------------------------------------------------------------------
# Utilidades de prueba
# ---------------------------------------------------------------------------


class Reloj:
    """Reloj monotónico controlable (intervalos, *backoff* y alertas)."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class GrabadorHyprctl:
    """Sustituto de ``ControlHyprland._hyprctl``: registra las llamadas sin ejecutar nada."""

    def __init__(self, *respuestas: SalidaHyprctl | None) -> None:
        self.llamadas: list[tuple[str, ...]] = []
        self._respuestas = list(respuestas) or [SalidaHyprctl(0, "ok")]

    async def __call__(self, *args: str, timeout: float = 3.0) -> SalidaHyprctl | None:
        self.llamadas.append(tuple(args))
        if len(self._respuestas) > 1:
            return self._respuestas.pop(0)
        return self._respuestas[0]


def _hypr_simulado(settings: Settings, *, pids: frozenset[int] = frozenset(),
                   respuestas: tuple[SalidaHyprctl | None, ...] = ()) -> tuple[ControlHyprland, GrabadorHyprctl]:
    hypr = ControlHyprland(settings, pids_propios=pids)
    hypr.disponible = True
    grabador = GrabadorHyprctl(*respuestas)
    hypr._hyprctl = grabador  # type: ignore[method-assign]
    return hypr, grabador


def _eventos(bus: BusEventos, patron: str) -> list[tuple[str, dict[str, Any], float]]:
    registro: list[tuple[str, dict[str, Any], float]] = []
    bus.suscribir(patron, lambda ev: registro.append((ev.nombre, dict(ev.datos), time.monotonic())))
    return registro


@pytest.fixture
def sin_hyprland(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garantiza que no hay sesión de Hyprland (en cualquier plataforma)."""
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)


@pytest.fixture
def cv2_simulado(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """``cv2`` presente pero falso: la cámara real nunca se abre."""
    falso = types.SimpleNamespace(__name__="cv2_simulado")
    monkeypatch.setattr(aura_mod, "cv2", falso)
    return falso


@pytest.fixture
def settings_camara(settings_prueba: Settings) -> Settings:
    return settings_prueba.con(camara_habilitada=True)


def _parpadeo_fijo(resultado: ResultadoVistazo, llamadas: list[int] | None = None):
    def parpadeo(cancelar: threading.Event) -> ResultadoVistazo:
        if llamadas is not None:
            llamadas.append(1)
        return resultado
    return parpadeo


# ---------------------------------------------------------------------------
# Privacidad y API
# ---------------------------------------------------------------------------


def test_fuente_no_persiste_fotogramas() -> None:
    fuente = inspect.getsource(aura_mod)
    for prohibido in ("imwrite", "VideoWriter", "imencode"):
        assert prohibido not in fuente, prohibido
    assert "shell=True" not in fuente


def test_constantes_y_protocolo(settings_prueba: Settings) -> None:
    assert SAFE_CLASS_RE.pattern == r"^[A-Za-z0-9_.\-]{1,64}$"
    assert DIRECCION_RE.pattern == r"^0x[0-9a-fA-F]{1,16}$"
    assert isinstance(ControlHyprland(settings_prueba), ProveedorHypr)


def test_camara_deshabilitada_sin_importar_cv2(settings_prueba: Settings, bus: BusEventos, correr,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "cv2", None)
    vistazos = _eventos(bus, "vision.vistazo")
    aura = AuraVisionPC(settings_prueba, bus)
    assert aura.camara_habilitada is False
    r = correr(aura.vistazo())
    assert r.error == "camara_deshabilitada" and r.rostro is None and r.motivo == "curiosidad"
    assert [d["error"] for _, d, _ in vistazos] == ["camara_deshabilitada"]
    # El interruptor en tiempo de ejecución no puede saltarse el maestro RAMS_CAMERA=0.
    assert aura.set_camara(True) is False
    assert aura.camara_habilitada is False
    correr(aura.cerrar())


def test_sin_opencv(settings_camara: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "cv2", None)
    aura = AuraVisionPC(settings_camara)
    assert correr(aura.vistazo("usuario")).error == "sin_opencv"
    diagnostico = aura._diagnostico_camara()
    assert diagnostico.estado == EstadoModulo.OFF and diagnostico.detalle == "OpenCV no instalado"
    correr(aura.cerrar())


def test_set_camara_en_tiempo_de_ejecucion(settings_camara: Settings, bus: BusEventos, correr,
                                           cv2_simulado: types.SimpleNamespace) -> None:
    modulos = _eventos(bus, "sistema.modulo")
    aura = AuraVisionPC(settings_camara, bus)
    assert aura.camara_habilitada is True
    assert aura.set_camara(False) is False
    assert correr(aura.vistazo("usuario")).error == "camara_deshabilitada"
    assert aura.set_camara(True) is True
    assert [d["clave"] for _, d, _ in modulos] == ["camara", "camara"]
    assert modulos[0][1]["estado"] == "OFF"
    correr(aura.cerrar())
    assert aura.camara_habilitada is False


# ---------------------------------------------------------------------------
# Hyprland
# ---------------------------------------------------------------------------


def test_hyprland_no_disponible_en_windows_o_sin_variable(settings_prueba: Settings, correr,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    for es_linux in (False, True):     # Windows (o macOS) y Linux sin la variable
        monkeypatch.setattr(aura_mod, "_es_linux", lambda valor=es_linux: valor)
        monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
        hypr = ControlHyprland(settings_prueba)
        grabador = GrabadorHyprctl()
        hypr._hyprctl = grabador  # type: ignore[method-assign]
        assert hypr.disponible is False, es_linux
        assert hypr.dialecto == "ninguno"
        assert correr(hypr.enfocar_clase("kitty")) is False
        assert correr(hypr.consultar("clients")) is None
        assert correr(hypr.ventana_activa()) is None
        assert correr(hypr.detectar_dialecto()) == "ninguno"
        assert grabador.llamadas == []
    monkeypatch.setattr(aura_mod, "_es_linux", lambda: False)
    assert ControlHyprland(settings_prueba).motivo_no_disponible == "no disponible en este sistema"


def test_validacion_no_llama_a_hyprctl(settings_prueba: Settings, correr) -> None:
    hypr, grabador = _hypr_simulado(settings_prueba)

    async def principal() -> list[bool]:
        return [
            await hypr.enfocar_clase("a; rm -rf /"),
            await hypr.enfocar_clase("kitty\n"),
            await hypr.enfocar_clase('x")$" }) hl.dsp.exec_cmd("rm'),
            await hypr.cerrar_direccion("0xZZ"),
            await hypr.cerrar_direccion("0x55aa; killall"),
            await hypr.enfocar_direccion("address:0x1"),
            await hypr.ir_workspace(11),
            await hypr.ir_workspace(0),
            await hypr.ir_workspace(True),  # type: ignore[arg-type]
            await hypr.lanzar("bitcoinminer"),
            await hypr.lanzar("kitty"),       # es un comando, no un alias permitido
        ]

    assert correr(principal()) == [False] * 11
    assert grabador.llamadas == []


def test_cerrar_direccion_con_pid_propio_no_despacha(settings_prueba: Settings, correr) -> None:
    hypr, grabador = _hypr_simulado(settings_prueba, pids=frozenset({os.getpid()}))
    consultas: list[str] = []

    async def consultar(que: str) -> Any:
        consultas.append(que)
        return [{"address": "0xabc", "pid": os.getpid(), "class": "kitty", "title": "rams"}]

    hypr.consultar = consultar  # type: ignore[method-assign]
    assert correr(hypr.cerrar_direccion("0xabc")) is False
    assert consultas == ["clients"]
    assert not any(llamada and llamada[0] == "dispatch" for llamada in grabador.llamadas)


def test_cerrar_direccion_inexistente_no_despacha(settings_prueba: Settings, correr) -> None:
    hypr, grabador = _hypr_simulado(settings_prueba)

    async def consultar(que: str) -> Any:
        return [{"address": "0xdef", "pid": PID_AJENO}]

    hypr.consultar = consultar  # type: ignore[method-assign]
    assert correr(hypr.cerrar_direccion("0xabc")) is False
    assert grabador.llamadas == []


@pytest.mark.parametrize("modo, esperado", [
    ("legacy", ("dispatch", "closewindow", "address:0xabc")),
    ("lua", ("dispatch", 'hl.dsp.window.close({ window = "address:0xabc" })')),
])
def test_cerrar_direccion_ajena_despacha_con_selector(settings_prueba: Settings, correr, modo: str,
                                                      esperado: tuple[str, ...]) -> None:
    hypr, grabador = _hypr_simulado(settings_prueba.con(hypr_modo_dispatch=modo),
                                    pids=frozenset({os.getpid()}))

    async def consultar(que: str) -> Any:
        return [{"address": "0xABC", "pid": PID_AJENO}]

    hypr.consultar = consultar  # type: ignore[method-assign]
    assert correr(hypr.cerrar_direccion("0xabc")) is True
    assert grabador.llamadas == [esperado]
    assert all("killactive" not in " ".join(llamada) for llamada in grabador.llamadas)


def test_dispatch_por_dialecto(settings_prueba: Settings, correr) -> None:
    ajustes = settings_prueba.con(apps_permitidas_items=(("terminal", "kitty"),))
    legacy, grab_legacy = _hypr_simulado(ajustes.con(hypr_modo_dispatch="legacy"))
    lua, grab_lua = _hypr_simulado(ajustes.con(hypr_modo_dispatch="lua"))

    async def principal(h: ControlHyprland) -> list[bool]:
        return [await h.enfocar_clase("kitty"), await h.enfocar_direccion("0x55aa"),
                await h.ir_workspace(3), await h.lanzar("terminal")]

    assert correr(principal(legacy)) == [True] * 4
    assert correr(principal(lua)) == [True] * 4
    assert grab_legacy.llamadas == [
        ("dispatch", "focuswindow", "class:^(kitty)$"),
        ("dispatch", "focuswindow", "address:0x55aa"),
        ("dispatch", "workspace", "3"),
        ("dispatch", "exec", "kitty"),
    ]
    assert grab_lua.llamadas == [
        ("dispatch", 'hl.dsp.focus({ window = "class:^(kitty)$" })'),
        ("dispatch", 'hl.dsp.focus({ window = "address:0x55aa" })'),
        ("dispatch", 'hl.dsp.focus({ workspace = "3" })'),
        ("dispatch", "hl.dsp.exec_cmd('kitty')"),
    ]


def test_autocorreccion_del_dialecto(settings_prueba: Settings, correr) -> None:
    error_lua = SalidaHyprctl(0, '[string "workspace 3"]:1: syntax error near \'3\'')
    hypr, grabador = _hypr_simulado(settings_prueba, respuestas=(error_lua, SalidaHyprctl(0, "ok")))
    hypr._detectado = True
    hypr.dialecto = "legacy"
    assert correr(hypr.ir_workspace(3)) is True
    assert hypr.dialecto == "lua"
    assert grabador.llamadas == [("dispatch", "workspace", "3"),
                                 ("dispatch", 'hl.dsp.focus({ workspace = "3" })')]

    hypr2, grabador2 = _hypr_simulado(settings_prueba, respuestas=(SalidaHyprctl(0, "Invalid dispatcher"),
                                                                   SalidaHyprctl(0, "ok")))
    hypr2._detectado = True
    hypr2.dialecto = "lua"
    assert correr(hypr2.ir_workspace(4)) is True
    assert hypr2.dialecto == "legacy"
    assert grabador2.llamadas[-1] == ("dispatch", "workspace", "4")


def test_sin_autocorreccion_si_el_dialecto_esta_forzado(settings_prueba: Settings, correr) -> None:
    error_lua = SalidaHyprctl(0, "syntax error near 'workspace'")
    hypr, grabador = _hypr_simulado(settings_prueba.con(hypr_modo_dispatch="legacy"), respuestas=(error_lua,))
    assert correr(hypr.ir_workspace(3)) is False
    assert hypr.dialecto == "legacy"
    assert len(grabador.llamadas) == 1


def test_detectar_dialecto(settings_prueba: Settings, correr, tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def con_status(estado: Any) -> ControlHyprland:
        hypr, _ = _hypr_simulado(settings_prueba)

        async def consultar(que: str, *, timeout: float = 3.0) -> Any:
            assert que == "status"
            return estado

        hypr.consultar = consultar  # type: ignore[method-assign]
        return hypr

    assert correr(con_status({"version": "0.56.2", "configProvider": "lua"}).detectar_dialecto()) == "lua"
    assert correr(con_status({"config": {"path": "/home/u/.config/hypr/hyprland.conf"}}).detectar_dialecto()) \
        == "legacy"
    assert correr(con_status({"configFormat": "hyprlang"}).detectar_dialecto()) == "legacy"
    # Sin información en status: decide el archivo de configuración.
    assert correr(con_status(None).detectar_dialecto()) == "legacy"
    lua = tmp_path / "config" / "hypr" / "hyprland.lua"
    lua.parent.mkdir(parents=True)
    lua.write_text("-- config\n", encoding="utf-8")
    assert correr(con_status({"version": "0.54"}).detectar_dialecto()) == "lua"
    # Forzado por HYPR_DISPATCH_MODE: no consulta status.
    forzado, grabador = _hypr_simulado(settings_prueba.con(hypr_modo_dispatch="legacy"))
    assert correr(forzado.detectar_dialecto()) == "legacy"
    assert grabador.llamadas == []


def test_dialecto_desde_status_ignora_directorios() -> None:
    assert aura_mod._dialecto_desde_status({"configPath": "/home/lua_fan/.config/hypr/hyprland.conf"}) == "legacy"
    assert aura_mod._dialecto_desde_status({"configPath": "/home/u/.config/hypr/hyprland.lua"}) == "lua"
    assert aura_mod._dialecto_desde_status({"version": "0.54"}) is None
    assert aura_mod._dialecto_desde_status(None) is None


def test_consultar_parsea_json_y_tolera_fallos(settings_prueba: Settings, correr) -> None:
    hypr, grabador = _hypr_simulado(settings_prueba, respuestas=(SalidaHyprctl(0, '[{"address": "0x1"}]'),
                                                                 SalidaHyprctl(0, "no es json"),
                                                                 SalidaHyprctl(1, "", "error"),
                                                                 None))

    async def principal() -> list[Any]:
        return [await hypr.consultar("clients"), await hypr.consultar("clients"),
                await hypr.consultar("clients"), await hypr.consultar("clients"),
                await hypr.consultar("dispatch")]

    assert correr(principal()) == [[{"address": "0x1"}], None, None, None, None]
    assert grabador.llamadas == [("-j", "clients")] * 4


# ---------------------------------------------------------------------------
# Telemetría y alertas
# ---------------------------------------------------------------------------


def test_telemetria_con_psutil(settings_prueba: Settings, correr, sin_hyprland: None) -> None:
    pytest.importorskip("psutil")
    aura = AuraVisionPC(settings_prueba)

    async def sin_nvidia() -> None:
        return None

    aura._leer_nvidia = sin_nvidia  # type: ignore[method-assign]

    async def principal() -> tuple[Telemetria, Telemetria]:
        primera = await aura.telemetria()
        segunda = await aura.telemetria()   # caché de 2 s
        await aura.cerrar()
        return primera, segunda

    primera, segunda = correr(principal())
    assert isinstance(primera, Telemetria)
    assert primera.ram_total_gb > 0
    assert 0 <= primera.ram_pct <= 100 and 0 <= primera.disco_pct <= 100
    assert segunda is primera
    assert "RAM" in primera.resumen_textual()


def test_telemetria_sin_psutil(settings_prueba: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "psutil", None)
    aura = AuraVisionPC(settings_prueba)
    assert isinstance(correr(aura.telemetria()), Telemetria)
    parar = asyncio.Event()
    correr(aura.bucle_telemetria(parar))   # sin psutil termina enseguida
    diagnostico = correr(aura._diagnostico_telemetria())
    assert diagnostico.estado == EstadoModulo.OFF and diagnostico.detalle == "psutil no instalado"
    correr(aura.cerrar())


def test_alertas_por_flanco_con_persistencia_y_refractario(settings_prueba: Settings) -> None:
    reloj = Reloj(0.0)
    aura = AuraVisionPC(settings_prueba, reloj=reloj)
    caliente = Telemetria(cpu_pct=40, ram_total_gb=16, ram_usada_gb=8, ram_pct=50, disco_pct=50, temp_cpu=94,
                          proceso_top=("ffmpeg", 48211, 780.0))
    fria = Telemetria(cpu_pct=40, ram_total_gb=16, ram_usada_gb=8, ram_pct=50, disco_pct=50, temp_cpu=60)

    assert aura._evaluar_alertas(caliente, 0.0) == []
    assert aura._evaluar_alertas(caliente, 3.0) == []
    alertas = aura._evaluar_alertas(caliente, 9.0)          # 3.ª muestra seguida
    assert len(alertas) == 1
    alerta = alertas[0]
    assert set(alerta) == {"tipo", "valor", "umbral", "critica", "detalle"}
    assert alerta["tipo"] == "temperatura_cpu" and alerta["critica"] is True
    assert alerta["valor"] == 94.0 and alerta["umbral"] == 90.0
    assert alerta["detalle"] == "CPU a 94 °C sostenidos 9 s; proceso ffmpeg (PID 48211) al 780 %"
    assert aura._evaluar_alertas(caliente, 12.0) == []      # mismo episodio: sin repetir
    assert aura._evaluar_alertas(fria, 15.0) == []          # baja: se rearma el flanco
    for t in (18.0, 21.0, 24.0):                            # nuevo episodio dentro del refractario
        assert aura._evaluar_alertas(caliente, t) == []
    tardia = aura._evaluar_alertas(caliente, 9.0 + 600.0)    # vence el refractario y sigue caliente
    assert [a["tipo"] for a in tardia] == ["temperatura_cpu"]


def test_alertas_disco_ram_cpu(settings_prueba: Settings) -> None:
    aura = AuraVisionPC(settings_prueba, reloj=Reloj(0.0))
    disco = Telemetria(cpu_pct=10, ram_total_gb=16, ram_usada_gb=4, ram_pct=25, disco_pct=96)
    inmediata = aura._evaluar_alertas(disco, 0.0)
    assert [(a["tipo"], a["critica"]) for a in inmediata] == [("disco", True)]
    assert inmediata[0]["detalle"] == "Disco al 96 % de su capacidad"

    aura = AuraVisionPC(settings_prueba, reloj=Reloj(0.0))
    ram = Telemetria(cpu_pct=10, ram_total_gb=16.0, ram_usada_gb=15.0, ram_pct=94, disco_pct=40)
    assert aura._evaluar_alertas(ram, 0.0) == [] and aura._evaluar_alertas(ram, 3.0) == []
    alerta_ram = aura._evaluar_alertas(ram, 6.0)
    assert [(a["tipo"], a["critica"]) for a in alerta_ram] == [("ram", False)]
    assert alerta_ram[0]["detalle"].startswith("RAM al 94 % (15,0 de 16,0 GB)")

    aura = AuraVisionPC(settings_prueba, reloj=Reloj(0.0))
    cpu = Telemetria(cpu_pct=99, ram_total_gb=16, ram_usada_gb=4, ram_pct=25, disco_pct=40)
    for t in range(0, 60, 3):
        assert aura._evaluar_alertas(cpu, float(t)) == [], t
    alerta_cpu = aura._evaluar_alertas(cpu, 60.0)
    assert [(a["tipo"], a["critica"]) for a in alerta_cpu] == [("cpu", False)]
    assert alerta_cpu[0]["detalle"] == "CPU al 99 % sostenido 60 s"


def test_bucle_telemetria_publica(settings_prueba: Settings, bus: BusEventos, correr) -> None:
    aura = AuraVisionPC(settings_prueba, bus, reloj=Reloj(0.0))
    publicados = _eventos(bus, "pc.*")
    lectura = Telemetria(cpu_pct=12, ram_total_gb=16, ram_usada_gb=4, ram_pct=25, disco_pct=97)

    async def falsa() -> Telemetria:
        return lectura

    aura.telemetria = falsa  # type: ignore[method-assign]
    if aura_mod.psutil is None:
        pytest.skip("psutil no instalado")

    async def principal() -> None:
        parar = asyncio.Event()
        tarea = asyncio.ensure_future(aura.bucle_telemetria(parar, periodo_s=0.5))
        await asyncio.sleep(0.2)
        parar.set()
        await asyncio.wait_for(tarea, 2)
        await aura.cerrar()

    correr(principal())
    nombres = [n for n, _, _ in publicados]
    assert nombres[:2] == ["pc.telemetria", "pc.alerta"]
    assert publicados[0][1]["disco_pct"] == 97.0
    assert publicados[1][1]["tipo"] == "disco"


# ---------------------------------------------------------------------------
# Acciones
# ---------------------------------------------------------------------------


def test_ejecutar_cerrar_ventana_origen_llm(settings_prueba: Settings, bus: BusEventos, correr) -> None:
    permisos = CompuertaPermisos(settings_prueba, bus, preguntar=lambda _t: None)
    aura = AuraVisionPC(settings_prueba, bus, permisos=permisos)
    r = correr(aura.ejecutar(AccionPC("cerrar_ventana", origen="llm")))
    assert isinstance(r, ResultadoAccion)
    assert r.ok is False
    assert r.mensaje.startswith("Acción no autorizada:")
    assert r.accion_tipo == "cerrar_ventana"
    assert permisos.hay_pendiente() is False
    correr(aura.cerrar())


def test_ejecutar_sin_hyprland_ni_nexus(settings_prueba: Settings, correr, sin_hyprland: None,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "NexusLink", None)
    aura = AuraVisionPC(settings_prueba)
    assert aura.nexus is None
    aura.hypr.disponible = False

    async def principal() -> list[ResultadoAccion]:
        return [
            await aura.ejecutar(AccionPC("ir_workspace", {"n": 3}, "cambiar al espacio 3")),
            await aura.ejecutar(AccionPC("enfocar_nexus", {}, "traer Nexus IDE al frente")),
            await aura.ejecutar(AccionPC("captura_pantalla", {}, "capturar la pantalla")),
            await aura.ejecutar(AccionPC("vistazo", {"motivo": "usuario"})),
            await aura.ejecutar(AccionPC("lanzar_app", {"alias": "bitcoinminer"})),
            await aura.ejecutar(AccionPC("comando", {"cmd": "ls"})),
            await aura.ejecutar(_cerrar_ajena()),   # sin permisos: "confirmar" no basta
        ]

    resultados = correr(principal())
    assert all(r.ok is False for r in resultados)
    assert resultados[0].mensaje == "Hyprland no disponible en este sistema."
    assert resultados[1].mensaje == "Nexus IDE no está integrado en esta instalación."
    assert resultados[2].mensaje == "Acción no soportada por ejecutar"
    assert resultados[3].mensaje == "Acción no soportada por ejecutar"
    assert resultados[4].mensaje.startswith("Acción no autorizada:")
    assert resultados[5].mensaje.startswith("Acción no autorizada:")
    assert resultados[6].mensaje.startswith("Acción no autorizada:")
    correr(aura.cerrar())


def _cerrar_ajena() -> AccionPC:
    return AccionPC("cerrar_ventana", {"direccion": "0x55aa", "pid": PID_AJENO, "clase": "firefox",
                                       "titulo": "Docs"}, "cerrar firefox «Docs»")


def test_ejecutar_con_hyprland_simulado(settings_prueba: Settings, bus: BusEventos, correr) -> None:
    permisos = CompuertaPermisos(settings_prueba, bus, preguntar=lambda _t: None,
                                 pids_propios=frozenset({os.getpid()}))
    aura = AuraVisionPC(settings_prueba, bus, permisos=permisos)
    aura.hypr.disponible = True
    aura.hypr._forzado = "legacy"
    grabador = GrabadorHyprctl()
    aura.hypr._hyprctl = grabador  # type: ignore[method-assign]

    async def consultar(que: str) -> Any:
        return [{"address": "0x55aa", "pid": PID_AJENO}] if que == "clients" else None

    aura.hypr.consultar = consultar  # type: ignore[method-assign]

    async def principal() -> tuple[ResultadoAccion, ResultadoAccion]:
        espacio = await aura.ejecutar(AccionPC("ir_workspace", {"n": 3}, "cambiar al espacio 3"))
        tarea = asyncio.ensure_future(aura.ejecutar(_cerrar_ajena()))
        while not permisos.hay_pendiente():
            await asyncio.sleep(0.005)
        assert "firefox" in permisos.pendiente().accion.descripcion
        permisos.resolver("sí")
        return espacio, await tarea

    espacio, cierre = correr(principal())
    assert espacio.ok is True and espacio.mensaje == "Espacio 3 activo."
    assert cierre.ok is True and cierre.mensaje == "Cerré firefox «Docs»."
    assert ("dispatch", "closewindow", "address:0x55aa") in grabador.llamadas
    correr(aura.cerrar())


def test_ejecutar_nunca_lanza(settings_prueba: Settings, correr) -> None:
    aura = AuraVisionPC(settings_prueba)
    aura.hypr.disponible = True

    async def roto(n: int) -> bool:
        raise RuntimeError("hyprctl explotó")

    aura.hypr.ir_workspace = roto  # type: ignore[method-assign]
    r = correr(aura.ejecutar(AccionPC("ir_workspace", {"n": 2})))
    assert r.ok is False and r.mensaje == "Error interno al ejecutar la acción."
    assert correr(aura.ejecutar("no es una acción")).ok is False  # type: ignore[arg-type]
    correr(aura.cerrar())


def test_nexus_opcional(settings_prueba: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "NexusLink", None)
    aura = AuraVisionPC(settings_prueba)
    assert aura.nexus is None
    diagnostico = correr(aura._diagnostico_nexus())
    assert diagnostico.clave == "nexus" and diagnostico.estado == EstadoModulo.OFF

    class NexusRoto:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("sin psutil")

    monkeypatch.setattr(aura_mod, "NexusLink", NexusRoto)
    assert AuraVisionPC(settings_prueba).nexus is None

    recibidos: dict[str, Any] = {}

    class NexusFalso:
        def __init__(self, settings: Settings, *, hypr: Any = None) -> None:
            recibidos["hypr"] = hypr

        def diagnostico(self) -> DiagnosticoModulo:
            return DiagnosticoModulo("nexus", "Nexus IDE", EstadoModulo.EN_ESPERA, "no detectado")

        async def enfocar(self) -> bool:
            return True

        async def cerrar(self) -> None:
            recibidos["cerrado"] = True

    monkeypatch.setattr(aura_mod, "NexusLink", NexusFalso)
    aura = AuraVisionPC(settings_prueba)
    assert recibidos["hypr"] is aura.hypr
    assert correr(aura._diagnostico_nexus()).detalle == "no detectado"
    r = correr(aura.ejecutar(AccionPC("enfocar_nexus", {}, "traer Nexus IDE al frente", origen="nexus")))
    assert r.ok is True and r.mensaje == "Nexus IDE al frente."
    correr(aura.cerrar())
    assert recibidos["cerrado"] is True


def test_capturar_pantalla_autoriza_y_degrada(settings_prueba: Settings, bus: BusEventos, correr,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    async def prohibido(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no debe lanzarse ningún subproceso")

    monkeypatch.setattr(aura_mod, "_correr", prohibido)
    permisos = CompuertaPermisos(settings_prueba, bus, preguntar=lambda _t: None)
    aura = AuraVisionPC(settings_prueba, bus, permisos=permisos)
    assert correr(aura.capturar_pantalla(origen="telegram")) is None
    assert correr(aura.capturar_pantalla(origen="iniciativa")) is None
    monkeypatch.setattr(aura_mod, "_es_linux", lambda: False)
    assert correr(aura.capturar_pantalla(origen="usuario")) is None   # Windows: no implementado
    correr(aura.cerrar())


def test_capturar_pantalla_reescala_con_grim(settings_prueba: Settings, correr,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def png(ancho: int) -> bytes:
        return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", ancho, 1080)
                + b"\x08\x06\x00\x00\x00" + b"\x00" * 16)

    llamadas: list[list[str]] = []

    async def correr_falso(argv: list[str], *, timeout: float, procesos: Any = None) -> tuple[int, bytes, bytes]:
        llamadas.append(list(argv))
        return 0, (png(1280) if "-s" in argv else png(2560)), b""

    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setattr(aura_mod, "_es_linux", lambda: True)
    monkeypatch.setattr(aura_mod.shutil, "which", lambda nombre: f"/usr/bin/{nombre}")
    monkeypatch.setattr(aura_mod, "_correr", correr_falso)
    monkeypatch.setattr(aura_mod, "cv2", None)   # sin cv2: el ancho sale de la cabecera IHDR
    aura = AuraVisionPC(settings_prueba)

    async def escala() -> float:
        return 1.0

    aura._escala_monitores = escala  # type: ignore[method-assign]
    resultado = correr(aura.capturar_pantalla(1280))
    assert resultado == png(1280)
    assert llamadas[0] == ["/usr/bin/grim", "-t", "png", "-"]
    assert llamadas[1] == ["/usr/bin/grim", "-s", "0.5000", "-t", "png", "-"]
    # Si no hace falta reducir, devuelve los bytes originales de grim (una sola invocación).
    llamadas.clear()
    assert correr(aura.capturar_pantalla(4000)) == png(2560)
    assert len(llamadas) == 1
    correr(aura.cerrar())


# ---------------------------------------------------------------------------
# Cámara: vistazo (sin hardware)
# ---------------------------------------------------------------------------


def test_vistazo_ok_publica_y_no_expone_imagenes(settings_camara: Settings, bus: BusEventos, correr,
                                                 cv2_simulado: types.SimpleNamespace) -> None:
    eventos = _eventos(bus, "vision.*")
    aura = AuraVisionPC(settings_camara, bus)
    aura._parpadeo = _parpadeo_fijo(ResultadoVistazo(True, n_rostros=1, duracion_ms=250, brillo=0.4))  # type: ignore[method-assign]
    r = correr(aura.vistazo("usuario"))
    assert r.error is None and r.rostro is True and r.n_rostros == 1
    assert r.motivo == "usuario" and r.duracion_ms == 250
    nombres = [(n, d.get("activa")) for n, d, _ in eventos]
    assert nombres == [("vision.camara", True), ("vision.camara", False), ("vision.vistazo", None)]
    assert eventos[0][1] == {"activa": True, "habilitada": True, "duracion_ms": 0}
    assert set(eventos[1][1]) == {"activa", "habilitada", "duracion_ms"}
    assert set(eventos[2][1]) == {"rostro", "n_rostros", "duracion_ms", "brillo", "motivo", "error", "ts"}
    assert aura._ocupada is False
    correr(aura.cerrar())


def test_vistazo_origen_telegram_o_nexus_denegado(settings_camara: Settings, bus: BusEventos, correr,
                                                  cv2_simulado: types.SimpleNamespace) -> None:
    llamadas: list[int] = []
    permisos = CompuertaPermisos(settings_camara, bus, preguntar=lambda _t: None)
    aura = AuraVisionPC(settings_camara, bus, permisos=permisos)
    aura._parpadeo = _parpadeo_fijo(ResultadoVistazo(True), llamadas)  # type: ignore[method-assign]
    assert correr(aura.vistazo("curiosidad", origen="telegram")).error == "denegada"
    assert correr(aura.vistazo("curiosidad", origen="nexus")).error == "denegada"
    assert correr(aura.vistazo("espiar")).error == "denegada"
    assert llamadas == []
    # Por iniciativa (origen por defecto) sí se permite.
    assert correr(aura.vistazo("curiosidad")).error is None
    assert llamadas == [1]
    correr(aura.cerrar())


def test_camara_colgada_tiempo_agotado_y_ocupada(settings_camara: Settings, bus: BusEventos, correr,
                                                 cv2_simulado: types.SimpleNamespace) -> None:
    camara = _eventos(bus, "vision.camara")
    vistazos = _eventos(bus, "vision.vistazo")
    fin_hilo: dict[str, float] = {}
    aura = AuraVisionPC(settings_camara, bus)

    def parpadeo_colgado(cancelar: threading.Event) -> ResultadoVistazo:
        time.sleep(4.0)                       # driver colgado: ignora la cancelación
        fin_hilo["t"] = time.monotonic()
        return ResultadoVistazo(False, duracion_ms=4000)

    aura._parpadeo = parpadeo_colgado  # type: ignore[method-assign]

    async def principal() -> dict[str, Any]:
        inicio = time.monotonic()
        primero = await aura.vistazo("usuario")
        transcurrido = time.monotonic() - inicio
        vistazos_en_timeout = [d["error"] for _, d, _ in vistazos]
        segundo = await aura.vistazo("usuario")
        apagados_antes = [d for _, d, _ in camara if d["activa"] is False]
        for _ in range(200):                  # espera a que el hilo suelte la cámara
            if not aura._ocupada:
                break
            await asyncio.sleep(0.05)
        await aura.cerrar()
        return {"primero": primero, "segundo": segundo, "transcurrido": transcurrido,
                "apagados_antes": apagados_antes, "vistazos_en_timeout": vistazos_en_timeout}

    datos = correr(principal())
    assert datos["primero"].error == "tiempo_agotado"
    assert 2.8 <= datos["transcurrido"] < 3.9
    assert datos["segundo"].error == "ocupada"
    assert datos["apagados_antes"] == []      # el indicador NO se apagó al vencer el timeout
    apagados = [(d, t) for _, d, t in camara if d["activa"] is False]
    assert len(apagados) == 1
    assert apagados[0][1] >= fin_hilo["t"]    # llegó después de que el hilo terminó
    assert apagados[0][0]["duracion_ms"] >= 3900
    # El vision.vistazo del parpadeo colgado lo publica el done callback con el resultado REAL
    # (§10.17, paso 2), no el timeout: antes solo sale el "ocupada" del segundo intento.
    assert datos["vistazos_en_timeout"] == []
    assert [d["error"] for _, d, _ in vistazos] == ["ocupada", None]
    assert vistazos[1][1]["rostro"] is False and vistazos[1][1]["motivo"] == "usuario"
    assert vistazos[1][2] >= fin_hilo["t"]
    assert aura._ocupada is False


def test_resultado_tardio_tras_timeout_llega_al_bus(settings_camara: Settings, bus: BusEventos, correr,
                                                    cv2_simulado: types.SimpleNamespace,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Un hilo que termina después del timeout con rostro publica ese rostro (conectoma, presencia)."""
    monkeypatch.setattr(aura_mod, "TIMEOUT_CAMARA_S", 0.2)
    vistazos = _eventos(bus, "vision.vistazo")
    camara = _eventos(bus, "vision.camara")
    fin_hilo: dict[str, float] = {}
    aura = AuraVisionPC(settings_camara, bus)
    aura._escalon = 2

    def parpadeo_lento(cancelar: threading.Event) -> ResultadoVistazo:
        time.sleep(0.5)                       # ya estaba dentro de read(): no ve la cancelación
        fin_hilo["t"] = time.monotonic()
        return ResultadoVistazo(True, n_rostros=1, duracion_ms=500)

    aura._parpadeo = parpadeo_lento  # type: ignore[method-assign]

    async def principal() -> tuple[ResultadoVistazo, list[Any]]:
        r = await aura.vistazo("curiosidad")
        en_timeout = list(vistazos)
        for _ in range(100):
            if not aura._ocupada:
                break
            await asyncio.sleep(0.02)
        await aura.cerrar()
        return r, en_timeout

    r, en_timeout = correr(principal())
    assert r.error == "tiempo_agotado" and r.rostro is None
    assert en_timeout == []
    assert len(vistazos) == 1
    _, datos, t = vistazos[0]
    assert datos["rostro"] is True and datos["n_rostros"] == 1 and datos["error"] is None
    assert datos["motivo"] == "curiosidad"
    assert t >= fin_hilo["t"]
    assert aura._escalon == 0                 # backoff coherente con lo publicado (con rostro)
    assert [d["activa"] for _, d, _ in camara] == [True, False]


def test_hilo_que_atiende_la_cancelacion_publica_tiempo_agotado(settings_camara: Settings, bus: BusEventos,
                                                                correr, cv2_simulado: types.SimpleNamespace,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "TIMEOUT_CAMARA_S", 0.2)
    vistazos = _eventos(bus, "vision.vistazo")
    aura = AuraVisionPC(settings_camara, bus)

    def parpadeo_cooperativo(cancelar: threading.Event) -> ResultadoVistazo:
        cancelar.wait(2.0)                    # como _parpadeo: sale entre grab() al cancelarse
        return ResultadoVistazo(None, error="tiempo_agotado", duracion_ms=200)

    aura._parpadeo = parpadeo_cooperativo  # type: ignore[method-assign]

    async def principal() -> ResultadoVistazo:
        r = await aura.vistazo("usuario")
        for _ in range(100):
            if not aura._ocupada:
                break
            await asyncio.sleep(0.02)
        await aura.cerrar()
        return r

    assert correr(principal()).error == "tiempo_agotado"
    assert [(d["error"], d["motivo"]) for _, d, _ in vistazos] == [("tiempo_agotado", "usuario")]
    assert aura._ocupada is False


def test_backoff_con_reloj_inyectado(settings_camara: Settings, correr,
                                     cv2_simulado: types.SimpleNamespace) -> None:
    reloj = Reloj(1000.0)
    aura = AuraVisionPC(settings_camara, reloj=reloj)
    aura._parpadeo = _parpadeo_fijo(ResultadoVistazo(False, duracion_ms=200))  # type: ignore[method-assign]
    assert correr(aura.vistazo("curiosidad")).error is None             # sin rostro ⇒ escalón 2 (5 min)
    reloj.t += 120
    assert correr(aura.vistazo("curiosidad")).error == "backoff"
    aura.reiniciar_backoff_camara()                                      # entrada local del usuario
    reloj.t += 91
    assert correr(aura.vistazo("curiosidad")).error is None             # 90 s del primer escalón
    # Los vistazos del usuario solo respetan su mínimo de 5 s.
    reloj.t += 2
    assert correr(aura.vistazo("usuario")).error == "intervalo"
    reloj.t += 4
    assert correr(aura.vistazo("usuario")).error is None
    # Tras otro vistazo sin rostro, el escalón sube a 15 min.
    reloj.t += 301
    assert correr(aura.vistazo("presencia")).error is None
    reloj.t += 600
    assert correr(aura.vistazo("presencia")).error == "backoff"
    # Un vistazo CON rostro devuelve al primer escalón.
    aura._parpadeo = _parpadeo_fijo(ResultadoVistazo(True, n_rostros=1))  # type: ignore[method-assign]
    reloj.t += 301
    assert correr(aura.vistazo("curiosidad")).error is None
    reloj.t += 91
    assert correr(aura.vistazo("curiosidad")).error is None
    correr(aura.cerrar())


def test_limite_diario(settings_camara: Settings, correr, cv2_simulado: types.SimpleNamespace) -> None:
    reloj = Reloj(0.0)
    ajustes = settings_camara.con(camara_max_dia=2, camara_intervalo_min_s=0.0)
    aura = AuraVisionPC(ajustes, reloj=reloj)
    aura._parpadeo = _parpadeo_fijo(ResultadoVistazo(True, n_rostros=1))  # type: ignore[method-assign]
    assert correr(aura.vistazo("curiosidad")).error is None
    reloj.t += 1
    assert correr(aura.vistazo("presencia")).error is None
    reloj.t += 1
    assert correr(aura.vistazo("curiosidad")).error == "limite_diario"
    reloj.t += 10
    assert correr(aura.vistazo("usuario")).error is None   # el usuario no cuenta para el límite
    correr(aura.cerrar())


# ---------------------------------------------------------------------------
# Cámara: _parpadeo con un OpenCV falso (sin hardware)
# ---------------------------------------------------------------------------


class CapturaFalsa:
    instancias: list["CapturaFalsa"] = []

    def __init__(self, indice: int, backend: int, *, abierta: bool = True, read_falla: bool = False) -> None:
        self.indice, self.backend = indice, backend
        self.abierta, self.read_falla = abierta, read_falla
        self.grabs = 0
        self.liberada = False
        self.ajustes: list[tuple[int, int]] = []
        CapturaFalsa.instancias.append(self)

    def isOpened(self) -> bool:  # noqa: N802 - API de OpenCV
        return self.abierta

    def set(self, prop: int, valor: int) -> bool:
        self.ajustes.append((prop, valor))
        return True

    def grab(self) -> bool:
        self.grabs += 1
        return True

    def read(self) -> tuple[bool, Any]:
        if self.read_falla:
            raise RuntimeError("V4L2 select timeout")
        np = pytest.importorskip("numpy")
        return True, np.full((480, 640, 3), 128, dtype=np.uint8)

    def release(self) -> None:
        self.liberada = True


def _cv2_falso(*, abierta: bool = True, read_falla: bool = False, haar_dir: Path | None = None,
               caras: int = 0) -> types.SimpleNamespace:
    CapturaFalsa.instancias = []
    falso = types.SimpleNamespace(
        CAP_V4L2=200, CAP_DSHOW=700, CAP_ANY=0, CAP_PROP_FOURCC=6, CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4, COLOR_BGR2GRAY=6, COLOR_BGRA2GRAY=10, COLOR_GRAY2BGR=8, COLOR_BGRA2BGR=3,
        VideoCapture=lambda indice, backend: CapturaFalsa(indice, backend, abierta=abierta, read_falla=read_falla),
        cvtColor=lambda img, codigo: img[..., 0],
        equalizeHist=lambda img: img,
    )
    if haar_dir is not None:
        class Cascada:
            def __init__(self, ruta: str) -> None:
                self.ruta = ruta

            def empty(self) -> bool:
                return False

            def detectMultiScale(self, img: Any, scaleFactor: float, minNeighbors: int,  # noqa: N802,N803
                                 minSize: tuple[int, int]) -> list[tuple[int, int, int, int]]:
                assert (scaleFactor, minNeighbors, minSize) == (1.1, 5, (60, 60))
                return [(10, 10, 80, 80)] * caras

        falso.CascadeClassifier = Cascada
        falso.data = types.SimpleNamespace(haarcascades=str(haar_dir))
    return falso


def test_parpadeo_libera_siempre_y_sin_detector(settings_camara: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    monkeypatch.setattr(aura_mod, "cv2", _cv2_falso())
    aura = AuraVisionPC(settings_camara)
    r = aura._parpadeo(threading.Event())
    captura = CapturaFalsa.instancias[-1]
    assert captura.liberada is True and captura.grabs == 4
    assert captura.indice == settings_camara.camara_indice
    assert (6, aura_mod._FOURCC_MJPG) in captura.ajustes
    assert r.error is None and r.rostro is None and r.n_rostros == 0   # sin Haar ni YuNet
    assert r.brillo == pytest.approx(128 / 255, abs=1e-3)
    assert aura._diagnostico_camara().detalle == "sin detector de rostros"
    aura._ejecutor.shutdown(wait=False)


def test_parpadeo_no_abre_cancelado_y_error(settings_camara: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    aura = AuraVisionPC(settings_camara)
    monkeypatch.setattr(aura_mod, "cv2", _cv2_falso(abierta=False))
    assert aura._parpadeo(threading.Event()).error == "no_abre"
    assert CapturaFalsa.instancias[-1].liberada is True

    monkeypatch.setattr(aura_mod, "cv2", _cv2_falso())
    cancelado = threading.Event()
    cancelado.set()
    assert aura._parpadeo(cancelado).error == "tiempo_agotado"
    assert CapturaFalsa.instancias[-1].grabs == 0 and CapturaFalsa.instancias[-1].liberada is True

    monkeypatch.setattr(aura_mod, "cv2", _cv2_falso(read_falla=True))
    with pytest.raises(RuntimeError):
        aura._parpadeo(threading.Event())
    assert CapturaFalsa.instancias[-1].liberada is True     # release() en el finally
    aura._ejecutor.shutdown(wait=False)


def test_parpadeo_con_haar(settings_camara: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    (tmp_path / "haarcascade_frontalface_default.xml").write_text("<opencv_storage/>", encoding="utf-8")
    monkeypatch.setattr(aura_mod, "cv2", _cv2_falso(haar_dir=tmp_path, caras=2))
    aura = AuraVisionPC(settings_camara)
    r = aura._parpadeo(threading.Event())
    assert r.rostro is True and r.n_rostros == 2
    assert aura._diagnostico_camara().estado == EstadoModulo.EN_ESPERA
    aura._ejecutor.shutdown(wait=False)


def test_vistazo_hilo_con_excepcion(settings_camara: Settings, bus: BusEventos, correr,
                                    cv2_simulado: types.SimpleNamespace) -> None:
    camara = _eventos(bus, "vision.camara")
    aura = AuraVisionPC(settings_camara, bus)

    def roto(cancelar: threading.Event) -> ResultadoVistazo:
        raise RuntimeError("driver roto")

    aura._parpadeo = roto  # type: ignore[method-assign]
    r = correr(aura.vistazo("usuario"))
    assert r.error == "no_abre" and r.rostro is None
    assert [d["activa"] for _, d, _ in camara] == [True, False]
    assert aura._ocupada is False
    correr(aura.cerrar())


# ---------------------------------------------------------------------------
# Eventos de Hyprland → pc.ventana
# ---------------------------------------------------------------------------


def test_eventos_activewindow_limitados_a_300_ms(settings_prueba: Settings, bus: BusEventos, correr) -> None:
    ventanas = _eventos(bus, "pc.ventana")
    aura = AuraVisionPC(settings_prueba, bus)
    actual = {"class": "kitty", "title": "nvim main.py", "address": "0x55aa", "pid": 4242}

    async def ventana_activa() -> dict[str, Any] | None:
        return dict(actual)

    aura.hypr.ventana_activa = ventana_activa  # type: ignore[method-assign]

    async def principal() -> None:
        await aura._al_evento_hypr("activewindow", "kitty,nvim main.py")
        await asyncio.sleep(0.01)
        for i in range(5):                       # ráfaga: solo debe salir la última
            actual["title"] = f"ventana {i}"
            await aura._al_evento_hypr("activewindow", f"kitty,ventana {i}")
            await aura._al_evento_hypr("workspace", "3")
        await asyncio.sleep(0.45)
        await aura.cerrar()

    correr(principal())
    assert len(ventanas) == 2
    assert ventanas[0][1] == {"clase": "kitty", "titulo": "nvim main.py", "direccion": "0x55aa", "pid": 4242}
    assert ventanas[1][1]["titulo"] == "ventana 4"
    assert ventanas[1][2] - ventanas[0][2] >= 0.28


def test_eventos_activewindow_sin_consulta(settings_prueba: Settings, bus: BusEventos, correr) -> None:
    ventanas = _eventos(bus, "pc.ventana")
    aura = AuraVisionPC(settings_prueba, bus)

    async def sin_respuesta() -> None:
        return None

    aura.hypr.ventana_activa = sin_respuesta  # type: ignore[method-assign]

    async def principal() -> None:
        await aura._al_evento_hypr("activewindow", "firefox,Docs, con comas" + "x" * 300)
        await asyncio.sleep(0.05)
        await aura.cerrar()

    correr(principal())
    assert len(ventanas) == 1
    datos = ventanas[0][1]
    assert datos["clase"] == "firefox" and datos["direccion"] is None and datos["pid"] is None
    assert datos["titulo"].startswith("Docs, con comas") and len(datos["titulo"]) == 200


def test_bucle_eventos_sin_hyprland_termina(settings_prueba: Settings, correr, sin_hyprland: None) -> None:
    aura = AuraVisionPC(settings_prueba)
    parar = asyncio.Event()
    correr(asyncio.wait_for(aura.bucle_eventos_hypr(parar), 2))
    correr(aura.cerrar())


# ---------------------------------------------------------------------------
# Arranque y cierre
# ---------------------------------------------------------------------------


def test_iniciar_diagnosticos_y_cerrar(settings_prueba: Settings, correr, sin_hyprland: None) -> None:
    aura = AuraVisionPC(settings_prueba)

    async def principal() -> list[DiagnosticoModulo]:
        diagnosticos = await aura.iniciar()
        await aura.cerrar()
        await aura.cerrar()          # idempotente
        return diagnosticos

    diagnosticos = correr(principal())
    assert [d.clave for d in diagnosticos] == ["camara", "telemetria", "hyprland", "nexus"]
    assert all(isinstance(d, DiagnosticoModulo) for d in diagnosticos)
    camara, telemetria, hyprland, _nexus = diagnosticos
    assert camara.nombre == "Lóbulo visual (cámara)" and camara.estado == EstadoModulo.OFF
    assert hyprland.estado == EstadoModulo.OFF
    if aura_mod.psutil is not None:
        assert telemetria.estado == EstadoModulo.OK
    # Tras cerrar: la cámara queda inutilizable y la telemetría no lanza.
    assert correr(aura.vistazo("usuario")).error == "camara_deshabilitada"
    assert isinstance(correr(aura.telemetria()), Telemetria)


def test_iniciar_cabe_en_el_plazo_de_main_con_hyprctl_colgado(settings_prueba: Settings, correr, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """main envuelve iniciar() en wait_for(…, 4): un hyprctl colgado no puede tumbar los 4 diagnósticos."""
    llamadas: list[tuple[tuple[str, ...], float]] = []

    async def correr_colgado(argv: list[str], *, timeout: float, procesos: Any = None) -> None:
        llamadas.append((tuple(argv[1:]), timeout))
        await asyncio.sleep(timeout + 1.0)    # agota su plazo y la espera tras matar el proceso
        return None

    monkeypatch.setattr(aura_mod, "_correr", correr_colgado)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    aura = AuraVisionPC(settings_prueba)
    aura.hypr.disponible = True
    aura.hypr._ruta_hyprctl = "hyprctl"
    aura.hypr._forzado = None

    async def principal() -> tuple[list[DiagnosticoModulo], float]:
        inicio = time.monotonic()
        diagnosticos = await asyncio.wait_for(aura.iniciar(), timeout=4.0)   # como main._medir
        transcurrido = time.monotonic() - inicio
        await aura.cerrar()
        return diagnosticos, transcurrido

    diagnosticos, transcurrido = correr(principal())
    assert transcurrido < aura_mod.PLAZO_DIAGNOSTICO_S < 4.0
    por_clave = {d.clave: d for d in diagnosticos}
    assert list(por_clave) == ["camara", "telemetria", "hyprland", "nexus"]
    assert por_clave["hyprland"].estado == EstadoModulo.DEGRADADO
    assert por_clave["camara"].estado == EstadoModulo.OFF
    if aura_mod.psutil is not None:
        assert por_clave["telemetria"].estado == EstadoModulo.OK
    # status y version en paralelo, con el plazo corto del diagnóstico.
    assert sorted(args for args, _ in llamadas) == [("-j", "status"), ("-j", "version")]
    assert {t for _, t in llamadas} == {aura_mod.TIMEOUT_HYPRCTL_DIAG_S}


def test_iniciar_acota_cada_diagnostico(settings_prueba: Settings, correr, sin_hyprland: None,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aura_mod, "PLAZO_DIAGNOSTICO_S", 0.2)
    aura = AuraVisionPC(settings_prueba)

    async def colgado() -> DiagnosticoModulo:
        await asyncio.sleep(30)
        raise AssertionError("no debería llegar aquí")

    aura._diagnostico_hyprland = colgado  # type: ignore[method-assign]

    async def principal() -> tuple[list[DiagnosticoModulo], float]:
        inicio = time.monotonic()
        diagnosticos = await aura.iniciar()
        transcurrido = time.monotonic() - inicio
        await aura.cerrar()
        return diagnosticos, transcurrido

    diagnosticos, transcurrido = correr(principal())
    assert transcurrido < 2.0
    hyprland = next(d for d in diagnosticos if d.clave == "hyprland")
    assert hyprland.estado == EstadoModulo.DEGRADADO and hyprland.detalle == "sin respuesta a tiempo"
    assert hyprland.nombre == "Hyprland"


def test_ancho_png_cabecera() -> None:
    cabecera = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1920, 1080)
    assert aura_mod._ancho_png_cabecera(cabecera + b"\x08\x06\x00\x00\x00") == 1920
    assert aura_mod._ancho_png_cabecera(b"no es png") is None


def test_correr_cancelado_mata_y_espera_al_subproceso(correr) -> None:
    """Cancelar ``_correr`` (apagado, §6.6) mata el subproceso y espera su salida, para que asyncio
    cierre sus tuberías antes de cerrar el loop (en Windows, un ``nvidia-smi`` a medio leer dejaba
    el transporte abierto y su ``__del__`` ensuciaba stderr al salir)."""
    vistos: list[asyncio.subprocess.Process] = []

    class Registro(set):
        def add(self, proc: Any) -> None:  # type: ignore[override]
            vistos.append(proc)
            super().add(proc)

    async def principal() -> tuple[int | None, float]:
        tarea = asyncio.create_task(aura_mod._correr(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=30.0, procesos=Registro()))
        limite = time.monotonic() + 10.0
        while not vistos and time.monotonic() < limite:
            await asyncio.sleep(0.01)
        assert vistos, "el subproceso no llegó a lanzarse"
        await asyncio.sleep(0.05)
        inicio = time.monotonic()
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        return vistos[0].returncode, time.monotonic() - inicio

    codigo, duracion = correr(principal())
    assert codigo is not None                     # el proceso terminó antes de devolver el control
    assert duracion <= aura_mod.ESPERA_SALIDA_CANCELADO_S + 1.0
