"""Tests de lobes/riko_megumin_io.py (grupo io-dispatch, §10.19 y §12.3).

Sin red ni audio: ``httpx.MockTransport`` simula Telegram y ElevenLabs, el avatar y la memoria
son falsos (la memoria implementa todo ``RegistroLlamadas``), ``imprimir`` se captura y el
reproductor de audio se sustituye por uno en memoria. edge-tts se sustituye por un falso que
entrega un MP3 real escrito con soundfile (camino sin ffmpeg). Sin pytest-asyncio: ``asyncio.run``.
"""

from __future__ import annotations

import array
import asyncio
import io
import json
import logging
import math
import subprocess
import sys
import threading
import time
import types
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")

import lobes.riko_megumin_io as riko_mod  # noqa: E402
from core.bus import BusEventos, Evento  # noqa: E402
from core.config import Settings, cargar_settings, zona_local  # noqa: E402
from core.contracts import (  # noqa: E402
    BrocaReply,
    EstadoPresencia,
    Etiquetas,
    RegistroLlamadas,
    SalidaAvatar,
    Segmento,
    Utterance,
)
from lobes.riko_megumin_io import RikoMeguminIO  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
TOKEN = "123456789:TEST_token_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHAT = "42"
MP3_FALSO = b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-amadeus"


# ---------------------------------------------------------------------------
# Ayudantes
# ---------------------------------------------------------------------------


def _entorno_base(tmp_path: Path) -> dict[str, str]:
    """Mismo contrato que ``settings_prueba`` de conftest, con persona aislada en ``tmp_path``."""
    return {
        "RAMS_DATA_DIR": str(tmp_path / "data"), "RAMS_HUD_PORT": "0", "RAMS_CTL_PORT": "0",
        "RAMS_CTL_TOKEN_FILE": str(tmp_path / "ctl_token"), "VTS_ENABLED": "0", "RAMS_CAMERA": "0",
        "RAMS_TELEGRAM_POLL": "0", "RAMS_CONNECTOME_SEED": "7",
        "RAMS_PERSONA_DIR": str(tmp_path / "persona"),
    }


def _settings_offline(tmp_path: Path) -> Settings:
    return cargar_settings(entorno=_entorno_base(tmp_path), offline=True, base_dir=RAIZ)


def _settings_claves(tmp_path: Path, **extra: str) -> Settings:
    """Como ``settings_con_claves`` de conftest: offline=False y claves FALSAS."""
    entorno = _entorno_base(tmp_path)
    entorno.update({
        "GEMINI_API_KEY": "test-gemini-key-000000", "ELEVENLABS_API_KEY": "test-eleven-key-000000",
        "TELEGRAM_BOT_TOKEN": TOKEN, "CHAT_ID": CHAT, "BRAVE_API_KEY": "test-brave-key-000000",
    })
    entorno.update(extra)
    return cargar_settings(entorno=entorno, offline=False, base_dir=RAIZ)


def _ts_local(settings: Settings, hora: int, minuto: int = 0) -> float:
    return datetime(2026, 10, 16, hora, minuto, tzinfo=zona_local(settings)).timestamp()


class Reloj:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class MemoriaFalsa:
    """Implementa TODO ``RegistroLlamadas`` y anota cada llamada."""

    def __init__(self, *, ignoradas: int = 0, proactivos: int = 0, explosion: str | None = None) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.turnos: list[dict[str, Any]] = []
        self.marcadas: list[tuple[int, bool]] = []
        self.meta: dict[str, str] = {}
        if explosion is not None:
            self.meta["explosion_fecha"] = explosion
        self.ignoradas = ignoradas
        self.proactivos = proactivos

    async def registrar_llamada(self, *, origen: str, canal: str, nivel: int, motivo: str,
                                texto: str = "") -> int:
        self.llamadas.append({"origen": origen, "canal": canal, "nivel": nivel, "motivo": motivo,
                              "texto": texto})
        return len(self.llamadas)

    async def marcar_llamada(self, llamada_id: int, contestada: bool) -> None:
        self.marcadas.append((llamada_id, contestada))

    async def llamadas_ignoradas(self, horas: float = 24.0, gracia_min: float = 120.0) -> int:
        return self.ignoradas

    async def contar_proactivos_hoy(self) -> int:
        return self.proactivos

    async def registrar_turno(self, rol: str, texto: str, *, canal: str = "terminal",
                              etiquetas: Etiquetas | None = None) -> int:
        self.turnos.append({"rol": rol, "texto": texto, "canal": canal, "etiquetas": etiquetas})
        return len(self.turnos)

    async def meta_get(self, clave: str) -> str | None:
        return self.meta.get(clave)

    async def meta_set(self, clave: str, valor: str) -> None:
        self.meta[clave] = valor


class AvatarFalso:
    """Implementa ``SalidaAvatar``."""

    def __init__(self) -> None:
        self.en_linea = True
        self.etiquetas: list[Etiquetas] = []
        self.bocas: list[float] = []
        self.cierres = 0

    async def aplicar_etiquetas(self, etiquetas: Etiquetas) -> None:
        self.etiquetas.append(etiquetas)

    async def boca(self, valor: float) -> None:
        self.bocas.append(valor)

    async def cerrar_boca(self) -> None:
        self.cierres += 1


class ReproductorFalso(riko_mod._Reproductor):
    """Reproductor PCM en memoria (nunca toca el hardware de audio)."""

    nombre = "falso"
    latencia_s = 0.0

    def __init__(self) -> None:
        self.datos = bytearray()
        self.sr: int | None = None
        self.cerrado = False
        self.abortado = False

    async def abrir(self, sr: int) -> None:
        self.sr = sr

    async def escribir(self, pcm: bytes) -> None:
        self.datos += pcm

    async def cerrar(self) -> None:
        self.cerrado = True

    async def abortar(self) -> None:
        self.abortado = True


def _pcm_senoide(segundos: float = 0.3, sr: int = 24000, amplitud: int = 9000) -> bytes:
    muestras = array.array("h", (int(amplitud * math.sin(2 * math.pi * 220 * i / sr))
                                 for i in range(int(segundos * sr))))
    return muestras.tobytes()


class Transporte:
    """Manejador de ``httpx.MockTransport`` que simula Telegram y ElevenLabs y anota las peticiones."""

    def __init__(self) -> None:
        self.peticiones: list[httpx.Request] = []
        self.lotes_updates: list[Any] = []          # respuestas sucesivas de getUpdates
        self.al_agotar_updates: Callable[[], None] | None = None
        self.respuesta_getupdates: httpx.Response | None = None
        self.respuestas_stream: list[httpx.Response] = []
        self.respuesta_mp3: httpx.Response = httpx.Response(200, content=MP3_FALSO,
                                                           headers={"content-type": "audio/mpeg"})
        self.respuesta_voces = httpx.Response(200, json={"voice_id": "p7sm2zw5Us66lZr2brDW"})

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        self.peticiones.append(peticion)
        if peticion.url.host == "api.telegram.org":
            return self._telegram(peticion)
        if peticion.url.host == "api.elevenlabs.io":
            return self._eleven(peticion)
        return httpx.Response(404, json={"detail": "desconocido"})

    def _telegram(self, peticion: httpx.Request) -> httpx.Response:
        assert peticion.url.path.startswith(f"/bot{TOKEN}/")
        metodo = peticion.url.path.rsplit("/", 1)[-1]
        if metodo == "getUpdates":
            if self.respuesta_getupdates is not None:
                return self.respuesta_getupdates
            if self.lotes_updates:
                return httpx.Response(200, json={"ok": True, "result": self.lotes_updates.pop(0)})
            if self.al_agotar_updates is not None:
                self.al_agotar_updates()
            return httpx.Response(200, json={"ok": True, "result": []})
        if metodo == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "rams_amadeus_bot"}})
        if metodo in ("sendMessage", "sendVoice"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.peticiones)}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def _eleven(self, peticion: httpx.Request) -> httpx.Response:
        assert peticion.headers.get("xi-api-key") == "test-eleven-key-000000"
        if peticion.method == "GET" and peticion.url.path.startswith("/v1/voices/"):
            return self.respuesta_voces
        if peticion.url.path.endswith("/stream"):
            if self.respuestas_stream:
                return self.respuestas_stream.pop(0)
            return httpx.Response(200, content=_pcm_senoide())
        return self.respuesta_mp3

    # --- consultas ---
    def telegram(self, metodo: str) -> list[httpx.Request]:
        return [p for p in self.peticiones
                if p.url.host == "api.telegram.org" and p.url.path.endswith("/" + metodo)]

    def eleven(self, sufijo: str = "") -> list[httpx.Request]:
        return [p for p in self.peticiones
                if p.url.host == "api.elevenlabs.io" and p.url.path.endswith(sufijo)]


def _json(peticion: httpx.Request) -> dict[str, Any]:
    return json.loads(peticion.content.decode("utf-8"))


class Captura:
    """``imprimir`` capturado."""

    def __init__(self) -> None:
        self.lineas: list[tuple[str, Etiquetas | None]] = []

    def __call__(self, texto: str, etiquetas: Etiquetas | None = None) -> None:
        self.lineas.append((texto, etiquetas))

    @property
    def textos(self) -> list[str]:
        return [t for t, _ in self.lineas]


def _presencia(presente: bool) -> Callable[[], EstadoPresencia]:
    estado = EstadoPresencia(presente=presente, confianza=0.95 if presente else 0.85,
                             motivo="terminal_reciente" if presente else "inactividad")
    return lambda: estado


@pytest.fixture(autouse=True)
def _sin_esperas_ni_motores_reales(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin el mínimo de 1 s entre envíos y sin edge-tts real (nunca debe salir a la red)."""
    monkeypatch.setattr(riko_mod, "INTERVALO_MIN_ENVIO_S", 0.0)
    monkeypatch.setattr(riko_mod, "edge_tts", None)


def _crear(settings: Settings, transporte: Transporte | None = None, **kw: Any) -> tuple[RikoMeguminIO, Captura]:
    captura = Captura()
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(transporte or Transporte()))
    riko = RikoMeguminIO(settings, kw.pop("bus", None), cliente=cliente, imprimir=captura, **kw)
    return riko, captura


def _bus_con_eventos() -> tuple[BusEventos, list[Evento]]:
    bus = BusEventos()
    eventos: list[Evento] = []
    bus.suscribir("*", eventos.append)
    return bus, eventos


def _reply(texto: str, etiquetas: Etiquetas | None = None) -> BrocaReply:
    return BrocaReply.de_texto(texto, etiquetas)


def test_fakes_cumplen_los_protocolos() -> None:
    assert isinstance(MemoriaFalsa(), RegistroLlamadas)
    assert isinstance(AvatarFalso(), SalidaAvatar)


# ---------------------------------------------------------------------------
# 1. Presente + offline → terminal, sin Telegram
# ---------------------------------------------------------------------------


def test_presente_offline_llega_a_la_terminal(tmp_path: Path) -> None:
    transporte = Transporte()
    riko, captura = _crear(_settings_offline(tmp_path), transporte,
                           presencia=_presencia(True), avatar=AvatarFalso())

    async def escenario() -> None:
        diagnosticos = await riko.iniciar()
        assert {d.clave: d.estado.value for d in diagnosticos} == {"voz": "OFF", "telegram": "OFF"}
        assert riko.voz_disponible is False
        await riko.decir(_reply("Hola, Lab Mem 001. Sigo aquí."), origen="respuesta", canal="auto")
        await riko.decir(_reply("Llamada de prueba."), origen="iniciativa", prioridad=1,
                         canal="telegram", motivo="conversar")
        assert await riko.vaciar(2.0) is True
        await riko.cerrar()

    asyncio.run(escenario())
    assert "Hola, Lab Mem 001. Sigo aquí." in captura.textos
    assert "Llamada de prueba." in captura.textos
    assert transporte.peticiones == []


# ---------------------------------------------------------------------------
# 2. Telegram nivel 1: llamada perdida + botones dev:/no: + registro de la llamada
# ---------------------------------------------------------------------------


def test_telegram_nivel1_llamada_con_botones(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(settings, transporte, bus=bus, memoria=memoria,
                           presencia=_presencia(False), reloj=Reloj(_ts_local(settings, 12, 0)))
    texto = "Encontré un artículo sobre memoria episódica. Te lo resumo cuando vuelvas."

    async def escenario() -> None:
        await riko.decir(_reply(texto), origen="iniciativa", prioridad=1, canal="telegram",
                         motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(3.0) is True
        await riko.cerrar()

    asyncio.run(escenario())
    mensajes = transporte.telegram("sendMessage")
    assert mensajes, "no se envió ningún sendMessage"
    aviso = _json(mensajes[0])
    assert aviso["text"] == "Llamada perdida: Amadeus (12:00)"
    assert aviso["chat_id"] == 42 and aviso["parse_mode"] == "HTML"
    botones = aviso["reply_markup"]["inline_keyboard"][0]
    assert botones[0]["callback_data"] == "dev:1"
    assert botones[0]["text"] == "Devolver llamada"
    assert botones[1]["callback_data"] == "no:1"
    assert memoria.llamadas == [{"origen": "iniciativa", "canal": "telegram", "nivel": 1,
                                 "motivo": "conversar", "texto": texto}]
    # Nota de voz (ElevenLabs mp3_44100_64 sin streaming) con la transcripción como caption.
    notas = transporte.eleven()
    assert notas and notas[0].url.params["output_format"] == "mp3_44100_64"
    voces = transporte.telegram("sendVoice")
    assert len(voces) == 1
    assert texto.encode("utf-8") in voces[0].content
    # Turno registrado (origen != respuesta) por el canal efectivo.
    assert memoria.turnos and memoria.turnos[0]["rol"] == "amadeus"
    assert memoria.turnos[0]["canal"] == "telegram"
    # Terminal con el prefijo de Telegram y evento para el HUD.
    assert captura.textos == [f"[→ Telegram] {texto}"]
    enviados = [e.datos for e in eventos if e.nombre == "telegram.enviado"]
    assert enviados == [{"tipo": "llamada", "nivel": 1, "texto": texto[:200]}]


def test_telegram_nivel1_sin_audio_envia_transcripcion(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    transporte.respuesta_mp3 = httpx.Response(500, json={"detail": {"code": "internal_error"}})
    riko, _ = _crear(settings, transporte, memoria=MemoriaFalsa(),
                     reloj=Reloj(_ts_local(settings, 15, 30)))

    async def escenario() -> None:
        await riko.decir(_reply("El build terminó sin errores."), origen="nexus", prioridad=1,
                         canal="telegram", motivo="trabajo_terminado")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    textos = [_json(p)["text"] for p in transporte.telegram("sendMessage")]
    assert textos == ["Llamada perdida: Amadeus (15:30)", "El build terminó sin errores."]
    assert transporte.telegram("sendVoice") == []


# ---------------------------------------------------------------------------
# 3. Nivel 2 (Megumin) con <blockquote> y escape HTML
# ---------------------------------------------------------------------------


def test_nivel2_megumin_blockquote_y_escape(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    memoria = MemoriaFalsa(ignoradas=2)
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(settings, transporte, bus=bus, memoria=memoria,
                     reloj=Reloj(_ts_local(settings, 10, 0)))
    texto = "Revisa esto: <script>alert(1)</script> & vuelve."

    async def escenario() -> None:
        await riko.decir(_reply(texto), origen="iniciativa", prioridad=1, canal="telegram",
                         motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    mensajes = transporte.telegram("sendMessage")
    assert len(mensajes) == 1
    cuerpo = _json(mensajes[0])
    html_texto = cuerpo["text"]
    assert "<blockquote>Amadeus: " in html_texto and "</blockquote>" in html_texto
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; vuelve." in html_texto
    assert "<script>" not in html_texto
    assert "Megumin" in html_texto and "Lab Mem 001" in html_texto      # {usuario} rellenado
    assert html_texto.count("\n") == 2                                   # apertura, cita, cierre
    assert cuerpo["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "dev:1"
    assert transporte.telegram("sendVoice") == []                        # Megumin no tiene voz
    assert memoria.llamadas[0]["nivel"] == 2
    assert [e.datos["tipo"] for e in eventos if e.nombre == "telegram.enviado"] == ["megumin"]


def test_mensajero_megumin_entrega_el_nivel1(tmp_path: Path) -> None:
    """RAMS_MENSAJERO=megumin: la primera llamada ya la entrega Megumin (CONTRATO de cierre)."""
    settings = _settings_claves(tmp_path, RAMS_MENSAJERO="megumin")
    assert settings.mensajero == "megumin"
    transporte = Transporte()
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(settings, transporte, bus=bus, memoria=memoria,
                     reloj=Reloj(_ts_local(settings, 12, 0)))
    texto = "Terminé de consolidar la memoria de hoy."

    async def escenario() -> None:
        await riko.decir(_reply(texto), origen="iniciativa", prioridad=1, canal="telegram",
                         motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    mensajes = transporte.telegram("sendMessage")
    assert len(mensajes) == 1
    cuerpo = _json(mensajes[0])
    assert "Llamada perdida" not in cuerpo["text"] and "Megumin" in cuerpo["text"]
    assert f"<blockquote>Amadeus: {texto}</blockquote>" in cuerpo["text"]
    assert cuerpo["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "dev:1"
    assert transporte.telegram("sendVoice") == []                        # Megumin no tiene voz
    assert memoria.llamadas[0]["nivel"] == 1
    assert [e.datos["tipo"] for e in eventos if e.nombre == "telegram.enviado"] == ["megumin"]


def test_mensajero_amadeus_nunca_usa_a_megumin(tmp_path: Path) -> None:
    """RAMS_MENSAJERO=amadeus: la insistencia y los críticos son llamadas de Amadeus, sin Megumin."""
    settings = _settings_claves(tmp_path, RAMS_MENSAJERO="Amadeus")
    assert settings.mensajero == "amadeus"
    transporte = Transporte()
    memoria = MemoriaFalsa(ignoradas=2)
    riko, _ = _crear(settings, transporte, memoria=memoria, reloj=Reloj(_ts_local(settings, 12, 0)))

    async def escenario() -> None:
        await riko.decir(_reply("Sigo aquí, por si acaso."), origen="iniciativa", prioridad=1,
                         canal="telegram", motivo="conversar", tipo_iniciativa="conversar")
        await riko.decir(_reply("CPU a 96 °C durante 3 min."), origen="alerta", prioridad=2,
                         canal="telegram", motivo="temperatura_cpu")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    textos = [_json(p)["text"] for p in transporte.telegram("sendMessage")]
    # La alerta (prioridad 2) sale primero de la cola; después, la insistencia (nivel 2).
    assert "Aviso crítico: Amadeus (12:00)" in textos and "Llamada perdida: Amadeus (12:00)" in textos
    assert not any("Megumin" in x or "<blockquote>" in x for x in textos)
    assert sorted(ll["nivel"] for ll in memoria.llamadas) == [2, 3]
    assert memoria.meta.get("explosion_fecha") is None                    # sin Explosión


def test_mensajero_invalido_usa_hibrido(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rams.config"):
        settings = _settings_claves(tmp_path, RAMS_MENSAJERO="okabe")
    assert settings.mensajero == "hibrido"
    assert any("RAMS_MENSAJERO" in r.getMessage() and "okabe" not in r.getMessage() for r in caplog.records)
    assert _settings_claves(tmp_path, RAMS_MENSAJERO="híbrido").mensajero == "hibrido"


def test_megumin_usa_frases_json_de_persona(tmp_path: Path) -> None:
    persona = tmp_path / "persona"
    persona.mkdir()
    frases = {"version": 1, "categorias": {}, "megumin": {
        "nivel2": [{"apertura": "¡Contempla, {usuario}, el mensaje carmesí!", "cierre": "Fin {x}."}]}}
    (persona / "frases.json").write_text(json.dumps(frases, ensure_ascii=False), encoding="utf-8")
    settings = _settings_claves(tmp_path, RAMS_USER_NAME="Juan")
    transporte = Transporte()
    riko, _ = _crear(settings, transporte, memoria=MemoriaFalsa(ignoradas=3),
                     reloj=Reloj(_ts_local(settings, 9, 0)))

    async def escenario() -> None:
        await riko.decir(_reply("Hola."), origen="agenda", prioridad=1, canal="telegram",
                         motivo="recordatorio")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    texto = _json(transporte.telegram("sendMessage")[0])["text"]
    assert texto.startswith("¡Contempla, Juan, el mensaje carmesí!")
    assert texto.endswith("Fin .")          # placeholder desconocido borrado, nunca "{x}"


# ---------------------------------------------------------------------------
# 4. Cuota diaria de Telegram
# ---------------------------------------------------------------------------


def test_cuota_telegram_llena(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    memoria = MemoriaFalsa(proactivos=3)
    riko, captura = _crear(settings, transporte, memoria=memoria,
                           reloj=Reloj(_ts_local(settings, 14, 0)))

    async def escenario() -> None:
        await riko.actualizar_cuota()
        permitido, motivo = riko.puede_proactivo(canal="telegram", tipo="conversar")
        assert permitido is False and "cuota" in motivo
        # La cuota diaria no aplica en presencia.
        assert riko.puede_proactivo(canal="terminal", tipo="conversar")[0] is True

        await riko.decir(_reply("Iniciativa que no debe salir."), origen="iniciativa", prioridad=1,
                         canal="telegram", motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(2.0)
        assert transporte.telegram("sendMessage") == []
        assert memoria.llamadas == []
        assert captura.textos == []

        await riko.decir(_reply("CPU a 96 °C durante 3 min."), origen="alerta", prioridad=2,
                         canal="telegram", motivo="temperatura_cpu")
        assert await riko.vaciar(3.0)
        assert len(transporte.telegram("sendMessage")) == 1
        assert memoria.llamadas[0]["origen"] == "alerta" and memoria.llamadas[0]["nivel"] == 3

        await riko.decir(_reply("Iniciativa local."), origen="iniciativa", prioridad=1,
                         canal="terminal", motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(2.0)

    asyncio.run(escenario())
    assert "Iniciativa local." in captura.textos
    assert "Iniciativa que no debe salir." not in " ".join(captura.textos)


# ---------------------------------------------------------------------------
# 5. Horario silencioso (solo Telegram) e interrupciones por hora (presencia)
# ---------------------------------------------------------------------------


def test_horario_silencioso_solo_afecta_a_telegram(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    reloj = Reloj(_ts_local(settings, 23, 30))
    riko, captura = _crear(settings, transporte, memoria=MemoriaFalsa(), reloj=reloj)

    async def escenario() -> None:
        permitido, motivo = riko.puede_proactivo(canal="telegram", tipo="conversar")
        assert permitido is False and "silencioso" in motivo
        assert riko.puede_proactivo(canal="telegram", critico=True)[0] is True
        assert riko.puede_proactivo(canal="voz", tipo="conversar")[0] is True

        await riko.decir(_reply("Iniciativa nocturna."), origen="iniciativa", prioridad=1,
                         canal="telegram", motivo="conversar", tipo_iniciativa="conversar")
        await riko.decir(_reply("Iniciativa presencial."), origen="iniciativa", prioridad=1,
                         canal="voz", motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(2.0)
        assert transporte.telegram("sendMessage") == []
        assert captura.textos == ["Iniciativa presencial."]

        # La agenda sí pasa, pero sin notificación sonora.
        await riko.decir(_reply("Recordatorio: llamar a mamá."), origen="agenda", prioridad=1,
                         canal="telegram", motivo="recordatorio")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    mensajes = transporte.telegram("sendMessage")
    assert mensajes and all(_json(m).get("disable_notification") is True for m in mensajes)
    voces = transporte.telegram("sendVoice")
    assert voces and b'name="disable_notification"' in voces[0].content


def test_quinta_interrupcion_presencial_descartada_salvo_descanso(tmp_path: Path) -> None:
    settings = _settings_offline(tmp_path)
    reloj = Reloj(_ts_local(settings, 2, 0))
    riko, captura = _crear(settings, reloj=reloj)

    async def iniciativa(texto: str, tipo: str = "conversar") -> None:
        await riko.decir(_reply(texto), origen="iniciativa", prioridad=1, canal="terminal",
                         motivo=tipo, tipo_iniciativa=tipo)
        assert await riko.vaciar(2.0)

    async def escenario() -> None:
        for i in range(4):
            await iniciativa(f"Interrupción {i + 1}.")
            reloj.t += 5 * 60
            await iniciativa(f"Demasiado pronto {i + 1}.")   # < 10 min desde la anterior
            reloj.t += 6 * 60
        assert riko.puede_proactivo(canal="terminal", tipo="conversar") == (
            False, "límite de 4 interrupciones por hora")
        await iniciativa("Quinta interrupción.")
        await iniciativa("Deberías dormir: son las 2 a. m.", tipo="descanso")

    asyncio.run(escenario())
    assert captura.textos == ["Interrupción 1.", "Interrupción 2.", "Interrupción 3.",
                              "Interrupción 4.", "Deberías dormir: son las 2 a. m."]


def test_pausa_de_proactividad(tmp_path: Path) -> None:
    settings = _settings_offline(tmp_path)
    reloj = Reloj(_ts_local(settings, 12, 0))
    riko, _ = _crear(settings, reloj=reloj)
    riko.pausar_proactividad(30)
    permitido, motivo = riko.puede_proactivo(canal="terminal", tipo="descanso")
    assert permitido is False and "pausa" in motivo
    assert riko.puede_proactivo(canal="terminal", critico=True)[0] is True
    reloj.t += 31 * 60
    assert riko.puede_proactivo(canal="terminal", tipo="conversar")[0] is True
    riko.pausar_proactividad(10)
    riko.pausar_proactividad(0)
    assert riko.puede_proactivo(canal="terminal")[0] is True


# ---------------------------------------------------------------------------
# 6. Filtro de remitente de la entrada de Telegram
# ---------------------------------------------------------------------------


def _mensaje(uid: int, *, chat_id: int, chat_type: str = "private", from_id: int,
             texto: str) -> dict[str, Any]:
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat_id, "type": chat_type},
                                          "from": {"id": from_id, "is_bot": False}, "text": texto}}


def _boton(uid: int, *, from_id: int, chat_id: int, data: str) -> dict[str, Any]:
    return {"update_id": uid, "callback_query": {
        "id": f"cb{uid}", "from": {"id": from_id}, "data": data,
        "message": {"message_id": 7, "chat": {"id": chat_id, "type": "private"}}}}


def test_bucle_telegram_filtra_remitentes(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(settings, transporte, bus=bus, memoria=memoria)
    recibidos: list[str] = []

    async def manejador(texto: str) -> None:
        recibidos.append(texto)

    async def escenario() -> None:
        parar = asyncio.Event()
        transporte.lotes_updates = [
            [_mensaje(100, chat_id=42, from_id=42, texto="mensaje viejo del backlog")],
            [
                _mensaje(101, chat_id=42, from_id=42, texto="hola amadeus"),
                _mensaje(102, chat_id=999, from_id=999, texto="soy otro chat"),
                _mensaje(103, chat_id=42, chat_type="group", from_id=42, texto="desde un grupo"),
                _mensaje(104, chat_id=42, from_id=7, texto="otro remitente"),
                _boton(105, from_id=42, chat_id=42, data="dev:5"),
                _boton(106, from_id=7, chat_id=42, data="no:5"),
                _boton(107, from_id=42, chat_id=42, data="no:6"),
            ],
        ]
        transporte.al_agotar_updates = parar.set
        await asyncio.wait_for(riko.bucle_telegram_entrada(parar, manejador), 5.0)

    asyncio.run(escenario())
    assert recibidos == ["hola amadeus", "/devolver_llamada"]
    assert memoria.marcadas == [(5, True), (6, False)]
    respuestas = [_json(p) for p in transporte.telegram("answerCallbackQuery")]
    assert respuestas == [{"callback_query_id": "cb105", "text": "Devolviendo la llamada…"},
                          {"callback_query_id": "cb107", "text": "Anotado."}]
    offsets = [_json(p)["offset"] for p in transporte.telegram("getUpdates")]
    assert offsets[:3] == [-1, 101, 108]
    assert [e.datos for e in eventos if e.nombre == "telegram.respuesta_llamada"] == [
        {"llamada_id": 5, "contestada": True}, {"llamada_id": 6, "contestada": False}]


# ---------------------------------------------------------------------------
# 7. 409 Conflict termina el bucle
# ---------------------------------------------------------------------------


def test_bucle_telegram_409_termina(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    transporte = Transporte()
    transporte.respuesta_getupdates = httpx.Response(409, json={
        "ok": False, "error_code": 409,
        "description": "Conflict: terminated by other getUpdates request"})
    riko, _ = _crear(_settings_claves(tmp_path), transporte)

    async def manejador(texto: str) -> None:
        raise AssertionError("no debe llamarse")

    caplog.set_level(logging.ERROR, logger="rams.salida")
    asyncio.run(asyncio.wait_for(riko.bucle_telegram_entrada(asyncio.Event(), manejador), 3.0))
    assert len(transporte.telegram("getUpdates")) == 1
    assert any("Otro proceso usa getUpdates con este bot" in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 8. Drenaje con parar activo y vaciar sin bucle
# ---------------------------------------------------------------------------


def test_bucle_drena_al_parar(tmp_path: Path) -> None:
    riko, captura = _crear(_settings_offline(tmp_path))

    async def escenario() -> None:
        parar = asyncio.Event()
        parar.set()
        await riko.encolar(Utterance((Segmento(Etiquetas(), "Hasta luego. Fue una sesión productiva."),),
                                     origen="sistema", canal="terminal"))
        await asyncio.wait_for(riko.bucle(parar), 2.0)

    asyncio.run(escenario())
    assert captura.textos == ["Hasta luego. Fue una sesión productiva."]


def test_bucle_sin_drenar_retorna_de_inmediato(tmp_path: Path) -> None:
    riko, captura = _crear(_settings_offline(tmp_path))

    async def escenario() -> None:
        parar = asyncio.Event()
        parar.set()
        await riko.decir(_reply("Pendiente."), origen="sistema", canal="terminal")
        await asyncio.wait_for(riko.bucle(parar, drenar_al_parar=False), 1.0)
        assert captura.textos == []
        assert await riko.vaciar(1.0) is True

    asyncio.run(escenario())
    assert captura.textos == ["Pendiente."]


def test_vaciar_sin_bucle_procesa_la_cola(tmp_path: Path) -> None:
    riko, captura = _crear(_settings_offline(tmp_path))

    async def escenario() -> None:
        assert await riko.vaciar(0.5) is True           # cola vacía
        await riko.encolar(Utterance((Segmento(Etiquetas(), "Despedida."),), origen="sistema",
                                     canal="terminal"))
        assert await riko.vaciar(1.0) is True

    asyncio.run(escenario())
    assert captura.textos == ["Despedida."]


def test_bucle_activo_y_prioridad(tmp_path: Path) -> None:
    riko, captura = _crear(_settings_offline(tmp_path))

    async def escenario() -> None:
        parar = asyncio.Event()
        await riko.decir(_reply("Normal."), origen="sistema", canal="terminal")
        await riko.decir(_reply("Crítico."), origen="alerta", prioridad=2, canal="terminal",
                         motivo="disco")
        tarea = asyncio.create_task(riko.bucle(parar))
        assert await riko.vaciar(2.0) is True
        await riko.decir(_reply("Después."), origen="sistema", canal="terminal")
        assert await riko.vaciar(2.0) is True
        parar.set()
        await asyncio.wait_for(tarea, 2.0)

    asyncio.run(escenario())
    assert captura.textos == ["Crítico.", "Normal.", "Después."]


# ---------------------------------------------------------------------------
# 9. Voz/terminal: no registra llamada pero sí turno
# ---------------------------------------------------------------------------


def test_voz_o_terminal_no_registra_llamada_pero_si_turno(tmp_path: Path) -> None:
    memoria = MemoriaFalsa()
    riko, captura = _crear(_settings_offline(tmp_path), memoria=memoria, presencia=_presencia(True))

    async def escenario() -> None:
        await riko.decir(_reply("Perdón la interrupción: llevas 3 h sin pausa."), origen="iniciativa",
                         prioridad=1, canal="voz", motivo="descanso", tipo_iniciativa="descanso")
        await riko.decir(_reply("Respuesta normal."), origen="respuesta", canal="terminal")
        assert await riko.vaciar(2.0)

    asyncio.run(escenario())
    assert memoria.llamadas == []
    assert [(t["rol"], t["texto"], t["canal"]) for t in memoria.turnos] == [
        ("amadeus", "Perdón la interrupción: llevas 3 h sin pausa.", "terminal")]
    assert len(captura.textos) == 2


# ---------------------------------------------------------------------------
# 10. Codificación: JSON en métodos sin archivo, multipart en sendVoice
# ---------------------------------------------------------------------------


def test_codificacion_json_y_multipart(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    riko, _ = _crear(settings, transporte, memoria=MemoriaFalsa(),
                     reloj=Reloj(_ts_local(settings, 11, 0)))

    async def manejador(texto: str) -> None:
        return None

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["telegram"].estado.value == "OK"
        assert diagnosticos["telegram"].detalle == "@rams_amadeus_bot"
        await riko.decir(_reply("Nota para cuando vuelvas."), origen="iniciativa", prioridad=1,
                         canal="telegram", motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(3.0)
        assert await riko.enviar_telegram_texto("Respuesta <b>conversacional</b>.") is True
        parar = asyncio.Event()
        transporte.al_agotar_updates = parar.set
        await asyncio.wait_for(riko.bucle_telegram_entrada(parar, manejador), 3.0)
        await riko.cerrar()

    asyncio.run(escenario())
    for metodo in ("getMe", "sendMessage", "getUpdates", "sendChatAction"):
        peticiones = transporte.telegram(metodo)
        assert peticiones, metodo
        for peticion in peticiones:
            assert peticion.headers["content-type"] == "application/json", metodo
    aviso = _json(transporte.telegram("sendMessage")[0])
    assert isinstance(aviso["reply_markup"], dict)                    # objeto JSON real
    assert isinstance(_json(transporte.telegram("getUpdates")[0])["allowed_updates"], list)
    voz = transporte.telegram("sendVoice")[0]
    assert voz.headers["content-type"].startswith("multipart/form-data")
    assert b'name="voice"; filename="amadeus.mp3"' in voz.content
    assert b"audio/mpeg" in voz.content and MP3_FALSO in voz.content
    respuesta = _json(transporte.telegram("sendMessage")[-1])
    assert respuesta["text"] == "Respuesta &lt;b&gt;conversacional&lt;/b&gt;."
    assert "reply_markup" not in respuesta


# ---------------------------------------------------------------------------
# 11. Explosión persistente y refractario de críticos
# ---------------------------------------------------------------------------


def test_explosion_ya_usada_hoy_usa_nivel3_agotada(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    ts = _ts_local(settings, 16, 0)
    hoy = datetime.fromtimestamp(ts, zona_local(settings)).date().isoformat()
    transporte = Transporte()
    memoria = MemoriaFalsa(explosion=hoy)
    riko, _ = _crear(settings, transporte, memoria=memoria, reloj=Reloj(ts))

    async def escenario() -> None:
        await riko.decir(_reply("Disco al 97 %."), origen="alerta", prioridad=2, canal="telegram",
                         motivo="disco")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    texto = _json(transporte.telegram("sendMessage")[0])["text"]
    assert texto.startswith("Ya usé mi Explosión de hoy")
    assert "<blockquote>Amadeus: Disco al 97 %.</blockquote>" in texto
    assert transporte.telegram("sendVoice") == []
    assert memoria.meta["explosion_fecha"] == hoy


def test_explosion_del_dia_y_refractario(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    reloj = Reloj(_ts_local(settings, 16, 0))
    transporte = Transporte()
    memoria = MemoriaFalsa()
    riko, captura = _crear(settings, transporte, memoria=memoria, reloj=reloj)

    async def alerta(texto: str) -> None:
        await riko.decir(_reply(texto), origen="alerta", prioridad=2, canal="telegram",
                         motivo="temperatura_cpu")
        assert await riko.vaciar(3.0)

    async def escenario() -> None:
        await alerta("CPU a 95 °C.")                 # primera: Explosión + nota de voz
        reloj.t += 60
        await alerta("CPU a 96 °C.")                 # < 10 min: refractario
        reloj.t += 10 * 60
        await alerta("CPU a 97 °C.")                 # ≥ 10 min: pasa (Explosión agotada)
        reloj.t += 20 * 60
        await alerta("CPU a 98 °C.")                 # < 30 min desde la anterior: refractario
        reloj.t += 11 * 60
        await alerta("CPU a 99 °C.")                 # ≥ 30 min: pasa

    asyncio.run(escenario())
    textos = [_json(p)["text"] for p in transporte.telegram("sendMessage")]
    assert len(textos) == 3
    assert textos[0].startswith("¡EXPLOSIÓN!") and "CPU a 95 °C." in textos[0]
    assert textos[1].startswith("Ya usé mi Explosión de hoy") and "CPU a 97 °C." in textos[1]
    assert "CPU a 99 °C." in textos[2]
    assert len(transporte.telegram("sendVoice")) == 1
    assert memoria.meta["explosion_fecha"] == "2026-10-16"
    assert len(memoria.llamadas) == 3
    # Las alertas bloqueadas por el refractario igual llegaron a la terminal.
    assert "CPU a 96 °C." in captura.textos and "CPU a 98 °C." in captura.textos


def test_explosion_sin_memoria_se_guarda_en_ram(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    riko, _ = _crear(settings, transporte, reloj=Reloj(_ts_local(settings, 16, 0)))

    async def escenario() -> None:
        for motivo in ("disco", "ram"):
            await riko.decir(_reply(f"Alerta de {motivo}."), origen="alerta", prioridad=2,
                             canal="telegram", motivo=motivo)
            assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    textos = [_json(p)["text"] for p in transporte.telegram("sendMessage")]
    assert textos[0].startswith("¡EXPLOSIÓN!")
    assert textos[1].startswith("Ya usé mi Explosión de hoy")
    callbacks = [_json(p)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                 for p in transporte.telegram("sendMessage")]
    assert callbacks == ["dev:-1", "dev:-2"]     # ids de RAM negativos: nunca chocan con SQLite


# ---------------------------------------------------------------------------
# 12. voz_disponible
# ---------------------------------------------------------------------------


def _audio_falso(monkeypatch: pytest.MonkeyPatch) -> ReproductorFalso:
    """Sustituye la detección y la fábrica de reproductores por uno en memoria."""
    reproductor = ReproductorFalso()
    monkeypatch.setattr(riko_mod, "_detectar_reproductor_pcm", lambda excluir=frozenset(): "falso")
    monkeypatch.setattr(riko_mod, "_fabricar_reproductor", lambda nombre, obtener: reproductor)
    monkeypatch.setattr(riko_mod, "_argv_reproductor_mp3", lambda: None)
    monkeypatch.setattr(riko_mod.shutil, "which", lambda *a, **k: None)
    return reproductor


def test_voz_disponible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _audio_falso(monkeypatch)
    riko, _ = _crear(_settings_claves(tmp_path))

    async def escenario() -> None:
        assert riko.voz_disponible is False                 # antes de iniciar()
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].estado.value == "OK"
        assert diagnosticos["voz"].detalle == "ElevenLabs"
        assert riko.voz_disponible is True
        riko.set_silencio(True)
        assert riko.silenciado is True and riko.voz_disponible is False
        riko.set_silencio(False)
        assert riko.voz_disponible is True
        await riko.cerrar()
        assert riko.voz_disponible is False

    asyncio.run(escenario())


def test_voz_disponible_parcheable_sin_perder_texto(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    memoria = MemoriaFalsa()
    riko, captura = _crear(_settings_offline(tmp_path), memoria=memoria)
    monkeypatch.setattr(riko, "voz_disponible", True)        # como el test 8 de main (§10.21)
    assert riko.voz_disponible is True

    async def escenario() -> None:
        await riko.decir(_reply("Sin motor real de voz."), origen="sistema", canal="voz")
        assert await riko.vaciar(2.0)

    asyncio.run(escenario())
    assert captura.textos == ["Sin motor real de voz."]
    assert memoria.turnos[0]["canal"] == "terminal"           # no sonó: quedó en la terminal


def test_voz_sin_reproductor_degrada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(riko_mod, "_detectar_reproductor_pcm", lambda excluir=frozenset(): None)
    monkeypatch.setattr(riko_mod, "_argv_reproductor_mp3", lambda: None)
    riko, _ = _crear(_settings_claves(tmp_path))

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].estado.value == "DEGRADADO"
        assert diagnosticos["voz"].detalle == "sin reproductor de audio"
        assert riko.voz_disponible is False

    asyncio.run(escenario())


# ---------------------------------------------------------------------------
# Voz: ElevenLabs PCM en streaming con lip-sync (reproductor y avatar falsos)
# ---------------------------------------------------------------------------


def test_voz_elevenlabs_streaming_con_boca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_falso(monkeypatch)
    transporte = Transporte()
    avatar = AvatarFalso()
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_claves(tmp_path), transporte, bus=bus, avatar=avatar,
                           memoria=memoria)
    reply = BrocaReply(segmentos=(
        Segmento(Etiquetas("seria", "brazos_cruzados", "analitico"), "No es que me preocupe."),
        Segmento(Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"), "Solo descansa."),
    ))

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(reply, origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)
        assert riko.hablando is False

    asyncio.run(escenario())
    assert captura.textos == ["No es que me preocupe. Solo descansa."]
    assert [e.exp for e in avatar.etiquetas] == ["seria", "sonrojo_tsundere"]
    assert avatar.bocas and max(avatar.bocas) > 0.2
    assert avatar.cierres >= 2
    flujos = transporte.eleven("/stream")
    assert len(flujos) == 2
    primero = _json(flujos[0])
    assert flujos[0].url.params["output_format"] == "pcm_24000"
    assert primero["text"] == "[thoughtful] No es que me preocupe."
    assert primero["model_id"] == "eleven_v4_turbo"
    assert primero["voice_settings"]["stability"] in (0.0, 0.5, 1.0)
    assert "language_code" not in primero
    assert _json(flujos[1])["text"] == "[nervous] Solo descansa."
    assert reproductor.sr == 24000
    assert len(reproductor.datos) == 2 * len(_pcm_senoide())
    nombres = [e.nombre for e in eventos]
    assert nombres.count("voz.inicio") == 1 and nombres.count("voz.fin") == 1
    fin = next(e.datos for e in eventos if e.nombre == "voz.fin")
    assert fin["motor"] == "elevenlabs" and fin["duracion_s"] > 0.4
    assert any(e.nombre == "voz.nivel_boca" and e.datos["valor"] > 0 for e in eventos)
    assert [e.datos["canal"] for e in eventos if e.nombre == "salida.subtitulo"] == ["voz", "voz"]
    assert memoria.turnos[0]["canal"] == "voz"


def test_voz_recorta_a_dos_oraciones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _audio_falso(monkeypatch)
    transporte = Transporte()
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_claves(tmp_path), transporte, bus=bus)
    texto = "Primera oración. Segunda oración. Tercera oración con la cifra 42."

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_reply(texto), origen="respuesta", canal="voz")
        assert await riko.vaciar(10.0)

    asyncio.run(escenario())
    assert captura.textos == [texto]                         # la terminal lleva el texto completo
    hablado = _json(transporte.eleven("/stream")[0])["text"]
    assert hablado.endswith("Primera oración. Segunda oración.")
    # El HUD también recibe el texto completo, no solo la parte hablada.
    assert [e.datos["texto"] for e in eventos if e.nombre == "salida.subtitulo"] == [texto]


def test_voz_subtitula_los_segmentos_no_hablados(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _audio_falso(monkeypatch)
    transporte = Transporte()
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_claves(tmp_path), transporte, bus=bus)
    reply = BrocaReply(segmentos=(
        Segmento(Etiquetas("seria", "brazos_cruzados", "analitico"), "La CPU está a 71 °C."),
        Segmento(Etiquetas(), "Nada grave."),
        Segmento(Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"),
                 "Te aviso si sube de 85 °C."),
    ))

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(reply, origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)

    asyncio.run(escenario())
    assert len(transporte.eleven("/stream")) == 2             # en voz caben 2 oraciones
    assert captura.textos == ["La CPU está a 71 °C. Nada grave. Te aviso si sube de 85 °C."]
    assert [e.datos["texto"] for e in eventos if e.nombre == "salida.subtitulo"] == [
        "La CPU está a 71 °C.", "Nada grave.", "Te aviso si sube de 85 °C."]


def test_voz_que_no_suena_igual_subtitula_todo(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_offline(tmp_path), bus=bus)
    monkeypatch.setattr(riko, "voz_disponible", True)        # sin motor real: no suena
    texto = "Primera oración. Segunda oración. Tercera oración."

    async def escenario() -> None:
        await riko.decir(_reply(texto), origen="sistema", canal="voz")
        assert await riko.vaciar(2.0)

    asyncio.run(escenario())
    assert captura.textos == [texto]
    assert [e.datos["texto"] for e in eventos if e.nombre == "salida.subtitulo"] == [texto]


def test_eleven_401_desactiva_la_sesion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _audio_falso(monkeypatch)
    transporte = Transporte()
    transporte.respuestas_stream = [httpx.Response(401, json={"detail": {"code": "invalid_api_key"}})]
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_claves(tmp_path), transporte, bus=bus)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_reply("Prueba de voz."), origen="sistema", canal="voz")
        assert await riko.vaciar(5.0)

    asyncio.run(escenario())
    assert captura.textos == ["Prueba de voz."]              # el texto nunca se pierde
    assert len(transporte.eleven("/stream")) == 1             # 401 no se reintenta
    assert riko._eleven_activo() is False
    assert riko.voz_disponible is False                       # sin edge-tts no queda motor
    modulos = [e.datos for e in eventos if e.nombre == "sistema.modulo" and e.datos["clave"] == "voz"]
    assert modulos[-1]["estado"] == "OFF"


def test_eleven_400_reintenta_sin_speed_ni_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_falso(monkeypatch)
    transporte = Transporte()
    transporte.respuestas_stream = [httpx.Response(400, json={"detail": {"code": "invalid_parameters"}})]
    riko, _ = _crear(_settings_claves(tmp_path), transporte)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_reply("Hola."), origen="sistema", canal="voz")
        assert await riko.vaciar(5.0)

    asyncio.run(escenario())
    primero, segundo = (_json(p) for p in transporte.eleven("/stream"))
    assert primero["text"].startswith("[thoughtful]") and "speed" in primero["voice_settings"]
    assert segundo["text"] == "Hola." and "speed" not in segundo["voice_settings"]
    assert reproductor.datos                                  # el reintento sí sonó


def test_nivel_trama_numpy_y_respaldo(monkeypatch: pytest.MonkeyPatch) -> None:
    trama = _pcm_senoide(0.02)
    silencio = bytes(len(trama))
    con_numpy = riko_mod._nivel_trama(trama)
    monkeypatch.setattr(riko_mod, "np", None)
    sin_numpy = riko_mod._nivel_trama(trama)
    assert sin_numpy == pytest.approx(con_numpy, abs=1e-4)
    assert 0.0 < sin_numpy <= 1.0
    assert riko_mod._nivel_trama(silencio) == 0.0
    assert riko_mod._nivel_trama(b"") == 0.0


def test_auto_ausente_va_por_telegram(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    riko, _ = _crear(settings, transporte, memoria=MemoriaFalsa(), presencia=_presencia(False),
                     reloj=Reloj(_ts_local(settings, 13, 0)))

    async def escenario() -> None:
        await riko.decir(_reply("El build terminó."), origen="nexus", prioridad=1, canal="auto",
                         motivo="trabajo_terminado")
        await riko.decir(_reply("Respuesta local."), origen="respuesta", canal="auto")
        assert await riko.vaciar(3.0)

    asyncio.run(escenario())
    assert len(transporte.telegram("sendMessage")) == 1


# ---------------------------------------------------------------------------
# Cuota: envíos fallidos, Nexus, críticos y arranque acotado
# ---------------------------------------------------------------------------


class MemoriaContadora(MemoriaFalsa):
    """Como ``MemoryEngine``: ``contar_proactivos_hoy`` cuenta solo iniciativas por Telegram."""

    async def contar_proactivos_hoy(self) -> int:
        return sum(1 for ll in self.llamadas if ll["origen"] == "iniciativa" and ll["canal"] == "telegram")


class TransporteConCaida(Transporte):
    """Telegram sin red (``ConnectError``) en los envíos mientras ``caido`` es ``True``."""

    def __init__(self) -> None:
        super().__init__()
        self.caido = False
        self.fallidas = 0

    def __call__(self, peticion: httpx.Request) -> httpx.Response:
        metodo = peticion.url.path.rsplit("/", 1)[-1]
        if (self.caido and peticion.url.host == "api.telegram.org"
                and metodo in ("sendMessage", "sendVoice", "sendChatAction")):
            self.fallidas += 1          # no se anota en ``peticiones``: nunca llegó a Telegram
            raise httpx.ConnectError("sin red", request=peticion)
        return super().__call__(peticion)


def test_envios_fallidos_no_gastan_la_cuota(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    assert settings.max_proactivos_dia == 3
    transporte = TransporteConCaida()
    memoria = MemoriaContadora()
    reloj = Reloj(_ts_local(settings, 12, 0))
    riko, captura = _crear(settings, transporte, memoria=memoria, reloj=reloj)

    async def iniciativa(texto: str) -> None:
        await riko.decir(_reply(texto), origen="iniciativa", prioridad=1, canal="telegram",
                         motivo="conversar", tipo_iniciativa="conversar")
        assert await riko.vaciar(3.0)

    async def escenario() -> None:
        transporte.caido = True
        for i in range(3):
            await iniciativa(f"Intento {i + 1}.")
        assert transporte.fallidas >= 3 and transporte.telegram("sendMessage") == []
        assert await memoria.contar_proactivos_hoy() == 3       # las filas existen…
        assert riko.puede_proactivo(canal="telegram", tipo="conversar") == (True, "permitido")
        transporte.caido = False
        await iniciativa("Ya hay red.")
        assert len(transporte.telegram("sendMessage")) == 1     # …pero no gastaron la cuota

        # Tras reiniciar el programa el mismo día, las no entregadas siguen sin contar.
        reiniciado, _ = _crear(settings, transporte, memoria=memoria, reloj=reloj)
        await reiniciado.actualizar_cuota()
        assert reiniciado._proactivos_hoy() == 1
        assert reiniciado.puede_proactivo(canal="telegram", tipo="conversar")[0] is True

    asyncio.run(escenario())
    # Las no entregadas se marcan para no contar como "ignoradas" y quedaron en la terminal.
    assert memoria.marcadas == [(1, True), (2, True), (3, True)]
    assert json.loads(memoria.meta[riko_mod.META_NO_ENTREGADAS]) == {"fecha": "2026-10-16",
                                                                      "ids": [1, 2, 3]}
    assert [t for t in captura.textos if "Intento" in t] == [
        "[→ Telegram] Intento 1.", "[→ Telegram] Intento 2.", "[→ Telegram] Intento 3."]


def test_cuota_se_refresca_tras_cada_registro(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    memoria = MemoriaContadora()
    riko, _ = _crear(settings, Transporte(), memoria=memoria,
                     reloj=Reloj(_ts_local(settings, 12, 0)))

    async def escenario() -> None:
        for i in range(3):
            await riko.decir(_reply(f"Iniciativa {i + 1}."), origen="iniciativa", prioridad=1,
                             canal="telegram", motivo="conversar", tipo_iniciativa="conversar")
            assert await riko.vaciar(3.0)
        # main pregunta antes de gastar Gemini: la caché ya refleja los 3 registros.
        permitido, motivo = riko.puede_proactivo(canal="telegram", tipo="conversar")
        assert permitido is False and "(3/3)" in motivo

    asyncio.run(escenario())


def test_nexus_por_telegram_no_gasta_la_cuota_de_iniciativas(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    transporte = Transporte()
    memoria = MemoriaContadora()
    riko, _ = _crear(settings, transporte, memoria=memoria, reloj=Reloj(_ts_local(settings, 12, 0)))

    async def escenario() -> None:
        for i in range(3):
            await riko.decir(_reply(f"Trabajo {i + 1} terminado."), origen="nexus", prioridad=1,
                             canal="telegram", motivo="trabajo_terminado")
            assert await riko.vaciar(3.0)
        assert await memoria.contar_proactivos_hoy() == 0
        assert riko.puede_proactivo(canal="telegram", tipo="conversar") == (True, "permitido")

    asyncio.run(escenario())
    assert len(transporte.telegram("sendMessage")) == 3
    assert len(memoria.llamadas) == 3


def test_critico_no_salta_cuota_ni_limite_por_hora(tmp_path: Path) -> None:
    settings = _settings_claves(tmp_path)
    reloj = Reloj(_ts_local(settings, 14, 0))
    riko, _ = _crear(settings, Transporte(), memoria=MemoriaFalsa(proactivos=3), reloj=reloj)

    async def escenario() -> None:
        await riko.actualizar_cuota()
        permitido, motivo = riko.puede_proactivo(canal="telegram", critico=True)
        assert permitido is False and "cuota" in motivo
        for i in range(4):
            await riko.decir(_reply(f"Interrupción {i + 1}."), origen="iniciativa", prioridad=1,
                             canal="terminal", motivo="conversar", tipo_iniciativa="conversar")
            assert await riko.vaciar(2.0)
            reloj.t += 11 * 60
        assert riko.puede_proactivo(canal="terminal", critico=True) == (
            False, "límite de 4 interrupciones por hora")
        # La pausa sí exime a los críticos (§10.19 paso 2); el límite por hora no.
        riko.pausar_proactividad(30)
        assert "pausa" in riko.puede_proactivo(canal="terminal", tipo="conversar")[1]
        assert "límite" in riko.puede_proactivo(canal="terminal", critico=True)[1]
        reloj.t += 3600
        assert riko.puede_proactivo(canal="terminal", critico=True)[0] is True

    asyncio.run(escenario())


class MemoriaLenta(MemoriaFalsa):
    async def contar_proactivos_hoy(self) -> int:
        await asyncio.sleep(5.0)
        return 0


def _transporte_lento(lento: str) -> httpx.MockTransport:
    """``lento`` = ``"eleven"`` (GET de la voz) o ``"telegram"`` (``getMe``) tarda 5 s."""
    base = Transporte()
    host = "api.elevenlabs.io" if lento == "eleven" else "api.telegram.org"

    async def manejador(peticion: httpx.Request) -> httpx.Response:
        if peticion.url.host == host:
            await asyncio.sleep(5.0)
        return base(peticion)

    return httpx.MockTransport(manejador)


@pytest.mark.parametrize("lento", ["eleven", "telegram"])
def test_iniciar_cabe_en_el_tope_de_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         lento: str) -> None:
    """Un sondeo lento da su propio diagnóstico sin arrastrar al otro ni pasar del tope de main.

    Escala reducida: sondeos de 0,3 s + 0,2 s de margen frente a un tope de 1 s (en producción,
    3 s + 0,5 s frente a los 4 s de ``wait_for`` de main, §6.4).
    """
    _audio_falso(monkeypatch)
    monkeypatch.setattr(riko_mod, "TIMEOUT_DIAGNOSTICO_S", 0.3)
    monkeypatch.setattr(riko_mod, "MARGEN_DIAGNOSTICO_S", 0.2)
    monkeypatch.setattr(riko_mod, "TIMEOUT_CUOTA_ARRANQUE_S", 0.2)
    settings = _settings_claves(tmp_path)

    async def escenario() -> tuple[dict[str, Any], float]:
        cliente = httpx.AsyncClient(transport=_transporte_lento(lento))
        riko = RikoMeguminIO(settings, cliente=cliente, imprimir=Captura(), memoria=MemoriaLenta())
        loop = asyncio.get_running_loop()
        inicio = loop.time()
        diagnosticos = await asyncio.wait_for(riko.iniciar(), 1.0)   # como main, a escala
        transcurrido = loop.time() - inicio
        await cliente.aclose()
        return {d.clave: d for d in diagnosticos}, transcurrido

    diags, transcurrido = asyncio.run(escenario())
    assert transcurrido < 0.9
    if lento == "eleven":
        assert diags["telegram"].estado.value == "OK"
        assert diags["telegram"].detalle == "@rams_amadeus_bot"
        assert diags["voz"].estado.value == "DEGRADADO"
        assert diags["voz"].detalle == "ElevenLabs sin verificar"
    else:
        assert diags["voz"].estado.value == "OK" and diags["voz"].detalle == "ElevenLabs"
        assert diags["telegram"].estado.value == "SIN CONEXIÓN"
        assert diags["telegram"].detalle == "sin respuesta en 0.3 s"


# ---------------------------------------------------------------------------
# Voz: edge-tts sin ffmpeg, decodificado con soundfile (Windows: soundfile + sounddevice)
# ---------------------------------------------------------------------------


def _settings_solo_edge(tmp_path: Path) -> Settings:
    """En línea, sin ElevenLabs ni Telegram: la única voz posible es edge-tts (voz por defecto)."""
    return cargar_settings(entorno=_entorno_base(tmp_path), offline=False, base_dir=RAIZ)


def _mp3_senoide(segundos: float = 1.0, sr: int = 24000, canales: int = 1) -> bytes:
    """MP3 real (MPEG Layer III) escrito en memoria por soundfile; sin red ni edge-tts."""
    sf = pytest.importorskip("soundfile")
    np_ = pytest.importorskip("numpy")
    t = np_.arange(int(segundos * sr)) / sr
    senal = 0.4 * np_.sin(2 * np_.pi * 220 * t)
    if canales > 1:
        senal = np_.stack([senal * (1.0 - 0.3 * c) for c in range(canales)], axis=1)
    buf = io.BytesIO()
    sf.write(buf, senal.astype(np_.float32), sr, format="MP3")
    return buf.getvalue()


def _edge_falso(monkeypatch: pytest.MonkeyPatch, mp3: bytes) -> list[dict[str, str]]:
    """``edge_tts`` falso: ``Communicate(...).stream()`` entrega ``mp3`` en trozos (nunca sale a la red)."""
    llamadas: list[dict[str, str]] = []

    class Communicate:
        def __init__(self, texto: str, voz: str, *, rate: str = "+0%", pitch: str = "+0Hz") -> None:
            llamadas.append({"texto": texto, "voz": voz, "rate": rate, "pitch": pitch})

        async def stream(self) -> AsyncIterator[dict[str, Any]]:
            for inicio in range(0, len(mp3), 700):
                yield {"type": "audio", "data": mp3[inicio:inicio + 700]}
            yield {"type": "WordBoundary", "offset": 0, "text": "fin"}

    monkeypatch.setattr(riko_mod, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    return llamadas


def _audio_edge(monkeypatch: pytest.MonkeyPatch, *, soundfile: bool,
                mp3_argv: list[str] | None = None, ffmpeg: bool = False) -> ReproductorFalso:
    """Reproductor PCM falso; soundfile «instalado» o no; mpv/ffplay y ffmpeg opcionales (falsos)."""
    reproductor = _audio_falso(monkeypatch)
    monkeypatch.setattr(riko_mod, "SOUNDFILE_INSTALADO", soundfile)
    if mp3_argv is not None:
        monkeypatch.setattr(riko_mod, "_argv_reproductor_mp3", lambda: list(mp3_argv))
    if ffmpeg:
        monkeypatch.setattr(riko_mod.shutil, "which",
                            lambda nombre, *a, **k: "/usr/bin/ffmpeg" if nombre == "ffmpeg" else None)
    return reproductor


def _espiar_soundfile(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Anota el hilo de cada decodificación con soundfile (debe ser el executor ``rams-audio``)."""
    hilos: list[str] = []
    original = riko_mod._mp3_a_pcm_soundfile

    def espia(mp3: bytes) -> tuple[bytes, int]:
        hilos.append(threading.current_thread().name)
        return original(mp3)

    monkeypatch.setattr(riko_mod, "_mp3_a_pcm_soundfile", espia)
    return hilos


def _espiar_mp3(monkeypatch: pytest.MonkeyPatch, riko: RikoMeguminIO) -> list[bytes]:
    """Sustituye la reproducción por mpv/ffplay (nunca lanza procesos ni suena)."""
    reproducidos: list[bytes] = []

    async def reproducir_mp3(mp3: bytes, palabras: int, *,
                             al_empezar: Callable[[], None] | None = None) -> float:
        reproducidos.append(mp3)
        if al_empezar is not None:
            al_empezar()
        return 0.8

    monkeypatch.setattr(riko, "_reproducir_mp3", reproducir_mp3)
    return reproducidos


def _rms_s16(pcm: bytes) -> float:
    muestras = array.array("h", pcm)
    if sys.byteorder == "big":
        muestras.byteswap()
    return math.sqrt(sum(m * m for m in muestras) / max(1, len(muestras)))


def test_soundfile_se_detecta_sin_importarlo() -> None:
    """Cargar el módulo no importa soundfile (ni sounddevice): solo ``find_spec``."""
    codigo = ("import sys, lobes.riko_megumin_io as r; "
              "print(r.SOUNDFILE_INSTALADO, 'soundfile' in sys.modules, 'sounddevice' in sys.modules)")
    salida = subprocess.run([sys.executable, "-c", codigo], cwd=str(RAIZ), capture_output=True,
                            text=True, timeout=120, check=True).stdout.split()
    assert salida[1:] == ["False", "False"]
    assert salida[0] in ("True", "False")


def test_mp3_a_pcm_soundfile_y_bloques() -> None:
    """Decodifica un MP3 estéreo real a s16le mono y lo trocea en bloques de 100 ms."""
    mp3 = _mp3_senoide(0.5, canales=2)
    pcm, sr = riko_mod._mp3_a_pcm_soundfile(mp3)
    assert sr == 24000
    assert abs(len(pcm) / (2 * sr) - 0.5) < 0.05              # mono: 2 bytes por muestra
    assert _rms_s16(pcm) > 4000                               # audio real, no silencio ni basura

    async def trocear() -> list[bytes]:
        return [b async for b in riko_mod._bloques_pcm(pcm, sr)]

    bloques = asyncio.run(trocear())
    assert b"".join(bloques) == pcm
    assert all(len(b) == 4800 for b in bloques[:-1]) and 0 < len(bloques[-1]) <= 4800


def test_edge_sin_ffmpeg_suena_con_soundfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin ffmpeg ni mpv/ffplay pero con soundfile: Salome suena por el reproductor PCM con lip-sync."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    mp3 = _mp3_senoide(1.0)
    llamadas = _edge_falso(monkeypatch, mp3)
    hilos = _espiar_soundfile(monkeypatch)
    avatar = AvatarFalso()
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_solo_edge(tmp_path), bus=bus, avatar=avatar, memoria=memoria)

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].estado.value == "DEGRADADO"
        assert diagnosticos["voz"].detalle == "edge-tts es-CO-SalomeNeural · soundfile + falso"
        assert riko.voz_disponible is True
        await riko.decir(_reply("Hola, soy Amadeus."), origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)
        await riko.cerrar()

    asyncio.run(escenario())
    assert captura.textos == ["Hola, soy Amadeus."]
    assert [ll["voz"] for ll in llamadas] == ["es-CO-SalomeNeural"]
    assert hilos and all(h.startswith("rams-audio") for h in hilos)   # nunca en el event loop
    assert reproductor.sr == 24000 and reproductor.cerrado and not reproductor.abortado
    assert abs(len(reproductor.datos) / (2 * 24000) - 1.0) < 0.05     # duración del MP3: 1 s
    assert _rms_s16(bytes(reproductor.datos)) > 4000                  # PCM int16 de verdad
    assert avatar.bocas and max(avatar.bocas) > 0.2 and avatar.cierres >= 1
    assert any(e.nombre == "voz.nivel_boca" and e.datos["valor"] > 0 for e in eventos)
    fin = next(e.datos for e in eventos if e.nombre == "voz.fin")
    assert fin["motor"] == "edge" and fin["duracion_s"] > 0.8
    assert memoria.turnos[0]["canal"] == "voz"


def test_diagnostico_windows_soundfile_y_sounddevice(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _audio_edge(monkeypatch, soundfile=True)
    monkeypatch.setattr(riko_mod, "_detectar_reproductor_pcm", lambda excluir=frozenset(): "sounddevice")
    _edge_falso(monkeypatch, b"")
    riko, _ = _crear(_settings_solo_edge(tmp_path))

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].estado.value == "DEGRADADO"
        assert diagnosticos["voz"].detalle == "edge-tts es-CO-SalomeNeural · soundfile + sounddevice"
        assert riko.voz_disponible is True
        await riko.cerrar()

    asyncio.run(escenario())


def test_edge_sin_soundfile_ni_ffmpeg_ni_mpv_sigue_en_texto(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Comportamiento anterior: sin decodificador ni mpv/ffplay, edge-tts no suena y el texto sigue."""
    reproductor = _audio_edge(monkeypatch, soundfile=False)
    llamadas = _edge_falso(monkeypatch, _mp3_senoide(0.3))
    memoria = MemoriaFalsa()
    riko, captura = _crear(_settings_solo_edge(tmp_path), memoria=memoria)

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].estado.value == "DEGRADADO"
        assert diagnosticos["voz"].detalle == "edge-tts necesita soundfile, ffmpeg, mpv o ffplay"
        assert riko.voz_disponible is False
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola.")) is None
        await riko.decir(_reply("Solo texto."), origen="sistema", canal="voz")
        assert await riko.vaciar(5.0)
        await riko.cerrar()

    asyncio.run(escenario())
    assert captura.textos == ["Solo texto."]
    assert llamadas == [] and not reproductor.datos
    assert memoria.turnos[0]["canal"] == "terminal"


def test_edge_sin_soundfile_usa_mpv_como_antes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=False, mp3_argv=["/usr/bin/mpv", "-"])
    mp3 = _mp3_senoide(0.3)
    _edge_falso(monkeypatch, mp3)
    riko, _ = _crear(_settings_solo_edge(tmp_path))
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].detalle == "edge-tts es-CO-SalomeNeural · mpv"
        assert riko.voz_disponible is True
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola.")) == 0.8
        await riko.cerrar()

    asyncio.run(escenario())
    assert reproducidos == [mp3] and not reproductor.datos


def test_edge_prefiere_soundfile_a_mpv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=True, mp3_argv=["C:/mpv/mpv.EXE", "-"])
    _edge_falso(monkeypatch, _mp3_senoide(0.4))
    hilos = _espiar_soundfile(monkeypatch)
    riko, _ = _crear(_settings_solo_edge(tmp_path))
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> float | None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].detalle == "edge-tts es-CO-SalomeNeural · soundfile + falso"
        duracion = await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola."))
        await riko.cerrar()
        return duracion

    duracion = asyncio.run(escenario())
    assert duracion is not None and abs(duracion - 0.4) < 0.05
    assert len(hilos) == 1 and reproducidos == [] and reproductor.datos


def test_edge_prefiere_ffmpeg_en_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=True, mp3_argv=["/usr/bin/mpv", "-"], ffmpeg=True)
    _edge_falso(monkeypatch, _mp3_senoide(0.3))
    hilos = _espiar_soundfile(monkeypatch)
    riko, _ = _crear(_settings_solo_edge(tmp_path))
    reproducidos = _espiar_mp3(monkeypatch, riko)
    consumido = bytearray()

    async def decodificar_ffmpeg(fuente: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for parte in fuente:            # el MP3 llega en streaming, sin esperar al final
            consumido.extend(parte)
        yield _pcm_senoide(0.3)

    monkeypatch.setattr(riko, "_decodificar_mp3", decodificar_ffmpeg)

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].detalle == "edge-tts es-CO-SalomeNeural · ffmpeg + falso"
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola."))
        await riko.cerrar()

    asyncio.run(escenario())
    assert consumido and hilos == [] and reproducidos == []
    assert bytes(reproductor.datos) == _pcm_senoide(0.3)


def _subtitulos(eventos: list[Evento]) -> list[tuple[str, str]]:
    return [(e.datos["texto"], e.datos["canal"]) for e in eventos if e.nombre == "salida.subtitulo"]


def _detalles_voz(eventos: list[Evento]) -> list[str]:
    return [e.datos["detalle"] for e in eventos
            if e.nombre == "sistema.modulo" and e.datos["clave"] == "voz"]


def _sin_mp3_en_libsndfile(monkeypatch: pytest.MonkeyPatch) -> None:
    """libsndfile anterior a 1.1 (la del sistema, sin wheel con librería): carga, pero no lee MP3."""
    sf = pytest.importorskip("soundfile")
    formatos = {k: v for k, v in sf.available_formats().items() if k != "MP3"}
    monkeypatch.setattr(sf, "available_formats", lambda: dict(formatos))

    def leer_sin_mp3(*_a: Any, **_k: Any) -> Any:
        raise sf.LibsndfileError(1, prefix="Error opening <_io.BytesIO>: ")   # formato no reconocido

    monkeypatch.setattr(sf, "read", leer_sin_mp3)


def _edge_lento(monkeypatch: pytest.MonkeyPatch, mp3: bytes, orden: list[str], *,
                espera: float | dict[str, float] = 0.2) -> None:
    """``edge_tts`` falso que tarda ``espera`` s (por texto, si es un dict) en entregar el MP3.

    Anota en ``orden`` el inicio (``edge:texto``), el final (``edge_fin:texto``) y la cancelación
    (``edge_cancelado:texto``) de cada síntesis. Nunca sale a la red.
    """

    class Communicate:
        def __init__(self, texto: str, voz: str, **_: str) -> None:
            self.texto = texto

        async def stream(self) -> AsyncIterator[dict[str, Any]]:
            orden.append(f"edge:{self.texto}")
            try:
                await asyncio.sleep(espera.get(self.texto, 0.0) if isinstance(espera, dict) else espera)
                for inicio in range(0, len(mp3), 700):
                    await asyncio.sleep(0)
                    yield {"type": "audio", "data": mp3[inicio:inicio + 700]}
            except (asyncio.CancelledError, GeneratorExit):
                orden.append(f"edge_cancelado:{self.texto}")
                raise
            orden.append(f"edge_fin:{self.texto}")

    monkeypatch.setattr(riko_mod, "edge_tts", types.SimpleNamespace(Communicate=Communicate))


def _anotar_reproductor(reproductor: ReproductorFalso, orden: list[str]) -> None:
    """Anota en ``orden`` cuándo se abre (empieza a sonar), se cierra y se aborta el reproductor."""
    abrir, cerrar, abortar = reproductor.abrir, reproductor.cerrar, reproductor.abortar

    async def abrir_anotado(sr: int) -> None:
        orden.append("abrir")
        await abrir(sr)

    async def cerrar_anotado() -> None:
        orden.append("cerrar")
        await cerrar()

    async def abortar_anotado() -> None:
        orden.append("abortar")
        await abortar()

    reproductor.abrir = abrir_anotado          # type: ignore[method-assign]
    reproductor.cerrar = cerrar_anotado        # type: ignore[method-assign]
    reproductor.abortar = abortar_anotado      # type: ignore[method-assign]


def _anotar_subtitulos(bus: BusEventos, orden: list[str]) -> None:
    bus.suscribir("salida.subtitulo",
                  lambda e: orden.append(f"sub:{e.datos['canal']}:{e.datos['texto']}"))


def _dos_segmentos() -> BrocaReply:
    return BrocaReply(segmentos=(
        Segmento(Etiquetas("seria", "brazos_cruzados", "analitico"), "Hola."),
        Segmento(Etiquetas("sonrojo_tsundere", "mirar_a_otro_lado", "nervioso"), "Soy Amadeus."),
    ))


# --- Sincronía de los subtítulos de voz con el audio (labios del visor de VTuber AI Studio) ---


def test_subtitulo_de_voz_sale_cuando_empieza_el_audio_y_se_anticipa_el_mp3(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Con soundfile, el subtítulo «voz» de cada segmento sale al abrir el reproductor, nunca antes
    de tener el MP3 completo; y el MP3 del segundo segmento se descarga mientras suena el primero."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    orden: list[str] = []
    _anotar_reproductor(reproductor, orden)
    _edge_lento(monkeypatch, _mp3_senoide(0.4), orden, espera=0.2)
    bus, eventos = _bus_con_eventos()
    _anotar_subtitulos(bus, orden)
    memoria = MemoriaFalsa()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus, memoria=memoria)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_dos_segmentos(), origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)
        assert riko._mp3_anticipados == {}
        await riko.cerrar()

    asyncio.run(escenario())
    sin_edge = [o for o in orden if not o.startswith("edge")]
    assert sin_edge == ["abrir", "sub:voz:Hola.", "cerrar", "abrir", "sub:voz:Soy Amadeus.", "cerrar"]
    assert orden.index("edge_fin:Hola.") < orden.index("abrir")      # el MP3 entero antes de sonar
    # Anticipación: una sola síntesis por segmento y la segunda empieza mientras suena la primera.
    assert orden.count("edge:Hola.") == 1 and orden.count("edge:Soy Amadeus.") == 1
    assert orden.index("sub:voz:Hola.") < orden.index("edge:Soy Amadeus.") < orden.index("cerrar")
    assert not any(o.startswith("edge_cancelado") for o in orden)
    assert memoria.turnos[0]["canal"] == "voz"


def test_subtitulo_elevenlabs_sale_al_abrir_el_reproductor(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_falso(monkeypatch)
    orden: list[str] = []
    _anotar_reproductor(reproductor, orden)
    bus, _ = _bus_con_eventos()
    _anotar_subtitulos(bus, orden)
    transporte = Transporte()
    riko, _ = _crear(_settings_claves(tmp_path), transporte, bus=bus)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_dos_segmentos(), origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)
        await riko.cerrar()

    asyncio.run(escenario())
    assert orden == ["abrir", "sub:voz:Hola.", "cerrar", "abrir", "sub:voz:Soy Amadeus.", "cerrar"]
    assert len(transporte.eleven("/stream")) == 2


def test_subtitulo_con_mpv_sale_al_lanzar_el_reproductor(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _audio_edge(monkeypatch, soundfile=False, mp3_argv=["/usr/bin/mpv", "-"])
    orden: list[str] = []
    _edge_lento(monkeypatch, _mp3_senoide(0.3), orden, espera=0.1)
    bus, _ = _bus_con_eventos()
    _anotar_subtitulos(bus, orden)
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus)
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_reply("Hola."), origen="sistema", canal="voz")
        assert await riko.vaciar(5.0)
        await riko.cerrar()

    asyncio.run(escenario())
    assert len(reproducidos) == 1
    assert orden == ["edge:Hola.", "edge_fin:Hola.", "sub:voz:Hola."]


def test_silenciar_durante_la_descarga_corta_sin_esperar_a_edge(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Un corte mientras llega el MP3 se atiende enseguida (no al terminar la síntesis) y el subtítulo
    sale con canal «voz»: fue un corte, no una voz que faltó, así que el visor no habla por su cuenta."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    orden: list[str] = []
    _edge_lento(monkeypatch, _mp3_senoide(0.3), orden, espera=5.0)
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_solo_edge(tmp_path), bus=bus)

    async def escenario() -> float:
        await riko.iniciar()
        await riko.decir(_reply("Hola."), origen="sistema", canal="voz")
        entrega = asyncio.create_task(riko.vaciar(10.0))
        await asyncio.sleep(0.3)
        riko.set_silencio(True)
        t0 = time.monotonic()
        assert await entrega
        espera = time.monotonic() - t0
        await riko.cerrar()
        return espera

    espera = asyncio.run(escenario())
    assert espera < 1.0
    assert "edge_cancelado:Hola." in orden and not reproductor.datos
    assert captura.textos == ["Hola."]
    assert _subtitulos(eventos) == [("Hola.", "voz")]


def test_corte_cancela_la_descarga_anticipada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    orden: list[str] = []
    _anotar_reproductor(reproductor, orden)
    _edge_lento(monkeypatch, _mp3_senoide(0.6), orden, espera={"Hola.": 0.0, "Soy Amadeus.": 5.0})
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus)

    def silenciar_con_la_boca_abierta(evento: Evento) -> None:   # ya suena el primer segmento
        if evento.datos["valor"] > 0:
            riko.set_silencio(True)

    bus.suscribir("voz.nivel_boca", silenciar_con_la_boca_abierta)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_dos_segmentos(), origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert riko._mp3_anticipados == {}
        assert "edge_cancelado:Soy Amadeus." in orden       # cancelada al acabar la utterance
        await riko.cerrar()

    asyncio.run(escenario())
    assert orden.index("abrir") < orden.index("edge:Soy Amadeus.") < orden.index("abortar")
    assert orden.count("abrir") == 1                          # el segundo segmento no suena
    assert _subtitulos(eventos) == [("Hola.", "voz"), ("Soy Amadeus.", "voz")]


def test_silencio_antes_de_empezar_no_anticipa_nada(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Si la voz se corta justo al empezar, no se descarga el MP3 siguiente para nada."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    orden: list[str] = []
    _anotar_reproductor(reproductor, orden)
    _edge_lento(monkeypatch, _mp3_senoide(0.3), orden, espera=0.0)
    bus, _ = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus)
    bus.suscribir("salida.subtitulo", lambda e: riko.set_silencio(True))

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_dos_segmentos(), origen="sistema", canal="voz")
        assert await riko.vaciar(10.0)
        await riko.cerrar()

    asyncio.run(escenario())
    assert "edge:Soy Amadeus." not in orden and orden.count("abrir") == 1


# --- soundfile que no sirve: se descarta (iniciar, primera frase o fallos seguidos) ---


def test_edge_soundfile_que_no_carga_se_descarta_al_iniciar(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """soundfile «instalado» pero roto: iniciar lo descarta y el diagnóstico ya dice ffplay."""
    reproductor = _audio_edge(monkeypatch, soundfile=True, mp3_argv=["/usr/bin/ffplay", "-"])
    mp3 = _mp3_senoide(0.3)
    llamadas = _edge_falso(monkeypatch, mp3)
    monkeypatch.setitem(sys.modules, "soundfile", None)      # «import soundfile» → ImportError
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus)
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].detalle == "edge-tts es-CO-SalomeNeural · ffplay"
        assert riko._soundfile is False and riko.voz_disponible is True
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola.")) == 0.8
        await riko.cerrar()

    asyncio.run(escenario())
    assert len(llamadas) == 1 and reproducidos == [mp3] and not reproductor.datos
    assert _detalles_voz(eventos) == ["edge-tts es-CO-SalomeNeural · ffplay"]


def test_edge_soundfile_que_deja_de_cargar_cae_a_mpv(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Si soundfile falla después de iniciar, el mismo MP3 va por ffplay sin volver a sintetizar."""
    reproductor = _audio_edge(monkeypatch, soundfile=True, mp3_argv=["/usr/bin/ffplay", "-"])
    mp3 = _mp3_senoide(0.3)
    llamadas = _edge_falso(monkeypatch, mp3)
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus)
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> None:
        await riko.iniciar()
        assert riko._soundfile is True
        monkeypatch.setitem(sys.modules, "soundfile", None)
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola.")) == 0.8
        assert riko._soundfile is False and riko.voz_disponible is True
        await riko.cerrar()

    asyncio.run(escenario())
    assert len(llamadas) == 1 and reproducidos == [mp3] and not reproductor.datos
    assert _detalles_voz(eventos) == ["edge-tts es-CO-SalomeNeural · soundfile + falso",
                                      "edge-tts es-CO-SalomeNeural · ffplay"]


def test_libsndfile_sin_mp3_se_descarta_al_iniciar(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """libsndfile < 1.1 sin mpv/ffplay: no hay voz desde el arranque, no se sintetiza nada para nada
    y los subtítulos salen por «terminal» (el visor 3D puede leerlos con la voz del navegador)."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    _sin_mp3_en_libsndfile(monkeypatch)
    llamadas = _edge_falso(monkeypatch, _mp3_senoide(0.3))
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, captura = _crear(_settings_solo_edge(tmp_path), bus=bus, memoria=memoria)

    async def escenario() -> None:
        diagnosticos = {d.clave: d for d in await riko.iniciar()}
        assert diagnosticos["voz"].detalle == "edge-tts necesita soundfile, ffmpeg, mpv o ffplay"
        assert riko._soundfile is False and riko.voz_disponible is False
        for i in range(3):
            await riko.decir(_reply(f"Frase {i}."), origen="sistema", canal="voz")
            assert await riko.vaciar(5.0)
        await riko.cerrar()

    asyncio.run(escenario())
    assert llamadas == [] and not reproductor.datos
    assert captura.textos == ["Frase 0.", "Frase 1.", "Frase 2."]
    assert [c for _, c in _subtitulos(eventos)] == ["terminal"] * 3
    assert [t["canal"] for t in memoria.turnos] == ["terminal"] * 3
    assert _detalles_voz(eventos) == ["edge-tts necesita soundfile, ffmpeg, mpv o ffplay"]


def test_libsndfile_sin_mp3_descubierta_en_la_primera_frase(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """La comprobación de iniciar no llega a tiempo (sin alargar iniciar): la primera decodificación
    la repite, descarta soundfile y esa frase sale por «terminal»; la siguiente ni se sintetiza."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    _sin_mp3_en_libsndfile(monkeypatch)
    monkeypatch.setattr(riko_mod, "TIMEOUT_VALIDAR_SOUNDFILE_S", 0.05)
    cargas: list[str] = []
    original = riko_mod._soundfile_con_mp3

    def carga(*_a: Any) -> Any:
        cargas.append(threading.current_thread().name)
        if len(cargas) == 1:
            time.sleep(1.0)              # libsndfile en frío, más lento que el tope de iniciar
        return original()

    monkeypatch.setattr(riko_mod, "_soundfile_con_mp3", carga)
    llamadas = _edge_falso(monkeypatch, _mp3_senoide(0.3))
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus, memoria=memoria)

    async def escenario() -> float:
        t0 = time.monotonic()
        await riko.iniciar()
        duracion_iniciar = time.monotonic() - t0
        assert riko._soundfile is True and riko.voz_disponible is True
        for i in range(2):
            await riko.decir(_reply(f"Frase {i}."), origen="sistema", canal="voz")
            assert await riko.vaciar(10.0)
        assert riko._soundfile is False and riko.voz_disponible is False
        await riko.cerrar()
        return duracion_iniciar

    assert asyncio.run(escenario()) < 0.8
    assert len(cargas) == 2 and all(h.startswith("rams-audio") for h in cargas)
    assert len(llamadas) == 1 and not reproductor.datos
    assert _subtitulos(eventos) == [("Frase 0.", "terminal"), ("Frase 1.", "terminal")]
    assert [t["canal"] for t in memoria.turnos] == ["terminal", "terminal"]
    assert _detalles_voz(eventos) == ["edge-tts es-CO-SalomeNeural · soundfile + falso",
                                      "edge-tts necesita soundfile, ffmpeg, mpv o ffplay"]


def test_soundfile_que_nunca_decodifica_se_descarta(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """libsndfile dice que lee MP3 pero cada lectura falla: tras MAX_FALLOS_SOUNDFILE seguidas se
    descarta y deja de sintetizarse para nada."""
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    sf = pytest.importorskip("soundfile")

    def leer_roto(*_a: Any, **_k: Any) -> Any:
        raise sf.LibsndfileError(1, prefix="Error opening <_io.BytesIO>: ")

    monkeypatch.setattr(sf, "read", leer_roto)
    llamadas = _edge_falso(monkeypatch, _mp3_senoide(0.3))
    memoria = MemoriaFalsa()
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus, memoria=memoria)

    async def escenario() -> None:
        await riko.iniciar()
        assert riko.voz_disponible is True
        for i in range(5):
            await riko.decir(_reply(f"Frase {i}."), origen="sistema", canal="voz")
            assert await riko.vaciar(5.0)
        assert riko._soundfile is False and riko.voz_disponible is False
        await riko.cerrar()

    asyncio.run(escenario())
    assert len(llamadas) == riko_mod.MAX_FALLOS_SOUNDFILE and not reproductor.datos
    assert [c for _, c in _subtitulos(eventos)] == ["terminal"] * 5
    assert [t["canal"] for t in memoria.turnos] == ["terminal"] * 5
    assert _detalles_voz(eventos)[-1] == "edge-tts necesita soundfile, ffmpeg, mpv o ffplay"


def test_mp3_ilegible_para_soundfile_va_por_mpv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=True, mp3_argv=["/usr/bin/mpv", "-"])
    sf = pytest.importorskip("soundfile")
    mp3 = _mp3_senoide(0.3)

    def leer_roto(*_a: Any, **_k: Any) -> Any:
        raise sf.LibsndfileError(1, prefix="Error opening <_io.BytesIO>: ")

    monkeypatch.setattr(sf, "read", leer_roto)
    llamadas = _edge_falso(monkeypatch, mp3)
    bus, eventos = _bus_con_eventos()
    riko, _ = _crear(_settings_solo_edge(tmp_path), bus=bus)
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> None:
        await riko.iniciar()
        await riko.decir(_reply("Hola."), origen="sistema", canal="voz")
        assert await riko.vaciar(5.0)
        assert riko._fallos_soundfile == 1 and riko._soundfile is True
        await riko.cerrar()

    asyncio.run(escenario())
    assert len(llamadas) == 1 and reproducidos == [mp3] and not reproductor.datos
    assert _subtitulos(eventos) == [("Hola.", "voz")]


def test_decodificacion_que_no_termina_cae_a_mpv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=True, mp3_argv=["/usr/bin/mpv", "-"])
    mp3 = _mp3_senoide(0.3)
    _edge_falso(monkeypatch, mp3)
    monkeypatch.setattr(riko_mod, "TIMEOUT_DECODIFICAR_S", 0.05)
    original = riko_mod._mp3_a_pcm_soundfile

    def lenta(datos: bytes) -> tuple[bytes, int]:
        time.sleep(0.5)
        return original(datos)

    monkeypatch.setattr(riko_mod, "_mp3_a_pcm_soundfile", lenta)
    riko, _ = _crear(_settings_solo_edge(tmp_path))
    reproducidos = _espiar_mp3(monkeypatch, riko)

    async def escenario() -> None:
        await riko.iniciar()
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola.")) == 0.8
        assert riko._fallos_soundfile == 1 and riko._soundfile is True
        await riko.cerrar()

    asyncio.run(escenario())
    assert reproducidos == [mp3] and not reproductor.datos


def test_soundfile_valido_reinicia_los_fallos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reproductor = _audio_edge(monkeypatch, soundfile=True)
    _edge_falso(monkeypatch, _mp3_senoide(0.3))
    riko, _ = _crear(_settings_solo_edge(tmp_path))

    async def escenario() -> None:
        await riko.iniciar()
        assert riko._soundfile is True
        riko._fallos_soundfile = riko_mod.MAX_FALLOS_SOUNDFILE - 1
        assert await riko._hablar_edge("Hola.", Segmento(Etiquetas(), "Hola."))
        assert riko._fallos_soundfile == 0 and riko._soundfile is True
        await riko.cerrar()

    asyncio.run(escenario())
    assert reproductor.datos
