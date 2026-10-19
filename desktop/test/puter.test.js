'use strict';
/**
 * Respaldo Puter (§C2.15), módulos puros: lib/puter.js (validación, mapeo de mensajes,
 * clasificación de fallos, relevo WS ↔ ventana con dobles) y lib/puter-sitio.js (recursos, CSP y
 * dominios permitidos). Sin Electron, sin red y sin Puter real.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puter = require('../lib/puter');
const sitio = require('../lib/puter-sitio');

const {
  MODELO_DEFECTO, RE_MODELO, ERRORES, MAX_SYSTEM, MAX_MENSAJES, MAX_TEXTO_MENSAJE, MAX_TOKENS, MAX_EN_CURSO,
  TIMEOUT_PAGINA_MS, TIMEOUT_RELEVO_MS, MAX_TEXTO_RESPUESTA, MAX_BYTES_RESPUESTA, MAX_BYTES_WS_RESPUESTA,
  puterHabilitado, modeloDefecto, esfuerzoRazonamiento, validarPedido, aMensajesPuter, clasificarFallo,
  recortarTexto, validarEstadoVentana, crearRelevoPuter,
} = puter;

const ID = '0123456789abcdef01234567'; // como los de R.A.M.S. (secrets.token_hex(12))
const ID2 = 'fedcba9876543210fedcba98';
const ID3 = 'aaaaaaaaaaaaaaaaaaaaaaaa';
const pedido = (extra = {}) => ({
  id: ID, modelo: '', system: 'Eres Amadeus.', mensajes: [{ rol: 'usuario', texto: 'Hola' }],
  max_tokens: 512, temperatura: 0.8, ...extra,
});

// ---------------------------------------------------------------------------------------------
// Configuración y contrato
// ---------------------------------------------------------------------------------------------

test('puter: contrato — límites, errores y plazos de §C2.15', () => {
  assert.deepEqual([...ERRORES], ['sin_sesion', 'timeout', 'cuota', 'red', 'rechazado', 'otro']);
  assert.equal(MAX_SYSTEM, 60000);
  assert.equal(MAX_MENSAJES, 40);
  assert.equal(MAX_TEXTO_MENSAJE, 20000);
  assert.equal(MAX_EN_CURSO, 2);
  // la página aborta antes que el relevo y el relevo responde antes de los 45 s del contrato
  assert.ok(TIMEOUT_PAGINA_MS < TIMEOUT_RELEVO_MS && TIMEOUT_RELEVO_MS <= 45000);
  assert.ok(MAX_BYTES_RESPUESTA + 1024 <= MAX_BYTES_WS_RESPUESTA && MAX_BYTES_WS_RESPUESTA === 64 * 1024);
  // modelo por defecto verificado en developer.puter.com / docs.puter.com
  assert.equal(MODELO_DEFECTO, 'google/gemini-3.5-flash-lite');
  assert.ok(RE_MODELO.test(MODELO_DEFECTO));
});

test('puter: RE_MODELO igual que PUTER_MODEL de R.A.M.S. (letra o cifra + hasta 99 de [A-Za-z0-9_.:/-])', () => {
  for (const ok of ['gpt-5.4-nano', 'openai/gpt-6-luna:flex', 'anthropic/claude-haiku-5-5', 'x', '7', 'a'.repeat(100), 'a_b-c.d:e/f']) {
    assert.ok(RE_MODELO.test(ok), ok);
  }
  for (const mal of ['', 'a'.repeat(101), '_raro', '-x', '.x', '/x', ':x', 'con espacio', 'ñandú', 'a;b', 'modelo\n']) {
    assert.ok(!RE_MODELO.test(mal), mal);
  }
});

test('puter: RE_MODELO es literalmente el patrón de R.A.M.S. (core/config.py y escritorio_link.py)', (t) => {
  const raiz = path.join(__dirname, '..', '..');
  const fuentes = [
    [path.join(raiz, 'core', 'config.py'), /_RE_MODELO_PUTER: Final = re\.compile\(r"([^"]+)"\)/],
    [path.join(raiz, 'interface', 'escritorio_link.py'), /_MODELO_PUTER_RE: Final = re\.compile\(r"([^"]+)"\)/],
  ];
  if (!fuentes.every(([f]) => fs.existsSync(f))) {
    t.skip('Amadeus Desktop fuera del repositorio de R.A.M.S.');
    return;
  }
  // Mismo patrón salvo anclas (fullmatch en escritorio_link.py) y el escape `\-` de Python.
  const normalizar = (s) => s.replace(/^\^/, '').replace(/\$$/, '').replace(/\\-/g, '-');
  for (const [archivo, re] of fuentes) {
    const m = fs.readFileSync(archivo, 'utf8').match(re);
    assert.ok(m, `sin patrón de modelo en ${path.basename(archivo)}`);
    assert.equal(normalizar(m[1]), normalizar(RE_MODELO.source), path.basename(archivo));
  }
});

test('puter: AMADEUS_PUTER, AMADEUS_PUTER_MODELO y AMADEUS_PUTER_ESFUERZO', () => {
  assert.equal(puterHabilitado({}), true);
  assert.equal(puterHabilitado({ AMADEUS_PUTER: '1' }), true);
  for (const v of ['0', 'false', 'NO', ' off ']) assert.equal(puterHabilitado({ AMADEUS_PUTER: v }), false, v);
  assert.deepEqual(modeloDefecto({}), { modelo: MODELO_DEFECTO, aviso: null });
  assert.deepEqual(modeloDefecto({ AMADEUS_PUTER_MODELO: ' anthropic/claude-haiku-5-5 ' }),
    { modelo: 'anthropic/claude-haiku-5-5', aviso: null });
  const malo = modeloDefecto({ AMADEUS_PUTER_MODELO: 'rm -rf /; secreto' });
  assert.equal(malo.modelo, MODELO_DEFECTO);
  assert.ok(malo.aviso && !malo.aviso.includes('secreto')); // el aviso no repite el valor
  // lo que R.A.M.S. no aceptaría (y convertiría en `null` en puter.estado/llm.respuesta) tampoco vale aquí
  for (const v of ['_raro', '-x', 'a'.repeat(101)]) {
    const r = modeloDefecto({ AMADEUS_PUTER_MODELO: v });
    assert.equal(r.modelo, MODELO_DEFECTO, v);
    assert.ok(r.aviso, v);
  }
  assert.equal(esfuerzoRazonamiento({}), null);
  assert.equal(esfuerzoRazonamiento({ AMADEUS_PUTER_ESFUERZO: 'LOW' }), 'low');
  assert.equal(esfuerzoRazonamiento({ AMADEUS_PUTER_ESFUERZO: 'turbo' }), null);
});

// ---------------------------------------------------------------------------------------------
// Validación y mapeo
// ---------------------------------------------------------------------------------------------

test('puter: validarPedido acepta el pedido de R.A.M.S. y aplica defectos y topes', () => {
  const v = validarPedido(pedido({ max_tokens: 999999, temperatura: 7 }));
  assert.equal(v.ok, true);
  assert.deepEqual(v.pedido, {
    id: ID, modelo: '', system: 'Eres Amadeus.', mensajes: [{ rol: 'usuario', texto: 'Hola' }],
    maxTokens: MAX_TOKENS, temperatura: 2,
  });
  const d = validarPedido({ id: ID, mensajes: [{ rol: 'modelo', texto: 'a\u0000b' }] });
  assert.equal(d.ok, true);
  assert.equal(d.pedido.maxTokens, 1024);
  assert.equal(d.pedido.temperatura, 1);
  assert.equal(d.pedido.system, '');
  assert.equal(d.pedido.mensajes[0].texto, 'ab'); // sin NUL
});

test('puter: validarPedido rechaza lo que se sale del contrato (con el id si es válido)', () => {
  assert.deepEqual(validarPedido(null), { ok: false, id: null, motivo: 'sin datos' });
  assert.equal(validarPedido(pedido({ id: 'ABC' })).id, null);
  assert.equal(validarPedido(pedido({ id: 'abc' })).ok, false); // < 16 hex
  const casos = [
    pedido({ modelo: 'con espacio' }),
    pedido({ system: 'x'.repeat(MAX_SYSTEM + 1) }),
    pedido({ system: 42 }),
    pedido({ mensajes: 'hola' }),
    pedido({ mensajes: Array.from({ length: MAX_MENSAJES + 1 }, () => ({ rol: 'usuario', texto: 'a' })) }),
    pedido({ mensajes: [{ rol: 'system', texto: 'inyección' }] }),
    pedido({ mensajes: [{ rol: '__proto__', texto: 'x' }] }),
    pedido({ mensajes: [{ rol: 'usuario', texto: 'x'.repeat(MAX_TEXTO_MENSAJE + 1) }] }),
    pedido({ max_tokens: 1.5 }),
    pedido({ max_tokens: 0 }),
    pedido({ temperatura: 'alta' }),
    pedido({ temperatura: Number.NaN }),
  ];
  for (const c of casos) {
    const v = validarPedido(c);
    assert.equal(v.ok, false, JSON.stringify(c).slice(0, 80));
    assert.equal(v.id, ID);
  }
  // en el límite exacto sí vale
  assert.equal(validarPedido(pedido({
    system: 'x'.repeat(MAX_SYSTEM),
    mensajes: Array.from({ length: MAX_MENSAJES }, (_v, i) => ({ rol: i % 2 ? 'modelo' : 'usuario', texto: 'y'.repeat(MAX_TEXTO_MENSAJE) })),
  })).ok, true);
});

test('puter: aMensajesPuter — system primero, roles de Puter, une consecutivos y empieza por el usuario', () => {
  assert.deepEqual(aMensajesPuter('S', [
    { rol: 'usuario', texto: 'a' }, { rol: 'usuario', texto: 'b' }, { rol: 'modelo', texto: 'c' },
  ]), [
    { role: 'system', content: 'S' },
    { role: 'user', content: 'a\n\nb' },
    { role: 'assistant', content: 'c' },
  ]);
  assert.deepEqual(aMensajesPuter('', [{ rol: 'modelo', texto: 'hola' }]), [
    { role: 'user', content: '…' }, { role: 'assistant', content: 'hola' },
  ]);
  assert.deepEqual(aMensajesPuter('   ', []), [{ role: 'user', content: '…' }]);
});

test('puter: clasificarFallo → lista cerrada de errores', () => {
  const casos = [
    [{ sinSesion: true, abortado: true }, 'sin_sesion'],
    [{ abortado: true, code: 'insufficient_funds' }, 'timeout'],
    [{ code: 'request_timeout' }, 'timeout'],
    [{ status: 504 }, 'timeout'],
    [{ status: 401 }, 'sin_sesion'],
    [{ code: 'token_auth_failed' }, 'sin_sesion'],
    [{ code: 'insufficient_funds', message: 'Insufficient funds' }, 'cuota'],
    [{ status: '429' }, 'cuota'],
    [{ message: 'usage-limited' }, 'cuota'],
    [{ code: 'network_error', status: 0 }, 'red'],
    [{ message: 'Failed to fetch' }, 'red'],
    [{ code: 'content_filter' }, 'rechazado'],
    [{ status: 403 }, 'rechazado'],
    [{ code: 'respuesta_vacia' }, 'otro'],
    [{}, 'otro'],
    [null, 'otro'],
  ];
  for (const [f, esperado] of casos) assert.equal(clasificarFallo(f), esperado, JSON.stringify(f));
});

test('puter: recortarTexto — ≤ 32 000 caracteres, ≤ 60 KiB en JSON y sin partir pares sustitutos', () => {
  assert.equal(recortarTexto(42), '');
  assert.equal(recortarTexto('a\u0000b'), 'ab');
  assert.equal(recortarTexto('x'.repeat(40000)).length, MAX_TEXTO_RESPUESTA);
  const emojis = recortarTexto('😀'.repeat(20000)); // 4 bytes UTF-8 cada uno
  assert.ok(Buffer.byteLength(JSON.stringify(emojis), 'utf8') <= MAX_BYTES_RESPUESTA);
  assert.ok(emojis.length > 0 && emojis.length % 2 === 0);
  assert.doesNotThrow(() => new TextDecoder('utf-8', { fatal: true }).decode(Buffer.from(emojis, 'utf8')));
  assert.ok(!/[\ud800-\udbff]$/.test(emojis));
});

test('puter: validarEstadoVentana', () => {
  assert.equal(validarEstadoVentana(null), null);
  assert.equal(validarEstadoVentana({ cargado: 1, sesion: true }), null);
  assert.deepEqual(validarEstadoVentana({ cargado: false, sesion: true, error: 'carga_fallida' }),
    { cargado: false, sesion: false, error: 'carga_fallida' }); // sin cargar no hay sesión
  assert.deepEqual(validarEstadoVentana({ cargado: true, sesion: true, error: 'inventado' }),
    { cargado: true, sesion: true, error: null });
});

// ---------------------------------------------------------------------------------------------
// Relevo con dobles
// ---------------------------------------------------------------------------------------------

/** Relevo con WS, ventana y reloj falsos. */
function dobles(opciones = {}, { ws = true, ventana = true } = {}) {
  const r = { ws: [], ventana: [], cambios: 0, logs: [], timers: [], t: 1000, wsArriba: ws, ventanaArriba: ventana };
  const relevo = crearRelevoPuter({
    enviarWs: (obj, maxBytes) => {
      if (!r.wsArriba) return false;
      r.ws.push({ obj: JSON.parse(JSON.stringify(obj)), maxBytes });
      return true;
    },
    enviarVentana: (canal, datos) => {
      if (!r.ventanaArriba) return false;
      r.ventana.push([canal, datos]);
      return true;
    },
    temporizador: (fn, ms) => {
      const h = { fn, en: r.t + ms, vivo: true };
      r.timers.push(h);
      return () => { h.vivo = false; };
    },
    ahora: () => r.t,
    alCambiarEstado: () => { r.cambios += 1; },
    registrar: (texto) => r.logs.push(texto),
  }, opciones);
  r.avanzar = (ms) => {
    r.t += ms;
    for (const h of r.timers) {
      if (h.vivo && h.en <= r.t) {
        h.vivo = false;
        h.fn();
      }
    }
  };
  r.tipo = (tipo) => r.ws.filter((m) => m.obj.tipo === tipo).map((m) => m.obj.datos);
  return { relevo, r };
}

const listo = (relevo) => relevo.alEstadoVentana({ cargado: true, sesion: true, error: null });

test('relevo: puter.estado tras el hola y solo en cada cambio', () => {
  const { relevo, r } = dobles();
  listo(relevo); // aún sin hola: no sale nada
  assert.deepEqual(r.ws, []);
  assert.equal(relevo.alHola(), true);
  assert.deepEqual(r.tipo('puter.estado'), [{ listo: true, sesion: true, modelo: MODELO_DEFECTO, error: null }]);
  listo(relevo); // sin cambio
  assert.equal(r.tipo('puter.estado').length, 1);
  relevo.alEstadoVentana({ cargado: true, sesion: false, error: null });
  assert.deepEqual(r.tipo('puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_DEFECTO, error: null });
  relevo.alEstadoVentana({ cargado: false, sesion: false, error: 'carga_fallida' });
  assert.deepEqual(r.tipo('puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_DEFECTO, error: 'carga_fallida' });
  assert.ok(r.cambios >= 3);
  // reconexión: se vuelve a mandar tras el nuevo hola aunque no haya cambiado
  relevo.alDesconectado();
  relevo.alHola();
  assert.equal(r.tipo('puter.estado').length, 4);
  // valores basura de la ventana se ignoran
  assert.equal(relevo.alEstadoVentana({ cargado: 'sí' }), false);
});

test('relevo: llm.pedir → puter:pedir → llm.respuesta ok (≤ 64 KiB y modelo usado)', () => {
  const { relevo, r } = dobles({ modelo: 'openai/gpt-6-luna' });
  relevo.alHola();
  listo(relevo);
  assert.equal(relevo.alPedido(pedido()), true);
  assert.equal(relevo.enCurso(), 1);
  const [canal, datos] = r.ventana.at(-1);
  assert.equal(canal, 'puter:pedir');
  assert.deepEqual(datos, {
    id: ID, modelo: 'openai/gpt-6-luna',
    mensajes: [{ role: 'system', content: 'Eres Amadeus.' }, { role: 'user', content: 'Hola' }],
    maxTokens: 512, temperatura: 0.8, esfuerzo: null, timeoutMs: TIMEOUT_PAGINA_MS,
  });
  r.avanzar(1234);
  assert.equal(relevo.alRespuestaVentana({ id: ID, ok: true, texto: 'Hmph. Hola.', modelo: 'openai/gpt-6-luna' }), true);
  const resp = r.ws.find((m) => m.obj.tipo === 'llm.respuesta');
  assert.deepEqual(resp.obj.datos, { id: ID, ok: true, texto: 'Hmph. Hola.', error: null, modelo: 'openai/gpt-6-luna', ms: 1234 });
  assert.equal(resp.maxBytes, MAX_BYTES_WS_RESPUESTA);
  assert.equal(relevo.enCurso(), 0);
  // una respuesta repetida o ajena no sale dos veces
  assert.equal(relevo.alRespuestaVentana({ id: ID, ok: true, texto: 'otra' }), false);
  assert.equal(r.tipo('llm.respuesta').length, 1);
  // el modelo del pedido manda sobre el defecto del escritorio
  relevo.alPedido(pedido({ id: ID2, modelo: 'anthropic/claude-haiku-5-5' }));
  assert.equal(r.ventana.at(-1)[1].modelo, 'anthropic/claude-haiku-5-5');
  // el relevo nunca registra el contenido
  assert.ok(!r.logs.join('\n').includes('Hola') && !r.logs.join('\n').includes('Amadeus'));
});

test('relevo: sin sesión → «sin_sesion» al instante, sin tocar la ventana', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  relevo.alPedido(pedido());
  assert.deepEqual(r.ventana, []);
  assert.deepEqual(r.tipo('llm.respuesta'), [{ id: ID, ok: false, texto: null, error: 'sin_sesion', modelo: MODELO_DEFECTO, ms: 0 }]);
  // se reenvía el estado real para que R.A.M.S. se resincronice
  assert.equal(r.tipo('puter.estado').length, 2);
  assert.equal(relevo.enCurso(), 0);
});

test('relevo: pedido inválido con id → «rechazado»; sin id válido → se ignora', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  relevo.alPedido(pedido({ mensajes: [{ rol: 'system', texto: 'x' }] }));
  assert.equal(r.tipo('llm.respuesta').at(-1).error, 'rechazado');
  assert.equal(relevo.alPedido(pedido({ id: 'no-hex' })), false);
  assert.equal(r.tipo('llm.respuesta').length, 1);
  assert.deepEqual(r.ventana, []);
});

test('relevo: como mucho 2 pedidos a la vez; el 3.º es «rechazado»; duplicados ignorados', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  assert.equal(relevo.alPedido(pedido()), true);
  assert.equal(relevo.alPedido(pedido()), false); // mismo id en curso: no se duplica
  assert.equal(relevo.alPedido(pedido({ id: ID2 })), true);
  relevo.alPedido(pedido({ id: ID3 }));
  assert.deepEqual(r.tipo('llm.respuesta'), [{ id: ID3, ok: false, texto: null, error: 'rechazado', modelo: MODELO_DEFECTO, ms: 0 }]);
  assert.equal(r.ventana.filter((v) => v[0] === 'puter:pedir').length, 2);
  assert.equal(relevo.enCurso(), 2);
});

test('relevo: la ventana no contesta → «timeout» a los 42 s y puter:cancelar; la tardía se ignora', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  relevo.alPedido(pedido());
  r.avanzar(TIMEOUT_RELEVO_MS - 1);
  assert.deepEqual(r.tipo('llm.respuesta'), []);
  r.avanzar(1);
  assert.deepEqual(r.tipo('llm.respuesta'), [{ id: ID, ok: false, texto: null, error: 'timeout', modelo: MODELO_DEFECTO, ms: TIMEOUT_RELEVO_MS }]);
  assert.deepEqual(r.ventana.at(-1), ['puter:cancelar', ID]);
  assert.equal(relevo.alRespuestaVentana({ id: ID, ok: true, texto: 'tarde' }), false);
  assert.equal(r.tipo('llm.respuesta').length, 1);
});

test('relevo: fallos de la ventana clasificados; la página sin sesión apaga la sesión; vacío → «otro»', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  relevo.alPedido(pedido());
  relevo.alRespuestaVentana({ id: ID, ok: false, fallo: { code: 'insufficient_funds', status: 402 } });
  assert.equal(r.tipo('llm.respuesta').at(-1).error, 'cuota');
  relevo.alPedido(pedido({ id: ID2 }));
  relevo.alRespuestaVentana({ id: ID2, ok: true, texto: '   ' });
  assert.equal(r.tipo('llm.respuesta').at(-1).error, 'otro');
  relevo.alPedido(pedido({ id: ID3 }));
  relevo.alRespuestaVentana({ id: ID3, ok: false, fallo: { code: 'token_auth_failed', status: 401, sinSesion: true } });
  assert.equal(r.tipo('llm.respuesta').at(-1).error, 'sin_sesion');
  assert.deepEqual(r.tipo('puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_DEFECTO, error: null });
  assert.equal(relevo.estado().listo, false);
  assert.deepEqual(relevo.estadisticas(), { pedidos: 3, respuestas: 0, fallos: 3 });
  // la página ya sabía que no había sesión: no hace falta pedirle que la relea
  assert.ok(!r.ventana.some(([canal]) => canal === 'puter:refrescar'));
});

test('relevo: un 401 con la página aún con sesión → «sin_sesion», pero la sesión la decide la página', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  relevo.alPedido(pedido());
  // puter.js sigue con su token (isSignedIn() === true tras el fallo), pero la API dijo 401
  relevo.alRespuestaVentana({ id: ID, ok: false, fallo: { code: 'token_auth_failed', message: 'Unauthorized', status: 401, sinSesion: false } });
  assert.deepEqual(r.tipo('llm.respuesta').at(-1), { id: ID, ok: false, texto: null, error: 'sin_sesion', modelo: MODELO_DEFECTO, ms: 0 });
  // el relevo no se inventa «sin sesión»: sigue con lo que dijo la página y se lo reenvía a
  // R.A.M.S. (que al ver «sin_sesion» se marca sin sesión hasta el siguiente puter.estado)…
  assert.equal(relevo.estado().listo, true);
  assert.deepEqual(r.tipo('puter.estado').at(-1), { listo: true, sesion: true, modelo: MODELO_DEFECTO, error: null });
  // …y le pide a la página que relea la sesión y vuelva a publicar su estado
  assert.deepEqual(r.ventana.at(-1), ['puter:refrescar', null]);
  // el siguiente pedido llega a la página (no se responde «sin_sesion» a ciegas)
  relevo.alPedido(pedido({ id: ID2 }));
  assert.equal(r.ventana.at(-1)[0], 'puter:pedir');
  // si la página relee y ya no hay sesión (puter.js borró el token), el estado baja de verdad
  relevo.alEstadoVentana({ cargado: true, sesion: false, error: null });
  assert.deepEqual(r.tipo('puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_DEFECTO, error: null });
  assert.equal(relevo.enCurso(), 1);
});

test('relevo: respuesta enorme recortada a ≤ 60 KiB; ventana caída → «otro» y estado de error', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  relevo.alPedido(pedido());
  relevo.alRespuestaVentana({ id: ID, ok: true, texto: 'ñ'.repeat(100000) });
  const resp = r.tipo('llm.respuesta').at(-1);
  assert.equal(resp.ok, true);
  assert.ok(Buffer.byteLength(JSON.stringify({ tipo: 'llm.respuesta', datos: resp }), 'utf8') <= MAX_BYTES_WS_RESPUESTA);
  relevo.alPedido(pedido({ id: ID2 }));
  relevo.alReinicioVentana('otro', 'ventana_caida');
  assert.equal(r.tipo('llm.respuesta').at(-1).error, 'otro');
  assert.deepEqual(r.tipo('puter.estado').at(-1), { listo: false, sesion: false, modelo: MODELO_DEFECTO, error: 'ventana_caida' });
  assert.equal(relevo.enCurso(), 0);
});

test('relevo: la ventana no está lista → «otro» al instante; WS caído → cancela en la ventana', () => {
  const { relevo, r } = dobles();
  relevo.alHola();
  listo(relevo);
  r.ventanaArriba = false;
  relevo.alPedido(pedido());
  assert.equal(r.tipo('llm.respuesta').at(-1).error, 'otro');
  r.ventanaArriba = true;
  relevo.alPedido(pedido({ id: ID2 }));
  relevo.alDesconectado();
  assert.deepEqual(r.ventana.at(-1), ['puter:cancelar', ID2]);
  assert.equal(relevo.enCurso(), 0);
  r.avanzar(TIMEOUT_RELEVO_MS); // su temporizador quedó cancelado
  assert.equal(r.tipo('llm.respuesta').length, 1);
});

test('relevo: AMADEUS_PUTER=0 → estado «deshabilitado» y todo pedido «rechazado»', () => {
  const { relevo, r } = dobles({ habilitado: false });
  relevo.alHola();
  assert.deepEqual(r.tipo('puter.estado'), [{ listo: false, sesion: false, modelo: null, error: 'deshabilitado' }]);
  assert.equal(relevo.alEstadoVentana({ cargado: true, sesion: true }), false);
  relevo.alPedido(pedido());
  assert.deepEqual(r.tipo('llm.respuesta'), [{ id: ID, ok: false, texto: null, error: 'rechazado', modelo: null, ms: 0 }]);
  assert.deepEqual(r.ventana, []);
});

test('relevo: esfuerzo de razonamiento opcional y modelo inválido → defecto', () => {
  const { relevo, r } = dobles({ esfuerzo: 'low', modelo: 'no vale' });
  assert.equal(relevo.modelo, MODELO_DEFECTO);
  relevo.alHola();
  listo(relevo);
  relevo.alPedido(pedido());
  assert.equal(r.ventana.at(-1)[1].esfuerzo, 'low');
});

// ---------------------------------------------------------------------------------------------
// lib/puter-sitio.js
// ---------------------------------------------------------------------------------------------

test('puter-sitio: solo se sirven los recursos de la lista, por GET/HEAD y en el origen propio', () => {
  assert.equal(sitio.URL_TRABAJO, 'http://amadeus-puter.localhost/trabajo.html');
  assert.equal(sitio.URL_CONECTAR, 'http://amadeus-puter.localhost/conectar.html');
  assert.deepEqual(sitio.resolverRecurso(sitio.URL_TRABAJO), { archivo: 'trabajo.html', tipo: 'text/html; charset=utf-8' });
  assert.deepEqual(sitio.resolverRecurso('http://amadeus-puter.localhost/trabajo.js?v=1', 'HEAD'),
    { archivo: 'trabajo.js', tipo: 'text/javascript; charset=utf-8' });
  for (const url of [
    'http://amadeus-puter.localhost/', 'http://amadeus-puter.localhost/../main.js', 'http://amadeus-puter.localhost/%2e%2e/main.js',
    'http://amadeus-puter.localhost:8080/trabajo.html', 'http://u:p@amadeus-puter.localhost/trabajo.html',
    'https://amadeus-puter.localhost/trabajo.html', 'http://evil.localhost/trabajo.html', 'http://amadeus-puter.localhost/constructor',
    'no es url',
  ]) assert.equal(sitio.resolverRecurso(url), null, url);
  assert.equal(sitio.resolverRecurso(sitio.URL_TRABAJO, 'POST'), null);
  // todo lo publicado existe en desktop/puter/
  for (const [archivo] of Object.values(sitio.RECURSOS)) {
    assert.ok(fs.existsSync(path.join(__dirname, '..', 'puter', archivo)), archivo);
  }
});

test('puter-sitio: CSP — scripts solo propios y de js.puter.com; conexiones solo a Puter; sin marcos', () => {
  const csp = sitio.CSP_PUTER;
  const directiva = (n) => csp.split(';').map((d) => d.trim()).find((d) => d.startsWith(`${n} `));
  assert.equal(directiva('default-src'), "default-src 'none'");
  assert.equal(directiva('script-src'), "script-src 'self' https://js.puter.com");
  assert.ok(!/unsafe-eval/.test(csp));
  assert.ok(!/script-src[^;]*unsafe-inline/.test(csp));
  const conectar = directiva('connect-src').split(/\s+/).slice(1);
  for (const o of conectar) assert.ok(o === "'self'" || /^(https|wss):\/\/([a-z]+\.)?puter\.com$/.test(o), o);
  for (const d of ['frame-src', 'object-src', 'base-uri', 'form-action', 'frame-ancestors', 'worker-src']) {
    assert.equal(directiva(d), `${d} 'none'`);
  }
  const cab = sitio.cabecerasPuter('text/html; charset=utf-8');
  assert.equal(cab['Content-Security-Policy'], csp);
  assert.equal(cab['X-Content-Type-Options'], 'nosniff');
  assert.equal(cab['Cache-Control'], 'no-store');
});

test('puter-sitio: window.open y navegación del inicio de sesión solo a https://puter.com y subdominios', () => {
  for (const ok of ['https://puter.com/action/sign-in?embedded_in_popup=true&msg_id=1', 'https://api.puter.com/x',
    'https://PUTER.com/', 'https://puter.com:443/']) {
    assert.equal(sitio.esUrlPuterPermitida(ok), true, ok);
  }
  for (const mal of ['http://puter.com/', 'https://puter.com.evil.com/', 'https://evilputer.com/', 'https://u:p@puter.com/',
    'https://puter.com:8443/', 'javascript:alert(1)', 'about:blank', 'file:///C:/', `https://puter.com/${'a'.repeat(2100)}`, 42]) {
    assert.equal(sitio.esUrlPuterPermitida(mal), false, String(mal).slice(0, 60));
  }
  assert.equal(sitio.esPaginaPuter(sitio.URL_TRABAJO, 'trabajo.html'), true);
  assert.equal(sitio.esPaginaPuter(`${sitio.URL_TRABAJO}?x`, 'trabajo.html'), false);
  assert.equal(sitio.esPaginaPuter(sitio.URL_CONECTAR, 'trabajo.html'), false);
});
