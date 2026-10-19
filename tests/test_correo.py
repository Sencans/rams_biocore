"""Tests de ``lobes/correo.py`` (v0.2.2, ``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §7 y §18).

Sin red ni Gmail real: la cuenta es una ``CuentaGoogle`` de verdad con un token falso en ``tmp_path`` y
Google es un ``httpx.MockTransport``. Las carpetas del usuario (Descargas, Documentos, Escritorio) se
crean en ``tmp_path``.
"""

from __future__ import annotations

import base64
import contextlib
import email
import email.policy
import hashlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.contracts import bytes_json, huella_contenido, largo_utf16
from lobes import correo as correo_mod
from lobes.correo import Adjunto, Borrador, Correo, ResultadoEnvio, buscar_archivo_local
from lobes.google_cuenta import SCOPE_DRIVE_FILE, SCOPE_DRIVE_READONLY, SCOPE_GMAIL_SEND, CuentaGoogle, ProtectorNinguno

CLIENT_ID = "123456789-falso.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-secreto-falso-000000"
REFRESH = "1//refresh-falso-" + "b" * 30
ACCESO = "ya29.acceso-falso-" + "a" * 30
CUENTA = "juan.perez@gmail.com"
MAMA = "mama.querida@gmail.com"
JEFE = "jefe.supremo@empresa.com"


class FalsoGmail:
    """Doble de Google: renovación del token y ``messages.send`` (respuesta configurable)."""

    def __init__(self) -> None:
        self.envios: list[httpx.Request] = []
        self.refrescos = 0
        self.respuesta: Callable[[httpx.Request], Any] = lambda p: httpx.Response(
            200, json={"id": "18c0ffee1234", "threadId": "t1", "labelIds": ["SENT"]})

    async def __call__(self, peticion: httpx.Request) -> httpx.Response:
        await peticion.aread()
        assert peticion.method != "DELETE"
        if peticion.url.host == "oauth2.googleapis.com" and peticion.url.path == "/token":
            self.refrescos += 1
            return httpx.Response(200, json={"access_token": ACCESO, "expires_in": 3600})
        if peticion.url.host == "gmail.googleapis.com":
            self.envios.append(peticion)
            respuesta = self.respuesta(peticion)
            if isinstance(respuesta, BaseException):
                raise respuesta
            return respuesta
        return httpx.Response(404)


@pytest.fixture
def ajustes(settings_con_claves: Any, tmp_path: Path) -> Any:
    s = settings_con_claves.con(google_habilitado=True, offline=False,
                                correo_contactos_items=(("mama", MAMA), ("jefe", JEFE)))
    assert tmp_path in Path(s.google_token_path).parents
    return s


@pytest.fixture
def carpetas(tmp_path: Path) -> dict[str, Path]:
    base = tmp_path / "usuario"
    resultado = {}
    for clave, nombre in (("descargas", "Downloads"), ("documentos", "Documents"), ("escritorio", "Desktop"),
                          ("imagenes", "Pictures")):
        ruta = base / nombre
        ruta.mkdir(parents=True)
        resultado[clave] = ruta
    return resultado


def preparar_cuenta(settings: Any, scopes: tuple[str, ...] = (SCOPE_GMAIL_SEND, SCOPE_DRIVE_READONLY,
                                                              SCOPE_DRIVE_FILE)) -> None:
    cliente = Path(settings.google_cliente_path)
    cliente.parent.mkdir(parents=True, exist_ok=True)
    cliente.write_text(json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}}),
                       encoding="utf-8")
    token = {"version": 1, "cifrado": "ninguno", "refresh_token": REFRESH, "scopes": list(scopes),
             "cuenta": CUENTA, "client_id_sha": hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:12],
             "conectado_ts": 0.0, "refresh_expira_ts": None}
    Path(settings.google_token_path).write_text(json.dumps(token), encoding="utf-8")


@contextlib.asynccontextmanager
async def entorno(settings: Any, falso: FalsoGmail, carpetas: dict[str, Path], *, conectar: bool = True,
                  reloj: Callable[[], float] | None = None,
                  scopes: tuple[str, ...] | None = None) -> AsyncIterator[tuple[CuentaGoogle, Correo]]:
    if conectar:
        preparar_cuenta(settings, *((scopes,) if scopes is not None else ()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(falso)) as cliente:
        cuenta = CuentaGoogle(settings, None, cliente=cliente, abrir_navegador=lambda url: False,
                              protector=ProtectorNinguno())
        await cuenta.iniciar()
        correo = Correo(settings, cuenta, None, carpetas=carpetas, **({"reloj": reloj} if reloj else {}))
        await correo.iniciar()
        yield cuenta, correo


async def preparar(correo: Correo, **kwargs: Any) -> Borrador:
    datos: dict[str, Any] = {"para": ["mamá"], "asunto": None, "cuerpo": "Llegué bien", "canal_origen": "escritorio",
                             "ttl_s": 1800.0}
    datos.update(kwargs)
    return await correo.preparar(**datos)


def decodificar(peticion: httpx.Request) -> email.message.EmailMessage:
    if peticion.url.path.startswith("/upload/"):
        crudo = peticion.content
    else:
        raw = json.loads(peticion.content)["raw"]
        crudo = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    return email.message_from_bytes(crudo, policy=email.policy.default)  # type: ignore[return-value]


def correo_largo(i: int) -> str:
    """Dirección válida de exactamente 254 caracteres."""
    local = f"{i:02d}" + "x" * 62
    dominio = "a" * 63 + "." + "b" * 63 + "." + "c" * 57 + ".com"
    direccion = f"{local}@{dominio}"
    assert len(direccion) == 254
    return direccion


# ---------------------------------------------------------------------------
# Destinatarios
# ---------------------------------------------------------------------------


def test_resolver_destinatarios(ajustes: Any, carpetas: dict[str, Path]) -> None:
    correo = Correo(ajustes, None, carpetas=carpetas)
    assert correo.resolver_destinatarios(["mamá"]) == ((MAMA,), ())
    assert correo.resolver_destinatarios(["mi jefe"]) == ((JEFE,), ())
    assert correo.resolver_destinatarios(["para mi jefe"]) == ((JEFE,), ())
    assert correo.resolver_destinatarios(["ana@x.com, pedro@y.com y mamá"]) == (("ana@x.com", "pedro@y.com", MAMA), ())
    assert correo.resolver_destinatarios(["Ana@X.com", "ana@x.com", "mamá", "mama"]) == (("Ana@X.com", MAMA), ())
    assert correo.resolver_destinatarios(["mi tía Rosa"]) == ((), ("mi tía Rosa",))
    assert correo.resolver_destinatarios(["a" * 250 + "@x.com"]) == ((), ("a" * 250 + "@x.com",))
    assert correo.resolver_destinatarios(None) == ((), ())  # type: ignore[arg-type]


def test_preparar_basico_y_vista_exacta(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoGmail()

    async def principal() -> Borrador:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            return await preparar(correo)

    b = correr(principal())
    assert b.para == (MAMA,) and b.cc == () and b.cco == ()
    assert b.asunto == "Llegué bien" and b.cuerpo == "Llegué bien"
    assert b.listo and b.n_destinatarios == 1 and b.version == 1
    assert len(b.id) == 8 and int(b.id, 16) >= 0
    assert b.vista() == {"para": [MAMA], "cc": [], "cco": [], "asunto": "Llegué bien", "cuerpo": "Llegué bien",
                         "adjuntos": [], "problemas": [], "avisos": [], "listo": True}
    assert MAMA not in repr(b) and "Llegué" not in repr(b)
    assert falso.envios == []


def test_no_resuelto_y_sin_destinatario(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> tuple[Borrador, Borrador]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            uno = await preparar(correo, para=["mi tía Rosa"])
            dos = await preparar(correo, para=[])
            return uno, dos

    uno, dos = correr(principal())
    assert not uno.listo and any("«mi tía Rosa»" in p for p in uno.problemas)
    assert not dos.listo and any("Falta el destinatario" in p for p in dos.problemas)


def test_cr_lf_en_destinatario_rechazado(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, para=["ana@x.com\r\nBcc: espia@malo.com", "pedro@y.com"],
                                  cc=["x@y.com\nz"])

    b = correr(principal())
    assert b.para == ("pedro@y.com",) and b.cc == ()
    assert not b.listo
    assert any("saltos de línea" in p for p in b.problemas)
    assert all("espia" not in p for p in b.problemas)


def test_tope_de_destinatarios_los_que_sobran_no_entran(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    s = ajustes.con(correo_max_destinatarios=3)

    async def principal() -> Borrador:
        async with entorno(s, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, para=["a@x.com", "b@x.com"], cc=["c@x.com", "a@x.com"],
                                  cco=["d@x.com", "e@x.com"])

    b = correr(principal())
    assert b.para == ("a@x.com", "b@x.com") and b.cc == ("c@x.com",) and b.cco == ()
    assert b.n_destinatarios == 3 and not b.listo
    assert any("Sobran 2 destinatarios (máximo 3)" in p for p in b.problemas)


# ---------------------------------------------------------------------------
# Texto: anti-secretos, topes y saneado
# ---------------------------------------------------------------------------


def test_anti_secretos_sobre_el_cuerpo_crudo(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> tuple[Borrador, Borrador, Borrador]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            uno = await preparar(correo, cuerpo="hola\npassword: x\nchao", asunto="Saludo")
            dos = await preparar(correo, cuerpo="todo bien", asunto="clave: 1234")
            tres = await preparar(correo, cuerpo="mira https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123/view",
                                  asunto="Archivo")
            return uno, dos, tres

    uno, dos, tres = correr(principal())
    assert not uno.listo
    assert any(p.startswith("La línea 2 parece llevar una contraseña") for p in uno.problemas)
    textos = " ".join(uno.problemas + uno.avisos)
    assert "password: x" not in textos and "password" not in textos
    assert uno.cuerpo == "hola\npassword: x\nchao"           # crudo: nunca redactado
    assert any(p.startswith("El asunto parece llevar") for p in dos.problemas)
    assert "1234" not in " ".join(dos.problemas)
    assert not tres.listo
    assert any("comparte el 2 con" in a for a in tres.avisos)
    assert "drive.google.com" in tres.cuerpo


def test_texto_largo_sin_espacios_llega_intacto(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    cuerpo = "Te mando informe_final_ventas_2026_q3 mañana"

    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, cuerpo=cuerpo, asunto="Informe")

    b = correr(principal())
    assert b.cuerpo == cuerpo and "oculto" not in b.cuerpo


@pytest.mark.parametrize("cuerpo,esperado", [
    ("a" * 10_001, "a" * 10_000),
    ("😀" * 5_001, "😀" * 5_000),
    ("b" * 9_999 + "😀", "b" * 9_999),
], ids=["10001_ascii", "5001_emojis", "emoji_partido"])
def test_cuerpo_largo_se_recorta_antes_de_huellar(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                                  cuerpo: str, esperado: str) -> None:
    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, cuerpo=cuerpo, asunto="Largo")

    b = correr(principal())
    assert b.cuerpo == esperado and largo_utf16(b.cuerpo) <= 10_000
    assert not b.listo
    assert any(p.startswith("El cuerpo pasaba de 10 000 caracteres y lo corté") for p in b.problemas)
    vista = b.vista()
    assert vista["cuerpo"] == b.cuerpo                     # lo que se ve es exactamente lo huellado
    esperada = huella_contenido({"clase": "correo", "para": vista["para"], "cc": vista["cc"], "cco": vista["cco"],
                                 "asunto": vista["asunto"], "cuerpo": vista["cuerpo"], "adjuntos": []})
    assert b.huella() == esperada and len(esperada) == 64


def test_cuerpo_justo_en_el_tope_no_se_recorta(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, cuerpo="c" * 10_000, asunto="Justo")

    b = correr(principal())
    assert b.cuerpo == "c" * 10_000 and not any("10 000" in p for p in b.problemas)


def test_saneado_del_texto(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, cuerpo="Cafe\u0301\r\nlinea\u0007 dos\rtres\tcon tab \ud800 fin",
                                  asunto="Asunto\ncon salto\u0000")

    b = correr(principal())
    assert b.cuerpo == "Café\nlinea dos\ntres\tcon tab \ufffd fin"
    assert b.asunto == "Asunto con salto"
    assert len(b.huella()) == 64


def test_asunto_largo_y_por_defecto(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> tuple[Borrador, Borrador, Borrador]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            largo = await preparar(correo, asunto="x" * 250)
            defecto = await preparar(correo, asunto=None,
                                     cuerpo="\n\nEsta es una primera línea bastante larga que pasa de sesenta caracteres "
                                            "seguro\nsegunda")
            vacio = await preparar(correo, asunto="", cuerpo="")
            return largo, defecto, vacio

    largo, defecto, vacio = correr(principal())
    assert largo.asunto == "x" * 200 and any("asunto pasaba de 200" in p for p in largo.problemas)
    assert defecto.asunto == "Esta es una primera línea bastante larga que pasa de sesenta"
    assert largo_utf16(defecto.asunto) <= 60
    assert vacio.asunto == "(sin asunto)" and any("vacío" in p for p in vacio.problemas)


def test_vista_peor_caso_cabe_en_el_presupuesto() -> None:
    adjuntos = tuple(Adjunto("€" * 251 + f"{i}.pd", "descargas", None, 10**7, "a" * 64, "application/pdf")
                     for i in range(5))
    b = Borrador(id="0123abcd", version=1, para=tuple(correo_largo(i) for i in range(20)),
                 cc=tuple(correo_largo(i) for i in range(20, 40)), cco=tuple(correo_largo(i) for i in range(40, 50)),
                 asunto="€" * 200, cuerpo="€" * 10_000, adjuntos=adjuntos,
                 problemas=tuple("€" * 160 for _ in range(10)), avisos=tuple("€" * 160 for _ in range(5)),
                 canal_origen="telegram", creado=0.0, caduca=1.0)
    vista = b.vista()
    assert all(largo_utf16(a["nombre"]) == 255 for a in vista["adjuntos"])
    assert bytes_json(vista) <= 56_000
    mensaje = {"tipo": "tarjeta", "datos": {
        "id": b.id, "clase": "correo", "version": 1, "sha256": b.huella(), "titulo": "€" * 120,
        "resumen": "€" * 300, "aviso": "€" * 300, "botones": ["aprobar", "editar", "descartar"],
        "caduca_ts": 1.0e9, "canal_origen": "telegram", "correo": vista, "drive": None, "script": None,
        "salida": None, "estado": "pendiente"}}
    assert bytes_json(mensaje) <= 61_440


def test_preparar_peor_caso_problemas_y_avisos_acotados(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    s = ajustes.con(correo_max_destinatarios=50)
    alias = [f"alias{i} " + "€" * 150 for i in range(12)]

    async def principal() -> Borrador:
        async with entorno(s, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, para=[correo_largo(i) for i in range(50)], cc=alias,
                                  cuerpo="€" * 10_000, asunto="€" * 200,
                                  avisos=[f"aviso {i} " + "€" * 400 for i in range(7)])

    b = correr(principal())
    assert b.n_destinatarios == 50
    assert len(b.problemas) == 10 and all(largo_utf16(p) <= 160 for p in b.problemas)
    assert len(b.avisos) == 5 and all(largo_utf16(a) <= 160 for a in b.avisos)
    assert bytes_json(b.vista()) <= 56_000


# ---------------------------------------------------------------------------
# Adjuntos
# ---------------------------------------------------------------------------


def test_adjuntos_por_nombre_y_ruta_relativa(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    (carpetas["descargas"] / "informe.pdf").write_bytes(b"%PDF-1.4 informe")
    anidada = carpetas["documentos"] / "trabajo" / "2026"
    anidada.mkdir(parents=True)
    (anidada / "plan.xlsx").write_bytes(b"plan")
    (carpetas["escritorio"] / "foto.jpg").write_bytes(b"jpg")

    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, adjuntos=["informe.pdf", "PLAN.xlsx", "escritorio/foto.jpg"])

    b = correr(principal())
    assert b.listo, b.problemas
    assert [(a.nombre, a.carpeta, a.bytes) for a in b.adjuntos] == [
        ("informe.pdf", "descargas", 16), ("plan.xlsx", "documentos", 4), ("foto.jpg", "escritorio", 3)]
    assert b.adjuntos[0].sha256 == hashlib.sha256(b"%PDF-1.4 informe").hexdigest()
    assert b.adjuntos[0].tipo_mime == "application/pdf"
    assert b.vista()["adjuntos"][1] == {"nombre": "plan.xlsx", "bytes": 4, "carpeta": "documentos"}
    assert str(carpetas["descargas"]) not in json.dumps(b.vista())


def test_adjuntos_rechazados(ajustes: Any, carpetas: dict[str, Path], correr: Any, tmp_path: Path) -> None:
    profunda = carpetas["descargas"] / "a" / "b" / "c" / "d"
    profunda.mkdir(parents=True)
    (profunda / "hondo.txt").write_text("x")
    (carpetas["descargas"] / "id_rsa").write_text("clave")
    (carpetas["documentos"] / "certificado.PEM").write_text("clave")
    (carpetas["imagenes"] / "foto.png").write_bytes(b"png")
    oculta = carpetas["descargas"] / ".secreta"
    oculta.mkdir()
    (oculta / "nota.txt").write_text("x")
    (carpetas["descargas"] / "dup.txt").write_text("1")
    (carpetas["escritorio"] / "sub").mkdir()
    (carpetas["escritorio"] / "sub" / "dup.txt").write_text("2")

    async def principal(entrada: str) -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, adjuntos=[entrada])

    casos = {
        "../fuera.txt": "sin «..»",
        "C:/Windows/win.ini": "sin «..»",
        "/etc/passwd": "sin «..»",
        "hondo.txt": "No encontré",
        "id_rsa": "archivo de claves",
        "certificado.PEM": "archivo de claves",
        "nota.txt": "No encontré",
        ".secreta/nota.txt": "sin «..»",
        "imagenes/foto.png": "solo busco",
        "dup.txt": "Hay varios",
        "no_existe.pdf": "No encontré",
    }
    for entrada, fragmento in casos.items():
        b = correr(principal(entrada))
        assert not b.listo and b.adjuntos == (), entrada
        assert any(fragmento in p for p in b.problemas), (entrada, b.problemas)
    ambiguo = correr(principal("dup.txt"))
    texto = " ".join(ambiguo.problemas)
    assert "Descargas/dup.txt" in texto and "Escritorio/sub/dup.txt" in texto
    assert str(tmp_path) not in texto


def test_adjunto_bajo_data_dir_y_tamano(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    interna = Path(ajustes.data_dir) / "descargas_falsas"
    interna.mkdir(parents=True)
    (interna / "memoria.txt").write_text("privado")
    (carpetas["descargas"] / "grande.bin").write_bytes(b"\0" * (1024 * 1024 + 10))
    s = ajustes.con(correo_max_adjuntos_mb=1)

    async def principal(carpetas_usadas: dict[str, Path], entrada: str) -> Borrador:
        async with entorno(s, FalsoGmail(), carpetas_usadas) as (_, correo):
            return await preparar(correo, adjuntos=[entrada])

    dentro = correr(principal({**carpetas, "descargas": interna}, "memoria.txt"))
    assert any("archivos de Amadeus" in p for p in dentro.problemas)
    grande = correr(principal(carpetas, "grande.bin"))
    assert any("pesa" in p and "máximo" in p for p in grande.problemas)


def test_adjunto_enlace_que_sale_de_la_carpeta(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                               tmp_path: Path) -> None:
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    (fuera / "secreto.txt").write_text("x")
    entradas = ["enlace/secreto.txt", "secreto.txt"]
    try:
        os.symlink(fuera, carpetas["descargas"] / "enlace", target_is_directory=True)
        os.symlink(fuera / "secreto.txt", carpetas["descargas"] / "atajo.txt")
        entradas.append("atajo.txt")
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("este sistema no permite crear enlaces simbólicos")
        import _winapi  # Windows: una «junction» de directorio no necesita privilegios

        _winapi.CreateJunction(str(fuera), str(carpetas["descargas"] / "enlace"))

    async def principal(entrada: str) -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, adjuntos=[entrada])

    for entrada in entradas:
        b = correr(principal(entrada))
        assert b.adjuntos == () and not b.listo, entrada


def test_mas_de_cinco_adjuntos(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    for i in range(7):
        (carpetas["descargas"] / f"a{i}.txt").write_text(str(i))

    async def principal() -> Borrador:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            return await preparar(correo, adjuntos=[f"a{i}.txt" for i in range(7)])

    b = correr(principal())
    assert len(b.adjuntos) == 5 and any("Como mucho 5 adjuntos" in p for p in b.problemas)


def test_buscar_archivo_local_nombre_demasiado_largo(tmp_path: Path) -> None:
    carpeta = tmp_path / "d"
    carpeta.mkdir()
    resultado = buscar_archivo_local("x" * 300 + ".txt", {"descargas": carpeta}, ("descargas",), max_bytes=10)
    assert resultado.archivo is None and resultado.problema


# ---------------------------------------------------------------------------
# Huella, edición y caducidad
# ---------------------------------------------------------------------------


def test_huella_estable_y_sensible_a_cada_campo(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    (carpetas["descargas"] / "a.txt").write_text("uno")
    (carpetas["descargas"] / "b.txt").write_text("dos")
    base = {"para": ["ana@x.com"], "cc": ["c@x.com"], "cco": ["d@x.com"], "asunto": "Hola", "cuerpo": "Texto",
            "adjuntos": ["a.txt"]}
    variantes = [{"para": ["otra@x.com"]}, {"cc": []}, {"cco": ["e@x.com"]}, {"asunto": "Hola!"},
                 {"cuerpo": "Texto."}, {"adjuntos": ["b.txt"]}]

    async def principal() -> tuple[str, str, list[str]]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            h1 = (await preparar(correo, **base)).huella()
            h2 = (await preparar(correo, **base)).huella()
            otras = [(await preparar(correo, **{**base, **v})).huella() for v in variantes]
            return h1, h2, otras

    h1, h2, otras = correr(principal())
    assert h1 == h2 and len(h1) == 64
    assert len(set(otras)) == len(otras) and h1 not in otras


def test_editar_sube_version_y_revalida(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    (carpetas["descargas"] / "a.txt").write_text("uno")

    async def principal() -> tuple[Borrador, Borrador | None, Borrador | None, Borrador | None, Borrador | None]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            b = await preparar(correo, para=["mi tía Rosa"], adjuntos=["a.txt"])
            editado = await correo.editar(b.id, {"para": "mi jefe, ana@x.com", "cuerpo": "Nuevo\r\ntexto",
                                                 "asunto": "Otro"})
            otra = await correo.editar("ffffffff", {"cuerpo": "x"})
            mala = await correo.editar(b.id, {"de": "x"})
            no_texto = await correo.editar(b.id, {"cuerpo": 3})  # type: ignore[dict-item]
            return b, editado, otra, mala, no_texto

    b, editado, otra, mala, no_texto = correr(principal())
    assert not b.listo
    assert editado is not None and editado.id == b.id and editado.version == 2
    assert editado.para == (JEFE, "ana@x.com") and editado.cuerpo == "Nuevo\ntexto" and editado.asunto == "Otro"
    assert [a.nombre for a in editado.adjuntos] == ["a.txt"]
    assert editado.listo and editado.huella() != b.huella()
    assert otra is None and mala is None and no_texto is None


def test_editar_quita_un_adjunto(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    (carpetas["descargas"] / "a.txt").write_text("uno")
    (carpetas["descargas"] / "b.txt").write_text("dos")

    async def principal() -> Borrador | None:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            b = await preparar(correo, adjuntos=["a.txt", "b.txt"])
            return await correo.editar(b.id, {"adjuntos": "b.txt"})

    editado = correr(principal())
    assert editado is not None and [a.nombre for a in editado.adjuntos] == ["b.txt"]


def test_editar_otro_campo_conserva_lo_que_no_entro(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    """Los clientes solo mandan los campos cambiados. Editar el asunto o el cuerpo nunca hace desaparecer en silencio
    un destinatario sin resolver, uno con CR/LF, uno que sobraba del tope ni un adjunto no encontrado (§7.3)."""
    s = ajustes.con(correo_max_destinatarios=3)

    async def principal() -> list[tuple[Borrador, Borrador | None]]:
        async with entorno(s, FalsoGmail(), carpetas) as (_, correo):
            pares = []
            for kwargs in ({"para": ["mi tía Rosa", "ana@x.com"]},
                           {"para": ["ana@x.com"], "cc": ["x@y.com\r\nBcc: espia@malo.com"]},
                           {"para": ["a@x.com", "b@x.com"], "cco": ["c@x.com", "d@x.com"]},
                           {"para": ["ana@x.com"], "adjuntos": ["informe.pdf"]}):
                b = await preparar(correo, **kwargs)
                pares.append((b, await correo.editar(b.id, {"asunto": "Saludo", "cuerpo": "Hola a todos"})))
            return pares

    pares = correr(principal())
    for antes, despues in pares:
        assert not antes.listo
        assert despues is not None and despues.version == 2 and despues.asunto == "Saludo"
        assert not despues.listo, despues.problemas
        assert despues.problemas == antes.problemas
        assert (despues.para, despues.cc, despues.cco, despues.adjuntos) == (
            antes.para, antes.cc, antes.cco, antes.adjuntos)
    rosa, saltos, sobra, adjunto = (a for a, _ in pares)
    assert any("«mi tía Rosa» (Para)" in p for p in rosa.problemas)
    assert any("de CC tenía saltos de línea" in p for p in saltos.problemas)
    assert all("espia" not in p for p in saltos.problemas)
    assert sobra.cco == ("c@x.com",) and any("Sobra 1 destinatario (máximo 3): no entró en CCO" in p
                                              for p in sobra.problemas)
    assert adjunto.adjuntos == () and any("informe.pdf" in p for p in adjunto.problemas)
    # Lo pendiente nunca sale del módulo: ni en la vista, ni en la huella, ni en repr.
    for b in (rosa, saltos, sobra):
        texto = json.dumps(b.vista(), ensure_ascii=False) + repr(b)
        assert "espia" not in texto and "d@x.com" not in texto


def test_editar_ese_campo_resuelve_lo_pendiente(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    """Al editar el campo (lo que la tarjeta muestra), lo pendiente se sustituye por lo escrito."""
    (carpetas["descargas"] / "informe.pdf").write_bytes(b"%PDF")

    async def principal() -> tuple[Borrador | None, Borrador | None, Borrador | None]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            b = await preparar(correo, para=["mi tía Rosa", "ana@x.com"], adjuntos=["no_existe.pdf"])
            con_correo = await correo.editar(b.id, {"para": "ana@x.com, rosa@x.com"})
            assert con_correo is not None and not con_correo.listo       # falta resolver el adjunto
            sin_adjunto = await correo.editar(b.id, {"adjuntos": ""})
            otro = await preparar(correo, para=["mi tía Rosa", "ana@x.com"], adjuntos=["no_existe.pdf"])
            cambiado = await correo.editar(otro.id, {"para": "ana@x.com,", "adjuntos": "informe.pdf"})
            return con_correo, sin_adjunto, cambiado

    con_correo, sin_adjunto, cambiado = correr(principal())
    assert con_correo is not None and con_correo.para == ("ana@x.com", "rosa@x.com")
    assert all("Rosa" not in p for p in con_correo.problemas)
    assert sin_adjunto is not None and sin_adjunto.listo and sin_adjunto.adjuntos == ()
    assert sin_adjunto.para == ("ana@x.com", "rosa@x.com")
    assert cambiado is not None and cambiado.listo, cambiado.problemas if cambiado else None
    assert cambiado.para == ("ana@x.com",) and [a.nombre for a in cambiado.adjuntos] == ["informe.pdf"]


def test_editar_con_las_listas_tal_como_se_ven_suelta_lo_pendiente(ajustes: Any, carpetas: dict[str, Path],
                                                                  correr: Any) -> None:
    """Con problemas, los clientes mandan Para, CC, CCO y adjuntos tal como los muestra el formulario (integración):
    «Guardar cambios» sin tocar nada deja el correo como se ve, con el mismo adjunto."""
    (carpetas["documentos"] / "informe.pdf").write_bytes(b"%PDF-1.4 uno")

    async def principal() -> tuple[Borrador, Borrador | None]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            b = await preparar(correo, para=["mi tía Rosa", "ana@x.com"], adjuntos=["informe.pdf"])
            vista = b.vista()
            campos = {"para": ", ".join(vista["para"]), "cc": ", ".join(vista["cc"]), "cco": ", ".join(vista["cco"]),
                      "adjuntos": ", ".join(a["nombre"] for a in vista["adjuntos"])}
            return b, await correo.editar(b.id, campos)

    antes, despues = correr(principal())
    assert not antes.listo and any("Rosa" in p for p in antes.problemas)
    assert despues is not None and despues.listo, despues.problemas if despues else None
    assert despues.para == ("ana@x.com",) and despues.adjuntos[0].ruta == antes.adjuntos[0].ruta
    assert despues.adjuntos[0].sha256 == antes.adjuntos[0].sha256


def test_editar_otro_campo_no_vuelve_a_buscar_los_adjuntos(ajustes: Any, carpetas: dict[str, Path],
                                                         correr: Any) -> None:
    """Un adjunto ya elegido se vuelve a medir (ese mismo archivo), no se busca otra vez por nombre: dos
    «informe.pdf» de carpetas distintas siguen siendo dos, y uno que desaparece sigue bloqueando."""
    (carpetas["descargas"] / "informe.pdf").write_bytes(b"uno")
    (carpetas["documentos"] / "informe.pdf").write_bytes(b"dos")
    (carpetas["escritorio"] / "foto.jpg").write_bytes(b"jpg")

    async def principal() -> tuple[Borrador, Borrador | None, Borrador | None, Borrador | None]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            b = await preparar(correo, adjuntos=["descargas/informe.pdf", "documentos/informe.pdf",
                                                 "escritorio/foto.jpg"])
            uno = await correo.editar(b.id, {"cuerpo": "Te mando los dos"})
            dos = await correo.editar(b.id, {"adjuntos": "informe.pdf, informe.pdf"})   # lo que muestra la tarjeta
            otro = await preparar(correo, adjuntos=["foto.jpg"])
            assert otro.listo and otro.adjuntos[0].carpeta == "escritorio"
            (carpetas["escritorio"] / "foto.jpg").unlink()
            (carpetas["descargas"] / "foto.jpg").write_bytes(b"otra foto")
            tres = await correo.editar(otro.id, {"asunto": "Foto"})
            return b, uno, dos, tres

    b, uno, dos, tres = correr(principal())
    assert b.listo and [a.carpeta for a in b.adjuntos] == ["descargas", "documentos", "escritorio"]
    assert uno is not None and uno.listo and [a.sha256 for a in uno.adjuntos] == [a.sha256 for a in b.adjuntos]
    assert dos is not None and [a.carpeta for a in dos.adjuntos] == ["descargas", "documentos"]
    # La foto elegida (Escritorio) desapareció: no se cambia en silencio por la de Descargas.
    assert tres is not None and not tres.listo and tres.adjuntos == ()
    assert any("«foto.jpg» ya no está" in p for p in tres.problemas)


def test_mas_de_cinco_adjuntos_siguen_bloqueando_al_editar(ajustes: Any, carpetas: dict[str, Path],
                                                          correr: Any) -> None:
    for i in range(7):
        (carpetas["descargas"] / f"a{i}.txt").write_text(str(i))

    async def principal() -> tuple[Borrador, Borrador | None]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            b = await preparar(correo, adjuntos=[f"a{i}.txt" for i in range(7)])
            return b, await correo.editar(b.id, {"cuerpo": "Van todos"})

    b, editado = correr(principal())
    assert any("Como mucho 5 adjuntos: 2 no entraron" in p for p in b.problemas)
    assert editado is not None and not editado.listo and len(editado.adjuntos) == 5
    assert any("Como mucho 5 adjuntos" in p for p in editado.problemas)


def test_caducidad_con_reloj_inyectado(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    ahora = {"t": 1000.0}

    async def principal() -> tuple[Any, ...]:
        async with entorno(ajustes, FalsoGmail(), carpetas, reloj=lambda: ahora["t"]) as (_, correo):
            b = await preparar(correo, ttl_s=60.0)
            vigente = correo.borrador(b.id)
            ahora["t"] += 61
            resultado = await correo.enviar(b.id, b.huella())
            return b, vigente, correo.borrador(b.id), resultado

    b, vigente, despues, resultado = correr(principal())
    assert b.caduca == 1060.0 and vigente is b
    assert despues is None and resultado.codigo == "caducado" and not resultado.pudo_salir


def test_un_solo_borrador_vigente_y_descartar(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def principal() -> tuple[Any, ...]:
        async with entorno(ajustes, FalsoGmail(), carpetas) as (_, correo):
            uno = await preparar(correo)
            dos = await preparar(correo, cuerpo="otro")
            return correo.borrador(uno.id), correo.descartar(uno.id), correo.descartar(dos.id), correo.borrador(dos.id)

    assert correr(principal()) == (None, False, True, None)


# ---------------------------------------------------------------------------
# Enviar
# ---------------------------------------------------------------------------


def test_enviar_ok_mime_y_borrador_consumido(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    (carpetas["descargas"] / "informe.pdf").write_bytes(b"%PDF contenido")
    falso = FalsoGmail()

    async def principal() -> tuple[Borrador, ResultadoEnvio, Any]:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            b = await preparar(correo, para=["mamá", "ana@x.com"], cc=["mi jefe"], cco=["oculto@z.com"],
                               asunto="Reunión mañana", cuerpo="Hola,\nnos vemos.", adjuntos=["informe.pdf"])
            resultado = await correo.enviar(b.id, b.huella())
            return b, resultado, correo.borrador(b.id)

    b, resultado, despues = correr(principal())
    assert resultado.ok and resultado.codigo == "enviado" and resultado.pudo_salir
    assert resultado.gmail_id == "18c0ffee1234"
    assert resultado.n_destinatarios == 4 and resultado.n_adjuntos == 1 and resultado.bytes > 0
    assert "Enviado a Gmail" in resultado.mensaje and MAMA not in resultado.mensaje
    assert despues is None
    assert len(falso.envios) == 1
    peticion = falso.envios[0]
    assert peticion.url.path == "/gmail/v1/users/me/messages/send"
    assert peticion.headers["Authorization"] == f"Bearer {ACCESO}"
    mensaje = decodificar(peticion)
    assert mensaje["From"] is None
    assert mensaje["To"] == f"{MAMA}, ana@x.com"
    assert mensaje["Cc"] == JEFE and mensaje["Bcc"] == "oculto@z.com"
    assert mensaje["Subject"] == "Reunión mañana"
    raw = json.loads(peticion.content)["raw"]
    crudo = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    assert b"=?utf-8?" in crudo.lower() and b"\r\n" in crudo           # RFC 2047 y CRLF (policy SMTP)
    cuerpo = mensaje.get_body(preferencelist=("plain",))
    assert cuerpo is not None and cuerpo.get_content().replace("\r\n", "\n").strip() == "Hola,\nnos vemos."
    adjuntos = list(mensaje.iter_attachments())
    assert len(adjuntos) == 1 and adjuntos[0].get_filename() == "informe.pdf"
    assert adjuntos[0].get_content() == b"%PDF contenido"
    assert adjuntos[0].get_content_type() == "application/pdf"


def test_enviar_mime_grande_usa_el_endpoint_de_subida(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    datos = os.urandom(4 * 1024 * 1024)
    (carpetas["descargas"] / "video.bin").write_bytes(datos)
    falso = FalsoGmail()

    async def principal() -> ResultadoEnvio:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            b = await preparar(correo, adjuntos=["video.bin"], asunto="Video")
            return await correo.enviar(b.id, b.huella())

    resultado = correr(principal())
    assert resultado.ok and resultado.bytes > correo_mod.UMBRAL_SUBIDA_GMAIL
    peticion = falso.envios[0]
    assert peticion.url.path == "/upload/gmail/v1/users/me/messages/send"
    assert peticion.url.params["uploadType"] == "media"
    assert peticion.headers["Content-Type"] == "message/rfc822"
    mensaje = decodificar(peticion)
    assert next(iter(mensaje.iter_attachments())).get_content() == datos


def test_enviar_demasiado_grande(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(correo_mod, "MAX_MIME_GMAIL", 100)
    falso = FalsoGmail()

    async def principal() -> ResultadoEnvio:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            b = await preparar(correo, cuerpo="hola " * 100)
            return await correo.enviar(b.id, b.huella())

    resultado = correr(principal())
    assert resultado.codigo == "demasiado_grande" and not resultado.pudo_salir and falso.envios == []


def test_enviar_comprobaciones_previas(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    (carpetas["descargas"] / "a.txt").write_text("original")
    falso = FalsoGmail()

    async def principal() -> list[ResultadoEnvio]:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            resultados = [await correo.enviar("00000000", "0" * 64)]
            b = await preparar(correo, adjuntos=["a.txt"])
            resultados.append(await correo.enviar(b.id, "f" * 64))
            resultados.append(await correo.enviar(b.id, None))  # type: ignore[arg-type]
            (carpetas["descargas"] / "a.txt").write_text("cambiado")
            resultados.append(await correo.enviar(b.id, b.huella()))
            malo = await preparar(correo, para=["desconocido raro"])
            resultados.append(await correo.enviar(malo.id, malo.huella()))
            return resultados

    resultados = correr(principal())
    assert [r.codigo for r in resultados] == ["no_existe", "huella", "huella", "adjunto_cambio", "no_listo"]
    assert all(not r.pudo_salir and not r.ok for r in resultados)
    assert falso.envios == []


def test_enviar_sin_conectar_o_sin_permiso(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoGmail()

    async def sin_conectar() -> ResultadoEnvio:
        async with entorno(ajustes, falso, carpetas, conectar=False) as (_, correo):
            b = await preparar(correo)
            return await correo.enviar(b.id, b.huella())

    async def sin_gmail() -> ResultadoEnvio:
        async with entorno(ajustes, falso, carpetas, scopes=(SCOPE_DRIVE_READONLY,)) as (_, correo):
            b = await preparar(correo)
            return await correo.enviar(b.id, b.huella())

    async def sin_cuenta() -> ResultadoEnvio:
        correo = Correo(ajustes, None, carpetas=carpetas)
        b = await preparar(correo)
        return await correo.enviar(b.id, b.huella())

    assert correr(sin_conectar()).codigo == "desconectado"
    assert correr(sin_gmail()).codigo == "sin_permiso"
    assert correr(sin_cuenta()).codigo == "desconectado"
    assert falso.envios == []


@pytest.mark.parametrize("respuesta,codigo,pudo_salir", [
    (lambda p: httpx.Response(429, json={"error": {"code": 429}}), "limite", False),
    (lambda p: httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}}),
     "sin_permiso", False),
    (lambda p: httpx.Response(400, json={"error": {"errors": [{"reason": "invalidArgument"}]}}), "rechazado", False),
    (lambda p: httpx.ConnectError("sin conexión", request=p), "red", False),
    (lambda p: httpx.ConnectTimeout("sin conexión", request=p), "red", False),
    (lambda p: httpx.ReadTimeout("timeout tras enviar", request=p), "red_ambigua", True),
    (lambda p: httpx.WriteTimeout("timeout enviando", request=p), "red_ambigua", True),
    (lambda p: httpx.RemoteProtocolError("cortado", request=p), "red_ambigua", True),
    (lambda p: httpx.Response(500), "error", True),
    (lambda p: httpx.Response(503), "error", True),
])
def test_enviar_fallos_sin_reintentos(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                      respuesta: Callable[[httpx.Request], Any], codigo: str, pudo_salir: bool) -> None:
    falso = FalsoGmail()
    falso.respuesta = respuesta

    async def principal() -> tuple[Borrador, ResultadoEnvio, Borrador | None]:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            b = await preparar(correo)
            resultado = await correo.enviar(b.id, b.huella())
            return b, resultado, correo.borrador(b.id)

    b, resultado, despues = correr(principal())
    assert resultado.codigo == codigo and not resultado.ok
    assert resultado.pudo_salir is pudo_salir
    assert len(falso.envios) == 1                  # nunca se reintenta solo
    assert despues is b                            # ante un fallo, el borrador se conserva
    if codigo == "red_ambigua":
        assert "revisa Enviados" in resultado.mensaje


def test_enviar_fallo_al_renovar_el_permiso_no_sale_nada(ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    falso = FalsoGmail()

    async def manejador(peticion: httpx.Request) -> httpx.Response:
        if peticion.url.path == "/token":
            raise httpx.ReadTimeout("google lento", request=peticion)
        return await falso(peticion)

    async def principal() -> ResultadoEnvio:
        preparar_cuenta(ajustes)
        async with httpx.AsyncClient(transport=httpx.MockTransport(manejador)) as cliente:
            cuenta = CuentaGoogle(ajustes, None, cliente=cliente, protector=ProtectorNinguno())

            async def dormir(_: float) -> None:
                return None

            cuenta._dormir = dormir  # type: ignore[assignment]
            await cuenta.iniciar()
            correo = Correo(ajustes, cuenta, carpetas=carpetas)
            b = await preparar(correo)
            return await correo.enviar(b.id, b.huella())

    resultado = correr(principal())
    assert resultado.codigo == "red" and not resultado.pudo_salir
    assert falso.envios == []


def test_pudo_salir_por_codigo() -> None:
    for codigo in correo_mod.CODIGOS_ENVIO:
        esperado = codigo in ("enviado", "red_ambigua", "error")
        assert ResultadoEnvio(codigo == "enviado", codigo, "x").pudo_salir is esperado, codigo
    assert "red" in correo_mod.CODIGOS_ENVIO and "red" in correo_mod.CODIGOS_SIN_SALIDA


# ---------------------------------------------------------------------------
# Diagnóstico y logs
# ---------------------------------------------------------------------------


def test_diagnosticos(settings_con_claves: Any, ajustes: Any, carpetas: dict[str, Path], correr: Any) -> None:
    async def diagnostico(s: Any, **kwargs: Any) -> Any:
        async with entorno(s, FalsoGmail(), carpetas, **kwargs) as (_, correo):
            return correo.diagnostico(), correo.disponible

    off, disponible = correr(diagnostico(settings_con_claves, conectar=False))
    assert off.clave == "correo" and off.estado.value == "OFF" and not disponible
    sin_servicio, _ = correr(diagnostico(ajustes.con(google_servicios=("drive",)), conectar=False))
    assert sin_servicio.estado.value == "OFF" and "RAMS_GOOGLE_SERVICIOS" in sin_servicio.detalle
    espera, _ = correr(diagnostico(ajustes, conectar=False))
    assert espera.estado.value == "EN ESPERA"
    degradado, _ = correr(diagnostico(ajustes, scopes=(SCOPE_DRIVE_READONLY,)))
    assert degradado.estado.value == "DEGRADADO" and "gmail.send" in degradado.detalle
    ok, disponible = correr(diagnostico(ajustes))
    assert ok.estado.value == "OK" and disponible and ok.nombre == "Correo (Gmail)"
    assert "30/día" in ok.detalle and "10 destinatarios" in ok.detalle


def test_logs_sin_direcciones_asunto_cuerpo_ni_archivos(ajustes: Any, carpetas: dict[str, Path], correr: Any,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams")
    (carpetas["descargas"] / "presupuesto_secreto.pdf").write_bytes(b"datos")
    falso = FalsoGmail()

    async def principal() -> None:
        async with entorno(ajustes, falso, carpetas) as (_, correo):
            b = await preparar(correo, para=["mamá", "ana@x.com"], asunto="Asunto privado",
                               cuerpo="Cuerpo muy privado", adjuntos=["presupuesto_secreto.pdf"])
            await correo.editar(b.id, {"cuerpo": "Otro cuerpo privado"})
            b2 = correo.borrador(b.id)
            assert b2 is not None
            await correo.enviar(b2.id, b2.huella())
            falso.respuesta = lambda p: httpx.ReadTimeout("x", request=p)
            b3 = await preparar(correo, para=["ana@x.com"], cuerpo="Tercero privado")
            await correo.enviar(b3.id, b3.huella())

    correr(principal())
    texto = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("rams"))
    assert "rams.correo" in {r.name for r in caplog.records}
    for privado in (MAMA, "ana@x.com", "Asunto privado", "Cuerpo muy privado", "Otro cuerpo privado",
                    "Tercero privado", "presupuesto_secreto", "mamá", REFRESH, ACCESO, str(carpetas["descargas"])):
        assert privado not in texto, privado
