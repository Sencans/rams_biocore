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
      AMADEUS_PUTER: '1',
      AMADEUS_PUTER_MODELO: '',
      AMADEUS_PUTER_ESFUERZO: '',
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

// ---- tarjetas aprobables (v0.2.2, docs/V022_CORREO_DRIVE_SCRIPTS.md §13.3) ----

const HUELLA = 'ab'.repeat(32);
const CODIGO = 'Get-ChildItem $env:USERPROFILE\n# fin\n';
const tarjetaScript = (id = '5c1a7e00') => ({
  id, clase: 'script', version: 1, sha256: HUELLA, titulo: 'Script', resumen: '', aviso: '',
  botones: ['aprobar', 'guardar', 'copiar', 'descartar'], caduca_ts: null, canal_origen: 'escritorio', estado: 'pendiente',
  correo: null, drive: null, salida: null,
  script: { lenguaje: 'powershell', plataforma: 'windows', codigo: CODIGO, explicacion: '', efectos: [], requisitos: [],
    nivel: 'bajo', ejecutable: true, motivo_no_ejecutable: '', metodo: 'ast', hallazgos: [], hallazgos_omitidos: 0,
    habilidad: null, habilidad_cambiada: false, ejecuciones: 0 },
});
const tarjetaCorreo = (id = '0a1b2c3d') => ({
  ...tarjetaScript(id), clase: 'correo', botones: ['aprobar', 'editar', 'descartar'], script: null,
  correo: { para: ['ana@example.com'], cc: [], cco: [], asunto: 'Hola', cuerpo: 'Cuerpo', adjuntos: [], problemas: [],
    avisos: [], listo: true },
});

test('main/tarjetas: chat:tarjeta solo desde el chat; la decisión sale con su tope de 60 KiB', async (t) => {
  const { ch, ov, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'tarjeta', datos: tarjetaCorreo() });
  assert.equal(ch.webContents.canal('chat:tarjeta').at(-1).id, '0a1b2c3d');
  // pendiente nueva → el chat se abre SIN foco
  assert.deepEqual(ch.nombres().filter((n) => ['show', 'showInactive', 'focus'].includes(n)), ['showInactive']);
  const n = ws().enviados.length;
  const v1 = { version: 1 };
  assert.equal(await sim.invocar('chat:tarjeta', ov.webContents.evento(), '0a1b2c3d', HUELLA, 'aprobar', v1), false);
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento({ parent: null, url: 'https://evil.example/' }),
    '0a1b2c3d', HUELLA, 'aprobar', v1), false);
  assert.equal(ws().enviados.length, n);
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento(), '0a1b2c3d', HUELLA, 'aprobar', v1), true);
  assert.deepEqual(ws().enviados.at(-1), { tipo: 'tarjeta_decision', id: '0a1b2c3d', sha256: HUELLA, decision: 'aprobar',
    version: 1 });
  // Un «editar» de ~57 KB (> 8 KiB) sale entero: el cableado respeta maxBytes hasta 61 440.
  const campos = { cuerpo: '€'.repeat(10000), para: '€'.repeat(2600), cc: '€'.repeat(2600), cco: '€'.repeat(2600),
    asunto: '€'.repeat(200), adjuntos: '€'.repeat(1000) };
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento(), '0a1b2c3d', HUELLA, 'editar', { campos, version: 1 }),
    true);
  const enviado = ws().enviados.at(-1);
  assert.deepEqual(enviado.campos, campos);
  const bytes = Buffer.byteLength(JSON.stringify(enviado), 'utf8');
  assert.ok(bytes > 8192 && bytes <= 61440, `${bytes}`);
  // Inválidas (campo por encima del tope, decisión desconocida, sin la versión que se ve): false y nada sale.
  const m = ws().enviados.length;
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento(), '0a1b2c3d', HUELLA, 'editar',
    { campos: { cuerpo: 'x'.repeat(10001) }, version: 1 }), false);
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento(), '0a1b2c3d', HUELLA, 'copiar', v1), false);
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento(), '0a1b2c3d', HUELLA, 'aprobar', {}), false);
  assert.equal(ws().enviados.length, m);
  // El resto de mensajes sigue con 8 KiB (p. ej. el chat, que ya va acotado a 2000 caracteres).
  assert.equal(await sim.invocar('chat:send', ch.webContents.evento(), 'hola'), true);
  // Sin WebSocket: false (el chat reactiva los botones).
  ws().cerrar();
  await asentar();
  assert.equal(await sim.invocar('chat:tarjeta', ch.webContents.evento(), '0a1b2c3d', HUELLA, 'descartar', {}), false);
});

test('main/tarjetas: chat:copiar solo desde el chat y solo el código de una tarjeta de script vigente', async (t) => {
  const { ch, ov, ws, todoListo, sim } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'tarjeta', datos: tarjetaCorreo() });
  ws().recibir({ tipo: 'tarjeta', datos: tarjetaScript() });
  sim.ipc('chat:copiar', ov.webContents.evento(), '5c1a7e00');             // otro emisor
  sim.ipc('chat:copiar', ch.webContents.evento(), '0a1b2c3d');             // un correo no se copia
  sim.ipc('chat:copiar', ch.webContents.evento(), 'deadbeef');             // desconocida
  sim.ipc('chat:copiar', ch.webContents.evento(), 'Texto arbitrario');     // nunca texto del renderer
  assert.deepEqual(sim.r.portapapeles, []);
  sim.ipc('chat:copiar', ch.webContents.evento(), '5c1a7e00');
  assert.deepEqual(sim.r.portapapeles, [CODIGO]);
  // Cerrada → ya no se copia.
  ws().recibir({ tipo: 'tarjeta', datos: { id: '5c1a7e00', clase: 'script', estado: 'cerrada', resultado: 'descartado', motivo: '' } });
  sim.ipc('chat:copiar', ch.webContents.evento(), '5c1a7e00');
  assert.equal(sim.r.portapapeles.length, 1);
});

test('main/tarjetas: las vigentes se reenvían al recargar el chat; una tarjeta de más de 64 KiB no entra', async (t) => {
  const { ch, ws, todoListo } = await escenario(t);
  await todoListo();
  ws().recibir({ tipo: 'tarjeta', datos: tarjetaScript() });
  const antes = ch.webContents.canal('chat:tarjeta').length;
  ch.webContents.simularCarga(URL_CHAT);
  assert.equal(ch.webContents.canal('chat:tarjeta').length, antes + 1);
  const grande = tarjetaScript('00000002');
  grande.titulo = 'x'.repeat(70000); // > MAX_WS_ENTRANTE (R.A.M.S. nunca lo manda: ≤ 60 KiB)
  ws().recibir({ tipo: 'tarjeta', datos: grande });
  assert.equal(ch.webContents.canal('chat:tarjeta').length, antes + 1);
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

// ---------------------------------------------------------------------------------------------
// Respaldo Puter (§C2.15): ventana oculta, relevo WS ↔ IPC, bandeja y ventanita de conexión
// ---------------------------------------------------------------------------------------------

const MODELO_PUTER = 'google/gemini-3.5-flash-lite';
const URL_TRABAJO = 'http://amadeus-puter.localhost/trabajo.html';
const URL_CONECTAR = 'http://amadeus-puter.localhost/conectar.html';
const ID_A = '0123456789abcdef01234567';
const ID_B = 'fedcba9876543210fedcba98';
const pedirLlm = (id, extra = {}) => ({
  tipo: 'llm.pedir',
  datos: { id, modelo: '', system: 'Eres Amadeus.', mensajes: [{ rol: 'usuario', texto: 'Hola' }], max_tokens: 256, temperatura: 0.9, ...extra },
});
const deTipo = (ws, tipo) => ws.enviados.filter((m) => m.tipo === tipo).map((m) => m.datos);
const navegarA = (wc, url) => {
  let bloqueada = false;
  wc.emit('will-navigate', { url, preventDefault: () => { bloqueada = true; } }, url);
  return bloqueada;
};

/** Ventana oculta cargada y con sesión de Puter (como la informaría trabajo.js). */
function puterListo(sim) {
  const pw = sim.puter();
  pw.webContents.simularCarga(URL_TRABAJO);
  sim.ipc('puter:estado', pw.webContents.evento(), { cargado: true, sesion: true, error: null });
  return pw;
}

test('main/puter: ventana OCULTA en persist:puter con sandbox, protocolo propio con CSP y 404', async (t) => {
  const { sim } = await escenario(t);
  const pw = sim.puter();
  assert.ok(pw, 'no se creó la ventana oculta de Puter');
  assert.equal(pw.opciones.show, false);
  assert.equal(pw.opciones.skipTaskbar, true);
  assert.equal(pw.opciones.focusable, false);
  const wp = pw.opciones.webPreferences;
  assert.equal(wp.partition, 'persist:puter');
  assert.equal(wp.contextIsolation, true);
  assert.equal(wp.sandbox, true);
  assert.equal(wp.nodeIntegration, false);
  assert.equal(wp.webviewTag, false);
  assert.equal(wp.backgroundThrottling, false);
  assert.ok(wp.preload.endsWith('preload-puter.js'));
  assert.deepEqual(ultimo(pw.llamadas, 'loadURL'), ['loadURL', URL_TRABAJO]);
  assert.ok(!pw.nombres().includes('show') && !pw.nombres().includes('showInactive'));
  // sesión endurecida (sin permisos) y `http:` interceptado SOLO en la partición de Puter
  const ses = sim.r.sesiones.get('persist:puter');
  let concedido = null;
  ses.handlers.request(null, 'media', (v) => { concedido = v; });
  assert.equal(concedido, false);
  assert.equal(ses.handlers.check(), false);
  for (const otra of ['(defecto)', 'amadeus-overlay', 'amadeus-chat']) assert.equal(sim.r.sesiones.get(otra).protocolos.size, 0);
  const servir = ses.protocolos.get('http');
  const pagina = await servir({ url: URL_TRABAJO, method: 'GET' });
  assert.equal(pagina.status, 200);
  assert.equal(pagina.headers.get('content-type'), 'text/html; charset=utf-8');
  assert.match(pagina.headers.get('content-security-policy'), /default-src 'none'; script-src 'self' https:\/\/js\.puter\.com;/);
  assert.equal(await pagina.text(), fs.readFileSync(path.join(__dirname, '..', 'puter', 'trabajo.html'), 'utf8'));
  const cabeza = await servir({ url: 'http://amadeus-puter.localhost/conectar.js', method: 'HEAD' });
  assert.equal(cabeza.status, 200);
  assert.equal(await cabeza.text(), '');
  for (const [url, metodo] of [['http://amadeus-puter.localhost/main.js', 'GET'], [URL_TRABAJO, 'POST'],
    ['http://otro.localhost/trabajo.html', 'GET'], ['http://amadeus-puter.localhost/../preload-puter.js', 'GET']]) {
    assert.equal((await servir({ url, method: metodo })).status, 404, `${metodo} ${url}`);
  }
  // nunca navega fuera de trabajo.html ni abre ventanas
  assert.equal(navegarA(pw.webContents, 'https://puter.com/'), true);
  assert.deepEqual(pw.webContents.openHandler({ url: 'https://puter.com/action/sign-in' }), { action: 'deny' });
});

test('main/puter: relevo — puter.estado tras el hola, llm.pedir → IPC → llm.respuesta, emisor validado', async (t) => {
  const { sim, ov, ch, ws, todoListo, avanzar, carga } = await escenario(t);
  await todoListo();
  const pw = sim.puter();
  // tras el hola: estado sin sesión (la ventana aún no informó)
  assert.deepEqual(deTipo(ws(), 'puter.estado'), [{ listo: false, sesion: false, modelo: MODELO_PUTER, error: null }]);
  assert.ok(ws().enviados.findIndex((m) => m.tipo === 'puter.estado') > ws().enviados.findIndex((m) => m.tipo === 'hola_app'));
  pw.webContents.simularCarga(URL_TRABAJO);
  assert.deepEqual(pw.webContents.canal('puter:info'), [{ modelo: MODELO_PUTER }]);
  // estado: solo de la ventana oculta, de su frame principal y con trabajo.html
  const listoDatos = { cargado: true, sesion: true, error: null };
  sim.ipc('puter:estado', ov.webContents.evento(), listoDatos);
  sim.ipc('puter:estado', ch.webContents.evento(), listoDatos);
  sim.ipc('puter:estado', pw.webContents.evento({ parent: {}, url: URL_TRABAJO }), listoDatos);
  sim.ipc('puter:estado', pw.webContents.evento({ parent: null, url: 'https://puter.com/' }), listoDatos);
  assert.equal(deTipo(ws(), 'puter.estado').length, 1);
  sim.ipc('puter:estado', pw.webContents.evento(), listoDatos);
  assert.deepEqual(deTipo(ws(), 'puter.estado').at(-1), { listo: true, sesion: true, modelo: MODELO_PUTER, error: null });
  assert.ok(sim.r.bandejas[0].menu.some((i) => i.label === `Puter: conectado (${MODELO_PUTER})`));
  // pedido → ventana
  const chatAntes = ch.webContents.enviados.length;
  ws().recibir(pedirLlm(ID_A));
  assert.deepEqual(pw.webContents.canal('puter:pedir'), [{
    id: ID_A, modelo: MODELO_PUTER,
    mensajes: [{ role: 'system', content: 'Eres Amadeus.' }, { role: 'user', content: 'Hola' }],
    maxTokens: 256, temperatura: 0.9, esfuerzo: null, timeoutMs: 38000,
  }]);
  assert.equal(ch.webContents.enviados.length, chatAntes); // no pasa por el chat
  await avanzar(1500);
  // respuesta: solo de la ventana oculta
  sim.ipc('puter:respuesta', ov.webContents.evento(), { id: ID_A, ok: true, texto: 'intruso' });
  assert.deepEqual(deTipo(ws(), 'llm.respuesta'), []);
  sim.ipc('puter:respuesta', pw.webContents.evento(), { id: ID_A, ok: true, texto: 'Hmph. Hola.', modelo: MODELO_PUTER });
  assert.deepEqual(deTipo(ws(), 'llm.respuesta'), [{ id: ID_A, ok: true, texto: 'Hmph. Hola.', error: null, modelo: MODELO_PUTER, ms: 1500 }]);
  // sin respuesta de la ventana → «timeout» a los 42 s (antes de los 45 s del contrato) y cancelación
  ws().recibir(pedirLlm(ID_B));
  await avanzar(41999);
  assert.equal(deTipo(ws(), 'llm.respuesta').length, 1);
  await avanzar(1);
  assert.deepEqual(deTipo(ws(), 'llm.respuesta').at(-1), { id: ID_B, ok: false, texto: null, error: 'timeout', modelo: MODELO_PUTER, ms: 42000 });
  assert.deepEqual(pw.webContents.canal('puter:cancelar'), [ID_B]);
  // ni el token de R.A.M.S. ni nada del prompt acaba en los logs o en la ventana de Puter
  assert.ok(!JSON.stringify(pw.webContents.enviados).includes(TOKEN));
  assert.ok(!carga.logs.join('\n').includes('Eres Amadeus') && !carga.logs.join('\n').includes('Hmph'));
});

test('main/puter: llm.pedir grande (> 64 KiB) se acepta; respuesta grande sale ≤ 64 KiB; otros grandes no', async (t) => {
  const { sim, ch, ws, todoListo } = await escenario(t);
  await todoListo();
  const pw = puterListo(sim);
  const mensajes = Array.from({ length: 6 }, (_v, i) => ({ rol: i % 2 ? 'modelo' : 'usuario', texto: 'ñ'.repeat(19000) }));
  ws().recibir(pedirLlm(ID_A, { system: 's'.repeat(59000), mensajes }));
  const p = pw.webContents.canal('puter:pedir').at(-1);
  assert.equal(p.id, ID_A);
  assert.equal(p.mensajes.length, 7);
  sim.ipc('puter:respuesta', pw.webContents.evento(), { id: ID_A, ok: true, texto: 'ñ'.repeat(100000) });
  const r = ws().enviados.at(-1);
  assert.equal(r.tipo, 'llm.respuesta');
  assert.equal(r.datos.ok, true);
  assert.ok(Buffer.byteLength(JSON.stringify(r), 'utf8') <= 64 * 1024);
  const n = ch.webContents.canal('chat:msg').length;
  ws().recibir({ tipo: 'subtitulo', datos: { texto: 'a'.repeat(70000) } }); // > 64 KiB y no es llm.pedir
  assert.equal(ch.webContents.canal('chat:msg').length, n);
});

test('main/puter: sin sesión, llm.pedir → «sin_sesion» al instante sin abrir ni mostrar nada', async (t) => {
  const { sim, ws, todoListo } = await escenario(t);
  await todoListo();
  const pw = sim.puter();
  pw.webContents.simularCarga(URL_TRABAJO);
  sim.ipc('puter:estado', pw.webContents.evento(), { cargado: false, sesion: false, error: null });
  const ventanas = sim.r.ventanas.length;
  ws().recibir(pedirLlm(ID_A));
  assert.deepEqual(deTipo(ws(), 'llm.respuesta'), [{ id: ID_A, ok: false, texto: null, error: 'sin_sesion', modelo: MODELO_PUTER, ms: 0 }]);
  assert.deepEqual(pw.webContents.canal('puter:pedir'), []);
  assert.equal(sim.r.ventanas.length, ventanas);
  assert.ok(sim.r.ventanas.every((v) => !v.visible));
  assert.equal(sim.conectar(), undefined);
});

test('main/puter: bandeja «Conectar Puter…» → ventanita; window.open solo a puter.com; conexión recarga la oculta', async (t) => {
  const { sim, ws, todoListo } = await escenario(t);
  await todoListo();
  const menu = () => sim.r.bandejas[0].menu;
  const item = (l) => menu().find((i) => i.label === l);
  assert.ok(item('Conectar Puter (IA de respaldo)…'));
  assert.equal(item('Desconectar Puter'), undefined);
  assert.equal(sim.conectar(), undefined); // nada se abre solo
  item('Conectar Puter (IA de respaldo)…').click();
  const cw = sim.conectar();
  assert.equal(cw.opciones.show, false);
  const wp = cw.opciones.webPreferences;
  assert.equal(wp.partition, 'persist:puter');
  assert.equal(wp.sandbox, true);
  assert.equal(wp.contextIsolation, true);
  assert.equal(wp.nodeIntegration, false);
  assert.ok(wp.preload.endsWith('preload-puter.js'));
  assert.deepEqual(ultimo(cw.llamadas, 'loadURL'), ['loadURL', URL_CONECTAR]);
  cw.webContents.simularCarga(URL_CONECTAR); // ready-to-show → se muestra con foco (la pidió el usuario)
  assert.equal(cw.visible, true);
  assert.ok(cw.nombres().includes('focus'));
  assert.deepEqual(cw.webContents.canal('puter:info'), [{ modelo: MODELO_PUTER }]);
  item('Conectar Puter (IA de respaldo)…').click(); // reutiliza la misma
  assert.equal(sim.r.ventanas.filter((v) => v.opciones.title === 'Conectar Puter').length, 1);
  // window.open: solo https://puter.com y subdominios, en ventana sandbox
  const abrir = cw.webContents.openHandler;
  const permitido = abrir({ url: 'https://puter.com/action/sign-in?embedded_in_popup=true&msg_id=1' });
  assert.equal(permitido.action, 'allow');
  const wpp = permitido.overrideBrowserWindowOptions.webPreferences;
  assert.equal(wpp.sandbox, true);
  assert.equal(wpp.contextIsolation, true);
  assert.equal(wpp.nodeIntegration, false);
  for (const url of ['https://evil.example/', 'http://puter.com/', 'https://puter.com.evil.example/', 'about:blank']) {
    assert.deepEqual(abrir({ url }), { action: 'deny' }, url);
  }
  // la emergente: sin más ventanas y sin salir de puter.com
  const popup = new sim.electron.BrowserWindow({ title: 'Puter', width: 520, height: 720 });
  cw.webContents.emit('did-create-window', popup, { url: 'https://puter.com/action/sign-in' });
  assert.deepEqual(popup.webContents.openHandler({ url: 'https://puter.com/otra' }), { action: 'deny' });
  assert.equal(navegarA(popup.webContents, 'https://evil.example/'), true);
  assert.equal(navegarA(popup.webContents, 'https://puter.com/dashboard'), false);
  assert.equal(navegarA(cw.webContents, 'https://puter.com/'), true); // la ventanita no navega
  // puter:conexion: solo de la ventanita; un fallo no recarga nada; la sesión nueva sí
  const pw = sim.puter();
  pw.webContents.simularCarga(URL_TRABAJO);
  const cargas = () => pw.llamadas.filter((l) => l[0] === 'loadURL').length;
  const antes = cargas();
  sim.ipc('puter:conexion', pw.webContents.evento(), { ok: true, nueva: true });
  sim.ipc('puter:conexion', cw.webContents.evento(), { ok: false, error: 'popup_blocked' });
  assert.equal(cargas(), antes);
  sim.ipc('puter:conexion', cw.webContents.evento(), { ok: true, nueva: true });
  assert.equal(cargas(), antes + 1);
  assert.deepEqual(ultimo(pw.llamadas, 'loadURL'), ['loadURL', URL_TRABAJO]);
  // la página recargada informa la sesión → bandeja con «Desconectar Puter»
  pw.webContents.simularCarga(URL_TRABAJO);
  sim.ipc('puter:estado', pw.webContents.evento(), { cargado: true, sesion: true, error: null });
  assert.deepEqual(deTipo(ws(), 'puter.estado').at(-1), { listo: true, sesion: true, modelo: MODELO_PUTER, error: null });
  assert.ok(item('Desconectar Puter'));
  assert.equal(item('Conectar Puter (IA de respaldo)…'), undefined);
  // «Cerrar» de la ventanita (solo desde ella)
  sim.ipc('puter:cerrar', pw.webContents.evento());
  assert.equal(cw.destruida, false);
  sim.ipc('puter:cerrar', cw.webContents.evento());
  assert.equal(cw.destruida, true);
});

test('main/puter: «Desconectar Puter» borra la sesión de la partición, recarga y avisa a R.A.M.S.', async (t) => {
  const { sim, ws, todoListo } = await escenario(t);
  await todoListo();
  const pw = puterListo(sim);
  const item = (l) => sim.r.bandejas[0].menu.find((i) => i.label === l);
  const cargas = pw.llamadas.filter((l) => l[0] === 'loadURL').length;
  item('Desconectar Puter').click();
  await asentar();
  assert.equal(sim.r.sesiones.get('persist:puter').borrados, 1);
  assert.equal(pw.llamadas.filter((l) => l[0] === 'loadURL').length, cargas + 1);
  assert.deepEqual(deTipo(ws(), 'puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_PUTER, error: null });
  assert.ok(item('Conectar Puter (IA de respaldo)…'));
  ws().recibir(pedirLlm(ID_A));
  assert.equal(deTipo(ws(), 'llm.respuesta').at(-1).error, 'sin_sesion');
});

test('main/puter: la ventana oculta se cae → pedidos «otro», estado «ventana_caida» y recarga en 2 s', async (t) => {
  const { sim, ws, todoListo, avanzar } = await escenario(t);
  await todoListo();
  const pw = puterListo(sim);
  ws().recibir(pedirLlm(ID_A));
  const cargas = () => pw.llamadas.filter((l) => l[0] === 'loadURL').length;
  const antes = cargas();
  pw.webContents.emit('render-process-gone', {}, { reason: 'crashed' });
  assert.deepEqual(deTipo(ws(), 'llm.respuesta'), [{ id: ID_A, ok: false, texto: null, error: 'otro', modelo: MODELO_PUTER, ms: 0 }]);
  assert.deepEqual(deTipo(ws(), 'puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_PUTER, error: 'ventana_caida' });
  ws().recibir(pedirLlm(ID_B)); // mientras tanto: sin sesión utilizable
  assert.equal(deTipo(ws(), 'llm.respuesta').at(-1).error, 'sin_sesion');
  await avanzar(1999);
  assert.equal(cargas(), antes);
  await avanzar(1);
  assert.equal(cargas(), antes + 1);
  pw.webContents.simularCarga(URL_TRABAJO);
  assert.equal(pw.webContents.canal('puter:info').length, 2);
  await avanzar(45000); // el temporizador del pedido caído quedó cancelado
  assert.equal(deTipo(ws(), 'llm.respuesta').length, 2);
});

test('main/puter: caídas seguidas → 2 s, 4 s… 60 s aunque la página informe entre medias; 60 s estable → 2 s', async (t) => {
  const { sim, todoListo, avanzar } = await escenario(t);
  await todoListo();
  const pw = puterListo(sim);
  const cargas = () => pw.llamadas.filter((l) => l[0] === 'loadURL').length;
  const esperaHastaRecarga = async () => {
    const antes = cargas();
    let ms = 0;
    while (cargas() === antes && ms < 120000) {
      await avanzar(500);
      ms += 500;
    }
    return ms;
  };
  const esperas = [];
  for (let i = 0; i < 7; i++) {
    pw.webContents.emit('render-process-gone', {}, { reason: 'crashed' });
    esperas.push(await esperaHastaRecarga());
    // la página recargada arranca y manda su primer estado (trabajo.js lo hace antes de cargar
    // puter.js), y vuelve a caerse a los pocos segundos
    pw.webContents.simularCarga(URL_TRABAJO);
    sim.ipc('puter:estado', pw.webContents.evento(), { cargado: false, sesion: false, error: null });
    sim.ipc('puter:estado', pw.webContents.evento(), { cargado: true, sesion: true, error: null });
    await avanzar(5000);
  }
  assert.deepEqual(esperas, [2000, 4000, 8000, 16000, 32000, 60000, 60000]);
  // un fallo de carga también cuenta como caída (sigue en 60 s)
  pw.webContents.emit('did-fail-load', {}, -105, 'ERR_NAME_NOT_RESOLVED', URL_TRABAJO, true);
  assert.equal(await esperaHastaRecarga(), 60000);
  // la página aguanta 60 s cargada → la siguiente caída vuelve a esperar 2 s
  pw.webContents.simularCarga(URL_TRABAJO);
  await avanzar(59500);
  pw.webContents.emit('render-process-gone', {}, { reason: 'oom' });
  assert.equal(await esperaHastaRecarga(), 60000); // a los 59,5 s aún no era estable
  pw.webContents.simularCarga(URL_TRABAJO);
  await avanzar(60000);
  pw.webContents.emit('render-process-gone', {}, { reason: 'oom' });
  assert.equal(await esperaHastaRecarga(), 2000);
  pw.webContents.simularCarga(URL_TRABAJO);
  pw.webContents.emit('render-process-gone', {}, { reason: 'crashed' });
  assert.equal(await esperaHastaRecarga(), 4000);
});

test('main/puter: 401 con la página aún con sesión → «sin_sesion», sigue lista y se pide puter:refrescar', async (t) => {
  const { sim, ws, todoListo } = await escenario(t);
  await todoListo();
  const pw = puterListo(sim);
  const item = (l) => sim.r.bandejas[0].menu.find((i) => i.label === l);
  ws().recibir(pedirLlm(ID_A));
  sim.ipc('puter:respuesta', pw.webContents.evento(), {
    id: ID_A, ok: false, texto: null, modelo: null,
    fallo: { code: 'token_auth_failed', status: 401, message: 'Unauthorized', name: null, abortado: false, sinSesion: false },
  });
  assert.deepEqual(deTipo(ws(), 'llm.respuesta').at(-1), { id: ID_A, ok: false, texto: null, error: 'sin_sesion', modelo: MODELO_PUTER, ms: 0 });
  // R.A.M.S. recibe el estado real justo detrás (la página sigue con sesión) y la bandeja no cambia
  assert.equal(ws().enviados.at(-1).tipo, 'puter.estado');
  assert.deepEqual(ws().enviados.at(-1).datos, { listo: true, sesion: true, modelo: MODELO_PUTER, error: null });
  assert.ok(item('Desconectar Puter'));
  assert.deepEqual(pw.webContents.canal('puter:refrescar'), [null]);
  // la página relee y ya no hay sesión → ahora sí baja, en R.A.M.S. y en la bandeja
  sim.ipc('puter:estado', pw.webContents.evento(), { cargado: true, sesion: false, error: null });
  assert.deepEqual(deTipo(ws(), 'puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_PUTER, error: null });
  assert.ok(item('Conectar Puter (IA de respaldo)…'));
  // con la página sin sesión, otro 401 no vuelve a pedir que relea
  sim.ipc('puter:estado', pw.webContents.evento(), { cargado: true, sesion: true, error: null });
  ws().recibir(pedirLlm(ID_B));
  sim.ipc('puter:respuesta', pw.webContents.evento(), { id: ID_B, ok: false, fallo: { status: 401, sinSesion: true } });
  assert.equal(deTipo(ws(), 'llm.respuesta').at(-1).error, 'sin_sesion');
  assert.equal(pw.webContents.canal('puter:refrescar').length, 1);
});

test('main/puter: AMADEUS_PUTER=0 — sin ventana ni sesión de Puter, estado «deshabilitado» y pedidos rechazados', async (t) => {
  const { sim, ws, todoListo } = await escenario(t, { env: { AMADEUS_PUTER: '0' } });
  assert.equal(sim.puter(), undefined);
  assert.equal(sim.r.sesiones.has('persist:puter'), false);
  await todoListo();
  assert.deepEqual(deTipo(ws(), 'puter.estado'), [{ listo: false, sesion: false, modelo: null, error: 'deshabilitado' }]);
  assert.ok(!sim.r.bandejas[0].menu.some((i) => i.label && i.label.includes('Puter')));
  ws().recibir(pedirLlm(ID_A));
  assert.deepEqual(deTipo(ws(), 'llm.respuesta'), [{ id: ID_A, ok: false, texto: null, error: 'rechazado', modelo: null, ms: 0 }]);
  sim.electron.app.emit('second-instance', {}, [], '/', { v: 1, amadeus: 'puter', oculto: false, invalido: false });
  assert.equal(sim.conectar(), undefined);
});

test('main/puter: --amadeus=puter abre la ventanita; AMADEUS_PUTER_MODELO cambia el modelo por defecto', async (t) => {
  const { sim, ws, todoListo, carga } = await escenario(t, { env: { AMADEUS_PUTER_MODELO: 'anthropic/claude-haiku-5-5' } });
  sim.electron.app.emit('second-instance', {}, [], '/', { v: 1, amadeus: 'puter', oculto: false, invalido: false });
  assert.ok(sim.conectar());
  await todoListo();
  assert.equal(deTipo(ws(), 'puter.estado')[0].modelo, 'anthropic/claude-haiku-5-5');
  assert.ok(!carga.logs.some((l) => l.includes('AMADEUS_PUTER_MODELO')));
});

test('main/puter: AMADEUS_PUTER_MODELO inválido → aviso sin repetir el valor y modelo por defecto', async (t) => {
  const { ws, todoListo, carga } = await escenario(t, { env: { AMADEUS_PUTER_MODELO: 'modelo con espacios y secreto' } });
  await todoListo();
  assert.equal(deTipo(ws(), 'puter.estado')[0].modelo, MODELO_PUTER);
  assert.ok(carga.logs.some((l) => l.includes('AMADEUS_PUTER_MODELO no es un nombre de modelo válido')));
  assert.ok(!carga.logs.join('\n').includes('secreto'));
});
