'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  VISOR_DEFECTO, origenVisor, normalizarAcelerador, chocaConAltGr, resolverAtajos, ATAJOS_DEFECTO,
} = require('../lib/config');

// ------------------------------------------------------------------ VTUBER_AI_URL

test('config: VTUBER_AI_URL sin definir o vacía → defecto sin aviso', () => {
  for (const v of [undefined, null, '', '   ']) {
    assert.deepEqual(origenVisor(v), { origen: VISOR_DEFECTO, aviso: null });
  }
  assert.equal(VISOR_DEFECTO, 'http://127.0.0.1:3939');
});

test('config: VTUBER_AI_URL local → su origen', () => {
  assert.equal(origenVisor('http://127.0.0.1:3939').origen, 'http://127.0.0.1:3939');
  assert.equal(origenVisor('http://127.0.0.1:4000/').origen, 'http://127.0.0.1:4000');
  assert.equal(origenVisor(' http://localhost:3939 ').origen, 'http://localhost:3939');
  assert.equal(origenVisor('https://localhost').origen, 'https://localhost');
  assert.equal(origenVisor('http://[::1]:3939').origen, 'http://[::1]:3939');
  assert.equal(origenVisor('http://127.0.0.1:3939').aviso, null);
});

test('config: VTUBER_AI_URL remota o rara → defecto con aviso (sin repetir el valor)', () => {
  const casos = [
    'https://visor.ejemplo-remoto.com',
    'http://192.168.1.20:3939',
    'http://127.0.0.1.nip.io:3939',
    'http://localhost.evil.com',
    'http://usuario:clave@127.0.0.1:3939',
    'http://127.0.0.1:3939/ruta',
    'http://127.0.0.1:3939/?x=1',
    'file:///C:/visor/index.html',
    'ws://127.0.0.1:3939',
    'javascript:alert(1)',
    'http://127.0.0.1:99999',
    'http://127.0.0.1:0',
    `http://127.0.0.1:3939/${'a'.repeat(100)}`,
  ];
  for (const v of casos) {
    const r = origenVisor(v);
    assert.equal(r.origen, VISOR_DEFECTO, v);
    assert.ok(r.aviso && r.aviso.includes('origen local'), v);
    assert.ok(!r.aviso.includes('clave') && !r.aviso.includes('ejemplo-remoto'), 'el aviso no repite el valor');
  }
});

// ------------------------------------------------------------------ atajos

test('config: atajos por defecto — «Quieta» con Shift (Ctrl+Alt+Q = AltGr+Q = «@»)', () => {
  const { atajos, avisos } = resolverAtajos({}, 'win32');
  assert.deepEqual(atajos, [
    { accion: 'llamar', acelerador: 'Control+Alt+A' },
    { accion: 'chat', acelerador: 'Control+Alt+Space' },
    { accion: 'basta', acelerador: 'Control+Alt+Shift+Q' },
    { accion: 'vision', acelerador: 'Control+Alt+V' },
    { accion: 'emergencia', acelerador: 'Control+Alt+Shift+A' },
  ]);
  assert.deepEqual(avisos, []);
  assert.ok(!atajos.some((a) => a.acelerador === 'Control+Alt+Q'));
  assert.equal(chocaConAltGr('Control+Alt+Q'), true);
  assert.equal(chocaConAltGr(ATAJOS_DEFECTO.basta[1]), false);
});

test('config: AMADEUS_ATAJO_* cambia, normaliza o quita un atajo', () => {
  const { atajos, avisos } = resolverAtajos({
    AMADEUS_ATAJO_BASTA: ' ctrl+alt+shift+f9 ',
    AMADEUS_ATAJO_VISION: 'off',
    AMADEUS_ATAJO_CHAT: '',
  }, 'linux');
  assert.deepEqual(atajos.map((a) => [a.accion, a.acelerador]), [
    ['llamar', 'Control+Alt+A'],
    ['chat', 'Control+Alt+Space'],
    ['basta', 'Control+Alt+Shift+F9'],
    ['emergencia', 'Control+Alt+Shift+A'],
  ]);
  assert.deepEqual(avisos, []);
});

test('config: atajo inválido → defecto con aviso; repetido → solo el primero', () => {
  const r = resolverAtajos({ AMADEUS_ATAJO_BASTA: 'Q', AMADEUS_ATAJO_LLAMAR: 'Hyper+A' }, 'win32');
  assert.equal(r.atajos.find((a) => a.accion === 'basta').acelerador, 'Control+Alt+Shift+Q');
  assert.equal(r.atajos.find((a) => a.accion === 'llamar').acelerador, 'Control+Alt+A');
  assert.equal(r.avisos.length, 2);
  const d = resolverAtajos({ AMADEUS_ATAJO_VISION: 'Control+Alt+A' }, 'linux');
  assert.equal(d.atajos.some((a) => a.accion === 'vision'), false);
  assert.match(d.avisos[0], /ya es el atajo de «llamar»/);
});

test('config: en Windows avisa si el usuario elige Ctrl+Alt+<tecla> sin Shift (AltGr)', () => {
  const win = resolverAtajos({ AMADEUS_ATAJO_BASTA: 'Control+Alt+Q' }, 'win32');
  assert.equal(win.atajos.find((a) => a.accion === 'basta').acelerador, 'Control+Alt+Q'); // es su elección
  assert.match(win.avisos[0], /AltGr/);
  assert.deepEqual(resolverAtajos({ AMADEUS_ATAJO_BASTA: 'Control+Alt+Q' }, 'linux').avisos, []);
  assert.deepEqual(resolverAtajos({ AMADEUS_ATAJO_BASTA: 'Control+Alt+F9' }, 'win32').avisos, []);
});

test('config: normalizarAcelerador', () => {
  assert.equal(normalizarAcelerador('Ctrl+Alt+Shift+q'), 'Control+Alt+Shift+Q');
  assert.equal(normalizarAcelerador('CmdOrCtrl+Shift+Space'), 'CommandOrControl+Shift+Space');
  assert.equal(normalizarAcelerador('Super+F12'), 'Super+F12');
  assert.equal(normalizarAcelerador('Control+Alt++'), 'Control+Alt++');
  assert.equal(normalizarAcelerador('Control+Shift+Plus'), 'Control+Shift+Plus');
  for (const malo of ['', 'Q', 'Control+', '+Q', 'Control+Control+Q', 'Control+Alt+Ñ', 'Control+F25',
    'Control+Alt+QQ', 'Foo+Q', 42, null, `Control+${'A'.repeat(80)}`]) {
    assert.equal(normalizarAcelerador(malo), null, String(malo));
  }
});
