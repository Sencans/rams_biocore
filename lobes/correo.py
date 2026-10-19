"""Correo (Gmail, solo enviar) de R.A.M.S. BIO-CORE (v0.2.2).

Qué hace (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §7):
- Prepara **un** borrador en memoria (nunca en disco): resuelve destinatarios (correos o alias de
  ``RAMS_CORREO_CONTACTOS``), sanea asunto y cuerpo, busca los adjuntos **solo por nombre o ruta
  relativa** dentro de Descargas, Documentos y Escritorio, y valida límites y el control
  anti-secretos (regla 5) sobre el texto **crudo**. Lo que no cumple va a ``problemas`` (bloquean).
- Ofrece la vista de la tarjeta (sección ``correo`` de §12.2, sin recortar nada aprobable) y su
  huella (sha256). Main la muestra; el usuario aprueba con el botón.
- Envía el borrador aprobado con ``users.messages.send``: comprueba de nuevo la huella y el sha256 de
  cada adjunto (anti-TOCTOU), arma el MIME sin ``From`` y **nunca reintenta**. Distingue ``red``
  (nada salió) de ``red_ambigua`` (pudo salir) para la reserva de tasa de la compuerta.

También ofrece :func:`buscar_archivo_local`, la búsqueda confinada de archivos del usuario que comparte
``lobes/drive.py`` (subidas).

Dependencias: ``lobes.google_cuenta`` (REST autenticado) y la biblioteca estándar (``email``).

Degradación: sin Google, sin el servicio ``correo`` o sin ``gmail.send`` concedido → ``disponible``
falso y diagnóstico OFF / EN ESPERA / DEGRADADO; ``preparar`` sigue funcionando (el borrador se puede
ver), pero ``enviar`` responde ``desconectado`` o ``sin_permiso``.

Privacidad (§7.4, §15.12): los logs llevan solo conteos, bytes y códigos; nunca direcciones, asunto,
cuerpo ni nombres de archivo. La API pública nunca lanza.
"""

from __future__ import annotations

import asyncio
import base64
import email.policy
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import stat
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

try:  # opcional: solo para distinguir errores de conexión (red) de los ambiguos
    import httpx
except ImportError:  # sin httpx la cuenta queda OFF y nunca se llega a enviar
    httpx = None  # type: ignore[assignment]

from core.config import Settings, carpetas_usuario, normalizar_titulo, parece_secreto
from core.contracts import (
    CORREO_RE,
    MAX_ADJUNTOS_CORREO,
    MAX_ASUNTO_CORREO,
    MAX_CUERPO_CORREO,
    PATRON_ARCHIVO_SENSIBLE,
    TTL_TARJETA_S,
    DiagnosticoModulo,
    EstadoModulo,
    huella_contenido,
    largo_utf16,
)
from lobes.google_cuenta import ErrorGoogle

if TYPE_CHECKING:  # solo para anotaciones
    from core.bus import BusEventos
    from lobes.google_cuenta import CuentaGoogle

__all__ = [
    "URL_ENVIAR", "URL_ENVIAR_SUBIDA", "UMBRAL_SUBIDA_GMAIL", "MAX_MIME_GMAIL", "CODIGOS_ENVIO",
    "CODIGOS_SIN_SALIDA", "CARPETAS_ADJUNTOS", "NOMBRES_CARPETA",
    "Adjunto", "Borrador", "ResultadoEnvio", "Correo",
    "ArchivoLocal", "BusquedaLocal", "buscar_archivo_local", "sanear_texto", "recortar_utf16",
    "recortar_aviso",
]

log = logging.getLogger("rams.correo")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOMBRE_MODULO: Final = "Correo (Gmail)"
URL_ENVIAR: Final = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
URL_ENVIAR_SUBIDA: Final = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send"
UMBRAL_SUBIDA_GMAIL: Final = 5 * 1024 * 1024    # MIME mayor ⇒ endpoint de subida uploadType=media
MAX_MIME_GMAIL: Final = 36_700_160              # 35 MiB: tope de messages.send [V]
TIMEOUT_ENVIO_S: Final = 120.0
CODIGOS_ENVIO: Final = ("enviado", "no_listo", "huella", "caducado", "no_existe", "desconectado", "sin_permiso",
                        "limite", "red", "red_ambigua", "rechazado", "demasiado_grande", "adjunto_cambio", "error")
CODIGOS_PUDO_SALIR: Final = frozenset({"enviado", "red_ambigua", "error"})
CODIGOS_SIN_SALIDA: Final = frozenset(c for c in CODIGOS_ENVIO if c not in CODIGOS_PUDO_SALIR)
CANALES_ORIGEN: Final = ("terminal", "escritorio", "telegram")
CARPETAS_ADJUNTOS: Final = ("descargas", "documentos", "escritorio")
NOMBRES_CARPETA: Final[Mapping[str, str]] = {
    "descargas": "Descargas", "documentos": "Documentos", "escritorio": "Escritorio",
    "imagenes": "Imágenes", "musica": "Música", "videos": "Vídeos",
}
# Sinónimos (normalizados) del primer segmento de una ruta relativa → clave de carpeta.
_PREFIJOS_CARPETA: Final[Mapping[str, str]] = {
    "descargas": "descargas", "downloads": "descargas", "documentos": "documentos", "documents": "documentos",
    "escritorio": "escritorio", "desktop": "escritorio", "imagenes": "imagenes", "pictures": "imagenes",
    "fotos": "imagenes", "musica": "musica", "music": "musica", "videos": "videos",
}
MAX_DIRECCION: Final = 254
MAX_NOMBRE_ARCHIVO: Final = 255       # unidades UTF-16
MAX_PROBLEMAS: Final = 10
MAX_AVISOS: Final = 5
MAX_TEXTO_AVISO: Final = 160          # unidades UTF-16 de cada problema o aviso
MAX_ENTRADA_EN_MENSAJE: Final = 60    # unidades UTF-16 del texto del usuario citado en un problema
MAX_ASUNTO_DEFECTO: Final = 60
MAX_PROFUNDIDAD: Final = 3            # niveles de subcarpetas que se recorren
MAX_ENTRADAS: Final = 5000            # entradas como mucho por carpeta del usuario
TAM_BLOQUE: Final = 1024 * 1024
ASUNTO_VACIO: Final = "(sin asunto)"
_MIB: Final = 1024 * 1024
_ATRIBUTOS_EXCLUIDOS: Final = 0x2 | 0x4 | 0x400   # FILE_ATTRIBUTE_HIDDEN | SYSTEM | REPARSE_POINT (Windows)

_RE_SENSIBLE: Final = re.compile(PATRON_ARCHIVO_SENSIBLE)
_RE_UNIDAD: Final = re.compile(r"^[A-Za-z]:")
_RE_SEPARADOR_DESTINOS: Final = re.compile(r"\s*[,;]\s*|\s+y\s+|\s+e\s+")
_PREFIJOS_ALIAS: Final = frozenset({"mi", "mis", "a", "al", "para"})
_RE_GMAIL_ID: Final = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_RE_DRIVE_EN_LINEA: Final = re.compile(r"(?:drive|docs)\.google\.com", re.IGNORECASE)

_MENSAJES_ENVIO: Final[Mapping[str, str]] = {
    "no_listo": "El borrador tiene problemas pendientes: corrígelos en la tarjeta.",
    "huella": "El borrador cambió desde que lo viste: revisa la versión nueva.",
    "caducado": "El borrador caducó: prepáralo otra vez.",
    "no_existe": "Ese borrador ya no existe.",
    "desconectado": "Primero conecta tu cuenta de Google (di «conecta mi cuenta de Google»).",
    "sin_permiso": "Falta el permiso para enviar correo: reconecta y marca «Enviar correo».",
    "limite": "Gmail limitó tus envíos; puede durar horas.",
    "red": "No hay conexión con Gmail: no salió nada. Inténtalo de nuevo.",
    "red_ambigua": "Puede que ya se haya enviado: revisa Enviados en Gmail antes de volver a pulsar Enviar.",
    "rechazado": "Gmail rechazó el correo.",
    "demasiado_grande": "El correo pasa de 35 MB: quita algún adjunto.",
    "adjunto_cambio": "Un archivo adjunto cambió desde que lo aprobaste: revísalo de nuevo.",
    "error": "Algo falló al enviar y puede que haya salido: revisa Enviados en Gmail antes de reintentar.",
}


# ---------------------------------------------------------------------------
# Texto (funciones puras, compartidas con lobes/drive.py)
# ---------------------------------------------------------------------------


def _sin_sustitutos(texto: str) -> str:
    """Cambia los sustitutos sueltos (U+D800–U+DFFF) por U+FFFD."""
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in texto):
        return texto
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in texto)


def sanear_texto(texto: Any, *, multilinea: bool) -> str:
    """Texto saneado: sustitutos sueltos → U+FFFD; ``\\r\\n``/``\\r`` → ``\\n``; NFC; sin controles.

    Con ``multilinea`` se conservan ``\\n`` y ``\\t``; sin él, todo control (saltos incluidos) pasa a un
    espacio y se recortan los espacios de los extremos. ``None`` ⇒ ``""``. Nunca lanza.
    """
    if texto is None:
        return ""
    try:
        cadena = texto if isinstance(texto, str) else str(texto)
        cadena = _sin_sustitutos(cadena).replace("\r\n", "\n").replace("\r", "\n")
        cadena = unicodedata.normalize("NFC", cadena)
        if multilinea:
            return "".join(c for c in cadena if c in "\n\t" or unicodedata.category(c) != "Cc")
        return "".join(" " if unicodedata.category(c) == "Cc" else c for c in cadena).strip()
    except Exception as exc:  # red de seguridad: texto imposible de sanear
        log.debug("Texto no saneable (%s)", type(exc).__name__)
        return ""


def recortar_utf16(texto: str, maximo: int) -> str:
    """Prefijo de ``texto`` de como mucho ``maximo`` unidades UTF-16, cortado en un límite de carácter."""
    if largo_utf16(texto) <= maximo:
        return texto
    unidades = 0
    for indice, caracter in enumerate(texto):
        unidades += 2 if ord(caracter) > 0xFFFF else 1
        if unidades > maximo:
            return texto[:indice]
    return texto


def recortar_aviso(texto: str, maximo: int = MAX_TEXTO_AVISO) -> str:
    """Texto informativo de una línea, ≤ ``maximo`` unidades UTF-16 (con «…» si se recorta)."""
    limpio = sanear_texto(texto, multilinea=False)
    if largo_utf16(limpio) <= maximo:
        return limpio
    return recortar_utf16(limpio, maximo - 1).rstrip() + "…"


def _cita(texto: str) -> str:
    """Texto del usuario para citarlo en un problema (una línea, ≤ 60 unidades)."""
    return recortar_aviso(texto, MAX_ENTRADA_EN_MENSAJE)


def _acotar(lista: Sequence[str], maximo: int, sobrante: str) -> tuple[str, ...]:
    """≤ ``maximo`` textos (≤ 160 unidades cada uno, sin repetidos); si sobran, el último los cuenta."""
    vistos: list[str] = []
    for item in lista:
        texto = recortar_aviso(item)
        if texto and texto not in vistos:
            vistos.append(texto)
    if len(vistos) <= maximo:
        return tuple(vistos)
    resto = len(vistos) - (maximo - 1)
    return tuple(vistos[: maximo - 1]) + (recortar_aviso(sobrante.format(n=resto)),)


def _tamano_legible(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < _MIB:
        return f"{round(n / 1024)} KB"
    valor = n / _MIB
    texto = f"{valor:.1f}".replace(".", ",")
    return f"{texto.removesuffix(',0')} MB"


# ---------------------------------------------------------------------------
# Archivos del usuario (síncrono: se llama en to_thread)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchivoLocal:
    """Archivo del usuario resuelto y confinado (la ruta nunca sale al bus ni a la tarjeta)."""

    nombre: str          # basename real (≤ 255 unidades UTF-16)
    carpeta: str         # clave de CARPETAS_USUARIO
    ruta: Path
    relativa: str        # «Descargas/a/informe.pdf» (para mensajes; nunca la ruta absoluta)
    bytes: int
    sha256: str
    tipo_mime: str

    def __repr__(self) -> str:
        return f"ArchivoLocal(carpeta={self.carpeta!r}, bytes={self.bytes})"


@dataclass(frozen=True)
class BusquedaLocal:
    """Resultado de :func:`buscar_archivo_local`: el archivo o un problema en español."""

    archivo: ArchivoLocal | None
    problema: str | None


def _lista_carpetas(claves: Sequence[str]) -> str:
    nombres = [NOMBRES_CARPETA.get(c, c) for c in claves]
    if len(nombres) <= 1:
        return "".join(nombres)
    return ", ".join(nombres[:-1]) + " ni " + nombres[-1]


def _excluido(info: os.stat_result) -> bool:
    """Enlace simbólico, *junction* u oculto/sistema de Windows: no se entra ni se usa."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _ATRIBUTOS_EXCLUIDOS)


def _dentro(ruta: Path, base: Path) -> bool:
    """``ruta`` (ya resuelta) queda dentro de ``base`` (resuelta): ``commonpath`` sin distinguir mayúsculas en Windows."""
    try:
        r = os.path.normcase(str(ruta))
        b = os.path.normcase(str(base))
        return os.path.commonpath([r, b]) == b
    except ValueError:  # unidades distintas
        return False


def _segmentos(entrada: Any) -> list[str] | None:
    """Segmentos de un nombre o ruta relativa válida (``/`` o ``\\``); ``None`` si no vale."""
    texto = sanear_texto(entrada, multilinea=False).strip("«»\"'").strip().replace("\\", "/")
    if not texto or "\x00" in texto or texto.startswith(("/", "~")) or _RE_UNIDAD.match(texto):
        return None
    segmentos = texto.split("/")
    if any(s in ("", ".", "..") or s.startswith(".") or s != s.strip() for s in segmentos):
        return None
    return segmentos


def _recorrer(base: Path, nombre: str, maximo: int) -> list[Path]:
    """Archivos llamados ``nombre`` (sin distinguir mayúsculas) bajo ``base``: ≤ 3 niveles, ≤ ``maximo`` entradas,
    sin seguir enlaces ni entrar en carpetas ocultas."""
    buscado = nombre.casefold()
    encontrados: list[Path] = []
    pendientes: list[tuple[Path, int]] = [(base, 0)]
    vistas = 0
    while pendientes:
        directorio, nivel = pendientes.pop(0)
        try:
            with os.scandir(directorio) as iterador:
                entradas = list(iterador)
        except OSError:
            continue
        for entrada in entradas:
            vistas += 1
            if vistas > maximo:
                return encontrados
            if entrada.name.startswith("."):
                continue
            try:
                info = entrada.stat(follow_symlinks=False)
            except OSError:
                continue
            if _excluido(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                if nivel < MAX_PROFUNDIDAD:
                    pendientes.append((Path(entrada.path), nivel + 1))
            elif stat.S_ISREG(info.st_mode) and entrada.name.casefold() == buscado:
                encontrados.append(Path(entrada.path))
    return encontrados


def _hash_archivo(ruta: Path, maximo: int) -> tuple[int, str] | None:
    """(bytes, sha256) leyendo por bloques; ``None`` si pasa de ``maximo``. Lanza ``OSError``."""
    resumen = hashlib.sha256()
    total = 0
    with open(ruta, "rb") as archivo:
        while True:
            bloque = archivo.read(TAM_BLOQUE)
            if not bloque:
                break
            total += len(bloque)
            if total > maximo:
                return None
            resumen.update(bloque)
    return total, resumen.hexdigest()


def leer_archivo_verificado(ruta: Path, sha256: str, maximo: int) -> bytes | None:
    """Contenido de ``ruta`` si su sha256 sigue siendo ``sha256`` (y cabe en ``maximo``); si no, ``None``.

    Lo leído es exactamente lo que se envía o sube (anti-TOCTOU). Síncrona; nunca lanza.
    """
    try:
        info = os.lstat(ruta)
        if _excluido(info) or not stat.S_ISREG(info.st_mode) or info.st_size > maximo:
            return None
        with open(ruta, "rb") as archivo:
            datos = archivo.read(maximo + 1)
    except OSError as exc:
        log.debug("Archivo local ilegible (%s)", type(exc).__name__)
        return None
    if len(datos) > maximo:
        return None
    if not hmac.compare_digest(hashlib.sha256(datos).hexdigest(), str(sha256)):
        return None
    return datos


def buscar_archivo_local(entrada: Any, carpetas: Mapping[str, Path], claves: Sequence[str], *,
                         max_bytes: int, prohibidas: Sequence[Path] = ()) -> BusquedaLocal:
    """Resuelve un nombre o ruta relativa del usuario a un archivo confinado (§7.3, §8.3). Síncrona; nunca lanza.

    - ``entrada``: nombre (``informe.pdf``) o ruta relativa con ``/`` (``trabajo/plan.xlsx``), sin ``..``, sin
      raíz, sin unidad y sin segmentos ocultos. Un primer segmento como «documentos» limita la búsqueda a esa
      carpeta.
    - Se busca en ``claves`` (en orden): primero la ruta exacta y, para un nombre suelto, también en
      subcarpetas hasta 3 niveles (≤ 5000 entradas por carpeta, sin seguir enlaces ni entrar en ocultas).
    - La ruta final debe quedar dentro de su carpeta (``commonpath`` sobre ``resolve()``); se rechaza lo que
      case con ``PATRON_ARCHIVO_SENSIBLE`` y lo que esté bajo ``prohibidas`` (``base_dir``, ``data_dir``).
    - Varias coincidencias → problema con las rutas cortas; nombre de más de 255 unidades UTF-16 → problema.
    """
    try:
        return _buscar_archivo_local(entrada, carpetas, claves, max_bytes=max_bytes, prohibidas=prohibidas)
    except Exception as exc:  # contrato: nunca lanza
        log.debug("Búsqueda de archivo local fallida (%s)", type(exc).__name__)
        return BusquedaLocal(None, f"No pude buscar «{_cita(str(entrada))}» en tus carpetas.")


def _buscar_archivo_local(entrada: Any, carpetas: Mapping[str, Path], claves: Sequence[str], *,
                          max_bytes: int, prohibidas: Sequence[Path]) -> BusquedaLocal:
    cita = _cita(str(entrada if entrada is not None else ""))
    segmentos = _segmentos(entrada)
    if segmentos is None:
        return BusquedaLocal(None, f"«{cita}»: escribe solo el nombre o una ruta como «documentos/plan.xlsx» "
                                   "(sin «..» ni rutas absolutas).")
    claves_busqueda = [c for c in claves if c in carpetas]
    solo_exacta = len(segmentos) > 1
    prefijo = _PREFIJOS_CARPETA.get(normalizar_titulo(segmentos[0])) if len(segmentos) > 1 else None
    if prefijo is not None:
        if prefijo not in claves:
            return BusquedaLocal(None, f"«{cita}»: solo busco en {_lista_carpetas(claves).replace(' ni ', ' y ')}.")
        claves_busqueda = [prefijo] if prefijo in carpetas else []
        segmentos = segmentos[1:]
    if not claves_busqueda:
        return BusquedaLocal(None, f"No encontré tus carpetas para buscar «{cita}».")
    nombre = segmentos[-1]
    candidatos: list[tuple[str, Path, Path]] = []      # (clave, base resuelta, ruta)
    for clave in claves_busqueda:
        try:
            base = Path(carpetas[clave]).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        rutas = [base.joinpath(*segmentos)]
        if not solo_exacta:
            rutas.extend(_recorrer(base, nombre, MAX_ENTRADAS))
        for ruta in rutas:
            candidatos.append((clave, base, ruta))
    validos: dict[str, tuple[str, Path, Path, Path, os.stat_result]] = {}
    for clave, base, ruta in candidatos:
        try:
            info = os.lstat(ruta)
        except OSError:
            continue
        if _excluido(info) or not stat.S_ISREG(info.st_mode):
            continue
        try:
            resuelta = ruta.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not _dentro(resuelta, base):
            continue
        validos.setdefault(os.path.normcase(str(resuelta)), (clave, base, ruta, resuelta, info))
    if not validos:
        return BusquedaLocal(None, f"No encontré «{cita}» en {_lista_carpetas(claves_busqueda)}.")
    aceptados: list[tuple[str, Path, Path, Path, os.stat_result, str]] = []
    rechazo: str | None = None
    for clave, base, ruta, resuelta, info in validos.values():
        try:
            relativa = ruta.relative_to(base).as_posix()
        except ValueError:
            relativa = ruta.name
        if _RE_SENSIBLE.search(relativa.lower()) or _RE_SENSIBLE.search(ruta.name.lower()):
            rechazo = rechazo or f"«{cita}» parece un archivo de claves o de configuración privada: no lo uso."
            continue
        if any(_dentro(resuelta, p) for p in _resueltas(prohibidas)):
            rechazo = rechazo or f"«{cita}» está dentro de los archivos de Amadeus: no lo uso."
            continue
        aceptados.append((clave, base, ruta, resuelta, info, f"{NOMBRES_CARPETA.get(clave, clave)}/{relativa}"))
    if not aceptados:
        return BusquedaLocal(None, rechazo or f"No encontré «{cita}».")
    if len(aceptados) > 1:
        cortas = [a[5] for a in aceptados[:3]]
        mas = "…" if len(aceptados) > 3 else ""
        return BusquedaLocal(None, f"Hay varios «{cita}»: {', '.join(cortas)}{mas}. Escribe la ruta, p. ej. "
                                   f"«{cortas[0].lower()}».")
    clave, _base, ruta, resuelta, info, corta = aceptados[0]
    if largo_utf16(ruta.name) > MAX_NOMBRE_ARCHIVO:
        return BusquedaLocal(None, f"El nombre de «{cita}» pasa de 255 caracteres: renómbralo.")
    if info.st_size > max_bytes:
        return BusquedaLocal(None, f"«{cita}» pesa {_tamano_legible(info.st_size)} y el máximo es "
                                   f"{_tamano_legible(max_bytes)}.")
    try:
        medido = _hash_archivo(resuelta, max_bytes)
    except OSError:
        return BusquedaLocal(None, f"No pude leer «{cita}».")
    if medido is None:
        return BusquedaLocal(None, f"«{cita}» pasa del máximo de {_tamano_legible(max_bytes)}.")
    tamano, sha = medido
    tipo = mimetypes.guess_type(ruta.name)[0] or "application/octet-stream"
    return BusquedaLocal(ArchivoLocal(nombre=ruta.name, carpeta=clave, ruta=resuelta, relativa=corta,
                                      bytes=tamano, sha256=sha, tipo_mime=tipo), None)


def _resueltas(rutas: Sequence[Path]) -> list[Path]:
    resultado: list[Path] = []
    for ruta in rutas:
        try:
            resultado.append(Path(ruta).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return resultado


# ---------------------------------------------------------------------------
# §7.1 Tipos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Adjunto:
    """Adjunto resuelto. La ruta NUNCA sale al bus ni a la tarjeta."""

    nombre: str          # basename (lo que ve el usuario)
    carpeta: str         # clave de CARPETAS_USUARIO (p. ej. "descargas") o "" si no se resolvió
    ruta: Path | None    # NUNCA sale al bus ni a la tarjeta
    bytes: int
    sha256: str          # del contenido, calculado en to_thread
    tipo_mime: str       # mimetypes.guess_type o application/octet-stream

    def __repr__(self) -> str:
        return f"Adjunto(carpeta={self.carpeta!r}, bytes={self.bytes})"


@dataclass(frozen=True)
class Borrador:
    """Borrador de correo (solo en memoria). Los destinatarios ya están resueltos.

    ``pendientes_para``/``_cc``/``_cco`` y ``adjuntos_pendientes`` guardan lo que el usuario pidió y **no** entró
    (destinatarios sin resolver, con CR/LF o de más; adjuntos no encontrados, rechazados, ambiguos o de más). Cada
    uno genera un problema que bloquea. :meth:`Correo.editar` los conserva mientras el usuario no edite **ese**
    campo, así que una edición de otro campo nunca los hace desaparecer en silencio (§7.3). No son aprobables: no
    salen del módulo (ni en la vista, ni en la huella, ni en ``repr``).
    """

    id: str
    version: int
    para: tuple[str, ...]
    cc: tuple[str, ...]
    cco: tuple[str, ...]
    asunto: str
    cuerpo: str
    adjuntos: tuple[Adjunto, ...]
    problemas: tuple[str, ...]   # ("no conozco a «mi jefe»: escribe su correo en la tarjeta", …)
    avisos: tuple[str, ...]      # no bloquean ("No tengo a Gemini: usé tu texto tal cual")
    canal_origen: str
    creado: float
    caduca: float
    pendientes_para: tuple[str, ...] = field(default=(), compare=False)
    pendientes_cc: tuple[str, ...] = field(default=(), compare=False)
    pendientes_cco: tuple[str, ...] = field(default=(), compare=False)
    # str = entrada cruda que se vuelve a buscar; Adjunto = archivo ya elegido que dejó de valer (se reintenta igual).
    adjuntos_pendientes: tuple[str | Adjunto, ...] = field(default=(), compare=False)

    def __repr__(self) -> str:  # sin contenido: ni direcciones, ni asunto, ni cuerpo
        return (f"Borrador(id={self.id!r}, version={self.version}, n_destinatarios={self.n_destinatarios}, "
                f"n_adjuntos={len(self.adjuntos)}, problemas={len(self.problemas)})")

    @property
    def listo(self) -> bool:
        """Sin problemas y con ≥ 1 destinatario."""
        return not self.problemas and self.n_destinatarios >= 1

    @property
    def n_destinatarios(self) -> int:
        return len(self.para) + len(self.cc) + len(self.cco)

    def huella(self) -> str:
        """sha256 del contenido aprobable (destinatarios, asunto, cuerpo y adjuntos con su sha256)."""
        return huella_contenido({
            "clase": "correo", "para": list(self.para), "cc": list(self.cc), "cco": list(self.cco),
            "asunto": self.asunto, "cuerpo": self.cuerpo,
            "adjuntos": [{"nombre": a.nombre, "bytes": a.bytes, "sha256": a.sha256} for a in self.adjuntos],
        })

    def vista(self) -> dict[str, Any]:
        """Sección "correo" de la tarjeta (§12.2). Nada aprobable se recorta (los topes se garantizan antes)."""
        return {
            "para": list(self.para), "cc": list(self.cc), "cco": list(self.cco),
            "asunto": self.asunto, "cuerpo": self.cuerpo,
            "adjuntos": [{"nombre": a.nombre, "bytes": int(a.bytes), "carpeta": a.carpeta[:20]}
                         for a in self.adjuntos],
            "problemas": list(self.problemas), "avisos": list(self.avisos), "listo": self.listo,
        }


@dataclass(frozen=True)
class ResultadoEnvio:
    """Resultado de :meth:`Correo.enviar` (mensaje en español y sin contenido)."""

    ok: bool
    codigo: str     # ∈ CODIGOS_ENVIO
    mensaje: str    # español, sin contenido
    gmail_id: str | None = None
    n_destinatarios: int = 0
    n_adjuntos: int = 0
    bytes: int = 0

    @property
    def pudo_salir(self) -> bool:
        """``True`` si algo pudo llegar a Gmail (``enviado``, ``red_ambigua`` o ``error``, fail-closed). Con
        ``False`` main libera la reserva de tasa de la compuerta (§5.4)."""
        return self.codigo in CODIGOS_PUDO_SALIR


# ---------------------------------------------------------------------------
# §7.2 Clase
# ---------------------------------------------------------------------------


class Correo:
    """Borrador único en memoria y envío por Gmail (solo tras la aprobación con la tarjeta)."""

    def __init__(self, settings: Settings, cuenta: CuentaGoogle | None, bus: BusEventos | None = None, *,
                 reloj: Callable[[], float] = time.time,
                 carpetas: Mapping[str, Path] | None = None) -> None:   # None = config.carpetas_usuario()
        self._settings = settings
        self._cuenta = cuenta
        self._bus = bus
        self._reloj = reloj
        self._carpetas: dict[str, Path] | None = dict(carpetas) if carpetas is not None else None
        self._borrador: Borrador | None = None
        self._ttl_s = TTL_TARJETA_S
        self._avisos_externos: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"Correo(disponible={self.disponible}, borrador={self._borrador is not None})"

    # ------------------------------------------------------------------ ciclo de vida

    async def iniciar(self) -> DiagnosticoModulo:
        """Resuelve las carpetas del usuario (en un hilo) y devuelve el diagnóstico ``correo``."""
        try:
            await self._carpetas_usuario()
        except Exception as exc:  # contrato: nunca lanza
            log.debug("Carpetas del usuario no disponibles (%s)", type(exc).__name__)
        return self.diagnostico()

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico ``correo`` (§7.5)."""
        s = self._settings
        if not bool(getattr(s, "tiene_google", False)):
            detalle = "--offline" if bool(getattr(s, "offline", False)) and s.google_habilitado else "Google deshabilitado"
            return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.OFF, detalle)
        if "correo" not in tuple(getattr(s, "google_servicios", ()) or ()):
            return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.OFF,
                                     "servicio no pedido (RAMS_GOOGLE_SERVICIOS)")
        estado = self._estado_cuenta()
        if estado is None:
            return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.OFF, "sin la cuenta de Google")
        if getattr(estado, "motivo", "") == "sin_httpx":
            return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.OFF, "sin httpx")
        if not estado.conectado:
            return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.EN_ESPERA, "sin conectar")
        if not estado.correo:
            return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "falta permiso gmail.send: reconecta y marca «Enviar correo»")
        return DiagnosticoModulo("correo", NOMBRE_MODULO, EstadoModulo.OK,
                                 f"listo · ≤ {s.correo_max_dia}/día · ≤ {s.correo_max_destinatarios} destinatarios")

    async def cerrar(self) -> None:
        """Olvida el borrador (vive solo en memoria)."""
        self._borrador = None

    def _estado_cuenta(self) -> Any:
        if self._cuenta is None:
            return None
        try:
            return self._cuenta.estado()
        except Exception as exc:  # una cuenta rota cuenta como desconectada
            log.debug("Estado de la cuenta de Google ilegible (%s)", type(exc).__name__)
            return None

    @property
    def disponible(self) -> bool:
        """"correo" ∈ servicios y cuenta conectada con gmail.send."""
        s = self._settings
        if not bool(getattr(s, "tiene_google", False)):
            return False
        if "correo" not in tuple(getattr(s, "google_servicios", ()) or ()):
            return False
        estado = self._estado_cuenta()
        return bool(estado is not None and estado.conectado and estado.correo)

    async def _carpetas_usuario(self) -> dict[str, Path]:
        if self._carpetas is None:
            self._carpetas = await asyncio.to_thread(carpetas_usuario)
        return self._carpetas

    # ------------------------------------------------------------------ destinatarios

    def _resolver_uno(self, pieza: str) -> str | None:
        if len(pieza) <= MAX_DIRECCION and CORREO_RE.fullmatch(pieza):
            return pieza
        try:
            contactos = self._settings.correo_contactos
        except Exception as exc:  # contactos ilegibles: sin alias
            log.debug("Contactos de correo ilegibles (%s)", type(exc).__name__)
            return None
        alias = normalizar_titulo(pieza).strip(" .,:;¡!¿?")
        if alias in contactos:
            return contactos[alias]
        palabras = alias.split()
        while palabras and palabras[0] in _PREFIJOS_ALIAS:
            palabras = palabras[1:]
            reducido = " ".join(palabras)
            if reducido in contactos:
                return contactos[reducido]
        return None

    def resolver_destinatarios(self, crudos: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(correos válidos, no resueltos). Alias normalizado (minúsculas, sin tildes) → ``correo_contactos``; si
        no está, se reintenta sin "mi", "mis", "a", "al" ni "para" al principio ("mi jefe" → "jefe"). Acepta
        "a, b y c". Sin repetidos (sin distinguir mayúsculas). Nunca lanza."""
        validos, pendientes = self._resolver_detallado(crudos)
        return validos, tuple(saneado for saneado, _ in pendientes)

    def _resolver_detallado(self, crudos: Any) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """(correos válidos, ((pendiente saneado, pieza cruda), …)). La pieza cruda es la que se vuelve a resolver
        si el usuario no edita ese campo (así el resultado es idéntico). Nunca lanza."""
        validos: list[str] = []
        pendientes: list[tuple[str, str]] = []
        vistos: set[str] = set()
        if isinstance(crudos, str):
            crudos = (crudos,)
        try:
            elementos = list(crudos or ())
        except TypeError:
            return (), ()
        for crudo in elementos:
            if not isinstance(crudo, str):
                continue
            texto = _sin_sustitutos(crudo)
            for pieza in _RE_SEPARADOR_DESTINOS.split(texto):
                pieza = pieza.strip().strip("<>«»\"'").strip()
                if not pieza:
                    continue
                correo = self._resolver_uno(pieza) if "\r" not in pieza and "\n" not in pieza else None
                if correo is not None:
                    clave = correo.lower()
                    if clave not in vistos:
                        vistos.add(clave)
                        validos.append(correo)
                    continue
                saneado = sanear_texto(pieza, multilinea=False)
                if saneado and all(saneado != p for p, _ in pendientes):
                    pendientes.append((saneado, pieza))
        return tuple(validos), tuple(pendientes)

    # ------------------------------------------------------------------ preparar y editar (§7.3)

    async def preparar(self, *, para: Sequence[str], cc: Sequence[str] = (), cco: Sequence[str] = (),
                       asunto: str | None, cuerpo: str | None, adjuntos: Sequence[str] = (),
                       canal_origen: str, ttl_s: float, avisos: Sequence[str] = ()) -> Borrador:
        """Crea el borrador vigente (reemplaza al anterior). Recibe asunto, cuerpo y destinatarios **sin
        redactar**: el control anti-secretos va sobre el texto crudo. Nunca lanza."""
        ttl = _ttl_valido(ttl_s)
        externos = _lista_textos(avisos)
        try:
            borrador = await self._construir(
                id=uuid.uuid4().hex[:8], version=1, creado=self._reloj(), ttl=ttl,
                canal_origen=canal_origen, para=_lista_textos(para), cc=_lista_textos(cc), cco=_lista_textos(cco),
                asunto=asunto, cuerpo=cuerpo, entradas_adjuntos=_lista_textos(adjuntos), previos=(),
                avisos_externos=externos)
        except Exception as exc:  # contrato: nunca lanza; borrador vacío y bloqueado
            log.warning("Fallo inesperado al preparar un correo (%s)", type(exc).__name__)
            ahora = self._reloj()
            borrador = Borrador(id=uuid.uuid4().hex[:8], version=1, para=(), cc=(), cco=(), asunto=ASUNTO_VACIO,
                                cuerpo="", adjuntos=(), problemas=("No pude preparar el correo: inténtalo de nuevo.",),
                                avisos=(), canal_origen=_canal_valido(canal_origen), creado=ahora, caduca=ahora + ttl)
        self._borrador = borrador
        self._ttl_s = ttl
        self._avisos_externos = externos
        log.info("Borrador de correo preparado: %d destinatarios, %d adjuntos, %d problemas",
                 borrador.n_destinatarios, len(borrador.adjuntos), len(borrador.problemas))
        return borrador

    async def editar(self, id: str, campos: Mapping[str, str]) -> Borrador | None:
        """Edita el borrador vigente: ``campos`` ⊆ {"para","cc","cco","asunto","cuerpo","adjuntos"} (listas como
        texto separado por comas), **sin redactar**. Revalida todo; ``version+1``. ``None`` si no existe, caducó
        o los campos no son válidos. Nunca lanza."""
        actual = self.borrador(id)
        if actual is None or not isinstance(campos, Mapping):
            return None
        permitidas = {"para", "cc", "cco", "asunto", "cuerpo", "adjuntos"}
        if any(k not in permitidas or not isinstance(v, str) for k, v in campos.items()):
            log.debug("Edición de correo con campos no válidos: se ignora")
            return None
        try:
            def lista(clave: str, resueltos: Sequence[Any], pendientes: Sequence[Any]) -> list[Any]:
                # Campo no editado: lo que mostraba la tarjeta MÁS lo que no entró (sigue bloqueando, §7.3). Los
                # clientes solo mandan los campos cambiados: sin esto, editar el asunto borraba en silencio un
                # destinatario sin resolver o un adjunto no encontrado y dejaba el borrador «listo».
                if clave not in campos:
                    return [*resueltos, *pendientes]
                return [p for p in (x.strip() for x in re.split(r"[,;]", campos[clave])) if p]

            borrador = await self._construir(
                id=actual.id, version=actual.version + 1, creado=actual.creado, ttl=self._ttl_s,
                canal_origen=actual.canal_origen, para=lista("para", actual.para, actual.pendientes_para),
                cc=lista("cc", actual.cc, actual.pendientes_cc), cco=lista("cco", actual.cco, actual.pendientes_cco),
                asunto=campos.get("asunto", actual.asunto), cuerpo=campos.get("cuerpo", actual.cuerpo),
                entradas_adjuntos=lista("adjuntos", actual.adjuntos, actual.adjuntos_pendientes),
                previos=actual.adjuntos if "adjuntos" in campos else (),
                avisos_externos=self._avisos_externos)
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al editar un correo (%s)", type(exc).__name__)
            return None
        self._borrador = borrador
        log.info("Borrador de correo editado: versión %d, %d destinatarios, %d problemas", borrador.version,
                 borrador.n_destinatarios, len(borrador.problemas))
        return borrador

    def borrador(self, id: str) -> Borrador | None:
        """El borrador vigente con esa id (``None`` si no existe o caducó; caducado equivale a descartado)."""
        b = self._borrador
        if b is None or not isinstance(id, str) or b.id != id:
            return None
        if self._reloj() >= b.caduca:
            self._borrador = None
            return None
        return b

    def descartar(self, id: str) -> bool:
        """Olvida el borrador con esa id. ``True`` si existía."""
        b = self._borrador
        if b is not None and isinstance(id, str) and b.id == id:
            self._borrador = None
            return True
        return False

    async def _construir(self, *, id: str, version: int, creado: float, ttl: float, canal_origen: str,
                         para: Sequence[str], cc: Sequence[str], cco: Sequence[str], asunto: Any, cuerpo: Any,
                         entradas_adjuntos: Sequence[str | Adjunto], previos: Sequence[Adjunto],
                         avisos_externos: Sequence[str]) -> Borrador:
        s = self._settings
        problemas: list[str] = []
        avisos: list[str] = list(avisos_externos)

        # Destinatarios: CR/LF fuera, resolución, sin repetidos entre listas y tope total. Lo que no entra se guarda
        # (crudo) en los pendientes de SU lista: bloquea hasta que el usuario edite ese campo.
        listas: list[list[str]] = []
        pendientes_listas: list[list[str]] = [[], [], []]
        no_resueltos: list[tuple[str, int]] = []      # (texto saneado, índice de la lista)
        con_saltos: list[int] = []
        for indice, crudos in enumerate((para, cc, cco)):
            limpios = []
            for crudo in crudos:
                if "\r" in crudo or "\n" in crudo:
                    if indice not in con_saltos:
                        con_saltos.append(indice)
                    pendientes_listas[indice].append(crudo)
                    continue
                limpios.append(crudo)
            validos, pendientes = self._resolver_detallado(limpios)
            listas.append(list(validos))
            for saneado, pieza in pendientes:
                pendientes_listas[indice].append(pieza)
                if all(saneado != p for p, _ in no_resueltos):
                    no_resueltos.append((saneado, indice))
        if con_saltos:
            problemas.append(f"Un destinatario de {_campos_legibles(con_saltos)} tenía saltos de línea y no lo uso: "
                             "corrige ese campo en la tarjeta.")
        for pendiente, indice in no_resueltos:
            problemas.append(f"No conozco a «{_cita(pendiente)}» ({_CAMPOS_DESTINO[indice]}): escribe su correo en "
                             "ese campo de la tarjeta.")
        vistos: set[str] = set()
        maximo = max(1, int(getattr(s, "correo_max_destinatarios", 10)))
        finales: list[list[str]] = [[], [], []]
        sobran = 0
        listas_sobrantes: list[int] = []
        for indice, lista in enumerate(listas):
            for direccion in lista:
                clave = direccion.lower()
                if clave in vistos:
                    continue
                vistos.add(clave)
                if sum(len(f) for f in finales) >= maximo:
                    sobran += 1
                    pendientes_listas[indice].append(direccion)
                    if indice not in listas_sobrantes:
                        listas_sobrantes.append(indice)
                    continue
                finales[indice].append(direccion)
        if sobran:
            cuantos = ("Sobra 1 destinatario", "no entró") if sobran == 1 else (f"Sobran {sobran} destinatarios",
                                                                                 "no entraron")
            problemas.append(f"{cuantos[0]} (máximo {maximo}): {cuantos[1]} en {_campos_legibles(listas_sobrantes)}; "
                             "quita alguno en la tarjeta.")
        if not any(finales) and not no_resueltos and not con_saltos:
            problemas.append("Falta el destinatario: escríbelo en la tarjeta.")

        # Cuerpo: saneado y recortado ANTES de huellar.
        cuerpo_limpio = sanear_texto(cuerpo, multilinea=True)
        if largo_utf16(cuerpo_limpio) > MAX_CUERPO_CORREO:
            cuerpo_limpio = recortar_utf16(cuerpo_limpio, MAX_CUERPO_CORREO)
            problemas.append("El cuerpo pasaba de 10 000 caracteres y lo corté al final: revísalo en Editar y "
                             "guarda los cambios.")

        # Asunto: una línea; por defecto, el principio del cuerpo.
        asunto_limpio = sanear_texto(asunto, multilinea=False)
        if largo_utf16(asunto_limpio) > MAX_ASUNTO_CORREO:
            asunto_limpio = recortar_utf16(asunto_limpio, MAX_ASUNTO_CORREO).rstrip()
            problemas.append("El asunto pasaba de 200 caracteres y lo corté: revísalo en Editar y guarda los "
                             "cambios.")
        if not asunto_limpio:
            asunto_limpio = _asunto_por_defecto(cuerpo_limpio)
        if not cuerpo_limpio.strip() and asunto_limpio == ASUNTO_VACIO:
            problemas.append("El correo está vacío: escribe algo en la tarjeta.")

        # Anti-secretos (regla 5) sobre el texto crudo: asunto y cada línea del cuerpo; la primera que dé True.
        secretos = _secretos(s)
        sospechosa: tuple[str, str] | None = None
        if asunto_limpio != ASUNTO_VACIO and parece_secreto(asunto_limpio, secretos):
            sospechosa = ("El asunto", asunto_limpio)
        else:
            for numero, linea in enumerate(cuerpo_limpio.split("\n"), start=1):
                if parece_secreto(linea, secretos):
                    sospechosa = (f"La línea {numero}", linea)
                    break
        if sospechosa is not None:
            problemas.append(f"{sospechosa[0]} parece llevar una contraseña, un código o una clave (o una palabra "
                             "de 24+ caracteres seguidos): no lo envío por correo. Cámbialo en la tarjeta.")
            if _RE_DRIVE_EN_LINEA.search(sospechosa[1]):
                avisos.append("Para compartir un archivo de Drive usa «comparte el 2 con ana@x.com».")

        # Adjuntos (todo en un hilo).
        adjuntos, problemas_adjuntos, adjuntos_pendientes = await self._resolver_adjuntos(entradas_adjuntos, previos)
        problemas.extend(problemas_adjuntos)

        ahora = self._reloj()
        return Borrador(
            id=id, version=version, para=tuple(finales[0]), cc=tuple(finales[1]), cco=tuple(finales[2]),
            asunto=asunto_limpio, cuerpo=cuerpo_limpio, adjuntos=tuple(adjuntos),
            problemas=_acotar(problemas, MAX_PROBLEMAS, "Y {n} problemas más: revísalos en la tarjeta."),
            avisos=_acotar(avisos, MAX_AVISOS, "Y {n} avisos más."),
            canal_origen=_canal_valido(canal_origen), creado=creado, caduca=ahora + ttl,
            pendientes_para=tuple(pendientes_listas[0]), pendientes_cc=tuple(pendientes_listas[1]),
            pendientes_cco=tuple(pendientes_listas[2]), adjuntos_pendientes=tuple(adjuntos_pendientes))

    async def _resolver_adjuntos(self, entradas: Sequence[str | Adjunto], previos: Sequence[Adjunto]
                                 ) -> tuple[list[Adjunto], list[str], list[str | Adjunto]]:
        """(adjuntos, problemas, pendientes). Cada entrada es:

        - un :class:`Adjunto` ya elegido (campo no editado): se vuelve a medir **ese** archivo, sin buscar otro;
        - un texto: si coincide por nombre con uno de ``previos`` (el campo editado muestra los nombres), se usa ese
          archivo (por orden, así dos «informe.pdf» de carpetas distintas siguen siendo dos); si no, se busca.

        Lo que falla o pasa del máximo de adjuntos va a ``pendientes`` tal cual, para que siga bloqueando.
        """
        s = self._settings
        problemas: list[str] = []
        pendientes: list[str | Adjunto] = []
        entradas = [e for e in entradas if isinstance(e, Adjunto) or (isinstance(e, str) and e.strip())]
        if not entradas:
            return [], problemas, pendientes
        if len(entradas) > MAX_ADJUNTOS_CORREO:
            de_mas = len(entradas) - MAX_ADJUNTOS_CORREO
            problemas.append(f"Como mucho {MAX_ADJUNTOS_CORREO} adjuntos: {de_mas} no "
                             f"{'entraron' if de_mas != 1 else 'entró'}; quita alguno en la tarjeta.")
            pendientes.extend(entradas[MAX_ADJUNTOS_CORREO:])
            entradas = entradas[:MAX_ADJUNTOS_CORREO]
        maximo = max(1, int(getattr(s, "correo_max_adjuntos_mb", 20))) * _MIB
        carpetas = await self._carpetas_usuario()
        prohibidas = (Path(s.base_dir), Path(s.data_dir))
        por_nombre: dict[str, list[Adjunto]] = {}
        for previo in previos:
            if previo.ruta is not None:
                por_nombre.setdefault(previo.nombre.casefold(), []).append(previo)

        def medir(previo: Adjunto) -> Adjunto | None:
            if previo.ruta is None:
                return None
            try:
                medido = _hash_archivo(previo.ruta, maximo)
            except OSError:
                medido = None
            if medido is None:
                return None
            return Adjunto(previo.nombre, previo.carpeta, previo.ruta, medido[0], medido[1], previo.tipo_mime)

        def resolver() -> tuple[list[Adjunto], list[str], list[str | Adjunto]]:
            encontrados: list[Adjunto] = []
            fallos: list[str] = []
            sin_resolver: list[str | Adjunto] = []
            for entrada in entradas:
                previo = entrada if isinstance(entrada, Adjunto) else None
                if previo is None:
                    candidatos = por_nombre.get(entrada.strip().casefold())
                    previo = candidatos.pop(0) if candidatos else None
                if previo is not None:
                    adjunto = medir(previo)
                    if adjunto is None:
                        fallos.append(f"«{_cita(previo.nombre)}» ya no está o pasa del máximo: quítalo o cámbialo.")
                        sin_resolver.append(previo)
                        continue
                    encontrados.append(adjunto)
                    continue
                assert isinstance(entrada, str)
                busqueda = buscar_archivo_local(entrada, carpetas, CARPETAS_ADJUNTOS, max_bytes=maximo,
                                                prohibidas=prohibidas)
                if busqueda.archivo is None:
                    fallos.append(busqueda.problema or f"No encontré «{_cita(entrada)}».")
                    sin_resolver.append(entrada)
                    continue
                a = busqueda.archivo
                encontrados.append(Adjunto(a.nombre, a.carpeta, a.ruta, a.bytes, a.sha256, a.tipo_mime))
            return encontrados, fallos, sin_resolver

        adjuntos, fallos, sin_resolver = await asyncio.to_thread(resolver)
        problemas.extend(fallos)
        pendientes[:0] = sin_resolver
        unicos: list[Adjunto] = []
        rutas: set[str] = set()
        for adjunto in adjuntos:
            clave = os.path.normcase(str(adjunto.ruta))
            if clave not in rutas:
                rutas.add(clave)
                unicos.append(adjunto)
        total = sum(a.bytes for a in unicos)
        if total > maximo:
            problemas.append(f"Los adjuntos suman {_tamano_legible(total)} y el máximo es {_tamano_legible(maximo)}.")
        return unicos, problemas, pendientes

    # ------------------------------------------------------------------ enviar (§7.4)

    def _resultado(self, codigo: str, b: Borrador | None = None, *, gmail_id: str | None = None,
                   n_bytes: int = 0) -> ResultadoEnvio:
        if codigo == "enviado":
            n = b.n_destinatarios if b is not None else 0
            mensaje = f"Enviado a Gmail para {n} destinatario{'s' if n != 1 else ''}."
        else:
            mensaje = _MENSAJES_ENVIO.get(codigo, _MENSAJES_ENVIO["error"])
        return ResultadoEnvio(ok=codigo == "enviado", codigo=codigo, mensaje=mensaje, gmail_id=gmail_id,
                              n_destinatarios=b.n_destinatarios if b is not None else 0,
                              n_adjuntos=len(b.adjuntos) if b is not None else 0, bytes=n_bytes)

    async def enviar(self, id: str, huella: str) -> ResultadoEnvio:
        """Envía el borrador aprobado. **Sin reintentos.** Nunca lanza.

        Los códigos con ``pudo_salir`` falso solo salen cuando no se llegó a mandar el cuerpo a Gmail: ``red``
        solo ante un error de conexión antes de enviar; ante la duda, ``red_ambigua``.
        """
        try:
            resultado = await self._enviar(id, huella)
        except Exception as exc:  # contrato: nunca lanza; ante la duda, pudo salir
            log.warning("Fallo inesperado al enviar un correo (%s)", type(exc).__name__)
            resultado = self._resultado("error", self._borrador)
        log.info("Envío de correo: %s (%d destinatarios, %d adjuntos, %d bytes)", resultado.codigo,
                 resultado.n_destinatarios, resultado.n_adjuntos, resultado.bytes)
        return resultado

    async def _enviar(self, id: str, huella: str) -> ResultadoEnvio:
        b = self._borrador
        if b is None or not isinstance(id, str) or b.id != id:
            return self._resultado("no_existe")
        if self._reloj() >= b.caduca:
            self._borrador = None
            return self._resultado("caducado", b)
        if not b.listo:
            return self._resultado("no_listo", b)
        actual = b.huella()
        if (not actual or not isinstance(huella, str)
                or not hmac.compare_digest(actual.encode("ascii"), huella.encode("utf-8", "replace"))):
            return self._resultado("huella", b)
        estado = self._estado_cuenta()
        if (self._cuenta is None or estado is None or not estado.conectado
                or not bool(getattr(self._settings, "tiene_google", False))
                or "correo" not in tuple(getattr(self._settings, "google_servicios", ()) or ())):
            return self._resultado("desconectado", b)
        if not estado.correo:
            return self._resultado("sin_permiso", b)

        # Anti-TOCTOU: se relee cada adjunto y se envía exactamente lo que coincide con su sha256.
        maximo = max(1, int(getattr(self._settings, "correo_max_adjuntos_mb", 20))) * _MIB
        datos_adjuntos: list[bytes] = []
        for adjunto in b.adjuntos:
            if adjunto.ruta is None:
                return self._resultado("adjunto_cambio", b)
            datos = await asyncio.to_thread(leer_archivo_verificado, adjunto.ruta, adjunto.sha256, maximo)
            if datos is None or len(datos) != adjunto.bytes:
                return self._resultado("adjunto_cambio", b)
            datos_adjuntos.append(datos)

        try:
            crudo = await asyncio.to_thread(_mime, b, datos_adjuntos)
        except Exception as exc:  # cabeceras imposibles: nada salió
            log.info("No se pudo armar el MIME del correo (%s)", type(exc).__name__)
            return self._resultado("no_listo", b)
        n_bytes = len(crudo)
        if n_bytes > MAX_MIME_GMAIL:
            return self._resultado("demasiado_grande", b, n_bytes=n_bytes)

        # Fase 1: el permiso (nada va a Gmail todavía).
        try:
            await self._cuenta.token_acceso()
        except ErrorGoogle as exc:
            codigo = "desconectado" if exc.codigo in ("desconectado", "token_invalido") else "red"
            return self._resultado(codigo, b, n_bytes=n_bytes)

        # Fase 2: el envío (sin reintentos).
        try:
            if n_bytes <= UMBRAL_SUBIDA_GMAIL:
                cuerpo_json = {"raw": base64.urlsafe_b64encode(crudo).decode("ascii")}
                respuesta = await self._cuenta.solicitud("POST", URL_ENVIAR, json=cuerpo_json,
                                                         timeout=TIMEOUT_ENVIO_S, reintentar=False)
            else:
                respuesta = await self._cuenta.solicitud("POST", URL_ENVIAR_SUBIDA, params={"uploadType": "media"},
                                                         contenido=crudo,
                                                         cabeceras={"Content-Type": "message/rfc822"},
                                                         timeout=TIMEOUT_ENVIO_S, reintentar=False)
        except ErrorGoogle as exc:
            return self._resultado(_codigo_de_error(exc), b, n_bytes=n_bytes)
        if not 200 <= respuesta.status_code < 300:
            return self._resultado("error", b, n_bytes=n_bytes)
        gmail_id: str | None = None
        try:
            datos = respuesta.json()
            candidato = datos.get("id") if isinstance(datos, Mapping) else None
            gmail_id = candidato if isinstance(candidato, str) and _RE_GMAIL_ID.fullmatch(candidato) else None
        except Exception as exc:  # 200 sin JSON: enviado igual
            log.debug("Respuesta de Gmail sin JSON (%s)", type(exc).__name__)
        if self._borrador is not None and self._borrador.id == b.id:
            self._borrador = None   # consumido
        return self._resultado("enviado", b, gmail_id=gmail_id, n_bytes=n_bytes)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _mime(b: Borrador, datos_adjuntos: Sequence[bytes]) -> bytes:
    """MIME del borrador: To, Cc, Bcc y Subject (RFC 2047 si hace falta), **sin From**, policy SMTP (CRLF)."""
    mensaje = EmailMessage(policy=email.policy.SMTP)
    if b.para:
        mensaje["To"] = ", ".join(b.para)
    if b.cc:
        mensaje["Cc"] = ", ".join(b.cc)
    if b.cco:
        mensaje["Bcc"] = ", ".join(b.cco)
    mensaje["Subject"] = b.asunto
    mensaje.set_content(b.cuerpo)
    for adjunto, datos in zip(b.adjuntos, datos_adjuntos):
        principal, _, secundario = (adjunto.tipo_mime or "application/octet-stream").partition("/")
        if not principal or not secundario:
            principal, secundario = "application", "octet-stream"
        mensaje.add_attachment(datos, maintype=principal, subtype=secundario, filename=adjunto.nombre)
    return mensaje.as_bytes()


def _codigo_de_error(exc: ErrorGoogle) -> str:
    """Código de envío a partir del error de la petición a Gmail (§7.4, paso 5)."""
    codigo = exc.codigo
    if codigo == "red":
        causa = exc.__cause__
        if httpx is not None and isinstance(causa, (httpx.ConnectError, httpx.ConnectTimeout)):
            return "red"            # la conexión no llegó a abrirse: nada salió
        return "red_ambigua"
    if codigo == "tiempo_agotado":
        return "red_ambigua"
    if codigo == "limite":
        return "limite"
    if codigo == "sin_permiso":
        return "sin_permiso"
    if codigo in ("desconectado", "token_invalido"):
        return "desconectado"
    if codigo in ("rechazado", "politica", "no_encontrado"):
        return "rechazado"
    if codigo == "demasiado_grande":
        return "demasiado_grande"
    return "error"                  # 5xx y lo desconocido: pudo salir (fail-closed)


def _asunto_por_defecto(cuerpo: str) -> str:
    """Primera línea no vacía del cuerpo, ≤ 60 unidades en un límite de palabra; si no, «(sin asunto)»."""
    for linea in cuerpo.split("\n"):
        linea = " ".join(linea.split())
        if not linea:
            continue
        if largo_utf16(linea) <= MAX_ASUNTO_DEFECTO:
            return linea
        corte = recortar_utf16(linea, MAX_ASUNTO_DEFECTO)
        espacio = corte.rfind(" ")
        termina_palabra = linea[len(corte)].isspace()      # el corte ya cae en un límite de palabra
        if not termina_palabra and espacio >= MAX_ASUNTO_DEFECTO // 3:
            corte = corte[:espacio]
        return corte.rstrip(" ,.;:") or corte
    return ASUNTO_VACIO


_CAMPOS_DESTINO: Final = ("Para", "CC", "CCO")   # etiquetas de la tarjeta, por índice de lista


def _campos_legibles(indices: Sequence[int]) -> str:
    """``[0, 2]`` → ``"Para y CCO"`` (nombres de los campos de destinatarios de la tarjeta)."""
    nombres = [_CAMPOS_DESTINO[i] for i in indices if 0 <= i < len(_CAMPOS_DESTINO)]
    if len(nombres) <= 1:
        return "".join(nombres)
    return ", ".join(nombres[:-1]) + " y " + nombres[-1]


def _lista_textos(valores: Any) -> list[str]:
    """Lista de textos (tolera ``None``, una cadena suelta y elementos no texto)."""
    if valores is None:
        return []
    if isinstance(valores, str):
        return [valores]
    try:
        return [v for v in valores if isinstance(v, str)]
    except TypeError:
        return []


def _ttl_valido(ttl_s: Any) -> float:
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError):
        return TTL_TARJETA_S
    if ttl != ttl or ttl <= 0 or ttl > 86400:
        return TTL_TARJETA_S
    return ttl


def _canal_valido(canal: Any) -> str:
    return canal if canal in CANALES_ORIGEN else "terminal"


def _secretos(settings: Settings) -> tuple[str, ...]:
    try:
        return tuple(settings.secretos())
    except Exception as exc:  # sin secretos literales: siguen las reglas por patrón
        log.debug("Secretos no disponibles (%s)", type(exc).__name__)
        return ()
