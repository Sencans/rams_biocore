'use strict';
/**
 * §C2.4.4: R.A.M.S. distingue a Amadeus Desktop (sin cabecera Origin) de la página del HUD (con
 * Origin local). Aquí se comprueba que el WebSocket GLOBAL de Node (el que usa el main de Electron)
 * no envía Origin. Servidor TCP propio en 127.0.0.1:0 que solo lee el handshake y responde 403.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const net = require('node:net');

test('el WebSocket global no envía Origin en el handshake', { skip: typeof WebSocket !== 'function' }, async () => {
  let cabeceras = null;
  const servidor = net.createServer((sock) => {
    let buf = '';
    sock.setEncoding('latin1');
    sock.on('data', (d) => {
      buf += d;
      const fin = buf.indexOf('\r\n\r\n');
      if (fin < 0) return;
      const [linea, ...resto] = buf.slice(0, fin).split('\r\n');
      cabeceras = { linea, ...Object.fromEntries(resto.map((l) => {
        const i = l.indexOf(':');
        return [l.slice(0, i).trim().toLowerCase(), l.slice(i + 1).trim()];
      })) };
      sock.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');
    });
    sock.on('error', () => {});
  });
  await new Promise((res) => servidor.listen(0, '127.0.0.1', res));
  const { port } = servidor.address();
  try {
    await new Promise((res) => {
      const ws = new WebSocket(`ws://127.0.0.1:${port}/escritorio?t=prueba`);
      ws.addEventListener('error', () => {});
      ws.addEventListener('close', () => res());
    });
  } finally {
    servidor.close();
  }
  assert.ok(cabeceras, 'no llegó el handshake');
  assert.equal(cabeceras.linea, 'GET /escritorio?t=prueba HTTP/1.1');
  assert.ok(cabeceras['sec-websocket-key']);
  assert.equal(cabeceras.origin, undefined);
});
