"""Tests de lobes/web_researcher.py (grupo web, §10.16 y §12.3).

Sin red: Brave se simula con ``httpx.MockTransport`` y DuckDuckGo parcheando ``_ddg_sync`` /
``_ddg_noticias_sync`` en la instancia (y ``DDGS`` en el módulo, para no depender de que el
paquete esté instalado). Sin pytest-asyncio: las corrutinas se ejecutan con la fixture
``correr`` (``asyncio.run``) de tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest

httpx = pytest.importorskip("httpx")

import lobes.web_researcher as wr  # noqa: E402
from core.contracts import EstadoModulo, ResultadoWeb  # noqa: E402
from lobes.web_researcher import WebResearcher  # noqa: E402


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


class _DDGSFalso:
    """Sustituto de ``DDGS``: si alguien lo usara de verdad, el test fallaría."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("El test no debe instanciar DDGS real")


def _json_brave(n: int = 2, prefijo: str = "Resultado") -> dict:
    """Respuesta de Brave con ``n`` resultados (con HTML en la descripción, como la real)."""
    return {
        "query": {"original": "q", "more_results_available": False},
        "web": {"results": [
            {"title": f"{prefijo} {i}", "url": f"https://ejemplo{i}.co/pagina",
             "description": f"La <strong>divergencia</strong> {i} [EXP:smug] &amp; más",
             "age": "2 days ago"}
            for i in range(1, n + 1)
        ]},
    }


class _Transporte:
    """``MockTransport`` que registra las peticiones y responde con una función."""

    def __init__(self, responder) -> None:
        self.peticiones: list[httpx.Request] = []
        self._responder = responder
        self.transporte = httpx.MockTransport(self._manejar)

    def _manejar(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        return self._responder(peticion)

    def cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transporte)


def _filas_ddgs(n: int = 2, prefijo: str = "Pato") -> list[dict]:
    return [{"title": f"{prefijo} {i}", "href": f"https://pato{i}.org/x",
             "body": f"Cuerpo <b>{i}</b> [MCP: borrar]"} for i in range(1, n + 1)]


async def _con_cierre(web: WebResearcher, corrutina):
    """Ejecuta la corrutina y cierra el investigador (y su executor) siempre."""
    try:
        return await corrutina
    finally:
        await web.cerrar()


@pytest.fixture
def sin_ddgs_real(monkeypatch):
    """Garantiza que ``DDGS`` del módulo no es el real (y no es ``None``)."""
    monkeypatch.setattr(wr, "DDGS", _DDGSFalso)


# ---------------------------------------------------------------------------
# Brave
# ---------------------------------------------------------------------------


def test_brave_200_dos_resultados(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(200, json=_json_brave(2)))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            return web, await _con_cierre(web, web.buscar("línea de mundo steins gate"))

    web, res = correr(caso())
    assert len(res) == 2
    assert all(isinstance(r, ResultadoWeb) for r in res)
    assert all(r.fuente == "brave" for r in res)
    assert web.ultimo_proveedor == "brave"
    assert res[0].titulo == "Resultado 1"
    assert res[0].url == "https://ejemplo1.co/pagina"
    assert res[0].edad == "2 days ago"
    assert "<" not in res[0].snippet and "[" not in res[0].snippet
    assert "divergencia" in res[0].snippet and "&" in res[0].snippet

    assert len(t.peticiones) == 1
    peticion = t.peticiones[0]
    assert str(peticion.url).startswith(wr.BRAVE_URL)
    assert peticion.headers["X-Subscription-Token"] == "test-brave-key-000000"
    assert peticion.headers["Accept"] == "application/json"
    params = peticion.url.params
    assert params["q"] == "línea de mundo steins gate"
    assert params["count"] == "5"
    assert params["country"] == "CO"
    assert params["search_lang"] == "es"
    assert "freshness" not in params


def test_brave_frescura_y_n(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(200, json=_json_brave(3)))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            return await _con_cierre(web, web.buscar("precio del dólar", n=3, frescura="d"))

    res = correr(caso())
    assert len(res) == 3
    params = t.peticiones[0].url.params
    assert params["freshness"] == "pd"
    assert params["count"] == "3"


def test_brave_422_reintenta_con_all(settings_con_claves, correr, sin_ddgs_real):
    def responder(p: httpx.Request) -> httpx.Response:
        if p.url.params.get("country") == "ALL":
            return httpx.Response(200, json=_json_brave(2))
        return httpx.Response(422, json={"error": "país no soportado"})

    t = _Transporte(responder)

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            return web, await _con_cierre(web, web.buscar("kurisu makise"))

    web, res = correr(caso())
    assert len(res) == 2 and all(r.fuente == "brave" for r in res)
    assert [p.url.params["country"] for p in t.peticiones] == ["CO", "ALL"]
    assert web.ultimo_proveedor == "brave"


def test_brave_402_cae_a_ddgs_y_se_desactiva(settings_con_claves, correr, sin_ddgs_real,
                                             caplog):
    t = _Transporte(lambda p: httpx.Response(402, json={"error": "sin créditos"}))
    llamadas: list[tuple] = []

    def ddg_falso(q, n, timelimit, region):
        llamadas.append((q, n, timelimit, region))
        return _filas_ddgs(2, prefijo=q)

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            web._ddg_sync = ddg_falso
            primera = await web.buscar("primera consulta")
            prov1 = web.ultimo_proveedor
            segunda = await _con_cierre(web, web.buscar("segunda consulta distinta"))
            return web, primera, prov1, segunda

    with caplog.at_level(logging.WARNING, logger="rams.web"):
        web, primera, prov1, segunda = correr(caso())
    assert len(primera) == 2 and all(r.fuente == "ddgs" for r in primera)
    assert prov1 == "ddgs"
    assert len(segunda) == 2 and all(r.fuente == "ddgs" for r in segunda)
    assert web.ultimo_proveedor == "ddgs"
    assert len(t.peticiones) == 1, "la segunda búsqueda no debe tocar Brave"
    assert len(llamadas) == 2
    assert any("Brave Search sin créditos; uso DuckDuckGo" in r.getMessage()
               for r in caplog.records)
    diag = web.diagnostico()
    assert diag.estado is EstadoModulo.OK and diag.detalle == "DuckDuckGo"


@pytest.mark.parametrize("codigo", [401, 403])
def test_brave_401_403_desactiva(settings_con_claves, correr, sin_ddgs_real, codigo):
    t = _Transporte(lambda p: httpx.Response(codigo))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            web._ddg_sync = lambda q, n, tl, reg: _filas_ddgs(1)
            await web.buscar("uno")
            return await _con_cierre(web, web.buscar("dos"))

    res = correr(caso())
    assert len(res) == 1 and res[0].fuente == "ddgs"
    assert len(t.peticiones) == 1


def test_brave_429_cae_a_ddgs_sin_desactivar(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(429))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            web._ddg_sync = lambda q, n, tl, reg: _filas_ddgs(1)
            r1 = await web.buscar("uno")
            r2 = await _con_cierre(web, web.buscar("dos"))
            return web, r1, r2

    web, r1, r2 = correr(caso())
    assert r1[0].fuente == "ddgs" and r2[0].fuente == "ddgs"
    assert len(t.peticiones) == 2, "un 429 no desactiva Brave para la sesión"


def test_brave_error_de_red_cae_a_ddgs(settings_con_claves, correr, sin_ddgs_real):
    def responder(p: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=p)

    t = _Transporte(responder)

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            web._ddg_sync = lambda q, n, tl, reg: _filas_ddgs(2)
            return web, await _con_cierre(web, web.buscar("algo"))

    web, res = correr(caso())
    assert len(res) == 2 and web.ultimo_proveedor == "ddgs"


# ---------------------------------------------------------------------------
# DuckDuckGo
# ---------------------------------------------------------------------------


def test_sin_clave_usa_ddgs(settings_prueba, correr, sin_ddgs_real):
    settings = settings_prueba.con(offline=False)
    assert settings.tiene_web and not settings.tiene_brave
    llamadas: list[tuple] = []
    hilos: list[str] = []

    async def caso():
        web = WebResearcher(settings)

        def ddg_falso(q, n, timelimit, region):  # firma normativa de §10.16, sin **kw
            llamadas.append((q, n, timelimit, region, web._timeout_hilo()))
            hilos.append(threading.current_thread().name)
            return _filas_ddgs(2)

        web._ddg_sync = ddg_falso
        return web, await _con_cierre(web, web.buscar("el psy kongroo", frescura="pw"))

    web, res = correr(caso())
    assert len(res) == 2
    assert all(r.fuente == "ddgs" for r in res)
    assert web.ultimo_proveedor == "ddgs"
    assert res[0].url == "https://pato1.org/x"
    assert "[" not in res[0].snippet and "<b>" not in res[0].snippet
    q, n, timelimit, region, timeout_s = llamadas[0]
    assert (q, n, timelimit, region) == ("el psy kongroo", 5, "w", "co-es")
    assert isinstance(timeout_s, int) and 2 <= timeout_s <= 10
    assert hilos[0].startswith("rams-web"), "ddgs corre en el executor propio"


def test_ddgs_region_vacia_reintenta_wt_wt(settings_prueba, correr, sin_ddgs_real):
    settings = settings_prueba.con(offline=False)
    regiones: list[str] = []

    def ddg_falso(q, n, timelimit, region):
        regiones.append(region)
        if region == "co-es":
            raise wr.DDGSException("No results found.")
        return _filas_ddgs(1)

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_falso
        return web, await _con_cierre(web, web.buscar("hououin kyouma"))

    web, res = correr(caso())
    assert regiones == ["co-es", "wt-wt"]
    assert len(res) == 1 and web.ultimo_proveedor == "ddgs"


def test_ddgs_ratelimit_devuelve_vacio_sin_reintentos(settings_prueba, correr, monkeypatch):
    class Ratelimit(Exception):
        """Simula ddgs.exceptions.RatelimitException."""

    monkeypatch.setattr(wr, "DDGS", _DDGSFalso)
    monkeypatch.setattr(wr, "RatelimitException", Ratelimit)
    settings = settings_prueba.con(offline=False)
    regiones: list[str] = []

    def ddg_falso(q, n, timelimit, region):
        regiones.append(region)
        raise Ratelimit("202 Ratelimit")

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_falso
        inicio = time.monotonic()
        res = await _con_cierre(web, web.buscar("algo"))
        return web, res, time.monotonic() - inicio

    web, res, duracion = correr(caso())
    assert res == []
    assert regiones == ["co-es"], "sin reintento de región ni backoff tras un ratelimit"
    assert duracion < 1.0
    assert web.ultimo_proveedor == "ninguno"


def test_noticias_ddgs(settings_prueba, correr, sin_ddgs_real):
    settings = settings_prueba.con(offline=False)
    llamadas: list[tuple] = []

    def noticias_falsas(q, n, timelimit, region):
        llamadas.append((q, n, timelimit, region))
        return [{"title": "Futuro Gadget Lab", "url": "https://noticias.co/a",
                 "body": "Nuevo <i>invento</i>", "date": "2026-10-15T12:00:00+00:00",
                 "source": "Lab"}]

    async def caso():
        web = WebResearcher(settings)
        web._ddg_noticias_sync = noticias_falsas
        web._ddg_sync = lambda *a, **k: pytest.fail("noticias no debe usar .text()")
        return web, await _con_cierre(web, web.noticias("laboratorio", n=3))

    web, res = correr(caso())
    assert len(res) == 1
    assert res[0].fuente == "ddgs" and res[0].edad == "2026-10-15T12:00:00+00:00"
    assert res[0].snippet == "Nuevo invento"
    assert llamadas == [("laboratorio", 3, "w", "co-es")]


def test_plazo_global(settings_prueba, correr, sin_ddgs_real):
    settings = settings_prueba.con(offline=False)
    liberar = threading.Event()

    def ddg_lento(q, n, timelimit, region):
        liberar.wait(5)  # "duerme" 5 s (se libera al terminar el test)
        return _filas_ddgs(2)

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_lento
        inicio = time.monotonic()
        res = await web.buscar("consulta lenta", presupuesto_s=0.5)
        duracion = time.monotonic() - inicio
        await web.cerrar()
        return web, res, duracion

    try:
        web, res, duracion = correr(caso())
    finally:
        liberar.set()
    assert res == []
    assert duracion < 1.5
    assert web.ultimo_proveedor == "ninguno"


def test_plazo_global_con_brave_lento(settings_con_claves, correr, sin_ddgs_real):
    """El plazo también corta a Brave (y a ddgs después) aunque el transporte se cuelgue."""

    async def lento(peticion: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_json_brave(2))

    async def caso():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lento)) as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            web._ddg_sync = lambda *a, **k: []
            inicio = time.monotonic()
            res = await _con_cierre(web, web.buscar("algo", presupuesto_s=0.5))
            return web, res, time.monotonic() - inicio

    web, res, duracion = correr(caso())
    assert res == [] and duracion < 1.5
    assert web.ultimo_proveedor == "ninguno"


def test_ddg_sync_parcheado_en_la_clase(settings_prueba, correr, sin_ddgs_real, monkeypatch):
    """Un sustituto con la firma de §10.16 parcheado en la clase también funciona."""
    settings = settings_prueba.con(offline=False)
    llamadas: list[tuple] = []

    def falso_cls(self, q, n, timelimit, region):
        llamadas.append((q, n, timelimit, region))
        return _filas_ddgs(2)

    monkeypatch.setattr(WebResearcher, "_ddg_sync", falso_cls)

    async def caso():
        web = WebResearcher(settings)
        return web, await _con_cierre(web, web.buscar("mayuri"))

    web, res = correr(caso())
    assert len(res) == 2 and web.ultimo_proveedor == "ddgs"
    assert llamadas == [("mayuri", 5, None, "co-es")]


class _DDGSRegistro:
    """Sustituto de ``DDGS`` que registra el timeout y los argumentos de cada búsqueda."""

    llamadas: list[tuple] = []

    def __init__(self, timeout=5, **kwargs) -> None:
        self.timeout = timeout

    def text(self, query, **kwargs):
        self.llamadas.append(("text", self.timeout, query, kwargs))
        return _filas_ddgs(1)

    def news(self, query, **kwargs):
        self.llamadas.append(("news", self.timeout, query, kwargs))
        return [{"title": "Nota", "url": "https://n.co/1", "body": "b", "date": "hoy"}]


@pytest.mark.parametrize("metabuscador", [True, False])
def test_ddg_sync_real_acota_motores_y_timeout(settings_prueba, correr, monkeypatch, metabuscador):
    """``DDGS`` recibe el timeout del hilo (2-10 s) y, con ddgs ≥ 9, dos motores (un lote)."""
    monkeypatch.setattr(wr, "DDGS", _DDGSRegistro)
    monkeypatch.setattr(wr, "_DDGS_METABUSCADOR", metabuscador)
    monkeypatch.setattr(_DDGSRegistro, "llamadas", [])
    settings = settings_prueba.con(offline=False)

    async def caso():
        web = WebResearcher(settings)
        r1 = await web.buscar("okabe", presupuesto_s=60)
        r2 = await _con_cierre(web, web.noticias("sern", n=3, presupuesto_s=3.7))
        return r1, r2

    r1, r2 = correr(caso())
    assert len(r1) == 1 and len(r2) == 1
    (m1, t1, q1, kw1), (m2, t2, q2, kw2) = _DDGSRegistro.llamadas
    assert (m1, q1, m2, q2) == ("text", "okabe", "news", "sern")
    assert t1 == 10, "el timeout de ddgs nunca pasa de 10 s (§6.1)"
    assert t2 == 3
    assert kw1["region"] == "co-es" and kw1["safesearch"] == "moderate"
    assert kw1["max_results"] == 5 and kw1["timelimit"] is None
    assert kw2["max_results"] == 3 and kw2["timelimit"] == "w"
    if metabuscador:
        assert kw1["backend"] == wr.DDGS_BACKEND_TEXTO
        assert kw2["backend"] == wr.DDGS_BACKEND_NOTICIAS
        for backend in (wr.DDGS_BACKEND_TEXTO, wr.DDGS_BACKEND_NOTICIAS):
            assert len(backend.split(",")) <= 2, "como mucho dos motores: un solo lote"
    else:
        assert "backend" not in kw1 and "backend" not in kw2


def test_timeout_ddgs_acotado():
    assert wr._timeout_ddgs(0.4) == 2
    assert wr._timeout_ddgs(7.9) == 7
    assert wr._timeout_ddgs(59.0) == 10
    assert wr._timeout_ddgs(float("nan")) == wr.DDGS_TIMEOUT_DEF_S


def test_timeout_por_hilo_no_se_filtra():
    """Fuera de una consulta, el timeout del hilo es el valor por defecto."""
    web = WebResearcher.__new__(WebResearcher)
    web._local = threading.local()
    assert web._timeout_hilo() == wr.DDGS_TIMEOUT_DEF_S
    assert web._en_hilo(lambda q, n, tl, reg: web._timeout_hilo(), 7, "q", 1, None, "co-es") == 7
    assert web._timeout_hilo() == wr.DDGS_TIMEOUT_DEF_S


def test_error_de_programacion_se_registra_como_warning(settings_prueba, correr, sin_ddgs_real,
                                                        caplog):
    settings = settings_prueba.con(offline=False)
    regiones: list[str] = []

    def ddg_roto(q, n, timelimit, region):
        regiones.append(region)
        raise TypeError("firma inesperada")

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_roto
        return web, await _con_cierre(web, web.buscar("algo"))

    with caplog.at_level(logging.INFO, logger="rams.web"):
        web, res = correr(caso())
    assert res == [] and web.ultimo_proveedor == "ninguno"
    assert regiones == ["co-es"]
    avisos = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert avisos and "TypeError" in avisos[0].getMessage()


def test_ddgs_error_de_red_envuelto_no_reintenta(settings_prueba, correr, sin_ddgs_real):
    """``DDGSException(err)`` con la excepción de un motor es un fallo, no «sin resultados»."""
    settings = settings_prueba.con(offline=False)
    regiones: list[str] = []

    def ddg_sin_red(q, n, timelimit, region):
        regiones.append(region)
        raise wr.DDGSException(wr.DDGSException("ConnectError: sin red"))

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_sin_red
        return web, await _con_cierre(web, web.buscar("algo"))

    web, res = correr(caso())
    assert res == []
    assert regiones == ["co-es"], "un error de red no reintenta con wt-wt"
    assert web.ultimo_proveedor == "ninguno"


def test_ddgs_sin_resultados_tras_agotar_timeout_no_reintenta(settings_prueba, correr,
                                                             sin_ddgs_real, monkeypatch):
    """«No results found.» tras consumir su timeout = motores colgados: sin reintento wt-wt."""
    monkeypatch.setattr(wr, "DDGS_TIMEOUT_MIN_S", 0)  # timeout de 1 s para un test rápido
    settings = settings_prueba.con(offline=False)
    regiones: list[str] = []

    async def caso():
        web = WebResearcher(settings)

        def ddg_colgado(q, n, timelimit, region):
            regiones.append(region)
            time.sleep(web._timeout_hilo())
            raise wr.DDGSException("No results found.")

        web._ddg_sync = ddg_colgado
        return web, await _con_cierre(web, web.buscar("algo", presupuesto_s=1.9))

    web, res = correr(caso())
    assert res == [] and regiones == ["co-es"]
    assert web.ultimo_proveedor == "ninguno"


def test_es_sin_resultados():
    assert wr._es_sin_resultados(wr.DDGSException("No results found."))
    assert not wr._es_sin_resultados(wr.DDGSException(ValueError("x")))
    assert not wr._es_sin_resultados(wr.DDGSException("ConnectError: sin red"))
    assert not wr._es_sin_resultados(ValueError("No results found."))


def test_cerrar_con_consulta_en_cola_no_propaga_cancelacion(settings_prueba, correr,
                                                           sin_ddgs_real):
    """``cerrar()`` cancela la consulta que esperaba en la cola: esa búsqueda devuelve ``[]``."""
    settings = settings_prueba.con(offline=False)
    liberar = threading.Event()

    def ddg_bloqueado(q, n, timelimit, region):
        liberar.wait(5)
        return _filas_ddgs(1)

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_bloqueado
        tiempos: dict[str, float] = {}
        inicio = time.monotonic()

        async def una(consulta: str):
            try:
                return await web.buscar(consulta, presupuesto_s=1.0)
            finally:
                tiempos[consulta] = time.monotonic() - inicio

        tareas = [asyncio.create_task(una(f"consulta {i}")) for i in range(3)]
        await asyncio.sleep(0.2)
        await web.cerrar()
        res = await asyncio.gather(*tareas, return_exceptions=True)
        return res, tiempos

    try:
        res, tiempos = correr(caso())
    finally:
        liberar.set()
    assert res == [[], [], []], res
    assert tiempos["consulta 2"] < 0.9, "la consulta en cola vuelve al cerrar, sin esperar plazo"


def test_cancelacion_real_se_propaga(settings_prueba, correr, sin_ddgs_real):
    """Si quien llama cancela la tarea, la cancelación sí se propaga."""
    settings = settings_prueba.con(offline=False)
    liberar = threading.Event()

    def ddg_bloqueado(q, n, timelimit, region):
        liberar.wait(5)
        return []

    async def caso():
        web = WebResearcher(settings)
        web._ddg_sync = ddg_bloqueado
        tarea = asyncio.create_task(web.buscar("algo", presupuesto_s=5))
        await asyncio.sleep(0.1)
        tarea.cancel()
        try:
            await tarea
        except asyncio.CancelledError:
            cancelada = True
        else:
            cancelada = False
        await web.cerrar()
        return cancelada

    try:
        assert correr(caso()) is True
    finally:
        liberar.set()


# ---------------------------------------------------------------------------
# Offline, caché, consulta y saneado
# ---------------------------------------------------------------------------


def test_offline_devuelve_vacio(settings_prueba, correr, sin_ddgs_real):
    assert settings_prueba.offline
    t = _Transporte(lambda p: httpx.Response(200, json=_json_brave(2)))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_prueba, cliente=cliente)
            web._ddg_sync = lambda *a, **k: pytest.fail("offline no debe consultar ddgs")
            inicio = time.monotonic()
            res = await _con_cierre(web, web.buscar("algo"))
            return web, res, time.monotonic() - inicio

    web, res, duracion = correr(caso())
    assert res == []
    assert web.ultimo_proveedor == "ninguno"
    assert t.peticiones == []
    assert duracion < 0.5


def test_cache_no_toca_el_transporte(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(200, json=_json_brave(2)))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            r1 = await web.buscar("Divergencia  1.048596")
            prov1 = web.ultimo_proveedor
            r2 = await web.buscar("  divergencia 1.048596 ")
            prov2 = web.ultimo_proveedor
            r3 = await _con_cierre(web, web.buscar("divergencia 1.048596", n=3))
            return r1, prov1, r2, prov2, r3

    r1, prov1, r2, prov2, r3 = correr(caso())
    assert prov1 == "brave" and prov2 == "cache"
    assert r1 == r2
    assert len(t.peticiones) == 2, "otra n es otra clave de caché"
    assert len(r3) == 2


def test_cache_ttl_expira(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(200, json=_json_brave(1)))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente, ttl_s=60)
            await web.buscar("algo")
            # Envejece la entrada 61 s (sin tocar el reloj global, que usa el event loop).
            for clave, (guardado, resultados) in list(web._cache.items()):
                web._cache[clave] = (guardado - 61, resultados)
            await _con_cierre(web, web.buscar("algo"))
            return web

    web = correr(caso())
    assert len(t.peticiones) == 2
    assert web.ultimo_proveedor == "brave"


def test_consulta_vacia_y_larga(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(200, json=_json_brave(1)))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            vacia = await web.buscar("   \n\t ")
            prov_vacia = web.ultimo_proveedor
            larga = await _con_cierre(web, web.buscar("palabra " * 100))
            return vacia, prov_vacia, larga

    vacia, prov_vacia, larga = correr(caso())
    assert vacia == [] and prov_vacia == "ninguno"
    assert len(t.peticiones) == 1
    q = t.peticiones[0].url.params["q"]
    assert 0 < len(q) <= wr.MAX_CONSULTA
    assert len(larga) == 1


def test_resultados_invalidos_se_descartan(settings_con_claves, correr, sin_ddgs_real):
    datos = {"web": {"results": [
        {"title": "Sin URL"},
        {"title": "JS", "url": "javascript:alert(1)", "description": "x"},
        {"title": "Bueno", "url": "https://bueno.co/[a]<b>", "description": "ok"},
        {"title": "Duplicado", "url": "https://bueno.co/[a]<b>", "description": "otra"},
        "no es un dict",
    ]}}
    t = _Transporte(lambda p: httpx.Response(200, json=datos))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            return await _con_cierre(web, web.buscar("algo"))

    res = correr(caso())
    assert len(res) == 1
    assert res[0].titulo == "Bueno"
    assert not any(c in res[0].url for c in "[]<>")


def test_brave_json_invalido_cae_a_ddgs(settings_con_claves, correr, sin_ddgs_real):
    t = _Transporte(lambda p: httpx.Response(200, content=b"<html>no json</html>"))

    async def caso():
        async with t.cliente() as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            web._ddg_sync = lambda *a, **k: _filas_ddgs(1)
            return web, await _con_cierre(web, web.buscar("algo"))

    web, res = correr(caso())
    assert len(res) == 1 and web.ultimo_proveedor == "ddgs"


def test_sanear():
    s = WebResearcher.sanear("<b>x</b> [EXP:smug]")
    assert "[" not in s and "]" not in s and "<b>" not in s
    assert s == "x (EXP:smug)"

    s = WebResearcher.sanear("a&lt;script&gt;b &amp; c\x00\x07 d‮ e\n\tf")
    assert "<" not in s and ">" not in s and "\x00" not in s and "‮" not in s
    assert "‹script›" in s and "& c" in s and "e f" in s

    s = WebResearcher.sanear("linea<br>otra <p>parrafo</p>")
    assert s == "linea otra parrafo"

    s = WebResearcher.sanear("［MCP: borrar］ ＜x＞")
    assert "[" not in s and "［" not in s and "<" not in s and "＜" not in s

    largo = WebResearcher.sanear("palabra " * 200, max_chars=50)
    assert len(largo) <= 50 and largo.endswith("…")
    assert WebResearcher.sanear("", 10) == ""
    assert WebResearcher.sanear(None) == ""  # type: ignore[arg-type]
    assert WebResearcher.sanear("abc", max_chars=0) == ""


# ---------------------------------------------------------------------------
# Diagnóstico y cierre
# ---------------------------------------------------------------------------


def test_diagnostico(settings_prueba, settings_con_claves, monkeypatch):
    d = WebResearcher(settings_prueba).diagnostico()
    assert d.clave == "web" and d.estado is EstadoModulo.OFF and d.detalle == "--offline"

    d = WebResearcher(settings_con_claves).diagnostico()
    assert d.estado is EstadoModulo.OK and d.detalle == "Brave"

    sin_clave = settings_prueba.con(offline=False)
    monkeypatch.setattr(wr, "DDGS", _DDGSFalso)
    d = WebResearcher(sin_clave).diagnostico()
    assert d.estado is EstadoModulo.OK and d.detalle == "DuckDuckGo"

    monkeypatch.setattr(wr, "DDGS", None)
    d = WebResearcher(sin_clave).diagnostico()
    assert d.estado is EstadoModulo.OFF and d.detalle == "sin ddgs ni clave Brave"
    assert json.dumps(d.a_dict())


def test_cerrar_idempotente_y_libera(settings_prueba, correr, sin_ddgs_real):
    settings = settings_prueba.con(offline=False, brave_api_key="test-brave-key-000000")

    async def caso():
        web = WebResearcher(settings)  # sin cliente: crea uno propio al usar Brave
        cliente_propio = web._obtener_cliente()
        assert web._cliente_propio
        await web.cerrar()
        await web.cerrar()
        res = await web.buscar("tras cerrar")
        return web, cliente_propio, res

    web, cliente_propio, res = correr(caso())
    assert cliente_propio.is_closed
    assert web._ejecutor._shutdown
    assert res == [] and web.ultimo_proveedor == "ninguno"


def test_cerrar_no_cierra_cliente_compartido(settings_con_claves, correr):
    async def caso():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda p: httpx.Response(200, json=_json_brave(1)))) as cliente:
            web = WebResearcher(settings_con_claves, cliente=cliente)
            await web.cerrar()
            return cliente.is_closed

    assert correr(caso()) is False
