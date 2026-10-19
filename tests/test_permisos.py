"""Tests de core/permisos.py (grupo vision-pc, §10.10 y tabla de §9.1).

Sin red ni hardware. Las pruebas asíncronas usan ``asyncio.run`` (fixture ``correr`` de
tests/conftest.py). ``pids_propios`` se inyecta donde hace falta para que el resultado no
dependa de los procesos de la máquina.
"""

from __future__ import annotations

import asyncio
import inspect
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


# ---------------------------------------------------------------------------
# v0.2 (docs/COMPANION.md §C2.11): filas nuevas, vista previa y resolver_por_id
# ---------------------------------------------------------------------------

HWND_AJENO = 0x1A2B
PID_EXTRA = 31337


def _v02(ajustes: Settings, *, maestro: bool = False, activas: bool = False, bus: BusEventos | None = None,
         preguntar=None, timeout_s: float = 60.0, pids_extra=None) -> CompuertaPermisos:
    ajustes = ajustes.con(travesuras_habilitadas=maestro)
    return CompuertaPermisos(ajustes, bus, preguntar=preguntar or (lambda _t: None), timeout_s=timeout_s,
                             pids_propios=PIDS_PROPIOS,
                             pids_extra=pids_extra if pids_extra is not None else (lambda: frozenset({PID_EXTRA})),
                             travesuras_activas=lambda: activas)


def _escribir(texto: str = "hola", **objetivo: object) -> AccionPC:
    args: dict[str, object] = {"texto": texto, "hwnd": HWND_AJENO, "pid": PID_AJENO, "exe": "chrome.exe",
                               "direccion": None, "clase": "Chrome_WidgetWin_1"}
    args.update(objetivo)
    return AccionPC("escribir_texto", args, "escribir en la ventana anterior")


def _travesura(origen: str = "iniciativa", **cambios: object) -> AccionPC:
    args: dict[str, object] = {"patron": "empujon", "amplitud_px": 30, "segundos": 0.5}
    args.update(cambios)
    return AccionPC("travesura_cursor", args, "travesura de cursor", origen=origen)


def test_constructor_v02_por_defecto(ajustes: Settings) -> None:
    compuerta = CompuertaPermisos(ajustes, pids_propios=PIDS_PROPIOS)
    assert compuerta.pids_extra() == frozenset()
    assert compuerta.travesuras_activas() is False

    def roto() -> frozenset[int]:
        raise RuntimeError("callback roto")

    rota = CompuertaPermisos(ajustes, pids_propios=PIDS_PROPIOS, pids_extra=roto,
                             travesuras_activas=lambda: 1 / 0)  # type: ignore[arg-type,return-value]
    assert rota.pids_extra() == frozenset() and rota.travesuras_activas() is False


def test_iniciativa_solo_vistazo_y_travesura(ajustes: Settings) -> None:
    compuerta = _v02(ajustes, maestro=True, activas=True)
    vistazo = AccionPC("vistazo", {"motivo": "curiosidad"}, "vistazo", origen="iniciativa")
    assert compuerta.evaluar(vistazo).veredicto == "permitida"
    assert compuerta.evaluar(_travesura("iniciativa")).veredicto == "permitida"
    for tipo, args in (("abrir_destino", {"alias": "whatsapp"}), ("buscar_en", {"alias": "youtube", "consulta": "x"}),
                       ("prerrellenar", {"alias": "whatsapp", "texto": "hola"}), ("lanzar_app", {"alias": "terminal"})):
        decision = compuerta.evaluar(AccionPC(tipo, args, tipo, origen="iniciativa"))
        assert decision.veredicto == "denegada", tipo
    assert compuerta.evaluar(_escribir()).veredicto == "confirmar"
    assert compuerta.evaluar(AccionPC("escribir_texto", dict(_escribir().args), "x",
                                      origen="iniciativa")).veredicto == "denegada"
    # Nexus, Telegram y el LLM siguen sin poder nada de esto.
    for origen in ("nexus", "telegram", "llm"):
        assert compuerta.evaluar(AccionPC("abrir_destino", {"alias": "whatsapp"}, "abrir",
                                          origen=origen)).veredicto == "denegada"
        assert compuerta.evaluar(_travesura(origen)).veredicto == "denegada"


def test_abrir_destino_solo_claves_del_catalogo(ajustes: Settings) -> None:
    compuerta = _v02(ajustes)
    assert compuerta.evaluar(AccionPC("abrir_destino", {"alias": "whatsapp"}, "abrir")).aprobada is True
    assert compuerta.evaluar(AccionPC("abrir_destino", {"alias": "navegador"}, "abrir")).aprobada is True
    for alias in ("WhatsApp", "wasap", "malware", "", None, 3, "a" * 25, "whatsapp; rm -rf /"):
        decision = compuerta.evaluar(AccionPC("abrir_destino", {"alias": alias}, "abrir"))
        assert decision.veredicto == "denegada", alias


def test_buscar_en_destinos_con_busqueda(ajustes: Settings) -> None:
    compuerta = _v02(ajustes)

    def buscar(alias: str, consulta: object) -> str:
        return compuerta.evaluar(AccionPC("buscar_en", {"alias": alias, "consulta": consulta}, "buscar")).veredicto

    assert buscar("youtube", "gatos graciosos") == "permitida"
    assert buscar("navegador", "clima en Bogotá") == "permitida"
    assert buscar("whatsapp", "mamá") == "denegada"                   # sin búsqueda en el catálogo
    assert buscar("youtube", "") == "denegada" and buscar("youtube", None) == "denegada"
    assert buscar("youtube", "x" * 121) == "denegada"
    assert buscar("youtube", "AIza" + "B" * 35) == "denegada"         # parece una clave


def test_prerrellenar_confirma_y_rechaza_secretos(ajustes: Settings) -> None:
    compuerta = _v02(ajustes)

    def prerrellenar(alias: str, texto: object) -> DecisionPermiso:
        return compuerta.evaluar(AccionPC("prerrellenar", {"alias": alias, "texto": texto}, "borrador"))

    decision = prerrellenar("whatsapp", "Llego tarde,\nguárdame comida")
    assert decision.veredicto == "confirmar" and decision.aprobada is False and decision.destructiva is False
    assert prerrellenar("x", "Hola mundo").veredicto == "confirmar"
    assert prerrellenar("instagram", "hola").veredicto == "denegada"   # sin prerrelleno: escribir_texto
    for secreto in ("482915", "4111 1111 1111 1111", "password: hunter2", "AIza" + "C" * 35):
        assert prerrellenar("whatsapp", secreto).veredicto == "denegada", secreto
    assert prerrellenar("whatsapp", "x" * 501).veredicto == "denegada"
    assert prerrellenar("whatsapp", " \n\t ").veredicto == "denegada"
    assert prerrellenar("whatsapp", ("hola mamá " * 60)[:500]).veredicto == "confirmar"


def test_escribir_texto_objetivo_y_texto(ajustes: Settings) -> None:
    compuerta = _v02(ajustes)
    assert compuerta.evaluar(_escribir()).veredicto == "confirmar"
    assert compuerta.evaluar(_escribir(hwnd=None, direccion="0x55aa")).veredicto == "confirmar"
    denegadas = [
        _escribir(pid=os.getpid()),                 # R.A.M.S. (pids_propios)
        _escribir(pid=4242),                        # ancestro de R.A.M.S.
        _escribir(pid=PID_EXTRA),                   # Amadeus Desktop (pids_extra)
        _escribir(pid=None), _escribir(pid=True), _escribir(pid="999"),
        _escribir(hwnd=None, direccion=None),       # sin ventana
        _escribir(hwnd=None, direccion="0xZZ"), _escribir(hwnd=None, direccion="0xABC"),
        _escribir(hwnd=True),
        _escribir(texto="482915"), _escribir(texto="clave = sup3r"), _escribir(texto=""), _escribir(texto="x" * 501),
    ]
    for accion in denegadas:
        assert compuerta.evaluar(accion).veredicto == "denegada", accion.args


def test_travesura_exige_maestro_y_estado_de_main(ajustes: Settings) -> None:
    assert _v02(ajustes, maestro=False, activas=True).evaluar(_travesura()).veredicto == "denegada"
    assert _v02(ajustes, maestro=True, activas=False).evaluar(_travesura()).veredicto == "denegada"
    compuerta = _v02(ajustes, maestro=True, activas=True)
    assert compuerta.evaluar(_travesura()).veredicto == "permitida"
    assert compuerta.evaluar(_travesura("usuario")).veredicto == "permitida"
    maximo = ajustes.travesura_amplitud_max_px
    for cambios in ({"patron": "clic"}, {"amplitud_px": maximo + 1}, {"amplitud_px": 0}, {"amplitud_px": 2.5},
                    {"amplitud_px": True}, {"segundos": 0.61}, {"segundos": 0}, {"segundos": "0.5"},
                    {"segundos": float("nan")}):
        assert compuerta.evaluar(_travesura(**cambios)).veredicto == "denegada", cambios
    for patron in ("empujon", "circulo", "temblor", "hacia_avatar"):
        assert compuerta.evaluar(_travesura(patron=patron, amplitud_px=maximo, segundos=0.6)).aprobada is True


def test_vista_previa_y_recuento_iguales_a_lo_que_escribe_control_pc(compuerta: CompuertaPermisos) -> None:
    """La vista previa y el «N caracteres» salen del mismo saneado que teclea ControlPC:
    los invisibles (ZWSP, marcas bidi, etiquetas Unicode) no viajan ni inflan el recuento."""
    from core.permisos import sanear_texto_accion
    from lobes.control_pc import _sanear

    oculto = "".join(chr(0xE0000 + ord(c)) for c in "clave")       # "ASCII smuggling"
    texto = "ho​la‮  mundo\t" + oculto + "\n"
    assert sanear_texto_accion(texto) == _sanear(texto, 500) == "hola mundo"
    assert sanear_texto_accion("​⁠") == "" and sanear_texto_accion(None) == ""
    decision = compuerta.evaluar(AccionPC("prerrellenar", {"alias": "whatsapp", "texto": "​" * 600 + "hola"},
                                          "borrador"))
    assert decision.veredicto == "confirmar"                        # 4 caracteres visibles, no 604


def test_vista_previa_va_aparte_de_la_descripcion(ajustes: Settings, bus: BusEventos, correr,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    eventos: list[tuple[str, dict]] = []
    preguntas: list[str] = []
    bus.suscribir("permiso.*", lambda ev: eventos.append((ev.nombre, dict(ev.datos))))
    compuerta = _v02(ajustes, bus=bus, preguntar=preguntas.append)
    texto = "Llego tarde,\nguárdame arepa"

    async def principal() -> tuple[DecisionPermiso, bool]:
        tarea = asyncio.ensure_future(compuerta.autorizar(
            AccionPC("prerrellenar", {"alias": "whatsapp", "texto": texto}, f"borrador: {texto}")))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        resuelta = compuerta.resolver_por_id(compuerta.pendiente().id, True)
        return await tarea, resuelta

    with caplog.at_level("DEBUG"):
        decision, resuelta = correr(principal())
    assert resuelta is True and decision.aprobada is True and decision.destructiva is False
    assert decision.motivo == "confirmada con el botón del chat"
    solicitado = eventos[0][1]
    saneado = "Llego tarde, guárdame arepa"
    assert solicitado["vista_previa"] == saneado and solicitado["tipo"] == "prerrellenar"
    assert solicitado["descripcion"] == f"Abrir WhatsApp con un borrador de {len(saneado)} caracteres (no se enviará)"
    assert "arepa" not in solicitado["descripcion"] and solicitado["destructiva"] is False
    assert eventos[1][1]["aprobado"] is True
    # La consola local SÍ la muestra (pantalla), pero ningún log la registra.
    assert "arepa" in preguntas[0] and preguntas[0].startswith("⚠ Confirmación requerida: Abrir WhatsApp")
    assert all("arepa" not in r.getMessage() for r in caplog.records)
    # escribir_texto: "Escribir N caracteres en chrome.exe (sin enviar)".
    eventos.clear()

    async def escribir() -> DecisionPermiso:
        tarea = asyncio.ensure_future(compuerta.autorizar(_escribir("hola")))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        assert compuerta.resolver_por_id(compuerta.pendiente().id, False) is True
        return await tarea

    rechazo = correr(escribir())
    assert rechazo.aprobada is False and rechazo.motivo == "rechazada con el botón del chat"
    assert eventos[0][1]["descripcion"] == "Escribir 4 caracteres en chrome.exe (sin enviar)"
    assert eventos[0][1]["vista_previa"] == "hola"


def test_cerrar_ventana_no_lleva_vista_previa(ajustes: Settings, bus: BusEventos, correr) -> None:
    eventos: list[dict] = []
    bus.suscribir("permiso.solicitado", lambda ev: eventos.append(dict(ev.datos)))
    compuerta = _v02(ajustes, bus=bus, timeout_s=0.02)
    correr(compuerta.autorizar(_cerrar()))
    assert "vista_previa" not in eventos[0] and eventos[0]["destructiva"] is True


def test_resolver_por_id(ajustes: Settings, correr) -> None:
    compuerta = _v02(ajustes)
    assert compuerta.resolver_por_id("0123abcd", True) is False          # nada pendiente
    assert not inspect.iscoroutinefunction(compuerta.resolver_por_id)

    async def principal() -> tuple[list[bool], DecisionPermiso]:
        tarea = asyncio.ensure_future(compuerta.autorizar(
            AccionPC("prerrellenar", {"alias": "x", "texto": "hola"}, "borrador")))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        ident = compuerta.pendiente().id
        viejo = "ffffffff" if ident != "ffffffff" else "00000000"
        intentos = [compuerta.resolver_por_id(viejo, True),               # id viejo
                    compuerta.resolver_por_id(ident.upper(), True) if ident.upper() != ident else False,
                    compuerta.resolver_por_id(ident[:7], True),
                    compuerta.resolver_por_id(ident, "sí"),                # type: ignore[arg-type]
                    compuerta.resolver_por_id(ident, 1),                   # type: ignore[arg-type]
                    compuerta.resolver_por_id(None, True)]                 # type: ignore[arg-type]
        intentos.append(compuerta.hay_pendiente())                         # sigue pendiente
        # Un lock asyncio tomado (como _lock_turno de main) no impide resolver: es síncrono.
        candado = asyncio.Lock()
        async with candado:
            intentos.append(compuerta.resolver_por_id(ident, True))
        return intentos, await tarea

    intentos, decision = correr(principal())
    assert intentos[:6] == [False] * 6
    assert intentos[6] is True and intentos[7] is True
    assert decision.aprobada is True


def test_resolver_por_id_no_cierra_ventanas(ajustes: Settings, correr) -> None:
    compuerta = _v02(ajustes)

    async def principal() -> tuple[bool, bool, DecisionPermiso]:
        tarea = asyncio.ensure_future(compuerta.autorizar(_cerrar()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        con_boton = compuerta.resolver_por_id(compuerta.pendiente().id, True)
        sigue = compuerta.hay_pendiente()
        assert compuerta.resolver("no") is True                           # la terminal sí puede
        return con_boton, sigue, await tarea

    con_boton, sigue, decision = correr(principal())
    assert con_boton is False and sigue is True
    assert decision.aprobada is False and decision.motivo == "rechazada por el usuario"


def test_resolver_por_id_desde_otro_hilo(ajustes: Settings, correr) -> None:
    compuerta = _v02(ajustes)

    async def principal() -> tuple[list[bool], DecisionPermiso]:
        tarea = asyncio.ensure_future(compuerta.autorizar(_escribir()))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        ident = compuerta.pendiente().id
        resultados: list[bool] = []
        hilo = threading.Thread(target=lambda: resultados.append(compuerta.resolver_por_id(ident, True)))
        hilo.start()
        await asyncio.to_thread(hilo.join, 2.0)
        return resultados, await asyncio.wait_for(tarea, 2.0)

    resultados, decision = correr(principal())
    assert resultados == [True] and decision.aprobada is True


# ---------------------------------------------------------------------------
# v0.2.1: órdenes desde Telegram, apps del menú Inicio y fotos
# ---------------------------------------------------------------------------


class _RelojFalso:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


NIVELES = {"steam": ("permitida", "Steam"), "juego": ("descubierta", "Juego"),
           "windows_powershell": ("sensible", "Windows PowerShell")}


def _v021(ajustes: Settings, *, telegram_acciones: bool = True, telegram_fotos: bool = False,
          nivel_app=None, reloj=None, preguntar=None, bus: BusEventos | None = None) -> CompuertaPermisos:
    ajustes = ajustes.con(telegram_acciones=telegram_acciones, telegram_fotos=telegram_fotos)
    return CompuertaPermisos(ajustes, bus, preguntar=preguntar or (lambda _t: None), pids_propios=PIDS_PROPIOS,
                             nivel_app=nivel_app if nivel_app is not None else NIVELES.get,
                             reloj=reloj or _RelojFalso())


def _por_telegram(tipo: str, args: dict[str, object], origen: str = "usuario") -> AccionPC:
    return AccionPC(tipo, {**args, "canal": "telegram"}, tipo, origen=origen)


def _app(clave: str, **extra: object) -> AccionPC:
    return AccionPC("lanzar_app", {"alias": clave, "local": True, "nombre": clave, **extra}, f"abrir {clave}")


def _foto(fuente: str = "camara", origen: str = "usuario", **extra: object) -> AccionPC:
    return AccionPC("enviar_foto", {"fuente": fuente, **extra}, "enviar una foto", origen=origen)


def test_constantes_v021() -> None:
    from core.permisos import (ACCIONES_POR_TELEGRAM, FUENTES_FOTO, MAX_FOTOS_HORA, NIVELES_APP,
                               SAFE_CLAVE_APP_RE, TIPOS_ACCION_EXTRA)

    assert ACCIONES_POR_TELEGRAM == frozenset({"abrir_destino", "buscar_en", "lanzar_app", "media", "enviar_foto"})
    assert "enviar_foto" in TIPOS_ACCION_EXTRA      # v0.2.2 añade los suyos (test_v022_constantes)
    assert FUENTES_FOTO == frozenset({"camara", "pantalla"}) and MAX_FOTOS_HORA == 10
    assert NIVELES_APP == ("permitida", "descubierta", "sensible")
    assert SAFE_CLAVE_APP_RE.fullmatch("visual_studio_code") and not SAFE_CLAVE_APP_RE.fullmatch("a" * 41)


def test_telegram_solo_acciones_de_bajo_riesgo(ajustes: Settings) -> None:
    compuerta = _v021(ajustes)
    for tipo, args in (("abrir_destino", {"alias": "youtube"}), ("buscar_en", {"alias": "youtube", "consulta": "lofi"}),
                       ("media", {"orden": "play_pause"}), ("lanzar_app", {"alias": "terminal"})):
        decision = compuerta.evaluar(_por_telegram(tipo, args))
        assert decision.veredicto == "permitida" and decision.aprobada is True, tipo
    for tipo, args in (("escribir_texto", dict(_escribir().args)),
                       ("prerrellenar", {"alias": "whatsapp", "texto": "hola"}),
                       ("cerrar_ventana", dict(_cerrar().args)), ("volumen", {"nivel": 20}),
                       ("ir_workspace", {"n": 2}), ("enfocar_nexus", {}), ("vistazo", {"motivo": "usuario"}),
                       ("captura_pantalla", {}), ("travesura_cursor", dict(_travesura().args))):
        decision = compuerta.evaluar(_por_telegram(tipo, args))
        assert decision.veredicto == "denegada", tipo
        assert "Telegram" in decision.motivo, tipo
    # El origen "telegram" sigue prohibido aunque la acción sea de bajo riesgo.
    assert compuerta.evaluar(_por_telegram("abrir_destino", {"alias": "youtube"}, origen="telegram")).aprobada is False
    # Un canal inventado no cuela nada.
    raro = AccionPC("abrir_destino", {"alias": "youtube", "canal": "hud"}, "abrir")
    assert compuerta.evaluar(raro).veredicto == "denegada"


def test_telegram_sin_acciones_activadas(ajustes: Settings) -> None:
    compuerta = _v021(ajustes, telegram_acciones=False)
    decision = compuerta.evaluar(_por_telegram("abrir_destino", {"alias": "youtube"}))
    assert decision.veredicto == "denegada" and "RAMS_TELEGRAM_ACCIONES" in decision.motivo
    # Lo mismo desde el PC sí se puede.
    assert compuerta.evaluar(AccionPC("abrir_destino", {"alias": "youtube", "canal": "terminal"})).aprobada is True


def test_apps_locales_por_nivel(ajustes: Settings) -> None:
    compuerta = _v021(ajustes)
    assert compuerta.evaluar(_app("steam")).veredicto == "permitida"
    descubierta = compuerta.evaluar(_app("juego"))
    assert descubierta.veredicto == "confirmar" and descubierta.destructiva is False and descubierta.aprobada is False
    assert compuerta.evaluar(_app("windows_powershell")).veredicto == "confirmar"
    desconocida = compuerta.evaluar(_app("no_existe"))
    assert desconocida.veredicto == "denegada" and "menú Inicio" in desconocida.motivo
    for clave in ("", "Steam", "a; rm", "a" * 41, None, 3):
        assert compuerta.evaluar(_app(clave)).veredicto == "denegada", clave   # type: ignore[arg-type]
    for origen in ("iniciativa", "llm", "telegram", "nexus"):
        accion = AccionPC("lanzar_app", {"alias": "steam", "local": True}, "abrir", origen=origen)
        assert compuerta.evaluar(accion).veredicto == "denegada", origen
    # Por Telegram: solo las ya permitidas, nunca una confirmación.
    assert compuerta.evaluar(_app("steam", canal="telegram")).veredicto == "permitida"
    nueva = compuerta.evaluar(_app("juego", canal="telegram"))
    assert nueva.veredicto == "denegada" and "aún no está aprobada" in nueva.motivo
    consola = compuerta.evaluar(_app("windows_powershell", canal="telegram"))
    assert consola.veredicto == "denegada" and "computador" in consola.motivo


def test_apps_locales_sin_indice_o_con_indice_roto(ajustes: Settings) -> None:
    sin_indice = CompuertaPermisos(ajustes, pids_propios=PIDS_PROPIOS)
    assert sin_indice.evaluar(_app("steam")).veredicto == "denegada"

    def roto(_clave: str) -> tuple[str, str]:
        raise RuntimeError("índice roto")

    assert _v021(ajustes, nivel_app=roto).evaluar(_app("steam")).veredicto == "denegada"
    assert _v021(ajustes, nivel_app=lambda _c: ("superpermitida", "X")).evaluar(_app("steam")).veredicto == "denegada"
    compuerta = CompuertaPermisos(ajustes, pids_propios=PIDS_PROPIOS)
    compuerta.set_nivel_app(NIVELES.get)
    assert compuerta.evaluar(_app("steam")).aprobada is True
    # Sin "local", lanzar_app sigue siendo la fila de RAMS_APPS de siempre.
    assert compuerta.evaluar(AccionPC("lanzar_app", {"alias": "steam"})).veredicto == "denegada"


def test_app_descubierta_se_confirma_con_su_nombre(ajustes: Settings, correr) -> None:
    preguntas: list[str] = []
    compuerta = _v021(ajustes, preguntar=preguntas.append)

    async def principal() -> tuple[DecisionPermiso, DecisionPermiso]:
        tarea = asyncio.ensure_future(compuerta.autorizar(_app("juego", nombre="texto del usuario")))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        assert compuerta.resolver_por_id(compuerta.pendiente().id, True) is True
        primera = await tarea
        tarea = asyncio.ensure_future(compuerta.autorizar(_app("windows_powershell")))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        compuerta.resolver("no")
        return primera, await tarea

    primera, segunda = correr(principal())
    assert primera.veredicto == "confirmar" and primera.aprobada is True
    assert segunda.aprobada is False
    assert "Abrir «Juego»" in preguntas[0] and "no te lo vuelvo a preguntar" in preguntas[0]
    assert "texto del usuario" not in preguntas[0]
    assert "te lo preguntaré cada vez" in preguntas[1]


def test_lo_pedido_por_telegram_nunca_espera_confirmacion(ajustes: Settings, correr, monkeypatch) -> None:
    compuerta = _v021(ajustes)
    monkeypatch.setattr(compuerta, "evaluar", lambda _a: DecisionPermiso("confirmar", "x", aprobada=False))
    decision = correr(compuerta.autorizar(_app("juego", canal="telegram")))
    assert decision.veredicto == "denegada" and decision.aprobada is False
    assert compuerta.hay_pendiente() is False


def test_enviar_foto_reglas(ajustes: Settings) -> None:
    compuerta = _v021(ajustes)
    assert compuerta.evaluar(_foto("camara")).aprobada is True
    assert compuerta.evaluar(_foto("pantalla", canal="escritorio")).aprobada is True
    assert compuerta.evaluar(_foto("microfono")).veredicto == "denegada"
    for origen in ("iniciativa", "llm", "telegram", "nexus", ""):
        assert compuerta.evaluar(_foto(origen=origen)).veredicto == "denegada", origen
    # Por Telegram exige RAMS_TELEGRAM_FOTOS=1 (aunque RAMS_TELEGRAM_ACCIONES esté apagado).
    apagadas = compuerta.evaluar(_foto(canal="telegram"))
    assert apagadas.veredicto == "denegada" and "RAMS_TELEGRAM_FOTOS" in apagadas.motivo
    encendidas = _v021(ajustes, telegram_acciones=False, telegram_fotos=True)
    assert encendidas.evaluar(_foto("pantalla", canal="telegram")).aprobada is True


def test_enviar_foto_tope_por_hora(ajustes: Settings, correr) -> None:
    reloj = _RelojFalso()
    compuerta = _v021(ajustes, reloj=reloj)

    async def principal() -> list[bool]:
        return [(await compuerta.autorizar(_foto())).aprobada for _ in range(11)]

    resultados = correr(principal())
    assert resultados == [True] * 10 + [False]
    assert compuerta.fotos_ultima_hora() == 10
    assert "10 fotos" in compuerta.evaluar(_foto()).motivo
    reloj.t += 3600.0
    assert compuerta.fotos_ultima_hora() == 0
    assert correr(compuerta.autorizar(_foto())).aprobada is True
    # evaluar es pura: no gasta el tope.
    for _ in range(20):
        compuerta.evaluar(_foto())
    assert compuerta.fotos_ultima_hora() == 1


# ---------------------------------------------------------------------------
# v0.2.2: Google, correo, Drive, taller y tarjetas aprobables (docs/V022_CORREO_DRIVE_SCRIPTS.md §5.5)
# ---------------------------------------------------------------------------

H1 = "a" * 64
H2 = "b" * 64
TIPOS_NUEVOS = ("conectar_google", "desconectar_google", "enviar_correo", "drive_descargar", "drive_crear_carpeta",
                "drive_subir", "drive_papelera", "drive_compartir", "ejecutar_script")


def _v022(ajustes: Settings, *, reloj: _RelojFalso | None = None, bus: BusEventos | None = None,
          **cambios: object) -> CompuertaPermisos:
    base = {"google_habilitado": True, "offline": False, "taller_habilitado": True}
    base.update(cambios)
    return CompuertaPermisos(ajustes.con(**base), bus, preguntar=lambda _t: None, pids_propios=PIDS_PROPIOS,
                             reloj=reloj or _RelojFalso())


def _tarjeta_accion(tipo: str, ident: str, sha: str = H1, origen: str = "usuario", **extra: object) -> AccionPC:
    args: dict[str, object] = {"canal": "escritorio", "tarjeta": ident, "sha256": sha, "aprobacion": "tarjeta"}
    if tipo == "enviar_correo":
        args["n_destinatarios"] = 1
    elif tipo == "drive_compartir":
        args.update({"rol": "reader", "publico": False, "doble_confirmacion": False})
    elif tipo == "ejecutar_script":
        args["nivel"] = "medio"
    args.update(extra)
    return AccionPC(tipo, args, "acción con tarjeta", origen=origen)


def _local(tipo: str, origen: str = "usuario", **extra: object) -> AccionPC:
    args: dict[str, object] = {"canal": "terminal"}
    if tipo == "drive_descargar":
        args.update({"destino": "descargas", "formato": "pdf"})
    elif tipo == "drive_crear_carpeta":
        args["nombre"] = "Facturas 2026"
    args.update(extra)
    return AccionPC(tipo, args, "acción de Google", origen=origen)


def _registrar(compuerta: CompuertaPermisos, ident: str, tipo: str, huella: str = H1, **kw: object):
    nueva, reemplazada = compuerta.registrar_tarjeta(ident, tipo_accion=tipo, huella=huella,
                                                     ttl_s=float(kw.pop("ttl_s", 1800.0)),
                                                     canal_origen=str(kw.pop("canal_origen", "escritorio")),
                                                     doble=bool(kw.pop("doble", False)))
    return nueva, reemplazada


def test_v022_constantes() -> None:
    from core.contracts import TIPOS_ACCION
    from core.permisos import (ACCIONES_POR_TELEGRAM, CLASE_DE_ACCION, TIPOS_ACCION_EXTRA, TIPOS_ACCION_TARJETA,
                               TIPOS_INICIATIVA, VENTANA_CORREO_CORTA_S, VENTANA_CORREO_DIA_S)

    assert TIPOS_ACCION_EXTRA == frozenset({"enviar_foto", *TIPOS_NUEVOS})
    assert TIPOS_ACCION_TARJETA == frozenset({"enviar_correo", "drive_subir", "drive_papelera", "drive_compartir",
                                              "ejecutar_script"})
    assert dict(CLASE_DE_ACCION) == {"enviar_correo": "correo", "drive_subir": "drive", "drive_papelera": "drive",
                                     "drive_compartir": "drive", "ejecutar_script": "script"}
    assert VENTANA_CORREO_CORTA_S == 600.0 and VENTANA_CORREO_DIA_S == 86400.0
    # No cambian: los 13 tipos, lo que va por Telegram y lo que va por iniciativa.
    assert len(TIPOS_ACCION) == 13 and not set(TIPOS_NUEVOS) & set(TIPOS_ACCION)
    assert ACCIONES_POR_TELEGRAM == frozenset({"abrir_destino", "buscar_en", "lanzar_app", "media", "enviar_foto"})
    assert TIPOS_INICIATIVA == frozenset({"vistazo", "travesura_cursor"})


def test_v022_tipos_nuevos_solo_del_usuario_y_nunca_por_telegram(ajustes: Settings) -> None:
    compuerta = _v022(ajustes)
    for tipo in TIPOS_NUEVOS:
        if tipo in ("enviar_correo", "drive_subir", "drive_papelera", "drive_compartir", "ejecutar_script"):
            _registrar(compuerta, f"0000000{TIPOS_NUEVOS.index(tipo)}", tipo)
            base = _tarjeta_accion(tipo, f"0000000{TIPOS_NUEVOS.index(tipo)}")
        else:
            base = _local(tipo)
        for origen in ("llm", "telegram", "iniciativa", "nexus"):
            accion = AccionPC(tipo, dict(base.args), base.descripcion, origen=origen)
            assert compuerta.evaluar(accion).veredicto == "denegada", (tipo, origen)
        remota = AccionPC(tipo, {**base.args, "canal": "telegram"}, base.descripcion)
        assert compuerta.evaluar(remota).veredicto == "denegada", tipo


def test_v022_google_y_drive_locales(ajustes: Settings) -> None:
    compuerta = _v022(ajustes)
    assert compuerta.evaluar(_local("conectar_google")).veredicto == "permitida"
    assert compuerta.evaluar(_local("conectar_google", canal="escritorio")).veredicto == "permitida"
    assert compuerta.evaluar(_local("drive_descargar")).veredicto == "permitida"
    assert compuerta.evaluar(_local("drive_descargar", formato=None)).veredicto == "permitida"
    assert compuerta.evaluar(_local("drive_descargar", destino="C:/Windows")).veredicto == "denegada"
    assert compuerta.evaluar(_local("drive_descargar", formato="exe")).veredicto == "denegada"
    assert compuerta.evaluar(_local("drive_crear_carpeta")).veredicto == "permitida"
    for nombre in ("", "a/b", "a\\b", "x" * 121, "tab\tx", 7):
        assert compuerta.evaluar(_local("drive_crear_carpeta", nombre=nombre)).veredicto == "denegada", nombre
    # Sin canal local, denegada.
    assert compuerta.evaluar(_local("conectar_google", canal=None)).veredicto == "denegada"
    # desconectar_google: confirmación clásica.
    desconectar = compuerta.evaluar(_local("desconectar_google"))
    assert desconectar.veredicto == "confirmar" and desconectar.aprobada is False


def test_v022_conectar_google_apagado_u_offline(ajustes: Settings) -> None:
    for cambios in ({"google_habilitado": False}, {"offline": True}):
        compuerta = _v022(ajustes, **cambios)
        decision = compuerta.evaluar(_local("conectar_google"))
        assert decision.veredicto == "denegada" and "RAMS_GOOGLE" in decision.motivo
        assert compuerta.evaluar(_local("drive_descargar")).veredicto == "denegada"


def test_v022_desconectar_google_usa_la_confirmacion_clasica(ajustes: Settings, correr) -> None:
    bus = BusEventos()
    preguntas: list[str] = []
    compuerta = CompuertaPermisos(ajustes.con(google_habilitado=True, offline=False), bus,
                                  preguntar=preguntas.append, pids_propios=PIDS_PROPIOS)
    solicitudes: list[dict[str, object]] = []

    async def principal() -> DecisionPermiso:
        bus.vincular_loop()
        bus.suscribir("permiso.solicitado", lambda e: solicitudes.append(dict(e.datos)))
        tarea = asyncio.ensure_future(compuerta.autorizar(_local("desconectar_google")))
        while not compuerta.hay_pendiente():
            await asyncio.sleep(0.001)
        compuerta.resolver("sí")
        decision = await tarea
        await asyncio.sleep(0.01)
        await bus.cerrar(timeout=0.5)
        return decision

    decision = correr(principal())
    assert decision.veredicto == "confirmar" and decision.aprobada is True
    assert "Desconectar tu cuenta de Google" in preguntas[0]
    assert solicitudes and solicitudes[0]["tipo"] == "desconectar_google"


def test_v022_fila_de_tarjeta(ajustes: Settings, correr) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj)
    _registrar(compuerta, "aaaa0001", "drive_subir")
    ok = _tarjeta_accion("drive_subir", "aaaa0001")
    assert compuerta.evaluar(ok).veredicto == "permitida"
    casos = {
        "inexistente": _tarjeta_accion("drive_subir", "ffff0000"),
        "otro_tipo": _tarjeta_accion("drive_papelera", "aaaa0001"),
        "otra_huella": _tarjeta_accion("drive_subir", "aaaa0001", sha=H2),
        "sin_aprobacion": AccionPC("drive_subir", {k: v for k, v in ok.args.items() if k != "aprobacion"}, "x"),
        "canal_terminal": AccionPC("drive_subir", {**ok.args, "canal": "terminal"}, "x"),
        "id_malo": _tarjeta_accion("drive_subir", "AAAA0001"),
        "huella_mala": _tarjeta_accion("drive_subir", "aaaa0001", sha="x" * 64),
    }
    for nombre, accion in casos.items():
        decision = compuerta.evaluar(accion)
        assert decision.veredicto == "denegada", nombre
    assert "botón de la tarjeta en el computador" in compuerta.evaluar(casos["canal_terminal"]).motivo
    assert "cambió" in compuerta.evaluar(casos["otra_huella"]).motivo
    # Caducada (reloj inyectado).
    reloj.t += 1800.0
    assert "ya no está vigente" in compuerta.evaluar(ok).motivo
    assert compuerta.tarjeta("aaaa0001") is None


def test_v022_un_solo_uso_y_evaluar_no_muta(ajustes: Settings, correr) -> None:
    compuerta = _v022(ajustes)
    _registrar(compuerta, "aaaa0002", "drive_papelera")
    accion = _tarjeta_accion("drive_papelera", "aaaa0002")
    for _ in range(5):
        assert compuerta.evaluar(accion).veredicto == "permitida"
    assert compuerta.tarjeta("aaaa0002") is not None

    async def doble_clic() -> list[DecisionPermiso]:
        return list(await asyncio.gather(compuerta.autorizar(accion), compuerta.autorizar(accion)))

    primera, segunda = correr(doble_clic())
    assert primera.aprobada is True and segunda.aprobada is False
    assert compuerta.tarjeta("aaaa0002") is None and compuerta.hay_pendiente() is False


def test_v022_registro_reemplazo_y_reapertura(ajustes: Settings) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj)
    primera, reemplazada = _registrar(compuerta, "c0000001", "enviar_correo")
    assert primera is not None and primera.version == 1 and primera.clase == "correo" and reemplazada is None
    assert primera.caduca == reloj.t + 1800.0
    otra, reemplazada = _registrar(compuerta, "c0000002", "enviar_correo")
    assert otra.version == 1 and reemplazada is not None and reemplazada.id == "c0000001"
    assert compuerta.tarjeta("c0000001") is None
    # Otra clase convive.
    _registrar(compuerta, "d0000001", "drive_subir")
    assert {t.id for t in compuerta.tarjetas()} == {"c0000002", "d0000001"}
    # Reapertura con la misma id (vigente): version+1 y sin reemplazo.
    reabierta, reemplazada = _registrar(compuerta, "c0000002", "enviar_correo", huella=H2)
    assert reabierta.version == 2 and reemplazada is None and reabierta.huella == H2
    # Reapertura tras retirarla (un uso): sigue la numeración.
    assert compuerta.retirar_tarjeta("c0000002").version == 2
    reabierta, _ = _registrar(compuerta, "c0000002", "enviar_correo")
    assert reabierta.version == 3
    # Formatos inválidos.
    for kw in ({"ident": "xyz", "tipo": "enviar_correo"}, {"ident": "c0000003", "tipo": "enviar_foto"},
               {"ident": "c0000003", "tipo": "enviar_correo", "huella": "corta"},
               {"ident": "c0000003", "tipo": "enviar_correo", "ttl_s": 0.0},
               {"ident": "c0000003", "tipo": "enviar_correo", "ttl_s": 86401.0},
               {"ident": "c0000003", "tipo": "enviar_correo", "canal_origen": "nexus"}):
        ident, tipo = kw.pop("ident"), kw.pop("tipo")
        assert _registrar(compuerta, ident, tipo, **kw) == (None, None)


def test_v022_actualizar_purgar_y_memoria_de_ids(ajustes: Settings) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj)
    _registrar(compuerta, "c0000001", "enviar_correo")
    reloj.t += 1000.0
    editada = compuerta.actualizar_tarjeta("c0000001", H2)
    assert editada.version == 2 and editada.huella == H2 and editada.caduca == reloj.t + 1800.0
    assert compuerta.actualizar_tarjeta("ffffffff", H2) is None
    assert compuerta.actualizar_tarjeta("c0000001", "mala") is None
    reloj.t += 1800.0
    vencidas = compuerta.purgar_caducadas()
    assert [t.id for t in vencidas] == ["c0000001"] and compuerta.tarjetas() == ()
    # Recuerda como mucho 32 ids retiradas: la más vieja vuelve a empezar en 1.
    for n in range(33):
        _registrar(compuerta, f"e{n:07d}", "ejecutar_script")
        compuerta.retirar_tarjeta(f"e{n:07d}")
    assert _registrar(compuerta, "e0000000", "ejecutar_script")[0].version == 1
    assert _registrar(compuerta, "e0000032", "ejecutar_script")[0].version == 2


def test_v022_tasa_de_correo(ajustes: Settings, correr) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj)

    def enviar(n: int) -> DecisionPermiso:
        ident = f"c{n:07d}"
        _registrar(compuerta, ident, "enviar_correo")
        return correr(compuerta.autorizar(_tarjeta_accion("enviar_correo", ident)))

    assert [enviar(n).aprobada for n in range(5)] == [True] * 5
    sexto = enviar(5)
    assert sexto.aprobada is False and "10 minutos" in sexto.motivo
    assert compuerta.correos_recientes() == (5, 5)
    reloj.t += 600.0
    assert enviar(6).aprobada is True
    assert compuerta.correos_recientes() == (1, 6)
    # Destinatarios 0 u 11 (máximo 10 por defecto) → denegado.
    for n in (0, 11, True, "2"):
        _registrar(compuerta, "c9999999", "enviar_correo")
        accion = _tarjeta_accion("enviar_correo", "c9999999", n_destinatarios=n)
        assert compuerta.evaluar(accion).veredicto == "denegada", n


def test_v022_tope_diario_de_correo(ajustes: Settings, correr) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj, correo_max_dia=3)
    resultados = []
    for n in range(4):
        _registrar(compuerta, f"c{n:07d}", "enviar_correo")
        resultados.append(correr(compuerta.autorizar(_tarjeta_accion("enviar_correo", f"c{n:07d}"))).aprobada)
        reloj.t += 700.0
    assert resultados == [True, True, True, False]
    reloj.t += 86400.0
    assert compuerta.correos_recientes() == (0, 0)


def test_v022_reserva_y_liberacion_del_cupo(ajustes: Settings, correr) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj)
    _registrar(compuerta, "c0000001", "enviar_correo")
    assert correr(compuerta.autorizar(_tarjeta_accion("enviar_correo", "c0000001"))).aprobada is True
    assert compuerta.correos_recientes() == (1, 1)
    assert compuerta.liberar_envio_correo("c0000001") is True
    assert compuerta.correos_recientes() == (0, 0)
    assert compuerta.liberar_envio_correo("c0000001") is False          # una sola vez
    assert compuerta.liberar_envio_correo("ffffffff") is False          # id desconocida: sin cambios
    assert compuerta.liberar_envio_correo(None) is False  # type: ignore[arg-type]
    # Cinco aprobaciones seguidas de fallos deterministas (liberadas) no agotan el cupo: la 6.ª pasa.
    for n in range(5):
        _registrar(compuerta, "c0000002", "enviar_correo")       # reapertura tras el fallo
        assert correr(compuerta.autorizar(_tarjeta_accion("enviar_correo", "c0000002"))).aprobada is True
        assert compuerta.liberar_envio_correo("c0000002") is True
    _registrar(compuerta, "c0000002", "enviar_correo")
    assert correr(compuerta.autorizar(_tarjeta_accion("enviar_correo", "c0000002"))).aprobada is True
    assert compuerta.correos_recientes() == (1, 1)
    # Una reserva de más de 24 h ya no se libera (está fuera de la ventana).
    reloj.t += 86400.0
    assert compuerta.liberar_envio_correo("c0000002") is False


def test_v022_compartir_en_publico(ajustes: Settings, correr) -> None:
    reloj = _RelojFalso()
    compuerta = _v022(ajustes, reloj=reloj)
    _registrar(compuerta, "d0000001", "drive_compartir", doble=True)
    publico = {"rol": "reader", "publico": True, "doble_confirmacion": True}
    # Sin el 1.er clic → denegado.
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000001", **publico)).veredicto == "denegada"
    assert compuerta.marcar_primera_confirmacion("d0000001", H2) is None          # otra huella
    paso1 = compuerta.marcar_primera_confirmacion("d0000001", H1)
    assert paso1 is not None and paso1.version == 2 and paso1.doble_ok_hasta == reloj.t + 120.0
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000001", **publico)).veredicto == "permitida"
    # El 2.º clic sin doble_confirmacion → denegado; con writer → denegado siempre.
    sin_doble = {**publico, "doble_confirmacion": False}
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000001", **sin_doble)).veredicto == "denegada"
    editable = compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000001", **{**publico, "rol": "writer"}))
    assert editable.veredicto == "denegada" and "público y editable" in editable.motivo
    # Doble vencido (> 120 s) → denegado.
    reloj.t += 121.0
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000001", **publico)).veredicto == "denegada"
    # Editar borra el 1.er clic.
    compuerta.marcar_primera_confirmacion("d0000001", H1)
    compuerta.actualizar_tarjeta("d0000001", H1)
    assert compuerta.tarjeta("d0000001").doble_ok_hasta is None
    # Una tarjeta sin doble no se puede aprobar como pública, ni una doble como privada.
    _registrar(compuerta, "d0000002", "drive_compartir")
    assert compuerta.marcar_primera_confirmacion("d0000002", H1) is None
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000002", **publico)).veredicto == "denegada"
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000002")).veredicto == "permitida"
    assert compuerta.evaluar(_tarjeta_accion("drive_compartir", "d0000002", rol="admin")).veredicto == "denegada"


def test_v022_ejecutar_script(ajustes: Settings, correr) -> None:
    compuerta = _v022(ajustes)
    _registrar(compuerta, "5c000001", "ejecutar_script")
    assert compuerta.evaluar(_tarjeta_accion("ejecutar_script", "5c000001")).veredicto == "permitida"
    for nivel in ("critico", None, "desconocido"):
        decision = compuerta.evaluar(_tarjeta_accion("ejecutar_script", "5c000001", nivel=nivel))
        assert decision.veredicto == "denegada", nivel
    assert "crítico" in compuerta.evaluar(_tarjeta_accion("ejecutar_script", "5c000001", nivel="critico")).motivo
    apagado = _v022(ajustes, taller_habilitado=False)
    _registrar(apagado, "5c000001", "ejecutar_script")
    decision = apagado.evaluar(_tarjeta_accion("ejecutar_script", "5c000001"))
    assert decision.veredicto == "denegada" and "RAMS_TALLER" in decision.motivo


def test_v022_tarjetas_no_publican_permiso_ni_se_resuelven_por_texto(ajustes: Settings, correr) -> None:
    bus = BusEventos()
    compuerta = _v022(ajustes, bus=bus)
    eventos: list[str] = []

    async def principal() -> DecisionPermiso:
        bus.vincular_loop()
        bus.suscribir("permiso.solicitado", lambda e: eventos.append(e.nombre))
        _registrar(compuerta, "aaaa0003", "drive_subir")
        decision = await compuerta.autorizar(_tarjeta_accion("drive_subir", "aaaa0003"))
        await asyncio.sleep(0.01)
        await bus.cerrar(timeout=0.5)
        return decision

    assert correr(principal()).aprobada is True
    assert eventos == []
    assert compuerta.resolver("sí") is False and compuerta.resolver_por_id("aaaa0003", True) is False
