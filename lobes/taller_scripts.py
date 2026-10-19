"""Taller de scripts de R.A.M.S. BIO-CORE (v0.2.2, ``docs/V022_CORREO_DRIVE_SCRIPTS.md`` §9).

Cuando el usuario pide algo que Amadeus no sabe hacer con sus herramientas, ella puede **ofrecer**
escribir un script (PowerShell o Python en Windows; bash o Python en Linux). Este módulo:

- pide el código al generador inyectado (``BrocaLLM.generar_script``, Gemini). El código es **texto**:
  nunca se ejecuta por salir del modelo (§0.2);
- lo **analiza en local** sin ejecutarlo (fail-closed: ante la duda el nivel sube): AST de PowerShell
  con ``lobes/taller_analizar.ps1`` (``Parser::ParseInput``), ``ast`` de Python y heurística para bash.
  El nivel (``bajo | medio | alto | critico``) lo pone el analizador con el catálogo normativo
  :data:`CATALOGO_RIESGO`, nunca Gemini;
- guarda **una** propuesta vigente en memoria (la tarjeta ``script`` de §12.2 sale de
  :meth:`PropuestaScript.vista`, con el código **entero**) y las habilidades en
  ``taller_dir/habilidades.json`` (0o600, escritura atómica);
- **ejecuta** solo cuando ``main`` lo llama tras el botón «Ejecutar» y la compuerta, con la huella
  aprobada (anti-TOCTOU): sin admin, con tiempo límite, carpeta de trabajo propia, entorno de lista
  blanca, salida acotada y redactada, y matando el árbol de procesos al vencer (Job Object en
  Windows; grupo de procesos y, si los hay, cgroups de ``systemd-run --user --scope`` en Linux). Si
  el script termina antes, lo que dejó vivo también muere, salvo las apps conocidas que abrió para el
  usuario (el Bloc de notas, el navegador…): nada se escapa del tiempo límite ni de los topes.

Nunca ejecuta nada por iniciativa propia, desde Telegram ni por decisión del LLM. Lo crítico o con
errores de sintaxis no se ejecuta. Los logs solo llevan lenguaje, nivel, huella[:12], código de salida,
duración y bytes: **nunca** el código, la tarea ni la salida.

Dependencias: biblioteca estándar y ``psutil`` (opcional: reanudar el proceso suspendido; si falta se
usa ``NtResumeProcess``). Sin ``shell=True``, ``os.system``, ``eval`` ni ``exec``. Nada bloqueante en el
event loop: el análisis, la E/S de archivos y las llamadas Win32 van al ``ThreadPoolExecutor`` propio,
y los subprocesos se lanzan con ``asyncio.create_subprocess_exec``.

Degradación (§9.7, §16): sin ``RAMS_TALLER`` → OFF; sistema que no es Windows ni Linux → OFF; sin
PowerShell → solo Python; sin generador → solo habilidades guardadas; R.A.M.S. elevado (o sin poder
comprobarlo) → no ejecuta; analizador de PowerShell caído → heurística con nivel mínimo ``alto``.
"""

from __future__ import annotations

import ast
import asyncio
import codecs
import contextlib
import hmac
import json
import logging
import math
import ntpath
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Protocol

from core.config import Settings, carpetas_usuario, redactar
from core.contracts import (
    CATEGORIAS_RIESGO,
    LENGUAJES_SCRIPT,
    MAX_CODIGO_SCRIPT,
    MAX_HALLAZGOS_TARJETA,
    NIVELES_EJECUTABLES,
    NIVELES_RIESGO,
    PATRON_ARCHIVO_SENSIBLE,
    TTL_TARJETA_S,
    DiagnosticoModulo,
    EstadoModulo,
    formatear_decimal,
    huella_contenido,
    largo_utf16,
)

if TYPE_CHECKING:  # solo para anotaciones
    from core.bus import BusEventos

try:  # opcional: reanudar el proceso suspendido dentro del Job Object (Windows)
    import psutil
except ImportError:  # sin psutil: se usa NtResumeProcess vía ctypes
    psutil = None  # type: ignore[assignment]

log = logging.getLogger("rams.taller")

__all__ = [
    "CATALOGO_RIESGO",
    "PatronRiesgo",
    "Hallazgo",
    "Analisis",
    "PropuestaScript",
    "FalloTaller",
    "SalidaProceso",
    "ResultadoScript",
    "Habilidad",
    "GeneradorScript",
    "Lanzador",
    "AnalizadorPS",
    "ErrorConfinamiento",
    "TallerScripts",
    "lanzador_por_defecto",
    "analizar_python",
    "analizar_bash",
    "analizar_powershell_ast",
    "analizar_powershell_heuristico",
    "detectar_interpretes",
    "sonda_limites_linux",
    "es_elevado_por_defecto",
    "entorno_saneado",
]

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CLAVE: Final = "taller"
NOMBRE: Final = "Taller de scripts"

MAX_TAREA: Final = 500                    # tarea saneada que va al generador (unidades UTF-16)
MAX_TAREA_PROPUESTA: Final = 300          # PropuestaScript.tarea
MAX_EXPLICACION: Final = 600
MAX_EFECTOS: Final = 10
MAX_EFECTO: Final = 160
MAX_REQUISITOS: Final = 10
MAX_REQUISITO: Final = 80
MAX_DETALLE: Final = 160
MAX_MOTIVO: Final = 200
MAX_NOMBRE_HABILIDAD: Final = 40
MAX_DESCRIPCION_HABILIDAD: Final = 300
MAX_HABILIDADES: Final = 100
MAX_EJECUCIONES_GUARDADAS: Final = 20     # carpetas de ejecución que se conservan
MAX_HALLAZGOS_ANALISIS: Final = 1000      # tope de memoria (la tarjeta muestra 20 y cuenta el resto)
VENTANA_GUARDAR_S: Final = 1800.0         # guardar como habilidad hasta 30 min después de ejecutar
TIMEOUT_ANALIZADOR_S: Final = 20.0        # powershell.exe + taller_analizar.ps1
MAX_SALIDA_ANALIZADOR: Final = 1024 * 1024
BLOQUE_LECTURA: Final = 64 * 1024         # lectura acotada de stdout/stderr
GRACIA_TERM_S: Final = 2.0                # Linux: SIGTERM → 2 s → SIGKILL
PLAZO_MUERTE_S: Final = 5.0               # espera a que el árbol muerto suelte las tuberías
PLAZO_SYSTEMCTL_S: Final = 5.0
PLAZO_SONDA_S: Final = 5.0
GRACIA_LECTURA_S: Final = 2.0             # tras salir el proceso, espera a que se vacíen las tuberías
TIMEOUT_MIN_S: Final = 0.5
TIMEOUT_MAX_S: Final = 300.0
RUTA_ANALIZADOR_PS: Final = Path(__file__).resolve().with_name("taller_analizar.ps1")
NOMBRE_HABILIDADES: Final = "habilidades.json"

FALLOS_TALLER: Final = ("deshabilitado", "sin_modelo", "respuesta_invalida", "vacio", "demasiado_grande",
                        "lenguaje_no_disponible", "no_existe", "nombre_invalido", "huella", "caducado", "error")
CODIGOS_RESULTADO: Final = ("ejecutado", "error_salida", "tiempo_agotado", "huella", "caducado", "no_existe",
                            "bloqueado", "deshabilitado", "sin_interprete", "confinamiento", "elevado",
                            "ocupado", "politica", "error")
MODOS_LIMITES: Final = ("cgroups", "prlimit", "ninguno")
CANALES_ORIGEN: Final = ("terminal", "escritorio", "telegram")

# Entorno de lista blanca (§9.6 paso 5): nunca os.environ entero, nunca Settings ni el .env.
# Sin APPDATA ni LOCALAPPDATA (almacenes de credenciales y token de escritorio) ni, en Linux,
# DBUS_SESSION_BUS_ADDRESS (el bus de sesión da acceso al llavero).
ENTORNO_WINDOWS: Final = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "USERPROFILE",
                          "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
                          "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "PUBLIC", "PROGRAMDATA",
                          "PROGRAMFILES", "PROGRAMFILES(X86)", "COMMONPROGRAMFILES")
ENTORNO_LINUX: Final = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DISPLAY",
                        "WAYLAND_DISPLAY")

# Sonda de cgroups (§9.6 paso 4.1): lee su propio cgroup v2 e imprime memory.max y pids.max.
SONDA_CGROUP: Final = (
    "import sys\n"
    "try:\n"
    "    ruta = ''\n"
    "    with open('/proc/self/cgroup', encoding='utf-8') as f:\n"
    "        for linea in f:\n"
    "            partes = linea.strip().split(':', 2)\n"
    "            if len(partes) == 3 and partes[0] == '0':\n"
    "                ruta = partes[2]\n"
    "    base = '/sys/fs/cgroup' + ruta\n"
    "    with open(base + '/memory.max', encoding='utf-8') as f:\n"
    "        memoria = f.read().strip()\n"
    "    with open(base + '/pids.max', encoding='utf-8') as f:\n"
    "        procesos = f.read().strip()\n"
    "    print(memoria)\n"
    "    print(procesos)\n"
    "except Exception:\n"
    "    sys.exit(3)\n"
)
_SONDA_MEMORIA_ESPERADA: Final = "67108864"   # MemoryMax=64M
_SONDA_PROCESOS_ESPERADOS: Final = "4"        # TasksMax=4

# Banderas de CreateProcess y del Job Object (valores de la API de Windows).
_CREATE_SUSPENDED: Final = 0x00000004
_CREATE_NEW_PROCESS_GROUP: Final = 0x00000200
_CREATE_NO_WINDOW: Final = 0x08000000
_JOB_INFO_EXTENDIDA: Final = 9                 # JobObjectExtendedLimitInformation
_JOB_LIMITE_PROCESOS: Final = 0x00000008       # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
_JOB_LIMITE_MEMORIA: Final = 0x00000200        # JOB_OBJECT_LIMIT_JOB_MEMORY
_JOB_MORIR_EXCEPCION: Final = 0x00000400       # JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
_JOB_MATAR_AL_CERRAR: Final = 0x00002000       # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
_JOB_INFO_PIDS: Final = 3                      # JobObjectBasicProcessIdList
_PROCESS_TERMINATE: Final = 0x0001
_PROCESS_SET_QUOTA: Final = 0x0100
_PROCESS_SUSPEND_RESUME: Final = 0x0800
_PROCESS_QUERY_LIMITED: Final = 0x1000          # PROCESS_QUERY_LIMITED_INFORMATION
_SYNCHRONIZE: Final = 0x00100000
_WAIT_OBJECT_0: Final = 0
_ERROR_PARAMETRO_INVALIDO: Final = 87           # ERROR_INVALID_PARAMETER: el pid ya no existe
_MAX_PIDS_JOB: Final = 256                      # cabe de sobra: ACTIVE_PROCESS ≤ 64 (taller_max_procesos)
_ESPERA_TERMINAR_MS: Final = 1000

# Limpieza tras una salida NORMAL del script (§9.6 paso 6): rondas para matar lo que dejó vivo.
_RONDAS_LIMPIEZA: Final = 4
_SONDEO_SALIDA_S: Final = 0.05                  # espera de la salida del proceso principal (no de las tuberías)

_RE_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RE_SUSTITUTO: Final = re.compile("[\ud800-\udfff]")
# Controles de formato que cambian cómo se VE el código respecto de lo que se ejecuta ("Trojan
# Source"): marcas y aislamientos bidireccionales, más los separadores de línea y párrafo Unicode.
_INVISIBLES_PELIGROSOS: Final = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                                          "\u2066\u2067\u2068\u2069\u2028\u2029")
_RE_CERCA_EXTERIOR: Final = re.compile(r"^\s*```[\w+\-.]*[ \t]*\n(?P<cuerpo>.*?)\n?```\s*$", re.DOTALL)
_RE_NOMBRE_HABILIDAD: Final = re.compile(r"^[\w áéíóúüñÁÉÍÓÚÜÑ.\-]{1,40}$")
_RE_ID_HABILIDAD: Final = re.compile(r"^[a-z0-9_-]{1,40}$")
_RE_ARCHIVO_SENSIBLE: Final = re.compile(PATRON_ARCHIVO_SENSIBLE)


# ---------------------------------------------------------------------------
# Catálogo de riesgo (§9.4, normativo; lo fijan los tests)
# ---------------------------------------------------------------------------


class PatronRiesgo(NamedTuple):
    """Fila del catálogo: ``(lenguaje | "*", categoria, nivel, patron, detalle)``.

    ``patron`` es el nombre del comando (PowerShell o bash, en minúsculas y sin alias), el nombre
    calificado de Python (``import:<módulo>`` para un import; ``<módulo>.*`` para todo el módulo) o la
    clave de una regla de contexto (``remove-item+raiz``, ``descarga+iex``…). Las filas ``"*"`` valen
    para los tres lenguajes (rutas, registro, ofuscación…). ``detalle`` va a la tarjeta (≤ 160, en
    español y sin rutas del usuario).
    """

    lenguaje: str
    categoria: str
    nivel: str
    patron: str
    detalle: str


# (lenguaje, categoria, nivel, patrones, detalle): cada patrón genera una fila.
_GRUPOS_CATALOGO: Final[tuple[tuple[str, str, str, tuple[str, ...], str], ...]] = (
    # --- Comunes (rutas, registro, ofuscación, análisis) ---
    ("*", "credenciales", "critico", ("archivo_sensible",),
     "Toca un archivo sensible (claves, certificados, tokens o datos de R.A.M.S.)"),
    ("*", "credenciales", "critico", ("ruta_credenciales",),
     "Ruta a credenciales o datos de navegadores (User Data, Login Data, Cookies, .ssh…)"),
    ("*", "credenciales", "critico", ("ruta_proyecto",), "Ruta dentro de R.A.M.S. o de sus datos privados"),
    ("*", "credenciales", "critico", ("appdata+credenciales",),
     "Datos de aplicaciones junto a nombres de credenciales (navegadores, llaveros, R.A.M.S.)"),
    ("*", "fuera_de_carpeta", "alto", ("appdata",),
     "Ruta construida hacia los datos de aplicaciones (AppData, ~/.config, ~/.local/share)"),
    ("*", "fuera_de_carpeta", "critico", ("ruta_sistema",),
     "Ruta del sistema (Windows, Program Files, /etc, /usr o la raíz de una unidad)"),
    ("*", "fuera_de_carpeta", "alto", ("ruta_externa",), "Ruta fuera de tus carpetas permitidas"),
    ("*", "fuera_de_carpeta", "alto", ("ruta_padre",), "Ruta con «..»: sale de la carpeta de trabajo"),
    ("*", "persistencia", "critico", ("arranque",),
     "Toca el arranque automático (claves Run, carpeta Inicio, .bashrc, autostart)"),
    ("*", "registro", "critico", ("hklm",), "Toca el registro del sistema (HKLM)"),
    ("*", "registro", "alto", ("hkcu",), "Toca el registro de tu usuario (HKCU)"),
    ("*", "ofuscacion", "alto", ("base64_largo",), "Cadena larga en base64: puede esconder código"),
    ("*", "descarga_ejecucion", "critico", ("instalar",),
     "Instala programas o paquetes desde internet (pip, npm, winget…)"),
    ("*", "sintaxis", "alto", ("sintaxis",), "Errores de sintaxis: el script no se puede ejecutar"),
    ("*", "tamano", "critico", ("tamano",), "Script demasiado grande para analizarlo: no se ejecuta"),
    ("*", "analisis_degradado", "alto", ("degradado",),
     "Análisis aproximado (sin el analizador de PowerShell): riesgo mínimo alto"),
    ("*", "sobrescritura", "alto", ("escritura+usuario",),
     "Escribe sobre archivos de tus carpetas (puede sobrescribirlos)"),
    ("*", "sobrescritura", "alto", ("mover+usuario",), "Mueve o renombra archivos tuyos o en bloque"),
    ("*", "fuera_de_carpeta", "medio", ("lectura+usuario",), "Lee archivos o carpetas tuyas"),
    ("*", "sobrescritura", "medio", ("escritura+trabajo",),
     "Crea o escribe archivos en la carpeta de trabajo o archivos nuevos en tus carpetas"),
    # --- PowerShell: crítico ---
    ("powershell", "borrado", "critico", ("remove-item+raiz",),
     "Borrado recursivo de una unidad, del perfil, de una variable de entorno o con comodín"),
    ("powershell", "borrado", "critico", ("format-volume", "clear-disk", "initialize-disk", "remove-partition",
                                          "format", "diskpart", "cipher"),
     "Formatea o borra discos enteros"),
    ("powershell", "elevacion", "critico", ("start-process+runas", "runas"),
     "Pide permisos de administrador o de otro usuario (RunAs)"),
    ("powershell", "seguridad", "critico", ("set-mppreference", "add-mppreference", "remove-mppreference",
                                            "set-executionpolicy", "bcdedit", "set-netfirewallprofile",
                                            "new-netfirewallrule", "set-netfirewallrule",
                                            "disable-netfirewallrule", "remove-netfirewallrule",
                                            "netsh+firewall", "wevtutil", "vssadmin", "new-localuser",
                                            "set-localuser", "add-localgroupmember", "new-smbshare",
                                            "disable-computerrestore", "set-processmitigation"),
     "Desactiva o cambia la seguridad del sistema (antivirus, cortafuegos, directivas)"),
    ("powershell", "descarga_ejecucion", "critico", ("descarga+iex", "-encodedcommand", "mshta",
                                                     "install-module", "install-script", "install-package"),
     "Descarga código de internet y lo ejecuta (o ejecuta código codificado)"),
    ("powershell", "credenciales", "critico", ("export-pfxcertificate", "cmdkey", "vaultcmd", "passwordvault",
                                               "protecteddata"),
     "Accede a credenciales guardadas o exporta claves"),
    ("powershell", "apagado", "critico", ("stop-computer", "restart-computer", "shutdown", "logoff",
                                          "remove-computer", "rename-computer"),
     "Apaga, reinicia o cierra la sesión del equipo"),
    ("powershell", "persistencia", "critico", ("register-scheduledtask", "new-scheduledtask",
                                               "new-scheduledtaskaction", "new-scheduledtasktrigger",
                                               "set-scheduledtask", "schtasks", "register-wmievent",
                                               "set-wmiinstance", "schedule.service"),
     "Crea tareas programadas o arranques automáticos"),
    ("powershell", "servicios", "critico", ("new-service", "set-service", "remove-service", "sc.exe"),
     "Crea o cambia servicios de Windows"),
    ("powershell", "ofuscacion", "critico", ("frombase64string+iex", "char+iex", "dinamico+iex"),
     "Decodifica o reensambla texto y lo ejecuta (ofuscación)"),
    ("powershell", "registro", "critico", ("reg+modifica",), "Borra o importa claves del registro (reg.exe)"),
    # --- PowerShell: alto ---
    ("powershell", "borrado", "alto", ("remove-item", "clear-recyclebin", "remove-appxpackage",
                                       "uninstall-package"),
     "Borra archivos, carpetas o programas"),
    ("powershell", "sobrescritura", "alto", ("takeown", "icacls", "set-acl", "attrib"),
     "Cambia permisos, dueños o atributos de archivos"),
    ("powershell", "red", "alto", ("invoke-webrequest", "invoke-restmethod", "start-bitstransfer",
                                   "send-mailmessage", "bitsadmin", "certutil", "netsh", "ftp", "net",
                                   "net.webclient", "downloadfile", "downloadstring"),
     "Se conecta a internet o a la red"),
    ("powershell", "subproceso", "alto", ("start-process", "invoke-item", "start-job", "start-threadjob",
                                          "cmd", "powershell", "pwsh", "wscript", "cscript", "wmic",
                                          "msiexec", "ejecutable", "[diagnostics.process]::start",
                                          "wscript.shell", "shell.application", "cmdlet_desconocido"),
     "Lanza otro programa o un comando que no reconozco"),
    ("powershell", "winapi", "alto", ("add-type", "import-module", "rundll32", "regsvr32",
                                      "[reflection.assembly]::load", "[runtime.interopservices.marshal]",
                                      "sendkeys", "using assembly"),
     "Compila código .NET, carga módulos o llama a la API de Windows"),
    ("powershell", "dinamico", "alto", ("& $variable", "invoke-expression", "invoke-command", "set-alias",
                                        "new-alias", "invoke-history", "register-objectevent",
                                        "register-engineevent", "[scriptblock]::create"),
     "Ejecuta texto o bloques de código dinámicos: no puedo verificar qué hacen"),
    ("powershell", "ofuscacion", "alto", ("frombase64string",), "Decodifica base64 (posible ofuscación)"),
    ("powershell", "servicios", "alto", ("stop-process", "taskkill", "stop-service", "start-service",
                                         "restart-service", "suspend-service", "resume-service"),
     "Cierra procesos o detiene servicios"),
    ("powershell", "registro", "alto", ("new-itemproperty", "set-itemproperty", "remove-itemproperty", "reg",
                                        "setenvironmentvariable"),
     "Cambia valores del registro o variables de entorno permanentes"),
    ("powershell", "seguridad", "alto", ("unblock-file",),
     "Quita la marca de «descargado de internet» a archivos"),
    ("powershell", "credenciales", "alto", ("get-clipboard",), "Lee el portapapeles (puede tener contraseñas)"),
    # --- PowerShell: medio ---
    ("powershell", "fuera_de_carpeta", "medio", ("get-childitem", "get-content", "get-item", "test-path",
                                                 "resolve-path", "select-string", "import-csv",
                                                 "get-filehash", "get-acl", "import-clixml"),
     "Lee archivos o carpetas tuyas"),
    ("powershell", "sobrescritura", "medio", ("set-content", "add-content", "out-file", "clear-content",
                                              "export-csv", "export-clixml", "tee-object", "new-item",
                                              "copy-item", "expand-archive", "compress-archive", "move-item",
                                              "rename-item"),
     "Crea o escribe archivos"),
    ("powershell", "servicios", "medio", ("get-process", "get-ciminstance", "get-wmiobject", "get-service",
                                          "get-computerinfo", "get-psdrive", "get-volume", "get-disk",
                                          "get-physicaldisk", "get-netadapter", "get-netipaddress",
                                          "get-netipconfiguration", "get-nettcpconnection", "get-hotfix",
                                          "get-winevent", "get-eventlog", "get-counter", "get-printer",
                                          "get-scheduledtask", "systeminfo", "ipconfig", "hostname", "whoami",
                                          "tasklist", "getmac", "driverquery", "schtasks+consulta",
                                          "reg+consulta"),
     "Consulta procesos o información del sistema"),
    ("powershell", "red", "medio", ("test-connection", "test-netconnection", "resolve-dnsname", "ping",
                                    "nslookup", "tracert", "pathping"),
     "Consulta la red (ping o DNS)"),
    ("powershell", "interactivo", "medio", ("read-host", "get-credential", "out-gridview", "pause",
                                            "show-command", "readkey"),
     "Pide datos por teclado: sin consola fallará"),
    # --- bash: crítico ---
    ("bash", "borrado", "critico", ("rm+raiz",),
     "Borrado recursivo de la raíz, del home, de una variable o con comodín sin confinar"),
    ("bash", "borrado", "critico", ("mkfs", "shred", "wipefs", "fdisk", "parted", "sfdisk", "blkdiscard",
                                    "dd+dispositivo"),
     "Formatea, destruye o escribe directamente en discos"),
    ("bash", "elevacion", "critico", ("sudo", "doas", "pkexec", "su", "run0", "sudoedit"),
     "Pide permisos de administrador o de otro usuario"),
    ("bash", "seguridad", "critico", ("setenforce", "ufw", "iptables", "ip6tables", "nft", "firewall-cmd",
                                      "aa-disable", "aa-complain", "aa-teardown", "systemctl+seguridad"),
     "Desactiva o cambia la seguridad del sistema (cortafuegos, SELinux, AppArmor)"),
    ("bash", "descarga_ejecucion", "critico", ("curl|sh",), "Descarga código de internet y lo ejecuta"),
    ("bash", "credenciales", "critico", ("secret-tool", "kwallet-query", "kwalletcli", "pass", "dbus+secretos"),
     "Accede al llavero o a contraseñas guardadas"),
    ("bash", "apagado", "critico", ("shutdown", "reboot", "halt", "poweroff", "telinit", "systemctl+apagado",
                                    "loginctl+apagado"),
     "Apaga, reinicia o suspende el equipo"),
    ("bash", "persistencia", "critico", ("crontab", "at", "batch", "systemd-run", "systemctl+arranque"),
     "Programa tareas o crea arranques automáticos"),
    ("bash", "ofuscacion", "critico", ("eval+base64",), "Decodifica base64 y lo ejecuta (ofuscación)"),
    # --- bash: alto ---
    ("bash", "borrado", "alto", ("rm", "rmdir", "unlink", "find+delete"), "Borra archivos o carpetas"),
    ("bash", "sobrescritura", "alto", ("chmod", "chown", "chgrp", "chattr", "setfacl", "truncate", "dd"),
     "Cambia permisos o reescribe archivos"),
    ("bash", "red", "alto", ("curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "ftp",
                             "rsync", "telnet", "aria2c"),
     "Se conecta a internet o a la red"),
    ("bash", "subproceso", "alto", ("xdg-open", "gio", "python", "python3", "perl", "ruby", "node", "bash",
                                    "sh", "zsh", "dash", "flatpak", "snap", "exec", "binario", "dbus",
                                    "find+exec"),
     "Lanza otro programa o un comando que no reconozco"),
    ("bash", "dinamico", "alto", ("eval", "source", ".", "alias", "$comando", "$(…)"),
     "Ejecuta texto o comandos construidos: no puedo verificar qué hacen"),
    ("bash", "ofuscacion", "alto", ("base64",), "Decodifica base64 (posible ofuscación)"),
    ("bash", "servicios", "alto", ("kill", "pkill", "killall", "service", "systemctl"),
     "Cierra procesos o cambia servicios"),
    # --- bash: medio ---
    ("bash", "sobrescritura", "medio", ("mkdir", "touch", "cp", "mv", "ln", "tee", "tar", "zip", "unzip",
                                        "gzip", "gunzip", "bzip2", "xz", "7z", "sed"),
     "Crea o escribe archivos"),
    ("bash", "servicios", "medio", ("ps", "pgrep", "df", "uname", "free", "uptime", "lsblk", "lscpu", "lsusb",
                                    "lspci", "ip", "ss", "netstat", "hostnamectl", "journalctl", "dmesg", "who",
                                    "w", "last", "vmstat", "sensors", "top", "systemctl+consulta",
                                    "crontab+consulta", "loginctl"),
     "Consulta procesos o información del sistema"),
    ("bash", "red", "medio", ("ping", "dig", "nslookup", "host", "traceroute", "tracepath"),
     "Consulta la red (ping o DNS)"),
    ("bash", "interactivo", "medio", ("read", "select"), "Pide datos por teclado: sin consola fallará"),
    # --- Python: crítico ---
    ("python", "borrado", "critico", ("shutil.rmtree+raiz",), "Borrado recursivo de la raíz o del home"),
    ("python", "elevacion", "critico", ("ShellExecute+runas",), "Pide permisos de administrador (runas)"),
    ("python", "descarga_ejecucion", "critico", ("descarga+ejecucion",),
     "Descarga de internet y ejecuta el resultado"),
    ("python", "ofuscacion", "critico", ("exec+b64decode", "marshal.loads", "marshal.load", "pickle.loads",
                                         "pickle.load", "dill.loads", "dill.load"),
     "Ejecuta código decodificado o deserializado (ofuscación)"),
    ("python", "credenciales", "critico", ("import:keyring", "import:secretstorage", "import:win32crypt",
                                           "import:browser_cookie3", "sqlite3+navegador",
                                           "CryptUnprotectData"),
     "Accede al llavero, a credenciales guardadas o a bases de datos del navegador"),
    ("python", "registro", "critico", ("winreg+HKEY_LOCAL_MACHINE",), "Toca el registro del sistema (HKLM)"),
    # --- Python: alto ---
    ("python", "borrado", "alto", ("os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree",
                                   "pathlib.Path.unlink", "pathlib.Path.rmdir", "send2trash.send2trash"),
     "Borra archivos o carpetas"),
    ("python", "sobrescritura", "alto", ("os.truncate", "os.chmod", "os.chown", "os.lchown", "shutil.chown",
                                         "pathlib.Path.chmod"),
     "Cambia permisos o vacía archivos"),
    ("python", "red", "alto", ("import:requests", "import:httpx", "import:aiohttp", "import:urllib3",
                               "import:urllib.request", "import:http.client", "import:http.server",
                               "import:socketserver", "import:ftplib", "import:smtplib", "import:imaplib",
                               "import:poplib", "import:telnetlib", "import:paramiko", "import:websocket",
                               "import:websockets", "import:xmlrpc", "urllib.request.*", "http.client.*",
                               "socket.socket", "socket.create_connection", "socket.create_server"),
     "Se conecta a internet o a la red"),
    ("python", "subproceso", "alto", ("subprocess.*", "os.system", "os.popen", "os.startfile", "os.exec*",
                                      "os.spawn*", "os.posix_spawn", "os.posix_spawnp", "pty.spawn",
                                      "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
                                      "multiprocessing.*", "import:pty"),
     "Lanza otros programas"),
    ("python", "winapi", "alto", ("import:ctypes", "import:win32api", "import:win32con", "import:win32com",
                                  "import:win32gui", "import:win32process", "import:pywintypes",
                                  "import:importlib", "__import__", "importlib.import_module"),
     "Llama a la API del sistema o carga módulos de forma dinámica"),
    ("python", "dinamico", "alto", ("eval", "exec", "compile", "getattr", "setattr", "delattr", "llamada",
                                    "__builtins__"),
     "Ejecuta texto como código o usa nombres dinámicos: no puedo verificar qué hace"),
    ("python", "ofuscacion", "alto", ("base64.b64decode", "base64.decodebytes", "base64.b32decode",
                                      "base64.a85decode", "base64.b85decode", "base64.urlsafe_b64decode",
                                      "base64.standard_b64decode", "codecs.decode", "binascii.a2b_base64"),
     "Decodifica base64 (posible ofuscación)"),
    ("python", "registro", "alto", ("import:winreg", "import:_winreg"), "Usa el registro de Windows"),
    ("python", "servicios", "alto", ("os.kill", "os.killpg", "psutil.Process.kill", "psutil.Process.terminate",
                                     "signal.pthread_kill"),
     "Cierra procesos"),
    # --- Python: medio ---
    ("python", "fuera_de_carpeta", "medio", ("os.listdir", "os.scandir", "os.walk", "glob.glob", "glob.iglob",
                                             "pathlib.Path.glob", "pathlib.Path.rglob", "pathlib.Path.iterdir",
                                             "pathlib.Path.read_text", "pathlib.Path.read_bytes"),
     "Lee archivos o carpetas tuyas"),
    ("python", "sobrescritura", "medio", ("open", "io.open", "codecs.open", "os.makedirs", "os.mkdir",
                                          "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
                                          "shutil.move", "os.rename", "os.renames", "os.replace",
                                          "pathlib.Path.write_text", "pathlib.Path.write_bytes",
                                          "pathlib.Path.touch", "pathlib.Path.mkdir", "pathlib.Path.rename",
                                          "pathlib.Path.replace", "shutil.make_archive", "shutil.unpack_archive"),
     "Crea o escribe archivos"),
    ("python", "servicios", "medio", ("import:platform", "import:psutil", "shutil.disk_usage",
                                      "os.getloadavg"),
     "Consulta procesos o información del sistema"),
    ("python", "red", "medio", ("socket.gethostbyname", "socket.getaddrinfo", "socket.gethostbyaddr"),
     "Consulta la red (DNS)"),
    ("python", "interactivo", "medio", ("input", "getpass.getpass"), "Pide datos por teclado: sin consola fallará"),
)

CATALOGO_RIESGO: Final[tuple[PatronRiesgo, ...]] = tuple(
    PatronRiesgo(lenguaje, categoria, nivel, patron, detalle)
    for lenguaje, categoria, nivel, patrones, detalle in _GRUPOS_CATALOGO
    for patron in patrones
)
_FILAS: Final[dict[tuple[str, str], PatronRiesgo]] = {(f.lenguaje, f.patron): f for f in CATALOGO_RIESGO}


def _fila(lenguaje: str, patron: str) -> PatronRiesgo:
    """Fila del catálogo para ``(lenguaje, patron)``; si no existe, la común ``("*", patron)``."""
    fila = _FILAS.get((lenguaje, patron)) or _FILAS.get(("*", patron))
    if fila is None:  # error de programación: se trata como lo peor (fail-closed)
        log.debug("Patrón de riesgo sin fila en el catálogo: %s/%s", lenguaje, patron)
        return PatronRiesgo(lenguaje, "dinamico", "critico", patron, "Patrón no catalogado")
    return fila


def _indice_nivel(nivel: str) -> int:
    try:
        return NIVELES_RIESGO.index(nivel)
    except ValueError:
        return len(NIVELES_RIESGO) - 1   # desconocido ⇒ crítico


# ---------------------------------------------------------------------------
# Tipos (§9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Hallazgo:
    """Un hallazgo del análisis: nivel, categoría, línea (1..n o ``None``) y detalle (≤ 160)."""

    nivel: str
    categoria: str
    linea: int | None
    detalle: str

    def a_dict(self) -> dict[str, Any]:
        return {"nivel": self.nivel, "categoria": self.categoria, "linea": self.linea, "detalle": self.detalle}


def _clave_gravedad(hallazgo: Hallazgo) -> tuple[int, int]:
    """Orden de la tarjeta: de más a menos grave y, dentro del nivel, por línea (sin línea, primero)."""
    return (-_indice_nivel(hallazgo.nivel), hallazgo.linea if hallazgo.linea is not None else 0)


@dataclass(frozen=True)
class Analisis:
    """Resultado del análisis estático. El nivel es el máximo de los hallazgos (o el mínimo del método)."""

    nivel: str
    hallazgos: tuple[Hallazgo, ...]
    metodo: str            # "ast" | "heuristico"
    sintaxis_ok: bool

    @property
    def ejecutable(self) -> bool:
        """Se puede ejecutar desde la app: sintaxis correcta y nivel bajo, medio o alto."""
        return bool(self.sintaxis_ok) and self.nivel in NIVELES_EJECUTABLES

    def motivo_no_ejecutable(self) -> str:
        """Por qué no tiene botón «Ejecutar» (``""`` si es ejecutable). ≤ 200, sin contenido privado."""
        if self.ejecutable:
            return ""
        if any(h.categoria == "tamano" for h in self.hallazgos):
            return "El script es demasiado grande para revisarlo: no lo ejecuto"
        if not self.sintaxis_ok:
            return "El script tiene errores de sintaxis: así no se puede ejecutar"
        if self.nivel == "critico":
            return ("Riesgo crítico: es demasiado peligroso para que yo lo ejecute; "
                    "te lo dejo para copiar, por si quieres revisarlo y correrlo tú")
        return "No se puede ejecutar"

    def a_dict(self) -> dict[str, Any]:
        return {"nivel": self.nivel, "hallazgos": [h.a_dict() for h in self.hallazgos], "metodo": self.metodo,
                "sintaxis_ok": self.sintaxis_ok, "ejecutable": self.ejecutable,
                "motivo_no_ejecutable": self.motivo_no_ejecutable()}


@dataclass(frozen=True)
class PropuestaScript:
    """Un script propuesto (generado o cargado de una habilidad), con su análisis. Vive en memoria."""

    id: str
    version: int
    tarea: str                     # ≤ 300, ya redactada por main
    lenguaje: str
    plataforma: str                # "windows" | "linux"
    codigo: str                    # normalizado: NFC, LF, sin BOM, ≤ MAX_CODIGO_SCRIPT bytes
    explicacion: str
    efectos: tuple[str, ...]
    requisitos: tuple[str, ...]
    analisis: Analisis
    habilidad: str | None
    habilidad_cambiada: bool
    ejecuciones: int
    canal_origen: str
    creado: float
    caduca: float

    def huella(self) -> str:
        """sha256 de ``{"clase": "script", "lenguaje", "codigo"}`` (lo que el usuario aprueba)."""
        return huella_contenido({"clase": "script", "lenguaje": self.lenguaje, "codigo": self.codigo})

    def vista(self) -> dict[str, Any]:
        """Sección ``script`` de la tarjeta (§12.2), exacta: el código ENTERO; los hallazgos de más a
        menos grave (y por línea), solo los ``MAX_HALLAZGOS_TARJETA`` primeros, y el resto contado en
        ``hallazgos_omitidos``."""
        ordenados = sorted(self.analisis.hallazgos, key=_clave_gravedad)
        visibles = ordenados[:MAX_HALLAZGOS_TARJETA]
        return {
            "lenguaje": self.lenguaje,
            "plataforma": self.plataforma,
            "codigo": self.codigo,
            "explicacion": self.explicacion,
            "efectos": list(self.efectos),
            "requisitos": list(self.requisitos),
            "nivel": self.analisis.nivel,
            "ejecutable": self.analisis.ejecutable,
            "motivo_no_ejecutable": _recortar_utf16(self.analisis.motivo_no_ejecutable(), MAX_MOTIVO),
            "metodo": self.analisis.metodo,
            "hallazgos": [h.a_dict() for h in visibles],
            "hallazgos_omitidos": len(ordenados) - len(visibles),
            "habilidad": self.habilidad,
            "habilidad_cambiada": self.habilidad_cambiada,
            "ejecuciones": self.ejecuciones,
        }


@dataclass(frozen=True)
class FalloTaller:
    """Fallo al proponer, guardar o cargar. ``mensaje`` en español, sin rutas ni contenido privado."""

    codigo: str     # ∈ FALLOS_TALLER
    mensaje: str


@dataclass(frozen=True)
class SalidaProceso:
    """Lo que devuelve un :data:`Lanzador`: salida cruda acotada y cómo terminó."""

    codigo_salida: int | None
    tiempo_agotado: bool
    duracion_s: float
    stdout: bytes
    stderr: bytes
    recortada: bool
    bytes_totales: int
    confinado: bool


@dataclass(frozen=True)
class ResultadoScript:
    """Resultado de :meth:`TallerScripts.ejecutar`. La salida ya va recortada y redactada."""

    ok: bool                  # ejecutado con código 0 y sin tiempo agotado
    codigo: str               # ∈ CODIGOS_RESULTADO
    mensaje: str
    codigo_salida: int | None = None
    tiempo_agotado: bool = False
    duracion_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    recortada: bool = False
    bytes_salida: int = 0
    carpeta: str = ""         # relativa a base_dir, p. ej. "data/scripts/ejecuciones/ab12cd34-1"


@dataclass(frozen=True)
class Habilidad:
    """Script guardado para volver a usarlo (siempre con re-análisis y confirmación)."""

    id: str
    nombre: str
    descripcion: str
    lenguaje: str
    plataforma: str
    codigo: str
    huella: str
    nivel: str
    creada_utc: str
    usada_utc: str | None
    usos: int

    def a_dict(self) -> dict[str, Any]:
        return {"id": self.id, "nombre": self.nombre, "descripcion": self.descripcion,
                "lenguaje": self.lenguaje, "plataforma": self.plataforma, "codigo": self.codigo,
                "huella": self.huella, "nivel": self.nivel, "creada_utc": self.creada_utc,
                "usada_utc": self.usada_utc, "usos": self.usos}


class GeneradorScript(Protocol):
    """``await generador(tarea, plataforma="windows"|"linux", lenguajes=("powershell","python"))``.

    Devuelve ``{"lenguaje","codigo","explicacion","efectos","requisitos"}`` o ``None`` (sin modelo).
    Lo implementa ``BrocaLLM.generar_script`` (§10.2)."""

    def __call__(self, tarea: str, *, plataforma: str,
                 lenguajes: Sequence[str]) -> Awaitable[Mapping[str, Any] | None]: ...


class Lanzador(Protocol):
    """``await lanzador(argv, *, cwd, env, timeout_s, max_salida, max_procesos, max_memoria_mb,
    unidad=None)``. ``argv`` ya trae el prefijo de límites de Linux (§9.6 paso 4); ``unidad`` es
    ``"rams-taller-<id>-<n>"`` en modo cgroups (``None`` fuera de él). Lanza :class:`ErrorConfinamiento`
    si no puede confinar el proceso (nunca lo deja correr sin confinar)."""

    def __call__(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float,
                 max_salida: int, max_procesos: int, max_memoria_mb: int,
                 unidad: str | None = None) -> Awaitable[SalidaProceso]: ...


class AnalizadorPS(Protocol):
    """``await analizador(ruta)`` → el JSON de ``taller_analizar.ps1`` o ``None`` si no pudo."""

    def __call__(self, ruta: Path) -> Awaitable[Mapping[str, Any] | None]: ...


class ErrorConfinamiento(RuntimeError):
    """El lanzador no pudo meter el proceso en su Job Object: se mató sin llegar a reanudarlo."""


# ---------------------------------------------------------------------------
# Texto: saneo y normalización
# ---------------------------------------------------------------------------


def _sin_sustitutos(texto: str) -> str:
    """Cambia los sustitutos sueltos (U+D800–U+DFFF) por U+FFFD."""
    return _RE_SUSTITUTO.sub("\ufffd", texto)


def _recortar_utf16(texto: str, maximo: int, *, elipsis: str = "…") -> str:
    """Recorta a ``maximo`` unidades UTF-16 sin partir un carácter, añadiendo ``elipsis`` si recorta."""
    if largo_utf16(texto) <= maximo:
        return texto
    limite = max(0, maximo - largo_utf16(elipsis))
    unidades = 0
    partes: list[str] = []
    for caracter in texto:
        ancho = 2 if ord(caracter) > 0xFFFF else 1
        if unidades + ancho > limite:
            break
        partes.append(caracter)
        unidades += ancho
    return "".join(partes).rstrip() + elipsis


_SIN_TOPE: Final = sys.maxsize     # _texto_informativo sin recortar (para comprobar los topes antes)


def _es_control(caracter: str) -> bool:
    return unicodedata.category(caracter) == "Cc" or caracter in _INVISIBLES_PELIGROSOS


def _texto_informativo(valor: Any, maximo: int, *, multilinea: bool = False) -> str:
    """Texto informativo de la tarjeta: sin controles (``\\n`` solo si ``multilinea``), sin sustitutos,
    espacios colapsados y recortado a ``maximo`` unidades UTF-16 con «…»."""
    if not isinstance(valor, str):
        return ""
    texto = _sin_sustitutos(unicodedata.normalize("NFC", valor.replace("\r\n", "\n").replace("\r", "\n")))
    limpio = []
    for caracter in texto:
        if caracter == "\n" and multilinea:
            limpio.append("\n")
        elif _es_control(caracter):
            limpio.append(" ")
        else:
            limpio.append(caracter)
    texto = "".join(limpio)
    if multilinea:
        lineas = [re.sub(r"[ \t]+", " ", linea).strip() for linea in texto.split("\n")]
        texto = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    else:
        texto = re.sub(r"\s+", " ", texto).strip()
    return _recortar_utf16(texto, maximo)


def _lista_informativa(valor: Any, maximo_items: int, maximo_texto: int) -> tuple[str, ...] | None:
    """Lista de textos informativos ya saneados. ``None`` si el tipo no es una lista de textos o si se pasa
    de los topes (más de ``maximo_items`` textos no vacíos, o alguno de más de ``maximo_texto`` unidades
    UTF-16): §9.3 paso 4 pide entonces ``respuesta_invalida``, nunca recortar en silencio."""
    if valor is None:
        return ()
    if isinstance(valor, str) or not isinstance(valor, Sequence):
        return None
    resultado: list[str] = []
    for elemento in valor:
        if not isinstance(elemento, str):
            return None
        texto = _texto_informativo(elemento, _SIN_TOPE)
        if not texto:
            continue
        if largo_utf16(texto) > maximo_texto or len(resultado) >= maximo_items:
            return None
        resultado.append(texto)
    return tuple(resultado)


def sanear_tarea(tarea: Any) -> str:
    """La tarea (ya redactada por main): sin controles ni sustitutos, espacios colapsados, ≤ 500."""
    return _texto_informativo(tarea, MAX_TAREA)


def normalizar_codigo(codigo: Any) -> tuple[str | None, str]:
    """Normaliza el código del generador. Devuelve ``(codigo, "")`` o ``(None, motivo)`` con
    ``motivo`` ∈ ``FALLOS_TALLER`` (``respuesta_invalida`` o ``demasiado_grande``).

    Quita una cerca exterior ```` ``` ```` (y rechaza cualquier otra), el BOM, pasa a NFC y LF, y
    rechaza los caracteres de control distintos de ``\\n`` y ``\\t`` (y los invisibles bidireccionales),
    los sustitutos sueltos y lo que pase de ``MAX_CODIGO_SCRIPT`` bytes UTF-8. **Nunca recorta.**
    """
    if not isinstance(codigo, str):
        return None, "respuesta_invalida"
    texto = codigo.replace("\r\n", "\n").replace("\r", "\n")
    cerca = _RE_CERCA_EXTERIOR.match(texto)
    if cerca is not None:
        texto = cerca.group("cuerpo")
    if texto.startswith("\ufeff"):
        texto = texto[1:]
    if "```" in texto:
        return None, "respuesta_invalida"
    if _RE_SUSTITUTO.search(texto):
        return None, "respuesta_invalida"
    texto = unicodedata.normalize("NFC", texto)
    if any(caracter not in "\n\t" and _es_control(caracter) for caracter in texto):
        return None, "respuesta_invalida"
    if not texto.strip():
        return None, "respuesta_invalida"
    if len(texto.encode("utf-8")) > MAX_CODIGO_SCRIPT:
        return None, "demasiado_grande"
    return texto, ""


def _plataforma_de(plataforma: str) -> str:
    """``"windows"`` | ``"linux"`` | ``"otro"`` a partir de ``sys.platform``."""
    if plataforma == "win32":
        return "windows"
    if str(plataforma).startswith("linux"):
        return "linux"
    return "otro"


def _ahora_utc(segundos: float) -> str:
    try:
        return datetime.fromtimestamp(segundos, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _segundos_legibles(segundos: float) -> str:
    if not math.isfinite(segundos):
        return "—"
    return str(int(segundos)) if float(segundos).is_integer() else formatear_decimal(segundos, 1)


# ---------------------------------------------------------------------------
# Análisis: colector de hallazgos
# ---------------------------------------------------------------------------


class _Colector:
    """Acumula hallazgos sin repetidos (misma categoría, nivel, línea y detalle)."""

    def __init__(self, lenguaje: str) -> None:
        self.lenguaje = lenguaje
        self._items: dict[tuple[str, str, int | None, str], Hallazgo] = {}
        self.patrones: set[str] = set()
        self.lineas: dict[str, int | None] = {}
        self.contador = 0

    def agregar(self, patron: str, linea: int | None, *, lenguaje: str | None = None) -> None:
        self.contador += 1
        fila = _fila(lenguaje or self.lenguaje, patron)
        if linea is not None and (not isinstance(linea, int) or linea < 1):
            linea = None
        detalle = _recortar_utf16(fila.detalle, MAX_DETALLE)
        clave = (fila.categoria, fila.nivel, linea, detalle)
        self.patrones.add(fila.patron)
        self.lineas.setdefault(fila.patron, linea)
        if clave not in self._items and len(self._items) < MAX_HALLAZGOS_ANALISIS:
            categoria = fila.categoria if fila.categoria in CATEGORIAS_RIESGO else "dinamico"
            self._items[clave] = Hallazgo(fila.nivel, categoria, linea, detalle)

    def tiene(self, patron: str) -> bool:
        return patron in self.patrones

    def analisis(self, metodo: str, sintaxis_ok: bool, minimo: str = "bajo") -> Analisis:
        hallazgos = tuple(self._items.values())
        indice = _indice_nivel(minimo)
        for hallazgo in hallazgos:
            indice = max(indice, _indice_nivel(hallazgo.nivel))
        return Analisis(NIVELES_RIESGO[indice], hallazgos, metodo, bool(sintaxis_ok))


def _analisis_tamano(metodo: str) -> Analisis:
    colector = _Colector("*")
    colector.agregar("tamano", None)
    return colector.analisis(metodo, False)


def _demasiado_grande(codigo: str) -> bool:
    return len(codigo.encode("utf-8", "surrogatepass")) > MAX_CODIGO_SCRIPT


# ---------------------------------------------------------------------------
# Análisis: rutas (§9.4 «Carpetas permitidas» y «Rutas construidas»)
# ---------------------------------------------------------------------------

# Nombres de las carpetas conocidas del usuario bajo su perfil (en inglés y en español).
_NOMBRES_CARPETAS_USUARIO: Final = frozenset({
    "downloads", "descargas", "documents", "documentos", "mis documentos", "desktop", "escritorio",
    "pictures", "imagenes", "imágenes", "mis imágenes", "music", "musica", "música", "videos", "vídeos",
})
_ARRANQUE_HOME: Final = frozenset({".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
                                   ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".xprofile", ".xinitrc",
                                   ".xsession", ".pam_environment", ".config/autostart",
                                   ".config/systemd", ".config/environment.d"})
_CREDENCIALES_HOME: Final = frozenset({".ssh", ".gnupg", ".mozilla", ".password-store", ".aws", ".kube",
                                       ".docker", ".netrc", ".git-credentials", ".pgpass", ".pki",
                                       "ntuser.dat"})
_SISTEMA_WINDOWS: Final = frozenset({"windows", "program files", "program files (x86)", "programdata",
                                     "$recycle.bin", "system volume information", "boot", "recovery",
                                     "perflogs", "windows.old", "$windows.~bt", "$windows.~ws", "efi",
                                     "pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr"})
_SISTEMA_POSIX: Final = frozenset({"etc", "usr", "boot", "dev", "bin", "sbin", "lib", "lib32", "lib64", "libx32",
                                   "root", "efi"})
_VARIABLES_SISTEMA: Final = frozenset({"windir", "systemroot", "programfiles", "programfiles(x86)",
                                       "programw6432", "commonprogramfiles", "commonprogramfiles(x86)",
                                       "commonprogramw6432", "programdata", "allusersprofile", "systemdrive",
                                       "driverdata", "comspec", "psmodulepath"})
_VARIABLES_HOME: Final = frozenset({"userprofile", "home"})
_VARIABLES_APPDATA: Final = frozenset({"appdata", "localappdata", "xdg_config_home", "xdg_data_home",
                                       "xdg_state_home", "xdg_cache_home"})
_VARIABLES_TRABAJO: Final = frozenset({"temp", "tmp", "tmpdir", "pwd", "psscriptroot"})
# SpecialFolder de .NET (GetFolderPath / [Environment+SpecialFolder]::X) → clase de ruta.
_CARPETAS_ESPECIALES: Final[Mapping[str, str]] = {
    "mydocuments": "usuario", "personal": "usuario", "desktop": "usuario", "desktopdirectory": "usuario",
    "mypictures": "usuario", "mymusic": "usuario", "myvideos": "usuario",
    "applicationdata": "appdata", "localapplicationdata": "appdata", "internetcache": "appdata",
    "history": "appdata", "recent": "appdata", "sendto": "appdata", "templates": "appdata",
    "startmenu": "appdata", "programs": "appdata", "cdburning": "appdata", "networkshortcuts": "appdata",
    "printershortcuts": "appdata", "cookies": "ruta_credenciales",
    "startup": "arranque", "commonstartup": "arranque",
    "commonapplicationdata": "ruta_sistema", "commonprograms": "ruta_sistema", "commonstartmenu": "ruta_sistema",
    "windows": "ruta_sistema", "system": "ruta_sistema", "systemx86": "ruta_sistema",
    "programfiles": "ruta_sistema", "programfilesx86": "ruta_sistema", "commonprogramfiles": "ruta_sistema",
    "commonprogramfilesx86": "ruta_sistema", "fonts": "ruta_sistema", "resources": "ruta_sistema",
    "localizedresources": "ruta_sistema", "admintools": "ruta_sistema", "commonadmintools": "ruta_sistema",
    "commontemplates": "ruta_sistema",
}
_RE_SEGMENTO_CREDENCIALES: Final = re.compile(
    r"(?:^|/)(?:user data|login data|local state|web data|cookies|\.ssh|\.gnupg|\.?mozilla|google-chrome"
    r"|chromium|bravesoftware|microsoft-edge|\.password-store|keyrings|\.aws|\.kube|\.docker"
    r"|microsoft/credentials|microsoft/protect|microsoft/vault|ntuser\.dat|system32/config"
    r"|etc/g?shadow|etc/sudoers(?:\.d)?)(?:/|$)")
_RE_PREFIJO_VARIABLE: Final = re.compile(
    r"^(?:\$env:(?P<ps>[a-z_][\w]*(?:\(x86\))?)|\$\{env:(?P<psl>[^}]+)\}|%(?P<cmd>[a-z_][\w]{2,}(?:\(x86\))?)%"
    r"|\$\{?(?P<sh>home|xdg_[a-z_]+|tmpdir|pwd|psscriptroot)\}?(?![\w])|(?P<tilde>~))")
# Interruptores de Windows («/s», «/query», «/t:5») que no son rutas (fuera de bash).
_RE_INTERRUPTOR: Final = re.compile(r"^/[a-z?][\w?-]{0,15}(?::\S*)?$", re.IGNORECASE)
_RAICES_POSIX_CONOCIDAS: Final = frozenset({"etc", "usr", "boot", "dev", "bin", "sbin", "lib", "lib32", "lib64",
                                            "libx32", "root", "efi", "home", "tmp", "mnt", "media", "run", "var",
                                            "opt", "srv", "proc", "sys", "snap"})
_DISPOSITIVOS_INOCUOS: Final = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/zero",
                                          "/dev/random", "/dev/urandom", "/dev/tty"})
_RE_RAIZ_UNIDAD: Final = re.compile(r"^[a-z]:/?$")
_RE_RUTA_ABSOLUTA_WINDOWS: Final = re.compile(r"^[a-z]:/")
_RE_PADRE: Final = re.compile(r"(?:^|/)\.\.(?:/|$)")


class _CtxRutas:
    """Carpetas permitidas y protegidas, normalizadas para comparar prefijos."""

    def __init__(self, carpetas: Iterable[Path | str | None], protegidas: Iterable[Path | str | None]) -> None:
        self.permitidas = [_normalizar(str(c)) for c in carpetas if c is not None and str(c).strip()]
        self.protegidas = [_normalizar(str(c)) for c in protegidas if c is not None and str(c).strip()]
        # Carpetas permitidas DENTRO de una protegida (la de trabajo está bajo data/): son la excepción.
        self.trabajo = [c for c in self.permitidas if any(_bajo(c, p) for p in self.protegidas)]
        # El script cambia la carpeta actual a otra que no es la de trabajo (o a una desconocida):
        # ``os.chdir``, ``cd``/``pushd``, ``Set-Location``… Sus rutas relativas dejan de ser «trabajo».
        self.cwd_fuera = False


def _normalizar(ruta: str) -> str:
    """Minúsculas, ``/`` como separador, sin comillas ni ``/`` repetidas ni final (UNC: ``//``)."""
    texto = ruta.strip().strip("\"'`").replace("\\", "/").lower()
    unc = texto.startswith("//")
    texto = re.sub(r"/{2,}", "/", texto)
    if unc:
        texto = "/" + texto
    if len(texto) > 1 and texto.endswith("/") and not _RE_RAIZ_UNIDAD.match(texto):
        texto = texto.rstrip("/") or "/"
    return texto


def _es_relativa(ruta: str) -> bool:
    """Ruta relativa a la carpeta ACTUAL: ni absoluta, ni del perfil, ni de una variable de entorno."""
    t = _normalizar(ruta)
    return not (_RE_RUTA_ABSOLUTA_WINDOWS.match(t) or _RE_RAIZ_UNIDAD.match(t) or t.startswith("/")
                or _RE_PREFIJO_VARIABLE.match(t))


def _clase_relativa(clase: str, ruta: str, ctx: _CtxRutas) -> str:
    """Tras un cambio de carpeta fuera de la de trabajo, una ruta relativa ya no es «trabajo»: el destino
    pasa a ser desconocido (§9.4, fail-closed)."""
    if clase == "trabajo" and ctx.cwd_fuera and _es_relativa(ruta):
        return "desconocido"
    return clase


def _bajo(ruta: str, base: str) -> bool:
    base = base.rstrip("/")
    return ruta == base or ruta.startswith(base + "/")


def _parece_ruta(texto: str) -> bool:
    """Texto que se interpreta como ruta con riesgo propio (absoluta, del perfil, de entorno o con «..»)."""
    crudo = texto.strip().strip("\"'`")
    if crudo.startswith("\\") and not re.match(r"^\\\\[\w.$-]+\\", crudo):
        return False   # "\s+", "\d": expresiones regulares o escapes, no rutas
    t = _normalizar(texto)
    if not t or len(t) > 1024:
        return False
    if _RE_RUTA_ABSOLUTA_WINDOWS.match(t) or _RE_RAIZ_UNIDAD.match(t) or t.startswith("//"):
        return True
    if re.match(r"^/[\w.$~@+-]", t):
        return True
    if _RE_PREFIJO_VARIABLE.match(t):
        return True
    return bool(_RE_PADRE.search(t))


def _clasificar_home(resto: str) -> str:
    """Clase de una ruta bajo el perfil del usuario (``resto`` = lo que va después de ``~/``)."""
    segmentos = [s for s in resto.split("/") if s and s != "."]
    if not segmentos:
        return "usuario"   # la raíz del perfil sola: base para Join-Path / Path.home() / "Downloads"
    primero = segmentos[0]
    dos = "/".join(segmentos[:2])
    if primero in _ARRANQUE_HOME or dos in _ARRANQUE_HOME:
        return "arranque"
    if primero in _CREDENCIALES_HOME:
        return "ruta_credenciales"
    if primero == "..":
        return "ruta_externa"
    if primero in (".config", ".local", "appdata", ".cache"):
        return "appdata"
    if primero in _NOMBRES_CARPETAS_USUARIO:
        return "usuario"
    if primero.startswith("onedrive") and len(segmentos) > 1 and segmentos[1] in _NOMBRES_CARPETAS_USUARIO:
        return "usuario"
    return "ruta_externa"


def clasificar_ruta(texto: str, ctx: _CtxRutas) -> str:
    """Clase de una ruta: ``"trabajo"`` (relativa, en la carpeta de trabajo), ``"usuario"`` (carpetas
    permitidas) o la clave de una fila de riesgo (``archivo_sensible``, ``ruta_credenciales``,
    ``ruta_proyecto``, ``appdata``, ``arranque``, ``ruta_sistema``, ``ruta_externa``, ``ruta_padre``)."""
    t = _normalizar(texto)
    if not t or t in _DISPOSITIVOS_INOCUOS or t.startswith("/dev/fd/"):
        return "trabajo"
    if _RE_ARCHIVO_SENSIBLE.search(t):
        return "archivo_sensible"
    if _RE_SEGMENTO_CREDENCIALES.search(t) and (t.startswith(("/", "~", "$", "%")) or ":" in t[:3]
                                                 or "/" in t):
        return "ruta_credenciales"
    if re.search(r"currentversion/run(?:once)?(?:/|$)|start menu/programs/startup|shell:(?:common )?startup", t):
        return "arranque"
    variable = _RE_PREFIJO_VARIABLE.match(t)
    if variable is not None:
        nombre = next(g for g in (variable.group("ps"), variable.group("psl"), variable.group("cmd"),
                                  variable.group("sh"), variable.group("tilde")) if g is not None)
        resto = t[variable.end():].lstrip("/")
        if nombre == "~" or nombre in _VARIABLES_HOME:
            return _clasificar_home(resto)
        if nombre == "onedrive":
            return _clasificar_home("onedrive/" + resto)
        if nombre in _VARIABLES_APPDATA:
            return "appdata"
        if nombre in _VARIABLES_TRABAJO:
            return "ruta_padre" if _RE_PADRE.search(resto) else "trabajo"
        if nombre in _VARIABLES_SISTEMA:
            return "ruta_sistema"
        if nombre == "xdg_runtime_dir":
            return "ruta_credenciales"
        return "ruta_externa"
    absoluta = bool(_RE_RUTA_ABSOLUTA_WINDOWS.match(t) or _RE_RAIZ_UNIDAD.match(t) or t.startswith("/"))
    if not absoluta:
        return "ruta_padre" if _RE_PADRE.search(t) else "trabajo"
    if t.startswith("//"):
        return "ruta_externa"           # UNC: red
    if _RE_RAIZ_UNIDAD.match(t) or t == "/":
        return "ruta_sistema"
    segmentos = [s for s in (t[3:] if _RE_RUTA_ABSOLUTA_WINDOWS.match(t) else t[1:]).split("/") if s]
    if not segmentos:
        return "ruta_sistema"
    if _RE_PADRE.search(t):
        return "ruta_externa"
    # Bajo una carpeta protegida (base_dir, data_dir) y no en la de trabajo: crítico.
    if any(_bajo(t, p) for p in ctx.protegidas) and not any(_bajo(t, c) for c in ctx.trabajo):
        return "ruta_proyecto"
    if any(_bajo(t, c) for c in ctx.permitidas):
        return "usuario"
    windows = bool(_RE_RUTA_ABSOLUTA_WINDOWS.match(t))
    if windows and segmentos[0] in _SISTEMA_WINDOWS:
        return "ruta_sistema"
    if not windows and segmentos[0] in _SISTEMA_POSIX:
        return "ruta_sistema"
    if not windows and segmentos[0] == "run" and len(segmentos) > 1 and segmentos[1] == "user":
        return "ruta_credenciales"   # XDG_RUNTIME_DIR: tokens y sockets de la sesión
    perfil = None
    if windows and segmentos[0] == "users" and len(segmentos) > 1:
        perfil = segmentos[1]
    elif not windows and segmentos[0] == "home" and len(segmentos) > 1:
        perfil = segmentos[1]
    if perfil is not None and perfil not in ("public", "default", "all users", "default user"):
        return _clasificar_home("/".join(segmentos[2:]))
    return "ruta_externa"


_CLASES_CON_RIESGO: Final = frozenset({"archivo_sensible", "ruta_credenciales", "ruta_proyecto", "appdata",
                                       "arranque", "ruta_sistema", "ruta_externa", "ruta_padre"})


def _es_raiz_de_borrado(texto: str) -> bool:
    """Objetivo de un borrado recursivo que arrasa demasiado: raíz, perfil, variable, comodín…"""
    t = _normalizar(texto)
    if not t:
        return False
    if t in ("/", "/*", "*", "*.*", ".*", "~", "~/*", "~/", ".", "./*", "./", "..", "../*"):
        return True
    if _RE_RAIZ_UNIDAD.match(t) or re.match(r"^[a-z]:/\*(?:\.\*)?$", t):
        return True
    if re.match(r"^\$env:[\w()]+(?:/\*?)?$", t) or re.match(r"^%[\w()]+%(?:/\*?)?$", t):
        return True
    if re.match(r"^\$\{?(?:home|userprofile)\}?(?:/\*?)?$", t):
        return True
    if re.match(r"^/[^/]+/?\*?$", t):            # /home, /etc/*…: primer nivel del sistema
        return True
    if re.match(r"^[a-z]:/users/?(?:[^/]+/?)?\*?$", t) or re.match(r"^/home/(?:[^/]+/?)?\*?$", t):
        return True
    return False


# Nombres de credenciales para la regla «datos de aplicaciones + credenciales» (§9.4).
_RE_NOMBRES_CREDENCIALES: Final = re.compile(
    r"user data|login data|local state|web data|cookies|\brams\b|escritorio\.json|key[34]\.db|logins\.json"
    r"|credential|\bvault\b|keyring|wallet|keychain|\.ssh\b|id_rsa|id_ed25519|password-store",
    re.IGNORECASE)
_RE_BASE64_LARGO: Final = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_RE_HKLM: Final = re.compile(r"\bhklm:|hkey_local_machine|registry::hklm\b|\bhklm\\", re.IGNORECASE)
_RE_HKCU: Final = re.compile(r"\bhkcu:|hkey_current_user|registry::hkcu\b|\bhkcu\\", re.IGNORECASE)
_RE_RUN: Final = re.compile(r"currentversion[\\/]+run(?:once)?\b", re.IGNORECASE)
_RE_INICIO: Final = re.compile(
    r"start menu[\\/]+programs[\\/]+startup|shell:(?:common )?startup|specialfolder\]::(?:common)?startup\b"
    r"|getfolderpath\(\s*(?:\[[\w.+]*\]::)?['\"]?(?:common)?startup\b", re.IGNORECASE)
_RE_CARPETA_ESPECIAL: Final = re.compile(
    r"(?:getfolderpath\s*\(\s*(?:\[[\w.+]*\]::)?['\"]?|specialfolder\]::)(\w+)", re.IGNORECASE)


def _escaneo_comun(texto: str, colector: _Colector, *, sin_lineas: bool = False) -> None:
    """Reglas de texto comunes (registro, arranque, base64 largo), línea a línea."""
    for numero, linea in enumerate(texto.split("\n"), start=1):
        destino = None if sin_lineas else numero
        if _RE_RUN.search(linea) or _RE_INICIO.search(linea):
            colector.agregar("arranque", destino, lenguaje="*")
        if _RE_HKLM.search(linea):
            colector.agregar("hklm", destino, lenguaje="*")
        elif _RE_HKCU.search(linea) and not _RE_RUN.search(linea):
            colector.agregar("hkcu", destino, lenguaje="*")
        if _RE_BASE64_LARGO.search(linea):
            colector.agregar("base64_largo", destino, lenguaje="*")
        for especial in _RE_CARPETA_ESPECIAL.finditer(linea):
            clase = _CARPETAS_ESPECIALES.get(especial.group(1).lower(), "ruta_externa")
            if clase in _CLASES_CON_RIESGO:
                colector.agregar(clase, destino, lenguaje="*")


def _cierre_credenciales(texto: str, colector: _Colector) -> None:
    """Datos de aplicaciones + un nombre de credenciales en el script → crítico (§9.4)."""
    if colector.tiene("appdata") and _RE_NOMBRES_CREDENCIALES.search(texto):
        colector.agregar("appdata+credenciales", colector.lineas.get("appdata"), lenguaje="*")


def _registrar_ruta(texto: str, linea: int | None, ctx: _CtxRutas, colector: _Colector) -> str:
    """Clasifica ``texto`` (si parece una ruta o un archivo sensible) y añade su hallazgo. Devuelve la clase."""
    if not texto or len(texto) > 2048:
        return "trabajo"
    normal = _normalizar(texto)
    if colector.lenguaje != "bash" and _RE_INTERRUPTOR.match(texto.strip()) \
            and normal[1:].split(":", 1)[0] not in _RAICES_POSIX_CONOCIDAS:
        return "trabajo"   # interruptor de un ejecutable de Windows («/s», «/all»), no una ruta
    if _RE_ARCHIVO_SENSIBLE.search(normal):
        colector.agregar("archivo_sensible", linea, lenguaje="*")
        return "archivo_sensible"
    if not _parece_ruta(texto) and not _RE_SEGMENTO_CREDENCIALES.search(normal):
        return "trabajo"
    clase = clasificar_ruta(texto, ctx)
    if clase in _CLASES_CON_RIESGO:
        colector.agregar(clase, linea, lenguaje="*")
    return clase


# ---------------------------------------------------------------------------
# Análisis: comandos externos compartidos (subprocess, cmd /c, Start-Process…)
# ---------------------------------------------------------------------------

_GESTORES_PAQUETES: Final = frozenset({"pip", "pip3", "pipx", "npm", "pnpm", "yarn", "winget", "choco", "scoop",
                                       "apt", "apt-get", "pacman", "yay", "paru", "dnf", "yum", "zypper",
                                       "gem", "cargo", "conda", "uv"})
_ORDENES_INSTALAR: Final = frozenset({"install", "add", "i", "-s", "-sy", "-syu", "-u", "upgrade"})


def _nombre_comando(texto: str) -> str:
    """Nombre base en minúsculas de un comando (``C:\\x\\Shutdown.EXE`` → ``shutdown``; ``sc.exe`` se
    conserva porque ``sc`` es ``Set-Content`` en PowerShell)."""
    base = texto.strip().strip("\"'`").replace("\\", "/").rsplit("/", 1)[-1].lower().lstrip("\\")
    if base == "sc.exe":
        return base
    for extension in (".exe", ".com"):
        if base.endswith(extension):
            return base[: -len(extension)]
    return base


def _instala_paquetes(tokens: Sequence[str]) -> bool:
    nombres = [_nombre_comando(t) for t in tokens[:6]]
    if not nombres:
        return False
    if nombres[0] in _GESTORES_PAQUETES and any(n in _ORDENES_INSTALAR for n in nombres[1:]):
        return True
    # python -m pip install …
    return "-m" in nombres and "pip" in nombres and "install" in nombres


def _comando_suelto(tokens: Sequence[str], linea: int | None, ctx: _CtxRutas, colector: _Colector) -> None:
    """Analiza un comando externo dado como lista de palabras (subprocess, cmd /c, Start-Process…), con
    las reglas de PowerShell **y** de bash (fail-closed: vale la más grave)."""
    tokens = [t for t in tokens if isinstance(t, str) and t.strip()]
    if not tokens:
        return
    if _instala_paquetes(tokens):
        colector.agregar("instalar", linea, lenguaje="*")
    nombre = _nombre_comando(tokens[0])
    antes = colector.contador
    _clasificar_comando_bash(nombre, tokens[1:], linea, ctx, colector, texto_linea=" ".join(tokens),
                             funciones=frozenset(), anidado=True)
    if colector.contador == antes:   # bash no lo reconoció: reglas de los ejecutables de Windows
        _clasificar_externo_ps(nombre, [t.lower() for t in tokens[1:]], tokens[1:], linea, ctx, colector)
    for token in tokens[1:]:
        _registrar_ruta(token, linea, ctx, colector)


# ---------------------------------------------------------------------------
# Análisis de Python (ast)
# ---------------------------------------------------------------------------

_PREFIJOS_PY: Final = tuple(f.patron[:-1] for f in CATALOGO_RIESGO
                            if f.lenguaje == "python" and f.patron.endswith("*"))
_CONSTRUCTORES_RUTA: Final = frozenset({"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath",
                                        "pathlib.WindowsPath", "pathlib.PurePosixPath",
                                        "pathlib.PureWindowsPath", "os.path.join", "os.path.expanduser",
                                        "os.path.expandvars", "os.path.abspath", "os.path.normpath",
                                        "os.path.realpath", "os.fspath", "str", "posixpath.join",
                                        "ntpath.join"})
_METODOS_RUTA: Final[Mapping[str, str]] = {
    "unlink": "pathlib.Path.unlink", "rmdir": "pathlib.Path.rmdir", "write_text": "pathlib.Path.write_text",
    "write_bytes": "pathlib.Path.write_bytes", "touch": "pathlib.Path.touch", "mkdir": "pathlib.Path.mkdir",
    "rename": "pathlib.Path.rename", "glob": "pathlib.Path.glob", "rglob": "pathlib.Path.rglob",
    "iterdir": "pathlib.Path.iterdir", "read_text": "pathlib.Path.read_text",
    "read_bytes": "pathlib.Path.read_bytes", "chmod": "pathlib.Path.chmod",
}
_LECTURA_PY: Final = frozenset({"os.listdir", "os.scandir", "os.walk", "glob.glob", "glob.iglob",
                                "pathlib.Path.glob", "pathlib.Path.rglob", "pathlib.Path.iterdir",
                                "pathlib.Path.read_text", "pathlib.Path.read_bytes"})
_ESCRITURA_PY: Final = frozenset({"os.makedirs", "os.mkdir", "pathlib.Path.write_text",
                                  "pathlib.Path.write_bytes", "pathlib.Path.touch", "pathlib.Path.mkdir",
                                  "shutil.make_archive", "shutil.unpack_archive"})
_CREAR_PY: Final = frozenset({"os.makedirs", "os.mkdir", "pathlib.Path.mkdir", "pathlib.Path.touch"})
_COPIA_PY: Final = frozenset({"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree"})
_MOVER_PY: Final = frozenset({"shutil.move", "os.rename", "os.renames", "os.replace", "pathlib.Path.rename",
                              "pathlib.Path.replace"})
_BORRADO_PY: Final = frozenset({"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree",
                                "pathlib.Path.unlink", "pathlib.Path.rmdir", "send2trash.send2trash"})
_ABRIR_PY: Final = frozenset({"open", "io.open", "codecs.open", "builtins.open", "pathlib.Path.open"})
_DECODIFICADORES_PY: Final = frozenset({"base64.b64decode", "base64.decodebytes", "base64.b32decode",
                                        "base64.a85decode", "base64.b85decode", "base64.urlsafe_b64decode",
                                        "base64.standard_b64decode", "codecs.decode", "binascii.a2b_base64",
                                        "bytes.fromhex", "zlib.decompress"})
_DESCARGA_PY: Final = frozenset({"urllib.request.urlopen", "urllib.request.urlretrieve", "requests.get",
                                 "requests.post", "requests.request", "httpx.get", "httpx.post",
                                 "httpx.request", "http.client.HTTPConnection", "http.client.HTTPSConnection"})
_EJECUTORES_PY: Final = frozenset({"eval", "exec", "compile", "builtins.eval", "builtins.exec",
                                   "builtins.compile"})
_ENV_PY: Final = frozenset({"os.environ.get", "os.getenv", "os.environb.get"})
_RAICES_DINAMICAS: Final = frozenset({"globals", "locals", "vars", "__builtins__", "builtins.globals",
                                      "builtins.vars", "builtins.locals"})
_MODOS_ESCRITURA: Final = re.compile(r"[wax+]")
# Módulos raíz con filas en el catálogo (``os``, ``shutil``, ``pickle``, ``ctypes``…) más los que dan
# acceso a otros módulos (``sys.modules``, ``builtins``, ``importlib``): sirven para reconocer un módulo
# reexportado por otro (``shutil.os.remove``, ``importlib.sys.modules``, ``tempfile._os``).
_RAICES_MODULOS_PY: Final = frozenset(
    {f.patron.split(".")[0] for f in CATALOGO_RIESGO
     if f.lenguaje == "python" and "." in f.patron and "+" not in f.patron and not f.patron.startswith("import:")}
    | {f.patron[len("import:"):].split(".")[0] for f in CATALOGO_RIESGO
       if f.lenguaje == "python" and f.patron.startswith("import:")}
    | {"sys", "builtins", "importlib"})
# Funciones sueltas del catálogo que, usadas como VALOR (``f = eval``; ``map(exec, …)``), se cuentan.
_REFERENCIAS_PY: Final = frozenset({"eval", "exec", "compile", "getattr", "setattr", "delattr", "__import__"})
_LECTURA_SYS_MODULES: Final = frozenset({"sys.modules.get", "sys.modules.pop", "sys.modules.setdefault"})
_CAMBIO_CARPETA_PY: Final = frozenset({"os.chdir", "os.fchdir"})
# Prefijos con puntos de las filas de Python («os», «urllib.request», «pathlib.Path»…).
_PREFIJOS_MODULO_PY: Final = frozenset(
    ".".join(f.patron.split(".")[:i]) for f in CATALOGO_RIESGO
    if f.lenguaje == "python" and "+" not in f.patron and not f.patron.startswith("import:")
    for i in range(1, f.patron.count(".") + 1))


class _AnalizadorPython:
    """Recorre el AST de un script Python (sin ejecutarlo) y llena el colector."""

    def __init__(self, arbol: ast.AST, ctx: _CtxRutas, colector: _Colector) -> None:
        self.arbol = arbol
        self.ctx = ctx
        self.col = colector
        self.alias: dict[str, str] = {}
        self.modulos: set[str] = set()
        self.asignaciones: dict[str, list[ast.expr]] = {}
        self.externo = False
        self._evaluando: set[str] = set()
        self.decodifica = False
        self.ejecuta = False
        self.descarga = False
        self.sqlite = False
        self._raices_import: frozenset[str] = frozenset()
        self._padres: dict[int, ast.AST] = {}

    # --- pasada 1: imports y asignaciones simples ---
    def _primera_pasada(self) -> None:
        for nodo in ast.walk(self.arbol):
            if isinstance(nodo, ast.Import):
                for nombre in nodo.names:
                    self.modulos.add(nombre.name)
                    if nombre.asname:
                        self.alias[nombre.asname] = nombre.name
                    else:
                        raiz = nombre.name.split(".")[0]
                        self.alias.setdefault(raiz, raiz)
                    self._import(nombre.name, nodo.lineno)
            elif isinstance(nodo, ast.ImportFrom):
                modulo = nodo.module or ""
                if nodo.level:          # import relativo: no hay paquete propio en la carpeta de trabajo
                    self.col.agregar("llamada", nodo.lineno)
                self.modulos.add(modulo)
                self._import(modulo, nodo.lineno)
                for nombre in nodo.names:
                    if nombre.name == "*":
                        self.col.agregar("llamada", nodo.lineno)
                        continue
                    calificado = f"{modulo}.{nombre.name}" if modulo else nombre.name
                    self.alias[nombre.asname or nombre.name] = calificado
                    self._import(calificado, nodo.lineno)
            elif isinstance(nodo, ast.Assign) and len(nodo.targets) == 1 and isinstance(nodo.targets[0], ast.Name):
                self.asignaciones.setdefault(nodo.targets[0].id, []).append(nodo.value)
            elif isinstance(nodo, (ast.AnnAssign,)) and isinstance(nodo.target, ast.Name) and nodo.value is not None:
                self.asignaciones.setdefault(nodo.target.id, []).append(nodo.value)
            elif isinstance(nodo, ast.NamedExpr) and isinstance(nodo.target, ast.Name):
                self.asignaciones.setdefault(nodo.target.id, []).append(nodo.value)
        self._raices_import = frozenset(m.split(".")[0] for m in self.modulos if m)
        # Alias por asignación: borrar = os.remove
        for nombre, valores in self.asignaciones.items():
            if len(valores) == 1 and isinstance(valores[0], (ast.Name, ast.Attribute)) and nombre not in self.alias:
                calificado, _ = self._calificado(valores[0])
                if calificado and calificado.split(".")[0] in {m.split(".")[0] for m in self.modulos}:
                    self.alias[nombre] = calificado

    def _import(self, modulo: str, linea: int) -> None:
        candidatos = [modulo]
        partes = modulo.split(".")
        candidatos.extend(".".join(partes[:i]) for i in range(1, len(partes)))
        for candidato in candidatos:
            if ("python", f"import:{candidato}") in _FILAS:
                self.col.agregar(f"import:{candidato}", linea)
        if partes[0] == "sqlite3":
            self.sqlite = True

    # --- nombres calificados ---
    def _calificado(self, nodo: ast.AST) -> tuple[str | None, bool]:
        """``(nombre calificado, dinámico)`` del objeto llamado. ``".metodo"`` = método sobre un valor."""
        partes: list[str] = []
        actual = nodo
        while isinstance(actual, ast.Attribute):
            partes.append(actual.attr)
            actual = actual.value
        partes.reverse()
        if isinstance(actual, ast.Name):
            if actual.id == "__builtins__":
                return "__builtins__." + ".".join(partes) if partes else "__builtins__", True
            raiz = self.alias.get(actual.id, actual.id)
            nombre = self._sin_reexport(".".join([raiz, *partes]))
            # sys.modules da cualquier módulo por su nombre: lo que se saque de ahí es dinámico.
            return nombre, nombre == "sys.modules" or nombre.startswith("sys.modules.")
        if isinstance(actual, ast.Call):
            funcion, _ = self._calificado(actual.func) if isinstance(actual.func, (ast.Name, ast.Attribute)) \
                else (None, True)
            constante = (actual.args[0].value if actual.args and isinstance(actual.args[0], ast.Constant)
                         and isinstance(actual.args[0].value, str) else None)
            if funcion in ("__import__", "builtins.__import__", "importlib.import_module") \
                    or funcion in _LECTURA_SYS_MODULES:
                if constante is None or not constante.strip():
                    return None, True
                self._import(constante, getattr(actual, "lineno", 0))
                return self._sin_reexport(".".join([constante, *partes])), True
            if funcion in ("getattr", "builtins.getattr") and len(actual.args) >= 2:
                atributo = actual.args[1]
                if isinstance(atributo, ast.Constant) and isinstance(atributo.value, str):
                    base, dinamico = self._calificado(actual.args[0])
                    if base:
                        return ".".join([base, atributo.value, *partes]), dinamico
                    return (("." + partes[-1]) if partes else None), dinamico
                return None, True
            if funcion in _RAICES_DINAMICAS:
                return None, True
            if partes:
                return "." + partes[-1], False          # método sobre el resultado de otra llamada
            return None, True                            # f()(): no resoluble
        if isinstance(actual, ast.Subscript):
            base, _ = self._calificado(actual.value)
            if base == "sys.modules":                    # sys.modules["os"].remove: os.remove, dinámico
                clave = actual.slice
                if isinstance(clave, ast.Constant) and isinstance(clave.value, str) and clave.value.strip():
                    self._import(clave.value, getattr(actual, "lineno", 0))
                    return self._sin_reexport(".".join([clave.value, *partes])), True
                return None, True
            if partes:
                if base in _RAICES_DINAMICAS or (base and base.endswith("__dict__")):
                    return None, True
                return "." + partes[-1], False
            return None, True                            # tabla[k](): no resoluble
        if partes:
            return "." + partes[-1], False               # "texto".join(…), (a or b).x(…)
        if isinstance(actual, ast.Lambda):
            return None, False
        return None, True

    def _tiene_fila(self, nombre: str) -> bool:
        return ("python", nombre) in _FILAS or nombre.startswith(_PREFIJOS_PY)

    def _sin_reexport(self, calificado: str) -> str:
        """Un módulo del catálogo alcanzado a través de otro (``shutil.os.remove`` → ``os.remove``;
        ``importlib.sys.modules`` → ``sys.modules``; ``tempfile._os`` → ``os``)."""
        if self._tiene_fila(calificado) or calificado == "sys.modules" or calificado.startswith("sys.modules."):
            return calificado
        partes = calificado.split(".")
        if len(partes) < 2 or (partes[0] not in self._raices_import and partes[0] not in _RAICES_MODULOS_PY):
            return calificado
        for indice in range(1, len(partes)):
            modulo = partes[indice].lstrip("_")
            if modulo not in _RAICES_MODULOS_PY or modulo == partes[0]:
                continue
            candidato = ".".join([modulo, *partes[indice + 1:]])
            if indice == len(partes) - 1 or self._tiene_fila(candidato) or candidato.startswith("sys.modules"):
                return candidato
        return calificado

    def _es_modulo(self, nombre: str) -> bool:
        """¿``nombre`` es un módulo (importado, o con filas del catálogo debajo)? Las clases de rutas
        (``Path``) no cuentan: sus métodos peligrosos se reconocen por el nombre (``x.unlink()``)."""
        if nombre in _CONSTRUCTORES_RUTA:
            return False
        return nombre in self.modulos or nombre in _RAICES_MODULOS_PY or nombre in _PREFIJOS_MODULO_PY

    def _alias_resuelto(self, nodo: ast.AST, padre: ast.AST | None, calificado: str) -> bool:
        """``borrar = os.remove`` / ``m = os`` con un solo valor: el alias ya se sigue en las llamadas."""
        if isinstance(padre, ast.Assign) and padre.value is nodo and len(padre.targets) == 1:
            objetivo: ast.AST | None = padre.targets[0]
        elif isinstance(padre, (ast.AnnAssign, ast.NamedExpr)) and padre.value is nodo:
            objetivo = padre.target
        else:
            return False
        return isinstance(objetivo, ast.Name) and self.alias.get(objetivo.id) == calificado

    def _referencia(self, nodo: ast.Name | ast.Attribute, padre: ast.AST | None, linea: int | None) -> None:
        """Un módulo o una función del catálogo usados como VALOR (en una lista o un diccionario, como
        argumento, devueltos, como valor por defecto…), no llamados ni como base de un atributo. Desde
        ahí se pueden llamar con otro nombre que el análisis ya no sigue (``m = [os][0]; m.remove(…)``,
        ``def f(m): m.remove(…)``, ``map(os.remove, …)``): se cuentan aquí (fail-closed, §9.4)."""
        calificado, dinamico = self._calificado(nodo)
        if calificado is None or calificado.startswith("."):
            if calificado is None and dinamico:
                self.col.agregar("llamada", linea)
            return
        if calificado == "sys.modules" or calificado.startswith("sys.modules."):
            self.col.agregar("llamada", linea)
            return
        if self._alias_resuelto(nodo, padre, calificado):
            return
        if isinstance(padre, ast.Call) and len(padre.args) >= 2 and padre.args[0] is nodo:
            # getattr(sys, "frozen", False): lo que cuenta es el atributo, que sí se conoce.
            funcion_padre, _ = self._calificado(padre.func)
            atributo = padre.args[1]
            if funcion_padre in ("getattr", "hasattr", "builtins.getattr", "builtins.hasattr") \
                    and isinstance(atributo, ast.Constant) and isinstance(atributo.value, str):
                calificado = self._sin_reexport(f"{calificado}.{atributo.value}")
                if calificado == "sys.modules" or calificado.startswith("sys.modules."):
                    self.col.agregar("llamada", linea)
                    return
        if "." not in calificado:
            if calificado in _REFERENCIAS_PY:
                self.col.agregar(calificado, linea)
                if calificado in ("eval", "exec", "compile"):
                    self.ejecuta = True
            elif calificado in self.modulos:
                self.col.agregar("llamada", linea)
            return
        if ("python", calificado) in _FILAS:
            self.col.agregar(calificado, linea)
        elif calificado.startswith(_PREFIJOS_PY):
            self.col.agregar(next(p for p in _PREFIJOS_PY if calificado.startswith(p)) + "*", linea)
        elif self._es_modulo(calificado):
            self.col.agregar("llamada", linea)

    def _cambia_carpeta(self) -> bool:
        """¿El script cambia la carpeta actual (``os.chdir``) a otra que no es la de trabajo?"""
        for nodo in ast.walk(self.arbol):
            if not isinstance(nodo, ast.Call):
                continue
            funcion, _ = self._calificado(nodo.func)
            if funcion not in _CAMBIO_CARPETA_PY:
                continue
            if funcion == "os.fchdir":
                return True
            objetivo = nodo.args[0] if nodo.args else next((k.value for k in nodo.keywords if k.arg == "path"), None)
            ruta = self._ruta(objetivo)
            if ruta is None or clasificar_ruta(ruta, self.ctx) != "trabajo":
                return True
        return False

    # --- evaluación de rutas construidas ---
    def _ruta(self, nodo: ast.AST | None, profundidad: int = 0) -> str | None:
        """Ruta (simbólica) a la que evalúa una expresión, o ``None`` si no se puede saber."""
        if nodo is None or profundidad > 12:
            return None
        if isinstance(nodo, ast.Constant):
            return nodo.value if isinstance(nodo.value, str) else None
        if isinstance(nodo, ast.JoinedStr):
            partes = []
            for valor in nodo.values:
                if isinstance(valor, ast.Constant) and isinstance(valor.value, str):
                    partes.append(valor.value)
                elif isinstance(valor, ast.FormattedValue):
                    partes.append(self._ruta(valor.value, profundidad + 1) or "{x}")
            return "".join(partes)
        if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Div):
            izquierda = self._ruta(nodo.left, profundidad + 1)
            derecha = self._ruta(nodo.right, profundidad + 1)
            if izquierda is None or derecha is None:
                return None if derecha is None or not _parece_ruta(derecha) else derecha
            return _unir_rutas(izquierda, derecha)
        if isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Add):
            izquierda = self._ruta(nodo.left, profundidad + 1)
            derecha = self._ruta(nodo.right, profundidad + 1)
            if izquierda is not None and derecha is not None:
                return izquierda + derecha
            return None
        if isinstance(nodo, ast.Name):
            if nodo.id == "__file__":
                return "./script.py"
            valores = self.asignaciones.get(nodo.id, [])
            if len(valores) == 1 and nodo.id not in self._evaluando:
                self._evaluando.add(nodo.id)
                try:
                    return self._ruta(valores[0], profundidad + 1)
                finally:
                    self._evaluando.discard(nodo.id)
            return None
        if isinstance(nodo, ast.Subscript):
            base, _ = self._calificado(nodo.value)
            if base in ("os.environ", "os.environb"):
                clave = nodo.slice
                if isinstance(clave, ast.Constant) and isinstance(clave.value, str):
                    return _ruta_de_variable(clave.value)
            return None
        if isinstance(nodo, ast.Attribute):
            if nodo.attr == "parent":
                base = self._ruta(nodo.value, profundidad + 1)
                return None if base is None else _ruta_padre(base)
            return None
        if isinstance(nodo, ast.Call):
            funcion, _ = self._calificado(nodo.func)
            if funcion is None:
                return None
            if funcion in ("pathlib.Path.home", "os.path.expanduser") and not nodo.args:
                return "~"
            if funcion in ("pathlib.Path.home",):
                return "~"
            if funcion in ("pathlib.Path.cwd", "os.getcwd"):
                return "."
            if funcion in ("tempfile.gettempdir", "tempfile.mkdtemp"):
                return "%temp%"      # la carpeta temporal del script (dentro de la de trabajo)
            if funcion == "os.path.dirname":
                base = self._ruta(nodo.args[0], profundidad + 1) if nodo.args else None
                return None if base is None else _ruta_padre(base)
            if funcion in _ENV_PY:
                if nodo.args and isinstance(nodo.args[0], ast.Constant) and isinstance(nodo.args[0].value, str):
                    return _ruta_de_variable(nodo.args[0].value)
                return None
            if funcion in _CONSTRUCTORES_RUTA:
                if not nodo.args:
                    return "." if funcion.startswith("pathlib.") else None
                resultado: str | None = None
                for argumento in nodo.args:
                    valor = self._ruta(argumento, profundidad + 1)
                    if valor is None:
                        return None
                    resultado = valor if resultado is None else _unir_rutas(resultado, valor)
                return resultado
            if funcion.startswith(".") and isinstance(nodo.func, ast.Attribute):
                metodo = funcion[1:]
                base = self._ruta(nodo.func.value, profundidad + 1)
                if base is None:
                    return None
                if metodo == "joinpath":
                    resultado = base
                    for argumento in nodo.args:
                        valor = self._ruta(argumento, profundidad + 1)
                        if valor is None:
                            return None
                        resultado = _unir_rutas(resultado, valor)
                    return resultado
                if metodo in ("resolve", "absolute", "expanduser", "with_name", "with_suffix", "with_stem"):
                    return base
            if funcion.endswith((".joinpath",)) and isinstance(nodo.func, ast.Attribute):
                return None
        return None

    def _clase_destino(self, nodo: ast.AST | None, linea: int | None) -> str:
        """``trabajo`` | ``usuario`` | ``riesgo`` | ``desconocido`` para el destino de una operación."""
        ruta = self._ruta(nodo)
        if ruta is None:
            return "desconocido"
        clase = clasificar_ruta(ruta, self.ctx)
        if clase in _CLASES_CON_RIESGO:
            self.col.agregar(clase, linea, lenguaje="*")
            return "riesgo"
        return _clase_relativa(clase, ruta, self.ctx)

    def _escritura(self, nodo: ast.AST | None, linea: int | None, *, crear: bool = False) -> None:
        clase = self._clase_destino(nodo, linea)
        if clase == "trabajo" or (clase == "usuario" and crear):
            self.col.agregar("escritura+trabajo", linea, lenguaje="*")
        elif clase in ("usuario", "riesgo") or (clase == "desconocido" and self.externo):
            self.col.agregar("escritura+usuario", linea, lenguaje="*")
        else:
            self.col.agregar("escritura+trabajo", linea, lenguaje="*")

    def _lectura(self, nodo: ast.AST | None, linea: int | None) -> None:
        clase = self._clase_destino(nodo, linea)
        if clase in ("usuario", "riesgo") or (clase == "desconocido" and self.externo):
            self.col.agregar("lectura+usuario", linea, lenguaje="*")

    # --- pasada 2 ---
    def ejecutar(self) -> None:
        self._primera_pasada()
        self.externo = self._menciona_externo()
        if self._cambia_carpeta():
            self.ctx.cwd_fuera = True
            self.externo = True
        for nodo in ast.walk(self.arbol):
            for hijo in ast.iter_child_nodes(nodo):
                self._padres[id(hijo)] = nodo
        for nodo in ast.walk(self.arbol):
            linea = getattr(nodo, "lineno", None)
            if isinstance(nodo, (ast.Name, ast.Attribute)) and isinstance(nodo.ctx, ast.Load):
                padre = self._padres.get(id(nodo))
                cadena = isinstance(padre, ast.Attribute) and padre.value is nodo
                llamado = isinstance(padre, ast.Call) and padre.func is nodo
                if not cadena and not llamado:
                    self._referencia(nodo, padre, linea)
            if isinstance(nodo, ast.Call):
                self._llamada(nodo, linea)
            elif isinstance(nodo, ast.Subscript):
                self._subindice(nodo, linea)
            elif isinstance(nodo, ast.Name) and nodo.id == "__builtins__":
                self.col.agregar("__builtins__", linea)
            elif isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                self._cadena(nodo.value, linea)
            elif isinstance(nodo, ast.JoinedStr):
                ruta = self._ruta(nodo)
                if ruta:
                    _registrar_ruta(ruta, linea, self.ctx, self.col)
            elif isinstance(nodo, ast.BinOp) and isinstance(nodo.op, ast.Div):
                ruta = self._ruta(nodo)
                if ruta:
                    _registrar_ruta(ruta, linea, self.ctx, self.col)
            elif isinstance(nodo, ast.Attribute) and nodo.attr in ("HKEY_LOCAL_MACHINE", "HKEY_USERS"):
                self.col.agregar("winreg+HKEY_LOCAL_MACHINE", linea)
            elif isinstance(nodo, ast.Attribute) and nodo.attr == "HKEY_CURRENT_USER":
                self.col.agregar("hkcu", linea, lenguaje="*")
        self._combinaciones()

    def _menciona_externo(self) -> bool:
        """¿El script nombra algún sitio fuera de la carpeta de trabajo (perfil, entorno, absolutas…)?"""
        for nodo in ast.walk(self.arbol):
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                if _parece_ruta(nodo.value) and clasificar_ruta(nodo.value, self.ctx) != "trabajo":
                    return True
            elif isinstance(nodo, ast.Call):
                funcion, _ = self._calificado(nodo.func)
                if funcion in ("pathlib.Path.home", "os.path.expanduser", "os.path.expandvars", *_ENV_PY):
                    return True
            elif isinstance(nodo, ast.Attribute) and nodo.attr in ("environ", "environb"):
                return True
        return False

    def _cadena(self, valor: str, linea: int | None) -> None:
        if len(valor) > 4096:
            if _RE_BASE64_LARGO.search(valor):
                self.col.agregar("base64_largo", linea, lenguaje="*")
            return
        _registrar_ruta(valor, linea, self.ctx, self.col)
        if _RE_BASE64_LARGO.search(valor):
            self.col.agregar("base64_largo", linea, lenguaje="*")
        if _RE_RUN.search(valor) or _RE_INICIO.search(valor):
            self.col.agregar("arranque", linea, lenguaje="*")
        if _RE_HKLM.search(valor):
            self.col.agregar("winreg+HKEY_LOCAL_MACHINE", linea)
        elif _RE_HKCU.search(valor):
            self.col.agregar("hkcu", linea, lenguaje="*")
        if self.sqlite and re.search(r"login data|cookies|history|places\.sqlite|cookies\.sqlite|web data",
                                     valor, re.IGNORECASE):
            self.col.agregar("sqlite3+navegador", linea)

    def _subindice(self, nodo: ast.Subscript, linea: int | None) -> None:
        base, _ = self._calificado(nodo.value)
        if base in ("os.environ", "os.environb"):
            clave = nodo.slice
            if isinstance(clave, ast.Constant) and isinstance(clave.value, str):
                clase = clasificar_ruta(_ruta_de_variable(clave.value), self.ctx)
                if clase in ("appdata", "ruta_sistema", "ruta_credenciales"):
                    self.col.agregar(clase, linea, lenguaje="*")
        elif isinstance(nodo.value, ast.Call):
            funcion, _ = self._calificado(nodo.value.func)
            if funcion in _RAICES_DINAMICAS:
                self.col.agregar("llamada", linea)

    def _llamada(self, nodo: ast.Call, linea: int | None) -> None:
        funcion, dinamico = self._calificado(nodo.func)
        if dinamico:
            self.col.agregar("llamada", linea)
        if funcion is None:
            return
        argumentos = list(nodo.args)
        claves = {k.arg: k.value for k in nodo.keywords if k.arg}
        if funcion in _CONSTRUCTORES_RUTA or funcion in ("pathlib.Path.home", ".joinpath"):
            ruta = self._ruta(nodo)      # ruta construida: os.path.join(expanduser("~"), ".config"…)
            if ruta:
                _registrar_ruta(ruta, linea, self.ctx, self.col)
        if funcion.startswith("."):
            self._metodo(funcion[1:], nodo, linea)
            return
        raiz = funcion.split(".")[0]
        es_modulo = raiz in {m.split(".")[0] for m in self.modulos if m} or raiz in ("os", "shutil", "pathlib")
        ultimo = funcion.rsplit(".", 1)[-1]
        if not es_modulo and "." in funcion and ultimo in _METODOS_RUTA:
            self._metodo(ultimo, nodo, linea)
            return
        if not es_modulo and "." in funcion and ultimo in ("kill", "terminate") and "psutil" in self.modulos:
            self.col.agregar("psutil.Process.kill", linea)
            return
        # Ejecución dinámica, decodificación y descarga (para las combinaciones).
        if funcion in _EJECUTORES_PY:
            self.ejecuta = True
            self.col.agregar(funcion.split(".")[-1], linea)
            if any(self._contiene_decodificacion(a) for a in argumentos):
                self.col.agregar("exec+b64decode", linea)
            return
        if funcion in ("getattr", "setattr", "delattr", "builtins.getattr", "builtins.setattr", "builtins.delattr"):
            nombre = argumentos[1] if len(argumentos) >= 2 else None
            if not (isinstance(nombre, ast.Constant) and isinstance(nombre.value, str)):
                self.col.agregar(funcion.split(".")[-1], linea)
            return
        if funcion in ("__import__", "builtins.__import__", "importlib.import_module"):
            self.col.agregar("__import__" if "import_module" not in funcion else "importlib.import_module", linea)
            if argumentos and isinstance(argumentos[0], ast.Constant) and isinstance(argumentos[0].value, str):
                self._import(argumentos[0].value, linea or 0)
            return
        if funcion in _DECODIFICADORES_PY:
            self.decodifica = True
        if funcion in _DESCARGA_PY or funcion.startswith(("requests.", "httpx.", "urllib.request.")):
            self.descarga = True
        if "ShellExecute" in ultimo and any(
                isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.lower() == "runas"
                for a in [*argumentos, *claves.values()]):
            self.col.agregar("ShellExecute+runas", linea)
        if ultimo == "CryptUnprotectData":
            self.col.agregar("CryptUnprotectData", linea)
        # Filas directas del catálogo.
        if ("python", funcion) in _FILAS and funcion not in _ABRIR_PY and funcion not in _ESCRITURA_PY \
                and funcion not in _LECTURA_PY and funcion not in _MOVER_PY and funcion not in _COPIA_PY \
                and funcion not in _BORRADO_PY:
            self.col.agregar(funcion, linea)
        elif funcion.startswith(_PREFIJOS_PY):
            prefijo = next(p for p in _PREFIJOS_PY if funcion.startswith(p))
            self.col.agregar(prefijo + "*", linea)
        # Subprocesos: el comando anidado se analiza con las reglas de PowerShell y bash.
        if funcion.startswith(("subprocess.", "asyncio.create_subprocess")) or funcion in (
                "os.system", "os.popen", "os.startfile") or funcion.startswith(("os.exec", "os.spawn")):
            self.ejecuta = True
            tokens = self._tokens_comando(argumentos, funcion)
            if tokens:
                _comando_suelto(tokens, linea, self.ctx, self.col)
            return
        # Archivos.
        if funcion in _ABRIR_PY:
            modo = argumentos[1] if len(argumentos) >= 2 else claves.get("mode")
            if modo is None:
                self._lectura(argumentos[0] if argumentos else claves.get("file"), linea)
            elif isinstance(modo, ast.Constant) and isinstance(modo.value, str) \
                    and not _MODOS_ESCRITURA.search(modo.value):
                self._lectura(argumentos[0] if argumentos else claves.get("file"), linea)
            else:
                self._escritura(argumentos[0] if argumentos else claves.get("file"), linea)
            return
        if funcion in _BORRADO_PY:
            self._borrado(funcion, argumentos[0] if argumentos else claves.get("path"), linea)
            return
        if funcion in _LECTURA_PY:
            self._lectura(argumentos[0] if argumentos else None, linea)
            return
        if funcion in _ESCRITURA_PY:
            self._escritura(argumentos[0] if argumentos else claves.get("name"), linea, crear=funcion in _CREAR_PY)
            return
        if funcion in _COPIA_PY:
            if argumentos:
                self._lectura(argumentos[0], linea)
            self._escritura(argumentos[1] if len(argumentos) >= 2 else claves.get("dst"), linea)
            return
        if funcion in _MOVER_PY:
            self._mover([*argumentos[:2]], linea)
            return
        if funcion == "webbrowser.open" or funcion.startswith("webbrowser."):
            if argumentos:
                ruta = self._ruta(argumentos[0])
                if ruta and not re.match(r"^https?://", ruta, re.IGNORECASE):
                    _registrar_ruta(ruta, linea, self.ctx, self.col)
            return
        if funcion in _ENV_PY:
            if argumentos and isinstance(argumentos[0], ast.Constant) and isinstance(argumentos[0].value, str):
                clase = clasificar_ruta(_ruta_de_variable(argumentos[0].value), self.ctx)
                if clase in ("appdata", "ruta_sistema", "ruta_credenciales"):
                    self.col.agregar(clase, linea, lenguaje="*")

    def _metodo(self, metodo: str, nodo: ast.Call, linea: int | None) -> None:
        """Métodos sobre valores (``ruta.unlink()``, ``Path(x).write_text(…)``)."""
        base = nodo.func.value if isinstance(nodo.func, ast.Attribute) else None
        if metodo in ("unlink", "rmdir"):
            self._borrado(_METODOS_RUTA[metodo], base, linea)
        elif metodo in ("write_text", "write_bytes", "touch", "mkdir"):
            self._escritura(base, linea, crear=metodo in ("touch", "mkdir"))
        elif metodo == "rename":
            self._mover([base, nodo.args[0] if nodo.args else None], linea)
        elif metodo == "replace" and self._ruta(base) is not None and isinstance(base, ast.Call):
            funcion, _ = self._calificado(base.func)
            if funcion in _CONSTRUCTORES_RUTA:
                self._mover([base, nodo.args[0] if nodo.args else None], linea)
        elif metodo in ("glob", "rglob", "iterdir", "read_text", "read_bytes"):
            self._lectura(base, linea)
        elif metodo == "chmod":
            self.col.agregar("pathlib.Path.chmod", linea)
        elif metodo == "open" and base is not None and self._ruta(base) is not None:
            modo = nodo.args[0] if nodo.args else next((k.value for k in nodo.keywords if k.arg == "mode"), None)
            if modo is None or (isinstance(modo, ast.Constant) and isinstance(modo.value, str)
                                and not _MODOS_ESCRITURA.search(modo.value)):
                self._lectura(base, linea)
            else:
                self._escritura(base, linea)
        elif metodo in ("kill", "terminate") and "psutil" in self.modulos:
            self.col.agregar("psutil.Process.kill", linea)
        elif metodo in ("execute", "executescript") and self.sqlite:
            pass  # las rutas de bases del navegador las marca _cadena
        elif metodo in ("load", "loads") and isinstance(nodo.func, ast.Attribute):
            calificado, _ = self._calificado(nodo.func.value)
            if calificado in ("pickle", "marshal", "dill"):
                self.col.agregar(f"{calificado}.{metodo}", linea)

    def _borrado(self, funcion: str, objetivo: ast.AST | None, linea: int | None) -> None:
        ruta = self._ruta(objetivo)
        if funcion == "shutil.rmtree" and ruta is not None and _es_raiz_de_borrado(ruta):
            self.col.agregar("shutil.rmtree+raiz", linea)
        if ruta is not None:
            clase = clasificar_ruta(ruta, self.ctx)
            if clase in _CLASES_CON_RIESGO:
                self.col.agregar(clase, linea, lenguaje="*")
        self.col.agregar(funcion, linea)

    def _mover(self, nodos: Sequence[ast.AST | None], linea: int | None) -> None:
        clases = [self._clase_destino(n, linea) for n in nodos]
        if any(c in ("usuario", "riesgo") or (c == "desconocido" and self.externo) for c in clases):
            self.col.agregar("mover+usuario", linea, lenguaje="*")
        else:
            self.col.agregar("escritura+trabajo", linea, lenguaje="*")

    def _tokens_comando(self, argumentos: Sequence[ast.AST], funcion: str) -> list[str]:
        if not argumentos:
            return []
        primero = argumentos[0]
        if funcion.startswith("os.exec") or funcion.startswith("os.spawn"):
            valores = [a.value for a in argumentos if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            return valores
        if isinstance(primero, (ast.List, ast.Tuple)):
            return [e.value for e in primero.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if funcion == "asyncio.create_subprocess_exec":
            return [a.value for a in argumentos if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if isinstance(primero, ast.Constant) and isinstance(primero.value, str):
            try:
                return shlex.split(primero.value, posix=True)
            except ValueError:
                return primero.value.split()
        return []

    def _contiene_decodificacion(self, nodo: ast.AST) -> bool:
        for hijo in ast.walk(nodo):
            if isinstance(hijo, ast.Call):
                funcion, _ = self._calificado(hijo.func)
                if funcion in _DECODIFICADORES_PY or (funcion or "").endswith(("b64decode", "decodebytes")):
                    return True
        return False

    def _combinaciones(self) -> None:
        if self.decodifica and self.ejecuta:
            self.col.agregar("exec+b64decode", self.col.lineas.get("eval") or self.col.lineas.get("exec"))
        if self.descarga and (self.ejecuta or self.col.tiene("subprocess.*") or self.col.tiene("os.system")
                              or self.col.tiene("os.startfile")):
            self.col.agregar("descarga+ejecucion", None)


def _ruta_de_variable(nombre: str) -> str:
    """Ruta simbólica de una variable de entorno (``APPDATA`` → ``%appdata%``; ``HOME`` → ``~``)."""
    clave = nombre.strip().lower()
    if clave in _VARIABLES_HOME:
        return "~"
    if clave in ("temp", "tmp", "tmpdir"):
        return f"%{clave}%"       # la temporal del script: no depende de la carpeta actual
    if clave in _VARIABLES_TRABAJO:
        return "."
    if clave == "xdg_config_home":
        return "~/.config"
    if clave == "xdg_data_home":
        return "~/.local/share"
    return f"%{clave}%"


def _ruta_padre(ruta: str) -> str:
    """``Path(ruta).parent`` simbólico: ``./script.py`` → ``.``; ``~`` → ``~/..``; ``a/b`` → ``a``."""
    limpia = ruta.replace("\\", "/").rstrip("/")
    if limpia in ("", ".", "./"):
        return "./.."
    if limpia == "~" or _RE_PREFIJO_VARIABLE.fullmatch(_normalizar(limpia) or ""):
        return limpia + "/.."
    if "/" not in limpia:
        return "."
    padre = limpia.rsplit("/", 1)[0]
    return padre or "/"


def _unir_rutas(base: str, resto: str) -> str:
    """Une dos rutas como ``Path``: si ``resto`` es absoluta (o del perfil, o de entorno), gana."""
    normal = _normalizar(resto)
    if _RE_RUTA_ABSOLUTA_WINDOWS.match(normal) or normal.startswith("/") or _RE_PREFIJO_VARIABLE.match(normal):
        return resto
    if not base:
        return resto
    return base.rstrip("/\\") + "/" + resto.lstrip("/\\")


def analizar_python(codigo: str, *, carpetas: Sequence[Path], protegidas: Sequence[Path] = ()) -> Analisis:
    """Análisis estático de un script Python con ``ast`` (sin ejecutarlo). Puro; nunca lanza.

    Resuelve alias (``import shutil as sh`` → ``shutil.move``; ``from os import remove`` →
    ``os.remove``; ``borrar = os.remove``) y marca como dinámico lo no resoluble
    (``getattr(__import__("os"), …)()``). Una sintaxis rota da ``sintaxis_ok=False`` (no ejecutable).
    """
    if not isinstance(codigo, str):
        return _analisis_tamano("ast")
    if _demasiado_grande(codigo):
        return _analisis_tamano("ast")
    ctx = _CtxRutas(carpetas, protegidas)
    colector = _Colector("python")
    try:
        arbol = ast.parse(codigo, mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        linea = getattr(exc, "lineno", None) if isinstance(exc, SyntaxError) else None
        colector.agregar("sintaxis", linea if isinstance(linea, int) else None, lenguaje="*")
        _escaneo_texto_cadenas(codigo, ctx, colector)
        _escaneo_comun(codigo, colector)
        _cierre_credenciales(codigo, colector)
        return colector.analisis("ast", False)
    try:
        _AnalizadorPython(arbol, ctx, colector).ejecutar()
    except RecursionError:
        colector.agregar("sintaxis", None, lenguaje="*")
        return colector.analisis("ast", False)
    except Exception as exc:  # defensivo: un fallo del analizador nunca deja pasar el script
        log.debug("Análisis de Python fallido (%s)", type(exc).__name__)
        colector.agregar("degradado", None, lenguaje="*")
        return colector.analisis("ast", True, "alto")
    _cierre_credenciales(codigo, colector)
    return colector.analisis("ast", True)


_RE_CADENA_SIMPLE: Final = re.compile(r"'([^'\n]{1,1024})'|\"([^\"\n]{1,1024})\"")


def _escaneo_texto_cadenas(texto: str, ctx: _CtxRutas, colector: _Colector) -> None:
    """Rutas dentro de las cadenas entre comillas de un texto que no se pudo parsear."""
    for numero, linea in enumerate(texto.split("\n"), start=1):
        for coincidencia in _RE_CADENA_SIMPLE.finditer(linea):
            valor = coincidencia.group(1) if coincidencia.group(1) is not None else coincidencia.group(2)
            _registrar_ruta(valor, numero, ctx, colector)


# ---------------------------------------------------------------------------
# Análisis de bash (heurístico, nivel mínimo medio)
# ---------------------------------------------------------------------------

_SEGUROS_BASH: Final = frozenset({
    "echo", "printf", "cd", "pwd", "pushd", "popd", "dirs", "test", "[", "[[", "]]", "true", "false", ":",
    "exit", "return", "set", "unset", "shift", "local", "declare", "typeset", "readonly", "export", "let",
    "sleep", "date", "basename", "dirname", "wc", "sort", "uniq", "cut", "tr", "grep", "egrep", "fgrep",
    "awk", "gawk", "paste", "join", "column", "seq", "expr", "bc", "realpath", "readlink", "mktemp",
    "getopts", "trap", "wait", "type", "hash", "which", "jobs", "umask", "shopt", "notify-send", "tput",
    "clear", "yes", "nproc", "numfmt", "md5sum", "sha1sum", "sha256sum", "sha512sum", "cksum", "od", "xxd",
    "hexdump", "strings", "iconv", "rev", "tac", "nl", "fold", "fmt", "comm", "diff", "cmp", "ls", "cat",
    "head", "tail", "less", "more", "tree", "stat", "file", "du", "find", "jq", "locale", "logname", "id",
    "whoami", "groups", "hostname", "printenv", "cal", "break", "continue", "command", "builtin", "time",
    "env", "nohup", "nice", "ionice", "timeout", "stdbuf", "xargs", "then", "do", "else", "elif", "fi", "done",
    "esac", "in", "function", "{", "}", "!", "if", "while", "until", "for", "case", "coproc",
})
_PALABRAS_CLAVE_BASH: Final = frozenset({"if", "then", "else", "elif", "fi", "for", "in", "do", "done", "while",
                                         "until", "case", "esac", "function", "{", "}", "!", "time", "[[", "]]",
                                         "coproc", "select"})
_PREFIJOS_BASH: Final = frozenset({"env", "nohup", "nice", "ionice", "time", "command", "builtin", "exec",
                                   "xargs", "timeout", "stdbuf", "setsid", "chrt", "taskset", "unbuffer"})
_ELEVACION_BASH: Final = frozenset({"sudo", "doas", "pkexec", "su", "run0", "sudoedit"})
_SHELLS: Final = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "perl", "ruby",
                            "node", "source", ".", "eval"})
_OPERADORES_BASH: Final = frozenset({";", "&&", "||", "|", "&", "(", ")", ";;", "|&", ";&", ";;&"})
_REDIRECCIONES_BASH: Final = frozenset({">", ">>", "&>", "&>>", ">|", "1>", "2>", ">&"})
_RE_HEREDOC: Final = re.compile(r"(?<!<)<<(-?)(?!<)\s*(['\"]?)([A-Za-z_][\w]*)\2")
_RE_BINARIO_SISTEMA: Final = re.compile(r"^/(?:usr/)?(?:local/)?s?bin/[\w.+-]+$")
_RE_SUSTITUCION_VARIABLE: Final = re.compile(r"\$\([^)]*\$[\w{]|`[^`]*\$[\w{][^`]*`")
_RE_SUSTITUCION: Final = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_RE_EVAL_BASE64: Final = re.compile(r"\beval\b.*\bbase64\b\s+(?:-\w*d\w*|--decode)", re.IGNORECASE)
_RE_BASE64_A_SHELL: Final = re.compile(r"\bbase64\b\s+(?:-\w*d\w*|--decode).*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")
_RE_DESCARGA_SHELL: Final = re.compile(
    r"(?:\b(?:ba|z|da|k)?sh\b|\bsource\b|\beval\b|\bpython3?\b|\bperl\b)\s+(?:-\w+\s+)*(?:<\(\s*|[\"']?\$\(\s*)"
    r"(?:curl|wget)\b|<\(\s*(?:curl|wget)\b")
_RE_FUNCION_BASH: Final = re.compile(r"^\s*(?:function\s+([\w.-]+)|([\w.-]+)\s*\(\s*\))", re.MULTILINE)
_SEGURIDAD_SYSTEMCTL: Final = re.compile(r"firewalld|ufw|apparmor|selinux|nftables|iptables|fail2ban|auditd")


def _quitar_comentarios_bash(linea: str) -> str:
    """Quita el comentario ``#`` de una línea de bash (fuera de comillas y al empezar una palabra)."""
    resultado = []
    comilla = ""
    anterior = " "
    escapado = False
    for caracter in linea:
        if escapado:
            resultado.append(caracter)
            escapado = False
        elif caracter == "\\" and comilla != "'":
            resultado.append(caracter)
            escapado = True
        elif comilla:
            resultado.append(caracter)
            if caracter == comilla:
                comilla = ""
        elif caracter in ("'", '"'):
            comilla = caracter
            resultado.append(caracter)
        elif caracter == "#" and (anterior.isspace() or anterior in ";|&("):
            break
        else:
            resultado.append(caracter)
        anterior = caracter
    return "".join(resultado)


def _lineas_logicas_bash(codigo: str) -> tuple[list[tuple[int, str]], list[int]]:
    """Líneas lógicas (con ``\\`` de continuación unidas) y sin comentarios. El cuerpo de un heredoc es
    texto (se salta), salvo si lo lee una shell (``bash <<EOF``): entonces es código y se analiza, y su
    número de línea se devuelve en la segunda lista (ejecución dinámica)."""
    resultado: list[tuple[int, str]] = []
    dinamicas: list[int] = []
    lineas = codigo.split("\n")
    i = 0
    delimitador: tuple[str, bool, bool] | None = None
    while i < len(lineas):
        linea = lineas[i]
        if delimitador is not None:
            fin, sangria, es_codigo = delimitador
            if (linea.lstrip("\t") if sangria else linea).rstrip() == fin:
                delimitador = None
            elif es_codigo:
                resultado.append((i + 1, _quitar_comentarios_bash(linea)))
            i += 1
            continue
        inicio = i + 1
        while linea.endswith("\\") and i + 1 < len(lineas):
            i += 1
            linea = linea[:-1] + " " + lineas[i]
        limpia = _quitar_comentarios_bash(linea)
        heredoc = _RE_HEREDOC.search(limpia)
        if heredoc is not None:
            palabras = _tokens_bash(limpia[:heredoc.start()])
            nombres = [_nombre_comando(p) for p in palabras if p not in _OPERADORES_BASH
                       and not re.match(r"^[A-Za-z_]\w*=", p)]
            nombres = [n for n in nombres if n not in _PREFIJOS_BASH and n not in _ELEVACION_BASH]
            es_codigo = bool(nombres) and nombres[0] in _SHELLS
            if es_codigo:
                dinamicas.append(inicio)
            delimitador = (heredoc.group(3), heredoc.group(1) == "-", es_codigo)
        resultado.append((inicio, limpia))
        i += 1
    return resultado, dinamicas


def _tokens_bash(texto: str) -> list[str]:
    try:
        lexer = shlex.shlex(texto, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:  # comillas sin cerrar (cadena de varias líneas): reparto tolerante
        return re.findall(r";;|&&|\|\||[;&|()<>]|[^\s;&|()<>]+", texto)


def _comandos_simples(tokens: Sequence[str]) -> list[tuple[list[str], list[str], str]]:
    """Separa en comandos simples: ``(palabras, destinos de redirección, operador anterior)``."""
    comandos: list[tuple[list[str], list[str], str]] = []
    palabras: list[str] = []
    destinos: list[str] = []
    operador = ""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _OPERADORES_BASH:
            if palabras or destinos:
                comandos.append((palabras, destinos, operador))
            palabras, destinos = [], []
            operador = token
        elif token in _REDIRECCIONES_BASH or (token.endswith(">") and token.rstrip(">").isdigit()):
            if i + 1 < len(tokens):
                siguiente = tokens[i + 1]
                if not (token in (">&", "<&") and (siguiente.isdigit() or siguiente == "-")):
                    destinos.append(siguiente)
                i += 1
        elif token in ("<", "<<", "<<<", "<<-"):
            i += 1
        elif token.isdigit() and i + 1 < len(tokens) and tokens[i + 1] in (">", ">>", ">&"):
            pass   # descriptor (2>…)
        else:
            palabras.append(token)
        i += 1
    if palabras or destinos:
        comandos.append((palabras, destinos, operador))
    return comandos


def _clase_bash(objetivo: str, linea: int | None, ctx: _CtxRutas, colector: _Colector) -> str:
    """``trabajo`` | ``usuario`` | ``riesgo`` | ``desconocido`` para un argumento de bash."""
    if objetivo.startswith("$") and not _RE_PREFIJO_VARIABLE.match(_normalizar(objetivo)):
        return "desconocido"
    clase = _registrar_ruta(objetivo, linea, ctx, colector)
    return "riesgo" if clase in _CLASES_CON_RIESGO else _clase_relativa(clase, objetivo, ctx)


def _cambia_carpeta_bash(lineas: Sequence[tuple[int, str]], ctx: _CtxRutas) -> bool:
    """¿Algún ``cd``/``pushd`` lleva a otra carpeta que no es la de trabajo (``cd`` solo = el home; ``cd -``,
    variables o sustituciones = desconocida)? Las subcarpetas relativas no cuentan."""
    for _, texto in lineas:
        if not re.search(r"(?<![\w.-])(?:cd|pushd)(?![\w.-])", texto):
            continue
        for palabras, _, _ in _comandos_simples(_tokens_bash(texto)):
            palabras = list(palabras)
            while palabras and (re.match(r"^[A-Za-z_][\w]*=", palabras[0]) or palabras[0] in _PALABRAS_CLAVE_BASH
                                or palabras[0] in ("builtin", "command")):
                palabras.pop(0)
            if not palabras or palabras[0] not in ("cd", "pushd"):
                continue
            objetivos = [p for p in palabras[1:] if p not in ("-P", "-L", "-e", "-@", "--", "-n")]
            if not objetivos:
                return True                    # cd sin argumentos: el home
            objetivo = objetivos[0]
            if objetivo.startswith(("-", "+")) or "$" in objetivo or "`" in objetivo:
                return True
            if _parece_ruta(objetivo) and clasificar_ruta(objetivo, ctx) != "trabajo":
                return True
    return False


def _destino_escritura(destino: str, linea: int | None, ctx: _CtxRutas, colector: _Colector,
                       externo: bool) -> None:
    """Escritura en ``destino`` (redirección, ``tee``, ``cp``…): medio en la carpeta de trabajo; alto en
    tus carpetas, fuera de ellas o en un destino desconocido si el script nombra sitios de fuera."""
    if not destino or destino.startswith("&") or destino.isdigit() or destino == "-" \
            or _normalizar(destino) in ("/dev/null", "/dev/stdout", "/dev/stderr", "nul", "$null"):
        return
    clase = _clase_bash(destino, linea, ctx, colector)
    if clase in ("usuario", "riesgo") or (clase == "desconocido" and externo):
        colector.agregar("escritura+usuario", linea, lenguaje="*")
    else:
        colector.agregar("escritura+trabajo", linea, lenguaje="*")


def _clasificar_comando_bash(nombre: str, argumentos: Sequence[str], linea: int | None, ctx: _CtxRutas,
                             colector: _Colector, *, texto_linea: str, funciones: frozenset[str],
                             anidado: bool = False, externo: bool = True) -> None:
    """Reglas de bash para un comando simple (``nombre`` ya sin ruta ni prefijos)."""
    args = list(argumentos)
    bajos = [a.lower() for a in args]
    if nombre in _ELEVACION_BASH:
        colector.agregar(nombre, linea, lenguaje="bash")
        return
    if nombre.startswith("mkfs") or nombre in ("shred", "wipefs", "fdisk", "parted", "sfdisk", "blkdiscard"):
        colector.agregar("mkfs" if nombre.startswith("mkfs") else nombre, linea, lenguaje="bash")
        return
    if nombre == "dd":
        if any(a.startswith("of=/dev/") for a in bajos):
            colector.agregar("dd+dispositivo", linea, lenguaje="bash")
        else:
            colector.agregar("dd", linea, lenguaje="bash")
        return
    if nombre == "rm":
        recursivo = any(a in ("-r", "-R", "--recursive") or (a.startswith("-") and not a.startswith("--")
                                                              and ("r" in a or "R" in a)) for a in args)
        objetivos = [a for a in args if not a.startswith("-")]
        if recursivo and any(_es_raiz_de_borrado(o) or (o.startswith("$") and ":?" not in o
                                                         and "/" not in o.strip("\"'{}$"))
                             for o in objetivos):
            colector.agregar("rm+raiz", linea, lenguaje="bash")
        elif any(o.strip("\"'") in ("/", "/*", "~", "~/*", "*") for o in objetivos):
            colector.agregar("rm+raiz", linea, lenguaje="bash")
        colector.agregar("rm", linea, lenguaje="bash")
        return
    if nombre in ("rmdir", "unlink"):
        colector.agregar(nombre, linea, lenguaje="bash")
        return
    if nombre == "find":
        if "-delete" in bajos or any(a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(bajos)
                                     and _nombre_comando(bajos[i + 1]) in ("rm", "shred", "unlink")
                                     for i, a in enumerate(bajos)):
            colector.agregar("find+delete", linea, lenguaje="bash")
        elif any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in bajos):
            colector.agregar("find+exec", linea, lenguaje="bash")
        return
    if nombre == "systemctl":
        sub = next((a for a in bajos if not a.startswith("-")), "")
        if sub in ("poweroff", "reboot", "halt", "suspend", "hibernate", "hybrid-sleep", "kexec", "rescue",
                   "emergency", "soft-reboot"):
            colector.agregar("systemctl+apagado", linea, lenguaje="bash")
        elif sub in ("enable", "link", "preset", "preset-all", "edit", "set-default", "add-wants",
                     "add-requires", "reenable"):
            colector.agregar("systemctl+arranque", linea, lenguaje="bash")
        elif sub in ("stop", "disable", "mask", "kill") and _SEGURIDAD_SYSTEMCTL.search(" ".join(bajos)):
            colector.agregar("systemctl+seguridad", linea, lenguaje="bash")
        elif sub in ("status", "show", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
                     "list-timers", "cat", "list-dependencies", "list-sockets"):
            colector.agregar("systemctl+consulta", linea, lenguaje="bash")
        else:
            colector.agregar("systemctl", linea, lenguaje="bash")
        return
    if nombre == "loginctl":
        if any(a in ("poweroff", "reboot", "suspend", "hibernate", "terminate-session", "terminate-user",
                     "kill-session", "kill-user", "lock-session") for a in bajos):
            colector.agregar("loginctl+apagado", linea, lenguaje="bash")
        else:
            colector.agregar("loginctl", linea, lenguaje="bash")
        return
    if nombre in ("init", "telinit"):
        if any(a in ("0", "6") for a in bajos) or nombre == "telinit":
            colector.agregar("telinit", linea, lenguaje="bash")
        return
    if nombre == "crontab":
        colector.agregar("crontab+consulta" if bajos == ["-l"] else "crontab", linea, lenguaje="bash")
        return
    if nombre in ("dbus-send", "gdbus", "busctl", "qdbus", "dbus-monitor"):
        if re.search(r"secrets|kwallet|keyring|org\.freedesktop\.secrets", texto_linea, re.IGNORECASE):
            colector.agregar("dbus+secretos", linea, lenguaje="bash")
        else:
            colector.agregar("dbus", linea, lenguaje="bash")
        return
    if nombre == "base64":
        if any(a in ("-d", "--decode", "-D") or (a.startswith("-") and not a.startswith("--") and "d" in a)
               for a in args):
            colector.agregar("base64", linea, lenguaje="bash")
        return
    if nombre == "mv":
        objetivos = [a for a in args if not a.startswith("-")]
        comodin = any(c in a for a in objetivos for c in "*?[")
        clases = [_clase_bash(o, linea, ctx, colector) for o in objetivos]
        if comodin or any(c in ("usuario", "riesgo") or (c == "desconocido" and externo) for c in clases):
            colector.agregar("mover+usuario", linea, lenguaje="*")
        else:
            colector.agregar("mv", linea, lenguaje="bash")
        return
    if nombre in ("cp", "tee", "touch", "mkdir", "ln", "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "xz",
                  "7z", "sed", "truncate"):
        colector.agregar(nombre, linea, lenguaje="bash")
        objetivos = [a for a in args if not a.startswith("-")]
        if nombre == "sed" and not any(a.startswith("-i") or a == "--in-place" for a in args):
            return
        if nombre == "tee":
            for objetivo in objetivos:
                _destino_escritura(objetivo, linea, ctx, colector, externo)
        elif nombre in ("cp", "ln") and objetivos:
            _destino_escritura(objetivos[-1], linea, ctx, colector, externo)
        elif objetivos and nombre in ("touch", "mkdir"):
            for objetivo in objetivos:
                clase = _registrar_ruta(objetivo, linea, ctx, colector)
                colector.agregar("escritura+trabajo", linea, lenguaje="*")
                if clase in _CLASES_CON_RIESGO:
                    colector.agregar("escritura+usuario", linea, lenguaje="*")
        elif objetivos and nombre in ("sed", "truncate"):
            for objetivo in objetivos[1:] if nombre == "sed" else objetivos:
                _destino_escritura(objetivo, linea, ctx, colector, externo)
        return
    if nombre in ("ping", "dig", "nslookup", "host", "traceroute", "tracepath"):
        colector.agregar(nombre, linea, lenguaje="bash")
        return
    if ("bash", nombre) in _FILAS:
        colector.agregar(nombre, linea, lenguaje="bash")
        return
    if anidado:
        return   # comando suelto (subprocess): lo que no está en el catálogo lo ve la regla de PowerShell
    if nombre in _SEGUROS_BASH or nombre in funciones or nombre in _PALABRAS_CLAVE_BASH:
        return
    if nombre.startswith("$") or "$(" in nombre or "`" in nombre:
        colector.agregar("$comando", linea, lenguaje="bash")
        return
    if re.match(r"^[A-Za-z_][\w]*=", nombre):
        return
    colector.agregar("binario", linea, lenguaje="bash")


def analizar_bash(codigo: str, *, carpetas: Sequence[Path], protegidas: Sequence[Path] = ()) -> Analisis:
    """Análisis heurístico de bash línea a línea (``shlex`` tolerante). Nivel mínimo ``medio``.

    ``eval``, ``source``, ``.``, ``$(…)``/comillas invertidas sobre variables y comandos en variables →
    dinámico; ``curl … | sh`` → crítico. Nunca usa ``bash -n``: la sintaxis se da por buena.
    """
    if not isinstance(codigo, str) or _demasiado_grande(codigo):
        return _analisis_tamano("heuristico")
    ctx = _CtxRutas(carpetas, protegidas)
    colector = _Colector("bash")
    try:
        _analizar_bash(codigo, ctx, colector)
    except Exception as exc:  # defensivo: un fallo del analizador nunca baja el nivel
        log.debug("Análisis de bash fallido (%s)", type(exc).__name__)
        colector.agregar("degradado", None, lenguaje="*")
        return colector.analisis("heuristico", True, "alto")
    return colector.analisis("heuristico", True, "medio")


def _analizar_bash(codigo: str, ctx: _CtxRutas, colector: _Colector, profundidad: int = 0) -> None:
    funciones = frozenset(n for par in _RE_FUNCION_BASH.findall(codigo) for n in par if n)
    lineas, dinamicas = _lineas_logicas_bash(codigo)
    for numero in dinamicas:
        colector.agregar("$(…)", numero)
    sin_comentarios = "\n".join(texto for _, texto in lineas)
    externo = bool(re.search(r"(?:^|[\s\"'=])(?:/|~|\$\{?home\b|\$\{?xdg_|\.\./)", sin_comentarios,
                             re.IGNORECASE | re.MULTILINE))
    if profundidad == 0 and _cambia_carpeta_bash(lineas, ctx):
        ctx.cwd_fuera = True
    externo = externo or ctx.cwd_fuera
    for numero, texto in lineas:
        if not texto.strip():
            continue
        if _RE_EVAL_BASE64.search(texto) or _RE_BASE64_A_SHELL.search(texto):
            colector.agregar("eval+base64", numero)
        if _RE_DESCARGA_SHELL.search(texto):
            colector.agregar("curl|sh", numero)
        if _RE_SUSTITUCION_VARIABLE.search(texto):
            colector.agregar("$(…)", numero)
        if _RE_RUN.search(texto) or _RE_HKLM.search(texto):
            colector.agregar("arranque" if _RE_RUN.search(texto) else "hklm", numero, lenguaje="*")
        if _RE_BASE64_LARGO.search(texto):
            colector.agregar("base64_largo", numero, lenguaje="*")
        if profundidad < 3:
            for sustitucion in _RE_SUSTITUCION.finditer(texto):
                interior = sustitucion.group(1) if sustitucion.group(1) is not None else sustitucion.group(2)
                if interior and interior.strip():
                    _analizar_bash_linea(interior, numero, ctx, colector, funciones, externo, profundidad + 1)
        _analizar_bash_linea(texto, numero, ctx, colector, funciones, externo, profundidad)
    _cierre_credenciales(sin_comentarios, colector)


def _analizar_bash_linea(texto: str, numero: int, ctx: _CtxRutas, colector: _Colector, funciones: frozenset[str],
                         externo: bool, profundidad: int) -> None:
    tokens = _tokens_bash(texto)
    comandos = _comandos_simples(tokens)
    tuberia: list[str] = []
    for palabras, destinos, operador in comandos:
        if operador != "|" and operador != "|&":
            tuberia = []
        for destino in destinos:
            _destino_escritura(destino, numero, ctx, colector, externo)
        palabras = list(palabras)
        if palabras and palabras[0] in ("for", "select", "case"):
            # Cabecera de bucle o de case: «for f in *.jpg». Solo cuentan las rutas de la lista.
            for palabra in palabras[1:]:
                _registrar_ruta(palabra, numero, ctx, colector)
            continue
        for posicion, palabra in enumerate(palabras):
            valor = palabra.split("=", 1)[1] if re.match(r"^-{0,2}[\w.-]+=", palabra) else palabra
            if posicion == 0 and _RE_BINARIO_SISTEMA.match(valor):
                continue   # /usr/bin/rm: el nombre del comando, no una ruta que toque
            _registrar_ruta(valor, numero, ctx, colector)
        # Asignaciones iniciales y palabras clave.
        while palabras and (re.match(r"^[A-Za-z_][\w]*=", palabras[0]) or palabras[0] in _PALABRAS_CLAVE_BASH):
            palabras.pop(0)
        # Prefijos (sudo, env, nohup, xargs, timeout…).
        while palabras:
            nombre = _nombre_comando(palabras[0])
            if nombre in _ELEVACION_BASH:
                colector.agregar(nombre, numero, lenguaje="bash")
                palabras.pop(0)
                while palabras and palabras[0].startswith("-"):
                    palabras.pop(0)
                if nombre == "su":
                    break
                continue
            if nombre in _PREFIJOS_BASH:
                palabras.pop(0)
                if nombre == "exec" and palabras:
                    colector.agregar("exec", numero, lenguaje="bash")
                while palabras and (palabras[0].startswith("-") or re.match(r"^[A-Za-z_][\w]*=", palabras[0])
                                    or (nombre == "timeout" and re.match(r"^\d", palabras[0]))
                                    or (nombre in ("nice", "ionice", "chrt", "taskset") and palabras[0].isdigit())):
                    palabras.pop(0)
                continue
            break
        if not palabras:
            continue
        crudo = palabras[0]
        if crudo.startswith("$") or "$(" in crudo or "`" in crudo:
            colector.agregar("$comando", numero, lenguaje="bash")
            tuberia.append("$")
            continue
        nombre = _nombre_comando(crudo)
        if _instala_paquetes(palabras):
            colector.agregar("instalar", numero, lenguaje="*")
        if nombre in _SHELLS and any(p in ("curl", "wget") for p in tuberia):
            colector.agregar("curl|sh", numero, lenguaje="bash")
        if nombre in ("sh", "bash", "zsh", "dash") and palabras[1:2] == ["-c"]:
            colector.agregar("$(…)", numero, lenguaje="bash")
            if len(palabras) > 2 and profundidad < 3:
                _analizar_bash_linea(palabras[2], numero, ctx, colector, funciones, externo, profundidad + 1)
        _clasificar_comando_bash(nombre, palabras[1:], numero, ctx, colector, texto_linea=texto,
                                 funciones=funciones, externo=externo)
        if nombre == "xargs" and len(palabras) > 1:
            _clasificar_comando_bash(_nombre_comando(palabras[1]), palabras[2:], numero, ctx, colector,
                                     texto_linea=texto, funciones=funciones, externo=externo)
        tuberia.append(nombre)


# ---------------------------------------------------------------------------
# Análisis de PowerShell (AST del analizador del repo, o heurística)
# ---------------------------------------------------------------------------

_ALIAS_PS: Final[Mapping[str, str]] = {
    "iex": "invoke-expression", "iwr": "invoke-webrequest", "irm": "invoke-restmethod",
    "curl": "invoke-webrequest", "wget": "invoke-webrequest", "rm": "remove-item", "del": "remove-item",
    "erase": "remove-item", "ri": "remove-item", "rd": "remove-item", "rmdir": "remove-item",
    "mi": "move-item", "mv": "move-item", "move": "move-item", "rni": "rename-item", "ren": "rename-item",
    "cpi": "copy-item", "cp": "copy-item", "copy": "copy-item", "sc": "set-content", "ac": "add-content",
    "gc": "get-content", "cat": "get-content", "type": "get-content", "gci": "get-childitem",
    "ls": "get-childitem", "dir": "get-childitem", "gi": "get-item", "ni": "new-item", "md": "new-item",
    "mkdir": "new-item", "saps": "start-process", "start": "start-process", "kill": "stop-process",
    "spps": "stop-process", "gps": "get-process", "ps": "get-process", "gwmi": "get-wmiobject",
    "gcim": "get-ciminstance", "icm": "invoke-command", "ii": "invoke-item", "sajb": "start-job",
    "echo": "write-output", "write": "write-output", "clc": "clear-content", "gsv": "get-service",
    "sasv": "start-service", "spsv": "stop-service", "ipmo": "import-module", "cd": "set-location",
    "sl": "set-location", "chdir": "set-location", "pwd": "get-location", "gl": "get-location",
    "select": "select-object", "?": "where-object", "where": "where-object", "%": "foreach-object",
    "foreach": "foreach-object", "ogv": "out-gridview", "sleep": "start-sleep", "r": "invoke-history",
    "ihy": "invoke-history", "nal": "new-alias", "sal": "set-alias", "epal": "export-alias",
    "sp": "set-itemproperty", "gp": "get-itemproperty", "rp": "remove-itemproperty", "cls": "clear-host",
    "tee": "tee-object", "sort": "sort-object", "measure": "measure-object", "group": "group-object",
    "ft": "format-table", "fl": "format-list", "fw": "format-wide", "h": "get-history",
    "history": "get-history", "gm": "get-member", "gcm": "get-command", "man": "help", "rv": "remove-variable",
    "sv": "set-variable", "gv": "get-variable", "nv": "new-variable", "clv": "clear-variable",
    "pushd": "push-location", "popd": "pop-location", "rjb": "remove-job", "gjb": "get-job", "rcjb": "receive-job",
    "wjb": "wait-job", "spjb": "stop-job", "ipcsv": "import-csv", "epcsv": "export-csv", "dbp": "disable-psbreakpoint",
    "compare": "compare-object", "diff": "compare-object", "gu": "get-unique", "lp": "out-printer",
    "rvpa": "resolve-path", "cvpa": "convert-path", "gdr": "get-psdrive", "ndr": "new-psdrive",
    "rdr": "remove-psdrive", "shcm": "show-command", "gin": "get-computerinfo", "gtz": "get-timezone",
    "stz": "set-timezone", "scb": "set-clipboard", "gcb": "get-clipboard", "trcm": "trace-command",
    "iwmi": "invoke-wmimethod", "swmi": "set-wmiinstance", "rwmi": "remove-wmiobject",
}
_VERBOS_SEGUROS_PS: Final = frozenset({"get", "write", "format", "select", "where", "foreach", "sort", "group",
                                       "measure", "compare", "convertto", "convertfrom", "convert", "out", "test",
                                       "join", "split", "resolve", "read", "wait", "trace", "show", "find",
                                       "search", "use", "debug", "push", "pop", "receive", "enter", "exit"})
_SEGUROS_PS: Final = frozenset({
    "new-timespan", "new-guid", "new-variable", "new-object", "set-variable", "set-location", "set-strictmode",
    "add-member", "start-sleep", "stop-transcript", "start-transcript", "remove-variable", "clear-host",
    "clear-variable", "set-clipboard", "new-temporaryfile", "foreach-object", "where-object", "select-object",
    "sort-object", "group-object", "measure-object", "tee-object", "write-output", "write-host",
    "write-error", "write-warning", "write-verbose", "write-debug", "write-information", "write-progress",
    "get-date", "get-random", "get-location", "get-member", "get-command", "get-help", "get-variable",
    "get-host", "get-culture", "get-uicculture", "get-uptime", "get-timezone", "get-history", "get-unique",
    "get-job", "get-alias", "get-module", "get-verb", "help", "out-string", "out-null", "out-host",
    "out-default", "exit", "return", "break", "continue", "throw", "param", "switch", "begin", "process",
    "end", "remove-job", "receive-job", "wait-job", "stop-job", "start-sleep",
})
_APPS_CONOCIDAS: Final = frozenset({"notepad", "calc", "mspaint", "explorer", "write", "wordpad", "charmap",
                                    "snippingtool", "magnify", "osk", "msedge", "chrome", "firefox", "winword",
                                    "excel", "powerpnt", "onenote", "code", "spotify", "ms-settings:",
                                    "control", "taskmgr", "notepad++"})
_LECTORES_PS: Final = frozenset({"get-childitem", "get-content", "get-item", "test-path", "resolve-path",
                                 "select-string", "import-csv", "get-filehash", "get-acl", "import-clixml"})
_ESCRITORES_PS: Final = frozenset({"set-content", "add-content", "out-file", "clear-content", "export-csv",
                                   "export-clixml", "tee-object", "copy-item", "expand-archive",
                                   "compress-archive"})
_MOVER_PS: Final = frozenset({"move-item", "rename-item"})
_RE_DINAMICO_IEX: Final = re.compile(r"i\W*e\W*x|invoke\W*expression", re.IGNORECASE)
_RE_IEX_TEXTO: Final = re.compile(r"(?<![\w$-])(?:iex|invoke-expression)(?![\w-])|\.invokescript\s*\("
                                  r"|\[scriptblock\]::create", re.IGNORECASE)
_RE_VARIABLE_PS: Final = re.compile(r"\$(?!(?:true|false|null)\b)[\w{(:]", re.IGNORECASE)
_RE_ENCODED: Final = re.compile(r"(?<![\w-])-(?:e|ec|en|enc|enco|encod|encode|encoded|encodedc\w*)\s+"
                                r"['\"]?[A-Za-z0-9+/]{8,}", re.IGNORECASE)
_RE_CHAR_ARRAY: Final = re.compile(r"\[char\s*\[\s*\]\s*\]|\[char\]\s*\d", re.IGNORECASE)
_RE_FUNCION_PS: Final = re.compile(r"(?im)^\s*(?:function|filter)\s+([\w-]+)")
_RE_EXTERNO_PS: Final = re.compile(
    r"\$env:(?!te?mp\b)\w+|\$\{env:|\$home\b|getfolderpath|specialfolder|(?<![\w])~[\\/]|\b[a-z]:[\\/]"
    r"|\\\\\w|%\w+%|\.\.[\\/]|get-psdrive|get-volume|\$psscriptroot\s*\\?\.\.", re.IGNORECASE)
_RE_USUARIO_PS: Final = re.compile(r"\$env:(?:userprofile|home|onedrive)\b|\$home\b|getfolderpath|(?<![\w])~[\\/]",
                                   re.IGNORECASE)
_TIPOS_PS: Final[tuple[tuple[str, str], ...]] = (
    ("net.webclient", "net.webclient"), ("net.http.httpclient", "net.webclient"),
    ("net.sockets.", "net.webclient"), ("net.webrequest", "net.webclient"), ("net.httpwebrequest", "net.webclient"),
    ("net.websockets", "net.webclient"), ("net.mail.smtpclient", "net.webclient"),
    ("reflection.assembly", "[reflection.assembly]::load"), ("reflection.emit", "[reflection.assembly]::load"),
    ("runtime.interopservices.marshal", "[runtime.interopservices.marshal]"),
    ("passwordvault", "passwordvault"), ("security.cryptography.protecteddata", "protecteddata"),
    ("diagnostics.process", "[diagnostics.process]::start"), ("windows.forms.sendkeys", "sendkeys"),
    ("microsoft.win32.registry", "reg"),
)
_COM_PS: Final[Mapping[str, str]] = {
    "wscript.shell": "wscript.shell", "shell.application": "shell.application",
    "schedule.service": "schedule.service", "msxml2.xmlhttp": "net.webclient",
    "msxml2.serverxmlhttp": "net.webclient", "winhttp.winhttprequest.5.1": "net.webclient",
    "internetexplorer.application": "net.webclient", "outlook.application": "net.webclient",
    "scripting.filesystemobject": "escritura+usuario", "wscript.network": "net",
}


def _ps_limpiar(codigo: str) -> tuple[str, str, list[tuple[int, str]]]:
    """``(sin_comentarios, sin_comentarios_ni_cadenas, cadenas)`` de un script de PowerShell.

    Mantiene la longitud y los saltos de línea (los números de línea no cambian). ``cadenas`` son
    ``(línea, contenido)`` de cada literal entre comillas (incluidos los here-strings).
    """
    sin_comentarios: list[str] = []
    sin_cadenas: list[str] = []
    cadenas: list[tuple[int, str]] = []
    i = 0
    n = len(codigo)
    linea = 1
    while i < n:
        caracter = codigo[i]
        siguiente = codigo[i + 1] if i + 1 < n else ""
        anterior = codigo[i - 1] if i > 0 else "\n"
        if caracter == "<" and siguiente == "#":
            fin = codigo.find("#>", i + 2)
            fin = n if fin < 0 else fin + 2
            bloque = codigo[i:fin]
            relleno = re.sub(r"[^\n]", " ", bloque)
            sin_comentarios.append(relleno)
            sin_cadenas.append(relleno)
            linea += bloque.count("\n")
            i = fin
            continue
        if caracter == "#" and (anterior.isspace() or anterior in ";|{}()"):
            fin = codigo.find("\n", i)
            fin = n if fin < 0 else fin
            relleno = " " * (fin - i)
            sin_comentarios.append(relleno)
            sin_cadenas.append(relleno)
            i = fin
            continue
        if caracter == "@" and siguiente in ("'", '"') and codigo[i + 2:i + 3] in ("\n", "\r"):
            cierre = "\n" + siguiente + "@"
            fin = codigo.find(cierre, i + 2)
            fin = n if fin < 0 else fin + len(cierre)
            bloque = codigo[i:fin]
            sin_comentarios.append(bloque)
            sin_cadenas.append(caracter + siguiente + re.sub(r"[^\n]", " ", bloque[2:-2]) + bloque[-2:]
                               if len(bloque) >= 4 else bloque)
            for desplazamiento, contenido in enumerate(bloque[3:-3].split("\n")):
                cadenas.append((linea + 1 + desplazamiento, contenido))
            linea += bloque.count("\n")
            i = fin
            continue
        if caracter in ("'", '"', "\u2018", "\u2019", "\u201c", "\u201d"):
            simple = caracter in ("'", "\u2018", "\u2019")
            j = i + 1
            contenido: list[str] = []
            while j < n:
                c = codigo[j]
                if not simple and c == "`" and j + 1 < n:
                    contenido.append(codigo[j + 1])
                    j += 2
                    continue
                if (simple and c in ("'", "\u2018", "\u2019")) or (not simple and c in ('"', "\u201c", "\u201d")):
                    if j + 1 < n and codigo[j + 1] == c:
                        contenido.append(c)
                        j += 2
                        continue
                    break
                contenido.append(c)
                j += 1
            fin = min(n, j + 1)
            bloque = codigo[i:fin]
            sin_comentarios.append(bloque)
            sin_cadenas.append(bloque[0] + re.sub(r"[^\n]", " ", bloque[1:-1]) + bloque[-1:]
                               if len(bloque) >= 2 else bloque)
            texto = "".join(contenido)
            for desplazamiento, parte in enumerate(texto.split("\n")):
                cadenas.append((linea + desplazamiento, parte))
            linea += bloque.count("\n")
            i = fin
            continue
        sin_comentarios.append(caracter)
        sin_cadenas.append(caracter)
        if caracter == "\n":
            linea += 1
        i += 1
    return "".join(sin_comentarios), "".join(sin_cadenas), cadenas


def _lista(valor: Any) -> list[Any]:
    """Lista tolerante para el JSON de PowerShell (``ConvertTo-Json`` puede aplanar un solo elemento)."""
    if valor is None:
        return []
    if isinstance(valor, list):
        return valor
    if isinstance(valor, tuple):
        return list(valor)
    return [valor]


def _texto_ps(valor: Any) -> str:
    return valor if isinstance(valor, str) else ("" if valor is None else str(valor))


def _linea_ps(valor: Any) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) and valor > 0 else None


def _clasificar_externo_ps(nombre: str, bajos: Sequence[str], literales: Sequence[str], linea: int | None,
                           ctx: _CtxRutas, colector: _Colector) -> bool:
    """Reglas de los ejecutables de Windows con contexto (netsh, schtasks, reg, cmd, powershell…).
    Devuelve ``True`` si reconoció el nombre."""
    texto = " ".join(bajos)
    if nombre == "netsh":
        colector.agregar("netsh+firewall" if "firewall" in texto else "netsh", linea, lenguaje="powershell")
        return True
    if nombre == "schtasks":
        modifica = any(a in ("/create", "/change", "/delete", "/run", "/end", "-create", "-change", "-delete")
                       for a in bajos)
        colector.agregar("schtasks" if modifica or not any(a in ("/query", "-query") for a in bajos)
                         else "schtasks+consulta", linea, lenguaje="powershell")
        return True
    if nombre == "reg":
        sub = bajos[0] if bajos else ""
        if sub in ("delete", "import", "restore", "load", "unload", "copy"):
            colector.agregar("reg+modifica", linea, lenguaje="powershell")
        elif sub in ("query", "export", "compare"):
            colector.agregar("reg+consulta", linea, lenguaje="powershell")
        else:
            colector.agregar("reg", linea, lenguaje="powershell")
        if _RE_HKLM.search(texto) or re.search(r"\bhklm\b", texto):
            colector.agregar("hklm", linea, lenguaje="*")
        return True
    if nombre == "cmd":
        colector.agregar("cmd", linea, lenguaje="powershell")
        for i, argumento in enumerate(bajos):
            if argumento in ("/c", "/k", "/r") and i + 1 < len(literales):
                interior = " ".join(literales[i + 1:])
                try:
                    tokens = shlex.split(interior, posix=False)
                except ValueError:
                    tokens = interior.split()
                _comando_cmd(tokens, linea, ctx, colector)
                break
        return True
    if nombre in ("powershell", "pwsh"):
        colector.agregar(nombre, linea, lenguaje="powershell")
        if any(re.match(r"^-(?:e|ec|en|enc|enco|encod|encode|encoded|encodedc\w*)$", a) for a in bajos):
            colector.agregar("-encodedcommand", linea, lenguaje="powershell")
        elif any(a in ("-c", "-command", "-com", "-comm", "-comma", "-comman") for a in bajos):
            colector.agregar("& $variable", linea, lenguaje="powershell")
        return True
    if nombre in ("del", "erase", "rd", "rmdir") and literales:
        colector.agregar("remove-item", linea, lenguaje="powershell")
        if any(a in ("/s", "-s") for a in bajos) and any(_es_raiz_de_borrado(lit) for lit in literales
                                                          if not lit.startswith("/")):
            colector.agregar("remove-item+raiz", linea, lenguaje="powershell")
        return True
    if nombre == "runas":
        colector.agregar("runas", linea, lenguaje="powershell")
        return True
    if nombre in ("format", "diskpart", "cipher"):
        if nombre == "cipher" and not any(a.startswith("/w") for a in bajos):
            return True
        colector.agregar(nombre, linea, lenguaje="powershell")
        return True
    if nombre in ("wmic",):
        colector.agregar("wmic", linea, lenguaje="powershell")
        if "shadowcopy" in texto and "delete" in texto:
            colector.agregar("vssadmin", linea, lenguaje="powershell")
        return True
    if ("powershell", nombre) in _FILAS:
        colector.agregar(nombre, linea, lenguaje="powershell")
        return True
    return False


def _comando_cmd(tokens: Sequence[str], linea: int | None, ctx: _CtxRutas, colector: _Colector) -> None:
    """Comando dentro de ``cmd /c "…"``: se separa por ``&``, ``&&``, ``|`` y se analiza cada parte."""
    actual: list[str] = []
    for token in [*tokens, "&"]:
        if token in ("&", "&&", "||", "|"):
            if actual:
                nombre = _nombre_comando(actual[0])
                bajos = [t.lower() for t in actual[1:]]
                for palabra in actual:
                    _registrar_ruta(palabra.strip("\"'"), linea, ctx, colector)
                if not _clasificar_externo_ps(nombre, bajos, [t.strip("\"'") for t in actual[1:]], linea, ctx,
                                              colector):
                    _clasificar_comando_bash(nombre, actual[1:], linea, ctx, colector,
                                             texto_linea=" ".join(actual), funciones=frozenset(), anidado=True)
            actual = []
        else:
            actual.append(token)


class _ContextoPS:
    """Datos compartidos del análisis de PowerShell."""

    def __init__(self, codigo: str, ctx: _CtxRutas, colector: _Colector) -> None:
        self.ctx = ctx
        self.col = colector
        self.sin_comentarios, self.sin_cadenas, self.cadenas = _ps_limpiar(codigo)
        self.lineas = self.sin_comentarios.split("\n")
        self.lineas_sin_cadenas = self.sin_cadenas.split("\n")
        self.funciones = frozenset(n.lower() for n in _RE_FUNCION_PS.findall(self.sin_comentarios))
        self.externo = bool(_RE_EXTERNO_PS.search(self.sin_comentarios))
        self.iex = bool(_RE_IEX_TEXTO.search(self.sin_cadenas))
        self.descarga = False
        self.base64 = False

    def linea(self, numero: int | None) -> str:
        if numero is None or numero < 1 or numero > len(self.lineas):
            return ""
        return self.lineas[numero - 1]

    def argumentos(self, numero: int | None, nombre: str) -> str:
        """Texto de los argumentos de ``nombre`` en esa línea (hasta el siguiente ``|``, ``;`` o ``}``
        fuera de comillas). Si no lo encuentra, la línea entera (fail-closed)."""
        if numero is None or numero < 1 or numero > len(self.lineas):
            return ""
        linea = self.lineas[numero - 1]
        sin_cadenas = self.lineas_sin_cadenas[numero - 1] if numero <= len(self.lineas_sin_cadenas) else linea
        posicion = sin_cadenas.lower().find(nombre.lower()) if nombre else -1
        if posicion < 0:
            return linea
        inicio = posicion + len(nombre)
        fin = len(linea)
        for separador in ("|", ";", "}"):
            encontrado = sin_cadenas.find(separador, inicio)
            if encontrado >= 0:
                fin = min(fin, encontrado)
        return linea[inicio:fin]


def _destino_ps(literales: Sequence[str], argumentos: str, contexto: _ContextoPS, linea: int | None) -> str:
    """Clase del destino de un comando de archivos: ``trabajo`` | ``usuario`` | ``riesgo`` | ``desconocido``.

    Una ruta literal de riesgo o de tus carpetas decide; si los argumentos usan variables (``$ruta``), el
    destino es desconocido; si solo hay literales relativos, es la carpeta de trabajo."""
    clases = []
    relativa = False
    for ruta in (lit for lit in literales if lit and not lit.startswith("-")):
        if _parece_ruta(ruta):
            clase = clasificar_ruta(ruta, contexto.ctx)
        else:
            clase = "trabajo"
            relativa = True
        if clase in _CLASES_CON_RIESGO:
            contexto.col.agregar(clase, linea, lenguaje="*")
            clase = "riesgo"
        clases.append(clase)
    if "riesgo" in clases:
        return "riesgo"
    if "usuario" in clases or _RE_USUARIO_PS.search(argumentos):
        return "usuario"
    if _RE_VARIABLE_PS.search(argumentos) or "(" in argumentos:
        return "desconocido"
    if contexto.ctx.cwd_fuera and (relativa or not clases):
        return "desconocido"     # tras un Set-Location fuera de la carpeta de trabajo
    return "trabajo"


_RE_CAMBIO_CARPETA_TEXTO_PS: Final = re.compile(r"currentdirectory\s*=(?!=)|setcurrentdirectory\s*\(",
                                                re.IGNORECASE)


def _cambia_carpeta_ps(comando: Mapping[str, Any], contexto: _ContextoPS) -> bool:
    """¿``Set-Location``/``Push-Location`` (``cd``, ``sl``, ``chdir``, ``pushd``) lleva a otra carpeta que
    no es la de trabajo (o a una desconocida)? ``$PSScriptRoot`` y las subcarpetas relativas no cuentan."""
    nombre_crudo = comando.get("nombre")
    if comando.get("dinamico") or not isinstance(nombre_crudo, str) or not nombre_crudo.strip():
        return False
    base = _nombre_comando(nombre_crudo)
    if _ALIAS_PS.get(base, base) not in ("set-location", "push-location"):
        return False
    linea = _linea_ps(comando.get("linea"))
    literales = [_texto_ps(lit) for lit in _lista(comando.get("literales"))]
    objetivos = [lit for lit in literales if lit and not lit.startswith("-")]
    for objetivo in objetivos:
        if objetivo.strip() in ("-", "+"):
            return True       # cd - / cd +: la carpeta anterior o la siguiente del historial
        if _parece_ruta(objetivo) and clasificar_ruta(objetivo, contexto.ctx) != "trabajo":
            return True
    argumentos = re.sub(r"\$psscriptroot\b", "", contexto.argumentos(linea, nombre_crudo.strip()),
                        flags=re.IGNORECASE)
    return bool(_RE_VARIABLE_PS.search(argumentos) or "(" in argumentos)


def _comando_ps(comando: Mapping[str, Any], contexto: _ContextoPS) -> None:
    col = contexto.col
    linea = _linea_ps(comando.get("linea"))
    texto_linea = contexto.linea(linea)
    nombre_crudo = comando.get("nombre")
    dinamico = bool(comando.get("dinamico")) or not isinstance(nombre_crudo, str) or not nombre_crudo.strip()
    params = [_texto_ps(p).lower() for p in _lista(comando.get("params"))]
    literales = [_texto_ps(lit) for lit in _lista(comando.get("literales"))]
    bajos = [lit.lower() for lit in literales]
    for literal in literales:
        _registrar_ruta(literal, linea, contexto.ctx, col)
    if dinamico:
        col.agregar("& $variable", linea, lenguaje="powershell")
        # Nombre construido que acaba en Invoke-Expression: & ('ie'+'x'), &(gcm i*x)…
        cabeza = re.sub(r"['\"`+\s(),\[\]]", "", texto_linea)
        if _RE_DINAMICO_IEX.search(cabeza):
            col.agregar("dinamico+iex", linea, lenguaje="powershell")
        return
    crudo = nombre_crudo.strip()
    es_ruta = "\\" in crudo or "/" in crudo
    base = _nombre_comando(crudo)
    canonico = _ALIAS_PS.get(base, base)
    if es_ruta:
        _registrar_ruta(crudo, linea, contexto.ctx, col)
    if canonico in contexto.funciones:
        return
    # Ejecutables de Windows con contexto (netsh, schtasks, reg, cmd, powershell…).
    if canonico in ("netsh", "schtasks", "reg", "cmd", "powershell", "pwsh", "runas", "format", "diskpart",
                    "cipher", "wmic") or (canonico in ("del", "erase", "rd", "rmdir") and es_ruta):
        _clasificar_externo_ps(canonico, bajos, literales, linea, contexto.ctx, col)
        return
    if _instala_paquetes([canonico, *bajos]):
        col.agregar("instalar", linea, lenguaje="*")
    if canonico == "remove-item":
        recursivo = any("recurse".startswith(p) and p for p in params)
        objetivos = [lit for lit in literales if not lit.startswith("-")]
        raiz = any(_es_raiz_de_borrado(o) for o in objetivos)
        raiz = raiz or bool(re.search(r"remove-item\b[^|;]*\$env:\w+\s*(?:[|;]|$)", texto_linea, re.IGNORECASE))
        raiz = raiz or bool(re.search(r"(?:\b(?:rm|del|ri|rd|rmdir|erase|remove-item)\b)[^|;]*"
                                      r"(?:\$env:(?:userprofile|systemdrive|windir|homedrive)|\$home)\b\s*"
                                      r"(?:\\\*?)?\s*(?:-|[|;]|$)", texto_linea, re.IGNORECASE))
        if (recursivo or any("*" in o for o in objetivos)) and raiz:
            col.agregar("remove-item+raiz", linea, lenguaje="powershell")
        elif raiz and any(_RE_RAIZ_UNIDAD.match(_normalizar(o)) for o in objetivos):
            col.agregar("remove-item+raiz", linea, lenguaje="powershell")
        col.agregar("remove-item", linea, lenguaje="powershell")
        return
    if canonico == "start-process":
        if "runas" in bajos or re.search(r"-verb\s+['\"]?runas", texto_linea, re.IGNORECASE):
            col.agregar("start-process+runas", linea, lenguaje="powershell")
            return
        objetivo = next((lit for lit in literales if lit and not lit.startswith("-")), None)
        argumentos_extra = [lit for lit in literales[1:] if lit.startswith("-")]
        if objetivo is None:
            col.agregar("start-process", linea, lenguaje="powershell")
            return
        if re.match(r"^https?://", objetivo, re.IGNORECASE):
            return
        nombre_objetivo = _nombre_comando(objetivo)
        if nombre_objetivo in _APPS_CONOCIDAS and not argumentos_extra:
            return
        try:
            anidado = shlex.split(" ".join(literales), posix=False)
        except ValueError:
            anidado = literales
        if not _clasificar_externo_ps(nombre_objetivo, [t.lower() for t in anidado[1:]],
                                      [t.strip("\"'") for t in anidado[1:]], linea, contexto.ctx, col):
            col.agregar("start-process", linea, lenguaje="powershell")
        return
    if canonico == "new-object":
        texto = " ".join(bajos) + " " + texto_linea.lower()
        if "-comobject" in texto_linea.lower() or any(p.startswith("com") for p in params):
            for clave, patron in _COM_PS.items():
                if clave in texto:
                    col.agregar(patron, linea, lenguaje="powershell" if patron != "escritura+usuario" else "*")
                    if patron == "net.webclient":
                        contexto.descarga = True
            return
        for fragmento, patron in _TIPOS_PS:
            if fragmento in texto:
                col.agregar(patron, linea, lenguaje="powershell")
                if patron == "net.webclient":
                    contexto.descarga = True
        return
    if canonico in ("invoke-webrequest", "invoke-restmethod", "start-bitstransfer"):
        contexto.descarga = True
        col.agregar(canonico, linea, lenguaje="powershell")
        return
    argumentos = contexto.argumentos(linea, crudo)
    if canonico in _LECTORES_PS:
        destino = _destino_ps(literales, argumentos, contexto, linea)
        if destino in ("usuario", "riesgo") or (destino == "desconocido" and contexto.externo):
            col.agregar(canonico, linea, lenguaje="powershell")
        return
    if canonico in _ESCRITORES_PS or canonico == "new-item":
        destino = _destino_ps(literales, argumentos, contexto, linea)
        forzado = any("force".startswith(p) and p for p in params)
        if destino in ("riesgo",) or (destino == "usuario" and (canonico != "new-item" or forzado)) \
                or (destino == "desconocido" and contexto.externo):
            col.agregar("escritura+usuario", linea, lenguaje="*")
        else:
            col.agregar(canonico, linea, lenguaje="powershell")
        return
    if canonico in _MOVER_PS:
        destino = _destino_ps(literales, argumentos, contexto, linea)
        comodin = any(c in lit for lit in literales for c in "*?[")
        if comodin or destino in ("usuario", "riesgo") or (destino == "desconocido" and contexto.externo):
            col.agregar("mover+usuario", linea, lenguaje="*")
        else:
            col.agregar(canonico, linea, lenguaje="powershell")
        return
    if canonico in ("set-itemproperty", "new-itemproperty", "remove-itemproperty"):
        col.agregar(canonico, linea, lenguaje="powershell")
        return
    if ("powershell", canonico) in _FILAS:
        col.agregar(canonico, linea, lenguaje="powershell")
        return
    if canonico in _SEGUROS_PS or canonico in _APPS_CONOCIDAS:
        return
    if "-" in canonico:
        verbo = canonico.split("-", 1)[0]
        if verbo in _VERBOS_SEGUROS_PS:
            return
        col.agregar("cmdlet_desconocido", linea, lenguaje="powershell")
        return
    if canonico.endswith((".ps1", ".bat", ".cmd", ".vbs", ".js", ".msi", ".lnk", ".hta", ".scr")) or es_ruta:
        col.agregar("ejecutable", linea, lenguaje="powershell")
        return
    if canonico in ("if", "else", "elseif", "foreach", "for", "while", "do", "switch", "try", "catch",
                    "finally", "function", "filter", "param", "return", "exit", "break", "continue", "throw",
                    "trap", "until", "class", "enum", "using", "begin", "process", "end", "data", "dynamicparam"):
        return
    col.agregar("ejecutable", linea, lenguaje="powershell")


_RE_FUENTE_ARCHIVOS_PS: Final = re.compile(
    r"(?<![\w$-])(?:get-item|gi|get-childitem|gci|dir|ls)(?![\w-])|io\.(?:file|directory)|fileinfo|directoryinfo"
    r"|filesysteminfo", re.IGNORECASE)


def _objeto_de_archivo(objeto: str, contexto: _ContextoPS) -> bool:
    """¿El objeto de un método .NET puede ser un archivo o una carpeta (``FileInfo``/``DirectoryInfo``)?
    Por la expresión (``(Get-Item x)``, ``[IO.FileInfo]``…) o, si es una variable suelta, por cómo se
    asigna en el script (``$f = Get-Item …``, ``foreach ($f in Get-ChildItem …)``, ``[IO.FileInfo]$f``).
    ``$_``/``$PSItem`` cuentan si el script lista archivos. Las propiedades (``$_.Name.Replace``) no."""
    if _RE_FUENTE_ARCHIVOS_PS.search(objeto):
        return True
    variable = re.fullmatch(r"\$\{?([\w:]+)\}?", objeto.strip())
    if variable is None:
        return False
    nombre = variable.group(1).lower()
    texto = contexto.sin_comentarios
    if nombre in ("_", "psitem"):
        return bool(_RE_FUENTE_ARCHIVOS_PS.search(texto))
    fuente = r"\(?\s*(?:get-item|gi|get-childitem|gci|dir|ls|\[(?:system\.)?io\.(?:file|directory)info\]" \
             r"|new-object\s+(?:-typename\s+)?(?:system\.)?io\.(?:file|directory)info)(?![\w-])"
    variable_re = re.escape(nombre)
    patrones = (rf"\${variable_re}\s*=\s*{fuente}",
                rf"foreach\s*\(\s*\${variable_re}\s+in\s+{fuente}",
                rf"\[(?:system\.)?io\.(?:file|directory|filesystem)info\]\s*\${variable_re}(?![\w:])")
    return any(re.search(patron, texto, re.IGNORECASE) for patron in patrones)


def _miembro_ps(miembro: Mapping[str, Any], contexto: _ContextoPS) -> None:
    col = contexto.col
    linea = _linea_ps(miembro.get("linea"))
    objeto_crudo = _texto_ps(miembro.get("objeto"))
    objeto = objeto_crudo.lower().replace(" ", "")
    nombre = _texto_ps(miembro.get("miembro")).lower().strip("'\"")
    if nombre.startswith("$"):
        col.agregar("& $variable", linea, lenguaje="powershell")
        return
    if nombre == "frombase64string":
        contexto.base64 = True
        col.agregar("frombase64string", linea, lenguaje="powershell")
    elif nombre in ("downloadstring", "downloaddata", "openread"):
        contexto.descarga = True
        col.agregar("downloadstring", linea, lenguaje="powershell")
    elif nombre in ("downloadfile", "uploadfile", "uploadstring", "uploaddata", "getasync", "postasync",
                    "getstringasync", "getbytearrayasync", "getresponse", "connect", "sendasync"):
        contexto.descarga = True
        col.agregar("downloadfile", linea, lenguaje="powershell")
    elif nombre in ("load", "loadfrom", "loadfile", "loadwithpartialname", "unsafeloadfrom") and "assembly" in objeto:
        col.agregar("[reflection.assembly]::load", linea, lenguaje="powershell")
    elif "interopservices.marshal" in objeto:
        col.agregar("[runtime.interopservices.marshal]", linea, lenguaje="powershell")
    elif nombre == "setenvironmentvariable":
        col.agregar("setenvironmentvariable", linea, lenguaje="powershell")
        if "machine" in contexto.linea(linea).lower():
            col.agregar("hklm", linea, lenguaje="*")
    elif nombre in ("create", "invoke", "invokereturnasis", "invokescript", "newscriptblock") and (
            "scriptblock" in objeto or "invokecommand" in objeto or "executioncontext" in objeto):
        col.agregar("[scriptblock]::create", linea, lenguaje="powershell")
    elif nombre == "start" and "diagnostics.process" in objeto:
        col.agregar("[diagnostics.process]::start", linea, lenguaje="powershell")
    elif nombre in ("readkey", "readline", "promptforchoice", "promptforcredential") and (
            "console" in objeto or "host" in objeto):
        col.agregar("readkey", linea, lenguaje="powershell")
    elif nombre in ("sendwait", "send") and "sendkeys" in objeto:
        col.agregar("sendkeys", linea, lenguaje="powershell")
    elif nombre == "delete":
        # FileInfo/DirectoryInfo.Delete(), [IO.File]::Delete… sobre CUALQUIER objeto (fail-closed): un
        # Delete() de otra cosa (una fila, un elemento COM) también borra.
        col.agregar("remove-item", linea, lenguaje="powershell")
    elif nombre == "moveto" or (nombre in ("move", "replace") and _objeto_de_archivo(objeto_crudo, contexto)):
        col.agregar("mover+usuario", linea, lenguaje="*")
    elif nombre == "copyto" and _objeto_de_archivo(objeto_crudo, contexto):
        externo = contexto.externo or contexto.ctx.cwd_fuera
        col.agregar("escritura+usuario" if externo else "escritura+trabajo", linea, lenguaje="*")
    elif nombre in ("encrypt", "decrypt") and _objeto_de_archivo(objeto_crudo, contexto):
        col.agregar("attrib", linea, lenguaje="powershell")
    elif nombre == "setaccesscontrol":
        col.agregar("set-acl", linea, lenguaje="powershell")
    elif nombre.startswith(("writeall", "appendall", "create", "copy", "open")) and "io.file" in objeto:
        col.agregar("escritura+usuario" if contexto.externo else "escritura+trabajo", linea, lenguaje="*")
    elif "registry" in objeto and nombre in ("setvalue", "deletevalue", "deletesubkey", "deletesubkeytree",
                                              "createsubkey", "opensubkey", "getvalue"):
        col.agregar("reg", linea, lenguaje="powershell")
        if "localmachine" in objeto or "localmachine" in contexto.linea(linea).lower():
            col.agregar("hklm", linea, lenguaje="*")


def _cierre_ps(contexto: _ContextoPS) -> None:
    col = contexto.col
    lineas_sin = contexto.sin_cadenas.split("\n")
    for numero, linea in enumerate(lineas_sin, start=1):
        if _RE_ENCODED.search(contexto.linea(numero)) and re.search(r"powershell|pwsh", contexto.linea(numero),
                                                                     re.IGNORECASE):
            col.agregar("-encodedcommand", numero, lenguaje="powershell")
        if re.search(r"\$executioncontext\b", linea, re.IGNORECASE):
            col.agregar("[scriptblock]::create", numero, lenguaje="powershell")
        if re.search(r"(?<![\w-])&\s*\(\s*['\"]", contexto.linea(numero)):
            col.agregar("& $variable", numero, lenguaje="powershell")
        if re.search(r"(?<![\w-])(?:iex|invoke-expression)(?![\w-])", linea, re.IGNORECASE):
            col.agregar("invoke-expression", numero, lenguaje="powershell")
        if re.search(r"\.\s*(?:attributes|isreadonly)\s*[-+]?=(?!=)", linea, re.IGNORECASE):
            col.agregar("attrib", numero, lenguaje="powershell")   # $f.Attributes = …, $f.IsReadOnly = $false
    if contexto.iex:
        linea_iex = col.lineas.get("invoke-expression")
        if contexto.descarga or re.search(r"downloadstring|invoke-webrequest|\biwr\b|invoke-restmethod|\birm\b"
                                          r"|net\.webclient|start-bitstransfer", contexto.sin_comentarios,
                                          re.IGNORECASE):
            col.agregar("descarga+iex", linea_iex, lenguaje="powershell")
        if contexto.base64 or re.search(r"frombase64string", contexto.sin_comentarios, re.IGNORECASE):
            col.agregar("frombase64string+iex", linea_iex, lenguaje="powershell")
        if _RE_CHAR_ARRAY.search(contexto.sin_comentarios):
            col.agregar("char+iex", linea_iex, lenguaje="powershell")
    if re.search(r"frombase64string", contexto.sin_comentarios, re.IGNORECASE) and not col.tiene("frombase64string"):
        col.agregar("frombase64string", None, lenguaje="powershell")
    # Rutas: literales entre comillas y rutas sueltas (C:\…, $env:…, %VAR%, ~\…).
    for numero, contenido in contexto.cadenas:
        if len(contenido) <= 2048:
            _registrar_ruta(contenido, numero, contexto.ctx, col)
            for trozo in re.split(r"[\s;,|]+", contenido):
                if trozo and trozo != contenido and _parece_ruta(trozo):
                    _registrar_ruta(trozo, numero, contexto.ctx, col)
    for numero, linea in enumerate(lineas_sin, start=1):
        for suelta in re.finditer(r"(?<![\w$:])(?:[A-Za-z]:[\\/][^\s'\"`;|,)(<>{}]*|\$env:[\w()]+[^\s'\"`;|,)(<>{}]*"
                                  r"|\$\{env:[^}]+\}[^\s'\"`;|,)(<>{}]*|%\w+%[^\s'\"`;|,)(<>{}]*"
                                  r"|~[\\/][^\s'\"`;|,)(<>{}]*|\$home\b[^\s'\"`;|,)(<>{}]*)", linea, re.IGNORECASE):
            _registrar_ruta(suelta.group(0), numero, contexto.ctx, col)
    _escaneo_comun(contexto.sin_comentarios, col)
    _cierre_credenciales(contexto.sin_comentarios, col)


def _analizar_ps(datos: Mapping[str, Any], codigo: str, ctx: _CtxRutas, metodo: str) -> Analisis:
    colector = _Colector("powershell")
    contexto = _ContextoPS(codigo, ctx, colector)
    errores = _lista(datos.get("errores"))
    comandos = [comando for comando in _lista(datos.get("comandos")) if isinstance(comando, Mapping)]
    if _RE_CAMBIO_CARPETA_TEXTO_PS.search(contexto.sin_comentarios) \
            or any(_cambia_carpeta_ps(comando, contexto) for comando in comandos):
        ctx.cwd_fuera = True
        contexto.externo = True
    for comando in comandos:
        _comando_ps(comando, contexto)
    for miembro in _lista(datos.get("miembros")):
        if isinstance(miembro, Mapping):
            _miembro_ps(miembro, contexto)
    for tipo in _lista(datos.get("tipos")):
        nombre = _texto_ps(tipo).lower()
        for fragmento, patron in _TIPOS_PS:
            if fragmento in nombre:
                colector.agregar(patron, None, lenguaje="powershell")
                if patron == "net.webclient":
                    contexto.descarga = True
    for using in _lista(datos.get("usings")):
        texto = _texto_ps(using).lower()
        if re.match(r"^\s*using\s+(?:assembly|module)\b", texto):
            colector.agregar("using assembly", None, lenguaje="powershell")
    for variable in _lista(datos.get("variables")):
        if _texto_ps(variable).lower() == "executioncontext":
            colector.agregar("[scriptblock]::create", None, lenguaje="powershell")
    _cierre_ps(contexto)
    sintaxis_ok = not errores
    if errores:
        primero = errores[0] if isinstance(errores[0], Mapping) else {}
        colector.agregar("sintaxis", _linea_ps(primero.get("linea")), lenguaje="*")
    minimo = "bajo"
    if metodo == "heuristico":
        colector.agregar("degradado", None, lenguaje="*")
        minimo = "alto"
    return colector.analisis(metodo, sintaxis_ok, minimo)


def _datos_validos(datos: Any) -> bool:
    if not isinstance(datos, Mapping):
        return False
    return all(k in datos for k in ("errores", "comandos", "miembros"))


def analizar_powershell_ast(datos: Mapping[str, Any], codigo: str, *, carpetas: Sequence[Path],
                            protegidas: Sequence[Path] = ()) -> Analisis:
    """Analiza un script de PowerShell a partir del JSON de ``taller_analizar.ps1`` (``{errores, comandos,
    miembros, tipos, variables, usings}``) y del propio texto. Puro; nunca lanza. Si el JSON no tiene la
    forma esperada, cae al análisis heurístico (nivel mínimo ``alto``)."""
    if not isinstance(codigo, str) or _demasiado_grande(codigo):
        return _analisis_tamano("ast")
    if not _datos_validos(datos):
        return analizar_powershell_heuristico(codigo, carpetas=carpetas, protegidas=protegidas)
    try:
        return _analizar_ps(datos, codigo, _CtxRutas(carpetas, protegidas), "ast")
    except Exception as exc:  # defensivo: nunca baja el nivel
        log.debug("Análisis de PowerShell (AST) fallido (%s)", type(exc).__name__)
        return analizar_powershell_heuristico(codigo, carpetas=carpetas, protegidas=protegidas)


_RE_POSICION_COMANDO_PS: Final = re.compile(
    r"(?:^|[;|{}(=]|&&|\|\||\bin\b)\s*(?P<op>[&.](?=\s*[\w$('\"\\]))?\s*(?P<nombre>\$[\w:{}]+|\((?:[^()\n]|\([^()\n]*\))*\)"
    r"|'[^'\n]*'|\"[^\"\n]*\"|[\w.\\/:?%-]+)", re.MULTILINE)


def _datos_ps_heuristicos(codigo: str) -> dict[str, Any]:
    """Construye un JSON como el del analizador del repo a partir del texto (sin AST)."""
    sin_comentarios, sin_cadenas, _ = _ps_limpiar(codigo)
    comandos: list[dict[str, Any]] = []
    miembros: list[dict[str, Any]] = []
    tipos: set[str] = set()
    usings: list[str] = []
    lineas_sin = sin_cadenas.split("\n")
    lineas_con = sin_comentarios.split("\n")
    for numero, (linea, original) in enumerate(zip(lineas_sin, lineas_con), start=1):
        if re.match(r"^\s*using\s+", linea, re.IGNORECASE):
            usings.append(linea.strip())
        for coincidencia in _RE_POSICION_COMANDO_PS.finditer(linea):
            nombre_sin = coincidencia.group("nombre")
            operador = coincidencia.group("op")
            inicio, fin_nombre = coincidencia.start("nombre"), coincidencia.end("nombre")
            nombre = original[inicio:fin_nombre]
            fin = len(linea)
            for separador in (";", "|", "}", "{"):
                posicion = linea.find(separador, fin_nombre)
                if posicion >= 0:
                    fin = min(fin, posicion)
            resto_original = original[fin_nombre:fin]
            if linea[fin_nombre:].lstrip().startswith("=") and not linea[fin_nombre:].lstrip().startswith("=="):
                continue   # clave de una tabla hash o asignación: no es un comando
            dinamico = False
            if nombre_sin.startswith(("'", '"')):
                if not operador:
                    continue   # una cadena suelta es una expresión, no un comando
                nombre = nombre[1:-1]
            elif nombre_sin.startswith("$") or nombre_sin.startswith("("):
                if not operador:
                    continue
                dinamico = True
            if re.fullmatch(r"[\d.]+", nombre_sin) or nombre_sin.startswith("-") or nombre_sin.startswith("["):
                continue
            literales = []   # en orden: cadenas entre comillas y palabras sueltas (no parámetros ni variables)
            for simple, doble, suelta in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"|([^\s'\"]+)", resto_original):
                if suelta:
                    if suelta[0] not in "-$(@[{`,)" and not re.fullmatch(r"[\d.]+", suelta):
                        literales.append(suelta)
                else:
                    literales.append(simple or doble)
            params = re.findall(r"(?<![\w$])-([A-Za-z][\w]*)", linea[fin_nombre:fin])
            comandos.append({"nombre": None if dinamico else nombre, "dinamico": dinamico,
                             "operador": "Ampersand" if operador == "&" else ("Dot" if operador == "." else "Unknown"),
                             "params": params, "literales": literales, "linea": numero})
        for coincidencia in re.finditer(r"(\[[\w.+`]+\])\s*::\s*(\w+)\s*\(", original):
            miembros.append({"objeto": coincidencia.group(1), "miembro": coincidencia.group(2), "estatico": True,
                             "linea": numero})
        for coincidencia in re.finditer(r"(\$[\w:]+(?:\.\w+)*|\))\s*\.\s*(\w+)\s*\(", original):
            miembros.append({"objeto": coincidencia.group(1), "miembro": coincidencia.group(2), "estatico": False,
                             "linea": numero})
        for coincidencia in re.finditer(r"\[([A-Za-z_][\w.+`]*(?:\[\])?)\]", original):
            tipos.add(coincidencia.group(1))
    return {"errores": [], "comandos": comandos, "miembros": miembros, "tipos": sorted(tipos), "variables": [],
            "usings": usings}


def analizar_powershell_heuristico(codigo: str, *, carpetas: Sequence[Path], protegidas: Sequence[Path] = ()) -> Analisis:
    """Análisis de PowerShell sin el analizador del repo (método ``heuristico``): nivel mínimo ``alto``
    y hallazgo ``analisis_degradado``. Puro; nunca lanza."""
    if not isinstance(codigo, str) or _demasiado_grande(codigo):
        return _analisis_tamano("heuristico")
    ctx = _CtxRutas(carpetas, protegidas)
    try:
        return _analizar_ps(_datos_ps_heuristicos(codigo), codigo, ctx, "heuristico")
    except Exception as exc:  # defensivo
        log.debug("Análisis heurístico de PowerShell fallido (%s)", type(exc).__name__)
        colector = _Colector("powershell")
        colector.agregar("degradado", None, lenguaje="*")
        return colector.analisis("heuristico", True, "alto")


# ---------------------------------------------------------------------------
# Plataforma: intérpretes, entorno, elevación y límites
# ---------------------------------------------------------------------------


def detectar_interpretes(plataforma: str) -> dict[str, str]:
    """Rutas de los intérpretes disponibles: ``{"powershell"|"bash": ruta, "python": sys.executable}``.

    En Windows se usa siempre ``powershell.exe`` (5.1), nunca ``pwsh``. Nunca lanza."""
    encontrados: dict[str, str] = {}
    try:
        if sys.executable and Path(sys.executable).is_file():
            encontrados["python"] = sys.executable
        if plataforma == "win32":
            ruta = shutil.which("powershell.exe") or shutil.which("powershell")
            if not ruta:
                raiz = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
                if raiz:
                    candidata = Path(raiz) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
                    if candidata.is_file():
                        ruta = str(candidata)
            if ruta:
                encontrados["powershell"] = ruta
        elif str(plataforma).startswith("linux"):
            ruta = shutil.which("bash")
            if ruta:
                encontrados["bash"] = ruta
    except Exception as exc:  # entorno exótico: lo que se haya encontrado
        log.debug("Detección de intérpretes incompleta (%s)", type(exc).__name__)
    return encontrados


def entorno_saneado(plataforma: str, carpeta_tmp: Path, origen: Mapping[str, str] | None = None) -> dict[str, str]:
    """Entorno de LISTA BLANCA para el script (§9.6 paso 5). ``origen`` = ``os.environ`` por defecto.

    Nunca incluye nada de ``Settings`` ni del ``.env``; sin ``APPDATA``/``LOCALAPPDATA`` (Windows) ni
    ``DBUS_SESSION_BUS_ADDRESS`` (Linux); ``TEMP=TMP=TMPDIR=<carpeta>/tmp``; sin ``PYTHONUTF8`` ni
    ``PYTHONIOENCODING`` (con ``-I`` serían letra muerta)."""
    fuente = os.environ if origen is None else origen
    windows = plataforma == "win32"
    lista = ENTORNO_WINDOWS if windows else ENTORNO_LINUX
    por_nombre: dict[str, str] = {}
    for clave, valor in fuente.items():
        if not isinstance(clave, str) or not isinstance(valor, str):
            continue
        por_nombre.setdefault(clave.upper() if windows else clave, valor)
    entorno = {nombre: por_nombre[nombre] for nombre in lista if nombre in por_nombre}
    tmp = str(carpeta_tmp)
    entorno["TEMP"] = tmp
    entorno["TMP"] = tmp
    entorno["TMPDIR"] = tmp
    return entorno


def es_elevado_por_defecto(plataforma: str | None = None) -> bool | None:
    """``True`` si R.A.M.S. corre como administrador (Windows: ``IsUserAnAdmin``; Linux: euid 0).
    ``None`` si no se puede comprobar (y eso cuenta como «elevado»)."""
    sistema = sys.platform if plataforma is None else plataforma
    try:
        if sistema == "win32":
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        geteuid = getattr(os, "geteuid", None)
        if geteuid is None:
            return None
        return geteuid() == 0
    except Exception as exc:  # sin ctypes, sin shell32…: no se pudo comprobar
        log.debug("No pude comprobar si R.A.M.S. corre elevado (%s)", type(exc).__name__)
        return None


async def _ejecutar_breve(argv: Sequence[str], *, timeout_s: float, env: Mapping[str, str] | None = None,
                          max_salida: int = 4096) -> tuple[int | None, bytes]:
    """Lanza un comando propio (sonda, systemctl) sin shell, con plazo; devuelve ``(código, stdout)``."""
    proceso = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=dict(env) if env is not None else None)
    acumulador = _Acumulador(max_salida)
    try:
        await asyncio.wait_for(asyncio.gather(acumulador.leer(proceso.stdout), proceso.wait()), timeout_s)
        return proceso.returncode, bytes(acumulador.datos)
    except (TimeoutError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError, OSError):
            proceso.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proceso.wait(), 2.0)
        return None, b""


async def sonda_limites_linux() -> str:
    """Modo de límites de Linux (§9.6 paso 4): ``"cgroups"`` si ``systemd-run --user --scope`` aplica de
    verdad ``MemoryMax`` y ``TasksMax`` (la sonda lee su propio cgroup), ``"prlimit"`` si solo hay
    ``prlimit`` y ``"ninguno"`` si no hay nada. Nunca lanza (salvo la cancelación)."""
    try:
        systemd_run = shutil.which("systemd-run")
        if systemd_run:
            argv = [systemd_run, "--user", "--scope", "--quiet", "--collect", "-p", "TasksMax=4", "-p",
                    "MemoryMax=64M", "--", sys.executable, "-I", "-c", SONDA_CGROUP]
            codigo, salida = await _ejecutar_breve(argv, timeout_s=PLAZO_SONDA_S,
                                                   env=entorno_saneado("linux", Path("/tmp")))
            lineas = salida.decode("utf-8", "replace").split()
            if codigo == 0 and lineas[:2] == [_SONDA_MEMORIA_ESPERADA, _SONDA_PROCESOS_ESPERADOS]:
                return "cgroups"
        if shutil.which("prlimit"):
            return "prlimit"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("Sonda de límites fallida (%s)", type(exc).__name__)
    return "ninguno"


# ---------------------------------------------------------------------------
# Lanzadores por defecto
# ---------------------------------------------------------------------------


class _Acumulador:
    """Lee un flujo por bloques de 64 KiB guardando solo los primeros ``maximo`` bytes."""

    def __init__(self, maximo: int) -> None:
        self.maximo = max(0, int(maximo))
        self.datos = bytearray()
        self.total = 0

    async def leer(self, flujo: asyncio.StreamReader | None) -> None:
        if flujo is None:
            return
        while True:
            bloque = await flujo.read(BLOQUE_LECTURA)
            if not bloque:
                return
            self.total += len(bloque)
            hueco = self.maximo - len(self.datos)
            if hueco > 0:
                self.datos.extend(bloque[:hueco])


async def _esperar_salida(proceso: asyncio.subprocess.Process) -> None:
    """Espera a que salga el proceso **principal**. Desde Python 3.12, ``Process.wait()`` espera además a
    que se cierren sus tuberías, y un descendiente que las heredó puede tenerlas abiertas: aquí se mira
    ``returncode`` (que el transporte fija al salir el proceso) para no confundir las dos cosas."""
    espera = asyncio.ensure_future(proceso.wait())
    try:
        while not espera.done() and proceso.returncode is None:
            await asyncio.wait({espera}, timeout=_SONDEO_SALIDA_S)
    finally:
        if not espera.done():
            espera.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await espera


async def _esperar_proceso(proceso: asyncio.subprocess.Process, *, timeout_s: float, max_salida: int,
                           matar: Callable[[], Awaitable[None]],
                           al_terminar: Callable[[], Awaitable[None]] | None = None,
                           ) -> tuple[int | None, bool, float, _Acumulador, _Acumulador]:
    """Espera al proceso con plazo, leyendo de forma acotada. Al vencer llama a ``matar`` (todo el árbol);
    si el principal sale antes, llama a ``al_terminar`` (limpia lo que dejó vivo: §9.6 paso 6) y, si esa
    limpieza falla, a ``matar``."""
    salida = _Acumulador(max_salida)
    errores = _Acumulador(max_salida)
    lectores = [asyncio.ensure_future(salida.leer(proceso.stdout)),
                asyncio.ensure_future(errores.leer(proceso.stderr))]
    inicio = time.monotonic()
    agotado = False
    try:
        try:
            await asyncio.wait_for(_esperar_salida(proceso), timeout_s)
        except (TimeoutError, asyncio.TimeoutError):
            agotado = True
            await matar()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(_esperar_salida(proceso), PLAZO_MUERTE_S)
        duracion = time.monotonic() - inicio
        if not agotado and al_terminar is not None:
            try:
                await al_terminar()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # la limpieza falló: fail-closed, se mata todo el árbol
                log.debug("Limpieza tras la salida del script fallida (%s)", type(exc).__name__)
                await matar()
        plazo = PLAZO_MUERTE_S if agotado else max(0.05, min(GRACIA_LECTURA_S, timeout_s - duracion))
        _, pendientes = await asyncio.wait(lectores, timeout=plazo)
        for pendiente in pendientes:
            pendiente.cancel()
        if pendientes:
            await asyncio.gather(*pendientes, return_exceptions=True)
        return proceso.returncode, agotado, duracion, salida, errores
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(matar())
        for lector in lectores:
            lector.cancel()
        raise


# --- Qué sobrevive al script (§9.6 paso 6) ---------------------------------------------------------
#
# Al vencer el plazo (o al cancelar) se mata el árbol entero. Si el script termina **normalmente**, lo
# que haya dejado vivo también se mata (si no, un hijo desligado escaparía al tiempo límite y a los
# topes de procesos y memoria), con una excepción: las apps conocidas que el script abrió para el
# usuario (``Start-Process notepad``, el navegador…), las mismas que el analizador da por «bajo». Una app
# conocida cuenta solo si su ejecutable está instalado en una carpeta del sistema (que el usuario sin
# admin no puede escribir) o en ``%LOCALAPPDATA%\Programs`` (instalación por usuario, como VS Code): una
# copia de python.exe renombrada a notepad.exe en la carpeta de trabajo no pasa. Ante cualquier duda
# (no se pudo listar o comprobar un proceso, siguen apareciendo procesos ajenos) se mata TODO.

_APPS_CONOCIDAS_LINUX: Final = frozenset({
    "firefox", "firefox-esr", "librewolf", "chromium", "chrome", "google-chrome", "brave", "vivaldi-bin",
    "opera", "code", "codium", "gedit", "gnome-text-editor", "kate", "kwrite", "mousepad", "xed", "pluma",
    "nautilus", "dolphin", "thunar", "nemo", "pcmanfm", "pcmanfm-qt", "evince", "okular", "zathura", "atril",
    "eog", "loupe", "gwenview", "feh", "imv", "ristretto", "mpv", "vlc", "totem", "celluloid", "soffice.bin",
    "oosplash", "gimp", "inkscape", "krita", "spotify", "thunderbird", "gnome-calculator", "kcalc",
    "galculator", "qalculate-gtk",
})
_RAICES_APPS_LINUX: Final = ("/usr/", "/opt/", "/snap/")


def _raices_apps_windows() -> tuple[str, ...]:
    """Carpetas donde se instalan las apps conocidas (normalizadas con ``ntpath.normcase``)."""
    raices: list[str] = []
    for variable in ("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        valor = os.environ.get(variable)
        if valor and ntpath.isabs(valor):
            raices.append(ntpath.normcase(ntpath.normpath(valor)))
    local = os.environ.get("LOCALAPPDATA")
    if local and ntpath.isabs(local):
        raices.append(ntpath.normcase(ntpath.normpath(ntpath.join(local, "Programs"))))
    return tuple(raices)


def _app_conocida_windows(ruta: str, raices: Sequence[str] | None = None) -> bool:
    """¿El ejecutable ``ruta`` es una app conocida (``_APPS_CONOCIDAS``) instalada en una carpeta de
    apps? Puro salvo por las raíces (de ``os.environ`` si no se pasan)."""
    if not isinstance(ruta, str) or not ruta.strip() or not ntpath.isabs(ruta):
        return False
    normal = ntpath.normcase(ntpath.normpath(ruta))
    nombre = ntpath.basename(normal)
    if nombre.endswith(".exe"):
        nombre = nombre[:-4]
    if nombre.endswith(":") or nombre not in _APPS_CONOCIDAS:
        return False
    raices = _raices_apps_windows() if raices is None else tuple(ntpath.normcase(ntpath.normpath(r))
                                                                 for r in raices if r)
    return any(normal.startswith(raiz.rstrip("\\") + "\\") for raiz in raices)


def _app_conocida_linux(ruta: str) -> bool:
    """¿El ejecutable ``ruta`` es una app gráfica conocida instalada en ``/usr``, ``/opt`` o ``/snap``?"""
    if not isinstance(ruta, str) or not ruta.startswith(_RAICES_APPS_LINUX) or "/../" in ruta:
        return False
    return ruta.rsplit("/", 1)[-1].lower() in _APPS_CONOCIDAS_LINUX


def _limpiar_restantes(listar: Callable[[], Sequence[Any] | None], clasificar: Callable[[Any], str],
                       matar: Callable[[list[Any]], None], *, rondas: int = _RONDAS_LIMPIEZA,
                       plazo_s: float = PLAZO_MUERTE_S) -> int:
    """Política común tras una salida NORMAL del script. En cada ronda lista lo que sigue vivo del árbol
    y ``clasificar`` dice de cada uno ``"app"`` (app conocida: se conserva), ``"ajeno"`` (se mata),
    ``"fuera"`` (ya terminó o no es del árbol) o ``"dudoso"`` (no se pudo comprobar). Devuelve cuántas
    apps quedan vivas, o ``-1`` si no se pudo dejar limpio (no se pudo listar, algo dudoso, o siguen
    apareciendo procesos ajenos tras las rondas o el plazo): el llamador mata entonces el árbol ENTERO."""
    limite = time.monotonic() + plazo_s
    for _ in range(max(1, rondas)):
        restantes = listar()
        if restantes is None:
            return -1
        apps = 0
        ajenos: list[Any] = []
        for proceso in restantes:
            estado = clasificar(proceso)
            if estado == "app":
                apps += 1
            elif estado == "ajeno":
                ajenos.append(proceso)
            elif estado != "fuera":
                return -1
        if not ajenos:
            return apps
        if time.monotonic() > limite:
            return -1
        matar(ajenos)
    return -1


class _ApiJob:
    """Llamadas Win32 del Job Object (solo Windows; se cargan al primer uso). Van en el executor."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", wintypes.DWORD),
                ("NumberOfProcessIdsInList", wintypes.DWORD),
                ("ProcessIdList", ctypes.c_size_t * _MAX_PIDS_JOB),
            ]

        self._info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        self._lista_pids = JOBOBJECT_BASIC_PROCESS_ID_LIST
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.DWORD))
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._k32 = k32

    def crear(self, max_procesos: int, max_memoria_mb: int) -> int:
        """Job sin nombre (no heredable) con KILL_ON_JOB_CLOSE, procesos, memoria y sin diálogos de error."""
        job = self._k32.CreateJobObjectW(None, None)
        if not job:
            raise ErrorConfinamiento("CreateJobObjectW")
        info = self._info()
        info.BasicLimitInformation.LimitFlags = (_JOB_MATAR_AL_CERRAR | _JOB_LIMITE_PROCESOS
                                                 | _JOB_LIMITE_MEMORIA | _JOB_MORIR_EXCEPCION)
        info.BasicLimitInformation.ActiveProcessLimit = max(1, int(max_procesos))
        info.JobMemoryLimit = max(1, int(max_memoria_mb)) * 1024 * 1024
        if not self._k32.SetInformationJobObject(job, _JOB_INFO_EXTENDIDA, self._ctypes.byref(info),
                                                 self._ctypes.sizeof(info)):
            self._k32.CloseHandle(job)
            raise ErrorConfinamiento("SetInformationJobObject")
        return int(job)

    def asignar(self, job: int, pid: int) -> bool:
        proceso = self._k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not proceso:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(job, proceso))
        finally:
            self._k32.CloseHandle(proceso)

    def reanudar(self, pid: int) -> None:
        if psutil is not None:
            psutil.Process(int(pid)).resume()
            return
        ntdll = self._ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        ntdll.NtResumeProcess.argtypes = (self._wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = self._ctypes.c_long
        proceso = self._k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
        if not proceso:
            raise ErrorConfinamiento("OpenProcess")
        try:
            if ntdll.NtResumeProcess(proceso) != 0:
                raise ErrorConfinamiento("NtResumeProcess")
        finally:
            self._k32.CloseHandle(proceso)

    def terminar(self, job: int) -> None:
        self._k32.TerminateJobObject(job, 1)

    def cerrar(self, job: int) -> None:
        self._k32.CloseHandle(job)

    def pids(self, job: int) -> list[int] | None:
        """Pids de los procesos vivos del Job; ``None`` si no se pudo obtener la lista ENTERA."""
        lista = self._lista_pids()
        if not self._k32.QueryInformationJobObject(job, _JOB_INFO_PIDS, self._ctypes.byref(lista),
                                                   self._ctypes.sizeof(lista), None):
            return None
        cuantos = int(lista.NumberOfProcessIdsInList)
        if cuantos < int(lista.NumberOfAssignedProcesses) or cuantos > _MAX_PIDS_JOB:
            return None
        return [int(lista.ProcessIdList[i]) for i in range(cuantos)]

    def _abrir_del_job(self, job: int, pid: int, acceso: int) -> tuple[int | None, str]:
        """Abre ``pid`` comprobando que sigue vivo y DENTRO del Job (un pid reciclado por otro proceso no
        cuenta). ``(handle, "")`` o ``(None, "fuera" | "dudoso")``."""
        proceso = self._k32.OpenProcess(acceso | _PROCESS_QUERY_LIMITED | _SYNCHRONIZE, False, int(pid))
        if not proceso:
            return None, "fuera" if self._ctypes.get_last_error() == _ERROR_PARAMETRO_INVALIDO else "dudoso"
        dentro = self._wintypes.BOOL()
        if not self._k32.IsProcessInJob(proceso, job, self._ctypes.byref(dentro)):
            self._k32.CloseHandle(proceso)
            return None, "dudoso"
        if not dentro.value or self._k32.WaitForSingleObject(proceso, 0) == _WAIT_OBJECT_0:
            self._k32.CloseHandle(proceso)
            return None, "fuera"
        return proceso, ""

    def _ruta_imagen(self, proceso: int) -> str:
        tamano = self._wintypes.DWORD(32768)
        memoria = self._ctypes.create_unicode_buffer(tamano.value)
        if not self._k32.QueryFullProcessImageNameW(proceso, 0, memoria, self._ctypes.byref(tamano)):
            return ""
        return memoria.value

    def clasificar(self, job: int, pid: int, conservar: Callable[[str], bool]) -> str:
        proceso, estado = self._abrir_del_job(job, pid, 0)
        if proceso is None:
            return estado
        try:
            ruta = self._ruta_imagen(proceso)
            return "app" if ruta and conservar(ruta) else "ajeno"
        finally:
            self._k32.CloseHandle(proceso)

    def matar_procesos(self, job: int, pids: Sequence[int]) -> None:
        for pid in pids:
            proceso, _ = self._abrir_del_job(job, pid, _PROCESS_TERMINATE)
            if proceso is None:
                continue
            try:
                self._k32.TerminateProcess(proceso, 1)
                self._k32.WaitForSingleObject(proceso, _ESPERA_TERMINAR_MS)
            finally:
                self._k32.CloseHandle(proceso)

    def _quitar_limites(self, job: int) -> bool:
        """Quita todos los límites (también KILL_ON_JOB_CLOSE) para que las apps conocidas sobrevivan."""
        info = self._info()
        info.BasicLimitInformation.LimitFlags = 0
        return bool(self._k32.SetInformationJobObject(job, _JOB_INFO_EXTENDIDA, self._ctypes.byref(info),
                                                      self._ctypes.sizeof(info)))

    def finalizar(self, job: int, conservar: Callable[[str], bool]) -> int:
        """Tras una salida NORMAL: mata lo que quede en el Job salvo las apps conocidas; si queda alguna,
        quita los límites para que siga viva. Ante cualquier duda mata el Job entero. Cierra el handle
        (una sola vez) y devuelve cuántos procesos quedaron vivos."""
        vivas = -1
        try:
            vivas = _limpiar_restantes(lambda: self.pids(job), lambda pid: self.clasificar(job, pid, conservar),
                                       lambda pids: self.matar_procesos(job, pids))
        except Exception as exc:  # fail-closed: se mata todo
            log.debug("Limpieza del Job fallida (%s)", type(exc).__name__)
            vivas = -1
        finally:
            try:
                if vivas <= 0 or not self._quitar_limites(job):
                    vivas = 0
                    self._k32.TerminateJobObject(job, 1)
            finally:
                self._k32.CloseHandle(job)
        return vivas


_API_JOB: _ApiJob | None = None


def _api_job() -> _ApiJob:
    global _API_JOB
    if _API_JOB is None:
        _API_JOB = _ApiJob()
    return _API_JOB


class _JobVivo:
    """El handle de un Job con un único dueño: ``terminar``, ``finalizar`` y ``cerrar`` se serializan y el
    handle se cierra exactamente una vez (un handle ya cerrado nunca se reutiliza: Windows podría haberle
    dado ese número a otro objeto)."""

    def __init__(self, api: _ApiJob, job: int) -> None:
        self._api = api
        self._job: int | None = job
        self._candado = threading.Lock()

    def asignar(self, pid: int) -> bool:
        with self._candado:
            return self._job is not None and self._api.asignar(self._job, pid)

    def terminar(self) -> None:
        with self._candado:
            if self._job is not None:
                self._api.terminar(self._job)

    def finalizar(self, conservar: Callable[[str], bool]) -> int:
        with self._candado:
            if self._job is None:
                return 0
            job, self._job = self._job, None
            return self._api.finalizar(job, conservar)

    def cerrar(self) -> None:
        """Mata lo que quede y cierra (salidas por plazo, cancelación o error)."""
        with self._candado:
            if self._job is None:
                return
            job, self._job = self._job, None
            try:
                self._api.terminar(job)
            finally:
                self._api.cerrar(job)


async def _en_ejecutor(ejecutor: Executor | None, funcion: Callable[..., Any], *args: Any) -> Any:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(ejecutor, funcion, *args)
    except RuntimeError:   # executor ya apagado (cierre): la llamada es breve, se hace aquí
        return funcion(*args)


def _lanzador_windows(ejecutor: Executor | None, conservar: Callable[[str], bool]) -> Lanzador:
    async def lanzar(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float,
                     max_salida: int, max_procesos: int, max_memoria_mb: int,
                     unidad: str | None = None) -> SalidaProceso:
        api = await _en_ejecutor(ejecutor, _api_job)
        job = _JobVivo(api, await _en_ejecutor(ejecutor, api.crear, max_procesos, max_memoria_mb))
        try:
            proceso = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=dict(env), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_SUSPENDED | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW)
            asignado = await _en_ejecutor(ejecutor, job.asignar, proceso.pid)
            if not asignado:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proceso.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proceso.wait(), PLAZO_MUERTE_S)
                raise ErrorConfinamiento("AssignProcessToJobObject")
            try:
                await _en_ejecutor(ejecutor, api.reanudar, proceso.pid)
            except Exception as exc:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proceso.kill()
                raise ErrorConfinamiento("reanudar") from exc

            async def matar() -> None:
                await _en_ejecutor(ejecutor, job.terminar)

            async def al_terminar() -> None:
                vivas = await _en_ejecutor(ejecutor, job.finalizar, conservar)
                if vivas:
                    log.info("El script dejó abiertas %d ventanas o procesos de apps conocidas", vivas)

            codigo, agotado, duracion, salida, errores = await _esperar_proceso(
                proceso, timeout_s=timeout_s, max_salida=max_salida, matar=matar, al_terminar=al_terminar)
            return SalidaProceso(codigo, agotado, duracion, bytes(salida.datos), bytes(errores.datos),
                                 salida.total > len(salida.datos) or errores.total > len(errores.datos),
                                 salida.total + errores.total, True)
        finally:
            # Plazo vencido, cancelación o error: mata el árbol y cierra (si finalizar ya lo cerró, nada).
            with contextlib.suppress(Exception):
                await _en_ejecutor(ejecutor, job.cerrar)

    return lanzar


def _restantes_linux(pgid: int, unidad: str | None) -> list[Any] | None:
    """Procesos que siguen en el grupo del script o, en modo cgroups, en su unidad (aunque se hayan ido a
    otra sesión con ``setsid``). ``None`` sin psutil o si no se pudo listar."""
    if psutil is None:
        return None
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        return None
    marca = f"/{unidad}.scope" if unidad else None
    restantes: list[Any] = []
    for proceso in psutil.process_iter():
        try:
            dentro = getpgid(proceso.pid) == pgid
            if not dentro and marca is not None:
                with open(f"/proc/{proceso.pid}/cgroup", encoding="utf-8", errors="replace") as archivo:
                    dentro = marca in archivo.read()
        except (OSError, ValueError):
            continue
        if dentro:
            restantes.append(proceso)
    return restantes


def _clasificar_linux(proceso: Any, conservar: Callable[[str], bool]) -> str:
    try:
        if proceso.status() == psutil.STATUS_ZOMBIE:
            return "fuera"
        ruta = proceso.exe()
    except psutil.NoSuchProcess:
        return "fuera"
    except (psutil.Error, OSError):
        return "ajeno"     # sin poder leer el ejecutable no se conserva
    return "app" if ruta and conservar(ruta) else "ajeno"


def _matar_linux(procesos: list[Any]) -> None:
    for proceso in procesos:
        with contextlib.suppress(psutil.Error, OSError):
            proceso.terminate()
    _, vivos = psutil.wait_procs(procesos, timeout=GRACIA_TERM_S)
    for proceso in vivos:
        with contextlib.suppress(psutil.Error, OSError):
            proceso.kill()
    if vivos:
        psutil.wait_procs(vivos, timeout=1.0)


def _lanzador_linux(ejecutor: Executor | None, conservar: Callable[[str], bool]) -> Lanzador:
    async def lanzar(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float,
                     max_salida: int, max_procesos: int, max_memoria_mb: int,
                     unidad: str | None = None) -> SalidaProceso:
        proceso = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), env=dict(env), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        systemctl = shutil.which("systemctl") or "systemctl"

        async def systemctl_usuario(*orden: str) -> None:
            try:
                await _ejecutar_breve([systemctl, "--user", *orden], timeout_s=PLAZO_SYSTEMCTL_S, env=env)
            except Exception as exc:
                log.debug("systemctl --user %s falló (%s)", orden[0], type(exc).__name__)

        async def matar() -> None:
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    killpg(proceso.pid, signal.SIGTERM)
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(_esperar_salida(proceso), GRACIA_TERM_S)
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    killpg(proceso.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proceso.kill()
            if unidad:
                await systemctl_usuario("kill", "--signal=SIGKILL", f"{unidad}.scope")

        async def al_terminar() -> None:
            vivas = await _en_ejecutor(ejecutor, lambda: _limpiar_restantes(
                lambda: _restantes_linux(proceso.pid, unidad), lambda p: _clasificar_linux(p, conservar),
                _matar_linux, plazo_s=PLAZO_MUERTE_S + GRACIA_TERM_S))
            if vivas < 0:
                await matar()
                return
            if vivas == 0:
                # Barrido final (lo que no se pudo ver en la lista) y, en modo cgroups, la unidad entera.
                killpg = getattr(os, "killpg", None)
                if killpg is not None:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        killpg(proceso.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                if unidad:
                    await systemctl_usuario("kill", "--signal=SIGKILL", f"{unidad}.scope")
                return
            log.info("El script dejó abiertas %d ventanas o procesos de apps conocidas", vivas)
            if unidad:   # las apps siguen en la unidad: sin los topes del script (como el Job en Windows)
                await systemctl_usuario("set-property", "--runtime", f"{unidad}.scope", "TasksMax=infinity",
                                        "MemoryMax=infinity")

        codigo, agotado, duracion, salida, errores = await _esperar_proceso(
            proceso, timeout_s=timeout_s, max_salida=max_salida, matar=matar, al_terminar=al_terminar)
        return SalidaProceso(codigo, agotado, duracion, bytes(salida.datos), bytes(errores.datos),
                             salida.total > len(salida.datos) or errores.total > len(errores.datos),
                             salida.total + errores.total, unidad is not None)

    return lanzar


def lanzador_por_defecto(plataforma: str, *, ejecutor: Executor | None = None,
                         conservar: Callable[[str], bool] | None = None) -> Lanzador:
    """Lanzador real: Job Object en Windows; grupo de procesos (y cgroups) en Linux. ``conservar(ruta)``
    decide qué procesos que el script dejó vivos al terminar NORMALMENTE se conservan (por defecto, solo
    las apps conocidas instaladas en carpetas de apps); el resto se mata. Al vencer, se mata todo."""
    if plataforma == "win32":
        return _lanzador_windows(ejecutor, conservar or _app_conocida_windows)
    return _lanzador_linux(ejecutor, conservar or _app_conocida_linux)


# ---------------------------------------------------------------------------
# Salida del script
# ---------------------------------------------------------------------------


def _decodificar(datos: bytes, plataforma: str) -> str:
    """UTF-8 (tolerando un carácter partido al final por el recorte); si no, OEM en Windows; si no,
    UTF-8 con reemplazo."""
    try:
        decodificador = codecs.getincrementaldecoder("utf-8")("strict")
        return decodificador.decode(datos, final=False)
    except UnicodeDecodeError:
        pass
    if plataforma == "win32":
        try:
            return datos.decode("oem")
        except (UnicodeDecodeError, LookupError):
            pass
    return datos.decode("utf-8", "replace")


def limpiar_salida(datos: bytes, plataforma: str, secretos: Iterable[str]) -> str:
    """Salida legible y segura: decodificada, sin ANSI ni controles (salvo ``\\n`` y ``\\t``) y redactada
    con ``redactar(…, alta_entropia=True)``."""
    texto = _decodificar(datos, plataforma).replace("\r\n", "\n").replace("\r", "\n")
    texto = _RE_ANSI.sub("", texto)
    texto = "".join(c for c in texto if c in "\n\t" or not _es_control(c))
    texto = _sin_sustitutos(texto)
    return redactar(texto, secretos, alta_entropia=True)


# ---------------------------------------------------------------------------
# Archivos: escritura privada y borrado sin seguir enlaces
# ---------------------------------------------------------------------------


def _chmod(ruta: Path, modo: int) -> None:
    try:
        os.chmod(ruta, modo)
    except OSError as exc:
        log.debug("chmod %o fallido (%s)", modo, type(exc).__name__)


def _crear_directorio_privado(ruta: Path) -> None:
    ruta.mkdir(mode=0o700, parents=True, exist_ok=True)
    _chmod(ruta, 0o700)


def _escribir_privado(ruta: Path, datos: bytes, *, reemplazar: bool = False) -> None:
    """Escribe ``datos`` en un archivo NUEVO con 0o600 (binario: sin traducir saltos de línea).

    Con ``reemplazar``, lo que hubiera en esa ruta se quita antes (un enlace se quita él mismo, nunca su
    destino): el script corre en esa carpeta y podría haber dejado ahí un enlace hacia otro archivo."""
    if reemplazar and os.path.lexists(ruta):
        if _es_enlace(ruta):
            _quitar_enlace(ruta)
        elif ruta.is_dir():
            raise IsADirectoryError("destino ocupado por una carpeta")
        else:
            os.unlink(ruta)
    banderas = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(ruta, banderas, 0o600)
    with os.fdopen(descriptor, "wb") as archivo:
        archivo.write(datos)
        archivo.flush()
        os.fsync(archivo.fileno())
    _chmod(ruta, 0o600)


def _escribir_json_atomico(ruta: Path, datos: Any) -> None:
    """``.tmp`` + ``os.replace`` con 0o600."""
    _crear_directorio_privado(ruta.parent)
    temporal = ruta.with_name(f"{ruta.name}.tmp-{uuid.uuid4().hex[:8]}")
    contenido = json.dumps(datos, ensure_ascii=False, indent=1).encode("utf-8")
    try:
        _escribir_privado(temporal, contenido)
        os.replace(temporal, ruta)
        _chmod(ruta, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporal)
        raise


def _es_enlace(entrada: os.DirEntry[str] | Path) -> bool:
    """Enlace simbólico o punto de reanálisis (unión de Windows): nunca se sigue."""
    try:
        info = entrada.stat(follow_symlinks=False) if isinstance(entrada, os.DirEntry) else os.lstat(entrada)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    atributos = getattr(info, "st_file_attributes", 0)
    return bool(atributos & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _quitar_enlace(ruta: Path) -> None:
    """Quita el enlace en sí (nunca su destino)."""
    try:
        os.unlink(ruta)
    except (IsADirectoryError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            os.rmdir(ruta)   # unión de directorio: rmdir quita la unión, no el destino


def _borrar_arbol(ruta: Path, profundidad: int = 0) -> None:
    """Borra un árbol de directorios sin seguir enlaces ni uniones (nunca sale de ``ruta``)."""
    if profundidad > 64:
        return
    try:
        entradas = list(os.scandir(ruta))
    except OSError as exc:
        log.debug("No pude listar una carpeta de ejecución (%s)", type(exc).__name__)
        return
    for entrada in entradas:
        hijo = Path(entrada.path)
        if _es_enlace(entrada):
            _quitar_enlace(hijo)
            continue
        try:
            if entrada.is_dir(follow_symlinks=False):
                _borrar_arbol(hijo, profundidad + 1)
                continue
            try:
                os.unlink(hijo)
            except PermissionError:
                _chmod(hijo, stat.S_IWRITE | stat.S_IREAD)
                os.unlink(hijo)
        except OSError as exc:
            log.debug("No pude borrar un archivo de una ejecución vieja (%s)", type(exc).__name__)
    try:
        os.rmdir(ruta)
    except OSError as exc:
        log.debug("No pude borrar una carpeta de ejecución vieja (%s)", type(exc).__name__)


def _purgar_ejecuciones(raiz: Path, conservar: int) -> None:
    """Conserva las ``conservar`` carpetas más recientes de ``raiz``; borra el resto solo si
    ``os.path.commonpath`` confirma que están dentro de ``raiz`` (sin seguir enlaces)."""
    try:
        base = os.path.realpath(raiz)
        entradas = []
        for entrada in os.scandir(raiz):
            try:
                momento = entrada.stat(follow_symlinks=False).st_mtime
            except OSError:
                momento = 0.0
            entradas.append((momento, entrada))
    except OSError as exc:
        log.debug("No pude listar las ejecuciones (%s)", type(exc).__name__)
        return
    entradas.sort(key=lambda par: par[0], reverse=True)
    for _, entrada in entradas[max(0, conservar):]:
        ruta = Path(entrada.path)
        try:
            absoluta = os.path.abspath(ruta)
            if os.path.commonpath([base, absoluta]) != base or absoluta == base:
                continue
        except ValueError:
            continue
        if _es_enlace(entrada):
            _quitar_enlace(ruta)
        elif entrada.is_dir(follow_symlinks=False):
            _borrar_arbol(ruta)
        else:
            with contextlib.suppress(OSError):
                os.unlink(ruta)


def _slug(nombre: str) -> str:
    """``id`` de una habilidad: minúsculas, sin tildes, espacios → ``_`` y solo ``[a-z0-9_-]`` (≤ 40)."""
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", nombre) if not unicodedata.combining(c))
    texto = re.sub(r"\s+", "_", sin_tildes.strip().lower())
    texto = re.sub(r"[^a-z0-9_-]", "", texto)
    return re.sub(r"_+", "_", texto).strip("_")[:MAX_NOMBRE_HABILIDAD]


def _habilidad_desde(dato: Any) -> Habilidad | None:
    if not isinstance(dato, Mapping):
        return None
    try:
        textos = {clave: dato.get(clave) for clave in ("id", "nombre", "descripcion", "lenguaje", "plataforma",
                                                       "codigo", "huella", "nivel", "creada_utc")}
        if not all(isinstance(v, str) for v in textos.values()):
            return None
        usada = dato.get("usada_utc")
        usos = dato.get("usos", 0)
        if usada is not None and not isinstance(usada, str):
            return None
        if not isinstance(usos, int) or isinstance(usos, bool) or usos < 0:
            usos = 0
        if textos["lenguaje"] not in LENGUAJES_SCRIPT or not _RE_ID_HABILIDAD.match(textos["id"]):
            return None
        if textos["nivel"] not in NIVELES_RIESGO:
            textos["nivel"] = "critico"
        return Habilidad(id=textos["id"], nombre=textos["nombre"][:MAX_NOMBRE_HABILIDAD],
                         descripcion=textos["descripcion"][:MAX_DESCRIPCION_HABILIDAD],
                         lenguaje=textos["lenguaje"], plataforma=textos["plataforma"], codigo=textos["codigo"],
                         huella=textos["huella"], nivel=textos["nivel"], creada_utc=textos["creada_utc"],
                         usada_utc=usada, usos=usos)
    except Exception as exc:  # entrada rara: se descarta
        log.debug("Habilidad ilegible (%s)", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# El taller
# ---------------------------------------------------------------------------


class TallerScripts:
    """Taller de scripts (§9.2). API que no lanza (salvo la cancelación)."""

    def __init__(self, settings: Settings, bus: BusEventos | None = None, *,
                 generador: GeneradorScript | None = None,
                 reloj: Callable[[], float] = time.time,
                 plataforma: str | None = None,
                 lanzador: Lanzador | None = None,
                 analizador_ps: AnalizadorPS | None = None,
                 carpetas: Mapping[str, Path] | None = None,
                 sonda_limites: Callable[[], Awaitable[str]] | None = None,
                 es_elevado: Callable[[], bool | None] | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self._generador = generador
        self._reloj = reloj
        self._plataforma = sys.platform if plataforma is None else str(plataforma)
        self._ejecutor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rams-taller")
        self._lanzador: Lanzador = lanzador or lanzador_por_defecto(self._plataforma, ejecutor=self._ejecutor)
        self._analizador_ps: AnalizadorPS = analizador_ps or self._analizador_ps_por_defecto
        self._carpetas: dict[str, Path] | None = dict(carpetas) if carpetas is not None else None
        self._sonda = sonda_limites or sonda_limites_linux
        self._es_elevado = es_elevado or (lambda: es_elevado_por_defecto(self._plataforma))
        self._interpretes: dict[str, str] = {}
        self._modo_limites = "job" if self._plataforma == "win32" else "ninguno"
        self._elevado: bool | None = False
        self._iniciado = False
        self._cerrado = False
        self._propuesta: PropuestaScript | None = None
        self._ejecutada: tuple[PropuestaScript, float] | None = None
        self._ocupado = False
        self._tarea_ejecucion: asyncio.Future[SalidaProceso] | None = None
        self._habilidades: tuple[Habilidad, ...] = ()
        self._candado_habilidades: asyncio.Lock | None = None
        self._candado_inicio: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    @property
    def _soportado(self) -> bool:
        return _plataforma_de(self._plataforma) in ("windows", "linux")

    @property
    def _habilitado(self) -> bool:
        return bool(getattr(self.settings, "taller_habilitado", False)) and self._soportado and not self._cerrado

    async def iniciar(self) -> DiagnosticoModulo:
        """Detecta intérpretes, carpetas y el modo de límites (una sola vez) y carga las habilidades.
        Clave ``"taller"``. Nunca lanza (salvo la cancelación)."""
        try:
            await self._asegurar_iniciado()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No pude iniciar el taller de scripts (%s)", type(exc).__name__)
        return self.diagnostico()

    async def _asegurar_iniciado(self) -> None:
        if self._iniciado or not self._habilitado:
            return
        if self._candado_inicio is None:
            self._candado_inicio = asyncio.Lock()
        async with self._candado_inicio:
            if self._iniciado:
                return
            loop = asyncio.get_running_loop()
            self._interpretes = await loop.run_in_executor(self._ejecutor, detectar_interpretes, self._plataforma)
            if self._carpetas is None:
                try:
                    self._carpetas = await loop.run_in_executor(self._ejecutor, lambda: carpetas_usuario(
                        plataforma=self._plataforma))
                except Exception as exc:
                    log.debug("Sin carpetas del usuario (%s)", type(exc).__name__)
                    self._carpetas = {}
            if _plataforma_de(self._plataforma) == "linux":
                try:
                    modo = await self._sonda()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("Sonda de límites fallida (%s)", type(exc).__name__)
                    modo = "ninguno"
                self._modo_limites = modo if modo in MODOS_LIMITES else "ninguno"
            self._elevado = await self._comprobar_elevado()
            await self._recargar_habilidades()
            self._iniciado = True
            log.info("Taller de scripts listo: %s; límites: %s; %d habilidades",
                     ", ".join(self.lenguajes()) or "sin intérpretes", self._modo_limites, len(self._habilidades))

    async def _comprobar_elevado(self) -> bool | None:
        try:
            resultado = await asyncio.get_running_loop().run_in_executor(self._ejecutor, self._es_elevado)
        except Exception as exc:
            log.debug("Comprobación de elevación fallida (%s)", type(exc).__name__)
            return None
        return resultado if isinstance(resultado, bool) else None

    def diagnostico(self) -> DiagnosticoModulo:
        """Diagnóstico ``taller`` (§9.7). Nunca lanza."""
        try:
            if not getattr(self.settings, "taller_habilitado", False):
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OFF, "deshabilitado (RAMS_TALLER=0)")
            if not self._soportado:
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OFF, "sistema no soportado")
            if self._cerrado:
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OFF, "cerrado")
            if not self._iniciado:
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.EN_ESPERA, "sin iniciar")
            windows = self._plataforma == "win32"
            motivos: list[str] = []
            if not self.lenguajes():
                motivos.append("sin intérpretes")
            elif windows and "powershell" not in self._interpretes:
                motivos.append("sin PowerShell: solo Python")
            elif not windows and "bash" not in self._interpretes:
                motivos.append("sin bash: solo Python")
            if self._generador is None:
                motivos.append("sin generador (Gemini): solo habilidades guardadas")
            if self._elevado is not False:
                motivos.append("R.A.M.S. corre como administrador: no ejecuto scripts" if self._elevado
                               else "no pude comprobar si R.A.M.S. corre como administrador: no ejecuto scripts")
            if not windows and self._modo_limites == "prlimit":
                motivos.append("memoria por proceso (prlimit), sin tope de procesos")
            elif not windows and self._modo_limites == "ninguno":
                motivos.append("sin cgroups ni prlimit: solo tiempo límite")
            if motivos:
                return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.DEGRADADO, "; ".join(motivos))
            partes: list[str] = []
            if windows:
                partes.append("PowerShell 5.1")
            else:
                partes.append("bash")
            version = sys.version_info
            partes.append(f"Python {version.major}.{version.minor}")
            partes.append(f"≤ {_segundos_legibles(self._timeout_s())} s")
            if not windows:
                partes.append(self._modo_limites)
            cuantas = len(self._habilidades)
            partes.append(f"{cuantas} habilidad" if cuantas == 1 else f"{cuantas} habilidades")
            return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.OK, " · ".join(partes))
        except Exception as exc:  # pragma: no cover - defensivo
            log.debug("Diagnóstico del taller fallido (%s)", type(exc).__name__)
            return DiagnosticoModulo(CLAVE, NOMBRE, EstadoModulo.DEGRADADO, "error interno")

    def set_generador(self, generador: GeneradorScript | None) -> None:
        """Cambia el generador (Broca con Gemini) o lo quita (``None``)."""
        self._generador = generador

    @property
    def disponible(self) -> bool:
        """Puede proponer scripts ahora: habilitado, soportado, con intérpretes y con generador."""
        return self._habilitado and self._generador is not None and (bool(self.lenguajes()) or not self._iniciado)

    def lenguajes(self) -> tuple[str, ...]:
        """Windows: ``("powershell", "python")`` (sin PowerShell, ``("python",)``); Linux: ``("bash",
        "python")`` (sin bash, ``("python",)``). Antes de iniciar, solo lo que ya se detectó."""
        if self._plataforma == "win32":
            orden: tuple[str, ...] = ("powershell", "python")
        elif _plataforma_de(self._plataforma) == "linux":
            orden = ("bash", "python")
        else:
            return ()
        return tuple(lenguaje for lenguaje in orden if lenguaje in self._interpretes)

    @property
    def ocupado(self) -> bool:
        return self._ocupado

    async def cerrar(self) -> None:
        """Mata el script en curso (TerminateJobObject / killpg) y apaga el executor. Idempotente."""
        self._cerrado = True
        tarea = self._tarea_ejecucion
        if tarea is not None and not tarea.done():
            tarea.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait({tarea}, timeout=PLAZO_MUERTE_S + GRACIA_TERM_S + PLAZO_SYSTEMCTL_S)
        self._propuesta = None
        self._ejecutada = None
        self._ejecutor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Rutas
    # ------------------------------------------------------------------

    def _taller_dir(self) -> Path:
        return Path(getattr(self.settings, "taller_dir", Path(self.settings.data_dir) / "scripts"))

    def _carpetas_permitidas(self) -> list[Path]:
        carpetas: list[Path] = [self._taller_dir() / "ejecuciones"]
        carpetas.extend(Path(p) for p in (self._carpetas or {}).values() if p)
        descargas = getattr(self.settings, "drive_descargas_dir", None)
        if descargas:
            carpetas.append(Path(descargas))
        return carpetas

    def _protegidas(self) -> list[Path]:
        return [Path(self.settings.base_dir), Path(self.settings.data_dir)]

    def _timeout_s(self) -> float:
        try:
            valor = float(getattr(self.settings, "taller_timeout_s", 30.0))
        except (TypeError, ValueError):
            valor = 30.0
        if not math.isfinite(valor):
            valor = 30.0
        return min(TIMEOUT_MAX_S, max(TIMEOUT_MIN_S, valor))

    def _carpeta_relativa(self, carpeta: Path) -> str:
        try:
            relativa = os.path.relpath(carpeta, self.settings.base_dir)
            if not relativa.startswith(".."):
                return relativa.replace("\\", "/")
        except ValueError:
            pass
        return f"{self._taller_dir().name}/ejecuciones/{carpeta.name}"

    # ------------------------------------------------------------------
    # Análisis
    # ------------------------------------------------------------------

    async def analizar(self, lenguaje: str, codigo: str) -> Analisis:
        """Análisis estático en local (sin ejecutar nada del script). Nunca lanza (salvo la cancelación)."""
        try:
            await self._asegurar_iniciado()
            return await self._analizar(lenguaje, codigo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensivo: lo desconocido no se ejecuta
            log.debug("Análisis fallido (%s)", type(exc).__name__)
            colector = _Colector("*")
            colector.agregar("degradado", None)
            return colector.analisis("heuristico", False, "alto")

    async def _analizar(self, lenguaje: str, codigo: str) -> Analisis:
        if not isinstance(codigo, str) or _demasiado_grande(codigo):
            return _analisis_tamano("ast")
        loop = asyncio.get_running_loop()
        carpetas = self._carpetas_permitidas()
        protegidas = self._protegidas()
        if lenguaje == "python":
            return await loop.run_in_executor(self._ejecutor, lambda: analizar_python(
                codigo, carpetas=carpetas, protegidas=protegidas))
        if lenguaje == "bash":
            return await loop.run_in_executor(self._ejecutor, lambda: analizar_bash(
                codigo, carpetas=carpetas, protegidas=protegidas))
        if lenguaje == "powershell":
            datos = await self._datos_powershell(codigo)
            if datos is not None and _datos_validos(datos):
                return await loop.run_in_executor(self._ejecutor, lambda: analizar_powershell_ast(
                    datos, codigo, carpetas=carpetas, protegidas=protegidas))
            return await loop.run_in_executor(self._ejecutor, lambda: analizar_powershell_heuristico(
                codigo, carpetas=carpetas, protegidas=protegidas))
        colector = _Colector("*")
        colector.agregar("sintaxis", None)
        return colector.analisis("heuristico", False)

    async def _datos_powershell(self, codigo: str) -> Mapping[str, Any] | None:
        """Escribe el código en ``taller_dir/analisis/<id>.ps1`` (UTF-8 con BOM) y llama al analizador."""
        carpeta = self._taller_dir() / "analisis"
        ruta = carpeta / f"{uuid.uuid4().hex[:8]}.ps1"
        loop = asyncio.get_running_loop()

        def preparar() -> None:
            _crear_directorio_privado(self._taller_dir())
            _crear_directorio_privado(carpeta)
            _escribir_privado(ruta, b"\xef\xbb\xbf" + codigo.encode("utf-8"))

        try:
            await loop.run_in_executor(self._ejecutor, preparar)
        except Exception as exc:
            log.debug("No pude preparar el análisis de PowerShell (%s)", type(exc).__name__)
            return None
        try:
            datos = await self._analizador_ps(ruta)
            return datos if isinstance(datos, Mapping) else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Analizador de PowerShell fallido (%s)", type(exc).__name__)
            return None
        finally:
            with contextlib.suppress(Exception):
                await loop.run_in_executor(self._ejecutor, lambda: ruta.unlink(missing_ok=True))

    async def _analizador_ps_por_defecto(self, ruta: Path) -> Mapping[str, Any] | None:
        """``powershell.exe … -File lobes/taller_analizar.ps1 -Ruta <ruta>`` (sin shell, stdin DEVNULL,
        20 s, ≤ 1 MiB). ``None`` si no está o falla."""
        powershell = self._interpretes.get("powershell")
        if not powershell or not RUTA_ANALIZADOR_PS.is_file():
            return None
        argv = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                str(RUTA_ANALIZADOR_PS), "-Ruta", str(ruta)]
        extra: dict[str, Any] = {"creationflags": _CREATE_NO_WINDOW} if self._plataforma == "win32" else {}
        proceso = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=entorno_saneado(self._plataforma, ruta.parent), **extra)
        acumulador = _Acumulador(MAX_SALIDA_ANALIZADOR)
        lector = asyncio.ensure_future(acumulador.leer(proceso.stdout))
        try:
            await asyncio.wait_for(asyncio.gather(lector, proceso.wait()), TIMEOUT_ANALIZADOR_S)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(ProcessLookupError, OSError):
                proceso.kill()
            lector.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proceso.wait(), PLAZO_MUERTE_S)
            log.debug("El analizador de PowerShell venció")
            return None
        if proceso.returncode != 0 or acumulador.total > MAX_SALIDA_ANALIZADOR:
            return None
        try:
            datos = json.loads(bytes(acumulador.datos).decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return None
        return datos if isinstance(datos, Mapping) else None

    # ------------------------------------------------------------------
    # Propuestas
    # ------------------------------------------------------------------

    def _vigente(self, id: str) -> PropuestaScript | None:
        propuesta = self._propuesta
        if propuesta is None or not _iguales(propuesta.id, id):
            return None
        return propuesta

    async def proponer(self, tarea: str, *, canal_origen: str, ttl_s: float) -> PropuestaScript | FalloTaller:
        """Pide el script al generador, lo valida, lo analiza y lo deja como propuesta vigente (§9.3).
        Sin reintentos. Nunca lanza (salvo la cancelación)."""
        if not self._habilitado:
            return FalloTaller("deshabilitado", "El taller está apagado (RAMS_TALLER=1 en el .env)")
        if canal_origen == "telegram":
            return FalloTaller("deshabilitado", "Los scripts solo se piden desde el computador")
        generador = self._generador
        if generador is None:
            return FalloTaller("sin_modelo", "Ahora no tengo al modelo para escribirlo; tus habilidades "
                                             "guardadas sí funcionan")
        texto = sanear_tarea(tarea)
        if not texto:
            return FalloTaller("vacio", "No me dijiste qué debe hacer el script")
        try:
            await self._asegurar_iniciado()
            lenguajes = self.lenguajes()
            if not lenguajes:
                return FalloTaller("lenguaje_no_disponible", "No encuentro ningún intérprete para ejecutar scripts")
            plataforma = _plataforma_de(self._plataforma)
            try:
                datos = await generador(texto, plataforma=plataforma, lenguajes=lenguajes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("El generador de scripts falló (%s)", type(exc).__name__)
                return FalloTaller("error", "No pude escribir el script (error del modelo)")
            if datos is None:
                return FalloTaller("sin_modelo", "Ahora no tengo al modelo para escribirlo; tus habilidades "
                                                 "guardadas sí funcionan")
            validado = self._validar_datos(datos, lenguajes)
            if isinstance(validado, FalloTaller):
                log.info("Respuesta del generador descartada: %s", validado.codigo)
                return validado
            lenguaje, codigo, explicacion, efectos, requisitos = validado
            analisis = await self._analizar(lenguaje, codigo)
            ahora = self._reloj()
            propuesta = PropuestaScript(
                id=uuid.uuid4().hex[:8], version=1, tarea=_recortar_utf16(texto, MAX_TAREA_PROPUESTA),
                lenguaje=lenguaje, plataforma=plataforma, codigo=codigo, explicacion=explicacion,
                efectos=efectos, requisitos=requisitos, analisis=analisis, habilidad=None,
                habilidad_cambiada=False, ejecuciones=0, canal_origen=_canal(canal_origen), creado=ahora,
                caduca=ahora + _ttl(ttl_s))
            self._propuesta = propuesta
            log.info("Script propuesto: %s, riesgo %s (%s), huella %s, %d bytes", lenguaje, analisis.nivel,
                     analisis.metodo, propuesta.huella()[:12], len(codigo.encode("utf-8")))
            return propuesta
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensivo
            log.warning("No pude preparar el script (%s)", type(exc).__name__)
            return FalloTaller("error", "No pude preparar el script (error interno)")

    @staticmethod
    def _validar_datos(datos: Any, lenguajes: Sequence[str]) -> (
            tuple[str, str, str, tuple[str, ...], tuple[str, ...]] | FalloTaller):
        invalido = FalloTaller("respuesta_invalida", "El modelo devolvió algo que no puedo revisar; "
                                                     "pídemelo otra vez con otras palabras")
        if not isinstance(datos, Mapping):
            return invalido
        lenguaje = datos.get("lenguaje")
        if not isinstance(lenguaje, str):
            return invalido
        lenguaje = lenguaje.strip().lower()
        if lenguaje not in LENGUAJES_SCRIPT:
            return invalido
        if lenguaje not in lenguajes:
            return FalloTaller("lenguaje_no_disponible", f"El modelo eligió {lenguaje}, que aquí no puedo ejecutar")
        codigo, motivo = normalizar_codigo(datos.get("codigo"))
        if codigo is None:
            if motivo == "demasiado_grande":
                return FalloTaller("demasiado_grande", "El script salió demasiado largo para revisarlo en la "
                                                       "tarjeta; pídemelo por partes")
            return invalido
        explicacion_cruda = datos.get("explicacion", "")
        if explicacion_cruda is not None and not isinstance(explicacion_cruda, str):
            return invalido
        # §9.3 paso 4: los textos informativos que pasan de los topes invalidan la respuesta (no se
        # recortan: la explicación o un efecto cortados podrían esconder justo lo importante).
        explicacion = _texto_informativo(explicacion_cruda or "", _SIN_TOPE, multilinea=True)
        if largo_utf16(explicacion) > MAX_EXPLICACION:
            return invalido
        efectos = _lista_informativa(datos.get("efectos"), MAX_EFECTOS, MAX_EFECTO)
        requisitos = _lista_informativa(datos.get("requisitos"), MAX_REQUISITOS, MAX_REQUISITO)
        if efectos is None or requisitos is None:
            return invalido
        return lenguaje, codigo, explicacion, efectos, requisitos

    def propuesta(self, id: str) -> PropuestaScript | None:
        """La propuesta vigente con esa id (``None`` si no existe o caducó)."""
        propuesta = self._vigente(id)
        if propuesta is None or self._reloj() > propuesta.caduca:
            return None
        return propuesta

    def reabrir(self, id: str) -> PropuestaScript | None:
        """Renueva la caducidad de la propuesta (las ejecuciones ya están contadas). ``main`` la llama
        tras ejecutar, para «Ejecutar otra vez» con otro clic."""
        propuesta = self._vigente(id)
        if propuesta is None:
            return None
        ahora = self._reloj()
        ejecutada_hace_poco = (self._ejecutada is not None and self._ejecutada[0].id == propuesta.id
                               and ahora - self._ejecutada[1] <= VENTANA_GUARDAR_S)
        if ahora > propuesta.caduca and not ejecutada_hace_poco:
            return None
        ttl = max(1.0, propuesta.caduca - propuesta.creado)
        renovada = replace(propuesta, version=propuesta.version + 1, caduca=ahora + ttl)
        self._propuesta = renovada
        return renovada

    def descartar(self, id: str) -> bool:
        """Quita la propuesta vigente si es esa id."""
        if self._vigente(id) is None:
            return False
        self._propuesta = None
        return True

    # ------------------------------------------------------------------
    # Ejecución (§9.6)
    # ------------------------------------------------------------------

    async def ejecutar(self, id: str, huella: str) -> ResultadoScript:
        """Ejecuta la propuesta aprobada (solo tras la compuerta). Nunca lanza (salvo la cancelación)."""
        if not self._habilitado:
            return ResultadoScript(False, "deshabilitado", "El taller está apagado (RAMS_TALLER=1 en el .env)")
        propuesta = self._vigente(id)
        if propuesta is None:
            return ResultadoScript(False, "no_existe", "Ese script ya no está vigente")
        if self._reloj() > propuesta.caduca:
            return ResultadoScript(False, "caducado", "Esa propuesta caducó: pídemela de nuevo")
        if not _iguales(propuesta.huella(), huella):
            return ResultadoScript(False, "huella", "El código cambió desde que lo revisaste: revisa la versión nueva")
        if not propuesta.analisis.ejecutable:
            return ResultadoScript(False, "bloqueado", "Este script no se puede ejecutar desde aquí: "
                                   + (propuesta.analisis.motivo_no_ejecutable() or "riesgo crítico"))
        if self._ocupado:
            return ResultadoScript(False, "ocupado", "Ya hay un script en marcha: espera a que termine")
        self._ocupado = True
        try:
            return await self._ejecutar(propuesta, huella)
        except asyncio.CancelledError:
            if self._cerrado:
                return ResultadoScript(False, "error", "Cerré el taller: detuve el script")
            raise
        except ErrorConfinamiento:
            log.warning("Script %s no ejecutado: no se pudo confinar", propuesta.lenguaje)
            return ResultadoScript(False, "confinamiento", "No pude confinar el script (Job Object): no lo ejecuto")
        except FileNotFoundError:
            log.warning("Script %s no ejecutado: falta el intérprete", propuesta.lenguaje)
            return ResultadoScript(False, "sin_interprete", f"No encuentro el intérprete de {propuesta.lenguaje}")
        except Exception as exc:
            log.warning("Fallo al ejecutar un script de %s (%s)", propuesta.lenguaje, type(exc).__name__)
            return ResultadoScript(False, "error", "No pude ejecutar el script (error interno)")
        finally:
            self._ocupado = False
            self._tarea_ejecucion = None

    async def _ejecutar(self, propuesta: PropuestaScript, huella: str) -> ResultadoScript:
        await self._asegurar_iniciado()
        elevado = await self._comprobar_elevado()
        self._elevado = elevado
        if elevado is not False:
            return ResultadoScript(False, "elevado", "R.A.M.S. corre como administrador: por seguridad no "
                                                     "ejecuto scripts así")
        interprete = self._interpretes.get(propuesta.lenguaje)
        if not interprete:
            return ResultadoScript(False, "sin_interprete", f"No encuentro el intérprete de {propuesta.lenguaje}")
        loop = asyncio.get_running_loop()
        numero = propuesta.ejecuciones + 1
        carpeta, archivo = await loop.run_in_executor(self._ejecutor, self._preparar_carpeta, propuesta, numero)
        # Anti-TOCTOU: se relee el archivo y se vuelve a huellar justo antes de lanzar.
        releido = await loop.run_in_executor(self._ejecutor, self._releer, archivo, propuesta.lenguaje)
        huella_archivo = huella_contenido({"clase": "script", "lenguaje": propuesta.lenguaje, "codigo": releido})
        if releido != propuesta.codigo or not _iguales(huella_archivo, huella):
            log.warning("El archivo del script no coincide con lo aprobado: no lo ejecuto")
            return ResultadoScript(False, "huella", "El código cambió desde que lo revisaste: revisa la versión nueva")
        argv, unidad = self._argv(propuesta, numero, interprete, archivo.name)
        entorno = entorno_saneado(self._plataforma, carpeta / "tmp")
        timeout = self._timeout_s()
        self._tarea_ejecucion = asyncio.ensure_future(self._lanzador(
            argv, cwd=carpeta, env=entorno, timeout_s=timeout,
            max_salida=int(getattr(self.settings, "taller_max_salida", 16384)),
            max_procesos=int(getattr(self.settings, "taller_max_procesos", 16)),
            max_memoria_mb=int(getattr(self.settings, "taller_max_memoria_mb", 512)), unidad=unidad))
        salida = await self._tarea_ejecucion
        return await self._resultado(propuesta, salida, carpeta, timeout)

    def _preparar_carpeta(self, propuesta: PropuestaScript, numero: int) -> tuple[Path, Path]:
        """Crea ``taller_dir/ejecuciones/<id>-<n>/`` (0o700) con ``tmp/`` y el archivo exacto del script."""
        raiz = self._taller_dir() / "ejecuciones"
        _crear_directorio_privado(self._taller_dir())
        _crear_directorio_privado(raiz)
        carpeta = raiz / f"{propuesta.id}-{numero}"
        sufijo = 0
        while True:
            try:
                carpeta.mkdir(mode=0o700)
                break
            except FileExistsError:
                sufijo += 1
                if sufijo > 50:
                    raise
                carpeta = raiz / f"{propuesta.id}-{numero}-{sufijo}"
        _chmod(carpeta, 0o700)
        _crear_directorio_privado(carpeta / "tmp")
        extension = {"powershell": ".ps1", "python": ".py", "bash": ".sh"}[propuesta.lenguaje]
        archivo = carpeta / f"script{extension}"
        contenido = propuesta.codigo.encode("utf-8")
        if propuesta.lenguaje == "powershell":
            contenido = b"\xef\xbb\xbf" + contenido   # PowerShell 5.1 lee sin BOM como ANSI
        _escribir_privado(archivo, contenido)
        _purgar_ejecuciones(raiz, MAX_EJECUCIONES_GUARDADAS)
        return carpeta, archivo

    @staticmethod
    def _releer(archivo: Path, lenguaje: str) -> str:
        datos = archivo.read_bytes()
        if lenguaje == "powershell":
            if not datos.startswith(b"\xef\xbb\xbf"):
                return "\ufffd"   # sin BOM: no es lo que se escribió
            datos = datos[3:]
        return datos.decode("utf-8")

    def _argv(self, propuesta: PropuestaScript, numero: int, interprete: str,
              archivo: str) -> tuple[list[str], str | None]:
        """argv sin shell (§9.6 paso 4) y la unidad de systemd (solo en modo cgroups)."""
        if propuesta.lenguaje == "powershell":
            argv = [interprete, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", archivo]
        elif propuesta.lenguaje == "python":
            argv = [interprete, "-I", "-X", "utf8", "-B", archivo]
        else:
            argv = [interprete, "--noprofile", "--norc", archivo]
        unidad = None
        if _plataforma_de(self._plataforma) == "linux":
            procesos = int(getattr(self.settings, "taller_max_procesos", 16))
            memoria = int(getattr(self.settings, "taller_max_memoria_mb", 512))
            if self._modo_limites == "cgroups":
                unidad = f"rams-taller-{propuesta.id}-{numero}"
                prefijo = [shutil.which("systemd-run") or "systemd-run", "--user", "--scope", "--quiet",
                           "--collect", f"--unit={unidad}", "-p", f"TasksMax={procesos}", "-p",
                           f"MemoryMax={memoria}M", "--"]
                argv = prefijo + argv
            elif self._modo_limites == "prlimit":
                argv = [shutil.which("prlimit") or "prlimit", f"--as={memoria * 1024 * 1024}", "--"] + argv
        return argv, unidad

    async def _resultado(self, propuesta: PropuestaScript, salida: SalidaProceso, carpeta: Path,
                         timeout: float) -> ResultadoScript:
        loop = asyncio.get_running_loop()
        maximo = int(getattr(self.settings, "taller_max_salida", 16384))
        secretos = tuple(self.settings.secretos())
        crudo_out = bytes(salida.stdout)[:maximo] if isinstance(salida.stdout, (bytes, bytearray)) else b""
        crudo_err = bytes(salida.stderr)[:maximo] if isinstance(salida.stderr, (bytes, bytearray)) else b""
        stdout = limpiar_salida(crudo_out, self._plataforma, secretos)
        stderr = limpiar_salida(crudo_err, self._plataforma, secretos)
        recortada = bool(salida.recortada) or len(salida.stdout or b"") > maximo or len(salida.stderr or b"") > maximo

        def guardar() -> None:
            _escribir_privado(carpeta / "salida.txt", stdout.encode("utf-8"), reemplazar=True)
            _escribir_privado(carpeta / "errores.txt", stderr.encode("utf-8"), reemplazar=True)

        try:
            await loop.run_in_executor(self._ejecutor, guardar)
        except Exception as exc:
            log.debug("No pude guardar la salida del script (%s)", type(exc).__name__)
        codigo_salida = salida.codigo_salida if isinstance(salida.codigo_salida, int) else None
        duracion = float(salida.duracion_s) if isinstance(salida.duracion_s, (int, float)) else 0.0
        duracion_txt = formatear_decimal(duracion, 1)
        if salida.tiempo_agotado:
            codigo = "tiempo_agotado"
            mensaje = f"El script pasó del tiempo límite ({_segundos_legibles(timeout)} s): lo detuve"
        elif propuesta.lenguaje == "powershell" and codigo_salida != 0 and re.search(
                r"executionpolicy|pssecurityexception|about_execution_policies", stderr, re.IGNORECASE):
            codigo = "politica"
            mensaje = ("Una directiva del equipo impide ejecutar scripts de PowerShell; "
                       "puedo hacerlo en Python")
        elif codigo_salida == 0:
            codigo = "ejecutado"
            mensaje = f"Script terminado: código 0 en {duracion_txt} s"
        else:
            codigo = "error_salida"
            mensaje = f"El script terminó con código {codigo_salida} en {duracion_txt} s"
        actual = self._propuesta
        actualizada = replace(propuesta, ejecuciones=propuesta.ejecuciones + 1)
        if actual is not None and actual.id == propuesta.id:
            self._propuesta = replace(actual, ejecuciones=actual.ejecuciones + 1)
            actualizada = self._propuesta
        self._ejecutada = (actualizada, self._reloj())
        bytes_salida = int(salida.bytes_totales) if isinstance(salida.bytes_totales, int) else 0
        log.info("Script de %s (riesgo %s, huella %s): %s, código %s en %s s, %d bytes de salida",
                 propuesta.lenguaje, propuesta.analisis.nivel, propuesta.huella()[:12], codigo, codigo_salida,
                 duracion_txt, bytes_salida)
        if propuesta.habilidad:
            with contextlib.suppress(Exception):
                await self._contar_uso(propuesta.habilidad)
        return ResultadoScript(ok=codigo == "ejecutado", codigo=codigo, mensaje=mensaje, codigo_salida=codigo_salida,
                               tiempo_agotado=bool(salida.tiempo_agotado), duracion_s=duracion, stdout=stdout,
                               stderr=stderr, recortada=recortada, bytes_salida=bytes_salida,
                               carpeta=self._carpeta_relativa(carpeta))

    # ------------------------------------------------------------------
    # Habilidades (§9.5)
    # ------------------------------------------------------------------

    def _ruta_habilidades(self) -> Path:
        return self._taller_dir() / NOMBRE_HABILIDADES

    def _candado(self) -> asyncio.Lock:
        if self._candado_habilidades is None:
            self._candado_habilidades = asyncio.Lock()
        return self._candado_habilidades

    def _leer_habilidades(self) -> tuple[Habilidad, ...]:
        """Lee el archivo (en el executor). Corrupto → se renombra y se empieza vacío."""
        ruta = self._ruta_habilidades()
        if not ruta.is_file():
            return ()
        try:
            datos = json.loads(ruta.read_bytes().decode("utf-8"))
            if not isinstance(datos, Mapping) or not isinstance(datos.get("habilidades"), list):
                raise ValueError("forma inesperada")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            destino = ruta.with_name(f"{ruta.name}.corrupto-{int(self._reloj())}")
            try:
                os.replace(ruta, destino)
                log.warning("El archivo de habilidades estaba dañado (%s): lo renombré y empiezo vacío",
                            type(exc).__name__)
            except OSError as exc2:
                log.warning("El archivo de habilidades está dañado y no pude renombrarlo (%s)", type(exc2).__name__)
            return ()
        habilidades: list[Habilidad] = []
        descartadas = 0
        vistos: set[str] = set()
        for dato in datos["habilidades"]:
            habilidad = _habilidad_desde(dato)
            if habilidad is None or habilidad.id in vistos:
                descartadas += 1
                continue
            vistos.add(habilidad.id)
            habilidades.append(habilidad)
        if descartadas:
            log.warning("Descarté %d habilidades ilegibles del archivo", descartadas)
        return tuple(habilidades[:MAX_HABILIDADES])

    def _escribir_habilidades(self, habilidades: Sequence[Habilidad]) -> None:
        _crear_directorio_privado(self._taller_dir())
        _escribir_json_atomico(self._ruta_habilidades(),
                               {"version": 1, "habilidades": [h.a_dict() for h in habilidades]})

    async def _recargar_habilidades(self) -> tuple[Habilidad, ...]:
        loop = asyncio.get_running_loop()
        try:
            self._habilidades = await loop.run_in_executor(self._ejecutor, self._leer_habilidades)
        except Exception as exc:
            log.debug("No pude leer las habilidades (%s)", type(exc).__name__)
        return self._habilidades

    def habilidades(self) -> tuple[Habilidad, ...]:
        """Las habilidades guardadas (copia en memoria; se refresca en cada operación asíncrona)."""
        return self._habilidades

    @staticmethod
    def _buscar(habilidades: Sequence[Habilidad], nombre: str) -> Habilidad | None:
        if not isinstance(nombre, str):
            return None
        limpio = unicodedata.normalize("NFC", nombre.strip())
        slug = _slug(limpio)
        for habilidad in habilidades:
            if (slug and habilidad.id == slug) or habilidad.nombre.casefold() == limpio.casefold():
                return habilidad
        return None

    async def guardar_habilidad(self, id: str, huella: str, nombre: str) -> Habilidad | FalloTaller:
        """Guarda la propuesta (vigente o ejecutada en los últimos 30 min, con esa huella y ejecutable)."""
        if not self._habilitado:
            return FalloTaller("deshabilitado", "El taller está apagado (RAMS_TALLER=1 en el .env)")
        ahora = self._reloj()
        propuesta = self._vigente(id)
        if propuesta is not None and ahora > propuesta.caduca:
            if not (self._ejecutada and self._ejecutada[0].id == propuesta.id
                    and ahora - self._ejecutada[1] <= VENTANA_GUARDAR_S):
                return FalloTaller("caducado", "Esa propuesta caducó: pídemela de nuevo")
        if propuesta is None and self._ejecutada is not None and self._ejecutada[0].id == id \
                and ahora - self._ejecutada[1] <= VENTANA_GUARDAR_S:
            propuesta = self._ejecutada[0]
        if propuesta is None:
            return FalloTaller("no_existe", "Ese script ya no está vigente")
        if not _iguales(propuesta.huella(), huella):
            return FalloTaller("huella", "El código cambió desde que lo revisaste: revisa la versión nueva")
        if not propuesta.analisis.ejecutable:
            return FalloTaller("error", "No guardo como habilidad un script que no se puede ejecutar")
        limpio = unicodedata.normalize("NFC", nombre.strip()) if isinstance(nombre, str) else ""
        slug = _slug(limpio)
        if not limpio or any(_es_control(c) for c in limpio) or not _RE_NOMBRE_HABILIDAD.match(limpio) or not slug:
            return FalloTaller("nombre_invalido", "Ese nombre no me sirve: usa letras, números, espacios, "
                                                  "puntos o guiones (hasta 40)")
        try:
            async with self._candado():
                actuales = await self._recargar_habilidades()
                if self._buscar(actuales, limpio) is not None or any(h.id == slug for h in actuales):
                    return FalloTaller("nombre_invalido", "Ya tengo una habilidad con ese nombre: elige otro")
                if len(actuales) >= MAX_HABILIDADES:
                    return FalloTaller("error", f"Ya tengo {MAX_HABILIDADES} habilidades: olvida alguna antes")
                habilidad = Habilidad(
                    id=slug, nombre=limpio, descripcion=_recortar_utf16(propuesta.explicacion.replace("\n", " "),
                                                                        MAX_DESCRIPCION_HABILIDAD),
                    lenguaje=propuesta.lenguaje, plataforma=propuesta.plataforma, codigo=propuesta.codigo,
                    huella=propuesta.huella(), nivel=propuesta.analisis.nivel, creada_utc=_ahora_utc(ahora),
                    usada_utc=None, usos=0)
                nuevas = (*actuales, habilidad)
                await asyncio.get_running_loop().run_in_executor(self._ejecutor, self._escribir_habilidades, nuevas)
                self._habilidades = nuevas
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No pude guardar la habilidad (%s)", type(exc).__name__)
            return FalloTaller("error", "No pude guardar la habilidad (error al escribir)")
        actual = self._vigente(propuesta.id)
        if actual is not None:
            self._propuesta = replace(actual, habilidad=limpio, habilidad_cambiada=False)
        log.info("Habilidad guardada: %s, riesgo %s, huella %s", habilidad.lenguaje, habilidad.nivel,
                 habilidad.huella[:12])
        return habilidad

    async def cargar_habilidad(self, nombre: str, *, canal_origen: str, ttl_s: float) -> PropuestaScript | FalloTaller:
        """Crea una propuesta nueva con el código de la habilidad, **siempre** re-analizado."""
        if not self._habilitado:
            return FalloTaller("deshabilitado", "El taller está apagado (RAMS_TALLER=1 en el .env)")
        if canal_origen == "telegram":
            return FalloTaller("deshabilitado", "Los scripts solo se usan desde el computador")
        try:
            await self._asegurar_iniciado()
            async with self._candado():
                habilidades = await self._recargar_habilidades()
            habilidad = self._buscar(habilidades, nombre)
            if habilidad is None:
                return FalloTaller("no_existe", "No tengo ninguna habilidad con ese nombre")
            if habilidad.lenguaje not in self.lenguajes():
                return FalloTaller("lenguaje_no_disponible",
                                   f"Esa habilidad es de {habilidad.lenguaje}, que aquí no puedo ejecutar")
            codigo, motivo = normalizar_codigo(habilidad.codigo)
            if codigo is None:
                if motivo == "demasiado_grande":
                    return FalloTaller("demasiado_grande", "El código de esa habilidad es demasiado largo para "
                                                           "revisarlo en la tarjeta")
                return FalloTaller("error", "El código guardado de esa habilidad está dañado")
            analisis = await self._analizar(habilidad.lenguaje, codigo)
            ahora = self._reloj()
            propuesta = PropuestaScript(
                id=uuid.uuid4().hex[:8], version=1, tarea=_recortar_utf16(f"habilidad «{habilidad.nombre}»",
                                                                         MAX_TAREA_PROPUESTA),
                lenguaje=habilidad.lenguaje, plataforma=_plataforma_de(self._plataforma), codigo=codigo,
                explicacion=_texto_informativo(habilidad.descripcion, MAX_EXPLICACION, multilinea=True),
                efectos=(), requisitos=(), analisis=analisis, habilidad=habilidad.nombre[:MAX_NOMBRE_HABILIDAD],
                habilidad_cambiada=False, ejecuciones=0, canal_origen=_canal(canal_origen), creado=ahora,
                caduca=ahora + _ttl(ttl_s))
            propuesta = replace(propuesta, habilidad_cambiada=not _iguales(propuesta.huella(), habilidad.huella))
            self._propuesta = propuesta
            log.info("Habilidad cargada: %s, riesgo %s, %s", habilidad.lenguaje, analisis.nivel,
                     "cambiada" if propuesta.habilidad_cambiada else "sin cambios")
            return propuesta
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No pude cargar la habilidad (%s)", type(exc).__name__)
            return FalloTaller("error", "No pude cargar la habilidad (error interno)")

    async def olvidar_habilidad(self, nombre: str) -> bool:
        """Quita la habilidad del archivo. ``True`` si existía."""
        if not self._habilitado:
            return False
        try:
            async with self._candado():
                actuales = await self._recargar_habilidades()
                habilidad = self._buscar(actuales, nombre)
                if habilidad is None:
                    return False
                restantes = tuple(h for h in actuales if h.id != habilidad.id)
                await asyncio.get_running_loop().run_in_executor(self._ejecutor, self._escribir_habilidades,
                                                                 restantes)
                self._habilidades = restantes
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("No pude olvidar la habilidad (%s)", type(exc).__name__)
            return False

    async def _contar_uso(self, nombre: str) -> None:
        """Suma un uso a la habilidad ejecutada (``usos`` y ``usada_utc``)."""
        async with self._candado():
            actuales = await self._recargar_habilidades()
            habilidad = self._buscar(actuales, nombre)
            if habilidad is None:
                return
            usada = replace(habilidad, usos=habilidad.usos + 1, usada_utc=_ahora_utc(self._reloj()))
            nuevas = tuple(usada if h.id == habilidad.id else h for h in actuales)
            await asyncio.get_running_loop().run_in_executor(self._ejecutor, self._escribir_habilidades, nuevas)
            self._habilidades = nuevas


def _iguales(a: Any, b: Any) -> bool:
    """Comparación en tiempo constante de dos textos ASCII (ids y huellas); cualquier otra cosa ⇒ ``False``."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not a.isascii() or not b.isascii():
        return False
    return hmac.compare_digest(a, b)


def _canal(canal: Any) -> str:
    return canal if canal in CANALES_ORIGEN else "terminal"


def _ttl(ttl_s: Any) -> float:
    try:
        valor = float(ttl_s)
    except (TypeError, ValueError):
        return TTL_TARJETA_S
    return valor if math.isfinite(valor) and valor > 0 else TTL_TARJETA_S
