"""Cuenta de Google de R.A.M.S. BIO-CORE: OAuth de app de escritorio y cliente REST (v0.2.2).

Qué hace (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §6):
- Conecta la cuenta de Google del usuario con OAuth 2.0 de «app instalada»: PKCE ``S256``,
  ``state`` comparado con ``hmac.compare_digest`` y una redirección *loopback* propia en
  ``127.0.0.1:<puerto aleatorio>`` (``asyncio.start_server``). El consentimiento se da en el
  **navegador del sistema** (nunca en Electron ni en un WebView).
- Guarda el *refresh token* de forma privada y atómica en ``google_token_path``: en Windows,
  cifrado con DPAPI (ámbito del usuario actual; si DPAPI falla, **no** se guarda y la sesión vale
  solo en memoria: ``token_no_guardado``); en el resto, con permisos 0o600. El *access token*
  vive solo en memoria y se refresca bajo un ``asyncio.Lock``.
- Ofrece un cliente REST autenticado (``solicitud`` y ``flujo``) sobre el ``httpx.AsyncClient``
  compartido: solo ``https://`` hacia ``HOSTS_PERMITIDOS``, nunca ``DELETE``, sin seguir
  redirecciones, un 401 → refresca una vez, y errores traducidos a :class:`ErrorGoogle`.
- Desconecta: revoca el permiso (200 o 400 = correcto) y **siempre** borra el archivo local.
- Publica ``google.estado`` (cuenta enmascarada) al iniciar y en cada cambio.

Dependencias: ``httpx`` (opcional: sin él, OFF). Sin librerías de Google (§2.4). DPAPI por
``ctypes`` solo en Windows.

Degradación: ``RAMS_GOOGLE=0`` u ``--offline`` → OFF; sin ``httpx`` → OFF; sin el JSON del cliente
→ EN ESPERA; cliente de tipo Web o roto → DEGRADADO; ``invalid_grant`` → DEGRADADO («reconecta»).

Privacidad (§15.5–§15.6, §6.5): nunca se registran tokens, ``code``, ``state``, ``verifier``,
``client_secret``, URLs con *query*, cuerpos ni la cuenta. Los logs llevan solo el método, la ruta
sin *query* (con los ids tapados), el estado HTTP y la razón. La API pública nunca lanza, salvo
``token_acceso``, ``solicitud`` y ``flujo``, que lanzan :class:`ErrorGoogle`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import functools
import hashlib
import hmac
import json as jsonlib
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse
import uuid
import webbrowser
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

try:  # opcional: sin httpx el módulo queda OFF
    import httpx
except ImportError:  # sin httpx: OFF («sin httpx»)
    httpx = None  # type: ignore[assignment]

from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo

if TYPE_CHECKING:  # solo para anotaciones (sin dependencia en tiempo de ejecución)
    from core.bus import BusEventos

__all__ = [
    "SCOPE_GMAIL_SEND", "SCOPE_DRIVE_READONLY", "SCOPE_DRIVE_FILE", "SCOPE_DRIVE",
    "URL_AUTORIZACION", "URL_TOKEN", "URL_REVOCAR", "URL_ABOUT", "HOSTS_PERMITIDOS",
    "TIMEOUT_CONEXION_S", "MARGEN_REFRESCO_S", "MAX_PETICION_CALLBACK", "MOTIVOS_GOOGLE",
    "CODIGOS_CONEXION", "CODIGOS_ERROR_GOOGLE", "METODOS_PERMITIDOS",
    "scopes_pedidos", "clasificar_scopes", "enmascarar_correo", "protector_por_defecto",
    "EstadoGoogle", "ResultadoConexion", "ErrorGoogle", "ProtectorToken", "ProtectorDPAPI",
    "ProtectorNinguno", "CuentaGoogle",
]

log = logging.getLogger("rams.google")

# ---------------------------------------------------------------------------
# §6.1 Constantes
# ---------------------------------------------------------------------------

SCOPE_GMAIL_SEND: Final = "https://www.googleapis.com/auth/gmail.send"
SCOPE_DRIVE_READONLY: Final = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_DRIVE_FILE: Final = "https://www.googleapis.com/auth/drive.file"
SCOPE_DRIVE: Final = "https://www.googleapis.com/auth/drive"
URL_AUTORIZACION: Final = "https://accounts.google.com/o/oauth2/v2/auth"
URL_TOKEN: Final = "https://oauth2.googleapis.com/token"
URL_REVOCAR: Final = "https://oauth2.googleapis.com/revoke"
URL_ABOUT: Final = "https://www.googleapis.com/drive/v3/about"
HOSTS_PERMITIDOS: Final = frozenset({"accounts.google.com", "oauth2.googleapis.com", "gmail.googleapis.com",
                                     "www.googleapis.com"})
TIMEOUT_CONEXION_S: Final = 300.0      # espera máxima del navegador
MARGEN_REFRESCO_S: Final = 120.0
MAX_PETICION_CALLBACK: Final = 8192
MOTIVOS_GOOGLE: Final = ("", "deshabilitado", "offline", "sin_httpx", "sin_cliente", "cliente_invalido",
                         "sin_conectar", "conectando", "token_invalido", "token_no_guardado")
CODIGOS_CONEXION: Final = ("conectado", "cancelado", "tiempo_agotado", "sin_cliente", "cliente_web",
                           "cliente_invalido", "denegado", "state_invalido", "canje_fallido", "red", "ya_en_curso",
                           "deshabilitado", "offline", "desconectado", "error")
CODIGOS_ERROR_GOOGLE: Final = ("desconectado", "token_invalido", "sin_permiso", "no_encontrado", "limite",
                               "politica", "red", "tiempo_agotado", "rechazado", "servidor", "demasiado_grande",
                               "no_descargable", "no_exportable", "almacenamiento_lleno", "error")
# Métodos que el cliente REST acepta. NINGÚN DELETE (§15.7): ni borrado definitivo, ni vaciar la
# papelera, ni quitar permisos; la revocación del OAuth es un POST.
METODOS_PERMITIDOS: Final = frozenset({"GET", "POST", "PATCH", "PUT"})

NOMBRE_MODULO: Final = "Cuenta de Google"
SCOPES_CONOCIDOS: Final = (SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE, SCOPE_DRIVE)
ESPERAS_REINTENTO: Final = (1.0, 2.0, 4.0)      # reintentos (refresco y solicitud con reintentar=True)
MAX_RETRY_AFTER_S: Final = 30.0
MAX_CLIENTE_JSON: Final = 64 * 1024
MAX_TOKEN_JSON: Final = 64 * 1024
MAX_CUERPO_ERROR: Final = 64 * 1024
PLAZO_LECTURA_CALLBACK_S: Final = 10.0
MAX_INTENTOS_STATE: Final = 3
PLAZO_CIERRE_SERVIDOR_S: Final = 5.0
VERSION_TOKEN: Final = 1
# Entropía adicional de DPAPI (liga el blob a esta aplicación, no solo al usuario).
_ENTROPIA_DPAPI: Final = b"R.A.M.S. BIO-CORE google_token v1"
_CRYPTPROTECT_UI_FORBIDDEN: Final = 0x1

_RE_RAZON: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_RE_CLIENT_ID: Final = re.compile(r"^[A-Za-z0-9._\-]{1,256}$")
_RE_SECRETO_CLIENTE: Final = re.compile(r"^[\x21-\x7e]{1,256}$")
_RE_CODIGO_AUTORIZACION: Final = re.compile(r"^[\x21-\x7e]{1,2048}$")
_RE_TOKEN: Final = re.compile(r"^[\x21-\x7e]{1,4096}$")
_RE_CORREO_CUENTA: Final = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
# Segmentos de ruta que se pueden registrar tal cual; el resto (ids de Drive, de mensajes…) → "{id}".
_SEGMENTOS_RUTA_LOG: Final = frozenset({
    "o", "oauth2", "v2", "v1", "v3", "auth", "token", "revoke", "gmail", "users", "me", "messages", "send",
    "upload", "drive", "files", "export", "permissions", "about",
})

_RAZONES_SIN_PERMISO: Final = frozenset({"insufficientPermissions", "appNotAuthorizedToFile",
                                         "insufficientFilePermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                                         "forbidden", "PERMISSION_DENIED"})
_RAZONES_POLITICA: Final = frozenset({"domainPolicy", "admin_policy_enforced"})
_RAZONES_LIMITE: Final = frozenset({"userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded",
                                    "sharingRateLimitExceeded", "RESOURCE_EXHAUSTED", "quotaExceeded"})
_RAZONES_ALMACENAMIENTO: Final = frozenset({"storageQuotaExceeded"})
_RAZONES_NO_DESCARGABLE: Final = frozenset({"fileNotDownloadable", "cannotDownloadAbusiveFile"})
_RAZONES_NO_EXPORTABLE: Final = frozenset({"fileNotExportable", "exportSizeLimitExceeded"})

_MENSAJES_ERROR: Final[Mapping[str, str]] = {
    "desconectado": "Primero conecta tu cuenta de Google (di «conecta mi cuenta de Google»).",
    "token_invalido": "Tu permiso de Google caducó o se revocó: di «conecta mi cuenta de Google».",
    "sin_permiso": "Google dice que falta permiso para eso.",
    "no_encontrado": "Google no lo encontró (no existe o no tienes acceso).",
    "limite": "Google limitó las peticiones: espera un rato antes de volver a intentarlo.",
    "politica": "La política de tu cuenta de Google no lo permite.",
    "red": "No hay conexión con Google.",
    "tiempo_agotado": "Google tardó demasiado en responder.",
    "rechazado": "Google rechazó la petición.",
    "servidor": "Google tuvo un error interno: inténtalo más tarde.",
    "demasiado_grande": "Es demasiado grande para Google.",
    "no_descargable": "Ese archivo no se puede descargar directamente.",
    "no_exportable": "Es demasiado grande para exportarlo; ábrelo en el navegador.",
    "almacenamiento_lleno": "Tu Drive está lleno.",
    "error": "Algo falló al hablar con Google.",
}

_MENSAJES_CONEXION: Final[Mapping[str, str]] = {
    "cancelado": "La conexión con Google se canceló.",
    "tiempo_agotado": "La conexión con Google se canceló (no llegó la autorización).",
    "sin_cliente": ("Me falta el archivo del cliente OAuth: sigue la guía (docs/V022_CORREO_DRIVE_SCRIPTS.md "
                    "§17) y guárdalo como data/google_cliente.json."),
    "cliente_web": "Creaste un cliente de tipo Web; crea uno de tipo App de escritorio.",
    "cliente_invalido": ("El archivo del cliente OAuth (data/google_cliente.json) no es válido: descárgalo "
                         "otra vez desde Google Cloud."),
    "denegado": "No diste el permiso en Google (o tu cuenta no permite apps no verificadas): no conecté nada.",
    "state_invalido": "La respuesta que llegó del navegador no era válida: no conecté nada. Inténtalo de nuevo.",
    "canje_fallido": "Google no aceptó la autorización: inténtalo de nuevo.",
    "red": "No pude hablar con Google (¿sin conexión?): inténtalo de nuevo.",
    "ya_en_curso": "Ya hay una conexión con Google en curso: termina en el navegador.",
    "deshabilitado": ("La conexión con Google está apagada: pon RAMS_GOOGLE=1 en el .env, sigue la guía "
                      "y reinicia."),
    "offline": "Estoy en modo sin conexión (--offline): no puedo conectar Google.",
    "error": "Algo falló al conectar con Google.",
}

_HTML_LISTO: Final = ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\"><title>Amadeus</title>"
                      "</head><body><p>Listo. Ya puedes cerrar esta pestaña y volver con Amadeus.</p></body></html>")
_HTML_CANCELADO: Final = ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\"><title>Amadeus</title>"
                          "</head><body><p>No se concedió el permiso. Puedes cerrar esta pestaña.</p></body></html>")
_HTML_TERMINADO: Final = ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\"><title>Amadeus</title>"
                          "</head><body><p>Esta conexión ya terminó. Puedes cerrar esta pestaña.</p></body></html>")


def scopes_pedidos(settings: Settings) -> tuple[str, ...]:
    """gmail.send si "correo" ∈ google_servicios; si "drive": (drive) con drive_completo, si no
    (drive.readonly, drive.file). Orden estable. Vacío si no hay servicios."""
    try:
        servicios = tuple(getattr(settings, "google_servicios", ()) or ())
    except TypeError as exc:
        log.debug("google_servicios no iterable (%s): sin scopes", type(exc).__name__)
        return ()
    scopes: list[str] = []
    if "correo" in servicios:
        scopes.append(SCOPE_GMAIL_SEND)
    if "drive" in servicios:
        if bool(getattr(settings, "drive_completo", False)):
            scopes.append(SCOPE_DRIVE)
        else:
            scopes.extend((SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE))
    return tuple(scopes)


_DRIVE_ESTRECHOS: Final = (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)
_NOMBRES_SCOPE: Final[Mapping[str, str]] = {
    SCOPE_GMAIL_SEND: "Gmail", SCOPE_DRIVE: "Drive completo", SCOPE_DRIVE_READONLY: "Drive (leer)",
    SCOPE_DRIVE_FILE: "Drive (archivos propios)",
}


def clasificar_scopes(concedidos: Sequence[str], pedidos: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(usables, de_mas) de los scopes que tiene un token frente a los que pide la configuración (§0.1, §15.4).

    - Se usa un scope concedido si se pidió, o si es más estrecho que uno pedido (``drive.readonly``/``drive.file``
      con ``drive`` pedido: un token así puede menos de lo configurado, nunca más).
    - ``de_mas``: concedidos que la configuración no pide (p. ej. ``drive`` con ``RAMS_DRIVE_COMPLETO=0``, o tras
      quitar un servicio). Nunca se usan. Si sobra ``drive``, Drive entero queda sin usar: con ese token cualquier
      llamada a Drive tiene el poder completo, aunque el token traiga también los scopes estrechos.
    """
    pedidos_set = set(pedidos)
    validos = [s for s in SCOPES_CONOCIDOS if s in set(concedidos)]
    drive_de_mas = SCOPE_DRIVE in validos and SCOPE_DRIVE not in pedidos_set
    usables: list[str] = []
    de_mas: list[str] = []
    for scope in validos:
        if not (scope in pedidos_set or (scope in _DRIVE_ESTRECHOS and SCOPE_DRIVE in pedidos_set)):
            de_mas.append(scope)
        elif not (drive_de_mas and scope in _DRIVE_ESTRECHOS):
            usables.append(scope)
    return tuple(usables), tuple(de_mas)


def enmascarar_correo(correo: str | None) -> str | None:
    """``"juan.perez@gmail.com"`` → ``"ju***@gmail.com"``. ``None`` o vacío ⇒ ``None``; sin «@» ⇒ ``"***"``."""
    if not isinstance(correo, str) or not correo.strip():
        return None
    local, arroba, dominio = correo.strip().rpartition("@")
    if not arroba or not local or not dominio:
        return "***"
    return f"{local[:2]}***@{dominio}"


# ---------------------------------------------------------------------------
# §6.2 Tipos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EstadoGoogle:
    """Estado de la conexión con Google (lo que se publica en ``google.estado``, enmascarado)."""

    conectado: bool
    correo: bool            # gmail.send concedido
    drive_leer: bool        # drive.readonly o drive
    drive_escribir: bool    # drive.file o drive
    drive_completo: bool    # drive
    cuenta: str | None      # correo completo (solo local; en el bus va enmascarado)
    motivo: str             # ∈ MOTIVOS_GOOGLE

    def a_dict(self) -> dict[str, Any]:
        """Payload de google.estado: {conectado, correo, drive_leer, drive_escribir, drive_completo,
        cuenta: enmascarar_correo(cuenta) | None, motivo}."""
        return {"conectado": bool(self.conectado), "correo": bool(self.correo),
                "drive_leer": bool(self.drive_leer), "drive_escribir": bool(self.drive_escribir),
                "drive_completo": bool(self.drive_completo), "cuenta": enmascarar_correo(self.cuenta),
                "motivo": self.motivo if self.motivo in MOTIVOS_GOOGLE else ""}

    def __repr__(self) -> str:  # la cuenta completa nunca sale en un repr
        return (f"EstadoGoogle(conectado={self.conectado}, correo={self.correo}, drive_leer={self.drive_leer}, "
                f"drive_escribir={self.drive_escribir}, drive_completo={self.drive_completo}, "
                f"cuenta={enmascarar_correo(self.cuenta)!r}, motivo={self.motivo!r})")


@dataclass(frozen=True)
class ResultadoConexion:
    """Resultado de :meth:`CuentaGoogle.conectar` / :meth:`CuentaGoogle.desconectar`."""

    ok: bool
    codigo: str                       # ∈ CODIGOS_CONEXION
    mensaje: str                      # español, para el usuario (sin secretos ni URL)
    concedidos: tuple[str, ...] = ()  # scopes concedidos de verdad (campo "scope" de la respuesta)
    faltan: tuple[str, ...] = ()      # pedidos y no concedidos (consentimiento granular)


class ErrorGoogle(Exception):
    """Error de una llamada a Google, ya traducido. ``codigo`` ∈ ``CODIGOS_ERROR_GOOGLE``.

    ``mensaje`` va en español y sin datos; ``razon`` es la razón de Google (``insufficientPermissions``…),
    sin datos del usuario. La excepción de transporte original (si la hubo) queda en ``__cause__``.
    """

    def __init__(self, codigo: str, mensaje: str, *, http: int | None = None, razon: str | None = None) -> None:
        codigo = codigo if codigo in CODIGOS_ERROR_GOOGLE else "error"
        mensaje = mensaje if isinstance(mensaje, str) and mensaje else _MENSAJES_ERROR[codigo]
        super().__init__(mensaje)
        self.codigo: str = codigo
        self.mensaje: str = mensaje
        self.http: int | None = http if isinstance(http, int) else None
        self.razon: str | None = razon if isinstance(razon, str) and _RE_RAZON.fullmatch(razon) else None

    def __repr__(self) -> str:
        return f"ErrorGoogle(codigo={self.codigo!r}, http={self.http!r}, razon={self.razon!r})"


class ProtectorToken(Protocol):
    """Cifra y descifra el refresh token antes de ir a disco."""

    nombre: str                                  # "dpapi" | "ninguno"

    def proteger(self, datos: bytes) -> bytes: ...

    def desproteger(self, datos: bytes) -> bytes: ...


class ProtectorNinguno:
    """Sin cifrado: el archivo queda con permisos 0o600 (Linux y el resto de sistemas)."""

    nombre = "ninguno"

    def proteger(self, datos: bytes) -> bytes:
        return bytes(datos)

    def desproteger(self, datos: bytes) -> bytes:
        return bytes(datos)


@functools.lru_cache(maxsize=1)
def _api_dpapi() -> Any:
    """Funciones de DPAPI (crypt32/kernel32) con sus firmas, en instancias propias de ``WinDLL``.

    Se usan instancias propias para no tocar las firmas globales de ``ctypes.windll``. Solo Windows.
    """
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    proteger = crypt32.CryptProtectData
    proteger.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    proteger.restype = wintypes.BOOL
    desproteger = crypt32.CryptUnprotectData
    desproteger.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    desproteger.restype = wintypes.BOOL
    liberar = kernel32.LocalFree
    liberar.argtypes = [ctypes.c_void_p]
    liberar.restype = ctypes.c_void_p

    class Api:
        pass

    api = Api()
    api.ctypes = ctypes  # type: ignore[attr-defined]
    api.Blob = Blob  # type: ignore[attr-defined]
    api.proteger = proteger  # type: ignore[attr-defined]
    api.desproteger = desproteger  # type: ignore[attr-defined]
    api.liberar = liberar  # type: ignore[attr-defined]
    return api


def _dpapi(datos: bytes, *, cifrar: bool) -> bytes:
    """``CryptProtectData``/``CryptUnprotectData`` (ámbito del usuario actual, sin interfaz)."""
    if sys.platform != "win32":
        raise OSError("DPAPI solo existe en Windows")
    datos = bytes(datos)
    if not datos:
        raise ValueError("nada que proteger")
    api = _api_dpapi()
    ctypes = api.ctypes
    tipo_puntero = ctypes.POINTER(ctypes.c_char)
    bufer = ctypes.create_string_buffer(datos, len(datos))
    entrada = api.Blob(len(datos), ctypes.cast(bufer, tipo_puntero))
    bufer_entropia = ctypes.create_string_buffer(_ENTROPIA_DPAPI, len(_ENTROPIA_DPAPI))
    entropia = api.Blob(len(_ENTROPIA_DPAPI), ctypes.cast(bufer_entropia, tipo_puntero))
    salida = api.Blob()
    if cifrar:
        ok = api.proteger(ctypes.byref(entrada), "RAMS", ctypes.byref(entropia), None, None,
                          _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(salida))
    else:
        ok = api.desproteger(ctypes.byref(entrada), None, ctypes.byref(entropia), None, None,
                             _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(salida))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI falló")
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        api.liberar(salida.pbData)


class ProtectorDPAPI:
    """DPAPI de Windows (``crypt32.CryptProtectData``/``CryptUnprotectData`` vía ctypes)."""

    nombre = "dpapi"

    def proteger(self, datos: bytes) -> bytes:
        return _dpapi(datos, cifrar=True)

    def desproteger(self, datos: bytes) -> bytes:
        return _dpapi(datos, cifrar=False)


def protector_por_defecto() -> ProtectorToken:
    """win32 → DPAPI (crypt32.CryptProtectData/CryptUnprotectData vía ctypes, ámbito del usuario actual);
    otro sistema → "ninguno" (el archivo queda con 0o600)."""
    if sys.platform == "win32":
        return ProtectorDPAPI()
    return ProtectorNinguno()


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _ruta_segura(url: str) -> str:
    """Ruta de ``url`` sin *query* y con los segmentos desconocidos (ids) como ``{id}``: para los logs."""
    try:
        ruta = urllib.parse.urlsplit(str(url)).path
    except ValueError:
        return "?"
    segmentos = [s if s in _SEGMENTOS_RUTA_LOG else "{id}" for s in ruta.split("/") if s]
    return "/" + "/".join(segmentos)


def _validar_url(url: Any) -> str:
    """``url`` si es ``https://`` hacia ``HOSTS_PERMITIDOS`` (sin usuario ni puerto raro); si no, lanza."""
    if not isinstance(url, str) or len(url) > 8192:
        raise ErrorGoogle("error", "Destino de Google no permitido.")
    try:
        partes = urllib.parse.urlsplit(url)
        puerto = partes.port
    except ValueError as exc:
        raise ErrorGoogle("error", "Destino de Google no permitido.") from exc
    host = (partes.hostname or "").lower()
    if (partes.scheme != "https" or host not in HOSTS_PERMITIDOS or partes.username is not None
            or partes.password is not None or puerto not in (None, 443) or partes.fragment):
        raise ErrorGoogle("error", "Destino de Google no permitido.")
    return url


def _razones(datos: Any) -> list[str]:
    """Razones de un cuerpo de error de Google (OAuth: ``{"error": "invalid_grant"}``; APIs: ``errors[].reason``,
    ``details[].reason`` y ``status``)."""
    razones: list[str] = []
    if not isinstance(datos, Mapping):
        return razones
    error = datos.get("error")
    if isinstance(error, str):
        razones.append(error)
    elif isinstance(error, Mapping):
        for lista in (error.get("errors"), error.get("details")):
            if isinstance(lista, list):
                for item in lista[:10]:
                    if isinstance(item, Mapping) and isinstance(item.get("reason"), str):
                        razones.append(item["reason"])
        if isinstance(error.get("status"), str):
            razones.append(error["status"])
    return [r for r in razones if _RE_RAZON.fullmatch(r)]


def _error_de_estado(estado: int, razones: list[str]) -> ErrorGoogle:
    """Traduce un estado HTTP ≥ 400 y sus razones a :class:`ErrorGoogle` (§6.5)."""
    conjunto = set(razones)
    razon = razones[0] if razones else None
    if estado == 401:
        codigo = "token_invalido"
    elif conjunto & _RAZONES_POLITICA:
        codigo = "politica"
    elif conjunto & _RAZONES_LIMITE or estado == 429:
        codigo = "limite"
    elif conjunto & _RAZONES_ALMACENAMIENTO:
        codigo = "almacenamiento_lleno"
    elif conjunto & _RAZONES_NO_DESCARGABLE:
        codigo = "no_descargable"
    elif conjunto & _RAZONES_NO_EXPORTABLE:
        codigo = "no_exportable"
    elif conjunto & _RAZONES_SIN_PERMISO or estado == 403:
        codigo = "sin_permiso"
    elif estado == 404:
        codigo = "no_encontrado"
    elif estado == 408:
        codigo = "tiempo_agotado"
    elif estado == 413:
        codigo = "demasiado_grande"
    elif estado >= 500:
        codigo = "servidor"
    elif 400 <= estado < 500:
        codigo = "rechazado"
    else:
        codigo = "error"
    for preferida in (_RAZONES_POLITICA, _RAZONES_LIMITE, _RAZONES_ALMACENAMIENTO, _RAZONES_NO_DESCARGABLE,
                      _RAZONES_NO_EXPORTABLE, _RAZONES_SIN_PERMISO):
        coincidencias = [r for r in razones if r in preferida]
        if coincidencias:
            razon = coincidencias[0]
            break
    return ErrorGoogle(codigo, _MENSAJES_ERROR[codigo], http=estado, razon=razon)


def _error_de_transporte(exc: BaseException) -> ErrorGoogle:
    """Traduce una excepción de httpx: conexión → ``red``; tiempo → ``tiempo_agotado``; resto → ``red``/``error``."""
    if httpx is not None:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return ErrorGoogle("red", _MENSAJES_ERROR["red"])
        if isinstance(exc, httpx.TimeoutException):
            return ErrorGoogle("tiempo_agotado", _MENSAJES_ERROR["tiempo_agotado"])
        if isinstance(exc, httpx.TransportError):
            return ErrorGoogle("red", _MENSAJES_ERROR["red"])
    return ErrorGoogle("error", _MENSAJES_ERROR["error"])


def _json_de(respuesta: Any) -> Any:
    """JSON de una respuesta (``None`` si no lo es); nunca lanza."""
    try:
        return respuesta.json()
    except Exception as exc:  # cuerpo vacío, no JSON o codificación rara
        log.debug("Respuesta de Google sin JSON (%s)", type(exc).__name__)
        return None


def _segundos_retry_after(respuesta: Any) -> float | None:
    """``Retry-After`` en segundos (acotado a ``MAX_RETRY_AFTER_S``) o ``None``."""
    try:
        valor = respuesta.headers.get("Retry-After")
        if valor is None:
            return None
        segundos = float(str(valor).strip())
    except (ValueError, AttributeError, TypeError):
        return None
    if segundos != segundos or segundos < 0:  # NaN o negativo
        return None
    return min(segundos, MAX_RETRY_AFTER_S)


def _escribir_privado_atomico(ruta: Path, contenido: bytes) -> None:
    """Escritura atómica (``.tmp`` en el mismo directorio + ``os.replace``) con modo 0o600 (síncrona)."""
    directorio = ruta.parent
    directorio.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporal = directorio / f".{ruta.name}.{uuid.uuid4().hex[:8]}.tmp"
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as archivo:
            archivo.write(contenido)
            archivo.flush()
            os.fsync(archivo.fileno())
        if os.name == "posix":
            os.chmod(temporal, 0o600)
        os.replace(temporal, ruta)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(ruta, 0o600)


def _leer_json_acotado(ruta: Path, maximo: int) -> tuple[str, Any]:
    """(``"ok"``, datos) | (``"falta"``, None) | (``"invalido"``, None). Síncrona; nunca registra el contenido."""
    try:
        tamano = ruta.stat().st_size
    except FileNotFoundError:
        return "falta", None
    except OSError as exc:
        log.debug("No se pudo leer un archivo de Google (%s)", type(exc).__name__)
        return "invalido", None
    if tamano > maximo:
        return "invalido", None
    try:
        crudo = ruta.read_bytes()[: maximo + 1]
        return "ok", jsonlib.loads(crudo.decode("utf-8-sig"))
    except FileNotFoundError:
        return "falta", None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        log.debug("Archivo de Google ilegible (%s)", type(exc).__name__)
        return "invalido", None


def _sha12(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# §6.3 Clase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cliente:
    """Cliente OAuth leído del JSON (solo en memoria; nunca se registra)."""

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return "_Cliente(«oculto»)"


class CuentaGoogle:
    """Conexión con la cuenta de Google del usuario (OAuth de escritorio) y cliente REST autenticado."""

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 cliente: httpx.AsyncClient | None = None,
                 abrir_navegador: Callable[[str], bool] | None = None,   # defecto: webbrowser.open (en to_thread)
                 reloj: Callable[[], float] = time.time,
                 protector: ProtectorToken | None = None) -> None:
        self._settings = settings
        self._bus = bus
        self._cliente_http = cliente
        self._cliente_propio = False
        self._abrir_navegador = abrir_navegador
        self._reloj = reloj
        self._protector: ProtectorToken = protector if protector is not None else protector_por_defecto()
        self._dormir: Callable[[float], Any] = asyncio.sleep   # los tests lo sustituyen
        # Estado (solo se modifica desde el loop).
        self._cliente_oauth: _Cliente | None = None
        self._estado_cliente = "sin_leer"     # "sin_leer" | "ok" | "sin_cliente" | "cliente_web" | "cliente_invalido"
        self._refresh: str | None = None
        self._acceso: str | None = None
        self._acceso_expira = 0.0
        self._scopes: tuple[str, ...] = ()
        self._cuenta: str | None = None
        self._token_invalido = False
        self._token_guardado = True
        self._client_id_sha: str | None = None
        self._futuro_conexion: asyncio.Future[tuple[str, str | None]] | None = None
        self._conectando = False
        self._cerrado = False
        self._candados: dict[int, asyncio.Lock] = {}

    def __repr__(self) -> str:
        return f"CuentaGoogle(conectado={self._conectado()}, motivo={self._motivo()!r})"

    # ------------------------------------------------------------------ estado

    def _conectado(self) -> bool:
        return (self._refresh is not None and not self._token_invalido and self._estado_cliente == "ok"
                and bool(getattr(self._settings, "tiene_google", False)) and httpx is not None)

    def _motivo(self) -> str:
        s = self._settings
        if not bool(getattr(s, "google_habilitado", False)):
            return "deshabilitado"
        if bool(getattr(s, "offline", False)):
            return "offline"
        if httpx is None:
            return "sin_httpx"
        if self._conectado():
            return "" if self._token_guardado else "token_no_guardado"
        if self._estado_cliente in ("cliente_web", "cliente_invalido"):
            return "cliente_invalido"
        if self._estado_cliente == "sin_cliente":
            return "sin_cliente"
        if self._token_invalido:
            return "token_invalido"
        if self._conectando:
            return "conectando"
        return "sin_conectar"

    def _scopes_usables(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(usables, de_mas) del token cargado frente a la configuración actual (:func:`clasificar_scopes`). El
        token puede ser de antes de un cambio del ``.env`` (p. ej. conectado con ``RAMS_DRIVE_COMPLETO=1``)."""
        return clasificar_scopes(self._scopes, scopes_pedidos(self._settings))

    def estado(self) -> EstadoGoogle:
        """Estado actual (sin red). Los permisos solo cuentan si está conectada, y solo los que la configuración
        actual pide (un scope de más nunca se usa: ver :func:`clasificar_scopes`)."""
        conectado = self._conectado()
        scopes = set(self._scopes_usables()[0]) if conectado else set()
        completo = SCOPE_DRIVE in scopes
        return EstadoGoogle(
            conectado=conectado,
            correo=SCOPE_GMAIL_SEND in scopes,
            drive_leer=completo or SCOPE_DRIVE_READONLY in scopes,
            drive_escribir=completo or SCOPE_DRIVE_FILE in scopes,
            drive_completo=completo,
            cuenta=self._cuenta if conectado else None,
            motivo=self._motivo(),
        )

    @property
    def disponible(self) -> bool:
        """Conectada y con Google permitido por configuración (``tiene_google``)."""
        return self._conectado()

    def tiene_scope(self, scope: str) -> bool:
        """``True`` si está conectada y ``scope`` se concedió y la configuración actual lo pide (o pide uno más amplio
        que lo incluye)."""
        return self._conectado() and scope in self._scopes_usables()[0]

    @property
    def conectando(self) -> bool:
        """Hay una conexión OAuth en curso (esperando al navegador)."""
        return self._conectando

    def _publicar_estado(self) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publicar("google.estado", self.estado().a_dict(), origen="google")
        except Exception as exc:  # el bus nunca debe tumbar la cuenta
            log.debug("No se pudo publicar google.estado (%s)", type(exc).__name__)

    def _candado(self) -> asyncio.Lock:
        """``asyncio.Lock`` del loop actual (los tests corren varios ``asyncio.run`` con la misma cuenta)."""
        loop = asyncio.get_running_loop()
        clave = id(loop)
        candado = self._candados.get(clave)
        if candado is None:
            self._candados = {clave: asyncio.Lock()}
            candado = self._candados[clave]
        return candado

    def _obtener_cliente(self) -> httpx.AsyncClient:
        """Cliente compartido o, si no se recibió, uno propio (se cierra en :meth:`cerrar`)."""
        if httpx is None:
            raise ErrorGoogle("error", "Falta httpx para hablar con Google.")
        if self._cliente_http is None:
            self._cliente_http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0),
                                                   headers={"User-Agent": "RAMS-BioCore/0.1"})
            self._cliente_propio = True
        return self._cliente_http

    # ------------------------------------------------------------------ ciclo de vida

    async def iniciar(self) -> DiagnosticoModulo:
        """Carga el cliente y el token (``to_thread``), **sin red**, y publica ``google.estado``."""
        try:
            if bool(getattr(self._settings, "tiene_google", False)) and httpx is not None:
                cliente, estado_cliente, token = await asyncio.to_thread(self._cargar_sync)
                self._cliente_oauth = cliente
                self._estado_cliente = estado_cliente
                self._aplicar_token_cargado(token)
        except Exception as exc:  # contrato: iniciar nunca lanza
            log.warning("No se pudo cargar la cuenta de Google (%s)", type(exc).__name__)
        self._publicar_estado()
        diagnostico = self.diagnostico()
        log.info("Cuenta de Google: %s %s", diagnostico.estado.value, self._motivo() or "conectada")
        return diagnostico

    async def releer_cliente(self) -> str:
        """Vuelve a leer el JSON del cliente y el token (``to_thread``, **sin red**) si la cuenta no está conectada
        ni conectándose, y devuelve el motivo actual. Nunca lanza (salvo la cancelación).

        ``iniciar`` solo los lee al arrancar: sin esto, guardar ``data/google_cliente.json`` (o cambiar un cliente
        Web por uno de escritorio) con R.A.M.S. en marcha dejaba el estado en ``sin_cliente`` /
        ``cliente_invalido`` hasta reiniciar. Publica ``google.estado`` solo si el estado cambió.
        """
        if self._cerrado or self._conectando or self._conectado():
            return self._motivo()
        if not bool(getattr(self._settings, "tiene_google", False)) or httpx is None:
            return self._motivo()
        antes = self.estado()
        try:
            cliente, estado_cliente, token = await asyncio.to_thread(self._cargar_sync)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # contrato: nunca lanza
            log.warning("No se pudo releer el cliente de Google (%s)", type(exc).__name__)
            return self._motivo()
        if self._conectando or self._conectado() or self._cerrado:   # otra corrutina se adelantó
            return self._motivo()
        self._cliente_oauth = cliente
        self._estado_cliente = estado_cliente
        self._aplicar_token_cargado(token)
        if self.estado() != antes:
            log.info("Cuenta de Google: el cliente o el token cambiaron en disco (%s)", self._motivo() or "conectada")
            self._publicar_estado()
        return self._motivo()

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico ``google`` (§6.6)."""
        motivo = self._motivo()
        if motivo == "deshabilitado":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.OFF, "deshabilitado (RAMS_GOOGLE=0)")
        if motivo == "offline":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.OFF, "--offline")
        if motivo == "sin_httpx":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.OFF, "sin httpx")
        if motivo == "sin_cliente":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.EN_ESPERA,
                                     "falta data/google_cliente.json (guía en docs/V022_CORREO_DRIVE_SCRIPTS.md §17)")
        if motivo == "sin_conectar":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.EN_ESPERA,
                                     "sin conectar: di «conecta mi cuenta de Google»")
        if motivo == "conectando":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.EN_ESPERA,
                                     "conectando: esperando la autorización en el navegador")
        if motivo == "cliente_invalido":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "cliente OAuth inválido (¿tipo Web?)")
        if motivo == "token_invalido":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "reconecta: el permiso caducó o se revocó")
        estado = self.estado()
        partes: list[str] = []
        cuenta = enmascarar_correo(estado.cuenta)
        if cuenta:
            partes.append(cuenta)
        if estado.correo:
            partes.append("Gmail")
        if estado.drive_completo:
            partes.append("Drive completo")
        elif estado.drive_leer and estado.drive_escribir:
            partes.append("Drive (leer + archivos propios)")
        elif estado.drive_leer:
            partes.append("Drive (solo leer)")
        elif estado.drive_escribir:
            partes.append("Drive (solo archivos propios)")
        _, de_mas = self._scopes_usables()
        if de_mas:
            sobran = ", ".join(dict.fromkeys(_NOMBRES_SCOPE.get(s, "otro") for s in de_mas))
            servicios = partes[1:] if cuenta else partes
            en_uso = (" · en uso: " + " · ".join(servicios)) if servicios else ""
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     f"el permiso guardado es más amplio que el configurado ({sobran}, sin usar): "
                                     f"desconecta y vuelve a conectar Google{en_uso}")
        if not (estado.correo or estado.drive_leer or estado.drive_escribir):
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "conectada sin permisos: reconecta y marca las casillas")
        if motivo == "token_no_guardado":
            return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "token sin guardar (DPAPI falló): " + " · ".join(partes))
        return DiagnosticoModulo("google", NOMBRE_MODULO, EstadoModulo.OK, " · ".join(partes))

    async def cerrar(self) -> None:
        """Cancela una conexión en curso y cierra el cliente propio. **No** revoca nada."""
        self._cerrado = True
        self.cancelar_conexion()
        self._acceso = None
        self._acceso_expira = 0.0
        if self._cliente_propio and self._cliente_http is not None:
            cliente, self._cliente_http = self._cliente_http, None
            self._cliente_propio = False
            try:
                await cliente.aclose()
            except Exception as exc:  # cerrar nunca lanza
                log.debug("Error al cerrar el cliente HTTP de Google (%s)", type(exc).__name__)

    # ------------------------------------------------------------------ archivos (síncronos, en to_thread)

    def _leer_cliente_sync(self) -> tuple[_Cliente | None, str]:
        """(cliente, estado) con estado ∈ {"ok", "sin_cliente", "cliente_web", "cliente_invalido"}."""
        resultado, datos = _leer_json_acotado(Path(self._settings.google_cliente_path), MAX_CLIENTE_JSON)
        if resultado == "falta":
            return None, "sin_cliente"
        if resultado != "ok" or not isinstance(datos, Mapping):
            return None, "cliente_invalido"
        instalado = datos.get("installed")
        if not isinstance(instalado, Mapping):
            return None, "cliente_web" if isinstance(datos.get("web"), Mapping) else "cliente_invalido"
        client_id = instalado.get("client_id")
        client_secret = instalado.get("client_secret")
        if (not isinstance(client_id, str) or not _RE_CLIENT_ID.fullmatch(client_id)
                or not isinstance(client_secret, str) or not _RE_SECRETO_CLIENTE.fullmatch(client_secret)):
            return None, "cliente_invalido"
        return _Cliente(client_id, client_secret), "ok"

    def _leer_token_sync(self) -> dict[str, Any] | None:
        """Token guardado ya descifrado (``refresh_token`` en claro, solo en memoria) o ``None``."""
        resultado, datos = _leer_json_acotado(Path(self._settings.google_token_path), MAX_TOKEN_JSON)
        if resultado == "falta":
            return None
        if resultado != "ok" or not isinstance(datos, Mapping) or datos.get("version") != VERSION_TOKEN:
            log.warning("El archivo del token de Google no es válido: reconecta la cuenta")
            return {"invalido": True}
        cifrado = datos.get("cifrado")
        crudo = datos.get("refresh_token")
        scopes = datos.get("scopes")
        cuenta = datos.get("cuenta")
        token: dict[str, Any] = {
            "scopes": tuple(s for s in scopes if s in SCOPES_CONOCIDOS) if isinstance(scopes, list) else (),
            "cuenta": cuenta if isinstance(cuenta, str) and _RE_CORREO_CUENTA.fullmatch(cuenta) else None,
            "client_id_sha": datos.get("client_id_sha") if isinstance(datos.get("client_id_sha"), str) else None,
            "invalido": bool(datos.get("invalido", False)),
            "refresh_token": None,
        }
        if token["invalido"] or not isinstance(crudo, str) or not crudo:
            token["invalido"] = True
            return token
        if cifrado != getattr(self._protector, "nombre", None):
            log.warning("El token de Google se guardó con otro cifrado: reconecta la cuenta")
            token["invalido"] = True
            return token
        try:
            if cifrado == "ninguno":
                refresh = crudo
            else:
                refresh = self._protector.desproteger(base64.b64decode(crudo, validate=True)).decode("utf-8")
        except (binascii.Error, ValueError, OSError, UnicodeDecodeError) as exc:
            log.warning("No se pudo descifrar el token de Google (%s): reconecta la cuenta", type(exc).__name__)
            token["invalido"] = True
            return token
        except Exception as exc:  # protector inyectado con otros errores
            log.warning("No se pudo descifrar el token de Google (%s): reconecta la cuenta", type(exc).__name__)
            token["invalido"] = True
            return token
        if not _RE_TOKEN.fullmatch(refresh):
            token["invalido"] = True
            return token
        token["refresh_token"] = refresh
        return token

    def _cargar_sync(self) -> tuple[_Cliente | None, str, dict[str, Any] | None]:
        cliente, estado_cliente = self._leer_cliente_sync()
        return cliente, estado_cliente, self._leer_token_sync()

    def _aplicar_token_cargado(self, token: dict[str, Any] | None) -> None:
        self._refresh = None
        self._acceso = None
        self._acceso_expira = 0.0
        self._token_invalido = False
        self._token_guardado = True
        if token is None:
            self._scopes, self._cuenta, self._client_id_sha = (), None, None
            return
        self._scopes = tuple(token.get("scopes") or ())
        self._cuenta = token.get("cuenta")
        self._client_id_sha = token.get("client_id_sha")
        if token.get("invalido") or not token.get("refresh_token"):
            self._token_invalido = True
            return
        if (self._cliente_oauth is not None and self._client_id_sha is not None
                and not hmac.compare_digest(self._client_id_sha, _sha12(self._cliente_oauth.client_id))):
            log.warning("El token de Google es de otro cliente OAuth: reconecta la cuenta")
            self._token_invalido = True
            return
        self._refresh = token["refresh_token"]

    def _guardar_token_sync(self, refresh: str, scopes: tuple[str, ...], cuenta: str | None, client_id: str,
                            conectado_ts: float, refresh_expira_ts: float | None) -> bool:
        """Guarda el token de forma atómica. ``False`` si el protector falla (no se escribe nada)."""
        nombre = getattr(self._protector, "nombre", "ninguno")
        try:
            if nombre == "ninguno":
                guardado = refresh
            else:
                guardado = base64.b64encode(self._protector.proteger(refresh.encode("utf-8"))).decode("ascii")
        except Exception as exc:  # DPAPI u otro protector falló: no se guarda (token_no_guardado)
            log.warning("No se pudo cifrar el token de Google (%s): la sesión vale solo en memoria",
                        type(exc).__name__)
            return False
        datos = {"version": VERSION_TOKEN, "cifrado": nombre, "refresh_token": guardado,
                 "scopes": list(scopes), "cuenta": cuenta, "client_id_sha": _sha12(client_id),
                 "conectado_ts": float(conectado_ts), "refresh_expira_ts": refresh_expira_ts}
        _escribir_privado_atomico(Path(self._settings.google_token_path),
                                  jsonlib.dumps(datos, ensure_ascii=False).encode("utf-8"))
        return True

    def _marcar_token_invalido_sync(self) -> None:
        """Conserva el archivo (para el diagnóstico) pero sin el refresh token y marcado como inválido."""
        ruta = Path(self._settings.google_token_path)
        resultado, datos = _leer_json_acotado(ruta, MAX_TOKEN_JSON)
        if resultado != "ok" or not isinstance(datos, Mapping):
            return
        nuevo = dict(datos)
        nuevo["refresh_token"] = ""
        nuevo["invalido"] = True
        _escribir_privado_atomico(ruta, jsonlib.dumps(nuevo, ensure_ascii=False).encode("utf-8"))

    def _borrar_token_sync(self) -> None:
        ruta = Path(self._settings.google_token_path)
        try:
            ruta.unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------ conectar (§6.4)

    def cancelar_conexion(self) -> bool:
        """Cancela la conexión en curso (``conectar`` devuelve ``cancelado``). ``True`` si había una."""
        futuro = self._futuro_conexion
        if futuro is not None and not futuro.done():
            try:
                futuro.set_result(("cancelado", None))
            except asyncio.InvalidStateError:
                return False
            return True
        return False

    def _resultado(self, codigo: str, mensaje: str | None = None) -> ResultadoConexion:
        codigo = codigo if codigo in CODIGOS_CONEXION else "error"
        texto = mensaje or _MENSAJES_CONEXION.get(codigo, _MENSAJES_CONEXION["error"])
        return ResultadoConexion(ok=False, codigo=codigo, mensaje=texto)

    async def conectar(self, *, timeout_s: float = TIMEOUT_CONEXION_S,
                       mostrar_url: Callable[[str], None] | None = None) -> ResultadoConexion:
        """Conecta la cuenta con OAuth de escritorio (navegador del sistema + loopback). Nunca lanza."""
        s = self._settings
        if not bool(getattr(s, "google_habilitado", False)):
            return self._resultado("deshabilitado")
        if bool(getattr(s, "offline", False)):
            return self._resultado("offline")
        if httpx is None:
            return self._resultado("error", "Falta httpx para hablar con Google.")
        if self._conectando:
            return self._resultado("ya_en_curso")
        if self._cerrado:
            return self._resultado("error")
        pedidos = scopes_pedidos(s)
        if not pedidos:
            return self._resultado("error", "No hay servicios de Google pedidos (RAMS_GOOGLE_SERVICIOS).")
        self._conectando = True
        self._publicar_estado()
        try:
            return await self._conectar(pedidos, timeout_s, mostrar_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # contrato: conectar nunca lanza
            log.warning("Fallo inesperado al conectar Google (%s)", type(exc).__name__)
            return self._resultado("error")
        finally:
            self._conectando = False
            self._futuro_conexion = None
            self._publicar_estado()

    async def _conectar(self, pedidos: tuple[str, ...], timeout_s: float,
                        mostrar_url: Callable[[str], None] | None) -> ResultadoConexion:
        cliente_oauth, estado_cliente = await asyncio.to_thread(self._leer_cliente_sync)
        self._cliente_oauth = cliente_oauth
        self._estado_cliente = estado_cliente
        if cliente_oauth is None:
            log.info("Conexión con Google: %s", estado_cliente)
            return self._resultado(estado_cliente)
        loop = asyncio.get_running_loop()
        futuro: asyncio.Future[tuple[str, str | None]] = loop.create_future()
        self._futuro_conexion = futuro
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
        state = secrets.token_urlsafe(32)
        abiertos: set[asyncio.StreamWriter] = set()
        puerto_ref: dict[str, int] = {}
        intentos = {"state": 0}

        async def manejador(lector: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
            abiertos.add(escritor)
            try:
                await self._atender_callback(lector, escritor, futuro, state, intentos, puerto_ref.get("p", 0))
            except Exception as exc:  # una conexión rota nunca tumba la espera
                log.debug("Callback de OAuth descartado (%s)", type(exc).__name__)
            finally:
                abiertos.discard(escritor)
                with contextlib.suppress(Exception):
                    escritor.close()

        servidor = await asyncio.start_server(manejador, "127.0.0.1", 0, limit=MAX_PETICION_CALLBACK)
        try:
            puerto = int(servidor.sockets[0].getsockname()[1])
            puerto_ref["p"] = puerto
            redirect_uri = f"http://127.0.0.1:{puerto}/"
            parametros = {
                "client_id": cliente_oauth.client_id, "redirect_uri": redirect_uri, "response_type": "code",
                "scope": " ".join(pedidos), "code_challenge": challenge.decode("ascii"),
                "code_challenge_method": "S256", "state": state, "prompt": "consent",
            }
            url = URL_AUTORIZACION + "?" + urllib.parse.urlencode(parametros, quote_via=urllib.parse.quote)
            log.info("Conexión con Google: esperando la autorización en el navegador")
            await self._abrir(url, mostrar_url)
            try:
                codigo_resultado, code = await asyncio.wait_for(asyncio.shield(futuro), timeout_s)
            except asyncio.TimeoutError:
                log.info("Conexión con Google: tiempo agotado")
                return self._resultado("tiempo_agotado")
        finally:
            if not futuro.done():
                futuro.cancel()
            servidor.close()
            for escritor in list(abiertos):
                with contextlib.suppress(Exception):
                    escritor.close()
            try:
                await asyncio.wait_for(servidor.wait_closed(), PLAZO_CIERRE_SERVIDOR_S)
            except (asyncio.TimeoutError, Exception) as exc:  # el servidor ya no acepta conexiones
                log.debug("Cierre del servidor de OAuth incompleto (%s)", type(exc).__name__)
        if codigo_resultado != "ok" or code is None:
            log.info("Conexión con Google: %s", codigo_resultado)
            return self._resultado(codigo_resultado)
        return await self._canjear(cliente_oauth, code, verifier, redirect_uri, pedidos)

    async def _abrir(self, url: str, mostrar_url: Callable[[str], None] | None) -> None:
        """Abre el navegador del sistema (en un hilo). Si no puede, ``mostrar_url(url)``. Nunca registra la URL."""
        abrir = self._abrir_navegador if self._abrir_navegador is not None else webbrowser.open
        abierto = False
        try:
            abierto = bool(await asyncio.to_thread(abrir, url))
        except Exception as exc:  # sin navegador: se muestra la URL en la consola
            log.info("No se pudo abrir el navegador (%s)", type(exc).__name__)
        if not abierto and mostrar_url is not None:
            try:
                mostrar_url(url)
            except Exception as exc:  # mostrar la URL es opcional
                log.debug("mostrar_url falló (%s)", type(exc).__name__)

    async def _atender_callback(self, lector: asyncio.StreamReader, escritor: asyncio.StreamWriter,
                                futuro: asyncio.Future[tuple[str, str | None]], state: str,
                                intentos: dict[str, int], puerto: int) -> None:
        """Atiende una petición del navegador (≤ 8 KiB en ≤ 10 s, solo GET). Nunca registra la línea de petición."""
        try:
            cabecera = await asyncio.wait_for(lector.readuntil(b"\r\n\r\n"), PLAZO_LECTURA_CALLBACK_S)
        except asyncio.LimitOverrunError:
            await self._responder(escritor, 431, "Request Header Fields Too Large", "")
            return
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ValueError):
            return
        try:
            texto = cabecera.decode("latin-1")
        except UnicodeDecodeError:
            await self._responder(escritor, 400, "Bad Request", "")
            return
        lineas = texto.split("\r\n")
        partes = lineas[0].split(" ")
        if len(partes) != 3 or not partes[2].startswith("HTTP/1."):
            await self._responder(escritor, 400, "Bad Request", "")
            return
        metodo, objetivo = partes[0], partes[1]
        if metodo != "GET":
            await self._responder(escritor, 405, "Method Not Allowed", "")
            return
        host = ""
        for linea in lineas[1:]:
            nombre, _, valor = linea.partition(":")
            if nombre.strip().lower() == "host":
                host = valor.strip().lower()
        if host and host not in (f"127.0.0.1:{puerto}", "127.0.0.1"):
            await self._responder(escritor, 400, "Bad Request", "")
            return
        try:
            division = urllib.parse.urlsplit(objetivo)
        except ValueError:
            await self._responder(escritor, 400, "Bad Request", "")
            return
        if division.path == "/favicon.ico" or division.path != "/":
            await self._responder(escritor, 404, "Not Found", "")
            return
        if futuro.done():
            await self._responder(escritor, 200, "OK", _HTML_TERMINADO)
            return
        try:
            consulta = urllib.parse.parse_qs(division.query, keep_blank_values=True, max_num_fields=20)
        except ValueError:
            consulta = {}
        recibido = (consulta.get("state") or [""])[0]
        if not recibido or not hmac.compare_digest(recibido.encode("utf-8"), state.encode("utf-8")):
            intentos["state"] += 1
            log.info("Callback de OAuth con state inválido (%d/%d)", intentos["state"], MAX_INTENTOS_STATE)
            await self._responder(escritor, 400, "Bad Request", "")
            if intentos["state"] >= MAX_INTENTOS_STATE and not futuro.done():
                futuro.set_result(("state_invalido", None))
            return
        error = (consulta.get("error") or [""])[0]
        if error:   # access_denied (canceló) u otro rechazo de Google (p. ej. política de la cuenta)
            await self._responder(escritor, 200, "OK", _HTML_CANCELADO)
            if not futuro.done():
                futuro.set_result(("denegado", None))
            return
        code = (consulta.get("code") or [""])[0]
        if not _RE_CODIGO_AUTORIZACION.fullmatch(code):
            await self._responder(escritor, 400, "Bad Request", "")
            if not futuro.done():
                futuro.set_result(("canje_fallido", None))
            return
        await self._responder(escritor, 200, "OK", _HTML_LISTO)
        if not futuro.done():
            futuro.set_result(("ok", code))

    @staticmethod
    async def _responder(escritor: asyncio.StreamWriter, estado: int, razon: str, html: str) -> None:
        cuerpo = html.encode("utf-8")
        cabeceras = (f"HTTP/1.1 {estado} {razon}\r\n"
                     "Content-Type: text/html; charset=utf-8\r\n"
                     f"Content-Length: {len(cuerpo)}\r\n"
                     "Cache-Control: no-store\r\n"
                     "Referrer-Policy: no-referrer\r\n"
                     "X-Content-Type-Options: nosniff\r\n"
                     "Content-Security-Policy: default-src 'none'\r\n"
                     "Connection: close\r\n\r\n")
        try:
            escritor.write(cabeceras.encode("ascii") + cuerpo)
            await asyncio.wait_for(escritor.drain(), PLAZO_LECTURA_CALLBACK_S)
        except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
            log.debug("No se pudo responder al navegador (%s)", type(exc).__name__)

    async def _canjear(self, cliente_oauth: _Cliente, code: str, verifier: str, redirect_uri: str,
                       pedidos: tuple[str, ...]) -> ResultadoConexion:
        """Canje del código (con ``client_secret``), cuenta (``about``) y guardado del token (§6.4, pasos 8–12)."""
        cliente = self._obtener_cliente()
        formulario = {"client_id": cliente_oauth.client_id, "client_secret": cliente_oauth.client_secret,
                      "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
                      "redirect_uri": redirect_uri}
        try:
            respuesta = await cliente.post(URL_TOKEN, data=formulario, timeout=30.0, follow_redirects=False)
        except Exception as exc:  # red, tiempo agotado…
            log.info("Canje del código de Google: sin red (%s)", type(exc).__name__)
            return self._resultado("red")
        datos = _json_de(respuesta)
        if respuesta.status_code != 200 or not isinstance(datos, Mapping):
            log.info("Canje del código de Google: HTTP %d (%s)", respuesta.status_code,
                     ",".join(_razones(datos)) or "sin razón")
            return self._resultado("canje_fallido")
        acceso = datos.get("access_token")
        refresh = datos.get("refresh_token")
        if (not isinstance(acceso, str) or not _RE_TOKEN.fullmatch(acceso)
                or not isinstance(refresh, str) or not _RE_TOKEN.fullmatch(refresh)):
            log.info("Canje del código de Google: respuesta sin refresh token")
            return self._resultado("canje_fallido")
        scope = datos.get("scope")
        recibidos = scope.split() if isinstance(scope, str) else []
        # El token guarda lo que Google concedió de verdad (para detectar después un permiso de más); se USA solo lo
        # que la configuración pide. Un scope que Google dé sin pedirlo nunca cuenta como concedido.
        en_token = tuple(s for s in SCOPES_CONOCIDOS if s in recibidos)
        concedidos, de_mas = clasificar_scopes(en_token, pedidos)
        faltan = tuple(s for s in pedidos if s not in en_token
                       and not (s in _DRIVE_ESTRECHOS and SCOPE_DRIVE in en_token))
        expira = _numero(datos.get("expires_in"), 3600.0)
        expira_refresh = datos.get("refresh_token_expires_in")
        ahora = self._reloj()
        refresh_expira_ts = ahora + float(expira_refresh) if isinstance(expira_refresh, (int, float)) else None
        cuenta: str | None = None
        if any(s in concedidos for s in (SCOPE_DRIVE, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)):
            cuenta = await self._consultar_cuenta(acceso)
        try:
            guardado = await asyncio.to_thread(self._guardar_token_sync, refresh, en_token, cuenta,
                                               cliente_oauth.client_id, ahora, refresh_expira_ts)
        except OSError as exc:
            log.warning("No se pudo guardar el token de Google (%s): la sesión vale solo en memoria",
                        type(exc).__name__)
            guardado = False
        self._refresh = refresh
        self._acceso = acceso
        self._acceso_expira = ahora + max(0.0, expira)
        self._scopes = en_token
        self._cuenta = cuenta
        self._client_id_sha = _sha12(cliente_oauth.client_id)
        self._token_invalido = False
        self._token_guardado = guardado
        log.info("Cuenta de Google conectada: %d permisos concedidos, %d sin conceder, %d de más sin usar",
                 len(concedidos), len(faltan), len(de_mas))
        mensaje = _mensaje_conectado(concedidos, faltan, guardado)
        if de_mas:
            sobran = ", ".join(dict.fromkeys(_NOMBRES_SCOPE.get(s, "otro") for s in de_mas))
            sin_drive = " y Drive queda sin usar" if SCOPE_DRIVE in de_mas else ""
            mensaje += (f" Google me dio más permiso del que pedí ({sobran}): no lo uso{sin_drive}. Para quitarlo, "
                        "desconecta Google y vuelve a conectar (o revísalo en myaccount.google.com/connections).")
        return ResultadoConexion(ok=True, codigo="conectado", mensaje=mensaje, concedidos=concedidos, faltan=faltan)

    async def _consultar_cuenta(self, acceso: str) -> str | None:
        """``about.get?fields=user(emailAddress)`` con el access token recién obtenido; ``None`` si falla."""
        try:
            respuesta = await self._obtener_cliente().get(
                URL_ABOUT, params={"fields": "user(emailAddress)"},
                headers={"Authorization": f"Bearer {acceso}"}, timeout=20.0, follow_redirects=False)
        except Exception as exc:  # la cuenta es opcional
            log.debug("about.get de Drive falló (%s)", type(exc).__name__)
            return None
        datos = _json_de(respuesta)
        if respuesta.status_code != 200 or not isinstance(datos, Mapping):
            log.debug("about.get de Drive: HTTP %d", respuesta.status_code)
            return None
        usuario = datos.get("user")
        correo = usuario.get("emailAddress") if isinstance(usuario, Mapping) else None
        return correo if isinstance(correo, str) and _RE_CORREO_CUENTA.fullmatch(correo) else None

    # ------------------------------------------------------------------ desconectar

    async def desconectar(self) -> ResultadoConexion:
        """Revoca (200 o 400 = ok) y SIEMPRE borra el token local, aunque falle la red. Nunca lanza."""
        self.cancelar_conexion()
        refresh = self._refresh
        mensaje = ("Listo: desconecté tu cuenta de Google. También puedes revisarlo en "
                   "myaccount.google.com/connections.")
        if refresh is None:
            mensaje = ("Borré el permiso local de Google. Si quieres, revísalo en myaccount.google.com/connections.")
        elif (httpx is None or bool(getattr(self._settings, "offline", False))):
            mensaje = ("Borré el permiso local; si quieres, revísalo en myaccount.google.com/connections "
                       "(sin conexión no pude revocarlo en Google).")
        else:
            try:
                respuesta = await self._obtener_cliente().post(URL_REVOCAR, data={"token": refresh}, timeout=20.0,
                                                               follow_redirects=False)
                if respuesta.status_code == 200:
                    log.info("Permiso de Google revocado")
                elif respuesta.status_code == 400:
                    log.info("Revocación de Google: el permiso ya no era válido")
                    mensaje = ("Listo: ya estaba desconectado en Google y borré el permiso local. También puedes "
                               "revisarlo en myaccount.google.com/connections.")
                else:
                    log.info("Revocación de Google: HTTP %d", respuesta.status_code)
                    mensaje = ("Borré el permiso local; si quieres, revísalo en myaccount.google.com/connections "
                               "(Google no confirmó la revocación).")
            except Exception as exc:  # red caída: se borra igual
                log.info("Revocación de Google sin red (%s): borro el permiso local igual", type(exc).__name__)
                mensaje = ("Borré el permiso local; si quieres, revísalo en myaccount.google.com/connections.")
        self._refresh = None
        self._acceso = None
        self._acceso_expira = 0.0
        self._scopes = ()
        self._cuenta = None
        self._token_invalido = False
        self._token_guardado = True
        ok = True
        try:
            await asyncio.to_thread(self._borrar_token_sync)
        except Exception as exc:  # no se pudo borrar el archivo
            log.warning("No se pudo borrar el token local de Google (%s)", type(exc).__name__)
            ok = False
            mensaje = "Revoqué lo que pude, pero no pude borrar el archivo del permiso local de Google."
        self._publicar_estado()
        return ResultadoConexion(ok=ok, codigo="desconectado" if ok else "error", mensaje=mensaje)

    # ------------------------------------------------------------------ tokens (§6.5)

    def _comprobar_disponible(self) -> None:
        if not bool(getattr(self._settings, "tiene_google", False)) or httpx is None or self._cerrado:
            raise ErrorGoogle("desconectado", "La conexión con Google está apagada.")
        if self._token_invalido:
            raise ErrorGoogle("token_invalido", _MENSAJES_ERROR["token_invalido"])
        if self._refresh is None or self._cliente_oauth is None or self._estado_cliente != "ok":
            raise ErrorGoogle("desconectado", _MENSAJES_ERROR["desconectado"])

    def _acceso_vigente(self) -> str | None:
        if self._acceso is not None and self._reloj() < self._acceso_expira - MARGEN_REFRESCO_S:
            return self._acceso
        return None

    def _invalidar_acceso(self) -> None:
        self._acceso = None
        self._acceso_expira = 0.0

    async def token_acceso(self) -> str:
        """Access token vigente (refresca si quedan < 120 s). Lanza :class:`ErrorGoogle`."""
        self._comprobar_disponible()
        vigente = self._acceso_vigente()
        if vigente is not None:
            return vigente
        async with self._candado():
            self._comprobar_disponible()
            vigente = self._acceso_vigente()
            if vigente is not None:
                return vigente
            return await self._refrescar()

    async def _refrescar(self) -> str:
        """Refresco con ``client_secret`` y ``refresh_token``; 5xx/red → reintentos 1, 2 y 4 s."""
        cliente = self._obtener_cliente()
        cliente_oauth = self._cliente_oauth
        refresh = self._refresh
        if cliente_oauth is None or refresh is None:
            raise ErrorGoogle("desconectado", _MENSAJES_ERROR["desconectado"])
        formulario = {"client_id": cliente_oauth.client_id, "client_secret": cliente_oauth.client_secret,
                      "refresh_token": refresh, "grant_type": "refresh_token"}
        esperas = list(ESPERAS_REINTENTO)
        while True:
            try:
                respuesta = await cliente.post(URL_TOKEN, data=formulario, timeout=30.0, follow_redirects=False)
            except Exception as exc:
                error = _error_de_transporte(exc)
                if esperas:
                    log.info("Refresco del token de Google sin red (%s): reintento", type(exc).__name__)
                    await self._dormir(esperas.pop(0))
                    continue
                log.info("Refresco del token de Google: %s", error.codigo)
                raise error from exc
            if respuesta.status_code >= 500 and esperas:
                log.info("Refresco del token de Google: HTTP %d, reintento", respuesta.status_code)
                await self._dormir(_segundos_retry_after(respuesta) or esperas[0])
                esperas.pop(0)
                continue
            break
        datos = _json_de(respuesta)
        razones = _razones(datos)
        if respuesta.status_code == 200 and isinstance(datos, Mapping):
            acceso = datos.get("access_token")
            if isinstance(acceso, str) and _RE_TOKEN.fullmatch(acceso):
                self._acceso = acceso
                self._acceso_expira = self._reloj() + max(0.0, _numero(datos.get("expires_in"), 3600.0))
                log.debug("Token de acceso de Google renovado")
                return acceso
            raise ErrorGoogle("error", "Google devolvió una renovación sin token.", http=200)
        if respuesta.status_code in (400, 401) and ({"invalid_grant", "invalid_client", "unauthorized_client"}
                                                    & set(razones)):
            await self._invalidar_token()
            raise ErrorGoogle("token_invalido", _MENSAJES_ERROR["token_invalido"], http=respuesta.status_code,
                              razon=razones[0] if razones else None)
        error = _error_de_estado(respuesta.status_code, razones)
        log.info("Refresco del token de Google: HTTP %d (%s)", respuesta.status_code, error.codigo)
        raise error

    async def _invalidar_token(self) -> None:
        """``invalid_grant``: borra el token en memoria, marca el archivo y publica ``google.estado``."""
        self._refresh = None
        self._acceso = None
        self._acceso_expira = 0.0
        self._token_invalido = True
        log.warning("El permiso de Google caducó o se revocó: hay que reconectar")
        try:
            await asyncio.to_thread(self._marcar_token_invalido_sync)
        except Exception as exc:  # el archivo es solo para el diagnóstico
            log.debug("No se pudo marcar el token de Google como inválido (%s)", type(exc).__name__)
        self._publicar_estado()

    # ------------------------------------------------------------------ cliente REST

    async def solicitud(self, metodo: str, url: str, *, params: Mapping[str, Any] | None = None,
                        json: Any = None, contenido: bytes | None = None,
                        cabeceras: Mapping[str, str] | None = None, timeout: float = 30.0,
                        reintentar: bool = False) -> httpx.Response:
        """Petición autenticada (Bearer) a Google. Lanza :class:`ErrorGoogle` ante cualquier fallo o estado ≥ 400.

        Solo ``https://`` hacia ``HOSTS_PERMITIDOS`` y métodos de ``METODOS_PERMITIDOS`` (nunca ``DELETE``); sin
        seguir redirecciones. Un 401 → refresca una vez y repite. ``reintentar=True`` (solo GET e idempotentes)
        → 429/5xx y fallos de red con espera de 1, 2 y 4 s (respetando ``Retry-After``).
        """
        metodo = str(metodo).upper()
        if metodo not in METODOS_PERMITIDOS:
            raise ErrorGoogle("error", "Método no permitido hacia Google.")
        _validar_url(url)
        cliente = self._obtener_cliente()
        ruta = _ruta_segura(url)
        esperas = list(ESPERAS_REINTENTO) if reintentar else []
        refrescado = False
        while True:
            token = await self.token_acceso()
            encabezados = {str(k): str(v) for k, v in (cabeceras or {}).items()
                           if str(k).lower() != "authorization"}
            encabezados["Authorization"] = f"Bearer {token}"
            try:
                respuesta = await cliente.request(metodo, url, params=params, json=json, content=contenido,
                                                  headers=encabezados, timeout=timeout, follow_redirects=False)
            except Exception as exc:
                error = _error_de_transporte(exc)
                if esperas and error.codigo in ("red", "tiempo_agotado"):
                    log.debug("Google %s %s: %s, reintento", metodo, ruta, error.codigo)
                    await self._dormir(esperas.pop(0))
                    continue
                log.info("Google %s %s: %s", metodo, ruta, error.codigo)
                raise error from exc
            estado = respuesta.status_code
            if estado == 401 and not refrescado:
                refrescado = True
                log.debug("Google %s %s → 401: renuevo el token y repito", metodo, ruta)
                self._invalidar_acceso()
                continue
            if esperas and (estado == 429 or estado >= 500):
                espera = _segundos_retry_after(respuesta) or esperas[0]
                esperas.pop(0)
                log.debug("Google %s %s → %d: reintento", metodo, ruta, estado)
                await self._dormir(espera)
                continue
            if estado >= 400:
                error = _error_de_estado(estado, _razones(_json_de(respuesta)))
                log.info("Google %s %s → %d (%s)", metodo, ruta, estado, error.razon or error.codigo)
                raise error
            log.debug("Google %s %s → %d", metodo, ruta, estado)
            return respuesta

    def flujo(self, metodo: str, url: str, *, params: Mapping[str, Any] | None = None,
              timeout: float = 60.0) -> AbstractAsyncContextManager[httpx.Response]:
        """Respuesta en *streaming* (descargas por bloques). Al entrar lanza :class:`ErrorGoogle` si falla."""
        return self._flujo(metodo, url, params=params, timeout=timeout)

    @contextlib.asynccontextmanager
    async def _flujo(self, metodo: str, url: str, *, params: Mapping[str, Any] | None,
                     timeout: float) -> AsyncIterator[httpx.Response]:
        metodo = str(metodo).upper()
        if metodo not in METODOS_PERMITIDOS:
            raise ErrorGoogle("error", "Método no permitido hacia Google.")
        _validar_url(url)
        cliente = self._obtener_cliente()
        ruta = _ruta_segura(url)
        refrescado = False
        while True:
            token = await self.token_acceso()
            peticion = cliente.build_request(metodo, url, params=params, headers={"Authorization": f"Bearer {token}"},
                                             timeout=timeout)
            try:
                respuesta = await cliente.send(peticion, stream=True, follow_redirects=False)
            except Exception as exc:
                error = _error_de_transporte(exc)
                log.info("Google %s %s: %s", metodo, ruta, error.codigo)
                raise error from exc
            estado = respuesta.status_code
            if estado == 401 and not refrescado:
                refrescado = True
                await respuesta.aclose()
                self._invalidar_acceso()
                continue
            if estado >= 300:
                try:
                    cuerpo = b""
                    async for bloque in respuesta.aiter_bytes():
                        cuerpo += bloque
                        if len(cuerpo) > MAX_CUERPO_ERROR:
                            break
                except Exception as exc:  # el cuerpo del error es opcional
                    log.debug("Cuerpo de error ilegible (%s)", type(exc).__name__)
                    cuerpo = b""
                finally:
                    await respuesta.aclose()
                try:
                    datos = jsonlib.loads(cuerpo.decode("utf-8")) if cuerpo else None
                except (UnicodeDecodeError, ValueError):
                    datos = None
                error = (_error_de_estado(estado, _razones(datos)) if estado >= 400
                         else ErrorGoogle("error", _MENSAJES_ERROR["error"], http=estado))
                log.info("Google %s %s → %d (%s)", metodo, ruta, estado, error.razon or error.codigo)
                raise error
            log.debug("Google %s %s → %d (flujo)", metodo, ruta, estado)
            try:
                yield respuesta
            finally:
                await respuesta.aclose()
            return


def _numero(valor: Any, defecto: float) -> float:
    """Número finito ≥ 0 o ``defecto``."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return defecto
    numero = float(valor)
    if numero != numero or numero < 0 or numero == float("inf"):
        return defecto
    return numero


def _mensaje_conectado(concedidos: tuple[str, ...], faltan: tuple[str, ...], guardado: bool) -> str:
    """Mensaje para el usuario tras conectar: qué quedó y qué falta (sin datos de la cuenta)."""
    partes: list[str] = []
    if SCOPE_GMAIL_SEND in concedidos:
        partes.append("Gmail (solo enviar)")
    if SCOPE_DRIVE in concedidos:
        partes.append("Drive (completo)")
    elif SCOPE_DRIVE_READONLY in concedidos and SCOPE_DRIVE_FILE in concedidos:
        partes.append("Drive (leer todo, modificar lo que yo cree)")
    elif SCOPE_DRIVE_READONLY in concedidos:
        partes.append("Drive (solo leer)")
    elif SCOPE_DRIVE_FILE in concedidos:
        partes.append("Drive (solo lo que yo cree)")
    texto = ("Cuenta de Google conectada: " + " y ".join(partes) + "." if partes
             else "Conecté la cuenta de Google, pero sin ningún permiso.")
    sin_marcar: list[str] = []
    if SCOPE_GMAIL_SEND in faltan:
        sin_marcar.append("enviar correo")
    if any(s in faltan for s in (SCOPE_DRIVE, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)):
        sin_marcar.append("Drive")
    if sin_marcar:
        texto += " No marcaste " + " ni ".join(sin_marcar) + ": reconecta y marca la casilla si lo quieres."
    if not guardado:
        texto += " No pude guardar el permiso de forma segura: vale solo hasta que me reinicies."
    return texto
