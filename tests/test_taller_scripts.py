"""Tests de lobes/taller_scripts.py (grupo taller, docs/V022_CORREO_DRIVE_SCRIPTS.md §9 y §18).

Sin efectos reales: el analizador de PowerShell, el generador (Gemini), la sonda de límites de Linux
y la comprobación de «elevado» SIEMPRE se inyectan; la detección de intérpretes se sustituye por una
falsa. ``conftest`` prohíbe powershell, pwsh, bash, sh, systemd-run, systemctl y prlimit. El lanzador
real solo se prueba con scripts Python INOCUOS (``sys.executable``) en ``tmp_path``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import lobes.taller_scripts as ts
from core.config import Settings
from core.contracts import (
    CATEGORIAS_RIESGO,
    MAX_CODIGO_SCRIPT,
    MAX_HALLAZGOS_TARJETA,
    NIVELES_RIESGO,
    huella_contenido,
)
from lobes.taller_scripts import (
    CATALOGO_RIESGO,
    Analisis,
    FalloTaller,
    Hallazgo,
    PropuestaScript,
    ResultadoScript,
    SalidaProceso,
    TallerScripts,
    analizar_bash,
    analizar_powershell_ast,
    analizar_powershell_heuristico,
    analizar_python,
)

RAIZ = Path(__file__).resolve().parents[1]
POWERSHELL_FALSO = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
BASH_FALSO = "/usr/bin/bash"
JSON_VACIO: dict[str, Any] = {"errores": [], "comandos": [], "miembros": [], "tipos": [], "variables": [],
                              "usings": []}
CLAVES_VISTA = {"lenguaje", "plataforma", "codigo", "explicacion", "efectos", "requisitos", "nivel", "ejecutable",
                "motivo_no_ejecutable", "metodo", "hallazgos", "hallazgos_omitidos", "habilidad",
                "habilidad_cambiada", "ejecuciones"}


# ---------------------------------------------------------------------------
# Dobles y fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sin_sistema_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intérpretes falsos; la sonda real y la comprobación real de «elevado» nunca se usan."""

    def interpretes(plataforma: str) -> dict[str, str]:
        if plataforma == "win32":
            return {"powershell": POWERSHELL_FALSO, "python": sys.executable}
        if str(plataforma).startswith("linux"):
            return {"bash": BASH_FALSO, "python": sys.executable}
        return {"python": sys.executable}

    async def sonda_prohibida() -> str:
        raise AssertionError("se intentó usar la sonda real de límites")

    def elevado_prohibido(*_a: Any, **_k: Any) -> bool | None:
        raise AssertionError("se intentó comprobar la elevación real")

    async def analizador_prohibido(self: Any, ruta: Path) -> Any:
        raise AssertionError("se intentó lanzar el analizador real de PowerShell")

    monkeypatch.setattr(ts, "detectar_interpretes", interpretes)
    monkeypatch.setattr(ts, "sonda_limites_linux", sonda_prohibida)
    monkeypatch.setattr(ts, "es_elevado_por_defecto", elevado_prohibido)
    monkeypatch.setattr(ts.TallerScripts, "_analizador_ps_por_defecto", analizador_prohibido)


@pytest.fixture
def settings_taller(settings_prueba: Settings) -> Settings:
    return settings_prueba.con(taller_habilitado=True)


@pytest.fixture
def carpetas(tmp_path: Path) -> dict[str, Path]:
    resultado = {}
    for clave, nombre in (("descargas", "Descargas"), ("documentos", "Documentos")):
        ruta = tmp_path / "usuario" / nombre
        ruta.mkdir(parents=True)
        resultado[clave] = ruta
    return resultado


class Reloj:
    def __init__(self, ahora: float = 1_000_000.0) -> None:
        self.ahora = ahora

    def __call__(self) -> float:
        return self.ahora


class GeneradorFalso:
    def __init__(self, respuesta: Any) -> None:
        self.respuesta = respuesta
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tarea: str, **kw: Any) -> Any:
        self.llamadas.append((tarea, kw))
        return self.respuesta


def respuesta(codigo: str, lenguaje: str = "python", **extra: Any) -> dict[str, Any]:
    datos = {"lenguaje": lenguaje, "codigo": codigo, "explicacion": "Hace algo inocuo.",
             "efectos": ["Imprime un saludo"], "requisitos": []}
    datos.update(extra)
    return datos


class LanzadorFalso:
    """Registra la llamada (argv, cwd, env, límites) y el contenido del archivo del script."""

    def __init__(self, *, codigo_salida: int | None = 0, stdout: bytes = b"hola\n", stderr: bytes = b"",
                 tiempo_agotado: bool = False, recortada: bool = False, error: BaseException | None = None,
                 espera: asyncio.Event | None = None) -> None:
        self.codigo_salida = codigo_salida
        self.stdout = stdout
        self.stderr = stderr
        self.tiempo_agotado = tiempo_agotado
        self.recortada = recortada
        self.error = error
        self.espera = espera
        self.llamadas: list[dict[str, Any]] = []
        self.cancelado = False

    async def __call__(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float,
                       max_salida: int, max_procesos: int, max_memoria_mb: int,
                       unidad: str | None = None) -> SalidaProceso:
        archivo = Path(cwd) / argv[-1]
        self.llamadas.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env), "timeout_s": timeout_s,
                              "max_salida": max_salida, "max_procesos": max_procesos,
                              "max_memoria_mb": max_memoria_mb, "unidad": unidad,
                              "contenido": archivo.read_bytes() if archivo.is_file() else None})
        if self.error is not None:
            raise self.error
        if self.espera is not None:
            try:
                await self.espera.wait()
            except asyncio.CancelledError:
                self.cancelado = True
                raise
        return SalidaProceso(self.codigo_salida, self.tiempo_agotado, 1.25, self.stdout, self.stderr,
                             self.recortada, len(self.stdout) + len(self.stderr), True)


async def analizador_vacio(ruta: Path) -> Mapping[str, Any]:
    return dict(JSON_VACIO)


def sonda(modo: str):
    llamadas: list[int] = []

    async def falsa() -> str:
        llamadas.append(1)
        return modo

    falsa.llamadas = llamadas  # type: ignore[attr-defined]
    return falsa


def crear_taller(settings: Settings, *, plataforma: str = "win32", generador: Any = None, lanzador: Any = None,
                 analizador_ps: Any = analizador_vacio, carpetas: Mapping[str, Path] | None = None,
                 sonda_limites: Any = None, elevado: bool | None = False, reloj: Reloj | None = None) -> TallerScripts:
    return TallerScripts(settings, generador=generador, plataforma=plataforma, lanzador=lanzador or LanzadorFalso(),
                         analizador_ps=analizador_ps, carpetas=carpetas if carpetas is not None else {},
                         sonda_limites=sonda_limites or sonda("ninguno"), es_elevado=lambda: elevado,
                         reloj=reloj or Reloj())


def niveles_y_categorias(analisis: Analisis) -> set[tuple[str, str]]:
    return {(h.nivel, h.categoria) for h in analisis.hallazgos}


C_PERMITIDAS = [Path(r"C:\Users\ana\Downloads"), Path(r"C:\Users\ana\Documents"),
                Path(r"C:\rams\data\scripts\ejecuciones")]
C_PROTEGIDAS = [Path(r"C:\rams"), Path(r"C:\rams\data")]


def py(codigo: str) -> Analisis:
    return analizar_python(codigo, carpetas=C_PERMITIDAS, protegidas=C_PROTEGIDAS)


def sh(codigo: str) -> Analisis:
    return analizar_bash(codigo, carpetas=C_PERMITIDAS, protegidas=C_PROTEGIDAS)


def ps_heur(codigo: str) -> Analisis:
    return analizar_powershell_heuristico(codigo, carpetas=C_PERMITIDAS, protegidas=C_PROTEGIDAS)


def ps_ast(comandos: list[dict[str, Any]], codigo: str, **extra: Any) -> Analisis:
    datos = {**JSON_VACIO, "comandos": comandos, **extra}
    return analizar_powershell_ast(datos, codigo, carpetas=C_PERMITIDAS, protegidas=C_PROTEGIDAS)


def comando(nombre: str | None, linea: int = 1, *, params: Sequence[str] = (), literales: Sequence[str] = (),
            operador: str = "Unknown") -> dict[str, Any]:
    return {"nombre": nombre, "dinamico": nombre is None, "operador": operador, "params": list(params),
            "literales": list(literales), "linea": linea}


def miembro(objeto: str, nombre: str, linea: int = 1, *, estatico: bool = False) -> dict[str, Any]:
    return {"objeto": objeto, "miembro": nombre, "estatico": estatico, "linea": linea}


# ---------------------------------------------------------------------------
# Catálogo (§9.4, normativo)
# ---------------------------------------------------------------------------


def test_catalogo_fijado() -> None:
    assert len(CATALOGO_RIESGO) == 500
    claves = [(f.lenguaje, f.patron) for f in CATALOGO_RIESGO]
    assert len(claves) == len(set(claves)), "filas repetidas en el catálogo"
    for fila in CATALOGO_RIESGO:
        assert fila.lenguaje in ("*", "powershell", "bash", "python")
        assert fila.categoria in CATEGORIAS_RIESGO
        assert fila.nivel in NIVELES_RIESGO
        assert 0 < len(fila.detalle) <= 160 and fila.patron
    # Todas las categorías del contrato tienen al menos una fila.
    assert {f.categoria for f in CATALOGO_RIESGO} == set(CATEGORIAS_RIESGO)


@pytest.mark.parametrize(("lenguaje", "patron", "categoria", "nivel"), [
    ("powershell", "remove-item+raiz", "borrado", "critico"),
    ("powershell", "format-volume", "borrado", "critico"),
    ("powershell", "start-process+runas", "elevacion", "critico"),
    ("powershell", "set-mppreference", "seguridad", "critico"),
    ("powershell", "set-executionpolicy", "seguridad", "critico"),
    ("powershell", "descarga+iex", "descarga_ejecucion", "critico"),
    ("powershell", "-encodedcommand", "descarga_ejecucion", "critico"),
    ("powershell", "frombase64string+iex", "ofuscacion", "critico"),
    ("powershell", "register-scheduledtask", "persistencia", "critico"),
    ("powershell", "stop-computer", "apagado", "critico"),
    ("powershell", "remove-item", "borrado", "alto"),
    ("powershell", "add-type", "winapi", "alto"),
    ("powershell", "& $variable", "dinamico", "alto"),
    ("powershell", "frombase64string", "ofuscacion", "alto"),
    ("powershell", "get-process", "servicios", "medio"),
    ("powershell", "read-host", "interactivo", "medio"),
    ("bash", "rm+raiz", "borrado", "critico"),
    ("bash", "sudo", "elevacion", "critico"),
    ("bash", "curl|sh", "descarga_ejecucion", "critico"),
    ("bash", "crontab", "persistencia", "critico"),
    ("bash", "eval", "dinamico", "alto"),
    ("bash", "read", "interactivo", "medio"),
    ("python", "shutil.rmtree+raiz", "borrado", "critico"),
    ("python", "marshal.loads", "ofuscacion", "critico"),
    ("python", "import:keyring", "credenciales", "critico"),
    ("python", "os.remove", "borrado", "alto"),
    ("python", "subprocess.*", "subproceso", "alto"),
    ("python", "import:ctypes", "winapi", "alto"),
    ("python", "input", "interactivo", "medio"),
    ("*", "archivo_sensible", "credenciales", "critico"),
    ("*", "ruta_proyecto", "credenciales", "critico"),
    ("*", "appdata", "fuera_de_carpeta", "alto"),
    ("*", "appdata+credenciales", "credenciales", "critico"),
    ("*", "ruta_sistema", "fuera_de_carpeta", "critico"),
    ("*", "ruta_padre", "fuera_de_carpeta", "alto"),
    ("*", "hklm", "registro", "critico"),
    ("*", "sintaxis", "sintaxis", "alto"),
    ("*", "degradado", "analisis_degradado", "alto"),
    ("*", "tamano", "tamano", "critico"),
])
def test_filas_clave_del_catalogo(lenguaje: str, patron: str, categoria: str, nivel: str) -> None:
    fila = next(f for f in CATALOGO_RIESGO if (f.lenguaje, f.patron) == (lenguaje, patron))
    assert (fila.categoria, fila.nivel) == (categoria, nivel)


def test_sintaxis_no_es_critico() -> None:
    fila = next(f for f in CATALOGO_RIESGO if f.patron == "sintaxis")
    assert fila.nivel != "critico"


# ---------------------------------------------------------------------------
# Análisis de Python
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("codigo", "nivel", "categoria"), [
    ('print("hola")\n', "bajo", None),
    ('import math, json, datetime\nprint(math.sqrt(2))\n', "bajo", None),
    ('import re\nprint(re.sub(r"\\s+", " ", "a  b"))\n', "bajo", None),
    ('import datetime\nprint(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))\n', "bajo", None),
    ('x = input("¿?")\n', "medio", "interactivo"),
    ('with open("salida.txt", "w") as f:\n    f.write("x")\n', "medio", "sobrescritura"),
    ('from pathlib import Path\n(Path(__file__).parent / "salida.txt").write_text("x")\n', "medio", "sobrescritura"),
    ('import os\nfrom pathlib import Path\nprint(os.listdir(Path.home() / "Downloads"))\n', "medio",
     "fuera_de_carpeta"),
    ('from pathlib import Path\n(Path.home() / "Downloads" / "x.txt").write_text("x")\n', "alto", "sobrescritura"),
    ('import os\nos.remove("x.txt")\n', "alto", "borrado"),
    ('import requests\nprint(requests.get("https://x").text)\n', "alto", "red"),
    ('import subprocess\nsubprocess.run(["git", "status"])\n', "alto", "subproceso"),
    ('import ctypes\nprint(ctypes.sizeof(ctypes.c_int))\n', "alto", "winapi"),
    ('x = eval("1+1")\n', "alto", "dinamico"),
    ('import base64\nprint(base64.b64decode("aGk="))\n', "alto", "ofuscacion"),
    ('import winreg\nprint(winreg.HKEY_CURRENT_USER)\n', "alto", "registro"),
    ('open("../x.txt").read()\n', "alto", "fuera_de_carpeta"),
    ('open(r"D:\\datos\\x.txt").read()\n', "alto", "fuera_de_carpeta"),
    ('import shutil\nfrom pathlib import Path\nshutil.rmtree(Path.home())\n', "critico", "borrado"),
    ('import shutil\nshutil.rmtree("/")\n', "critico", "borrado"),
    ('import base64\nexec(base64.b64decode("cHJpbnQoMSk="))\n', "critico", "ofuscacion"),
    ('import pickle\npickle.loads(b"x")\n', "critico", "ofuscacion"),
    ('import subprocess\nsubprocess.run(["shutdown", "/s", "/t", "0"])\n', "critico", "apagado"),
    ('import os\nos.system("sudo rm -rf /")\n', "critico", "elevacion"),
    ('import subprocess\nsubprocess.run(["schtasks", "/create", "/tn", "x"])\n', "critico", "persistencia"),
    ('import subprocess, sys\nsubprocess.run([sys.executable, "-m", "pip", "install", "x"])\n', "critico",
     "descarga_ejecucion"),
    ('import urllib.request\nexec(urllib.request.urlopen("https://x").read())\n', "critico", "descarga_ejecucion"),
    ('import ctypes\nctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", None, None, 1)\n', "critico",
     "elevacion"),
    ('import winreg\nwinreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE")\n', "critico", "registro"),
    ('import winreg\nk = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"\n', "critico", "persistencia"),
    ('import keyring\nprint(keyring.get_password("a", "b"))\n', "critico", "credenciales"),
    ('print(open(".env").read())\n', "critico", "credenciales"),
    ('print(open("~/.ssh/id_rsa").read())\n', "critico", "credenciales"),
    ('open(r"C:\\Windows\\System32\\drivers\\etc\\hosts").read()\n', "critico", "fuera_de_carpeta"),
    ('open(r"C:\\rams\\core\\config.py").read()\n', "critico", "credenciales"),
    ('open(r"C:\\rams\\data\\memoria.db").read()\n', "critico", "credenciales"),
])
def test_analizar_python_por_categoria(codigo: str, nivel: str, categoria: str | None) -> None:
    analisis = py(codigo)
    assert analisis.metodo == "ast"
    assert analisis.sintaxis_ok
    assert analisis.nivel == nivel, analisis.hallazgos
    if categoria is not None:
        assert (nivel, categoria) in niveles_y_categorias(analisis), analisis.hallazgos
    assert analisis.ejecutable == (nivel != "critico")


def test_analizar_python_resuelve_alias() -> None:
    for codigo in ('import shutil as sh\nsh.rmtree("salida")\n', 'from os import remove\nremove("x.txt")\n',
                   'import os\nborrar = os.remove\nborrar("x.txt")\n',
                   'from os import remove as quitar\nquitar("x.txt")\n'):
        assert ("alto", "borrado") in niveles_y_categorias(py(codigo)), codigo


def test_analizar_python_dinamicos() -> None:
    for codigo in ('getattr(__import__("os"), "system")("dir")\n', 'import os\nn = "system"\ngetattr(os, n)("x")\n',
                   'f = globals()["eval"]\nf("1")\n', 'tabla = {}\ntabla["x"]()\n'):
        analisis = py(codigo)
        assert ("alto", "dinamico") in niveles_y_categorias(analisis), codigo
        assert analisis.nivel in ("alto", "critico")


@pytest.mark.parametrize(("codigo", "nivel", "categoria"), [
    # sys.modules por subíndice o con .get: se resuelve si la clave es constante, y siempre es dinámico.
    ('import sys\nsys.modules["os"].remove("notas.txt")\n', "alto", "borrado"),
    ('import sys\nsys.modules["pickle"].loads(b"")\n', "critico", "ofuscacion"),
    ('import sys\nsys.modules.get("os").remove("x")\n', "alto", "borrado"),
    ('import importlib\nimportlib.sys.modules["os"].system("x")\n', "alto", "dinamico"),
    ('import sys\nm = sys.modules\nm["os"].remove("x")\n', "alto", "dinamico"),
    ('import sys\nclave = "os"\nsys.modules[clave].remove("x")\n', "alto", "dinamico"),
    # Un módulo usado como valor (contenedor, argumento, valor por defecto, lambda, bucle).
    ('import os\nm = [os][0]\nm.remove("notas.txt")\n', "alto", "dinamico"),
    ('import os\nd = {"a": os}\nd["a"].remove("x")\n', "alto", "dinamico"),
    ('import os\ndef f(m):\n    m.remove("notas.txt")\nf(os)\n', "alto", "dinamico"),
    ('import os\ndef f(m=os):\n    m.remove("x")\n', "alto", "dinamico"),
    ('import os\nf = lambda: os\nf().remove("x")\n', "alto", "dinamico"),
    ('import os\nfor m in (os,):\n    m.remove("x")\n', "alto", "dinamico"),
    ('import os\nclass A:\n    def __init__(s, m):\n        s.m = m\nA(os).m.remove("x")\n', "alto", "dinamico"),
    # Una función del catálogo usada como valor, o un módulo reexportado por otro.
    ('import os\nlist(map(os.remove, ["a.txt"]))\n', "alto", "borrado"),
    ('import pickle\ncargar = [pickle.loads][0]\ncargar(b"")\n', "critico", "ofuscacion"),
    ('import shutil\nshutil.os.remove("x")\n', "alto", "borrado"),
    ('f = eval\nprint(f("1+1"))\n', "alto", "dinamico"),
    ('import os\nf = getattr(os, "remove")\nf("x")\n', "alto", "borrado"),
])
def test_analizar_python_modulo_por_via_indirecta(codigo: str, nivel: str, categoria: str) -> None:
    """§9.4: un llamado no resoluble cuenta como dinámico; una fila crítica nunca baja a «bajo»."""
    analisis = py(codigo)
    assert analisis.nivel == nivel, analisis.hallazgos
    assert (nivel, categoria) in niveles_y_categorias(analisis), analisis.hallazgos
    assert analisis.ejecutable == (nivel != "critico")


@pytest.mark.parametrize("codigo", [
    'import sys\nprint(getattr(sys, "frozen", False), sys.argv, sys.platform)\nsys.exit(0)\n',
    'import os\nprint(os.sep.join(["a", "b"]), os.linesep, os.name)\n',
    'import argparse\nfrom pathlib import Path\np = argparse.ArgumentParser()\np.add_argument("x", type=Path)\n',
    'import math\nprint(list(map(math.floor, [1.5])), math.pi)\n',
    'import json\ntry:\n    json.loads("{}")\nexcept json.JSONDecodeError as e:\n    print(e)\n',
    'lista = [1, 2]\nlista.remove(1)\ntexto = "a-b".replace("-", "_")\nprint(lista, texto)\n',
    'import datetime\nprint(datetime.datetime.now().isoformat(), datetime.timedelta(days=1))\n',
])
def test_analizar_python_valores_inocuos_siguen_en_bajo(codigo: str) -> None:
    analisis = py(codigo)
    assert analisis.nivel == "bajo", analisis.hallazgos


def test_analizar_python_metodo_sobre_variable_de_ruta() -> None:
    analisis = py('from pathlib import Path\narchivo = Path("viejo.txt")\narchivo.unlink()\n')
    assert ("alto", "borrado") in niveles_y_categorias(analisis)


def test_analizar_python_sintaxis_rota() -> None:
    analisis = py('def x(:\n    pass\n')
    assert not analisis.sintaxis_ok and not analisis.ejecutable
    assert analisis.nivel != "critico"
    assert any(h.categoria == "sintaxis" and h.linea == 1 for h in analisis.hallazgos)
    assert "sintaxis" in analisis.motivo_no_ejecutable()


def test_analizar_python_tamano() -> None:
    analisis = py("x = 1\n" + "#" * MAX_CODIGO_SCRIPT)
    assert not analisis.ejecutable and any(h.categoria == "tamano" for h in analisis.hallazgos)


@pytest.mark.parametrize(("codigo", "nivel"), [
    ('import os\nprint(os.environ["APPDATA"])\n', "alto"),
    ('import os\nprint(os.getenv("LOCALAPPDATA"))\n', "alto"),
    ('import os\nprint(os.environ.get("APPDATA"))\n', "alto"),
    ('from pathlib import Path\nprint(Path.home() / "AppData" / "Roaming")\n', "alto"),
    ('import os\nprint(os.path.expanduser("~/.config/algo"))\n', "alto"),
    ('import os\nprint(os.path.join(os.path.expanduser("~"), ".local", "share", "x"))\n', "alto"),
    ('import os\nr = os.path.join(os.getenv("LOCALAPPDATA"), "Google", "Chrome", "User Data")\nprint(r)\n',
     "critico"),
    ('from pathlib import Path\nr = Path.home() / "AppData" / "Local"\nprint(r, "rams", "escritorio.json")\n',
     "critico"),
])
def test_rutas_construidas_de_appdata_en_python(codigo: str, nivel: str) -> None:
    analisis = py(codigo)
    assert analisis.nivel == nivel, analisis.hallazgos
    assert ("alto", "fuera_de_carpeta") in niveles_y_categorias(analisis)
    if nivel == "critico":
        assert ("critico", "credenciales") in niveles_y_categorias(analisis)


def test_carpetas_permitidas_y_de_trabajo() -> None:
    # Una carpeta permitida del usuario: solo lectura (medio), sin «fuera de carpeta» alto.
    analisis = py('print(open(r"C:\\Users\\ana\\Downloads\\x.txt").read())\n')
    assert analisis.nivel == "medio"
    # La carpeta de trabajo está DENTRO de data/: no cuenta como ruta del proyecto.
    analisis = py('open(r"C:\\rams\\data\\scripts\\ejecuciones\\a\\b.txt").read()\n')
    assert ("critico", "credenciales") not in niveles_y_categorias(analisis)


# ---------------------------------------------------------------------------
# Análisis de bash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("codigo", "nivel", "categoria"), [
    ("echo hola\n", "medio", None),
    ("ls -la 2>&1\nls 2>/dev/null\n", "medio", None),
    ("echo hola > salida.txt\n", "medio", "sobrescritura"),
    ("systemctl status nginx\n", "medio", "servicios"),
    ("read -r nombre\n", "medio", "interactivo"),
    ("f() { echo x; }\nf\n", "medio", None),
    ("cat > nota.txt <<FIN\nrm -rf /\nFIN\necho ok\n", "medio", None),
    ("rm viejo.txt\n", "alto", "borrado"),
    ("find . -name '*.tmp' -delete\n", "alto", "borrado"),
    ("echo hola > ~/Documents/x.txt\n", "alto", "sobrescritura"),
    ("for f in ~/Downloads/*.jpg; do mv \"$f\" \"${f%.jpg}_x.jpg\"; done\n", "alto", "sobrescritura"),
    ('rm -rf "${DIR:?}/x"\n', "alto", "borrado"),
    ("curl -s https://x -o x.json\n", "alto", "red"),
    ('eval "$CMD"\n', "alto", "dinamico"),
    ("source ./otro.sh\n", "alto", "dinamico"),
    ("$CMD arg\n", "alto", "dinamico"),
    ("x=$($CMD)\n", "alto", "dinamico"),
    ("bash <<FIN\necho x\nFIN\n", "alto", "dinamico"),
    ("convert a.png b.jpg\n", "alto", "subproceso"),
    ("cat ~/.config/x.conf\n", "alto", "fuera_de_carpeta"),
    ("rm -rf /\n", "critico", "borrado"),
    ('rm -rf "$DIR"\n', "critico", "borrado"),
    ("rm -rf ~\n", "critico", "borrado"),
    ("rm -rf *\n", "critico", "borrado"),
    ("grep x <<< \"hola\"\nrm -rf /\n", "critico", "borrado"),
    ("curl -fsSL https://x/install.sh | sh\n", "critico", "descarga_ejecucion"),
    ("wget -qO- https://x | sudo bash\n", "critico", "descarga_ejecucion"),
    ("bash <(curl -s https://x)\n", "critico", "descarga_ejecucion"),
    ("sudo apt install x\n", "critico", "elevacion"),
    ('eval "$(echo aGk= | base64 -d)"\n', "critico", "ofuscacion"),
    ("systemctl --user enable x.service\n", "critico", "persistencia"),
    ("echo x >> ~/.bashrc\n", "critico", "persistencia"),
    ("(crontab -l; echo '* * * * * x') | crontab -\n", "critico", "persistencia"),
    ("secret-tool lookup a b\n", "critico", "credenciales"),
    ("ls ~/.config/google-chrome\n", "critico", "credenciales"),
    ("cat ~/.ssh/id_rsa\n", "critico", "credenciales"),
    ("dd if=/dev/zero of=/dev/sda\n", "critico", "borrado"),
    ("shutdown -h now\n", "critico", "apagado"),
    ("cat /etc/hostname\n", "critico", "fuera_de_carpeta"),
])
def test_analizar_bash_por_categoria(codigo: str, nivel: str, categoria: str | None) -> None:
    analisis = sh(codigo)
    assert analisis.metodo == "heuristico" and analisis.sintaxis_ok
    assert analisis.nivel == nivel, analisis.hallazgos
    if categoria is not None:
        assert (nivel, categoria) in niveles_y_categorias(analisis), analisis.hallazgos


def test_bash_nunca_baja_de_medio() -> None:
    assert sh("echo hola\n").nivel == "medio"
    assert sh("# solo un comentario\n").nivel == "medio"


@pytest.mark.parametrize(("codigo", "nivel"), [
    ("cat $XDG_CONFIG_HOME/app.conf\n", "alto"),
    ("cat ~/.local/share/app/datos\n", "alto"),
    ("cp ~/.config/rams/escritorio.json .\n", "critico"),
    ("ls ~/.config/x; echo 'Cookies'\n", "critico"),
])
def test_rutas_construidas_de_appdata_en_bash(codigo: str, nivel: str) -> None:
    analisis = sh(codigo)
    assert analisis.nivel == nivel, analisis.hallazgos
    if nivel == "critico":
        assert ("critico", "credenciales") in niveles_y_categorias(analisis)


# ---------------------------------------------------------------------------
# Análisis de PowerShell (AST del analizador del repo y heurística)
# ---------------------------------------------------------------------------


def test_powershell_ast_remove_item_recursivo_en_la_raiz() -> None:
    analisis = ps_ast([comando("Remove-Item", params=["Recurse", "Force"], literales=["C:\\"])],
                      "Remove-Item -Recurse -Force C:\\\n")
    assert analisis.metodo == "ast" and analisis.nivel == "critico" and not analisis.ejecutable
    assert ("critico", "borrado") in niveles_y_categorias(analisis)


def test_powershell_ast_remove_item_del_perfil_entero() -> None:
    codigo = 'Remove-Item -Recurse "$env:USERPROFILE\\*"\n'
    analisis = ps_ast([comando("Remove-Item", params=["Recurse"], literales=["$env:USERPROFILE\\*"])], codigo)
    assert ("critico", "borrado") in niveles_y_categorias(analisis)


def test_powershell_ast_dinamico() -> None:
    analisis = ps_ast([comando(None, 2, operador="Ampersand")], '$n = "Get-Date"\n& $n\n')
    assert analisis.nivel == "alto" and ("alto", "dinamico") in niveles_y_categorias(analisis)


def test_powershell_ast_nombre_dinamico_que_acaba_en_iex() -> None:
    analisis = ps_ast([comando(None, 1, operador="Ampersand", literales=["Write-Output 1"])],
                      "& ('ie' + 'x') 'Write-Output 1'\n")
    assert ("critico", "ofuscacion") in niveles_y_categorias(analisis)


def test_powershell_ast_base64_mas_iex() -> None:
    codigo = '$c = [Convert]::FromBase64String("aGk=")\niex ([Text.Encoding]::UTF8.GetString($c))\n'
    analisis = ps_ast([comando("iex", 2)], codigo,
                      miembros=[{"objeto": "[Convert]", "miembro": "FromBase64String", "estatico": True, "linea": 1}])
    assert analisis.nivel == "critico"
    assert ("critico", "ofuscacion") in niveles_y_categorias(analisis)


def test_powershell_ast_descarga_mas_iex() -> None:
    analisis = ps_ast([comando("iwr", 1, literales=["https://x/a.ps1"]), comando("iex", 1)],
                      "iwr https://x/a.ps1 | iex\n")
    assert ("critico", "descarga_ejecucion") in niveles_y_categorias(analisis)


def test_powershell_ast_bajo_y_apps_conocidas() -> None:
    codigo = 'Write-Output "hola"\nStart-Process notepad\nGet-Date\n'
    analisis = ps_ast([comando("Write-Output", 1, literales=["hola"]), comando("Start-Process", 2, literales=["notepad"]),
                       comando("Get-Date", 3)], codigo)
    assert analisis.nivel == "bajo" and analisis.ejecutable and not analisis.hallazgos


def test_powershell_ast_errores_de_sintaxis() -> None:
    analisis = ps_ast([], "if ($true) {\n", errores=[{"mensaje": "Falta la llave", "linea": 1}])
    assert not analisis.sintaxis_ok and not analisis.ejecutable and analisis.nivel != "critico"


@pytest.mark.parametrize(("comandos", "codigo", "nivel", "categoria"), [
    ([comando("Start-Process", literales=["powershell"], params=["Verb"])], "Start-Process powershell -Verb RunAs\n",
     "critico", "elevacion"),
    ([comando("Set-MpPreference", params=["DisableRealtimeMonitoring"])],
     "Set-MpPreference -DisableRealtimeMonitoring $true\n", "critico", "seguridad"),
    ([comando("schtasks", literales=["/create", "/tn", "x"])], "schtasks /create /tn x\n", "critico", "persistencia"),
    ([comando("shutdown", literales=["/s", "/t", "0"])], "shutdown /s /t 0\n", "critico", "apagado"),
    ([comando("cmd", literales=["/c", "del /s /q C:\\"])], 'cmd /c "del /s /q C:\\"\n', "critico", "borrado"),
    ([comando("Set-ItemProperty", params=["Path"], literales=["HKLM:\\Software\\X"])],
     'Set-ItemProperty -Path "HKLM:\\Software\\X" -Name a -Value 1\n', "critico", "registro"),
    ([comando("Get-ChildItem", params=["Path"], literales=["$env:USERPROFILE\\Downloads"])],
     'Get-ChildItem -Path "$env:USERPROFILE\\Downloads"\n', "medio", "fuera_de_carpeta"),
    ([comando("Set-Content", params=["Path", "Value"], literales=["salida.txt", "hola"])],
     'Set-Content -Path "salida.txt" -Value "hola"\n', "medio", "sobrescritura"),
    ([comando("Get-Process")], "Get-Process\n", "medio", "servicios"),
    ([comando("Read-Host", literales=["dime"])], '$x = Read-Host "dime"\n', "medio", "interactivo"),
    ([comando("git", literales=["status"])], "git status\n", "alto", "subproceso"),
    ([comando("Remove-Item", literales=["viejo.txt"])], "Remove-Item viejo.txt\n", "alto", "borrado"),
    ([comando("Invoke-WebRequest", literales=["https://x"], params=["OutFile"])],
     "Invoke-WebRequest https://x -OutFile x.zip\n", "alto", "red"),
    ([comando("Add-Type", params=["TypeDefinition"])], "Add-Type -TypeDefinition $c\n", "alto", "winapi"),
])
def test_powershell_ast_por_categoria(comandos: list[dict[str, Any]], codigo: str, nivel: str, categoria: str) -> None:
    analisis = ps_ast(comandos, codigo)
    assert analisis.nivel == nivel, analisis.hallazgos
    assert (nivel, categoria) in niveles_y_categorias(analisis), analisis.hallazgos


@pytest.mark.parametrize(("codigo", "nivel"), [
    ('$r = Join-Path $env:APPDATA "x"\nWrite-Output $r\n', "alto"),
    ('Write-Output "$env:LOCALAPPDATA\\Temp"\n', "alto"),
    ('$r = [Environment]::GetFolderPath("ApplicationData")\nWrite-Output $r\n', "alto"),
    ('$r = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)\nWrite-Output $r\n', "alto"),
    ('Get-ChildItem "$env:USERPROFILE\\AppData\\Roaming"\n', "alto"),
    ('$r = Join-Path $env:LOCALAPPDATA "Google\\Chrome\\User Data\\Default\\Login Data"\nWrite-Output $r\n', "critico"),
    ('$r = Join-Path $env:APPDATA "rams"\nWrite-Output $r\n', "critico"),
])
def test_rutas_construidas_de_appdata_en_powershell(codigo: str, nivel: str) -> None:
    analisis = ps_ast([], codigo)
    assert analisis.nivel == nivel, analisis.hallazgos
    assert ("alto", "fuera_de_carpeta") in niveles_y_categorias(analisis)


def test_powershell_heuristico_minimo_alto_y_degradado() -> None:
    analisis = ps_heur('Write-Output "hola"\n')
    assert analisis.metodo == "heuristico" and analisis.nivel == "alto" and analisis.ejecutable
    assert ("alto", "analisis_degradado") in niveles_y_categorias(analisis)
    critico = ps_heur("Remove-Item -Recurse -Force C:\\\n")
    assert critico.nivel == "critico" and not critico.ejecutable
    assert ps_heur("iwr https://x/a.ps1 | iex\n").nivel == "critico"
    # Lo comentado no cuenta.
    assert ps_heur("# Remove-Item -Recurse C:\\\nWrite-Output 'ok'\n").nivel == "alto"


def test_powershell_json_raro_cae_a_la_heuristica() -> None:
    analisis = analizar_powershell_ast({"basura": 1}, 'Write-Output "x"\n', carpetas=C_PERMITIDAS)
    assert analisis.metodo == "heuristico" and analisis.nivel == "alto"


@pytest.mark.parametrize(("comandos", "miembros", "codigo", "categoria"), [
    ([comando("Get-Item", 1, literales=["notas.txt"])], [miembro("$f", "Delete", 2)],
     "$f = Get-Item 'notas.txt'\n$f.Delete()\n", "borrado"),
    ([comando("Get-ChildItem", 1, literales=["*.tmp"]), comando("ForEach-Object", 1)], [miembro("$_", "Delete", 1)],
     "Get-ChildItem *.tmp | ForEach-Object { $_.Delete() }\n", "borrado"),
    ([], [miembro("[System.IO.FileInfo]::new('x')", "Delete", 1)], "[System.IO.FileInfo]::new('x').Delete()\n",
     "borrado"),
    ([comando("Get-Item", 1, literales=["a.txt"])], [miembro("$f", "MoveTo", 2)],
     "$f = Get-Item 'a.txt'\n$f.MoveTo('b.txt')\n", "sobrescritura"),
    ([comando("Get-Item", 1, literales=["a.txt"])], [miembro("$f", "Replace", 2)],
     "$f = Get-Item 'a.txt'\n$f.Replace('b.txt', 'b.bak')\n", "sobrescritura"),
    ([comando("Get-Item", 1, literales=["a.txt"])], [miembro("$f", "SetAccessControl", 2)],
     "$f = Get-Item 'a.txt'\n$f.SetAccessControl($acl)\n", "sobrescritura"),
    ([comando("Get-Item", 1, literales=["a.txt"])], [], "$f = Get-Item 'a.txt'\n$f.IsReadOnly = $false\n",
     "sobrescritura"),
    ([comando("Get-ChildItem", 1, literales=["*.txt"])], [miembro("$f", "Encrypt", 1)],
     "foreach ($f in Get-ChildItem *.txt) { $f.Encrypt() }\n", "sobrescritura"),
])
def test_powershell_metodos_net_de_archivos(comandos: list[dict[str, Any]], miembros: list[dict[str, Any]],
                                            codigo: str, categoria: str) -> None:
    """Borrar o mover con métodos de FileInfo/DirectoryInfo es tan alto como Remove-Item/Move-Item."""
    analisis = ps_ast(comandos, codigo, miembros=miembros)
    assert analisis.nivel == "alto", analisis.hallazgos
    assert ("alto", categoria) in niveles_y_categorias(analisis), analisis.hallazgos
    heuristico = ps_heur(codigo)
    assert ("alto", categoria) in niveles_y_categorias(heuristico), heuristico.hallazgos


def test_powershell_metodos_que_no_son_de_archivos_siguen_en_bajo() -> None:
    assert ps_ast([], "$s = 'abc'\n$s.Replace('a', 'b')\n", miembros=[miembro("$s", "Replace", 2)]).nivel == "bajo"
    assert ps_ast([], "$a = 1, 2\n$b = New-Object int[] 2\n$a.CopyTo($b, 0)\n",
                  miembros=[miembro("$a", "CopyTo", 3)]).nivel == "bajo"
    # Una propiedad del archivo (el nombre) es texto: su Replace no mueve nada.
    analisis = ps_ast([comando("Get-ChildItem", 1, literales=["*.jpg"]), comando("Rename-Item", 1)],
                      "Get-ChildItem *.jpg | ForEach-Object { Rename-Item $_ $_.Name.Replace(' ', '_') }\n",
                      miembros=[miembro("$_.Name", "Replace", 1)])
    assert analisis.nivel == "medio", analisis.hallazgos


# ---------------------------------------------------------------------------
# Cambio de la carpeta actual (§9.4): tras él, una ruta relativa ya no es la carpeta de trabajo
# ---------------------------------------------------------------------------


def test_cambio_de_carpeta_en_python() -> None:
    assert py('open("notas.txt", "w").write("x")\n').nivel == "medio"
    analisis = py('import os\nfrom pathlib import Path\nos.chdir(Path.home() / "Documents")\n'
                  'open("notas.txt", "w").write("x")\n')
    assert analisis.nivel == "alto" and ("alto", "sobrescritura") in niveles_y_categorias(analisis)
    lectura = py('import os\nfrom pathlib import Path\nos.chdir(Path.home() / "Downloads")\nprint(os.listdir("."))\n')
    assert lectura.nivel == "medio" and ("medio", "fuera_de_carpeta") in niveles_y_categorias(lectura)
    assert py('import os, sys\nos.chdir(sys.argv[1])\nopen("x.txt", "w").write("a")\n').nivel == "alto"
    # La carpeta del propio script, una subcarpeta o la temporal siguen siendo la de trabajo.
    for codigo in ('import os\nos.chdir(os.path.dirname(os.path.abspath(__file__)))\nopen("x.txt", "w").write("a")\n',
                   'import os\nfrom pathlib import Path\nos.chdir(Path(__file__).resolve().parent)\n'
                   'open("x.txt", "w").write("a")\n',
                   'import os\nos.makedirs("sub", exist_ok=True)\nos.chdir("sub")\nopen("x.txt", "w").write("a")\n',
                   'import os, tempfile\nos.chdir(tempfile.gettempdir())\nopen("x.txt", "w").write("a")\n'):
        assert py(codigo).nivel == "medio", (codigo, py(codigo).hallazgos)


def test_cambio_de_carpeta_en_bash() -> None:
    assert sh("echo x > notas.txt\n").nivel == "medio"
    for codigo in ("cd ~/Documents\necho x > notas.txt\n", "cd\nmv a.txt b.txt\n", 'cd "$DESTINO"\ntouch -c x\necho x > y\n',
                   "pushd ~/Downloads\ncp a.txt b.txt\n"):
        analisis = sh(codigo)
        assert analisis.nivel == "alto" and ("alto", "sobrescritura") in niveles_y_categorias(analisis), codigo
    assert sh("mkdir -p sub\ncd sub\necho x > notas.txt\n").nivel == "medio"


def test_cambio_de_carpeta_en_powershell() -> None:
    escribir = comando("Set-Content", 2, params=["Path", "Value"], literales=["notas.txt", "x"])
    texto = "Set-Content -Path 'notas.txt' -Value 'x'\n"
    assert ps_ast([escribir], "Write-Output 1\n" + texto).nivel == "medio"
    for destino, codigo_cd in ((["~\\Documents"], "Set-Location ~\\Documents\n"), ([], "Set-Location $destino\n"),
                               (["~\\Downloads"], "Push-Location ~\\Downloads\n")):
        analisis = ps_ast([comando("Set-Location" if "Set" in codigo_cd else "Push-Location", 1, literales=destino),
                           escribir], codigo_cd + texto)
        assert analisis.nivel == "alto" and ("alto", "sobrescritura") in niveles_y_categorias(analisis), codigo_cd
    alias = ps_ast([comando("cd", 1, literales=["~\\Documents"]), escribir], "cd ~\\Documents\n" + texto)
    assert alias.nivel == "alto"
    net = ps_ast([escribir], "[Environment]::CurrentDirectory = $destino\n" + texto)
    assert net.nivel == "alto"
    lectura = ps_ast([comando("Set-Location", 1), comando("Get-ChildItem", 2)], "Set-Location $destino\nGet-ChildItem\n")
    assert lectura.nivel == "medio" and ("medio", "fuera_de_carpeta") in niveles_y_categorias(lectura)
    for literales, codigo_cd in ((["sub"], "Set-Location sub\n"), ([], "Set-Location $PSScriptRoot\n")):
        analisis = ps_ast([comando("Set-Location", 1, literales=literales), escribir], codigo_cd + texto)
        assert analisis.nivel == "medio", (codigo_cd, analisis.hallazgos)


# ---------------------------------------------------------------------------
# proponer (§9.3)
# ---------------------------------------------------------------------------


def test_proponer_valido(settings_taller: Settings, correr, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams.taller")
    generador = GeneradorFalso(respuesta('print("hola mundo secreto_en_el_codigo")\n'))
    reloj = Reloj()
    taller = crear_taller(settings_taller, generador=generador, reloj=reloj)
    tarea = "  renombra\x07 las fotos\nde Descargas  " + "x" * 600
    propuesta = correr(taller.proponer(tarea, canal_origen="escritorio", ttl_s=1800.0))
    assert isinstance(propuesta, PropuestaScript)
    texto, kw = generador.llamadas[0]
    assert "\x07" not in texto and "\n" not in texto and len(texto) <= 500
    assert texto.startswith("renombra las fotos de Descargas")
    assert kw == {"plataforma": "windows", "lenguajes": ("powershell", "python")}
    assert re.fullmatch(r"[0-9a-f]{8}", propuesta.id) and propuesta.version == 1
    assert propuesta.plataforma == "windows" and propuesta.lenguaje == "python"
    assert propuesta.canal_origen == "escritorio" and propuesta.caduca == reloj.ahora + 1800.0
    assert len(propuesta.tarea) <= 300
    assert propuesta.huella() == huella_contenido({"clase": "script", "lenguaje": "python",
                                                   "codigo": propuesta.codigo})
    assert propuesta.analisis.nivel == "bajo" and propuesta.analisis.ejecutable
    assert taller.propuesta(propuesta.id) is propuesta
    registro = caplog.text
    assert "secreto_en_el_codigo" not in registro and "renombra" not in registro
    assert propuesta.huella()[:12] in registro


def test_proponer_reemplaza_la_anterior(settings_taller: Settings, correr) -> None:
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')))
    primera = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    segunda = correr(taller.proponer("b", canal_origen="terminal", ttl_s=60))
    assert isinstance(primera, PropuestaScript) and isinstance(segunda, PropuestaScript)
    assert taller.propuesta(primera.id) is None and taller.propuesta(segunda.id) is segunda


@pytest.mark.parametrize(("datos", "codigo"), [
    (None, "sin_modelo"),
    ("no es un dict", "respuesta_invalida"),
    ({"lenguaje": "python"}, "respuesta_invalida"),
    (respuesta(""), "respuesta_invalida"),
    (respuesta("   \n"), "respuesta_invalida"),
    (respuesta(12), "respuesta_invalida"),
    (respuesta('print("a")\n', lenguaje="cobol"), "respuesta_invalida"),
    (respuesta('echo a\n', lenguaje="bash"), "lenguaje_no_disponible"),
    (respuesta('print("\x07")\n'), "respuesta_invalida"),
    (respuesta('print("a")\x00\n'), "respuesta_invalida"),
    (respuesta('print("\ud800")\n'), "respuesta_invalida"),
    (respuesta('print("a")  # \u202e oculto\n'), "respuesta_invalida"),
    (respuesta('x = """```"""\n'), "respuesta_invalida"),
    (respuesta('print("a")\n', efectos="no es lista"), "respuesta_invalida"),
    (respuesta('print("a")\n', requisitos=[1, 2]), "respuesta_invalida"),
    (respuesta("x = 1\n" + "#" * (MAX_CODIGO_SCRIPT - 6 + 1)), "demasiado_grande"),
])
def test_proponer_rechazos(settings_taller: Settings, correr, datos: Any, codigo: str) -> None:
    generador = GeneradorFalso(datos)
    taller = crear_taller(settings_taller, generador=generador)
    resultado = correr(taller.proponer("haz algo", canal_origen="terminal", ttl_s=60))
    assert isinstance(resultado, FalloTaller) and resultado.codigo == codigo, resultado
    assert len(generador.llamadas) == 1          # sin reintentos


def test_proponer_tamano_exacto_y_nunca_recorta(settings_taller: Settings, correr) -> None:
    codigo = "x = 1\n" + "#" * (MAX_CODIGO_SCRIPT - 6 - 1) + "\n"
    assert len(codigo.encode("utf-8")) == MAX_CODIGO_SCRIPT
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta(codigo)))
    propuesta = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(propuesta, PropuestaScript) and propuesta.codigo == codigo
    grande = "x = 1\n" + "ñ" * MAX_CODIGO_SCRIPT     # 2 bytes por carácter: pasa del tope en bytes
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta(grande)))
    fallo = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(fallo, FalloTaller) and fallo.codigo == "demasiado_grande"


def test_proponer_normaliza(settings_taller: Settings, correr) -> None:
    crudo = "```python\r\n\ufeffprint('cafe\u0301')\r\n\tx = 1\r\n```"
    datos = respuesta(crudo, explicacion="E" * 600, efectos=[f"efecto {i}\x07" for i in range(10)] + ["  ", ""],
                      requisitos=["r" * 80, "  python   3  "])
    taller = crear_taller(settings_taller, generador=GeneradorFalso(datos))
    propuesta = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(propuesta, PropuestaScript)
    assert propuesta.codigo == "print('café')\n\tx = 1"
    assert "\r" not in propuesta.codigo and not propuesta.codigo.startswith("\ufeff")
    assert propuesta.explicacion == "E" * 600                      # en el tope: entera, sin «…»
    assert len(propuesta.efectos) == 10 and all("\x07" not in e for e in propuesta.efectos)
    assert propuesta.requisitos == ("r" * 80, "python 3")


@pytest.mark.parametrize("extra", [
    {"explicacion": "E" * 601},
    {"explicacion": "😀" * 301},                                    # 602 unidades UTF-16
    {"efectos": [f"efecto {i}" for i in range(11)]},
    {"efectos": ["e" * 161]},
    {"requisitos": [f"req {i}" for i in range(11)]},
    {"requisitos": ["r" * 81]},
])
def test_proponer_textos_informativos_largos_son_respuesta_invalida(settings_taller: Settings, correr,
                                                                    extra: dict[str, Any]) -> None:
    """§9.3 paso 4: pasarse de los topes invalida la respuesta (nunca se recorta en silencio)."""
    generador = GeneradorFalso(respuesta('print("a")\n', **extra))
    taller = crear_taller(settings_taller, generador=generador)
    resultado = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(resultado, FalloTaller) and resultado.codigo == "respuesta_invalida", resultado
    assert len(generador.llamadas) == 1


def test_proponer_sin_generador_deshabilitado_y_telegram(settings_prueba: Settings, settings_taller: Settings,
                                                          correr) -> None:
    sin_generador = crear_taller(settings_taller, generador=None)
    fallo = correr(sin_generador.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(fallo, FalloTaller) and fallo.codigo == "sin_modelo"
    apagado = crear_taller(settings_prueba, generador=GeneradorFalso(respuesta('print(1)\n')))
    fallo = correr(apagado.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(fallo, FalloTaller) and fallo.codigo == "deshabilitado"
    generador = GeneradorFalso(respuesta('print(1)\n'))
    telegram = crear_taller(settings_taller, generador=generador)
    fallo = correr(telegram.proponer("a", canal_origen="telegram", ttl_s=60))
    assert isinstance(fallo, FalloTaller) and fallo.codigo == "deshabilitado" and not generador.llamadas
    vacio = crear_taller(settings_taller, generador=generador)
    fallo = correr(vacio.proponer(" \x07 ", canal_origen="terminal", ttl_s=60))
    assert isinstance(fallo, FalloTaller) and fallo.codigo == "vacio"


def test_proponer_powershell_usa_el_analizador_inyectado_con_bom(settings_taller: Settings, correr) -> None:
    vistos: list[bytes] = []

    async def analizador(ruta: Path) -> Mapping[str, Any]:
        assert ruta.parent == Path(settings_taller.taller_dir) / "analisis" and ruta.suffix == ".ps1"
        vistos.append(ruta.read_bytes())
        return {**JSON_VACIO, "comandos": [comando("Write-Output", literales=["ñandú"])]}

    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('Write-Output "ñandú"\n', "powershell")),
                          analizador_ps=analizador)
    propuesta = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(propuesta, PropuestaScript) and propuesta.analisis.metodo == "ast"
    assert propuesta.analisis.nivel == "bajo"
    assert vistos == [b"\xef\xbb\xbf" + 'Write-Output "ñandú"\n'.encode("utf-8")]
    assert not list((Path(settings_taller.taller_dir) / "analisis").glob("*.ps1"))   # se borra al terminar


@pytest.mark.parametrize("falla", ["none", "excepcion"])
def test_proponer_powershell_sin_analizador_es_heuristico(settings_taller: Settings, correr, falla: str) -> None:
    async def analizador(ruta: Path) -> Any:
        if falla == "excepcion":
            raise OSError("caído")
        return None

    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('Write-Output "a"\n', "powershell")),
                          analizador_ps=analizador)
    propuesta = correr(taller.proponer("a", canal_origen="terminal", ttl_s=60))
    assert isinstance(propuesta, PropuestaScript)
    assert propuesta.analisis.metodo == "heuristico" and propuesta.analisis.nivel == "alto"
    assert any(h.categoria == "analisis_degradado" for h in propuesta.analisis.hallazgos)


def test_carpetas_inyectadas_y_descargas_de_drive_son_permitidas(settings_taller: Settings, carpetas: dict[str, Path],
                                                                 tmp_path: Path, correr) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    settings = settings_taller.con(drive_descargas_dir=drive)
    taller = crear_taller(settings, carpetas=carpetas)
    for permitida in (carpetas["descargas"] / "x.txt", carpetas["documentos"] / "y.txt", drive / "z.pdf"):
        analisis = correr(taller.analizar("python", f"print(open(r{str(permitida)!r}).read())\n"))
        assert analisis.nivel == "medio", (permitida, analisis.hallazgos)
    fuera = tmp_path / "otra" / "x.txt"
    analisis = correr(taller.analizar("python", f"print(open(r{str(fuera)!r}).read())\n"))
    assert ("alto", "fuera_de_carpeta") in niveles_y_categorias(analisis)
    proyecto = Path(settings.data_dir) / "memoria.db"
    analisis = correr(taller.analizar("python", f"print(open(r{str(proyecto)!r}).read())\n"))
    assert analisis.nivel == "critico"
    desconocido = correr(taller.analizar("cobol", "DISPLAY 'x'."))
    assert not desconocido.ejecutable


# ---------------------------------------------------------------------------
# vista() (§12.2)
# ---------------------------------------------------------------------------


def test_vista_con_30_hallazgos(settings_taller: Settings) -> None:
    niveles = ["bajo", "medio", "alto", "critico"]
    hallazgos = tuple(Hallazgo(niveles[i % 4], "subproceso", 30 - i, f"hallazgo {i}") for i in range(30))
    analisis = Analisis("critico", hallazgos, "ast", True)
    codigo = "\n".join(f"print({i})" for i in range(3000)) + "\n"
    propuesta = PropuestaScript(id="ab12cd34", version=1, tarea="t", lenguaje="python", plataforma="windows",
                                codigo=codigo, explicacion="e", efectos=("a",), requisitos=(), analisis=analisis,
                                habilidad=None, habilidad_cambiada=False, ejecuciones=0, canal_origen="escritorio",
                                creado=0.0, caduca=10.0)
    vista = propuesta.vista()
    assert set(vista) == CLAVES_VISTA
    assert vista["codigo"] == codigo                        # el código ENTERO
    assert len(vista["hallazgos"]) == MAX_HALLAZGOS_TARJETA == 20
    assert vista["hallazgos_omitidos"] == 10
    orden = [(-NIVELES_RIESGO.index(h["nivel"]), h["linea"]) for h in vista["hallazgos"]]
    assert orden == sorted(orden)
    assert [h["nivel"] for h in vista["hallazgos"][:8]] == ["critico"] * 7 + ["alto"]
    assert vista["nivel"] == "critico" and vista["ejecutable"] is False
    assert vista["motivo_no_ejecutable"] and len(vista["motivo_no_ejecutable"]) <= 200
    assert json.loads(json.dumps(vista)) == vista


# ---------------------------------------------------------------------------
# ejecutar con lanzador falso (§9.6)
# ---------------------------------------------------------------------------


def proponer_y_obtener(taller: TallerScripts, correr, *, ttl: float = 1800.0) -> PropuestaScript:
    propuesta = correr(taller.proponer("tarea", canal_origen="escritorio", ttl_s=ttl))
    assert isinstance(propuesta, PropuestaScript), propuesta
    return propuesta


def test_ejecutar_python_entorno_argv_y_archivo(settings_taller: Settings, correr,
                                                monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rams.taller")
    for variable in ("GEMINI_API_KEY", "APPDATA", "LOCALAPPDATA", "PYTHONUTF8", "PYTHONIOENCODING",
                     "DBUS_SESSION_BUS_ADDRESS", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(variable, "valor-que-no-debe-pasar")
    lanzador = LanzadorFalso(stdout=b"salida privada del script\n")
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("hola")\n')), lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert isinstance(resultado, ResultadoScript)
    assert resultado.ok and resultado.codigo == "ejecutado" and resultado.codigo_salida == 0
    llamada = lanzador.llamadas[0]
    carpeta = Path(settings_taller.taller_dir) / "ejecuciones" / f"{propuesta.id}-1"
    assert llamada["cwd"] == carpeta
    assert llamada["argv"] == [sys.executable, "-I", "-X", "utf8", "-B", "script.py"]
    assert llamada["contenido"] == b'print("hola")\n'              # .py sin BOM, exactamente lo aprobado
    entorno = llamada["env"]
    for prohibida in ("GEMINI_API_KEY", "APPDATA", "LOCALAPPDATA", "PYTHONUTF8", "PYTHONIOENCODING",
                      "DBUS_SESSION_BUS_ADDRESS", "TELEGRAM_BOT_TOKEN"):
        assert prohibida not in entorno
    assert entorno["TEMP"] == entorno["TMP"] == entorno["TMPDIR"] == str(carpeta / "tmp")
    assert (carpeta / "tmp").is_dir() and Path(entorno["TEMP"]).parent == carpeta
    assert set(entorno) <= set(ts.ENTORNO_WINDOWS) | {"TEMP", "TMP", "TMPDIR"}
    assert llamada["timeout_s"] == 30.0 and llamada["max_salida"] == 16384
    assert llamada["max_procesos"] == 16 and llamada["max_memoria_mb"] == 512 and llamada["unidad"] is None
    assert resultado.carpeta.endswith(f"ejecuciones/{propuesta.id}-1") and not Path(resultado.carpeta).is_absolute()
    assert (carpeta / "salida.txt").read_text(encoding="utf-8") == "salida privada del script\n"
    registro = caplog.text
    assert "salida privada" not in registro and 'print("hola")' not in registro
    assert "ejecutado" in registro and propuesta.huella()[:12] in registro


def test_ejecutar_powershell_con_bom_y_argv(settings_taller: Settings, correr) -> None:
    lanzador = LanzadorFalso()
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('Write-Output "ñ"\n', "powershell")),
                          lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == "ejecutado"
    llamada = lanzador.llamadas[0]
    assert llamada["argv"] == [POWERSHELL_FALSO, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                               "Bypass", "-File", "script.ps1"]
    assert llamada["contenido"] == b"\xef\xbb\xbf" + 'Write-Output "ñ"\n'.encode("utf-8")


def test_ejecutar_salida_recortada_redactada_y_limpia(settings_con_claves: Settings, correr) -> None:
    settings = settings_con_claves.con(taller_habilitado=True, taller_max_salida=1024)
    clave = "test-gemini-key-000000"
    stdout = (b"\x1b[31mrojo\x1b[0m clave " + clave.encode() + b" token abcdefghijklmnopqrstuvwxyz0123456 fin\x07\n"
              + b"linea con espacios\n" * 200)
    lanzador = LanzadorFalso(stdout=stdout, stderr="error en «utf-8»\n".encode("utf-8"), recortada=True)
    taller = crear_taller(settings, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.recortada and len(resultado.stdout.encode("utf-8")) <= 1024 + 64
    assert "\x1b" not in resultado.stdout and "\x07" not in resultado.stdout
    assert clave not in resultado.stdout and "«secreto»" in resultado.stdout
    assert "abcdefghijklmnopqrstuvwxyz0123456" not in resultado.stdout and "«oculto»" in resultado.stdout
    assert resultado.stdout.startswith("rojo clave")
    assert resultado.stderr == "error en «utf-8»\n"
    assert resultado.bytes_salida == len(stdout) + len("error en «utf-8»\n".encode("utf-8"))
    carpeta = Path(settings.taller_dir) / "ejecuciones" / f"{propuesta.id}-1"
    guardada = (carpeta / "salida.txt").read_text(encoding="utf-8")
    assert guardada == resultado.stdout and clave not in guardada
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(carpeta / "salida.txt").st_mode) == 0o600
        assert stat.S_IMODE(os.stat(carpeta).st_mode) == 0o700


@pytest.mark.parametrize(("lanzador", "codigo"), [
    (LanzadorFalso(codigo_salida=3), "error_salida"),
    (LanzadorFalso(codigo_salida=None, tiempo_agotado=True), "tiempo_agotado"),
])
def test_ejecutar_codigos(settings_taller: Settings, correr, lanzador: LanzadorFalso, codigo: str) -> None:
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == codigo and not resultado.ok


def test_ejecutar_politica_de_powershell(settings_taller: Settings, correr) -> None:
    lanzador = LanzadorFalso(codigo_salida=1, stdout=b"", stderr=b"... PSSecurityException ... ExecutionPolicy\n")
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('Write-Output "a"\n', "powershell")),
                          lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == "politica" and "Python" in resultado.mensaje


@pytest.mark.parametrize("elevado", [True, None])
def test_ejecutar_elevado_no_ejecuta(settings_taller: Settings, correr, elevado: bool | None) -> None:
    lanzador = LanzadorFalso()
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador,
                          elevado=elevado)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == "elevado" and not lanzador.llamadas
    assert taller.diagnostico().estado.value == "DEGRADADO"


def test_ejecutar_comprobaciones(settings_taller: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    reloj = Reloj()
    lanzador = LanzadorFalso()
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador,
                          reloj=reloj)
    propuesta = proponer_y_obtener(taller, correr, ttl=60.0)
    assert correr(taller.ejecutar(propuesta.id, "0" * 64)).codigo == "huella"
    assert correr(taller.ejecutar("ffffffff", propuesta.huella())).codigo == "no_existe"
    assert correr(taller.ejecutar("ñññññññ", propuesta.huella())).codigo == "no_existe"
    # Anti-TOCTOU: si el archivo escrito no es lo aprobado, no se lanza.
    original = TallerScripts.__dict__["_releer"]
    monkeypatch.setattr(TallerScripts, "_releer", staticmethod(lambda archivo, lenguaje: 'print("otro")\n'))
    assert correr(taller.ejecutar(propuesta.id, propuesta.huella())).codigo == "huella"
    monkeypatch.setattr(TallerScripts, "_releer", original)
    assert not lanzador.llamadas
    reloj.ahora += 61
    assert correr(taller.ejecutar(propuesta.id, propuesta.huella())).codigo == "caducado"
    assert not lanzador.llamadas


def test_ejecutar_bloqueado_si_es_critico(settings_taller: Settings, correr) -> None:
    lanzador = LanzadorFalso()
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('import shutil\nshutil.rmtree("/")\n')),
                          lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    assert not propuesta.analisis.ejecutable
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == "bloqueado" and not lanzador.llamadas


def test_ejecutar_ocupado_y_reabrir(settings_taller: Settings, correr) -> None:
    async def escenario() -> tuple[ResultadoScript, ResultadoScript, TallerScripts, PropuestaScript, LanzadorFalso]:
        espera = asyncio.Event()
        lanzador = LanzadorFalso(espera=espera)
        taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador)
        propuesta = await taller.proponer("t", canal_origen="escritorio", ttl_s=60)
        assert isinstance(propuesta, PropuestaScript)
        primera = asyncio.ensure_future(taller.ejecutar(propuesta.id, propuesta.huella()))
        for _ in range(200):
            if lanzador.llamadas:
                break
            await asyncio.sleep(0.01)
        assert taller.ocupado
        segunda = await taller.ejecutar(propuesta.id, propuesta.huella())
        espera.set()
        return await primera, segunda, taller, propuesta, lanzador

    primera, segunda, taller, propuesta, lanzador = correr(escenario())
    assert segunda.codigo == "ocupado" and primera.codigo == "ejecutado" and len(lanzador.llamadas) == 1
    assert not taller.ocupado
    actual = taller.propuesta(propuesta.id)
    assert actual is not None and actual.ejecuciones == 1 and actual.huella() == propuesta.huella()
    reabierta = taller.reabrir(propuesta.id)
    assert reabierta is not None and reabierta.ejecuciones == 1 and reabierta.huella() == propuesta.huella()
    # «Ejecutar otra vez»: otra carpeta (<id>-2).
    lanzador.espera = None
    otra = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert otra.codigo == "ejecutado" and lanzador.llamadas[-1]["cwd"].name == f"{propuesta.id}-2"
    assert taller.descartar(propuesta.id) and taller.propuesta(propuesta.id) is None
    assert not taller.descartar(propuesta.id)


def test_ejecutar_sin_confinamiento(settings_taller: Settings, correr) -> None:
    lanzador = LanzadorFalso(error=ts.ErrorConfinamiento("AssignProcessToJobObject"))
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == "confinamiento" and not taller.ocupado


def test_ejecutar_sin_interprete(settings_taller: Settings, correr) -> None:
    lanzador = LanzadorFalso(error=FileNotFoundError("powershell.exe"))
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('Write-Output "a"\n', "powershell")),
                          lanzador=lanzador)
    propuesta = proponer_y_obtener(taller, correr)
    assert correr(taller.ejecutar(propuesta.id, propuesta.huella())).codigo == "sin_interprete"


def test_ejecutar_purga_y_no_sigue_enlaces(settings_taller: Settings, correr, tmp_path: Path) -> None:
    raiz = Path(settings_taller.taller_dir) / "ejecuciones"
    raiz.mkdir(parents=True)
    antiguo = time.time() - 10_000
    for i in range(25):
        vieja = raiz / f"viejo{i:02d}"
        (vieja / "tmp").mkdir(parents=True)
        (vieja / "salida.txt").write_text("x", encoding="utf-8")
        os.utime(vieja, (antiguo + i, antiguo + i))
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    (fuera / "importante.txt").write_text("no borrar", encoding="utf-8")
    enlace = raiz / "viejo00" / "enlace"
    try:
        os.symlink(fuera, enlace, target_is_directory=True)
        os.utime(raiz / "viejo00", (antiguo - 1, antiguo - 1))
        hay_enlace = True
    except (OSError, NotImplementedError):
        hay_enlace = False
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')))
    propuesta = proponer_y_obtener(taller, correr)
    assert correr(taller.ejecutar(propuesta.id, propuesta.huella())).codigo == "ejecutado"
    restantes = sorted(p.name for p in raiz.iterdir())
    assert len(restantes) == 20 and f"{propuesta.id}-1" in restantes
    assert "viejo00" not in restantes
    assert (fuera / "importante.txt").read_text(encoding="utf-8") == "no borrar"
    if hay_enlace:
        assert fuera.is_dir()


# ---------------------------------------------------------------------------
# Linux: modos de límites con una sonda falsa
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("modo", ["cgroups", "prlimit", "ninguno"])
def test_linux_prefijo_de_limites(settings_taller: Settings, correr, modo: str,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    lanzador = LanzadorFalso()
    sonda_falsa = sonda(modo)
    taller = crear_taller(settings_taller, plataforma="linux", generador=GeneradorFalso(respuesta("echo hola\n", "bash")),
                          lanzador=lanzador, sonda_limites=sonda_falsa)
    assert correr(taller.iniciar()).estado.value in ("OK", "DEGRADADO")
    correr(taller.iniciar())
    assert len(sonda_falsa.llamadas) == 1                  # la sonda corre una sola vez
    propuesta = proponer_y_obtener(taller, correr)
    assert propuesta.plataforma == "linux" and propuesta.lenguaje == "bash"
    resultado = correr(taller.ejecutar(propuesta.id, propuesta.huella()))
    assert resultado.codigo == "ejecutado"
    llamada = lanzador.llamadas[0]
    argv = llamada["argv"]
    interprete = [BASH_FALSO, "--noprofile", "--norc", "script.sh"]
    assert argv[-4:] == interprete
    assert llamada["contenido"] == b"echo hola\n"
    assert "DBUS_SESSION_BUS_ADDRESS" not in llamada["env"]
    assert set(llamada["env"]) <= set(ts.ENTORNO_LINUX) | {"TEMP", "TMP", "TMPDIR"}
    assert not any("nproc" in a for a in argv)
    unidad = f"rams-taller-{propuesta.id}-1"
    if modo == "cgroups":
        assert Path(argv[0]).name == "systemd-run"
        assert argv[1:-4] == ["--user", "--scope", "--quiet", "--collect", f"--unit={unidad}", "-p", "TasksMax=16",
                              "-p", "MemoryMax=512M", "--"]
        assert llamada["unidad"] == unidad
        assert "cgroups" in taller.diagnostico().detalle
    elif modo == "prlimit":
        assert Path(argv[0]).name == "prlimit" and argv[1:3] == [f"--as={512 * 1024 * 1024}", "--"]
        assert llamada["unidad"] is None
        assert "prlimit" in taller.diagnostico().detalle
    else:
        assert argv == interprete and llamada["unidad"] is None
        assert "solo tiempo límite" in taller.diagnostico().detalle


def test_entorno_saneado_lista_blanca(tmp_path: Path) -> None:
    origen = {"Path": "C:\\x", "SystemRoot": "C:\\Windows", "APPDATA": "a", "LOCALAPPDATA": "b",
              "GEMINI_API_KEY": "k", "PYTHONUTF8": "1", "USERPROFILE": "C:\\Users\\ana"}
    windows = ts.entorno_saneado("win32", tmp_path / "tmp", origen)
    assert windows == {"PATH": "C:\\x", "SYSTEMROOT": "C:\\Windows", "USERPROFILE": "C:\\Users\\ana",
                       "TEMP": str(tmp_path / "tmp"), "TMP": str(tmp_path / "tmp"), "TMPDIR": str(tmp_path / "tmp")}
    linux = ts.entorno_saneado("linux", tmp_path / "tmp", {"PATH": "/usr/bin", "HOME": "/home/ana",
                                                           "DBUS_SESSION_BUS_ADDRESS": "x", "XDG_RUNTIME_DIR": "/run/x",
                                                           "SSH_AUTH_SOCK": "s"})
    assert set(linux) == {"PATH", "HOME", "XDG_RUNTIME_DIR", "TEMP", "TMP", "TMPDIR"}


def test_limpiar_salida() -> None:
    texto = ts.limpiar_salida(b"\x1b[1;32mok\x1b[0m\r\nlinea\x00dos\tfin\n", "linux", ())
    assert texto == "ok\nlineados\tfin\n"
    # UTF-8 partido al final por el recorte: no se cae a otra codificación.
    assert ts.limpiar_salida("añ".encode("utf-8")[:-1], "linux", ()) == "a"
    assert ts.limpiar_salida(b"\xff\xfe hola", "linux", ()).endswith("hola")


# ---------------------------------------------------------------------------
# Integración inocua con el lanzador real (sys.executable)
# ---------------------------------------------------------------------------

soportado = pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")),
                               reason="el taller solo existe en Windows y Linux")


def taller_real(settings: Settings, codigo: str) -> tuple[TallerScripts, PropuestaScript]:
    taller = TallerScripts(settings, generador=GeneradorFalso(respuesta(codigo)), plataforma=sys.platform,
                           analizador_ps=analizador_vacio, carpetas={}, sonda_limites=sonda("ninguno"),
                           es_elevado=lambda: False)
    propuesta = asyncio.run(taller.proponer("inocuo", canal_origen="escritorio", ttl_s=600))
    assert isinstance(propuesta, PropuestaScript), propuesta
    assert propuesta.analisis.ejecutable, propuesta.analisis
    return taller, propuesta


def _vivo(pid: int) -> bool:
    psutil = pytest.importorskip("psutil")
    try:
        proceso = psutil.Process(pid)
        return proceso.is_running() and proceso.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@soportado
def test_integracion_print(settings_taller: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "clave-falsa-de-prueba")
    monkeypatch.setenv("APPDATA", "C:\\no\\debe\\llegar")
    codigo = ('import os\nprint("hola desde el taller")\n'
              'print("APPDATA" in os.environ, "GEMINI_API_KEY" in os.environ, os.environ.get("TEMP", "")[-3:])\n')
    taller, propuesta = taller_real(settings_taller, codigo)
    resultado = asyncio.run(taller.ejecutar(propuesta.id, propuesta.huella()))
    asyncio.run(taller.cerrar())
    assert resultado.codigo == "ejecutado", (resultado.mensaje, resultado.stderr)
    assert resultado.ok and resultado.codigo_salida == 0 and not resultado.tiempo_agotado
    lineas = resultado.stdout.splitlines()
    assert lineas[0] == "hola desde el taller"
    assert lineas[1] == "False False tmp"


@soportado
def test_integracion_tiempo_agotado_mata_hijo_y_nieto(settings_taller: Settings) -> None:
    settings = settings_taller.con(taller_timeout_s=4.0)
    codigo = (
        "import subprocess, sys, time\n"
        "hijo = (\n"
        "    'import subprocess, sys, time\\n'\n"
        "    'n = subprocess.Popen([sys.executable, \"-c\", \"import time; time.sleep(60)\"])\\n'\n"
        "    'print(\"nieto\", n.pid, flush=True)\\n'\n"
        "    'time.sleep(60)\\n'\n"
        ")\n"
        "h = subprocess.Popen([sys.executable, '-c', hijo])\n"
        "print('hijo', h.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    taller, propuesta = taller_real(settings, codigo)
    inicio = time.monotonic()
    resultado = asyncio.run(taller.ejecutar(propuesta.id, propuesta.huella()))
    duracion = time.monotonic() - inicio
    asyncio.run(taller.cerrar())
    assert resultado.codigo == "tiempo_agotado" and resultado.tiempo_agotado, (resultado.stdout, resultado.stderr)
    assert duracion <= 4.0 + 3.0
    pids = {nombre: int(pid) for nombre, pid in re.findall(r"(hijo|nieto) (\d+)", resultado.stdout)}
    assert set(pids) == {"hijo", "nieto"}, resultado.stdout
    limite = time.monotonic() + 3.0
    while time.monotonic() < limite and any(_vivo(pid) for pid in pids.values()):
        time.sleep(0.1)
    assert not any(_vivo(pid) for pid in pids.values())


def _codigo_con_hijo(heredada: bool) -> str:
    """Script inocuo: lanza un hijo que solo duerme (y anota su pid real), y TERMINA enseguida."""
    hijo = ("import os, time\nopen('hijo.tmp', 'w').write(str(os.getpid()))\nos.replace('hijo.tmp', 'hijo.pid')\n"
            "time.sleep(60)\n")
    redireccion = "" if heredada else ", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    return (
        "import os, subprocess, sys, time\n"
        f"h = subprocess.Popen([sys.executable, '-c', {hijo!r}]{redireccion})\n"
        "limite = time.monotonic() + 15\n"
        "while not os.path.exists('hijo.pid') and time.monotonic() < limite:\n"
        "    time.sleep(0.05)\n"
        "print('hijo', h.pid, 'real', open('hijo.pid').read(), flush=True)\n"
    )


def _pids_y_limpieza(stdout: str) -> list[int]:
    return [int(pid) for pid in re.findall(r"(?:hijo|real) (\d+)", stdout)]


def _matar_si_vive(pids: Sequence[int]) -> None:
    psutil = pytest.importorskip("psutil")
    for pid in pids:
        try:
            proceso = psutil.Process(pid)
            for hijo in proceso.children(recursive=True):
                hijo.kill()
            proceso.kill()
        except psutil.Error:
            pass


@soportado
@pytest.mark.parametrize("heredada", [False, True], ids=["hijo_desligado", "hijo_con_las_tuberias"])
def test_integracion_salida_normal_mata_lo_que_queda(settings_taller: Settings, heredada: bool) -> None:
    """§9.6 paso 6: si el script termina, lo que dejó vivo (que no sea una app conocida) muere enseguida;
    no se escapa del tiempo límite ni de los topes, y una tubería heredada no alarga la espera."""
    settings = settings_taller.con(taller_timeout_s=10.0)
    taller, propuesta = taller_real(settings, _codigo_con_hijo(heredada))
    pids: list[int] = []
    try:
        inicio = time.monotonic()
        resultado = asyncio.run(taller.ejecutar(propuesta.id, propuesta.huella()))
        duracion = time.monotonic() - inicio
        pids = _pids_y_limpieza(resultado.stdout)
        asyncio.run(taller.cerrar())
        assert resultado.codigo == "ejecutado" and not resultado.tiempo_agotado, (resultado.codigo, resultado.stderr)
        assert duracion < 8.0
        assert len(pids) == 2, resultado.stdout
        limite = time.monotonic() + 3.0
        while time.monotonic() < limite and any(_vivo(pid) for pid in pids):
            time.sleep(0.1)
        assert not any(_vivo(pid) for pid in pids)
    finally:
        _matar_si_vive(pids)


@soportado
def test_integracion_conserva_solo_lo_que_permite_conservar(settings_taller: Settings) -> None:
    """Con un ``conservar`` que lo acepta todo, el hijo sobrevive al script (como el Bloc de notas que abre
    ``Start-Process notepad``); ``conservar`` recibe la ruta absoluta del ejecutable de cada proceso."""
    rutas: list[str] = []

    def conservar(ruta: str) -> bool:
        rutas.append(ruta)
        return True

    taller = TallerScripts(settings_taller, generador=GeneradorFalso(respuesta(_codigo_con_hijo(False))),
                           plataforma=sys.platform, analizador_ps=analizador_vacio, carpetas={},
                           sonda_limites=sonda("ninguno"), es_elevado=lambda: False,
                           lanzador=ts.lanzador_por_defecto(sys.platform, conservar=conservar))
    propuesta = asyncio.run(taller.proponer("inocuo", canal_origen="escritorio", ttl_s=600))
    assert isinstance(propuesta, PropuestaScript)
    pids: list[int] = []
    try:
        resultado = asyncio.run(taller.ejecutar(propuesta.id, propuesta.huella()))
        pids = _pids_y_limpieza(resultado.stdout)
        asyncio.run(taller.cerrar())
        assert resultado.codigo == "ejecutado", resultado.stderr
        assert len(pids) == 2 and rutas
        assert all(Path(ruta).is_absolute() for ruta in rutas), rutas
        assert any("python" in Path(ruta).name.lower() for ruta in rutas), rutas
        time.sleep(0.5)
        assert all(_vivo(pid) for pid in pids)
    finally:
        _matar_si_vive(pids)


def test_apps_conocidas_que_sobreviven() -> None:
    raices = [r"C:\Windows", r"C:\Program Files", r"C:\Users\ana\AppData\Local\Programs"]
    assert ts._app_conocida_windows(r"C:\Windows\System32\notepad.exe", raices)
    assert ts._app_conocida_windows(r"c:\program files\Mozilla Firefox\FIREFOX.EXE", raices)
    assert ts._app_conocida_windows(r"C:\Users\ana\AppData\Local\Programs\Microsoft VS Code\Code.exe", raices)
    for ruta in (r"C:\Windows\System32\cmd.exe", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 r"C:\Program Files\Python311\python.exe",
                 r"C:\rams\data\scripts\ejecuciones\ab12cd34-1\notepad.exe",     # copia renombrada: no
                 r"C:\Program Files Malo\notepad.exe", r"C:\Windows\..\Users\ana\notepad.exe", "notepad.exe", ""):
        assert not ts._app_conocida_windows(ruta, raices), ruta
    assert not ts._app_conocida_windows(r"C:\Windows\notepad.exe", [])
    assert ts._app_conocida_linux("/usr/lib/firefox/firefox") and ts._app_conocida_linux("/opt/google/chrome/chrome")
    for ruta in ("/usr/bin/python3", "/usr/bin/bash", "/home/ana/firefox", "/usr/../home/ana/firefox", "firefox", ""):
        assert not ts._app_conocida_linux(ruta), ruta


def test_limpiar_restantes_politica() -> None:
    muertos: list[list[str]] = []

    def con(rondas: list[list[str] | None], estados: Mapping[str, str]) -> int:
        cola = list(rondas)
        return ts._limpiar_restantes(lambda: cola.pop(0) if cola else [], lambda p: estados[p],
                                     lambda ps: muertos.append(list(ps)), rondas=4, plazo_s=60.0)

    estados = {"app": "app", "ajeno": "ajeno", "fuera": "fuera", "dudoso": "dudoso", "app2": "app"}
    assert con([["app", "fuera", "app2"]], estados) == 2 and muertos == []
    assert con([["app", "ajeno"], ["app"]], estados) == 1 and muertos == [["ajeno"]]
    assert con([[]], estados) == 0
    assert con([["app", "dudoso"]], estados) == -1            # algo sin comprobar: se mata todo
    assert con([None], estados) == -1                          # no se pudo listar: se mata todo
    muertos.clear()
    assert con([["ajeno"]] * 10, estados) == -1                # siguen apareciendo: se mata todo
    assert len(muertos) == 4


class _ProcesoFalso:
    """Lo mínimo de ``asyncio.subprocess.Process`` que usa ``_esperar_proceso`` (sin tuberías)."""

    def __init__(self, codigo: int | None) -> None:
        self.returncode = codigo
        self.stdout = None
        self.stderr = None

    async def wait(self) -> int:
        await asyncio.Event().wait()     # como en 3.12+: espera a unas tuberías que no se cierran
        return 0


@pytest.mark.parametrize("limpieza_falla", [False, True])
def test_esperar_proceso_limpia_al_terminar_y_mata_si_falla(limpieza_falla: bool) -> None:
    llamadas: list[str] = []

    async def matar() -> None:
        llamadas.append("matar")

    async def al_terminar() -> None:
        llamadas.append("al_terminar")
        if limpieza_falla:
            raise OSError("falla")

    codigo, agotado, _, _, _ = asyncio.run(ts._esperar_proceso(_ProcesoFalso(0), timeout_s=5.0, max_salida=10,
                                                               matar=matar, al_terminar=al_terminar))
    assert codigo == 0 and not agotado
    assert llamadas == (["al_terminar", "matar"] if limpieza_falla else ["al_terminar"])


def test_esperar_proceso_al_vencer_mata_sin_limpiar() -> None:
    llamadas: list[str] = []
    proceso = _ProcesoFalso(None)

    async def matar() -> None:
        llamadas.append("matar")
        proceso.returncode = 1

    async def al_terminar() -> None:
        llamadas.append("al_terminar")

    codigo, agotado, _, _, _ = asyncio.run(ts._esperar_proceso(proceso, timeout_s=0.2, max_salida=10, matar=matar,
                                                               al_terminar=al_terminar))
    assert agotado and codigo == 1 and llamadas == ["matar"]


@soportado
def test_integracion_salida_enorme_recortada(settings_taller: Settings) -> None:
    codigo = ('import sys\nfor i in range(200000):\n    sys.stdout.write("linea %d de salida\\n" % i)\n'
              'sys.stdout.flush()\n')
    taller, propuesta = taller_real(settings_taller, codigo)
    inicio = time.monotonic()
    resultado = asyncio.run(taller.ejecutar(propuesta.id, propuesta.huella()))
    asyncio.run(taller.cerrar())
    assert resultado.codigo == "ejecutado", resultado.stderr
    assert time.monotonic() - inicio < 25.0
    assert resultado.recortada and resultado.bytes_salida > 3_000_000
    assert len(resultado.stdout.encode("utf-8")) <= settings_taller.taller_max_salida
    assert resultado.stdout.startswith("linea 0 de salida\n")


# ---------------------------------------------------------------------------
# Habilidades (§9.5)
# ---------------------------------------------------------------------------


def ruta_habilidades(settings: Settings) -> Path:
    return Path(settings.taller_dir) / "habilidades.json"


def test_habilidades_guardar_cargar_olvidar(settings_taller: Settings, correr) -> None:
    lanzador = LanzadorFalso()
    reloj = Reloj()
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("fotos")\n')), lanzador=lanzador,
                          reloj=reloj)
    propuesta = proponer_y_obtener(taller, correr)
    habilidad = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), "Renombrar Fotos"))
    assert not isinstance(habilidad, FalloTaller), habilidad
    assert habilidad.id == "renombrar_fotos" and habilidad.nombre == "Renombrar Fotos" and habilidad.usos == 0
    assert habilidad.huella == propuesta.huella() and habilidad.nivel == "bajo"
    datos = json.loads(ruta_habilidades(settings_taller).read_text(encoding="utf-8"))
    assert datos["version"] == 1 and [h["id"] for h in datos["habilidades"]] == ["renombrar_fotos"]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(ruta_habilidades(settings_taller)).st_mode) == 0o600
    actual = taller.propuesta(propuesta.id)
    assert actual is not None and actual.habilidad == "Renombrar Fotos"
    assert [h.id for h in taller.habilidades()] == ["renombrar_fotos"]
    # Nombre repetido (también con otras mayúsculas o tildes) → se pide otro.
    for repetido in ("Renombrar Fotos", "renombrar fotos", "Renombrár fotos"):
        fallo = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), repetido))
        assert isinstance(fallo, FalloTaller) and fallo.codigo == "nombre_invalido", repetido
    for malo in ("", "../x", "a" * 41, "con\ncontrol", "dos;cosas", "   "):
        fallo = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), malo))
        assert isinstance(fallo, FalloTaller) and fallo.codigo == "nombre_invalido", malo
    fallo = correr(taller.guardar_habilidad(propuesta.id, "0" * 64, "Otra"))
    assert isinstance(fallo, FalloTaller) and fallo.codigo == "huella"
    # Cargar: id nueva, re-análisis y sin cambios.
    cargada = correr(taller.cargar_habilidad("renombrar fotos", canal_origen="escritorio", ttl_s=600))
    assert isinstance(cargada, PropuestaScript)
    assert cargada.id != propuesta.id and cargada.habilidad == "Renombrar Fotos" and not cargada.habilidad_cambiada
    assert cargada.codigo == 'print("fotos")\n' and cargada.caduca == reloj.ahora + 600
    # Ejecutar una habilidad suma un uso.
    assert correr(taller.ejecutar(cargada.id, cargada.huella())).codigo == "ejecutado"
    datos = json.loads(ruta_habilidades(settings_taller).read_text(encoding="utf-8"))
    assert datos["habilidades"][0]["usos"] == 1 and datos["habilidades"][0]["usada_utc"]
    # El código cambió en el archivo → se re-analiza y se marca.
    datos["habilidades"][0]["codigo"] = 'import os\nos.remove("x.txt")\n'
    ruta_habilidades(settings_taller).write_text(json.dumps(datos), encoding="utf-8")
    cambiada = correr(taller.cargar_habilidad("Renombrar Fotos", canal_origen="escritorio", ttl_s=600))
    assert isinstance(cambiada, PropuestaScript) and cambiada.habilidad_cambiada
    assert cambiada.analisis.nivel == "alto"
    no_existe = correr(taller.cargar_habilidad("otra", canal_origen="escritorio", ttl_s=600))
    assert isinstance(no_existe, FalloTaller) and no_existe.codigo == "no_existe"
    # Olvidar.
    assert correr(taller.olvidar_habilidad("renombrar fotos")) is True
    assert correr(taller.olvidar_habilidad("renombrar fotos")) is False
    assert json.loads(ruta_habilidades(settings_taller).read_text(encoding="utf-8"))["habilidades"] == []
    assert taller.habilidades() == ()


def test_habilidad_tras_caducar_si_se_ejecuto_hace_poco(settings_taller: Settings, correr) -> None:
    reloj = Reloj()
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), reloj=reloj)
    propuesta = proponer_y_obtener(taller, correr, ttl=60.0)
    assert taller.reabrir("ffffffff") is None
    assert correr(taller.ejecutar(propuesta.id, propuesta.huella())).codigo == "ejecutado"
    reloj.ahora += 120                                   # caducó, pero se ejecutó hace 2 min
    assert taller.propuesta(propuesta.id) is None
    habilidad = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), "Hace poco"))
    assert not isinstance(habilidad, FalloTaller), habilidad
    reloj.ahora += 1801                                  # más de 30 min después: ya no
    fallo = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), "Tarde"))
    assert isinstance(fallo, FalloTaller) and fallo.codigo in ("caducado", "no_existe")


def test_habilidad_no_ejecutable_no_se_guarda(settings_taller: Settings, correr) -> None:
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('import shutil\nshutil.rmtree("/")\n')))
    propuesta = proponer_y_obtener(taller, correr)
    fallo = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), "Peligro"))
    assert isinstance(fallo, FalloTaller)
    assert not ruta_habilidades(settings_taller).exists()


def test_habilidades_archivo_corrupto_se_renombra(settings_taller: Settings, correr,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    ruta = ruta_habilidades(settings_taller)
    ruta.parent.mkdir(parents=True)
    ruta.write_text("{ esto no es json secreto-del-archivo", encoding="utf-8")
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')))
    correr(taller.iniciar())
    assert taller.habilidades() == ()
    corruptos = list(ruta.parent.glob("habilidades.json.corrupto-*"))
    assert len(corruptos) == 1 and not ruta.exists()
    assert "secreto-del-archivo" not in caplog.text
    propuesta = proponer_y_obtener(taller, correr)
    assert not isinstance(correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), "Nueva")), FalloTaller)


def test_habilidades_maximo_100(settings_taller: Settings, correr) -> None:
    ruta = ruta_habilidades(settings_taller)
    ruta.parent.mkdir(parents=True)
    habilidades = [{"id": f"h{i}", "nombre": f"h{i}", "descripcion": "", "lenguaje": "python", "plataforma": "windows",
                    "codigo": "print(1)\n", "huella": "0" * 64, "nivel": "bajo", "creada_utc": "2026-01-01T00:00:00+00:00",
                    "usada_utc": None, "usos": 0} for i in range(100)]
    ruta.write_text(json.dumps({"version": 1, "habilidades": habilidades}), encoding="utf-8")
    taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')))
    propuesta = proponer_y_obtener(taller, correr)
    fallo = correr(taller.guardar_habilidad(propuesta.id, propuesta.huella(), "La 101"))
    assert isinstance(fallo, FalloTaller)
    assert len(json.loads(ruta.read_text(encoding="utf-8"))["habilidades"]) == 100


# ---------------------------------------------------------------------------
# Diagnóstico, lenguajes y cierre
# ---------------------------------------------------------------------------


def test_diagnostico_off(settings_prueba: Settings, settings_taller: Settings, correr) -> None:
    apagado = crear_taller(settings_prueba)
    diagnostico = correr(apagado.iniciar())
    assert diagnostico.clave == "taller" and diagnostico.nombre == "Taller de scripts"
    assert diagnostico.estado.value == "OFF" and diagnostico.detalle == "deshabilitado (RAMS_TALLER=0)"
    otro = crear_taller(settings_taller, plataforma="darwin")
    diagnostico = correr(otro.iniciar())
    assert diagnostico.estado.value == "OFF" and diagnostico.detalle == "sistema no soportado"
    assert otro.lenguajes() == ()


def test_diagnostico_windows(settings_taller: Settings, correr, monkeypatch: pytest.MonkeyPatch) -> None:
    taller = crear_taller(settings_taller, generador=GeneradorFalso(None))
    diagnostico = correr(taller.iniciar())
    assert diagnostico.estado.value == "OK", diagnostico
    assert "PowerShell 5.1" in diagnostico.detalle and "Python 3." in diagnostico.detalle
    assert "≤ 30 s" in diagnostico.detalle and "0 habilidades" in diagnostico.detalle
    assert taller.lenguajes() == ("powershell", "python") and taller.disponible
    sin_generador = crear_taller(settings_taller, generador=None)
    diagnostico = correr(sin_generador.iniciar())
    assert diagnostico.estado.value == "DEGRADADO" and "sin generador" in diagnostico.detalle
    assert not sin_generador.disponible
    monkeypatch.setattr(ts, "detectar_interpretes", lambda plataforma: {"python": sys.executable})
    sin_ps = crear_taller(settings_taller, generador=GeneradorFalso(None))
    diagnostico = correr(sin_ps.iniciar())
    assert diagnostico.estado.value == "DEGRADADO" and "sin PowerShell: solo Python" in diagnostico.detalle
    assert sin_ps.lenguajes() == ("python",)
    sin_bash = crear_taller(settings_taller, plataforma="linux", generador=GeneradorFalso(None),
                            sonda_limites=sonda("cgroups"))
    correr(sin_bash.iniciar())
    assert sin_bash.lenguajes() == ("python",) and "sin bash" in sin_bash.diagnostico().detalle


def test_cerrar_mata_el_script_en_curso(settings_taller: Settings) -> None:
    async def escenario() -> tuple[ResultadoScript, LanzadorFalso, TallerScripts]:
        lanzador = LanzadorFalso(espera=asyncio.Event())
        taller = crear_taller(settings_taller, generador=GeneradorFalso(respuesta('print("a")\n')), lanzador=lanzador)
        propuesta = await taller.proponer("t", canal_origen="escritorio", ttl_s=60)
        assert isinstance(propuesta, PropuestaScript)
        tarea = asyncio.ensure_future(taller.ejecutar(propuesta.id, propuesta.huella()))
        for _ in range(200):
            if lanzador.llamadas:
                break
            await asyncio.sleep(0.01)
        await taller.cerrar()
        return await tarea, lanzador, taller

    resultado, lanzador, taller = asyncio.run(escenario())
    assert lanzador.cancelado and resultado.codigo == "error"
    assert taller.diagnostico().estado.value == "OFF"
    asyncio.run(taller.cerrar())   # idempotente


# ---------------------------------------------------------------------------
# Archivos del módulo
# ---------------------------------------------------------------------------


def test_taller_analizar_ps1_existe_y_es_ascii() -> None:
    ruta = RAIZ / "lobes" / "taller_analizar.ps1"
    assert ruta == ts.RUTA_ANALIZADOR_PS and ruta.is_file()
    datos = ruta.read_bytes()
    datos.decode("ascii")                                    # solo ASCII (sin BOM)
    texto = datos.decode("ascii")
    assert "ParseInput" in texto and "exit 2" in texto and "ConvertTo-Json" in texto
    sin_comentarios = "\n".join(l for l in texto.splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"Invoke-Expression|\biex\b|-Command|\.\s+\$Ruta|&\s*\$Ruta", sin_comentarios,
                         re.IGNORECASE)


def test_modulo_sin_shell_eval_ni_exec() -> None:
    fuente = (RAIZ / "lobes" / "taller_scripts.py").read_text(encoding="utf-8")
    assert "\r" not in fuente and not fuente.startswith("\ufeff")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            if isinstance(nodo.func, ast.Name):
                assert nodo.func.id not in ("eval", "exec", "compile")
            if isinstance(nodo.func, ast.Attribute):
                assert nodo.func.attr not in ("system", "popen", "startfile")
            for clave in nodo.keywords:
                assert not (clave.arg == "shell" and isinstance(clave.value, ast.Constant) and clave.value.value)
