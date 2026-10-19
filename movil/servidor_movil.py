"""Servidor pasarela para la aplicación móvil Android de Amadeus y VTuber AI Studio.

Qué hace:
- Escucha por defecto en ``127.0.0.1:8767`` (compatible con ``adb reverse tcp:8767 tcp:8767``
  sin exponer puertos en red abierta).
- Sirve la interfaz móvil PWA en ``/`` y ``/movil/*`` con cabeceras de seguridad (`nosniff`,
  `no-store`).
- Reenvía en local (``127.0.0.1``) los dos canales WebSocket de R.A.M.S. BIO-CORE:
  - ``/rams/ws`` → ``ws://127.0.0.1:<puerto_hud>/ws?t=<token_hud>`` (telemetría, conectoma,
    divergencia, presencia, cámara, visión, subtítulos, boca a 15 Hz, módulos y controles
    ``silenciar``, ``camara``, ``colgar``).
  - ``/rams/escritorio`` → ``ws://127.0.0.1:<puerto_hud>/escritorio?t=<token_escritorio>``
    con ``Origin: http://127.0.0.1:<puerto_hud>`` (modo cliente de chat RINE: no expulsa a
    Amadeus Desktop con código 4001, permite enviar ``chat``, resolver permisos con botón
    ``confirmacion`` y mandar controles ``no_molestar_on``, ``no_molestar_off``,
    ``basta_travesuras`` y ``pausar_vision``).
- Proxifica en local hacia ``VTuber AI Studio`` (``http://127.0.0.1:3939``) la escena 3D VRM
  (``/visor3d``, ``/app.js``, ``/protocol.js``, ``/amadeus_theme.js``, ``/amadeus_theme.css``,
  ``/style.css``, ``/vendor/*``, ``/models/*``), el flujo SSE ``/api/events`` y los endpoints
  ``/api/state``, ``/api/models``, ``/api/help`` y ``/api/command``.

Seguridad:
- El LLM nunca crea ni ejecuta acciones: todo mensaje del móvil entra por ``/escritorio``
  como ``chat`` para pasar por ``core.intenciones.enrutar`` y ``CompuertaPermisos.autorizar()``.
- El token del móvil se compara siempre con ``hmac.compare_digest`` y nunca se escribe en logs.
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit

from core.contracts import DiagnosticoModulo, EstadoModulo

try:
    import httpx
    HTTPX_DISPONIBLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_DISPONIBLE = False

try:
    from websockets.asyncio.client import connect as _ws_connect
    from websockets.asyncio.server import serve as _ws_serve
    from websockets.datastructures import Headers as _WsHeaders
    from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
    from websockets.http11 import Response as _WsResponse
    WEBSOCKETS_DISPONIBLE = True
except ImportError:
    _ws_connect = None  # type: ignore[assignment]
    _ws_serve = None  # type: ignore[assignment]
    _WsHeaders = None  # type: ignore[assignment]
    _WsResponse = None  # type: ignore[assignment]

    class _WsConnectionClosed(Exception):  # type: ignore[no-redef]
        """Sustituto cuando websockets no está instalado."""

    WEBSOCKETS_DISPONIBLE = False

__all__ = [
    "HOST_DEFECTO",
    "NOMBRE_DIAGNOSTICO",
    "PUERTO_DEFECTO",
    "PUERTO_RAMS_DEFECTO",
    "PUERTO_VTUBER_DEFECTO",
    "ServidorMovil",
    "extraer_token_de_url",
]

log = logging.getLogger("rams.movil")

HOST_DEFECTO: Final = "127.0.0.1"
PUERTO_DEFECTO: Final = 8767
PUERTO_RAMS_DEFECTO: Final = 8765
PUERTO_VTUBER_DEFECTO: Final = 3939
NOMBRE_DIAGNOSTICO: Final = "Enlace móvil Android"

CARPETA_PUBLIC: Final = Path(__file__).resolve().parent / "public"
_VTUBER_HERMANO: Final = Path(__file__).resolve().parents[2] / "vtuber-ai"
CARPETA_VTUBER: Final = (
    _VTUBER_HERMANO
    if (_VTUBER_HERMANO / "public" / "vendor").is_dir()
    else (Path(__file__).resolve().parents[1] / "vtuber-ai")
)
MAX_TAMANO_MENSAJE_WS: Final = 65536
ESPERA_CIERRE_S: Final = 3.0

MIME_POR_EXT: Final[dict[str, str]] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".vrm": "model/gltf-binary",
    ".glb": "model/gltf-binary",
}

PREFIJOS_VTUBER: Final = (
    "/vendor/",
    "/models/",
    "/api/",
)

RUTAS_VTUBER_EXACTAS: Final = frozenset({
    "/visor3d",
    "/app.js",
    "/style.css",
    "/protocol.js",
    "/amadeus_theme.js",
    "/amadeus_theme.css",
})


import os


def extraer_token_de_url(texto_o_url: str) -> str:
    """Extrae el parámetro ``?t=`` si se pasa una URL completa del HUD, o devuelve el token limpio."""
    limpio = (texto_o_url or "").strip()
    if not limpio or "<" in limpio or ">" in limpio:
        return ""
    if "://" in limpio or "?" in limpio or limpio.startswith("/"):
        partes = urlsplit(limpio)
        valores = parse_qs(partes.query, keep_blank_values=True).get("t") or [""]
        candidato = valores[0].strip()
        return "" if ("<" in candidato or ">" in candidato) else candidato
    return limpio


def _resolver_token_escritorio_local() -> tuple[int | None, str]:
    """Resuelve en tiempo de ejecución el puerto y token de ``/escritorio`` (igual que ``desktop/lib/token.js``).

    Nunca registra ni imprime el contenido del archivo.
    """
    ruta: Path | None = None
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA") or "").strip()
        if base:
            ruta = Path(base) / "rams" / "escritorio.json"
    else:
        xdg = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
        if xdg and Path(xdg).is_absolute():
            ruta = Path(xdg) / "rams" / "escritorio.json"

    if ruta is None or not ruta.is_file():
        return None, ""
    try:
        crudo = ruta.read_text(encoding="utf-8")
        if len(crudo) > 4096:
            return None, ""
        datos = json.loads(crudo)
        if not isinstance(datos, dict):
            return None, ""
        token = str(datos.get("token") or "").strip()
        url = str(datos.get("url") or "").strip()
        puerto: int | None = None
        if url.startswith("ws://"):
            partes = urlsplit(url)
            if partes.port:
                puerto = int(partes.port)
        return puerto, token
    except Exception:
        return None, ""


class ServidorMovil:
    """Pasarela local HTTP + WebSocket para la app móvil Android de Amadeus y vtuber-ai."""

    def __init__(
        self,
        *,
        host: str = HOST_DEFECTO,
        puerto: int = PUERTO_DEFECTO,
        puerto_rams: int = PUERTO_RAMS_DEFECTO,
        puerto_vtuber: int = PUERTO_VTUBER_DEFECTO,
        token_hud: str = "",
        token_movil: str | None = None,
        carpeta_public: Path = CARPETA_PUBLIC,
        cliente_http: Any | None = None,
    ) -> None:
        self._host = host
        self._puerto_cfg = int(puerto)
        self._puerto_rams = int(puerto_rams)
        self._puerto_vtuber = int(puerto_vtuber)
        self._token_hud_inicial = extraer_token_de_url(token_hud)
        self.token: str = token_movil if token_movil else secrets.token_urlsafe(24)
        self._carpeta_public = Path(carpeta_public).resolve()
        self._cliente_http_externo = cliente_http is not None
        self._cliente_http: Any | None = cliente_http
        self._servidor: Any | None = None
        self._puerto_real: int | None = None
        self._estado = EstadoModulo.EN_ESPERA
        self._detalle = "sin iniciar"
        self._clientes_activos: set[Any] = set()

    def __repr__(self) -> str:
        return f"ServidorMovil(host={self._host!r}, puerto={self.puerto_real!r}, estado={self._estado.value!r})"

    @property
    def puerto_real(self) -> int | None:
        """Puerto TCP en el que escucha el servidor móvil o ``None`` si está cerrado."""
        return self._puerto_real

    @property
    def url(self) -> str | None:
        """URL completa con token de sesión móvil (solo para mostrar en consola al usuario)."""
        if self._puerto_real is None:
            return None
        params = {"t": self.token}
        if self._token_hud_inicial:
            params["hud_t"] = self._token_hud_inicial
        return f"http://{self._host}:{self._puerto_real}/?{urlencode(params)}"

    def diagnostico(self) -> DiagnosticoModulo:
        """Devuelve el diagnóstico actual sin exponer nunca el token."""
        return DiagnosticoModulo(
            clave="movil",
            nombre=NOMBRE_DIAGNOSTICO,
            estado=self._estado,
            detalle=self._detalle,
        )

    async def iniciar(self) -> DiagnosticoModulo:
        """Arranca el servidor pasarela móvil. Nunca lanza excepciones."""
        if self._servidor is not None:
            return self.diagnostico()
        if not WEBSOCKETS_DISPONIBLE or _ws_serve is None:
            self._estado = EstadoModulo.OFF
            self._detalle = "websockets no instalado"
            return self.diagnostico()
        if not HTTPX_DISPONIBLE and self._cliente_http is None:
            self._estado = EstadoModulo.OFF
            self._detalle = "httpx no instalado"
            return self.diagnostico()

        if self._cliente_http is None and httpx is not None:
            self._cliente_http = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(10.0, connect=3.0),
            )

        try:
            servidor = await _ws_serve(
                self._manejar_ws,
                host=self._host,
                port=self._puerto_cfg,
                process_request=self._procesar_http,
                max_size=MAX_TAMANO_MENSAJE_WS,
                server_header=None,
            )
        except OSError as exc:
            self._estado = EstadoModulo.SIN_CONEXION
            self._detalle = f"no se pudo abrir puerto ({type(exc).__name__})"
            log.warning("No se pudo iniciar el servidor móvil en %s:%d", self._host, self._puerto_cfg)
            return self.diagnostico()
        except Exception as exc:
            self._estado = EstadoModulo.ERROR
            self._detalle = f"error al iniciar ({type(exc).__name__})"
            log.warning("Error inesperado al iniciar el servidor móvil: %s", type(exc).__name__)
            return self.diagnostico()

        self._servidor = servidor
        puerto_efectivo = self._puerto_cfg
        for sock in getattr(servidor, "sockets", None) or ():
            try:
                nombre_sock = sock.getsockname()
                if isinstance(nombre_sock, tuple) and len(nombre_sock) >= 2:
                    puerto_efectivo = int(nombre_sock[1])
                    break
            except Exception as exc:
                log.debug("No se pudo leer getsockname del socket móvil: %s", exc)
        self._puerto_real = puerto_efectivo
        self._estado = EstadoModulo.OK
        self._detalle = f"http://{self._host}:{puerto_efectivo}/"
        log.info("Servidor móvil Android escuchando en http://%s:%d/", self._host, puerto_efectivo)
        return self.diagnostico()

    async def cerrar(self) -> None:
        """Cierra las conexiones activas, el servidor y el cliente HTTP propio. Idempotente."""
        servidor = self._servidor
        self._servidor = None
        for ws in list(self._clientes_activos):
            try:
                await ws.close(1000, "cierre")
            except Exception as exc:
                log.debug("Error al cerrar conexión móvil: %s", exc)
        self._clientes_activos.clear()

        if servidor is not None:
            try:
                servidor.close(close_connections=True)
            except Exception as exc:
                log.debug("Error en close() del servidor móvil: %s", exc)
            try:
                await asyncio.wait_for(servidor.wait_closed(), timeout=ESPERA_CIERRE_S)
            except Exception as exc:
                log.debug("Espera de cierre del servidor móvil finalizada: %s", exc)

        if self._cliente_http is not None and not self._cliente_http_externo:
            try:
                await self._cliente_http.aclose()
            except Exception as exc:
                log.debug("Error al cerrar cliente HTTP móvil: %s", exc)
            self._cliente_http = None

        self._puerto_real = None
        self._estado = EstadoModulo.EN_ESPERA
        self._detalle = "cerrado"

    # ------------------------------------------------------------------
    # Validación de token y respuestas HTTP
    # ------------------------------------------------------------------

    def _token_valido(self, consulta: str, solicitud: Any | None = None) -> bool:
        """Compara el token ``?t=`` o cabecera ``X-Amadeus-Token`` en tiempo constante (o APK nativo)."""
        valores = parse_qs(consulta, keep_blank_values=True).get("t") or [""]
        candidato = valores[0]
        if not candidato and solicitud is not None:
            try:
                candidato = solicitud.headers.get("X-Amadeus-Token") or ""
                if not candidato:
                    origen = (solicitud.headers.get("Origin") or "").strip().lower()
                    if origen == "https://amadeus.local":
                        return True
            except Exception:
                candidato = ""
        if not candidato:
            return False
        return hmac.compare_digest(candidato.encode("utf-8"), self.token.encode("utf-8"))

    @staticmethod
    def _respuesta_http(
        estado: HTTPStatus,
        cuerpo: bytes,
        tipo_contenido: str = "text/plain; charset=utf-8",
        cabeceras_extra: list[tuple[str, str]] | None = None,
    ) -> Any:
        cabeceras = [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(cuerpo))),
            ("Content-Type", tipo_contenido),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Access-Control-Allow-Origin", "*"),
        ]
        if cabeceras_extra:
            cabeceras.extend(cabeceras_extra)
        return _WsResponse(estado.value, estado.phrase, _WsHeaders(cabeceras), cuerpo)

    def _servir_estatico_movil(self, nombre_relativo: str) -> Any:
        """Sirve un archivo estático dentro de ``movil/public/`` evitando traversal."""
        try:
            destino = (self._carpeta_public / nombre_relativo).resolve()
            if destino != self._carpeta_public and self._carpeta_public not in destino.parents:
                return self._respuesta_http(HTTPStatus.FORBIDDEN, b"Prohibido\n")
            if not destino.is_file():
                return self._respuesta_http(HTTPStatus.NOT_FOUND, b"No encontrado\n")
            contenido = destino.read_bytes()
            mime = MIME_POR_EXT.get(destino.suffix.lower(), "application/octet-stream")
            return self._respuesta_http(HTTPStatus.OK, contenido, mime)
        except Exception as exc:
            log.debug("Error al servir archivo móvil (%s): %s", nombre_relativo, type(exc).__name__)
            return self._respuesta_http(HTTPStatus.INTERNAL_SERVER_ERROR, b"Error interno\n")

    async def _procesar_http(self, conexion: Any, solicitud: Any) -> Any:
        """Intercepta peticiones HTTP antes del handshake WebSocket."""
        try:
            partes = urlsplit(solicitud.path)
            ruta = partes.path
            consulta = partes.query

            # Handshakes WebSocket hacia R.A.M.S. (/rams/ws, /rams/escritorio) y SSE emulado (/rams/vtuber-events)
            if ruta in ("/rams/ws", "/rams/escritorio", "/rams/vtuber-events"):
                if not self._token_valido(consulta, solicitud):
                    return self._respuesta_http(HTTPStatus.FORBIDDEN, b"Token movil invalido\n")
                return None  # continúa el handshake WebSocket en _manejar_ws

            # Archivos propios de la PWA móvil Android
            if ruta in ("/", "/index.html", "/movil", "/movil/"):
                return self._servir_estatico_movil("index.html")
            if ruta == "/manifest.json":
                return self._servir_estatico_movil("manifest.json")
            if ruta == "/sw.js":
                return self._servir_estatico_movil("sw.js")
            if ruta == "/favicon.ico":
                return self._respuesta_http(HTTPStatus.NO_CONTENT, b"")
            if ruta.startswith("/movil/"):
                rel = ruta[len("/movil/"):]
                return self._servir_estatico_movil(rel)

            # Estado rápido del puente móvil
            if ruta == "/rams/info":
                if not self._token_valido(consulta, solicitud):
                    return self._respuesta_http(HTTPStatus.FORBIDDEN, b"Prohibido\n")
                datos = {
                    "ok": True,
                    "puerto_rams": self._puerto_rams,
                    "puerto_vtuber": self._puerto_vtuber,
                    "tiene_token_hud_inicial": bool(self._token_hud_inicial),
                    "token_hud_inicial": self._token_hud_inicial,
                }
                cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
                return self._respuesta_http(HTTPStatus.OK, cuerpo, "application/json; charset=utf-8")

            # Envío de comandos JSON a vtuber-ai (GET/POST /rams/vtuber-command)
            if ruta == "/rams/vtuber-command":
                if not self._token_valido(consulta, solicitud):
                    return self._respuesta_http(HTTPStatus.FORBIDDEN, b"Prohibido\n")
                return await self._reenviar_comando_vtuber(consulta)

            # Proxy de recursos de vtuber-ai (:3939) para el visor VRM 3D integrado
            if ruta in RUTAS_VTUBER_EXACTAS or any(ruta.startswith(p) for p in PREFIJOS_VTUBER):
                return await self._proxificar_vtuber_get(ruta, consulta, solicitud)

            return self._respuesta_http(HTTPStatus.NOT_FOUND, b"No encontrado\n")
        except Exception as exc:
            log.debug("Excepción en _procesar_http móvil: %s", type(exc).__name__)
            return self._respuesta_http(HTTPStatus.INTERNAL_SERVER_ERROR, b"Error interno\n")

    async def _proxificar_vtuber_get(self, ruta: str, consulta: str, solicitud: Any) -> Any:
        """Obtiene un recurso estático o de estado desde ``vtuber-ai`` en ``127.0.0.1:3939``."""
        if self._cliente_http is None:
            return self._respuesta_http(HTTPStatus.SERVICE_UNAVAILABLE, b"Cliente HTTP no disponible\n")

        ruta_destino = "/" if ruta == "/visor3d" else ruta
        # En /api/events el visor de vtuber-ai usa EventSource; si el navegador pide /api/events por HTTP,
        # inyectamos en /visor3d un pequeño adaptador que redirige EventSource('/api/events') al WS /rams/vtuber-events.
        url_destino = f"http://127.0.0.1:{self._puerto_vtuber}{ruta_destino}"
        if consulta:
            url_destino = f"{url_destino}?{consulta}"

        cabeceras = {
            "Host": f"127.0.0.1:{self._puerto_vtuber}",
            "Origin": f"http://127.0.0.1:{self._puerto_vtuber}",
        }
        try:
            resp = await self._cliente_http.get(url_destino, headers=cabeceras)
            cuerpo = resp.content
            tipo = resp.headers.get("content-type", "application/octet-stream")

            # Si es el HTML del visor 3D (/visor3d), inyectamos el puente EventSource -> WebSocket
            # para que funcione a través del mismo servidor de websockets sin bloquear por streaming HTTP.
            if ruta == "/visor3d" and b"<script type=\"importmap\">" in cuerpo:
                css_full = (
                    "<style>"
                    "html,body,body.obs{display:block!important;width:100%!important;height:100%!important;"
                    "margin:0!important;padding:0!important;overflow:hidden!important}"
                    ".stage,#scene{position:absolute!important;inset:0!important;width:100%!important;height:100%!important}"
                    ".panel,.hud,.audio-hint{display:none!important}"
                    "</style>"
                )
                puente_sse = (
                    f"{css_full}<script>"
                    f"(() => {{"
                    f"  const OrigES = window.EventSource;"
                    f"  const tokenMovil = new URLSearchParams(location.search).get('t') || '';"
                    f"  window.EventSource = function(url) {{"
                    f"    if (String(url).includes('/api/events')) {{"
                    f"      const evTarget = new EventTarget();"
                    f"      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';"
                    f"      const wsUrl = proto + '//' + location.host + '/rams/vtuber-events?t=' + encodeURIComponent(tokenMovil) + '&' + location.search.slice(1);"
                    f"      let ws;"
                    f"      const conectar = () => {{"
                    f"        ws = new WebSocket(wsUrl);"
                    f"        ws.onmessage = (e) => {{"
                    f"          try {{"
                    f"            const msg = JSON.parse(e.data);"
                    f"            const evt = new MessageEvent(msg.event || 'message', {{ data: typeof msg.data === 'string' ? msg.data : JSON.stringify(msg.data) }});"
                    f"            evTarget.dispatchEvent(evt);"
                    f"            if (msg.event === 'message' && typeof evTarget.onmessage === 'function') evTarget.onmessage(evt);"
                    f"          }} catch (_) {{}}"
                    f"        }};"
                    f"        ws.onopen = () => {{ if (typeof evTarget.onopen === 'function') evTarget.onopen(); }};"
                    f"        ws.onclose = () => {{"
                    f"          if (typeof evTarget.onerror === 'function') evTarget.onerror();"
                    f"          setTimeout(conectar, 2000);"
                    f"        }};"
                    f"      }};"
                    f"      conectar();"
                    f"      evTarget.close = () => {{ try {{ ws.close(); }} catch (_) {{}} }};"
                    f"      return evTarget;"
                    f"    }}"
                    f"    return new OrigES(url);"
                    f"  }};"
                    f"}})();"
                    f"</script>"
                ).encode("utf-8")
                cuerpo = cuerpo.replace(b"<script type=\"importmap\">", puente_sse + b"\n<script type=\"importmap\">", 1)

            estado_http = HTTPStatus(resp.status_code) if resp.status_code in HTTPStatus._value2member_map_ else HTTPStatus.OK
            return self._respuesta_http(estado_http, cuerpo, tipo)
        except Exception as exc:
            log.debug("No se pudo conectar con vtuber-ai en 127.0.0.1:%d (%s)", self._puerto_vtuber, type(exc).__name__)
            fallback = self._servir_vtuber_local_fallback(ruta)
            if fallback is not None:
                return fallback
            if ruta == "/visor3d":
                aviso_html = (
                    "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<meta http-equiv='refresh' content='3'>"
                    "<style>body{margin:0;background:#070b14;color:#d6e6ff;font:13px monospace;"
                    "display:grid;place-items:center;height:100vh;text-align:center;padding:16px}</style>"
                    "</head><body><div><strong>VTuber AI Studio (:3939) no detectado</strong><br>"
                    "Arranca <code>node server.js</code> en <code>vtuber-ai</code> para ver el modelo VRM 3D.<br>"
                    "<small style='opacity:.7'>Reintentando automáticamente cada 3 s…</small></div>"
                    "</body></html>"
                ).encode("utf-8")
                return self._respuesta_http(HTTPStatus.OK, aviso_html, "text/html; charset=utf-8")
            cuerpo_err = json.dumps({"ok": False, "error": "vtuber_ai_sin_conexion"}).encode("utf-8")
            return self._respuesta_http(HTTPStatus.BAD_GATEWAY, cuerpo_err, "application/json; charset=utf-8")

    def _servir_vtuber_local_fallback(self, ruta: str) -> Any | None:
        """Sirve el visor 3D y modelos directamente desde ``vtuber-ai/`` si ``:3939`` está apagado."""
        if not CARPETA_VTUBER.is_dir():
            return None
        try:
            if ruta == "/api/models":
                carpeta_modelos = CARPETA_VTUBER / "models"
                lista: list[dict[str, str]] = []
                if carpeta_modelos.is_dir():
                    for arch in sorted(carpeta_modelos.iterdir()):
                        if arch.is_file() and arch.suffix.lower() == ".vrm":
                            lista.append({"name": arch.name, "url": f"/models/{arch.name}"})
                cuerpo_json = json.dumps({"models": lista}).encode("utf-8")
                return self._respuesta_http(HTTPStatus.OK, cuerpo_json, "application/json; charset=utf-8")

            if ruta == "/api/state":
                return self._respuesta_http(
                    HTTPStatus.OK,
                    b'{"ok":true,"ready":true,"modo":"local"}',
                    "application/json; charset=utf-8",
                )

            base_dir = CARPETA_VTUBER / "public"
            rel = "index.html" if ruta == "/visor3d" else ruta.lstrip("/")
            if ruta == "/protocol.js":
                base_dir = CARPETA_VTUBER / "lib"
                rel = "protocol.js"
            elif ruta.startswith("/models/"):
                base_dir = CARPETA_VTUBER / "models"
                from urllib.parse import unquote
                rel = unquote(ruta[len("/models/"):])

            base_resuelta = base_dir.resolve()
            destino = (base_resuelta / rel).resolve()
            if destino != base_resuelta and base_resuelta not in destino.parents:
                return self._respuesta_http(HTTPStatus.FORBIDDEN, b"Prohibido\n")
            if not destino.is_file():
                return None

            cuerpo = destino.read_bytes()
            mime = MIME_POR_EXT.get(destino.suffix.lower(), "application/octet-stream")
            if ruta == "/visor3d" and b"<script type=\"importmap\">" in cuerpo:
                puente_local = (
                    b"<style>"
                    b"html,body,body.obs{display:block!important;width:100%!important;height:100%!important;"
                    b"margin:0!important;padding:0!important;overflow:hidden!important}"
                    b".stage,#scene{position:absolute!important;inset:0!important;width:100%!important;height:100%!important}"
                    b".panel,.hud,.audio-hint{display:none!important}"
                    b"</style><script>"
                    b"(() => {"
                    b"  window.EventSource = function() {"
                    b"    const ev = new EventTarget();"
                    b"    ev.close = () => {};"
                    b"    setTimeout(() => {"
                    b"      ev.dispatchEvent(new MessageEvent('hello', { data: '{\"id\":\"local\",\"viewers\":1}' }));"
                    b"    }, 50);"
                    b"    return ev;"
                    b"  };"
                    b"})();"
                    b"</script>"
                )
                cuerpo = cuerpo.replace(b"<script type=\"importmap\">", puente_local + b"\n<script type=\"importmap\">", 1)
            return self._respuesta_http(HTTPStatus.OK, cuerpo, mime)
        except Exception as exc:
            log.debug("Fallback local de vtuber-ai falló (%s): %s", ruta, type(exc).__name__)
            return None

    async def _reenviar_comando_vtuber(self, consulta: str) -> Any:
        """Envía un comando JSON validado a ``POST http://127.0.0.1:3939/api/command``."""
        if self._cliente_http is None:
            return self._respuesta_http(HTTPStatus.SERVICE_UNAVAILABLE, b"{}")
        params = parse_qs(consulta, keep_blank_values=True)
        cmd_raw = (params.get("payload") or [""])[0]
        if not cmd_raw or len(cmd_raw) > 16384:
            return self._respuesta_http(
                HTTPStatus.BAD_REQUEST,
                b'{"ok":false,"error":"payload_invalido"}',
                "application/json; charset=utf-8",
            )
        try:
            objeto = json.loads(cmd_raw)
        except ValueError:
            return self._respuesta_http(
                HTTPStatus.BAD_REQUEST,
                b'{"ok":false,"error":"json_invalido"}',
                "application/json; charset=utf-8",
            )

        url = f"http://127.0.0.1:{self._puerto_vtuber}/api/command"
        cabeceras = {
            "Host": f"127.0.0.1:{self._puerto_vtuber}",
            "Origin": f"http://127.0.0.1:{self._puerto_vtuber}",
            "Content-Type": "application/json",
            "X-Source": "rams",
        }
        try:
            resp = await self._cliente_http.post(url, content=json.dumps(objeto).encode("utf-8"), headers=cabeceras)
            estado_http = HTTPStatus(resp.status_code) if resp.status_code in HTTPStatus._value2member_map_ else HTTPStatus.OK
            return self._respuesta_http(estado_http, resp.content, "application/json; charset=utf-8")
        except Exception as exc:
            log.debug("Fallo al enviar comando a vtuber-ai: %s", type(exc).__name__)
            return self._respuesta_http(
                HTTPStatus.BAD_GATEWAY,
                b'{"ok":false,"error":"vtuber_ai_sin_conexion"}',
                "application/json; charset=utf-8",
            )

    # ------------------------------------------------------------------
    # Puentes WebSocket: /rams/ws, /rams/escritorio y /rams/vtuber-events
    # ------------------------------------------------------------------

    async def _manejar_ws(self, conexion: Any) -> None:
        if self._servidor is None:
            return
        self._clientes_activos.add(conexion)
        try:
            partes = urlsplit(conexion.request.path)
            ruta = partes.path
            params = parse_qs(partes.query, keep_blank_values=True)

            if ruta == "/rams/ws":
                token_hud = extraer_token_de_url((params.get("hud_t") or [self._token_hud_inicial])[0])
                await self._puente_ws_rams(
                    conexion,
                    ruta_remota=f"/ws?t={token_hud}",
                )
            elif ruta == "/rams/escritorio":
                token_esc = extraer_token_de_url((params.get("esc_t") or [""])[0])
                if not token_esc:
                    _puerto_auto, token_esc = _resolver_token_escritorio_local()
                await self._puente_ws_rams(
                    conexion,
                    ruta_remota=f"/escritorio?t={token_esc}",
                )
            elif ruta == "/rams/vtuber-events":
                await self._puente_sse_vtuber(conexion, partes.query)
        except _WsConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Error en puente WS móvil: %s", type(exc).__name__)
        finally:
            self._clientes_activos.discard(conexion)

    async def _puente_ws_rams(self, ws_movil: Any, *, ruta_remota: str) -> None:
        """Releva bidireccionalmente un WebSocket del móvil hacia ``127.0.0.1:<puerto_rams>``."""
        if _ws_connect is None:
            return
        url_rams = f"ws://127.0.0.1:{self._puerto_rams}{ruta_remota}"
        origen_local = f"http://127.0.0.1:{self._puerto_rams}"

        try:
            async with _ws_connect(
                url_rams,
                origin=origen_local,
                open_timeout=5.0,
                max_size=MAX_TAMANO_MENSAJE_WS,
            ) as ws_rams:
                async def de_rams_a_movil() -> None:
                    try:
                        async for mensaje in ws_rams:
                            await ws_movil.send(mensaje)
                    except _WsConnectionClosed:
                        pass

                async def de_movil_a_rams() -> None:
                    try:
                        async for mensaje in ws_movil:
                            if isinstance(mensaje, str) and len(mensaje) <= MAX_TAMANO_MENSAJE_WS:
                                await ws_rams.send(mensaje)
                    except _WsConnectionClosed:
                        pass

                tareas = [
                    asyncio.create_task(de_rams_a_movil()),
                    asyncio.create_task(de_movil_a_rams()),
                ]
                hechas, pendientes = await asyncio.wait(tareas, return_when=asyncio.FIRST_COMPLETED)
                for t in pendientes:
                    t.cancel()
                await asyncio.gather(*tareas, return_exceptions=True)
        except Exception as exc:
            log.debug("Puente hacia R.A.M.S. cerrado (%s)", type(exc).__name__)
            try:
                await ws_movil.send(json.dumps({
                    "tipo": "error_puente",
                    "datos": {"motivo": "sin_enlace_rams", "detalle": type(exc).__name__},
                }))
            except Exception:
                pass

    async def _puente_sse_vtuber(self, ws_movil: Any, consulta: str) -> None:
        """Conecta al SSE ``GET http://127.0.0.1:3939/api/events`` y reenvía cada evento al visor móvil."""
        if self._cliente_http is None:
            return
        params = parse_qs(consulta, keep_blank_values=True)
        qs_vtuber = {}
        if "desktop" in params:
            qs_vtuber["desktop"] = params["desktop"][0]
        if "obs" in params:
            qs_vtuber["obs"] = params["obs"][0]
        sufijo = f"?{urlencode(qs_vtuber)}" if qs_vtuber else ""
        url = f"http://127.0.0.1:{self._puerto_vtuber}/api/events{sufijo}"
        cabeceras = {
            "Host": f"127.0.0.1:{self._puerto_vtuber}",
            "Origin": f"http://127.0.0.1:{self._puerto_vtuber}",
            "Accept": "text/event-stream",
        }
        try:
            async with self._cliente_http.stream("GET", url, headers=cabeceras, timeout=None) as resp:
                evento_actual = "message"
                lineas_datos: list[str] = []
                async for linea in resp.aiter_lines():
                    if linea.startswith(":"):
                        continue
                    if not linea:
                        if lineas_datos:
                            carga = "\n".join(lineas_datos)
                            await ws_movil.send(json.dumps({"event": evento_actual, "data": carga}))
                            evento_actual = "message"
                            lineas_datos = []
                        continue
                    if linea.startswith("event:"):
                        evento_actual = linea[len("event:"):].strip()
                    elif linea.startswith("data:"):
                        lineas_datos.append(linea[len("data:"):].strip())
        except Exception as exc:
            log.debug("Stream SSE de vtuber-ai finalizado: %s", type(exc).__name__)


def _buscar_binario_adb() -> str | None:
    """Localiza ``adb`` en ``PATH`` o en las ubicaciones estándar del SDK de Android."""
    en_path = shutil.which("adb")
    if en_path:
        return en_path
    candidatos: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidatos.append(Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    candidatos.append(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb")
    for ruta in candidatos:
        if ruta.is_file():
            return str(ruta)
    return None


def _configurar_adb_y_abrir(puerto: int, url: str, token: str = "") -> str:
    """Configura ``adb reverse`` y abre la app nativa ``com.rams.amadeus`` (o navegador) sin ``shell=True``."""
    adb_bin = _buscar_binario_adb()
    if not adb_bin:
        return f"adb no encontrado en PATH (ejecuta: adb reverse tcp:{puerto} tcp:{puerto})"
    try:
        res_rev = subprocess.run(
            [adb_bin, "reverse", f"tcp:{puerto}", f"tcp:{puerto}"],
            capture_output=True,
            text=True,
            timeout=6.0,
            check=False,
        )
        if res_rev.returncode != 0:
            return "sin dispositivo ADB activo (conecta por USB y activa Depuración USB)"
        if url:
            base_srv = f"http://127.0.0.1:{puerto}"
            res_nativo = subprocess.run(
                [
                    adb_bin, "shell", "am", "start",
                    "-n", "com.rams.amadeus/.AmadeusActivity",
                    "--es", "servidor_url", base_srv,
                    "--es", "token_movil", token,
                ],
                capture_output=True,
                text=True,
                timeout=6.0,
                check=False,
            )
            if res_nativo.returncode != 0 or "Error" in (res_nativo.stdout + res_nativo.stderr):
                subprocess.run(
                    [adb_bin, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                    capture_output=True,
                    text=True,
                    timeout=6.0,
                    check=False,
                )
        return f"túnel USB activo (tcp:{puerto}) · abierto automáticamente en el celular"
    except Exception as exc:
        return f"no se pudo configurar ADB automáticamente ({type(exc).__name__})"


async def _main_async(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pasarela local para la app móvil Android de Amadeus (R.A.M.S. + vtuber-ai).",
    )
    parser.add_argument("--host", default=HOST_DEFECTO, help="Host de escucha (por defecto 127.0.0.1)")
    parser.add_argument("--puerto", type=int, default=PUERTO_DEFECTO, help="Puerto del servidor móvil (defecto 8767)")
    parser.add_argument("--puerto-rams", type=int, default=PUERTO_RAMS_DEFECTO, help="Puerto del HUD de R.A.M.S. (defecto 8765)")
    parser.add_argument("--puerto-vtuber", type=int, default=PUERTO_VTUBER_DEFECTO, help="Puerto de vtuber-ai (defecto 3939)")
    parser.add_argument("--url-hud", default="", help="URL con ?t= o token del HUD de R.A.M.S.")
    args = parser.parse_args(argv)

    servidor = ServidorMovil(
        host=args.host,
        puerto=args.puerto,
        puerto_rams=args.puerto_rams,
        puerto_vtuber=args.puerto_vtuber,
        token_hud=args.url_hud,
    )
    diag = await servidor.iniciar()
    if diag.estado is not EstadoModulo.OK:
        print(f"[Amadeus Móvil] No se pudo iniciar: {diag.detalle}", file=sys.stderr)
        return 1

    estado_adb = _configurar_adb_y_abrir(servidor.puerto_real or args.puerto, servidor.url or "", servidor.token)

    print("=" * 72)
    print("  AMADEUS MOBILE ANDROID // R.A.M.S. BIO-CORE + VTUBER-AI")
    print(f"  Enlace móvil : {servidor.url}")
    print(f"  USB (ADB)    : {estado_adb}")
    print("=" * 72)

    parada = asyncio.Event()
    try:
        await parada.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await servidor.cerrar()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_main_async(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
