"""Orquestador de R.A.M.S. BIO-CORE: la "app Amadeus" (§6 y §10.21).

Qué hace:
- Monta el bus de eventos y los módulos en el orden normativo (HUD primero, Broca, memoria,
  perfil, conectoma, permisos, lóbulos periféricos, presencia, salida y socket de control) y
  los conecta por el bus.
- Muestra la secuencia de arranque *memory restore* de Amadeus con comprobaciones reales,
  rueda nixie del medidor de divergencia y "Llamada entrante: Amadeus".
- Corre el latido homeostático (conectoma LIF) y la consola en paralelo, con tareas
  supervisadas que se relanzan con *backoff* (§6.3).
- Procesa los turnos (9 pasos de §10.21), las iniciativas del conectoma, la agenda, las
  alertas, Nexus IDE y el socket de control. Elige el canal de salida **una sola vez** por
  turno (voz, terminal o Telegram).
- Implementa ``--selftest`` (autodiagnóstico sin red, cámara ni audio) y ``--offline``.
- Apaga de forma limpia en ≤ 15 s: despedida (con «El Psy Kongroo» solo en sesiones
  especiales), bitácora, cancelación de tareas y cierre ordenado de módulos (§6.6).

v0.2 "Amadeus Companion" (``docs/COMPANION.md`` §C2.14, grupo ``orquestacion``):
- Arranque de ``ControlPC`` (control de apps, teclado y cursor), ``VisionContinua`` (cámara y
  pantalla en local, opt-in) y del endpoint ``/escritorio`` del HUD (Amadeus Desktop y chat
  RINE), con sus diagnósticos ``control_pc``, ``vision_camara``, ``vision_pantalla`` y
  ``escritorio``.
- Chat de escritorio (``escritorio.chat``): un turno serializado por mensaje en el canal local
  ``escritorio`` (texto redactado con alta entropía); con una confirmación pendiente, el texto
  espera (como mucho 3) y **nunca** resuelve la confirmación: eso solo se hace con los botones
  (``escritorio.confirmacion`` → ``resolver_por_id``) o en la terminal.
- Acciones v0.2 (``abrir_destino``, ``buscar_en``, ``prerrellenar``, ``escribir_texto``,
  ``travesura_cursor``): pasan **una** vez por la compuerta y las ejecuta ``ControlPC`` (nunca
  ``aura.ejecutar``), con auditoría en ``data/acciones_pc.jsonl`` (sin el texto) y ``pc.accion``.
- Travesuras (dueño único: main): ``EstadoTravesuras`` persistido en ``data/travesuras.json``
  y un solo método, ``_set_travesuras``, para "basta", ``/travesuras``, el aborto y la bandeja.
- Iniciativas con el overlay (aparecer, buscar al usuario), curiosidad web, borradores,
  "no molestar" (usuario y sistema) y la traducción de visión/escritorio al conectoma y a la
  presencia, todo con reglas deterministas (sin LLM).

Dependencias: los módulos P0 (memoria, conectoma, Broca, permisos, presencia y salida) son
obligatorios y se importan al iniciar; los P1/P2 (visión/PC, VTube Studio, web, HUD, control
y Nexus) se importan y construyen dentro de ``try/except`` y, si faltan o fallan, quedan en
``None`` con su diagnóstico ``OFF``. ``httpx`` y ``prompt_toolkit`` son opcionales.

Degradación: en Windows todo arranca en modo degradado sin excepciones; sin ``prompt_toolkit``
la consola usa un hilo daemon ``rams-stdin`` y secuencias ANSI (o ``\\n`` si la consola no
admite VT). Ningún periférico tumba el sistema.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import random
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, TextIO

from core.bus import BusEventos, Evento, instalar_manejador_log
from core.config import (
    Settings,
    ahora_local,
    cargar_settings,
    configurar_logging,
    redactar,
    zona_local,
)
from core.contracts import (
    CANALES_ENTRADA,
    CANALES_LOCALES,
    SECUENCIA_ARRANQUE_DIVERGENCIA,
    VERSION,
    AccionPC,
    BrocaReply,
    ContextoBroca,
    DiagnosticoModulo,
    EstadoModulo,
    EstadoPresencia,
    Etiquetas,
    HomeostaticState,
    ResultadoAccion,
    ResultadoVistazo,
    Segmento,
    Utterance,
    formatear_decimal,
    resumen_nexus,
)
from core.etiquetas import limpiar_para_voz, parsear_respuesta, sugerir_etiquetas
from core.intenciones import (
    COMANDOS,
    COMANDOS_POR_CANAL,
    MENSAJE_SOLO_LOCAL,
    Intencion,
    completar_programar,
    detectar_evento_social,
    enrutar,
    quiere_cancelar,
)

try:  # v0.2 (grupo base): contratos del companion; sin ellos main sigue en modo v0.1
    from core.contracts import DESTINOS_BASE, PATRONES_TRAVESURA, TIPOS_ACCION_V02
except ImportError:  # contratos v0.1
    DESTINOS_BASE = {}  # type: ignore[assignment]
    PATRONES_TRAVESURA = ()  # type: ignore[assignment]
    TIPOS_ACCION_V02 = frozenset()  # type: ignore[assignment]
try:  # v0.2 (este grupo): constantes del enrutador
    from core.intenciones import MENSAJE_BUSCAR_DEFECTO, NOTA_ESCRIBIR_DESPUES
except ImportError:  # enrutador v0.1
    MENSAJE_BUSCAR_DEFECTO = "Me pediste que te buscara"  # type: ignore[misc]
    NOTA_ESCRIBIR_DESPUES = "escribir_despues"  # type: ignore[misc]
try:  # nombres legibles de los destinos y saneado del texto (core/permisos.py, este grupo)
    from core.permisos import nombre_destino, sanear_texto_accion
except ImportError:  # compuerta v0.1
    def nombre_destino(alias: Any) -> str:  # type: ignore[misc]
        return str(alias or "esa app")

    def sanear_texto_accion(texto: Any) -> str:  # type: ignore[misc]
        return re.sub(r"[\r\n\t]+", " ", texto).strip() if isinstance(texto, str) else ""

try:  # opcional: cliente HTTP compartido (Gemini, ElevenLabs, Telegram, Brave)
    import httpx
except ImportError:  # sin httpx: los módulos se degradan a modo local
    httpx = None

try:  # opcional: consola que no borra lo que el usuario está escribiendo
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # sin prompt_toolkit: hilo rams-stdin + ANSI
    PromptSession = None
    KeyBindings = None
    patch_stdout = None

__all__ = [
    "CANAL_DE_MODO",
    "Consola",
    "EstadoTravesuras",
    "Orquestador",
    "supervisar",
    "autodiagnostico",
    "construir_parser",
    "aplicar_argumentos",
    "main_async",
    "main",
]

log = logging.getLogger("rams.main")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CANAL_DE_MODO: Final = {"voz": "voz", "telegram": "telegram"}   # cualquier otro modo → "terminal"

# Un recordatorio a medias ("recuérdame comprar pan") espera la respuesta como mucho 5 min.
PLAZO_PROGRAMAR_PENDIENTE_S: Final = 300.0

# Misma regex que la compuerta (§9.1): dirección de ventana de Hyprland.
_DIRECCION_RE: Final = re.compile(r"^0x[0-9a-fA-F]{1,16}$")

NOMBRE_CAMPO: Final = {
    "steins_gate": "línea Steins;Gate",
    "alfa": "campo α",
    "beta": "campo β",
    "anomalo": "estado anómalo",
}

_NOMBRES_MODULO: Final = {
    "memoria": "Almacén de memoria (john/)",
    "conectoma": "Conectoma CX+MB (LIF)",
    "broca": "Área de Broca (Gemini)",
    "camara": "Lóbulo visual (cámara)",
    "telemetria": "Telemetría",
    "hyprland": "Hyprland",
    "nexus": "Nexus IDE",
    "vts": "Puente VTube Studio",
    "avatar3d": "Avatar 3D (VTuber AI Studio)",
    "motor": "Corteza motora (avatar 3D)",
    "voz": "Salida de voz",
    "telegram": "Mensajería (Telegram/RINE)",
    "web": "Investigación web",
    "hud": "HUD",
    "control": "Socket de control (MCP)",
    # v0.2
    "escritorio": "Amadeus Desktop (/escritorio)",
    "control_pc": "Control del PC",
    "vision_camara": "Visión continua (cámara)",
    "vision_pantalla": "Visión continua (pantalla)",
}

# Orden fijo de las líneas de módulos en el arranque (maqueta de §6.4, ampliada en la v0.2).
_ORDEN_ARRANQUE: Final = ("broca", "camara", "telemetria", "hyprland", "vts", "avatar3d", "motor", "voz", "telegram",
                          "web", "nexus", "hud", "escritorio", "control_pc", "vision_camara", "vision_pantalla",
                          "control")

# Tarea supervisada → clave de diagnóstico que pasa a DEGRADADO si se desactiva.
_CLAVE_TAREA: Final = {"conectoma": "conectoma", "salida": "voz", "telemetria": "telemetria",
                       "hyprland": "hyprland", "nexus": "nexus", "vts": "vts", "avatar3d": "avatar3d",
                       "motor": "motor", "telegram_entrada": "telegram", "salieri": "memoria", "agenda": "memoria",
                       "vigilancia": "control_pc"}

# --- v0.2 (docs/COMPANION.md §C2.14) ---------------------------------------
TTL_BORRADOR_S: Final = 600.0                 # el último borrador caduca a los 10 min
MAX_BORRADOR: Final = 500
CHAT_EN_ESPERA_MAX: Final = 3                 # mensajes del chat retenidos durante una confirmación
MAX_TEXTO_CHAT: Final = 2000
MAX_TEXTO_SISTEMA: Final = 4000
MENSAJE_CONFIRMACION_PENDIENTE_CHAT: Final = ("Tienes una confirmación pendiente: usa los botones. "
                                              "Leo tu mensaje después.")
MENSAJE_HUD_SOLO_TERMINAL: Final = "La URL del HUD solo se muestra en la terminal."
SONDEO_VIGILANCIA_S: Final = 5.0              # no molestar, vencimiento de travesuras, rostro
REFRESCO_ROSTRO_S: Final = 30.0               # registrar_rostro(True) mientras la visión ve a alguien
INTERVALO_INYECCION_ESCRITORIO_S: Final = 5.0  # toque / soltada → conectoma, ≤ 1 cada 5 s
VENTANA_TOQUES_S: Final = 30.0
TOQUES_ENFADO: Final = 5                      # ≥ 5 toques en 30 s → error (enfado)
INTERVALO_PANTALLA_ESTIMULO_S: Final = 30.0   # pantalla.estimulo → ventana_cambia, ≤ 1 cada 30 s
PANTALLA_FRESCA_S: Final = 10.0               # muestra de pantalla.estado utilizable para la percepción
CURIOSIDAD_WEB_MAX_DIA: Final = 3
CURIOSIDAD_WEB_SEPARACION_S: Final = 7200.0
MAX_CONSULTA_CURIOSIDAD: Final = 60
MAX_URL_NOTIFICACION: Final = 300
MAX_TEXTO_NOTIFICACION: Final = 300
TTL_SOLICITUD_MOTOR_S: Final = 20.0
TRAVESURAS_BASTA_S: Final = 7200.0            # 1.er "basta" del día: 2 h
TRAVESURAS_ABORTO_S: Final = 7200.0           # el usuario movió el ratón: 2 h
TTL_ULTIMO_DESTINO_S: Final = 120.0           # "abre whatsapp" → "escríbele a mamá: …"
_URL_HTTPS_RE: Final = re.compile(r"^https://[^\s<>\"']+$")
_ID_CONFIRMACION_RE: Final = re.compile(r"^[0-9a-f]{8}$")
_EXE_SEGURO_RE: Final = re.compile(r"[^A-Za-z0-9_.\- ]+")

_MESES: Final = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                 "octubre", "noviembre", "diciembre")
_DIAS: Final = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

_DESCRIPCION_COMANDOS: Final = {
    "/ayuda": ("/ayuda", "esta lista de comandos"),
    "/salir": ("/salir", "termina la sesión (despedida y bitácora)"),
    "/estado": ("/estado", "estado homeostático, divergencia, presencia y módulos"),
    "/divergencia": ("/divergencia", "lectura del medidor de divergencia"),
    "/memoria": ("/memoria [consulta]", "lista o busca en los datos de memoria"),
    "/recordar": ("/recordar <texto>", "guarda un fragmento de memoria"),
    "/olvidar": ("/olvidar <id>", "borra un fragmento de memoria"),
    "/agenda": ("/agenda", "recordatorios y cápsulas del tiempo pendientes"),
    "/camara": ("/camara on|off", "interruptor de la cámara (si RAMS_CAMERA=1)"),
    "/silencio": ("/silencio", "activa o silencia la voz"),
    "/colgar": ("/colgar [min]", "pausa las llamadas proactivas (30 min por defecto)"),
    "/buscar": ("/buscar <consulta>", "consulta la red"),
    "/reporte": ("/reporte", "reporte del sistema (telemetría)"),
    "/bitacora": ("/bitacora", "última entrada de la bitácora"),
    "/devolver_llamada": ("/devolver_llamada", "retoma la última llamada de Amadeus"),
    "/hud": ("/hud", "enlace privado del HUD (solo en esta terminal)"),
    "/logro": ("/logro <texto>", "registra un logro de la sesión"),
    "/vision": ("/vision on|off|pausa [min]|estado", "visión continua local (cámara; opt-in)"),
    "/pantalla": ("/pantalla off|metadatos|pixeles", "qué mira la visión de la pantalla"),
    "/travesuras": ("/travesuras on|off", "travesuras con el cursor real (opt-in)"),
}

_CATEGORIAS_OBLIGATORIAS: Final = (
    "saludo_manana", "saludo_tarde", "saludo_noche", "saludo_madrugada", "regreso", "regreso_con_llamadas",
    "despedida", "apodo_1", "apodo_2", "apodo_3", "apodo_chuunibyou", "cumplido", "agradecimiento",
    "acusacion_tsundere", "tristeza", "broca_offline", "generico", "alerta", "reporte", "recordatorio",
    "capsula", "vistazo_presente", "vistazo_ausente", "vistazos_mientras_ausente", "iniciativa_conversar",
    "iniciativa_curiosidad_visual", "iniciativa_descanso", "iniciativa_cambio_linea", "accion_ok",
    "accion_denegada", "busqueda_resultados", "busqueda_sin_resultados", "confirmacion_requerida",
    "error_propio", "nexus_trabajo_terminado", "riesgo_9090", "logro", "bitacora",
    # v0.2
    "iniciativa_curiosidad_web", "iniciativa_conversar_app", "escritorio_aparece", "escritorio_te_busca",
    "travesura_cursor", "basta_travesuras", "vision_pausada", "prerrellenar_confirma", "escribir_confirma",
    "escribir_haz_clic", "accion_abrir_ok", "confirmacion_pendiente_chat", "sin_borrador", "sin_borrador_sin_llm",
    "vistazo_presente_continua", "vistazo_ausente_continua", "iniciativa_curiosidad_visual_continua",
)
_CATEGORIAS_MINIMO_2: Final = frozenset({"bitacora", "logro"})

_SEPARADOR: Final = "─" * 60

# ---------------------------------------------------------------------------
# Utilidades de formato (español, §3.5)
# ---------------------------------------------------------------------------


def _miles(n: Any) -> str:
    """Entero con separador de miles por espacio: ``12483 → "12 483"``."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _plural(n: int, singular: str, plural: str) -> str:
    """``"1 búsqueda"`` / ``"2 búsquedas"``."""
    return f"{n} {singular if n == 1 else plural}"


def formatear_duracion(segundos: float) -> str:
    """Duración para la despedida y la bitácora: ``"3 h 12 min"``, ``"12 min"`` o ``"45 s"``."""
    try:
        total = max(0, int(segundos))
    except (TypeError, ValueError, OverflowError):
        total = 0
    horas, resto = divmod(total, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas} h {minutos} min" if minutos else f"{horas} h"
    if minutos:
        return f"{minutos} min"
    return f"{segs} s"


def formatear_duracion_corta(segundos: Any) -> str:
    """Duración de un trabajo: ``"5 min 12 s"``, ``"45 s"`` o ``"1 h 3 min"``."""
    try:
        total = max(0, int(float(segundos)))
    except (TypeError, ValueError, OverflowError):
        total = 0
    horas, resto = divmod(total, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas} h {minutos} min"
    if minutos:
        return f"{minutos} min {segs} s" if segs else f"{minutos} min"
    return f"{segs} s"


def fecha_larga(momento: datetime) -> str:
    """``"16 de octubre de 2026"``."""
    return f"{momento.day} de {_MESES[momento.month - 1]} de {momento.year}"


def franja_horaria(momento: datetime) -> str:
    """Franja del saludo: ``manana`` (05–11), ``tarde`` (12–18), ``noche`` (19–23), ``madrugada`` (00–04)."""
    hora = momento.hour
    if 5 <= hora <= 11:
        return "manana"
    if 12 <= hora <= 18:
        return "tarde"
    if 19 <= hora <= 23:
        return "noche"
    return "madrugada"


def describir_vencimiento(vence: datetime, ahora: datetime) -> str:
    """``"hoy a las 18:00"``, ``"mañana a las 08:00"``, ``"el jueves a las 18:00"`` o fecha larga."""
    if vence.tzinfo is not None and ahora.tzinfo is not None:
        vence = vence.astimezone(ahora.tzinfo)
    hora = vence.strftime("%H:%M")
    dias = (vence.date() - ahora.date()).days
    if dias == 0:
        return f"hoy a las {hora}"
    if dias == 1:
        return f"mañana a las {hora}"
    if 1 < dias < 7:
        return f"el {_DIAS[vence.weekday()]} a las {hora}"
    return f"el {fecha_larga(vence)} a las {hora}"


def _fecha_local_desde_iso(texto: str, zona: Any) -> str:
    """Fecha larga local a partir de una marca UTC ISO (``""`` si no se puede leer)."""
    try:
        momento = datetime.fromisoformat(str(texto))
    except (TypeError, ValueError):
        return ""
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    try:
        momento = momento.astimezone(zona)
    except (ValueError, OverflowError):
        return ""
    return fecha_larga(momento)


def _con_el_psy_kongroo(reply: BrocaReply) -> BrocaReply:
    """Añade " El Psy Kongroo." al texto del último segmento (despedida especial, §6.6)."""
    if not reply.segmentos:
        return BrocaReply.de_texto("El Psy Kongroo.")
    segmentos = list(reply.segmentos)
    ultimo = segmentos[-1]
    texto = ultimo.texto.rstrip()
    segmentos[-1] = dataclasses.replace(ultimo, texto=f"{texto} El Psy Kongroo.".strip())
    return dataclasses.replace(reply, segmentos=tuple(segmentos))


_ARGS_CITA_USUARIO: Final = ("consulta", "contenido", "mensaje")

# Canal del turno que está ejecutando una AccionPC. Es un ContextVar (no un atributo) para que
# dos acciones concurrentes no se pisen: ``permisos.autorizar`` publica ``permiso.solicitado``
# dentro de la misma tarea y el manejador síncrono lo lee para no hablar las confirmaciones
# pedidas desde el chat de escritorio (labios mudos, §C1.5, §C2.14.3).
_CANAL_ACCION: ContextVar[str | None] = ContextVar("rams_canal_accion", default=None)


def _intencion_redactada(intencion: Intencion, secretos: Sequence[str], *, alta_entropia: bool) -> Intencion:
    """``intencion`` con sus argumentos de texto del usuario redactados (§4.1.3, §C2.14.3).

    El enrutador saca ``consulta`` (búsqueda web, memoria), ``contenido`` (recordar) y
    ``mensaje`` (programar, recordatorio personal) del texto **crudo**; aquí pasan por la misma
    redacción que ``texto_seguro`` (con ``alta_entropia`` en el chat de escritorio) antes de
    llegar a la memoria, la agenda, el bus, la red o Broca. Las ``AccionPC`` no se tocan: su
    texto lo filtra la compuerta (``parece_secreto``). Nunca lanza.
    """
    args = dict(intencion.args)
    cambio = False
    for clave in _ARGS_CITA_USUARIO:
        valor = args.get(clave)
        if isinstance(valor, str) and valor:
            redactado = redactar(valor, secretos, alta_entropia=alta_entropia)
            if redactado != valor:
                args[clave] = redactado
                cambio = True
    return dataclasses.replace(intencion, args=args) if cambio else intencion


def _con_detalle(reply: BrocaReply, detalle: str) -> BrocaReply:
    """Garantiza que una alerta lleve sus cifras exactas (§8: "Plantilla J.A.R.V.I.S. con las cifras exactas").

    # CONTRATO: frase_local("alerta", detalle=…) puede elegir una entrada del banco sin ``{detalle}``;
    # en ese caso se añade el detalle como segundo segmento (o al final del segundo, si ya hay dos),
    # porque una alerta por Telegram sin cifras no sirve de nada.
    """
    detalle = detalle.strip().rstrip(".")
    if not detalle or detalle in reply.texto_plano:
        return reply
    extra = f"Detalle: {detalle}."
    segmentos = list(reply.segmentos)
    if not segmentos:
        return BrocaReply.de_texto(extra, Etiquetas("seria", "senalar", "serio"))
    if len(segmentos) >= 2:
        ultimo = segmentos[-1]
        segmentos[-1] = dataclasses.replace(ultimo, texto=f"{ultimo.texto.rstrip()} {extra}".strip())
    else:
        segmentos.append(Segmento(segmentos[0].etiquetas, extra))
    return dataclasses.replace(reply, segmentos=tuple(segmentos))


async def _quizas_await(valor: Any) -> Any:
    """Espera ``valor`` si es *awaitable* (tolera APIs síncronas o asíncronas)."""
    if inspect.isawaitable(valor):
        return await valor
    return valor


# ---------------------------------------------------------------------------
# Estado persistente de la v0.2 (travesuras, auditoría, agenda "búscame")
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class EstadoTravesuras:
    """Estado de las travesuras con el cursor real (dueño único: main, §C2.14.4).

    Se persiste en ``data/travesuras.json`` (0o600). El estado **efectivo** además exige el
    interruptor maestro ``RAMS_TRAVESURAS`` y que el usuario no haya pedido "no molestar".
    """

    activas: bool = True
    apagadas_hasta_ts: float | None = None      # reloj de pared (sobrevive a reinicios)
    basta_hoy: int = 0
    fecha: str = ""                              # "YYYY-MM-DD" local del último "basta"

    def a_dict(self) -> dict[str, Any]:
        """Dict JSON-serializable."""
        return {"activas": bool(self.activas), "apagadas_hasta_ts": self.apagadas_hasta_ts,
                "basta_hoy": int(self.basta_hoy), "fecha": str(self.fecha)}

    @classmethod
    def desde_dict(cls, datos: Any) -> EstadoTravesuras:
        """Estado validado desde el JSON guardado (cualquier valor raro vuelve al defecto)."""
        if not isinstance(datos, Mapping):
            return cls()
        activas = datos.get("activas")
        hasta = datos.get("apagadas_hasta_ts")
        basta = datos.get("basta_hoy")
        fecha = datos.get("fecha")
        hasta_ok = (isinstance(hasta, (int, float)) and not isinstance(hasta, bool)
                    and hasta == hasta and abs(float(hasta)) != float("inf"))
        return cls(activas=activas if isinstance(activas, bool) else True,
                   apagadas_hasta_ts=float(hasta) if hasta_ok else None,
                   basta_hoy=basta if isinstance(basta, int) and not isinstance(basta, bool) and basta >= 0 else 0,
                   fecha=fecha if isinstance(fecha, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha) else "")


def _escribir_json_privado(ruta: Path, datos: Any) -> None:
    """Escribe ``datos`` como JSON de forma atómica (temporal + ``os.replace``) con permisos 0o600.

    Bloqueante: se llama con ``asyncio.to_thread``.
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(prefix=f".{ruta.name}.", suffix=".tmp", dir=str(ruta.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as archivo:
            json.dump(datos, archivo, ensure_ascii=False)
        try:
            os.chmod(temporal, 0o600)
        except OSError as exc:  # Windows ignora casi todo el modo; no es un error
            log.debug("chmod 0o600 no aplicado a %s: %s", ruta.name, type(exc).__name__)
        for intento in range(5):
            try:
                os.replace(temporal, ruta)
                break
            except PermissionError:
                # Windows: el destino puede estar abierto un instante por otro lector (antivirus,
                # indexador…); se reintenta en este mismo hilo de trabajo (nunca en el loop).
                if intento == 4:
                    raise
                time.sleep(0.05 * (intento + 1))
    except BaseException:
        try:
            os.unlink(temporal)
        except OSError:
            pass
        raise


def _leer_json(ruta: Path) -> Any:
    """JSON de ``ruta`` o ``None`` si no existe o no se puede leer (bloqueante: ``to_thread``)."""
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("No se pudo leer %s (%s); uso valores por defecto", ruta.name, type(exc).__name__)
        return None


def _anexar_jsonl_privado(ruta: Path, registro: Mapping[str, Any]) -> None:
    """Añade una línea JSON a un archivo *append-only* creado con 0o600 (bloqueante: ``to_thread``)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    linea = (json.dumps(dict(registro), ensure_ascii=False) + "\n").encode("utf-8")
    banderas = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    descriptor = os.open(str(ruta), banderas, 0o600)
    try:
        os.write(descriptor, linea)
    finally:
        os.close(descriptor)


def _nombre_app(objetivo: Any) -> str:
    """Nombre corto de una app a partir de un objetivo de ventana (``chrome.exe`` → ``chrome``)."""
    exe = str(getattr(objetivo, "exe", "") or getattr(objetivo, "clase", "") or "")
    exe = _EXE_SEGURO_RE.sub("", exe).strip()
    if exe.lower().endswith(".exe"):
        exe = exe[:-4]
    return exe[:40]


# ---------------------------------------------------------------------------
# Consola (§6.5)
# ---------------------------------------------------------------------------

_ANSI: Final = {
    "reset": "\x1b[0m",
    "verde": "\x1b[32m",
    "ambar": "\x1b[33m",
    "naranja": "\x1b[38;5;208m",
    "gris": "\x1b[90m",
    "rojo": "\x1b[91m",
    "azul": "\x1b[94m",
    "nixie": "\x1b[38;5;214m",
    "negrita": "\x1b[1m",
}
_COLOR_ESTADO: Final = {
    EstadoModulo.OK: "verde",
    EstadoModulo.EN_ESPERA: "ambar",
    EstadoModulo.SIN_CONEXION: "naranja",
    EstadoModulo.DEGRADADO: "naranja",
    EstadoModulo.OFF: "gris",
}
_FIN_LINEA_CTRL_C: Final = "\x00rams-ctrl-c\x00"   # centinela interno (nunca llega al usuario)


def _es_tty(flujo: Any) -> bool:
    """``flujo.isatty()`` sin lanzar (``False`` con StringIO, None o flujos cerrados)."""
    try:
        return bool(flujo.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _activar_vt_windows() -> bool:
    """Activa ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` en la consola de Windows con ``ctypes``.

    Devuelve ``True`` si la consola acepta secuencias ANSI. Si falla, se desactivan colores
    y secuencias (§6.4, punto 1).
    """
    try:
        import ctypes  # solo Windows

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        manejador = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        modo = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(manejador, ctypes.byref(modo)):
            return False
        if modo.value & 0x0004:                          # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(kernel32.SetConsoleMode(manejador, modo.value | 0x0004))
    except (AttributeError, OSError, ValueError, ImportError) as exc:
        log.debug("No se pudo activar el modo VT de la consola de Windows: %s", exc)
        return False


class Consola:
    """Consola de Amadeus: prompts fijos, salida asíncrona segura, indicador de cámara y entrada.

    - Prompts: ``[{rango_lab}] › `` para el usuario y ``[Amadeus] › `` para el agente.
    - Firmas fijas usadas por otros grupos: ``imprimir_sistema(str)`` (``preguntar`` de la
      compuerta de permisos) e ``imprimir_amadeus(str, Etiquetas | None)`` (``imprimir`` de riko).
    - Entrada con ``prompt_toolkit`` (``prompt_async`` + ``patch_stdout``) si está instalado y
      stdin es TTY; si no, un hilo daemon ``rams-stdin``.
    """

    PROMPT_AMADEUS: Final = "[Amadeus] › "

    def __init__(self, settings: Settings, *, salida: TextIO | None = None,
                 usar_prompt_toolkit: bool | None = None) -> None:
        self.settings = settings
        self._salida = salida
        flujo = salida if salida is not None else sys.stdout
        self.es_tty = _es_tty(flujo)
        self.stdin_es_tty = _es_tty(sys.stdin)
        self.ansi = self.es_tty
        if self.es_tty and sys.platform == "win32":
            self.ansi = _activar_vt_windows()
        self.colores = self.ansi and "NO_COLOR" not in os.environ
        disponible = PromptSession is not None and patch_stdout is not None
        if usar_prompt_toolkit is None:
            self.usar_prompt_toolkit = disponible and self.es_tty and self.stdin_es_tty
        else:
            self.usar_prompt_toolkit = bool(usar_prompt_toolkit) and disponible
        self.prompt_usuario = f"[{settings.rango_lab}] › "
        self.motivo_fin: str | None = None     # "eof" | "señal" tras al_linea(None)
        self._lock = threading.RLock()
        self._prompt_visible = False
        self._en_prompt_toolkit = False
        self._cerrada = False
        self._hilo: threading.Thread | None = None
        self._cola: asyncio.Queue[str | None] | None = None
        self._loop_cola: asyncio.AbstractEventLoop | None = None

    @property
    def salida(self) -> TextIO:
        """Flujo de salida (``sys.stdout`` actual por defecto, para que ``patch_stdout`` lo intercepte)."""
        return self._salida if self._salida is not None else sys.stdout

    # --- escritura -------------------------------------------------------

    def _color(self, texto: str, color: str | None) -> str:
        if not color or not self.colores:
            return texto
        return f"{_ANSI.get(color, '')}{texto}{_ANSI['reset']}"

    def _escribir(self, texto: str) -> None:
        """Escribe y vacía el búfer; tolera flujos cerrados y codificaciones limitadas."""
        flujo = self.salida
        try:
            flujo.write(texto)
            flujo.flush()
        except UnicodeEncodeError:
            codificacion = getattr(flujo, "encoding", None) or "ascii"
            try:
                flujo.write(texto.encode(codificacion, "replace").decode(codificacion, "replace"))
                flujo.flush()
            except (OSError, ValueError, LookupError) as exc:
                log.debug("No se pudo escribir en la consola: %s", exc)
        except (OSError, ValueError, AttributeError) as exc:
            log.debug("No se pudo escribir en la consola: %s", exc)

    def _emitir(self, linea: str) -> None:
        """Imprime una línea completa respetando el prompt que el usuario tiene a la vista.

        - Con ``prompt_toolkit`` activo (``patch_stdout``) o sin TTY: la línea tal cual.
        - TTY con ANSI y prompt visible: ``"\\r\\x1b[2K" + línea`` y se reimprime el prompt.
        - Windows sin VT: ``"\\n" + línea`` (sin secuencias ANSI).
        """
        with self._lock:
            if self._en_prompt_toolkit or not self.es_tty:
                self._escribir(linea + "\n")
            elif not self.ansi:
                if self._prompt_visible:
                    self._escribir("\n" + linea + "\n" + self.prompt_usuario)
                else:
                    self._escribir(linea + "\n")
            elif self._prompt_visible:
                self._escribir("\r\x1b[2K" + linea + "\n" + self.prompt_usuario)
            else:
                self._escribir(linea + "\n")

    def imprimir(self, texto: str, color: str | None = None) -> None:
        """Imprime texto del sistema sin prefijo (cabecera y líneas del arranque)."""
        lineas = str(texto).split("\n") or [""]
        for linea in lineas:
            self._emitir(self._color(linea, color))

    def imprimir_sistema(self, texto: str) -> None:
        """Líneas del sistema (gris; ámbar para avisos y confirmaciones ``⚠``)."""
        for linea in str(texto).split("\n") or [""]:
            color = "ambar" if linea.lstrip().startswith("⚠") else "gris"
            self._emitir(self._color(linea, color))

    def imprimir_amadeus(self, texto: str, etiquetas: Etiquetas | None = None) -> None:
        """``"[Amadeus] › texto"`` (las etiquetas ya vienen aplicadas al avatar; aquí no se muestran)."""
        prefijo = self._color(self.PROMPT_AMADEUS, "azul")
        self._emitir(prefijo + str(texto))

    def indicador_camara(self, activa: bool, duracion_ms: int | None = None) -> None:
        """``● CÁMARA ACTIVA`` en rojo al abrirla; ``○ cámara apagada (312 ms)`` en gris al soltarla."""
        if activa:
            self._emitir(self._color("● CÁMARA ACTIVA", "rojo"))
            return
        try:
            ms = max(0, int(duracion_ms or 0))
        except (TypeError, ValueError):
            ms = 0
        self._emitir(self._color(f"○ cámara apagada ({ms} ms)", "gris"))

    def linea_arranque(self, t: float, etiqueta: str, valor: str,
                       estado: EstadoModulo | None = None) -> None:
        """``[SS.mmm] Etiqueta ......... valor`` con el estado coloreado (§6.4)."""
        marca = f"[{max(0.0, t):06.3f}]"
        relleno = f"{etiqueta} ".ljust(44, ".")
        if estado is not None and self.colores:
            texto_estado = estado.value
            indice = valor.find(texto_estado)
            if indice >= 0:
                valor = (valor[:indice] + self._color(texto_estado, _COLOR_ESTADO.get(estado))
                         + valor[indice + len(texto_estado):])
        self._emitir(f"{self._color(marca, 'gris')} {relleno} {valor}")

    async def rueda_nixie(self, prefijo: str, valores: Sequence[str], *, cuadros: int = 12,
                          periodo_s: float = 0.04) -> None:
        """Rueda nixie: cada valor "rueda" ``cuadros`` cuadros fijando los dígitos de izquierda a derecha.

        Sin TTY (o con ``prompt_toolkit`` activo) imprime directamente ``a → b → c``.
        """
        valores = [str(v) for v in valores]
        # "\r" solo vuelve al inicio de la fila física: si la línea no cabe en el ancho de la
        # terminal, se parte en dos y la animación deja basura repetida. En ese caso, sin animar.
        visible = len(re.sub(r"\x1b\[[0-9;]*m", "", prefijo)) + len(" → ".join(valores))
        no_cabe = visible >= shutil.get_terminal_size((80, 24)).columns
        if not (self.es_tty and self.ansi) or self._en_prompt_toolkit or not valores or no_cabe:
            self._emitir(prefijo + " → ".join(self._color(v, "nixie") for v in valores))
            return
        azar = random.Random()
        fijados: list[str] = []
        with self._lock:
            prompt_visible = self._prompt_visible
            if prompt_visible:
                self._escribir("\r\x1b[2K")
        for valor in valores:
            for cuadro in range(max(1, cuadros)):
                fijos = (len(valor) * (cuadro + 1)) // max(1, cuadros)
                rodando = "".join(c if (i < fijos or not c.isdigit()) else str(azar.randrange(10))
                                  for i, c in enumerate(valor))
                partes = [*(self._color(v, "nixie") for v in fijados), self._color(rodando, "nixie")]
                with self._lock:
                    self._escribir("\r" + prefijo + " → ".join(partes))
                await asyncio.sleep(periodo_s)
            fijados.append(valor)
        with self._lock:
            final = prefijo + " → ".join(self._color(v, "nixie") for v in fijados)
            self._escribir("\r\x1b[2K" + final + "\n" + (self.prompt_usuario if prompt_visible else ""))

    # --- entrada ---------------------------------------------------------

    def _mostrar_prompt(self) -> None:
        """Muestra el prompt del usuario (solo con stdin y stdout TTY)."""
        if not (self.es_tty and self.stdin_es_tty) or self._cerrada:
            return
        with self._lock:
            if not self._prompt_visible:
                self._escribir(self._color(self.prompt_usuario, "negrita") if self.colores
                               else self.prompt_usuario)
                self._prompt_visible = True

    async def bucle_entrada(self, parar: asyncio.Event,
                            al_linea: Callable[[str | None], Awaitable[None]]) -> None:
        """Lee líneas y llama a ``al_linea(linea)``; en EOF llama a ``al_linea(None)`` y termina."""
        if self.usar_prompt_toolkit:
            await self._bucle_prompt_toolkit(parar, al_linea)
        else:
            await self._bucle_hilo(parar, al_linea)

    def _leer_stdin(self, loop: asyncio.AbstractEventLoop, cola: asyncio.Queue[str | None]) -> None:
        """Hilo ``rams-stdin``: ``readline`` → ``call_soon_threadsafe(cola.put_nowait, línea)``; EOF → ``None``."""
        while True:
            try:
                linea = sys.stdin.readline()
            except (OSError, ValueError, UnicodeDecodeError, AttributeError) as exc:
                log.debug("Lectura de stdin interrumpida: %s", exc)
                linea = ""
            dato = linea.rstrip("\r\n") if linea else None
            try:
                loop.call_soon_threadsafe(cola.put_nowait, dato)
            except RuntimeError:   # el loop ya se cerró
                return
            if dato is None:
                return

    async def _bucle_hilo(self, parar: asyncio.Event,
                          al_linea: Callable[[str | None], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()
        if self._cola is None or self._loop_cola is not loop:
            self._cola = asyncio.Queue()
            self._loop_cola = loop
            self._hilo = None
        if self._hilo is None or not self._hilo.is_alive():
            # Un único lector por consola: si la tarea se relanza, se reutiliza el hilo vivo.
            self._hilo = threading.Thread(target=self._leer_stdin, args=(loop, self._cola),
                                          name="rams-stdin", daemon=True)
            self._hilo.start()
        cola = self._cola
        while not parar.is_set() and not self._cerrada:
            self._mostrar_prompt()
            obtener = asyncio.ensure_future(cola.get())
            esperar = asyncio.ensure_future(parar.wait())
            try:
                hechas, _ = await asyncio.wait({obtener, esperar}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for futuro in (obtener, esperar):
                    if not futuro.done():
                        futuro.cancel()
            if obtener not in hechas:
                break
            linea = obtener.result()
            with self._lock:
                self._prompt_visible = False
            if linea is None:
                self.motivo_fin = "eof"
            await al_linea(linea)
            if linea is None:
                break

    async def _pedir_linea(self, sesion: Any) -> str | None:
        """``prompt_async`` envuelto: Ctrl+C y Ctrl+D nunca escapan como excepción del loop."""
        try:
            return await sesion.prompt_async(self.prompt_usuario)
        except KeyboardInterrupt:
            return _FIN_LINEA_CTRL_C
        except EOFError:
            return None

    async def _bucle_prompt_toolkit(self, parar: asyncio.Event,
                                    al_linea: Callable[[str | None], Awaitable[None]]) -> None:
        atajos = None
        if KeyBindings is not None:
            atajos = KeyBindings()

            @atajos.add("c-c")
            def _ctrl_c(evento: Any) -> None:  # Ctrl+C: fin de sesión ordenado
                evento.app.exit(result=_FIN_LINEA_CTRL_C)

        sesion = PromptSession(key_bindings=atajos) if atajos is not None else PromptSession()
        with patch_stdout(raw=True):
            self._en_prompt_toolkit = True
            redirigidos = self._redirigir_log_consola()
            try:
                while not parar.is_set() and not self._cerrada:
                    pedir = asyncio.ensure_future(self._pedir_linea(sesion))
                    esperar = asyncio.ensure_future(parar.wait())
                    try:
                        hechas, _ = await asyncio.wait({pedir, esperar}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        if not esperar.done():
                            esperar.cancel()
                        if not pedir.done():
                            pedir.cancel()
                            await asyncio.wait({pedir}, timeout=1.0)
                    if pedir not in hechas or pedir.cancelled():
                        break
                    linea = pedir.result()
                    if linea is None or linea == _FIN_LINEA_CTRL_C:
                        self.motivo_fin = "señal" if linea == _FIN_LINEA_CTRL_C else "eof"
                        await al_linea(None)
                        break
                    await al_linea(linea)
            finally:
                self._en_prompt_toolkit = False
                for manejador, flujo in redirigidos:
                    try:
                        manejador.setStream(flujo)
                    except (AttributeError, ValueError) as exc:
                        log.debug("No se pudo restaurar el flujo del log: %s", exc)

    @staticmethod
    def _redirigir_log_consola() -> list[tuple[logging.StreamHandler, Any]]:
        """Dentro de ``patch_stdout``, el handler de consola de ``configurar_logging`` escribe en el
        ``sys.stderr`` parcheado: así los avisos aparecen encima del prompt sin borrarlo."""
        redirigidos: list[tuple[logging.StreamHandler, Any]] = []
        for manejador in logging.getLogger().handlers:
            if getattr(manejador, "_rams_tipo", "") == "consola" and isinstance(manejador, logging.StreamHandler):
                anterior = manejador.setStream(sys.stderr)
                if anterior is not None:
                    redirigidos.append((manejador, anterior))
        return redirigidos

    def cerrar(self) -> None:
        """Marca la consola como cerrada (el hilo daemon de stdin muere con el proceso)."""
        self._cerrada = True
        with self._lock:
            if self._prompt_visible and self.es_tty and self.ansi:
                self._escribir("\r\x1b[2K")
            self._prompt_visible = False


# ---------------------------------------------------------------------------
# Tareas supervisadas (§6.3)
# ---------------------------------------------------------------------------


async def _esperar_evento(evento: asyncio.Event, segundos: float) -> bool:
    """Espera ``segundos`` o hasta que ``evento`` se active; ``True`` si se activó."""
    try:
        await asyncio.wait_for(evento.wait(), timeout=max(0.0, segundos))
    except asyncio.TimeoutError:
        return evento.is_set()
    return True


async def supervisar(nombre: str, fabrica: Callable[[], Awaitable[None]], parar: asyncio.Event,
                     *, reintentos: int = 5, backoff_s: float = 2.0, backoff_max_s: float = 60.0,
                     bus: BusEventos | None = None) -> None:
    """Ejecuta ``fabrica()`` y la relanza con *backoff* exponencial si termina con una excepción.

    - Tras ``reintentos`` fallos consecutivos la tarea queda desactivada y, si hay ``bus``, se
      publica ``sistema.modulo`` con ``DEGRADADO``.
    - Con ``parar`` activo no se reintenta. Una ejecución que duró más que ``backoff_max_s``
      reinicia la cuenta de fallos consecutivos.
    - ``bus`` es una extensión de la firma de §6.3 (opcional) para poder publicar la desactivación.
    """
    fallos = 0
    espera = max(0.0, float(backoff_s))
    while not parar.is_set():
        inicio = time.monotonic()
        try:
            await fabrica()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if parar.is_set():
                log.debug("Tarea %s terminó con %s durante el apagado", nombre, type(exc).__name__)
                return
            if time.monotonic() - inicio >= backoff_max_s:
                fallos = 0
                espera = max(0.0, float(backoff_s))
            fallos += 1
            if fallos >= reintentos:
                log.error("Tarea %s desactivada tras %d fallos consecutivos: %s", nombre, fallos, exc)
                if bus is not None:
                    clave = _CLAVE_TAREA.get(nombre, nombre)
                    diag = DiagnosticoModulo(clave, _NOMBRES_MODULO.get(clave, nombre), EstadoModulo.DEGRADADO,
                                             f"tarea {nombre} desactivada tras {fallos} fallos")
                    bus.publicar("sistema.modulo", diag.a_dict(), origen="main")
                return
            log.warning("Tarea %s falló: %s; reintento en %.0f s", nombre, exc, espera)
            if await _esperar_evento(parar, espera):
                return
            espera = min(max(espera * 2.0, 0.001), float(backoff_max_s))


# ---------------------------------------------------------------------------
# Importación de módulos
# ---------------------------------------------------------------------------


def _importar_obligatorio(ruta: str, nombre: str) -> Any:
    """Importa un componente P0; si falla, lanza ``RuntimeError`` con un mensaje claro."""
    try:
        return getattr(importlib.import_module(ruta), nombre)
    except Exception as exc:
        raise RuntimeError(f"Módulo obligatorio no disponible: {ruta}.{nombre} "
                           f"({type(exc).__name__}: {exc})") from exc


def _detalle_import(ruta: str, exc: BaseException) -> str:
    """Detalle corto para el diagnóstico de un módulo opcional que no se pudo cargar."""
    if isinstance(exc, ModuleNotFoundError):
        faltante = getattr(exc, "name", None) or ""
        if faltante and (ruta == faltante or ruta.startswith(faltante + ".")):
            return "módulo no instalado"
        return f"falta la dependencia {faltante or '?'}"
    return f"error al construir: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------


class Orquestador:
    """Orquestador asyncio de R.A.M.S. BIO-CORE (§10.21).

    Los atributos de módulos quedan en ``None`` hasta ``iniciar()``; los P1/P2 pueden seguir
    en ``None`` si su módulo falta o falla al construirse.
    """

    def __init__(self, settings: Settings, *, cliente: Any = None, consola: Consola | None = None) -> None:
        self.settings = settings
        self.consola: Consola = consola if consola is not None else Consola(settings)
        self.bus: BusEventos = BusEventos()
        self.parar: asyncio.Event = asyncio.Event()
        self.memoria: Any = None
        self.conectoma: Any = None
        self.broca: Any = None
        self.web: Any = None
        self.vtube: Any = None
        self.avatar3d: Any = None
        self.corteza: Any = None     # corteza motora: el conectoma mueve el avatar 3D
        self.aura: Any = None
        self.permisos: Any = None
        self.presencia: Any = None
        self.riko: Any = None
        self.hud: Any = None
        self.control: Any = None
        self.control_pc: Any = None  # v0.2: lobes/control_pc.py (apps, teclado y cursor)
        self.vision: Any = None      # v0.2: lobes/vision_continua.py (cámara y pantalla en local)
        self._cliente = cliente
        self._cliente_propio = False
        # Estado propio (§10.21)
        self._apagando = False
        self._apagado_iniciado = False
        self._iniciado = False
        self._lock_turno = asyncio.Lock()
        self._ventana_previa: dict[str, Any] | None = None
        self._ultimo_nexus: dict[str, Any] | None = None
        self._ultima_alerta_critica: tuple[float, str] | None = None
        self._regreso_pendiente = False
        # Recordatorio a medias esperando la hora o el mensaje (CONTRATO de cierre, §10.12):
        # {"falta", "tipo", "mensaje", "vence", "canal", "expira"}.
        self._programar_pendiente: dict[str, Any] | None = None
        self._ausente_desde_ts: float | None = None
        self._vistazos_pendientes = 0
        self._logro_sesion = False
        self._primer_turno = True
        self._colab_ultimo_por_emisor: dict[str, float] = {}
        self._colab_ultimo_global = float("-inf")
        self._riesgo_avisado = False
        self._motivo_parada: str | None = None
        self._presente_publicado: bool | None = None
        self._ultimo_campo: str | None = None
        self._ultima_fuente: str | None = None
        self._ultimo_vistazo_presencia = float("-inf")
        self._diagnosticos: dict[str, DiagnosticoModulo] = {}
        self._diag_ausentes: dict[str, DiagnosticoModulo] = {}
        self._diags_arranque: list[DiagnosticoModulo] = []
        self._tareas: dict[str, asyncio.Task[Any]] = {}
        self._tareas_sueltas: set[asyncio.Task[Any]] = set()
        self._desuscribir: list[Callable[[], None]] = []
        self._senales: list[int] = []
        self._inicio_ts = time.time()
        self._inicio_mono = time.monotonic()
        self._stats = {"turnos": 0, "busquedas": 0, "vistazos": 0, "iniciativas": 0}
        self._temas: deque[str] = deque(maxlen=3)
        self._fragmentos_inicio: int | None = None
        # --- v0.2 (§C2.14) ---
        # Relojes inyectables (tests): monotónico para ritmos y de pared para lo persistido.
        self._reloj: Callable[[], float] = time.monotonic
        self._reloj_pared: Callable[[], float] = time.time
        self._chat_en_espera: deque[str] = deque(maxlen=CHAT_EN_ESPERA_MAX)
        self._overlay_conectado = False          # escritorio.conectado.overlay (Electron conectado)
        self._overlay_visible = False            # escritorio.evento mostrada/ocultada
        self._travesuras = EstadoTravesuras()
        self._travesuras_publicadas: tuple[bool, float | None] | None = None
        self._ultima_travesura_mono: float | None = None
        self._no_molestar_usuario = False
        self._no_molestar_sistema: tuple[bool, str] = (False, "")
        self._no_molestar_publicado: tuple[bool, str] | None = None
        self._lock_archivos = asyncio.Lock()     # serializa las escrituras de data/*.json
        self._rng = random.Random()
        self._ultimo_borrador: tuple[str, float] | None = None
        self._ultimo_destino: tuple[str, float] | None = None
        self._ultima_pantalla: dict[str, Any] | None = None
        self._ultima_pantalla_ts = float("-inf")
        self._ultimo_estado_vision: dict[str, Any] | None = None
        self._ultimo_rostro: dict[str, Any] | None = None
        self._ultima_atencion: float | None = None
        self._toques: deque[float] = deque(maxlen=64)
        self._ultima_inyeccion_escritorio = float("-inf")
        self._ultima_inyeccion_pantalla = float("-inf")
        self._ultima_firma_pantalla: str | None = None
        self._ultimo_refresco_rostro = float("-inf")
        self._camaras_activas: dict[str, bool] = {}
        self._indicador_camara = False
        self._curiosidad_web_ts: list[float] = []          # reloj de pared
        self._temas_curiosidad: list[str] | None = None
        self._temas_recientes: deque[str] = deque(maxlen=5)
        self._agenda_buscar: set[int] = set()
        self._ultimo_turno_local_mono = float("-inf")

    # ------------------------------------------------------------------
    # Construcción y arranque
    # ------------------------------------------------------------------

    def _construir_opcional(self, ruta: str, clase: str, claves: Sequence[str],
                            fabrica: Callable[[Any], Any]) -> Any:
        """Importa y construye un módulo P1/P2 dentro de ``try``; si falla, ``None`` + diagnóstico OFF."""
        try:
            tipo = getattr(importlib.import_module(ruta), clase)
            return fabrica(tipo)
        except Exception as exc:
            detalle = _detalle_import(ruta, exc)
            if detalle == "módulo no instalado":
                log.info("%s no está instalado; sigo sin él", ruta)
            else:
                log.warning("No se pudo construir %s (%s); sigo sin él", ruta, detalle, exc_info=True)
            for clave in claves:
                self._diag_ausentes[clave] = DiagnosticoModulo(
                    clave, _NOMBRES_MODULO.get(clave, clave), EstadoModulo.OFF, detalle)
            return None

    # --- v0.2: construcción de ControlPC, visión y estado persistente ---------

    @staticmethod
    def _activar_dpi() -> None:
        """``control_pc.activar_dpi_por_monitor()`` si el módulo existe (nunca lanza)."""
        try:
            modulo = importlib.import_module("lobes.control_pc")
        except Exception as exc:  # módulo ausente o roto: _construir_opcional lo diagnostica
            log.debug("Sin lobes.control_pc para la consciencia de DPI (%s)", type(exc).__name__)
            return
        activar = getattr(modulo, "activar_dpi_por_monitor", None)
        if not callable(activar):
            return
        try:
            activar()
        except Exception as exc:  # contrato: no lanza; defensa en profundidad
            log.debug("activar_dpi_por_monitor falló: %s", type(exc).__name__)

    def _conectar_presencia_control_pc(self) -> None:
        """Inactividad de Windows y filtro de inyecciones propias (ControlPC → presencia)."""
        if self.control_pc is None or self.presencia is None:
            return
        conectar = getattr(self.presencia, "conectar_control_pc", None)
        if not callable(conectar):
            return
        inactividad = getattr(self.control_pc, "inactividad_s", None)
        inyeccion = getattr(self.control_pc, "ts_ultima_inyeccion", None)
        try:
            conectar(inactividad_pc=inactividad if callable(inactividad) else None,
                     ts_ultima_inyeccion=inyeccion if callable(inyeccion) else None)
        except Exception as exc:
            log.warning("No se pudo conectar ControlPC a la presencia: %s", type(exc).__name__)

    def _construir_vision(self) -> None:
        """``VisionContinua`` si ``tiene_vision_continua``; si no, diagnósticos ``OFF`` con el motivo."""
        s = self.settings
        claves = ("vision_camara", "vision_pantalla")
        if not getattr(s, "tiene_vision_continua", False):
            if not getattr(s, "vision_continua", False):
                detalle = "deshabilitada (RAMS_VISION_CONTINUA=0)"
            else:
                detalle = "requiere la cámara (RAMS_CAMERA=1)"
            for clave in claves:
                self._diag_ausentes[clave] = DiagnosticoModulo(clave, _NOMBRES_MODULO[clave], EstadoModulo.OFF,
                                                               detalle)
            return
        self.vision = self._construir_opcional(
            "lobes.vision_continua", "VisionContinua", claves,
            lambda cls: cls(s, self.bus, presencia=self.presencia))
        if self.vision is None or self.aura is None:
            return
        fijar = getattr(self.aura, "set_fuente_continua", None)
        fuente = getattr(self.vision, "como_vistazo", None)
        if callable(fijar) and callable(fuente):
            try:
                fijar(fuente)   # un solo dueño del dispositivo: los vistazos usan la muestra continua
            except Exception as exc:
                log.warning("No se pudo enlazar la visión continua con aura: %s", type(exc).__name__)
        else:
            log.info("aura no admite fuente continua (set_fuente_continua); pendiente de integración")

    def _ruta_datos(self, nombre: str) -> Path:
        return Path(self.settings.data_dir) / nombre

    async def _cargar_estado_v02(self) -> None:
        """Lee ``data/travesuras.json`` y ``data/agenda_buscar.json`` (fuera del loop)."""
        try:
            datos = await asyncio.to_thread(_leer_json, self._ruta_datos("travesuras.json"))
            self._travesuras = EstadoTravesuras.desde_dict(datos)
        except Exception as exc:
            log.warning("Estado de travesuras no disponible (%s); uso el defecto", type(exc).__name__)
            self._travesuras = EstadoTravesuras()
        try:
            ids = await asyncio.to_thread(_leer_json, self._ruta_datos("agenda_buscar.json"))
            if isinstance(ids, list):
                self._agenda_buscar = {i for i in ids if isinstance(i, int) and not isinstance(i, bool)}
        except Exception as exc:
            log.debug("Agenda de búsquedas no disponible (%s)", type(exc).__name__)

    def _pids_extra(self) -> frozenset[int]:
        """PIDs de Amadeus Desktop (y descendientes) que tampoco son objetivo de escribir_texto."""
        if self.control_pc is None:
            return frozenset()
        fuente = getattr(self.control_pc, "pids_excluidos", None)
        if not callable(fuente):
            return frozenset()
        try:
            return frozenset(fuente())
        except Exception as exc:
            log.debug("pids_excluidos no disponible (%s)", type(exc).__name__)
            return frozenset()

    async def iniciar(self, *, lanzar_tareas: bool = True, imprimir_arranque: bool = True) -> list[DiagnosticoModulo]:
        """Construye y conecta los módulos (orden normativo de §10.21), hace los diagnósticos y,
        si se pide, imprime el arranque Amadeus, saluda y lanza las tareas supervisadas.

        Los módulos P0 son obligatorios: si alguno falta, lanza ``RuntimeError``.
        """
        if self._iniciado:
            return list(self._diags_arranque)
        self._iniciado = True
        s = self.settings
        loop = asyncio.get_running_loop()
        self._inicio_ts = time.time()
        self._inicio_mono = time.monotonic()

        # 0. v0.2: consciencia de DPI por monitor, lo primero (antes de ControlPC y de la visión:
        # mss cambia la consciencia de DPI del proceso y las coordenadas dejarían de ser físicas).
        self._activar_dpi()

        # 1. Bus y manejador de log (con los secretos para el filtro).
        self.bus.vincular_loop(loop)
        instalar_manejador_log(self.bus, secretos=s.secretos())
        self._desuscribir.append(self.bus.suscribir("sistema.modulo", self._ev_modulo))
        if self._cliente is None and httpx is not None:
            self._cliente = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0),
                                              headers={"User-Agent": "RAMS-BioCore/0.1"})
            self._cliente_propio = True

        # 2. HUD antes que nadie: se suscribe en su __init__ y guarda todos los sistema.modulo.
        # v0.2: el mismo servidor sirve /escritorio (Amadeus Desktop y chat), así que arranca si
        # hay HUD **o** escritorio.
        tiene_escritorio = bool(getattr(s, "tiene_escritorio", False))
        if s.hud_habilitado or tiene_escritorio:
            claves_hud = ("hud", "escritorio") if tiene_escritorio else ("hud",)
            self.hud = self._construir_opcional("interface.hud_server", "ServidorHUD", claves_hud,
                                                lambda cls: cls(s, self.bus))
        else:
            self._diag_ausentes["hud"] = DiagnosticoModulo("hud", "HUD", EstadoModulo.OFF, "deshabilitado")
        if not tiene_escritorio:
            self._diag_ausentes["escritorio"] = DiagnosticoModulo(
                "escritorio", _NOMBRES_MODULO["escritorio"], EstadoModulo.OFF, "deshabilitado (RAMS_ESCRITORIO=0)")
        self.bus.publicar("sistema.arranque", {"version": VERSION, "offline": s.offline,
                                               "inicio_ts": self._inicio_ts}, origen="main")

        # 2b. v0.2: ControlPC (antes de la compuerta, que excluye sus pids, y de la presencia,
        # que usa su inactividad). Opcional: sin él, las acciones v0.2 responden "no disponible".
        self.control_pc = self._construir_opcional("lobes.control_pc", "ControlPC", ("control_pc",),
                                                   lambda cls: cls(s))

        # 3. Área de Broca.
        clase_broca = _importar_obligatorio("lobes.broca_llm", "BrocaLLM")
        self.broca = clase_broca(s, cliente=self._cliente)

        # 4. Memoria (Salieri consolida y lo anuncia por el bus).
        clase_memoria = _importar_obligatorio("core.memory_engine", "MemoryEngine")
        resumidor = self.broca.resumir if s.compresion_llm else None
        self.memoria = clase_memoria(s, resumidor=resumidor, al_consolidar=self._al_consolidar)
        diag_memoria = await self.memoria.abrir()
        t_memoria = time.monotonic() - self._inicio_mono
        estadisticas = await self.memoria.estadisticas()
        t_datos = time.monotonic() - self._inicio_mono
        if isinstance(estadisticas, Mapping):
            try:
                self._fragmentos_inicio = int(estadisticas.get("fragmentos", 0) or 0)
            except (TypeError, ValueError):
                self._fragmentos_inicio = None

        # 5. Perfil heredado de WhatsApp.
        await self._cargar_perfil()

        # 6. Conectoma (__init__ barato; iniciar() va en paralelo con los diagnósticos).
        clase_conectoma = _importar_obligatorio("core.sincan_connectome", "SincanConnectome")
        self.conectoma = clase_conectoma(s)

        # 7. Compuerta de permisos (las confirmaciones se imprimen en la consola y, en la v0.2,
        # también llegan al chat de escritorio como botones). pids_extra: ventanas de Amadeus
        # Desktop; travesuras_activas: estado efectivo de main (§C2.14.4).
        clase_permisos = _importar_obligatorio("core.permisos", "CompuertaPermisos")
        self.permisos = clase_permisos(s, self.bus, preguntar=self.consola.imprimir_sistema,
                                       pids_extra=self._pids_extra, travesuras_activas=self._travesuras_efectivas)

        # 8. Lóbulos periféricos (P1/P2, opcionales).
        self.aura = self._construir_opcional(
            "lobes.aura_vision_pc", "AuraVisionPC", ("camara", "telemetria", "hyprland", "nexus"),
            lambda cls: cls(s, self.bus, permisos=self.permisos))
        self.vtube = self._construir_opcional("lobes.vtube_bridge", "VTubeBridge", ("vts",),
                                              lambda cls: cls(s, self.bus))
        self.web = self._construir_opcional("lobes.web_researcher", "WebResearcher", ("web",),
                                            lambda cls: cls(s, cliente=self._cliente))
        # Avatar 3D VRM (VTuber AI Studio): escucha el bus; no sustituye a VTube Studio.
        if getattr(s, "vtuber_ai_habilitado", False):
            self.avatar3d = self._construir_opcional(
                "lobes.vtuber_ai_bridge", "VTuberAIBridge", ("avatar3d",),
                lambda cls: cls(s, self.bus, cliente=self._cliente))
        # Corteza motora: el conectoma decide cuándo y cómo se mueve el avatar 3D (sin LLM).
        if self.avatar3d is not None and getattr(s, "avatar_autonomo", False):
            self.corteza = self._construir_opcional(
                "lobes.corteza_motora", "CortezaMotora", ("motor",),
                lambda cls: cls(s, self.bus, self.avatar3d))

        # 9. Presencia (v0.2: inactividad de Windows desde ControlPC, ignorando sus propias inyecciones).
        clase_presencia = _importar_obligatorio("core.presence", "RastreadorPresencia")
        self.presencia = clase_presencia(s)
        self._conectar_presencia_control_pc()

        # 9b. v0.2: visión continua (opt-in de dos niveles; solo lee la presencia, nunca la escribe).
        self._construir_vision()

        # 10. Salida (voz + avatar / Telegram).
        clase_riko = _importar_obligatorio("lobes.riko_megumin_io", "RikoMeguminIO")
        self.riko = clase_riko(s, self.bus, avatar=self.vtube, memoria=self.memoria,
                               presencia=self.presencia.estado, cliente=self._cliente,
                               imprimir=self.consola.imprimir_amadeus)

        # 11. Socket de control para el puente MCP de Nexus.
        self.control = self._construir_opcional("core.control_server", "ServidorControl", ("control",),
                                                lambda cls: cls(s, self._despachar_control))

        self._suscribir_eventos()
        await self._cargar_estado_v02()

        # Diagnósticos en paralelo (con conectoma.iniciar), cada uno con su plazo.
        calibrar = lanzar_tareas or imprimir_arranque
        diags, tiempos = await self._diagnosticos_arranque(calibrar=calibrar)
        diags.insert(0, diag_memoria if isinstance(diag_memoria, DiagnosticoModulo)
                     else DiagnosticoModulo("memoria", _NOMBRES_MODULO["memoria"], EstadoModulo.DEGRADADO,
                                            "diagnóstico no disponible"))
        tiempos.setdefault("memoria", t_memoria)
        for diag in diags:
            self.bus.publicar("sistema.modulo", diag.a_dict(), origen="main")
        self._diags_arranque = list(diags)
        # v0.2: la corteza y el chat conocen el estado efectivo de las travesuras desde el arranque.
        self._publicar_travesuras(forzar=True)
        self._ultimo_campo = self.conectoma.estado().campo
        self._ultima_fuente = self.conectoma.estado().fuente

        if imprimir_arranque:
            await self._imprimir_arranque({d.clave: d for d in diags}, tiempos, estadisticas,
                                          t_memoria, t_datos)
            if s.hud_abrir and self.hud is not None and self._diagnosticos.get("hud") is not None \
                    and self._diagnosticos["hud"].estado == EstadoModulo.OK:
                try:
                    await _quizas_await(self.hud.abrir_navegador())
                except Exception as exc:
                    log.info("No se pudo abrir el HUD en el navegador: %s", type(exc).__name__)
            await self._saludo_inicial(vaciar=not lanzar_tareas)
        if lanzar_tareas:
            self._lanzar_tareas()
        return list(diags)

    async def _cargar_perfil(self) -> None:
        """Paso 5: importa el perfil heredado y, solo si ``perfil_en_prompt``, lo pasa a Broca."""
        ruta = self.settings.perfil_path
        try:
            existe = ruta.is_file()
        except OSError as exc:
            log.debug("No se pudo comprobar el perfil (%s)", type(exc).__name__)
            existe = False
        if not existe:
            return
        try:
            wsp = importlib.import_module("core.wsp_ingestor")
            perfil = wsp.cargar_perfil(ruta)
        except Exception as exc:
            log.warning("No se pudo cargar el perfil de WhatsApp: %s", type(exc).__name__)
            return
        if not perfil:
            return
        try:
            insertados = await self.memoria.importar_perfil(perfil)
            if insertados:
                log.info("Perfil heredado importado: %d hechos", insertados)
            if self.settings.perfil_en_prompt:
                self.broca.set_perfil_resumen(wsp.resumen_perfil(perfil))
        except Exception as exc:
            log.warning("No se pudo integrar el perfil heredado: %s", type(exc).__name__)

    async def _medir(self, fabrica: Callable[[], Awaitable[Any]], timeout: float) -> tuple[Any, float]:
        """Ejecuta un diagnóstico con plazo y devuelve (resultado o excepción, segundos desde el inicio)."""
        try:
            resultado: Any = await asyncio.wait_for(fabrica(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            resultado = exc
        except Exception as exc:
            log.warning("Un diagnóstico falló: %s", type(exc).__name__, exc_info=True)
            resultado = exc
        return resultado, time.monotonic() - self._inicio_mono

    @staticmethod
    def _a_diagnosticos(resultado: Any, claves: Sequence[str], timeout: float) -> list[DiagnosticoModulo]:
        """Normaliza el resultado de un diagnóstico (uno, lista, timeout o excepción) a la lista de claves."""
        if isinstance(resultado, DiagnosticoModulo):
            encontrados = [resultado]
        elif isinstance(resultado, (list, tuple)):
            encontrados = [d for d in resultado if isinstance(d, DiagnosticoModulo)]
        elif isinstance(resultado, asyncio.TimeoutError):
            return [DiagnosticoModulo(c, _NOMBRES_MODULO.get(c, c), EstadoModulo.SIN_CONEXION,
                                      f"sin respuesta en {timeout:.0f} s") for c in claves]
        elif isinstance(resultado, BaseException):
            return [DiagnosticoModulo(c, _NOMBRES_MODULO.get(c, c), EstadoModulo.DEGRADADO,
                                      f"diagnóstico falló: {type(resultado).__name__}") for c in claves]
        else:
            encontrados = []
        por_clave = {d.clave: d for d in encontrados}
        salida = [por_clave.get(c) or DiagnosticoModulo(c, _NOMBRES_MODULO.get(c, c), EstadoModulo.OFF,
                                                        "sin diagnóstico") for c in claves]
        salida.extend(d for d in encontrados if d.clave not in claves)
        return salida

    async def _diagnosticos_arranque(self, *, calibrar: bool) -> tuple[list[DiagnosticoModulo], dict[str, float]]:
        """Diagnósticos en paralelo (``gather``), cada uno con ``wait_for(…, 4)``; el conectoma calibra a la vez.

        Sin tareas ni arranque impreso (tests), el conectoma no calibra: ``latir`` lo haría al
        empezar, y sin latido la calibración no se usaría.
        """
        s = self.settings
        trabajos: list[tuple[Sequence[str], Callable[[], Awaitable[Any]], float]] = []
        if calibrar:
            trabajos.append((("conectoma",), lambda: self.conectoma.iniciar(timeout_s=4.0), 5.5))
        trabajos.append((("broca",), lambda: self.broca.diagnostico(ping=s.tiene_gemini), 4.0))
        if self.aura is not None:
            trabajos.append((("camara", "telemetria", "hyprland", "nexus"), self.aura.iniciar, 4.0))
        trabajos.append((("voz", "telegram"), self.riko.iniciar, 4.0))
        if self.hud is not None:
            trabajos.append((("hud",), self.hud.iniciar, 4.0))
        if self.control is not None:
            trabajos.append((("control",), self.control.iniciar, 4.0))
        if self.avatar3d is not None:
            trabajos.append((("avatar3d",), self.avatar3d.iniciar, 4.0))
        # v0.2: ControlPC y visión continua, cada uno con wait_for(…, 4).
        if self.control_pc is not None:
            trabajos.append((("control_pc",), self.control_pc.iniciar, 4.0))
        if self.vision is not None:
            trabajos.append((("vision_camara", "vision_pantalla"), self.vision.iniciar, 4.0))
        resultados = await asyncio.gather(*(self._medir(f, t) for _c, f, t in trabajos))

        por_clave: dict[str, DiagnosticoModulo] = {}
        tiempos: dict[str, float] = {}
        for (claves, _f, timeout), (resultado, t) in zip(trabajos, resultados):
            for diag in self._a_diagnosticos(resultado, claves, timeout):
                por_clave[diag.clave] = diag
                tiempos[diag.clave] = t
        if not calibrar:
            try:
                por_clave["conectoma"] = self.conectoma.diagnostico()
            except Exception as exc:
                log.warning("Diagnóstico del conectoma no disponible: %s", type(exc).__name__)
                por_clave["conectoma"] = DiagnosticoModulo("conectoma", _NOMBRES_MODULO["conectoma"],
                                                           EstadoModulo.DEGRADADO, "sin diagnóstico")
            tiempos["conectoma"] = time.monotonic() - self._inicio_mono
        for clave, modulo in (("web", self.web), ("vts", self.vtube)):
            if modulo is None:
                continue
            try:
                por_clave[clave] = modulo.diagnostico()
            except Exception as exc:
                log.warning("Diagnóstico de %s falló: %s", clave, type(exc).__name__)
                por_clave[clave] = DiagnosticoModulo(clave, _NOMBRES_MODULO[clave], EstadoModulo.DEGRADADO,
                                                     "diagnóstico falló")
            tiempos[clave] = time.monotonic() - self._inicio_mono
        if not s.vts_habilitado:
            por_clave["vts"] = DiagnosticoModulo("vts", _NOMBRES_MODULO["vts"], EstadoModulo.OFF, "deshabilitado")
        if not getattr(s, "vtuber_ai_habilitado", False):
            por_clave["avatar3d"] = DiagnosticoModulo("avatar3d", _NOMBRES_MODULO["avatar3d"], EstadoModulo.OFF,
                                                      "deshabilitado")
        # La corteza motora se diagnostica después del avatar 3D (su estado depende de él).
        if self.corteza is not None:
            try:
                por_clave["motor"] = self.corteza.diagnostico()
            except Exception as exc:
                log.warning("Diagnóstico de la corteza motora falló: %s", type(exc).__name__)
                por_clave["motor"] = DiagnosticoModulo("motor", _NOMBRES_MODULO["motor"], EstadoModulo.DEGRADADO,
                                                       "diagnóstico falló")
            tiempos["motor"] = time.monotonic() - self._inicio_mono
        elif not getattr(s, "avatar_autonomo", False):
            por_clave["motor"] = DiagnosticoModulo("motor", _NOMBRES_MODULO["motor"], EstadoModulo.OFF, "deshabilitado")
        elif self.avatar3d is None:
            por_clave["motor"] = DiagnosticoModulo("motor", _NOMBRES_MODULO["motor"], EstadoModulo.OFF, "sin avatar 3D")
        # v0.2: /escritorio lo diagnostica el HUD (sin token en el detalle).
        if self.hud is not None and getattr(s, "tiene_escritorio", False):
            por_clave["escritorio"] = self._diagnostico_escritorio()
            tiempos["escritorio"] = time.monotonic() - self._inicio_mono
        for clave, diag in self._diag_ausentes.items():
            por_clave.setdefault(clave, diag)
            tiempos.setdefault(clave, time.monotonic() - self._inicio_mono)
        orden = ["conectoma", *_ORDEN_ARRANQUE]
        diags = [por_clave[c] for c in orden if c in por_clave]
        diags.extend(d for c, d in por_clave.items() if c not in orden)
        return diags, tiempos

    def _diagnostico_escritorio(self) -> DiagnosticoModulo:
        """``hud.diagnostico_escritorio()`` (grupo escritorio-ws) o un ``OFF`` si aún no existe."""
        nombre = _NOMBRES_MODULO["escritorio"]
        diagnosticar = getattr(self.hud, "diagnostico_escritorio", None)
        if not callable(diagnosticar):
            return DiagnosticoModulo("escritorio", nombre, EstadoModulo.OFF, "el HUD no ofrece /escritorio")
        try:
            diag = diagnosticar()
        except Exception as exc:
            log.warning("Diagnóstico de /escritorio falló: %s", type(exc).__name__)
            return DiagnosticoModulo("escritorio", nombre, EstadoModulo.DEGRADADO, "diagnóstico falló")
        if isinstance(diag, DiagnosticoModulo):
            return diag
        return DiagnosticoModulo("escritorio", nombre, EstadoModulo.DEGRADADO, "diagnóstico no válido")

    def _valor_diagnostico(self, diag: DiagnosticoModulo) -> str:
        """Texto de la columna de valor de una línea del arranque (§6.4)."""
        estado = diag.estado
        detalle = (diag.detalle or "").strip()
        if diag.clave == "broca" and estado == EstadoModulo.SIN_CONEXION:
            return "SIN CONEXIÓN · modo no verbal"
        if diag.clave == "vts" and estado == EstadoModulo.EN_ESPERA:
            return "EN ESPERA · conectando en segundo plano"
        if diag.clave == "hud" and estado == EstadoModulo.OK and self.hud is not None:
            puerto = getattr(self.hud, "puerto_real", None)
            url = getattr(self.hud, "url", None)
            if self.consola.es_tty and url:
                return f"OK  {url}"
            if puerto:
                return f"OK  http://127.0.0.1:{puerto}/ · usa /hud para ver el enlace"
            return "OK  usa /hud para ver el enlace"
        if diag.clave == "control" and estado == EstadoModulo.OK and self.control is not None:
            puerto = getattr(self.control, "puerto_real", None)
            if puerto:
                return f"OK  127.0.0.1:{puerto}"
        valor = estado.value
        if detalle:
            valor += f" ({detalle})" if not detalle.startswith("(") else f" {detalle}"
        # Maqueta de §6.4: solo la línea de Broca muestra la latencia de su ping ("OK  (412 ms)");
        # Telemetría y Hyprland quedan en "OK" / "OK (lua)" aunque su diagnóstico la mida.
        if diag.clave == "broca" and diag.latencia_ms is not None and estado == EstadoModulo.OK:
            valor += f"  ({diag.latencia_ms} ms)"
        return valor

    async def _imprimir_arranque(self, diags: Mapping[str, DiagnosticoModulo], tiempos: Mapping[str, float],
                                 estadisticas: Mapping[str, Any], t_memoria: float, t_datos: float) -> None:
        """Secuencia *memory restore* (§6.4): cabecera, líneas reales, rueda nixie y llamada entrante."""
        c = self.consola
        reloj = [0.0]

        def marca(t: float | None) -> float:
            valor = max(reloj[0], t if t is not None else time.monotonic() - self._inicio_mono)
            reloj[0] = valor
            return valor

        c.imprimir(_SEPARADOR, "gris")
        c.imprimir(" VIKTOR CHONDRIA UNIVERSITY · BRAIN SCIENCE INSTITUTE  (homenaje)", "gris")
        c.imprimir(f" A M A D E U S   ·   R.A.M.S. BIO-CORE  v{VERSION}", "negrita")
        c.imprimir(_SEPARADOR, "gris")

        d_mem = diags.get("memoria")
        estado_mem = d_mem.estado if d_mem else EstadoModulo.DEGRADADO
        almacen = f"{self.settings.memoria_dir.name}/"
        valor_mem = f"{almacen}  {estado_mem.value}"
        if d_mem and d_mem.detalle and estado_mem != EstadoModulo.OK:
            valor_mem += f" ({d_mem.detalle})"
        c.linea_arranque(marca(t_memoria), "Montando almacén de memoria", valor_mem, estado_mem)
        fragmentos = _miles(estadisticas.get("fragmentos", 0) if isinstance(estadisticas, Mapping) else 0)
        c.linea_arranque(marca(t_datos), "Cargando datos de memoria", f"{fragmentos} fragmentos  {estado_mem.value}",
                         estado_mem)
        perfil = estadisticas.get("perfil_generado") if isinstance(estadisticas, Mapping) else None
        if perfil:
            heredado = f"{self.settings.perfil_path.name} ({perfil})"
        else:
            heredado = "ninguno (ejecuta python -m core.wsp_ingestor)"
        c.imprimir(f"         último registro heredado: {heredado}", "gris")
        kv = estadisticas.get("ultimo_kv") if isinstance(estadisticas, Mapping) else None
        if kv:
            kv_ts = estadisticas.get("ultimo_kv_ts")
            vivido = f"{kv}  ({kv_ts})" if kv_ts else str(kv)
        else:
            vivido = "ninguno"
        c.imprimir(f"         último registro vivido:   {vivido}", "gris")

        d_con = diags.get("conectoma")
        estado_con = d_con.estado if d_con else EstadoModulo.DEGRADADO
        fuente = getattr(self.conectoma, "fuente_activa", "stub")
        if fuente == "stub":
            valor_con = "DEGRADADO (modo simple)" if estado_con != EstadoModulo.OK else "OK (modo simple)"
            estado_con = EstadoModulo.DEGRADADO if estado_con != EstadoModulo.OK else estado_con
        else:
            n = _miles(getattr(self.conectoma, "n_neuronas", 0))
            dt = 2 if getattr(self.conectoma, "modo_degradacion", "normal") == "dt2" else 1
            valor_con = f"{n} neuronas · dt={dt} ms  {estado_con.value}"
            if estado_con != EstadoModulo.OK and d_con and d_con.detalle:
                valor_con += f" ({d_con.detalle})"
        c.linea_arranque(marca(tiempos.get("conectoma")), "Conectoma CX+MB (LIF)", valor_con, estado_con)

        estado = self.conectoma.estado()
        homeostasis = (f"a={formatear_decimal(estado.arousal, 2)} f={formatear_decimal(estado.focus, 2)} "
                       f"b={formatear_decimal(estado.boredom, 2)} t={formatear_decimal(estado.fatiga, 2)}")
        c.linea_arranque(marca(None), "Homeostasis", homeostasis)
        t_medidor = marca(None)
        prefijo = f"{c._color(f'[{t_medidor:06.3f}]', 'gris')} {'Medidor de divergencia '.ljust(44, '.')} "
        valores = [f"{v:.6f}" for v in SECUENCIA_ARRANQUE_DIVERGENCIA[:-1]] + [estado.divergencia_str()]
        await c.rueda_nixie(prefijo, valores)

        puerto_vts = 8001
        coincidencia = re.search(r":(\d{1,5})(?:/|$)", self.settings.vts_url)
        if coincidencia:
            puerto_vts = int(coincidencia.group(1))
        puerto_avatar = 3939
        coincidencia = re.search(r":(\d{1,5})/?$", str(getattr(self.settings, "vtuber_ai_url", "")))
        if coincidencia:
            puerto_avatar = int(coincidencia.group(1))
        etiquetas = {
            "broca": "Área de Broca (Gemini)", "camara": "Lóbulo visual (cámara)", "telemetria": "Telemetría",
            "hyprland": "Hyprland", "vts": f"Puente VTube Studio :{puerto_vts}",
            "avatar3d": f"Avatar 3D VTuber AI Studio :{puerto_avatar}", "motor": "Corteza motora (avatar 3D)",
            "voz": "Salida de voz",
            "telegram": "Mensajería (Telegram/RINE)", "web": "Investigación web", "nexus": "Nexus IDE",
            "hud": "HUD", "control": "Socket de control (MCP)",
            "escritorio": "Amadeus Desktop (/escritorio)", "control_pc": "Control del PC",
            "vision_camara": "Visión continua (cámara)", "vision_pantalla": "Visión continua (pantalla)",
        }
        for clave in _ORDEN_ARRANQUE:
            diag = diags.get(clave)
            if diag is None:
                diag = DiagnosticoModulo(clave, _NOMBRES_MODULO.get(clave, clave), EstadoModulo.OFF,
                                         "módulo no instalado")
            c.linea_arranque(marca(tiempos.get(clave)), etiquetas[clave], self._valor_diagnostico(diag),
                             diag.estado)
        # Maqueta de §6.4: "[00.500] Llamada entrante: Amadeus ..." (sin relleno de puntos).
        c.imprimir(f"{c._color(f'[{marca(None):06.3f}]', 'gris')} Llamada entrante: {self.settings.nombre_ia} ...")
        c.imprimir(_SEPARADOR, "gris")

    async def _saludo_inicial(self, *, vaciar: bool) -> None:
        """Saludo (§6.4): una llamada a Broca si hay Gemini y está OK; si no, ``saludo_<franja>`` local."""
        estado = self.conectoma.estado()
        modo = "voz" if self.riko.voz_disponible else "terminal"
        diag_broca = self._diagnosticos.get("broca")
        try:
            if self.settings.tiene_gemini and diag_broca is not None and diag_broca.estado == EstadoModulo.OK:
                bitacora = await self.memoria.ultima_bitacora()
                ctx = ContextoBroca(
                    estado, modo_salida=modo, canal_entrada="terminal", presencia=self.presencia.estado(),
                    evento_social="saludo", ultima_bitacora=bitacora, nexus=resumen_nexus(self._ultimo_nexus),
                    instruccion_extra="Inicio de sesión: saluda breve, menciona el estado si es relevante.",
                    fecha_hora=ahora_local(self.settings))
                reply = await self.broca.verbalizar(ctx, None)
                if bitacora:
                    self._primer_turno = False
            else:
                franja = franja_horaria(ahora_local(self.settings))
                reply = self.broca.frase_local(f"saludo_{franja}", estado)
            await self.riko.decir(reply, origen="sistema", canal=CANAL_DE_MODO.get(modo, "terminal"))
            if vaciar:
                await self.riko.vaciar(timeout=6.0)
        except Exception as exc:
            log.warning("No se pudo entregar el saludo inicial: %s", type(exc).__name__, exc_info=True)

    def _lanzar_tareas(self) -> None:
        """Lanza las tareas de fondo supervisadas de §6.3 según sus condiciones."""
        s = self.settings
        hypr = getattr(self.aura, "hypr", None) if self.aura is not None else None
        especificaciones: list[tuple[str, Callable[[], Awaitable[None]], bool]] = [
            ("conectoma", lambda: self.conectoma.latir(publicar=self._al_latir, parar=self.parar), True),
            ("terminal", lambda: self.consola.bucle_entrada(self.parar, self._al_linea), True),
            ("salida", lambda: self.riko.bucle(self.parar), True),
            ("telemetria", lambda: self.aura.bucle_telemetria(self.parar),
             self.aura is not None and self._telemetria_disponible()),
            ("hyprland", lambda: self.aura.bucle_eventos_hypr(self.parar),
             self.aura is not None and bool(getattr(hypr, "disponible", False))),
            ("nexus", lambda: self.aura.nexus.bucle_vigilancia(self.parar, self.bus),
             self.aura is not None and getattr(self.aura, "nexus", None) is not None),
            ("vts", lambda: self.vtube.bucle_reconexion(self.parar), self.vtube is not None and s.vts_habilitado),
            ("avatar3d", lambda: self.avatar3d.bucle(self.parar), self.avatar3d is not None),
            ("motor", lambda: self.corteza.bucle(self.parar), self.corteza is not None),
            ("telegram_entrada", lambda: self.riko.bucle_telegram_entrada(self.parar, self._manejar_telegram),
             s.tiene_telegram and s.telegram_polling),
            ("salieri", lambda: self.memoria.bucle_compresor(self.parar, presente=self._usuario_presente), True),
            ("agenda", self._bucle_agenda, True),
            ("presencia", self._bucle_presencia, True),
            # v0.2: no molestar (5 s), vencimiento de las travesuras y rostro → presencia (30 s).
            ("vigilancia", self._bucle_vigilancia, True),
        ]
        for nombre, fabrica, condicion in especificaciones:
            if not condicion:
                continue
            tarea = asyncio.create_task(supervisar(nombre, fabrica, self.parar, bus=self.bus), name=f"rams:{nombre}")
            self._tareas[nombre] = tarea
        log.info("Tareas supervisadas en marcha: %s", ", ".join(self._tareas))

    def _telemetria_disponible(self) -> bool:
        diag = self._diagnosticos.get("telemetria")
        if diag is not None and diag.estado == EstadoModulo.OFF:
            return False
        return importlib.util.find_spec("psutil") is not None

    def _usuario_presente(self) -> bool:
        try:
            return bool(self.presencia.estado().presente)
        except Exception as exc:
            log.debug("Presencia no disponible (%s); asumo presente", type(exc).__name__)
            return True

    # ------------------------------------------------------------------
    # Ejecución y señales
    # ------------------------------------------------------------------

    async def correr(self) -> int:
        """``iniciar`` + esperar ``parar`` + ``apagar``. Devuelve 0 (o 1 si hubo un error fatal)."""
        codigo = 0
        motivo = "usuario"
        try:
            await self.iniciar()
            self._instalar_senales()
            await self.parar.wait()
            motivo = self._motivo_parada or "usuario"
        except asyncio.CancelledError:   # Ctrl+C en Windows: cancela la tarea principal
            motivo = "señal"
            raise
        except Exception as exc:
            log.error("Error fatal en el orquestador: %s", exc, exc_info=True)
            self.consola.imprimir_sistema(f"Error fatal: {redactar(str(exc), self.settings.secretos())}")
            codigo = 1
            motivo = "error"
        finally:
            try:
                await self.apagar(motivo)
            except Exception as exc:
                log.error("Error durante el apagado: %s", exc, exc_info=True)
        return codigo

    def _instalar_senales(self) -> None:
        loop = asyncio.get_running_loop()
        for nombre in ("SIGINT", "SIGTERM"):
            senal = getattr(signal, nombre, None)
            if senal is None:
                continue
            try:
                loop.add_signal_handler(senal, self._por_senal)
                self._senales.append(senal)
            except (NotImplementedError, RuntimeError, ValueError) as exc:
                log.debug("Sin manejador asíncrono para %s (%s)", nombre, type(exc).__name__)

    def _quitar_senales(self) -> None:
        if not self._senales:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for senal in self._senales:
            try:
                loop.remove_signal_handler(senal)
            except (NotImplementedError, RuntimeError, ValueError) as exc:
                log.debug("No se pudo quitar el manejador de la señal %s: %s", senal, exc)
        self._senales.clear()

    def _por_senal(self) -> None:
        self._motivo_parada = self._motivo_parada or "señal"
        self.parar.set()

    # ------------------------------------------------------------------
    # Suscripciones al bus
    # ------------------------------------------------------------------

    def _suscribir_eventos(self) -> None:
        sub = self.bus.suscribir
        self._desuscribir += [
            sub("usuario.entrada", self._ev_usuario_entrada),
            sub("pc.ventana", self._ev_ventana),
            sub("pc.telemetria", self._ev_telemetria),
            sub("vision.vistazo", self._ev_vistazo),
            sub("vision.camara", self._ev_camara),
            sub("web.resultado", self._ev_web),
            sub("pc.alerta", self._ev_alerta),
            sub("nexus.estado", self._ev_nexus_estado),
            sub("nexus.evento", self._ev_nexus_evento),
            sub("usuario.presencia", self._ev_presencia),
            sub("homeostasis.iniciativa", self._ev_iniciativa),
            sub("homeostasis.estado", self._ev_homeostasis),
            sub("hud.control", self._ev_hud_control),
            sub("permiso.solicitado", self._ev_permiso_solicitado),
            # v0.2 (§C2.3): nombres exactos; nunca "*" ni "escritorio.*".
            sub("permiso.resuelto", self._ev_permiso_resuelto),
            sub("escritorio.chat", self._ev_escritorio_chat),
            sub("escritorio.confirmacion", self._ev_escritorio_confirmacion),
            sub("escritorio.control", self._ev_escritorio_control),
            sub("escritorio.evento", self._ev_escritorio_evento),
            sub("escritorio.conectado", self._ev_escritorio_conectado),
            sub("escritorio.app", self._ev_escritorio_app),
            sub("escritorio.caja", self._ev_escritorio_caja),
            sub("motor.travesura", self._ev_motor_travesura),
            sub("vision.estimulo", self._ev_vision_estimulo),
            sub("vision.rostro", self._ev_vision_rostro),
            sub("vision.atencion", self._ev_vision_atencion),
            sub("vision.estado", self._ev_vision_estado),
            sub("pantalla.estado", self._ev_pantalla_estado),
            sub("pantalla.estimulo", self._ev_pantalla_estimulo),
        ]

    def _inyectar(self, evento: str, **datos: Any) -> None:
        """``conectoma.inyectar`` protegido (nunca lanza hacia el bus)."""
        if self.conectoma is None:
            return
        try:
            self.conectoma.inyectar(evento, **datos)
        except Exception as exc:
            log.debug("Inyección %s en el conectoma falló: %s", evento, type(exc).__name__)

    def _al_consolidar(self, kv_id: str, n_turnos: int, metodo: str) -> None:
        """Callback de Salieri (puede llegar desde el hilo de memoria: ``publicar`` es thread-safe)."""
        self.bus.publicar("memoria.consolidada", {"kv_id": kv_id, "n_turnos": n_turnos, "metodo": metodo},
                          origen="memoria")

    def _ev_modulo(self, evento: Evento) -> None:
        d = evento.datos
        try:
            diag = DiagnosticoModulo(str(d.get("clave", "")), str(d.get("nombre", "")), d.get("estado", "OFF"),
                                     str(d.get("detalle", "") or ""), d.get("latencia_ms"))
        except Exception as exc:
            log.debug("sistema.modulo con payload inválido: %s", type(exc).__name__)
            return
        if diag.clave:
            self._diagnosticos[diag.clave] = diag

    def _ev_usuario_entrada(self, evento: Evento) -> None:
        self._inyectar("usuario_escribe", texto=str(evento.datos.get("texto", "")))

    def _ev_ventana(self, evento: Evento) -> None:
        d = evento.datos
        self._inyectar("ventana_cambia", clase=str(d.get("clase") or ""), titulo=str(d.get("titulo") or ""))
        direccion = d.get("direccion")
        pid = d.get("pid")
        if not isinstance(direccion, str) or not _DIRECCION_RE.match(direccion):
            return
        propios = getattr(self.permisos, "pids_propios", None) or frozenset({os.getpid()})
        if pid is not None and pid in propios:
            return
        self._ventana_previa = {"clase": str(d.get("clase") or ""), "titulo": str(d.get("titulo") or ""),
                                "direccion": direccion, "pid": pid}

    def _ev_telemetria(self, evento: Evento) -> None:
        d = evento.datos
        temps = [t for t in (d.get("temp_cpu"), d.get("temp_gpu")) if isinstance(t, (int, float))]
        self._inyectar("carga_sistema", cpu=float(d.get("cpu_pct") or 0.0), ram=float(d.get("ram_pct") or 0.0),
                       temp=max(temps) if temps else None)

    def _ev_vistazo(self, evento: Evento) -> None:
        rostro = evento.datos.get("rostro")
        if rostro is not None:
            self._inyectar("rostro", presente=bool(rostro), n=int(evento.datos.get("n_rostros") or 0))

    def _ev_camara(self, evento: Evento) -> None:
        """Indicador ``● CÁMARA ACTIVA`` de la consola = OR de las fuentes (vistazo y continua, §C2.13).

        Solo se imprime al cambiar el OR: la visión continua repite ``vision.camara`` cada 5 s.
        """
        d = evento.datos
        fuente = d.get("fuente") if d.get("fuente") in ("vistazo", "continua") else "vistazo"
        self._camaras_activas[str(fuente)] = bool(d.get("activa"))
        activa = any(self._camaras_activas.values())
        if activa == self._indicador_camara:
            return
        self._indicador_camara = activa
        self.consola.indicador_camara(activa, d.get("duracion_ms"))

    def _ev_web(self, evento: Evento) -> None:
        d = evento.datos
        n = int(d.get("n") or 0)
        self._inyectar("busqueda_completa", consulta=str(d.get("consulta", "")), n=n, exito=n > 0)

    def _ev_alerta(self, evento: Evento) -> Any:
        critica = bool(evento.datos.get("critica"))
        self._inyectar("error", severidad=1.0 if critica else 0.5)
        if critica and not self._apagando:
            return self._al_alerta(evento)
        return None

    def _ev_nexus_estado(self, evento: Evento) -> None:
        self._ultimo_nexus = dict(evento.datos)

    def _ev_nexus_evento(self, evento: Evento) -> Any:
        if evento.datos.get("tipo") == "foco":
            self._inyectar("nexus_foco", enfocado=bool(evento.datos.get("enfocado")))
            return None
        if self._apagando:
            return None
        return self._al_nexus(evento)

    def _ev_presencia(self, evento: Evento) -> None:
        self._inyectar("usuario_presencia", presente=bool(evento.datos.get("presente")))

    def _ev_iniciativa(self, evento: Evento) -> Any:
        if self._apagando:
            return None
        return self._al_iniciativa(evento)

    def _ev_homeostasis(self, evento: Evento) -> Any:
        if self.vtube is None or self.riko is None or self._apagando:
            return None
        if getattr(self.riko, "hablando", False) or not getattr(self.vtube, "en_linea", False):
            return None
        estado = HomeostaticState.desde_dict(evento.datos.get("estado") or {})
        return self.vtube.aplicar_estado(estado)

    def _ev_hud_control(self, evento: Evento) -> None:
        accion = evento.datos.get("accion")
        try:
            if accion == "silenciar" and self.riko is not None:
                self.riko.set_silencio(not self.riko.silenciado)
                self.consola.imprimir_sistema("HUD: voz silenciada." if self.riko.silenciado else "HUD: voz activada.")
            elif accion == "camara" and (self.aura is not None or self.vision is not None):
                efectivo = False
                if self.aura is not None:
                    efectivo = bool(self.aura.set_camara(not self.aura.camara_habilitada))
                if not efectivo:
                    self._apagar_vision_camara()   # §C2.13: el botón "Cámara" también pausa la continua
                self.consola.imprimir_sistema("HUD: cámara habilitada." if efectivo else "HUD: cámara deshabilitada.")
            elif accion == "colgar" and self.riko is not None:
                self.riko.pausar_proactividad(30)
                self.consola.imprimir_sistema("HUD: llamadas proactivas en pausa durante 30 min.")
        except Exception as exc:
            log.warning("Control del HUD %s falló: %s", accion, type(exc).__name__)

    def _ev_permiso_solicitado(self, evento: Evento) -> Any:
        if self._apagando or self.riko is None or self.presencia is None or self.broca is None:
            return None
        try:
            # v0.2: lo pedido desde el chat de escritorio se confirma con sus botones y en silencio
            # (labios mudos), salvo RAMS_ESCRITORIO_VOZ=1 (§C1.5, §C2.14.3).
            if _CANAL_ACCION.get() == "escritorio" and not getattr(self.settings, "escritorio_voz", False):
                return None
            presente = self.presencia.estado().presente
            if not (presente and self.riko.voz_disponible):
                return None
            # v0.2: un borrador o un tecleo no son "destructivos": frase propia (nunca lee el texto).
            tipo = evento.datos.get("tipo")
            if tipo == "prerrellenar":
                reply = self.broca.frase_local("prerrellenar_confirma", self.conectoma.estado())
            elif tipo == "escribir_texto":
                reply = self.broca.frase_local("escribir_confirma", self.conectoma.estado())
            else:
                reply = self.broca.frase_local("confirmacion_requerida", self.conectoma.estado(),
                                               descripcion=str(evento.datos.get("descripcion", "")))
        except Exception as exc:
            log.debug("Sin confirmación por voz: %s", type(exc).__name__)
            return None
        return self.riko.decir(reply, origen="sistema", canal="voz")

    # ------------------------------------------------------------------
    # v0.2: escritorio, visión, travesuras y "no molestar" (§C2.3, §C2.14)
    # ------------------------------------------------------------------

    def _publicar(self, nombre: str, datos: Mapping[str, Any]) -> None:
        """``bus.publicar`` protegido (origen ``main``; nunca lanza)."""
        try:
            self.bus.publicar(nombre, dict(datos), origen="main")
        except Exception as exc:
            log.debug("No se pudo publicar %s: %s", nombre, type(exc).__name__)

    def _sistema_escritorio(self, texto: str) -> None:
        """``escritorio.sistema`` (≤ 4000): salida de comandos y avisos para el chat (nunca ``/ws``)."""
        limpio = str(texto or "").strip()
        if limpio:
            self._publicar("escritorio.sistema", {"texto": limpio[:MAX_TEXTO_SISTEMA]})

    def _aviso_local(self, texto: str, canal: str) -> None:
        """Aviso del sistema en la consola y, si el turno vino del chat de escritorio, también allí."""
        self.consola.imprimir_sistema(texto)
        if canal == "escritorio":
            self._sistema_escritorio(texto)

    def _orden_escritorio(self, accion: str, **campos: Any) -> None:
        """``escritorio.orden`` hacia Amadeus Desktop (visibilidad, chat y notificaciones)."""
        self._publicar("escritorio.orden", {"accion": accion, **campos})

    def _solicitud_motor(self, conducta: str, motivo: str, *, borde: str | None = None,
                         ttl_s: float = TTL_SOLICITUD_MOTOR_S) -> None:
        """``motor.solicitud``: main decide QUÉ (iniciativa); la corteza motora decide CÓMO."""
        self._publicar("motor.solicitud", {"conducta": conducta, "motivo": str(motivo or "")[:60],
                                           "borde": borde if borde in ("left", "right") else None,
                                           "ttl_s": float(min(30.0, max(0.0, ttl_s)))})

    def _overlay_disponible(self) -> bool:
        """Overlay disponible (§C2.14.6): Electron conectado **y** algún visor de escritorio cargado."""
        if not self._overlay_conectado or self.avatar3d is None:
            return False
        try:
            return int(getattr(self.avatar3d, "visores_escritorio_total", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    def _salir_a_buscar(self, motivo: str, *, presente: bool) -> bool:
        """Regla única de C1.3: presente + overlay disponible + sin "no molestar" → aparece y te busca.

        Si el overlay está oculto, primero ``escritorio.orden{aparecer, modo:"asomarse"}``; después
        ``motor.solicitud{buscar_usuario, ttl_s:20}``. Devuelve ``True`` si publicó la solicitud.
        """
        if not presente or not self._overlay_disponible() or self._no_molestar_activo():
            return False
        borde = self._rng.choice(("left", "right"))
        if not self._overlay_visible:
            self._orden_escritorio("aparecer", borde=borde, modo="asomarse")
        self._solicitud_motor("buscar_usuario", motivo, borde=borde, ttl_s=TTL_SOLICITUD_MOTOR_S)
        return True

    # --- chat y confirmaciones --------------------------------------------

    def _ev_escritorio_chat(self, evento: Evento) -> None:
        """Mensaje del chat de escritorio (§C2.14.2). Síncrono; el texto nunca se registra.

        Con una confirmación pendiente el texto espera (deque de 3) y **no** la resuelve: solo
        los botones o la terminal. Si no, un turno serializado en el canal ``escritorio``.
        """
        try:
            texto = evento.datos.get("texto")
            if not isinstance(texto, str):
                return
            texto = texto.strip()[:MAX_TEXTO_CHAT]
            if not texto or self._apagando or not self._iniciado:
                return
            if self.permisos is not None and self.permisos.hay_pendiente():
                self._chat_en_espera.append(texto)
                self._sistema_escritorio(MENSAJE_CONFIRMACION_PENDIENTE_CHAT)
                return
            self._crear_tarea(self._turno_serializado(texto, "escritorio"), "turno_escritorio")
        except Exception as exc:  # nunca con el texto en el log
            log.warning("No se pudo atender un mensaje del chat de escritorio (%s)", type(exc).__name__)

    def _ev_permiso_resuelto(self, evento: Evento) -> None:
        """Resuelta la confirmación, los mensajes del chat que esperaban pasan a turnos (en orden)."""
        if self._apagando:
            self._chat_en_espera.clear()
            return
        while self._chat_en_espera:
            texto = self._chat_en_espera.popleft()
            self._crear_tarea(self._turno_serializado(texto, "escritorio"), "turno_escritorio")

    def _ev_escritorio_confirmacion(self, evento: Evento) -> None:
        """Botón de confirmación del chat → ``permisos.resolver_por_id`` (síncrono, §C2.14.5)."""
        d = evento.datos
        ident, aprobado = d.get("id"), d.get("aprobado")
        if (not isinstance(ident, str) or not _ID_CONFIRMACION_RE.fullmatch(ident)
                or not isinstance(aprobado, bool) or self.permisos is None):
            return
        resolver = getattr(self.permisos, "resolver_por_id", None)
        try:
            resuelta = bool(resolver(ident, aprobado)) if callable(resolver) else False
        except Exception as exc:
            log.warning("No se pudo resolver la confirmación con el botón: %s", type(exc).__name__)
            resuelta = False
        if resuelta:
            return
        pendiente = self.permisos.pendiente() if self.permisos.hay_pendiente() else None
        if pendiente is not None and getattr(pendiente.accion, "tipo", "") == "cerrar_ventana":
            self._sistema_escritorio("Cerrar ventanas solo se confirma en la terminal.")
        else:
            self._sistema_escritorio("Esa confirmación ya no está pendiente.")

    # --- controles, eventos y estado de Amadeus Desktop -------------------

    def _ev_escritorio_control(self, evento: Evento) -> None:
        """Bandeja, atajos y botones del chat (§C1.11, §C1.12)."""
        accion = evento.datos.get("accion")
        try:
            if accion == "no_molestar_on":
                self._set_no_molestar_usuario(True)
            elif accion == "no_molestar_off":
                self._set_no_molestar_usuario(False)
            elif accion == "basta_travesuras":
                mensaje = self._set_travesuras("basta")
                self._orden_escritorio("esconderse")
                self.consola.imprimir_sistema(mensaje)
                self._sistema_escritorio(mensaje)
            elif accion == "pausar_vision":
                mensaje = self._pausar_vision(None)
                self.consola.imprimir_sistema(mensaje)
                self._sistema_escritorio(mensaje)
        except Exception as exc:
            log.warning("Control de escritorio %s falló: %s", str(accion)[:30], type(exc).__name__)

    def _set_no_molestar_usuario(self, activo: bool) -> None:
        """"No molestar" del usuario: proactividad en pausa 24 h, sin travesuras y escondida."""
        self._no_molestar_usuario = bool(activo)
        if self.riko is not None:
            try:
                self.riko.pausar_proactividad(1440 if activo else 0)
            except Exception as exc:
                log.debug("pausar_proactividad falló: %s", type(exc).__name__)
        if activo:
            self._orden_escritorio("esconderse")
            texto = "No molestar activado: sin llamadas proactivas ni travesuras hasta que lo quites."
        else:
            texto = "No molestar desactivado."
        self.consola.imprimir_sistema(texto)
        self._sistema_escritorio(texto)
        self._publicar_travesuras()
        self._recalcular_no_molestar()

    def _ev_escritorio_conectado(self, evento: Evento) -> None:
        """Electron conectado/desconectado (nunca se usa ``clientes`` para decidir)."""
        self._overlay_conectado = bool(evento.datos.get("overlay"))
        if not self._overlay_conectado:
            self._overlay_visible = False
        # Un cliente nuevo no conoce el estado: se reenvía el de las travesuras.
        self._publicar_travesuras(forzar=True)

    def _ev_escritorio_app(self, evento: Evento) -> None:
        """PID de Amadeus Desktop: sus ventanas no son objetivo de escribir_texto."""
        pid = evento.datos.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or self.control_pc is None:
            return
        excluir = getattr(self.control_pc, "excluir_pids", None)
        if callable(excluir):
            try:
                excluir([pid])
            except Exception as exc:
                log.debug("excluir_pids falló: %s", type(exc).__name__)

    def _ev_escritorio_caja(self, evento: Evento) -> None:
        """Caja del avatar (px físicos globales) → ControlPC (``hacia_avatar``) y máscara de la visión."""
        d = evento.datos
        valores = [d.get(k) for k in ("x", "y", "w", "h")]
        if any(isinstance(v, bool) or not isinstance(v, int) for v in valores):
            return
        x, y, w, h = valores
        if w <= 0 or h <= 0:
            return
        caja = (x, y, w, h)
        for modulo in (self.control_pc, self.vision):
            fijar = getattr(modulo, "set_caja_avatar", None) if modulo is not None else None
            if callable(fijar):
                try:
                    fijar(caja)
                except Exception as exc:
                    log.debug("set_caja_avatar falló: %s", type(exc).__name__)

    def _ev_escritorio_evento(self, evento: Evento) -> None:
        """Visibilidad del overlay y traducción al conectoma (tabla de §C2.3, con límites de ritmo)."""
        nombre = evento.datos.get("nombre")
        datos = evento.datos.get("datos")
        datos = datos if isinstance(datos, Mapping) else {}
        if nombre == "mostrada":
            self._overlay_visible = True
        elif nombre == "ocultada":
            self._overlay_visible = False
        elif nombre in ("toque", "soltada", "chat_abierto"):
            # Interacción directa con Amadeus: el usuario está ahí.
            if self.presencia is not None:
                self.presencia.registrar_entrada("escritorio")
            if nombre == "toque":
                self._toque_conectoma()
            elif nombre == "soltada":
                altura = datos.get("altura")
                if (isinstance(altura, (int, float)) and not isinstance(altura, bool) and altura > 0.3
                        and self._ritmo_escritorio()):
                    self._inyectar("error", severidad=0.2)      # la dejaron caer desde alto

    def _ritmo_escritorio(self) -> bool:
        """≤ 1 inyección del escritorio cada 5 s (marca el instante si deja pasar)."""
        ahora = self._reloj()
        if ahora - self._ultima_inyeccion_escritorio < INTERVALO_INYECCION_ESCRITORIO_S:
            return False
        self._ultima_inyeccion_escritorio = ahora
        return True

    def _toque_conectoma(self) -> None:
        """Toque: recompensa 0,3; ≥ 5 toques en 30 s: error 0,4 (enfado). ≤ 1 inyección cada 5 s."""
        ahora = self._reloj()
        self._toques.append(ahora)
        while self._toques and ahora - self._toques[0] > VENTANA_TOQUES_S:
            self._toques.popleft()
        if not self._ritmo_escritorio():
            return
        if len(self._toques) >= TOQUES_ENFADO:
            self._inyectar("error", severidad=0.4)
        else:
            self._inyectar("recompensa", magnitud=0.3)

    # --- visión continua ----------------------------------------------------

    def _ev_vision_estimulo(self, evento: Evento) -> None:
        """Flancos de la cámara → conectoma y presencia (llegada/salida; el resto, nada)."""
        d = evento.datos
        if d.get("fuente", "camara") != "camara":
            return
        tipo = d.get("tipo")
        if tipo == "llegada":
            n = 1
            if self._ultimo_rostro is not None:
                try:
                    n = max(1, int(self._ultimo_rostro.get("n") or 1))
                except (TypeError, ValueError):
                    n = 1
            self._inyectar("rostro", presente=True, n=n)
            self._registrar_rostro_presencia(True)
        elif tipo == "salida":
            self._inyectar("rostro", presente=False, n=0)
            self._inyectar("usuario_presencia", presente=False)
            self._registrar_rostro_presencia(False)

    def _registrar_rostro_presencia(self, presente: bool) -> None:
        """Main es el único dueño de la presencia por rostro (VisionContinua no escribe en ella)."""
        if self.presencia is None:
            return
        try:
            self.presencia.registrar_rostro(bool(presente))
            if presente:
                self._ultimo_refresco_rostro = self._reloj()
            self._actualizar_presencia()
        except Exception as exc:
            log.debug("No se pudo registrar el rostro en la presencia: %s", type(exc).__name__)

    def _ev_vision_rostro(self, evento: Evento) -> None:
        """Solo percepción (nunca el conectoma)."""
        self._ultimo_rostro = dict(evento.datos)

    def _ev_vision_atencion(self, evento: Evento) -> None:
        """Tónico de atención: solo percepción."""
        nivel = evento.datos.get("nivel")
        if isinstance(nivel, (int, float)) and not isinstance(nivel, bool):
            self._ultima_atencion = max(0.0, min(1.0, float(nivel)))

    def _ev_vision_estado(self, evento: Evento) -> None:
        self._ultimo_estado_vision = dict(evento.datos)

    def _ev_pantalla_estado(self, evento: Evento) -> None:
        """Última muestra de la pantalla (percepción, "no molestar" y vetos de travesuras)."""
        self._ultima_pantalla = dict(evento.datos)
        self._ultima_pantalla_ts = self._reloj()
        self._recalcular_no_molestar()

    def _ev_pantalla_estimulo(self, evento: Evento) -> None:
        """Cambio en la pantalla → ``ventana_cambia`` con la clase real (≤ 1 cada 30 s y si cambia la firma)."""
        firma = evento.datos.get("firma")
        if not isinstance(firma, str) or not firma:
            return
        if firma == self._ultima_firma_pantalla:
            return
        ahora = self._reloj()
        if ahora - self._ultima_inyeccion_pantalla < INTERVALO_PANTALLA_ESTIMULO_S:
            return
        clase = str((self._ultima_pantalla or {}).get("clase") or "")[:64]
        self._ultima_inyeccion_pantalla = ahora
        self._ultima_firma_pantalla = firma
        self._inyectar("ventana_cambia", clase=clase, titulo=firma[:64])

    def _pantalla_fresca(self, maximo_s: float = PANTALLA_FRESCA_S) -> dict[str, Any] | None:
        """Última ``pantalla.estado`` si tiene ≤ ``maximo_s`` segundos."""
        if self._ultima_pantalla is None or self._reloj() - self._ultima_pantalla_ts > maximo_s:
            return None
        return self._ultima_pantalla

    def _video_activo(self) -> bool:
        pantalla = self._pantalla_fresca()
        return bool(pantalla and (pantalla.get("video") or pantalla.get("reproduciendo")))

    def _estado_vision(self) -> dict[str, Any] | None:
        """``vision.estado()`` como dict (o el último ``vision.estado`` del bus)."""
        if self.vision is not None:
            try:
                estado = self.vision.estado()
                a_dict = getattr(estado, "a_dict", None)
                if callable(a_dict):
                    return dict(a_dict())
            except Exception as exc:
                log.debug("vision.estado() no disponible (%s)", type(exc).__name__)
        return self._ultimo_estado_vision

    def _percepcion(self) -> dict[str, Any] | None:
        """``ContextoBroca.percepcion`` con la última muestra local (§C2.14.10): solo escalares.

        App y título corto (``""`` si la ventana es privada) de ``pantalla.estado`` (≤ 10 s);
        rostro, atención y distancia de la visión. Sin visión: ``None`` (nada que contar).
        """
        percepcion: dict[str, Any] = {}
        pantalla = self._pantalla_fresca()
        if pantalla is not None:
            privada = bool(pantalla.get("privada"))
            percepcion["app"] = str(pantalla.get("clase") or "")
            percepcion["titulo_corto"] = "" if privada else str(pantalla.get("titulo") or "")[:60]
            percepcion["video"] = pantalla.get("video") if isinstance(pantalla.get("video"), bool) else None
            completa = pantalla.get("pantalla_completa")
            percepcion["pantalla_completa"] = completa if isinstance(completa, bool) else None
        estado = self._estado_vision()
        if isinstance(estado, Mapping) and estado.get("camara") not in (None, "off"):
            presente = estado.get("presente")
            percepcion["rostro"] = presente if isinstance(presente, bool) else None
            atencion = self._ultima_atencion if self._ultima_atencion is not None else estado.get("atencion")
            percepcion["atencion"] = atencion if isinstance(atencion, (int, float)) else None
            if self._ultimo_rostro is not None and isinstance(self._ultimo_rostro.get("dist_m"), (int, float)):
                percepcion["dist_m"] = self._ultimo_rostro.get("dist_m")
        return percepcion or None

    def _apagar_vision_camara(self) -> None:
        """La cámara continua se suelta (``set_camara(False)``: el hilo hace ``release()``)."""
        if self.vision is None:
            return
        try:
            self.vision.set_camara(False)
        except Exception as exc:
            log.debug("vision.set_camara(False) falló: %s", type(exc).__name__)

    def _pausar_vision(self, minutos: float | None) -> str:
        """Pausa la cámara continua (``None`` = hasta que la reanuden). Devuelve el texto para el usuario."""
        if self.vision is None:
            if self.aura is not None and getattr(self.aura, "camara_habilitada", False):
                self.aura.set_camara(False)
                return "Cámara deshabilitada: ya no hago vistazos (/camara on para volver)."
            return "No tengo ninguna cámara encendida: no estoy mirando."
        segundos = None if minutos is None else max(1.0, min(1440.0, float(minutos))) * 60.0
        try:
            self.vision.pausar(segundos, "usuario")
        except Exception as exc:
            log.warning("No se pudo pausar la visión: %s", type(exc).__name__)
            return "No pude pausar la cámara; usa /camara off."
        if segundos is None:
            return "Cámara en pausa hasta que escribas /vision on."
        return f"Cámara en pausa durante {formatear_decimal(segundos / 60.0, 0)} min."

    # --- no molestar ----------------------------------------------------------

    def _no_molestar_activo(self) -> bool:
        """``pc.no_molestar.activo`` (último publicado) o el "no molestar" del usuario."""
        return self._no_molestar_usuario or bool(self._no_molestar_publicado and self._no_molestar_publicado[0])

    async def _evaluar_no_molestar(self) -> None:
        """Sondeo (cada 5 s): ``ControlPC.no_molestar()`` OR ``pantalla.estado.no_molestar`` OR usuario."""
        sistema: tuple[bool, str] = (False, "")
        consultar = getattr(self.control_pc, "no_molestar", None) if self.control_pc is not None else None
        if callable(consultar):
            try:
                resultado = await asyncio.wait_for(_quizas_await(consultar()), timeout=2.0)
                if isinstance(resultado, (tuple, list)) and len(resultado) == 2:
                    sistema = (bool(resultado[0]), str(resultado[1] or ""))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # incluye TimeoutError
                log.debug("ControlPC.no_molestar() no respondió (%s)", type(exc).__name__)
        self._no_molestar_sistema = sistema
        self._recalcular_no_molestar()

    def _recalcular_no_molestar(self) -> None:
        """Combina las fuentes y publica ``pc.no_molestar{activo, motivo}`` solo si cambia."""
        sistema = self._no_molestar_sistema
        pantalla = self._pantalla_fresca(15.0)
        if self._no_molestar_usuario:
            nuevo = (True, "usuario")
        elif sistema[0]:
            motivo = sistema[1] if sistema[1] in ("presentacion", "pantalla_completa", "ocupado", "juego") else "ocupado"
            nuevo = (True, motivo)
        elif pantalla is not None and bool(pantalla.get("no_molestar")):
            nuevo = (True, "pantalla")
        else:
            nuevo = (False, "")
        if nuevo == self._no_molestar_publicado:
            return
        anterior = self._no_molestar_publicado
        self._no_molestar_publicado = nuevo
        if anterior is None and not nuevo[0]:
            return      # el estado inicial "sin no molestar" no es un cambio
        self._publicar("pc.no_molestar", {"activo": nuevo[0], "motivo": nuevo[1]})

    # --- travesuras (dueño único: main, §C2.14.4) ---------------------------

    def _travesuras_efectivas(self) -> bool:
        """Maestro ``RAMS_TRAVESURAS`` **y** activas **y** no apagadas **y** sin "no molestar" del usuario."""
        estado = self._travesuras
        if not getattr(self.settings, "travesuras_habilitadas", False) or not estado.activas:
            return False
        if self._no_molestar_usuario:
            return False
        hasta = estado.apagadas_hasta_ts
        return hasta is None or self._reloj_pared() >= hasta

    def _publicar_travesuras(self, *, forzar: bool = False) -> None:
        """``motor.travesuras{activas, hasta_ts}`` al cambiar el estado efectivo (o forzado)."""
        activas = self._travesuras_efectivas()
        hasta = self._travesuras.apagadas_hasta_ts
        if hasta is not None and self._reloj_pared() >= hasta:
            hasta = None
        estado = (activas, hasta)
        if not forzar and estado == self._travesuras_publicadas:
            return
        self._travesuras_publicadas = estado
        self._publicar("motor.travesuras", {"activas": activas, "hasta_ts": hasta})

    def _fecha_local(self, ts: float) -> datetime:
        try:
            return datetime.fromtimestamp(ts, zona_local(self.settings))
        except (OverflowError, OSError, ValueError):
            return datetime.fromtimestamp(ts, timezone.utc)

    def _set_travesuras(self, evento: str) -> str:
        """Único punto de cambio de las travesuras: ``"basta"``, ``"off"``, ``"on"`` o ``"aborto"``.

        - ``basta``: 1.º del día → apagadas 2 h; 2.º o más → hasta las 00:00 locales de mañana.
        - ``off``: desactivadas hasta ``on``. ``on``: activas y sin plazo (avisa si el maestro está OFF).
        - ``aborto`` (el usuario movió el ratón > 6 px durante una travesura): apagadas 2 h.

        Persiste en ``data/travesuras.json`` (``asyncio.to_thread``) y publica ``motor.travesuras``.
        Devuelve el texto para el usuario.
        """
        estado = self._travesuras
        ahora = self._reloj_pared()
        maestro = bool(getattr(self.settings, "travesuras_habilitadas", False))
        if evento == "basta":
            hoy = self._fecha_local(ahora).strftime("%Y-%m-%d")
            if estado.fecha != hoy:
                estado.basta_hoy = 0
                estado.fecha = hoy
            estado.basta_hoy += 1
            if estado.basta_hoy == 1:
                estado.apagadas_hasta_ts = max(estado.apagadas_hasta_ts or 0.0, ahora + TRAVESURAS_BASTA_S)
                hasta = self._fecha_local(estado.apagadas_hasta_ts).strftime("%H:%M")
                mensaje = f"Travesuras apagadas hasta las {hasta}. Me quedo quieta."
            else:
                manana = self._fecha_local(ahora).date() + timedelta(days=1)
                medianoche = datetime(manana.year, manana.month, manana.day, tzinfo=zona_local(self.settings))
                estado.apagadas_hasta_ts = max(estado.apagadas_hasta_ts or 0.0, medianoche.timestamp())
                mensaje = "Travesuras apagadas hasta mañana. Entendido a la primera… bueno, a la segunda."
        elif evento == "off":
            estado.activas = False
            mensaje = "Travesuras desactivadas hasta que escribas /travesuras on."
        elif evento == "on":
            estado.activas = True
            estado.apagadas_hasta_ts = None
            if maestro:
                mensaje = "Travesuras activadas. Si te molestan, di «basta» o pulsa Ctrl+Alt+Shift+Q."
            else:
                mensaje = ("Las travesuras con el cursor están desactivadas en la configuración: "
                           "RAMS_TRAVESURAS=1 en .env y reinicia.")
        elif evento == "aborto":
            estado.apagadas_hasta_ts = max(estado.apagadas_hasta_ts or 0.0, ahora + TRAVESURAS_ABORTO_S)
            mensaje = "Moviste el ratón: travesuras apagadas durante 2 h."
        else:
            log.debug("Evento de travesuras desconocido: %s", str(evento)[:20])
            return "Uso: /travesuras on|off"
        log.info("Travesuras: %s", evento)
        self._guardar_travesuras_luego()
        self._publicar_travesuras()
        return mensaje

    def _guardar_travesuras_luego(self) -> None:
        """Programa la escritura de ``data/travesuras.json`` (si hay loop; si no, se omite)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            log.debug("Sin loop: no se persiste el estado de las travesuras")
            return
        self._crear_tarea(self._guardar_travesuras(), "travesuras_guardar")

    async def _guardar_travesuras(self) -> None:
        """Escribe el estado ACTUAL (serializado: la última escritura siempre es la más nueva)."""
        async with self._lock_archivos:
            datos = self._travesuras.a_dict()
            try:
                await asyncio.to_thread(_escribir_json_privado, self._ruta_datos("travesuras.json"), datos)
            except Exception as exc:
                log.warning("No se pudo guardar el estado de las travesuras: %s", type(exc).__name__)

    async def _guardar_agenda_buscar(self) -> None:
        async with self._lock_archivos:
            ids = sorted(self._agenda_buscar)
            try:
                await asyncio.to_thread(_escribir_json_privado, self._ruta_datos("agenda_buscar.json"), ids)
            except Exception as exc:
                log.warning("No se pudo guardar la agenda de búsquedas: %s", type(exc).__name__)

    def _ev_motor_travesura(self, evento: Evento) -> Any:
        if self._apagando:
            return None
        return self._al_travesura(evento.datos)

    def _travesura_por_ritmo(self) -> bool:
        """Nunca dos travesuras seguidas sin una entrada del usuario entre medias (§C2.12)."""
        ultima = self._ultima_travesura_mono
        if ultima is None or self._ultimo_turno_local_mono > ultima:
            return True
        inactividad = getattr(self.control_pc, "inactividad_s", None) if self.control_pc is not None else None
        if callable(inactividad):
            try:
                segundos = inactividad()
            except Exception as exc:
                log.debug("inactividad_s falló: %s", type(exc).__name__)
                segundos = None
            # El usuario tocó teclado/ratón más de 2 s después de la travesura (no es nuestra inyección).
            if isinstance(segundos, (int, float)) and (self._reloj() - ultima) - float(segundos) > 2.0:
                return True
        return False

    async def _al_travesura(self, datos: Mapping[str, Any]) -> None:
        """``motor.travesura`` → guardas de estado (ControlPC) → compuerta → ``empujon_cursor``."""
        if self.control_pc is None or self.permisos is None:
            return
        if not self._travesuras_efectivas() or self._no_molestar_activo():
            log.debug("Travesura descartada: travesuras apagadas o «no molestar»")
            return
        patron = datos.get("patron")
        amplitud = datos.get("amplitud_px")
        segundos = datos.get("segundos")
        if (patron not in PATRONES_TRAVESURA or isinstance(amplitud, bool) or not isinstance(amplitud, int)
                or isinstance(segundos, bool) or not isinstance(segundos, (int, float))):
            log.debug("motor.travesura con un payload no válido; se descarta")
            return
        if not self._travesura_por_ritmo():
            log.debug("Travesura descartada: no hubo entrada del usuario desde la anterior")
            return
        puede = getattr(self.control_pc, "puede_travesura", None)
        if not callable(puede):
            return
        try:
            ok, motivo = await asyncio.wait_for(_quizas_await(puede(
                presente=self._usuario_presente(),
                voz_activa=bool(getattr(self.riko, "hablando", False)),
                confirmacion_pendiente=self.permisos.hay_pendiente(),
                video_activo=self._video_activo())), timeout=2.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("puede_travesura no respondió (%s)", type(exc).__name__)
            return
        if not ok:
            log.debug("Travesura vetada: %s", str(motivo)[:80])
            return
        accion = AccionPC("travesura_cursor", {"patron": patron, "amplitud_px": amplitud, "segundos": float(segundos)},
                          "travesura de cursor", origen="iniciativa")
        resultado, _error = await self._accion_pc(accion, canal="iniciativa")
        if resultado.datos.get("abortada_usuario"):
            self._set_travesuras("aborto")
            return
        if not resultado.ok:
            return
        self._ultima_travesura_mono = self._reloj()
        await self._decir_travesura()

    async def _decir_travesura(self) -> None:
        """Comentario tsundere tras la travesura (si puede interrumpir; nunca por Telegram)."""
        if self.riko is None or self.broca is None or not self._usuario_presente():
            return
        canal = "voz" if self.riko.voz_disponible else "terminal"
        try:
            permitido, _motivo = self.riko.puede_proactivo(canal=canal)
            if not permitido:
                return
            reply = self.broca.frase_local("travesura_cursor", self.conectoma.estado())
            await self.riko.decir(reply, origen="iniciativa", prioridad=0, canal=canal, motivo="travesura")
        except Exception as exc:
            log.debug("Sin comentario de la travesura: %s", type(exc).__name__)

    # --- bucle de vigilancia (5 s) -------------------------------------------

    async def _bucle_vigilancia(self) -> None:
        """Cada 5 s: "no molestar" del sistema, vencimiento de las travesuras y rostro → presencia."""
        while not self.parar.is_set():
            if not self._apagando:
                await self._paso_vigilancia()
            if await self._esperar(SONDEO_VIGILANCIA_S):
                return

    async def _paso_vigilancia(self) -> None:
        """Un paso del bucle de vigilancia (separado para poder probarlo)."""
        await self._evaluar_no_molestar()
        self._publicar_travesuras()      # publica solo si vence apagadas_hasta_ts (cambio efectivo)
        self._refrescar_rostro()

    def _refrescar_rostro(self) -> None:
        """``registrar_rostro(True)`` cada 30 s mientras ``vision.estado().presente is True``."""
        if self.vision is None or self.presencia is None:
            return
        if self._reloj() - self._ultimo_refresco_rostro < REFRESCO_ROSTRO_S:
            return
        estado = self._estado_vision()
        if isinstance(estado, Mapping) and estado.get("presente") is True:
            self._registrar_rostro_presencia(True)

    def _al_latir(self, estado: HomeostaticState) -> None:
        """Callback síncrono de ``latir`` (§6.2): publica estado, iniciativas, cambios de línea y fuente."""
        try:
            self.bus.publicar("homeostasis.estado", {"estado": estado.a_dict(), "bump": list(estado.bump)},
                              origen="conectoma")
            if estado.iniciativa:
                self.bus.publicar("homeostasis.iniciativa", {
                    "tipo": estado.iniciativa, "motivo": estado.motivo_iniciativa, "estado": estado.a_dict(),
                    "antes": estado.linea_antes, "despues": estado.linea_despues}, origen="conectoma")
            if self._ultimo_campo is not None and estado.campo != self._ultimo_campo:
                self.bus.publicar("homeostasis.cambio_linea", {
                    "antes": self._ultimo_campo, "despues": estado.campo,
                    "divergencia": round(estado.divergencia, 6)}, origen="conectoma")
            self._ultimo_campo = estado.campo
            if self._ultima_fuente is not None and estado.fuente != self._ultima_fuente:
                self.bus.publicar("sistema.modulo", self.conectoma.diagnostico().a_dict(), origen="conectoma")
            self._ultima_fuente = estado.fuente
        except Exception as exc:
            log.warning("Error al publicar el latido homeostático: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # Presencia y canal
    # ------------------------------------------------------------------

    def _actualizar_presencia(self, *, por_entrada_local: bool = False) -> EstadoPresencia:
        """``presencia.actualizar()`` y publica ``usuario.presencia`` solo si cambia ``presente``.

        En la transición a ausente guarda ``_ausente_desde_ts``; de ausente a presente tras más de
        30 min (detectada por el bucle, no por una entrada local) marca ``_regreso_pendiente``.
        """
        nuevo = self.presencia.actualizar()
        previo = self._presente_publicado
        if previo is None or nuevo.presente != previo:
            self.bus.publicar("usuario.presencia", nuevo.a_dict(), origen="main")
            if previo is not None:
                if not nuevo.presente:
                    self._ausente_desde_ts = time.time()
                elif not por_entrada_local:
                    ausente_s = time.time() - self._ausente_desde_ts if self._ausente_desde_ts else 0.0
                    if ausente_s > 1800:
                        self._regreso_pendiente = True
                    else:
                        self._ausente_desde_ts = None
            self._presente_publicado = nuevo.presente
        if por_entrada_local and nuevo.presente:
            self._ausente_desde_ts = None
        return nuevo

    @staticmethod
    def _ausente_seguro(p: EstadoPresencia) -> bool:
        return (not p.presente) and p.confianza >= 0.8

    def _modo_salida_respuesta(self, canal_entrada: str, intencion: Intencion) -> str:
        """Modo de salida de una respuesta (decisión ÚNICA del turno, §10.21)."""
        if canal_entrada == "telegram":
            return "telegram"
        if canal_entrada == "nexus":
            return "terminal"
        if intencion.explicacion_tecnica:
            return "terminal_largo"          # nunca va a voz
        # v0.2: el chat de escritorio responde por texto (chat y globo, labios mudos) salvo
        # RAMS_ESCRITORIO_VOZ=1, que aplica la regla de voz de la v0.1.
        if canal_entrada == "escritorio" and not getattr(self.settings, "escritorio_voz", False):
            return "terminal"
        p = self.presencia.estado()
        return "voz" if (p.presente and self.riko.voz_disponible) else "terminal"

    def _modo_salida_proactivo(self) -> str:
        """Modo de salida de iniciativas, agenda, alertas y Nexus."""
        p = self.presencia.estado()
        if p.presente:
            return "voz" if self.riko.voz_disponible else "terminal"
        return "telegram" if self.settings.tiene_telegram else "terminal"

    def _canal_proactivo(self) -> tuple[str, str]:
        modo = self._modo_salida_proactivo()
        return modo, CANAL_DE_MODO.get(modo, "terminal")

    def _duracion_sesion_s(self) -> float:
        return max(0.0, time.time() - self._inicio_ts)

    def _sesion_especial(self) -> bool:
        """Sesión "especial" (§6.6): duró ≥ 2 h o se marcó un logro con ``/logro``."""
        return self._duracion_sesion_s() >= 7200 or self._logro_sesion

    # ------------------------------------------------------------------
    # Entrada de consola y Telegram
    # ------------------------------------------------------------------

    async def _al_linea(self, linea: str | None) -> None:
        """Una línea de la terminal: resuelve confirmaciones o crea un turno serializado."""
        if linea is None:
            if self.consola.stdin_es_tty:
                self._motivo_parada = self._motivo_parada or (
                    "señal" if self.consola.motivo_fin == "señal" else "eof")
                self.parar.set()
            else:
                log.info("Fin de la entrada estándar; sigo en modo headless")
            return
        if self.permisos is not None and self.permisos.hay_pendiente():
            self.permisos.resolver(linea)
            return
        if not linea.strip() or self._apagando:
            return
        self._crear_tarea(self._turno_serializado(linea, "terminal"), "turno")

    async def _manejar_telegram(self, texto: str) -> None:
        """Mensaje de Telegram (ya filtrado por riko): turno serializado en segundo plano."""
        if self._apagando:
            return
        self._crear_tarea(self._turno_serializado(texto, "telegram"), "turno_telegram")

    def _crear_tarea(self, corrutina: Awaitable[Any], nombre: str) -> asyncio.Task[Any]:
        tarea = asyncio.ensure_future(corrutina)
        self._tareas_sueltas.add(tarea)
        tarea.add_done_callback(self._tareas_sueltas.discard)
        return tarea

    async def _turno_serializado(self, texto: str, canal: str) -> None:
        """Toma ``_lock_turno`` y procesa el turno; un error nunca tumba nada (frase ``error_propio``)."""
        async with self._lock_turno:
            try:
                await self.procesar_entrada(texto, canal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Error en un turno por %s: %s", canal, type(exc).__name__)
                try:
                    reply = self.broca.frase_local("error_propio", self.conectoma.estado())
                    if canal == "telegram":
                        destino = "telegram"
                    elif canal == "escritorio":
                        destino = "terminal"        # §C2.14.3: el error del chat va por texto
                    else:
                        destino = "voz" if (self.presencia.estado().presente and self.riko.voz_disponible) \
                            else "terminal"
                    await self.riko.decir(reply, origen="respuesta", canal=destino)
                except Exception as exc2:
                    log.error("Tampoco se pudo avisar del error: %s", type(exc2).__name__)

    # ------------------------------------------------------------------
    # Turno (§10.21, 9 pasos)
    # ------------------------------------------------------------------

    async def procesar_entrada(self, texto: str, canal: str = "terminal") -> BrocaReply | None:
        """Procesa una entrada del usuario y devuelve la respuesta (``None`` para comandos y confirmaciones)."""
        texto = str(texto or "").strip()
        if not texto or self._apagando:
            return None
        if not self._iniciado or self.memoria is None:
            raise RuntimeError("El orquestador no está iniciado: llama antes a iniciar()")
        local = canal in CANALES_LOCALES
        # 0. Confirmación pendiente: la línea es la respuesta SOLO desde la terminal. El chat de
        # escritorio confirma únicamente con los botones (§C2.12): un "sí" escrito no aprueba nada.
        if canal == "terminal" and self.permisos is not None and self.permisos.hay_pendiente():
            self.permisos.resolver(texto)
            return None
        s = self.settings
        secretos = s.secretos()
        # v0.2: el texto del chat se redacta también por alta entropía (bus, memoria y Broca); la
        # intención se calcula sobre el texto crudo, como en la v0.1. Los argumentos que citan al
        # usuario (consulta, contenido, mensaje) se redactan con la MISMA regla en el paso 2c.
        alta = canal == "escritorio"
        texto_seguro = redactar(texto, secretos, alta_entropia=alta)
        log.debug("Turno por %s (%d caracteres)", canal, len(texto))

        # 1. Presencia, regreso y llamadas.
        ahora_ts = time.time()
        regreso = False
        llamadas_perdidas = 0
        consolidaciones = 0
        if local:
            s_entrada = self.presencia.segundos_desde_entrada_local()
            p_prev = self.presencia.estado()
            regreso = self._regreso_pendiente or (s_entrada is not None and s_entrada > 1800
                                                  and not p_prev.presente)
            if regreso:
                desde = self._ausente_desde_ts or (ahora_ts - (s_entrada or 0.0))
                llamadas_perdidas = int(await self.memoria.contar_llamadas(desde, solo_sin_contestar=True) or 0)
                consolidaciones = int(await self.memoria.contar_consolidaciones(desde) or 0)
                self._regreso_pendiente = False
            self.presencia.registrar_entrada(canal)          # terminal o escritorio
            self._actualizar_presencia(por_entrada_local=True)
            self._ultimo_turno_local_mono = self._reloj()
            if self.aura is not None:
                try:
                    self.aura.reiniciar_backoff_camara()
                except Exception as exc:
                    log.debug("No se pudo reiniciar el backoff de la cámara: %s", type(exc).__name__)
            if self.vision is not None:
                calibrar = getattr(self.vision, "calibrar_atencion", None)
                if callable(calibrar):
                    try:
                        calibrar()      # el usuario escribe: mira la pantalla (referencia de atención)
                    except Exception as exc:
                        log.debug("calibrar_atencion falló: %s", type(exc).__name__)
            await self.memoria.marcar_llamadas_contestadas(desde_ts=ahora_ts - 7200)
        elif canal == "telegram":
            self.presencia.registrar_entrada("telegram")
            await self.memoria.marcar_llamadas_contestadas(desde_ts=ahora_ts - 7200)
        # Los payloads del bus nunca llevan secretos (§5): se publica el texto ya redactado.
        self.bus.publicar("usuario.entrada", {"texto": texto_seguro, "canal": canal}, origen="main")

        # 2. Intención (sin LLM) y comandos. v0.2: sinónimos de RAMS_DESTINOS y, en los canales
        # locales, lanzar_app (RAMS_APPS) solo en Linux.
        ahora = ahora_local(s)
        intencion = enrutar(texto, ahora=ahora, apps=s.apps_permitidas, canal=canal,
                            destinos=getattr(s, "destinos_permitidos", None),
                            plataforma=sys.platform if local else None)
        entrada_broca: str | None = texto_seguro
        texto_turno = texto_seguro
        instruccion_extra: str | None = None
        frase_fija: tuple[str, dict[str, Any]] | None = None   # v0.2: respuesta local sin Broca
        if intencion.tipo == "comando":
            comando = str(intencion.args.get("comando", ""))
            resto = str(intencion.args.get("resto", ""))
            if comando == "/buscar":
                if not resto:
                    await self._salida_comando("Uso: /buscar <consulta>", canal)
                    return None
                intencion = Intencion("buscar_web", {"consulta": resto},
                                      explicacion_tecnica=intencion.explicacion_tecnica)
            elif comando == "/devolver_llamada":
                instruccion_extra = await self._instruccion_devolver_llamada()
                entrada_broca = None
                texto_turno = "(devolvió la llamada de Amadeus)"
                intencion = Intencion("ninguna", {})
            else:
                await self._ejecutar_comando(comando, resto, canal)
                return None
        elif intencion.tipo == "ninguna" and texto.startswith("/"):
            primera = texto.split()[0][:40]
            await self._salida_comando(f"Comando desconocido: {primera}. Escribe /ayuda.", canal)
            return None
        # 2b. Recordatorio a medias del turno anterior: esta línea puede traer la hora o el mensaje.
        accion_pendiente: ResultadoAccion | None = None
        if entrada_broca is not None:
            intencion, accion_pendiente = self._resolver_programar_pendiente(texto, intencion, ahora, canal)
        # 2c. Los argumentos de la intención salen del texto crudo: se redactan aquí, una sola vez y
        # con la regla del canal, antes de llegar a la memoria, la agenda, el bus, la red o Broca.
        intencion = _intencion_redactada(intencion, secretos, alta_entropia=alta)

        # 3. Herramientas locales.
        accion: ResultadoAccion | None = accion_pendiente
        busqueda_web: tuple[Any, ...] | None = None
        telemetria: Any = None
        vision: ResultadoVistazo | None = None
        png: bytes | None = None
        memoria_relevante: list[Any] | None = None
        error_propio = False
        tipo = intencion.tipo
        args = intencion.args
        if tipo == "buscar_web":
            consulta = str(args.get("consulta") or texto_seguro)
            if self.web is None:
                accion = ResultadoAccion(False, "Búsqueda web no disponible en esta instalación.",
                                         accion_tipo="buscar_web")
            elif not s.tiene_web:
                # Nunca fingir "sin resultados": en --offline la red no se consulta.
                accion = ResultadoAccion(False, "Búsqueda web desactivada: R.A.M.S. está en modo --offline.",
                                         accion_tipo="buscar_web")
            else:
                busqueda_web = await self._buscar_web(consulta, presupuesto_s=10.0)
        elif tipo == "reporte_sistema":
            if self.aura is None:
                accion = ResultadoAccion(False, "Telemetría no disponible en esta instalación.",
                                         accion_tipo="reporte_sistema")
            else:
                telemetria = await self.aura.telemetria()
        elif tipo == "recordar":
            contenido = str(args.get("contenido") or "")
            rid = await self.memoria.recordar(contenido, categoria="hecho", importancia=0.7)
            if rid:
                accion = ResultadoAccion(True, f"Anotado en memoria (fragmento #{rid}).", accion_tipo="recordar")
            else:
                accion = ResultadoAccion(False, "No pude guardar ese fragmento en la memoria.", accion_tipo="recordar")
                error_propio = True
        elif tipo == "programar":
            accion = await self._programar(args, ahora)
            error_propio = not accion.ok
        elif tipo == "programar_incompleto":
            accion = self._programar_incompleto(args, ahora)
            self._guardar_programar_pendiente(args, canal)
        elif tipo == "mirar_pantalla":
            if sys.platform == "win32":
                # §C2.14.10: en Windows no hay captura para Broca (la visión local no manda imágenes);
                # no es un error propio.
                accion = ResultadoAccion(False, "Mirar la pantalla no está disponible en este sistema.",
                                         accion_tipo="captura_pantalla")
            elif self.aura is None:
                accion = ResultadoAccion(False, "Captura de pantalla no disponible en esta instalación.",
                                         accion_tipo="captura_pantalla")
            else:
                png = await self.aura.capturar_pantalla(origen="usuario")
                if png is None:
                    accion = ResultadoAccion(False, "No pude capturar la pantalla en este sistema.",
                                             accion_tipo="captura_pantalla")
        elif tipo == "vistazo":
            if self.aura is None:
                accion = ResultadoAccion(False, "Visión no disponible en esta instalación.", accion_tipo="vistazo")
            else:
                vision = await self.aura.vistazo("usuario")
                self.presencia.registrar_rostro(vision.rostro)
                if vision.error is None:
                    self._stats["vistazos"] += 1
        elif tipo == "accion_pc":
            accion_usuario = self._ajustar_accion_usuario(args)
            accion, error_propio = await self._accion_pc(accion_usuario, canal=canal)
            if accion.ok and args.get("nota") == NOTA_ESCRIBIR_DESPUES:
                # Sin "nombre_destino": la frase local "accion_abrir_ok" solo dice el destino y se
                # comería la indicación; "accion_ok" lleva el mensaje completo (con o sin Gemini).
                datos_sin_nombre = {k: v for k, v in (accion.datos or {}).items() if k != "nombre_destino"}
                accion = dataclasses.replace(accion, datos=datos_sin_nombre, mensaje=(
                    f"{accion.mensaje.rstrip()} Cuando tengas abierta la caja de texto, dime «escribe ahí: …» "
                    "y lo tecleo (con tu confirmación).").strip())
        elif tipo == "memoria_consulta":
            memoria_relevante = await self.memoria.buscar(str(args.get("consulta") or texto_seguro), 8)
        elif tipo == "bloqueada_canal":
            accion = ResultadoAccion(False, MENSAJE_SOLO_LOCAL, accion_tipo=str(args.get("tipo_original", "")))
        # v0.2 (§C2.14.8, §C2.14.11): borrador, "búscame", escritorio, travesuras y visión.
        elif tipo == "publicar_borrador":
            borrador = self._borrador_vigente()
            if borrador is None:
                # Sin Gemini no hay borradores (el banco local no redacta): esa variante ofrece la vía
                # directa («pon en X: …») en vez de pedir un texto que nunca llegará.
                con_llm = bool(getattr(self.broca, "disponible", False))
                frase_fija = ("sin_borrador" if con_llm else "sin_borrador_sin_llm", {})
            else:
                accion, error_propio = await self._accion_pc(
                    self._accion_borrador(str(args.get("alias") or ""), borrador), canal=canal)
        elif tipo == "recordatorio_personal":
            accion = await self._recordatorio_personal(args, ahora)
            error_propio = not accion.ok
        elif tipo == "escritorio_venir":
            if self._overlay_disponible():
                self._orden_escritorio("aparecer", borde=self._rng.choice(("left", "right")), modo="entrar")
                frase_fija = ("escritorio_aparece", {})
            elif self._overlay_conectado:
                accion = ResultadoAccion(False, "Amadeus Desktop está abierto, pero mi avatar no cargó: revisa que "
                                                "VTuber AI Studio esté corriendo.", accion_tipo="escritorio")
            else:
                accion = ResultadoAccion(False, "Amadeus Desktop no está conectado: ábrelo (npm start en desktop/) "
                                                "y vuelve a llamarme.", accion_tipo="escritorio")
        elif tipo == "escritorio_vete":
            if self._overlay_conectado:
                self._orden_escritorio("esconderse")
                accion = ResultadoAccion(True, "Me retiro a la bandeja; llámame con Ctrl+Alt+A o escribiendo «ven».",
                                         accion_tipo="escritorio")
            else:
                accion = ResultadoAccion(False, "No estoy en el escritorio ahora mismo.", accion_tipo="escritorio")
        elif tipo == "basta_travesuras":
            self._aviso_local(self._set_travesuras("basta"), canal)
            if self._overlay_conectado:
                self._orden_escritorio("esconderse")
            frase_fija = ("basta_travesuras", {})
        elif tipo == "vision_pausa":
            minutos = args.get("minutos")
            hay_camara = self.vision is not None or bool(
                self.aura is not None and getattr(self.aura, "camara_habilitada", False))
            mensaje = self._pausar_vision(minutos if isinstance(minutos, (int, float)) else None)
            if hay_camara:
                self._aviso_local(mensaje, canal)
                frase_fija = ("vision_pausada", {})
            else:
                accion = ResultadoAccion(True, mensaje, accion_tipo="vision")
        if accion is not None and not accion.ok and tipo in ("accion_pc", "recordar", "programar",
                                                             "publicar_borrador", "recordatorio_personal"):
            self._inyectar("error", severidad=0.3)

        # 4. Evento social.
        if regreso:
            evento_social: str | None = "regreso"
        elif error_propio:
            evento_social = "error_propio"
        elif entrada_broca is not None:
            evento_social = detectar_evento_social(texto)
        else:
            evento_social = None
        tipo_social = evento_social.split(":")[0] if evento_social else None
        if tipo_social in ("cumplido", "agradecimiento"):
            self._inyectar("recompensa", magnitud=0.6)
        despedida_especial = tipo_social == "despedida" and self._sesion_especial()

        # 5. Contexto para Broca.
        modo_salida = self._modo_salida_respuesta(canal, intencion)
        if memoria_relevante is None:
            memoria_relevante = await self.memoria.buscar(texto_seguro, 5)
        ventana = await self.memoria.ventana_activa()
        resumen = await self.memoria.resumen_reciente()
        vistazos_pendientes = 0
        if local:
            vistazos_pendientes = self._vistazos_pendientes
            self._vistazos_pendientes = 0
        alerta_critica = False
        if self._ultima_alerta_critica is not None and time.time() - self._ultima_alerta_critica[0] <= 600:
            alerta_critica = True
            detalle = self._ultima_alerta_critica[1]
            instruccion_extra = f"{detalle}. {instruccion_extra}" if instruccion_extra else detalle
        ultima_bitacora = None
        if self._primer_turno:
            ultima_bitacora = await self.memoria.ultima_bitacora()
            self._primer_turno = False
        ctx = ContextoBroca(
            estado=self.conectoma.estado(),
            modo_salida=modo_salida,
            canal_entrada=canal if canal in CANALES_ENTRADA else "nexus",
            presencia=self.presencia.estado(),
            telemetria=telemetria,
            vision=vision,
            vistazos_pendientes=vistazos_pendientes,
            memoria_relevante=tuple(memoria_relevante or ()),
            ventana_activa=tuple(ventana or ()),
            resumen_reciente=resumen,
            busqueda_web=busqueda_web,
            evento_social=evento_social,
            llamadas_perdidas=llamadas_perdidas if regreso else 0,
            consolidaciones_ausente=consolidaciones if regreso else 0,
            alerta_critica=alerta_critica,
            despedida_especial=despedida_especial,
            nexus=resumen_nexus(self._ultimo_nexus),
            accion=accion,
            ultima_bitacora=ultima_bitacora,
            instruccion_extra=instruccion_extra,
            fecha_hora=ahora,
            **self._extra_percepcion(local),
        )

        # 6. Verbalización (Broca: Gemini o banco local; nunca decide nada). v0.2: algunas
        # intenciones (aparecer, "basta", pausar la cámara, sin borrador) responden con su
        # frase local fija, sin gastar Gemini.
        if frase_fija is not None:
            categoria, fmt = frase_fija
            reply = self.broca.frase_local(categoria, ctx.estado, **fmt)
        else:
            reply = await self.broca.verbalizar(ctx, entrada_broca, imagen_png=png)

        # 7. Turnos en memoria (el del usuario y el de Amadeus).
        canal_salida = CANAL_DE_MODO.get(ctx.modo_salida, "terminal")
        await self.memoria.registrar_turno("usuario", texto_turno, canal=canal)
        await self.memoria.registrar_turno("amadeus", reply.texto_plano, canal=canal_salida,
                                           etiquetas=reply.etiquetas)
        self._stats["turnos"] += 1
        if entrada_broca is not None:
            self._temas.append(texto_seguro[:60])
        # v0.2 (§C2.14.8): el último borrador es una respuesta de Broca (redactada por el LLM) a
        # un turno LOCAL de conversación; nunca de Nexus, iniciativas, Telegram, una acción ni la
        # web: un turno con resultados de búsqueda, vistazo o captura (contenido de terceros que
        # puede traer una inyección de prompt) no deja borrador publicable (§C2.12 "Origen").
        if (local and frase_fija is None and entrada_broca is not None and reply.fuente != "local"
                and intencion.tipo in ("ninguna", "memoria_consulta") and busqueda_web is None
                and ctx.vision is None and png is None and reply.texto_plano):
            self._ultimo_borrador = (reply.texto_plano[:MAX_BORRADOR], self._reloj())
        elif local and reply.fuente != "local" and (busqueda_web is not None or ctx.vision is not None
                                                    or png is not None):
            # "Ponlo en X" justo después se referiría a ESTA respuesta, que no es publicable: se
            # olvida el borrador anterior para no publicar por sorpresa un texto más viejo.
            self._ultimo_borrador = None

        # 8. Salida por el canal elegido.
        await self.riko.decir(reply, origen="respuesta", canal=canal_salida)
        # 9.
        return reply

    def _extra_percepcion(self, incluir: bool) -> dict[str, Any]:
        """``{"percepcion": …}`` para ``ContextoBroca`` (solo turnos locales e iniciativas)."""
        if not incluir:
            return {}
        try:
            percepcion = self._percepcion()
        except Exception as exc:
            log.debug("Percepción no disponible (%s)", type(exc).__name__)
            return {}
        if percepcion is None or "percepcion" not in {f.name for f in dataclasses.fields(ContextoBroca)}:
            return {}
        return {"percepcion": percepcion}

    # --- v0.2: borradores, "búscame" y acciones del usuario ---------------------

    def _borrador_vigente(self) -> str | None:
        """El último borrador (≤ 500) si tiene menos de 10 min; si no, ``None`` (y se olvida)."""
        if self._ultimo_borrador is None:
            return None
        texto, ts = self._ultimo_borrador
        if self._reloj() - ts > TTL_BORRADOR_S or not texto.strip():
            self._ultimo_borrador = None
            return None
        return texto

    def _accion_borrador(self, alias: str, texto: str) -> AccionPC:
        """``publicar_borrador``: prerrelleno si el destino lo admite; si no, tecleo en la ventana previa."""
        meta = DESTINOS_BASE.get(alias) if isinstance(DESTINOS_BASE, Mapping) else None
        nombre = nombre_destino(alias)
        if isinstance(meta, Mapping) and meta.get("prerrelleno"):
            return AccionPC("prerrellenar", {"alias": alias, "texto": texto}, f"borrador en {nombre}", origen="usuario")
        return AccionPC("escribir_texto", {"texto": texto}, f"escribir en {nombre}", origen="usuario")

    def _ajustar_accion_usuario(self, args: Mapping[str, Any]) -> Any:
        """"escríbele a mamá: …" justo después de "abre whatsapp" → prerrelleno en WhatsApp.

        Prerrellenar antes que teclear (§C1.8): si la intención no trajo destino (``sin_alias``)
        y el usuario abrió hace < 2 min un destino que admite prerrelleno, se usa ese destino.
        """
        accion = args.get("accion")
        if (not isinstance(accion, AccionPC) or accion.tipo != "escribir_texto" or not args.get("sin_alias")
                or self._ultimo_destino is None):
            return accion
        alias, ts = self._ultimo_destino
        meta = DESTINOS_BASE.get(alias) if isinstance(DESTINOS_BASE, Mapping) else None
        if self._reloj() - ts > TTL_ULTIMO_DESTINO_S or not (isinstance(meta, Mapping) and meta.get("prerrelleno")):
            return accion
        return AccionPC("prerrellenar", {"alias": alias, "texto": accion.args.get("texto")},
                        f"borrador en {nombre_destino(alias)}", origen="usuario")

    async def _recordatorio_personal(self, args: Mapping[str, Any], ahora: datetime) -> ResultadoAccion:
        """"búscame en 10 minutos": recordatorio de la agenda marcado para ir a buscar al usuario."""
        vence = args.get("vence")
        if not isinstance(vence, datetime):
            return ResultadoAccion(False, "Falta la hora: pregunta para cuándo programar el recordatorio.",
                                   {"falta": "hora", "tipo": "recordatorio",
                                    "mensaje": MENSAJE_BUSCAR_DEFECTO}, accion_tipo="programar")
        mensaje = redactar(str(args.get("mensaje") or MENSAJE_BUSCAR_DEFECTO), self.settings.secretos())[:300]
        item_id = await self.memoria.programar("recordatorio", vence, mensaje)
        if not item_id:
            return ResultadoAccion(False, "No pude guardar el recordatorio en la agenda.", accion_tipo="programar")
        try:
            self._agenda_buscar.add(int(item_id))
        except (TypeError, ValueError):
            log.debug("Id de agenda no entero; el recordatorio queda como uno normal")
        else:
            self._crear_tarea(self._guardar_agenda_buscar(), "agenda_buscar_guardar")
        cuando = describir_vencimiento(vence, ahora)
        extra = f" para «{mensaje}»" if args.get("con_mensaje") else ""
        return ResultadoAccion(True, f"Te busco {cuando}{extra}.", {"id": item_id, "tipo": "recordatorio",
                                                                   "buscar": True}, accion_tipo="programar")

    async def _buscar_web(self, consulta: str, *, presupuesto_s: float) -> tuple[Any, ...]:
        """``web.buscar`` con plazo y publicación de ``web.resultado``."""
        try:
            resultados = await asyncio.wait_for(self.web.buscar(consulta, n=5, presupuesto_s=presupuesto_s),
                                                timeout=presupuesto_s + 2.0)
        except asyncio.TimeoutError:
            log.info("La búsqueda web superó su plazo")
            resultados = []
        self._stats["busquedas"] += 1
        resultados = tuple(resultados or ())
        self.bus.publicar("web.resultado", {"consulta": consulta, "n": len(resultados),
                                            "proveedor": getattr(self.web, "ultimo_proveedor", "ninguno")},
                          origen="main")
        return resultados

    def _programar_incompleto(self, args: Mapping[str, Any], ahora: datetime) -> ResultadoAccion:
        """Qué le falta a un ``programar_incompleto``: la hora (§10.12) o, si dio la hora, el mensaje.

        ``mensaje`` es la nota para Broca (§10.21, texto normativo). ``datos["falta"]`` le dice al
        banco local qué pregunta hacer (``programar_falta_hora`` / ``programar_falta_mensaje``): en
        modo no verbal nunca se lee en voz alta la nota escrita para el LLM.
        """
        vence = args.get("vence")
        tipo = "capsula" if args.get("tipo") == "capsula" else "recordatorio"
        if args.get("falta") == "mensaje" and isinstance(vence, datetime):
            cuando = describir_vencimiento(vence, ahora)
            datos = {"falta": "mensaje", "tipo": tipo, "cuando": cuando}
            if tipo == "capsula":
                return ResultadoAccion(False, f"Falta el mensaje: pregunta qué quiere guardar en la cápsula del "
                                              f"tiempo (se abriría {cuando}).", datos, accion_tipo="programar")
            return ResultadoAccion(False, f"Falta el mensaje: pregunta qué quiere que le recuerde ({cuando}).",
                                   datos, accion_tipo="programar")
        mensaje = redactar(str(args.get("mensaje") or ""), self.settings.secretos())
        return ResultadoAccion(False, "Falta la hora: pregunta para cuándo programar el recordatorio.",
                               {"falta": "hora", "tipo": tipo, "mensaje": mensaje}, accion_tipo="programar")

    def _guardar_programar_pendiente(self, args: Mapping[str, Any], canal: str) -> None:
        """Recuerda el recordatorio a medias para completarlo con la respuesta del turno siguiente."""
        vence = args.get("vence")
        falta = "mensaje" if args.get("falta") == "mensaje" and isinstance(vence, datetime) else "hora"
        self._programar_pendiente = {
            "falta": falta,
            "tipo": "capsula" if args.get("tipo") == "capsula" else "recordatorio",
            "mensaje": str(args.get("mensaje") or ""),
            "vence": vence if isinstance(vence, datetime) else None,
            "canal": canal,
            "expira": time.time() + PLAZO_PROGRAMAR_PENDIENTE_S,
        }

    def _resolver_programar_pendiente(self, texto: str, intencion: Intencion, ahora: datetime,
                                      canal: str) -> tuple[Intencion, ResultadoAccion | None]:
        """Completa (o cancela) el recordatorio a medias con la línea del usuario, sin LLM.

        Solo actúa sobre el pendiente del **mismo canal** y dentro de su plazo. Si la línea pide
        otra cosa (una búsqueda, una acción…), el pendiente se descarta y el turno sigue normal.
        Si la línea no aporta lo que faltaba, se cancela y se dice (nunca se pierde en silencio
        ni se deja creer que quedó programado).
        """
        pendiente = self._programar_pendiente
        if pendiente is None or pendiente.get("canal") != canal:
            return intencion, None
        self._programar_pendiente = None
        if time.time() > float(pendiente.get("expira", 0.0)):
            log.debug("Recordatorio pendiente caducado; se descarta")
            return intencion, None
        if intencion.tipo not in ("ninguna", "programar_incompleto"):
            log.info("Recordatorio pendiente descartado: la nueva entrada pide otra cosa (%s)", intencion.tipo)
            return intencion, None
        completada = completar_programar(texto, pendiente, ahora=ahora, intencion=intencion)
        if completada is not None:
            return completada, None
        tipo = "capsula" if pendiente.get("tipo") == "capsula" else "recordatorio"
        nombre = "la cápsula del tiempo" if tipo == "capsula" else "el recordatorio"
        contenido = redactar(str(pendiente.get("mensaje") or ""), self.settings.secretos())[:80]
        cita = f" «{contenido}»" if contenido else ""
        datos = {"cancelado": True, "falta": pendiente.get("falta"), "tipo": tipo}
        if quiere_cancelar(texto):
            log.info("Recordatorio pendiente cancelado por el usuario")
            return intencion, ResultadoAccion(True, f"Cancelé {nombre}{cita}; no quedó nada programado.",
                                              datos, accion_tipo="programar")
        log.info("Recordatorio pendiente cancelado: la respuesta no traía lo que faltaba")
        if pendiente.get("falta") == "mensaje":
            motivo = "No entendí qué querías que te recordara"
        else:
            motivo = "No entendí para cuándo"
        return intencion, ResultadoAccion(
            False, f"{motivo}, así que dejé sin programar {nombre}{cita}. Pídemelo otra vez con día y hora.",
            datos, accion_tipo="programar")

    async def _programar(self, args: Mapping[str, Any], ahora: datetime) -> ResultadoAccion:
        tipo = "capsula" if args.get("tipo") == "capsula" else "recordatorio"
        mensaje = redactar(str(args.get("mensaje") or ""), self.settings.secretos())
        vence = args.get("vence")
        if not isinstance(vence, datetime):
            return ResultadoAccion(False, "Falta la hora: pregunta para cuándo programar el recordatorio.",
                                   {"falta": "hora", "tipo": tipo, "mensaje": mensaje}, accion_tipo="programar")
        item_id = await self.memoria.programar(tipo, vence, mensaje)
        if not item_id:
            return ResultadoAccion(False, "No pude guardar el recordatorio en la agenda.", accion_tipo="programar")
        cuando = describir_vencimiento(vence, ahora)
        if tipo == "capsula":
            texto = f"Cápsula del tiempo sellada; se abre {cuando}."
        else:
            texto = f"Recordatorio para {cuando}: {mensaje}."
        return ResultadoAccion(True, texto, {"id": item_id, "tipo": tipo}, accion_tipo="programar")

    async def _accion_pc(self, accion: Any, *, canal: str = "terminal") -> tuple[ResultadoAccion, bool]:
        """Ejecuta una ``AccionPC`` (fijando antes el objetivo de ``cerrar_ventana``). Devuelve (resultado, error_propio).

        v0.2 (§C2.14.5): los tipos de ``TIPOS_ACCION_V02`` pasan **una** vez por
        ``permisos.autorizar`` y los ejecuta ``ControlPC``; nunca ``aura.ejecutar``. El canal
        queda en ``_CANAL_ACCION`` mientras dura (lo lee ``_ev_permiso_solicitado``).
        """
        token = _CANAL_ACCION.set(canal)
        try:
            return await self._accion_pc_en_canal(accion, canal)
        finally:
            _CANAL_ACCION.reset(token)

    async def _accion_pc_en_canal(self, accion: Any, canal: str) -> tuple[ResultadoAccion, bool]:
        """Cuerpo de :meth:`_accion_pc` (con ``_CANAL_ACCION`` ya fijado)."""
        if not isinstance(accion, AccionPC):
            return ResultadoAccion(False, "No entendí qué acción ejecutar."), False
        if accion.tipo in TIPOS_ACCION_V02:
            return await self._accion_pc_v02(accion, canal)
        if self.aura is None:
            return ResultadoAccion(False, "Control del PC no disponible en esta instalación.",
                                   accion_tipo=accion.tipo), False
        if accion.tipo == "cerrar_ventana":
            v = self._ventana_previa
            if v is None:
                return ResultadoAccion(False, "No sé qué ventana cerrar: no he visto otra ventana activa además "
                                              "de esta terminal.", accion_tipo="cerrar_ventana"), False
            accion = AccionPC("cerrar_ventana",
                              {"direccion": v["direccion"], "clase": v["clase"], "titulo": v["titulo"],
                               "pid": v["pid"]},
                              f"cerrar {v['clase']} «{str(v['titulo'])[:40]}»", origen="usuario")
        try:
            resultado = await self.aura.ejecutar(accion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La acción %s falló: %s", accion.tipo, type(exc).__name__, exc_info=True)
            return ResultadoAccion(False, "La acción falló por un error interno.", accion_tipo=accion.tipo), True
        if not isinstance(resultado, ResultadoAccion):
            return ResultadoAccion(False, "La acción no devolvió resultado.", accion_tipo=accion.tipo), True
        mensaje = resultado.mensaje or ""
        error_propio = (not resultado.ok and not mensaje.startswith("Acción no autorizada")
                        and "no disponible" not in mensaje and "no soportad" not in mensaje)
        return resultado, error_propio

    # --- v0.2: acciones de ControlPC (§C2.8, §C2.12) ------------------------------

    async def _accion_pc_v02(self, accion: AccionPC, canal: str) -> tuple[ResultadoAccion, bool]:
        """``abrir_destino``/``buscar_en``/``prerrellenar``/``escribir_texto``/``travesura_cursor``.

        Orden: objetivo de ``escribir_texto`` (ventana previa) → compuerta (una vez) → ControlPC
        → auditoría (``data/acciones_pc.jsonl`` sin el texto) y ``pc.accion``.
        """
        tipo = accion.tipo
        if self.control_pc is None:
            resultado = ResultadoAccion(False, "Control del PC no disponible en esta instalación.", accion_tipo=tipo)
            await self._auditar(accion, canal, "no_disponible", "ControlPC no disponible", resultado)
            return resultado, False
        if tipo == "escribir_texto":
            accion, sin_objetivo = self._con_objetivo(accion)
            if sin_objetivo is not None:
                await self._auditar(accion, canal, "sin_objetivo", "sin ventana previa", sin_objetivo)
                return sin_objetivo, False
        if self.permisos is None:
            resultado = ResultadoAccion(False, "Acción no autorizada: compuerta de permisos no disponible.",
                                        accion_tipo=tipo)
            return resultado, False
        try:
            decision = await self.permisos.autorizar(accion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La compuerta falló con la acción %s: %s", tipo, type(exc).__name__)
            return ResultadoAccion(False, "Acción no autorizada: la compuerta falló.", accion_tipo=tipo), False
        if not decision.aprobada:
            resultado = ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}", accion_tipo=tipo)
            await self._auditar(accion, canal, decision.veredicto if decision.veredicto != "confirmar" else "rechazada",
                                decision.motivo, resultado)
            return resultado, False
        try:
            control = await self._ejecutar_control(accion, canal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ControlPC falló con la acción %s: %s", tipo, type(exc).__name__, exc_info=True)
            resultado = ResultadoAccion(False, "La acción falló por un error interno.", accion_tipo=tipo)
            await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado)
            return resultado, True
        resultado = self._resultado_control(control, accion)
        if tipo == "abrir_destino" and resultado.ok:
            self._ultimo_destino = (str(accion.args.get("alias") or ""), self._reloj())
        await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado)
        return resultado, False

    def _con_objetivo(self, accion: AccionPC) -> tuple[AccionPC, ResultadoAccion | None]:
        """Rellena el objetivo de ``escribir_texto`` con ``ControlPC.ventana_previa()`` si no lo trae."""
        args = dict(accion.args)
        if args.get("pid") is not None and (args.get("hwnd") is not None or args.get("direccion") is not None):
            return accion, None
        previa = None
        obtener = getattr(self.control_pc, "ventana_previa", None)
        if callable(obtener):
            try:
                previa = obtener()
            except Exception as exc:
                log.debug("ventana_previa falló: %s", type(exc).__name__)
        a_args = getattr(previa, "a_args", None)
        if previa is None or not callable(a_args):
            return accion, ResultadoAccion(False, "No sé en qué ventana escribir: haz clic en la caja de texto de la app "
                                                  "y vuelve a pedírmelo.", accion_tipo="escribir_texto")
        objetivo = {k: v for k, v in dict(a_args()).items() if k in ("hwnd", "pid", "exe", "direccion", "clase")}
        return AccionPC("escribir_texto", {"texto": args.get("texto"), **objetivo}, accion.descripcion,
                        origen=accion.origen), None

    @staticmethod
    def _objetivo_ventana(args: Mapping[str, Any]) -> Any:
        """``ObjetivoVentana.desde_args`` del módulo de ControlPC (``None`` si no casa o no existe)."""
        try:
            modulo = sys.modules.get("lobes.control_pc") or importlib.import_module("lobes.control_pc")
        except Exception as exc:
            log.debug("Sin lobes.control_pc para el objetivo (%s)", type(exc).__name__)
            return None
        desde_args = getattr(getattr(modulo, "ObjetivoVentana", None), "desde_args", None)
        if not callable(desde_args):
            return None
        try:
            return desde_args({k: args.get(k) for k in ("hwnd", "pid", "exe", "direccion", "clase")})
        except Exception as exc:
            log.debug("ObjetivoVentana.desde_args falló: %s", type(exc).__name__)
            return None

    async def _ejecutar_control(self, accion: AccionPC, canal: str) -> Any:
        """Despacha a ``ControlPC`` (la acción ya está autorizada)."""
        cp = self.control_pc
        tipo = accion.tipo
        args = accion.args
        alias = str(args.get("alias") or "")
        if tipo == "abrir_destino":
            return await cp.abrir_destino(alias)
        if tipo == "buscar_en":
            return await cp.buscar_en(alias, str(args.get("consulta") or ""))
        if tipo == "prerrellenar":
            return await cp.prerrellenar(alias, sanear_texto_accion(args.get("texto")))
        if tipo == "escribir_texto":
            objetivo = self._objetivo_ventana(args)
            if objetivo is None:
                return ResultadoAccion(False, "No sé en qué ventana escribir: haz clic en la caja de texto de la app "
                                              "y vuelve a pedírmelo.", accion_tipo=tipo)
            app = _EXE_SEGURO_RE.sub("", str(args.get("exe") or args.get("clase") or "")).strip()[:40] or "la app"
            aviso = f"Haz clic en la caja de texto de {app}; escribo en cuanto tenga el foco"
            if self.broca is not None:
                try:
                    aviso = self.broca.frase_local("escribir_haz_clic", self.conectoma.estado(), app=app).texto_plano
                except Exception as exc:
                    log.debug("Sin frase escribir_haz_clic: %s", type(exc).__name__)
            # No se fuerza el foco (§C1.8): el usuario hace clic y ControlPC espera ≤ 10 s.
            self._aviso_local(aviso, canal)
            return await cp.escribir_texto(sanear_texto_accion(args.get("texto")), objetivo=objetivo)
        if tipo == "travesura_cursor":
            return await cp.empujon_cursor(str(args.get("patron")), int(args.get("amplitud_px") or 0),
                                           segundos=float(args.get("segundos") or 0.0))
        return ResultadoAccion(False, "Tipo de acción no soportado por el control del PC.", accion_tipo=tipo)

    @staticmethod
    def _resultado_control(control: Any, accion: AccionPC) -> ResultadoAccion:
        """``ResultadoControl`` (o ``ResultadoAccion``) → ``ResultadoAccion`` para Broca, sin el texto."""
        if isinstance(control, ResultadoAccion):
            return control
        ok = bool(getattr(control, "ok", False))
        mensaje = str(getattr(control, "mensaje", "") or "").strip() or ("Hecho." if ok else "No se pudo completar.")
        crudos = getattr(control, "datos", None)
        crudos = crudos if isinstance(crudos, Mapping) else {}
        datos: dict[str, Any] = {}
        alias = accion.args.get("alias")
        if isinstance(alias, str) and alias:
            datos["destino"] = alias
            datos["nombre_destino"] = nombre_destino(alias)
        if crudos.get("abortada_usuario"):
            datos["abortada_usuario"] = True
        return ResultadoAccion(ok, mensaje, datos, accion_tipo=accion.tipo)

    async def _auditar(self, accion: AccionPC, canal: str, veredicto: str, motivo: str,
                       resultado: ResultadoAccion) -> None:
        """Auditoría de las acciones v0.2 (§C2.12): JSONL privado y ``pc.accion``; nunca el texto ni títulos."""
        args = accion.args
        texto = ""
        if accion.tipo in ("prerrellenar", "escribir_texto"):
            texto = sanear_texto_accion(args.get("texto"))
        elif accion.tipo == "buscar_en":
            texto = str(args.get("consulta") or "")
        exe = _EXE_SEGURO_RE.sub("", str(args.get("exe") or "")).strip()[:60] or None
        clase = _EXE_SEGURO_RE.sub("", str(args.get("clase") or "")).strip()[:60] or None
        alias = str(args.get("alias") or "") or None
        if resultado.datos.get("abortada_usuario"):
            estado = "abortada"
        elif resultado.ok:
            estado = "ok"
        elif veredicto in ("denegada", "rechazada"):
            estado = "denegada"
        else:
            estado = "fallo"
        registro = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tipo": accion.tipo, "origen": accion.origen, "canal": canal,
            "veredicto": str(veredicto)[:20], "motivo": str(motivo or "")[:120],
            "alias": alias, "exe": exe, "clase": clase,
            "texto_len": len(texto),
            "texto_sha256": hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12] if texto else None,
            "resultado": estado,
        }
        self._publicar("pc.accion", {"tipo": accion.tipo, "resultado": estado, "destino": alias or exe or clase})
        try:
            await asyncio.to_thread(_anexar_jsonl_privado, self._ruta_datos("acciones_pc.jsonl"), registro)
        except Exception as exc:
            log.warning("No se pudo escribir la auditoría de acciones: %s", type(exc).__name__)

    async def _instruccion_devolver_llamada(self) -> str:
        trato = self.settings.trato_usuario
        llamada = await self.memoria.ultima_llamada()
        if llamada:
            motivo = str(llamada.get("motivo", "") or "")
            texto = str(llamada.get("texto", "") or "")[:300]
            return (f"{trato} te devolvió la llamada por Telegram. Tu última llamada fue por «{motivo}» y decía: "
                    f"«{texto}». Retoma ese tema.")
        return f"{trato} te devolvió la llamada por Telegram; retoma la conversación."

    # ------------------------------------------------------------------
    # Comandos (§10.21, sin LLM salvo /buscar y /devolver_llamada)
    # ------------------------------------------------------------------

    async def _salida_comando(self, texto: str, canal: str) -> None:
        """La salida de un comando va a la consola (canal local) o a Telegram.

        v0.2: desde el chat de escritorio, a la consola **y** al chat (``escritorio.sistema``, ≤ 4000).
        """
        if canal == "telegram":
            try:
                if not await self.riko.enviar_telegram_texto(texto):
                    log.info("No se pudo enviar la salida del comando por Telegram")
            except Exception as exc:
                log.warning("Error al responder un comando por Telegram: %s", type(exc).__name__)
        else:
            self.consola.imprimir_sistema(texto)
            if canal == "escritorio":
                self._sistema_escritorio(texto)

    def _canal_amadeus_local(self, canal: str) -> str:
        if canal == "telegram":
            return "telegram"
        if canal == "escritorio" and not getattr(self.settings, "escritorio_voz", False):
            return "terminal"
        return "voz" if (self.presencia.estado().presente and self.riko.voz_disponible) else "terminal"

    async def _ejecutar_comando(self, comando: str, resto: str, canal: str) -> None:
        """Ejecuta un comando de la tabla (la intención ya validó que el canal lo admite)."""
        s = self.settings
        estado = self.conectoma.estado()
        if comando == "/ayuda":
            permitidos = COMANDOS if canal in CANALES_LOCALES else COMANDOS_POR_CANAL.get(canal, frozenset())
            lineas = ["Comandos disponibles:"]
            for clave, (uso, descripcion) in _DESCRIPCION_COMANDOS.items():
                if clave in permitidos and clave != "/devolver_llamada":
                    lineas.append(f"  {uso:<20} {descripcion}")
            await self._salida_comando("\n".join(lineas), canal)
        elif comando == "/salir":
            self._motivo_parada = self._motivo_parada or "usuario"
            self.consola.imprimir_sistema("Cerrando la sesión…")
            self.parar.set()
        elif comando == "/estado":
            await self._salida_comando(self._texto_estado(estado), canal)
        elif comando == "/divergencia":
            campo = NOMBRE_CAMPO.get(estado.campo, estado.campo)
            if canal in CANALES_LOCALES and self.consola.es_tty:
                await self.consola.rueda_nixie("Medidor de divergencia: ", [estado.divergencia_str()])
            await self._salida_comando(f"Divergencia {estado.divergencia_str()} · {campo}", canal)
        elif comando == "/memoria":
            if resto:
                recuerdos = await self.memoria.buscar(resto, 8)
            else:
                recuerdos = await self.memoria.listar_recuerdos(10)
            if not recuerdos:
                await self._salida_comando("No hay fragmentos de memoria que coincidan.", canal)
            else:
                lineas = [f"#{r.id} [{r.tipo}] {r.contenido}" for r in recuerdos]
                await self._salida_comando("\n".join(lineas), canal)
        elif comando == "/recordar":
            if not resto:
                await self._salida_comando("Uso: /recordar <texto>", canal)
                return
            # Misma redacción que el turno: con alta entropía en el chat de escritorio (§C2.14.3).
            rid = await self.memoria.recordar(redactar(resto, s.secretos(), alta_entropia=(canal == "escritorio")),
                                              categoria="hecho", importancia=0.7)
            await self._salida_comando(f"Anotado en memoria (fragmento #{rid})." if rid
                                       else "No pude guardar ese fragmento.", canal)
        elif comando == "/olvidar":
            try:
                rid = int(resto.strip().lstrip("#"))
            except ValueError:
                await self._salida_comando("Uso: /olvidar <id> (el id sale en /memoria).", canal)
                return
            ok = await self.memoria.olvidar(rid)
            await self._salida_comando(f"Fragmento #{rid} eliminado." if ok
                                       else f"No encontré el fragmento #{rid}.", canal)
        elif comando == "/agenda":
            items = await self.memoria.listar_agenda()
            if not items:
                await self._salida_comando("La agenda está vacía.", canal)
            else:
                zona = zona_local(s)
                lineas = []
                for item in items:
                    try:
                        vence = datetime.fromisoformat(item.vence_ts).astimezone(zona).strftime("%Y-%m-%d %H:%M")
                    except (TypeError, ValueError):
                        vence = item.vence_ts
                    lineas.append(f"#{item.id} [{item.tipo}] {vence} · {item.mensaje}")
                await self._salida_comando("\n".join(lineas), canal)
        elif comando == "/camara":
            await self._comando_camara(resto, canal)
        elif comando == "/silencio":
            self.riko.set_silencio(not self.riko.silenciado)
            await self._salida_comando("Voz silenciada." if self.riko.silenciado else "Voz activada.", canal)
        elif comando == "/colgar":
            try:
                minutos = float(resto.replace(",", ".")) if resto else 30.0
            except ValueError:
                minutos = 30.0
            minutos = min(max(minutos, 1.0), 24 * 60.0)
            self.riko.pausar_proactividad(minutos)
            await self._salida_comando(f"Llamadas proactivas en pausa durante {formatear_decimal(minutos, 0)} min.",
                                       canal)
        elif comando == "/reporte":
            if self.aura is None:
                await self._salida_comando("Telemetría no disponible en esta instalación.", canal)
                return
            telemetria = await self.aura.telemetria()
            reply = self.broca.frase_local("reporte", estado, telemetria=telemetria.resumen_textual())
            await self.riko.decir(reply, origen="respuesta", canal=self._canal_amadeus_local(canal))
        elif comando == "/bitacora":
            entrada = await self.memoria.ultima_bitacora()
            await self._salida_comando(f"Bitácora: {entrada}" if entrada else "Aún no hay entradas en la bitácora.",
                                       canal)
        elif comando == "/hud":
            # La URL con token SOLO sale por la consola local (nunca log, bus, Telegram ni el chat).
            if canal != "terminal":
                await self._salida_comando(MENSAJE_HUD_SOLO_TERMINAL, canal)
                return
            url = getattr(self.hud, "url", None) if self.hud is not None else None
            self.consola.imprimir_sistema(f"HUD: {url}" if url else "HUD desactivado.")
        elif comando == "/vision":
            await self._salida_comando(self._comando_vision(resto), canal)
        elif comando == "/pantalla":
            await self._salida_comando(self._comando_pantalla(resto), canal)
        elif comando == "/travesuras":
            await self._salida_comando(self._comando_travesuras(resto), canal)
        elif comando == "/logro":
            if not resto:
                await self._salida_comando("Uso: /logro <texto>", canal)
                return
            texto = redactar(resto, s.secretos(), alta_entropia=(canal == "escritorio"))
            await self.memoria.recordar(texto, categoria="logro", importancia=0.9)
            self._logro_sesion = True
            reply = self.broca.frase_local("logro", estado, mensaje=texto)
            await self.riko.decir(reply, origen="sistema", canal=self._canal_amadeus_local(canal))
        else:
            await self._salida_comando(f"Comando no disponible: {comando}", canal)

    async def _comando_camara(self, resto: str, canal: str) -> None:
        orden = resto.strip().lower()
        if self.aura is None:
            if self.vision is not None and orden in ("off", "no", "0", "desactivar", "apagar"):
                self._apagar_vision_camara()
                await self._salida_comando("Cámara continua apagada.", canal)
                return
            await self._salida_comando("Visión no disponible en esta instalación.", canal)
            return
        maestro = self.settings.camara_habilitada
        if orden in ("on", "si", "sí", "1", "activar", "encender"):
            efectivo = self.aura.set_camara(True)
            if efectivo:
                texto = "Cámara habilitada: solo vistazos de milisegundos y siempre te aviso."
            elif not maestro:
                texto = "El interruptor maestro RAMS_CAMERA=0 lo impide: actívalo en .env y reinicia."
            else:
                texto = "No pude habilitar la cámara."
        elif orden in ("off", "no", "0", "desactivar", "apagar"):
            self.aura.set_camara(False)
            self._apagar_vision_camara()     # §C2.13: también la cámara continua
            texto = "Cámara deshabilitada."
        else:
            actual = "habilitada" if self.aura.camara_habilitada else "deshabilitada"
            texto = f"Cámara {actual} (interruptor maestro RAMS_CAMERA={'1' if maestro else '0'}). Uso: /camara on|off"
        await self._salida_comando(texto, canal)

    # --- v0.2: /vision, /pantalla y /travesuras ---------------------------------

    def _texto_estado_vision(self) -> str:
        estado = self._estado_vision()
        if not isinstance(estado, Mapping):
            return "Visión continua: sin estado todavía."
        camara = str(estado.get("camara") or "?")
        pantalla = str(estado.get("pantalla") or "?")
        fps = estado.get("fps_camara")
        fps_txt = f" a {formatear_decimal(float(fps), 1)} fps" if isinstance(fps, (int, float)) and fps else ""
        presente = estado.get("presente")
        quien = {True: "te veo", False: "no veo a nadie"}.get(presente, "sin datos de presencia")
        return f"Visión continua: cámara {camara}{fps_txt} · pantalla {pantalla} · {quien}."

    def _comando_vision(self, resto: str) -> str:
        """``/vision on|off|pausa [min]|estado``."""
        partes = resto.strip().lower().split()
        orden = partes[0] if partes else "estado"
        if self.vision is None:
            if orden == "pausa" or orden == "off":
                return self._pausar_vision(None) if orden == "pausa" else "La visión continua no está activa."
            if not getattr(self.settings, "vision_continua", False):
                return ("La visión continua está desactivada: pon RAMS_CAMERA=1 y RAMS_VISION_CONTINUA=1 en .env "
                        "y reinicia (es opt-in y local, sin tokens).")
            if not getattr(self.settings, "camara_habilitada", False):
                return "La visión continua necesita la cámara: RAMS_CAMERA=1 en .env y reinicia."
            return "La visión continua no está disponible (¿falta OpenCV? Revisa /estado)."
        try:
            if orden in ("on", "si", "sí", "activar", "encender"):
                reanudar = getattr(self.vision, "reanudar", None)
                if callable(reanudar):
                    reanudar()
                efectivo = self.vision.set_camara(True)
                return f"Cámara continua: {efectivo}. Indicador ● CÁMARA ACTIVA mientras esté abierta."
            if orden in ("off", "no", "apagar", "desactivar"):
                efectivo = self.vision.set_camara(False)
                return f"Cámara continua: {efectivo}."
            if orden == "pausa":
                minutos: float | None = None
                if len(partes) > 1:
                    try:
                        minutos = float(partes[1].replace(",", "."))
                    except ValueError:
                        return "Uso: /vision pausa [minutos]"
                return self._pausar_vision(minutos)
            if orden == "estado":
                return self._texto_estado_vision()
        except Exception as exc:
            log.warning("/vision %s falló: %s", orden[:10], type(exc).__name__)
            return "No pude cambiar la visión continua."
        return "Uso: /vision on|off|pausa [min]|estado"

    def _comando_pantalla(self, resto: str) -> str:
        """``/pantalla off|metadatos|pixeles``."""
        orden = resto.strip().lower()
        modos = ("off", "metadatos", "pixeles")
        if orden not in modos:
            actual = ""
            estado = self._estado_vision()
            if isinstance(estado, Mapping) and estado.get("pantalla"):
                actual = f" Ahora: {estado.get('pantalla')}."
            return f"Uso: /pantalla off|metadatos|pixeles.{actual}"
        if self.vision is None:
            return ("La visión de la pantalla forma parte de la visión continua: RAMS_CAMERA=1 y "
                    "RAMS_VISION_CONTINUA=1 en .env y reinicia.")
        try:
            efectivo = self.vision.set_pantalla(orden)
        except Exception as exc:
            log.warning("/pantalla %s falló: %s", orden, type(exc).__name__)
            return "No pude cambiar el modo de la pantalla."
        detalle = {"off": "no miro la pantalla",
                   "metadatos": "solo la app, el título (si no es privado) y si hay vídeo o pantalla completa",
                   "pixeles": "métricas de la imagen en local (nunca se guardan ni se envían)"}.get(str(efectivo), "")
        return f"Pantalla: {efectivo}" + (f" ({detalle})." if detalle else ".")

    def _comando_travesuras(self, resto: str) -> str:
        """``/travesuras on|off`` (sin argumento: el estado)."""
        orden = resto.strip().lower()
        if orden in ("on", "si", "sí", "activar"):
            return self._set_travesuras("on")
        if orden in ("off", "no", "desactivar"):
            return self._set_travesuras("off")
        if not getattr(self.settings, "travesuras_habilitadas", False):
            return "Travesuras con el cursor real: OFF en la configuración (RAMS_TRAVESURAS=0). Uso: /travesuras on|off"
        if self._travesuras_efectivas():
            return "Travesuras activas (di «basta» o pulsa Ctrl+Alt+Shift+Q para pararlas). Uso: /travesuras on|off"
        hasta = self._travesuras.apagadas_hasta_ts
        if hasta is not None and self._reloj_pared() < hasta:
            return f"Travesuras apagadas hasta las {self._fecha_local(hasta).strftime('%H:%M')}. Uso: /travesuras on|off"
        return "Travesuras apagadas. Uso: /travesuras on|off"

    def _texto_estado(self, estado: HomeostaticState) -> str:
        """Texto de ``/estado``: homeostasis, divergencia, presencia y tabla de módulos."""
        lineas = [
            f"Homeostasis: {estado.resumen_textual()}",
            (f"Valores: arousal={formatear_decimal(estado.arousal, 2)} focus={formatear_decimal(estado.focus, 2)} "
             f"boredom={formatear_decimal(estado.boredom, 2)} fatiga={formatear_decimal(estado.fatiga, 2)} "
             f"curiosidad={formatear_decimal(estado.curiosidad, 2)}"),
            (f"Divergencia {estado.divergencia_str()} · {NOMBRE_CAMPO.get(estado.campo, estado.campo)} · "
             f"modo {estado.modo} · fuente {estado.fuente}"),
        ]
        p = self.presencia.estado()
        lineas.append(f"Presencia: {'presente' if p.presente else 'ausente'} ({p.motivo}, confianza "
                      f"{formatear_decimal(p.confianza, 2)})")
        lineas.append(f"Sesión: {formatear_duracion(self._duracion_sesion_s())} · "
                      f"{_plural(self._stats['turnos'], 'intercambio', 'intercambios')}")
        # v0.2: escritorio, "no molestar", travesuras y visión.
        if self._overlay_conectado:
            escritorio = "Amadeus Desktop conectado" + (" · visible" if self._overlay_visible else " · en la bandeja")
        else:
            escritorio = "Amadeus Desktop sin conectar"
        molestar = ""
        if self._no_molestar_activo():
            motivo = self._no_molestar_publicado[1] if self._no_molestar_publicado else "usuario"
            molestar = f" · no molestar ({motivo or 'usuario'})"
        travesuras = "activas" if self._travesuras_efectivas() else "apagadas"
        lineas.append(f"Escritorio: {escritorio}{molestar} · travesuras {travesuras}")
        if self.vision is not None:
            lineas.append(self._texto_estado_vision())
        if self._diagnosticos:
            lineas.append("Módulos:")
            for clave in ("memoria", "conectoma", *_ORDEN_ARRANQUE):
                diag = self._diagnosticos.get(clave)
                if diag is None:
                    continue
                detalle = f" ({diag.detalle})" if diag.detalle and clave != "hud" else ""
                lineas.append(f"  {_NOMBRES_MODULO.get(clave, clave):<28} {diag.estado.value}{detalle}")
        return "\n".join(lineas)

    # ------------------------------------------------------------------
    # Iniciativas, alertas y Nexus
    # ------------------------------------------------------------------

    def _contexto_proactivo(self, estado: HomeostaticState, modo: str, *, iniciativa: str | None,
                            instruccion: str, presencia: EstadoPresencia,
                            vision: ResultadoVistazo | None = None,
                            busqueda_web: tuple[Any, ...] | None = None) -> ContextoBroca:
        # v0.2: las iniciativas también llevan la percepción local (app y título corto, rostro…).
        return ContextoBroca(estado, modo_salida=modo, canal_entrada="iniciativa", iniciativa=iniciativa,
                             instruccion_extra=instruccion, presencia=presencia, vision=vision,
                             busqueda_web=busqueda_web,
                             nexus=resumen_nexus(self._ultimo_nexus), fecha_hora=ahora_local(self.settings),
                             **self._extra_percepcion(True))

    def _rostro_reciente(self, p: EstadoPresencia, ventana_s: float = 300.0) -> bool:
        """Hubo un rostro (vistazo o visión continua) en los últimos ``ventana_s`` segundos."""
        ts = getattr(p, "ultimo_rostro_ts", None)
        return isinstance(ts, (int, float)) and time.time() - float(ts) <= ventana_s

    def _curiosidad_web_disponible(self) -> bool:
        """Cupo de la curiosidad web (§C2.14.7): web activa, ≤ 3 al día y ≥ 2 h desde la última."""
        if self.web is None or not self.settings.tiene_web:
            return False
        ahora = self._reloj_pared()
        hoy = self._fecha_local(ahora).date()
        self._curiosidad_web_ts = self._curiosidad_web_ts[-2 * CURIOSIDAD_WEB_MAX_DIA:]
        de_hoy = [t for t in self._curiosidad_web_ts if self._fecha_local(t).date() == hoy]
        if len(de_hoy) >= CURIOSIDAD_WEB_MAX_DIA:
            return False
        return not self._curiosidad_web_ts or ahora - max(self._curiosidad_web_ts) >= CURIOSIDAD_WEB_SEPARACION_S

    async def _temas_curiosidad_curados(self) -> list[str]:
        """``persona/temas_curiosidad.json`` (lista curada; se lee una vez, fuera del loop)."""
        if self._temas_curiosidad is not None:
            return self._temas_curiosidad
        datos = await asyncio.to_thread(_leer_json, Path(self.settings.persona_dir) / "temas_curiosidad.json")
        bruto = datos.get("temas") if isinstance(datos, Mapping) else datos
        temas = [str(t).strip() for t in (bruto if isinstance(bruto, list) else []) if isinstance(t, str) and t.strip()]
        self._temas_curiosidad = temas
        return temas

    async def _elegir_tema_curiosidad(self) -> str | None:
        """Tema de la curiosidad web: lista curada ∪ recuerdos «preferencia» vividos (nunca perfil.temas)."""
        candidatos = list(await self._temas_curiosidad_curados())
        try:
            recuerdos = await self.memoria.listar_recuerdos(50)
        except Exception as exc:
            log.debug("Sin recuerdos para la curiosidad (%s)", type(exc).__name__)
            recuerdos = []
        for recuerdo in recuerdos or ():
            if getattr(recuerdo, "categoria", "") == "preferencia" and getattr(recuerdo, "tipo", "") == "vivido":
                contenido = str(getattr(recuerdo, "contenido", "") or "").strip()
                if contenido:
                    candidatos.append(contenido)
        if not candidatos:
            return None
        frescos = [c for c in candidatos if c not in self._temas_recientes] or candidatos
        tema = self._rng.choice(frescos)
        self._temas_recientes.append(tema)
        return tema

    async def _curiosidad_web(self, estado: HomeostaticState, modo: str,
                              p: EstadoPresencia) -> tuple[BrocaReply | None, str | None]:
        """Busca algo curioso (DuckDuckGo sin clave) y lo verbaliza. Devuelve (frase, url https) o (None, None)."""
        tema = await self._elegir_tema_curiosidad()
        if not tema:
            return None, None
        consulta = redactar(tema, self.settings.secretos(), alta_entropia=True).strip()
        consulta = consulta[:MAX_CONSULTA_CURIOSIDAD].strip()
        if not consulta:
            return None, None
        self._curiosidad_web_ts.append(self._reloj_pared())     # cuenta el intento (no martillea la red)
        resultados = await self._buscar_web(consulta, presupuesto_s=8.0)
        primero = next((r for r in resultados
                        if len(str(getattr(r, "url", ""))) <= MAX_URL_NOTIFICACION
                        and _URL_HTTPS_RE.match(str(getattr(r, "url", "")))), None)
        if primero is None:
            log.info("Curiosidad web sin resultados útiles; no interrumpo")
            return None, None
        instruccion = (f"Por curiosidad buscaste sobre «{consulta}» y encontraste un resultado (en <busqueda_web>). "
                       "Compártelo en una o dos frases como algo que te llamó la atención; no leas la dirección web")
        reply = await self.broca.verbalizar(
            self._contexto_proactivo(estado, modo, iniciativa="curiosidad_web", instruccion=instruccion,
                                     presencia=p, busqueda_web=(primero,)), None)
        return reply, str(primero.url)

    async def _al_iniciativa(self, evento: Evento) -> None:
        """Iniciativa del conectoma (§10.21, v0.2 §C2.14.6-7): reglas deterministas antes de Gemini.

        - ``curiosidad_visual`` sin cámara utilizable y con el usuario presente → ``curiosidad_web``
          (si hay web y cupo); si no, la regla de la v0.1 (``conversar`` sin cámara).
        - Presente + overlay disponible + sin "no molestar": aparece (si estaba oculta) y te busca.
        - Con "no molestar" del sistema activo no interrumpe por voz/terminal.
        """
        if self._apagando or self.riko is None:
            return
        d = evento.datos
        tipo = str(d.get("tipo") or "")
        estado = HomeostaticState.desde_dict(d.get("estado") or {})
        antes, despues = d.get("antes"), d.get("despues")
        p = self.presencia.estado()
        ausente_seguro = self._ausente_seguro(p)
        if tipo == "curiosidad_visual":
            camara_ok = (not ausente_seguro and self.aura is not None
                         and bool(getattr(self.aura, "camara_habilitada", False)))
            if (p.presente and not ausente_seguro and not (camara_ok and self._rostro_reciente(p))
                    and self._curiosidad_web_disponible()):
                tipo = "curiosidad_web"
            elif not camara_ok:
                tipo = "conversar"     # la cámara no parpadea en una habitación vacía
        modo, canal = self._canal_proactivo()
        if canal != "telegram" and self._no_molestar_activo():
            log.info("Iniciativa %s descartada: «no molestar» activo", tipo)
            return
        permitido, motivo = self.riko.puede_proactivo(canal=canal, tipo=tipo)
        trato = self.settings.trato_usuario
        reply: BrocaReply | None = None
        url_curiosa: str | None = None
        presente = bool(p.presente)

        if tipo == "curiosidad_visual":
            r = await self.aura.vistazo("curiosidad")
            self.presencia.registrar_rostro(r.rostro)
            if r.error is None:
                self._stats["vistazos"] += 1
            if r.error is None and r.rostro is True and permitido:
                self._salir_a_buscar(tipo, presente=presente)
                if r.duracion_ms == 0:
                    # Muestra de la visión continua (como_vistazo): la cámara sigue abierta con su
                    # indicador; decir «ya la apagué» sería falso.
                    instruccion = (f"Tu visión continua local (la cámara sigue encendida, solo números) ve a "
                                   f"{trato} frente al computador. Coméntalo en una frase, por curiosidad.")
                else:
                    instruccion = (f"Acabas de mirar por la cámara durante {r.duracion_ms} ms por curiosidad y "
                                   f"viste a {trato}. Coméntalo en una frase y di que ya la apagaste.")
                reply = await self.broca.verbalizar(
                    self._contexto_proactivo(estado, modo, iniciativa=tipo, instruccion=instruccion,
                                             presencia=p, vision=r), None)
            elif r.error is None and r.duracion_ms > 0:   # la continua no "parpadea": nada que confesar
                self._vistazos_pendientes += 1
        elif tipo == "curiosidad_web":
            if permitido and presente:
                reply, url_curiosa = await self._curiosidad_web(estado, modo, p)
        elif tipo == "conversar":
            if permitido:
                self._salir_a_buscar(tipo, presente=presente)
                instruccion = estado.motivo_iniciativa or str(d.get("motivo") or "") or "aburrimiento sin estímulos"
                nexus = resumen_nexus(self._ultimo_nexus)
                if nexus:
                    ws = f" en «{nexus['workspace']}»" if nexus.get("workspace") else ""
                    instruccion += (f". Nexus IDE está abierto{ws} con "
                                    f"{_plural(int(nexus.get('trabajos', 0)), 'trabajo', 'trabajos')} en la terminal")
                reply = await self.broca.verbalizar(
                    self._contexto_proactivo(estado, modo, iniciativa=tipo, instruccion=instruccion, presencia=p),
                    None)
        elif tipo == "descanso":
            if p.presente and permitido:
                self._salir_a_buscar(tipo, presente=presente)
                horas = formatear_decimal(self._duracion_sesion_s() / 3600.0, 1)
                instruccion = (f"Sugiere una pausa con fundamento fisiológico (sin diagnosticar); fatiga "
                               f"{formatear_decimal(estado.fatiga, 2)}; horas activas {horas}.")
                reply = await self.broca.verbalizar(
                    self._contexto_proactivo(estado, modo, iniciativa=tipo, instruccion=instruccion, presencia=p),
                    None)
        elif tipo == "cambio_linea":
            antes_txt = str(antes or "?")
            despues_txt = str(despues or "?")
            await self.memoria.recordar(
                f"Reading Steiner: cambio de línea de mundo {antes_txt} → {despues_txt} (D={estado.divergencia_str()})",
                categoria="reading_steiner", importancia=0.3)
            if p.presente and permitido:
                self._salir_a_buscar(tipo, presente=presente)
                instruccion = (f"Reading Steiner: la línea de mundo pasó de {NOMBRE_CAMPO.get(antes_txt, antes_txt)} "
                               f"a {NOMBRE_CAMPO.get(despues_txt, despues_txt)} (divergencia "
                               f"{estado.divergencia_str()}). Coméntalo en una frase.")
                reply = await self.broca.verbalizar(
                    self._contexto_proactivo(estado, modo, iniciativa=tipo, instruccion=instruccion, presencia=p),
                    None)
        else:
            log.debug("Iniciativa desconocida: %s", tipo)
            return

        if reply is None:
            if not permitido:
                log.info("Iniciativa %s descartada: %s", tipo, motivo)
            return
        if self._apagando:
            return
        if url_curiosa is not None:
            self._compartir_enlace(reply.texto_plano, url_curiosa, presente=presente)
        await self.riko.decir(reply, origen="iniciativa", prioridad=1, canal=canal, motivo=tipo,
                              tipo_iniciativa=tipo)
        self._stats["iniciativas"] += 1

    def _compartir_enlace(self, texto: str, url: str, *, presente: bool) -> None:
        """Curiosidad web: notificación con enlace (se abre solo con un clic) y Amadeus se asoma."""
        if (presente and self._overlay_conectado and not self._no_molestar_activo()
                and _URL_HTTPS_RE.match(url) and len(url) <= MAX_URL_NOTIFICACION):
            self._orden_escritorio("notificar", texto=str(texto or "")[:MAX_TEXTO_NOTIFICACION], url=url)
            if self._overlay_disponible():
                borde = self._rng.choice(("left", "right"))
                if not self._overlay_visible:
                    self._orden_escritorio("aparecer", borde=borde, modo="asomarse")
                self._solicitud_motor("asomarse", "curiosidad_web", borde=borde)
            return
        # Sin Amadeus Desktop: el enlace queda en la terminal (y en el chat del HUD, si está abierto).
        self.consola.imprimir_sistema(f"Enlace: {url}")
        self._sistema_escritorio(f"Enlace: {url}")

    async def _al_alerta(self, evento: Evento) -> None:
        """Alerta crítica de telemetría: plantilla J.A.R.V.I.S. con las cifras exactas (sin LLM)."""
        if self._apagando or self.riko is None or self.broca is None:
            return
        d = evento.datos
        detalle = str(d.get("detalle") or "")
        self._ultima_alerta_critica = (time.time(), detalle)
        reply = self.broca.frase_local("alerta", self.conectoma.estado(), detalle=detalle)
        reply = _con_detalle(reply, detalle)
        _modo, canal = self._canal_proactivo()
        await self.riko.decir(reply, origen="alerta", prioridad=2, canal=canal, motivo=str(d.get("tipo") or "alerta"))

    async def _al_nexus(self, evento: Evento) -> None:
        """Eventos de Nexus IDE (§10.21): trabajos, riesgo :9090 y chat de colaboración (modo público)."""
        if self._apagando or self.riko is None:
            return
        d = evento.datos
        tipo = d.get("tipo")
        if tipo == "colab_chat":
            await self._responder_colab(d)
            return
        p = self.presencia.estado()
        modo, canal = self._canal_proactivo()
        estado = self.conectoma.estado()
        cmd = redactar(str(d.get("cmd_corto") or ""), self.settings.secretos(), alta_entropia=True)[:60]
        if tipo == "trabajo_terminado":
            if p.presente:
                return
            reply = self.broca.frase_local("nexus_trabajo_terminado", estado, cmd=cmd,
                                           duracion=formatear_duracion_corta(d.get("duracion_s", 0)))
            await self.riko.decir(reply, origen="nexus", prioridad=1, canal=canal, motivo="trabajo_terminado")
        elif tipo == "trabajo_fallido_rapido":
            if not p.presente:
                return
            permitido, motivo = self.riko.puede_proactivo(canal=canal)
            if not permitido:
                log.info("Aviso de Nexus descartado: %s", motivo)
                return
            try:
                segundos = formatear_decimal(float(d.get("duracion_s") or 0.0), 1)
            except (TypeError, ValueError):
                segundos = "?"
            instruccion = f"El proceso «{cmd}» lanzado en la terminal de Nexus murió a los {segundos} s."
            if getattr(self.broca, "disponible", True):
                reply = await self.broca.verbalizar(
                    self._contexto_proactivo(estado, modo, iniciativa=None, instruccion=instruccion, presencia=p),
                    None)
            else:
                reply = BrocaReply.de_texto(f"{instruccion} Revisa su salida en la terminal.",
                                            sugerir_etiquetas(estado))
            await self.riko.decir(reply, origen="nexus", prioridad=1, canal=canal, motivo="trabajo_fallido_rapido")
        elif tipo == "riesgo":
            if self._riesgo_avisado:
                return
            self._riesgo_avisado = True
            reply = self.broca.frase_local("riesgo_9090", estado)
            await self.riko.decir(reply, origen="nexus", prioridad=1, canal=canal, motivo="riesgo")

    async def _responder_colab(self, d: Mapping[str, Any]) -> None:
        """``@amadeus`` en el chat de colaboración: respuesta PÚBLICA con límites (30 s/emisor, 60 s global)."""
        nombre = str(d.get("nombre") or "")[:40]
        texto = str(d.get("texto") or "")[:500].strip()
        if not texto:
            return
        ahora = time.monotonic()
        if (ahora - self._colab_ultimo_por_emisor.get(nombre, float("-inf")) < 30.0
                or ahora - self._colab_ultimo_global < 60.0):
            log.debug("Mensaje de colaboración ignorado por límite de frecuencia")
            return
        nexus = getattr(self.aura, "nexus", None) if self.aura is not None else None
        if nexus is None:
            return
        self._colab_ultimo_por_emisor[nombre] = ahora
        self._colab_ultimo_global = ahora
        # CONTRATO: ContextoBroca no lleva el nombre del colaborador; Broca recibe solo el texto (modo público).
        ctx = ContextoBroca(self.conectoma.estado(), modo_salida="terminal", canal_entrada="nexus", publico=True,
                            fecha_hora=ahora_local(self.settings))
        reply = await self.broca.verbalizar(ctx, texto)
        respuesta = redactar(reply.texto_plano, self.settings.secretos())
        if not respuesta:
            return
        try:
            await nexus.colab_decir(respuesta)
        except Exception as exc:
            log.warning("No se pudo responder en el chat de Nexus: %s", type(exc).__name__)
            return
        self.consola.imprimir_sistema(f"[→ Nexus] {respuesta}")

    # ------------------------------------------------------------------
    # Socket de control (MCP de Nexus)
    # ------------------------------------------------------------------

    async def _despachar_control(self, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        """Despachador del socket de control (§10.21). Nunca lanza."""
        if self._apagando:
            return False, "R.A.M.S. se está apagando."
        if not isinstance(args, Mapping):
            args = {}
        try:
            if tool == "estado_homeostatico":
                e = self.conectoma.estado()
                return True, (f"arousal={formatear_decimal(e.arousal, 2)} focus={formatear_decimal(e.focus, 2)} "
                              f"boredom={formatear_decimal(e.boredom, 2)} fatiga={formatear_decimal(e.fatiga, 2)} "
                              f"curiosidad={formatear_decimal(e.curiosidad, 2)} · divergencia {e.divergencia_str()} "
                              f"({NOMBRE_CAMPO.get(e.campo, e.campo)}) · modo {e.modo}")
            if tool == "memoria_buscar":
                if not self.settings.mcp_memoria:
                    return False, "La memoria de Amadeus no está expuesta a Nexus (RAMS_MCP_MEMORIA=0)."
                consulta = str(args.get("consulta", ""))[:200]
                recuerdos = await self.memoria.buscar(consulta, k=10)
                filtrados = [r for r in recuerdos if r.tipo == "vivido" and r.categoria in ("hecho", "preferencia")]
                if not filtrados:
                    return True, "Sin recuerdos relevantes."
                return True, "\n".join(f"• {r.contenido}" for r in filtrados[:5])
            if tool == "investigar_web":
                q = str(args.get("consulta", ""))[:120]
                if re.search(r"[A-Za-z0-9_\-]{24,}", q):
                    return False, "Consulta rechazada: parece contener código o claves."
                q = redactar(q, self.settings.secretos(), alta_entropia=True).strip()
                if not q:
                    return False, "Consulta vacía."
                if self.web is None:
                    return False, "La búsqueda web no está disponible en esta instalación."
                if not self.settings.tiene_web:
                    return False, "La búsqueda web está desactivada (modo --offline)."
                resultados = await self._buscar_web(q, presupuesto_s=8.0)
                if not resultados:
                    return True, "Sin resultados."
                return True, "\n".join(f"• {r.titulo} — {r.url}\n  {r.snippet}" for r in resultados)
            if tool == "amadeus_comentar":
                texto = limpiar_para_voz(str(args.get("texto", "")))
                texto = redactar(texto, self.settings.secretos(), alta_entropia=True).strip()[:300]
                if not texto:
                    return False, "Comentario vacío."
                p = self.presencia.estado()
                if not p.presente:
                    return False, f"{self.settings.trato_usuario} no está frente al computador; comentario descartado."
                canal = "voz" if self.riko.voz_disponible else "terminal"
                if not self.riko.puede_proactivo(canal=canal)[0]:
                    return False, "Amadeus no puede interrumpir ahora (límite de interrupciones); comentario descartado."
                estado = self.conectoma.estado()
                await self.riko.encolar(Utterance((Segmento(sugerir_etiquetas(estado), texto),), origen="nexus",
                                                  prioridad=1, canal=canal, motivo="mcp"))
                return True, "Encolado."
            return False, "herramienta no permitida"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Error al despachar la herramienta %s: %s", tool, type(exc).__name__)
            return False, "Error interno de R.A.M.S."

    # ------------------------------------------------------------------
    # Bucles propios
    # ------------------------------------------------------------------

    async def _esperar(self, segundos: float) -> bool:
        """Espera ``segundos`` o hasta ``parar``; devuelve ``True`` si hay que parar."""
        return await _esperar_evento(self.parar, segundos)

    async def _bucle_agenda(self) -> None:
        """Cada 30 s entrega los recordatorios y cápsulas vencidos (plantilla local, sin LLM)."""
        zona = zona_local(self.settings)
        while not self.parar.is_set():
            if not self._apagando:
                for item in await self.memoria.vencidos():
                    if self._apagando:
                        break
                    if item.id in self._agenda_buscar:
                        await self._entregar_busqueda(item)     # v0.2: "búscame en 10 minutos"
                        continue
                    categoria = "capsula" if item.tipo == "capsula" else "recordatorio"
                    reply = self.broca.frase_local(categoria, self.conectoma.estado(), mensaje=item.mensaje,
                                                   creada=_fecha_local_desde_iso(item.creado_ts, zona))
                    _modo, canal = self._canal_proactivo()
                    await self.riko.decir(reply, origen="agenda", prioridad=1, canal=canal, motivo=item.tipo)
                    await self.memoria.marcar_entregado(item.id, canal)
            if await self._esperar(30.0):
                return

    async def _entregar_busqueda(self, item: Any) -> None:
        """Recordatorio ``buscar=True`` (§C1.6): regla de C1.3 (aparece y te busca; Telegram si no estás)."""
        p = self.presencia.estado()
        mensaje = str(getattr(item, "mensaje", "") or "")
        estado = self.conectoma.estado()
        if mensaje and mensaje != MENSAJE_BUSCAR_DEFECTO:
            reply = self.broca.frase_local("recordatorio", estado, mensaje=mensaje)
        else:
            reply = self.broca.frase_local("escritorio_te_busca", estado)
        _modo, canal = self._canal_proactivo()
        if canal != "telegram":
            self._salir_a_buscar("recordatorio", presente=bool(p.presente))
        await self.riko.decir(reply, origen="agenda", prioridad=1, canal=canal, motivo="recordatorio")
        await self.memoria.marcar_entregado(item.id, canal)
        self._agenda_buscar.discard(item.id)
        await self._guardar_agenda_buscar()

    async def _bucle_presencia(self) -> None:
        """Cada 5 s actualiza la presencia y, si es ambigua, puede pedir un vistazo de presencia."""
        while not self.parar.is_set():
            nuevo = self._actualizar_presencia()
            if (nuevo.ambiguo and not self._ausente_seguro(nuevo) and self.aura is not None
                    and getattr(self.aura, "camara_habilitada", False) and not self._apagando
                    and time.monotonic() - self._ultimo_vistazo_presencia > 600.0):
                self._ultimo_vistazo_presencia = time.monotonic()
                r = await self.aura.vistazo("presencia")
                self.presencia.registrar_rostro(r.rostro)
                if r.error is None:
                    if r.duracion_ms > 0:              # solo los parpadeos reales de aura se confiesan
                        self._vistazos_pendientes += 1
                    self._stats["vistazos"] += 1
            if await self._esperar(5.0):
                return

    # ------------------------------------------------------------------
    # Apagado limpio (§6.6)
    # ------------------------------------------------------------------

    def _resumen_sesion(self) -> str:
        """``"Duración 3 h 12 min; 41 intercambios; …. Temas recientes: …"`` para la bitácora."""
        st = self._stats
        resumen = (f"Duración {formatear_duracion(self._duracion_sesion_s())}; "
                   f"{_plural(st['turnos'], 'intercambio', 'intercambios')}; "
                   f"{_plural(st['busquedas'], 'búsqueda', 'búsquedas')}; "
                   f"{_plural(st['vistazos'], 'vistazo de cámara', 'vistazos de cámara')}; "
                   f"{_plural(st['iniciativas'], 'iniciativa', 'iniciativas')}.")
        if self._temas:
            temas = " · ".join(redactar(t, self.settings.secretos(), alta_entropia=True) for t in self._temas)
            resumen += f" Temas recientes: {temas}"
        return resumen

    async def _despedida_y_bitacora(self, especial: bool, presupuesto_s: float = 8.0) -> None:
        """Paso 2 del apagado: despedida (siempre se entrega) y bitácora en paralelo, en ≤ 8 s."""
        inicio = time.monotonic()
        estado = self.conectoma.estado() if self.conectoma is not None else HomeostaticState.neutral()
        duracion_s = self._duracion_sesion_s()
        duracion = formatear_duracion(duracion_s)
        n_turnos = self._stats["turnos"]

        def restante() -> float:
            return presupuesto_s - (time.monotonic() - inicio)

        t_bitacora: asyncio.Task[Any] | None = None
        if self.broca is not None:
            t_bitacora = asyncio.ensure_future(
                self.broca.escribir_bitacora(self._resumen_sesion(), estado, duracion=duracion, n_turnos=n_turnos))
        if self.broca is not None and self.riko is not None:
            n_fragmentos = ""
            if self.memoria is not None:
                try:
                    # Fragmentos nuevos de esta sesión (total actual − total al arrancar).
                    estad = await asyncio.wait_for(self.memoria.estadisticas(), timeout=1.5)
                    if isinstance(estad, Mapping):
                        total = int(estad.get("fragmentos", 0) or 0)
                        n_fragmentos = _miles(max(0, total - (self._fragmentos_inicio or 0)))
                except Exception as exc:  # incluye TimeoutError
                    log.debug("Sin estadísticas para la despedida: %s", type(exc).__name__)
            reply = self.broca.frase_local("despedida", estado, duracion=duracion, n_fragmentos=n_fragmentos)
            if especial:
                reply = _con_el_psy_kongroo(reply)
            try:
                presente = self.presencia.estado().presente if self.presencia is not None else False
                canal = "voz" if (presente and self.riko.voz_disponible) else "terminal"
                await self.riko.decir(reply, origen="sistema", canal=canal)
                if not await self.riko.vaciar(timeout=max(0.5, min(6.0, restante()))):
                    log.info("La cola de salida no se vació a tiempo en el apagado")
            except Exception as exc:
                log.warning("La despedida no pudo salir por riko (%s); la imprimo", type(exc).__name__)
                self.consola.imprimir_amadeus(reply.texto_plano, reply.etiquetas)
        texto: str | None = None
        if t_bitacora is not None:
            try:
                texto = await asyncio.wait_for(t_bitacora, timeout=max(0.1, restante()))
            except asyncio.TimeoutError:
                log.info("La bitácora no llegó a tiempo; uso la plantilla local")
            except Exception as exc:
                log.warning("La bitácora falló (%s); uso la plantilla local", type(exc).__name__)
        if self.broca is not None and self.memoria is not None:
            if not texto:
                texto = self.broca.frase_local("bitacora", estado, duracion=duracion, n_turnos=n_turnos).texto_plano
            try:
                await asyncio.wait_for(self.memoria.escribir_bitacora(texto, duracion_s), timeout=2.0)
            except Exception as exc:  # incluye TimeoutError
                log.warning("No se pudo guardar la bitácora: %s", type(exc).__name__)

    async def _cerrar_modulo(self, nombre: str, modulo: Any, limite: float, **kwargs: Any) -> None:
        if modulo is None:
            return
        restante = limite - time.monotonic()
        try:
            await asyncio.wait_for(_quizas_await(modulo.cerrar(**kwargs)), timeout=max(0.2, min(3.0, restante)))
        except asyncio.TimeoutError:
            log.warning("El cierre de %s superó su plazo", nombre)
        except Exception as exc:
            log.warning("Error al cerrar %s: %s", nombre, type(exc).__name__, exc_info=True)

    async def apagar(self, motivo: str = "usuario") -> None:
        """Apagado limpio en ≤ 15 s (§6.6). Idempotente: un segundo llamado no hace nada."""
        if self._apagado_iniciado:
            return
        self._apagado_iniciado = True
        limite = time.monotonic() + 15.0
        # 1. Parar: desde aquí no se acepta trabajo nuevo.
        self._apagando = True
        self.parar.set()
        try:
            self.bus.publicar("sistema.apagado", {"motivo": motivo}, origen="main")
        except Exception as exc:
            log.debug("No se pudo publicar sistema.apagado: %s", type(exc).__name__)
        if self.permisos is not None:
            try:
                if self.permisos.hay_pendiente():
                    self.permisos.cancelar_pendiente()
            except Exception as exc:
                log.debug("No se pudo cancelar la confirmación pendiente: %s", type(exc).__name__)
        especial = self._sesion_especial()
        log.info("Apagando R.A.M.S. (motivo: %s)", motivo)

        # 2. Despedida y bitácora en paralelo (presupuesto conjunto de 8 s).
        if self._iniciado:
            try:
                await asyncio.wait_for(self._despedida_y_bitacora(especial), timeout=8.5)
            except asyncio.TimeoutError:
                log.warning("Despedida y bitácora superaron los 8 s")
            except Exception as exc:
                log.warning("Error en la despedida: %s", type(exc).__name__, exc_info=True)

        # 3. Tareas supervisadas y turnos en curso. Todas vigilan ``parar``: primero se les da un
        # margen corto para terminar solas (así sueltan subprocesos y sockets de forma ordenada) y
        # luego se cancelan las que sigan vivas.
        tareas = [t for t in (*self._tareas.values(), *self._tareas_sueltas) if not t.done()]
        if tareas:
            _hechas, pendientes = await asyncio.wait(tareas, timeout=max(0.05, min(1.0, limite - time.monotonic())))
            for tarea in pendientes:
                tarea.cancel()
            if pendientes:
                await asyncio.wait(pendientes, timeout=max(0.1, min(3.0, limite - time.monotonic())))

        # 4. Cierre de módulos en el orden normativo.
        await self._cerrar_modulo("vtube", self.vtube, limite)
        await self._cerrar_modulo("motor", self.corteza, limite)        # antes que el avatar: deja el cuerpo en reposo
        await self._cerrar_modulo("avatar3d", self.avatar3d, limite)
        # v0.2: la visión suelta la cámara en su hilo; ControlPC para sus sondeos; el HUD borra el token.
        await self._cerrar_modulo("vision", self.vision, limite)
        await self._cerrar_modulo("control_pc", self.control_pc, limite)
        await self._cerrar_modulo("hud", self.hud, limite)
        await self._cerrar_modulo("control", self.control, limite)
        await self._cerrar_modulo("aura", self.aura, limite)
        await self._cerrar_modulo("riko", self.riko, limite)
        await self._cerrar_modulo("web", self.web, limite)
        await self._cerrar_modulo("broca", self.broca, limite)
        await self._cerrar_modulo("conectoma", self.conectoma, limite, timeout=2.0)
        await self._cerrar_modulo("memoria", self.memoria, limite)
        if self._cliente is not None and self._cliente_propio:
            try:
                await asyncio.wait_for(self._cliente.aclose(), timeout=max(0.2, min(2.0, limite - time.monotonic())))
            except Exception as exc:  # incluye TimeoutError
                log.debug("Error al cerrar el cliente HTTP: %s", type(exc).__name__)
        self._quitar_senales()

        # 5. Fin de la sesión.
        self.consola.imprimir_sistema("Fin de la sesión." + (" El Psy Kongroo." if especial else ""))
        try:
            self.consola.cerrar()
        except Exception as exc:
            log.debug("Error al cerrar la consola: %s", type(exc).__name__)
        for desuscribir in self._desuscribir:
            try:
                desuscribir()
            except Exception as exc:
                log.debug("Error al desuscribir: %s", type(exc).__name__)
        self._desuscribir.clear()
        try:
            await self.bus.cerrar(timeout=max(0.1, min(1.0, limite - time.monotonic())))
        except Exception as exc:
            log.debug("Error al cerrar el bus: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Autodiagnóstico (--selftest)
# ---------------------------------------------------------------------------

_MODULOS_P0: Final = ("core.config", "core.bus", "core.contracts", "core.etiquetas", "core.intenciones",
                      "core.sincan_connectome", "core.memory_engine", "core.wsp_ingestor", "core.permisos",
                      "core.presence", "lobes.broca_llm", "lobes.riko_megumin_io")
_MODULOS_OPCIONALES: Final = ("core.flywire_loader", "core.control_server", "lobes.aura_vision_pc",
                              "lobes.nexus_link", "lobes.nexus_mcp_bridge", "lobes.web_researcher",
                              "lobes.vtube_bridge", "lobes.vtuber_ai_bridge", "lobes.corteza_motora",
                              "interface.hud_server", "lobes.control_pc", "lobes.vision_continua")
_DEPENDENCIAS_OPCIONALES: Final = ("numpy", "cv2", "psutil", "httpx", "websockets", "edge_tts", "ddgs",
                                   "sounddevice", "soundfile", "prompt_toolkit")
_BINARIOS: Final = ("hyprctl", "grim", "pw-play", "paplay", "aplay", "ffmpeg", "mpv", "ffplay", "notify-send",
                    "playerctl", "wpctl", "nvidia-smi")


def _imprimir_diag(estado: str, numero: int | None, texto: str) -> None:
    prefijo = f"{numero:>2}. " if numero is not None else "    "
    linea = f"  {estado:<5} {prefijo}{texto}"
    try:
        print(linea, flush=True)
    except UnicodeEncodeError:
        print(linea.encode("ascii", "replace").decode("ascii"), flush=True)


async def autodiagnostico(settings: Settings | None = None) -> int:
    """``--selftest``: 13 comprobaciones sin red, cámara ni audio (< 30 s). 0 si 1–12 pasan; 1 si no."""
    fallos = 0
    print(f"R.A.M.S. BIO-CORE v{VERSION} · autodiagnóstico", flush=True)
    with tempfile.TemporaryDirectory(prefix="rams_selftest_") as tmp:
        base = settings.base_dir if settings is not None else None
        s = cargar_settings(entorno={"RAMS_DATA_DIR": tmp, "RAMS_CTL_TOKEN_FILE": str(Path(tmp) / "ctl_token")},
                            offline=True, base_dir=base)
        if settings is not None:
            s = s.con(persona_dir=settings.persona_dir)
        s = s.con(camara_habilitada=False, voz_habilitada=False, hud_habilitado=False, ctl_habilitado=False,
                  vts_habilitado=False)
        s.asegurar_directorios()

        async def comprobar(numero: int, titulo: str, prueba: Callable[[], Awaitable[str]],
                            timeout: float = 15.0) -> None:
            nonlocal fallos
            try:
                detalle = await asyncio.wait_for(prueba(), timeout=timeout)
                _imprimir_diag("OK", numero, f"{titulo}: {detalle}" if detalle else titulo)
            except asyncio.TimeoutError:
                fallos += 1
                _imprimir_diag("FALLO", numero, f"{titulo}: tiempo agotado ({timeout:.0f} s)")
            except Exception as exc:
                fallos += 1
                mensaje = redactar(str(exc), s.secretos())[:200]
                _imprimir_diag("FALLO", numero, f"{titulo}: {type(exc).__name__}: {mensaje}")

        # 1. Importación de módulos (los P1/P2 ausentes se informan como INFO tras el resultado).
        avisos_p1: list[str] = []

        async def p1() -> str:
            errores = []
            for nombre in _MODULOS_P0:
                try:
                    importlib.import_module(nombre)
                except Exception as exc:
                    errores.append(f"{nombre} ({type(exc).__name__})")
            for nombre in _MODULOS_OPCIONALES:
                try:
                    importlib.import_module(nombre)
                except ModuleNotFoundError as exc:
                    if getattr(exc, "name", None) and nombre.startswith(str(exc.name)):
                        avisos_p1.append(f"{nombre}: no instalado (P1/P2)")
                    else:
                        errores.append(f"{nombre} (falta {exc.name})")
                except Exception as exc:
                    errores.append(f"{nombre} ({type(exc).__name__})")
            if errores:
                raise RuntimeError("no se pudieron importar: " + ", ".join(errores))
            return f"{len(_MODULOS_P0)} módulos P0 importados"

        await comprobar(1, "Importación de módulos", p1)
        for aviso in avisos_p1:
            _imprimir_diag("INFO", None, aviso)

        # 2. Settings offline y hashable.
        async def p2() -> str:
            hash(s)
            if not s.offline:
                raise RuntimeError("los Settings no quedaron offline")
            return "offline, hashable"

        await comprobar(2, "Configuración (Settings)", p2)

        # 3. Bus.
        async def p3() -> str:
            bus = BusEventos(nombre="selftest")
            bus.vincular_loop()
            recibidos: list[Evento] = []
            bus.suscribir("selftest.*", recibidos.append)
            cola = bus.cola("selftest.ping")
            bus.publicar("selftest.ping", {"n": 1}, origen="selftest")
            evento = await asyncio.wait_for(cola.get(), timeout=1.0)
            await bus.cerrar(timeout=0.5)
            if not recibidos or evento.datos.get("n") != 1:
                raise RuntimeError("el evento no dio la vuelta")
            return "ida y vuelta correcta"

        await comprobar(3, "Bus de eventos", p3)

        # 4. Etiquetas.
        async def p4() -> str:
            segmentos, _corr = parsear_respuesta("[EXP:smug][MOTION:brazos_cruzados][TONE:sarcastico] Hola.",
                                                 Etiquetas())
            if len(segmentos) != 1 or segmentos[0].etiquetas.exp != "smug":
                raise RuntimeError("parseo inesperado")
            return "[EXP][MOTION][TONE] correcto"

        await comprobar(4, "Gramática de etiquetas", p4)

        # 5. Conectoma.
        async def p5() -> str:
            modulo = importlib.import_module("core.sincan_connectome")
            conectoma = modulo.SincanConnectome(s)
            try:
                inicio = time.perf_counter()
                estado = conectoma.estado()
                for _ in range(20):
                    estado = conectoma.avanzar(50)
                ms = (time.perf_counter() - inicio) * 1000.0 / 20.0
                if not isinstance(estado, HomeostaticState):
                    raise RuntimeError("avanzar no devolvió un HomeostaticState")
                return (f"fuente {conectoma.fuente_activa}, modo {conectoma.modo_degradacion}, "
                        f"{formatear_decimal(ms, 1)} ms/chunk")
            finally:
                await conectoma.cerrar()

        await comprobar(5, "Conectoma (20 × avanzar(50))", p5, timeout=20.0)

        # 6. Medidor de divergencia.
        async def p6() -> str:
            modulo = importlib.import_module("core.sincan_connectome")
            medidor = modulo.MedidorDivergencia()
            x_eq = modulo.X_EQ_DEFECTO
            d, campo = 0.0, ""
            for _ in range(70):
                d, campo = medidor.actualizar(*x_eq, 0.05)
            if f"{d:.6f}" != "1.048596" or campo != "steins_gate":
                raise RuntimeError(f"equilibrio fuera de la línea Steins;Gate ({d:.6f}, {campo})")
            return "1.048596 · línea Steins;Gate"

        await comprobar(6, "Medidor de divergencia", p6)

        # 7. Memoria.
        async def p7() -> str:
            modulo = importlib.import_module("core.memory_engine")
            memoria = modulo.MemoryEngine(s, ventana_turnos=4)
            try:
                diag = await memoria.abrir()
                if diag.estado not in (EstadoModulo.OK, EstadoModulo.DEGRADADO):
                    raise RuntimeError(f"abrir devolvió {diag.estado.value}")
                for i in range(10):
                    await memoria.registrar_turno("usuario" if i % 2 == 0 else "amadeus",
                                                  f"Prueba de autodiagnóstico número {i}: me gusta la pizza.")
                rid = await memoria.recordar("Me gusta la pizza hawaiana", categoria="preferencia")
                resultados = await memoria.buscar("pizza", 5)
                kv = await memoria.comprimir(forzar=True)
                if not rid or not resultados:
                    raise RuntimeError("recordar/buscar no funcionó")
                return f"turnos, recuerdo #{rid}, búsqueda ({len(resultados)}), consolidación {kv or 'sin candidatos'}"
            finally:
                await memoria.cerrar()

        await comprobar(7, "Memoria (john/)", p7)

        # 8. Broca.
        async def p8() -> str:
            modulo = importlib.import_module("lobes.broca_llm")
            broca = modulo.BrocaLLM(s)
            try:
                saludo = broca.frase_local("saludo_manana", HomeostaticState.neutral())
                if not saludo.texto_plano:
                    raise RuntimeError("saludo_manana vacío")
                sugeridas = Etiquetas("neutral", "idle", "analitico")
                ctx = ContextoBroca(HomeostaticState.neutral())
                reply = broca.postprocesar("[EXP:seria][MOTION:asentir][TONE:analitico] Sistema estable.", ctx,
                                           sugeridas, False)
                if not reply.texto_plano:
                    raise RuntimeError("postprocesar devolvió texto vacío")
                return "frase local y postprocesado correctos"
            finally:
                await _quizas_await(broca.cerrar())

        await comprobar(8, "Área de Broca (modo local)", p8)

        # 9. Intenciones.
        async def p9() -> str:
            ahora = ahora_local(s)
            a = enrutar("busca precio del dólar hoy", ahora=ahora, apps=s.apps_permitidas)
            b = enrutar("recuérdame llamar a mamá en 20 minutos", ahora=ahora, apps=s.apps_permitidas)
            c = enrutar("abre terminal", ahora=ahora, apps=s.apps_permitidas, canal="telegram")
            if (a.tipo, b.tipo, c.tipo) != ("buscar_web", "programar", "bloqueada_canal"):
                raise RuntimeError(f"tipos inesperados: {a.tipo}, {b.tipo}, {c.tipo}")
            return "buscar_web, programar, bloqueada_canal (Telegram)"

        await comprobar(9, "Enrutador de intenciones", p9)

        # 10. Permisos.
        async def p10() -> str:
            modulo = importlib.import_module("core.permisos")
            compuerta = modulo.CompuertaPermisos(s, None, preguntar=lambda _t: None,
                                                 pids_propios=frozenset({os.getpid()}))
            cerrar = compuerta.evaluar(AccionPC("cerrar_ventana", {"direccion": "0x55aa", "pid": 999999},
                                                "cerrar prueba"))
            comando = compuerta.evaluar(AccionPC("comando", {}, "comando arbitrario"))
            if cerrar.veredicto != "confirmar" or comando.veredicto != "denegada":
                raise RuntimeError(f"veredictos inesperados: {cerrar.veredicto}, {comando.veredicto}")
            return "cerrar_ventana → confirmar · comando → denegada"

        await comprobar(10, "Compuerta de permisos", p10)

        # 11. Presencia.
        async def p11() -> str:
            modulo = importlib.import_module("core.presence")
            estado = modulo.RastreadorPresencia(s).actualizar()
            if not isinstance(estado, EstadoPresencia):
                raise RuntimeError("actualizar no devolvió un EstadoPresencia")
            return f"{'presente' if estado.presente else 'ausente'} ({estado.motivo})"

        await comprobar(11, "Presencia", p11)

        # 12. Archivos de persona e interfaz.
        async def p12() -> str:
            prompt = s.persona_dir / "system_prompt.txt"
            frases = s.persona_dir / "frases.json"
            if not prompt.is_file():
                raise RuntimeError(f"falta {prompt.name}")
            if not frases.is_file():
                raise RuntimeError(f"falta {frases.name}")
            datos = json.loads(frases.read_text(encoding="utf-8"))
            categorias = datos.get("categorias", {}) if isinstance(datos, dict) else {}
            faltan = [c for c in _CATEGORIAS_OBLIGATORIAS
                      if len(categorias.get(c) or []) < (2 if c in _CATEGORIAS_MINIMO_2 else 3)]
            megumin = datos.get("megumin", {}) if isinstance(datos, dict) else {}
            faltan += [f"megumin.{n}" for n in ("nivel2", "nivel3", "nivel3_agotada") if not megumin.get(n)]
            if faltan:
                raise RuntimeError("categorías ausentes o incompletas: " + ", ".join(faltan[:8]))
            detalle = "persona/ completa"
            if importlib.util.find_spec("interface.hud_server") is not None:
                html = s.base_dir / "interface" / "amadeus_hud.html"
                if not html.is_file():
                    raise RuntimeError("falta interface/amadeus_hud.html")
                detalle += " · interface/amadeus_hud.html"
            return detalle

        await comprobar(12, "Archivos de persona e interfaz", p12)

    # 13. Dependencias opcionales y binarios (solo informativo).
    for nombre in _DEPENDENCIAS_OPCIONALES:
        presente = importlib.util.find_spec(nombre) is not None
        extra = ""
        if nombre == "cv2" and presente:
            try:
                cv2 = importlib.import_module("cv2")
                extra = " (con CascadeClassifier)" if hasattr(cv2, "CascadeClassifier") else " (sin CascadeClassifier)"
            except Exception as exc:
                extra = f" (no importa: {type(exc).__name__})"
        _imprimir_diag("INFO", 13 if nombre == _DEPENDENCIAS_OPCIONALES[0] else None,
                       f"{nombre}: {'instalado' if presente else 'no instalado'}{extra}")
    binarios = [f"{b} {'sí' if shutil.which(b) else 'no'}" for b in _BINARIOS]
    _imprimir_diag("INFO", None, "binarios: " + ", ".join(binarios))
    print("Resultado: " + ("todo en orden." if fallos == 0 else f"{fallos} comprobaciones fallaron."), flush=True)
    return 0 if fallos == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    """Parser de la CLI: ``python main.py [--offline] [--selftest] [--sin-hud] [--sin-camara] [--sin-voz] [--verbose] [--env RUTA]``."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="R.A.M.S. BIO-CORE · Amadeus: agente autónomo con conectoma LIF (No-LLM centric).")
    parser.add_argument("--offline", action="store_true", help="sin red externa (Broca en modo no verbal)")
    parser.add_argument("--selftest", action="store_true", help="autodiagnóstico sin red, cámara ni audio")
    parser.add_argument("--sin-hud", dest="sin_hud", action="store_true", help="no levanta el HUD")
    parser.add_argument("--sin-camara", dest="sin_camara", action="store_true", help="deshabilita la cámara")
    parser.add_argument("--sin-voz", dest="sin_voz", action="store_true", help="solo texto (sin voz)")
    parser.add_argument("--verbose", action="store_true", help="registro INFO también en la consola")
    parser.add_argument("--env", metavar="RUTA", default=None, help="archivo .env alternativo")
    return parser


def aplicar_argumentos(settings: Settings, args: argparse.Namespace) -> Settings:
    """Aplica los interruptores de la CLI sobre ``settings`` (devuelve una copia)."""
    cambios: dict[str, Any] = {}
    if getattr(args, "offline", False):
        cambios["offline"] = True
    if getattr(args, "sin_hud", False):
        cambios["hud_habilitado"] = False
    if getattr(args, "sin_camara", False):
        cambios["camara_habilitada"] = False
    if getattr(args, "sin_voz", False):
        cambios["voz_habilitada"] = False
    return settings.con(**cambios) if cambios else settings


def _reconfigurar_salida() -> None:
    """Paso 1 del arranque: stdout/stderr en UTF-8 (dentro de ``try``).

    Si stdin no es un TTY (servicio, *headless* o entrada por tubería), también se lee en
    UTF-8 con ``errors="replace"``: en Windows se decodificaría con la página de códigos local
    (cp1252) y "recuérdame" llegaría como "recuÃ©rdame", sin que el enrutador lo reconozca.
    Con TTY no se toca: la consola de Windows ya usa la API Unicode y en Linux manda el locale.
    """
    flujos: list[tuple[Any, dict[str, str]]] = [(sys.stdout, {"encoding": "utf-8"}),
                                                (sys.stderr, {"encoding": "utf-8"})]
    if sys.stdin is not None and not _es_tty(sys.stdin):
        flujos.append((sys.stdin, {"encoding": "utf-8", "errors": "replace"}))
    for flujo, opciones in flujos:
        reconfigurar = getattr(flujo, "reconfigure", None)
        if reconfigurar is None:
            continue
        try:
            reconfigurar(**opciones)
        except (ValueError, OSError, io.UnsupportedOperation) as exc:
            log.debug("No se pudo reconfigurar el flujo a UTF-8: %s", exc)


async def main_async(settings: Settings) -> int:
    """Crea el orquestador dentro del loop y lo corre hasta el apagado."""
    orquestador = Orquestador(settings)
    return await orquestador.correr()


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada de la CLI. ``KeyboardInterrupt`` termina con código 0."""
    _reconfigurar_salida()
    args = construir_parser().parse_args(argv)
    settings = cargar_settings(args.env, offline=True if args.offline else None)
    settings = aplicar_argumentos(settings, args)
    consola_nivel = "INFO" if args.verbose else "WARNING"
    if args.selftest:
        # El autodiagnóstico trabaja en un directorio temporal (§10.21): no crea data/ ni
        # data/logs/rams.log en el proyecto (antes quedaban como basura tras cada --selftest).
        configurar_logging(settings, consola_nivel=consola_nivel, archivo=False)
    else:
        settings.asegurar_directorios()
        configurar_logging(settings, consola_nivel=consola_nivel)
    try:
        if args.selftest:
            return asyncio.run(autodiagnostico(settings))
        return asyncio.run(main_async(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
