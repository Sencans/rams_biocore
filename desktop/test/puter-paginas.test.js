'use strict';
/**
 * Páginas de Puter (§C2.15) con DOBLES de puter.js, del DOM y del puente (sin navegador, sin red y
 * sin iniciar sesión en Puter): puter/trabajo.js (ventana oculta), puter/conectar.js (ventanita de
 * inicio de sesión) y preload-puter.js (window.amadeusPuter).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const trabajo = require('../puter/trabajo');
const conectarJs = require('../puter/conectar');

const URL_PUTER_JS = 'https://js.puter.com/v2/';

// ---------------------------------------------------------------------------------------------
// Dobles
// ---------------------------------------------------------------------------------------------

class ElementoFalso {
  constructor(etiqueta) {
    this.etiqueta = etiqueta;
    this.oyentes = {};
    this.textContent = '';
    this.className = '';
    this.disabled = false;
    this.hidden = false;
    this.quitado = false;
  }

  addEventListener(tipo, fn) { (this.oyentes[tipo] ||= []).push(fn); }
  emitir(tipo) { for (const fn of this.oyentes[tipo] || []) fn({ type: tipo }); }
  remove() { this.quitado = true; }
}

function documentoFalso(ids = []) {
  const elementos = Object.fromEntries(ids.map((id) => [id, new ElementoFalso('x')]));
  const scripts = [];
  return {
    scripts,
    elementos,
    head: { appendChild: (s) => scripts.push(s) },
    createElement: (etiqueta) => new ElementoFalso(etiqueta),
    getElementById: (id) => elementos[id] || null,
  };
}

function temporizadorFalso() {
  const pendientes = [];
  return {
    pendientes,
    temporizador: (fn, ms) => {
      const h = { fn, ms, vivo: true };
      pendientes.push(h);
      return () => { h.vivo = false; };
    },
    /** Dispara los temporizadores vivos de `ms` exactos. */
    disparar: (ms) => {
      for (const h of pendientes) {
        if (h.vivo && h.ms === ms) {
          h.vivo = false;
          h.fn();
        }
      }
    },
  };
}

/** puter.js falso: `ai.chat` controlable, sesión y listado de modelos. */
function puterFalso({ sesion = true, modelos = null } = {}) {
  const p = {
    sesion,
    llamadas: [],
    respuesta: null, // función (mensajes, testMode, opciones) → promesa
    auth: {
      isSignedIn: () => p.sesion,
      signIn: () => Promise.resolve({}),
    },
    ai: {
      chat: (...args) => {
        p.llamadas.push(args);
        return p.respuesta ? p.respuesta(...args) : Promise.resolve({ message: { role: 'assistant', content: 'Hola, soy Amadeus.' } });
      },
      listModels: () => Promise.resolve(modelos || []),
    },
  };
  return p;
}

function puenteTrabajo() {
  const b = { estados: [], respuestas: [], oyentes: {} };
  b.puente = {
    version: 1,
    onPedido: (cb) => { b.oyentes.pedir = cb; },
    onCancelar: (cb) => { b.oyentes.cancelar = cb; },
    onInfo: (cb) => { b.oyentes.info = cb; },
    onRefrescar: (cb) => { b.oyentes.refrescar = cb; },
    estado: (d) => b.estados.push(d),
    responder: (r) => b.respuestas.push(r),
  };
  return b;
}

async function asentar(n = 10) {
  for (let i = 0; i < n; i++) await new Promise((res) => setImmediate(res));
}

function montarTrabajo({ marca = true, lib = null } = {}) {
  const doc = documentoFalso();
  const b = puenteTrabajo();
  const reloj = temporizadorFalso();
  const estado = { lib };
  const t = trabajo.crearTrabajador({
    puter: () => estado.lib,
    puente: b.puente,
    documento: doc,
    leerMarca: () => marca,
    temporizador: reloj.temporizador,
  });
  t.arrancar();
  return { t, doc, b, reloj, estado };
}

const PEDIDO = {
  id: '0123456789abcdef01234567',
  modelo: 'google/gemini-3.5-flash-lite',
  mensajes: [{ role: 'system', content: 'Eres Amadeus.' }, { role: 'user', content: 'Hola' }],
  maxTokens: 512,
  temperatura: 0.8,
  esfuerzo: null,
  timeoutMs: 38000,
};

// ---------------------------------------------------------------------------------------------
// trabajo.js (ventana oculta)
// ---------------------------------------------------------------------------------------------

test('trabajo: sin haber conectado Puter no carga puter.js ni llama a nada', async () => {
  const { t, doc, b } = montarTrabajo({ marca: false });
  assert.deepEqual(doc.scripts, []);
  assert.deepEqual(b.estados, [{ cargado: false, sesion: false, error: null }]);
  await t.alPedido(PEDIDO);
  assert.deepEqual(b.respuestas, [{ id: PEDIDO.id, ok: false, fallo: { sinSesion: true } }]);
});

test('trabajo: con la marca carga https://js.puter.com/v2/ e informa cargado + sesión', async () => {
  const { doc, b, estado } = montarTrabajo();
  assert.equal(doc.scripts.length, 1);
  assert.equal(doc.scripts[0].src, URL_PUTER_JS);
  assert.equal(doc.scripts[0].async, true);
  estado.lib = puterFalso();
  doc.scripts[0].emitir('load');
  await asentar();
  assert.deepEqual(b.estados.at(-1), { cargado: true, sesion: true, error: null });
});

test('trabajo: pedido con sesión → puter.ai.chat(mensajes, false, opciones) y respuesta ok', async () => {
  const { t, doc, b, estado } = montarTrabajo();
  const p = puterFalso();
  estado.lib = p;
  doc.scripts[0].emitir('load');
  await asentar();
  await t.alPedido({ ...PEDIDO, esfuerzo: 'low' });
  assert.equal(p.llamadas.length, 1);
  const [mensajes, testMode, opciones] = p.llamadas[0];
  assert.deepEqual(mensajes, PEDIDO.mensajes);
  assert.equal(testMode, false);
  assert.equal(opciones.model, PEDIDO.modelo);
  assert.equal(opciones.max_tokens, 512);
  assert.equal(opciones.temperature, 0.8);
  assert.equal(opciones.stream, false);
  assert.equal(opciones.normalize, true);
  assert.equal(opciones.reasoning_effort, 'low');
  assert.ok(opciones.signal instanceof AbortSignal);
  assert.deepEqual(b.respuestas, [{ id: PEDIDO.id, ok: true, texto: 'Hola, soy Amadeus.', modelo: PEDIDO.modelo }]);
  assert.equal(t.enCurso(), 0);
});

test('trabajo: sesión perdida → «sin sesión» SIN llamar a puter.ai.chat (que abriría un diálogo)', async () => {
  const { t, doc, b, estado } = montarTrabajo();
  const p = puterFalso();
  estado.lib = p;
  doc.scripts[0].emitir('load');
  await asentar();
  p.sesion = false;
  await t.alPedido(PEDIDO);
  assert.equal(p.llamadas.length, 0);
  assert.deepEqual(b.respuestas.at(-1), { id: PEDIDO.id, ok: false, fallo: { sinSesion: true } });
  assert.deepEqual(b.estados.at(-1), { cargado: true, sesion: false, error: null });
});

test('trabajo: error de Puter → fallo descrito (código, estado, mensaje) para que el main lo clasifique', async () => {
  const { t, doc, b, estado } = montarTrabajo();
  const p = puterFalso();
  estado.lib = p;
  doc.scripts[0].emitir('load');
  await asentar();
  p.respuesta = () => Promise.reject({ success: false, error: { code: 'insufficient_funds', message: 'Insufficient funds', status: 402 } });
  await t.alPedido(PEDIDO);
  assert.deepEqual(b.respuestas.at(-1), {
    id: PEDIDO.id, ok: false,
    fallo: { code: 'insufficient_funds', status: 402, message: 'Insufficient funds', abortado: false, sinSesion: false },
  });
  // respuesta vacía por filtro del proveedor
  p.respuesta = () => Promise.resolve({ message: { content: '' }, finish_reason: 'content_filter' });
  await t.alPedido({ ...PEDIDO, id: 'fedcba9876543210fedcba98' });
  assert.deepEqual(b.respuestas.at(-1).fallo, { code: 'content_filter' });
});

test('trabajo: 401 con el token aún puesto → sinSesion:false; puter:refrescar relee y republica', async () => {
  const { t, doc, b, estado } = montarTrabajo();
  const p = puterFalso();
  estado.lib = p;
  doc.scripts[0].emitir('load');
  await asentar();
  p.respuesta = () => Promise.reject({ success: false, error: { code: 'token_auth_failed', message: 'Unauthorized', status: 401 } });
  await t.alPedido(PEDIDO);
  // la página relee isSignedIn() tras el fallo: sigue con sesión, y así lo dice
  assert.equal(b.respuestas.at(-1).fallo.sinSesion, false);
  const publicados = b.estados.length;
  // el main pide releer: aunque no cambió nada, se vuelve a publicar el estado real
  assert.equal(typeof b.oyentes.refrescar, 'function');
  b.oyentes.refrescar();
  await asentar();
  assert.equal(b.estados.length, publicados + 1);
  assert.deepEqual(b.estados.at(-1), { cargado: true, sesion: true, error: null });
  // puter.js soltó el token un instante después: al releer, se publica «sin sesión» (una vez)
  p.sesion = false;
  b.oyentes.refrescar();
  await asentar();
  assert.equal(b.estados.length, publicados + 2);
  assert.deepEqual(b.estados.at(-1), { cargado: true, sesion: false, error: null });
  // sin puter.js cargado no llama a nada y republica lo que hay
  const sinCargar = montarTrabajo({ marca: false });
  sinCargar.b.oyentes.refrescar();
  await asentar();
  assert.deepEqual(sinCargar.b.estados, [{ cargado: false, sesion: false, error: null }, { cargado: false, sesion: false, error: null }]);
});

test('trabajo: plazo vencido o cancelación del main → aborta la llamada y responde «abortado»', async () => {
  const { t, doc, b, estado, reloj } = montarTrabajo();
  const p = puterFalso();
  estado.lib = p;
  doc.scripts[0].emitir('load');
  await asentar();
  let senal = null;
  p.respuesta = (_m, _t, o) => {
    senal = o.signal;
    return new Promise(() => {}); // puter.js colgado
  };
  const enVuelo = t.alPedido(PEDIDO);
  await asentar();
  assert.equal(t.enCurso(), 1);
  reloj.disparar(38000);
  await enVuelo;
  assert.equal(senal.aborted, true);
  assert.equal(b.respuestas.at(-1).fallo.abortado, true);
  assert.equal(t.enCurso(), 0);
  // cancelación explícita (el main ya respondió «timeout»)
  const otro = t.alPedido({ ...PEDIDO, id: 'fedcba9876543210fedcba98' });
  await asentar();
  t.alCancelar('fedcba9876543210fedcba98');
  await otro;
  assert.equal(b.respuestas.at(-1).fallo.abortado, true);
  assert.equal(b.respuestas.at(-1).id, 'fedcba9876543210fedcba98');
});

test('trabajo: si puter.js no carga → «carga_fallida» y reintento con espera creciente', async () => {
  const { doc, b, reloj } = montarTrabajo();
  doc.scripts[0].emitir('error');
  assert.deepEqual(b.estados.at(-1), { cargado: false, sesion: false, error: 'carga_fallida' });
  assert.equal(doc.scripts[0].quitado, true);
  assert.deepEqual(reloj.pendientes.map((h) => h.ms), [30000]);
  reloj.disparar(30000);
  assert.equal(doc.scripts.length, 2);
  doc.scripts[1].emitir('error');
  assert.equal(reloj.pendientes.at(-1).ms, 60000);
});

test('trabajo: modelo por defecto ausente de listModels → aviso «modelo_desconocido» (una vez)', async () => {
  const { t, doc, b, estado } = montarTrabajo();
  estado.lib = puterFalso({ modelos: [{ id: 'gpt-5.4-nano', provider: 'openai' }] });
  doc.scripts[0].emitir('load');
  await asentar();
  t.alInfo({ modelo: 'google/gemini-3.5-flash-lite' });
  await asentar();
  assert.deepEqual(b.estados.at(-1), { cargado: true, sesion: true, error: 'modelo_desconocido' });
  // presente (por id con proveedor, alias o sin el prefijo) → sin aviso
  for (const lista of [
    [{ id: 'gemini-3.5-flash-lite', provider: 'google' }],
    [{ id: 'x', aliases: ['google/gemini-3.5-flash-lite'] }],
    [{ id: 'gemini-3.5-flash-lite' }],
  ]) assert.equal(trabajo.modeloDisponible(lista, 'google/gemini-3.5-flash-lite:flex'), true, JSON.stringify(lista));
  assert.equal(trabajo.modeloDisponible([], 'x'), false);
});

test('trabajo: extraerTexto, respuestaRechazada y describirFallo con las formas de Puter', () => {
  assert.equal(trabajo.extraerTexto('directo'), 'directo');
  assert.equal(trabajo.extraerTexto({ message: { content: 'a' } }), 'a');
  assert.equal(trabajo.extraerTexto({ message: { content: [{ type: 'thinking', thinking: 'x' }, { type: 'text', text: 'b' }] } }), 'b');
  assert.equal(trabajo.extraerTexto({ choices: [{ message: { content: 'c' } }] }), 'c');
  assert.equal(trabajo.extraerTexto({ text: 'd' }), 'd');
  assert.equal(trabajo.extraerTexto(null), '');
  assert.equal(trabajo.respuestaRechazada({ finish_reason: 'content_filter' }), true);
  assert.equal(trabajo.respuestaRechazada({ message: { refusal: 'no' } }), true);
  assert.equal(trabajo.respuestaRechazada({ finish_reason: 'stop' }), false);
  const xhr = { readyState: 4, status: 0, open() {} };
  assert.deepEqual(trabajo.describirFallo(xhr), { code: 'network_error', status: 0 });
  assert.deepEqual(trabajo.describirFallo('texto'), { message: 'texto' });
  assert.deepEqual(trabajo.describirFallo({ error: 'auth_canceled', msg: 'cancelado' }), { code: 'auth_canceled', message: 'cancelado' });
  assert.deepEqual(trabajo.describirFallo(Object.assign(new Error('x'), { name: 'AbortError' })), { message: 'x', name: 'AbortError' });
});

test('trabajo: el archivo no usa innerHTML, eval ni abre ventanas', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'puter', 'trabajo.js'), 'utf8');
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|window\.open|signIn/.test(src));
  const html = fs.readFileSync(path.join(__dirname, '..', 'puter', 'trabajo.html'), 'utf8');
  assert.ok(!/<script>/.test(html) && html.includes('src="trabajo.js"'));
});

// ---------------------------------------------------------------------------------------------
// conectar.js (ventanita de inicio de sesión)
// ---------------------------------------------------------------------------------------------

function montarConectar() {
  const doc = documentoFalso(['entrar', 'cerrar', 'estado', 'modelo']);
  const c = { conexiones: [], cerrado: 0, marcas: 0, info: null };
  const estado = { lib: null };
  const puente = {
    version: 1,
    conexion: (r) => c.conexiones.push(r),
    cerrar: () => { c.cerrado += 1; },
    onInfo: (cb) => { c.info = cb; },
  };
  const conexion = conectarJs.crearConexion({
    puter: () => estado.lib, puente, documento: doc, marcar: () => { c.marcas += 1; },
  });
  conexion.arrancar();
  return { conexion, doc, c, estado, el: doc.elementos };
}

test('conectar: botón deshabilitado hasta cargar puter.js; sin sesión invita a iniciarla', () => {
  const { doc, el, estado, c } = montarConectar();
  assert.equal(el.entrar.disabled, true);
  assert.equal(doc.scripts[0].src, URL_PUTER_JS);
  estado.lib = puterFalso({ sesion: false });
  doc.scripts[0].emitir('load');
  assert.equal(el.entrar.disabled, false);
  assert.match(el.estado.textContent, /Pulsa el botón/);
  assert.deepEqual(c.conexiones, []);
  assert.equal(c.marcas, 0);
  c.info({ modelo: 'google/gemini-3.5-flash-lite' });
  assert.equal(el.modelo.hidden, false);
  assert.match(el.modelo.textContent, /gemini-3\.5-flash-lite/);
});

test('conectar: con la sesión ya iniciada pone la marca y avisa al main', () => {
  const { doc, el, estado, c } = montarConectar();
  estado.lib = puterFalso({ sesion: true });
  doc.scripts[0].emitir('load');
  assert.equal(c.marcas, 1);
  assert.deepEqual(c.conexiones, [{ ok: true }]);
  assert.equal(el.entrar.textContent, 'Cambiar de cuenta');
});

test('conectar: el clic llama a signIn en el MISMO gesto; al terminar marca y avisa «nueva»', async () => {
  const { conexion, doc, el, estado, c } = montarConectar();
  const p = puterFalso({ sesion: false });
  const llamadas = [];
  p.auth.signIn = (o) => { llamadas.push(o); return Promise.resolve({ user: {} }); };
  estado.lib = p;
  doc.scripts[0].emitir('load');
  el.entrar.emitir('click');
  assert.deepEqual(llamadas, [undefined]); // síncrono: antes de cualquier await
  assert.equal(el.entrar.disabled, true);
  await asentar();
  assert.equal(c.marcas, 1);
  assert.deepEqual(c.conexiones.at(-1), { ok: true, nueva: true });
  assert.equal(el.entrar.disabled, false);
  assert.match(el.estado.textContent, /Conectado/);
  // segundo clic: cambiar de cuenta pide elegirla de nuevo
  await conexion.alPulsar();
  assert.deepEqual(llamadas.at(-1), { request_auth: true });
});

test('conectar: ventana bloqueada o cerrada → mensaje claro, aviso al main y botón reactivado', async () => {
  const { doc, el, estado, c } = montarConectar();
  const p = puterFalso({ sesion: false });
  p.auth.signIn = () => Promise.reject({ error: 'popup_blocked', msg: 'Popup blocked' });
  estado.lib = p;
  doc.scripts[0].emitir('load');
  el.entrar.emitir('click');
  await asentar();
  assert.equal(el.estado.textContent, conectarJs.MENSAJES.popup_blocked);
  assert.deepEqual(c.conexiones.at(-1), { ok: false, error: 'popup_blocked' });
  assert.equal(el.entrar.disabled, false);
  assert.equal(c.marcas, 0);
  p.auth.signIn = () => { throw new Error('boom'); }; // excepción síncrona
  el.entrar.emitir('click');
  await asentar();
  assert.deepEqual(c.conexiones.at(-1), { ok: false, error: 'otro' });
});

test('conectar: sin red → «carga_fallida»; «Cerrar» pide cerrar la ventanita', () => {
  const { doc, el, c } = montarConectar();
  doc.scripts[0].emitir('error');
  assert.equal(el.entrar.disabled, true);
  assert.deepEqual(c.conexiones, [{ ok: false, error: 'carga_fallida' }]);
  el.cerrar.emitir('click');
  assert.equal(c.cerrado, 1);
});

test('conectar: codigoError e iniciar sin puente', () => {
  assert.equal(conectarJs.codigoError({ error: 'auth_window_closed' }), 'auth_window_closed');
  assert.equal(conectarJs.codigoError({ code: 'Unsupported-Origin' }), 'unsupported_origin');
  assert.equal(conectarJs.codigoError('x'.repeat(40)).length, 24);
  assert.equal(conectarJs.codigoError(null), 'otro');
  assert.equal(conectarJs.iniciar({}), null);
  assert.equal(conectarJs.iniciar({ amadeusPuter: { version: 2 } }), null);
  const html = fs.readFileSync(path.join(__dirname, '..', 'puter', 'conectar.html'), 'utf8');
  assert.ok(!/<script>/.test(html) && !/\son[a-z]+=/i.test(html)); // sin scripts ni manejadores en línea
  const src = fs.readFileSync(path.join(__dirname, '..', 'puter', 'conectar.js'), 'utf8');
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function/.test(src));
});

// ---------------------------------------------------------------------------------------------
// preload-puter.js
// ---------------------------------------------------------------------------------------------

function cargarPreloadPuter(origen) {
  const expuesto = {};
  const enviados = [];
  const oyentes = new Map();
  const electron = {
    contextBridge: { exposeInMainWorld: (nombre, api) => { expuesto[nombre] = api; } },
    ipcRenderer: {
      send: (canal, ...args) => enviados.push([canal, ...args]),
      on: (canal, fn) => {
        if (!oyentes.has(canal)) oyentes.set(canal, new Set());
        oyentes.get(canal).add(fn);
      },
      removeListener: (canal, fn) => oyentes.get(canal)?.delete(fn),
    },
  };
  const fuente = fs.readFileSync(path.join(__dirname, '..', 'preload-puter.js'), 'utf8');
  const requerir = (m) => {
    if (m === 'electron') return electron;
    throw new Error(`el preload no debe cargar ${m}`);
  };
  // eslint-disable-next-line no-new-func
  new Function('require', 'module', 'exports', 'location', fuente)(requerir, { exports: {} }, {}, { origin: origen });
  const emitir = (canal, dato) => {
    for (const fn of oyentes.get(canal) || []) fn({ sender: 'falso' }, dato);
  };
  return { expuesto, enviados, oyentes, emitir };
}

test('preload-puter: window.amadeusPuter v1 solo en el origen propio (no en la emergente de puter.com)', () => {
  const { expuesto } = cargarPreloadPuter('http://amadeus-puter.localhost');
  assert.deepEqual(Object.keys(expuesto), ['amadeusPuter']);
  const api = expuesto.amadeusPuter;
  assert.deepEqual(Object.keys(api).sort(),
    ['cerrar', 'conexion', 'estado', 'onCancelar', 'onInfo', 'onPedido', 'onRefrescar', 'responder', 'version'].sort());
  assert.equal(api.version, 1);
  assert.ok(!('ipcRenderer' in api));
  for (const ajeno of ['https://puter.com', 'http://localhost', 'null', '']) {
    assert.deepEqual(cargarPreloadPuter(ajeno).expuesto, {}, ajeno);
  }
});

test('preload-puter: canales y saneo', () => {
  const { expuesto, enviados, emitir, oyentes } = cargarPreloadPuter('http://amadeus-puter.localhost');
  const api = expuesto.amadeusPuter;
  api.estado({ cargado: true, sesion: 'sí', error: 'Carga Fallida!' });
  api.responder({ id: 'x'.repeat(100), ok: true, texto: 'hola', modelo: 'm', fallo: { code: 'no' } });
  api.responder({ id: 'abc', ok: false, fallo: { code: { raro: 1 }, status: 402, message: 'm'.repeat(500), abortado: 'sí', sinSesion: true } });
  api.responder('basura');
  api.conexion({ ok: true, nueva: true, error: 'popup_blocked' });
  api.cerrar();
  assert.deepEqual(enviados, [
    ['puter:estado', { cargado: true, sesion: false, error: null }],
    ['puter:respuesta', { id: 'x'.repeat(40), ok: true, texto: 'hola', modelo: 'm', fallo: null }],
    ['puter:respuesta', {
      id: 'abc', ok: false, texto: null, modelo: null,
      fallo: { code: null, status: 402, message: 'm'.repeat(200), name: null, abortado: false, sinSesion: true },
    }],
    ['puter:conexion', { ok: true, nueva: true, error: 'popup_blocked' }],
    ['puter:cerrar'],
  ]);
  const recibidos = [];
  const quitar = api.onPedido((p) => recibidos.push(p));
  api.onCancelar((id) => recibidos.push(id));
  api.onInfo((i) => recibidos.push(i));
  api.onRefrescar((...args) => recibidos.push(['refrescar', args.length]));
  emitir('puter:pedir', { id: 'a' });
  emitir('puter:cancelar', 'a');
  emitir('puter:info', { modelo: 'm' });
  emitir('puter:refrescar', { lo: 'que sea' }); // sin datos para la página
  assert.deepEqual(recibidos, [{ id: 'a' }, 'a', { modelo: 'm' }, ['refrescar', 0]]);
  quitar();
  assert.equal(oyentes.get('puter:pedir').size, 0);
});
