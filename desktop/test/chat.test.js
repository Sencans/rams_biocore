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
  get firstChild() { return this.children[0] || null; }
  append(...nodos) {
    for (const n of nodos) {
      if (n.parentNode && n.parentNode !== this) n.remove();
      n.parentNode = this;
      this.children.push(n);
    }
  }
  replaceChildren(...nodos) {
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...nodos);
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

function cargarChat({ confirmar = () => true, decidir = () => true } = {}) {
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
  const decisiones = [];
  const copias = [];
  const api = {
    version: 2,
    send: async () => false,
    onMessage: (cb) => { oyentes.msg = cb; return () => {}; },
    onStatus: (cb) => { oyentes.status = cb; return () => {}; },
    onVoz: (cb) => { oyentes.voz = cb; return () => {}; },
    onPermiso: (cb) => { oyentes.permiso = cb; return () => {}; },
    onTarjeta: (cb) => { oyentes.tarjeta = cb; return () => {}; },
    confirmar: (id, aprobado) => {
      confirmaciones.push([id, aprobado]);
      return Promise.resolve(confirmar());
    },
    decidirTarjeta: (id, sha256, decision, extra) => {
      decisiones.push([id, sha256, decision, extra]);
      return Promise.resolve(decidir());
    },
    copiarCodigo: (id) => { copias.push(id); },
    control() {},
    abrirEnlace() {},
    hide() {},
  };
  const ventana = { amadeusChat: api, addEventListener() {} };
  // Reloj y temporizadores FALSOS: el tiempo solo avanza con `avanzar(ms)` (guarda de teclado, armado).
  const reloj = { t: 1_000_000, now() { return this.t; } };
  let temporizadores = [];
  const ponerTemporizador = (fn, ms) => { temporizadores.push({ fn, en: reloj.t + ms }); return temporizadores.length; };
  const avanzar = (ms) => {
    reloj.t += ms;
    const vencidos = temporizadores.filter((x) => x.en <= reloj.t);
    temporizadores = temporizadores.filter((x) => x.en > reloj.t);
    for (const x of vencidos) x.fn();
  };
  // eslint-disable-next-line no-new-func
  new Function('window', 'document', 'performance', 'setTimeout', FUENTE)(ventana, documento, reloj, ponerTemporizador);
  const lista = porId.get('lista');
  const tarjetas = () => lista.children.filter((e) => e.tagName === 'SECTION');
  const partes = (tarjeta) => {
    const botones = tarjeta.children.find((c) => c.className === 'botones').children;
    const resultado = tarjeta.children.find((c) => c.className === 'resultado');
    return { aprobar: botones[0], rechazar: botones[1], resultado };
  };
  const sistema = () => lista.children.filter((e) => e.className === 'msg sistema')
    .map((e) => e.children.find((c) => c.className === 'cuerpo').textContent);
  const aprobables = () => lista.children.filter((e) => e.tagName === 'ARTICLE' && e.className.startsWith('msg tarjeta'));
  return { oyentes, confirmaciones, decisiones, copias, tarjetas, aprobables, partes, sistema, lista, avanzar };
}

/** Todos los descendientes de `el` que cumplen `pred` (en orden de documento). */
function buscar(el, pred) {
  const out = [];
  for (const c of el.children) {
    if (pred(c)) out.push(c);
    out.push(...buscar(c, pred));
  }
  return out;
}
/** Texto visible de un subárbol del DOM falso (textContent propio + el de los hijos). */
const texto = (el) => el.textContent + el.children.map(texto).join('');
const botonesDe = (el) => buscar(el, (c) => c.tagName === 'BUTTON');
const boton = (el, etiqueta) => botonesDe(el).find((b) => b.textContent === etiqueta);

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

// ---------------------------------------------------------------- tarjetas aprobables (v0.2.2, §13.4)

const H = 'ab'.repeat(32);
function tarjetaDe(clase, seccion, extra = {}) {
  const t = { id: '0a1b2c3d', clase, version: 1, sha256: H, titulo: '', resumen: '', aviso: '', caduca_ts: null,
    canal_origen: 'escritorio', estado: clase === 'salida' ? 'info' : 'pendiente', correo: null, drive: null, script: null,
    salida: null, botones: [], ...extra };
  t[clase] = seccion;
  return t;
}
const CORREO = { para: ['ana@example.com', 'jefe@empresa.com'], cc: [], cco: ['yo@example.com'], asunto: 'Informe <b>Q3</b>',
  cuerpo: 'Hola Ana,\n\t<script>alert(1)</script>\nSaludos', adjuntos: [{ nombre: 'informe.pdf', bytes: 2150000, carpeta: 'documentos' }],
  problemas: [], avisos: ['Revisa el destinatario'], listo: true };
const correo = (extra = {}, seccion = {}) => tarjetaDe('correo', { ...CORREO, ...seccion },
  { botones: ['aprobar', 'editar', 'descartar'], ...extra });
const CODIGO = 'Remove-Item -Recurse "$env:TEMP\\x"\n# <img src=x onerror=alert(1)>\nWrite-Output "listo"\n';
const script = (seccion = {}, extra = {}) => tarjetaDe('script', {
  lenguaje: 'powershell', plataforma: 'windows', codigo: CODIGO, explicacion: 'Limpia la carpeta temporal.',
  efectos: ['borra archivos de TEMP'], requisitos: [], nivel: 'medio', ejecutable: true, motivo_no_ejecutable: '',
  metodo: 'ast', hallazgos: [{ nivel: 'alto', categoria: 'borrado', linea: 1, detalle: 'Remove-Item -Recurse' }],
  hallazgos_omitidos: 0, habilidad: null, habilidad_cambiada: false, ejecuciones: 0, ...seccion,
}, { id: '5c1a7e00', botones: ['aprobar', 'guardar', 'copiar', 'descartar'], ...extra });

test('chat/tarjetas: correo con textContent (nada de HTML), etiquetas y Enviar ligado a id y huella', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo({ titulo: 'Correo para Ana' }));
  const [art] = c.aprobables();
  assert.equal(art.className, 'msg tarjeta correo');
  assert.equal(art.dataset.id, '0a1b2c3d');
  const todo = texto(art);
  assert.ok(todo.includes('📧 Correo') && todo.includes('Correo para Ana'));
  assert.ok(todo.includes('ana@example.com, jefe@empresa.com') && todo.includes('yo@example.com'));
  assert.ok(todo.includes('Hola Ana,\n\t<script>alert(1)</script>\nSaludos'));          // literal, como texto
  assert.ok(todo.includes('Informe <b>Q3</b>') && todo.includes('informe.pdf · 2,1 MB · Documentos'));
  assert.equal(buscar(art, (e) => ['SCRIPT', 'B', 'IMG'].includes(e.tagName)).length, 0);
  assert.deepEqual(botonesDe(art).map((b) => b.textContent), ['Enviar', 'Editar', 'Descartar']);
  await boton(art, 'Enviar').clic();
  assert.deepEqual(c.decisiones, [['0a1b2c3d', H, 'aprobar', { version: 1 }]]);       // la versión que se ve
  // Bloqueada con «Enviando…» hasta la versión nueva o el cierre.
  assert.ok(botonesDe(art).every((b) => b.disabled));
  assert.ok(texto(art).includes('Enviando…'));
  await boton(art, 'Enviar').clic();
  assert.equal(c.decisiones.length, 1);
  // Cerrada: resultado y sin botones.
  c.oyentes.tarjeta({ id: '0a1b2c3d', clase: 'correo', estado: 'cerrada', resultado: 'enviado', motivo: '' });
  assert.equal(botonesDe(art).length, 0);
  assert.ok(texto(art).includes('Enviado a Gmail'));
  assert.ok(art.classList.contains('cerrada'));
});

test('chat/tarjetas: «listo» falso deshabilita Enviar; decidirTarjeta false → «Sin conexión con R.A.M.S.» y se reactiva', async () => {
  const c = cargarChat({ decidir: () => false });
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo({}, { listo: false, problemas: ['Falta el destinatario'] }));
  const [art] = c.aprobables();
  assert.equal(boton(art, 'Enviar').disabled, true);
  assert.ok(texto(art).includes('Falta el destinatario'));
  await boton(art, 'Descartar').clic();
  assert.deepEqual(c.decisiones.map((d) => d[2]), ['descartar']);
  assert.ok(texto(art).includes('Sin conexión con R.A.M.S.'));
  assert.equal(boton(art, 'Descartar').disabled, false);
  // Con R.A.M.S. desconectada: bloqueada y sin intentar enviar.
  c.oyentes.status('desconectada');
  assert.ok(botonesDe(art).every((b) => b.disabled));
});

test('chat/tarjetas: con problemas, «Guardar cambios» manda destinatarios y adjuntos como se ven (sin atascarse)', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  // R.A.M.S. no conoce a «mi tía Rosa»: no sale en Para (queda pendiente y bloquea) y el formulario no la muestra.
  c.oyentes.tarjeta(correo({}, { listo: false, problemas: ['No conozco a «mi tía Rosa» (Para): escribe su correo'] }));
  const [art] = c.aprobables();
  await boton(art, 'Editar').clic();
  await boton(art, 'Guardar cambios').clic();                             // sin tocar nada: deja Para como se ve
  assert.deepEqual(c.decisiones, [['0a1b2c3d', H, 'editar', { campos: {
    para: 'ana@example.com, jefe@empresa.com', cc: '', cco: 'yo@example.com', adjuntos: 'informe.pdf' }, version: 1 }]]);
  // Sin problemas y sin cambios no se manda nada (como siempre).
  const d = cargarChat();
  d.oyentes.status('en_linea');
  d.oyentes.tarjeta(correo());
  const [art2] = d.aprobables();
  await boton(art2, 'Editar').clic();
  await boton(art2, 'Guardar cambios').clic();
  assert.equal(d.decisiones.length, 0);
});

test('chat/tarjetas: edición del correo con maxlength; solo salen los campos cambiados, sin recortar', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo());
  const [art] = c.aprobables();
  await boton(art, 'Editar').clic();
  assert.equal(c.decisiones.length, 0);                                  // editar es local hasta guardar
  const entradas = buscar(art, (e) => e.tagName === 'INPUT' || e.tagName === 'TEXTAREA');
  assert.deepEqual(entradas.map((e) => [e.dataset.campo, e.atributos.maxlength]),
    [['para', '2600'], ['cc', '2600'], ['cco', '2600'], ['asunto', '200'], ['cuerpo', '10000'], ['adjuntos', '1000']]);
  assert.equal(entradas.find((e) => e.dataset.campo === 'cuerpo').tagName, 'TEXTAREA');
  assert.equal(entradas.find((e) => e.dataset.campo === 'cuerpo').value, CORREO.cuerpo);
  assert.deepEqual(botonesDe(art).map((b) => b.textContent), ['Guardar cambios', 'Cancelar']);
  // Una versión igual mientras se edita (p. ej. la instantánea) no borra lo escrito.
  const cuerpo = entradas.find((e) => e.dataset.campo === 'cuerpo');
  cuerpo.value = 'Nuevo cuerpo\n\tcon sangría';
  c.oyentes.tarjeta(correo());
  assert.equal(buscar(art, (e) => e.dataset.campo === 'cuerpo')[0].value, 'Nuevo cuerpo\n\tcon sangría');
  await boton(art, 'Guardar cambios').clic();
  assert.deepEqual(c.decisiones, [['0a1b2c3d', H, 'editar', { campos: { cuerpo: 'Nuevo cuerpo\n\tcon sangría' }, version: 1 }]]);
  // R.A.M.S. la pone «en curso» (misma versión, sin botones de decisión): el formulario sigue, sin
  // «Guardar cambios» activo y con el aviso de lo que está haciendo.
  c.oyentes.tarjeta(correo({ botones: [], aviso: 'Guardando los cambios…' }));
  assert.equal(buscar(art, (e) => e.dataset.campo === 'cuerpo')[0].value, 'Nuevo cuerpo\n\tcon sangría');
  assert.equal(boton(art, 'Guardar cambios').disabled, true);
  assert.equal(boton(art, 'Cancelar').disabled, false);
  assert.ok(texto(art).includes('Guardando los cambios…'));
  // Un valor por encima del tope (pegado por código) no se recorta: no se envía.
  c.oyentes.tarjeta(correo({ version: 2 }));
  await boton(art, 'Editar').clic();
  buscar(art, (e) => e.dataset.campo === 'asunto')[0].value = 'x'.repeat(201);
  await boton(art, 'Guardar cambios').clic();
  assert.equal(c.decisiones.length, 1);
  assert.ok(texto(art).includes('Es demasiado largo para guardarlo: acórtalo.'));
  await boton(art, 'Cancelar').clic();
  assert.deepEqual(botonesDe(art).map((b) => b.textContent), ['Enviar', 'Editar', 'Descartar']);
});

test('chat/tarjetas: script con nivel, hallazgos, código COMPLETO con números de línea y copiar', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(script({ hallazgos_omitidos: 7 }));
  const [art] = c.aprobables();
  assert.equal(art.className, 'msg tarjeta script');
  const todo = texto(art);
  assert.ok(todo.includes('🧪 Script · PowerShell') && todo.includes('riesgo medio'));
  assert.ok(todo.includes('línea 1 · borrado · alto · Remove-Item -Recurse'));
  assert.ok(todo.includes('… y 7 hallazgos más'));
  const pre = buscar(art, (e) => e.tagName === 'PRE')[0];
  const code = buscar(pre, (e) => e.tagName === 'CODE')[0];
  assert.equal(code.textContent, CODIGO);                                  // nunca recortado
  assert.equal(buscar(pre, (e) => e.className === 'lineas')[0].textContent, '1\n2\n3');
  assert.equal(buscar(art, (e) => e.tagName === 'IMG').length, 0);
  assert.deepEqual(botonesDe(art).map((b) => b.textContent), ['Ejecutar', 'Guardar como habilidad', 'Copiar código', 'Descartar']);
  await boton(art, 'Copiar código').clic();
  assert.deepEqual(c.copias, ['5c1a7e00']);
  assert.ok(boton(art, 'Copiado'));
  await boton(art, 'Ejecutar').clic();
  assert.deepEqual(c.decisiones.map((d) => d[2]), ['aprobar']);
  assert.ok(texto(art).includes('Ejecutando…'));
  // Versión nueva (tras ejecutar): misma burbuja, «Ejecutar otra vez», pulsable tras ARMADO_MS (1 s).
  c.oyentes.tarjeta(script({ ejecuciones: 1 }, { version: 2 }));
  assert.equal(c.aprobables().length, 1);
  assert.ok(boton(art, 'Ejecutar otra vez') && boton(art, 'Ejecutar otra vez').disabled);
  c.avanzar(999);
  assert.equal(boton(art, 'Ejecutar otra vez').disabled, true);
  c.avanzar(25);
  assert.equal(boton(art, 'Ejecutar otra vez').disabled, false);
  // Una versión vieja se ignora.
  c.oyentes.tarjeta(script({ ejecuciones: 0 }, { version: 1 }));
  assert.ok(boton(art, 'Ejecutar otra vez'));
});

test('chat/tarjetas: riesgo alto exige la casilla; crítico o no ejecutable, sin Ejecutar y con el motivo', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(script({ nivel: 'alto' }));
  const [art] = c.aprobables();
  const ejecutar = () => boton(art, 'Ejecutar');
  assert.equal(ejecutar().disabled, true);
  const casilla = buscar(art, (e) => e.tagName === 'INPUT' && e.type === 'checkbox')[0];
  assert.ok(texto(art).includes('Leí el código y entiendo el riesgo'));
  casilla.checked = true;
  casilla.emitir('change');
  assert.equal(ejecutar().disabled, false);
  // Crítico.
  c.oyentes.tarjeta(script({ nivel: 'critico', ejecutable: false, motivo_no_ejecutable: 'Formatea discos' },
    { id: 'c0a1b2c3', botones: ['copiar', 'descartar'] }));
  const critico = c.aprobables()[1];
  assert.deepEqual(botonesDe(critico).map((b) => b.textContent), ['Copiar código', 'Descartar']);
  assert.ok(texto(critico).includes('riesgo crítico') && texto(critico).includes('Formatea discos'));
  // Aunque R.A.M.S. mandara «aprobar» con un crítico, el chat no pinta Ejecutar.
  c.oyentes.tarjeta(script({ nivel: 'critico', ejecutable: true }, { id: 'c0a1b2c4' }));
  assert.ok(!boton(c.aprobables()[2], 'Ejecutar'));
});

test('chat/tarjetas: guardar como habilidad pide el nombre en línea (≤ 40), nunca con prompt()', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(script());
  const [art] = c.aprobables();
  await boton(art, 'Guardar como habilidad').clic();
  const entrada = buscar(art, (e) => e.tagName === 'INPUT' && e.type === 'text')[0];
  assert.equal(entrada.atributos.maxlength, '40');
  await boton(art, 'Guardar').clic();                                     // vacío: no sale
  assert.equal(c.decisiones.length, 0);
  entrada.value = 'limpiar\ttemporales';                                  // un tabulador pegado: se dice
  await boton(art, 'Guardar').clic();
  assert.equal(c.decisiones.length, 0);
  assert.ok(texto(art).includes('El nombre tiene caracteres no válidos') && !texto(art).includes('Sin conexión'));
  entrada.value = '  limpiar temporales  ';
  await boton(art, 'Guardar').clic();
  assert.deepEqual(c.decisiones, [['5c1a7e00', H, 'guardar', { nombre: 'limpiar temporales', version: 1 }]]);
  // R.A.M.S. responde con la MISMA versión y la habilidad puesta (§14.4): se sale del modo nombre.
  c.oyentes.tarjeta(script({ habilidad: 'limpiar temporales' }, { botones: ['aprobar', 'copiar', 'descartar'] }));
  assert.equal(buscar(art, (e) => e.tagName === 'INPUT' && e.type === 'text').length, 0);
  assert.ok(texto(art).includes('Habilidad «limpiar temporales»'));
  assert.deepEqual(botonesDe(art).map((b) => b.textContent), ['Ejecutar', 'Copiar código', 'Descartar']);
});

test('chat/tarjetas: una versión igual mientras se nombra (sin haber pulsado Guardar) no borra lo escrito', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(script());
  const [art] = c.aprobables();
  await boton(art, 'Guardar como habilidad').clic();
  buscar(art, (e) => e.tagName === 'INPUT' && e.type === 'text')[0].value = 'a medias';
  c.oyentes.tarjeta(script({}, { aviso: 'Recuerda revisar el código' }));
  assert.equal(buscar(art, (e) => e.tagName === 'INPUT' && e.type === 'text')[0].value, 'a medias');
});

test('chat/tarjetas: guarda de teclado de 600 ms tras el foco', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo());
  const [art] = c.aprobables();
  await Promise.all(boton(art, 'Enviar').emitir('click', { detail: 0 })); // Enter/Espacio recién enfocada
  assert.equal(c.decisiones.length, 0);
});

test('chat/tarjetas: Drive (paso 2 público) y salida del script', () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(tarjetaDe('drive', { op: 'compartir', paso: 2, archivo: { nombre: 'plan.xlsx', tipo: 'hoja', tamano: 1, modificado: '', propio: true },
    local: null, carpeta_destino: null, compartir: { con: '', rol: 'reader', publico: true }, problemas: [], listo: true },
  { botones: ['aprobar_publico', 'descartar'] }));
  const [drive] = c.aprobables();
  assert.ok(texto(drive).includes('🗂 Drive · compartir'));
  assert.ok(texto(drive).includes('Cualquiera con el enlace podrá verlo'));
  assert.deepEqual(botonesDe(drive).map((b) => b.textContent), ['Sí, hacerlo público', 'Descartar']);
  c.oyentes.tarjeta(tarjetaDe('salida', { script_id: '5c1a7e00', lenguaje: 'python', codigo_salida: 0, tiempo_agotado: true,
    duracion_s: 1.25, stdout: '<b>42</b>', stderr: 'aviso', recortada: true, carpeta: 'taller/5c1a7e00' },
  { id: '5a11da00', botones: ['descartar'] }));
  const salida = c.aprobables()[1];
  const todo = texto(salida);
  assert.ok(todo.includes('▶ Resultado del script') && todo.includes('Código de salida: 0 · 1,3 s'));
  assert.ok(todo.includes('Terminado por tiempo límite') && todo.includes('<b>42</b>') && todo.includes('… (salida recortada)'));
  assert.deepEqual(botonesDe(salida).map((b) => b.textContent), ['Cerrar']);
});

test('chat/tarjetas: reconexión — bloqueadas hasta la instantánea; caducada → «Ya no está vigente.»', () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo());
  c.oyentes.tarjeta(script());
  const [art, scr] = c.aprobables();
  c.oyentes.status('desconectada');
  assert.ok([...botonesDe(art), ...botonesDe(scr)].filter((b) => b.textContent !== 'Copiar código').every((b) => b.disabled));
  c.oyentes.status('en_linea');
  assert.equal(boton(art, 'Enviar').disabled, true);                     // aún sin instantánea
  c.oyentes.tarjeta(correo());                                            // vuelve: vigente (misma burbuja)
  assert.equal(boton(art, 'Enviar').disabled, false);
  c.oyentes.tarjeta({ id: '5c1a7e00', clase: 'script', estado: 'cerrada', resultado: 'caducado', motivo: '', caducada: true });
  assert.ok(texto(scr).includes('Ya no está vigente.'));
  assert.equal(botonesDe(scr).length, 0);
  assert.equal(c.aprobables().length, 2);
});

const DRIVE_PUBLICO = (paso) => ({ op: 'compartir', paso, archivo: { nombre: 'plan.xlsx', tipo: 'hoja', tamano: 1, modificado: '',
  propio: true }, local: null, carpeta_destino: null, compartir: { con: '', rol: 'reader', publico: true }, problemas: [], listo: true });

test('chat/tarjetas: un doble clic sobre «Compartir» nunca completa también «Sí, hacerlo público»', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(tarjetaDe('drive', DRIVE_PUBLICO(1), { id: 'd0d0d0d0', botones: ['aprobar', 'descartar'] }));
  const [art] = c.aprobables();
  const primero = () => botonesDe(art)[0];                                 // el mismo sitio de la burbuja
  assert.equal(primero().textContent, 'Compartir');
  await primero().clic();                                                  // 1.er clic del doble clic
  assert.deepEqual(c.decisiones, [['d0d0d0d0', H, 'aprobar', { version: 1 }]]);
  // R.A.M.S. responde en milisegundos: «en curso» (misma versión, sin botones) y el paso 2 (versión 2).
  c.oyentes.tarjeta(tarjetaDe('drive', DRIVE_PUBLICO(1), { id: 'd0d0d0d0', botones: [], aviso: 'En curso: espera el resultado.' }));
  assert.equal(botonesDe(art).length, 0);
  c.oyentes.tarjeta(tarjetaDe('drive', DRIVE_PUBLICO(2), { id: 'd0d0d0d0', version: 2, botones: ['aprobar_publico', 'descartar'] }));
  assert.equal(primero().textContent, 'Sí, hacerlo público');
  assert.equal(primero().disabled, true);                                  // recién pintado: aún no
  // 2.º clic del doble clic (detail 2), sobre el mismo punto: no cuenta.
  await Promise.all(primero().emitir('click', { detail: 2 }));
  // Y un clic sencillo que se colara 150 ms después (aunque el botón no estuviera deshabilitado): tampoco.
  c.avanzar(150);
  primero().disabled = false;
  await primero().clic();
  assert.equal(c.decisiones.length, 1);
  // Pasado el armado (1 s), la 2.ª confirmación es un clic explícito más y lleva la versión 2.
  c.avanzar(900);
  assert.equal(primero().disabled, false);
  await primero().clic();
  assert.deepEqual(c.decisiones.at(-1), ['d0d0d0d0', H, 'aprobar_publico', { version: 2 }]);
});

test('chat/tarjetas: una 2.ª confirmación pública que llega ya pintada (p. ej. tras recargar) también espera 1 s', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(tarjetaDe('drive', DRIVE_PUBLICO(2), { id: 'd0d0d0d0', version: 2, botones: ['aprobar_publico', 'descartar'] }));
  const [art] = c.aprobables();
  assert.equal(boton(art, 'Sí, hacerlo público').disabled, true);
  assert.equal(boton(art, 'Descartar').disabled, false);                   // descartar nunca espera
  c.avanzar(1025);
  assert.equal(boton(art, 'Sí, hacerlo público').disabled, false);
});

test('chat/tarjetas: «en curso» (sin botones de decisión) no deja volver a ejecutar; la versión nueva sí, tras 1 s', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(script());
  const [art] = c.aprobables();
  await boton(art, 'Ejecutar').clic();
  // Otro cliente o una reconexión: R.A.M.S. la da «en curso» (misma versión, solo «copiar»).
  c.oyentes.tarjeta(script({}, { botones: ['copiar'], aviso: 'Ejecutando el script: espera el resultado.' }));
  assert.deepEqual(botonesDe(art).map((b) => b.textContent), ['Copiar código']);
  assert.ok(texto(art).includes('Ejecutando el script: espera el resultado.'));
  c.avanzar(5000);
  assert.equal(c.decisiones.length, 1);
  c.oyentes.tarjeta(script({ ejecuciones: 1 }, { version: 2 }));
  assert.equal(boton(art, 'Ejecutar otra vez').disabled, true);
  c.avanzar(1025);
  await boton(art, 'Ejecutar otra vez').clic();
  assert.deepEqual(c.decisiones.map((d) => [d[2], d[3].version]), [['aprobar', 1], ['aprobar', 2]]);
});

test('chat/tarjetas: caracteres de control al editar → se dice dónde y se ofrece quitarlos (nunca «Sin conexión»)', async () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo());
  const [art] = c.aprobables();
  await boton(art, 'Editar').clic();
  const campo = (k) => buscar(art, (e) => e.dataset.campo === k)[0];
  campo('cuerpo').value = 'Hola\u000bAna,\u000cnos vemos\r\nfin\u0007';      // salto manual de Word, \f, \r\n, BEL
  campo('asunto').value = 'Informe\tQ3';                                   // tabulador pegado
  await boton(art, 'Guardar cambios').clic();
  assert.equal(c.decisiones.length, 0);                                    // nada sale
  const msg = texto(art);
  assert.ok(!msg.includes('Sin conexión'), msg);
  assert.ok(msg.includes('caracteres invisibles no válidos') && msg.includes('en Asunto, Cuerpo'), msg);
  assert.equal(campo('cuerpo').value, 'Hola\u000bAna,\u000cnos vemos\r\nfin\u0007');   // nunca se quitan en silencio
  const limpiar = boton(art, 'Quitar caracteres no válidos');
  assert.ok(limpiar && !limpiar.hidden && !limpiar.disabled);
  await limpiar.clic();
  assert.equal(campo('cuerpo').value, 'Hola\nAna,\nnos vemos\nfin');
  assert.equal(campo('asunto').value, 'Informe Q3');
  assert.equal(limpiar.hidden, true);
  assert.ok(texto(art).includes('revisa el texto y pulsa «Guardar cambios»'));
  await boton(art, 'Guardar cambios').clic();
  assert.deepEqual(c.decisiones, [['0a1b2c3d', H, 'editar',
    { campos: { asunto: 'Informe Q3', cuerpo: 'Hola\nAna,\nnos vemos\nfin' }, version: 1 }]]);
});

test('chat/tarjetas: cuentan en MAX_MENSAJES (300) y su entrada se borra con la burbuja', () => {
  const c = cargarChat();
  c.oyentes.status('en_linea');
  c.oyentes.tarjeta(correo());
  for (let i = 0; i < 300; i += 1) c.oyentes.msg({ de: 'amadeus', texto: `m${i}` });
  assert.equal(c.lista.children.length, 300);
  assert.equal(c.aprobables().length, 0);
  // Si R.A.M.S. la vuelve a mandar, es una burbuja nueva (la vieja ya no existe).
  c.oyentes.tarjeta(correo());
  assert.equal(c.aprobables().length, 1);
});
