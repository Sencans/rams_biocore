"""Tests de lobes/vtuber_ai_bridge.py (avatar 3D VRM en VTuber AI Studio).

Sin red ni visor real: el servidor se simula con ``httpx.MockTransport``. Las corrutinas se
ejecutan con la fixture ``correr`` (``asyncio.run``) de tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")

import lobes.vtuber_ai_bridge as va  # noqa: E402
from core.config import cargar_settings  # noqa: E402
from core.contracts import EstadoModulo, HomeostaticState  # noqa: E402
from lobes.vtuber_ai_bridge import VTuberAIBridge  # noqa: E402

EXPRESIONES = ["aa", "angry", "blink", "happy", "neutral", "relaxed", "sad", "surprised"]
HUESOS = ["hips", "spine", "chest", "neck", "head", "leftUpperArm", "rightUpperArm"]


class _ServidorFalso:
    """Imita /api/state y /api/command de VTuber AI Studio y guarda los comandos recibidos."""

    def __init__(self, *, visores: int = 1, caido: bool = False) -> None:
        self.visores = visores
        self.caido = caido
        self.comandos: list[Any] = []
        self.actual: dict[str, Any] = {"pose": "relaxed", "look": "camera", "position": {"x": 0.4, "z": -0.2},
                                       "rotation": 15.0, "camera": "full", "speaking": False}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.caido:
            raise httpx.ConnectError("conexión rechazada", request=request)
        if request.url.path == "/api/state":
            return httpx.Response(200, json={"viewers": self.visores, "ready": True,
                                             "model": "Kurisu.vrm", "expressionsAvailable": EXPRESIONES,
                                             "bonesAvailable": HUESOS, "current": self.actual})
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


def _segmentos_de_prueba() -> list[dict[str, Any]]:
    return [{"texto": "Hmph.", "exp": "smug", "motion": "idle", "tone": "sarcastico", "voz_propia": True},
            {"texto": "Bueno, sí.", "exp": "feliz", "motion": "asentir", "tone": "calido", "voz_propia": False}]


def test_speak_sin_globo_por_defecto(settings_prueba: Any) -> None:
    """VTUBER_AI_BURBUJA=0 (por defecto): cada speak lleva ``"bubble": False`` (bool, no se omite)."""
    assert settings_prueba.vtuber_ai_burbuja is False
    puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True), None)
    seq = puente.secuencia_para(_segmentos_de_prueba())
    hablas = [p for p in seq["steps"] if p["type"] == "speak"]
    assert len(hablas) == 2 and all(p["bubble"] is False for p in hablas)
    # Solo los speak llevan bubble: el resto del protocolo no lo conoce.
    assert all("bubble" not in p for p in seq["steps"] if p["type"] != "speak")


def test_speak_con_globo_si_se_pide(settings_prueba: Any) -> None:
    puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True, vtuber_ai_burbuja=True), None)
    seq = puente.secuencia_para(_segmentos_de_prueba())
    assert [p["bubble"] for p in seq["steps"] if p["type"] == "speak"] == [True, True]


def test_speak_sin_setting_de_burbuja_no_muestra_globo() -> None:
    """Defensivo: unos Settings sin ``vtuber_ai_burbuja`` (p. ej. un doble) no activan el globo."""

    class _SettingsMinimos:
        vtuber_ai_url = "http://127.0.0.1:3939"

    puente = VTuberAIBridge(_SettingsMinimos(), None)  # type: ignore[arg-type]
    assert puente.burbuja is False
    seq = puente.secuencia_para(_segmentos_de_prueba()[:1])
    assert [p["bubble"] for p in seq["steps"] if p["type"] == "speak"] == [False]


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
    # El JSON que llega al visor lleva bubble explícito (false por defecto: sin globo de texto).
    assert [p["bubble"] for p in seq["steps"] if p["type"] == "speak"] == [False, False]


@pytest.mark.parametrize("burbuja", [False, True])
def test_bubble_viaja_en_el_post_segun_settings(settings_prueba: Any, bus: Any, correr: Any, burbuja: bool) -> None:
    servidor = _ServidorFalso()

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True, vtuber_ai_burbuja=burbuja), bus, servidor)
        await puente.iniciar()
        bus.publicar("salida.subtitulo", _subtitulo("Con voz propia.", canal="voz"), origen="riko")
        await asyncio.sleep(0.1)
        await puente.cerrar()

    correr(escenario())
    hablas = [p for c in servidor.comandos for p in c["steps"] if p["type"] == "speak"]
    assert [(p["text"], p["bubble"], p["tts"]) for p in hablas] == [("Con voz propia.", burbuja, False)]


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


def test_api_para_la_corteza_motora(settings_prueba: Any, bus: Any, correr: Any) -> None:
    """Inventario, estado del visor, habla estimada, encolar y reposo externo."""
    servidor = _ServidorFalso()

    async def escenario() -> VTuberAIBridge:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        assert puente.huesos_disponibles is None and puente.posicion is None and puente.ts_estado is None
        await puente.iniciar()
        assert puente.huesos_disponibles == frozenset(HUESOS)
        assert puente.expresiones_disponibles == frozenset(EXPRESIONES)
        assert puente.posicion == (0.4, -0.2, 15.0)
        assert puente.estado_visor is not None and puente.estado_visor["camera"] == "full"
        assert puente.ts_estado is not None
        # La copia no expone el estado interno.
        copia = puente.estado_visor
        copia["camera"] = "face"
        assert puente.estado_visor["camera"] == "full"
        # Con reposo externo el puente no toca la cara por homeostasis.
        puente.reposo_externo = True
        bus.publicar("homeostasis.estado", {"estado": HomeostaticState.neutral().a_dict(), "bump": []},
                     origen="main")
        # encolar no bloquea y respeta el orden (una lista viaja como lista: en paralelo en el visor).
        puente.encolar({"type": "look", "at": "mouse"})
        puente.encolar([{"type": "pose", "name": "thinking"}, {"type": "look", "at": "up_left"}])
        await asyncio.sleep(0.2)
        assert not puente.hablando
        puente.secuencia_para([{"texto": "Hmph. " * 10, "exp": "smug", "motion": "idle", "tone": "sarcastico",
                                "voz_propia": True}])
        assert puente.hablando
        await puente.cerrar()
        return puente

    correr(escenario())
    assert servidor.comandos == [{"type": "look", "at": "mouse"},
                                 [{"type": "pose", "name": "thinking"}, {"type": "look", "at": "up_left"}]]


def test_posicion_ilegible_es_none(settings_prueba: Any) -> None:
    puente = VTuberAIBridge(settings_prueba.con(vtuber_ai_habilitado=True), None)
    puente._actual = {"position": {"x": "a", "z": 0}, "rotation": 0}
    assert puente.posicion is None
    puente._actual = {"position": {"x": float("nan"), "z": 0}}
    assert puente.posicion is None
    puente._actual = {"position": [1, 2]}
    assert puente.posicion is None


def test_vocabulario_del_protocolo() -> None:
    assert {"camera", "mouse", "up_left", "point", "none"} <= va.MIRADAS_VISOR
    assert va.MANOS_VISOR == frozenset({"open", "relaxed", "fist", "point", "peace", "thumbs_up"})
    assert va.CAMARAS_VISOR == frozenset({"face", "bust", "full", "wide"})


def test_url_no_local_desactiva_el_puente(settings_prueba: Any, bus: Any) -> None:
    s = settings_prueba.con(vtuber_ai_habilitado=True, vtuber_ai_url="http://10.0.0.8:3939")
    puente = VTuberAIBridge(s, bus)
    assert puente.diagnostico().estado == EstadoModulo.OFF
    assert bus.estadisticas()["suscriptores"] == 0


# ---------------------------------------------------------------------------
# Modo escritorio y tema (v0.2, docs/COMPANION.md §C2.5 y §C2.10)
# ---------------------------------------------------------------------------


class _ServidorEscritorio(_ServidorFalso):
    """VTuber AI Studio v0.2 simulado: desktopViewers*, viewerSeq, códigos 409 y cabeceras recibidas."""

    def __init__(self, *, visores: int = 1, escritorio: int = 1, escritorio_total: int = 1,
                 seq: int | None = 1) -> None:
        super().__init__(visores=visores)
        self.escritorio = escritorio
        self.escritorio_total = escritorio_total
        self.seq = seq
        self.peticiones: list[tuple[str, str, dict[str, str]]] = []
        self.estados = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append((request.method, request.url.path, dict(request.headers)))
        if request.url.path == "/api/state":
            self.estados += 1
            datos: dict[str, Any] = {"viewers": self.visores, "ready": True, "model": "Kurisu.vrm",
                                     "expressionsAvailable": EXPRESIONES, "bonesAvailable": HUESOS,
                                     "current": self.actual, "desktopViewers": self.escritorio,
                                     "desktopViewersTotal": self.escritorio_total}
            if self.seq is not None:
                datos["viewerSeq"] = self.seq
            return httpx.Response(200, json=datos)
        if request.url.path == "/api/command":
            cuerpo = json.loads(request.content)
            self.comandos.append(cuerpo)
            if self.visores <= 0:
                return httpx.Response(409, json={"ok": False, "code": "sin_visores", "error": "Sin visores."})
            lote = cuerpo if isinstance(cuerpo, list) else [cuerpo]
            if any(c.get("type") == "desktop" for c in lote):
                if len(lote) > 1:
                    return httpx.Response(400, json={"ok": False, "error": "lote mixto"})
                if self.escritorio_total <= 0:
                    return httpx.Response(409, json={"ok": False, "code": "sin_overlay", "error": "Sin overlay."})
            return httpx.Response(200, json={"ok": True, "delivered": self.visores, "results": []})
        return httpx.Response(404)

    def de_tipo(self, tipo: str) -> list[Any]:
        return [c for c in self.comandos if isinstance(c, dict) and c.get("type") == tipo]


TEMA_AMADEUS = {"type": "theme", "name": "amadeus", "rim": True, "scanlines": 0.07}


def test_sanear_desktop_y_comando_tema() -> None:
    assert va.sanear_desktop({"type": "desktop", "action": "walk_to", "x": 0.5}) == \
        {"type": "desktop", "action": "walk_to", "x": 0.5}
    assert va.sanear_desktop({"action": "walk_to", "x": 1920, "unit": "px", "speed": 1, "otra": 3}) == \
        {"type": "desktop", "action": "walk_to", "x": 1920.0, "unit": "px", "speed": 1.0}
    for accion in ("show", "hide", "enter", "leave", "baila", None):
        assert va.sanear_desktop({"type": "desktop", "action": accion}) is None
    for malo in ({"action": "wander", "seconds": 60}, {"action": "wander", "seconds": 0},
                 {"action": "walk_to"}, {"action": "walk_to", "x": 1.5}, {"action": "walk_to", "x": True},
                 {"action": "walk_to", "x": 0.5, "unit": "em"}, {"action": "walk_to", "x": 20000, "unit": "px"},
                 {"action": "peek", "edge": "top"}, {"action": "chase_cursor", "speed": 5},
                 {"action": "chase_cursor", "speed": 0.1}, {"type": "move", "action": "stop"}, "basura"):
        assert va.sanear_desktop(malo) is None, malo
    assert va.sanear_desktop({"action": "wander", "seconds": 15, "speed": 3}) == \
        {"type": "desktop", "action": "wander", "seconds": 15.0, "speed": 3.0}
    assert va.sanear_desktop({"action": "peek", "edge": "left", "unit": "px"}) == \
        {"type": "desktop", "action": "peek", "edge": "left"}
    assert va.comando_tema() == TEMA_AMADEUS
    assert va.comando_tema("none", rim=False, scanlines=0.9) == \
        {"type": "theme", "name": "none", "rim": False, "scanlines": 0.2}
    assert va.comando_tema("kurisu") is None
    assert va.ACCIONES_DESKTOP == ("walk_to", "peek", "chase_cursor", "seek_user", "wander", "stop")


def test_estado_de_escritorio_y_tema_al_cambiar_viewer_seq(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio(visores=2, escritorio=1, escritorio_total=2, seq=3)

    async def escenario() -> list[Any]:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        assert (puente.visores_escritorio, puente.visores_escritorio_total, puente.viewer_seq) == (0, 0, 0)
        await puente.iniciar()
        assert (puente.visores_escritorio, puente.visores_escritorio_total, puente.viewer_seq) == (1, 2, 3)
        assert "overlay de escritorio" in puente.diagnostico().detalle
        await asyncio.sleep(0.05)
        temas = [len(servidor.de_tipo("theme"))]                 # 1.ª consulta con visores: cuenta como cambio
        await puente.consultar_estado()                          # mismo viewerSeq: nada
        await asyncio.sleep(0.05)
        temas.append(len(servidor.de_tipo("theme")))
        servidor.seq = 4                                         # nuevo visor o recarga
        await puente.consultar_estado()
        await asyncio.sleep(0.05)
        temas.append(len(servidor.de_tipo("theme")))
        servidor.visores, servidor.seq = 0, 5                    # sin visores: no se gasta el tema…
        await puente.consultar_estado()
        servidor.visores = 1                                     # …y llega en cuanto vuelve uno
        await puente.consultar_estado()
        await asyncio.sleep(0.05)
        temas.append(len(servidor.de_tipo("theme")))
        await puente.cerrar()
        return temas

    assert correr(escenario()) == [1, 1, 2, 3]
    assert all(c == TEMA_AMADEUS for c in servidor.de_tipo("theme"))


@pytest.mark.parametrize(("tema", "seq"), [("none", 1), ("amadeus", None)])
def test_sin_tema_con_none_o_servidor_v01(settings_prueba: Any, bus: Any, correr: Any, tema: str,
                                          seq: int | None) -> None:
    servidor = _ServidorEscritorio(seq=seq)

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True, vtuber_ai_tema=tema), bus, servidor)
        await puente.iniciar()
        if seq is not None:
            servidor.seq = seq + 1
        await puente.consultar_estado()
        await asyncio.sleep(0.05)
        await puente.cerrar()

    correr(escenario())
    assert servidor.de_tipo("theme") == []


def test_aplicar_tema_encola_el_comando(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio(seq=None)

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        puente.aplicar_tema()
        puente.aplicar_tema("none", rim=False, scanlines=-1)
        puente.aplicar_tema("kurisu")                            # desconocido: se descarta
        await asyncio.sleep(0.1)
        await puente.cerrar()

    correr(escenario())
    assert servidor.de_tipo("theme") == [TEMA_AMADEUS, {"type": "theme", "name": "none", "rim": False,
                                                        "scanlines": 0.0}]


def test_desktop_encola_solo_y_devuelve_false_sin_overlay(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio(seq=None)

    async def escenario() -> list[bool]:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        assert puente.desktop("walk_to", x=0.5) is False         # aún no está en línea
        await puente.iniciar()
        resultados = [puente.desktop("walk_to", x=0.5), puente.desktop("show"), puente.desktop("wander", seconds=60),
                      puente.desktop("peek", edge="left", type="move", action="stop")]
        await asyncio.sleep(0.1)
        servidor.escritorio_total = 0                            # el overlay se cerró
        await puente.consultar_estado()
        resultados.append(puente.desktop("seek_user"))
        await puente.cerrar()
        return resultados

    assert correr(escenario()) == [True, False, False, True, False]
    # Cada desktop viaja solo (nunca en una lista paralela) y con type/action propios.
    assert servidor.comandos == [{"type": "desktop", "action": "walk_to", "x": 0.5},
                                 {"type": "desktop", "action": "peek", "edge": "left"}]


def test_desktop_sin_visores_ni_servidor(settings_prueba: Any, bus: Any, correr: Any) -> None:
    async def escenario(servidor: _ServidorFalso) -> bool:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        resultado = puente.desktop("wander", seconds=5)
        await puente.cerrar()
        return resultado

    assert correr(escenario(_ServidorEscritorio(visores=0))) is False
    assert correr(escenario(_ServidorFalso(caido=True))) is False
    assert correr(escenario(_ServidorFalso())) is False          # servidor v0.1: sin desktopViewersTotal


def test_409_sin_overlay_no_cambia_el_estado(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio(escritorio_total=0)
    modulos: list[dict[str, Any]] = []
    bus.suscribir("sistema.modulo", lambda e: modulos.append(dict(e.datos)))

    async def escenario() -> tuple[bool, Any, bool, int]:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        puente._visores_escritorio_total = 1                     # el sondeo quedó viejo: el overlay ya cerró
        n = len(modulos)
        ok = await puente.enviar({"type": "desktop", "action": "wander"})
        estado = puente.diagnostico().estado
        resultado = ok, estado, puente.en_linea and len(modulos) == n, puente.visores_escritorio_total
        await puente.cerrar()
        return resultado

    ok, estado, sin_cambios, total = correr(escenario())
    # Falla solo ese comando: ni el estado del módulo ni los contadores (los decide el próximo sondeo).
    assert ok is False and estado == EstadoModulo.OK and sin_cambios and total == 1


def test_409_sin_overlay_sondea_ya_y_deprisa_hasta_ver_el_overlay(settings_prueba: Any, bus: Any, correr: Any,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Un 409 sin_overlay suele ser un hueco breve (el overlay reconecta su SSE o se recarga): el puente no
    espera los 15 s del sondeo normal, sondea ya y luego cada ``SONDEO_RAPIDO_S`` hasta volver a verlo."""
    monkeypatch.setattr(va, "MIN_ENTRE_SONDEOS_S", 0.05)
    monkeypatch.setattr(va, "SONDEO_RAPIDO_S", 0.3)
    servidor = _ServidorEscritorio(seq=None)

    async def esperar(condicion: Any, maximo: float = 2.0) -> float:
        inicio = time.monotonic()
        while not condicion() and time.monotonic() - inicio < maximo:
            await asyncio.sleep(0.02)
        return time.monotonic() - inicio

    async def escenario() -> tuple[float, int, float, int, int]:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        parar = asyncio.Event()
        tarea = asyncio.create_task(puente.bucle(parar))
        await esperar(lambda: puente.visores_escritorio_total == 1)
        servidor.escritorio_total = 0                            # el overlay reconecta su SSE
        assert puente.desktop("wander", seconds=5)
        t_cero = await esperar(lambda: puente.visores_escritorio_total == 0)   # sondeo inmediato (no 15 s)
        sondeos = servidor.estados
        await asyncio.sleep(0.1)
        servidor.escritorio_total = 1                            # ya volvió
        t_vuelta = await esperar(lambda: puente.visores_escritorio_total == 1)  # sondeo rápido (no 15 s)
        vistos = servidor.estados
        await asyncio.sleep(0.8)                                 # visto el overlay: vuelve el ritmo normal
        tras_volver = servidor.estados - vistos
        parar.set()
        await asyncio.wait_for(tarea, timeout=2.0)
        await puente.cerrar()
        return t_cero, sondeos, t_vuelta, tras_volver, puente.diagnostico().estado == EstadoModulo.OK

    t_cero, sondeos, t_vuelta, tras_volver, en_linea = correr(escenario())
    assert t_cero < 1.0 and sondeos >= 2
    assert t_vuelta < 1.0 and tras_volver == 0 and en_linea


def test_409_sin_visores_queda_en_espera(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio()

    async def escenario() -> tuple[bool, Any]:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        assert puente.en_linea
        servidor.visores = 0                                     # cerraron el último visor
        ok = await puente.enviar({"type": "look", "at": "camera"})
        diag = puente.diagnostico()
        await puente.cerrar()
        return ok, diag

    ok, diag = correr(escenario())
    assert ok is False and diag.estado == EstadoModulo.EN_ESPERA and "abre el visor" in diag.detalle


def test_peticiones_con_x_source_y_json(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio()

    async def escenario() -> None:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        await puente.iniciar()
        puente.encolar({"type": "look", "at": "mouse"})
        puente.desktop("seek_user")
        await asyncio.sleep(0.1)
        await puente.cerrar()

    correr(escenario())
    assert {p for _m, p, _h in servidor.peticiones} == {"/api/state", "/api/command"}
    for metodo, _ruta, cabeceras in servidor.peticiones:
        assert cabeceras.get("x-source") == "rams"
        if metodo == "POST":
            assert cabeceras.get("content-type", "").startswith("application/json")


def test_refrescar_adelanta_el_sondeo(settings_prueba: Any, bus: Any, correr: Any) -> None:
    servidor = _ServidorEscritorio(seq=None)

    async def escenario() -> tuple[int, int]:
        puente = _puente(settings_prueba.con(vtuber_ai_habilitado=True), bus, servidor)
        puente.refrescar()                                       # sin bucle: no hace nada (ni lanza)
        parar = asyncio.Event()
        tarea = asyncio.create_task(puente.bucle(parar))
        await asyncio.sleep(0.2)
        antes = servidor.estados                                 # primer sondeo; el siguiente sería en 15 s
        servidor.escritorio_total = 3
        puente.refrescar()
        for _ in range(40):                                      # ≤ 2 s (mínimo 1 s entre sondeos)
            await asyncio.sleep(0.05)
            if servidor.estados > antes:
                break
        despues = servidor.estados
        assert puente.visores_escritorio_total == 3
        parar.set()
        await asyncio.wait_for(tarea, timeout=2.0)
        await puente.cerrar()
        return antes, despues

    antes, despues = correr(escenario())
    assert antes == 1 and despues == 2


def test_settings_vtuber_ai(tmp_path: Any) -> None:
    s = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path), "VTUBER_AI_URL": "http://127.0.0.1:3940",
                                 "VTUBER_AI_TTS": "0", "VTUBER_AI_LANG": "es-CO"}, offline=True)
    assert (s.vtuber_ai_habilitado, s.vtuber_ai_url, s.vtuber_ai_tts, s.vtuber_ai_lang) == \
        (True, "http://127.0.0.1:3940", False, "es-CO")
    assert s.vtuber_ai_burbuja is False                          # por defecto: sin globo de texto
    con_globo = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path), "VTUBER_AI_BURBUJA": "1"}, offline=True)
    assert con_globo.vtuber_ai_burbuja is True
    remoto = cargar_settings(entorno={"RAMS_DATA_DIR": str(tmp_path),
                                      "VTUBER_AI_URL": "http://ejemplo.com:3939"}, offline=True)
    assert remoto.vtuber_ai_url == "http://127.0.0.1:3939"
