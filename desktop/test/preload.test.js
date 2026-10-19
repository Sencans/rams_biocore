'use strict';
/**
 * Contrato IPC de los preloads (§C2.7.2): se ejecutan con un módulo `electron` falso que captura lo
 * que exponen por `contextBridge` y lo que mandan por `ipcRenderer`.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

function cargarPreload(archivo) {
  const expuesto = {};
  const enviados = [];
  const oyentes = new Map();
  const ipcRenderer = {
    send: (canal, ...args) => enviados.push(['send', canal, ...args]),
    invoke: (canal, ...args) => {
      enviados.push(['invoke', canal, ...args]);
      return Promise.resolve(false);
    },
    on: (canal, fn) => {
      if (!oyentes.has(canal)) oyentes.set(canal, new Set());
      oyentes.get(canal).add(fn);
    },
    removeListener: (canal, fn) => oyentes.get(canal)?.delete(fn),
  };
  const electron = {
    contextBridge: { exposeInMainWorld: (nombre, api) => { expuesto[nombre] = api; } },
    ipcRenderer,
  };
  const fuente = fs.readFileSync(path.join(__dirname, '..', archivo), 'utf8');
  const requerir = (m) => {
    if (m === 'electron') return electron;
    throw new Error(`el preload no debe cargar ${m}`); // sandbox: solo 'electron'
  };
  // eslint-disable-next-line no-new-func
  new Function('require', 'module', 'exports', fuente)(requerir, { exports: {} }, {});
  const emitir = (canal, dato) => {
    for (const fn of oyentes.get(canal) || []) fn({ sender: 'falso' }, dato);
  };
  return { expuesto, enviados, oyentes, emitir };
}

test('preload-overlay: window.amadeusDesktop v3 con la API exacta', () => {
  const { expuesto } = cargarPreload('preload-overlay.js');
  assert.deepEqual(Object.keys(expuesto), ['amadeusDesktop']);
  const api = expuesto.amadeusDesktop;
  assert.deepEqual(Object.keys(api).sort(), ['event', 'hidden', 'onCursor', 'onOrder', 'onVisibility', 'openChat',
    'reportAvatar', 'setInteractive', 'version'].sort());
  assert.equal(api.version, 3);
  assert.ok(!('ipcRenderer' in api));
});

test('preload-overlay: canales y saneo', () => {
  const { expuesto, enviados } = cargarPreload('preload-overlay.js');
  const api = expuesto.amadeusDesktop;
  api.setInteractive(true);
  api.setInteractive('sí'); // solo `true` estricto pide interactivo
  api.reportAvatar({ x: '10', y: 20, w: 340, h: NaN, extra: 'x' });
  api.reportAvatar(null);
  api.hidden();
  api.openChat();
  api.event('soltada', { altura: 0.4, objeto: { a: 1 }, f() {} });
  assert.deepEqual(enviados, [
    ['send', 'overlay:interactive', true],
    ['send', 'overlay:interactive', false],
    ['send', 'overlay:rect', { x: 10, y: 20, w: 340, h: 0 }],
    ['send', 'overlay:hidden'],
    ['send', 'overlay:chat'],
    ['send', 'overlay:event', 'soltada', { altura: 0.4 }],
  ]);
});

test('preload-overlay: onCursor/onOrder/onVisibility se suscriben y se cancelan', () => {
  const { expuesto, emitir, oyentes } = cargarPreload('preload-overlay.js');
  const api = expuesto.amadeusDesktop;
  const recibidos = [];
  const quitar = api.onOrder((o) => recibidos.push(o));
  api.onCursor((p) => recibidos.push(p));
  api.onVisibility((v) => recibidos.push(v));
  emitir('overlay:order', { accion: 'aparecer', borde: 'left', modo: 'entrar' });
  emitir('overlay:cursor', { x: 1, y: 2 });
  emitir('overlay:visibility', false);
  assert.deepEqual(recibidos, [{ accion: 'aparecer', borde: 'left', modo: 'entrar' }, { x: 1, y: 2 }, false]);
  quitar();
  assert.equal(oyentes.get('overlay:order').size, 0);
  assert.equal(typeof api.onOrder('no es función'), 'function');
});

test('preload-chat: window.amadeusChat v2 con la API exacta (la v1 + tarjetas)', () => {
  const { expuesto } = cargarPreload('preload-chat.js');
  assert.deepEqual(Object.keys(expuesto), ['amadeusChat']);
  const api = expuesto.amadeusChat;
  assert.deepEqual(Object.keys(api).sort(), ['abrirEnlace', 'confirmar', 'control', 'copiarCodigo', 'decidirTarjeta',
    'hide', 'onMessage', 'onPermiso', 'onStatus', 'onTarjeta', 'onVoz', 'send', 'version'].sort());
  assert.equal(api.version, 2);
  assert.ok(!('ipcRenderer' in api));
});

test('preload-chat: tarjetas — decidirTarjeta con extra plano (nunca recorta) y copiarCodigo solo con el id', async () => {
  const { expuesto, enviados, emitir } = cargarPreload('preload-chat.js');
  const api = expuesto.amadeusChat;
  const H = 'ab'.repeat(32);
  assert.equal(await api.decidirTarjeta('0a1b2c3d', H, 'aprobar'), false); // false = no salió
  const cuerpo = '😀'.repeat(5000); // 10 000 unidades: justo en el tope, intacto
  api.decidirTarjeta('0a1b2c3d', H, 'editar', { campos: { cuerpo, para: 'a@b.co', firma: 'x', otro: 1 }, extra: 'y' });
  api.decidirTarjeta('0a1b2c3d', H, 'editar', { campos: { cuerpo: `${cuerpo}x` } }); // 10 001 → campos null
  api.decidirTarjeta('0a1b2c3d', H, 'editar', { campos: { asunto: 42 } });          // no string → campos null
  api.decidirTarjeta('0a1b2c3d', H, 'editar', { campos: ['cuerpo'] });
  api.decidirTarjeta('5c1a7e00', H, 'guardar', { nombre: 'limpiar descargas' });
  api.decidirTarjeta('5c1a7e00', H, 'guardar', { nombre: 'n'.repeat(41) });
  api.decidirTarjeta({ malo: 1 }, H, 'aprobar', 'texto');
  // La versión que se ve: solo un entero 1..2³¹−1; si no, null (y R.A.M.S. no acepta la decisión).
  api.decidirTarjeta('0a1b2c3d', H, 'aprobar', { version: 4, otra: 1 });
  api.decidirTarjeta('0a1b2c3d', H, 'aprobar', { version: '4' });
  api.decidirTarjeta('0a1b2c3d', H, 'aprobar', { version: 2 ** 31 });
  api.decidirTarjeta('0a1b2c3d', H, 'editar', { version: 2, campos: { asunto: 'x' } });
  api.copiarCodigo('5c1a7e00-y-mas');
  api.copiarCodigo({ toString: () => 'deadbeef' });
  assert.deepEqual(enviados.slice(8, 12), [
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'aprobar', { version: 4 }],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'aprobar', { version: null }],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'aprobar', { version: null }],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'editar', { version: 2, campos: { asunto: 'x' } }],
  ]);
  enviados.splice(8, 4);
  assert.deepEqual(enviados, [
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'aprobar', {}],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'editar', { campos: { cuerpo, para: 'a@b.co' } }],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'editar', { campos: null }],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'editar', { campos: null }],
    ['invoke', 'chat:tarjeta', '0a1b2c3d', H, 'editar', { campos: null }],
    ['invoke', 'chat:tarjeta', '5c1a7e00', H, 'guardar', { nombre: 'limpiar descargas' }],
    ['invoke', 'chat:tarjeta', '5c1a7e00', H, 'guardar', { nombre: null }],
    ['invoke', 'chat:tarjeta', null, H, 'aprobar', {}],
    ['send', 'chat:copiar', '5c1a7e00'],
    ['send', 'chat:copiar', 'deadbeef'],
  ]);
  const recibidas = [];
  api.onTarjeta((t) => recibidas.push(t));
  emitir('chat:tarjeta', { id: '0a1b2c3d', clase: 'correo', estado: 'cerrada', resultado: 'enviado', motivo: '' });
  assert.equal(recibidas.length, 1);
});

test('preload-chat: canales', async () => {
  const { expuesto, enviados, emitir } = cargarPreload('preload-chat.js');
  const api = expuesto.amadeusChat;
  assert.equal(await api.send('hola'), false);
  api.send('x'.repeat(3000));
  // confirmar devuelve si la orden salió (con false el chat reactiva los botones)
  assert.equal(await api.confirmar('deadbeef', true), false);
  api.confirmar({ malo: 1 }, 'true');
  api.control('basta_travesuras');
  api.abrirEnlace('https://example.com');
  api.hide();
  assert.deepEqual(enviados[0], ['invoke', 'chat:send', 'hola']);
  assert.equal(enviados[1][2].length, 2000);
  assert.deepEqual(enviados.slice(2), [
    ['invoke', 'chat:confirm', 'deadbeef', true],
    ['invoke', 'chat:confirm', null, 'true'], // el main lo rechaza (id y booleano)
    ['send', 'chat:control', 'basta_travesuras'],
    ['send', 'chat:open-url', 'https://example.com'],
    ['send', 'chat:hide'],
  ]);
  const msgs = [];
  api.onMessage((m) => msgs.push(m));
  api.onStatus((s) => msgs.push(s));
  api.onVoz((v) => msgs.push(v));
  api.onPermiso((p) => msgs.push(p));
  emitir('chat:msg', { de: 'amadeus', texto: 'hola' });
  emitir('chat:status', 'en_linea');
  emitir('chat:voz', true);
  emitir('chat:permiso', { id: 'deadbeef', descripcion: 'x', pendiente: true });
  assert.equal(msgs.length, 4);
});
