"""Socket de control local de R.A.M.S. BIO-CORE para el puente MCP de Nexus IDE.

Qué hace (docs/ARQUITECTURA.md §10.13 y §9.4):
- Escucha **solo** en ``127.0.0.1`` (nunca ``0.0.0.0``), por defecto en el puerto 8766.
- Protocolo por conexión: el cliente (``lobes/nexus_mcp_bridge.py``) envía **una** línea JSON
  ``{"token": str, "tool": str, "args": object}``; el servidor responde **una** línea JSON
  ``{"ok": bool, "text": str}`` y cierra.
- Autentica con el token del archivo ``ctl_token`` (32 hex, permisos 600) comparado con
  ``hmac.compare_digest``; solo acepta las herramientas de :data:`HERRAMIENTAS_PERMITIDAS` y
  aplica un intervalo mínimo por herramienta (:data:`INTERVALO_MIN_S`).
- El texto de toda respuesta pasa por :func:`sanear_para_nexus` (``[``→``⟦``, ``]``→``⟧``):
  así un resultado nunca forma etiquetas ejecutables de Nexus (``[RUN_COMMAND]``, ``[MCP: …]``).

Este módulo no decide **qué** se responde: lo hace el despachador que le pasa ``main.py``
(que aplica ``RAMS_MCP_MEMORIA``, el tope de 120 caracteres de ``investigar_web``, etc.).

Dependencias: solo la biblioteca estándar y ``core.config`` / ``core.contracts``.

Degradación: con ``RAMS_CTL_ENABLED=0`` :meth:`ServidorControl.iniciar` devuelve ``OFF``; si el
puerto está ocupado, ``SIN CONEXIÓN`` ("puerto 8766 ocupado"); si no se puede crear el token,
``DEGRADADO``. Ningún error de una conexión afecta al resto del sistema.
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import inspect
import json
import logging
import math
import os
import re
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo

__all__ = [
    "HERRAMIENTAS_PERMITIDAS",
    "INTERVALO_MIN_S",
    "Despachador",
    "HOST_CONTROL",
    "LIMITE_LINEA",
    "token_control",
    "sanear_para_nexus",
    "ServidorControl",
]

log = logging.getLogger("rams.control")

HERRAMIENTAS_PERMITIDAS: Final = ("estado_homeostatico", "memoria_buscar", "investigar_web", "amadeus_comentar")
INTERVALO_MIN_S: Final = {"amadeus_comentar": 30.0, "investigar_web": 5.0, "memoria_buscar": 1.0, "estado_homeostatico": 0.5}
Despachador = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]]

HOST_CONTROL: Final = "127.0.0.1"   # nunca 0.0.0.0 (§9.4)
LIMITE_LINEA: Final = 65536         # 64 KiB por línea de petición

# Plazos (segundos). Son atributos del módulo para que los tests puedan acortarlos.
_TIEMPO_LECTURA_S: float = 5.0      # lectura de la línea de petición
_TIEMPO_DESPACHO_S: float = 10.0    # despachador de main (por debajo de los 12 s del puente)
_TIEMPO_ESCRITURA_S: float = 5.0    # envío de la respuesta
_TIEMPO_CIERRE_S: float = 2.0       # cierre del servidor y de las conexiones

_MAX_CONEXIONES: Final = 16         # conexiones simultáneas (solo hay un puente por sesión de Nexus)
_NOMBRE_DIAGNOSTICO: Final = "Socket de control (MCP)"

# Token aceptado al leer el archivo (el que se genera es de 32 hex; se admite uno propio razonable).
_RE_TOKEN_VALIDO: Final = re.compile(r"^[A-Za-z0-9_\-]{16,256}$")

# Errores de bind que significan "puerto ocupado" (POSIX y Winsock).
_ERRNOS_OCUPADO: Final = frozenset({errno.EADDRINUSE, 10048})        # 10048 = WSAEADDRINUSE
_ERRNOS_OCUPADO_WINDOWS: Final = frozenset({errno.EACCES, 10013})    # puerto reservado/exclusivo en Windows

_MSG_INVALIDA: Final = "petición inválida"
_MSG_TOKEN: Final = "token inválido"
_MSG_NO_PERMITIDA: Final = "herramienta no permitida"
_MSG_TIEMPO: Final = "tiempo agotado"
_MSG_ERROR_INTERNO: Final = "error interno al atender la herramienta"
_MSG_SATURADO: Final = "Demasiadas conexiones simultáneas; inténtalo de nuevo en unos segundos."


# ---------------------------------------------------------------------------
# Utilidades públicas
# ---------------------------------------------------------------------------


def _chmod_privado(ruta: Path) -> None:
    """Deja ``ruta`` en 0o600 (POSIX); en Windows no hace nada (§3.4)."""
    if os.name != "posix":
        return
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:
        log.debug("No se pudo aplicar chmod 600 a %s: %s", ruta, exc.strerror or exc)


def _escribir_privado(ruta: Path, contenido: str) -> None:
    """Escritura atómica con permisos 0o600 desde la creación (temporal + ``os.replace``)."""
    temporal = ruta.with_name(ruta.name + ".tmp")
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
            archivo.write(contenido)
    except BaseException:
        try:
            temporal.unlink()
        except OSError as exc:
            log.debug("No se pudo borrar el temporal %s: %s", temporal, exc)
        raise
    os.replace(temporal, ruta)
    _chmod_privado(ruta)


def token_control(ruta: Path) -> str:
    """Lee el token del socket de control o lo crea con ``secrets.token_hex(16)``.

    - Si ``ruta`` existe y contiene un token válido, lo devuelve (y reaplica ``chmod 600``).
    - Si no existe, está vacío o tiene un formato inválido, genera uno nuevo, crea el
      directorio con ``parents=True`` (0o700) y lo escribe de forma atómica con permisos 600.

    El token **nunca** se registra en el log. Lanza ``OSError`` si no se puede leer ni crear
    (el llamador, :meth:`ServidorControl.iniciar`, lo convierte en un diagnóstico degradado).
    """
    ruta = Path(ruta)
    existente = ""
    try:
        existente = ruta.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existente = ""
    except UnicodeDecodeError:
        log.warning("El archivo del token de control %s no es texto UTF-8; se regenera", ruta)
        existente = ""
    if existente and _RE_TOKEN_VALIDO.match(existente):
        _chmod_privado(ruta)
        return existente
    if existente:
        log.warning("El token de control de %s no tiene un formato válido; se genera uno nuevo", ruta)
    token = secrets.token_hex(16)
    ruta.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _escribir_privado(ruta, token + "\n")
    log.info("Token de control creado en %s (permisos 600)", ruta)
    return token


def sanear_para_nexus(texto: str, max_chars: int = 8000) -> str:
    """Prepara un texto para el agente de Nexus IDE.

    Sustituye ``[`` → ``⟦`` y ``]`` → ``⟧`` (así nunca forma etiquetas ejecutables de Nexus como
    ``[RUN_COMMAND]`` o ``[MCP: …]``, §9.5) y trunca a ``max_chars`` caracteres (el último pasa
    a ser ``…`` cuando se trunca). Nunca lanza.
    """
    if texto is None:
        texto = ""
    elif not isinstance(texto, str):
        texto = str(texto)
    texto = texto.replace("[", "⟦").replace("]", "⟧")
    try:
        limite = int(max_chars)
    except (TypeError, ValueError):
        limite = 8000
    if limite <= 0:
        return ""
    if len(texto) > limite:
        texto = texto[: limite - 1] + "…"
    return texto


def _es_puerto_ocupado(exc: OSError) -> bool:
    """``True`` si el error de ``bind`` indica que el puerto ya está en uso."""
    codigos = {getattr(exc, "errno", None), getattr(exc, "winerror", None)}
    if codigos & _ERRNOS_OCUPADO:
        return True
    return sys.platform == "win32" and bool(codigos & _ERRNOS_OCUPADO_WINDOWS)


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------


class ServidorControl:
    """Servidor TCP local (una línea JSON de petición, una de respuesta) para el puente MCP.

    ``despachar(tool, args)`` es una corrutina de ``main.py`` que devuelve ``(ok, texto)``.
    Este servidor solo autentica, filtra, limita la frecuencia, aplica los plazos y sanea.
    """

    def __init__(self, settings: Settings, despachar: Despachador, *, puerto: int | None = None) -> None:
        self.settings = settings
        self._despachar = despachar
        try:
            self._puerto = int(settings.ctl_puerto if puerto is None else puerto)
        except (TypeError, ValueError):
            log.warning("Puerto de control inválido; uso 8766")
            self._puerto = 8766
        self.puerto_real: int | None = None
        self._servidor: asyncio.Server | None = None
        self._token: bytes | None = None
        self._ultimo_uso: dict[str, float] = {}
        self._conexiones: set[asyncio.Task[Any]] = set()
        self._reloj: Callable[[], float] = time.monotonic   # inyectable en tests

    # --- ciclo de vida ---

    def _diagnostico(self, estado: EstadoModulo, detalle: str) -> DiagnosticoModulo:
        return DiagnosticoModulo(clave="control", nombre=_NOMBRE_DIAGNOSTICO, estado=estado, detalle=detalle)

    @property
    def activo(self) -> bool:
        """``True`` mientras el servidor está escuchando."""
        return self._servidor is not None

    async def iniciar(self) -> DiagnosticoModulo:
        """Abre el socket en ``127.0.0.1`` y devuelve el diagnóstico (clave ``"control"``).

        - ``settings.ctl_habilitado`` en ``False`` ⇒ ``OFF`` ("deshabilitado").
        - Token imposible de leer o crear ⇒ ``DEGRADADO``.
        - Puerto ocupado ⇒ ``SIN CONEXIÓN`` ("puerto N ocupado").
        - Con ``puerto=0`` (tests), :attr:`puerto_real` es el puerto asignado.

        Es idempotente y nunca lanza.
        """
        if not self.settings.ctl_habilitado:
            log.info("Socket de control deshabilitado (RAMS_CTL_ENABLED=0)")
            return self._diagnostico(EstadoModulo.OFF, "deshabilitado")
        if self._servidor is not None:
            return self._diagnostico(EstadoModulo.OK, f"{HOST_CONTROL}:{self.puerto_real}")
        try:
            token = token_control(self.settings.ctl_token_file)
        except OSError as exc:
            log.warning("No se pudo leer ni crear el token de control (%s)",
                        exc.strerror or type(exc).__name__)
            return self._diagnostico(EstadoModulo.DEGRADADO, "no se pudo crear el token de control")
        self._token = token.encode("utf-8")
        try:
            servidor = await asyncio.start_server(
                self._atender, host=HOST_CONTROL, port=self._puerto, limit=LIMITE_LINEA)
        except OSError as exc:
            if _es_puerto_ocupado(exc):
                log.warning("El puerto de control %d está ocupado; el puente MCP no podrá conectar",
                            self._puerto)
                return self._diagnostico(EstadoModulo.SIN_CONEXION, f"puerto {self._puerto} ocupado")
            log.warning("No se pudo abrir el socket de control en %s:%d (%s)",
                        HOST_CONTROL, self._puerto, exc.strerror or type(exc).__name__)
            return self._diagnostico(EstadoModulo.SIN_CONEXION,
                                     f"no se pudo abrir {HOST_CONTROL}:{self._puerto}")
        self._servidor = servidor
        puerto_real = self._puerto
        for sock in servidor.sockets or ():
            try:
                puerto_real = int(sock.getsockname()[1])
                break
            except (OSError, IndexError, TypeError, ValueError) as exc:
                log.debug("No se pudo leer el puerto asignado: %s", exc)
        self.puerto_real = puerto_real
        log.info("Socket de control escuchando en %s:%d", HOST_CONTROL, puerto_real)
        return self._diagnostico(EstadoModulo.OK, f"{HOST_CONTROL}:{puerto_real}")

    async def cerrar(self) -> None:
        """Deja de escuchar, cancela las conexiones en curso y espera el cierre (≤ 2 s). Idempotente."""
        servidor, self._servidor = self._servidor, None
        if servidor is None:
            return
        servidor.close()
        pendientes = [t for t in list(self._conexiones) if not t.done()]
        for tarea in pendientes:
            tarea.cancel()
        if pendientes:
            await asyncio.wait(pendientes, timeout=_TIEMPO_CIERRE_S)
        try:
            await asyncio.wait_for(servidor.wait_closed(), _TIEMPO_CIERRE_S)
        except TimeoutError:
            log.debug("El socket de control tardó en cerrarse; se continúa")
        except Exception as exc:  # cierre de un servidor ya roto: no debe impedir el apagado
            log.debug("Error al cerrar el socket de control: %s", exc)
        self._conexiones.clear()
        log.info("Socket de control cerrado")

    # --- atención de conexiones ---

    async def _atender(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Atiende una conexión: lee una línea, responde una línea y cierra."""
        tarea = asyncio.current_task()
        if tarea is not None:
            self._conexiones.add(tarea)
            tarea.add_done_callback(self._conexiones.discard)
        inicio = self._reloj()
        try:
            if len(self._conexiones) > _MAX_CONEXIONES:
                log.warning("Socket de control saturado (%d conexiones); se rechaza una", len(self._conexiones))
                ok, texto = False, _MSG_SATURADO
            else:
                ok, texto = await self._procesar(reader)
            await self._responder(writer, ok, texto)
            log.debug("Petición de control atendida en %.0f ms (ok=%s)", (self._reloj() - inicio) * 1000, ok)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # una conexión rota nunca afecta al servidor
            log.debug("Conexión de control interrumpida (%s): %s", type(exc).__name__, exc)
        finally:
            await self._cerrar_conexion(writer)

    async def _procesar(self, reader: asyncio.StreamReader) -> tuple[bool, str]:
        """Valida la petición y la despacha. Devuelve ``(ok, texto)`` sin sanear."""
        try:
            linea = await asyncio.wait_for(reader.readline(), _TIEMPO_LECTURA_S)
        except TimeoutError:
            log.debug("Petición de control incompleta tras %.0f s", _TIEMPO_LECTURA_S)
            return False, _MSG_INVALIDA
        except (ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
            log.warning("Petición de control demasiado larga o truncada (%s)", type(exc).__name__)
            return False, _MSG_INVALIDA
        if not linea.strip():
            return False, _MSG_INVALIDA
        try:
            peticion = json.loads(linea.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            log.debug("Petición de control con JSON inválido (%d bytes)", len(linea))
            return False, _MSG_INVALIDA
        if not isinstance(peticion, dict):
            return False, _MSG_INVALIDA
        token = peticion.get("token")
        herramienta = peticion.get("tool")
        argumentos = peticion.get("args")
        if argumentos is None:
            argumentos = {}
        if not isinstance(token, str) or not isinstance(herramienta, str) or not isinstance(argumentos, dict):
            return False, _MSG_INVALIDA

        if not self._token_correcto(token):
            log.warning("Petición al socket de control con token inválido; se rechaza")
            return False, _MSG_TOKEN
        if herramienta not in HERRAMIENTAS_PERMITIDAS:
            log.info("Herramienta no permitida pedida por el socket de control: %r", herramienta[:40])
            return False, _MSG_NO_PERMITIDA

        espera = self._espera_restante(herramienta)
        if espera > 0:
            segundos = max(1, math.ceil(espera))
            log.debug("Límite de frecuencia para %s: faltan %d s", herramienta, segundos)
            return False, f"Demasiadas solicitudes; espera {segundos} s."
        self._ultimo_uso[herramienta] = self._reloj()
        return await self._ejecutar(herramienta, argumentos)

    def _token_correcto(self, token: str) -> bool:
        """Compara en tiempo constante con el token del archivo."""
        if self._token is None:
            return False
        try:
            recibido = token.encode("utf-8", "surrogatepass")
        except UnicodeError:
            return False
        return hmac.compare_digest(recibido, self._token)

    def _espera_restante(self, herramienta: str) -> float:
        """Segundos que faltan para poder volver a usar ``herramienta`` (0 si ya se puede)."""
        ultimo = self._ultimo_uso.get(herramienta)
        if ultimo is None:
            return 0.0
        intervalo = float(INTERVALO_MIN_S.get(herramienta, 1.0))
        return max(0.0, intervalo - (self._reloj() - ultimo))

    async def _ejecutar(self, herramienta: str, argumentos: dict[str, Any]) -> tuple[bool, str]:
        """Llama al despachador con plazo de 10 s y normaliza su resultado."""
        try:
            resultado: Any = self._despachar(herramienta, argumentos)
            if inspect.isawaitable(resultado):
                resultado = await asyncio.wait_for(resultado, _TIEMPO_DESPACHO_S)
        except TimeoutError:
            log.warning("La herramienta %s superó los %.0f s de despacho", herramienta, _TIEMPO_DESPACHO_S)
            return False, _MSG_TIEMPO
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # un fallo del despachador nunca tumba el servidor
            log.warning("Error al despachar %s: %s", herramienta, type(exc).__name__,
                        exc_info=log.isEnabledFor(logging.DEBUG))
            return False, _MSG_ERROR_INTERNO
        if isinstance(resultado, (tuple, list)) and len(resultado) == 2:
            ok, texto = resultado
            return bool(ok), "" if texto is None else str(texto)
        log.warning("El despachador devolvió un resultado inválido para %s", herramienta)
        return False, _MSG_ERROR_INTERNO

    async def _responder(self, writer: asyncio.StreamWriter, ok: bool, texto: str) -> None:
        """Escribe la línea JSON de respuesta (texto siempre saneado)."""
        cuerpo = json.dumps({"ok": bool(ok), "text": sanear_para_nexus(texto)}, ensure_ascii=False)
        writer.write((cuerpo + "\n").encode("utf-8", "replace"))
        await asyncio.wait_for(writer.drain(), _TIEMPO_ESCRITURA_S)

    async def _cerrar_conexion(self, writer: asyncio.StreamWriter) -> None:
        """Cierra el *socket* del cliente sin propagar errores de red."""
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # cliente que ya cerró o reseteó la conexión
            log.debug("Cierre de conexión de control con error (%s)", type(exc).__name__)
