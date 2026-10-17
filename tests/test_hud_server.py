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
import os
import shutil
import socket
import subprocess
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


def test_traducir_evento_v02() -> None:
    """§C2.4.6: vision.estado, pantalla.estado (sin título si privada), pc.accion y vision.camara{fuente}."""
    assert traducir_evento("vision.camara", {"activa": True, "habilitada": True, "duracion_ms": 0}) == (
        "camara", {"activa": True, "habilitada": True, "duracion_ms": 0, "fuente": "vistazo"})
    assert traducir_evento("vision.camara", {"activa": True, "habilitada": True, "duracion_ms": 0,
                                             "fuente": "continua"})[1]["fuente"] == "continua"
    assert traducir_evento("vision.camara", {"activa": False, "fuente": "rara"})[1]["fuente"] == "vistazo"

    tipo, vision = traducir_evento("vision.estado", {
        "camara": "continua", "pantalla": "metadatos", "fps_camara": 3.0, "fps_pantalla": 1.0, "nivel": 0,
        "detector": "yunet", "presente": True, "atencion": 1.7})
    assert tipo == "vision"
    assert vision == {"camara": "continua", "pantalla": "metadatos", "fps_camara": 3.0, "fps_pantalla": 1.0,
                      "nivel": 0, "detector": "yunet", "presente": True, "atencion": 1.0}
    assert traducir_evento("vision.estado", {"camara": "off", "presente": None})[1]["presente"] is None

    base = {"cambio": 0.1, "brillo": 0.4, "tono": 210, "video": True, "pantalla_completa": False,
            "reproduciendo": None, "no_molestar": False, "clase": "chrome", "titulo": "YouTube - Steins;Gate OP"}
    tipo, pantalla = traducir_evento("pantalla.estado", {**base, "privada": False})
    assert tipo == "pantalla" and pantalla["titulo"] == "YouTube - Steins;Gate OP"
    assert pantalla["reproduciendo"] is None and pantalla["video"] is True and pantalla["clase"] == "chrome"
    _tipo, privada = traducir_evento("pantalla.estado", {**base, "privada": True, "titulo": "Bitwarden"})
    assert privada["privada"] is True and "titulo" not in privada
    assert "Bitwarden" not in json.dumps(privada)

    assert traducir_evento("pc.accion", {"tipo": "prerrellenar", "resultado": "ok", "destino": "whatsapp",
                                         "texto": "nunca"}) == (
        "accion", {"tipo": "prerrellenar", "resultado": "ok", "destino": "whatsapp"})
    assert traducir_evento("pc.accion", {"tipo": "escribir_texto", "resultado": "abortada"})[1]["destino"] is None

    # La vista previa de un permiso nunca sale por /ws, y el chat/sistema del escritorio tampoco.
    _tipo, permiso = traducir_evento("permiso.solicitado", {"id": "a1b2c3d4", "descripcion": "d",
                                                            "destructiva": False, "vista_previa": "hola mamá"})
    assert "vista_previa" not in permiso
    assert traducir_evento("escritorio.chat", {"texto": "x"}) is None
    assert traducir_evento("escritorio.sistema", {"texto": "x"}) is None
    assert traducir_evento("escritorio.orden", {"accion": "aparecer"}) is None
    for nombre in ("vision.estado", "pantalla.estado", "pc.accion"):
        assert nombre in hud_server.EVENTOS_SUSCRITOS
    for nombre in ("escritorio.chat", "escritorio.sistema"):
        assert nombre not in hud_server.EVENTOS_SUSCRITOS


def test_ws_reenvia_vision_pantalla_accion_y_led_por_fuente(settings_prueba: Settings, bus: BusEventos,
                                                            correr: Any) -> None:
    async def caso() -> None:
        hud = _hud(settings_prueba, bus)
        # Antes de iniciar: cámara continua abierta y un vistazo ya cerrado.
        bus.publicar("vision.camara", {"activa": True, "habilitada": True, "duracion_ms": 0, "fuente": "continua"})
        bus.publicar("vision.camara", {"activa": False, "habilitada": True, "duracion_ms": 120})
        bus.publicar("vision.estado", {"camara": "continua", "pantalla": "off", "fps_camara": 3.0,
                                       "fps_pantalla": 0.0, "nivel": 0, "detector": "haar", "presente": True,
                                       "atencion": 0.5})
        await hud.iniciar()
        try:
            async with _conectar(hud) as ws:
                vistos = await _recoger(ws, 0.4)
                camaras = [m["datos"] for m in vistos if m["tipo"] == "camara"]
                # Una por fuente: el HUD calcula el LED como OR (la continua sigue encendida).
                assert {(c["fuente"], c["activa"]) for c in camaras} == {("vistazo", False), ("continua", True)}
                vision = next(m["datos"] for m in vistos if m["tipo"] == "vision")
                assert vision["camara"] == "continua" and vision["detector"] == "haar"

                bus.publicar("pantalla.estado", {"cambio": 0.0, "brillo": 0.3, "tono": None, "video": False,
                                                 "pantalla_completa": None, "reproduciendo": None,
                                                 "no_molestar": False, "privada": True, "clase": "keepassxc",
                                                 "titulo": ""})
                pantalla, _ = await _recibir_hasta(ws, "pantalla")
                assert pantalla["privada"] is True and "titulo" not in pantalla
                bus.publicar("pc.accion", {"tipo": "abrir_destino", "resultado": "ok", "destino": "youtube"})
                accion, _ = await _recibir_hasta(ws, "accion")
                assert accion == {"tipo": "abrir_destino", "resultado": "ok", "destino": "youtube"}
        finally:
            await hud.cerrar()

    correr(caso())


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


def test_html_chat_rine_e_indicadores_v02() -> None:
    """§C2.4 / §C2.13: panel RINE seguro, LED = OR por fuente e indicador 👁."""
    html = RUTA_HTML.read_text(encoding="utf-8")
    assert "innerHTML" not in html and "outerHTML" not in html and "insertAdjacentHTML" not in html
    assert "document.write" not in html and "eval(" not in html and "new Function" not in html
    # Mensajes nuevos de /ws.
    for tipo in ("vision", "pantalla", "accion", "escritorio"):
        assert f'["{tipo}", ' in html, tipo
    # Panel RINE: segundo cliente de /escritorio con el token que llega por /ws.
    for texto in ('id="rine"', 'id="rine-lista"', 'id="rine-form"', 'maxlength="2000"', "RINE",
                  "/escritorio?t=${encodeURIComponent(chat.token)}", 'tipo: "confirmacion"', 'tipo: "chat"',
                  "/^[0-9a-f]{8}$/", '"noopener noreferrer"', 'protocol !== "https:"', "vista-previa",
                  "no_molestar_on", "basta_travesuras", "pausar_vision", 'role="log"'):
        assert texto in html, texto
    for tipo in ("hola", "subtitulo", "voz", "permiso", "sistema", "travesuras"):
        assert f'["{tipo}", ' in html, tipo
    # El token nunca se pinta: solo se usa para abrir el WebSocket.
    assert "chat.token;" not in html and "textContent = chat.token" not in html
    # LED = OR de fuentes e indicador de visión.
    assert "camaraFuentes.vistazo || camaraFuentes.continua" in html
    assert 'id="ojo"' in html and "👁" in html



# ---------------------------------------------------------------------------
# Panel RINE de verdad: el HUD cargado en jsdom (Node) contra un ServidorHUD propio en
# 127.0.0.1:0. Opcional: se salta si no hay ``node`` o ``jsdom`` (``npm i jsdom`` en una carpeta y
# ``RAMS_TEST_NODE_PATH=<carpeta>/node_modules``). jsdom no carga recursos externos (fuentes) ni
# abre ventanas: nada sale de 127.0.0.1.
# ---------------------------------------------------------------------------

_HARNESS_JSDOM = r"""
"use strict";
const { JSDOM, VirtualConsole } = require("jsdom");
const [url, modo, tokenEscritorio] = process.argv.slice(2);
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));
const errores = [];
const avisos = [];
function paso(nombre, datos) {
  process.stdout.write(JSON.stringify(Object.assign({ paso: nombre }, datos || {})) + "\n");
}
async function hasta(cond, ms, nombre) {
  const fin = Date.now() + ms;
  while (Date.now() < fin) {
    try { if (cond()) { return; } } catch (e) { /* aún no */ }
    await esperar(20);
  }
  throw new Error("tiempo agotado: " + nombre);
}
(async () => {
  const consola = new VirtualConsole();
  consola.on("jsdomError", (e) => errores.push(String(e && e.message)));
  consola.on("error", (...a) => errores.push(a.map(String).join(" ")));
  consola.on("warn", (...a) => avisos.push(a.map(String).join(" ")));
  const dom = await JSDOM.fromURL(url, { runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: consola });
  const w = dom.window;
  const d = w.document;
  const $ = (id) => d.getElementById(id);
  const enLinea = () => $("rine-estado").textContent === "en línea";
  const pulsado = () => $("rine-no-molestar").getAttribute("aria-pressed");
  const permisos = () => Array.from(d.querySelectorAll("#rine-lista .msg.permiso")).map((li) => ({
    texto: li.querySelector(".burbuja").textContent,
    aprobar: li.querySelector(".aprobar") ? li.querySelector(".aprobar").disabled : null,
    resolucion: (li.querySelector(".resolucion") || {}).textContent || ""
  }));
  await hasta(() => $("llamada-texto").textContent === "EN LLAMADA", 8000, "en llamada");
  if (modo === "sin_chat") {
    await esperar(600);
    paso("fin", { lanzador_oculto: $("rine-lanzador").hidden, panel_oculto: $("rine").hidden,
                  estado: $("rine-estado").textContent, errores, avisos });
    w.close();
    return;
  }
  await hasta(enLinea, 8000, "chat en línea");
  await hasta(() => pulsado() === "true", 3000, "no molestar desde la instantánea");
  paso("conectado", { lanzador_oculto: $("rine-lanzador").hidden, pulsado: pulsado(),
                      token_en_dom: d.documentElement.outerHTML.includes(tokenEscritorio) });
  $("rine-no-molestar").click();
  paso("clic1", { pulsado: pulsado() });
  await hasta(() => pulsado() === "true", 5000, "no molestar reactivado por otro cliente");
  $("rine-no-molestar").click();
  paso("clic2", { pulsado: pulsado() });

  await hasta(() => d.querySelector("#rine-lista .msg.permiso .aprobar"), 8000, "permiso A");
  const ella = Array.from(d.querySelectorAll("#rine-lista .msg.ella .burbuja")).pop();
  const datos = {
    texto_ella: ella ? ella.textContent : null,
    enlaces: ella ? Array.from(ella.querySelectorAll("a")).map((a) => ({ href: a.href, rel: a.rel, target: a.target })) : [],
    sistema: Array.from(d.querySelectorAll("#rine-lista .msg.sistema .burbuja")).map((b) => b.textContent),
    html_inyectado: d.querySelectorAll("#rine-lista b, #rine-lista script, #rine-lista img").length,
    vista: (d.querySelector("#rine-lista .msg.permiso .vista-previa") || {}).textContent || null
  };
  d.querySelector("#rine-lista .msg.permiso .aprobar").click();
  paso("aprobado", datos);

  await hasta(() => permisos().length === 2 && permisos()[1].aprobar === false &&
                    permisos()[0].resolucion === "Aprobada.", 8000, "permiso B");
  paso("segundo", { permisos: permisos() });
  await hasta(() => !enLinea(), 5000, "desconexión 1");
  await hasta(enLinea, 8000, "reconexión 1");
  await hasta(() => permisos().length === 3, 3000, "permiso C");
  await esperar(200);
  paso("reconectado", { permisos: permisos() });
  await hasta(() => !enLinea(), 5000, "desconexión 2");
  await hasta(enLinea, 8000, "reconexión 2");
  await esperar(300);
  paso("revivido", { permisos: permisos(), errores, avisos });
  w.close();
})().catch((e) => {
  paso("fallo", { error: String(e && e.stack || e), errores, avisos });
  process.exit(1);
});
"""


def _entorno_jsdom() -> tuple[str, dict[str, str]] | None:
    """``(node, entorno)`` si ``node`` resuelve ``jsdom`` (NODE_PATH: ``RAMS_TEST_NODE_PATH``,
    ``NODE_PATH`` y los ``node_modules`` del proyecto); si no, ``None``."""
    node = shutil.which("node")
    if node is None:
        return None
    raiz = Path(__file__).resolve().parents[1]
    rutas = [os.environ.get("RAMS_TEST_NODE_PATH", ""), os.environ.get("NODE_PATH", ""),
             str(raiz / "node_modules"), str(raiz / "desktop" / "node_modules")]
    entorno = dict(os.environ)
    entorno["NODE_PATH"] = os.pathsep.join(r for r in rutas if r)
    try:
        resultado = subprocess.run([node, "-e", "require.resolve('jsdom')"], env=entorno,
                                   capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (node, entorno) if resultado.returncode == 0 else None


async def _hud_en_jsdom(hud: ServidorHUD, modo: str, token: str, tmp_path: Path,
                        node: str, entorno: dict[str, str]) -> tuple[Any, Any]:
    """Lanza el HUD en jsdom; devuelve el proceso y ``paso(nombre)`` para leer sus hitos."""
    script = tmp_path / "hud_jsdom.cjs"
    script.write_text(_HARNESS_JSDOM, encoding="utf-8")
    proceso = await asyncio.create_subprocess_exec(
        node, str(script), str(hud.url), modo, token, cwd=str(tmp_path), env=entorno,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def paso(nombre: str, timeout: float = 20.0) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            while True:
                linea = await proceso.stdout.readline()
                if not linea:
                    error = (await proceso.stderr.read()).decode("utf-8", "replace")[-2000:]
                    pytest.fail(f"jsdom terminó antes de «{nombre}»: {error}")
                try:
                    datos = json.loads(linea)
                except ValueError:
                    continue  # salida de consola ajena al protocolo
                if not isinstance(datos, dict):
                    continue
                if datos.get("paso") == "fallo":
                    pytest.fail(f"jsdom falló antes de «{nombre}»: {datos}")
                if datos.get("paso") == nombre:
                    return datos

    return proceso, paso


async def _terminar(proceso: Any) -> None:
    """Espera a que termine el proceso de jsdom (o lo mata) y vacía sus tuberías."""
    try:
        await asyncio.wait_for(proceso.communicate(), timeout=5)
    except TimeoutError:
        proceso.kill()
        await proceso.communicate()


def _cortar_paneles(hud: ServidorHUD) -> None:
    """Corta desde el servidor los paneles del HUD conectados a ``/escritorio`` (sin cierre limpio)."""
    assert hud.escritorio is not None
    for cliente in list(hud.escritorio._huds.values()):
        cliente.conexion.transport.abort()


def test_rine_en_jsdom_no_molestar_saneado_y_permisos(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                      tmp_path: Path) -> None:
    """Regresión del panel RINE (revisión escritorio-ws): token de /ws, «No molestar» derivado del
    estado real, texto y enlaces saneados, botones ligados al id y reconciliación tras reconectar."""
    jsdom = _entorno_jsdom()
    if jsdom is None:
        pytest.skip("sin node o sin jsdom (npm i jsdom; RAMS_TEST_NODE_PATH=<carpeta>/node_modules)")
    node, entorno = jsdom
    controles: list[dict[str, Any]] = []
    confirmaciones: list[dict[str, Any]] = []
    bus.suscribir("escritorio.control", lambda e: controles.append(dict(e.datos)))
    bus.suscribir("escritorio.confirmacion", lambda e: confirmaciones.append(dict(e.datos)))

    async def esperar(cond: Any, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.01)

    async def caso() -> None:
        # Estado previo: el usuario activó «No molestar» (p. ej. desde la bandeja de Electron) antes
        # de que el HUD cargara: el botón tiene que salir pulsado.
        bus.publicar("pc.no_molestar", {"activo": True, "motivo": "usuario"})
        hud = ServidorHUD(settings_prueba.con(hud_habilitado=True, hud_puerto=0, escritorio_habilitado=True,
                                              hud_chat=True), bus)
        await hud.iniciar()
        proceso = None
        try:
            token = json.loads(Path(settings_prueba.escritorio_token_file).read_text(encoding="utf-8"))["token"]
            proceso, paso = await _hud_en_jsdom(hud, "chat", token, tmp_path, node, entorno)
            datos = await paso("conectado")
            assert datos["lanzador_oculto"] is False and datos["pulsado"] == "true"
            assert datos["token_en_dom"] is False

            # 1.er clic: con el estado real «activo», el HUD manda «off» (no otro «on»).
            assert (await paso("clic1"))["pulsado"] == "false"
            await esperar(lambda: controles == [{"accion": "no_molestar_off"}])
            bus.publicar("pc.no_molestar", {"activo": False, "motivo": ""})        # main lo aplica
            bus.publicar("pc.no_molestar", {"activo": True, "motivo": "usuario"})  # la bandeja lo reactiva
            assert (await paso("clic2"))["pulsado"] == "false"
            await esperar(lambda: len(controles) == 2)
            assert controles[1] == {"accion": "no_molestar_off"}

            # Texto y enlaces saneados; botones ligados al id.
            bus.publicar("salida.subtitulo", {"texto": "Mira <b>esto</b>: https://example.org/a?b=1. "
                                                       "Y javascript:alert(1)", "exp": "feliz",
                                              "canal": "terminal", "origen": "respuesta"})
            bus.publicar("escritorio.sistema", {"texto": "Estado: <img src=x onerror=alert(1)>"})
            bus.publicar("permiso.solicitado", {"id": "a1b2c3d4", "descripcion": "Acción A", "destructiva": False,
                                                "tipo": "prerrellenar", "vista_previa": "hola <script>x</script>"})
            datos = await paso("aprobado")
            assert datos["html_inyectado"] == 0
            assert "<b>esto</b>" in datos["texto_ella"] and "javascript:alert(1)" in datos["texto_ella"]
            assert datos["enlaces"] == [{"href": "https://example.org/a?b=1", "rel": "noopener noreferrer",
                                         "target": "_blank"}]
            assert "Estado: <img src=x onerror=alert(1)>" in datos["sistema"]
            assert datos["vista"] == "hola <script>x</script>"
            await esperar(lambda: confirmaciones == [{"id": "a1b2c3d4", "aprobado": True}])

            bus.publicar("permiso.resuelto", {"id": "a1b2c3d4", "aprobado": True, "motivo": "aprobada"})
            bus.publicar("permiso.solicitado", {"id": "0badc0de", "descripcion": "Acción B", "destructiva": False,
                                                "tipo": "abrir_app"})
            datos = await paso("segundo")
            assert [p["aprobar"] for p in datos["permisos"]] == [True, False]

            # Sin conexión, B se resuelve en otro lado y llega C: al reconectar, B queda caducada (no
            # pulsable) y C aparece vigente.
            _cortar_paneles(hud)
            await asyncio.sleep(0.05)
            bus.publicar("permiso.resuelto", {"id": "0badc0de", "aprobado": False, "motivo": "sin respuesta"})
            bus.publicar("permiso.solicitado", {"id": "cafe1234", "descripcion": "Acción C", "destructiva": False,
                                                "tipo": "abrir_app"})
            a, b, c = (await paso("reconectado"))["permisos"]
            assert a["resolucion"] == "Aprobada." and a["aprobar"] is True
            assert b["resolucion"] == "Ya no está pendiente." and b["aprobar"] is True
            assert "Acción C" in c["texto"] and c["aprobar"] is False and c["resolucion"] == ""

            # Otra reconexión sin cambios: C sigue vigente en la misma burbuja (sin duplicados).
            _cortar_paneles(hud)
            datos = await paso("revivido")
            assert len(datos["permisos"]) == 3
            assert datos["permisos"][2]["aprobar"] is False and datos["permisos"][2]["resolucion"] == ""
            assert datos["errores"] == [] and datos["avisos"] == []
        finally:
            if proceso is not None:
                await _terminar(proceso)
            await hud.cerrar()

    correr(caso())


@pytest.mark.parametrize("cambios", [{"hud_chat": False}, {"escritorio_habilitado": False}],
                         ids=["sin_hud_chat", "sin_escritorio"])
def test_rine_en_jsdom_sin_chat_no_muestra_el_lanzador(settings_prueba: Settings, bus: BusEventos, correr: Any,
                                                       tmp_path: Path, cambios: dict[str, Any]) -> None:
    """§C1.5: sin ``RAMS_HUD_CHAT=1`` (o sin /escritorio) no hay lanzador ni panel RINE."""
    jsdom = _entorno_jsdom()
    if jsdom is None:
        pytest.skip("sin node o sin jsdom (npm i jsdom; RAMS_TEST_NODE_PATH=<carpeta>/node_modules)")
    node, entorno = jsdom

    async def caso() -> None:
        hud = ServidorHUD(settings_prueba.con(hud_habilitado=True, hud_puerto=0, **cambios), bus)
        await hud.iniciar()
        proceso = None
        try:
            proceso, paso = await _hud_en_jsdom(hud, "sin_chat", "-", tmp_path, node, entorno)
            datos = await paso("fin")
            assert datos["lanzador_oculto"] is True and datos["panel_oculto"] is True
            assert datos["errores"] == [] and datos["avisos"] == []
        finally:
            if proceso is not None:
                await _terminar(proceso)
            await hud.cerrar()

    correr(caso())
