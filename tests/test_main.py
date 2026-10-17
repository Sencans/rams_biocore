"""Tests del orquestador (main.py): consola, canales, confirmaciones, Telegram y apagado (§10.21, §6.6).

Sin red ni hardware: ``settings_prueba`` es offline, la cámara está deshabilitada y la consola
escribe en un ``io.StringIO``. Sin pytest-asyncio: cada test usa ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

from core.bus import BusEventos
from core.config import Settings, cargar_settings, leer_env_archivo
from core.contracts import (
    BrocaReply,
    ContextoBroca,
    EstadoModulo,
    EstadoPresencia,
    Etiquetas,
    HomeostaticState,
)
from core.intenciones import MENSAJE_SOLO_LOCAL, Intencion

RAIZ = Path(__file__).resolve().parents[1]


def _orquestador(settings: Settings) -> tuple[Any, io.StringIO]:
    import main

    salida = io.StringIO()
    consola = main.Consola(settings, salida=salida, usar_prompt_toolkit=False)
    return main.Orquestador(settings, consola=consola), salida


def _espiar_broca(monkeypatch: pytest.MonkeyPatch, orq: Any) -> list[ContextoBroca]:
    """Envuelve ``broca.verbalizar`` para capturar los contextos que recibe."""
    contextos: list[ContextoBroca] = []
    original = orq.broca.verbalizar

    async def espia(contexto: ContextoBroca, entrada: str | None, **kwargs: Any) -> BrocaReply:
        contextos.append(contexto)
        return await original(contexto, entrada, **kwargs)

    monkeypatch.setattr(orq.broca, "verbalizar", espia)
    return contextos


async def _esperar(condicion: Any, maximo_s: float = 2.0) -> bool:
    limite = time.monotonic() + maximo_s
    while time.monotonic() < limite:
        if condicion():
            return True
        await asyncio.sleep(0.01)
    return bool(condicion())


# ---------------------------------------------------------------------------
# 1. --selftest
# ---------------------------------------------------------------------------


def test_autodiagnostico_devuelve_cero(capsys: pytest.CaptureFixture[str]) -> None:
    for carpeta in ("persona", "interface"):
        if not (RAIZ / carpeta).is_dir():
            pytest.skip(f"falta {carpeta}/ (lo entrega otro grupo)")
    import main

    inicio = time.monotonic()
    codigo = asyncio.run(main.autodiagnostico())
    salida = capsys.readouterr().out
    assert codigo == 0, salida
    assert time.monotonic() - inicio < 30.0
    assert "FALLO" not in salida
    assert "Medidor de divergencia" in salida


# ---------------------------------------------------------------------------
# 2. CLI
# ---------------------------------------------------------------------------


def test_parser_offline_sin_hud(settings_prueba: Settings) -> None:
    import main

    args = main.construir_parser().parse_args(["--offline", "--sin-hud"])
    base = settings_prueba.con(offline=False, hud_habilitado=True)
    resultado = main.aplicar_argumentos(base, args)
    assert resultado.offline is True
    assert resultado.hud_habilitado is False
    assert resultado.voz_habilitada is True and resultado.camara_habilitada is False

    otros = main.construir_parser().parse_args(["--sin-voz", "--sin-camara", "--env", "x.env", "--verbose"])
    cambiado = main.aplicar_argumentos(base.con(camara_habilitada=True), otros)
    assert cambiado.voz_habilitada is False and cambiado.camara_habilitada is False
    assert otros.env == "x.env" and otros.verbose is True and otros.selftest is False


# ---------------------------------------------------------------------------
# 3. Turno local offline
# ---------------------------------------------------------------------------


def test_turno_local_offline(settings_prueba: Settings) -> None:
    async def principal() -> tuple[Any, list[Any]]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            reply = await orq.procesar_entrada("hola amadeus")
            turnos = await orq.memoria.ventana_activa()
            return reply, turnos
        finally:
            await orq.apagar()

    reply, turnos = asyncio.run(principal())
    assert isinstance(reply, BrocaReply)
    assert reply.texto_plano
    assert reply.fuente == "local"
    assert len(turnos) == 2
    assert [t.rol for t in turnos] == ["usuario", "amadeus"]


# ---------------------------------------------------------------------------
# 4. Comando /estado
# ---------------------------------------------------------------------------


def test_comando_estado_sin_broca(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> tuple[Any, list[ContextoBroca], str]:
        orq, salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            contextos = _espiar_broca(monkeypatch, orq)
            resultado = await orq.procesar_entrada("/estado")
            return resultado, contextos, salida.getvalue()
        finally:
            await orq.apagar()

    resultado, contextos, texto = asyncio.run(principal())
    assert resultado is None
    assert contextos == []
    assert "Divergencia" in texto
    assert "Homeostasis:" in texto
    assert "Presencia:" in texto


# ---------------------------------------------------------------------------
# 5. Confirmaciones (cerrar una ventana ajena)
# ---------------------------------------------------------------------------


def test_confirmacion_rechazada_sin_turno_extra(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> dict[str, Any]:
        orq, salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            assert orq.aura is not None, "lobes.aura_vision_pc no disponible"
            contextos = _espiar_broca(monkeypatch, orq)
            orq.bus.publicar("pc.ventana", {"clase": "firefox", "titulo": "Docs", "direccion": "0x55aa",
                                            "pid": 999999})
            tarea = asyncio.create_task(orq.procesar_entrada("cierra la ventana"))
            pendiente = await _esperar(orq.permisos.hay_pendiente, 2.0)
            descripcion = orq.permisos.pendiente().accion.descripcion if pendiente else ""
            respuesta_no = await orq.procesar_entrada("no")
            reply = await asyncio.wait_for(tarea, timeout=5.0)
            turnos = await orq.memoria.ventana_activa()
            return {"pendiente": pendiente, "descripcion": descripcion, "no": respuesta_no, "reply": reply,
                    "contextos": contextos, "turnos": turnos, "salida": salida.getvalue()}
        finally:
            await orq.apagar()

    r = asyncio.run(principal())
    assert r["pendiente"] is True
    assert "firefox" in r["descripcion"]
    assert r["no"] is None
    assert isinstance(r["reply"], BrocaReply)
    accion = r["contextos"][-1].accion
    assert accion is not None and accion.ok is False
    assert "no autorizada" in accion.mensaje.lower()
    assert len(r["turnos"]) == 2
    assert "⚠ Confirmación requerida" in r["salida"]


# ---------------------------------------------------------------------------
# 6. Sin ventana previa / ventana propia
# ---------------------------------------------------------------------------


def test_cerrar_sin_ventana_previa(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> dict[str, Any]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            contextos = _espiar_broca(monkeypatch, orq)
            reply = await orq.procesar_entrada("cierra la ventana")
            pendiente = orq.permisos.hay_pendiente()
            orq.bus.publicar("pc.ventana", {"clase": "kitty", "titulo": "R.A.M.S.", "direccion": "0xabc",
                                            "pid": os.getpid()})
            return {"reply": reply, "pendiente": pendiente, "contextos": contextos,
                    "ventana": orq._ventana_previa}
        finally:
            await orq.apagar()

    r = asyncio.run(principal())
    assert r["pendiente"] is False
    assert isinstance(r["reply"], BrocaReply)
    accion = r["contextos"][-1].accion
    assert accion is not None and accion.ok is False
    assert accion.mensaje.startswith("No sé qué ventana cerrar")
    assert r["ventana"] is None


# ---------------------------------------------------------------------------
# 7. Telegram: capacidades reducidas
# ---------------------------------------------------------------------------


def test_telegram_no_actua_sobre_el_pc(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[str] = []

    async def principal() -> list[ContextoBroca]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            assert orq.aura is not None, "lobes.aura_vision_pc no disponible"

            async def ejecutar_prohibido(*_a: Any, **_k: Any) -> Any:
                llamadas.append("ejecutar")
                raise AssertionError("aura.ejecutar no debe llamarse desde Telegram")

            async def vistazo_prohibido(*_a: Any, **_k: Any) -> Any:
                llamadas.append("vistazo")
                raise AssertionError("aura.vistazo no debe llamarse desde Telegram")

            monkeypatch.setattr(orq.aura, "ejecutar", ejecutar_prohibido)
            monkeypatch.setattr(orq.aura, "vistazo", vistazo_prohibido)
            contextos = _espiar_broca(monkeypatch, orq)
            await orq.procesar_entrada("abre terminal", canal="telegram")
            await orq.procesar_entrada("me ves", canal="telegram")
            return contextos
        finally:
            await orq.apagar()

    contextos = asyncio.run(principal())
    assert llamadas == []
    assert len(contextos) == 2
    for contexto in contextos:
        assert contexto.accion is not None
        assert contexto.accion.mensaje == MENSAJE_SOLO_LOCAL
        assert contexto.canal_entrada == "telegram"
        assert contexto.modo_salida == "telegram"


# ---------------------------------------------------------------------------
# 8. Elección de canal: la explicación técnica nunca va a voz
# ---------------------------------------------------------------------------


def test_explicacion_tecnica_nunca_por_voz(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> tuple[list[dict[str, Any]], list[ContextoBroca]]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            monkeypatch.setattr(type(orq.riko), "voz_disponible", property(lambda _self: True))
            entregas: list[dict[str, Any]] = []

            async def decir_espia(reply: BrocaReply, **kwargs: Any) -> None:
                entregas.append(kwargs)

            monkeypatch.setattr(orq.riko, "decir", decir_espia)
            contextos = _espiar_broca(monkeypatch, orq)
            await orq.procesar_entrada("explícame cómo funciona el conectoma de la mosca y por qué usa neuronas LIF")
            assert orq.presencia.estado().presente is True
            await orq.procesar_entrada("hola amadeus")
            return entregas, contextos
        finally:
            await orq.apagar()

    entregas, contextos = asyncio.run(principal())
    assert entregas[0]["canal"] == "terminal"
    assert entregas[0]["origen"] == "respuesta"
    assert contextos[0].modo_salida == "terminal_largo"
    assert entregas[1]["canal"] == "voz"           # control: con voz y presencia, una charla sí va a voz
    assert contextos[1].modo_salida == "voz"


# ---------------------------------------------------------------------------
# 9. Apagado (§6.6)
# ---------------------------------------------------------------------------


def test_apagado_imprime_despedida_con_parar_activo(settings_prueba: Settings) -> None:
    async def principal() -> tuple[str, float, list[str]]:
        orq, salida = _orquestador(settings_prueba)
        await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
        eventos: list[str] = []
        orq.bus.suscribir("sistema.apagado", lambda e: eventos.append(e.datos["motivo"]))
        orq.parar.set()
        inicio = time.monotonic()
        await orq.apagar("usuario")
        duracion = time.monotonic() - inicio
        await orq.apagar("usuario")      # idempotente
        return salida.getvalue(), duracion, eventos

    texto, duracion, eventos = asyncio.run(principal())
    assert duracion < 15.0
    assert "[Amadeus] › " in texto
    assert "Fin de la sesión." in texto
    assert "El Psy Kongroo" not in texto
    assert eventos == ["usuario"]


def test_apagado_especial_con_logro(settings_prueba: Settings) -> None:
    async def principal() -> tuple[str, Any]:
        orq, salida = _orquestador(settings_prueba)
        await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
        resultado = await orq.procesar_entrada("/logro terminé el prototipo del conectoma")
        await orq.apagar()
        return salida.getvalue(), resultado

    texto, resultado = asyncio.run(principal())
    assert resultado is None
    assert "El Psy Kongroo." in texto
    assert "Fin de la sesión. El Psy Kongroo." in texto


# ---------------------------------------------------------------------------
# 10. .env.example
# ---------------------------------------------------------------------------


def test_env_example_sin_avisos(caplog: pytest.LogCaptureFixture) -> None:
    ruta = RAIZ / ".env.example"
    assert ruta.is_file()
    leido = leer_env_archivo(ruta)
    assert leido, ".env.example vacío o ilegible"
    with caplog.at_level(logging.DEBUG, logger="rams.config"):
        settings = cargar_settings(entorno={**leido, "XDG_RUNTIME_DIR": "/run/user/1000"})
    assert settings.tiene_telegram is False
    assert settings.idle_flag_path == Path("/run/user/1000/rams_idle")
    assert settings.hypr_modo_dispatch == "auto"
    assert settings.camara_habilitada is False
    assert settings.rango_lab == "Lab Mem 001" and settings.nombre_usuario is None
    assert settings.mcp_memoria is False and settings.perfil_en_prompt is True
    assert settings.conectoma_fuente == "reducido"
    avisos = [r for r in caplog.records if r.name == "rams.config" and r.levelno >= logging.WARNING]
    assert avisos == [], [r.getMessage() for r in avisos]
    texto = ruta.read_text(encoding="utf-8")
    for linea in texto.splitlines():
        if "=" in linea and not linea.lstrip().startswith("#"):
            assert "#" not in linea.split("=", 1)[1], f"comentario tras '=': {linea}"


# ---------------------------------------------------------------------------
# Arranque impreso y ciclo completo
# ---------------------------------------------------------------------------


def test_arranque_impreso(settings_prueba: Settings) -> None:
    ajustes = settings_prueba.con(conectoma_fuente="stub")

    async def principal() -> tuple[str, list[Any]]:
        orq, salida = _orquestador(ajustes)
        try:
            diags = await orq.iniciar(lanzar_tareas=False, imprimir_arranque=True)
            return salida.getvalue(), diags
        finally:
            await orq.apagar()

    texto, diags = asyncio.run(principal())
    assert "A M A D E U S   ·   R.A.M.S. BIO-CORE  v0.1.0" in texto
    assert "VIKTOR CHONDRIA UNIVERSITY" in texto
    assert "Montando almacén de memoria" in texto and "john/" in texto
    assert "último registro heredado: ninguno (ejecuta python -m core.wsp_ingestor)" in texto
    assert "último registro vivido:   ninguno" in texto
    assert "0.571024 → 1.129848 → " in texto
    assert "Área de Broca (Gemini)" in texto and "OFF" in texto
    assert "Llamada entrante: Amadeus" in texto
    assert "[Amadeus] › " in texto                       # saludo inicial
    assert "?t=" not in texto                            # sin TTY no se imprime el token del HUD
    claves = {d.clave for d in diags}
    assert {"memoria", "conectoma", "broca", "voz", "telegram"} <= claves
    for linea in texto.splitlines():
        if linea.startswith("[") and "] " in linea and "Amadeus] ›" not in linea:
            assert linea[1:3].isdigit() and linea[3] == "." and linea[7] == "]", linea


def test_correr_ciclo_completo(settings_prueba: Settings) -> None:
    ajustes = settings_prueba.con(conectoma_fuente="stub")

    async def principal() -> tuple[int, str, list[str]]:
        orq, salida = _orquestador(ajustes)
        tarea = asyncio.create_task(orq.correr())
        assert await _esperar(lambda: "presencia" in orq._tareas, 15.0)
        estados: list[float] = []
        orq.bus.suscribir("homeostasis.estado", lambda e: estados.append(e.ts))
        await asyncio.sleep(0.8)
        orq._motivo_parada = "usuario"
        orq.parar.set()
        codigo = await asyncio.wait_for(tarea, timeout=20.0)
        return codigo, salida.getvalue(), sorted(orq._tareas)

    codigo, texto, tareas = asyncio.run(principal())
    assert codigo == 0
    assert {"conectoma", "terminal", "salida", "salieri", "agenda", "presencia"} <= set(tareas)
    assert "vts" not in tareas and "telegram_entrada" not in tareas
    assert "Llamada entrante: Amadeus" in texto
    assert "Fin de la sesión." in texto


# ---------------------------------------------------------------------------
# Regreso, iniciativas y alertas
# ---------------------------------------------------------------------------


def test_regreso_con_llamadas_perdidas(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> tuple[list[ContextoBroca], bool, Any]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            contextos = _espiar_broca(monkeypatch, orq)
            for motivo in ("conversar", "descanso"):
                await orq.memoria.registrar_llamada(origen="iniciativa", canal="telegram", nivel=1,
                                                    motivo=motivo, texto="¿Sigues ahí?")
            orq._ausente_desde_ts = time.time() - 3600
            orq._regreso_pendiente = True
            await orq.procesar_entrada("ya volví")
            await orq.procesar_entrada("¿y ahora?")          # el regreso se menciona una sola vez
            return contextos, orq._regreso_pendiente, orq._ausente_desde_ts
        finally:
            await orq.apagar()

    contextos, pendiente, ausente_desde = asyncio.run(principal())
    assert contextos[0].evento_social == "regreso"
    assert contextos[0].llamadas_perdidas == 2
    assert contextos[1].evento_social != "regreso"
    assert contextos[1].llamadas_perdidas == 0
    assert pendiente is False and ausente_desde is None


def test_curiosidad_visual_sin_camara_se_degrada(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> list[dict[str, Any]]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            orq.presencia.registrar_entrada("terminal")
            orq.presencia.actualizar()
            entregas: list[dict[str, Any]] = []

            async def decir_espia(reply: BrocaReply, **kwargs: Any) -> None:
                entregas.append({"texto": reply.texto_plano, **kwargs})

            monkeypatch.setattr(orq.riko, "decir", decir_espia)
            estado = HomeostaticState(curiosidad=0.8, iniciativa="curiosidad_visual",
                                      motivo_iniciativa="curiosidad 0,80 sin estímulos")
            await orq.bus.emitir("homeostasis.iniciativa", {
                "tipo": "curiosidad_visual", "motivo": estado.motivo_iniciativa, "estado": estado.a_dict(),
                "antes": None, "despues": None})
            await asyncio.sleep(0.05)
            return entregas
        finally:
            await orq.apagar()

    entregas = asyncio.run(principal())
    assert entregas, "la iniciativa debió entregarse por la terminal"
    assert entregas[0]["origen"] == "iniciativa"
    assert entregas[0]["tipo_iniciativa"] == "conversar"     # sin cámara no hay vistazo
    assert entregas[0]["canal"] == "terminal" and entregas[0]["prioridad"] == 1
    assert entregas[0]["texto"]


def test_alerta_critica_sin_llm_y_contexto(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def principal() -> tuple[list[dict[str, Any]], list[ContextoBroca]]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            entregas: list[dict[str, Any]] = []

            async def decir_espia(reply: BrocaReply, **kwargs: Any) -> None:
                entregas.append({"texto": reply.texto_plano, **kwargs})

            monkeypatch.setattr(orq.riko, "decir", decir_espia)
            contextos = _espiar_broca(monkeypatch, orq)
            await orq.bus.emitir("pc.alerta", {"tipo": "temperatura_cpu", "valor": 94.0, "umbral": 90.0,
                                               "critica": True,
                                               "detalle": "CPU a 94 °C sostenidos 9 s; proceso ffmpeg (PID 48211)"})
            alerta_sin_llm = list(contextos)
            await orq.procesar_entrada("¿qué pasa?")
            return entregas, alerta_sin_llm + contextos[len(alerta_sin_llm):]
        finally:
            await orq.apagar()

    entregas, contextos = asyncio.run(principal())
    alerta = entregas[0]
    assert alerta["origen"] == "alerta" and alerta["prioridad"] == 2 and alerta["motivo"] == "temperatura_cpu"
    assert "94" in alerta["texto"]
    assert len(contextos) == 1                       # la alerta no usó Broca.verbalizar
    assert contextos[0].alerta_critica is True
    assert "94 °C" in (contextos[0].instruccion_extra or "")


# ---------------------------------------------------------------------------
# Unidades sin otros módulos: consola, canal, supervisión, control
# ---------------------------------------------------------------------------


def test_consola_formatos(consola_capturada: Any) -> None:
    import main

    consola = consola_capturada
    assert consola.es_tty is False
    consola.imprimir_amadeus("Hola.", Etiquetas("smug", "asentir", "sarcastico"))
    consola.imprimir_sistema("⚠ Confirmación requerida: cerrar kitty. ¿Procedo? [s/N]")
    consola.indicador_camara(True)
    consola.indicador_camara(False, 312)
    consola.linea_arranque(0.012, "Montando almacén de memoria", "john/  OK", EstadoModulo.OK)
    asyncio.run(consola.rueda_nixie("Medidor: ", ["0.571024", "1.129848", "1.048596"]))
    texto = consola.salida.getvalue()
    assert "[Amadeus] › Hola." in texto
    assert "⚠ Confirmación requerida: cerrar kitty. ¿Procedo? [s/N]" in texto
    assert "● CÁMARA ACTIVA" in texto and "○ cámara apagada (312 ms)" in texto
    esperado = "[00.012] " + "Montando almacén de memoria ".ljust(44, ".") + " john/  OK"
    assert esperado in texto
    assert "Medidor: 0.571024 → 1.129848 → 1.048596" in texto
    assert "\x1b[" not in texto                  # sin TTY no hay secuencias ANSI
    assert consola.prompt_usuario == "[Lab Mem 001] › "
    assert main.Consola.PROMPT_AMADEUS == "[Amadeus] › "


class _PresenciaFalsa:
    def __init__(self, presente: bool, confianza: float = 0.95) -> None:
        self._estado = EstadoPresencia(presente=presente, confianza=confianza, motivo="prueba")

    def estado(self) -> EstadoPresencia:
        return self._estado


class _RikoFalso:
    def __init__(self, voz: bool) -> None:
        self.voz_disponible = voz


def test_eleccion_de_canal(settings_prueba: Settings, settings_con_claves: Settings) -> None:
    import main

    orq, _ = _orquestador(settings_prueba)
    orq.presencia = _PresenciaFalsa(True)
    orq.riko = _RikoFalso(True)
    charla = Intencion("ninguna")
    tecnica = Intencion("ninguna", explicacion_tecnica=True)
    assert orq._modo_salida_respuesta("telegram", charla) == "telegram"
    assert orq._modo_salida_respuesta("nexus", charla) == "terminal"
    assert orq._modo_salida_respuesta("terminal", tecnica) == "terminal_largo"
    assert orq._modo_salida_respuesta("terminal", charla) == "voz"
    assert main.CANAL_DE_MODO.get("terminal_largo", "terminal") == "terminal"
    orq.riko = _RikoFalso(False)
    assert orq._modo_salida_respuesta("terminal", charla) == "terminal"
    assert orq._modo_salida_proactivo() == "terminal"
    orq.presencia = _PresenciaFalsa(False, 0.85)
    assert orq._modo_salida_proactivo() == "terminal"          # offline: sin Telegram
    con_telegram, _ = _orquestador(settings_con_claves)
    con_telegram.presencia = _PresenciaFalsa(False, 0.85)
    con_telegram.riko = _RikoFalso(True)
    assert con_telegram._modo_salida_proactivo() == "telegram"
    con_telegram.presencia = _PresenciaFalsa(True)
    assert con_telegram._modo_salida_proactivo() == "voz"


def test_supervisar_reintenta_y_desactiva() -> None:
    import main

    async def principal() -> tuple[int, int, list[dict[str, Any]], int]:
        parar = asyncio.Event()
        bus = BusEventos()
        bus.vincular_loop()
        modulos: list[dict[str, Any]] = []
        bus.suscribir("sistema.modulo", lambda e: modulos.append(e.datos))
        intentos = {"n": 0}

        async def siempre_falla() -> None:
            intentos["n"] += 1
            raise RuntimeError("fallo simulado")

        await main.supervisar("telemetria", siempre_falla, parar, reintentos=3, backoff_s=0.01,
                              backoff_max_s=0.05, bus=bus)
        fallidos = intentos["n"]

        intentos_bien = {"n": 0}

        async def falla_una_vez() -> None:
            intentos_bien["n"] += 1
            if intentos_bien["n"] == 1:
                raise RuntimeError("fallo transitorio")

        await main.supervisar("agenda", falla_una_vez, parar, reintentos=3, backoff_s=0.01)

        parar.set()
        intentos_parado = {"n": 0}

        async def nunca() -> None:
            intentos_parado["n"] += 1

        await main.supervisar("presencia", nunca, parar)
        await bus.cerrar()
        return fallidos, intentos_bien["n"], modulos, intentos_parado["n"]

    fallidos, recuperados, modulos, con_parar = asyncio.run(principal())
    assert fallidos == 3
    assert recuperados == 2
    assert modulos and modulos[-1]["clave"] == "telemetria" and modulos[-1]["estado"] == "DEGRADADO"
    assert con_parar == 0


def test_despachar_control_sin_modulos(settings_prueba: Settings) -> None:
    async def principal() -> list[tuple[bool, str]]:
        orq, _ = _orquestador(settings_prueba)
        return [
            await orq._despachar_control("memoria_buscar", {"consulta": "pizza"}),
            await orq._despachar_control("investigar_web", {"consulta": "clave " + "A" * 30}),
            await orq._despachar_control("borrar_todo", {}),
        ]

    memoria, web, otra = asyncio.run(principal())
    assert memoria == (False, "La memoria de Amadeus no está expuesta a Nexus (RAMS_MCP_MEMORIA=0).")
    assert web == (False, "Consulta rechazada: parece contener código o claves.")
    assert otra == (False, "herramienta no permitida")


def test_latido_publica_iniciativa_y_cambio_de_linea(settings_prueba: Settings) -> None:
    async def principal() -> dict[str, list[dict[str, Any]]]:
        orq, _ = _orquestador(settings_prueba)
        orq.bus.vincular_loop()
        vistos: dict[str, list[dict[str, Any]]] = {}
        for nombre in ("homeostasis.estado", "homeostasis.iniciativa", "homeostasis.cambio_linea"):
            orq.bus.suscribir(nombre, lambda e: vistos.setdefault(e.nombre, []).append(e.datos))
        orq._ultimo_campo = "steins_gate"
        orq._al_latir(HomeostaticState(campo="steins_gate"))
        orq._al_latir(HomeostaticState(campo="beta", iniciativa="cambio_linea", motivo_iniciativa="x",
                                       linea_antes="steins_gate", linea_despues="beta",
                                       bump=tuple([0.5] * 16)))
        return vistos

    vistos = asyncio.run(principal())
    assert len(vistos["homeostasis.estado"]) == 2
    assert vistos["homeostasis.estado"][1]["bump"] == [0.5] * 16
    iniciativa = vistos["homeostasis.iniciativa"][0]
    assert iniciativa["tipo"] == "cambio_linea"
    assert (iniciativa["antes"], iniciativa["despues"]) == ("steins_gate", "beta")
    assert vistos["homeostasis.cambio_linea"] == [{"antes": "steins_gate", "despues": "beta",
                                                   "divergencia": 1.048596}]


def test_formatos_auxiliares() -> None:
    import main
    from datetime import datetime, timezone

    assert main.formatear_duracion(3 * 3600 + 12 * 60) == "3 h 12 min"
    assert main.formatear_duracion(45) == "45 s"
    assert main.formatear_duracion_corta(312) == "5 min 12 s"
    momento = datetime(2026, 10, 16, 22, 41, tzinfo=timezone.utc)
    assert main.fecha_larga(momento) == "16 de octubre de 2026"
    assert main.franja_horaria(momento.replace(hour=6)) == "manana"
    assert main.franja_horaria(momento.replace(hour=13)) == "tarde"
    assert main.franja_horaria(momento.replace(hour=22)) == "noche"
    assert main.franja_horaria(momento.replace(hour=3)) == "madrugada"
    ahora = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
    assert main.describir_vencimiento(ahora.replace(hour=18), ahora) == "hoy a las 18:00"


def test_recordatorio_sin_mensaje_pide_el_mensaje(settings_prueba: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """La orden "recuérdame a las 6 pm" no programa "pm" a las 06:00: pide el mensaje (§10.12, cortes)."""
    async def principal() -> list[ContextoBroca]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            contextos = _espiar_broca(monkeypatch, orq)
            await orq.procesar_entrada("recuérdame a las 6 pm")
            await orq.procesar_entrada("recuérdame que en 20 minutos saque la ropa")
            await orq.procesar_entrada("recuérdame comprar pan")
            return contextos
        finally:
            await orq.apagar()

    sin_mensaje, con_que, sin_hora = asyncio.run(principal())
    assert sin_mensaje.accion is not None and sin_mensaje.accion.ok is False
    assert sin_mensaje.accion.mensaje.startswith("Falta el mensaje")
    assert "18:00" in sin_mensaje.accion.mensaje
    assert con_que.accion is not None and con_que.accion.ok is True
    assert con_que.accion.mensaje.endswith(": saque la ropa.")
    assert sin_hora.accion is not None and sin_hora.accion.ok is False
    assert sin_hora.accion.mensaje.startswith("Falta la hora")


def test_recordatorio_a_medias_se_completa_en_el_turno_siguiente(settings_prueba: Settings,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """«recuérdame comprar pan» → «mañana a las 6 pm» programa el recordatorio (CONTRATO de cierre).

    En modo no verbal la pregunta sale del banco local (nunca la nota «pregunta para cuándo…»
    escrita para el LLM) y una respuesta que no trae la hora cancela el pendiente y lo dice.
    """
    async def principal() -> tuple[Any, ...]:
        orq, salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            contextos = _espiar_broca(monkeypatch, orq)
            pregunta = await orq.procesar_entrada("recuérdame comprar pan")
            assert orq._programar_pendiente is not None
            confirmacion = await orq.procesar_entrada("mañana a las 6 pm")
            assert orq._programar_pendiente is None
            agenda = await orq.memoria.listar_agenda()
            await orq.procesar_entrada("recuérdame pagar la luz")
            no_entendido = await orq.procesar_entrada("uff, no sé")
            agenda_final = await orq.memoria.listar_agenda()
            await orq.riko.vaciar(2.0)
            return pregunta, confirmacion, agenda, no_entendido, agenda_final, contextos, salida.getvalue()
        finally:
            await orq.apagar()

    pregunta, confirmacion, agenda, no_entendido, agenda_final, contextos, salida = asyncio.run(principal())
    assert pregunta is not None and "comprar pan" in pregunta.texto_plano and "?" in pregunta.texto_plano
    assert "pregunta" not in pregunta.texto_plano.lower()          # nunca la nota para el LLM
    assert contextos[1].accion is not None and contextos[1].accion.ok is True
    assert "18:00" in contextos[1].accion.mensaje and "comprar pan" in contextos[1].accion.mensaje
    assert [(i.tipo, i.mensaje) for i in agenda] == [("recordatorio", "comprar pan")]
    assert confirmacion is not None and "comprar pan" in confirmacion.texto_plano
    # Respuesta sin hora: se cancela y se dice; la agenda no cambia.
    assert contextos[3].accion is not None and contextos[3].accion.ok is False
    assert contextos[3].accion.datos.get("cancelado") is True
    assert no_entendido is not None and "pagar la luz" in no_entendido.texto_plano
    assert len(agenda_final) == 1
    assert "Falta la hora" not in salida


def test_recordatorio_a_medias_caduca_y_respeta_el_canal(settings_prueba: Settings,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """El pendiente solo lo completa el mismo canal y dentro de su plazo; otra petición lo descarta."""
    async def principal() -> tuple[list[ContextoBroca], list[Any]]:
        orq, _salida = _orquestador(settings_prueba)
        try:
            await orq.iniciar(lanzar_tareas=False, imprimir_arranque=False)
            contextos = _espiar_broca(monkeypatch, orq)
            await orq.procesar_entrada("recuérdame regar las plantas")
            await orq.procesar_entrada("a las 6 pm", canal="telegram")       # otro canal: no lo toca
            assert orq._programar_pendiente is not None
            orq._programar_pendiente["expira"] = 0.0                          # plazo vencido
            await orq.procesar_entrada("a las 6 pm")
            assert orq._programar_pendiente is None
            await orq.procesar_entrada("recuérdame sacar al perro")
            await orq.procesar_entrada("/estado")                             # un comando no lo consume
            assert orq._programar_pendiente is not None
            await orq.procesar_entrada("¿cómo está el sistema? dame un reporte")   # otra intención
            assert orq._programar_pendiente is None
            return contextos, await orq.memoria.listar_agenda()
        finally:
            await orq.apagar()

    contextos, agenda = asyncio.run(principal())
    assert agenda == []
    assert contextos[1].accion is None and contextos[2].accion is None
    assert contextos[-1].accion is None


def test_selftest_no_deja_datos_en_el_proyecto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main.py --selftest`` trabaja en un temporal: no crea el ``data/`` configurado ni ``rams.log``."""
    import sys

    import main

    for carpeta in ("persona", "interface"):
        if not (RAIZ / carpeta).is_dir():
            pytest.skip(f"falta {carpeta}/ (lo entrega otro grupo)")
    destino = tmp_path / "datos_configurados"
    monkeypatch.setenv("RAMS_DATA_DIR", str(destino))
    monkeypatch.setenv("RAMS_CTL_TOKEN_FILE", str(tmp_path / "ctl_token"))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert main.main(["--selftest", "--env", str(tmp_path / "no_existe.env")]) == 0
    assert not destino.exists()
    assert not (tmp_path / "ctl_token").exists()


def test_reconfigurar_stdin_no_tty_a_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con stdin por tubería (no TTY) se lee en UTF-8 aunque la página de códigos local sea cp1252."""
    import sys

    import main

    entrada = io.TextIOWrapper(io.BytesIO("recuérdame llamar a mamá\n".encode("utf-8")), encoding="cp1252")
    monkeypatch.setattr(sys, "stdin", entrada)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    main._reconfigurar_salida()
    assert entrada.encoding.lower().replace("-", "") == "utf8"
    assert entrada.readline() == "recuérdame llamar a mamá\n"
