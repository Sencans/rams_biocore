"""Tests de core/memory_engine.py (grupo memory, §10.8 y §12.3).

Sin red ni hardware. Sin pytest-asyncio: cada prueba asíncrona corre con ``asyncio.run``
(fixture ``correr`` de tests/conftest.py) sobre ``settings_prueba`` (datos en ``tmp_path``).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import core.memory_engine as memory_engine
from core.config import Settings
from core.contracts import EstadoModulo, Etiquetas, ItemAgenda, Recuerdo, RegistroLlamadas, Turno
from core.memory_engine import MemoryEngine, extracto_salieri, hechos_desde_perfil, normalizar_texto

PERFIL_MINIMO = {
    "version": 1,
    "generado": "2026-10-16T22:41:03+00:00",
    "muletillas": [{"ngrama": "de una", "freq": 8, "lift": 3.1}, {"ngrama": "parce", "freq": 6, "lift": 2.0}],
    "horario": {"hora_pico": 22, "franja": "noche"},
    "estilo": {"longitud_media_palabras": 7.2, "mensajes_por_rafaga": 2.4, "pct_con_emoji": 0.18},
    "temas": [{"palabra": "código", "freq": 50}, {"palabra": "música", "freq": 20}],
}


def _contar(db: Path, sql: str) -> int:
    with contextlib.closing(sqlite3.connect(db)) as con:
        return int(con.execute(sql).fetchone()[0])


# ---------------------------------------------------------------------------
# Los 9 casos de §10.8
# ---------------------------------------------------------------------------


def test_abrir_ok_y_ventana_activa_ascendente(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[object, list[Turno], list[int]]:
        mem = MemoryEngine(settings_prueba)
        diag = await mem.abrir()
        ids = [await mem.registrar_turno("usuario" if i % 2 == 0 else "amadeus", f"mensaje número {i}",
                                         etiquetas=None if i % 2 == 0 else Etiquetas("smug", "idle", "sarcastico"))
               for i in range(30)]
        ventana = await mem.ventana_activa()
        await mem.cerrar()
        return diag, ventana, ids

    diag, ventana, ids = correr(principal())
    assert diag.clave == "memoria" and diag.estado is EstadoModulo.OK
    assert "john/" in diag.detalle
    assert all(i > 0 for i in ids)
    assert len(ventana) == 12
    assert [t.id for t in ventana] == sorted(t.id for t in ventana) == ids[-12:]
    assert ventana[-1].texto == "mensaje número 29"
    assert ventana[-1].etiquetas == Etiquetas("smug", "idle", "sarcastico")
    assert ventana[-2].etiquetas is None and ventana[-2].rol == "usuario"


def test_recordar_idempotente_con_tildes_y_buscar(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, int, list[Recuerdo], list[Recuerdo]]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        a = await mem.recordar("Me gusta la pizza hawaiana")
        b = await mem.recordar("me gusta la pízza   HAWÁIANA", importancia=0.9)
        await mem.recordar("Trabajo en un proyecto de neurociencia")
        encontrados = await mem.buscar("pizza")
        vacia = await mem.buscar("   ")
        await mem.cerrar()
        return a, b, encontrados, vacia

    a, b, encontrados, vacia = correr(principal())
    assert a > 0 and a == b
    assert encontrados and encontrados[0].contenido == "Me gusta la pizza hawaiana"
    assert encontrados[0].puntuacion > 0
    assert encontrados[0].importancia == pytest.approx(0.9)   # max(0.5, 0.9)
    assert vacia == []


def test_comprimir_forzado_da_kv0001(settings_prueba: Settings, correr) -> None:
    avisos: list[tuple[str, int, str]] = []

    async def principal() -> tuple[str | None, str | None, str | None, dict]:
        mem = MemoryEngine(settings_prueba, al_consolidar=lambda kv, n, m: avisos.append((kv, n, m)))
        await mem.abrir()
        for i in range(24):
            rol = "usuario" if i % 2 == 0 else "amadeus"
            await mem.registrar_turno(rol, f"Me gusta programar en Python los martes. Tengo {i} gatos en casa.")
        kv = await mem.comprimir(forzar=True)
        resumen = await mem.resumen_reciente()
        repetido = await mem.comprimir(forzar=True)        # quedan < 4 candidatos
        stats = await mem.estadisticas()
        await mem.cerrar()
        return kv, resumen, repetido, stats

    kv, resumen, repetido, stats = correr(principal())
    assert kv == "KV-0001"
    assert resumen is not None and resumen.startswith("KV-0001: ")
    assert "Sesión del " in resumen and "Tú: " in resumen
    assert repetido is None
    assert avisos == [("KV-0001", 12, "extractivo")]
    db = settings_prueba.db_path
    assert _contar(db, "SELECT count(*) FROM turnos WHERE consolidado = 1") == 12
    assert _contar(db, "SELECT count(*) FROM recuerdos WHERE categoria = 'consolidacion' AND kv_id = 'KV-0001'") == 1
    assert stats["consolidaciones"] == 1 and stats["ultimo_kv"] == "KV-0001"
    assert stats["ultimo_kv_ts"] and len(stats["ultimo_kv_ts"]) == len("2026-10-16 23:41")


def test_agenda_vencida_y_marcar_entregado(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, list[ItemAgenda], list[ItemAgenda], list[ItemAgenda]]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        pasado = datetime.now(UTC) - timedelta(minutes=5)
        futuro = datetime.now(UTC) + timedelta(days=1)
        item = await mem.programar("recordatorio", pasado, "llamar a mamá")
        await mem.programar("capsula", futuro, "abrir dentro de un día")
        antes = await mem.vencidos()
        await mem.marcar_entregado(item, "terminal")
        despues = await mem.vencidos()
        pendientes = await mem.listar_agenda()
        await mem.cerrar()
        return item, antes, despues, pendientes

    item, antes, despues, pendientes = correr(principal())
    assert [i.id for i in antes] == [item]
    assert antes[0].mensaje == "llamar a mamá" and antes[0].tipo == "recordatorio"
    assert despues == []
    assert [i.tipo for i in pendientes] == ["capsula"]


def test_llamadas_ignoradas_ultima_y_contar(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, dict | None, int, int, int]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        uno = await mem.registrar_llamada(origen="iniciativa", canal="telegram", nivel=1, motivo="conversar",
                                          texto="¿Sigues vivo? Llevas tres horas sin hablarme.")
        dos = await mem.registrar_llamada(origen="iniciativa", canal="telegram", nivel=2, motivo="descanso",
                                          texto="Segunda llamada: ve a dormir.")
        await mem.marcar_llamada(uno, False)
        await mem.marcar_llamada(dos, False)
        ignoradas = await mem.llamadas_ignoradas()
        ultima = await mem.ultima_llamada()
        sin_contestar = await mem.contar_llamadas(desde_ts=0)
        proactivos = await mem.contar_proactivos_hoy()
        contestadas = await mem.marcar_llamadas_contestadas(desde_ts=time.time() - 7200)
        await mem.cerrar()
        return ignoradas, ultima, sin_contestar, proactivos, contestadas

    ignoradas, ultima, sin_contestar, proactivos, contestadas = correr(principal())
    assert ignoradas == 2
    assert ultima is not None and ultima["texto"] == "Segunda llamada: ve a dormir."
    assert set(ultima) == {"id", "ts", "origen", "canal", "nivel", "motivo", "texto", "contestada"}
    assert ultima["contestada"] is False and ultima["nivel"] == 2
    assert sin_contestar == 2
    assert proactivos == 2
    assert contestadas == 0          # ya estaban rechazadas (contestada = 0), no NULL


def test_buscar_sin_fts5_por_atributo(settings_prueba: Settings, correr) -> None:
    async def principal() -> list[Recuerdo]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        await mem.recordar("Me gusta la pizza hawaiana")
        await mem.recordar("Odio el café frío")
        mem.fts5 = False
        resultado = await mem.buscar("pízza hawaiana")
        await mem.cerrar()
        return resultado

    resultado = correr(principal())
    assert len(resultado) == 1
    assert resultado[0].contenido == "Me gusta la pizza hawaiana" and resultado[0].puntuacion > 0


def test_buscar_sin_fts5_desde_la_apertura(settings_prueba: Settings, correr,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_engine, "_detectar_fts5", lambda con: False)

    async def principal() -> tuple[bool, list[Recuerdo], dict]:
        mem = MemoryEngine(settings_prueba)
        diag = await mem.abrir()
        assert "sin FTS5" in diag.detalle
        await mem.recordar("Prefiero el té verde por la mañana", categoria="preferencia")
        resultado = await mem.buscar("te verde")
        stats = await mem.estadisticas()
        await mem.cerrar()
        return mem.fts5, resultado, stats

    fts5, resultado, stats = correr(principal())
    assert fts5 is False and stats["fts5"] is False
    assert resultado and resultado[0].categoria == "preferencia"


def test_importar_perfil_idempotente(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, int, list[Recuerdo], dict]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        primero = await mem.importar_perfil(PERFIL_MINIMO)
        segundo = await mem.importar_perfil(dict(PERFIL_MINIMO))
        heredados = await mem.listar_recuerdos(20, tipo="heredado")
        stats = await mem.estadisticas()
        await mem.cerrar()
        return primero, segundo, heredados, stats

    primero, segundo, heredados, stats = correr(principal())
    assert 3 <= primero <= 6
    assert segundo == 0
    assert len(heredados) == primero
    assert all(r.tipo == "heredado" and r.categoria == "perfil" for r in heredados)
    contenidos = {r.contenido for r in heredados}
    assert "Muletillas de Lab Mem 001: «de una», «parce»." in contenidos
    assert "Suele escribir más hacia las 22 h (franja nocturna)." in contenidos
    assert "Escribe mensajes cortos (~7 palabras) en ráfagas de 2,4 mensajes." in contenidos
    assert stats["perfil_generado"] == "2026-10-16"


def test_meta_set_y_get(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[str | None, str | None, str | None]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        await mem.meta_set("explosion_fecha", "2026-10-16")
        valor = await mem.meta_get("explosion_fecha")
        inexistente = await mem.meta_get("inexistente")
        await mem.meta_set("kv_contador", "999")              # interna: se rechaza
        contador = await mem.meta_get("kv_contador")
        await mem.cerrar()
        return valor, inexistente, contador

    valor, inexistente, contador = correr(principal())
    assert valor == "2026-10-16"
    assert inexistente is None
    assert contador == "0"


def test_vencidos_trae_creado_ts(settings_prueba: Settings, correr) -> None:
    async def principal() -> list[ItemAgenda]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        await mem.programar("capsula", datetime.now(UTC) - timedelta(seconds=1), "mensaje al futuro")
        vencidos = await mem.vencidos()
        await mem.cerrar()
        return vencidos

    vencidos = correr(principal())
    assert len(vencidos) == 1 and isinstance(vencidos[0], ItemAgenda)
    assert vencidos[0].creado_ts and datetime.fromisoformat(vencidos[0].creado_ts).tzinfo is not None
    assert vencidos[0].estado == "pendiente"


# ---------------------------------------------------------------------------
# Degradación, contratos y casos adicionales
# ---------------------------------------------------------------------------


def test_degrada_a_memoria_si_no_se_puede_abrir(settings_prueba: Settings, correr, tmp_path: Path) -> None:
    carpeta = tmp_path / "no_es_una_base"
    carpeta.mkdir()

    async def principal() -> tuple[object, int, list[Recuerdo]]:
        mem = MemoryEngine(settings_prueba, ruta_db=carpeta)    # un directorio: SQLite no lo abre
        diag = await mem.abrir()
        ident = await mem.recordar("Me gusta la física de partículas")
        encontrados = await mem.buscar("partículas")
        await mem.cerrar()
        return diag, ident, encontrados

    diag, ident, encontrados = correr(principal())
    assert diag.estado is EstadoModulo.DEGRADADO
    assert ident > 0 and encontrados


def test_cumple_protocolo_y_cierre_idempotente(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[bool, int, list[Turno], object]:
        mem = MemoryEngine(settings_prueba)
        cumple = isinstance(mem, RegistroLlamadas)
        await mem.abrir()
        await mem.cerrar()
        await mem.cerrar()
        tras_cerrar = await mem.registrar_turno("usuario", "hola")
        ventana = await mem.ventana_activa()
        return cumple, tras_cerrar, ventana, mem.diagnostico()

    cumple, tras_cerrar, ventana, diag = correr(principal())
    assert cumple is True
    assert tras_cerrar == 0 and ventana == []
    assert diag.estado is EstadoModulo.OFF


def test_apertura_implicita_y_validaciones(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, int, int, int, bool]:
        mem = MemoryEngine(settings_prueba)
        ident = await mem.registrar_turno("usuario", "sin abrir antes")      # abre solo
        rol_malo = await mem.registrar_turno("intruso", "texto")
        vacio = await mem.recordar("   ")
        tipo_malo = await mem.programar("alarma", datetime.now(UTC), "x")
        olvidado = await mem.olvidar(12345)
        await mem.cerrar()
        return ident, rol_malo, vacio, tipo_malo, olvidado

    ident, rol_malo, vacio, tipo_malo, olvidado = correr(principal())
    assert ident > 0
    assert (rol_malo, vacio, tipo_malo, olvidado) == (0, 0, 0, False)


def test_olvidar_y_listar(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[bool, list[Recuerdo], list[Recuerdo]]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        uno = await mem.recordar("Mi cumpleaños es el 14 de marzo", importancia=0.8)
        await mem.recordar("Estudio neurociencia computacional")
        borrado = await mem.olvidar(uno)
        lista = await mem.listar_recuerdos(10)
        busqueda = await mem.buscar("cumpleaños marzo")
        await mem.cerrar()
        return borrado, lista, busqueda

    borrado, lista, busqueda = correr(principal())
    assert borrado is True
    assert [r.contenido for r in lista] == ["Estudio neurociencia computacional"]
    assert busqueda == []


def test_contar_proactivos_hoy_solo_iniciativa_por_telegram(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, int, int]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        await mem.registrar_llamada(origen="iniciativa", canal="telegram", nivel=1, motivo="conversar")
        await mem.registrar_llamada(origen="agenda", canal="telegram", nivel=1, motivo="recordatorio")
        await mem.registrar_llamada(origen="iniciativa", canal="voz", nivel=1, motivo="conversar")
        proactivos = await mem.contar_proactivos_hoy()
        marcadas = await mem.marcar_llamadas_contestadas()
        ignoradas = await mem.llamadas_ignoradas()
        await mem.cerrar()
        return proactivos, marcadas, ignoradas

    proactivos, marcadas, ignoradas = correr(principal())
    assert proactivos == 1
    assert marcadas == 3 and ignoradas == 0


def test_contar_consolidaciones_y_bitacora(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[int, int, str | None, int]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        inicio = time.time() - 1
        for i in range(20):
            await mem.registrar_turno("usuario", f"Necesito terminar el informe número {i} antes del viernes.")
        await mem.comprimir(forzar=True)
        consolidaciones = await mem.contar_consolidaciones(inicio)
        futuras = await mem.contar_consolidaciones(time.time() + 3600)
        await mem.escribir_bitacora("Sesión breve: el usuario terminó el informe.", 3600.0)
        bitacora = await mem.ultima_bitacora()
        stats = await mem.estadisticas()
        await mem.cerrar()
        return consolidaciones, futuras, bitacora, stats["fragmentos"]

    consolidaciones, futuras, bitacora, fragmentos = correr(principal())
    assert consolidaciones == 1 and futuras == 0
    assert bitacora == "Sesión breve: el usuario terminó el informe."
    assert fragmentos == 21     # 20 turnos + 1 fragmento de consolidación


def test_comprimir_con_resumidor_llm(settings_prueba: Settings, correr) -> None:
    llamadas: list[str] = []

    async def resumidor(texto: str) -> str | None:
        llamadas.append(texto)
        return "El usuario prefiere Python y trabaja los martes."

    async def principal() -> tuple[str | None, str | None]:
        mem = MemoryEngine(settings_prueba.con(compresion_llm=True), resumidor=resumidor)
        await mem.abrir()
        for i in range(20):
            await mem.registrar_turno("usuario", f"Prefiero Python, turno {i}.")
        kv = await mem.comprimir(forzar=True)
        resumen = await mem.resumen_reciente()
        await mem.cerrar()
        return kv, resumen

    kv, resumen = correr(principal())
    assert kv == "KV-0001" and llamadas and "Usuario: Prefiero Python" in llamadas[0]
    assert resumen is not None and resumen.endswith("El usuario prefiere Python y trabaja los martes.")
    assert _contar(settings_prueba.db_path, "SELECT count(*) FROM resumenes WHERE metodo = 'llm'") == 1


def test_resumidor_que_falla_cae_a_extractivo(settings_prueba: Settings, correr) -> None:
    async def resumidor(texto: str) -> str | None:
        raise RuntimeError("Gemini sin conexión")

    avisos: list[tuple[str, int, str]] = []

    async def principal() -> str | None:
        mem = MemoryEngine(settings_prueba.con(compresion_llm=True), resumidor=resumidor,
                           al_consolidar=lambda kv, n, m: avisos.append((kv, n, m)))
        await mem.abrir()
        for i in range(20):
            await mem.registrar_turno("usuario", f"Tengo una reunión el lunes número {i}.")
        kv = await mem.comprimir(forzar=True)
        await mem.cerrar()
        return kv

    assert correr(principal()) == "KV-0001"
    assert avisos[0][2] == "extractivo"


def test_bucle_compresor_consolida_en_ausencia_y_para(settings_prueba: Settings, correr) -> None:
    async def principal() -> tuple[str | None, float]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        for i in range(40):
            await mem.registrar_turno("usuario", f"Me gusta escuchar a Mozart mientras estudio, día {i}.")
        parar = asyncio.Event()
        tarea = asyncio.create_task(mem.bucle_compresor(parar, periodo_s=0.05, presente=lambda: False))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if await mem.resumen_reciente():
                break
        inicio = time.monotonic()
        parar.set()
        await asyncio.wait_for(tarea, timeout=2)
        demora = time.monotonic() - inicio
        resumen = await mem.resumen_reciente()
        await mem.cerrar()
        return resumen, demora

    resumen, demora = correr(principal())
    assert resumen is not None and resumen.startswith("KV-0001: ")
    assert demora < 1.0


def test_bucle_compresor_respeta_presencia(settings_prueba: Settings, correr) -> None:
    async def principal() -> str | None:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        for i in range(40):
            await mem.registrar_turno("usuario", f"Turno de prueba {i} con contenido suficiente.")
        parar = asyncio.Event()
        tarea = asyncio.create_task(mem.bucle_compresor(parar, periodo_s=0.05, presente=lambda: True))
        await asyncio.sleep(0.3)
        parar.set()
        await asyncio.wait_for(tarea, timeout=2)
        resumen = await mem.resumen_reciente()
        await mem.cerrar()
        return resumen

    assert correr(principal()) is None      # 28 candidatos < 80 y el usuario presente


def test_ventana_activa_excluye_sesiones_viejas(settings_prueba: Settings, correr) -> None:
    async def principal() -> list[Turno]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        await mem.registrar_turno("usuario", "turno de esta sesión")
        await mem.cerrar()
        return []

    correr(principal())
    viejo = (datetime.now(UTC) - timedelta(hours=7)).isoformat(timespec="seconds")
    with contextlib.closing(sqlite3.connect(settings_prueba.db_path)) as con:
        con.execute("INSERT INTO turnos(ts, sesion, rol, canal, texto) VALUES (?, 'otra0000', 'usuario', "
                    "'terminal', 'conversación vieja')", (viejo,))
        con.commit()

    async def segunda() -> list[Turno]:
        mem = MemoryEngine(settings_prueba)
        await mem.abrir()
        ventana = await mem.ventana_activa()
        await mem.cerrar()
        return ventana

    ventana = correr(segunda())
    assert [t.texto for t in ventana] == ["turno de esta sesión"]


# ---------------------------------------------------------------------------
# Funciones puras
# ---------------------------------------------------------------------------


def test_normalizar_texto() -> None:
    assert normalizar_texto("  Me GUSTA   la pízza\nHawáiana ") == "me gusta la pizza hawaiana"
    assert normalizar_texto(None) == ""


def test_extracto_salieri_formato() -> None:
    turnos = [
        (1, "2026-10-16T22:41:03+00:00", "usuario", "Hola. Me gusta la música de Mozart y tengo 3 gatos."),
        (2, "2026-10-16T22:41:10+00:00", "amadeus", "Anotado. Mozart, como el catálogo Köchel."),
        (3, "2026-10-16T22:42:00+00:00", "usuario", "ok"),
    ]
    from datetime import timezone
    texto = extracto_salieri(turnos, zona=timezone(timedelta(hours=-5)))
    assert texto.startswith("Sesión del 2026-10-16 17:41: Tú: ")
    assert "Me gusta la música de Mozart y tengo 3 gatos." in texto
    assert "ok" not in texto.split(": ", 1)[1].split()
    assert len(texto) < 700


def test_hechos_desde_perfil_siempre_al_menos_tres() -> None:
    assert 3 <= len(hechos_desde_perfil({}, "Lab Mem 001")) <= 6
    completo = hechos_desde_perfil({**PERFIL_MINIMO, "saludos": ["buenas"], "despedidas": ["chao"],
                                    "fuente": {"mensajes_propios": 120}}, "Okabe")
    assert len(completo) == 6
    assert completo[0] == "Muletillas de Okabe: «de una», «parce»."
    assert "Usa emojis en el 18 % de sus mensajes." in completo
    assert "Temas frecuentes: código, música." in completo
