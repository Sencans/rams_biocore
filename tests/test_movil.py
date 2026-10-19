"""Tests del servidor y la aplicación móvil Android (``movil/``).

Sin red externa ni puertos reales: usa ``127.0.0.1`` con puerto aleatorio (``puerto=0``),
``ServidorHUD`` efímero de prueba y ``httpx.MockTransport`` para simular ``vtuber-ai``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

RAIZ_PROYECTO = Path(__file__).resolve().parents[1]
if str(RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROYECTO))

import httpx
import websockets
from websockets.asyncio.client import connect

from core.bus import BusEventos
from core.config import Settings, cargar_settings
from core.contracts import EstadoModulo
from interface.hud_server import ServidorHUD
from movil.servidor_movil import CARPETA_PUBLIC, ServidorMovil, extraer_token_de_url


def test_extraer_token_de_url() -> None:
    assert extraer_token_de_url("http://127.0.0.1:8765/?t=tok_secreto_123") == "tok_secreto_123"
    assert extraer_token_de_url("ws://localhost:8765/ws?t=abc_XYZ-99") == "abc_XYZ-99"
    assert extraer_token_de_url("  token_directo_456  ") == "token_directo_456"
    assert extraer_token_de_url("http://127.0.0.1:8765/?t=<TOKEN_DEL_HUD>") == ""
    assert extraer_token_de_url("") == ""


def test_archivos_estaticos_movil_sin_innerhtml() -> None:
    """Verifica la regla 4.5.12 de IMPORTANTEIA.md: nunca usar innerHTML con datos en la UI móvil."""
    app_js = (CARPETA_PUBLIC / "app.js").read_text(encoding="utf-8")
    index_html = (CARPETA_PUBLIC / "index.html").read_text(encoding="utf-8")
    assert ".innerHTML" not in app_js
    assert ".outerHTML" not in app_js
    assert "document.write" not in app_js
    assert "<title>Amadeus Móvil" in index_html


def test_servidor_movil_http_y_autenticacion(correr: Any) -> None:
    comandos_recibidos: list[dict[str, Any]] = []

    def handler_vtuber(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "GET":
            html_falso = (
                "<!doctype html><html><head>"
                "<script type=\"importmap\">{}</script>"
                "</head><body>Visor VRM</body></html>"
            )
            return httpx.Response(200, text=html_falso, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.path == "/api/models" and request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "Kurisu.vrm", "url": "/models/Kurisu.vrm"}]})
        if request.url.path == "/api/command" and request.method == "POST":
            cuerpo = json.loads(request.content.decode("utf-8"))
            comandos_recibidos.append(cuerpo)
            return httpx.Response(200, json={"ok": True, "applied": cuerpo.get("type")})
        return httpx.Response(404, text="No encontrado")

    async def caso() -> None:
        cliente_mock = httpx.AsyncClient(transport=httpx.MockTransport(handler_vtuber))
        srv = ServidorMovil(
            host="127.0.0.1",
            puerto=0,
            token_hud="http://127.0.0.1:8765/?t=hud_tok_prueba",
            cliente_http=cliente_mock,
        )
        diag = await srv.iniciar()
        try:
            assert diag.estado is EstadoModulo.OK
            assert srv.puerto_real is not None and srv.puerto_real > 0
            assert srv.token not in diag.detalle
            assert srv.token not in repr(srv)

            base = f"http://127.0.0.1:{srv.puerto_real}"
            async with httpx.AsyncClient(trust_env=False, timeout=5.0) as cli:
                # Archivos estáticos PWA
                r_idx = await cli.get(f"{base}/")
                assert r_idx.status_code == 200
                assert "Amadeus Móvil" in r_idx.text
                assert r_idx.headers["x-content-type-options"] == "nosniff"

                r_man = await cli.get(f"{base}/manifest.json")
                assert r_man.status_code == 200
                assert "standalone" in r_man.text

                r_js = await cli.get(f"{base}/movil/app.js")
                assert r_js.status_code == 200

                # Endpoints protegidos por token móvil
                assert (await cli.get(f"{base}/rams/info")).status_code == 403
                r_info = await cli.get(f"{base}/rams/info", params={"t": srv.token})
                assert r_info.status_code == 200
                assert r_info.json()["token_hud_inicial"] == "hud_tok_prueba"

                # Proxy de /visor3d con adaptador SSE inyectado
                r_visor = await cli.get(f"{base}/visor3d", params={"obs": "1", "t": srv.token})
                assert r_visor.status_code == 200
                assert "/rams/vtuber-events" in r_visor.text

                # Proxy de /api/models y /rams/vtuber-command
                r_mod = await cli.get(f"{base}/api/models")
                assert r_mod.status_code == 200
                assert r_mod.json()["models"][0]["name"] == "Kurisu.vrm"

                cmd = {"type": "pose", "name": "thinking", "duration": 0.5}
                assert (await cli.get(f"{base}/rams/vtuber-command", params={"payload": json.dumps(cmd)})).status_code == 403
                r_cmd = await cli.get(
                    f"{base}/rams/vtuber-command",
                    params={"t": srv.token, "payload": json.dumps(cmd)},
                )
                assert r_cmd.status_code == 200
                assert r_cmd.json()["ok"] is True
                assert len(comandos_recibidos) == 1
                assert comandos_recibidos[0]["name"] == "thinking"
        finally:
            await srv.cerrar()
            await cliente_mock.aclose()

    correr(caso())


def test_puente_ws_con_hud_y_escritorio(settings_prueba: Settings, bus: BusEventos, correr: Any) -> None:
    async def caso() -> None:
        hud = ServidorHUD(
            settings_prueba.con(hud_habilitado=True, hud_puerto=0, escritorio_habilitado=True, hud_chat=True),
            bus,
        )
        await hud.iniciar()
        assert hud.puerto_real is not None

        srv = ServidorMovil(
            host="127.0.0.1",
            puerto=0,
            puerto_rams=hud.puerto_real,
            token_hud=hud.token,
        )
        await srv.iniciar()
        assert srv.puerto_real is not None

        try:
            url_ws_movil = f"ws://127.0.0.1:{srv.puerto_real}/rams/ws?t={srv.token}&hud_t={hud.token}"
            async with connect(url_ws_movil, open_timeout=5.0) as ws_hud_m:
                msg_hola = json.loads(await ws_hud_m.recv())
                assert msg_hola["tipo"] == "hola"

                msg_esc = json.loads(await ws_hud_m.recv())
                assert msg_esc["tipo"] == "escritorio"
                token_esc = msg_esc["datos"]["token"]
                assert len(token_esc) >= 16

                # Conectar al canal /rams/escritorio usando el token entregado por /ws
                url_esc_movil = (
                    f"ws://127.0.0.1:{srv.puerto_real}/rams/escritorio?t={srv.token}&esc_t={token_esc}"
                )
                recibido_ev = asyncio.Event()
                chats_recibidos: list[dict[str, Any]] = []

                def al_chat(ev: Any) -> None:
                    chats_recibidos.append(dict(ev.datos))
                    recibido_ev.set()

                desuscribir = bus.suscribir("escritorio.chat", al_chat)
                try:
                    async with connect(url_esc_movil, open_timeout=5.0) as ws_esc_m:
                        hola_esc = json.loads(await ws_esc_m.recv())
                        assert hola_esc["tipo"] == "hola"

                        await ws_esc_m.send(json.dumps({"tipo": "chat", "texto": "Hola desde Android"}))
                        await asyncio.wait_for(recibido_ev.wait(), timeout=3.0)
                        assert len(chats_recibidos) == 1
                        assert chats_recibidos[0]["texto"] == "Hola desde Android"
                finally:
                    desuscribir()
        finally:
            await srv.cerrar()
            await hud.cerrar()

    correr(caso())


if __name__ == "__main__":
    test_extraer_token_de_url()
    test_archivos_estaticos_movil_sin_innerhtml()
    test_servidor_movil_http_y_autenticacion(asyncio.run)
    test_puente_ws_con_hud_y_escritorio(Settings(offline=True), BusEventos(), asyncio.run)
    print("4/4 tests de movil pasaron correctamente.")

