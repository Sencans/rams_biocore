"""Tests de lobes/vtuber_ai_bridge.py (avatar 3D VRM en VTuber AI Studio).

Sin red ni visor real: el servidor se simula con ``httpx.MockTransport``. Las corrutinas se
ejecutan con la fixture ``correr`` (``asyncio.run``) de tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")

import lobes.vtuber_ai_bridge as va  # noqa: E402
from core.config import cargar_settings  # noqa: E402
from core.contracts import EstadoModulo, HomeostaticState  # noqa: E402
from lobes.vtuber_ai_bridge import VTuberAIBridge  # noqa: E402

EXPRESIONES = ["aa", "angry", "blink", "happy", "neutral", "relaxed", "sad", "surprised"]


class _ServidorFalso:
    """Imita /api/state y /api/command de VTuber AI Studio y guarda los comandos recibidos."""

    def __init__(self, *, visores: int = 1, caido: bool = False) -> None:
        self.visores = visores
        self.caido = caido
        self.comandos: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.caido:
            raise httpx.ConnectError("conexión rechazada", request=request)
        if request.url.path == "/api/state":
            return httpx.Response(200, json={"viewers": self.visores, "ready": True,
                                             "model": "Kurisu.vrm", "expressionsAvailable": EXPRESIONES})
        if request.url.path == "/api/command":
            cuerpo = json.loads(request.content)
            self.comandos.append(cuerpo)
            if self.visores <= 0:
                return httpx.Response(409, json={"ok": False, "error": "No hay ningún visor abierto."})
            return httpx.Response(200, json={"ok": True, "delivered": self.visores, "results": []})
        return httpx.Response(404)


def _puente(settings: Any, bus: Any, servidor: _ServidorFalso) -> VTuberAIBridge:
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(servidor))
    return VTuberAIBridge(settings, bus, cliente=cliente)


def _subtitulo(texto: str, *, exp: str = "smug", motion: str = "brazos_cruzados", tone: str = "sarcastico",
               canal: str = "terminal") -> dict[str, Any]:
    return {"texto": texto, "exp": exp, "motion": motion, "tone": tone, "canal": canal, "origen": "usuario"}


# ---------------------------------------------------------------------------
# Traducciones puras
# ---------------------------------------------------------------------------

def test_url_solo_local() -> None:
    assert va.url_es_local("http://127.0.0.1:3939")
    assert va.url_es_local("http://localhost:3940/")
    assert not va.url_es_local("http://192.168.1.5:3939")
    assert not va.url_es_local("http://ejemplo.com:3939")


def test_expresion_mezcla_y_filtrado() -> None:
    disponibles = frozenset(EXPRESIONES)
    assert va.comando_expresion("molesta", disponibles)["values"] == {"angry": 0.75}
    assert va.comando_expresion("neutral", disponibles)["name"] == "neutral"
    assert va.comando_expresion("desconocida", disponibles)["name"] == "neutral"
    # Un modelo sin "happy" ni "relaxed" no puede mostrar smug: cae a neutral en vez de fallar.
    assert va.comando_expresion("smug", frozenset({"angry"}))["name"] == "neutral"


def test_todos_los_exp_y_motion_tienen_traduccion() -> None:
    from core.contracts import EXP_VALIDOS, MOTION_VALIDOS, TONE_VALIDOS
    assert set(EXP_VALIDOS) <= set(va.EXP_A_MEZCLA)
    assert set(MOTION_VALIDOS) <= set(va.MOTION_A_PASOS)
    assert set(TONE_VALIDOS) <= set(va.TONO_A_PROSODIA)
    for mezcla in va.EXP_A_MEZCLA.values():
        assert set(mezcla) <= set(va.EMOCIONES_VISOR)
    for pasos in va.MOTION_A_PASOS.values():
        for paso in pasos:
            if paso["type"] == "gesture":
                assert paso["name"] in va.GESTOS_VISOR
            if paso["type"] == "pose":
                assert paso["name"] in va.POSES_VISOR


def test_motion_con_pose_o_mirada_se_deshace() -> None:
    durante, deshacer = va.pasos_motion("brazos_cruzados")
    assert durante == [{"type": "pose", "name": "arms_crossed", "await": False}]
    assert deshacer == [{"type": "pose", "name": "relaxed"}]
    durante, deshacer = va.pasos_motion("mirar_a_otro_lado")
    assert durante[0]["type"] == "look" and deshacer == [{"type": "look", "at": "camera"}]
    assert va.pasos_motion("asentir") == ([{"type": "gesture", "name": "nod", "await": False}], [])


def test_secuencia_de_dos_segmentos(settings_prueba: Any) -> None:
    puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True), None)
    puente._disponibles = frozenset(EXPRESIONES)
    seq = puente.secuencia_para([
        {"texto": "¿Christina? Ni lo intentes.", "exp": "molesta", "motion": "brazos_cruzados",
         "tone": "serio", "voz_propia": False},
        {"texto": "...Bueno, dime qué necesitas.", "exp": "sonrojo_tsundere", "motion": "mirar_a_otro_lado",
         "tone": "nervioso", "voz_propia": False},
    ])
    assert seq["type"] == "sequence"
    tipos = [p["type"] for p in seq["steps"]]
    assert tipos == ["expression", "pose", "speak", "expression", "look", "speak", "pose", "look"]
    hablas = [p for p in seq["steps"] if p["type"] == "speak"]
    assert hablas[0]["text"].startswith("¿Christina?") and hablas[0]["tts"] is True
    assert (hablas[0]["rate"], hablas[0]["pitch"]) == va.TONO_A_PROSODIA["serio"]
    assert (hablas[1]["rate"], hablas[1]["pitch"]) == va.TONO_A_PROSODIA["nervioso"]
    # Las acciones de acompañamiento no bloquean la secuencia; el habla sí.
    assert all(p.get("await") is False for p in seq["steps"] if p["type"] != "speak")
    assert all("await" not in p for p in hablas)


def test_voz_propia_no_usa_tts_del_navegador(settings_prueba: Any) -> None:
    puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True), None)
    seq = puente.secuencia_para([{"texto": "Hola.", "exp": "neutral", "motion": "idle", "tone": "calido",
                                  "voz_propia": True}])
    assert [p["tts"] for p in seq["steps"] if p["type"] == "speak"] == [False]


# ---------------------------------------------------------------------------
# Con bus y servidor simulado
# ---------------------------------------------------------------------------

def test_subtitulos_de_terminal_se_agrupan_en_una_secuencia(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorFalso()

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        diag = await puente.iniciar()
        assert diag.estado == EstadoModulo.OK and puente.en_linea
        bus.publicar("salida.subtitulo", _subtitulo("Primero esto."), origen="riko")
        bus.publicar("salida.subtitulo", _subtitulo("Y luego esto.", exp="feliz", motion="asentir"), origen="riko")
        bus.publicar("salida.subtitulo", _subtitulo("No va al avatar.", canal="telegram"), origen="riko")
        await asyncio.sleep(va.VENTANA_AGRUPAR_S + 0.3)
        await puente.cerrar()

    correr(escenario())
    assert len(servidor.comandos) == 1
    seq = servidor.comandos[0]
    textos = [p["text"] for p in seq["steps"] if p["type"] == "speak"]
    assert textos == ["Primero esto.", "Y luego esto."]


def test_despedida_al_cerrar_se_entrega(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorFalso()

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        bus.publicar("sistema.apagado", {"motivo": "usuario"}, origen="main")
        bus.publicar("salida.subtitulo", _subtitulo("Vuelve temprano mañana.", exp="smug"), origen="riko")
        await puente.cerrar()  # antes de que venza la ventana de agrupado

    correr(escenario())
    assert servidor.comandos[0] == {"type": "gesture", "name": "bow"}
    assert any(p.get("text") == "Vuelve temprano mañana." for p in servidor.comandos[-1]["steps"])


def test_sin_visores_queda_en_espera(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorFalso(visores=0)

    async def escenario() -> Any:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        diag = await puente.iniciar()
        await puente.cerrar()
        return diag

    diag = correr(escenario())
    assert diag.estado == EstadoModulo.EN_ESPERA and "abre el visor" in diag.detalle


def test_servidor_caido_no_lanza_y_publica_estado(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorFalso(caido=True)
    eventos: list[dict[str, Any]] = []
    bus.suscribir("sistema.modulo", lambda e: eventos.append(dict(e.datos)))

    async def escenario() -> Any:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        diag = await puente.iniciar()
        bus.publicar("salida.subtitulo", _subtitulo("Nadie me ve."), origen="riko")
        await asyncio.sleep(va.VENTANA_AGRUPAR_S + 0.2)
        await puente.cerrar()
        return diag

    diag = correr(escenario())
    assert diag.estado == EstadoModulo.EN_ESPERA and "no está corriendo" in diag.detalle
    assert servidor.comandos == []


def test_expresion_de_reposo_por_homeostasis(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorFalso()

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        estado = HomeostaticState.neutral()
        bus.publicar("homeostasis.estado", {"estado": estado.a_dict(), "bump": []}, origen="main")
        bus.publicar("homeostasis.estado", {"estado": estado.a_dict(), "bump": []}, origen="main")
        await asyncio.sleep(0.2)
        await puente.cerrar()

    correr(escenario())
    assert len(servidor.comandos) == 1 and servidor.comandos[0]["type"] == "expression"


def test_url_no_local_desactiva_el_puente(settings_prueba: Any, bus: Any) -> None:
    s = settings_prueba.con(vtuber_ai_habilitado=True, vtuber_ai_url="http://10.0.0.8:3939")
    puente = VTuberAIBridge(s, bus)
    assert puente.diagnostico().estado == EstadoModulo.OFF
    assert bus.estadisticas()["suscriptores"] == 0


def test_settings_vtuber_ai(tmp_path: Any) -> None:
    s = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path), "VTUBER_AI_URL": "http://127.0.0.1:3940",
                                 "VTUBER_AI_TTS": "0", "VTUBER_AI_LANG": "es-CO"}, offline=True)
    assert (s.vtuber_ai_habilitado, s.vtuber_ai_url, s.vtuber_ai_tts, s.vtuber_ai_lang) == \
        (True, "http://127.0.0.1:3940", False, "es-CO")
    remoto = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path),
                                      "VTUBER_AI_URL": "http://ejemplo.com:3939"}, offline=True)
    assert remoto.vtuber_ai_url == "http://127.0.0.1:3939"
