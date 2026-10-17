"""Fixtures compartidas de los tests de R.A.M.S. BIO-CORE (§12.2; contrato para todos los grupos).

- Sin red: ``sin_red`` (autouse) bloquea toda conexión que no sea local (AF_UNIX, 127.0.0.1,
  ::1, localhost y el resto de direcciones de *loopback*). Además de ``socket.connect`` y
  ``connect_ex`` (contrato), cubre ``loop.sock_connect`` (el Proactor de Windows conecta con
  ``ConnectEx`` sin pasar por ``socket.connect``) y la resolución DNS de hosts no locales.
- Sin hardware ni claves reales: ``settings_prueba`` es offline y aislado en ``tmp_path`` (v0.2:
  token de ``/escritorio`` en ``tmp_path`` y travesuras, visión continua y pantalla apagadas).
- Sin efectos reales en el PC (v0.2, ``docs/COMPANION.md`` §C3 "Grupo base"): ``sin_efectos_pc``
  (autouse) hace fallar el test si algo abre apps o URLs, teclea o mueve el cursor de verdad.
- Sin pytest-asyncio: las pruebas asíncronas usan ``asyncio.run`` (fixture ``correr``).
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import os
import socket
import subprocess
import sys
import webbrowser
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


# ---------------------------------------------------------------------------
# Sin efectos reales en el PC (v0.2)
# ---------------------------------------------------------------------------

# Programas que abren apps/URLs, teclean o mandan órdenes al compositor: un subproceso con
# cualquiera de estos nombres base en sus argumentos hace fallar el test.
PROGRAMAS_PROHIBIDOS: frozenset[str] = frozenset({
    "xdg-open", "gtk-launch", "wtype", "ydotool", "hyprctl", "explorer", "explorer.exe",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "start",
})
# Funciones de Win32 que mueven el cursor, teclean, roban el foco o abren documentos/URLs.
_FUNCIONES_WIN32: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("user32", ("SendInput", "SetCursorPos", "keybd_event", "mouse_event", "SetForegroundWindow")),
    ("shell32", ("ShellExecuteW", "ShellExecuteExW")),
)


def _prohibido(que: str) -> Callable[..., Any]:
    """Función que hace fallar el test si se llama (marcada con ``_rams_sin_efectos``)."""

    def falso(*args: Any, **kwargs: Any) -> Any:
        pytest.fail(f"Efecto real en el PC durante un test: {que}. Usa un fake o monkeypatch.")

    falso._rams_sin_efectos = True  # type: ignore[attr-defined]
    falso.__name__ = f"prohibido_{que.replace('.', '_')}"
    return falso


def _como_texto(valor: Any) -> str:
    """Argumento de un subproceso como texto (``bytes`` y ``PathLike`` incluidos); nunca lanza."""
    try:
        if isinstance(valor, (bytes, bytearray, os.PathLike)):
            return os.fsdecode(bytes(valor) if isinstance(valor, bytearray) else valor)
        return str(valor)
    except (TypeError, ValueError):
        return ""


def _nombre_base(valor: Any) -> str:
    """Nombre base en minúsculas (``C:\\Windows\\explorer.exe`` → ``explorer.exe``)."""
    texto = _como_texto(valor).strip().strip("\"'")
    return texto.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def programa_prohibido(args: Any, executable: Any = None) -> str | None:
    """Primer nombre base de ``PROGRAMAS_PROHIBIDOS`` en los argumentos de un subproceso (o ``None``).

    ``args`` puede ser una cadena (línea de órdenes: se mira entera y palabra por palabra), un
    ``bytes``/``PathLike`` o una secuencia de argumentos.
    """
    candidatos: list[str] = []
    if isinstance(args, (str, bytes, bytearray, os.PathLike)):
        texto = _como_texto(args)
        candidatos.append(texto)
        candidatos.extend(texto.split())
    else:
        try:
            candidatos.extend(_como_texto(arg) for arg in args)
        except TypeError:
            candidatos.append(_como_texto(args))
    if executable is not None:
        candidatos.append(_como_texto(executable))
    for candidato in candidatos:
        nombre = _nombre_base(candidato)
        if nombre in PROGRAMAS_PROHIBIDOS:
            return nombre
    return None


def _vetar_subproceso(args: Any, executable: Any, via: str) -> None:
    """Hace fallar el test si el subproceso lanzaría un programa prohibido."""
    nombre = programa_prohibido(args, executable)
    if nombre is not None:
        pytest.fail(f"Efecto real en el PC durante un test: {via} con «{nombre}». "
                    "Parchea el subproceso con un fake o monkeypatch.")


_POPEN_ORIGINAL = subprocess.Popen


class _PopenVigilado(_POPEN_ORIGINAL):  # type: ignore[misc, valid-type]
    """``subprocess.Popen`` que falla con los programas de ``PROGRAMAS_PROHIBIDOS`` (lo demás pasa).

    Es una subclase (no una función) para que ``isinstance(p, subprocess.Popen)`` y
    ``subprocess.run``/``check_output`` sigan funcionando con ``sys.executable`` y similares.
    """

    _rams_sin_efectos = True

    def __init__(self, args: Any, *posicionales: Any, **nombrados: Any) -> None:
        if not isinstance(args, (str, bytes, bytearray, os.PathLike)):
            args = list(args)   # un iterador se consumiría al revisarlo
        ejecutable = nombrados.get("executable")
        if ejecutable is None and len(posicionales) >= 2:   # Popen(args, bufsize, executable, …)
            ejecutable = posicionales[1]
        _vetar_subproceso(args, ejecutable, "subprocess.Popen")
        super().__init__(args, *posicionales, **nombrados)


@pytest.fixture(autouse=True)
def sin_efectos_pc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test abre apps ni URLs, teclea, mueve el cursor ni roba el foco de verdad.

    Sustituye por funciones que hacen ``pytest.fail(...)``: ``os.startfile`` (si existe),
    ``webbrowser.open``/``open_new``/``open_new_tab`` y, en Windows,
    ``ctypes.windll.user32.{SendInput, SetCursorPos, keybd_event, mouse_event, SetForegroundWindow}``
    (y ``shell32.ShellExecuteW``/``ShellExecuteExW``). Envuelve ``subprocess.Popen`` y
    ``asyncio.create_subprocess_exec`` para fallar **solo** si algún argumento tiene como nombre
    base ``xdg-open``, ``gtk-launch``, ``wtype``, ``ydotool``, ``hyprctl``, ``explorer[.exe]``,
    ``cmd[.exe]``, ``powershell[.exe]`` o ``start`` (lo demás, como ``sys.executable``, pasa).
    Un ``monkeypatch`` del propio test se aplica encima y tiene prioridad. Los sustitutos llevan
    el atributo ``_rams_sin_efectos = True``.

    Además quita ``HYPRLAND_INSTANCE_SIGNATURE``: corriendo la suite dentro de una sesión real de
    Hyprland (Arch), ningún módulo detecta el compositor del usuario ni le habla por su socket; los
    tests de Hyprland fijan su propia firma con ``monkeypatch.setenv`` (que se aplica después).
    """
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", _prohibido("os.startfile"))
    for nombre in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, nombre, _prohibido(f"webbrowser.{nombre}"))

    if sys.platform == "win32":
        import ctypes

        for libreria, funciones in _FUNCIONES_WIN32:
            try:
                dll = getattr(ctypes.windll, libreria)  # type: ignore[attr-defined]
            except (AttributeError, OSError) as exc:
                logging.getLogger("rams.tests").debug("Sin %s en ctypes (%s)", libreria, exc)
                continue
            for funcion in funciones:
                try:
                    monkeypatch.setattr(dll, funcion, _prohibido(f"{libreria}.{funcion}"))
                except AttributeError as exc:
                    logging.getLogger("rams.tests").debug("Sin %s.%s (%s)", libreria, funcion, exc)

    monkeypatch.setattr(subprocess, "Popen", _PopenVigilado)

    exec_original = asyncio.create_subprocess_exec

    def create_subprocess_exec_vigilado(program: Any, *args: Any, **kwargs: Any) -> Any:
        _vetar_subproceso([program, *args], None, "asyncio.create_subprocess_exec")
        return exec_original(program, *args, **kwargs)

    create_subprocess_exec_vigilado._rams_sin_efectos = True  # type: ignore[attr-defined]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec_vigilado)
    monkeypatch.setattr(asyncio.subprocess, "create_subprocess_exec", create_subprocess_exec_vigilado)


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
        "RAMS_TELEGRAM_POLL": "0", "RAMS_CONNECTOME_SEED": "7",
        "RAMS_ESCRITORIO_TOKEN_FILE": str(tmp_path / "escritorio.json"), "RAMS_TRAVESURAS": "0",
        "RAMS_VISION_CONTINUA": "0", "RAMS_PANTALLA_MODO": "off", ...},
        offline=True, base_dir=RAIZ)
    y luego .asegurar_directorios(). Ningún test toca el %LOCALAPPDATA%\\rams\\escritorio.json real."""
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
        # v0.2: el token de /escritorio nunca va al %LOCALAPPDATA% / $XDG_RUNTIME_DIR reales;
        # travesuras, visión continua y pantalla apagadas (aunque cambien los defectos).
        "RAMS_ESCRITORIO_TOKEN_FILE": str(tmp_path / "escritorio.json"),
        "RAMS_TRAVESURAS": "0",
        "RAMS_VISION_CONTINUA": "0",
        "RAMS_PANTALLA_MODO": "off",
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
