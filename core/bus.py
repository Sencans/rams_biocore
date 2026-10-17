"""Bus de eventos asyncio (pub/sub) de R.A.M.S. BIO-CORE.

Qué hace:
- ``BusEventos`` reparte eventos (``Evento``) a manejadores síncronos, corrutinas y colas
  ``asyncio.Queue``, con patrones exactos (``"pc.telemetria"``), de prefijo (``"pc.*"``) y
  ``"*"`` (§4.2).
- ``publicar`` es no bloqueante y *thread-safe*: desde otro hilo delega en el loop con
  ``call_soon_threadsafe``. Un manejador que falla nunca afecta a los demás.
- ``instalar_manejador_log`` publica los registros de log de nivel AVISO o superior como
  eventos ``log`` (mensaje ya redactado por ``FiltroSecretos``), para el HUD.

Dependencias: solo la biblioteca estándar (y ``core.config`` para ``FiltroSecretos``).

Degradación: sin loop en ejecución, los manejadores asíncronos se descartan con
``log.debug`` y los síncronos y las colas funcionan igual. Un nombre de evento inválido se
registra con ``log.error`` y se descarta sin lanzar.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from core.config import FiltroSecretos, nombre_nivel

__all__ = ["Evento", "Manejador", "BusEventos", "instalar_manejador_log", "NOMBRE_VALIDO_RE"]

log = logging.getLogger("rams.bus")

# Patrón de §4.2.2. Se aplica SIEMPRE con ``fullmatch``: con ``match``, el ``$`` también casa
# antes de un salto de línea final y "log\n" pasaría por válido.
NOMBRE_VALIDO_RE: Final = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")
_PREFIJO_VALIDO_RE: Final = re.compile(r"^[a-z_]+(\.[a-z_]+)*\.\*$")


@dataclass(frozen=True)
class Evento:
    """Evento del bus. ``datos`` es un payload JSON-serializable con las claves de §5."""

    nombre: str                                        # p. ej. "homeostasis.estado"
    datos: dict[str, Any] = field(hash=False)          # payload (§5)
    origen: str = ""                                   # módulo emisor, p. ej. "conectoma"
    ts: float = field(default_factory=time.time)


Manejador = Callable[[Evento], Any]  # función síncrona o corrutina


class _SuscriptorCola:
    """Suscriptor especial que entrega los eventos a una ``asyncio.Queue``."""

    __slots__ = ("cola",)

    def __init__(self, cola: asyncio.Queue[Evento]) -> None:
        self.cola = cola


def _coincide(patron: str, nombre: str) -> bool:
    """``True`` si ``nombre`` casa con ``patron`` (exacto, ``"x.*"`` o ``"*"``)."""
    if patron == "*":
        return True
    if patron.endswith(".*"):
        return nombre.startswith(patron[:-1])
    return patron == nombre


def _nombre_manejador(manejador: Any) -> str:
    """Nombre legible de un manejador para los logs."""
    nombre = getattr(manejador, "__qualname__", None) or getattr(manejador, "__name__", None)
    return str(nombre) if nombre else type(manejador).__name__


class BusEventos:
    """Bus pub/sub asyncio, *thread-safe* (§4.2, §10.3)."""

    def __init__(self, *, nombre: str = "rams") -> None:
        self.nombre = nombre
        self._suscripciones: list[tuple[str, Any]] = []
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tareas: set[asyncio.Task[Any]] = set()
        self._ultimos: dict[str, Evento] = {}
        self._publicados = 0
        self._entregados = 0
        self._descartados = 0
        self._errores = 0
        self._cerrado = False

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def vincular_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Vincula el bus a ``loop`` (o al loop en ejecución).

        El loop puede estar aún sin arrancar y correr después en cualquier hilo: no se supone
        nada sobre su hilo. :meth:`publicar` comprueba en cada llamada si se ejecuta dentro
        de ese loop (``asyncio.get_running_loop() is loop``, que es por hilo).
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                log.debug("vincular_loop sin loop en ejecución; se vinculará en la primera publicación")
                return
        with self._lock:
            self._loop = loop

    @staticmethod
    def _dentro_del_loop(loop: asyncio.AbstractEventLoop) -> bool:
        """``True`` si el código actual se ejecuta dentro de ``loop`` (en el hilo de ese loop)."""
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:  # este hilo no ejecuta ningún loop
            return False

    def _autovincular(self) -> None:
        """Si no hay loop vinculado (o está cerrado), se vincula al loop en ejecución."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            return
        try:
            actual = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.vincular_loop(actual)

    # ------------------------------------------------------------------
    # Suscripción
    # ------------------------------------------------------------------

    @staticmethod
    def _patron_valido(patron: str) -> bool:
        return patron == "*" or bool(NOMBRE_VALIDO_RE.fullmatch(patron)) or bool(
            _PREFIJO_VALIDO_RE.fullmatch(patron))

    def suscribir(self, patron: str, manejador: Manejador) -> Callable[[], None]:
        """Suscribe ``manejador`` a ``patron`` y devuelve una función para desuscribirlo.

        Patrones: nombre exacto, prefijo con comodín (``"pc.*"``) o ``"*"``. Un patrón
        inválido se registra con ``log.error`` y no se suscribe (la función devuelta no hace nada).
        """
        if not isinstance(patron, str) or not self._patron_valido(patron):
            log.error("Patrón de suscripción inválido: %r; se ignora", patron)
            return lambda: None
        if not callable(manejador):
            log.error("El manejador para %s no es invocable; se ignora", patron)
            return lambda: None
        entrada = (patron, manejador)
        with self._lock:
            self._suscripciones.append(entrada)

        def desuscribir() -> None:
            with self._lock:
                for i, existente in enumerate(self._suscripciones):
                    if existente is entrada:
                        del self._suscripciones[i]
                        break

        return desuscribir

    def cola(self, patron: str, *, maxsize: int = 256) -> asyncio.Queue[Evento]:
        """Crea una ``asyncio.Queue`` suscrita a ``patron``.

        Si la cola se llena, se descarta el evento **más antiguo** y se cuenta en
        ``descartados``. Se libera con :meth:`soltar_cola`.
        """
        nueva: asyncio.Queue[Evento] = asyncio.Queue(maxsize=max(1, int(maxsize)))
        if not isinstance(patron, str) or not self._patron_valido(patron):
            log.error("Patrón de cola inválido: %r; la cola no recibirá eventos", patron)
            return nueva
        with self._lock:
            self._suscripciones.append((patron, _SuscriptorCola(nueva)))
        return nueva

    def soltar_cola(self, cola: asyncio.Queue[Evento]) -> None:
        """Desuscribe una cola creada con :meth:`cola`."""
        with self._lock:
            self._suscripciones = [
                (p, m) for (p, m) in self._suscripciones
                if not (isinstance(m, _SuscriptorCola) and m.cola is cola)
            ]

    # ------------------------------------------------------------------
    # Publicación
    # ------------------------------------------------------------------

    def _crear_evento(self, nombre: str, datos: Mapping[str, Any] | None,
                      origen: str) -> Evento | None:
        """Valida y construye el evento; registra y guarda el último. ``None`` si se descarta."""
        if self._cerrado:
            log.debug("Bus cerrado; se descarta el evento %s", nombre)
            return None
        if not isinstance(nombre, str) or not NOMBRE_VALIDO_RE.fullmatch(nombre):
            log.error("Nombre de evento inválido: %r; se descarta", nombre)
            return None
        if datos is None:
            payload: dict[str, Any] = {}
        elif isinstance(datos, Mapping):
            payload = dict(datos)
        else:
            log.error("Payload no es un diccionario en el evento %s; se descarta", nombre)
            return None
        evento = Evento(nombre=nombre, datos=payload, origen=str(origen or ""))
        with self._lock:
            self._publicados += 1
            self._ultimos[nombre] = evento
        return evento

    def publicar(self, nombre: str, datos: Mapping[str, Any] | None = None, *,
                 origen: str = "") -> None:
        """Publica un evento sin bloquear. Se puede llamar desde cualquier hilo.

        Desde un hilo distinto al del loop vinculado (y en ejecución) delega el despacho con
        ``loop.call_soon_threadsafe``; si no, despacha de inmediato. "Distinto hilo" se decide
        en cada llamada (¿se está ejecutando dentro de ese loop?), no con un hilo recordado.
        """
        evento = self._crear_evento(nombre, datos, origen)
        if evento is None:
            return
        self._autovincular()
        loop = self._loop
        if loop is not None and not loop.is_closed() and loop.is_running():
            if not self._dentro_del_loop(loop):
                try:
                    loop.call_soon_threadsafe(self._despachar, evento)
                except RuntimeError as exc:  # el loop se cerró entre la comprobación y la llamada
                    log.debug("Loop cerrado al publicar %s (%s); se descarta", nombre, exc)
                return
        self._despachar(evento)

    async def emitir(self, nombre: str, datos: Mapping[str, Any] | None = None, *,
                     origen: str = "") -> None:
        """Como :meth:`publicar`, pero espera a los manejadores asíncronos.

        Usa ``gather(..., return_exceptions=True)``: las excepciones se registran y no se propagan.
        """
        evento = self._crear_evento(nombre, datos, origen)
        if evento is None:
            return
        self._autovincular()
        tareas = self._despachar(evento)
        if tareas:
            await asyncio.gather(*tareas, return_exceptions=True)

    def _despachar(self, evento: Evento) -> list[asyncio.Task[Any]]:
        """Entrega ``evento`` a los suscriptores que casan. Devuelve las tareas creadas."""
        with self._lock:
            suscripciones = list(self._suscripciones)
        try:
            loop_actual: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop_actual = None
        tareas: list[asyncio.Task[Any]] = []
        for patron, manejador in suscripciones:
            if not _coincide(patron, evento.nombre):
                continue
            if isinstance(manejador, _SuscriptorCola):
                self._entregar_a_cola(manejador.cola, evento)
                continue
            if loop_actual is None and inspect.iscoroutinefunction(manejador):
                log.debug("Sin loop en ejecución: se descarta el manejador asíncrono %s de %s",
                          _nombre_manejador(manejador), evento.nombre)
                continue
            try:
                resultado = manejador(evento)
            except Exception as exc:
                with self._lock:
                    self._errores += 1
                log.error("Error en el manejador %s del evento %s: %s",
                          _nombre_manejador(manejador), evento.nombre, exc, exc_info=True)
                continue
            if inspect.isawaitable(resultado):
                tarea = self._programar(resultado, evento, manejador, loop_actual)
                if tarea is not None:
                    tareas.append(tarea)
                    with self._lock:
                        self._entregados += 1
                continue
            with self._lock:
                self._entregados += 1
        return tareas

    def _entregar_a_cola(self, cola: asyncio.Queue[Evento], evento: Evento) -> None:
        """``put_nowait``; si la cola está llena, descarta el más antiguo."""
        try:
            cola.put_nowait(evento)
        except asyncio.QueueFull:
            try:
                cola.get_nowait()
            except asyncio.QueueEmpty:
                log.debug("Cola llena y vacía a la vez (carrera); se reintenta")
            try:
                cola.put_nowait(evento)
            except asyncio.QueueFull:
                with self._lock:
                    self._descartados += 1
                log.debug("Cola llena: se descarta %s", evento.nombre)
                return
            with self._lock:
                self._descartados += 1
        except RuntimeError as exc:  # cola ligada a un loop ya cerrado
            with self._lock:
                self._errores += 1
            log.debug("No se pudo entregar %s a una cola: %s", evento.nombre, exc)
            return
        with self._lock:
            self._entregados += 1

    def _programar(self, awaitable: Any, evento: Evento, manejador: Any,
                   loop_actual: asyncio.AbstractEventLoop | None) -> asyncio.Task[Any] | None:
        """Programa un *awaitable* como tarea guardada en el set interno."""
        if loop_actual is None:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            log.debug("Sin loop en ejecución: se descarta el resultado asíncrono de %s (%s)",
                      _nombre_manejador(manejador), evento.nombre)
            return None
        try:
            if inspect.iscoroutine(awaitable):
                tarea = loop_actual.create_task(awaitable, name=f"bus:{evento.nombre}")
            else:
                tarea = asyncio.ensure_future(awaitable, loop=loop_actual)
        except Exception as exc:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            with self._lock:
                self._errores += 1
            log.error("No se pudo programar el manejador %s de %s: %s",
                      _nombre_manejador(manejador), evento.nombre, exc)
            return None
        tarea._rams_evento = evento.nombre  # type: ignore[attr-defined]
        tarea._rams_manejador = _nombre_manejador(manejador)  # type: ignore[attr-defined]
        with self._lock:
            self._tareas.add(tarea)
        tarea.add_done_callback(self._tarea_terminada)
        return tarea

    def _tarea_terminada(self, tarea: asyncio.Task[Any]) -> None:
        """Retira la tarea del set y registra su excepción, si la hubo."""
        with self._lock:
            self._tareas.discard(tarea)
        if tarea.cancelled():
            return
        excepcion = tarea.exception()
        if excepcion is None:
            return
        with self._lock:
            self._errores += 1
        log.error("Error en el manejador asíncrono %s del evento %s: %s",
                  getattr(tarea, "_rams_manejador", "?"), getattr(tarea, "_rams_evento", "?"),
                  excepcion, exc_info=(type(excepcion), excepcion, excepcion.__traceback__))

    # ------------------------------------------------------------------
    # Consulta y cierre
    # ------------------------------------------------------------------

    def ultimo(self, nombre: str) -> Evento | None:
        """Último evento publicado con ese nombre exacto (o ``None``)."""
        with self._lock:
            return self._ultimos.get(nombre)

    def estadisticas(self) -> dict[str, int]:
        """Contadores: publicados, entregados, descartados, errores y suscriptores."""
        with self._lock:
            return {
                "publicados": self._publicados,
                "entregados": self._entregados,
                "descartados": self._descartados,
                "errores": self._errores,
                "suscriptores": len(self._suscripciones),
            }

    async def cerrar(self, timeout: float = 2.0) -> None:
        """Deja de aceptar eventos, espera a las tareas pendientes hasta ``timeout`` y cancela el resto.

        También retira de logging el manejador instalado para este bus, si lo hay.
        """
        self._cerrado = True
        _retirar_manejadores_log(self)
        with self._lock:
            pendientes = [t for t in self._tareas if not t.done()]
        try:
            loop_actual: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop_actual = None
        propias = [t for t in pendientes if t.get_loop() is loop_actual]
        ajenas = [t for t in pendientes if t.get_loop() is not loop_actual]
        if propias:
            _hechas, sin_terminar = await asyncio.wait(propias, timeout=max(0.0, float(timeout)))
            if sin_terminar:
                log.debug("Cancelando %d manejadores del bus que no terminaron", len(sin_terminar))
                for tarea in sin_terminar:
                    tarea.cancel()
                await asyncio.wait(sin_terminar, timeout=0.5)
        for tarea in ajenas:
            loop_tarea = tarea.get_loop()
            if not loop_tarea.is_closed():
                try:
                    loop_tarea.call_soon_threadsafe(tarea.cancel)
                except RuntimeError as exc:
                    log.debug("No se pudo cancelar una tarea de otro loop: %s", exc)
        with self._lock:
            self._suscripciones.clear()
            self._tareas.clear()
        log.debug("Bus %s cerrado: %s", self.nombre, self.estadisticas())


# ---------------------------------------------------------------------------
# Manejador de logging → bus
# ---------------------------------------------------------------------------

_hilo_log = threading.local()


class _ManejadorLogBus(logging.Handler):
    """Publica registros de log como eventos ``log`` (§5): ``{nivel, modulo, mensaje, ts}``.

    Ignora los registros de ``rams.bus`` (evita recursión), corta la reentrada en el mismo hilo
    y limita a ``max_por_segundo`` eventos por segundo para que un manejador que registra
    avisos al recibir eventos ``log`` no provoque una tormenta.
    """

    def __init__(self, bus: BusEventos, nivel: int, *, max_por_segundo: int = 30) -> None:
        super().__init__(level=nivel)
        self._bus = bus
        self._max_por_segundo = max(1, int(max_por_segundo))
        self._ventana_inicio = time.monotonic()
        self._en_ventana = 0
        self.omitidos = 0

    def _permitir(self) -> bool:
        ahora = time.monotonic()
        if ahora - self._ventana_inicio >= 1.0:
            self._ventana_inicio = ahora
            self._en_ventana = 0
        if self._en_ventana >= self._max_por_segundo:
            self.omitidos += 1
            return False
        self._en_ventana += 1
        return True

    def emit(self, record: logging.LogRecord) -> None:
        """Publica el registro (ya redactado por ``FiltroSecretos``) en el bus."""
        if record.name == "rams.bus" or record.name.startswith("rams.bus."):
            return
        if getattr(_hilo_log, "activo", False):
            return
        _hilo_log.activo = True
        try:
            if not self._permitir():
                return
            mensaje = record.getMessage()
            if len(mensaje) > 2000:
                mensaje = mensaje[:1999] + "…"
            self._bus.publicar(
                "log",
                {"nivel": nombre_nivel(record.levelno), "modulo": record.name,
                 "mensaje": mensaje, "ts": float(record.created)},
                origen="log",
            )
        except Exception:  # logging estándar: informa por stderr sin propagar
            self.handleError(record)
        finally:
            _hilo_log.activo = False


def _retirar_manejadores_log(bus: BusEventos) -> None:
    """Quita del logger raíz los manejadores de log instalados para ``bus``."""
    raiz = logging.getLogger()
    for handler in list(raiz.handlers):
        if getattr(handler, "_rams_tipo", "") == "bus" and getattr(handler, "_rams_bus", None) is bus:
            raiz.removeHandler(handler)
            try:
                handler.close()
            except Exception as exc:
                log.debug("Error al cerrar el manejador de log del bus: %s", exc)


def instalar_manejador_log(bus: BusEventos, nivel: int = logging.WARNING, *,
                           secretos: Iterable[str] = ()) -> logging.Handler:
    """Instala en el logger raíz un handler que publica eventos ``log`` en ``bus`` (§4.2 punto 9).

    El handler (marcado ``_rams = True``) lleva ``FiltroSecretos(secretos)``: mensaje,
    ``exc_text`` y ``stack_info`` llegan redactados, y el payload usa solo el mensaje (sin
    traza). Es idempotente: si ya había uno para este ``bus``, lo reemplaza.
    """
    _retirar_manejadores_log(bus)
    manejador = _ManejadorLogBus(bus, nivel)
    manejador.addFilter(FiltroSecretos(secretos))
    manejador._rams = True  # type: ignore[attr-defined]
    manejador._rams_tipo = "bus"  # type: ignore[attr-defined]
    manejador._rams_bus = bus  # type: ignore[attr-defined]
    logging.getLogger().addHandler(manejador)
    return manejador
