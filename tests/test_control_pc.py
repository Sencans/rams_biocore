"""Tests de ``lobes/control_pc.py`` (v0.2, ``docs/COMPANION.md`` §C2.8, §C2.12 y §C3 "Grupo control-pc").

Nada toca el PC real: la fixture autouse ``sin_efectos_pc`` (conftest) hace fallar cualquier
``os.startfile``, ``SendInput``, ``SetCursorPos`` o subproceso de ``xdg-open``/``wtype``/…; aquí
se inyectan la plataforma, un reloj falso, un Win32 falso (``FakeWin``, con su
``GetAsyncKeyState``), un cliente falso del socket de Hyprland y subprocesos falsos.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from core.contracts import DiagnosticoModulo, EstadoModulo
from lobes import control_pc as cpc_mod
from lobes.control_pc import (
    PATRONES_TRAVESURA,
    SAFE_ALIAS_RE,
    TITULOS_PROPIOS_RE,
    ControlPC,
    ObjetivoVentana,
    ResultadoControl,
    activar_dpi_por_monitor,
)

PID_PROPIO = 99991          # "R.A.M.S." en los tests (calcular_pids_propios parcheado)
PID_APP = 4242


# ---------------------------------------------------------------------------
# Falsos
# ---------------------------------------------------------------------------


class RelojFalso:
    """Reloj monotónico falso; ``dormir`` lo adelanta (sustituye a ``asyncio.sleep``)."""

    def __init__(self, t: float = 10_000.0) -> None:
        self.t = t
        self.al_dormir: Callable[[RelojFalso], None] | None = None

    def __call__(self) -> float:
        return self.t

    async def dormir(self, segundos: float) -> None:
        self.t += max(0.0, float(segundos))
        if self.al_dormir is not None:
            self.al_dormir(self)
        await asyncio.sleep(0)


def ventana(hwnd: int, pid: int, exe: str, titulo: str, clase: str = "Chrome_WidgetWin_1") -> dict[str, Any]:
    return {"hwnd": hwnd, "pid": pid, "tid": hwnd + 1, "exe": exe, "clase": clase, "titulo": titulo}


class FakeWin:
    """Win32 falso con la misma interfaz que ``control_pc._ApiWin``."""

    def __init__(self) -> None:
        self.ventanas: dict[int, dict[str, Any]] = {
            100: ventana(100, PID_APP, "chrome.exe", "WhatsApp - Google Chrome"),
            200: ventana(200, 7777, "Amadeus Desktop.exe", "Amadeus Chat"),
        }
        self.fg: int | None = 100
        self.fg_fn: Callable[[], int | None] | None = None
        self.pos: tuple[int, int] = (500, 400)
        self.movs: list[tuple[int, int]] = []
        self.al_mover: Callable[[FakeWin], None] | None = None
        self.teclas: dict[int, int] = {}
        self.idle: float | None = 30.0
        self.q: int | None = 5
        self.cubre = False
        self.elev: bool | None = False
        self.password: bool | None = False
        self.tecleado: list[int] = []
        self.al_teclear: Callable[[FakeWin], None] | None = None
        self.insertar_menos = False
        self.virtual = (0, 0, 1920, 1080)

    def primer_plano(self) -> int | None:
        return self.fg_fn() if self.fg_fn is not None else self.fg

    def info(self, hwnd: int) -> dict[str, Any] | None:
        datos = self.ventanas.get(hwnd)
        return dict(datos) if datos else None

    def existe(self, hwnd: int) -> bool:
        return hwnd in self.ventanas

    def cursor(self) -> tuple[int, int] | None:
        return self.pos

    def mover_cursor(self, x: int, y: int) -> bool:
        self.movs.append((x, y))
        self.pos = (x, y)
        if self.al_mover is not None:
            self.al_mover(self)
        return True

    def estado_tecla(self, vk: int) -> int:
        return self.teclas.get(vk, 0)

    def inactividad_s(self) -> float | None:
        return self.idle

    def quns(self) -> int | None:
        return self.q

    def cubre_monitor(self, hwnd: int) -> bool:
        return self.cubre

    def elevado(self, pid: int) -> bool | None:
        return self.elev

    def foco_password(self, tid: int) -> bool | None:
        return self.password

    def teclear(self, unidades: list[int]) -> int:
        self.tecleado.extend(unidades)
        if self.al_teclear is not None:
            self.al_teclear(self)
        return 2 * len(unidades) - (1 if self.insertar_menos else 0)

    def escritorio_virtual(self) -> tuple[int, int, int, int] | None:
        return self.virtual


class FakeHypr:
    """Cliente falso del ``.socket.sock`` de Hyprland."""

    def __init__(self) -> None:
        self.activa: dict[str, Any] | None = {
            "address": "0xabc", "pid": 777, "class": "discord", "title": "Discord",
            "fullscreen": 0, "contentType": "none", "inhibitingIdle": False,
        }
        self.cursor: dict[str, Any] | None = {"x": 10, "y": 20}
        self.consultas: list[str] = []

    async def consultar(self, que: str) -> Any:
        self.consultas.append(que)
        return {"activewindow": self.activa, "cursorpos": self.cursor}.get(que)


class FakeProc:
    def __init__(self, codigo: int | None = 0) -> None:
        self._codigo = codigo
        self.returncode: int | None = None
        self.stdin = b""

    async def wait(self) -> int:
        if self._codigo is None:            # se queda vivo (como un xdg-open que no sale)
            await asyncio.sleep(3600)
        self.returncode = self._codigo
        return self._codigo  # type: ignore[return-value]

    async def communicate(self, datos: bytes | None = None) -> tuple[None, None]:
        self.stdin += datos or b""
        self.returncode = self._codigo
        return None, None

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def subprocesos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any], FakeProc]]:
    """Sustituye ``asyncio.create_subprocess_exec`` por un falso que registra las llamadas."""
    llamadas: list[tuple[tuple[Any, ...], dict[str, Any], FakeProc]] = []

    async def falso(programa: Any, *args: Any, **kwargs: Any) -> FakeProc:
        proceso = FakeProc(0)
        llamadas.append(((programa, *args), kwargs, proceso))
        return proceso

    monkeypatch.setattr(asyncio, "create_subprocess_exec", falso)
    return llamadas


@pytest.fixture
def abiertos(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Sustituye ``os.startfile`` (también fuera de Windows) por un registro."""
    registro: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda objetivo, *a, **k: registro.append(objetivo), raising=False)
    return registro


@pytest.fixture
def crear(monkeypatch: pytest.MonkeyPatch, settings_prueba: Any) -> Callable[..., ControlPC]:
    """Construye un ``ControlPC`` aislado: plataforma, Win32/Hyprland, programas y reloj falsos."""
    monkeypatch.setattr(cpc_mod, "_pids_propios", lambda: frozenset({PID_PROPIO}))
    monkeypatch.setattr(cpc_mod, "_descendientes", lambda base: set())

    def fabricar(plataforma: str = "win", *, api: Any = None, hypr: Any = None, reloj: RelojFalso | None = None,
                 programas: dict[str, str | None] | None = None, settings: Any = None,
                 menu: Path | None = None, paquetes: Path | None = None) -> ControlPC:
        monkeypatch.setattr(cpc_mod, "_crear_api_win", lambda: api)
        monkeypatch.setattr(cpc_mod, "_crear_cliente_hypr", lambda: hypr)
        tabla = programas or {}
        monkeypatch.setattr(cpc_mod, "_buscar_programa", lambda nombre: tabla.get(nombre))
        reloj = reloj or RelojFalso()
        # v0.2.1: el índice del menú Inicio nunca lee el del usuario (carpeta vacía por defecto).
        dirs = [menu] if menu is not None else [Path(settings_prueba.data_dir) / "menu_inicio_vacio"]
        control = ControlPC(settings or settings_prueba, reloj=reloj, plataforma=plataforma,
                            dirs_menu_inicio=dirs, dir_paquetes=paquetes)
        control._dormir = reloj.dormir
        return control

    return fabricar


PROGRAMAS_LINUX = {"xdg-open": "/usr/bin/xdg-open", "gtk-launch": "/usr/bin/gtk-launch", "wtype": "/usr/bin/wtype"}


def con_travesuras(settings: Any, **extra: Any) -> Any:
    return settings.con(travesuras_habilitadas=True, **extra)


# ---------------------------------------------------------------------------
# Contratos y piezas puras
# ---------------------------------------------------------------------------


def test_reexporta_contratos_y_titulos_propios() -> None:
    from core import contracts

    assert PATRONES_TRAVESURA == contracts.PATRONES_TRAVESURA
    assert SAFE_ALIAS_RE is contracts.SAFE_ALIAS_RE
    for propio in ("Amadeus", "Amadeus Chat", "Amadeus Overlay", "Amadeus - Google Chrome",
                   "Amadeus — HUD - Brave", "Amadeus Chat – algo"):
        assert TITULOS_PROPIOS_RE.match(propio), propio
    for ajeno in ("WhatsApp", "Amadeus2", "Mi Amadeus", "Amadeus-Google Chrome", ""):
        assert not TITULOS_PROPIOS_RE.match(ajeno), ajeno


def test_objetivo_ventana_ida_y_vuelta() -> None:
    win = ObjetivoVentana(hwnd=123, pid=456, exe="chrome.exe", direccion=None, clase="Chrome_WidgetWin_1")
    hypr = ObjetivoVentana(hwnd=None, pid=77, exe="discord", direccion="0x55d1ab", clase="discord")
    for objetivo in (win, hypr):
        assert set(objetivo.a_args()) == {"hwnd", "pid", "exe", "direccion", "clase"}
        assert ObjetivoVentana.desde_args(objetivo.a_args()) == objetivo
    malos = [
        {**win.a_args(), "hwnd": True},           # bool no es un entero válido
        {**win.a_args(), "pid": "456"},
        {**win.a_args(), "hwnd": -1},
        {**hypr.a_args(), "direccion": "0xZZ"},
        {**hypr.a_args(), "direccion": "55d1ab"},
        {**win.a_args(), "hwnd": None},           # sin hwnd ni dirección
        {**win.a_args(), "exe": "a\nb"},
        {**win.a_args(), "clase": 5},
        "no es un dict",
        None,
    ]
    for args in malos:
        assert ObjetivoVentana.desde_args(args) is None  # type: ignore[arg-type]


def test_resultado_control_copia_datos() -> None:
    datos = {"destino": "x"}
    resultado = ResultadoControl(1, "hola", datos)  # type: ignore[arg-type]
    datos["destino"] = "otro"
    assert resultado.ok is True and resultado.datos == {"destino": "x"}
    assert ResultadoControl(False, "m").datos == {}


def test_enlace_prerrelleno_urlcodifica_y_quita_saltos(crear: Callable[..., ControlPC]) -> None:
    control = crear("otro")
    enlace = control.enlace_prerrelleno("whatsapp", "hola ñ 😀\n(enter)")
    assert enlace == "https://wa.me/?text=hola%20%C3%B1%20%F0%9F%98%80%20%28enter%29"
    assert "\n" not in enlace and "%0A" not in enlace.upper() and "%0D" not in enlace.upper()
    assert control.enlace_prerrelleno("telegram", "a&b=c") == "https://t.me/share/url?url=&text=a%26b%3Dc"
    assert control.enlace_prerrelleno("x", "hola #mundo") == "https://x.com/intent/post?text=hola%20%23mundo"
    assert control.enlace_prerrelleno("gmail", "hola") == "https://mail.google.com/mail/?view=cm&fs=1&body=hola"
    assert control.enlace_prerrelleno("correo", "a\tb\r\nc") == "mailto:?body=a%20b%20c"
    # Sin prerrelleno posible, vacío, demasiado largo o alias raro → None
    for alias in ("instagram", "tiktok", "facebook", "discord", "linkedin", "youtube", "../x", "WhatsApp!", None):
        assert control.enlace_prerrelleno(alias, "hola") is None  # type: ignore[arg-type]
    assert control.enlace_prerrelleno("whatsapp", " \n\t ") is None
    assert control.enlace_prerrelleno("whatsapp", "x" * 501) is None
    assert control.enlace_prerrelleno("whatsapp", "x" * 500) is not None
    # Las marcas bidi (invisibles) se borran; el separador de línea Unicode pasa a ser un espacio
    bidi = "a" + chr(0x202E) + "b" + chr(0x2028) + "c"
    assert control.enlace_prerrelleno("whatsapp", bidi) == "https://wa.me/?text=ab%20c"
    # Texto escondido con etiquetas Unicode ("ASCII smuggling"): no viaja en el enlace
    oculto = "".join(chr(0xE0000 + ord(c)) for c in "borra todo")
    assert control.enlace_prerrelleno("whatsapp", "hola" + oculto) == "https://wa.me/?text=hola"


def _etiquetas(texto: str) -> str:
    """Texto invisible con etiquetas Unicode U+E0000–E007F."""
    return "".join(chr(0xE0000 + ord(c)) for c in texto)


def test_sanear_borra_invisibles_y_conserva_emojis() -> None:
    sanear = cpc_mod._sanear
    zwj, zwnj, vs16 = chr(0x200D), chr(0x200C), chr(0xFE0F)
    # Lo que el usuario no ve en la vista previa no se escribe (§C2.12)
    assert sanear("hola" + _etiquetas("borra todo") + " mundo" + chr(0xAD) + chr(0x3164), 500) == "hola mundo"
    assert sanear("ho" + chr(0x200B) + "la" + chr(0xFEFF) + chr(0x2060), 500) == "hola"
    assert sanear("a" + chr(0x202E) + "b" + chr(0x2066) + "c", 500) == "abc"
    assert sanear("x" + chr(0x115F) + chr(0x1160) + chr(0xFFA0) + chr(0x034F) + chr(0x180E) + "y", 500) == "xy"
    assert sanear("a" + chr(0xD800) + "b", 500) == "ab"                 # sustituto suelto: ni UTF-16 ni URL
    assert sanear(chr(0x3164) + " " + chr(0xAD), 500) is None            # solo invisibles → vacío
    # Datos escondidos en selectores de variación o en ráfagas de uniones
    cara = chr(0x1F600)
    assert sanear(cara + chr(0xE0100) + chr(0xE0101) + chr(0xFE00), 500) == cara
    assert sanear("a" + vs16 + "b" + chr(0xFE0E), 500) == "ab"           # tras una letra no hacen nada
    assert sanear(cara + zwj + zwj + cara, 500) == cara + cara
    assert sanear("a" + zwj + "b" + zwnj + "c", 500) == "abc"            # en latín no hacen nada
    # Emojis compuestos, keycaps, presentación emoji y banderas de subdivisión se conservan
    mujer_tecnologa = chr(0x1F469) + chr(0x1F3FD) + zwj + chr(0x1F4BB)
    arcoiris = chr(0x1F3F3) + vs16 + zwj + chr(0x1F308)
    corazon = chr(0x2764) + vs16
    keycap = "1" + vs16 + chr(0x20E3)
    inglaterra = chr(0x1F3F4) + _etiquetas("gbeng") + chr(0xE007F)
    for emoji in (mujer_tecnologa, arcoiris, corazon, keycap, inglaterra, "#" + vs16 + chr(0x20E3)):
        assert sanear("ok " + emoji, 500) == "ok " + emoji
    assert sanear(corazon + vs16 + vs16, 500) == corazon                  # un solo selector
    assert sanear(chr(0x1F3F4) + _etiquetas("xx") + chr(0xE007F), 500) == chr(0x1F3F4)   # bandera falsa
    persa = chr(0x0645) + chr(0x06CC) + zwnj + chr(0x062E) + chr(0x0648) + chr(0x0627) + chr(0x0647) + chr(0x0645)
    assert sanear(persa, 500) == persa                                    # ZWNJ legítimo entre letras
    # Los identificadores de ventana tampoco aceptan invisibles
    args = {"hwnd": 5, "pid": 6, "exe": "chrome.exe" + _etiquetas("x"), "direccion": None, "clase": "C"}
    assert ObjetivoVentana.desde_args(args) is None
    assert ObjetivoVentana.desde_args({**args, "exe": "chrome.exe"}) is not None


def test_sinonimos_de_rams_destinos(crear: Callable[..., ControlPC], settings_prueba: Any) -> None:
    settings = settings_prueba.con(destinos_items=(("wasap", "whatsapp"),))
    control = crear("otro", settings=settings)
    assert control.enlace_prerrelleno("wasap", "hola") == "https://wa.me/?text=hola"


def test_anti_contrasena(crear: Callable[..., ControlPC]) -> None:
    control = crear("otro")
    assert control.anti_contrasena("482915")                                  # OTP
    assert control.anti_contrasena("mi tarjeta es 4111 1111 1111 1111")       # Luhn
    assert control.anti_contrasena("AIza" + "B" * 35)                         # clave de Google
    assert control.anti_contrasena("password: x")
    assert control.anti_contrasena("contraseña = hunter2")
    assert control.anti_contrasena("hola", titulo="Bancolombia - Sucursal Virtual")
    assert control.anti_contrasena("hola", titulo="Iniciar sesión - Google Chrome")
    assert not control.anti_contrasena("hola, ¿vienes mañana?")
    assert not control.anti_contrasena("Llego tarde, ¿me guardas comida? 😀 #hola")
    assert not control.anti_contrasena("hola", titulo="WhatsApp")


def test_codigo_sin_shell_true_ni_ejecutores_peligrosos() -> None:
    fuente = Path(cpc_mod.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in fuente.replace(" ", "")
    for prohibido in ("os.system(", "subprocess.run(", "subprocess.call(", "create_subprocess_shell",
                      "check_output(", "keybd_event", "mouse_event", "SetForegroundWindow", "VK_RETURN"):
        assert prohibido not in fuente, prohibido
    assert "\r\n" not in fuente   # LF


def test_activar_dpi_fuera_de_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[Any] = []
    monkeypatch.setattr(cpc_mod, "_es_windows", lambda: False)
    monkeypatch.setattr(cpc_mod, "_funcion_dpi", lambda: llamadas.append(1))
    assert activar_dpi_por_monitor() is False
    assert llamadas == []


def test_activar_dpi_en_windows_idempotente_y_sin_lanzar(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[Any] = []

    def falsa(contexto: Any) -> int:
        llamadas.append(getattr(contexto, "value", contexto))
        return 1

    monkeypatch.setattr(cpc_mod, "_es_windows", lambda: True)
    monkeypatch.setattr(cpc_mod, "_dpi_activado", False)
    monkeypatch.setattr(cpc_mod, "_funcion_dpi", lambda: falsa)
    assert activar_dpi_por_monitor() is True
    assert activar_dpi_por_monitor() is True
    assert len(llamadas) == 1                       # idempotente: no se vuelve a llamar
    assert llamadas[0] in (-4, 2 ** (8 * ctypes.sizeof(ctypes.c_void_p)) - 4)

    def rota(contexto: Any) -> int:
        raise OSError("sin user32")

    monkeypatch.setattr(cpc_mod, "_dpi_activado", False)
    monkeypatch.setattr(cpc_mod, "_funcion_dpi", lambda: rota)
    assert activar_dpi_por_monitor() is False       # nunca lanza


# ---------------------------------------------------------------------------
# Disponibilidad y diagnóstico
# ---------------------------------------------------------------------------


def test_disponible_false_sin_ctypes_ni_wtype(crear: Callable[..., ControlPC], correr: Any,
                                               abiertos: list[str]) -> None:
    win = crear("win", api=None)
    assert win.disponible is False
    hypr = crear("hypr", hypr=None, programas={})
    assert hypr.disponible is False
    otro = crear("otro", programas={})
    assert otro.disponible is False

    async def escenario() -> None:
        for control in (win, hypr):
            diag = await control.iniciar()
            assert isinstance(diag, DiagnosticoModulo) and diag.clave == "control_pc"
            assert diag.estado == EstadoModulo.OFF
            objetivo = ObjetivoVentana(hwnd=100, pid=PID_APP, exe="chrome.exe", direccion=None, clase="c")
            resultado = await control.escribir_texto("hola", objetivo=objetivo, espera_foco_s=0)
            assert isinstance(resultado, ResultadoControl) and not resultado.ok
            assert await control.puede_travesura(presente=True, voz_activa=False, confirmacion_pendiente=False,
                                                 video_activo=False) in (
                (False, "no disponible en este sistema"), (False, "sin estado de botones en Wayland"))
            r = await control.empujon_cursor("empujon", 10)
            assert not r.ok and r.datos["abortada_usuario"] is False
            assert await control.no_molestar() == (False, "")
            await control.cerrar()

    correr(escenario())
    assert win.ventana_activa() is None and win.ventana_previa() is None and win.cursor() is None
    assert win.inactividad_s() is None and win.quieto() is None
    assert abiertos == []


def test_iniciar_diagnostico_y_sondeo(crear: Callable[..., ControlPC], monkeypatch: pytest.MonkeyPatch,
                                      correr: Any) -> None:
    monkeypatch.setattr(cpc_mod, "PERIODO_SONDEO_S", 0.005)
    monkeypatch.setattr(cpc_mod, "_resolver_catalogo_win", lambda base: dict(base))
    api = FakeWin()
    control = crear("win", api=api)
    assert control.disponible is True

    async def escenario() -> None:
        diag = await control.iniciar()
        assert diag.clave == "control_pc" and diag.estado == EstadoModulo.OK
        assert "travesuras OFF" in diag.detalle
        await asyncio.sleep(0.05)                    # el sondeo real (asyncio) llena el anillo
        assert control._tarea_sondeo is not None and not control._tarea_sondeo.done()
        await control.cerrar()
        assert control._tarea_sondeo is None

    correr(escenario())
    assert control.ventana_previa() == ObjetivoVentana(hwnd=100, pid=PID_APP, exe="chrome.exe", direccion=None,
                                                       clase="Chrome_WidgetWin_1")


def test_diagnostico_hyprland(crear: Callable[..., ControlPC], monkeypatch: pytest.MonkeyPatch,
                              correr: Any) -> None:
    monkeypatch.setattr(cpc_mod, "_resolver_catalogo_linux", lambda base, gtk: dict(base))
    completo = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)
    sin_wtype = crear("hypr", hypr=FakeHypr(), programas={"xdg-open": "/usr/bin/xdg-open"})
    sin_socket = crear("hypr", hypr=None, programas=PROGRAMAS_LINUX)

    async def escenario() -> None:
        assert (await completo.iniciar()).estado == EstadoModulo.OK
        assert (await sin_wtype.iniciar()).estado == EstadoModulo.DEGRADADO
        assert (await sin_socket.iniciar()).estado == EstadoModulo.DEGRADADO
        for control in (completo, sin_wtype, sin_socket):
            await control.cerrar()

    correr(escenario())


# ---------------------------------------------------------------------------
# Abrir, buscar y prerrellenar
# ---------------------------------------------------------------------------


def test_abrir_buscar_prerrellenar_en_windows(crear: Callable[..., ControlPC], abiertos: list[str],
                                              correr: Any) -> None:
    control = crear("win", api=FakeWin())

    async def escenario() -> None:
        r = await control.abrir_destino("youtube")
        assert isinstance(r, ResultadoControl) and r.ok and r.datos["destino"] == "youtube"
        r = await control.buscar_en("youtube", "Steins;Gate 0 OST")
        assert r.ok and r.datos["len"] == len("Steins;Gate 0 OST")
        r = await control.prerrellenar("whatsapp", "Llego tarde\n¿me guardas comida?")
        assert r.ok and "No envié nada" in r.mensaje
        assert "Llego" not in r.mensaje and r.datos["len"] == len("Llego tarde ¿me guardas comida?")
        assert re.fullmatch(r"[0-9a-f]{12}", r.datos["sha256_12"])
        r = await control.buscar_en("navegador", "1984")      # un año no es un OTP en una búsqueda
        assert r.ok

    correr(escenario())
    assert abiertos == [
        "https://www.youtube.com/",
        "https://www.youtube.com/results?search_query=Steins%3BGate%200%20OST",
        "https://wa.me/?text=Llego%20tarde%20%C2%BFme%20guardas%20comida%3F",
        "https://duckduckgo.com/?q=1984",
    ]


def test_abrir_y_prerrellenar_en_linux_sin_shell(crear: Callable[..., ControlPC],
                                                 subprocesos: list[Any], correr: Any) -> None:
    control = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)

    async def escenario() -> None:
        assert (await control.abrir_destino("x")).ok
        assert (await control.prerrellenar("x", "hola mundo")).ok
        assert (await control.buscar_en("spotify", "lofi")).ok

    correr(escenario())
    argv = [llamada[0] for llamada in subprocesos]
    assert argv == [
        ("/usr/bin/xdg-open", "https://x.com/home"),
        ("/usr/bin/xdg-open", "https://x.com/intent/post?text=hola%20mundo"),
        ("/usr/bin/xdg-open", "https://open.spotify.com/search/lofi"),
    ]
    for _, kwargs, _ in subprocesos:
        assert "shell" not in kwargs and kwargs.get("stdin") == asyncio.subprocess.DEVNULL


def test_linux_usa_gtk_launch_si_hay_desktop(crear: Callable[..., ControlPC], subprocesos: list[Any],
                                            monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                            correr: Any) -> None:
    aplicaciones = tmp_path / "share" / "applications"
    aplicaciones.mkdir(parents=True)
    (aplicaciones / "org.telegram.desktop.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "nada"))
    control = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)

    async def escenario() -> None:
        await control.iniciar()
        assert control.destinos()["telegram"]["via"] == "gtk-launch"
        assert (await control.abrir_destino("telegram")).ok
        assert (await control.abrir_destino("discord")).ok          # sin .desktop → la web
        await control.cerrar()

    correr(escenario())
    assert [llamada[0] for llamada in subprocesos] == [
        ("/usr/bin/gtk-launch", "org.telegram.desktop"),
        ("/usr/bin/xdg-open", "https://discord.com/app"),
    ]


def test_lanzador_con_error_o_que_sigue_vivo(crear: Callable[..., ControlPC], monkeypatch: pytest.MonkeyPatch,
                                            correr: Any) -> None:
    codigos = iter([3, None])

    async def falso(programa: Any, *args: Any, **kwargs: Any) -> FakeProc:
        return FakeProc(next(codigos))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", falso)
    monkeypatch.setattr(cpc_mod, "ESPERA_FALLO_LANZADOR_S", 0.01)
    control = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)

    async def escenario() -> None:
        r = await control.abrir_destino("youtube")
        assert not r.ok and "código 3" in r.mensaje
        r = await control.abrir_destino("youtube")
        assert r.ok                                   # xdg-open sigue vivo: se vigila aparte
        await control.cerrar()

    correr(escenario())


def test_destinos_rechazados_y_sin_excepciones(crear: Callable[..., ControlPC], abiertos: list[str],
                                               correr: Any) -> None:
    control = crear("win", api=FakeWin())

    async def escenario() -> None:
        for alias in (None, "", "../etc", "WHATS APP", "noexiste", 42):
            r = await control.abrir_destino(alias)  # type: ignore[arg-type]
            assert isinstance(r, ResultadoControl) and not r.ok
        assert not (await control.buscar_en("instagram", "gatos")).ok      # sin búsqueda
        assert not (await control.buscar_en("youtube", "")).ok
        assert not (await control.buscar_en("youtube", "x" * 121)).ok
        assert not (await control.buscar_en("youtube", "AIza" + "C" * 35)).ok
        assert not (await control.prerrellenar("instagram", "hola")).ok     # sin prerrelleno
        assert not (await control.prerrellenar("whatsapp", "482915")).ok    # parece un OTP
        assert not (await control.prerrellenar("whatsapp", None)).ok        # type: ignore[arg-type]

    correr(escenario())
    assert abiertos == []
    control.set_caja_avatar("basura")  # type: ignore[arg-type]
    control.set_caja_avatar((1, 2, 0, 5))
    assert control._caja is None
    control.excluir_pids(None)  # type: ignore[arg-type]
    control.excluir_pids(["a", -1, True, 0])  # type: ignore[list-item]
    assert control.pids_excluidos() == frozenset()


def test_tasas_abrir_y_prerrellenar(crear: Callable[..., ControlPC], abiertos: list[str], correr: Any) -> None:
    reloj = RelojFalso()
    control = crear("win", api=FakeWin(), reloj=reloj)

    async def escenario() -> None:
        for _ in range(6):
            assert (await control.abrir_destino("youtube")).ok
        assert not (await control.buscar_en("youtube", "gatos")).ok        # 7.ª en el minuto
        reloj.t += 61
        assert (await control.buscar_en("youtube", "gatos")).ok
        assert (await control.prerrellenar("x", "uno")).ok
        reloj.t += 10
        r = await control.prerrellenar("x", "dos")
        assert not r.ok and "segundos" in r.mensaje                       # ≤ 1 cada 20 s
        reloj.t += 11
        assert (await control.prerrellenar("x", "tres")).ok

    correr(escenario())
    assert len(abiertos) == 9


def test_prerrellenar_en_x_avisa_de_280(crear: Callable[..., ControlPC], abiertos: list[str], correr: Any) -> None:
    control = crear("win", api=FakeWin())
    r = correr(control.prerrellenar("x", "palabra " * 40))
    assert r.ok and "280" in r.mensaje


# ---------------------------------------------------------------------------
# Ventana previa
# ---------------------------------------------------------------------------


def test_ventana_previa_excluye_propias_en_windows(crear: Callable[..., ControlPC]) -> None:
    api = FakeWin()
    api.ventanas = {
        301: ventana(301, 6000, "notepad.exe", "Sin título: Bloc de notas", clase="Notepad"),
        302: ventana(302, PID_PROPIO, "WindowsTerminal.exe", "R.A.M.S."),
        303: ventana(303, 5555, "Amadeus Desktop.exe", "algo"),
        304: ventana(304, 6100, "Amadeus Desktop.exe", "Amadeus Chat"),
        305: ventana(305, 6200, "chrome.exe", "Amadeus - Google Chrome"),
        306: ventana(306, 6300, "explorer.exe", "", clase="Shell_TrayWnd"),
        307: ventana(307, 6400, "LockApp.exe", "", clase="Windows.UI.Core.CoreWindow"),
    }
    control = crear("win", api=api)
    control.excluir_pids([5555])
    assert control.pids_excluidos() == frozenset({5555})
    for hwnd in (301, 302, 303, 304, 305, 306, 307):
        api.fg = hwnd
        assert control.ventana_activa().hwnd == hwnd  # type: ignore[union-attr]
    previa = control.ventana_previa()
    assert previa == ObjetivoVentana(hwnd=301, pid=6000, exe="notepad.exe", direccion=None, clase="Notepad")
    # El objetivo viaja en AccionPC.args y vuelve igual
    assert ObjetivoVentana.desde_args(previa.a_args()) == previa  # type: ignore[union-attr]
    # Si después se excluye su pid (p. ej. resultó ser de Amadeus Desktop), deja de ser la previa
    control.excluir_pids([6000])
    assert control.ventana_previa() is None


def test_consola_propia_en_conhost_nunca_es_objetivo(crear: Callable[..., ControlPC], correr: Any) -> None:
    class ConConsola(FakeWin):
        def consola(self) -> int | None:
            return 500

    api = ConConsola()
    api.ventanas[500] = ventana(500, 8000, "conhost.exe", "C:\\rams\\main.py", clase="ConsoleWindowClass")
    control = crear("win", api=api)
    api.fg = 100
    control.ventana_activa()
    api.fg = 500                                     # el usuario escribe en la consola de R.A.M.S.
    control.ventana_activa()
    assert control.ventana_previa().hwnd == 100  # type: ignore[union-attr]
    consola = ObjetivoVentana(hwnd=500, pid=8000, exe="conhost.exe", direccion=None, clase="ConsoleWindowClass")
    assert correr(control.puede_teclear(consola)) == (False, "es una ventana de Amadeus")
    r = correr(control.escribir_texto("hola", objetivo=consola, espera_foco_s=0))
    assert not r.ok and api.tecleado == []


def test_ventana_previa_conserva_la_ventana_de_la_pestana_del_hud_y_olvida_cerradas(
        crear: Callable[..., ControlPC]) -> None:
    api = FakeWin()
    api.ventanas = {
        401: ventana(401, 6000, "notepad.exe", "Notas", clase="Notepad"),
        402: ventana(402, 6500, "chrome.exe", "WhatsApp - Google Chrome"),
        403: ventana(403, 6600, "brave.exe", "Amadeus - Brave"),
    }
    control = crear("win", api=api)
    api.fg = 401
    control.ventana_activa()
    api.fg = 402
    control.ventana_activa()
    assert control.ventana_previa().hwnd == 402  # type: ignore[union-attr]
    # El usuario cambia a la pestaña del HUD en la MISMA ventana de Chrome: sigue siendo el objetivo
    # (la espera del foco y puede_teclear revalidan el título antes de escribir).
    api.ventanas[402]["titulo"] = "Amadeus - Google Chrome"
    control.ventana_activa()
    assert control.ventana_previa().hwnd == 402  # type: ignore[union-attr]
    # Una ventana que solo ha mostrado el HUD nunca entra en el anillo
    api.fg = 403
    control.ventana_activa()
    assert control.ventana_previa().hwnd == 402  # type: ignore[union-attr]
    # Pasar por el Bloc de notas y volver a Chrome (aún con el HUD delante) no adelanta a Chrome
    api.fg = 401
    control.ventana_activa()
    api.fg = 402
    control.ventana_activa()
    assert control.ventana_previa().hwnd == 401  # type: ignore[union-attr]
    api.fg = None
    del api.ventanas[401]                                        # se cerró el Bloc de notas
    assert control.ventana_previa().hwnd == 402  # type: ignore[union-attr]
    # Si el hwnd lo reutiliza otro proceso con un título propio, la entrada vieja se descarta
    api.ventanas[402] = ventana(402, 9999, "msedge.exe", "Amadeus - Microsoft Edge")
    api.fg = 402
    control.ventana_activa()
    api.fg = None
    assert control.ventana_previa() is None


def test_escribir_espera_a_que_la_pestana_del_hud_deje_paso(crear: Callable[..., ControlPC],
                                                            correr: Any) -> None:
    reloj = RelojFalso()
    api = FakeWin()
    api.ventanas = {402: ventana(402, 6500, "chrome.exe", "WhatsApp - Google Chrome")}
    control = crear("win", api=api, reloj=reloj)
    api.fg = 402
    control.ventana_activa()
    api.ventanas[402]["titulo"] = "Amadeus - Google Chrome"     # chatea en el HUD (misma ventana)
    control.ventana_activa()
    objetivo = control.ventana_previa()
    assert objetivo is not None and objetivo.hwnd == 402
    # Confirma desde el HUD: con el HUD delante no cuenta como foco y no escribe nada
    r = correr(control.escribir_texto("hola", objetivo=objetivo, espera_foco_s=2))
    assert not r.ok and "a tiempo" in r.mensaje and api.tecleado == []
    # A los 3 s cambia a la pestaña de WhatsApp: entonces escribe
    reloj.t += 60
    inicio = reloj.t

    def cambia_de_pestana(r_: RelojFalso) -> None:
        if r_.t >= inicio + 3.0:
            api.ventanas[402]["titulo"] = "WhatsApp - Google Chrome"

    reloj.al_dormir = cambia_de_pestana
    r = correr(control.escribir_texto("hola", objetivo=objetivo))
    assert r.ok, r.mensaje
    assert api.tecleado == _utf16("hola") and reloj.t - inicio >= 3.0

    # Hyprland: la misma regla con el título de activewindow
    hypr = FakeHypr()
    control_h = crear("hypr", hypr=hypr, programas=PROGRAMAS_LINUX)
    discord = ObjetivoVentana(hwnd=None, pid=777, exe="discord", direccion="0xabc", clase="discord")
    assert correr(control_h._tiene_foco(discord)) is True
    hypr.activa = {**(hypr.activa or {}), "title": "Amadeus - Firefox"}
    assert correr(control_h._tiene_foco(discord)) is False


def test_ventana_previa_en_hyprland(crear: Callable[..., ControlPC], correr: Any) -> None:
    hypr = FakeHypr()
    control = crear("hypr", hypr=hypr, programas=PROGRAMAS_LINUX)

    async def escenario() -> None:
        await control._muestrear_hypr()
        hypr.activa = {"address": "0xdef", "pid": PID_PROPIO, "class": "kitty", "title": "rams"}
        await control._muestrear_hypr()
        hypr.activa = {"address": "0x123", "pid": 888, "class": "chrome", "title": "Amadeus Chat"}
        await control._muestrear_hypr()
        hypr.activa = {"address": "0x124", "pid": 889, "class": "polkit-gnome-authentication-agent-1",
                       "title": "Autenticación"}
        await control._muestrear_hypr()

    correr(escenario())
    assert control.ventana_activa() is not None and control.ventana_activa().direccion == "0x124"  # type: ignore[union-attr]
    assert control.ventana_previa() == ObjetivoVentana(hwnd=None, pid=777, exe="discord", direccion="0xabc",
                                                       clase="discord")
    assert control.cursor() == (10, 20)
    assert control.inactividad_s() is None


def test_excluir_pids_con_descendientes_psutil(monkeypatch: pytest.MonkeyPatch, settings_prueba: Any,
                                               correr: Any) -> None:
    class Hijo:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class Proceso:
        def __init__(self, pid: int) -> None:
            if pid != 5555:
                raise ProcessLookupError(pid)
            self._pid = pid

        def children(self, recursive: bool = False) -> list[Hijo]:
            assert recursive is True
            return [Hijo(5556), Hijo(5557)]

    monkeypatch.setattr(cpc_mod, "psutil", type("FakePsutil", (), {"Process": Proceso}))
    monkeypatch.setattr(cpc_mod, "_pids_propios", lambda: frozenset({PID_PROPIO}))
    monkeypatch.setattr(cpc_mod, "_crear_api_win", lambda: FakeWin())
    control = ControlPC(settings_prueba, plataforma="win")
    control.excluir_pids([5555, 1234])          # sin loop: cálculo directo
    assert control.pids_excluidos() == frozenset({5555, 1234, 5556, 5557})

    otro = ControlPC(settings_prueba, plataforma="win")

    async def escenario() -> None:
        otro.excluir_pids([5555])                # con loop: en un hilo
        assert 5555 in otro.pids_excluidos()
        for _ in range(100):
            if 5557 in otro.pids_excluidos():
                break
            await asyncio.sleep(0.01)
        await otro.cerrar()

    correr(escenario())
    assert otro.pids_excluidos() == frozenset({5555, 5556, 5557})


# ---------------------------------------------------------------------------
# Escribir texto
# ---------------------------------------------------------------------------


def _utf16(texto: str) -> list[int]:
    datos = texto.encode("utf-16-le")
    return [int.from_bytes(datos[i:i + 2], "little") for i in range(0, len(datos), 2)]


def _objetivo_app() -> ObjetivoVentana:
    return ObjetivoVentana(hwnd=100, pid=PID_APP, exe="chrome.exe", direccion=None, clase="Chrome_WidgetWin_1")


def test_escribir_texto_sanea_y_espera_el_foco(crear: Callable[..., ControlPC], correr: Any) -> None:
    reloj = RelojFalso()
    api = FakeWin()
    inicio = reloj.t
    api.fg_fn = lambda: 100 if reloj.t >= inicio + 2.0 else 200     # el usuario hace clic a los 2 s
    control = crear("win", api=api, reloj=reloj)
    texto = "hola\r\nmundo\tx ñ 😀 " + "nos vemos " * 4
    r = correr(control.escribir_texto(texto, objetivo=_objetivo_app()))
    limpio = "hola mundo x ñ 😀 " + " ".join(["nos vemos"] * 4)
    assert r.ok, r.mensaje
    assert "No envié nada" in r.mensaje and "hola" not in r.mensaje
    assert api.tecleado == _utf16(limpio)
    assert not {0x0A, 0x0D, 0x09} & set(api.tecleado)
    assert r.datos["len"] == len(limpio) and r.datos["escritos"] == len(limpio)
    assert 2.0 <= reloj.t - inicio < 5.0
    assert control.ts_ultima_inyeccion() is not None and control.ts_ultima_inyeccion() > inicio


def test_escribir_texto_aborta_sin_foco_en_10_s(crear: Callable[..., ControlPC], correr: Any) -> None:
    reloj = RelojFalso()
    api = FakeWin()
    api.fg = 200                                     # el foco se queda en el chat de Amadeus
    control = crear("win", api=api, reloj=reloj)
    inicio = reloj.t
    r = correr(control.escribir_texto("hola", objetivo=_objetivo_app()))
    assert not r.ok
    assert r.mensaje == "No tuviste el foco en chrome.exe a tiempo; no escribí nada."
    assert api.tecleado == []
    assert 10.0 <= reloj.t - inicio < 10.5
    assert control.ts_ultima_inyeccion() is None


def test_escribir_texto_rechazos_previos(crear: Callable[..., ControlPC], correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api)

    async def escenario() -> None:
        assert not (await control.escribir_texto("482915", objetivo=_objetivo_app())).ok          # OTP
        assert not (await control.escribir_texto("", objetivo=_objetivo_app())).ok
        assert not (await control.escribir_texto("x" * 501, objetivo=_objetivo_app())).ok
        assert not (await control.escribir_texto("hola", objetivo="basura")).ok                   # type: ignore[arg-type]
        propia = ObjetivoVentana(hwnd=100, pid=PID_PROPIO, exe="py.exe", direccion=None, clase="c")
        assert not (await control.escribir_texto("hola", objetivo=propia)).ok
        bloqueo = ObjetivoVentana(hwnd=100, pid=PID_APP, exe="LockApp.exe", direccion=None, clase="c")
        assert not (await control.escribir_texto("hola", objetivo=bloqueo)).ok
        de_hyprland = ObjetivoVentana(hwnd=None, pid=PID_APP, exe="discord", direccion="0xabc", clase="discord")
        r = await control.escribir_texto("hola", objetivo=de_hyprland)
        assert not r.ok and "no es de este sistema" in r.mensaje
        # También acepta el dict de AccionPC.args
        r = await control.escribir_texto("hola", objetivo=_objetivo_app().a_args())  # type: ignore[arg-type]
        assert r.ok

    correr(escenario())
    assert api.tecleado == _utf16("hola")


def test_puede_teclear_revalida(crear: Callable[..., ControlPC], correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api)
    objetivo = _objetivo_app()

    def comprobar() -> tuple[bool, str]:
        return correr(control.puede_teclear(objetivo))

    assert comprobar() == (True, "ok")
    api.elev = True
    assert not comprobar()[0]
    api.elev = None                                    # sin poder comprobarlo también se rechaza
    assert not comprobar()[0]
    api.elev = False
    api.password = True
    assert comprobar() == (False, "el cuadro con el foco es de contraseña")
    api.password = None
    api.teclas = {0x11: 0x8000}                        # Ctrl pulsada
    assert not comprobar()[0]
    api.teclas = {}
    api.ventanas[100]["titulo"] = "Iniciar sesión - Google Chrome"
    assert not comprobar()[0]
    api.ventanas[100]["titulo"] = "WhatsApp"
    api.fg = 200
    assert comprobar() == (False, "chrome.exe no tiene el foco")
    api.fg = 100
    api.ventanas[100]["pid"] = 1                       # el hwnd ahora es de otro proceso
    assert comprobar() == (False, "la ventana cambió de app")
    api.ventanas[100]["pid"] = PID_APP
    control.excluir_pids([PID_APP])
    assert comprobar() == (False, "es una ventana de Amadeus")


def test_escribir_para_si_cambia_el_foco_o_el_usuario_teclea(crear: Callable[..., ControlPC],
                                                             correr: Any) -> None:
    texto = "hola " * 8                               # 39 caracteres saneados: 3 trozos de 16
    api = FakeWin()
    control = crear("win", api=api, reloj=RelojFalso())

    def robar_foco(fake: FakeWin) -> None:
        fake.fg = 200

    api.al_teclear = robar_foco
    r = correr(control.escribir_texto(texto, objetivo=_objetivo_app()))
    assert not r.ok and r.datos["escritos"] == 16 and r.datos["abortada_usuario"] is False
    assert "16 de 39" in r.mensaje and len(api.tecleado) == 16

    api2 = FakeWin()
    control2 = crear("win", api=api2, reloj=RelojFalso())

    def teclea(fake: FakeWin) -> None:
        fake.teclas = {0x41: 0x8001}                   # tecla "A" del usuario

    api2.al_teclear = teclea
    r2 = correr(control2.escribir_texto(texto, objetivo=_objetivo_app()))
    assert not r2.ok and r2.datos["abortada_usuario"] is True and len(api2.tecleado) == 16

    api3 = FakeWin()
    api3.insertar_menos = True                         # UIPI/SendInput incompleto
    control3 = crear("win", api=api3, reloj=RelojFalso())
    r3 = correr(control3.escribir_texto(texto, objetivo=_objetivo_app()))
    assert not r3.ok and "bloqueó" in r3.mensaje


def test_escribir_tasas_y_una_a_la_vez(crear: Callable[..., ControlPC], correr: Any) -> None:
    reloj = RelojFalso()
    api = FakeWin()
    control = crear("win", api=api, reloj=reloj)

    async def escenario() -> None:
        assert (await control.escribir_texto("uno", objetivo=_objetivo_app())).ok
        r = await control.escribir_texto("dos", objetivo=_objetivo_app())
        assert not r.ok and "segundos" in r.mensaje               # ≤ 1 cada 30 s
        reloj.t += 31
        api.fg = 200
        tarea = asyncio.ensure_future(control.escribir_texto("tres", objetivo=_objetivo_app()))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        r = await control.escribir_texto("cuatro", objetivo=_objetivo_app())
        assert not r.ok and "Ya estoy escribiendo" in r.mensaje
        assert not (await tarea).ok                                # sin foco

    correr(escenario())


def test_escribir_en_hyprland_con_wtype_por_stdin(crear: Callable[..., ControlPC], subprocesos: list[Any],
                                                 correr: Any) -> None:
    hypr = FakeHypr()
    control = crear("hypr", hypr=hypr, programas=PROGRAMAS_LINUX)
    objetivo = ObjetivoVentana(hwnd=None, pid=777, exe="discord", direccion="0xabc", clase="discord")
    texto = "hola\nqué tal 😀 " + "sí señor " * 5
    r = correr(control.escribir_texto(texto, objetivo=objetivo))
    assert r.ok, r.mensaje
    limpio = "hola qué tal 😀 " + " ".join(["sí señor"] * 5)
    assert all(llamada[0] == ("/usr/bin/wtype", "-d", "8", "-") for llamada in subprocesos)
    assert all("shell" not in llamada[1] for llamada in subprocesos)
    enviado = b"".join(llamada[2].stdin for llamada in subprocesos)
    assert enviado == limpio.encode("utf-8") and b"\n" not in enviado and b"\r" not in enviado
    assert len(subprocesos) == 2                                   # trozos de 32 caracteres
    assert control.ts_ultima_inyeccion() is not None
    # Sin foco en la dirección pedida: no escribe nada
    hypr.activa = {"address": "0xfff", "pid": 1, "class": "kitty", "title": "x"}
    antes = len(subprocesos)
    r = correr(control.escribir_texto("hola", objetivo=objetivo, espera_foco_s=1.0))
    assert not r.ok and len(subprocesos) == antes


# ---------------------------------------------------------------------------
# No molestar e inactividad
# ---------------------------------------------------------------------------


def test_no_molestar_windows_quns(crear: Callable[..., ControlPC], correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api)
    esperado = {1: (False, ""), 2: (True, "pantalla_completa"), 3: (True, "juego"), 4: (True, "presentacion"),
                5: (False, ""), 6: (False, ""), None: (False, "")}
    for quns, resultado in esperado.items():
        api.q = quns
        assert correr(control.no_molestar()) == resultado


def test_no_molestar_hyprland(crear: Callable[..., ControlPC], correr: Any) -> None:
    hypr = FakeHypr()
    control = crear("hypr", hypr=hypr, programas=PROGRAMAS_LINUX)
    assert correr(control.no_molestar()) == (False, "")
    hypr.activa = {**(hypr.activa or {}), "fullscreen": 2}
    assert correr(control.no_molestar()) == (True, "pantalla_completa")
    hypr.activa = {**hypr.activa, "fullscreen": 3}                     # FSMODE_MAX: pantalla completa
    assert correr(control.no_molestar()) == (True, "pantalla_completa")
    hypr.activa = {**hypr.activa, "fullscreen": 1}                     # maximizada: NO es pantalla completa
    assert correr(control.no_molestar()) == (False, "")
    # Hyprland < 0.42: fullscreen booleano + fullscreenMode (0 pantalla completa, 1 maximizada)
    hypr.activa = {**hypr.activa, "fullscreen": True, "fullscreenMode": 1}
    assert correr(control.no_molestar()) == (False, "")
    hypr.activa = {**hypr.activa, "fullscreen": True, "fullscreenMode": 0}
    assert correr(control.no_molestar()) == (True, "pantalla_completa")
    hypr.activa = {**hypr.activa, "fullscreen": False, "fullscreenMode": 0}
    assert correr(control.no_molestar()) == (False, "")
    hypr.activa = {**hypr.activa, "fullscreen": 0, "contentType": "game"}
    assert correr(control.no_molestar()) == (True, "juego")
    hypr.activa = {**hypr.activa, "contentType": "video", "inhibitingIdle": True}
    assert correr(control.no_molestar()) == (True, "ocupado")


def test_quieto_e_inactividad(crear: Callable[..., ControlPC], monkeypatch: pytest.MonkeyPatch,
                              tmp_path: Path) -> None:
    api = FakeWin()
    control = crear("win", api=api)
    api.idle = 2.0
    assert control.inactividad_s() == 2.0 and control.quieto() is False
    api.idle = 6.0
    assert control.quieto() is True
    api.idle = None
    assert control.quieto() is None

    hypr = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert hypr.quieto() is None
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert hypr.quieto() is False
    (tmp_path / "rams").mkdir()
    (tmp_path / "rams" / "quieto").write_text("", encoding="utf-8")
    assert hypr.quieto() is True
    assert hypr.inactividad_s() is None


# ---------------------------------------------------------------------------
# Travesuras
# ---------------------------------------------------------------------------


def _puede(control: ControlPC, correr: Any, **cambios: bool) -> tuple[bool, str]:
    contexto = {"presente": True, "voz_activa": False, "confirmacion_pendiente": False, "video_activo": False}
    contexto.update(cambios)
    return correr(control.puede_travesura(**contexto))


def test_puede_travesura_en_hyprland(crear: Callable[..., ControlPC], settings_prueba: Any, correr: Any) -> None:
    control = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX, settings=con_travesuras(settings_prueba))
    assert _puede(control, correr) == (False, "sin estado de botones en Wayland")
    r = correr(control.empujon_cursor("empujon", 10))
    assert not r.ok and "Wayland" in r.mensaje


def test_vetos_de_travesura(crear: Callable[..., ControlPC], settings_prueba: Any, correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api, settings=con_travesuras(settings_prueba))
    assert _puede(control, correr) == (True, "ok")
    assert _puede(control, correr, video_activo=True) == (False, "hay un vídeo en reproducción")
    assert _puede(control, correr, voz_activa=True) == (False, "estoy hablando")
    assert _puede(control, correr, confirmacion_pendiente=True) == (False, "hay una confirmación pendiente")
    assert _puede(control, correr, presente=False) == (False, "no estás presente")

    api.ventanas[100]["exe"] = "Zoom.exe"                               # app de llamada (vision_apps_pausa)
    ok, motivo = _puede(control, correr)
    assert not ok and "llamada" in motivo
    api.ventanas[100]["exe"] = "chrome.exe"

    api.q = 3                                                           # juego D3D a pantalla completa
    ok, motivo = _puede(control, correr)
    assert not ok and "estado 3" in motivo
    api.q = 1                                                           # QUNS != 5 también veta
    assert not _puede(control, correr)[0]
    api.q = 5

    api.ventanas[100]["exe"] = "LockApp.exe"
    assert _puede(control, correr) == (False, "sesión bloqueada o diálogo de seguridad")
    api.ventanas[100]["exe"] = "chrome.exe"

    api.cubre = True
    assert _puede(control, correr) == (False, "hay una ventana a pantalla completa")
    api.fg = 200                                                        # la propia (Amadeus) no cuenta
    assert _puede(control, correr) == (True, "ok")
    api.fg = 100
    api.cubre = False

    api.idle = 2.0
    assert _puede(control, correr) == (False, "estás usando el teclado o el ratón")
    api.idle = 601.0
    assert _puede(control, correr) == (False, "pareces ausente")
    api.idle = None
    assert not _puede(control, correr)[0]
    api.idle = 30.0

    api.teclas = {0x01: 0x8000}
    assert _puede(control, correr) == (False, "hay botones o teclas pulsadas")
    api.teclas = {}

    control._escribiendo = True
    assert _puede(control, correr) == (False, "estoy escribiendo en otra app")
    control._escribiendo = False
    assert _puede(control, correr) == (True, "ok")


def test_travesuras_desactivadas_por_defecto(crear: Callable[..., ControlPC], correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api)                                    # RAMS_TRAVESURAS=0
    assert _puede(control, correr) == (False, "desactivadas en la configuración")
    r = correr(control.empujon_cursor("empujon", 10))
    assert not r.ok and api.movs == []


@pytest.mark.parametrize("patron", PATRONES_TRAVESURA)
def test_empujon_respeta_amplitud_y_patron(patron: str, crear: Callable[..., ControlPC], settings_prueba: Any,
                                           correr: Any) -> None:
    api = FakeWin()
    reloj = RelojFalso()
    control = crear("win", api=api, reloj=reloj, settings=con_travesuras(settings_prueba))
    control.set_caja_avatar((900, 300, 340, 600))
    inicio = api.pos
    r = correr(control.empujon_cursor(patron, 20, segundos=0.5))
    assert r.ok, r.mensaje
    assert r.datos["patron"] == patron and r.datos["abortada_usuario"] is False
    assert 4 <= len(api.movs) <= 24 and r.datos["pasos"] == len(api.movs)
    assert all(math.hypot(x - inicio[0], y - inicio[1]) <= 20 for x, y in api.movs)
    assert max(math.hypot(x - inicio[0], y - inicio[1]) for x, y in api.movs) >= 5
    if patron in ("circulo", "temblor"):
        assert api.movs[-1] == inicio                                  # vuelve a donde estaba
    if patron == "hacia_avatar":
        centro = (900 + 170, 300 + 300)
        antes = math.hypot(centro[0] - inicio[0], centro[1] - inicio[1])
        despues = math.hypot(centro[0] - api.movs[-1][0], centro[1] - api.movs[-1][1])
        assert despues < antes - 15                                     # se acercó al avatar
    assert reloj.t - 10_000.0 <= 0.6 + 1e-9                            # ≤ 600 ms


def test_empujon_topes_de_amplitud_y_duracion(crear: Callable[..., ControlPC], settings_prueba: Any,
                                              correr: Any) -> None:
    api = FakeWin()
    reloj = RelojFalso()
    settings = con_travesuras(settings_prueba, travesura_amplitud_max_px=15)
    control = crear("win", api=api, reloj=reloj, settings=settings)
    inicio = api.pos
    r = correr(control.empujon_cursor("empujon", 500, segundos=30))
    assert r.ok
    assert all(math.hypot(x - inicio[0], y - inicio[1]) <= 15 for x, y in api.movs)
    assert len(api.movs) <= 24 and reloj.t - 10_000.0 <= 0.6 + 1e-9
    for malo in ("bailar", None, 3):
        assert not correr(control.empujon_cursor(malo, 10)).ok  # type: ignore[arg-type]
    for amplitud in (0, -3, True, "10", 2.5):
        assert not correr(control.empujon_cursor("empujon", amplitud)).ok  # type: ignore[arg-type]


def test_empujon_aborta_con_lbutton(crear: Callable[..., ControlPC], settings_prueba: Any, correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api, settings=con_travesuras(settings_prueba))

    def pulsa_tras_dos(fake: FakeWin) -> None:
        if len(fake.movs) == 2:
            fake.teclas = {0x01: 0x8000}                                # LBUTTON

    api.al_mover = pulsa_tras_dos
    r = correr(control.empujon_cursor("empujon", 20))
    assert not r.ok and r.datos["abortada_usuario"] is True
    assert len(api.movs) == 2

    # Con el botón ya pulsado antes de empezar, ni siquiera empieza (no cuenta como aborto)
    api2 = FakeWin()
    api2.teclas = {0x01: 0x8000}
    control2 = crear("win", api=api2, settings=con_travesuras(settings_prueba))
    r2 = correr(control2.empujon_cursor("empujon", 20))
    assert not r2.ok and r2.datos["abortada_usuario"] is False and api2.movs == []


def test_empujon_aborta_si_el_usuario_mueve_el_raton(crear: Callable[..., ControlPC], settings_prueba: Any,
                                                    correr: Any) -> None:
    api = FakeWin()
    reloj = RelojFalso()
    control = crear("win", api=api, reloj=reloj, settings=con_travesuras(settings_prueba))

    def usuario_mueve(_reloj: RelojFalso) -> None:                      # durante la pausa tras el 3.er paso
        if len(api.movs) == 3:
            api.pos = (api.pos[0] + 7, api.pos[1])                      # > 6 px

    reloj.al_dormir = usuario_mueve
    r = correr(control.empujon_cursor("temblor", 20))
    assert not r.ok and r.datos["abortada_usuario"] is True and len(api.movs) == 3
    assert "recolocado" not in r.datos

    api2 = FakeWin()
    reloj2 = RelojFalso()
    control2 = crear("win", api=api2, reloj=reloj2, settings=con_travesuras(settings_prueba))

    def tiembla_poco(_reloj: RelojFalso) -> None:
        api2.pos = (api2.pos[0] + 3, api2.pos[1])                       # ≤ 6 px: no es el usuario

    reloj2.al_dormir = tiembla_poco
    assert correr(control2.empujon_cursor("empujon", 20)).ok


class DosMonitores(FakeWin):
    """Primario 1920x1080 en (0,0) y secundario 1280x1024 en (1920,0): la caja virtual (3200x1080)
    tiene un hueco bajo el secundario. Como Windows, ``SetCursorPos`` fuera de todo monitor deja
    el cursor en el borde del monitor más cercano. Sin ``monitor_de`` (solo la caja virtual)."""

    MONITORES = ((0, 0, 1920, 1080), (1920, 0, 1280, 1024))

    def __init__(self) -> None:
        super().__init__()
        self.virtual = (0, 0, 3200, 1080)

    def _monitor(self, x: int, y: int) -> tuple[int, int, int, int] | None:
        return next((m for m in self.MONITORES if m[0] <= x < m[0] + m[2] and m[1] <= y < m[1] + m[3]), None)

    def mover_cursor(self, x: int, y: int) -> bool:
        if self._monitor(x, y) is None:
            m = self.MONITORES[1]
            x, y = min(max(x, m[0]), m[0] + m[2] - 1), min(max(y, m[1]), m[1] + m[3] - 1)
        return super().mover_cursor(x, y)


class DosMonitoresConMonitorDe(DosMonitores):
    """Como ``_ApiWin``: sabe el rectángulo del monitor que contiene un punto."""

    def monitor_de(self, x: int, y: int) -> tuple[int, int, int, int] | None:
        return self._monitor(x, y)


class AzarHaciaAbajo:
    def uniform(self, a: float, b: float) -> float:
        return math.pi / 2                                              # ángulo del empujón: hacia abajo


def test_empujon_no_culpa_al_usuario_si_windows_recoloca_el_cursor(crear: Callable[..., ControlPC],
                                                                   settings_prueba: Any, correr: Any) -> None:
    # Sin MonitorFromPoint (solo la caja virtual): Windows recoloca el cursor en el hueco → para,
    # pero NO es un aborto del usuario (main no apaga las travesuras 2 h).
    api = DosMonitores()
    api.pos = (2500, 1015)
    control = crear("win", api=api, settings=con_travesuras(settings_prueba))
    control._azar = AzarHaciaAbajo()  # type: ignore[assignment]
    r = correr(control.empujon_cursor("empujon", 40, segundos=0.5))
    assert not r.ok and r.datos["abortada_usuario"] is False and r.datos["recolocado"] is True
    assert "moviste" not in r.mensaje
    # Con el monitor del cursor, el recorrido no sale de él y la travesura termina bien
    api2 = DosMonitoresConMonitorDe()
    api2.pos = (2500, 1015)
    control2 = crear("win", api=api2, settings=con_travesuras(settings_prueba))
    control2._azar = AzarHaciaAbajo()  # type: ignore[assignment]
    r2 = correr(control2.empujon_cursor("empujon", 40, segundos=0.5))
    assert r2.ok, r2.mensaje
    assert all(1920 <= x < 3200 and 0 <= y < 1024 for x, y in api2.movs)
    assert api2.movs[-1] == (2500, 1023)                                # se quedó en el borde del monitor


def test_empujon_dura_como_mucho_600_ms_con_el_loop_real(crear: Callable[..., ControlPC], settings_prueba: Any,
                                                        correr: Any) -> None:
    """Esperas reales de asyncio (en Windows se alargan hasta ~15,6 ms cada una): el plan por
    plazo mantiene el total ≤ 600 ms y el último paso (volver al inicio) nunca se salta."""
    api = FakeWin()
    reloj = RelojFalso()                                                # tasas e inactividad
    control = crear("win", api=api, reloj=reloj, settings=con_travesuras(settings_prueba))
    control._dormir = asyncio.sleep
    control._crono = time.perf_counter
    inicio = api.pos

    async def medir() -> tuple[ResultadoControl, float]:
        t0 = time.perf_counter()
        resultado = await control.empujon_cursor("circulo", 20, segundos=0.6)
        return resultado, time.perf_counter() - t0

    duraciones = []
    for _ in range(3):                                                  # el mejor de 3 (PC cargado)
        reloj.t += 901
        api.movs.clear()
        r, segundos = correr(medir())
        assert r.ok, r.mensaje
        assert api.movs[-1] == inicio and r.datos["pasos"] == len(api.movs) >= 4
        duraciones.append(segundos)
    assert 0.5 <= min(duraciones) <= 0.62, duraciones


def test_tasas_de_travesura(crear: Callable[..., ControlPC], settings_prueba: Any, correr: Any) -> None:
    api = FakeWin()
    reloj = RelojFalso()
    control = crear("win", api=api, reloj=reloj, settings=con_travesuras(settings_prueba))
    assert correr(control.empujon_cursor("temblor", 10)).ok
    # Nunca dos seguidas sin entrada del usuario: aunque pasen 15 min, si la última entrada fue antes
    reloj.t += 901
    api.idle = 901 + 30.0                                              # última entrada: ANTES de la travesura
    assert _puede(control, correr) == (False, "no has tocado el PC desde la última")
    api.idle = 5.0                                                     # el usuario tocó el PC hace 5 s
    assert _puede(control, correr) == (True, "ok")
    # ≤ 1 cada 15 min
    assert correr(control.empujon_cursor("temblor", 10)).ok
    reloj.t += 60
    api.idle = 5.0
    assert _puede(control, correr) == (False, "hace menos de 15 minutos de la última")
    # ≤ 6 en 24 h
    for _ in range(4):
        reloj.t += 901
        assert correr(control.empujon_cursor("temblor", 10)).ok
    reloj.t += 901
    assert _puede(control, correr) == (False, "ya hice demasiadas hoy")
    reloj.t += 86_400
    assert _puede(control, correr) == (True, "ok")


def test_hacia_avatar_sin_caja_no_mueve(crear: Callable[..., ControlPC], settings_prueba: Any,
                                       correr: Any) -> None:
    api = FakeWin()
    control = crear("win", api=api, settings=con_travesuras(settings_prueba))
    r = correr(control.empujon_cursor("hacia_avatar", 20))
    assert not r.ok and api.movs == [] and r.datos["abortada_usuario"] is False
    control.set_caja_avatar((900, 300, 340, 600))
    assert control._caja == (900, 300, 340, 600)
    control.set_caja_avatar(None)
    assert control._caja is None


def test_empujon_no_sale_del_escritorio_virtual(crear: Callable[..., ControlPC], settings_prueba: Any,
                                               correr: Any) -> None:
    api = FakeWin()
    api.pos = (0, 0)
    control = crear("win", api=api, settings=con_travesuras(settings_prueba))
    assert correr(control.empujon_cursor("circulo", 30)).ok
    assert all(0 <= x < 1920 and 0 <= y < 1080 for x, y in api.movs)


# ---------------------------------------------------------------------------
# Win32 real (solo Windows): los efectos pasan por ctypes.windll (red de seguridad)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="solo Windows")
def test_api_win_real_resuelve_efectos_en_ctypes_windll(monkeypatch: pytest.MonkeyPatch) -> None:
    api = cpc_mod._ApiWin()
    # La fixture sin_efectos_pc ya puso sustitutos que fallan: el módulo DEBE pasar por ahí.
    assert getattr(ctypes.windll.user32.SetCursorPos, "_rams_sin_efectos", False)  # type: ignore[attr-defined]
    assert getattr(ctypes.windll.user32.SendInput, "_rams_sin_efectos", False)  # type: ignore[attr-defined]
    cursor: list[tuple[int, int]] = []
    monkeypatch.setattr(ctypes.windll.user32, "SetCursorPos",  # type: ignore[attr-defined]
                        lambda x, y: cursor.append((x, y)) or 1)
    assert api.mover_cursor(10, 20) is True and cursor == [(10, 20)]
    enviados: list[tuple[int, list[tuple[int, int, int, int]], int]] = []

    def send_input(n: int, eventos: Any, tamano: int) -> int:
        enviados.append((n, [(e.type, e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in eventos], tamano))
        return n

    monkeypatch.setattr(ctypes.windll.user32, "SendInput", send_input)  # type: ignore[attr-defined]
    assert api.teclear([0x61, 0x0D, 0x0A, 0x09, 0xD83D, 0xDE00]) == 6     # los controles se descartan
    n, eventos, tamano = enviados[0]
    assert n == 6 and tamano == ctypes.sizeof(cpc_mod._INPUT) == (40 if sys.maxsize > 2 ** 32 else 28)
    assert eventos == [(1, 0, 0x61, 0x4), (1, 0, 0x61, 0x6), (1, 0, 0xD83D, 0x4), (1, 0, 0xD83D, 0x6),
                       (1, 0, 0xDE00, 0x4), (1, 0, 0xDE00, 0x6)]
    # Lecturas reales (solo consultas, sin efectos): no lanzan
    hwnd = api.primer_plano()
    assert hwnd is None or isinstance(hwnd, int)
    assert api.quns() is None or isinstance(api.quns(), int)
    assert api.inactividad_s() is None or api.inactividad_s() >= 0  # type: ignore[operator]
    assert api.elevado(os.getpid()) in (True, False, None)
    assert api.estado_tecla(0x01) in range(0, 0x10000)
    # MonitorFromPoint + GetMonitorInfoW (solo lectura): el monitor del cursor, y nada fuera de todos
    punto = api.cursor()
    if punto is not None:
        monitor = api.monitor_de(*punto)
        assert monitor is None or (len(monitor) == 4 and monitor[2] > 0 and monitor[3] > 0
                                   and monitor[0] <= punto[0] < monitor[0] + monitor[2]
                                   and monitor[1] <= punto[1] < monitor[1] + monitor[3])
    assert api.monitor_de(-10_000_000, -10_000_000) is None


@pytest.mark.skipif(sys.platform != "win32", reason="solo Windows")
def test_api_win_tecla_media_solo_teclas_multimedia(monkeypatch: pytest.MonkeyPatch) -> None:
    api = cpc_mod._ApiWin()
    enviados: list[list[tuple[int, int, int, int]]] = []

    def send_input(n: int, eventos: Any, tamano: int) -> int:
        enviados.append([(e.type, e.ki.wVk, e.ki.wScan, e.ki.dwFlags) for e in eventos])
        return n

    monkeypatch.setattr(ctypes.windll.user32, "SendInput", send_input)  # type: ignore[attr-defined]
    assert api.tecla_media(0xB3) == 2
    assert enviados == [[(1, 0xB3, 0, 0x1), (1, 0xB3, 0, 0x3)]]
    for vk in (0x0D, 0x41, 0x5B, 0xAF, -1):          # Enter, letras, Win, volumen: nunca
        assert api.tecla_media(vk) == 0
    assert len(enviados) == 1


# ---------------------------------------------------------------------------
# v0.2.1: apps del menú Inicio, teclas multimedia y título de la ventana activa
# ---------------------------------------------------------------------------


ACCESOS: dict[str, bytes] = {
    "Steam/Steam.lnk": b"lnk",
    "Steam/Steam Support Center.url": b"[InternetShortcut]\nURL=https://help.steampowered.com/\n",
    "Steam/Portal 2.url": b"[InternetShortcut]\r\nURL=steam://rungameid/620\r\n",
    "Google Chrome.lnk": b"lnk",
    "Chrome Remote Desktop.lnk": b"lnk",
    "Visual Studio Code/Visual Studio Code.lnk": b"lnk",
    "Visual Studio Code/Uninstall Visual Studio Code.lnk": b"lnk",
    "Foo/Foo Editor.lnk": b"lnk",
    "Foo/Foo Viewer.lnk": b"lnk",
    "Foo/Foo Manual.lnk": b"lnk",
    "Foo/Foo Website.url": b"[InternetShortcut]\nURL=https://foo.example/\n",
    "Foo/Foo Setup.lnk": b"lnk",
    "Windows PowerShell/Windows PowerShell.lnk": b"lnk",
    "Python 3.12/Python 3.12 (64-bit).lnk": b"lnk",
    "Notepad++.lnk": b"lnk",
    "léeme.txt": b"no es un acceso",
    "desktop.ini": b"[.ShellClassInfo]",
}


def _menu_inicio(tmp_path: Path) -> tuple[Path, Path]:
    menu = tmp_path / "Start Menu" / "Programs"
    for relativa, contenido in ACCESOS.items():
        ruta = menu / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
    paquetes = tmp_path / "Packages"
    (paquetes / "Microsoft.WindowsCalculator_8wekyb3d8bbwe").mkdir(parents=True)
    return menu, paquetes


class FakeWinMedia(FakeWin):
    def __init__(self, insertados: int = 2) -> None:
        super().__init__()
        self.teclas_media: list[int] = []
        self.insertados = insertados

    def tecla_media(self, vk: int) -> int:
        self.teclas_media.append(vk)
        return self.insertados


def test_normalizar_y_clasificar_nombres_de_apps() -> None:
    assert cpc_mod.normalizar_nombre_app("Python 3.12 (64-bit)") == "python"
    assert cpc_mod.normalizar_nombre_app("Microsoft® Word™ 2021") == "microsoft word 2021"
    assert cpc_mod.normalizar_nombre_app("  Configuración  de   Windows ") == "configuracion de windows"
    assert cpc_mod.normalizar_nombre_app("Notepad++") == "notepad"
    assert cpc_mod.normalizar_nombre_app(None) == ""
    assert cpc_mod.clasificar_nombre_app("uninstall steam") == ("excluida", "es un desinstalador")
    assert cpc_mod.clasificar_nombre_app("desinstalar foo")[0] == "excluida"
    assert cpc_mod.clasificar_nombre_app("foo setup") == ("excluida", "es un instalador")
    assert cpc_mod.clasificar_nombre_app("steam support center") == ("excluida", "es un enlace a una página web")
    assert cpc_mod.clasificar_nombre_app("foo manual")[0] == "excluida"
    assert cpc_mod.clasificar_nombre_app("readme")[0] == "excluida"
    assert cpc_mod.clasificar_nombre_app("windows powershell")[0] == "sensible"
    assert cpc_mod.clasificar_nombre_app("simbolo del sistema")[0] == "sensible"
    assert cpc_mod.clasificar_nombre_app("steam") == ("normal", "")


def test_indice_del_menu_inicio_resuelve_por_nombre(crear: Callable[..., ControlPC], tmp_path: Path,
                                                    correr: Any) -> None:
    menu, paquetes = _menu_inicio(tmp_path)
    control = crear("win", api=FakeWin(), menu=menu, paquetes=paquetes)

    async def escenario() -> dict[str, Any]:
        return {consulta: await control.resolver_app_local(consulta) for consulta in (
            "steam", "Steam", "chrome", "visual", "vs code", "calc", "calculadora", "portal 2", "notepad++",
            "uninstall visual studio code", "foo website", "foo", "powershell", "stem", "abc xyz", "léeme",
            "foo manual", "")}

    r = correr(escenario())
    assert r["steam"].estado == "unica" and r["steam"].app.nombre == "Steam" and r["steam"].nivel == "descubierta"
    assert r["Steam"].app == r["steam"].app
    assert r["chrome"].app.nombre == "Google Chrome"          # gana la que menos palabras le sobran
    assert r["visual"].app.nombre == r["vs code"].app.nombre == "Visual Studio Code"
    assert r["calc"].app.nombre == r["calculadora"].app.nombre == "Calculadora"
    assert r["calc"].app.objetivo == "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert r["portal 2"].app.nombre == "Portal 2"             # .url de Steam (no es una web)
    assert r["notepad++"].app.nombre == "Notepad++"
    assert r["uninstall visual studio code"].estado == "excluida"
    assert r["uninstall visual studio code"].motivo == "es un desinstalador"
    assert r["foo website"].estado == "excluida" and "web" in r["foo website"].motivo
    assert r["foo manual"].estado == "excluida"
    assert r["foo"].estado == "varias" and set(r["foo"].opciones) == {"Foo Editor", "Foo Viewer"}
    assert r["powershell"].estado == "unica" and r["powershell"].nivel == "sensible"
    assert r["stem"].estado == "ninguna" and r["stem"].opciones == ("Steam",) and r["stem"].app is None
    assert r["abc xyz"].estado == "ninguna" and r["abc xyz"].opciones == ()
    assert r["léeme"].estado == "ninguna" and r[""].estado == "ninguna"
    nombres = {a.nombre for a in control._apps}
    assert "Foo Setup" not in nombres and "Steam Support Center" not in nombres and "desktop" not in nombres
    assert all(cpc_mod.SAFE_CLAVE_APP_RE.fullmatch(a.clave) for a in control._apps)
    assert control.nivel_app_local("steam") == ("descubierta", "Steam")
    assert control.nivel_app_local("windows_powershell") == ("sensible", "Windows PowerShell")
    assert control.nivel_app_local("no_existe") is None and control.nivel_app_local(None) is None  # type: ignore[arg-type]


def test_desinstalador_entre_parentesis_no_ocupa_el_sitio_de_la_app(crear: Callable[..., ControlPC], tmp_path: Path,
                                                                    correr: Any) -> None:
    """"Steam (Uninstall).lnk" va antes que "Steam.lnk" (``sorted``) y se normaliza igual."""
    menu = tmp_path / "Start Menu" / "Programs"
    for relativa in ("Steam/Steam (Uninstall).lnk", "Steam/Steam.lnk", "Zoom/Zoom (Uninstall).lnk",
                     "Discord/Discord (desinstalar).lnk", "Foo/Foo (beta).lnk", "Foo/Foo.lnk",
                     "Anaconda3 (64-bit)/Anaconda Prompt (anaconda3).lnk", "Anaconda3 (64-bit)/Spyder (anaconda3).lnk",
                     "Windows Subsystem for Linux.lnk", "System Tools/Run.lnk", "Temple Run.lnk"):
        ruta = menu / relativa
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(b"lnk")
    control = crear("win", api=FakeWin(), menu=menu, paquetes=None)

    async def escenario() -> dict[str, Any]:
        resueltas = {consulta: await control.resolver_app_local(consulta) for consulta in (
            "steam", "zoom", "discord", "foo", "anaconda prompt", "spyder", "windows subsystem for linux", "run",
            "temple run")}
        resueltas["recordar_prompt"] = await control.aprobar_app_local(resueltas["anaconda prompt"].app.clave)
        return resueltas

    r = correr(escenario())
    assert r["steam"].estado == "unica" and r["steam"].app.nombre == "Steam"
    assert r["steam"].app.objetivo.endswith("Steam.lnk")
    assert r["zoom"].estado == "excluida" and r["zoom"].motivo == "es un desinstalador"
    assert r["discord"].estado == "excluida" and r["discord"].motivo == "es un desinstalador"
    assert r["foo"].app.nombre == "Foo"                    # gana el nombre sin paréntesis
    nombres = {a.nombre for a in control._apps}
    assert not any("Uninstall" in n or "desinstalar" in n for n in nombres)
    assert r["anaconda prompt"].nivel == "sensible" and r["recordar_prompt"] is False
    assert r["windows subsystem for linux"].nivel == "sensible" and r["run"].nivel == "sensible"
    assert r["spyder"].nivel == "descubierta" and r["temple run"].nivel == "descubierta"
    assert cpc_mod.clasificar_nombre_app("anaconda prompt")[0] == "sensible"
    assert cpc_mod.clasificar_nombre_app("ejecutar")[0] == "sensible"
    assert cpc_mod.clasificar_nombre_app("temple run") == ("normal", "")


def test_alias_de_rams_apps_y_aprobaciones_persistentes(crear: Callable[..., ControlPC], tmp_path: Path,
                                                        settings_prueba: Any, correr: Any) -> None:
    menu, paquetes = _menu_inicio(tmp_path)
    ajustes = settings_prueba.con(apps_permitidas_items=(("juegos", "steam"),))
    control = crear("win", api=FakeWin(), menu=menu, paquetes=paquetes, settings=ajustes)

    async def escenario() -> tuple[Any, ...]:
        por_alias = await control.resolver_app_local("juegos")
        chrome = await control.resolver_app_local("chrome")
        aprobada = await control.aprobar_app_local(chrome.app.clave)
        consola = await control.aprobar_app_local("windows_powershell")
        desconocida = await control.aprobar_app_local("no_existe")
        otro = crear("win", api=FakeWin(), menu=menu, paquetes=paquetes, settings=ajustes)
        await otro.resolver_app_local("steam")
        return por_alias, chrome, aprobada, consola, desconocida, otro

    por_alias, chrome, aprobada, consola, desconocida, otro = correr(escenario())
    assert por_alias.app.nombre == "Steam" and por_alias.nivel == "permitida"
    assert control.nivel_app_local("steam") == ("permitida", "Steam")
    assert aprobada is True and control.nivel_app_local(chrome.app.clave)[0] == "permitida"
    assert consola is False and desconocida is False      # las sensibles nunca se recuerdan
    assert control.nivel_app_local("windows_powershell")[0] == "sensible"
    datos = json.loads((Path(ajustes.data_dir) / "apps_aprobadas.json").read_text(encoding="utf-8"))
    assert [e["nombre"] for e in datos["aprobadas"]] == ["Google Chrome"]
    assert datos["aprobadas"][0]["objetivo"] == chrome.app.objetivo
    assert otro.nivel_app_local(chrome.app.clave)[0] == "permitida"      # persiste entre arranques


def test_lanzar_app_local_abre_el_acceso_con_startfile(crear: Callable[..., ControlPC], tmp_path: Path,
                                                       abiertos: list[str], correr: Any) -> None:
    menu, paquetes = _menu_inicio(tmp_path)
    control = crear("win", api=FakeWin(), menu=menu, paquetes=paquetes)

    async def escenario() -> list[ResultadoControl]:
        resultados = [await control.lanzar_app_local("steam"), await control.lanzar_app_local("calculadora"),
                      await control.lanzar_app_local("no_existe"), await control.lanzar_app_local("foo")]
        (menu / "Google Chrome.lnk").unlink()
        resultados.append(await control.lanzar_app_local("google_chrome"))
        return resultados

    steam, calc, nada, varias, borrada = correr(escenario())
    assert steam.ok and steam.mensaje == "Abrí Steam." and steam.datos["app"] == "steam"
    assert re.fullmatch(r"[0-9a-f]{12}", steam.datos["sha256_12"])
    assert calc.ok
    assert nada.ok is False and varias.ok is False
    assert borrada.ok is False and "ya no está" in borrada.mensaje
    assert abiertos == [str(menu / "Steam" / "Steam.lnk"),
                        "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"]


def test_lanzar_app_local_comparte_cupo_y_no_sale_del_menu(crear: Callable[..., ControlPC], tmp_path: Path,
                                                           abiertos: list[str], correr: Any) -> None:
    menu, paquetes = _menu_inicio(tmp_path)
    control = crear("win", api=FakeWin(), menu=menu, paquetes=paquetes)

    async def escenario() -> tuple[list[bool], ResultadoControl]:
        oks = [(await control.abrir_destino("youtube")).ok for _ in range(5)]
        oks.append((await control.lanzar_app_local("steam")).ok)
        septima = await control.lanzar_app_local("steam")
        return oks, septima

    oks, septima = correr(escenario())
    assert oks == [True] * 6 and septima.ok is False and "6 cosas" in septima.mensaje
    fuera = tmp_path / "fuera.lnk"
    fuera.write_bytes(b"lnk")
    assert cpc_mod._dentro_de(str(fuera), [menu]) is False
    assert cpc_mod._dentro_de(str(menu / "Steam" / ".." / ".." / ".." / "fuera.lnk"), [menu]) is False
    assert cpc_mod._dentro_de(str(menu / "Steam" / "Steam.lnk"), [menu]) is True
    assert cpc_mod._dentro_de("Steam.lnk", [menu]) is False
    assert cpc_mod._dentro_de(str(menu / "léeme.txt"), [menu]) is False


def test_apps_locales_solo_en_windows_y_con_el_ajuste(crear: Callable[..., ControlPC], tmp_path: Path,
                                                      settings_prueba: Any, abiertos: list[str],
                                                      correr: Any) -> None:
    menu, paquetes = _menu_inicio(tmp_path)
    linux = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX, menu=menu, paquetes=paquetes)
    apagado = crear("win", api=FakeWin(), menu=menu, paquetes=paquetes,
                    settings=settings_prueba.con(apps_menu_inicio=False))

    async def escenario() -> tuple[Any, ...]:
        return (await linux.resolver_app_local("steam"), await apagado.resolver_app_local("steam"),
                await apagado.lanzar_app_local("steam"))

    en_linux, desactivada, lanzada = correr(escenario())
    assert en_linux.estado == "desactivada" and en_linux.motivo == "solo en Windows"
    assert desactivada.estado == "desactivada" and "RAMS_APPS_MENU_INICIO" in desactivada.motivo
    assert lanzada.ok is False and abiertos == []
    assert linux.apps_locales_disponibles is False and apagado.apps_locales_disponibles is False


def test_media_con_teclas_multimedia(crear: Callable[..., ControlPC], correr: Any) -> None:
    api = FakeWinMedia()
    control = crear("win", api=api)

    async def escenario() -> list[ResultadoControl]:
        return [await control.media(orden) for orden in ("play_pause", "siguiente", "anterior", "subir_volumen")]

    pausa, siguiente, anterior, invalida = correr(escenario())
    assert pausa.ok and siguiente.ok and anterior.ok and invalida.ok is False
    assert api.teclas_media == [0xB3, 0xB0, 0xB1]
    assert control.ts_ultima_inyeccion() is not None
    rechazo = crear("win", api=FakeWinMedia(insertados=0))
    assert correr(rechazo.media("play_pause")).ok is False
    linux = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)
    assert correr(linux.media("play_pause")).ok is False


def test_media_tiene_cupo_por_minuto(crear: Callable[..., ControlPC], correr: Any) -> None:
    reloj = RelojFalso()
    api = FakeWinMedia()
    control = crear("win", api=api, reloj=reloj)

    async def escenario() -> list[bool]:
        return [(await control.media("siguiente")).ok for _ in range(31)]

    resultados = correr(escenario())
    assert resultados == [True] * 30 + [False] and len(api.teclas_media) == 30
    reloj.t += 61.0
    assert correr(control.media("siguiente")).ok is True


def test_titulo_de_la_ventana_activa(crear: Callable[..., ControlPC]) -> None:
    api = FakeWin()
    control = crear("win", api=api)
    assert control.titulo_activa() == "WhatsApp - Google Chrome"
    api.fg = None
    assert control.titulo_activa() is None
    hypr = crear("hypr", hypr=FakeHypr(), programas=PROGRAMAS_LINUX)
    hypr._hypr_activa = {"title": "Bancolombia — Firefox"}
    assert hypr.titulo_activa() == "Bancolombia — Firefox"
    assert crear("win", api=None).titulo_activa() is None
