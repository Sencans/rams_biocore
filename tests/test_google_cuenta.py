"""Tests de ``lobes/google_cuenta.py`` (v0.2.2, ``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §6 y §18).

Sin red ni credenciales reales: Google es un ``httpx.MockTransport``; el OAuth usa el servidor *loopback*
real en ``127.0.0.1:0`` y un «navegador» inyectado (``webbrowser.open`` está prohibido por ``conftest``)
que hace el GET del callback con un ``httpx.Client`` local. Todos los archivos van a ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from core.bus import BusEventos
from lobes import google_cuenta as gc
from lobes.google_cuenta import (
    SCOPE_DRIVE,
    SCOPE_DRIVE_FILE,
    SCOPE_DRIVE_READONLY,
    SCOPE_GMAIL_SEND,
    CuentaGoogle,
    ErrorGoogle,
    EstadoGoogle,
    clasificar_scopes,
    enmascarar_correo,
    protector_por_defecto,
    scopes_pedidos,
)

CLIENT_ID = "123456789-falso.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-secreto-falso-000000"
ACCESO = "ya29.acceso-falso-" + "a" * 30
ACCESO_2 = "ya29.acceso-renovado-" + "c" * 30
REFRESH = "1//refresh-falso-" + "b" * 30
CODE = "4/codigo-de-autorizacion-falso-0123456789"
CUENTA = "juan.perez@gmail.com"
TODOS = f"{SCOPE_GMAIL_SEND} {SCOPE_DRIVE_READONLY} {SCOPE_DRIVE_FILE}"


class ProtectorFalso:
    """Protector de prueba (XOR): el refresh token nunca queda en claro en el archivo."""

    nombre = "falso"

    def proteger(self, datos: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in datos)

    def desproteger(self, datos: bytes) -> bytes:
        return bytes(b ^ 0x5A for b in datos)


class ProtectorRoto:
    nombre = "dpapi"

    def proteger(self, datos: bytes) -> bytes:
        raise OSError("DPAPI no disponible")

    def desproteger(self, datos: bytes) -> bytes:
        raise OSError("DPAPI no disponible")


class FalsoGoogle:
    """Doble de Google: token, revocación, about y lo que añada cada test (``extra``)."""

    def __init__(self) -> None:
        self.peticiones: list[httpx.Request] = []
        self.formularios: list[dict[str, list[str]]] = []
        self.scope = TODOS
        self.respuesta_refresco: Callable[[], httpx.Response] = lambda: httpx.Response(
            200, json={"access_token": ACCESO_2, "expires_in": 3599, "token_type": "Bearer"})
        self.estado_revocar = 200
        self.extra: Callable[[httpx.Request], httpx.Response | None] | None = None
        self.retardo = 0.0
        self.caida = False

    async def __call__(self, peticion: httpx.Request) -> httpx.Response:
        await peticion.aread()
        self.peticiones.append(peticion)
        if self.caida:
            raise httpx.ConnectError("sin red", request=peticion)
        if self.retardo:
            await asyncio.sleep(self.retardo)
        if self.extra is not None:
            respuesta = self.extra(peticion)
            if respuesta is not None:
                return respuesta
        url = peticion.url
        if peticion.method == "POST" and url.host == "oauth2.googleapis.com" and url.path == "/token":
            formulario = parse_qs(peticion.content.decode("utf-8"))
            self.formularios.append(formulario)
            if formulario.get("grant_type") == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": ACCESO, "expires_in": 3599, "refresh_token": REFRESH,
                                                 "scope": self.scope, "token_type": "Bearer"})
            return self.respuesta_refresco()
        if peticion.method == "POST" and url.host == "oauth2.googleapis.com" and url.path == "/revoke":
            return httpx.Response(self.estado_revocar)
        if peticion.method == "GET" and url.host == "www.googleapis.com" and url.path == "/drive/v3/about":
            return httpx.Response(200, json={"user": {"emailAddress": CUENTA}})
        return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})

    def contar(self, metodo: str, ruta: str) -> int:
        return sum(1 for p in self.peticiones if p.method == metodo and p.url.path == ruta)


class Navegador:
    """«Navegador» inyectado: guarda la URL y ejecuta ``accion(url)`` (GETs reales al loopback)."""

    def __init__(self, accion: Callable[[str], Any] | None = None) -> None:
        self.urls: list[str] = []
        self.accion = accion
        self.estados: list[int] = []
        self.cuerpos: list[str] = []

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        if self.accion is not None:
            self.accion(url)
        return True

    def get(self, url_autorizacion: str, *, ruta: str = "/", **params: str) -> int:
        consulta = parse_qs(urlsplit(url_autorizacion).query)
        redirect = consulta["redirect_uri"][0]
        with httpx.Client(trust_env=False, timeout=5.0) as cliente:
            respuesta = cliente.get(redirect.rstrip("/") + ruta, params=params)
        self.estados.append(respuesta.status_code)
        self.cuerpos.append(respuesta.text)
        return respuesta.status_code


def autorizar(navegador: Navegador) -> Callable[[str], None]:
    """Acción del navegador: el usuario autoriza (callback con el ``state`` correcto y un ``code``)."""

    def accion(url: str) -> None:
        state = parse_qs(urlsplit(url).query)["state"][0]
        navegador.get(url, code=CODE, state=state)

    return accion


@pytest.fixture
def ajustes(settings_con_claves: Any, tmp_path: Path) -> Any:
    s = settings_con_claves.con(google_habilitado=True, offline=False)
    # Nunca el data/ real: cliente y token dentro de tmp_path.
    assert tmp_path in Path(s.google_cliente_path).parents
    assert tmp_path in Path(s.google_token_path).parents
    return s


def escribir_cliente(settings: Any, contenido: Any = None) -> None:
    ruta = Path(settings.google_cliente_path)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    if contenido is None:
        contenido = {"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                   "redirect_uris": ["http://localhost"]}}
    ruta.write_text(contenido if isinstance(contenido, str) else json.dumps(contenido), encoding="utf-8")


def nueva_cuenta(settings: Any, falso: FalsoGoogle, cliente: httpx.AsyncClient, *, bus: BusEventos | None = None,
                 navegador: Navegador | None = None, protector: Any = None,
                 reloj: Callable[[], float] | None = None) -> CuentaGoogle:
    cuenta = CuentaGoogle(settings, bus, cliente=cliente, abrir_navegador=navegador or Navegador(),
                          protector=protector or ProtectorFalso(), **({"reloj": reloj} if reloj else {}))
    esperas: list[float] = []

    async def dormir(segundos: float) -> None:
        esperas.append(segundos)

    cuenta._dormir = dormir  # type: ignore[assignment]
    cuenta.esperas = esperas  # type: ignore[attr-defined]
    return cuenta


async def conectada(settings: Any, falso: FalsoGoogle, cliente: httpx.AsyncClient, **kwargs: Any) -> CuentaGoogle:
    """Cuenta conectada por el flujo OAuth completo (con el doble del navegador)."""
    navegador = Navegador()
    navegador.accion = autorizar(navegador)
    cuenta = nueva_cuenta(settings, falso, cliente, navegador=navegador, **kwargs)
    resultado = await cuenta.conectar(timeout_s=10)
    assert resultado.ok, resultado
    return cuenta


def assert_servidor_cerrado(url: str) -> None:
    """Nadie escucha ya en el puerto del loopback (en Windows, un puerto cerrado puede dar ConnectTimeout)."""
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        with httpx.Client(trust_env=False, timeout=1.0) as c:
            c.get(url)


# ---------------------------------------------------------------------------
# Funciones puras
# ---------------------------------------------------------------------------


def test_scopes_pedidos_segun_settings(ajustes: Any) -> None:
    assert scopes_pedidos(ajustes) == (SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
    assert scopes_pedidos(ajustes.con(drive_completo=True)) == (SCOPE_GMAIL_SEND, SCOPE_DRIVE)
    assert scopes_pedidos(ajustes.con(google_servicios=("correo",))) == (SCOPE_GMAIL_SEND,)
    assert scopes_pedidos(ajustes.con(google_servicios=("drive",))) == (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
    assert scopes_pedidos(ajustes.con(google_servicios=())) == ()


def test_clasificar_scopes_solo_usa_lo_pedido() -> None:
    normal = (SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
    completo = (SCOPE_GMAIL_SEND, SCOPE_DRIVE)
    assert clasificar_scopes(normal, normal) == (normal, ())
    assert clasificar_scopes(completo, completo) == (completo, ())
    # Más amplio que lo configurado: «drive» nunca se usa, y con él Drive entero (el token puede más de lo pedido).
    assert clasificar_scopes(completo, normal) == ((SCOPE_GMAIL_SEND,), (SCOPE_DRIVE,))
    assert clasificar_scopes((*normal, SCOPE_DRIVE), normal) == ((SCOPE_GMAIL_SEND,), (SCOPE_DRIVE,))
    # Más estrecho que lo configurado (conectado antes de poner RAMS_DRIVE_COMPLETO=1): se usa, puede menos.
    assert clasificar_scopes(normal, completo) == (normal, ())
    # Un servicio que ya no se pide.
    assert clasificar_scopes(normal, (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)) == (
        (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE), (SCOPE_GMAIL_SEND,))
    assert clasificar_scopes(normal, (SCOPE_GMAIL_SEND,)) == (
        (SCOPE_GMAIL_SEND,), (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE))
    assert clasificar_scopes(("https://otro/scope", SCOPE_GMAIL_SEND), normal) == ((SCOPE_GMAIL_SEND,), ())
    assert clasificar_scopes((), normal) == ((), ())


def test_enmascarar_correo() -> None:
    assert enmascarar_correo("juan.perez@gmail.com") == "ju***@gmail.com"
    assert enmascarar_correo("a@x.co") == "a***@x.co"
    assert enmascarar_correo(None) is None
    assert enmascarar_correo("") is None
    assert enmascarar_correo("sin-arroba") == "***"


def test_estado_a_dict_enmascara_y_repr_sin_cuenta() -> None:
    estado = EstadoGoogle(True, True, True, True, False, CUENTA, "")
    datos = estado.a_dict()
    assert datos == {"conectado": True, "correo": True, "drive_leer": True, "drive_escribir": True,
                     "drive_completo": False, "cuenta": "ju***@gmail.com", "motivo": ""}
    assert CUENTA not in repr(estado)


def test_error_google_codigo_desconocido_es_error() -> None:
    error = ErrorGoogle("raro", "")
    assert error.codigo == "error" and error.mensaje
    assert ErrorGoogle("limite", "x", http=429, razon="rateLimitExceeded").razon == "rateLimitExceeded"
    assert ErrorGoogle("limite", "x", razon="con espacios y datos").razon is None


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI solo existe en Windows")
def test_dpapi_real_ida_y_vuelta_en_memoria() -> None:
    protector = protector_por_defecto()
    assert protector.nombre == "dpapi"
    blob = protector.proteger(REFRESH.encode())
    assert REFRESH.encode() not in blob
    assert protector.desproteger(blob) == REFRESH.encode()


def test_protector_por_defecto_fuera_de_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc.sys, "platform", "linux")
    assert protector_por_defecto().nombre == "ninguno"


# ---------------------------------------------------------------------------
# OAuth (servidor loopback real)
# ---------------------------------------------------------------------------


def test_conectar_flujo_completo(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    bus = BusEventos()
    navegador = Navegador()
    navegador.accion = autorizar(navegador)

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, bus=bus, navegador=navegador)
            resultado = await cuenta.conectar(timeout_s=10)
            return cuenta, resultado

    cuenta, resultado = correr(principal())
    assert resultado.ok and resultado.codigo == "conectado"
    assert resultado.concedidos == (SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
    assert resultado.faltan == ()
    assert "Gmail" in resultado.mensaje and "Drive" in resultado.mensaje
    # URL de autorización: PKCE S256, loopback 127.0.0.1, consent y scopes.
    url = navegador.urls[0]
    partes = urlsplit(url)
    assert f"{partes.scheme}://{partes.netloc}{partes.path}" == gc.URL_AUTORIZACION
    consulta = parse_qs(partes.query)
    assert consulta["code_challenge_method"] == ["S256"]
    assert consulta["response_type"] == ["code"]
    assert consulta["prompt"] == ["consent"]
    assert consulta["client_id"] == [CLIENT_ID]
    assert consulta["scope"] == [TODOS]
    redirect = consulta["redirect_uri"][0]
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/")
    puerto = int(urlsplit(redirect).port or 0)
    assert puerto > 0
    # La pestaña local dice «Listo».
    assert navegador.estados == [200]
    assert "Listo" in navegador.cuerpos[0]
    # Canje CON client_secret, mismo redirect_uri y verifier que casa con el challenge.
    canje = falso.formularios[0]
    assert canje["grant_type"] == ["authorization_code"]
    assert canje["client_secret"] == [CLIENT_SECRET]
    assert canje["client_id"] == [CLIENT_ID]
    assert canje["code"] == [CODE]
    assert canje["redirect_uri"] == [redirect]
    verifier = canje["code_verifier"][0]
    assert 43 <= len(verifier) <= 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert consulta["code_challenge"] == [challenge]
    # about.get solo una vez (hay Drive) y sin pedir scopes extra.
    assert falso.contar("GET", "/drive/v3/about") == 1
    # Estado y evento con la cuenta enmascarada.
    estado = cuenta.estado()
    assert estado.conectado and estado.correo and estado.drive_leer and estado.drive_escribir
    assert not estado.drive_completo and estado.cuenta == CUENTA
    evento = bus.ultimo("google.estado")
    assert evento is not None and evento.datos["conectado"] is True
    assert evento.datos["cuenta"] == "ju***@gmail.com"
    assert cuenta.diagnostico().estado.value == "OK"
    assert "ju***@gmail.com" in cuenta.diagnostico().detalle
    # Token en disco: protegido, sin el access token.
    texto = Path(ajustes.google_token_path).read_text(encoding="utf-8")
    assert REFRESH not in texto and ACCESO not in texto
    datos = json.loads(texto)
    assert datos["version"] == 1 and datos["cifrado"] == "falso"
    assert datos["client_id_sha"] == hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:12]
    assert datos["cuenta"] == CUENTA
    assert set(datos["scopes"]) == {SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE}
    assert not list(Path(ajustes.google_token_path).parent.glob("*.tmp"))
    # El servidor de escucha quedó cerrado.
    assert_servidor_cerrado(redirect)


def test_state_invalido_tres_veces(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    navegador = Navegador()

    def accion(url: str) -> None:
        for _ in range(3):
            navegador.get(url, code=CODE, state="state-ajeno")

    navegador.accion = accion

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador)
            return cuenta, await cuenta.conectar(timeout_s=10)

    cuenta, resultado = correr(principal())
    assert navegador.estados == [400, 400, 400]
    assert not resultado.ok and resultado.codigo == "state_invalido"
    assert falso.formularios == []          # nunca se canjeó nada
    assert not cuenta.estado().conectado
    assert not Path(ajustes.google_token_path).exists()


def test_state_malo_y_despues_bueno(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    navegador = Navegador()

    def accion(url: str) -> None:
        navegador.get(url, code=CODE, state="otro")
        navegador.get(url, ruta="/favicon.ico")
        state = parse_qs(urlsplit(url).query)["state"][0]
        navegador.get(url, code=CODE, state=state)

    navegador.accion = accion

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            return await nueva_cuenta(ajustes, falso, cliente, navegador=navegador).conectar(timeout_s=10)

    resultado = correr(principal())
    assert navegador.estados == [400, 404, 200]
    assert resultado.ok


def _crudo(url_autorizacion: str, datos: bytes) -> bytes:
    """Envía bytes en bruto al loopback y devuelve lo que responda (b"" si cierra con RST)."""
    redirect = parse_qs(urlsplit(url_autorizacion).query)["redirect_uri"][0]
    puerto = int(urlsplit(redirect).port or 0)
    respuesta = b""
    with socket.create_connection(("127.0.0.1", puerto), timeout=5) as conexion:
        try:
            conexion.sendall(datos)
            while True:
                trozo = conexion.recv(4096)
                if not trozo:
                    break
                respuesta += trozo
        except OSError:
            pass
    return respuesta


def test_callback_rechaza_metodos_hosts_y_peticiones_enormes(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    navegador = Navegador()
    respuestas: dict[str, bytes] = {}

    def accion(url: str) -> None:
        state = parse_qs(urlsplit(url).query)["state"][0]
        puerto = urlsplit(parse_qs(urlsplit(url).query)["redirect_uri"][0]).port
        respuestas["post"] = _crudo(url, f"POST /?code=x&state={state} HTTP/1.1\r\nHost: 127.0.0.1:{puerto}\r\n"
                                         "Content-Length: 0\r\n\r\n".encode())
        respuestas["host"] = _crudo(url, f"GET /?code=x&state={state} HTTP/1.1\r\nHost: evil.example\r\n\r\n".encode())
        respuestas["enorme"] = _crudo(url, b"GET /?relleno=" + b"a" * 10_000 + b" HTTP/1.1\r\n\r\n")
        respuestas["basura"] = _crudo(url, b"\x16\x03\x01 no es http\r\n\r\n")
        navegador.get(url, code=CODE, state=state)

    navegador.accion = accion

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            return await nueva_cuenta(ajustes, falso, cliente, navegador=navegador).conectar(timeout_s=10)

    resultado = correr(principal())
    assert respuestas["post"].startswith(b"HTTP/1.1 405")
    assert respuestas["host"].startswith(b"HTTP/1.1 400")
    assert respuestas["enorme"] == b"" or respuestas["enorme"].startswith(b"HTTP/1.1 431")
    assert respuestas["basura"].startswith(b"HTTP/1.1 400")
    assert resultado.ok and len(falso.formularios) == 1     # nada de eso contó como autorización


def test_access_denied(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    navegador = Navegador()

    def accion(url: str) -> None:
        state = parse_qs(urlsplit(url).query)["state"][0]
        navegador.get(url, error="access_denied", state=state)

    navegador.accion = accion

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            return await nueva_cuenta(ajustes, falso, cliente, navegador=navegador).conectar(timeout_s=10)

    resultado = correr(principal())
    assert resultado.codigo == "denegado" and not resultado.ok
    assert falso.formularios == []


def test_tiempo_agotado_cierra_el_servidor(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    navegador = Navegador()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador)
            resultado = await cuenta.conectar(timeout_s=0.3)
            return cuenta, resultado

    cuenta, resultado = correr(principal())
    assert resultado.codigo == "tiempo_agotado"
    assert "no llegó la autorización" in resultado.mensaje
    assert not cuenta.conectando
    redirect = parse_qs(urlsplit(navegador.urls[0]).query)["redirect_uri"][0]
    assert_servidor_cerrado(redirect)


def test_cancelar_conexion(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente)
            tarea = asyncio.create_task(cuenta.conectar(timeout_s=10))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if cuenta._futuro_conexion is not None:
                    break
            assert cuenta.conectando
            segunda = await cuenta.conectar(timeout_s=1)
            assert segunda.codigo == "ya_en_curso"
            assert cuenta.cancelar_conexion() is True
            return await tarea

    assert correr(principal()).codigo == "cancelado"


def test_mostrar_url_si_no_abre_el_navegador(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    mostradas: list[str] = []

    def navegador_roto(url: str) -> bool:
        raise OSError("sin navegador")

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = CuentaGoogle(ajustes, None, cliente=cliente, abrir_navegador=navegador_roto,
                                  protector=ProtectorFalso())
            return await cuenta.conectar(timeout_s=0.3, mostrar_url=mostradas.append)

    assert correr(principal()).codigo == "tiempo_agotado"
    assert len(mostradas) == 1 and mostradas[0].startswith(gc.URL_AUTORIZACION)


@pytest.mark.parametrize(("contenido", "codigo"), [
    ({"web": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}, "cliente_web"),
    ("{esto no es json", "cliente_invalido"),
    ({"installed": {"client_id": CLIENT_ID}}, "cliente_invalido"),
    (None, "sin_cliente"),
])
def test_cliente_oauth_invalido(ajustes: Any, correr: Any, contenido: Any, codigo: str) -> None:
    if contenido is not None:
        escribir_cliente(ajustes, contenido)
    falso = FalsoGoogle()
    navegador = Navegador()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador)
            return cuenta, await cuenta.conectar(timeout_s=1)

    cuenta, resultado = correr(principal())
    assert resultado.codigo == codigo
    assert navegador.urls == [] and falso.peticiones == []
    diagnostico = cuenta.diagnostico()
    if codigo == "sin_cliente":
        assert diagnostico.estado.value == "EN ESPERA" and "google_cliente.json" in diagnostico.detalle
    else:
        assert diagnostico.estado.value == "DEGRADADO"
    if codigo == "cliente_web":
        assert "App de escritorio" in resultado.mensaje


def test_releer_cliente_guardado_con_rams_en_marcha(ajustes: Any, correr: Any) -> None:
    """El JSON del cliente (o el token) que aparece tras ``iniciar`` se recoge sin reiniciar y sin red."""
    falso = FalsoGoogle()
    bus = BusEventos()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, bus=bus)
            await cuenta.iniciar()
            antes = cuenta.estado().motivo
            sigue = await cuenta.releer_cliente()             # todavía sin archivo: nada cambia
            escribir_cliente(ajustes)
            motivo = await cuenta.releer_cliente()
            publicado = bus.ultimo("google.estado").datos["motivo"]
            return cuenta, antes, sigue, motivo, publicado

    cuenta, antes, sigue, motivo, publicado = correr(principal())
    assert antes == "sin_cliente" and sigue == "sin_cliente"
    assert motivo == "sin_conectar" and publicado == "sin_conectar"
    assert cuenta.diagnostico().estado.value == "EN ESPERA" and "conecta" in cuenta.diagnostico().detalle
    assert falso.peticiones == []

    # Con un token guardado de antes y el cliente recién restaurado, la cuenta vuelve a estar conectada.
    async def con_token() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            await conectada(ajustes, falso, cliente)
        Path(ajustes.google_cliente_path).unlink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta2 = nueva_cuenta(ajustes, falso, cliente, bus=bus)
            await cuenta2.iniciar()
            sin = cuenta2.estado()
            escribir_cliente(ajustes)
            n = len(falso.peticiones)
            await cuenta2.releer_cliente()
            con = cuenta2.estado()
            cuenta2._acceso = "ya29.en-memoria"
            await cuenta2.releer_cliente()                   # conectada: no relee ni borra el acceso
            return sin, con, cuenta2._acceso, len(falso.peticiones) - n

    sin, con, acceso, peticiones = correr(con_token())
    assert not sin.conectado and sin.motivo == "sin_cliente"
    assert con.conectado and con.cuenta == CUENTA and con.correo
    assert acceso == "ya29.en-memoria" and peticiones == 0


def test_scope_parcial_sin_drive_no_pide_about(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    falso.scope = SCOPE_GMAIL_SEND

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            navegador = Navegador()
            navegador.accion = autorizar(navegador)
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador)
            return cuenta, await cuenta.conectar(timeout_s=10)

    cuenta, resultado = correr(principal())
    assert resultado.ok
    assert resultado.concedidos == (SCOPE_GMAIL_SEND,)
    assert resultado.faltan == (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
    assert "No marcaste Drive" in resultado.mensaje
    assert falso.contar("GET", "/drive/v3/about") == 0
    estado = cuenta.estado()
    assert estado.correo and not estado.drive_leer and not estado.drive_escribir and estado.cuenta is None


def test_canje_con_un_scope_no_pedido_no_lo_usa(ajustes: Any, correr: Any) -> None:
    """Google devuelve «drive» (completo) sin que la configuración lo pida: nunca cuenta como concedido."""
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    falso.scope = f"{SCOPE_GMAIL_SEND} {SCOPE_DRIVE}"

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            navegador = Navegador()
            navegador.accion = autorizar(navegador)
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador)
            return cuenta, await cuenta.conectar(timeout_s=10)

    cuenta, resultado = correr(principal())
    assert resultado.ok and resultado.concedidos == (SCOPE_GMAIL_SEND,) and resultado.faltan == ()
    assert "más permiso del que pedí (Drive completo)" in resultado.mensaje
    assert "Drive queda sin usar" in resultado.mensaje and "No marcaste" not in resultado.mensaje
    assert falso.contar("GET", "/drive/v3/about") == 0          # ni siquiera para la cuenta
    estado = cuenta.estado()
    assert estado.correo and not (estado.drive_leer or estado.drive_escribir or estado.drive_completo)
    assert not cuenta.tiene_scope(SCOPE_DRIVE) and cuenta.tiene_scope(SCOPE_GMAIL_SEND)
    diagnostico = cuenta.diagnostico()
    assert diagnostico.estado.value == "DEGRADADO" and "más amplio que el configurado" in diagnostico.detalle
    assert "Drive completo" in diagnostico.detalle and "en uso: Gmail" in diagnostico.detalle
    # En disco queda lo que Google concedió de verdad (para seguir detectándolo al reiniciar).
    datos = json.loads(Path(ajustes.google_token_path).read_text(encoding="utf-8"))
    assert set(datos["scopes"]) == {SCOPE_GMAIL_SEND, SCOPE_DRIVE}


def test_token_guardado_mas_amplio_que_la_configuracion(ajustes: Any, correr: Any) -> None:
    """Conectado con RAMS_DRIVE_COMPLETO=1 y reiniciado con =0: el scope «drive» guardado no se usa y el diagnóstico
    pide reconectar. Con =1 otra vez, vuelve a ser Drive completo."""
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    falso.scope = f"{SCOPE_GMAIL_SEND} {SCOPE_DRIVE}"
    con_completo = ajustes.con(drive_completo=True)

    async def conectar() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(con_completo, falso, cliente)
            return cuenta.estado()

    antes = correr(conectar())
    assert antes.drive_completo and antes.drive_leer and antes.drive_escribir

    async def cargar(s: Any) -> Any:
        sin_red = FalsoGoogle()
        sin_red.caida = True
        bus = BusEventos()
        async with httpx.AsyncClient(transport=httpx.MockTransport(sin_red)) as cliente:
            cuenta = nueva_cuenta(s, sin_red, cliente, bus=bus)
            diagnostico = await cuenta.iniciar()
            return cuenta, diagnostico, bus.ultimo("google.estado")

    cuenta, diagnostico, evento = correr(cargar(ajustes))
    estado = cuenta.estado()
    assert estado.conectado and estado.correo
    assert not (estado.drive_completo or estado.drive_leer or estado.drive_escribir)
    assert evento is not None and evento.datos["drive_completo"] is False and evento.datos["drive_leer"] is False
    assert diagnostico.estado.value == "DEGRADADO"
    assert "más amplio que el configurado (Drive completo, sin usar)" in diagnostico.detalle
    assert "desconecta y vuelve a conectar" in diagnostico.detalle
    cuenta, diagnostico, _ = correr(cargar(con_completo))
    assert cuenta.estado().drive_completo and diagnostico.estado.value == "OK" and "Drive completo" in diagnostico.detalle


def test_token_con_un_servicio_que_ya_no_se_pide(ajustes: Any, correr: Any) -> None:
    """Conectado con correo y Drive; después RAMS_GOOGLE_SERVICIOS=drive: gmail.send queda sin usar (y se avisa)."""
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def conectar() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            await conectada(ajustes, falso, cliente)

    correr(conectar())

    async def cargar() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(FalsoGoogle())) as cliente:
            cuenta = nueva_cuenta(ajustes.con(google_servicios=("drive",)), falso, cliente)
            return cuenta, await cuenta.iniciar()

    cuenta, diagnostico = correr(cargar())
    estado = cuenta.estado()
    assert not estado.correo and estado.drive_leer and estado.drive_escribir
    assert not cuenta.tiene_scope(SCOPE_GMAIL_SEND)
    assert diagnostico.estado.value == "DEGRADADO" and "(Gmail, sin usar)" in diagnostico.detalle


def test_deshabilitado_y_offline_no_conectan(settings_con_claves: Any, correr: Any) -> None:
    falso = FalsoGoogle()

    async def principal(s: Any) -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(s, falso, cliente)
            diagnostico = await cuenta.iniciar()
            return diagnostico, await cuenta.conectar(timeout_s=1)

    diagnostico, resultado = correr(principal(settings_con_claves))
    assert resultado.codigo == "deshabilitado"
    assert diagnostico.estado.value == "OFF" and "RAMS_GOOGLE=0" in diagnostico.detalle
    diagnostico, resultado = correr(principal(settings_con_claves.con(google_habilitado=True, offline=True)))
    assert resultado.codigo == "offline" and diagnostico.detalle == "--offline"
    assert falso.peticiones == []


# ---------------------------------------------------------------------------
# Token en disco
# ---------------------------------------------------------------------------


def test_iniciar_carga_el_token_sin_red(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def conectar() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            await conectada(ajustes, falso, cliente)

    correr(conectar())
    sin_red = FalsoGoogle()
    sin_red.caida = True
    bus = BusEventos()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(sin_red)) as cliente:
            cuenta = nueva_cuenta(ajustes, sin_red, cliente, bus=bus)
            diagnostico = await cuenta.iniciar()
            return cuenta, diagnostico

    cuenta, diagnostico = correr(principal())
    assert sin_red.peticiones == []
    assert diagnostico.estado.value == "OK"
    assert cuenta.estado().conectado and cuenta.estado().cuenta == CUENTA
    assert cuenta.tiene_scope(SCOPE_GMAIL_SEND) and cuenta.disponible
    assert bus.ultimo("google.estado").datos["conectado"] is True


def test_dpapi_falla_no_guarda_el_token(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            navegador = Navegador()
            navegador.accion = autorizar(navegador)
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador, protector=ProtectorRoto())
            return cuenta, await cuenta.conectar(timeout_s=10)

    cuenta, resultado = correr(principal())
    assert resultado.ok and "solo hasta que me reinicies" in resultado.mensaje
    assert not Path(ajustes.google_token_path).exists()
    assert cuenta.estado().conectado and cuenta.estado().motivo == "token_no_guardado"
    diagnostico = cuenta.diagnostico()
    assert diagnostico.estado.value == "DEGRADADO" and "DPAPI" in diagnostico.detalle


def test_token_de_otro_protector_no_se_usa(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def conectar() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            await conectada(ajustes, falso, cliente)

    correr(conectar())

    class OtroProtector(ProtectorFalso):
        nombre = "otro"

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, protector=OtroProtector())
            await cuenta.iniciar()
            return cuenta

    cuenta = correr(principal())
    assert not cuenta.estado().conectado and cuenta.estado().motivo == "token_invalido"


# ---------------------------------------------------------------------------
# Refresco y cliente REST
# ---------------------------------------------------------------------------


def test_dos_refrescos_concurrentes_un_solo_post(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            cuenta._acceso_expira = 0.0          # el access token venció
            falso.retardo = 0.05
            tokens = await asyncio.gather(cuenta.token_acceso(), cuenta.token_acceso())
            return tokens

    tokens = correr(principal())
    assert tokens == [ACCESO_2, ACCESO_2]
    refrescos = [f for f in falso.formularios if f.get("grant_type") == ["refresh_token"]]
    assert len(refrescos) == 1
    assert refrescos[0]["client_secret"] == [CLIENT_SECRET]
    assert refrescos[0]["refresh_token"] == [REFRESH]


def test_token_vigente_no_refresca(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            return await cuenta.token_acceso()

    assert correr(principal()) == ACCESO
    assert [f for f in falso.formularios if f.get("grant_type") == ["refresh_token"]] == []


def test_invalid_grant_marca_token_invalido(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    bus = BusEventos()
    falso.respuesta_refresco = lambda: httpx.Response(400, json={"error": "invalid_grant",
                                                                 "error_description": "Token has been expired"})

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente, bus=bus)
            cuenta._acceso_expira = 0.0
            with pytest.raises(ErrorGoogle) as error:
                await cuenta.token_acceso()
            return cuenta, error.value

    cuenta, error = correr(principal())
    assert error.codigo == "token_invalido" and "conecta mi cuenta de Google" in error.mensaje
    assert cuenta.estado().motivo == "token_invalido" and not cuenta.estado().conectado
    assert bus.ultimo("google.estado").datos["motivo"] == "token_invalido"
    assert cuenta.diagnostico().estado.value == "DEGRADADO"
    datos = json.loads(Path(ajustes.google_token_path).read_text(encoding="utf-8"))
    assert datos["invalido"] is True and datos["refresh_token"] == ""


def test_refresco_reintenta_5xx_con_esperas(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    respuestas = [httpx.Response(503), httpx.Response(502),
                  httpx.Response(200, json={"access_token": ACCESO_2, "expires_in": 3600})]
    falso.respuesta_refresco = lambda: respuestas.pop(0)

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            cuenta._acceso_expira = 0.0
            return cuenta, await cuenta.token_acceso()

    cuenta, token = correr(principal())
    assert token == ACCESO_2
    assert cuenta.esperas == [1.0, 2.0]


def test_401_refresca_una_vez_y_repite(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    vistos: list[str] = []

    def extra(peticion: httpx.Request) -> httpx.Response | None:
        if peticion.url.host == "gmail.googleapis.com":
            vistos.append(peticion.headers["Authorization"])
            return httpx.Response(401 if len(vistos) == 1 else 200, json={"id": "abc"})
        return None

    falso.extra = extra

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            return await cuenta.solicitud("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                          json={"raw": "x"})

    respuesta = correr(principal())
    assert respuesta.status_code == 200
    assert vistos == [f"Bearer {ACCESO}", f"Bearer {ACCESO_2}"]


def test_401_dos_veces_es_token_invalido(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    falso.extra = lambda p: httpx.Response(401) if p.url.host == "www.googleapis.com" and "files" in p.url.path else None

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            with pytest.raises(ErrorGoogle) as error:
                await cuenta.solicitud("GET", "https://www.googleapis.com/drive/v3/files")
            return error.value

    assert correr(principal()).codigo == "token_invalido"


@pytest.mark.parametrize("metodo,url", [
    ("GET", "https://evil.example.com/drive/v3/files"),
    ("GET", "http://www.googleapis.com/drive/v3/files"),
    ("GET", "https://www.googleapis.com:8443/drive/v3/files"),
    ("GET", "https://usuario@www.googleapis.com/drive/v3/files"),
    ("DELETE", "https://www.googleapis.com/drive/v3/files/abc"),
])
def test_destinos_y_metodos_no_permitidos(ajustes: Any, correr: Any, metodo: str, url: str) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            antes = len(falso.peticiones)
            with pytest.raises(ErrorGoogle) as error:
                await cuenta.solicitud(metodo, url)
            return error.value, len(falso.peticiones) - antes

    error, nuevas = correr(principal())
    assert error.codigo == "error" and nuevas == 0


@pytest.mark.parametrize("estado,cuerpo,codigo", [
    (403, {"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}, "sin_permiso"),
    (403, {"error": {"code": 403, "errors": [{"reason": "appNotAuthorizedToFile"}]}}, "sin_permiso"),
    (403, {"error": {"code": 403, "errors": [{"reason": "domainPolicy"}]}}, "politica"),
    (403, {"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}, "limite"),
    (429, {}, "limite"),
    (403, {"error": {"code": 403, "errors": [{"reason": "storageQuotaExceeded"}]}}, "almacenamiento_lleno"),
    (403, {"error": {"code": 403, "errors": [{"reason": "fileNotDownloadable"}]}}, "no_descargable"),
    (403, {"error": {"code": 403, "errors": [{"reason": "exportSizeLimitExceeded"}]}}, "no_exportable"),
    (404, {}, "no_encontrado"),
    (500, {}, "servidor"),
    (400, {"error": {"code": 400, "errors": [{"reason": "badRequest"}]}}, "rechazado"),
])
def test_mapeo_de_errores(ajustes: Any, correr: Any, estado: int, cuerpo: Any, codigo: str) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    falso.extra = lambda p: httpx.Response(estado, json=cuerpo) if "files" in p.url.path else None

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            with pytest.raises(ErrorGoogle) as error:
                await cuenta.solicitud("GET", "https://www.googleapis.com/drive/v3/files")
            return error.value

    error = correr(principal())
    assert error.codigo == codigo and error.http == estado


def test_reintentar_respeta_retry_after(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    respuestas = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503),
                  httpx.Response(200, json={"files": []})]
    falso.extra = lambda p: respuestas.pop(0) if "files" in p.url.path else None

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            respuesta = await cuenta.solicitud("GET", "https://www.googleapis.com/drive/v3/files", reintentar=True)
            return cuenta, respuesta

    cuenta, respuesta = correr(principal())
    assert respuesta.status_code == 200
    assert cuenta.esperas == [7.0, 2.0]


def test_sin_reintentar_un_5xx_falla_a_la_primera(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    contador = {"n": 0}

    def extra(p: httpx.Request) -> httpx.Response | None:
        if p.url.host == "gmail.googleapis.com":
            contador["n"] += 1
            return httpx.Response(503)
        return None

    falso.extra = extra

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            with pytest.raises(ErrorGoogle) as error:
                await cuenta.solicitud("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                       json={"raw": "x"})
            return error.value

    assert correr(principal()).codigo == "servidor"
    assert contador["n"] == 1


def test_errores_de_transporte(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    modo = {"exc": httpx.ConnectError}

    def extra(p: httpx.Request) -> httpx.Response | None:
        if "files" in p.url.path:
            raise modo["exc"]("fallo", request=p)
        return None

    falso.extra = extra

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            errores = []
            for exc in (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError):
                modo["exc"] = exc
                with pytest.raises(ErrorGoogle) as error:
                    await cuenta.solicitud("GET", "https://www.googleapis.com/drive/v3/files")
                errores.append((error.value.codigo, type(error.value.__cause__)))
            return errores

    assert correr(principal()) == [("red", httpx.ConnectError), ("red", httpx.ConnectTimeout),
                                   ("tiempo_agotado", httpx.ReadTimeout), ("red", httpx.RemoteProtocolError)]


def test_flujo_descarga_y_error(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    def extra(p: httpx.Request) -> httpx.Response | None:
        if p.url.path == "/drive/v3/files/abc":
            return httpx.Response(200, content=b"x" * 3000)
        if p.url.path == "/drive/v3/files/prohibido":
            return httpx.Response(403, json={"error": {"errors": [{"reason": "fileNotDownloadable"}]}})
        return None

    falso.extra = extra

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            datos = b""
            async with cuenta.flujo("GET", "https://www.googleapis.com/drive/v3/files/abc",
                                    params={"alt": "media"}) as respuesta:
                async for bloque in respuesta.aiter_bytes(1024):
                    datos += bloque
            with pytest.raises(ErrorGoogle) as error:
                async with cuenta.flujo("GET", "https://www.googleapis.com/drive/v3/files/prohibido"):
                    pass
            with pytest.raises(ErrorGoogle):
                async with cuenta.flujo("DELETE", "https://www.googleapis.com/drive/v3/files/abc"):
                    pass
            return datos, error.value

    datos, error = correr(principal())
    assert datos == b"x" * 3000
    assert error.codigo == "no_descargable"


def test_solicitud_sin_conectar_lanza_desconectado(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente)
            await cuenta.iniciar()
            with pytest.raises(ErrorGoogle) as error:
                await cuenta.token_acceso()
            return cuenta, error.value

    cuenta, error = correr(principal())
    assert error.codigo == "desconectado"
    assert cuenta.diagnostico().estado.value == "EN ESPERA"
    assert falso.peticiones == []


# ---------------------------------------------------------------------------
# Desconectar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado_revocar", [200, 400])
def test_desconectar_revoca_y_borra(ajustes: Any, correr: Any, estado_revocar: int) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    falso.estado_revocar = estado_revocar
    bus = BusEventos()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente, bus=bus)
            assert Path(ajustes.google_token_path).exists()
            return cuenta, await cuenta.desconectar()

    cuenta, resultado = correr(principal())
    assert resultado.ok and resultado.codigo == "desconectado"
    assert "myaccount.google.com/connections" in resultado.mensaje
    assert not Path(ajustes.google_token_path).exists()
    revocar = [p for p in falso.peticiones if p.url.path == "/revoke"]
    assert len(revocar) == 1 and parse_qs(revocar[0].content.decode())["token"] == [REFRESH]
    assert not cuenta.estado().conectado
    assert bus.ultimo("google.estado").datos["conectado"] is False


def test_desconectar_sin_red_borra_igual(ajustes: Any, correr: Any) -> None:
    escribir_cliente(ajustes)
    falso = FalsoGoogle()

    async def principal() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = await conectada(ajustes, falso, cliente)
            falso.caida = True
            return await cuenta.desconectar()

    resultado = correr(principal())
    assert resultado.ok
    assert "borré el permiso local" in resultado.mensaje.lower()
    assert not Path(ajustes.google_token_path).exists()


# ---------------------------------------------------------------------------
# Logs sin secretos
# ---------------------------------------------------------------------------


def test_logs_sin_secretos_ni_cuenta(ajustes: Any, correr: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams")
    escribir_cliente(ajustes)
    falso = FalsoGoogle()
    navegador = Navegador()
    navegador.accion = autorizar(navegador)

    async def principal() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
            cuenta = nueva_cuenta(ajustes, falso, cliente, navegador=navegador)
            await cuenta.iniciar()
            assert (await cuenta.conectar(timeout_s=10)).ok
            cuenta._acceso_expira = 0.0
            await cuenta.token_acceso()
            with pytest.raises(ErrorGoogle):
                await cuenta.solicitud("GET", "https://www.googleapis.com/drive/v3/files/idSecretoDeDrive123")
            await cuenta.desconectar()
            await cuenta.cerrar()

    correr(principal())
    url = navegador.urls[0]
    consulta = parse_qs(urlsplit(url).query)
    verifier = falso.formularios[0]["code_verifier"][0]
    # Solo los registros del proyecto: httpx y httpcore quedan en WARNING en producción (configurar_logging).
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("rams"))
    assert "rams.google" in {r.name for r in caplog.records}
    for secreto in (CLIENT_SECRET, CLIENT_ID, ACCESO, ACCESO_2, REFRESH, CODE, verifier, consulta["state"][0],
                    consulta["code_challenge"][0], CUENTA, "juan", "idSecretoDeDrive123", "127.0.0.1:"):
        assert secreto not in texto, secreto
    assert "{id}" in texto      # la ruta se registra con los ids tapados
