"""Tests del servidor del HUD «videollamada Amadeus» (grupo hud, §10.23 y §12).

Sin red externa ni hardware: el servidor escucha en ``127.0.0.1`` con puerto aleatorio
(``hud_puerto=0``) y los clientes son ``httpx`` y ``websockets`` locales. Nunca se abre un
navegador real (``create_subprocess_exec`` y ``webbrowser.open`` se sustituyen por *fakes*).
Sin pytest-asyncio: las pruebas asíncronas usan la fixture ``correr`` (``asyncio.run``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any

import pytest

websockets = pytest.importorskip("websockets")
httpx = pytest.importorskip("httpx")

from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import InvalidStatus  # noqa: E402

from core.bus import BusEventos  # noqa: E402
from core.config import Settings  # noqa: E402
from core.contracts import (  # noqa: E402
    DiagnosticoModulo,
    EstadoModulo,
    EstadoPresencia,
    HomeostaticState,
    Telemetria,
)
from interface import hud_server  # noqa: E402
from interface.hud_server import CONTROLES_PERMITIDOS, CSP, ServidorHUD, traducir_evento  # noqa: E402

RUTA_HTML = Path(hud_server.__file__).resolve().parent / "amadeus_hud.html"
# websockets ≥ 15 usa por defecto el proxy del entorno: en los tests se desactiva.
_KW_CONEXION: dict[str, Any] = {"proxy": None} if "proxy" in inspect.signature(connect).parameters else {}


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------


def _hud(settings: Settings, bus: BusEventos, **kw: Any) -> ServidorHUD:
    return ServidorHUD(settings.con(hud_habilitado=True, hud_puerto=0), bus, **kw)


def _url_ws(hud: ServidorHUD, token: str | None = None) -> str:
    return f"ws://127.0.0.1:{hud.puerto_real}/ws?t={hud.token if token is None else token}"


def _origen(hud: ServidorHUD, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{hud.puerto_real}"


def _conectar(hud: ServidorHUD, *, token: str | None = None, origen: str | None = "auto") -> Any:
    origin = _origen(hud) if origen == "auto" else origen
    return connect(_url_ws(hud, token), origin=origin, open_timeout=5, **_KW_CONEXION)


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


def _estado(**kw: Any) -> dict[str, Any]:
    estado = HomeostaticState(**kw)
    return {"estado": estado.a_dict(), "bump": list(estado.bump)}


# ---------------------------------------------------------------------------
# Arranque, diagnóstico y HTTP
# ---------------------------------------------------------------------------


def test_iniciar_ok_con_puerto_aleatorio(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        assert hud.url is None and hud.puerto_real is None and hud.n_clientes == 0
        diag = await hud.iniciar()
        try:
            assert isinstance(diag, DiagnosticoModulo)
            assert diag.clave == "hud"
            assert diag.estado is EstadoModulo.OK
            assert hud.puerto_real is not None and hud.puerto_real > 0
            assert hud.url == f"http://127.0.0.1:{hud.puerto_real}/?t={hud.token}"
            # El detalle lleva la URL SIN token; el token nunca sale en repr.
            assert diag.detalle == f"http://127.0.0.1:{hud.puerto_real}/"
            assert hud.token not in diag.detalle
            assert hud.token not in repr(hud)
            assert len(hud.token) >= 16
            # Segunda llamada: no abre otro servidor.
            assert (await hud.iniciar()).estado is EstadoModulo.OK
        finally:
            await hud.cerrar()
        assert hud.url is None
        await hud.cerrar()  # idempotente

    correr(caso())


def test_tokens_distintos_por_instancia(settings_prueba: Settings, bus: BusEventos) -> None:
    a = _hud(settings_prueba, bus)
    b = _hud(settings_prueba, bus)
    assert a.token != b.token


def test_get_raiz_sirve_html_con_cabeceras(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        base = f"http://127.0.0.1:{hud.puerto_real}"
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5.0) as cliente:
                r = await cliente.get(base + "/")
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/html")
                assert r.headers["cache-control"] == "no-store"
                assert r.headers["x-content-type-options"] == "nosniff"
                assert r.headers["content-security-policy"] == CSP
                assert "<title>Amadeus</title>" in r.text
                assert hud.token not in r.text
                assert (await cliente.get(base + "/index.html")).status_code == 200
                assert (await cliente.get(base + "/favicon.ico")).status_code == 204
                assert (await cliente.get(base + "/secreto")).status_code == 404
                # /ws sin token también se rechaza por HTTP.
                assert (await cliente.get(base + "/ws")).status_code == 403
        finally:
            await hud.cerrar()

    correr(caso())


def test_sin_html_degradado_y_pagina_de_aviso(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                              tmp_path: Path) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus, ruta_html=tmp_path / "no_existe.html")
        diag = await hud.iniciar()
        try:
            assert diag.estado is EstadoModulo.DEGRADADO
            assert "amadeus_hud.html" in diag.detalle and hud.token not in diag.detalle
            async with httpx.AsyncClient(trust_env=False, timeout=5.0) as cliente:
                r = await cliente.get(f"http://127.0.0.1:{hud.puerto_real}/")
            assert r.status_code == 200 and "<title>Amadeus</title>" in r.text
        finally:
            await hud.cerrar()

    correr(caso())


def test_deshabilitado_da_off(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = ServidorHUD(settings_prueba.con(hud_habilitado=False, hud_puerto=0), bus)
        diag = await hud.iniciar()
        assert diag.clave == "hud" and diag.estado is EstadoModulo.OFF
        assert hud.puerto_real is None and hud.url is None
        await hud.cerrar()

    correr(caso())


def test_sin_websockets_da_off(settings_prueba: Settings, bus: BusEventos, correr: Any,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hud_server, "WEBSOCKETS_DISPONIBLE", False)

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        diag = await hud.iniciar()
        assert diag.estado is EstadoModulo.OFF
        assert diag.detalle == "websockets no instalado"
        assert hud.url is None
        await hud.cerrar()

    correr(caso())


def test_puerto_ocupado_da_sin_conexion(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    ocupante = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        ocupante.bind(("127.0.0.1", 0))
        ocupante.listen(1)
        puerto = ocupante.getsockname()[1]

        async def caso() -> None:
            hud = ServidorHUD(settings_prueba.con(hud_habilitado=True, hud_puerto=puerto), bus)
            diag = await hud.iniciar()
            assert diag.estado is EstadoModulo.SIN_CONEXION
            assert diag.detalle == "puerto ocupado"
            assert hud.url is None
            await hud.cerrar()

        correr(caso())
    finally:
        ocupante.close()


# ---------------------------------------------------------------------------
# WebSocket: hola, instantánea, reenvío y controles
# ---------------------------------------------------------------------------


def test_ws_hola_estado_y_control_colgar(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    controles: list[dict[str, Any]] = []
    bus.suscribir("hud.control", lambda evento: controles.append(dict(evento.datos)))

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                hola = json.loads(await asyncio.wait_for(ws.recv(), 3))
                assert hola["tipo"] == "hola"
                assert set(hola["datos"]) == {"version", "nombre_ia", "usuario", "inicio_ts"}
                assert hola["datos"]["version"] == "0.1.0"
                assert hola["datos"]["nombre_ia"] == "Amadeus"
                assert hola["datos"]["usuario"] == settings_prueba.trato_usuario == "Lab Mem 001"
                assert isinstance(hola["datos"]["inicio_ts"], float)

                bus.publicar("homeostasis.estado", _estado(arousal=0.62, bump=tuple([0.5] * 16)))
                datos, _ = await _recibir_hasta(ws, "estado")
                assert datos["estado"]["arousal"] == pytest.approx(0.62)
                assert datos["estado"]["divergencia"] == pytest.approx(1.048596)
                assert datos["bump"] == [0.5] * 16
                assert hud.n_clientes == 1

                await ws.send(json.dumps({"tipo": "control", "accion": "colgar"}))
                ack, _ = await _recibir_hasta(ws, "control_ack")
                assert ack == {"accion": "colgar", "ok": True}
                assert controles == [{"accion": "colgar"}]
        finally:
            await hud.cerrar()
        assert hud.n_clientes == 0

    correr(caso())


def test_controles_no_permitidos_y_limite_por_cliente(settings_prueba: Settings, bus: BusEventos,
                                                     correr: Any) -> None:
    assert CONTROLES_PERMITIDOS == frozenset({"silenciar", "camara", "colgar"})
    controles: list[str] = []
    bus.suscribir("hud.control", lambda evento: controles.append(evento.datos["accion"]))

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                await _recibir_hasta(ws, "hola")
                # Nada de esto es un control permitido: se ignora sin respuesta.
                await ws.send(json.dumps({"tipo": "control", "accion": "apagar_pc"}))
                await ws.send(json.dumps({"tipo": "texto", "texto": "abre terminal"}))
                await ws.send(json.dumps({"tipo": "control", "accion": ["colgar"]}))
                await ws.send("esto no es JSON")
                await ws.send(b"\x00\x01")
                vistos = await _recoger(ws, 0.4)
                assert not [m for m in vistos if m["tipo"] == "control_ack"]
                assert controles == []

                await asyncio.sleep(1.1)  # nueva ventana de 1 s
                for _ in range(10):
                    await ws.send(json.dumps({"tipo": "control", "accion": "silenciar"}))
                acks = [m for m in await _recoger(ws, 0.8) if m["tipo"] == "control_ack"]
                assert len(acks) == 5
                assert controles == ["silenciar"] * 5
                assert hud.descartados_cliente >= 5
        finally:
            await hud.cerrar()

    correr(caso())


def test_handshake_rechaza_token_u_origin_ajenos(settings_prueba: Settings, bus: BusEventos,
                                                 correr: Any) -> None:
    async def rechazado(hud: ServidorHUD, **kw: Any) -> int:
        with pytest.raises(InvalidStatus) as info:
            async with _conectar(hud, **kw):
                pass
        return info.value.response.status_code

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            assert await rechazado(hud, token="incorrecto") == 403
            assert await rechazado(hud, token="") == 403
            assert await rechazado(hud, origen="http://evil.example") == 403
            assert await rechazado(hud, origen=f"http://127.0.0.1:{hud.puerto_real + 1}") == 403
            assert await rechazado(hud, origen=None) == 403
            assert hud.n_clientes == 0
            # Los dos orígenes locales permitidos funcionan.
            async with _conectar(hud, origen=_origen(hud, "localhost")) as ws:
                await _recibir_hasta(ws, "hola")
        finally:
            await hud.cerrar()

    correr(caso())


def test_instantanea_incluye_modulo_publicado_antes_de_iniciar(settings_prueba: Settings, bus: BusEventos,
                                                              correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        # Publicados DESPUÉS de construir y ANTES de iniciar(): el HUD ya está suscrito.
        broca = DiagnosticoModulo("broca", "Área de Broca (Gemini)", EstadoModulo.OK, "", 412)
        bus.publicar("sistema.modulo", broca.a_dict())
        bus.publicar("sistema.modulo", DiagnosticoModulo("vts", "Puente VTube Studio :8001",
                                                         EstadoModulo.EN_ESPERA).a_dict())
        bus.publicar("sistema.arranque", {"version": "0.1.0", "offline": True, "inicio_ts": 1234.5})
        bus.publicar("pc.telemetria", Telemetria(cpu_pct=23.0, ram_pct=59.0, temp_cpu=61.0).a_dict())
        bus.publicar("usuario.presencia", EstadoPresencia(True, 0.95, "terminal_reciente").a_dict())
        bus.publicar("vision.camara", {"activa": False, "habilitada": False, "duracion_ms": 0})
        bus.publicar("homeostasis.estado", _estado(fatiga=0.26))
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                hola, vistos = await _recibir_hasta(ws, "hola")
                assert hola["inicio_ts"] == pytest.approx(1234.5)
                datos, resto = await _recibir_hasta(ws, "modulo", cond=lambda d: d["clave"] == "broca")
                assert datos == {"clave": "broca", "nombre": "Área de Broca (Gemini)", "estado": "OK",
                                 "detalle": "", "latencia_ms": 412}
                vistos += resto + await _recoger(ws, 0.3)
                tipos = [m["tipo"] for m in vistos]
                assert vistos[0]["tipo"] == "hola"
                for tipo in ("estado", "telemetria", "presencia", "camara"):
                    assert tipo in tipos, tipo
                claves = {m["datos"]["clave"] for m in vistos if m["tipo"] == "modulo"}
                assert {"broca", "vts", "hud"} <= claves
                tele = next(m["datos"] for m in vistos if m["tipo"] == "telemetria")
                assert tele["cpu_pct"] == 23.0 and tele["temp_gpu"] is None
        finally:
            await hud.cerrar()

    correr(caso())


def test_apagado_y_permiso(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                await _recibir_hasta(ws, "hola")
                bus.publicar("permiso.solicitado", {"id": "p1", "descripcion": "cerrar firefox «Docs»",
                                                    "destructiva": True, "tipo": "cerrar_ventana"})
                permiso, _ = await _recibir_hasta(ws, "permiso")
                assert permiso == {"id": "p1", "descripcion": "cerrar firefox «Docs»",
                                   "destructiva": True, "pendiente": True}
                # Un cliente nuevo ve el banner pendiente en la instantánea.
                async with _conectar(hud) as ws2:
                    pendiente, _ = await _recibir_hasta(ws2, "permiso")
                    assert pendiente["pendiente"] is True and pendiente["id"] == "p1"

                bus.publicar("permiso.resuelto", {"id": "p1", "aprobado": False,
                                                  "motivo": "rechazada por el usuario"})
                resuelto, _ = await _recibir_hasta(ws, "permiso")
                assert resuelto == {"id": "p1", "aprobado": False, "motivo": "rechazada por el usuario",
                                    "pendiente": False}

                bus.publicar("sistema.apagado", {"motivo": "usuario"})
                apagado, _ = await _recibir_hasta(ws, "apagado")
                assert apagado == {"motivo": "usuario"}

            # Tras el apagado, un cliente nuevo recibe «apagado» en la instantánea y sin permiso pendiente.
            async with _conectar(hud) as ws3:
                vistos = await _recoger(ws3, 0.4)
                tipos = [m["tipo"] for m in vistos]
                assert "apagado" in tipos and "permiso" not in tipos
        finally:
            await hud.cerrar()

    correr(caso())


def test_reenvio_de_eventos_de_la_tabla(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                await _recibir_hasta(ws, "hola")
                bus.publicar("telegram.enviado", {"tipo": "llamada", "nivel": 1, "texto": "x" * 500})
                llamada, _ = await _recibir_hasta(ws, "llamada")
                assert llamada["tipo"] == "perdida" and len(llamada["texto"]) <= 200
                bus.publicar("telegram.respuesta_llamada", {"llamada_id": 7, "contestada": True})
                llamada, _ = await _recibir_hasta(ws, "llamada")
                assert llamada == {"tipo": "devuelta", "texto": ""}
                bus.publicar("memoria.consolidada", {"kv_id": "KV-0007", "n_turnos": 24, "metodo": "extractivo"})
                consolidacion, _ = await _recibir_hasta(ws, "consolidacion")
                assert consolidacion == {"kv_id": "KV-0007", "n_turnos": 24, "metodo": "extractivo"}
                bus.publicar("homeostasis.cambio_linea", {"antes": "steins_gate", "despues": "beta",
                                                          "divergencia": 1.09})
                linea, _ = await _recibir_hasta(ws, "linea_mundo")
                assert linea == {"antes": "steins_gate", "despues": "beta", "divergencia": 1.09}
                bus.publicar("vision.camara", {"activa": True, "habilitada": True, "duracion_ms": 0})
                camara, _ = await _recibir_hasta(ws, "camara")
                assert camara["activa"] is True
                bus.publicar("salida.subtitulo", {"texto": "Hola.", "exp": "seria", "motion": "asentir",
                                                  "tone": "analitico", "canal": "voz", "origen": "respuesta"})
                subtitulo, _ = await _recibir_hasta(ws, "subtitulo")
                assert subtitulo["texto"] == "Hola." and subtitulo["exp"] == "seria"
                bus.publicar("voz.inicio", {"motor": "edge", "tone": "calido", "utterance_id": "u1"})
                voz, _ = await _recibir_hasta(ws, "voz")
                assert voz == {"hablando": True, "motor": "edge"}
                bus.publicar("voz.nivel_boca", {"valor": 1.7})
                boca, _ = await _recibir_hasta(ws, "boca")
                assert boca == {"valor": 1.0}
                bus.publicar("voz.fin", {"utterance_id": "u1", "duracion_s": 1.2, "motor": "edge"})
                voz, _ = await _recibir_hasta(ws, "voz")
                assert voz == {"hablando": False, "motor": "edge"}
                bus.publicar("log", {"nivel": "AVISO", "modulo": "rams.vts", "mensaje": "sin VTS", "ts": 1.0})
                registro, _ = await _recibir_hasta(ws, "log")
                assert registro["mensaje"] == "sin VTS"
                # El HUD no reenvía su propio canal de control ni eventos fuera de la tabla.
                bus.publicar("usuario.entrada", {"texto": "secreto del usuario", "canal": "terminal"})
                vistos = await _recoger(ws, 0.3)
                assert not any("secreto del usuario" in json.dumps(m) for m in vistos)
        finally:
            await hud.cerrar()

    correr(caso())


def test_limites_de_frecuencia_estado_y_log(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                await _recibir_hasta(ws, "hola")
                await _recoger(ws, 0.2)  # vacía la instantánea
                # 5 estados en ráfaga: sale el primero y, coalescido, el último (2 Hz).
                for i in range(5):
                    bus.publicar("homeostasis.estado", _estado(divergencia=1.0 + i / 10))
                estados = [m for m in await _recoger(ws, 1.0) if m["tipo"] == "estado"]
                assert len(estados) == 2
                assert estados[0]["datos"]["estado"]["divergencia"] == pytest.approx(1.0)
                assert estados[-1]["datos"]["estado"]["divergencia"] == pytest.approx(1.4)
                # 25 logs en ráfaga: como mucho 10 por segundo.
                await asyncio.sleep(1.05)
                for i in range(25):
                    bus.publicar("log", {"nivel": "AVISO", "modulo": "rams.test", "mensaje": f"m{i}", "ts": 1.0})
                logs = [m for m in await _recoger(ws, 0.5) if m["tipo"] == "log"]
                assert len(logs) == 10
                assert hud.descartados_log == 15
        finally:
            await hud.cerrar()

    correr(caso())


def test_cerrar_avisa_apagado_y_suelta_clientes(settings_prueba: Settings, bus: BusEventos,
                                                correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        suscriptores_con_hud = bus.estadisticas()["suscriptores"]
        async with _conectar(hud) as ws:
            await _recibir_hasta(ws, "hola")
            assert hud.n_clientes == 1
            await hud.cerrar()
            apagado, _ = await _recibir_hasta(ws, "apagado")
            assert apagado["motivo"] == "cierre"
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
        assert hud.n_clientes == 0 and hud.url is None
        assert bus.estadisticas()["suscriptores"] < suscriptores_con_hud
        await hud.cerrar()  # idempotente

    correr(caso())


# ---------------------------------------------------------------------------
# Traducción bus → HUD (función pura)
# ---------------------------------------------------------------------------


def test_traducir_evento_tabla() -> None:
    assert traducir_evento("telegram.enviado", {"tipo": "recordatorio", "texto": "t"}) == (
        "llamada", {"tipo": "perdida", "texto": "t"})
    assert traducir_evento("telegram.enviado", {"tipo": "megumin", "texto": ""})[1]["tipo"] == "megumin"
    assert traducir_evento("telegram.enviado", {"tipo": "critico", "texto": ""})[1]["tipo"] == "critico"
    assert traducir_evento("telegram.enviado", {"tipo": "respuesta", "texto": ""})[1]["tipo"] == "mensaje"
    assert traducir_evento("telegram.respuesta_llamada", {"llamada_id": 1, "contestada": False}) == (
        "llamada", {"tipo": "rechazada", "texto": ""})
    assert traducir_evento("sistema.apagado", {"motivo": "señal"}) == ("apagado", {"motivo": "señal"})
    tipo, datos = traducir_evento("homeostasis.estado",
                                  {"estado": {"divergencia": 1.048596, "bump": [2.0, -1.0]}})
    assert tipo == "estado" and datos["bump"] == [1.0, 0.0]
    assert traducir_evento("homeostasis.estado", {"bump": []}) is None
    assert traducir_evento("usuario.entrada", {"texto": "hola"}) is None
    assert traducir_evento("hud.control", {"accion": "colgar"}) is None


# ---------------------------------------------------------------------------
# Navegador (sin abrir nada real)
# ---------------------------------------------------------------------------


class _ProcesoFalso:
    def __init__(self, codigo: int = 0) -> None:
        self.codigo = codigo

    async def wait(self) -> int:
        return self.codigo


def _which_falso(*rutas: str) -> Any:
    """``shutil.which`` simulado: solo encuentra los ejecutables de ``rutas`` (por nombre base)."""
    por_nombre = {Path(r).name: r for r in rutas}
    return lambda nombre: por_nombre.get(nombre)


def test_abrir_navegador_linux_chromium_sin_shell(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    lanzados: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def exec_falso(*argv: str, **kw: Any) -> _ProcesoFalso:
        lanzados.append((argv, kw))
        return _ProcesoFalso()

    monkeypatch.setattr(hud_server, "_plataforma", lambda: "linux")
    monkeypatch.setattr(hud_server.shutil, "which", _which_falso("/usr/bin/chromium", "/usr/bin/setsid"))
    monkeypatch.setattr(hud_server.asyncio, "create_subprocess_exec", exec_falso)
    caplog.set_level(logging.DEBUG, logger="rams.hud")

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        assert hud.abrir_navegador() is False  # aún no escucha
        await hud.iniciar()
        try:
            assert hud.abrir_navegador() is True
            await asyncio.sleep(0.05)
            assert len(lanzados) == 1
            argv, kw = lanzados[0]
            # setsid --fork: el navegador queda fuera de RAMS (un transporte de asyncio lo mataría).
            assert argv == ("/usr/bin/setsid", "--fork", "/usr/bin/chromium", f"--app={hud.url}",
                            "--class=amadeus-hud")
            assert "shell" not in kw
            assert hud.abrir_navegador() is True  # justo después: no abre otra ventana
            await asyncio.sleep(0.05)
            assert len(lanzados) == 1
            assert hud.token not in caplog.text
        finally:
            await hud.cerrar()

    correr(caso())


def test_abrir_navegador_linux_xdg_open(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    lanzados: list[tuple[str, ...]] = []

    async def exec_falso(*argv: str, **kw: Any) -> _ProcesoFalso:
        lanzados.append(argv)
        return _ProcesoFalso()

    monkeypatch.setattr(hud_server, "_plataforma", lambda: "linux")
    monkeypatch.setattr(hud_server.shutil, "which", _which_falso("/usr/bin/xdg-open", "/usr/bin/setsid"))
    monkeypatch.setattr(hud_server.asyncio, "create_subprocess_exec", exec_falso)

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            assert hud.abrir_navegador() is True
            await asyncio.sleep(0.05)
            assert lanzados == [("/usr/bin/setsid", "--fork", "/usr/bin/xdg-open", hud.url)]
        finally:
            await hud.cerrar()

    correr(caso())


@pytest.mark.parametrize("setsid", [None, "/usr/bin/setsid"], ids=["sin_setsid", "setsid_sin_fork"])
def test_abrir_navegador_linux_sin_setsid_usa_popen_desacoplado(settings_prueba: Settings, bus: BusEventos,
                                                               correr: Any, monkeypatch: pytest.MonkeyPatch,
                                                               caplog: pytest.LogCaptureFixture,
                                                               setsid: str | None) -> None:
    """Sin setsid (o si no admite --fork, como el de busybox): Popen en sesión nueva y sin shell."""
    ejecutados: list[tuple[str, ...]] = []
    popens: list[tuple[list[str], dict[str, Any]]] = []

    async def exec_falso(*argv: str, **kw: Any) -> _ProcesoFalso:
        ejecutados.append(argv)
        return _ProcesoFalso(1)  # «setsid: unrecognized option '--fork'»

    class _PopenFalso:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            popens.append((list(argv), kw))

        def wait(self, timeout: float | None = None) -> int:
            raise hud_server.subprocess.TimeoutExpired("chromium", timeout or 0)  # ventana viva

    rutas = ["/usr/bin/chromium"] + ([setsid] if setsid else [])
    monkeypatch.setattr(hud_server, "_plataforma", lambda: "linux")
    monkeypatch.setattr(hud_server.shutil, "which", _which_falso(*rutas))
    monkeypatch.setattr(hud_server.asyncio, "create_subprocess_exec", exec_falso)
    monkeypatch.setattr(hud_server.subprocess, "Popen", _PopenFalso)
    caplog.set_level(logging.DEBUG, logger="rams.hud")

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            assert hud.abrir_navegador() is True
            url = hud.url
            async with asyncio.timeout(3):
                while not popens:
                    await asyncio.sleep(0.02)
        finally:
            await hud.cerrar()
        assert len(popens) == 1
        argv, kw = popens[0]
        assert argv == ["/usr/bin/chromium", f"--app={url}", "--class=amadeus-hud"]
        assert kw.get("start_new_session") is True and "shell" not in kw
        assert ejecutados == ([] if setsid is None else
                              [("/usr/bin/setsid", "--fork", "/usr/bin/chromium", f"--app={url}",
                                "--class=amadeus-hud")])
        assert hud.token not in caplog.text

    correr(caso())


@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_cerrar_no_mata_el_navegador_lanzado(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Regresión: el navegador sobrevive a ``cerrar()`` y a la recolección del servidor.

    Un transporte de asyncio mata con SIGKILL al hijo vivo al recolectarse; si RAMS había arrancado
    Chromium, apagar RAMS cerraba todo el navegador del usuario. El «navegador» aquí es un proceso
    Python inofensivo que duerme (nunca un navegador real) y el test lo termina al final.
    """
    psutil = pytest.importorskip("psutil")
    import gc
    import sys
    import uuid

    marca = f"rams-hud-prueba-{uuid.uuid4().hex}"
    argv = [sys.executable, "-c", "import time; time.sleep(30)", marca]
    monkeypatch.setattr(hud_server, "_plataforma", lambda: "linux")
    monkeypatch.setattr(hud_server.shutil, "which", lambda nombre: None)  # sin setsid: ruta Popen
    monkeypatch.setattr(ServidorHUD, "_argv_navegador_linux", lambda self, url: list(argv))

    def buscar() -> list[Any]:
        encontrados = []
        for hijo in psutil.Process().children(recursive=True):
            try:
                if marca in hijo.cmdline():
                    encontrados.append(hijo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return encontrados

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        assert hud.abrir_navegador() is True
        async with asyncio.timeout(10):
            while not buscar():
                await asyncio.sleep(0.05)
        await hud.cerrar()  # justo después de lanzar: antes cancelaba la tarea y perdía el proceso

    try:
        correr(caso())
        gc.collect()
        time.sleep(0.3)
        vivos = [p for p in buscar() if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
        assert vivos, "cerrar() o la recolección mataron el proceso del navegador"
    finally:
        for proceso in buscar():
            try:
                proceso.kill()
                proceso.wait(timeout=5)
            except psutil.Error:
                pass


def test_abrir_navegador_otros_sistemas_usa_webbrowser(settings_prueba: Settings, bus: BusEventos,
                                                       correr: Any, monkeypatch: pytest.MonkeyPatch,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    import webbrowser

    abiertas: list[str] = []

    def abrir_falso(url: str, new: int = 0, autoraise: bool = True) -> bool:
        abiertas.append(url)
        return True

    monkeypatch.setattr(hud_server, "_plataforma", lambda: "win32")
    monkeypatch.setattr(webbrowser, "open", abrir_falso)
    caplog.set_level(logging.DEBUG, logger="rams.hud")

    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        await hud.iniciar()
        try:
            assert hud.abrir_navegador() is True
            assert abiertas == [hud.url]
            assert hud.token not in caplog.text
        finally:
            await hud.cerrar()

    correr(caso())


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_html_seguro_y_fiel() -> None:
    html = RUTA_HTML.read_text(encoding="utf-8")
    assert "innerHTML" not in html
    assert "eval(" not in html
    assert "<title>Amadeus</title>" in html
    assert '<html lang="es">' in html
    assert "<img" not in html.lower()
    assert "<script src" not in html.lower()  # sin librerías JS externas
    for texto in ("SEÑAL PERDIDA", "LLAMADA FINALIZADA", "EN LLAMADA",
                  "Abre el enlace que imprime la terminal (o escribe /hud)",
                  "LÍNEA STEINS;GATE", "CAMPO α", "CAMPO β", "ESTADO ANÓMALO", "K-004",
                  "memory restore", "Salieri consolidó", "Confirmación requerida en la terminal",
                  "prefers-reduced-motion", "Nixie One", "Share Tech Mono", "textContent",
                  "Silenciar", "Cámara", "Colgar"):
        assert texto in html, texto
    for tipo in ("hola", "estado", "linea_mundo", "telemetria", "subtitulo", "voz", "boca", "camara",
                 "presencia", "modulo", "apagado", "llamada", "consolidacion", "permiso", "log",
                 "control_ack"):
        assert f'["{tipo}", ' in html, tipo
    assert "encodeURIComponent(t)" in html and "/ws?t=" in html
    assert html.count("aria-label=") >= 3
