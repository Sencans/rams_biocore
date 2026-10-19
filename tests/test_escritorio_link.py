"""Tests del endpoint ``/escritorio`` (grupo escritorio-ws, ``docs/COMPANION.md`` §C2.4 y §C3)
y del respaldo Puter de Broca sobre ese endpoint (§C2.15).

Sin red externa ni hardware: un ``ServidorHUD`` propio en ``127.0.0.1:0`` y clientes
``websockets`` locales (para Puter, un Amadeus Desktop falso que contesta los ``llm.pedir``: nunca
se carga Puter.js ni se inicia sesión en Puter). El token de ``/escritorio`` va a ``tmp_path`` (``settings_prueba``): ningún
test toca ``%LOCALAPPDATA%\\rams\\escritorio.json`` ni el R.A.M.S. en vivo (``:8765``).
Sin pytest-asyncio: las pruebas asíncronas usan la fixture ``correr`` (``asyncio.run``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")
httpx = pytest.importorskip("httpx")

from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

from core.bus import BusEventos, Evento  # noqa: E402
from core.config import Settings  # noqa: E402
from core.contracts import EstadoModulo, HomeostaticState  # noqa: E402
from interface import escritorio_link  # noqa: E402
from interface.escritorio_link import (  # noqa: E402
    CODIGO_REEMPLAZADO,
    ERRORES_PUTER,
    EVENTOS_SUSCRITOS_ESCRITORIO,
    MAX_MENSAJES_PUTER,
    MAX_PEDIDOS_PUTER,
    MAX_SYSTEM_PUTER,
    MAX_TEXTO_MENSAJE_PUTER,
    EnlaceEscritorio,
    RespaldoPuter,
    borrar_archivo_token_si_propio,
    escribir_archivo_token,
)
from interface.hud_server import EVENTOS_SUSCRITOS, ServidorHUD  # noqa: E402

# websockets ≥ 15 usa por defecto el proxy del entorno: en los tests se desactiva.
_KW_CONEXION: dict[str, Any] = {"proxy": None} if "proxy" in inspect.signature(connect).parameters else {}


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _token_en_tmp(settings_prueba: Settings, tmp_path: Path) -> None:
    """Salvaguarda: el token de escritorio de los tests siempre vive en ``tmp_path``."""
    assert Path(settings_prueba.escritorio_token_file).resolve().is_relative_to(tmp_path.resolve())


def _servidor(settings: Settings, bus: BusEventos, **cambios: Any) -> ServidorHUD:
    base = {"hud_habilitado": True, "hud_puerto": 0, "escritorio_habilitado": True}
    base.update(cambios)
    return ServidorHUD(settings.con(**base), bus)


def _archivo(settings: Settings) -> dict[str, Any]:
    return json.loads(Path(settings.escritorio_token_file).read_text(encoding="utf-8"))


def _url_escritorio(hud: ServidorHUD, token: str) -> str:
    return f"ws://127.0.0.1:{hud.puerto_escritorio}/escritorio?t={token}"


def _conectar(hud: ServidorHUD, token: str, *, origen: str | None = None) -> Any:
    """Cliente de ``/escritorio``: sin ``origen`` = Amadeus Desktop; con ``origen`` = panel del HUD."""
    return connect(_url_escritorio(hud, token), origin=origen, open_timeout=5, **_KW_CONEXION)


def _origen_hud(hud: ServidorHUD, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{hud.puerto_escritorio}"


def _conectar_ws(hud: ServidorHUD) -> Any:
    """Cliente de ``/ws`` (página del HUD)."""
    return connect(f"ws://127.0.0.1:{hud.puerto_real}/ws?t={hud.token}",
                   origin=f"http://127.0.0.1:{hud.puerto_real}", open_timeout=5, **_KW_CONEXION)


async def _rechazo(conexion: Any) -> int:
    with pytest.raises(InvalidStatus) as info:
        async with conexion:
            pass
    return info.value.response.status_code


async def _recibir_hasta(ws: Any, tipo: str, *, timeout: float = 3.0,
                         cond: Any = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Lee mensajes hasta el primero de ``tipo`` (que cumpla ``cond``). Devuelve sus datos y lo visto."""
    vistos: list[dict[str, Any]] = []
    async with asyncio.timeout(timeout):
        while True:
            mensaje = json.loads(await ws.recv())
            vistos.append(mensaje)
            if mensaje["tipo"] == tipo and (cond is None or cond(mensaje["datos"])):
                return mensaje["datos"], vistos


async def _recoger(ws: Any, segundos: float) -> list[dict[str, Any]]:
    """Todos los mensajes que lleguen durante ``segundos``."""
    vistos: list[dict[str, Any]] = []
    limite = time.monotonic() + segundos
    while True:
        restante = limite - time.monotonic()
        if restante <= 0:
            return vistos
        try:
            vistos.append(json.loads(await asyncio.wait_for(ws.recv(), restante)))
        except TimeoutError:
            return vistos


async def _esperar(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


class _Registro:
    """Recoge todos los eventos del bus (en los tests sí se puede escuchar ``*``)."""

    def __init__(self, bus: BusEventos) -> None:
        self.eventos: list[Evento] = []
        bus.suscribir("*", self.eventos.append)

    def de(self, nombre: str) -> list[dict[str, Any]]:
        return [dict(e.datos) for e in self.eventos if e.nombre == nombre]


def _estado(**kw: Any) -> dict[str, Any]:
    estado = HomeostaticState(**kw)
    return {"estado": estado.a_dict(), "bump": list(estado.bump)}


# ---------------------------------------------------------------------------
# Token, archivo y diagnóstico
# ---------------------------------------------------------------------------


def test_token_archivo_diagnostico_y_borrado(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.diagnostico_escritorio().estado is EstadoModulo.EN_ESPERA
        await hud.iniciar()
        ruta = Path(settings_prueba.escritorio_token_file)
        try:
            datos = _archivo(settings_prueba)
            assert set(datos) == {"url", "token", "pid"}
            assert datos["url"] == f"ws://127.0.0.1:{hud.puerto_escritorio}/escritorio"
            assert datos["pid"] == os.getpid()
            token = datos["token"]
            assert isinstance(token, str) and len(token) >= 16
            assert token != hud.token  # distinto del token del HUD
            if os.name == "posix":
                assert stat.S_IMODE(ruta.stat().st_mode) == 0o600
            # Sin temporales sueltos en el directorio.
            assert [p.name for p in ruta.parent.iterdir() if p.name.startswith(".escritorio-")] == []

            diag = hud.diagnostico_escritorio()
            assert diag.clave == "escritorio" and diag.estado is EstadoModulo.OK
            assert diag.detalle == datos["url"]
            assert token not in diag.detalle and token not in repr(hud)
            assert hud.escritorio is not None and token not in repr(hud.escritorio)
            assert hud.escritorio.url == datos["url"]
        finally:
            await hud.cerrar()
        assert not ruta.exists()  # borrado al cerrar (seguía siendo propio)
        await hud.cerrar()  # idempotente

    correr(caso())


def test_cerrar_no_borra_token_ajeno(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        ruta = Path(settings_prueba.escritorio_token_file)
        propio = _archivo(settings_prueba)
        # Otra instancia de R.A.M.S. reescribió el archivo: no se toca al cerrar.
        ajeno = {"url": "ws://127.0.0.1:1/escritorio", "token": "otro-token-de-otra-instancia", "pid": 4242}
        ruta.write_text(json.dumps(ajeno), encoding="utf-8")
        await hud.cerrar()
        assert json.loads(ruta.read_text(encoding="utf-8")) == ajeno

        # Mismo token pero otro pid: tampoco.
        mismo_token = dict(propio, pid=propio["pid"] + 1)
        ruta.write_text(json.dumps(mismo_token), encoding="utf-8")
        assert borrar_archivo_token_si_propio(ruta, propio["token"], propio["pid"]) is False
        assert ruta.exists()
        # JSON roto o archivo ausente: False sin lanzar.
        ruta.write_text("{roto", encoding="utf-8")
        assert borrar_archivo_token_si_propio(ruta, propio["token"], propio["pid"]) is False
        ruta.unlink()
        assert borrar_archivo_token_si_propio(ruta, propio["token"], propio["pid"]) is False

    correr(caso())


def test_escribir_archivo_token_atomico(tmp_path: Path) -> None:
    ruta = tmp_path / "sub" / "rams" / "escritorio.json"
    escribir_archivo_token(ruta, {"url": "ws://127.0.0.1:9/escritorio", "token": "abc", "pid": 1})
    escribir_archivo_token(ruta, {"url": "ws://127.0.0.1:9/escritorio", "token": "def", "pid": 1})
    assert json.loads(ruta.read_text(encoding="utf-8"))["token"] == "def"
    assert sorted(p.name for p in ruta.parent.iterdir()) == ["escritorio.json"]
    if os.name == "posix":
        assert stat.S_IMODE(ruta.stat().st_mode) == 0o600
    assert borrar_archivo_token_si_propio(ruta, "def", 1) is True and not ruta.exists()


def test_token_ausente_de_hola_repr_y_logs(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                          caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams")

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as ws:
                hola, _ = await _recibir_hasta(ws, "hola")
                assert set(hola) == {"version", "nombre_ia", "usuario"}
                assert hola["nombre_ia"] == "Amadeus" and hola["usuario"] == settings_prueba.trato_usuario
                assert token not in json.dumps(hola)
                await ws.send(json.dumps({"tipo": "hola_app", "pid": 1234, "version": "0.2.0"}))
                await ws.send(json.dumps({"tipo": "chat", "texto": "mi secreto de chat 31415"}))
                await asyncio.sleep(0.2)
            async with _conectar_ws(hud) as ws_hud:
                hola_hud, _ = await _recibir_hasta(ws_hud, "hola")
                assert token not in json.dumps(hola_hud) and hud.token not in json.dumps(hola_hud)
            await _rechazo(_conectar(hud, "malo"))
        finally:
            await hud.cerrar()
        assert token not in caplog.text
        assert hud.token not in caplog.text
        assert "mi secreto de chat" not in caplog.text  # escritorio.chat nunca se registra

    correr(caso())


# ---------------------------------------------------------------------------
# Handshake y límites por tipo de cliente
# ---------------------------------------------------------------------------


def test_handshake_origin_y_token(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            # Sin Origin (Amadeus Desktop) → OK.
            async with _conectar(hud, token) as ws:
                await _recibir_hasta(ws, "hola")
            # Origin local del HUD (127.0.0.1 y localhost) → OK.
            for host in ("127.0.0.1", "localhost"):
                async with _conectar(hud, token, origen=_origen_hud(hud, host)) as ws:
                    await _recibir_hasta(ws, "hola")
            # Origin ajeno, puerto equivocado o token malo → 403.
            assert await _rechazo(_conectar(hud, token, origen="http://evil.example")) == 403
            assert await _rechazo(_conectar(hud, token, origen=f"http://127.0.0.1:{hud.puerto_escritorio + 1}")) == 403
            assert await _rechazo(_conectar(hud, "incorrecto")) == 403
            assert await _rechazo(_conectar(hud, "")) == 403
            # Los tokens no se intercambian entre /ws y /escritorio.
            assert await _rechazo(_conectar(hud, hud.token)) == 403
            url_ws = f"ws://127.0.0.1:{hud.puerto_real}/ws?t={token}"
            assert await _rechazo(connect(url_ws, origin=_origen_hud(hud), open_timeout=5, **_KW_CONEXION)) == 403
            # Varias cabeceras Origin → 403 (nunca se toma por Electron).
            dobles = connect(_url_escritorio(hud, token), open_timeout=5,
                             additional_headers=[("Origin", _origen_hud(hud)), ("Origin", "http://evil.example")],
                             **_KW_CONEXION)
            assert await _rechazo(dobles) == 403
            # Por HTTP sin token también se rechaza.
            async with httpx.AsyncClient(trust_env=False, timeout=5.0) as cliente:
                r = await cliente.get(f"http://127.0.0.1:{hud.puerto_escritorio}/escritorio")
                assert r.status_code == 403
        finally:
            await hud.cerrar()

    correr(caso())


def test_segundo_cliente_sin_origin_reemplaza_al_primero(settings_prueba: Settings, bus: BusEventos,
                                                         correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as primero:
                await _recibir_hasta(primero, "hola")
                await _esperar(lambda: registro.de("escritorio.conectado") == [
                    {"clientes": 1, "overlay": True, "hud": 0}])
                async with _conectar(hud, token) as segundo:
                    await _recibir_hasta(segundo, "hola")
                    with pytest.raises(ConnectionClosed):
                        async with asyncio.timeout(3):
                            while True:
                                await primero.recv()
                    assert primero.close_code == CODIGO_REEMPLAZADO == 4001
                    assert hud.escritorio is not None and hud.escritorio.overlay_conectado
                    # El segundo sigue vivo y recibe lo que publica R.A.M.S.
                    bus.publicar("escritorio.sistema", {"texto": "sigues aquí"})
                    sistema, _ = await _recibir_hasta(segundo, "sistema")
                    assert sistema == {"texto": "sigues aquí"}
                    # Nunca hubo un overlay:false entre medias.
                    assert all(d["overlay"] for d in registro.de("escritorio.conectado"))
                await _esperar(lambda: registro.de("escritorio.conectado")[-1] ==
                               {"clientes": 0, "overlay": False, "hud": 0})
        finally:
            await hud.cerrar()

    correr(caso())


def test_tercer_cliente_del_hud_403(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            origen = _origen_hud(hud)
            async with _conectar(hud, token, origen=origen) as a, _conectar(hud, token, origen=origen) as b:
                await _recibir_hasta(a, "hola")
                await _recibir_hasta(b, "hola")
                assert await _rechazo(_conectar(hud, token, origen=origen)) == 403
                # El cliente de Electron no cuenta para ese límite.
                async with _conectar(hud, token) as app:
                    await _recibir_hasta(app, "hola")
                    await _esperar(lambda: registro.de("escritorio.conectado")[-1] ==
                                   {"clientes": 3, "overlay": True, "hud": 2})
            await _esperar(lambda: registro.de("escritorio.conectado")[-1] ==
                           {"clientes": 0, "overlay": False, "hud": 0})
            assert {"clientes": 1, "overlay": False, "hud": 1} in registro.de("escritorio.conectado")
        finally:
            await hud.cerrar()

    correr(caso())


# ---------------------------------------------------------------------------
# Entrantes
# ---------------------------------------------------------------------------


def test_chat_publica_solo_escritorio_chat(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(app, "hola")
                await _recibir_hasta(panel, "hola")
                await app.send(json.dumps({"tipo": "chat", "texto": "  Hola, Kurisu  "}))
                await _esperar(lambda: len(registro.de("escritorio.chat")) == 1)
                await panel.send(json.dumps({"tipo": "chat", "texto": "desde el HUD"}))
                await _esperar(lambda: len(registro.de("escritorio.chat")) == 2)
                assert registro.de("escritorio.chat") == [{"texto": "Hola, Kurisu"}, {"texto": "desde el HUD"}]
                # Límites de texto: vacío, solo espacios, > 2000, no texto → nada (≤ 5 mensajes por ventana).
                await asyncio.sleep(1.05)
                for malo in ("", "   ", "x" * 2001, 42):
                    await app.send(json.dumps({"tipo": "chat", "texto": malo}))
                await asyncio.sleep(1.05)
                for malo in (None, ["hola"]):
                    await app.send(json.dumps({"tipo": "chat", "texto": malo}))
                await app.send(json.dumps({"tipo": "chat", "texto": "y" * 2000}))  # justo en el límite
                await _esperar(lambda: len(registro.de("escritorio.chat")) == 3)
                await asyncio.sleep(0.2)
                assert len(registro.de("escritorio.chat")) == 3
            nombres = {e.nombre for e in registro.eventos}
            assert "usuario.entrada" not in nombres  # §C2.3 (c): exclusivo de main
            assert all(e.origen == "escritorio" for e in registro.eventos if e.nombre.startswith("escritorio."))
        finally:
            await hud.cerrar()

    correr(caso())


def test_hola_app_evento_y_caja_solo_desde_electron(settings_prueba: Settings, bus: BusEventos,
                                                    correr: Any) -> None:
    registro = _Registro(bus)
    mensajes = [
        {"tipo": "hola_app", "pid": 4321, "version": "0.2.0"},
        {"tipo": "evento", "nombre": "toque", "datos": {}},
        {"tipo": "caja", "x": 10, "y": 20, "w": 340, "h": 600},
    ]

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(panel, "hola")
                for mensaje in mensajes:
                    await panel.send(json.dumps(mensaje))
                await asyncio.sleep(0.3)
                for nombre in ("escritorio.app", "escritorio.evento", "escritorio.caja"):
                    assert registro.de(nombre) == [], nombre
            async with _conectar(hud, token) as app:
                await _recibir_hasta(app, "hola")
                for mensaje in mensajes:
                    await app.send(json.dumps(mensaje))
                await _esperar(lambda: registro.de("escritorio.caja") != [])
                assert registro.de("escritorio.app") == [{"pid": 4321}]
                assert registro.de("escritorio.evento") == [{"nombre": "toque", "datos": {}}]
                assert registro.de("escritorio.caja") == [{"x": 10, "y": 20, "w": 340, "h": 600}]
                # Un segundo hola_app en la misma conexión se ignora; pid inválido también.
                await asyncio.sleep(1.05)
                await app.send(json.dumps({"tipo": "hola_app", "pid": 999, "version": "x"}))
                await asyncio.sleep(0.2)
                assert registro.de("escritorio.app") == [{"pid": 4321}]
            async with _conectar(hud, token) as app2:
                await _recibir_hasta(app2, "hola")
                for pid in (0, -5, True, 3.5, "12"):
                    await app2.send(json.dumps({"tipo": "hola_app", "pid": pid}))
                await asyncio.sleep(0.3)
                assert registro.de("escritorio.app") == [{"pid": 4321}]
        finally:
            await hud.cerrar()

    correr(caso())


def test_evento_validaciones(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app:
                await _recibir_hasta(app, "hola")
                malos = [
                    {"tipo": "evento", "nombre": "volar", "datos": {}},
                    {"tipo": "evento", "nombre": "toque", "datos": {"lista": [1, 2]}},
                    {"tipo": "evento", "nombre": "toque", "datos": {"x": "y" * 1100}},
                    {"tipo": "evento", "nombre": "toque", "datos": "texto"},
                ]
                for mensaje in malos:
                    await app.send(json.dumps(mensaje))
                await asyncio.sleep(1.05)
                await app.send(json.dumps({"tipo": "evento", "nombre": "soltada", "datos": {"altura": 3}}))
                await app.send(json.dumps({"tipo": "evento", "nombre": "cursor_encima"}))
                await app.send(json.dumps({"tipo": "evento", "nombre": "cursor_encima"}))  # < 10 s: no
                await app.send(json.dumps({"tipo": "evento", "nombre": "mostrada", "datos": {"monitor": 1}}))
                await _esperar(lambda: len(registro.de("escritorio.evento")) == 3)
                await asyncio.sleep(0.2)
                assert registro.de("escritorio.evento") == [
                    {"nombre": "soltada", "datos": {"altura": 1.0}},
                    {"nombre": "cursor_encima", "datos": {}},
                    {"nombre": "mostrada", "datos": {"monitor": 1}},
                ]
        finally:
            await hud.cerrar()

    correr(caso())


def test_control_y_confirmacion(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(panel, "hola")
                for accion in ("no_molestar_on", "silenciar", "apagar_pc", ["pausar_vision"]):
                    await panel.send(json.dumps({"tipo": "control", "accion": accion}))
                await _esperar(lambda: registro.de("hud.control") != [])
                await asyncio.sleep(0.2)
                assert registro.de("escritorio.control") == [{"accion": "no_molestar_on"}]
                assert registro.de("hud.control") == [{"accion": "silenciar"}]

                await asyncio.sleep(1.05)
                invalidas = [
                    {"tipo": "confirmacion", "id": "0A1B2C3D", "aprobado": True},
                    {"tipo": "confirmacion", "id": "0a1b2c3", "aprobado": True},
                    {"tipo": "confirmacion", "id": "0a1b2c3d\n", "aprobado": True},
                    {"tipo": "confirmacion", "id": 12345678, "aprobado": True},
                    {"tipo": "confirmacion", "id": "0a1b2c3d", "aprobado": "si"},
                ]
                for mensaje in invalidas:
                    await panel.send(json.dumps(mensaje))
                await asyncio.sleep(0.3)
                assert registro.de("escritorio.confirmacion") == []
                await asyncio.sleep(1.05)
                await panel.send(json.dumps({"tipo": "confirmacion", "id": "0a1b2c3d", "aprobado": False}))
                await _esperar(lambda: registro.de("escritorio.confirmacion") != [])
                assert registro.de("escritorio.confirmacion") == [{"id": "0a1b2c3d", "aprobado": False}]
        finally:
            await hud.cerrar()

    correr(caso())


def test_limites_de_mensajes(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app:
                await _recibir_hasta(app, "hola")
                # Basura: se ignora sin cerrar la conexión.
                await app.send("esto no es JSON")
                await app.send(b"\x00\x01")
                await app.send(json.dumps([1, 2, 3]))
                await app.send(json.dumps({"tipo": "desconocido"}))
                await app.send(json.dumps({"tipo": "chat", "texto": "ñ" * 1900, "relleno": "z" * 6000}))  # > 8 KiB
                await asyncio.sleep(1.1)
                # Ráfaga: como mucho 5 mensajes por segundo.
                for i in range(10):
                    await app.send(json.dumps({"tipo": "chat", "texto": f"m{i}"}))
                await asyncio.sleep(0.4)
                assert registro.de("escritorio.chat") == [{"texto": f"m{i}"} for i in range(5)]
                assert hud.escritorio is not None and hud.escritorio.descartados >= 5
                # La conexión sigue viva.
                bus.publicar("escritorio.sistema", {"texto": "vivo"})
                assert (await _recibir_hasta(app, "sistema"))[0] == {"texto": "vivo"}
        finally:
            await hud.cerrar()

    correr(caso())


def test_caja_a_2_hz_y_solo_si_cambia(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app:
                await _recibir_hasta(app, "hola")
                for x in (100, 110, 120, 130):
                    await app.send(json.dumps({"tipo": "caja", "x": x, "y": 5, "w": 340, "h": 600}))
                await asyncio.sleep(0.15)
                assert [c["x"] for c in registro.de("escritorio.caja")] == [100]
                await asyncio.sleep(0.6)  # vence el intervalo de 0,5 s: sale la última
                assert [c["x"] for c in registro.de("escritorio.caja")] == [100, 130]
                await asyncio.sleep(1.0)
                await app.send(json.dumps({"tipo": "caja", "x": 130, "y": 5, "w": 340, "h": 600}))  # igual
                for mala in ({"x": 1, "y": 1, "w": 0, "h": 10}, {"x": 1, "y": 1, "w": 2001, "h": 10},
                             {"x": 1.5, "y": 1, "w": 10, "h": 10}, {"x": True, "y": 1, "w": 10, "h": 10}):
                    await app.send(json.dumps({"tipo": "caja", **mala}))
                await asyncio.sleep(0.7)
                assert [c["x"] for c in registro.de("escritorio.caja")] == [100, 130]
        finally:
            await hud.cerrar()

    correr(caso())


# ---------------------------------------------------------------------------
# Salientes
# ---------------------------------------------------------------------------


def test_salientes_a_todos_y_solo_a_electron(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(app, "hola")
                await _recibir_hasta(panel, "hola")
                bus.publicar("salida.subtitulo", {"texto": "El cerebro no es magia.", "exp": "seria",
                                                  "motion": "asentir", "tone": "analitico",
                                                  "canal": "terminal", "origen": "respuesta"})
                for ws in (app, panel):
                    datos, _ = await _recibir_hasta(ws, "subtitulo")
                    assert datos == {"texto": "El cerebro no es magia.", "exp": "seria",
                                     "canal": "terminal", "origen": "respuesta"}
                bus.publicar("voz.inicio", {"motor": "edge", "tone": "calido", "utterance_id": "u1"})
                for ws in (app, panel):
                    assert (await _recibir_hasta(ws, "voz"))[0] == {"hablando": True}
                bus.publicar("motor.travesuras", {"activas": False, "hasta_ts": 1234.5})
                for ws in (app, panel):
                    assert (await _recibir_hasta(ws, "travesuras"))[0] == {"activas": False, "hasta_ts": 1234.5}
                bus.publicar("escritorio.sistema", {"texto": "Comandos: /estado, /ayuda"})
                for ws in (app, panel):
                    assert (await _recibir_hasta(ws, "sistema"))[0] == {"texto": "Comandos: /estado, /ayuda"}

                # Órdenes: solo al cliente de Electron, saneadas.
                bus.publicar("escritorio.orden", {"accion": "aparecer", "borde": "left", "modo": "asomarse",
                                                  "extra": "x"})
                assert (await _recibir_hasta(app, "orden"))[0] == {"accion": "aparecer", "borde": "left",
                                                                   "modo": "asomarse"}
                bus.publicar("escritorio.orden", {"accion": "volar"})
                bus.publicar("escritorio.orden", {"accion": "notificar", "texto": "Mira esto",
                                                  "url": "http://inseguro.example/x"})
                assert (await _recibir_hasta(app, "orden"))[0] == {"accion": "notificar", "texto": "Mira esto"}
                bus.publicar("escritorio.orden", {"accion": "notificar", "texto": "Y esto",
                                                  "url": "https://example.org/articulo?id=1"})
                assert (await _recibir_hasta(app, "orden"))[0] == {"accion": "notificar", "texto": "Y esto",
                                                                   "url": "https://example.org/articulo?id=1"}

                # Estado homeostático: solo a Electron, ≤ 1 Hz con coalescencia.
                await _recoger(app, 1.05)
                for i in range(5):
                    bus.publicar("homeostasis.estado", _estado(boredom=0.1 * (i + 1), fatiga=0.3, valencia=-0.2))
                estados = [m["datos"] for m in await _recoger(app, 1.4) if m["tipo"] == "estado"]
                assert len(estados) == 2
                assert estados[0] == {"aburrimiento": pytest.approx(0.1), "fatiga": pytest.approx(0.3),
                                      "valencia": pytest.approx(-0.2)}
                assert estados[-1]["aburrimiento"] == pytest.approx(0.5)

                vistos_panel = await _recoger(panel, 0.3)
                assert not [m for m in vistos_panel if m["tipo"] in ("orden", "estado")]
        finally:
            await hud.cerrar()

    correr(caso())


def test_permiso_con_vista_previa_solo_por_escritorio(settings_prueba: Settings, bus: BusEventos,
                                                     correr: Any) -> None:
    solicitado = {"id": "a1b2c3d4", "descripcion": "Abrir WhatsApp con un borrador de 9 caracteres (no se enviará)",
                  "destructiva": False, "tipo": "prerrellenar", "vista_previa": "hola mamá"}

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar_ws(hud) as ws_hud, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(ws_hud, "hola")
                await _recibir_hasta(panel, "hola")
                bus.publicar("permiso.solicitado", solicitado)
                datos, _ = await _recibir_hasta(panel, "permiso")
                assert datos == {"id": "a1b2c3d4", "descripcion": solicitado["descripcion"],
                                 "destructiva": False, "pendiente": True, "vista_previa": "hola mamá"}
                hud_datos, _ = await _recibir_hasta(ws_hud, "permiso")
                assert "vista_previa" not in hud_datos and hud_datos["pendiente"] is True

                # Un cliente nuevo recibe el permiso pendiente (con vista previa) en el saludo.
                async with _conectar(hud, token) as app:
                    _hola, vistos = await _recibir_hasta(app, "permiso")
                    assert vistos[0]["tipo"] == "hola"
                    assert vistos[-1]["datos"]["vista_previa"] == "hola mamá"

                bus.publicar("permiso.resuelto", {"id": "a1b2c3d4", "aprobado": True, "motivo": "aprobada"})
                resuelto, _ = await _recibir_hasta(panel, "permiso")
                assert resuelto == {"id": "a1b2c3d4", "descripcion": solicitado["descripcion"],
                                    "destructiva": False, "pendiente": False, "aprobado": True}
                vistos_hud = await _recoger(ws_hud, 0.3)
                assert not any("hola mamá" in json.dumps(m, ensure_ascii=False) for m in vistos_hud)
                # Tras resolverse, un cliente nuevo ya no recibe el permiso.
                async with _conectar(hud, token) as app2:
                    tipos = [m["tipo"] for m in await _recoger(app2, 0.3)]
                    assert "permiso" not in tipos
        finally:
            await hud.cerrar()

    correr(caso())


def test_escritorio_chat_y_sistema_nunca_por_ws(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    assert "escritorio.chat" not in EVENTOS_SUSCRITOS and "escritorio.sistema" not in EVENTOS_SUSCRITOS
    assert "escritorio.chat" not in EVENTOS_SUSCRITOS_ESCRITORIO
    assert not any(n == "*" or n.endswith(".*") for n in EVENTOS_SUSCRITOS + EVENTOS_SUSCRITOS_ESCRITORIO)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar_ws(hud) as ws_hud:
                await _recibir_hasta(ws_hud, "hola")
                await _recoger(ws_hud, 0.2)
                bus.publicar("escritorio.sistema", {"texto": "salida de /estado"})
                bus.publicar("escritorio.chat", {"texto": "texto crudo del usuario"})
                bus.publicar("escritorio.orden", {"accion": "esconderse"})
                vistos = await _recoger(ws_hud, 0.4)
                texto = json.dumps(vistos, ensure_ascii=False)
                assert "salida de /estado" not in texto and "texto crudo del usuario" not in texto
                assert not [m for m in vistos if m["tipo"] in ("sistema", "chat", "orden")]
        finally:
            await hud.cerrar()

    correr(caso())


# ---------------------------------------------------------------------------
# Arranque condicionado: RAMS_HUD=0, RAMS_ESCRITORIO=0 y RAMS_HUD_CHAT=0
# ---------------------------------------------------------------------------


def test_hud_deshabilitado_escritorio_funciona(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus, hud_habilitado=False)
        diag = await hud.iniciar()
        try:
            assert diag.clave == "hud" and diag.estado is EstadoModulo.OFF
            assert hud.url is None and hud.puerto_real is None
            assert hud.puerto_escritorio is not None
            assert hud.diagnostico_escritorio().estado is EstadoModulo.OK
            base = f"http://127.0.0.1:{hud.puerto_escritorio}"
            async with httpx.AsyncClient(trust_env=False, timeout=5.0) as cliente:
                for ruta in ("/", "/index.html", "/ws", "/favicon.ico"):
                    assert (await cliente.get(base + ruta)).status_code == 404, ruta
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app:
                await _recibir_hasta(app, "hola")
                await app.send(json.dumps({"tipo": "chat", "texto": "hola sin HUD"}))
                await _esperar(lambda: registro.de("escritorio.chat") == [{"texto": "hola sin HUD"}])
            # Sin HUD no hay panel de chat: un Origin "local" no tiene a quién representar.
            assert await _rechazo(_conectar(hud, token, origen=base)) == 403
        finally:
            await hud.cerrar()

    correr(caso())


def test_escritorio_deshabilitado(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus, escritorio_habilitado=False)
        assert hud.escritorio is None
        diag_esc = hud.diagnostico_escritorio()
        assert diag_esc.estado is EstadoModulo.OFF and diag_esc.detalle == "deshabilitado"
        assert (await hud.iniciar()).estado is EstadoModulo.OK
        try:
            assert hud.puerto_escritorio is None
            assert not Path(settings_prueba.escritorio_token_file).exists()
            async with httpx.AsyncClient(trust_env=False, timeout=5.0) as cliente:
                r = await cliente.get(f"http://127.0.0.1:{hud.puerto_real}/escritorio?t=x")
                assert r.status_code == 404
            async with _conectar_ws(hud) as ws_hud:
                tipos = [m["tipo"] for m in await _recoger(ws_hud, 0.4)]
                assert tipos[0] == "hola" and "escritorio" not in tipos
        finally:
            await hud.cerrar()
        # Ni HUD ni escritorio: OFF y sin servidor.
        nada = _servidor(settings_prueba, bus, hud_habilitado=False, escritorio_habilitado=False)
        diag = await nada.iniciar()
        assert diag.estado is EstadoModulo.OFF and nada.puerto_escritorio is None and nada.puerto_real is None
        await nada.cerrar()

    correr(caso())


def test_hud_chat_deshabilitado(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus, hud_chat=False)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            assert await _rechazo(_conectar(hud, token, origen=_origen_hud(hud))) == 403
            async with _conectar(hud, token) as app:  # Electron sigue funcionando
                await _recibir_hasta(app, "hola")
            async with _conectar_ws(hud) as ws_hud:
                tipos = [m["tipo"] for m in await _recoger(ws_hud, 0.4)]
                assert tipos[0] == "hola" and "escritorio" not in tipos
        finally:
            await hud.cerrar()

    correr(caso())


def test_puerto_ocupado_diagnostico_escritorio(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    import socket

    ocupante = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        ocupante.bind(("127.0.0.1", 0))
        ocupante.listen(1)
        puerto = ocupante.getsockname()[1]

        async def caso() -> None:
            hud = _servidor(settings_prueba, bus, hud_puerto=puerto)
            assert (await hud.iniciar()).estado is EstadoModulo.SIN_CONEXION
            diag = hud.diagnostico_escritorio()
            assert diag.estado is EstadoModulo.SIN_CONEXION and diag.detalle == "puerto ocupado"
            # No se pisa el token de la instancia que sí escucha en ese puerto.
            assert not Path(settings_prueba.escritorio_token_file).exists()
            await hud.cerrar()

        correr(caso())
    finally:
        ocupante.close()


def test_sin_websockets_diagnostico_off(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from interface import hud_server

    monkeypatch.setattr(hud_server, "WEBSOCKETS_DISPONIBLE", False)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert (await hud.iniciar()).estado is EstadoModulo.OFF
        diag = hud.diagnostico_escritorio()
        assert diag.estado is EstadoModulo.OFF and diag.detalle == "websockets no instalado"
        await hud.cerrar()

    correr(caso())


# ---------------------------------------------------------------------------
# /ws: mensaje «escritorio» justo después del hola y uso del token desde el HUD
# ---------------------------------------------------------------------------


def test_ws_envia_token_de_escritorio_tras_el_hola(settings_prueba: Settings, bus: BusEventos,
                                                   correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar_ws(hud) as ws_hud:
                primero = json.loads(await asyncio.wait_for(ws_hud.recv(), 3))
                segundo = json.loads(await asyncio.wait_for(ws_hud.recv(), 3))
                assert primero["tipo"] == "hola"
                assert set(primero["datos"]) == {"version", "nombre_ia", "usuario", "inicio_ts"}
                assert segundo["tipo"] == "escritorio"
                assert segundo["datos"] == {"token": _archivo(settings_prueba)["token"], "url": "/escritorio"}
                # El panel del HUD usa ese token con su Origin local.
                async with _conectar(hud, segundo["datos"]["token"], origen=_origen_hud(hud, "localhost")) as panel:
                    await _recibir_hasta(panel, "hola")
                    await _esperar(lambda: registro.de("escritorio.conectado")[-1:] ==
                                   [{"clientes": 1, "overlay": False, "hud": 1}])
        finally:
            await hud.cerrar()

    correr(caso())


def test_enlace_sin_activar_no_acepta(settings_prueba: Settings, bus: BusEventos) -> None:
    enlace = EnlaceEscritorio(settings_prueba, bus, evitar_token="x")
    assert enlace.activo is False and enlace.url is None and enlace.datos_para_hud() is None
    assert enlace.diagnostico().estado is EstadoModulo.EN_ESPERA

    class _Conexion:
        def respond(self, estado: Any, texto: str) -> int:
            return int(estado)

    assert enlace.validar_handshake(_Conexion(), object(), "t=x") == 404
    assert escritorio_link.RUTA_ESCRITORIO == "/escritorio"


# ---------------------------------------------------------------------------
# Revisión: estado real del «no molestar» y ritmo por clase (§C2.4.5)
# ---------------------------------------------------------------------------


def test_no_molestar_estado_real_a_todos_y_en_la_instantanea(settings_prueba: Settings, bus: BusEventos,
                                                            correr: Any) -> None:
    """Los chats derivan el botón «No molestar» del estado real (``pc.no_molestar``), también al recargar."""
    assert "pc.no_molestar" in EVENTOS_SUSCRITOS_ESCRITORIO
    apagado = {"activo": False, "motivo": "", "usuario": False}

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            # Sin pc.no_molestar previo (main no publica el "sin no molestar" inicial): todo false.
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                datos, vistos = await _recibir_hasta(panel, "no_molestar")
                assert datos == apagado and vistos[0]["tipo"] == "hola"
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                for ws in (app, panel):
                    await _recibir_hasta(ws, "no_molestar")
                casos = (
                    ({"activo": True, "motivo": "usuario"}, {"activo": True, "motivo": "usuario", "usuario": True}),
                    ({"activo": True, "motivo": "juego"}, {"activo": True, "motivo": "juego", "usuario": False}),
                    ({"activo": "sí", "motivo": "<b>x</b>"}, apagado),            # saneado
                    ({"activo": True, "motivo": "inventado"}, {"activo": True, "motivo": "", "usuario": False}),
                    ({"activo": False, "motivo": "usuario"}, apagado),
                    ({"activo": True, "motivo": "usuario"}, {"activo": True, "motivo": "usuario", "usuario": True}),
                )
                for publicado, esperado in casos:
                    bus.publicar("pc.no_molestar", publicado)
                    for ws in (app, panel):
                        assert (await _recibir_hasta(ws, "no_molestar"))[0] == esperado
            # Un panel que se conecta (o una página que se recarga) recibe el estado real.
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                datos, vistos = await _recibir_hasta(panel, "no_molestar")
                assert datos == {"activo": True, "motivo": "usuario", "usuario": True}
                assert vistos[0]["tipo"] == "hola"
        finally:
            await hud.cerrar()

        # Publicado antes de crear el servidor: la instantánea sale de bus.ultimo().
        bus.publicar("pc.no_molestar", {"activo": True, "motivo": "presentacion"})
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar(hud, _archivo(settings_prueba)["token"]) as app:
                datos, _ = await _recibir_hasta(app, "no_molestar")
                assert datos == {"activo": True, "motivo": "presentacion", "usuario": False}
        finally:
            await hud.cerrar()

    correr(caso())


def test_vision_camara_or_de_fuentes_solo_a_electron(settings_prueba: Settings, bus: BusEventos,
                                                    correr: Any) -> None:
    """Ojo del overlay (§C2.13): ``vision{camara}`` = OR de ``vision.camara`` por ``fuente``, solo a
    Electron, en la instantánea (siempre) y solo cuando cambia (la continua se repite cada 5 s)."""
    assert "vision.camara" in EVENTOS_SUSCRITOS_ESCRITORIO

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                datos, vistos = await _recibir_hasta(app, "vision")
                assert datos == {"camara": False}
                assert [m["tipo"] for m in vistos][:1] == ["hola"]
                await _recibir_hasta(panel, "no_molestar")
                continua = {"activa": True, "habilitada": True, "duracion_ms": 0, "fuente": "continua"}
                bus.publicar("vision.camara", continua)
                assert (await _recibir_hasta(app, "vision"))[0] == {"camara": True}
                bus.publicar("vision.camara", {**continua, "duracion_ms": 5000})          # latido: nada
                bus.publicar("vision.camara", {"activa": True, "habilitada": True, "duracion_ms": 0})  # vistazo
                bus.publicar("vision.camara", {"activa": False, "habilitada": True, "duracion_ms": 900})
                assert not [m for m in await _recoger(app, 0.3) if m["tipo"] == "vision"]  # OR sigue True
                bus.publicar("vision.camara", {**continua, "activa": False})
                assert (await _recibir_hasta(app, "vision"))[0] == {"camara": False}
                assert not [m for m in await _recoger(panel, 0.3) if m["tipo"] == "vision"]
                bus.publicar("vision.camara", {**continua, "fuente": "inventada"})      # = vistazo
                assert (await _recibir_hasta(app, "vision"))[0] == {"camara": True}
            # Una reconexión de Electron recibe el estado real en la instantánea.
            async with _conectar(hud, token) as app:
                assert (await _recibir_hasta(app, "vision"))[0] == {"camara": True}
        finally:
            await hud.cerrar()

    correr(caso())


def test_ritmo_por_clase_no_pierde_estado_de_electron(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    """Una ráfaga de toques no se come ``ocultada``/``control``/``confirmacion`` (hallazgo de la revisión)."""
    registro = _Registro(bus)
    caplog.set_level(logging.INFO, logger="rams.escritorio")

    def visibilidad() -> list[str]:
        return [e["nombre"] for e in registro.de("escritorio.evento") if e["nombre"] in ("mostrada", "ocultada")]

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app:
                await _recibir_hasta(app, "hola")
                # Electron al conectar + el usuario aporreando el avatar, todo en < 1 s.
                await app.send(json.dumps({"tipo": "hola_app", "pid": 4321, "version": "0.2.0"}))
                await app.send(json.dumps({"tipo": "caja", "x": 1, "y": 2, "w": 340, "h": 600}))
                for _ in range(6):
                    await app.send(json.dumps({"tipo": "evento", "nombre": "toque", "datos": {}}))
                await app.send(json.dumps({"tipo": "evento", "nombre": "ocultada", "datos": {}}))
                await app.send(json.dumps({"tipo": "control", "accion": "no_molestar_on"}))
                await app.send(json.dumps({"tipo": "confirmacion", "id": "0a1b2c3d", "aprobado": True}))
                await app.send(json.dumps({"tipo": "chat", "texto": "sigo aquí"}))
                await _esperar(lambda: registro.de("escritorio.chat") != [])
                assert visibilidad() == ["ocultada"]
                assert registro.de("escritorio.app") == [{"pid": 4321}]
                assert registro.de("escritorio.control") == [{"accion": "no_molestar_on"}]
                assert registro.de("escritorio.confirmacion") == [{"id": "0a1b2c3d", "aprobado": True}]
                # El ruido sí tiene tope: caja + 4 toques caben en 5/s; sobran 2 toques.
                toques = [e for e in registro.de("escritorio.evento") if e["nombre"] == "toque"]
                assert len(toques) == 4 and len(registro.de("escritorio.caja")) == 1

                # Abuso de mensajes de estado (> 20/s): se descartan con log.info, pero la última
                # visibilidad no se pierde: se publica al vencer la ventana.
                await asyncio.sleep(1.05)
                for _ in range(20):
                    await app.send(json.dumps({"tipo": "control", "accion": "pausar_vision"}))
                for nombre in ("mostrada", "ocultada", "mostrada"):
                    await app.send(json.dumps({"tipo": "evento", "nombre": nombre, "datos": {}}))
                await app.send(json.dumps({"tipo": "control", "accion": "basta_travesuras"}))
                await _esperar(lambda: len(registro.de("escritorio.control")) == 21)
                await asyncio.sleep(0.1)
                assert visibilidad() == ["ocultada"]          # aún pospuesta
                assert {"accion": "basta_travesuras"} not in registro.de("escritorio.control")
                await _esperar(lambda: visibilidad() == ["ocultada", "mostrada"], timeout=2.0)
                await asyncio.sleep(0.3)
                assert visibilidad() == ["ocultada", "mostrada"]  # una sola, la última
                assert any("demasiados mensajes de estado" in r.getMessage() and r.levelno == logging.INFO
                           for r in caplog.records)
        finally:
            await hud.cerrar()

    correr(caso())


# ---------------------------------------------------------------------------
# Respaldo Puter (§C2.15): puter.estado, llm.pedir y llm.respuesta con un Amadeus Desktop falso
# ---------------------------------------------------------------------------

_SECRETO_PROMPT = "PROMPT-SECRETO-7f3a"   # nunca debe aparecer en los logs


class _EscritorioFalso:
    """Amadeus Desktop falso: cliente WS sin ``Origin`` que contesta los ``llm.pedir`` como Puter.js.

    ``responder(datos) -> dict | None`` construye los ``datos`` de ``llm.respuesta`` (sin ``id``);
    ``None`` = no contesta nunca (el pedido se queda en curso). ``retraso`` simula lo que tarda el
    modelo. ``resincronizar``: si no es ``None``, tras responder ``sin_sesion`` reenvía ese
    ``puter.estado`` justo detrás. ``limite`` (por defecto 2, ``None`` = sin límite): como el relevo
    real, con ``limite`` pedidos en curso el siguiente se responde ``rechazado`` al momento (en
    ``rechazados``), y un pedido deja de estar en curso justo ANTES de enviar su respuesta (como
    ``quitar(id)`` → ``responder`` en ``desktop/lib/puter.js``).
    """

    def __init__(self, ws: Any, responder: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                 *, retraso: float = 0.0, limite: int | None = MAX_PEDIDOS_PUTER) -> None:
        self.ws = ws
        self.responder = responder or (lambda d: {"ok": True, "texto": "[EXP:neutral] Hola.", "error": None,
                                                  "modelo": "openai/gpt-falso", "ms": 12})
        self.retraso = retraso
        self.limite = limite
        self.resincronizar: dict[str, Any] | None = None
        self.pedidos: list[dict[str, Any]] = []
        self.otros: list[dict[str, Any]] = []
        self.en_curso: set[str] = set()
        self.max_en_vuelo = 0
        self.rechazados: list[str] = []
        self._tareas: set[asyncio.Task[Any]] = set()
        self._bucle: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> _EscritorioFalso:
        await _recibir_hasta(self.ws, "hola")
        self._bucle = asyncio.create_task(self._leer())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._bucle is not None:
            self._bucle.cancel()
        for tarea in list(self._tareas):
            tarea.cancel()

    async def estado(self, *, listo: bool = True, sesion: bool = True, modelo: str | None = "openai/gpt-falso",
                     error: str | None = None) -> None:
        await self.ws.send(json.dumps({"tipo": "puter.estado", "datos": {
            "listo": listo, "sesion": sesion, "modelo": modelo, "error": error}}))

    async def _leer(self) -> None:
        try:
            async for crudo in self.ws:
                mensaje = json.loads(crudo)
                if mensaje["tipo"] == "llm.pedir":
                    datos = mensaje["datos"]
                    self.pedidos.append(datos)
                    if self.limite is not None and len(self.en_curso) >= self.limite:
                        self.rechazados.append(datos["id"])
                        await self.ws.send(json.dumps({"tipo": "llm.respuesta", "datos": {
                            "id": datos["id"], "ok": False, "texto": None, "error": "rechazado",
                            "modelo": None, "ms": 0}}))
                        continue
                    self.en_curso.add(datos["id"])
                    self.max_en_vuelo = max(self.max_en_vuelo, len(self.en_curso))
                    tarea = asyncio.create_task(self._contestar(datos))
                    self._tareas.add(tarea)
                    tarea.add_done_callback(self._tareas.discard)
                else:
                    self.otros.append(mensaje)
        except ConnectionClosed:
            pass

    async def _contestar(self, datos: dict[str, Any]) -> None:
        try:
            await asyncio.sleep(self.retraso)
            respuesta = self.responder(datos)
            if respuesta is not None:
                self.en_curso.discard(datos["id"])
                await self.ws.send(json.dumps({"tipo": "llm.respuesta", "datos": {"id": datos["id"], **respuesta}}))
                if respuesta.get("error") == "sin_sesion" and self.resincronizar is not None:
                    await self.ws.send(json.dumps({"tipo": "puter.estado", "datos": self.resincronizar}))
        except ConnectionClosed:
            pass


def _diags_puter(registro: _Registro) -> list[tuple[str, str]]:
    return [(d["estado"], d["detalle"]) for d in registro.de("sistema.modulo") if d.get("clave") == "puter"]


_MENSAJES = [{"rol": "usuario", "texto": f"hola Kurisu {_SECRETO_PROMPT}"}]


def test_puter_estado_disponible_y_diagnostico(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        assert hud.escritorio.puter is respaldo and respaldo.nombre == "puter"
        assert respaldo.disponible is False
        assert respaldo.diagnostico().detalle == "sin app de escritorio"
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(panel, "hola")
                async with _conectar(hud, token) as ws, _EscritorioFalso(ws) as app:
                    await _esperar(lambda: ("EN ESPERA", "sin sesión de Puter") in _diags_puter(registro))
                    # El chat del HUD no puede fingir la sesión de Puter.
                    await panel.send(json.dumps({"tipo": "puter.estado", "datos": {"listo": True, "sesion": True}}))
                    await asyncio.sleep(0.2)
                    assert respaldo.disponible is False
                    # Sesión iniciada pero puter.js aún cargando: en espera.
                    await app.estado(listo=False, sesion=True, error="carga")
                    await _esperar(lambda: respaldo.diagnostico().detalle == "Puter sin cargar (carga)")
                    assert respaldo.disponible is False
                    # Listo (con el sobre {tipo, datos}).
                    await app.estado()
                    await _esperar(lambda: respaldo.disponible)
                    diag = respaldo.diagnostico()
                    assert diag.clave == "puter" and diag.nombre == "Respaldo Puter (Amadeus Desktop)"
                    assert diag.estado is EstadoModulo.OK and diag.detalle == "openai/gpt-falso"
                    assert _diags_puter(registro)[-1] == ("OK", "openai/gpt-falso")
                    assert respaldo.estado()["conectado"] and respaldo.estado()["sesion"]
                    # Inválidos: se ignoran sin cambiar el estado.
                    for malo in ({"listo": "sí", "sesion": True}, {"listo": True}, {"sesion": 1, "listo": True}):
                        await ws.send(json.dumps({"tipo": "puter.estado", "datos": malo}))
                    await asyncio.sleep(0.2)
                    assert respaldo.disponible
                    # listo sin sesión nunca está listo.
                    await app.estado(listo=True, sesion=False)
                    await _esperar(lambda: not respaldo.disponible)
                    assert respaldo.diagnostico().detalle == "sin sesión de Puter"
                    await app.estado()
                    await _esperar(lambda: respaldo.disponible)
                # Al irse la app vuelve a "sin app de escritorio" y deja de estar disponible.
                await _esperar(lambda: _diags_puter(registro)[-1] == ("EN ESPERA", "sin app de escritorio"))
                assert respaldo.disponible is False and respaldo.estado()["conectado"] is False
            # Solo se publica al cambiar: nunca dos iguales seguidos.
            diags = _diags_puter(registro)
            assert all(a != b for a, b in zip(diags, diags[1:]))
        finally:
            await hud.cerrar()

    correr(caso())


def test_puter_completar_ida_y_vuelta_solo_a_la_app(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams")

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio, modelo="anthropic/claude-falso")
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(panel, "hola")
                async with _conectar(hud, token) as ws, _EscritorioFalso(ws) as app:
                    await app.estado()
                    await _esperar(lambda: respaldo.disponible)
                    mensajes = [
                        {"rol": "usuario", "texto": "primera"},
                        {"rol": "modelo", "texto": "[EXP:neutral] respuesta"},
                        {"role": "user", "content": "con claves al estilo OpenAI"},
                        {"rol": "sistema", "texto": "rol desconocido: fuera"},
                        {"rol": "usuario", "texto": "   "},
                        {"rol": "usuario", "texto": _MENSAJES[0]["texto"]},
                    ]
                    texto = await respaldo.completar(f"Eres Amadeus. {_SECRETO_PROMPT}", mensajes, modelo="",
                                                     max_tokens=1024, temperatura=0.8, timeout_s=5.0)
                    assert texto == "[EXP:neutral] Hola."
                    assert respaldo.ultimo_modelo == "openai/gpt-falso"
                    assert respaldo.estadisticas() == {"pedidos": 1, "respuestas": 1, "fallos": 0,
                                                       "ultimo_error": None}
                    (pedido,) = app.pedidos
                    assert set(pedido) == {"id", "modelo", "system", "mensajes", "max_tokens", "temperatura"}
                    assert len(pedido["id"]) == 24 and int(pedido["id"], 16) >= 0
                    assert pedido["modelo"] == "anthropic/claude-falso"     # PUTER_MODEL si modelo == ""
                    assert pedido["system"] == f"Eres Amadeus. {_SECRETO_PROMPT}"
                    assert pedido["mensajes"] == [
                        {"rol": "usuario", "texto": "primera"},
                        {"rol": "modelo", "texto": "[EXP:neutral] respuesta"},
                        {"rol": "usuario", "texto": "con claves al estilo OpenAI"},
                        {"rol": "usuario", "texto": _MENSAJES[0]["texto"]},
                    ]
                    assert pedido["max_tokens"] == 1024 and pedido["temperatura"] == 0.8
                    # Un modelo explícito gana a PUTER_MODEL.
                    assert await respaldo.completar("s", _MENSAJES, modelo="x-ai/grok-falso", max_tokens=99999,
                                                    temperatura=7.0, timeout_s=5.0)
                    assert app.pedidos[1]["modelo"] == "x-ai/grok-falso"
                    assert app.pedidos[1]["max_tokens"] == 8192 and app.pedidos[1]["temperatura"] == 2.0
                    # Sin mensajes válidos no se envía nada.
                    assert await respaldo.completar("s", [{"rol": "modelo", "texto": " "}], modelo="", max_tokens=10,
                                                    temperatura=1.0, timeout_s=1.0) is None
                    assert len(app.pedidos) == 2
                # El panel del HUD nunca ve los pedidos (llevan el system prompt).
                vistos = await _recoger(panel, 0.2)
                assert all(m["tipo"] != "llm.pedir" for m in vistos)
        finally:
            await hud.cerrar()
        assert _SECRETO_PROMPT not in caplog.text and "Hola." not in caplog.text

    correr(caso())


def test_puter_errores_del_escritorio(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    registro = _Registro(bus)
    reloj = [1000.0]

    def responder(datos: dict[str, Any]) -> dict[str, Any] | None:
        orden = datos["mensajes"][-1]["texto"]
        if orden in ERRORES_PUTER:
            return {"ok": False, "texto": None, "error": orden, "modelo": None, "ms": 5}
        if orden == "vacio":
            return {"ok": True, "texto": "   ", "error": None, "modelo": None, "ms": 5}
        if orden == "raro":
            return {"ok": False, "texto": "algo", "error": "codigo_inventado", "modelo": None, "ms": 5}
        return {"ok": True, "texto": "bien", "error": None, "modelo": None, "ms": 5}

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio, reloj=lambda: reloj[0])
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, responder) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)

                async def pedir(orden: str) -> str | None:
                    return await respaldo.completar("s", [{"rol": "usuario", "texto": orden}], modelo="",
                                                    max_tokens=100, temperatura=1.0, timeout_s=5.0)

                for orden in ("timeout", "red", "rechazado", "otro", "vacio", "raro"):
                    assert await pedir(orden) is None, orden
                    assert respaldo.disponible, orden            # estos errores no la apagan
                assert respaldo.ultimo_error == "otro"           # "codigo_inventado" → "otro"
                assert await pedir("normal") == "bien"
                # Sesión caducada: deja de estar disponible hasta el próximo puter.estado.
                assert await pedir("sin_sesion") is None
                assert respaldo.disponible is False
                assert respaldo.diagnostico().detalle == "sin sesión de Puter"
                assert await pedir("normal") is None and len(app.pedidos) == 8  # ni lo intenta
                await app.estado()
                await _esperar(lambda: respaldo.disponible)
                # Saldo de Puter agotado: enfriamiento de 10 min (DEGRADADO) y vuelta sola.
                assert await pedir("cuota") is None
                assert respaldo.disponible is False
                diag = respaldo.diagnostico()
                pausa = "saldo de Puter agotado · pausa de 10 min"
                assert diag.estado is EstadoModulo.DEGRADADO and diag.detalle == pausa
                assert _diags_puter(registro)[-1] == ("DEGRADADO", pausa)
                # A los 9 min el detalle sigue valiendo (el HUD y /estado muestran el último
                # publicado: nada de «reintento en 10 min» congelado); la cuenta atrás va en estado().
                reloj[0] += 540.0
                assert respaldo.diagnostico().detalle == pausa and not respaldo.disponible
                assert respaldo.estado()["enfriamiento_s"] == 60.0 and respaldo.estado()["listo"]
                reloj[0] += 61.0
                assert respaldo.disponible
                respaldo._fin_enfriamiento()                     # lo que hace el temporizador
                assert _diags_puter(registro)[-1] == ("OK", "openai/gpt-falso")
                assert respaldo.fallos == 8 and respaldo.respuestas == 1
                # sin_sesion seguido del puter.estado real (el relevo resincroniza): gana el estado
                # que llega DESPUÉS aunque completar() se reanude más tarde (nada de quedarse
                # "sin sesión" para siempre: la app no vuelve a mandar un estado que no cambió).
                app.resincronizar = {"listo": True, "sesion": True, "modelo": "openai/gpt-falso", "error": None}
                assert await pedir("sin_sesion") is None
                await _esperar(lambda: respaldo.disponible)
                await asyncio.sleep(0.1)
                assert respaldo.disponible and respaldo.ultimo_error == "sin_sesion"
                assert await pedir("normal") == "bien"
                # Con la sesión de verdad caída, el estado reenviado la deja en espera.
                app.resincronizar = {"listo": False, "sesion": False, "modelo": "openai/gpt-falso", "error": None}
                assert await pedir("sin_sesion") is None
                await asyncio.sleep(0.1)
                assert respaldo.disponible is False
                assert respaldo.diagnostico().detalle == "sin sesión de Puter"
        finally:
            await hud.cerrar()

    correr(caso())


def test_puter_timeout_respuesta_tardia_y_desconexion(settings_prueba: Settings, bus: BusEventos,
                                                      correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            # 1) El escritorio tarda más que el plazo: None a tiempo y la respuesta tardía se ignora.
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, retraso=0.5) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)
                inicio = time.monotonic()
                assert await respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10, temperatura=1.0,
                                                timeout_s=0.2) is None
                assert time.monotonic() - inicio < 0.45
                assert respaldo.ultimo_error == "timeout" and respaldo.estado()["pendientes"] == 0
                assert respaldo.estado()["en_curso"] == 1        # la app aún lo tiene entre manos
                await asyncio.sleep(0.5)                        # llega la tardía: solo suelta el turno
                assert respaldo.disponible and len(app.pedidos) == 1
                assert respaldo.estado()["en_curso"] == 0 and respaldo.respuestas == 0
                # Un plazo enorme se acota a 40 s (no se comprueba esperando: solo que responde).
                app.retraso = 0.0
                assert await respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10, temperatura=1.0,
                                                timeout_s=10_000.0) == "[EXP:neutral] Hola."
            # 2) La app se va con un pedido en vuelo: None enseguida (sin esperar al plazo).
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, lambda d: None) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)
                tarea = asyncio.create_task(respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10,
                                                               temperatura=1.0, timeout_s=10.0))
                await _esperar(lambda: len(app.pedidos) == 1)
                inicio = time.monotonic()
                await ws.close()
                assert await asyncio.wait_for(tarea, 3.0) is None
                assert time.monotonic() - inicio < 2.0
                assert respaldo.ultimo_error == "desconectado" and not respaldo.disponible
                assert respaldo.estado()["en_curso"] == 0         # la app se fue: su turno queda libre
        finally:
            await hud.cerrar()

    correr(caso())


def test_puter_app_reemplazada_y_cierre_del_enlace(settings_prueba: Settings, bus: BusEventos,
                                                   correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        cerrado = False
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as ws1, _EscritorioFalso(ws1, lambda d: None) as primera:
                await primera.estado()
                await _esperar(lambda: respaldo.disponible)
                tarea = asyncio.create_task(respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10,
                                                               temperatura=1.0, timeout_s=10.0))
                await _esperar(lambda: len(primera.pedidos) == 1)
                # Otra instancia de Amadeus Desktop reemplaza a la primera: el pedido viejo → None y la
                # nueva no está lista hasta que mande su puter.estado.
                async with _conectar(hud, token) as ws2, _EscritorioFalso(ws2) as segunda:
                    assert await asyncio.wait_for(tarea, 3.0) is None
                    assert respaldo.disponible is False
                    # Una respuesta de la conexión vieja ya no cuenta (ni su estado).
                    try:
                        await ws1.send(json.dumps({"tipo": "puter.estado", "datos": {"listo": True, "sesion": True}}))
                    except ConnectionClosed:
                        pass
                    await asyncio.sleep(0.2)
                    assert respaldo.disponible is False
                    await segunda.estado()
                    await _esperar(lambda: respaldo.disponible)
                    segunda.responder = lambda d: None
                    tarea = asyncio.create_task(respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10,
                                                                   temperatura=1.0, timeout_s=10.0))
                    await _esperar(lambda: len(segunda.pedidos) == 1)
                    # Cerrar R.A.M.S. con un pedido en vuelo: None, sin colgarse.
                    await hud.cerrar()
                    cerrado = True
                    assert await asyncio.wait_for(tarea, 3.0) is None
                    assert respaldo.disponible is False
        finally:
            if not cerrado:
                await hud.cerrar()

    correr(caso())


def test_puter_como_mucho_dos_pedidos_a_la_vez(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            eco = lambda d: {"ok": True, "texto": d["mensajes"][-1]["texto"], "error": None,  # noqa: E731
                             "modelo": None, "ms": 1}
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, eco, retraso=0.15) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)
                textos = [f"pedido {i}" for i in range(5)]
                resultados = await asyncio.gather(*(
                    respaldo.completar("s", [{"rol": "usuario", "texto": t}], modelo="", max_tokens=10,
                                       temperatura=1.0, timeout_s=5.0) for t in textos))
                assert resultados == textos                       # cada respuesta va a su pedido
                assert app.max_en_vuelo == MAX_PEDIDOS_PUTER == 2
                assert len({p["id"] for p in app.pedidos}) == 5
                # Con el turno ocupado más allá del plazo: None ("ocupado") sin enviar nada.
                app.retraso = 1.0
                lentos = [asyncio.create_task(respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10,
                                                                 temperatura=1.0, timeout_s=5.0)) for _ in range(2)]
                await _esperar(lambda: len(app.pedidos) == 7)
                assert await respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10, temperatura=1.0,
                                                timeout_s=0.2) is None
                assert respaldo.ultimo_error == "ocupado" and len(app.pedidos) == 7
                assert await asyncio.gather(*lentos) == [_MENSAJES[0]["texto"]] * 2
                assert app.rechazados == []                       # nunca un 3.º en curso en la app
        finally:
            await hud.cerrar()

    correr(caso())


def test_puter_turno_ocupado_hasta_la_respuesta_del_escritorio(settings_prueba: Settings, bus: BusEventos,
                                                               correr: Any) -> None:
    """Un pedido que R.A.M.S. da por perdido (venció su plazo) sigue en curso en la app: su turno no
    se suelta hasta su ``llm.respuesta``, así que la app nunca recibe un 3.º en curso (lo rechazaría
    y Broca lo contaría como fallo del respaldo). Y si tras esperar turno queda poco plazo, el pedido
    no se envía (Puter no llegaría a tiempo y se cobraría igual)."""
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            eco = lambda d: {"ok": True, "texto": d["mensajes"][-1]["texto"], "error": None,  # noqa: E731
                             "modelo": None, "ms": 1}
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, eco, retraso=0.6) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)

                def pedir(texto: str, plazo: float) -> Any:
                    return respaldo.completar("s", [{"rol": "usuario", "texto": texto}], modelo="",
                                              max_tokens=10, temperatura=1.0, timeout_s=plazo)

                # Dos pedidos vencen a los 0,3 s mientras la app tarda 0,6 s: siguen en curso en ella.
                assert await asyncio.gather(pedir("a", 0.3), pedir("b", 0.3)) == [None, None]
                assert respaldo.estado()["en_curso"] == 2 and respaldo.estado()["pendientes"] == 0
                assert respaldo.ultimo_error == "timeout" and respaldo.fallos == 2
                # Otros dos enseguida: esperan a las respuestas tardías en vez de salir y ser rechazados.
                assert await asyncio.gather(pedir("c", 3.0), pedir("d", 3.0)) == ["c", "d"]
                assert app.rechazados == [] and app.max_en_vuelo == 2 and len(app.pedidos) == 4
                assert respaldo.estado()["en_curso"] == 0 and respaldo.fallos == 2
                assert respaldo.respuestas == 2                   # las tardías no cuentan como respuesta
                # Tres a la vez con 1 s: el 3.º espera turno 0,6 s; con 0,4 s ya no le compensa → no sale.
                assert await asyncio.gather(pedir("e", 1.0), pedir("f", 1.0), pedir("g", 1.0)) == ["e", "f", None]
                assert len(app.pedidos) == 6 and app.rechazados == [] and respaldo.fallos == 3
                assert respaldo._semaforo is not None and respaldo._semaforo._value == MAX_PEDIDOS_PUTER
        finally:
            await hud.cerrar()

    correr(caso())


def test_puter_turno_sin_respuesta_se_suelta_al_tope_y_tardia_con_cuota(settings_prueba: Settings,
                                                                        bus: BusEventos, correr: Any,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Si la ``llm.respuesta`` no llega nunca (la app garantiza ≤ 45 s), el turno se suelta a los
    ``HUECO_PUTER_MAX_S`` del envío, una sola vez. Una respuesta tardía con ``cuota`` aplica su pausa."""
    monkeypatch.setattr(escritorio_link, "HUECO_PUTER_MAX_S", 0.5)
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]

            def pedir(plazo: float) -> Any:
                return respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10, temperatura=1.0,
                                          timeout_s=plazo)

            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, lambda d: None, limite=None) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)
                assert await asyncio.gather(pedir(0.1), pedir(0.1)) == [None, None]
                assert respaldo.estado()["en_curso"] == 2
                # Los dos turnos siguen ocupados: el siguiente espera y ni se envía.
                assert await pedir(0.2) is None
                assert respaldo.ultimo_error == "ocupado" and len(app.pedidos) == 2
                # Al tope se sueltan solos; una respuesta posterior a uno de ellos ya no suelta nada.
                await _esperar(lambda: respaldo.estado()["en_curso"] == 0, 2.0)
                await ws.send(json.dumps({"tipo": "llm.respuesta", "datos": {
                    "id": app.pedidos[0]["id"], "ok": False, "texto": None, "error": "cuota", "modelo": None,
                    "ms": 1}}))
                await asyncio.sleep(0.2)
                assert respaldo.disponible                          # ya no es un pedido en curso: sin efecto
                assert respaldo._semaforo is not None and respaldo._semaforo._value == MAX_PEDIDOS_PUTER
                # Tardía con "cuota" (saldo agotado): suelta el turno y pone la pausa igual.
                app.responder = lambda d: {"ok": False, "texto": None, "error": "cuota", "modelo": None, "ms": 5}
                app.retraso = 0.3
                assert await pedir(0.1) is None and respaldo.ultimo_error == "timeout"
                await _esperar(lambda: not respaldo.disponible, 2.0)
                assert respaldo.estado()["en_curso"] == 0 and respaldo.estado()["enfriamiento_s"] > 590
                assert _diags_puter(registro)[-1] == ("DEGRADADO", "saldo de Puter agotado · pausa de 10 min")
        finally:
            await hud.cerrar()

    correr(caso())


class _EnlaceConectado:
    """Lo mínimo de :class:`EnlaceEscritorio` para :class:`RespaldoPuter`: activo y con la app conectada."""

    activo = True
    overlay_conectado = True

    def __init__(self, bus: BusEventos) -> None:
        self._bus = bus
        self.puter: Any = None

    def vincular_puter(self, respaldo: Any) -> None:
        self.puter = respaldo


def test_puter_diagnostico_explica_el_error_del_escritorio(bus: BusEventos) -> None:
    """Con ``listo: false`` el código de ``error`` manda sobre ``sesion``: con el relevo real
    (``desktop/lib/puter.js``), sin puter.js cargado ``sesion`` es siempre false, y «sin sesión de
    Puter» mandaría al usuario a iniciar una sesión que no arregla nada."""
    registro = _Registro(bus)
    respaldo = RespaldoPuter(_EnlaceConectado(bus))  # type: ignore[arg-type]
    sanear = escritorio_link._estado_puter_saneado
    casos = [
        ({"listo": False, "sesion": False, "modelo": None, "error": "deshabilitado"},
         "EN ESPERA", "Puter desactivado en Amadeus Desktop (AMADEUS_PUTER=0)"),
        ({"listo": False, "sesion": False, "modelo": "google/x", "error": "carga_fallida"},
         "EN ESPERA", "sin conexión con puter.com"),
        ({"listo": False, "sesion": False, "modelo": "google/x", "error": "ventana_caida"},
         "EN ESPERA", "ventana de Puter caída (se recarga sola)"),
        ({"listo": False, "sesion": False, "modelo": "google/x", "error": None}, "EN ESPERA", "sin sesión de Puter"),
        ({"listo": False, "sesion": False, "modelo": "google/x", "error": "codigo_nuevo"},
         "EN ESPERA", "sin sesión de Puter"),
        ({"listo": False, "sesion": True, "modelo": "google/x", "error": None}, "EN ESPERA", "Puter sin cargar"),
        ({"listo": True, "sesion": True, "modelo": "google/x", "error": "modelo_desconocido"},
         "OK", "google/x · no aparece en el listado de Puter"),
        ({"listo": True, "sesion": True, "modelo": "google/x", "error": None}, "OK", "google/x"),
        ({"listo": True, "sesion": True, "modelo": None, "error": None}, "OK", "modelo por defecto de Amadeus Desktop"),
    ]
    for datos, estado, detalle in casos:
        saneado = sanear(datos)
        assert saneado is not None
        respaldo._al_estado(saneado)
        diag = respaldo.diagnostico()
        assert (diag.estado.value, diag.detalle) == (estado, detalle), datos
        assert _diags_puter(registro)[-1] == (estado, detalle), datos      # y es lo publicado
        assert respaldo.disponible is (estado == "OK"), datos
    # Con PUTER_MODEL el aviso no aplica: es sobre el modelo por defecto de la app, que no se usa.
    propio = RespaldoPuter(_EnlaceConectado(bus), modelo="anthropic/claude-x")  # type: ignore[arg-type]
    propio._al_estado({"listo": True, "sesion": True, "modelo": "google/x", "error": "modelo_desconocido"})
    assert propio.diagnostico().detalle == "anthropic/claude-x"


def test_puter_estado_sobre_el_cupo_gana_el_ultimo(settings_prueba: Settings, bus: BusEventos,
                                                  correr: Any) -> None:
    """Más ``puter.estado`` de los que caben en su ventana: el último no se pierde (se aplica al
    vencerla), porque la app solo lo reenvía cuando cambia."""
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws) as app:
                for _ in range(escritorio_link.MAX_ESTADO_CLIENTE_S + 5):
                    await app.estado(listo=False, sesion=True, error="carga")
                await app.estado(listo=True, sesion=True, modelo="google/gemini-falso")
                await _esperar(lambda: respaldo.disponible, 3.0)
                assert hud.escritorio.descartados > 0
                assert respaldo.diagnostico().detalle == "google/gemini-falso"
        finally:
            await hud.cerrar()

    correr(caso())


def test_llm_respuesta_pendiente_nunca_se_pierde_por_ritmo(settings_prueba: Settings, bus: BusEventos,
                                                           correr: Any) -> None:
    """Una ráfaga de ``llm.respuesta`` basura gasta el cupo, pero la que responde a un pedido
    pendiente pasa siempre (perderla dejaría a Broca esperando hasta 40 s)."""
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as ws, _EscritorioFalso(ws, lambda d: None) as app:
                await app.estado()
                await _esperar(lambda: respaldo.disponible)
                tarea = asyncio.create_task(respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10,
                                                               temperatura=1.0, timeout_s=5.0))
                await _esperar(lambda: len(app.pedidos) == 1)
                for i in range(3 * escritorio_link.MAX_LLM_CLIENTE_S):
                    await ws.send(json.dumps({"tipo": "llm.respuesta", "datos": {
                        "id": f"{i:024x}", "ok": True, "texto": "basura", "error": None, "modelo": None, "ms": 1}}))
                await ws.send(json.dumps({"tipo": "llm.respuesta", "datos": {
                    "id": app.pedidos[0]["id"], "ok": True, "texto": "la buena", "error": None, "modelo": None,
                    "ms": 1}}))
                assert await asyncio.wait_for(tarea, 3.0) == "la buena"
                assert hud.escritorio.descartados > 0               # la basura sí agotó el cupo
        finally:
            await hud.cerrar()

    correr(caso())


def test_llm_respuesta_hasta_64_kib_solo_desde_la_app(settings_prueba: Settings, bus: BusEventos,
                                                      correr: Any) -> None:
    registro = _Registro(bus)
    largo = "ñ" * 9000 + " fin"                                  # ~18 KiB en UTF-8 (> 8 KiB)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        assert hud.escritorio is not None
        respaldo = RespaldoPuter(hud.escritorio)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            responder = lambda d: {"ok": True, "texto": largo, "error": None, "modelo": None, "ms": 3}  # noqa: E731
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(panel, "hola")
                async with _conectar(hud, token) as ws, _EscritorioFalso(ws, responder) as app:
                    await app.estado()
                    await _esperar(lambda: respaldo.disponible)
                    assert await respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10, temperatura=1.0,
                                                    timeout_s=5.0) == largo
                    # El resto de tipos sigue limitado a 8 KiB también para la app.
                    await ws.send(json.dumps({"tipo": "chat", "texto": "hola", "relleno": "z" * 9000}))
                    # El chat del HUD no puede responder pedidos (ni con un id real) ni pasar de 8 KiB.
                    app.responder = lambda d: None
                    tarea = asyncio.create_task(respaldo.completar("s", _MENSAJES, modelo="", max_tokens=10,
                                                                   temperatura=1.0, timeout_s=0.6))
                    await _esperar(lambda: len(app.pedidos) == 2)
                    ident = app.pedidos[1]["id"]
                    await panel.send(json.dumps({"tipo": "llm.respuesta", "datos": {
                        "id": ident, "ok": True, "texto": "suplantado", "error": None, "modelo": None, "ms": 1}}))
                    # Ids inválidos o ajenos desde la app: se ignoran.
                    for malo in ("XYZ", "0" * 8, "f" * 40, "a" * 24):
                        await ws.send(json.dumps({"tipo": "llm.respuesta", "datos": {
                            "id": malo, "ok": True, "texto": "ajeno", "error": None, "modelo": None, "ms": 1}}))
                    assert await tarea is None
                    assert respaldo.ultimo_error == "timeout"
                    await asyncio.sleep(0.1)
                    assert registro.de("escritorio.chat") == []
                    # La conexión sigue viva.
                    bus.publicar("escritorio.sistema", {"texto": "vivo"})
                    assert (await _recibir_hasta(panel, "sistema"))[0] == {"texto": "vivo"}
        finally:
            await hud.cerrar()

    correr(caso())


def test_pedido_puter_saneado_limites() -> None:
    sanear = escritorio_link._pedido_puter_saneado
    mensajes = [{"rol": "usuario" if i % 2 == 0 else "modelo", "texto": f"m{i}"} for i in range(45)]
    datos = sanear("S" * (MAX_SYSTEM_PUTER + 500), mensajes, modelo="", max_tokens=0, temperatura=float("nan"))
    assert datos is not None
    assert len(datos["system"]) == MAX_SYSTEM_PUTER
    assert len(datos["mensajes"]) <= MAX_MENSAJES_PUTER
    assert datos["mensajes"][-1] == {"rol": "usuario", "texto": "m44"}
    assert datos["mensajes"][0]["rol"] == "usuario"              # tras recortar, empieza por el usuario
    assert datos["max_tokens"] == 1024 and datos["temperatura"] == 1.0
    # Textos largos se recortan; los más antiguos salen si el JSON no cabe (el último siempre queda).
    enormes = [{"rol": "usuario", "texto": "x" * 50_000} for _ in range(MAX_MENSAJES_PUTER)]
    datos = sanear("s", enormes, modelo="m", max_tokens=5, temperatura=0.5)
    assert datos is not None
    assert all(len(m["texto"]) == MAX_TEXTO_MENSAJE_PUTER for m in datos["mensajes"])
    tamano = len(json.dumps({"tipo": "llm.pedir", "datos": datos}, ensure_ascii=False).encode("utf-8"))
    assert tamano <= escritorio_link.MAX_BYTES_PEDIDO_PUTER and len(datos["mensajes"]) >= 1
    # Basura: nada que pedir.
    assert sanear("s", "no es una lista", modelo="", max_tokens=1, temperatura=1) is None
    assert sanear("s", [None, 3, {"rol": "usuario", "texto": 5}], modelo="", max_tokens=1, temperatura=1) is None


def _largo_js(texto: str) -> int:
    """``String.length`` de JavaScript (unidades UTF-16)."""
    return len(texto.encode("utf-16-le")) // 2


def test_pedido_puter_limites_contados_como_en_javascript() -> None:
    """Amadeus Desktop valida en JS (un emoji mide 2): R.A.M.S. nunca le manda algo que rechazaría."""
    sanear = escritorio_link._pedido_puter_saneado
    emojis = [{"rol": "usuario", "texto": "😀" * 15_000}, {"rol": "modelo", "texto": "ñ" * 25_000},
              {"rol": "usuario", "texto": "a" + "😀" * 10_000}]
    datos = sanear("😀" * 40_000, emojis, modelo="", max_tokens=10, temperatura=1.0)
    assert datos is not None
    assert _largo_js(datos["system"]) <= MAX_SYSTEM_PUTER and datos["system"] == "😀" * (MAX_SYSTEM_PUTER // 2)
    for mensaje in datos["mensajes"]:
        assert _largo_js(mensaje["texto"]) <= MAX_TEXTO_MENSAJE_PUTER
    assert datos["mensajes"][0]["texto"].endswith("…") and "�" not in datos["mensajes"][0]["texto"]
    assert datos["mensajes"][1]["texto"] == "ñ" * (MAX_TEXTO_MENSAJE_PUTER - 1) + "…"
    # Justo en el límite (un emoji de más ya no cabe): sin recortar.
    exacto = "ab" + "😀" * ((MAX_TEXTO_MENSAJE_PUTER - 2) // 2)
    assert _largo_js(exacto) == MAX_TEXTO_MENSAJE_PUTER
    assert escritorio_link._recortar_utf16(exacto, MAX_TEXTO_MENSAJE_PUTER, "…") == exacto
    assert escritorio_link._recortar_utf16("😀😀", 3) == "😀"                # nunca parte un emoji


def test_pedido_puter_recorte_por_tamano_exacto_y_barato() -> None:
    """El recorte por tamaño deja el máximo de mensajes que cabe (contando los escapes de JSON)."""
    sanear = escritorio_link._pedido_puter_saneado
    tope = escritorio_link.MAX_BYTES_PEDIDO_PUTER - 64
    # Caracteres de control: en JSON ocupan 6 bytes cada uno (\u0001), no 1.
    mensajes = [{"rol": "usuario" if i % 2 == 0 else "modelo", "texto": f"{i}:" + "\x01" * 19_000}
                for i in range(MAX_MENSAJES_PUTER - 1)]
    inicio = time.perf_counter()
    datos = sanear("s" * 1000, mensajes, modelo="google/gemini-falso", max_tokens=10, temperatura=1.0)
    assert time.perf_counter() - inicio < 0.5
    assert datos is not None

    def tamano(lista: list[dict[str, str]]) -> int:
        return len(escritorio_link._serializar("llm.pedir", dict(datos, mensajes=lista)).encode("utf-8"))

    assert tamano(datos["mensajes"]) <= tope
    assert datos["mensajes"][-1]["texto"].startswith(f"{MAX_MENSAJES_PUTER - 2}:")
    assert datos["mensajes"][0]["rol"] == "usuario"
    # Con un mensaje más (el anterior del usuario, dos atrás) ya no cabría.
    primero = int(datos["mensajes"][0]["texto"].split(":", 1)[0])
    anteriores = [m for m in mensajes if int(m["texto"].split(":", 1)[0]) >= primero - 2]
    assert tamano(anteriores) > tope


def test_modelo_puter_coherente_con_config_y_con_amadeus_desktop() -> None:
    """Lo que acepta el enlace = lo que acepta ``PUTER_MODEL`` en ``core/config.py`` = lo que acepta
    Amadeus Desktop (``RE_MODELO`` de ``desktop/lib/puter.js``), en los dos sentidos: nunca un
    ``rechazado`` por el nombre, ni un modelo que la app usa y anuncia pero R.A.M.S. lee como ``None``."""
    import re

    from core import config

    valido = escritorio_link._modelo_puter
    muestras = ["google/gemini-3.5-flash-lite", "anthropic/claude-haiku-5-5", "openrouter:meta/llama",
                "_x", "/abs", "-x", ".x", "a" * 100, "a" * 101, "a" * 120, "a" * 121, "", "con espacio",
                "<script>", "ñandú", "x\n", "modelo\x00"]
    for bueno in ("google/gemini-3.5-flash-lite", "openrouter:meta/llama", "a" * 100):
        assert valido(bueno) == bueno
    for malo in ("", "_x", "/abs", "a" * 101, "con espacio", "<script>", "ñandú", "x\n", None, 5):
        assert valido(malo) is None
    patron_config = getattr(config, "_RE_MODELO_PUTER", None)
    if patron_config is not None:
        for muestra in muestras:
            assert bool(patron_config.fullmatch(muestra)) == (valido(muestra) is not None), muestra
    sitio = Path(__file__).resolve().parents[1] / "desktop" / "lib" / "puter.js"
    if not sitio.is_file():
        pytest.skip("desktop/lib/puter.js aún no existe (frente desktop)")
    hallado = re.search(r"const RE_MODELO = /\^(.+?)\$/;", sitio.read_text(encoding="utf-8"))
    assert hallado, "desktop/lib/puter.js sin RE_MODELO"
    patron_js = re.compile(hallado.group(1))   # clase de caracteres simple: misma sintaxis en JS y Python
    for muestra in muestras:                       # mismo patrón en los dos sentidos
        assert bool(patron_js.fullmatch(muestra)) == (valido(muestra) is not None), muestra


def test_respuesta_y_estado_saneados() -> None:
    respuesta = escritorio_link._respuesta_llm_saneada
    base = {"id": "ab" * 12, "ok": True, "texto": "hola\x00", "error": "red", "modelo": "openai/gpt-x", "ms": 9}
    assert respuesta(base) == {"id": "ab" * 12, "ok": True, "texto": "hola", "error": None,
                               "modelo": "openai/gpt-x", "ms": 9}
    assert respuesta(dict(base, id="AB" * 12)) is None           # solo hex en minúsculas
    assert respuesta(dict(base, ok="true"))["ok"] is False       # ok debe ser booleano
    assert respuesta(dict(base, ok=False, error="cuota"))["error"] == "cuota"
    assert respuesta(dict(base, ok=False, error=None))["error"] == "otro"
    assert respuesta(dict(base, modelo="<script>", ms=-1)) == dict(respuesta(base), modelo=None, ms=None)
    largo = respuesta(dict(base, texto="z" * 50_000))
    assert len(largo["texto"]) == escritorio_link.MAX_TEXTO_RESPUESTA_LLM
    estado = escritorio_link._estado_puter_saneado
    assert estado({"listo": True, "sesion": True, "modelo": "a/b", "error": None}) == {
        "listo": True, "sesion": True, "modelo": "a/b", "error": None}
    assert estado({"listo": True, "sesion": False})["listo"] is False
    assert estado({"listo": 1, "sesion": True}) is None
    assert estado({"listo": False, "sesion": True, "error": "Error: <b>raro</b>"})["error"] is None


def test_respaldo_sin_enlace_activo_y_desvincular(settings_prueba: Settings, bus: BusEventos,
                                                  correr: Any) -> None:
    registro = _Registro(bus)

    async def caso() -> None:
        enlace = EnlaceEscritorio(settings_prueba, bus, evitar_token="x")
        respaldo = RespaldoPuter(enlace)
        assert respaldo.disponible is False
        assert await respaldo.completar("s", _MENSAJES, modelo="", max_tokens=1, temperatura=1.0,
                                        timeout_s=1.0) is None
        assert respaldo.pedidos == 0
        # Un estado "listo" sin app conectada no basta.
        respaldo._al_estado({"listo": True, "sesion": True, "modelo": None, "error": None})
        assert respaldo.disponible is False
        assert respaldo.diagnostico().detalle == "sin app de escritorio"
        # PUTER_MODEL inválido: se ignora (modelo del escritorio).
        otro = RespaldoPuter(enlace, modelo="modelo con espacios")
        assert otro.modelo == "" and enlace.puter is otro
        otro.desvincular()
        assert enlace.puter is None and otro.disponible is False
        await enlace.cerrar()

    correr(caso())
    assert all(d.get("clave") != "puter" or d["estado"] in ("EN ESPERA", "OFF") for d in registro.de("sistema.modulo"))


def test_respaldo_cumple_el_protocolo_de_broca(settings_prueba: Settings, bus: BusEventos) -> None:
    broca_llm = pytest.importorskip("lobes.broca_llm")
    protocolo = getattr(broca_llm, "ProveedorRespaldo", None)
    if protocolo is None:
        pytest.skip("lobes/broca_llm.py aún sin ProveedorRespaldo (frente broca)")
    respaldo = RespaldoPuter(EnlaceEscritorio(settings_prueba, bus, evitar_token="x"))
    assert isinstance(respaldo, protocolo)
    assert inspect.iscoroutinefunction(respaldo.completar)
    parametros = inspect.signature(respaldo.completar).parameters
    for nombre in ("modelo", "max_tokens", "temperatura", "timeout_s"):
        assert parametros[nombre].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# Tarjetas aprobables (v0.2.2, docs/V022_CORREO_DRIVE_SCRIPTS.md §12 y §13.6)
# ---------------------------------------------------------------------------

_HUELLA = "ab" * 32
_MARCA = "CONTENIDO-PRIVADO-91c2"   # cuerpo, código, nombres y campos: nunca en los logs
_LIMITE_HUD = 65536                 # lo que aceptan el chat RINE, Electron y el puente de movil/


def _seccion(clase: str) -> dict[str, Any]:
    """Sección de contenido mínima y válida de cada clase (lo que devolvería ``vista()``)."""
    if clase == "correo":
        return {"para": ["ana@example.com"], "cc": [], "cco": [], "asunto": f"Informe {_MARCA}",
                "cuerpo": f"Hola Ana,\n\tadjunto el informe. {_MARCA}",
                "adjuntos": [{"nombre": f"informe-{_MARCA}.pdf", "bytes": 2048, "carpeta": "documentos"}],
                "problemas": [], "avisos": [], "listo": True}
    if clase == "drive":
        return {"op": "subir", "paso": 1, "archivo": None,
                "local": {"nombre": f"plan-{_MARCA}.xlsx", "bytes": 2_100_000, "carpeta": "documentos"},
                "carpeta_destino": "Trabajo", "compartir": None, "problemas": [], "listo": True}
    if clase == "script":
        return {"lenguaje": "powershell", "plataforma": "windows",
                "codigo": f"# {_MARCA}\nGet-ChildItem $env:USERPROFILE | Measure-Object\n",
                "explicacion": "Cuenta los archivos de tu carpeta.", "efectos": ["solo lee"], "requisitos": [],
                "nivel": "bajo", "ejecutable": True, "motivo_no_ejecutable": "", "metodo": "ast",
                "hallazgos": [], "hallazgos_omitidos": 0, "habilidad": None, "habilidad_cambiada": False,
                "ejecuciones": 0}
    return {"script_id": "0a1b2c3d", "lenguaje": "powershell", "codigo_salida": 0, "tiempo_agotado": False,
            "duracion_s": 1.25, "stdout": f"42 {_MARCA}\n", "stderr": "", "recortada": False,
            "carpeta": "taller/0a1b2c3d"}


_BOTONES_CLASE = {"correo": ["aprobar", "editar", "descartar"], "drive": ["aprobar", "descartar"],
                  "script": ["aprobar", "guardar", "copiar", "descartar"], "salida": ["descartar"]}


def _tarjeta(clase: str = "correo", *, ident: str = "0a1b2c3d", version: int = 1,
             seccion: dict[str, Any] | None = None, **cambios: Any) -> dict[str, Any]:
    """Payload de ``tarjeta.mostrar`` con el esquema de §12.2 (como lo publicaría main)."""
    tarjeta: dict[str, Any] = {
        "id": ident, "clase": clase, "version": version, "sha256": _HUELLA,
        "titulo": f"Tarjeta de {clase}", "resumen": "Revísala antes de aprobar", "aviso": "",
        "botones": list(_BOTONES_CLASE[clase]), "caduca_ts": 1_800_000_000.0, "canal_origen": "escritorio",
        "correo": None, "drive": None, "script": None, "salida": None,
    }
    tarjeta[clase] = seccion if seccion is not None else _seccion(clase)
    tarjeta.update(cambios)
    return tarjeta


def _mensaje_tarjeta(tarjeta: dict[str, Any]) -> int:
    """Bytes del mensaje WS ``tarjeta`` tal como lo serializa escritorio_link."""
    return len(json.dumps({"tipo": "tarjeta", "datos": tarjeta}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


_COMILLA_Y_BARRA = '"' + chr(92)   # cada uno ocupa 2 bytes al escaparse en JSON


def _script_peor_caso() -> tuple[dict[str, Any], str]:
    """Script al máximo: 24 576 bytes de comillas y barras, 20 hallazgos de 160 unidades no ASCII y
    efectos, requisitos y textos comunes al máximo."""
    codigo = _COMILLA_Y_BARRA * (escritorio_link.MAX_CODIGO_SCRIPT // 2)
    seccion = dict(_seccion("script"), codigo=codigo, nivel="alto", explicacion="€" * 600,
                   efectos=["€" * 160] * 10, requisitos=["€" * 80] * 10, motivo_no_ejecutable="",
                   hallazgos=[{"nivel": nivel, "categoria": "borrado", "linea": i, "detalle": "€" * 160}
                              for i, nivel in enumerate(["bajo", "medio", "alto", "alto"] * 5)],
                   hallazgos_omitidos=3, habilidad="limpiar descargas")
    return _tarjeta("script", ident="5c1a7e00", seccion=seccion, titulo="é" * 120, resumen="é" * 300,
                    aviso="é" * 300), codigo


def _correo_peor_caso() -> dict[str, Any]:
    """Correo al máximo de §12.2: cuerpo de 10 000 «€», 50 direcciones de 254, todo lo demás al tope."""
    direccion = "a" * 64 + "@" + "b" * 185 + ".com"
    assert len(direccion) == 254
    seccion = {"para": [direccion] * 30, "cc": [direccion] * 10, "cco": [direccion] * 10,
               "asunto": "€" * 200, "cuerpo": "€" * 10_000,
               "adjuntos": [{"nombre": "€" * 255, "bytes": 5_000_000, "carpeta": "documentos"}] * 5,
               "problemas": ["ñ" * 160] * 10, "avisos": ["ñ" * 160] * 5, "listo": False}
    return _tarjeta("correo", ident="c0a1b2c3", seccion=seccion, titulo="é" * 120, resumen="é" * 300,
                    aviso="é" * 300)


def _salida_peor_caso() -> dict[str, Any]:
    seccion = dict(_seccion("salida"), stdout="€" * 8192, stderr="€" * 8192, recortada=True, carpeta="é" * 160)
    return _tarjeta("salida", ident="5a11da00", seccion=seccion, titulo="é" * 120, resumen="é" * 300,
                    aviso="é" * 300)


def test_tarjeta_saneada_esquema_exacto() -> None:
    sanear = escritorio_link._tarjeta_saneada
    cruda = _tarjeta("correo", extra="x", titulo="T" * 130 + "\x1b[31m", drive={"op": "subir"},
                     botones=["aprobar", "copiar", "aprobar", "volar", "descartar", "editar"],
                     canal_origen="nexus", caduca_ts=float("nan"))
    cruda["correo"] = dict(_seccion("correo"), problemas=["p" * 200] * 12, avisos=["a"] * 7, secreto="no",
                           adjuntos=[{"nombre": "x.pdf", "bytes": -1, "carpeta": "documentos", "ruta": "C:/x"}])
    tarjeta = sanear(cruda)
    assert set(tarjeta) == {"id", "clase", "version", "sha256", "titulo", "resumen", "aviso", "botones", "caduca_ts",
                            "canal_origen", "estado", "correo", "drive", "script", "salida"}
    assert tarjeta["estado"] == "pendiente"
    assert tarjeta["drive"] is None and tarjeta["script"] is None and tarjeta["salida"] is None
    assert tarjeta["botones"] == ["aprobar", "descartar", "editar"]       # sin «copiar» en un correo
    assert tarjeta["canal_origen"] == "escritorio" and tarjeta["caduca_ts"] is None
    # Informativos: sin controles y recortados con «…» en unidades UTF-16.
    assert tarjeta["titulo"] == "T" * 119 + "…"
    correo = tarjeta["correo"]
    assert "secreto" not in correo and correo["adjuntos"] == [{"nombre": "x.pdf", "bytes": 0, "carpeta": "documentos"}]
    assert len(correo["problemas"]) == 10 and all(len(p) == 160 and p.endswith("…") for p in correo["problemas"])
    assert len(correo["avisos"]) == 5
    # Aprobables: intactos (con \n y \t).
    assert correo["cuerpo"] == _seccion("correo")["cuerpo"] and correo["asunto"] == f"Informe {_MARCA}"
    # Salida → "info"; script no ejecutable → sin «aprobar» ni «guardar»; crítico nunca ejecutable.
    assert sanear(_tarjeta("salida"))["estado"] == "info"
    critico = sanear(_tarjeta("script", seccion=dict(_seccion("script"), nivel="critico", ejecutable=True)))
    assert critico["script"]["ejecutable"] is False and critico["botones"] == ["copiar", "descartar"]
    # Hallazgos: los inválidos se cuentan como omitidos; de más a menos grave.
    script = sanear(_tarjeta("script", seccion=dict(_seccion("script"), hallazgos=[
        {"nivel": "bajo", "categoria": "red", "linea": 3, "detalle": "a"},
        {"nivel": "raro", "categoria": "red", "linea": 1, "detalle": "b"},
        {"nivel": "alto", "categoria": "borrado", "linea": 9, "detalle": "c", "extra": 1},
        {"nivel": "medio", "categoria": "inventada", "linea": 2, "detalle": "d"}])))["script"]
    assert [h["detalle"] for h in script["hallazgos"]] == ["c", "a"] and script["hallazgos_omitidos"] == 2
    assert set(script["hallazgos"][0]) == {"nivel", "categoria", "linea", "detalle"}


@pytest.mark.parametrize("cambio", [
    ("correo", "cuerpo", "x" * 10_001),
    ("correo", "cuerpo", "a" * 9_999 + "😀"),               # 10 000 caracteres = 10 001 unidades UTF-16
    ("correo", "cuerpo", "hola \ud800"),                    # sustituto suelto: no se puede mostrar tal cual
    ("correo", "cuerpo", 42),
    ("correo", "asunto", "x" * 201),
    ("correo", "para", ["x" * 255]),
    ("correo", "para", "ana@example.com"),
    ("correo", "cc", ["a@b.co"] * 50),                      # 1 + 50 > 50 direcciones
    ("correo", "adjuntos", [{"nombre": "a.pdf", "bytes": 1, "carpeta": "d"}] * 6),
    ("correo", "adjuntos", [{"nombre": "n" * 256, "bytes": 1, "carpeta": "d"}]),
    ("script", "codigo", "x" * 24_577),
    ("script", "codigo", "é" * 12_289),                     # 24 578 bytes UTF-8
    ("script", "codigo", None),
    ("drive", "local", {"nombre": "n" * 256, "bytes": 1, "carpeta": "d"}),
    ("drive", "compartir", {"con": "c" * 255, "rol": "reader", "publico": False}),
])
def test_tarjeta_aprobable_fuera_de_tope_se_descarta_entera(cambio: tuple[str, str, Any]) -> None:
    clase, clave, valor = cambio
    tarjeta = _tarjeta(clase, seccion=dict(_seccion(clase), **{clave: valor}))
    with pytest.raises(escritorio_link._TarjetaInvalida) as info:
        escritorio_link._tarjeta_saneada(tarjeta)
    assert info.value.aprobable is True


def test_tarjeta_aprobable_en_el_tope_sale_intacta() -> None:
    cuerpo = "😀" * 5_000                                    # 10 000 unidades UTF-16 justas
    codigo = "é" * 12_288                                   # 24 576 bytes justos
    correo = escritorio_link._tarjeta_saneada(_tarjeta("correo", seccion=dict(_seccion("correo"), cuerpo=cuerpo)))
    assert correo["correo"]["cuerpo"] == cuerpo
    script = escritorio_link._tarjeta_saneada(_tarjeta("script", seccion=dict(_seccion("script"), codigo=codigo)))
    assert script["script"]["codigo"] == codigo


@pytest.mark.parametrize("cambios", [
    {"id": "0A1B2C3D"}, {"id": "0a1b2c3d\n"}, {"id": 12345678}, {"clase": "otra"}, {"version": 0},
    {"version": True}, {"version": "1"}, {"sha256": "ab" * 31}, {"correo": None},
    {"clase": "drive", "drive": dict(_seccion("drive"), op="borrar")},
    {"clase": "drive", "drive": dict(_seccion("drive"), paso=3)},
    {"clase": "drive", "drive": dict(_seccion("drive"), paso=True)},
    {"clase": "drive", "drive": dict(_seccion("drive"), compartir={"con": "a@b.co", "rol": "owner"})},
    {"clase": "script", "script": dict(_seccion("script"), nivel="extremo")},
    {"clase": "script", "script": dict(_seccion("script"), lenguaje="cmd")},
    {"clase": "salida", "salida": dict(_seccion("salida"), script_id="xyz")},
])
def test_tarjeta_con_formato_invalido(cambios: dict[str, Any]) -> None:
    tarjeta = _tarjeta("correo")
    tarjeta.update(cambios)
    with pytest.raises(escritorio_link._TarjetaInvalida) as info:
        escritorio_link._tarjeta_saneada(tarjeta)
    assert info.value.aprobable is False


def test_presupuesto_peor_caso_script_se_encoge_sin_tocar_el_codigo() -> None:
    cruda, codigo = _script_peor_caso()
    saneada = escritorio_link._tarjeta_saneada(cruda)
    assert _mensaje_tarjeta(saneada) > escritorio_link.MAX_BYTES_TARJETA      # sin encoger no cabe
    encogida = escritorio_link._tarjeta_que_cabe(saneada)
    assert encogida is not None
    assert _mensaje_tarjeta(encogida) <= escritorio_link.MAX_BYTES_TARJETA == 61_440
    script = encogida["script"]
    assert script["codigo"] == codigo
    assert len(script["hallazgos"]) == 5 and script["hallazgos_omitidos"] == 3 + 15
    assert all(h["nivel"] == "alto" for h in script["hallazgos"])            # los más graves primero
    assert (script["nivel"], script["ejecutable"], script["metodo"], script["habilidad"]) == (
        "alto", True, "ast", "limpiar descargas")
    # Pasos 1–3 bastan: la explicación y los textos comunes quedan como estaban.
    assert all(len(h["detalle"]) == 80 for h in script["hallazgos"])
    assert len(script["efectos"]) == 3 and len(script["requisitos"]) == 3
    assert script["explicacion"] == "€" * 600 and encogida["resumen"] == "é" * 300
    assert saneada["script"]["hallazgos_omitidos"] == 3 and len(saneada["script"]["hallazgos"]) == 20  # sin mutar


def test_presupuesto_peor_caso_correo_y_salida_salen_sin_encoger(monkeypatch: pytest.MonkeyPatch) -> None:
    for cruda in (_correo_peor_caso(), _salida_peor_caso()):
        saneada = escritorio_link._tarjeta_saneada(cruda)
        assert escritorio_link._tarjeta_que_cabe(saneada) is saneada
        assert _mensaje_tarjeta(saneada) <= 61_440
    correo = escritorio_link._tarjeta_saneada(_correo_peor_caso())["correo"]
    assert correo["cuerpo"] == "€" * 10_000 and len(correo["para"]) + len(correo["cc"]) + len(correo["cco"]) == 50
    # Si aun así no cupiera (error de programación), se descarta: nunca se recorta.
    monkeypatch.setattr(escritorio_link, "MAX_BYTES_TARJETA", 2_000)
    assert escritorio_link._tarjeta_que_cabe(escritorio_link._tarjeta_saneada(_correo_peor_caso())) is None
    assert escritorio_link._tarjeta_que_cabe(escritorio_link._tarjeta_saneada(_script_peor_caso()[0])) is None


async def _recoger_crudo(ws: Any, segundos: float) -> list[str]:
    """Mensajes crudos (texto tal cual llega) durante ``segundos``."""
    vistos: list[str] = []
    limite = time.monotonic() + segundos
    while True:
        restante = limite - time.monotonic()
        if restante <= 0:
            return vistos
        try:
            vistos.append(await asyncio.wait_for(ws.recv(), restante))
        except TimeoutError:
            return vistos


def test_tarjetas_a_la_app_y_al_hud_con_presupuesto(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams.escritorio")
    script, codigo = _script_peor_caso()

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(app, "hola")
                await _recibir_hasta(panel, "hola")
                await _recoger(panel, 0.2)
                await _recoger(app, 0.05)
                # Una aprobable fuera de tope y una de formato inválido: no salen.
                bus.publicar("tarjeta.mostrar", _tarjeta("correo", seccion=dict(_seccion("correo"), cuerpo="x" * 10_001)))
                bus.publicar("tarjeta.mostrar", _tarjeta("correo", ident="NOVALIDA"))
                bus.publicar("tarjeta.mostrar", _tarjeta("correo", extra="fuera"))
                bus.publicar("tarjeta.mostrar", script)
                bus.publicar("tarjeta.mostrar", _correo_peor_caso())
                bus.publicar("tarjeta.mostrar", _salida_peor_caso())
                crudos_panel = await _recoger_crudo(panel, 0.6)
                crudos_app = await _recoger_crudo(app, 0.1)
                for crudos in (crudos_panel, crudos_app):
                    tarjetas = [json.loads(c)["datos"] for c in crudos if json.loads(c)["tipo"] == "tarjeta"]
                    assert [t["id"] for t in tarjetas] == ["0a1b2c3d", "5c1a7e00", "c0a1b2c3", "5a11da00"]
                    assert "extra" not in tarjetas[0]
                    assert tarjetas[0]["correo"]["cuerpo"] == _seccion("correo")["cuerpo"]
                    assert tarjetas[1]["script"]["codigo"] == codigo
                    assert tarjetas[1]["script"]["hallazgos_omitidos"] == 18
                    assert tarjetas[3]["estado"] == "info"
                    assert all(len(c.encode("utf-8")) <= 61_440 for c in crudos if '"tipo":"tarjeta"' in c)
                    assert all(len(c.encode("utf-8")) <= _LIMITE_HUD for c in crudos)
        finally:
            await hud.cerrar()

    correr(caso())
    assert "Tarjeta correo inválida: descartada" in caplog.text
    assert _MARCA not in caplog.text and "€€€" not in caplog.text


def test_instantanea_con_tarjetas_version_vieja_y_cerrada(settings_prueba: Settings, bus: BusEventos,
                                                          correr: Any) -> None:
    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            # Antes de que haya clientes: se guardan para la instantánea (≤ 4; sobra la más vieja).
            bus.publicar("permiso.solicitado", {"id": "a1b2c3d4", "descripcion": "Acción", "destructiva": False})
            bus.publicar("tarjeta.mostrar", _tarjeta("salida", ident="00000001"))
            bus.publicar("tarjeta.mostrar", _tarjeta("correo", ident="00000002", version=2))
            bus.publicar("tarjeta.mostrar", _tarjeta("drive", ident="00000003"))
            bus.publicar("tarjeta.mostrar", _tarjeta("script", ident="00000004"))
            bus.publicar("tarjeta.mostrar", _tarjeta("salida", ident="00000005"))
            bus.publicar("tarjeta.mostrar", _tarjeta("correo", ident="00000002", version=1, titulo="vieja"))
            bus.publicar("tarjeta.mostrar", _tarjeta("drive", ident="00000003", version=1, titulo="igual"))
            async with _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                vistos = await _recoger(panel, 0.4)
                tipos = [m["tipo"] for m in vistos]
                assert tipos[:2] == ["hola", "permiso"]
                tarjetas = [m["datos"] for m in vistos if m["tipo"] == "tarjeta"]
                assert [t["id"] for t in tarjetas] == ["00000002", "00000004", "00000005", "00000003"]
                assert tarjetas[0]["version"] == 2 and tarjetas[3]["titulo"] == "igual"
                assert tipos.index("no_molestar") > max(i for i, t in enumerate(tipos) if t == "tarjeta")
                # tarjeta.cerrada: a todos y deja de estar en la instantánea.
                bus.publicar("tarjeta.cerrada", {"id": "00000002", "clase": "correo", "resultado": "enviado",
                                                 "motivo": "m" * 300})
                cerrada, _ = await _recibir_hasta(panel, "tarjeta")
                assert cerrada == {"id": "00000002", "clase": "correo", "estado": "cerrada", "resultado": "enviado",
                                   "motivo": "m" * 199 + "…"}
                bus.publicar("tarjeta.cerrada", {"id": "00000004", "clase": "?", "resultado": "descartado"})
                cerrada, _ = await _recibir_hasta(panel, "tarjeta")
                assert cerrada["clase"] == "script"          # la clase guardada
                bus.publicar("tarjeta.cerrada", {"id": "00000005", "clase": "salida", "resultado": "volado"})
                assert not [m for m in await _recoger(panel, 0.2) if m["tipo"] == "tarjeta"]
            async with _conectar(hud, token) as app:
                vistos = await _recoger(app, 0.4)
                assert [m["datos"]["id"] for m in vistos if m["tipo"] == "tarjeta"] == ["00000003"]
        finally:
            await hud.cerrar()

    correr(caso())


_V1 = {"version": 1}


@pytest.mark.parametrize("mensaje", [
    {"id": "0A1B2C3D", "sha256": _HUELLA, "decision": "aprobar", **_V1},
    {"id": "0a1b2c3d\n", "sha256": _HUELLA, "decision": "aprobar", **_V1},
    {"id": "0a1b2c3d", "sha256": "AB" * 32, "decision": "aprobar", **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "copiar", **_V1},             # copiar es local
    # La versión que ve el cliente: obligatoria salvo en «descartar»; entero 1..2³¹−1 (bool no vale).
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar"},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar_publico"},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": "x"}},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", "nombre": "x"},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": 0},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": True},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": "1"},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": 1.0},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": 2 ** 31},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "descartar", "version": -1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "nombre": "x", **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "campos": {}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", "nombre": "   ", **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", "nombre": "n" * 41, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", "nombre": "a\nb", **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": ["cuerpo"], **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"firma": "x"}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": 5}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": "x" * 10_001}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": "😀" * 5_000 + "x"}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"para": "x" * 2_601}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"asunto": "a\nb"}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"asunto": "Informe\tQ3"}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": "a\rb"}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": "Hola\x0bAna"}, **_V1},
    {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": {"cuerpo": "a\x00b"}, **_V1},
])
def test_decision_tarjeta_invalida(mensaje: dict[str, Any]) -> None:
    assert escritorio_link._decision_tarjeta_valida(mensaje) is None


def test_decision_tarjeta_valida_sin_recortar() -> None:
    validar = escritorio_link._decision_tarjeta_valida
    assert validar({"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "descartar", "extra": 1}) == {
        "id": "0a1b2c3d", "sha256": _HUELLA, "decision": "descartar"}         # sin versión: compatibilidad
    assert validar({"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "descartar", "version": 7}) == {
        "id": "0a1b2c3d", "sha256": _HUELLA, "decision": "descartar", "version": 7}
    assert validar({"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": 2 ** 31 - 1}) == {
        "id": "0a1b2c3d", "sha256": _HUELLA, "decision": "aprobar", "version": 2 ** 31 - 1}
    assert validar({"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", "nombre": "  ñ" * 13 + "  ",
                    **_V1}) == {"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "guardar", "version": 1,
                                "nombre": ("  ñ" * 13).strip()}
    campos = {"para": "a@b.co, c@d.co", "cc": "", "cco": "x" * 2600, "asunto": "€" * 200,
              "cuerpo": "😀" * 5_000, "adjuntos": "informe.pdf, plan.xlsx"}
    assert validar({"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "campos": campos,
                    **_V1})["campos"] == campos
    assert validar({"id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar",
                    "campos": {"cuerpo": "línea 1\n\tlínea 2"}, **_V1}) is not None


def _decision_de_bytes(total: int) -> str:
    """``tarjeta_decision`` válida («descartar») de exactamente ``total`` bytes UTF-8 (con relleno ASCII)."""
    base = json.dumps({"tipo": "tarjeta_decision", "id": "0a1b2c3d", "sha256": _HUELLA, "decision": "descartar",
                       "relleno": ""}, separators=(",", ":"))
    crudo = base.replace('"relleno":""', '"relleno":"' + "z" * (total - len(base)) + '"')
    assert len(crudo.encode("utf-8")) == total
    return crudo


def test_tarjeta_decision_desde_la_app_y_el_hud(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams.escritorio")
    registro = _Registro(bus)
    editar = {"tipo": "tarjeta_decision", "id": "0a1b2c3d", "sha256": _HUELLA, "decision": "editar", "version": 1,
              "campos": {"cuerpo": f"Hola {_MARCA}\n\tfin", "para": f"ana@example.com, {_MARCA}"}}
    guardar = {"tipo": "tarjeta_decision", "id": "5c1a7e00", "sha256": _HUELLA, "decision": "guardar", "version": 3,
               "nombre": f"habilidad {_MARCA[:10]}"}

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(app, "hola")
                await _recibir_hasta(panel, "hola")
                await app.send(json.dumps(editar))
                await panel.send(json.dumps(guardar))
                await _esperar(lambda: len(registro.de("escritorio.tarjeta")) == 2)
                assert registro.de("escritorio.tarjeta") == [
                    {k: v for k, v in editar.items() if k != "tipo"}, {k: v for k, v in guardar.items() if k != "tipo"}]
                # 61 440 bytes sí y 61 441 no, desde la app y desde el HUD; un chat de 9 KiB sigue fuera.
                for ws in (app, panel):
                    await ws.send(_decision_de_bytes(61_440))
                    await ws.send(_decision_de_bytes(61_441))
                    await ws.send(json.dumps({"tipo": "chat", "texto": "hola", "relleno": "z" * 9 * 1024}))
                    await ws.send(json.dumps({"tipo": "tarjeta_decision", "id": "0a1b2c3d", "sha256": _HUELLA,
                                              "decision": "editar", "version": 1, "campos": {"cuerpo": "x" * 10_001}}))
                await _esperar(lambda: len(registro.de("escritorio.tarjeta")) == 4)
                await asyncio.sleep(0.3)
                assert len(registro.de("escritorio.tarjeta")) == 4
                assert all(d["decision"] == "descartar" for d in registro.de("escritorio.tarjeta")[2:])
                assert registro.de("escritorio.chat") == []
                # Ritmo: como mucho 5 decisiones por segundo y cliente.
                await asyncio.sleep(1.05)
                for _ in range(8):   # (sin tarjeta vigente aquí: las decide main, que dirá que no está vigente)
                    await app.send(json.dumps({"tipo": "tarjeta_decision", "id": "0a1b2c3d", "sha256": _HUELLA,
                                               "decision": "aprobar", "version": 1}))
                await asyncio.sleep(0.4)
                assert len(registro.de("escritorio.tarjeta")) == 4 + 5
                # La conexión sigue viva.
                bus.publicar("escritorio.sistema", {"texto": "vivo"})
                assert (await _recibir_hasta(panel, "sistema"))[0] == {"texto": "vivo"}
        finally:
            await hud.cerrar()

    correr(caso())
    assert "Decisión de tarjeta desde el escritorio: editar" in caplog.text
    assert "demasiadas decisiones de tarjeta" in caplog.text
    assert _MARCA not in caplog.text and _MARCA[:10] not in caplog.text


def test_tarjetas_suscritas_y_privadas() -> None:
    for nombre in ("tarjeta.mostrar", "tarjeta.cerrada"):
        assert nombre in EVENTOS_SUSCRITOS_ESCRITORIO and nombre not in EVENTOS_SUSCRITOS
    assert "escritorio.tarjeta" not in EVENTOS_SUSCRITOS and "escritorio.tarjeta" not in EVENTOS_SUSCRITOS_ESCRITORIO
    assert escritorio_link.MAX_BYTES_TARJETA == escritorio_link.MAX_BYTES_DECISION_TARJETA == 61_440 < _LIMITE_HUD


# --- Decisiones ligadas a la versión vista y tarjetas «en curso» (revisión ui de la v0.2.2) ---------


def _decision(ident: str, decision: str, version: int | None = 1, sha: str = _HUELLA,
              **extra: Any) -> dict[str, Any]:
    """Mensaje ``tarjeta_decision`` como lo manda un cliente (``version=None``: sin versión)."""
    mensaje: dict[str, Any] = {"tipo": "tarjeta_decision", "id": ident, "sha256": sha, "decision": decision, **extra}
    if version is not None:
        mensaje["version"] = version
    return mensaje


class _Reloj:
    """Reloj monotónico falso para el enlace (solo avanza a mano)."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


def _tarjetas_iniciales(enlace: EnlaceEscritorio) -> list[dict[str, Any]]:
    """Las tarjetas de la instantánea que recibiría un cliente que (re)conecta ahora."""
    return [m["datos"] for m in map(json.loads, enlace._mensajes_iniciales(False)) if m["tipo"] == "tarjeta"]


def test_decision_aceptada_deja_la_tarjeta_en_curso_y_un_clic_rancio_no_vale(
        settings_prueba: Settings, bus: BusEventos, caplog: pytest.LogCaptureFixture) -> None:
    """Regresión: mientras main ejecuta una decisión, un cliente que reconecta (o el otro cliente) no
    puede volver a pulsar la tarjeta, y un clic sobre la versión 1 no vale para la versión 2 que main
    reabre con la MISMA huella (el script no cambió): un doble clic nunca ejecuta ni envía dos veces."""
    caplog.set_level(logging.DEBUG, logger="rams.escritorio")
    registro = _Registro(bus)
    enlace = EnlaceEscritorio(settings_prueba, bus, reloj=_Reloj())
    app = escritorio_link._Cliente(object(), True)
    panel = escritorio_link._Cliente(object(), False)
    assert enlace._traducir("tarjeta.mostrar", _tarjeta("script", ident="5c1a7e00")) is not None
    enlace._en_tarjeta_decision(app, _decision("5c1a7e00", "aprobar"))
    assert registro.de("escritorio.tarjeta") == [{"id": "5c1a7e00", "sha256": _HUELLA, "decision": "aprobar",
                                                   "version": 1}]
    # main ejecuta (no publica nada): quien (re)conecta la recibe «en curso», sin botones de decisión.
    [vista] = _tarjetas_iniciales(enlace)
    assert (vista["version"], vista["estado"], vista["botones"]) == (1, "pendiente", ["copiar"])
    assert vista["aviso"] == "Ejecutando el script: espera el resultado."
    guardada = enlace._tarjetas["5c1a7e00"]
    assert vista["script"] == guardada["script"]                               # el contenido, intacto
    assert guardada["botones"] == ["aprobar", "guardar", "copiar", "descartar"] and guardada["aviso"] == ""
    # El 2.º clic (del otro cliente, o reactivado por una instantánea vieja) no llega a main.
    enlace._en_tarjeta_decision(panel, _decision("5c1a7e00", "aprobar"))
    enlace._en_tarjeta_decision(panel, _decision("5c1a7e00", "guardar", nombre="limpiar"))
    assert len(registro.de("escritorio.tarjeta")) == 1
    # main reabre tras ejecutar: versión 2 con la MISMA huella. El clic rancio (versión 1) no vale.
    enlace._traducir("tarjeta.mostrar", _tarjeta("script", ident="5c1a7e00", version=2))
    [vista] = _tarjetas_iniciales(enlace)
    assert vista["version"] == 2 and "aprobar" in vista["botones"]
    enlace._en_tarjeta_decision(panel, _decision("5c1a7e00", "aprobar", version=1))
    assert len(registro.de("escritorio.tarjeta")) == 1
    enlace._en_tarjeta_decision(panel, _decision("5c1a7e00", "aprobar", version=2))   # el que sí ve la v2
    assert [d["version"] for d in registro.de("escritorio.tarjeta")] == [1, 2]
    # «Descartar» siempre llega (también «en curso» y sin versión, por compatibilidad).
    enlace._en_tarjeta_decision(app, _decision("5c1a7e00", "descartar", version=None))
    assert registro.de("escritorio.tarjeta")[-1] == {"id": "5c1a7e00", "sha256": _HUELLA, "decision": "descartar"}
    enlace._traducir("tarjeta.cerrada", {"id": "5c1a7e00", "clase": "script", "resultado": "descartado"})
    assert enlace._en_curso == {} and _tarjetas_iniciales(enlace) == []
    assert "Decisión de tarjeta ignorada (aprobar): otra decisión sobre esa tarjeta sigue en curso" in caplog.text
    assert "Decisión de tarjeta ignorada (aprobar): no es la versión vigente" in caplog.text
    assert _MARCA not in caplog.text and "limpiar" not in caplog.text


def test_decision_con_otra_huella_o_boton_no_ofrecido_se_ignora_y_en_curso_caduca(
        settings_prueba: Settings, bus: BusEventos) -> None:
    registro = _Registro(bus)
    reloj = _Reloj()
    enlace = EnlaceEscritorio(settings_prueba, bus, reloj=reloj)
    cliente = escritorio_link._Cliente(object(), True)
    enlace._traducir("tarjeta.mostrar", _tarjeta("correo"))
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "aprobar", sha="cd" * 32))    # otra huella
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "aprobar_publico"))           # no la ofrece
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "guardar", nombre="x"))       # no la ofrece
    assert registro.de("escritorio.tarjeta") == [] and enlace._en_curso == {}
    # Una tarjeta que aquí no está vigente (cerrada, o de antes de reiniciar) la decide main, como siempre.
    enlace._en_tarjeta_decision(cliente, _decision("99999999", "aprobar"))
    assert [d["id"] for d in registro.de("escritorio.tarjeta")] == ["99999999"]
    # Un «editar» sin campos no cambia nada y main no le responde: no deja la tarjeta bloqueada.
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "editar", campos={}))
    assert len(registro.de("escritorio.tarjeta")) == 2 and enlace._en_curso == {}
    # «Enviar»: en curso (sin ningún botón, el correo no tiene «copiar») hasta la respuesta de main...
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "aprobar"))
    [vista] = _tarjetas_iniciales(enlace)
    assert vista["botones"] == [] and vista["aviso"] == "Enviando el correo: espera el resultado."
    reloj.t += escritorio_link.EN_CURSO_MAX_S - 1
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "aprobar"))
    assert len(registro.de("escritorio.tarjeta")) == 3
    # ...o, como red de seguridad si main no contestara nunca, hasta EN_CURSO_MAX_S (15 min).
    reloj.t += 1
    assert _tarjetas_iniciales(enlace)[0]["botones"] == ["aprobar", "editar", "descartar"]
    enlace._en_tarjeta_decision(cliente, _decision("0a1b2c3d", "aprobar"))
    assert len(registro.de("escritorio.tarjeta")) == 4


def test_segunda_confirmacion_publica_no_vale_antes_del_armado(settings_prueba: Settings, bus: BusEventos) -> None:
    """Regresión: un doble clic sobre «Compartir» no completa también «Sí, hacerlo público» (§0.1.3 y §5.3:
    lo público pide dos confirmaciones explícitas). El 2.º clic llega ~150 ms después del 1.º."""
    registro = _Registro(bus)
    reloj = _Reloj()
    enlace = EnlaceEscritorio(settings_prueba, bus, reloj=reloj)
    cliente = escritorio_link._Cliente(object(), False)
    seccion = dict(_seccion("drive"), op="compartir", local=None,
                   archivo={"nombre": "plan.xlsx", "tipo": "hoja", "tamano": 1, "modificado": "", "propio": True},
                   compartir={"con": "", "rol": "reader", "publico": True})
    paso_2 = dict(seccion, paso=2)
    enlace._traducir("tarjeta.mostrar", _tarjeta("drive", ident="d0d0d0d0", seccion=seccion))
    enlace._en_tarjeta_decision(cliente, _decision("d0d0d0d0", "aprobar"))                     # 1.er clic
    reloj.t += 0.03                                                       # main contesta con el paso 2
    enlace._traducir("tarjeta.mostrar", _tarjeta("drive", ident="d0d0d0d0", version=2, seccion=paso_2,
                                                 botones=["aprobar_publico", "descartar"]))
    reloj.t += 0.12
    enlace._en_tarjeta_decision(cliente, _decision("d0d0d0d0", "aprobar_publico", version=2))  # 2.º clic
    assert [d["decision"] for d in registro.de("escritorio.tarjeta")] == ["aprobar"]
    # La misma versión republicada (otro aviso) no reinicia la espera; pasado el armado, sí vale.
    reloj.t += 0.5                                                        # 0,62 s desde el paso 2
    enlace._traducir("tarjeta.mostrar", _tarjeta("drive", ident="d0d0d0d0", version=2, seccion=paso_2,
                                                 botones=["aprobar_publico", "descartar"], aviso="Confirma"))
    reloj.t += escritorio_link.ARMADO_PUBLICO_S - 0.62 - 0.01            # justo antes
    enlace._en_tarjeta_decision(cliente, _decision("d0d0d0d0", "aprobar_publico", version=2))
    assert [d["decision"] for d in registro.de("escritorio.tarjeta")] == ["aprobar"]
    reloj.t += 0.02                                                       # justo después
    enlace._en_tarjeta_decision(cliente, _decision("d0d0d0d0", "aprobar_publico", version=2))
    assert [d["decision"] for d in registro.de("escritorio.tarjeta")] == ["aprobar", "aprobar_publico"]


def test_en_curso_se_difunde_antes_de_publicar_y_el_rechazo_se_contesta_solo_a_ese_cliente(
        settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    """Con clientes reales: la vista «en curso» llega a la app y al HUD (el otro cliente se entera del
    clic), un clic repetido se ignora y solo quien lo mandó recibe la tarjeta otra vez (para dejar de
    esperar); la respuesta de main (versión nueva) la deja como antes."""
    registro = _Registro(bus)

    async def caso() -> None:
        hud = _servidor(settings_prueba, bus)
        await hud.iniciar()
        try:
            token = _archivo(settings_prueba)["token"]
            async with _conectar(hud, token) as app, _conectar(hud, token, origen=_origen_hud(hud)) as panel:
                await _recibir_hasta(app, "hola")
                await _recibir_hasta(panel, "hola")
                bus.publicar("tarjeta.mostrar", _tarjeta("script", ident="5c1a7e00"))
                for ws in (app, panel):
                    datos, _ = await _recibir_hasta(ws, "tarjeta")
                    assert datos["botones"] == ["aprobar", "guardar", "copiar", "descartar"]
                await panel.send(json.dumps(_decision("5c1a7e00", "aprobar")))
                for ws in (app, panel):
                    datos, _ = await _recibir_hasta(ws, "tarjeta")
                    assert (datos["version"], datos["botones"]) == (1, ["copiar"])
                await _esperar(lambda: len(registro.de("escritorio.tarjeta")) == 1)
                # La app pulsa a la vez (aún veía «Ejecutar»): no llega a main y solo ella recibe respuesta.
                await app.send(json.dumps(_decision("5c1a7e00", "aprobar")))
                datos, _ = await _recibir_hasta(app, "tarjeta")
                assert datos["botones"] == ["copiar"]
                assert not [m for m in await _recoger(panel, 0.3) if m["tipo"] == "tarjeta"]
                assert len(registro.de("escritorio.tarjeta")) == 1
                # main reabre: versión 2, otra vez con sus botones, para todos.
                bus.publicar("tarjeta.mostrar", _tarjeta("script", ident="5c1a7e00", version=2))
                for ws in (app, panel):
                    datos, _ = await _recibir_hasta(ws, "tarjeta")
                    assert (datos["version"], datos["botones"]) == (2, ["aprobar", "guardar", "copiar", "descartar"])
        finally:
            await hud.cerrar()

    correr(caso())
