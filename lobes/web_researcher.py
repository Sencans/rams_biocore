"""Investigación web en tiempo real de R.A.M.S. BIO-CORE (§10.16).

Qué hace:
- Busca en la web con la **Brave Search API** si hay ``BRAVE_API_KEY`` y, si no (o si Brave
  falla, se queda sin créditos o rechaza la clave), cae automáticamente a **DuckDuckGo**
  mediante ``ddgs`` (sucesor de ``duckduckgo-search``).
- Toda búsqueda corre bajo un **plazo global** (``asyncio.timeout(presupuesto_s)``): Brave
  recibe ``min(8, 0,6·presupuesto)`` segundos y DuckDuckGo el tiempo que quede. Si el plazo
  se agota, devuelve lo que haya (normalmente ``[]``) con ``ultimo_proveedor="ninguno"``.
- Sanea títulos y *snippets* (sin HTML, sin caracteres de control, sin ``[`` ``]`` ni
  ``<`` ``>``), porque son **datos no confiables** que acaban dentro del prompt de Gemini
  (§9.5): nunca deben poder formar etiquetas ``[EXP:...]`` ni etiquetas de Nexus.
- Mantiene una caché LRU (128 entradas, TTL configurable) para no gastar créditos ni
  peticiones repitiendo la misma consulta.

Amadeus "consulta la red" cuando algo excede sus datos de memoria; cuántas fuentes coinciden
lo verbaliza Broca (el único formateador del bloque ``<busqueda_web>`` es
``BrocaLLM.construir_contexto``, §10.18).

Dependencias opcionales:
- ``httpx`` (Brave). Sin él, Brave queda desactivado y se usa solo DuckDuckGo.
- ``ddgs`` (o el paquete heredado ``duckduckgo_search``). Sin él, solo Brave.

Degradación: en modo ``--offline`` devuelve ``[]`` al instante. Ninguna API pública propaga
excepciones de red al orquestador: ante cualquier fallo devuelve ``[]``. Las llamadas
interactivas no hacen *backoff* por límite de frecuencia.

Concurrencia (§6.1): DuckDuckGo es síncrono y corre en el executor propio
``ThreadPoolExecutor(2, "rams-web")``, que ``cerrar()`` apaga con
``shutdown(wait=False, cancel_futures=True)``. Los hilos de ``concurrent.futures`` se esperan al
salir del intérprete, así que cada consulta a ddgs está **acotada**: ``DDGS(timeout=...)`` recibe
el plazo que quede, entre 2 y 10 s (§6.1: «timeouts propios: ddgs 10 s»), y con ``ddgs`` ≥ 9 se
fijan dos motores de proveedores distintos (``backend``) para que el metabuscador haga **un solo
lote** de peticiones en paralelo: el peor caso es ≈ ``timeout`` y no ≈ 4·``timeout`` como con
``backend="auto"`` (unos 8 motores en lotes de 2). Desviación documentada respecto al ejemplo de
§10.16, que no pasa ``backend``. Al importarse no abre sockets, no crea hilos ni lee ``.env``
(§12.4).
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final

try:  # opcional: sin httpx no hay Brave (DuckDuckGo sigue funcionando)
    import httpx
except ImportError:  # sin httpx: solo DuckDuckGo
    httpx = None  # type: ignore[assignment]

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException
    _DDGS_METABUSCADOR = True    # ddgs ≥ 9: metabuscador que acepta ``backend``
except ImportError:
    _DDGS_METABUSCADOR = False
    try:  # paquete heredado (congelado en 8.1.1): solo DuckDuckGo, sin ``backend`` de motores
        from duckduckgo_search import DDGS  # type: ignore[no-redef]
        from duckduckgo_search.exceptions import (  # type: ignore[no-redef]
            DuckDuckGoSearchException as DDGSException,
            RatelimitException,
            TimeoutException,
        )
    except ImportError:  # sin ddgs ni duckduckgo_search: solo Brave
        DDGS = None  # type: ignore[assignment,misc]

        class DDGSException(Exception):  # type: ignore[no-redef]
            """Sustituto local cuando no hay ningún paquete de DuckDuckGo instalado."""

        RatelimitException = TimeoutException = DDGSException  # type: ignore[misc]

from core.config import Settings
from core.contracts import DiagnosticoModulo, EstadoModulo, ResultadoWeb

__all__ = ["WebResearcher", "BRAVE_URL", "MAX_CONSULTA", "PROVEEDORES"]

log = logging.getLogger("rams.web")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

BRAVE_URL: Final = "https://api.search.brave.com/res/v1/web/search"
PROVEEDORES: Final = ("brave", "ddgs", "cache", "ninguno")

NOMBRE_MODULO: Final = "Investigación web"
MAX_CONSULTA: Final = 300            # caracteres máximos de una consulta (se recorta)
MAX_RESULTADOS: Final = 20           # Brave admite count ≤ 20
MAX_TITULO: Final = 160
MAX_SNIPPET: Final = 300
MAX_EDAD: Final = 40
MAX_URL: Final = 2048
MAX_CACHE: Final = 128
PRESUPUESTO_MAX_S: Final = 60.0
BRAVE_TIMEOUT_MAX_S: Final = 8.0
BRAVE_FRACCION_PLAZO: Final = 0.6
PLAZO_MIN_REINTENTO_S: Final = 0.3   # tiempo mínimo restante para reintentar (422 o región)
PLAZO_MIN_DDGS_S: Final = 0.05       # por debajo de esto no merece la pena lanzar DuckDuckGo
DDGS_TIMEOUT_MIN_S: Final = 2        # DDGS(timeout=max(2, int(restante))) (§10.16)…
DDGS_TIMEOUT_MAX_S: Final = 10       # …y nunca más de 10 s (§6.1: «timeouts propios: ddgs 10 s»)
DDGS_TIMEOUT_DEF_S: Final = 5        # si _ddg_sync se llama fuera de _ejecutar_ddgs
DDGS_FRACCION_TIMEOUT: Final = 0.9   # «sin resultados» tras ≥ 90 % del timeout ⇒ fue un timeout
# Motores de ddgs ≥ 9. Dos motores de proveedores distintos, con una sola petición HTTP cada uno:
# el metabuscador usa min(proveedores, ceil(n/10)+1) = 2 hilos, así que los lanza en un único
# lote y el trabajo total queda acotado por ``timeout`` (con "auto" serían unos 8 motores en
# lotes de 2, ≈ 4·timeout, y el hilo seguiría trabajando mucho después de agotarse el plazo).
# Las noticias de DuckDuckGo salen de Bing (y piden antes un token "vqd": dos peticiones en
# serie), por eso se consulta Bing News directamente junto con Yahoo News.
DDGS_BACKEND_TEXTO: Final = "duckduckgo,brave"
DDGS_BACKEND_NOTICIAS: Final = "bing,yahoo"
REGION_GLOBAL: Final = "wt-wt"
FRESCURA_NOTICIAS: Final = "pw"      # noticias: última semana

# Frescura de Brave (pd/pw/pm/py) → timelimit de ddgs (d/w/m/y). Los rangos de fechas solo los
# entiende Brave; DuckDuckGo los ignora.
_TIMELIMIT_DDGS: Final[Mapping[str, str]] = {"pd": "d", "pw": "w", "pm": "m", "py": "y"}
_RE_RANGO_FRESCURA: Final = re.compile(r"^\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}$")

# Saneado (§10.16).
_RE_ETIQUETA_BLOQUE: Final = re.compile(
    r"<\s*/?\s*(?:br|p|div|li|ul|ol|tr|td|th|h[1-6]|hr|table|blockquote|section|article|"
    r"header|footer|nav|pre)\b[^>]*>",
    re.IGNORECASE,
)
_RE_ETIQUETA_HTML: Final = re.compile(r"<[^>]+>")
_RE_ESPACIOS_CONTROL: Final = re.compile(r"[\t\n\r\v\f\x85  ]")
_RE_ESPACIOS: Final = re.compile(r"\s+")
_CATEGORIAS_CONTROL: Final = frozenset({"Cc", "Cf", "Cs"})
_TABLA_CORCHETES: Final = str.maketrans({"[": "(", "]": ")", "<": "‹", ">": "›"})
_TABLA_URL: Final = str.maketrans({
    "[": "%5B", "]": "%5D", "<": "%3C", ">": "%3E", '"': "%22", "'": "%27",
    "`": "%60", " ": "%20", "{": "%7B", "}": "%7D", "|": "%7C", "\\": "%5C",
})
_RE_ESQUEMA_URL: Final = re.compile(r"^https?://[^/\s]+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _quitar_controles(texto: str) -> str:
    """Sustituye los saltos y tabuladores por espacios y elimina el resto de caracteres de
    control y de formato (incluye controles bidi y de ancho cero, vector típico de inyección)."""
    texto = _RE_ESPACIOS_CONTROL.sub(" ", texto)
    return "".join(c for c in texto if unicodedata.category(c) not in _CATEGORIAS_CONTROL)


def _truncar(texto: str, max_chars: int) -> str:
    """Trunca a ``max_chars`` caracteres como máximo, en un límite de palabra si es posible,
    terminando en ``…``."""
    if max_chars <= 0:
        return ""
    if len(texto) <= max_chars:
        return texto
    if max_chars < 2:
        return texto[:max_chars]
    corte = texto[: max_chars - 1]
    espacio = corte.rfind(" ")
    if espacio >= int(max_chars * 0.6):
        corte = corte[:espacio]
    return corte.rstrip(" ,.;:-–—") + "…"


def _preparar_consulta(consulta: Any) -> str:
    """Limpia la consulta: sin controles, espacios colapsados y como mucho ``MAX_CONSULTA``
    caracteres (recortada en un límite de palabra si es posible). Vacía ⇒ ``""``."""
    if consulta is None:
        return ""
    texto = _quitar_controles(str(consulta))
    texto = _RE_ESPACIOS.sub(" ", texto).strip()
    if len(texto) > MAX_CONSULTA:
        corte = texto[:MAX_CONSULTA]
        espacio = corte.rfind(" ")
        texto = (corte[:espacio] if espacio >= int(MAX_CONSULTA * 0.6) else corte).strip()
    return texto


def _normalizar(consulta: str) -> str:
    """Clave de caché de una consulta: NFKC, ``casefold`` y espacios colapsados."""
    texto = unicodedata.normalize("NFKC", consulta).casefold()
    return _RE_ESPACIOS.sub(" ", texto).strip()


def _normalizar_frescura(frescura: Any) -> str | None:
    """Acepta la frescura de Brave (``pd``/``pw``/``pm``/``py`` o ``AAAA-MM-DDtoAAAA-MM-DD``) o la
    de ddgs (``d``/``w``/``m``/``y``) y la devuelve en formato Brave. Inválida ⇒ ``None``."""
    if frescura is None:
        return None
    valor = str(frescura).strip().lower()
    if not valor:
        return None
    if valor in _TIMELIMIT_DDGS:
        return valor
    if valor in ("d", "w", "m", "y"):
        return "p" + valor
    if _RE_RANGO_FRESCURA.match(valor):
        return valor
    log.debug("Frescura de búsqueda no reconocida; se ignora")
    return None


def _entero_resultados(n: Any) -> int:
    """Número de resultados acotado a ``[1, MAX_RESULTADOS]`` (inválido ⇒ 5)."""
    try:
        valor = int(n)
    except (TypeError, ValueError):
        log.debug("Número de resultados inválido; uso 5")
        return 5
    return max(1, min(MAX_RESULTADOS, valor))


def _url_segura(url: Any) -> str:
    """Devuelve la URL si es ``http(s)`` y razonable, con los caracteres que podrían formar
    etiquetas (``[`` ``]`` ``<`` ``>`` y comillas) codificados en porcentaje. Si no, ``""``."""
    if not isinstance(url, str):
        return ""
    texto = _quitar_controles(url).strip()
    if not texto or len(texto) > MAX_URL or not _RE_ESQUEMA_URL.match(texto):
        return ""
    return texto.translate(_TABLA_URL)


def _es_ratelimit(exc: BaseException) -> bool:
    """``True`` si la excepción de DuckDuckGo indica límite de frecuencia (también envuelta)."""
    if isinstance(exc, RatelimitException) and RatelimitException is not DDGSException:
        return True
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, BaseException) and arg is not exc and _es_ratelimit(arg):
            return True
    return "ratelimit" in str(exc).lower()


def _es_timeout_ddgs(exc: BaseException) -> bool:
    """``True`` si la excepción de DuckDuckGo indica que se agotó su propio timeout."""
    if isinstance(exc, TimeoutException) and TimeoutException is not DDGSException:
        return True
    return "timed out" in str(exc).lower() or "timeout" in type(exc).__name__.lower()


def _es_sin_resultados(exc: BaseException) -> bool:
    """``True`` solo si la ``DDGSException`` significa «no hay resultados».

    ddgs ≥ 9 lanza ``DDGSException(err or "No results found.")``: si algún motor falló (red
    caída, TLS, HTTP…), ``err`` es la excepción de ese motor y viaja en ``args``. Eso es un
    **fallo** del proveedor (sin reintento de región y con ``ultimo_proveedor="ninguno"``), no
    una respuesta vacía.
    """
    if not isinstance(exc, DDGSException):
        return False
    if any(isinstance(arg, BaseException) for arg in getattr(exc, "args", ())):
        return False
    texto = str(exc).strip().lower()
    return not texto or "no results" in texto


# Excepciones que delatan un error de programación (nuestro o de compatibilidad con la versión
# de ddgs: firma distinta, atributo inexistente…), no un fallo de red: se registran con WARNING.
_ERRORES_DE_PROGRAMACION: Final = (TypeError, AttributeError, NameError, AssertionError)


def _timeout_ddgs(restante: float) -> int:
    """Timeout entero que recibe ``DDGS``: el plazo que quede, acotado a [2, 10] s."""
    try:
        valor = int(restante)
    except (TypeError, ValueError, OverflowError):
        valor = DDGS_TIMEOUT_DEF_S
    return max(DDGS_TIMEOUT_MIN_S, min(DDGS_TIMEOUT_MAX_S, valor))


class _Intento:
    """Acumulador de una búsqueda en curso: lo que haya si el plazo global se agota."""

    __slots__ = ("resultados", "proveedor")

    def __init__(self) -> None:
        self.resultados: list[ResultadoWeb] = []
        self.proveedor: str = "ninguno"


# ---------------------------------------------------------------------------
# WebResearcher
# ---------------------------------------------------------------------------


class WebResearcher:
    """Investigador web: Brave Search con *fallback* a DuckDuckGo, plazo global y caché.

    Args:
        settings: configuración (``tiene_web``, ``tiene_brave``, ``brave_api_key`` y
            ``web_region``).
        cliente: ``httpx.AsyncClient`` compartido (lo crea ``main.py``). Si no se pasa, se crea
            uno propio la primera vez que haga falta y se cierra en :meth:`cerrar`.
        ttl_s: vida de cada entrada de la caché, en segundos (``<= 0`` desactiva la caché).

    Atributos:
        ultimo_proveedor: quién respondió la última búsqueda: ``"brave"``, ``"ddgs"``,
            ``"cache"`` o ``"ninguno"`` (offline, sin respuesta o plazo agotado).
    """

    def __init__(self, settings: Settings, *, cliente: httpx.AsyncClient | None = None,
                 ttl_s: float = 900.0) -> None:
        self.settings = settings
        self.ultimo_proveedor: str = "ninguno"
        self._cliente: Any = cliente
        self._cliente_propio = False
        try:
            ttl = float(ttl_s)
        except (TypeError, ValueError):
            log.debug("TTL de caché inválido; uso 900 s")
            ttl = 900.0
        self._ttl_s = ttl if math.isfinite(ttl) else 900.0
        self._cache: OrderedDict[tuple[str, str, int, str | None],
                                 tuple[float, tuple[ResultadoWeb, ...]]] = OrderedDict()
        self._brave_desactivado = False
        self._motivo_brave: str = ""
        self._cerrado = False
        # Los hilos se crean de forma perezosa con el primer submit (nada al construir).
        self._ejecutor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rams-web")
        # Timeout de la consulta a DuckDuckGo en curso, por hilo: así _ddg_sync conserva la firma
        # normativa (q, n, timelimit, region) de §10.16 y un sustituto de test con esa firma
        # funciona tal cual, parcheado en la instancia o en la clase.
        self._local = threading.local()

    # ------------------------------------------------------------------ API pública

    async def buscar(self, consulta: str, *, n: int = 5, frescura: str | None = None,
                     presupuesto_s: float = 10.0) -> list[ResultadoWeb]:
        """Busca ``consulta`` en la web y devuelve como mucho ``n`` resultados saneados.

        ``frescura`` acepta ``pd``/``pw``/``pm``/``py`` (o ``d``/``w``/``m``/``y``) y rangos
        ``AAAA-MM-DDtoAAAA-MM-DD`` (estos solo los aplica Brave). Toda la búsqueda (Brave, sus
        reintentos, DuckDuckGo y su reintento de región) respeta el plazo global
        ``presupuesto_s``. Nunca lanza: ante cualquier fallo devuelve ``[]`` (solo
        propaga ``CancelledError`` si quien llama cancela la tarea).
        """
        return await self._consultar("web", consulta, n=n, frescura=_normalizar_frescura(frescura),
                                     presupuesto_s=presupuesto_s)

    async def noticias(self, consulta: str, *, n: int = 5,
                       presupuesto_s: float = 10.0) -> list[ResultadoWeb]:
        """Busca noticias recientes (última semana) sobre ``consulta``.

        Con Brave usa la búsqueda web con ``freshness=pw``; con DuckDuckGo usa ``.news()``
        (claves ``title``, ``url``, ``body`` y ``date``). Mismo plazo global y misma caché
        (con clave propia) que :meth:`buscar`. Nunca lanza.
        """
        return await self._consultar("noticias", consulta, n=n, frescura=FRESCURA_NOTICIAS,
                                     presupuesto_s=presupuesto_s)

    @staticmethod
    def sanear(texto: str, max_chars: int = 300) -> str:
        """Convierte texto web no confiable en texto plano seguro para el prompt (§10.16).

        1. Quita las etiquetas HTML (``<[^>]+>``) y desescapa las entidades (``html.unescape``).
        2. Quita los caracteres de control (y los de formato: bidi, ancho cero).
        3. ``[``→``(`` y ``]``→``)``: nunca forman etiquetas ``[EXP:...]`` ni de Nexus.
        4. ``<``→``‹`` y ``>``→``›``.
        5. Colapsa los espacios y trunca a ``max_chars`` (con ``…`` si se corta).
        """
        if texto is None:
            return ""
        crudo = texto if isinstance(texto, str) else str(texto)
        try:
            limite = int(max_chars)
        except (TypeError, ValueError):
            limite = 300
        # 1. Etiquetas HTML: las de bloque separan palabras; las de línea (<b>, <strong>…) no.
        limpio = _RE_ETIQUETA_BLOQUE.sub(" ", crudo)
        limpio = _RE_ETIQUETA_HTML.sub("", limpio)
        limpio = html.unescape(limpio)
        # Compatibilidad Unicode: los corchetes y signos de ancho completo pasan a su forma
        # ASCII y así también los cubren los pasos 3 y 4.
        limpio = unicodedata.normalize("NFKC", limpio)
        # 2. Caracteres de control.
        limpio = _quitar_controles(limpio)
        # 3 y 4. Corchetes y ángulos.
        limpio = limpio.translate(_TABLA_CORCHETES)
        # 5. Espacios y longitud.
        limpio = _RE_ESPACIOS.sub(" ", limpio).strip()
        return _truncar(limpio, limite)

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico sin gastar créditos (clave ``"web"``).

        ``OK (Brave)`` si hay Brave utilizable, ``OK (DuckDuckGo)`` si existe ``DDGS``,
        ``OFF (--offline)`` u ``OFF (sin ddgs ni clave Brave)``.
        """
        if self.settings.offline:
            return DiagnosticoModulo("web", NOMBRE_MODULO, EstadoModulo.OFF, "--offline")
        if self._brave_utilizable():
            return DiagnosticoModulo("web", NOMBRE_MODULO, EstadoModulo.OK, "Brave")
        if DDGS is not None:
            return DiagnosticoModulo("web", NOMBRE_MODULO, EstadoModulo.OK, "DuckDuckGo")
        if self.settings.tiene_brave and self._brave_desactivado:
            return DiagnosticoModulo("web", NOMBRE_MODULO, EstadoModulo.OFF,
                                     f"Brave desactivado ({self._motivo_brave}) y sin ddgs")
        if self.settings.tiene_brave and httpx is None:
            return DiagnosticoModulo("web", NOMBRE_MODULO, EstadoModulo.OFF,
                                     "sin httpx para Brave ni ddgs")
        return DiagnosticoModulo("web", NOMBRE_MODULO, EstadoModulo.OFF, "sin ddgs ni clave Brave")

    async def cerrar(self) -> None:
        """Cierra el cliente HTTP propio (si lo creó) y apaga el executor ``rams-web``.

        Idempotente. El executor se apaga sin esperar (``wait=False, cancel_futures=True``):
        las consultas a DuckDuckGo en curso están acotadas (un solo lote de motores con
        ``timeout`` ≤ 10 s) y las que esperaban en la cola se cancelan; las búsquedas que las
        esperaban devuelven ``[]`` (no propagan la cancelación).
        """
        if self._cerrado:
            return
        self._cerrado = True
        try:
            self._ejecutor.shutdown(wait=False, cancel_futures=True)
        except RuntimeError as exc:
            log.debug("No se pudo apagar el executor web: %s", exc)
        if self._cliente_propio and self._cliente is not None:
            cliente, self._cliente = self._cliente, None
            try:
                await cliente.aclose()
            except Exception as exc:  # noqa: BLE001 - el cierre nunca debe tumbar el apagado
                log.debug("Error al cerrar el cliente HTTP de la búsqueda web: %s",
                          type(exc).__name__)
        self._cache.clear()
        log.debug("Investigador web cerrado")

    # ------------------------------------------------------------------ Núcleo

    async def _consultar(self, clase: str, consulta: Any, *, n: Any, frescura: str | None,
                         presupuesto_s: Any) -> list[ResultadoWeb]:
        """Flujo común de :meth:`buscar` y :meth:`noticias` (caché, plazo global y proveedores)."""
        if self._cerrado:
            log.debug("Búsqueda web pedida tras cerrar el investigador; se ignora")
            self.ultimo_proveedor = "ninguno"
            return []
        if not self.settings.tiene_web:
            self.ultimo_proveedor = "ninguno"
            return []
        q = _preparar_consulta(consulta)
        if not q:
            log.debug("Consulta web vacía; no se busca")
            self.ultimo_proveedor = "ninguno"
            return []
        cantidad = _entero_resultados(n)
        presupuesto = self._presupuesto(presupuesto_s)
        if presupuesto <= 0:
            log.info("Búsqueda web sin presupuesto de tiempo; no se busca")
            self.ultimo_proveedor = "ninguno"
            return []

        clave = (clase, _normalizar(q), cantidad, frescura)
        en_cache = self._cache_leer(clave)
        if en_cache is not None:
            self.ultimo_proveedor = "cache"
            log.debug("Búsqueda web servida desde la caché (%d resultados)", len(en_cache))
            return list(en_cache)

        log.debug("Búsqueda web (%s): %s", clase, q[:200])
        intento = _Intento()
        inicio = time.monotonic()
        limite = inicio + presupuesto
        agotado = False
        try:
            async with asyncio.timeout(presupuesto):
                await self._resolver(clase, q, cantidad, frescura, presupuesto, limite, intento)
        except TimeoutError:
            agotado = True
            log.info("La búsqueda web agotó su plazo de %.1f s; devuelvo lo que haya (%d)",
                     presupuesto, len(intento.resultados))

        resultados = list(intento.resultados[:cantidad])
        self.ultimo_proveedor = "ninguno" if agotado else intento.proveedor
        if resultados and not agotado:
            self._cache_guardar(clave, tuple(resultados))
        log.info("Búsqueda web (%d caracteres): %d resultados vía %s en %d ms",
                 len(q), len(resultados), self.ultimo_proveedor,
                 int((time.monotonic() - inicio) * 1000))
        return resultados

    async def _resolver(self, clase: str, q: str, n: int, frescura: str | None,
                        presupuesto: float, limite: float, intento: _Intento) -> None:
        """Brave primero (si está disponible) y DuckDuckGo con el tiempo que quede."""
        brave = await self._buscar_brave(q, n, frescura, presupuesto, limite)
        if brave:
            intento.resultados, intento.proveedor = brave, "brave"
            return
        if brave is not None:  # Brave respondió, pero sin resultados
            intento.proveedor = "brave"
        ddg = await self._buscar_ddgs(clase, q, n, frescura, limite)
        if ddg is not None:
            intento.resultados, intento.proveedor = ddg, "ddgs"

    # ------------------------------------------------------------------ Brave

    def _brave_utilizable(self) -> bool:
        """Hay clave, httpx, no es offline y Brave no se desactivó en esta sesión."""
        return bool(self.settings.tiene_brave and httpx is not None and not self._brave_desactivado)

    def _obtener_cliente(self) -> Any:
        """Cliente HTTP compartido o, si no se recibió, uno propio creado ahora (§6.1)."""
        if self._cliente is None and httpx is not None:
            self._cliente = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0),
                                              headers={"User-Agent": "RAMS-BioCore/0.1"})
            self._cliente_propio = True
        return self._cliente

    def _desactivar_brave(self, motivo: str) -> None:
        """Desactiva Brave para el resto de la sesión (clave rechazada o sin créditos)."""
        self._brave_desactivado = True
        self._motivo_brave = motivo

    async def _buscar_brave(self, q: str, n: int, frescura: str | None, presupuesto: float,
                            limite: float) -> list[ResultadoWeb] | None:
        """Consulta Brave. ``None`` = no disponible o falló (se cae a DuckDuckGo);
        ``[]`` = respondió sin resultados."""
        if not self._brave_utilizable():
            return None
        cliente = self._obtener_cliente()
        if cliente is None:
            return None
        t_brave = min(BRAVE_TIMEOUT_MAX_S, BRAVE_FRACCION_PLAZO * presupuesto)
        params: dict[str, Any] = {"q": q, "count": n, "country": "CO", "search_lang": "es"}
        if frescura:
            params["freshness"] = frescura
        cabeceras = {"X-Subscription-Token": str(self.settings.brave_api_key),
                     "Accept": "application/json"}

        respuesta = await self._get_brave(cliente, params, cabeceras,
                                          min(t_brave, max(0.0, limite - time.monotonic())))
        if respuesta is None:
            return None
        if respuesta.status_code == 422:
            restante = limite - time.monotonic()
            if restante <= PLAZO_MIN_REINTENTO_S:
                log.info("Brave Search no acepta la región CO y no queda plazo para reintentar")
                return None
            log.info("Brave Search no acepta la región CO (422); reintento con país ALL")
            params["country"] = "ALL"
            respuesta = await self._get_brave(cliente, params, cabeceras, min(t_brave, restante))
            if respuesta is None:
                return None

        estado = respuesta.status_code
        if estado in (401, 403):
            self._desactivar_brave("clave rechazada")
            log.warning("Brave Search rechazó la clave (HTTP %d); la desactivo en esta sesión "
                        "y uso DuckDuckGo", estado)
            return None
        if estado == 402:
            self._desactivar_brave("sin créditos")
            log.warning("Brave Search sin créditos; uso DuckDuckGo")
            return None
        if estado == 429:
            log.info("Brave Search limitó la frecuencia (429); uso DuckDuckGo")
            return None
        if not 200 <= estado < 300:
            log.info("Brave Search respondió HTTP %d; uso DuckDuckGo", estado)
            return None
        try:
            datos = respuesta.json()
        except ValueError as exc:
            log.info("Brave Search devolvió un JSON inválido (%s); uso DuckDuckGo",
                     type(exc).__name__)
            return None
        return self._resultados_brave(datos, n)

    async def _get_brave(self, cliente: Any, params: Mapping[str, Any],
                         cabeceras: Mapping[str, str], timeout_s: float) -> Any:
        """Un GET a Brave con su timeout. Devuelve la respuesta o ``None`` si hubo error de red."""
        if timeout_s <= 0:
            return None
        try:
            return await cliente.get(BRAVE_URL, params=dict(params), headers=dict(cabeceras),
                                     timeout=httpx.Timeout(timeout_s))
        except httpx.TimeoutException:
            log.info("Brave Search no respondió en %.1f s; uso DuckDuckGo", timeout_s)
        except httpx.HTTPError as exc:
            log.info("Error de red con Brave Search (%s); uso DuckDuckGo", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - p. ej. cliente compartido ya cerrado
            log.info("Fallo inesperado con Brave Search (%s); uso DuckDuckGo", type(exc).__name__)
        return None

    def _resultados_brave(self, datos: Any, n: int) -> list[ResultadoWeb]:
        """Convierte ``web.results[]`` (``title``, ``url``, ``description``, ``age``) en
        ``ResultadoWeb`` saneados, leyendo siempre con ``.get()``."""
        if not isinstance(datos, Mapping):
            return []
        web = datos.get("web")
        filas = web.get("results") if isinstance(web, Mapping) else None
        if not isinstance(filas, list):
            return []
        return self._filas_a_resultados(filas, n, fuente="brave",
                                        claves_snippet=("description",), claves_edad=("age",))

    # ------------------------------------------------------------------ DuckDuckGo

    def _timeout_hilo(self) -> int:
        """Timeout (s) de la consulta a DuckDuckGo que corre en este hilo (lo fija
        :meth:`_en_hilo`; fuera de él, ``DDGS_TIMEOUT_DEF_S``)."""
        return int(getattr(self._local, "timeout_s", DDGS_TIMEOUT_DEF_S))

    def _en_hilo(self, funcion: Any, timeout_s: int, q: str, n: int, timelimit: str | None,
                 region: str) -> Any:
        """Corre en el executor ``rams-web``: fija el timeout del hilo y llama a ``funcion`` con
        la firma normativa ``(q, n, timelimit, region)``."""
        self._local.timeout_s = timeout_s
        try:
            return funcion(q, n, timelimit, region)
        finally:
            self._local.__dict__.pop("timeout_s", None)

    @staticmethod
    def _kwargs_ddgs(region: str, timelimit: str | None, n: int,
                     backend: str) -> dict[str, Any]:
        """Argumentos comunes de ``DDGS.text``/``DDGS.news``. ``backend`` solo con ddgs ≥ 9 (el
        paquete heredado no tiene motores que elegir)."""
        kwargs: dict[str, Any] = {"region": region, "safesearch": "moderate",
                                  "timelimit": timelimit, "max_results": n}
        if _DDGS_METABUSCADOR:
            kwargs["backend"] = backend
        return kwargs

    def _ddg_sync(self, q: str, n: int, timelimit: str | None,
                  region: str) -> list[dict[str, Any]]:
        """Búsqueda de texto síncrona en DuckDuckGo (corre en el executor ``rams-web``).

        Firma normativa de §10.16. Usa ``DDGS(timeout=...).text(...)`` sin *context manager*,
        con el timeout del hilo (:meth:`_timeout_hilo`, entre 2 y 10 s) y los motores
        ``DDGS_BACKEND_TEXTO`` (un solo lote), y devuelve las filas crudas (claves ``title``,
        ``href`` y ``body``). Las excepciones de ddgs se propagan: las interpreta
        :meth:`_ejecutar_ddgs`.
        """
        if DDGS is None:
            return []
        filas = DDGS(timeout=self._timeout_hilo()).text(
            q, **self._kwargs_ddgs(region, timelimit, n, DDGS_BACKEND_TEXTO))
        return list(filas or [])

    def _ddg_noticias_sync(self, q: str, n: int, timelimit: str | None,
                           region: str) -> list[dict[str, Any]]:
        """Búsqueda de noticias síncrona (``.news()``: claves ``title``, ``url``, ``body`` y
        ``date``), con los motores ``DDGS_BACKEND_NOTICIAS``. Misma firma y mismo timeout por
        hilo que :meth:`_ddg_sync`; las excepciones se propagan."""
        if DDGS is None:
            return []
        filas = DDGS(timeout=self._timeout_hilo()).news(
            q, **self._kwargs_ddgs(region, timelimit, n, DDGS_BACKEND_NOTICIAS))
        return list(filas or [])

    async def _buscar_ddgs(self, clase: str, q: str, n: int, frescura: str | None,
                           limite: float) -> list[ResultadoWeb] | None:
        """Consulta DuckDuckGo en la región configurada y, si vuelve vacía y queda plazo, en
        ``wt-wt``. ``None`` = no disponible o falló; ``[]`` = respondió sin resultados."""
        if DDGS is None or self._cerrado:
            return None
        timelimit = _TIMELIMIT_DDGS.get(frescura or "")
        region = str(self.settings.web_region or REGION_GLOBAL)
        regiones = [region] if region == REGION_GLOBAL else [region, REGION_GLOBAL]
        respondio = False
        for indice, reg in enumerate(regiones):
            restante = limite - time.monotonic()
            umbral = PLAZO_MIN_DDGS_S if indice == 0 else PLAZO_MIN_REINTENTO_S
            if restante <= umbral:
                if indice > 0:
                    log.info("DuckDuckGo sin resultados en %s y sin plazo para %s", region, reg)
                break
            if indice > 0:
                log.info("DuckDuckGo sin resultados en %s; reintento con %s", region, reg)
            filas = await self._ejecutar_ddgs(clase, q, n, timelimit, reg, restante)
            if filas is _LIMITE_FRECUENCIA:
                return None  # límite de frecuencia: [] de inmediato, sin backoff ni reintento
            if filas is None:
                return [] if respondio else None
            respondio = True
            resultados = self._filas_a_resultados(
                filas, n, fuente="ddgs",
                claves_snippet=("body", "description", "excerpt", "snippet"),
                claves_edad=("date", "age"))
            if resultados:
                return resultados
        return [] if respondio else None

    async def _ejecutar_ddgs(self, clase: str, q: str, n: int, timelimit: str | None,
                             region: str, restante: float) -> Any:
        """Lanza una consulta a DuckDuckGo en el executor propio e interpreta sus errores.

        Devuelve las filas (lista, posiblemente vacía), ``None`` si falló o
        ``_LIMITE_FRECUENCIA`` si DuckDuckGo limitó la frecuencia.
        """
        funcion = self._ddg_noticias_sync if clase == "noticias" else self._ddg_sync
        timeout_s = _timeout_ddgs(restante)
        loop = asyncio.get_running_loop()
        inicio = time.monotonic()
        try:
            filas = await loop.run_in_executor(self._ejecutor, self._en_hilo, funcion,
                                               timeout_s, q, n, timelimit, region)
        except asyncio.CancelledError:
            tarea = asyncio.current_task()
            if tarea is not None and tarea.cancelling():
                raise  # cancelación real de la tarea (incluida la expiración del plazo global)
            # cerrar() canceló la consulta que esperaba en la cola del executor
            # (shutdown(cancel_futures=True)): no es una cancelación de quien llama.
            log.info("Consulta a DuckDuckGo cancelada por el cierre del investigador web")
            return None
        except RuntimeError as exc:
            # Executor ya apagado (búsqueda en paralelo con cerrar()) u otro fallo interno.
            log.info("DuckDuckGo no disponible (%s)", type(exc).__name__)
            return None
        except _ERRORES_DE_PROGRAMACION as exc:
            # Firma o atributo equivocados (nuestros o por la versión de ddgs): no es un fallo
            # de red y no debe pasar desapercibido como tal.
            log.warning("Fallo interno al consultar DuckDuckGo (%s); revisa el código o la "
                        "versión de ddgs", type(exc).__name__, exc_info=True)
            return None
        except Exception as exc:  # noqa: BLE001 - ddgs/primp lanzan tipos muy variados
            if _es_ratelimit(exc):
                log.info("DuckDuckGo limitó la frecuencia; sin resultados (sin reintentos)")
                return _LIMITE_FRECUENCIA
            if _es_timeout_ddgs(exc):
                log.info("DuckDuckGo no respondió a tiempo en la región %s", region)
                return None
            if _es_sin_resultados(exc):
                if time.monotonic() - inicio >= DDGS_FRACCION_TIMEOUT * timeout_s:
                    # ddgs también dice «No results found.» cuando su wait(timeout) vence sin que
                    # ningún motor haya terminado: es un timeout, no una respuesta vacía (no se
                    # reintenta la región).
                    log.info("DuckDuckGo no respondió a tiempo en la región %s", region)
                    return None
                # ddgs lanza DDGSException("No results found.") cuando no hay resultados.
                log.debug("DuckDuckGo sin resultados en la región %s", region)
                return []
            # Incluye DDGSException(err) con la excepción de un motor (p. ej. red caída).
            log.info("Error consultando DuckDuckGo (%s)", type(exc).__name__)
            return None
        if not isinstance(filas, list):
            try:
                filas = list(filas or [])
            except TypeError:
                log.debug("DuckDuckGo devolvió un tipo inesperado; se ignora")
                return []
        return filas

    # ------------------------------------------------------------------ Conversión

    def _filas_a_resultados(self, filas: list[Any], n: int, *, fuente: str,
                            claves_snippet: tuple[str, ...],
                            claves_edad: tuple[str, ...]) -> list[ResultadoWeb]:
        """Convierte filas crudas (dicts) en ``ResultadoWeb`` saneados, sin duplicados y sin
        filas sin URL válida o sin texto."""
        resultados: list[ResultadoWeb] = []
        vistas: set[str] = set()
        for fila in filas:
            if len(resultados) >= n:
                break
            if not isinstance(fila, Mapping):
                continue
            url = _url_segura(fila.get("url") or fila.get("href") or fila.get("link"))
            if not url or url in vistas:
                continue
            titulo = self.sanear(_primero(fila, ("title",)), MAX_TITULO)
            snippet = self.sanear(_primero(fila, claves_snippet), MAX_SNIPPET)
            if not titulo and not snippet:
                continue
            edad = self.sanear(_primero(fila, claves_edad), MAX_EDAD) or None
            vistas.add(url)
            resultados.append(ResultadoWeb(titulo=titulo or _dominio(url), url=url,
                                           snippet=snippet, fuente=fuente, edad=edad))
        return resultados

    # ------------------------------------------------------------------ Caché y plazos

    def _cache_leer(self, clave: tuple[str, str, int, str | None]) -> tuple[ResultadoWeb, ...] | None:
        """Entrada vigente de la caché (y la marca como reciente) o ``None``."""
        if self._ttl_s <= 0:
            return None
        entrada = self._cache.get(clave)
        if entrada is None:
            return None
        guardado, resultados = entrada
        if time.monotonic() - guardado > self._ttl_s:
            del self._cache[clave]
            return None
        self._cache.move_to_end(clave)
        return resultados

    def _cache_guardar(self, clave: tuple[str, str, int, str | None],
                       resultados: tuple[ResultadoWeb, ...]) -> None:
        """Guarda en la caché LRU (máximo ``MAX_CACHE`` entradas)."""
        if self._ttl_s <= 0:
            return
        self._cache[clave] = (time.monotonic(), resultados)
        self._cache.move_to_end(clave)
        while len(self._cache) > MAX_CACHE:
            self._cache.popitem(last=False)

    @staticmethod
    def _presupuesto(presupuesto_s: Any) -> float:
        """Plazo global en segundos: inválido ⇒ 10 s; acotado a ``PRESUPUESTO_MAX_S``."""
        try:
            valor = float(presupuesto_s)
        except (TypeError, ValueError):
            log.debug("Presupuesto de búsqueda inválido; uso 10 s")
            return 10.0
        if math.isnan(valor):
            log.debug("Presupuesto de búsqueda inválido; uso 10 s")
            return 10.0
        return min(PRESUPUESTO_MAX_S, valor)


# Centinela: DuckDuckGo limitó la frecuencia (se devuelve [] sin reintentar).
_LIMITE_FRECUENCIA: Final = object()


def _primero(fila: Mapping[str, Any], claves: tuple[str, ...]) -> str:
    """Primer valor no vacío de ``claves`` en ``fila``, como texto."""
    for clave in claves:
        valor = fila.get(clave)
        if valor is None:
            continue
        texto = valor if isinstance(valor, str) else str(valor)
        if texto.strip():
            return texto
    return ""


def _dominio(url: str) -> str:
    """Dominio de una URL ``http(s)`` (para un resultado sin título)."""
    resto = url.split("://", 1)[-1]
    return resto.split("/", 1)[0].split("?", 1)[0][:MAX_TITULO]
