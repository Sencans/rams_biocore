"""Tipos y constantes compartidos de R.A.M.S. BIO-CORE (§4.3).

Qué hace: define las dataclasses inmutables (``frozen=True``) que viajan entre módulos
(``HomeostaticState``, ``BrocaReply``, ``Utterance``, ``Telemetria``, ``ContextoBroca``…),
las constantes del dominio (líneas de mundo, vocabulario de etiquetas, canales, tipos de
acción) y los ``Protocol`` que permiten inyectar *fakes* entre grupos.

Reglas: los ``__post_init__`` acotan y sanean valores con ``object.__setattr__`` (nunca
lanzan por un valor fuera de rango) y los ``a_dict()`` devuelven tipos JSON-serializables
(tuplas → listas, ``Enum`` → ``.value``).

v0.2 "Amadeus Companion" (``docs/COMPANION.md`` §C2.2): canal local ``escritorio``, iniciativa
``curiosidad_web``, tipos de acción de ``ControlPC``, catálogos cerrados del escritorio y de la
visión, ``DESTINOS_BASE``, ``PATRON_TITULO_SENSIBLE`` y ``ContextoBroca.percepcion``.

v0.2.2 "Correo, Drive y taller de scripts" (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §3.1): catálogos
de Google, de las tarjetas aprobables y del taller, tiempos y topes del protocolo, y las funciones
puras :func:`huella_contenido`, :func:`largo_utf16` y :func:`bytes_json` (nunca lanzan).

Dependencias: solo la biblioteca estándar. Sin degradación posible: no toca E/S. No importa
``core.config`` (``config`` sí importa de aquí: no hay ciclo).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sys
import time
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from pathlib import PurePath
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable

__all__ = [
    # constantes
    "VERSION", "DIVERGENCIA_STEINS_GATE", "LINEAS_CANONICAS", "SECUENCIA_ARRANQUE_DIVERGENCIA",
    "CAMPOS", "INICIATIVAS", "MODOS_SALIDA", "CANALES_ENTRADA", "CANALES_LOCALES",
    "CANALES_SALIDA", "EXP_VALIDOS", "MOTION_VALIDOS", "TONE_VALIDOS", "LANG_VALIDOS",
    "TIPOS_ACCION", "ORIGENES_ACCION", "ORIGENES_UTTERANCE", "CANALES_UTTERANCE",
    "ERRORES_VISTAZO", "DIVERGENCIA_MAX",
    # constantes v0.2 (§C2.2)
    "TIPOS_ACCION_V02", "PATRONES_TRAVESURA", "MODOS_PANTALLA", "ESTADOS_VISION",
    "ACCIONES_ORDEN_ESCRITORIO", "EVENTOS_ESCRITORIO", "CONTROLES_ESCRITORIO",
    "CONDUCTAS_SOLICITUD", "SAFE_ALIAS_RE", "DESTINOS_BASE", "PATRON_TITULO_SENSIBLE",
    "CLAVES_PERCEPCION", "TITULO_CORTO_MAX",
    # constantes v0.2.2 (docs/V022_CORREO_DRIVE_SCRIPTS.md §3.1)
    "SERVICIOS_GOOGLE", "CARPETAS_USUARIO", "DESTINOS_DESCARGA", "OPERACIONES_DRIVE", "FORMATOS_DRIVE",
    "ROLES_DRIVE", "CLASES_TARJETA", "ESTADOS_TARJETA", "BOTONES_TARJETA", "DECISIONES_TARJETA",
    "RESULTADOS_TARJETA", "LENGUAJES_SCRIPT", "NIVELES_RIESGO", "NIVELES_EJECUTABLES",
    "CATEGORIAS_RIESGO", "TTL_TARJETA_S", "TTL_TARJETA_TELEGRAM_S", "TTL_DOBLE_CONFIRMACION_S",
    "MAX_TARJETAS_VIVAS", "MAX_CODIGO_SCRIPT", "MAX_SALIDA_TARJETA", "MAX_HALLAZGOS_TARJETA",
    "MAX_BYTES_TARJETA", "MAX_BYTES_DECISION_TARJETA", "MAX_CUERPO_CORREO", "MAX_ASUNTO_CORREO",
    "MAX_ADJUNTOS_CORREO", "ID_TARJETA_RE", "HUELLA_RE", "CORREO_RE", "PATRON_ARCHIVO_SENSIBLE",
    # funciones
    "formatear_decimal", "banda", "resumen_nexus", "normalizar_percepcion",
    "huella_contenido", "largo_utf16", "bytes_json",
    # tipos
    "HomeostaticState", "Etiquetas", "Segmento", "BrocaReply", "Utterance", "Telemetria",
    "ResultadoVistazo", "EstadoPresencia", "ResultadoWeb", "Recuerdo", "Turno", "ItemAgenda",
    "AccionPC", "DecisionPermiso", "ResultadoAccion", "EstadoModulo", "DiagnosticoModulo",
    "ContextoBroca",
    # protocolos
    "SalidaAvatar", "RegistroLlamadas", "ProveedorHypr",
]

# §3.1: toda excepción capturada se registra (aquí, en DEBUG: un valor saneado no es un fallo).
log = logging.getLogger("rams.contracts")

# ---------------------------------------------------------------------------
# §4.3.1 Constantes
# ---------------------------------------------------------------------------

VERSION: Final = "0.1.0"
DIVERGENCIA_STEINS_GATE: Final = 1.048596
DIVERGENCIA_MAX: Final = 3.999999
LINEAS_CANONICAS: Final = (0.337337, 0.409431, 0.456914, 0.523307, 0.571024, 0.571046, 1.123581)
SECUENCIA_ARRANQUE_DIVERGENCIA: Final = (0.571024, 1.129848, 1.048596)   # homenaje (rueda nixie del boot)
CAMPOS: Final = ("alfa", "steins_gate", "beta", "anomalo")
# v0.2: "curiosidad_web" (compartir algo curioso de la web; regla determinista de main, §C2.14.7).
INICIATIVAS: Final = ("conversar", "curiosidad_visual", "descanso", "cambio_linea", "curiosidad_web")
MODOS_SALIDA: Final = ("voz", "terminal", "terminal_largo", "telegram")
# Canales: "escritorio" (chat de Amadeus Desktop y del HUD) es local, como "terminal" (§C2.2).
CANALES_ENTRADA: Final = ("terminal", "telegram", "nexus", "escritorio")
CANALES_LOCALES: Final = frozenset({"terminal", "escritorio"})  # cuentan como presencia y tienen todas las capacidades
# CANALES_SALIDA NO cambia: el chat de escritorio recibe la salida en modo "terminal".
CANALES_SALIDA: Final = ("voz", "terminal", "telegram")  # canal efectivo de una Utterance

EXP_VALIDOS: Final = ("neutral", "smug", "sonrojo_tsundere", "molesta", "pensativa", "sorprendida",
                      "triste", "feliz", "avergonzada", "seria", "curiosa", "aburrida", "somnolienta")
MOTION_VALIDOS: Final = ("idle", "asentir", "negar", "ladear_cabeza", "brazos_cruzados", "mirar_a_otro_lado",
                         "facepalm", "senalar", "suspirar", "bostezar", "estirarse")
TONE_VALIDOS: Final = ("sarcastico", "analitico", "calido", "nervioso", "serio", "jugueton", "somnoliento")
LANG_VALIDOS: Final = ("es", "en", "ja")

# Solo tipos con productor real (core/intenciones.py, main o los lóbulos). Sin "comando" ni tipos huérfanos.
# v0.2: 13 tipos; los de TIPOS_ACCION_V02 los ejecuta lobes/control_pc.py, nunca aura.ejecutar.
TIPOS_ACCION: Final = ("lanzar_app", "enfocar_nexus", "ir_workspace", "cerrar_ventana",
                       "volumen", "media", "captura_pantalla", "vistazo",
                       "abrir_destino", "buscar_en", "prerrellenar", "escribir_texto", "travesura_cursor")
TIPOS_ACCION_V02: Final = frozenset({"abrir_destino", "buscar_en", "prerrellenar", "escribir_texto",
                                     "travesura_cursor"})   # van a ControlPC, nunca a aura.ejecutar
ORIGENES_ACCION: Final = ("usuario", "iniciativa", "nexus", "telegram", "llm")   # NO cambia en la v0.2

# Catálogos cerrados de la v0.2 (§C2.2).
PATRONES_TRAVESURA: Final = ("empujon", "circulo", "temblor", "hacia_avatar")
MODOS_PANTALLA: Final = ("off", "metadatos", "pixeles")
ESTADOS_VISION: Final = ("off", "continua", "pausada", "ocupada", "error", "degradada")
ACCIONES_ORDEN_ESCRITORIO: Final = ("aparecer", "esconderse", "abrir_chat", "notificar")
EVENTOS_ESCRITORIO: Final = ("toque", "soltada", "cursor_encima", "mostrada", "ocultada", "chat_abierto")
CONTROLES_ESCRITORIO: Final = ("no_molestar_on", "no_molestar_off", "basta_travesuras", "pausar_vision")
CONDUCTAS_SOLICITUD: Final = ("buscar_usuario", "asomarse", "esconderse", "pasear")
SAFE_ALIAS_RE: Final = re.compile(r"^[a-z0-9_]{1,24}$")

# Metadatos de destinos: si admiten prerrelleno (enlace profundo con borrador) y búsqueda.
# Cómo se ABRE cada uno en cada sistema vive en lobes/control_pc.py. Inmutable a dos niveles
# (cada valor también es MappingProxyType).
DESTINOS_BASE: Final[Mapping[str, Mapping[str, bool]]] = MappingProxyType({
    clave: MappingProxyType({"prerrelleno": prerrelleno, "busqueda": busqueda})
    for clave, prerrelleno, busqueda in (
        ("whatsapp", True, False),
        ("telegram", True, False),
        ("x", True, True),
        ("gmail", True, False),
        ("correo", True, False),        # mailto:
        ("instagram", False, False),
        ("tiktok", False, False),
        ("facebook", False, False),
        ("discord", False, False),
        ("linkedin", False, False),
        ("youtube", False, True),
        ("spotify", False, True),
        ("navegador", False, True),     # búsqueda web en DuckDuckGo
        ("archivos", False, False),
    )
})

# Títulos sensibles: se aplica a títulos en minúsculas y sin tildes (config.titulo_sensible y
# Settings.pantalla_excluir_re). Base de la regla anti-contraseñas y de la privacidad de la visión.
PATRON_TITULO_SENSIBLE: Final = (
    r"contrasena|password|iniciar sesion|sign ?in|log ?in|verificacion|codigo|otp|2fa|token|\bpin\b|"
    r"banco|bancolombia|nequi|daviplata|\bpse\b|pago|checkout|tarjeta|wallet|binance|"
    r"incognito|inprivate|private browsing|navegacion privada|keepass|bitwarden|1password")

# Claves permitidas en ContextoBroca.percepcion (las demás se descartan) y largo del título corto.
CLAVES_PERCEPCION: Final = ("app", "titulo_corto", "video", "pantalla_completa", "rostro", "atencion",
                            "dist_m")
TITULO_CORTO_MAX: Final = 60

# Valores documentados en los comentarios de §4.3.3 y §4.3.4 (de apoyo; no normativos aparte).
ORIGENES_UTTERANCE: Final = ("respuesta", "iniciativa", "agenda", "alerta", "sistema", "nexus")
CANALES_UTTERANCE: Final = ("auto", "voz", "terminal", "telegram")
ERRORES_VISTAZO: Final = ("camara_deshabilitada", "sin_opencv", "no_abre", "intervalo", "ocupada",
                          "backoff", "limite_diario", "denegada", "tiempo_agotado")

# --- v0.2.2: Google, tarjetas y taller (docs/V022_CORREO_DRIVE_SCRIPTS.md) ---
SERVICIOS_GOOGLE: Final = ("correo", "drive")
CARPETAS_USUARIO: Final = ("descargas", "documentos", "escritorio", "imagenes", "musica", "videos")
DESTINOS_DESCARGA: Final = ("descargas", "documentos", "escritorio")
OPERACIONES_DRIVE: Final = ("buscar", "info", "descargar", "leer", "crear_carpeta", "subir", "papelera", "compartir")
FORMATOS_DRIVE: Final = ("pdf", "texto", "original")
ROLES_DRIVE: Final = ("reader", "commenter", "writer")
CLASES_TARJETA: Final = ("correo", "drive", "script", "salida")
ESTADOS_TARJETA: Final = ("pendiente", "info", "cerrada")
BOTONES_TARJETA: Final = ("aprobar", "aprobar_publico", "editar", "guardar", "copiar", "descartar")
DECISIONES_TARJETA: Final = ("aprobar", "aprobar_publico", "editar", "guardar", "descartar")   # "copiar" es local
RESULTADOS_TARJETA: Final = ("enviado", "hecho", "ejecutado", "descartado", "caducado", "reemplazado",
                             "denegado", "fallo")
LENGUAJES_SCRIPT: Final = ("powershell", "python", "bash")
NIVELES_RIESGO: Final = ("bajo", "medio", "alto", "critico")            # orden creciente
NIVELES_EJECUTABLES: Final = frozenset({"bajo", "medio", "alto"})
CATEGORIAS_RIESGO: Final = ("borrado", "sobrescritura", "registro", "servicios", "red", "descarga_ejecucion",
                            "credenciales", "elevacion", "apagado", "seguridad", "persistencia", "ofuscacion",
                            "dinamico", "fuera_de_carpeta", "subproceso", "winapi", "interactivo", "sintaxis",
                            "tamano", "analisis_degradado")
TTL_TARJETA_S: Final = 1800.0              # preparada en el PC
TTL_TARJETA_TELEGRAM_S: Final = 43200.0    # preparada por Telegram (12 h)
TTL_DOBLE_CONFIRMACION_S: Final = 120.0    # entre el 1.er y el 2.º clic de "compartir en público"
MAX_TARJETAS_VIVAS: Final = 4              # una por clase
# Presupuesto de tamaño (§12.2): TODO mensaje WS de tarjeta, en los dos sentidos, cabe en 60 KiB, por
# debajo de los 64 KiB que aceptan el servidor del HUD, Electron (MAX_WS_ENTRANTE), el HUD y el puente
# de movil/ (max_size=65536, que CIERRA la conexión si se pasa). Los topes de texto del protocolo se
# miden en unidades UTF-16 (lo que mide String.length en JS): largo_utf16().
MAX_CODIGO_SCRIPT: Final = 24_576          # bytes UTF-8 (sin controles salvo "\n" y "\t", §9.3) ⇒ ≤ 48 KiB escapado
MAX_SALIDA_TARJETA: Final = 8_192          # unidades UTF-16 por flujo (stdout, stderr) en la tarjeta "salida"
MAX_HALLAZGOS_TARJETA: Final = 20          # hallazgos en la vista del script (los más graves); el resto se cuenta
MAX_BYTES_TARJETA: Final = 60 * 1024       # bytes_json del mensaje WS {"tipo":"tarjeta","datos":…} (§12.2)
MAX_BYTES_DECISION_TARJETA: Final = 60 * 1024   # bytes UTF-8 de tarjeta_decision (§12.3)
MAX_CUERPO_CORREO: Final = 10_000          # unidades UTF-16
MAX_ASUNTO_CORREO: Final = 200             # unidades UTF-16
MAX_ADJUNTOS_CORREO: Final = 5
ID_TARJETA_RE: Final = re.compile(r"^[0-9a-f]{8}$")
HUELLA_RE: Final = re.compile(r"^[0-9a-f]{64}$")
CORREO_RE: Final = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}$")
# Archivos que nunca se adjuntan, suben ni analizan como datos del usuario (sobre el nombre o la ruta
# con "/"; minúsculas). Además, todo lo que esté bajo base_dir o data_dir del proyecto.
PATRON_ARCHIVO_SENSIBLE: Final = (
    r"(?:^|/)(?:\.env(?:\..*)?|.*\.(?:pem|key|p12|pfx|kdbx|ovpn|ppk|gpg|asc|keychain)"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|login data|cookies|web data|key[34]\.db|logins\.json"
    r"|ctl_token|escritorio\.json|google_(?:cliente|token)\.json|client_secret[^/]*\.json)$")


def _canonico(valor: Any) -> Any:
    """Forma canónica de ``valor`` para :func:`huella_contenido` (recursiva).

    Textos (también las claves) con ``"\\r\\n"``/``"\\r"`` → ``"\\n"`` y en NFC; ``Mapping`` → ``dict``;
    tuplas y listas → listas; el resto, tal cual (``json.dumps`` decide si es serializable). Lanza
    ``ValueError`` si dos claves coinciden tras normalizar: la huella sería ambigua (fail-closed).
    """
    if isinstance(valor, str):
        return unicodedata.normalize("NFC", valor.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(valor, Mapping):
        resultado: dict[Any, Any] = {}
        for clave, contenido in valor.items():
            canonica = _canonico(clave) if isinstance(clave, str) else clave
            if canonica in resultado:
                raise ValueError("claves repetidas tras normalizar")
            resultado[canonica] = _canonico(contenido)
        return resultado
    if isinstance(valor, (list, tuple)):
        return [_canonico(v) for v in valor]
    return valor


def huella_contenido(contenido: Mapping[str, Any]) -> str:
    """sha256 (64 hex) del JSON canónico de ``contenido`` (huella de una tarjeta).

    Canónico: textos en NFC con "\\r\\n" y "\\r" → "\\n" (recursivo), tuplas → listas,
    ``json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)``
    en UTF-8. Si no es serializable (bytes, NaN, objetos…), devuelve ``""`` con ``log.debug`` (nunca
    lanza): quien la use trata ``""`` como huella inválida (fail-closed).

    También da ``""`` si ``contenido`` no es un ``Mapping``, si dos claves coinciden tras normalizar
    o si hay sustitutos sueltos (U+D800–U+DFFF, que no son UTF-8 válido: los módulos los cambian por
    U+FFFD antes de huellar). El registro solo lleva el tipo del error, nunca el contenido.
    """
    try:
        if not isinstance(contenido, Mapping):
            raise TypeError("el contenido no es un Mapping")
        texto = json.dumps(_canonico(contenido), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(texto.encode("utf-8")).hexdigest()
    except Exception as exc:  # contrato: nunca lanza (TypeError, ValueError, UnicodeError, RecursionError…)
        log.debug("huella_contenido: contenido no huellable (%s); huella vacía", type(exc).__name__)
        return ""


def largo_utf16(texto: str) -> int:
    """Longitud en unidades UTF-16 (= ``String.length`` de JavaScript): ``len(texto.encode("utf-16-le",
    "surrogatepass")) // 2``. Un carácter fuera del BMP (emoji) cuenta 2. Nunca lanza (no-str ⇒ 0)."""
    if not isinstance(texto, str):
        return 0
    try:
        return len(str.encode(texto, "utf-16-le", "surrogatepass")) // 2
    except Exception as exc:  # red de seguridad: el mismo cálculo, carácter a carácter
        log.debug("largo_utf16: codificación fallida (%s); cuento por caracteres", type(exc).__name__)
        return sum(2 if ord(caracter) > 0xFFFF else 1 for caracter in texto)


def bytes_json(valor: Any) -> int:
    """Bytes UTF-8 de ``json.dumps(valor, ensure_ascii=False, separators=(",", ":"))``, la forma en que
    escritorio_link serializa. No serializable o con sustitutos sueltos ⇒ ``sys.maxsize`` (fail-closed: no
    cabe en ningún tope). Nunca lanza.

    ``NaN`` e infinitos también dan ``sys.maxsize`` (``allow_nan=False``: no son JSON válido).
    """
    try:
        return len(json.dumps(valor, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False).encode("utf-8"))
    except Exception as exc:  # contrato: nunca lanza (TypeError, ValueError, UnicodeError, RecursionError…)
        log.debug("bytes_json: valor no serializable (%s); uso sys.maxsize", type(exc).__name__)
        return sys.maxsize


def formatear_decimal(x: float, decimales: int = 1) -> str:
    """Número con coma decimal para el usuario: ``9.43 → "9,4"`` (§3.5).

    Un valor no finito o no numérico devuelve ``"—"``; nunca devuelve ``"-0,0"``.
    """
    try:
        valor = float(x)
    except (TypeError, ValueError) as exc:
        log.debug("formatear_decimal: valor no numérico (%s)", type(exc).__name__)
        return "—"
    if not math.isfinite(valor):
        return "—"
    decimales = max(0, int(decimales))
    texto = f"{valor:.{decimales}f}"
    if texto.startswith("-") and float(texto) == 0.0:
        texto = texto[1:]
    return texto.replace(".", ",")


def banda(x: float) -> str:
    """Banda cualitativa de un valor 0..1: ``<0.34 "bajo"``, ``<0.67 "medio"``, resto ``"alto"``.

    Un valor no finito o no numérico se considera ``"medio"`` (sin dato fiable).
    """
    try:
        valor = float(x)
    except (TypeError, ValueError) as exc:
        log.debug("banda: valor no numérico (%s); se trata como 'medio'", type(exc).__name__)
        return "medio"
    if not math.isfinite(valor):
        return "medio"
    if valor < 0.34:
        return "bajo"
    if valor < 0.67:
        return "medio"
    return "alto"


def resumen_nexus(d: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Resumen mínimo del payload de ``nexus.estado`` para ``ContextoBroca.nexus`` (§4.3.1).

    ``None`` si ``d`` es ``None`` o ``d["corriendo"]`` es falso. Si no:
    ``{"corriendo": True, "enfocado": bool, "workspace": basename | None, "trabajos": int}``.
    El workspace se reduce a su último componente (separando por ``/`` y ``\\``): nunca
    salen rutas completas ni comandos. Nunca lanza.
    """
    try:
        if d is None or not isinstance(d, Mapping) or not d.get("corriendo"):
            return None
        workspace: str | None = None
        crudo = d.get("workspace")
        if crudo:
            partes = [p for p in str(crudo).replace("\\", "/").split("/") if p]
            workspace = partes[-1] if partes else None
        trabajos_crudo = d.get("trabajos")
        trabajos = 0
        if isinstance(trabajos_crudo, bool):
            trabajos = 0
        elif isinstance(trabajos_crudo, int):
            trabajos = max(0, trabajos_crudo)
        elif trabajos_crudo is not None:
            try:
                trabajos = len(trabajos_crudo)
            except TypeError as exc:
                log.debug("resumen_nexus: 'trabajos' sin longitud (%s); uso 0", type(exc).__name__)
                trabajos = 0
        return {
            "corriendo": True,
            "enfocado": bool(d.get("enfocado", False)),
            "workspace": workspace,
            "trabajos": trabajos,
        }
    except Exception as exc:  # contrato: nunca lanza (Mapping exóticos con get roto, etc.)
        # Solo el tipo: el payload de nexus.estado puede contener rutas o comandos (§9).
        log.debug("resumen_nexus: payload no válido (%s); se ignora", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------


def _real(valor: Any, defecto: float) -> float:
    """Float finito o ``defecto`` (``None``, NaN, inf o no numérico)."""
    if valor is None:
        return defecto
    try:
        numero = float(valor)
    except (TypeError, ValueError) as exc:
        log.debug("Valor no numérico (%s); uso el valor por defecto", type(exc).__name__)
        return defecto
    return numero if math.isfinite(numero) else defecto


def _acotar(valor: Any, minimo: float, maximo: float, defecto: float) -> float:
    """Float finito acotado a ``[minimo, maximo]``; NaN/inf/no numérico ⇒ ``defecto``."""
    return min(maximo, max(minimo, _real(valor, defecto)))


def _real_opcional(valor: Any, minimo: float | None = None, maximo: float | None = None) -> float | None:
    """Float finito opcional (acotado si se indica) o ``None``."""
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError) as exc:
        log.debug("Valor opcional no numérico (%s); uso None", type(exc).__name__)
        return None
    if not math.isfinite(numero):
        return None
    if minimo is not None:
        numero = max(minimo, numero)
    if maximo is not None:
        numero = min(maximo, numero)
    return numero


def _entero(valor: Any, defecto: int, minimo: int | None = None, maximo: int | None = None) -> int:
    """Entero (acotado si se indica) o ``defecto``."""
    if valor is None:
        return defecto
    try:
        if isinstance(valor, float) and not math.isfinite(valor):
            return defecto
        numero = int(valor)
    except (TypeError, ValueError, OverflowError) as exc:
        log.debug("Valor no entero (%s); uso el valor por defecto", type(exc).__name__)
        return defecto
    if minimo is not None:
        numero = max(minimo, numero)
    if maximo is not None:
        numero = min(maximo, numero)
    return numero


def _a_json(valor: Any) -> Any:
    """Convierte un valor a tipos JSON básicos (tuplas → listas, Enum → value, Path → str)."""
    if valor is None or isinstance(valor, (bool, int, str)):
        return valor
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, Enum):
        return _a_json(valor.value)
    if isinstance(valor, PurePath):
        return str(valor)
    if isinstance(valor, datetime):
        return valor.isoformat(timespec="seconds")
    a_dict = getattr(valor, "a_dict", None)
    if callable(a_dict):
        return a_dict()
    if isinstance(valor, Mapping):
        return {str(k): _a_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set, frozenset)):
        return [_a_json(v) for v in valor]
    if isinstance(valor, (bytes, bytearray)):
        return f"<{len(valor)} bytes>"
    return str(valor)


# ---------------------------------------------------------------------------
# §4.3.2 HomeostaticState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HomeostaticState:
    """Estado homeostático que produce el conectoma (sin LLM).

    ``campo`` es el campo CONFIRMADO por la histéresis del medidor; ``divergencia`` es el
    valor instantáneo. ``__post_init__`` acota todo (NaN o inf ⇒ valor por defecto del campo).
    """

    arousal: float = 0.55          # 0..1  activación
    focus: float = 0.70            # 0..1  coherencia del bump E-PG
    boredom: float = 0.20          # 0..1  aburrimiento (habituación MBON-α'3)
    fatiga: float = 0.25           # 0..1  proceso S + C + dFB
    curiosidad: float = 0.10       # 0..1
    valencia: float = 0.0          # -1..1 (PAM − PPL1)
    divergencia: float = 1.048596  # 0.000000..3.999999
    campo: str = "steins_gate"     # ∈ CAMPOS
    presion_sueno: float = 0.30    # 0..1 (proceso S)
    rumbo_deg: float = 0.0         # 0..360 fase del bump ("dirección de atención")
    modo: str = "vigilia"          # "vigilia" | "reposo"
    disparar_vision: bool = False  # flanco de subida de la curiosidad (pedir vistazo de cámara)
    iniciativa: str | None = None  # ∈ INICIATIVAS; solo en el estado del chunk donde ocurre el flanco
    motivo_iniciativa: str = ""    # texto corto en español para logs y prompt
    linea_antes: str | None = None    # ∈ CAMPOS; solo si iniciativa == "cambio_linea"
    linea_despues: str | None = None  # ∈ CAMPOS; solo si iniciativa == "cambio_linea"
    factor_tiempo_real: float = 1.0
    fuente: str = "stub"           # "reducido" | "stub" ("flywire" reservado para v0.2)
    tasas: Mapping[str, float] = field(default_factory=dict, hash=False)  # Hz por población (vacío en stub)
    bump: tuple[float, ...] = ()   # 16 valores 0..1 del anillo E-PG, calculados DENTRO de avanzar
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        poner = object.__setattr__
        for nombre, defecto in (("arousal", 0.55), ("focus", 0.70), ("boredom", 0.20),
                                ("fatiga", 0.25), ("curiosidad", 0.10), ("presion_sueno", 0.30)):
            poner(self, nombre, _acotar(getattr(self, nombre), 0.0, 1.0, defecto))
        poner(self, "valencia", _acotar(self.valencia, -1.0, 1.0, 0.0))
        poner(self, "divergencia", _acotar(self.divergencia, 0.0, DIVERGENCIA_MAX, DIVERGENCIA_STEINS_GATE))
        poner(self, "rumbo_deg", _real(self.rumbo_deg, 0.0) % 360.0)
        poner(self, "factor_tiempo_real", max(0.0, _real(self.factor_tiempo_real, 1.0)))
        poner(self, "ts", _real(self.ts, time.time()))
        poner(self, "campo", self.campo if self.campo in CAMPOS else "anomalo")
        poner(self, "modo", self.modo if self.modo in ("vigilia", "reposo") else "vigilia")
        poner(self, "disparar_vision", bool(self.disparar_vision))
        poner(self, "iniciativa", self.iniciativa if self.iniciativa in INICIATIVAS else None)
        poner(self, "motivo_iniciativa", "" if self.motivo_iniciativa is None else str(self.motivo_iniciativa))
        poner(self, "linea_antes", self.linea_antes if self.linea_antes in CAMPOS else None)
        poner(self, "linea_despues", self.linea_despues if self.linea_despues in CAMPOS else None)
        poner(self, "fuente", str(self.fuente) if self.fuente else "stub")
        tasas: dict[str, float] = {}
        if isinstance(self.tasas, Mapping):
            for clave, valor in self.tasas.items():
                numero = _real(valor, 0.0)
                tasas[str(clave)] = max(0.0, numero)
        poner(self, "tasas", tasas)
        bump: list[float] = []
        try:
            for valor in self.bump or ():
                if len(bump) >= 16:
                    break
                try:
                    numero = float(valor)
                except (TypeError, ValueError) as exc:
                    log.debug("bump: valor no numérico (%s); uso 0", type(exc).__name__)
                    numero = 0.0
                if math.isnan(numero):
                    numero = 0.0
                bump.append(min(1.0, max(0.0, numero)))
        except TypeError as exc:  # bump no iterable
            log.debug("bump no iterable (%s); se deja vacío", type(exc).__name__)
            bump = []
        poner(self, "bump", tuple(bump))

    @classmethod
    def neutral(cls) -> HomeostaticState:
        """Estado por defecto (antes de que el conectoma calibre)."""
        return cls()

    def a_dict(self) -> dict[str, Any]:
        """Todos los campos; ``tasas`` como dict, ``bump`` como lista y ``divergencia`` a 6 decimales."""
        return {
            "arousal": self.arousal,
            "focus": self.focus,
            "boredom": self.boredom,
            "fatiga": self.fatiga,
            "curiosidad": self.curiosidad,
            "valencia": self.valencia,
            "divergencia": round(self.divergencia, 6),
            "campo": self.campo,
            "presion_sueno": self.presion_sueno,
            "rumbo_deg": self.rumbo_deg,
            "modo": self.modo,
            "disparar_vision": self.disparar_vision,
            "iniciativa": self.iniciativa,
            "motivo_iniciativa": self.motivo_iniciativa,
            "linea_antes": self.linea_antes,
            "linea_despues": self.linea_despues,
            "factor_tiempo_real": self.factor_tiempo_real,
            "fuente": self.fuente,
            "tasas": dict(self.tasas),
            "bump": list(self.bump),
            "ts": self.ts,
        }

    @classmethod
    def desde_dict(cls, d: Mapping[str, Any]) -> HomeostaticState:
        """Reconstruye un estado tolerando dicts incompletos o con claves desconocidas."""
        if not isinstance(d, Mapping):
            return cls()
        nombres = {f.name for f in fields(cls)}
        argumentos = {k: v for k, v in d.items() if k in nombres}
        if "tasas" in argumentos and not isinstance(argumentos["tasas"], Mapping):
            del argumentos["tasas"]
        if "ts" in argumentos and argumentos["ts"] is None:
            del argumentos["ts"]
        try:
            return cls(**argumentos)
        except (TypeError, ValueError) as exc:
            log.debug("HomeostaticState.desde_dict: datos no válidos (%s); uso el neutro",
                      type(exc).__name__)
            return cls()

    def divergencia_str(self) -> str:
        """Divergencia con punto y 6 decimales (excepción canónica de §3.5): ``"1.048596"``."""
        return f"{self.divergencia:.6f}"

    def vector(self) -> tuple[float, float, float, float]:
        """``(arousal, focus, boredom, fatiga)``: el espacio de los prototipos de EXP."""
        return (self.arousal, self.focus, self.boredom, self.fatiga)

    def resumen_textual(self) -> str:
        """``"arousal=…, focus=…, boredom=…, fatiga=…, curiosidad=…, valencia=…"`` con bandas."""
        if self.valencia > 0.2:
            valencia = "positiva"
        elif self.valencia < -0.2:
            valencia = "negativa"
        else:
            valencia = "neutra"
        return (f"arousal={banda(self.arousal)}, focus={banda(self.focus)}, "
                f"boredom={banda(self.boredom)}, fatiga={banda(self.fatiga)}, "
                f"curiosidad={banda(self.curiosidad)}, valencia={valencia}")


# ---------------------------------------------------------------------------
# §4.3.3 Etiquetas, segmentos y respuestas de Broca
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Etiquetas:
    """Triple ``[EXP][MOTION][TONE]``. La validación contra el vocabulario está en ``core.etiquetas``."""

    exp: str = "neutral"
    motion: str = "idle"
    tone: str = "analitico"

    def como_prefijo(self) -> str:
        """``"[EXP:neutral][MOTION:idle][TONE:analitico]"``."""
        return f"[EXP:{self.exp}][MOTION:{self.motion}][TONE:{self.tone}]"

    def a_dict(self) -> dict[str, str]:
        """``{"exp", "motion", "tone"}``."""
        return {"exp": self.exp, "motion": self.motion, "tone": self.tone}


@dataclass(frozen=True)
class Segmento:
    """Un tramo de respuesta con sus etiquetas y su texto limpio."""

    etiquetas: Etiquetas
    texto: str            # limpio, sin etiquetas
    lang: str = "es"      # ∈ LANG_VALIDOS

    def __post_init__(self) -> None:
        if not isinstance(self.etiquetas, Etiquetas):
            if isinstance(self.etiquetas, Mapping):
                datos = self.etiquetas
                etq = Etiquetas(str(datos.get("exp", "neutral")), str(datos.get("motion", "idle")),
                                str(datos.get("tone", "analitico")))
            else:
                etq = Etiquetas()
            object.__setattr__(self, "etiquetas", etq)
        object.__setattr__(self, "texto", "" if self.texto is None else str(self.texto))
        object.__setattr__(self, "lang", self.lang if self.lang in LANG_VALIDOS else "es")

    def a_dict(self) -> dict[str, str]:
        """``{"exp", "motion", "tone", "texto", "lang"}``."""
        return {**self.etiquetas.a_dict(), "texto": self.texto, "lang": self.lang}


def _tupla_segmentos(valor: Any) -> tuple[Segmento, ...]:
    """Normaliza una secuencia de segmentos a tupla (descarta lo que no sea ``Segmento``)."""
    if valor is None:
        return ()
    if isinstance(valor, Segmento):
        return (valor,)
    try:
        return tuple(s for s in valor if isinstance(s, Segmento))
    except TypeError as exc:
        log.debug("Segmentos no iterables (%s); se ignoran", type(exc).__name__)
        return ()


@dataclass(frozen=True)
class BrocaReply:
    """Respuesta del Área de Broca (Gemini o banco local), siempre con etiquetas válidas."""

    segmentos: tuple[Segmento, ...]
    texto_crudo: str = ""
    fuente: str = "local"         # "gemini" | "local"
    modelo: str | None = None
    latencia_ms: int = 0
    corregido: bool = False       # hubo correcciones locales de etiquetas o formato
    tsundere: bool = False        # consumió presupuesto tsundere
    finish: str | None = None     # finishReason de Gemini, si aplica

    def __post_init__(self) -> None:
        object.__setattr__(self, "segmentos", _tupla_segmentos(self.segmentos))
        object.__setattr__(self, "latencia_ms", _entero(self.latencia_ms, 0, minimo=0))

    @property
    def texto_plano(self) -> str:
        """Texto de todos los segmentos unido por espacios."""
        return " ".join(s.texto for s in self.segmentos if s.texto).strip()

    @property
    def etiquetas(self) -> Etiquetas:
        """Etiquetas del primer segmento (o ``Etiquetas()`` si no hay)."""
        return self.segmentos[0].etiquetas if self.segmentos else Etiquetas()

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable con las claves de §4.3.3."""
        return {
            "segmentos": [s.a_dict() for s in self.segmentos],
            "fuente": self.fuente,
            "modelo": self.modelo,
            "latencia_ms": self.latencia_ms,
            "corregido": self.corregido,
            "tsundere": self.tsundere,
            "finish": self.finish,
            "texto_plano": self.texto_plano,
        }

    @classmethod
    def de_texto(cls, texto: str, etiquetas: Etiquetas | None = None, *,
                 fuente: str = "local") -> BrocaReply:
        """Respuesta de un solo segmento a partir de un texto ya limpio."""
        limpio = "" if texto is None else str(texto).strip()
        return cls(segmentos=(Segmento(etiquetas or Etiquetas(), limpio),), texto_crudo=limpio,
                   fuente=fuente)


@dataclass(frozen=True)
class Utterance:
    """Unidad de salida que encola ``riko_megumin_io`` (voz, terminal o Telegram)."""

    segmentos: tuple[Segmento, ...]
    origen: str = "respuesta"       # "respuesta" | "iniciativa" | "agenda" | "alerta" | "sistema" | "nexus"
    prioridad: int = 0              # 0 normal · 1 proactivo · 2 crítico
    canal: str = "auto"             # "auto" | "voz" | "terminal" | "telegram". main.py SIEMPRE pasa un canal explícito
                                    # (§10.21 "Elección de canal"); "auto" queda para otros llamadores y tests.
    nivel_mensajeria: int | None = None  # 1 | 2 | 3 forzado; None = lo decide riko (§10.19)
    motivo: str = ""                # clave corta (p. ej. tipo de iniciativa o de alerta): refractarios y registro
    tipo_iniciativa: str | None = None   # ∈ INICIATIVAS si origen == "iniciativa" (la compuerta lo usa: "descanso")
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    creado_ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        poner = object.__setattr__
        poner(self, "segmentos", _tupla_segmentos(self.segmentos))
        poner(self, "prioridad", _entero(self.prioridad, 0, minimo=0, maximo=2))
        poner(self, "canal", self.canal if self.canal in CANALES_UTTERANCE else "auto")
        poner(self, "nivel_mensajeria",
              self.nivel_mensajeria if self.nivel_mensajeria in (1, 2, 3) else None)
        poner(self, "tipo_iniciativa",
              self.tipo_iniciativa if self.tipo_iniciativa in INICIATIVAS else None)
        poner(self, "motivo", "" if self.motivo is None else str(self.motivo))
        poner(self, "creado_ts", _real(self.creado_ts, time.time()))

    @property
    def texto_plano(self) -> str:
        """Texto de todos los segmentos unido por espacios."""
        return " ".join(s.texto for s in self.segmentos if s.texto).strip()

    @classmethod
    def desde_reply(cls, reply: BrocaReply, **kw: Any) -> Utterance:
        """``Utterance(segmentos=reply.segmentos, **kw)``."""
        return cls(segmentos=reply.segmentos, **kw)


# ---------------------------------------------------------------------------
# §4.3.4 Percepción, memoria y web
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Telemetria:
    """Lectura de telemetría del PC. Un dato ausente es ``None`` (nunca se inventa)."""

    cpu_pct: float = 0.0
    ram_usada_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_pct: float = 0.0
    disco_pct: float = 0.0
    temp_cpu: float | None = None
    temp_gpu: float | None = None
    gpu_pct: float | None = None
    gpu_nombre: str | None = None
    proceso_top: tuple[str, int, float] | None = None   # (nombre, pid, cpu %)
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        poner = object.__setattr__
        poner(self, "cpu_pct", _acotar(self.cpu_pct, 0.0, 100.0, 0.0))
        poner(self, "ram_usada_gb", max(0.0, _real(self.ram_usada_gb, 0.0)))
        poner(self, "ram_total_gb", max(0.0, _real(self.ram_total_gb, 0.0)))
        poner(self, "ram_pct", _acotar(self.ram_pct, 0.0, 100.0, 0.0))
        poner(self, "disco_pct", _acotar(self.disco_pct, 0.0, 100.0, 0.0))
        poner(self, "temp_cpu", _real_opcional(self.temp_cpu))
        poner(self, "temp_gpu", _real_opcional(self.temp_gpu))
        poner(self, "gpu_pct", _real_opcional(self.gpu_pct, 0.0, 100.0))
        poner(self, "ts", _real(self.ts, time.time()))
        top = self.proceso_top
        if top is not None:
            try:
                nombre, pid, cpu = top
                top = (str(nombre), _entero(pid, 0, minimo=0), max(0.0, _real(cpu, 0.0)))
            except (TypeError, ValueError) as exc:
                log.debug("Telemetria.proceso_top no válido (%s); se ignora", type(exc).__name__)
                top = None
        poner(self, "proceso_top", top)

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable; ``proceso_top`` → ``{"nombre","pid","cpu"}`` o ``None``."""
        top = None
        if self.proceso_top is not None:
            top = {"nombre": self.proceso_top[0], "pid": self.proceso_top[1], "cpu": self.proceso_top[2]}
        return {
            "cpu_pct": self.cpu_pct,
            "ram_usada_gb": self.ram_usada_gb,
            "ram_total_gb": self.ram_total_gb,
            "ram_pct": self.ram_pct,
            "disco_pct": self.disco_pct,
            "temp_cpu": self.temp_cpu,
            "temp_gpu": self.temp_gpu,
            "gpu_pct": self.gpu_pct,
            "gpu_nombre": self.gpu_nombre,
            "proceso_top": top,
            "ts": self.ts,
        }

    def resumen_textual(self) -> str:
        """Reporte en español estilo J.A.R.V.I.S., sin inventar datos.

        Ej.: ``"CPU 23 %, RAM 9,4 de 16,0 GB (59 %), disco 78 %, CPU 61 °C, sin lectura de GPU"``.
        """
        partes = [f"CPU {formatear_decimal(self.cpu_pct, 0)} %"]
        if self.ram_total_gb > 0:
            partes.append(f"RAM {formatear_decimal(self.ram_usada_gb, 1)} de "
                          f"{formatear_decimal(self.ram_total_gb, 1)} GB "
                          f"({formatear_decimal(self.ram_pct, 0)} %)")
        else:
            partes.append("sin lectura de RAM")
        partes.append(f"disco {formatear_decimal(self.disco_pct, 0)} %")
        if self.temp_cpu is not None:
            partes.append(f"CPU {formatear_decimal(self.temp_cpu, 0)} °C")
        else:
            partes.append("sin lectura de temperatura de CPU")
        if self.gpu_pct is None and self.temp_gpu is None:
            partes.append("sin lectura de GPU")
        else:
            nombre = f"GPU {self.gpu_nombre}" if self.gpu_nombre else "GPU"
            if self.gpu_pct is not None and self.temp_gpu is not None:
                partes.append(f"{nombre} {formatear_decimal(self.gpu_pct, 0)} %, "
                              f"{formatear_decimal(self.temp_gpu, 0)} °C")
            elif self.gpu_pct is not None:
                partes.append(f"{nombre} {formatear_decimal(self.gpu_pct, 0)} %, "
                              "sin lectura de temperatura de GPU")
            else:
                partes.append(f"{nombre} {formatear_decimal(self.temp_gpu, 0)} °C, "
                              "sin lectura de uso de GPU")
        if self.proceso_top is not None:
            partes.append(f"proceso más activo: {self.proceso_top[0]} "
                          f"({formatear_decimal(self.proceso_top[2], 0)} %)")
        return ", ".join(partes)


@dataclass(frozen=True)
class ResultadoVistazo:
    """Resultado de un "parpadeo" de cámara (nunca contiene imágenes)."""

    rostro: bool | None              # None = no se pudo determinar
    n_rostros: int = 0
    duracion_ms: int = 0             # tiempo con la cámara abierta
    brillo: float | None = None      # 0..1, media de luminancia
    motivo: str = ""                 # "curiosidad" | "presencia" | "usuario"
    error: str | None = None         # "camara_deshabilitada" | "sin_opencv" | "no_abre" | "intervalo" | "ocupada" |
                                     # "backoff" | "limite_diario" | "denegada" | "tiempo_agotado"
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        poner = object.__setattr__
        poner(self, "rostro", None if self.rostro is None else bool(self.rostro))
        poner(self, "n_rostros", _entero(self.n_rostros, 0, minimo=0))
        poner(self, "duracion_ms", _entero(self.duracion_ms, 0, minimo=0))
        poner(self, "brillo", _real_opcional(self.brillo, 0.0, 1.0))
        poner(self, "ts", _real(self.ts, time.time()))

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {
            "rostro": self.rostro,
            "n_rostros": self.n_rostros,
            "duracion_ms": self.duracion_ms,
            "brillo": self.brillo,
            "motivo": self.motivo,
            "error": self.error,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class EstadoPresencia:
    """Decisión presente/ausente de ``core/presence.py`` (§7)."""

    presente: bool
    confianza: float                 # 0..1
    motivo: str                      # ver §7
    ambiguo: bool = False            # confianza < 0.6
    segundos_inactivo: float | None = None
    fuente_inactividad: str = "ninguna"   # "hypridle" | "win32" | "ninguna"
    ultima_entrada_ts: float | None = None
    ultimo_rostro_ts: float | None = None
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        poner = object.__setattr__
        poner(self, "presente", bool(self.presente))
        poner(self, "confianza", _acotar(self.confianza, 0.0, 1.0, 0.0))
        poner(self, "ambiguo", bool(self.ambiguo))
        poner(self, "segundos_inactivo", _real_opcional(self.segundos_inactivo, 0.0))
        poner(self, "ultima_entrada_ts", _real_opcional(self.ultima_entrada_ts))
        poner(self, "ultimo_rostro_ts", _real_opcional(self.ultimo_rostro_ts))
        poner(self, "ts", _real(self.ts, time.time()))

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable (payload de ``usuario.presencia``)."""
        return {
            "presente": self.presente,
            "confianza": self.confianza,
            "motivo": self.motivo,
            "ambiguo": self.ambiguo,
            "segundos_inactivo": self.segundos_inactivo,
            "fuente_inactividad": self.fuente_inactividad,
            "ultima_entrada_ts": self.ultima_entrada_ts,
            "ultimo_rostro_ts": self.ultimo_rostro_ts,
            "ts": self.ts,
        }

    @classmethod
    def desconocido(cls) -> EstadoPresencia:
        """Sin datos: se asume presente con confianza baja (``ambiguo``)."""
        return cls(presente=True, confianza=0.3, motivo="sin_datos", ambiguo=True)


@dataclass(frozen=True)
class ResultadoWeb:
    """Resultado de búsqueda web (snippet ya saneado, §10.16)."""

    titulo: str
    url: str
    snippet: str                     # saneado (§10.16)
    fuente: str                      # "brave" | "ddgs"
    edad: str | None = None

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"titulo": self.titulo, "url": self.url, "snippet": self.snippet,
                "fuente": self.fuente, "edad": self.edad}


@dataclass(frozen=True)
class Recuerdo:
    """Fragmento de memoria recuperado del almacén ``john/``."""

    id: int
    ts: str                          # UTC ISO
    tipo: str                        # "vivido" | "heredado"
    categoria: str                   # "hecho" | "preferencia" | "consolidacion" | "reading_steiner" | "perfil" | ...
    contenido: str
    importancia: float = 0.5
    kv_id: str | None = None         # "KV-0007" si viene de una consolidación
    puntuacion: float = 0.0          # relevancia en la búsqueda

    def __post_init__(self) -> None:
        object.__setattr__(self, "importancia", _acotar(self.importancia, 0.0, 1.0, 0.5))
        object.__setattr__(self, "puntuacion", _real(self.puntuacion, 0.0))

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"id": self.id, "ts": self.ts, "tipo": self.tipo, "categoria": self.categoria,
                "contenido": self.contenido, "importancia": self.importancia,
                "kv_id": self.kv_id, "puntuacion": self.puntuacion}


@dataclass(frozen=True)
class Turno:
    """Turno de conversación guardado en memoria."""

    id: int
    ts: str
    rol: str                         # "usuario" | "amadeus" | "sistema"
    texto: str                       # limpio (sin etiquetas)
    canal: str = "terminal"
    etiquetas: Etiquetas | None = None   # solo en turnos de "amadeus"

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"id": self.id, "ts": self.ts, "rol": self.rol, "texto": self.texto,
                "canal": self.canal,
                "etiquetas": self.etiquetas.a_dict() if self.etiquetas is not None else None}


@dataclass(frozen=True)
class ItemAgenda:
    """Recordatorio o "cápsula del tiempo" de la agenda."""

    id: int
    tipo: str                        # "recordatorio" | "capsula"
    vence_ts: str                    # UTC ISO
    mensaje: str
    estado: str = "pendiente"        # "pendiente" | "entregado" | "cancelado"
    creado_ts: str = ""              # UTC ISO (columna agenda.creado_ts); lo usa la plantilla "capsula" ({creada})

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"id": self.id, "tipo": self.tipo, "vence_ts": self.vence_ts,
                "mensaje": self.mensaje, "estado": self.estado, "creado_ts": self.creado_ts}


# ---------------------------------------------------------------------------
# §4.3.5 Acciones, permisos y diagnóstico
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccionPC:
    """Acción sobre el PC propuesta por el enrutador local (nunca por el LLM directamente)."""

    tipo: str                                   # ∈ TIPOS_ACCION
    args: Mapping[str, Any] = field(default_factory=dict, hash=False)
    descripcion: str = ""                       # en español, legible: "cerrar kitty «nvim main.py»"
    origen: str = "usuario"                     # ∈ ORIGENES_ACCION

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", dict(self.args) if isinstance(self.args, Mapping) else {})

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"tipo": self.tipo, "args": _a_json(self.args), "descripcion": self.descripcion,
                "origen": self.origen}


@dataclass(frozen=True)
class DecisionPermiso:
    """Veredicto de la compuerta de permisos."""

    veredicto: str                              # "permitida" | "confirmar" | "denegada" (resultado de evaluar)
    motivo: str                                 # en español; si se denegó o no se confirmó, explica por qué
    destructiva: bool = False
    aprobada: bool = False                      # decisión FINAL de autorizar(): True si se puede ejecutar


@dataclass(frozen=True)
class ResultadoAccion:
    """Resultado de ejecutar una acción local."""

    ok: bool
    mensaje: str                                # en español, para el contexto de Broca
    datos: Mapping[str, Any] = field(default_factory=dict, hash=False)
    accion_tipo: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "datos", dict(self.datos) if isinstance(self.datos, Mapping) else {})

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"ok": bool(self.ok), "mensaje": self.mensaje, "datos": _a_json(self.datos),
                "accion_tipo": self.accion_tipo}


class EstadoModulo(str, Enum):
    """Estado de un módulo en el arranque y en el HUD."""

    OK = "OK"
    EN_ESPERA = "EN ESPERA"
    SIN_CONEXION = "SIN CONEXIÓN"
    DEGRADADO = "DEGRADADO"
    OFF = "OFF"

    def __str__(self) -> str:
        return self.value

    def __format__(self, especificacion: str) -> str:
        return format(self.value, especificacion)


def _estado_modulo(valor: Any) -> EstadoModulo:
    """Convierte un valor (Enum, valor o nombre) en ``EstadoModulo``; desconocido ⇒ ``DEGRADADO``."""
    if isinstance(valor, EstadoModulo):
        return valor
    texto = str(valor).strip()
    for miembro in EstadoModulo:
        if texto == miembro.value or texto.upper() == miembro.name:
            return miembro
    return EstadoModulo.DEGRADADO


@dataclass(frozen=True)
class DiagnosticoModulo:
    """Diagnóstico de un módulo (línea del arranque y payload de ``sistema.modulo``)."""

    clave: str        # "memoria" | "conectoma" | "broca" | "camara" | "telemetria" | "hyprland" | "nexus" |
                      # "vts" | "voz" | "telegram" | "web" | "hud" | "control"
                      # v0.2: "escritorio" | "control_pc" | "vision_camara" | "vision_pantalla"
                      # v0.2.2: "google" | "correo" | "drive" | "taller"
    nombre: str      # nombre para el arranque, p. ej. "Área de Broca (Gemini)"
    estado: EstadoModulo
    detalle: str = ""
    latencia_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "estado", _estado_modulo(self.estado))
        if self.latencia_ms is not None:
            object.__setattr__(self, "latencia_ms", _entero(self.latencia_ms, 0, minimo=0))

    def a_dict(self) -> dict[str, Any]:
        """``{"clave","nombre","estado","detalle","latencia_ms"}`` con el estado como texto."""
        return {"clave": self.clave, "nombre": self.nombre, "estado": self.estado.value,
                "detalle": self.detalle, "latencia_ms": self.latencia_ms}


# ---------------------------------------------------------------------------
# §4.3.6 Contexto que recibe Broca
# ---------------------------------------------------------------------------


def _tupla_de(valor: Any, tipo: type) -> tuple[Any, ...]:
    """Normaliza a tupla de elementos de ``tipo`` (descarta el resto)."""
    if valor is None:
        return ()
    try:
        return tuple(v for v in valor if isinstance(v, tipo))
    except TypeError as exc:
        log.debug("Secuencia de %s no iterable (%s); se ignora", tipo.__name__, type(exc).__name__)
        return ()


_RE_CONTROL_PERCEPCION: Final = re.compile(r"[\x00-\x1f\x7f]+")


def _texto_percepcion(valor: Any) -> str:
    """Texto corto de una sola línea (sin caracteres de control, ≤ ``TITULO_CORTO_MAX``); ``None`` ⇒ ``""``."""
    if valor is None or isinstance(valor, (bytes, bytearray)):
        return ""
    texto = _RE_CONTROL_PERCEPCION.sub(" ", str(valor)).strip()
    return texto[:TITULO_CORTO_MAX].rstrip()


def _bool_opcional(valor: Any) -> bool | None:
    """``None`` o booleano (acepta ``bool``/enteros y tipos con ``__bool__``; texto o NaN ⇒ ``None``)."""
    if valor is None or isinstance(valor, (str, bytes, bytearray)):
        return None
    if isinstance(valor, float) and not math.isfinite(valor):
        return None
    try:
        return bool(valor)
    except Exception as exc:  # __bool__ roto o ambiguo (p. ej. un array de varios elementos)
        log.debug("percepcion: valor booleano no válido (%s); uso None", type(exc).__name__)
        return None


def _real_percepcion(valor: Any, minimo: float, maximo: float | None) -> float | None:
    """Float finito opcional acotado; booleanos y texto ⇒ ``None``."""
    if isinstance(valor, (bool, str, bytes, bytearray)):
        return None
    return _real_opcional(valor, minimo, maximo)


def normalizar_percepcion(valor: Any) -> Mapping[str, Any] | None:
    """Normaliza ``ContextoBroca.percepcion`` (§C2.2) a un ``MappingProxyType`` de solo lectura.

    Conserva **solo** las claves de ``CLAVES_PERCEPCION`` que vengan (no rellena las ausentes):
    ``app``/``titulo_corto`` → texto de una línea ≤ 60 (``None`` ⇒ ``""``); ``video``,
    ``pantalla_completa`` y ``rostro`` → ``bool | None``; ``atencion`` → ``float | None`` en 0..1;
    ``dist_m`` → ``float | None`` ≥ 0. ``None`` o algo que no sea un ``Mapping`` ⇒ ``None``.
    Nunca lanza.
    """
    if valor is None:
        return None
    if not isinstance(valor, Mapping):
        log.debug("percepcion no es un Mapping (%s); se ignora", type(valor).__name__)
        return None
    limpio: dict[str, Any] = {}
    try:
        for clave in CLAVES_PERCEPCION:
            if clave not in valor:
                continue
            crudo = valor[clave]
            if clave in ("app", "titulo_corto"):
                limpio[clave] = _texto_percepcion(crudo)
            elif clave in ("video", "pantalla_completa", "rostro"):
                limpio[clave] = _bool_opcional(crudo)
            elif clave == "atencion":
                limpio[clave] = _real_percepcion(crudo, 0.0, 1.0)
            else:  # dist_m
                limpio[clave] = _real_percepcion(crudo, 0.0, None)
    except Exception as exc:  # Mapping exótico (get/__contains__ rotos): nunca lanza
        log.debug("percepcion no legible (%s); se ignora", type(exc).__name__)
        return None
    return MappingProxyType(limpio)


@dataclass(frozen=True)
class ContextoBroca:
    """Todo lo que Broca necesita para verbalizar un turno (Broca no decide nada)."""

    estado: HomeostaticState
    modo_salida: str = "terminal"                       # ∈ MODOS_SALIDA; lo decide main UNA vez (§10.21) y fija el canal
    canal_entrada: str = "terminal"                     # ∈ CANALES_ENTRADA, o "iniciativa"
    presencia: EstadoPresencia | None = None
    telemetria: Telemetria | None = None                # solo si es relevante (reporte o alerta)
    vision: ResultadoVistazo | None = None              # solo si hubo un vistazo en este turno
    vistazos_pendientes: int = 0                        # vistazos no comentados aún (§9.2); solo en turnos locales
    memoria_relevante: tuple[Recuerdo, ...] = ()
    ventana_activa: tuple[Turno, ...] = ()              # historial reciente SIN el turno actual
    resumen_reciente: str | None = None                 # último KV consolidado
    busqueda_web: tuple[ResultadoWeb, ...] | None = None  # None = no se buscó · () = sin resultados
    evento_social: str | None = None                    # ver §4.4.4
    llamadas_perdidas: int = 0                          # solo con evento_social == "regreso" (§10.21)
    consolidaciones_ausente: int = 0                    # solo con evento_social == "regreso"
    alerta_critica: bool = False
    despedida_especial: bool = False                    # permite «El Psy Kongroo» (§10.18, Anexo B regla 13)
    nexus: Mapping[str, Any] | None = field(default=None, hash=False)  # SIEMPRE resumen_nexus(...), nunca el payload crudo
    accion: ResultadoAccion | None = None               # resultado de una acción local ejecutada este turno
    ultima_bitacora: str | None = None                  # solo en el primer turno de la sesión
    instruccion_extra: str | None = None                # p. ej. motivo de una iniciativa, detalle de una alerta
    iniciativa: str | None = None                       # ∈ INICIATIVAS cuando canal_entrada == "iniciativa"
    publico: bool = False                               # respuesta visible para terceros (chat de colaboración Nexus)
    fecha_hora: datetime | None = None                  # aware, America/Bogota
    # v0.2 (§C2.2): lo que Amadeus percibe en local, sin tokens ni imágenes. Claves permitidas
    # (las demás se descartan; se guarda como MappingProxyType):
    #   app: str ("" si no se sabe) · titulo_corto: str (≤ 60; "" si la ventana es privada)
    #   video: bool | None · pantalla_completa: bool | None · rostro: bool | None
    #   atencion: float | None (0..1) · dist_m: float | None
    percepcion: Mapping[str, Any] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        poner = object.__setattr__
        if not isinstance(self.estado, HomeostaticState):
            poner(self, "estado", HomeostaticState.desde_dict(self.estado)
                  if isinstance(self.estado, Mapping) else HomeostaticState.neutral())
        poner(self, "modo_salida", self.modo_salida if self.modo_salida in MODOS_SALIDA else "terminal")
        if self.canal_entrada not in CANALES_ENTRADA and self.canal_entrada != "iniciativa":
            poner(self, "canal_entrada", "terminal")
        poner(self, "vistazos_pendientes", _entero(self.vistazos_pendientes, 0, minimo=0))
        poner(self, "llamadas_perdidas", _entero(self.llamadas_perdidas, 0, minimo=0))
        poner(self, "consolidaciones_ausente", _entero(self.consolidaciones_ausente, 0, minimo=0))
        poner(self, "memoria_relevante", _tupla_de(self.memoria_relevante, Recuerdo))
        poner(self, "ventana_activa", _tupla_de(self.ventana_activa, Turno))
        if self.busqueda_web is not None:
            poner(self, "busqueda_web", _tupla_de(self.busqueda_web, ResultadoWeb))
        poner(self, "iniciativa", self.iniciativa if self.iniciativa in INICIATIVAS else None)
        poner(self, "alerta_critica", bool(self.alerta_critica))
        poner(self, "despedida_especial", bool(self.despedida_especial))
        poner(self, "publico", bool(self.publico))
        poner(self, "percepcion", normalizar_percepcion(self.percepcion))


# ---------------------------------------------------------------------------
# §4.3.7 Protocolos para inyección entre grupos
# ---------------------------------------------------------------------------


@runtime_checkable
class SalidaAvatar(Protocol):
    """Avatar que expresa etiquetas y mueve la boca (lo implementa ``VTubeBridge``)."""

    en_linea: bool

    async def aplicar_etiquetas(self, etiquetas: Etiquetas) -> None: ...

    async def boca(self, valor: float) -> None: ...

    async def cerrar_boca(self) -> None: ...


@runtime_checkable
class RegistroLlamadas(Protocol):
    """Memoria que necesita la salida (lo implementa ``MemoryEngine``)."""

    async def registrar_llamada(self, *, origen: str, canal: str, nivel: int, motivo: str,
                                texto: str = "") -> int: ...

    async def marcar_llamada(self, llamada_id: int, contestada: bool) -> None: ...

    async def llamadas_ignoradas(self, horas: float = 24.0, gracia_min: float = 120.0) -> int: ...

    async def contar_proactivos_hoy(self) -> int: ...

    async def registrar_turno(self, rol: str, texto: str, *, canal: str = "terminal",
                              etiquetas: Etiquetas | None = None) -> int: ...

    async def meta_get(self, clave: str) -> str | None: ...

    async def meta_set(self, clave: str, valor: str) -> None: ...


@runtime_checkable
class ProveedorHypr(Protocol):
    """Acceso a Hyprland (lo implementa ``ControlHyprland`` de ``aura_vision_pc``)."""

    disponible: bool

    async def consultar(self, que: str) -> Any: ...      # "clients" | "activewindow" | "workspaces" | "monitors"

    async def enfocar_direccion(self, direccion: str) -> bool: ...
