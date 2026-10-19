"""Enlace WebSocket ``/escritorio`` entre R.A.M.S. y Amadeus Desktop o el chat del HUD (§C2.4).

Qué hace (``docs/COMPANION.md`` §C1.5, §C2.3 y §C2.4):
- Vive dentro del mismo servidor que el HUD (``interface/hud_server.py`` lo compone): mismo puerto,
  ``127.0.0.1`` y nunca ``0.0.0.0``.
- **Token propio** (``secrets.token_urlsafe(16)``, distinto del del HUD y nuevo en cada arranque),
  escrito de forma atómica (temporal en el mismo directorio + ``os.replace``) y con 0o600 en
  ``settings.escritorio_token_file`` como ``{"url", "token", "pid"}``. Al cerrar se borra solo si
  el archivo sigue teniendo el token y el pid propios. El token nunca va a logs, ``repr``,
  diagnósticos ni al ``hola``.
- **Handshake:** ``?t=<token>`` comparado con ``hmac.compare_digest``. Sin ``Origin`` = Amadeus
  Desktop (el ``WebSocket`` global de Node no lo envía; uno nuevo **reemplaza** al anterior, que
  se cierra con 4001). Con ``Origin`` del HUD (``http://127.0.0.1|localhost:<puerto>``, solo con
  ``hud_chat``) = panel de chat del HUD, hasta 2 clientes. Cualquier otro caso ⇒ 403.
- **Entrantes** (≤ 8 KiB, JSON inválido o tipo desconocido ⇒ se ignora con ``log.debug``; ritmo
  por cliente **y por clase**, §C2.4.5: ``chat`` y lo no reconocido ≤ 5/s; ``caja`` y
  ``evento{toque, soltada, cursor_encima}`` ≤ 5/s; ``hola_app``, ``control``, ``confirmacion`` y
  ``evento{mostrada, ocultada, chat_abierto}`` ≤ 20/s con ``log.info`` al descartar, y de
  ``mostrada``/``ocultada`` nunca se pierde la última: se publica al vencer la ventana).
  ``hola_app`` → ``escritorio.app``; ``chat`` → ``escritorio.chat`` (**nunca**
  ``usuario.entrada``: el texto solo lo consume main y nadie lo registra); ``evento`` →
  ``escritorio.evento``; ``control`` → ``escritorio.control`` (``silenciar`` → ``hud.control``);
  ``confirmacion`` → ``escritorio.confirmacion``; ``caja`` → ``escritorio.caja`` (≤ 2 Hz y solo si
  cambia). ``hola_app``, ``evento`` y ``caja`` solo se aceptan del cliente sin ``Origin``. Publica
  ``escritorio.conectado`` al conectar y al desconectar.
- **Salientes:** ``hola``, ``subtitulo``, ``voz``, ``permiso`` (con ``vista_previa``), ``sistema``,
  ``travesuras`` y ``no_molestar`` (de ``pc.no_molestar``: ``{activo, motivo, usuario}``) a todos;
  ``orden``, ``estado`` (≤ 1 Hz) y ``vision`` (``{camara: bool}`` = OR de ``vision.camara`` por
  ``fuente``, solo al cambiar: el ojo del overlay, §C2.13) solo a Amadeus Desktop. Al conectar,
  cada cliente recibe ``hola`` y la instantánea: permiso pendiente, travesuras, ``no_molestar``
  (siempre; sin ``pc.no_molestar`` previo = todo ``false``) y, para Electron, ``vision``
  (siempre) y el último ``estado``.

Reglas del bus (§C2.3): nadie se suscribe a ``"*"`` ni a ``"escritorio.*"``; este módulo publica
``escritorio.chat`` pero no lo escucha ni lo registra.

**Respaldo Puter (§C2.15).** :class:`RespaldoPuter` es el proveedor de respaldo del Área de Broca
(``lobes.broca_llm.ProveedorRespaldo``): pide el texto a Amadeus Desktop, que lo genera con Puter.js
en una ventana oculta con la cuenta de Puter del usuario. Solo con el cliente sin ``Origin``:
entra ``puter.estado{listo, sesion, modelo, error}`` (estado de la sesión) y ``llm.respuesta{id, ok,
texto, error, modelo, ms}`` (≤ 64 KiB; el resto de entrantes sigue en ≤ 8 KiB); sale
``llm.pedir{id, modelo, system, mensajes, max_tokens, temperatura}`` (nunca al chat del HUD).
Correlación por ``id`` (hex de 24), como mucho 2 pedidos en curso en la app (un pedido conserva su
turno hasta su ``llm.respuesta``, aunque llegue después de los ≤ 40 s que espera R.A.M.S.) y, al
desconectarse o reemplazarse la app, los pedidos pendientes devuelven ``None``. Los límites del
pedido se miden en unidades UTF-16 (como los valida la app en JavaScript). Ritmo: un
``puter.estado`` sobre el cupo se aplica al vencer la ventana (gana el último) y una
``llm.respuesta`` a un pedido pendiente nunca se descarta. El prompt y las respuestas nunca se
registran. Publica ``sistema.modulo`` con la clave ``"puter"`` al cambiar.

**Tarjetas aprobables (v0.2.2, ``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §12).** ``tarjeta.mostrar``
(correo, Drive, script o la salida de un script) se sanea con el esquema exacto de §12.2 (claves
desconocidas fuera, una sola sección por clase) y sale como ``{"tipo":"tarjeta","datos":{…,
"estado":"pendiente"|"info"}}`` a **todos** los clientes; ``tarjeta.cerrada`` sale como
``{"tipo":"tarjeta","datos":{id, clase, estado:"cerrada", resultado, motivo}}``. Todo mensaje de
tarjeta cabe en 60 KiB (``bytes_json`` ≤ ``MAX_BYTES_TARJETA``): la de ``script`` se encoge de forma
determinista (nunca el código) y lo que no cabe se descarta. Los campos **aprobables** (destinatarios,
asunto, cuerpo y nombres de adjuntos del correo; ``local.nombre`` y ``compartir.con`` de Drive; el
código del script) **nunca** se recortan: si uno no cumple su tope (unidades UTF-16; el código en
bytes UTF-8), la tarjeta entera se descarta. Se guardan como mucho ``MAX_TARJETAS_VIVAS`` vigentes
para la instantánea. Entra ``tarjeta_decision{id, sha256, decision, version, nombre?, campos?}``
(≤ 60 KiB, de la app **y** del chat del HUD, ≤ 5/s) → ``escritorio.tarjeta`` (solo la consume main;
nunca se registran los campos). ``version`` es la versión que el cliente **muestra** (obligatoria
salvo en ``descartar``, por compatibilidad). Si la tarjeta está vigente aquí, una decisión que no sea
``descartar`` se ignora (y se le reenvía la tarjeta a ese cliente) cuando su versión o su huella no
son las vigentes, cuando la tarjeta no ofrece ese botón, cuando otra decisión sobre ella sigue **en
curso** o cuando es la segunda confirmación de un enlace público llegada < ``ARMADO_PUBLICO_S`` tras
mostrar el paso 2. Al aceptar una, la tarjeta pasa a «en curso» hasta que main la republica o la
cierra (≤ ``EN_CURSO_MAX_S``): se difunde y se da en la instantánea sin sus botones de decisión, así
que ningún cliente (ni uno que reconecta) puede volver a pulsarla mientras main la ejecuta.

Dependencias opcionales: ``websockets`` (≥ 14, API ``websockets.asyncio``). Sin ella el módulo se
importa igual y el servidor del HUD queda en ``OFF``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from core.bus import BusEventos, Evento
from core.config import Settings
from core.contracts import VERSION, DiagnosticoModulo, EstadoModulo

try:  # contratos v0.2 (grupo base); respaldo literal de §C2.2 por si faltaran
    from core.contracts import ACCIONES_ORDEN_ESCRITORIO, CONTROLES_ESCRITORIO, EVENTOS_ESCRITORIO
except ImportError:  # pragma: no cover - solo con un core/contracts.py anterior a la v0.2
    ACCIONES_ORDEN_ESCRITORIO = ("aparecer", "esconderse", "abrir_chat", "notificar")
    EVENTOS_ESCRITORIO = ("toque", "soltada", "cursor_encima", "mostrada", "ocultada", "chat_abierto")
    CONTROLES_ESCRITORIO = ("no_molestar_on", "no_molestar_off", "basta_travesuras", "pausar_vision")

try:  # contratos v0.2.2 (grupo base, docs/V022_CORREO_DRIVE_SCRIPTS.md §3.1)
    from core.contracts import (
        BOTONES_TARJETA,
        CATEGORIAS_RIESGO,
        CLASES_TARJETA,
        DECISIONES_TARJETA,
        HUELLA_RE,
        ID_TARJETA_RE,
        LENGUAJES_SCRIPT,
        MAX_ADJUNTOS_CORREO,
        MAX_ASUNTO_CORREO,
        MAX_BYTES_DECISION_TARJETA,
        MAX_BYTES_TARJETA,
        MAX_CODIGO_SCRIPT,
        MAX_CUERPO_CORREO,
        MAX_HALLAZGOS_TARJETA,
        MAX_SALIDA_TARJETA,
        MAX_TARJETAS_VIVAS,
        NIVELES_EJECUTABLES,
        NIVELES_RIESGO,
        RESULTADOS_TARJETA,
        ROLES_DRIVE,
        bytes_json,
        largo_utf16,
    )
except ImportError:  # pragma: no cover - solo con un core/contracts.py anterior a la v0.2.2
    import sys as _sys

    CLASES_TARJETA = ("correo", "drive", "script", "salida")
    BOTONES_TARJETA = ("aprobar", "aprobar_publico", "editar", "guardar", "copiar", "descartar")
    DECISIONES_TARJETA = ("aprobar", "aprobar_publico", "editar", "guardar", "descartar")
    RESULTADOS_TARJETA = ("enviado", "hecho", "ejecutado", "descartado", "caducado", "reemplazado",
                          "denegado", "fallo")
    LENGUAJES_SCRIPT = ("powershell", "python", "bash")
    NIVELES_RIESGO = ("bajo", "medio", "alto", "critico")
    NIVELES_EJECUTABLES = frozenset({"bajo", "medio", "alto"})
    CATEGORIAS_RIESGO = ("borrado", "sobrescritura", "registro", "servicios", "red", "descarga_ejecucion",
                         "credenciales", "elevacion", "apagado", "seguridad", "persistencia", "ofuscacion",
                         "dinamico", "fuera_de_carpeta", "subproceso", "winapi", "interactivo", "sintaxis",
                         "tamano", "analisis_degradado")
    ROLES_DRIVE = ("reader", "commenter", "writer")
    MAX_TARJETAS_VIVAS = 4
    MAX_CODIGO_SCRIPT = 24_576
    MAX_SALIDA_TARJETA = 8_192
    MAX_HALLAZGOS_TARJETA = 20
    MAX_BYTES_TARJETA = 60 * 1024
    MAX_BYTES_DECISION_TARJETA = 60 * 1024
    MAX_CUERPO_CORREO = 10_000
    MAX_ASUNTO_CORREO = 200
    MAX_ADJUNTOS_CORREO = 5
    ID_TARJETA_RE = re.compile(r"^[0-9a-f]{8}$")
    HUELLA_RE = re.compile(r"^[0-9a-f]{64}$")

    def largo_utf16(texto: str) -> int:
        """Unidades UTF-16 (``String.length`` de JavaScript); no-str ⇒ 0."""
        if not isinstance(texto, str):
            return 0
        return len(texto.encode("utf-16-le", "surrogatepass")) // 2

    def bytes_json(valor: Any) -> int:
        """Bytes UTF-8 del JSON compacto de ``valor``; no serializable ⇒ ``sys.maxsize``."""
        try:
            return len(json.dumps(valor, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False).encode("utf-8"))
        except Exception:  # fail-closed: no cabe en ningún tope
            return _sys.maxsize

try:  # opcional: sin websockets no hay servidor
    from websockets.asyncio.server import broadcast as _ws_broadcast
    from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
except ImportError:  # sin websockets: el enlace nunca se activa
    _ws_broadcast = None

    class _WsConnectionClosed(Exception):  # type: ignore[no-redef]
        """Sustituto para poder escribir ``except`` sin websockets instalado."""


__all__ = [
    "CODIGO_REEMPLAZADO",
    "ERRORES_PUTER",
    "EVENTOS_SUSCRITOS_ESCRITORIO",
    "EnlaceEscritorio",
    "NOMBRE_DIAG_PUTER",
    "NOMBRE_RESPALDO_PUTER",
    "RUTA_ESCRITORIO",
    "RespaldoPuter",
    "escribir_archivo_token",
    "borrar_archivo_token_si_propio",
]

log = logging.getLogger("rams.escritorio")

# ---------------------------------------------------------------------------
# Constantes (§C2.4)
# ---------------------------------------------------------------------------

RUTA_ESCRITORIO: Final = "/escritorio"
HOST: Final = "127.0.0.1"  # nunca 0.0.0.0

MAX_MENSAJES_CLIENTE_S: Final = 5       # clase "general": chat y lo no reconocido
MAX_RUIDO_CLIENTE_S: Final = 5          # clase "ruido": caja, toque, soltada, cursor_encima
MAX_ESTADO_CLIENTE_S: Final = 20        # clase "estado": hola_app, control, confirmacion, visibilidad
MAX_BYTES_MENSAJE: Final = 8 * 1024
MAX_TEXTO_CHAT: Final = 2000
MAX_CLIENTES_HUD: Final = 2
MAX_BYTES_DATOS_EVENTO: Final = 1024
MAX_CLAVES_DATOS_EVENTO: Final = 16
MAX_TEXTO_SUBTITULO: Final = 4000
MAX_TEXTO_SISTEMA: Final = 4000
MAX_TEXTO_NOTIFICAR: Final = 300
MAX_URL: Final = 300
MAX_VISTA_PREVIA: Final = 500
MAX_LADO_CAJA: Final = 2000
MAX_COORD_CAJA: Final = 100_000      # píxeles físicos globales: holgura para varios monitores
MAX_PID: Final = 2 ** 32
MAX_BUFFER_CLIENTE: Final = 1024 * 1024  # bytes sin escribir antes de soltar a un cliente lento

INTERVALO_CAJA_S: Final = 0.5            # ≤ 2 Hz
INTERVALO_ESTADO_S: Final = 1.0          # ≤ 1 Hz
INTERVALO_CURSOR_ENCIMA_S: Final = 10.0  # cursor_encima ≤ 1 cada 10 s

CODIGO_REEMPLAZADO: Final = 4001          # cierre del cliente de Electron reemplazado por otro
CODIGO_DEMASIADOS: Final = 1008           # carrera entre handshakes del HUD (política)

ID_CONFIRMACION_RE: Final = re.compile(r"[0-9a-f]{8}")             # se usa con fullmatch
_URL_HTTPS_RE: Final = re.compile(r"https://[^\s\x00-\x1f\x7f<>\"'`\\]+")  # fullmatch
_TIPO_SEGURO_RE: Final = re.compile(r"[a-z_]{1,24}")                # solo para registrar el tipo
_VERSION_SEGURA_RE: Final = re.compile(r"[0-9A-Za-z.+\-]{1,40}")

# Eventos del bus que reenvía /escritorio (nunca "*" ni "escritorio.*", §C2.3 regla a).
EVENTOS_SUSCRITOS_ESCRITORIO: Final = (
    "salida.subtitulo",
    "voz.inicio",
    "voz.fin",
    "escritorio.orden",
    "escritorio.sistema",
    "permiso.solicitado",
    "permiso.resuelto",
    "homeostasis.estado",
    "motor.travesuras",
    "pc.no_molestar",
    "vision.camara",
    # v0.2.2 (§12.4): tarjetas aprobables. Privadas: solo /escritorio, nunca /ws ni el log.
    "tarjeta.mostrar",
    "tarjeta.cerrada",
)

# ``vision.camara.fuente`` (§C2.3): sin ``fuente`` = "vistazo". El ojo del overlay es el OR (§C2.13).
FUENTES_CAMARA: Final = ("vistazo", "continua")

# ``pc.no_molestar.motivo`` (§C2.3); "usuario" = el "no molestar" que el usuario puso a mano.
MOTIVOS_NO_MOLESTAR: Final = ("usuario", "presentacion", "pantalla_completa", "ocupado", "juego", "pantalla", "")
NO_MOLESTAR_INICIAL: Final[dict[str, Any]] = {"activo": False, "motivo": "", "usuario": False}

# Destinos de un mensaje saliente.
_TODOS: Final = "todos"
_APP: Final = "app"

# Clases de ritmo de los entrantes (§C2.4.5): cada una con su propio presupuesto por cliente.
_GENERAL: Final = "general"
_RUIDO: Final = "ruido"
_ESTADO: Final = "estado"
_LLM: Final = "llm"                     # llm.respuesta del respaldo Puter (§C2.15)
MAX_LLM_CLIENTE_S: Final = 10
_TARJETA: Final = "tarjeta"             # tarjeta_decision (v0.2.2, §12.3)
MAX_TARJETA_CLIENTE_S: Final = 5
_PRESUPUESTOS: Final[dict[str, int]] = {
    _GENERAL: MAX_MENSAJES_CLIENTE_S,
    _RUIDO: MAX_RUIDO_CLIENTE_S,
    _ESTADO: MAX_ESTADO_CLIENTE_S,
    _LLM: MAX_LLM_CLIENTE_S,
    _TARJETA: MAX_TARJETA_CLIENTE_S,
}
_EVENTOS_RUIDO: Final = frozenset({"toque", "soltada", "cursor_encima"})
_EVENTOS_VISIBILIDAD: Final = frozenset({"mostrada", "ocultada"})
_TIPOS_ESTADO: Final = frozenset({"hola_app", "control", "confirmacion", "puter.estado"})

# --- Respaldo Puter (§C2.15): IA de respaldo del Área de Broca dentro de Amadeus Desktop ---
NOMBRE_RESPALDO_PUTER: Final = "puter"                      # ProveedorRespaldo.nombre
CLAVE_DIAG_PUTER: Final = "puter"
NOMBRE_DIAG_PUTER: Final = "Respaldo Puter (Amadeus Desktop)"
ERRORES_PUTER: Final = ("sin_sesion", "timeout", "cuota", "red", "rechazado", "otro")
MAX_SYSTEM_PUTER: Final = 60_000
MAX_MENSAJES_PUTER: Final = 40
MAX_TEXTO_MENSAJE_PUTER: Final = 20_000
MAX_BYTES_PEDIDO_PUTER: Final = 768 * 1024   # JSON de llm.pedir: deja margen bajo MAX_BUFFER_CLIENTE
MAX_TOKENS_PUTER: Final = 8192
MAX_PEDIDOS_PUTER: Final = 2                  # Amadeus Desktop procesa como mucho 2 a la vez
TIMEOUT_PUTER_S: Final = 40.0                 # R.A.M.S. espera ≤ 40 s (el escritorio responde en ≤ 45 s)
# Un pedido enviado ocupa su turno hasta que llega SU llm.respuesta (aunque completar() ya haya
# devuelto None por su plazo): el escritorio lo sigue teniendo en curso y rechazaría un 3.º. Si la
# respuesta no llega, el turno se suelta a los 45 s del envío (lo que garantiza el escritorio) + 1.
HUECO_PUTER_MAX_S: Final = 46.0
# Tras esperar turno, si queda menos de esto (o de la mitad del plazo, si es corto) no se envía:
# Puter no contestaría a tiempo y el pedido se cobraría igual en el saldo del usuario.
MIN_PLAZO_ENVIO_PUTER_S: Final = 5.0
ENFRIAMIENTO_CUOTA_PUTER_S: Final = 600.0     # tras "cuota" (saldo de Puter agotado) no se pide en 10 min
MAX_BYTES_RESPUESTA_LLM: Final = 64 * 1024    # llm.respuesta (= max_size del servidor del HUD)
MAX_TEXTO_RESPUESTA_LLM: Final = 32_000
_ID_PEDIDO_RE: Final = re.compile(r"[0-9a-f]{16,32}")                 # fullmatch
# fullmatch; el MISMO patrón que PUTER_MODEL (_RE_MODELO_PUTER de core/config.py) y que RE_MODELO de
# desktop/lib/puter.js (empieza por letra o cifra, ≤ 100 caracteres): lo que sale de aquí la app lo
# acepta (nunca un "rechazado" por el nombre) y el modelo que anuncia la app se entiende aquí.
_MODELO_PUTER_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,99}")
# Detalle del diagnóstico para los códigos de ``puter.estado.error`` que explican un "no listo"
# (con el escritorio real, ``sesion`` es false en todos ellos: no es que falte iniciar sesión).
_DETALLE_ERROR_ESTADO_PUTER: Final[dict[str, str]] = {
    "deshabilitado": "Puter desactivado en Amadeus Desktop (AMADEUS_PUTER=0)",
    "carga_fallida": "sin conexión con puter.com",
    "ventana_caida": "ventana de Puter caída (se recarga sola)",
}
_ERROR_PUTER_RE: Final = re.compile(r"[a-z_]{1,24}")                  # fullmatch (código corto)
_ROLES_PUTER: Final[dict[str, str]] = {
    "usuario": "usuario", "user": "usuario",
    "modelo": "modelo", "model": "modelo", "assistant": "modelo", "amadeus": "modelo",
}

# --- Tarjetas aprobables (v0.2.2, docs/V022_CORREO_DRIVE_SCRIPTS.md §12) ---
# Tope por tipo de los entrantes (§12.3): se decodifica con 64 KiB (= max_size del servidor) y, con el
# tipo en la mano, se aplica su tope. llm.respuesta solo de la app; el resto, como siempre, 8 KiB.
_MAX_BYTES_POR_TIPO: Final[Mapping[str, int]] = {
    "llm.respuesta": MAX_BYTES_RESPUESTA_LLM,
    "tarjeta_decision": MAX_BYTES_DECISION_TARJETA,
}
MAX_DIRECCIONES_TARJETA: Final = 50          # Para + CC + CCO (presupuesto de §12.2)
MAX_NOMBRE_HABILIDAD: Final = 40
# Topes (unidades UTF-16) de los campos de un «editar» del correo (§12.3). Nunca se recortan.
TOPES_CAMPOS_EDITAR: Final[Mapping[str, int]] = {
    "para": 2600, "cc": 2600, "cco": 2600, "asunto": MAX_ASUNTO_CORREO, "cuerpo": MAX_CUERPO_CORREO,
    "adjuntos": 1000,
}
CANALES_ORIGEN_TARJETA: Final = ("terminal", "escritorio", "telegram")
OPERACIONES_TARJETA_DRIVE: Final = ("subir", "papelera", "compartir")
PLATAFORMAS_SCRIPT: Final = ("windows", "linux")
METODOS_ANALISIS: Final = ("ast", "heuristico")
# Botones que tienen sentido en cada clase (§12.2): el resto se quita (defensa en profundidad).
BOTONES_POR_CLASE: Final[Mapping[str, frozenset[str]]] = {
    "correo": frozenset({"aprobar", "editar", "descartar"}),
    "drive": frozenset({"aprobar", "aprobar_publico", "descartar"}),
    "script": frozenset({"aprobar", "guardar", "copiar", "descartar"}),
    "salida": frozenset({"descartar"}),
}
# Controles que se quitan de los textos informativos (los mismos que textoSeguro de Electron:
# se conservan \t, \n y \r).
_CONTROLES_INFO_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Una decisión aceptada deja su tarjeta «en curso» (sin botones de decisión, en la difusión y en la
# instantánea) hasta que main la republica o la cierra. Red de seguridad por si main nunca contestara:
# por encima del peor caso real (script ≤ 300 s + muerte del árbol; correo ≤ 120 s por envío).
EN_CURSO_MAX_S: Final = 900.0
# Compartir en público: la 2.ª confirmación no vale si llega antes de esto tras mostrar el paso 2
# (un doble clic sobre «Compartir» no puede completar las dos). Los clientes esperan 1 s.
ARMADO_PUBLICO_S: Final = 0.8
MAX_VERSION_TARJETA: Final = 2 ** 31 - 1
# Decisiones que llegan a main y bloquean la tarjeta hasta su respuesta (descartar nunca se bloquea).
_DECISIONES_EN_CURSO: Final = frozenset({"aprobar", "aprobar_publico", "editar", "guardar"})
_SUSTITUTOS_RE: Final = re.compile(r"[\ud800-\udfff]")
_CONTROLES_RE: Final = re.compile(r"[\x00-\x1f\x7f]")              # campos de una línea
_CONTROLES_CUERPO_RE: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # cuerpo: solo \t y \n


# ---------------------------------------------------------------------------
# Saneado (los payloads del bus y de los clientes se tratan con cautela)
# ---------------------------------------------------------------------------


def _texto(valor: Any, maximo: int) -> str:
    """Texto recortado a ``maximo`` caracteres (con «…»); ``None`` ⇒ ``""``."""
    if valor is None:
        return ""
    texto = valor if isinstance(valor, str) else str(valor)
    if len(texto) > maximo:
        return texto[: max(0, maximo - 1)] + "…"
    return texto


def _real(valor: Any, defecto: float | None = None) -> float | None:
    """Número real finito (``bool`` no cuenta) o ``defecto``."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return defecto
    numero = float(valor)
    return numero if math.isfinite(numero) else defecto


def _acotar(valor: Any, minimo: float, maximo: float, defecto: float) -> float:
    """Real acotado a ``[minimo, maximo]`` (``defecto`` si no es un número finito)."""
    numero = _real(valor)
    if numero is None:
        return defecto
    return min(maximo, max(minimo, numero))


def _es_entero(valor: Any) -> bool:
    """``True`` si es un ``int`` de verdad (``bool`` no cuenta)."""
    return isinstance(valor, int) and not isinstance(valor, bool)


def _url_https_valida(url: Any) -> bool:
    """``True`` si ``url`` es ``https://`` con host, sin espacios ni controles y ≤ ``MAX_URL``."""
    if not isinstance(url, str) or len(url) > MAX_URL or not _URL_HTTPS_RE.fullmatch(url):
        return False
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _por_defecto_json(valor: Any) -> Any:
    """``default`` de ``json.dumps``: tipos no estándar a algo representable."""
    if isinstance(valor, Enum):
        return valor.value
    if isinstance(valor, (set, frozenset, tuple)):
        return list(valor)
    if isinstance(valor, Mapping):
        return dict(valor)
    return str(valor)


def _serializar(tipo: str, datos: Mapping[str, Any]) -> str:
    """Mensaje ``{"tipo", "datos"}`` como JSON estricto y compacto (UTF-8 sin escapar)."""
    return json.dumps({"tipo": tipo, "datos": dict(datos)}, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False, default=_por_defecto_json)


def _datos_evento_validos(datos: Any) -> dict[str, Any] | None:
    """``datos`` de un ``evento`` de Electron: dict de escalares, ≤ 16 claves y ≤ 1 KiB en JSON."""
    if datos is None:
        return {}
    if not isinstance(datos, dict) or len(datos) > MAX_CLAVES_DATOS_EVENTO:
        return None
    limpio: dict[str, Any] = {}
    for clave, valor in datos.items():
        if not isinstance(clave, str) or not 0 < len(clave) <= 40:
            return None
        if valor is None or isinstance(valor, (bool, str)):
            limpio[clave] = valor
        elif isinstance(valor, (int, float)):
            if not math.isfinite(float(valor)):
                return None
            limpio[clave] = valor
        else:
            return None  # listas, dicts y demás: solo escalares
    try:
        tamano = len(json.dumps(limpio, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                     .encode("utf-8"))
    except (TypeError, ValueError):
        return None
    return limpio if tamano <= MAX_BYTES_DATOS_EVENTO else None


def _orden_saneada(datos: Mapping[str, Any]) -> dict[str, Any] | None:
    """Payload de ``escritorio.orden`` saneado según §C2.3 (o ``None`` si no es válido)."""
    accion = datos.get("accion")
    if not isinstance(accion, str) or accion not in ACCIONES_ORDEN_ESCRITORIO:
        return None
    salida: dict[str, Any] = {"accion": accion}
    if accion == "aparecer":
        borde = datos.get("borde")
        if borde in ("left", "right"):
            salida["borde"] = borde
        modo = datos.get("modo")
        if modo in ("entrar", "asomarse"):
            salida["modo"] = modo
    elif accion == "notificar":
        texto = _texto(datos.get("texto"), MAX_TEXTO_NOTIFICAR).strip()
        if not texto:
            return None
        salida["texto"] = texto
        url = datos.get("url")
        if _url_https_valida(url):
            salida["url"] = url
    return salida


def _sobre(mensaje: Mapping[str, Any]) -> Mapping[str, Any]:
    """``datos`` de un entrante con sobre ``{tipo, datos}`` (si no trae ``datos``, el propio mensaje)."""
    datos = mensaje.get("datos")
    return datos if isinstance(datos, Mapping) else mensaje


def _modelo_puter(valor: Any) -> str | None:
    """Nombre de modelo de Puter seguro (``openai/gpt-…``, ``claude-…``) o ``None``."""
    if isinstance(valor, str) and _MODELO_PUTER_RE.fullmatch(valor):
        return valor
    return None


def _recortar_utf16(texto: str, maximo: int, marca: str = "") -> str:
    """``texto`` con como mucho ``maximo`` unidades UTF-16 (lo que mide ``String.length`` en JS).

    Amadeus Desktop valida los límites de ``llm.pedir`` en JavaScript, donde un emoji cuenta 2: un
    recorte por puntos de código de Python podría pasarse y el pedido volvería ``rechazado``. Si
    recorta, termina en ``marca`` (que cabe en ``maximo``); nunca parte un carácter.
    """
    if len(texto) <= maximo // 2 or len(texto.encode("utf-16-le")) <= 2 * maximo:
        return texto
    cupo = maximo - len(marca.encode("utf-16-le")) // 2
    unidades = 0
    for posicion, caracter in enumerate(texto):
        unidades += 2 if ord(caracter) > 0xFFFF else 1
        if unidades > cupo:
            return texto[:posicion] + marca
    return texto  # pragma: no cover - inalcanzable: el texto medía más de ``maximo``


def _estado_puter_saneado(datos: Mapping[str, Any]) -> dict[str, Any] | None:
    """``puter.estado{listo, sesion, modelo, error}`` saneado (``None`` si no es válido).

    ``listo`` = puter.js cargado **y** sesión iniciada: sin sesión nunca está listo.
    """
    listo = datos.get("listo")
    sesion = datos.get("sesion")
    if not isinstance(listo, bool) or not isinstance(sesion, bool):
        return None
    error = datos.get("error")
    return {
        "listo": listo and sesion,
        "sesion": sesion,
        "modelo": _modelo_puter(datos.get("modelo")),
        "error": error if isinstance(error, str) and _ERROR_PUTER_RE.fullmatch(error) else None,
    }


def _respuesta_llm_saneada(datos: Mapping[str, Any]) -> dict[str, Any] | None:
    """``llm.respuesta{id, ok, texto, error, modelo, ms}`` saneada (``None`` si el ``id`` no vale).

    ``ok`` solo si además trae texto no vacío; un ``error`` fuera de la lista cerrada pasa a ``"otro"``.
    """
    ident = datos.get("id")
    if not isinstance(ident, str) or not _ID_PEDIDO_RE.fullmatch(ident):
        return None
    texto = datos.get("texto")
    texto = texto.replace("\x00", "") if isinstance(texto, str) else None
    if texto is not None and len(texto) > MAX_TEXTO_RESPUESTA_LLM:
        texto = texto[:MAX_TEXTO_RESPUESTA_LLM]
    ok = datos.get("ok") is True and texto is not None and bool(texto.strip())
    error = datos.get("error")
    if ok:
        error = None
    elif not isinstance(error, str) or error not in ERRORES_PUTER:
        error = "otro"
    ms = datos.get("ms")
    return {
        "id": ident,
        "ok": ok,
        "texto": texto if ok else None,
        "error": error,
        "modelo": _modelo_puter(datos.get("modelo")),
        "ms": ms if _es_entero(ms) and 0 <= ms <= 10 * 60 * 1000 else None,
    }


def _pedido_puter_saneado(system: Any, mensajes: Any, *, modelo: str, max_tokens: Any,
                          temperatura: Any) -> dict[str, Any] | None:
    """Datos de ``llm.pedir`` (sin ``id``) con los límites de §C2.15; ``None`` si no hay mensajes.

    ``system`` ≤ 60 000 caracteres; los **últimos** 40 mensajes con ``rol ∈ {usuario, modelo}``
    (acepta también ``role``/``content``) y texto no vacío ≤ 20 000 (caracteres contados como en
    JavaScript, en unidades UTF-16); si el JSON pasa de ``MAX_BYTES_PEDIDO_PUTER``, se descartan
    los mensajes más antiguos (siempre queda el último). Tras recortar, la conversación sigue
    empezando por un mensaje del usuario (si lo hay). Mide cada mensaje una sola vez: nada de
    reserializar el pedido entero por cada descarte (no bloquea el loop).
    """
    sistema = system.replace("\x00", "") if isinstance(system, str) else ""
    sistema = _recortar_utf16(sistema, MAX_SYSTEM_PUTER)
    lista: list[dict[str, str]] = []
    for mensaje in mensajes if isinstance(mensajes, (list, tuple)) else ():
        if not isinstance(mensaje, Mapping):
            continue
        rol_crudo = mensaje.get("rol", mensaje.get("role"))
        rol = _ROLES_PUTER.get(rol_crudo.strip().lower()) if isinstance(rol_crudo, str) else None
        texto = mensaje.get("texto", mensaje.get("text", mensaje.get("content")))
        if rol is None or not isinstance(texto, str):
            continue
        texto = texto.replace("\x00", "")
        if not texto.strip():
            continue
        lista.append({"rol": rol, "texto": _recortar_utf16(texto, MAX_TEXTO_MENSAJE_PUTER, "…")})
    lista = lista[-MAX_MENSAJES_PUTER:]
    if not lista:
        return None
    tokens = max_tokens if _es_entero(max_tokens) and max_tokens > 0 else 1024
    datos: dict[str, Any] = {
        "modelo": modelo,
        "system": sistema,
        "mensajes": [],
        "max_tokens": min(tokens, MAX_TOKENS_PUTER),
        "temperatura": _acotar(temperatura, 0.0, 2.0, 1.0),
    }
    tope = MAX_BYTES_PEDIDO_PUTER - 64   # margen para el "id" que se añade al enviar
    # Bytes del JSON sin mensajes + los de cada mensaje (+1 por su coma: cota superior exacta).
    total = len(_serializar("llm.pedir", datos).encode("utf-8"))
    tamanos = [len(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1 for m in lista]
    total += sum(tamanos)
    inicio = 0
    while len(lista) - inicio > 1 and total > tope:
        total -= tamanos[inicio]
        inicio += 1
    while len(lista) - inicio > 1 and lista[inicio]["rol"] != "usuario" \
            and any(m["rol"] == "usuario" for m in lista[inicio:]):
        inicio += 1
    datos["mensajes"] = lista[inicio:]
    return datos


# ---------------------------------------------------------------------------
# Tarjetas aprobables (v0.2.2, §12.2–§12.3): saneado, presupuesto y decisiones
# ---------------------------------------------------------------------------


class _TarjetaInvalida(ValueError):
    """La tarjeta no cumple el esquema de §12.2. ``aprobable``: falló un campo que el usuario aprueba."""

    def __init__(self, aprobable: bool = False) -> None:
        super().__init__("tarjeta inválida")
        self.aprobable = aprobable


def _info(valor: Any, maximo: int) -> str:
    """Texto **informativo** de una tarjeta: ``""`` si no es str; sin controles (salvo ``\\t``, ``\\n`` y
    ``\\r``), sustitutos sueltos → U+FFFD y como mucho ``maximo`` unidades UTF-16 (recorta con «…»)."""
    if not isinstance(valor, str):
        return ""
    texto = _SUSTITUTOS_RE.sub("�", _CONTROLES_INFO_RE.sub("", valor))
    return _recortar_utf16(texto, maximo, "…")


def _info_o_none(valor: Any, maximo: int) -> str | None:
    """Como :func:`_info`, pero ``None`` (o un no-str) se queda en ``None``."""
    return _info(valor, maximo) if isinstance(valor, str) else None


def _aprobable(valor: Any, maximo: int, *, nulo: bool = True) -> str:
    """Texto **aprobable**: nunca se recorta ni se toca. ``None`` ⇒ ``""`` (si ``nulo``); otro tipo, un
    sustituto suelto o más de ``maximo`` unidades UTF-16 ⇒ :class:`_TarjetaInvalida` (aprobable)."""
    if valor is None and nulo:
        return ""
    if not isinstance(valor, str) or _SUSTITUTOS_RE.search(valor) or largo_utf16(valor) > maximo:
        raise _TarjetaInvalida(aprobable=True)
    return valor


def _codigo_aprobable(valor: Any) -> str:
    """``script.codigo``: str sin sustitutos sueltos de ≤ ``MAX_CODIGO_SCRIPT`` bytes UTF-8 (nunca se recorta)."""
    if not isinstance(valor, str) or _SUSTITUTOS_RE.search(valor) \
            or len(valor.encode("utf-8")) > MAX_CODIGO_SCRIPT:
        raise _TarjetaInvalida(aprobable=True)
    return valor


def _lista_info(valor: Any, n: int, maximo: int) -> list[str]:
    """Los primeros ``n`` textos informativos de una lista (lo que no es str se salta)."""
    if not isinstance(valor, (list, tuple)):
        return []
    return [_info(x, maximo) for x in valor if isinstance(x, str)][:n]


def _natural(valor: Any, defecto: int | None = 0) -> int | None:
    """Entero ≥ 0 (``bool`` no cuenta) o ``defecto``."""
    return valor if _es_entero(valor) and valor >= 0 else defecto


def _uno_de(valor: Any, opciones: tuple[str, ...] | frozenset[str]) -> str:
    """``valor`` si es uno de ``opciones``; si no, :class:`_TarjetaInvalida` (formato)."""
    if not isinstance(valor, str) or valor not in opciones:
        raise _TarjetaInvalida()
    return valor


def _seccion_correo(d: Any) -> dict[str, Any]:
    """Sección ``correo`` (§12.2). Destinatarios, asunto, cuerpo y nombres de adjuntos son aprobables."""
    if not isinstance(d, Mapping):
        raise _TarjetaInvalida()
    seccion: dict[str, Any] = {}
    total = 0
    for clave in ("para", "cc", "cco"):
        valor = d.get(clave)
        if valor is None:
            valor = []
        if not isinstance(valor, (list, tuple)):
            raise _TarjetaInvalida(aprobable=True)
        seccion[clave] = [_aprobable(x, 254, nulo=False) for x in valor]
        total += len(valor)
    if total > MAX_DIRECCIONES_TARJETA:
        raise _TarjetaInvalida(aprobable=True)
    seccion["asunto"] = _aprobable(d.get("asunto"), MAX_ASUNTO_CORREO)
    seccion["cuerpo"] = _aprobable(d.get("cuerpo"), MAX_CUERPO_CORREO)
    adjuntos = d.get("adjuntos")
    if adjuntos is None:
        adjuntos = []
    if not isinstance(adjuntos, (list, tuple)) or len(adjuntos) > MAX_ADJUNTOS_CORREO:
        raise _TarjetaInvalida(aprobable=True)
    seccion["adjuntos"] = []
    for adjunto in adjuntos:
        if not isinstance(adjunto, Mapping):
            raise _TarjetaInvalida(aprobable=True)
        seccion["adjuntos"].append({
            "nombre": _aprobable(adjunto.get("nombre"), 255, nulo=False),
            "bytes": _natural(adjunto.get("bytes")),
            "carpeta": _info(adjunto.get("carpeta"), 20),
        })
    seccion["problemas"] = _lista_info(d.get("problemas"), 10, 160)
    seccion["avisos"] = _lista_info(d.get("avisos"), 5, 160)
    seccion["listo"] = d.get("listo") is True
    return seccion


def _seccion_drive(d: Any) -> dict[str, Any]:
    """Sección ``drive`` (§12.2). ``local.nombre`` y ``compartir.con`` son aprobables."""
    if not isinstance(d, Mapping):
        raise _TarjetaInvalida()
    paso = d.get("paso")
    if not _es_entero(paso) or paso not in (1, 2):
        raise _TarjetaInvalida()
    seccion: dict[str, Any] = {"op": _uno_de(d.get("op"), OPERACIONES_TARJETA_DRIVE), "paso": paso}
    archivo = d.get("archivo")
    seccion["archivo"] = None if not isinstance(archivo, Mapping) else {
        "nombre": _info(archivo.get("nombre"), 200),
        "tipo": _info(archivo.get("tipo"), 40),
        "tamano": _natural(archivo.get("tamano"), None),
        "modificado": _info(archivo.get("modificado"), 32),
        "propio": archivo.get("propio") is True,
    }
    local = d.get("local")
    if local is not None and not isinstance(local, Mapping):
        raise _TarjetaInvalida(aprobable=True)
    seccion["local"] = None if local is None else {
        "nombre": _aprobable(local.get("nombre"), 255, nulo=False),
        "bytes": _natural(local.get("bytes")),
        "carpeta": _info(local.get("carpeta"), 20),
    }
    seccion["carpeta_destino"] = _info_o_none(d.get("carpeta_destino"), 200)
    compartir = d.get("compartir")
    if compartir is not None and not isinstance(compartir, Mapping):
        raise _TarjetaInvalida(aprobable=True)
    seccion["compartir"] = None if compartir is None else {
        "con": _aprobable(compartir.get("con"), 254),
        "rol": _uno_de(compartir.get("rol"), ROLES_DRIVE),
        "publico": compartir.get("publico") is True,
    }
    seccion["problemas"] = _lista_info(d.get("problemas"), 10, 160)
    seccion["listo"] = d.get("listo") is True
    return seccion


def _seccion_script(d: Any) -> dict[str, Any]:
    """Sección ``script`` (§12.2). Solo ``codigo`` es aprobable (en bytes UTF-8, nunca se recorta)."""
    if not isinstance(d, Mapping):
        raise _TarjetaInvalida()
    nivel = _uno_de(d.get("nivel"), NIVELES_RIESGO)
    gravedad = {n: i for i, n in enumerate(NIVELES_RIESGO)}
    hallazgos: list[dict[str, Any]] = []
    descartados = 0
    crudos = d.get("hallazgos")
    for h in crudos if isinstance(crudos, (list, tuple)) else ():
        if not isinstance(h, Mapping) or h.get("nivel") not in NIVELES_RIESGO \
                or h.get("categoria") not in CATEGORIAS_RIESGO:
            descartados += 1
            continue
        hallazgos.append({
            "nivel": h["nivel"],
            "categoria": h["categoria"],
            "linea": _natural(h.get("linea"), None),
            "detalle": _info(h.get("detalle"), 160),
        })
    # De más a menos grave (orden estable: dentro de un nivel, el del analizador).
    hallazgos.sort(key=lambda h: -gravedad[h["nivel"]])
    sobran = max(0, len(hallazgos) - MAX_HALLAZGOS_TARJETA)
    habilidad = _info(d.get("habilidad"), MAX_NOMBRE_HABILIDAD) or None
    return {
        "lenguaje": _uno_de(d.get("lenguaje"), LENGUAJES_SCRIPT),
        "plataforma": _uno_de(d.get("plataforma"), PLATAFORMAS_SCRIPT),
        "codigo": _codigo_aprobable(d.get("codigo")),
        "explicacion": _info(d.get("explicacion"), 600),
        "efectos": _lista_info(d.get("efectos"), 10, 160),
        "requisitos": _lista_info(d.get("requisitos"), 10, 80),
        "nivel": nivel,
        # Lo crítico nunca es ejecutable, diga lo que diga el payload (el freno real es la compuerta).
        "ejecutable": d.get("ejecutable") is True and nivel in NIVELES_EJECUTABLES,
        "motivo_no_ejecutable": _info(d.get("motivo_no_ejecutable"), 200),
        "metodo": _uno_de(d.get("metodo"), METODOS_ANALISIS),
        "hallazgos": hallazgos[:MAX_HALLAZGOS_TARJETA],
        "hallazgos_omitidos": (_natural(d.get("hallazgos_omitidos")) or 0) + descartados + sobran,
        "habilidad": habilidad,
        "habilidad_cambiada": d.get("habilidad_cambiada") is True,
        "ejecuciones": _natural(d.get("ejecuciones")),
    }


def _seccion_salida(d: Any) -> dict[str, Any]:
    """Sección ``salida`` (§12.2): solo informativa (stdout/stderr recortados a 8 192 unidades UTF-16)."""
    if not isinstance(d, Mapping):
        raise _TarjetaInvalida()
    script_id = d.get("script_id")
    if not isinstance(script_id, str) or not ID_TARJETA_RE.fullmatch(script_id):
        raise _TarjetaInvalida()
    codigo_salida = d.get("codigo_salida")
    duracion = _real(d.get("duracion_s"), 0.0)
    return {
        "script_id": script_id,
        "lenguaje": _uno_de(d.get("lenguaje"), LENGUAJES_SCRIPT),
        "codigo_salida": codigo_salida if _es_entero(codigo_salida) else None,
        "tiempo_agotado": d.get("tiempo_agotado") is True,
        "duracion_s": max(0.0, duracion if duracion is not None else 0.0),
        "stdout": _info(d.get("stdout"), MAX_SALIDA_TARJETA),
        "stderr": _info(d.get("stderr"), MAX_SALIDA_TARJETA),
        "recortada": d.get("recortada") is True,
        "carpeta": _info(d.get("carpeta"), 160),
    }


_SECCIONES_TARJETA: Final[Mapping[str, Callable[[Any], dict[str, Any]]]] = {
    "correo": _seccion_correo,
    "drive": _seccion_drive,
    "script": _seccion_script,
    "salida": _seccion_salida,
}


def _tarjeta_saneada(datos: Mapping[str, Any]) -> dict[str, Any]:
    """Payload de ``tarjeta.mostrar`` saneado con el esquema **exacto** de §12.2 (más ``estado``).

    Claves desconocidas fuera; solo la sección de su clase (las demás, ``null``); botones válidos para
    la clase, sin repetir y en orden. Lanza :class:`_TarjetaInvalida` si el id, la clase, la versión, la
    huella o la sección no cumplen (``aprobable=True`` si falló un campo aprobable: nunca se recorta).
    """
    ident = datos.get("id")
    clase = datos.get("clase")
    version = datos.get("version")
    huella = datos.get("sha256")
    if not isinstance(ident, str) or not ID_TARJETA_RE.fullmatch(ident) or clase not in CLASES_TARJETA \
            or not _es_entero(version) or version < 1 \
            or not isinstance(huella, str) or not HUELLA_RE.fullmatch(huella):
        raise _TarjetaInvalida()
    seccion = _SECCIONES_TARJETA[clase](datos.get(clase))
    permitidos = BOTONES_POR_CLASE[clase]
    if clase == "script" and not seccion["ejecutable"]:
        permitidos = permitidos - {"aprobar", "guardar"}
    botones: list[str] = []
    crudos = datos.get("botones")
    for boton in crudos if isinstance(crudos, (list, tuple)) else ():
        if isinstance(boton, str) and boton in BOTONES_TARJETA and boton in permitidos and boton not in botones:
            botones.append(boton)
    canal = datos.get("canal_origen")
    tarjeta: dict[str, Any] = {
        "id": ident,
        "clase": clase,
        "version": version,
        "sha256": huella,
        "titulo": _info(datos.get("titulo"), 120),
        "resumen": _info(datos.get("resumen"), 300),
        "aviso": _info(datos.get("aviso"), 300),
        "botones": botones,
        "caduca_ts": _real(datos.get("caduca_ts")),
        "canal_origen": canal if canal in CANALES_ORIGEN_TARJETA else "escritorio",
        "estado": "info" if clase == "salida" else "pendiente",
    }
    for nombre in CLASES_TARJETA:
        tarjeta[nombre] = seccion if nombre == clase else None
    return tarjeta


def _cabe_tarjeta(tarjeta: Mapping[str, Any]) -> bool:
    """``True`` si el mensaje WS ``{"tipo":"tarjeta","datos":…}`` ocupa ≤ ``MAX_BYTES_TARJETA`` bytes."""
    return bytes_json({"tipo": "tarjeta", "datos": tarjeta}) <= MAX_BYTES_TARJETA


def _tarjeta_que_cabe(tarjeta: dict[str, Any]) -> dict[str, Any] | None:
    """La tarjeta tal cual si cabe en 60 KiB; si es de ``script``, encogida (§12.2); si no, ``None``.

    Encogimiento determinista, midiendo tras cada paso y parando en cuanto cabe: (1) los 5 hallazgos
    más graves (el resto suma a ``hallazgos_omitidos``); (2) ``detalle`` ≤ 80; (3) los 3 primeros
    efectos y requisitos, cada uno ≤ 80; (4) ``explicacion`` ≤ 200; (5) título, resumen y aviso ≤ 120.
    Nunca toca ``codigo``, ``nivel``, ``ejecutable``, ``motivo_no_ejecutable``, ``metodo``, ``habilidad``
    ni ``habilidad_cambiada``. No modifica la original (trabaja sobre copias).
    """
    if _cabe_tarjeta(tarjeta):
        return tarjeta
    if tarjeta.get("clase") != "script" or not isinstance(tarjeta.get("script"), Mapping):
        return None
    tarjeta = dict(tarjeta)
    script = dict(tarjeta["script"])
    tarjeta["script"] = script

    def paso_1() -> None:
        hallazgos = list(script["hallazgos"])
        script["hallazgos_omitidos"] = script["hallazgos_omitidos"] + max(0, len(hallazgos) - 5)
        script["hallazgos"] = hallazgos[:5]

    def paso_2() -> None:
        script["hallazgos"] = [dict(h, detalle=_recortar_utf16(h["detalle"], 80, "…")) for h in script["hallazgos"]]

    def paso_3() -> None:
        script["efectos"] = [_recortar_utf16(e, 80, "…") for e in script["efectos"][:3]]
        script["requisitos"] = [_recortar_utf16(r, 80, "…") for r in script["requisitos"][:3]]

    def paso_4() -> None:
        script["explicacion"] = _recortar_utf16(script["explicacion"], 200, "…")

    def paso_5() -> None:
        for clave in ("titulo", "resumen", "aviso"):
            tarjeta[clave] = _recortar_utf16(tarjeta[clave], 120, "…")

    for paso in (paso_1, paso_2, paso_3, paso_4, paso_5):
        paso()
        if _cabe_tarjeta(tarjeta):
            return tarjeta
    return None


def _tarjeta_cerrada_saneada(datos: Mapping[str, Any], clase_guardada: str | None) -> dict[str, Any] | None:
    """``tarjeta.cerrada{id, clase, resultado, motivo}`` → datos del mensaje ``tarjeta`` cerrado (o ``None``)."""
    ident = datos.get("id")
    if not isinstance(ident, str) or not ID_TARJETA_RE.fullmatch(ident):
        return None
    clase = datos.get("clase")
    if clase not in CLASES_TARJETA:
        clase = clase_guardada
    resultado = datos.get("resultado")
    if clase not in CLASES_TARJETA or not isinstance(resultado, str) or resultado not in RESULTADOS_TARJETA:
        return None
    return {"id": ident, "clase": clase, "estado": "cerrada", "resultado": resultado,
            "motivo": _info(datos.get("motivo"), 200)}


def _decision_tarjeta_valida(mensaje: Mapping[str, Any]) -> dict[str, Any] | None:
    """``tarjeta_decision`` validada (§12.3) → payload de ``escritorio.tarjeta``; ``None`` si algo no cumple.

    ``version``: la versión que muestra el cliente (``int`` 1..``MAX_VERSION_TARJETA``); obligatoria
    salvo con ``descartar`` (que no ejecuta nada y la acepta ausente por compatibilidad).
    ``nombre`` solo (y siempre) con ``guardar``: 1..40 unidades UTF-16 tras ``strip``, sin controles.
    ``campos`` solo (y siempre) con ``editar``: claves ⊆ ``TOPES_CAMPOS_EDITAR``, valores str dentro de su
    tope y sin controles (en ``cuerpo`` se admiten ``\\n`` y ``\\t``). Un valor por encima de su tope
    **invalida** la decisión entera: es contenido aprobable y nunca se recorta. Aquí no se mira si la
    tarjeta está vigente (eso lo hacen :meth:`EnlaceEscritorio._en_tarjeta_decision` y main).
    """
    ident = mensaje.get("id")
    huella = mensaje.get("sha256")
    decision = mensaje.get("decision")
    if not isinstance(ident, str) or not ID_TARJETA_RE.fullmatch(ident) \
            or not isinstance(huella, str) or not HUELLA_RE.fullmatch(huella) \
            or not isinstance(decision, str) or decision not in DECISIONES_TARJETA:
        return None
    salida: dict[str, Any] = {"id": ident, "sha256": huella, "decision": decision}
    version = mensaje.get("version")
    if version is None:
        if decision != "descartar":
            return None
    elif not _es_entero(version) or not 1 <= version <= MAX_VERSION_TARJETA:
        return None
    else:
        salida["version"] = version
    nombre = mensaje.get("nombre")
    if decision == "guardar":
        if not isinstance(nombre, str):
            return None
        nombre = nombre.strip()
        if not 1 <= largo_utf16(nombre) <= MAX_NOMBRE_HABILIDAD or _CONTROLES_RE.search(nombre) \
                or _SUSTITUTOS_RE.search(nombre):
            return None
        salida["nombre"] = nombre
    elif nombre is not None:
        return None
    campos = mensaje.get("campos")
    if decision == "editar":
        if not isinstance(campos, Mapping):
            return None
        limpios: dict[str, str] = {}
        for clave, valor in campos.items():
            tope = TOPES_CAMPOS_EDITAR.get(clave) if isinstance(clave, str) else None
            if tope is None or not isinstance(valor, str) or largo_utf16(valor) > tope:
                return None
            controles = _CONTROLES_CUERPO_RE if clave == "cuerpo" else _CONTROLES_RE
            if controles.search(valor):
                return None
            limpios[clave] = valor
        salida["campos"] = limpios
    elif campos is not None:
        return None
    return salida


def _aviso_en_curso(clase: str, decision: str) -> str:
    """Aviso de una tarjeta «en curso» (sin contenido: solo qué se está haciendo)."""
    if decision == "editar":
        return "Guardando los cambios…"
    if decision == "guardar":
        return "Guardando la habilidad…"
    if clase == "script":
        return "Ejecutando el script: espera el resultado."
    if clase == "correo":
        return "Enviando el correo: espera el resultado."
    return "En curso: espera el resultado."


def _tarjeta_en_curso(tarjeta: Mapping[str, Any], decision: str) -> dict[str, Any]:
    """Copia de una tarjeta vigente mientras main ejecuta ``decision``: misma versión (los clientes la
    reemplazan), sin botones de decisión (solo «copiar», que es local) y con el aviso de qué se hace.

    Nunca toca el contenido. Si con el aviso nuevo no cupiera en 60 KiB (una de ``script`` ya encogida
    al límite), conserva el aviso original: quitar botones solo la achica.
    """
    botones = [b for b in tarjeta.get("botones", ()) if b == "copiar"]
    vista = dict(tarjeta, botones=botones, aviso=_aviso_en_curso(str(tarjeta.get("clase")), decision))
    return vista if _cabe_tarjeta(vista) else dict(tarjeta, botones=botones)


# ---------------------------------------------------------------------------
# Archivo del token (§C2.4.3): escritura atómica y borrado seguro
# ---------------------------------------------------------------------------


def _reemplazar(origen: str, destino: Path, intentos: int = 3) -> None:
    """``os.replace`` con reintentos breves: en Windows falla si otro proceso tiene el destino abierto
    en ese instante (p. ej. Amadeus Desktop leyendo el token). Bloqueante: solo dentro de un hilo."""
    for intento in range(intentos):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if intento == intentos - 1:
                raise
            time.sleep(0.05)


def escribir_archivo_token(ruta: Path, contenido: Mapping[str, Any]) -> None:
    """Escribe ``contenido`` en ``ruta`` de forma atómica y privada (0o600). Bloqueante: usar en un hilo.

    Crea un temporal en el **mismo** directorio (``mkstemp``: 0o600 en POSIX), lo vuelca, hace
    ``fsync`` y lo mueve con ``os.replace`` (atómico también en Windows). Si falla, borra el
    temporal y relanza ``OSError``.
    """
    ruta = Path(ruta)
    ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    datos = json.dumps(dict(contenido), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd, temporal = tempfile.mkstemp(prefix=".escritorio-", suffix=".tmp", dir=ruta.parent)
    try:
        with os.fdopen(fd, "wb") as archivo:
            archivo.write(datos)
            archivo.flush()
            os.fsync(archivo.fileno())
        os.chmod(temporal, 0o600)
        _reemplazar(temporal, ruta)
    except BaseException:
        try:
            os.unlink(temporal)
        except OSError as exc:
            log.debug("No se pudo borrar el temporal del token de escritorio: %s", type(exc).__name__)
        raise
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:  # p. ej. un sistema de archivos sin permisos POSIX
        log.debug("No se pudo fijar 0o600 en el token de escritorio: %s", type(exc).__name__)


def borrar_archivo_token_si_propio(ruta: Path, token: str, pid: int) -> bool:
    """Borra ``ruta`` solo si su contenido tiene **este** token y **este** pid. Bloqueante.

    Devuelve ``True`` si lo borró. Un archivo ajeno (otra instancia de R.A.M.S. lo reescribió),
    ilegible o demasiado grande se deja como está.
    """
    ruta = Path(ruta)
    try:
        if ruta.stat().st_size > 64 * 1024:
            return False
        crudo = ruta.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.debug("No se pudo leer el token de escritorio al cerrar: %s", type(exc).__name__)
        return False
    try:
        datos = json.loads(crudo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(datos, dict):
        return False
    guardado = datos.get("token")
    if not isinstance(guardado, str) or not _es_entero(datos.get("pid")) or datos.get("pid") != pid:
        return False
    if not hmac.compare_digest(guardado.encode("utf-8"), token.encode("utf-8")):
        return False
    try:
        ruta.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("No se pudo borrar el token de escritorio: %s", type(exc).__name__)
        return False
    return True


# ---------------------------------------------------------------------------
# Estado por conexión y límites con coalescencia
# ---------------------------------------------------------------------------


@dataclass
class _Ventana:
    """Ventana de 1 s de una clase de ritmo (§C2.4.5)."""

    inicio: float = -math.inf
    n: int = 0
    avisado: bool = False              # ya se registró un descarte en esta ventana


@dataclass
class _Cliente:
    """Estado de una conexión de ``/escritorio`` (solo se toca desde el hilo del loop)."""

    conexion: Any
    es_app: bool                       # True = Amadeus Desktop (sin Origin); False = chat del HUD
    ventanas: dict[str, _Ventana] = field(default_factory=dict)
    hola_app: bool = False
    cursor_encima_ts: float = -math.inf
    caja_publicada: tuple[int, int, int, int] | None = None
    caja_pendiente: tuple[int, int, int, int] | None = None
    caja_ts: float = -math.inf
    caja_temporizador: asyncio.TimerHandle | None = None
    # mostrada/ocultada que no cupo en su ventana: se publica al vencerla (gana la última).
    visibilidad_pendiente: tuple[str, dict[str, Any]] | None = None
    visibilidad_temporizador: asyncio.TimerHandle | None = None
    # puter.estado que no cupo en su ventana: se aplica al vencerla (gana el último; §C2.15). La app
    # solo lo reenvía si cambia, así que perderlo dejaría el respaldo con un estado viejo.
    puter_estado_pendiente: dict[str, Any] | None = None
    puter_estado_temporizador: asyncio.TimerHandle | None = None
    descartados: int = 0


@dataclass
class _Limite:
    """Límite de frecuencia con coalescencia: si llega otro dato dentro del intervalo, gana el último."""

    intervalo: float
    ultimo_envio: float = -math.inf
    pendiente: dict[str, Any] | None = None
    temporizador: asyncio.TimerHandle | None = None


@dataclass
class _Rechazos:
    """Registro de rechazos sin inundar el log (una línea cada 10 s)."""

    ultimo_log: float = -math.inf
    sin_log: int = 0


# ---------------------------------------------------------------------------
# Enlace
# ---------------------------------------------------------------------------


class EnlaceEscritorio:
    """Endpoint ``/escritorio`` del servidor del HUD (§C2.4). Lo compone :class:`ServidorHUD`.

    ``__init__`` es barato (sin sockets ni archivos) y se suscribe ya al bus para tener el permiso
    pendiente y el estado de las travesuras cuando conecte el primer cliente.
    """

    def __init__(self, settings: Settings, bus: BusEventos, *, evitar_token: str | None = None,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._bus = bus
        self._reloj = reloj
        token = secrets.token_urlsafe(16)
        while evitar_token is not None and hmac.compare_digest(token, evitar_token):
            token = secrets.token_urlsafe(16)  # distinto del token del HUD (§C2.4.3)
        self._token: str = token
        self._pid = os.getpid()

        ruta = getattr(settings, "escritorio_token_file", None)
        self._ruta_token: Path | None = Path(ruta) if ruta else None
        self._puerto: int | None = None
        self._activo = False
        self._archivo_escrito = False
        self._error_archivo: str | None = None

        # Estado observado del bus (el bus puede llamar desde otro hilo).
        self._lock = threading.Lock()
        self._permiso_pendiente: dict[str, Any] | None = None
        self._travesuras: dict[str, Any] | None = None
        self._estado: dict[str, Any] | None = None
        self._no_molestar: dict[str, Any] | None = None
        self._camara_por_fuente: dict[str, bool] = {}   # vision.camara.activa por fuente
        self._camara_enviada = False                      # último OR enviado a Electron
        # Tarjetas vigentes ya saneadas (v0.2.2, §12.4), de la más vieja a la más nueva (≤ 4).
        self._tarjetas: dict[str, dict[str, Any]] = {}
        # id → (decisión, reloj) de la decisión aceptada que main aún no ha respondido (tarjeta «en curso»).
        self._en_curso: dict[str, tuple[str, float]] = {}
        # id → reloj en que la tarjeta empezó a ofrecer «aprobar_publico» (paso 2 de compartir en público).
        self._publico_desde: dict[str, float] = {}

        # Conexiones (solo desde el hilo del loop).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hilo_loop: int | None = None
        self._app: _Cliente | None = None
        self._huds: dict[int, _Cliente] = {}
        self._limite_estado = _Limite(INTERVALO_ESTADO_S)
        self._tareas: set[asyncio.Task[Any]] = set()
        self._rechazos = _Rechazos()
        self.descartados = 0
        # Respaldo Puter del Área de Broca (§C2.15): lo vincula RespaldoPuter al construirse.
        self._puter: RespaldoPuter | None = None

        self._desuscripciones: list[Callable[[], None]] = []
        self._suscribir()

    # ------------------------------------------------------------------
    # Propiedades públicas (nunca exponen el token)
    # ------------------------------------------------------------------

    @property
    def activo(self) -> bool:
        """``True`` mientras el servidor escucha y ``/escritorio`` acepta clientes."""
        return self._activo

    @property
    def url(self) -> str | None:
        """``ws://127.0.0.1:<puerto>/escritorio`` **sin** token (``None`` si no está activo)."""
        if not self._activo or self._puerto is None:
            return None
        return f"ws://{HOST}:{self._puerto}{RUTA_ESCRITORIO}"

    @property
    def overlay_conectado(self) -> bool:
        """``True`` si Amadeus Desktop (cliente sin Origin) está conectado."""
        return self._app is not None

    @property
    def n_hud(self) -> int:
        """Número de paneles de chat del HUD conectados."""
        return len(self._huds)

    @property
    def n_clientes(self) -> int:
        """Clientes de ``/escritorio`` conectados (Electron + HUD)."""
        return (1 if self._app is not None else 0) + len(self._huds)

    def __repr__(self) -> str:  # sin token
        return (f"EnlaceEscritorio(puerto={self._puerto!r}, activo={self._activo}, "
                f"overlay={self.overlay_conectado}, hud={self.n_hud})")

    @property
    def puter(self) -> RespaldoPuter | None:
        """Respaldo Puter vinculado a este enlace (``None`` si no hay)."""
        return self._puter

    def vincular_puter(self, respaldo: RespaldoPuter | None) -> None:
        """Vincula (o suelta, con ``None``) el respaldo Puter que recibe ``puter.estado``/``llm.respuesta``.

        Si había otro distinto, sus pedidos pendientes devuelven ``None``.
        """
        anterior = self._puter
        self._puter = respaldo
        if anterior is not None and anterior is not respaldo:
            anterior._al_desconectar()

    def datos_para_hud(self) -> dict[str, str] | None:
        """Mensaje ``escritorio`` de ``/ws`` (§C2.4.6): ``{"token", "url": "/escritorio"}`` o ``None``.

        Solo si el enlace está activo, el HUD está habilitado y ``hud_chat`` lo permite. El llamador
        lo envía únicamente por ``/ws`` (token del HUD + Origin local ya validados).
        """
        if not self._activo or not self._chat_hud_permitido():
            return None
        return {"token": self._token, "url": RUTA_ESCRITORIO}

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    async def activar(self, puerto: int | None) -> None:
        """Marca el enlace como activo en ``puerto`` y escribe el archivo del token (en un hilo)."""
        if puerto is None:
            log.warning("Enlace de escritorio sin puerto: no se activa")
            return
        if not self._desuscripciones:  # reinicio tras cerrar()
            self._suscribir()
        self._loop = asyncio.get_running_loop()
        self._hilo_loop = threading.get_ident()
        self._puerto = int(puerto)
        self._activo = True
        await self._escribir_token()

    async def cerrar(self) -> None:
        """Suelta el bus, cierra los clientes y borra el token si sigue siendo propio. Idempotente."""
        for desuscribir in self._desuscripciones:
            try:
                desuscribir()
            except Exception as exc:
                log.debug("Error al desuscribir el enlace de escritorio del bus: %s", exc)
        self._desuscripciones = []
        self._activo = False
        limite = self._limite_estado
        if limite.temporizador is not None:
            limite.temporizador.cancel()
        limite.temporizador = None
        limite.pendiente = None

        clientes = ([self._app] if self._app is not None else []) + list(self._huds.values())
        self._app = None
        self._huds.clear()
        self._avisar_puter(conectada=False)  # los pedidos pendientes devuelven None
        for cliente in clientes:
            self._cancelar_temporizadores(cliente)
            try:
                await asyncio.wait_for(cliente.conexion.close(1001, "cierre"), timeout=2.0)
            except (TimeoutError, _WsConnectionClosed):
                pass
            except Exception as exc:
                log.debug("Error al cerrar un cliente de /escritorio: %s", type(exc).__name__)
        tareas = [t for t in self._tareas if not t.done()]
        if tareas:
            _hechas, pendientes = await asyncio.wait(tareas, timeout=2.5)
            for tarea in pendientes:
                tarea.cancel()
        self._tareas.clear()

        if self._archivo_escrito and self._ruta_token is not None:
            try:
                borrado = await asyncio.to_thread(borrar_archivo_token_si_propio, self._ruta_token,
                                                  self._token, self._pid)
            except Exception as exc:
                log.debug("Error al borrar el token de escritorio: %s", type(exc).__name__)
                borrado = False
            if borrado:
                log.debug("Token de escritorio borrado")
            else:
                log.info("El archivo del token de escritorio ya no es de esta instancia: no se borra")
            self._archivo_escrito = False

    def diagnostico(self) -> DiagnosticoModulo:
        """``OK`` con la URL sin token, ``DEGRADADO`` si no se pudo escribir el token, ``EN ESPERA`` si no escucha."""
        url = self.url
        if url is None:
            return DiagnosticoModulo("escritorio", "Amadeus Desktop (/escritorio)", EstadoModulo.EN_ESPERA,
                                     "sin iniciar")
        if self._error_archivo is not None:
            return DiagnosticoModulo("escritorio", "Amadeus Desktop (/escritorio)", EstadoModulo.DEGRADADO,
                                     f"{self._error_archivo} · {url}")
        return DiagnosticoModulo("escritorio", "Amadeus Desktop (/escritorio)", EstadoModulo.OK, url)

    async def _escribir_token(self) -> None:
        """Escribe ``{url, token, pid}`` en ``escritorio_token_file`` (atómico y 0o600)."""
        self._error_archivo = None
        if self._ruta_token is None:
            self._error_archivo = "sin ruta para el token"
            log.warning("Sin RAMS_ESCRITORIO_TOKEN_FILE: Amadeus Desktop no podrá conectarse")
            return
        contenido = {"url": self.url, "token": self._token, "pid": self._pid}
        try:
            await asyncio.to_thread(escribir_archivo_token, self._ruta_token, contenido)
        except OSError as exc:
            self._error_archivo = "no se pudo escribir el token"
            log.warning("No se pudo escribir el token de escritorio en %s (%s)", self._ruta_token.name,
                        exc.strerror or type(exc).__name__)
            return
        self._archivo_escrito = True
        log.info("Token de /escritorio listo en %s", self._ruta_token.name)

    # ------------------------------------------------------------------
    # Handshake (process_request del servidor del HUD)
    # ------------------------------------------------------------------

    def validar_handshake(self, conexion: Any, solicitud: Any, consulta: str) -> Any:
        """Valida token, ``Origin`` y límites. ``None`` sigue el handshake; si no, responde 403/404."""
        if not self._activo:
            return conexion.respond(HTTPStatus.NOT_FOUND, "No encontrado\n")
        valores = parse_qs(consulta, keep_blank_values=True).get("t") or [""]
        token = valores[0]
        if not hmac.compare_digest(token.encode("utf-8"), self._token.encode("utf-8")):
            self._registrar_rechazo("token inválido")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        try:
            origenes = list(solicitud.headers.get_all("Origin"))
        except Exception as exc:
            log.debug("Cabeceras Origin ilegibles en /escritorio: %s", type(exc).__name__)
            origenes = ["?", "?"]
        if len(origenes) > 1:
            self._registrar_rechazo("varias cabeceras Origin")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        if not origenes:
            return None  # Amadeus Desktop: uno nuevo reemplaza al anterior en manejar()
        origen = origenes[0]
        if not self._chat_hud_permitido() or origen not in self._origenes_hud():
            log.debug("Origin rechazado en /escritorio: %r", _texto(origen, 80))
            self._registrar_rechazo("Origin no permitido")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        if len(self._huds) >= MAX_CLIENTES_HUD:
            self._registrar_rechazo("demasiados paneles del HUD")
            return conexion.respond(HTTPStatus.FORBIDDEN, "Prohibido\n")
        return None

    def _chat_hud_permitido(self) -> bool:
        """El panel de chat del HUD solo existe con el HUD habilitado y ``hud_chat``."""
        return bool(getattr(self._settings, "hud_habilitado", False)) and bool(
            getattr(self._settings, "hud_chat", True))

    def _origenes_hud(self) -> frozenset[str]:
        """``{http://127.0.0.1:<p>, http://localhost:<p>}`` con el puerto real."""
        if self._puerto is None:
            return frozenset()
        return frozenset({f"http://127.0.0.1:{self._puerto}", f"http://localhost:{self._puerto}"})

    def _registrar_rechazo(self, motivo: str) -> None:
        """Registra conexiones rechazadas sin inundar el log (como mucho una línea cada 10 s)."""
        ahora = time.monotonic()
        r = self._rechazos
        if ahora - r.ultimo_log >= 10.0:
            extra = f" (+{r.sin_log} más)" if r.sin_log else ""
            log.info("Conexión a /escritorio rechazada: %s%s", motivo, extra)
            r.ultimo_log = ahora
            r.sin_log = 0
        else:
            r.sin_log += 1

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------

    @staticmethod
    def _origen_de(conexion: Any) -> str | None:
        """``Origin`` de la petición de la conexión (``None`` si no lo trae)."""
        try:
            origenes = conexion.request.headers.get_all("Origin")
        except Exception:
            return "?"  # no se puede leer: se trata como un Origin cualquiera (nunca como Electron)
        return origenes[0] if origenes else None

    async def manejar(self, conexion: Any) -> None:
        """Ciclo de vida de un cliente de ``/escritorio``: alta, mensajes iniciales, entrantes y baja."""
        if not self._activo or _ws_broadcast is None:
            return
        es_app = self._origen_de(conexion) is None
        cliente = _Cliente(conexion, es_app)
        if not es_app and len(self._huds) >= MAX_CLIENTES_HUD:  # carrera entre dos handshakes
            await self._cerrar_cliente(conexion, CODIGO_DEMASIADOS, "demasiados clientes")
            return
        try:
            for texto in self._mensajes_iniciales(es_app):  # síncrono y en orden: hola primero
                _ws_broadcast([conexion], texto, raise_exceptions=True)
        except Exception as exc:
            log.debug("No se pudo enviar el saludo a un cliente de /escritorio: %s", type(exc).__name__)
            return

        if es_app:
            anterior = self._app
            self._app = cliente
            if anterior is not None:
                log.info("Nueva conexión de Amadeus Desktop: se reemplaza la anterior")
                self._cancelar_temporizadores(anterior)
                self._crear_tarea(self._cerrar_cliente(anterior.conexion, CODIGO_REEMPLAZADO, "reemplazado"))
            else:
                log.info("Amadeus Desktop conectado a /escritorio")
            # Puter: la app nueva no conoce los pedidos de la anterior; espera su puter.estado.
            self._avisar_puter(conectada=True)
        else:
            self._huds[id(conexion)] = cliente
            log.debug("Chat del HUD conectado a /escritorio (%d)", len(self._huds))
        self._publicar_conectado()

        try:
            async for crudo in conexion:
                self._procesar(cliente, crudo)  # valida, aplica el ritmo de su clase y publica
        except _WsConnectionClosed:
            log.debug("Cliente de /escritorio desconectado")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Error con un cliente de /escritorio: %s", type(exc).__name__)
        finally:
            self._cancelar_temporizadores(cliente)
            if es_app:
                if self._app is cliente:
                    self._app = None
                    log.info("Amadeus Desktop desconectado de /escritorio")
                    self._avisar_puter(conectada=False)
            else:
                self._huds.pop(id(conexion), None)
            if cliente.descartados:
                log.debug("Cliente de /escritorio: %d mensajes descartados por ritmo", cliente.descartados)
            if self._activo:
                self._publicar_conectado()

    async def _cerrar_cliente(self, conexion: Any, codigo: int, motivo: str) -> None:
        """Cierra una conexión con ``codigo`` (sin propagar errores)."""
        try:
            await conexion.close(codigo, motivo)
        except _WsConnectionClosed:
            pass
        except Exception as exc:
            log.debug("Error al cerrar un cliente de /escritorio: %s", type(exc).__name__)

    def _crear_tarea(self, corrutina: Any) -> None:
        """Lanza una tarea guardando la referencia (que no la recolecte el GC)."""
        try:
            tarea = asyncio.get_running_loop().create_task(corrutina, name="rams-escritorio")
        except RuntimeError:
            corrutina.close()
            return
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)

    def _avisar_puter(self, *, conectada: bool) -> None:
        """Avisa al respaldo Puter de que la app se conectó (o se reemplazó) o se fue (nunca lanza)."""
        puter = self._puter
        if puter is None:
            return
        try:
            if conectada:
                puter._al_cambiar_app()
            else:
                puter._al_desconectar()
        except Exception as exc:
            log.debug("Error al avisar al respaldo Puter: %s", type(exc).__name__)

    def _publicar_conectado(self) -> None:
        """``escritorio.conectado{clientes, overlay, hud}`` con el estado actual."""
        self._bus.publicar("escritorio.conectado", {
            "clientes": self.n_clientes,
            "overlay": self._app is not None,
            "hud": len(self._huds),
        }, origen="escritorio")

    def _mensajes_iniciales(self, es_app: bool) -> list[str]:
        """``hola`` y la instantánea: permiso pendiente (con vista previa), tarjetas vigentes (de la más
        vieja a la más nueva), travesuras, ``no_molestar`` (siempre) y, para Electron, ``vision`` y el
        estado."""
        mensajes = [_serializar("hola", {
            "version": VERSION,
            "nombre_ia": _texto(getattr(self._settings, "nombre_ia", "Amadeus"), 40),
            "usuario": _texto(getattr(self._settings, "trato_usuario", ""), 60),
        })]
        with self._lock:
            permiso = dict(self._permiso_pendiente) if self._permiso_pendiente else None
            travesuras = dict(self._travesuras) if self._travesuras else None
            estado = dict(self._estado) if self._estado else None
            no_molestar = dict(self._no_molestar) if self._no_molestar else None
            # Ya saneadas y ≤ 60 KiB cada una (nunca se mutan); una «en curso» va sin botones de decisión.
            ahora = self._reloj()
            tarjetas = [self._vista_tarjeta(ident, t, ahora) for ident, t in self._tarjetas.items()]
            camara = any(self._camara_por_fuente.values())
            sin_camara_vista = not self._camara_por_fuente
        if sin_camara_vista:
            ultimo = self._bus.ultimo("vision.camara")   # publicado antes de crear el enlace
            camara = bool(ultimo is not None and isinstance(ultimo.datos, Mapping)
                          and ultimo.datos.get("activa") is True)
        if travesuras is None:
            ultimo = self._bus.ultimo("motor.travesuras")
            if ultimo is not None and isinstance(ultimo.datos, Mapping):
                travesuras = self._travesuras_saneadas(ultimo.datos)
        if no_molestar is None:
            # main solo publica pc.no_molestar al cambiar (el "sin no molestar" inicial no se
            # publica): sin ningún evento previo, el estado real es todo false.
            ultimo = self._bus.ultimo("pc.no_molestar")
            no_molestar = (self._no_molestar_saneado(ultimo.datos)
                           if ultimo is not None and isinstance(ultimo.datos, Mapping)
                           else dict(NO_MOLESTAR_INICIAL))
        if permiso is not None:
            mensajes.append(_serializar("permiso", permiso))
        for tarjeta in tarjetas:
            mensajes.append(_serializar("tarjeta", tarjeta))
        if travesuras is not None:
            mensajes.append(_serializar("travesuras", travesuras))
        mensajes.append(_serializar("no_molestar", no_molestar))
        if es_app:
            # Ojo del overlay (§C2.13): siempre, para que Electron no conserve un "abierta" viejo.
            mensajes.append(_serializar("vision", {"camara": camara}))
        if es_app and estado is not None:
            mensajes.append(_serializar("estado", estado))
        return mensajes

    # ------------------------------------------------------------------
    # Entrantes
    # ------------------------------------------------------------------

    def _admitir(self, cliente: _Cliente, clase: str) -> bool:
        """Presupuesto por cliente y por clase en ventanas de 1 s (§C2.4.5); el exceso se descarta."""
        ventana = cliente.ventanas.get(clase)
        if ventana is None:
            ventana = cliente.ventanas[clase] = _Ventana()
        ahora = self._reloj()
        if ahora - ventana.inicio >= 1.0:
            ventana.inicio = ahora
            ventana.n = 0
            ventana.avisado = False
        ventana.n += 1
        if ventana.n > _PRESUPUESTOS[clase]:
            cliente.descartados += 1
            self.descartados += 1
            return False
        return True

    @staticmethod
    def _clase(tipo: str, mensaje: Mapping[str, Any]) -> str:
        """Clase de ritmo de un mensaje con ``tipo`` conocido (§C2.4.5)."""
        if tipo in _TIPOS_ESTADO:
            return _ESTADO
        if tipo == "llm.respuesta":
            return _LLM
        if tipo == "tarjeta_decision":
            return _TARJETA
        if tipo == "caja":
            return _RUIDO
        if tipo == "evento":
            nombre = mensaje.get("nombre")
            if isinstance(nombre, str) and nombre in EVENTOS_ESCRITORIO and nombre not in _EVENTOS_RUIDO:
                return _ESTADO  # mostrada, ocultada, chat_abierto
            return _RUIDO
        return _GENERAL     # chat

    @staticmethod
    def _excede(crudo: str, maximo: int) -> bool:
        """``True`` si ``crudo`` ocupa más de ``maximo`` bytes en UTF-8."""
        return len(crudo) > maximo or len(crudo.encode("utf-8")) > maximo

    @staticmethod
    def _decodificar(crudo: Any, maximo: int = MAX_BYTES_MENSAJE) -> tuple[dict[str, Any] | None, str]:
        """``(mensaje, "")`` si ``crudo`` es un objeto JSON de ≤ ``maximo`` bytes; si no, ``(None, motivo)``."""
        if not isinstance(crudo, str):
            return None, "mensaje binario"
        if EnlaceEscritorio._excede(crudo, maximo):
            return None, f"mensaje de más de {maximo // 1024} KiB"
        try:
            mensaje = json.loads(crudo)
        except (ValueError, RecursionError):
            return None, "JSON inválido"
        if not isinstance(mensaje, dict):
            return None, "mensaje que no es un objeto"
        return mensaje, ""

    @staticmethod
    def _tope_bytes(cliente: _Cliente, tipo: Any) -> int:
        """Tope en bytes de un entrante según su tipo (§12.3): ``llm.respuesta`` ≤ 64 KiB (solo la app),
        ``tarjeta_decision`` ≤ 60 KiB (la app y el HUD) y todo lo demás ≤ 8 KiB, como siempre."""
        tope = _MAX_BYTES_POR_TIPO.get(tipo, MAX_BYTES_MENSAJE) if isinstance(tipo, str) else MAX_BYTES_MENSAJE
        if tipo == "llm.respuesta" and not cliente.es_app:
            tope = MAX_BYTES_MENSAJE
        return tope

    def _procesar(self, cliente: _Cliente, crudo: Any) -> None:
        """Valida un mensaje entrante, aplica el ritmo de su clase y publica lo que toque (nunca lanza)."""
        # Se decodifica con 64 KiB (el max_size del servidor) para todos los clientes y, con el tipo en
        # la mano, se aplica su tope: solo llm.respuesta (de la app) y tarjeta_decision pasan de 8 KiB.
        mensaje, motivo = self._decodificar(crudo, MAX_BYTES_RESPUESTA_LLM)
        if mensaje is not None:
            tope = self._tope_bytes(cliente, mensaje.get("tipo"))
            if self._excede(crudo, tope):
                mensaje, motivo = None, f"mensaje de más de {tope // 1024} KiB"
        if mensaje is None:
            if self._admitir(cliente, _GENERAL):  # la basura gasta el cupo general (y no inunda el log)
                log.debug("/escritorio: %s ignorado", motivo)
            return
        tipo = mensaje.get("tipo")
        manejador = self._ENTRANTES.get(tipo) if isinstance(tipo, str) else None
        if manejador is None:
            if self._admitir(cliente, _GENERAL):
                nombre = tipo if isinstance(tipo, str) and _TIPO_SEGURO_RE.fullmatch(tipo) else "<no válido>"
                log.debug("/escritorio: tipo desconocido ignorado (%s)", nombre)
            return
        try:
            clase = self._clase(tipo, mensaje)
            if clase == _LLM and self._responde_pedido_pendiente(cliente, mensaje):
                pass  # nunca se descarta: hay como mucho MAX_PEDIDOS_PUTER esperando su respuesta
            elif not self._admitir(cliente, clase):
                if clase == _ESTADO:
                    self._descartar_estado(cliente, tipo, mensaje)
                elif clase == _TARJETA:
                    ventana = cliente.ventanas[_TARJETA]
                    if not ventana.avisado:   # una línea por ventana (nunca el contenido)
                        ventana.avisado = True
                        log.info("/escritorio: demasiadas decisiones de tarjeta; se descarta el exceso")
                return
            manejador(self, cliente, mensaje)
        except Exception as exc:  # un mensaje raro nunca tumba la conexión
            log.debug("/escritorio: error al procesar «%s»: %s", tipo, type(exc).__name__)

    def _responde_pedido_pendiente(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> bool:
        """``True`` si es la ``llm.respuesta`` de la app activa a un ``llm.pedir`` aún sin respuesta.

        Esas no gastan el cupo de ``llm``: perder una dejaría a Broca esperando hasta 40 s o un turno
        ocupado hasta 46 s (también si llega tarde: el pedido sigue en curso en la app). Las que no
        responden a nada (ajenas, repetidas, inventadas) sí lo gastan.
        """
        puter = self._puter
        if puter is None or not cliente.es_app or self._app is not cliente:
            return False
        ident = _sobre(mensaje).get("id")
        return isinstance(ident, str) and puter._espera_respuesta(ident)

    def _descartar_estado(self, cliente: _Cliente, tipo: str, mensaje: Mapping[str, Any]) -> None:
        """Un mensaje de estado no cupo en su ventana: ``mostrada``/``ocultada`` se pospone (gana la
        última); el resto se descarta con ``log.info`` (una línea por ventana)."""
        ventana = cliente.ventanas[_ESTADO]
        if tipo == "puter.estado":
            if self._diferir_puter_estado(cliente, mensaje) and not ventana.avisado:
                ventana.avisado = True
                log.info("/escritorio: demasiados mensajes de estado; «puter.estado» se aplica al vencer la ventana")
            return
        nombre = mensaje.get("nombre") if tipo == "evento" else None
        if isinstance(nombre, str) and nombre in _EVENTOS_VISIBILIDAD:
            if self._diferir_visibilidad(cliente, nombre, mensaje):
                if not ventana.avisado:
                    ventana.avisado = True
                    log.info("/escritorio: demasiados mensajes de estado; «%s» se publica al vencer la ventana",
                             nombre)
            return
        if not ventana.avisado:
            ventana.avisado = True
            log.info("/escritorio: demasiados mensajes de estado; se descarta «%s»", tipo)

    def _diferir_visibilidad(self, cliente: _Cliente, nombre: str, mensaje: Mapping[str, Any]) -> bool:
        """Guarda ``mostrada``/``ocultada`` para publicarla al vencer la ventana (``True`` si se guardó)."""
        if not cliente.es_app:
            return False
        datos = _datos_evento_validos(mensaje.get("datos"))
        loop = self._loop
        if datos is None or loop is None:
            return False
        cliente.visibilidad_pendiente = (nombre, datos)
        if cliente.visibilidad_temporizador is None:
            ventana = cliente.ventanas[_ESTADO]
            espera = min(1.0, max(0.0, 1.0 - (self._reloj() - ventana.inicio)))
            cliente.visibilidad_temporizador = loop.call_later(espera, self._vaciar_visibilidad, cliente)
        return True

    def _vaciar_visibilidad(self, cliente: _Cliente) -> None:
        """Publica la visibilidad pospuesta más reciente (si sigue siendo el cliente activo)."""
        cliente.visibilidad_temporizador = None
        pendiente = cliente.visibilidad_pendiente
        cliente.visibilidad_pendiente = None
        if pendiente is None or not self._activo or self._app is not cliente:
            return
        nombre, datos = pendiente
        self._bus.publicar("escritorio.evento", {"nombre": nombre, "datos": datos}, origen="escritorio")

    def _en_hola_app(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``hola_app{pid, version}`` (solo Electron, una vez por conexión) → ``escritorio.app{pid}``."""
        if not cliente.es_app:
            log.debug("/escritorio: hola_app desde el HUD ignorado")
            return
        if cliente.hola_app:
            return
        pid = mensaje.get("pid")
        if not _es_entero(pid) or not 0 < pid <= MAX_PID:
            log.debug("/escritorio: hola_app con pid inválido ignorado")
            return
        cliente.hola_app = True
        version = mensaje.get("version")
        texto_version = version if isinstance(version, str) and _VERSION_SEGURA_RE.fullmatch(version) else "?"
        log.info("Amadeus Desktop identificado (versión %s, pid %d)", texto_version, pid)
        self._bus.publicar("escritorio.app", {"pid": pid}, origen="escritorio")

    def _en_chat(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``chat{texto}`` → ``escritorio.chat{texto}`` (1..2000). Nunca ``usuario.entrada``; nunca se registra."""
        texto = mensaje.get("texto")
        if not isinstance(texto, str) or len(texto) > MAX_TEXTO_CHAT:
            log.debug("/escritorio: chat con texto inválido ignorado")
            return
        texto = texto.replace("\x00", "").strip()
        if not texto:
            return
        self._bus.publicar("escritorio.chat", {"texto": texto}, origen="escritorio")

    def _en_evento(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``evento{nombre, datos}`` (solo Electron) → ``escritorio.evento`` (escalares ≤ 1 KiB)."""
        if not cliente.es_app:
            log.debug("/escritorio: evento desde el HUD ignorado")
            return
        nombre = mensaje.get("nombre")
        if not isinstance(nombre, str) or nombre not in EVENTOS_ESCRITORIO:
            log.debug("/escritorio: evento desconocido ignorado")
            return
        datos = _datos_evento_validos(mensaje.get("datos"))
        if datos is None:
            log.debug("/escritorio: evento %s con datos inválidos ignorado", nombre)
            return
        if nombre == "cursor_encima":
            ahora = self._reloj()
            if ahora - cliente.cursor_encima_ts < INTERVALO_CURSOR_ENCIMA_S:
                return
            cliente.cursor_encima_ts = ahora
        elif nombre == "soltada":
            datos["altura"] = _acotar(datos.get("altura"), 0.0, 1.0, 0.0)
        elif nombre in _EVENTOS_VISIBILIDAD:
            self._cancelar_visibilidad(cliente)  # una visibilidad más nueva gana a la pospuesta
        self._bus.publicar("escritorio.evento", {"nombre": nombre, "datos": datos}, origen="escritorio")

    def _en_control(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``control{accion}``: ``CONTROLES_ESCRITORIO`` → ``escritorio.control``; ``silenciar`` → ``hud.control``."""
        accion = mensaje.get("accion")
        if not isinstance(accion, str):
            return
        if accion in CONTROLES_ESCRITORIO:
            log.info("Control desde el escritorio: %s", accion)
            self._bus.publicar("escritorio.control", {"accion": accion}, origen="escritorio")
        elif accion == "silenciar":
            log.info("Control desde el escritorio: silenciar")
            self._bus.publicar("hud.control", {"accion": "silenciar"}, origen="escritorio")
        else:
            log.debug("/escritorio: control no permitido ignorado")

    def _en_confirmacion(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``confirmacion{id, aprobado}`` (id ``^[0-9a-f]{8}$``, booleano) → ``escritorio.confirmacion``."""
        ident = mensaje.get("id")
        aprobado = mensaje.get("aprobado")
        if not isinstance(ident, str) or not ID_CONFIRMACION_RE.fullmatch(ident) or not isinstance(aprobado, bool):
            log.debug("/escritorio: confirmación inválida ignorada")
            return
        log.info("Confirmación desde el escritorio: %s", "aprobada" if aprobado else "rechazada")
        self._bus.publicar("escritorio.confirmacion", {"id": ident, "aprobado": aprobado}, origen="escritorio")

    def _en_caja(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``caja{x,y,w,h}`` (solo Electron, enteros) → ``escritorio.caja`` a ≤ 2 Hz y solo si cambia."""
        if not cliente.es_app:
            log.debug("/escritorio: caja desde el HUD ignorada")
            return
        valores = [mensaje.get(k) for k in ("x", "y", "w", "h")]
        if not all(_es_entero(v) for v in valores):
            log.debug("/escritorio: caja con valores no enteros ignorada")
            return
        x, y, w, h = valores
        if not (0 < w <= MAX_LADO_CAJA and 0 < h <= MAX_LADO_CAJA
                and abs(x) <= MAX_COORD_CAJA and abs(y) <= MAX_COORD_CAJA):
            log.debug("/escritorio: caja fuera de rango ignorada")
            return
        caja = (x, y, w, h)
        if cliente.caja_temporizador is not None:  # ya hay una publicación diferida: gana la última
            cliente.caja_pendiente = caja
            return
        if caja == cliente.caja_publicada:
            return
        ahora = self._reloj()
        espera = INTERVALO_CAJA_S - (ahora - cliente.caja_ts)
        if espera <= 0:
            self._publicar_caja(cliente, caja)
            return
        loop = self._loop
        if loop is None:
            return
        cliente.caja_pendiente = caja
        cliente.caja_temporizador = loop.call_later(espera, self._vaciar_caja, cliente)

    def _vaciar_caja(self, cliente: _Cliente) -> None:
        """Publica la caja diferida más reciente (si sigue siendo el cliente activo y cambió)."""
        cliente.caja_temporizador = None
        caja = cliente.caja_pendiente
        cliente.caja_pendiente = None
        if caja is None or not self._activo or self._app is not cliente or caja == cliente.caja_publicada:
            return
        self._publicar_caja(cliente, caja)

    def _publicar_caja(self, cliente: _Cliente, caja: tuple[int, int, int, int]) -> None:
        cliente.caja_publicada = caja
        cliente.caja_ts = self._reloj()
        x, y, w, h = caja
        self._bus.publicar("escritorio.caja", {"x": x, "y": y, "w": w, "h": h}, origen="escritorio")

    @staticmethod
    def _cancelar_caja(cliente: _Cliente) -> None:
        if cliente.caja_temporizador is not None:
            cliente.caja_temporizador.cancel()
            cliente.caja_temporizador = None
        cliente.caja_pendiente = None

    @staticmethod
    def _cancelar_visibilidad(cliente: _Cliente) -> None:
        if cliente.visibilidad_temporizador is not None:
            cliente.visibilidad_temporizador.cancel()
            cliente.visibilidad_temporizador = None
        cliente.visibilidad_pendiente = None

    @staticmethod
    def _cancelar_puter_estado(cliente: _Cliente) -> None:
        if cliente.puter_estado_temporizador is not None:
            cliente.puter_estado_temporizador.cancel()
            cliente.puter_estado_temporizador = None
        cliente.puter_estado_pendiente = None

    def _cancelar_temporizadores(self, cliente: _Cliente) -> None:
        """Cancela las publicaciones diferidas (caja, visibilidad y ``puter.estado``) de un cliente que se va."""
        self._cancelar_caja(cliente)
        self._cancelar_visibilidad(cliente)
        self._cancelar_puter_estado(cliente)

    def _diferir_puter_estado(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> bool:
        """Guarda un ``puter.estado`` válido de la app activa para aplicarlo al vencer la ventana."""
        if not cliente.es_app or self._app is not cliente or self._puter is None or self._loop is None:
            return False
        estado = _estado_puter_saneado(_sobre(mensaje))
        if estado is None:
            return False
        cliente.puter_estado_pendiente = estado
        if cliente.puter_estado_temporizador is None:
            ventana = cliente.ventanas[_ESTADO]
            espera = min(1.0, max(0.0, 1.0 - (self._reloj() - ventana.inicio)))
            cliente.puter_estado_temporizador = self._loop.call_later(espera, self._vaciar_puter_estado, cliente)
        return True

    def _vaciar_puter_estado(self, cliente: _Cliente) -> None:
        """Aplica el ``puter.estado`` pospuesto más reciente (si sigue siendo la app activa)."""
        cliente.puter_estado_temporizador = None
        estado = cliente.puter_estado_pendiente
        cliente.puter_estado_pendiente = None
        puter = self._puter
        if estado is None or puter is None or not self._activo or self._app is not cliente:
            return
        puter._al_estado(estado)

    def _en_puter_estado(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``puter.estado{listo, sesion, modelo, error}`` (solo la app activa) → estado del respaldo Puter."""
        if not cliente.es_app or self._app is not cliente:
            log.debug("/escritorio: puter.estado fuera de la app activa ignorado")
            return
        puter = self._puter
        if puter is None:
            return  # RAMS_PUTER=0 u offline: el estado no se usa
        estado = _estado_puter_saneado(_sobre(mensaje))
        if estado is None:
            log.debug("/escritorio: puter.estado inválido ignorado")
            return
        self._cancelar_puter_estado(cliente)   # este es más nuevo que uno pospuesto
        puter._al_estado(estado)

    def _en_llm_respuesta(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``llm.respuesta`` (solo la app activa) → resuelve el pedido pendiente con ese ``id``."""
        if not cliente.es_app or self._app is not cliente:
            log.debug("/escritorio: llm.respuesta fuera de la app activa ignorada")
            return
        puter = self._puter
        if puter is None:
            log.debug("/escritorio: llm.respuesta sin respaldo Puter ignorada")
            return
        respuesta = _respuesta_llm_saneada(_sobre(mensaje))
        if respuesta is None:
            log.debug("/escritorio: llm.respuesta con id inválido ignorada")
            return
        puter._al_respuesta(respuesta)

    def _en_tarjeta_decision(self, cliente: _Cliente, mensaje: Mapping[str, Any]) -> None:
        """``tarjeta_decision{id, sha256, decision, version, nombre?, campos?}`` (app o HUD, §12.3) →
        ``escritorio.tarjeta``.

        Con la tarjeta vigente aquí, una decisión que no es ``descartar`` tiene que ser sobre lo que el
        cliente ve ahora (versión y huella vigentes), con un botón que la tarjeta ofrece y sin otra en
        curso; si no, se ignora y se le reenvía la tarjeta a ese cliente (que deja de esperar). La
        aceptada deja la tarjeta «en curso» y esa vista se difunde **antes** de publicar: ni el otro
        cliente ni uno que reconecte verán un botón activo de una tarjeta que main está ejecutando, y
        un clic rancio no puede valer para la versión que main reabra con la misma huella.

        Solo se registra la decisión, nunca el nombre ni los campos (contenido privado: van sin
        redactar a main, su único consumidor).
        """
        decision = _decision_tarjeta_valida(mensaje)
        if decision is None:
            log.debug("/escritorio: decisión de tarjeta inválida ignorada")
            return
        ident = decision["id"]
        tipo = decision["decision"]
        ahora = self._reloj()
        motivo: str | None = None
        respuesta: dict[str, Any] | None = None
        en_curso: dict[str, Any] | None = None
        with self._lock:
            guardada = self._tarjetas.get(ident)
            if guardada is not None and tipo != "descartar":
                motivo = self._motivo_para_ignorar(ident, guardada, decision, ahora)
                if motivo is not None:
                    respuesta = self._vista_tarjeta(ident, guardada, ahora)
                elif tipo in _DECISIONES_EN_CURSO and (tipo != "editar" or decision.get("campos")):
                    # (un «editar» sin campos no cambia nada y main no responde: no se bloquea)
                    self._en_curso[ident] = (tipo, ahora)
                    en_curso = _tarjeta_en_curso(guardada, tipo)
        if motivo is not None:
            log.info("Decisión de tarjeta ignorada (%s): %s", tipo, motivo)
            if respuesta is not None:
                self._responder(cliente, "tarjeta", respuesta)
            return
        log.info("Decisión de tarjeta desde el escritorio: %s", tipo)
        if en_curso is not None:
            self._enviar(_TODOS, "tarjeta", en_curso)
        self._bus.publicar("escritorio.tarjeta", decision, origen="escritorio")

    def _motivo_para_ignorar(self, ident: str, guardada: Mapping[str, Any], decision: Mapping[str, Any],
                             ahora: float) -> str | None:
        """Por qué se ignora una decisión (no ``descartar``) sobre una tarjeta vigente, o ``None`` si vale.

        Bajo ``self._lock``. Los motivos son textos fijos (nunca el contenido de la tarjeta).
        """
        if self._marca_en_curso(ident, ahora) is not None:
            return "otra decisión sobre esa tarjeta sigue en curso"
        if decision.get("version") != guardada["version"] \
                or not hmac.compare_digest(str(decision["sha256"]), str(guardada["sha256"])):
            return "no es la versión vigente"
        tipo = decision["decision"]
        if tipo not in guardada["botones"]:
            return "la tarjeta no ofrece ese botón"
        if tipo == "aprobar_publico" and ahora - self._publico_desde.get(ident, -math.inf) < ARMADO_PUBLICO_S:
            return "segunda confirmación demasiado pronto"
        return None

    def _marca_en_curso(self, ident: str, ahora: float) -> tuple[str, float] | None:
        """``(decisión, reloj)`` si la tarjeta sigue «en curso» (la marca vencida se borra). Bajo el lock."""
        marca = self._en_curso.get(ident)
        if marca is not None and ahora - marca[1] >= EN_CURSO_MAX_S:
            del self._en_curso[ident]
            log.info("Una tarjeta llevaba más de %d s en curso sin respuesta: vuelve a estar disponible",
                     int(EN_CURSO_MAX_S))
            return None
        return marca

    def _vista_tarjeta(self, ident: str, tarjeta: dict[str, Any], ahora: float) -> dict[str, Any]:
        """Lo que se envía de una tarjeta vigente: ella misma o, si está «en curso», su vista sin botones de
        decisión (:func:`_tarjeta_en_curso`). Bajo ``self._lock``."""
        marca = self._marca_en_curso(ident, ahora)
        return tarjeta if marca is None else _tarjeta_en_curso(tarjeta, marca[0])

    def _responder(self, cliente: _Cliente, tipo: str, datos: Mapping[str, Any]) -> None:
        """Envía ``{tipo, datos}`` solo a ``cliente`` (hilo del loop; nunca lanza ni registra el contenido)."""
        if not self._activo or _ws_broadcast is None:
            return
        transporte = getattr(cliente.conexion, "transport", None)
        try:
            pendiente = transporte.get_write_buffer_size() if transporte is not None else 0
        except Exception:
            pendiente = 0
        if pendiente > MAX_BUFFER_CLIENTE:
            log.debug("/escritorio: cliente saturado; no se le reenvía %s", tipo)
            return
        try:
            _ws_broadcast([cliente.conexion], _serializar(tipo, datos), raise_exceptions=True)
        except Exception as exc:  # incluye ExceptionGroup
            log.debug("No se pudo reenviar %s a un cliente de /escritorio: %s", tipo, type(exc).__name__)

    _ENTRANTES: Final[dict[str, Callable[[EnlaceEscritorio, _Cliente, Mapping[str, Any]], None]]] = {
        "hola_app": _en_hola_app,
        "chat": _en_chat,
        "evento": _en_evento,
        "control": _en_control,
        "confirmacion": _en_confirmacion,
        "caja": _en_caja,
        "puter.estado": _en_puter_estado,
        "llm.respuesta": _en_llm_respuesta,
        "tarjeta_decision": _en_tarjeta_decision,
    }

    # ------------------------------------------------------------------
    # Bus → salientes
    # ------------------------------------------------------------------

    def _suscribir(self) -> None:
        """Se suscribe a los eventos que reenvía ``/escritorio`` (nombres exactos)."""
        self._desuscripciones = [self._bus.suscribir(nombre, self._al_evento)
                                 for nombre in EVENTOS_SUSCRITOS_ESCRITORIO]

    @staticmethod
    def _travesuras_saneadas(datos: Mapping[str, Any]) -> dict[str, Any]:
        return {"activas": bool(datos.get("activas", False)), "hasta_ts": _real(datos.get("hasta_ts"))}

    @staticmethod
    def _no_molestar_saneado(datos: Mapping[str, Any]) -> dict[str, Any]:
        """``pc.no_molestar`` → ``{activo, motivo, usuario}``; ``usuario`` = el que el usuario puso a mano
        (``activo and motivo == "usuario"``), que es el estado del botón «No molestar» de los chats."""
        activo = datos.get("activo") is True
        motivo = datos.get("motivo")
        if not activo or not isinstance(motivo, str) or motivo not in MOTIVOS_NO_MOLESTAR:
            motivo = ""
        return {"activo": activo, "motivo": motivo, "usuario": activo and motivo == "usuario"}

    def _al_evento(self, evento: Evento) -> None:
        """Manejador síncrono del bus: guarda el estado y programa el mensaje saliente."""
        try:
            nombre = evento.nombre
            datos: Mapping[str, Any] = evento.datos if isinstance(evento.datos, Mapping) else {}
            salida = self._traducir(nombre, datos)
            if salida is None or not self._activo:
                return
            destino, tipo, mensaje = salida
            self._programar(destino, tipo, mensaje)
        except Exception as exc:  # nunca propaga al bus
            log.debug("Error al procesar %s para /escritorio: %s", getattr(evento, "nombre", "?"),
                      type(exc).__name__)

    def _traducir(self, nombre: str, datos: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        """Evento del bus → ``(destino, tipo, datos)`` de ``/escritorio`` (y guarda el estado)."""
        if nombre == "salida.subtitulo":
            return _TODOS, "subtitulo", {
                "texto": _texto(datos.get("texto"), MAX_TEXTO_SUBTITULO),
                "exp": _texto(datos.get("exp"), 40),
                "canal": _texto(datos.get("canal"), 20),
                "origen": _texto(datos.get("origen"), 20),
            }
        if nombre in ("voz.inicio", "voz.fin"):
            return _TODOS, "voz", {"hablando": nombre == "voz.inicio"}
        if nombre == "escritorio.orden":
            orden = _orden_saneada(datos)
            if orden is None:
                log.debug("escritorio.orden inválida: no se reenvía")
                return None
            return _APP, "orden", orden
        if nombre == "escritorio.sistema":
            texto = _texto(datos.get("texto"), MAX_TEXTO_SISTEMA)
            return (_TODOS, "sistema", {"texto": texto}) if texto else None
        if nombre == "permiso.solicitado":
            permiso: dict[str, Any] = {
                "id": _texto(datos.get("id"), 64),
                "descripcion": _texto(datos.get("descripcion"), 300),
                "destructiva": bool(datos.get("destructiva", False)),
                "pendiente": True,
            }
            vista = datos.get("vista_previa")
            if isinstance(vista, str) and vista:
                permiso["vista_previa"] = _texto(vista, MAX_VISTA_PREVIA)
            with self._lock:
                self._permiso_pendiente = dict(permiso)
            return _TODOS, "permiso", permiso
        if nombre == "permiso.resuelto":
            ident = _texto(datos.get("id"), 64)
            with self._lock:
                pendiente = self._permiso_pendiente
                mismo = pendiente is not None and pendiente.get("id") in ("", ident)
                if pendiente is None or mismo:
                    self._permiso_pendiente = None
            return _TODOS, "permiso", {
                "id": ident,
                "descripcion": pendiente.get("descripcion", "") if (pendiente and mismo) else "",
                "destructiva": bool(pendiente.get("destructiva", False)) if (pendiente and mismo) else False,
                "pendiente": False,
                "aprobado": bool(datos.get("aprobado", False)),
            }
        if nombre == "homeostasis.estado":
            estado = datos.get("estado")
            if not isinstance(estado, Mapping):
                return None
            resumen = {
                "aburrimiento": _acotar(estado.get("boredom"), 0.0, 1.0, 0.0),
                "fatiga": _acotar(estado.get("fatiga"), 0.0, 1.0, 0.0),
                "valencia": _acotar(estado.get("valencia"), -1.0, 1.0, 0.0),
            }
            with self._lock:
                self._estado = dict(resumen)
            return _APP, "estado", resumen
        if nombre == "motor.travesuras":
            travesuras = self._travesuras_saneadas(datos)
            with self._lock:
                self._travesuras = dict(travesuras)
            return _TODOS, "travesuras", travesuras
        if nombre == "pc.no_molestar":
            no_molestar = self._no_molestar_saneado(datos)
            with self._lock:
                self._no_molestar = dict(no_molestar)
            return _TODOS, "no_molestar", no_molestar
        if nombre == "vision.camara":
            # OR de las fuentes (la continua se repite cada 5 s): solo se envía si cambia.
            fuente = datos.get("fuente")
            fuente = fuente if fuente in FUENTES_CAMARA else "vistazo"
            with self._lock:
                self._camara_por_fuente[fuente] = datos.get("activa") is True
                camara = any(self._camara_por_fuente.values())
                if camara == self._camara_enviada:
                    return None
                self._camara_enviada = camara
            return _APP, "vision", {"camara": camara}
        if nombre == "tarjeta.mostrar":
            return self._traducir_tarjeta(datos)
        if nombre == "tarjeta.cerrada":
            ident = datos.get("id")
            with self._lock:
                guardada = self._tarjetas.pop(ident, None) if isinstance(ident, str) else None
                if isinstance(ident, str):
                    self._en_curso.pop(ident, None)
                    self._publico_desde.pop(ident, None)
            cerrada = _tarjeta_cerrada_saneada(datos, guardada["clase"] if guardada else None)
            if cerrada is None:
                log.debug("tarjeta.cerrada inválida: no se reenvía")
                return None
            return _TODOS, "tarjeta", cerrada
        return None

    def _traducir_tarjeta(self, datos: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
        """``tarjeta.mostrar`` → mensaje ``tarjeta`` para todos (§12.2–§12.4), o ``None`` si se descarta.

        Sanea, encoge la de ``script`` si hace falta y la guarda como vigente (≤ ``MAX_TARJETAS_VIVAS``;
        sobra la más vieja). Una ``version`` menor que la guardada se ignora; igual o mayor la reemplaza
        y es la respuesta de main: la tarjeta deja de estar «en curso». Anota cuándo empezó a ofrecer
        ``aprobar_publico`` (paso 2). Nunca registra el contenido: solo la clase.
        """
        clase = datos.get("clase")
        clase_log = clase if isinstance(clase, str) and clase in CLASES_TARJETA else "?"
        try:
            tarjeta: dict[str, Any] | None = _tarjeta_saneada(datos)
        except _TarjetaInvalida as exc:
            if exc.aprobable:
                log.warning("Tarjeta %s inválida: descartada", clase_log)
            else:
                log.debug("Tarjeta %s con formato inválido: descartada", clase_log)
            return None
        tarjeta = _tarjeta_que_cabe(tarjeta)
        if tarjeta is None:
            log.warning("Tarjeta %s demasiado grande: descartada", clase_log)
            return None
        ident = tarjeta["id"]
        ahora = self._reloj()
        with self._lock:
            anterior = self._tarjetas.get(ident)
            if anterior is not None and anterior["version"] > tarjeta["version"]:
                log.debug("Tarjeta %s con una versión vieja: se ignora", clase_log)
                return None
            self._en_curso.pop(ident, None)
            if "aprobar_publico" not in tarjeta["botones"]:
                self._publico_desde.pop(ident, None)
            elif anterior is None or anterior["version"] != tarjeta["version"] \
                    or "aprobar_publico" not in anterior["botones"]:
                self._publico_desde[ident] = ahora
            self._tarjetas.pop(ident, None)
            self._tarjetas[ident] = tarjeta
            while len(self._tarjetas) > MAX_TARJETAS_VIVAS:
                sobrante = next(iter(self._tarjetas))
                del self._tarjetas[sobrante]
                self._en_curso.pop(sobrante, None)
                self._publico_desde.pop(sobrante, None)
        return _TODOS, "tarjeta", tarjeta

    def _programar(self, destino: str, tipo: str, datos: dict[str, Any]) -> None:
        """Envía en el hilo del loop (delegando con ``call_soon_threadsafe`` si hace falta)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if threading.get_ident() == self._hilo_loop:
            self._enviar(destino, tipo, datos)
            return
        try:
            loop.call_soon_threadsafe(self._enviar, destino, tipo, datos)
        except RuntimeError as exc:
            log.debug("Loop de /escritorio cerrado; se descarta %s (%s)", tipo, exc)

    def _enviar(self, destino: str, tipo: str, datos: dict[str, Any]) -> None:
        """Aplica el límite de ``estado`` (≤ 1 Hz, con coalescencia) y difunde."""
        if not self._activo:
            return
        if tipo != "estado":
            self._difundir(destino, _serializar(tipo, datos))
            return
        if self._app is None:
            return
        limite = self._limite_estado
        if limite.temporizador is not None:
            limite.pendiente = datos
            return
        ahora = self._reloj()
        espera = limite.intervalo - (ahora - limite.ultimo_envio)
        if espera <= 0:
            limite.ultimo_envio = ahora
            self._difundir(destino, _serializar(tipo, datos))
            return
        loop = self._loop
        if loop is None:
            return
        limite.pendiente = datos
        limite.temporizador = loop.call_later(espera, self._vaciar_estado)

    def _vaciar_estado(self) -> None:
        """Envía el ``estado`` diferido más reciente."""
        limite = self._limite_estado
        datos = limite.pendiente
        limite.pendiente = None
        limite.temporizador = None
        if datos is None or not self._activo or self._app is None:
            return
        limite.ultimo_envio = self._reloj()
        self._difundir(_APP, _serializar("estado", datos))

    def _enviar_a_app(self, tipo: str, datos: Mapping[str, Any]) -> bool:
        """Envía ``{tipo, datos}`` **solo** a Amadeus Desktop, en el hilo del loop.

        ``True`` si el mensaje salió hacia el socket; ``False`` sin app, fuera del hilo del loop, con
        el cliente saturado o si el envío falló. Nunca registra el contenido.
        """
        cliente = self._app
        if not self._activo or cliente is None or _ws_broadcast is None:
            return False
        if self._hilo_loop is not None and threading.get_ident() != self._hilo_loop:
            log.debug("/escritorio: envío de %s fuera del hilo del loop descartado", tipo)
            return False
        transporte = getattr(cliente.conexion, "transport", None)
        try:
            pendiente = transporte.get_write_buffer_size() if transporte is not None else 0
        except Exception:
            pendiente = 0
        if pendiente > MAX_BUFFER_CLIENTE:
            log.info("Amadeus Desktop saturado (%d bytes sin enviar): no se envía %s", pendiente, tipo)
            return False
        try:
            _ws_broadcast([cliente.conexion], _serializar(tipo, datos), raise_exceptions=True)
        except Exception as exc:  # incluye ExceptionGroup
            log.debug("No se pudo enviar %s a Amadeus Desktop: %s", tipo, type(exc).__name__)
            return False
        return True

    def _difundir(self, destino: str, texto: str) -> None:
        """Escribe ``texto`` en los clientes del destino (sin esperar; suelta a los saturados)."""
        if _ws_broadcast is None:
            return
        clientes = [self._app] if self._app is not None else []
        if destino == _TODOS:
            clientes.extend(self._huds.values())
        conexiones = []
        for cliente in clientes:
            transporte = getattr(cliente.conexion, "transport", None)
            try:
                pendiente = transporte.get_write_buffer_size() if transporte is not None else 0
            except Exception:
                pendiente = 0
            if pendiente > MAX_BUFFER_CLIENTE:
                log.info("Cliente de /escritorio saturado (%d bytes sin enviar); se desconecta", pendiente)
                try:
                    transporte.abort()
                except Exception as exc:
                    log.debug("Error al abortar un cliente saturado: %s", type(exc).__name__)
                continue
            conexiones.append(cliente.conexion)
        if not conexiones:
            return
        try:
            _ws_broadcast(conexiones, texto, raise_exceptions=True)
        except Exception as exc:  # incluye ExceptionGroup: algún cliente no pudo recibir
            log.debug("Difusión parcial de /escritorio: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Respaldo Puter del Área de Broca (§C2.15)
# ---------------------------------------------------------------------------


def _resolver(futuro: asyncio.Future[Any], valor: Any) -> None:
    """Resuelve ``futuro`` con ``valor`` en su propio loop (si aún no terminó; nunca lanza)."""
    def poner() -> None:
        if not futuro.done():
            futuro.set_result(valor)

    loop = futuro.get_loop()
    try:
        actual: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        actual = None
    try:
        if actual is loop:
            poner()
        else:
            loop.call_soon_threadsafe(poner)
    except RuntimeError:  # loop cerrado: quien esperaba ya no está
        pass


class RespaldoPuter:
    """Proveedor de respaldo de Broca sobre ``/escritorio``: Puter.js dentro de Amadeus Desktop (§C2.15).

    Cumple ``lobes.broca_llm.ProveedorRespaldo`` (``nombre``, ``disponible``, ``completar``). El
    uso se descuenta del saldo de la **cuenta de Puter del usuario** (modelo *user-pays*), que inicia
    sesión desde la bandeja de Amadeus Desktop; R.A.M.S. no guarda ninguna clave de Puter.

    - ``disponible``: ``/escritorio`` activo, Amadeus Desktop conectado, último ``puter.estado`` con
      ``listo`` y sin enfriamiento por ``cuota``.
    - ``completar(...)``: envía ``llm.pedir`` solo a la app, espera su ``llm.respuesta`` (mismo ``id``)
      como mucho ``min(timeout_s, 40)`` s (contando la espera de turno) y devuelve el texto o
      ``None`` (nunca lanza por la red).
    - Turnos: como mucho ``MAX_PEDIDOS_PUTER`` pedidos **en curso en la app** a la vez; el resto
      espera turno dentro de su plazo. Un pedido enviado conserva su turno hasta que llega su
      ``llm.respuesta`` aunque ``completar`` ya haya devuelto ``None`` (la app lo sigue procesando
      y rechazaría un 3.º), hasta que se va la app o, como tope, ``HUECO_PUTER_MAX_S``. Si tras
      esperar turno queda poco plazo, no se envía (se cobraría sin llegar a tiempo).
    - Al desconectarse o reemplazarse la app, los pedidos pendientes devuelven ``None``, sus turnos
      quedan libres y el estado vuelve a "sin sesión" hasta el siguiente ``puter.estado``.
    - Publica ``sistema.modulo`` (clave ``"puter"``) cuando cambia su diagnóstico.

    Todo ocurre en el hilo del loop de ``/escritorio`` (el mismo que el de Broca). El prompt y las
    respuestas nunca se registran.
    """

    nombre: str = NOMBRE_RESPALDO_PUTER

    def __init__(self, enlace: EnlaceEscritorio, *, modelo: str = "",
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self._enlace = enlace
        self._modelo: str = _modelo_puter(modelo) or ""
        if modelo and not self._modelo:
            log.warning("PUTER_MODEL no es un nombre de modelo válido: se usa el de Amadeus Desktop")
        self._reloj = reloj
        self._estado: dict[str, Any] | None = None   # último puter.estado de la app conectada
        self._pendientes: dict[str, asyncio.Future[Any]] = {}
        # Pedidos que la app tiene en curso (enviados, sin su llm.respuesta todavía): cada uno guarda
        # su turno del semáforo y el temporizador de seguridad que lo suelta si la respuesta no llega.
        self._en_curso: dict[str, tuple[asyncio.Semaphore, asyncio.TimerHandle]] = {}
        self._semaforo: asyncio.Semaphore | None = None
        self._loop_semaforo: asyncio.AbstractEventLoop | None = None
        self._enfriamiento_hasta = -math.inf
        self._temporizador: asyncio.TimerHandle | None = None
        self._diag_publicado: tuple[str, str] | None = None
        self._vinculado = True
        # Contadores de la sesión (no consumen el tope diario de Gemini).
        self.pedidos = 0
        self.respuestas = 0
        self.fallos = 0
        self.ultimo_error: str | None = None
        self.ultimo_modelo: str | None = None   # modelo de la última respuesta (Broca lo usa en BrocaReply)
        enlace.vincular_puter(self)

    def __repr__(self) -> str:
        return (f"RespaldoPuter(disponible={self.disponible}, modelo={self.modelo_efectivo!r}, "
                f"pendientes={len(self._pendientes)}, en_curso={len(self._en_curso)})")

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    @property
    def disponible(self) -> bool:
        """``True`` si se le puede pedir texto ahora mismo (app conectada y sesión de Puter lista)."""
        estado = self._estado
        return (self._vinculado and self._enlace.activo and self._enlace.overlay_conectado
                and estado is not None and bool(estado.get("listo")) and not self._en_enfriamiento())

    @property
    def modelo(self) -> str:
        """Modelo configurado (``PUTER_MODEL``); ``""`` = el modelo por defecto de Amadeus Desktop."""
        return self._modelo

    @property
    def modelo_efectivo(self) -> str | None:
        """Modelo que se usará: el configurado o el que anuncia Amadeus Desktop (``None`` si no se sabe)."""
        if self._modelo:
            return self._modelo
        estado = self._estado
        return estado.get("modelo") if estado is not None else None

    def estado(self) -> dict[str, Any]:
        """Instantánea: ``{conectado, listo, sesion, modelo, error, enfriamiento_s, pendientes, en_curso}``.

        ``pendientes``: pedidos cuyo ``completar`` aún espera; ``en_curso``: pedidos que la app tiene
        entre manos (incluye los que ``completar`` ya dio por perdidos y aún ocupan su turno).
        """
        estado = self._estado or {}
        restante = self._enfriamiento_hasta - self._reloj()
        return {
            "conectado": bool(self._enlace.activo and self._enlace.overlay_conectado),
            "listo": bool(estado.get("listo", False)),
            "sesion": bool(estado.get("sesion", False)),
            "modelo": self.modelo_efectivo,
            "error": estado.get("error"),
            "enfriamiento_s": round(restante, 1) if restante > 0 else 0.0,
            "pendientes": len(self._pendientes),
            "en_curso": len(self._en_curso),
        }

    def estadisticas(self) -> dict[str, Any]:
        """Contadores de la sesión: ``{pedidos, respuestas, fallos, ultimo_error}``."""
        return {"pedidos": self.pedidos, "respuestas": self.respuestas, "fallos": self.fallos,
                "ultimo_error": self.ultimo_error}

    def diagnostico(self) -> DiagnosticoModulo:
        """``EN ESPERA`` (sin app de escritorio, Puter desactivado o sin conexión en la app, ventana
        caída, sin sesión de Puter), ``DEGRADADO`` (saldo de Puter agotado: pausa) u ``OK`` con el modelo.

        El detalle no depende del reloj (el HUD y la tabla de ``/estado`` muestran el último
        publicado): la cuenta atrás de la pausa la da ``estado()["enfriamiento_s"]``.
        """
        def diag(estado_modulo: EstadoModulo, detalle: str) -> DiagnosticoModulo:
            return DiagnosticoModulo(CLAVE_DIAG_PUTER, NOMBRE_DIAG_PUTER, estado_modulo, detalle)

        estado = self._estado
        if not self._vinculado or not self._enlace.activo or not self._enlace.overlay_conectado:
            return diag(EstadoModulo.EN_ESPERA, "sin app de escritorio")
        if estado is None:
            return diag(EstadoModulo.EN_ESPERA, "sin sesión de Puter")
        error = estado.get("error")
        if not estado.get("listo"):
            # El código manda sobre "sesion": con el escritorio real, sin puter.js cargado "sesion" es
            # siempre false, y decir "sin sesión" mandaría al usuario a iniciar una que no arregla nada.
            explicado = _DETALLE_ERROR_ESTADO_PUTER.get(error) if isinstance(error, str) else None
            if explicado is not None:
                return diag(EstadoModulo.EN_ESPERA, explicado)
            if not estado.get("sesion"):
                return diag(EstadoModulo.EN_ESPERA, "sin sesión de Puter")
            return diag(EstadoModulo.EN_ESPERA, "Puter sin cargar" + (f" ({error})" if error else ""))
        if self._en_enfriamiento():
            minutos = max(1, math.ceil(ENFRIAMIENTO_CUOTA_PUTER_S / 60.0))
            return diag(EstadoModulo.DEGRADADO, f"saldo de Puter agotado · pausa de {minutos} min")
        detalle = self.modelo_efectivo or "modelo por defecto de Amadeus Desktop"
        if error == "modelo_desconocido" and not self._modelo:
            # Aviso de la app sobre SU modelo por defecto (con PUTER_MODEL no se usa): puede ser un
            # alias que puter.ai.listModels() no enumera, así que no pasa a DEGRADADO.
            detalle += " · no aparece en el listado de Puter"
        return diag(EstadoModulo.OK, detalle)

    def desvincular(self) -> None:
        """Suelta el enlace: deja de estar disponible y los pedidos pendientes devuelven ``None``."""
        if self._enlace.puter is self:
            self._enlace.vincular_puter(None)
        self._vinculado = False
        self._al_desconectar()
        if self._temporizador is not None:
            self._temporizador.cancel()
            self._temporizador = None

    # ------------------------------------------------------------------
    # ProveedorRespaldo
    # ------------------------------------------------------------------

    async def completar(self, system: str, mensajes: list[dict[str, str]], *, modelo: str = "",
                        max_tokens: int = 1024, temperatura: float = 1.0,
                        timeout_s: float = TIMEOUT_PUTER_S) -> str | None:
        """Pide a Puter (vía Amadeus Desktop) la respuesta al ``system`` + ``mensajes``.

        ``mensajes``: ``[{"rol": "usuario"|"modelo", "texto": str}, ...]`` en orden. ``modelo=""``
        usa ``PUTER_MODEL`` o, si tampoco hay, el modelo por defecto de Amadeus Desktop. Devuelve el
        texto tal cual (el postprocesado de etiquetas es cosa de Broca) o ``None`` si no hay
        respuesta válida a tiempo.
        """
        if not self.disponible:
            return None
        pedido = _pedido_puter_saneado(system, mensajes, modelo=_modelo_puter(modelo) or self._modelo,
                                       max_tokens=max_tokens, temperatura=temperatura)
        if pedido is None:
            log.debug("Pedido a Puter sin mensajes: no se envía")
            return None
        plazo = _real(timeout_s)
        plazo = TIMEOUT_PUTER_S if plazo is None or plazo <= 0 else min(plazo, TIMEOUT_PUTER_S)
        loop = asyncio.get_running_loop()
        fin = loop.time() + plazo
        semaforo = self._semaforo_de(loop)
        try:
            async with asyncio.timeout_at(fin):
                await semaforo.acquire()
        except TimeoutError:
            self._fallo("ocupado")
            return None
        # Con el turno tomado: _pedir lo deja en _en_curso si el pedido sale (lo suelta su
        # llm.respuesta, no este plazo); si no se envía, se devuelve aquí.
        if not self.disponible:  # pudo cambiar mientras esperaba turno
            semaforo.release()
            return None
        if fin - loop.time() < min(MIN_PLAZO_ENVIO_PUTER_S, plazo / 2):
            semaforo.release()
            self._fallo("ocupado")  # la espera de turno se comió el plazo
            return None
        resultado = await self._pedir(loop, semaforo, pedido, fin)
        if resultado is None:
            return None
        if not resultado["ok"]:
            # El efecto en el estado (sin sesión / enfriamiento) ya se aplicó al llegar la respuesta.
            self._fallo(resultado["error"])
            return None
        self.respuestas += 1
        self.ultimo_error = None
        self.ultimo_modelo = resultado["modelo"] or pedido["modelo"] or self.modelo_efectivo
        if resultado["ms"] is not None:
            log.debug("Puter respondió en %d ms (%s)", resultado["ms"], resultado["modelo"] or "modelo por defecto")
        return resultado["texto"]

    async def _pedir(self, loop: asyncio.AbstractEventLoop, semaforo: asyncio.Semaphore,
                     pedido: dict[str, Any], fin: float) -> dict[str, Any] | None:
        """Envía ``llm.pedir`` y espera su ``llm.respuesta`` hasta ``fin`` (``None``: sin respuesta).

        Recibe el turno de ``semaforo`` ya tomado. Si el pedido sale, el turno pasa a ``_en_curso``
        y lo suelta la respuesta de la app (o su desconexión, o ``HUECO_PUTER_MAX_S``), aunque este
        plazo venza antes o la espera se cancele; si no sale, se suelta aquí mismo.
        """
        ident = secrets.token_hex(12)
        while ident in self._pendientes or ident in self._en_curso:  # pragma: no cover - 96 bits
            ident = secrets.token_hex(12)
        futuro: asyncio.Future[Any] = loop.create_future()
        self._pendientes[ident] = futuro
        entregado = False
        try:
            if not self._enlace._enviar_a_app("llm.pedir", {"id": ident, **pedido}):
                self._fallo("sin_app")
                return None
            self._en_curso[ident] = (semaforo, loop.call_later(HUECO_PUTER_MAX_S, self._soltar_turno, ident))
            entregado = True
            self.pedidos += 1
            try:
                async with asyncio.timeout_at(fin):
                    resultado = await futuro
            except TimeoutError:
                self._fallo("timeout")
                return None
            if resultado is None:
                self._fallo("desconectado")
            return resultado
        finally:
            if not entregado:
                semaforo.release()
            self._pendientes.pop(ident, None)
            if not futuro.done():
                futuro.cancel()

    def _semaforo_de(self, loop: asyncio.AbstractEventLoop) -> asyncio.Semaphore:
        """Semáforo de ``MAX_PEDIDOS_PUTER`` del loop actual (se rehace si cambia el loop: los turnos
        en curso del loop anterior ya no cuentan)."""
        if self._semaforo is None or self._loop_semaforo is not loop:
            self._olvidar_turnos()
            self._semaforo = asyncio.Semaphore(MAX_PEDIDOS_PUTER)
            self._loop_semaforo = loop
        return self._semaforo

    def _soltar_turno(self, ident: str) -> bool:
        """Suelta el turno del pedido ``ident`` en curso en la app (``True`` si lo tenía)."""
        entrada = self._en_curso.pop(ident, None)
        if entrada is None:
            return False
        semaforo, temporizador = entrada
        temporizador.cancel()
        semaforo.release()
        return True

    def _soltar_turnos(self) -> None:
        """La app se fue (o la reemplazó otra): nada de lo enviado sigue en curso en ella."""
        for ident in list(self._en_curso):
            self._soltar_turno(ident)

    def _olvidar_turnos(self) -> None:
        """Descarta los turnos de un loop que ya no se usa (sin tocar su semáforo; nunca lanza)."""
        for _semaforo, temporizador in self._en_curso.values():
            try:
                temporizador.cancel()
            except Exception:  # pragma: no cover - loop anterior ya cerrado
                pass
        self._en_curso.clear()

    def _fallo(self, motivo: str) -> None:
        self.fallos += 1
        self.ultimo_error = motivo
        log.info("Respaldo Puter sin respuesta (%s)", motivo)

    def _efecto_error(self, error: str) -> None:
        """Efecto en el estado de un error que informa Amadeus Desktop en ``llm.respuesta``.

        Se aplica al **recibir** la respuesta (en ``_al_respuesta``), no cuando ``completar`` se
        reanuda: tras un ``sin_sesion`` la app reenvía su ``puter.estado`` real justo detrás, y ese
        estado tiene que pisar a la marca de "sin sesión", no al revés.
        """
        if error == "sin_sesion" and self._estado is not None:
            # La sesión caducó: hasta el próximo puter.estado no se le pide nada.
            self._estado = dict(self._estado, listo=False, sesion=False)
            self._publicar_si_cambia()
        elif error == "cuota":
            self._enfriamiento_hasta = self._reloj() + ENFRIAMIENTO_CUOTA_PUTER_S
            self._programar_fin_enfriamiento()
            self._publicar_si_cambia()

    def _en_enfriamiento(self) -> bool:
        return self._reloj() < self._enfriamiento_hasta

    def _programar_fin_enfriamiento(self) -> None:
        """Vuelve a publicar el diagnóstico cuando termina el enfriamiento por ``cuota``."""
        if self._temporizador is not None:
            self._temporizador.cancel()
            self._temporizador = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        espera = max(0.0, self._enfriamiento_hasta - self._reloj()) + 0.05
        self._temporizador = loop.call_later(espera, self._fin_enfriamiento)

    def _fin_enfriamiento(self) -> None:
        self._temporizador = None
        self._publicar_si_cambia()

    # ------------------------------------------------------------------
    # Avisos del enlace (hilo del loop)
    # ------------------------------------------------------------------

    def _al_estado(self, estado: dict[str, Any]) -> None:
        """Nuevo ``puter.estado`` (ya saneado) de la app activa."""
        self._estado = dict(estado)
        self._publicar_si_cambia()

    def _espera_respuesta(self, ident: str) -> bool:
        """``True`` si ``ident`` es un pedido que la app tiene en curso: su ``llm.respuesta``, aunque
        llegue tarde, suelta un turno (y si ``completar`` aún espera, le da el texto). Son ≤ 2."""
        if ident in self._en_curso:
            return True
        futuro = self._pendientes.get(ident)
        return futuro is not None and not futuro.done()

    def _al_respuesta(self, respuesta: dict[str, Any]) -> None:
        """``llm.respuesta`` (ya saneada): suelta el turno de ese ``id`` y resuelve su espera.

        Una respuesta **tardía** a un pedido propio (``completar`` ya devolvió ``None``) suelta el
        turno y aplica el efecto de su error (``cuota``/``sin_sesion`` siguen siendo ciertos), pero
        no cambia los contadores; una ajena o repetida se ignora.
        """
        ident = respuesta["id"]
        propio = self._soltar_turno(ident)
        futuro = self._pendientes.get(ident)
        esperando = futuro is not None and not futuro.done()
        if not propio and not esperando:
            log.debug("/escritorio: llm.respuesta sin pedido en curso (ajena o repetida) ignorada")
            return
        if not respuesta["ok"]:
            self._efecto_error(respuesta["error"])
        if futuro is not None and esperando:
            _resolver(futuro, respuesta)
        else:
            log.debug("/escritorio: llm.respuesta tardía: su turno queda libre")

    def _al_cambiar_app(self) -> None:
        """Se conectó (o reemplazó) Amadeus Desktop: los pedidos de antes ya no tendrán respuesta."""
        self._fallar_pendientes()
        self._soltar_turnos()
        self._estado = None
        self._publicar_si_cambia()

    def _al_desconectar(self) -> None:
        """Se fue Amadeus Desktop (o se cerró el enlace): pendientes → ``None``, turnos libres y
        estado a cero."""
        self._fallar_pendientes()
        self._soltar_turnos()
        self._estado = None
        self._publicar_si_cambia()

    def _fallar_pendientes(self) -> None:
        for futuro in list(self._pendientes.values()):
            _resolver(futuro, None)

    def _publicar_si_cambia(self) -> None:
        """``sistema.modulo`` (clave ``"puter"``) solo si el diagnóstico cambió (nunca lanza)."""
        try:
            diag = self.diagnostico()
            firma = (diag.estado.value, diag.detalle)
            if firma == self._diag_publicado:
                return
            self._diag_publicado = firma
            log.info("Respaldo Puter: %s (%s)", diag.estado.value, diag.detalle)
            self._enlace._bus.publicar("sistema.modulo", diag.a_dict(), origen="escritorio")
        except Exception as exc:
            log.debug("No se pudo publicar el diagnóstico de Puter: %s", type(exc).__name__)
