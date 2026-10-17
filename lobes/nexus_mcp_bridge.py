#!/usr/bin/env python3
"""Puente MCP (stdio) entre Nexus IDE y R.A.M.S. BIO-CORE.

Nexus IDE lanza este script como servidor MCP: JSON-RPC 2.0, **un mensaje por línea** en
stdin/stdout (docs/ARQUITECTURA.md §10.15 e informe 02 §1.3). Cada ``tools/call`` se reenvía
al demonio R.A.M.S. por su socket de control local (``127.0.0.1:8766``, token en
``~/.config/rams_biocore/ctl_token``); si R.A.M.S. no está en ejecución se responde con
``isError: true`` y un texto amable, de modo que Nexus degrada sin colgarse.

Restricciones duras:
- **Solo biblioteca estándar** y ningún módulo del proyecto: Nexus lo ejecuta con ``python -I``.
- **Nunca escribe en stderr**: Nexus no lee ese pipe; si se llena, el proceso se bloquea. Los
  logs van a ``~/.cache/rams_biocore/mcp_bridge.log`` (Linux) o
  ``%LOCALAPPDATA%\\rams_biocore\\mcp_bridge.log`` (Windows); si no se puede, a un
  ``NullHandler``. Durante :func:`main` ``sys.stderr`` apunta a ``os.devnull``.
- Todo texto devuelto a Nexus se sanea (``[``→``⟦``, ``]``→``⟧``, ≤ 8000 caracteres) para que
  nunca forme etiquetas ejecutables de Nexus (``[RUN_COMMAND]``, ``[MCP: …]``).
- El token nunca aparece en el log.

Registro en Nexus (Ajustes → Servidores MCP)::

    [{"name":"rams","command":"/home/<usuario>/rams_biocore/.venv/bin/python",
      "args":["-I","/home/<usuario>/rams_biocore/lobes/nexus_mcp_bridge.py"]}]

Opcionalmente se añaden ``"--puerto","8766","--token-file","/ruta/ctl_token"`` a ``args``; si
no, el puente lee el ``.env`` del proyecto (Nexus lo lanza sin ese entorno).
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, TextIO

__all__ = ["PROTO", "HERRAMIENTAS", "resolver_config", "preguntar_a_rams", "procesar_mensaje", "main"]

PROTO: Final = "2024-11-05"                       # el que envía Nexus (mcp-client.js)
VERSION_SERVIDOR: Final = "0.1.0"
PUERTO_DEFECTO: Final = 8766
TIEMPO_ESPERA_S: Final = 12.0                      # por debajo de los 15 s de Nexus
MAX_TEXTO: Final = 8000
_MAX_RESPUESTA_BYTES: Final = 1 << 20

MSG_SIN_TOKEN: Final = "R.A.M.S. no está configurado (falta el token de control)."
MSG_SIN_CONEXION: Final = "R.A.M.S. no está en ejecución: Amadeus está desconectada del laboratorio."
MSG_SIN_RESPUESTA: Final = "R.A.M.S. no respondió a tiempo; inténtalo de nuevo en unos segundos."
MSG_RESPUESTA_INVALIDA: Final = "R.A.M.S. devolvió una respuesta inválida."
MSG_ERROR_INTERNO: Final = "Error interno del puente MCP de R.A.M.S."

HERRAMIENTAS: Final[list[dict[str, Any]]] = [
    {"name": "estado_homeostatico",
     "description": "Estado interno de Amadeus (arousal, focus, boredom, fatiga) calculado por el conectoma, sin LLM.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "memoria_buscar",
     "description": "Busca en la memoria de R.A.M.S. hechos sobre el usuario o el proyecto.",
     "inputSchema": {"type": "object", "properties": {"consulta": {"type": "string"}}, "required": ["consulta"]}},
    {"name": "investigar_web",
     "description": "Búsqueda web en tiempo real (Brave o DuckDuckGo). Devuelve titulares y URLs resumidos.",
     "inputSchema": {"type": "object", "properties": {"consulta": {"type": "string"}}, "required": ["consulta"]}},
    {"name": "amadeus_comentar",
     "description": "Amadeus dice en voz alta un comentario breve (se encola, no bloquea; con límite de frecuencia).",
     "inputSchema": {"type": "object", "properties": {"texto": {"type": "string"}}, "required": ["texto"]}},
]
_NOMBRES_HERRAMIENTAS: Final = frozenset(h["name"] for h in HERRAMIENTAS)

# Logger propio. El NullHandler evita el "lastResort" de logging, que escribiría en stderr.
log = logging.getLogger("rams.mcp_puente")
log.addHandler(logging.NullHandler())

# Argumentos de la línea de órdenes de este proceso (los fija main(); vacío en tests).
_ARGV: tuple[str, ...] = ()

_NIVELES_ES: Final = {logging.DEBUG: "DEPURACIÓN", logging.INFO: "INFO", logging.WARNING: "AVISO",
                      logging.ERROR: "ERROR", logging.CRITICAL: "CRÍTICO"}


# ---------------------------------------------------------------------------
# Logging a archivo (nunca a stderr)
# ---------------------------------------------------------------------------


class _FormatoEs(logging.Formatter):
    """Formato con los nombres de nivel en español (§3.3)."""

    def format(self, record: logging.LogRecord) -> str:
        record.nivel_es = _NIVELES_ES.get(record.levelno, record.levelname)
        return super().format(record)


def _ruta_log() -> Path:
    """Ruta del log del puente según la plataforma."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "rams_biocore" / "mcp_bridge.log"
    return Path.home() / ".cache" / "rams_biocore" / "mcp_bridge.log"


def _configurar_log() -> None:
    """Log rotativo pequeño (256 KiB × 1) con permisos 600; si falla, ``NullHandler``."""
    _cerrar_log()
    log.propagate = False
    log.setLevel(logging.INFO)
    manejador: logging.Handler
    aviso: str | None = None
    try:
        ruta = _ruta_log()
        ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        manejador = logging.handlers.RotatingFileHandler(ruta, maxBytes=262144, backupCount=1, encoding="utf-8")
        manejador.setFormatter(_FormatoEs("%(asctime)s %(nivel_es)s %(message)s"))
        if os.name == "posix":
            try:
                os.chmod(ruta, 0o600)
            except OSError as exc:
                aviso = f"No se pudo aplicar chmod 600 al log ({type(exc).__name__})"
    except (OSError, ValueError, RuntimeError):
        manejador = logging.NullHandler()   # sin archivo de log: el puente funciona igual, en silencio
    log.addHandler(manejador)
    if aviso:
        log.debug(aviso)


def _cerrar_log() -> None:
    """Quita y cierra los manejadores del logger del puente (deja un ``NullHandler``)."""
    for manejador in list(log.handlers):
        log.removeHandler(manejador)
        try:
            manejador.close()
        except (OSError, ValueError) as exc:  # archivo de log ya roto: se registra en lo que quede
            log.debug("No se pudo cerrar un manejador de log (%s)", type(exc).__name__)
    log.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Configuración: argv > os.environ > .env del proyecto > valores por defecto
# ---------------------------------------------------------------------------

_RE_LINEA_ENV: Final = re.compile(r"^\s*(?:export\s+)?(?P<clave>[A-Za-z_][A-Za-z0-9_.\-]*)\s*=(?P<resto>.*)$")
_RE_COMENTARIO: Final = re.compile(r"\s+#.*$")
_RE_ESCAPE_DOBLE: Final = re.compile(r"\\([\\'\"abfnrtv])")
_RE_ESCAPE_SIMPLE: Final = re.compile(r"\\([\\'])")
_ESCAPES: Final = {"\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b", "f": "\f",
                   "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_RE_XDG: Final = re.compile(r"\$\{XDG_RUNTIME_DIR\}|\$XDG_RUNTIME_DIR(?![A-Za-z0-9_])")


def _base_proyecto() -> Path:
    """Raíz del proyecto (directorio padre de ``lobes/``)."""
    return Path(__file__).resolve().parent.parent


def _buscar_cierre(cuerpo: str, comilla: str) -> int | None:
    """Índice de la comilla de cierre no escapada en ``cuerpo`` (o ``None``)."""
    i = 0
    while i < len(cuerpo):
        if cuerpo[i] == "\\":
            i += 2
            continue
        if cuerpo[i] == comilla:
            return i
        i += 1
    return None


def _parsear_env(texto: str) -> dict[str, str]:
    """Parser mínimo de ``.env`` con las reglas de §4.1 (imita a python-dotenv).

    Ignora líneas vacías y comentarios, acepta ``export``, toma literal lo que va entre
    comillas, quita el comentario en línea de los valores sin comillas y un valor que empieza
    por ``#`` se trata como vacío. No expande variables.
    """
    resultado: dict[str, str] = {}
    for linea in texto.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        limpia = linea.strip().lstrip("﻿")
        if not limpia or limpia.startswith("#"):
            continue
        coincidencia = _RE_LINEA_ENV.match(linea.lstrip("﻿"))
        if coincidencia is None:
            continue
        clave = coincidencia.group("clave")
        resto = coincidencia.group("resto").strip()
        if resto[:1] in ("'", '"'):
            comilla = resto[0]
            cuerpo = resto[1:]
            cierre = _buscar_cierre(cuerpo, comilla)
            if cierre is None:
                continue  # comillas sin cerrar: se ignora la variable
            valor = cuerpo[:cierre]
            if comilla == '"':
                valor = _RE_ESCAPE_DOBLE.sub(lambda m: _ESCAPES[m.group(1)], valor)
            else:
                valor = _RE_ESCAPE_SIMPLE.sub(lambda m: m.group(1), valor)
            resultado[clave] = valor
        else:
            valor = _RE_COMENTARIO.sub("", resto).strip()
            resultado[clave] = "" if valor.startswith("#") else valor
    return resultado


def _leer_env(ruta: Path) -> dict[str, str]:
    """Lee un ``.env``; inexistente o ilegible ⇒ ``{}``."""
    try:
        return _parsear_env(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("Sin .env legible en %s (%s)", ruta, type(exc).__name__)
        return {}


def _valor_util(valor: str | None) -> str | None:
    """Regla única de ausencia: ausente, vacío, en blanco o empieza por ``#`` ⇒ ``None``."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.startswith("#"):
        return None
    return texto


def _puerto(valor: str | None, origen: str) -> int | None:
    texto = _valor_util(valor)
    if texto is None:
        return None
    try:
        numero = int(texto, 10)
    except ValueError:
        numero = -1
    if 1 <= numero <= 65535:
        return numero
    log.warning("Puerto inválido en %s; se ignora", origen)
    return None


def _ruta(valor: str | None, origen: str, base: Path) -> Path | None:
    """Ruta con ``~`` y ``${XDG_RUNTIME_DIR}`` expandidos; relativa ⇒ respecto a la raíz del proyecto."""
    texto = _valor_util(valor)
    if texto is None:
        return None
    if "\x00" in texto:
        log.warning("Ruta inválida en %s; se ignora", origen)
        return None
    if _RE_XDG.search(texto):
        xdg = _valor_util(os.environ.get("XDG_RUNTIME_DIR"))
        if xdg is None:
            log.warning("%s usa XDG_RUNTIME_DIR, que no está definida; se ignora", origen)
            return None
        texto = _RE_XDG.sub(lambda _m: xdg, texto)
    if texto == "~":
        ruta = Path.home()
    elif texto.startswith("~/") or texto.startswith("~\\"):
        ruta = Path.home() / texto[2:]
    else:
        ruta = Path(texto)
    if not ruta.anchor:
        ruta = base / ruta
    return ruta


def _argumentos(argv: Sequence[str]) -> tuple[str | None, str | None]:
    """Extrae ``--puerto`` y ``--token-file`` (formas ``--x V`` y ``--x=V``; gana la última)."""
    puerto: str | None = None
    token_file: str | None = None
    elementos = [str(a) for a in argv]
    i = 0
    while i < len(elementos):
        actual = elementos[i]
        for nombre in ("--puerto", "--token-file"):
            valor: str | None = None
            if actual == nombre:
                if i + 1 < len(elementos):
                    valor = elementos[i + 1]
                    i += 1
            elif actual.startswith(nombre + "="):
                valor = actual[len(nombre) + 1:]
            else:
                continue
            if nombre == "--puerto":
                puerto = valor
            else:
                token_file = valor
            break
        else:
            log.debug("Argumento desconocido ignorado: %r", actual[:40])
        i += 1
    return puerto, token_file


def resolver_config(argv: Sequence[str]) -> tuple[int, Path]:
    """Puerto y ruta del token del socket de control, con esta precedencia (por cada dato):

    1. ``--puerto N`` / ``--token-file RUTA`` en ``argv``.
    2. ``os.environ`` (``RAMS_CTL_PORT``, ``RAMS_CTL_TOKEN_FILE``).
    3. El ``.env`` del proyecto (``Path(__file__).resolve().parent.parent / ".env"``).
    4. Valores por defecto: ``8766`` y ``~/.config/rams_biocore/ctl_token``.

    Un valor inválido en un nivel se ignora (con aviso) y se pasa al siguiente. Nunca lanza.
    """
    base = _base_proyecto()
    arg_puerto, arg_token = _argumentos(argv)
    cache_env: dict[str, str] | None = None

    def del_archivo(clave: str) -> str | None:
        nonlocal cache_env
        if cache_env is None:
            cache_env = _leer_env(base / ".env")
        return cache_env.get(clave)

    puerto = _puerto(arg_puerto, "--puerto")
    if puerto is None:
        puerto = _puerto(os.environ.get("RAMS_CTL_PORT"), "RAMS_CTL_PORT del entorno")
    if puerto is None:
        puerto = _puerto(del_archivo("RAMS_CTL_PORT"), "RAMS_CTL_PORT del .env")
    if puerto is None:
        puerto = PUERTO_DEFECTO

    ruta = _ruta(arg_token, "--token-file", base)
    if ruta is None:
        ruta = _ruta(os.environ.get("RAMS_CTL_TOKEN_FILE"), "RAMS_CTL_TOKEN_FILE del entorno", base)
    if ruta is None:
        ruta = _ruta(del_archivo("RAMS_CTL_TOKEN_FILE"), "RAMS_CTL_TOKEN_FILE del .env", base)
    if ruta is None:
        ruta = Path.home() / ".config" / "rams_biocore" / "ctl_token"
    return puerto, ruta


# ---------------------------------------------------------------------------
# Comunicación con R.A.M.S.
# ---------------------------------------------------------------------------


def _sanear(texto: Any) -> str:
    """``[``→``⟦``, ``]``→``⟧`` y como mucho 8000 caracteres."""
    limpio = "" if texto is None else str(texto)
    return limpio.replace("[", "⟦").replace("]", "⟧")[:MAX_TEXTO]


def preguntar_a_rams(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Reenvía una herramienta al socket de control de R.A.M.S. y devuelve ``(ok, texto)``.

    Una línea JSON ``{"token","tool","args"}`` por ``socket.create_connection(("127.0.0.1",
    puerto), timeout=12)`` y una línea JSON ``{"ok","text"}`` de vuelta. Nunca lanza:
    sin token ⇒ :data:`MSG_SIN_TOKEN`; sin conexión ⇒ :data:`MSG_SIN_CONEXION`.
    """
    puerto, ruta_token = resolver_config(_ARGV)
    try:
        token = ruta_token.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        log.info("No se pudo leer el token de control (%s)", type(exc).__name__)
        return False, MSG_SIN_TOKEN
    if not token:
        return False, MSG_SIN_TOKEN
    argumentos = args if isinstance(args, dict) else {}
    try:
        peticion = json.dumps({"token": token, "tool": str(tool), "args": argumentos},
                              ensure_ascii=False, default=str) + "\n"
    except (TypeError, ValueError) as exc:
        log.warning("Argumentos no serializables para %s (%s)", str(tool)[:40], type(exc).__name__)
        return False, MSG_ERROR_INTERNO
    limite = time.monotonic() + TIEMPO_ESPERA_S
    try:
        conexion = socket.create_connection(("127.0.0.1", puerto), timeout=TIEMPO_ESPERA_S)
    except OSError as exc:
        log.info("Sin conexión con R.A.M.S. en 127.0.0.1:%d (%s)", puerto, type(exc).__name__)
        return False, MSG_SIN_CONEXION
    buffer = bytearray()
    try:
        with conexion:
            conexion.sendall(peticion.encode("utf-8", "replace"))
            while b"\n" not in buffer and len(buffer) < _MAX_RESPUESTA_BYTES:
                restante = limite - time.monotonic()
                if restante <= 0:
                    raise TimeoutError("plazo agotado")
                conexion.settimeout(restante)
                trozo = conexion.recv(65536)
                if not trozo:
                    break
                buffer += trozo
    except TimeoutError:
        log.info("R.A.M.S. no respondió en %.0f s a %s", TIEMPO_ESPERA_S, str(tool)[:40])
        return False, MSG_SIN_RESPUESTA
    except OSError as exc:
        log.info("Conexión con R.A.M.S. interrumpida (%s)", type(exc).__name__)
        return False, MSG_SIN_CONEXION
    linea = bytes(buffer).split(b"\n", 1)[0]
    try:
        respuesta = json.loads(linea.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        log.info("Respuesta inválida de R.A.M.S. (%d bytes)", len(linea))
        return False, MSG_RESPUESTA_INVALIDA
    if not isinstance(respuesta, dict):
        return False, MSG_RESPUESTA_INVALIDA
    texto = respuesta.get("text", "")
    return bool(respuesta.get("ok")), "" if texto is None else str(texto)


# ---------------------------------------------------------------------------
# JSON-RPC (MCP)
# ---------------------------------------------------------------------------


def _resultado(id_: Any, resultado: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": resultado}


def _error(id_: Any, codigo: int, mensaje: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensaje}}


def _contenido(texto: str, es_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": _sanear(texto)}], "isError": bool(es_error)}


def procesar_mensaje(req: dict[str, Any]) -> dict[str, Any] | None:
    """Respuesta JSON-RPC para ``req`` o ``None`` (notificaciones, que no se responden).

    - ``initialize`` → versión de protocolo, capacidades (``tools``) e info del servidor.
    - ``tools/list`` → las 4 herramientas.
    - ``tools/call`` → reenvío a R.A.M.S.; ``{"content": [{"type":"text","text": saneado}],
      "isError": not ok}``.
    - ``ping`` → ``{}``.
    - Método desconocido con ``id`` → error ``-32601``.
    """
    if not isinstance(req, dict):
        return None
    if req.get("id") is None:
        return None  # notificación (p. ej. notifications/initialized)
    id_ = req.get("id")
    metodo = req.get("method")
    if metodo == "initialize":
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        cliente = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        log.info("Handshake MCP con %s", str(cliente.get("name") or "cliente desconocido")[:60])
        return _resultado(id_, {"protocolVersion": PROTO, "capabilities": {"tools": {}},
                                "serverInfo": {"name": "rams-biocore", "version": VERSION_SERVIDOR}})
    if metodo == "tools/list":
        return _resultado(id_, {"tools": copy.deepcopy(HERRAMIENTAS)})
    if metodo == "tools/call":
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        nombre = params.get("name")
        argumentos = params.get("arguments")
        if not isinstance(argumentos, dict):
            argumentos = {}
        if not isinstance(nombre, str) or nombre not in _NOMBRES_HERRAMIENTAS:
            log.info("tools/call con herramienta desconocida")
            return _resultado(id_, _contenido(f"Herramienta desconocida: {str(nombre)[:60]}", True))
        try:
            ok, texto = preguntar_a_rams(nombre, argumentos)
        except Exception as exc:  # preguntar_a_rams no lanza; red de seguridad
            log.warning("Fallo inesperado al reenviar %s (%s)", nombre, type(exc).__name__)
            ok, texto = False, MSG_ERROR_INTERNO
        log.info("tools/call %s → %s (%d caracteres)", nombre, "ok" if ok else "error", len(texto))
        return _resultado(id_, _contenido(texto, not ok))
    if metodo == "ping":
        return _resultado(id_, {})
    log.info("Método MCP no soportado: %s", str(metodo)[:60])
    return _error(id_, -32601, f"Método no soportado: {str(metodo)[:60]}")


def _bucle(entrada: TextIO, salida: TextIO) -> int:
    """Lee mensajes línea a línea hasta EOF y escribe cada respuesta con ``flush``."""
    while True:
        try:
            linea = entrada.readline()
        except (OSError, ValueError) as exc:
            log.warning("No se pudo leer stdin (%s); el puente termina", type(exc).__name__)
            return 0
        if not linea:
            log.info("EOF en stdin: Nexus cerró el puente")
            return 0
        linea = linea.strip()
        if not linea:
            continue
        try:
            req = json.loads(linea)
        except ValueError:
            log.debug("Línea no JSON ignorada (%d caracteres)", len(linea))
            continue
        try:
            respuesta = procesar_mensaje(req)
        except Exception:  # red de seguridad: el puente nunca muere por un mensaje
            log.exception("Error al procesar un mensaje MCP")
            id_ = req.get("id") if isinstance(req, dict) else None
            respuesta = _error(id_, -32603, MSG_ERROR_INTERNO) if id_ is not None else None
        if respuesta is None:
            continue
        try:
            salida.write(json.dumps(respuesta, ensure_ascii=False) + "\n")
            salida.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            log.info("stdout cerrado (%s); el puente termina", type(exc).__name__)
            return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: stdin/stdout en UTF-8, bucle ``readline`` y salida 0 en EOF."""
    global _ARGV
    _ARGV = tuple(sys.argv[1:] if argv is None else argv)
    stderr_original = sys.stderr
    lanzar_original = logging.raiseExceptions
    sumidero: TextIO | None = None
    try:
        sumidero = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - se cierra en finally
        sys.stderr = sumidero
    except OSError:
        sumidero = None
    logging.raiseExceptions = False  # un fallo del log nunca se imprime en stderr
    try:
        _configurar_log()
        for flujo, opciones in ((sys.stdin, {"encoding": "utf-8", "errors": "replace"}),
                                (sys.stdout, {"encoding": "utf-8", "errors": "replace", "newline": "\n"})):
            try:
                flujo.reconfigure(**opciones)  # type: ignore[union-attr]
            except (AttributeError, ValueError, OSError) as exc:
                log.debug("No se pudo reconfigurar un flujo estándar (%s)", type(exc).__name__)
        puerto, _ruta_token = resolver_config(_ARGV)
        log.info("Puente MCP iniciado (pid %d, socket de control 127.0.0.1:%d)", os.getpid(), puerto)
        return _bucle(sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        return 0
    except Exception:  # nunca una traza en stderr: se registra en el archivo
        log.exception("Error fatal del puente MCP")
        return 1
    finally:
        log.info("Puente MCP terminado")
        _cerrar_log()
        log.propagate = True
        logging.raiseExceptions = lanzar_original
        sys.stderr = stderr_original
        if sumidero is not None:
            try:
                sumidero.close()
            except OSError:
                log.debug("No se pudo cerrar os.devnull")


if __name__ == "__main__":
    sys.exit(main())
