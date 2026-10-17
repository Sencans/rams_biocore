'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  MARGEN_PX, WATCHDOG_MS, HYPR_RAPIDO_MS, HYPR_LENTO_MS,
  validarRect, dentro, distanciaARect, intervaloCursorHyprMs, AutoridadClicks, LimitadorCaja,
} = require('../lib/hit');

const AREA = { width: 1920, height: 1040 };
const CAJA = { x: 1500, y: 400, w: 340, h: 600 };

function autoridadLista() {
  const a = new AutoridadClicks();
  a.fijarArea(AREA, 0);
  assert.equal(a.fijarRect(CAJA, 0).aceptado, true);
  return a;
}

test('hit: dentro con margen de 8 px', () => {
  assert.equal(MARGEN_PX, 8);
  assert.equal(dentro({ x: 1500 - 8, y: 500 }, CAJA), true);
  assert.equal(dentro({ x: 1500 - 9, y: 500 }, CAJA), false);
  assert.equal(dentro({ x: 1840 + 8, y: 1000 + 8 }, CAJA), true);
  assert.equal(dentro({ x: 1840 + 8.5, y: 700 }, CAJA), false);
  assert.equal(dentro(null, CAJA), false);
  assert.equal(dentro({ x: NaN, y: 1 }, CAJA), false);
});

test('hit: validarRect acepta solo w ≤ 600, h ≤ 900 y dentro del workArea', () => {
  assert.deepEqual(validarRect(CAJA, AREA), CAJA);
  assert.equal(validarRect({ ...CAJA, w: 601 }, AREA), null);
  assert.equal(validarRect({ ...CAJA, h: 901 }, AREA), null);
  assert.equal(validarRect({ ...CAJA, w: 0 }, AREA), null);
  assert.equal(validarRect({ ...CAJA, x: Infinity }, AREA), null);
  assert.equal(validarRect({ x: '1', y: 2, w: 3, h: 4 }, AREA), null);
  assert.equal(validarRect({ x: 5000, y: 10, w: 300, h: 500 }, AREA), null); // fuera del todo
  assert.equal(validarRect(CAJA, null), null);
  // asomada por el borde derecho: se recorta al área
  assert.deepEqual(validarRect({ x: 1800, y: 400, w: 340, h: 600 }, AREA), { x: 1800, y: 400, w: 120, h: 600 });
});

test('hit: interactivo solo con solicitud y cursor dentro de la caja', () => {
  const a = autoridadLista();
  assert.equal(a.solicitar(true, 10, { x: 100, y: 100 }), null); // cursor fuera: sigue click-through
  assert.equal(a.interactivo, false);
  assert.deepEqual(a.moverCursor({ x: 1600, y: 700 }, 20), { interactivo: true, motivo: 'cursor' });
  assert.equal(a.interactivo, true);
  // sin solicitud, aunque el cursor esté dentro, nunca pasa a interactivo
  const b = autoridadLista();
  assert.equal(b.moverCursor({ x: 1600, y: 700 }, 5), null);
  assert.equal(b.interactivo, false);
});

test('hit: la solicitud de "no interactivo" se aplica al instante', () => {
  const a = autoridadLista();
  a.solicitar(true, 0, { x: 1600, y: 700 });
  assert.equal(a.interactivo, true);
  assert.deepEqual(a.solicitar(false, 1), { interactivo: false, motivo: 'solicitud' });
});

test('hit: una caja inválida se ignora y se conserva la anterior', () => {
  const a = autoridadLista();
  const r = a.fijarRect({ x: 0, y: 0, w: 1920, h: 1040 }, 1); // intento de cubrir la pantalla
  assert.deepEqual(r, { aceptado: false, cambio: null });
  assert.deepEqual(a.rect, CAJA);
  // el cursor en una zona que habría cubierto la caja falsa no la vuelve interactiva
  a.solicitar(true, 2, { x: 50, y: 50 });
  assert.equal(a.interactivo, false);
});

test('hit: sin caja nunca es interactivo', () => {
  const a = new AutoridadClicks();
  a.fijarArea(AREA, 0);
  assert.equal(a.solicitar(true, 0, { x: 10, y: 10 }), null);
  assert.equal(a.interactivo, false);
});

test('hit: watchdog — más de 1,5 s fuera de la caja vuelve al click-through', () => {
  assert.equal(WATCHDOG_MS, 1500);
  const a = autoridadLista();
  a.solicitar(true, 0, { x: 1600, y: 700 });
  assert.equal(a.interactivo, true);
  assert.equal(a.moverCursor({ x: 10, y: 10 }, 1000), null); // fuera (arrastre rápido): tolera
  assert.equal(a.revisar(2400), null);                        // 1,4 s fuera
  assert.equal(a.interactivo, true);
  assert.deepEqual(a.revisar(2501), { interactivo: false, motivo: 'watchdog' });
  assert.equal(a.interactivo, false);
  // con la solicitud aún activa, volver a entrar la reactiva
  assert.deepEqual(a.moverCursor({ x: 1600, y: 700 }, 2600), { interactivo: true, motivo: 'cursor' });
});

test('hit: volver a entrar antes de 1,5 s reinicia el watchdog', () => {
  const a = autoridadLista();
  a.solicitar(true, 0, { x: 1600, y: 700 });
  a.moverCursor({ x: 10, y: 10 }, 0);
  a.moverCursor({ x: 1600, y: 700 }, 1400);
  a.moverCursor({ x: 10, y: 10 }, 1450);
  assert.equal(a.revisar(2900), null); // 1,45 s desde la última salida
  assert.equal(a.interactivo, true);
  assert.equal(a.revisar(2951).interactivo, false);
});

test('hit: cursor desconocido cuenta como fuera para el watchdog', () => {
  const a = autoridadLista();
  a.solicitar(true, 0, { x: 1600, y: 700 });
  a.moverCursor(null, 100);
  assert.equal(a.revisar(1700).interactivo, false);
});

test('hit: crash/unresponsive → click-through forzado SIEMPRE', () => {
  const a = autoridadLista();
  a.solicitar(true, 0, { x: 1600, y: 700 });
  assert.equal(a.interactivo, true);
  assert.deepEqual(a.forzar('render-process-gone'), { interactivo: false, motivo: 'render-process-gone', forzado: true });
  assert.equal(a.interactivo, false);
  // también si ya estaba en click-through (el main reaplica setIgnoreMouseEvents(true))
  assert.equal(a.forzar('unresponsive').forzado, true);
  // la solicitud previa se olvida: el renderer tiene que volver a pedirlo
  assert.equal(a.moverCursor({ x: 1600, y: 700 }, 10), null);
});

test('hit: oculta → nunca interactivo; al mostrarse empieza en click-through', () => {
  const a = autoridadLista();
  a.solicitar(true, 0, { x: 1600, y: 700 });
  assert.deepEqual(a.deshabilitar(), { interactivo: false, motivo: 'oculta' });
  assert.equal(a.solicitar(true, 1, { x: 1600, y: 700 }), null);
  a.habilitar();
  assert.equal(a.interactivo, false);
  assert.equal(a.solicitar(true, 2, { x: 1600, y: 700 }).interactivo, true);
});

test('hit: cambiar el área revalida la caja', () => {
  const a = autoridadLista();
  a.fijarArea({ width: 1280, height: 720 }, 1); // monitor más pequeño: la caja queda fuera
  assert.equal(a.rect, null);
});

test('hit: cursor de Hyprland a 15 Hz cerca y 4 Hz lejos (> 300 px)', () => {
  assert.equal(distanciaARect({ x: 1600, y: 700 }, CAJA), 0);
  assert.equal(distanciaARect({ x: 1200, y: 700 }, CAJA), 300);
  assert.equal(intervaloCursorHyprMs({ x: 1200, y: 700 }, CAJA), HYPR_RAPIDO_MS);
  assert.equal(intervaloCursorHyprMs({ x: 1199, y: 700 }, CAJA), HYPR_LENTO_MS);
  assert.equal(intervaloCursorHyprMs(null, CAJA), HYPR_LENTO_MS);
  assert.equal(HYPR_RAPIDO_MS, 67);
  assert.equal(HYPR_LENTO_MS, 250);
});

test('hit: caja a R.A.M.S. a ≤ 2 Hz y solo si cambia', () => {
  const l = new LimitadorCaja({ intervaloMs: 500 });
  assert.deepEqual(l.ofrecer({ x: 1.4, y: 2.6, w: 340, h: 600 }, 0), { enviar: { x: 1, y: 3, w: 340, h: 600 }, esperaMs: null });
  assert.deepEqual(l.ofrecer({ x: 1, y: 3, w: 340, h: 600 }, 100), { enviar: null, esperaMs: null }); // igual
  assert.deepEqual(l.ofrecer({ x: 50, y: 3, w: 340, h: 600 }, 100), { enviar: null, esperaMs: 400 });
  assert.deepEqual(l.ofrecer({ x: 60, y: 3, w: 340, h: 600 }, 200), { enviar: null, esperaMs: 300 });
  assert.deepEqual(l.vencer(500), { enviar: { x: 60, y: 3, w: 340, h: 600 }, esperaMs: null }); // la última
  assert.deepEqual(l.vencer(600), { enviar: null, esperaMs: null });
  assert.deepEqual(l.ofrecer(null, 2000), { enviar: null, esperaMs: null });
  l.reiniciar();
  assert.deepEqual(l.ofrecer({ x: 60, y: 3, w: 340, h: 600 }, 700).enviar, { x: 60, y: 3, w: 340, h: 600 });
});
