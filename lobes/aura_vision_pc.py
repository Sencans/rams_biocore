"""Lóbulo sensorimotor del PC de R.A.M.S. BIO-CORE (§10.17, §9.1, §9.2, §6.1).

Qué hace:
- **Visión activa** ("parpadeo"): abre la webcam unos 150–600 ms en un **hilo daemon**
  ``rams-camara``, mira si hay un rostro (Haar o YuNet) y la libera siempre en un ``finally``.
  Ningún fotograma sale de una variable local: nunca se codifica, se guarda ni se graba, y al
  LLM solo llegan ``{"rostro", "n_rostros", "duracion_ms"}``. Canon del episodio 3 de
  *Steins;Gate 0*: Amadeus ve el mundo por la cámara del teléfono, y siempre lo dice.
- **Captura de pantalla** bajo petición explícita del usuario (``grim``, solo Linux/Wayland).
- **Telemetría** del sistema (psutil, ``nvidia-smi``, sysfs) con alertas por flanco en
  formato J.A.R.V.I.S. y refractario de 10 min por tipo.
- **Control de Hyprland** (``ControlHyprland``) en los dialectos Lua (≥ 0.55) y legacy, con
  validación estricta de cada argumento (con Lua, ``hyprctl dispatch`` evalúa código).
- **Ejecución de acciones** (``AccionPC``) a través de la compuerta de permisos, que se llama
  **internamente** en ``ejecutar``, ``vistazo`` y ``capturar_pantalla``.
- **Integración con Nexus IDE**: compone ``NexusLink`` como ``self.nexus`` (opcional).

Dependencias opcionales: ``cv2`` (OpenCV 4.x o 5.x), ``numpy``, ``psutil`` y
``lobes.nexus_link``; binarios ``hyprctl``, ``grim``, ``nvidia-smi``, ``wpctl`` y
``playerctl``. Todos los subprocesos van con ``asyncio.create_subprocess_exec`` (sin shell),
con argumentos validados y con timeout.

Degradación: en Windows Hyprland queda OFF, la cámara usa ``CAP_DSHOW`` y la telemetría usa
psutil; sin cv2 la cámara queda OFF ("OpenCV no instalado"); con OpenCV 5 sin contrib y sin
el modelo YuNet, ``rostro=None`` (DEGRADADO: "sin detector de rostros"); sin psutil la
telemetría queda OFF; sin ``lobes.nexus_link``, ``self.nexus = None``. **Ningún método
público lanza excepciones** (la cancelación de tareas sí se propaga, como debe ser).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import glob
import json
import logging
import os
import shutil
import struct
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

log = logging.getLogger("rams.vision")

try:  # opcional: visión activa y decodificación de la captura de pantalla
    import cv2
except ImportError:  # sin OpenCV: cámara OFF
    cv2 = None
except Exception as _exc_cv2:  # OpenCV roto (p. ej. ABI de numpy incompatible): se trata como ausente
    log.debug("OpenCV no se pudo importar (%s); cámara OFF", type(_exc_cv2).__name__)
    cv2 = None

try:  # opcional: lo usa OpenCV; aquí solo para decodificar la captura
    import numpy as np
except ImportError:  # sin numpy: no se decodifica la captura
    np = None

try:  # opcional: telemetría
    import psutil
except ImportError:  # sin psutil: telemetría OFF
    psutil = None

from core.bus import BusEventos
from core.config import Settings, ahora_local
from core.contracts import (
    AccionPC,
    DiagnosticoModulo,
    EstadoModulo,
    ResultadoAccion,
    ResultadoVistazo,
    Telemetria,
    formatear_decimal,
)
from core.permisos import (
    CLASE_SEGURA_RE,
    DIRECCION_RE,
    CompuertaPermisos,
    calcular_pids_propios,
)

# CONTRATO: lobes.nexus_link (grupo nexus, fase P2) es opcional. Si no se puede importar,
# self.nexus = None y el diagnóstico "nexus" es OFF ("módulo Nexus no instalado").
try:
    from lobes.nexus_link import NexusLink
except ImportError:  # módulo Nexus no instalado
    NexusLink = None
    _ERROR_NEXUS: str | None = "módulo Nexus no instalado"
except Exception as _exc:  # módulo presente pero roto: no debe tumbar la visión
    NexusLink = None
    _ERROR_NEXUS = f"módulo Nexus con errores ({type(_exc).__name__})"
else:
    _ERROR_NEXUS = None

__all__ = [
    "SAFE_CLASS_RE",
    "DIRECCION_RE",
    "SalidaHyprctl",
    "ControlHyprland",
    "AuraVisionPC",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SAFE_CLASS_RE: Final = CLASE_SEGURA_RE          # ^[A-Za-z0-9_.\-]{1,64}$ (mismo patrón que la compuerta)

ORIGEN_BUS: Final = "aura_vision_pc"

# Cámara (§9.2, §10.17).
TIMEOUT_CAMARA_S: Final = 3.0
INTERVALO_USUARIO_S: Final = 5.0
ESCALONES_BACKOFF_S: Final = (300.0, 900.0, 3600.0)   # tras el primero (camara_intervalo_min_s)
CALENTAMIENTO_GRABS: Final = 4
_ANCHO_CAMARA: Final = 640
_ALTO_CAMARA: Final = 480
_XML_HAAR: Final = "haarcascade_frontalface_default.xml"
_DIRS_HAAR_SISTEMA: Final = ("/usr/share/opencv4/haarcascades", "/usr/local/share/opencv4/haarcascades")
_MODELO_YUNET: Final = "face_detection_yunet.onnx"
# Código FourCC de MJPG calculado a mano (evita depender de utilidades de grabación de OpenCV).
_FOURCC_MJPG: Final = sum(ord(c) << (8 * i) for i, c in enumerate("MJPG"))

# Hyprland.
TIMEOUT_HYPRCTL_S: Final = 3.0
# En el diagnóstico de arranque, ``status`` y ``version`` van en paralelo con este plazo:
# incluso con ``hyprctl`` colgado (plazo + 1 s de espera tras matarlo) cabe en el
# presupuesto de ``iniciar()``.
TIMEOUT_HYPRCTL_DIAG_S: Final = 1.5
_CONSULTAS_HYPR: Final = frozenset({"clients", "activewindow", "workspaces", "monitors", "version", "status"})
_VENTANA_MIN_S: Final = 0.3
_RECONEXION_EVENTOS_S: Final = 5.0
_MARCAS_ERROR_DISPATCH: Final = ("error", "invalid", "unknown", "not found", "no such", "couldn't",
                                 "could not", "failed", "no window", "syntax")

# Pantalla.
TIMEOUT_GRIM_S: Final = 5.0
# Tras matar un subproceso por cancelación (apagado), cuánto se espera a que salga para que
# asyncio cierre sus tuberías (cabe en la espera de 3 s del paso 3 del apagado, §6.6).
ESPERA_SALIDA_CANCELADO_S: Final = 2.5
_FIRMA_PNG: Final = b"\x89PNG\r\n\x1a\n"

# Arranque: main envuelve ``iniciar()`` en ``wait_for(…, 4)`` (§6.4). Cada diagnóstico interno
# tiene un plazo MENOR para que uno lento se marque solo a sí mismo como DEGRADADO y no
# arrastre a los demás a "SIN CONEXIÓN".
PLAZO_DIAGNOSTICO_S: Final = 3.5

# Telemetría y alertas.
CACHE_TELEMETRIA_S: Final = 2.0
INTERVALO_TOP_S: Final = 15.0
INTERVALO_NVIDIA_S: Final = 15.0
TIMEOUT_NVIDIA_S: Final = 2.0
REFRACTARIO_ALERTA_S: Final = 600.0
_CHIPS_NO_CPU: Final = ("amdgpu", "radeon", "nouveau", "nvme", "drivetemp", "iwlwifi", "ath", "mt7", "spd")


@dataclass(frozen=True)
class _ReglaAlerta:
    umbral: float
    muestras: int        # muestras seguidas por encima del umbral
    segundos: float      # duración mínima por encima del umbral
    critica: bool


_REGLAS_ALERTA: Final[dict[str, _ReglaAlerta]] = {
    "temperatura_cpu": _ReglaAlerta(90.0, 3, 0.0, True),
    "temperatura_gpu": _ReglaAlerta(88.0, 3, 0.0, True),
    "disco": _ReglaAlerta(95.0, 1, 0.0, True),
    "ram": _ReglaAlerta(92.0, 3, 0.0, False),
    "cpu": _ReglaAlerta(95.0, 1, 60.0, False),
}

_ORDENES_PLAYERCTL: Final = {"play_pause": "play-pause", "siguiente": "next", "anterior": "previous"}
_MENSAJES_MEDIA: Final = {
    "play_pause": "Reproducción pausada o reanudada.",
    "siguiente": "Siguiente pista.",
    "anterior": "Pista anterior.",
}


# ---------------------------------------------------------------------------
# Subprocesos (sin shell, con timeout)
# ---------------------------------------------------------------------------


def _es_linux() -> bool:
    """``True`` en Linux (Hyprland, grim, wpctl y playerctl solo existen ahí)."""
    return sys.platform.startswith("linux")


def _matar(proc: asyncio.subprocess.Process) -> None:
    """Mata un subproceso si sigue vivo (sin lanzar)."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError) as exc:
        log.debug("No se pudo matar el subproceso %s: %s", proc.pid, exc)


async def _esperar_salida(proc: asyncio.subprocess.Process, limite_s: float) -> None:
    """Espera (como mucho ``limite_s``) a que un subproceso ya matado termine.

    Se usa dentro de un ``except CancelledError``: si llega otra cancelación mientras tanto (la
    final de ``asyncio.run``), se sigue esperando hasta el límite, porque cortar aquí dejaría
    el transporte abierto. Quien llama re-lanza la cancelación original al volver.
    """
    if proc.returncode is not None:
        return
    espera = asyncio.ensure_future(proc.wait())
    fin = time.monotonic() + max(0.0, limite_s)
    while not espera.done():
        restante = fin - time.monotonic()
        if restante <= 0:
            log.debug("El subproceso %s no terminó tras matarlo (%.1f s)", proc.pid, limite_s)
            break
        try:
            await asyncio.wait({espera}, timeout=restante)
        except asyncio.CancelledError:
            log.debug("Cancelación repetida mientras termina el subproceso %s; sigo esperando", proc.pid)
    if not espera.done():
        espera.cancel()
    elif not espera.cancelled() and espera.exception() is not None:
        log.debug("Espera del subproceso %s: %s", proc.pid, type(espera.exception()).__name__)


async def _correr(argv: list[str], *, timeout: float,
                  procesos: set[asyncio.subprocess.Process] | None = None) -> tuple[int, bytes, bytes] | None:
    """Ejecuta ``argv`` sin shell y devuelve ``(código, stdout, stderr)`` o ``None`` si falla.

    Si vence ``timeout`` mata el proceso y devuelve ``None``. Mientras corre, el proceso queda
    en ``procesos`` para que ``cerrar()`` pueda matarlo.
    """
    nombre = Path(argv[0]).name if argv else "?"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        log.debug("No se pudo lanzar %s: %s", nombre, exc)
        return None
    if procesos is not None:
        procesos.add(proc)
    try:
        salida, error = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.debug("%s superó el tiempo límite de %s s; se mata", nombre, timeout)
        _matar(proc)
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        return None
    except asyncio.CancelledError:
        # Apagado o cancelación: se mata y se espera a que salga (acotado) para que asyncio
        # cierre sus tuberías antes de cerrar el loop. Sin esta espera, en Windows el transporte
        # del subproceso (p. ej. nvidia-smi a medio leer, que tarda ~0,5–1,5 s en morir) queda
        # abierto y su __del__ ensucia stderr al salir del intérprete.
        _matar(proc)
        await _esperar_salida(proc, ESPERA_SALIDA_CANCELADO_S)
        raise
    except OSError as exc:
        log.debug("Error de E/S con %s: %s", nombre, exc)
        _matar(proc)
        return None
    finally:
        if procesos is not None:
            procesos.discard(proc)
    codigo = proc.returncode if proc.returncode is not None else -1
    return codigo, salida or b"", error or b""


def _decodificar(datos: bytes) -> str:
    """Texto UTF-8 tolerante y sin espacios sobrantes."""
    return datos.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Hyprland
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SalidaHyprctl:
    """Resultado de una invocación de ``hyprctl``."""

    codigo: int
    salida: str
    error: str = ""

    @property
    def texto(self) -> str:
        """La salida estándar o, si está vacía, la de error."""
        return self.salida or self.error


def _despacho_ok(resultado: SalidaHyprctl | None) -> bool:
    """``True`` si un ``hyprctl dispatch`` terminó bien (``ok`` o sin texto de error)."""
    if resultado is None or resultado.codigo != 0:
        return False
    texto = resultado.texto.strip().lower()
    if texto in ("", "ok"):
        return True
    return not any(marca in texto for marca in _MARCAS_ERROR_DISPATCH)


def _es_error_lua(texto: str) -> bool:
    """Un dispatch legacy evaluado como Lua: el texto menciona ``lua`` o ``syntax error``."""
    bajo = texto.lower()
    return "lua" in bajo or "syntax error" in bajo


def _es_error_legacy(texto: str) -> bool:
    """Un dispatch Lua rechazado por hyprlang: ``invalid dispatcher``."""
    return "invalid dispatcher" in texto.lower()


def _valor_para_dialecto(valor: Any) -> str | None:
    """Texto en minúsculas de un valor de ``status``; en las rutas, solo el último componente."""
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, (str, int, float)):
        texto = str(valor).strip().lower()
    elif isinstance(valor, (list, tuple)) and all(isinstance(v, (str, int, float)) for v in valor):
        texto = " ".join(str(v) for v in valor).strip().lower()
    else:
        return None
    if "/" in texto or "\\" in texto:
        partes = [p for p in texto.replace("\\", "/").split("/") if p]
        texto = partes[-1] if partes else ""
    return texto or None


def _candidatos_config(datos: Any, ruta: str = "") -> list[tuple[str, str]]:
    """Pares ``(clave, valor)`` cuya clave contiene ``config`` (búsqueda genérica y recursiva)."""
    encontrados: list[tuple[str, str]] = []
    if isinstance(datos, Mapping):
        for clave, valor in datos.items():
            nombre = f"{ruta}.{clave}" if ruta else str(clave)
            if "config" in str(clave).lower():
                texto = _valor_para_dialecto(valor)
                if texto:
                    encontrados.append((nombre, texto))
                elif isinstance(valor, Mapping):   # p. ej. {"config": {"path": ".../hyprland.lua"}}
                    for subclave, subvalor in valor.items():
                        subtexto = _valor_para_dialecto(subvalor)
                        if subtexto:
                            encontrados.append((f"{nombre}.{subclave}", subtexto))
            if isinstance(valor, (Mapping, list)):
                encontrados.extend(_candidatos_config(valor, nombre))
    elif isinstance(datos, list):
        for i, valor in enumerate(datos[:50]):
            if isinstance(valor, (Mapping, list)):
                encontrados.extend(_candidatos_config(valor, f"{ruta}[{i}]"))
    return encontrados


def _dialecto_desde_status(datos: Any) -> str | None:
    """Dialecto según ``hyprctl -j status`` (Hyprland ≥ 0.55) o ``None`` si no lo informa.

    Busca de forma genérica las claves que contienen ``config``: si algún valor contiene
    ``lua`` ⇒ ``"lua"``; si no, si alguno contiene ``hyprlang`` o ``conf`` ⇒ ``"legacy"``.
    """
    candidatos = _candidatos_config(datos)
    if candidatos:
        log.debug("Hyprland status: claves de configuración %s", candidatos[:10])
    if any("lua" in texto for _, texto in candidatos):
        return "lua"
    if any("hyprlang" in texto or "conf" in texto for _, texto in candidatos):
        return "legacy"
    return None


def _directorio_hypr(his: str) -> Path | None:
    """``$XDG_RUNTIME_DIR/hypr/$HIS`` y, si no existe, ``/tmp/hypr/$HIS`` (Hyprland < 0.40)."""
    candidatos: list[Path] = []
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        candidatos.append(Path(xdg) / "hypr" / his)
    candidatos.append(Path("/tmp/hypr") / his)
    existentes = []
    for candidato in candidatos:
        try:
            if candidato.is_dir():
                existentes.append(candidato)
        except OSError as exc:
            log.debug("No se pudo comprobar %s: %s", candidato, exc)
    for candidato in existentes:
        if (candidato / ".socket.sock").exists() or (candidato / ".socket2.sock").exists():
            return candidato
    return existentes[0] if existentes else None


def _ruta_config_lua() -> Path | None:
    """``$XDG_CONFIG_HOME/hypr/hyprland.lua`` (por defecto ``~/.config/hypr/hyprland.lua``)."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not base:
        try:
            base = str(Path.home() / ".config")
        except (RuntimeError, KeyError, OSError) as exc:
            log.debug("Sin directorio personal para buscar hyprland.lua (%s)", type(exc).__name__)
            return None
    return Path(base) / "hypr" / "hyprland.lua"


def _existe_config_lua() -> bool:
    ruta = _ruta_config_lua()
    try:
        return ruta is not None and ruta.is_file()
    except OSError:
        return False


class ControlHyprland:
    """Control acotado de Hyprland por ``hyprctl`` (implementa ``ProveedorHypr``).

    Cada argumento se valida antes de llegar a ``hyprctl`` (``SAFE_CLASS_RE``,
    ``DIRECCION_RE``, ``1 ≤ n ≤ 10`` y la lista permitida de apps): si algo no cumple, el
    método devuelve ``False`` sin invocar nada. Nunca se usa ``killactive`` ni un cierre sin
    selector de ventana.
    """

    def __init__(self, settings: Settings, *, pids_propios: frozenset[int] = frozenset()) -> None:
        self.settings = settings
        self.pids_propios: frozenset[int] = frozenset(pids_propios)
        modo = str(getattr(settings, "hypr_modo_dispatch", "auto") or "auto").strip().lower()
        self._forzado: str | None = modo if modo in ("lua", "legacy") else None
        self._procesos: set[asyncio.subprocess.Process] = set()
        self._detectado = False
        self._ruta_hyprctl: str | None = None
        self.directorio: Path | None = None
        self.disponible: bool = False
        self.motivo_no_disponible: str = ""
        if not _es_linux():
            self.motivo_no_disponible = "no disponible en este sistema"
        else:
            his = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
            if not his:
                self.motivo_no_disponible = "sin sesión de Hyprland"
            elif "/" in his or his in (".", ".."):
                self.motivo_no_disponible = "firma de instancia de Hyprland inválida"
            else:
                self.directorio = _directorio_hypr(his)
                self._ruta_hyprctl = shutil.which("hyprctl")
                if not self._ruta_hyprctl:
                    self.motivo_no_disponible = "hyprctl no instalado"
                elif self.directorio is None or not (
                        (self.directorio / ".socket.sock").exists()
                        or (self.directorio / ".socket2.sock").exists()):
                    self.motivo_no_disponible = "socket de Hyprland no encontrado"
                else:
                    self.disponible = True
        if self.disponible:
            # Provisional hasta detectar_dialecto(): forzado o según exista hyprland.lua.
            self.dialecto: str = self._forzado or ("lua" if _existe_config_lua() else "legacy")
        else:
            self.dialecto = "ninguno"

    # ------------------------------------------------------------------
    # Bajo nivel
    # ------------------------------------------------------------------

    async def _hyprctl(self, *args: str, timeout: float = TIMEOUT_HYPRCTL_S) -> SalidaHyprctl | None:
        """Invoca ``hyprctl`` con ``args`` (sin shell, timeout 3 s). ``None`` si no se pudo."""
        if not self._ruta_hyprctl:
            return None
        resultado = await _correr([self._ruta_hyprctl, *args], timeout=timeout, procesos=self._procesos)
        if resultado is None:
            return None
        codigo, salida, error = resultado
        return SalidaHyprctl(codigo, _decodificar(salida), _decodificar(error))

    def _matar_procesos(self) -> None:
        """Mata los ``hyprctl`` que sigan vivos (apagado)."""
        for proc in list(self._procesos):
            _matar(proc)
        self._procesos.clear()

    # ------------------------------------------------------------------
    # Dialecto
    # ------------------------------------------------------------------

    async def detectar_dialecto(self, *, timeout: float = TIMEOUT_HYPRCTL_S) -> str:
        """Fija el dialecto definitivo (``lua``/``legacy``/``ninguno``) y lo cachea.

        Orden: ``HYPR_DISPATCH_MODE`` lo fuerza; en ``auto``, ``hyprctl -j status`` (con
        ``timeout``); si no informa, existencia de ``$XDG_CONFIG_HOME/hypr/hyprland.lua``.
        """
        if not self.disponible:
            self.dialecto = "ninguno"
            self._detectado = True
            return self.dialecto
        if self._detectado:
            return self.dialecto
        try:
            if self._forzado:
                self.dialecto = self._forzado
                log.info("Hyprland: dialecto %s forzado por HYPR_DISPATCH_MODE", self.dialecto)
            else:
                estado = await self.consultar("status", timeout=timeout)
                dialecto = _dialecto_desde_status(estado)
                if dialecto is None:
                    dialecto = "lua" if _existe_config_lua() else "legacy"
                    log.debug("Hyprland status no informa el formato; uso %s por el archivo de configuración",
                              dialecto)
                self.dialecto = dialecto
                log.info("Hyprland: dialecto de dispatch %s", self.dialecto)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No se pudo detectar el dialecto de Hyprland (%s); uso %s",
                        type(exc).__name__, self.dialecto)
        self._detectado = True
        return self.dialecto

    def _argv_dispatch(self, accion: str, valor: str, dialecto: str) -> list[str]:
        """Argumentos de ``hyprctl`` para un dispatch YA validado, según el dialecto (§10.17)."""
        lua = dialecto == "lua"
        if accion == "enfocar_clase":
            selector = f"class:^({valor})$"
            if lua:
                return ["dispatch", f'hl.dsp.focus({{ window = "{selector}" }})']
            return ["dispatch", "focuswindow", selector]
        if accion == "enfocar_direccion":
            selector = f"address:{valor}"
            if lua:
                return ["dispatch", f'hl.dsp.focus({{ window = "{selector}" }})']
            return ["dispatch", "focuswindow", selector]
        if accion == "workspace":
            if lua:
                return ["dispatch", f'hl.dsp.focus({{ workspace = "{valor}" }})']
            return ["dispatch", "workspace", valor]
        if accion == "lanzar":
            if lua:
                return ["dispatch", f"hl.dsp.exec_cmd('{valor}')"]
            return ["dispatch", "exec", valor]
        if accion == "cerrar":
            selector = f"address:{valor}"
            if lua:
                return ["dispatch", f'hl.dsp.window.close({{ window = "{selector}" }})']
            return ["dispatch", "closewindow", selector]
        raise ValueError(f"acción de Hyprland desconocida: {accion}")

    async def _despachar(self, accion: str, valor: str) -> bool:
        """Despacha con autocorrección del dialecto ante un error de sintaxis (una vez)."""
        if not self.disponible:
            return False
        if not self._detectado:
            await self.detectar_dialecto()
        dialecto = self.dialecto if self.dialecto in ("lua", "legacy") else "legacy"
        resultado = await self._hyprctl(*self._argv_dispatch(accion, valor, dialecto))
        if _despacho_ok(resultado):
            return True
        texto = resultado.texto if resultado is not None else ""
        otro: str | None = None
        if dialecto == "legacy" and _es_error_lua(texto):
            otro = "lua"
        elif dialecto == "lua" and _es_error_legacy(texto):
            otro = "legacy"
        if otro is not None:
            if self._forzado:
                log.warning("Hyprland rechazó el dispatch %s forzado por HYPR_DISPATCH_MODE; "
                            "revisa esa variable", dialecto)
                return False
            log.info("Hyprland: autocorrijo el dialecto %s → %s y reintento", dialecto, otro)
            self.dialecto = otro
            resultado = await self._hyprctl(*self._argv_dispatch(accion, valor, otro))
            if _despacho_ok(resultado):
                return True
        log.debug("Dispatch %s de Hyprland fallido: %s", accion,
                  (resultado.texto[:200] if resultado is not None else "sin respuesta"))
        return False

    # ------------------------------------------------------------------
    # API pública (ProveedorHypr y acciones)
    # ------------------------------------------------------------------

    async def consultar(self, que: str, *, timeout: float = TIMEOUT_HYPRCTL_S) -> Any:
        """``hyprctl -j <que>`` → JSON. ``que`` ∈ clients/activewindow/workspaces/monitors/version/status.

        Cualquier fallo (no disponible, timeout, código distinto de 0, JSON inválido) ⇒ ``None``.
        """
        if que not in _CONSULTAS_HYPR:
            log.debug("Consulta de Hyprland no permitida: %r", que)
            return None
        if not self.disponible:
            return None
        try:
            resultado = await self._hyprctl("-j", que, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Consulta %s de Hyprland fallida (%s)", que, type(exc).__name__)
            return None
        if resultado is None or resultado.codigo != 0 or not resultado.salida:
            return None
        try:
            return json.loads(resultado.salida)
        except ValueError:
            log.debug("Hyprland devolvió una respuesta no JSON para %s", que)
            return None

    async def ventana_activa(self) -> dict[str, Any] | None:
        """La ventana activa (``hyprctl -j activewindow``) o ``None`` si no hay o falla."""
        try:
            datos = await self.consultar("activewindow")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("ventana_activa falló (%s)", type(exc).__name__)
            return None
        if isinstance(datos, dict) and datos.get("address"):
            return datos
        return None

    async def enfocar_clase(self, clase: str) -> bool:
        """Enfoca la primera ventana de clase exacta ``clase`` (validada con ``SAFE_CLASS_RE``)."""
        if not (isinstance(clase, str) and SAFE_CLASS_RE.fullmatch(clase)):
            log.info("Clase de ventana rechazada por la validación")
            return False
        if not self.disponible:
            return False
        return await self._seguro(self._despachar("enfocar_clase", clase), "enfocar_clase")

    async def enfocar_direccion(self, direccion: str) -> bool:
        """Enfoca la ventana con dirección ``0x…`` (validada con ``DIRECCION_RE``)."""
        if not (isinstance(direccion, str) and DIRECCION_RE.fullmatch(direccion)):
            log.info("Dirección de ventana rechazada por la validación")
            return False
        if not self.disponible:
            return False
        return await self._seguro(self._despachar("enfocar_direccion", direccion), "enfocar_direccion")

    async def ir_workspace(self, n: int) -> bool:
        """Cambia al espacio de trabajo ``n`` (entero 1..10)."""
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10:
            log.info("Espacio de trabajo rechazado por la validación")
            return False
        if not self.disponible:
            return False
        return await self._seguro(self._despachar("workspace", str(n)), "ir_workspace")

    async def lanzar(self, alias: str) -> bool:
        """Lanza la app del alias ``alias`` **solo** si está en ``settings.apps_permitidas``."""
        comando = self.settings.apps_permitidas.get(alias) if isinstance(alias, str) else None
        if not (isinstance(comando, str) and SAFE_CLASS_RE.fullmatch(comando)):
            log.info("Alias de app rechazado: no está en la lista permitida")
            return False
        if not self.disponible:
            return False
        return await self._seguro(self._despachar("lanzar", comando), "lanzar")

    async def cerrar_direccion(self, direccion: str) -> bool:
        """Cierra la ventana ``direccion`` si existe y **no** pertenece a R.A.M.S.

        Defensa en profundidad además de la compuerta: valida ``DIRECCION_RE``, consulta
        ``clients`` y, si la dirección no existe o su ``pid`` está en ``pids_propios``,
        devuelve ``False`` sin despachar.
        """
        if not (isinstance(direccion, str) and DIRECCION_RE.fullmatch(direccion)):
            log.info("Dirección de ventana rechazada por la validación")
            return False
        if not self.disponible:
            return False
        try:
            clientes = await self.consultar("clients")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("No se pudo consultar la lista de ventanas (%s)", type(exc).__name__)
            return False
        if not isinstance(clientes, list):
            log.info("No pude verificar la ventana a cerrar: Hyprland no devolvió la lista de ventanas")
            return False
        objetivo = next((c for c in clientes if isinstance(c, dict)
                         and str(c.get("address", "")).lower() == direccion.lower()), None)
        if objetivo is None:
            log.info("La ventana a cerrar ya no existe")
            return False
        pid = objetivo.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid in self.pids_propios:
            log.warning("Se rechazó cerrar una ventana de R.A.M.S. (su propia terminal)")
            return False
        return await self._seguro(self._despachar("cerrar", direccion), "cerrar_direccion")

    async def eventos(self, al_evento: Callable[[str, str], Awaitable[None]], parar: asyncio.Event) -> None:
        """Escucha ``.socket2.sock`` y llama a ``al_evento(evento, datos)`` por cada ``EV>>DATOS``.

        Reconecta cada 5 s si el socket se cae. Termina cuando ``parar`` se activa. Solo en
        Linux con ``asyncio.open_unix_connection``.
        """
        if not self.disponible or self.directorio is None:
            log.debug("Eventos de Hyprland no disponibles")
            return
        if not (_es_linux() and hasattr(asyncio, "open_unix_connection")):
            return
        ruta = str(self.directorio / ".socket2.sock")
        espera_parar = asyncio.ensure_future(parar.wait())
        try:
            while not parar.is_set():
                escritor: asyncio.StreamWriter | None = None
                try:
                    lector, escritor = await asyncio.wait_for(asyncio.open_unix_connection(ruta), timeout=3.0)
                    log.debug("Conectado al socket de eventos de Hyprland")
                    while not parar.is_set():
                        lectura = asyncio.ensure_future(lector.readline())
                        hechas, _ = await asyncio.wait({lectura, espera_parar},
                                                       return_when=asyncio.FIRST_COMPLETED)
                        if lectura not in hechas:
                            lectura.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await lectura
                            break
                        linea = lectura.result()
                        if not linea:
                            log.debug("Hyprland cerró el socket de eventos")
                            break
                        evento, separador, datos = linea.decode("utf-8", "replace").rstrip("\r\n").partition(">>")
                        if not separador or not evento:
                            continue
                        try:
                            await al_evento(evento, datos)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.debug("El manejador del evento %s de Hyprland falló (%s)", evento,
                                      type(exc).__name__)
                except asyncio.CancelledError:
                    raise
                except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                    log.debug("Socket de eventos de Hyprland no disponible (%s); reintento en %.0f s",
                              type(exc).__name__, _RECONEXION_EVENTOS_S)
                finally:
                    if escritor is not None:
                        escritor.close()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(escritor.wait_closed(), timeout=1.0)
                if parar.is_set():
                    break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(espera_parar), timeout=_RECONEXION_EVENTOS_S)
        finally:
            espera_parar.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await espera_parar

    async def _seguro(self, corrutina: Awaitable[bool], nombre: str) -> bool:
        """Ejecuta un dispatch sin dejar escapar excepciones (salvo la cancelación)."""
        try:
            return bool(await corrutina)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Hyprland: %s falló (%s)", nombre, type(exc).__name__)
            return False


# ---------------------------------------------------------------------------
# Lóbulo
# ---------------------------------------------------------------------------


@dataclass
class _Parpadeo:
    """Estado de un vistazo en curso (vive en el loop; el hilo solo toca ``cancelar``)."""

    motivo: str
    inicio: float                 # time.monotonic() real (duración del indicador)
    inicio_reloj: float           # reloj inyectado (intervalos y backoff)
    cancelar: threading.Event
    agotado: bool = False
    resultado: ResultadoVistazo | None = None


@dataclass
class _EstadoAlerta:
    consecutivas: int = 0
    desde: float | None = None
    alertada: bool = False
    ultima_ts: float | None = None


def _texto_rostro(rostro: bool | None) -> str:
    if rostro is True:
        return "sí"
    if rostro is False:
        return "no"
    return "desconocido"


def _a_resultado(valor: Any) -> ResultadoVistazo:
    """Normaliza lo que devuelve ``_parpadeo`` (``ResultadoVistazo``, dict o bool)."""
    if isinstance(valor, ResultadoVistazo):
        return valor
    if isinstance(valor, Mapping):
        return ResultadoVistazo(
            valor.get("rostro"),
            n_rostros=valor.get("n_rostros", 0),
            duracion_ms=valor.get("duracion_ms", 0),
            brillo=valor.get("brillo"),
            error=valor.get("error"),
        )
    if isinstance(valor, bool):
        return ResultadoVistazo(valor, n_rostros=1 if valor else 0)
    return ResultadoVistazo(None, error="no_abre")


def _resolver_futuro(futuro: asyncio.Future[Any], valor: Any) -> None:
    if not futuro.done():
        futuro.set_result(valor)


def _fallar_futuro(futuro: asyncio.Future[Any], exc: BaseException) -> None:
    if not futuro.done():
        futuro.set_exception(exc)


def _ancho_png_cabecera(png: bytes) -> int | None:
    """Ancho de un PNG leído de su cabecera IHDR (sin decodificar la imagen)."""
    if len(png) < 24 or not png.startswith(_FIRMA_PNG) or png[12:16] != b"IHDR":
        return None
    ancho = struct.unpack(">I", png[16:20])[0]
    return int(ancho) if ancho > 0 else None


def _numero(valor: str) -> float | None:
    """Float de un campo de ``nvidia-smi`` (``[N/A]`` u otros ⇒ ``None``)."""
    try:
        return float(valor.strip())
    except (TypeError, ValueError):
        return None


class AuraVisionPC:
    """Lóbulo sensorimotor: cámara "parpadeo", pantalla, telemetría, Hyprland y acciones.

    Compone ``ControlHyprland`` (``self.hypr``) y, si está instalado, ``NexusLink``
    (``self.nexus``). Las acciones pasan por la compuerta de permisos **dentro** de
    ``ejecutar``, ``vistazo`` y ``capturar_pantalla``.
    """

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 permisos: CompuertaPermisos | None = None,
                 reloj: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.bus = bus
        self.permisos = permisos
        self._reloj = reloj
        # CONTRATO: CompuertaPermisos.pids_propios (§10.10); sin compuerta (o con un fake que no
        # lo tenga) se calculan aquí: {os.getpid()} ∪ ancestros.
        pids = getattr(permisos, "pids_propios", None) if permisos is not None else None
        if not isinstance(pids, frozenset):
            pids = calcular_pids_propios()
        self.hypr = ControlHyprland(settings, pids_propios=pids)
        self._compuerta: CompuertaPermisos | None = None

        # Nexus IDE (opcional, grupo nexus).
        self.nexus: Any = None
        self._error_nexus: str | None = _ERROR_NEXUS
        if NexusLink is not None:
            try:
                self.nexus = NexusLink(settings, hypr=self.hypr)
            except Exception as exc:
                log.warning("No se pudo construir NexusLink (%s); Nexus queda OFF", type(exc).__name__)
                self.nexus = None
                self._error_nexus = "no se pudo iniciar el enlace con Nexus"

        # Cámara.
        self._camara_runtime = True
        self._ocupada = False
        self._parpadeo_actual: _Parpadeo | None = None
        self._ultimo_vistazo_ts: float | None = None
        self._escalon = 0
        self._dia: date | None = None
        self._vistazos_dia = 0
        self._haar: Any = None
        self._haar_intentado = False
        self._yunet: Any = None
        self._yunet_tam: tuple[int, int] | None = None
        self._lock_detector = threading.Lock()

        # Telemetría.
        self._ejecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rams-pc")
        self._lock_telemetria = asyncio.Lock()
        self._telemetria_cache: Telemetria | None = None
        self._telemetria_ts = float("-inf")
        self._top: tuple[str, int, float] | None = None
        self._top_ts = float("-inf")
        self._ruta_nvidia: str | None = None
        self._nvidia_buscado = False
        self._nvidia: tuple[float | None, float | None, str | None] | None = None
        self._nvidia_ts = float("-inf")
        self._alertas: dict[str, _EstadoAlerta] = {tipo: _EstadoAlerta() for tipo in _REGLAS_ALERTA}

        # Eventos de Hyprland.
        self._ventana_pendiente: str | None = None
        self._tarea_ventana: asyncio.Task[None] | None = None
        self._ultima_ventana_ts = float("-inf")

        # Procesos y tareas propias.
        self._procesos: set[asyncio.subprocess.Process] = set()
        self._esperas: set[asyncio.Future[Any]] = set()
        self._cerrado = False

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------

    def _publicar(self, nombre: str, datos: Mapping[str, Any]) -> None:
        """Publica en el bus (si hay); nunca lanza."""
        if self.bus is None:
            return
        try:
            self.bus.publicar(nombre, dict(datos), origen=ORIGEN_BUS)
        except Exception as exc:
            log.debug("No se pudo publicar %s: %s", nombre, exc)

    def _compuerta_local(self) -> CompuertaPermisos:
        """Compuerta solo para ``evaluar`` cuando no se inyectó ``permisos`` (tests)."""
        if self._compuerta is None:
            self._compuerta = CompuertaPermisos(self.settings, None, pids_propios=self.hypr.pids_propios)
        return self._compuerta

    async def _autorizada(self, accion: AccionPC) -> bool:
        """Pasa ``accion`` por la compuerta. Sin ``permisos``, solo lo ``permitida`` pasa."""
        if self.permisos is not None:
            decision = await self.permisos.autorizar(accion)
            return bool(decision.aprobada)
        return self._compuerta_local().evaluar(accion).veredicto == "permitida"

    # ------------------------------------------------------------------
    # Cámara: estado
    # ------------------------------------------------------------------

    @property
    def camara_habilitada(self) -> bool:
        """``True`` si el maestro ``RAMS_CAMERA`` y el interruptor en tiempo de ejecución lo permiten."""
        return bool(self.settings.camara_habilitada) and self._camara_runtime and not self._cerrado

    def set_camara(self, habilitada: bool) -> bool:
        """Interruptor en tiempo de ejecución (``/camara``, HUD). Devuelve el estado efectivo.

        No puede habilitar la cámara si el maestro ``RAMS_CAMERA`` está en ``False``. Al
        deshabilitarla, un vistazo en curso se cancela entre ``grab()``.
        """
        try:
            antes = self.camara_habilitada
            self._camara_runtime = bool(habilitada)
            efectivo = self.camara_habilitada
            if habilitada and not self.settings.camara_habilitada:
                log.info("El interruptor maestro RAMS_CAMERA=0 impide habilitar la cámara")
            if not efectivo and self._parpadeo_actual is not None:
                self._parpadeo_actual.cancelar.set()
            if efectivo != antes:
                log.info("Cámara %s", "habilitada" if efectivo else "deshabilitada")
                self._publicar("sistema.modulo", self._diagnostico_camara().a_dict())
            return efectivo
        except Exception as exc:  # defensa: nunca lanza
            log.warning("set_camara falló (%s)", type(exc).__name__)
            return False

    def reiniciar_backoff_camara(self) -> None:
        """Vuelve al primer escalón del *backoff* (main la llama con cada entrada local)."""
        self._escalon = 0

    def _intervalo_backoff(self) -> float:
        base = max(0.0, float(self.settings.camara_intervalo_min_s))
        escalera = (base, *(max(base, s) for s in ESCALONES_BACKOFF_S))
        return escalera[min(self._escalon, len(escalera) - 1)]

    def _vistazos_hoy(self) -> int:
        """Vistazos no pedidos por el usuario en el día local actual."""
        hoy = ahora_local(self.settings).date()
        if self._dia != hoy:
            self._dia = hoy
            self._vistazos_dia = 0
        return self._vistazos_dia

    def _fallo(self, motivo: str, error: str) -> ResultadoVistazo:
        """Resultado fallido de un vistazo; también se publica en ``vision.vistazo``."""
        resultado = ResultadoVistazo(None, motivo=motivo, error=error)
        log.debug("Vistazo (%s) no realizado: %s", motivo, error)
        self._publicar("vision.vistazo", resultado.a_dict())
        return resultado

    # ------------------------------------------------------------------
    # Cámara: vistazo
    # ------------------------------------------------------------------

    async def vistazo(self, motivo: str = "curiosidad", *, origen: str | None = None) -> ResultadoVistazo:
        """Parpadeo de cámara con todas las comprobaciones de §10.17 y §9.2. Nunca lanza.

        ``origen`` ``None`` ⇒ ``"usuario"`` si ``motivo == "usuario"``; si no, ``"iniciativa"``.
        Autoriza internamente con la compuerta. Si la cámara no responde en 3 s devuelve
        ``error="tiempo_agotado"`` sin liberar el *flag* de ocupada: lo libera el hilo cuando
        realmente suelta el dispositivo, y es entonces cuando se publica ``vision.vistazo``
        con el resultado real del parpadeo.
        """
        motivo = str(motivo) if motivo else "curiosidad"
        try:
            return await self._vistazo(motivo, origen)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Error inesperado en el vistazo de cámara (%s)", type(exc).__name__)
            return self._fallo(motivo, "no_abre")

    async def _vistazo(self, motivo: str, origen: str | None) -> ResultadoVistazo:
        if origen is None:
            origen = "usuario" if motivo == "usuario" else "iniciativa"
        if not self.camara_habilitada:
            return self._fallo(motivo, "camara_deshabilitada")
        if cv2 is None:
            return self._fallo(motivo, "sin_opencv")
        accion = AccionPC("vistazo", {"motivo": motivo}, "mirar por la cámara", origen=origen)
        if not await self._autorizada(accion):
            return self._fallo(motivo, "denegada")
        if not self.camara_habilitada:  # se pudo deshabilitar mientras se autorizaba
            return self._fallo(motivo, "camara_deshabilitada")
        ahora = self._reloj()
        ultimo = self._ultimo_vistazo_ts
        if motivo == "usuario":
            if ultimo is not None and ahora - ultimo < INTERVALO_USUARIO_S:
                return self._fallo(motivo, "intervalo")
        else:
            if ultimo is not None and ahora - ultimo < self._intervalo_backoff():
                return self._fallo(motivo, "backoff")
            if self._vistazos_hoy() >= max(0, int(self.settings.camara_max_dia)):
                return self._fallo(motivo, "limite_diario")
        if self._ocupada:
            return self._fallo(motivo, "ocupada")

        # Desde aquí no hay ningún await hasta arrancar el hilo: nadie puede colarse.
        self._ocupada = True
        loop = asyncio.get_running_loop()
        futuro: asyncio.Future[Any] = loop.create_future()
        estado = _Parpadeo(motivo=motivo, inicio=time.monotonic(), inicio_reloj=ahora,
                           cancelar=threading.Event())
        self._parpadeo_actual = estado
        self._publicar("vision.camara", {"activa": True, "habilitada": True, "duracion_ms": 0})
        futuro.add_done_callback(lambda f: self._al_terminar_parpadeo(estado, f))
        hilo = threading.Thread(target=self._hilo_camara, args=(loop, futuro, estado.cancelar),
                                name="rams-camara", daemon=True)
        try:
            hilo.start()
        except RuntimeError as exc:  # no se pudo crear el hilo
            log.warning("No se pudo crear el hilo de cámara (%s)", exc)
            futuro.set_result(ResultadoVistazo(None, error="no_abre"))

        try:
            await asyncio.wait_for(asyncio.shield(futuro), timeout=TIMEOUT_CAMARA_S)
        except asyncio.TimeoutError:
            estado.cancelar.set()
            estado.agotado = True
            log.warning("La cámara no respondió en %.0f s; se liberará cuando el hilo termine",
                        TIMEOUT_CAMARA_S)
            # Aquí NO se publica vision.vistazo: lo publica el *done callback* con el resultado
            # REAL cuando el hilo termina (§10.17, paso 2). Si el hilo atiende la cancelación,
            # ese resultado es "tiempo_agotado"; si aun así llegó a leer un fotograma, el
            # rostro detectado llega al bus (conectoma, presencia) igual que al backoff.
            return ResultadoVistazo(None, motivo=motivo, error="tiempo_agotado")
        except asyncio.CancelledError:
            estado.cancelar.set()
            estado.agotado = True
            raise
        except Exception as exc:  # el hilo terminó con excepción: la trata el callback
            log.debug("El hilo de cámara terminó con %s", type(exc).__name__)
        return self._resultado_de(estado, futuro)

    def _hilo_camara(self, loop: asyncio.AbstractEventLoop, futuro: asyncio.Future[Any],
                     cancelar: threading.Event) -> None:
        """Cuerpo del hilo daemon ``rams-camara``: parpadea y entrega el resultado al loop."""
        try:
            resultado = self._parpadeo(cancelar)
        except BaseException as exc:  # noqa: BLE001 - se entrega al loop como excepción
            error = exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__)
            try:
                loop.call_soon_threadsafe(_fallar_futuro, futuro, error)
            except RuntimeError:
                log.debug("El loop se cerró antes de terminar el vistazo")
            return
        try:
            loop.call_soon_threadsafe(_resolver_futuro, futuro, resultado)
        except RuntimeError:
            log.debug("El loop se cerró antes de terminar el vistazo")

    def _resultado_de(self, estado: _Parpadeo, futuro: asyncio.Future[Any]) -> ResultadoVistazo:
        """Resultado normalizado (con ``motivo``) de un parpadeo terminado; se calcula una vez."""
        if estado.resultado is not None:
            return estado.resultado
        if futuro.cancelled():
            bruto = ResultadoVistazo(None, error="no_abre")
        else:
            excepcion = futuro.exception()
            if excepcion is not None:
                log.warning("Fallo en el hilo de cámara (%s)", type(excepcion).__name__)
                bruto = ResultadoVistazo(None, error="no_abre")
            else:
                bruto = _a_resultado(futuro.result())
        duracion = bruto.duracion_ms
        if duracion <= 0 and bruto.error is None:
            duracion = int((time.monotonic() - estado.inicio) * 1000)
        estado.resultado = dataclasses.replace(bruto, motivo=estado.motivo, duracion_ms=duracion)
        return estado.resultado

    def _al_terminar_parpadeo(self, estado: _Parpadeo, futuro: asyncio.Future[Any]) -> None:
        """*Done callback* (en el loop): libera el flag, publica y contabiliza el vistazo."""
        try:
            self._ocupada = False
            if self._parpadeo_actual is estado:
                self._parpadeo_actual = None
            ms_reales = int((time.monotonic() - estado.inicio) * 1000)
            resultado = self._resultado_de(estado, futuro)
            self._publicar("vision.camara", {"activa": False, "habilitada": self.camara_habilitada,
                                             "duracion_ms": ms_reales})
            # Siempre, también tras un timeout: un vistazo ⇒ un vision.vistazo con lo que de
            # verdad pasó (lo mismo que se contabiliza abajo para el backoff).
            self._publicar("vision.vistazo", resultado.a_dict())
            if estado.agotado:
                log.info("El hilo de cámara terminó tras el tiempo límite (%d ms)", ms_reales)
            if resultado.error is None:
                log.info("Vistazo de cámara: %d ms, rostro=%s", resultado.duracion_ms,
                         _texto_rostro(resultado.rostro))
            else:
                log.info("Vistazo de cámara fallido (%s) tras %d ms", resultado.error, ms_reales)
            self._contabilizar(estado, resultado)
        except Exception as exc:  # un callback nunca debe romper el loop
            self._ocupada = False
            log.warning("Error al cerrar el vistazo de cámara (%s)", type(exc).__name__)

    def _contabilizar(self, estado: _Parpadeo, resultado: ResultadoVistazo) -> None:
        """Actualiza intervalo, *backoff* (sin rostro ⇒ siguiente escalón) y contador diario."""
        self._ultimo_vistazo_ts = estado.inicio_reloj
        if estado.motivo == "usuario":
            if resultado.rostro is True:
                self._escalon = 0
            return
        self._vistazos_hoy()
        self._vistazos_dia += 1
        if resultado.rostro is True:
            self._escalon = 0
        else:
            self._escalon = min(self._escalon + 1, len(ESCALONES_BACKOFF_S))

    # ------------------------------------------------------------------
    # Cámara: hilo (hardware)
    # ------------------------------------------------------------------

    def _parpadeo(self, cancelar: threading.Event) -> ResultadoVistazo:
        """Abre la cámara, calienta 4 ``grab()``, lee un fotograma y la libera (en un ``finally``).

        Corre en el hilo daemon ``rams-camara``. El fotograma es una variable local que se
        descarta al terminar: nunca se escribe en disco ni se devuelve.
        """
        if cv2 is None:
            return ResultadoVistazo(None, error="sin_opencv")
        if _es_linux():
            backend = getattr(cv2, "CAP_V4L2", 200)
        elif sys.platform == "win32":
            backend = getattr(cv2, "CAP_DSHOW", 700)
        else:
            backend = getattr(cv2, "CAP_ANY", 0)
        inicio = time.monotonic()
        captura = None
        rostro: bool | None = None
        n_rostros = 0
        brillo: float | None = None
        try:
            captura = cv2.VideoCapture(int(self.settings.camara_indice), backend)
            if captura is None or not captura.isOpened():
                return ResultadoVistazo(None, error="no_abre",
                                        duracion_ms=int((time.monotonic() - inicio) * 1000))
            with contextlib.suppress(Exception):
                captura.set(cv2.CAP_PROP_FOURCC, _FOURCC_MJPG)
                captura.set(cv2.CAP_PROP_FRAME_WIDTH, _ANCHO_CAMARA)
                captura.set(cv2.CAP_PROP_FRAME_HEIGHT, _ALTO_CAMARA)
            for _ in range(CALENTAMIENTO_GRABS):
                if cancelar.is_set():
                    return ResultadoVistazo(None, error="tiempo_agotado",
                                            duracion_ms=int((time.monotonic() - inicio) * 1000))
                captura.grab()
            if cancelar.is_set():
                return ResultadoVistazo(None, error="tiempo_agotado",
                                        duracion_ms=int((time.monotonic() - inicio) * 1000))
            ok, fotograma = captura.read()
            if not ok or fotograma is None:
                return ResultadoVistazo(None, error="no_abre",
                                        duracion_ms=int((time.monotonic() - inicio) * 1000))
        finally:
            if captura is not None:
                try:
                    captura.release()      # SIEMPRE: apaga el LED de la cámara
                except Exception as exc:
                    log.debug("release() de la cámara falló (%s)", type(exc).__name__)
        duracion = int((time.monotonic() - inicio) * 1000)
        try:
            gris = self._a_gris(fotograma)
            brillo = float(gris.mean()) / 255.0
            rostro, n_rostros = self._detectar_rostros(fotograma, gris)
            del gris
        except Exception as exc:
            log.debug("Análisis del fotograma fallido (%s)", type(exc).__name__)
        finally:
            del fotograma            # se descarta: nunca sale de esta función
        return ResultadoVistazo(rostro, n_rostros=n_rostros, duracion_ms=duracion, brillo=brillo)

    @staticmethod
    def _a_gris(fotograma: Any) -> Any:
        """Convierte el fotograma a escala de grises (BGR, BGRA o ya gris)."""
        forma = getattr(fotograma, "shape", ())
        if len(forma) == 2:
            return fotograma
        if len(forma) == 3 and forma[2] == 4:
            return cv2.cvtColor(fotograma, cv2.COLOR_BGRA2GRAY)
        return cv2.cvtColor(fotograma, cv2.COLOR_BGR2GRAY)

    def _ruta_haar(self) -> Path | None:
        """XML de Haar de ``cv2.data.haarcascades`` o de ``/usr/share/opencv4/haarcascades/``."""
        if cv2 is None or not hasattr(cv2, "CascadeClassifier"):
            return None
        candidatos: list[Path] = []
        datos = getattr(cv2, "data", None)
        directorio = getattr(datos, "haarcascades", None)
        if directorio:
            candidatos.append(Path(str(directorio)) / _XML_HAAR)
        candidatos.extend(Path(d) / _XML_HAAR for d in _DIRS_HAAR_SISTEMA)
        for candidato in candidatos:
            try:
                if candidato.is_file():
                    return candidato
            except OSError:
                continue
        return None

    def _ruta_yunet(self) -> Path | None:
        """Modelo YuNet en ``data_dir/models`` si OpenCV trae ``FaceDetectorYN``."""
        if cv2 is None or not hasattr(cv2, "FaceDetectorYN"):
            return None
        ruta = self.settings.data_dir / "models" / _MODELO_YUNET
        try:
            return ruta if ruta.is_file() else None
        except OSError:
            return None

    def _tipo_detector(self) -> str | None:
        """``"haar"``, ``"yunet"`` o ``None`` (sin detector de rostros)."""
        if self._ruta_haar() is not None:
            return "haar"
        if self._ruta_yunet() is not None:
            return "yunet"
        return None

    def _clasificador_haar(self) -> Any:
        """Clasificador Haar cacheado (solo hay un hilo de cámara vivo a la vez)."""
        with self._lock_detector:
            if self._haar_intentado:
                return self._haar
            self._haar_intentado = True
            ruta = self._ruta_haar()
            if ruta is None:
                return None
            try:
                clasificador = cv2.CascadeClassifier(str(ruta))
                if clasificador.empty():
                    log.warning("El clasificador Haar está vacío; sin detección Haar")
                    return None
                self._haar = clasificador
            except Exception as exc:
                log.warning("No se pudo cargar el clasificador Haar (%s)", type(exc).__name__)
            return self._haar

    def _detector_yunet(self, ancho: int, alto: int) -> Any:
        """Detector YuNet cacheado y ajustado al tamaño del fotograma."""
        ruta = self._ruta_yunet()
        if ruta is None:
            return None
        with self._lock_detector:
            try:
                if self._yunet is None:
                    clase = getattr(cv2, "FaceDetectorYN", None)
                    crear = getattr(clase, "create", None) or getattr(cv2, "FaceDetectorYN_create", None)
                    if crear is None:
                        return None
                    self._yunet = crear(str(ruta), "", (ancho, alto))
                    self._yunet_tam = (ancho, alto)
                elif self._yunet_tam != (ancho, alto):
                    self._yunet.setInputSize((ancho, alto))
                    self._yunet_tam = (ancho, alto)
            except Exception as exc:
                log.warning("No se pudo crear el detector YuNet (%s)", type(exc).__name__)
                self._yunet = None
            return self._yunet

    def _detectar_rostros(self, fotograma: Any, gris: Any) -> tuple[bool | None, int]:
        """Haar (``equalizeHist`` + ``detectMultiScale``) o YuNet; sin detector ⇒ ``(None, 0)``."""
        clasificador = self._clasificador_haar()
        if clasificador is not None:
            ecualizada = cv2.equalizeHist(gris)
            caras = clasificador.detectMultiScale(ecualizada, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            n = len(caras)
            return n > 0, n
        forma = getattr(fotograma, "shape", ())
        if len(forma) >= 2:
            alto, ancho = int(forma[0]), int(forma[1])
            detector = self._detector_yunet(ancho, alto)
            if detector is not None:
                if len(forma) == 2:
                    imagen = cv2.cvtColor(fotograma, cv2.COLOR_GRAY2BGR)
                elif forma[2] == 4:
                    imagen = cv2.cvtColor(fotograma, cv2.COLOR_BGRA2BGR)
                else:
                    imagen = fotograma
                _, caras = detector.detect(imagen)
                n = 0 if caras is None else len(caras)
                return n > 0, n
        return None, 0

    # ------------------------------------------------------------------
    # Pantalla
    # ------------------------------------------------------------------

    async def capturar_pantalla(self, max_ancho: int = 1280, *, origen: str = "usuario") -> bytes | None:
        """PNG de la pantalla (``grim``) reducido a ``max_ancho`` si hace falta. Autoriza internamente.

        Linux con ``grim``: devuelve los bytes **originales** de grim si no hace falta reducir;
        si el ancho supera ``max_ancho``, vuelve a invocar grim con ``-s <escala>`` (calculada
        con ``hyprctl -j monitors``). Windows y otros: ``None``. Nunca lanza.
        """
        try:
            accion = AccionPC("captura_pantalla", {}, "capturar la pantalla", origen=origen)
            if not await self._autorizada(accion):
                log.info("Captura de pantalla no autorizada")
                return None
            if not _es_linux():
                log.info("La captura de pantalla no está implementada en este sistema")
                return None
            grim = shutil.which("grim")
            if not grim:
                log.info("grim no está instalado: no puedo capturar la pantalla")
                return None
            png = await self._grim(grim, None)
            if png is None:
                return None
            ancho = await self._ancho_png(png)
            if ancho is None or max_ancho <= 0 or ancho <= max_ancho:
                return png
            escala_actual = await self._escala_monitores()
            escala = max(0.05, min(escala_actual, escala_actual * max_ancho / ancho))
            reducida = await self._grim(grim, escala)
            if reducida is None:
                log.debug("La captura reducida falló; devuelvo la original")
                return png
            return reducida
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Captura de pantalla fallida (%s)", type(exc).__name__)
            return None

    async def _grim(self, ruta: str, escala: float | None) -> bytes | None:
        """``grim [-s escala] -t png -`` (stdout) con timeout de 5 s."""
        argv = [ruta]
        if escala is not None:
            argv += ["-s", f"{escala:.4f}"]
        argv += ["-t", "png", "-"]
        resultado = await _correr(argv, timeout=TIMEOUT_GRIM_S, procesos=self._procesos)
        if resultado is None:
            return None
        codigo, salida, error = resultado
        if codigo != 0 or not salida.startswith(_FIRMA_PNG):
            log.info("grim falló (código %s): %s", codigo, _decodificar(error)[:200])
            return None
        return salida

    async def _ancho_png(self, png: bytes) -> int | None:
        """Ancho de la captura: ``cv2.imdecode`` si hay cv2 y numpy; si no, la cabecera IHDR."""
        if cv2 is not None and np is not None:
            try:
                loop = asyncio.get_running_loop()
                ancho = await loop.run_in_executor(self._ejecutor, self._ancho_decodificado, png)
                if ancho:
                    return ancho
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("No se pudo decodificar la captura (%s)", type(exc).__name__)
        return _ancho_png_cabecera(png)

    @staticmethod
    def _ancho_decodificado(png: bytes) -> int | None:
        imagen = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if imagen is None:
            return None
        ancho = int(imagen.shape[1])
        del imagen
        return ancho

    async def _escala_monitores(self) -> float:
        """Mayor ``scale`` de ``hyprctl -j monitors`` (la que grim usa por defecto); 1.0 si no hay datos."""
        monitores = await self.hypr.consultar("monitors") if self.hypr.disponible else None
        escalas: list[float] = []
        if isinstance(monitores, list):
            for monitor in monitores:
                if isinstance(monitor, dict):
                    try:
                        valor = float(monitor.get("scale", 1.0))
                    except (TypeError, ValueError):
                        continue
                    if 0 < valor < 10:
                        escalas.append(valor)
        return max(escalas) if escalas else 1.0

    # ------------------------------------------------------------------
    # Telemetría
    # ------------------------------------------------------------------

    async def telemetria(self) -> Telemetria:
        """Lectura del sistema (caché 2 s). Sin psutil devuelve ``Telemetria()``. Nunca lanza."""
        if psutil is None:
            return Telemetria()
        try:
            if self._telemetria_cache is not None and time.monotonic() - self._telemetria_ts < CACHE_TELEMETRIA_S:
                return self._telemetria_cache
            async with self._lock_telemetria:
                ahora = time.monotonic()
                if self._telemetria_cache is not None and ahora - self._telemetria_ts < CACHE_TELEMETRIA_S:
                    return self._telemetria_cache
                incluir_top = ahora - self._top_ts >= INTERVALO_TOP_S
                loop = asyncio.get_running_loop()
                datos = await loop.run_in_executor(self._ejecutor, self._leer_psutil, incluir_top)
                if incluir_top:
                    self._top_ts = ahora
                    self._top = datos.get("proceso_top")
                gpu_pct = datos.get("gpu_pct")
                temp_gpu = datos.get("temp_gpu")
                gpu_nombre: str | None = None
                nvidia = await self._leer_nvidia()
                if nvidia is not None:
                    uso, temperatura, nombre = nvidia
                    gpu_pct = uso if uso is not None else gpu_pct
                    temp_gpu = temperatura if temperatura is not None else temp_gpu
                    gpu_nombre = nombre
                lectura = Telemetria(
                    cpu_pct=datos.get("cpu_pct", 0.0),
                    ram_usada_gb=datos.get("ram_usada_gb", 0.0),
                    ram_total_gb=datos.get("ram_total_gb", 0.0),
                    ram_pct=datos.get("ram_pct", 0.0),
                    disco_pct=datos.get("disco_pct", 0.0),
                    temp_cpu=datos.get("temp_cpu"),
                    temp_gpu=temp_gpu,
                    gpu_pct=gpu_pct,
                    gpu_nombre=gpu_nombre,
                    proceso_top=self._top,
                )
                self._telemetria_cache = lectura
                self._telemetria_ts = time.monotonic()
                return lectura
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Lectura de telemetría fallida (%s)", type(exc).__name__)
            return self._telemetria_cache if self._telemetria_cache is not None else Telemetria()

    def _leer_psutil(self, incluir_top: bool) -> dict[str, Any]:
        """Lectura síncrona (executor ``rams-pc``). Cada dato falla por separado."""
        datos: dict[str, Any] = {}
        try:
            datos["cpu_pct"] = float(psutil.cpu_percent(None))
        except Exception as exc:
            log.debug("cpu_percent falló (%s)", type(exc).__name__)
        try:
            memoria = psutil.virtual_memory()
            total = float(memoria.total)
            datos["ram_total_gb"] = total / 1024 ** 3
            datos["ram_usada_gb"] = (total - float(memoria.available)) / 1024 ** 3
            datos["ram_pct"] = float(memoria.percent)
        except Exception as exc:
            log.debug("virtual_memory falló (%s)", type(exc).__name__)
        try:
            raiz = "/" if os.name != "nt" else (os.environ.get("SystemDrive", "C:").rstrip("\\") + "\\")
            datos["disco_pct"] = float(psutil.disk_usage(raiz).percent)
        except Exception as exc:
            log.debug("disk_usage falló (%s)", type(exc).__name__)
        temp_cpu, temp_gpu = self._leer_temperaturas()
        datos["temp_cpu"] = temp_cpu
        datos["temp_gpu"] = temp_gpu
        datos["gpu_pct"] = self._leer_gpu_busy()
        if incluir_top:
            datos["proceso_top"] = self._leer_proceso_top()
        return datos

    @staticmethod
    def _leer_temperaturas() -> tuple[float | None, float | None]:
        """CPU: ``k10temp``/Tctl, ``coretemp``/Package id 0, ``zenpower`` o el máximo; GPU: ``amdgpu``/edge."""
        lector = getattr(psutil, "sensors_temperatures", None)
        if lector is None:
            return None, None
        try:
            sensores = lector() or {}
        except Exception as exc:
            log.debug("sensors_temperatures falló (%s)", type(exc).__name__)
            return None, None

        def valor(chip: str, etiqueta: str | None) -> float | None:
            entradas = sensores.get(chip) or []
            for entrada in entradas:
                if etiqueta is None or getattr(entrada, "label", "") == etiqueta:
                    actual = getattr(entrada, "current", None)
                    if isinstance(actual, (int, float)) and actual > 0:
                        return float(actual)
            return None

        temp_cpu = (valor("k10temp", "Tctl") or valor("coretemp", "Package id 0")
                    or valor("zenpower", None) or valor("k10temp", None))
        if temp_cpu is None:
            candidatos = [
                float(e.current)
                for chip, entradas in sensores.items()
                if not any(str(chip).lower().startswith(p) for p in _CHIPS_NO_CPU)
                for e in (entradas or [])
                if isinstance(getattr(e, "current", None), (int, float)) and e.current > 0
            ]
            temp_cpu = max(candidatos) if candidatos else None
        temp_gpu = valor("amdgpu", "edge")
        return temp_cpu, temp_gpu

    @staticmethod
    def _leer_gpu_busy() -> float | None:
        """Uso de GPU AMD desde ``/sys/class/drm/card*/device/gpu_busy_percent`` (solo Linux)."""
        if not _es_linux():
            return None
        for ruta in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
            try:
                with open(ruta, encoding="utf-8") as archivo:
                    return float(archivo.read().strip())
            except (OSError, ValueError) as exc:
                log.debug("No se pudo leer %s (%s)", ruta, type(exc).__name__)
        return None

    @staticmethod
    def _leer_proceso_top() -> tuple[str, int, float] | None:
        """El proceso con más CPU (``process_iter``); ``None`` si ninguno consume."""
        mejor: tuple[str, int, float] | None = None
        try:
            for proceso in psutil.process_iter(["name", "pid", "cpu_percent"]):
                info = getattr(proceso, "info", {}) or {}
                pid = info.get("pid")
                cpu = info.get("cpu_percent")
                if not isinstance(pid, int) or pid == 0 or not isinstance(cpu, (int, float)):
                    continue
                if mejor is None or cpu > mejor[2]:
                    mejor = (str(info.get("name") or "?")[:60], pid, float(cpu))
        except Exception as exc:
            log.debug("process_iter falló (%s)", type(exc).__name__)
            return None
        if mejor is None or mejor[2] <= 0:
            return None
        return mejor

    async def _leer_nvidia(self) -> tuple[float | None, float | None, str | None] | None:
        """``nvidia-smi`` (timeout 2 s, como mucho cada 15 s); ``None`` sin NVIDIA."""
        if not self._nvidia_buscado:
            self._nvidia_buscado = True
            self._ruta_nvidia = shutil.which("nvidia-smi")
        if not self._ruta_nvidia:
            return None
        ahora = time.monotonic()
        if ahora - self._nvidia_ts < INTERVALO_NVIDIA_S:
            return self._nvidia
        self._nvidia_ts = ahora
        resultado = await _correr(
            [self._ruta_nvidia, "--query-gpu=utilization.gpu,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            timeout=TIMEOUT_NVIDIA_S, procesos=self._procesos)
        if resultado is None or resultado[0] != 0:
            self._nvidia = None
            return None
        lineas = [l for l in _decodificar(resultado[1]).splitlines() if l.strip()]
        if not lineas:
            self._nvidia = None
            return None
        partes = [p.strip() for p in lineas[0].split(",", 2)]
        uso = _numero(partes[0]) if len(partes) > 0 else None
        temperatura = _numero(partes[1]) if len(partes) > 1 else None
        nombre = partes[2][:60] if len(partes) > 2 and partes[2] else None
        self._nvidia = (uso, temperatura, nombre)
        return self._nvidia

    async def bucle_telemetria(self, parar: asyncio.Event, periodo_s: float = 3.0) -> None:
        """Publica ``pc.telemetria`` cada ``periodo_s`` y ``pc.alerta`` por flanco. Nunca muere."""
        if psutil is None:
            log.info("Telemetría OFF: psutil no instalado")
            return
        periodo = max(0.5, float(periodo_s))
        while not parar.is_set():
            inicio = time.monotonic()
            try:
                lectura = await self.telemetria()
                self._publicar("pc.telemetria", lectura.a_dict())
                for alerta in self._evaluar_alertas(lectura):
                    if alerta["critica"]:
                        log.warning("Alerta crítica: %s", alerta["detalle"])
                    else:
                        log.info("Alerta: %s", alerta["detalle"])
                    self._publicar("pc.alerta", alerta)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Iteración de telemetría fallida (%s)", type(exc).__name__)
            espera = max(0.05, periodo - (time.monotonic() - inicio))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(parar.wait(), timeout=espera)

    def _evaluar_alertas(self, lectura: Telemetria, ahora: float | None = None) -> list[dict[str, Any]]:
        """Alertas por flanco con persistencia y refractario de 10 min por tipo (§10.17)."""
        if ahora is None:
            ahora = self._reloj()
        valores: dict[str, float | None] = {
            "temperatura_cpu": lectura.temp_cpu,
            "temperatura_gpu": lectura.temp_gpu,
            "disco": lectura.disco_pct,
            "ram": lectura.ram_pct if lectura.ram_total_gb > 0 else None,
            "cpu": lectura.cpu_pct,
        }
        alertas: list[dict[str, Any]] = []
        for tipo, regla in _REGLAS_ALERTA.items():
            estado = self._alertas[tipo]
            valor = valores.get(tipo)
            if valor is None or valor < regla.umbral:
                estado.consecutivas = 0
                estado.desde = None
                estado.alertada = False
                continue
            estado.consecutivas += 1
            if estado.desde is None:
                estado.desde = ahora
            sostenido = ahora - estado.desde
            if estado.alertada or estado.consecutivas < regla.muestras or sostenido < regla.segundos:
                continue
            if estado.ultima_ts is not None and ahora - estado.ultima_ts < REFRACTARIO_ALERTA_S:
                continue   # refractario: se avisará si el episodio sigue cuando venza
            estado.alertada = True
            estado.ultima_ts = ahora
            alertas.append({
                "tipo": tipo,
                "valor": float(valor),
                "umbral": float(regla.umbral),
                "critica": regla.critica,
                "detalle": self._detalle_alerta(tipo, valor, sostenido, lectura),
            })
        return alertas

    @staticmethod
    def _detalle_alerta(tipo: str, valor: float, sostenido: float, lectura: Telemetria) -> str:
        """Detalle J.A.R.V.I.S. con cifras exactas, p. ej. "CPU a 94 °C sostenidos 9 s; proceso …"."""
        segundos = formatear_decimal(sostenido, 0)
        if tipo == "temperatura_cpu":
            texto = f"CPU a {formatear_decimal(valor, 0)} °C sostenidos {segundos} s"
        elif tipo == "temperatura_gpu":
            nombre = f"GPU {lectura.gpu_nombre}" if lectura.gpu_nombre else "GPU"
            texto = f"{nombre} a {formatear_decimal(valor, 0)} °C sostenidos {segundos} s"
        elif tipo == "disco":
            return f"Disco al {formatear_decimal(valor, 0)} % de su capacidad"
        elif tipo == "ram":
            texto = (f"RAM al {formatear_decimal(valor, 0)} % ({formatear_decimal(lectura.ram_usada_gb, 1)} "
                     f"de {formatear_decimal(lectura.ram_total_gb, 1)} GB) sostenida {segundos} s")
        else:
            texto = f"CPU al {formatear_decimal(valor, 0)} % sostenido {segundos} s"
        if lectura.proceso_top is not None:
            nombre_proc, pid, cpu = lectura.proceso_top
            texto += f"; proceso {nombre_proc} (PID {pid}) al {formatear_decimal(cpu, 0)} %"
        return texto

    # ------------------------------------------------------------------
    # Eventos de Hyprland
    # ------------------------------------------------------------------

    async def bucle_eventos_hypr(self, parar: asyncio.Event) -> None:
        """Escucha Hyprland y publica ``pc.ventana`` (≤ 1 cada 300 ms; la última de una ráfaga gana)."""
        if not self.hypr.disponible:
            log.debug("Hyprland no disponible: sin eventos de ventana")
            return
        try:
            await self.hypr.eventos(self._al_evento_hypr, parar)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("El bucle de eventos de Hyprland terminó con un error (%s)", type(exc).__name__)
        finally:
            tarea = self._tarea_ventana
            if tarea is not None and not tarea.done():
                tarea.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tarea

    async def _al_evento_hypr(self, evento: str, datos: str) -> None:
        """Solo ``activewindow``: guarda el último y programa su procesado con el límite de 300 ms."""
        if evento != "activewindow":
            return
        self._ventana_pendiente = datos
        if self._tarea_ventana is None or self._tarea_ventana.done():
            self._tarea_ventana = asyncio.ensure_future(self._procesar_ventanas())

    async def _procesar_ventanas(self) -> None:
        """Procesa la ventana pendiente respetando ≥ 300 ms entre publicaciones."""
        while self._ventana_pendiente is not None:
            espera = self._ultima_ventana_ts + _VENTANA_MIN_S - time.monotonic()
            if espera > 0:
                await asyncio.sleep(espera)   # mientras tanto, llegan eventos: gana el último
            datos = self._ventana_pendiente
            self._ventana_pendiente = None
            if datos is None:
                return
            self._ultima_ventana_ts = time.monotonic()
            try:
                await self._publicar_ventana(datos)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("No se pudo procesar la ventana activa (%s)", type(exc).__name__)

    async def _publicar_ventana(self, datos: str) -> None:
        """Consulta ``activewindow`` y publica ``pc.ventana``; si falla, usa los datos del evento."""
        ventana = await self.hypr.ventana_activa()
        if ventana is not None:
            pid = ventana.get("pid")
            direccion = ventana.get("address")
            carga = {
                "clase": str(ventana.get("class") or ""),
                "titulo": str(ventana.get("title") or "")[:200],
                "direccion": str(direccion) if direccion else None,
                "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            }
        else:
            clase, _, titulo = datos.partition(",")
            carga = {"clase": clase, "titulo": titulo[:200], "direccion": None, "pid": None}
        if not carga["clase"] and not carga["titulo"]:
            return  # ninguna ventana enfocada
        self._publicar("pc.ventana", carga)

    # ------------------------------------------------------------------
    # Ejecución de acciones
    # ------------------------------------------------------------------

    async def ejecutar(self, accion: AccionPC) -> ResultadoAccion:
        """Autoriza internamente ``accion`` y la despacha. Nunca lanza.

        Sin ``permisos`` (solo tests) se usa ``CompuertaPermisos.evaluar`` y se rechaza todo
        lo que no sea ``permitida``.
        """
        tipo = getattr(accion, "tipo", "")
        try:
            if not isinstance(accion, AccionPC):
                return ResultadoAccion(False, "Acción mal formada.", accion_tipo=str(tipo))
            if self.permisos is not None:
                decision = await self.permisos.autorizar(accion)
                aprobada = bool(decision.aprobada)
            else:
                decision = self._compuerta_local().evaluar(accion)
                aprobada = decision.veredicto == "permitida"
            if not aprobada:
                return ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}", accion_tipo=tipo)
            return await self._despachar_accion(accion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Error al ejecutar la acción %s (%s)", tipo, type(exc).__name__)
            return ResultadoAccion(False, "Error interno al ejecutar la acción.", accion_tipo=str(tipo))

    async def _despachar_accion(self, accion: AccionPC) -> ResultadoAccion:
        """Tabla de despacho de §10.17 (la acción ya está autorizada)."""
        tipo = accion.tipo
        args = accion.args
        if tipo == "lanzar_app":
            return await self._lanzar_app(str(args.get("alias", "")))
        if tipo == "enfocar_nexus":
            if self.nexus is None:
                return ResultadoAccion(False, "Nexus IDE no está integrado en esta instalación.", accion_tipo=tipo)
            # CONTRATO: NexusLink.enfocar() -> bool (§10.14).
            ok = bool(await self.nexus.enfocar())
            return ResultadoAccion(ok, "Nexus IDE al frente." if ok else "No encontré la ventana de Nexus IDE.",
                                   accion_tipo=tipo)
        if tipo == "ir_workspace":
            if not self.hypr.disponible:
                return ResultadoAccion(False, "Hyprland no disponible en este sistema.", accion_tipo=tipo)
            n = _entero_arg(args.get("n"))
            ok = n is not None and await self.hypr.ir_workspace(n)
            return ResultadoAccion(ok, f"Espacio {n} activo." if ok else "No pude cambiar de espacio de trabajo.",
                                   datos={"n": n}, accion_tipo=tipo)
        if tipo == "cerrar_ventana":
            if not self.hypr.disponible:
                return ResultadoAccion(False, "Hyprland no disponible en este sistema.", accion_tipo=tipo)
            direccion = args.get("direccion")
            ok = isinstance(direccion, str) and await self.hypr.cerrar_direccion(direccion)
            if not ok:
                return ResultadoAccion(False, "No pude cerrar esa ventana: ya no existe o es de R.A.M.S.",
                                       accion_tipo=tipo)
            clase = str(args.get("clase") or "").strip()
            titulo = " ".join(str(args.get("titulo") or "").split())[:40]
            if clase and titulo:
                mensaje = f"Cerré {clase} «{titulo}»."
            elif clase:
                mensaje = f"Cerré {clase}."
            else:
                mensaje = "Cerré la ventana."
            return ResultadoAccion(True, mensaje, accion_tipo=tipo)
        if tipo == "volumen":
            return await self._volumen(_entero_arg(args.get("nivel")))
        if tipo == "media":
            return await self._media(str(args.get("orden", "")))
        if tipo in ("captura_pantalla", "vistazo"):
            return ResultadoAccion(False, "Acción no soportada por ejecutar", accion_tipo=tipo)
        return ResultadoAccion(False, "Tipo de acción no soportado.", accion_tipo=tipo)

    async def _lanzar_app(self, alias: str) -> ResultadoAccion:
        tipo = "lanzar_app"
        comando = self.settings.apps_permitidas.get(alias)
        if not (isinstance(comando, str) and SAFE_CLASS_RE.fullmatch(comando)):
            return ResultadoAccion(False, f"«{alias[:40]}» no está en la lista permitida.", accion_tipo=tipo)
        if self.hypr.disponible:
            ok = await self.hypr.lanzar(alias)
            return ResultadoAccion(ok, f"Abrí {alias}." if ok else f"No pude abrir {alias}.",
                                   datos={"alias": alias}, accion_tipo=tipo)
        if not _es_linux():
            return ResultadoAccion(False, "Abrir aplicaciones no está soportado en este sistema.", accion_tipo=tipo)
        ruta = shutil.which(comando)
        if not ruta:
            return ResultadoAccion(False, f"No encuentro «{comando}» en este sistema.", accion_tipo=tipo)
        try:
            proc = await asyncio.create_subprocess_exec(
                ruta,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            log.info("No se pudo lanzar %s (%s)", comando, type(exc).__name__)
            return ResultadoAccion(False, f"No pude abrir {alias}.", accion_tipo=tipo)
        # La app sigue viva aunque R.A.M.S. se apague; solo se recoge su código de salida.
        espera = asyncio.ensure_future(proc.wait())
        self._esperas.add(espera)
        espera.add_done_callback(self._esperas.discard)
        return ResultadoAccion(True, f"Abrí {alias}.", datos={"alias": alias}, accion_tipo=tipo)

    async def _volumen(self, nivel: int | None) -> ResultadoAccion:
        tipo = "volumen"
        if nivel is None or not 0 <= nivel <= 100:
            return ResultadoAccion(False, "Nivel de volumen no válido.", accion_tipo=tipo)
        if not _es_linux():
            return ResultadoAccion(False, "Control de volumen no disponible en este sistema.", accion_tipo=tipo)
        wpctl = shutil.which("wpctl")
        if not wpctl:
            return ResultadoAccion(False, "wpctl no está instalado: no puedo cambiar el volumen.", accion_tipo=tipo)
        resultado = await _correr([wpctl, "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", f"{nivel}%"],
                                  timeout=TIMEOUT_HYPRCTL_S, procesos=self._procesos)
        ok = resultado is not None and resultado[0] == 0
        return ResultadoAccion(ok, f"Volumen al {nivel} %." if ok else "No pude cambiar el volumen.",
                               datos={"nivel": nivel}, accion_tipo=tipo)

    async def _media(self, orden: str) -> ResultadoAccion:
        tipo = "media"
        argumento = _ORDENES_PLAYERCTL.get(orden)
        if argumento is None:
            return ResultadoAccion(False, "Orden multimedia no válida.", accion_tipo=tipo)
        if not _es_linux():
            return ResultadoAccion(False, "Control multimedia no disponible en este sistema.", accion_tipo=tipo)
        playerctl = shutil.which("playerctl")
        if not playerctl:
            return ResultadoAccion(False, "playerctl no está instalado: no puedo controlar la música.",
                                   accion_tipo=tipo)
        resultado = await _correr([playerctl, argumento], timeout=TIMEOUT_HYPRCTL_S, procesos=self._procesos)
        if resultado is None:
            return ResultadoAccion(False, "El reproductor no respondió.", accion_tipo=tipo)
        if resultado[0] != 0:
            return ResultadoAccion(False, "No hay ningún reproductor activo.", accion_tipo=tipo)
        return ResultadoAccion(True, _MENSAJES_MEDIA[orden], datos={"orden": orden}, accion_tipo=tipo)

    # ------------------------------------------------------------------
    # Diagnóstico, arranque y cierre
    # ------------------------------------------------------------------

    def _diagnostico_camara(self) -> DiagnosticoModulo:
        nombre = "Lóbulo visual (cámara)"
        if cv2 is None:
            return DiagnosticoModulo("camara", nombre, EstadoModulo.OFF, "OpenCV no instalado")
        if not self.camara_habilitada:
            return DiagnosticoModulo("camara", nombre, EstadoModulo.OFF, "deshabilitado")
        if self._tipo_detector() is None:
            return DiagnosticoModulo("camara", nombre, EstadoModulo.DEGRADADO, "sin detector de rostros")
        return DiagnosticoModulo("camara", nombre, EstadoModulo.EN_ESPERA, "")

    async def _diagnostico_telemetria(self) -> DiagnosticoModulo:
        nombre = "Telemetría"
        if psutil is None:
            return DiagnosticoModulo("telemetria", nombre, EstadoModulo.OFF, "psutil no instalado")
        inicio = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
            # La primera llamada a cpu_percent(None) siempre da 0: se descarta aquí.
            await asyncio.wait_for(loop.run_in_executor(self._ejecutor, self._cebar_psutil), timeout=3.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La telemetría no responde (%s)", type(exc).__name__)
            return DiagnosticoModulo("telemetria", nombre, EstadoModulo.DEGRADADO, "sin lectura del sistema")
        latencia = int((time.monotonic() - inicio) * 1000)
        return DiagnosticoModulo("telemetria", nombre, EstadoModulo.OK, "", latencia_ms=latencia)

    @staticmethod
    def _cebar_psutil() -> None:
        psutil.cpu_percent(None)
        psutil.virtual_memory()

    async def _diagnostico_hyprland(self) -> DiagnosticoModulo:
        nombre = "Hyprland"
        if not self.hypr.disponible:
            return DiagnosticoModulo("hyprland", nombre, EstadoModulo.OFF,
                                     self.hypr.motivo_no_disponible or "no disponible en este sistema")
        inicio = time.monotonic()
        # En paralelo y con plazo corto: dos consultas seguidas de 3 s (más la espera tras
        # matar un hyprctl colgado) superarían el plazo de 4 s con el que main envuelve
        # iniciar(), y se perderían también los diagnósticos que sí funcionaron (§6.4).
        dialecto, version = await asyncio.gather(
            self.hypr.detectar_dialecto(timeout=TIMEOUT_HYPRCTL_DIAG_S),
            self.hypr.consultar("version", timeout=TIMEOUT_HYPRCTL_DIAG_S),
        )
        latencia = int((time.monotonic() - inicio) * 1000)
        if version is None:
            return DiagnosticoModulo("hyprland", nombre, EstadoModulo.DEGRADADO,
                                     f"hyprctl no responde ({dialecto})", latencia_ms=latencia)
        return DiagnosticoModulo("hyprland", nombre, EstadoModulo.OK, dialecto, latencia_ms=latencia)

    async def _diagnostico_nexus(self) -> DiagnosticoModulo:
        nombre = "Nexus IDE"
        if self.nexus is None:
            return DiagnosticoModulo("nexus", nombre, EstadoModulo.OFF,
                                     self._error_nexus or "módulo Nexus no instalado")
        # CONTRATO: NexusLink.diagnostico() -> DiagnosticoModulo (clave "nexus", §10.14).
        diagnostico = getattr(self.nexus, "diagnostico", None)
        if not callable(diagnostico):
            return DiagnosticoModulo("nexus", nombre, EstadoModulo.DEGRADADO, "diagnóstico no disponible")
        resultado = diagnostico()
        if asyncio.iscoroutine(resultado) or isinstance(resultado, asyncio.Future):
            resultado = await resultado
        if isinstance(resultado, DiagnosticoModulo):
            return resultado
        return DiagnosticoModulo("nexus", nombre, EstadoModulo.DEGRADADO, "diagnóstico no válido")

    async def iniciar(self) -> list[DiagnosticoModulo]:
        """Diagnósticos reales de ``camara``, ``telemetria``, ``hyprland`` y ``nexus``. Nunca lanza.

        La cámara **no** se abre aquí: queda "EN ESPERA" (o OFF/DEGRADADO). En Hyprland se
        detecta el dialecto de dispatch. Los diagnósticos corren en paralelo, cada uno con
        ``PLAZO_DIAGNOSTICO_S`` (3,5 s): el total queda por debajo de los 4 s con los que main
        envuelve esta llamada, así que un diagnóstico lento solo se marca a sí mismo.
        """
        async def seguro(clave: str, nombre: str, corrutina: Awaitable[DiagnosticoModulo]) -> DiagnosticoModulo:
            try:
                return await asyncio.wait_for(corrutina, timeout=PLAZO_DIAGNOSTICO_S)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                log.warning("Diagnóstico de %s sin respuesta en %s s", clave,
                            formatear_decimal(PLAZO_DIAGNOSTICO_S, 1))
                return DiagnosticoModulo(clave, nombre, EstadoModulo.DEGRADADO, "sin respuesta a tiempo")
            except Exception as exc:
                log.warning("Diagnóstico de %s fallido (%s)", clave, type(exc).__name__)
                return DiagnosticoModulo(clave, nombre, EstadoModulo.DEGRADADO, "error en el diagnóstico")

        try:
            camara = self._diagnostico_camara()
        except Exception as exc:
            log.warning("Diagnóstico de cámara fallido (%s)", type(exc).__name__)
            camara = DiagnosticoModulo("camara", "Lóbulo visual (cámara)", EstadoModulo.DEGRADADO,
                                       "error en el diagnóstico")
        telemetria, hyprland, nexus = await asyncio.gather(
            seguro("telemetria", "Telemetría", self._diagnostico_telemetria()),
            seguro("hyprland", "Hyprland", self._diagnostico_hyprland()),
            seguro("nexus", "Nexus IDE", self._diagnostico_nexus()),
        )
        for diagnostico in (camara, telemetria, hyprland, nexus):
            log.info("Diagnóstico %s: %s%s", diagnostico.clave, diagnostico.estado.value,
                     f" ({diagnostico.detalle})" if diagnostico.detalle else "")
        return [camara, telemetria, hyprland, nexus]

    async def cerrar(self) -> None:
        """Cierra NexusLink, cancela el vistazo en curso, mata subprocesos y apaga el executor."""
        if self._cerrado:
            return
        self._cerrado = True
        try:
            if self._parpadeo_actual is not None:
                self._parpadeo_actual.cancelar.set()
            tarea = self._tarea_ventana
            if tarea is not None and not tarea.done():
                tarea.cancel()
            if self.nexus is not None:
                cerrar_nexus = getattr(self.nexus, "cerrar", None)
                if callable(cerrar_nexus):
                    try:
                        await asyncio.wait_for(cerrar_nexus(), timeout=3.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.warning("Cierre de NexusLink fallido (%s)", type(exc).__name__)
            self.hypr._matar_procesos()
            for proc in list(self._procesos):
                _matar(proc)
            self._procesos.clear()
            for espera in list(self._esperas):
                espera.cancel()
            self._esperas.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Error al cerrar el lóbulo visión/PC (%s)", type(exc).__name__)
        finally:
            self._ejecutor.shutdown(wait=False, cancel_futures=True)
            log.debug("Lóbulo visión/PC cerrado")


def _entero_arg(valor: Any) -> int | None:
    """Entero de un argumento de acción (``int`` o texto de dígitos); otro ⇒ ``None``."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str):
        texto = valor.strip()
        if texto.isascii() and texto.isdigit() and len(texto) <= 4:
            return int(texto)
    return None
