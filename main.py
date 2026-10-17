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
from datetime import datetime, timezone
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
    "voz": "Salida de voz",
    "telegram": "Mensajería (Telegram/RINE)",
    "web": "Investigación web",
    "hud": "HUD",
    "control": "Socket de control (MCP)",
}

# Orden fijo de las líneas de módulos en el arranque (maqueta de §6.4).
_ORDEN_ARRANQUE: Final = ("broca", "camara", "telemetria", "hyprland", "vts", "avatar3d", "voz", "telegram", "web",
                          "nexus", "hud", "control")

# Tarea supervisada → clave de diagnóstico que pasa a DEGRADADO si se desactiva.
_CLAVE_TAREA: Final = {"conectoma": "conectoma", "salida": "voz", "telemetria": "telemetria",
                       "hyprland": "hyprland", "nexus": "nexus", "vts": "vts", "avatar3d": "avatar3d",
                       "telegram_entrada": "telegram", "salieri": "memoria", "agenda": "memoria"}

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
}

_CATEGORIAS_OBLIGATORIAS: Final = (
    "saludo_manana", "saludo_tarde", "saludo_noche", "saludo_madrugada", "regreso", "regreso_con_llamadas",
    "despedida", "apodo_1", "apodo_2", "apodo_3", "apodo_chuunibyou", "cumplido", "agradecimiento",
    "acusacion_tsundere", "tristeza", "broca_offline", "generico", "alerta", "reporte", "recordatorio",
    "capsula", "vistazo_presente", "vistazo_ausente", "vistazos_mientras_ausente", "iniciativa_conversar",
    "iniciativa_curiosidad_visual", "iniciativa_descanso", "iniciativa_cambio_linea", "accion_ok",
    "accion_denegada", "busqueda_resultados", "busqueda_sin_resultados", "confirmacion_requerida",
    "error_propio", "nexus_trabajo_terminado", "riesgo_9090", "logro", "bitacora",
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
        self.aura: Any = None
        self.permisos: Any = None
        self.presencia: Any = None
        self.riko: Any = None
        self.hud: Any = None
        self.control: Any = None
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

        # 1. Bus y manejador de log (con los secretos para el filtro).
        self.bus.vincular_loop(loop)
        instalar_manejador_log(self.bus, secretos=s.secretos())
        self._desuscribir.append(self.bus.suscribir("sistema.modulo", self._ev_modulo))
        if self._cliente is None and httpx is not None:
            self._cliente = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0),
                                              headers={"User-Agent": "RAMS-BioCore/0.1"})
            self._cliente_propio = True

        # 2. HUD antes que nadie: se suscribe en su __init__ y guarda todos los sistema.modulo.
        if s.hud_habilitado:
            self.hud = self._construir_opcional("interface.hud_server", "ServidorHUD", ("hud",),
                                                lambda cls: cls(s, self.bus))
        else:
            self._diag_ausentes["hud"] = DiagnosticoModulo("hud", "HUD", EstadoModulo.OFF, "deshabilitado")
        self.bus.publicar("sistema.arranque", {"version": VERSION, "offline": s.offline,
                                               "inicio_ts": self._inicio_ts}, origen="main")

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

        # 7. Compuerta de permisos (las confirmaciones se imprimen en la consola).
        clase_permisos = _importar_obligatorio("core.permisos", "CompuertaPermisos")
        self.permisos = clase_permisos(s, self.bus, preguntar=self.consola.imprimir_sistema)

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

        # 9. Presencia.
        clase_presencia = _importar_obligatorio("core.presence", "RastreadorPresencia")
        self.presencia = clase_presencia(s)

        # 10. Salida (voz + avatar / Telegram).
        clase_riko = _importar_obligatorio("lobes.riko_megumin_io", "RikoMeguminIO")
        self.riko = clase_riko(s, self.bus, avatar=self.vtube, memoria=self.memoria,
                               presencia=self.presencia.estado, cliente=self._cliente,
                               imprimir=self.consola.imprimir_amadeus)

        # 11. Socket de control para el puente MCP de Nexus.
        self.control = self._construir_opcional("core.control_server", "ServidorControl", ("control",),
                                                lambda cls: cls(s, self._despachar_control))

        self._suscribir_eventos()

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
        for clave, diag in self._diag_ausentes.items():
            por_clave.setdefault(clave, diag)
            tiempos.setdefault(clave, time.monotonic() - self._inicio_mono)
        orden = ["conectoma", *_ORDEN_ARRANQUE]
        diags = [por_clave[c] for c in orden if c in por_clave]
        diags.extend(d for c, d in por_clave.items() if c not in orden)
        return diags, tiempos

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
            "avatar3d": f"Avatar 3D VTuber AI Studio :{puerto_avatar}", "voz": "Salida de voz",
            "telegram": "Mensajería (Telegram/RINE)", "web": "Investigación web", "nexus": "Nexus IDE",
            "hud": "HUD", "control": "Socket de control (MCP)",
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
            ("telegram_entrada", lambda: self.riko.bucle_telegram_entrada(self.parar, self._manejar_telegram),
             s.tiene_telegram and s.telegram_polling),
            ("salieri", lambda: self.memoria.bucle_compresor(self.parar, presente=self._usuario_presente), True),
            ("agenda", self._bucle_agenda, True),
            ("presencia", self._bucle_presencia, True),
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
        d = evento.datos
        self.consola.indicador_camara(bool(d.get("activa")), d.get("duracion_ms"))

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
            elif accion == "camara" and self.aura is not None:
                efectivo = self.aura.set_camara(not self.aura.camara_habilitada)
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
            presente = self.presencia.estado().presente
            if not (presente and self.riko.voz_disponible):
                return None
            reply = self.broca.frase_local("confirmacion_requerida", self.conectoma.estado(),
                                           descripcion=str(evento.datos.get("descripcion", "")))
        except Exception as exc:
            log.debug("Sin confirmación por voz: %s", type(exc).__name__)
            return None
        return self.riko.decir(reply, origen="sistema", canal="voz")

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
        # 0. Confirmación pendiente: la línea es la respuesta (solo desde la terminal local).
        if local and self.permisos is not None and self.permisos.hay_pendiente():
            self.permisos.resolver(texto)
            return None
        s = self.settings
        secretos = s.secretos()
        texto_seguro = redactar(texto, secretos)
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
            self.presencia.registrar_entrada("terminal")
            self._actualizar_presencia(por_entrada_local=True)
            if self.aura is not None:
                try:
                    self.aura.reiniciar_backoff_camara()
                except Exception as exc:
                    log.debug("No se pudo reiniciar el backoff de la cámara: %s", type(exc).__name__)
            await self.memoria.marcar_llamadas_contestadas(desde_ts=ahora_ts - 7200)
        elif canal == "telegram":
            self.presencia.registrar_entrada("telegram")
            await self.memoria.marcar_llamadas_contestadas(desde_ts=ahora_ts - 7200)
        # Los payloads del bus nunca llevan secretos (§5): se publica el texto ya redactado.
        self.bus.publicar("usuario.entrada", {"texto": texto_seguro, "canal": canal}, origen="main")

        # 2. Intención (sin LLM) y comandos.
        ahora = ahora_local(s)
        intencion = enrutar(texto, ahora=ahora, apps=s.apps_permitidas, canal=canal)
        entrada_broca: str | None = texto_seguro
        texto_turno = texto_seguro
        instruccion_extra: str | None = None
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
            consulta = redactar(str(args.get("consulta") or texto), secretos)
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
            contenido = redactar(str(args.get("contenido") or ""), secretos)
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
            if self.aura is None:
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
            accion, error_propio = await self._accion_pc(args.get("accion"))
        elif tipo == "memoria_consulta":
            memoria_relevante = await self.memoria.buscar(str(args.get("consulta") or texto), 8)
        elif tipo == "bloqueada_canal":
            accion = ResultadoAccion(False, MENSAJE_SOLO_LOCAL, accion_tipo=str(args.get("tipo_original", "")))
        if accion is not None and not accion.ok and tipo in ("accion_pc", "recordar", "programar"):
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
        )

        # 6. Verbalización (Broca: Gemini o banco local; nunca decide nada).
        reply = await self.broca.verbalizar(ctx, entrada_broca, imagen_png=png)

        # 7. Turnos en memoria (el del usuario y el de Amadeus).
        canal_salida = CANAL_DE_MODO.get(ctx.modo_salida, "terminal")
        await self.memoria.registrar_turno("usuario", texto_turno, canal=canal)
        await self.memoria.registrar_turno("amadeus", reply.texto_plano, canal=canal_salida,
                                           etiquetas=reply.etiquetas)
        self._stats["turnos"] += 1
        if entrada_broca is not None:
            self._temas.append(texto_seguro[:60])

        # 8. Salida por el canal elegido.
        await self.riko.decir(reply, origen="respuesta", canal=canal_salida)
        # 9.
        return reply

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

    async def _accion_pc(self, accion: Any) -> tuple[ResultadoAccion, bool]:
        """Ejecuta una ``AccionPC`` (fijando antes el objetivo de ``cerrar_ventana``). Devuelve (resultado, error_propio)."""
        if not isinstance(accion, AccionPC):
            return ResultadoAccion(False, "No entendí qué acción ejecutar."), False
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
        """La salida de un comando va a la consola (canal local) o a Telegram."""
        if canal == "telegram":
            try:
                if not await self.riko.enviar_telegram_texto(texto):
                    log.info("No se pudo enviar la salida del comando por Telegram")
            except Exception as exc:
                log.warning("Error al responder un comando por Telegram: %s", type(exc).__name__)
        else:
            self.consola.imprimir_sistema(texto)

    def _canal_amadeus_local(self, canal: str) -> str:
        if canal == "telegram":
            return "telegram"
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
            rid = await self.memoria.recordar(redactar(resto, s.secretos()), categoria="hecho", importancia=0.7)
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
            # La URL con token SOLO sale por la consola local (nunca log, bus ni Telegram).
            url = getattr(self.hud, "url", None) if self.hud is not None else None
            self.consola.imprimir_sistema(f"HUD: {url}" if url else "HUD desactivado.")
        elif comando == "/logro":
            if not resto:
                await self._salida_comando("Uso: /logro <texto>", canal)
                return
            texto = redactar(resto, s.secretos())
            await self.memoria.recordar(texto, categoria="logro", importancia=0.9)
            self._logro_sesion = True
            reply = self.broca.frase_local("logro", estado, mensaje=texto)
            await self.riko.decir(reply, origen="sistema", canal=self._canal_amadeus_local(canal))
        else:
            await self._salida_comando(f"Comando no disponible: {comando}", canal)

    async def _comando_camara(self, resto: str, canal: str) -> None:
        if self.aura is None:
            await self._salida_comando("Visión no disponible en esta instalación.", canal)
            return
        orden = resto.strip().lower()
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
            texto = "Cámara deshabilitada."
        else:
            actual = "habilitada" if self.aura.camara_habilitada else "deshabilitada"
            texto = f"Cámara {actual} (interruptor maestro RAMS_CAMERA={'1' if maestro else '0'}). Uso: /camara on|off"
        await self._salida_comando(texto, canal)

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
                            vision: ResultadoVistazo | None = None) -> ContextoBroca:
        return ContextoBroca(estado, modo_salida=modo, canal_entrada="iniciativa", iniciativa=iniciativa,
                             instruccion_extra=instruccion, presencia=presencia, vision=vision,
                             nexus=resumen_nexus(self._ultimo_nexus), fecha_hora=ahora_local(self.settings))

    async def _al_iniciativa(self, evento: Evento) -> None:
        """Iniciativa del conectoma (§10.21): cuotas antes de Gemini, cámara solo con alguien presente."""
        if self._apagando or self.riko is None:
            return
        d = evento.datos
        tipo = str(d.get("tipo") or "")
        estado = HomeostaticState.desde_dict(d.get("estado") or {})
        antes, despues = d.get("antes"), d.get("despues")
        p = self.presencia.estado()
        ausente_seguro = self._ausente_seguro(p)
        if tipo == "curiosidad_visual" and (ausente_seguro or self.aura is None
                                            or not getattr(self.aura, "camara_habilitada", False)):
            tipo = "conversar"     # la cámara no parpadea en una habitación vacía
        modo, canal = self._canal_proactivo()
        permitido, motivo = self.riko.puede_proactivo(canal=canal, tipo=tipo)
        trato = self.settings.trato_usuario
        reply: BrocaReply | None = None

        if tipo == "curiosidad_visual":
            r = await self.aura.vistazo("curiosidad")
            self.presencia.registrar_rostro(r.rostro)
            if r.error is None:
                self._stats["vistazos"] += 1
            if r.error is None and r.rostro is True and permitido:
                instruccion = (f"Acabas de mirar por la cámara durante {r.duracion_ms} ms por curiosidad y viste a "
                               f"{trato}. Coméntalo en una frase y di que ya la apagaste.")
                reply = await self.broca.verbalizar(
                    self._contexto_proactivo(estado, modo, iniciativa=tipo, instruccion=instruccion,
                                             presencia=p, vision=r), None)
            elif r.error is None:
                self._vistazos_pendientes += 1
        elif tipo == "conversar":
            if permitido:
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
        await self.riko.decir(reply, origen="iniciativa", prioridad=1, canal=canal, motivo=tipo,
                              tipo_iniciativa=tipo)
        self._stats["iniciativas"] += 1

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
                    categoria = "capsula" if item.tipo == "capsula" else "recordatorio"
                    reply = self.broca.frase_local(categoria, self.conectoma.estado(), mensaje=item.mensaje,
                                                   creada=_fecha_local_desde_iso(item.creado_ts, zona))
                    _modo, canal = self._canal_proactivo()
                    await self.riko.decir(reply, origen="agenda", prioridad=1, canal=canal, motivo=item.tipo)
                    await self.memoria.marcar_entregado(item.id, canal)
            if await self._esperar(30.0):
                return

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
        await self._cerrar_modulo("avatar3d", self.avatar3d, limite)
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
                              "lobes.vtube_bridge", "interface.hud_server")
_DEPENDENCIAS_OPCIONALES: Final = ("numpy", "cv2", "psutil", "httpx", "websockets", "edge_tts", "ddgs",
                                   "sounddevice", "prompt_toolkit")
_BINARIOS: Final = ("hyprctl", "grim", "pw-play", "paplay", "aplay", "ffmpeg", "mpv", "notify-send", "playerctl",
                    "wpctl", "nvidia-smi")


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
