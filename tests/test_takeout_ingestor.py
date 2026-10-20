"""Tests de core/takeout_ingestor.py con un Google Takeout 100 % sintético.

Los zips se construyen dentro de cada test (``tmp_path``) con la misma forma que el Takeout real
(nombres en español, "Google Fotos" con espacio duro, mbox con CRLF, etiquetas de Gmail en
RFC 2047…). Llevan **marcadores secretos** (direcciones, teléfonos, nombres, textos ajenos,
coordenadas, lugares) que nunca deben aparecer en el perfil, el resumen ni la salida de la CLI.
Sin red.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from email.header import Header
from pathlib import Path
from typing import Any

import pytest

import core.takeout_ingestor as ti
from core import memory_engine, wsp_ingestor
from core.takeout_ingestor import (
    MAX_RESUMEN,
    TIPO_PERFIL,
    VERSION_PERFIL_TAKEOUT,
    Exclusiones,
    cargar_exclusiones,
    cargar_perfil_takeout,
    construir_perfil_takeout,
    dominio_registrable,
    guardar_perfil,
    main,
    perfil_vigente,
    quitar_temas,
    resumen_perfil_takeout,
    ruta_exclusiones,
)

BOGOTA = timezone(timedelta(hours=-5), "COT")
SELLO = "takeout-20261017T170949Z"
CUENTA = "yo.prueba@gmail.com"
FOTOS = "Takeout/Google Fotos"

# Nada de esto puede salir del ingestor (perfil, resumen ni CLI).
SECRETOS = (
    "Calle Falsa", "3001234567", "300 123 4567", "palabrasecretadiccionario", "PC-SECRETO", "Zacar", "3009876543",
    "Apellidosecreto", "123456789012345", "CAMBIOSECRETO", "ACCIONSECRETA", "Persona Etiquetada",
    "descripcion secreta", "4.6097", "74.0817", "texto ajeno secreto", "spamsecreto", "papelerasecreto",
    "borradorsecreto", "texto citado ajeno", "autonotasecreta", "xyzzyplugh", "usuario-secreto",
    "Gertrudis", "Juanito", "secretoanidado", "bloqueado@", "migrana", "localhost", "127.0.0.1",
    "ana.amiga", "yo.prueba", "pornhub", "Quiubo", "Chao", "Pedro Ficticio", "Ana Amiga", "token=",
    "ejemplo.com", "Ayudante de Ana", "tiendaficticia", "spammer", "papelerafict", "Analista de datos",
    "vereda", "esperanza", "abuela", "zuleta", "Encabezado Suelto",
)


def _usec(momento: datetime) -> int:
    return int(momento.timestamp() * 1_000_000)


def _escribir_zip(ruta: Path, archivos: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as zf:
        for nombre, contenido in archivos.items():
            datos = contenido.encode("utf-8") if isinstance(contenido, str) else contenido
            zf.writestr(zipfile.ZipInfo(nombre, date_time=(2026, 10, 17, 17, 9, 49)), datos)
    return ruta


def _historial_de(visitas: list[tuple[str, str, int, int]]) -> str:
    """``(url, título, día, minuto)`` → ``Historial.json`` (lunes 2025-06-02 03:10 UTC + día)."""
    base = datetime(2025, 6, 2, 3, 10, tzinfo=UTC)
    entradas = [{"url": url, "title": titulo, "time_usec": _usec(base + timedelta(days=dia, minutes=minuto)),
                 "favicon_url": "", "client_id": "cliente-secreto", "page_transition_qualifier": "CLIENT_REDIRECT"}
                for url, titulo, dia, minuto in visitas]
    entradas.sort(key=lambda e: -e["time_usec"])
    return json.dumps({"Browser History": entradas, "Typed Url": [], "Session": [], "Shared Tab Group": []})


# ---------------------------------------------------------------------------
# Constructores de cada producto
# ---------------------------------------------------------------------------


def _historial() -> dict[str, Any]:
    """Visitas sobre todo a las 03:xx UTC (22:xx en Bogotá), repartidas en varios días."""
    base = datetime(2025, 6, 2, 3, 10, tzinfo=UTC)          # lunes 03:10 UTC = domingo 22:10 en Bogotá
    entradas: list[dict[str, Any]] = []

    def visita(url: str, titulo: str, dia: int, minuto: int = 0) -> None:
        entradas.append({"url": url, "title": titulo, "time_usec": _usec(base + timedelta(days=dia, minutes=minuto)),
                         "favicon_url": "https://www.google.com/favicon.ico", "client_id": "cliente-secreto",
                         "page_transition_qualifier": "CLIENT_REDIRECT"})

    for dia in range(4):
        visita("https://www.youtube.com/watch?v=abc", "tutorial de python para principiantes - YouTube", dia, 1)
        visita("https://www.youtube.com/watch?v=def", "receta de arepas caseras - YouTube", dia, 2)
        visita("https://www.youtube.com/watch?v=ghi", "arepas rellenas tutorial facil - YouTube", dia, 3)
        visita("https://www.youtube.com/watch?v=jkl", "las mejores arepas del mundo con tutorial - YouTube", dia, 4)
        visita("https://github.com/usuario-secreto/repo?token=abc123", "repositorio de prisma y docker", dia, 5)
        visita("https://github.com/otro", "servidor nginx con docker en un vps", dia, 6)
        visita("https://www.google.com/search?q=receta+xyzzyplugh",
               "receta xyzzyplugh secreta - Buscar con Google", dia, 7)
        visita("https://mail.google.com/mail/u/0/#inbox", f"Recibidos (3) - {CUENTA} - Gmail", dia, 8)
        visita("https://www.facebook.com/pedro", "Pedro Ficticio | Facebook", dia, 9)
        for minuto in (10, 15):
            visita("https://www.bancolombia.com/personas", "saldo de la cuenta de ahorros", dia, minuto)
            visita("https://es.pornhub.com/view", "video adulto", dia, minuto + 1)
        visita("https://es.wikipedia.org/wiki/Migrana", "sintomas de migrana cronica - Wikipedia", dia, 12)
        visita("https://www.youtube.com/watch?v=mno", "Entrevista a Gertrudis Ficticia - YouTube", dia, 13)
        visita("https://claude.ai/chat/1", "Ayuda con un script de powershell", dia, 14)
        visita("https://www.google.com/maps/place/x/@4.6097,-74.0817,17z",
               "casa de la abuela vereda la esperanza - Google Maps", dia, 16)
    for minuto in (20, 21, 22):                             # el domingo (en Bogotá) es el día pico
        visita("https://www.youtube.com/watch?v=pqr", "musica para programar - YouTube", 0, minuto)
    # Excluidas por diseño: localhost, IP literal, puerto explícito y chrome-extension.
    visita("http://localhost:3000/panel", "Panel local", 0)
    visita("http://127.0.0.1/admin", "Admin", 0)
    visita("https://ejemplo.com:8443/x", "Puerto raro", 0)
    visita("chrome-extension://abc/opciones.html", "Opciones", 0)
    entradas.sort(key=lambda e: -e["time_usec"])
    return {
        "Browser History": entradas,
        "Typed Url": [],
        "Session": [{"session_tag": "s", "header": {"client_name": "PC-SECRETO"}}],
        "Shared Tab Group": [],
    }


FAVORITOS = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="0" LAST_MODIFIED="1700000000">Bookmarks bar</H3>
    <DL><p>
        <DT><A HREF="https://github.com/usuario-secreto" ADD_DATE="1700000000" ICON_URI="data:image/png;base64,AAAA">mis repositorios de codigo</A>
        <DT><A HREF="https://github.com/otro" ADD_DATE="1700000001">recetas de codigo limpio</A>
        <DT><A HREF="https://www.freecodecamp.org/" ADD_DATE="1700000001">aprender codigo gratis</A>
        <DT><A HREF="https://dev.to/x" ADD_DATE="1700000001">articulos de codigo</A>
        <DT><A HREF="https://www.w3schools.com/" ADD_DATE="1700000001">codigo de ejemplo</A>
        <DT><A HREF="https://docs.python.org/3/" ADD_DATE="1700000002">documentacion de python</A>
        <DT><A HREF="https://www.python.org/" ADD_DATE="1700000003">python</A>
        <DT><A HREF="https://www.google.com/maps/place/y" ADD_DATE="1700000003">finca de la abuela vereda la esperanza</A>
        <DT><H3 ADD_DATE="1700000004" LAST_MODIFIED="1700000005">Proyectos</H3>
        <DL><p>
            <DT><A HREF="https://www.bancolombia.com/" ADD_DATE="1700000006">banco</A>
            <DT><A HREF="about:blank" ADD_DATE="1700000007">vacio</A>
        </DL><p>
    </DL><p>
</DL><p>
"""


def _extensiones() -> dict[str, Any]:
    return {
        "Extensions": [
            {"id": "cjpalhdlnbpafiamejdnhcphjbkeiagm", "version": "1.0", "enabled": True, "incognito_enabled": False,
             "remote_install": False, "disable_reasons": 0, "update_url": "https://clients2.google.com/x"},
            {"id": "nkbihfbeogaeaoehlefnkodbefgpgknn", "enabled": True},          # MetaMask: finanzas
            {"id": "a" * 32, "name": "Lector de Atajos", "enabled": True},
            {"id": "b" * 32, "enabled": True},                                   # sin nombre ni id conocido
            {"id": "fmkadmapgofadopljbjfkapdkoienihi", "enabled": False},        # desactivada
        ],
        "Extension Settings": [{"extension_id": "x", "key": "k", "value": "PC-SECRETO"}],
    }


GEMS = ("<html><body><h1>Gems</h1><h2>Encabezado Suelto</h2><ul><li>Nombre: Tutor de Python</li>"
        "<li>Name: Ayudante de Ana</li><li>Nombre: Mi doctor de sintomas</li>"
        "<li>Instrucciones: eres el asistente de Gertrudis</li></ul></body></html>")


def _zip_chrome_y_cuenta(tmp_path: Path, nombre: str = f"{SELLO}-1-001.zip", *,
                         historial: str | None = None) -> Path:
    return _escribir_zip(tmp_path / nombre, {
        "Takeout/Chrome/Historial.json": historial if historial is not None else json.dumps(_historial()),
        "Takeout/Chrome/Favoritos.html": FAVORITOS,
        "Takeout/Chrome/Lista de lectura.html": "<!DOCTYPE NETSCAPE-Bookmark-file-1><TITLE>Reading List</TITLE>",
        "Takeout/Chrome/Extensiones.json": json.dumps(_extensiones()),
        "Takeout/Chrome/Direcciones y más.json": json.dumps(
            {"Autofill": [{"address": "Calle Falsa 123", "phone": "3001234567"}]}),
        "Takeout/Chrome/Diccionario.csv": "palabrasecretadiccionario\n",
        "Takeout/Chrome/Configuración.json": json.dumps({"Preferences": [{"name": "x", "value": "PC-SECRETO"}]}),
        "Takeout/Chrome/Información del dispositivo.json": json.dumps({"Device Info": [{"client_name": "PC-SECRETO"}]}),
        "Takeout/Contactos/Mis contactos/Mis contactos.vcf":
            "BEGIN:VCARD\nFN:Zacarías Contacto\nTEL:3009876543\nEND:VCARD\n",
        "Takeout/Cuenta de Google/123456789012345.SubscriberInfo.html":
            "<html><body><h1>GOOGLE SUBSCRIBER INFORMATION</h1><ul>"
            "<li>Google Account ID: 123456789012345</li><li>Name: Yoprueba Apellidosecreto</li>"
            "<li>Given Name: Yoprueba</li><li>Family Name: Apellidosecreto</li>"
            f"<li>e-Mail: {CUENTA}</li><li>Recovery SMS: 3001234567</li></ul></body></html>",
        "Takeout/Cuenta de Google/123456789012345.ChangeHistory.html": "<p>CAMBIOSECRETO</p>",
        "Takeout/Gemini/gemini_gems_data.html": GEMS,
        "Takeout/Gemini/gemini_scheduled_actions_data.html": "<p>ACCIONSECRETA</p>",
    })


def _sidecar(segundos: int, **extra: Any) -> str:
    datos = {
        "title": "IMG_0001.jpg", "description": "descripcion secreta de la foto", "imageViews": "3",
        "creationTime": {"timestamp": str(segundos + 60), "formatted": "x"},
        "photoTakenTime": {"timestamp": str(segundos), "formatted": "x"},
        "geoData": {"latitude": 4.6097102, "longitude": -74.0817500, "altitude": 2600.0},
        "geoDataExif": {"latitude": 4.6097102, "longitude": -74.0817500},
        "people": [{"name": "Persona Etiquetada"}],
        "url": "https://photos.google.com/photo/secreta",
        "googlePhotosOrigin": {"mobileUpload": {"deviceFolder": {"localFolderName": ""}, "deviceType": "ANDROID_PHONE"}},
    }
    datos.update(extra)
    return json.dumps(datos)


def _zip_fotos(tmp_path: Path) -> Path:
    archivos: dict[str, bytes | str] = {}
    viernes = datetime(2024, 3, 2, 3, 30, tzinfo=UTC)        # sábado 03:30 UTC = viernes 22:30 en Bogotá
    for i in range(25):
        momento = viernes + timedelta(weeks=i)
        nombre = f"IMG_{i:04d}.jpg" if i < 23 else f"VID_{i:04d}.mp4"
        archivos[f"{FOTOS}/Fotos del {momento.year}/{nombre}"] = b"\xff\xd8 no es una foto"
        archivos[f"{FOTOS}/Fotos del {momento.year}/{nombre}.supplemental-metadata.json"] = _sidecar(
            int(momento.timestamp()))
    martes = int(datetime(2024, 5, 7, 15, 0, tzinfo=UTC).timestamp())
    archivos[f"{FOTOS}/Fotos del 2024/borrada.jpg.supplemental-metadata.json"] = _sidecar(martes, trashed=True)
    archivos[f"{FOTOS}/Fotos del 2024/ajena.jpg.supplemental-metadata.json"] = _sidecar(
        martes, googlePhotosOrigin={"fromSharedAlbum": {}})
    archivos[f"{FOTOS}/Papelera/IMG_9.jpg.supplemental-metadata.json"] = _sidecar(martes)
    archivos[f"{FOTOS}/Fotos del 2024/rota.jpg.supplemental-metadata.json"] = "{no es json"
    return _escribir_zip(tmp_path / f"{SELLO}-2-001.zip", archivos)


def _zip_drive(tmp_path: Path) -> tuple[Path, Path]:
    anidado = io.BytesIO()
    with zipfile.ZipFile(anidado, "w") as zf:
        zf.writestr("secretoanidado.txt", "secretoanidado")
    uno = _escribir_zip(tmp_path / f"{SELLO}-3-001.zip", {
        "Takeout/Drive/Proyecto Amadeus notas.docx": b"PK docx",
        "Takeout/Drive/proyecto robot ideas.pdf": b"%PDF secretoanidado",
        "Takeout/Drive/IMG_20240101_101010.jpg": b"\xff\xd8",
        "Takeout/Drive/CV Juanito Perez.pdf": b"%PDF",
        "Takeout/Drive/anidado.zip": anidado.getvalue(),
        "Takeout/Drive/WhatsApp Image 2024-01-01 at 10.10.10.jpeg": b"\xff\xd8",
        "Takeout/Drive/a3f9c2e1b4d5e6f7a8b9c0d1.png": b"\x89PNG",
        "Takeout/Drive/contrato arriendo zuleta.pdf": b"%PDF",
        "Takeout/Drive/recibo arriendo zuleta.pdf": b"%PDF",
        "Takeout/Drive/paz_y_salvo_zuleta.pdf": b"%PDF",
    })
    tres = _escribir_zip(tmp_path / f"{SELLO}-3-003.zip", {
        "Takeout/Drive/Copia de proyecto final (1).docx": b"PK",
        "Takeout/Drive/Tesis/capitulo uno.docx": b"PK",
        "Takeout/Drive/Tesis/tesis borrador.docx": b"PK",
        "Takeout/Drive/Tesis/tesis final.docx": b"PK",
        "Takeout/Drive/video del viaje.mp4": b"\x00" * 2048,
    })
    return uno, tres


def _etiquetas(*nombres: str, codificar: bool = False) -> str:
    valor = ",".join(nombres)
    return Header(valor, "utf-8").encode() if codificar else valor


def _mensaje(*, desde: str, para: str, etiquetas: str, cuerpo: str, fecha: str,
             extra: dict[str, str] | None = None, html: str | None = None,
             entregado: bool = True) -> bytes:
    lineas = [f"From 1234567890123456789@xxx {fecha.split(', ', 1)[0]} Jan 01 03:15:00 +0000 2024",
              "X-GM-THRID: 1234567890123456789", f"X-Gmail-Labels: {etiquetas}"]
    if entregado:
        lineas.append(f"Delivered-To: {CUENTA}")
    lineas += [f"From: {desde}", f"To: {para}", f"Date: {fecha}", "Subject: =?UTF-8?B?QXN1bnRvIHNlY3JldG8=?=",
               "MIME-Version: 1.0"]
    for clave, valor in (extra or {}).items():
        lineas.append(f"{clave}: {valor}")
    if html is None:
        lineas += ['Content-Type: text/plain; charset="UTF-8"', "Content-Transfer-Encoding: 8bit", "", *cuerpo.split("\n")]
    else:
        lineas += ['Content-Type: multipart/alternative; boundary="LIMITE"', "", "--LIMITE",
                   'Content-Type: text/plain; charset="UTF-8"', "", *cuerpo.split("\n"), "--LIMITE",
                   'Content-Type: text/html; charset="UTF-8"', "", html, "--LIMITE--"]
    return ("\r\n".join(lineas) + "\r\n").encode("utf-8")


# Correos propios a terceros: ≥ 20 palabras propias cada uno, con firma (nombre, cargo y teléfono)
# detrás de la despedida y una cita del correo anterior.
CUERPOS_PROPIOS = (
    "Hola Ana,\n\nTe envío el informe de la semana con los avances del proyecto y las tareas pendientes para el "
    "lunes. ¿Lo revisas cuando puedas? jaja\n\nGracias",
    "Hola,\n\nte comparto las notas de la reunión de ayer y el resumen de lo que acordamos con el equipo de "
    "diseño jaja\n\nGracias",
    "Hola equipo,\n\n¿Quedó listo el despliegue del servidor nuevo? te aviso cuando suba los cambios y revise "
    "que todo funcione bien jaja\n\nGracias",
)
FIRMA = "\nYo Prueba\nAnalista de datos\nCel 300 123 4567"
N_PROPIOS = 24


def _mbox() -> bytes:
    partes: list[bytes] = []
    servicio = {"List-Unsubscribe": "<mailto:baja@github.com>", "Feedback-ID": "1:2:3"}
    for i in range(4):
        partes.append(_mensaje(desde="GitHub <noreply@github.com>", para=CUENTA,
                               etiquetas=_etiquetas("Recibidos", "Categoría: Cambios", "No leídos", codificar=i % 2 == 0),
                               cuerpo="Tu repositorio tiene novedades.", fecha="Mon, 01 Jan 2024 10:00:00 +0000",
                               extra=servicio))
    for _ in range(3):
        partes.append(_mensaje(desde="Tienda <ofertas@tiendaficticia.com.co>", para=CUENTA,
                               etiquetas=_etiquetas("Recibidos", "Categoría: Promociones", "No leídos"),
                               cuerpo="Ofertas.", fecha="Tue, 02 Jan 2024 10:00:00 +0000",
                               extra={"Precedence": "bulk"}))
        partes.append(_mensaje(desde="Mercado Libre <ofertas@mercadolibre.com.co>", para=CUENTA,
                               etiquetas=_etiquetas("Recibidos", "Categoría: Promociones"),
                               cuerpo="Ofertas.", fecha="Tue, 02 Jan 2024 10:00:00 +0000",
                               extra={"List-Unsubscribe": "<mailto:baja@mercadolibre.com.co>"}))
        partes.append(_mensaje(desde="Bancolombia <alertas@bancolombia.com.co>", para=CUENTA,
                               etiquetas=_etiquetas("Recibidos", "Categoría: Cambios"),
                               cuerpo="Movimiento en tu cuenta.", fecha="Tue, 02 Jan 2024 10:00:00 +0000",
                               extra=servicio))
        partes.append(_mensaje(desde="Promo <promo@spammer.com>", para=CUENTA, etiquetas=_etiquetas("Spam"),
                               cuerpo="spamsecreto", fecha="Tue, 02 Jan 2024 10:00:00 +0000", extra=servicio))
        partes.append(_mensaje(desde="Viejo <x@papelerafict.com>", para=CUENTA,
                               etiquetas=_etiquetas("Papelera", "Categoría: Promociones"),
                               cuerpo="papelerasecreto", fecha="Tue, 02 Jan 2024 10:00:00 +0000", extra=servicio))
    for _ in range(2):
        partes.append(_mensaje(desde="Ana Amiga <ana.amiga@gmail.com>", para=CUENTA,
                               etiquetas=_etiquetas("Recibidos", "Categoría: Personal", "Importante"),
                               cuerpo="Quiubo\n\ntexto ajeno secreto\n\nChao", fecha="Wed, 03 Jan 2024 10:00:00 +0000"))
    partes.append(_mensaje(desde=f"Yo Prueba <{CUENTA}>", para="Ana Amiga <ana.amiga@gmail.com>",
                           etiquetas=_etiquetas("Borradores"), cuerpo="borradorsecreto",
                           fecha="Wed, 03 Jan 2024 10:00:00 +0000", entregado=False))
    for i in range(N_PROPIOS):
        cuerpo = CUERPOS_PROPIOS[i % 3] + FIRMA + (
            "\n\nEl lun, 1 ene 2024 a las 10:00, Ana Amiga (<ana.amiga@gmail.com>)\nescribió:\n"
            "> texto citado ajeno\n> más texto citado ajeno")
        html = None if i % 2 else "<div>" + cuerpo.replace("\n", "<br>") + "</div>"
        partes.append(_mensaje(desde=f"Yo Prueba <{CUENTA}>", para="Ana Amiga <ana.amiga@gmail.com>",
                               etiquetas=_etiquetas("Enviado"), cuerpo=cuerpo, html=html,
                               fecha="Mon, 01 Jan 2024 03:15:00 +0000", entregado=False))
    partes.append(_mensaje(desde=f"Yo Prueba <{CUENTA}>", para=CUENTA, etiquetas=_etiquetas("Enviado", "Recibidos"),
                           cuerpo="autonotasecreta", fecha="Mon, 01 Jan 2024 03:15:00 +0000"))
    return b"".join(partes)


def _zip_correo(tmp_path: Path, mbox: bytes | None = None, nombre: str = f"{SELLO}-4-001.zip") -> Path:
    return _escribir_zip(tmp_path / nombre, {
        "Takeout/Correo electrónico/Todo el correo, con Spam y Papelera incluidos.mbox": mbox if mbox is not None
        else _mbox(),
        "Takeout/Correo electrónico/Ajustes de configuración de usuario/Direcciones bloqueadas.json":
            json.dumps({"addresses": ["bloqueado@secreto.com"]}),
    })


@pytest.fixture
def takeout(tmp_path: Path) -> list[Path]:
    carpeta = tmp_path / "descargas"
    carpeta.mkdir()
    uno, tres = _zip_drive(carpeta)
    return [_zip_chrome_y_cuenta(carpeta), _zip_fotos(carpeta), uno, tres, _zip_correo(carpeta)]


def _sin_secretos(texto: str) -> None:
    for secreto in SECRETOS:
        assert secreto.lower() not in texto.lower(), secreto
    assert not re.search(r"https?://|www\.", texto)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", texto)
    assert not re.search(r"\d{7,}", re.sub(r"(?<=\d)[\s.\-()/]+(?=\d)", "", texto))


def _cadenas(valor: Any) -> list[str]:
    if isinstance(valor, dict):
        return [s for v in valor.values() for s in _cadenas(v)]
    if isinstance(valor, list):
        return [s for v in valor for s in _cadenas(v)]
    return [valor] if isinstance(valor, str) else []


def _sin_nombre_pila(perfil: dict[str, Any]) -> str:
    """El perfil serializado sin el campo ``nombre_pila`` ni la fecha (tiene dígitos)."""
    return json.dumps({k: v for k, v in perfil.items() if k not in ("nombre_pila", "generado")}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Perfil completo
# ---------------------------------------------------------------------------


def test_perfil_completo_sin_datos_personales(takeout: list[Path]) -> None:
    perfil = construir_perfil_takeout(takeout, zona=BOGOTA)
    assert perfil["version"] == VERSION_PERFIL_TAKEOUT == 2 and perfil["tipo"] == TIPO_PERFIL
    assert perfil_vigente(perfil) and "exclusiones" not in perfil      # las exclusiones viven aparte
    assert perfil["incluye_sensibles"] is False
    assert perfil["nombre_pila"] == "Yoprueba"
    texto = _sin_nombre_pila(perfil)
    _sin_secretos(texto)
    assert "yoprueba" not in texto.lower()                        # el nombre solo vive en su campo
    assert "prueba" not in texto.lower()                          # ni el apellido del From propio
    fuentes = perfil["fuente"]["fuentes"]
    assert {c: fuentes[c]["estado"] for c in fuentes} == {
        "cuenta": "ok", "historial": "ok", "favoritos": "ok", "extensiones": "ok", "gemini": "ok", "drive": "ok",
        "fotos": "ok", "correo": "ok"}
    assert perfil["fuente"]["zips"] == 5 and perfil["fuente"]["partes_faltantes"] == ["3-002"]
    # Excluidos sin abrir: autocompletar, diccionario, configuración, dispositivo, contactos, ChangeHistory,
    # acciones programadas, direcciones bloqueadas y la papelera de Fotos.
    assert perfil["fuente"]["excluidos_por_diseno"] == 9
    for cadena in _cadenas({k: v for k, v in perfil.items() if k != "nombre_pila"}):
        assert len(cadena) <= 80, cadena


def test_intereses_palabras_y_plataformas(takeout: list[Path]) -> None:
    perfil = construir_perfil_takeout(takeout, zona=BOGOTA)
    temas = [i["tema"] for i in perfil["intereses"]]
    assert "programación y desarrollo" in temas and "cocina y comida" in temas
    assert all(0 < i["peso"] <= 1 for i in perfil["intereses"]) and perfil["intereses"][0]["peso"] == 1.0
    assert not any(i.get("sensible") for i in perfil["intereses"])
    palabras = {p["palabra"] for p in perfil["palabras_clave"]}
    # «tutorial» y «arepa» (con «arepas»): ≥ 3 títulos en ≥ 2 días; «proyecto» y «tesis»: ≥ 3 nombres de
    # Drive; «codigo»: 3 favoritos; «python»: 3 documentos en 2 fuentes (aunque visitara python.org).
    assert {"tutorial", "arepa", "proyecto", "tesis", "codigo", "python"} <= palabras
    # Nunca la búsqueda, el chat de IA, las forjas (dueño/repo), los mapas, nombres ni lugares.
    assert not palabras & {"receta", "script", "powershell", "youtube", "github", "entrevista", "musica",
                           "programar", "repositorio", "docker", "zuleta", "arriendo", "vereda", "casa", "abuela",
                           "esperanza", "arepas"}
    assert all(p["freq"] >= 3 for p in perfil["palabras_clave"])
    dominios = [p["dominio"] for p in perfil["plataformas"]]
    assert dominios == ["youtube.com", "github.com"]           # google.com va en servicios; facebook: 4 visitas
    assert all(p["visitas"] >= 5 and p["dias"] >= 2 for p in perfil["plataformas"])
    assert perfil["servicios_google"] == []                   # Gmail, búsqueda y Maps: 4 visitas cada uno
    assert perfil["sitios_guardados"] == [{"dominio": "github.com", "favoritos": 2},
                                          {"dominio": "python.org", "favoritos": 2}]
    assert perfil["herramientas"] == {"extensiones": ["Lector de Atajos", "uBlock Origin"], "sin_nombre": 1,
                                      "activas": 4}
    assert perfil["gemini"]["gems"] == ["Tutor de Python"]    # sin encabezados, nombres ni salud


def test_ritmo_en_la_zona_del_usuario(takeout: list[Path]) -> None:
    bogota = construir_perfil_takeout(takeout, zona=BOGOTA)
    utc = construir_perfil_takeout(takeout, zona=UTC)
    navegacion = bogota["ritmo"]["navegacion"]
    assert navegacion["hora_pico"] == 22 and navegacion["franja"] == "noche" and navegacion["dia_pico"] == "domingo"
    assert utc["ritmo"]["navegacion"]["hora_pico"] == 3 and utc["ritmo"]["navegacion"]["dia_pico"] == "lunes"
    assert len(navegacion["histograma_horas"]) == 24 and max(navegacion["histograma_horas"]) == 1.0
    fotos = bogota["fotos"]
    assert fotos["eventos"] == 25 and fotos["videos"] == 2                 # sin papelera, ajenas ni rota
    assert fotos["hora_pico"] == 22 and fotos["dia_pico"] == "viernes" and fotos["anio_pico"] == 2024
    assert fotos["excluidas_papelera"] == 1 and fotos["excluidas_ajenas"] == 1
    assert utc["fotos"]["dia_pico"] == "sábado" and utc["fotos"]["hora_pico"] == 3
    enviado = bogota["ritmo"]["correo_enviado"]
    assert enviado["eventos"] == N_PROPIOS + 1 and enviado["hora_pico"] == 22


def test_estilo_solo_de_los_correos_enviados(takeout: list[Path]) -> None:
    correo = construir_perfil_takeout(takeout, zona=BOGOTA)["correo"]
    assert correo["excluidos_spam_papelera"] == 6 and correo["borradores_ignorados"] == 1
    assert correo["enviados"] == N_PROPIOS + 1
    estilo = correo["estilo"]
    assert estilo["suficiente"] is True and estilo["mensajes"] == N_PROPIOS   # sin la nota a sí mismo
    assert estilo["confianza"] == "baja"                                     # < 50 correos
    assert estilo["saludos"] == ["Hola"] and estilo["despedidas"] == ["Gracias"]   # nunca «Quiubo»/«Chao»
    assert estilo["tratamiento"] == "tú" and estilo["formalidad"] == "informal"
    assert 20 <= estilo["longitud_mediana_palabras"] <= 30                   # sin cita ni firma
    assert estilo["muletillas"][0] == {"ngrama": "jaja", "freq": N_PROPIOS}
    assert 0 < estilo["pct_preguntas"] < 1
    servicios = {s["dominio"]: s for s in correo["servicios"]}
    assert servicios["github.com"] == {"dominio": "github.com", "mensajes": 4, "categoria": "actualizaciones"}
    assert servicios["mercadolibre.com.co"]["categoria"] == "promociones"
    # Una tienda desconocida solo se cuenta; un banco (finanzas) y las personas, nunca.
    assert set(servicios) == {"github.com", "mercadolibre.com.co"} and correo["servicios_sin_nombrar"] == 1
    # 15 recibidos (sin Spam, Papelera, borradores ni enviados): etiquetas en RFC 2047 y en UTF-8 crudo.
    assert correo["bandeja"] == {"actualizaciones": 0.47, "promociones": 0.4, "personal": 0.13, "sin_leer": 0.47}


def _enviado(cuerpo: str, i: int = 0) -> bytes:
    return _mensaje(desde=f"Yo <{CUENTA}>", para=f"Cliente {i} <cliente@empresa.co>", etiquetas="Enviado",
                    cuerpo=cuerpo, fecha="Mon, 01 Jan 2024 03:15:00 +0000", entregado=False)


def test_estilo_exige_correos_largos_y_suficientes(tmp_path: Path) -> None:
    # 30 correos de una o dos palabras («ok», «Adjunto»): no hay estilo que aprender.
    cortos = b"".join(_enviado(t) for t in ("ok", "Adjunto", "listo", "Gracias", "Va", "dale") * 5)
    perfil = construir_perfil_takeout([_zip_correo(tmp_path, cortos)], zona=BOGOTA)
    assert perfil["correo"]["estilo"] == {"suficiente": False, "mensajes": 0, "confianza": "insuficiente"}
    assert "correos son" not in resumen_perfil_takeout(perfil)
    # 3 correos largos tampoco bastan (hacen falta 20).
    largo = CUERPOS_PROPIOS[0]
    pocos = b"".join(_enviado(largo) for _ in range(3))
    perfil = construir_perfil_takeout([_zip_correo(tmp_path, pocos, nombre="b.zip")], zona=BOGOTA)
    assert perfil["correo"]["estilo"] == {"suficiente": False, "mensajes": 3, "confianza": "insuficiente"}


def test_despedida_antes_de_la_firma(tmp_path: Path) -> None:
    cuerpo = ("Buenos días,\n\nAdjunto la cotización actualizada con los cambios que pidieron en la reunión del "
              "martes y quedo atento a sus comentarios sobre el alcance.\n\nCordialmente,\nYo Prueba\n"
              "Gerente de proyectos\nCel 300 123 4567\nCalle Falsa 123")
    mbox = b"".join(_enviado(cuerpo, i) for i in range(20))
    estilo = construir_perfil_takeout([_zip_correo(tmp_path, mbox)], zona=BOGOTA)["correo"]["estilo"]
    assert estilo["suficiente"] is True and estilo["mensajes"] == 20 and estilo["confianza"] == "baja"
    assert estilo["despedidas"] == ["Cordialmente"] and estilo["saludos"] == ["Buenos días"]
    assert estilo["longitud_mediana_palabras"] == 26          # sin el nombre, el cargo ni la dirección


def test_resumen_concuerda_el_plural() -> None:
    perfil = {"correo": {"estilo": {"suficiente": True, "longitud_mediana_palabras": 1, "confianza": "baja"}}}
    assert "(~1 palabra)" in resumen_perfil_takeout(perfil)
    hechos = memory_engine.hechos_desde_takeout(perfil, "Okabe")
    assert any("(~1 palabra)" in h for h in hechos)


def test_filtro_de_categorias_sensibles(takeout: list[Path]) -> None:
    filtrado = construir_perfil_takeout(takeout, zona=BOGOTA)
    assert filtrado["fuente"]["filtrados_sensibles"] > 0
    con = construir_perfil_takeout(takeout, zona=BOGOTA, incluir_sensibles=True)
    assert con["incluye_sensibles"] is True
    dominios = {p["dominio"] for p in con["plataformas"]}
    assert {"bancolombia.com", "pornhub.com"} <= dominios
    sensibles = {i["tema"] for i in con["intereses"] if i.get("sensible")}
    assert "finanzas personales" in sensibles
    assert "bancolombia.com.co" in {s["dominio"] for s in con["correo"]["servicios"]}
    assert "MetaMask" in con["herramientas"]["extensiones"]
    assert "Mi doctor de sintomas" in con["gemini"]["gems"]


def test_ningun_archivo_excluido_se_abre(takeout: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    abiertos: list[str] = []
    original = zipfile.ZipFile.open

    def espia(self: zipfile.ZipFile, nombre: Any, *args: Any, **kwargs: Any) -> Any:
        abiertos.append(nombre.filename if isinstance(nombre, zipfile.ZipInfo) else str(nombre))
        return original(self, nombre, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", espia)
    construir_perfil_takeout(takeout, zona=BOGOTA)
    assert abiertos
    prohibidos = ("Direcciones y más", "Diccionario", "Configuración", "Información del dispositivo", "Contactos",
                  "ChangeHistory", "scheduled_actions", "Direcciones bloqueadas", "/Drive/", "Papelera/")
    for nombre in abiertos:
        assert not any(p in nombre for p in prohibidos), nombre
        assert not nombre.endswith((".jpg", ".mp4", ".pdf", ".docx", ".zip", ".png", ".jpeg", ".vcf"))


# ---------------------------------------------------------------------------
# Privacidad: nombres de terceros, lugares, categorías especiales y números
# ---------------------------------------------------------------------------

TERCEROS = ("cardozo", "yolima", "bustamante", "yeimy", "mariaperez", "torvalds", "canalamigo", "cuartas",
            "johan", "roundcube", "cotizacion", "mirador", "bellavista", "chapinero")


def _takeout_hostil(carpeta: Path) -> list[Path]:
    """Nombres de terceros en minúscula por todas partes (Docs, Drive, favoritos, webmail, GitHub,
    canales de YouTube, Gems, extensiones, boletines en dominio propio) y lugares en Maps."""
    visitas: list[tuple[str, str, int, int]] = []
    for dia in range(6):
        visitas += [
            ("https://drive.google.com/file/d/A/view", f"paz_y_salvo_bustamante_cardozo_{dia}.pdf - Google Drive", dia, 1),
            ("https://docs.google.com/document/d/B/edit", f"contrato arriendo bustamante cardozo {dia} - Documentos de Google",
             dia, 2),
            ("https://webmail.miempresa-ficticia.co/?_task=mail&_uid=1", f"Roundcube Webmail :: cotizacion para yolima "
             f"cardozo {dia}", dia, 3),
            ("https://correo.otra-ficticia.co/inbox", f"cotizacion yolima cardozo {dia}", dia, 4),
            ("https://github.com/mariaperez/proyecto/issues/1", f"arreglo del login #{dia} · Issue · mariaperez/proyecto",
             dia, 5),
            ("https://github.com/torvalds/linux", "GitHub - torvalds/linux: Linux kernel source tree", dia, 6),
            ("https://www.youtube.com/@canalamigo", f"({dia + 2}) canalamigo - YouTube", dia, 7),
            ("https://www.google.com/maps/place/x", f"conjunto residencial mirador de bellavista torre {dia} - Google Maps",
             dia, 8),
            ("https://maps.google.com/maps?ll=1,2", f"escort chapinero mirador {dia}", dia, 9),
            ("https://www.waze.com/live-map", f"mirador de bellavista {dia} - Waze", dia, 10),
            ("https://www.alquile.com.co/admin", f"panel de johan cuartas reporte {dia}", dia, 11),
            ("https://yolimacardozo.com/blog", f"blog de yolima cardozo {dia}", dia, 12),
            ("https://www.youtube.com/watch?v=1", f"tutorial de react desde cero {dia} - YouTube", dia, 13),
        ]
    favoritos = """<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p>
      <DT><H3>clientes yolima cardozo</H3><DL><p>
        <DT><A HREF="https://drive.google.com/drive/folders/1">expediente bustamante cardozo</A>
        <DT><A HREF="https://www.google.com/maps/place/y">casa vereda bellavista</A>
        <DT><A HREF="https://yolimacardozo.com/">yolima cardozo</A>
        <DT><A HREF="https://yolimacardozo.com/b">tienda de yolima cardozo</A>
      </DL><p><DT><H3>clientes yolima cardozo</H3><DT><H3>cardozo bustamante</H3></DL><p>"""
    extensiones = {"Extensions": [
        {"id": "b" * 32, "name": "Panel de Yolima Cardozo", "enabled": True},
        {"id": "c" * 32, "name": "Bot WhatsApp 300-123-4567", "enabled": True},
        {"id": "d" * 32, "name": "Notas CC 1.036.123.456", "enabled": True},
        {"id": "e" * 32, "name": "Rastreador de ovulacion", "enabled": True},
        {"id": "f" * 32, "name": "Dark Reader", "enabled": True},
    ]}
    gems = ("<html><body><h1>Gems</h1><h2>Asesor caso Yeimy Bustamante</h2><ul><li>Nombre: Asesor caso Yolima "
            "Cardozo</li><li>Nombre: Coach para mi tdah</li><li>Nombre: Planificador de fertilidad</li>"
            "<li>Nombre: Tutor de React</li><li>Nombre: Revisor de codigo</li></ul></body></html>")
    uno = _escribir_zip(carpeta / f"{SELLO}-1-001.zip", {
        "Takeout/Chrome/Historial.json": _historial_de(visitas),
        "Takeout/Chrome/Favoritos.html": favoritos,
        "Takeout/Chrome/Extensiones.json": json.dumps(extensiones),
        "Takeout/Cuenta de Google/1.SubscriberInfo.html": "<ul><li>Given Name: Johan</li><li>Family Name: Cuartas</li></ul>",
        "Takeout/Gemini/gemini_gems_data.html": gems,
    })
    drive = {f"Takeout/Drive/{n}": b"x" for n in (
        "paz_y_salvo_bustamante_cardozo.pdf", "recibo_bustamante_cardozo_marzo.pdf",
        "contrato_arriendo_bustamante_cardozo.pdf", "clientes/yeimy bustamante/soportes.pdf",
        "clientes/yeimy bustamante/pagos.pdf", "familia/yeimy bustamante 2/otros.pdf")}
    tres = _escribir_zip(carpeta / f"{SELLO}-3-001.zip", drive)
    mbox: list[bytes] = []
    for i in range(4):
        mbox.append(_mensaje(desde="Yolima <hola@yolimacardozo.com>", para=CUENTA, etiquetas="Recibidos",
                             cuerpo="texto ajeno secreto", fecha="Mon, 05 May 2025 14:00:00 +0000",
                             extra={"List-Id": "<boletin.yolimacardozo.com>"}))
        mbox.append(_mensaje(desde="Yeimy Bustamante <yeimy.bustamante@gmail.com>", para=CUENTA, etiquetas="Recibidos",
                             cuerpo="texto ajeno secreto", fecha="Mon, 05 May 2025 14:00:00 +0000"))
        mbox.append(_mensaje(desde=f"Johan Cuartas <{CUENTA}>", para="Cliente <cliente@empresa.co>",
                             etiquetas="Enviado", cuerpo="Hola", fecha="Mon, 05 May 2025 14:00:00 +0000",
                             entregado=False))
    cuatro = _zip_correo(carpeta, b"".join(mbox))
    return [uno, tres, cuatro]


def test_nombres_de_terceros_y_lugares_nunca_salen(tmp_path: Path) -> None:
    perfil = construir_perfil_takeout(_takeout_hostil(tmp_path), zona=BOGOTA)
    texto = _sin_nombre_pila(perfil).lower()
    resumen = resumen_perfil_takeout(perfil).lower()
    hechos = " ".join(memory_engine.hechos_desde_takeout(perfil, "Lab Mem 001")).lower()
    for nombre in TERCEROS:
        assert nombre not in texto, nombre
        assert nombre not in resumen and nombre not in hechos, nombre
    for sensible in ("tdah", "fertilidad", "ovulacion", "escort", "300-123", "1.036"):
        assert sensible not in texto, sensible
    assert {p["palabra"] for p in perfil["palabras_clave"]} == {"tutorial", "react", "reporte"}
    assert perfil["herramientas"]["extensiones"] == ["Dark Reader"]
    assert perfil["gemini"]["gems"] == ["Tutor de React", "Revisor de codigo"]
    assert [p["dominio"] for p in perfil["plataformas"]] == ["github.com", "youtube.com", "alquile.com.co"]
    # yolimacardozo.com, los dos webmails propios y waze.com: contados, nunca nombrados.
    assert perfil["sitios_sin_nombrar"] == 4
    assert perfil["correo"]["servicios"] == [] and perfil["correo"]["servicios_sin_nombrar"] == 1
    # Google Maps sí cuenta como servicio usado (sin ningún lugar).
    assert {s["servicio"] for s in perfil["servicios_google"]} >= {"Google Maps", "Google Drive", "Google Docs"}


def test_lexico_sensible_ampliado() -> None:
    for texto in ("tdah en adultos", "trastorno bipolar", "metilfenidato", "resultados de biopsia", "aborto legal",
                  "fertilidad", "rastreador de ovulacion", "hipoteca o leasing", "escort prepago",
                  "ecografia obstetrica", "psiquiatria infantil", "radiografia de torax",
                  "resultados examenes laboratorio", "antidepresivos efectos", "hipoteca apartamento",
                  "desalojo inquilino", "restitucion de inmueble", "pacto historico", "colombia humana",
                  "demanda laboral", "politica colombiana"):
        assert ti._sensibles_de(texto), texto
    # Palabras ambiguas sin contexto sensible: no se filtran.
    for texto in ("God of War Ragnarok", "herencia en python", "oferta y demanda", "Doctor Who",
                  "politica de privacidad", "sucesion de fibonacci"):
        assert not ti._sensibles_de(texto), texto
    sensibles = {"erome.com": "sexualidad", "f95zone.to": "sexualidad", "missav.com": "sexualidad",
                 "cruzverde.com.co": "salud", "farmatodo.com.co": "salud", "larebajavirtual.com": "salud",
                 "locatel.com.co": "salud", "profamilia.org.co": "salud", "addi.com": "finanzas personales",
                 "segurossura.com.co": "salud", "tdah-info.com": "salud", "pornhub.com": "sexualidad",
                 "pactohistorico.co": "política", "dian.gov.co": "legal y trámites", "jw.org": "religión"}
    for dominio, categoria in sensibles.items():
        assert ti._sensible_de_dominio(dominio) == categoria, dominio
    for dominio in ("sussex.ac.uk", "essex.ac.uk", "lasexta.com", "occidental.edu", "partidosdelaliga.com",
                    "petrobras.com", "github.com", "nasa.gov", "medium.com"):
        assert ti._sensible_de_dominio(dominio) is None, dominio


def test_numeros_con_separadores() -> None:
    for texto in ("Cel 300 123 4567", "CC 1.036.123.456", "Bot 300-123-4567", "(300) 123-4567", "+57 300 1234567"):
        assert ti._etiqueta_segura(texto) == "", texto
        assert wsp_ingestor._item(texto) == "", texto
        assert memory_engine._item_perfil(texto) == "", texto
    assert ti._etiqueta_segura("Python 3.12") == "Python 3.12"
    perfil = {"palabras_clave": [{"palabra": "x 300-123-4567"}, {"palabra": "python"}]}
    ti._verificar_privacidad(perfil, None)
    assert perfil["palabras_clave"] == [{"palabra": "python"}]


def test_palabras_clave_de_vocabulario_cerrado() -> None:
    terminos = [t for t, _ in ti._terminos_vocabulario("Inteligencia Artificial Explicada Con ReactJS Y Steins;Gate")]
    assert terminos == ["inteligencia artificial", "react", "steins gate"]      # frases, alias y Title Case
    assert [t for t, _ in ti._terminos_vocabulario("los mejores juegos y el juego de anime")] == ["juego", "juego",
                                                                                                    "anime"]
    assert ti._terminos_vocabulario("mariaperez canalamigo zuleta vereda") == []
    assert not ti._clave_permitida("python", frozenset({"python"}))           # un nombre de las cabeceras
    assert ti._limpiar_titulo("(3) canal amigo - YouTube", ti._analizar_url("https://www.youtube.com/x")) == \
        "canal amigo"
    assert ti._limpiar_titulo("python - How to create a list - Stack Overflow",
                              ti._analizar_url("https://stackoverflow.com/q/1")) == "python - How to create a list"
    assert ti._limpiar_titulo("Issues · torvalds/linux", None) == "Issues ·"


def test_plataformas_por_dias_sin_infraestructura(tmp_path: Path) -> None:
    visitas: list[tuple[str, str, int, int]] = []
    for dia in range(6):
        for minuto in range(3):
            visitas.append(("https://www.googleadservices.com/pagead/aclk", "", dia, minuto))
            visitas.append(("https://t.co/abc", "", dia, 10 + minuto))
            visitas.append(("https://login.microsoftonline.com/x", "Iniciar sesión", dia, 20 + minuto))
            visitas.append(("https://accounts.google.com/x", "Cuenta", dia, 30 + minuto))
            visitas.append(("https://www.google.com/search?q=x", "x - Buscar con Google", dia, 40 + minuto))
            visitas.append(("https://desconocido-ficticio.com/a", "pagina", dia, 50 + minuto))
        visitas.append(("https://github.com/x", "x", dia, 59))                # 6 visitas en 6 días
    for minuto in range(20):
        visitas.append(("https://www.youtube.com/watch", "video", 0, 60 + minuto))   # 20 visitas en 1 día…
    visitas.append(("https://www.youtube.com/watch", "video", 1, 0))                  # …y 1 en otro
    zipeado = _escribir_zip(tmp_path / f"{SELLO}-1-001.zip", {"Takeout/Chrome/Historial.json": _historial_de(visitas)})
    perfil = construir_perfil_takeout([zipeado], zona=BOGOTA)
    assert [p["dominio"] for p in perfil["plataformas"]] == ["github.com", "youtube.com"]   # por días
    assert perfil["sitios_sin_nombrar"] == 1
    assert perfil["servicios_google"] == [{"servicio": "Búsqueda de Google", "visitas": 18}]


def test_cabeceras_con_comas_corchetes_y_dos_categorias(tmp_path: Path) -> None:
    assert ti._remitente("=?UTF-8?Q?P=C3=A9rez=2C_Juan?= <juan@empresa.co>") == ("Pérez, Juan", "juan@empresa.co")
    assert ti._remitente("Pérez, Juan <juan@empresa.co>")[1] == "juan@empresa.co"
    assert ti._remitente("=?UTF-8?Q?=5BAlertas=5D_Plataforma?= <noreply@plataforma.io>")[1] == "noreply@plataforma.io"
    assert ti._remitente("=?UTF-8?Q?usuario=40dominio_via_LinkedIn?= <messages-noreply@linkedin.com>")[1] == \
        "messages-noreply@linkedin.com"
    partes = []
    for _ in range(30):
        partes.append(_mensaje(desde="Tienda <noreply@mercadolibre.com.co>", para=CUENTA,
                               etiquetas=_etiquetas("Categoría Compras", "Categoría: Personal", "Recibidos",
                                                    codificar=True),
                               cuerpo="compra", fecha="Mon, 01 Jan 2024 10:00:00 +0000",
                               extra={"List-Unsubscribe": "<mailto:x@mercadolibre.com.co>"}))
    for _ in range(5):
        partes.append(_mensaje(desde="=?UTF-8?Q?=5BAlertas=5D_GitHub?= <noreply@github.com>", para=CUENTA,
                               etiquetas="Recibidos", cuerpo="alerta", fecha="Mon, 01 Jan 2024 10:00:00 +0000",
                               extra={"List-Unsubscribe": "<mailto:x@github.com>"}))
    for _ in range(3):
        partes.append(_mensaje(desde=f"=?UTF-8?Q?P=C3=A9rez=2C_Juan?= <{CUENTA}>", para="Cliente <c@empresa.co>",
                               etiquetas="Enviado", cuerpo="Hola", fecha="Mon, 01 Jan 2024 10:00:00 +0000",
                               entregado=False))
    correo = construir_perfil_takeout([_zip_correo(tmp_path, b"".join(partes))], zona=BOGOTA)["correo"]
    assert correo["enviados"] == 3                                 # el From propio con coma se reconoce
    assert correo["bandeja"]["compras"] == 0.86                    # prioridad fija, no el orden de un set
    assert {s["dominio"]: s["categoria"] for s in correo["servicios"]} == {
        "mercadolibre.com.co": "compras", "github.com": "otros"}


def test_gems_solo_de_campos_etiquetados(tmp_path: Path) -> None:
    gems = ("<html><body><h1>Gems</h1><h3>Instrucciones</h3><p>Name: Tutor de Python</p>"
            "<table><tr><td>Nombre: Experto en Docker</td></tr></table><p>Descripción: ayuda con docker</p>"
            "<li>Nombre: Gem 1234</li></body></html>")
    zipeado = _escribir_zip(tmp_path / f"{SELLO}-1-001.zip", {"Takeout/Gemini/gemini_gems_data.html": gems})
    perfil = construir_perfil_takeout([zipeado], zona=BOGOTA)
    assert perfil["gemini"] == {"gems": ["Tutor de Python", "Experto en Docker"], "n": 2}


def test_perfil_corrupto_con_numeros_enormes() -> None:
    perfil = {"correo": {"estilo": {"suficiente": True, "longitud_mediana_palabras": 10**400}},
              "ritmo": {"navegacion": {"eventos": 10**400, "hora_pico": 3}},
              "fotos": {"eventos": 10**400, "hora_pico": 3, "anio_pico": 10**400}}
    assert isinstance(resumen_perfil_takeout(perfil), str)
    assert len(memory_engine.hechos_desde_takeout(perfil, "Okabe")) >= 3


# ---------------------------------------------------------------------------
# Dedupe, partes y tolerancia a fallos
# ---------------------------------------------------------------------------


def test_dedupe_de_zips_identicos(tmp_path: Path) -> None:
    original = _zip_chrome_y_cuenta(tmp_path)
    copia = tmp_path / f"{SELLO}-1-001 (1).zip"
    shutil.copyfile(original, copia)
    una = construir_perfil_takeout([original], zona=BOGOTA)
    tres = construir_perfil_takeout([original, copia, original], zona=BOGOTA)
    assert tres["fuente"]["zips"] == 1 and tres["fuente"]["zips_duplicados"] == 2
    assert tres["plataformas"] == una["plataformas"]                       # nada contado dos veces
    assert tres["ritmo"]["navegacion"]["eventos"] == una["ritmo"]["navegacion"]["eventos"]
    assert tres["fuente"]["partes_faltantes"] == []


def test_partes_faltantes() -> None:
    nombres = [f"{SELLO}-3-001.zip", f"{SELLO}-3-002.zip", f"{SELLO}-3-004.zip", f"{SELLO}-3-004 (1).zip",
               f"{SELLO}-2-001.zip", "otra-cosa.zip"]
    assert ti._partes_faltantes(nombres) == ["3-003"]


def test_fuente_rota_no_tumba_a_las_demas(tmp_path: Path) -> None:
    roto = _zip_chrome_y_cuenta(tmp_path, historial="{esto no es json")
    no_zip = tmp_path / f"{SELLO}-9-001.zip"
    no_zip.write_bytes(b"no soy un zip")
    perfil = construir_perfil_takeout([roto, no_zip, _zip_fotos(tmp_path), tmp_path / "inexistente.zip"],
                                      zona=BOGOTA)
    fuentes = perfil["fuente"]["fuentes"]
    assert fuentes["historial"]["estado"] == "error" and fuentes["historial"]["motivo"] == "JSONDecodeError"
    assert fuentes["favoritos"]["estado"] == "ok" and fuentes["fotos"]["estado"] == "ok"
    assert fuentes["correo"]["estado"] == "ausente"
    assert perfil["fuente"]["zips_ilegibles"] == 2 and perfil["fuente"]["zips"] == 2
    assert perfil["fotos"]["eventos"] == 25 and perfil["plataformas"] == []
    assert perfil["ritmo"]["navegacion"] == {"eventos": 0}


def test_progreso_y_callback_roto(takeout: list[Path]) -> None:
    mensajes: list[str] = []
    construir_perfil_takeout(takeout, zona=BOGOTA, progreso=mensajes.append)
    assert any("Índice listo" in m for m in mensajes) and any("Gmail" in m for m in mensajes)
    _sin_secretos("\n".join(mensajes))

    def roto(_: str) -> None:
        raise RuntimeError("callback roto")

    assert construir_perfil_takeout(takeout[:1], zona=BOGOTA, progreso=roto)["fuente"]["zips"] == 1


# ---------------------------------------------------------------------------
# Piezas sueltas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "esperado"), [
    ("mail.google.com", "google.com"), ("www.eltiempo.com", "eltiempo.com"), ("tienda.ejemplo.com.co", "ejemplo.com.co"),
    ("alguien.github.io", "github.io"), ("bbc.co.uk", "bbc.co.uk"), ("localhost", None), ("127.0.0.1", None),
    ("::1", None), ("intranet", None), ("pc.local", None), ("com.co", None), ("", None),
])
def test_dominio_registrable(host: str, esperado: str | None) -> None:
    assert dominio_registrable(host) == esperado


def test_mbox_en_streaming_con_crlf_y_lineas_gigantes() -> None:
    # Una línea gigante se lee en trozos: el trozo que empieza por "From …" no es un separador.
    gigante = b"x" * ti.MAX_LINEA_MBOX + b"From 9@xxx Mon Jan 01 00:00:00 +0000 2024"
    datos = (b"From 1@xxx Mon Jan 01 00:00:00 +0000 2024\r\nX-Gmail-Labels: Enviado\r\nFrom: a@b.co\r\n\r\n"
             b"linea uno\r\n>From escapado\r\n" + gigante + b"\r\nFrom no es separador\r\n"
             b"From 2@xxx Tue Jan 02 00:00:00 +0000 2024\r\nX-Gmail-Labels: Spam\r\n"
             b"From 3@xxx Wed Jan 03 00:00:00 +0000 2024\r\nX-Gmail-Labels: Recibidos\r\n\r\ncuerpo\r\n")
    cabeceras: list[bytes] = []
    cuerpos: list[bytes] = []

    def al_cabeceras(cab: bytes, separador: bytes) -> Any:
        cabeceras.append(cab)
        return cab if b"Enviado" in cab else None

    def al_cerrar(estado: Any, cuerpo: bytes) -> None:
        cuerpos.append(cuerpo)

    n = ti._recorrer_mbox(io.BufferedReader(io.BytesIO(datos)), al_cabeceras, al_cerrar)
    assert n == 3 and len(cabeceras) == 3
    assert len(cuerpos) == 1                                               # solo el enviado guarda cuerpo
    assert cuerpos[0].startswith(b"linea uno\r\nFrom escapado\r\n")
    assert b"From 9@xxx" in cuerpos[0] and b"From no es separador" in cuerpos[0]
    # El cuerpo propio tiene tope: lo que sobra se descarta sin guardarse.
    enorme = (b"From 1@xxx Mon Jan 01 00:00:00 +0000 2024\r\nX-Gmail-Labels: Enviado\r\n\r\n"
              + (b"y" * 1000 + b"\r\n") * 5000)
    cuerpos.clear()
    assert ti._recorrer_mbox(io.BufferedReader(io.BytesIO(enorme)), al_cabeceras, al_cerrar) == 1
    assert ti.MAX_CUERPO_ENVIADO <= len(cuerpos[0]) <= ti.MAX_CUERPO_ENVIADO + 1002


def test_texto_propio_quita_citas_y_firmas() -> None:
    texto = ("Hola,\nva el archivo.\n\n-- \nFirma Secreta\n")
    assert ti._texto_propio(texto) == "Hola,\nva el archivo."
    texto = "Listo, gracias\n\nOn Mon, Jan 1, 2024 at 10:00 AM Someone <a@b.co>\nwrote:\n> citado"
    assert ti._texto_propio(texto) == "Listo, gracias"
    texto = "Va.\n\nEnviado desde mi iPhone\n\nDe: Alguien\nEnviado: lunes\n> viejo"
    assert ti._texto_propio(texto) == "Va."
    assert ti._texto_sin_firma("Saludos,\nlinea uno\nCordialmente,\nYo\nCargo") == (
        "Saludos,\nlinea uno\nCordialmente,", "cordialmente")


def test_resumen_natural_sin_datos_personales(takeout: list[Path]) -> None:
    perfil = construir_perfil_takeout(takeout, zona=BOGOTA)
    resumen = resumen_perfil_takeout(perfil)
    assert 0 < len(resumen) <= MAX_RESUMEN
    _sin_secretos(resumen)
    assert "Yoprueba" not in resumen
    assert "Por su actividad en Google le interesan" in resumen
    assert "Sitios que más usa: youtube.com, github.com" in resumen
    assert "Navega sobre todo hacia las 22 h (noche); su día más activo es el domingo." in resumen
    assert "saluda con «Hola»" in resumen and "confianza baja" in resumen
    assert "Toma más fotos los viernes hacia las 22 h; el año con más fotos fue 2024." in resumen
    assert resumen_perfil_takeout(perfil, max_chars=120) and len(resumen_perfil_takeout(perfil, max_chars=120)) <= 120
    assert resumen_perfil_takeout({}) == "Perfil de Google con pocos datos."
    assert resumen_perfil_takeout("no es un perfil") == ""                 # type: ignore[arg-type]
    # Un perfil manipulado no cuela URLs, correos ni números largos en el resumen.
    malo = {"intereses": [{"tema": "https://malo.example/x"}, {"tema": "a@b.co"}, {"tema": "3001234567"},
                          {"tema": "300-123-4567"}, {"tema": "música"}]}
    assert resumen_perfil_takeout(malo) == "Por su actividad en Google le interesan: música."


def test_quitar_temas_y_exclusiones_ofuscadas(takeout: list[Path]) -> None:
    exclusiones = Exclusiones()
    perfil = construir_perfil_takeout(takeout, zona=BOGOTA, excluir=["arepa"], exclusiones=exclusiones)
    assert "arepa" not in {p["palabra"] for p in perfil["palabras_clave"]}
    quitados: list[str] = []
    n = quitar_temas(perfil, ["cocina", "github.com", "mercado"], exclusiones, quitados=quitados)
    assert n >= 3 and {"cocina y comida", "github.com", "mercadolibre.com.co"} <= set(quitados)
    assert "cocina y comida" not in {i["tema"] for i in perfil["intereses"]}
    assert "github.com" not in {p["dominio"] for p in perfil["plataformas"]}
    assert "mercadolibre.com.co" not in {s["dominio"] for s in perfil["correo"]["servicios"]}   # pegado al dominio
    datos = exclusiones.a_dict()
    texto = json.dumps(datos)
    assert all(re.fullmatch(r"[0-9a-f]{32}", h) for h in datos["hashes"])
    assert "arepa" not in texto and "cocina" not in texto and "mercado" not in texto   # nunca el pedido
    # Sin la clave, un diccionario de SHA-256 no las revierte (eran sha256[:16] sin sal).
    sin_sal = {hashlib.sha256(p.encode()).hexdigest()[:16] for p in ("arepa", "cocina", "mercado")}
    assert not sin_sal & {h[:16] for h in datos["hashes"]}
    # Regenerar con el mismo almacén respeta todo (también el dominio completo).
    otra = construir_perfil_takeout(takeout, zona=BOGOTA, exclusiones=exclusiones)
    assert "mercadolibre.com.co" not in {s["dominio"] for s in otra["correo"]["servicios"]}
    assert "github.com" not in {p["dominio"] for p in otra["plataformas"]}


def test_guardar_y_cargar(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ruta = tmp_path / "john" / "perfil_takeout.json"
    perfil = {"version": VERSION_PERFIL_TAKEOUT, "tipo": TIPO_PERFIL, "intereses": [{"tema": "música", "peso": 1.0}]}
    guardar_perfil(perfil, ruta)
    assert ruta.read_bytes().decode("utf-8").endswith("}\n") and b"\r" not in ruta.read_bytes()
    assert cargar_perfil_takeout(ruta) == perfil
    assert not list(ruta.parent.glob("*.tmp"))
    assert cargar_perfil_takeout(tmp_path / "no_existe.json") is None
    (tmp_path / "roto.json").write_text("{roto", encoding="utf-8")
    assert cargar_perfil_takeout(tmp_path / "roto.json") is None
    (tmp_path / "lista.json").write_text("[1, 2]", encoding="utf-8")
    assert cargar_perfil_takeout(tmp_path / "lista.json") is None
    (tmp_path / "otro.json").write_text('{"tipo": "perfil_conductual"}', encoding="utf-8")
    assert cargar_perfil_takeout(tmp_path / "otro.json") is None
    (tmp_path / "nuevo.json").write_text('{"tipo": "perfil_takeout", "version": 99}', encoding="utf-8")
    with caplog.at_level("WARNING", logger="rams.takeout"):
        assert cargar_perfil_takeout(tmp_path / "nuevo.json") == {"tipo": "perfil_takeout", "version": 99}
    assert any("más nuevo" in r.getMessage() for r in caplog.records)
    assert not perfil_vigente({"version": 1}) and not perfil_vigente({}) and perfil_vigente({"version": 2})
    # Exclusiones: archivo aparte, ilegible ⇒ vacío (nunca lanza).
    assert ruta_exclusiones(ruta) == ruta.parent / "takeout_exclusiones.json"
    (tmp_path / "exc.json").write_text("{roto", encoding="utf-8")
    assert len(cargar_exclusiones(tmp_path / "exc.json")) == 0
    assert len(cargar_exclusiones(tmp_path / "no.json")) == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_genera_muestra_y_quita(takeout: list[Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    salida = tmp_path / "data" / "john" / "perfil_takeout.json"
    duplicado = takeout[0].with_name(takeout[0].stem + " (1).zip")
    shutil.copyfile(takeout[0], duplicado)
    codigo = main([*map(str, takeout), str(duplicado), "--salida", str(salida), "--zona", "America/Bogota",
                   "--mostrar"])
    capturado = capsys.readouterr()
    assert codigo == 0 and salida.is_file()
    assert "Resumen que verá Amadeus" in capturado.out and "duplicados ignorados: 1" in capturado.out
    assert "Faltan partes del Takeout: 3-002" in capturado.out
    assert "no es exhaustivo" in capturado.out                              # el filtro es por léxico
    assert "Recuerdos heredados que guardará en su memoria" in capturado.out
    assert "Sitios web que más usa Lab Mem 001: youtube.com, github.com." in capturado.out
    assert "Detalle agregado del perfil:" in capturado.out and "Intereses:" in capturado.out
    assert "Índice listo" in capturado.err                                  # el progreso va a stderr
    sin_ruta = (capturado.out + capturado.err).replace(str(salida), "")
    _sin_secretos(sin_ruta)
    assert "Yoprueba" not in sin_ruta
    assert not ruta_exclusiones(salida).exists()                           # sin --quitar no hay exclusiones

    assert main(["--quitar", "arepa,youtube.com", "--salida", str(salida)]) == 0
    texto = capsys.readouterr().out
    assert "Quitados 2 elementos del perfil: arepa, youtube.com" in texto
    assert "youtube.com" not in texto.split("Resumen que verá Amadeus:")[1]
    guardado = cargar_perfil_takeout(salida)
    assert guardado is not None and "exclusiones" not in guardado
    archivo_exc = ruta_exclusiones(salida)
    contenido = archivo_exc.read_text(encoding="utf-8")
    assert "arepa" not in contenido and "youtube" not in contenido and len(cargar_exclusiones(archivo_exc)) == 2
    # Regenerar respeta las exclusiones anteriores (en su archivo aparte).
    assert main([*map(str, takeout), "--salida", str(salida), "--zona", "America/Bogota"]) == 0
    capsys.readouterr()
    regenerado = cargar_perfil_takeout(salida)
    assert regenerado is not None
    assert "arepa" not in {p["palabra"] for p in regenerado["palabras_clave"]}
    assert "youtube.com" not in {p["dominio"] for p in regenerado["plataformas"]}

    assert main(["--mostrar", "--salida", str(salida)]) == 0
    assert "Detalle agregado del perfil:" in capsys.readouterr().out


def test_cli_no_pisa_un_perfil_mas_completo(takeout: list[Path], tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    salida = tmp_path / "perfil_takeout.json"
    assert main([*map(str, takeout), "--salida", str(salida), "--zona", "UTC"]) == 0
    antes = salida.read_bytes()
    # Un zip sin datos del Takeout: ninguna fuente «ok» ⇒ no se guarda nada.
    otro = _escribir_zip(tmp_path / "otro.zip", {"cosas/archivo.txt": "x"})
    assert main([str(otro), "--salida", str(salida), "--zona", "UTC"]) == 1
    assert "Ninguna fuente del Takeout" in capsys.readouterr().err and salida.read_bytes() == antes
    # Solo las partes de Drive: faltarían fuentes que el perfil guardado sí tiene.
    assert main([str(takeout[2]), str(takeout[3]), "--salida", str(salida), "--zona", "UTC"]) == 1
    assert "no lo sobrescribo" in capsys.readouterr().err and salida.read_bytes() == antes
    assert main([str(takeout[2]), "--salida", str(salida), "--zona", "UTC", "--reemplazar"]) == 0
    capsys.readouterr()
    assert salida.read_bytes() != antes


def test_cli_perfil_v1_se_migra_y_avisa(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    salida = tmp_path / "perfil_takeout.json"
    viejo_hash = hashlib.sha256(b"musica").hexdigest()[:16]
    guardar_perfil({"version": 1, "tipo": TIPO_PERFIL, "intereses": [{"tema": "música"}, {"tema": "videojuegos"}],
                    "exclusiones": [viejo_hash]}, salida)
    assert main(["--mostrar", "--salida", str(salida)]) == 0
    capturado = capsys.readouterr()
    assert "versión anterior" in capturado.err
    exclusiones = cargar_exclusiones(ruta_exclusiones(salida))
    assert exclusiones.legado == {viejo_hash} and exclusiones.coincide("música")
    perfil = cargar_perfil_takeout(salida)
    assert perfil is not None and "exclusiones" not in perfil


def test_cli_errores(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert main(["--mostrar", "--salida", str(tmp_path / "no.json")]) == 1
    no_zip = tmp_path / "nada.zip"
    no_zip.write_bytes(b"x")
    assert main([str(no_zip), "--salida", str(tmp_path / "p.json"), "--zona", "UTC"]) == 1
    assert not (tmp_path / "p.json").exists()
    assert main(["--opcion-inventada"]) == 2
    capsys.readouterr()
