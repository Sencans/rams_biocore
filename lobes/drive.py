"""Google Drive de R.A.M.S. BIO-CORE (v0.2.2).

Qué hace (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §8):
- Sin tarjeta: buscar (siempre ``trashed = false``), ver detalles, descargar a una carpeta del usuario
  (binarios por bloques de 1 MiB a ``.part`` + ``os.replace``; Docs, Sheets y Slides se **exportan**),
  leer el texto de un documento (solo si el usuario pide resumirlo) y crear carpetas.
- Con tarjeta (``OperacionDrive`` + huella; main la muestra y el usuario aprueba): subir un archivo local
  (``multipart/related`` ≤ 5 MB o subida reanudable), mover a la **papelera** (``PATCH trashed=true``) y
  compartir (con una persona, o en público solo de lectura y con doble confirmación en main).
- **Nunca** borra de verdad: el cliente no tiene ningún ``DELETE`` (tabla ``RUTAS_DRIVE``; la cuenta
  rechaza además ese método). Nunca ``transferOwnership`` ni ``allowFileDiscovery``.
- Recuerda la última lista (≤ 25 archivos, 30 min) para referencias por número, ordinal o nombre. Solo la cambia
  ``buscar`` (lo que se mostró); un nombre ambiguo nunca la sustituye por candidatos que el usuario no vio.
- Los nombres de Drive son datos no confiables: se quitan controles y caracteres bidireccionales (no pueden
  disfrazar la extensión) y, en Windows, lo descargado de archivos ajenos lleva la marca de origen
  (``Zone.Identifier``) como una descarga del navegador.

Dependencias: ``lobes.google_cuenta`` (REST autenticado) y ``lobes.correo`` (búsqueda confinada de
archivos locales). Sin librerías de Google.

Degradación: sin Google, sin el servicio ``drive`` o sin los permisos → ``disponible`` falso y un
``ResultadoDrive`` con ``deshabilitado``/``desconectado``/``sin_permiso``. En el modo por defecto
(``drive.readonly`` + ``drive.file``), un 403 al tocar un archivo ajeno explica ``RAMS_DRIVE_COMPLETO``.

Privacidad (§8.3, §15.12): los logs llevan solo la operación, el código HTTP y los conteos; nunca
consultas, nombres, ids ni contenido. Los mensajes no llevan rutas absolutas. La API pública nunca lanza.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json as jsonlib
import logging
import mimetypes
import os
import re
import secrets
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, BinaryIO, Final
from urllib.parse import quote, urlsplit

from core.config import Settings, carpetas_usuario, normalizar_titulo
from core.contracts import (
    CARPETAS_USUARIO,
    CORREO_RE,
    DESTINOS_DESCARGA,
    FORMATOS_DRIVE,
    ROLES_DRIVE,
    TTL_TARJETA_S,
    DiagnosticoModulo,
    EstadoModulo,
    formatear_decimal,
    huella_contenido,
    largo_utf16,
)
from lobes.correo import (
    NOMBRES_CARPETA,
    buscar_archivo_local,
    leer_archivo_verificado,
    recortar_aviso,
    recortar_utf16,
    sanear_texto,
)
from lobes.google_cuenta import CODIGOS_ERROR_GOOGLE, ErrorGoogle

if TYPE_CHECKING:  # solo para anotaciones
    from core.bus import BusEventos
    from lobes.google_cuenta import CuentaGoogle

__all__ = [
    "URL_DRIVE", "URL_SUBIDA_DRIVE", "RUTAS_DRIVE", "MIME_CARPETA", "CODIGOS_DRIVE", "OPERACIONES_TARJETA",
    "ArchivoDrive", "OperacionDrive", "ResultadoDrive", "Drive", "sanear_nombre_archivo",
]

log = logging.getLogger("rams.drive")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

NOMBRE_MODULO: Final = "Google Drive"
URL_DRIVE: Final = "https://www.googleapis.com/drive/v3"
URL_SUBIDA_DRIVE: Final = "https://www.googleapis.com/upload/drive/v3/files"
# Única tabla de rutas del cliente de Drive (§8.3). NO existe ningún DELETE: ni files.delete, ni
# emptyTrash, ni quitar permisos. Un test recorre la tabla y el cliente para comprobarlo.
RUTAS_DRIVE: Final[Mapping[str, tuple[str, str]]] = MappingProxyType({
    "listar": ("GET", f"{URL_DRIVE}/files"),
    "crear": ("POST", f"{URL_DRIVE}/files"),
    "obtener": ("GET", f"{URL_DRIVE}/files/{{id}}"),
    "actualizar": ("PATCH", f"{URL_DRIVE}/files/{{id}}"),
    "exportar": ("GET", f"{URL_DRIVE}/files/{{id}}/export"),
    "compartir": ("POST", f"{URL_DRIVE}/files/{{id}}/permissions"),
    "subir": ("POST", URL_SUBIDA_DRIVE),
    "subir_sesion": ("PUT", URL_SUBIDA_DRIVE),     # URI de sesión de la subida reanudable (prefijo)
    "about": ("GET", f"{URL_DRIVE}/about"),
})
MIME_CARPETA: Final = "application/vnd.google-apps.folder"
PREFIJO_GOOGLE: Final = "application/vnd.google-apps."
MIME_DOC: Final = "application/vnd.google-apps.document"
MIME_HOJA: Final = "application/vnd.google-apps.spreadsheet"
MIME_PRESENTACION: Final = "application/vnd.google-apps.presentation"
MIME_DIBUJO: Final = "application/vnd.google-apps.drawing"
_MIME_PDF: Final = "application/pdf"
_MIME_DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MIME_PPTX: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
NOTA_PRIMERA_HOJA: Final = "Solo la primera hoja (así exporta Google las hojas de cálculo a CSV)."
CAMPOS_ARCHIVO: Final = ("id,name,mimeType,size,modifiedTime,parents,webViewLink,ownedByMe,trashed,"
                         "capabilities(canDownload,canEdit,canTrash,canShare)")
CAMPOS_LISTA: Final = f"nextPageToken,incompleteSearch,files({CAMPOS_ARCHIVO})"
CAMPOS_INFO: Final = CAMPOS_ARCHIVO + ",createdTime"
CODIGOS_DRIVE: Final = ("hecho", *CODIGOS_ERROR_GOOGLE, "no_existe", "huella", "caducado", "ambiguo", "no_soportado",
                        "archivo_cambio", "prohibido", "deshabilitado")
OPERACIONES_TARJETA: Final = ("subir", "papelera", "compartir")
CANALES_ORIGEN: Final = ("terminal", "escritorio", "telegram")
MAX_LISTA: Final = 25
TTL_LISTA_S: Final = 1800.0
MAX_CONSULTA: Final = 200
TAM_BLOQUE_DESCARGA: Final = 1024 * 1024
UMBRAL_SUBIDA_SIMPLE: Final = 5 * 1024 * 1024       # ≤ 5 MB ⇒ multipart/related; más ⇒ reanudable
TAM_BLOQUE_SUBIDA: Final = 8 * 1024 * 1024          # múltiplo de 256 KiB
MAX_REANUDACIONES: Final = 3
MAX_LECTURA_BINARIA: Final = 2 * 1024 * 1024        # .txt/.md/.csv/.json que se leen directamente
MAX_EXPORTACION: Final = 10 * 1024 * 1024           # límite de exportación de Google [V]
MAX_NOMBRE_DESCARGA: Final = 150
MAX_NOMBRE_CARPETA: Final = 120
MAX_NOMBRE_LOCAL: Final = 255
MAX_LEER_CARACTERES: Final = 100_000
_MIB: Final = 1024 * 1024

_RE_ID: Final = re.compile(r"^[A-Za-z0-9_\-]{1,256}$")
_RE_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
# Controles bidireccionales (categoría Cf): ALM, LRM/RLM, LRE/RLE/PDF/LRO/RLO y LRI/RLI/FSI/PDI. En un nombre ajeno
# reordenan lo que se ve: «factura‮fdp.exe» se muestra como «facturaexe.pdf». Se quitan de los nombres de Drive.
_RE_BIDI: Final = re.compile("[؜‎‏‪-‮⁦-⁩]")
# Marca de origen (Mark of the Web) para lo descargado de archivos ajenos: SmartScreen y la vista protegida de Office
# los tratan como algo bajado de internet, igual que una descarga del navegador. Sin URL ni datos de la cuenta.
_ZONA_INTERNET: Final = "[ZoneTransfer]\r\nZoneId=3\r\n"
_RE_PROHIBIDOS_NOMBRE: Final = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_RE_RANGO: Final = re.compile(r"^bytes=0-(\d+)$")
_RESERVADOS_WINDOWS: Final = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                                        *(f"lpt{i}" for i in range(1, 10))})
_ORDINALES: Final[Mapping[str, int]] = {
    "primero": 1, "primera": 1, "primer": 1, "segundo": 2, "segunda": 2, "tercero": 3, "tercera": 3,
    "tercer": 3, "cuarto": 4, "cuarta": 4, "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6, "septimo": 7,
    "septima": 7, "setimo": 7, "octavo": 8, "octava": 8, "noveno": 9, "novena": 9, "decimo": 10, "decima": 10,
    "ultimo": -1, "ultima": -1,
}
_MESES: Final = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_EXTENSIONES_TEXTO: Final = (".txt", ".md", ".csv", ".json")
_MIMES_TEXTO: Final = frozenset({"text/plain", "text/markdown", "text/csv", "application/json", "text/x-markdown"})
# Exportación de los documentos de Google: formato → (mime, extensión, nota).
_EXPORTAR: Final[Mapping[str, Mapping[str, tuple[str, str, str]]]] = {
    MIME_DOC: {"pdf": (_MIME_PDF, ".pdf", ""), "texto": ("text/plain", ".txt", ""),
               "original": (_MIME_DOCX, ".docx", "")},
    MIME_HOJA: {"pdf": (_MIME_PDF, ".pdf", ""), "texto": ("text/csv", ".csv", NOTA_PRIMERA_HOJA),
                "original": (_MIME_XLSX, ".xlsx", "")},
    MIME_PRESENTACION: {"pdf": (_MIME_PDF, ".pdf", ""), "texto": ("text/plain", ".txt", ""),
                        "original": (_MIME_PPTX, ".pptx", "")},
    MIME_DIBUJO: {"pdf": (_MIME_PDF, ".pdf", ""), "texto": ("image/png", ".png", ""),
                  "original": ("image/png", ".png", "")},
}
# Lectura para resumir: documento → (mime de exportación, nota).
_LEER: Final[Mapping[str, tuple[str, str]]] = {
    MIME_DOC: ("text/markdown", ""), MIME_HOJA: ("text/csv", NOTA_PRIMERA_HOJA), MIME_PRESENTACION: ("text/plain", ""),
}
_TIPOS_LEGIBLES: Final[Mapping[str, str]] = {
    MIME_CARPETA: "Carpeta", MIME_DOC: "Documento de Google", MIME_HOJA: "Hoja de cálculo",
    MIME_PRESENTACION: "Presentación", MIME_DIBUJO: "Dibujo", "application/vnd.google-apps.form": "Formulario",
    "application/vnd.google-apps.shortcut": "Acceso directo", _MIME_PDF: "PDF", "text/plain": "Texto",
    "text/csv": "CSV", "text/markdown": "Markdown", "application/json": "JSON", "application/zip": "ZIP",
    _MIME_DOCX: "Documento de Word", "application/msword": "Documento de Word", _MIME_XLSX: "Hoja de Excel",
    "application/vnd.ms-excel": "Hoja de Excel", _MIME_PPTX: "Presentación de PowerPoint",
}
_ROLES_LEGIBLES: Final[Mapping[str, str]] = {"reader": "lector", "commenter": "comentarista", "writer": "editor"}
_MENSAJE_COMPLETO: Final = "Ese archivo no lo creé yo: para tocarlo activa RAMS_DRIVE_COMPLETO=1 y reconecta Google."
_MENSAJES: Final[Mapping[str, str]] = {
    "no_existe": "Esa operación de Drive ya no existe.",
    "huella": "La operación cambió desde que la viste: revisa la versión nueva.",
    "caducado": "La operación caducó: pídemela otra vez.",
    "ambiguo": "Hay varios archivos que encajan: dime el número de la lista.",
    "no_soportado": "Ese tipo de archivo no lo puedo leer: descárgalo y ábrelo.",
    "archivo_cambio": "El archivo local cambió desde que lo aprobaste: revísalo de nuevo.",
    "prohibido": "Eso no lo hago.",
    "deshabilitado": "La conexión con Google está apagada: pon RAMS_GOOGLE=1 en el .env, sigue la guía y reinicia.",
    "desconectado": "Primero conecta tu cuenta de Google (di «conecta mi cuenta de Google»).",
    "sin_permiso": "Falta el permiso de Drive: reconecta y marca Drive.",
    "sin_lista": "No tengo una lista reciente de Drive: busca primero (p. ej. «busca informe en drive»).",
}


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _escapar_q(texto: str) -> str:
    """Escapado de un valor dentro de comillas simples de ``q``: ``\\`` → ``\\\\`` y ``'`` → ``\\'``."""
    return texto.replace("\\", "\\\\").replace("'", "\\'")


def _tamano_legible(n: int | None) -> str:
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < _MIB:
        return f"{round(n / 1024)} KB"
    if n < 1024 * _MIB:
        return f"{formatear_decimal(n / _MIB, 1).removesuffix(',0')} MB"
    return f"{formatear_decimal(n / (1024 * _MIB), 1).removesuffix(',0')} GB"


def _fecha_legible(iso: str, *, con_anio: bool | None = None) -> str:
    """``"2026-10-12T15:04:05.000Z"`` → ``"12 oct"`` (con el año si no es el actual)."""
    try:
        fecha = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ""
    texto = f"{fecha.day} {_MESES[fecha.month - 1]}"
    if con_anio or (con_anio is None and fecha.year != datetime.now(timezone.utc).year):
        texto += f" {fecha.year}"
    return texto


def _sin_bidi(texto: str) -> str:
    """``texto`` sin controles bidireccionales (que harían ver una extensión distinta de la real)."""
    return _RE_BIDI.sub("", texto)


def _nombre_drive(valor: Any) -> str:
    """Nombre de un archivo de Drive (dato no confiable) para mostrar o comparar: una línea, sin controles ni bidi."""
    return _sin_bidi(sanear_texto(valor, multilinea=False)).strip()


def sanear_nombre_archivo(nombre: str, extension: str = "") -> str:
    """Nombre seguro para guardar en disco: sin separadores, ``..``, controles (bidireccionales incluidos) ni
    nombres reservados de Windows, sin puntos ni espacios en los extremos, ≤ 150 unidades UTF-16 y con
    ``extension`` al final."""
    texto = _nombre_drive(nombre)
    texto = _RE_PROHIBIDOS_NOMBRE.sub("_", texto)
    texto = re.sub(r"\.{2,}", ".", texto).strip(" .")
    extension = extension if extension and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", extension) else ""
    if extension and texto.lower().endswith(extension.lower()):
        texto = texto[: -len(extension)].rstrip(" .")
    if not texto:
        texto = "archivo"
    base, punto, resto = texto.partition(".")
    if base.strip().lower() in _RESERVADOS_WINDOWS:
        texto = "_" + texto
    maximo = MAX_NOMBRE_DESCARGA - len(extension)
    if largo_utf16(texto) > maximo:
        if not extension and "." in texto[-9:]:
            raiz, _, ext = texto.rpartition(".")
            ext = "." + ext
            texto = recortar_utf16(raiz, maximo - len(ext)).rstrip(" .") + ext
        else:
            texto = recortar_utf16(texto, maximo).rstrip(" .")
    return texto + extension


def _numero_ref(texto: str) -> int | None:
    """Número de la lista a partir de «2», «el 2», «#2», «el segundo», «el último»…; ``None`` si es un nombre."""
    normal = normalizar_titulo(texto).strip(" .")
    for prefijo in ("el ", "la ", "numero ", "nro ", "no ", "n ", "#", "archivo ", "documento "):
        if normal.startswith(prefijo):
            normal = normal[len(prefijo):].strip()
    normal = normal.removeprefix("#").strip(" .")
    if re.fullmatch(r"\d{1,3}", normal):
        return int(normal)
    return _ORDINALES.get(normal)


def _quitar_articulos(texto: str) -> str:
    limpio = sanear_texto(texto, multilinea=False).strip("«»\"'“” ")
    for prefijo in ("el archivo ", "la carpeta ", "el documento ", "el doc ", "la hoja ", "el ", "la "):
        if limpio.lower().startswith(prefijo):
            return limpio[len(prefijo):].strip("«»\"'“” ")
    return limpio


def _entero(valor: Any) -> int | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor if valor >= 0 else None
    if isinstance(valor, str) and valor.isdigit():
        return int(valor)
    return None


# ---------------------------------------------------------------------------
# §8.1 Tipos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchivoDrive:
    """Archivo o carpeta de Drive (metadatos mínimos)."""

    id: str
    nombre: str
    tipo_mime: str
    es_carpeta: bool
    es_google: bool          # application/vnd.google-apps.*
    tamano: int | None
    modificado: str          # ISO UTC
    enlace: str | None       # webViewLink
    propio: bool             # ownedByMe
    puede_descargar: bool
    puede_editar: bool
    puede_papelera: bool
    puede_compartir: bool

    def __repr__(self) -> str:  # sin nombre ni id (datos del usuario)
        return f"ArchivoDrive(tipo={self.tipo_legible()!r}, propio={self.propio})"

    def tipo_legible(self) -> str:
        """"Documento de Google", "Hoja de cálculo", "PDF", "Carpeta"…"""
        if self.tipo_mime in _TIPOS_LEGIBLES:
            return _TIPOS_LEGIBLES[self.tipo_mime]
        if self.es_google:
            return "Archivo de Google"
        principal = self.tipo_mime.partition("/")[0]
        return {"image": "Imagen", "video": "Vídeo", "audio": "Audio", "text": "Texto"}.get(principal, "Archivo")

    def linea(self, n: int) -> str:
        """``"2. Informe Q3 · Documento de Google · 12 oct · tuyo"``."""
        partes = [recortar_aviso(_sin_bidi(self.nombre), 80) or "(sin nombre)", self.tipo_legible()]
        fecha = _fecha_legible(self.modificado)
        if fecha:
            partes.append(fecha)
        partes.append("tuyo" if self.propio else "compartido contigo")
        return f"{n}. " + " · ".join(partes)

    @classmethod
    def desde_json(cls, datos: Any) -> ArchivoDrive | None:
        """Archivo a partir de la respuesta de Drive (``None`` si no es válido)."""
        if not isinstance(datos, Mapping):
            return None
        ident = datos.get("id")
        if not isinstance(ident, str) or not _RE_ID.fullmatch(ident):
            return None
        mime = datos.get("mimeType") if isinstance(datos.get("mimeType"), str) else "application/octet-stream"
        mime = sanear_texto(mime, multilinea=False)[:120] or "application/octet-stream"
        capacidades = datos.get("capabilities") if isinstance(datos.get("capabilities"), Mapping) else {}
        enlace = datos.get("webViewLink")
        if not (isinstance(enlace, str) and enlace.startswith("https://") and len(enlace) <= 2048
                and not _RE_CONTROL.search(enlace)):
            enlace = None
        modificado = datos.get("modifiedTime")
        return cls(
            id=ident, nombre=_nombre_drive(datos.get("name") or ""), tipo_mime=mime,
            es_carpeta=mime == MIME_CARPETA, es_google=mime.startswith(PREFIJO_GOOGLE),
            tamano=_entero(datos.get("size")),
            modificado=sanear_texto(modificado, multilinea=False)[:32] if isinstance(modificado, str) else "",
            enlace=enlace, propio=datos.get("ownedByMe") is True,
            puede_descargar=capacidades.get("canDownload") is True, puede_editar=capacidades.get("canEdit") is True,
            puede_papelera=capacidades.get("canTrash") is True, puede_compartir=capacidades.get("canShare") is True)


@dataclass(frozen=True)
class OperacionDrive:
    """Propuesta con tarjeta (subir, papelera, compartir). La ruta local NUNCA sale al bus ni a la tarjeta."""

    id: str
    version: int
    op: str                                     # ∈ ("subir", "papelera", "compartir")
    archivo: ArchivoDrive | None                # papelera / compartir
    local_nombre: str | None
    local_carpeta: str | None
    local_bytes: int | None
    local_sha256: str | None
    local_ruta: Path | None                     # NUNCA sale al bus ni a la tarjeta
    carpeta_destino: ArchivoDrive | None        # subir (None = raíz de Mi unidad)
    compartir_con: str | None
    rol: str | None
    publico: bool
    problemas: tuple[str, ...]
    canal_origen: str
    creado: float
    caduca: float

    def __repr__(self) -> str:
        return f"OperacionDrive(id={self.id!r}, op={self.op!r}, version={self.version}, problemas={len(self.problemas)})"

    @property
    def listo(self) -> bool:
        """Sin problemas y con lo que necesita su operación."""
        if self.problemas or self.op not in OPERACIONES_TARJETA:
            return False
        if self.op == "subir":
            return (self.local_ruta is not None and self.local_nombre is not None and self.local_sha256 is not None
                    and self.local_bytes is not None)
        if self.archivo is None:
            return False
        if self.op == "compartir":
            if self.rol not in ROLES_DRIVE:
                return False
            if self.publico:
                return self.rol == "reader"
            return bool(self.compartir_con)
        return True

    def huella(self) -> str:
        """sha256 de lo aprobable. Además de los campos de §8.1 incluye ``local_nombre`` (el nombre con el que
        se sube, aprobable en la tarjeta)."""
        return huella_contenido({
            "clase": "drive", "op": self.op, "archivo_id": self.archivo.id if self.archivo else None,
            "local_nombre": self.local_nombre, "local_sha256": self.local_sha256, "local_bytes": self.local_bytes,
            "carpeta_id": self.carpeta_destino.id if self.carpeta_destino else None,
            "compartir_con": self.compartir_con, "rol": self.rol, "publico": bool(self.publico),
        })

    def vista(self) -> dict[str, Any]:
        """Sección "drive" de la tarjeta (§12.2). ``paso`` es 1: el paso 2 (público) lo pone main."""
        archivo = None
        if self.archivo is not None:
            archivo = {"nombre": recortar_aviso(self.archivo.nombre, 200), "tipo": recortar_aviso(
                self.archivo.tipo_legible(), 40), "tamano": self.archivo.tamano,
                "modificado": self.archivo.modificado[:32], "propio": self.archivo.propio}
        local = None
        if self.local_nombre is not None:
            local = {"nombre": self.local_nombre, "bytes": int(self.local_bytes or 0),
                     "carpeta": (self.local_carpeta or "")[:20]}
        compartir = None
        if self.op == "compartir":
            compartir = {"con": self.compartir_con or "", "rol": self.rol if self.rol in ROLES_DRIVE else "reader",
                         "publico": bool(self.publico)}
        return {
            "op": self.op, "paso": 1, "archivo": archivo, "local": local,
            "carpeta_destino": (recortar_aviso(self.carpeta_destino.nombre, 200)
                                if self.carpeta_destino is not None else None),
            "compartir": compartir, "problemas": list(self.problemas), "listo": self.listo,
        }


@dataclass(frozen=True)
class ResultadoDrive:
    """Resultado de una operación de Drive (mensaje en español y sin rutas completas)."""

    ok: bool
    codigo: str            # "hecho" | CODIGOS_ERROR_GOOGLE | "no_existe" | "huella" | "caducado" | "ambiguo"
                           # | "no_soportado" | "archivo_cambio" | "prohibido" | "deshabilitado"
    mensaje: str           # español; sin rutas completas
    archivos: tuple[ArchivoDrive, ...] = ()
    datos: Mapping[str, Any] = field(default_factory=dict)
    # datos posibles: "texto" (leer), "recortado": bool, "nota": str, "guardado": "Descargas/Informe.pdf",
    #                 "bytes": int, "enlace": str, "archivo_id": str; además "lista" (buscar) y
    #                 "detalle" (info): texto para la pantalla local, nunca para Broca.

    def __repr__(self) -> str:
        return f"ResultadoDrive(ok={self.ok}, codigo={self.codigo!r}, archivos={len(self.archivos)})"


def _fallo(codigo: str, mensaje: str | None = None, **kwargs: Any) -> ResultadoDrive:
    codigo = codigo if codigo in CODIGOS_DRIVE else "error"
    return ResultadoDrive(False, codigo, mensaje or _MENSAJES.get(codigo) or "Algo falló con Drive.", **kwargs)


# ---------------------------------------------------------------------------
# §8.2 Clase
# ---------------------------------------------------------------------------


class Drive:
    """Cliente de Google Drive (buscar, descargar, leer, crear carpetas y operaciones con tarjeta)."""

    def __init__(self, settings: Settings, cuenta: CuentaGoogle | None, bus: BusEventos | None = None, *,
                 reloj: Callable[[], float] = time.time, carpetas: Mapping[str, Path] | None = None) -> None:
        self._settings = settings
        self._cuenta = cuenta
        self._bus = bus
        self._reloj = reloj
        self._carpetas: dict[str, Path] | None = dict(carpetas) if carpetas is not None else None
        self._dormir: Callable[[float], Any] = asyncio.sleep   # los tests lo sustituyen
        self._lista: tuple[ArchivoDrive, ...] = ()
        self._lista_ts = 0.0
        self._operacion: OperacionDrive | None = None

    def __repr__(self) -> str:
        return f"Drive(disponible={self.disponible}, operacion={self._operacion is not None})"

    # ------------------------------------------------------------------ ciclo de vida y estado

    async def iniciar(self) -> DiagnosticoModulo:
        """Resuelve las carpetas del usuario (en un hilo) y devuelve el diagnóstico ``drive``."""
        try:
            await self._carpetas_usuario()
        except Exception as exc:  # contrato: nunca lanza
            log.debug("Carpetas del usuario no disponibles (%s)", type(exc).__name__)
        return self.diagnostico()

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico ``drive`` (§8.4)."""
        s = self._settings
        if not bool(getattr(s, "tiene_google", False)):
            detalle = "--offline" if bool(getattr(s, "offline", False)) and s.google_habilitado else "Google deshabilitado"
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.OFF, detalle)
        if "drive" not in tuple(getattr(s, "google_servicios", ()) or ()):
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.OFF, "servicio no pedido (RAMS_GOOGLE_SERVICIOS)")
        estado = self._estado_cuenta()
        if estado is None:
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.OFF, "sin la cuenta de Google")
        if getattr(estado, "motivo", "") == "sin_httpx":
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.OFF, "sin httpx")
        if not estado.conectado:
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.EN_ESPERA, "sin conectar")
        if not estado.drive_leer:
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "falta permiso de Drive: reconecta y marca Drive")
        if estado.drive_completo:
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.OK, "completo")
        if not estado.drive_escribir:
            return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.DEGRADADO,
                                     "solo lectura: reconecta y marca Drive para subir o crear")
        return DiagnosticoModulo("drive", NOMBRE_MODULO, EstadoModulo.OK, "leer todo + modificar lo que cree Amadeus")

    async def cerrar(self) -> None:
        """Olvida la última lista y la operación pendiente (viven solo en memoria)."""
        self._lista = ()
        self._operacion = None

    def _estado_cuenta(self) -> Any:
        if self._cuenta is None:
            return None
        try:
            return self._cuenta.estado()
        except Exception as exc:  # una cuenta rota cuenta como desconectada
            log.debug("Estado de la cuenta de Google ilegible (%s)", type(exc).__name__)
            return None

    def _habilitado(self) -> bool:
        s = self._settings
        return bool(getattr(s, "tiene_google", False)) and "drive" in tuple(getattr(s, "google_servicios", ()) or ())

    @property
    def disponible(self) -> bool:
        """Conectada con permiso de lectura de Drive (``drive_leer``)."""
        estado = self._estado_cuenta()
        return self._habilitado() and estado is not None and bool(estado.conectado and estado.drive_leer)

    @property
    def puede_escribir(self) -> bool:
        """Conectada con ``drive.file`` o ``drive`` (``drive_escribir``)."""
        estado = self._estado_cuenta()
        return self._habilitado() and estado is not None and bool(estado.conectado and estado.drive_escribir)

    @property
    def completo(self) -> bool:
        """Conectada con el scope ``drive`` completo."""
        estado = self._estado_cuenta()
        return self._habilitado() and estado is not None and bool(estado.conectado and estado.drive_completo)

    def _no_disponible(self, *, escribir: bool = False) -> ResultadoDrive | None:
        s = self._settings
        if not bool(getattr(s, "tiene_google", False)):
            return _fallo("deshabilitado")
        if "drive" not in tuple(getattr(s, "google_servicios", ()) or ()):
            return _fallo("deshabilitado", "Drive no está entre los servicios pedidos (RAMS_GOOGLE_SERVICIOS).")
        estado = self._estado_cuenta()
        if estado is None or not estado.conectado:
            return _fallo("desconectado")
        if not estado.drive_leer and not escribir:
            return _fallo("sin_permiso")
        if escribir and not estado.drive_escribir:
            return _fallo("sin_permiso", "Falta el permiso de Drive para crear o subir: reconecta y marca Drive.")
        return None

    async def _carpetas_usuario(self) -> dict[str, Path]:
        if self._carpetas is None:
            self._carpetas = await asyncio.to_thread(carpetas_usuario)
        return self._carpetas

    def _error(self, exc: ErrorGoogle, operacion: str) -> ResultadoDrive:
        """ResultadoDrive a partir de un :class:`ErrorGoogle` (403 en el modo por defecto → RAMS_DRIVE_COMPLETO)."""
        log.info("Drive %s: %s (HTTP %s)", operacion, exc.codigo, exc.http if exc.http is not None else "-")
        if (exc.codigo == "sin_permiso" and not self.completo
                and exc.razon in ("insufficientFilePermissions", "appNotAuthorizedToFile", "insufficientPermissions")):
            return _fallo("sin_permiso", _MENSAJE_COMPLETO)
        if exc.codigo == "no_exportable":
            return _fallo("no_exportable", "Es demasiado grande para exportarlo; ábrelo en el navegador.")
        return _fallo(exc.codigo, exc.mensaje)

    # ------------------------------------------------------------------ peticiones (solo por RUTAS_DRIVE)

    def _url(self, ruta: str, ident: str | None, url: str | None) -> tuple[str, str]:
        metodo, plantilla = RUTAS_DRIVE[ruta]
        if url is not None:
            partes = urlsplit(url)
            if not url.startswith(plantilla) or partes.hostname != "www.googleapis.com" or partes.scheme != "https":
                raise ErrorGoogle("error", "Destino de Drive no permitido.")
            return metodo, url
        if "{id}" in plantilla:
            if not isinstance(ident, str) or not _RE_ID.fullmatch(ident):
                raise ErrorGoogle("no_encontrado", "Ese archivo de Drive no es válido.")
            return metodo, plantilla.format(id=quote(ident, safe=""))
        return metodo, plantilla

    async def _peticion(self, ruta: str, *, ident: str | None = None, url: str | None = None,
                        params: Mapping[str, Any] | None = None, json: Any = None, contenido: bytes | None = None,
                        cabeceras: Mapping[str, str] | None = None, timeout: float = 30.0,
                        reintentar: bool = False) -> Any:
        """Única vía de peticiones: método y URL salen de ``RUTAS_DRIVE`` (lanza :class:`ErrorGoogle`)."""
        if self._cuenta is None:
            raise ErrorGoogle("desconectado", _MENSAJES["desconectado"])
        metodo, destino = self._url(ruta, ident, url)
        return await self._cuenta.solicitud(metodo, destino, params=params, json=json, contenido=contenido,
                                            cabeceras=cabeceras, timeout=timeout, reintentar=reintentar)

    def _flujo(self, ruta: str, *, ident: str, params: Mapping[str, Any] | None = None) -> Any:
        if self._cuenta is None:
            raise ErrorGoogle("desconectado", _MENSAJES["desconectado"])
        metodo, destino = self._url(ruta, ident, None)
        if metodo != "GET":
            raise ErrorGoogle("error", "Solo se descargan peticiones GET.")
        return self._cuenta.flujo(metodo, destino, params=params, timeout=60.0)

    async def _metadatos(self, ident: str, campos: str = CAMPOS_ARCHIVO) -> ArchivoDrive:
        respuesta = await self._peticion("obtener", ident=ident, params={"fields": campos}, reintentar=True)
        archivo = ArchivoDrive.desde_json(_json(respuesta))
        if archivo is None:
            raise ErrorGoogle("error", "Drive devolvió un archivo no válido.")
        return archivo

    # ------------------------------------------------------------------ última lista y referencias

    def _guardar_lista(self, archivos: Sequence[ArchivoDrive]) -> None:
        self._lista = tuple(archivos[:MAX_LISTA])
        self._lista_ts = self._reloj()

    def _lista_vigente(self) -> tuple[ArchivoDrive, ...]:
        if self._lista and self._reloj() - self._lista_ts <= TTL_LISTA_S:
            return self._lista
        self._lista = ()
        return ()

    def _en_lista(self, nombre: str) -> list[ArchivoDrive]:
        lista = self._lista_vigente()
        buscado = normalizar_titulo(nombre)
        if not buscado:
            return []
        exactos = [a for a in lista if normalizar_titulo(a.nombre) == buscado]
        if exactos:
            return exactos
        return [a for a in lista if normalizar_titulo(a.nombre).startswith(buscado)]

    def resolver_local(self, ref: str) -> ArchivoDrive | None:
        """Solo la última lista (≤ 30 min): número, ordinal o nombre (exacto o prefijo único)."""
        try:
            texto = sanear_texto(ref, multilinea=False)
            if not texto:
                return None
            lista = self._lista_vigente()
            numero = _numero_ref(texto)
            if numero is not None:
                indice = len(lista) if numero == -1 else numero
                return lista[indice - 1] if lista and 1 <= indice <= len(lista) else None
            coincidencias = self._en_lista(_quitar_articulos(texto))
            return coincidencias[0] if len(coincidencias) == 1 else None
        except Exception as exc:  # contrato: nunca lanza
            log.debug("Referencia de Drive no resoluble (%s)", type(exc).__name__)
            return None

    async def resolver(self, ref: str) -> ResultadoDrive:
        """Número, ordinal o nombre (última lista y, si no, búsqueda ``name = '…'`` en Drive). Nunca lanza."""
        fallo = self._no_disponible()
        if fallo is not None:
            return fallo
        try:
            return await self._resolver(ref)
        except ErrorGoogle as exc:
            return self._error(exc, "resolver")
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al resolver un archivo de Drive (%s)", type(exc).__name__)
            return _fallo("error")

    async def _resolver(self, ref: Any) -> ResultadoDrive:
        texto = sanear_texto(ref, multilinea=False)
        if not texto:
            return _fallo("no_existe", "¿Cuál archivo? Dime el número de la lista o el nombre.")
        numero = _numero_ref(texto)
        if numero is not None:
            lista = self._lista_vigente()
            if not lista:
                return _fallo("no_existe", _MENSAJES["sin_lista"])
            indice = len(lista) if numero == -1 else numero
            if not 1 <= indice <= len(lista):
                return _fallo("no_existe", f"La última lista tiene {len(lista)} archivos: elige un número del 1 al "
                                           f"{len(lista)}.")
            return ResultadoDrive(True, "hecho", "Archivo encontrado.", archivos=(lista[indice - 1],))
        nombre = _quitar_articulos(texto)[:MAX_CONSULTA]
        locales = self._en_lista(nombre)
        if len(locales) == 1:
            return ResultadoDrive(True, "hecho", "Archivo encontrado.", archivos=(locales[0],))
        if len(locales) > 1:
            return _fallo("ambiguo", archivos=tuple(locales))
        q = f"name = '{_escapar_q(nombre)}' and trashed = false"
        respuesta = await self._peticion("listar", params={"q": q, "pageSize": 10, "fields": CAMPOS_LISTA,
                                                           "spaces": "drive"}, reintentar=True)
        archivos = _archivos(respuesta)
        if not archivos:
            return _fallo("no_encontrado", "No encontré ese archivo en tu Drive.")
        if len(archivos) > 1:
            # La «última lista» NO cambia: el usuario no ha visto estos candidatos, así que «el 2» debe seguir
            # siendo el 2 de la lista que sí vio (si no, «resume el 2» mandaría a Gemini un documento que no eligió).
            # Para elegir, que busque: buscar() muestra la lista y la guarda.
            cuantos = str(len(archivos)) if len(archivos) < 10 else "10 o más"
            return _fallo("ambiguo", f"Hay {cuantos} archivos con ese nombre en tu Drive: búscalos primero "
                                     "(p. ej. «busca … en drive») y dime el número de la lista.",
                          archivos=tuple(archivos))
        return ResultadoDrive(True, "hecho", "Archivo encontrado.", archivos=(archivos[0],))

    async def _resolver_fresco(self, ref: str) -> tuple[ArchivoDrive | None, ResultadoDrive | None]:
        """Resuelve ``ref`` y vuelve a pedir sus metadatos (tamaño y permisos actuales)."""
        resultado = await self._resolver(ref)
        if not resultado.ok or not resultado.archivos:
            return None, resultado
        return await self._metadatos(resultado.archivos[0].id), None

    async def _buscar_carpeta(self, nombre: str) -> tuple[ArchivoDrive | None, str | None]:
        """(carpeta, problema): por nombre exacto, ``mimeType = folder and name = '…' and trashed = false``."""
        limpio = _quitar_articulos(nombre)[:MAX_CONSULTA]
        if not limpio:
            return None, "Falta el nombre de la carpeta de Drive."
        q = f"mimeType = '{MIME_CARPETA}' and name = '{_escapar_q(limpio)}' and trashed = false"
        respuesta = await self._peticion("listar", params={"q": q, "pageSize": 10, "fields": CAMPOS_LISTA,
                                                           "spaces": "drive"}, reintentar=True)
        carpetas = [a for a in _archivos(respuesta) if a.es_carpeta]
        if not carpetas:
            return None, f"No encontré la carpeta «{recortar_aviso(limpio, 60)}» en tu Drive."
        if len(carpetas) > 1:
            return None, f"Hay {len(carpetas)} carpetas «{recortar_aviso(limpio, 60)}» en tu Drive: renombra una."
        return carpetas[0], None

    # ------------------------------------------------------------------ buscar e info

    async def buscar(self, consulta: str | None, *, carpeta: str | None = None, limite: int = 10) -> ResultadoDrive:
        """Busca (``trashed = false`` siempre) y guarda la «última lista». Sin consulta: los más recientes."""
        fallo = self._no_disponible()
        if fallo is not None:
            return fallo
        try:
            limite = max(1, min(MAX_LISTA, int(limite)))
        except (TypeError, ValueError):
            limite = 10
        try:
            texto = sanear_texto(consulta, multilinea=False)[:MAX_CONSULTA].strip() if consulta else ""
            condiciones: list[str] = []
            if texto:
                valor = _escapar_q(texto)
                condiciones.append(f"(name contains '{valor}' or fullText contains '{valor}')")
            if carpeta:
                encontrada, problema = await self._buscar_carpeta(carpeta)
                if encontrada is None:
                    return _fallo("no_encontrado", problema)
                condiciones.append(f"'{encontrada.id}' in parents")
            condiciones.append("trashed = false")
            params: dict[str, Any] = {"q": " and ".join(condiciones), "pageSize": limite, "fields": CAMPOS_LISTA,
                                      "spaces": "drive"}
            if not texto:
                params["orderBy"] = "modifiedTime desc"     # nunca con fullText (§8.3)
            respuesta = await self._peticion("listar", params=params, reintentar=True)
        except ErrorGoogle as exc:
            return self._error(exc, "buscar")
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al buscar en Drive (%s)", type(exc).__name__)
            return _fallo("error")
        archivos = [a for a in _archivos(respuesta)][:limite]
        datos_respuesta = _json(respuesta)
        incompleta = isinstance(datos_respuesta, Mapping) and datos_respuesta.get("incompleteSearch") is True
        self._guardar_lista(archivos)
        log.info("Drive buscar: %d archivos", len(archivos))
        if not archivos:
            return ResultadoDrive(True, "hecho", "No encontré nada en tu Drive.", datos={"lista": "", "incompleta": incompleta})
        lista = "\n".join(a.linea(i) for i, a in enumerate(archivos, start=1))
        mensaje = f"Encontré {len(archivos)} archivo{'s' if len(archivos) != 1 else ''} en tu Drive."
        return ResultadoDrive(True, "hecho", mensaje, archivos=tuple(archivos),
                              datos={"lista": lista, "incompleta": incompleta})

    async def info(self, ref: str) -> ResultadoDrive:
        """Detalles de un archivo: nombre, tipo, tamaño, fechas, dueño, enlace y qué se puede hacer."""
        fallo = self._no_disponible()
        if fallo is not None:
            return fallo
        try:
            resultado = await self._resolver(ref)
            if not resultado.ok:
                return resultado
            respuesta = await self._peticion("obtener", ident=resultado.archivos[0].id,
                                             params={"fields": CAMPOS_INFO}, reintentar=True)
        except ErrorGoogle as exc:
            return self._error(exc, "info")
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al pedir detalles a Drive (%s)", type(exc).__name__)
            return _fallo("error")
        datos = _json(respuesta)
        archivo = ArchivoDrive.desde_json(datos)
        if archivo is None:
            return _fallo("error", "Drive devolvió un archivo no válido.")
        creado = datos.get("createdTime") if isinstance(datos, Mapping) else None
        puede = [texto for ok, texto in ((archivo.puede_descargar, "descargarlo"),
                                          (archivo.puede_papelera, "moverlo a la papelera"),
                                          (archivo.puede_compartir, "compartirlo")) if ok]
        lineas = [f"Nombre: {recortar_aviso(archivo.nombre, 200)}", f"Tipo: {archivo.tipo_legible()}",
                  f"Tamaño: {_tamano_legible(archivo.tamano)}" if not archivo.es_google else "Tamaño: —",
                  f"Modificado: {_fecha_legible(archivo.modificado, con_anio=True) or '—'}"]
        if isinstance(creado, str):
            lineas.append(f"Creado: {_fecha_legible(creado, con_anio=True) or '—'}")
        lineas.append("Dueño: tú" if archivo.propio else "Dueño: otra persona (compartido contigo)")
        if archivo.enlace:
            lineas.append(f"Enlace: {archivo.enlace}")
        lineas.append("Puedes: " + (", ".join(puede) if puede else "solo verlo"))
        datos_resultado: dict[str, Any] = {"detalle": "\n".join(lineas), "archivo_id": archivo.id}
        if archivo.enlace:
            datos_resultado["enlace"] = archivo.enlace
        log.info("Drive info: hecho")
        return ResultadoDrive(True, "hecho", "Te dejé los detalles del archivo en pantalla.", archivos=(archivo,),
                              datos=datos_resultado)

    # ------------------------------------------------------------------ descargar y leer

    async def _destino(self, destino: str) -> tuple[Path, str] | ResultadoDrive:
        configurada = getattr(self._settings, "drive_descargas_dir", None)
        if destino == "descargas" and configurada:
            ruta = Path(configurada)
            try:
                await asyncio.to_thread(ruta.mkdir, parents=True, exist_ok=True)
            except OSError as exc:
                log.info("Carpeta de descargas de Drive no disponible (%s)", type(exc).__name__)
                return _fallo("error", "No pude usar tu carpeta de descargas de Drive (RAMS_DRIVE_DESCARGAS).")
            return ruta, (ruta.name or NOMBRES_CARPETA["descargas"])
        carpetas = await self._carpetas_usuario()
        ruta_usuario = carpetas.get(destino)
        if ruta_usuario is None:
            return _fallo("error", f"No encontré tu carpeta {NOMBRES_CARPETA.get(destino, destino)}.")
        return Path(ruta_usuario), NOMBRES_CARPETA.get(destino, destino)

    async def descargar(self, ref: str, *, destino: str = "descargas", formato: str | None = None) -> ResultadoDrive:
        """Descarga (binarios) o exporta (Docs, Sheets, Slides) a una carpeta del usuario, sin sobrescribir."""
        fallo = self._no_disponible()
        if fallo is not None:
            return fallo
        if destino not in DESTINOS_DESCARGA:
            return _fallo("prohibido", "Solo descargo en Descargas, Documentos o Escritorio.")
        if formato is not None and formato not in FORMATOS_DRIVE:
            return _fallo("prohibido", "Formatos posibles: pdf, texto u original.")
        try:
            return await self._descargar(ref, destino, formato)
        except ErrorGoogle as exc:
            return self._error(exc, "descargar")
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al descargar de Drive (%s)", type(exc).__name__)
            return _fallo("error")

    async def _descargar(self, ref: str, destino: str, formato: str | None) -> ResultadoDrive:
        archivo, fallo = await self._resolver_fresco(ref)
        if archivo is None:
            return fallo or _fallo("no_encontrado")
        if archivo.es_carpeta:
            return _fallo("no_soportado", "Es una carpeta: no se puede descargar entera.")
        tope = max(1, int(getattr(self._settings, "drive_max_descarga_mb", 200))) * _MIB
        nota = ""
        if archivo.es_google:
            opciones = _EXPORTAR.get(archivo.tipo_mime)
            if opciones is None:
                return _fallo("no_exportable", "Ese tipo de archivo de Google no se puede descargar.")
            mime, extension, nota = opciones[formato or "pdf"]
            ruta_api, params = "exportar", {"mimeType": mime}
            tope = min(tope, MAX_EXPORTACION + _MIB)
        else:
            if not archivo.puede_descargar:
                return _fallo("no_descargable", "El dueño no permite descargar ese archivo.")
            if archivo.tamano is not None and archivo.tamano > tope:
                return _fallo("demasiado_grande", f"Pesa {_tamano_legible(archivo.tamano)} y el tope es "
                                                  f"{_tamano_legible(tope)} (RAMS_DRIVE_MAX_DESCARGA_MB).")
            if formato not in (None, "original"):
                nota = "No es un documento de Google: lo descargué tal cual."
            extension = ""
            ruta_api, params = "obtener", {"alt": "media"}
        lugar = await self._destino(destino)
        if isinstance(lugar, ResultadoDrive):
            return lugar
        directorio, etiqueta = lugar
        nombre = sanear_nombre_archivo(archivo.nombre, extension)
        escritor = _EscritorParcial(directorio, nombre, marcar_origen=not archivo.propio)
        total = 0
        try:
            await asyncio.to_thread(escritor.abrir)
            async with self._flujo(ruta_api, ident=archivo.id, params=params) as respuesta:
                async for bloque in respuesta.aiter_bytes(TAM_BLOQUE_DESCARGA):
                    total += len(bloque)
                    if total > tope:
                        raise _Demasiado()
                    await asyncio.to_thread(escritor.escribir, bloque)
            final = await asyncio.to_thread(escritor.terminar)
        except _Demasiado:
            await asyncio.to_thread(escritor.abortar)
            return _fallo("demasiado_grande", f"Pasa del tope de {_tamano_legible(tope)} (RAMS_DRIVE_MAX_DESCARGA_MB).")
        except BaseException:
            await asyncio.shield(asyncio.to_thread(escritor.abortar))
            raise
        guardado = f"{etiqueta}/{final}"
        datos: dict[str, Any] = {"guardado": guardado, "bytes": total, "archivo_id": archivo.id}
        if nota:
            datos["nota"] = nota
        log.info("Drive descargar: %d bytes", total)
        return ResultadoDrive(True, "hecho", f"Lo guardé en {guardado} ({_tamano_legible(total)}).",
                              archivos=(archivo,), datos=datos)

    async def leer_texto(self, ref: str, *, max_caracteres: int = 30_000) -> ResultadoDrive:
        """Texto de un documento para resumirlo (solo a pedido). PDF, docx y otros binarios → ``no_soportado``."""
        fallo = self._no_disponible()
        if fallo is not None:
            return fallo
        try:
            maximo = max(1, min(MAX_LEER_CARACTERES, int(max_caracteres)))
        except (TypeError, ValueError):
            maximo = 30_000
        try:
            return await self._leer_texto(ref, maximo)
        except ErrorGoogle as exc:
            return self._error(exc, "leer")
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al leer de Drive (%s)", type(exc).__name__)
            return _fallo("error")

    async def _leer_texto(self, ref: str, maximo: int) -> ResultadoDrive:
        archivo, fallo = await self._resolver_fresco(ref)
        if archivo is None:
            return fallo or _fallo("no_encontrado")
        nota = ""
        if archivo.tipo_mime in _LEER:
            mime, nota = _LEER[archivo.tipo_mime]
            ruta_api, params, tope = "exportar", {"mimeType": mime}, MAX_EXPORTACION
        elif (not archivo.es_google and not archivo.es_carpeta
              and (archivo.tipo_mime in _MIMES_TEXTO or archivo.nombre.lower().endswith(_EXTENSIONES_TEXTO))):
            if archivo.tamano is not None and archivo.tamano > MAX_LECTURA_BINARIA:
                return _fallo("no_soportado", "Es demasiado largo para leerlo aquí: descárgalo y ábrelo.")
            ruta_api, params, tope = "obtener", {"alt": "media"}, MAX_LECTURA_BINARIA
        else:
            return _fallo("no_soportado")
        tope = min(tope, maximo * 4 + 4)
        crudo = bytearray()
        recortado = False
        async with self._flujo(ruta_api, ident=archivo.id, params=params) as respuesta:
            async for bloque in respuesta.aiter_bytes(TAM_BLOQUE_DESCARGA):
                crudo.extend(bloque)
                if len(crudo) >= tope:
                    recortado = True
                    break
        texto = sanear_texto(bytes(crudo[:tope]).decode("utf-8", "replace").lstrip("﻿"), multilinea=True)
        if len(texto) > maximo:
            texto = texto[:maximo]
            recortado = True
        datos: dict[str, Any] = {"texto": texto, "recortado": recortado, "archivo_id": archivo.id}
        if nota:
            datos["nota"] = nota
        log.info("Drive leer: %d caracteres%s", len(texto), " (recortado)" if recortado else "")
        return ResultadoDrive(True, "hecho", "Leí el documento.", archivos=(archivo,), datos=datos)

    # ------------------------------------------------------------------ crear carpeta

    async def crear_carpeta(self, nombre: str, *, dentro: str | None = None) -> ResultadoDrive:
        """Crea una carpeta (en la raíz o dentro de ``dentro``, que debe existir y ser única)."""
        fallo = self._no_disponible(escribir=True)
        if fallo is not None:
            return fallo
        limpio = sanear_texto(nombre, multilinea=False) if isinstance(nombre, str) else ""
        if (not limpio or len(limpio) > MAX_NOMBRE_CARPETA or "/" in limpio or "\\" in limpio
                or _RE_CONTROL.search(str(nombre))):
            return _fallo("prohibido", "El nombre de la carpeta debe tener de 1 a 120 caracteres, sin «/» ni «\\».")
        try:
            cuerpo: dict[str, Any] = {"name": limpio, "mimeType": MIME_CARPETA}
            if dentro:
                padre, problema = await self._buscar_carpeta(dentro)
                if padre is None:
                    return _fallo("no_encontrado", problema)
                cuerpo["parents"] = [padre.id]
            respuesta = await self._peticion("crear", params={"fields": "id,name,webViewLink"}, json=cuerpo)
        except ErrorGoogle as exc:
            return self._error(exc, "crear_carpeta")
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al crear una carpeta en Drive (%s)", type(exc).__name__)
            return _fallo("error")
        datos = _json(respuesta)
        resultado: dict[str, Any] = {}
        if isinstance(datos, Mapping):
            if isinstance(datos.get("id"), str) and _RE_ID.fullmatch(datos["id"]):
                resultado["archivo_id"] = datos["id"]
            enlace = datos.get("webViewLink")
            if isinstance(enlace, str) and enlace.startswith("https://") and not _RE_CONTROL.search(enlace):
                resultado["enlace"] = enlace
        log.info("Drive crear_carpeta: hecho")
        return ResultadoDrive(True, "hecho", "Creé la carpeta en tu Drive.", datos=resultado)

    # ------------------------------------------------------------------ operaciones con tarjeta

    def _nueva_operacion(self, op: str, *, canal_origen: str, ttl_s: float, problemas: Sequence[str],
                         archivo: ArchivoDrive | None = None, local: Any = None,
                         carpeta_destino: ArchivoDrive | None = None, compartir_con: str | None = None,
                         rol: str | None = None, publico: bool = False) -> OperacionDrive:
        ahora = self._reloj()
        ttl = _ttl_valido(ttl_s)
        operacion = OperacionDrive(
            id=uuid.uuid4().hex[:8], version=1, op=op, archivo=archivo,
            local_nombre=local.nombre if local is not None else None,
            local_carpeta=local.carpeta if local is not None else None,
            local_bytes=local.bytes if local is not None else None,
            local_sha256=local.sha256 if local is not None else None,
            local_ruta=local.ruta if local is not None else None,
            carpeta_destino=carpeta_destino, compartir_con=compartir_con, rol=rol, publico=bool(publico),
            problemas=_acotar_problemas(problemas), canal_origen=canal_origen if canal_origen in CANALES_ORIGEN
            else "terminal", creado=ahora, caduca=ahora + ttl)
        self._operacion = operacion
        log.info("Drive: propuesta de %s preparada (%d problemas)", op, len(operacion.problemas))
        return operacion

    async def preparar_subida(self, nombre_local: str, *, carpeta: str | None, canal_origen: str,
                              ttl_s: float) -> OperacionDrive:
        """Propuesta de subida: el archivo se resuelve como los adjuntos, pero en todas las carpetas del usuario."""
        problemas: list[str] = []
        local = None
        destino: ArchivoDrive | None = None
        try:
            maximo = max(1, int(getattr(self._settings, "drive_max_subida_mb", 100))) * _MIB
            carpetas = await self._carpetas_usuario()
            prohibidas = (Path(self._settings.base_dir), Path(self._settings.data_dir))
            busqueda = await asyncio.to_thread(buscar_archivo_local, nombre_local, carpetas, CARPETAS_USUARIO,
                                               max_bytes=maximo, prohibidas=prohibidas)
            if busqueda.archivo is None:
                problemas.append(busqueda.problema or "No encontré ese archivo en tus carpetas.")
            elif largo_utf16(busqueda.archivo.nombre) > MAX_NOMBRE_LOCAL:
                problemas.append("El nombre del archivo pasa de 255 caracteres: renómbralo.")
            else:
                local = busqueda.archivo
            fallo = self._no_disponible(escribir=True)
            if fallo is not None:
                problemas.append(fallo.mensaje)
            elif carpeta:
                destino, problema = await self._buscar_carpeta(carpeta)
                if problema:
                    problemas.append(problema)
        except ErrorGoogle as exc:
            problemas.append(self._error(exc, "preparar_subida").mensaje)
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al preparar una subida (%s)", type(exc).__name__)
            problemas.append("No pude preparar la subida: inténtalo de nuevo.")
        return self._nueva_operacion("subir", canal_origen=canal_origen, ttl_s=ttl_s, problemas=problemas,
                                     local=local, carpeta_destino=destino)

    async def preparar_papelera(self, ref: str, *, canal_origen: str, ttl_s: float) -> OperacionDrive:
        """Propuesta de mover a la papelera (nunca borrado definitivo)."""
        problemas: list[str] = []
        archivo: ArchivoDrive | None = None
        try:
            fallo = self._no_disponible(escribir=True)
            if fallo is not None:
                problemas.append(fallo.mensaje)
            else:
                archivo, resultado = await self._resolver_fresco(ref)
                if archivo is None:
                    problemas.append((resultado.mensaje if resultado else None) or "No encontré ese archivo.")
                elif not archivo.puede_papelera:
                    problemas.append("No puedo moverlo a la papelera: no eres dueño o no lo creé yo." +
                                     ("" if self.completo else " Para archivos ajenos, RAMS_DRIVE_COMPLETO=1."))
        except ErrorGoogle as exc:
            problemas.append(self._error(exc, "preparar_papelera").mensaje)
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al preparar la papelera (%s)", type(exc).__name__)
            problemas.append("No pude preparar la operación: inténtalo de nuevo.")
        return self._nueva_operacion("papelera", canal_origen=canal_origen, ttl_s=ttl_s, problemas=problemas,
                                     archivo=archivo)

    async def preparar_compartir(self, ref: str, *, correo: str | None, rol: str, publico: bool,
                                 canal_origen: str, ttl_s: float) -> OperacionDrive:
        """Propuesta de compartir. Público y editable → problema que bloquea (nunca)."""
        problemas: list[str] = []
        archivo: ArchivoDrive | None = None
        publico = publico is True
        rol_valido = rol if rol in ROLES_DRIVE else None
        con: str | None = None
        if rol_valido is None:
            problemas.append("Rol no válido: lector, comentarista o editor.")
        if publico and rol != "reader":
            problemas.append("Público y editable, nunca: en público solo comparto de lectura.")
        if not publico:
            direccion = correo.strip() if isinstance(correo, str) else ""
            if ("\r" in direccion or "\n" in direccion or len(direccion) > 254
                    or not CORREO_RE.fullmatch(direccion)):
                problemas.append("Falta un correo válido para compartir (p. ej. «ana@x.com»).")
            else:
                con = direccion
        try:
            fallo = self._no_disponible(escribir=True)
            if fallo is not None:
                problemas.append(fallo.mensaje)
            else:
                archivo, resultado = await self._resolver_fresco(ref)
                if archivo is None:
                    problemas.append((resultado.mensaje if resultado else None) or "No encontré ese archivo.")
                elif not archivo.puede_compartir:
                    problemas.append("No puedo compartirlo: no tienes permiso para compartir ese archivo." +
                                     ("" if self.completo else " Si no lo creé yo, RAMS_DRIVE_COMPLETO=1."))
        except ErrorGoogle as exc:
            problemas.append(self._error(exc, "preparar_compartir").mensaje)
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al preparar compartir (%s)", type(exc).__name__)
            problemas.append("No pude preparar la operación: inténtalo de nuevo.")
        return self._nueva_operacion("compartir", canal_origen=canal_origen, ttl_s=ttl_s, problemas=problemas,
                                     archivo=archivo, compartir_con=con, rol=rol_valido or "reader", publico=publico)

    def operacion(self, id: str) -> OperacionDrive | None:
        """La operación vigente con esa id (``None`` si no existe o caducó)."""
        op = self._operacion
        if op is None or not isinstance(id, str) or op.id != id:
            return None
        if self._reloj() >= op.caduca:
            self._operacion = None
            return None
        return op

    def descartar(self, id: str) -> bool:
        """Olvida la operación con esa id. ``True`` si existía."""
        op = self._operacion
        if op is not None and isinstance(id, str) and op.id == id:
            self._operacion = None
            return True
        return False

    async def ejecutar(self, id: str, huella: str) -> ResultadoDrive:
        """Ejecuta la operación aprobada (tras la compuerta). Vuelve a comprobar la huella y el archivo local."""
        op = self._operacion
        if op is None or not isinstance(id, str) or op.id != id:
            return _fallo("no_existe")
        if self._reloj() >= op.caduca:
            self._operacion = None
            return _fallo("caducado")
        actual = op.huella()
        if not actual or not isinstance(huella, str) or not _iguales(actual, huella):
            return _fallo("huella")
        if not op.listo:
            return _fallo("prohibido", "La operación tiene problemas pendientes: revísalos en la tarjeta.")
        fallo = self._no_disponible(escribir=True)
        if fallo is not None:
            return fallo
        try:
            if op.op == "subir":
                resultado = await self._subir(op)
            elif op.op == "papelera":
                resultado = await self._papelera(op)
            elif op.op == "compartir":
                resultado = await self._compartir(op)
            else:
                resultado = _fallo("prohibido")
        except ErrorGoogle as exc:
            resultado = self._error(exc, op.op)
        except Exception as exc:  # contrato: nunca lanza
            log.warning("Fallo inesperado al ejecutar una operación de Drive (%s)", type(exc).__name__)
            resultado = _fallo("error")
        if resultado.ok and self._operacion is not None and self._operacion.id == op.id:
            self._operacion = None    # consumida; ante un fallo se conserva
        log.info("Drive %s: %s", op.op, resultado.codigo)
        return resultado

    async def _papelera(self, op: OperacionDrive) -> ResultadoDrive:
        assert op.archivo is not None
        await self._peticion("actualizar", ident=op.archivo.id, params={"fields": "id,trashed"},
                             json={"trashed": True}, reintentar=True)
        return ResultadoDrive(True, "hecho", "Lo moví a la papelera de Drive (se puede restaurar durante 30 días).",
                              archivos=(op.archivo,), datos={"archivo_id": op.archivo.id})

    async def _compartir(self, op: OperacionDrive) -> ResultadoDrive:
        assert op.archivo is not None
        if op.publico:
            if op.rol != "reader":
                return _fallo("prohibido", "Público y editable, nunca.")
            cuerpo: dict[str, Any] = {"type": "anyone", "role": "reader"}
        else:
            if not op.compartir_con or not CORREO_RE.fullmatch(op.compartir_con) or op.rol not in ROLES_DRIVE:
                return _fallo("prohibido")
            cuerpo = {"type": "user", "role": op.rol, "emailAddress": op.compartir_con}
        await self._peticion("compartir", ident=op.archivo.id, params={"fields": "id"}, json=cuerpo)
        datos: dict[str, Any] = {"archivo_id": op.archivo.id}
        try:
            respuesta = await self._peticion("obtener", ident=op.archivo.id, params={"fields": "webViewLink"},
                                             reintentar=True)
            enlace = _json(respuesta).get("webViewLink") if isinstance(_json(respuesta), Mapping) else None
            if isinstance(enlace, str) and enlace.startswith("https://") and not _RE_CONTROL.search(enlace):
                datos["enlace"] = enlace
        except ErrorGoogle as exc:  # ya está compartido: el enlace es opcional
            log.debug("Enlace de Drive no disponible (%s)", exc.codigo)
        rol = _ROLES_LEGIBLES.get(op.rol or "reader", "lector")
        mensaje = ("Listo: cualquiera con el enlace puede verlo (solo lectura)." if op.publico
                   else f"Listo: compartido como {rol} (Google le avisa por correo).")
        if "enlace" in datos:
            mensaje += f" Enlace: {datos['enlace']}"
        return ResultadoDrive(True, "hecho", mensaje, archivos=(op.archivo,), datos=datos)

    async def _subir(self, op: OperacionDrive) -> ResultadoDrive:
        assert op.local_ruta is not None and op.local_sha256 is not None and op.local_bytes is not None
        maximo = max(1, int(getattr(self._settings, "drive_max_subida_mb", 100))) * _MIB
        if op.local_bytes > maximo:
            return _fallo("demasiado_grande", "Pasa del tope de subida (RAMS_DRIVE_MAX_SUBIDA_MB).")
        metadatos: dict[str, Any] = {"name": op.local_nombre}
        if op.carpeta_destino is not None:
            metadatos["parents"] = [op.carpeta_destino.id]
        tipo = mimetypes.guess_type(op.local_nombre or "")[0] or "application/octet-stream"
        if op.local_bytes <= UMBRAL_SUBIDA_SIMPLE:
            datos = await asyncio.to_thread(leer_archivo_verificado, op.local_ruta, op.local_sha256, maximo)
            if datos is None or len(datos) != op.local_bytes:
                return _fallo("archivo_cambio")
            frontera = "rams-" + secrets.token_hex(16)
            while frontera.encode("ascii") in datos:
                frontera = "rams-" + secrets.token_hex(16)
            cuerpo = (f"--{frontera}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("ascii")
                      + jsonlib.dumps(metadatos, ensure_ascii=False).encode("utf-8")
                      + f"\r\n--{frontera}\r\nContent-Type: {tipo}\r\n\r\n".encode("ascii")
                      + datos + f"\r\n--{frontera}--\r\n".encode("ascii"))
            respuesta = await self._peticion(
                "subir", params={"uploadType": "multipart", "fields": "id,name,webViewLink"}, contenido=cuerpo,
                cabeceras={"Content-Type": f"multipart/related; boundary={frontera}"}, timeout=120.0,
                reintentar=False)   # nunca se reintenta: podría duplicar el archivo
        else:
            respuesta = await self._subir_reanudable(op, metadatos, tipo)
            if isinstance(respuesta, ResultadoDrive):
                return respuesta
        datos_archivo = _json(respuesta)
        resultado: dict[str, Any] = {"bytes": op.local_bytes}
        if isinstance(datos_archivo, Mapping):
            if isinstance(datos_archivo.get("id"), str) and _RE_ID.fullmatch(datos_archivo["id"]):
                resultado["archivo_id"] = datos_archivo["id"]
            enlace = datos_archivo.get("webViewLink")
            if isinstance(enlace, str) and enlace.startswith("https://") and not _RE_CONTROL.search(enlace):
                resultado["enlace"] = enlace
        return ResultadoDrive(True, "hecho", f"Subido a tu Drive ({_tamano_legible(op.local_bytes)}).", datos=resultado)

    async def _subir_reanudable(self, op: OperacionDrive, metadatos: Mapping[str, Any], tipo: str) -> Any:
        """Subida reanudable: bloques múltiplos de 256 KiB, 308/``Range`` y ≤ 3 reanudaciones tras 5xx.

        Anti-TOCTOU: cada byte se lee del disco **una sola vez**, en orden, y se hashea al leerlo. Lo que Drive no
        confirmó (308 con un ``Range`` corto, o la consulta ``bytes */total`` tras un 5xx) se reenvía desde la
        copia en memoria del último bloque, nunca releyendo el disco: así lo que recibe Drive es exactamente lo
        hasheado. Si Drive pide volver a un punto anterior a esa copia, se aborta. Si al llegar al último bloque el
        sha256 no coincide, ese bloque **no** se envía y la sesión queda sin completar (Drive no crea el archivo).
        """
        assert op.local_ruta is not None and op.local_bytes is not None and op.local_sha256 is not None
        total = op.local_bytes
        bloque = max(256 * 1024, (int(TAM_BLOQUE_SUBIDA) // (256 * 1024)) * 256 * 1024)
        inicio = await self._peticion(
            "subir", params={"uploadType": "resumable", "fields": "id,name,webViewLink"}, json=dict(metadatos),
            cabeceras={"X-Upload-Content-Type": tipo, "X-Upload-Content-Length": str(total)}, reintentar=False)
        sesion = inicio.headers.get("Location") if hasattr(inicio, "headers") else None
        if not isinstance(sesion, str):
            return _fallo("error", "Drive no abrió la sesión de subida: empieza de nuevo.")
        self._url("subir_sesion", None, sesion)   # valida el destino (lanza si no es de Drive)
        archivo = await asyncio.to_thread(_abrir_lectura, op.local_ruta)
        try:
            resumen = hashlib.sha256()
            hasheado = 0               # bytes [0, hasheado) ya leídos y hasheados (una sola vez, en orden)
            copia = b""                # bytes [copia_inicio, hasheado): lo hasheado que Drive aún puede pedir
            copia_inicio = 0
            desplazamiento = 0
            reanudaciones = 0
            vueltas = 0
            while True:
                vueltas += 1
                if vueltas > total // bloque + 4 * (MAX_REANUDACIONES + 2):
                    return _fallo("error", "La subida no avanzaba: empieza de nuevo.")
                if not copia_inicio <= desplazamiento <= hasheado:
                    return _fallo("error", "La subida se desordenó: empieza de nuevo.")
                if desplazamiento >= total:
                    # Drive confirmó todo (lo último salió tras comprobar el sha256) sin cerrar: se pide el final.
                    final = await self._estado_sesion(sesion, total)
                    if isinstance(final, int):
                        return _fallo("error", "La subida no se completó: empieza de nuevo.")
                    return final
                tamano = min(bloque, total - desplazamiento)
                datos = copia[desplazamiento - copia_inicio:]    # lo ya hasheado sale de la memoria, no del disco
                faltan = tamano - len(datos)
                if faltan < 0:
                    return _fallo("error", "La subida se desordenó: empieza de nuevo.")
                if faltan:
                    nuevos = await asyncio.to_thread(_leer_en, archivo, hasheado, faltan)
                    if len(nuevos) != faltan:
                        return _fallo("archivo_cambio")
                    resumen.update(nuevos)
                    hasheado += faltan
                    datos += nuevos
                copia, copia_inicio = datos, desplazamiento      # [desplazamiento, hasheado)
                if desplazamiento + tamano >= total and not _iguales(resumen.hexdigest(), op.local_sha256):
                    log.info("Drive subir: el archivo cambió durante la subida; no envío el último bloque")
                    return _fallo("archivo_cambio")
                rango = f"bytes {desplazamiento}-{desplazamiento + tamano - 1}/{total}"
                try:
                    respuesta = await self._peticion("subir_sesion", url=sesion, contenido=datos,
                                                     cabeceras={"Content-Range": rango}, timeout=120.0)
                except ErrorGoogle as exc:
                    if exc.codigo == "no_encontrado":
                        return _fallo("error", "La sesión de subida caducó: empieza de nuevo.")
                    if exc.codigo in ("servidor", "red", "tiempo_agotado") and reanudaciones < MAX_REANUDACIONES:
                        reanudaciones += 1
                        log.info("Drive subir: reanudo (%d/%d)", reanudaciones, MAX_REANUDACIONES)
                        await self._dormir(float(2 ** (reanudaciones - 1)))
                        estado = await self._estado_sesion(sesion, total)
                        if isinstance(estado, int):
                            desplazamiento = estado
                            continue
                        return estado
                    raise
                if respuesta.status_code in (200, 201):
                    return respuesta
                if respuesta.status_code == 308:
                    nuevo = _rango_recibido(respuesta)
                    if nuevo <= desplazamiento:
                        reanudaciones += 1
                        if reanudaciones > MAX_REANUDACIONES:
                            return _fallo("error", "La subida no avanzaba: empieza de nuevo.")
                    desplazamiento = nuevo
                    continue
                return _fallo("error", "Drive respondió algo inesperado a la subida: empieza de nuevo.")
        finally:
            await asyncio.to_thread(archivo.close)

    async def _estado_sesion(self, sesion: str, total: int) -> Any:
        """Consulta ``bytes */total``: devuelve el desplazamiento (int) o la respuesta final del archivo."""
        respuesta = await self._peticion("subir_sesion", url=sesion, contenido=b"",
                                         cabeceras={"Content-Range": f"bytes */{total}"}, timeout=60.0)
        if respuesta.status_code in (200, 201):
            return respuesta
        if respuesta.status_code == 308:
            return _rango_recibido(respuesta)
        return _fallo("error", "Drive respondió algo inesperado a la subida: empieza de nuevo.")


# ---------------------------------------------------------------------------
# Utilidades de E/S (síncronas, se llaman en to_thread)
# ---------------------------------------------------------------------------


class _Demasiado(Exception):
    """La descarga pasó del tope."""


class _EscritorParcial:
    """Escribe en ``<destino>/<nombre>.part`` y al terminar hace ``os.replace`` sin sobrescribir nada.

    Con ``marcar_origen`` (archivos ajenos) y en Windows, el ``.part`` lleva desde el principio la marca de origen
    (``Zone.Identifier``, zona Internet), que ``os.replace`` conserva: el archivo final nunca existe sin ella.
    """

    def __init__(self, directorio: Path, nombre: str, *, marcar_origen: bool = False) -> None:
        self._directorio = directorio
        self._nombre = nombre
        self._marcar_origen = marcar_origen
        self._parcial: Path | None = None
        self._archivo: BinaryIO | None = None

    def abrir(self) -> None:
        if not self._directorio.is_dir():
            raise FileNotFoundError("carpeta de destino inexistente")
        nombre = _nombre_libre(self._directorio, self._nombre)
        self._nombre = nombre
        self._parcial = self._directorio / f"{nombre}.part"
        self._archivo = open(self._parcial, "xb")
        if self._marcar_origen:
            _marcar_origen_internet(self._parcial)

    def escribir(self, bloque: bytes) -> None:
        assert self._archivo is not None
        self._archivo.write(bloque)

    def terminar(self) -> str:
        assert self._archivo is not None and self._parcial is not None
        self._archivo.flush()
        os.fsync(self._archivo.fileno())
        self._archivo.close()
        self._archivo = None
        final = self._nombre
        if (self._directorio / final).exists():
            final = _nombre_libre(self._directorio, final)
        os.replace(self._parcial, self._directorio / final)
        self._parcial = None
        return final

    def abortar(self) -> None:
        if self._archivo is not None:
            with contextlib.suppress(OSError):
                self._archivo.close()
            self._archivo = None
        if self._parcial is not None:
            with contextlib.suppress(OSError):
                self._parcial.unlink()
            self._parcial = None


def _nombre_libre(directorio: Path, nombre: str) -> str:
    """``nombre`` o « (1)», « (2)»… para no sobrescribir (ni chocar con un ``.part``)."""
    def libre(candidato: str) -> bool:
        return not (directorio / candidato).exists() and not (directorio / f"{candidato}.part").exists()

    if libre(nombre):
        return nombre
    raiz, punto, extension = nombre.rpartition(".")
    if not punto or not raiz:
        raiz, extension = nombre, ""
    else:
        extension = "." + extension
    for numero in range(1, 10_000):
        candidato = f"{raiz} ({numero}){extension}"
        if libre(candidato):
            return candidato
    return f"{raiz} ({uuid.uuid4().hex[:6]}){extension}"


def _marcar_origen_internet(ruta: Path) -> bool:
    """Escribe el flujo alternativo ``Zone.Identifier`` (ZoneId=3) en Windows. ``False`` fuera de Windows o si el
    volumen no admite flujos (FAT32, exFAT, red): la descarga sigue igual. Síncrona; nunca lanza."""
    if sys.platform != "win32":
        return False
    try:
        with open(f"{ruta}:Zone.Identifier", "w", encoding="ascii", newline="") as flujo:
            flujo.write(_ZONA_INTERNET)
        return True
    except OSError as exc:
        log.debug("Sin marca de origen en la descarga (%s)", type(exc).__name__)
        return False


def _abrir_lectura(ruta: Path) -> BinaryIO:
    return open(ruta, "rb")


def _leer_en(archivo: BinaryIO, desplazamiento: int, tamano: int) -> bytes:
    archivo.seek(desplazamiento)
    return archivo.read(tamano)


def _rango_recibido(respuesta: Any) -> int:
    """Siguiente byte a enviar según la cabecera ``Range: bytes=0-N`` de un 308 (sin ella, 0)."""
    valor = respuesta.headers.get("Range") if hasattr(respuesta, "headers") else None
    coincidencia = _RE_RANGO.match(str(valor).strip()) if valor else None
    return int(coincidencia.group(1)) + 1 if coincidencia else 0


def _json(respuesta: Any) -> Any:
    try:
        return respuesta.json()
    except Exception as exc:  # cuerpo vacío o no JSON
        log.debug("Respuesta de Drive sin JSON (%s)", type(exc).__name__)
        return None


def _archivos(respuesta: Any) -> list[ArchivoDrive]:
    datos = _json(respuesta)
    lista = datos.get("files") if isinstance(datos, Mapping) else None
    if not isinstance(lista, list):
        return []
    archivos: list[ArchivoDrive] = []
    for item in lista[:100]:
        if isinstance(item, Mapping) and item.get("trashed") is True:
            continue
        archivo = ArchivoDrive.desde_json(item)
        if archivo is not None:
            archivos.append(archivo)
    return archivos


def _acotar_problemas(problemas: Sequence[str]) -> tuple[str, ...]:
    vistos: list[str] = []
    for problema in problemas:
        texto = recortar_aviso(problema)
        if texto and texto not in vistos:
            vistos.append(texto)
    if len(vistos) > 10:
        vistos = vistos[:9] + [recortar_aviso(f"Y {len(vistos) - 9} problemas más.")]
    return tuple(vistos)


def _ttl_valido(ttl_s: Any) -> float:
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError):
        return TTL_TARJETA_S
    if ttl != ttl or ttl <= 0 or ttl > 86400:
        return TTL_TARJETA_S
    return ttl


def _iguales(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))
