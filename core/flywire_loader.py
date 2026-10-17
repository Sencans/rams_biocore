"""Herramienta offline: CSV de FlyWire Codex (FAFB v783) → red CX+MB y estadísticas por población.

Qué hace
--------
Lee los CSV comprimidos que el usuario descarga **a mano** desde FlyWire Codex (FAFB → v783 →
*Download Data*) y los deja en ``data/flywire/``; selecciona las neuronas del Complejo Central y
del Cuerpo Fungiforme con el mapa de tipos del Anexo A.7, asigna el signo por neurotransmisor,
agrega las sinapsis por par en dos pasadas en *streaming* sobre ``connections.csv.gz`` y construye
una red CSR (``RedFlyWire``) con pesos de Shiu et al. (``w = signo·sinapsis·0,275 mV``). Guarda una
caché ``flywire_cxmb_783.npz`` con los md5 de los archivos de origen.

En v0.1 es **solo una herramienta offline**: valida los archivos y compara los conteos con los del
modelo reducido (Anexo A.3). La simulación en tiempo real (``core/sincan_connectome.py``) no la
usa; eso queda para v0.2, con proceso aparte y mapeo de estímulos. **No descarga nada.**

Uso::

    python -m core.flywire_loader --stats [DIRECTORIO]

Sale con 0 si la red es válida, o con 1 si faltan archivos o poblaciones obligatorias
(EPG, D7, KC_g o KC_ab, MBON_a3, PAM, PPL1 y dFB).

Archivos y columnas (se detectan alias y se registran las columnas encontradas)
-------------------------------------------------------------------------------
- ``connections.csv.gz`` (obligatorio): ``pre_root_id, post_root_id, neuropil, syn_count, nt_type``.
  Puede haber varias filas por par (una por neuropilo): se suman.
- ``consolidated_cell_types.csv.gz`` (``root_id, primary_type, additional_type(s)``) o
  ``classification.csv.gz`` (``root_id, …, class, sub_class, …``): al menos uno.
- ``neurons.csv.gz`` (opcional): ``root_id, nt_type`` (NT por neurona; si falta se vota con el
  ``nt_type`` de las conexiones salientes).
- ``names.csv.gz`` / ``labels.csv.gz`` (opcionales): columna angular E-PG a partir de ``_L3``/``_R5``.

Citas y licencia
----------------
- Dorkenwald, S. et al. (2024). *Neuronal wiring diagram of an adult brain.* Nature 634:124–138.
- Schlegel, P. et al. (2024). *Whole-brain annotation and multi-connectome cell typing of
  Drosophila.* Nature 634:139–152.
- Eckstein, N. et al. (2024). *Neurotransmitter classification from electron microscopy images
  at synaptic sites in Drosophila melanogaster.* Cell 187:2574–2594.
- Shiu, P. K. et al. (2024). *A Drosophila computational brain model reveals sensorimotor
  processing.* Nature 634:210–219 (modelo LIF y peso de 0,275 mV por sinapsis).

Los datos de FlyWire se usan bajo los FlyWire Terms of Service (https://flywire.ai/tos) y la
licencia CC-BY 4.0 de la versión publicada (Zenodo 10676866), con las citas de arriba. **No
redistribuyas los CSV ni la caché ``.npz`` en el repositorio.**

Dependencias y degradación: solo ``csv`` + ``gzip`` + numpy (sin pandas). Sin numpy,
``cargar_red_flywire`` devuelve ``None`` y la CLI sale con 1. El módulo se importa sin numpy.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sys
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

try:
    import numpy as np  # opcional: sin numpy no se construye la red
except ImportError:  # sin numpy: cargar_red_flywire devuelve None
    np = None  # type: ignore[assignment]

__all__ = [
    "RedFlyWire", "POBLACIONES", "MAPA_TIPOS", "SIGNO_NT", "OBLIGATORIAS", "MODELO_REDUCIDO",
    "cargar_red_flywire", "estadisticas", "main",
]

log = logging.getLogger("rams.conectoma")

# ---------------------------------------------------------------------------
# Constantes (Anexo A.7 y §10.7)
# ---------------------------------------------------------------------------

POBLACIONES: Final[tuple[str, ...]] = (
    "EPG", "PEN1", "PEN2", "PEG", "D7", "ER_vis", "ER5", "ExR1", "dFB", "FBt", "PFN",
    "hDelta", "KC_g", "KC_ab", "KC_apbp", "APL", "DPM", "MBON_a3", "MBON", "PAM", "PPL1",
)

# Regex → población; se evalúa EN ORDEN (la primera que coincide gana) sobre primary_type, luego
# cell_type y luego hemibrain_type (Anexo A.7).
MAPA_TIPOS: Final[list[tuple[str, str]]] = [
    (r"^EPGt?$|^E-PG", "EPG"), (r"^PEN_?a|PEN1", "PEN1"), (r"^PEN_?b|PEN2", "PEN2"),
    (r"^PEG$|^P-EG", "PEG"), (r"^Delta7|^Δ7", "D7"), (r"^ER5", "ER5"), (r"^ER\d", "ER_vis"),
    (r"^ExR1", "ExR1"), (r"^FB[67]", "dFB"), (r"^FB[1-5]", "FBt"), (r"^PFN", "PFN"),
    (r"^hDelta|^hΔ", "hDelta"), (r"^KCg", "KC_g"), (r"^KCab", "KC_ab"),
    (r"^KCa'b'|^KCapbp", "KC_apbp"), (r"^APL", "APL"), (r"^DPM", "DPM"),
    (r"MBON.*a'3|α'3|^MBON1[67]$", "MBON_a3"), (r"^MBON", "MBON"),
    (r"^PAM", "PAM"), (r"^PPL1", "PPL1"),
]

SIGNO_NT: Final[dict[str, float]] = {"GABA": -1.0, "GLUT": -1.0, "ACH": 1.0, "DA": 1.0, "SER": 1.0, "OCT": 1.0}

# Poblaciones obligatorias para dar la red por válida (una tupla = basta con una de ellas).
OBLIGATORIAS: Final[tuple[str | tuple[str, ...], ...]] = (
    "EPG", "D7", ("KC_g", "KC_ab"), "MBON_a3", "PAM", "PPL1", "dFB",
)

# Neuronas por población en el modelo reducido (Anexo A.3, modo normal). None = no existe en v0.1.
MODELO_REDUCIDO: Final[dict[str, int | None]] = {
    "EPG": 48, "PEN1": 32, "PEN2": 32, "PEG": 16, "D7": 32, "ER_vis": 48, "ER5": 16, "ExR1": 8,
    "dFB": 24, "FBt": None, "PFN": None, "hDelta": None, "KC_g": 300, "KC_ab": 450, "KC_apbp": 150,
    "APL": 1, "DPM": 1, "MBON_a3": 4, "MBON": 30, "PAM": 60, "PPL1": 12,
}

W_SYN_MV: Final = 0.275                 # Shiu et al. 2024: 0,275 mV por sinapsis
S_POB_RELATIVO: Final = 500.0           # sinapsis-equivalentes por población en el modo "relativo"
VERSION_DATOS: Final = "783"
VERSION_CACHE: Final = 1
ARCHIVO_CACHE: Final = "flywire_cxmb_783.npz"

ARCHIVO_CONEXIONES: Final = "connections.csv.gz"
ARCHIVOS_TIPOS: Final = ("consolidated_cell_types.csv.gz", "classification.csv.gz")
ARCHIVO_NEURONAS: Final = "neurons.csv.gz"
ARCHIVOS_NOMBRES: Final = ("names.csv.gz", "labels.csv.gz")

# Alias de columnas (en orden de preferencia).
_ALIAS: Final[dict[str, tuple[str, ...]]] = {
    "root": ("root_id", "pt_root_id", "root", "id"),
    "pre": ("pre_root_id", "pre_pt_root_id", "pre_id", "pre"),
    "post": ("post_root_id", "post_pt_root_id", "post_id", "post"),
    "syn": ("syn_count", "syn_cnt", "n_syn", "synapses", "weight", "count"),
    "nt": ("nt_type", "top_nt", "nt", "neurotransmitter", "predicted_nt"),
    "neuropil": ("neuropil",),
    "nombre": ("name", "label", "names", "labels"),
}
# Columnas de tipo, en el orden de evaluación de A.7 (y respaldos de classification.csv.gz).
_COLUMNAS_TIPO: Final = (
    "primary_type", "cell_type", "hemibrain_type", "additional_type", "additional_types",
    "additional_type(s)", "sub_class", "class",
)

_MAPA_COMPILADO: Final = [(re.compile(patron), pob) for patron, pob in MAPA_TIPOS]
_RE_COLUMNA: Final = re.compile(r"(?:^|[_\s(])([LR])(\d{1,2})(?:\b|[_)\s]|$)")

# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------


@dataclass
class RedFlyWire:
    """Red CX+MB construida a partir de FlyWire (CSR por presináptica)."""

    root_ids: Any                 # np.ndarray int64 [N]
    poblacion: Any                # np.ndarray int16 [N] índice en POBLACIONES
    indptr: Any                   # np.ndarray int64 [N+1]  CSR por presináptica
    post: Any                     # np.ndarray int32 [E]
    w: Any                        # np.ndarray float32 [E] mV (con signo)
    columna_epg: Any              # np.ndarray int8 [N] (−1 si no aplica; 0..15 para EPG)
    meta: dict[str, Any] = field(default_factory=dict)  # versión, fecha, md5 de origen, conteos por población


class _ErrorFormato(Exception):
    """Archivo de FlyWire con un formato no reconocido (columnas que faltan, gzip corrupto…)."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _clasificar(tipos: Sequence[str]) -> int | None:
    """Índice en ``POBLACIONES`` del primer tipo que encaja con ``MAPA_TIPOS`` (evaluado en orden)."""
    for tipo in tipos:
        t = (tipo or "").strip()
        if not t:
            continue
        for patron, pob in _MAPA_COMPILADO:
            if patron.search(t):
                return POBLACIONES.index(pob)
    return None


def _columna_epg(nombre: str) -> int:
    """Columna angular (0..15) a partir de un nombre con glomérulo ``R1``–``R8`` / ``L1``–``L8``; −1 si no hay."""
    m = _RE_COLUMNA.search(nombre or "")
    if not m:
        return -1
    lado, k = m.group(1), int(m.group(2))
    if not 1 <= k <= 8:
        return -1
    return (k - 1) if lado == "R" else 8 + (k - 1)


def _md5(ruta: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _filas(ruta: Path) -> Iterator[list[str]]:
    """Itera las filas de un CSV comprimido con gzip (la primera es la cabecera)."""
    with gzip.open(ruta, "rt", encoding="utf-8", newline="") as f:
        yield from csv.reader(f)


def _cabecera(ruta: Path) -> tuple[list[str], Iterator[list[str]]]:
    filas = _filas(ruta)
    try:
        cab = next(filas)
    except StopIteration as exc:
        raise _ErrorFormato(f"{ruta.name} está vacío") from exc
    return [c.strip().lower() for c in cab], filas


def _indice(cab: list[str], clave: str, archivo: str, *, obligatoria: bool = True) -> int | None:
    for alias in _ALIAS[clave]:
        if alias in cab:
            return cab.index(alias)
    if obligatoria:
        raise _ErrorFormato(f"{archivo}: falta la columna {clave} (alias aceptados: {', '.join(_ALIAS[clave])})")
    return None


def _entero(texto: str) -> int:
    """Entero exacto de un campo CSV (los root_id de FlyWire tienen 18 dígitos: nada de float)."""
    if not texto:
        return 0
    try:
        return int(texto)
    except ValueError:
        try:
            return int(float(texto))   # "12.0" en algunos exportes de recuentos
        except (ValueError, OverflowError):
            return 0


def _buscar(directorio: Path, nombres: Sequence[str]) -> Path | None:
    for nombre in nombres:
        ruta = directorio / nombre
        if ruta.is_file():
            return ruta
    return None


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------


def _leer_tipos(ruta: Path) -> tuple[dict[str, int], list[str]]:
    """``root_id`` (texto) → índice de población, para las neuronas CX+MB."""
    cab, filas = _cabecera(ruta)
    i_root = _indice(cab, "root", ruta.name)
    cols_tipo = [(c, cab.index(c)) for c in _COLUMNAS_TIPO if c in cab]
    if not cols_tipo:
        raise _ErrorFormato(f"{ruta.name}: no hay columnas de tipo ({', '.join(_COLUMNAS_TIPO)})")
    log.info("FlyWire: %s → raíz '%s', tipos %s", ruta.name, cab[i_root],  # type: ignore[index]
             ", ".join(c for c, _ in cols_tipo))
    seleccion: dict[str, int] = {}
    for fila in filas:
        if len(fila) <= i_root:  # type: ignore[operator]
            continue
        tipos = [fila[i] for _, i in cols_tipo if i < len(fila)]
        pob = _clasificar(tipos)
        if pob is not None:
            seleccion[fila[i_root].strip()] = pob  # type: ignore[index]
    return seleccion, [c for c, _ in cols_tipo]


def _leer_nt_neuronas(ruta: Path, seleccion: dict[str, int]) -> dict[str, str]:
    cab, filas = _cabecera(ruta)
    i_root = _indice(cab, "root", ruta.name)
    i_nt = _indice(cab, "nt", ruta.name, obligatoria=False)
    if i_nt is None:
        log.info("FlyWire: %s no tiene columna de neurotransmisor; se usará el voto de las conexiones", ruta.name)
        return {}
    log.info("FlyWire: %s → raíz '%s', NT '%s'", ruta.name, cab[i_root], cab[i_nt])  # type: ignore[index]
    nts: dict[str, str] = {}
    for fila in filas:
        if len(fila) <= max(i_root, i_nt):  # type: ignore[type-var]
            continue
        rid = fila[i_root].strip()  # type: ignore[index]
        if rid in seleccion:
            nts[rid] = fila[i_nt].strip().upper()
    return nts


def _leer_columnas_epg(ruta: Path, seleccion: dict[str, int]) -> dict[str, int]:
    cab, filas = _cabecera(ruta)
    i_root = _indice(cab, "root", ruta.name)
    i_nom = _indice(cab, "nombre", ruta.name, obligatoria=False)
    if i_nom is None:
        return {}
    epg = POBLACIONES.index("EPG")
    columnas: dict[str, int] = {}
    for fila in filas:
        if len(fila) <= max(i_root, i_nom):  # type: ignore[type-var]
            continue
        rid = fila[i_root].strip()  # type: ignore[index]
        if seleccion.get(rid) == epg and rid not in columnas:
            col = _columna_epg(fila[i_nom])
            if col >= 0:
                columnas[rid] = col
    return columnas


def _construir(directorio: Path, rutas: dict[str, Path | None], modo_pesos: str,
               md5s: dict[str, str]) -> RedFlyWire | None:
    ruta_tipos = rutas["tipos"]
    assert ruta_tipos is not None
    seleccion, columnas_tipo = _leer_tipos(ruta_tipos)
    if not seleccion:
        log.warning("FlyWire: ninguna neurona de %s encaja con el mapa de tipos CX+MB", ruta_tipos.name)
        return None
    nts = _leer_nt_neuronas(rutas["neuronas"], seleccion) if rutas["neuronas"] is not None else {}

    # Pasada 1 sobre las conexiones: entrada total por postsináptica seleccionada y voto de NT.
    ruta_con = rutas["conexiones"]
    assert ruta_con is not None
    cab, filas = _cabecera(ruta_con)
    i_pre = _indice(cab, "pre", ruta_con.name)
    i_post = _indice(cab, "post", ruta_con.name)
    i_syn = _indice(cab, "syn", ruta_con.name)
    i_nt = _indice(cab, "nt", ruta_con.name, obligatoria=False)
    log.info("FlyWire: %s → pre '%s', post '%s', sinapsis '%s', NT %s", ruta_con.name,
             cab[i_pre], cab[i_post], cab[i_syn],  # type: ignore[index]
             f"'{cab[i_nt]}'" if i_nt is not None else "(sin columna)")
    largo = max(i for i in (i_pre, i_post, i_syn, i_nt) if i is not None) + 1
    entrada_total: dict[str, int] = {}
    votos: dict[str, dict[str, int]] = {}
    filas_totales = 0
    for fila in filas:
        filas_totales += 1
        if len(fila) < largo:
            continue
        post = fila[i_post]  # type: ignore[index]
        if post in seleccion:
            entrada_total[post] = entrada_total.get(post, 0) + _entero(fila[i_syn])  # type: ignore[index]
        if i_nt is not None:
            pre = fila[i_pre]  # type: ignore[index]
            if pre in seleccion and pre not in nts:
                nt = fila[i_nt].strip().upper()
                if nt:
                    d = votos.setdefault(pre, {})
                    d[nt] = d.get(nt, 0) + _entero(fila[i_syn])  # type: ignore[index]

    # Pasada 2: aristas con pre y post seleccionadas, agregadas por par.
    _, filas = _cabecera(ruta_con)
    agregadas: dict[tuple[str, str], int] = {}
    for fila in filas:
        if len(fila) < largo:
            continue
        pre = fila[i_pre]  # type: ignore[index]
        if pre not in seleccion:
            continue
        post = fila[i_post]  # type: ignore[index]
        if post not in seleccion:
            continue
        clave = (pre, post)
        agregadas[clave] = agregadas.get(clave, 0) + _entero(fila[i_syn])  # type: ignore[index]

    # Neuronas ordenadas por población y root_id.
    ids = sorted(seleccion, key=lambda r: (seleccion[r], _entero(r)))
    indice = {rid: i for i, rid in enumerate(ids)}
    n = len(ids)
    signos = np.ones(n, dtype=np.float32)
    sin_nt = 0
    for rid, i in indice.items():
        nt = nts.get(rid)
        if not nt and rid in votos:
            nt = max(votos[rid].items(), key=lambda kv: kv[1])[0]
        if nt in SIGNO_NT:
            signos[i] = SIGNO_NT[nt]
        else:
            sin_nt += 1
    if sin_nt:
        log.info("FlyWire: %d neuronas sin neurotransmisor reconocido; se asume signo +1", sin_nt)

    columnas: dict[str, int] = {}
    ruta_nombres = rutas["nombres"]
    if ruta_nombres is not None:
        try:
            columnas = _leer_columnas_epg(ruta_nombres, seleccion)
        except (_ErrorFormato, OSError, EOFError, zlib.error, csv.Error) as exc:
            log.info("FlyWire: no se pudieron leer las columnas E-PG de %s (%s)", ruta_nombres.name, exc)

    pre_idx = np.empty(len(agregadas), dtype=np.int64)
    post_idx = np.empty(len(agregadas), dtype=np.int64)
    syn = np.empty(len(agregadas), dtype=np.float64)
    for k, ((pre, post), s) in enumerate(agregadas.items()):
        pre_idx[k] = indice[pre]
        post_idx[k] = indice[post]
        syn[k] = s
    if modo_pesos == "relativo":
        entrada = np.array([max(1, entrada_total.get(ids[j], 0)) for j in post_idx.tolist()], dtype=np.float64)
        w = signos[pre_idx] * (syn / entrada) * S_POB_RELATIVO * W_SYN_MV
    else:
        w = signos[pre_idx] * syn * W_SYN_MV
    orden = np.argsort(pre_idx, kind="stable")
    pre_idx, post_idx, w, syn = pre_idx[orden], post_idx[orden], w[orden], syn[orden]
    indptr = np.zeros(n + 1, dtype=np.int64)
    indptr[1:] = np.cumsum(np.bincount(pre_idx, minlength=n))

    poblacion = np.array([seleccion[r] for r in ids], dtype=np.int16)
    conteos: dict[str, dict[str, int]] = {}
    syn_por_pre = np.bincount(pre_idx, weights=syn, minlength=n) if len(syn) else np.zeros(n)
    for p, nombre in enumerate(POBLACIONES):
        mascara = poblacion == p
        conteos[nombre] = {"neuronas": int(mascara.sum()), "sinapsis_salida": int(round(float(syn_por_pre[mascara].sum())))}
    meta = {
        "version": VERSION_DATOS,
        "version_cache": VERSION_CACHE,
        "fecha": datetime.now(UTC).isoformat(timespec="seconds"),
        "md5": md5s,
        "modo_pesos": modo_pesos,
        "columnas_tipo": columnas_tipo,
        "filas_conexiones": filas_totales,
        "n_neuronas": n,
        "n_aristas": int(len(w)),
        "conteos": conteos,
    }
    return RedFlyWire(
        root_ids=np.array([_entero(r) for r in ids], dtype=np.int64),
        poblacion=poblacion,
        indptr=indptr,
        post=post_idx.astype(np.int32),
        w=w.astype(np.float32),
        columna_epg=np.array([columnas.get(r, -1) for r in ids], dtype=np.int8),
        meta=meta,
    )


def _leer_cache(ruta: Path, md5s: dict[str, str], modo_pesos: str) -> RedFlyWire | None:
    try:
        with np.load(ruta, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            if meta.get("version_cache") != VERSION_CACHE or meta.get("md5") != md5s \
                    or meta.get("modo_pesos") != modo_pesos:
                log.info("FlyWire: la caché %s no corresponde a los CSV actuales; la reconstruyo", ruta.name)
                return None
            return RedFlyWire(root_ids=z["root_ids"], poblacion=z["poblacion"], indptr=z["indptr"],
                              post=z["post"], w=z["w"], columna_epg=z["columna_epg"], meta=meta)
    except FileNotFoundError:
        return None
    except Exception as exc:  # caché corrupta: se reconstruye
        log.warning("FlyWire: caché ilegible (%s); la reconstruyo", exc)
        return None


def _escribir_cache(ruta: Path, red: RedFlyWire) -> None:
    try:
        memoria = io.BytesIO()
        np.savez_compressed(memoria, root_ids=red.root_ids, poblacion=red.poblacion, indptr=red.indptr,
                            post=red.post, w=red.w, columna_epg=red.columna_epg,
                            meta=np.array(json.dumps(red.meta, ensure_ascii=False)))
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(ruta.suffix + ".tmp")
        tmp.write_bytes(memoria.getvalue())
        os.replace(tmp, ruta)
        log.info("FlyWire: caché guardada en %s", ruta.name)
    except OSError as exc:
        log.warning("FlyWire: no se pudo guardar la caché (%s)", exc)


def cargar_red_flywire(directorio: Path, *, cache: Path | None = None, forzar: bool = False,
                       modo_pesos: str = "shiu") -> RedFlyWire | None:
    """Construye la red CX+MB desde los CSV de FlyWire Codex v783 en ``directorio``.

    Devuelve ``None`` (con ``log.warning`` que dice qué archivo falta) si no están
    ``connections.csv.gz`` y (``consolidated_cell_types.csv.gz`` o ``classification.csv.gz``),
    si numpy no está instalado o si los archivos no tienen el formato esperado. Nunca lanza.

    ``modo_pesos``: ``"shiu"`` (``w = signo·sinapsis·0,275 mV``) o ``"relativo"``
    (``w = signo·(sinapsis/entrada_total)·S_pob·0,275 mV``, como el *relative weight* de Hulse 2021).
    La caché (``directorio/flywire_cxmb_783.npz`` o ``cache``) se reutiliza si los md5 coinciden,
    salvo con ``forzar=True``.
    """
    try:
        if np is None:
            log.warning("FlyWire: numpy no está instalado; no se puede construir la red")
            return None
        directorio = Path(directorio)
        if modo_pesos not in ("shiu", "relativo"):
            log.warning("FlyWire: modo de pesos desconocido (%s); uso 'shiu'", str(modo_pesos)[:20])
            modo_pesos = "shiu"
        rutas: dict[str, Path | None] = {
            "conexiones": _buscar(directorio, (ARCHIVO_CONEXIONES,)),
            "tipos": _buscar(directorio, ARCHIVOS_TIPOS),
            "neuronas": _buscar(directorio, (ARCHIVO_NEURONAS,)),
            "nombres": _buscar(directorio, ARCHIVOS_NOMBRES),
        }
        faltan = []
        if rutas["conexiones"] is None:
            faltan.append(ARCHIVO_CONEXIONES)
        if rutas["tipos"] is None:
            faltan.append(" o ".join(ARCHIVOS_TIPOS))
        if faltan:
            log.warning("FlyWire: faltan archivos en %s: %s (descárgalos a mano desde Codex FAFB v783)",
                        directorio, "; ".join(faltan))
            return None
        md5s = {ruta.name: _md5(ruta) for ruta in rutas.values() if ruta is not None}
        ruta_cache = Path(cache) if cache is not None else directorio / ARCHIVO_CACHE
        if not forzar:
            red = _leer_cache(ruta_cache, md5s, modo_pesos)
            if red is not None:
                log.info("FlyWire: red cargada desde la caché %s", ruta_cache.name)
                return red
        red = _construir(directorio, rutas, modo_pesos, md5s)
        if red is not None:
            _escribir_cache(ruta_cache, red)
        return red
    except (_ErrorFormato, OSError, EOFError, zlib.error, csv.Error, ValueError, UnicodeDecodeError) as exc:
        log.warning("FlyWire: no se pudo leer la red (%s)", exc)
        return None
    except Exception as exc:  # contrato: nunca lanza
        log.warning("FlyWire: error inesperado al construir la red (%s: %s)", type(exc).__name__, exc)
        return None


def estadisticas(red: RedFlyWire) -> dict[str, dict[str, int]]:
    """``{poblacion: {"neuronas": n, "sinapsis_salida": e}}`` para todas las ``POBLACIONES``.

    ``sinapsis_salida`` es el total de sinapsis de las aristas que salen de la población hacia la
    red CX+MB (de ``meta["conteos"]`` si existe; si no, se estima con ``|w|/0,275``).
    """
    conteos = red.meta.get("conteos") if isinstance(red.meta, dict) else None
    if isinstance(conteos, dict) and all(p in conteos for p in POBLACIONES):
        return {p: {"neuronas": int(conteos[p].get("neuronas", 0)),
                    "sinapsis_salida": int(conteos[p].get("sinapsis_salida", 0))} for p in POBLACIONES}
    resultado: dict[str, dict[str, int]] = {}
    if np is None:
        return {p: {"neuronas": 0, "sinapsis_salida": 0} for p in POBLACIONES}
    n = len(red.poblacion)
    pre = np.repeat(np.arange(n), np.diff(red.indptr))
    syn = np.abs(np.asarray(red.w, dtype=np.float64)) / W_SYN_MV
    por_pre = np.bincount(pre, weights=syn, minlength=n) if len(syn) else np.zeros(n)
    for p, nombre in enumerate(POBLACIONES):
        mascara = np.asarray(red.poblacion) == p
        resultado[nombre] = {"neuronas": int(mascara.sum()), "sinapsis_salida": int(round(float(por_pre[mascara].sum())))}
    return resultado


def _faltan_obligatorias(stats: dict[str, dict[str, int]]) -> list[str]:
    faltan = []
    for req in OBLIGATORIAS:
        opciones = req if isinstance(req, tuple) else (req,)
        if not any(stats.get(p, {}).get("neuronas", 0) > 0 for p in opciones):
            faltan.append(" o ".join(opciones))
    return faltan


def _imprimir_tabla(stats: dict[str, dict[str, int]], salida: Any) -> None:
    print(f"{'Población':<10} {'FlyWire neuronas':>17} {'Sinapsis de salida':>19} {'Modelo reducido':>16}", file=salida)
    print("-" * 65, file=salida)
    for p in POBLACIONES:
        reducido = MODELO_REDUCIDO.get(p)
        texto_red = "—" if reducido is None else f"{reducido:,}".replace(",", " ")
        neuronas = f"{stats[p]['neuronas']:,}".replace(",", " ")
        sinapsis = f"{stats[p]['sinapsis_salida']:,}".replace(",", " ")
        print(f"{p:<10} {neuronas:>17} {sinapsis:>19} {texto_red:>16}", file=salida)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python -m core.flywire_loader --stats [DIRECTORIO]``.

    Imprime la tabla por población comparada con el modelo reducido y devuelve 0, o 1 si faltan
    archivos o poblaciones obligatorias. Sin ``--stats`` muestra la ayuda y devuelve 2.
    """
    parser = argparse.ArgumentParser(
        prog="python -m core.flywire_loader",
        description="Valida los CSV de FlyWire Codex v783 y compara la red CX+MB con el modelo reducido "
                    "(herramienta offline; no descarga nada).")
    parser.add_argument("--stats", nargs="?", const="", default=None, metavar="DIRECTORIO",
                        help="directorio con los CSV (por defecto, RAMS_FLYWIRE_DIR o data/flywire)")
    parser.add_argument("--forzar", action="store_true", help="ignora la caché .npz y reconstruye la red")
    parser.add_argument("--pesos", choices=("shiu", "relativo"), default="shiu", help="modo de pesos")
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # argparse sale con 2 ante argumentos inválidos
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.stats is None:
        parser.print_help()
        return 2
    if args.stats:
        directorio = Path(args.stats).expanduser()
    else:
        from core.config import cargar_settings  # import tardío: la CLI no debe leer .env al importarse
        directorio = cargar_settings().flywire_dir
    salida = sys.stdout
    print(f"FlyWire Codex v{VERSION_DATOS} · directorio: {directorio}", file=salida)
    if np is None:
        print("ERROR: numpy no está instalado; no se puede construir la red.", file=salida)
        return 1
    red = cargar_red_flywire(directorio, forzar=args.forzar, modo_pesos=args.pesos)
    if red is None:
        print("ERROR: faltan archivos o no tienen el formato esperado. Necesitas "
              f"{ARCHIVO_CONEXIONES} y {' o '.join(ARCHIVOS_TIPOS)} (descarga manual desde "
              "codex.flywire.ai, FAFB v783).", file=salida)
        return 1
    stats = estadisticas(red)
    _imprimir_tabla(stats, salida)
    meta = red.meta if isinstance(red.meta, dict) else {}
    print(f"Total: {meta.get('n_neuronas', len(red.poblacion))} neuronas CX+MB, "
          f"{meta.get('n_aristas', len(red.w))} aristas (pesos '{meta.get('modo_pesos', args.pesos)}').",
          file=salida)
    faltan = _faltan_obligatorias(stats)
    if faltan:
        print(f"ERROR: faltan poblaciones obligatorias: {', '.join(faltan)}.", file=salida)
        return 1
    print("Red válida. Recuerda: no redistribuyas los CSV de FlyWire (FlyWire ToS / CC-BY 4.0).", file=salida)
    return 0


if __name__ == "__main__":  # pragma: no cover - punto de entrada de la CLI
    sys.exit(main())
