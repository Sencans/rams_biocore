"""Tests del puente con VTube Studio (§10.20) contra un servidor VTS falso local.

Sin red externa ni VTube Studio real: el servidor falso usa ``websockets.asyncio.server.serve``
en ``127.0.0.1:0`` y responde como la API pública de VTS (autenticación y token, hotkeys,
estado y activación de expresiones, disparo de hotkeys, inyección de parámetros y
``APIStateRequest``). Solo usa las *fixtures* de ``tests/conftest.py`` (§12.1): los ``Settings``
salen de ``settings_prueba`` y las corrutinas se ejecutan con ``correr`` (sin pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.server import serve  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from core.bus import BusEventos  # noqa: E402
from core.config import Settings  # noqa: E402
from core.contracts import (  # noqa: E402
    EXP_VALIDOS,
    MOTION_VALIDOS,
    EstadoModulo,
    Etiquetas,
    HomeostaticState,
    SalidaAvatar,
)
from core.etiquetas import sugerir_etiquetas  # noqa: E402
from lobes import vtube_bridge  # noqa: E402
from lobes.vtube_bridge import (  # noqa: E402
    MAPEO_EXP_DEFECTO,
    MAPEO_MOTION_DEFECTO,
    MAX_ERRORES_BOCA,
    VTSError,
    VTubeBridge,
)

T = TypeVar("T")
Correr = Callable[[Awaitable[T]], T]

TOKEN = "tok-falso-vts-0123456789abcdef"
PLUGIN = {"pluginName": "RAMS BioCore", "pluginDeveloper": "Sencans"}
ERROR_OCUPADO = (454, "Parameter MouthOpen is currently controlled by another plugin.")


# ---------------------------------------------------------------------------
# Servidor VTS falso
# ---------------------------------------------------------------------------


def _hotkey(nombre: str, tipo: str, archivo: str, id_hotkey: str) -> dict[str, Any]:
    return {"name": nombre, "type": tipo, "description": nombre, "file": archivo,
            "hotkeyID": id_hotkey, "keyCombination": [], "onScreenButtonID": -1}


class ServidorVTSFalso:
    """Imitación mínima de la API pública de VTube Studio (una sesión por conexión).

    ``congelado``: recibe pero no contesta nada (VTS colgado con el TCP abierto).
    ``retraso_s``: cada respuesta tarda esos segundos (VTS lento pero vivo).
    ``error_boca``: ``(errorID, mensaje)`` con el que se rechaza ``InjectParameterDataRequest``.
    """

    def __init__(self, *, denegar: bool = False, error_boca: tuple[int, str] | None = None,
                 ruido: bool = False) -> None:
        self.denegar = denegar
        self.error_boca = error_boca
        self.ruido = ruido                      # envía un mensaje ajeno antes de cada respuesta
        self.congelado = False
        self.retraso_s = 0.0
        self.model_id = "modelo-amadeus-001"
        self.hotkeys: list[dict[str, Any]] = [
            _hotkey("Smug", "ToggleExpression", "Smug.exp3.json", "hk-smug"),
            _hotkey("Nod", "TriggerAnimation", "Nod.motion3.json", "hk-nod"),
        ]
        self.parametros = {"MouthOpen", "RamsMouthOpen"}
        self.activas: set[str] = set()
        self.recibidos: list[dict[str, Any]] = []     # se puede vaciar en cada test
        self.historial: list[dict[str, Any]] = []     # nunca se vacía
        self.conexiones = 0
        self._abiertas: set[Any] = set()
        self._servidor: Any = None
        self.puerto = 0

    async def __aenter__(self) -> ServidorVTSFalso:
        self._servidor = await serve(self._manejar, "127.0.0.1", 0)
        self.puerto = self._servidor.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._servidor.close()
        await self._servidor.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.puerto}"

    @property
    def archivos_expresion(self) -> list[str]:
        return [h["file"] for h in self.hotkeys if h["file"].endswith(".exp3.json")]

    def mensajes(self, tipo: str, *, todos: bool = False) -> list[dict[str, Any]]:
        fuente = self.historial if todos else self.recibidos
        return [m for m in fuente if m.get("messageType") == tipo]

    async def cortar_conexiones(self) -> None:
        for ws in list(self._abiertas):
            await ws.close()

    async def _manejar(self, ws: Any) -> None:
        self.conexiones += 1
        self._abiertas.add(ws)
        sesion = {"autenticada": False}
        try:
            async for crudo in ws:
                msg = json.loads(crudo)
                msg["_t"] = time.monotonic()
                self.recibidos.append(msg)
                self.historial.append(msg)
                if self.congelado:
                    continue                          # VTS colgado: no contesta nada
                tipo, datos = self._responder(msg.get("messageType"), msg.get("data") or {}, sesion)
                if self.retraso_s > 0:
                    await asyncio.sleep(self.retraso_s)
                if self.ruido:
                    await ws.send(json.dumps({"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                                              "requestID": "evento-ajeno", "messageType": "TestEvent",
                                              "data": {}}))
                await ws.send(json.dumps({
                    "apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
                    "timestamp": int(time.time() * 1000), "requestID": msg.get("requestID"),
                    "messageType": tipo, "data": datos,
                }))
        except ConnectionClosed:
            return  # el cliente cortó: fin normal de la sesión falsa
        finally:
            self._abiertas.discard(ws)

    @staticmethod
    def _error(codigo: int, mensaje: str) -> tuple[str, dict[str, Any]]:
        return "APIError", {"errorID": codigo, "message": mensaje}

    def _buscar_hotkey(self, valor: str) -> dict[str, Any] | None:
        for h in self.hotkeys:
            if h["hotkeyID"] == valor or h["name"].casefold() == str(valor).casefold():
                return h
        return None

    def _responder(self, tipo: str, d: dict[str, Any], sesion: dict[str, bool]) -> tuple[str, dict[str, Any]]:
        if tipo == "APIStateRequest":
            return "APIStateResponse", {"active": True, "vTubeStudioVersion": "1.28.15",
                                        "currentSessionAuthenticated": sesion["autenticada"]}
        if tipo == "AuthenticationTokenRequest":
            if {k: d.get(k) for k in PLUGIN} != PLUGIN:
                return self._error(52, "Plugin name invalid")
            if self.denegar:
                return self._error(50, "User has denied API access for your plugin.")
            return "AuthenticationTokenResponse", {"authenticationToken": TOKEN}
        if tipo == "AuthenticationRequest":
            ok = d.get("authenticationToken") == TOKEN and {k: d.get(k) for k in PLUGIN} == PLUGIN
            sesion["autenticada"] = ok
            return "AuthenticationResponse", {"authenticated": ok,
                                              "reason": "Token valid." if ok else "Token invalid."}
        if not sesion["autenticada"]:
            return self._error(8, "This request requires authentication.")
        if tipo == "HotkeysInCurrentModelRequest":
            return "HotkeysInCurrentModelResponse", {
                "modelLoaded": True, "modelName": "Amadeus K-004", "modelID": self.model_id,
                "availableHotkeys": [dict(h) for h in self.hotkeys]}
        if tipo == "ExpressionStateRequest":
            expresiones = [{"name": f[: -len(".exp3.json")], "file": f, "active": f in self.activas,
                            "deactivateWhenKeyIsLetGo": False, "autoDeactivateAfterSeconds": False,
                            "secondsRemaining": 0, "usedInHotkeys": [], "parameters": []}
                           for f in self.archivos_expresion]
            return "ExpressionStateResponse", {"modelLoaded": True, "modelName": "Amadeus K-004",
                                               "modelID": self.model_id, "expressions": expresiones}
        if tipo == "ExpressionActivationRequest":
            archivo = d.get("expressionFile")
            if archivo not in self.archivos_expresion:
                return self._error(351, "Expression file not found in current model.")
            if d.get("active"):
                self.activas.add(archivo)
            else:
                self.activas.discard(archivo)
            return "ExpressionActivationResponse", {}
        if tipo == "HotkeyTriggerRequest":
            hotkey = self._buscar_hotkey(d.get("hotkeyID", ""))
            if hotkey is None:
                return self._error(202, "Hotkey not found in model.")
            if hotkey["type"] == "ToggleExpression":       # VTS alterna la expresión
                self.activas ^= {hotkey["file"]}
            return "HotkeyTriggerResponse", {"hotkeyID": hotkey["hotkeyID"]}
        if tipo == "InjectParameterDataRequest":
            if self.error_boca is not None:
                return self._error(*self.error_boca)
            for valor in d.get("parameterValues") or []:
                if valor.get("id") not in self.parametros:
                    return self._error(453, "Parameter not found.")
            return "InjectParameterDataResponse", {}
        return self._error(5, f"Unknown request type {tipo}")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _puente(settings_prueba: Settings, url: str, bus: BusEventos | None = None, *,
            habilitado: bool = True) -> VTubeBridge:
    """Puente con los ``Settings`` de la fixture (VTS habilitado y apuntando a ``url``).

    El token y el mapeo usan las rutas por defecto: ``settings_prueba.data_dir`` (en ``tmp_path``).
    """
    return VTubeBridge(settings_prueba.con(vts_habilitado=habilitado, vts_url=url), bus)


def _ruta_token(settings_prueba: Settings) -> Path:
    return settings_prueba.data_dir / "vts_token.txt"


async def _esperar_hasta(condicion: Callable[[], bool], timeout: float = 3.0) -> bool:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        await asyncio.sleep(0.02)
    return condicion()


def _puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _registrar_modulo(bus: BusEventos) -> list[tuple[str, str]]:
    """Lista que se llena con ``(estado, detalle)`` de cada ``sistema.modulo`` publicado."""
    eventos: list[tuple[str, str]] = []
    bus.suscribir("sistema.modulo", lambda e: eventos.append((e.datos["estado"], e.datos["detalle"])))
    return eventos


# ---------------------------------------------------------------------------
# Contratos estáticos
# ---------------------------------------------------------------------------


def test_mapeos_por_defecto_cubren_el_vocabulario() -> None:
    assert set(MAPEO_EXP_DEFECTO) == set(EXP_VALIDOS)
    assert set(MAPEO_MOTION_DEFECTO) == set(MOTION_VALIDOS)
    assert MAPEO_EXP_DEFECTO["neutral"] == [] and MAPEO_MOTION_DEFECTO["idle"] == []
    assert MAPEO_EXP_DEFECTO["smug"] == ["smug", "engreida", "presumida"]
    assert MAPEO_MOTION_DEFECTO["mirar_a_otro_lado"] == ["mirar_lado", "look_away", "lookaway"]


def test_vtserror_lleva_error_id() -> None:
    exc = VTSError(50, "User has denied API access for your plugin.")
    assert isinstance(exc, RuntimeError)
    assert exc.error_id == 50
    assert "50" in str(exc)


def test_implementa_salida_avatar_y_diagnostico_inicial(settings_prueba: Settings) -> None:
    puente = _puente(settings_prueba, "ws://localhost:8001")
    assert isinstance(puente, SalidaAvatar)
    assert puente.en_linea is False
    assert puente.ruta_token == _ruta_token(settings_prueba)
    diag = puente.diagnostico()
    assert diag.clave == "vts"
    assert diag.nombre == "Puente VTube Studio :8001"
    assert diag.estado == EstadoModulo.EN_ESPERA
    assert diag.detalle == "conectando en segundo plano"


def test_sin_websockets_queda_off(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch,
                                  correr: Correr[bool]) -> None:
    monkeypatch.setattr(vtube_bridge, "WEBSOCKETS_DISPONIBLE", False)
    puente = _puente(settings_prueba, "ws://127.0.0.1:9")
    diag = puente.diagnostico()
    assert diag.estado == EstadoModulo.OFF and diag.detalle == "websockets no instalado"

    async def cuerpo() -> bool:
        ok = await puente.conectar()
        await asyncio.wait_for(puente.bucle_reconexion(asyncio.Event()), timeout=1.0)
        await puente.aplicar_etiquetas(Etiquetas("smug", "asentir", "sarcastico"))
        await puente.boca(0.5)
        await puente.cerrar_boca()
        await puente.cerrar()
        return ok

    assert correr(cuerpo()) is False


def test_deshabilitado_queda_off(settings_prueba: Settings, correr: Correr[bool]) -> None:
    puente = _puente(settings_prueba, "ws://127.0.0.1:9", habilitado=False)
    assert puente.diagnostico().estado == EstadoModulo.OFF
    assert puente.diagnostico().detalle == "deshabilitado"
    assert correr(puente.conectar()) is False


# ---------------------------------------------------------------------------
# Conexión y autenticación
# ---------------------------------------------------------------------------


def test_conecta_y_guarda_el_token(settings_prueba: Settings, bus: BusEventos, correr: Correr[None]) -> None:
    eventos: list[dict[str, Any]] = []
    bus.suscribir("sistema.modulo", lambda e: eventos.append(dict(e.datos)))
    ruta = _ruta_token(settings_prueba)

    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url, bus)
            try:
                assert await puente.conectar() is True
                assert puente.en_linea is True
                assert ruta.read_text(encoding="utf-8").strip() == TOKEN
                if os.name == "posix":
                    assert ruta.stat().st_mode & 0o777 == 0o600
                tipos = [m["messageType"] for m in srv.recibidos]
                assert tipos.index("AuthenticationTokenRequest") < tipos.index("AuthenticationRequest")
                assert "HotkeysInCurrentModelRequest" in tipos and "ExpressionStateRequest" in tipos
                assert srv.mensajes("AuthenticationTokenRequest")[0]["data"] == PLUGIN
                for msg in srv.recibidos:          # sobre exacto de la API
                    assert msg["apiName"] == "VTubeStudioPublicAPI"
                    assert msg["apiVersion"] == "1.0"
                    assert isinstance(msg["requestID"], str) and 1 <= len(msg["requestID"]) <= 16
                    assert isinstance(msg["data"], dict)
                diag = puente.diagnostico()
                assert diag.estado == EstadoModulo.OK
                assert "Amadeus K-004" in diag.detalle
            finally:
                await puente.cerrar()
            assert puente.en_linea is False

            # Segunda sesión: reutiliza el token guardado, sin volver a pedirlo.
            srv.recibidos.clear()
            puente2 = _puente(settings_prueba, srv.url, bus)
            try:
                assert await puente2.conectar() is True
                assert srv.mensajes("AuthenticationTokenRequest") == []
                auth = srv.mensajes("AuthenticationRequest")[0]["data"]
                assert auth == {**PLUGIN, "authenticationToken": TOKEN}
            finally:
                await puente2.cerrar()

    correr(cuerpo())
    assert any(e["estado"] == "EN ESPERA" and e["detalle"] == "acepta el plugin «RAMS BioCore» en VTube Studio"
               for e in eventos)
    assert any(e["estado"] == "OK" and e["clave"] == "vts" for e in eventos)


def test_token_invalido_pide_uno_nuevo(settings_prueba: Settings, correr: Correr[None]) -> None:
    ruta = _ruta_token(settings_prueba)
    ruta.write_text("token-caducado-000\n", encoding="utf-8")

    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar() is True
                assert len(srv.mensajes("AuthenticationTokenRequest")) == 1
            finally:
                await puente.cerrar()

    correr(cuerpo())
    assert ruta.read_text(encoding="utf-8").strip() == TOKEN


def test_servidor_apagado_conectar_devuelve_false(settings_prueba: Settings,
                                                  correr: Correr[tuple[bool, float, EstadoModulo]]) -> None:
    puente = _puente(settings_prueba, f"ws://127.0.0.1:{_puerto_libre()}")

    async def cuerpo() -> tuple[bool, float, EstadoModulo]:
        inicio = time.monotonic()
        ok = await puente.conectar()
        duracion = time.monotonic() - inicio
        estado = puente.diagnostico().estado
        # Todos los métodos públicos son no-op sin conexión y no lanzan.
        await puente.aplicar_etiquetas(Etiquetas("smug", "asentir", "sarcastico"))
        await puente.aplicar_estado(HomeostaticState.neutral(), forzar=True)
        assert await puente.expresion("smug") is False
        assert await puente.movimiento("asentir") is False
        await puente.boca(0.5)
        await puente.cerrar_boca()
        await puente.cerrar()
        return ok, duracion, estado

    ok, duracion, estado = correr(cuerpo())
    assert ok is False
    assert puente.en_linea is False
    assert estado == EstadoModulo.SIN_CONEXION
    assert duracion < 6.0


def test_acceso_denegado_no_reintenta_en_10_min(settings_prueba: Settings, caplog: pytest.LogCaptureFixture,
                                                correr: Correr[None]) -> None:
    caplog.set_level(logging.INFO, logger="rams.vts")

    async def cuerpo() -> None:
        async with ServidorVTSFalso(denegar=True) as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar() is False
                diag = puente.diagnostico()
                assert diag.estado == EstadoModulo.SIN_CONEXION
                assert diag.detalle == "acceso denegado por el usuario"
                conexiones = srv.conexiones
                assert await puente.conectar() is False          # bloqueado: ni siquiera conecta
                assert srv.conexiones == conexiones
            finally:
                await puente.cerrar()

    correr(cuerpo())
    assert not _ruta_token(settings_prueba).exists()
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------------------
# Expresiones y movimientos
# ---------------------------------------------------------------------------


def test_aplicar_etiquetas_smug_asentir_y_vuelta_a_neutral(settings_prueba: Settings, correr: Correr[None]) -> None:
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                srv.recibidos.clear()
                await puente.aplicar_etiquetas(Etiquetas("smug", "asentir", "sarcastico"))
                activaciones = [m["data"] for m in srv.mensajes("ExpressionActivationRequest")]
                assert activaciones == [{"expressionFile": "Smug.exp3.json", "fadeTime": 0.25, "active": True}]
                assert [m["data"] for m in srv.mensajes("HotkeyTriggerRequest")] == [{"hotkeyID": "hk-nod"}]
                tipos = [m["messageType"] for m in srv.recibidos]
                assert (tipos.index("ExpressionStateRequest") < tipos.index("ExpressionActivationRequest")
                        < tipos.index("HotkeyTriggerRequest"))
                assert srv.activas == {"Smug.exp3.json"}

                srv.recibidos.clear()
                await puente.aplicar_etiquetas(Etiquetas("neutral", "idle", "analitico"))
                assert [m["data"] for m in srv.mensajes("ExpressionActivationRequest")] == [
                    {"expressionFile": "Smug.exp3.json", "active": False}]
                assert srv.mensajes("HotkeyTriggerRequest") == []
                assert srv.activas == set()
            finally:
                await puente.cerrar()
            # Nunca se re-dispara la hotkey toggle de Smug para apagarla.
            assert all(m["data"]["hotkeyID"] not in ("hk-smug", "Smug")
                       for m in srv.mensajes("HotkeyTriggerRequest", todos=True))

    correr(cuerpo())


def test_misma_expresion_no_repite_y_reactiva_si_vts_la_apago(settings_prueba: Settings,
                                                             correr: Correr[None]) -> None:
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                assert await puente.expresion("smug") is True
                srv.recibidos.clear()
                assert await puente.expresion("smug") is True       # sigue activa: nada que hacer
                assert srv.mensajes("ExpressionActivationRequest") == []
                srv.activas.clear()                                  # el usuario la apagó en VTS
                assert await puente.expresion("smug") is True
                assert [m["data"] for m in srv.mensajes("ExpressionActivationRequest")] == [
                    {"expressionFile": "Smug.exp3.json", "fadeTime": 0.25, "active": True}]
                assert srv.activas == {"Smug.exp3.json"}
                assert srv.mensajes("HotkeyTriggerRequest", todos=True) == []
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_expresion_sin_resolver_se_ignora_con_un_solo_log(settings_prueba: Settings,
                                                         caplog: pytest.LogCaptureFixture,
                                                         correr: Correr[None]) -> None:
    caplog.set_level(logging.INFO, logger="rams.vts")

    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                assert await puente.expresion("molesta") is False
                assert await puente.expresion("molesta") is False
                assert await puente.movimiento("facepalm") is False
                assert await puente.movimiento("idle") is True
                assert await puente.expresion("no_existe") is False
            finally:
                await puente.cerrar()

    correr(cuerpo())
    mensajes = [r.getMessage() for r in caplog.records if r.name == "rams.vts"]
    assert mensajes.count("sin hotkey para EXP «molesta»; ignorando") == 1
    assert mensajes.count("sin hotkey para MOTION «facepalm»; ignorando") == 1


def test_movimientos_respetan_250_ms_y_descartan_mensajes_ajenos(settings_prueba: Settings,
                                                                 correr: Correr[None]) -> None:
    async def cuerpo() -> None:
        async with ServidorVTSFalso(ruido=True) as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                srv.recibidos.clear()
                assert await puente.movimiento("asentir") is True
                assert await puente.movimiento("asentir") is True
                disparos = srv.mensajes("HotkeyTriggerRequest")
                assert len(disparos) == 2
                assert disparos[1]["_t"] - disparos[0]["_t"] >= 0.24
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_mapeo_personalizado(settings_prueba: Settings, correr: Correr[None]) -> None:
    (settings_prueba.data_dir / "vts_mapeo.json").write_text(json.dumps({
        "exp": {"seria": "Smug", "feliz": "NoExiste.exp3.json", "inventada": "X"},
        "motion": {"negar": "Nod"},
        "parametro_boca": "RamsMouthOpen",
    }), encoding="utf-8")

    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                srv.recibidos.clear()
                assert await puente.expresion("seria") is True
                assert [m["data"] for m in srv.mensajes("ExpressionActivationRequest")] == [
                    {"expressionFile": "Smug.exp3.json", "fadeTime": 0.25, "active": True}]
                assert await puente.movimiento("negar") is True
                assert [m["data"]["hotkeyID"] for m in srv.mensajes("HotkeyTriggerRequest")] == ["hk-nod"]
                assert await puente.expresion("feliz") is False
                await puente.boca(0.4)
                assert await _esperar_hasta(lambda: bool(srv.mensajes("InjectParameterDataRequest")))
                inyeccion = srv.mensajes("InjectParameterDataRequest")[0]["data"]
                assert inyeccion["parameterValues"] == [{"id": "RamsMouthOpen", "value": 0.4}]
            finally:
                await puente.cerrar()

    correr(cuerpo())


_ESTADO_SMUG = HomeostaticState(arousal=0.6, focus=0.5, boredom=0.2, fatiga=0.2, curiosidad=0.1, valencia=0.5)


def test_aplicar_estado_respeta_15_s(settings_prueba: Settings, correr: Correr[None]) -> None:
    assert sugerir_etiquetas(_ESTADO_SMUG).exp == "smug"

    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            assert puente.intervalo_estado_s == 15.0
            try:
                assert await puente.conectar()
                await puente.aplicar_estado(_ESTADO_SMUG)
                assert srv.activas == {"Smug.exp3.json"}
                srv.activas.clear()
                srv.recibidos.clear()
                await puente.aplicar_estado(_ESTADO_SMUG)                 # < 15 s: no hace nada
                assert srv.recibidos == []
                await puente.aplicar_estado(_ESTADO_SMUG, forzar=True)    # forzado: relee y reactiva
                assert srv.activas == {"Smug.exp3.json"}
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_aplicar_etiquetas_no_reinicia_el_intervalo_de_aplicar_estado(settings_prueba: Settings,
                                                                     correr: Correr[None]) -> None:
    """§10.20 punto 8: el intervalo cuenta desde la última llamada efectiva de aplicar_estado."""
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            puente.intervalo_estado_s = 0.4
            try:
                assert await puente.conectar()
                await puente.aplicar_estado(_ESTADO_SMUG)                 # reposo: smug
                assert srv.activas == {"Smug.exp3.json"}
                await asyncio.sleep(0.45)
                await puente.aplicar_etiquetas(Etiquetas("neutral", "idle", "analitico"))   # una frase
                assert srv.activas == set()
                await puente.aplicar_estado(_ESTADO_SMUG)                 # vuelve al reposo ya
                assert srv.activas == {"Smug.exp3.json"}
                srv.recibidos.clear()
                await puente.aplicar_estado(_ESTADO_SMUG)                 # < intervalo: nada
                assert srv.recibidos == []
            finally:
                await puente.cerrar()

    correr(cuerpo())


# ---------------------------------------------------------------------------
# Boca
# ---------------------------------------------------------------------------


def test_boca_inyecta_a_30_hz_y_cerrar_boca(settings_prueba: Settings, correr: Correr[None]) -> None:
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                srv.recibidos.clear()
                await puente.boca(0.5)
                await asyncio.sleep(0.3)
                inyecciones = srv.mensajes("InjectParameterDataRequest")
                assert len(inyecciones) >= 3
                assert inyecciones[0]["data"] == {"faceFound": True, "mode": "set",
                                                  "parameterValues": [{"id": "MouthOpen", "value": 0.5}]}
                await puente.cerrar_boca()
                ultima = srv.mensajes("InjectParameterDataRequest")[-1]["data"]
                assert ultima == {"faceFound": False, "mode": "set",
                                  "parameterValues": [{"id": "MouthOpen", "value": 0.0}]}
                total = len(srv.mensajes("InjectParameterDataRequest"))
                await asyncio.sleep(0.15)
                assert len(srv.mensajes("InjectParameterDataRequest")) == total   # la tarea paró

                # Sin boca() reciente la inyección se detiene sola (ventana de 0,5 s).
                await puente.boca(0.3)
                await asyncio.sleep(0.9)
                tras_ventana = len(srv.mensajes("InjectParameterDataRequest"))
                await asyncio.sleep(0.2)
                assert len(srv.mensajes("InjectParameterDataRequest")) == tras_ventana
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_boca_ocupada_por_otro_plugin_se_desactiva_con_un_aviso(settings_prueba: Settings,
                                                               caplog: pytest.LogCaptureFixture,
                                                               correr: Correr[None]) -> None:
    caplog.set_level(logging.WARNING, logger="rams.vts")

    async def cuerpo() -> None:
        async with ServidorVTSFalso(error_boca=ERROR_OCUPADO) as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                srv.recibidos.clear()
                await puente.boca(0.6)
                await asyncio.sleep(0.2)
                assert len(srv.mensajes("InjectParameterDataRequest")) == 1
                await puente.boca(0.7)
                await puente.cerrar_boca()
                await asyncio.sleep(0.1)
                assert len(srv.mensajes("InjectParameterDataRequest")) == 1
                assert puente.en_linea is True       # el resto del avatar sigue funcionando
            finally:
                await puente.cerrar()

    correr(cuerpo())
    avisos = [r for r in caplog.records
              if r.name == "rams.vts" and r.levelno == logging.WARNING and "Otro plugin" in r.getMessage()]
    assert len(avisos) == 1


def test_boca_con_errores_seguidos_se_pausa_hasta_la_proxima_frase(settings_prueba: Settings,
                                                                  caplog: pytest.LogCaptureFixture,
                                                                  correr: Correr[None]) -> None:
    caplog.set_level(logging.INFO, logger="rams.vts")

    def pausas() -> int:
        return sum("pauso la inyección" in r.getMessage() for r in caplog.records if r.name == "rams.vts")

    async def cuerpo() -> None:
        async with ServidorVTSFalso(error_boca=(205, "Cannot inject right now.")) as srv:
            puente = _puente(settings_prueba, srv.url)

            def inyecciones() -> int:
                return len(srv.mensajes("InjectParameterDataRequest"))

            try:
                assert await puente.conectar()
                srv.recibidos.clear()
                fin = time.monotonic() + 0.8
                while time.monotonic() < fin:              # una frase: riko llama a boca() a 30 Hz
                    await puente.boca(0.5)
                    await asyncio.sleep(1 / 30)
                # Tras MAX_ERRORES_BOCA errores seguidos la boca queda en pausa el resto de la frase.
                assert inyecciones() == MAX_ERRORES_BOCA
                assert pausas() == 1
                await puente.cerrar_boca()                 # fin de la frase: un cierre y fin de la pausa
                assert inyecciones() == MAX_ERRORES_BOCA + 1
                assert srv.mensajes("InjectParameterDataRequest")[-1]["data"]["faceFound"] is False
                await puente.boca(0.5)                     # frase nueva: vuelve a intentarlo
                assert await _esperar_hasta(lambda: inyecciones() > MAX_ERRORES_BOCA + 1, 1.0)
                assert puente.en_linea is True
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_boca_con_sesion_no_autenticada_da_la_conexion_por_perdida(settings_prueba: Settings, bus: BusEventos,
                                                                  correr: Correr[None]) -> None:
    eventos = _registrar_modulo(bus)

    async def cuerpo() -> None:
        async with ServidorVTSFalso(error_boca=(8, "This request requires authentication.")) as srv:
            puente = _puente(settings_prueba, srv.url, bus)
            try:
                assert await puente.conectar()
                await puente.boca(0.5)
                assert await _esperar_hasta(lambda: not puente.en_linea, 2.0)
                assert puente.diagnostico().estado == EstadoModulo.SIN_CONEXION
                assert puente.diagnostico().detalle == "sesión no autenticada"
            finally:
                await puente.cerrar()

    correr(cuerpo())
    assert eventos.count(("SIN CONEXIÓN", "sesión no autenticada")) == 1


# ---------------------------------------------------------------------------
# Reconexión, keepalive, VTS colgado y cierre
# ---------------------------------------------------------------------------


def test_bucle_reconexion_keepalive_cambio_de_modelo_y_reconexion(settings_prueba: Settings, bus: BusEventos,
                                                                   correr: Correr[None]) -> None:
    eventos = _registrar_modulo(bus)

    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url, bus)
            puente.intervalo_keepalive_s = 0.1
            parar = asyncio.Event()
            tarea = asyncio.create_task(puente.bucle_reconexion(parar, base_s=0.05, max_s=0.2))
            try:
                assert await _esperar_hasta(lambda: puente.en_linea, 5.0)
                assert await puente.expresion("molesta") is False
                # El usuario carga otro modelo con una expresión "Angry".
                srv.model_id = "modelo-amadeus-002"
                srv.hotkeys.append(_hotkey("Angry", "ToggleExpression", "Angry.exp3.json", "hk-angry"))
                assert await _esperar_hasta(lambda: bool(srv.mensajes("APIStateRequest")), 3.0)
                limite = time.monotonic() + 3.0
                while not await puente.expresion("molesta") and time.monotonic() < limite:
                    await asyncio.sleep(0.05)
                assert "Angry.exp3.json" in srv.activas

                # VTS corta la conexión: el bucle lo detecta y vuelve a conectar.
                await srv.cortar_conexiones()
                assert await _esperar_hasta(lambda: srv.conexiones >= 2 and puente.en_linea, 5.0)
                assert any(estado == "SIN CONEXIÓN" for estado, _detalle in eventos)

                parar.set()
                await asyncio.wait_for(tarea, timeout=3.0)
            finally:
                if not tarea.done():
                    tarea.cancel()
                    await asyncio.gather(tarea, return_exceptions=True)
                await puente.cerrar()

    correr(cuerpo())


def test_vts_colgado_se_detecta_aunque_quien_llama_corte_antes(settings_prueba: Settings, bus: BusEventos,
                                                               correr: Correr[float]) -> None:
    """VTS deja de contestar con el TCP abierto y riko abandona cada llamada antes del timeout."""
    eventos = _registrar_modulo(bus)

    async def cuerpo() -> float:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url, bus)
            puente.timeout_peticion_s = 1.0           # los presupuestos de "riko" son todos menores
            puente.intervalo_keepalive_s = 30.0       # sin bucle: el keepalive no puede detectarlo
            try:
                assert await puente.conectar()
                srv.congelado = True
                inicio = time.monotonic()
                while puente.en_linea and time.monotonic() - inicio < 4.0:
                    # Un segmento de voz como en riko: etiquetas (0,2 s), boca y cerrar_boca (0,45 s).
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            puente.aplicar_etiquetas(Etiquetas("smug", "asentir", "sarcastico")), 0.2)
                    for _ in range(3):
                        await puente.boca(0.5)
                        await asyncio.sleep(1 / 30)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(puente.cerrar_boca(), 0.45)
                detectado = time.monotonic() - inicio
                assert puente.en_linea is False
                diag = puente.diagnostico()
                assert diag.estado == EstadoModulo.SIN_CONEXION
                assert diag.detalle == "VTube Studio no responde"
                # Ya fuera de línea, un segmento nuevo no espera a VTS.
                t = time.monotonic()
                await puente.aplicar_etiquetas(Etiquetas("smug", "asentir", "sarcastico"))
                await puente.boca(0.5)
                await puente.cerrar_boca()
                assert time.monotonic() - t < 0.1
                return detectado
            finally:
                await puente.cerrar()

    detectado = correr(cuerpo())
    assert detectado < 2.0              # ~1 s (el plazo de la primera petición sin respuesta)
    assert eventos.count(("SIN CONEXIÓN", "VTube Studio no responde")) == 1


def test_vts_lento_pero_vivo_no_se_da_por_perdido(settings_prueba: Settings, correr: Correr[None]) -> None:
    """Peticiones abandonadas cuyas respuestas llegan tarde no cuentan como VTS colgado."""
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            puente.timeout_peticion_s = 0.5
            try:
                assert await puente.conectar()
                srv.retraso_s = 0.3                   # más que el presupuesto de quien llama (0,1 s)
                abandonadas = 0
                limite = time.monotonic() + 1.5
                while time.monotonic() < limite:
                    try:
                        await asyncio.wait_for(puente.expresion("smug"), 0.1)
                    except TimeoutError:
                        abandonadas += 1
                assert abandonadas >= 5
                assert puente.en_linea is True
                assert puente.diagnostico().estado == EstadoModulo.OK
                srv.retraso_s = 0.0
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_reintentos_con_vts_cerrado_no_repiten_eventos(settings_prueba: Settings, bus: BusEventos,
                                                       monkeypatch: pytest.MonkeyPatch,
                                                       correr: Correr[None]) -> None:
    """§5 "por cambio": con VTS cerrado, cada reintento no publica dos sistema.modulo."""
    intentos: list[str] = []

    async def conectar_rechazado(url: str, **_opciones: Any) -> Any:
        intentos.append(url)
        raise ConnectionRefusedError("conexión rechazada")

    monkeypatch.setattr(vtube_bridge, "_ws_connect", conectar_rechazado)
    eventos = _registrar_modulo(bus)
    puente = _puente(settings_prueba, "ws://127.0.0.1:8001", bus)

    async def cuerpo() -> None:
        parar = asyncio.Event()
        tarea = asyncio.create_task(puente.bucle_reconexion(parar, base_s=0.05, max_s=0.05))
        try:
            assert await _esperar_hasta(lambda: len(intentos) >= 6, 3.0)
        finally:
            parar.set()
            await asyncio.wait_for(tarea, timeout=2.0)
            await puente.cerrar()

    correr(cuerpo())
    assert [e for e in eventos if e[0] == "SIN CONEXIÓN"] == [
        ("SIN CONEXIÓN", "VTube Studio no responde"),
        ("SIN CONEXIÓN", "VTube Studio no responde · reintento en 0 s"),
    ]


def test_conexion_cortada_no_lanza_y_queda_sin_conexion(settings_prueba: Settings, correr: Correr[None]) -> None:
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            try:
                assert await puente.conectar()
                await srv.cortar_conexiones()
                await asyncio.sleep(0.1)
                await puente.aplicar_etiquetas(Etiquetas("smug", "asentir", "sarcastico"))
                await puente.boca(0.5)
                await puente.cerrar_boca()
                assert puente.en_linea is False
                assert puente.diagnostico().estado == EstadoModulo.SIN_CONEXION
            finally:
                await puente.cerrar()

    correr(cuerpo())


def test_bucle_reconexion_termina_con_parar_sin_servidor(settings_prueba: Settings, correr: Correr[None]) -> None:
    puente = _puente(settings_prueba, f"ws://127.0.0.1:{_puerto_libre()}")

    async def cuerpo() -> None:
        parar = asyncio.Event()
        tarea = asyncio.create_task(puente.bucle_reconexion(parar, base_s=0.05, max_s=0.1))
        await asyncio.sleep(0.3)
        parar.set()
        await asyncio.wait_for(tarea, timeout=6.0)
        assert puente.en_linea is False
        await puente.cerrar()

    correr(cuerpo())


def test_cerrar_devuelve_el_avatar_a_reposo(settings_prueba: Settings, correr: Correr[None]) -> None:
    async def cuerpo() -> None:
        async with ServidorVTSFalso() as srv:
            puente = _puente(settings_prueba, srv.url)
            assert await puente.conectar()
            await puente.expresion("smug")
            assert srv.activas == {"Smug.exp3.json"}
            srv.recibidos.clear()
            await puente.cerrar()
            await puente.cerrar()                       # idempotente
            assert srv.activas == set()
            assert srv.mensajes("InjectParameterDataRequest")[-1]["data"]["faceFound"] is False
            assert puente.en_linea is False
            assert puente.diagnostico().estado == EstadoModulo.OFF

    correr(cuerpo())
