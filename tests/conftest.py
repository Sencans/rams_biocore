"""Fixtures compartidas de los tests de R.A.M.S. BIO-CORE (§12.2; contrato para todos los grupos).

- Sin red: ``sin_red`` (autouse) bloquea toda conexión que no sea local (AF_UNIX, 127.0.0.1,
  ::1, localhost y el resto de direcciones de *loopback*). Además de ``socket.connect`` y
  ``connect_ex`` (contrato), cubre ``loop.sock_connect`` (el Proactor de Windows conecta con
  ``ConnectEx`` sin pasar por ``socket.connect``) y la resolución DNS de hosts no locales.
- Sin hardware ni claves reales: ``settings_prueba`` es offline y aislado en ``tmp_path``.
- Sin pytest-asyncio: las pruebas asíncronas usan ``asyncio.run`` (fixture ``correr``).
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest

from core.bus import BusEventos
from core.config import Settings, cargar_settings
from core.contracts import HomeostaticState

T = TypeVar("T")

RAIZ: Path = Path(__file__).resolve().parents[1]          # raíz del proyecto

_HOSTS_LOCALES = frozenset({"127.0.0.1", "::1", "localhost"})
_COMODINES_BIND = frozenset({"", "0.0.0.0", "::"})       # direcciones de enlace (no generan tráfico)


def _es_local(host: Any) -> bool:
    """``True`` para los hosts locales del contrato y cualquier dirección de *loopback*."""
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("utf-8", "replace")
    texto = str(host).strip().strip("[]").lower()
    if texto in _HOSTS_LOCALES or texto.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(texto.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _verificar_destino(direccion: Any, familia: Any) -> None:
    """Lanza ``OSError("Red bloqueada en tests: <host>")`` si el destino no es local."""
    familia_unix = getattr(socket, "AF_UNIX", None)
    if familia_unix is not None and familia == familia_unix:
        return  # socket UNIX (ruta local): siempre permitido
    host = direccion[0] if isinstance(direccion, tuple) and direccion else direccion
    if not _es_local(host):
        raise OSError(f"Red bloqueada en tests: {host}")


@pytest.fixture(autouse=True)
def sin_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bloquea conexiones de red no locales: parchea socket.socket.connect y connect_ex.
    Permite AF_UNIX y los hosts {"127.0.0.1", "::1", "localhost"}; cualquier otro lanza
    OSError("Red bloqueada en tests: <host>")."""
    connect_original = socket.socket.connect
    connect_ex_original = socket.socket.connect_ex
    getaddrinfo_original = socket.getaddrinfo

    def connect(self: socket.socket, direccion: Any) -> Any:
        _verificar_destino(direccion, self.family)
        return connect_original(self, direccion)

    def connect_ex(self: socket.socket, direccion: Any) -> Any:
        _verificar_destino(direccion, self.family)
        return connect_ex_original(self, direccion)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _es_local(host) and str(host).strip() not in _COMODINES_BIND:
            raise socket.gaierror(f"Red bloqueada en tests: {host}")
        return getaddrinfo_original(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    # El Proactor de Windows (y el selector en algunos caminos) conectan sin socket.connect.
    from asyncio import proactor_events, selector_events

    for clase in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
        original = clase.sock_connect

        def fabricar(original_: Any) -> Callable[..., Awaitable[Any]]:
            async def sock_connect(self: Any, sock: socket.socket, direccion: Any) -> Any:
                _verificar_destino(direccion, sock.family)
                return await original_(self, sock, direccion)
            return sock_connect

        monkeypatch.setattr(clase, "sock_connect", fabricar(original))


@pytest.fixture(autouse=True)
def _aislar_logging() -> Iterator[None]:
    """Restaura los handlers y niveles de logging tras cada test (configurar_logging e
    instalar_manejador_log tocan el logger raíz y no deben contaminar otros tests)."""
    raiz = logging.getLogger()
    handlers_antes = list(raiz.handlers)
    niveles = {nombre: logging.getLogger(nombre).level
               for nombre in ("", "rams", "httpx", "httpcore", "websockets", "asyncio")}
    yield
    for handler in list(raiz.handlers):
        if handler not in handlers_antes and getattr(handler, "_rams", False):
            raiz.removeHandler(handler)
            try:
                handler.close()
            except Exception as exc:  # cerrar un handler roto no debe tumbar la suite
                logging.getLogger("rams.tests").debug("Error al cerrar un handler: %s", exc)
    for nombre, nivel in niveles.items():
        logging.getLogger(nombre).setLevel(nivel)


@pytest.fixture
def settings_prueba(tmp_path: Path) -> Settings:
    """Settings offline y aislados del entorno real:
    cargar_settings(entorno={
        "RAMS_DATA_DIR": str(tmp_path / "data"), "RAMS_HUD_PORT": "0", "RAMS_CTL_PORT": "0",
        "RAMS_CTL_TOKEN_FILE": str(tmp_path / "ctl_token"), "VTS_ENABLED": "0", "RAMS_CAMERA": "0",
        "RAMS_TELEGRAM_POLL": "0", "RAMS_CONNECTOME_SEED": "7"},
        offline=True, base_dir=RAIZ)
    y luego .asegurar_directorios()."""
    settings = cargar_settings(entorno=_entorno_prueba(tmp_path), offline=True, base_dir=RAIZ)
    settings.asegurar_directorios()
    return settings


@pytest.fixture
def settings_con_claves(tmp_path: Path) -> Settings:
    """Como settings_prueba, pero offline=False y con claves FALSAS (sin red real gracias a sin_red):
    GEMINI_API_KEY="test-gemini-key-000000", ELEVENLABS_API_KEY="test-eleven-key-000000",
    TELEGRAM_BOT_TOKEN="123456789:TEST_token_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", CHAT_ID="42",
    BRAVE_API_KEY="test-brave-key-000000". (Todas cumplen las validaciones de formato de §4.1.1.)"""
    entorno = {
        **_entorno_prueba(tmp_path),
        "GEMINI_API_KEY": "test-gemini-key-000000",
        "ELEVENLABS_API_KEY": "test-eleven-key-000000",
        "TELEGRAM_BOT_TOKEN": "123456789:TEST_token_" + "a" * 30,
        "CHAT_ID": "42",
        "BRAVE_API_KEY": "test-brave-key-000000",
    }
    settings = cargar_settings(entorno=entorno, offline=False, base_dir=RAIZ)
    settings.asegurar_directorios()
    return settings


def _entorno_prueba(tmp_path: Path) -> dict[str, str]:
    return {
        "RAMS_DATA_DIR": str(tmp_path / "data"),
        "RAMS_HUD_PORT": "0",
        "RAMS_CTL_PORT": "0",
        "RAMS_CTL_TOKEN_FILE": str(tmp_path / "ctl_token"),
        "VTS_ENABLED": "0",
        # Que los tests nunca muevan un VTuber AI Studio real que esté corriendo en :3939.
        "VTUBER_AI_ENABLED": "0",
        "RAMS_CAMERA": "0",
        "RAMS_TELEGRAM_POLL": "0",
        "RAMS_CONNECTOME_SEED": "7",
    }


@pytest.fixture
def bus() -> BusEventos:
    """Un ``BusEventos`` nuevo (se vincula al loop en la primera publicación dentro de ``asyncio.run``)."""
    return BusEventos()


@pytest.fixture
def estado_neutral() -> HomeostaticState:
    """``HomeostaticState.neutral()``."""
    return HomeostaticState.neutral()


@pytest.fixture
def correr() -> Callable[[Awaitable[T]], T]:
    """Devuelve ``asyncio.run`` para ejecutar corrutinas desde tests síncronos."""
    return asyncio.run  # type: ignore[return-value]


@pytest.fixture
def consola_capturada(settings_prueba: Settings) -> Any:
    """main.Consola(settings_prueba, salida=io.StringIO(), usar_prompt_toolkit=False); el texto se lee con
    consola_capturada.salida.getvalue(). La usa test_main.py."""
    import main  # dentro de la fixture: si main.py falta, no se rompe la colección de otros tests

    return main.Consola(settings_prueba, salida=io.StringIO(), usar_prompt_toolkit=False)
