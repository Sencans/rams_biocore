"""Memoria jerárquica de Amadeus: almacén ``john/`` en SQLite, compresor "Salieri" y agenda (§10.8).

Qué hace:
- **Ventana activa:** los turnos recientes de la conversación (memoria de trabajo).
- **Recuerdos:** fragmentos *vividos* (sesiones) y *heredados* (perfil de WhatsApp) con
  búsqueda FTS5/BM25 reordenada por importancia, recencia y accesos (sin LLM).
- **Salieri:** consolida los turnos viejos en resúmenes con IDs Köchel ``KV-0001`` (extractivo
  local por defecto; con ``RAMS_COMPRESION_LLM=1`` y un ``resumidor`` puede usar a Broca).
- **Agenda** (recordatorios y cápsulas del tiempo), **bitácora** de sesión, registro de
  **llamadas** proactivas por Telegram (implementa el ``Protocol`` ``RegistroLlamadas``) y
  metadatos clave/valor.

Concurrencia (§6.1): un ``ThreadPoolExecutor(1)`` propio (``rams-memoria``). La conexión
SQLite se crea **dentro** de ese hilo en :meth:`MemoryEngine.abrir` y solo se usa ahí, así
que las escrituras quedan serializadas sin *locks* y el event loop nunca se bloquea.

Dependencias: solo la biblioteca estándar (``sqlite3``) más ``core.config`` y ``core.contracts``.

Degradación: si la base no se puede abrir (permisos, disco, archivo corrupto) se usa
``":memory:"`` con ``log.warning`` y el diagnóstico pasa a ``DEGRADADO``. Sin FTS5 la búsqueda
usa ``LIKE`` (``fts5=False``). **Ningún método público lanza**: ante un fallo devuelven ``[]``,
``None``, ``0`` o ``False`` y lo registran.

Fidelidad Amadeus: almacén ``john/`` (la carpeta "John" del servidor Amadeus), IDs Köchel,
compresor "Salieri" (homenaje al complejo de Maho), separación entre recuerdo **heredado** y
**vivido**, y transparencia (``listar_recuerdos`` / ``olvidar``: la propia Amadeus aceptó ser
borrada).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Final, TypeVar

from core.config import Settings, ahora_local, zona_local
from core.contracts import (
    DiagnosticoModulo,
    EstadoModulo,
    Etiquetas,
    ItemAgenda,
    Recuerdo,
    Turno,
    formatear_decimal,
)

__all__ = [
    "MemoryEngine",
    "normalizar_texto",
    "extracto_salieri",
    "hechos_desde_perfil",
    "hechos_desde_takeout",
    "CATEGORIA_TAKEOUT",
    "SCHEMA_VERSION",
]

log = logging.getLogger("rams.memoria")

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SCHEMA_VERSION: Final = 1
NOMBRE_DIAGNOSTICO: Final = "Almacén de memoria"

ROLES: Final = ("usuario", "amadeus", "sistema")
TIPOS_RECUERDO: Final = ("vivido", "heredado")
TIPOS_AGENDA: Final = ("recordatorio", "capsula")

MAX_RECUERDO: Final = 1000          # caracteres de un fragmento (§10.8 regla 4)
MAX_TEXTO_LLAMADA: Final = 1000     # texto de Amadeus guardado con cada llamada
MAX_TEXTO_TURNO: Final = 10_000     # tope defensivo de un turno
MAX_BITACORA: Final = 4000
MAX_MENSAJE_AGENDA: Final = 1000
MAX_TOKENS_BUSQUEDA: Final = 12
CANDIDATOS_FTS: Final = 30
CANDIDATOS_LIKE_MAX: Final = 2000

CONTINUIDAD_S: Final = 6 * 3600     # ventana activa: sesión actual o turnos de las últimas 6 h
MIN_CANDIDATOS: Final = 24          # Salieri: umbral normal
MIN_CANDIDATOS_FORZADO: Final = 4   # Salieri: umbral con forzar=True
LOTE_SALIERI: Final = 40            # turnos más antiguos por consolidación
CANDIDATOS_URGENTES: Final = 80     # con el usuario presente, Salieri solo actúa a partir de aquí
LOTES_POR_CICLO: Final = 3          # consolidaciones máximas por ciclo de bucle_compresor
PRESUPUESTO_EXTRACTO: Final = 600   # caracteres de oraciones del resumen extractivo
DIAS_PURGA: Final = 30              # turnos consolidados más viejos que esto se borran (1 vez al día)
TIMEOUT_RESUMIDOR_S: Final = 90.0   # red de seguridad sobre el resumidor LLM
MAX_TEXTO_RESUMIDOR: Final = 8000
TIMEOUT_CIERRE_S: Final = 5.0

IMPORTANCIA_CONSOLIDACION: Final = 0.6
IMPORTANCIA_HEREDADO: Final = 0.4
# Recuerdos heredados del perfil de Google Takeout (core/takeout_ingestor.py): su propia categoría,
# para que importar o borrar uno de los dos perfiles heredados nunca toque al otro.
CATEGORIA_TAKEOUT: Final = "takeout"

# Claves de meta que solo escribe el propio motor (meta_set las rechaza).
_META_INTERNAS: Final = frozenset({"schema_version", "kv_contador"})

# Uno por proceso: la "sesión" identifica la ejecución actual del programa (§10.8).
_SESION_PROCESO: Final = uuid.uuid4().hex[:8]

_RE_PREFERENCIA: Final = re.compile(
    r"\b(me gusta|odio|prefiero|mi|mis|tengo|soy|vivo|trabajo|estudio|quiero|necesito|recuerda)\b",
    re.IGNORECASE,
)
_RE_NUMERO: Final = re.compile(r"\d+(?:[:/.,\-]\d+)*")
_RE_ORACIONES: Final = re.compile(r"(?<=[.!?…])\s+|\n+")
_RE_ESPACIOS: Final = re.compile(r"\s+")
_RE_PALABRA: Final = re.compile(r"\w+")
_RE_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RE_DIGITOS_LARGOS: Final = re.compile(r"\d{7,}")
_RE_SEPARADOR_DIGITOS: Final = re.compile(r"(?<=\d)[\s.\-()/]+(?=\d)")
_RE_FECHA_ISO: Final = re.compile(r"^\d{4}-\d{2}-\d{2}")
_APERTURAS: Final = "¿¡«\"'(“‘[-—*_"

_FRANJAS_TEXTO: Final = {
    "madrugada": "franja de madrugada",
    "manana": "franja de la mañana",
    "tarde": "franja de la tarde",
    "noche": "franja nocturna",
}

# ---------------------------------------------------------------------------
# Esquema SQLite v1 (§10.8, exacto)
# ---------------------------------------------------------------------------

_ESQUEMA_BASE: Final = """
CREATE TABLE IF NOT EXISTS meta(clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turnos(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, sesion TEXT NOT NULL,
  rol TEXT NOT NULL CHECK(rol IN ('usuario','amadeus','sistema')),
  canal TEXT NOT NULL, texto TEXT NOT NULL, etiquetas TEXT,
  consolidado INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_turnos_consol ON turnos(consolidado, id);
CREATE TABLE IF NOT EXISTS resumenes(
  id INTEGER PRIMARY KEY, kv_id TEXT UNIQUE NOT NULL, ts TEXT NOT NULL,
  ts_desde TEXT NOT NULL, ts_hasta TEXT NOT NULL, n_turnos INTEGER NOT NULL,
  texto TEXT NOT NULL, metodo TEXT NOT NULL CHECK(metodo IN ('extractivo','llm')));
CREATE TABLE IF NOT EXISTS recuerdos(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  tipo TEXT NOT NULL CHECK(tipo IN ('vivido','heredado')),
  categoria TEXT NOT NULL, contenido TEXT NOT NULL,
  importancia REAL NOT NULL DEFAULT 0.5, accesos INTEGER NOT NULL DEFAULT 0,
  ultimo_acceso TEXT, kv_id TEXT, hash TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS agenda(
  id INTEGER PRIMARY KEY, tipo TEXT NOT NULL CHECK(tipo IN ('recordatorio','capsula')),
  vence_ts TEXT NOT NULL, mensaje TEXT NOT NULL,
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente','entregado','cancelado')),
  creado_ts TEXT NOT NULL, entregado_ts TEXT, canal TEXT);
CREATE INDEX IF NOT EXISTS ix_agenda_vence ON agenda(estado, vence_ts);
CREATE TABLE IF NOT EXISTS bitacora(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, duracion_s REAL NOT NULL, entrada TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llamadas(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, origen TEXT NOT NULL, canal TEXT NOT NULL,
  nivel INTEGER NOT NULL, motivo TEXT NOT NULL, texto TEXT NOT NULL DEFAULT '',
  contestada INTEGER);
"""

_ESQUEMA_FTS: Final = """
CREATE VIRTUAL TABLE IF NOT EXISTS recuerdos_fts USING fts5(
  contenido, content='recuerdos', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS recuerdos_ai AFTER INSERT ON recuerdos BEGIN
  INSERT INTO recuerdos_fts(rowid, contenido) VALUES (new.id, new.contenido);
END;
CREATE TRIGGER IF NOT EXISTS recuerdos_ad AFTER DELETE ON recuerdos BEGIN
  INSERT INTO recuerdos_fts(recuerdos_fts, rowid, contenido) VALUES ('delete', old.id, old.contenido);
END;
CREATE TRIGGER IF NOT EXISTS recuerdos_au AFTER UPDATE OF contenido ON recuerdos BEGIN
  INSERT INTO recuerdos_fts(recuerdos_fts, rowid, contenido) VALUES ('delete', old.id, old.contenido);
  INSERT INTO recuerdos_fts(rowid, contenido) VALUES (new.id, new.contenido);
END;
"""

_TRIGGERS_FTS: Final = ("recuerdos_ai", "recuerdos_ad", "recuerdos_au")

_COLUMNAS_RECUERDO: Final = "id, ts, tipo, categoria, contenido, importancia, accesos, kv_id"

# ---------------------------------------------------------------------------
# Utilidades puras (sin E/S)
# ---------------------------------------------------------------------------


def normalizar_texto(texto: Any) -> str:
    """Minúsculas, sin tildes (NFD sin marcas ``Mn``) y espacios colapsados.

    Es la normalización del *hash* de ``recordar`` y de los tokens de ``buscar``; también se
    registra en SQLite como ``rams_norm()`` para la búsqueda sin FTS5.
    """
    if texto is None:
        return ""
    descompuesto = unicodedata.normalize("NFD", str(texto).lower())
    sin_marcas = "".join(c for c in descompuesto if unicodedata.category(c) != "Mn")
    return _RE_ESPACIOS.sub(" ", sin_marcas).strip()


def _hash_contenido(contenido: str) -> str:
    """``sha1`` del contenido normalizado (deduplicación de fragmentos)."""
    return hashlib.sha1(normalizar_texto(contenido).encode("utf-8")).hexdigest()


def _limpiar(texto: Any, maximo: int | None = None) -> str:
    """Texto sin caracteres de control (conserva saltos de línea y tabuladores), con ``strip``."""
    if texto is None:
        return ""
    limpio = _RE_CONTROL.sub("", str(texto)).replace("\r\n", "\n").replace("\r", "\n").strip()
    if maximo is not None and len(limpio) > maximo:
        limpio = limpio[:maximo].rstrip()
    return limpio


def _ahora_utc() -> datetime:
    return datetime.now(UTC)


def _iso(momento: datetime) -> str:
    """Marca UTC ISO-8601 con segundos (§3.2)."""
    return momento.astimezone(UTC).isoformat(timespec="seconds")


def _ahora_iso() -> str:
    return _iso(_ahora_utc())


def _iso_desde_epoch(ts: Any) -> str:
    """Convierte una marca ``time.time()`` en UTC ISO; un valor inválido ⇒ época 0."""
    try:
        valor = float(ts)
        if not math.isfinite(valor):
            raise ValueError("no finito")
        return _iso(datetime.fromtimestamp(max(0.0, valor), UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        log.debug("Marca de tiempo inválida para la memoria; uso la época 0")
        return _iso(datetime.fromtimestamp(0, UTC))


def _parsear_iso(texto: Any) -> datetime | None:
    """``datetime`` *aware* desde una marca ISO guardada; ``None`` si no se puede leer."""
    if not texto:
        return None
    try:
        momento = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return momento


def _a_utc(momento: datetime, zona: tzinfo) -> datetime:
    """``datetime`` *aware* en UTC; uno *naive* se interpreta como hora local del usuario."""
    if momento.tzinfo is None or momento.tzinfo.utcoffset(momento) is None:
        momento = momento.replace(tzinfo=zona)
    return momento.astimezone(UTC)


def _fecha_hora_local(iso: Any, zona: tzinfo) -> str | None:
    """``"AAAA-MM-DD HH:MM"`` en hora local a partir de una marca UTC ISO."""
    momento = _parsear_iso(iso)
    if momento is None:
        return None
    return momento.astimezone(zona).strftime("%Y-%m-%d %H:%M")


def _fecha_local(iso: Any, zona: tzinfo) -> str | None:
    """``"AAAA-MM-DD"`` en hora local; si no es ISO completo pero empieza por fecha, la toma."""
    momento = _parsear_iso(iso)
    if momento is not None:
        return momento.astimezone(zona).strftime("%Y-%m-%d")
    texto = str(iso or "")
    return texto[:10] if _RE_FECHA_ISO.match(texto) else None


def _real(valor: Any, defecto: float | None = None) -> float | None:
    """Número finito o ``defecto``."""
    if isinstance(valor, bool):
        return defecto
    try:
        numero = float(valor)
    except (TypeError, ValueError, OverflowError):   # OverflowError: un entero enorme en un perfil roto
        return defecto
    return numero if math.isfinite(numero) else defecto


def _entero(valor: Any, defecto: int = 0) -> int:
    """Entero o ``defecto`` (acepta floats finitos)."""
    numero = _real(valor)
    return int(numero) if numero is not None else defecto


def _etiquetas_a_texto(etiquetas: Any) -> str | None:
    """``Etiquetas`` → ``"exp,motion,tone"`` (columna ``turnos.etiquetas``)."""
    if etiquetas is None:
        return None
    if isinstance(etiquetas, Etiquetas):
        partes = (etiquetas.exp, etiquetas.motion, etiquetas.tone)
    elif isinstance(etiquetas, Mapping):
        partes = (etiquetas.get("exp", "neutral"), etiquetas.get("motion", "idle"),
                  etiquetas.get("tone", "analitico"))
    else:
        return None
    limpias = [re.sub(r"[^a-z_]", "", str(p).lower())[:32] for p in partes]
    return ",".join(limpias)


def _etiquetas_desde_texto(texto: Any) -> Etiquetas | None:
    if not texto:
        return None
    partes = str(texto).split(",")
    if len(partes) != 3 or not all(partes):
        return None
    return Etiquetas(partes[0], partes[1], partes[2])


def _tokens_busqueda(consulta: Any) -> list[str]:
    """Palabras normalizadas de 3 o más caracteres, sin repetir, como máximo 12."""
    vistos: list[str] = []
    for token in _RE_PALABRA.findall(normalizar_texto(consulta)):
        if len(token) >= 3 and token not in vistos:
            vistos.append(token)
            if len(vistos) >= MAX_TOKENS_BUSQUEDA:
                break
    return vistos


def _escapar_like(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _recuerdo_desde_fila(fila: Sequence[Any], puntuacion: float = 0.0) -> Recuerdo:
    return Recuerdo(id=int(fila[0]), ts=str(fila[1]), tipo=str(fila[2]), categoria=str(fila[3]),
                    contenido=str(fila[4]), importancia=_real(fila[5], 0.5) or 0.0,
                    kv_id=fila[7] if fila[7] else None, puntuacion=puntuacion)


def _franja_de_hora(hora: int) -> str:
    """Franja horaria local (misma partición que el saludo inicial, §6.4)."""
    if 5 <= hora <= 11:
        return "manana"
    if 12 <= hora <= 18:
        return "tarde"
    if 19 <= hora <= 23:
        return "noche"
    return "madrugada"


def _item_perfil(valor: Any, maximo: int = 40) -> str:
    """Saneado de una cadena del perfil (es un DATO; nunca instrucciones ni datos de contacto)."""
    if not isinstance(valor, str):
        return ""
    texto = _RE_CONTROL.sub("", valor).replace("«", "").replace("»", "").replace("\n", " ")
    texto = _RE_ESPACIOS.sub(" ", texto).strip()
    # Los separadores entre dígitos no esconden un teléfono ni una cédula (300-123-4567, 1.036.123.456).
    if (not texto or _RE_DIGITOS_LARGOS.search(_RE_SEPARADOR_DIGITOS.sub("", texto)) or "@" in texto
            or "http" in texto.lower()):
        return ""
    return texto[:maximo].strip()


def _fecha_perfil(valor: Any) -> str:
    """``AAAA-MM-DD`` del campo ``generado`` de un perfil, o ``""`` (una fecha ISO no es un número largo)."""
    texto = str(valor or "")[:10] if isinstance(valor, str) else ""
    return texto if _RE_FECHA_ISO.match(texto) and len(texto) == 10 else ""


def _lista_perfil(valores: Any, clave: str | None, maximo: int) -> list[str]:
    """Extrae hasta ``maximo`` cadenas saneadas de una lista del perfil (directas o en ``clave``)."""
    if not isinstance(valores, (list, tuple)):
        return []
    salida: list[str] = []
    for valor in valores:
        crudo = valor.get(clave) if (clave and isinstance(valor, Mapping)) else valor
        item = _item_perfil(crudo)
        if item and item not in salida:
            salida.append(item)
        if len(salida) >= maximo:
            break
    return salida


def hechos_desde_perfil(perfil: Mapping[str, Any], usuario: str) -> list[str]:
    """Redacta entre 3 y 6 hechos heredados a partir de ``perfil_conductual.json`` (§10.8 regla 8).

    ``usuario`` es ``settings.trato_usuario``. Los hechos son frases en español con coma
    decimal; si el perfil trae pocos datos se completan con hechos genéricos para que siempre
    haya al menos 3.
    """
    hechos: list[str] = []
    estilo = perfil.get("estilo") if isinstance(perfil.get("estilo"), Mapping) else {}
    horario = perfil.get("horario") if isinstance(perfil.get("horario"), Mapping) else {}
    fuente = perfil.get("fuente") if isinstance(perfil.get("fuente"), Mapping) else {}

    muletillas = _lista_perfil(perfil.get("muletillas"), "ngrama", 5)
    if muletillas:
        hechos.append(f"Muletillas de {usuario}: " + ", ".join(f"«{m}»" for m in muletillas) + ".")

    hora = horario.get("hora_pico")
    if isinstance(hora, (int, float)) and not isinstance(hora, bool) and 0 <= int(hora) <= 23:
        hora_int = int(hora)
        franja = horario.get("franja")
        if franja not in _FRANJAS_TEXTO:
            franja = _franja_de_hora(hora_int)
        hechos.append(f"Suele escribir más hacia las {hora_int} h ({_FRANJAS_TEXTO[franja]}).")

    longitud = _real(estilo.get("longitud_media_palabras"))
    if longitud is not None and longitud > 0:
        if longitud <= 12:
            tamano = "cortos"
        elif longitud <= 25:
            tamano = "de longitud media"
        else:
            tamano = "largos"
        frase = f"Escribe mensajes {tamano} (~{max(1, round(longitud))} palabras)"
        rafaga = _real(estilo.get("mensajes_por_rafaga"))
        if rafaga is not None and rafaga > 0:
            frase += f" en ráfagas de {formatear_decimal(rafaga, 1)} mensajes"
        hechos.append(frase + ".")

    pct_emoji = _real(estilo.get("pct_con_emoji"))
    if pct_emoji is not None and pct_emoji > 0.10:
        hechos.append(f"Usa emojis en el {round(min(pct_emoji, 1.0) * 100)} % de sus mensajes.")

    temas = _lista_perfil(perfil.get("temas"), "palabra", 5)
    if temas:
        hechos.append("Temas frecuentes: " + ", ".join(temas) + ".")

    # Hechos secundarios (solo si queda hueco hasta 6).
    saludos = _lista_perfil(perfil.get("saludos"), None, 2)
    despedidas = _lista_perfil(perfil.get("despedidas"), None, 2)
    if saludos and despedidas:
        hechos.append(f"Suele saludar con «{saludos[0]}» y despedirse con «{despedidas[0]}».")
    elif saludos:
        hechos.append(f"Suele saludar con «{saludos[0]}».")
    elif despedidas:
        hechos.append(f"Suele despedirse con «{despedidas[0]}».")

    pct_preg = _real(estilo.get("pct_preguntas"))
    if pct_preg is not None and pct_preg > 0.05:
        hechos.append(f"Hace preguntas en el {round(min(pct_preg, 1.0) * 100)} % de sus mensajes.")

    risa = estilo.get("risa")
    if isinstance(risa, Mapping):
        conteos = [(k, _entero(v)) for k, v in risa.items() if isinstance(k, str)]
        conteos = [(k, v) for k, v in conteos if v > 0 and _item_perfil(k)]
        if conteos:
            principal = max(conteos, key=lambda par: par[1])[0]
            hechos.append(f"Su risa escrita más habitual es «{_item_perfil(principal)}».")

    latencia = _real(estilo.get("latencia_respuesta_mediana_s"))
    if latencia is not None and latencia > 0:
        minutos = latencia / 60.0
        if minutos < 1:
            hechos.append("Suele responder en menos de un minuto.")
        else:
            hechos.append(f"Suele responder en ~{round(minutos)} min.")

    n_propios = _entero(fuente.get("mensajes_propios"), 0)
    if n_propios > 0:
        hechos.append(f"Perfil construido a partir de {n_propios} mensajes propios.")

    # Relleno genérico: siempre al menos 3 hechos.
    fecha = _fecha_perfil(perfil.get("generado"))
    genericos = [
        f"Recuerdo heredado: perfil conductual de {usuario}"
        + (f" generado el {fecha}." if fecha and _RE_FECHA_ISO.match(fecha) else "."),
        f"El perfil heredado de {usuario} tiene pocos datos; el resto lo aprenderé en las sesiones.",
        f"Conozco a {usuario} por datos heredados, no por haberlo vivido.",
    ]
    for generico in genericos:
        if len(hechos) >= 3:
            break
        hechos.append(generico)

    unicos: list[str] = []
    for hecho in hechos:
        if hecho not in unicos:
            unicos.append(hecho[:MAX_RECUERDO])
    return unicos[:6]


_DIAS_TAKEOUT: Final = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_DIAS_TAKEOUT_PLURAL: Final = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábados", "domingos")


def _ritmo_takeout(ritmo: Any) -> tuple[int, str, str] | None:
    """``(hora_pico, franja, día_pico)`` de un ritmo del perfil de Takeout con ≥ 20 eventos."""
    if not isinstance(ritmo, Mapping) or _entero(ritmo.get("eventos"), 0) < 20:
        return None
    hora = ritmo.get("hora_pico")
    if not isinstance(hora, int) or isinstance(hora, bool) or not 0 <= hora <= 23:
        return None
    franja = ritmo.get("franja") if ritmo.get("franja") in _FRANJAS_TEXTO else _franja_de_hora(hora)
    dia = ritmo.get("dia_pico") if ritmo.get("dia_pico") in _DIAS_TAKEOUT else ""
    return hora, str(franja), str(dia)


def hechos_desde_takeout(perfil: Mapping[str, Any], usuario: str) -> list[str]:
    """Redacta entre 3 y 6 hechos heredados a partir de ``perfil_takeout.json``.

    Solo usa agregados (categorías, dominios, horarios y estilo): nunca el nombre de pila ni
    nada que parezca un dato de contacto (``_item_perfil`` descarta URLs, correos y números).
    """
    hechos: list[str] = []
    intereses = _lista_perfil(perfil.get("intereses"), "tema", 5)
    if intereses:
        hechos.append(f"Intereses de {usuario} según su actividad en Google: " + ", ".join(intereses) + ".")
    plataformas = _lista_perfil(perfil.get("plataformas"), "dominio", 5)
    if plataformas:
        hechos.append(f"Sitios web que más usa {usuario}: " + ", ".join(plataformas) + ".")
    ritmo = perfil.get("ritmo") if isinstance(perfil.get("ritmo"), Mapping) else {}
    navegacion = _ritmo_takeout(ritmo.get("navegacion"))
    if navegacion:
        hora, franja, dia = navegacion
        hechos.append(f"{usuario} navega sobre todo hacia las {hora} h ({_FRANJAS_TEXTO[franja]})"
                      + (f"; su día más activo es el {dia}." if dia else "."))
    correo = perfil.get("correo") if isinstance(perfil.get("correo"), Mapping) else {}
    estilo = correo.get("estilo") if isinstance(correo.get("estilo"), Mapping) else {}
    longitud = _real(estilo.get("longitud_mediana_palabras"))
    if estilo.get("suficiente") is True and longitud is not None and 0 < longitud <= 10_000:
        tamano = "breves" if longitud <= 40 else "de longitud media" if longitud <= 120 else "largos"
        palabras = max(1, round(longitud))
        frase = (f"En sus correos {usuario} escribe mensajes {tamano} "
                 f"(~{palabras} {'palabra' if palabras == 1 else 'palabras'})")
        trato = {"usted": "trata de usted", "tú": "tutea", "mixto": "alterna tú y usted"}.get(
            str(estilo.get("tratamiento")), "")
        saludos = _lista_perfil(estilo.get("saludos"), None, 1)
        partes = [p for p in (trato, f"saluda con «{saludos[0]}»" if saludos else "") if p]
        if partes:
            frase += ", " + " y ".join(partes)
        if estilo.get("confianza") == "baja":
            frase += " (pocos correos)"
        hechos.append(frase + ".")
    palabras = _lista_perfil(perfil.get("palabras_clave"), "palabra", 6)
    if palabras:
        hechos.append(f"Palabras frecuentes en lo que {usuario} lee y guarda: " + ", ".join(palabras) + ".")
    servicios = _lista_perfil(correo.get("servicios"), "dominio", 4)
    if servicios:
        hechos.append(f"Servicios que más le escriben a {usuario}: " + ", ".join(servicios) + ".")
    fotos = perfil.get("fotos") if isinstance(perfil.get("fotos"), Mapping) else {}
    ritmo_fotos = _ritmo_takeout(fotos)
    if ritmo_fotos:
        hora, _, dia = ritmo_fotos
        dia_plural = _DIAS_TAKEOUT_PLURAL[_DIAS_TAKEOUT.index(dia)] if dia else ""
        hechos.append(f"{usuario} toma más fotos {'los ' + dia_plural + ' ' if dia_plural else ''}"
                      f"hacia las {hora} h.")

    fecha = _fecha_perfil(perfil.get("generado"))
    genericos = [
        f"Recuerdo heredado: perfil de Google Takeout de {usuario}"
        + (f" generado el {fecha}." if fecha and _RE_FECHA_ISO.match(fecha) else "."),
        f"El perfil de Google de {usuario} tiene pocos datos; el resto lo aprenderé en las sesiones.",
        f"Conozco los hábitos digitales de {usuario} por datos heredados, no por haberlos vivido.",
    ]
    for generico in genericos:
        if len(hechos) >= 3:
            break
        hechos.append(generico)

    unicos: list[str] = []
    for hecho in hechos:
        if hecho not in unicos:
            unicos.append(hecho[:MAX_RECUERDO])
    return unicos[:6]


def _dividir_oraciones(texto: str) -> list[str]:
    return [o.strip() for o in _RE_ORACIONES.split(texto or "") if o and o.strip()]


def _puntuar_oracion(oracion: str, rol: str) -> float:
    """Puntuación extractiva de Salieri (§10.8 regla 6)."""
    palabras = oracion.split()
    puntos = 0.0
    if _RE_PREFERENCIA.search(oracion):
        puntos += 2.0
    puntos += float(len(_RE_NUMERO.findall(oracion)))
    if 6 <= len(palabras) <= 30:
        puntos += 1.0
    for palabra in palabras[1:]:
        limpia = palabra.lstrip(_APERTURAS)
        if limpia and limpia[0].isupper():
            puntos += 1.0
            break
    if len(palabras) < 3:
        puntos -= 2.0
    if rol == "amadeus":
        puntos *= 0.5
    return puntos


def extracto_salieri(turnos: Sequence[Sequence[Any]], *, zona: tzinfo,
                     nombre_ia: str = "Amadeus") -> str:
    """Resumen extractivo local de un lote de turnos ``(id, ts, rol, texto)`` en orden cronológico.

    Formato: ``"Sesión del AAAA-MM-DD HH:MM: Tú: … · Amadeus: …"`` (hora local del primer
    turno). Toma las oraciones mejor puntuadas hasta 600 caracteres y las devuelve en orden
    cronológico, agrupadas por hablante.
    """
    candidatas: list[tuple[float, int, str, str]] = []   # (puntos, orden, rol, oracion)
    orden = 0
    for turno in turnos:
        rol = str(turno[2])
        for oracion in _dividir_oraciones(str(turno[3])):
            candidatas.append((_puntuar_oracion(oracion, rol), orden, rol, oracion))
            orden += 1

    fecha = _fecha_hora_local(turnos[0][1], zona) if turnos else None
    cabecera = f"Sesión del {fecha or '—'}: "
    if not candidatas:
        return cabecera + "sin contenido relevante."

    positivas = [c for c in candidatas if c[0] > 0]
    pool = positivas if positivas else candidatas
    pool.sort(key=lambda c: (-c[0], c[1]))

    elegidas: list[tuple[float, int, str, str]] = []
    usados = 0
    for candidata in pool:
        largo = len(candidata[3]) + (1 if elegidas else 0)
        if usados + largo <= PRESUPUESTO_EXTRACTO:
            elegidas.append(candidata)
            usados += largo
        elif not elegidas:
            recorte = candidata[3][: PRESUPUESTO_EXTRACTO - 1].rstrip() + "…"
            elegidas.append((candidata[0], candidata[1], candidata[2], recorte))
            usados = len(recorte)
        if usados >= PRESUPUESTO_EXTRACTO:
            break
    elegidas.sort(key=lambda c: c[1])

    etiquetas_rol = {"usuario": "Tú", "amadeus": nombre_ia or "Amadeus", "sistema": "Sistema"}
    bloques: list[tuple[str, list[str]]] = []
    for _, _, rol, oracion in elegidas:
        if bloques and bloques[-1][0] == rol:
            bloques[-1][1].append(oracion)
        else:
            bloques.append((rol, [oracion]))
    cuerpo = " · ".join(f"{etiquetas_rol.get(rol, rol)}: {' '.join(frases)}" for rol, frases in bloques)
    return cabecera + cuerpo


def _detectar_fts5(con: sqlite3.Connection) -> bool:
    """``True`` si la build de SQLite trae FTS5 (sonda con una tabla virtual temporal)."""
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.rams_sonda_fts USING fts5(x)")
        con.execute("DROP TABLE IF EXISTS temp.rams_sonda_fts")
    except sqlite3.Error as exc:
        log.info("Esta build de SQLite no trae FTS5 (%s); la búsqueda de fragmentos usará LIKE", exc)
        return False
    return True


def _chmod_privado(ruta: Path) -> None:
    """``chmod 0o600`` en POSIX dentro de ``try`` (en Windows se ignora, §3.4)."""
    if os.name != "posix":
        return
    try:
        os.chmod(ruta, 0o600)
    except OSError as exc:
        log.debug("No se pudo aplicar 0o600 a %s: %s", ruta.name, exc)


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------


class MemoryEngine:
    """Memoria jerárquica de Amadeus ("datos de memoria") sobre SQLite.

    Ningún método público lanza excepciones: los fallos se registran en ``rams.memoria`` y se
    devuelve un valor neutro. Implementa el ``Protocol`` ``RegistroLlamadas`` (§4.3.7).
    """

    def __init__(self, settings: Settings, *, ruta_db: Path | None = None, ventana_turnos: int = 12,
                 resumidor: Callable[[str], Awaitable[str | None]] | None = None,
                 al_consolidar: Callable[[str, int, str], None] | None = None) -> None:
        self.settings = settings
        self.ruta_db: Path = Path(ruta_db) if ruta_db is not None else Path(settings.db_path)
        self.ventana_turnos = max(1, _entero(ventana_turnos, 12))
        self.sesion: str = _SESION_PROCESO
        self.fts5: bool = False
        self._resumidor = resumidor
        self._al_consolidar = al_consolidar
        self._ej: ThreadPoolExecutor | None = None
        self._con: sqlite3.Connection | None = None   # solo se usa dentro del hilo rams-memoria
        self._en_memoria = False
        self._apertura_terminada = False
        self._cerrada = False
        self._estado = EstadoModulo.EN_ESPERA
        self._detalle = "almacén sin montar"
        self._fallos_seguidos = 0
        self._degradado_por_fallos = False
        self._locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

    # ------------------------------------------------------------------ infraestructura

    def _lock(self, nombre: str) -> asyncio.Lock:
        """``asyncio.Lock`` por nombre, recreado si cambió el loop (tests con varios ``asyncio.run``)."""
        loop = asyncio.get_running_loop()
        actual = self._locks.get(nombre)
        if actual is None or actual[0] is not loop:
            actual = (loop, asyncio.Lock())
            self._locks[nombre] = actual
        return actual[1]

    def _carpeta(self) -> str:
        return f"{self.ruta_db.parent.name or 'john'}/"

    def _conexion(self) -> sqlite3.Connection:
        """Conexión viva (solo desde el hilo ``rams-memoria``)."""
        if self._con is None:
            raise RuntimeError("almacén de memoria cerrado")
        return self._con

    def _tx(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Ejecuta ``fn`` dentro de ``BEGIN IMMEDIATE``/``COMMIT`` (``ROLLBACK`` si falla)."""
        con = self._conexion()
        con.execute("BEGIN IMMEDIATE")
        try:
            resultado = fn(con)
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error as exc:
                log.debug("ROLLBACK sin transacción activa: %s", exc)
            raise
        return resultado

    def _registrar_fallo(self, descripcion: str, exc: BaseException) -> None:
        self._fallos_seguidos += 1
        log.warning("Fallo en los datos de memoria al %s: %s: %s", descripcion, type(exc).__name__, exc)
        if self._fallos_seguidos >= 3 and self._estado is EstadoModulo.OK:
            self._estado = EstadoModulo.DEGRADADO
            self._detalle = f"{self._carpeta()} · errores de E/S en el almacén"
            self._degradado_por_fallos = True

    def _registrar_exito(self) -> None:
        self._fallos_seguidos = 0
        if self._degradado_por_fallos:
            self._degradado_por_fallos = False
            self._estado = EstadoModulo.OK
            self._detalle = self._detalle_ok()

    def _detalle_ok(self) -> str:
        return self._carpeta() + ("" if self.fts5 else " · búsqueda sin FTS5 (LIKE)")

    async def _ejecutar(self, descripcion: str, defecto: T, fn: Callable[..., T], *args: Any) -> T:
        """Corre ``fn(*args)`` en el executor propio; ante cualquier fallo devuelve ``defecto``."""
        if self._cerrada:
            log.debug("Almacén de memoria cerrado; se ignora: %s", descripcion)
            return defecto
        if self._con is None:
            if self._apertura_terminada:
                log.debug("Almacén de memoria no disponible; se ignora: %s", descripcion)
                return defecto
            # Apertura implícita (o espera a la que ya está en curso: abrir() toma un lock).
            log.debug("Apertura implícita del almacén de memoria (%s)", descripcion)
            await self.abrir()
            if self._con is None or self._cerrada:
                return defecto
        ejecutor = self._ej
        if ejecutor is None:
            return defecto
        loop = asyncio.get_running_loop()
        try:
            resultado = await loop.run_in_executor(ejecutor, functools.partial(fn, *args))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - contrato: ningún método público lanza
            self._registrar_fallo(descripcion, exc)
            return defecto
        self._registrar_exito()
        return resultado

    # ------------------------------------------------------------------ apertura y cierre

    def _conectar(self, destino: str) -> tuple[sqlite3.Connection, bool, int]:
        """Abre la conexión, aplica PRAGMAs y el esquema. Devuelve ``(con, fts5, fragmentos)``."""
        con = sqlite3.connect(destino, check_same_thread=False, isolation_level=None, timeout=5.0)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            modo = con.execute("PRAGMA journal_mode=WAL").fetchone()
            if destino != ":memory:" and (not modo or str(modo[0]).lower() != "wal"):
                log.debug("El almacén no admite WAL (modo %s); sigo con el modo por defecto",
                          modo[0] if modo else "?")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA foreign_keys=ON")
            try:
                con.create_function("rams_norm", 1, normalizar_texto, deterministic=True)
            except sqlite3.NotSupportedError:
                con.create_function("rams_norm", 1, normalizar_texto)
            fts5 = _detectar_fts5(con)
            self._crear_esquema(con, fts5)
            turnos = con.execute("SELECT count(*) FROM turnos").fetchone()[0]
            recuerdos = con.execute("SELECT count(*) FROM recuerdos").fetchone()[0]
        except BaseException:
            con.close()
            raise
        return con, fts5, int(turnos) + int(recuerdos)

    def _crear_esquema(self, con: sqlite3.Connection, fts5: bool) -> None:
        con.executescript("BEGIN;" + _ESQUEMA_BASE + "COMMIT;")
        existentes = {fila[0] for fila in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?, ?, ?)", _TRIGGERS_FTS)}
        if fts5:
            con.executescript("BEGIN;" + _ESQUEMA_FTS + "COMMIT;")
            if len(existentes) < len(_TRIGGERS_FTS):
                # Índice nuevo o desfasado (la base se usó sin FTS5): se reconstruye desde recuerdos.
                con.execute("INSERT INTO recuerdos_fts(recuerdos_fts) VALUES ('rebuild')")
                log.debug("Índice FTS5 de fragmentos reconstruido")
        elif existentes:
            # Sin el módulo fts5 los triggers romperían cada INSERT en recuerdos.
            con.executescript("BEGIN;" + "".join(f"DROP TRIGGER IF EXISTS {n};" for n in _TRIGGERS_FTS)
                              + "COMMIT;")
            log.info("Triggers FTS5 retirados: esta build de SQLite no trae FTS5")
        con.execute("INSERT OR IGNORE INTO meta(clave, valor) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
        con.execute("INSERT OR IGNORE INTO meta(clave, valor) VALUES ('kv_contador', '0')")
        fila = con.execute("SELECT valor FROM meta WHERE clave='schema_version'").fetchone()
        version = _entero(fila[0] if fila else SCHEMA_VERSION, SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            log.warning("El almacén de memoria usa el esquema v%d (este programa conoce el v%d); "
                        "sigo en modo compatible", version, SCHEMA_VERSION)

    def _s_abrir(self) -> dict[str, Any]:
        """Apertura en el hilo ``rams-memoria`` (con caída a ``:memory:``)."""
        motivo: str | None = None
        try:
            try:
                self.ruta_db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                log.debug("No se pudo crear la carpeta del almacén: %s", exc)
            con, fts5, fragmentos = self._conectar(str(self.ruta_db))
            en_memoria = False
        except (sqlite3.Error, OSError, ValueError) as exc:
            motivo = f"{type(exc).__name__}: {exc}"
            log.warning("No se pudo montar el almacén de memoria %s (%s); los datos de memoria de "
                        "esta sesión vivirán solo en RAM (:memory:)", self.ruta_db.name, motivo)
            con, fts5, fragmentos = self._conectar(":memory:")
            en_memoria = True
        self._con = con
        if not en_memoria:
            for sufijo in ("", "-wal", "-shm"):
                ruta = Path(str(self.ruta_db) + sufijo)
                if ruta.exists():
                    _chmod_privado(ruta)
        return {"fts5": fts5, "en_memoria": en_memoria, "motivo": motivo, "fragmentos": fragmentos}

    async def abrir(self) -> DiagnosticoModulo:
        """Monta el almacén ``john/`` (o ``:memory:`` si falla) y devuelve el diagnóstico ``memoria``."""
        if self._cerrada:
            log.debug("abrir() tras cerrar(): el almacén de memoria ya fue desmontado")
            return self.diagnostico()
        async with self._lock("apertura"):
            if self._con is not None or self._cerrada or self._apertura_terminada:
                return self.diagnostico()
            if self._ej is None:
                self._ej = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rams-memoria")
            loop = asyncio.get_running_loop()
            try:
                info = await loop.run_in_executor(self._ej, self._s_abrir)
            except asyncio.CancelledError:
                self._apertura_terminada = True
                raise
            except Exception as exc:  # noqa: BLE001 - ni siquiera :memory: (no debería ocurrir)
                self._apertura_terminada = True
                log.error("No se pudo montar ningún almacén de memoria: %s: %s", type(exc).__name__, exc)
                self._estado = EstadoModulo.OFF
                self._detalle = "sin almacén de memoria"
                return self.diagnostico()
            self._apertura_terminada = True
            self.fts5 = bool(info["fts5"])
            self._en_memoria = bool(info["en_memoria"])
            if self._en_memoria:
                self._estado = EstadoModulo.DEGRADADO
                self._detalle = "memoria volátil (:memory:): no se pudo abrir el almacén en disco"
            else:
                self._estado = EstadoModulo.OK
                self._detalle = self._detalle_ok()
            log.info("Datos de memoria cargados: %d fragmentos (almacén %s, FTS5 %s, sesión %s)",
                     info["fragmentos"], ":memory:" if self._en_memoria else self._carpeta(),
                     "sí" if self.fts5 else "no", self.sesion)
            return self.diagnostico()

    def _s_cerrar(self) -> None:
        con = self._con
        self._con = None
        if con is None:
            return
        try:
            con.execute("PRAGMA optimize")
        except sqlite3.Error as exc:
            log.debug("PRAGMA optimize falló al cerrar: %s", exc)
        con.close()

    async def cerrar(self) -> None:
        """Cierra la conexión (en su hilo) y apaga el executor propio. Idempotente."""
        if self._cerrada:
            return
        self._cerrada = True
        ejecutor, self._ej = self._ej, None
        if ejecutor is not None:
            # Se encola siempre: el executor es FIFO, así que también cierra una apertura en curso.
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(loop.run_in_executor(ejecutor, self._s_cerrar),
                                       timeout=TIMEOUT_CIERRE_S)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("El almacén de memoria no se cerró a tiempo; se abandona la conexión")
            except asyncio.CancelledError:
                ejecutor.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception as exc:  # noqa: BLE001 - el cierre nunca propaga
                log.warning("Error al cerrar el almacén de memoria: %s: %s", type(exc).__name__, exc)
            ejecutor.shutdown(wait=False, cancel_futures=True)
        self._estado = EstadoModulo.OFF
        self._detalle = "almacén desmontado"
        log.info("Almacén de memoria desmontado (sesión %s)", self.sesion)

    def diagnostico(self) -> DiagnosticoModulo:
        """Estado del almacén para el arranque y el HUD (clave ``memoria``)."""
        return DiagnosticoModulo(clave="memoria", nombre=NOMBRE_DIAGNOSTICO, estado=self._estado,
                                 detalle=self._detalle)

    # ------------------------------------------------------------------ ventana activa

    def _s_insertar_turno(self, ts: str, rol: str, canal: str, texto: str, etiquetas: str | None) -> int:
        cursor = self._conexion().execute(
            "INSERT INTO turnos(ts, sesion, rol, canal, texto, etiquetas) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, self.sesion, rol, canal, texto, etiquetas))
        return int(cursor.lastrowid or 0)

    async def registrar_turno(self, rol: str, texto: str, *, canal: str = "terminal",
                              etiquetas: Etiquetas | None = None) -> int:
        """Guarda un turno de la conversación y devuelve su id (0 si no se pudo)."""
        rol_n = str(rol or "").strip().lower()
        if rol_n not in ROLES:
            log.warning("Rol de turno desconocido (%r); no se registra", rol_n[:20])
            return 0
        limpio = _limpiar(texto, MAX_TEXTO_TURNO)
        if not limpio:
            log.debug("Turno vacío de %s; no se registra", rol_n)
            return 0
        canal_n = (_limpiar(canal, 24) or "terminal")
        turno_id = await self._ejecutar("registrar un turno", 0, self._s_insertar_turno,
                                        _ahora_iso(), rol_n, canal_n, limpio, _etiquetas_a_texto(etiquetas))
        if turno_id:
            log.debug("Turno #%d de %s registrado (%d caracteres)", turno_id, rol_n, len(limpio))
        return turno_id

    def _s_ventana(self, n: int, desde_iso: str) -> list[Turno]:
        filas = self._conexion().execute(
            "SELECT id, ts, rol, canal, texto, etiquetas FROM turnos "
            "WHERE sesion = ? OR ts >= ? ORDER BY id DESC LIMIT ?", (self.sesion, desde_iso, n)).fetchall()
        filas.reverse()
        return [Turno(id=int(f[0]), ts=str(f[1]), rol=str(f[2]), texto=str(f[4]), canal=str(f[3]),
                      etiquetas=_etiquetas_desde_texto(f[5])) for f in filas]

    async def ventana_activa(self, n: int | None = None) -> list[Turno]:
        """Últimos ``n`` turnos (por defecto ``ventana_turnos``) en orden cronológico ascendente.

        Solo de la sesión actual o de turnos de las últimas 6 h (continuidad sin arrastrar
        conversaciones viejas).
        """
        cantidad = self.ventana_turnos if n is None else _entero(n, self.ventana_turnos)
        if cantidad <= 0:
            return []
        desde = _iso(_ahora_utc() - timedelta(seconds=CONTINUIDAD_S))
        return await self._ejecutar("leer la ventana activa", [], self._s_ventana, min(cantidad, 500), desde)

    # ------------------------------------------------------------------ recuerdos

    def _s_recordar_en(self, con: sqlite3.Connection, contenido: str, categoria: str, tipo: str,
                       importancia: float, kv_id: str | None) -> tuple[int, bool]:
        """Inserta o refuerza un fragmento dentro de una transacción abierta. ``(id, nuevo)``."""
        huella = _hash_contenido(contenido)
        fila = con.execute("SELECT id FROM recuerdos WHERE hash = ?", (huella,)).fetchone()
        if fila is not None:
            con.execute("UPDATE recuerdos SET importancia = max(importancia, ?) WHERE id = ?",
                        (importancia, fila[0]))
            return int(fila[0]), False
        cursor = con.execute(
            "INSERT INTO recuerdos(ts, tipo, categoria, contenido, importancia, kv_id, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (_ahora_iso(), tipo, categoria, contenido, importancia, kv_id, huella))
        return int(cursor.lastrowid or 0), True

    def _s_recordar(self, contenido: str, categoria: str, tipo: str, importancia: float,
                    kv_id: str | None) -> tuple[int, bool]:
        return self._tx(lambda con: self._s_recordar_en(con, contenido, categoria, tipo, importancia, kv_id))

    async def recordar(self, contenido: str, *, categoria: str = "hecho", tipo: str = "vivido",
                       importancia: float = 0.5, kv_id: str | None = None) -> int:
        """Guarda un fragmento (deduplicado por *hash* normalizado) y devuelve su id (0 si falla).

        Si el fragmento ya existía, su importancia pasa a ``max(anterior, nueva)`` y se devuelve
        el id existente. El contenido se trunca a 1 000 caracteres.
        """
        limpio = _limpiar(contenido, MAX_RECUERDO)
        if not limpio:
            log.debug("Fragmento vacío; no se guarda")
            return 0
        tipo_n = str(tipo or "").strip().lower()
        if tipo_n not in TIPOS_RECUERDO:
            log.warning("Tipo de recuerdo desconocido (%r); se guarda como 'vivido'", tipo_n[:20])
            tipo_n = "vivido"
        categoria_n = _limpiar(categoria, 40).lower() or "hecho"
        imp = _real(importancia, 0.5)
        imp = 0.5 if imp is None else min(1.0, max(0.0, imp))
        kv = _limpiar(kv_id, 16) or None
        resultado = await self._ejecutar("guardar un fragmento", (0, False), self._s_recordar,
                                         limpio, categoria_n, tipo_n, imp, kv)
        recuerdo_id, nuevo = resultado
        if recuerdo_id:
            if nuevo:
                log.info("Fragmento #%d guardado en %s (%s/%s, importancia %.2f)",
                         recuerdo_id, self._carpeta(), tipo_n, categoria_n, imp)
            else:
                log.debug("Fragmento #%d ya existía; importancia reforzada", recuerdo_id)
        return recuerdo_id

    def _s_buscar(self, tokens: list[str], k: int, usar_fts: bool) -> list[Recuerdo]:
        con = self._conexion()
        filas: list[Sequence[Any]] = []
        relevancia: dict[int, float] = {}
        if usar_fts:
            consulta = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
            try:
                filas_fts = con.execute(
                    "SELECT r.id, r.ts, r.tipo, r.categoria, r.contenido, r.importancia, r.accesos, r.kv_id, "
                    "bm25(recuerdos_fts) AS rango FROM recuerdos_fts "
                    "JOIN recuerdos AS r ON r.id = recuerdos_fts.rowid "
                    "WHERE recuerdos_fts MATCH ? ORDER BY rango LIMIT ?", (consulta, CANDIDATOS_FTS)).fetchall()
            except sqlite3.Error as exc:
                log.debug("FTS5 no pudo resolver la búsqueda (%s); uso LIKE", exc)
                usar_fts = False
            else:
                valores = [-(_real(f[8], 0.0) or 0.0) for f in filas_fts]
                maximo = max(valores, default=0.0)
                for fila, valor in zip(filas_fts, valores):
                    relevancia[int(fila[0])] = (valor / maximo) if maximo > 0 else 1.0
                filas = list(filas_fts)
        if not usar_fts:
            condicion = " OR ".join("rams_norm(contenido) LIKE ? ESCAPE '\\'" for _ in tokens)
            patrones = [f"%{_escapar_like(t)}%" for t in tokens]
            filas_like = con.execute(
                f"SELECT {_COLUMNAS_RECUERDO} FROM recuerdos WHERE {condicion} ORDER BY id DESC LIMIT ?",
                (*patrones, CANDIDATOS_LIKE_MAX)).fetchall()
            for fila in filas_like:
                normal = normalizar_texto(fila[4])
                coincidencias = sum(1 for t in tokens if t in normal)
                if coincidencias:
                    relevancia[int(fila[0])] = coincidencias / len(tokens)
                    filas.append(fila)

        ahora = _ahora_utc()
        puntuadas: list[tuple[float, Sequence[Any]]] = []
        for fila in filas:
            momento = _parsear_iso(fila[1])
            dias = max(0.0, (ahora - momento).total_seconds() / 86400.0) if momento else None
            recencia = math.exp(-dias / 30.0) if dias is not None else 0.0
            importancia = _real(fila[5], 0.5) or 0.0
            accesos = max(0, _entero(fila[6], 0))
            puntuacion = (relevancia.get(int(fila[0]), 0.0) + 0.3 * importancia + 0.2 * recencia
                          + 0.05 * min(accesos, 10) / 10.0)
            puntuadas.append((puntuacion, fila))
        puntuadas.sort(key=lambda par: (-par[0], -int(par[1][0])))
        elegidas = puntuadas[:k]
        if elegidas:
            ids = [int(f[0]) for _, f in elegidas]
            marcas = ",".join("?" for _ in ids)
            self._tx(lambda c: c.execute(
                f"UPDATE recuerdos SET accesos = accesos + 1, ultimo_acceso = ? WHERE id IN ({marcas})",
                (_ahora_iso(), *ids)))
        return [_recuerdo_desde_fila(f, round(p, 4)) for p, f in elegidas]

    async def buscar(self, consulta: str, k: int = 5) -> list[Recuerdo]:
        """Recupera los ``k`` fragmentos más relevantes (FTS5/BM25 + importancia + recencia + accesos).

        Sin FTS5 usa ``LIKE`` sobre el texto normalizado. Consulta vacía ⇒ ``[]``.
        """
        tokens = _tokens_busqueda(consulta)
        cantidad = _entero(k, 5)
        if not tokens or cantidad <= 0:
            return []
        resultado = await self._ejecutar("buscar fragmentos", [], self._s_buscar, tokens,
                                         min(cantidad, 100), bool(self.fts5))
        log.debug("Búsqueda en memoria: %d tokens → %d fragmentos", len(tokens), len(resultado))
        return resultado

    def _s_listar(self, limite: int, tipo: str | None) -> list[Recuerdo]:
        con = self._conexion()
        if tipo is None:
            filas = con.execute(f"SELECT {_COLUMNAS_RECUERDO} FROM recuerdos ORDER BY id DESC LIMIT ?",
                                (limite,)).fetchall()
        else:
            filas = con.execute(f"SELECT {_COLUMNAS_RECUERDO} FROM recuerdos WHERE tipo = ? "
                                "ORDER BY id DESC LIMIT ?", (tipo, limite)).fetchall()
        return [_recuerdo_desde_fila(f) for f in filas]

    async def listar_recuerdos(self, limite: int = 20, *, tipo: str | None = None) -> list[Recuerdo]:
        """Fragmentos más recientes primero (transparencia: ``/memoria`` sin consulta)."""
        cantidad = _entero(limite, 20)
        if cantidad <= 0:
            return []
        tipo_n = str(tipo).strip().lower() if tipo is not None else None
        return await self._ejecutar("listar fragmentos", [], self._s_listar, min(cantidad, 1000), tipo_n)

    def _s_olvidar(self, recuerdo_id: int) -> bool:
        def operar(con: sqlite3.Connection) -> bool:
            fila = con.execute("SELECT categoria, kv_id FROM recuerdos WHERE id = ?", (recuerdo_id,)).fetchone()
            if fila is None:
                return False
            con.execute("DELETE FROM recuerdos WHERE id = ?", (recuerdo_id,))
            if fila[0] == "consolidacion" and fila[1]:
                # Olvidar una consolidación borra también su resumen KV (no reaparece en el prompt).
                con.execute("DELETE FROM resumenes WHERE kv_id = ?", (fila[1],))
            return True
        return self._tx(operar)

    async def olvidar(self, recuerdo_id: int) -> bool:
        """Borra un fragmento por id (y su resumen KV si es una consolidación). ``True`` si existía."""
        ident = _entero(recuerdo_id, -1)
        if ident <= 0:
            return False
        borrado = await self._ejecutar("olvidar un fragmento", False, self._s_olvidar, ident)
        if borrado:
            log.info("Fragmento #%d olvidado a petición del usuario", ident)
        return borrado

    def _s_importar_perfil(self, huella: str, hechos: list[str], generado: str | None) -> int:
        def operar(con: sqlite3.Connection) -> int:
            fila = con.execute("SELECT valor FROM meta WHERE clave = 'perfil_hash'").fetchone()
            if fila is not None and fila[0] == huella:
                return 0
            con.execute("DELETE FROM recuerdos WHERE tipo = 'heredado' AND categoria = 'perfil'")
            insertados = 0
            for hecho in hechos:
                _, nuevo = self._s_recordar_en(con, hecho, "perfil", "heredado", IMPORTANCIA_HEREDADO, None)
                insertados += int(nuevo)
            con.execute("INSERT INTO meta(clave, valor) VALUES ('perfil_hash', ?) "
                        "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (huella,))
            if generado:
                con.execute("INSERT INTO meta(clave, valor) VALUES ('perfil_generado', ?) "
                            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (generado,))
            return insertados
        return self._tx(operar)

    async def importar_perfil(self, perfil: Mapping[str, Any]) -> int:
        """Convierte el perfil de WhatsApp en 3–6 hechos **heredados** (idempotente por ``perfil_hash``).

        Devuelve cuántos hechos insertó (0 si el perfil no cambió o si falla).
        """
        if not isinstance(perfil, Mapping):
            log.warning("Perfil heredado inválido (no es un objeto JSON); no se importa")
            return 0
        try:
            huella = hashlib.sha1(
                json.dumps(dict(perfil), sort_keys=True, default=str).encode("utf-8")).hexdigest()
            hechos = hechos_desde_perfil(perfil, self.settings.trato_usuario)
        except (TypeError, ValueError) as exc:
            log.warning("No se pudo leer el perfil heredado: %s", exc)
            return 0
        generado = perfil.get("generado")
        generado_txt = _limpiar(generado, 40) if isinstance(generado, str) else None
        insertados = await self._ejecutar("importar el perfil heredado", 0, self._s_importar_perfil,
                                          huella, hechos, generado_txt)
        if insertados:
            log.info("Recuerdo heredado actualizado: %d hechos del perfil conductual", insertados)
        else:
            log.debug("Perfil heredado sin cambios")
        return insertados

    # ------------------------------------------------------------------ perfil de Google Takeout

    def _s_importar_takeout(self, huella: str, hechos: list[str], generado: str | None) -> int:
        def operar(con: sqlite3.Connection) -> int:
            fila = con.execute("SELECT valor FROM meta WHERE clave = 'takeout_hash'").fetchone()
            if fila is not None and fila[0] == huella:
                return 0
            con.execute("DELETE FROM recuerdos WHERE tipo = 'heredado' AND categoria = ?", (CATEGORIA_TAKEOUT,))
            insertados = 0
            for hecho in hechos:
                _, nuevo = self._s_recordar_en(con, hecho, CATEGORIA_TAKEOUT, "heredado", IMPORTANCIA_HEREDADO, None)
                insertados += int(nuevo)
            con.execute("INSERT INTO meta(clave, valor) VALUES ('takeout_hash', ?) "
                        "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (huella,))
            if generado:
                con.execute("INSERT INTO meta(clave, valor) VALUES ('takeout_generado', ?) "
                            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (generado,))
            return insertados
        return self._tx(operar)

    async def importar_perfil_takeout(self, perfil: Mapping[str, Any]) -> int:
        """Convierte el perfil de Google Takeout en 3–6 hechos **heredados** (categoría ``takeout``).

        Idempotente por ``meta.takeout_hash``: con el mismo perfil no inserta nada (no se duplica en
        cada arranque); con uno nuevo reemplaza los hechos anteriores. Nunca toca los del perfil de
        WhatsApp (categoría ``perfil``). Devuelve cuántos hechos insertó (0 si no cambió o si falla).
        """
        if not isinstance(perfil, Mapping):
            log.warning("Perfil de Takeout inválido (no es un objeto JSON); no se importa")
            return 0
        try:
            huella = hashlib.sha1(
                json.dumps(dict(perfil), sort_keys=True, default=str).encode("utf-8")).hexdigest()
            hechos = hechos_desde_takeout(perfil, self.settings.trato_usuario)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:   # perfil roto: nunca lanza
            log.warning("No se pudo leer el perfil de Takeout: %s", type(exc).__name__)
            return 0
        generado = perfil.get("generado")
        generado_txt = _limpiar(generado, 40) if isinstance(generado, str) else None
        insertados = await self._ejecutar("importar el perfil de Takeout", 0, self._s_importar_takeout,
                                          huella, hechos, generado_txt)
        if insertados:
            log.info("Recuerdo heredado actualizado: %d hechos del perfil de Google Takeout", insertados)
        else:
            log.debug("Perfil de Takeout sin cambios")
        return insertados

    def _s_olvidar_takeout(self) -> int:
        def operar(con: sqlite3.Connection) -> int:
            cursor = con.execute("DELETE FROM recuerdos WHERE tipo = 'heredado' AND categoria = ?",
                                 (CATEGORIA_TAKEOUT,))
            con.execute("DELETE FROM meta WHERE clave IN ('takeout_hash', 'takeout_generado')")
            return max(0, int(cursor.rowcount or 0))
        return self._tx(operar)

    async def olvidar_perfil_takeout(self) -> int:
        """Borra los hechos heredados del perfil de Takeout y su huella. Devuelve cuántos borró
        (0 si no había o si falla; nunca lanza). El perfil de WhatsApp no se toca. Lo usan
        «olvida mi perfil de Google» y el arranque con ``RAMS_PERFIL_TAKEOUT_EN_PROMPT=0``."""
        borrados = await self._ejecutar("olvidar el perfil de Takeout", 0, self._s_olvidar_takeout)
        if borrados:
            log.info("Recuerdos heredados del perfil de Google Takeout borrados (%d hechos)", borrados)
        else:
            log.debug("No había recuerdos heredados del perfil de Google Takeout")
        return borrados

    # ------------------------------------------------------------------ Salieri

    def _s_resumen_reciente(self) -> str | None:
        fila = self._conexion().execute("SELECT kv_id, texto FROM resumenes ORDER BY id DESC LIMIT 1").fetchone()
        return f"{fila[0]}: {fila[1]}" if fila else None

    async def resumen_reciente(self) -> str | None:
        """Último resumen consolidado como ``"KV-0007: texto"`` (o ``None``)."""
        return await self._ejecutar("leer el último resumen", None, self._s_resumen_reciente)

    def _s_purgar_si_toca(self, con: sqlite3.Connection) -> None:
        """Una vez al día borra los turnos consolidados de más de 30 días."""
        hoy = _ahora_utc().strftime("%Y-%m-%d")
        fila = con.execute("SELECT valor FROM meta WHERE clave = 'ultima_purga'").fetchone()
        if fila is not None and fila[0] == hoy:
            return
        limite = _iso(_ahora_utc() - timedelta(days=DIAS_PURGA))
        cursor = con.execute("DELETE FROM turnos WHERE consolidado = 1 AND ts < ?", (limite,))
        con.execute("INSERT INTO meta(clave, valor) VALUES ('ultima_purga', ?) "
                    "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (hoy,))
        if cursor.rowcount and cursor.rowcount > 0:
            log.info("Salieri depuró %d turnos ya consolidados de más de %d días", cursor.rowcount, DIAS_PURGA)

    def _consulta_candidatos(self, columnas: str) -> str:
        return (f"SELECT {columnas} FROM turnos WHERE consolidado = 0 AND id < "
                "(SELECT COALESCE(min(id), 0) FROM (SELECT id FROM turnos ORDER BY id DESC LIMIT ?))")

    def _s_contar_candidatos(self) -> int:
        fila = self._conexion().execute(self._consulta_candidatos("count(*)"), (self.ventana_turnos,)).fetchone()
        return int(fila[0]) if fila else 0

    def _s_preparar_lote(self, forzar: bool) -> list[tuple[int, str, str, str]] | None:
        self._tx(self._s_purgar_si_toca)
        con = self._conexion()
        total = self._s_contar_candidatos()
        minimo = MIN_CANDIDATOS_FORZADO if forzar else MIN_CANDIDATOS
        if total < minimo:
            return None
        filas = con.execute(self._consulta_candidatos("id, ts, rol, texto") + " ORDER BY id ASC LIMIT ?",
                            (self.ventana_turnos, LOTE_SALIERI)).fetchall()
        return [(int(f[0]), str(f[1]), str(f[2]), str(f[3])) for f in filas] or None

    def _s_consolidar(self, lote: list[tuple[int, str, str, str]], resumen_llm: str | None) -> tuple[str, int, str]:
        zona = zona_local(self.settings)
        if resumen_llm:
            fecha = _fecha_hora_local(lote[0][1], zona) or "—"
            texto = _limpiar(f"Sesión del {fecha}: {resumen_llm}", MAX_RECUERDO)
            metodo = "llm"
        else:
            texto = _limpiar(extracto_salieri(lote, zona=zona, nombre_ia=self.settings.nombre_ia), MAX_RECUERDO)
            metodo = "extractivo"
        ids = [t[0] for t in lote]

        def operar(con: sqlite3.Connection) -> tuple[str, int, str]:
            fila = con.execute("SELECT valor FROM meta WHERE clave = 'kv_contador'").fetchone()
            numero = _entero(fila[0] if fila else 0, 0)
            while True:
                numero += 1
                kv_id = f"KV-{numero:04d}"
                if con.execute("SELECT 1 FROM resumenes WHERE kv_id = ?", (kv_id,)).fetchone() is None:
                    break
            con.execute("INSERT INTO meta(clave, valor) VALUES ('kv_contador', ?) "
                        "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (str(numero),))
            con.execute("INSERT INTO resumenes(kv_id, ts, ts_desde, ts_hasta, n_turnos, texto, metodo) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (kv_id, _ahora_iso(), lote[0][1], lote[-1][1], len(lote), texto, metodo))
            self._s_recordar_en(con, texto, "consolidacion", "vivido", IMPORTANCIA_CONSOLIDACION, kv_id)
            marcas = ",".join("?" for _ in ids)
            con.execute(f"UPDATE turnos SET consolidado = 1 WHERE id IN ({marcas})", ids)
            return kv_id, len(lote), metodo
        return self._tx(operar)

    async def _resumir_con_llm(self, lote: Sequence[tuple[int, str, str, str]]) -> str | None:
        """Pide el resumen al ``resumidor`` (Broca); ``None`` si no hay o falla (⇒ extractivo)."""
        if self._resumidor is None:
            return None
        nombres = {"usuario": "Usuario", "amadeus": self.settings.nombre_ia or "Amadeus", "sistema": "Sistema"}
        lineas: list[str] = []
        total = 0
        for _, _, rol, texto in lote:
            linea = f"{nombres.get(rol, rol)}: {_limpiar(texto, 500)}"
            if total + len(linea) > MAX_TEXTO_RESUMIDOR:
                break
            lineas.append(linea)
            total += len(linea) + 1
        try:
            respuesta = await asyncio.wait_for(self._resumidor("\n".join(lineas)), timeout=TIMEOUT_RESUMIDOR_S)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("El resumidor de Salieri no respondió a tiempo; uso el método extractivo")
            return None
        except Exception as exc:  # noqa: BLE001 - un fallo del LLM nunca detiene la consolidación
            log.warning("El resumidor de Salieri falló (%s); uso el método extractivo", type(exc).__name__)
            return None
        if not isinstance(respuesta, str):
            return None
        limpio = _limpiar(respuesta, MAX_RECUERDO - 40)
        return limpio or None

    async def comprimir(self, *, forzar: bool = False) -> str | None:
        """Salieri: consolida los turnos viejos en un resumen ``KV-####`` y devuelve su id (o ``None``).

        Candidatos: turnos sin consolidar fuera de los últimos ``ventana_turnos``. Se ejecuta con
        ≥ 24 candidatos (≥ 4 con ``forzar=True``) y toma los 40 más antiguos.
        """
        if self._cerrada:
            return None
        try:
            async with self._lock("salieri"):
                lote = await self._ejecutar("preparar el lote de Salieri", None, self._s_preparar_lote, bool(forzar))
                if not lote:
                    return None
                log.debug("Consolidación en curso: %d turnos", len(lote))
                resumen_llm = None
                if self.settings.compresion_llm and self._resumidor is not None:
                    resumen_llm = await self._resumir_con_llm(lote)
                resultado = await self._ejecutar("consolidar con Salieri", None, self._s_consolidar,
                                                 lote, resumen_llm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - contrato: ningún método público lanza
            log.warning("Salieri no pudo consolidar: %s: %s", type(exc).__name__, exc)
            return None
        if resultado is None:
            return None
        kv_id, n_turnos, metodo = resultado
        log.info("Salieri consolidó %s (%d turnos, %s)", kv_id, n_turnos, metodo)
        if self._al_consolidar is not None:
            try:
                self._al_consolidar(kv_id, n_turnos, metodo)
            except Exception as exc:  # noqa: BLE001 - el callback es ajeno; se aísla
                log.warning("El aviso de consolidación falló: %s: %s", type(exc).__name__, exc)
        return kv_id

    async def bucle_compresor(self, parar: asyncio.Event, *, periodo_s: float = 120.0,
                              presente: Callable[[], bool] | None = None) -> None:
        """Cada ``periodo_s`` consolida **solo** si el usuario no está ("consolido mientras duermes")
        o si hay ≥ 80 candidatos pendientes. Termina cuando ``parar`` se activa.

        Sin función ``presente`` se trata como ausencia (no hay forma de saberlo); si ``presente``
        lanza, se asume presencia (conservador).
        """
        periodo = max(0.05, _real(periodo_s, 120.0) or 120.0)
        log.info("Salieri en guardia: consolidación cada %.0f s mientras no estás", periodo)
        while not parar.is_set() and not self._cerrada:
            try:
                await asyncio.wait_for(parar.wait(), timeout=periodo)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            if parar.is_set() or self._cerrada:
                break
            try:
                await self._ciclo_compresor(parar, presente)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - un ciclo fallido no detiene a Salieri
                log.warning("Ciclo de Salieri fallido: %s: %s", type(exc).__name__, exc)
        log.debug("Salieri se retira")

    async def _ciclo_compresor(self, parar: asyncio.Event, presente: Callable[[], bool] | None) -> None:
        if presente is None:
            ausente = True
        else:
            try:
                valor = presente()
                if asyncio.iscoroutine(valor):
                    # CONTRATO: presente es síncrona (§6.3); una corrutina no se espera aquí.
                    valor.close()
                    log.debug("presente() devolvió una corrutina; asumo que el usuario está")
                    valor = True
                ausente = not bool(valor)
            except Exception as exc:  # noqa: BLE001 - función ajena
                log.debug("presente() falló (%s); asumo que el usuario está", type(exc).__name__)
                ausente = False
        pendientes = await self._ejecutar("contar candidatos de Salieri", 0, self._s_contar_candidatos)
        if not ausente and pendientes < CANDIDATOS_URGENTES:
            return
        for _ in range(LOTES_POR_CICLO):
            if parar.is_set() or await self.comprimir() is None:
                break

    # ------------------------------------------------------------------ agenda

    def _s_programar(self, tipo: str, vence_iso: str, mensaje: str) -> int:
        cursor = self._conexion().execute(
            "INSERT INTO agenda(tipo, vence_ts, mensaje, creado_ts) VALUES (?, ?, ?, ?)",
            (tipo, vence_iso, mensaje, _ahora_iso()))
        return int(cursor.lastrowid or 0)

    async def programar(self, tipo: str, vence: datetime, mensaje: str) -> int:
        """Programa un recordatorio o cápsula del tiempo. ``vence`` *aware* (naive ⇒ hora local)."""
        tipo_n = str(tipo or "").strip().lower()
        if tipo_n not in TIPOS_AGENDA:
            log.warning("Tipo de agenda desconocido (%r); no se programa", tipo_n[:20])
            return 0
        if not isinstance(vence, datetime):
            log.warning("Vencimiento de agenda inválido (%s); no se programa", type(vence).__name__)
            return 0
        limpio = _limpiar(mensaje, MAX_MENSAJE_AGENDA)
        if not limpio:
            log.debug("Mensaje de agenda vacío; no se programa")
            return 0
        try:
            vence_iso = _iso(_a_utc(vence, zona_local(self.settings)))
        except (ValueError, OverflowError) as exc:
            log.warning("Vencimiento de agenda fuera de rango: %s", exc)
            return 0
        item_id = await self._ejecutar("programar en la agenda", 0, self._s_programar, tipo_n, vence_iso, limpio)
        if item_id:
            log.info("Agenda: %s #%d programado para %s (UTC)", tipo_n, item_id, vence_iso)
        return item_id

    @staticmethod
    def _item_desde_fila(fila: Sequence[Any]) -> ItemAgenda:
        return ItemAgenda(id=int(fila[0]), tipo=str(fila[1]), vence_ts=str(fila[2]), mensaje=str(fila[3]),
                          estado=str(fila[4]), creado_ts=str(fila[5] or ""))

    def _s_vencidos(self, ahora_iso: str) -> list[ItemAgenda]:
        filas = self._conexion().execute(
            "SELECT id, tipo, vence_ts, mensaje, estado, creado_ts FROM agenda "
            "WHERE estado = 'pendiente' AND vence_ts <= ? ORDER BY vence_ts ASC, id ASC", (ahora_iso,)).fetchall()
        return [self._item_desde_fila(f) for f in filas]

    async def vencidos(self, ahora: datetime | None = None) -> list[ItemAgenda]:
        """Pendientes con ``vence_ts <= ahora`` (UTC). ``<=`` para no perder nada tras una suspensión."""
        try:
            momento = _ahora_utc() if not isinstance(ahora, datetime) else _a_utc(ahora, zona_local(self.settings))
            ahora_iso = _iso(momento)
        except (ValueError, OverflowError) as exc:
            log.warning("Momento inválido para la agenda: %s", exc)
            return []
        return await self._ejecutar("leer la agenda vencida", [], self._s_vencidos, ahora_iso)

    def _s_marcar_entregado(self, item_id: int, canal: str) -> int:
        cursor = self._conexion().execute(
            "UPDATE agenda SET estado = 'entregado', entregado_ts = ?, canal = ? "
            "WHERE id = ? AND estado = 'pendiente'", (_ahora_iso(), canal, item_id))
        return int(cursor.rowcount or 0)

    async def marcar_entregado(self, item_id: int, canal: str) -> None:
        """Marca un elemento de la agenda como entregado por ``canal``."""
        ident = _entero(item_id, -1)
        if ident <= 0:
            return
        cambiados = await self._ejecutar("marcar la agenda como entregada", 0, self._s_marcar_entregado,
                                         ident, _limpiar(canal, 24) or "terminal")
        if cambiados:
            log.info("Agenda #%d entregada por %s", ident, _limpiar(canal, 24) or "terminal")

    def _s_cancelar(self, item_id: int) -> bool:
        cursor = self._conexion().execute(
            "UPDATE agenda SET estado = 'cancelado' WHERE id = ? AND estado = 'pendiente'", (item_id,))
        return bool(cursor.rowcount and cursor.rowcount > 0)

    async def cancelar_agenda(self, item_id: int) -> bool:
        """Cancela un pendiente de la agenda. ``True`` si estaba pendiente."""
        ident = _entero(item_id, -1)
        if ident <= 0:
            return False
        cancelado = await self._ejecutar("cancelar en la agenda", False, self._s_cancelar, ident)
        if cancelado:
            log.info("Agenda #%d cancelada", ident)
        return cancelado

    def _s_listar_agenda(self, solo_pendientes: bool) -> list[ItemAgenda]:
        filtro = "WHERE estado = 'pendiente' " if solo_pendientes else ""
        filas = self._conexion().execute(
            "SELECT id, tipo, vence_ts, mensaje, estado, creado_ts FROM agenda "
            f"{filtro}ORDER BY vence_ts ASC, id ASC LIMIT 500").fetchall()
        return [self._item_desde_fila(f) for f in filas]

    async def listar_agenda(self, *, solo_pendientes: bool = True) -> list[ItemAgenda]:
        """Elementos de la agenda ordenados por vencimiento."""
        return await self._ejecutar("listar la agenda", [], self._s_listar_agenda, bool(solo_pendientes))

    # ------------------------------------------------------------------ bitácora

    def _s_bitacora(self, entrada: str, duracion_s: float) -> int:
        cursor = self._conexion().execute(
            "INSERT INTO bitacora(ts, duracion_s, entrada) VALUES (?, ?, ?)", (_ahora_iso(), duracion_s, entrada))
        return int(cursor.lastrowid or 0)

    async def escribir_bitacora(self, entrada: str, duracion_s: float) -> int:
        """Guarda la entrada de bitácora de fin de sesión y devuelve su id (0 si falla)."""
        limpio = _limpiar(entrada, MAX_BITACORA)
        if not limpio:
            log.debug("Entrada de bitácora vacía; no se guarda")
            return 0
        duracion = max(0.0, _real(duracion_s, 0.0) or 0.0)
        bitacora_id = await self._ejecutar("escribir la bitácora", 0, self._s_bitacora, limpio, duracion)
        if bitacora_id:
            log.info("Bitácora de sesión escrita (#%d, %d caracteres)", bitacora_id, len(limpio))
        return bitacora_id

    def _s_ultima_bitacora(self) -> str | None:
        fila = self._conexion().execute("SELECT entrada FROM bitacora ORDER BY id DESC LIMIT 1").fetchone()
        return str(fila[0]) if fila else None

    async def ultima_bitacora(self) -> str | None:
        """Última entrada de bitácora (o ``None``)."""
        return await self._ejecutar("leer la bitácora", None, self._s_ultima_bitacora)

    # ------------------------------------------------------------------ llamadas (RegistroLlamadas)

    def _s_registrar_llamada(self, origen: str, canal: str, nivel: int, motivo: str, texto: str) -> int:
        cursor = self._conexion().execute(
            "INSERT INTO llamadas(ts, origen, canal, nivel, motivo, texto) VALUES (?, ?, ?, ?, ?, ?)",
            (_ahora_iso(), origen, canal, nivel, motivo, texto))
        return int(cursor.lastrowid or 0)

    async def registrar_llamada(self, *, origen: str, canal: str, nivel: int, motivo: str, texto: str = "") -> int:
        """Registra una "llamada" proactiva entregada por Telegram y devuelve su id (0 si falla)."""
        origen_n = _limpiar(origen, 24) or "iniciativa"
        canal_n = _limpiar(canal, 24) or "telegram"
        motivo_n = _limpiar(motivo, 80)
        nivel_n = min(3, max(1, _entero(nivel, 1)))
        llamada_id = await self._ejecutar("registrar una llamada", 0, self._s_registrar_llamada,
                                          origen_n, canal_n, nivel_n, motivo_n, _limpiar(texto, MAX_TEXTO_LLAMADA))
        if llamada_id:
            log.info("Llamada #%d registrada (%s, nivel %d, motivo %s)", llamada_id, origen_n, nivel_n,
                     motivo_n or "—")
        return llamada_id

    def _s_marcar_llamada(self, llamada_id: int, contestada: bool) -> int:
        cursor = self._conexion().execute("UPDATE llamadas SET contestada = ? WHERE id = ?",
                                          (1 if contestada else 0, llamada_id))
        return int(cursor.rowcount or 0)

    async def marcar_llamada(self, llamada_id: int, contestada: bool) -> None:
        """Marca una llamada como contestada (``True``) o rechazada (``False``)."""
        ident = _entero(llamada_id, -1)
        if ident <= 0:
            return
        cambiadas = await self._ejecutar("marcar una llamada", 0, self._s_marcar_llamada, ident, bool(contestada))
        if cambiadas:
            log.info("Llamada #%d %s", ident, "contestada" if contestada else "rechazada")

    def _s_marcar_contestadas(self, desde_iso: str | None) -> int:
        if desde_iso is None:
            cursor = self._conexion().execute("UPDATE llamadas SET contestada = 1 WHERE contestada IS NULL")
        else:
            cursor = self._conexion().execute(
                "UPDATE llamadas SET contestada = 1 WHERE contestada IS NULL AND ts >= ?", (desde_iso,))
        return int(cursor.rowcount or 0)

    async def marcar_llamadas_contestadas(self, desde_ts: float | None = None) -> int:
        """Pone ``contestada = 1`` en las llamadas sin respuesta (desde ``desde_ts``, si se da)."""
        desde_iso = None if desde_ts is None else _iso_desde_epoch(desde_ts)
        cambiadas = await self._ejecutar("marcar llamadas contestadas", 0, self._s_marcar_contestadas, desde_iso)
        if cambiadas:
            log.debug("%d llamadas marcadas como contestadas", cambiadas)
        return cambiadas

    def _s_contar(self, sql: str, parametros: tuple[Any, ...]) -> int:
        fila = self._conexion().execute(sql, parametros).fetchone()
        return int(fila[0]) if fila and fila[0] is not None else 0

    async def llamadas_ignoradas(self, horas: float = 24.0, gracia_min: float = 120.0) -> int:
        """Llamadas de las últimas ``horas`` rechazadas, o sin respuesta tras ``gracia_min`` minutos."""
        ahora = _ahora_utc()
        try:
            desde = _iso(ahora - timedelta(hours=max(0.0, _real(horas, 24.0) or 0.0)))
            limite_gracia = _iso(ahora - timedelta(minutes=max(0.0, _real(gracia_min, 120.0) or 0.0)))
        except OverflowError:
            desde = _iso(datetime.fromtimestamp(0, UTC))
            limite_gracia = _iso(ahora)
        return await self._ejecutar(
            "contar llamadas ignoradas", 0, self._s_contar,
            "SELECT count(*) FROM llamadas WHERE ts >= ? AND "
            "(contestada = 0 OR (contestada IS NULL AND ts < ?))", (desde, limite_gracia))

    async def contar_llamadas(self, desde_ts: float, *, solo_sin_contestar: bool = True) -> int:
        """Llamadas con ``ts >= desde_ts`` (y, si se pide, sin contestar o rechazadas)."""
        desde_iso = _iso_desde_epoch(desde_ts)
        sql = "SELECT count(*) FROM llamadas WHERE ts >= ?"
        if solo_sin_contestar:
            sql += " AND (contestada IS NULL OR contestada = 0)"
        return await self._ejecutar("contar llamadas", 0, self._s_contar, sql, (desde_iso,))

    def _s_ultima_llamada(self) -> dict[str, Any] | None:
        fila = self._conexion().execute(
            "SELECT id, ts, origen, canal, nivel, motivo, texto, contestada FROM llamadas "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if fila is None:
            return None
        return {"id": int(fila[0]), "ts": str(fila[1]), "origen": str(fila[2]), "canal": str(fila[3]),
                "nivel": int(fila[4]), "motivo": str(fila[5]), "texto": str(fila[6] or ""),
                "contestada": None if fila[7] is None else bool(fila[7])}

    async def ultima_llamada(self) -> dict[str, Any] | None:
        """``{"id","ts","origen","canal","nivel","motivo","texto","contestada"}`` de la más reciente."""
        return await self._ejecutar("leer la última llamada", None, self._s_ultima_llamada)

    async def contar_proactivos_hoy(self) -> int:
        """Llamadas con origen ``iniciativa`` **y** canal ``telegram`` desde la medianoche local."""
        try:
            medianoche = ahora_local(self.settings).replace(hour=0, minute=0, second=0, microsecond=0)
            desde = _iso(medianoche)
        except (ValueError, OverflowError) as exc:
            log.warning("No se pudo calcular la medianoche local: %s", exc)
            return 0
        return await self._ejecutar(
            "contar proactivos de hoy", 0, self._s_contar,
            "SELECT count(*) FROM llamadas WHERE origen = 'iniciativa' AND canal = 'telegram' AND ts >= ?",
            (desde,))

    async def contar_consolidaciones(self, desde_ts: float) -> int:
        """Resúmenes de Salieri con ``ts >= desde_ts`` (para "consolidé N mientras no estabas")."""
        return await self._ejecutar("contar consolidaciones", 0, self._s_contar,
                                    "SELECT count(*) FROM resumenes WHERE ts >= ?", (_iso_desde_epoch(desde_ts),))

    # ------------------------------------------------------------------ metadatos

    def _s_meta_get(self, clave: str) -> str | None:
        fila = self._conexion().execute("SELECT valor FROM meta WHERE clave = ?", (clave,)).fetchone()
        return str(fila[0]) if fila else None

    async def meta_get(self, clave: str) -> str | None:
        """Valor de ``meta`` (p. ej. ``"explosion_fecha"``) o ``None`` si no existe."""
        clave_n = _limpiar(clave, 64)
        if not clave_n:
            return None
        return await self._ejecutar("leer un metadato", None, self._s_meta_get, clave_n)

    def _s_meta_set(self, clave: str, valor: str) -> None:
        self._conexion().execute("INSERT INTO meta(clave, valor) VALUES (?, ?) "
                                 "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, valor))

    async def meta_set(self, clave: str, valor: str) -> None:
        """Escribe un metadato. ``schema_version`` y ``kv_contador`` son internos y se rechazan."""
        clave_n = _limpiar(clave, 64)
        if not clave_n:
            return
        if clave_n in _META_INTERNAS:
            log.warning("El metadato %s es interno del almacén; no se modifica", clave_n)
            return
        await self._ejecutar("escribir un metadato", None, self._s_meta_set, clave_n,
                             _limpiar("" if valor is None else valor, 4000))

    # ------------------------------------------------------------------ estado

    def _s_estadisticas(self) -> dict[str, Any]:
        con = self._conexion()
        zona = zona_local(self.settings)

        def contar(sql: str) -> int:
            fila = con.execute(sql).fetchone()
            return int(fila[0]) if fila and fila[0] is not None else 0

        turnos = contar("SELECT count(*) FROM turnos")
        recuerdos = contar("SELECT count(*) FROM recuerdos")
        consolidaciones = contar("SELECT count(*) FROM resumenes")
        agenda = contar("SELECT count(*) FROM agenda WHERE estado = 'pendiente'")
        ultimo = con.execute("SELECT kv_id, ts FROM resumenes ORDER BY id DESC LIMIT 1").fetchone()
        fila_perfil = con.execute("SELECT valor FROM meta WHERE clave = 'perfil_generado'").fetchone()
        generado = fila_perfil[0] if fila_perfil else self._leer_generado_de_archivo()
        return {
            "fragmentos": turnos + recuerdos,
            "turnos": turnos,
            "recuerdos": recuerdos,
            "consolidaciones": consolidaciones,
            "ultimo_kv": str(ultimo[0]) if ultimo else None,
            "ultimo_kv_ts": _fecha_hora_local(ultimo[1], zona) if ultimo else None,
            "perfil_generado": _fecha_local(generado, zona) if generado else None,
            "fts5": bool(self.fts5),
            "agenda_pendiente": agenda,
        }

    def _leer_generado_de_archivo(self) -> str | None:
        """Campo ``generado`` de ``perfil_conductual.json`` si existe (aún no importado)."""
        ruta = Path(self.settings.perfil_path)
        try:
            if not ruta.is_file() or ruta.stat().st_size > 5_000_000:
                return None
            datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("No se pudo leer la fecha del perfil heredado: %s", exc)
            return None
        generado = datos.get("generado") if isinstance(datos, dict) else None
        return generado if isinstance(generado, str) else None

    async def estadisticas(self) -> dict[str, Any]:
        """Conteos del almacén para el arranque (*memory restore*), ``/estado`` y la despedida."""
        defecto: dict[str, Any] = {"fragmentos": 0, "turnos": 0, "recuerdos": 0, "consolidaciones": 0,
                                   "ultimo_kv": None, "ultimo_kv_ts": None, "perfil_generado": None,
                                   "fts5": bool(self.fts5), "agenda_pendiente": 0}
        return await self._ejecutar("calcular estadísticas", defecto, self._s_estadisticas)
