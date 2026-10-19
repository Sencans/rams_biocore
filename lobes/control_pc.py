"""Control del PC para Amadeus (v0.2, ``docs/COMPANION.md`` §C1.8, §C1.11, §C2.8 y §C2.12).

Efector **sin decisiones propias**. Abre apps y webs del catálogo cerrado ``DESTINOS_BASE``,
prerrellena borradores con enlaces profundos (nunca envía), teclea texto Unicode en la ventana
que eligió el usuario (sin forzar el foco y **sin pulsar Enter jamás**) y hace travesuras
acotadas con el cursor real (opt-in, OFF por defecto). Quién decide es main (texto del usuario o
reglas deterministas de iniciativa) y ``core/permisos.py`` autoriza **antes** de llamar aquí;
este módulo vuelve a validar (defensa en profundidad) y aplica las tasas de §C2.12.

Plataformas (``plataforma`` inyectable: ``"win"`` | ``"hypr"`` | ``"otro"``):

- **Windows** (``"win"``): solo biblioteca estándar. ``ctypes`` para la ventana en primer plano
  (sondeo asyncio a 2 Hz: ``GetForegroundWindow``/``GetWindowThreadProcessId``/
  ``QueryFullProcessImageNameW``), el cursor (``GetCursorPos``/``SetCursorPos``), el teclado
  (``SendInput`` + ``KEYEVENTF_UNICODE``), la inactividad (``GetLastInputInfo``) y el "no
  molestar" (``SHQueryUserNotificationState``). ``os.startfile`` (ShellExecute, en un hilo) abre
  URLs, protocolos y apps de la Store (``shell:AppsFolder\\<AUMID>``).
- **Hyprland** (``"hypr"``): caché de ``j/activewindow`` y ``j/cursorpos`` refrescada por una
  tarea asyncio con ``asyncio.open_unix_connection`` al ``.socket.sock`` (timeout 250 ms, ≤ 2 Hz);
  ``wtype`` con el texto por **stdin**; ``xdg-open``/``gtk-launch`` con ``create_subprocess_exec``.
  Wayland no deja leer los botones del ratón: **no hay travesuras con el cursor real**.
- **Otro** (``"otro"``): solo abrir enlaces con ``xdg-open`` si existe.

v0.2.1 (Windows): **apps del menú Inicio** por nombre ("abre steam"). El índice se arma (en un
hilo, sin PowerShell) con los accesos directos ``.lnk``/``.url`` de ``%APPDATA%`` y
``%ProgramData%\\Microsoft\\Windows\\Start Menu\\Programs`` (más unas pocas apps de la Store
verificadas por su carpeta de paquete), excluye desinstaladores, ayudas, manuales, licencias y
sitios web, y marca como *sensibles* las consolas y herramientas del sistema. La coincidencia
es por nombre normalizado (más los alias de ``RAMS_APPS``); se abre con ``os.startfile`` del
acceso directo (que debe seguir dentro del menú Inicio). Las apps aprobadas por el usuario se
recuerdan en ``data/apps_aprobadas.json`` (por ruta del acceso; nunca las sensibles). También:
teclas multimedia de Windows (``media``) y el título de la ventana activa (para no capturar
ventanas privadas; nunca se registra).

Reglas: nunca un shell intermedio (solo ``create_subprocess_exec`` con argumentos sueltos) ni
``subprocess.run`` en el loop; nunca Enter (``\\r``, ``\\n`` y
``\\t`` se sanean y además se filtran las unidades de control antes de ``SendInput``);
anti-contraseñas con ``config.parece_secreto`` y ``config.titulo_sensible``; el texto tecleado y
los títulos nunca se registran (solo longitud y ``sha256[:12]``). Ningún método público lanza
(salvo la cancelación de asyncio). Importable sin ``ctypes``, ``psutil`` ni ``hyprctl``
(``disponible`` queda en ``False``).

Notas de integración (grupo ``orquestacion``):
- main llama a :func:`activar_dpi_por_monitor` **antes** de construir ``ControlPC`` y
  ``VisionContinua`` (las coordenadas son píxeles físicos, como ``escritorio.caja``).
- ``ts_ultima_inyeccion()`` está en las unidades de ``reloj`` (``time.monotonic`` por defecto):
  ``SendInput`` reinicia ``GetLastInputInfo`` y la presencia debe ignorar ese reinicio (±1 s).
- ``SetCursorPos`` no reinicia la inactividad: las travesuras no engañan a la presencia.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import ntpath
import os
import random
import re
import shutil
import sys
import tempfile
import time
import types
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from difflib import get_close_matches
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit

try:  # ctypes está en casi cualquier CPython; solo se usa de verdad en Windows
    import ctypes
    import ctypes.wintypes as _wt
except (ImportError, OSError, ValueError):  # pragma: no cover - Python sin ctypes
    ctypes = None  # type: ignore[assignment]
    _wt = None  # type: ignore[assignment]

try:  # opcional: descendientes de Amadeus Desktop (renderer, GPU…)
    import psutil
except ImportError:  # pragma: no cover - depende del entorno
    psutil = None  # type: ignore[assignment]

from core import config as _config
from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo

try:  # contratos del grupo base (§C2.2); respaldo defensivo por si faltan
    from core.contracts import DESTINOS_BASE, PATRON_TITULO_SENSIBLE, PATRONES_TRAVESURA, SAFE_ALIAS_RE
except ImportError:  # pragma: no cover - el grupo base ya los publica
    PATRONES_TRAVESURA = ("empujon", "circulo", "temblor", "hacia_avatar")  # type: ignore[misc]
    SAFE_ALIAS_RE = re.compile(r"^[a-z0-9_]{1,24}$")  # type: ignore[misc]
    DESTINOS_BASE = types.MappingProxyType({})  # type: ignore[misc]
    PATRON_TITULO_SENSIBLE = (  # type: ignore[misc]
        r"contrasena|password|iniciar sesion|sign ?in|log ?in|verificacion|codigo|otp|2fa|token|\bpin\b|"
        r"banco|bancolombia|nequi|daviplata|\bpse\b|pago|checkout|tarjeta|wallet|binance|"
        r"incognito|inprivate|private browsing|navegacion privada|keepass|bitwarden|1password")

__all__ = [
    "PATRONES_TRAVESURA", "SAFE_ALIAS_RE", "SAFE_CLAVE_APP_RE", "TITULOS_PROPIOS_RE", "activar_dpi_por_monitor",
    "ObjetivoVentana", "ResultadoControl", "AppLocal", "ResolucionApp", "ControlPC",
    "normalizar_nombre_app", "clasificar_nombre_app",
]

log = logging.getLogger("rams.control_pc")

CLAVE: Final = "control_pc"
NOMBRE: Final = "Control del PC"

# Ventanas propias por título (chat y overlay de Electron, y el HUD en un navegador:
# "Amadeus - Google Chrome", "Amadeus — HUD - Brave").
TITULOS_PROPIOS_RE: Final = re.compile(r"^(Amadeus Chat|Amadeus Overlay|Amadeus)( [-—–] .*)?$")

# ---------------------------------------------------------------------------
# Límites y tasas (§C2.12)
# ---------------------------------------------------------------------------

MAX_TEXTO: Final = 500                 # prerrellenar / escribir
MAX_CONSULTA: Final = 120              # buscar_en
X_MAX_CARACTERES: Final = 280          # aviso (no límite) al prerrellenar en X
ANILLO_VENTANAS: Final = 8
PERIODO_SONDEO_S: Final = 0.5          # 2 Hz (Windows y Hyprland)
TIMEOUT_HYPR_S: Final = 0.25
MAX_RESPUESTA_HYPR: Final = 1 << 20    # 1 MiB
REFRESCO_DESCENDIENTES_S: Final = 15.0
ESPERA_FOCO_MAX_S: Final = 30.0
PAUSA_FOCO_S: Final = 0.25
ASENTAR_FOCO_S: Final = 0.35           # tras el clic del usuario, antes del primer trozo
TROZO_WIN: Final = 16                  # caracteres por SendInput
TROZO_WTYPE: Final = 32                # caracteres por invocación de wtype
PAUSA_TROZO_S: Final = 0.03
TIMEOUT_WTYPE_S: Final = 10.0
ESPERA_FALLO_LANZADOR_S: Final = 2.0   # xdg-open/gtk-launch: cuánto se espera un fallo temprano
VIGILANCIA_LANZADOR_S: Final = 60.0

VENTANA_MINUTO_S: Final = 60.0
VENTANA_DIA_S: Final = 86400.0         # "por día" = ventana deslizante de 24 h (reloj inyectable)
ABRIR_MAX_MINUTO: Final = 6            # abrir_destino + buscar_en
PRERRELLENAR_INTERVALO_S: Final = 20.0
PRERRELLENAR_MAX_DIA: Final = 30
ESCRIBIR_INTERVALO_S: Final = 30.0
ESCRIBIR_MAX_DIA: Final = 20
TRAVESURA_INTERVALO_S: Final = 900.0   # ≤ 1 cada 15 min
TRAVESURA_MAX_DIA: Final = 6
TRAVESURA_MAX_S: Final = 0.6
TRAVESURA_MIN_S: Final = 0.1
TRAVESURA_MAX_PASOS: Final = 24
# El último paso va a más tardar en duración − margen: las esperas de asyncio en Windows se
# alargan hasta un tic del temporizador (~15,6 ms) y así el total no pasa de la duración.
TRAVESURA_MARGEN_S: Final = 0.02
TRAVESURA_AMPLITUD_TOPE: Final = 80    # tope absoluto aunque la configuración diga otra cosa
UMBRAL_USUARIO_PX: Final = 6
# Tras SetCursorPos el cursor debe estar donde se pidió (± redondeo de DPI). Si no, lo recolocó
# Windows (fuera de todo monitor, ClipCursor): se para SIN culpar al usuario.
TOLERANCIA_RECOLOCADO_PX: Final = 2
MEDIA_MAX_MINUTO: Final = 30           # teclas multimedia (Windows)
# Apps del menú Inicio (v0.2.1)
MAX_APPS_INDICE: Final = 3000
MAX_PROFUNDIDAD_MENU: Final = 6
TTL_INDICE_APPS_S: Final = 300.0
MAX_NOMBRE_APP: Final = 80
MAX_URL_ACCESO: Final = 4096           # bytes que se leen de un .url
MAX_APROBADAS: Final = 500
ARCHIVO_APPS_APROBADAS: Final = "apps_aprobadas.json"
SAFE_CLAVE_APP_RE: Final = re.compile(r"^[a-z0-9_]{1,40}$")
INACTIVIDAD_MIN_S: Final = 4.0         # menos: el usuario está escribiendo o moviendo el ratón
INACTIVIDAD_MAX_S: Final = 600.0       # más: el usuario no está

# Win32
VK_BOTONES: Final = (0x01, 0x02, 0x04, 0x05, 0x06)
VK_MODIFICADORES: Final = (0x10, 0x11, 0x12, 0x5B, 0x5C)
VK_PACKET: Final = 0xE7
QUNS_ACEPTA_NOTIFICACIONES: Final = 5
QUNS_NO_MOLESTAR: Final[Mapping[int, str]] = types.MappingProxyType({
    2: "pantalla_completa",   # QUNS_BUSY: app a pantalla completa o ajustes de presentación
    3: "juego",               # QUNS_RUNNING_D3D_FULL_SCREEN
    4: "presentacion",        # QUNS_PRESENTATION_MODE
})
EXES_SEGURIDAD_WIN: Final = frozenset({"lockapp", "logonui", "consent", "credentialuibroker"})
# Ventanas del shell que nunca son un buen objetivo (barra de tareas, escritorio, Inicio, Alt+Tab).
CLASES_SHELL_WIN: Final = frozenset({
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow", "MultitaskingViewFrame",
    "ForegroundStaging", "Windows.UI.Core.CoreWindow",
})
EXES_SHELL_WIN: Final = frozenset({
    "searchhost", "searchapp", "startmenuexperiencehost", "shellexperiencehost", "textinputhost",
})
CLASES_ESCRITORIO_WIN: Final = frozenset({"Progman", "WorkerW"})   # el fondo cubre el monitor
# Agentes de contraseñas en Linux (polkit, pinentry, gcr-prompter, kwalletd…).
_RE_SEGURIDAD_LINUX: Final = re.compile(
    r"polkit|pinentry|gcr-prompter|kwalletd|ksshaskpass|ssh-askpass|keyring|seahorse", re.IGNORECASE)

_CONSULTAS_HYPR: Final = frozenset({"activewindow", "cursorpos", "monitors"})

# Controles (Cc) y separadores de línea/párrafo: se cambian por un espacio (un "\n" pulsaría Enter).
_RE_CONTROLES: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# Invisibles que se BORRAN (no se ven en la vista previa, así que tampoco se escriben): toda la
# categoría Cf (marcas bidi, ZWSP, guion blando, etiquetas U+E0000–E007F del "ASCII smuggling"…),
# sustitutos sueltos (Cs) y estos rellenos o selectores que no son Cf: CGJ, rellenos Hangul,
# vocales inherentes jemer, selectores mongoles, VS1–VS14 y VS17–VS256.
_INVISIBLES_NO_CF: Final = frozenset({0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0})
_RANGOS_INVISIBLES: Final = ((0x180B, 0x180F), (0xFE00, 0xFE0D), (0xE0100, 0xE01EF))
_ZWNJ: Final = "\u200c"
_ZWJ: Final = "\u200d"
_UNIONES: Final = frozenset({_ZWNJ, _ZWJ})
_SELECTORES_EMOJI: Final = frozenset({"\ufe0e", "\ufe0f"})   # presentación de texto / emoji
_BASES_SELECTOR_PO: Final = frozenset("#*\u203c\u2049\u3030\u303d")  # emojis de categoría P*
# Las únicas secuencias de etiquetas visibles: banderas de Inglaterra, Escocia y Gales (RGI).
_RE_BANDERA_ETIQUETAS: Final = re.compile("\U0001F3F4[\U000E0020-\U000E007E]+\U000E007F")
_BANDERAS_SUBDIVISION: Final = frozenset(
    "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in codigo) + "\U000E007F"
    for codigo in ("gbeng", "gbsct", "gbwls"))
_RE_DIRECCION_HYPR: Final = re.compile(r"^0x[0-9a-f]{1,16}$")
_RE_ID_DESKTOP: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")
_RE_AUMID: Final = re.compile(r"^[A-Za-z0-9._{}\-]{1,200}(![A-Za-z0-9._\-]{1,100})?$")
_RE_OTP_RESPALDO: Final = re.compile(r"^\s*\d{4,8}\s*$")
_RE_ASIGNA_RESPALDO: Final = re.compile(r"(?:contrasena|clave|password|\bpin|codigo)\s*[:=]\s*\S+")
_RE_DIGITOS_RESPALDO: Final = re.compile(r"[0-9](?:[ \-]?[0-9]){12,}")
_RE_TITULO_RESPALDO: Final = re.compile(PATRON_TITULO_SENSIBLE, re.IGNORECASE)

# ---------------------------------------------------------------------------
# Catálogo de destinos (cómo se ABRE cada clave de DESTINOS_BASE en cada sistema)
# ---------------------------------------------------------------------------

_NOMBRES: Final[Mapping[str, str]] = types.MappingProxyType({
    "whatsapp": "WhatsApp", "telegram": "Telegram", "x": "X", "gmail": "Gmail", "correo": "el correo",
    "instagram": "Instagram", "tiktok": "TikTok", "facebook": "Facebook", "discord": "Discord",
    "linkedin": "LinkedIn", "youtube": "YouTube", "spotify": "Spotify", "navegador": "el navegador",
    "archivos": "el explorador de archivos",
})
_WEB: Final[Mapping[str, str]] = types.MappingProxyType({
    "whatsapp": "https://web.whatsapp.com/",
    "telegram": "https://web.telegram.org/",
    "x": "https://x.com/home",
    "gmail": "https://mail.google.com/",
    "correo": "mailto:",
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/",
    "facebook": "https://www.facebook.com/",
    "discord": "https://discord.com/app",
    "linkedin": "https://www.linkedin.com/",
    "youtube": "https://www.youtube.com/",
    "spotify": "https://open.spotify.com/",
    "navegador": "https://duckduckgo.com/",
    # "archivos": la carpeta personal (se calcula al construir el catálogo)
})
# Enlaces que dejan un BORRADOR (nunca envían). Instagram, TikTok, Facebook, Discord y LinkedIn
# no admiten prerrelleno de texto.
_PRERRELLENO: Final[Mapping[str, str]] = types.MappingProxyType({
    "whatsapp": "https://wa.me/?text={texto}",
    "telegram": "https://t.me/share/url?url=&text={texto}",
    "x": "https://x.com/intent/post?text={texto}",
    "gmail": "https://mail.google.com/mail/?view=cm&fs=1&body={texto}",
    "correo": "mailto:?body={texto}",
})
_BUSQUEDA: Final[Mapping[str, str]] = types.MappingProxyType({
    "x": "https://x.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "navegador": "https://duckduckgo.com/?q={q}",
})
_HOSTS_HTTPS: Final = frozenset({
    "wa.me", "web.whatsapp.com", "web.telegram.org", "t.me", "x.com", "mail.google.com",
    "www.instagram.com", "www.tiktok.com", "www.facebook.com", "discord.com", "www.linkedin.com",
    "www.youtube.com", "open.spotify.com", "duckduckgo.com",
})
_ESQUEMAS_APP: Final = frozenset({"mailto", "spotify", "tg", "discord", "whatsapp"})

# Windows: apps de escritorio verificadas en el PC del usuario (investigación 2026-10-17).
_PFN_WHATSAPP: Final = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm"
_AUMID_WHATSAPP: Final = _PFN_WHATSAPP + "!App"
_AUMID_DISCORD: Final = "com.squirrel.Discord.Discord"
# Linux: ids .desktop candidatos (gtk-launch); si ninguno existe se abre la web con xdg-open.
_DESKTOP_LINUX: Final[Mapping[str, tuple[str, ...]]] = types.MappingProxyType({
    "whatsapp": ("com.rtosta.zapzap", "zapzap", "wasistlos", "whatsapp-for-linux"),
    "telegram": ("org.telegram.desktop", "telegramdesktop", "telegram-desktop"),
    "discord": ("discord", "com.discordapp.Discord", "vesktop", "dev.vencord.Vesktop"),
    "spotify": ("spotify", "spotify-launcher", "com.spotify.Client"),
})


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _es_windows() -> bool:
    """``True`` en Windows (función aparte para poder simular otro sistema en los tests)."""
    return sys.platform == "win32"


def _es_entero(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _buscar_programa(nombre: str) -> str | None:
    """Ruta de un programa del PATH (``shutil.which``); ``None`` si no está o algo falla."""
    try:
        return shutil.which(nombre)
    except (OSError, ValueError) as exc:
        log.debug("No se pudo buscar %s en el PATH (%s)", nombre, type(exc).__name__)
        return None


def _es_invisible(caracter: str) -> bool:
    """Invisible que siempre se borra (ZWJ/ZWNJ y VS15/VS16 dependen del contexto: aparte)."""
    if caracter in _UNIONES or caracter in _SELECTORES_EMOJI:
        return False
    if unicodedata.category(caracter) in ("Cf", "Cs"):
        return True
    codigo = ord(caracter)
    return codigo in _INVISIBLES_NO_CF or any(a <= codigo <= b for a, b in _RANGOS_INVISIBLES)


def _base_emoji(caracter: str) -> bool:
    """¿Puede llevar detrás un selector de presentación o un ZWJ de emoji? (símbolos, dígitos
    de los keycaps y los pocos emojis de puntuación)."""
    if not caracter:
        return False
    return unicodedata.category(caracter) in ("So", "Sm", "Sk", "Nd") or caracter in _BASES_SELECTOR_PO


def _letra_union(caracter: str) -> bool:
    """Letras y marcas de los alfabetos que usan ZWJ/ZWNJ (árabe, persa, índicos…); no latinas."""
    return (bool(caracter) and caracter not in _SELECTORES_EMOJI
            and unicodedata.category(caracter) in ("Lo", "Mn", "Mc"))


def _quitar_invisibles(texto: str) -> str:
    """Borra todo carácter invisible que el usuario no vería en la vista previa (§C2.12: lo
    confirmado es lo que se escribe). Se conservan solo los que tienen efecto visible y un uso
    legítimo, y nunca en ráfagas (que es como se esconden datos en un texto):

    - VS15/VS16 (U+FE0E/U+FE0F): uno solo, tras un emoji o un dígito (keycaps).
    - ZWJ entre dos emojis (mujer + ZWJ + portátil) o entre dos letras de alfabetos que lo
      usan; ZWNJ solo entre letras de esos alfabetos. Dos uniones seguidas se borran las dos.
    - Etiquetas U+E0020–E007F solo dentro de las tres banderas de subdivisión RGI.
    """
    protegidos: set[int] = set()
    for coincidencia in _RE_BANDERA_ETIQUETAS.finditer(texto):
        if coincidencia.group(0) in _BANDERAS_SUBDIVISION:
            protegidos.update(range(coincidencia.start() + 1, coincidencia.end()))
    visibles = [c for i, c in enumerate(texto) if i in protegidos or not _es_invisible(c)]
    con_selectores: list[str] = []
    for caracter in visibles:
        if caracter in _SELECTORES_EMOJI and not _base_emoji(con_selectores[-1] if con_selectores else ""):
            continue
        con_selectores.append(caracter)
    salida: list[str] = []
    for i, caracter in enumerate(con_selectores):
        if caracter in _UNIONES:
            antes = con_selectores[i - 1] if i > 0 else ""
            despues = con_selectores[i + 1] if i + 1 < len(con_selectores) else ""
            if antes in _UNIONES or despues in _UNIONES:
                continue
            letras = _letra_union(antes) and _letra_union(despues)
            emoji = (caracter == _ZWJ and (_base_emoji(antes) or antes in _SELECTORES_EMOJI)
                     and _base_emoji(despues))
            if not (letras or emoji):
                continue
        salida.append(caracter)
    return "".join(salida)


def _sanear(texto: Any, maximo: int) -> str | None:
    """NFC, sin controles (``\\r``, ``\\n``, ``\\t`` → espacio), sin invisibles (marcas bidi,
    etiquetas Unicode, guion blando, rellenos Hangul…: se borran), espacios colapsados. ``None``
    si queda vacío o pasa de ``maximo`` (no se recorta: lo confirmado es lo que se escribe).
    Quitar los saltos evita que un ``\\n`` "pulse" Enter en apps de chat."""
    if not isinstance(texto, str):
        return None
    limpio = unicodedata.normalize("NFC", texto)
    limpio = _RE_CONTROLES.sub(" ", limpio)
    limpio = _quitar_invisibles(limpio)
    limpio = " ".join(limpio.split())
    if not limpio or len(limpio) > maximo:
        return None
    return limpio


def _huella(texto: str) -> str:
    """``sha256(texto)[:12]`` para auditar sin guardar el texto."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12]


def _utf16(texto: str) -> list[int]:
    """Unidades UTF-16 (fuera del BMP → par sustituto). Filtra cualquier control < 0x20 y 0x7F
    (defensa en profundidad: nunca viaja un Enter ni un tabulador)."""
    datos = texto.encode("utf-16-le")
    unidades = [int.from_bytes(datos[i:i + 2], "little") for i in range(0, len(datos), 2)]
    return [u for u in unidades if u >= 0x20 and u != 0x7F]


def _trozos(texto: str, tamano: int) -> list[str]:
    return [texto[i:i + tamano] for i in range(0, len(texto), tamano)]


def _base_exe(exe: Any) -> str:
    """``"C:\\Windows\\System32\\LockApp.exe"`` → ``"lockapp"`` (minúsculas, sin ``.exe``)."""
    nombre = ntpath.basename(str(exe or "")).strip().lower()
    return nombre[:-4] if nombre.endswith(".exe") else nombre


def _enlace_seguro(enlace: str) -> bool:
    """Lista blanca de enlaces que este módulo puede abrir (esquema y host)."""
    if not isinstance(enlace, str) or not enlace or len(enlace) > 4096:
        return False
    if any(c in enlace for c in " \t\r\n\x00"):
        return False
    try:
        partes = urlsplit(enlace)
        esquema = partes.scheme.lower()
        if esquema == "https":
            return ((partes.hostname or "") in _HOSTS_HTTPS and partes.username is None
                    and partes.password is None and partes.port is None)
        if esquema == "shell":
            prefijo = "shell:AppsFolder\\"
            return enlace.startswith(prefijo) and _RE_AUMID.match(enlace[len(prefijo):]) is not None
        return esquema in _ESQUEMAS_APP
    except ValueError:
        return False


def _carpeta_personal() -> str | None:
    try:
        return str(Path.home())
    except (RuntimeError, KeyError, OSError) as exc:
        log.debug("Sin carpeta personal (%s)", type(exc).__name__)
        return None


def _distancia(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _parece_secreto(texto: Any, secretos: Iterable[str]) -> bool:
    """``config.parece_secreto`` del grupo base (o un respaldo equivalente si aún no existe)."""
    funcion = getattr(_config, "parece_secreto", None)
    if callable(funcion):
        return bool(funcion(texto, secretos))
    return _parece_secreto_respaldo(texto, secretos)


def _titulo_sensible(titulo: Any) -> bool:
    """``config.titulo_sensible`` del grupo base (o un respaldo equivalente si aún no existe)."""
    funcion = getattr(_config, "titulo_sensible", None)
    if callable(funcion):
        return bool(funcion(titulo))
    return _RE_TITULO_RESPALDO.search(_minusculas_sin_tildes(titulo)) is not None


def _minusculas_sin_tildes(texto: Any) -> str:
    if texto is None:
        return ""
    descompuesto = unicodedata.normalize("NFKD", str(texto))
    return " ".join("".join(c for c in descompuesto if not unicodedata.combining(c)).lower().split())


def _luhn(digitos: str) -> bool:
    total = 0
    for indice, caracter in enumerate(reversed(digitos)):
        valor = ord(caracter) - 48
        if indice % 2 == 1:
            valor *= 2
            if valor > 9:
                valor -= 9
        total += valor
    return total % 10 == 0


def _parece_secreto_respaldo(texto: Any, secretos: Iterable[str]) -> bool:
    """Respaldo de las reglas "Texto" de §C2.12 (solo si ``core.config`` no las publica)."""
    try:
        cadena = "" if texto is None else str(texto)
        if not cadena.strip():
            return False
        redactar = getattr(_config, "redactar", None)
        if callable(redactar) and redactar(cadena, secretos, alta_entropia=True) != cadena:
            return True
        if _RE_OTP_RESPALDO.match(cadena):
            return True
        for racha in _RE_DIGITOS_RESPALDO.finditer(cadena):
            digitos = re.sub(r"[ \-]", "", racha.group())
            if 13 <= len(digitos) <= 19 and _luhn(digitos):
                return True
        if _RE_ASIGNA_RESPALDO.search(_minusculas_sin_tildes(cadena)):
            return True
        for palabra in cadena.split():
            if len(palabra) >= 12:
                clases = sum((any(c.islower() for c in palabra), any(c.isupper() for c in palabra),
                              any(c.isdigit() for c in palabra),
                              any(not c.isalnum() for c in palabra)))
                if clases >= 3:
                    return True
        return False
    except Exception as exc:  # ante la duda, no se escribe
        log.debug("parece_secreto (respaldo) no pudo evaluar el texto (%s)", type(exc).__name__)
        return True


def _pids_propios() -> frozenset[int]:
    """R.A.M.S. y sus ancestros (``core.permisos.calcular_pids_propios``); respaldo: el PID actual."""
    try:
        from core.permisos import calcular_pids_propios

        return frozenset(int(p) for p in calcular_pids_propios())
    except Exception as exc:  # permisos.py ausente o roto: no se cae el control del PC
        log.debug("Sin calcular_pids_propios (%s); uso solo el PID actual", type(exc).__name__)
        return frozenset({os.getpid()})


def _descendientes(base: Iterable[int]) -> set[int]:
    """PIDs descendientes (recursivos) de ``base`` con psutil; vacío sin psutil. Bloqueante:
    se llama con ``asyncio.to_thread`` desde el loop."""
    if psutil is None:
        return set()
    resultado: set[int] = set()
    for pid in base:
        try:
            for hijo in psutil.Process(int(pid)).children(recursive=True):
                resultado.add(int(hijo.pid))
        except Exception as exc:  # NoSuchProcess, AccessDenied…
            log.debug("Sin descendientes de un proceso excluido (%s)", type(exc).__name__)
    return resultado


# ---------------------------------------------------------------------------
# DPI (main la llama al arrancar)
# ---------------------------------------------------------------------------

_dpi_activado: bool = False


def _funcion_dpi() -> Callable[[Any], Any] | None:
    """``user32.SetProcessDpiAwarenessContext`` con firma explícita (instancia privada de WinDLL)."""
    if ctypes is None:
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        funcion = user32.SetProcessDpiAwarenessContext
        funcion.argtypes = (ctypes.c_void_p,)
        funcion.restype = ctypes.c_int
        return funcion
    except (AttributeError, OSError) as exc:
        log.debug("SetProcessDpiAwarenessContext no disponible (%s)", type(exc).__name__)
        return None


def _dpi_ya_por_monitor() -> bool:
    """Tras ``ERROR_ACCESS_DENIED``: ¿el proceso ya es *per-monitor aware* (manifiesto u otra llamada)?"""
    if ctypes is None:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        obtener = user32.GetThreadDpiAwarenessContext
        obtener.restype = ctypes.c_void_p
        consciencia = user32.GetAwarenessFromDpiAwarenessContext
        consciencia.argtypes = (ctypes.c_void_p,)
        consciencia.restype = ctypes.c_int
        return int(consciencia(obtener())) == 2          # DPI_AWARENESS_PER_MONITOR_AWARE
    except (AttributeError, OSError) as exc:
        log.debug("No se pudo leer la consciencia de DPI (%s)", type(exc).__name__)
        return False


def activar_dpi_por_monitor() -> bool:
    """Windows: ``SetProcessDpiAwarenessContext(-4)`` (PER_MONITOR_AWARE_V2), idempotente.

    Fuera de Windows: ``False``. Nunca lanza. main la llama al arrancar, ANTES de construir
    ``ControlPC`` o ``VisionContinua`` (mss cambia la consciencia de DPI del proceso).
    """
    global _dpi_activado
    try:
        if not _es_windows():
            return False
        if _dpi_activado:
            return True
        funcion = _funcion_dpi()
        if funcion is None:
            return False
        if bool(funcion(ctypes.c_void_p(-4) if ctypes is not None else -4)):
            _dpi_activado = True
            return True
        error = ctypes.get_last_error() if ctypes is not None else 0
        if error == 5 and _dpi_ya_por_monitor():      # ERROR_ACCESS_DENIED: ya estaba fijada
            _dpi_activado = True
            return True
        log.debug("SetProcessDpiAwarenessContext devolvió error %s", error)
        return False
    except Exception as exc:  # contrato: nunca lanza
        log.debug("activar_dpi_por_monitor falló (%s)", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# Tipos públicos
# ---------------------------------------------------------------------------


def _texto_corto_valido(valor: Any, maximo: int = 256) -> bool:
    return (isinstance(valor, str) and len(valor) <= maximo and _RE_CONTROLES.search(valor) is None
            and _quitar_invisibles(valor) == valor)


@dataclass(frozen=True)
class ObjetivoVentana:
    """Ventana sobre la que se teclea, fijada al crear la acción (§C2.12): lo confirmado es
    exactamente lo que se ejecuta. Nunca lleva el título (dato no confiable y privado)."""

    hwnd: int | None          # Windows
    pid: int | None
    exe: str                  # nombre del ejecutable (Win) o clase (Hyprland)
    direccion: str | None     # address 0x… (Hyprland)
    clase: str

    def a_args(self) -> dict[str, Any]:
        """``{"hwnd","pid","exe","direccion","clase"}`` (para ``AccionPC.args``)."""
        return {"hwnd": self.hwnd, "pid": self.pid, "exe": self.exe, "direccion": self.direccion,
                "clase": self.clase}

    @classmethod
    def desde_args(cls, args: Mapping[str, Any]) -> ObjetivoVentana | None:
        """Valida tipos y forma; ``None`` si no casa (nunca lanza).

        ``hwnd``/``pid``: entero > 0 o ``None`` (``bool`` no vale); ``direccion``: ``None`` o
        ``^0x[0-9a-f]+$``; ``exe``/``clase``: texto ≤ 256 sin controles. Hace falta ``hwnd`` o
        ``direccion``.
        """
        try:
            if not isinstance(args, Mapping):
                return None
            hwnd, pid = args.get("hwnd"), args.get("pid")
            exe, clase = args.get("exe", ""), args.get("clase", "")
            direccion = args.get("direccion")
            for valor in (hwnd, pid):
                if valor is not None and (not _es_entero(valor) or valor <= 0):
                    return None
            if direccion is not None and (not isinstance(direccion, str)
                                          or not _RE_DIRECCION_HYPR.match(direccion)):
                return None
            if not _texto_corto_valido(exe) or not _texto_corto_valido(clase):
                return None
            if hwnd is None and direccion is None:
                return None
            return cls(hwnd=hwnd, pid=pid, exe=exe, direccion=direccion, clase=clase)
        except Exception as exc:  # contrato: nunca lanza
            log.debug("ObjetivoVentana.desde_args: argumentos no válidos (%s)", type(exc).__name__)
            return None


@dataclass(frozen=True)
class ResultadoControl:
    """Resultado de una operación de ControlPC. ``mensaje`` en español y **sin** el texto tecleado.

    ``datos`` (opcional, JSON-serializable): ``destino``, ``len``, ``sha256_12``, ``abortada_usuario``
    y, según el caso, ``escritos``, ``patron`` o ``pasos``.
    """

    ok: bool
    mensaje: str
    datos: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "mensaje", str(self.mensaje))
        object.__setattr__(self, "datos", dict(self.datos) if isinstance(self.datos, Mapping) else {})


# ---------------------------------------------------------------------------
# Piezas internas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Destino:
    """Cómo se abre un destino en este sistema."""

    clave: str
    nombre: str
    via: str                  # "startfile" (Windows) | "xdg-open" | "gtk-launch"
    objetivo: str             # URL, protocolo, shell:AppsFolder\AUMID, ruta o id .desktop
    prerrelleno: str | None   # plantilla con {texto}
    busqueda: str | None      # plantilla con {q}


class _Cupo:
    """Tasa: intervalo mínimo entre usos y máximo de usos en una ventana deslizante."""

    def __init__(self, *, intervalo_s: float = 0.0, maximo: int = 0, ventana_s: float = 0.0) -> None:
        self.intervalo_s = float(intervalo_s)
        self.maximo = int(maximo)
        self.ventana_s = float(ventana_s)
        self._usos: deque[float] = deque()

    @property
    def ultimo(self) -> float | None:
        return self._usos[-1] if self._usos else None

    def motivo(self, ahora: float) -> str | None:
        """``None`` si se permite; si no, ``"intervalo"`` o ``"maximo"``."""
        while self._usos and self.ventana_s > 0 and ahora - self._usos[0] >= self.ventana_s:
            self._usos.popleft()
        if self.intervalo_s > 0 and self._usos and ahora - self._usos[-1] < self.intervalo_s:
            return "intervalo"
        if self.maximo > 0 and len(self._usos) >= self.maximo:
            return "maximo"
        return None

    def registrar(self, ahora: float) -> None:
        self._usos.append(ahora)


def _catalogo_base(so: str) -> dict[str, _Destino]:
    """Catálogo puro (sin E/S) con la web como vía por defecto. ``so``: ``"win"``|``"linux"``."""
    if so not in ("win", "linux"):
        return {}
    via = "startfile" if so == "win" else "xdg-open"
    catalogo: dict[str, _Destino] = {}
    for clave, meta in DESTINOS_BASE.items():
        objetivo = _carpeta_personal() if clave == "archivos" else _WEB.get(clave)
        if not objetivo:
            continue
        prerrelleno = _PRERRELLENO.get(clave) if meta.get("prerrelleno") else None
        busqueda = _BUSQUEDA.get(clave) if meta.get("busqueda") else None
        catalogo[clave] = _Destino(clave, _NOMBRES.get(clave, clave), via, objetivo, prerrelleno, busqueda)
    return catalogo


def _resolver_catalogo_win(base: Mapping[str, _Destino]) -> dict[str, _Destino]:
    """Prefiere las apps instaladas (Store/Squirrel/protocolos registrados). Bloqueante (registro
    y disco): se llama con ``asyncio.to_thread``. Nunca lanza."""
    catalogo = dict(base)
    try:
        import winreg
    except ImportError:
        return catalogo

    def esquema_registrado(esquema: str) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, esquema) as clave_reg:  # type: ignore[attr-defined]
                winreg.QueryValueEx(clave_reg, "URL Protocol")  # type: ignore[attr-defined]
                return True
        except OSError:
            return False

    def existe(ruta: Path | None) -> bool:
        try:
            return ruta is not None and ruta.exists()
        except OSError:
            return False

    def cambiar(clave: str, **cambios: Any) -> None:
        if clave in catalogo:
            catalogo[clave] = dataclasses.replace(catalogo[clave], **cambios)

    try:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        roaming = os.environ.get("APPDATA", "").strip()
        if local and existe(Path(local) / "Packages" / _PFN_WHATSAPP):
            cambiar("whatsapp", objetivo="shell:AppsFolder\\" + _AUMID_WHATSAPP)
        if local and existe(Path(local) / "Discord" / "Update.exe"):
            cambiar("discord", objetivo="shell:AppsFolder\\" + _AUMID_DISCORD)
        elif esquema_registrado("discord"):
            cambiar("discord", objetivo="discord://")
        telegram = Path(roaming) / "Telegram Desktop" / "Telegram.exe" if roaming else None
        if existe(telegram):
            cambiar("telegram", objetivo=str(telegram))
        elif esquema_registrado("tg"):
            cambiar("telegram", objetivo="tg://")
        spotify = Path(roaming) / "Spotify" / "Spotify.exe" if roaming else None
        if esquema_registrado("spotify"):
            cambiar("spotify", objetivo="spotify:", busqueda="spotify:search:{q}")
        elif existe(spotify):
            cambiar("spotify", objetivo=str(spotify))
    except Exception as exc:  # el catálogo web sigue sirviendo
        log.debug("No se pudo resolver el catálogo de Windows (%s)", type(exc).__name__)
    return catalogo


def _dirs_aplicaciones() -> list[Path]:
    """Directorios ``applications`` de XDG (incluidas las exportaciones de Flatpak)."""
    bases: list[Path] = []
    datos_usuario = os.environ.get("XDG_DATA_HOME", "").strip()
    if datos_usuario:
        bases.append(Path(datos_usuario))
    else:
        personal = _carpeta_personal()
        if personal:
            bases.append(Path(personal) / ".local" / "share")
    for directorio in (os.environ.get("XDG_DATA_DIRS", "").strip() or "/usr/local/share:/usr/share").split(":"):
        if directorio.strip():
            bases.append(Path(directorio.strip()))
    personal = _carpeta_personal()
    if personal:
        bases.append(Path(personal) / ".local" / "share" / "flatpak" / "exports" / "share")
    bases.append(Path("/var/lib/flatpak/exports/share"))
    return [base / "applications" for base in bases]


def _resolver_catalogo_linux(base: Mapping[str, _Destino], gtk_launch: str | None) -> dict[str, _Destino]:
    """Usa ``gtk-launch <id>`` para las apps con ``.desktop`` instalado. Bloqueante (disco): se
    llama con ``asyncio.to_thread``. Nunca lanza."""
    catalogo = dict(base)
    if not gtk_launch:
        return catalogo
    try:
        directorios = _dirs_aplicaciones()
        for clave, candidatos in _DESKTOP_LINUX.items():
            if clave not in catalogo:
                continue
            for desktop_id in candidatos:
                if any(_es_archivo(d / f"{desktop_id}.desktop") for d in directorios):
                    catalogo[clave] = dataclasses.replace(catalogo[clave], via="gtk-launch", objetivo=desktop_id)
                    break
    except Exception as exc:
        log.debug("No se pudo resolver el catálogo de Linux (%s)", type(exc).__name__)
    return catalogo


def _es_archivo(ruta: Path) -> bool:
    try:
        return ruta.is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Apps del menú Inicio (Windows, v0.2.1; R18 del diseño v0.3)
# ---------------------------------------------------------------------------

# Lo que nunca se abre por nombre: desinstaladores, instaladores, ayudas, manuales, licencias y webs.
_RE_APP_EXCLUIDA: Final = re.compile(
    r"\b(?:uninstall\w*|desinstal\w*|unins\d*|remove|repair|reparar|setup|instalador|install\w*|update\w*"
    r"|actualizador|actualizacion|actualizar|readme|leeme|lee me|help|ayuda|manual\w*|user guide|guia del usuario|documentacion|documentation"
    r"|license|licencia|eula|release notes|changelog|notas de la version|website|sitio web|pagina web|web site"
    r"|homepage|support|soporte|feedback|faq|crash reporter|what s new|novedades)\b")
# Consolas y herramientas del sistema: se abren solo con confirmación, cada vez.
_RE_APP_SENSIBLE: Final = re.compile(
    r"\b(?:powershell|pwsh|simbolo del sistema|command prompt|cmd|terminal|git bash|git cmd|wsl|ubuntu|debian"
    r"|kali|regedit|editor del registro|registry editor|msconfig|configuracion del sistema|system configuration"
    r"|panel de control|control panel|administrador de tareas|task manager|visor de eventos|event viewer"
    r"|servicios|services|administracion de equipos|computer management|administrador de dispositivos"
    r"|device manager|programador de tareas|task scheduler|directiva de grupo|group policy"
    r"|herramientas de windows|windows tools|herramientas administrativas|administrative tools"
    r"|unidad de recuperacion|recovery drive|developer command prompt|developer powershell|python|idle"
    r"|node js|nodejs|java|consola|console|bash|seguridad de windows|windows security|configuracion|settings"
    r"|prompt|subsystem for linux|miniconda|cygwin|msys2?|mingw\w*)\b"
    r"|^(?:run|ejecutar)$")
_RE_VERSION_APP: Final = re.compile(r"\b(?:v?\d+(?:\.\d+)+|x64|x86|64 ?-?bits?|32 ?-?bits?)\b")
_RE_PARENTESIS_APP: Final = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_RE_NO_ALNUM_APP: Final = re.compile(r"[^a-z0-9]+")
_EXTENSIONES_ACCESO: Final = (".lnk", ".url")
# Sinónimos de serie (consulta normalizada → nombre normalizado del menú Inicio).
SINONIMOS_APPS: Final[Mapping[str, str]] = types.MappingProxyType({
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "consola": "terminal", "cmd": "simbolo del sistema", "notepad": "bloc de notas", "bloc": "bloc de notas",
    "calc": "calculadora", "calculator": "calculadora", "snipping tool": "recortes",
})
# Apps de la Store verificadas por su carpeta de paquete (no tienen .lnk en el menú Inicio).
_APPS_STORE_WIN: Final = (
    ("Calculadora", "Microsoft.WindowsCalculator_8wekyb3d8bbwe", ("calculadora", "calculator")),
    ("Bloc de notas", "Microsoft.WindowsNotepad_8wekyb3d8bbwe", ("bloc de notas", "notepad")),
    ("Paint", "Microsoft.Paint_8wekyb3d8bbwe", ("paint",)),
    ("Recortes", "Microsoft.ScreenSketch_8wekyb3d8bbwe", ("recortes", "herramienta recortes", "snipping tool")),
    ("Fotos", "Microsoft.Windows.Photos_8wekyb3d8bbwe", ("fotos", "photos")),
    ("Reloj", "Microsoft.WindowsAlarms_8wekyb3d8bbwe", ("reloj", "alarmas", "clock")),
)


def normalizar_nombre_app(texto: Any) -> str:
    """Nombre de app para comparar: minúsculas, sin tildes, sin ®™©, sin paréntesis, sin
    versiones ni ``x64``/``64-bit`` y solo letras y dígitos separados por un espacio. Puro."""
    crudo = "" if texto is None else str(texto)
    base = _minusculas_sin_tildes(crudo.replace("®", " ").replace("™", " ").replace("©", " "))
    base = _RE_PARENTESIS_APP.sub(" ", base)
    base = _RE_VERSION_APP.sub(" ", base)
    return " ".join(_RE_NO_ALNUM_APP.sub(" ", base).split())


def _texto_clasificable(nombre: Any) -> str:
    """Nombre crudo en minúsculas y sin tildes CON lo que va entre paréntesis y las versiones
    ("Steam (Uninstall)" → "steam uninstall"). Solo para clasificar: ``normalizar_nombre_app``
    quita los paréntesis y con ellos la palabra que delata un desinstalador. Puro."""
    crudo = "" if nombre is None else str(nombre)
    base = _minusculas_sin_tildes(crudo.replace("®", " ").replace("™", " ").replace("©", " "))
    return " ".join(_RE_NO_ALNUM_APP.sub(" ", base).split())


def _clasificar_acceso(nombre: str, norm: str) -> tuple[str, str]:
    """Clasificación más estricta entre el nombre crudo (con paréntesis) y el normalizado. Puro."""
    resultados = (clasificar_nombre_app(_texto_clasificable(nombre)), clasificar_nombre_app(norm))
    for grado in ("excluida", "sensible"):
        for obtenido, motivo in resultados:
            if obtenido == grado:
                return obtenido, motivo
    return "normal", ""


def clasificar_nombre_app(nombre_norm: str) -> tuple[str, str]:
    """``("excluida", motivo)``, ``("sensible", motivo)`` o ``("normal", "")`` por el nombre. Puro.

    El índice la aplica también al nombre crudo con paréntesis (``_clasificar_acceso``).
    """
    m = _RE_APP_EXCLUIDA.search(nombre_norm)
    if m:
        palabra = m.group(0)
        if palabra.startswith(("unins", "desinstal", "uninstall", "remove", "repair", "reparar")):
            return "excluida", "es un desinstalador"
        if palabra.startswith(("setup", "instal", "install", "update", "actualiza")):
            return "excluida", "es un instalador"
        if palabra.startswith(("website", "sitio", "pagina", "web site", "homepage", "support", "soporte")):
            return "excluida", "es un enlace a una página web"
        return "excluida", "es una ayuda, un manual o una licencia"
    if _RE_APP_SENSIBLE.search(nombre_norm):
        return "sensible", "es una consola o una herramienta del sistema"
    return "normal", ""


@dataclass(frozen=True)
class AppLocal:
    """App del índice del menú Inicio. ``objetivo`` (ruta del acceso o ``shell:AppsFolder\\…``) no sale
    de ControlPC salvo como huella (``sha256[:12]``)."""

    clave: str                 # SAFE_CLAVE_APP_RE, única en el índice
    nombre: str                # visible (≤ 80)
    norm: str                  # nombre normalizado
    objetivo: str
    via: str                   # "acceso" (.lnk/.url) | "appsfolder"
    sensible: bool = False
    alias: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolucionApp:
    """Resultado de buscar una app por nombre (sin abrir nada)."""

    estado: str                        # "unica" | "varias" | "ninguna" | "excluida" | "desactivada"
    app: AppLocal | None = None
    opciones: tuple[str, ...] = ()     # nombres visibles (≤ 3): candidatas o sugerencias
    nivel: str = ""                    # de la única: "permitida" | "descubierta" | "sensible"
    motivo: str = ""


def _dirs_menu_inicio() -> list[Path]:
    """``Start Menu\\Programs`` del usuario (``%APPDATA%``) y de todos (``%ProgramData%``)."""
    dirs: list[Path] = []
    for variable in ("APPDATA", "ProgramData", "ALLUSERSPROFILE"):
        base = os.environ.get(variable, "").strip()
        if not base:
            continue
        directorio = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if directorio not in dirs:
            dirs.append(directorio)
    return dirs


def _dir_paquetes() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "Packages" if local else None


def _url_de_acceso(ruta: Path) -> str | None:
    """``URL=`` de un ``.url`` (como mucho 4 KiB); ``None`` si no se puede leer."""
    try:
        with open(ruta, "rb") as archivo:
            crudo = archivo.read(MAX_URL_ACCESO)
    except OSError:
        return None
    for linea in crudo.decode("utf-8", "replace").splitlines():
        linea = linea.strip()
        if linea.lower().startswith("url="):
            return linea[4:].strip()
    return None


def _clave_unica(norm: str, usadas: set[str]) -> str:
    base = (norm.replace(" ", "_")[:40].strip("_")) or "app"
    clave = base
    n = 2
    while clave in usadas:
        sufijo = f"_{n}"
        clave = base[: 40 - len(sufijo)] + sufijo
        n += 1
    usadas.add(clave)
    return clave


def _indexar_menu_inicio(dirs: Sequence[Path], paquetes: Path | None) -> tuple[list[AppLocal], dict[str, str]]:
    """Índice ``(apps, excluidas{norm: motivo})`` del menú Inicio. Bloqueante (disco): se llama con
    ``asyncio.to_thread``. No sigue enlaces simbólicos ni baja más de 6 niveles. Nunca lanza."""
    apps: list[AppLocal] = []
    excluidas: dict[str, str] = {}
    vistas: dict[str, int] = {}       # nombre normalizado → posición en ``apps``
    usadas: set[str] = set()
    try:
        for base in dirs:
            if not base.is_dir():
                continue
            raiz = str(base)
            for actual, carpetas, archivos in os.walk(raiz, followlinks=False):
                profundidad = os.path.relpath(actual, raiz).count(os.sep)
                if profundidad >= MAX_PROFUNDIDAD_MENU:
                    carpetas[:] = []
                for nombre_archivo in sorted(archivos):
                    if len(apps) >= MAX_APPS_INDICE:
                        return apps, excluidas
                    ruta = Path(actual) / nombre_archivo
                    if ruta.suffix.lower() not in _EXTENSIONES_ACCESO:
                        continue
                    nombre = " ".join(ruta.stem.split())[:MAX_NOMBRE_APP]
                    norm = normalizar_nombre_app(nombre)
                    if not norm:
                        continue
                    previa = vistas.get(norm)
                    # Se clasifica ANTES de deduplicar y con el texto entre paréntesis: "Steam
                    # (Uninstall).lnk" (que ``sorted`` pone delante de "Steam.lnk") es un
                    # desinstalador y no puede ocupar el sitio de la app.
                    grado, motivo = _clasificar_acceso(nombre, norm)
                    if grado == "normal" and ruta.suffix.lower() == ".url":
                        url = (_url_de_acceso(ruta) or "").lower()
                        if url.startswith(("http:", "https:")):
                            grado, motivo = "excluida", "es un enlace a una página web"
                    if grado == "excluida":
                        excluidas.setdefault(_texto_clasificable(nombre) or norm, motivo)
                        continue
                    if previa is not None:
                        # Dos accesos con el mismo nombre normalizado ("Foo (beta)" y "Foo"): gana
                        # el de nombre visible más corto (sin paréntesis ni versión); la clave se queda.
                        anterior = apps[previa]
                        if len(nombre) < len(anterior.nombre):
                            apps[previa] = AppLocal(anterior.clave, nombre, norm, str(ruta), "acceso",
                                                    sensible=grado == "sensible")
                        continue
                    vistas[norm] = len(apps)
                    apps.append(AppLocal(_clave_unica(norm, usadas), nombre, norm, str(ruta), "acceso",
                                         sensible=grado == "sensible"))
        if paquetes is not None:
            for nombre, paquete, alias in _APPS_STORE_WIN:
                norm = normalizar_nombre_app(nombre)
                if norm in vistas or not (paquetes / paquete).is_dir():
                    continue
                vistas[norm] = len(apps)
                apps.append(AppLocal(_clave_unica(norm, usadas), nombre, norm,
                                     "shell:AppsFolder\\" + paquete + "!App", "appsfolder",
                                     alias=tuple(normalizar_nombre_app(a) for a in alias)))
    except Exception as exc:  # un menú Inicio raro nunca rompe nada: índice parcial
        log.debug("Índice del menú Inicio incompleto (%s)", type(exc).__name__)
    return apps, excluidas


def _resolver_en_indice(apps: Sequence[AppLocal], excluidas: Mapping[str, str],
                        consulta: str) -> tuple[str, list[AppLocal], str]:
    """``(estado, apps, motivo)`` de una consulta YA normalizada. Puro.

    Orden: nombre o alias exacto → excluida exacta → todas las palabras dentro del nombre (gana
    la que menos palabras sobran, si es la única así) → prefijo único → ``difflib`` (≥ 0,75) como
    sugerencia, que **nunca** abre nada.
    """
    if not consulta:
        return "ninguna", [], ""
    exactas = [a for a in apps if a.norm == consulta or consulta in a.alias]
    sinonimo = SINONIMOS_APPS.get(consulta)
    if not exactas and sinonimo:
        consulta = sinonimo
        exactas = [a for a in apps if a.norm == consulta or consulta in a.alias]
    if len(exactas) == 1:
        return "unica", exactas, ""
    if exactas:
        return "varias", exactas[:3], ""
    if consulta in excluidas:
        return "excluida", [], excluidas[consulta]
    palabras = set(consulta.split())
    contienen = [a for a in apps if palabras <= set(a.norm.split())]
    if contienen:
        contienen.sort(key=lambda a: (len(a.norm.split()), a.norm))
        sobra = [len(a.norm.split()) for a in contienen]
        if len(contienen) == 1 or sobra[0] < sobra[1]:
            return "unica", contienen[:1], ""
        return "varias", contienen[:3], ""
    prefijo = [a for a in apps if a.norm.startswith(consulta)]
    if len(prefijo) == 1:
        return "unica", prefijo, ""
    if prefijo:
        return "varias", sorted(prefijo, key=lambda a: len(a.norm))[:3], ""
    for norm, motivo in excluidas.items():
        if palabras <= set(norm.split()):
            return "excluida", [], motivo
    nombres = {a.norm: a for a in apps}
    for alias_app in apps:
        for alias in alias_app.alias:
            nombres.setdefault(alias, alias_app)
    parecidas: list[AppLocal] = []
    for norm in get_close_matches(consulta, list(nombres), n=3, cutoff=0.75):
        if nombres[norm] not in parecidas:
            parecidas.append(nombres[norm])
    return "ninguna", parecidas, ""


def _leer_aprobadas(ruta: Path) -> set[str]:
    """Objetivos aprobados de ``data/apps_aprobadas.json`` (vacío si no existe o está roto)."""
    try:
        with open(ruta, encoding="utf-8") as archivo:
            datos = json.load(archivo)
    except (OSError, ValueError):
        return set()
    entradas = datos.get("aprobadas") if isinstance(datos, Mapping) else None
    if not isinstance(entradas, list):
        return set()
    aprobadas: set[str] = set()
    for entrada in entradas[:MAX_APROBADAS]:
        objetivo = entrada.get("objetivo") if isinstance(entrada, Mapping) else None
        if isinstance(objetivo, str) and 0 < len(objetivo) <= 1024:
            aprobadas.add(objetivo)
    return aprobadas


def _guardar_aprobadas(ruta: Path, entradas: list[dict[str, str]]) -> None:
    """Escritura atómica y privada (0o600) de ``data/apps_aprobadas.json``. Bloqueante (hilo)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=".apps_aprobadas_", suffix=".tmp", dir=str(ruta.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
            json.dump({"version": 1, "aprobadas": entradas[-MAX_APROBADAS:]}, archivo, ensure_ascii=False,
                      indent=1)
        with contextlib.suppress(OSError):
            os.chmod(temporal, 0o600)
        os.replace(temporal, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


def _dentro_de(ruta: str, dirs: Sequence[Path]) -> bool:
    """``ruta`` absoluta, de un acceso directo, que existe y sigue dentro de algún directorio del
    menú Inicio (sin escapar con ``..`` ni enlaces). Bloqueante (disco)."""
    try:
        if not os.path.isabs(ruta) or Path(ruta).suffix.lower() not in _EXTENSIONES_ACCESO:
            return False
        real = os.path.realpath(ruta)
        if not os.path.isfile(real):
            return False
        for base in dirs:
            raiz = os.path.realpath(str(base))
            with contextlib.suppress(ValueError):
                if os.path.commonpath([os.path.normcase(real), os.path.normcase(raiz)]) == os.path.normcase(raiz):
                    return True
    except (OSError, ValueError, TypeError):
        return False
    return False


# ---------------------------------------------------------------------------
# Win32 (ctypes)
# ---------------------------------------------------------------------------

_ESTRUCTURAS_OK = False
if ctypes is not None and _wt is not None:
    try:
        _ULONG_PTR = ctypes.c_size_t

        class _MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", _wt.LONG), ("dy", _wt.LONG), ("mouseData", _wt.DWORD),
                        ("dwFlags", _wt.DWORD), ("time", _wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", _wt.WORD), ("wScan", _wt.WORD), ("dwFlags", _wt.DWORD),
                        ("time", _wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]

        class _HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", _wt.DWORD), ("wParamL", _wt.WORD), ("wParamH", _wt.WORD)]

        class _UNION_INPUT(ctypes.Union):
            # Debe incluir MOUSEINPUT (el miembro más grande): si no, sizeof(INPUT) no coincide
            # con el del SDK (40 en x64) y SendInput devuelve 0.
            _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

        class _INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", _wt.DWORD), ("u", _UNION_INPUT)]

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", _wt.UINT), ("dwTime", _wt.DWORD)]

        class _GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", _wt.DWORD), ("flags", _wt.DWORD), ("hwndActive", _wt.HWND),
                        ("hwndFocus", _wt.HWND), ("hwndCapture", _wt.HWND), ("hwndMenuOwner", _wt.HWND),
                        ("hwndMoveSize", _wt.HWND), ("hwndCaret", _wt.HWND), ("rcCaret", _wt.RECT)]

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", _wt.DWORD), ("rcMonitor", _wt.RECT), ("rcWork", _wt.RECT),
                        ("dwFlags", _wt.DWORD)]

        _ESTRUCTURAS_OK = True
    except (AttributeError, TypeError, ValueError) as _exc:  # pragma: no cover - ctypes exótico
        log.debug("Estructuras Win32 no disponibles (%s)", type(_exc).__name__)

_INPUT_KEYBOARD: Final = 1
_KEYEVENTF_EXTENDEDKEY: Final = 0x0001
_KEYEVENTF_KEYUP: Final = 0x0002
# Teclas multimedia (orden de "media" → tecla virtual): siguiente, anterior y play/pausa.
_VK_MEDIA_ORDEN: Final[Mapping[str, int]] = types.MappingProxyType(
    {"siguiente": 0xB0, "anterior": 0xB1, "play_pause": 0xB3})
_VK_MEDIA: Final = frozenset(_VK_MEDIA_ORDEN.values())
_KEYEVENTF_UNICODE: Final = 0x0004
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_TOKEN_QUERY: Final = 0x0008
_TOKEN_ELEVATION: Final = 20
_GWL_STYLE: Final = -16
_ES_PASSWORD: Final = 0x0020
_MONITOR_DEFAULTTONULL: Final = 0
_MONITOR_DEFAULTTONEAREST: Final = 2
_CLASES_EDIT: Final = frozenset({"edit", "richedit20w", "richedit50w", "richedit20a"})


class _ApiWin:
    """Envoltorio mínimo de Win32.

    Las funciones de **lectura** usan instancias privadas de ``WinDLL`` con firmas explícitas
    (no tocan ``ctypes.windll``, que comparten otros módulos). Las que tienen **efecto**
    (``SendInput`` y ``SetCursorPos``) se resuelven en **cada** llamada desde
    ``ctypes.windll.user32``: así la fixture ``sin_efectos_pc`` de los tests las intercepta
    siempre. Cada método devuelve ``None``/``False`` ante un error (no lanza).
    """

    def __init__(self) -> None:
        if not _es_windows() or ctypes is None or _wt is None or not _ESTRUCTURAS_OK:
            raise OSError("Win32 no disponible")
        if ctypes.sizeof(_INPUT) not in (28, 40):
            raise OSError("estructura INPUT con tamaño inesperado")
        wt = _wt
        self._u = u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        self._k = k = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._s = s = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
        self._a = a = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        firmas: tuple[tuple[Any, str, tuple[Any, ...], Any], ...] = (
            (u, "GetForegroundWindow", (), wt.HWND),
            (u, "GetWindowThreadProcessId", (wt.HWND, ctypes.POINTER(wt.DWORD)), wt.DWORD),
            (u, "GetWindowTextW", (wt.HWND, wt.LPWSTR, ctypes.c_int), ctypes.c_int),
            (u, "GetClassNameW", (wt.HWND, wt.LPWSTR, ctypes.c_int), ctypes.c_int),
            (u, "IsWindow", (wt.HWND,), wt.BOOL),
            (u, "GetCursorPos", (ctypes.POINTER(wt.POINT),), wt.BOOL),
            (u, "GetAsyncKeyState", (ctypes.c_int,), ctypes.c_short),
            (u, "GetLastInputInfo", (ctypes.POINTER(_LASTINPUTINFO),), wt.BOOL),
            (u, "GetGUIThreadInfo", (wt.DWORD, ctypes.POINTER(_GUITHREADINFO)), wt.BOOL),
            (u, "GetWindowLongW", (wt.HWND, ctypes.c_int), wt.LONG),
            (u, "GetWindowRect", (wt.HWND, ctypes.POINTER(wt.RECT)), wt.BOOL),
            (u, "MonitorFromWindow", (wt.HWND, wt.DWORD), wt.HMONITOR),
            (u, "MonitorFromPoint", (wt.POINT, wt.DWORD), wt.HMONITOR),
            (u, "GetMonitorInfoW", (wt.HMONITOR, ctypes.POINTER(_MONITORINFO)), wt.BOOL),
            (u, "GetSystemMetrics", (ctypes.c_int,), ctypes.c_int),
            (k, "GetTickCount", (), wt.DWORD),
            (k, "GetConsoleWindow", (), wt.HWND),
            (k, "OpenProcess", (wt.DWORD, wt.BOOL, wt.DWORD), wt.HANDLE),
            (k, "QueryFullProcessImageNameW", (wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)),
             wt.BOOL),
            (k, "CloseHandle", (wt.HANDLE,), wt.BOOL),
            (s, "SHQueryUserNotificationState", (ctypes.POINTER(ctypes.c_int),), ctypes.c_long),
            (a, "OpenProcessToken", (wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)), wt.BOOL),
            (a, "GetTokenInformation", (wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)), wt.BOOL),
        )
        for dll, nombre, argumentos, retorno in firmas:
            funcion = getattr(dll, nombre)
            funcion.argtypes = argumentos
            funcion.restype = retorno
        self._cache_exe: dict[int, tuple[float, str]] = {}

    # --- lectura -----------------------------------------------------------

    def primer_plano(self) -> int | None:
        try:
            hwnd = self._u.GetForegroundWindow()
            return int(hwnd) if hwnd else None
        except (OSError, ValueError, TypeError):
            return None

    def info(self, hwnd: int) -> dict[str, Any] | None:
        """``{hwnd, pid, tid, exe, clase, titulo}``. ``GetWindowTextW`` no envía mensajes a
        ventanas de otros procesos (no se cuelga con una app colgada)."""
        try:
            pid = _wt.DWORD(0)
            tid = int(self._u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)))
            if not tid:
                return None
            titulo = ctypes.create_unicode_buffer(512)
            self._u.GetWindowTextW(hwnd, titulo, 512)
            clase = ctypes.create_unicode_buffer(257)
            self._u.GetClassNameW(hwnd, clase, 257)
            return {"hwnd": int(hwnd), "pid": int(pid.value), "tid": tid, "exe": self._exe(int(pid.value)),
                    "clase": clase.value, "titulo": titulo.value}
        except (OSError, ValueError, TypeError):
            return None

    def _exe(self, pid: int) -> str:
        ahora = time.monotonic()
        guardado = self._cache_exe.get(pid)
        if guardado is not None and ahora - guardado[0] < 30.0:
            return guardado[1]
        nombre = ""
        manejador = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if manejador:
            try:
                tamano = _wt.DWORD(1024)
                ruta = ctypes.create_unicode_buffer(1024)
                if self._k.QueryFullProcessImageNameW(manejador, 0, ruta, ctypes.byref(tamano)):
                    nombre = ntpath.basename(ruta.value)
            finally:
                self._k.CloseHandle(manejador)
        if len(self._cache_exe) > 128:
            self._cache_exe.clear()
        self._cache_exe[pid] = (ahora, nombre)
        return nombre

    def existe(self, hwnd: int) -> bool:
        try:
            return bool(self._u.IsWindow(hwnd))
        except (OSError, ValueError, TypeError):
            return False

    def consola(self) -> int | None:
        """Ventana de la consola de R.A.M.S. (conhost clásico; su dueño no es un ancestor)."""
        try:
            hwnd = self._k.GetConsoleWindow()
            return int(hwnd) if hwnd else None
        except (OSError, ValueError, TypeError):
            return None

    def cursor(self) -> tuple[int, int] | None:
        try:
            punto = _wt.POINT()
            if not self._u.GetCursorPos(ctypes.byref(punto)):
                return None
            return int(punto.x), int(punto.y)
        except (OSError, ValueError, TypeError):
            return None

    def estado_tecla(self, vk: int) -> int:
        """``GetAsyncKeyState`` como entero de 16 bits (``0x8000`` = pulsada ahora)."""
        try:
            return int(self._u.GetAsyncKeyState(int(vk))) & 0xFFFF
        except (OSError, ValueError, TypeError):
            return 0

    def inactividad_s(self) -> float | None:
        try:
            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
            if not self._u.GetLastInputInfo(ctypes.byref(info)):
                return None
            return ((int(self._k.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0
        except (OSError, ValueError, TypeError):
            return None

    def quns(self) -> int | None:
        """``SHQueryUserNotificationState`` (5 = acepta notificaciones)."""
        try:
            valor = ctypes.c_int(0)
            if self._s.SHQueryUserNotificationState(ctypes.byref(valor)) != 0:
                return None
            return int(valor.value)
        except (OSError, ValueError, TypeError):
            return None

    def cubre_monitor(self, hwnd: int) -> bool:
        """La ventana ocupa todo su monitor (juegos y vídeo en ventana sin bordes incluidos)."""
        try:
            rect = _wt.RECT()
            if not hwnd or not self._u.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            monitor = self._u.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
            if not monitor or not self._u.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return False
            m = info.rcMonitor
            return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
        except (OSError, ValueError, TypeError):
            return False

    def elevado(self, pid: int) -> bool | None:
        """``True`` si el proceso corre elevado (UIPI bloquearía SendInput en silencio)."""
        try:
            proceso = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not proceso:
                return None
            token = _wt.HANDLE()
            try:
                if not self._a.OpenProcessToken(proceso, _TOKEN_QUERY, ctypes.byref(token)):
                    return None
                elevacion = _wt.DWORD(0)
                devuelto = _wt.DWORD(0)
                ok = self._a.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(elevacion),
                                                 ctypes.sizeof(elevacion), ctypes.byref(devuelto))
                return bool(elevacion.value) if ok else None
            finally:
                if token:
                    self._k.CloseHandle(token)
                self._k.CloseHandle(proceso)
        except (OSError, ValueError, TypeError):
            return None

    def foco_password(self, tid: int) -> bool | None:
        """``True`` si el control con foco es un EDIT de Win32 con ``ES_PASSWORD``; ``None`` si
        no se sabe (navegadores, Electron y UWP no tienen HWND por control)."""
        try:
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if not self._u.GetGUIThreadInfo(int(tid), ctypes.byref(info)) or not info.hwndFocus:
                return None
            clase = ctypes.create_unicode_buffer(257)
            self._u.GetClassNameW(info.hwndFocus, clase, 257)
            if clase.value.lower() not in _CLASES_EDIT:
                return None
            return bool(int(self._u.GetWindowLongW(info.hwndFocus, _GWL_STYLE)) & _ES_PASSWORD)
        except (OSError, ValueError, TypeError):
            return None

    def escritorio_virtual(self) -> tuple[int, int, int, int] | None:
        try:
            x, y, ancho, alto = (int(self._u.GetSystemMetrics(i)) for i in (76, 77, 78, 79))
            return (x, y, ancho, alto) if ancho > 0 and alto > 0 else None
        except (OSError, ValueError, TypeError):
            return None

    def monitor_de(self, x: int, y: int) -> tuple[int, int, int, int] | None:
        """``(x, y, ancho, alto)`` del monitor que contiene el punto (``MonitorFromPoint`` +
        ``GetMonitorInfoW``); ``None`` si el punto no está en ningún monitor o algo falla. Con
        monitores de distinto tamaño la caja virtual tiene huecos que no son de ningún monitor."""
        try:
            punto = _wt.POINT(int(x), int(y))
            monitor = self._u.MonitorFromPoint(punto, _MONITOR_DEFAULTTONULL)
            if not monitor:
                return None
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if not self._u.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return None
            m = info.rcMonitor
            ancho, alto = int(m.right) - int(m.left), int(m.bottom) - int(m.top)
            return (int(m.left), int(m.top), ancho, alto) if ancho > 0 and alto > 0 else None
        except (OSError, ValueError, TypeError):
            return None

    # --- efectos (resueltos en cada llamada desde ctypes.windll) ---------------

    def mover_cursor(self, x: int, y: int) -> bool:
        """``SetCursorPos`` (no reinicia ``GetLastInputInfo``; nunca hace clic)."""
        try:
            funcion = ctypes.windll.user32.SetCursorPos  # type: ignore[attr-defined]
            return bool(funcion(int(x), int(y)))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def teclear(self, unidades: list[int]) -> int:
        """``SendInput`` de pares pulsar/soltar con ``KEYEVENTF_UNICODE`` (``wVk = 0``). Devuelve
        el número de eventos insertados. Las unidades ya vienen filtradas (sin controles)."""
        try:
            limpias = [int(u) & 0xFFFF for u in unidades if 0x20 <= int(u) <= 0xFFFF and int(u) != 0x7F]
            if not limpias:
                return 0
            eventos = (_INPUT * (2 * len(limpias)))()
            for indice, unidad in enumerate(limpias):
                for desplazamiento, arriba in enumerate((False, True)):
                    evento = eventos[2 * indice + desplazamiento]
                    evento.type = _INPUT_KEYBOARD
                    evento.ki.wVk = 0
                    evento.ki.wScan = unidad
                    evento.ki.dwFlags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if arriba else 0)
                    evento.ki.time = 0
                    evento.ki.dwExtraInfo = 0
            funcion = ctypes.windll.user32.SendInput  # type: ignore[attr-defined]
            return int(funcion(len(eventos), eventos, ctypes.sizeof(_INPUT)))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0

    def tecla_media(self, vk: int) -> int:
        """``SendInput`` de una tecla multimedia (pulsar y soltar). Solo play/pausa, siguiente y
        anterior: cualquier otra tecla virtual se rechaza (nunca Enter ni atajos)."""
        try:
            if int(vk) not in _VK_MEDIA:
                return 0
            eventos = (_INPUT * 2)()
            for indice, arriba in enumerate((False, True)):
                evento = eventos[indice]
                evento.type = _INPUT_KEYBOARD
                evento.ki.wVk = int(vk)
                evento.ki.wScan = 0
                evento.ki.dwFlags = _KEYEVENTF_EXTENDEDKEY | (_KEYEVENTF_KEYUP if arriba else 0)
                evento.ki.time = 0
                evento.ki.dwExtraInfo = 0
            funcion = ctypes.windll.user32.SendInput  # type: ignore[attr-defined]
            return int(funcion(len(eventos), eventos, ctypes.sizeof(_INPUT)))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0


def _crear_api_win() -> Any:
    """``_ApiWin`` o ``None`` (sin Windows/ctypes). Los tests lo sustituyen por un falso."""
    try:
        return _ApiWin()
    except Exception as exc:
        log.debug("Win32 no disponible para el control del PC (%s)", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Hyprland (socket de órdenes, asíncrono)
# ---------------------------------------------------------------------------


class _ClienteHypr:
    """Consultas JSON al ``.socket.sock`` de Hyprland con ``asyncio.open_unix_connection``.

    Una petición por conexión (``j/<orden>``), timeout total de 250 ms, respuesta ≤ 1 MiB.
    Nunca usa ``hyprctl`` ni sockets bloqueantes. ``consultar`` devuelve ``None`` ante cualquier
    fallo (salvo la cancelación).
    """

    def __init__(self, ruta: str) -> None:
        self.ruta = ruta

    async def consultar(self, que: str) -> Any:
        if que not in _CONSULTAS_HYPR:
            return None
        abrir = getattr(asyncio, "open_unix_connection", None)
        if abrir is None:
            return None
        try:
            return await asyncio.wait_for(self._peticion(abrir, f"j/{que}"), TIMEOUT_HYPR_S)
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.TimeoutError, ValueError, UnicodeDecodeError) as exc:
            log.debug("Consulta %s a Hyprland fallida (%s)", que, type(exc).__name__)
            return None

    async def _peticion(self, abrir: Callable[..., Any], orden: str) -> Any:
        lector, escritor = await abrir(self.ruta)
        partes: list[bytes] = []
        try:
            escritor.write(orden.encode("ascii"))
            await escritor.drain()
            total = 0
            while True:
                trozo = await lector.read(65536)
                if not trozo:
                    break
                partes.append(trozo)
                total += len(trozo)
                if total > MAX_RESPUESTA_HYPR:
                    raise ValueError("respuesta de Hyprland demasiado grande")
        finally:
            escritor.close()
            with contextlib.suppress(Exception):
                await escritor.wait_closed()
        texto = b"".join(partes).decode("utf-8").strip()
        if not texto or texto[0] not in "{[":
            return None                     # "Invalid", "ok"… (p. ej. sin ventana activa)
        return json.loads(texto)


def _crear_cliente_hypr() -> Any:
    """Cliente del socket de Hyprland, o ``None`` sin sesión/socket. Los tests lo sustituyen."""
    try:
        firma = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
        if not firma or "/" in firma or firma in (".", ".."):
            return None
        candidatos: list[Path] = []
        xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if xdg:
            candidatos.append(Path(xdg) / "hypr" / firma / ".socket.sock")
        candidatos.append(Path("/tmp/hypr") / firma / ".socket.sock")
        for ruta in candidatos:
            if ruta.exists():
                return _ClienteHypr(str(ruta))
    except OSError as exc:
        log.debug("No se pudo localizar el socket de Hyprland (%s)", type(exc).__name__)
    return None


def _objetivo_hypr(ventana: Mapping[str, Any]) -> ObjetivoVentana | None:
    """``j/activewindow`` → ``ObjetivoVentana`` (``exe`` = clase), o ``None`` si no es válida."""
    direccion = ventana.get("address")
    pid = ventana.get("pid")
    clase = ventana.get("class")
    if not isinstance(direccion, str) or not _RE_DIRECCION_HYPR.match(direccion):
        return None
    if not _es_entero(pid) or pid <= 0 or not isinstance(clase, str) or not _texto_corto_valido(clase):
        return None
    return ObjetivoVentana(hwnd=None, pid=pid, exe=clase, direccion=direccion, clase=clase)


def _pantalla_completa_hypr(ventana: Mapping[str, Any]) -> bool:
    """``fullscreen`` es el modo interno de Hyprland (bits): 0 ninguno, 1 MAXIMIZADA (conserva
    márgenes y barra: no es pantalla completa), 2 pantalla completa, 3 ``FSMODE_MAX`` (pantalla
    completa sobre una maximizada). Hyprland < 0.42 daba ``fullscreen: bool`` y
    ``fullscreenMode`` (0 pantalla completa, 1 maximizada)."""
    pantalla = ventana.get("fullscreen")
    if pantalla is True:
        modo = ventana.get("fullscreenMode")
        return not (_es_entero(modo) and modo == 1)
    return _es_entero(pantalla) and pantalla > 0 and bool(pantalla & 2)


def _no_molestar_hypr(ventana: Mapping[str, Any]) -> tuple[bool, str]:
    if str(ventana.get("contentType", "") or "").lower() == "game":
        return True, "juego"
    if _pantalla_completa_hypr(ventana):
        return True, "pantalla_completa"
    if ventana.get("inhibitingIdle") is True:
        return True, "ocupado"
    return False, ""


def _detectar_plataforma(plataforma: str | None) -> str:
    if plataforma in ("win", "hypr", "otro"):
        return plataforma
    if plataforma is not None:
        log.warning("Plataforma de control del PC desconocida; se detecta sola")
    if _es_windows():
        return "win"
    if sys.platform.startswith("linux") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip():
        return "hypr"
    return "otro"


# ---------------------------------------------------------------------------
# Trayectorias de las travesuras (puras)
# ---------------------------------------------------------------------------


def _desplazamiento(dx: float, dy: float, amplitud: int) -> tuple[int, int]:
    """Redondea hacia cero y garantiza ``hypot ≤ amplitud``."""
    norma = math.hypot(dx, dy)
    if norma > amplitud > 0:
        dx, dy = dx * amplitud / norma, dy * amplitud / norma
    return int(dx), int(dy)


def _recorrido(patron: str, amplitud: int, pasos: int, inicio: tuple[int, int],
               caja: tuple[int, int, int, int] | None, azar: random.Random) -> list[tuple[int, int]]:
    """Posiciones absolutas (a más de ``amplitud`` px del inicio nunca)."""
    x0, y0 = inicio
    desplazamientos: list[tuple[int, int]] = []
    if patron == "circulo":
        radio = amplitud / 2.0
        angulo0 = azar.uniform(0.0, 2.0 * math.pi)
        cx, cy = radio * math.cos(angulo0), radio * math.sin(angulo0)   # el inicio está en el círculo
        for i in range(1, pasos + 1):
            angulo = angulo0 + math.pi + 2.0 * math.pi * i / pasos
            desplazamientos.append(_desplazamiento(cx + radio * math.cos(angulo),
                                                   cy + radio * math.sin(angulo), amplitud))
        desplazamientos[-1] = (0, 0)
    elif patron == "temblor":
        for i in range(1, pasos + 1):
            if i == pasos:
                desplazamientos.append((0, 0))
                continue
            fuerza = amplitud / 3.0 * (1.0 - 0.5 * i / pasos)
            desplazamientos.append(_desplazamiento(fuerza * (1 if i % 2 else -1),
                                                   azar.uniform(-fuerza / 2.0, fuerza / 2.0), amplitud))
    else:  # "empujon" y "hacia_avatar"
        angulo = azar.uniform(0.0, 2.0 * math.pi)
        alcance = float(amplitud)
        if patron == "hacia_avatar" and caja is not None:
            cx = caja[0] + caja[2] / 2.0 - x0
            cy = caja[1] + caja[3] / 2.0 - y0
            distancia = math.hypot(cx, cy)
            if distancia >= 1.0:
                angulo = math.atan2(cy, cx)
                alcance = min(float(amplitud), distancia)
        for i in range(1, pasos + 1):
            suave = 0.5 - 0.5 * math.cos(math.pi * i / pasos)          # ease-in-out
            desplazamientos.append(_desplazamiento(alcance * suave * math.cos(angulo),
                                                   alcance * suave * math.sin(angulo), amplitud))
    return [(x0 + dx, y0 + dy) for dx, dy in desplazamientos]


# ---------------------------------------------------------------------------
# ControlPC
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ventana:
    objetivo: ObjetivoVentana


class ControlPC:
    """Efector de control del PC (apps, teclado y cursor) sin decisiones propias (§C2.8).

    ``plataforma`` es inyectable en los tests (``"win"`` | ``"hypr"`` | ``"otro"``); ``None``
    la detecta. ``reloj`` mide tasas, esperas y ``ts_ultima_inyeccion``. ``dirs_menu_inicio`` y
    ``dir_paquetes`` (v0.2.1, tests) sustituyen a los del menú Inicio y de la Store del usuario.
    """

    def __init__(self, settings: Settings, *, reloj: Callable[[], float] = time.monotonic,
                 plataforma: str | None = None, dirs_menu_inicio: Sequence[Path] | None = None,
                 dir_paquetes: Path | None = None) -> None:
        self.settings = settings
        self._reloj = reloj
        # v0.2.1: índice perezoso de las apps del menú Inicio (se arma en un hilo al primer uso).
        self._dirs_menu: list[Path] = (list(dirs_menu_inicio) if dirs_menu_inicio is not None
                                       else _dirs_menu_inicio())
        self._dir_paquetes: Path | None = (dir_paquetes if dir_paquetes is not None or dirs_menu_inicio is not None
                                           else _dir_paquetes())
        self._apps: list[AppLocal] = []
        self._apps_por_clave: dict[str, AppLocal] = {}
        self._apps_excluidas: dict[str, str] = {}
        self._apps_ts: float | None = None
        self._apps_alias_permitidos: set[str] = set()
        self._aprobadas: set[str] = set()
        self._aprobadas_entradas: list[dict[str, str]] = []
        self._lock_apps: asyncio.Lock | None = None
        # Esperas del tecleo y de las travesuras (los tests ponen una que avanza el reloj falso).
        self._dormir: Callable[[float], Any] = asyncio.sleep
        # Plazo de las travesuras (≤ 600 ms): ``perf_counter`` con el reloj de serie (en Windows
        # ``monotonic`` puede ir a saltos de ~15,6 ms); con un reloj inyectado, ese mismo.
        self._crono: Callable[[], float] = time.perf_counter if reloj is time.monotonic else reloj
        self.plataforma: str = _detectar_plataforma(plataforma)
        if self.plataforma == "win":
            self._so = "win"
        elif self.plataforma == "hypr" or os.name == "posix":
            self._so = "linux"
        else:
            self._so = "ninguno"
        self._win: Any = _crear_api_win() if self.plataforma == "win" else None
        self._hypr: Any = _crear_cliente_hypr() if self.plataforma == "hypr" else None
        self._prog: dict[str, str | None] = (
            {nombre: _buscar_programa(nombre) for nombre in ("xdg-open", "gtk-launch", "wtype")}
            if self._so == "linux" else {})
        self._catalogo: dict[str, _Destino] = _catalogo_base(self._so)
        self._pids_propios: frozenset[int] = _pids_propios()
        # La consola de R.A.M.S. en conhost clásico pertenece a conhost.exe (no es un ancestro):
        # se excluye por hwnd. Con Windows Terminal, WindowsTerminal.exe ya es un ancestro.
        self._hwnd_consola: int | None = None
        consola = getattr(self._win, "consola", None)
        if callable(consola):
            try:
                valor = consola()
                self._hwnd_consola = int(valor) if _es_entero(valor) and valor > 0 else None
            except Exception as exc:
                log.debug("Sin ventana de consola propia (%s)", type(exc).__name__)
        self._pids_base: set[int] = set()
        self._pids_desc: frozenset[int] = frozenset()
        self._anillo: deque[_Ventana] = deque(maxlen=ANILLO_VENTANAS)
        self._activa: ObjetivoVentana | None = None
        self._hypr_activa: dict[str, Any] = {}
        self._cursor: tuple[int, int] | None = None
        self._caja: tuple[int, int, int, int] | None = None
        self._cupo_abrir = _Cupo(maximo=ABRIR_MAX_MINUTO, ventana_s=VENTANA_MINUTO_S)
        self._cupo_prerrellenar = _Cupo(intervalo_s=PRERRELLENAR_INTERVALO_S, maximo=PRERRELLENAR_MAX_DIA,
                                        ventana_s=VENTANA_DIA_S)
        self._cupo_escribir = _Cupo(intervalo_s=ESCRIBIR_INTERVALO_S, maximo=ESCRIBIR_MAX_DIA,
                                    ventana_s=VENTANA_DIA_S)
        self._cupo_travesura = _Cupo(intervalo_s=TRAVESURA_INTERVALO_S, maximo=TRAVESURA_MAX_DIA,
                                     ventana_s=VENTANA_DIA_S)
        self._cupo_media = _Cupo(maximo=MEDIA_MAX_MINUTO, ventana_s=VENTANA_MINUTO_S)
        self._fin_ultima_travesura: float | None = None
        self._escribiendo = False
        self._ts_inyeccion: float | None = None
        self._tarea_sondeo: asyncio.Task[None] | None = None
        self._tareas: set[asyncio.Task[Any]] = set()
        self._cerrado = False
        self._azar = random.Random()
        self.disponible: bool = self._calcular_disponible()

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def _calcular_disponible(self) -> bool:
        if self.plataforma == "win":
            return self._win is not None
        return bool(self._prog.get("xdg-open") or self._prog.get("gtk-launch")
                    or (self._hypr is not None and self._prog.get("wtype")))

    async def iniciar(self) -> DiagnosticoModulo:
        """Resuelve el catálogo de apps instaladas (en un hilo) y arranca el sondeo de ventanas.
        Devuelve el diagnóstico ``"control_pc"``. Nunca lanza."""
        try:
            if self._so == "win":
                self._catalogo = await asyncio.to_thread(_resolver_catalogo_win, dict(self._catalogo))
            elif self._so == "linux":
                self._catalogo = await asyncio.to_thread(_resolver_catalogo_linux, dict(self._catalogo),
                                                         self._prog.get("gtk-launch"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Catálogo de destinos sin resolver (%s); uso la web", type(exc).__name__)
        try:
            sondea = (self.plataforma == "win" and self._win is not None) or (
                self.plataforma == "hypr" and self._hypr is not None)
            if sondea and not self._cerrado and (self._tarea_sondeo is None or self._tarea_sondeo.done()):
                self._tarea_sondeo = asyncio.get_running_loop().create_task(
                    self._bucle_sondeo(), name="control_pc_sondeo")
        except Exception as exc:
            log.debug("No se pudo arrancar el sondeo de ventanas (%s)", type(exc).__name__)
        return self._diagnostico()

    def _diagnostico(self) -> DiagnosticoModulo:
        try:
            if self.plataforma == "win":
                if self._win is None:
                    return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OFF, "Win32 (ctypes) no disponible")
                travesuras = "ON" if getattr(self.settings, "travesuras_habilitadas", False) else "OFF"
                menu = "ON" if self.apps_locales_disponibles else "OFF"
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OK,
                                         f"Windows: apps (menú Inicio {menu}), teclado y cursor "
                                         f"(travesuras {travesuras})")
            if self.plataforma == "hypr":
                if not self.disponible:
                    return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OFF,
                                             "faltan xdg-open, gtk-launch y wtype (pacman -S xdg-utils gtk3 wtype)")
                if self._hypr is None:
                    return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.DEGRADADO,
                                             "sin socket de Hyprland: solo abro enlaces")
                faltan = [p for p in ("wtype", "xdg-open") if not self._prog.get(p)]
                if faltan:
                    return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.DEGRADADO, "falta " + ", ".join(faltan))
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OK,
                                         "Hyprland: apps y teclado (wtype); sin travesuras con el cursor real")
            if self.disponible:
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.DEGRADADO, "solo abrir enlaces (xdg-open)")
            return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OFF, "no disponible en este sistema")
        except Exception as exc:  # pragma: no cover - defensivo
            log.debug("Diagnóstico del control del PC fallido (%s)", type(exc).__name__)
            return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.DEGRADADO, "error interno")

    async def cerrar(self) -> None:
        """Detiene el sondeo y los vigilantes de procesos. Idempotente; nunca lanza."""
        self._cerrado = True
        tareas = [t for t in (self._tarea_sondeo, *self._tareas) if t is not None and not t.done()]
        for tarea in tareas:
            tarea.cancel()
        if tareas:
            await asyncio.gather(*tareas, return_exceptions=True)
        self._tareas.clear()
        self._tarea_sondeo = None

    def _crear_tarea(self, corrutina: Any, nombre: str) -> None:
        try:
            tarea = asyncio.get_running_loop().create_task(corrutina, name=nombre)
        except RuntimeError:
            corrutina.close()
            return
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)

    # ------------------------------------------------------------------
    # Sondeo de la ventana en primer plano (2 Hz)
    # ------------------------------------------------------------------

    async def _bucle_sondeo(self) -> None:
        ultimo_refresco: float | None = None
        while not self._cerrado:
            try:
                if self.plataforma == "win" and self._win is not None:
                    self._muestrear_win()
                elif self.plataforma == "hypr" and self._hypr is not None:
                    await self._muestrear_hypr()
                ahora = self._reloj()
                if self._pids_base and (ultimo_refresco is None
                                        or ahora - ultimo_refresco >= REFRESCO_DESCENDIENTES_S):
                    ultimo_refresco = ahora
                    await self._refrescar_descendientes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Sondeo de ventanas fallido (%s)", type(exc).__name__)
            await asyncio.sleep(PERIODO_SONDEO_S)

    def _muestrear_win(self) -> ObjetivoVentana | None:
        """Lectura directa (< 1 ms) de la ventana en primer plano y del cursor."""
        api = self._win
        if api is None:
            return None
        hwnd = api.primer_plano()
        cursor = api.cursor()
        if cursor is not None:
            self._cursor = (int(cursor[0]), int(cursor[1]))
        if not hwnd:
            self._activa = None
            return None
        info = api.info(hwnd)
        if not info:
            self._activa = None
            return None
        pid = info.get("pid")
        objetivo = ObjetivoVentana(hwnd=int(hwnd), pid=int(pid) if _es_entero(pid) and pid > 0 else None,
                                   exe=str(info.get("exe") or "")[:256], direccion=None,
                                   clase=str(info.get("clase") or "")[:256])
        self._activa = objetivo
        self._anotar(objetivo, str(info.get("titulo") or ""))
        return objetivo

    async def _muestrear_hypr(self) -> None:
        cliente = self._hypr
        if cliente is None:
            return
        ventana = await cliente.consultar("activewindow")
        if isinstance(ventana, Mapping):
            self._hypr_activa = dict(ventana)
            objetivo = _objetivo_hypr(ventana)
            self._activa = objetivo
            if objetivo is not None:
                self._anotar(objetivo, str(ventana.get("title") or ""))
        elif ventana is None:
            self._activa = None
            self._hypr_activa = {}
        cursor = await cliente.consultar("cursorpos")
        if isinstance(cursor, Mapping) and _es_entero(cursor.get("x")) and _es_entero(cursor.get("y")):
            self._cursor = (int(cursor["x"]), int(cursor["y"]))

    def _misma_ventana(self, a: ObjetivoVentana, b: ObjetivoVentana) -> bool:
        if a.hwnd is not None or b.hwnd is not None:
            return a.hwnd == b.hwnd
        return a.direccion == b.direccion

    def _es_ventana_propia(self, objetivo: ObjetivoVentana) -> bool:
        """Por proceso (R.A.M.S. y ancestros, Amadeus Desktop y descendientes) o por ser la
        consola de R.A.M.S."""
        if objetivo.pid is not None and objetivo.pid in (self._pids_propios | self.pids_excluidos()):
            return True
        return self._hwnd_consola is not None and objetivo.hwnd == self._hwnd_consola

    def _es_objetivo_util(self, objetivo: ObjetivoVentana) -> bool:
        """Descarta el shell de Windows y los diálogos de seguridad (nunca son el objetivo)."""
        if self.plataforma == "win":
            exe = _base_exe(objetivo.exe)
            return not (objetivo.clase in CLASES_SHELL_WIN or exe in EXES_SHELL_WIN or exe in EXES_SEGURIDAD_WIN)
        return _RE_SEGURIDAD_LINUX.search(objetivo.clase or "") is None

    def _anotar(self, objetivo: ObjetivoVentana, titulo: str) -> None:
        """Anillo de las 8 últimas ventanas en primer plano que no son de Amadeus.

        Una ventana AJENA que ahora muestra un título propio (la pestaña del HUD en el mismo
        navegador que WhatsApp Web) ni entra ni sale: si ya estaba, conserva su sitio, que es la
        última vez que estuvo delante con un título ajeno. No se escribe en ella mientras
        muestre el HUD: la espera del foco y :meth:`puede_teclear` (antes de cada trozo)
        revalidan el título actual."""
        pestana_propia = (TITULOS_PROPIOS_RE.match(titulo.strip()) is not None
                          and not self._es_ventana_propia(objetivo))
        for anterior in list(self._anillo):
            if self._misma_ventana(anterior.objetivo, objetivo):
                if pestana_propia and anterior.objetivo.pid == objetivo.pid:
                    continue
                self._anillo.remove(anterior)
        if pestana_propia or self._es_ventana_propia(objetivo) or not self._es_objetivo_util(objetivo):
            return
        self._anillo.append(_Ventana(objetivo))

    def _purgar_anillo(self) -> None:
        excluidos = self._pids_propios | self.pids_excluidos()
        for anterior in list(self._anillo):
            if anterior.objetivo.pid in excluidos:
                self._anillo.remove(anterior)

    async def _refrescar_descendientes(self) -> None:
        base = frozenset(self._pids_base)
        if not base:
            return
        try:
            self._pids_desc = frozenset(await asyncio.to_thread(_descendientes, base))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("No se pudieron refrescar los descendientes (%s)", type(exc).__name__)
            return
        self._purgar_anillo()

    # ------------------------------------------------------------------
    # Consultas síncronas (cachés)
    # ------------------------------------------------------------------

    def ventana_activa(self) -> ObjetivoVentana | None:
        """Ventana en primer plano (Windows: lectura directa; Hyprland: caché del sondeo)."""
        try:
            if self.plataforma == "win":
                return self._muestrear_win()
            if self.plataforma == "hypr":
                return self._activa
        except Exception as exc:
            log.debug("ventana_activa falló (%s)", type(exc).__name__)
        return None

    def ventana_previa(self) -> ObjetivoVentana | None:
        """Última ventana en primer plano que NO es de Amadeus (ni R.A.M.S. y sus ancestros, ni
        Amadeus Desktop y sus descendientes, ni títulos ``TITULOS_PROPIOS_RE``). Cuenta la última
        vez que estuvo delante con un título ajeno (ver :meth:`_anotar`)."""
        try:
            if self.plataforma == "win":
                self._muestrear_win()
            excluidos = self._pids_propios | self.pids_excluidos()
            for anterior in reversed(self._anillo):
                objetivo = anterior.objetivo
                if objetivo.pid is None or objetivo.pid in excluidos:
                    continue
                if self.plataforma == "win" and objetivo.hwnd is not None and self._win is not None \
                        and not self._win.existe(objetivo.hwnd):
                    continue
                return objetivo
        except Exception as exc:
            log.debug("ventana_previa falló (%s)", type(exc).__name__)
        return None

    def excluir_pids(self, pids: Iterable[int]) -> None:
        """Excluye Amadeus Desktop (``escritorio.app``) y sus descendientes (psutil, en un hilo)."""
        try:
            nuevos = {int(p) for p in pids if _es_entero(p) and p > 0}
        except TypeError:
            log.debug("excluir_pids: lista no iterable; se ignora")
            return
        except Exception as exc:
            log.debug("excluir_pids: lista no válida (%s)", type(exc).__name__)
            return
        if not nuevos - self._pids_base:
            return
        self._pids_base |= nuevos
        self._purgar_anillo()
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # sin loop (scripts/tests síncronos): cálculo directo
            try:
                self._pids_desc = frozenset(_descendientes(self._pids_base))
                self._purgar_anillo()
            except Exception as exc:
                log.debug("Descendientes no calculados (%s)", type(exc).__name__)
            return
        self._crear_tarea(self._refrescar_descendientes(), "control_pc_descendientes")

    def pids_excluidos(self) -> frozenset[int]:
        """Amadeus Desktop y sus descendientes (la compuerta los usa como ``pids_extra``)."""
        return frozenset(self._pids_base) | self._pids_desc

    def set_caja_avatar(self, caja: tuple[int, int, int, int] | None) -> None:
        """Caja del avatar en píxeles físicos globales (``escritorio.caja``), para ``hacia_avatar``."""
        try:
            if caja is None:
                self._caja = None
                return
            valores = tuple(caja)
            if len(valores) != 4 or not all(_es_entero(v) for v in valores):
                return
            x, y, ancho, alto = valores
            if not (0 < ancho <= 20000 and 0 < alto <= 20000 and -100000 <= x <= 100000
                    and -100000 <= y <= 100000):
                return
            self._caja = (x, y, ancho, alto)
        except Exception as exc:
            log.debug("set_caja_avatar: caja no válida (%s)", type(exc).__name__)

    def inactividad_s(self) -> float | None:
        """Segundos sin entrada (Windows: ``GetLastInputInfo``); Hyprland y otros: ``None``."""
        try:
            if self.plataforma == "win" and self._win is not None:
                valor = self._win.inactividad_s()
                return float(valor) if valor is not None else None
        except Exception as exc:
            log.debug("inactividad_s falló (%s)", type(exc).__name__)
        return None

    def quieto(self) -> bool | None:
        """¿El usuario lleva ≥ 4–5 s sin teclado ni ratón?

        Linux/Hyprland: archivo-bandera de hypridle (``$XDG_RUNTIME_DIR/rams/quieto``, o el
        antiguo ``$XDG_RUNTIME_DIR/rams_quieto``); sin ``XDG_RUNTIME_DIR``: ``None``. Windows:
        ``inactividad_s() ≥ 4``.
        """
        try:
            if self.plataforma == "win":
                inactividad = self.inactividad_s()
                return None if inactividad is None else inactividad >= INACTIVIDAD_MIN_S
            if self._so == "linux":
                xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
                if not xdg:
                    return None
                return any(os.path.exists(ruta) for ruta in (os.path.join(xdg, "rams", "quieto"),
                                                             os.path.join(xdg, "rams_quieto")))
        except Exception as exc:
            log.debug("quieto falló (%s)", type(exc).__name__)
        return None

    def cursor(self) -> tuple[int, int] | None:
        """Posición del cursor (Windows: lectura directa; Hyprland: caché del sondeo)."""
        try:
            if self.plataforma == "win" and self._win is not None:
                actual = self._win.cursor()
                if actual is not None:
                    self._cursor = (int(actual[0]), int(actual[1]))
            return self._cursor
        except Exception as exc:
            log.debug("cursor falló (%s)", type(exc).__name__)
            return None

    def ts_ultima_inyeccion(self) -> float | None:
        """Instante (unidades de ``reloj``) de la última entrada sintética (``SendInput``/wtype)."""
        return self._ts_inyeccion

    def destinos(self) -> Mapping[str, Mapping[str, Any]]:
        """Clave de ``DESTINOS_BASE`` → cómo se abre en este sistema (``nombre``, ``via``,
        ``objetivo``, ``prerrelleno``, ``busqueda``), de solo lectura."""
        try:
            return types.MappingProxyType({
                clave: types.MappingProxyType({"nombre": d.nombre, "via": d.via, "objetivo": d.objetivo,
                                               "prerrelleno": d.prerrelleno, "busqueda": d.busqueda})
                for clave, d in self._catalogo.items() if clave in DESTINOS_BASE})
        except Exception as exc:  # pragma: no cover - defensivo
            log.debug("destinos falló (%s)", type(exc).__name__)
            return types.MappingProxyType({})

    def anti_contrasena(self, texto: str, *, titulo: str = "") -> bool:
        """``True`` = rechazar: ``config.parece_secreto(texto, settings.secretos())`` o
        ``config.titulo_sensible(titulo)``. Nunca lanza (ante un error, ``True``)."""
        try:
            secretos_fn = getattr(self.settings, "secretos", None)
            secretos = tuple(secretos_fn()) if callable(secretos_fn) else ()
            if _parece_secreto(texto, secretos):
                return True
            return bool(titulo) and _titulo_sensible(titulo)
        except Exception as exc:
            log.debug("anti_contrasena no pudo evaluar (%s); se rechaza", type(exc).__name__)
            return True

    # ------------------------------------------------------------------
    # Abrir, buscar y prerrellenar
    # ------------------------------------------------------------------

    def _clave(self, alias: Any) -> str | None:
        """Alias (clave o sinónimo de ``RAMS_DESTINOS``) → clave de ``DESTINOS_BASE``."""
        if not isinstance(alias, str):
            return None
        alias = alias.strip().lower()
        if not SAFE_ALIAS_RE.match(alias):
            return None
        try:
            permitidos = getattr(self.settings, "destinos_permitidos", None)
            clave = permitidos.get(alias) if isinstance(permitidos, Mapping) else None
        except Exception:
            clave = None
        if clave is None and alias in DESTINOS_BASE:
            clave = alias
        return clave if clave in DESTINOS_BASE else None

    def _nombre(self, clave: str) -> str:
        return _NOMBRES.get(clave, clave)

    def _via_enlaces(self) -> str | None:
        if self._so == "win":
            return "startfile"
        if self._so == "linux":
            return "xdg-open"
        return None

    async def _lanzar(self, via: str | None, objetivo: str) -> tuple[bool, str]:
        """Abre ``objetivo`` (siempre del catálogo o un enlace construido aquí). Sin shell."""
        try:
            if via == "startfile":
                if not (_enlace_seguro(objetivo) or os.path.isabs(objetivo)):
                    return False, "enlace no permitido"
                abrir = getattr(os, "startfile", None)
                if abrir is None:
                    return False, "no disponible en este sistema"
                await asyncio.to_thread(abrir, objetivo)
                return True, ""
            if via in ("xdg-open", "gtk-launch"):
                programa = self._prog.get(via)
                if not programa:
                    return False, f"falta {via}"
                if via == "gtk-launch":
                    if not _RE_ID_DESKTOP.match(objetivo):
                        return False, "aplicación no válida"
                elif not (_enlace_seguro(objetivo) or os.path.isabs(objetivo)):
                    return False, "enlace no permitido"
                proceso = await asyncio.create_subprocess_exec(
                    programa, objetivo, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
                try:
                    codigo = await asyncio.wait_for(proceso.wait(), ESPERA_FALLO_LANZADOR_S)
                except asyncio.TimeoutError:
                    self._crear_tarea(self._vigilar_proceso(proceso), "control_pc_lanzador")
                    return True, ""
                if codigo == 0:
                    return True, ""
                return False, f"{via} terminó con código {codigo}"
            return False, "no disponible en este sistema"
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log.debug("No se pudo abrir un destino (%s)", type(exc).__name__)
            return False, "el sistema no encontró una app para abrirlo"

    async def _vigilar_proceso(self, proceso: Any) -> None:
        """Recoge el código de salida de un lanzador que siguió vivo (evita zombis)."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proceso.wait(), VIGILANCIA_LANZADOR_S)

    def _fallo(self, mensaje: str, **datos: Any) -> ResultadoControl:
        return ResultadoControl(False, mensaje, datos)

    async def abrir_destino(self, alias: str) -> ResultadoControl:
        """Abre la app o la web de una clave de ``DESTINOS_BASE`` (≤ 6/min con ``buscar_en``)."""
        try:
            clave = self._clave(alias)
            if clave is None:
                return self._fallo("No conozco ese destino.")
            if self._cerrado:
                return self._fallo("El control del PC está apagado.", destino=clave)
            destino = self._catalogo.get(clave)
            if destino is None:
                return self._fallo(f"No sé abrir {self._nombre(clave)} en este sistema.", destino=clave)
            ahora = self._reloj()
            if self._cupo_abrir.motivo(ahora):
                return self._fallo("Vas muy rápido: ya abrí 6 cosas en el último minuto.", destino=clave)
            self._cupo_abrir.registrar(ahora)
            ok, detalle = await self._lanzar(destino.via, destino.objetivo)
            if ok:
                return ResultadoControl(True, f"Abrí {destino.nombre}.", {"destino": clave})
            return self._fallo(f"No pude abrir {destino.nombre}: {detalle}.", destino=clave)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("abrir_destino falló (%s)", type(exc).__name__)
            return self._fallo("No pude abrirlo por un error interno.")

    async def buscar_en(self, alias: str, consulta: str) -> ResultadoControl:
        """Abre la búsqueda de ``consulta`` (≤ 120, sin claves) en YouTube, Spotify, X o DuckDuckGo."""
        try:
            clave = self._clave(alias)
            if clave is None:
                return self._fallo("No conozco ese destino.")
            nombre = self._nombre(clave)
            destino = self._catalogo.get(clave)
            if not DESTINOS_BASE[clave].get("busqueda") or destino is None or not destino.busqueda:
                return self._fallo(f"No sé buscar en {nombre}.", destino=clave)
            limpia = _sanear(consulta, MAX_CONSULTA)
            if limpia is None:
                return self._fallo("La búsqueda está vacía o pasa de 120 caracteres.", destino=clave)
            datos = {"destino": clave, "len": len(limpia), "sha256_12": _huella(limpia)}
            # Misma regla que la compuerta (§C2.11): redactar(alta_entropia) no debe cambiar nada.
            # (No se usa la regla OTP: "busca 1984 en youtube" es una búsqueda legítima.)
            secretos_fn = getattr(self.settings, "secretos", None)
            secretos = tuple(secretos_fn()) if callable(secretos_fn) else ()
            redactar = getattr(_config, "redactar", None)
            sospechosa = (redactar(limpia, secretos, alta_entropia=True) != limpia if callable(redactar)
                          else self.anti_contrasena(limpia))
            if sospechosa:
                return ResultadoControl(False, "Eso parece una clave o un token; no lo busco.", datos)
            if self._cerrado:
                return ResultadoControl(False, "El control del PC está apagado.", datos)
            enlace = destino.busqueda.replace("{q}", quote(limpia, safe=""))
            if not _enlace_seguro(enlace):
                return ResultadoControl(False, f"No pude preparar la búsqueda en {nombre}.", datos)
            ahora = self._reloj()
            if self._cupo_abrir.motivo(ahora):
                return ResultadoControl(False, "Vas muy rápido: ya abrí 6 cosas en el último minuto.", datos)
            self._cupo_abrir.registrar(ahora)
            ok, detalle = await self._lanzar(self._via_enlaces(), enlace)
            if ok:
                return ResultadoControl(True, f"Abrí la búsqueda en {nombre}.", datos)
            return ResultadoControl(False, f"No pude abrir la búsqueda en {nombre}: {detalle}.", datos)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("buscar_en falló (%s)", type(exc).__name__)
            return self._fallo("No pude buscar por un error interno.")

    def enlace_prerrelleno(self, alias: str, texto: str) -> str | None:
        """Enlace profundo que deja ``texto`` como borrador (puro; ``None`` si no se puede).

        WhatsApp ``wa.me/?text=``, Telegram ``t.me/share/url?url=&text=``, X
        ``x.com/intent/post?text=``, Gmail ``view=cm&fs=1&body=`` y correo ``mailto:?body=``. El
        texto se sanea (sin saltos ni controles, ≤ 500) y se urlcodifica entero.
        """
        try:
            clave = self._clave(alias)
            if clave is None or not DESTINOS_BASE[clave].get("prerrelleno"):
                return None
            plantilla = _PRERRELLENO.get(clave)
            limpio = _sanear(texto, MAX_TEXTO)
            if plantilla is None or limpio is None:
                return None
            enlace = plantilla.replace("{texto}", quote(limpio, safe=""))
            return enlace if _enlace_seguro(enlace) else None
        except Exception as exc:
            log.debug("enlace_prerrelleno falló (%s)", type(exc).__name__)
            return None

    async def prerrellenar(self, alias: str, texto: str) -> ResultadoControl:
        """Abre el destino con un borrador. **Nunca envía**: el usuario revisa y pulsa Enviar."""
        try:
            clave = self._clave(alias)
            if clave is None:
                return self._fallo("No conozco ese destino.")
            nombre = self._nombre(clave)
            if not DESTINOS_BASE[clave].get("prerrelleno"):
                return self._fallo(f"{nombre} no admite borradores por enlace.", destino=clave)
            limpio = _sanear(texto, MAX_TEXTO)
            if limpio is None:
                return self._fallo("El borrador está vacío o pasa de 500 caracteres.", destino=clave)
            datos = {"destino": clave, "len": len(limpio), "sha256_12": _huella(limpio)}
            if self.anti_contrasena(limpio):
                return ResultadoControl(False, "Eso parece una contraseña o un código; no lo pongo en ningún "
                                               "borrador.", datos)
            if self._cerrado or self._via_enlaces() is None:
                return ResultadoControl(False, "Abrir enlaces no está disponible en este sistema.", datos)
            ahora = self._reloj()
            motivo = self._cupo_prerrellenar.motivo(ahora)
            if motivo == "intervalo":
                return ResultadoControl(False, "Espera unos segundos antes de otro borrador.", datos)
            if motivo:
                return ResultadoControl(False, "Ya preparé demasiados borradores hoy.", datos)
            enlace = self.enlace_prerrelleno(clave, limpio)
            if enlace is None:
                return ResultadoControl(False, f"No pude preparar el borrador para {nombre}.", datos)
            self._cupo_prerrellenar.registrar(ahora)
            ok, detalle = await self._lanzar(self._via_enlaces(), enlace)
            if not ok:
                return ResultadoControl(False, f"No pude abrir {nombre}: {detalle}.", datos)
            mensaje = (f"Abrí {nombre} con el borrador ({len(limpio)} caracteres). No envié nada: "
                       "revísalo y envíalo tú.")
            if clave == "x" and len(limpio) > X_MAX_CARACTERES:
                mensaje += f" Ojo: X corta en {X_MAX_CARACTERES} caracteres."
            return ResultadoControl(True, mensaje, datos)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("prerrellenar falló (%s)", type(exc).__name__)
            return self._fallo("No pude preparar el borrador por un error interno.")

    # ------------------------------------------------------------------
    # v0.2.1: apps del menú Inicio (Windows)
    # ------------------------------------------------------------------

    @property
    def apps_locales_disponibles(self) -> bool:
        """``True`` en Windows con ``RAMS_APPS_MENU_INICIO=1`` (por defecto)."""
        return self._so == "win" and bool(getattr(self.settings, "apps_menu_inicio", True))

    def _ruta_aprobadas(self) -> Path:
        return Path(self.settings.data_dir) / ARCHIVO_APPS_APROBADAS

    async def _asegurar_indice(self, *, forzar: bool = False) -> None:
        """Arma (o refresca cada 5 min) el índice del menú Inicio en un hilo. Nunca lanza."""
        if not self.apps_locales_disponibles:
            return
        ahora = self._reloj()
        if not forzar and self._apps_ts is not None and ahora - self._apps_ts < TTL_INDICE_APPS_S:
            return
        if self._lock_apps is None:
            self._lock_apps = asyncio.Lock()
        async with self._lock_apps:
            ahora = self._reloj()
            if not forzar and self._apps_ts is not None and ahora - self._apps_ts < TTL_INDICE_APPS_S:
                return
            try:
                apps, excluidas = await asyncio.to_thread(_indexar_menu_inicio, list(self._dirs_menu),
                                                          self._dir_paquetes)
                aprobadas = await asyncio.to_thread(_leer_aprobadas, self._ruta_aprobadas())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("No se pudo leer el menú Inicio (%s)", type(exc).__name__)
                return
            self._apps = apps
            self._apps_por_clave = {a.clave: a for a in apps}
            self._apps_excluidas = excluidas
            self._aprobadas = aprobadas
            self._apps_alias_permitidos = self._claves_de_rams_apps()
            self._apps_ts = ahora
            log.info("Menú Inicio: %d apps indexadas (%d aprobadas)", len(apps),
                     sum(1 for a in apps if a.objetivo in aprobadas))

    def _alias_rams_apps(self) -> Mapping[str, str]:
        try:
            apps = getattr(self.settings, "apps_permitidas", None)
            return apps if isinstance(apps, Mapping) else {}
        except Exception:
            return {}

    def _claves_de_rams_apps(self) -> set[str]:
        """Claves del índice a las que lleva un alias de ``RAMS_APPS`` (cuentan como permitidas)."""
        claves: set[str] = set()
        for alias, comando in self._alias_rams_apps().items():
            for candidato in (comando, alias):
                estado, apps, _ = _resolver_en_indice(self._apps, self._apps_excluidas,
                                                      normalizar_nombre_app(str(candidato).replace("_", " ")))
                if estado == "unica":
                    claves.add(apps[0].clave)
                    break
        return claves

    def _nivel(self, app: AppLocal) -> str:
        if app.sensible:
            return "sensible"
        if app.objetivo in self._aprobadas or app.clave in self._apps_alias_permitidos:
            return "permitida"
        return "descubierta"

    def nivel_app_local(self, clave: str) -> tuple[str, str] | None:
        """``(nivel, nombre)`` de una app del índice (para la compuerta) o ``None``. Síncrono."""
        try:
            app = self._apps_por_clave.get(clave) if isinstance(clave, str) else None
            return (self._nivel(app), app.nombre) if app is not None else None
        except Exception as exc:  # pragma: no cover - defensivo
            log.debug("nivel_app_local falló (%s)", type(exc).__name__)
            return None

    async def resolver_app_local(self, nombre: str) -> ResolucionApp:
        """Busca ``nombre`` en el menú Inicio (alias de ``RAMS_APPS`` primero). No abre nada; nunca lanza."""
        try:
            if not self.apps_locales_disponibles:
                motivo = ("desactivado (RAMS_APPS_MENU_INICIO=0)" if self._so == "win"
                          else "solo en Windows")
                return ResolucionApp("desactivada", motivo=motivo)
            await self._asegurar_indice()
            texto = str(nombre or "").strip()[:MAX_NOMBRE_APP]
            if not texto:
                return ResolucionApp("ninguna")
            existente = self._apps_por_clave.get(texto) if SAFE_CLAVE_APP_RE.match(texto) else None
            if existente is not None:
                return ResolucionApp("unica", existente, nivel=self._nivel(existente))
            consulta = normalizar_nombre_app(texto)
            alias = self._alias_rams_apps().get(consulta.replace(" ", "_"))
            if isinstance(alias, str) and alias:
                estado, apps, _ = _resolver_en_indice(self._apps, self._apps_excluidas,
                                                      normalizar_nombre_app(alias.replace("_", " ")))
                if estado == "unica":
                    return ResolucionApp("unica", apps[0], nivel=self._nivel(apps[0]))
            estado, apps, motivo = _resolver_en_indice(self._apps, self._apps_excluidas, consulta)
            if estado == "unica":
                return ResolucionApp("unica", apps[0], nivel=self._nivel(apps[0]))
            return ResolucionApp(estado, opciones=tuple(a.nombre for a in apps[:3]), motivo=motivo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("resolver_app_local falló (%s)", type(exc).__name__)
            return ResolucionApp("ninguna")

    async def aprobar_app_local(self, clave: str) -> bool:
        """Recuerda la aprobación de una app (por su acceso) en ``data/apps_aprobadas.json``.

        Las sensibles nunca se recuerdan (se confirman cada vez). Devuelve ``True`` si quedó guardada.
        """
        try:
            app = self._apps_por_clave.get(clave) if isinstance(clave, str) else None
            if app is None or app.sensible:
                return False
            if app.objetivo in self._aprobadas:
                return True
            ruta = self._ruta_aprobadas()
            if not self._aprobadas_entradas:
                self._aprobadas_entradas = await asyncio.to_thread(self._entradas_aprobadas, ruta)
            entradas = [e for e in self._aprobadas_entradas if e.get("objetivo") != app.objetivo]
            entradas.append({"objetivo": app.objetivo, "nombre": app.nombre,
                             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            await asyncio.to_thread(_guardar_aprobadas, ruta, entradas)
            self._aprobadas_entradas = entradas[-MAX_APROBADAS:]
            self._aprobadas.add(app.objetivo)
            log.info("App del menú Inicio aprobada (%s)", _huella(app.objetivo))
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No se pudo guardar la aprobación de la app (%s)", type(exc).__name__)
            return False

    @staticmethod
    def _entradas_aprobadas(ruta: Path) -> list[dict[str, str]]:
        """Entradas válidas del archivo de aprobadas (para reescribirlo sin perder las demás)."""
        try:
            with open(ruta, encoding="utf-8") as archivo:
                datos = json.load(archivo)
        except (OSError, ValueError):
            return []
        entradas = datos.get("aprobadas") if isinstance(datos, Mapping) else None
        if not isinstance(entradas, list):
            return []
        validas: list[dict[str, str]] = []
        for entrada in entradas[:MAX_APROBADAS]:
            if isinstance(entrada, Mapping) and isinstance(entrada.get("objetivo"), str):
                validas.append({k: str(v)[:1024] for k, v in entrada.items()
                                if k in ("objetivo", "nombre", "ts") and isinstance(v, str)})
        return validas

    async def lanzar_app_local(self, nombre: str) -> ResultadoControl:
        """Abre una app del menú Inicio por su clave del índice (o por nombre, si es inequívoco).

        Sin shell ni argumentos: ``os.startfile`` del acceso directo (que debe seguir dentro del
        menú Inicio) o ``shell:AppsFolder\\<AUMID>`` de una app de la Store verificada. Comparte el
        cupo de 6 aperturas por minuto con ``abrir_destino``. La autorización ya la dio la compuerta.
        """
        try:
            if not self.apps_locales_disponibles:
                return self._fallo("Abrir apps del menú Inicio no está disponible aquí.")
            if self._cerrado:
                return self._fallo("El control del PC está apagado.")
            resolucion = await self.resolver_app_local(nombre)
            app = resolucion.app
            if resolucion.estado != "unica" or app is None:
                return self._fallo("No encuentro esa app en el menú Inicio.")
            datos = {"app": app.clave, "nombre": app.nombre, "sha256_12": _huella(app.objetivo)}
            if app.via == "acceso":
                valido = await asyncio.to_thread(_dentro_de, app.objetivo, list(self._dirs_menu))
            else:
                valido = _enlace_seguro(app.objetivo)
            if not valido:
                return ResultadoControl(False, f"El acceso de {app.nombre} ya no está en el menú Inicio.", datos)
            ahora = self._reloj()
            if self._cupo_abrir.motivo(ahora):
                return ResultadoControl(False, "Vas muy rápido: ya abrí 6 cosas en el último minuto.", datos)
            self._cupo_abrir.registrar(ahora)
            ok, detalle = await self._lanzar("startfile", app.objetivo)
            if ok:
                return ResultadoControl(True, f"Abrí {app.nombre}.", datos)
            return ResultadoControl(False, f"No pude abrir {app.nombre}: {detalle}.", datos)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("lanzar_app_local falló (%s)", type(exc).__name__)
            return self._fallo("No pude abrir la app por un error interno.")

    # ------------------------------------------------------------------
    # v0.2.1: teclas multimedia (Windows) y título de la ventana activa
    # ------------------------------------------------------------------

    async def media(self, orden: str) -> ResultadoControl:
        """Pausa/reanuda, siguiente o anterior con las teclas multimedia de Windows (≤ 30/min)."""
        try:
            vk = _VK_MEDIA_ORDEN.get(str(orden or ""))
            if vk is None:
                return self._fallo("Orden multimedia no válida.")
            if self.plataforma != "win" or self._win is None:
                return self._fallo("Las teclas multimedia solo están disponibles en Windows.")
            if self._cerrado:
                return self._fallo("El control del PC está apagado.")
            ahora = self._reloj()
            if self._cupo_media.motivo(ahora):
                return self._fallo("Demasiadas órdenes de música seguidas; espera un momento.")
            self._cupo_media.registrar(ahora)
            pulsar = getattr(self._win, "tecla_media", None)
            insertados = int(pulsar(vk) or 0) if callable(pulsar) else 0
            self._ts_inyeccion = self._reloj()
            if insertados < 2:
                return self._fallo("Windows no aceptó la tecla multimedia.", orden=orden)
            mensajes = {"play_pause": "Pausé o reanudé la música.", "siguiente": "Pasé a la siguiente canción.",
                        "anterior": "Volví a la canción anterior."}
            return ResultadoControl(True, mensajes.get(str(orden), "Hecho."), {"orden": orden})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("media falló (%s)", type(exc).__name__)
            return self._fallo("No pude controlar la música por un error interno.")

    def titulo_activa(self) -> str | None:
        """Título de la ventana en primer plano (para no capturar ventanas privadas). Nunca se
        registra ni sale de R.A.M.S.; ``None`` si no se sabe."""
        try:
            if self.plataforma == "win" and self._win is not None:
                hwnd = self._win.primer_plano()
                info = self._win.info(hwnd) if hwnd else None
                titulo = info.get("titulo") if isinstance(info, Mapping) else None
                return titulo if isinstance(titulo, str) else None
            if self.plataforma == "hypr":
                titulo = self._hypr_activa.get("title")
                return titulo if isinstance(titulo, str) else None
        except Exception as exc:
            log.debug("titulo_activa falló (%s)", type(exc).__name__)
        return None

    # ------------------------------------------------------------------
    # No molestar
    # ------------------------------------------------------------------

    async def no_molestar(self) -> tuple[bool, str]:
        """``(activo, motivo)`` del sistema (§C1.11). Windows: QUNS ∈ {2, 3, 4}. Hyprland: ventana
        activa a pantalla completa, ``contentType == "game"`` o ``inhibitingIdle``."""
        try:
            if self.plataforma == "win" and self._win is not None:
                estado = self._win.quns()
                if estado in QUNS_NO_MOLESTAR:
                    return True, QUNS_NO_MOLESTAR[estado]
                return False, ""
            if self.plataforma == "hypr" and self._hypr is not None:
                ventana = await self._hypr.consultar("activewindow")
                if not isinstance(ventana, Mapping):
                    ventana = self._hypr_activa
                return _no_molestar_hypr(ventana or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("no_molestar falló (%s)", type(exc).__name__)
        return False, ""

    # ------------------------------------------------------------------
    # Teclear
    # ------------------------------------------------------------------

    def _como_objetivo(self, objetivo: Any) -> ObjetivoVentana | None:
        if isinstance(objetivo, ObjetivoVentana):
            return ObjetivoVentana.desde_args(objetivo.a_args())
        if isinstance(objetivo, Mapping):
            return ObjetivoVentana.desde_args(objetivo)
        return None

    def _nombre_app(self, objetivo: ObjetivoVentana) -> str:
        nombre = (objetivo.exe or objetivo.clase or "").strip()
        return nombre[:40] if nombre else "esa ventana"

    def _modificadores(self) -> bool:
        api = self._win
        return api is not None and any(int(api.estado_tecla(vk)) & 0x8000 for vk in VK_MODIFICADORES)

    def _botones_o_modificadores(self) -> bool:
        api = self._win
        return api is not None and any(int(api.estado_tecla(vk)) & 0x8000
                                       for vk in (*VK_BOTONES, *VK_MODIFICADORES))

    def _usuario_teclea(self) -> bool:
        """Windows: ¿alguna tecla pulsada ahora o desde la consulta anterior? (bit ``0x8001``).
        Ignora ``VK_PACKET`` (lo nuestro) y los botones del ratón. Recorre todas las teclas para
        consumir los bits "pulsada desde la última vez"."""
        api = self._win
        if api is None:
            return False
        tecleo = False
        for vk in range(0x08, 0xFF):
            if vk == VK_PACKET or vk in VK_BOTONES:
                continue
            if int(api.estado_tecla(vk)) & 0x8001:
                tecleo = True
        return tecleo

    async def _tiene_foco(self, objetivo: ObjetivoVentana) -> bool:
        """¿Está ``objetivo`` delante con un título que no es de Amadeus? Si el usuario confirmó
        desde la pestaña del HUD de esa misma ventana del navegador, se espera a que cambie de
        pestaña (no se cuenta como foco)."""
        if self.plataforma == "win" and self._win is not None:
            hwnd = self._win.primer_plano()
            if not hwnd or objetivo.hwnd is None or int(hwnd) != objetivo.hwnd:
                return False
            titulo = str((self._win.info(hwnd) or {}).get("titulo") or "")
            return TITULOS_PROPIOS_RE.match(titulo.strip()) is None
        if self.plataforma == "hypr" and self._hypr is not None:
            ventana = await self._hypr.consultar("activewindow")
            return (isinstance(ventana, Mapping) and objetivo.direccion is not None
                    and ventana.get("address") == objetivo.direccion
                    and TITULOS_PROPIOS_RE.match(str(ventana.get("title") or "").strip()) is None)
        return False

    async def puede_teclear(self, objetivo: ObjetivoVentana) -> tuple[bool, str]:
        """¿Se puede teclear YA en ``objetivo``? Foco, proceso, no elevado, no ``ES_PASSWORD``, sin
        modificadores, título no sensible y no es una ventana de Amadeus. Nunca lanza."""
        try:
            obj = self._como_objetivo(objetivo)
            if obj is None:
                return False, "no sé en qué ventana escribir"
            if obj.pid is None:
                return False, "no sé de qué app es la ventana"
            if self._es_ventana_propia(obj):
                return False, "es una ventana de Amadeus"
            if self.plataforma == "win":
                return self._puede_teclear_win(obj)
            if self.plataforma == "hypr":
                return await self._puede_teclear_hypr(obj)
            return False, "escribir en otras apps no está disponible en este sistema"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("puede_teclear falló (%s)", type(exc).__name__)
            return False, "no pude comprobar la ventana"

    def _puede_teclear_win(self, obj: ObjetivoVentana) -> tuple[bool, str]:
        api = self._win
        if api is None:
            return False, "escribir en otras apps no está disponible en este sistema"
        app = self._nombre_app(obj)
        if obj.hwnd is None:
            return False, "la ventana no es de Windows"
        if _base_exe(obj.exe) in EXES_SEGURIDAD_WIN:
            return False, "es un diálogo de seguridad o la pantalla de bloqueo"
        hwnd = api.primer_plano()
        if not hwnd or int(hwnd) != obj.hwnd:
            return False, f"{app} no tiene el foco"
        info = api.info(hwnd) or {}
        if info.get("pid") != obj.pid:
            return False, "la ventana cambió de app"
        if _base_exe(info.get("exe")) in EXES_SEGURIDAD_WIN:
            return False, "es un diálogo de seguridad o la pantalla de bloqueo"
        if TITULOS_PROPIOS_RE.match(str(info.get("titulo") or "").strip()):
            return False, "es una ventana de Amadeus"
        if _titulo_sensible(info.get("titulo") or ""):
            return False, "la ventana parece de contraseñas, bancos o pagos"
        if api.elevado(obj.pid) is not False:
            return False, f"{app} corre como administrador (o no pude comprobarlo)"
        tid = info.get("tid")
        if _es_entero(tid) and api.foco_password(tid) is True:
            return False, "el cuadro con el foco es de contraseña"
        if self._modificadores():
            return False, "hay Ctrl, Alt, Shift o Windows pulsada"
        return True, "ok"

    async def _puede_teclear_hypr(self, obj: ObjetivoVentana) -> tuple[bool, str]:
        if self._hypr is None:
            return False, "sin conexión con Hyprland"
        if not self._prog.get("wtype"):
            return False, "falta wtype (pacman -S wtype)"
        app = self._nombre_app(obj)
        if obj.direccion is None:
            return False, "la ventana no es de Hyprland"
        if _RE_SEGURIDAD_LINUX.search(obj.clase or obj.exe or ""):
            return False, "es un diálogo de contraseña del sistema"
        ventana = await self._hypr.consultar("activewindow")
        if not isinstance(ventana, Mapping) or ventana.get("address") != obj.direccion:
            return False, f"{app} no tiene el foco"
        if ventana.get("pid") != obj.pid:
            return False, "la ventana cambió de app"
        if _RE_SEGURIDAD_LINUX.search(str(ventana.get("class") or "")):
            return False, "es un diálogo de contraseña del sistema"
        titulo = str(ventana.get("title") or "")
        if TITULOS_PROPIOS_RE.match(titulo.strip()):
            return False, "es una ventana de Amadeus"
        if _titulo_sensible(titulo):
            return False, "la ventana parece de contraseñas, bancos o pagos"
        return True, "ok"

    async def escribir_texto(self, texto: str, *, objetivo: ObjetivoVentana,
                             espera_foco_s: float = 10.0) -> ResultadoControl:
        """Teclea ``texto`` (saneado, ≤ 500) en ``objetivo``. **NUNCA pulsa Enter.**

        No fuerza el foco: espera hasta ``espera_foco_s`` a que el usuario haga clic en la app; si
        no, aborta sin escribir. Revalida :meth:`puede_teclear` antes de cada trozo y para si el
        usuario teclea (Windows). Tasas: ≤ 1 cada 30 s, ≤ 20 al día, una a la vez.
        """
        try:
            return await self._escribir(texto, objetivo, espera_foco_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("escribir_texto falló (%s)", type(exc).__name__)
            return self._fallo("No pude escribir por un error interno; no envié nada.")

    async def _escribir(self, texto: Any, objetivo: Any, espera_foco_s: Any) -> ResultadoControl:
        obj = self._como_objetivo(objetivo)
        if obj is None:
            return self._fallo("No sé en qué ventana escribir.")
        app = self._nombre_app(obj)
        limpio = _sanear(texto, MAX_TEXTO)
        if limpio is None:
            return self._fallo("El texto está vacío o pasa de 500 caracteres; no escribí nada.", destino=app)
        datos: dict[str, Any] = {"destino": app, "len": len(limpio), "sha256_12": _huella(limpio),
                                 "escritos": 0, "abortada_usuario": False}
        if self.anti_contrasena(limpio):
            return ResultadoControl(False, "Eso parece una contraseña o un código; no lo escribo.", datos)
        disponible = (self.plataforma == "win" and self._win is not None) or (
            self.plataforma == "hypr" and self._hypr is not None and bool(self._prog.get("wtype")))
        if not disponible or self._cerrado:
            return ResultadoControl(False, "Escribir en otras apps no está disponible en este sistema.", datos)
        if self._escribiendo:
            return ResultadoControl(False, "Ya estoy escribiendo algo; espera a que termine.", datos)
        if (self.plataforma == "win" and obj.hwnd is None) or (self.plataforma == "hypr" and obj.direccion is None):
            return ResultadoControl(False, "Esa ventana no es de este sistema; no escribí nada.", datos)
        if obj.pid is None or self._es_ventana_propia(obj):
            return ResultadoControl(False, "Esa es una ventana de Amadeus; no escribo ahí.", datos)
        if (self.plataforma == "win" and _base_exe(obj.exe) in EXES_SEGURIDAD_WIN) or (
                self.plataforma == "hypr" and _RE_SEGURIDAD_LINUX.search(obj.clase or obj.exe or "")):
            return ResultadoControl(False, "Eso es un diálogo de seguridad; ahí no escribo.", datos)
        motivo = self._cupo_escribir.motivo(self._reloj())
        if motivo == "intervalo":
            return ResultadoControl(False, "Espera unos segundos antes de que vuelva a escribir.", datos)
        if motivo:
            return ResultadoControl(False, "Ya escribí demasiadas veces hoy.", datos)
        try:
            espera = float(espera_foco_s)
        except (TypeError, ValueError):
            espera = 10.0
        if not math.isfinite(espera):
            espera = 10.0
        espera = min(max(espera, 0.0), ESPERA_FOCO_MAX_S)
        self._escribiendo = True
        try:
            if not await self._esperar_foco(obj, espera):
                return ResultadoControl(False, f"No tuviste el foco en {app} a tiempo; no escribí nada.", datos)
            await self._dormir(ASENTAR_FOCO_S)
            self._cupo_escribir.registrar(self._reloj())
            if self.plataforma == "win":
                return await self._teclear_win(obj, limpio, app, datos)
            return await self._teclear_wtype(obj, limpio, app, datos)
        finally:
            self._escribiendo = False

    async def _esperar_foco(self, obj: ObjetivoVentana, espera: float) -> bool:
        inicio = self._reloj()
        while not self._cerrado:
            if await self._tiene_foco(obj):
                return True
            if self._reloj() - inicio >= espera:
                return False
            await self._dormir(PAUSA_FOCO_S)
        return False

    def _parcial(self, mensaje: str, datos: dict[str, Any], escritos: int, *,
                 usuario: bool = False) -> ResultadoControl:
        datos = {**datos, "escritos": escritos, "abortada_usuario": usuario}
        if escritos:
            mensaje += f" Alcancé a escribir {escritos} de {datos.get('len', 0)} caracteres; no envié nada."
        else:
            mensaje += " No escribí nada."
        return ResultadoControl(False, mensaje, datos)

    async def _teclear_win(self, obj: ObjetivoVentana, limpio: str, app: str,
                           datos: dict[str, Any]) -> ResultadoControl:
        api = self._win
        escritos = 0
        self._usuario_teclea()          # consume los bits "pulsada desde la última consulta"
        for trozo in _trozos(limpio, TROZO_WIN):
            ok, motivo = await self.puede_teclear(obj)
            if not ok:
                return self._parcial(f"Paré de escribir: {motivo}.", datos, escritos)
            if self._usuario_teclea():
                return self._parcial("Paré porque estás tecleando.", datos, escritos, usuario=True)
            unidades = _utf16(trozo)
            insertados = int(api.teclear(unidades)) if unidades else 0
            self._ts_inyeccion = self._reloj()
            if insertados != 2 * len(unidades):
                return self._parcial("Windows bloqueó la escritura.", datos, escritos)
            escritos += len(trozo)
            await self._dormir(PAUSA_TROZO_S)
        return ResultadoControl(True, f"Listo: escribí {escritos} caracteres en {app}. No envié nada.",
                                {**datos, "escritos": escritos})

    async def _teclear_wtype(self, obj: ObjetivoVentana, limpio: str, app: str,
                             datos: dict[str, Any]) -> ResultadoControl:
        programa = self._prog.get("wtype")
        escritos = 0
        for trozo in _trozos(limpio, TROZO_WTYPE):
            ok, motivo = await self.puede_teclear(obj)
            if not ok:
                return self._parcial(f"Paré de escribir: {motivo}.", datos, escritos)
            # El texto viaja por stdin (no por argv: no aparece en /proc/*/cmdline) y sin "\n".
            proceso = await asyncio.create_subprocess_exec(
                programa, "-d", "8", "-", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proceso.communicate(trozo.encode("utf-8")), TIMEOUT_WTYPE_S)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    proceso.kill()
                    await proceso.wait()
                self._ts_inyeccion = self._reloj()
                return self._parcial("wtype no respondió.", datos, escritos)
            self._ts_inyeccion = self._reloj()
            if proceso.returncode != 0:
                return self._parcial("wtype falló.", datos, escritos)
            escritos += len(trozo)
            await self._dormir(PAUSA_TROZO_S)
        return ResultadoControl(True, f"Listo: escribí {escritos} caracteres en {app}. No envié nada.",
                                {**datos, "escritos": escritos})

    # ------------------------------------------------------------------
    # Travesuras con el cursor (§C2.12)
    # ------------------------------------------------------------------

    def _vetos_travesura(self) -> tuple[bool, str]:
        """Guardas propias (sin el contexto de main): plataforma, maestro, tecleo en curso, tasas,
        sesión/diálogos de seguridad, QUNS, pantalla completa, apps de llamada, inactividad y
        botones. Las comparten :meth:`puede_travesura` y :meth:`empujon_cursor`."""
        if self.plataforma == "hypr":
            return False, "sin estado de botones en Wayland"
        api = self._win
        if self.plataforma != "win" or api is None:
            return False, "no disponible en este sistema"
        if self._cerrado:
            return False, "el control del PC está apagado"
        if not getattr(self.settings, "travesuras_habilitadas", False):
            return False, "desactivadas en la configuración"
        if self._escribiendo:
            return False, "estoy escribiendo en otra app"
        ahora = self._reloj()
        cupo = self._cupo_travesura.motivo(ahora)
        if cupo == "intervalo":
            return False, "hace menos de 15 minutos de la última"
        if cupo:
            return False, "ya hice demasiadas hoy"
        inactividad = api.inactividad_s()
        if self._fin_ultima_travesura is not None:
            if inactividad is None or ahora - float(inactividad) <= self._fin_ultima_travesura:
                return False, "no has tocado el PC desde la última"
        hwnd = api.primer_plano()
        if not hwnd:
            return False, "no hay ventana en primer plano"
        info = api.info(hwnd) or {}
        exe = _base_exe(info.get("exe"))
        if exe in EXES_SEGURIDAD_WIN:
            return False, "sesión bloqueada o diálogo de seguridad"
        estado = api.quns()
        if estado != QUNS_ACEPTA_NOTIFICACIONES:
            return False, f"Windows no acepta notificaciones ahora (estado {estado})"
        # "Excluida la propia": el overlay de Amadeus Desktop cubre el área de trabajo a propósito.
        # (Una terminal de R.A.M.S. a pantalla completa SÍ veta: es conservador.)
        pid = info.get("pid")
        overlay = (_es_entero(pid) and pid in self.pids_excluidos()) or (
            TITULOS_PROPIOS_RE.match(str(info.get("titulo") or "").strip()) is not None)
        if not overlay and info.get("clase") not in CLASES_ESCRITORIO_WIN and api.cubre_monitor(hwnd):
            return False, "hay una ventana a pantalla completa"
        apps_pausa = {_base_exe(a) for a in (getattr(self.settings, "vision_apps_pausa", ()) or ())}
        if exe and exe in apps_pausa:
            return False, "hay una app de llamada o de grabación en primer plano"
        if inactividad is None:
            return False, "no sé si estás usando el PC"
        if inactividad < INACTIVIDAD_MIN_S:
            return False, "estás usando el teclado o el ratón"
        if inactividad > INACTIVIDAD_MAX_S:
            return False, "pareces ausente"
        if self._botones_o_modificadores():
            return False, "hay botones o teclas pulsadas"
        if api.cursor() is None:
            return False, "no puedo leer el cursor"
        return True, "ok"

    async def puede_travesura(self, *, presente: bool, voz_activa: bool, confirmacion_pendiente: bool,
                              video_activo: bool) -> tuple[bool, str]:
        """¿Se puede hacer una travesura ahora? Contexto de main (presencia, voz, confirmación,
        vídeo) + guardas propias. En Hyprland: ``(False, "sin estado de botones en Wayland")``."""
        try:
            if self.plataforma == "hypr":
                return False, "sin estado de botones en Wayland"
            if not presente:
                return False, "no estás presente"
            if voz_activa:
                return False, "estoy hablando"
            if confirmacion_pendiente:
                return False, "hay una confirmación pendiente"
            if video_activo:
                return False, "hay un vídeo en reproducción"
            return self._vetos_travesura()
        except Exception as exc:
            log.debug("puede_travesura falló (%s)", type(exc).__name__)
            return False, "no pude comprobar si es buen momento"

    async def empujon_cursor(self, patron: str, amplitud_px: int, *, segundos: float = 0.5) -> ResultadoControl:
        """Travesura con el cursor real: solo ``SetCursorPos`` (nunca clics, rueda ni teclas).

        Antes de cada paso aborta si hay botones o modificadores pulsados, y si el usuario movió
        el ratón > 6 px desde donde quedó el cursor en el paso anterior (``datos["abortada_usuario"]
        = True``: main las apaga 2 h). Si Windows recoloca el cursor (``ClipCursor``, fuera de
        todo monitor) para con ``datos["recolocado"] = True`` sin culpar al usuario. El recorrido
        se queda en el monitor del cursor. Topes: ``amplitud ≤ travesura_amplitud_max_px``,
        ``≤ 600 ms`` (plan por plazo), ≤ 1 cada 15 min, ≤ 6 al día.
        """
        try:
            return await self._empujon(patron, amplitud_px, segundos)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("empujon_cursor falló (%s)", type(exc).__name__)
            return self._fallo("La travesura falló por un error interno.", abortada_usuario=False)

    async def _empujon(self, patron: Any, amplitud_px: Any, segundos: Any) -> ResultadoControl:
        if not isinstance(patron, str) or patron not in PATRONES_TRAVESURA:
            return self._fallo("No conozco esa travesura.", abortada_usuario=False)
        datos: dict[str, Any] = {"patron": patron, "abortada_usuario": False}
        if not _es_entero(amplitud_px) or amplitud_px <= 0:
            return ResultadoControl(False, "Amplitud de travesura no válida.", datos)
        tope = getattr(self.settings, "travesura_amplitud_max_px", 40)
        tope = tope if _es_entero(tope) and tope > 0 else 40
        amplitud = min(int(amplitud_px), tope, TRAVESURA_AMPLITUD_TOPE)
        try:
            duracion = float(segundos)
        except (TypeError, ValueError):
            duracion = 0.5
        if not math.isfinite(duracion):
            duracion = 0.5
        duracion = min(max(duracion, TRAVESURA_MIN_S), TRAVESURA_MAX_S)
        ok, motivo = self._vetos_travesura()
        if not ok:
            return ResultadoControl(False, f"Travesura vetada: {motivo}.", datos)
        if patron == "hacia_avatar" and self._caja is None:
            return ResultadoControl(False, "No sé dónde está el avatar.", datos)
        api = self._win
        inicio = api.cursor()
        if inicio is None:
            return ResultadoControl(False, "No puedo leer el cursor.", datos)
        inicio = (int(inicio[0]), int(inicio[1]))
        pasos = max(4, min(TRAVESURA_MAX_PASOS, round(duracion / 0.025)))
        recorrido = _recorrido(patron, amplitud, pasos, inicio, self._caja, self._azar)
        limites = self._limites_cursor(api, inicio)
        if limites is not None:
            vx, vy, ancho, alto = limites
            recorrido = [(min(max(x, vx), vx + ancho - 1), min(max(y, vy), vy + alto - 1)) for x, y in recorrido]
        ultimo_indice = len(recorrido) - 1
        # Plan por plazo (≤ 600 ms): el paso k va en t0 + k·intervalo y el último a más tardar en
        # t0 + duración − margen. Si una espera se alarga (en Windows se redondean hacia arriba
        # al tic del temporizador), se saltan pasos intermedios, nunca el último.
        intervalo = max(duracion - TRAVESURA_MARGEN_S, 0.0) / max(1, ultimo_indice)
        self._cupo_travesura.registrar(self._reloj())
        ultimo = inicio
        movidos = 0
        indice = 0
        datos["pasos"] = 0
        t0 = self._crono()
        try:
            while True:
                x, y = recorrido[indice]
                if self._botones_o_modificadores():
                    return ResultadoControl(False, "Paré: tocaste un botón o una tecla.",
                                            {**datos, "pasos": movidos, "abortada_usuario": True})
                actual = api.cursor()
                if actual is None:
                    return ResultadoControl(False, "Perdí el cursor; paré.", {**datos, "pasos": movidos})
                if _distancia((int(actual[0]), int(actual[1])), ultimo) > UMBRAL_USUARIO_PX:
                    return ResultadoControl(False, "Paré: moviste el ratón.",
                                            {**datos, "pasos": movidos, "abortada_usuario": True})
                if not api.mover_cursor(x, y):
                    return ResultadoControl(False, "Windows no me dejó mover el cursor.",
                                            {**datos, "pasos": movidos})
                movidos += 1
                # La referencia del paso siguiente es donde QUEDÓ el cursor, no el punto pedido:
                # Windows lo recoloca si cae fuera de todo monitor o de un ClipCursor.
                real = api.cursor()
                if real is None:
                    return ResultadoControl(False, "Perdí el cursor; paré.", {**datos, "pasos": movidos})
                ultimo = (int(real[0]), int(real[1]))
                if _distancia(ultimo, (x, y)) > TOLERANCIA_RECOLOCADO_PX:
                    return ResultadoControl(False, "Windows recolocó el cursor; paré.",
                                            {**datos, "pasos": movidos, "recolocado": True})
                if indice >= ultimo_indice:
                    return ResultadoControl(True, "Travesura hecha.", {**datos, "pasos": movidos})
                siguiente = indice + 1
                espera = t0 + siguiente * intervalo - self._crono()
                if espera > 0:
                    await self._dormir(espera)
                transcurrido = self._crono() - t0
                toca = int(transcurrido / intervalo) if intervalo > 0 else ultimo_indice
                indice = min(ultimo_indice, max(siguiente, toca))
        finally:
            self._fin_ultima_travesura = self._reloj()

    @staticmethod
    def _limites_cursor(api: Any, punto: tuple[int, int]) -> tuple[int, int, int, int] | None:
        """Rectángulo en el que se queda la travesura: el monitor que contiene ``punto`` (con
        monitores de distinto tamaño la caja virtual tiene huecos donde Windows recolocaría el
        cursor); si no se sabe, la caja virtual."""
        monitor_de = getattr(api, "monitor_de", None)
        if callable(monitor_de):
            caja = monitor_de(*punto)
            if caja is not None:
                return caja
        return api.escritorio_virtual()
