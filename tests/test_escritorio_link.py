"""Tests del endpoint ``/escritorio`` (grupo escritorio-ws, ``docs/COMPANION.md`` §C2.4 y §C3).

Sin red externa ni hardware: un ``ServidorHUD`` propio en ``127.0.0.1:0`` y clientes
``websockets`` locales. El token de ``/escritorio`` va a ``tmp_path`` (``settings_prueba``): ningún
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
    EVENTOS_SUSCRITOS_ESCRITORIO,
    EnlaceEscritorio,
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
