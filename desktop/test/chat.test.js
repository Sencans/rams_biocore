'use strict';
/**
 * `chat.js` con un DOM FALSO mínimo (sin navegador ni Electron): confirmaciones con R.A.M.S.
 * desconectada, reconexión con la instantánea y caducadas.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const FUENTE = fs.readFileSync(path.join(__dirname, '..', 'chat.js'), 'utf8');

class ElementoFalso {
  constructor(tag, id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.textContent = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.dataset = {};
    this.style = {};
    this.value = '';
    this.open = false;
    this.oyentes = {};
    this.atributos = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    const clases = new Set();
    this.classList = {
      add: (c) => clases.add(c),
      remove: (c) => clases.delete(c),
      toggle: (c, si) => ((si ?? !clases.has(c)) ? clases.add(c) : clases.delete(c)),
      contains: (c) => clases.has(c),
    };
  }

  get firstElementChild() { return this.children[0] || null; }
  append(...nodos) {
    for (const n of nodos) {
      n.parentNode = this;
      this.children.push(n);
    }
  }
  remove() {
    if (!this.parentNode) return;
    const hermanos = this.parentNode.children;
    hermanos.splice(hermanos.indexOf(this), 1);
    this.parentNode = null;
  }
  addEventListener(tipo, fn) { (this.oyentes[tipo] ||= []).push(fn); }
  emitir(tipo, ev = {}) { return (this.oyentes[tipo] || []).map((fn) => fn({ preventDefault() {}, ...ev })); }
  setAttribute(k, v) { this.atributos[k] = String(v); }
  scrollIntoView() {}
  contains(n) { return n === this || this.children.some((c) => c.contains(n)); }
  querySelectorAll() { return []; }
  /** Clic con ratón (detail 1): espera a que termine el manejador asíncrono. */
  async clic() { await Promise.all(this.emitir('click', { detail: 1 })); }
}

function cargarChat({ confirmar }) {
  const ids = ['lista', 'vacio', 'texto', 'form', 'enviar', 'estado', 'estado-texto', 'hablando', 'contador', 'menu', 'btn-cerrar'];
  const porId = new Map(ids.map((id) => [id, new ElementoFalso('div', id)]));
  porId.get('lista').append(porId.get('vacio'));
  const documento = {
    getElementById: (id) => porId.get(id) || null,
    createElement: (tag) => new ElementoFalso(tag),
    addEventListener() {},
    hasFocus: () => false,
  };
  const oyentes = {};
  const confirmaciones = [];
  const api = {
    version: 1,
    send: async () => false,
    onMessage: (cb) => { oyentes.msg = cb; return () => {}; },
    onStatus: (cb) => { oyentes.status = cb; return () => {}; },
    onVoz: (cb) => { oyentes.voz = cb; return () => {}; },
    onPermiso: (cb) => { oyentes.permiso = cb; return () => {}; },
    confirmar: (id, aprobado) => {
      confirmaciones.push([id, aprobado]);
      return Promise.resolve(confirmar());
    },
    control() {},
    abrirEnlace() {},
    hide() {},
  };
  const ventana = { amadeusChat: api, addEventListener() {} };
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'performance', FUENTE)(ventana, documento, performance);
  const lista = porId.get('lista');
  const tarjetas = () => lista.children.filter((e) => e.tagName === 'SECTION');
  const partes = (tarjeta) => {
    const botones = tarjeta.children.find((c) => c.className === 'botones').children;
    const resultado = tarjeta.children.find((c) => c.className === 'resultado');
    return { aprobar: botones[0], rechazar: botones[1], resultado };
  };
  const sistema = () => lista.children.filter((e) => e.className === 'msg sistema')
    .map((e) => e.children.find((c) => c.className === 'cuerpo').textContent);
  return { oyentes, confirmaciones, tarjetas, partes, sistema };
}

const PENDIENTE = { id: 'deadbeef', descripcion: 'Abrir WhatsApp con un borrador', pendiente: true };
const estadoBotones = (p) => [p.aprobar.disabled, p.rechazar.disabled];

test('chat: aprobar con R.A.M.S. caída → botones de nuevo activos y aviso (no se queda «Aprobando…»)', async () => {
  const c = cargarChat({ confirmar: () => false });
  c.oyentes.status('en_linea');
  c.oyentes.permiso(PENDIENTE);
  const p = c.partes(c.tarjetas()[0]);
  await p.aprobar.clic();
  assert.deepEqual(c.confirmaciones, [['deadbeef', true]]);
  assert.deepEqual(estadoBotones(p), [false, false]);
  assert.equal(p.resultado.hidden, true);
  assert.match(c.sistema().at(-1), /No se envió la confirmación/);
  // la misma pendiente otra vez (instantánea): sigue siendo una sola tarjeta y se puede responder
  c.oyentes.permiso(PENDIENTE);
  assert.equal(c.tarjetas().length, 1);
  assert.deepEqual(estadoBotones(p), [false, false]);
});

test('chat: enviada → «Aprobando…»; se cae y vuelve → la instantánea la reactiva; luego se resuelve', async () => {
  const c = cargarChat({ confirmar: () => true });
  c.oyentes.status('en_linea');
  c.oyentes.permiso(PENDIENTE);
  const p = c.partes(c.tarjetas()[0]);
  await p.rechazar.clic();
  assert.deepEqual(estadoBotones(p), [true, true]);
  assert.equal(p.resultado.textContent, 'Rechazando…');
  await p.aprobar.clic(); // bloqueada mientras espera la resolución
  assert.equal(c.confirmaciones.length, 1);
  c.oyentes.status('desconectada');
  assert.deepEqual(estadoBotones(p), [true, true]);
  assert.match(p.resultado.textContent, /Sin conexión/);
  c.oyentes.status('en_linea');
  assert.deepEqual(estadoBotones(p), [true, true]); // hasta ver la instantánea
  c.oyentes.permiso(PENDIENTE);
  assert.deepEqual(estadoBotones(p), [false, false]);
  assert.equal(p.resultado.hidden, true);
  await p.aprobar.clic();
  c.oyentes.permiso({ id: 'deadbeef', descripcion: 'x', pendiente: false, aprobado: true });
  assert.equal(p.resultado.textContent, 'Aprobada.');
  assert.deepEqual(estadoBotones(p), [true, true]);
});

test('chat: sin conexión el clic no la deja pulsable hasta que vuelva', async () => {
  const c = cargarChat({ confirmar: () => false });
  c.oyentes.status('en_linea');
  c.oyentes.permiso(PENDIENTE);
  const p = c.partes(c.tarjetas()[0]);
  c.oyentes.status('desconectada');
  // (un clic que se colara antes de bloquearla) → sigue bloqueada, con el motivo
  p.aprobar.disabled = false;
  await p.aprobar.clic();
  assert.deepEqual(estadoBotones(p), [true, true]);
  assert.match(p.resultado.textContent, /Sin conexión/);
  assert.match(c.sistema().at(-1), /No se envió la confirmación/);
});

test('chat: caducada → «Ya no está pendiente.»; si R.A.M.S. la vuelve a pedir, tarjeta nueva', () => {
  const c = cargarChat({ confirmar: () => true });
  c.oyentes.status('en_linea');
  c.oyentes.permiso(PENDIENTE);
  const p = c.partes(c.tarjetas()[0]);
  c.oyentes.permiso({ id: 'deadbeef', pendiente: false, caducada: true });
  assert.equal(p.resultado.textContent, 'Ya no está pendiente.');
  assert.deepEqual(estadoBotones(p), [true, true]);
  c.oyentes.permiso(PENDIENTE);
  assert.equal(c.tarjetas().length, 2);
  assert.deepEqual(estadoBotones(c.partes(c.tarjetas()[1])), [false, false]);
});

test('chat: una pendiente nueva cierra la anterior', () => {
  const c = cargarChat({ confirmar: () => true });
  c.oyentes.status('en_linea');
  c.oyentes.permiso(PENDIENTE);
  c.oyentes.permiso({ id: '0a1b2c3d', descripcion: 'Otra', pendiente: true });
  const [vieja, nueva] = c.tarjetas().map(c.partes);
  assert.equal(vieja.resultado.textContent, 'Ya no está pendiente.');
  assert.deepEqual(estadoBotones(nueva), [false, false]);
});
