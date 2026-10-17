"""Tests de core/permisos.py (grupo vision-pc, §10.10 y tabla de §9.1).

Sin red ni hardware. Las pruebas asíncronas usan ``asyncio.run`` (fixture ``correr`` de
tests/conftest.py). ``pids_propios`` se inyecta donde hace falta para que el resultado no
dependa de los procesos de la máquina.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from core.bus import BusEventos
from core.config import Settings
from core.contracts import AccionPC, DecisionPermiso
from core.permisos import (
    CLASE_SEGURA_RE,
    DIRECCION_RE,
    RESPUESTAS_SI,
    CompuertaPermisos,
    SolicitudPermiso,
    calcular_pids_propios,
)

PID_AJENO = 999_999
PIDS_PROPIOS = frozenset({os.getpid(), 4242})


@pytest.fixture
def ajustes(settings_prueba: Settings) -> Settings:
    return settings_prueba.con(apps_permitidas_items=(("terminal", "kitty"), ("navegador", "firefox")))


@pytest.fixture
def compuerta(ajustes: Settings) -> CompuertaPermisos:
    return CompuertaPermisos(ajustes, pids_propios=PIDS_PROPIOS)


def _cerrar(pid: object = PID_AJENO, direccion: object = "0x55aa", origen: str = "usuario") -> AccionPC:
    return AccionPC("cerrar_ventana", {"direccion": direccion, "pid": pid, "clase": "firefox", "titulo": "Docs"},
                    "cerrar firefox «Docs»", origen=origen)


# ---------------------------------------------------------------------------
# Constantes y utilidades
# ---------------------------------------------------------------------------


def test_constantes_publicas() -> None:
    assert CLASE_SEGURA_RE.pattern == r"^[A-Za-z0-9_.\-]{1,64}$"
    assert DIRECCION_RE.pattern == r"^0x[0-9a-fA-F]{1,16}$"
    assert RESPUESTAS_SI == frozenset({"s", "si", "sí", "y", "yes", "procede", "dale"})
    assert DIRECCION_RE.fullmatch("0x55aa") and not DIRECCION_RE.fullmatch("0xZZ")
    assert CLASE_SEGURA_RE.fullmatch("kitty") and not CLASE_SEGURA_RE.fullmatch("a; rm -rf /")


def test_calcular_pids_propios_incluye_el_proceso_actual() -> None:
    pids = calcular_pids_propios()
    assert isinstance(pids, frozenset)
    assert os.getpid() in pids


def test_pids_propios_por_defecto(ajustes: Settings) -> None:
    assert os.getpid() in CompuertaPermisos(ajustes).pids_propios
    assert CompuertaPermisos(ajustes, pids_propios=frozenset({7})).pids_propios == frozenset({7})


# ---------------------------------------------------------------------------
# Tabla de §9.1 (evaluar es pura)
# ---------------------------------------------------------------------------


def test_lanzar_app_conocida_y_desconocida(compuerta: CompuertaPermisos) -> None:
    ok = compuerta.evaluar(AccionPC("lanzar_app", {"alias": "terminal"}, "abrir terminal"))
    assert ok.veredicto == "permitida" and ok.aprobada is True
    malo = compuerta.evaluar(AccionPC("lanzar_app", {"alias": "bitcoinminer"}, "abrir bitcoinminer"))
    assert malo.veredicto == "denegada" and malo.aprobada is False
    assert "RAMS_APPS" in malo.motivo
    sin_alias = compuerta.evaluar(AccionPC("lanzar_app", {}))
    assert sin_alias.veredicto == "denegada"


def test_cerrar_ventana_ajena_pide_confirmacion(compuerta: CompuertaPermisos) -> None:
    d = compuerta.evaluar(_cerrar())
    assert d.veredicto == "confirmar"
    assert d.destructiva is True
    assert d.aprobada is False   # evaluar nunca aprueba lo que exige confirmación


def test_cerrar_ventana_sin_direccion_se_deniega(compuerta: CompuertaPermisos) -> None:
    d = compuerta.evaluar(AccionPC("cerrar_ventana", {}, "cerrar la ventana"))
    assert d.veredicto == "denegada"
    assert "no sé qué ventana cerrar" in d.motivo
    for direccion in ("0xZZ", "55aa", "0x", "0x55aa\n", 1234, None, "0x" + "f" * 17):
        assert compuerta.evaluar(_cerrar(direccion=direccion)).veredicto == "denegada", direccion


def test_cerrar_ventana_de_rams_se_deniega(compuerta: CompuertaPermisos, ajustes: Settings) -> None:
    d = compuerta.evaluar(_cerrar(pid=os.getpid()))
    assert d.veredicto == "denegada"
    assert "terminal de R.A.M.S." in d.motivo
    # Un ancestro (p. ej. la terminal) también es propio.
    assert compuerta.evaluar(_cerrar(pid=4242)).veredicto == "denegada"
    # Con los pids calculados de verdad, el propio PID sigue protegido.
    assert CompuertaPermisos(ajustes).evaluar(_cerrar(pid=os.getpid())).veredicto == "denegada"


def test_cerrar_ventana_pid_none_o_invalido(compuerta: CompuertaPermisos) -> None:
    assert compuerta.evaluar(_cerrar(pid=None)).veredicto == "confirmar"
    assert compuerta.evaluar(_cerrar(pid="123")).veredicto == "denegada"
    assert compuerta.evaluar(_cerrar(pid=True)).veredicto == "denegada"


def test_tipo_comando_no_soportado(compuerta: CompuertaPermisos) -> None:
    d = compuerta.evaluar(AccionPC("comando", {"cmd": "rm -rf /"}, "ejecutar algo"))
    assert d.veredicto == "denegada"
    assert "no soportado" in d.motivo


@pytest.mark.parametrize("origen", ["llm", "telegram", "hud", ""])
def test_origenes_prohibidos_o_desconocidos(compuerta: CompuertaPermisos, origen: str) -> None:
    for accion in (AccionPC("lanzar_app", {"alias": "terminal"}, origen=origen),
                   AccionPC("vistazo", {"motivo": "usuario"}, origen=origen),
                   _cerrar(origen=origen)):
        d = compuerta.evaluar(accion)
        assert d.veredicto == "denegada" and d.aprobada is False, (origen, accion.tipo)


def test_vistazo_por_origen_y_motivo(compuerta: CompuertaPermisos) -> None:
    iniciativa = compuerta.evaluar(AccionPC("vistazo", {"motivo": "curiosidad"}, origen="iniciativa"))
    assert iniciativa.veredicto == "permitida" and iniciativa.aprobada is True
    usuario = compuerta.evaluar(AccionPC("vistazo", {"motivo": "usuario"}, origen="usuario"))
    assert usuario.veredicto == "permitida"
    presencia = compuerta.evaluar(AccionPC("vistazo", {"motivo": "presencia"}, origen="iniciativa"))
    assert presencia.veredicto == "permitida"
    nexus = compuerta.evaluar(AccionPC("vistazo", {"motivo": "curiosidad"}, origen="nexus"))
    assert nexus.veredicto == "denegada"
    motivo_raro = compuerta.evaluar(AccionPC("vistazo", {"motivo": "espiar"}, origen="usuario"))
    assert motivo_raro.veredicto == "denegada"


def test_restricciones_de_iniciativa_y_nexus(compuerta: CompuertaPermisos) -> None:
    assert compuerta.evaluar(AccionPC("lanzar_app", {"alias": "terminal"}, origen="iniciativa")).veredicto == "denegada"
    assert compuerta.evaluar(AccionPC("enfocar_nexus", {}, origen="iniciativa")).veredicto == "denegada"
    assert compuerta.evaluar(AccionPC("enfocar_nexus", {}, origen="nexus")).veredicto == "permitida"
    assert compuerta.evaluar(AccionPC("lanzar_app", {"alias": "terminal"}, origen="nexus")).veredicto == "denegada"
    assert compuerta.evaluar(_cerrar(origen="nexus")).veredicto == "denegada"


@pytest.mark.parametrize("n, veredicto", [(1, "permitida"), (10, "permitida"), (3, "permitida"),
                                          (11, "denegada"), (0, "denegada"), (-1, "denegada"),
                                          (True, "denegada"), (2.5, "denegada"), (None, "denegada")])
def test_ir_workspace(compuerta: CompuertaPermisos, n: object, veredicto: str) -> None:
    assert compuerta.evaluar(AccionPC("ir_workspace", {"n": n})).veredicto == veredicto


def test_volumen_media_y_captura(compuerta: CompuertaPermisos) -> None:
    assert compuerta.evaluar(AccionPC("volumen", {"nivel": 40})).veredicto == "permitida"
    assert compuerta.evaluar(AccionPC("volumen", {"nivel": 0})).veredicto == "permitida"
    assert compuerta.evaluar(AccionPC("volumen", {"nivel": 150})).veredicto == "denegada"
    for orden in ("play_pause", "siguiente", "anterior"):
        assert compuerta.evaluar(AccionPC("media", {"orden": orden})).veredicto == "permitida"
    assert compuerta.evaluar(AccionPC("media", {"orden": "rm"})).veredicto == "denegada"
    assert compuerta.evaluar(AccionPC("captura_pantalla", {}, origen="usuario")).veredicto == "permitida"
    assert compuerta.evaluar(AccionPC("captura_pantalla", {}, origen="iniciativa")).veredicto == "denegada"


def test_evaluar_nunca_lanza(compuerta: CompuertaPermisos) -> None:
    d = compuerta.evaluar(None)  # type: ignore[arg-type]
    assert isinstance(d, DecisionPermiso) and d.veredicto == "denegada"


# ---------------------------------------------------------------------------
# autorizar / resolver
# ---------------------------------------------------------------------------


def test_autorizar_y_resolver_si(ajustes: Settings, bus: BusEventos, correr) -> None:
    preguntas: list[str] = []
    eventos: list[tuple[str, dict]] = []
    bus.suscribir("permiso.*", lambda ev: eventos.append((ev.nombre, dict(ev.datos))))
    compuerta = CompuertaPermisos(ajustes, bus, preguntar=preguntas.append, pids_propios=PIDS_PROPIOS)

    async def principal() -> tuple[DecisionPermiso, SolicitudPermiso | None, bool]:
        tarea = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        for _ in range(200):
            if compuerta.hay_pendiente():
                break
            await asyncio.sleep(0.005)
        pendiente = compuerta.pendiente()
        consumida = compuerta.resolver("sí")
        return await tarea, pendiente, consumida

    decision, pendiente, consumida = correr(principal())
    assert consumida is True
    assert decision.aprobada is True
    assert decision.veredicto == "confirmar" and decision.destructiva is True
    assert decision.motivo == "confirmada por el usuario"
    assert isinstance(pendiente, SolicitudPermiso) and len(pendiente.id) == 8
    assert pendiente.accion.tipo == "cerrar_ventana"
    assert preguntas == ["⚠ Confirmación requerida: cerrar firefox «Docs». ¿Procedo? [s/N]"]
    assert [n for n, _ in eventos] == ["permiso.solicitado", "permiso.resuelto"]
    solicitado, resuelto = eventos[0][1], eventos[1][1]
    assert solicitado == {"id": pendiente.id, "descripcion": "cerrar firefox «Docs»",
                          "destructiva": True, "tipo": "cerrar_ventana"}
    assert resuelto == {"id": pendiente.id, "aprobado": True, "motivo": "confirmada por el usuario"}
    assert compuerta.hay_pendiente() is False and compuerta.pendiente() is None


@pytest.mark.parametrize("respuesta, esperado", [
    ("s", True), ("SI", True), ("Sí", True), (" sí ", True), ("y", True), ("YES", True),
    ("procede", True), ("Dale", True),
    ("no", False), ("n", False), ("", False), ("sí, pero no", False), ("ok", False), ("si.", False),
])
def test_respuestas_si_y_no(ajustes: Settings, correr, respuesta: str, esperado: bool) -> None:
    compuerta = CompuertaPermisos(ajustes, preguntar=lambda _t: None, pids_propios=PIDS_PROPIOS)

    async def principal() -> DecisionPermiso:
        tarea = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        assert compuerta.resolver(respuesta) is True
        return await tarea

    decision = correr(principal())
    assert decision.aprobada is esperado
    assert decision.motivo == ("confirmada por el usuario" if esperado else "rechazada por el usuario")


def test_timeout_equivale_a_no(ajustes: Settings, bus: BusEventos, correr) -> None:
    resueltos: list[dict] = []
    bus.suscribir("permiso.resuelto", lambda ev: resueltos.append(dict(ev.datos)))
    compuerta = CompuertaPermisos(ajustes, bus, preguntar=lambda _t: None, timeout_s=0.05,
                                  pids_propios=PIDS_PROPIOS)
    decision = correr(compuerta.autorizar(_cerrar()))
    assert decision.aprobada is False
    assert decision.veredicto == "confirmar"
    assert decision.motivo.startswith("sin respuesta en")
    assert resueltos and resueltos[0]["aprobado"] is False
    assert compuerta.hay_pendiente() is False
    assert compuerta.resolver("sí") is False   # ya no hay nada pendiente


def test_motivo_timeout_legible(ajustes: Settings, correr) -> None:
    from core.permisos import _segundos

    assert CompuertaPermisos(ajustes, pids_propios=PIDS_PROPIOS).timeout_s == 60.0
    assert f"sin respuesta en {_segundos(60.0)} s" == "sin respuesta en 60 s"
    compuerta = CompuertaPermisos(ajustes, preguntar=lambda _t: None, timeout_s=0.05,
                                  pids_propios=PIDS_PROPIOS)
    assert correr(compuerta.autorizar(_cerrar())).motivo == "sin respuesta en 0,05 s"


def test_segunda_solicitud_con_otra_pendiente_se_deniega(ajustes: Settings, correr) -> None:
    compuerta = CompuertaPermisos(ajustes, preguntar=lambda _t: None, pids_propios=PIDS_PROPIOS)

    async def principal() -> tuple[DecisionPermiso, DecisionPermiso]:
        primera = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        segunda = await compuerta.autorizar(_cerrar(direccion="0x77bb"))
        assert compuerta.hay_pendiente() is True   # la primera sigue esperando
        compuerta.resolver("no")
        return await primera, segunda

    primera, segunda = correr(principal())
    assert segunda.veredicto == "denegada" and segunda.aprobada is False
    assert segunda.motivo == "otra confirmación en curso"
    assert primera.aprobada is False and primera.motivo == "rechazada por el usuario"


def test_resolver_sin_pendiente_no_hace_nada(compuerta: CompuertaPermisos) -> None:
    assert compuerta.hay_pendiente() is False
    assert compuerta.pendiente() is None
    assert compuerta.resolver("sí") is False
    compuerta.cancelar_pendiente()   # no lanza
    assert compuerta.hay_pendiente() is False


def test_cancelar_pendiente_resuelve_como_no(ajustes: Settings, correr) -> None:
    compuerta = CompuertaPermisos(ajustes, preguntar=lambda _t: None, pids_propios=PIDS_PROPIOS)

    async def principal() -> DecisionPermiso:
        tarea = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        compuerta.cancelar_pendiente()
        return await tarea

    decision = correr(principal())
    assert decision.aprobada is False
    assert compuerta.hay_pendiente() is False


def test_resolver_desde_otro_hilo(ajustes: Settings, correr) -> None:
    compuerta = CompuertaPermisos(ajustes, preguntar=lambda _t: None, pids_propios=PIDS_PROPIOS)

    async def principal() -> DecisionPermiso:
        tarea = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        resultado: list[bool] = []
        hilo = threading.Thread(target=lambda: resultado.append(compuerta.resolver("dale")))
        hilo.start()
        hilo.join(2)
        assert resultado == [True]
        assert compuerta.hay_pendiente() is False   # la siguiente línea ya no se consume
        return await asyncio.wait_for(tarea, 2)

    assert correr(principal()).aprobada is True


def test_permitida_y_denegada_no_preguntan_ni_publican(ajustes: Settings, bus: BusEventos, correr) -> None:
    preguntas: list[str] = []
    eventos: list[str] = []
    bus.suscribir("permiso.*", lambda ev: eventos.append(ev.nombre))
    compuerta = CompuertaPermisos(ajustes, bus, preguntar=preguntas.append, pids_propios=PIDS_PROPIOS)
    ok = correr(compuerta.autorizar(AccionPC("ir_workspace", {"n": 3}, "cambiar al espacio 3")))
    assert ok.aprobada is True and ok.veredicto == "permitida"
    no = correr(compuerta.autorizar(AccionPC("lanzar_app", {"alias": "terminal"}, origen="llm")))
    assert no.aprobada is False and no.veredicto == "denegada"
    assert preguntas == [] and eventos == []


def test_sin_bus_ni_preguntar_usa_print(ajustes: Settings, correr, capsys: pytest.CaptureFixture[str]) -> None:
    compuerta = CompuertaPermisos(ajustes, timeout_s=0.02, pids_propios=PIDS_PROPIOS)
    decision = correr(compuerta.autorizar(_cerrar()))
    assert decision.aprobada is False
    assert "⚠ Confirmación requerida: cerrar firefox «Docs». ¿Procedo? [s/N]" in capsys.readouterr().out


def test_set_preguntar_y_preguntar_que_falla(ajustes: Settings, correr) -> None:
    compuerta = CompuertaPermisos(ajustes, timeout_s=0.02, pids_propios=PIDS_PROPIOS)

    def roto(_texto: str) -> None:
        raise RuntimeError("consola cerrada")

    compuerta.set_preguntar(roto)
    decision = correr(compuerta.autorizar(_cerrar()))   # no lanza aunque preguntar falle
    assert decision.aprobada is False
    recibidas: list[str] = []
    compuerta.set_preguntar(recibidas.append)
    correr(compuerta.autorizar(_cerrar()))
    assert len(recibidas) == 1


def test_cancelacion_de_la_tarea_limpia_la_pendiente(ajustes: Settings, bus: BusEventos, correr) -> None:
    eventos: list[tuple[str, dict]] = []
    bus.suscribir("permiso.*", lambda ev: eventos.append((ev.nombre, dict(ev.datos))))
    compuerta = CompuertaPermisos(ajustes, bus, preguntar=lambda _t: None, pids_propios=PIDS_PROPIOS)

    async def principal() -> bool:
        tarea = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
        return compuerta.hay_pendiente()

    assert correr(principal()) is False
    # Cada permiso.solicitado tiene su permiso.resuelto (el HUD cierra el banner), también
    # cuando la tarea que esperaba la respuesta se cancela.
    assert [n for n, _ in eventos] == ["permiso.solicitado", "permiso.resuelto"]
    solicitado, resuelto = eventos[0][1], eventos[1][1]
    assert resuelto == {"id": solicitado["id"], "aprobado": False, "motivo": "confirmación cancelada"}
