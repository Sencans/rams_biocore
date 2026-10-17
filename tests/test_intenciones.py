"""Tests del enrutador local de intenciones y del detector de eventos sociales (§10.12).

Lógica pura: sin red, sin hardware y con un ``ahora`` fijo *aware* (America/Bogota).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.contracts import DESTINOS_BASE, AccionPC
from core.intenciones import (
    COMANDOS,
    COMANDOS_POR_CANAL,
    INTENCIONES_POR_CANAL,
    MENSAJE_BUSCAR_DEFECTO,
    MENSAJE_SOLO_LOCAL,
    TIPOS_INTENCION,
    Intencion,
    completar_programar,
    detectar_evento_social,
    enrutar,
    normalizar,
    parsear_comando,
    parsear_cuando,
    permitida_en_canal,
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
