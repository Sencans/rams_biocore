"""Compuerta de permisos para toda acción sobre el PC (§9.1, §10.10).

Qué hace: decide, con código determinista y sin LLM, si una :class:`AccionPC` se puede
ejecutar. Implementa la tabla normativa de §9.1 (se evalúa de arriba abajo y gana la primera
fila que aplique) y la mecánica de confirmación en la terminal local para las acciones
destructivas: publica ``permiso.solicitado``, pregunta con ``⚠ Confirmación requerida: …
¿Procedo? [s/N]``, espera la siguiente línea del usuario (o el timeout, que equivale a "no")
y publica ``permiso.resuelto``.

Reglas de seguridad que aplica:
- Las acciones nunca nacen del texto del LLM ni de Telegram: ``origen`` ``llm`` o
  ``telegram`` ⇒ denegada siempre.
- Solo hay **una** confirmación pendiente a la vez; otra solicitud se deniega.
- ``cerrar_ventana`` exige una dirección de Hyprland válida y un ``pid`` que no sea de
  R.A.M.S. (ni su terminal ni sus ancestros).

Dependencias opcionales: ``psutil`` (para calcular los procesos propios: el propio PID y sus
ancestros). Degradación: sin psutil, solo ``os.getpid()``; sin bus no publica eventos; sin
callback ``preguntar`` usa ``print``.

La compuerta se limita a la mecánica: Amadeus confirma «qué, dónde, cuántos y qué se pierde»
sin sarcasmo, pero eso lo verbalizan main y Broca.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

try:  # opcional: ancestros del proceso para no cerrar la terminal de R.A.M.S.
    import psutil
except ImportError:  # sin psutil: solo el PID propio
    psutil = None

from core.bus import BusEventos
from core.config import Settings
from core.contracts import (
    ORIGENES_ACCION,
    TIPOS_ACCION,
    AccionPC,
    DecisionPermiso,
    formatear_decimal,
)

__all__ = [
    "CLASE_SEGURA_RE",
    "DIRECCION_RE",
    "RESPUESTAS_SI",
    "MOTIVOS_VISTAZO",
    "ORDENES_MEDIA",
    "SolicitudPermiso",
    "calcular_pids_propios",
    "CompuertaPermisos",
]

log = logging.getLogger("rams.permisos")

# ---------------------------------------------------------------------------
# Constantes (§10.10)
# ---------------------------------------------------------------------------

CLASE_SEGURA_RE: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
DIRECCION_RE: Final = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
RESPUESTAS_SI: Final = frozenset({"s", "si", "sí", "y", "yes", "procede", "dale"})

MOTIVOS_VISTAZO: Final = frozenset({"usuario", "curiosidad", "presencia"})
ORIGENES_VISTAZO: Final = frozenset({"usuario", "iniciativa"})
ORDENES_MEDIA: Final = frozenset({"play_pause", "siguiente", "anterior"})

_ORIGENES_PROHIBIDOS: Final = frozenset({"llm", "telegram"})
# Solo dígitos ASCII: con \d (Unicode) se aceptarían '٣' o '０５０', y el ejecutor
# (aura_vision_pc._entero_arg) exige isascii(); compuerta y ejecutor deben coincidir.
_RE_ENTERO: Final = re.compile(r"^[0-9]{1,4}$")

# Motivos de la confirmación (§9.1 "Confirmación").
MOTIVO_CONFIRMADA: Final = "confirmada por el usuario"
MOTIVO_RECHAZADA: Final = "rechazada por el usuario"
MOTIVO_CANCELADA: Final = "confirmación cancelada"
MOTIVO_OTRA_EN_CURSO: Final = "otra confirmación en curso"


def _sin_tildes(texto: str) -> str:
    """Quita los diacríticos (NFD sin la categoría Mn)."""
    descompuesto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")


_SI_NORMALIZADAS: Final = frozenset(_sin_tildes(r) for r in RESPUESTAS_SI)


def _es_si(respuesta: str) -> bool:
    """``True`` si la respuesta (``strip().lower()`` y sin tildes) es exactamente un "sí"."""
    if not isinstance(respuesta, str):
        return False
    return _sin_tildes(respuesta.strip().lower()) in _SI_NORMALIZADAS


def _entero(valor: Any) -> int | None:
    """Entero estricto: ``int`` (no ``bool``) o texto de dígitos; cualquier otra cosa ⇒ ``None``."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, str) and _RE_ENTERO.fullmatch(valor.strip()):
        return int(valor.strip())
    return None


def _texto_corto(valor: Any, maximo: int = 40) -> str:
    """Texto de un argumento para un motivo legible (truncado y sin saltos de línea)."""
    texto = " ".join(str(valor).split())
    return texto if len(texto) <= maximo else texto[: maximo - 1] + "…"


def _segundos(valor: float) -> str:
    """Duración legible para el usuario: ``60.0 → "60"``, ``0.05 → "0,05"``."""
    if valor >= 1:
        return formatear_decimal(valor, 0)
    return formatear_decimal(valor, 2)


def calcular_pids_propios() -> frozenset[int]:
    """PIDs de R.A.M.S.: ``{os.getpid()}`` ∪ sus ancestros (terminal, shell…) según psutil.

    Sin psutil, o si psutil falla, devuelve solo ``{os.getpid()}``. Nunca lanza.
    """
    pids = {os.getpid()}
    if psutil is None:
        log.debug("psutil no está instalado: los procesos propios son solo el PID actual")
        return frozenset(pids)
    try:
        for padre in psutil.Process().parents():
            pids.add(int(padre.pid))
    except Exception as exc:  # psutil.Error, OSError, permisos…
        log.debug("No se pudieron leer los procesos ancestros (%s)", type(exc).__name__)
    return frozenset(pids)


@dataclass(frozen=True)
class SolicitudPermiso:
    """Confirmación pendiente de una acción destructiva."""

    id: str                 # uuid4().hex[:8]
    accion: AccionPC
    creada_ts: float


class CompuertaPermisos:
    """Compuerta única de permisos para el control del PC (§9.1).

    ``evaluar`` es pura (tabla de §9.1); ``autorizar`` añade la confirmación interactiva de
    las acciones destructivas. La siguiente línea que escriba el usuario en la terminal local
    se pasa a :meth:`resolver`.
    """

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 preguntar: Callable[[str], None] | None = None, timeout_s: float = 60.0,
                 pids_propios: frozenset[int] | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self._preguntar: Callable[[str], None] | None = preguntar
        try:
            self.timeout_s = max(0.0, float(timeout_s))
        except (TypeError, ValueError):
            log.warning("timeout_s inválido para la compuerta; uso 60 s")
            self.timeout_s = 60.0
        if pids_propios is None:
            self.pids_propios: frozenset[int] = calcular_pids_propios()
        else:
            self.pids_propios = frozenset(int(p) for p in pids_propios)
        self._lock = threading.Lock()
        self._pendiente: SolicitudPermiso | None = None
        self._futuro: asyncio.Future[tuple[bool, str]] | None = None

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------

    def set_preguntar(self, preguntar: Callable[[str], None]) -> None:
        """Cambia el callback que muestra la pregunta de confirmación (p. ej. la consola)."""
        self._preguntar = preguntar

    # ------------------------------------------------------------------
    # Evaluación pura (tabla de §9.1)
    # ------------------------------------------------------------------

    def evaluar(self, accion: AccionPC) -> DecisionPermiso:
        """Aplica la tabla de §9.1 en orden. Pura: no publica, no pregunta y no ejecuta.

        ``aprobada`` es ``True`` solo cuando el veredicto es ``"permitida"``; un veredicto
        ``"confirmar"`` necesita :meth:`autorizar`. Nunca lanza: una acción mal formada se
        deniega.
        """
        try:
            return self._evaluar(accion)
        except Exception as exc:  # defensa: una acción exótica nunca rompe la compuerta
            log.warning("Error al evaluar una acción (%s); se deniega", type(exc).__name__)
            return DecisionPermiso("denegada", "no pude evaluar la acción", aprobada=False)

    def _evaluar(self, accion: AccionPC) -> DecisionPermiso:
        if not isinstance(accion, AccionPC):
            return self._denegada("acción mal formada")
        tipo = accion.tipo
        origen = accion.origen
        args: Mapping[str, Any] = accion.args if isinstance(accion.args, Mapping) else {}

        # 1. Origen: nunca del LLM ni de Telegram; orígenes desconocidos tampoco.
        if origen == "llm":
            return self._denegada("las acciones nunca nacen del texto del modelo de lenguaje")
        if origen == "telegram":
            return self._denegada("desde Telegram no se puede actuar sobre el computador")
        if origen not in ORIGENES_ACCION or origen in _ORIGENES_PROHIBIDOS:
            return self._denegada("origen de la acción desconocido")

        # 2. Tipo soportado.
        if tipo not in TIPOS_ACCION:
            return self._denegada("tipo de acción no soportado")

        # 3-4. Restricciones por origen.
        if origen == "iniciativa" and tipo != "vistazo":
            return self._denegada("por iniciativa propia solo puedo echar un vistazo por la cámara")
        if origen == "nexus" and tipo != "enfocar_nexus":
            return self._denegada("desde Nexus IDE solo se permite traer su ventana al frente")

        # 5+. Por tipo.
        if tipo == "vistazo":
            motivo = args.get("motivo")
            if motivo not in MOTIVOS_VISTAZO:
                return self._denegada("motivo de vistazo no válido")
            if origen not in ORIGENES_VISTAZO:
                return self._denegada("ese origen no puede pedir un vistazo de cámara")
            return self._permitida("vistazo de cámara permitido")

        if tipo == "lanzar_app":
            alias = args.get("alias")
            if isinstance(alias, str) and alias in self.settings.apps_permitidas:
                return self._permitida(f"«{alias}» está en la lista permitida")
            return self._denegada(f"«{_texto_corto(alias)}» no está en la lista permitida RAMS_APPS")

        if tipo == "enfocar_nexus":
            return self._permitida("traer Nexus IDE al frente está permitido")

        if tipo == "ir_workspace":
            n = _entero(args.get("n"))
            if n is not None and 1 <= n <= 10:
                return self._permitida(f"espacio de trabajo {n} permitido")
            return self._denegada("el espacio de trabajo debe ser un entero entre 1 y 10")

        if tipo == "volumen":
            nivel = _entero(args.get("nivel"))
            if nivel is not None and 0 <= nivel <= 100:
                return self._permitida(f"volumen al {nivel} % permitido")
            return self._denegada("el volumen debe ser un entero entre 0 y 100")

        if tipo == "media":
            orden = args.get("orden")
            if orden in ORDENES_MEDIA:
                return self._permitida("control multimedia permitido")
            return self._denegada("orden multimedia no válida")

        if tipo == "captura_pantalla":
            if origen == "usuario":
                return self._permitida("captura de pantalla pedida por el usuario")
            return self._denegada("la captura de pantalla solo la puede pedir el usuario")

        if tipo == "cerrar_ventana":
            direccion = args.get("direccion")
            if not (isinstance(direccion, str) and DIRECCION_RE.fullmatch(direccion)):
                return self._denegada("no sé qué ventana cerrar", destructiva=True)
            pid = args.get("pid")
            if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
                return self._denegada("identificador de proceso no válido", destructiva=True)
            if pid is not None and pid in self.pids_propios:
                return self._denegada("esa ventana es la terminal de R.A.M.S.", destructiva=True)
            return DecisionPermiso("confirmar", "cerrar una ventana requiere confirmación",
                                   destructiva=True, aprobada=False)

        # Inalcanzable mientras TIPOS_ACCION y esta tabla estén alineados.
        return self._denegada("tipo de acción no soportado")

    @staticmethod
    def _permitida(motivo: str) -> DecisionPermiso:
        return DecisionPermiso("permitida", motivo, destructiva=False, aprobada=True)

    @staticmethod
    def _denegada(motivo: str, *, destructiva: bool = False) -> DecisionPermiso:
        return DecisionPermiso("denegada", motivo, destructiva=destructiva, aprobada=False)

    # ------------------------------------------------------------------
    # Autorización con confirmación
    # ------------------------------------------------------------------

    async def autorizar(self, accion: AccionPC) -> DecisionPermiso:
        """Decisión final (``.aprobada``) para ``accion``; pregunta si es destructiva.

        - ``permitida`` ⇒ ``aprobada=True``.
        - ``denegada`` ⇒ ``log.info`` y ``aprobada=False``.
        - ``confirmar`` ⇒ publica ``permiso.solicitado``, llama a ``preguntar``, espera a
          :meth:`resolver` (como mucho ``timeout_s``; el silencio equivale a "no"), publica
          ``permiso.resuelto`` y devuelve ``DecisionPermiso("confirmar", motivo,
          destructiva=True, aprobada=<respuesta>)``.

        Si ya hay otra confirmación pendiente, la nueva se deniega ("otra confirmación en
        curso"). La cancelación de la tarea que espera se propaga, limpia la pendiente y
        publica ``permiso.resuelto`` con ``aprobado=False`` y motivo "confirmación cancelada".
        """
        decision = self.evaluar(accion)
        tipo = getattr(accion, "tipo", "?")
        if decision.veredicto == "permitida":
            log.debug("Acción %s permitida: %s", tipo, decision.motivo)
            return decision
        if decision.veredicto != "confirmar":
            log.info("Acción %s denegada: %s", tipo, decision.motivo)
            return decision

        loop = asyncio.get_running_loop()
        futuro: asyncio.Future[tuple[bool, str]] = loop.create_future()
        solicitud = SolicitudPermiso(id=uuid.uuid4().hex[:8], accion=accion, creada_ts=time.time())
        with self._lock:
            if self._hay_pendiente_sin_lock():
                ocupada = True
            else:
                ocupada = False
                self._pendiente = solicitud
                self._futuro = futuro
        if ocupada:
            log.info("Acción %s denegada: %s", tipo, MOTIVO_OTRA_EN_CURSO)
            return DecisionPermiso("denegada", MOTIVO_OTRA_EN_CURSO, destructiva=True, aprobada=False)

        descripcion = accion.descripcion or self._descripcion_por_defecto(accion)
        # Valor si la espera no termina normalmente (tarea cancelada, p. ej. en el apagado):
        # aun así se publica permiso.resuelto para que el HUD cierre el banner (§5).
        aprobada, motivo = False, MOTIVO_CANCELADA
        try:
            self._publicar("permiso.solicitado", {
                "id": solicitud.id,
                "descripcion": descripcion,
                "destructiva": True,
                "tipo": tipo,
            })
            self._mostrar(f"⚠ Confirmación requerida: {descripcion}. ¿Procedo? [s/N]")
            log.info("Confirmación solicitada (%s) para la acción %s", solicitud.id, tipo)
            try:
                aprobada, motivo = await asyncio.wait_for(futuro, timeout=self.timeout_s)
            except asyncio.TimeoutError:
                aprobada, motivo = False, f"sin respuesta en {_segundos(self.timeout_s)} s"
        finally:
            with self._lock:
                if self._pendiente is solicitud:
                    self._pendiente = None
                    self._futuro = None
            if not futuro.done():
                futuro.cancel()
            # En el finally: cada permiso.solicitado tiene su permiso.resuelto, también
            # cuando la cancelación de la tarea se propaga desde aquí.
            self._publicar("permiso.resuelto", {"id": solicitud.id, "aprobado": bool(aprobada),
                                                 "motivo": motivo})
            log.info("Confirmación %s resuelta: %s", solicitud.id, motivo)

        return DecisionPermiso("confirmar", motivo, destructiva=True, aprobada=bool(aprobada))

    @staticmethod
    def _descripcion_por_defecto(accion: AccionPC) -> str:
        """Descripción mínima si la intención no trajo una."""
        if accion.tipo == "cerrar_ventana":
            return f"cerrar la ventana {accion.args.get('direccion', '?')}"
        return accion.tipo.replace("_", " ")

    # ------------------------------------------------------------------
    # Estado de la confirmación pendiente
    # ------------------------------------------------------------------

    def _hay_pendiente_sin_lock(self) -> bool:
        futuro = self._futuro
        return self._pendiente is not None and futuro is not None and not futuro.done()

    def hay_pendiente(self) -> bool:
        """``True`` si hay una confirmación esperando la respuesta del usuario."""
        with self._lock:
            return self._hay_pendiente_sin_lock()

    def pendiente(self) -> SolicitudPermiso | None:
        """La solicitud pendiente (o ``None``)."""
        with self._lock:
            return self._pendiente if self._hay_pendiente_sin_lock() else None

    def resolver(self, respuesta: str) -> bool:
        """Interpreta ``respuesta`` como la contestación a la solicitud pendiente.

        "Sí" es exactamente ``s``, ``si``, ``sí``, ``y``, ``yes``, ``procede`` o ``dale`` (sin
        distinguir mayúsculas ni tildes); cualquier otra cosa es "no". Devuelve ``True`` si
        consumió una solicitud pendiente; si no había ninguna, ``False`` y no hace nada.
        """
        si = _es_si(respuesta)
        return self._terminar(si, MOTIVO_CONFIRMADA if si else MOTIVO_RECHAZADA)

    def cancelar_pendiente(self) -> None:
        """Resuelve la solicitud pendiente como "no" (p. ej. durante el apagado)."""
        if self._terminar(False, MOTIVO_CANCELADA):
            log.info("Confirmación pendiente cancelada")

    def _terminar(self, aprobada: bool, motivo: str) -> bool:
        """Entrega el resultado al future pendiente (thread-safe). ``True`` si había uno."""
        with self._lock:
            if not self._hay_pendiente_sin_lock():
                return False
            futuro = self._futuro
            # La solicitud deja de estar pendiente YA: la siguiente línea no se consume aquí.
            self._pendiente = None
            self._futuro = None
        assert futuro is not None
        loop = futuro.get_loop()
        try:
            en_su_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            en_su_loop = False
        if en_su_loop:
            _entregar(futuro, (aprobada, motivo))
        else:
            try:
                loop.call_soon_threadsafe(_entregar, futuro, (aprobada, motivo))
            except RuntimeError as exc:  # el loop ya se cerró
                log.debug("No se pudo entregar la respuesta de confirmación: %s", exc)
                return False
        return True

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    def _mostrar(self, texto: str) -> None:
        """Muestra la pregunta con el callback (o ``print``); un fallo no rompe la compuerta."""
        preguntar = self._preguntar
        try:
            if preguntar is not None:
                preguntar(texto)
            else:
                print(texto, flush=True)
        except Exception as exc:
            log.warning("No se pudo mostrar la pregunta de confirmación (%s)", type(exc).__name__)

    def _publicar(self, nombre: str, datos: dict[str, Any]) -> None:
        """Publica en el bus si existe; nunca lanza."""
        if self.bus is None:
            return
        try:
            self.bus.publicar(nombre, datos, origen="permisos")
        except Exception as exc:
            log.debug("No se pudo publicar %s: %s", nombre, exc)


def _entregar(futuro: asyncio.Future[tuple[bool, str]], valor: tuple[bool, str]) -> None:
    """``set_result`` si el future sigue vivo (pudo vencer el timeout entretanto)."""
    if not futuro.done():
        futuro.set_result(valor)
