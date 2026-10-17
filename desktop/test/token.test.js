'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  rutaToken, comprobarPermisos, validarContenido, leerToken, urlConexion, urlSinSecreto,
} = require('../lib/token');

const CONTENIDO = { url: 'ws://127.0.0.1:8765/escritorio', token: 'AbCdEfGhIjKlMnOpQrStUv', pid: 1234 };

/** `fs/promises` falso: registra las llamadas y devuelve el stat y el contenido indicados. */
function fspFalso({ modo = 0o100600, uid = 1000, enlace = false, contenido = JSON.stringify(CONTENIDO), error = null } = {}) {
  const llamadas = [];
  const st = {
    isFile: () => (modo & 0o170000) === 0o100000,
    isSymbolicLink: () => enlace,
    mode: modo,
    uid,
    size: Buffer.byteLength(contenido),
  };
  return {
    llamadas,
    async lstat(ruta) {
      llamadas.push(['lstat', ruta]);
      if (error) throw error;
      return st;
    },
    async open(ruta) {
      llamadas.push(['open', ruta]);
      if (error) throw error;
      return {
        stat: async () => st,
        readFile: async () => contenido,
        close: async () => llamadas.push(['close']),
      };
    },
  };
}

test('token: Linux sin XDG_RUNTIME_DIR → no conecta y no toca el disco (nunca /tmp)', async () => {
  const fsp = fspFalso();
  const r = await leerToken({ env: {}, plataforma: 'linux', fsp, getuid: () => 1000 });
  assert.equal(r.ok, false);
  assert.equal(r.conectar, false);
  assert.match(r.motivo, /XDG_RUNTIME_DIR/);
  assert.deepEqual(fsp.llamadas, []);
  assert.equal(rutaToken({ env: { XDG_RUNTIME_DIR: 'relativa/run' }, plataforma: 'linux' }).ruta, null);
  assert.equal(rutaToken({ env: { XDG_RUNTIME_DIR: '   ' }, plataforma: 'linux' }).ruta, null);
});

test('token: rutas por plataforma y variable explícita', () => {
  assert.equal(rutaToken({ env: { XDG_RUNTIME_DIR: '/run/user/1000' }, plataforma: 'linux' }).ruta,
    '/run/user/1000/rams/escritorio.json');
  assert.equal(rutaToken({ env: { LOCALAPPDATA: 'C:\\Users\\Ana\\AppData\\Local' }, plataforma: 'win32' }).ruta,
    'C:\\Users\\Ana\\AppData\\Local\\rams\\escritorio.json');
  assert.equal(rutaToken({ env: {}, plataforma: 'win32' }).ruta, null);
  assert.equal(rutaToken({ env: { RAMS_ESCRITORIO_TOKEN_FILE: '/srv/x/escritorio.json' }, plataforma: 'linux' }).ruta,
    '/srv/x/escritorio.json');
  assert.equal(rutaToken({ env: { RAMS_ESCRITORIO_TOKEN_FILE: 'D:\\rams\\t.json', LOCALAPPDATA: 'C:\\L' }, plataforma: 'win32' }).ruta,
    'D:\\rams\\t.json');
  const rel = rutaToken({ env: { RAMS_ESCRITORIO_TOKEN_FILE: 'data/escritorio.json', XDG_RUNTIME_DIR: '/run/user/1' }, plataforma: 'linux' });
  assert.equal(rel.ruta, null);
  assert.match(rel.motivo, /absoluta/);
});

test('token: Linux con modo 0644 → rechaza', async () => {
  const r = await leerToken({
    env: { XDG_RUNTIME_DIR: '/run/user/1000' }, plataforma: 'linux', fsp: fspFalso({ modo: 0o100644 }), getuid: () => 1000,
  });
  assert.equal(r.ok, false);
  assert.match(r.motivo, /0644/);
});

test('token: Linux 0600 del mismo usuario → ok', async () => {
  const fsp = fspFalso();
  const r = await leerToken({ env: { XDG_RUNTIME_DIR: '/run/user/1000' }, plataforma: 'linux', fsp, getuid: () => 1000 });
  assert.deepEqual(r, { ok: true, ruta: '/run/user/1000/rams/escritorio.json', ...CONTENIDO });
  assert.deepEqual(fsp.llamadas.at(-1), ['close']);
});

test('token: Linux de otro usuario, enlace simbólico o sin getuid → rechaza', async () => {
  const env = { XDG_RUNTIME_DIR: '/run/user/1000' };
  const otro = await leerToken({ env, plataforma: 'linux', fsp: fspFalso({ uid: 0 }), getuid: () => 1000 });
  assert.match(otro.motivo, /otro usuario/);
  const enl = await leerToken({ env, plataforma: 'linux', fsp: fspFalso({ enlace: true }), getuid: () => 1000 });
  assert.match(enl.motivo, /enlace simbólico/);
  const sinUid = await leerToken({ env, plataforma: 'linux', fsp: fspFalso(), getuid: undefined });
  assert.equal(sinUid.ok, false);
});

test('token: Windows no exige uid/modo; directorio o archivo enorme → rechaza', () => {
  const st = { isFile: () => true, mode: 0o100666, uid: 0, size: 100 };
  assert.equal(comprobarPermisos(st, { plataforma: 'win32' }).ok, true);
  assert.equal(comprobarPermisos({ ...st, isFile: () => false }, { plataforma: 'win32' }).ok, false);
  assert.equal(comprobarPermisos({ ...st, size: 10_000 }, { plataforma: 'win32' }).ok, false);
});

test('token: archivo inexistente → no conecta todavía, pero reintenta', async () => {
  const error = Object.assign(new Error('no'), { code: 'ENOENT' });
  const r = await leerToken({ env: { LOCALAPPDATA: 'C:\\L' }, plataforma: 'win32', fsp: fspFalso({ error }) });
  assert.equal(r.ok, false);
  assert.equal(r.conectar, true);
  assert.match(r.motivo, /no existe/);
});

test('token: contenido inválido', async () => {
  const env = { LOCALAPPDATA: 'C:\\L' };
  const mal = await leerToken({ env, plataforma: 'win32', fsp: fspFalso({ contenido: '{no es json' }) });
  assert.match(mal.motivo, /JSON/);
  for (const url of ['ws://0.0.0.0:8765/escritorio', 'ws://192.168.1.5:8765/escritorio', 'wss://127.0.0.1:8765/escritorio',
    'ws://127.0.0.1:8765/ws', 'ws://127.0.0.1:99999/escritorio', 'ws://127.0.0.1:0/escritorio']) {
    assert.equal(validarContenido({ ...CONTENIDO, url }).ok, false, url);
  }
  for (const token of ['corto', 'con espacios dentro 1234', 'x'.repeat(200), 'abc&t=1234567890123', 42]) {
    assert.equal(validarContenido({ ...CONTENIDO, token }).ok, false, String(token));
  }
  assert.equal(validarContenido({ ...CONTENIDO, pid: -3 }).pid, null);
  assert.equal(validarContenido({ ...CONTENIDO, url: 'ws://localhost:8765/escritorio' }).ok, true);
  assert.equal(validarContenido([1]).ok, false);
});

test('token: la URL de conexión lleva el token codificado; la de logs no', () => {
  const u = urlConexion({ url: CONTENIDO.url, token: 'a-b_c' });
  assert.equal(u, 'ws://127.0.0.1:8765/escritorio?t=a-b_c');
  assert.equal(urlSinSecreto(u), 'ws://127.0.0.1:8765/escritorio');
  assert.equal(urlSinSecreto(undefined), '');
});

test('token: lectura real de un archivo temporal (RAMS_ESCRITORIO_TOKEN_FILE)', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'amadeus-token-test-'));
  try {
    const ruta = path.join(dir, 'escritorio.json');
    fs.writeFileSync(ruta, JSON.stringify(CONTENIDO), { mode: 0o600 });
    fs.chmodSync(ruta, 0o600);
    const env = { RAMS_ESCRITORIO_TOKEN_FILE: ruta };
    const getuid = typeof process.getuid === 'function' ? () => process.getuid() : undefined;
    const r = await leerToken({ env, plataforma: process.platform, getuid });
    assert.equal(r.ok, true, r.motivo);
    assert.equal(r.token, CONTENIDO.token);
    if (process.platform !== 'win32') {
      fs.chmodSync(ruta, 0o644);
      const abierto = await leerToken({ env, plataforma: process.platform, getuid });
      assert.equal(abierto.ok, false);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
