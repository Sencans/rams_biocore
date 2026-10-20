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
- Respaldo Puter (§C2.15): si ``puter_habilitado``, hay red (sin ``--offline``) y existe el enlace
  ``/escritorio``, crea ``RespaldoPuter`` y se lo pasa a ``broca.set_respaldo``: cuando Gemini no
  puede, Broca pide el texto a Puter.js dentro de Amadeus Desktop (cuenta de Puter del usuario).
  Diagnóstico ``puter`` en el arranque y en ``/estado``.

v0.2.1 (órdenes naturales, apps del menú Inicio y Telegram multimedia):
- ``lanzar_app`` con ``args.local`` (Windows, ``RAMS_APPS_MENU_INICIO``): main resuelve el nombre
  con ``ControlPC.resolver_app_local`` → compuerta (``nivel_app``) → ``ControlPC.lanzar_app_local``
  (nunca ``aura.ejecutar``, R18 del diseño v0.3); una app confirmada se recuerda
  (``aprobar_app_local``). Si no la encuentra, responde con nombres parecidos del menú Inicio.
- Telegram (``RAMS_TELEGRAM_ACCIONES``): las acciones de bajo riesgo llegan con
  ``args["canal"] = "telegram"``; cada una se anuncia en el PC (consola, chat de escritorio y
  notificación) y queda en la auditoría.
- ``enviar_foto`` (cámara o pantalla → Telegram del usuario; por Telegram exige
  ``RAMS_TELEGRAM_FOTOS``): compuerta → ``lobes.capturas.capturar_jpeg`` →
  ``riko.enviar_foto_telegram`` y aviso «📸 …» en el PC. Nunca se escribe una imagen a disco.
- Audio: ``_manejar_telegram(texto, *, voz=False)``; con Gemini y ``RAMS_TELEGRAM_TRANSCRIBIR``,
  ``riko.set_transcriptor(broca.transcribir_audio)``. La respuesta por Telegram va como nota de
  voz si ``RAMS_TELEGRAM_AUDIO=siempre`` o (``auto`` y el usuario mandó voz o pidió audio).

v0.2.2 «Correo, Drive y taller de scripts» (``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §14):
- Construye siempre ``CuentaGoogle``, ``Correo``, ``Drive`` y ``TallerScripts`` (opcionales: si faltan o
  están apagados, su diagnóstico dice OFF y el motivo). Conectar Google va en segundo plano (el turno
  no espera); la URL de respaldo solo se imprime en la consola.
- Tarjetas aprobables: el borrador, la operación de Drive o el script se muestran con
  ``tarjeta.mostrar`` (contenido completo, solo para ``/escritorio``) y su huella queda registrada en
  la compuerta. La acción (``enviar_correo``, ``drive_*``, ``ejecutar_script``) nace SOLO al pulsar el
  botón (``escritorio.tarjeta``), con ``origen="usuario"`` y la huella mostrada, y pasa una vez por
  la compuerta, que consume la tarjeta. Las decisiones se serializan con ``_lock_tarjetas``.
- Redacción «en la salida»: el cuerpo, el asunto y los destinatarios van crudos al módulo de correo
  (su anti-secretos decide); el tema, los alias y la tarea se redactan con la regla del canal justo
  antes de ir a Gemini. En un turno de correo, Broca y la memoria reciben un resumen sin cuerpo ni
  direcciones. Las listas de Drive y la salida de un script nunca van a Gemini ni a Telegram.
- Oferta del taller: sin intención, con una tarea del PC (``pide_tarea_pc``), ofrece un script; un
  «sí» corto en el turno siguiente (``acepta_oferta``) lo genera.

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
import hmac
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
    DecisionPermiso,
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
    normalizar,
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
try:  # v0.2.1 (este grupo): "¿pidió la respuesta en audio?"
    from core.intenciones import quiere_audio
except ImportError:  # enrutador v0.2
    def quiere_audio(texto: str) -> bool:  # type: ignore[misc]
        return False
try:  # v0.2.2 (este grupo): oferta del taller
    from core.intenciones import acepta_oferta, pide_tarea_pc
except ImportError:  # enrutador v0.2.1: sin oferta del taller
    def pide_tarea_pc(texto: str) -> bool:  # type: ignore[misc]
        return False

    def acepta_oferta(texto: str) -> bool:  # type: ignore[misc]
        return False
try:  # v0.2.2 (grupo base): tarjetas aprobables y taller
    from core.contracts import (
        CORREO_RE,
        DECISIONES_TARJETA,
        HUELLA_RE,
        ID_TARJETA_RE,
        MAX_SALIDA_TARJETA,
        TTL_TARJETA_S,
        TTL_TARJETA_TELEGRAM_S,
        huella_contenido,
        largo_utf16,
    )
except ImportError:  # contratos v0.2.1: los valores de la spec (§3.1)
    CORREO_RE = re.compile(  # type: ignore[misc]
        r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})*\.[A-Za-z]{2,24}$")
    DECISIONES_TARJETA = ("aprobar", "aprobar_publico", "editar", "guardar", "descartar")  # type: ignore[misc]
    HUELLA_RE = re.compile(r"^[0-9a-f]{64}$")  # type: ignore[misc]
    ID_TARJETA_RE = re.compile(r"^[0-9a-f]{8}$")  # type: ignore[misc]
    MAX_SALIDA_TARJETA = 8_192  # type: ignore[misc]
    TTL_TARJETA_S = 1800.0  # type: ignore[misc]
    TTL_TARJETA_TELEGRAM_S = 43200.0  # type: ignore[misc]

    def huella_contenido(contenido: Any) -> str:  # type: ignore[misc]
        return ""                                   # fail-closed: huella inválida

    def largo_utf16(texto: Any) -> int:  # type: ignore[misc]
        return sum(2 if ord(c) > 0xFFFF else 1 for c in texto) if isinstance(texto, str) else 0
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
    "puter": "Respaldo Puter (Amadeus Desktop)",
    "control_pc": "Control del PC",
    "vision_camara": "Visión continua (cámara)",
    "vision_pantalla": "Visión continua (pantalla)",
    # v0.2.2
    "google": "Cuenta de Google",
    "correo": "Correo (Gmail)",
    "drive": "Google Drive",
    "taller": "Taller de scripts",
}

# Orden fijo de las líneas de módulos en el arranque (maqueta de §6.4, ampliada en la v0.2 y la v0.2.2).
_ORDEN_ARRANQUE: Final = ("broca", "camara", "telemetria", "hyprland", "vts", "avatar3d", "motor", "voz", "telegram",
                          "web", "nexus", "hud", "escritorio", "puter", "control_pc", "vision_camara",
                          "vision_pantalla", "google", "correo", "drive", "taller", "control")

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
# --- v0.2.1: Telegram multimedia, órdenes remotas y apps del menú Inicio ----------
PLAZO_VOZ_TELEGRAM_S: Final = 90.0            # síntesis + envío de una nota de voz
PLAZO_FOTO_TELEGRAM_S: Final = 60.0           # captura + envío de una foto
INSTRUCCION_AUDIO: Final = ("El usuario pidió que le respondas con una nota de voz: contesta breve y natural, "
                            "como si le hablaras (sin listas ni enlaces).")
# Motivo de lobes.capturas.ResultadoCaptura → explicación para el usuario (Broca la verbaliza).
MENSAJES_CAPTURA: Final = {
    "deshabilitada": "La cámara está desactivada (RAMS_CAMERA=0), así que no puedo tomar la foto.",
    "sin_camara": "No encontré ninguna cámara conectada al computador.",
    "camara_ocupada": "La cámara está ocupada por otra app ahora mismo.",
    "ventana_sensible": "En la pantalla hay una ventana privada (contraseñas, banco o pagos): no la capturo.",
    "sin_backend": "No tengo con qué capturar en este sistema (faltan mss u OpenCV).",
    "error": "La captura falló.",
    "pausada": "Me pediste que no te mirara: tengo la cámara en pausa.",
    "fotos_desactivadas": ("Las fotos por Telegram están desactivadas: pon RAMS_TELEGRAM_FOTOS=1 en el .env y "
                           "reinicia R.A.M.S."),
    "sin_telegram": "No tengo Telegram configurado para mandártela (TELEGRAM_BOT_TOKEN y CHAT_ID).",
    "envio": "Tomé la imagen, pero Telegram no la aceptó; inténtalo en un momento.",
}
# Leyendas cortas de las fotos (voz de Kurisu; nunca llevan datos de la pantalla).
LEYENDAS_FOTO: Final = {
    "camara": ("Aquí tienes. Y no, no me quedé mirando.", "Foto tomada. Te ves… funcional. Eso es un cumplido.",
               "Ahí está. Si sales mal, culpa a la iluminación, no a mí."),
    "pantalla": ("Tu pantalla, tal cual está ahora.", "Captura lista. No husmeé más de lo necesario.",
                 "Esto es lo que hay en tu pantalla en este momento."),
}
_URL_HTTPS_RE: Final = re.compile(r"^https://[^\s<>\"']+$")
_ID_CONFIRMACION_RE: Final = re.compile(r"^[0-9a-f]{8}$")
_EXE_SEGURO_RE: Final = re.compile(r"[^A-Za-z0-9_.\- ]+")
# --- v0.2.2: Google, correo, Drive y taller (docs/V022_CORREO_DRIVE_SCRIPTS.md §14) -----------
TTL_OFERTA_TALLER_S: Final = 300.0            # «¿lo intento con un script?» vale 5 min
TTL_TARJETA_SALIDA_S: Final = 1800.0          # la tarjeta "salida" de un script caduca a los 30 min
MAX_TITULO_TARJETA: Final = 120
MAX_RESUMEN_TARJETA: Final = 300
MAX_AVISO_TARJETA: Final = 300
MAX_MOTIVO_CIERRE: Final = 200
MAX_TARJETAS_PUBLICADAS: Final = 32
CLASES_APROBABLES: Final = ("correo", "drive", "script")
_TIPO_ACCION_DRIVE: Final = {"subir": "drive_subir", "papelera": "drive_papelera", "compartir": "drive_compartir"}
_NOMBRES_LENGUAJE: Final = {"powershell": "PowerShell", "python": "Python", "bash": "bash"}
# Campos que la auditoría admite de las acciones v0.2.2 (§14.5): nunca contenido.
_CAMPOS_AUDITORIA_V022: Final = frozenset({
    "n_destinatarios", "n_adjuntos", "bytes", "asunto_len", "cuerpo_len", "cuerpo_sha256", "gmail_id", "codigo",
    "op", "archivo_sha12", "rol", "publico", "lenguaje", "nivel", "huella12", "codigo_salida", "tiempo_agotado",
    "dur_s", "bytes_salida", "servicios"})
_CODIGOS_SCRIPT_CORRIDO: Final = frozenset({"ejecutado", "error_salida", "tiempo_agotado", "politica"})
MENSAJE_ABRE_CHAT: Final = "Abre el chat de Amadeus Desktop o el del HUD para aprobarlo."

# --- Perfil heredado de Google Takeout (core/takeout_ingestor.py) ----------------------------------
TTL_OLVIDO_TAKEOUT_S: Final = 60.0            # «¿olvido tu perfil de Google?» espera el «sí» 60 s
COMANDO_OLVIDAR_TAKEOUT: Final = "/olvidar_takeout"
# Comandos que atiende main antes del enrutador (no están en intenciones.COMANDOS): solo canales locales.
_COMANDOS_MAIN: Final = frozenset({COMANDO_OLVIDAR_TAKEOUT})
# Sobre el texto normalizado (sin tildes ni ¿¡, sin puntuación final): «olvida mi perfil de Google»,
# «bórrame el perfil de Google Takeout», «quita mi takeout», «/olvidar_takeout».
_RE_OLVIDAR_TAKEOUT: Final = re.compile(
    r"^(?:(?:oye|amadeus|kurisu|por favor|porfa)[ ,]+)*"
    r"(?:olvida|olvidate de|borra|borrame|elimina|eliminame|quita|quitame) (?:mi|el|tu) "
    r"(?:perfil (?:heredado )?(?:de |del )?(?:google(?: takeout)?|takeout)|takeout)(?: heredado)?"
    r"(?:,? por favor)?$")
_RE_CONFIRMA_OLVIDO_TAKEOUT: Final = re.compile(r"^/olvidar_takeout (?:confirmar|si|s)$")
# El «sí» que confirma (mismas palabras que la compuerta, permisos.RESPUESTAS_SI, normalizadas).
_SI_OLVIDO_TAKEOUT: Final = frozenset({"s", "si", "y", "yes", "procede", "dale"})
_NO_OLVIDO_TAKEOUT: Final = frozenset({"n", "no", "cancela", "cancelar", "mejor no", "no gracias"})

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
    "/google": ("/google [estado|conectar|desconectar]", "cuenta de Google (Gmail y Drive)"),
    "/script": ("/script <tarea>", "taller: escribe un script para revisar y aprobar"),
    "/habilidades": ("/habilidades", "scripts guardados como habilidades"),
    COMANDO_OLVIDAR_TAKEOUT: (COMANDO_OLVIDAR_TAKEOUT, "olvida tu perfil heredado de Google Takeout (pide confirmar)"),
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
    # v0.2.2
    "taller_ofrece", "tarjeta_lista", "google_navegador",
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


def _recortar_utf16(texto: str, maximo: int, marca: str = "…") -> tuple[str, bool]:
    """``texto`` con como mucho ``maximo`` unidades UTF-16 (como ``String.length``), conservando el principio
    y terminando en ``marca`` si recortó. ``(texto, recortado)``. Nunca parte un carácter fuera del BMP."""
    texto = str(texto or "")
    if largo_utf16(texto) <= maximo:
        return texto, False
    limite = max(0, maximo - largo_utf16(marca))
    unidades = 0
    fin = 0
    for indice, caracter in enumerate(texto):
        paso = 2 if ord(caracter) > 0xFFFF else 1
        if unidades + paso > limite:
            break
        unidades += paso
        fin = indice + 1
    return texto[:fin] + marca, True


def _corto(texto: Any, maximo: int) -> str:
    """Texto informativo de una tarjeta en una línea y con como mucho ``maximo`` unidades UTF-16."""
    limpio = " ".join(str(texto or "").split())
    return _recortar_utf16(limpio, maximo)[0]


def _textos(valor: Any, maximo: int = 10) -> list[str]:
    """Lista de textos no vacíos de un argumento de intención (los demás se descartan)."""
    if not isinstance(valor, (list, tuple)):
        return []
    return [v for v in valor if isinstance(v, str) and v.strip()][:maximo]


def _conteo(valor: Any, maximo: int = 999) -> int:
    """Entero ≥ 0 de un argumento de intención (``0`` si no lo es; nunca un ``bool``)."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        return 0
    return max(0, min(valor, maximo))


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
        self.puter: Any = None       # v0.2: RespaldoPuter (IA de respaldo de Broca vía Amadeus Desktop)
        self.google: Any = None      # v0.2.2: lobes/google_cuenta.py (OAuth de Gmail y Drive)
        self.correo: Any = None      # v0.2.2: lobes/correo.py (borradores y envío con tarjeta)
        self.drive: Any = None       # v0.2.2: lobes/drive.py
        self.taller: Any = None      # v0.2.2: lobes/taller_scripts.py
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
        # --- v0.2.2 (docs/V022_CORREO_DRIVE_SCRIPTS.md §14) ---
        self._clientes_escritorio = 0                    # escritorio.conectado.clientes (Desktop, RINE, móvil)
        self._oferta_taller: dict[str, Any] | None = None   # {"tarea", "canal", "ts"}
        # Perfil heredado de Google Takeout: fecha del perfil cargado y el «¿lo olvido?» pendiente.
        self._takeout_generado: str | None = None
        self._takeout_sin_usar: str | None = None           # motivo si el perfil existe pero no se usa
        self._olvido_takeout: dict[str, Any] | None = None  # {"canal", "ts"}
        self._lock_tarjetas = asyncio.Lock()             # serializa las decisiones de las tarjetas
        self._tarjetas_info: dict[str, dict[str, Any]] = {}       # tarjetas "salida": id → {"datos", "caduca"}
        self._tarjetas_publicadas: dict[str, dict[str, Any]] = {}  # id → último tarjeta.mostrar (sin logs)
        # Anti doble clic entre reaperturas (§14.4): cada botón recibido lleva un número creciente; al reabrir
        # una tarjeta tras ejecutarla o fallar (misma id y misma huella) se anota el último número recibido.
        # Un «aprobar» con número ≤ esa marca se pulsó sobre la versión anterior: nunca ejecuta otra vez.
        self._n_boton_tarjeta = 0
        self._reabierta_tras_boton: dict[str, int] = {}
        self._estado_google: dict[str, Any] | None = None

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

    def _configurar_respaldo_puter(self) -> None:
        """Respaldo Puter de Broca (§C2.15): ``RespaldoPuter`` sobre ``/escritorio`` → ``broca.set_respaldo``.

        Solo si ``puter_habilitado`` (``RAMS_PUTER``, defecto sí), con tope diario > 0
        (``PUTER_MAX_CALLS_DAY``), sin ``--offline`` (Puter es un servicio en línea) y con el enlace
        ``/escritorio`` del HUD. Si no, diagnóstico ``OFF`` con el motivo. Nunca lanza: sin respaldo,
        Broca sigue con Gemini y el banco local.
        """
        s = self.settings
        nombre = _NOMBRES_MODULO["puter"]

        def ausente(estado: EstadoModulo, detalle: str) -> None:
            self._diag_ausentes["puter"] = DiagnosticoModulo("puter", nombre, estado, detalle)

        if not getattr(s, "puter_habilitado", True):
            ausente(EstadoModulo.OFF, "deshabilitado (RAMS_PUTER=0)")
            return
        tope = getattr(s, "puter_max_llamadas_dia", None)
        if isinstance(tope, int) and not isinstance(tope, bool) and tope <= 0:
            ausente(EstadoModulo.OFF, "tope diario 0 (PUTER_MAX_CALLS_DAY=0)")
            return
        if s.offline:
            ausente(EstadoModulo.OFF, "modo offline")
            return
        enlace = getattr(self.hud, "escritorio", None) if self.hud is not None else None
        if enlace is None:
            detalle = ("sin /escritorio (RAMS_ESCRITORIO=0)" if not getattr(s, "tiene_escritorio", False)
                       else "sin /escritorio")
            ausente(EstadoModulo.OFF, detalle)
            return
        set_respaldo = getattr(self.broca, "set_respaldo", None)
        if not callable(set_respaldo):
            log.warning("El Área de Broca no admite un proveedor de respaldo: Puter no se usará")
            ausente(EstadoModulo.OFF, "Broca sin respaldo")
            return
        modelo = getattr(s, "puter_modelo", "")
        try:
            clase = getattr(importlib.import_module("interface.escritorio_link"), "RespaldoPuter")
            respaldo = clase(enlace, modelo=modelo if isinstance(modelo, str) else "")
        except Exception as exc:
            detalle = _detalle_import("interface.escritorio_link", exc)
            log.warning("No se pudo crear el respaldo Puter (%s); sigo sin él", detalle)
            ausente(EstadoModulo.DEGRADADO, detalle)
            return
        try:
            set_respaldo(respaldo)
        except Exception as exc:
            log.warning("broca.set_respaldo falló (%s): Puter no se usará", type(exc).__name__)
            respaldo.desvincular()
            ausente(EstadoModulo.DEGRADADO, "Broca rechazó el respaldo")
            return
        self.puter = respaldo
        primero = bool(getattr(s, "puter_primero", False))
        log.info("Respaldo Puter preparado (%s; %s)", "antes que Gemini" if primero else "después de Gemini",
                 respaldo.modelo or "modelo por defecto de Amadeus Desktop")

    def _diagnostico_puter(self) -> DiagnosticoModulo:
        """``puter.diagnostico()`` o un ``DEGRADADO`` si falla (nunca lanza)."""
        try:
            diag = self.puter.diagnostico()
        except Exception as exc:
            log.warning("Diagnóstico del respaldo Puter falló: %s", type(exc).__name__)
            diag = None
        if isinstance(diag, DiagnosticoModulo):
            return diag
        return DiagnosticoModulo("puter", _NOMBRES_MODULO["puter"], EstadoModulo.DEGRADADO, "diagnóstico falló")

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

    # --- v0.2.1: compuerta con apps locales, transcriptor de Telegram ---------

    def _nivel_app_local(self, clave: str) -> tuple[str, str] | None:
        """``ControlPC.nivel_app_local`` para la compuerta (``None`` si no hay índice o falla)."""
        fuente = getattr(self.control_pc, "nivel_app_local", None) if self.control_pc is not None else None
        if not callable(fuente):
            return None
        try:
            return fuente(clave)
        except Exception as exc:
            log.debug("nivel_app_local falló (%s)", type(exc).__name__)
            return None

    def _crear_compuerta(self, clase: Any) -> Any:
        """``CompuertaPermisos`` con la consola, los pids de Amadeus Desktop, las travesuras y (v0.2.1)
        el índice de apps del menú Inicio. Tolera una compuerta que aún no acepte ``nivel_app``."""
        base: dict[str, Any] = {"preguntar": self.consola.imprimir_sistema, "pids_extra": self._pids_extra,
                                "travesuras_activas": self._travesuras_efectivas}
        try:
            return clase(self.settings, self.bus, nivel_app=self._nivel_app_local, **base)
        except TypeError:
            log.info("La compuerta no admite apps del menú Inicio (nivel_app); sigo sin ellas")
            return clase(self.settings, self.bus, **base)

    def _conectar_transcriptor(self) -> None:
        """``riko.set_transcriptor(broca.transcribir_audio)`` si ``RAMS_TELEGRAM_TRANSCRIBIR`` y hay Gemini."""
        s = self.settings
        if self.riko is None or self.broca is None:
            return
        fijar = getattr(self.riko, "set_transcriptor", None)
        if not callable(fijar):
            log.info("La mensajería no admite notas de voz entrantes (set_transcriptor); sigo sin ellas")
            return
        transcribir = getattr(self.broca, "transcribir_audio", None)
        activo = (bool(getattr(s, "telegram_transcribir", True)) and bool(getattr(s, "tiene_gemini", False))
                  and callable(transcribir))
        try:
            fijar(transcribir if activo else None)
        except Exception as exc:
            log.warning("No se pudo conectar el transcriptor de Telegram: %s", type(exc).__name__)
            return
        if activo:
            log.info("Notas de voz de Telegram: se transcriben con Gemini")

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
        # 3b. v0.2: respaldo Puter de Broca sobre /escritorio (necesita el HUD ya construido).
        self._configurar_respaldo_puter()

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

        # 5. Perfil heredado de WhatsApp y, si existe, el de Google Takeout.
        await self._cargar_perfil()
        await self._cargar_perfil_takeout()

        # 6. Conectoma (__init__ barato; iniciar() va en paralelo con los diagnósticos).
        clase_conectoma = _importar_obligatorio("core.sincan_connectome", "SincanConnectome")
        self.conectoma = clase_conectoma(s)

        # 7. Compuerta de permisos (las confirmaciones se imprimen en la consola y, en la v0.2,
        # también llegan al chat de escritorio como botones). pids_extra: ventanas de Amadeus
        # Desktop; travesuras_activas: estado efectivo de main (§C2.14.4).
        clase_permisos = _importar_obligatorio("core.permisos", "CompuertaPermisos")
        self.permisos = self._crear_compuerta(clase_permisos)

        # 8. Lóbulos periféricos (P1/P2, opcionales).
        self.aura = self._construir_opcional(
            "lobes.aura_vision_pc", "AuraVisionPC", ("camara", "telemetria", "hyprland", "nexus"),
            lambda cls: cls(s, self.bus, permisos=self.permisos))
        self.vtube = self._construir_opcional("lobes.vtube_bridge", "VTubeBridge", ("vts",),
                                              lambda cls: cls(s, self.bus))
        self.web = self._construir_opcional("lobes.web_researcher", "WebResearcher", ("web",),
                                            lambda cls: cls(s, cliente=self._cliente))
        # v0.2.2: cuenta de Google, correo, Drive y taller. Se construyen SIEMPRE (aunque estén apagados:
        # su diagnóstico dice OFF y el motivo). El taller recibe a Broca como generador si hay Gemini.
        self.google = self._construir_opcional("lobes.google_cuenta", "CuentaGoogle", ("google",),
                                               lambda cls: cls(s, self.bus, cliente=self._cliente))
        self.correo = self._construir_opcional("lobes.correo", "Correo", ("correo",),
                                               lambda cls: cls(s, self.google, self.bus))
        self.drive = self._construir_opcional("lobes.drive", "Drive", ("drive",),
                                              lambda cls: cls(s, self.google, self.bus))
        self.taller = self._construir_opcional("lobes.taller_scripts", "TallerScripts", ("taller",),
                                               lambda cls: cls(s, self.bus, generador=self._generador_script()))
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
        # 10b. v0.2.1: notas de voz de Telegram → texto (Gemini, modelo ligero).
        self._conectar_transcriptor()

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
        # v0.2.2: Broca conoce desde el arranque qué herramientas de Google y del taller hay.
        self._actualizar_herramientas()
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

    async def _cargar_perfil_takeout(self) -> None:
        """Paso 5b: perfil de Google Takeout → recuerdo heredado (idempotente) y su resumen a Broca.

        Solo con ``perfil_takeout_en_prompt`` y un perfil vigente (v2+). Si no, el perfil **no se
        usa**: ni resumen en el prompt ni recuerdos heredados (que viajarían igual al LLM como
        recuerdos relevantes de cada turno); los que se hubieran importado antes se borran."""
        ruta = getattr(self.settings, "perfil_takeout_path", None)
        if not isinstance(ruta, Path):
            return
        try:
            existe = ruta.is_file()
        except OSError as exc:
            log.debug("No se pudo comprobar el perfil de Takeout (%s)", type(exc).__name__)
            existe = False
        if not existe:
            return
        try:
            takeout = importlib.import_module("core.takeout_ingestor")
            perfil = takeout.cargar_perfil_takeout(ruta)
        except Exception as exc:
            log.warning("No se pudo cargar el perfil de Google Takeout: %s", type(exc).__name__)
            return
        if not perfil:
            return
        generado = perfil.get("generado")
        self._takeout_generado = generado[:10] if isinstance(generado, str) else "sin fecha"
        vigente = bool(takeout.perfil_vigente(perfil))
        activo = bool(getattr(self.settings, "perfil_takeout_en_prompt", True))
        self._takeout_sin_usar = (None if vigente and activo
                                  else "RAMS_PERFIL_TAKEOUT_EN_PROMPT=0" if vigente
                                  else "versión antigua: regenéralo")
        try:
            if self._takeout_sin_usar is None:
                insertados = await self.memoria.importar_perfil_takeout(perfil)
                if insertados:
                    log.info("Perfil de Google Takeout importado: %d hechos", insertados)
                self.broca.set_perfil_takeout(takeout.resumen_perfil_takeout(perfil))
                return
            if not vigente:
                log.warning("El perfil de Google Takeout es de una versión anterior, con filtros de privacidad "
                            "más débiles: no se usa. Regenéralo con python -m core.takeout_ingestor")
            else:
                log.info("RAMS_PERFIL_TAKEOUT_EN_PROMPT=0: el perfil de Google Takeout no se usa (ni resumen ni "
                         "recuerdos heredados)")
            await self.memoria.olvidar_perfil_takeout()      # los recuerdos importados antes también viajarían
            self.broca.set_perfil_takeout(None)
        except Exception as exc:
            log.warning("No se pudo integrar el perfil de Google Takeout: %s", type(exc).__name__)

    # --- perfil de Google Takeout: «olvida mi perfil de Google» (solo local, con confirmación) ------

    @staticmethod
    def _norm_olvido(texto: str) -> str:
        return re.sub(r"[\s.!?…]+$", "", normalizar(texto)).strip()

    async def _turno_olvido_takeout(self, texto: str, canal: str) -> bool:
        """``True`` si el turno era el pedido de olvidar el perfil de Takeout o su confirmación.

        - «olvida mi perfil de Google» o ``/olvidar_takeout`` en un canal local: pregunta y deja
          pendiente la confirmación (60 s, mismo canal). Por Telegram: solo desde el computador.
        - Con la pregunta vigente, en el mismo canal, ``/olvidar_takeout confirmar`` (o, solo en la
          terminal, un «sí» exacto: el chat de escritorio nunca confirma con un «sí» escrito,
          COMPANION §C2.4) borra el archivo, los recuerdos heredados y el resumen del prompt; un
          «no» lo cancela y cualquier otro mensaje la anula (y sigue su curso normal).
        - No es una ``AccionPC`` ni pasa por la compuerta: no toca el PC, solo datos propios de
          Amadeus (como ``/olvidar <id>``), y el perfil se puede regenerar desde los zips.
        """
        if canal not in CANALES_LOCALES and canal != "telegram":
            return False
        norm = self._norm_olvido(texto)
        pendiente, self._olvido_takeout = self._olvido_takeout, None
        if pendiente is not None and canal in CANALES_LOCALES and pendiente.get("canal") == canal:
            try:
                vigente = self._reloj() - float(pendiente.get("ts", float("-inf"))) <= TTL_OLVIDO_TAKEOUT_S
            except (TypeError, ValueError):
                vigente = False
            confirma = (_RE_CONFIRMA_OLVIDO_TAKEOUT.match(norm) is not None
                        or (canal == "terminal" and norm in _SI_OLVIDO_TAKEOUT))
            if vigente and confirma:
                await self._salida_comando(await self._olvidar_takeout(), canal)
                return True
            if vigente and norm in _NO_OLVIDO_TAKEOUT:
                await self._salida_comando("Vale: conservo tu perfil de Google.", canal)
                return True
            log.debug("Confirmación de olvidar el perfil de Takeout anulada (vigente=%s)", vigente)
        es_comando = norm == COMANDO_OLVIDAR_TAKEOUT or norm.startswith(COMANDO_OLVIDAR_TAKEOUT + " ")
        if not (es_comando or _RE_OLVIDAR_TAKEOUT.match(norm)):
            return False
        if canal not in CANALES_LOCALES:
            await self._salida_comando(MENSAJE_SOLO_LOCAL, canal)
            return True
        ruta = getattr(self.settings, "perfil_takeout_path", None)
        try:
            hay_archivo = isinstance(ruta, Path) and ruta.is_file()
        except OSError:
            hay_archivo = False
        if not hay_archivo and self._takeout_generado is None and getattr(self.broca, "perfil_takeout", None) is None:
            await self._salida_comando("No tengo ningún perfil de Google Takeout que olvidar.", canal)
            return True
        self._olvido_takeout = {"canal": canal, "ts": self._reloj()}
        nombre = ruta.name if isinstance(ruta, Path) else "perfil_takeout.json"
        como = ("Responde «sí»" if canal == "terminal"
                else f"Escribe «{COMANDO_OLVIDAR_TAKEOUT} confirmar» (aquí un «sí» escrito no confirma)")
        await self._salida_comando(
            f"¿Olvido tu perfil heredado de Google? Borraré {nombre}, sus recuerdos heredados y su resumen "
            f"del prompt. {como} en los próximos {int(TTL_OLVIDO_TAKEOUT_S)} s para confirmarlo "
            "(cualquier otra cosa lo cancela).", canal)
        return True

    async def _olvidar_takeout(self) -> str:
        """Borra el perfil de Takeout (archivo, recuerdos heredados y resumen de Broca). Nunca lanza."""
        ruta = getattr(self.settings, "perfil_takeout_path", None)
        archivo_ok = True
        if isinstance(ruta, Path):
            try:
                ruta.unlink(missing_ok=True)
            except OSError as exc:
                archivo_ok = False
                log.warning("No se pudo borrar el perfil de Takeout (%s)", type(exc).__name__)
        borrados = 0
        try:
            borrados = int(await self.memoria.olvidar_perfil_takeout() or 0)
        except Exception as exc:
            log.warning("No se pudieron olvidar los recuerdos del perfil de Takeout (%s)", type(exc).__name__)
        try:
            self.broca.set_perfil_takeout(None)
        except Exception as exc:
            log.warning("No se pudo quitar el perfil de Takeout del prompt (%s)", type(exc).__name__)
        self._takeout_generado = None
        self._takeout_sin_usar = None
        if not archivo_ok:
            return (f"Olvidé {borrados} recuerdos heredados de tu perfil de Google y lo quité del prompt, pero no "
                    "pude borrar el archivo: bórralo tú (data/john/perfil_takeout.json).")
        # Las exclusiones (--quitar) viven aparte y se conservan: si regenera el perfil, los temas que
        # pidió quitar no vuelven. Se le dice dónde están por si tampoco las quiere.
        exclusiones = ""
        if isinstance(ruta, Path):
            try:
                archivo_exc = importlib.import_module("core.takeout_ingestor").ruta_exclusiones(ruta)
                if archivo_exc.is_file():
                    exclusiones = (f" Conservo la lista de temas que pediste quitar ({archivo_exc.name}, ofuscada) "
                                   "para que no vuelvan si lo regeneras; bórrala tú si tampoco la quieres.")
            except Exception as exc:
                log.debug("No se pudo comprobar las exclusiones del perfil de Takeout (%s)", type(exc).__name__)
        return (f"Listo: olvidé tu perfil de Google Takeout ({borrados} recuerdos heredados) y ya no está en "
                "el prompt. Si quieres volver a dármelo, ejecuta python -m core.takeout_ingestor." + exclusiones)

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
        # v0.2.2: la cuenta de Google primero (carga el token, sin red) y después correo y Drive, que
        # dependen de ella; el taller va aparte. Cada uno con wait_for(…, 4).
        claves_google = tuple(c for c, m in (("google", self.google), ("correo", self.correo),
                                             ("drive", self.drive)) if m is not None)
        if claves_google:
            trabajos.append((claves_google, self._iniciar_google, 12.5))
        if self.taller is not None:
            trabajos.append((("taller",), self.taller.iniciar, 4.0))
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
        # v0.2: respaldo Puter (después del HUD: depende de /escritorio y de la app conectada).
        if self.puter is not None:
            por_clave["puter"] = self._diagnostico_puter()
            tiempos["puter"] = time.monotonic() - self._inicio_mono
        for clave, diag in self._diag_ausentes.items():
            por_clave.setdefault(clave, diag)
            tiempos.setdefault(clave, time.monotonic() - self._inicio_mono)
        orden = ["conectoma", *_ORDEN_ARRANQUE]
        diags = [por_clave[c] for c in orden if c in por_clave]
        diags.extend(d for c, d in por_clave.items() if c not in orden)
        return diags, tiempos

    async def _iniciar_google(self) -> list[DiagnosticoModulo]:
        """``iniciar()`` de la cuenta de Google y, después, de correo y Drive (v0.2.2), cada uno ≤ 4 s."""
        diags: list[DiagnosticoModulo] = []
        for clave, modulo in (("google", self.google), ("correo", self.correo), ("drive", self.drive)):
            if modulo is None:
                continue
            nombre = _NOMBRES_MODULO[clave]
            try:
                diag = await asyncio.wait_for(_quizas_await(modulo.iniciar()), timeout=4.0)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                diag = DiagnosticoModulo(clave, nombre, EstadoModulo.SIN_CONEXION, "sin respuesta en 4 s")
            except Exception as exc:
                log.warning("iniciar() de %s falló: %s", clave, type(exc).__name__)
                diag = DiagnosticoModulo(clave, nombre, EstadoModulo.DEGRADADO,
                                         f"diagnóstico falló: {type(exc).__name__}")
            if isinstance(diag, DiagnosticoModulo):
                diags.append(diag)
        return diags

    def _generador_script(self) -> Callable[..., Awaitable[Any]] | None:
        """``broca.generar_script`` si Broca lo ofrece y hay clave de Gemini; si no, ``None`` (§14.1)."""
        generar = getattr(self.broca, "generar_script", None) if self.broca is not None else None
        if callable(generar) and bool(getattr(self.settings, "tiene_gemini", False)):
            return generar
        return None

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
        if self._takeout_generado:
            nombre_takeout = getattr(self.settings.perfil_takeout_path, "name", "perfil_takeout.json")
            sin_usar = f" · sin usar ({self._takeout_sin_usar})" if self._takeout_sin_usar else ""
            c.imprimir(f"         perfil de Google heredado: {nombre_takeout} ({self._takeout_generado}){sin_usar}",
                       "gris")
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
            "escritorio": "Amadeus Desktop (/escritorio)", "puter": "Respaldo Puter (Amadeus Desktop)",
            "control_pc": "Control del PC",
            "vision_camara": "Visión continua (cámara)", "vision_pantalla": "Visión continua (pantalla)",
            "google": "Cuenta de Google", "correo": "Correo (Gmail)", "drive": "Google Drive",
            "taller": "Taller de scripts",
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
            # v0.2.2 (§11): botones de las tarjetas (privado, solo main) y estado de la cuenta de Google.
            sub("escritorio.tarjeta", self._ev_escritorio_tarjeta),
            sub("google.estado", self._ev_google_estado),
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
        """Electron conectado/desconectado. v0.2.2: ``clientes`` solo sirve para avisar de que, sin ningún
        cliente de ``/escritorio``, una tarjeta no se puede aprobar («abre el chat…»)."""
        self._overlay_conectado = bool(evento.datos.get("overlay"))
        clientes = evento.datos.get("clientes")
        if isinstance(clientes, int) and not isinstance(clientes, bool) and clientes >= 0:
            self._clientes_escritorio = clientes
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
        self._purgar_tarjetas()          # v0.2.2: las tarjetas caducadas se cierran

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
        # v0.2.1: "háblame" / "respóndeme por audio" en local pide la voz (también desde el chat).
        if intencion.tipo == "pedir_audio" and intencion.args.get("destino") != "telegram" \
                and self.riko.voz_disponible:
            return "voz"
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

    async def _manejar_telegram(self, texto: str, *, voz: bool = False) -> None:
        """Mensaje de Telegram (ya filtrado por riko): turno serializado en segundo plano.

        ``voz`` (v0.2.1): el texto viene de una nota de voz transcrita; con
        ``RAMS_TELEGRAM_AUDIO=auto`` la respuesta vuelve también como nota de voz.
        """
        if self._apagando:
            return
        self._crear_tarea(self._turno_serializado(texto, "telegram", voz=bool(voz)), "turno_telegram")

    def _crear_tarea(self, corrutina: Awaitable[Any], nombre: str) -> asyncio.Task[Any]:
        tarea = asyncio.ensure_future(corrutina)
        self._tareas_sueltas.add(tarea)
        tarea.add_done_callback(self._tareas_sueltas.discard)
        return tarea

    async def _turno_serializado(self, texto: str, canal: str, *, voz: bool = False) -> None:
        """Toma ``_lock_turno`` y procesa el turno; un error nunca tumba nada (frase ``error_propio``)."""
        async with self._lock_turno:
            try:
                if voz:
                    await self.procesar_entrada(texto, canal, voz=True)
                else:
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

    async def procesar_entrada(self, texto: str, canal: str = "terminal", *, voz: bool = False) -> BrocaReply | None:
        """Procesa una entrada del usuario y devuelve la respuesta (``None`` para comandos y confirmaciones).

        ``voz`` (v0.2.1): el texto es la transcripción de una nota de voz de Telegram.
        """
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

        # 1b. Perfil de Google Takeout: «olvida mi perfil de Google» / /olvidar_takeout (solo local) y su
        # «sí» (mismo canal, ≤ 60 s). Lo atiende main sin Broca ni memoria de turnos.
        if await self._turno_olvido_takeout(texto, canal):
            return None

        # 2. Intención (sin LLM) y comandos. v0.2: sinónimos de RAMS_DESTINOS y lanzar_app
        # (RAMS_APPS) solo en Linux. v0.2.1: apps del menú Inicio en Windows y, por Telegram, las
        # acciones de bajo riesgo si RAMS_TELEGRAM_ACCIONES=1.
        ahora = ahora_local(s)
        intencion = self._enrutar(texto, ahora, canal)
        entrada_broca: str | None = texto_seguro
        texto_turno = texto_seguro
        instruccion_extra: str | None = None
        frase_fija: tuple[str, dict[str, Any]] | None = None   # v0.2: respuesta local sin Broca
        desde_comando = False                                   # v0.2.2: /google, /script, /habilidades
        if intencion.tipo == "comando":
            comando = str(intencion.args.get("comando", ""))
            resto = str(intencion.args.get("resto", ""))
            if comando == "/buscar":
                if not resto:
                    await self._salida_comando("Uso: /buscar <consulta>", canal)
                    return None
                intencion = Intencion("buscar_web", {"consulta": resto},
                                      explicacion_tecnica=intencion.explicacion_tecnica)
            elif comando in ("/google", "/script", "/habilidades"):
                convertida = await self._intencion_de_comando_v022(comando, resto, canal)
                if convertida is None:
                    return None
                intencion, desde_comando = convertida, True
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
        # 2b'. v0.2.2: oferta del taller del turno anterior. Un «sí» corto (mismo canal local, ≤ 5 min)
        # la acepta; cualquier otro mensaje la anula.
        if entrada_broca is not None and not desde_comando:
            intencion = self._resolver_oferta_taller(texto, intencion, canal)
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
        elif tipo == "accion_pc" and not await self._es_orden_de_app(args):
            # v0.2.1: "pon <algo>" que no es una app única del menú Inicio: es conversación.
            intencion = Intencion("ninguna", {}, explicacion_tecnica=intencion.explicacion_tecnica)
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
        # v0.2.1: fotos a Telegram y respuestas en audio.
        elif tipo == "enviar_foto":
            accion = await self._enviar_foto(str(args.get("fuente") or ""), canal)
        elif tipo == "pedir_audio":
            instruccion_extra = (f"{instruccion_extra} {INSTRUCCION_AUDIO}" if instruccion_extra
                                 else INSTRUCCION_AUDIO)
        # v0.2.2 (§14.2): cuenta de Google, correo, Drive y taller. Sus fallos no son errores propios
        # (no inyectan «error» en el conectoma), salvo una excepción interna.
        elif tipo in ("google", "correo", "drive", "taller"):
            accion, resumen_turno, error_propio = await self._turno_v022(tipo, args, canal, alta, texto_seguro)
            if resumen_turno is not None:
                # Turno de correo: Broca y la memoria reciben un resumen sin cuerpo ni direcciones.
                entrada_broca = resumen_turno
                texto_turno = resumen_turno
            if desde_comando:
                if accion is not None and accion.accion_tipo not in ("google_estado", "taller_habilidades"):
                    await self._salida_comando(accion.mensaje, canal)
                return None
        elif (tipo == "ninguna" and accion is None and entrada_broca is not None
              and self._ofrecer_taller(texto, texto_seguro, canal)):
            # v0.2.2: sin herramienta para una tarea del PC, Amadeus ofrece escribir un script.
            accion = ResultadoAccion(True, "No tengo herramienta para eso; ofrécele escribir un script y que lo "
                                           "apruebe en la tarjeta", accion_tipo="taller_oferta")
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
            # v0.2.2: texto_turno (= texto_seguro, salvo el resumen sin contenido de un turno de correo).
            self._temas.append(texto_turno[:60])
        # v0.2 (§C2.14.8): el último borrador es una respuesta de Broca (redactada por el LLM) a
        # un turno LOCAL de conversación; nunca de Nexus, iniciativas, Telegram, una acción ni la
        # web: un turno con resultados de búsqueda, vistazo o captura (contenido de terceros que
        # puede traer una inyección de prompt) no deja borrador publicable (§C2.12 "Origen").
        if (local and frase_fija is None and entrada_broca is not None and reply.fuente != "local"
                and intencion.tipo in ("ninguna", "memoria_consulta") and busqueda_web is None
                and ctx.vision is None and png is None and reply.texto_plano
                and (accion is None or accion.accion_tipo != "taller_oferta")):
            self._ultimo_borrador = (reply.texto_plano[:MAX_BORRADOR], self._reloj())
        elif local and reply.fuente != "local" and (busqueda_web is not None or ctx.vision is not None
                                                    or png is not None):
            # "Ponlo en X" justo después se referiría a ESTA respuesta, que no es publicable: se
            # olvida el borrador anterior para no publicar por sorpresa un texto más viejo.
            self._ultimo_borrador = None

        # 8. Salida por el canal elegido. v0.2.1: por Telegram puede ir como nota de voz (y "mándame
        # un audio al telegram" desde el PC también); si la nota falla, va el texto de siempre.
        if self._responder_con_nota_de_voz(canal, intencion, texto, voz):
            if await self._enviar_nota_de_voz(reply):
                if canal != "telegram":
                    await self.riko.decir(reply, origen="respuesta", canal=canal_salida)
                return reply
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

    # --- v0.2.1: enrutado, apps del menú Inicio, fotos y notas de voz ------------

    def _apps_locales_activas(self) -> bool:
        """Apps del menú Inicio por nombre: Windows y ``RAMS_APPS_MENU_INICIO=1`` (por defecto)."""
        return sys.platform == "win32" and bool(getattr(self.settings, "apps_menu_inicio", True))

    def _enrutar(self, texto: str, ahora: datetime, canal: str) -> Intencion:
        """``enrutar`` con los ajustes de la v0.2.1 (tolera un enrutador v0.2 sin ellos)."""
        s = self.settings
        base: dict[str, Any] = {"ahora": ahora, "apps": s.apps_permitidas, "canal": canal,
                                "destinos": getattr(s, "destinos_permitidos", None)}
        local = canal in CANALES_LOCALES
        try:
            return enrutar(texto, plataforma=sys.platform if (local or canal == "telegram") else None,
                           apps_locales=self._apps_locales_activas(),
                           telegram_acciones=bool(getattr(s, "telegram_acciones", False)), **base)
        except TypeError:
            return enrutar(texto, plataforma=sys.platform if local else None, **base)

    async def _es_orden_de_app(self, args: Mapping[str, Any]) -> bool:
        """¿Es de verdad una orden de abrir una app del menú Inicio? ``False`` = conversación.

        - "pon <algo>" (``explicito=False``): solo si es UNA app del menú Inicio.
        - ``tentativa`` ("lanza una moneda", "arranca el carro", "abre un archivo"): salvo que
          nada del menú Inicio se le parezca (``ninguna`` sin sugerencias).
        - "abre <nombre>": siempre (si no existe, main dice "No encuentro…" con sugerencias).
        """
        accion = args.get("accion")
        if not (isinstance(accion, AccionPC) and accion.tipo == "lanzar_app" and accion.args.get("local") is True):
            return True
        explicito = accion.args.get("explicito", True) is not False
        tentativa = accion.args.get("tentativa") is True
        if explicito and not tentativa:
            return True
        resolver = getattr(self.control_pc, "resolver_app_local", None) if self.control_pc is not None else None
        if not callable(resolver):
            return explicito
        try:
            resolucion = await resolver(str(accion.args.get("nombre") or ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("resolver_app_local falló (%s)", type(exc).__name__)
            return explicito
        estado = getattr(resolucion, "estado", "")
        if not explicito:
            return estado == "unica"
        return not (estado == "ninguna" and not tuple(getattr(resolucion, "opciones", ()) or ()))

    def _anunciar_en_pc(self, texto: str, canal: str) -> None:
        """Aviso visible en el PC: consola siempre; chat de escritorio si no vino de la terminal; y,
        si lo pidieron por Telegram, también una notificación de Amadeus Desktop."""
        limpio = str(texto or "").strip()[:MAX_TEXTO_NOTIFICACION]
        if not limpio:
            return
        self.consola.imprimir_sistema(limpio)
        if canal != "terminal":
            self._sistema_escritorio(limpio)
        if canal == "telegram" and self._overlay_conectado and not self._no_molestar_activo():
            self._orden_escritorio("notificar", texto=limpio)

    def _anunciar_accion_remota(self, accion: AccionPC, resultado: ResultadoAccion) -> None:
        """Toda acción pedida por Telegram se anuncia en el PC (§ v0.2.1): quien esté delante lo sabe."""
        if resultado.ok:
            texto = f"📱 Desde Telegram: {resultado.mensaje}"
        else:
            que = " ".join(str(accion.descripcion or accion.tipo).split())[:60]
            texto = f"📱 Desde Telegram pidieron «{que}»: {resultado.mensaje}"
        self._anunciar_en_pc(texto, "telegram")

    @staticmethod
    def _accion_por_telegram(accion: AccionPC) -> AccionPC:
        """La acción con ``args["canal"] = "telegram"`` (la compuerta aplica las reglas de Telegram)."""
        if accion.args.get("canal") == "telegram":
            return accion
        return AccionPC(accion.tipo, {**accion.args, "canal": "telegram"}, accion.descripcion, origen=accion.origen)

    def _media_en_control_pc(self) -> bool:
        """En Windows la música se controla con las teclas multimedia de ControlPC (aura usa playerctl)."""
        return sys.platform == "win32" and callable(getattr(self.control_pc, "media", None))

    @staticmethod
    def _mensaje_app_no_unica(nombre: str, estado: str, opciones: Sequence[str], motivo: str) -> str:
        """Respuesta útil cuando "abre X" no da UNA app del menú Inicio (nunca abre nada)."""
        cita = f"«{nombre}»" if nombre else "esa app"
        nombres = [f"«{o}»" for o in opciones if isinstance(o, str) and o][:3]
        lista = (", ".join(nombres[:-1]) + f" o {nombres[-1]}") if len(nombres) > 1 else "".join(nombres)
        if estado == "desactivada":
            if "RAMS_APPS_MENU_INICIO" in motivo:
                return "Abrir apps del menú Inicio está desactivado (RAMS_APPS_MENU_INICIO=0)."
            return "Abrir apps por nombre solo funciona en Windows; aquí uso las de RAMS_APPS."
        if estado == "excluida":
            return (f"Lo que encuentro como {cita} en el menú Inicio {motivo or 'no es una app'}: "
                    "eso no lo abro.")
        # "abre steam y discord": una orden abre una sola app (si "X y Y" no es el nombre de una).
        partes = [p.strip() for p in re.split(r"\s+(?:y|e)\s+", nombre) if p.strip()] if nombre else []
        if estado == "ninguna" and len(partes) > 1:
            pasos = " y luego ".join(f"«abre {p}»" for p in partes[:3])
            return f"Las apps las abro de una en una: dime {pasos}."
        if estado == "varias" and lista:
            return f"Hay varias apps parecidas a {cita} en el menú Inicio: {lista}. Dime cuál con «abre <nombre>»."
        if lista:
            return f"No encuentro {cita} en el menú Inicio. ¿Quisiste decir {lista}? Pídemelo con «abre <nombre>»."
        return (f"No encuentro {cita} en el menú Inicio de Windows. Dime el nombre tal como aparece allí "
                "(por ejemplo «abre Steam»).")

    async def _accion_app_local(self, accion: AccionPC, canal: str) -> tuple[ResultadoAccion, bool]:
        """``lanzar_app`` local (R18): resolver en el menú Inicio → compuerta (una vez) → ControlPC.

        Nunca va a ``aura.ejecutar``. Si la compuerta pidió confirmación y el usuario aceptó, la app
        queda aprobada (``data/apps_aprobadas.json``) y la próxima vez no se pregunta.
        """
        tipo = "lanzar_app"
        cp = self.control_pc
        resolver = getattr(cp, "resolver_app_local", None) if cp is not None else None
        lanzar = getattr(cp, "lanzar_app_local", None) if cp is not None else None
        remota = canal == "telegram"

        async def terminar(resultado: ResultadoAccion, veredicto: str, motivo: str,
                           auditada: AccionPC = accion) -> tuple[ResultadoAccion, bool]:
            await self._auditar(auditada, canal, veredicto, motivo, resultado)
            if remota:
                self._anunciar_accion_remota(auditada, resultado)
            return resultado, False

        if not callable(resolver) or not callable(lanzar):
            return await terminar(ResultadoAccion(False, "Abrir apps del menú Inicio no está disponible en esta "
                                                         "instalación.", accion_tipo=tipo),
                                  "no_disponible", "sin índice de apps")
        nombre = str(accion.args.get("nombre") or accion.args.get("alias") or "").strip()[:60]
        nombre_seguro = redactar(nombre, self.settings.secretos(), alta_entropia=True)
        try:
            resolucion = await resolver(nombre)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No se pudo buscar la app en el menú Inicio: %s", type(exc).__name__)
            return await terminar(ResultadoAccion(False, "No pude buscar la app por un error interno.",
                                                  accion_tipo=tipo), "fallo", "error al resolver")
        estado = str(getattr(resolucion, "estado", "ninguna") or "ninguna")
        app = getattr(resolucion, "app", None)
        clave = getattr(app, "clave", None)
        nombre_app = str(getattr(app, "nombre", "") or "")[:80]
        if estado != "unica" or not isinstance(clave, str) or not clave:
            opciones = [str(o)[:80] for o in (getattr(resolucion, "opciones", ()) or ())][:3]
            mensaje = self._mensaje_app_no_unica(nombre_seguro, estado, opciones,
                                                 str(getattr(resolucion, "motivo", "") or ""))
            return await terminar(ResultadoAccion(False, mensaje, {"nombre": nombre_seguro, "opciones": opciones},
                                                  accion_tipo=tipo), "sin_app", estado)
        final = AccionPC(tipo, {"alias": clave, "local": True, "nombre": nombre_app or clave, "canal": canal},
                         f"abrir {nombre_app or clave}", origen="usuario")
        if self.permisos is None:
            return await terminar(ResultadoAccion(False, "Acción no autorizada: compuerta de permisos no disponible.",
                                                  accion_tipo=tipo), "denegada", "sin compuerta", final)
        try:
            decision = await self.permisos.autorizar(final)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La compuerta falló con una app local: %s", type(exc).__name__)
            return await terminar(ResultadoAccion(False, "Acción no autorizada: la compuerta falló.", accion_tipo=tipo),
                                  "denegada", "compuerta rota", final)
        if not decision.aprobada:
            veredicto = decision.veredicto if decision.veredicto != "confirmar" else "rechazada"
            return await terminar(ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}",
                                                  {"nombre_destino": nombre_app}, accion_tipo=tipo),
                                  veredicto, decision.motivo, final)
        recordada = False
        if decision.veredicto == "confirmar":
            aprobar = getattr(cp, "aprobar_app_local", None)
            if callable(aprobar):
                try:
                    recordada = bool(await aprobar(clave))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("No se pudo recordar la aprobación de la app: %s", type(exc).__name__)
        try:
            control = await lanzar(clave)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ControlPC falló al abrir una app local: %s", type(exc).__name__, exc_info=True)
            resultado = ResultadoAccion(False, "La acción falló por un error interno.", accion_tipo=tipo)
            await self._auditar(final, canal, decision.veredicto, decision.motivo, resultado)
            return resultado, True
        resultado = self._resultado_control(control, final)
        if resultado.ok and recordada:
            resultado = dataclasses.replace(resultado, mensaje=f"{resultado.mensaje.rstrip()} La próxima vez la abro "
                                                                "sin preguntarte.")
        return await terminar(resultado, decision.veredicto, decision.motivo, final)

    def _titulo_ventana_activa(self) -> str | None:
        """Título de la ventana activa (ControlPC; si no, la última muestra de la pantalla). Nunca se registra."""
        fuente = getattr(self.control_pc, "titulo_activa", None) if self.control_pc is not None else None
        if callable(fuente):
            try:
                titulo = fuente()
            except Exception as exc:
                log.debug("titulo_activa falló (%s)", type(exc).__name__)
                titulo = None
            if isinstance(titulo, str):
                return titulo
        muestra = self._pantalla_fresca()
        if muestra is not None and isinstance(muestra.get("titulo"), str):
            return str(muestra["titulo"])
        return None

    def _pausa_de_camara_del_usuario(self) -> bool:
        """``True`` si el usuario pidió que no lo mirara ("deja de mirarme", ``vision.pausar(…, "usuario")``).

        Usa ``VisionContinua.pausada_por_usuario()``: ``estado().camara == "pausada"`` también lo da
        la autopausa. Solo sin ese método (visión antigua o solo el bus) se recurre al estado.
        """
        vision = self.vision
        pausada_por_usuario = getattr(vision, "pausada_por_usuario", None) if vision is not None else None
        if callable(pausada_por_usuario):
            try:
                return bool(pausada_por_usuario())
            except Exception as exc:
                log.debug("pausada_por_usuario falló (%s)", type(exc).__name__)
                return True              # ante la duda, se respeta la privacidad
        return (self._estado_vision() or {}).get("camara") == "pausada"

    def _camara_bloqueada(self) -> str | None:
        """``"pausada"`` ("no me mires"), ``"deshabilitada"`` (configuración o botón del HUD) o ``None``.

        Una foto pedida por Telegram nunca se salta la privacidad que el usuario fijó en el PC. La
        autopausa de la visión (usuario ausente, sesión bloqueada, juego, batería, llamada) no es
        una orden suya: no bloquea aquí (``capturas`` abre la cámara un instante o responde
        ``camara_ocupada`` si la tiene una llamada).
        """
        if self._pausa_de_camara_del_usuario():
            return "pausada"
        if not getattr(self.settings, "camara_habilitada", False):
            return "deshabilitada"
        if self.aura is not None and getattr(self.aura, "camara_habilitada", True) is False:
            return "deshabilitada"
        return None

    async def _capturar(self, fuente: str, titulo: str | None) -> tuple[bool, bytes | None, str]:
        """``lobes.capturas.capturar_jpeg`` → ``(ok, jpeg, motivo)``. Nunca escribe a disco ni lanza.

        Pasa el ``bus``: si la foto abre la cámara un instante, ``capturas`` publica
        ``vision.camara{activa, fuente:"vistazo"}`` mientras está abierta (LED del HUD, consola y
        ojo del overlay, §C2.13), igual que los vistazos.
        """
        try:
            capturar = getattr(importlib.import_module("lobes.capturas"), "capturar_jpeg")
        except Exception as exc:
            log.info("Capturas no disponibles (%s)", _detalle_import("lobes.capturas", exc))
            return False, None, "sin_backend"
        try:
            resultado = await asyncio.wait_for(
                capturar(fuente, settings=self.settings, vision=self.vision, ventana_titulo=titulo, bus=self.bus),
                PLAZO_FOTO_TELEGRAM_S)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("La captura (%s) superó su plazo", fuente)
            return False, None, "error"
        except Exception as exc:
            log.warning("La captura (%s) falló: %s", fuente, type(exc).__name__)
            return False, None, "error"
        jpeg = getattr(resultado, "jpeg", None)
        if getattr(resultado, "ok", False) and isinstance(jpeg, (bytes, bytearray)) and jpeg:
            return True, bytes(jpeg), ""
        motivo = getattr(resultado, "motivo", None)
        return False, None, motivo if isinstance(motivo, str) and motivo else "error"

    async def _enviar_foto(self, fuente: str, canal: str) -> ResultadoAccion:
        """Foto de la cámara o captura de la pantalla → Telegram del usuario (v0.2.1).

        Por Telegram exige ``RAMS_TELEGRAM_FOTOS=1``. Pasa por la compuerta (``enviar_foto``: solo el
        usuario, ≤ 10 por hora), respeta la pausa de la cámara y las ventanas privadas, y avisa en
        el PC («📸 …»). La imagen solo vive en memoria.
        """
        tipo = "enviar_foto"
        s = self.settings
        remota = canal == "telegram"

        def fallo(motivo: str, mensaje: str | None = None) -> ResultadoAccion:
            return ResultadoAccion(False, mensaje or MENSAJES_CAPTURA.get(motivo, "No pude tomar la imagen."),
                                   {"fuente": fuente, "motivo": motivo}, accion_tipo=tipo)

        if fuente not in ("camara", "pantalla"):
            return fallo("error", "No sé de dónde tomar la foto.")
        if remota and not getattr(s, "telegram_fotos", False):
            return fallo("fotos_desactivadas")
        enviar = getattr(self.riko, "enviar_foto_telegram", None)
        if not callable(enviar) or not getattr(s, "tiene_telegram", False):
            return fallo("sin_telegram")
        if fuente == "camara":
            bloqueo = self._camara_bloqueada()
            if bloqueo is not None:
                return fallo(bloqueo)
        que = "una foto de la cámara" if fuente == "camara" else "una captura de la pantalla"
        accion = AccionPC(tipo, {"fuente": fuente, "canal": canal}, f"enviar {que} a tu Telegram", origen="usuario")
        if self.permisos is None:
            return fallo("denegada", "Acción no autorizada: compuerta de permisos no disponible.")
        try:
            decision = await self.permisos.autorizar(accion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La compuerta falló con una foto: %s", type(exc).__name__)
            return fallo("denegada", "Acción no autorizada: la compuerta falló.")
        if not decision.aprobada:
            motivo = "fotos_desactivadas" if "RAMS_TELEGRAM_FOTOS" in decision.motivo else "denegada"
            resultado = fallo(motivo, f"Acción no autorizada: {decision.motivo}")
            await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado)
            return resultado
        if fuente == "pantalla" and (self._pantalla_fresca() or {}).get("privada"):
            resultado = fallo("ventana_sensible")
            await self._auditar(accion, canal, decision.veredicto, "ventana privada", resultado)
            return resultado
        titulo = self._titulo_ventana_activa() if fuente == "pantalla" else None
        ok, jpeg, motivo = await self._capturar(fuente, titulo)
        if not ok or jpeg is None:
            resultado = fallo(motivo)
            await self._auditar(accion, canal, decision.veredicto, motivo, resultado)
            return resultado
        leyenda = self._rng.choice(LEYENDAS_FOTO[fuente])
        try:
            enviado = bool(await asyncio.wait_for(enviar(jpeg, leyenda=leyenda), PLAZO_FOTO_TELEGRAM_S))
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # incluye el plazo vencido
            log.warning("No se pudo enviar la imagen por Telegram: %s", type(exc).__name__)
            enviado = False
        finally:
            jpeg = None          # la imagen no se guarda en ningún sitio
        if not enviado:
            resultado = fallo("envio")
            await self._auditar(accion, canal, decision.veredicto, "telegram no la aceptó", resultado)
            return resultado
        aviso = ("📸 Envié una foto de la cámara a tu Telegram" if fuente == "camara"
                 else "📸 Envié una captura de la pantalla a tu Telegram")
        self._anunciar_en_pc(aviso + (" (la pediste desde Telegram)." if remota else "."), canal)
        hecho = "la foto de la cámara" if fuente == "camara" else "la captura de la pantalla"
        resultado = ResultadoAccion(True, f"Te mandé {hecho} por Telegram.", {"fuente": fuente}, accion_tipo=tipo)
        await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado)
        return resultado

    def _responder_con_nota_de_voz(self, canal: str, intencion: Intencion, texto: str, voz: bool) -> bool:
        """Regla de la respuesta por voz (v0.2.1): por Telegram, ``siempre`` o (``auto`` y el usuario
        mandó una nota de voz o pidió audio); desde el PC, "mándame un audio al telegram"."""
        s = self.settings
        if canal == "telegram":
            modo = str(getattr(s, "telegram_audio", "auto") or "auto").strip().lower()
            if modo == "siempre":
                return True
            if modo != "auto":
                return False
            return bool(voz) or intencion.tipo == "pedir_audio" or quiere_audio(texto)
        return (canal in CANALES_LOCALES and intencion.tipo == "pedir_audio"
                and intencion.args.get("destino") == "telegram" and bool(getattr(s, "tiene_telegram", False)))

    async def _enviar_nota_de_voz(self, reply: BrocaReply) -> bool:
        """``riko.enviar_voz_telegram`` con la respuesta; ``False`` si no se pudo (va el texto)."""
        enviar = getattr(self.riko, "enviar_voz_telegram", None)
        texto = reply.texto_plano.strip()
        if not callable(enviar) or not texto:
            return False
        tono = reply.segmentos[0].etiquetas.tone if reply.segmentos else "calido"
        try:
            ok = bool(await asyncio.wait_for(enviar(texto, tone=tono), PLAZO_VOZ_TELEGRAM_S))
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # incluye el plazo vencido
            log.warning("No se pudo enviar la nota de voz por Telegram: %s", type(exc).__name__)
            ok = False
        if ok:
            try:
                self.consola.imprimir_amadeus(f"[→ Telegram 🎤] {texto}", reply.etiquetas)
            except Exception as exc:
                log.debug("No se pudo mostrar la nota de voz en la consola: %s", type(exc).__name__)
        else:
            log.info("La nota de voz no salió; respondo por texto")
        return ok

    # ------------------------------------------------------------------
    # v0.2.2: Google, correo, Drive y taller (docs/V022_CORREO_DRIVE_SCRIPTS.md §14)
    # ------------------------------------------------------------------

    @staticmethod
    def _atributo(modulo: Any, nombre: str, defecto: Any = None) -> Any:
        """``getattr`` que nunca lanza (una propiedad rota de un módulo opcional cuenta como ``defecto``)."""
        if modulo is None:
            return defecto
        try:
            return getattr(modulo, nombre, defecto)
        except Exception as exc:
            log.debug("Atributo %s no disponible (%s)", nombre, type(exc).__name__)
            return defecto

    def _disponible(self, modulo: Any) -> bool:
        return bool(self._atributo(modulo, "disponible", False))

    def _estado_google_actual(self) -> dict[str, Any]:
        """``google.estado().a_dict()`` (la cuenta ya va enmascarada) o ``{}``."""
        estado_fn = self._atributo(self.google, "estado")
        if not callable(estado_fn):
            return dict(self._estado_google or {})
        try:
            estado = estado_fn()
            a_dict = getattr(estado, "a_dict", None)
            datos = a_dict() if callable(a_dict) else estado
            return dict(datos) if isinstance(datos, Mapping) else {}
        except Exception as exc:
            log.debug("Estado de Google no disponible (%s)", type(exc).__name__)
            return dict(self._estado_google or {})

    def _actualizar_herramientas(self, estado: Mapping[str, Any] | None = None) -> None:
        """``broca.set_herramientas`` (§10.5) con lo que hay ahora: al arrancar y en cada ``google.estado``."""
        fijar = self._atributo(self.broca, "set_herramientas")
        if not callable(fijar):
            return
        s = self.settings
        if estado is None:
            estado = self._estado_google_actual()
        drive = self._disponible(self.drive)
        herramientas = {
            "google": bool(getattr(s, "tiene_google", False)) and self.google is not None,
            "google_conectado": bool(estado.get("conectado")) if isinstance(estado, Mapping) else False,
            "correo": self._disponible(self.correo),
            "drive": drive,
            "drive_escribir": drive and bool(self._atributo(self.drive, "puede_escribir", False)),
            "taller": bool(getattr(s, "tiene_taller", False)) and self._disponible(self.taller),
        }
        try:
            fijar(herramientas)
        except Exception as exc:
            log.debug("set_herramientas falló (%s)", type(exc).__name__)

    def _ev_google_estado(self, evento: Evento) -> None:
        """``google.estado`` → capacidades de Broca y, si el permiso caducó, aviso en el PC (§14.1)."""
        datos = dict(evento.datos) if isinstance(evento.datos, Mapping) else {}
        previo, self._estado_google = self._estado_google, datos
        self._actualizar_herramientas(datos)
        if (previo is not None and previo.get("conectado") and not datos.get("conectado")
                and datos.get("motivo") == "token_invalido" and not self._apagando):
            self._aviso_local("Tu permiso de Google caducó o se revocó: di «conecta mi cuenta de Google».",
                              "escritorio")

    def _mensaje_google_apagado(self) -> str:
        s = self.settings
        if not getattr(s, "google_habilitado", False):
            return ("La conexión con Google está apagada: pon RAMS_GOOGLE=1 en el .env, sigue la guía "
                    "(docs/V022_CORREO_DRIVE_SCRIPTS.md §17) y reinicia.")
        if s.offline:
            return "Estoy en modo --offline: sin red no hay conexión con Google."
        return "La conexión con Google no está disponible."

    def _motivo_google_no_listo(self, modulo: Any, servicio: str) -> str | None:
        """``None`` si el módulo (correo o Drive) está listo; si no, el porqué en español (§16)."""
        nombre = "El correo" if servicio == "correo" else "Drive"
        if not getattr(self.settings, "tiene_google", False):
            return self._mensaje_google_apagado()
        if self.google is None or modulo is None:
            return f"{nombre} no está disponible en esta instalación."
        if self._disponible(modulo):
            return None
        servicios = tuple(getattr(self.settings, "google_servicios", ("correo", "drive")) or ())
        if servicio not in servicios:
            return f"{nombre} no está activado (RAMS_GOOGLE_SERVICIOS)."
        estado = self._estado_google_actual()
        if estado.get("motivo") == "sin_cliente":
            return ("Me falta el archivo del cliente OAuth: sigue la guía (docs/V022_CORREO_DRIVE_SCRIPTS.md §17) y "
                    "guárdalo como data/google_cliente.json.")
        if not estado.get("conectado"):
            return "Primero conecta tu cuenta de Google: di «conecta mi cuenta de Google»."
        que = "enviar correo" if servicio == "correo" else "Drive"
        return f"No marcaste el permiso de {que}: reconecta Google y marca la casilla."

    async def _autorizar_v022(self, accion: AccionPC, canal: str) -> DecisionPermiso:
        """``permisos.autorizar`` con el canal del turno en ``_CANAL_ACCION`` (nunca lanza, salvo cancelación)."""
        if self.permisos is None:
            return DecisionPermiso("denegada", "compuerta de permisos no disponible")
        token = _CANAL_ACCION.set(canal)
        try:
            return await self.permisos.autorizar(accion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La compuerta falló con la acción %s: %s", accion.tipo, type(exc).__name__)
            return DecisionPermiso("denegada", "la compuerta falló")
        finally:
            _CANAL_ACCION.reset(token)

    async def _intencion_de_comando_v022(self, comando: str, resto: str, canal: str) -> Intencion | None:
        """``/google [estado|conectar|desconectar]``, ``/script <tarea>`` y ``/habilidades`` → intención."""
        if comando == "/google":
            op = resto.strip().lower() or "estado"
            if op not in ("estado", "conectar", "desconectar"):
                await self._salida_comando("Uso: /google [estado|conectar|desconectar]", canal)
                return None
            if canal == "telegram" and op != "estado":
                await self._salida_comando("Conectar o desconectar Google solo se hace desde el computador.", canal)
                return None
            return Intencion("google", {"op": op})
        if comando == "/script":
            if not resto.strip():
                await self._salida_comando("Uso: /script <qué quieres que haga>", canal)
                return None
            return Intencion("taller", {"op": "crear", "tarea": resto.strip()[:500]})
        return Intencion("taller", {"op": "habilidades"})

    # --- oferta del taller (§14.2, paso 2b) ---------------------------------

    def _resolver_oferta_taller(self, texto: str, intencion: Intencion, canal: str) -> Intencion:
        """Un «sí» corto en el mismo canal local y ≤ 5 min acepta la oferta; cualquier otro mensaje la anula."""
        oferta, self._oferta_taller = self._oferta_taller, None
        if oferta is None:
            return intencion
        try:
            vigente = self._reloj() - float(oferta.get("ts", float("-inf"))) <= TTL_OFERTA_TALLER_S
        except (TypeError, ValueError):
            vigente = False
        if vigente and canal in CANALES_LOCALES and oferta.get("canal") == canal and acepta_oferta(texto):
            return Intencion("taller", {"op": "crear", "tarea": str(oferta.get("tarea") or "")[:500]})
        return intencion

    def _ofrecer_taller(self, texto: str, texto_seguro: str, canal: str) -> bool:
        """Sin intención, en un canal local y con el taller listo: ¿el mensaje pide una tarea del PC?"""
        s = self.settings
        if canal not in CANALES_LOCALES or not getattr(s, "tiene_taller", False):
            return False
        if not getattr(s, "taller_ofrecer", True) or not self._disponible(self.taller):
            return False
        if not pide_tarea_pc(texto):
            return False
        self._oferta_taller = {"tarea": texto_seguro[:500], "canal": canal, "ts": self._reloj()}
        return True

    # --- ramas del paso 3 -----------------------------------------------------

    async def _turno_v022(self, tipo: str, args: Mapping[str, Any], canal: str, alta: bool,
                          texto_seguro: str) -> tuple[ResultadoAccion | None, str | None, bool]:
        """``(resultado, resumen del turno, error_propio)`` de las ramas ``google``/``correo``/``drive``/``taller``.

        El resumen solo existe en los turnos de correo: lo que reciben Broca y la memoria en lugar del
        mensaje (sin cuerpo ni direcciones, §14.2). Una excepción interna es un error propio.
        """
        resumen = self._resumen_turno_correo(args, alta) if tipo == "correo" else None
        try:
            if tipo == "google":
                return await self._turno_google(args, canal), None, False
            if tipo == "correo":
                return await self._turno_correo(args, canal, alta), resumen, False
            if tipo == "drive":
                return await self._turno_drive(args, canal, texto_seguro), None, False
            return await self._turno_taller(args, canal, alta), None, False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # nunca con el contenido en el log
            log.warning("Fallo interno en la rama %s del turno (%s)", tipo, type(exc).__name__)
            return (ResultadoAccion(False, "Algo falló por un error interno; no hice nada.",
                                    accion_tipo=f"{tipo}_error"), resumen, True)

    def _alias_y_direcciones(self, destinatarios: Sequence[str], alta: bool) -> str:
        """«mamá, mi jefe y 2 direcciones más»: alias redactados con la regla del canal; las direcciones
        solo se cuentan (nunca salen hacia Gemini, Puter, Telegram ni la memoria)."""
        secretos = self.settings.secretos()
        alias = [redactar(d.strip()[:40], secretos, alta_entropia=alta) for d in destinatarios if "@" not in d]
        n_direcciones = sum(1 for d in destinatarios if "@" in d)
        nombres = ", ".join(a for a in alias[:5] if a)
        if nombres and n_direcciones:
            return f"{nombres} y {_plural(n_direcciones, 'dirección', 'direcciones')} más"
        if nombres:
            return nombres
        if n_direcciones:
            return _plural(n_direcciones, "dirección", "direcciones")
        return ""

    def _resumen_turno_correo(self, args: Mapping[str, Any], alta: bool) -> str:
        destinatarios = [*_textos(args.get("para")), *_textos(args.get("cc")), *_textos(args.get("cco"))]
        para = self._alias_y_direcciones(destinatarios, alta)
        return f"(pidió preparar un correo para {para})" if para else "(pidió preparar un correo)"

    def _texto_estado_google(self) -> str:
        """Estado local (sin red) de la cuenta de Google para ``/google`` (cuenta enmascarada)."""
        s = self.settings
        if self.google is None:
            return "Cuenta de Google: no disponible en esta instalación."
        if not getattr(s, "tiene_google", False):
            return "Cuenta de Google: " + ("apagada (RAMS_GOOGLE=0)." if not getattr(s, "google_habilitado", False)
                                           else "sin red (--offline).")
        e = self._estado_google_actual()
        if not e.get("conectado"):
            motivos = {
                "sin_cliente": "falta data/google_cliente.json (guía en docs/V022_CORREO_DRIVE_SCRIPTS.md §17)",
                "cliente_invalido": "el cliente OAuth no es válido (¿es de tipo Web?)",
                "conectando": "esperando tu autorización en el navegador",
                "token_invalido": "el permiso caducó o se revocó: di «conecta mi cuenta de Google»",
            }
            detalle = motivos.get(str(e.get("motivo") or ""), "sin conectar: di «conecta mi cuenta de Google»")
            return f"Cuenta de Google: {detalle}."
        cuenta = _corto(e.get("cuenta") or "", 80)
        lineas = [f"Cuenta de Google: conectada{f' ({cuenta})' if cuenta else ''}."]
        lineas.append("Gmail: solo enviar." if e.get("correo")
                      else "Gmail: sin permiso (reconecta y marca «Enviar correo»).")
        if e.get("drive_completo"):
            drive = "completo (leer y modificar todo)"
        elif e.get("drive_leer") and e.get("drive_escribir"):
            drive = "leer todo + modificar lo que cree Amadeus"
        elif e.get("drive_leer"):
            drive = "solo leer"
        else:
            drive = "sin permiso (reconecta y marca Drive)"
        lineas.append(f"Drive: {drive}.")
        return "\n".join(lineas)

    async def _releer_cliente_google(self) -> None:
        """Sin cliente OAuth válido, vuelve a leer ``data/google_cliente.json`` (y el token): el usuario puede
        guardarlo con R.A.M.S. en marcha, como indica la guía. Sin red; nunca lanza (salvo la cancelación)."""
        if not getattr(self.settings, "tiene_google", False):
            return
        if self._estado_google_actual().get("motivo") not in ("sin_cliente", "cliente_invalido"):
            return
        releer = self._atributo(self.google, "releer_cliente")
        if not callable(releer):
            return
        try:
            await releer()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("No se pudo releer el cliente de Google (%s)", type(exc).__name__)

    async def _turno_google(self, args: Mapping[str, Any], canal: str) -> ResultadoAccion:
        op = args.get("op")
        await self._releer_cliente_google()
        if op == "estado":
            await self._salida_comando(self._texto_estado_google(), canal)
            donde = "te lo mandé por aquí" if canal == "telegram" else "está en pantalla"
            return ResultadoAccion(True, f"El estado de la cuenta de Google {donde}.", accion_tipo="google_estado")
        if self.google is None:
            return ResultadoAccion(False, "La cuenta de Google no está disponible en esta instalación.",
                                   accion_tipo=f"google_{op}")
        if op == "conectar":
            return await self._google_conectar(canal)
        if op == "desconectar":
            return await self._google_desconectar(canal)
        return ResultadoAccion(False, "No entendí qué hacer con la cuenta de Google.", accion_tipo="google")

    async def _google_conectar(self, canal: str) -> ResultadoAccion:
        """Compuerta (permitida en local) y conexión EN SEGUNDO PLANO: el turno no espera al navegador."""
        if not getattr(self.settings, "tiene_google", False):
            return ResultadoAccion(False, self._mensaje_google_apagado(), accion_tipo="google_conectar")
        if bool(self._atributo(self.google, "conectando", False)):
            return ResultadoAccion(False, "Ya estoy esperando tu autorización en el navegador: termina allí (tienes "
                                          "5 minutos).", accion_tipo="google_conectar")
        if self._estado_google_actual().get("motivo") == "sin_cliente":
            return ResultadoAccion(False, "Me falta el archivo del cliente OAuth: sigue la guía "
                                          "(docs/V022_CORREO_DRIVE_SCRIPTS.md §17) y guárdalo como "
                                          "data/google_cliente.json.", accion_tipo="google_conectar")
        accion = AccionPC("conectar_google", {"canal": canal}, "conectar tu cuenta de Google", origen="usuario")
        decision = await self._autorizar_v022(accion, canal)
        if not decision.aprobada:
            resultado = ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}", accion_tipo="google_conectar")
            await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado, destino="google")
            return resultado
        self._crear_tarea(self._conectar_google(canal, accion), "conectar_google")
        return ResultadoAccion(True, "Abrí el navegador para conectar tu cuenta de Google. Si sale «Google no verificó "
                                     "esta app», es tu propia app: Configuración avanzada → Ir a Amadeus, y marca las "
                                     "casillas de correo y Drive.", accion_tipo="google_conectar")

    def _mostrar_url_google(self, url: str) -> None:
        """La URL de autorización solo en la consola local (nunca en el log, el bus, el chat ni Telegram)."""
        self.consola.imprimir_sistema("Si no se abrió el navegador, abre este enlace para conectar Google "
                                      f"(solo en esta terminal): {url}")

    @staticmethod
    def _servicios_de_scopes(scopes: Any) -> list[str]:
        """Scopes concedidos → ``["correo", "drive_leer", "drive_escribir"]`` (para la auditoría)."""
        servicios: list[str] = []
        for scope in scopes if isinstance(scopes, (list, tuple)) else ():
            texto = str(scope)
            if texto.endswith("/gmail.send"):
                nuevos = ["correo"]
            elif texto.endswith("/drive.readonly"):
                nuevos = ["drive_leer"]
            elif texto.endswith("/drive.file"):
                nuevos = ["drive_escribir"]
            elif texto.endswith("/drive"):
                nuevos = ["drive_leer", "drive_escribir"]
            else:
                nuevos = []
            servicios.extend(n for n in nuevos if n not in servicios)
        return servicios

    async def _conectar_google(self, canal: str, accion: AccionPC) -> None:
        """Tarea de fondo: ``CuentaGoogle.conectar`` y su resultado anunciado en el PC y auditado."""
        conectar = self._atributo(self.google, "conectar")
        resultado_conexion: Any = None
        if callable(conectar):
            try:
                resultado_conexion = await conectar(mostrar_url=self._mostrar_url_google)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("La conexión con Google falló (%s)", type(exc).__name__)
        ok = bool(getattr(resultado_conexion, "ok", False))
        codigo = str(getattr(resultado_conexion, "codigo", "") or "error")[:40]
        mensaje = str(getattr(resultado_conexion, "mensaje", "") or "").strip()
        if not mensaje:
            mensaje = "Cuenta de Google conectada." if ok else "No pude conectar tu cuenta de Google."
        if not self._apagando:
            self._anunciar_en_pc(mensaje, canal)
        resultado = ResultadoAccion(ok, mensaje[:200], accion_tipo="conectar_google")
        servicios = self._servicios_de_scopes(getattr(resultado_conexion, "concedidos", ()))
        await self._auditar(accion, canal, "permitida", "conexión con Google", resultado,
                            extra={"servicios": servicios, "codigo": codigo}, destino="google")
        self._actualizar_herramientas()

    async def _google_desconectar(self, canal: str) -> ResultadoAccion:
        """Confirmación clásica de la compuerta → revoca y borra el token local (§1.1)."""
        accion = AccionPC("desconectar_google", {"canal": canal},
                          "Desconectar tu cuenta de Google (revoca el permiso de Gmail y Drive)", origen="usuario")
        decision = await self._autorizar_v022(accion, canal)
        if not decision.aprobada:
            resultado = ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}",
                                        accion_tipo="google_desconectar")
            await self._auditar(accion, canal, decision.veredicto if decision.veredicto != "confirmar" else "rechazada",
                                decision.motivo, resultado, destino="google")
            return resultado
        resultado_desconexion: Any = None
        try:
            resultado_desconexion = await self.google.desconectar()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La desconexión de Google falló (%s)", type(exc).__name__)
        ok = bool(getattr(resultado_desconexion, "ok", False))
        mensaje = str(getattr(resultado_desconexion, "mensaje", "") or "").strip() or (
            "Listo: desconecté tu cuenta de Google." if ok else "No pude desconectar tu cuenta de Google.")
        resultado = ResultadoAccion(ok, mensaje[:300], accion_tipo="google_desconectar")
        await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado,
                            extra={"codigo": str(getattr(resultado_desconexion, "codigo", "") or "error")[:40]},
                            destino="google")
        self._actualizar_herramientas()
        return resultado

    async def _turno_correo(self, args: Mapping[str, Any], canal: str, alta: bool) -> ResultadoAccion:
        """Prepara el borrador (texto CRUDO hacia el módulo) y muestra su tarjeta (§14.2)."""
        s = self.settings
        if canal == "telegram" and not getattr(s, "telegram_correo", True):
            return ResultadoAccion(False, "Preparar correos desde Telegram está desactivado (RAMS_TELEGRAM_CORREO=1).",
                                   accion_tipo="correo_borrador")
        await self._releer_cliente_google()
        motivo = self._motivo_google_no_listo(self.correo, "correo")
        if motivo is not None:
            if motivo.startswith("Primero conecta"):
                motivo += " Mientras tanto, puedo dejarte un borrador en Gmail: «escríbele a juan por gmail: …»."
            return ResultadoAccion(False, motivo, accion_tipo="correo_borrador")
        para, cc, cco = _textos(args.get("para")), _textos(args.get("cc")), _textos(args.get("cco"))
        adjuntos = _textos(args.get("adjuntos"), 5)
        asunto = args.get("asunto") if isinstance(args.get("asunto"), str) else None
        cuerpo = args.get("cuerpo") if isinstance(args.get("cuerpo"), str) else None
        tema = args.get("tema") if isinstance(args.get("tema"), str) else None
        avisos: list[str] = []
        # Lo que el enrutador quitó por el contrato (§4.1) se avisa en la tarjeta: nunca en silencio.
        descartados = args.get("descartados") if isinstance(args.get("descartados"), Mapping) else {}
        sin_destinatarios, sin_adjuntos = (_conteo(descartados.get(clave)) for clave in ("destinatarios", "adjuntos"))
        if sin_destinatarios:
            avisos.append(f"No entraron {_plural(sin_destinatarios, 'destinatario', 'destinatarios')} de los que "
                          "dictaste (máximo 10 por campo): mándalos en otro correo")
        if sin_adjuntos:
            avisos.append(f"No adjunté {_plural(sin_adjuntos, 'archivo', 'archivos')}: di solo el nombre o una ruta "
                          "como «documentos/plan.xlsx» (sin «..» ni rutas absolutas; como mucho 5) y "
                          f"{'agrégalo' if sin_adjuntos == 1 else 'agrégalos'} en Editar")
        if args.get("redactar") and tema:
            # Hacia Gemini solo el tema y los ALIAS, redactados con la regla del canal (nunca direcciones).
            secretos = s.secretos()
            alias = [p for p in para if "@" not in p]
            destinatario = redactar(", ".join(alias), secretos, alta_entropia=alta) if alias else ""
            generado: Any = None
            redactar_correo = self._atributo(self.broca, "redactar_correo")
            if callable(redactar_correo):
                try:
                    generado = await redactar_correo(redactar(tema, secretos, alta_entropia=alta),
                                                     destinatario=destinatario)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("redactar_correo falló (%s)", type(exc).__name__)
            if isinstance(generado, Mapping) and isinstance(generado.get("cuerpo"), str) and generado["cuerpo"]:
                cuerpo = generado["cuerpo"]
                if not asunto and isinstance(generado.get("asunto"), str):
                    asunto = generado["asunto"] or None
            else:
                cuerpo = tema                      # el texto del usuario, sin redactar (es el entregable)
                avisos.append("No tengo a Gemini: usé tu texto tal cual")
        ttl = TTL_TARJETA_TELEGRAM_S if canal == "telegram" else TTL_TARJETA_S
        borrador = await self.correo.preparar(para=para, cc=cc, cco=cco, asunto=asunto, cuerpo=cuerpo,
                                              adjuntos=adjuntos, canal_origen=canal, ttl_s=ttl, avisos=avisos)
        destinatarios = [*para, *cc, *cco]
        local = ", ".join(d for d in destinatarios if "@" not in d)[:80]
        n_dir = sum(1 for d in destinatarios if "@" in d)
        que_pc = "un correo" + (f" para {local}" if local else "") + (
            f"{' y' if local else ' para'} {_plural(n_dir, 'dirección', 'direcciones')}" if n_dir else "")
        if self._mostrar_tarjeta("correo", borrador, canal, que_pc=que_pc, boton="Enviar") is None:
            self._descartar_en_modulo("correo", self._atributo(borrador, "id"))
            return ResultadoAccion(False, "No pude mostrar el borrador en una tarjeta.", accion_tipo="correo_borrador")
        para_broca = self._alias_y_direcciones(destinatarios, alta) or "sin destinatario todavía"
        asunto_broca = _corto(redactar(str(self._atributo(borrador, "asunto", "") or ""), s.secretos(),
                                       alta_entropia=alta), 120)
        if canal == "telegram":
            mensaje = (f"Te dejé el borrador para {para_broca} («{asunto_broca}»). Apruébalo en el computador con "
                       "el botón Enviar: desde aquí no envío nada.")
        else:
            mensaje = (f"Borrador para {para_broca} («{asunto_broca}») listo en la tarjeta: revísalo y pulsa Enviar. "
                       "No lo envío sin eso.")
            if self._clientes_escritorio == 0:
                mensaje += f" {MENSAJE_ABRE_CHAT}"
        problemas = tuple(self._atributo(borrador, "problemas", ()) or ())
        if problemas:
            mensaje += (f" Ojo: la tarjeta marca {_plural(len(problemas), 'problema', 'problemas')} por resolver "
                        "antes de enviar.")
        if sin_destinatarios or sin_adjuntos:
            fuera = " y ".join(t for t in (
                _plural(sin_destinatarios, "destinatario", "destinatarios") if sin_destinatarios else "",
                _plural(sin_adjuntos, "adjunto", "adjuntos") if sin_adjuntos else "") if t)
            mensaje += f" Ojo: dejé fuera {fuera}; el aviso de la tarjeta dice por qué."
        return ResultadoAccion(True, mensaje, {"que": "el borrador del correo"}, accion_tipo="correo_borrador")

    async def _turno_drive(self, args: Mapping[str, Any], canal: str, texto_seguro: str) -> ResultadoAccion:
        """Buscar e info (listas por ``_salida_comando``; a Broca solo conteos), descargar, leer, crear carpetas
        y las propuestas con tarjeta (subir, papelera, compartir)."""
        s = self.settings
        op = str(args.get("op") or "")
        tipo_resultado = f"drive_{op}" if op in ("buscar", "info", "descargar", "leer", "crear_carpeta") \
            else "drive_propuesta"
        if canal == "telegram" and not getattr(s, "telegram_drive", False):
            return ResultadoAccion(False, "Buscar en Drive desde Telegram está desactivado (RAMS_TELEGRAM_DRIVE=1).",
                                   accion_tipo=tipo_resultado)
        await self._releer_cliente_google()
        motivo = self._motivo_google_no_listo(self.drive, "drive")
        if motivo is not None:
            return ResultadoAccion(False, motivo, accion_tipo=tipo_resultado)
        ref = str(args.get("ref") or "")
        if op == "buscar":
            consulta = args.get("consulta") if isinstance(args.get("consulta"), str) else None
            resultado = await self.drive.buscar(consulta)
            if not getattr(resultado, "ok", False):
                return ResultadoAccion(False, str(getattr(resultado, "mensaje", "") or "La búsqueda en Drive falló."),
                                       accion_tipo="drive_buscar")
            archivos = tuple(getattr(resultado, "archivos", ()) or ())
            if archivos:
                lineas = [f"Drive · {_plural(len(archivos), 'archivo', 'archivos')}:"]
                lineas += [str(a.linea(i)) for i, a in enumerate(archivos, start=1)]
                await self._salida_comando("\n".join(lineas), canal)
            else:
                await self._salida_comando("Drive: no encontré archivos con eso.", canal)
            donde = "te la mandé por aquí" if canal == "telegram" else "la lista está en pantalla"
            return ResultadoAccion(True, f"Encontré {_plural(len(archivos), 'archivo', 'archivos')} en Drive; {donde}."
                                   if archivos else "No encontré archivos con eso en Drive.", accion_tipo="drive_buscar")
        if op == "info":
            resultado = await self.drive.info(ref)
            if not getattr(resultado, "ok", False):
                return ResultadoAccion(False, str(getattr(resultado, "mensaje", "") or "No encontré ese archivo."),
                                       accion_tipo="drive_info")
            await self._salida_comando(self._texto_info_drive(resultado), canal)
            donde = "te los mandé por aquí" if canal == "telegram" else "están en pantalla"
            return ResultadoAccion(True, f"Los detalles del archivo {donde}.", accion_tipo="drive_info")
        if op == "descargar":
            return await self._drive_descargar(args, canal)
        if op == "leer":
            return await self._drive_leer(ref, texto_seguro)
        if op == "crear_carpeta":
            return await self._drive_crear_carpeta(args, canal)
        ttl = TTL_TARJETA_TELEGRAM_S if canal == "telegram" else TTL_TARJETA_S
        if op == "subir":
            operacion = await self.drive.preparar_subida(str(args.get("nombre") or ""),
                                                         carpeta=args.get("carpeta") or None, canal_origen=canal,
                                                         ttl_s=ttl)
            que = "subir un archivo"
        elif op == "papelera":
            operacion = await self.drive.preparar_papelera(ref, canal_origen=canal, ttl_s=ttl)
            que = "mover un archivo a la papelera"
        elif op == "compartir":
            publico = bool(args.get("publico"))
            rol = str(args.get("rol") or "reader")
            operacion = await self.drive.preparar_compartir(ref, correo=args.get("correo") or None, rol=rol,
                                                            publico=publico, canal_origen=canal, ttl_s=ttl)
            que = "compartir un archivo en público" if publico else "compartir un archivo"
        else:
            return ResultadoAccion(False, "No entendí qué hacer en Drive.", accion_tipo="drive_propuesta")
        if self._mostrar_tarjeta("drive", operacion, canal, que_pc=f"una operación de Drive ({que})",
                                 boton="el botón") is None:
            self._descartar_en_modulo("drive", self._atributo(operacion, "id"))
            return ResultadoAccion(False, "No pude mostrar la operación de Drive en una tarjeta.",
                                   accion_tipo="drive_propuesta")
        mensaje = (f"Preparé la operación de Drive ({que}) en la tarjeta: revísala y pulsa el botón. No la hago sin "
                   "eso.")
        if op == "compartir" and bool(args.get("publico")):
            mensaje += " Hacerlo público pide dos confirmaciones y siempre es de solo lectura."
        problemas = tuple(self._atributo(operacion, "problemas", ()) or ())
        if problemas:
            mensaje += f" Ojo: la tarjeta marca {_plural(len(problemas), 'problema', 'problemas')}."
        if self._clientes_escritorio == 0:
            mensaje += f" {MENSAJE_ABRE_CHAT}"
        return ResultadoAccion(True, mensaje, {"que": "la operación de Drive"}, accion_tipo="drive_propuesta")

    @staticmethod
    def _texto_info_drive(resultado: Any) -> str:
        """Detalles de un archivo de Drive para la consola, el chat o Telegram (nunca para Gemini)."""
        archivos = tuple(getattr(resultado, "archivos", ()) or ())
        if not archivos:
            return str(getattr(resultado, "mensaje", "") or "Sin detalles.")
        a = archivos[0]
        tamano = getattr(a, "tamano", None)
        lineas = [f"Drive · {getattr(a, 'nombre', '')}"]
        tipo = getattr(a, "tipo_legible", None)
        lineas.append(f"Tipo: {tipo() if callable(tipo) else getattr(a, 'tipo_mime', '')}")
        if isinstance(tamano, int) and not isinstance(tamano, bool):
            lineas.append(f"Tamaño: {_miles(tamano)} bytes")
        lineas.append(f"Modificado: {getattr(a, 'modificado', '')}")
        lineas.append("Dueño: tú" if getattr(a, "propio", False) else "Dueño: otra persona")
        enlace = getattr(a, "enlace", None)
        if enlace:
            lineas.append(f"Enlace: {enlace}")
        puede = [n for n, c in (("descargar", "puede_descargar"), ("editar", "puede_editar"),
                                ("mandar a la papelera", "puede_papelera"), ("compartir", "puede_compartir"))
                 if getattr(a, c, False)]
        lineas.append("Puedo: " + (", ".join(puede) if puede else "solo verlo"))
        return "\n".join(lineas)

    async def _drive_descargar(self, args: Mapping[str, Any], canal: str) -> ResultadoAccion:
        destino = str(args.get("destino") or "descargas")
        formato = args.get("formato") if isinstance(args.get("formato"), str) else None
        accion = AccionPC("drive_descargar", {"canal": canal, "destino": destino, "formato": formato},
                          "descargar un archivo de Drive", origen="usuario")
        decision = await self._autorizar_v022(accion, canal)
        if not decision.aprobada:
            resultado = ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}", accion_tipo="drive_descargar")
            await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado, destino="drive",
                                extra={"op": "descargar"})
            return resultado
        r = await self.drive.descargar(str(args.get("ref") or ""), destino=destino, formato=formato)
        datos = getattr(r, "datos", {}) or {}
        ok = bool(getattr(r, "ok", False))
        if ok and datos.get("guardado"):
            tamano = datos.get("bytes")
            extra_tamano = f" ({_miles(tamano)} bytes)" if isinstance(tamano, int) and not isinstance(tamano, bool) else ""
            mensaje = f"Lo guardé en {datos['guardado']}{extra_tamano}."
            if datos.get("nota"):
                mensaje += f" {datos['nota']}"
        else:
            mensaje = str(getattr(r, "mensaje", "") or ("Descargado." if ok else "No pude descargarlo."))
        resultado = ResultadoAccion(ok, mensaje, accion_tipo="drive_descargar")
        archivo_id = str(datos.get("archivo_id") or "")
        await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado, destino="drive",
                            extra={"op": "descargar", "codigo": str(getattr(r, "codigo", ""))[:40],
                                   "bytes": datos.get("bytes") if isinstance(datos.get("bytes"), int) else None,
                                   "archivo_sha12": hashlib.sha256(archivo_id.encode()).hexdigest()[:12]
                                   if archivo_id else None})
        return resultado

    async def _drive_leer(self, ref: str, texto_seguro: str) -> ResultadoAccion:
        """Leer para resumir: solo a pedido y solo con Gemini (Puter no resume documentos)."""
        if not bool(self._atributo(self.broca, "gemini_disponible", False)):
            return ResultadoAccion(False, "Para resumirlo necesito a Gemini; si quieres, te lo descargo.",
                                   accion_tipo="drive_leer")
        r = await self.drive.leer_texto(ref)
        datos = getattr(r, "datos", {}) or {}
        texto = datos.get("texto")
        if not getattr(r, "ok", False) or not isinstance(texto, str) or not texto.strip():
            return ResultadoAccion(False, str(getattr(r, "mensaje", "") or "No pude leer ese documento."),
                                   accion_tipo="drive_leer")
        archivos = tuple(getattr(r, "archivos", ()) or ())
        nombre = str(getattr(archivos[0], "nombre", "") or ref) if archivos else ref
        resumir = self._atributo(self.broca, "resumir_documento")
        resumen = await resumir(texto, nombre=nombre, pedido=texto_seguro) if callable(resumir) else None
        if not resumen:
            return ResultadoAccion(False, "No pude resumirlo ahora; si quieres, te lo descargo.", accion_tipo="drive_leer")
        notas = []
        if datos.get("recortado"):
            notas.append("leí solo el principio, porque es largo")
        if datos.get("nota"):
            notas.append(str(datos["nota"]))
        extra = f" ({'; '.join(notas)})" if notas else ""
        return ResultadoAccion(True, f"Resumen de «{nombre}»{extra}: {resumen}", accion_tipo="drive_leer")

    async def _drive_crear_carpeta(self, args: Mapping[str, Any], canal: str) -> ResultadoAccion:
        nombre = str(args.get("nombre") or "")
        accion = AccionPC("drive_crear_carpeta", {"canal": canal, "nombre": nombre}, "crear una carpeta en Drive",
                          origen="usuario")
        decision = await self._autorizar_v022(accion, canal)
        if not decision.aprobada:
            resultado = ResultadoAccion(False, f"Acción no autorizada: {decision.motivo}",
                                        accion_tipo="drive_crear_carpeta")
            await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado, destino="drive",
                                extra={"op": "crear_carpeta"})
            return resultado
        r = await self.drive.crear_carpeta(nombre, dentro=args.get("carpeta") or None)
        ok = bool(getattr(r, "ok", False))
        mensaje = str(getattr(r, "mensaje", "") or ("Carpeta creada en Drive." if ok else "No pude crear la carpeta."))
        resultado = ResultadoAccion(ok, mensaje, accion_tipo="drive_crear_carpeta")
        await self._auditar(accion, canal, decision.veredicto, decision.motivo, resultado, destino="drive",
                            extra={"op": "crear_carpeta", "codigo": str(getattr(r, "codigo", ""))[:40]})
        return resultado

    async def _turno_taller(self, args: Mapping[str, Any], canal: str, alta: bool) -> ResultadoAccion:
        s = self.settings
        op = str(args.get("op") or "")
        tipo_resultado = {"crear": "taller_propuesta", "ejecutar_habilidad": "taller_propuesta",
                          "habilidades": "taller_habilidades"}.get(op, "taller_habilidad")
        if not getattr(s, "tiene_taller", False):
            mensaje = ("El taller está apagado (RAMS_TALLER=1 en el .env)." if not getattr(s, "taller_habilitado", False)
                       else "El taller de scripts no funciona en este sistema.")
            return ResultadoAccion(False, mensaje, accion_tipo=tipo_resultado)
        if self.taller is None:
            return ResultadoAccion(False, "El taller de scripts no está disponible en esta instalación.",
                                   accion_tipo=tipo_resultado)
        if op == "habilidades":
            habilidades = tuple(self.taller.habilidades() or ())
            if habilidades:
                lineas = ["Habilidades guardadas:"]
                for h in habilidades:
                    lenguaje = _NOMBRES_LENGUAJE.get(str(getattr(h, "lenguaje", "")), str(getattr(h, "lenguaje", "")))
                    usos = int(getattr(h, "usos", 0) or 0)
                    lineas.append(f"  {getattr(h, 'nombre', '')} · {lenguaje} · riesgo {getattr(h, 'nivel', '?')} · "
                                  f"{_plural(usos, 'uso', 'usos')}")
                texto = "\n".join(lineas)
            else:
                texto = "No tengo habilidades guardadas todavía."
            await self._salida_comando(texto, canal)
            return ResultadoAccion(True, f"Tienes {_plural(len(habilidades), 'habilidad', 'habilidades')} guardadas; "
                                         "la lista está en pantalla.", accion_tipo="taller_habilidades")
        if op == "olvidar_habilidad":
            nombre = str(args.get("nombre") or "")
            ok = bool(await self.taller.olvidar_habilidad(nombre))
            return ResultadoAccion(ok, f"Olvidé la habilidad «{nombre}»." if ok
                                   else f"No tengo ninguna habilidad llamada «{nombre}».", accion_tipo="taller_habilidad")
        if op == "crear":
            tarea = str(args.get("tarea") or "").strip()
            if not tarea:
                return ResultadoAccion(False, "Dime qué quieres que haga el script: «hazme un script que …».",
                                       accion_tipo="taller_propuesta")
            # Hacia Gemini, la tarea redactada con la regla del canal (§14.2).
            propuesta = await self.taller.proponer(redactar(tarea, s.secretos(), alta_entropia=alta)[:500],
                                                   canal_origen=canal, ttl_s=TTL_TARJETA_S)
        elif op == "ejecutar_habilidad":
            propuesta = await self.taller.cargar_habilidad(str(args.get("nombre") or ""), canal_origen=canal,
                                                           ttl_s=TTL_TARJETA_S)
        else:
            return ResultadoAccion(False, "No entendí qué hacer con el taller.", accion_tipo="taller_propuesta")
        if not callable(getattr(propuesta, "huella", None)):        # FalloTaller
            return ResultadoAccion(False, str(getattr(propuesta, "mensaje", "") or "No pude escribir el script."),
                                   accion_tipo="taller_propuesta")
        aviso = ("El código cambió desde que lo guardaste: revísalo de nuevo."
                 if getattr(propuesta, "habilidad_cambiada", False) else "")
        if self._mostrar_tarjeta("script", propuesta, canal, que_pc="un script", boton="Ejecutar", aviso=aviso) is None:
            self._descartar_en_modulo("script", self._atributo(propuesta, "id"))
            return ResultadoAccion(False, "No pude mostrar el script en una tarjeta.", accion_tipo="taller_propuesta")
        analisis = getattr(propuesta, "analisis", None)
        lenguaje = _NOMBRES_LENGUAJE.get(str(getattr(propuesta, "lenguaje", "")), "?")
        nivel = str(getattr(analisis, "nivel", "?"))
        if bool(getattr(analisis, "ejecutable", False)):
            mensaje = (f"Script de {lenguaje}, riesgo {nivel}, en la tarjeta: léelo y pulsa Ejecutar si estás de "
                       "acuerdo. No lo ejecuto sin eso.")
        else:
            mensaje = f"Script de {lenguaje}, riesgo {nivel}: no lo ejecuto desde la app; te lo dejo para copiar."
        if aviso:
            mensaje += f" {aviso}"
        if self._clientes_escritorio == 0:
            mensaje += f" {MENSAJE_ABRE_CHAT}"
        return ResultadoAccion(True, mensaje, {"que": "el script"}, accion_tipo="taller_propuesta")

    # --- tarjetas (§14.3) -----------------------------------------------------

    def _modulo_de_clase(self, clase: str) -> Any:
        return {"correo": self.correo, "drive": self.drive, "script": self.taller}.get(clase)

    def _descartar_en_modulo(self, clase: str, ident: Any) -> None:
        descartar = self._atributo(self._modulo_de_clase(clase), "descartar")
        if callable(descartar) and isinstance(ident, str):
            try:
                descartar(ident)
            except Exception as exc:
                log.debug("descartar(%s) falló (%s)", clase, type(exc).__name__)

    def _objeto_tarjeta(self, clase: str, ident: str) -> Any:
        """El borrador, la operación o la propuesta vigente en su módulo (o ``None``)."""
        nombre = {"correo": "borrador", "drive": "operacion", "script": "propuesta"}.get(clase, "")
        obtener = self._atributo(self._modulo_de_clase(clase), nombre)
        if not callable(obtener):
            return None
        try:
            return obtener(ident)
        except Exception as exc:
            log.debug("%s(%s) falló (%s)", nombre, clase, type(exc).__name__)
            return None

    @staticmethod
    def _tipo_accion_tarjeta(clase: str, objeto: Any) -> str | None:
        if clase == "correo":
            return "enviar_correo"
        if clase == "script":
            return "ejecutar_script"
        if clase == "drive":
            return _TIPO_ACCION_DRIVE.get(str(getattr(objeto, "op", "")))
        return None

    def _reloj_compuerta(self) -> float:
        reloj = self._atributo(self.permisos, "reloj")
        try:
            return float(reloj()) if callable(reloj) else time.monotonic()
        except Exception as exc:
            log.debug("Reloj de la compuerta no disponible (%s)", type(exc).__name__)
            return time.monotonic()

    def _presentacion_tarjeta(self, clase: str, seccion: Mapping[str, Any]) -> tuple[str, str, list[str]]:
        """``(titulo, resumen, botones)`` de una tarjeta aprobable: nunca cuerpo, direcciones ni código (§12.2)."""
        if clase == "correo":
            n = sum(len(seccion.get(k) or ()) for k in ("para", "cc", "cco"))
            problemas = len(seccion.get("problemas") or ())
            resumen = "Revisa Para, asunto, cuerpo y adjuntos: el correo sale de tu Gmail solo si pulsas Enviar."
            if problemas:
                resumen += f" Hay {_plural(problemas, 'problema', 'problemas')} por resolver."
            return f"Correo para {_plural(n, 'destinatario', 'destinatarios')}", resumen, ["aprobar", "editar",
                                                                                          "descartar"]
        if clase == "drive":
            op = seccion.get("op")
            paso = seccion.get("paso")
            if op == "subir":
                return ("Subir un archivo a Drive", "Revisa el archivo y la carpeta de destino: se sube solo si pulsas "
                        "Subir.", ["aprobar", "descartar"])
            if op == "papelera":
                return ("Mover a la papelera de Drive", "Se puede restaurar durante 30 días: nunca borro de forma "
                        "definitiva.", ["aprobar", "descartar"])
            compartir = seccion.get("compartir") if isinstance(seccion.get("compartir"), Mapping) else {}
            if compartir.get("publico"):
                if paso == 2:
                    return ("Compartir en público (paso 2 de 2)", "Confirma en menos de 2 minutos: cualquiera con el "
                            "enlace podrá verlo (solo lectura).", ["aprobar_publico", "descartar"])
                return ("Compartir en público (paso 1 de 2)", "Cualquiera con el enlace podrá verlo (solo lectura). "
                        "Pide dos confirmaciones.", ["aprobar", "descartar"])
            return ("Compartir un archivo de Drive", "Google le avisará por correo a la persona.",
                    ["aprobar", "descartar"])
        lenguaje = _NOMBRES_LENGUAJE.get(str(seccion.get("lenguaje") or ""), "?")
        nivel = str(seccion.get("nivel") or "?")
        if seccion.get("ejecutable"):
            botones = ["aprobar", "guardar", "copiar", "descartar"]
            if seccion.get("habilidad") and not seccion.get("habilidad_cambiada"):
                botones.remove("guardar")
            resumen = "Lee el código completo y el análisis: solo se ejecuta si pulsas Ejecutar."
        else:
            botones = ["copiar", "descartar"]
            resumen = ("Riesgo crítico o con errores: no lo ejecuto desde la app. Te lo dejo para copiar, por si lo "
                       "quieres revisar y correr tú.")
        return f"Script de {lenguaje} · riesgo {nivel}", resumen, botones

    def _datos_tarjeta(self, clase: str, objeto: Any, t: Any, *, aviso: str = "",
                       ajustes: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        """Mensaje ``tarjeta.mostrar`` (§12.2) a partir de ``objeto.vista()`` y la tarjeta de la compuerta."""
        try:
            seccion = dict(objeto.vista())
        except Exception as exc:
            log.warning("La vista de la tarjeta %s falló (%s)", clase, type(exc).__name__)
            return None
        if ajustes:
            seccion.update(ajustes)
        if clase == "drive":
            doble_hasta = getattr(t, "doble_ok_hasta", None)
            seccion["paso"] = 2 if (getattr(t, "doble", False) and doble_hasta is not None
                                    and self._reloj_compuerta() < float(doble_hasta)) else 1
        titulo, resumen, botones = self._presentacion_tarjeta(clase, seccion)
        restante = max(0.0, float(getattr(t, "caduca", 0.0)) - self._reloj_compuerta())
        return {"id": str(t.id), "clase": clase, "version": int(t.version), "sha256": str(t.huella),
                "titulo": _corto(titulo, MAX_TITULO_TARJETA), "resumen": _corto(resumen, MAX_RESUMEN_TARJETA),
                "aviso": _corto(aviso, MAX_AVISO_TARJETA), "botones": botones,
                "caduca_ts": time.time() + restante, "canal_origen": str(t.canal_origen),
                "correo": seccion if clase == "correo" else None, "drive": seccion if clase == "drive" else None,
                "script": seccion if clase == "script" else None, "salida": None}

    def _publicar_tarjeta(self, datos: dict[str, Any]) -> None:
        """``tarjeta.mostrar`` (privado: solo para ``/escritorio``; nadie lo registra)."""
        ident = str(datos.get("id"))
        self._tarjetas_publicadas.pop(ident, None)
        self._tarjetas_publicadas[ident] = datos
        while len(self._tarjetas_publicadas) > MAX_TARJETAS_PUBLICADAS:
            self._tarjetas_publicadas.pop(next(iter(self._tarjetas_publicadas)))
        self._publicar("tarjeta.mostrar", datos)

    def _publicar_cerrada(self, ident: str, clase: str, resultado: str, motivo: str) -> None:
        """``tarjeta.cerrada{id, clase, resultado, motivo}`` (limpia los clientes)."""
        self._tarjetas_publicadas.pop(ident, None)
        self._reabierta_tras_boton.pop(ident, None)     # cerrada: un clic tardío ya cae en «no vigente»
        if clase not in ("correo", "drive", "script", "salida"):
            return
        self._publicar("tarjeta.cerrada", {"id": ident, "clase": clase, "resultado": resultado,
                                           "motivo": _corto(motivo, MAX_MOTIVO_CIERRE)})

    def _registrar_y_publicar(self, clase: str, objeto: Any, canal_origen: str, *,
                              aviso: str = "") -> dict[str, Any] | None:
        """Registra (o reabre: misma id → ``version+1``) la tarjeta en la compuerta y la publica (§14.3, pasos 1-2)."""
        registrar = self._atributo(self.permisos, "registrar_tarjeta")
        tipo_accion = self._tipo_accion_tarjeta(clase, objeto)
        if not callable(registrar) or tipo_accion is None:
            return None
        try:
            ident = str(objeto.id)
            huella = str(objeto.huella())
        except Exception as exc:
            log.warning("La tarjeta %s no trae id o huella (%s)", clase, type(exc).__name__)
            return None
        ttl = TTL_TARJETA_TELEGRAM_S if canal_origen == "telegram" else TTL_TARJETA_S
        doble = clase == "drive" and bool(getattr(objeto, "publico", False))
        nueva, reemplazada = registrar(ident, tipo_accion=tipo_accion, huella=huella, ttl_s=ttl,
                                       canal_origen=canal_origen, doble=doble)
        if nueva is None:
            log.warning("La compuerta no aceptó la tarjeta %s", clase)
            return None
        if reemplazada is not None:
            self._descartar_en_modulo(reemplazada.clase, reemplazada.id)
            self._publicar_cerrada(reemplazada.id, reemplazada.clase, "reemplazado", "la reemplazó una tarjeta nueva")
        datos = self._datos_tarjeta(clase, objeto, nueva, aviso=aviso)
        if datos is None:
            self.permisos.retirar_tarjeta(ident)
            return None
        self._publicar_tarjeta(datos)
        return datos

    def _mostrar_tarjeta(self, clase: str, objeto: Any, canal_origen: str, *, que_pc: str = "", boton: str = "",
                         aviso: str = "") -> dict[str, Any] | None:
        """§14.3: registra la tarjeta, la publica y avisa según el canal de origen."""
        datos = self._registrar_y_publicar(clase, objeto, canal_origen, aviso=aviso)
        if datos is None:
            return None
        if canal_origen == "terminal":
            # La vista previa completa SOLO en la pantalla local (nunca en el log).
            self.consola.imprimir_sistema(self._vista_previa_consola(clase, datos))
            if self._overlay_conectado:
                self._orden_escritorio("abrir_chat")
        elif canal_origen == "telegram":
            self._anunciar_en_pc(f"📱 Desde Telegram preparaste {que_pc or 'algo'}: revísalo en el chat y pulsa "
                                 f"{boton or 'el botón'}.", "telegram")
        return datos

    def _vista_previa_consola(self, clase: str, datos: Mapping[str, Any]) -> str:
        """Vista previa completa de una tarjeta para la consola local (§1.2)."""
        lineas = [f"── Tarjeta: {datos.get('titulo', '')}"]
        seccion = datos.get(clase) if isinstance(datos.get(clase), Mapping) else {}
        if clase == "correo":
            for etiqueta, clave in (("Para", "para"), ("CC", "cc"), ("CCO", "cco")):
                if seccion.get(clave):
                    lineas.append(f"{etiqueta}: {', '.join(str(x) for x in seccion[clave])}")
            lineas.append(f"Asunto: {seccion.get('asunto', '')}")
            for adjunto in seccion.get("adjuntos") or ():
                if isinstance(adjunto, Mapping):
                    lineas.append(f"Adjunto: {adjunto.get('nombre', '')} ({_miles(adjunto.get('bytes', 0))} bytes)")
            lineas.append("Cuerpo:")
            lineas.extend(str(seccion.get("cuerpo") or "").split("\n"))
        elif clase == "drive":
            archivo = seccion.get("archivo") if isinstance(seccion.get("archivo"), Mapping) else None
            local = seccion.get("local") if isinstance(seccion.get("local"), Mapping) else None
            compartir = seccion.get("compartir") if isinstance(seccion.get("compartir"), Mapping) else None
            if archivo:
                lineas.append(f"Archivo de Drive: {archivo.get('nombre', '')}")
            if local:
                lineas.append(f"Archivo local: {local.get('nombre', '')} ({_miles(local.get('bytes', 0))} bytes)")
            if seccion.get("carpeta_destino"):
                lineas.append(f"Carpeta de destino: {seccion.get('carpeta_destino')}")
            if compartir:
                con = "cualquiera con el enlace" if compartir.get("publico") else str(compartir.get("con") or "")
                lineas.append(f"Compartir con: {con} ({compartir.get('rol', '')})")
        else:
            lineas.append(str(seccion.get("explicacion") or ""))
            for hallazgo in seccion.get("hallazgos") or ():
                if isinstance(hallazgo, Mapping):
                    linea = f" (línea {hallazgo.get('linea')})" if hallazgo.get("linea") else ""
                    lineas.append(f"  [{hallazgo.get('nivel', '')}] {hallazgo.get('detalle', '')}{linea}")
            for numero, codigo in enumerate(str(seccion.get("codigo") or "").split("\n"), start=1):
                lineas.append(f"{numero:>4} │ {codigo}")
        for problema in seccion.get("problemas") or ():
            lineas.append(f"⚠ {problema}")
        if datos.get("aviso"):
            lineas.append(f"⚠ {datos.get('aviso')}")
        lineas.append("Se aprueba solo con el botón de la tarjeta, en el chat de Amadeus Desktop o del HUD."
                      + ("" if self._clientes_escritorio else f" {MENSAJE_ABRE_CHAT}"))
        return "\n".join(lineas)

    # --- decisiones de las tarjetas (§14.4) -----------------------------------

    def _ev_escritorio_tarjeta(self, evento: Evento) -> None:
        """Botón de una tarjeta (``escritorio.tarjeta``, ya validado por escritorio_link). Síncrono: valida los
        formatos y programa ``_decidir_tarjeta``. Nunca registra los campos."""
        if self._apagando or not self._iniciado:
            return
        d = evento.datos if isinstance(evento.datos, Mapping) else {}
        ident, sha, decision = d.get("id"), d.get("sha256"), d.get("decision")
        if not (isinstance(ident, str) and ID_TARJETA_RE.fullmatch(ident)):
            return
        if not (isinstance(sha, str) and HUELLA_RE.fullmatch(sha)) or decision not in DECISIONES_TARJETA:
            return
        nombre: str | None = None
        campos: dict[str, Any] | None = None
        if decision == "guardar":
            bruto = d.get("nombre")
            if not isinstance(bruto, str) or not 1 <= len(bruto.strip()) <= 40:
                return
            nombre = bruto.strip()
        if decision == "editar":
            bruto_campos = d.get("campos")
            if not isinstance(bruto_campos, Mapping):
                return
            campos = {str(k): v for k, v in bruto_campos.items() if isinstance(v, str)}
        # Número de llegada, asignado aquí (síncrono, al publicarse el botón) y no al tomar el lock: un clic
        # que espera en la cola mientras se ejecuta la aprobación anterior queda «antes» de la reapertura.
        self._n_boton_tarjeta += 1
        self._crear_tarea(self._decidir_tarjeta(ident, sha, str(decision), nombre, campos, self._n_boton_tarjeta),
                          "tarjeta")

    async def _decidir_tarjeta(self, ident: str, sha: str, decision: str, nombre: str | None,
                               campos: Mapping[str, Any] | None, n_boton: int) -> None:
        """Una decisión cada vez (``_lock_tarjetas``); un fallo nunca tumba nada."""
        async with self._lock_tarjetas:
            try:
                await self._decidir_tarjeta_cuerpo(ident, sha, decision, nombre, campos, n_boton)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # nunca con el contenido en el log
                log.warning("No se pudo procesar el botón de una tarjeta (%s)", type(exc).__name__)
                self._aviso_local("No pude procesar el botón de la tarjeta por un error interno.", "escritorio")

    async def _decidir_tarjeta_cuerpo(self, ident: str, sha: str, decision: str, nombre: str | None,
                                      campos: Mapping[str, Any] | None, n_boton: int) -> None:
        # 1. Tarjetas «salida»: solo se cierran.
        if ident in self._tarjetas_info:
            if decision == "descartar":
                self._tarjetas_info.pop(ident, None)
                self._publicar_cerrada(ident, "salida", "descartado", "cerrada")
            return
        t = self._atributo(self.permisos, "tarjeta")
        t = t(ident) if callable(t) else None
        if t is None:
            publicada = self._tarjetas_publicadas.get(ident)
            if publicada is not None:
                self._publicar_cerrada(ident, str(publicada.get("clase")), "caducado", "ya no está vigente")
            self._sistema_escritorio("Esa tarjeta ya no está vigente.")
            return
        clase = str(t.clase)
        # 1b. Clic anterior a la reapertura (doble clic, o Desktop y RINE a la vez): la reapertura conserva id y
        # huella, así que la huella no lo distingue. Nunca ejecuta otra vez (§0.3, §1.2: nada se reintenta solo);
        # se republica la versión vigente tal cual (con su aviso, p. ej. «Puede que ya se haya enviado»).
        if decision in ("aprobar", "aprobar_publico") and n_boton <= self._reabierta_tras_boton.get(ident, 0):
            log.info("Botón de aprobar anterior a la reapertura de una tarjeta %s: ignorado", clase)
            vigente = self._tarjetas_publicadas.get(ident)
            if vigente is not None:
                self._publicar_tarjeta(vigente)
            return
        objeto = self._objeto_tarjeta(clase, ident)
        # 2. Huella: salvo descartar, el clic debe ser sobre lo que está registrado.
        if decision != "descartar" and not hmac.compare_digest(str(t.huella).encode("ascii"), sha.encode("ascii")):
            if objeto is not None:
                datos = self._datos_tarjeta(clase, objeto, t, aviso="Cambió: revisa la versión nueva.")
                if datos is not None:
                    self._publicar_tarjeta(datos)
            return
        # 3. Decisión.
        if decision == "descartar":
            self._descartar_en_modulo(clase, ident)
            self.permisos.retirar_tarjeta(ident)
            self._publicar_cerrada(ident, clase, "descartado", "descartada")
            return
        if objeto is None:
            self.permisos.retirar_tarjeta(ident)
            self._publicar_cerrada(ident, clase, "caducado", "ya no está vigente")
            self._sistema_escritorio("Esa tarjeta ya no está vigente.")
            return
        if decision == "editar":
            await self._editar_correo(ident, t, campos or {}, clase)
            return
        if decision == "guardar":
            await self._guardar_habilidad(ident, sha, t, objeto, clase, nombre or "")
            return
        if (decision == "aprobar" and clase == "drive" and getattr(t, "doble", False)
                and (t.doble_ok_hasta is None or self._reloj_compuerta() >= float(t.doble_ok_hasta))):
            # Compartir en público, 1.er clic: la tarjeta pasa al paso 2 (≤ 120 s para el 2.º).
            t2 = self.permisos.marcar_primera_confirmacion(ident, sha)
            datos = self._datos_tarjeta(clase, objeto, t2 or t,
                                        aviso="" if t2 is not None else "No pude registrar la primera confirmación.")
            if datos is not None:
                self._publicar_tarjeta(datos)
            return
        await self._aprobar_tarjeta(clase, objeto, t, sha, decision)

    async def _editar_correo(self, ident: str, t: Any, campos: Mapping[str, Any], clase: str) -> None:
        if clase != "correo" or not campos or self.correo is None:
            return
        borrador = await self.correo.editar(ident, campos)          # crudos: el módulo revalida todo
        t2 = self.permisos.actualizar_tarjeta(ident, borrador.huella()) if borrador is not None else None
        if borrador is None or t2 is None:
            self.permisos.retirar_tarjeta(ident)
            self._publicar_cerrada(ident, clase, "caducado", "ya no está vigente")
            self._sistema_escritorio("Ese borrador ya no está vigente.")
            return
        datos = self._datos_tarjeta(clase, borrador, t2)
        if datos is not None:
            self._publicar_tarjeta(datos)

    async def _guardar_habilidad(self, ident: str, sha: str, t: Any, propuesta: Any, clase: str,
                                 nombre: str) -> None:
        if clase != "script" or not nombre or self.taller is None:
            return
        habilidad = await self.taller.guardar_habilidad(ident, sha, nombre)
        # Habilidad | FalloTaller (este último trae «codigo» y «mensaje»).
        guardada = habilidad is not None and not (hasattr(habilidad, "codigo") and hasattr(habilidad, "mensaje"))
        if guardada:
            nombre_real = str(getattr(habilidad, "nombre", nombre))
            aviso = f"Guardé la habilidad «{nombre_real}»."
            ajustes: dict[str, Any] | None = {"habilidad": nombre_real[:40], "habilidad_cambiada": False}
        else:
            aviso = str(getattr(habilidad, "mensaje", "") or "No pude guardar la habilidad.")
            ajustes = None
        self._aviso_local(aviso, "escritorio")
        datos = self._datos_tarjeta(clase, propuesta, t, aviso=aviso, ajustes=ajustes)
        if datos is not None:
            self._publicar_tarjeta(datos)

    async def _aprobar_tarjeta(self, clase: str, objeto: Any, t: Any, sha: str, decision: str) -> None:
        """Construye la ``AccionPC`` (solo metadatos), la pasa una vez por la compuerta y ejecuta el módulo."""
        ident = str(t.id)
        args: dict[str, Any] = {"canal": "escritorio", "tarjeta": ident, "sha256": sha, "aprobacion": "tarjeta"}
        if clase == "correo":
            if not bool(getattr(objeto, "listo", False)):
                datos = self._datos_tarjeta(clase, objeto, t, aviso="Resuelve los problemas del borrador antes de "
                                                                     "enviarlo.")
                if datos is not None:
                    self._publicar_tarjeta(datos)
                return
            n = int(getattr(objeto, "n_destinatarios", 0) or 0)
            tipo, destino = "enviar_correo", "gmail"
            args["n_destinatarios"] = n
            descripcion = f"enviar un correo a {_plural(n, 'destinatario', 'destinatarios')}"
        elif clase == "drive":
            op = str(getattr(objeto, "op", ""))
            tipo, destino = _TIPO_ACCION_DRIVE.get(op, ""), "drive"
            if tipo == "drive_compartir":
                args.update({"rol": getattr(objeto, "rol", None), "publico": bool(getattr(objeto, "publico", False)),
                             "doble_confirmacion": decision == "aprobar_publico"})
            descripcion = {"drive_subir": "subir un archivo a Drive", "drive_papelera": "mover un archivo a la papelera "
                           "de Drive", "drive_compartir": "compartir un archivo de Drive"}.get(tipo, "operación de Drive")
        else:
            analisis = getattr(objeto, "analisis", None)
            nivel = str(getattr(analisis, "nivel", "") or "")
            lenguaje = str(getattr(objeto, "lenguaje", "") or "")
            tipo, destino = "ejecutar_script", lenguaje or "script"
            if not bool(getattr(analisis, "ejecutable", False)):
                motivo_fn = self._atributo(analisis, "motivo_no_ejecutable", "")
                try:
                    motivo = str((motivo_fn() if callable(motivo_fn) else motivo_fn) or "")
                except Exception as exc:
                    log.debug("motivo_no_ejecutable falló (%s)", type(exc).__name__)
                    motivo = ""
                datos = self._datos_tarjeta(clase, objeto, t, aviso=motivo or "Riesgo crítico: no lo ejecuto desde "
                                                                               "la app.")
                if datos is not None:
                    self._publicar_tarjeta(datos)
                return
            args["nivel"] = nivel
            descripcion = (f"ejecutar un script de {_NOMBRES_LENGUAJE.get(lenguaje, lenguaje)} (riesgo {nivel})")
        accion = AccionPC(tipo, args, descripcion, origen="usuario")
        decision_permiso = await self._autorizar_v022(accion, "escritorio")
        if not decision_permiso.aprobada:
            resultado = ResultadoAccion(False, f"Acción no autorizada: {decision_permiso.motivo}", accion_tipo=tipo)
            await self._auditar(accion, "escritorio", decision_permiso.veredicto, decision_permiso.motivo, resultado,
                                destino=destino)
            vigente = self.permisos.tarjeta(ident) or t
            datos = self._datos_tarjeta(clase, objeto, vigente, aviso=decision_permiso.motivo)
            if datos is not None:
                self._publicar_tarjeta(datos)
            return
        if clase == "correo":
            await self._enviar_correo_aprobado(accion, objeto, ident, sha, decision_permiso)
        elif clase == "drive":
            await self._ejecutar_drive_aprobado(accion, objeto, ident, sha, decision_permiso)
        else:
            await self._ejecutar_script_aprobado(accion, objeto, ident, sha, decision_permiso)

    def _reabrir_tarjeta(self, clase: str, ident: str, objeto: Any, aviso: str) -> None:
        """Reapertura tras un fallo o tras ejecutar: misma id → ``version+1`` (otro clic para reintentar).

        Solo con el objeto VIGENTE en su módulo: si ya no está (lo reemplazó otro o caducó), la tarjeta se
        cierra en vez de reabrir una copia vieja que desplazaría a la nueva de su clase.
        """
        if objeto is None:
            self._publicar_cerrada(ident, clase, "fallo", aviso or "ya no está vigente")
            return
        canal_origen = str(getattr(objeto, "canal_origen", "escritorio") or "escritorio")
        if canal_origen not in ("terminal", "escritorio", "telegram"):
            canal_origen = "escritorio"
        # Todo botón ya recibido se pulsó sobre la versión anterior (ver _decidir_tarjeta_cuerpo, paso 1b).
        self._reabierta_tras_boton.pop(ident, None)
        self._reabierta_tras_boton[ident] = self._n_boton_tarjeta
        while len(self._reabierta_tras_boton) > MAX_TARJETAS_PUBLICADAS:
            self._reabierta_tras_boton.pop(next(iter(self._reabierta_tras_boton)))
        if self._registrar_y_publicar(clase, objeto, canal_origen, aviso=aviso) is None:
            self._publicar_cerrada(ident, clase, "fallo", aviso or "no se pudo reabrir")

    async def _enviar_correo_aprobado(self, accion: AccionPC, borrador: Any, ident: str, sha: str,
                                      decision: DecisionPermiso) -> None:
        resultado_envio: Any = None
        try:
            resultado_envio = await self.correo.enviar(ident, sha)
        except asyncio.CancelledError:
            raise
        except Exception as exc:   # una excepción cuenta como «pudo salir»: no se libera el cupo
            log.warning("El envío del correo falló (%s)", type(exc).__name__)
        ok = bool(getattr(resultado_envio, "ok", False))
        codigo = str(getattr(resultado_envio, "codigo", "") or "error")[:40]
        cuerpo = str(getattr(borrador, "cuerpo", "") or "")
        n = int(getattr(borrador, "n_destinatarios", 0) or 0)
        extra = {"n_destinatarios": n, "n_adjuntos": len(tuple(getattr(borrador, "adjuntos", ()) or ())),
                 "bytes": getattr(resultado_envio, "bytes", None), "asunto_len": len(str(getattr(borrador, "asunto", "")
                                                                                         or "")),
                 "cuerpo_len": len(cuerpo), "cuerpo_sha256": hashlib.sha256(cuerpo.encode("utf-8")).hexdigest()[:12],
                 "gmail_id": getattr(resultado_envio, "gmail_id", None), "codigo": codigo}
        if ok:
            self._publicar_cerrada(ident, "correo", "enviado", "enviado a Gmail")
            asunto = _corto(getattr(borrador, "asunto", "") or "", 80)
            self._aviso_local(f"📧 Enviado a Gmail: «{asunto}» para {_plural(n, 'destinatario', 'destinatarios')}.",
                              "escritorio")
            mensaje = "enviado a Gmail"
        else:
            pudo_salir = True if resultado_envio is None else bool(getattr(resultado_envio, "pudo_salir", True))
            if not pudo_salir:
                liberar = self._atributo(self.permisos, "liberar_envio_correo")
                if callable(liberar):
                    liberar(ident)             # nada salió: el intento no gasta cupo (§5.4)
            mensaje = str(getattr(resultado_envio, "mensaje", "") or "") or (
                "El envío falló por un error interno: puede que haya salido. Revisa Enviados en Gmail antes de volver "
                "a pulsar Enviar.")
            self._reabrir_tarjeta("correo", ident, self._objeto_tarjeta("correo", ident), mensaje)
        resultado = ResultadoAccion(ok, mensaje[:200], accion_tipo="enviar_correo")
        await self._auditar(accion, "escritorio", decision.veredicto, decision.motivo, resultado, extra=extra,
                            destino="gmail")

    async def _ejecutar_drive_aprobado(self, accion: AccionPC, operacion: Any, ident: str, sha: str,
                                       decision: DecisionPermiso) -> None:
        resultado_drive: Any = None
        try:
            resultado_drive = await self.drive.ejecutar(ident, sha)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La operación de Drive falló (%s)", type(exc).__name__)
        ok = bool(getattr(resultado_drive, "ok", False))
        datos = getattr(resultado_drive, "datos", {}) or {}
        mensaje = str(getattr(resultado_drive, "mensaje", "") or ("Hecho en Drive." if ok else
                                                                   "La operación de Drive falló."))
        if ok:
            self._publicar_cerrada(ident, "drive", "hecho", "hecho")
            aviso = f"Drive: {mensaje}"
            if datos.get("enlace"):
                aviso += f" Enlace: {datos['enlace']}"
            self._aviso_local(aviso, "escritorio")
        else:
            self._reabrir_tarjeta("drive", ident, self._objeto_tarjeta("drive", ident), mensaje)
        archivo = getattr(operacion, "archivo", None)
        archivo_id = str(getattr(archivo, "id", "") or "")
        extra = {"op": str(getattr(operacion, "op", ""))[:20],
                 "archivo_sha12": hashlib.sha256(archivo_id.encode()).hexdigest()[:12] if archivo_id else None,
                 "bytes": getattr(operacion, "local_bytes", None), "rol": getattr(operacion, "rol", None),
                 "publico": bool(getattr(operacion, "publico", False)),
                 "codigo": str(getattr(resultado_drive, "codigo", "") or "error")[:40]}
        resultado = ResultadoAccion(ok, mensaje[:200], accion_tipo=accion.tipo)
        await self._auditar(accion, "escritorio", decision.veredicto, decision.motivo, resultado, extra=extra,
                            destino="drive")

    async def _ejecutar_script_aprobado(self, accion: AccionPC, propuesta: Any, ident: str, sha: str,
                                        decision: DecisionPermiso) -> None:
        resultado_script: Any = None
        try:
            resultado_script = await self.taller.ejecutar(ident, sha)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La ejecución del script falló (%s)", type(exc).__name__)
        codigo = str(getattr(resultado_script, "codigo", "") or "error")[:40]
        corrido = codigo in _CODIGOS_SCRIPT_CORRIDO
        mensaje = str(getattr(resultado_script, "mensaje", "") or "")
        if corrido:
            self._tarjeta_salida(propuesta, resultado_script)
            duracion = getattr(resultado_script, "duracion_s", 0.0)
            if getattr(resultado_script, "tiempo_agotado", False):
                aviso = "Script terminado por tiempo límite."
            else:
                aviso = (f"Script terminado: código {getattr(resultado_script, 'codigo_salida', None)} en "
                         f"{formatear_decimal(float(duracion or 0.0), 1)} s.")
            if codigo == "politica" and mensaje:
                aviso += f" {mensaje}"
            self._aviso_local(aviso, "escritorio")
            reabrir = self._atributo(self.taller, "reabrir")
            nueva = reabrir(ident) if callable(reabrir) else None
            self._reabrir_tarjeta("script", ident, nueva, "")
        else:
            aviso = mensaje or "No pude ejecutar el script."
            self._aviso_local(aviso, "escritorio")
            self._reabrir_tarjeta("script", ident, self._objeto_tarjeta("script", ident), aviso)
        analisis = getattr(propuesta, "analisis", None)
        extra = {"lenguaje": str(getattr(propuesta, "lenguaje", "") or "")[:20],
                 "nivel": str(getattr(analisis, "nivel", "") or "")[:20], "huella12": sha[:12],
                 "codigo_salida": getattr(resultado_script, "codigo_salida", None),
                 "tiempo_agotado": bool(getattr(resultado_script, "tiempo_agotado", False)),
                 "dur_s": round(float(getattr(resultado_script, "duracion_s", 0.0) or 0.0), 3),
                 "bytes_salida": getattr(resultado_script, "bytes_salida", None), "codigo": codigo}
        resultado = ResultadoAccion(bool(getattr(resultado_script, "ok", False)), aviso[:200],
                                    accion_tipo="ejecutar_script")
        await self._auditar(accion, "escritorio", decision.veredicto, decision.motivo, resultado, extra=extra,
                            destino=extra["lenguaje"] or "script")

    def _tarjeta_salida(self, propuesta: Any, resultado: Any) -> None:
        """Tarjeta «salida» (§12.2): stdout y stderr recortados a ``MAX_SALIDA_TARJETA`` unidades UTF-16,
        conservando el principio. Reemplaza a la anterior. Nunca va a Gemini, Telegram ni al log."""
        carpeta = _corto(getattr(resultado, "carpeta", "") or "", 160)
        stdout, recortado_1 = _recortar_utf16(str(getattr(resultado, "stdout", "") or ""), MAX_SALIDA_TARJETA,
                                              f"\n… (salida recortada: completa en {carpeta}/salida.txt)")
        stderr, recortado_2 = _recortar_utf16(str(getattr(resultado, "stderr", "") or ""), MAX_SALIDA_TARJETA,
                                              f"\n… (salida recortada: completa en {carpeta}/errores.txt)")
        codigo_salida = getattr(resultado, "codigo_salida", None)
        if isinstance(codigo_salida, bool) or not isinstance(codigo_salida, int):
            codigo_salida = None
        try:
            duracion = round(float(getattr(resultado, "duracion_s", 0.0) or 0.0), 3)
        except (TypeError, ValueError):
            duracion = 0.0
        if duracion != duracion or duracion in (float("inf"), float("-inf")):
            duracion = 0.0
        script_id = str(getattr(propuesta, "id", "") or "")
        contenido = {"clase": "salida", "script_id": script_id, "codigo_salida": codigo_salida, "duracion_s": duracion}
        sha = huella_contenido(contenido) or hashlib.sha256(
            json.dumps(contenido, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        tiempo_agotado = bool(getattr(resultado, "tiempo_agotado", False))
        resumen = ("Terminado por tiempo límite." if tiempo_agotado
                   else f"Código {codigo_salida} en {formatear_decimal(duracion, 1)} s.")
        canal_origen = str(getattr(propuesta, "canal_origen", "escritorio") or "escritorio")
        ident = os.urandom(4).hex()
        datos = {"id": ident, "clase": "salida", "version": 1, "sha256": sha, "titulo": "Salida del script",
                 "resumen": resumen, "aviso": "", "botones": ["descartar"],
                 "caduca_ts": time.time() + TTL_TARJETA_SALIDA_S,
                 "canal_origen": canal_origen if canal_origen in ("terminal", "escritorio") else "escritorio",
                 "correo": None, "drive": None, "script": None,
                 "salida": {"script_id": script_id, "lenguaje": str(getattr(propuesta, "lenguaje", "") or ""),
                            "codigo_salida": codigo_salida, "tiempo_agotado": tiempo_agotado, "duracion_s": duracion,
                            "stdout": stdout, "stderr": stderr,
                            "recortada": bool(getattr(resultado, "recortada", False)) or recortado_1 or recortado_2,
                            "carpeta": carpeta}}
        for anterior in list(self._tarjetas_info):
            self._tarjetas_info.pop(anterior, None)
            self._publicar_cerrada(anterior, "salida", "reemplazado", "la reemplazó una salida nueva")
        self._tarjetas_info[ident] = {"datos": datos, "caduca": self._reloj() + TTL_TARJETA_SALIDA_S}
        self._publicar_tarjeta(datos)

    def _purgar_tarjetas(self) -> None:
        """Bucle de vigilancia: cierra las tarjetas caducadas (aprobables y «salida»)."""
        purgar = self._atributo(self.permisos, "purgar_caducadas")
        if callable(purgar):
            try:
                vencidas = tuple(purgar())
            except Exception as exc:
                log.debug("purgar_caducadas falló (%s)", type(exc).__name__)
                vencidas = ()
            for t in vencidas:
                self._descartar_en_modulo(t.clase, t.id)
                self._publicar_cerrada(t.id, t.clase, "caducado", "caducó sin aprobarse")
        ahora = self._reloj()
        for ident, info in list(self._tarjetas_info.items()):
            if ahora >= float(info.get("caduca", 0.0)):
                self._tarjetas_info.pop(ident, None)
                self._publicar_cerrada(ident, "salida", "caducado", "caducó")

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
        if canal == "telegram":
            # v0.2.1: lo pedido por Telegram lleva SIEMPRE su canal (la compuerta aplica sus reglas).
            accion = self._accion_por_telegram(accion)
        if accion.tipo == "lanzar_app" and accion.args.get("local") is True:
            return await self._accion_app_local(accion, canal)
        if accion.tipo in TIPOS_ACCION_V02 or (accion.tipo == "media" and self._media_en_control_pc()):
            return await self._accion_pc_v02(accion, canal)
        if self.aura is None:
            resultado = ResultadoAccion(False, "Control del PC no disponible en esta instalación.",
                                        accion_tipo=accion.tipo)
            if canal == "telegram":
                self._anunciar_accion_remota(accion, resultado)
            return resultado, False
        if canal == "telegram":
            return await self._accion_aura_remota(accion)
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

    async def _accion_aura_remota(self, accion: AccionPC) -> tuple[ResultadoAccion, bool]:
        """Acción de aura pedida por Telegram (``lanzar_app`` de RAMS_APPS o la música en Linux).

        aura vuelve a pasar por la compuerta (con ``canal="telegram"``); aquí se audita y se anuncia
        en el PC, como toda acción remota.
        """
        try:
            resultado = await self.aura.ejecutar(accion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("La acción remota %s falló: %s", accion.tipo, type(exc).__name__)
            resultado = ResultadoAccion(False, "La acción falló por un error interno.", accion_tipo=accion.tipo)
        if not isinstance(resultado, ResultadoAccion):
            resultado = ResultadoAccion(False, "La acción no devolvió resultado.", accion_tipo=accion.tipo)
        mensaje = resultado.mensaje or ""
        if resultado.ok:
            veredicto = "permitida"
        elif mensaje.startswith("Acción no autorizada"):
            veredicto = "denegada"
        else:
            veredicto = "fallo"
        await self._auditar(accion, "telegram", veredicto, mensaje[:120], resultado)
        self._anunciar_accion_remota(accion, resultado)
        return resultado, False

    # --- v0.2: acciones de ControlPC (§C2.8, §C2.12) ------------------------------

    async def _accion_pc_v02(self, accion: AccionPC, canal: str) -> tuple[ResultadoAccion, bool]:
        """``abrir_destino``/``buscar_en``/``prerrellenar``/``escribir_texto``/``travesura_cursor``
        (v0.2.1: y ``media`` en Windows, con las teclas multimedia de ControlPC).

        Orden: objetivo de ``escribir_texto`` (ventana previa) → compuerta (una vez) → ControlPC
        → auditoría (``data/acciones_pc.jsonl`` sin el texto) y ``pc.accion``. Lo pedido por
        Telegram se anuncia además en el PC.
        """
        resultado, error_propio = await self._accion_pc_v02_cuerpo(accion, canal)
        if canal == "telegram":
            self._anunciar_accion_remota(accion, resultado)
        return resultado, error_propio

    async def _accion_pc_v02_cuerpo(self, accion: AccionPC, canal: str) -> tuple[ResultadoAccion, bool]:
        """Cuerpo de :meth:`_accion_pc_v02`."""
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
        if tipo == "media":
            return await cp.media(str(args.get("orden") or ""))
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
            nombre_app = accion.args.get("nombre")
            if accion.tipo == "lanzar_app" and isinstance(nombre_app, str) and nombre_app.strip():
                datos["nombre_destino"] = nombre_app.strip()[:60]   # v0.2.1: app del menú Inicio
        if crudos.get("abortada_usuario"):
            datos["abortada_usuario"] = True
        return ResultadoAccion(ok, mensaje, datos, accion_tipo=accion.tipo)

    async def _auditar(self, accion: AccionPC, canal: str, veredicto: str, motivo: str,
                       resultado: ResultadoAccion, *, extra: Mapping[str, Any] | None = None,
                       destino: str | None = None) -> None:
        """Auditoría de las acciones v0.2 (§C2.12): JSONL privado y ``pc.accion``; nunca el texto ni títulos.

        v0.2.2 (§14.5): ``extra`` añade solo metadatos de ``_CAMPOS_AUDITORIA_V022`` (tamaños, conteos,
        códigos y huellas[:12]; nunca cuerpo, direcciones, nombres de archivo, consultas, código ni
        salida) y ``destino`` es ``"gmail"``, ``"drive"``, ``"google"`` o el lenguaje del script.
        """
        args = accion.args
        texto = ""
        if accion.tipo in ("prerrellenar", "escribir_texto"):
            texto = sanear_texto_accion(args.get("texto"))
        elif accion.tipo == "buscar_en":
            texto = str(args.get("consulta") or "")
        exe = _EXE_SEGURO_RE.sub("", str(args.get("exe") or "")).strip()[:60] or None
        clase = _EXE_SEGURO_RE.sub("", str(args.get("clase") or "")).strip()[:60] or None
        # v0.2.1: la "fuente" de una foto (cámara o pantalla) cuenta como destino en la auditoría.
        alias = destino or str(args.get("alias") or args.get("fuente") or args.get("orden") or "")[:40] or None
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
        for clave, valor in (extra or {}).items():
            if clave in _CAMPOS_AUDITORIA_V022 and clave not in registro:
                if isinstance(valor, (str, int, float, bool)) or valor is None:
                    registro[clave] = valor[:80] if isinstance(valor, str) else valor
                elif isinstance(valor, (list, tuple)):
                    registro[clave] = [str(v)[:40] for v in valor][:8]
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
                propio_local = clave in _COMANDOS_MAIN and canal in CANALES_LOCALES
                if (clave in permitidos or propio_local) and clave != "/devolver_llamada":
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

    def _texto_estado_puter(self) -> str:
        """Línea de ``/estado`` del respaldo Puter: disponibilidad, orden en la cadena y contadores.

        «lista» = Broca lo usaría en el próximo turno (app conectada, sesión de Puter, bajo el tope
        diario y sin pausa por fallos); con la app lista pero Broca no, «tope diario alcanzado» o
        «en pausa»; con la app conectada y con sesión pero el saldo de Puter agotado, «en pausa (saldo
        de Puter agotado, reintento en N min)», con la cuenta atrás de ese momento; si no, «en espera».
        Los contadores son los del día de Broca (``PUTER_MAX_CALLS_DAY``: lo paga el usuario) o, si
        Broca no los da, los de la sesión del propio respaldo.
        """
        orden = "antes que Gemini" if getattr(self.settings, "puter_primero", False) else "si Gemini no puede"
        tope = getattr(self.settings, "puter_max_llamadas_dia", None)
        tope = tope if isinstance(tope, int) and not isinstance(tope, bool) and tope > 0 else None
        try:
            app_lista = bool(self.puter.disponible)
            leer_estado = getattr(self.puter, "estado", None)
            estado_app = leer_estado() if callable(leer_estado) else None
            pausa_cuota = 0.0
            if isinstance(estado_app, Mapping) and estado_app.get("conectado") and estado_app.get("listo"):
                restante = estado_app.get("enfriamiento_s")
                if isinstance(restante, (int, float)) and not isinstance(restante, bool) and restante > 0:
                    pausa_cuota = float(restante)
            usable = getattr(self.broca, "respaldo_disponible", None)
            usable = app_lista if usable is None else bool(usable)
            llamadas_hoy = getattr(self.broca, "llamadas_hoy", None)
            hoy = llamadas_hoy() if callable(llamadas_hoy) else None
            if isinstance(hoy, Mapping) and "respaldo" in hoy:
                pedidos = int(hoy.get("respaldo", 0) or 0)
                respondidos = int(hoy.get("respaldo_ok", 0) or 0)
                cuenta = (f"hoy {pedidos} de {tope} pedidos" if tope is not None
                          else f"hoy {_plural(pedidos, 'pedido', 'pedidos')}")
            else:
                cuentas = self.puter.estadisticas()
                pedidos = int(cuentas.get("pedidos", 0) or 0)
                respondidos = int(cuentas.get("respuestas", 0) or 0)
                cuenta = f"en esta sesión {_plural(pedidos, 'pedido', 'pedidos')}"
        except Exception as exc:
            log.debug("Estado del respaldo Puter no disponible: %s", type(exc).__name__)
            return f"IA de respaldo (Puter): sin datos · {orden}"
        if usable:
            situacion = "lista"
        elif not app_lista and pausa_cuota > 0:
            minutos = max(1, int(-(-pausa_cuota // 60)))
            situacion = f"en pausa (saldo de Puter agotado, reintento en {minutos} min)"
        elif not app_lista:
            situacion = "en espera"
        elif tope is not None and pedidos >= tope and cuenta.startswith("hoy"):
            situacion = "tope diario alcanzado"
        else:
            situacion = "en pausa"
        return f"IA de respaldo (Puter): {situacion} · {orden} · {cuenta}, {respondidos} con respuesta"

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
        if self.puter is not None:
            lineas.append(self._texto_estado_puter())
        if self.vision is not None:
            lineas.append(self._texto_estado_vision())
        if self._diagnosticos:
            lineas.append("Módulos:")
            for clave in ("memoria", "conectoma", *_ORDEN_ARRANQUE):
                diag = self._diagnosticos.get(clave)
                if diag is None:
                    continue
                if clave == "puter" and self.puter is not None:
                    diag = self._diagnostico_puter()   # el de ahora, no el último publicado
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
        # v0.2.2: el taller mata el script en curso (Job Object / grupo de procesos).
        await self._cerrar_modulo("taller", self.taller, limite)
        await self._cerrar_modulo("hud", self.hud, limite)
        await self._cerrar_modulo("control", self.control, limite)
        await self._cerrar_modulo("aura", self.aura, limite)
        await self._cerrar_modulo("riko", self.riko, limite)
        await self._cerrar_modulo("web", self.web, limite)
        # v0.2.2: Drive y correo antes que la cuenta de Google (cancela una conexión en curso; no revoca).
        # Las tarjetas viven solo en memoria: se pierden.
        await self._cerrar_modulo("drive", self.drive, limite)
        await self._cerrar_modulo("correo", self.correo, limite)
        await self._cerrar_modulo("google", self.google, limite)
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
                              "interface.hud_server", "lobes.control_pc", "lobes.vision_continua",
                              # v0.2.2
                              "lobes.google_cuenta", "lobes.correo", "lobes.drive", "lobes.taller_scripts")
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
