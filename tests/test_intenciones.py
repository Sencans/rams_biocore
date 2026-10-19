"""Tests del enrutador local de intenciones y del detector de eventos sociales (§10.12).

Lógica pura: sin red, sin hardware y con un ``ahora`` fijo *aware* (America/Bogota).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.contracts import DESTINOS_BASE, AccionPC
from core.intenciones import (
    ACCIONES_TELEGRAM,
    COMANDOS,
    COMANDOS_POR_CANAL,
    FUENTES_FOTO,
    INTENCIONES_POR_CANAL,
    MENSAJE_BUSCAR_DEFECTO,
    MENSAJE_SOLO_LOCAL,
    OPS_INTENCION,
    OPS_POR_CANAL,
    TIPOS_INTENCION,
    Intencion,
    acepta_oferta,
    completar_programar,
    detectar_evento_social,
    enrutar,
    normalizar,
    parsear_comando,
    parsear_cuando,
    permitida_en_canal,
    pide_tarea_pc,
    quiere_audio,
    quiere_cancelar,
)

try:
    from zoneinfo import ZoneInfo

    ZONA = ZoneInfo("America/Bogota")
except Exception:  # sin tzdata: UTC−5 fijo (Bogotá no tiene horario de verano)
    ZONA = timezone(timedelta(hours=-5), "COT")

AHORA = datetime(2026, 10, 16, 10, 0, tzinfo=ZONA)      # jueves 16 de octubre de 2026, 10:00
APPS = {"terminal": "kitty", "navegador": "firefox", "nexus": "nexus-ide"}


def _ruta(texto: str, canal: str = "terminal") -> Intencion:
    return enrutar(texto, ahora=AHORA, apps=APPS, canal=canal)


# ---------------------------------------------------------------------------
# Normalización y comandos
# ---------------------------------------------------------------------------


def test_normalizar() -> None:
    assert normalizar("  ¿Qué   PASÓ con  el Año? ¡Mañana! ") == "que paso con el ano? manana!"
    assert normalizar("Pingüino") == "pinguino"


def test_comando_memoria() -> None:
    intencion = _ruta("/memoria pizza")
    assert intencion.tipo == "comando"
    assert intencion.args == {"comando": "/memoria", "resto": "pizza"}


def test_parsear_comando() -> None:
    assert parsear_comando("/buscar clima en Bogotá") == ("/buscar", "clima en Bogotá")
    assert parsear_comando("/estado@MiBot") == ("/estado", "")
    assert parsear_comando("/inventado x") is None
    assert parsear_comando("hola") is None
    assert "/hud" in COMANDOS and "/logro" in COMANDOS


# ---------------------------------------------------------------------------
# Tabla de reglas
# ---------------------------------------------------------------------------


def test_buscar_web_con_consulta_del_texto_original() -> None:
    intencion = _ruta("busca precio del dólar hoy")
    assert intencion.tipo == "buscar_web"
    assert intencion.args["consulta"] == "precio del dólar hoy"


def test_buscar_web_debil() -> None:
    intencion = _ruta("¿cuáles son las últimas noticias de la NASA?")
    assert intencion.tipo == "buscar_web"
    assert intencion.confianza == pytest.approx(0.6)
    assert intencion.args["consulta"] == "¿cuáles son las últimas noticias de la NASA?"


def test_programar_a_las_6_pm() -> None:
    intencion = _ruta("recuérdame llamar a mamá a las 6 pm")
    assert intencion.tipo == "programar"
    vence = intencion.args["vence"]
    assert (vence.hour, vence.minute) == (18, 0)
    assert vence.date() in (AHORA.date(), AHORA.date() + timedelta(days=1))
    assert vence.tzinfo is not None
    assert intencion.args["mensaje"] == "llamar a mamá"
    assert intencion.args["tipo"] == "recordatorio"


def test_recordar() -> None:
    intencion = _ruta("recuerda que odio el cilantro")
    assert intencion.tipo == "recordar"
    assert intencion.args["contenido"] == "odio el cilantro"


def test_lanzar_app_permitida() -> None:
    intencion = _ruta("abre terminal")
    assert intencion.tipo == "accion_pc"
    accion = intencion.args["accion"]
    assert isinstance(accion, AccionPC)
    assert accion.tipo == "lanzar_app" and accion.args == {"alias": "terminal"}
    assert accion.origen == "usuario"


def test_app_desconocida_es_ninguna() -> None:
    assert _ruta("abre bitcoinminer").tipo == "ninguna"


def test_cerrar_ventana_sin_objetivo() -> None:
    intencion = _ruta("cierra la ventana")
    assert intencion.tipo == "accion_pc"
    accion = intencion.args["accion"]
    assert accion.tipo == "cerrar_ventana"
    assert dict(accion.args) == {}
    assert accion.origen == "usuario"


def test_todas_las_acciones_tienen_origen_usuario() -> None:
    for texto in ("abre navegador", "ve al espacio 3", "trae nexus", "cierra esta ventana", "pausa",
                  "siguiente canción", "volumen al 40%"):
        intencion = _ruta(texto)
        assert intencion.tipo == "accion_pc", texto
        assert intencion.args["accion"].origen == "usuario", texto


def test_workspace_volumen_y_nexus() -> None:
    assert _ruta("ve al espacio 3").args["accion"].args == {"n": 3}
    assert _ruta("volumen al 40%").args["accion"].args == {"nivel": 40}
    assert _ruta("muéstrame nexus").args["accion"].tipo == "enfocar_nexus"


def test_media_anclada() -> None:
    assert _ruta("voy a tomar una pausa").tipo == "ninguna"
    intencion = _ruta("pausa")
    assert intencion.tipo == "accion_pc"
    assert intencion.args["accion"].tipo == "media"
    assert intencion.args["accion"].args == {"orden": "play_pause"}
    assert _ruta("canción anterior").args["accion"].args == {"orden": "anterior"}


def test_vistazo_estoy_aqui() -> None:
    assert _ruta("¿estoy aquí?").tipo == "vistazo"
    assert _ruta("¿me ves?").tipo == "vistazo"


def test_reporte_y_pantalla() -> None:
    assert _ruta("dame un reporte del sistema").tipo == "reporte_sistema"
    assert _ruta("mira mi pantalla").tipo == "mirar_pantalla"


def test_memoria_consulta() -> None:
    intencion = _ruta("¿qué recuerdas de mi proyecto de tesis?")
    assert intencion.tipo == "memoria_consulta"
    assert intencion.args["consulta"] == "mi proyecto de tesis"


def test_explicacion_tecnica() -> None:
    larga = _ruta("explícame cómo funciona el conectoma de la mosca y por qué usa neuronas LIF")
    assert larga.explicacion_tecnica is True
    assert _ruta("cómo funciona eso").explicacion_tecnica is False   # 3 palabras


# ---------------------------------------------------------------------------
# parsear_cuando y algoritmo de cortes
# ---------------------------------------------------------------------------


def test_parsear_cuando_relativo_y_entero() -> None:
    assert parsear_cuando("en 20 minutos", AHORA) == AHORA + timedelta(minutes=20)
    assert parsear_cuando("el correo en 20 minutos", AHORA) is None
    assert parsear_cuando("en una hora", AHORA) == AHORA + timedelta(hours=1)
    assert parsear_cuando("en media hora", AHORA) == AHORA + timedelta(minutes=30)


def test_parsear_cuando_horas_y_dias() -> None:
    tarde = parsear_cuando("a las 6 p. m.", AHORA)
    assert tarde is not None and (tarde.date(), tarde.hour) == (AHORA.date(), 18)
    pasada = parsear_cuando("a las 7", AHORA)            # 07:00 ya pasó a las 10:00 ⇒ mañana
    assert pasada is not None and pasada.date() == AHORA.date() + timedelta(days=1) and pasada.hour == 7
    manana = parsear_cuando("mañana", AHORA)
    assert manana is not None and (manana.date(), manana.hour, manana.minute) == (
        AHORA.date() + timedelta(days=1), 9, 0)
    pasado = parsear_cuando("pasado mañana a las 8:30 am", AHORA)
    assert pasado is not None and (pasado.date(), pasado.hour, pasado.minute) == (
        AHORA.date() + timedelta(days=2), 8, 30)
    iso = parsear_cuando("el 2026-12-24 a las 20:00", AHORA)
    assert iso is not None and (iso.year, iso.month, iso.day, iso.hour) == (2026, 12, 24, 20)
    dm = parsear_cuando("el 01/02", AHORA)               # sin año y ya pasado ⇒ el año siguiente
    assert dm is not None and (dm.year, dm.month, dm.day) == (2027, 2, 1)
    assert parsear_cuando("a las 25", AHORA) is None
    assert parsear_cuando("el 2020-01-01", AHORA) is None
    assert parsear_cuando("cuando puedas", AHORA) is None


def test_programar_corte_sufijo() -> None:
    intencion = _ruta("recuérdame enviar el correo en 20 minutos")
    assert intencion.tipo == "programar"
    assert intencion.args["mensaje"] == "enviar el correo"
    assert intencion.args["vence"] == AHORA + timedelta(minutes=20)


def test_programar_corte_prefijo() -> None:
    intencion = _ruta("recuérdame mañana a las 8 llamar al banco")
    assert intencion.tipo == "programar"
    assert intencion.args["mensaje"] == "llamar al banco"
    vence = intencion.args["vence"]
    assert (vence.date(), vence.hour, vence.minute) == (AHORA.date() + timedelta(days=1), 8, 0)


def test_programar_incompleto() -> None:
    intencion = _ruta("recuérdame comprar pan")
    assert intencion.tipo == "programar_incompleto"
    assert intencion.args == {"mensaje": "comprar pan", "tipo": "recordatorio"}


@pytest.mark.parametrize("texto, esperado", [
    ("recuérdame a las 6 pm", (AHORA.date(), 18, 0)),
    ("recuérdame a las 6 p. m.", (AHORA.date(), 18, 0)),
    ("recuérdame pasado mañana a las 7", (AHORA.date() + timedelta(days=2), 7, 0)),
    ("recuérdame el 24/12 a las 8", (datetime(2026, 12, 24).date(), 8, 0)),
    ("recuérdame en 20 minutos", (AHORA.date(), 10, 20)),
    ("recuérdame que mañana", (AHORA.date() + timedelta(days=1), 9, 0)),
    ("recuérdame a las 6 pm -", (AHORA.date(), 18, 0)),
])
def test_programar_solo_hora_pide_el_mensaje(texto: str, esperado: tuple[object, int, int]) -> None:
    """Si todo el resto es la hora, no se parte la hora para inventar un mensaje ("pm", "pasado")."""
    intencion = _ruta(texto)
    assert intencion.tipo == "programar_incompleto", intencion
    assert intencion.args["falta"] == "mensaje"
    assert intencion.args["mensaje"] == ""
    vence = intencion.args["vence"]
    assert (vence.date(), vence.hour, vence.minute) == esperado


@pytest.mark.parametrize("texto, mensaje, esperado", [
    ("recuérdame a las 6 pm llamar a mamá", "llamar a mamá", (AHORA.date(), 18, 0)),
    ("recuérdame a las 6 p. m. llamar a mamá", "llamar a mamá", (AHORA.date(), 18, 0)),
    ("recuérdame pasado mañana a las 7 ir al médico", "ir al médico", (AHORA.date() + timedelta(days=2), 7, 0)),
    ("recuérdame llamar a mamá pasado mañana a las 7", "llamar a mamá", (AHORA.date() + timedelta(days=2), 7, 0)),
    ("recuérdame el 24/12 a las 8 comprar regalos", "comprar regalos", (datetime(2026, 12, 24).date(), 8, 0)),
    ("recuérdame comprar regalos el 24/12 a las 8", "comprar regalos", (datetime(2026, 12, 24).date(), 8, 0)),
    ("recuérdame a las 6 de la tarde llamar a mamá", "llamar a mamá", (AHORA.date(), 18, 0)),
])
def test_programar_coincidencia_mas_larga(texto: str, mensaje: str, esperado: tuple[object, int, int]) -> None:
    intencion = _ruta(texto)
    assert intencion.tipo == "programar", intencion
    assert intencion.args["mensaje"] == mensaje
    vence = intencion.args["vence"]
    assert (vence.date(), vence.hour, vence.minute) == esperado


@pytest.mark.parametrize("texto, mensaje, esperado", [
    ("recuérdame que mañana a las 8 llame al banco", "llame al banco", (AHORA.date() + timedelta(days=1), 8, 0)),
    ("recuérdame que a las 6 pm llame a mamá", "llame a mamá", (AHORA.date(), 18, 0)),
    ("avísame que en 20 minutos saque la ropa", "saque la ropa", (AHORA.date(), 10, 20)),
    ("recuérdame que llame a mamá en 1 hora", "llame a mamá", (AHORA.date(), 11, 0)),
])
def test_programar_con_que_inicial(texto: str, mensaje: str, esperado: tuple[object, int, int]) -> None:
    intencion = _ruta(texto)
    assert intencion.tipo == "programar", intencion
    assert intencion.args["mensaje"] == mensaje
    vence = intencion.args["vence"]
    assert (vence.date(), vence.hour, vence.minute) == esperado


def test_programar_incompleto_sin_hora_no_marca_falta_mensaje() -> None:
    intencion = _ruta("recuérdame que compre pan")
    assert intencion.tipo == "programar_incompleto"
    assert intencion.args == {"mensaje": "compre pan", "tipo": "recordatorio"}


def test_parsear_cuando_fecha_de_hoy_con_hora_pasada() -> None:
    # Hoy (16/10) a una hora ya pasada no es "el año que viene": None, igual que la forma ISO.
    assert parsear_cuando("el 16/10 a las 9", AHORA) is None
    assert parsear_cuando("el 16/10", AHORA) is None                 # 09:00 por defecto ya pasó
    assert parsear_cuando("el 2026-10-16 a las 9", AHORA) is None
    hoy_mas_tarde = parsear_cuando("el 16/10 a las 11", AHORA)
    assert hoy_mas_tarde is not None and (hoy_mas_tarde.date(), hoy_mas_tarde.hour) == (AHORA.date(), 11)
    ayer = parsear_cuando("el 15/10", AHORA)                          # el día ya pasó ⇒ año siguiente
    assert ayer is not None and (ayer.year, ayer.month, ayer.day) == (2027, 10, 15)
    assert parsear_cuando("el 15/10/2026", AHORA) is None              # con año explícito: None
    # Una fecha reconocida pero vencida no se recorta a "a las 9" (mañana) con "pagar el 16/10" de mensaje.
    for texto in ("recuérdame pagar el 16/10 a las 9", "recuérdame ir el 2026-10-15 a las 8",
                  "recuérdame el 16/10 a las 9 pagar"):
        intencion = _ruta(texto)
        assert intencion.tipo == "programar_incompleto", texto
        assert "falta" not in intencion.args, texto


def test_capsula_del_tiempo() -> None:
    intencion = _ruta("cápsula del tiempo: no te rindas con la tesis, para el 2027-01-01")
    assert intencion.tipo == "programar"
    assert intencion.args["tipo"] == "capsula"
    assert intencion.args["mensaje"] == "no te rindas con la tesis"
    assert intencion.args["vence"].year == 2027
    incompleta = _ruta("guarda para el futuro: sigue así")
    assert incompleta.tipo == "programar_incompleto" and incompleta.args["tipo"] == "capsula"
    solo_fecha = _ruta("cápsula del tiempo: para el 2027-01-01")      # no inventa el mensaje "para"
    assert solo_fecha.tipo == "programar_incompleto" and solo_fecha.args["falta"] == "mensaje"


# ---------------------------------------------------------------------------
# Capacidades por canal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("texto", ["abre terminal", "me ves", "mira mi pantalla", "/camara on", "/olvidar 3",
                                   "/salir"])
def test_telegram_bloquea_lo_local(texto: str) -> None:
    intencion = _ruta(texto, canal="telegram")
    assert intencion.tipo == "bloqueada_canal"
    assert "tipo_original" in intencion.args
    assert MENSAJE_SOLO_LOCAL == "Eso solo se puede pedir desde el computador."


def test_telegram_bloqueada_conserva_el_origen() -> None:
    comando = _ruta("/camara on", canal="telegram")
    assert comando.args == {"tipo_original": "comando", "comando": "/camara"}
    accion = _ruta("abre terminal", canal="telegram")
    assert accion.args == {"tipo_original": "accion_pc", "comando": None}


def test_telegram_permite_lo_remoto() -> None:
    estado = _ruta("/estado", canal="telegram")
    assert estado.tipo == "comando" and estado.args["comando"] == "/estado"
    assert _ruta("busca clima bogotá", canal="telegram").tipo == "buscar_web"
    pagar = _ruta("recuérdame pagar en 1 hora", canal="telegram")
    assert pagar.tipo == "programar"
    assert pagar.args["vence"] == AHORA + timedelta(hours=1)
    assert _ruta("recuérdame comprar pan", canal="telegram").tipo == "programar_incompleto"


def test_canal_desconocido_se_trata_como_nexus() -> None:
    assert _ruta("busca algo", canal="desconocido").tipo == "bloqueada_canal"
    assert _ruta("hola", canal="nexus").tipo == "ninguna"


def test_permitida_en_canal() -> None:
    assert permitida_en_canal(Intencion("vistazo"), "terminal") is True
    assert permitida_en_canal(Intencion("vistazo"), "telegram") is False
    assert permitida_en_canal(Intencion("comando", {"comando": "/estado"}), "telegram") is True
    assert permitida_en_canal(Intencion("comando", {"comando": "/hud"}), "telegram") is False
    assert permitida_en_canal(Intencion("comando", {"comando": "/estado"}), "nexus") is False
    assert "accion_pc" not in INTENCIONES_POR_CANAL["telegram"]
    assert "/hud" not in COMANDOS_POR_CANAL["telegram"] and "/logro" not in COMANDOS_POR_CANAL["telegram"]


# ---------------------------------------------------------------------------
# Eventos sociales (§4.4.4)
# ---------------------------------------------------------------------------


def test_christina_gana_a_saludo() -> None:
    assert detectar_evento_social("Hola Christina") == "apodo:christina"


def test_chuunibyou() -> None:
    assert detectar_evento_social("Soy Hououin Kyouma") == "apodo:chuunibyou"


def test_agradecimiento_y_otros() -> None:
    assert detectar_evento_social("gracias") == "agradecimiento"
    assert detectar_evento_social("hola amadeus") == "saludo"
    assert detectar_evento_social("buen trabajo, gracias") == "cumplido"
    assert detectar_evento_social("eres una tsundere") == "acusacion_tsundere"
    assert detectar_evento_social("chao, me voy a dormir") == "despedida"
    assert detectar_evento_social("me siento sola, Christina") == "tristeza"   # tristeza gana a apodo
    assert detectar_evento_social("¿cuánto es 2 + 2?") is None


# ---------------------------------------------------------------------------
# Recordatorio a medias: la respuesta del turno siguiente (CONTRATO de cierre)
# ---------------------------------------------------------------------------

FALTA_HORA = {"falta": "hora", "tipo": "recordatorio", "mensaje": "comprar pan", "vence": None}


def _completar(texto: str, pendiente: dict, ahora: datetime = AHORA) -> Intencion | None:
    return completar_programar(texto, pendiente, ahora=ahora, intencion=enrutar(texto, ahora=ahora, apps=APPS))


def test_respuestas_sueltas_no_son_intenciones_por_si_mismas() -> None:
    """Sin el pendiente, «mañana a las 6 pm» es ``ninguna``: por eso main guarda el recordatorio a medias."""
    noche = AHORA.replace(hour=22)
    assert enrutar("recuérdame comprar pan", ahora=noche).tipo == "programar_incompleto"
    for texto in ("mañana a las 6 pm", "a las 6 pm", "para mañana a las 8"):
        assert enrutar(texto, ahora=noche).tipo == "ninguna", texto


@pytest.mark.parametrize("texto, esperado", [
    ("mañana a las 6 pm", datetime(2026, 10, 17, 18, 0, tzinfo=ZONA)),
    ("a las 6 pm", datetime(2026, 10, 16, 18, 0, tzinfo=ZONA)),
    ("para mañana a las 8", datetime(2026, 10, 17, 8, 0, tzinfo=ZONA)),
    ("para las 8 pm", datetime(2026, 10, 16, 20, 0, tzinfo=ZONA)),
    ("las 6 pm, por favor", datetime(2026, 10, 16, 18, 0, tzinfo=ZONA)),
    ("que sea en 20 minutos", AHORA + timedelta(minutes=20)),
    ("recuérdamelo mañana", datetime(2026, 10, 17, 9, 0, tzinfo=ZONA)),
    ("uff, quizá en media hora", AHORA + timedelta(minutes=30)),
])
def test_completar_con_la_hora(texto: str, esperado: datetime) -> None:
    intencion = _completar(texto, FALTA_HORA)
    assert intencion is not None and intencion.tipo == "programar", texto
    assert intencion.args["mensaje"] == "comprar pan"       # el mensaje es el del turno anterior
    assert intencion.args["vence"] == esperado
    assert intencion.args["tipo"] == "recordatorio"


def test_completar_hora_vencida_vuelve_a_preguntar() -> None:
    intencion = _completar("el 16/10 a las 9", FALTA_HORA)    # hoy, pero las 9:00 ya pasaron
    assert intencion is not None and intencion.tipo == "programar_incompleto"
    assert intencion.args["mensaje"] == "comprar pan"


def test_completar_sin_hora_o_cancelado_devuelve_none() -> None:
    for texto in ("hmm no sé", "no, olvídalo", "mejor no", "nada"):
        assert _completar(texto, FALTA_HORA) is None, texto
    # Otra intención explícita no completa nada (main descarta el pendiente).
    assert completar_programar("busca el clima", FALTA_HORA, ahora=AHORA,
                               intencion=_ruta("busca el clima")) is None


def test_completar_con_el_mensaje() -> None:
    vence = datetime(2026, 10, 16, 18, 0, tzinfo=ZONA)
    pendiente = {"falta": "mensaje", "tipo": "recordatorio", "mensaje": "", "vence": vence}
    intencion = _completar("que saque la ropa.", pendiente)
    assert intencion is not None and intencion.tipo == "programar"
    assert intencion.args == {"mensaje": "saque la ropa", "vence": vence, "tipo": "recordatorio"}
    # «recuérdame comprar pan» aporta justo el mensaje que faltaba: se combina con la hora pendiente.
    combinada = _completar("recuérdame comprar pan", pendiente)
    assert combinada is not None and combinada.tipo == "programar"
    assert combinada.args["mensaje"] == "comprar pan" and combinada.args["vence"] == vence
    assert _completar("olvídalo", pendiente) is None


def test_completar_combina_la_hora_dicha_como_recuerdame() -> None:
    intencion = _completar("recuérdame a las 7 pm", FALTA_HORA)
    assert intencion is not None and intencion.tipo == "programar"
    assert intencion.args["mensaje"] == "comprar pan"
    assert intencion.args["vence"] == datetime(2026, 10, 16, 19, 0, tzinfo=ZONA)


def test_completar_capsula_conserva_el_tipo() -> None:
    pendiente = {"falta": "hora", "tipo": "capsula", "mensaje": "hola futuro", "vence": None}
    intencion = _completar("el 2027-01-01", pendiente)
    assert intencion is not None and intencion.tipo == "programar"
    assert intencion.args["tipo"] == "capsula" and intencion.args["mensaje"] == "hola futuro"


def test_completar_nunca_lanza() -> None:
    assert completar_programar("a las 6", {"falta": "hora"}, ahora=AHORA) is None     # sin mensaje
    assert completar_programar("a las 6", None, ahora=AHORA) is None                # type: ignore[arg-type]
    assert completar_programar("a las 6", FALTA_HORA, ahora="hoy") is None           # type: ignore[arg-type]


def test_quiere_cancelar() -> None:
    for texto in ("no", "No, olvídalo.", "mejor no", "déjalo así", "cancela el recordatorio", "nada, gracias"):
        assert quiere_cancelar(texto) is True, texto
    for texto in ("", "no sé", "a las 6", "llamar a mamá", "gracias", "no olvides el pan"):
        assert quiere_cancelar(texto) is False, texto


# ---------------------------------------------------------------------------
# v0.2 (docs/COMPANION.md §C3 "Grupo orquestacion"): intenciones del companion
# ---------------------------------------------------------------------------

DESTINOS = {**{clave: clave for clave in DESTINOS_BASE}, "wasa": "whatsapp", "trino": "x"}


def _v02(texto: str, canal: str = "escritorio", **kwargs: object) -> Intencion:
    return enrutar(texto, ahora=AHORA, apps=APPS, canal=canal, destinos=kwargs.pop("destinos", DESTINOS),
                   **kwargs)  # type: ignore[arg-type]


def _accion_de(intencion: Intencion) -> AccionPC:
    assert intencion.tipo == "accion_pc", intencion
    accion = intencion.args["accion"]
    assert isinstance(accion, AccionPC) and accion.origen == "usuario"
    return accion


def test_tipos_y_comandos_v02() -> None:
    for tipo in ("publicar_borrador", "recordatorio_personal", "escritorio_venir", "escritorio_vete",
                 "basta_travesuras", "vision_pausa"):
        assert tipo in TIPOS_INTENCION
    for comando in ("/vision", "/pantalla", "/travesuras"):
        assert comando in COMANDOS and comando not in COMANDOS_POR_CANAL["telegram"]
    assert _v02("/vision pausa 5").args == {"comando": "/vision", "resto": "pausa 5"}
    assert _v02("/travesuras off").args == {"comando": "/travesuras", "resto": "off"}


@pytest.mark.parametrize("texto, alias", [
    ("abre whatsapp", "whatsapp"), ("Abre WhatsApp.", "whatsapp"), ("pon spotify", "spotify"),
    ("lanza el navegador", "navegador"), ("abre twitter", "x"), ("abre wasa", "whatsapp"),
    ("ábreme el correo", "correo"), ("abre trino", "x"),
])
def test_abrir_destino_da_la_clave(texto: str, alias: str) -> None:
    accion = _accion_de(_v02(texto))
    assert accion.tipo == "abrir_destino" and accion.args == {"alias": alias}


def test_precedencia_catalogo_y_rams_apps() -> None:
    apps = {**APPS, "youtube": "firefox", "spotify": "spotify-launcher"}
    # Un alias del catálogo gana a RAMS_APPS.
    accion = _accion_de(enrutar("abre youtube", ahora=AHORA, apps=apps, destinos=DESTINOS, plataforma="linux"))
    assert accion.tipo == "abrir_destino" and accion.args == {"alias": "youtube"}
    # Fuera del catálogo: RAMS_APPS → lanzar_app, solo en Linux.
    linux = _accion_de(enrutar("abre terminal", ahora=AHORA, apps=apps, plataforma="linux"))
    assert linux.tipo == "lanzar_app" and linux.args == {"alias": "terminal"}
    assert enrutar("abre terminal", ahora=AHORA, apps=apps, plataforma="win32").tipo == "ninguna"
    assert _accion_de(enrutar("abre terminal", ahora=AHORA, apps=apps)).tipo == "lanzar_app"   # sin plataforma
    # Ni catálogo ni RAMS_APPS: sigue con las demás reglas ("pon pausa" es multimedia).
    assert _v02("abre bitcoinminer").tipo == "ninguna"
    assert _accion_de(_v02("pon pausa")).tipo == "media"


def test_sinonimos_de_rams_destinos_y_claves_desconocidas() -> None:
    assert _accion_de(_v02("abre wasa")).args["alias"] == "whatsapp"
    sin_sinonimo = enrutar("abre wasa", ahora=AHORA, apps=APPS, destinos={c: c for c in DESTINOS_BASE})
    assert _accion_de(sin_sinonimo).args["alias"] == "whatsapp"          # sinónimo de serie
    # Un sinónimo que apunta fuera del catálogo nunca produce una acción.
    malo = enrutar("abre cosa", ahora=AHORA, apps={}, destinos={"cosa": "no_existe"})
    assert malo.tipo == "ninguna"


def test_buscar_en() -> None:
    accion = _accion_de(_v02("busca gatos graciosos en YouTube"))
    assert accion.tipo == "buscar_en" and accion.args == {"alias": "youtube", "consulta": "gatos graciosos"}
    spotify = _accion_de(_v02("pon Despacito de Luis Fonsi en spotify"))
    assert spotify.args == {"alias": "spotify", "consulta": "Despacito de Luis Fonsi"}
    assert _accion_de(_v02("busca vuelos a Cartagena en el navegador")).args["alias"] == "navegador"
    # Destino sin búsqueda o desconocido: búsqueda web normal.
    assert _v02("busca mamá en whatsapp").tipo == "buscar_web"
    web = _v02("busca recetas de arepa en internet")
    assert web.tipo == "buscar_web" and web.args["consulta"] == "recetas de arepa en internet"
    assert _v02("busca precio del dólar hoy").tipo == "buscar_web"


def test_prerrellenar_con_el_texto_original() -> None:
    accion = _accion_de(_v02("Escríbele a mamá por WhatsApp: ¡Llego tarde, guárdame arepa!"))
    assert accion.tipo == "prerrellenar"
    assert accion.args == {"alias": "whatsapp", "texto": "¡Llego tarde, guárdame arepa!"}
    assert "arepa" not in accion.descripcion
    x = _accion_de(_v02("pon en X: Hoy aprendí algo sobre el conectoma"))
    assert x.args == {"alias": "x", "texto": "Hoy aprendí algo sobre el conectoma"}
    que = _accion_de(_v02("mándale un mensaje a Ana por telegram que ya voy saliendo"))
    assert que.args == {"alias": "telegram", "texto": "ya voy saliendo"}
    # Sin prerrelleno en el destino: teclear en la ventana previa (objetivo lo pone main).
    insta = _v02("escríbele a Sofi por instagram: nos vemos a las 8")
    accion_insta = _accion_de(insta)
    assert accion_insta.tipo == "escribir_texto" and accion_insta.args == {"texto": "nos vemos a las 8"}
    assert insta.args["destino"] == "instagram"


def test_abrir_y_escribir_en_una_linea() -> None:
    accion = _accion_de(_v02("abre whatsapp y escríbele a mamá: Hola, ¿cómo estás?"))
    assert accion.tipo == "prerrellenar" and accion.args == {"alias": "whatsapp", "texto": "Hola, ¿cómo estás?"}
    sin_prerrelleno = _v02("abre instagram y escríbele: hola")
    assert _accion_de(sin_prerrelleno).tipo == "abrir_destino"
    assert sin_prerrelleno.args["nota"] == "escribir_despues"


def test_escribir_aqui_y_escribele_sin_destino() -> None:
    aqui = _accion_de(_v02("escribe ahí: Buenas noches a todos"))
    assert aqui.tipo == "escribir_texto" and aqui.args == {"texto": "Buenas noches a todos"}
    assert _accion_de(_v02("escribe aquí que ya casi termino")).args == {"texto": "ya casi termino"}
    escribele = _v02("escríbele a Ana que ya voy")
    assert _accion_de(escribele).args == {"texto": "ya voy"} and escribele.args["sin_alias"] is True
    assert _v02("escríbele a Ana: «nos vemos»").args["accion"].args == {"texto": "nos vemos"}


@pytest.mark.parametrize("texto, alias", [
    ("ponlo en WhatsApp", "whatsapp"), ("pásalo a instagram", "instagram"), ("publícalo en twitter", "x"),
    ("mándalo por wasa", "whatsapp"),
])
def test_publicar_borrador(texto: str, alias: str) -> None:
    intencion = _v02(texto)
    assert intencion.tipo == "publicar_borrador" and intencion.args == {"alias": alias}


def test_publicar_borrador_destino_desconocido_no_casa() -> None:
    assert _v02("ponlo en la nevera").tipo == "ninguna"


def test_recordatorio_personal() -> None:
    simple = _v02("búscame en 10 minutos")
    assert simple.tipo == "recordatorio_personal"
    assert simple.args["vence"] == AHORA + timedelta(minutes=10)
    assert simple.args["mensaje"] == MENSAJE_BUSCAR_DEFECTO and simple.args["con_mensaje"] is False
    con_mensaje = _v02("llámame a las 6 pm para revisar el horno")
    assert con_mensaje.args["vence"].hour == 18 and con_mensaje.args["mensaje"] == "revisar el horno"
    assert con_mensaje.args["con_mensaje"] is True
    assert _v02("ven a buscarme en media hora").args["vence"] == AHORA + timedelta(minutes=30)
    # Sin una hora válida no es un recordatorio personal.
    assert _v02("búscame la receta").tipo != "recordatorio_personal"


@pytest.mark.parametrize("texto, tipo", [
    ("ven", "escritorio_venir"), ("Ven!", "escritorio_venir"), ("aparece", "escritorio_venir"),
    ("sal", "escritorio_venir"), ("Amadeus, ven", "escritorio_venir"), ("ven, Amadeus", "escritorio_venir"),
    ("vete", "escritorio_vete"), ("escóndete", "escritorio_vete"),
    ("basta", "basta_travesuras"), ("quieta", "basta_travesuras"), ("¡Quieta ya!", "basta_travesuras"),
    ("deja el mouse", "basta_travesuras"), ("para ya", "basta_travesuras"), ("deja el ratón", "basta_travesuras"),
    ("deja de mirarme", "vision_pausa"), ("no me mires", "vision_pausa"), ("cierra los ojos", "vision_pausa"),
])
def test_frases_cortas_ancladas(texto: str, tipo: str) -> None:
    assert _v02(texto).tipo == tipo


@pytest.mark.parametrize("texto", ["ven a ver esto", "sal de ahí", "basta de bromas, explícame el conectoma",
                                   "vete a saber", "no me mires así, es en serio", "quieta la máquina de café"])
def test_frases_cortas_solo_como_mensaje_completo(texto: str) -> None:
    assert _v02(texto).tipo not in ("escritorio_venir", "escritorio_vete", "basta_travesuras", "vision_pausa")


def test_vision_pausa_con_minutos() -> None:
    assert _v02("deja de mirarme").args == {"minutos": None}
    assert _v02("no me mires por 20 minutos").args == {"minutos": 20.0}
    assert _v02("cierra los ojos durante una hora").args == {"minutos": 60.0}


@pytest.mark.parametrize("texto", [
    "abre whatsapp", "escríbele a mamá por whatsapp: hola", "pon en x: hola", "escribe ahí: hola",
    "busca gatos en youtube", "ponlo en whatsapp", "ven", "vete", "basta", "deja de mirarme",
    "/vision on", "/pantalla off", "/travesuras on",
])
def test_telegram_bloquea_lo_del_companion(texto: str) -> None:
    intencion = _v02(texto, canal="telegram")
    assert intencion.tipo == "bloqueada_canal", texto
    assert _v02(texto, canal="nexus").tipo == "bloqueada_canal"


def test_escritorio_hereda_lo_de_la_terminal_y_telegram_no_gana_capacidades() -> None:
    for texto in ("abre terminal", "me ves", "mira mi pantalla", "/camara on", "/hud", "cierra la ventana"):
        terminal = _v02(texto, canal="terminal")
        assert _v02(texto, canal="escritorio").tipo == terminal.tipo != "bloqueada_canal", texto
    # §C2.12: "llámame/búscame en 10 minutos" movería el avatar en el escritorio al vencer.
    for texto in ("llámame en 10 minutos", "búscame a las 18:30"):
        remoto = _v02(texto, canal="telegram")
        assert remoto.tipo == "bloqueada_canal", texto
        assert remoto.args["tipo_original"] == "recordatorio_personal"
        assert _v02(texto, canal="escritorio").tipo == "recordatorio_personal"
    # El recordatorio normal de la v0.1 sigue disponible por Telegram.
    assert _v02("recuérdame en 10 minutos sacar la ropa", canal="telegram").tipo == "programar"
    assert permitida_en_canal(Intencion("recordatorio_personal", {}), "telegram") is False
    assert permitida_en_canal(Intencion("publicar_borrador", {"alias": "x"}), "escritorio") is True
    assert permitida_en_canal(Intencion("publicar_borrador", {"alias": "x"}), "telegram") is False


def test_alias_siempre_es_la_clave_y_origen_usuario() -> None:
    for texto in ("abre wasa", "busca lofi en yt", "escríbele a mamá por wasa: hola", "pon en twitter: hola",
                  "abre twitter y escríbele: hola"):
        accion = _accion_de(_v02(texto))
        assert accion.args["alias"] in DESTINOS_BASE, texto


# ---------------------------------------------------------------------------
# v0.2.1: órdenes naturales (vocativos, cortesías y peticiones)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("texto", [
    "asistente abre youtube porfavor", "Kurisu, ábreme YouTube pls", "¿Puedes abrir YouTube?",
    "Puedes abrir YouTube en el PC pls", "¿me abres youtube?", "quiero que abras youtube", "abre youtube en el pc",
    "oye amadeus, abre youtube por favor", "abre YouTube, Christina", "¿podrías abrirme YouTube?",
    "me puedes abrir youtube xfa", "Por favor abre YouTube.", "abre youtube en mi computador gracias",
])
def test_ordenes_naturales_abren_el_destino(texto: str) -> None:
    accion = _accion_de(_v02(texto, canal="terminal"))
    assert accion.tipo == "abrir_destino" and accion.args == {"alias": "youtube"}, texto


def test_ordenes_naturales_buscar_y_musica() -> None:
    busca = _accion_de(_v02("oye amadeus busca gatos en youtube por favor", canal="terminal"))
    assert busca.tipo == "buscar_en" and busca.args == {"alias": "youtube", "consulta": "gatos"}
    pedido = _accion_de(_v02("¿me puedes buscar lofi en youtube?", canal="terminal"))
    assert pedido.tipo == "buscar_en" and pedido.args["consulta"] == "lofi"
    musica = _accion_de(_v02("porfa pausa la música, kurisu", canal="terminal"))
    assert musica.tipo == "media" and musica.args == {"orden": "play_pause"}
    web = _v02("asistente, busca el precio del dólar por favor", canal="terminal")
    assert web.tipo == "buscar_web" and web.args["consulta"] == "el precio del dólar"
    assert _v02("¿puedes buscar el clima de hoy?", canal="terminal").tipo == "buscar_web"
    assert _v02("Amadeus, ven", canal="terminal").tipo == "escritorio_venir"
    assert _v02("ven pls", canal="terminal").tipo == "escritorio_venir"
    assert _v02("para ya", canal="terminal").tipo == "basta_travesuras"


@pytest.mark.parametrize("texto", [
    "¿puedes abrir archivos?", "¿podrías abrir apps?", "¿puedes abrir programas en mi pc?", "abre tu mente",
    "abre tu mente a otras ideas", "abre los ojos", "abre la ventana", "¿puedes abrir cualquier cosa?",
    "hola amadeus", "¿puedes abrir algo?", "abre el chat",
])
def test_ordenes_naturales_sin_falsos_positivos(texto: str) -> None:
    intencion = _v02(texto, canal="terminal", apps_locales=True, plataforma="win32")
    assert intencion.tipo == "ninguna", (texto, intencion)


def test_el_texto_dictado_no_se_recorta_por_el_final() -> None:
    borrador = _accion_de(_v02("amadeus, escríbele a mamá por whatsapp: ven por favor, kurisu", canal="terminal"))
    assert borrador.tipo == "prerrellenar" and borrador.args["texto"] == "ven por favor, kurisu"
    aqui = _accion_de(_v02("escribe ahí: gracias, amadeus", canal="terminal"))
    assert aqui.args["texto"] == "gracias, amadeus"
    recordatorio = _v02("amadeus, recuérdame en 10 minutos llamar a mamá por favor", canal="terminal")
    assert recordatorio.tipo == "programar"
    assert recordatorio.args["mensaje"] == "llamar a mamá por favor"


# ---------------------------------------------------------------------------
# v0.2.1: apps del menú Inicio (Windows)
# ---------------------------------------------------------------------------


def _app_local(texto: str, **kwargs: object) -> AccionPC:
    accion = _accion_de(_v02(texto, canal="terminal", apps_locales=True, plataforma="win32", **kwargs))
    assert accion.tipo == "lanzar_app" and accion.args["local"] is True, texto
    return accion


def test_apps_locales_por_nombre() -> None:
    steam = _app_local("asistente abre steam porfavor")
    assert steam.args == {"nombre": "steam", "local": True, "explicito": True}
    assert _app_local("Kurisu, ábreme el Bloc de notas").args["nombre"] == "Bloc de notas"
    assert _app_local("¿puedes abrir Visual Studio Code?").args["nombre"] == "Visual Studio Code"
    assert _app_local("abre steam en el pc pls").args["nombre"] == "steam"
    assert _app_local("quiero que abras la calculadora").args["nombre"] == "calculadora"
    pon = _app_local("pon visual studio code")
    assert pon.args["explicito"] is False
    assert _app_local("abre la app de Steam").args["nombre"] == "Steam"
    assert _app_local("abre steam ya").args["nombre"] == "steam"
    assert _app_local("¿Puedes abrir Steam en el PC por favor?").args["nombre"] == "Steam"
    # RAMS_APPS en Windows: el alias también se busca en el menú Inicio (solo Linux usa hyprctl).
    assert _app_local("abre terminal").args["nombre"] == "terminal"


def test_apps_locales_respetan_catalogo_y_linux() -> None:
    explorador = _accion_de(_v02("abre el explorador de archivos", canal="terminal", apps_locales=True,
                                 plataforma="win32"))
    assert explorador.tipo == "abrir_destino" and explorador.args == {"alias": "archivos"}
    assert _accion_de(_v02("abre whatsapp", canal="terminal", apps_locales=True, plataforma="win32")).tipo \
        == "abrir_destino"
    # Sin apps_locales (por defecto, o RAMS_APPS_MENU_INICIO=0) "abre steam" no es una orden.
    assert _v02("abre steam", canal="terminal", plataforma="win32").tipo == "ninguna"
    linux = _accion_de(_v02("abre terminal", canal="terminal", plataforma="linux"))
    assert linux.tipo == "lanzar_app" and linux.args == {"alias": "terminal"}
    # "pon pausa" y el volumen ganan a la regla de apps.
    assert _accion_de(_v02("pon pausa", canal="terminal", apps_locales=True, plataforma="win32")).tipo == "media"
    assert _accion_de(_v02("pon el volumen al 30", canal="terminal", apps_locales=True,
                           plataforma="win32")).tipo == "volumen"


# ---------------------------------------------------------------------------
# v0.2.1: fotos, capturas y audio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("texto, fuente", [
    ("mándame una foto", "camara"), ("Envíame una foto pls", "camara"), ("tómame una foto", "camara"),
    ("foto de la cámara", "camara"), ("mándame una foto de la cámara del pc", "camara"),
    ("¿me mandas una foto?", "camara"), ("kurisu, sácame una foto con la webcam", "camara"),
    ("mándame una captura", "pantalla"), ("mándame una captura de pantalla", "pantalla"),
    ("hazme un pantallazo", "pantalla"), ("captura de pantalla", "pantalla"), ("mándame la pantalla", "pantalla"),
    ("envíame una foto de la pantalla", "pantalla"), ("screenshot", "pantalla"),
])
def test_fotos_en_telegram_y_en_local(texto: str, fuente: str) -> None:
    for canal in ("telegram", "terminal", "escritorio"):
        intencion = _v02(texto, canal=canal)
        assert intencion.tipo == "enviar_foto" and intencion.args == {"fuente": fuente}, (texto, canal)


def test_pantalla_y_como_me_veo_dependen_del_canal() -> None:
    assert _v02("¿Qué hay en mi pantalla?", canal="telegram").args == {"fuente": "pantalla"}
    assert _v02("¿Qué hay en mi pantalla?", canal="terminal").tipo == "mirar_pantalla"
    assert _v02("¿cómo me veo?", canal="telegram").args == {"fuente": "camara"}
    assert _v02("¿cómo me veo?", canal="terminal").tipo == "vistazo"
    # "mira mi pantalla" y "me ves" siguen siendo solo locales.
    assert _v02("mira mi pantalla", canal="telegram").tipo == "bloqueada_canal"
    assert _v02("me ves", canal="telegram").tipo == "bloqueada_canal"


@pytest.mark.parametrize("texto", ["mándame una imagen de un gato", "toma una foto de la luna y descríbela",
                                   "hazme una captura de la ventana del juego mañana", "la foto de ayer",
                                   "busca fotos de gatos"])
def test_fotos_sin_falsos_positivos(texto: str) -> None:
    assert _v02(texto, canal="telegram").tipo != "enviar_foto", texto


@pytest.mark.parametrize("texto", ["mándame un audio", "envíame una nota de voz", "háblame", "respóndeme por audio",
                                   "dímelo en audio", "contéstame con tu voz", "quiero escucharte",
                                   "Kurisu, mándame un audio porfa"])
def test_pedir_audio(texto: str) -> None:
    for canal in ("telegram", "terminal"):
        intencion = _v02(texto, canal=canal)
        assert intencion.tipo == "pedir_audio" and intencion.args == {}, (texto, canal)
    assert quiere_audio(texto), texto


def test_pedir_audio_al_telegram_y_quiere_audio() -> None:
    assert _v02("mándame un audio al telegram", canal="terminal").args == {"destino": "telegram"}
    assert _v02("háblame de los agujeros negros", canal="terminal").tipo != "pedir_audio"
    assert quiere_audio("cuéntame un chiste por audio") is True
    assert quiere_audio("explícame la teoría de cuerdas con tu voz") is True
    assert quiere_audio("hola, ¿cómo estás?") is False
    assert quiere_audio("") is False


# ---------------------------------------------------------------------------
# v0.2.1: Telegram con RAMS_TELEGRAM_ACCIONES
# ---------------------------------------------------------------------------


def _remota(texto: str, **kwargs: object) -> Intencion:
    return _v02(texto, canal="telegram", telegram_acciones=True, **kwargs)


@pytest.mark.parametrize("texto, tipo", [
    ("Puedes abrir YouTube en el PC pls", "abrir_destino"), ("abre spotify", "abrir_destino"),
    ("busca lofi en youtube", "buscar_en"), ("pausa la música", "media"), ("siguiente canción", "media"),
])
def test_telegram_acciones_de_bajo_riesgo(texto: str, tipo: str) -> None:
    accion = _accion_de(_remota(texto))
    assert accion.tipo == tipo and accion.args["canal"] == "telegram", texto
    # Sin RAMS_TELEGRAM_ACCIONES (por defecto) sigue bloqueado, como en la v0.2.
    assert _v02(texto, canal="telegram").tipo == "bloqueada_canal", texto


def test_telegram_acciones_apps_locales_llevan_el_canal() -> None:
    accion = _accion_de(_remota("abre steam", apps_locales=True, plataforma="win32"))
    assert accion.tipo == "lanzar_app" and accion.args["canal"] == "telegram" and accion.args["local"] is True
    linux = _accion_de(_remota("abre terminal", plataforma="linux"))
    assert linux.args == {"alias": "terminal", "canal": "telegram"}


@pytest.mark.parametrize("texto", [
    "escribe ahí: hola", "escríbele a mamá por whatsapp: hola", "pon en x: hola", "cierra la ventana",
    "abre whatsapp y escríbele a mamá: hola", "ponlo en whatsapp", "volumen al 50", "ve al espacio 2",
    "me ves", "mira mi pantalla", "ven", "basta", "deja de mirarme", "trae nexus", "/camara on",
])
def test_telegram_acciones_lo_demas_sigue_siendo_local(texto: str) -> None:
    intencion = _remota(texto)
    assert intencion.tipo == "bloqueada_canal", texto


def test_capacidades_v021_por_canal() -> None:
    assert {"enviar_foto", "pedir_audio"} <= set(TIPOS_INTENCION)
    assert {"enviar_foto", "pedir_audio"} <= INTENCIONES_POR_CANAL["telegram"]
    assert "accion_pc" not in INTENCIONES_POR_CANAL["telegram"]
    assert ACCIONES_TELEGRAM == frozenset({"abrir_destino", "buscar_en", "lanzar_app", "media"})
    assert FUENTES_FOTO == ("camara", "pantalla")
    abrir = Intencion("accion_pc", {"accion": AccionPC("abrir_destino", {"alias": "youtube"}, "abrir")})
    assert permitida_en_canal(abrir, "telegram") is False
    assert permitida_en_canal(abrir, "telegram", telegram_acciones=True) is True
    assert permitida_en_canal(abrir, "nexus", telegram_acciones=True) is False
    teclear = Intencion("accion_pc", {"accion": AccionPC("escribir_texto", {"texto": "x"}, "escribir")})
    assert permitida_en_canal(teclear, "telegram", telegram_acciones=True) is False
    con_nota = Intencion("accion_pc", {"accion": AccionPC("abrir_destino", {"alias": "whatsapp"}, "abrir"),
                                       "nota": "escribir_despues"})
    assert permitida_en_canal(con_nota, "telegram", telegram_acciones=True) is False
    del_llm = Intencion("accion_pc", {"accion": AccionPC("abrir_destino", {"alias": "x"}, "abrir", origen="llm")})
    assert permitida_en_canal(del_llm, "telegram", telegram_acciones=True) is False
    assert _v02("mándame una foto", canal="nexus").tipo == "bloqueada_canal"


# ---------------------------------------------------------------------------
# v0.2.1 (revisión): citas, cortesías, fotos sin cámara explícita y audio como tema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("texto, consulta", [
    ("busca información sobre makise kurisu", "información sobre makise kurisu"),
    ("busca videos de amadeus", "videos de amadeus"),
    ("busca amadeus", "amadeus"),
    ("busca el significado de christina", "el significado de christina"),
    ("busca qué es un asistente", "qué es un asistente"),
    ("busca la letra de please", "la letra de please"),
    ("busca en youtube kurisu", "en youtube kurisu"),
    ("busca la canción hacking to the gate gracias", "la canción hacking to the gate gracias"),
    # Las cortesías sí sobran; un vocativo o un "gracias" tras una coma, también.
    ("busca recetas de pasta por favor", "recetas de pasta"),
    ("busca recetas de pasta porfa", "recetas de pasta"),
    ("busca recetas de pasta pliss", "recetas de pasta"),
    ("busca el clima de mañana, amadeus", "el clima de mañana"),
    ("busca recetas de arepa, gracias", "recetas de arepa"),
    ("Kurisu, busca el precio del dólar por fa", "el precio del dólar"),
    ("¿podrías buscar la verdad sobre el experimento?", "la verdad sobre el experimento"),
])
def test_la_consulta_web_conserva_los_nombres_del_personaje(texto: str, consulta: str) -> None:
    for canal in ("escritorio", "telegram"):
        intencion = _v02(texto, canal=canal, apps_locales=True, plataforma="win32")
        assert intencion.tipo == "buscar_web" and intencion.args["consulta"] == consulta, (texto, canal, intencion)


def test_buscar_en_un_destino_tampoco_pierde_nombres() -> None:
    accion = _accion_de(_v02("busca videos de makise kurisu en youtube, amadeus", canal="terminal"))
    assert accion.tipo == "buscar_en" and accion.args == {"alias": "youtube", "consulta": "videos de makise kurisu"}


@pytest.mark.parametrize("texto", ["¿quieres buscar problemas?", "¿quieres buscar pareja?",
                                   "¿quisieras buscar otro trabajo?"])
def test_quieres_buscar_es_una_pregunta(texto: str) -> None:
    assert _v02(texto, canal="terminal").tipo == "ninguna", texto


def test_quieres_buscarme_si_es_una_peticion() -> None:
    intencion = _v02("¿quieres buscarme el clima de hoy?", canal="terminal")
    assert intencion.tipo == "buscar_web" and intencion.args["consulta"] == "el clima de hoy"


@pytest.mark.parametrize("texto", [
    "abre steam pliss", "abre steam por fa", "abre steam porfavorr", "abre steam pofavor", "abre steam plis kurisu",
    "abre steam porfaaa", "por fa abre steam", "pliss abre steam", "abre steam x favor", "abre steam please",
])
def test_cortesias_estiradas_no_entran_en_el_nombre(texto: str) -> None:
    accion = _app_local(texto)
    assert accion.args == {"nombre": "steam", "local": True, "explicito": True}, texto


@pytest.mark.parametrize("texto", ["abre youtube en el navegador", "abre youtube en chrome",
                                   "Kurisu, ábreme YouTube en Firefox porfa", "abre youtube en mi navegador"])
def test_destino_en_el_navegador(texto: str) -> None:
    accion = _accion_de(_v02(texto, canal="terminal", apps_locales=True, plataforma="win32"))
    assert accion.tipo == "abrir_destino" and accion.args == {"alias": "youtube"}, texto


def test_app_en_el_navegador_no_es_una_orden_de_app() -> None:
    assert _v02("abre steam en chrome", canal="terminal", apps_locales=True, plataforma="win32").tipo == "ninguna"


@pytest.mark.parametrize("texto, nombre", [
    ("lanza una moneda", "moneda"), ("¿podrías lanzar una moneda?", "moneda"), ("arranca el carro", "carro"),
    ("ejecuta el plan", "plan"), ("abre un archivo", "archivo"), ("inicia la fiesta", "fiesta"),
])
def test_ordenes_de_app_tentativas(texto: str, nombre: str) -> None:
    accion = _app_local(texto)
    assert accion.args["nombre"] == nombre and accion.args["tentativa"] is True, texto


def test_abre_con_nombre_no_es_tentativa() -> None:
    for texto in ("abre steam", "ábreme el bloc de notas", "¿puedes abrir Visual Studio Code?"):
        assert "tentativa" not in _app_local(texto).args, texto


@pytest.mark.parametrize("texto", [
    "hazme una imagen", "haz una imagen", "muéstrame una imagen", "dame una imagen", "quiero una imagen",
    "¿me puedes mandar una imagen?", "mándame una imagen", "quiero una foto", "enséñame una foto", "dame una foto",
])
def test_imagen_sin_camara_explicita_no_usa_la_webcam(texto: str) -> None:
    for canal in ("telegram", "escritorio"):
        intencion = _v02(texto, canal=canal, telegram_acciones=True)
        assert not (intencion.tipo == "enviar_foto" and intencion.args.get("fuente") == "camara"), (texto, canal)


@pytest.mark.parametrize("texto", [
    "muéstrame una imagen de la webcam", "quiero una foto de la cámara", "dame una foto de la cámara del pc",
    "hazme una foto", "toma una foto", "mándame una imagen de la cámara", "¿me puedes mandar una foto?",
])
def test_foto_con_peticion_clara_usa_la_camara(texto: str) -> None:
    intencion = _v02(texto, canal="telegram")
    assert intencion.tipo == "enviar_foto" and intencion.args == {"fuente": "camara"}, texto


@pytest.mark.parametrize("texto", [
    "videos con audio", "tengo un problema con audio", "cómo grabo la pantalla con audio",
    "el audio en mi pc está fallando, en audio no hay sonido", "¿cómo exporto el video como audio?",
    "¿qué pasa con tu voz?", "cambia la salida de audio", "audio", "busca un mensaje de voz que perdí",
])
def test_quiere_audio_ignora_el_audio_como_tema(texto: str) -> None:
    assert quiere_audio(texto) is False, texto


@pytest.mark.parametrize("texto", [
    "cuéntame un chiste por audio", "cuéntame un chiste por audio porfa", "resúmeme las noticias en audio, amadeus",
    "dime por voz qué hora es", "mándamelo como nota de voz", "explícame la relatividad vía nota de voz",
    "cuéntame un cuento con tu voz", "mándame un audio contándome tu día",
])
def test_quiere_audio_cuando_pide_la_forma(texto: str) -> None:
    assert quiere_audio(texto) is True, texto


# ---------------------------------------------------------------------------
# v0.2.2: Google, correo, Drive y taller (docs/V022_CORREO_DRIVE_SCRIPTS.md §4.6)
# ---------------------------------------------------------------------------


def test_v022_tipos_ops_y_canales() -> None:
    for tipo in ("google", "correo", "drive", "taller"):
        assert tipo in TIPOS_INTENCION
        assert OPS_INTENCION[tipo]
    assert OPS_INTENCION["drive"] == ("buscar", "info", "descargar", "leer", "crear_carpeta", "subir", "papelera",
                                      "compartir")
    assert {"google", "correo", "drive"} <= INTENCIONES_POR_CANAL["telegram"]
    assert "taller" not in INTENCIONES_POR_CANAL["telegram"]
    assert OPS_POR_CANAL["telegram"] == {"google": frozenset({"estado"}), "correo": frozenset({"preparar"}),
                                         "drive": frozenset({"buscar", "info"})}
    assert {"/google", "/script", "/habilidades"} <= set(COMANDOS)
    assert "/google" in COMANDOS_POR_CANAL["telegram"]
    assert not {"/script", "/habilidades"} & COMANDOS_POR_CANAL["telegram"]
    # ACCIONES_TELEGRAM no cambia en la v0.2.2.
    assert ACCIONES_TELEGRAM == frozenset({"abrir_destino", "buscar_en", "lanzar_app", "media"})


def test_v022_google() -> None:
    conectar = _ruta("conecta mi cuenta de Google")
    assert conectar.tipo == "google" and conectar.args == {"op": "conectar"}
    assert _ruta("vincula mi gmail").args == {"op": "conectar"}
    assert _ruta("conecta google y drive").args == {"op": "conectar"}
    assert _ruta("desconecta google", "escritorio").args == {"op": "desconectar"}
    assert _ruta("desvincula mi cuenta de Google").args == {"op": "desconectar"}
    assert _ruta("¿está conectado google?").args == {"op": "estado"}
    bloqueada = _ruta("conecta google", "telegram")
    assert bloqueada.tipo == "bloqueada_canal" and bloqueada.args["tipo_original"] == "google"
    assert _ruta("desconecta google", "telegram").tipo == "bloqueada_canal"
    estado = _ruta("¿está conectado google?", "telegram")
    assert estado.tipo == "google" and estado.args == {"op": "estado"}
    comando = _ruta("/google", "telegram")
    assert comando.tipo == "comando" and comando.args["comando"] == "/google"


def test_v022_correo_casos_de_la_spec() -> None:
    a = _ruta("mándale un correo a mamá diciendo que llegué bien")
    assert a.tipo == "correo"
    assert a.args["op"] == "preparar" and a.args["para"] == ["mamá"] and a.args["cuerpo"] == "Llegué bien"
    assert a.args["redactar"] is False and a.args["tema"] is None and a.args["asunto"] is None
    b = _ruta("envía un email a juan@x.com con asunto Reunión: mañana no puedo")
    assert b.args["para"] == ["juan@x.com"] and b.args["asunto"] == "Reunión"
    assert b.args["cuerpo"] == "Mañana no puedo"
    c = _ruta("redacta un correo a mi jefe pidiendo vacaciones")
    assert c.args["para"] == ["mi jefe"] and c.args["redactar"] is True and c.args["tema"] == "Pidiendo vacaciones"
    assert c.args["cuerpo"] is None
    d = _ruta("manda un correo a ana@x.com y a pedro@y.com con copia a mamá, adjunta informe.pdf: aquí va el informe")
    assert d.args["para"] == ["ana@x.com", "pedro@y.com"] and d.args["cc"] == ["mamá"] and d.args["cco"] == []
    assert d.args["adjuntos"] == ["informe.pdf"] and d.args["cuerpo"] == "Aquí va el informe"
    assert set(d.args) == {"op", "para", "cc", "cco", "asunto", "cuerpo", "tema", "redactar", "adjuntos"}


def test_v022_correo_no_roba_otras_reglas() -> None:
    prerrelleno = _ruta("escríbele a juan por gmail: hola")
    assert prerrelleno.tipo == "accion_pc" and prerrelleno.args["accion"].tipo == "prerrellenar"
    assert prerrelleno.args["accion"].args["alias"] == "gmail"
    # Con un alias de destino, también «… un correo a juan por gmail: …» sigue siendo prerrelleno.
    assert _ruta("mándale un correo a juan por gmail: hola").args["accion"].tipo == "prerrellenar"
    assert _ruta("recuérdame enviar un correo a juan mañana a las 8").tipo == "programar"
    telegram = _ruta("mándale un correo a mamá diciendo hola", "telegram")
    assert telegram.tipo == "correo" and telegram.args["op"] == "preparar"


def test_v022_correo_texto_crudo_y_saltos_de_linea() -> None:
    # El cuerpo sale del texto ORIGINAL: tildes, saltos de línea y palabras largas intactos.
    intencion = _ruta("escríbele un correo a ana: hola\nel archivo es informe_final_ventas_2026_q3.xlsx")
    assert intencion.args["cuerpo"] == "Hola\nel archivo es informe_final_ventas_2026_q3.xlsx"
    # Varias listas, CCO y adjuntos por ruta relativa (sin "..").
    otro = _ruta("envía un correo a ana, juan y mamá con copia oculta a papá adjuntando docs/a.pdf y ../b.pdf "
                 "diciendo que ya está")
    assert otro.args["para"] == ["ana", "juan", "mamá"] and otro.args["cco"] == ["papá"]
    assert otro.args["adjuntos"] == ["docs/a.pdf"] and otro.args["cuerpo"] == "Ya está"
    assert otro.args["descartados"] == {"destinatarios": 0, "adjuntos": 1}      # «../b.pdf», nunca en silencio


@pytest.mark.parametrize("texto, cuerpo", [
    # «qué», «¿qué» y «¡qué» son contenido (interrogativo o exclamativo), no la conjunción que separa.
    ("mándale un correo a mamá diciendo ¿qué hora es allá?", "¿Qué hora es allá?"),
    ("mándale un correo a mamá diciendo qué rico estuvo el almuerzo", "Qué rico estuvo el almuerzo"),
    ("mándale un correo a mamá diciendo ¡qué rico estuvo!", "¡Qué rico estuvo!"),
    ("manda un correo a juan@x.com qué tal todo", "Qué tal todo"),
    ("manda un correo a juan@x.com ¿qué dice el informe?", "¿Qué dice el informe?"),
    ("manda un correo a juan@x.com que dice qué bonito día", "Qué bonito día"),
    ("manda un correo a juan@x.com asunto Hola diciendo qué tal", "Qué tal"),
    ("manda un correo a mamá diciendo que qué pena lo de ayer", "Qué pena lo de ayer"),
    # La conjunción sí se quita (también con mayúscula o coma).
    ("mándale un correo a mamá diciendo que llegué bien", "Llegué bien"),
    ("mándale un correo a mamá diciendo Que llegué bien", "Llegué bien"),
    ("manda un correo a juan@x.com que, la verdad, no puedo", "La verdad, no puedo"),
])
def test_v022_correo_que_interrogativo_es_contenido(texto: str, cuerpo: str) -> None:
    intencion = _ruta(texto)
    assert intencion.tipo == "correo", texto
    assert intencion.args["cuerpo"] == cuerpo, texto


@pytest.mark.parametrize("texto, cuerpo", [
    # Solo se quitan las comillas que envuelven el texto ENTERO; una suelta en un extremo es contenido.
    ("manda un correo a mamá diciendo que le diga a papá “te quiero”", "Le diga a papá “te quiero”"),
    ("manda un correo a juan@x.com: la contraseña del wifi es 'casa123'", "La contraseña del wifi es 'casa123'"),
    ("manda un correo a juan@x.com: \"Hola\", dijo él", "\"Hola\", dijo él"),
    ("manda un correo a mamá diciendo: «hola»\n\n— Juan", "«Hola»\n\n— Juan"),
    ("manda un correo a mamá diciendo \"hola\" y \"adiós\"", "\"Hola\" y \"adiós\""),
    ("manda un correo a mamá diciendo «hola»", "Hola"),
    ("manda un correo a mamá diciendo: “nos vemos mañana”", "Nos vemos mañana"),
    ("manda un correo a mamá diciendo 'hola'", "Hola"),
])
def test_v022_correo_comillas_sueltas_se_conservan(texto: str, cuerpo: str) -> None:
    assert _ruta(texto).args["cuerpo"] == cuerpo, texto


def test_v022_correo_asunto_con_comillas() -> None:
    assert _ruta("manda un correo a juan@x.com con asunto Informe \"final\": hola").args["asunto"] == \
        "Informe \"final\""
    assert _ruta("manda un correo a juan@x.com con asunto «Reunión»: mañana no puedo").args["asunto"] == "Reunión"


@pytest.mark.parametrize("texto, para, cuerpo, tema", [
    ("¿puedes mandarle un correo a mamá diciendo hola?", ["mamá"], "Hola", None),
    ("me puedes mandar un correo a mamá diciendo hola", ["mamá"], "Hola", None),
    ("quiero que le mandes un correo a mamá diciendo hola", ["mamá"], "Hola", None),
    ("¿le envías un correo a mamá diciendo hola?", ["mamá"], "Hola", None),
    ("¿podrías escribirle un email a juan@x.com: nos vemos el lunes?", ["juan@x.com"], "Nos vemos el lunes", None),
    ("necesito que le envíes un correo a ana@x.com diciendo ¿vienes?", ["ana@x.com"], "¿Vienes?", None),
    ("¿podrías redactarle un correo a mi jefe pidiendo vacaciones?", ["mi jefe"], None, "Pidiendo vacaciones"),
])
def test_v022_correo_en_forma_de_peticion(texto: str, para: list[str], cuerpo: str | None,
                                         tema: str | None) -> None:
    intencion = _ruta(texto)
    assert intencion.tipo == "correo", texto
    assert intencion.args["para"] == para and intencion.args["cuerpo"] == cuerpo, texto
    assert intencion.args["tema"] == tema and intencion.args["redactar"] is (tema is not None), texto
    assert _ruta(texto, "telegram").tipo == "correo", texto                  # preparar sí va por Telegram


@pytest.mark.parametrize("texto, tarea", [
    ("¿me puedes hacer un script que liste mis fotos?", "liste mis fotos"),
    ("¿podrías escribirme un script para ordenar mis descargas?", "ordenar mis descargas"),
    ("quiero que me hagas un script que renombre mis fotos", "renombre mis fotos"),
    ("¿me haces un script que comprima mis videos?", "comprima mis videos"),
])
def test_v022_taller_en_forma_de_peticion(texto: str, tarea: str) -> None:
    intencion = _ruta(texto)
    assert intencion.tipo == "taller" and intencion.args == {"op": "crear", "tarea": tarea}, texto
    assert _ruta(texto, "telegram").tipo == "bloqueada_canal", texto         # el taller nunca por Telegram


@pytest.mark.parametrize("texto", ["¿me puedes recordar enviar un correo a juan mañana a las 8?",
                                   "¿puedes mandarme la foto?", "¿le escribes cartas a tu mamá?",
                                   "quiero que me hagas un favor", "¿me haces un café?"])
def test_v022_peticiones_que_no_son_correo_ni_taller(texto: str) -> None:
    assert _ruta(texto).tipo not in ("correo", "taller"), texto


def test_v022_correo_lo_que_no_cabe_se_cuenta() -> None:
    lista = ", ".join(f"p{n}@x.com" for n in range(1, 13))
    muchos = _ruta(f"manda un correo a {lista} diciendo hola")
    assert len(muchos.args["para"]) == 10 and muchos.args["para"][-1] == "p10@x.com"
    assert muchos.args["descartados"] == {"destinatarios": 2, "adjuntos": 0}
    for ruta in ("C:/Users/x/secreto.txt", "../../.env", "~/notas.txt", "/etc/passwd"):
        intencion = _ruta(f"manda un correo a juan@x.com adjunta {ruta}: hola")
        assert intencion.args["adjuntos"] == [] and intencion.args["cuerpo"] == "Hola", ruta
        assert intencion.args["descartados"] == {"destinatarios": 0, "adjuntos": 1}, ruta
    seis = _ruta("manda un correo a juan@x.com adjunta a.pdf, b.pdf, c.pdf, d.pdf, e.pdf y f.pdf: hola")
    assert len(seis.args["adjuntos"]) == 5 and seis.args["descartados"] == {"destinatarios": 0, "adjuntos": 1}
    # Sin nada fuera, la clave no aparece (el contrato de §4.1 sigue intacto).
    assert "descartados" not in _ruta("manda un correo a juan@x.com adjunta docs/a.pdf: hola").args


def test_v022_drive_casos_de_la_spec() -> None:
    buscar = _ruta("busca en mi drive el informe de ventas")
    assert buscar.tipo == "drive" and buscar.args == {"op": "buscar", "consulta": "informe de ventas"}
    youtube = _ruta("busca lofi en youtube")
    assert youtube.tipo == "accion_pc" and youtube.args["accion"].tipo == "buscar_en"
    descargar = _ruta("descarga el 2 de drive como pdf en documentos")
    assert descargar.args == {"op": "descargar", "ref": "2", "formato": "pdf", "destino": "documentos"}
    assert _ruta("borra el 3 de drive").args == {"op": "papelera", "ref": "3"}
    assert _ruta("borra el archivo").tipo != "drive"
    publico = _ruta("haz público el 2")
    assert publico.tipo == "drive"
    assert {k: publico.args[k] for k in ("op", "ref", "publico", "rol")} == {"op": "compartir", "ref": "2",
                                                                          "publico": True, "rol": "reader"}
    bloqueada = _ruta("sube informe.pdf a drive", "telegram")
    assert bloqueada.tipo == "bloqueada_canal" and bloqueada.args["tipo_original"] == "drive"
    remota = _ruta("busca en mi drive impuestos", "telegram")
    assert remota.tipo == "drive" and remota.args["op"] == "buscar"


@pytest.mark.parametrize("texto, esperado", [
    ("¿qué tengo en drive sobre impuestos?", {"op": "buscar", "consulta": "impuestos"}),
    ("lista mi drive", {"op": "buscar", "consulta": None}),
    ("archivos recientes de drive", {"op": "buscar", "consulta": None}),
    ("busca informe en drive", {"op": "buscar", "consulta": "informe"}),
    ("detalles del 2", {"op": "info", "ref": "2"}),
    ("info del archivo informe ventas de drive", {"op": "info", "ref": "informe ventas"}),
    ("descarga el primero", {"op": "descargar", "ref": "1"}),
    ("resume el documento 1 de drive", {"op": "leer", "ref": "1"}),
    ("léeme el doc de presupuesto", {"op": "leer", "ref": "presupuesto"}),
    ("crea una carpeta Facturas 2026 en drive", {"op": "crear_carpeta", "nombre": "Facturas 2026"}),
    ("crea una carpeta Facturas en drive dentro de Trabajo",
     {"op": "crear_carpeta", "nombre": "Facturas", "carpeta": "Trabajo"}),
    ("sube informe.pdf a drive", {"op": "subir", "nombre": "informe.pdf"}),
    ("sube documentos/trabajo/plan.xlsx a la carpeta Trabajo de drive",
     {"op": "subir", "nombre": "documentos/trabajo/plan.xlsx", "carpeta": "Trabajo"}),
    ("manda a la papelera el 3", {"op": "papelera", "ref": "3"}),
    ("comparte el 2 con ana@x.com como editora",
     {"op": "compartir", "ref": "2", "correo": "ana@x.com", "rol": "writer", "publico": False}),
    ("comparte el enlace del 2 con cualquiera",
     {"op": "compartir", "ref": "2", "correo": None, "rol": "reader", "publico": True}),
])
def test_v022_drive_operaciones(texto: str, esperado: dict[str, object]) -> None:
    intencion = _ruta(texto)
    assert intencion.tipo == "drive", texto
    assert intencion.args == esperado, texto


@pytest.mark.parametrize("texto", ["detalles de la reunión", "descarga el juego", "lee esto", "borra el archivo",
                                   "manda la carta a la papelera", "busca la receta del ajiaco"])
def test_v022_drive_no_roba_conversacion(texto: str) -> None:
    assert _ruta(texto).tipo != "drive", texto


def test_v022_drive_publico_editable_pasa_como_writer() -> None:
    # El enrutador lo deja pasar con rol writer: el módulo y la compuerta lo bloquean siempre.
    intencion = _ruta("haz público el 2 como editora")
    assert intencion.args["publico"] is True and intencion.args["rol"] == "writer"


def test_v022_taller() -> None:
    crear = _ruta("hazme un script que renombre mis fotos")
    assert crear.tipo == "taller" and crear.args == {"op": "crear", "tarea": "renombre mis fotos"}
    assert _ruta("Escribe un script para Ordenar mis Descargas").args["tarea"] == "Ordenar mis Descargas"
    assert _ruta("hazme un script que renombre mis fotos", "telegram").tipo == "bloqueada_canal"
    habilidad = _ruta("ejecuta la habilidad renombrar fotos")
    assert habilidad.args == {"op": "ejecutar_habilidad", "nombre": "renombrar fotos"}
    assert _ruta("olvida la habilidad renombrar fotos").args == {"op": "olvidar_habilidad",
                                                                 "nombre": "renombrar fotos"}
    assert _ruta("¿qué habilidades tienes?").args == {"op": "habilidades"}
    assert _ruta("¿qué habilidades tienes?", "telegram").tipo == "bloqueada_canal"
    script = _ruta("/script ordena mis fotos")
    assert script.tipo == "comando" and script.args == {"comando": "/script", "resto": "ordena mis fotos"}
    assert _ruta("/script x", "telegram").tipo == "bloqueada_canal"
    assert _ruta("/habilidades", "telegram").tipo == "bloqueada_canal"


def test_v022_nexus_no_admite_nada_nuevo() -> None:
    for texto in ("conecta mi cuenta de Google", "busca en mi drive impuestos", "hazme un script que x",
                  "mándale un correo a mamá diciendo hola"):
        assert _ruta(texto, "nexus").tipo == "bloqueada_canal", texto


def test_v022_permitida_en_canal_mira_las_ops() -> None:
    assert permitida_en_canal(Intencion("drive", {"op": "info", "ref": "2"}), "telegram") is True
    assert permitida_en_canal(Intencion("drive", {"op": "compartir", "ref": "2"}), "telegram") is False
    assert permitida_en_canal(Intencion("google", {"op": "conectar"}), "telegram") is False
    assert permitida_en_canal(Intencion("google", {}), "telegram") is False
    assert permitida_en_canal(Intencion("taller", {"op": "crear"}), "telegram") is False
    assert permitida_en_canal(Intencion("taller", {"op": "crear"}), "escritorio") is True


def test_v022_pide_tarea_pc() -> None:
    assert pide_tarea_pc("organiza mis descargas por tipo") is True
    assert pide_tarea_pc("¿a qué hora abre el banco?") is False
    assert pide_tarea_pc("organiza tus ideas") is False
    assert pide_tarea_pc("¿puedes renombrar todas las fotos de Descargas con la fecha?") is True
    assert pide_tarea_pc("oye amadeus, apaga el wifi") is True
    assert pide_tarea_pc("quiero que conviertas estos videos a mp3") is True
    assert pide_tarea_pc("el ordenador de mi casa tiene fotos") is False
    assert pide_tarea_pc("descarga los videos mañana") is False      # habla de horarios
    assert pide_tarea_pc("") is False


def test_v022_acepta_oferta() -> None:
    for texto in ("sí, hazlo con un script", "dale", "Sí.", "ok, de una", "inténtalo", "sí por favor", "hágale"):
        assert acepta_oferta(texto) is True, texto
    for texto in ("sí, pero mañana", "no", "si puedes", "sí 2", "", "dale con todo el script y algo más"):
        assert acepta_oferta(texto) is False, texto
