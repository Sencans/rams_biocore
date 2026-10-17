"""Percepción de Nexus IDE para Amadeus: la "observación del sujeto de pruebas".

Qué hace (docs/ARQUITECTURA.md §10.14; Nexus no tiene API oficial, ver informe 02):
- **Procesos** (psutil): detecta Nexus IDE (proceso principal sin ``--type=``), su consumo
  (RSS y CPU), el shell de su terminal integrada (hijo de un *renderer*), el workspace (``cwd``
  de ese shell o ``NEXUS_WORKSPACE``) y los trabajos lanzados en esa terminal.
- **Ventana y foco**: solo a través de un ``ProveedorHypr`` (``ControlHyprland`` de
  ``aura_vision_pc``). Este módulo **nunca** llama a ``hyprctl`` directamente.
- **Guardados recientes**: recorrido acotado del workspace (3 000 archivos) que excluye
  dependencias, cachés y **secretos** (``.env``, claves, certificados, credenciales).
- **Colaboración** opcional por WebSocket ``:3737`` (token de la sesión ``Ctrl+Alt+L``): lee el
  chat del equipo, contesta en él y de los ``sync`` guarda **solo los tamaños** de los archivos.
  **Nunca envía ``publish``**: Amadeus no escribe en el disco del usuario.
- **Sondeo de seguridad** del servidor remoto ``:9090`` (ejecuta comandos sin autenticación).
- ``bucle_vigilancia`` publica ``nexus.estado`` cada 10 s y ``nexus.evento`` (``foco``,
  ``trabajo_fallido_rapido``, ``trabajo_terminado``, ``riesgo``, ``colab_chat``) con los límites
  de frecuencia aplicados aquí, en el productor.

Privacidad: fuera del módulo solo sale ``cmd_corto`` (nunca el comando completo ni su ``cwd``)
y el último componente del workspace. La instantánea del proyecto (solo tamaños) nunca se
guarda en la memoria de R.A.M.S.

Concurrencia: todo lo de psutil y ``os.walk`` corre en el executor propio
``ThreadPoolExecutor(1, "rams-nexus")`` (§6.1), que :meth:`NexusLink.cerrar` apaga con
``shutdown(wait=False, cancel_futures=True)`` antes que nada (así ocurre aunque el cierre se
cancele). Cada espera al executor lleva plazo y, si una tarea se queda colgada en el único hilo,
nada se encola detrás de ella: la vigilancia sigue atendiendo el chat, el foco y ``parar``.
Lo de Hyprland y la colaboración va con ``await``.

Dependencias opcionales: ``psutil`` (procesos) y ``websockets`` ≥ 14 (colaboración).

Degradación: sin psutil, ``esta_corriendo()`` devuelve ``False`` y el diagnóstico es
``OFF ("psutil no instalado")``; sin ``hypr`` o sin Hyprland, ``ventana=None`` y
``enfocado=False``; sin websockets no hay colaboración. En Windows funcionan la detección por
procesos, el workspace y los trabajos (shells ``powershell.exe``, ``pwsh.exe`` y ``cmd.exe``).
Ningún método público propaga excepciones.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

from core.bus import BusEventos
from core.config import Settings, redactar
from core.contracts import DiagnosticoModulo, EstadoModulo, ProveedorHypr

try:
    import psutil  # opcional: detección por procesos
except ImportError:  # sin psutil: no se detecta Nexus (diagnóstico OFF)
    psutil = None

try:
    from websockets.asyncio.client import connect as _ws_connect  # websockets ≥ 14
except ImportError:  # sin websockets: sin colaboración
    _ws_connect = None

__all__ = ["EstadoNexus", "NexusLink", "cmd_corto"]

log = logging.getLogger("rams.nexus")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_CLASE_RE: Final = re.compile(r"^nexus[ _-]?ide$", re.IGNORECASE)   # 'nexus-ide' (fuente) / 'Nexus IDE' (empaquetado)
_TITULO_RE: Final = re.compile(r"^nexus ide\b", re.IGNORECASE)      # 'Nexus IDE' / 'Nexus IDE - PC Desktop App'
_DIRECCION_RE: Final = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_NOMBRES_NEXUS: Final = frozenset({"nexus-ide", "nexus ide"})
# Nombres de shell normalizados (minúsculas y sin ".exe"): Linux + Windows.
_SHELLS: Final = frozenset({"bash", "zsh", "fish", "sh", "dash", "pwsh", "powershell", "cmd"})
_HIJOS_IGNORADOS: Final = frozenset({"conhost", "openconsole"})     # hosts de consola de Windows
_DIRS_SALTAR: Final = frozenset({"node_modules", ".git", "dist", "build", ".cache", ".next",
                                 "__pycache__", ".venv", "venv"})
_SECRETO_RE: Final = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx|keystore|crt)|id_rsa.*|\.npmrc|\.netrc|credentials.*|.*\.secret)$",
    re.IGNORECASE,
)
_PREFIJOS_TRABAJO: Final = ("python", "node", "npm", "npx", "cargo", "make", "go", "pytest", "deno")
_RE_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_RE_SEPARADOR: Final = re.compile(r"[\\/]")
_RE_SWITCH_WINDOWS: Final = re.compile(r"^/[A-Za-z?]{1,2}$")       # "cmd /c ...", "cmd /k ..."

# Límites de los eventos (§10.14).
VIDA_FALLO_RAPIDO_S: Final = 5.0
VIDA_TRABAJO_LARGO_S: Final = 300.0
REFRACTARIO_FALLO_S: Final = 300.0          # por cmd_corto
MAX_FALLOS_HORA: Final = 3                  # trabajo_fallido_rapido en total
INTERVALO_TERMINADO_S: Final = 600.0        # trabajo_terminado: como mucho 1 cada 10 min
DETALLE_RIESGO: Final = "servidor remoto :9090 sin autenticación"

# Ritmos de la vigilancia (atributos del módulo: los tests pueden acortarlos).
INTERVALO_SONDEO_S: float = 3600.0          # sondeo de seguridad: al arrancar y cada hora
INTERVALO_COLAB_S: float = 60.0             # reintento de colab_conectar
INTERVALO_FOCO_S: float = 2.0               # comprobación ligera del foco entre estados
PERIODO_RAPIDO_S: float = 0.5               # seguimiento de trabajos (vidas < 5 s)
PUERTO_REMOTO: Final = 9090

# Plazos del executor "rams-nexus" (un solo hilo): ninguna espera es ilimitada.
_TIEMPO_RECOLECCION_S: Final = 8.0          # tope de una recolección de psutil
_TIEMPO_ESCANEO_S: Final = 1.0              # pase rápido de seguimiento de trabajos (normalmente ms)
_TIEMPO_PIDS_S: Final = 3.0                 # PIDs de Nexus para la ventana/foco
_TIEMPO_GUARDADOS_S: Final = 30.0           # os.walk acotado a 3 000 archivos
_TIEMPO_MENSAJE_S: Final = 10.0             # parseo de un mensaje de colaboración grande (≤ 64 MiB)
_TIEMPO_HYPR_S: Final = 3.0
_TIEMPO_WS_S: Final = 5.0
# Cierre de la colaboración: en total < 3 s, el wait_for con el que AuraVisionPC.cerrar envuelve
# NexusLink.cerrar (despedida 0,5 + cierre del WebSocket 1,5 + espera del lector 0,5).
_TIEMPO_DESPEDIDA_S: Final = 0.5
_TIEMPO_CIERRE_WS_S: Final = 1.5
_TIEMPO_LECTOR_S: Final = 0.5
_CLOSE_TIMEOUT_WS_S: Final = 1.0            # close_timeout de websockets (por defecto 10 s): aborta el TCP
_MAX_COLA: Final = 200
_MAX_INSTANTANEA: Final = 20000
_UMBRAL_MENSAJE_GRANDE: Final = 256 * 1024  # mensajes de colaboración que se procesan en el executor

_ES_LINUX: Final = sys.platform.startswith("linux")

if psutil is not None:
    _ErrorPsutil: tuple[type[BaseException], ...] = (psutil.Error, OSError)
else:  # pragma: no cover - sin psutil estas rutas no se ejecutan
    _ErrorPsutil = (OSError,)


# ---------------------------------------------------------------------------
# Utilidades del módulo
# ---------------------------------------------------------------------------


def _basename(argumento: Any) -> str:
    """Último componente de una ruta (separando por ``/`` y ``\\``), sin caracteres de control."""
    limpio = _RE_CONTROL.sub("", str(argumento)).strip().strip("\"'")
    partes = [p for p in _RE_SEPARADOR.split(limpio) if p]
    return partes[-1] if partes else ""


def _nombre_normalizado(nombre: Any) -> str:
    """Nombre de proceso en minúsculas y sin ``.exe``."""
    texto = str(nombre or "").strip().lower()
    return texto[:-4] if texto.endswith(".exe") else texto


def _ultimo_componente(ruta: str | None) -> str | None:
    """Último componente de ``ruta`` (``None`` si no hay)."""
    if not ruta:
        return None
    base = _basename(ruta)
    return base or None


def _entero_o_none(valor: Any) -> int | None:
    try:
        if isinstance(valor, bool):
            return None
        return int(valor)
    except (TypeError, ValueError):
        return None


def _real(valor: Any, defecto: float = 0.0) -> float:
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if numero == numero and abs(numero) != float("inf") else defecto


def cmd_corto(cmdline: Sequence[str] | None) -> str:
    """Versión corta y segura de un comando, lo **único** del comando que sale del módulo.

    Basename de ``argv[0]`` más el basename de ``argv[1]`` si existe y no empieza por ``-`` (ni
    es un modificador de Windows como ``/c``). El resultado pasa por
    ``redactar(..., alta_entropia=True)`` y se trunca a 60 caracteres.

    >>> cmd_corto(["/usr/bin/python3", "/home/u/p/train.py", "--api-key", "sk_..."])
    'python3 train.py'
    """
    try:
        argv = [str(a) for a in (cmdline or ()) if a is not None]
    except TypeError:
        return ""
    if not argv:
        return ""
    partes: list[str] = []
    primero = _basename(argv[0])
    if primero:
        partes.append(primero)
    if len(argv) > 1:
        segundo = _RE_CONTROL.sub("", argv[1]).strip()
        if segundo and not segundo.startswith("-") and not _RE_SWITCH_WINDOWS.match(segundo):
            base = _basename(segundo)
            if base:
                partes.append(base)
    texto = redactar(" ".join(partes), alta_entropia=True)
    return texto[:60]


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass
class EstadoNexus:
    """Estado agregado de Nexus IDE (payload de ``nexus.estado`` vía :meth:`a_dict`)."""

    corriendo: bool = False
    pids: list[int] = field(default_factory=list)
    ventana: dict[str, Any] | None = None      # {"address","class","title","pid","workspace","xwayland","flotante","overlays"}
    enfocado: bool = False
    workspace: str | None = None               # ruta completa (uso interno; hacia Broca solo va el basename vía resumen_nexus)
    trabajos: list[dict[str, Any]] = field(default_factory=list)   # {"pid","cmd_corto","cpu","desde_s"} (sin cmd ni cwd completos)
    rss_mb: float = 0.0
    cpu_pct: float = 0.0
    colab_conectado: bool = False
    riesgo_9090: bool = False

    def a_dict(self) -> dict[str, Any]:
        """Exactamente las claves del payload de ``nexus.estado`` (§5), JSON-serializable.

        Por la regla de §5 ("los payloads nunca contienen rutas completas del workspace"), el
        ``workspace`` sale reducido a su último componente; ``resumen_nexus`` da el mismo
        resultado con la ruta completa o con el basename.
        """
        ventana: dict[str, Any] | None = None
        if isinstance(self.ventana, dict):
            v = self.ventana
            ventana = {
                "address": str(v["address"]) if v.get("address") else None,
                "class": str(v.get("class") or ""),
                "title": _RE_CONTROL.sub("", str(v.get("title") or ""))[:200],
                "pid": _entero_o_none(v.get("pid")),
                "workspace": str(v["workspace"]) if v.get("workspace") is not None else None,
                "xwayland": bool(v.get("xwayland")),
                "flotante": bool(v.get("flotante")),
                "overlays": max(0, _entero_o_none(v.get("overlays")) or 0),
            }
        trabajos = []
        for t in self.trabajos or ():
            if not isinstance(t, dict):
                continue
            trabajos.append({
                "pid": _entero_o_none(t.get("pid")) or 0,
                "cmd_corto": str(t.get("cmd_corto") or "")[:60],
                "cpu": round(_real(t.get("cpu")), 1),
                "desde_s": max(0, _entero_o_none(t.get("desde_s")) or 0),
            })
        return {
            "corriendo": bool(self.corriendo),
            "pids": [p for p in (_entero_o_none(x) for x in self.pids or ()) if p is not None],
            "ventana": ventana,
            "enfocado": bool(self.enfocado),
            "workspace": _ultimo_componente(self.workspace),
            "trabajos": trabajos,
            "rss_mb": round(_real(self.rss_mb), 1),
            "cpu_pct": round(_real(self.cpu_pct), 1),
            "colab_conectado": bool(self.colab_conectado),
            "riesgo_9090": bool(self.riesgo_9090),
        }


@dataclass(frozen=True)
class _Trabajo:
    """Proceso lanzado en la terminal de Nexus (seguimiento interno)."""

    pid: int
    creado: float              # create_time (época UNIX)
    argv0: str                 # basename de argv[0] en minúsculas
    cmd_corto: str
    shell_pid: int
    provisional: bool = False  # aún con la cmdline del shell (fork sin exec): se relee


@dataclass
class _Recoleccion:
    """Resultado de una recolección de psutil (hecha en el executor)."""

    corriendo: bool = False
    pids: list[int] = field(default_factory=list)
    rss_mb: float = 0.0
    cpu_pct: float = 0.0
    workspace: str | None = None
    trabajos: list[dict[str, Any]] = field(default_factory=list)
    shells: list[tuple[int, float]] = field(default_factory=list)


def _abortar_conexion(ws: Any) -> None:
    """Corta en seco el TCP de una conexión de colaboración que no se cerró a tiempo."""
    transporte = getattr(ws, "transport", None)
    abortar = getattr(transporte, "abort", None)
    if not callable(abortar):
        return
    try:
        abortar()
    except Exception as exc:  # transporte ya cerrado o en un estado inesperado
        log.debug("No se pudo abortar la conexión de colaboración (%s)", type(exc).__name__)


def _resumir_mensaje_colab(crudo: str, nombre: str) -> dict[str, Any] | None:
    """Parsea y filtra un mensaje del host de colaboración (función pura, apta para el executor).

    Devuelve ``None`` para lo que se ignora (eco propio, binarios, ``reject`` ajenos, tipos
    desconocidos). Un ``sync`` se reduce a ``{"t": "sync", "tamanos": {...}, "borrados": [...]}``:
    el contenido de los archivos se descarta aquí mismo.
    """
    try:
        msg = json.loads(crudo)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    tipo = msg.get("t")
    if tipo == "chat":
        emisor = _RE_CONTROL.sub("", str(msg.get("name") or "")).strip()
        if emisor == nombre:
            return None  # eco propio: el host re-difunde también al emisor
        return {"t": "chat", "name": emisor[:80], "text": str(msg.get("text") or "")[:2000]}
    if tipo == "sync":
        tamanos: dict[str, int] = {}
        borrados: list[str] = []
        archivos = msg.get("files")
        for f in archivos if isinstance(archivos, list) else ():
            if not isinstance(f, dict):
                continue
            ruta = f.get("path")
            if not isinstance(ruta, str) or not ruta or len(ruta) > 1024:
                continue
            if f.get("deleted"):
                borrados.append(ruta)
            elif isinstance(f.get("content"), str):
                tamanos[ruta] = len(f["content"].encode("utf-8", "surrogatepass"))
        return {"t": "sync", "tamanos": tamanos, "borrados": borrados}
    if tipo == "reject":
        if msg.get("to") != nombre:
            return None
        problemas = []
        issues = msg.get("issues")
        for i in issues if isinstance(issues, list) else ():
            if isinstance(i, dict):
                problemas.append({"path": str(i.get("path") or "")[:300], "error": str(i.get("error") or "")[:300]})
        return {"t": "reject", "from": str(msg.get("from") or "")[:80], "issues": problemas[:20]}
    if tipo == "sys":
        return {"t": "sys", "text": str(msg.get("text") or "")[:500]}
    return None


# ---------------------------------------------------------------------------
# NexusLink
# ---------------------------------------------------------------------------


class NexusLink:
    """Percepción de Nexus IDE. Si algo falla, cada método devuelve ``None``/``False``/``[]``."""

    def __init__(self, settings: Settings, *, hypr: ProveedorHypr | None = None,
                 nombre: str | None = None) -> None:
        self.settings = settings
        self.hypr = hypr
        limpio = _RE_CONTROL.sub("", str(nombre or settings.nombre_ia or "Amadeus")).strip()
        self.nombre: str = limpio[:40] or "Amadeus"
        self.eventos: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_COLA)
        self.instantanea: dict[str, int] = {}

        self._ejecutor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rams-nexus")
        self._en_vuelo: set[Future[Any]] = set()        # tareas enviadas al executor sin terminar
        self._tarea_atascada: Future[Any] | None = None  # superó su plazo y sigue ocupando el hilo
        self._cerrado = False
        self._reloj: Callable[[], float] = time.monotonic   # límites de frecuencia (inyectable)
        self._ahora: Callable[[], float] = time.time        # época, para vidas de procesos (inyectable)
        self._puerto_remoto: int = PUERTO_REMOTO

        # psutil (solo se tocan en el hilo del executor)
        self._cache_procs: dict[tuple[int, float], Any] = {}
        self._ws_inexistente_avisado = False

        # guardados_recientes
        self._lock_guardados = threading.Lock()
        self._mtimes: dict[str, float] = {}
        self._mtimes_raiz: str | None = None
        self._ts_guardados: float | None = None

        # estado y colaboración
        self._ultimo_estado: EstadoNexus | None = None
        self._recoleccion_ok = True
        self._pids_cache: tuple[float, list[int]] | None = None
        self._riesgo_9090 = False
        self._colab_3737_activo = False
        self._ts_sondeo: float | None = None
        self._ws: Any = None
        self._lector: asyncio.Task[None] | None = None
        self._proxy_soportado: bool | None = None

        # vigilancia
        self._shells: list[tuple[int, float]] = []
        self._seguimiento: dict[int, _Trabajo] = {}
        self._enfocado_previo = False
        self._modulo_publicado: tuple[bool, bool] | None = None
        self._riesgo_emitido = False
        self._fallos_por_cmd: dict[str, float] = {}
        self._fallos_hora: deque[float] = deque()
        self._ultimo_terminado: float | None = None

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------

    def _ejecutor_ocupado(self) -> bool:
        """``True`` si el executor tiene alguna tarea en cola o en marcha (solo desde el loop)."""
        self._en_vuelo = {t for t in self._en_vuelo if not t.done()}
        return bool(self._en_vuelo)

    async def _en_ejecutor(self, funcion: Callable[..., Any], *args: Any, plazo: float) -> Any:
        """Ejecuta ``funcion`` en el executor propio con un plazo (§6.1: ninguna espera ilimitada).

        - ``RuntimeError`` si el enlace ya se cerró (o el executor se apagó a mitad).
        - ``TimeoutError`` si no termina en ``plazo`` segundos, o **al instante** si una tarea
          anterior superó su plazo y sigue ocupando el único hilo: nada se encola detrás de ella.

        Al vencer el plazo, una tarea que aún estaba en cola se cancela; una que ya corría se deja
        terminar en segundo plano (su resultado se descarta) y queda marcada como atascada.
        """
        if self._cerrado:
            raise RuntimeError("NexusLink cerrado")
        atascada = self._tarea_atascada
        if atascada is not None:
            if not atascada.done():
                raise TimeoutError("el executor de Nexus sigue ocupado por una tarea atascada")
            self._tarea_atascada = None
        self._ejecutor_ocupado()                  # purga las tareas ya terminadas
        tarea = self._ejecutor.submit(funcion, *args)
        self._en_vuelo.add(tarea)
        futuro = asyncio.wrap_future(tarea)
        try:
            await asyncio.wait({futuro}, timeout=plazo)
        except asyncio.CancelledError:
            tarea.cancel()                        # si aún estaba en cola, no llega a correr
            futuro.cancel()
            raise
        if futuro.done():
            if futuro.cancelled():                # shutdown(cancel_futures=True) durante la espera
                raise RuntimeError("NexusLink cerrado")
            return futuro.result()
        en_cola = tarea.cancel()                  # False si ya corría (o acaba de terminar)
        futuro.cancel()                           # el resultado tardío se descarta sin avisos
        if not en_cola:
            if tarea.done():                      # terminó justo en el límite
                if tarea.cancelled():
                    raise RuntimeError("NexusLink cerrado")
                return tarea.result()
            self._tarea_atascada = tarea
        raise TimeoutError(f"tarea de Nexus sin terminar tras {plazo:.1f} s")

    def _hypr_disponible(self) -> bool:
        return self.hypr is not None and bool(getattr(self.hypr, "disponible", False))

    @property
    def colab_conectado(self) -> bool:
        """``True`` si hay una sesión de colaboración abierta."""
        return self._ws is not None

    # ------------------------------------------------------------------
    # Procesos (psutil; síncrono: se llama desde el executor)
    # ------------------------------------------------------------------

    def _procesos_principales(self) -> list[Any]:
        """Procesos principales de Nexus IDE (sin ``--type=``)."""
        if psutil is None:
            return []
        hallados: list[Any] = []
        vistos: set[int] = set()
        try:
            iterador = psutil.process_iter(["pid", "name"])
        except _ErrorPsutil as exc:
            log.debug("No se pudo enumerar procesos: %s", exc)
            return []
        for proc in iterador:
            try:
                info = getattr(proc, "info", None) or {}
                nombre = _nombre_normalizado(info.get("name") if info else proc.name())
                empaquetado = nombre in _NOMBRES_NEXUS
                electron = "electron" in nombre
                if not (empaquetado or electron) or proc.pid in vistos:
                    continue
                argv = [str(a) for a in (proc.cmdline() or [])]
                if any(a.startswith("--type=") for a in argv):
                    continue  # renderer, gpu, zygote, utility…
                if empaquetado or self._es_repo_nexus(proc, argv):
                    hallados.append(proc)
                    vistos.add(proc.pid)
            except _ErrorPsutil:
                continue
        return hallados

    @staticmethod
    def _es_repo_nexus(proc: Any, argv: list[str]) -> bool:
        """Electron lanzado desde el repositorio de Nexus (``npm start``)."""
        if "nexus-ide" in " ".join(argv).lower():
            return True
        try:
            cwd = proc.cwd()
        except _ErrorPsutil:
            return False
        if not cwd:
            return False
        paquete = Path(cwd) / "package.json"
        try:
            if not paquete.is_file() or paquete.stat().st_size > 1_000_000:
                return False
            datos = json.loads(paquete.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("package.json ilegible en un proceso electron: %s", type(exc).__name__)
            return False
        return isinstance(datos, dict) and datos.get("name") == "nexus-ide"

    def _cacheado(self, proc: Any) -> tuple[Any, tuple[int, float] | None]:
        """Objeto ``Process`` persistente (``cpu_percent(None)`` necesita la medida anterior)."""
        try:
            clave = (int(proc.pid), float(proc.create_time()))
        except _ErrorPsutil:
            return proc, None
        existente = self._cache_procs.get(clave)
        if existente is None:
            self._cache_procs[clave] = proc
            return proc, clave
        return existente, clave

    def _shells_de(self, principales: list[Any]) -> list[Any]:
        """Shells de la terminal integrada (hijos de un renderer de Nexus), el más antiguo primero."""
        if psutil is None:
            return []
        encontrados: dict[int, tuple[float, Any]] = {}
        for principal in principales:
            try:
                descendientes = principal.children(recursive=True)
            except _ErrorPsutil:
                continue
            for hijo in descendientes:
                try:
                    if hijo.pid in encontrados or _nombre_normalizado(hijo.name()) not in _SHELLS:
                        continue
                    padre = hijo.parent()
                    if padre is None:
                        continue
                    if not any(str(a).startswith("--type=renderer") for a in (padre.cmdline() or [])):
                        continue
                    encontrados[hijo.pid] = (float(hijo.create_time()), hijo)
                except _ErrorPsutil:
                    continue
        return [p for _, p in sorted(encontrados.values(), key=lambda par: par[0])]

    def _workspace_configurado(self) -> str | None:
        ruta = self.settings.nexus_workspace
        if ruta is None:
            return None
        try:
            if Path(ruta).is_dir():
                return str(ruta)
        except OSError as exc:
            log.debug("NEXUS_WORKSPACE inaccesible: %s", exc)
            return None
        if not self._ws_inexistente_avisado:
            self._ws_inexistente_avisado = True
            log.info("NEXUS_WORKSPACE no es un directorio; se detecta por el shell de Nexus")
        return None

    def _workspace_de(self, shells: list[Any]) -> str | None:
        configurado = self._workspace_configurado()
        if configurado:
            return configurado
        for shell in shells:
            try:
                cwd = shell.cwd()
            except _ErrorPsutil:
                continue
            if cwd:
                return str(cwd)
        return None

    def _detalle_trabajos(self, shells: list[Any], *, con_cwd: bool,
                          vistos: set[tuple[int, float]] | None = None) -> list[dict[str, Any]]:
        """Hijos directos de los shells: ``{"pid","cmd","cwd","cpu","desde_s","argv","shell_pid"}``.

        La CPU de cada trabajo suma la de sus descendientes (``npm run build`` delega en ``node``).
        """
        if psutil is None:
            return []
        ahora = self._ahora()
        trabajos: list[dict[str, Any]] = []
        for shell in shells:
            try:
                hijos = shell.children(recursive=False)
            except _ErrorPsutil:
                continue
            for hijo in hijos:
                try:
                    with hijo.oneshot():
                        nombre = _nombre_normalizado(hijo.name())
                        if nombre in _HIJOS_IGNORADOS or hijo.status() == psutil.STATUS_ZOMBIE:
                            continue
                        argv = [str(a) for a in (hijo.cmdline() or [])] or [hijo.name()]
                        creado = float(hijo.create_time())
                    cwd: str | None = None
                    if con_cwd:
                        try:
                            cwd = hijo.cwd() or None
                        except _ErrorPsutil:
                            cwd = None
                    cpu = 0.0
                    for proc in [hijo, *self._descendientes(hijo)]:
                        cacheado, clave = self._cacheado(proc)
                        if clave is not None and vistos is not None:
                            vistos.add(clave)
                        try:
                            cpu += float(cacheado.cpu_percent(None))
                        except _ErrorPsutil:
                            continue
                    trabajos.append({
                        "pid": int(hijo.pid),
                        "cmd": " ".join(argv)[:500],
                        "cwd": cwd,
                        "cpu": round(cpu, 1),
                        "desde_s": max(0, int(ahora - creado)),
                        "argv": argv,
                        "shell_pid": int(shell.pid),
                    })
                except _ErrorPsutil:
                    continue
        return trabajos

    @staticmethod
    def _descendientes(proc: Any) -> list[Any]:
        try:
            return list(proc.children(recursive=True))
        except _ErrorPsutil:
            return []

    def _recolectar(self) -> _Recoleccion:
        """Recolección completa (executor): procesos, consumo, shells, workspace y trabajos."""
        principales = self._procesos_principales()
        if not principales:
            self._cache_procs.clear()
            return _Recoleccion(workspace=None)
        vistos: set[tuple[int, float]] = set()
        rss = 0.0
        cpu = 0.0
        for principal in principales:
            for proc in [principal, *self._descendientes(principal)]:
                cacheado, clave = self._cacheado(proc)
                if clave is not None:
                    vistos.add(clave)
                try:
                    rss += float(cacheado.memory_info().rss)
                    cpu += float(cacheado.cpu_percent(None))
                except _ErrorPsutil:
                    continue
        shells = self._shells_de(principales)
        workspace = self._workspace_de(shells)
        detalle = self._detalle_trabajos(shells, con_cwd=False, vistos=vistos)
        for clave in [c for c in self._cache_procs if c not in vistos]:
            del self._cache_procs[clave]
        info_shells: list[tuple[int, float]] = []
        for shell in shells:
            try:
                info_shells.append((int(shell.pid), float(shell.create_time())))
            except _ErrorPsutil:
                continue
        return _Recoleccion(
            corriendo=True,
            pids=[int(p.pid) for p in principales],
            rss_mb=round(rss / 2**20, 1),
            cpu_pct=round(cpu, 1),
            workspace=workspace,
            trabajos=[{"pid": t["pid"], "cmd_corto": cmd_corto(t["argv"]), "cpu": t["cpu"],
                       "desde_s": t["desde_s"]} for t in detalle],
            shells=info_shells,
        )

    # --- API síncrona (para el executor o para herramientas) ---

    def esta_corriendo(self) -> bool:
        """``True`` si Nexus IDE está en ejecución (sin psutil: ``False``). Síncrono."""
        try:
            return bool(self._procesos_principales())
        except Exception as exc:  # nunca propaga (§3.1)
            log.debug("esta_corriendo falló: %s", exc)
            return False

    def raiz_workspace(self) -> str | None:
        """``NEXUS_WORKSPACE`` si es un directorio; si no, el ``cwd`` del shell más antiguo de Nexus."""
        try:
            configurado = self._workspace_configurado()
            if configurado:
                return configurado
            if psutil is None:
                return None
            return self._workspace_de(self._shells_de(self._procesos_principales()))
        except Exception as exc:
            log.debug("raiz_workspace falló: %s", exc)
            return None

    def trabajos_terminal(self) -> list[dict[str, Any]]:
        """Uso interno: ``[{"pid","cmd","cwd","cpu","desde_s"}]`` de la terminal de Nexus.

        Contiene el comando completo y su ``cwd``: **nunca** debe salir del módulo (hacia fuera
        solo va ``cmd_corto``).
        """
        try:
            shells = self._shells_de(self._procesos_principales())
            return [{k: t[k] for k in ("pid", "cmd", "cwd", "cpu", "desde_s")}
                    for t in self._detalle_trabajos(shells, con_cwd=True)]
        except Exception as exc:
            log.debug("trabajos_terminal falló: %s", exc)
            return []

    def guardados_recientes(self, max_archivos: int = 3000) -> list[str]:
        """Rutas relativas del workspace guardadas desde la llamada anterior (las más nuevas primero).

        La primera llamada (o tras cambiar de workspace) solo memoriza y devuelve ``[]``. Salta
        ``node_modules``, ``.git``, ``dist``, ``build``, ``.cache``, ``.next``, ``__pycache__``,
        ``.venv`` y ``venv``, y **excluye secretos** (``.env*``, claves, certificados,
        credenciales). Recorre como mucho ``max_archivos`` archivos. Síncrono (``os.walk``):
        desde el loop, usa :meth:`guardados_recientes_async`.
        """
        try:
            raiz = self.raiz_workspace()
            if not raiz:
                return []
            limite = max(1, int(max_archivos))
            with self._lock_guardados:
                return self._guardados_en(raiz, limite)
        except Exception as exc:
            log.debug("guardados_recientes falló: %s", exc)
            return []

    def _guardados_en(self, raiz: str, limite: int) -> list[str]:
        inicio = self._ahora()
        primera = self._mtimes_raiz != raiz
        previos = {} if primera else self._mtimes
        ts_previo = None if primera else self._ts_guardados
        nuevos: dict[str, float] = {}
        cambiados: list[tuple[float, str]] = []
        vistos = 0
        truncado = False

        def _al_error(exc: OSError) -> None:
            log.debug("No se pudo recorrer una carpeta del workspace: %s", exc.strerror or exc)

        for dirpath, dirnames, filenames in os.walk(raiz, onerror=_al_error):
            dirnames[:] = sorted(d for d in dirnames if d not in _DIRS_SALTAR)
            for nombre in sorted(filenames):
                vistos += 1
                if vistos > limite:
                    truncado = True
                    break
                ruta = os.path.join(dirpath, nombre)
                relativa = os.path.relpath(ruta, raiz).replace("\\", "/")
                if _SECRETO_RE.search(relativa):
                    continue
                try:
                    info = os.stat(ruta)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                mtime = float(info.st_mtime)
                nuevos[relativa] = mtime
                if primera:
                    continue
                previo = previos.get(relativa)
                if previo is None:
                    if ts_previo is not None and mtime > ts_previo:
                        cambiados.append((mtime, relativa))   # archivo nuevo guardado desde la última vez
                elif mtime > previo:
                    cambiados.append((mtime, relativa))
            if truncado:
                break
        if truncado:
            log.debug("Workspace con más de %d archivos: recorrido truncado", limite)
        self._mtimes = nuevos
        self._mtimes_raiz = raiz
        self._ts_guardados = inicio
        cambiados.sort(reverse=True)
        return [relativa for _, relativa in cambiados]

    async def guardados_recientes_async(self, max_archivos: int = 3000) -> list[str]:
        """:meth:`guardados_recientes` en el executor propio (no bloquea el loop)."""
        try:
            return await self._en_ejecutor(self.guardados_recientes, max_archivos,
                                           plazo=_TIEMPO_GUARDADOS_S)
        except Exception as exc:
            log.debug("guardados_recientes_async falló: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Hyprland (vía ProveedorHypr)
    # ------------------------------------------------------------------

    @staticmethod
    def _es_nexus(cliente: dict[str, Any], pids: set[int]) -> bool:
        pid = _entero_o_none(cliente.get("pid"))
        if pid is not None and pid in pids:
            return True
        for clave in ("class", "initialClass"):
            if _CLASE_RE.match(str(cliente.get(clave) or "")):
                return True
        for clave in ("title", "initialTitle"):
            if _TITULO_RE.match(str(cliente.get(clave) or "")):
                return True
        return False

    async def _consultar_hypr(self, que: str) -> Any:
        if not self._hypr_disponible():
            return None
        try:
            return await asyncio.wait_for(self.hypr.consultar(que), _TIEMPO_HYPR_S)  # type: ignore[union-attr]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Hyprland caído, timeout, JSON inesperado…
            log.debug("Consulta a Hyprland (%s) fallida: %s", que, type(exc).__name__)
            return None

    async def _pids_actuales(self) -> list[int]:
        """PIDs de Nexus (caché de 5 s; si no, recolección rápida en el executor)."""
        if self._pids_cache is not None and self._reloj() - self._pids_cache[0] < 5.0:
            return list(self._pids_cache[1])
        if psutil is None:
            return []
        try:
            procesos = await self._en_ejecutor(self._procesos_principales, plazo=_TIEMPO_PIDS_S)
            pids = [int(p.pid) for p in procesos]
        except Exception as exc:
            log.debug("No se pudieron obtener los PIDs de Nexus: %s", exc)
            return []
        self._pids_cache = (self._reloj(), pids)
        return pids

    async def _detectar_ventana_con(self, pids: Sequence[int]) -> dict[str, Any] | None:
        clientes = await self._consultar_hypr("clients")
        if not isinstance(clientes, list):
            return None
        conjunto = set(pids)
        candidatos = [c for c in clientes
                      if isinstance(c, dict) and c.get("mapped", True) and self._es_nexus(c, conjunto)]
        if not candidatos:
            return None
        # El overlay del compañero AIRI suele ser flotante y del tamaño del monitor: se prefiere el no flotante.
        candidatos.sort(key=lambda c: (bool(c.get("floating")), _entero_o_none(c.get("focusHistoryID")) or 999))
        v = candidatos[0]
        espacio = v.get("workspace")
        nombre_espacio = espacio.get("name") if isinstance(espacio, dict) else espacio
        return {
            "address": str(v["address"]) if v.get("address") else None,
            "class": str(v.get("class") or v.get("initialClass") or ""),
            "title": _RE_CONTROL.sub("", str(v.get("title") or ""))[:200],
            "pid": _entero_o_none(v.get("pid")),
            "workspace": str(nombre_espacio) if nombre_espacio is not None else None,
            "xwayland": bool(v.get("xwayland")),
            "flotante": bool(v.get("floating")),
            "overlays": len(candidatos) - 1,
        }

    async def _esta_enfocado_con(self, pids: Sequence[int]) -> bool:
        activa = await self._consultar_hypr("activewindow")
        if not isinstance(activa, dict) or not activa:
            return False
        return self._es_nexus(activa, set(pids))

    async def detectar_ventana(self) -> dict[str, Any] | None:
        """Ventana principal de Nexus en Hyprland (``None`` sin Hyprland o si no está)."""
        if not self._hypr_disponible():
            return None
        try:
            return await self._detectar_ventana_con(await self._pids_actuales())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("detectar_ventana falló: %s", exc)
            return None

    async def esta_enfocado(self) -> bool:
        """``True`` si la ventana activa de Hyprland es Nexus IDE."""
        if not self._hypr_disponible():
            return False
        try:
            return await self._esta_enfocado_con(await self._pids_actuales())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("esta_enfocado falló: %s", exc)
            return False

    async def enfocar(self) -> bool:
        """Trae Nexus al frente con ``ProveedorHypr.enfocar_direccion`` (nunca ``hyprctl`` directo)."""
        if not self._hypr_disponible():
            return False
        ventana = await self.detectar_ventana()
        direccion = (ventana or {}).get("address")
        if not direccion or not _DIRECCION_RE.match(str(direccion)):
            log.debug("No hay una ventana de Nexus que enfocar")
            return False
        try:
            ok = bool(await asyncio.wait_for(self.hypr.enfocar_direccion(str(direccion)),  # type: ignore[union-attr]
                                             _TIEMPO_HYPR_S))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("No se pudo enfocar Nexus: %s", type(exc).__name__)
            return False
        if ok:
            log.info("Nexus IDE enfocado")
        return ok

    # ------------------------------------------------------------------
    # Estado agregado
    # ------------------------------------------------------------------

    async def estado(self) -> EstadoNexus:
        """Estado agregado (psutil en el executor propio; Hyprland con ``await``). Nunca lanza."""
        est = EstadoNexus(colab_conectado=self.colab_conectado, riesgo_9090=self._riesgo_9090)
        if self._cerrado or psutil is None:
            self._recoleccion_ok = not self._cerrado
            return est
        self._recoleccion_ok = False
        try:
            rec: _Recoleccion = await self._en_ejecutor(self._recolectar, plazo=_TIEMPO_RECOLECCION_S)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:   # superó su plazo, o el executor sigue atascado
            log.debug("Recolección de procesos de Nexus sin respuesta: %s", exc)
            return est
        except Exception as exc:
            log.warning("No se pudo recolectar el estado de Nexus (%s)", type(exc).__name__)
            return est
        self._recoleccion_ok = True
        est.corriendo = rec.corriendo
        est.pids = list(rec.pids)
        est.rss_mb = rec.rss_mb
        est.cpu_pct = rec.cpu_pct
        est.workspace = rec.workspace
        est.trabajos = list(rec.trabajos)
        self._pids_cache = (self._reloj(), list(rec.pids))
        self._shells = list(rec.shells)
        if rec.corriendo and self._hypr_disponible():
            est.ventana = await self._detectar_ventana_con(rec.pids)
            est.enfocado = await self._esta_enfocado_con(rec.pids)
        self._ultimo_estado = est
        return est

    # ------------------------------------------------------------------
    # Seguridad
    # ------------------------------------------------------------------

    async def _http_get(self, host: str, puerto: int) -> str:
        """``GET /`` mínimo (HTTP/1.0) con plazos de 0,5 s (conexión) y 1 s (lectura)."""
        datos = bytearray()
        escritor: asyncio.StreamWriter | None = None
        try:
            lector, escritor = await asyncio.wait_for(asyncio.open_connection(host, puerto), 0.5)
            peticion = (f"GET / HTTP/1.0\r\nHost: {host}:{puerto}\r\nUser-Agent: RAMS-BioCore/0.1\r\n"
                        "Connection: close\r\n\r\n")
            escritor.write(peticion.encode("ascii", "replace"))
            loop = asyncio.get_running_loop()
            limite = loop.time() + 1.0
            await asyncio.wait_for(escritor.drain(), 1.0)
            while len(datos) < 4096:
                restante = limite - loop.time()
                if restante <= 0:
                    break
                trozo = await asyncio.wait_for(lector.read(4096 - len(datos)), restante)
                if not trozo:
                    break
                datos += trozo
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
            log.debug("Sondeo de %s:%d sin respuesta completa (%s)", host, puerto, type(exc).__name__)
        finally:
            if escritor is not None:
                try:
                    escritor.close()
                    await asyncio.wait_for(escritor.wait_closed(), 0.5)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("Cierre del sondeo de %d con error (%s)", puerto, type(exc).__name__)
        return bytes(datos).decode("utf-8", "replace")

    async def sondeo_seguridad(self) -> dict[str, bool]:
        """Sondea ``127.0.0.1:9090`` (servidor remoto de Nexus, sin autenticación) y el ``:3737``.

        Devuelve ``{"remoto_9090_sin_auth": bool, "colab_3737_activo": bool}``. Nunca usa
        ``/api/exec`` ni ``/api/fs``: solo un ``GET /``. Nunca lanza.
        """
        try:
            r9090, r3737 = await asyncio.gather(
                self._http_get("127.0.0.1", self._puerto_remoto),
                self._http_get(self.settings.nexus_colab_host, int(self.settings.nexus_colab_puerto)),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Sondeo de seguridad fallido: %s", exc)
            r9090, r3737 = "", ""
        texto_9090 = r9090 if isinstance(r9090, str) else ""
        texto_3737 = r3737 if isinstance(r3737, str) else ""
        resultado = {
            "remoto_9090_sin_auth": "<title>Nexus IDE" in texto_9090,
            "colab_3737_activo": texto_3737.startswith("HTTP/1.1 426") and "Solo WebSocket" in texto_3737,
        }
        self._riesgo_9090 = resultado["remoto_9090_sin_auth"]
        self._colab_3737_activo = resultado["colab_3737_activo"]
        self._ts_sondeo = self._reloj()
        return resultado

    # ------------------------------------------------------------------
    # Colaboración (WebSocket :3737)
    # ------------------------------------------------------------------

    def _url_colab(self, token: str) -> str:
        host = str(self.settings.nexus_colab_host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"ws://{host}:{int(self.settings.nexus_colab_puerto)}/?token={quote(token, safe='')}"

    def _opciones_ws(self) -> dict[str, Any]:
        opciones: dict[str, Any] = {"max_size": 64 * 2**20, "ping_interval": 20, "open_timeout": 3,
                                    "close_timeout": _CLOSE_TIMEOUT_WS_S, "compression": None}
        if self._proxy_soportado is None:
            try:
                self._proxy_soportado = "proxy" in inspect.signature(_ws_connect).parameters
            except (TypeError, ValueError):
                self._proxy_soportado = False
        if self._proxy_soportado:
            opciones["proxy"] = None  # conexión local directa aunque haya HTTP(S)_PROXY en el entorno
        return opciones

    async def colab_conectar(self, token: str | None = None) -> bool:
        """Se une a la sesión de colaboración de Nexus (``ws://host:3737/?token=…``).

        Solo funciona mientras el usuario aloja una sesión (``Ctrl+Alt+L`` → Alojar) y con su
        token. Al conectar anuncia la llegada con un mensaje ``sys``. Nunca lanza.
        """
        if self._cerrado:
            return False
        if self._ws is not None:
            return True
        if _ws_connect is None:
            log.debug("websockets no instalado: sin colaboración con Nexus")
            return False
        token = token or self.settings.nexus_colab_token
        if not token:
            log.debug("Sin NEXUS_COLLAB_TOKEN: no se intenta la colaboración")
            return False
        secretos = (*self.settings.secretos(), token)
        try:
            ws = await asyncio.wait_for(_ws_connect(self._url_colab(token), **self._opciones_ws()), _TIEMPO_WS_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 401 (token caducado), 426, conexión rechazada, timeout…
            log.info("Colaboración de Nexus no disponible: %s",
                     redactar(f"{type(exc).__name__}: {exc}", secretos)[:200])
            return False
        if self._cerrado or self._ws is not None:   # cerrar() (u otra conexión) llegó durante el handshake
            try:
                await asyncio.wait_for(ws.close(), _TIEMPO_CIERRE_WS_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Cierre de una conexión de colaboración sobrante (%s)", type(exc).__name__)
            return self._ws is not None and not self._cerrado
        self._ws = ws
        self._lector = asyncio.create_task(self._leer(ws), name="rams-nexus-colab")
        await self._enviar({"t": "sys", "text": f"{self.nombre} se unió a la sesión"})
        log.info("Conectada a la sesión de colaboración de Nexus IDE")
        return True

    def _encolar(self, mensaje: dict[str, Any]) -> None:
        """``put_nowait`` en :attr:`eventos`; con la cola llena se descarta el más antiguo."""
        try:
            self.eventos.put_nowait(mensaje)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.eventos.get_nowait()
        except asyncio.QueueEmpty:
            log.debug("Cola de colaboración llena y vacía a la vez")
        try:
            self.eventos.put_nowait(mensaje)
        except asyncio.QueueFull:
            log.debug("Cola de colaboración llena: mensaje descartado")

    def _aplicar_sync(self, resumen: dict[str, Any]) -> None:
        """Actualiza :attr:`instantanea` con tamaños (nunca contenido)."""
        for ruta in resumen.get("borrados") or ():
            self.instantanea.pop(ruta, None)
        for ruta, tamano in (resumen.get("tamanos") or {}).items():
            if ruta not in self.instantanea and len(self.instantanea) >= _MAX_INSTANTANEA:
                continue
            self.instantanea[ruta] = int(tamano)

    async def _leer(self, ws: Any) -> None:
        """Lector de la sesión: filtra, resume y encola (los ``sync`` no se encolan)."""
        try:
            async for crudo in ws:
                if not isinstance(crudo, str):
                    continue  # el host solo usa texto UTF-8
                if len(crudo) > _UMBRAL_MENSAJE_GRANDE:
                    # Nunca se parsea en el loop: si el executor está cerrado, atascado o tarda
                    # demasiado, el mensaje (casi siempre un ``sync``) se descarta.
                    try:
                        resumen = await self._en_ejecutor(_resumir_mensaje_colab, crudo, self.nombre,
                                                          plazo=_TIEMPO_MENSAJE_S)
                    except (TimeoutError, RuntimeError) as exc:
                        log.debug("Mensaje grande de colaboración descartado: %s", exc)
                        continue
                    finally:
                        crudo = ""
                else:
                    resumen = _resumir_mensaje_colab(crudo, self.nombre)
                crudo = ""  # el contenido de los archivos se descarta de inmediato
                if resumen is None:
                    continue
                if resumen["t"] == "sync":
                    self._aplicar_sync(resumen)
                    continue
                self._encolar(resumen)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # cierre anómalo, host caído, mensaje gigante…
            log.info("Sesión de colaboración de Nexus cerrada (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(ws.close(), 2.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc_cierre:
                log.debug("Cierre tras el fallo de la colaboración con error (%s)", type(exc_cierre).__name__)
        else:
            log.info("Sesión de colaboración de Nexus cerrada por el host")
        finally:
            if self._ws is ws:
                self._ws = None

    async def _enviar(self, mensaje: dict[str, Any]) -> bool:
        """Envía un mensaje ``chat`` o ``sys``. **Nunca** ``publish`` (§9.4)."""
        if mensaje.get("t") not in ("chat", "sys"):
            log.error("Se bloqueó un envío de tipo %r a la colaboración de Nexus", mensaje.get("t"))
            return False
        ws = self._ws
        if ws is None:
            return False
        try:
            await asyncio.wait_for(ws.send(json.dumps(mensaje, ensure_ascii=False)), _TIEMPO_WS_S)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("No se pudo enviar a la colaboración de Nexus (%s); se da por cerrada",
                     type(exc).__name__)
            if self._ws is ws:
                self._ws = None
            return False

    async def colab_decir(self, texto: str) -> bool:
        """Escribe en el chat del equipo: ``{"t":"chat","name":nombre,"text":texto[:2000]}`` (público)."""
        limpio = redactar(str(texto or ""), self.settings.secretos()).strip()
        if not limpio:
            return False
        return await self._enviar({"t": "chat", "name": self.nombre, "text": limpio[:2000]})

    async def colab_notificar(self, texto: str) -> bool:
        """Línea de sistema: ``{"t":"sys","text":f"⚗ {nombre}: {texto[:500]}"}``."""
        limpio = redactar(str(texto or ""), self.settings.secretos()).strip()
        if not limpio:
            return False
        return await self._enviar({"t": "sys", "text": f"⚗ {self.nombre}: {limpio[:500]}"})

    async def colab_cerrar(self) -> None:
        """Sale de la sesión de colaboración (idempotente; solo propaga la cancelación).

        Presupuesto total < 3 s (despedida 0,5 s, cierre del WebSocket 1,5 s y lector 0,5 s),
        por debajo del ``wait_for`` de 3 s de ``AuraVisionPC.cerrar``. Aunque se cancele a mitad,
        el ``finally`` suelta la conexión (abortando el TCP si no se cerró) y cancela el lector.
        """
        ws = self._ws
        lector = self._lector
        cerrada = ws is None
        try:
            if ws is not None:
                try:
                    await asyncio.wait_for(self._enviar({"t": "sys", "text": f"{self.nombre} salió de la sesión"}),
                                           _TIEMPO_DESPEDIDA_S)
                except TimeoutError:
                    log.debug("Despedida de la colaboración sin confirmar")
                if self._ws is ws:
                    self._ws = None
                try:
                    await asyncio.wait_for(ws.close(), _TIEMPO_CIERRE_WS_S)
                    cerrada = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("Cierre de la colaboración con error (%s)", type(exc).__name__)
        finally:
            if ws is not None and self._ws is ws:
                self._ws = None
            if self._lector is lector:
                self._lector = None
            if not cerrada:
                _abortar_conexion(ws)
            if lector is not None and not lector.done():
                lector.cancel()
        if lector is not None and not lector.done():
            await asyncio.wait({lector}, timeout=_TIEMPO_LECTOR_S)

    # ------------------------------------------------------------------
    # Vigilancia
    # ------------------------------------------------------------------

    @staticmethod
    def _publicar(bus: BusEventos | None, nombre: str, datos: dict[str, Any]) -> None:
        if bus is None:
            return
        try:
            bus.publicar(nombre, datos, origen="nexus")
        except Exception as exc:  # el bus nunca debería lanzar; se registra por si acaso
            log.debug("No se pudo publicar %s: %s", nombre, exc)

    def _evento(self, bus: BusEventos | None, datos: dict[str, Any]) -> None:
        log.debug("nexus.evento %s", datos.get("tipo"))
        self._publicar(bus, "nexus.evento", datos)

    def _limitar_fallo(self, cmd: str, vida_s: float) -> dict[str, Any] | None:
        ahora = self._reloj()
        for clave in [c for c, t in self._fallos_por_cmd.items() if ahora - t >= REFRACTARIO_FALLO_S]:
            del self._fallos_por_cmd[clave]
        if cmd in self._fallos_por_cmd:
            log.debug("trabajo_fallido_rapido de «%s» descartado (refractario de 5 min)", cmd)
            return None
        while self._fallos_hora and ahora - self._fallos_hora[0] >= 3600.0:
            self._fallos_hora.popleft()
        if len(self._fallos_hora) >= MAX_FALLOS_HORA:
            log.debug("trabajo_fallido_rapido de «%s» descartado (máximo %d por hora)", cmd, MAX_FALLOS_HORA)
            return None
        self._fallos_por_cmd[cmd] = ahora
        self._fallos_hora.append(ahora)
        return {"tipo": "trabajo_fallido_rapido", "cmd_corto": cmd, "duracion_s": round(vida_s, 1)}

    def _limitar_terminado(self, cmd: str, vida_s: float) -> dict[str, Any] | None:
        ahora = self._reloj()
        if self._ultimo_terminado is not None and ahora - self._ultimo_terminado < INTERVALO_TERMINADO_S:
            log.debug("trabajo_terminado de «%s» descartado (máximo 1 cada 10 min)", cmd)
            return None
        self._ultimo_terminado = ahora
        return {"tipo": "trabajo_terminado", "cmd_corto": cmd, "duracion_s": round(vida_s, 1)}

    def _procesar_fin(self, trabajo: _Trabajo, vida_s: float) -> dict[str, Any] | None:
        """Clasifica el fin de un trabajo y aplica los límites. Devuelve el evento o ``None``."""
        if vida_s < VIDA_FALLO_RAPIDO_S:
            if not trabajo.argv0.startswith(_PREFIJOS_TRABAJO):
                return None
            return self._limitar_fallo(trabajo.cmd_corto, vida_s)
        if vida_s >= VIDA_TRABAJO_LARGO_S:
            return self._limitar_terminado(trabajo.cmd_corto, vida_s)
        return None

    # --- seguimiento rápido de trabajos (executor) ---

    def _hijos_directos(self, pid: int) -> list[int]:
        """PIDs hijos directos de ``pid``. En Linux lee ``/proc/<pid>/task/*/children`` (barato)."""
        if _ES_LINUX:
            try:
                hijos: list[int] = []
                with os.scandir(f"/proc/{pid}/task") as tareas:
                    for tarea in tareas:
                        with open(f"/proc/{pid}/task/{tarea.name}/children", encoding="ascii") as archivo:
                            hijos.extend(int(x) for x in archivo.read().split())
                return hijos
            except (OSError, ValueError):
                pass  # kernel sin CONFIG_PROC_CHILDREN o proceso terminado: se usa psutil
        try:
            return [int(h.pid) for h in psutil.Process(pid).children(recursive=False)]
        except _ErrorPsutil:
            return []

    @staticmethod
    def _sigue_vivo(pid: int, creado: float) -> bool:
        """``True`` si el proceso ``pid`` (con ese ``create_time``) sigue vivo y no es zombi."""
        try:
            proc = psutil.Process(pid)
            if abs(float(proc.create_time()) - creado) > 0.5:
                return False  # PID reutilizado
            return proc.status() != psutil.STATUS_ZOMBIE
        except psutil.AccessDenied:
            return True
        except _ErrorPsutil:
            return False

    @staticmethod
    def _trabajo_nuevo(pid: int, shell_pid: int, nombre_shell: str) -> _Trabajo | None:
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                nombre = _nombre_normalizado(proc.name())
                if nombre in _HIJOS_IGNORADOS or proc.status() == psutil.STATUS_ZOMBIE:
                    return None
                argv = [str(a) for a in (proc.cmdline() or [])] or [proc.name()]
                creado = float(proc.create_time())
        except _ErrorPsutil:
            return None
        argv0 = _basename(argv[0]).lower()
        # Recién bifurcado y aún sin exec: conserva la cmdline del shell; se relee en el siguiente pase.
        provisional = _nombre_normalizado(argv0) == nombre_shell
        return _Trabajo(pid=int(pid), creado=creado, argv0=argv0, cmd_corto=cmd_corto(argv),
                        shell_pid=int(shell_pid), provisional=provisional)

    def _escanear_rapido(self, shells: list[tuple[int, float]],
                         previos: dict[int, _Trabajo]) -> tuple[dict[int, _Trabajo], list[tuple[_Trabajo, float]]]:
        """Pase de seguimiento (executor): trabajos actuales y trabajos que **murieron** con su vida.

        No cuenta como fin: un trabajo cuyo shell murió (se cerró la terminal o Nexus) ni uno que
        sigue vivo fuera del shell (reparentado / ``nohup``).
        """
        if psutil is None:
            return {}, []
        actuales: dict[int, _Trabajo] = {}
        shells_vivos: set[int] = set()
        for shell_pid, shell_creado in shells:
            if not self._sigue_vivo(shell_pid, shell_creado):
                continue
            shells_vivos.add(shell_pid)
            try:
                nombre_shell = _nombre_normalizado(psutil.Process(shell_pid).name())
            except _ErrorPsutil:
                nombre_shell = ""
            for pid in self._hijos_directos(shell_pid):
                previo = previos.get(pid)
                if previo is not None and previo.shell_pid == shell_pid and not previo.provisional:
                    if self._sigue_vivo(pid, previo.creado):
                        actuales[pid] = previo
                    continue
                nuevo = self._trabajo_nuevo(pid, shell_pid, nombre_shell)
                if nuevo is not None:
                    actuales[pid] = nuevo
        ahora = self._ahora()
        muertos: list[tuple[_Trabajo, float]] = []
        for pid, trabajo in previos.items():
            if pid in actuales:
                continue
            if trabajo.shell_pid not in shells_vivos:
                continue
            if self._sigue_vivo(pid, trabajo.creado):
                continue
            muertos.append((trabajo, max(0.0, ahora - trabajo.creado)))
        return actuales, muertos

    async def _vigilar_trabajos(self, bus: BusEventos | None) -> None:
        """Pase rápido de seguimiento: nunca bloquea el bucle más de ``_TIEMPO_ESCANEO_S``.

        Si el executor está ocupado (una recolección en curso o una llamada de psutil colgada),
        el pase se salta y se reintenta en el siguiente tic: los pases no se apilan.
        """
        if psutil is None or (not self._shells and not self._seguimiento):
            return
        if self._ejecutor_ocupado():
            return
        try:
            actuales, muertos = await self._en_ejecutor(self._escanear_rapido, list(self._shells),
                                                        dict(self._seguimiento), plazo=_TIEMPO_ESCANEO_S)
        except TimeoutError as exc:
            # Sin resultado no se toca el seguimiento: los fines se detectan en un pase posterior.
            log.debug("Seguimiento de trabajos de Nexus aplazado: %s", exc)
            return
        self._seguimiento = actuales
        for trabajo, vida_s in muertos:
            evento = self._procesar_fin(trabajo, vida_s)
            if evento is not None:
                log.info("Trabajo de la terminal de Nexus terminado: %s (%.1f s)", trabajo.cmd_corto, vida_s)
                self._evento(bus, evento)

    # --- otros pasos de la vigilancia ---

    async def _paso_sondeo(self, bus: BusEventos | None) -> None:
        resultado = await self.sondeo_seguridad()
        if resultado.get("remoto_9090_sin_auth") and not self._riesgo_emitido:
            self._riesgo_emitido = True
            log.warning("Nexus IDE expone el servidor remoto :9090 sin autenticación "
                        "(ejecuta comandos para cualquiera en la red local)")
            self._evento(bus, {"tipo": "riesgo", "detalle": DETALLE_RIESGO})

    async def _paso_colab(self, bus: BusEventos | None) -> None:
        if not self.settings.nexus_colab_token or self._ws is not None or _ws_connect is None:
            return
        if self._ts_sondeo is None or self._reloj() - self._ts_sondeo > 5.0:
            await self._paso_sondeo(bus)   # sondeo fresco (y aviso de riesgo si procede)
        if self._colab_3737_activo:
            await self.colab_conectar()

    async def _paso_estado(self, bus: BusEventos | None) -> None:
        est = await self.estado()
        if not self._recoleccion_ok:
            # Fallo puntual de psutil: no se publica un "no corre" falso ni un cambio de foco espurio.
            log.debug("Recolección de Nexus fallida; se conserva el último estado publicado")
            return
        self._publicar(bus, "nexus.estado", est.a_dict())
        self._comprobar_foco(bus, est.enfocado)
        clave = (bool(est.corriendo), bool(est.colab_conectado))
        if clave != self._modulo_publicado:
            self._modulo_publicado = clave
            self._publicar(bus, "sistema.modulo", self.diagnostico().a_dict())
        if not est.corriendo:
            self._shells = []

    def _comprobar_foco(self, bus: BusEventos | None, enfocado: bool) -> None:
        if bool(enfocado) != self._enfocado_previo:
            self._enfocado_previo = bool(enfocado)
            self._evento(bus, {"tipo": "foco", "enfocado": bool(enfocado)})

    async def _paso_foco(self, bus: BusEventos | None) -> None:
        if not self._hypr_disponible() or self._ultimo_estado is None or not self._ultimo_estado.corriendo:
            return
        pids = self._pids_cache[1] if self._pids_cache else list(self._ultimo_estado.pids)
        self._comprobar_foco(bus, await self._esta_enfocado_con(pids))

    async def _paso_chat(self, bus: BusEventos | None) -> None:
        nombre = self.nombre.lower()
        while True:
            try:
                mensaje = self.eventos.get_nowait()
            except asyncio.QueueEmpty:
                return
            if mensaje.get("t") != "chat":
                log.debug("Mensaje de colaboración %s recibido", mensaje.get("t"))
                continue
            texto = str(mensaje.get("text") or "")
            minusculas = texto.lower()
            if "@amadeus" not in minusculas and nombre not in minusculas:
                continue
            self._evento(bus, {"tipo": "colab_chat", "nombre": str(mensaje.get("name") or "")[:40],
                               "texto": texto[:500]})

    async def _paso(self, nombre: str, corrutina: Any, fallos: dict[str, int]) -> None:
        """Ejecuta un paso de la vigilancia aislando su fallo (el bucle nunca muere)."""
        try:
            await corrutina
            fallos[nombre] = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fallos[nombre] = fallos.get(nombre, 0) + 1
            if fallos[nombre] == 1 or fallos[nombre] % 120 == 0:
                log.warning("Fallo en la vigilancia de Nexus (%s): %s: %s", nombre, type(exc).__name__, exc)
            else:
                log.debug("Fallo repetido en la vigilancia de Nexus (%s): %s", nombre, exc)

    async def bucle_vigilancia(self, parar: asyncio.Event, bus: BusEventos | None, *,
                               periodo_s: float = 10.0) -> None:
        """Vigila Nexus hasta que ``parar`` se active.

        - Cada ``periodo_s``: publica ``nexus.estado`` (y ``foco`` si cambió).
        - Cada 0,5 s: sigue los trabajos de la terminal (``trabajo_fallido_rapido`` con vida
          < 5 s; ``trabajo_terminado`` con vida ≥ 300 s) y vacía la cola de colaboración
          (``colab_chat`` si mencionan ``@amadeus`` o el nombre).
        - Al arrancar y cada hora: sondeo de seguridad (``riesgo`` una sola vez por sesión).
        - Cada 60 s, con ``NEXUS_COLLAB_TOKEN`` y sin conexión: si el ``:3737`` está activo, se une.

        Cada paso va en su propio ``try``: un fallo puntual nunca termina el bucle.
        """
        try:
            periodo = max(0.05, float(periodo_s))
        except (TypeError, ValueError):
            periodo = 10.0
        tic = min(PERIODO_RAPIDO_S, periodo)
        inicio = self._reloj()
        proximo_estado = inicio
        proximo_sondeo = inicio
        proximo_colab = inicio
        proximo_foco = inicio + INTERVALO_FOCO_S
        fallos: dict[str, int] = {}
        log.info("Vigilancia de Nexus IDE en marcha (estado cada %.0f s)", periodo)
        while not parar.is_set() and not self._cerrado:
            ahora = self._reloj()
            if ahora >= proximo_sondeo:
                proximo_sondeo = ahora + INTERVALO_SONDEO_S
                await self._paso("sondeo", self._paso_sondeo(bus), fallos)
            if self.settings.nexus_colab_token and self._ws is None and ahora >= proximo_colab:
                proximo_colab = ahora + INTERVALO_COLAB_S
                await self._paso("colaboración", self._paso_colab(bus), fallos)
            if ahora >= proximo_estado:
                proximo_estado = ahora + periodo
                proximo_foco = ahora + INTERVALO_FOCO_S
                await self._paso("estado", self._paso_estado(bus), fallos)
            elif ahora >= proximo_foco:
                proximo_foco = ahora + INTERVALO_FOCO_S
                await self._paso("foco", self._paso_foco(bus), fallos)
            await self._paso("trabajos", self._vigilar_trabajos(bus), fallos)
            await self._paso("chat", self._paso_chat(bus), fallos)
            if parar.is_set():
                break
            try:
                await asyncio.wait_for(parar.wait(), tic)
            except TimeoutError:
                continue
        log.info("Vigilancia de Nexus IDE detenida")

    # ------------------------------------------------------------------
    # Diagnóstico y cierre
    # ------------------------------------------------------------------

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico ``"nexus"`` para el arranque y el HUD (sin rutas completas).

        Usa el último estado recolectado; si aún no hay ninguno, hace una detección rápida por
        nombre de proceso (acotada: solo se lee la cmdline de los candidatos).
        """
        nombre = "Nexus IDE"
        if psutil is None:
            return DiagnosticoModulo("nexus", nombre, EstadoModulo.OFF, "psutil no instalado")
        if self._cerrado:
            return DiagnosticoModulo("nexus", nombre, EstadoModulo.OFF, "cerrado")
        est = self._ultimo_estado
        corriendo = est.corriendo if est is not None else self.esta_corriendo()
        if not corriendo:
            return DiagnosticoModulo("nexus", nombre, EstadoModulo.EN_ESPERA, "no detectado")
        partes = ["detectado"]
        if est is not None:
            base = _ultimo_componente(est.workspace)
            if base:
                partes.append(f"workspace {base}")
            if est.enfocado:
                partes.append("enfocado")
        if self.colab_conectado:
            partes.append("colaboración activa")
        if self._riesgo_9090:
            partes.append("⚠ :9090 sin autenticación")
        return DiagnosticoModulo("nexus", nombre, EstadoModulo.OK, " · ".join(partes))

    async def cerrar(self) -> None:
        """Apaga el executor (``wait=False, cancel_futures=True``) y sale de la colaboración.

        Idempotente. El executor se apaga **antes** de la colaboración para que ocurra aunque el
        cierre se cancele (``AuraVisionPC.cerrar`` lo envuelve en ``wait_for(..., 3 s)``); a partir
        de aquí ``_en_ejecutor`` lanza ``RuntimeError`` y la vigilancia termina en su siguiente tic.
        """
        if self._cerrado:
            return
        self._cerrado = True
        self._ejecutor.shutdown(wait=False, cancel_futures=True)
        try:
            await self.colab_cerrar()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Error al cerrar la colaboración: %s", exc)
        log.debug("NexusLink cerrado")
