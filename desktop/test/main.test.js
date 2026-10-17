'use strict';
/**
 * `main.js` con un Electron FALSO (test/simulador-electron.js): sin ventanas reales, sin bandeja,
 * sin atajos globales, sin red (fetch y WebSocket simulados) y con temporizadores simulados.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { crearElectronFalso, crearWebSocketFalso, cargarMain, asentar, esperar } = require('./simulador-electron');
const { hashesImportmap } = require('../lib/csp');

const TOKEN = 'AbCdEfGhIjKlMnOpQrStUv';
const VISOR = 'http://127.0.0.1:3939';
const URL_OVERLAY = `${VISOR}/?obs=1&desktop=1&camera=full&theme=amadeus`;
const URL_CHAT = pathToFileURL(path.join(__dirname, '..', 'chat.html')).href;
const HTML_VISOR = '<!doctype html><script type="importmap">{"imports":{"three":"/vendor/three.module.js"}}</script>' +
  '<script type="module" src="/app.js"></script>';
const WAYLAND = process.platform === 'linux' && !!process.env.WAYLAND_DISPLAY;
// «Quieta» con Shift: en Windows Ctrl+Alt+Q es AltGr+Q = «@» en el teclado latinoamericano.
const ATAJOS = ['Control+Alt+A', 'Control+Alt+Space', 'Control+Alt+Shift+Q', 'Control+Alt+V', 'Control+Alt+Shift+A'];
const SIN_ATAJOS_PROPIOS = {
  AMADEUS_ATAJO_LLAMAR: '', AMADEUS_ATAJO_CHAT: '', AMADEUS_ATAJO_BASTA: '', AMADEUS_ATAJO_VISION: '', AMADEUS_ATAJO_EMERGENCIA: '',
};

async function escenario(t, { argv = ['--hidden'], html = HTML_VISOR, env: envExtra = {} } = {}) {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: 1_000_000 });
  const sim = crearElectronFalso();
  const { WebSocketFalso, instancias } = crearWebSocketFalso();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-main-test-'));
  const tokenFile = path.join(dir, 'escritorio.json');
  const escribirToken = (token) => {
    fs.writeFileSync(tokenFile, JSON.stringify({ url: 'ws://127.0.0.1:8765/escritorio', token, pid: process.pid }), { mode: 0o600 });
    fs.chmodSync(tokenFile, 0o600);
  };
  escribirToken(TOKEN);
  const pedidos = [];
  const estado = { html };
  const fetch = async (url) => {
    pedidos.push(url);
    if (estado.html === null) throw new Error('ECONNREFUSED (simulado)');
    return { ok: true, headers: { get: () => 'text/html; charset=utf-8' }, text: async () => estado.html };
  };
  const carga = cargarMain({
    electron: sim.electron,
    argv,
    env: {
      RAMS_ESCRITORIO_TOKEN_FILE: tokenFile,
      VTUBER_AI_URL: VISOR,
      AMADEUS_EXCLUIR_CAPTURA: '1',
      AMADEUS_AUTOARRANQUE: '',
      VTUBER_AI_TEMA: 'amadeus',
      ...SIN_ATAJOS_PROPIOS,
      ...envExtra,
    },
    fetch,
    WebSocket: WebSocketFalso,
  });
  t.after(async () => {
    // Detiene esta instancia de main.js (enlace, cursor…) para que nada se cuele en el siguiente
    // escenario, y espera a que termine cualquier lectura de disco pendiente.
    sim.electron.app.emit('second-instance', {}, [], '/', { v: 1, amadeus: 'quit', oculto: false, invalido: false });
    await asentar();
    carga.restaurar();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  await asentar();
  assert.ok(await esperar(() => instancias.length >= 1), 'no se abrió el WebSocket');
  const ov = sim.overlay();
  const ch = sim.chat();
  const avanzar = async (ms) => {
    t.mock.timers.tick(ms);
    await asentar();
  };
  const ws = () => instancias.at(-1);
  /** Carga el visor en el overlay, el chat y abre el WebSocket. */
  const todoListo = async () => {
    ov.webContents.simularCarga(URL_OVERLAY);
    ch.webContents.simularCarga(URL_CHAT);
    ws().abrir();
    ws().recibir({ tipo: 'hola', datos: { version: '0.2', nombre_ia: 'Amadeus', usuario: 'John' } });
    await asentar();
  };
  return { sim, ov, ch, instancias, ws, pedidos, estado, carga, avanzar, todoListo, escribirToken };
}

const ultimo = (lista, nombre) => lista.filter((l) => l[0] === nombre).at(-1);

test('main: arranque oculto — ventanas seguras, bandeja, atajos, visor tras GET y lock con additionalData', async (t) => {
  const { sim, ov, ch, pedidos, carga } = await escenario(t);
  // overlay
  assert.equal(ov.opciones.transparent, true);
  assert.equal(ov.opciones.frame, false);
  assert.equal(ov.opciones.show, false);
  assert.equal(ov.opciones.skipTaskbar, true);
  assert.equal(ov.opciones.focusable, WAYLAND);
  const wp = ov.opciones.webPreferences;
  assert.equal(wp.contextIsolation, true);
  assert.equal(wp.sandbox, true);
  assert.equal(wp.nodeIntegration, false);
  assert.equal(wp.partition, 'amadeus-overlay');
  assert.ok(wp.preload.endsWith('preload-overlay.js'));
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', true, { forward: true }]);
  assert.deepEqual(ultimo(ov.llamadas, 'setAlwaysOnTop'), ['setAlwaysOnTop', true, 'screen-saver']);
  if (process.platform === 'win32') assert.deepEqual(ultimo(ov.llamadas, 'setContentProtection'), ['setContentProtection', true]);
  assert.equal(ov.webContents.throttling, true); // oculta: sin gastar GPU
  assert.ok(!ov.nombres().includes('showInactive') && !ov.nombres().includes('show'));
  // el visor se carga SOLO tras un GET / correcto
  assert.deepEqual(pedidos, [`${VISOR}/`]);
  assert.deepEqual(ultimo(ov.llamadas, 'loadURL'), ['loadURL', URL_OVERLAY]);
  // chat
  assert.equal(ch.opciones.title, 'Amadeus Chat');
  assert.equal(ch.opciones.webPreferences.contextIsolation, true);
  assert.equal(ch.opciones.webPreferences.sandbox, true);
  assert.equal(ch.opciones.webPreferences.partition, 'amadeus-chat');
  assert.ok(ultimo(ch.llamadas, 'loadFile')[1].endsWith('chat.html'));
  assert.ok(!ch.nombres().includes('show') && !ch.nombres().includes('showInactive'));
  // instancia única con additionalData, bandeja y atajos (Win/X11)
  assert.deepEqual(sim.r.app.locks, [{ v: 1, amadeus: null, oculto: true, invalido: false }]);
  assert.equal(sim.r.bandejas.length, 1);
  const etiquetas = sim.r.bandejas[0].menu.map((i) => i.label).filter(Boolean);
  for (const e of ['Llamar a Amadeus', 'Abrir chat', 'No molestar', 'Quieta', 'Pausar visión', 'Dejar pasar clics', 'Salir']) {
    assert.ok(etiquetas.includes(e), e);
  }
  assert.deepEqual(sim.r.atajos.map((a) => a[0]), WAYLAND ? [] : ATAJOS);
  assert.deepEqual(sim.r.loginItems, []); // sin AMADEUS_AUTOARRANQUE no se toca el sistema
  assert.ok(!carga.logs.join('\n').includes(TOKEN));
});

test('main: CSP por cabecera solo en los documentos del visor y permisos denegados', async (t) => {
  const { sim } = await escenario(t);
  const ses = sim.r.sesiones.get('amadeus-overlay');
  let res;
  ses.handlers.headers({ url: `${VISOR}/?obs=1`, resourceType: 'mainFrame', responseHeaders: { 'content-type': ['text/html'] } }, (x) => { res = x; });
  const csp = res.responseHeaders['Content-Security-Policy'][0];
  assert.ok(csp.includes(`script-src 'self' ${hashesImportmap(HTML_VISOR)[0]};`), csp);
  assert.ok(!/script-src[^;]*jsdelivr/.test(csp));
  ses.handlers.headers({ url: `${VISOR}/app.js`, resourceType: 'script', responseHeaders: {} }, (x) => { res = x; });
  assert.deepEqual(res, {});
  for (const nombre of ['(defecto)', 'amadeus-overlay', 'amadeus-chat']) {
    const s = sim.r.sesiones.get(nombre);
    let concedido = null;
    s.handlers.request(null, 'media', (v) => { concedido = v; });
    assert.equal(concedido, false, nombre);
    assert.equal(s.handlers.check(), false);
  }
});

test('main: si el importmap cambia al navegar, recarga UNA vez con la CSP nueva', async (t) => {
  const { ov, estado, sim } = await escenario(t);
  estado.html = HTML_VISOR.replace('three.module.js', 'three.module.v2.js');
  ov.webContents.simularCarga(URL_OVERLAY);
  await asentar();
  assert.equal(ov.webContents.recargas, 1);
  let res;
  sim.r.sesiones.get('amadeus-overlay').handlers.headers(
    { url: `${VISOR}/`, resourceType: 'mainFrame', responseHeaders: {} }, (x) => { res = x; });
  assert.ok(res.responseHeaders['Content-Security-Policy'][0].includes(hashesImportmap(estado.html)[0]));
  ov.webContents.simularCarga(URL_OVERLAY); // la recarga: mismo hash → nada
  await asentar();
  assert.equal(ov.webContents.recargas, 1);
});

test('main: sin VTuber AI Studio no carga el overlay y reintenta cada 5 s', async (t) => {
  const { ov, pedidos, estado, avanzar } = await escenario(t, { html: null });
  assert.equal(pedidos.length, 1);
  assert.equal(ultimo(ov.llamadas, 'loadURL'), undefined);
  await avanzar(4999);
  assert.equal(pedidos.length, 1);
  estado.html = HTML_VISOR;
  await avanzar(1);
  assert.equal(pedidos.length, 2);
  assert.deepEqual(ultimo(ov.llamadas, 'loadURL'), ['loadURL', URL_OVERLAY]);
});

test('main: WebSocket a /escritorio con el token; hola_app primero y visibilidad actual', async (t) => {
  const { instancias, ws, carga } = await escenario(t);
  assert.equal(instancias.length, 1);
  assert.equal(ws().url, `ws://127.0.0.1:8765/escritorio?t=${TOKEN}`);
  ws().abrir();
  assert.deepEqual(ws().enviados.slice(0, 2), [
    { tipo: 'hola_app', pid: process.pid, version: '0.2.0' },
    { tipo: 'evento', nombre: 'ocultada', datos: {} },
  ]);
  assert.ok(!carga.logs.join('\n').includes(TOKEN));
});

test('main: reconexión con espera creciente y relectura del token', async (t) => {
  const { instancias, ws, avanzar, escribirToken } = await escenario(t);
  ws().cerrar(1006);
  escribirToken('NuevoTokenDeRams_123456'); // R.A.M.S. se reinició
  await avanzar(999);
  assert.equal(await esperar(() => instancias.length > 1, 200), false);
  await avanzar(1);
  assert.ok(await esperar(() => instancias.length === 2));
  assert.equal(ws().url, 'ws://127.0.0.1:8765/escritorio?t=NuevoTokenDeRams_123456');
  ws().cerrar(1006);
  await avanzar(1999);
  assert.equal(await esperar(() => instancias.length > 2, 200), false);
  await avanzar(1);
  assert.ok(await esperar(() => instancias.length === 3));
});

test('main: orden aparecer → showInactive + visibility + order + evento mostrada; cursor global al visor', async (t) => {
  const { ov, ws, todoListo, avanzar, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'orden', datos: { accion: 'aparecer', borde: 'left', modo: 'asomarse' } });
  assert.ok(ov.nombres().includes('showInactive'));
  assert.ok(!ov.nombres().includes('focus'));
  assert.equal(ov.webContents.throttling, false);
  assert.deepEqual(ov.webContents.canal('overlay:visibility').at(-1), true);
  assert.deepEqual(ov.webContents.canal('overlay:order').at(-1), { accion: 'aparecer', borde: 'left', modo: 'asomarse' });
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'evento', nombre: 'mostrada', datos: {} });
  await avanzar(0);
  assert.deepEqual(ov.webContents.canal('overlay:cursor').at(-1), { x: 1700, y: 900 });
  sim.r.cursor = { x: 10, y: 20 };
  await avanzar(33); // ~30 Hz
  assert.deepEqual(ov.webContents.canal('overlay:cursor').at(-1), { x: 10, y: 20 });
});

test('main: autoridad del click-through — origen validado, caja, cursor y caída del renderer', async (t) => {
  const { ov, ws, todoListo, sim, avanzar } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'orden', datos: { accion: 'aparecer' } });
  await avanzar(0);
  const ajeno = { sender: ov.webContents, senderFrame: { parent: null, url: 'http://evil.example/', origin: 'http://evil.example' } };
  const caja = { x: 1500, y: 400, w: 340, h: 600 };
  // desde un origen ajeno: nada
  sim.ipc('overlay:rect', ajeno, caja);
  sim.ipc('overlay:interactive', ajeno, true);
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', true, { forward: true }]);
  // desde el visor: caja aceptada y cursor (1700, 900) dentro → interactivo
  sim.ipc('overlay:rect', ov.webContents.evento(), caja);
  sim.ipc('overlay:interactive', ov.webContents.evento(), true);
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', false]);
  // la caja va a R.A.M.S. en píxeles físicos (escala 1,5)
  assert.deepEqual(ws().enviados.filter((m) => m.tipo === 'caja').at(-1), { tipo: 'caja', x: 2250, y: 600, w: 510, h: 900 });
  // una caja enorme no se acepta y no cambia nada
  sim.ipc('overlay:rect', ov.webContents.evento(), { x: 0, y: 0, w: 1920, h: 1040 });
  assert.equal(ws().enviados.filter((m) => m.tipo === 'caja').length, 1);
  // watchdog: el cursor sale y pasan > 1,5 s
  sim.r.cursor = { x: 10, y: 10 };
  await avanzar(33);
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', false]);
  for (let i = 0; i < 50; i++) await avanzar(33);
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', true, { forward: true }]);
  // vuelve a ser interactivo y el renderer se cae → click-through y fuera de pantalla
  sim.r.cursor = { x: 1700, y: 900 };
  await avanzar(33);
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', false]);
  ov.webContents.emit('render-process-gone', {}, { reason: 'crashed' });
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', true, { forward: true }]);
  assert.equal(ov.visible, false);
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'evento', nombre: 'ocultada', datos: {} });
});

test('main: ventana sin respuesta → click-through forzado', async (t) => {
  const { ov, todoListo, ws, sim, avanzar } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'orden', datos: { accion: 'aparecer' } });
  await avanzar(0);
  sim.ipc('overlay:rect', ov.webContents.evento(), { x: 1500, y: 400, w: 340, h: 600 });
  sim.ipc('overlay:interactive', ov.webContents.evento(), true);
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', false]);
  ov.emit('unresponsive');
  assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', true, { forward: true }]);
});

test('main: esconderse → order; overlay:hidden → hide + evento ocultada', async (t) => {
  const { ov, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'orden', datos: { accion: 'aparecer' } });
  ws().recibir({ tipo: 'orden', datos: { accion: 'esconderse' } });
  assert.deepEqual(ov.webContents.canal('overlay:order').at(-1), { accion: 'esconderse' });
  assert.equal(ov.visible, true);
  sim.ipc('overlay:hidden', ov.webContents.evento());
  assert.equal(ov.visible, false);
  assert.equal(ov.webContents.throttling, true);
  assert.deepEqual(ov.webContents.canal('overlay:visibility').at(-1), false);
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'evento', nombre: 'ocultada', datos: {} });
});

test('main: orden abrir_chat → showInactive sin foco; clic derecho del usuario sí enfoca', async (t) => {
  const { ch, ov, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'orden', datos: { accion: 'abrir_chat' } });
  assert.deepEqual(ch.nombres().filter((n) => ['show', 'showInactive', 'focus'].includes(n)), ['showInactive']);
  assert.ok(!ws().enviados.some((m) => m.nombre === 'chat_abierto'));
  sim.ipc('overlay:chat', ov.webContents.evento());
  assert.deepEqual(ch.nombres().filter((n) => ['show', 'focus'].includes(n)), ['show', 'focus']);
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'evento', nombre: 'chat_abierto', datos: {} });
});

test('main: IPC del chat validado (envío, confirmación, enlaces ofrecidos, ocultar)', async (t) => {
  const { ch, ov, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  assert.deepEqual(ch.webContents.canal('chat:status').at(-1), 'en_linea');
  // envío
  assert.equal(await sim.invocar('chat:send', ch.webContents.evento(), '  hola Kurisu '), true);
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'chat', texto: 'hola Kurisu' });
  assert.equal(await sim.invocar('chat:send', ov.webContents.evento(), 'intruso'), false);
  // confirmación por botón (invoke: devuelve si salió por el WebSocket)
  const n = ws().enviados.length;
  assert.equal(await sim.invocar('chat:confirm', ch.webContents.evento(), 'deadbeef', true), true);
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'confirmacion', id: 'deadbeef', aprobado: true });
  assert.equal(await sim.invocar('chat:confirm', ch.webContents.evento(), 'DEADBEEF', true), false);
  assert.equal(await sim.invocar('chat:confirm', ch.webContents.evento(), 'deadbeef', 'true'), false);
  assert.equal(await sim.invocar('chat:confirm', ov.webContents.evento(), 'deadbeef', false), false);
  assert.equal(ws().enviados.length, n + 1);
  // controles: lista cerrada
  sim.ipc('chat:control', ch.webContents.evento(), 'pausar_vision');
  assert.equal(ws().enviados.length, n + 1);
  sim.ipc('chat:control', ch.webContents.evento(), 'no_molestar_on');
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'control', accion: 'no_molestar_on' });
  // enlaces: solo los que R.A.M.S. ofreció, y solo tras clic
  const url = 'https://es.wikipedia.org/wiki/Neurona';
  sim.ipc('chat:open-url', ch.webContents.evento(), url);
  assert.deepEqual(sim.r.abiertas, []);
  ws().recibir({ tipo: 'orden', datos: { accion: 'notificar', texto: 'Mira esto', url } });
  assert.equal(sim.r.notificaciones.length, 1);
  assert.deepEqual(ch.webContents.canal('chat:msg').at(-1), { de: 'amadeus', texto: 'Mira esto', url });
  assert.deepEqual(sim.r.abiertas, []); // nada se abre solo
  sim.ipc('chat:open-url', ch.webContents.evento(), url);
  assert.deepEqual(sim.r.abiertas, [url]);
  sim.r.notificaciones[0].emit('click');
  assert.deepEqual(sim.r.abiertas, [url, url]);
  // ocultar
  ch.visible = true;
  sim.ipc('chat:hide', ch.webContents.evento());
  assert.equal(ch.visible, false);
});

test('main: los mensajes al chat se guardan hasta que carga', async (t) => {
  const { ch, ws } = await escenario(t);
  ws().abrir();
  ws().recibir({ tipo: 'subtitulo', datos: { texto: 'Llegas tarde.', exp: 'tsun', canal: 'terminal', origen: 'broca' } });
  assert.deepEqual(ch.webContents.canal('chat:msg'), []);
  ch.webContents.simularCarga(URL_CHAT);
  assert.deepEqual(ch.webContents.canal('chat:msg'), [{ de: 'amadeus', texto: 'Llegas tarde.' }]);
});

test('main: navegación fuera del visor bloqueada y ventanas nuevas denegadas', async (t) => {
  const { ov, ch } = await escenario(t);
  const navegar = (wc, url) => {
    let bloqueada = false;
    wc.emit('will-navigate', { url, preventDefault: () => { bloqueada = true; } }, url);
    return bloqueada;
  };
  assert.equal(navegar(ov.webContents, 'https://evil.example/'), true);
  assert.equal(navegar(ov.webContents, `${VISOR}/?obs=1&desktop=1`), false);
  assert.equal(navegar(ch.webContents, 'https://example.com/'), true);
  assert.deepEqual(ov.webContents.openHandler({ url: 'https://example.com' }), { action: 'deny' });
  assert.deepEqual(ch.webContents.openHandler({ url: 'https://example.com' }), { action: 'deny' });
});

test('main: 2.ª instancia (additionalData) y atajos de emergencia', async (t) => {
  const { sim, ov, ws, todoListo } = await escenario(t);
  await todoListo();
  sim.electron.app.emit('second-instance', {}, ['electron', '.', '--amadeus=quit'], '/', { v: 1, amadeus: 'show', oculto: false, invalido: false });
  assert.ok(ov.visible);
  assert.equal(sim.r.app.quit, 0); // manda additionalData, no argv
  if (!WAYLAND) {
    const atajo = (acel) => sim.r.atajos.find((a) => a[0] === acel)[1];
    assert.equal(sim.r.atajos.some((a) => a[0] === 'Control+Alt+Q'), false); // AltGr+Q = «@»
    atajo('Control+Alt+Shift+Q')();
    assert.deepEqual(ws().enviados.filter((m) => m.tipo === 'control').at(-1), { tipo: 'control', accion: 'basta_travesuras' });
    assert.deepEqual(ov.webContents.canal('overlay:order').at(-1), { accion: 'esconderse' });
    atajo('Control+Alt+Shift+A')();
    assert.equal(ov.visible, false);
    assert.deepEqual(ultimo(ov.llamadas, 'setIgnoreMouseEvents'), ['setIgnoreMouseEvents', true, { forward: true }]);
  }
  sim.electron.app.emit('second-instance', {}, [], '/', { v: 1, amadeus: 'quit', oculto: false, invalido: false });
  assert.equal(sim.r.app.quit, 1);
});

test('main: sin --hidden aparece en cuanto carga el visor', async (t) => {
  const { ov, ch } = await escenario(t, { argv: [] });
  assert.ok(!ov.nombres().includes('showInactive'));
  ch.webContents.simularCarga(URL_CHAT);
  assert.equal(ch.webContents.canal('chat:msg')[0].de, 'sistema'); // "aún no está listo…"
  ov.webContents.simularCarga(URL_OVERLAY);
  assert.ok(ov.nombres().includes('showInactive'));
  assert.equal(ov.webContents.canal('overlay:order').at(-1).accion, 'aparecer');
});

test('main: estado y travesuras de R.A.M.S. llegan al overlay y a la bandeja', async (t) => {
  const { ov, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'estado', datos: { aburrimiento: 0.7, fatiga: 0.2, valencia: 0.1 } });
  assert.deepEqual(ov.webContents.canal('overlay:order').at(-1), { accion: 'estado', aburrimiento: 0.7, fatiga: 0.2, valencia: 0.1 });
  ws().recibir({ tipo: 'travesuras', datos: { activas: false, hasta_ts: null } });
  const quieta = sim.r.bandejas[0].menu.find((i) => i.label === 'Quieta');
  assert.equal(quieta.checked, true);
  assert.equal(sim.r.bandejas[0].menu.find((i) => i.label && i.label.startsWith('R.A.M.S.')).label, 'R.A.M.S.: en línea');
});

test('main: chat:confirm con el WebSocket caído devuelve false (el chat reactiva los botones)', async (t) => {
  const { ch, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'permiso', datos: { id: 'deadbeef', descripcion: 'Abrir WhatsApp', destructiva: false, pendiente: true } });
  ws().cerrar(1006);
  assert.deepEqual(ch.webContents.canal('chat:status').at(-1), 'desconectada');
  assert.equal(await sim.invocar('chat:confirm', ch.webContents.evento(), 'deadbeef', true), false);
});

test('main: VTUBER_AI_URL remota se ignora — GET, loadURL e IPC solo del origen local', async (t) => {
  const { ov, pedidos, carga, sim, ws, todoListo } = await escenario(t, { env: { VTUBER_AI_URL: 'https://visor.ejemplo-remoto.com' } });
  assert.deepEqual(pedidos, [`${VISOR}/`]);
  assert.deepEqual(ultimo(ov.llamadas, 'loadURL'), ['loadURL', URL_OVERLAY]);
  assert.ok(carga.logs.some((l) => l.includes('VTUBER_AI_URL debe ser un origen local')));
  assert.ok(!carga.logs.join('\n').includes('ejemplo-remoto'));
  await todoListo();
  ws().recibir({ tipo: 'orden', datos: { accion: 'aparecer' } });
  const remoto = { sender: ov.webContents, senderFrame: { parent: null, url: 'https://visor.ejemplo-remoto.com/', origin: 'https://visor.ejemplo-remoto.com' } };
  const n = ws().enviados.length;
  sim.ipc('overlay:event', remoto, 'toque', {});
  sim.ipc('overlay:rect', remoto, { x: 1500, y: 400, w: 340, h: 600 });
  assert.equal(ws().enviados.length, n);
  let bloqueada = false;
  ov.webContents.emit('will-navigate', { url: 'https://visor.ejemplo-remoto.com/', preventDefault: () => { bloqueada = true; } });
  assert.equal(bloqueada, true);
});

test('main: AMADEUS_ATAJO_* cambia o quita atajos (Win/X11)', { skip: WAYLAND }, async (t) => {
  const { sim, carga } = await escenario(t, { env: { AMADEUS_ATAJO_BASTA: 'Ctrl+Alt+Shift+F9', AMADEUS_ATAJO_VISION: 'off' } });
  assert.deepEqual(sim.r.atajos.map((a) => a[0]),
    ['Control+Alt+A', 'Control+Alt+Space', 'Control+Alt+Shift+F9', 'Control+Alt+Shift+A']);
  assert.ok(!carga.logs.some((l) => l.includes('AltGr')));
});

test('main: la casilla No molestar sigue a R.A.M.S. y la reconexión no la reafirma', async (t) => {
  const { sim, ws, todoListo, avanzar, instancias } = await escenario(t);
  await todoListo();
  const casilla = () => sim.r.bandejas[0].menu.find((i) => i.label === 'No molestar');
  casilla().click({ checked: true });
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'control', accion: 'no_molestar_on' });
  assert.equal(casilla().checked, true);
  ws().recibir({ tipo: 'no_molestar', datos: { activo: false, motivo: '', usuario: false } }); // off desde el HUD
  assert.equal(casilla().checked, false);
  ws().cerrar(1006);
  await avanzar(1000);
  assert.ok(await esperar(() => instancias.length === 2));
  ws().abrir();
  assert.ok(!ws().enviados.some((m) => m.tipo === 'control'));
});

test('main: con la cámara de R.A.M.S. abierta la bandeja lo muestra (icono, menú y texto)', async (t) => {
  const { sim, ws, todoListo, ov } = await escenario(t);
  await todoListo();
  const bandeja = sim.r.bandejas[0];
  assert.ok(bandeja.img.ruta.endsWith('tray.png'));
  ws().recibir({ tipo: 'vision', datos: { camara: true } });
  assert.ok(bandeja.img.ruta.endsWith('tray-camara.png'));
  assert.equal(bandeja.menu[0].label, '● Cámara activa');
  assert.equal(bandeja.menu[0].enabled, false);
  assert.ok(bandeja.tooltip.startsWith('● Cámara activa'));
  assert.deepEqual(ov.webContents.canal('overlay:order').at(-1), { accion: 'camara', activa: true });
  ws().recibir({ tipo: 'vision', datos: { camara: false } });
  assert.ok(bandeja.img.ruta.endsWith('tray.png'));
  assert.ok(!bandeja.menu.some((i) => i.label === '● Cámara activa'));
  assert.ok(!bandeja.tooltip.includes('Cámara'));
});
