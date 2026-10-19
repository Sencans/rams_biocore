"""Tests de ``lobes/drive.py`` (v0.2.2, ``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §8 y §18).

Sin red ni Drive real: la cuenta es una ``CuentaGoogle`` de verdad con un token falso en ``tmp_path`` y
Drive es un ``httpx.MockTransport`` en memoria que **falla el test ante cualquier ``DELETE``**. Las
carpetas del usuario se crean en ``tmp_path``.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import logging
import re
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from core.contracts import bytes_json, largo_utf16
from lobes import drive as drive_mod
from lobes.drive import RUTAS_DRIVE, ArchivoDrive, Drive, OperacionDrive, ResultadoDrive, sanear_nombre_archivo
from lobes.google_cuenta import (
    SCOPE_DRIVE,
    SCOPE_DRIVE_FILE,
    SCOPE_DRIVE_READONLY,
    SCOPE_GMAIL_SEND,
    CuentaGoogle,
    ErrorGoogle,
    ProtectorNinguno,
)

CLIENT_ID = "123456789-falso.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-secreto-falso-000000"
REFRESH = "1//refresh-falso-" + "b" * 30
ACCESO = "ya29.acceso-falso-" + "a" * 30
DOC = "application/vnd.google-apps.document"
HOJA = "application/vnd.google-apps.spreadsheet"
PRES = "application/vnd.google-apps.presentation"
CARPETA = "application/vnd.google-apps.folder"
SESION = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=SESION123"
TODAS = {"canDownload": True, "canEdit": True, "canTrash": True, "canShare": True}


def archivo(ident: str, nombre: str, mime: str, *, size: int | None = None, propio: bool = True,
            capacidades: dict[str, bool] | None = None) -> dict[str, Any]:
    datos: dict[str, Any] = {"id": ident, "name": nombre, "mimeType": mime, "modifiedTime": "2026-10-12T15:00:00.000Z",
                             "ownedByMe": propio, "webViewLink": f"https://drive.google.com/file/d/{ident}/view",
                             "trashed": False, "capabilities": dict(capacidades or TODAS), "parents": ["root"]}
    if size is not None:
        datos["size"] = str(size)
    return datos


class FalsoDrive:
    """Drive en memoria. Cualquier DELETE hace fallar el test."""

    def __init__(self) -> None:
        self.peticiones: list[httpx.Request] = []
        self.archivos: dict[str, dict[str, Any]] = {}
        self.contenidos: dict[str, bytes] = {}
        self.exportaciones: dict[tuple[str, str], bytes] = {}
        self.lista: list[str] | None = None
        self.errores: dict[tuple[str, str], httpx.Response] = {}
        self.subidas: list[httpx.Request] = []
        self.sesion: list[Callable[[httpx.Request], httpx.Response]] = []
        self.recibido = bytearray()
        for datos in (archivo("doc1", "Informe ventas Q3", DOC), archivo("hoja1", "Presupuesto 2026", HOJA),
                      archivo("pres1", "Charla", PRES), archivo("pdf1", "Reporte.pdf", "application/pdf",
                                                                size=3 * 1024 * 1024 + 123),
                      archivo("txt1", "notas.txt", "text/plain", size=26),
                      archivo("carpeta1", "Trabajo", CARPETA),
                      archivo("ajeno1", "De otro.pdf", "application/pdf", size=10, propio=False,
                              capacidades={"canDownload": True, "canEdit": False, "canTrash": False,
                                           "canShare": False})):
            self.archivos[datos["id"]] = datos
        tamano_pdf = 3 * 1024 * 1024 + 123
        self.contenidos["pdf1"] = (bytes(range(256)) * (tamano_pdf // 256 + 1))[:tamano_pdf]
        self.contenidos["txt1"] = "hola, estas son mis notas".encode("utf-8") + b"\n"

    def respuesta_lista(self, ids: list[str]) -> httpx.Response:
        return httpx.Response(200, json={"files": [self.archivos[i] for i in ids], "incompleteSearch": False})

    async def __call__(self, p: httpx.Request) -> httpx.Response:
        await p.aread()
        if p.method == "DELETE":
            pytest.fail(f"Petición DELETE prohibida hacia Drive: {p.url.path}")
        self.peticiones.append(p)
        url = p.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            return httpx.Response(200, json={"access_token": ACCESO, "expires_in": 3600})
        assert url.host == "www.googleapis.com"
        clave = (p.method, url.path)
        if clave in self.errores:
            return self.errores[clave]
        params = url.params
        if p.method == "GET" and url.path == "/drive/v3/files":
            q = params.get("q", "")
            nombre = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
            if nombre:
                buscado = nombre.group(1).replace("\\'", "'").replace("\\\\", "\\")
                ids = [i for i, a in self.archivos.items() if a["name"] == buscado
                       and (a["mimeType"] == CARPETA or "folder" not in q)]
                return self.respuesta_lista(ids)
            ids = self.lista if self.lista is not None else [i for i, a in self.archivos.items()
                                                             if a["mimeType"] != CARPETA]
            return self.respuesta_lista(ids)
        m = re.fullmatch(r"/drive/v3/files/([^/]+)", url.path)
        if m and p.method == "GET":
            ident = m.group(1)
            if ident not in self.archivos:
                return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
            if params.get("alt") == "media":
                return httpx.Response(200, content=self.contenidos.get(ident, b""))
            return httpx.Response(200, json=self.archivos[ident])
        if m and p.method == "PATCH":
            return httpx.Response(200, json={"id": m.group(1), "trashed": True})
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/export", url.path)
        if m and p.method == "GET":
            mime = params.get("mimeType", "")
            return httpx.Response(200, content=self.exportaciones.get((m.group(1), mime), f"export {mime}".encode()))
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/permissions", url.path)
        if m and p.method == "POST":
            return httpx.Response(200, json={"id": "perm1"})
        if p.method == "POST" and url.path == "/drive/v3/files":
            cuerpo = json.loads(p.content)
            return httpx.Response(200, json={"id": "nuevaCarpeta1", "name": cuerpo["name"],
                                             "webViewLink": "https://drive.google.com/drive/folders/nuevaCarpeta1"})
        if p.method == "POST" and url.path == "/upload/drive/v3/files":
            self.subidas.append(p)
            if params.get("uploadType") == "resumable":
                return httpx.Response(200, headers={"Location": SESION})
            return httpx.Response(200, json={"id": "subido1", "name": "x",
                                             "webViewLink": "https://drive.google.com/file/d/subido1/view"})
        if p.method == "PUT" and url.path == "/upload/drive/v3/files":
            self.subidas.append(p)
            if self.sesion:
                return self.sesion.pop(0)(p)
            return httpx.Response(500)
        return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})

    def de(self, metodo: str, patron: str) -> list[httpx.Request]:
        return [p for p in self.peticiones if p.method == metodo and re.fullmatch(patron, p.url.path)]


@pytest.fixture
def ajustes(settings_con_claves: Any, tmp_path: Path) -> Any:
    s = settings_con_claves.con(google_habilitado=True, offline=False)
    assert tmp_path in Path(s.google_token_path).parents
    return s


@pytest.fixture
def carpetas(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path / "usuario"
    resultado = {}
    for clave, nombre in (("descargas", "Downloads"), ("documentos", "Documents"), ("escritorio", "Desktop"),
                          ("imagenes", "Pictures")):
        (base / nombre).mkdir(parents=True)
        resultado[clave] = base / nombre
    return resultado


def preparar_cuenta(settings: Any, scopes: tuple[str, ...]) -> None:
    cliente = Path(settings.google_cliente_path)
    cliente.parent.mkdir(parents=True, exist_ok=True)
    cliente.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}),
                       encoding="utf-8")
    token = {"version": 1, "cifrado": "ninguno", "refresh_token": REFRESH, "scopes": list(scopes),
             "cuenta": "juan.perez@gmail.com", "client_id_sha": hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:12],
             "conectado_ts": 0.0, "refresh_expira_ts": None}
    Path(settings.google_token_path).write_text(json.dumps(token), encoding="utf-8")


@contextlib.asynccontextmanager
async def entorno(settings: Any, falso: FalsoDrive, carpetas: dict[str, Path], *, conectar: bool = True,
                  scopes: tuple[str, ...] = (SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE),
                  reloj: Callable[[], float] | None = None) -> AsyncIterator[Drive]:
    if conectar:
        preparar_cuenta(settings, scopes)
    async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
        cuenta = CuentaGoogle(settings, None, cliente=cliente, protector=ProtectorNinguno())

        async def dormir(_: float) -> None:
            return None

        cuenta._dormir = dormir  # type: ignore[assignment]
        await cuenta.iniciar()
        drive = Drive(settings, cuenta, None, carpetas=carpetas, **({"reloj": reloj} if reloj else {}))
        drive.esperas = []  # type: ignore[attr-defined]

        async def dormir_drive(segundos: float) -> None:
            drive.esperas.append(segundos)  # type: ignore[attr-defined]

        drive._dormir = dormir_drive  # type: ignore[assignment]
        await drive.iniciar()
        yield drive


# ---------------------------------------------------------------------------
# Ningún DELETE
# ---------------------------------------------------------------------------


def test_rutas_drive_sin_delete() -> None:
    metodos = {metodo for metodo, _ in RUTAS_DRIVE.values()}
    assert metodos <= {"GET", "POST", "PATCH", "PUT"} and "DELETE" not in metodos
    assert all(url.startswith("https://www.googleapis.com/") for _, url in RUTAS_DRIVE.values())
    # Ningún literal del código (sin contar docstrings) nombra DELETE, emptyTrash, transferOwnership ni
    # allowFileDiscovery: el cliente no puede construir esas peticiones.
    arbol = ast.parse(Path(drive_mod.__file__).read_text(encoding="utf-8"))
    docstrings = {id(n.body[0].value) for n in ast.walk(arbol)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    literales = [n.value for n in ast.walk(arbol)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
    for prohibido in ("DELETE", "emptyTrash", "transferOwnership", "allowFileDiscovery"):
        assert not any(prohibido in literal for literal in literales), prohibido


def test_la_cuenta_rechaza_delete(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()

    async def principal() -> ErrorGoogle:
        async with entorno(ajustes, falso, carpetas) as drive:
            with pytest.raises(ErrorGoogle) as error:
                await drive._cuenta.solicitud("DELETE", "https://www.googleapis.com/drive/v3/files/doc1")
            return error.value

    assert correr(principal()).codigo == "error"
    assert all(p.method != "DELETE" for p in falso.peticiones)


# ---------------------------------------------------------------------------
# Buscar, última lista y referencias
# ---------------------------------------------------------------------------


def test_buscar_con_consulta(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1", "pdf1", "ajeno1"]

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            return await drive.buscar("informe")

    resultado = correr(principal())
    assert resultado.ok and len(resultado.archivos) == 3
    peticion = falso.de("GET", "/drive/v3/files")[0]
    params = peticion.url.params
    assert params["q"] == "(name contains 'informe' or fullText contains 'informe') and trashed = false"
    assert "orderBy" not in params
    assert params["fields"] == drive_mod.CAMPOS_LISTA
    assert params["pageSize"] == "10"
    assert resultado.mensaje == "Encontré 3 archivos en tu Drive."
    assert "Informe" not in resultado.mensaje
    lineas = resultado.datos["lista"].split("\n")
    assert lineas[0] == "1. Informe ventas Q3 · Documento de Google · 12 oct · tuyo"
    assert lineas[2].endswith("compartido contigo")


def test_buscar_sin_consulta_y_escapado(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()

    async def principal() -> None:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar(None, limite=99)
            await drive.buscar("o'reilly \\ x")

    correr(principal())
    recientes, escapada = falso.de("GET", "/drive/v3/files")
    assert recientes.url.params["q"] == "trashed = false"
    assert recientes.url.params["orderBy"] == "modifiedTime desc"
    assert recientes.url.params["pageSize"] == "25"
    q = escapada.url.params["q"]
    assert "name contains 'o\\'reilly \\\\ x'" in q and q.endswith("and trashed = false")


def test_buscar_dentro_de_una_carpeta(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            return await drive.buscar("plan", carpeta="Trabajo"), await drive.buscar("plan", carpeta="No existe")

    bien, mal = correr(principal())
    assert bien.ok and mal.codigo == "no_encontrado"
    q = falso.de("GET", "/drive/v3/files")[1].url.params["q"]
    assert "'carpeta1' in parents" in q and "trashed = false" in q


def test_ultima_lista_y_referencias(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1", "hoja1", "pdf1"]
    ahora = {"t": 1000.0}

    async def principal() -> list[Any]:
        async with entorno(ajustes, falso, carpetas, reloj=lambda: ahora["t"]) as drive:
            sin_lista = await drive.resolver("2")
            await drive.buscar("x")
            refs = [drive.resolver_local(r) for r in ("2", "el segundo", "#3", "el último", "informe ventas q3",
                                                       "presu", "Reporte", "9", "nada")]
            fuera = await drive.resolver("7")
            ahora["t"] += 1801
            caducada = drive.resolver_local("1")
            return [sin_lista, refs, fuera, caducada]

    sin_lista, refs, fuera, caducada = correr(principal())
    assert sin_lista.codigo == "no_existe" and "busca primero" in sin_lista.mensaje
    assert [a.id if a else None for a in refs] == ["hoja1", "hoja1", "pdf1", "pdf1", "doc1", "hoja1", "pdf1", None,
                                                    None]
    assert fuera.codigo == "no_existe" and "del 1 al 3" in fuera.mensaje
    assert caducada is None


def test_resolver_por_nombre_en_drive(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.archivos["doc2"] = archivo("doc2", "Charla", DOC)

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive, ResultadoDrive, Any]:
        async with entorno(ajustes, falso, carpetas) as drive:
            uno = await drive.resolver("el archivo notas.txt")
            dos = await drive.resolver("Charla")
            tres = await drive.resolver("inexistente")
            return uno, dos, tres, drive.resolver_local("2")

    uno, dos, tres, segundo = correr(principal())
    assert uno.ok and uno.archivos[0].id == "txt1"
    assert falso.de("GET", "/drive/v3/files")[0].url.params["q"] == "name = 'notas.txt' and trashed = false"
    assert dos.codigo == "ambiguo" and {a.id for a in dos.archivos} == {"pres1", "doc2"}
    assert "Hay 2 archivos" in dos.mensaje and "busca" in dos.mensaje
    assert "Charla" not in dos.mensaje                                  # sin nombres de Drive en el mensaje
    assert segundo is None          # los candidatos no se mostraron: nunca pasan a ser la «última lista»
    assert tres.codigo == "no_encontrado"


def test_ambiguo_no_reemplaza_la_lista_mostrada(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    """Con la lista [doc1, hoja1, pres1] a la vista, «resume el Acta» es ambiguo (dos «Acta» en Drive). Después,
    «el 2» sigue siendo hoja1 (el 2 que el usuario vio), nunca el 2.º «Acta», que mandaría a Gemini otro documento."""
    falso = FalsoDrive()
    falso.archivos["acta1"] = archivo("acta1", "Acta", DOC)
    falso.archivos["acta2"] = archivo("acta2", "Acta", DOC)
    falso.lista = ["doc1", "hoja1", "pres1"]

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive, ResultadoDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            mostrada = await drive.buscar("x")
            ambiguo = await drive.leer_texto("el Acta")
            segundo = await drive.leer_texto("el 2")
            info = await drive.info("el 2")
            return mostrada, ambiguo, segundo, info

    mostrada, ambiguo, segundo, info = correr(principal())
    assert [a.id for a in mostrada.archivos] == ["doc1", "hoja1", "pres1"]
    assert ambiguo.codigo == "ambiguo" and not ambiguo.ok and "texto" not in ambiguo.datos
    assert segundo.ok and segundo.archivos[0].id == "hoja1"
    assert info.ok and info.archivos[0].id == "hoja1"
    assert falso.de("GET", r"/drive/v3/files/acta\d(/export)?") == []    # nada de las «Acta» se leyó


def test_info(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["pdf1"]

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.info("1")

    resultado = correr(principal())
    assert resultado.ok and resultado.archivos[0].id == "pdf1"
    detalle = resultado.datos["detalle"]
    assert "Nombre: Reporte.pdf" in detalle and "Tipo: PDF" in detalle and "Tamaño: 3 MB" in detalle
    assert "Dueño: tú" in detalle and "Puedes: descargarlo, moverlo a la papelera, compartirlo" in detalle
    assert resultado.datos["enlace"].startswith("https://drive.google.com/")
    assert "Reporte" not in resultado.mensaje


# ---------------------------------------------------------------------------
# Descargar y exportar
# ---------------------------------------------------------------------------


def test_descargar_binario_por_bloques(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                       monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    falso = FalsoDrive()
    falso.lista = ["pdf1"]
    bloques: list[tuple[int, bool]] = []
    reemplazos: list[tuple[str, str]] = []
    escribir_original = drive_mod._EscritorParcial.escribir
    replace_original = drive_mod.os.replace

    def escribir(self: Any, bloque: bytes) -> None:
        bloques.append((len(bloque), self._parcial.name.endswith(".part") and self._parcial.exists()))
        escribir_original(self, bloque)

    def reemplazar(origen: Any, destino: Any) -> None:
        reemplazos.append((Path(origen).name, Path(destino).name))
        replace_original(origen, destino)

    monkeypatch.setattr(drive_mod._EscritorParcial, "escribir", escribir)
    monkeypatch.setattr(drive_mod.os, "replace", reemplazar)

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.descargar("1"), await drive.descargar("1")

    uno, dos = correr(principal())
    assert uno.ok and uno.datos["guardado"] == "Descargas/Reporte.pdf"
    assert dos.ok and dos.datos["guardado"] == "Descargas/Reporte (1).pdf"      # nunca sobrescribe
    assert (carpetas["descargas"] / "Reporte.pdf").read_bytes() == falso.contenidos["pdf1"]
    assert (carpetas["descargas"] / "Reporte (1).pdf").read_bytes() == falso.contenidos["pdf1"]
    assert uno.datos["bytes"] == len(falso.contenidos["pdf1"])
    assert [n for n, _ in bloques[:4]] == [1024 * 1024, 1024 * 1024, 1024 * 1024, 123]
    assert all(parte for _, parte in bloques)
    assert reemplazos == [("Reporte.pdf.part", "Reporte.pdf"), ("Reporte (1).pdf.part", "Reporte (1).pdf")]
    assert not list(carpetas["descargas"].glob("*.part"))
    assert str(tmp_path) not in uno.mensaje and "Descargas/Reporte.pdf" in uno.mensaje
    medios = falso.de("GET", "/drive/v3/files/pdf1")
    assert any(p.url.params.get("alt") == "media" for p in medios)


@pytest.mark.parametrize("ident,formato,mime,nombre,nota", [
    ("doc1", None, "application/pdf", "Informe ventas Q3.pdf", ""),
    ("doc1", "texto", "text/plain", "Informe ventas Q3.txt", ""),
    ("doc1", "original", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
     "Informe ventas Q3.docx", ""),
    ("hoja1", "texto", "text/csv", "Presupuesto 2026.csv", "primera hoja"),
    ("hoja1", "original", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
     "Presupuesto 2026.xlsx", ""),
    ("pres1", "pdf", "application/pdf", "Charla.pdf", ""),
])
def test_exportar(ajustes: Any, carpetas: dict[str, Path], correr: Any, ident: str, formato: str | None, mime: str,
                  nombre: str, nota: str) -> None:
    falso = FalsoDrive()
    falso.lista = [ident]

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.descargar("1", destino="documentos", formato=formato)

    resultado = correr(principal())
    assert resultado.ok, resultado.mensaje
    assert resultado.datos["guardado"] == f"Documentos/{nombre}"
    exportada = falso.de("GET", f"/drive/v3/files/{ident}/export")[0]
    assert exportada.url.params["mimeType"] == mime
    assert (carpetas["documentos"] / nombre).read_bytes() == f"export {mime}".encode()
    if nota:
        assert nota in resultado.datos["nota"]
    else:
        assert "nota" not in resultado.datos


def test_exportacion_de_mas_de_10_mb(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1"]
    falso.errores[("GET", "/drive/v3/files/doc1/export")] = httpx.Response(
        403, json={"error": {"code": 403, "errors": [{"reason": "exportSizeLimitExceeded"}]}})

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.descargar("1")

    resultado = correr(principal())
    assert resultado.codigo == "no_exportable" and "ábrelo en el navegador" in resultado.mensaje
    assert not list(carpetas["descargas"].iterdir())


def test_tope_de_descarga(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["pdf1"]
    falso.archivos["grande"] = archivo("grande", "sin_tamano.bin", "application/octet-stream")
    falso.contenidos["grande"] = b"z" * (2 * 1024 * 1024)
    s = ajustes.con(drive_max_descarga_mb=1)

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive]:
        async with entorno(s, falso, carpetas) as drive:
            await drive.buscar("x")
            por_metadatos = await drive.descargar("1")
            falso.lista = ["grande"]
            await drive.buscar("x")
            por_flujo = await drive.descargar("1")
            return por_metadatos, por_flujo

    por_metadatos, por_flujo = correr(principal())
    assert por_metadatos.codigo == "demasiado_grande" and "RAMS_DRIVE_MAX_DESCARGA_MB" in por_metadatos.mensaje
    assert not any(p.url.params.get("alt") == "media" for p in falso.de("GET", "/drive/v3/files/pdf1"))
    assert por_flujo.codigo == "demasiado_grande"
    assert not list(carpetas["descargas"].iterdir())        # ni el .part queda


def test_descargar_destino_y_formato_invalidos(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()

    async def principal() -> list[ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            return [await drive.descargar("1", destino="imagenes"), await drive.descargar("1", formato="exe"),
                    await drive.descargar("1")]

    resultados = correr(principal())
    assert [r.codigo for r in resultados] == ["prohibido", "prohibido", "no_existe"]


def test_descargar_en_la_carpeta_configurada(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                             tmp_path: Path) -> None:
    falso = FalsoDrive()
    falso.lista = ["txt1"]
    destino = tmp_path / "MisDescargas"
    s = ajustes.con(drive_descargas_dir=destino)

    async def principal() -> ResultadoDrive:
        async with entorno(s, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.descargar("1")

    resultado = correr(principal())
    assert resultado.ok and resultado.datos["guardado"] == "MisDescargas/notas.txt"
    assert (destino / "notas.txt").read_bytes() == falso.contenidos["txt1"]


def test_sanear_nombre_archivo() -> None:
    assert sanear_nombre_archivo("a/b\\c:d*?.pdf") == "a_b_c_d__.pdf"
    assert sanear_nombre_archivo("..secreto..txt") == "secreto.txt"
    assert sanear_nombre_archivo("CON", ".pdf") == "_CON.pdf"
    assert sanear_nombre_archivo("nul.txt") == "_nul.txt"
    assert sanear_nombre_archivo("Informe.PDF", ".pdf") == "Informe.pdf"
    assert sanear_nombre_archivo("linea\x00rara\x07", ".txt") == "linea rara.txt"
    assert sanear_nombre_archivo("", ".pdf") == "archivo.pdf"
    assert sanear_nombre_archivo(" . ", "") == "archivo"
    largo = sanear_nombre_archivo("x" * 300, ".docx")
    assert largo.endswith(".docx") and largo_utf16(largo) <= 150
    largo_ext = sanear_nombre_archivo("y" * 300 + ".jpeg")
    assert largo_ext.endswith(".jpeg") and largo_utf16(largo_ext) <= 150
    assert largo_utf16(sanear_nombre_archivo("😀" * 200, ".pdf")) <= 150


def test_nombres_de_drive_sin_controles_bidireccionales() -> None:
    """Un nombre ajeno con U+202E (RLO) se vería «facturaexe.pdf» siendo un .exe: se quitan los controles bidi."""
    rlo = "factura‮fdp.exe"
    assert sanear_nombre_archivo(rlo) == "facturafdp.exe"
    assert sanear_nombre_archivo("⁦a⁩b‎‏c؜‪‫‬‭d", ".txt") == "abcd.txt"
    a = ArchivoDrive.desde_json(archivo("rlo1", rlo, "application/octet-stream", propio=False))
    assert a is not None and a.nombre == "facturafdp.exe"
    assert a.linea(1) == "1. facturafdp.exe · Archivo · 12 oct · compartido contigo"
    construido = ArchivoDrive(id="x1", nombre=rlo, tipo_mime="text/plain", es_carpeta=False, es_google=False,
                              tamano=1, modificado="", enlace=None, propio=False, puede_descargar=True,
                              puede_editar=False, puede_papelera=False, puede_compartir=False)
    assert "‮" not in construido.linea(1)
    # Lo que no es bidi (emoji con ZWJ, tildes) se conserva.
    assert sanear_nombre_archivo("familia 👨‍👩‍👧 ñandú", ".jpg") == "familia 👨‍👩‍👧 ñandú.jpg"


@pytest.mark.skipif(sys.platform != "win32", reason="la marca de origen (Zone.Identifier) es de NTFS/Windows")
def test_descarga_de_archivo_ajeno_lleva_marca_de_origen(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.archivos["rlo1"] = archivo("rlo1", "factura‮fdp.exe", "application/octet-stream", size=4, propio=False)
    falso.contenidos["rlo1"] = b"MZ\x90\x00"
    falso.lista = ["rlo1", "pdf1"]

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.descargar("1"), await drive.descargar("2")

    ajeno, propio = correr(principal())
    assert ajeno.ok and ajeno.datos["guardado"] == "Descargas/facturafdp.exe"
    ruta = carpetas["descargas"] / "facturafdp.exe"
    assert ruta.read_bytes() == b"MZ\x90\x00"
    with open(f"{ruta}:Zone.Identifier", encoding="ascii", newline="") as flujo:
        assert flujo.read() == "[ZoneTransfer]\r\nZoneId=3\r\n"
    assert propio.ok
    with pytest.raises(FileNotFoundError):     # lo propio se descarga sin marca (como antes)
        open(f"{carpetas['descargas'] / 'Reporte.pdf'}:Zone.Identifier", encoding="ascii").close()
    assert not list(carpetas["descargas"].glob("*.part"))


def test_marca_de_origen_fuera_de_windows_no_hace_nada(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(drive_mod.sys, "platform", "linux")
    ruta = tmp_path / "x.bin"
    ruta.write_bytes(b"x")
    assert drive_mod._marcar_origen_internet(ruta) is False
    assert [p.name for p in tmp_path.iterdir()] == ["x.bin"]


# ---------------------------------------------------------------------------
# Leer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ident,mime,nota", [
    ("doc1", "text/markdown", ""), ("hoja1", "text/csv", "primera hoja"), ("pres1", "text/plain", ""),
])
def test_leer_documentos_de_google(ajustes: Any, carpetas: dict[str, Path], correr: Any, ident: str, mime: str,
                                   nota: str) -> None:
    falso = FalsoDrive()
    falso.lista = [ident]
    falso.exportaciones[(ident, mime)] = "﻿# Título\r\nTexto del documento\u0007 fin".encode("utf-8")

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return await drive.leer_texto("1")

    resultado = correr(principal())
    assert resultado.ok and resultado.datos["texto"] == "# Título\nTexto del documento fin"
    assert resultado.datos["recortado"] is False
    assert falso.de("GET", f"/drive/v3/files/{ident}/export")[0].url.params["mimeType"] == mime
    assert (nota in resultado.datos.get("nota", "")) if nota else "nota" not in resultado.datos


def test_leer_texto_plano_recorte_y_pdf(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["txt1", "pdf1"]

    async def principal() -> tuple[ResultadoDrive, ResultadoDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            return (await drive.leer_texto("1"), await drive.leer_texto("1", max_caracteres=10),
                    await drive.leer_texto("2"))

    entero, recortado, pdf = correr(principal())
    assert entero.ok and entero.datos["texto"] == "hola, estas son mis notas\n"
    assert recortado.datos["texto"] == "hola, esta" and recortado.datos["recortado"] is True
    assert pdf.codigo == "no_soportado" and "descárgalo" in pdf.mensaje
    assert not any(p.url.params.get("alt") == "media" for p in falso.de("GET", "/drive/v3/files/pdf1"))


# ---------------------------------------------------------------------------
# Crear carpeta
# ---------------------------------------------------------------------------


def test_crear_carpeta(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()

    async def principal() -> list[ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            return [await drive.crear_carpeta("Facturas 2026"),
                    await drive.crear_carpeta("Facturas", dentro="Trabajo"),
                    await drive.crear_carpeta("Facturas", dentro="No existe"),
                    await drive.crear_carpeta("a/b"),
                    await drive.crear_carpeta("x" * 121)]

    raiz, dentro, sin_padre, barra, largo = correr(principal())
    assert raiz.ok and raiz.datos["archivo_id"] == "nuevaCarpeta1" and raiz.datos["enlace"].startswith("https://")
    creaciones = falso.de("POST", "/drive/v3/files")
    assert json.loads(creaciones[0].content) == {"name": "Facturas 2026", "mimeType": CARPETA}
    assert creaciones[0].url.params["fields"] == "id,name,webViewLink"
    assert dentro.ok and json.loads(creaciones[1].content)["parents"] == ["carpeta1"]
    assert sin_padre.codigo == "no_encontrado" and barra.codigo == "prohibido" and largo.codigo == "prohibido"
    assert len(creaciones) == 2


# ---------------------------------------------------------------------------
# Subir
# ---------------------------------------------------------------------------


def test_subir_multipart(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    datos = b"contenido del plan \x00\x01\x02"
    (carpetas["documentos"] / "trabajo").mkdir()
    (carpetas["documentos"] / "trabajo" / "plan.xlsx").write_bytes(datos)

    async def principal() -> tuple[OperacionDrive, ResultadoDrive, ResultadoDrive, Any]:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("documentos/trabajo/plan.xlsx", carpeta="Trabajo",
                                             canal_origen="escritorio", ttl_s=1800)
            mala = await drive.ejecutar(op.id, "0" * 64)
            bien = await drive.ejecutar(op.id, op.huella())
            return op, mala, bien, drive.operacion(op.id)

    op, mala, bien, despues = correr(principal())
    assert op.listo and op.op == "subir" and op.carpeta_destino is not None
    vista = op.vista()
    assert vista["local"] == {"nombre": "plan.xlsx", "bytes": len(datos), "carpeta": "documentos"}
    assert vista["carpeta_destino"] == "Trabajo" and vista["archivo"] is None and vista["compartir"] is None
    assert vista["paso"] == 1 and vista["listo"] is True
    assert mala.codigo == "huella"
    assert bien.ok and bien.datos["archivo_id"] == "subido1" and despues is None
    subida = falso.subidas[0]
    assert subida.url.params["uploadType"] == "multipart"
    tipo = subida.headers["Content-Type"]
    assert tipo.startswith("multipart/related; boundary=")
    frontera = tipo.split("boundary=")[1].encode()
    partes = subida.content.split(b"--" + frontera)
    assert partes[-1].strip() == b"--"
    cabecera_json, cuerpo_json = partes[1].split(b"\r\n\r\n", 1)
    assert b"application/json; charset=UTF-8" in cabecera_json
    assert json.loads(cuerpo_json.strip()) == {"name": "plan.xlsx", "parents": ["carpeta1"]}
    cabecera_datos, contenido = partes[2].split(b"\r\n\r\n", 1)
    assert contenido == datos + b"\r\n"


def test_subir_archivo_cambiado_no_sube(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    (carpetas["escritorio"] / "nota.txt").write_text("v1")

    async def principal() -> tuple[ResultadoDrive, Any]:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("nota.txt", carpeta=None, canal_origen="escritorio", ttl_s=1800)
            (carpetas["escritorio"] / "nota.txt").write_text("v2")
            return await drive.ejecutar(op.id, op.huella()), drive.operacion(op.id)

    resultado, conservada = correr(principal())
    assert resultado.codigo == "archivo_cambio" and conservada is not None
    assert falso.subidas == []


def test_preparar_subida_con_problemas(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    (carpetas["imagenes"] / "foto.png").write_bytes(b"png")
    (carpetas["descargas"] / "cert.key").write_text("x")

    async def principal() -> list[OperacionDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            return [await drive.preparar_subida("foto.png", carpeta="No existe", canal_origen="escritorio", ttl_s=60),
                    await drive.preparar_subida("cert.key", carpeta=None, canal_origen="escritorio", ttl_s=60),
                    await drive.preparar_subida("../x", carpeta=None, canal_origen="escritorio", ttl_s=60)]

    carpeta_mala, sensible, ruta_mala = correr(principal())
    assert carpeta_mala.local_nombre == "foto.png" and carpeta_mala.local_carpeta == "imagenes"
    assert not carpeta_mala.listo and any("No encontré la carpeta" in p for p in carpeta_mala.problemas)
    assert sensible.vista()["local"] is None and any("claves" in p for p in sensible.problemas)
    assert not ruta_mala.listo


def test_subida_reanudable_308_y_5xx(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive_mod, "UMBRAL_SUBIDA_SIMPLE", 100 * 1024)
    monkeypatch.setattr(drive_mod, "TAM_BLOQUE_SUBIDA", 256 * 1024)
    total = 600 * 1024
    datos = bytes((i * 7) % 251 for i in range(total))
    (carpetas["descargas"] / "grande.bin").write_bytes(datos)
    falso = FalsoDrive()
    recibido = bytearray()

    def bloque(estado: int, rango: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
        def responder(p: httpx.Request) -> httpx.Response:
            if p.content:
                recibido.extend(p.content)
            cabeceras = {"Range": rango} if rango else {}
            if estado == 200:
                return httpx.Response(200, json={"id": "grande1", "webViewLink": "https://drive.google.com/x"})
            return httpx.Response(estado, headers=cabeceras)
        return responder

    falso.sesion = [bloque(308, "bytes=0-262143"), bloque(503), bloque(308, "bytes=0-524287"), bloque(200)]

    async def principal() -> tuple[ResultadoDrive, list[float]]:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("grande.bin", carpeta=None, canal_origen="escritorio", ttl_s=1800)
            return await drive.ejecutar(op.id, op.huella()), drive.esperas  # type: ignore[attr-defined]

    resultado, esperas = correr(principal())
    assert resultado.ok and resultado.datos["archivo_id"] == "grande1"
    inicio = falso.subidas[0]
    assert inicio.method == "POST" and inicio.url.params["uploadType"] == "resumable"
    assert inicio.headers["X-Upload-Content-Length"] == str(total)
    assert json.loads(inicio.content) == {"name": "grande.bin"}
    rangos = [p.headers["Content-Range"] for p in falso.subidas[1:]]
    assert rangos == ["bytes 0-262143/614400", "bytes 262144-524287/614400", "bytes */614400",
                      "bytes 524288-614399/614400"]
    assert all(str(p.url) == SESION for p in falso.subidas[1:])
    # El 503 hizo que se consultara el estado y se reanudara desde lo recibido de verdad.
    assert bytes(recibido[:262144]) == datos[:262144] and bytes(recibido[-(total - 524288):]) == datos[524288:]
    assert esperas == [1.0]


def test_subida_reanudable_archivo_cambiado_no_completa(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive_mod, "UMBRAL_SUBIDA_SIMPLE", 100 * 1024)
    monkeypatch.setattr(drive_mod, "TAM_BLOQUE_SUBIDA", 256 * 1024)
    ruta = carpetas["descargas"] / "grande.bin"
    ruta.write_bytes(b"a" * (600 * 1024))
    falso = FalsoDrive()
    falso.sesion = [lambda p: httpx.Response(308, headers={"Range": "bytes=0-262143"}),
                    lambda p: httpx.Response(308, headers={"Range": "bytes=0-524287"}),
                    lambda p: httpx.Response(200, json={"id": "no_deberia"})]

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("grande.bin", carpeta=None, canal_origen="escritorio", ttl_s=1800)
            with open(ruta, "r+b") as archivo_local:
                archivo_local.seek(600 * 1024 - 1)
                archivo_local.write(b"Z")
            return await drive.ejecutar(op.id, op.huella())

    resultado = correr(principal())
    assert resultado.codigo == "archivo_cambio"
    puts = [p for p in falso.subidas if p.method == "PUT"]
    assert len(puts) == 2                        # el último bloque nunca se envió: Drive no crea el archivo


def _reconstruir(recibido: dict[int, bytes], total: int) -> bytes:
    """Lo que guardó Drive: cada byte, de la última petición que lo cubrió."""
    subido = bytearray(total)
    for inicio in sorted(recibido):
        subido[inicio:inicio + len(recibido[inicio])] = recibido[inicio]
    return bytes(subido)


@pytest.mark.parametrize("via", ["308_corto", "5xx_y_consulta"])
def test_subida_reanudable_lo_no_confirmado_sale_de_la_memoria(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                                               monkeypatch: pytest.MonkeyPatch, via: str) -> None:
    """Drive confirma solo una parte del bloque y, mientras tanto, alguien cambia el archivo en la zona NO
    confirmada. Lo que se reenvía es lo ya hasheado (de la memoria), nunca una relectura del disco: Drive guarda
    exactamente el contenido aprobado (antes subía «X» con la huella aprobada)."""
    monkeypatch.setattr(drive_mod, "UMBRAL_SUBIDA_SIMPLE", 100 * 1024)
    monkeypatch.setattr(drive_mod, "TAM_BLOQUE_SUBIDA", 256 * 1024)
    total = 600 * 1024
    original = bytes((i * 7) % 251 for i in range(total))
    ruta = carpetas["descargas"] / "grande.bin"
    ruta.write_bytes(original)
    falso = FalsoDrive()
    recibido: dict[int, bytes] = {}
    lecturas: list[tuple[int, int]] = []
    leer_original = drive_mod._leer_en

    def leer(archivo_local: Any, desplazamiento: int, tamano: int) -> bytes:
        lecturas.append((desplazamiento, tamano))
        return leer_original(archivo_local, desplazamiento, tamano)

    monkeypatch.setattr(drive_mod, "_leer_en", leer)

    def guardar(p: httpx.Request) -> None:
        if p.content:
            recibido[int(p.headers["Content-Range"].split()[1].split("-")[0])] = bytes(p.content)

    def cambiar_archivo() -> None:
        with open(ruta, "r+b") as f:
            f.seek(131072)
            f.write(b"X" * 131072)

    def primero(p: httpx.Request) -> httpx.Response:
        guardar(p)
        cambiar_archivo()       # mientras el bloque viajaba
        if via == "308_corto":
            return httpx.Response(308, headers={"Range": "bytes=0-131071"})
        return httpx.Response(503)

    def responder(estado: int, rango: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
        def r(p: httpx.Request) -> httpx.Response:
            guardar(p)
            if estado == 200:
                return httpx.Response(200, json={"id": "grande1"})
            return httpx.Response(estado, headers={"Range": rango} if rango else {})
        return r

    pasos = [primero]
    if via == "5xx_y_consulta":
        pasos.append(responder(308, "bytes=0-131071"))      # respuesta a «bytes */total»
    pasos += [responder(308, "bytes=0-393215"), responder(200)]
    falso.sesion = pasos

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("grande.bin", carpeta=None, canal_origen="escritorio", ttl_s=1800)
            assert op.local_sha256 == hashlib.sha256(original).hexdigest()
            return await drive.ejecutar(op.id, op.huella())

    resultado = correr(principal())
    assert resultado.ok, resultado.mensaje
    assert _reconstruir(recibido, total) == original
    assert recibido[131072] == original[131072:393216]          # el reenvío lleva lo aprobado, no las «X» nuevas
    # Cada byte se leyó del disco una sola vez y en orden.
    assert lecturas == [(0, 262144), (262144, 131072), (393216, total - 393216)]
    rangos = [p.headers["Content-Range"] for p in falso.subidas if p.method == "PUT"]
    esperados = ["bytes 0-262143/614400", "bytes 131072-393215/614400", "bytes 393216-614399/614400"]
    if via == "5xx_y_consulta":
        esperados.insert(1, "bytes */614400")
    assert rangos == esperados


def test_subida_reanudable_drive_pide_volver_atras_aborta(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Un 308 sin ``Range`` (o con uno anterior a lo que ya confirmó) pediría reenviar bytes que ya no están en
    memoria: se aborta en vez de releerlos del disco sin comprobar."""
    monkeypatch.setattr(drive_mod, "UMBRAL_SUBIDA_SIMPLE", 100 * 1024)
    monkeypatch.setattr(drive_mod, "TAM_BLOQUE_SUBIDA", 256 * 1024)
    (carpetas["descargas"] / "grande.bin").write_bytes(b"g" * (600 * 1024))
    falso = FalsoDrive()
    falso.sesion = [lambda p: httpx.Response(308, headers={"Range": "bytes=0-262143"}),
                    lambda p: httpx.Response(308),
                    lambda p: httpx.Response(200, json={"id": "no_deberia"})]

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("grande.bin", carpeta=None, canal_origen="escritorio", ttl_s=1800)
            return await drive.ejecutar(op.id, op.huella())

    resultado = correr(principal())
    assert resultado.codigo == "error" and "empieza de nuevo" in resultado.mensaje
    assert len([p for p in falso.subidas if p.method == "PUT"]) == 2


def test_sesion_de_subida_caducada(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drive_mod, "UMBRAL_SUBIDA_SIMPLE", 1024)
    monkeypatch.setattr(drive_mod, "TAM_BLOQUE_SUBIDA", 256 * 1024)
    (carpetas["descargas"] / "medio.bin").write_bytes(b"m" * 4096)
    falso = FalsoDrive()
    falso.sesion = [lambda p: httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})]

    async def principal() -> ResultadoDrive:
        async with entorno(ajustes, falso, carpetas) as drive:
            op = await drive.preparar_subida("medio.bin", carpeta=None, canal_origen="escritorio", ttl_s=1800)
            return await drive.ejecutar(op.id, op.huella())

    resultado = correr(principal())
    assert resultado.codigo == "error" and "empieza de nuevo" in resultado.mensaje


# ---------------------------------------------------------------------------
# Papelera y compartir
# ---------------------------------------------------------------------------


def test_papelera(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1", "ajeno1"]

    async def principal() -> tuple[OperacionDrive, ResultadoDrive, OperacionDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            op = await drive.preparar_papelera("1", canal_origen="escritorio", ttl_s=1800)
            hecho = await drive.ejecutar(op.id, op.huella())
            ajena = await drive.preparar_papelera("2", canal_origen="escritorio", ttl_s=1800)
            return op, hecho, ajena, await drive.ejecutar(ajena.id, ajena.huella())

    op, hecho, ajena, rechazo = correr(principal())
    assert op.listo and op.vista()["archivo"]["nombre"] == "Informe ventas Q3"
    assert hecho.ok and "papelera" in hecho.mensaje and "30 días" in hecho.mensaje
    parche = falso.de("PATCH", "/drive/v3/files/doc1")[0]
    assert json.loads(parche.content) == {"trashed": True}
    assert not ajena.listo and any("no eres dueño" in p for p in ajena.problemas)
    assert rechazo.codigo == "prohibido" and len(falso.de("PATCH", r"/drive/v3/files/.*")) == 1


def test_compartir_con_una_persona_y_en_publico(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1"]

    async def principal() -> tuple[OperacionDrive, ResultadoDrive, OperacionDrive, ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            persona = await drive.preparar_compartir("1", correo="ana@x.com", rol="writer", publico=False,
                                                     canal_origen="escritorio", ttl_s=1800)
            r1 = await drive.ejecutar(persona.id, persona.huella())
            publico = await drive.preparar_compartir("1", correo=None, rol="reader", publico=True,
                                                     canal_origen="escritorio", ttl_s=1800)
            r2 = await drive.ejecutar(publico.id, publico.huella())
            return persona, r1, publico, r2

    persona, r1, publico, r2 = correr(principal())
    assert persona.vista()["compartir"] == {"con": "ana@x.com", "rol": "writer", "publico": False}
    assert publico.vista()["compartir"] == {"con": "", "rol": "reader", "publico": True}
    assert r1.ok and r2.ok and r2.datos["enlace"].startswith("https://")
    permisos = falso.de("POST", "/drive/v3/files/doc1/permissions")
    assert json.loads(permisos[0].content) == {"type": "user", "role": "writer", "emailAddress": "ana@x.com"}
    assert json.loads(permisos[1].content) == {"type": "anyone", "role": "reader"}
    for p in permisos:
        assert "sendNotificationEmail" not in p.url.params and "transferOwnership" not in p.url.params
        assert b"allowFileDiscovery" not in p.content and b"transferOwnership" not in p.content
    assert "ana@x.com" not in r1.mensaje


def test_compartir_publico_y_editable_nunca(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1"]

    async def principal() -> tuple[OperacionDrive, ResultadoDrive, OperacionDrive, OperacionDrive]:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("x")
            op = await drive.preparar_compartir("1", correo=None, rol="writer", publico=True,
                                                canal_origen="escritorio", ttl_s=1800)
            resultado = await drive.ejecutar(op.id, op.huella())
            correo_malo = await drive.preparar_compartir("1", correo="no-es-correo", rol="reader", publico=False,
                                                         canal_origen="escritorio", ttl_s=1800)
            rol_malo = await drive.preparar_compartir("1", correo="ana@x.com", rol="owner", publico=False,
                                                      canal_origen="escritorio", ttl_s=1800)
            return op, resultado, correo_malo, rol_malo

    op, resultado, correo_malo, rol_malo = correr(principal())
    assert not op.listo and any("Público y editable, nunca" in p for p in op.problemas)
    assert resultado.codigo == "prohibido"
    assert not correo_malo.listo and not rol_malo.listo
    assert falso.de("POST", r"/drive/v3/files/.*/permissions") == []


def test_403_en_modo_por_defecto_explica_drive_completo(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1"]
    falso.errores[("PATCH", "/drive/v3/files/doc1")] = httpx.Response(
        403, json={"error": {"code": 403, "errors": [{"reason": "appNotAuthorizedToFile"}]}})

    async def principal(s: Any, scopes: tuple[str, ...]) -> tuple[ResultadoDrive, Any]:
        async with entorno(s, falso, carpetas, scopes=scopes) as drive:
            await drive.buscar("x")
            op = await drive.preparar_papelera("1", canal_origen="escritorio", ttl_s=1800)
            return await drive.ejecutar(op.id, op.huella()), drive.operacion(op.id)

    resultado, conservada = correr(principal(ajustes, (SCOPE_DRIVE_READONLY, SCOPE_DRIVE_FILE)))
    assert resultado.codigo == "sin_permiso" and "RAMS_DRIVE_COMPLETO=1" in resultado.mensaje
    assert conservada is not None                 # ante un fallo, la operación se conserva
    completo, _ = correr(principal(ajustes.con(drive_completo=True), (SCOPE_DRIVE,)))
    assert completo.codigo == "sin_permiso" and "RAMS_DRIVE_COMPLETO" not in completo.mensaje


def test_ejecutar_comprobaciones(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoDrive()
    falso.lista = ["doc1"]
    ahora = {"t": 0.0}

    async def principal() -> list[ResultadoDrive]:
        async with entorno(ajustes, falso, carpetas, reloj=lambda: ahora["t"]) as drive:
            await drive.buscar("x")
            op = await drive.preparar_papelera("1", canal_origen="telegram", ttl_s=60)
            resultados = [await drive.ejecutar("ffffffff", op.huella()), await drive.ejecutar(op.id, None)]  # type: ignore[arg-type]
            ahora["t"] = 61
            resultados.append(await drive.ejecutar(op.id, op.huella()))
            return resultados

    assert [r.codigo for r in correr(principal())] == ["no_existe", "huella", "caducado"]
    assert falso.de("PATCH", r"/drive/v3/files/.*") == []


def test_huella_y_vista_de_la_operacion() -> None:
    a = ArchivoDrive.desde_json(archivo("doc1", "N" * 300, DOC))
    assert a is not None
    base = OperacionDrive(id="0123abcd", version=1, op="compartir", archivo=a, local_nombre=None, local_carpeta=None,
                          local_bytes=None, local_sha256=None, local_ruta=None, carpeta_destino=None,
                          compartir_con="x" * 64 + "@" + "a" * 63 + "." + "b" * 63 + "." + "c" * 57 + ".com",
                          rol="reader", publico=False, problemas=tuple("€" * 160 for _ in range(10)),
                          canal_origen="escritorio", creado=0.0, caduca=1.0)
    vista = base.vista()
    assert largo_utf16(vista["archivo"]["nombre"]) <= 200 and vista["compartir"]["con"] == base.compartir_con
    assert bytes_json(vista) < 6 * 1024
    import dataclasses

    huellas = {base.huella()}
    for cambio in ({"rol": "writer"}, {"publico": True}, {"compartir_con": "otra@x.com"},
                   {"archivo": dataclasses.replace(a, id="doc2")}, {"op": "papelera"}):
        huellas.add(dataclasses.replace(base, **cambio).huella())
    assert len(huellas) == 6
    assert dataclasses.replace(base, version=7, problemas=()).huella() == base.huella()
    assert "N" * 10 not in repr(a) and "doc1" not in repr(a)


# ---------------------------------------------------------------------------
# Disponibilidad, diagnóstico y logs
# ---------------------------------------------------------------------------


def test_disponibilidad_y_diagnosticos(settings_con_claves: Any, ajustes: Any, carpetas: dict[str, Path],
                                      correr: Any) -> None:
    falso = FalsoDrive()

    async def probar(s: Any, **kwargs: Any) -> tuple[Any, ResultadoDrive, ResultadoDrive]:
        async with entorno(s, falso, carpetas, **kwargs) as drive:
            return drive.diagnostico(), await drive.buscar("x"), await drive.crear_carpeta("Nueva")

    off, buscar, _ = correr(probar(settings_con_claves, conectar=False))
    assert off.clave == "drive" and off.estado.value == "OFF" and buscar.codigo == "deshabilitado"
    sin_servicio, buscar, _ = correr(probar(ajustes.con(google_servicios=("correo",)), conectar=False))
    assert sin_servicio.estado.value == "OFF" and buscar.codigo == "deshabilitado"
    espera, buscar, _ = correr(probar(ajustes, conectar=False))
    assert espera.estado.value == "EN ESPERA" and buscar.codigo == "desconectado"
    sin_drive, buscar, _ = correr(probar(ajustes, scopes=(SCOPE_GMAIL_SEND,)))
    assert sin_drive.estado.value == "DEGRADADO" and buscar.codigo == "sin_permiso"
    solo_leer, buscar, crear = correr(probar(ajustes, scopes=(SCOPE_DRIVE_READONLY,)))
    assert solo_leer.estado.value == "DEGRADADO" and buscar.ok and crear.codigo == "sin_permiso"
    normal, _, _ = correr(probar(ajustes))
    assert normal.estado.value == "OK" and "modificar lo que cree Amadeus" in normal.detalle
    completo, _, _ = correr(probar(ajustes.con(drive_completo=True), scopes=(SCOPE_DRIVE,)))
    assert completo.estado.value == "OK" and completo.detalle == "completo"
    # Un token de Drive completo con RAMS_DRIVE_COMPLETO=0 (p. ej. conectado antes con =1): Drive no lo usa.
    de_mas, buscar, crear = correr(probar(ajustes, scopes=(SCOPE_GMAIL_SEND, SCOPE_DRIVE)))
    assert de_mas.estado.value == "DEGRADADO" and buscar.codigo == "sin_permiso" and crear.codigo == "sin_permiso"
    # Uno más estrecho que lo pedido (drive.readonly + drive.file con RAMS_DRIVE_COMPLETO=1) sí se usa: puede menos.
    estrecho, buscar, _ = correr(probar(ajustes.con(drive_completo=True)))
    assert estrecho.estado.value == "OK" and buscar.ok and "modificar lo que cree Amadeus" in estrecho.detalle


def test_logs_sin_nombres_ids_consultas_ni_contenido(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams")
    falso = FalsoDrive()
    falso.lista = ["doc1", "pdf1", "txt1"]
    falso.exportaciones[("doc1", "text/markdown")] = b"Contenido confidencial del informe"
    (carpetas["descargas"] / "subida_privada.txt").write_text("datos privados")

    async def principal() -> None:
        async with entorno(ajustes, falso, carpetas) as drive:
            await drive.buscar("consulta muy privada")
            await drive.info("1")
            await drive.descargar("2")
            await drive.leer_texto("1")
            await drive.crear_carpeta("Carpeta privada")
            op = await drive.preparar_subida("subida_privada.txt", carpeta=None, canal_origen="escritorio", ttl_s=60)
            await drive.ejecutar(op.id, op.huella())
            comp = await drive.preparar_compartir("1", correo="ana@x.com", rol="reader", publico=False,
                                                  canal_origen="escritorio", ttl_s=60)
            await drive.ejecutar(comp.id, comp.huella())

    correr(principal())
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("rams"))
    assert "rams.drive" in {r.name for r in caplog.records}
    for privado in ("consulta muy privada", "Informe ventas", "Reporte", "doc1", "pdf1", "txt1", "Contenido confidencial",
                    "Carpeta privada", "subida_privada", "ana@x.com", "nuevaCarpeta1", "subido1",
                    str(carpetas["descargas"])):
        assert privado not in texto, privado
