'use strict';
/**
 * Archivo de token del endpoint `/escritorio` de R.A.M.S. (COMPANION.md §C2.4.3 y §C2.7.6).
 * Módulo puro salvo por la lectura, que recibe `fs/promises` inyectado (los tests usan uno falso).
 *
 * Ruta:
 * - `RAMS_ESCRITORIO_TOKEN_FILE` (absoluta) si está definida;
 * - Windows: `%LOCALAPPDATA%\rams\escritorio.json`;
 * - Linux: `$XDG_RUNTIME_DIR/rams/escritorio.json`; SIN `XDG_RUNTIME_DIR` no se conecta (nunca /tmp).
 * En Linux el archivo debe ser del usuario (`uid === process.getuid()`) y sin permisos para grupo
 * ni otros (`(mode & 0o077) === 0`), y no puede ser un enlace simbólico.
 * Contenido: `{"url":"ws://127.0.0.1:<puerto>/escritorio","token":"…","pid":<int>}`.
 * El token nunca se registra: usa `urlSinSecreto` para cualquier mensaje.
 */

const path = require('node:path');
const fs = require('node:fs');

const MAX_BYTES = 4096;
const RE_URL = /^ws:\/\/(127\.0\.0\.1|localhost):(\d{1,5})\/escritorio$/;
const RE_TOKEN = /^[A-Za-z0-9_-]{16,128}$/;

function limpiar(v) {
  if (typeof v !== 'string') return null;
  const t = v.trim();
  return t && !t.includes('\0') ? t : null;
}

function rutasDe(plataforma) {
  return plataforma === 'win32' ? path.win32 : path.posix;
}

/**
 * Calcula la ruta del archivo de token.
 * @returns {{ruta: string|null, motivo: string|null}}  `ruta === null` → no hay que conectar.
 */
function rutaToken({ env = process.env, plataforma = process.platform } = {}) {
  const p = rutasDe(plataforma);
  const explicita = limpiar(env.RAMS_ESCRITORIO_TOKEN_FILE);
  if (explicita) {
    if (!p.isAbsolute(explicita)) {
      return { ruta: null, motivo: 'RAMS_ESCRITORIO_TOKEN_FILE debe ser una ruta absoluta' };
    }
    return { ruta: p.normalize(explicita), motivo: null };
  }
  if (plataforma === 'win32') {
    const base = limpiar(env.LOCALAPPDATA);
    if (!base || !p.isAbsolute(base)) {
      return { ruta: null, motivo: 'sin LOCALAPPDATA: define RAMS_ESCRITORIO_TOKEN_FILE' };
    }
    return { ruta: p.join(base, 'rams', 'escritorio.json'), motivo: null };
  }
  const xdg = limpiar(env.XDG_RUNTIME_DIR);
  if (!xdg || !p.isAbsolute(xdg)) {
    return { ruta: null, motivo: 'sin XDG_RUNTIME_DIR: no se conecta (define RAMS_ESCRITORIO_TOKEN_FILE; nunca /tmp)' };
  }
  return { ruta: p.join(xdg, 'rams', 'escritorio.json'), motivo: null };
}

/**
 * Comprueba el `stat` del archivo abierto.
 * @param {{isFile:()=>boolean, mode:number, uid:number, size:number}} st
 * @returns {{ok: boolean, motivo: string|null}}
 */
function comprobarPermisos(st, { plataforma = process.platform, uid } = {}) {
  if (!st || typeof st.isFile !== 'function' || !st.isFile()) {
    return { ok: false, motivo: 'no es un archivo regular' };
  }
  if (typeof st.size === 'number' && st.size > MAX_BYTES) {
    return { ok: false, motivo: 'archivo demasiado grande' };
  }
  if (plataforma !== 'win32') {
    if (typeof uid !== 'number' || typeof st.uid !== 'number') {
      return { ok: false, motivo: 'no se puede verificar el dueño del archivo' };
    }
    if (st.uid !== uid) return { ok: false, motivo: 'el archivo es de otro usuario' };
    if ((st.mode & 0o077) !== 0) {
      const modo = (st.mode & 0o777).toString(8).padStart(4, '0');
      return { ok: false, motivo: `permisos demasiado abiertos (${modo}); deben ser 0600` };
    }
  }
  return { ok: true, motivo: null };
}

/**
 * Valida el JSON del archivo.
 * @returns {{ok:true, url:string, token:string, pid:number|null} | {ok:false, motivo:string}}
 */
function validarContenido(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return { ok: false, motivo: 'contenido inválido' };
  const m = typeof obj.url === 'string' ? RE_URL.exec(obj.url) : null;
  const puerto = m ? Number(m[2]) : 0;
  if (!m || puerto < 1 || puerto > 65535) return { ok: false, motivo: 'url inválida (debe ser ws://127.0.0.1:<puerto>/escritorio)' };
  if (typeof obj.token !== 'string' || !RE_TOKEN.test(obj.token)) return { ok: false, motivo: 'token inválido' };
  const pid = Number.isInteger(obj.pid) && obj.pid > 0 ? obj.pid : null;
  return { ok: true, url: obj.url, token: obj.token, pid };
}

/**
 * Lee y valida el archivo de token. Nunca lanza.
 * @param {{env?:object, plataforma?:string, fsp?:object, getuid?:Function}} opciones
 *   `fsp` con la forma de `fs/promises` (`lstat`, `open` → handle con `stat`, `readFile`, `close`).
 * @returns {Promise<{ok:true, ruta, url, token, pid} | {ok:false, motivo:string, conectar:boolean}>}
 */
async function leerToken({ env = process.env, plataforma = process.platform, fsp = fs.promises, getuid } = {}) {
  const { ruta, motivo } = rutaToken({ env, plataforma });
  if (!ruta) return { ok: false, motivo, conectar: false };
  let fh = null;
  try {
    if (plataforma !== 'win32') {
      const l = await fsp.lstat(ruta);
      if (l.isSymbolicLink()) return { ok: false, motivo: 'el archivo de token es un enlace simbólico', conectar: true };
    }
    const nofollow = plataforma !== 'win32' && fs.constants.O_NOFOLLOW ? fs.constants.O_NOFOLLOW : 0;
    fh = await fsp.open(ruta, fs.constants.O_RDONLY | nofollow);
    const st = await fh.stat();
    const uid = typeof getuid === 'function' ? getuid() : undefined;
    const perm = comprobarPermisos(st, { plataforma, uid });
    if (!perm.ok) return { ok: false, motivo: perm.motivo, conectar: true };
    const texto = await fh.readFile({ encoding: 'utf8' });
    if (typeof texto !== 'string' || texto.length > MAX_BYTES) {
      return { ok: false, motivo: 'archivo demasiado grande', conectar: true };
    }
    let obj;
    try {
      obj = JSON.parse(texto);
    } catch {
      return { ok: false, motivo: 'JSON inválido', conectar: true };
    }
    const v = validarContenido(obj);
    if (!v.ok) return { ok: false, motivo: v.motivo, conectar: true };
    return { ok: true, ruta, url: v.url, token: v.token, pid: v.pid };
  } catch (err) {
    const codigo = err && err.code ? err.code : 'error';
    const m = codigo === 'ENOENT' ? 'no existe (¿R.A.M.S. está apagada?)' : `no se pudo leer (${codigo})`;
    return { ok: false, motivo: m, conectar: true };
  } finally {
    if (fh) {
      try {
        await fh.close();
      } catch {
        /* nada */
      }
    }
  }
}

/** URL de conexión con el token en la query (NUNCA registrarla). */
function urlConexion({ url, token }) {
  return `${url}?t=${encodeURIComponent(token)}`;
}

/** Versión segura para logs: sin query ni token. */
function urlSinSecreto(url) {
  return typeof url === 'string' ? url.split('?')[0] : '';
}

module.exports = {
  MAX_BYTES,
  RE_URL,
  RE_TOKEN,
  rutaToken,
  comprobarPermisos,
  validarContenido,
  leerToken,
  urlConexion,
  urlSinSecreto,
};
