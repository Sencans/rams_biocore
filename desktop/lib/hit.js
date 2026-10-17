'use strict';
/**
 * Autoridad del click-through del overlay (COMPANION.md §C2.7.3). Módulo puro: sin Electron.
 *
 * El renderer (visor de VTuber AI Studio) solo SOLICITA ser interactivo; quien decide es el main de
 * Electron con estas reglas:
 * - Pasa a interactivo (`setIgnoreMouseEvents(false)`) solo si hay solicitud, hay una caja válida
 *   (`lastRect`) y el cursor GLOBAL está dentro de la caja con 8 px de margen.
 * - Vuelve al click-through (`setIgnoreMouseEvents(true, {forward:true})`) cuando el renderer lo
 *   pide, cuando el cursor lleva más de 1,5 s fuera de la caja (watchdog: tolera arrastres rápidos),
 *   al ocultarse, y siempre ante `render-process-gone`/`unresponsive` (`forzar`).
 * - La caja solo se acepta con `w ≤ 600`, `h ≤ 900` y dentro del workArea (la ventana). Una caja
 *   parcialmente fuera (asomada por el borde) se RECORTA al área; una caja inválida o del todo
 *   fuera se ignora y se conserva la anterior.
 *
 * Coordenadas: px CSS relativos a la ventana del overlay (el cursor ya llega convertido).
 */

const MARGEN_PX = 8;
const MAX_W = 600;
const MAX_H = 900;
const WATCHDOG_MS = 1500;
/** Cursor de Hyprland: 15 Hz cerca de la caja, 4 Hz si está a más de 300 px. */
const HYPR_RAPIDO_MS = Math.round(1000 / 15);
const HYPR_LENTO_MS = 250;
const HYPR_DISTANCIA_PX = 300;

function esNumero(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/** Normaliza el área `{width, height}`; `null` si no es válida. */
function normalizarArea(area) {
  if (!area || typeof area !== 'object') return null;
  const { width, height } = area;
  if (!esNumero(width) || !esNumero(height) || width <= 0 || height <= 0) return null;
  return { width, height };
}

/**
 * Valida y recorta la caja que informa el renderer.
 * @param {{x:number,y:number,w:number,h:number}} r  px CSS relativos a la ventana
 * @param {{width:number,height:number}} area  tamaño del workArea (contenido de la ventana)
 * @returns {{x:number,y:number,w:number,h:number}|null}
 */
function validarRect(r, area) {
  if (!r || typeof r !== 'object') return null;
  const { x, y, w, h } = r;
  if (![x, y, w, h].every(esNumero)) return null;
  if (!(w > 0 && h > 0 && w <= MAX_W && h <= MAX_H)) return null;
  const a = normalizarArea(area);
  if (!a) return null;
  const x0 = Math.max(0, x);
  const y0 = Math.max(0, y);
  const x1 = Math.min(a.width, x + w);
  const y1 = Math.min(a.height, y + h);
  if (x1 - x0 < 1 || y1 - y0 < 1) return null; // fuera del workArea
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** ¿El punto está dentro de la caja con `margen` px alrededor? */
function dentro(p, rect, margen = MARGEN_PX) {
  if (!p || !rect || !esNumero(p.x) || !esNumero(p.y)) return false;
  return p.x >= rect.x - margen && p.x <= rect.x + rect.w + margen &&
         p.y >= rect.y - margen && p.y <= rect.y + rect.h + margen;
}

/** Distancia euclídea del punto a la caja (0 si está dentro); `Infinity` sin datos. */
function distanciaARect(p, rect) {
  if (!p || !rect || !esNumero(p.x) || !esNumero(p.y)) return Infinity;
  const dx = Math.max(rect.x - p.x, 0, p.x - (rect.x + rect.w));
  const dy = Math.max(rect.y - p.y, 0, p.y - (rect.y + rect.h));
  return Math.hypot(dx, dy);
}

/** Intervalo de sondeo del cursor en Hyprland: 15 Hz cerca de la caja, 4 Hz lejos o sin caja. */
function intervaloCursorHyprMs(p, rect) {
  return distanciaARect(p, rect) > HYPR_DISTANCIA_PX ? HYPR_LENTO_MS : HYPR_RAPIDO_MS;
}

class AutoridadClicks {
  constructor({ margen = MARGEN_PX, watchdogMs = WATCHDOG_MS } = {}) {
    this.margen = margen;
    this.watchdogMs = watchdogMs;
    this.area = null;
    this.rect = null;
    this.cursor = null;
    this.solicitado = false;
    this.interactivo = false;
    this.habilitada = true;
    this.fueraDesde = null;
  }

  /** Tamaño de la ventana (workArea) en px CSS. Revalida la caja actual contra el área nueva. */
  fijarArea(area, ahora = Date.now()) {
    this.area = normalizarArea(area);
    if (this.rect) this.rect = validarRect(this.rect, this.area);
    return this._evaluar(ahora, 'area');
  }

  /**
   * Caja del avatar informada por el renderer.
   * @returns {{aceptado: boolean, cambio: object|null}}
   */
  fijarRect(r, ahora = Date.now()) {
    const v = validarRect(r, this.area);
    if (!v) return { aceptado: false, cambio: null }; // inválida: se ignora y se conserva la anterior
    this.rect = v;
    return { aceptado: true, cambio: this._evaluar(ahora, 'rect') };
  }

  /** Solicitud del renderer (`overlay:interactive`). Opcionalmente con el cursor recién leído. */
  solicitar(v, ahora = Date.now(), cursor) {
    this.solicitado = v === true;
    if (cursor !== undefined) this._guardarCursor(cursor);
    return this._evaluar(ahora, 'solicitud');
  }

  /** Nueva lectura del cursor global (px CSS relativos a la ventana) o `null` si no se sabe. */
  moverCursor(p, ahora = Date.now()) {
    this._guardarCursor(p);
    return this._evaluar(ahora, 'cursor');
  }

  /** Revisión periódica (watchdog) sin lectura nueva del cursor. */
  revisar(ahora = Date.now()) {
    return this._evaluar(ahora, 'watchdog');
  }

  /** Overlay oculto: nunca interactivo. */
  deshabilitar() {
    this.habilitada = false;
    this.solicitado = false;
    this.fueraDesde = null;
    return this._fijar(false, 'oculta');
  }

  /** Overlay visible otra vez: vuelve a aceptar solicitudes (empieza en click-through). */
  habilitar() {
    this.habilitada = true;
    this.solicitado = false;
    this.fueraDesde = null;
  }

  /**
   * Fuerza el click-through (caída del renderer, ventana que no responde, emergencia).
   * Devuelve SIEMPRE un cambio para que el main reaplique `setIgnoreMouseEvents(true, …)`.
   */
  forzar(motivo = 'forzado') {
    this.solicitado = false;
    this.fueraDesde = null;
    this.interactivo = false;
    return { interactivo: false, motivo, forzado: true };
  }

  /** Olvida la caja y el cursor (recarga del visor). */
  reiniciar() {
    this.rect = null;
    this.cursor = null;
    this.solicitado = false;
    this.fueraDesde = null;
    return this._fijar(false, 'reinicio');
  }

  _guardarCursor(p) {
    this.cursor = p && esNumero(p.x) && esNumero(p.y) ? { x: p.x, y: p.y } : null;
  }

  _fijar(v, motivo) {
    if (this.interactivo === v) return null;
    this.interactivo = v;
    return { interactivo: v, motivo };
  }

  _evaluar(ahora, motivo) {
    if (!this.habilitada) return this._fijar(false, 'oculta');
    const adentro = dentro(this.cursor, this.rect, this.margen);
    if (this.interactivo) {
      if (!this.solicitado) return this._fijar(false, 'solicitud');
      if (!this.rect) return this._fijar(false, 'sin_caja');
      if (adentro) {
        this.fueraDesde = null;
        return null;
      }
      if (this.fueraDesde === null) this.fueraDesde = ahora;
      if (ahora - this.fueraDesde > this.watchdogMs) {
        this.fueraDesde = null;
        return this._fijar(false, 'watchdog');
      }
      return null;
    }
    this.fueraDesde = null;
    if (this.solicitado && this.rect && adentro) return this._fijar(true, motivo);
    return null;
  }
}

/**
 * Limita el envío de la caja a R.A.M.S. (`{tipo:'caja'}`): como mucho a 2 Hz y solo si cambia.
 * `ofrecer` devuelve qué enviar ya y, si hay que esperar, cuánto (para un envío final diferido).
 */
class LimitadorCaja {
  constructor({ intervaloMs = 500 } = {}) {
    this.intervaloMs = intervaloMs;
    this.ultimo = null;
    this.ultimoTs = -Infinity;
    this.pendiente = null;
  }

  ofrecer(rect, ahora = Date.now()) {
    const r = redondearRect(rect);
    if (!r) return { enviar: null, esperaMs: null };
    if (igualesRect(r, this.ultimo)) {
      this.pendiente = null;
      return { enviar: null, esperaMs: null };
    }
    const espera = this.ultimoTs + this.intervaloMs - ahora;
    if (espera <= 0) {
      this.ultimo = r;
      this.ultimoTs = ahora;
      this.pendiente = null;
      return { enviar: r, esperaMs: null };
    }
    this.pendiente = r;
    return { enviar: null, esperaMs: Math.ceil(espera) };
  }

  /** Llamar cuando vence la espera: devuelve la caja pendiente si toca enviarla. */
  vencer(ahora = Date.now()) {
    if (!this.pendiente) return { enviar: null, esperaMs: null };
    const r = this.pendiente;
    this.pendiente = null;
    return this.ofrecer(r, ahora);
  }

  /** Tras reconectar: olvida lo enviado para volver a mandar la caja actual. */
  reiniciar() {
    this.ultimo = null;
    this.ultimoTs = -Infinity;
    this.pendiente = null;
  }
}

function redondearRect(r) {
  if (!r || ![r.x, r.y, r.w, r.h].every(esNumero)) return null;
  const out = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
  return out.w > 0 && out.h > 0 ? out : null;
}

function igualesRect(a, b) {
  return !!a && !!b && a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h;
}

module.exports = {
  MARGEN_PX,
  MAX_W,
  MAX_H,
  WATCHDOG_MS,
  HYPR_RAPIDO_MS,
  HYPR_LENTO_MS,
  HYPR_DISTANCIA_PX,
  validarRect,
  dentro,
  distanciaARect,
  intervaloCursorHyprMs,
  redondearRect,
  igualesRect,
  AutoridadClicks,
  LimitadorCaja,
};
