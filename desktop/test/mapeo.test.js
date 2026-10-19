'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  crearEscritorio, validarConfirmacion, validarUrlHttps, datosEscalares, textoSeguro, UrlsOfrecidas,
  ESPERA_SALIDA_MS, ESPERA_CAJA_MS, ESPERA_APARECER_MS, INTERVALO_CURSOR_ENCIMA_MS,
  validarTarjeta, validarTarjetaCerrada, validarDecisionTarjeta,
  MAX_CODIGO, MAX_CUERPO, MAX_SALIDA, MAX_HALLAZGOS, MAX_TARJETAS, MAX_BYTES_DECISION, RE_HUELLA,
  CLASES_TARJETA, BOTONES_TARJETA, DECISIONES_TARJETA, NIVELES_RIESGO,
} = require('../lib/mapeo');

/**
 * Banco de pruebas: puertos falsos que registran cada llamada en orden, con reloj y temporizadores
 * manuales. `wsAbierto` decide si `enviarWs` "sale".
 */
function banco({ listo = true, wsAbierto = true } = {}) {
  const llamadas = [];
  const reg = (nombre) => (...args) => {
    llamadas.push([nombre, ...args]);
  };
  const reloj = { t: 1_000_000 };
  const timers = [];
  const ventana = (prefijo, extra = {}) => ({
    showInactive: reg(`${prefijo}.showInactive`),
    show: reg(`${prefijo}.show`),
    focus: reg(`${prefijo}.focus`),
    hide: reg(`${prefijo}.hide`),
    ...extra,
  });
  const estadoChat = { visible: false, enfocado: false };
  const b = {
    llamadas,
    envios: [], // {obj, maxBytes} de cada enviarWs
    reloj,
    estadoChat,
    listo,
    wsAbierto,
    ws: () => llamadas.filter((l) => l[0] === 'ws').map((l) => l[1]),
    ipc: (canal) => llamadas.filter((l) => (l[0] === 'chat' || l[0] === 'overlay') && l[1] === canal).map((l) => l[2]),
    nombres: () => llamadas.map((l) => l[0]),
    avanzar(ms) {
      reloj.t += ms;
      for (const tm of timers) {
        if (!tm.hecho && tm.vence <= reloj.t) {
          tm.hecho = true;
          tm.fn();
        }
      }
    },
  };
  const puertos = {
    overlay: ventana('overlay'),
    chat: ventana('chat', {
      isVisible: () => estadoChat.visible,
      isFocused: () => estadoChat.enfocado,
    }),
    overlayListo: () => b.listo,
    enviarOverlay: (canal, datos) => llamadas.push(['overlay', canal, datos]),
    enviarChat: (canal, datos) => llamadas.push(['chat', canal, datos]),
    enviarWs: (obj, maxBytes) => {
      b.envios.push({ obj, maxBytes }); // también los que no salen: se comprueba el tope pedido
      if (!b.wsAbierto) return false;
      llamadas.push(['ws', obj]);
      return true;
    },
    forzarClickThrough: reg('forzarClickThrough'),
    notificar: reg('notificar'),
    abrirExterno: reg('abrirExterno'),
    temporizador: (fn, ms) => {
      const tm = { fn, vence: reloj.t + ms, hecho: false };
      timers.push(tm);
      return () => {
        tm.hecho = true;
      };
    },
    ahora: () => reloj.t,
    bordePreferido: () => 'left',
    alCambiarVisibilidad: reg('alCambiarVisibilidad'),
    fijarQuieta: reg('fijarQuieta'),
    fijarNoMolestar: reg('fijarNoMolestar'),
  };
  b.esc = crearEscritorio(puertos, { pid: 4321, version: '0.2.0' });
  return b;
}

// ------------------------------------------------------------------ WS → IPC del chat

test('mapeo: hola → chat:status en_linea', () => {
  const b = banco();
  assert.equal(b.esc.alMensajeWs({ tipo: 'hola', datos: { version: '0.2', nombre_ia: 'Amadeus', usuario: 'John' } }), true);
  assert.deepEqual(b.ipc('chat:status'), ['en_linea']);
  assert.equal(b.esc.estado().enLinea, true);
});

test('mapeo: subtitulo → chat:msg{de:amadeus}; sistema → chat:msg{de:sistema}', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'subtitulo', datos: { texto: 'Hmph. No es que me importe.', exp: 'tsun', canal: 'terminal', origen: 'broca' } });
  b.esc.alMensajeWs({ tipo: 'sistema', datos: { texto: 'Estado: todo en orden.' } });
  assert.deepEqual(b.ipc('chat:msg'), [
    { de: 'amadeus', texto: 'Hmph. No es que me importe.' },
    { de: 'sistema', texto: 'Estado: todo en orden.' },
  ]);
  // vacíos y basura no llegan al chat
  assert.equal(b.esc.alMensajeWs({ tipo: 'sistema', datos: { texto: '   ' } }), false);
  assert.equal(b.esc.alMensajeWs({ tipo: 'subtitulo', datos: { texto: 42 } }), false);
  assert.equal(b.esc.alMensajeWs({ tipo: 'subtitulo', datos: [1, 2] }), false);
  assert.equal(b.esc.alMensajeWs(null), false);
  assert.equal(b.esc.alMensajeWs({ tipo: 'desconocido', datos: {} }), false);
  assert.equal(b.ipc('chat:msg').length, 2);
});

test('mapeo: el texto se limita a 4000 caracteres y sin caracteres de control', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'sistema', datos: { texto: `a\u0000b\u0007c\n${'x'.repeat(5000)}` } });
  const [m] = b.ipc('chat:msg');
  assert.equal(m.texto.length, 4000);
  assert.ok(m.texto.startsWith('abc\n'));
});

test('mapeo: voz → chat:voz (booleano estricto)', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'voz', datos: { hablando: true } });
  b.esc.alMensajeWs({ tipo: 'voz', datos: { hablando: 'sí' } });
  assert.deepEqual(b.ipc('chat:voz'), [true, false]);
});

test('mapeo: permiso → chat:permiso (con vista previa) y resolución', () => {
  const b = banco();
  b.esc.alMensajeWs({
    tipo: 'permiso',
    datos: { id: '0a1b2c3d', descripcion: 'Abrir WhatsApp con un borrador de 12 caracteres (no se enviará)', vista_previa: 'hola mamá :)', destructiva: false, pendiente: true },
  });
  b.esc.alMensajeWs({ tipo: 'permiso', datos: { id: '0a1b2c3d', descripcion: 'x', destructiva: false, pendiente: false, aprobado: true } });
  assert.deepEqual(b.ipc('chat:permiso'), [
    { id: '0a1b2c3d', descripcion: 'Abrir WhatsApp con un borrador de 12 caracteres (no se enviará)', vista_previa: 'hola mamá :)', pendiente: true },
    { id: '0a1b2c3d', descripcion: 'x', pendiente: false, aprobado: true },
  ]);
});

test('mapeo: permiso con id inválido se descarta', () => {
  const b = banco();
  for (const id of ['0A1B2C3D', '0a1b2c3', '0a1b2c3d9', 'zzzzzzzz', 12345678, null]) {
    assert.equal(b.esc.alMensajeWs({ tipo: 'permiso', datos: { id, descripcion: 'x', pendiente: true } }), false);
  }
  assert.equal(b.ipc('chat:permiso').length, 0);
});

// ------------------------------------------------------------------ órdenes y visibilidad

test('mapeo: orden{abrir_chat} → chat.showInactive() y NUNCA focus()', () => {
  const b = banco();
  assert.equal(b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'abrir_chat' } }), true);
  const n = b.nombres();
  assert.deepEqual(n.filter((x) => x.startsWith('chat.')), ['chat.showInactive']);
  assert.ok(!n.includes('chat.focus'));
  assert.ok(!n.includes('chat.show'));
  assert.equal(b.ws().length, 0); // chat_abierto solo cuando lo abre el usuario
});

test('mapeo: abrir el chat por acción del usuario sí enfoca y avisa chat_abierto', () => {
  const b = banco();
  b.esc.abrirChatUsuario();
  assert.deepEqual(b.nombres().filter((x) => x.startsWith('chat.')), ['chat.show', 'chat.focus']);
  assert.deepEqual(b.ws(), [{ tipo: 'evento', nombre: 'chat_abierto', datos: {} }]);
  // con alternar y el chat visible y enfocado: se oculta
  b.estadoChat.visible = true;
  b.estadoChat.enfocado = true;
  b.esc.abrirChatUsuario({ alternar: true });
  assert.equal(b.nombres().at(-1), 'chat.hide');
});

test('mapeo: orden{aparecer} → showInactive + visibility(true) + order + WS mostrada, en ese orden', () => {
  const b = banco();
  assert.equal(b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'aparecer', borde: 'right', modo: 'asomarse' } }), true);
  assert.deepEqual(b.llamadas.slice(0, 5), [
    ['overlay.showInactive'],
    ['overlay', 'overlay:visibility', true],
    ['overlay', 'overlay:order', { accion: 'aparecer', borde: 'right', modo: 'asomarse' }],
    ['ws', { tipo: 'evento', nombre: 'mostrada', datos: {} }],
    ['alCambiarVisibilidad', true],
  ]);
  assert.ok(!b.nombres().includes('overlay.focus') && !b.nombres().includes('overlay.show'));
  assert.equal(b.esc.estado().visible, true);
});

test('mapeo: aparecer sin borde/modo válidos usa el borde preferido y "entrar"', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'aparecer', borde: 'arriba', modo: 'volar' } });
  assert.deepEqual(b.ipc('overlay:order')[0], { accion: 'aparecer', borde: 'left', modo: 'entrar' });
});

test('mapeo: aparecer estando ya fuera solo resincroniza (no vuelve a entrar)', () => {
  const b = banco();
  b.esc.aparecer({});
  const n = b.llamadas.length;
  b.esc.aparecer({});
  assert.deepEqual(b.llamadas.slice(n), [['ws', { tipo: 'evento', nombre: 'mostrada', datos: {} }]]);
});

test('mapeo: aparecer con el visor sin cargar queda pendiente hasta que carga', () => {
  const b = banco({ listo: false });
  assert.equal(b.esc.aparecer({ borde: 'right' }), false);
  assert.ok(!b.nombres().includes('overlay.showInactive'));
  assert.equal(b.ipc('chat:msg')[0].de, 'sistema');
  b.listo = true;
  b.reloj.t += 1000;
  assert.equal(b.esc.alOverlayListo(), true);
  assert.ok(b.nombres().includes('overlay.showInactive'));
  assert.deepEqual(b.ipc('overlay:order').at(-1), { accion: 'aparecer', borde: 'right', modo: 'entrar' });
});

test('mapeo: un aparecer pendiente caduca', () => {
  const b = banco({ listo: false });
  b.esc.aparecer({});
  b.listo = true;
  b.reloj.t += ESPERA_APARECER_MS + 1;
  assert.equal(b.esc.alOverlayListo(), false);
  assert.ok(!b.nombres().includes('overlay.showInactive'));
  assert.deepEqual(b.ipc('overlay:visibility'), [false]);
});

test('mapeo: orden{esconderse} → overlay:order{esconderse}; overlay:hidden → hide + visibility(false) + WS ocultada', () => {
  const b = banco();
  b.esc.aparecer({});
  b.llamadas.length = 0;
  assert.equal(b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'esconderse' } }), true);
  assert.deepEqual(b.llamadas, [['overlay', 'overlay:order', { accion: 'esconderse' }]]);
  assert.equal(b.esc.estado().visible, true); // sigue visible mientras sale caminando
  b.llamadas.length = 0;
  assert.equal(b.esc.alHidden(), true);
  assert.deepEqual(b.llamadas, [
    ['overlay.hide'],
    ['forzarClickThrough', 'renderer'],
    ['overlay', 'overlay:visibility', false],
    ['ws', { tipo: 'evento', nombre: 'ocultada', datos: {} }],
    ['alCambiarVisibilidad', false],
  ]);
  assert.equal(b.esc.estado().visible, false);
});

test('mapeo: un overlay:hidden no esperado se ignora', () => {
  const b = banco();
  assert.equal(b.esc.alHidden(), false); // oculta
  b.esc.aparecer({});
  assert.equal(b.esc.alHidden(), false); // visible pero sin salida en curso
  assert.ok(!b.nombres().includes('overlay.hide'));
});

test('mapeo: aparecer durante la salida la cancela (y un hidden tardío se ignora)', () => {
  const b = banco();
  b.esc.aparecer({});
  b.esc.esconderse();
  b.esc.aparecer({ borde: 'right' });
  assert.equal(b.esc.estado().saliendo, false);
  assert.equal(b.esc.alHidden(), false);
  b.avanzar(ESPERA_SALIDA_MS + 1);
  assert.ok(!b.nombres().includes('overlay.hide'));
});

test('mapeo: si el renderer no confirma la salida, se oculta igual', () => {
  const b = banco();
  b.esc.aparecer({});
  b.esc.esconderse();
  b.avanzar(ESPERA_SALIDA_MS - 1);
  assert.ok(!b.nombres().includes('overlay.hide'));
  b.avanzar(2);
  assert.ok(b.nombres().includes('overlay.hide'));
  assert.equal(b.esc.estado().visible, false);
});

test('mapeo: sin caja tras aparecer, la orden se reenvía UNA vez', () => {
  const b = banco();
  b.esc.aparecer({ borde: 'left' });
  b.avanzar(ESPERA_CAJA_MS + 1);
  assert.equal(b.ipc('overlay:order').filter((o) => o.accion === 'aparecer').length, 2);
  b.avanzar(ESPERA_CAJA_MS * 3);
  assert.equal(b.ipc('overlay:order').filter((o) => o.accion === 'aparecer').length, 2);
  // con caja recibida no hay reenvío
  const c = banco();
  c.esc.aparecer({});
  c.esc.alCajaRecibida();
  c.avanzar(ESPERA_CAJA_MS + 1);
  assert.equal(c.ipc('overlay:order').filter((o) => o.accion === 'aparecer').length, 1);
});

test('mapeo: recarga del visor estando fuera → vuelve a sacarla', () => {
  const b = banco();
  b.esc.aparecer({});
  b.llamadas.length = 0;
  b.esc.alOverlayListo();
  assert.deepEqual(b.ipc('overlay:visibility'), [true]);
  assert.equal(b.ipc('overlay:order')[0].accion, 'aparecer');
});

test('mapeo: emergencia oculta ya y fuerza el click-through (aunque esté oculta)', () => {
  const b = banco();
  b.esc.emergencia();
  assert.deepEqual(b.llamadas, [['forzarClickThrough', 'emergencia']]);
  b.esc.aparecer({});
  b.llamadas.length = 0;
  b.esc.emergencia();
  assert.deepEqual(b.nombres().slice(0, 2), ['overlay.hide', 'forzarClickThrough']);
  assert.equal(b.esc.estado().visible, false);
});

test('mapeo: alternar aparece y esconde', () => {
  const b = banco();
  b.esc.alternar();
  assert.equal(b.esc.estado().visible, true);
  b.esc.alternar();
  assert.equal(b.esc.estado().saliendo, true);
});

test('mapeo: caída del renderer con la ventana fuera → oculta y click-through', () => {
  const b = banco();
  b.esc.aparecer({});
  b.esc.alOverlayCaido();
  assert.ok(b.nombres().includes('overlay.hide'));
  assert.ok(b.llamadas.some((l) => l[0] === 'forzarClickThrough' && l[1] === 'caida'));
});

test('mapeo: notificar → Notification + chat:msg con url; la url queda ofrecida', () => {
  const b = banco();
  const url = 'https://es.wikipedia.org/wiki/Neurona';
  b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'notificar', texto: '¿Sabías esto?', url } });
  assert.deepEqual(b.llamadas.find((l) => l[0] === 'notificar'), ['notificar', { titulo: 'Amadeus', texto: '¿Sabías esto?', url }]);
  assert.deepEqual(b.ipc('chat:msg'), [{ de: 'amadeus', texto: '¿Sabías esto?', url }]);
  assert.equal(b.esc.alAbrirEnlace(url), true);
  assert.deepEqual(b.llamadas.at(-1), ['abrirExterno', url]);
});

test('mapeo: notificar con url no https la descarta', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'notificar', texto: 'mira', url: 'http://inseguro.example' } });
  assert.deepEqual(b.ipc('chat:msg'), [{ de: 'amadeus', texto: 'mira' }]);
  assert.equal(b.esc.alAbrirEnlace('http://inseguro.example'), false);
});

test('mapeo: abrirEnlace solo abre URLs https ofrecidas antes por R.A.M.S.', () => {
  const b = banco();
  assert.equal(b.esc.alAbrirEnlace('https://no-ofrecida.example/'), false);
  b.esc.alMensajeWs({ tipo: 'orden', datos: { accion: 'notificar', texto: 't', url: 'https://ok.example/a' } });
  assert.equal(b.esc.alAbrirEnlace('https://ok.example/a'), true);
  assert.equal(b.esc.alAbrirEnlace('https://ok.example/a?x=1'), false);
  assert.equal(b.esc.alAbrirEnlace('javascript:alert(1)'), false);
  assert.equal(b.esc.alAbrirEnlace({ toString: () => 'https://ok.example/a' }), false);
  assert.equal(b.llamadas.filter((l) => l[0] === 'abrirExterno').length, 1);
});

test('mapeo: orden desconocida o con acción de cuerpo (show/enter/walk_to) se ignora', () => {
  const b = banco();
  for (const accion of ['show', 'hide', 'enter', 'leave', 'walk_to', 'estado', undefined]) {
    assert.equal(b.esc.alMensajeWs({ tipo: 'orden', datos: { accion } }), false);
  }
  assert.ok(!b.nombres().includes('overlay.showInactive'));
});

test('mapeo: estado → overlay:order{estado} acotado', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'estado', datos: { aburrimiento: 1.7, fatiga: 0.3, valencia: -4, extra: 'x' } });
  assert.deepEqual(b.ipc('overlay:order'), [{ accion: 'estado', aburrimiento: 1, fatiga: 0.3, valencia: -1 }]);
  const c = banco({ listo: false });
  assert.equal(c.esc.alMensajeWs({ tipo: 'estado', datos: { fatiga: 0.5 } }), false);
});

test('mapeo: travesuras → casilla Quieta de la bandeja', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'travesuras', datos: { activas: false, hasta_ts: null } });
  b.esc.alMensajeWs({ tipo: 'travesuras', datos: { activas: true, hasta_ts: null } });
  assert.deepEqual(b.llamadas.filter((l) => l[0] === 'fijarQuieta'), [['fijarQuieta', true], ['fijarQuieta', false]]);
});

// ------------------------------------------------------------------ app → WS

test('mapeo: chat:confirm válido → WS {tipo:confirmacion}', () => {
  const b = banco();
  assert.equal(b.esc.alConfirmar('deadbeef', true), true);
  assert.equal(b.esc.alConfirmar('01234567', false), true);
  assert.deepEqual(b.ws(), [
    { tipo: 'confirmacion', id: 'deadbeef', aprobado: true },
    { tipo: 'confirmacion', id: '01234567', aprobado: false },
  ]);
});

test('mapeo: chat:confirm inválido (id o booleano) → nada', () => {
  const b = banco();
  const casos = [
    ['DEADBEEF', true], ['deadbee', true], ['deadbeef0', true], ['dead-eef', true], [12345678, true],
    [null, true], ['deadbeef', 'true'], ['deadbeef', 1], ['deadbeef', null], ['deadbeef', undefined],
  ];
  for (const [id, ok] of casos) assert.equal(b.esc.alConfirmar(id, ok), false, `${id}/${ok}`);
  assert.equal(b.ws().length, 0);
  assert.equal(validarConfirmacion('abcdef01', true).aprobado, true);
});

test('mapeo: chat:send recorta, rechaza vacíos y limita a 2000', () => {
  const b = banco();
  assert.equal(b.esc.alChatSend('  hola Kurisu  '), true);
  assert.equal(b.esc.alChatSend('   '), false);
  assert.equal(b.esc.alChatSend(42), false);
  b.esc.alChatSend('x'.repeat(2500));
  assert.deepEqual(b.ws()[0], { tipo: 'chat', texto: 'hola Kurisu' });
  assert.equal(b.ws()[1].texto.length, 2000);
  const c = banco({ wsAbierto: false });
  assert.equal(c.esc.alChatSend('hola'), false); // desconectada: el chat lo avisa
});

test('mapeo: controles del chat: lista cerrada (sin pausar_vision)', () => {
  const b = banco();
  assert.equal(b.esc.control('pausar_vision', 'chat'), false);
  assert.equal(b.esc.control('apagar_pc', 'chat'), false);
  assert.equal(b.esc.control('no_molestar_off', 'chat'), true);
  assert.deepEqual(b.ws(), [{ tipo: 'control', accion: 'no_molestar_off' }]);
  // desde la bandeja/atajos sí
  assert.equal(b.esc.control('pausar_vision', 'local'), true);
  assert.deepEqual(b.ws().at(-1), { tipo: 'control', accion: 'pausar_vision' });
});

test('mapeo: basta_travesuras (Ctrl+Alt+Shift+Q) → control + esconderse', () => {
  const b = banco();
  b.esc.aparecer({});
  b.esc.control('basta_travesuras', 'local');
  assert.deepEqual(b.ws().at(-1), { tipo: 'control', accion: 'basta_travesuras' });
  assert.deepEqual(b.ipc('overlay:order').at(-1), { accion: 'esconderse' });
  assert.equal(b.ipc('chat:msg').at(-1).de, 'sistema');
});

test('mapeo: no molestar cambiado SIN conexión se comunica al reconectar (on y off)', () => {
  const b = banco({ wsAbierto: false });
  b.esc.control('no_molestar_on', 'local');
  assert.equal(b.esc.estado().noMolestar, true);
  b.wsAbierto = true;
  b.esc.alConectado();
  assert.deepEqual(b.ws(), [
    { tipo: 'hola_app', pid: 4321, version: '0.2.0' },
    { tipo: 'evento', nombre: 'ocultada', datos: {} },
    { tipo: 'control', accion: 'no_molestar_on' },
  ]);
  // apagado estando desconectada → se avisa el "off" al reconectar
  b.wsAbierto = false;
  b.esc.alDesconectado();
  b.esc.control('no_molestar_off', 'local');
  b.wsAbierto = true;
  b.llamadas.length = 0;
  b.esc.alConectado();
  assert.deepEqual(b.ws().at(-1), { tipo: 'control', accion: 'no_molestar_off' });
  // ya comunicado: la siguiente reconexión no repite nada
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  b.esc.alConectado();
  assert.ok(!b.ws().some((m) => m.tipo === 'control'));
});

test('mapeo: no_molestar de R.A.M.S. manda en la casilla y NO se reafirma al reconectar', () => {
  const b = banco();
  b.esc.control('no_molestar_on', 'local'); // enviado con conexión
  assert.equal(b.esc.estado().noMolestar, true);
  // el usuario lo quita desde el panel RINE del HUD
  assert.equal(b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: false, motivo: '', usuario: false } }), true);
  assert.equal(b.esc.estado().noMolestar, false);
  assert.deepEqual(b.llamadas.filter((l) => l[0] === 'fijarNoMolestar').at(-1), ['fijarNoMolestar', false]);
  // reconexión: nada de volver a pausar la proactividad 24 h
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  b.esc.alConectado();
  assert.deepEqual(b.ws(), [
    { tipo: 'hola_app', pid: 4321, version: '0.2.0' },
    { tipo: 'evento', nombre: 'ocultada', datos: {} },
  ]);
  // el automático del sistema no marca la casilla; el del usuario sí (también desde el HUD)
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: true, motivo: 'pantalla_completa', usuario: false } });
  assert.equal(b.esc.estado().noMolestar, false);
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: true, motivo: 'usuario', usuario: true } });
  assert.equal(b.esc.estado().noMolestar, true);
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: true, motivo: 'usuario', usuario: 'true' } });
  assert.equal(b.esc.estado().noMolestar, false); // booleano estricto
});

test('mapeo: un no_molestar de R.A.M.S. descarta un cambio local pendiente ya superado', () => {
  const b = banco({ wsAbierto: false });
  b.esc.control('no_molestar_on', 'local'); // sin conexión: pendiente
  b.wsAbierto = true;
  b.esc.alConectado(); // se comunica
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: true, motivo: 'usuario', usuario: true } });
  assert.equal(b.esc.estado().noMolestar, true);
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  b.esc.alConectado();
  assert.ok(!b.ws().some((m) => m.tipo === 'control'));
});

// ------------------------------------------------------------------ confirmaciones al reconectar

const PEND = (id) => ({ tipo: 'permiso', datos: { id, descripcion: 'Abrir WhatsApp', destructiva: false, pendiente: true } });

test('mapeo: reconexión — la pendiente que vuelve en la instantánea sigue vigente', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(PEND('deadbeef'));
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  // instantánea: hola, permiso pendiente, travesuras, no_molestar
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(PEND('deadbeef'));
  b.esc.alMensajeWs({ tipo: 'travesuras', datos: { activas: false, hasta_ts: null } });
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: false, motivo: '', usuario: false } });
  assert.deepEqual(b.ipc('chat:permiso'), [{ id: 'deadbeef', descripcion: 'Abrir WhatsApp', pendiente: true }]);
  b.avanzar(10_000);
  assert.equal(b.ipc('chat:permiso').length, 1); // nada caduca después
});

test('mapeo: reconexión — la que ya no está en la instantánea se marca caducada', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(PEND('deadbeef'));
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  assert.equal(b.ipc('chat:permiso').length, 0); // aún en revisión
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: false, motivo: '', usuario: false } });
  assert.deepEqual(b.ipc('chat:permiso'), [{ id: 'deadbeef', pendiente: false, caducada: true }]);
  b.esc.alMensajeWs({ tipo: 'estado', datos: { aburrimiento: 0.1 } });
  assert.equal(b.ipc('chat:permiso').length, 1); // una sola vez
});

test('mapeo: la revisión se cierra sola si no llega nada más; sin conexión espera', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(PEND('0a1b2c3d'));
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alDesconectado(); // se cae otra vez antes de la instantánea: no caduca nada
  b.avanzar(60_000);
  assert.equal(b.ipc('chat:permiso').length, 0);
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.avanzar(2_999);
  assert.equal(b.ipc('chat:permiso').length, 0);
  b.avanzar(1);
  assert.deepEqual(b.ipc('chat:permiso'), [{ id: '0a1b2c3d', pendiente: false, caducada: true }]);
});

test('mapeo: una resuelta no entra en la revisión; la pendiente se reenvía si el chat se recarga', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(PEND('deadbeef'));
  b.llamadas.length = 0;
  b.esc.alChatCargado();
  assert.deepEqual(b.ipc('chat:permiso'), [{ id: 'deadbeef', descripcion: 'Abrir WhatsApp', pendiente: true }]);
  assert.deepEqual(b.llamadas.map((l) => l[1]).slice(-2), ['chat:permiso', 'chat:status']); // permiso antes que el estado
  b.esc.alMensajeWs({ tipo: 'permiso', datos: { id: 'deadbeef', descripcion: 'x', pendiente: false, aprobado: true } });
  b.llamadas.length = 0;
  b.esc.alChatCargado();
  assert.equal(b.ipc('chat:permiso').length, 0);
  b.esc.alDesconectado();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs({ tipo: 'no_molestar', datos: { activo: false, motivo: '', usuario: false } });
  assert.equal(b.ipc('chat:permiso').length, 0);
});

// ------------------------------------------------------------------ cámara (§C2.13)

test('mapeo: vision{camara} → estado, bandeja y overlay:order{camara}; se repite al recargar el visor', () => {
  const b = banco();
  assert.equal(b.esc.estado().camara, false);
  assert.equal(b.esc.alMensajeWs({ tipo: 'vision', datos: { camara: true } }), true);
  assert.equal(b.esc.estado().camara, true);
  assert.deepEqual(b.ipc('overlay:order').at(-1), { accion: 'camara', activa: true });
  const n = b.ipc('overlay:order').length;
  b.esc.alMensajeWs({ tipo: 'vision', datos: { camara: true } }); // sin cambio: nada
  assert.equal(b.ipc('overlay:order').length, n);
  assert.equal(b.esc.alMensajeWs({ tipo: 'vision', datos: { camara: 'sí' } }), false);
  assert.equal(b.esc.estado().camara, true);
  b.esc.alOverlayListo();
  assert.deepEqual(b.ipc('overlay:order').filter((o) => o.accion === 'camara').at(-1), { accion: 'camara', activa: true });
  b.esc.alDesconectado(); // sin conexión se conserva (mejor un aviso de más que uno de menos)
  assert.equal(b.esc.estado().camara, true);
  b.esc.alMensajeWs({ tipo: 'vision', datos: { camara: false } });
  assert.deepEqual(b.ipc('overlay:order').at(-1), { accion: 'camara', activa: false });
  // con el visor sin cargar no se manda nada al overlay, pero el estado cambia
  const c = banco({ listo: false });
  c.esc.alMensajeWs({ tipo: 'vision', datos: { camara: true } });
  assert.equal(c.ipc('overlay:order').length, 0);
  assert.equal(c.esc.estado().camara, true);
});

test('mapeo: al conectar envía hola_app y la visibilidad actual; al desconectar avisa al chat', () => {
  const b = banco();
  b.esc.aparecer({});
  b.llamadas.length = 0;
  b.esc.alConectado();
  assert.deepEqual(b.ws(), [
    { tipo: 'hola_app', pid: 4321, version: '0.2.0' },
    { tipo: 'evento', nombre: 'mostrada', datos: {} },
  ]);
  b.esc.alDesconectado();
  assert.deepEqual(b.ipc('chat:status'), ['desconectada']);
  b.esc.alChatCargado();
  assert.deepEqual(b.ipc('chat:status').at(-1), 'desconectada');
});

test('mapeo: eventos del renderer — solo toque, soltada y cursor_encima', () => {
  const b = banco();
  for (const n of ['mostrada', 'ocultada', 'chat_abierto', 'hackear', 42]) assert.equal(b.esc.alEventoOverlay(n, {}), false);
  assert.equal(b.esc.alEventoOverlay('toque', { x: 1, objeto: { a: 1 } }), true); // datos no escalares → {}
  assert.equal(b.esc.alEventoOverlay('soltada', { altura: 7 }), true);
  assert.equal(b.esc.alEventoOverlay('soltada', { altura: 'alta' }), true);
  assert.deepEqual(b.ws(), [
    { tipo: 'evento', nombre: 'toque', datos: {} },
    { tipo: 'evento', nombre: 'soltada', datos: { altura: 1 } },
    { tipo: 'evento', nombre: 'soltada', datos: { altura: 0 } },
  ]);
});

test('mapeo: cursor_encima como mucho una vez cada 10 s', () => {
  const b = banco();
  assert.equal(b.esc.alEventoOverlay('cursor_encima'), true);
  b.reloj.t += INTERVALO_CURSOR_ENCIMA_MS - 1;
  assert.equal(b.esc.alEventoOverlay('cursor_encima'), false);
  b.reloj.t += 1;
  assert.equal(b.esc.alEventoOverlay('cursor_encima'), true);
  assert.equal(b.ws().length, 2);
});

// ------------------------------------------------------------------ validaciones puras

test('mapeo: validarUrlHttps', () => {
  assert.equal(validarUrlHttps('https://example.com/a?b=1'), 'https://example.com/a?b=1');
  assert.equal(validarUrlHttps(`https://e.com/${'a'.repeat(290)}`), null); // > 300
  assert.equal(validarUrlHttps(`https://e.com/${'a'.repeat(286)}`).length, 300);
  for (const u of ['http://example.com', 'HTTPS://example.com', 'file:///etc/passwd', 'javascript:alert(1)',
    'https://user:pw@example.com', 'https://', 'https://exa mple.com', ' https://example.com', 42, null]) {
    assert.equal(validarUrlHttps(u), null, String(u));
  }
});

test('mapeo: UrlsOfrecidas está acotado', () => {
  const u = new UrlsOfrecidas(3);
  for (let i = 0; i < 5; i++) u.agregar(`https://e.com/${i}`);
  assert.equal(u.contiene('https://e.com/0'), false);
  assert.equal(u.contiene('https://e.com/4'), true);
  assert.equal(u.agregar('http://e.com'), false);
});

test('mapeo: datosEscalares y textoSeguro', () => {
  assert.deepEqual(datosEscalares({ a: 1, b: 'x', c: true, d: null }), { a: 1, b: 'x', c: true, d: null });
  assert.equal(datosEscalares({ a: { b: 1 } }), null);
  assert.equal(datosEscalares({ a: Infinity }), null);
  assert.equal(datosEscalares([1]), null);
  assert.deepEqual(datosEscalares(undefined), {});
  assert.equal(textoSeguro(5, 10), '');
  assert.equal(textoSeguro('a\tb\u0001', 10), 'a\tb');
});

// ------------------------------------------------------------------ tarjetas aprobables (v0.2.2, §12–§13)

const H = 'ab'.repeat(32);
const SECCIONES = {
  correo: () => ({ para: ['ana@example.com'], cc: [], cco: [], asunto: 'Informe', cuerpo: 'Hola Ana,\n\tadjunto.',
    adjuntos: [{ nombre: 'informe.pdf', bytes: 2048, carpeta: 'documentos' }], problemas: [], avisos: [], listo: true }),
  drive: () => ({ op: 'subir', paso: 1, archivo: null, local: { nombre: 'plan.xlsx', bytes: 2100000, carpeta: 'documentos' },
    carpeta_destino: 'Trabajo', compartir: null, problemas: [], listo: true }),
  script: () => ({ lenguaje: 'powershell', plataforma: 'windows', codigo: 'Get-ChildItem\n', explicacion: 'Lista',
    efectos: ['solo lee'], requisitos: [], nivel: 'bajo', ejecutable: true, motivo_no_ejecutable: '', metodo: 'ast',
    hallazgos: [], hallazgos_omitidos: 0, habilidad: null, habilidad_cambiada: false, ejecuciones: 0 }),
  salida: () => ({ script_id: '5c1a7e00', lenguaje: 'powershell', codigo_salida: 0, tiempo_agotado: false,
    duracion_s: 1.25, stdout: '42\n', stderr: '', recortada: false, carpeta: 'taller/5c1a7e00' }),
};
const BOTONES = { correo: ['aprobar', 'editar', 'descartar'], drive: ['aprobar', 'descartar'],
  script: ['aprobar', 'guardar', 'copiar', 'descartar'], salida: ['descartar'] };

function tarjeta(clase = 'correo', { id = '0a1b2c3d', version = 1, seccion, ...resto } = {}) {
  const t = { id, clase, version, sha256: H, titulo: `Tarjeta de ${clase}`, resumen: 'Revísala', aviso: '',
    botones: [...BOTONES[clase]], caduca_ts: 1800000000, canal_origen: 'escritorio',
    estado: clase === 'salida' ? 'info' : 'pendiente', correo: null, drive: null, script: null, salida: null, ...resto };
  t[clase] = seccion || SECCIONES[clase]();
  return t;
}
const conSeccion = (clase, cambios) => tarjeta(clase, { seccion: { ...SECCIONES[clase](), ...cambios } });
const msgTarjeta = (t) => ({ tipo: 'tarjeta', datos: t });
const NO_MOLESTAR = (activo, usuario) => ({ tipo: 'no_molestar', datos: { activo, motivo: usuario ? 'usuario' : 'juego', usuario } });

test('mapeo/tarjetas: constantes iguales a core/contracts.py', () => {
  assert.deepEqual([MAX_CODIGO, MAX_CUERPO, MAX_SALIDA, MAX_HALLAZGOS, MAX_TARJETAS, MAX_BYTES_DECISION],
    [24576, 10000, 8192, 20, 4, 61440]);
  assert.deepEqual(CLASES_TARJETA, ['correo', 'drive', 'script', 'salida']);
  assert.deepEqual(BOTONES_TARJETA, ['aprobar', 'aprobar_publico', 'editar', 'guardar', 'copiar', 'descartar']);
  assert.deepEqual(DECISIONES_TARJETA, ['aprobar', 'aprobar_publico', 'editar', 'guardar', 'descartar']);
  assert.deepEqual(NIVELES_RIESGO, ['bajo', 'medio', 'alto', 'critico']);
  assert.ok(RE_HUELLA.test(H) && !RE_HUELLA.test('AB'.repeat(32)));
});

test('mapeo/tarjetas: validarTarjeta — esquema exacto, informativos recortados, aprobables intactos', () => {
  const t = validarTarjeta({ ...tarjeta('correo'), extra: 'x', titulo: 'T'.repeat(200), drive: { op: 'subir' },
    botones: ['aprobar', 'copiar', 'aprobar', 'volar', 'descartar'], canal_origen: 'nexus', estado: 'cerrada' });
  assert.deepEqual(Object.keys(t).sort(), ['aviso', 'botones', 'caduca_ts', 'canal_origen', 'clase', 'correo', 'drive',
    'estado', 'id', 'resumen', 'salida', 'script', 'sha256', 'titulo', 'version']);
  assert.equal(t.titulo.length, 120);
  assert.deepEqual(t.botones, ['aprobar', 'descartar']);
  assert.equal(t.canal_origen, 'escritorio');
  assert.equal(t.estado, 'pendiente');
  assert.equal(t.drive, null);
  assert.equal(t.correo.cuerpo, 'Hola Ana,\n\tadjunto.');
  // En el tope: intactos (en .length = unidades UTF-16).
  const cuerpo = '😀'.repeat(5000);
  assert.equal(validarTarjeta(conSeccion('correo', { cuerpo })).correo.cuerpo, cuerpo);
  const codigo = 'x'.repeat(MAX_CODIGO);
  assert.equal(validarTarjeta(conSeccion('script', { codigo })).script.codigo, codigo);
  // Crítico: nunca ejecutable y sin «aprobar» ni «guardar».
  const critico = validarTarjeta(conSeccion('script', { nivel: 'critico', ejecutable: true }));
  assert.equal(critico.script.ejecutable, false);
  assert.deepEqual(critico.botones, ['copiar', 'descartar']);
  // Hallazgos: ≤ 20, de más a menos grave; el resto (y los inválidos) suman a hallazgos_omitidos.
  const hallazgos = Array.from({ length: 25 }, (_v, i) => ({ nivel: i % 2 ? 'alto' : 'bajo', categoria: 'red', linea: i, detalle: 'd' }));
  hallazgos.push({ nivel: 'raro', categoria: 'red' });
  const s = validarTarjeta(conSeccion('script', { hallazgos, hallazgos_omitidos: 2 })).script;
  assert.equal(s.hallazgos.length, 20);
  assert.equal(s.hallazgos_omitidos, 2 + 1 + 5);
  assert.deepEqual(s.hallazgos.slice(0, 12).map((h) => h.nivel), Array(12).fill('alto'));
  // Salida → info.
  assert.equal(validarTarjeta(tarjeta('salida')).estado, 'info');
});

test('mapeo/tarjetas: un campo aprobable fuera de tope → null (NUNCA se recorta)', () => {
  const casos = [
    ['correo', { cuerpo: 'x'.repeat(10001) }],
    ['correo', { cuerpo: `${'a'.repeat(9999)}😀` }],     // 10 001 unidades UTF-16
    ['correo', { asunto: 'x'.repeat(201) }],
    ['correo', { para: ['x'.repeat(255)] }],
    ['correo', { para: 'ana@example.com' }],
    ['correo', { cc: Array(50).fill('a@b.co') }],         // 51 direcciones
    ['correo', { adjuntos: Array(6).fill({ nombre: 'a', bytes: 1, carpeta: 'd' }) }],
    ['correo', { adjuntos: [{ nombre: 'n'.repeat(256), bytes: 1, carpeta: 'd' }] }],
    ['correo', { cuerpo: 7 }],
    ['script', { codigo: 'x'.repeat(MAX_CODIGO + 1) }],
    ['script', { codigo: null }],
    ['drive', { local: { nombre: 'n'.repeat(256), bytes: 1, carpeta: 'd' } }],
    ['drive', { compartir: { con: 'c'.repeat(255), rol: 'reader', publico: false } }],
  ];
  for (const [clase, cambios] of casos) assert.equal(validarTarjeta(conSeccion(clase, cambios)), null, JSON.stringify(cambios).slice(0, 60));
  for (const malo of [{ id: '0A1B2C3D' }, { clase: 'otra' }, { version: 0 }, { version: '1' }, { sha256: 'x' }]) {
    assert.equal(validarTarjeta({ ...tarjeta('correo'), ...malo }), null);
  }
  assert.equal(validarTarjeta(conSeccion('drive', { op: 'borrar' })), null);
  assert.equal(validarTarjeta(conSeccion('drive', { paso: 3 })), null);
  assert.equal(validarTarjeta(conSeccion('script', { nivel: 'extremo' })), null);
  assert.equal(validarTarjeta(null), null);
  assert.deepEqual(validarTarjetaCerrada({ id: '0a1b2c3d', clase: 'correo', estado: 'cerrada', resultado: 'enviado', motivo: 'ok', x: 1 }),
    { id: '0a1b2c3d', clase: 'correo', estado: 'cerrada', resultado: 'enviado', motivo: 'ok' });
  assert.equal(validarTarjetaCerrada({ id: '0a1b2c3d', clase: 'correo', estado: 'cerrada', resultado: 'volado' }), null);
});

test('mapeo/tarjetas: validarDecisionTarjeta — reglas de §12.3, nunca recorta', () => {
  const v = { version: 3 };
  assert.deepEqual(validarDecisionTarjeta('0a1b2c3d', H, 'aprobar', v), { id: '0a1b2c3d', sha256: H, decision: 'aprobar', version: 3 });
  assert.deepEqual(validarDecisionTarjeta('0a1b2c3d', H, 'guardar', { ...v, nombre: '  limpiar  ' }),
    { id: '0a1b2c3d', sha256: H, decision: 'guardar', version: 3, nombre: 'limpiar' });
  const campos = { cuerpo: '😀'.repeat(5000), para: 'x'.repeat(2600), asunto: '€'.repeat(200), adjuntos: 'a.pdf, b.pdf' };
  assert.deepEqual(validarDecisionTarjeta('0a1b2c3d', H, 'editar', { ...v, campos }).campos, campos);
  // «descartar» no ejecuta nada: la versión es opcional (compatibilidad); el resto, obligatoria.
  assert.deepEqual(validarDecisionTarjeta('0a1b2c3d', H, 'descartar'), { id: '0a1b2c3d', sha256: H, decision: 'descartar' });
  const malos = [
    ['0A1B2C3D', H, 'aprobar', v], ['0a1b2c3d', 'x', 'aprobar', v], ['0a1b2c3d', H, 'copiar', v],
    ['0a1b2c3d', H, 'aprobar'], ['0a1b2c3d', H, 'aprobar_publico', {}], ['0a1b2c3d', H, 'aprobar', { version: 0 }],
    ['0a1b2c3d', H, 'aprobar', { version: 1.5 }], ['0a1b2c3d', H, 'aprobar', { version: '1' }],
    ['0a1b2c3d', H, 'aprobar', { version: 2 ** 31 }], ['0a1b2c3d', H, 'descartar', { version: -1 }],
    ['0a1b2c3d', H, 'guardar', { nombre: 'x' }], ['0a1b2c3d', H, 'editar', { campos: { cuerpo: 'x' } }],
    ['0a1b2c3d', H, 'aprobar', { ...v, nombre: 'x' }], ['0a1b2c3d', H, 'descartar', { campos: {} }],
    ['0a1b2c3d', H, 'guardar', v], ['0a1b2c3d', H, 'guardar', { ...v, nombre: '   ' }],
    ['0a1b2c3d', H, 'guardar', { ...v, nombre: 'n'.repeat(41) }],
    ['0a1b2c3d', H, 'guardar', { ...v, nombre: 'a\nb' }], ['0a1b2c3d', H, 'editar', v], ['0a1b2c3d', H, 'editar', { ...v, campos: null }],
    ['0a1b2c3d', H, 'editar', { ...v, campos: { firma: 'x' } }], ['0a1b2c3d', H, 'editar', { ...v, campos: { cuerpo: 'x'.repeat(10001) } }],
    ['0a1b2c3d', H, 'editar', { ...v, campos: { cuerpo: `${'😀'.repeat(5000)}x` } }],
    ['0a1b2c3d', H, 'editar', { ...v, campos: { para: 'x'.repeat(2601) } }], ['0a1b2c3d', H, 'editar', { ...v, campos: { asunto: 'a\nb' } }],
    ['0a1b2c3d', H, 'editar', { ...v, campos: { cuerpo: 'a\rb' } }], ['0a1b2c3d', H, 'editar', { ...v, campos: { cuerpo: 5 } }],
    ['0a1b2c3d', H, 'editar', { ...v, campos: { cuerpo: 'Hola\u000bAna' } }], ['0a1b2c3d', H, 'editar', { ...v, campos: { asunto: 'Informe\tQ3' } }],
  ];
  for (const args of malos) assert.equal(validarDecisionTarjeta(...args), null, JSON.stringify(args).slice(0, 80));
  assert.ok(validarDecisionTarjeta('0a1b2c3d', H, 'editar', { ...v, campos: { cuerpo: 'línea\n\tsangrada' } }));
});

test('mapeo/tarjetas: una pendiente NUEVA → chat:tarjeta y chat.showInactive() (nunca focus); repetida o info no reabre', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  assert.equal(b.esc.alMensajeWs(msgTarjeta(tarjeta('correo'))), true);
  assert.deepEqual(b.ipc('chat:tarjeta').map((t) => [t.id, t.estado]), [['0a1b2c3d', 'pendiente']]);
  assert.deepEqual(b.nombres().filter((n) => n.startsWith('chat.')), ['chat.showInactive']);
  b.esc.alMensajeWs(msgTarjeta(tarjeta('correo', { version: 2 })));        // misma id: no vuelve a abrir
  b.esc.alMensajeWs(msgTarjeta(tarjeta('salida', { id: '5a11da00' })));    // informativa: no abre
  assert.deepEqual(b.nombres().filter((n) => n.startsWith('chat.')), ['chat.showInactive']);
  assert.equal(b.ipc('chat:tarjeta').length, 3);
  // Con «no molestar» (del usuario o automático) no se abre el chat.
  b.esc.alMensajeWs(NO_MOLESTAR(true, false));
  b.esc.alMensajeWs(msgTarjeta(tarjeta('drive', { id: '00000003' })));
  b.esc.alMensajeWs(NO_MOLESTAR(true, true));
  b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '00000004' })));
  assert.deepEqual(b.nombres().filter((n) => n.startsWith('chat.')), ['chat.showInactive']);
  assert.ok(!b.nombres().includes('chat.focus') && !b.nombres().includes('chat.show'));
  assert.equal(b.esc.estado().tarjetas, 4);
  // Inválida: no sale.
  assert.equal(b.esc.alMensajeWs(msgTarjeta(conSeccion('correo', { cuerpo: 'x'.repeat(10001) }))), false);
});

test('mapeo/tarjetas: versión vieja ignorada, igual reemplaza; cerrada → chat:tarjeta cerrada y deja de estar vigente', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '5c1a7e00', version: 3 })));
  assert.equal(b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '5c1a7e00', version: 2 }))), false);
  assert.equal(b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '5c1a7e00', version: 3, aviso: 'Cambió' }))), true);
  assert.equal(b.ipc('chat:tarjeta').at(-1).aviso, 'Cambió');
  assert.equal(b.esc.codigoDeTarjeta('5c1a7e00'), 'Get-ChildItem\n');
  b.esc.alMensajeWs({ tipo: 'tarjeta', datos: { id: '5c1a7e00', clase: 'script', estado: 'cerrada', resultado: 'descartado', motivo: '' } });
  assert.deepEqual(b.ipc('chat:tarjeta').at(-1), { id: '5c1a7e00', clase: 'script', estado: 'cerrada', resultado: 'descartado', motivo: '' });
  assert.equal(b.esc.codigoDeTarjeta('5c1a7e00'), null);
  assert.equal(b.esc.estado().tarjetas, 0);
});

test('mapeo/tarjetas: codigoDeTarjeta solo de una tarjeta de SCRIPT vigente', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(msgTarjeta(tarjeta('correo')));
  b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '5c1a7e00' })));
  assert.equal(b.esc.codigoDeTarjeta('0a1b2c3d'), null);           // un correo no se copia
  assert.equal(b.esc.codigoDeTarjeta('5c1a7e00'), 'Get-ChildItem\n');
  for (const malo of ['5C1A7E00', '5c1a7e0', null, 42, { id: '5c1a7e00' }]) assert.equal(b.esc.codigoDeTarjeta(malo), null);
});

test('mapeo/tarjetas: revisión tras reconectar — la que vuelve sigue vigente; la que no, «ya no está vigente»', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(msgTarjeta(tarjeta('correo')));
  b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '5c1a7e00' })));
  b.esc.alDesconectado();
  b.llamadas.length = 0;
  // instantánea: hola, tarjetas vigentes (solo el correo), no_molestar
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  assert.equal(b.esc.codigoDeTarjeta('5c1a7e00'), null);           // en revisión: no se copia
  b.esc.alMensajeWs(msgTarjeta(tarjeta('correo')));
  assert.equal(b.ipc('chat:tarjeta').length, 1);                     // aún en revisión
  b.esc.alMensajeWs(NO_MOLESTAR(false, false));
  assert.deepEqual(b.ipc('chat:tarjeta').at(-1), { id: '5c1a7e00', clase: 'script', estado: 'cerrada', resultado: 'caducado',
    motivo: '', caducada: true });
  assert.ok(!b.nombres().includes('chat.showInactive'));            // la que vuelve no reabre el chat
  b.avanzar(10_000);
  assert.equal(b.ipc('chat:tarjeta').length, 2);
  // Con un permiso y una tarjeta en revisión, ni `permiso` ni `tarjeta` cierran la revisión.
  b.esc.alDesconectado();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(PEND('deadbeef'));
  b.esc.alMensajeWs(msgTarjeta(tarjeta('drive', { id: '00000003' })));
  assert.equal(b.ipc('chat:tarjeta').filter((t) => t.caducada).length, 1);
  b.esc.alMensajeWs({ tipo: 'travesuras', datos: { activas: false } });
  assert.deepEqual(b.ipc('chat:tarjeta').filter((t) => t.caducada).map((t) => t.id), ['5c1a7e00', '0a1b2c3d']);
});

test('mapeo/tarjetas: alChatCargado reenvía las vigentes (de la más vieja a la más nueva) antes del estado', () => {
  const b = banco();
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alMensajeWs(msgTarjeta(tarjeta('correo')));
  b.esc.alMensajeWs(msgTarjeta(tarjeta('script', { id: '5c1a7e00' })));
  b.llamadas.length = 0;
  b.esc.alChatCargado();
  assert.deepEqual(b.ipc('chat:tarjeta').map((t) => t.id), ['0a1b2c3d', '5c1a7e00']);
  assert.deepEqual(b.llamadas.map((l) => l[1]), ['chat:tarjeta', 'chat:tarjeta', 'chat:status']);
});

test('mapeo/tarjetas: alDecidirTarjeta envía con maxBytes 61440; los demás envíos, sin maxBytes', () => {
  const b = banco();
  b.esc.alConectado();                                   // hola_app y evento
  b.esc.alMensajeWs({ tipo: 'hola', datos: {} });
  b.esc.alChatSend('hola');
  b.esc.alConfirmar('deadbeef', true);
  b.esc.control('no_molestar_on', 'chat');
  b.esc.alEventoOverlay('toque', {});
  const campos = { cuerpo: '€'.repeat(10000), para: '€'.repeat(2600), cc: '€'.repeat(2600), cco: '€'.repeat(2600),
    asunto: '€'.repeat(200), adjuntos: '€'.repeat(1000) };   // el «editar» más grande posible (≈ 57 KB)
  assert.equal(b.esc.alDecidirTarjeta('0a1b2c3d', H, 'editar', { campos, version: 2 }), true);
  assert.equal(b.esc.alDecidirTarjeta('0a1b2c3d', H, 'aprobar', { version: 3 }), true);
  const decisiones = b.envios.filter((e) => e.obj.tipo === 'tarjeta_decision');
  assert.deepEqual(decisiones.map((e) => e.maxBytes), [61440, 61440]);
  assert.deepEqual(decisiones[0].obj, { tipo: 'tarjeta_decision', id: '0a1b2c3d', sha256: H, decision: 'editar', version: 2, campos });
  assert.deepEqual(decisiones[1].obj, { tipo: 'tarjeta_decision', id: '0a1b2c3d', sha256: H, decision: 'aprobar', version: 3 });
  assert.ok(Buffer.byteLength(JSON.stringify(decisiones[0].obj), 'utf8') > 8192);
  const otros = b.envios.filter((e) => e.obj.tipo !== 'tarjeta_decision');
  assert.deepEqual([...new Set(otros.map((e) => e.obj.tipo))].sort(), ['chat', 'confirmacion', 'control', 'evento', 'hola_app']);
  assert.ok(otros.every((e) => e.maxBytes === undefined));
  // Inválida: false sin enviar nada (también un «aprobar» sin la versión que se ve).
  const n = b.envios.length;
  assert.equal(b.esc.alDecidirTarjeta('0a1b2c3d', H, 'editar', { campos: { cuerpo: 'x'.repeat(10001) }, version: 2 }), false);
  assert.equal(b.esc.alDecidirTarjeta('0a1b2c3d', H, 'copiar', { version: 2 }), false);
  assert.equal(b.esc.alDecidirTarjeta('0a1b2c3d', H, 'aprobar'), false);
  assert.equal(b.envios.length, n);
});

test('mapeo/tarjetas: alDecidirTarjeta sin WebSocket → false', () => {
  const b = banco({ wsAbierto: false });
  assert.equal(b.esc.alDecidirTarjeta('0a1b2c3d', H, 'descartar'), false);
  assert.equal(b.ws().length, 0);
});
