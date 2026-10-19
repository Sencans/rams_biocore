'use strict';
/**
 * Páginas de Puter de Amadeus Desktop (COMPANION.md §C2.15): qué se sirve, con qué CSP y a qué
 * dominios pueden abrir ventanas o navegar. Módulo puro (sin Electron ni E/S).
 *
 * Por qué un origen `http://amadeus-puter.localhost` y no `file://`: `puter.auth.signIn()` rechaza
 * los orígenes opacos (`unsupported_origin`, https://docs.puter.com/Auth/signIn/) y Puter guarda la
 * sesión por origen. El main intercepta `http:` SOLO en la partición `persist:puter` con
 * `protocol.handle` y sirve estos archivos de `desktop/puter/` sin abrir ningún puerto; cualquier
 * otra URL `http:` de esa partición recibe 404. `*.localhost` es un contexto seguro en Chromium y el
 * origen es estable, así que la sesión se conserva entre arranques.
 *
 * CSP (por cabecera): scripts solo propios y de https://js.puter.com (la biblioteca); conexiones
 * solo a los orígenes de la API y de la interfaz de Puter (`APIOrigin` = https://api.puter.com,
 * `GUIOrigin` = https://puter.com); sin marcos, objetos ni formularios.
 */

const { ORIGEN_PUTER } = require('./puter');

const HOST_PUTER = 'amadeus-puter.localhost';
const URL_PUTER_JS = 'https://js.puter.com/v2/';

/** Ruta publicada → [archivo en desktop/puter/, tipo MIME]. Nada más se sirve. */
const RECURSOS = Object.freeze({
  '/trabajo.html': ['trabajo.html', 'text/html; charset=utf-8'],
  '/trabajo.js': ['trabajo.js', 'text/javascript; charset=utf-8'],
  '/conectar.html': ['conectar.html', 'text/html; charset=utf-8'],
  '/conectar.js': ['conectar.js', 'text/javascript; charset=utf-8'],
  '/puter.css': ['puter.css', 'text/css; charset=utf-8'],
});

const URL_TRABAJO = `${ORIGEN_PUTER}/trabajo.html`;
const URL_CONECTAR = `${ORIGEN_PUTER}/conectar.html`;

const CSP_PUTER = [
  "default-src 'none'",
  "script-src 'self' https://js.puter.com",
  "style-src 'self' 'unsafe-inline'", // los diálogos de puter.js llevan estilos en línea
  "img-src 'self' data: blob: https://js.puter.com https://puter.com",
  "font-src 'self' data:",
  "connect-src 'self' https://api.puter.com wss://api.puter.com https://puter.com https://js.puter.com",
  "frame-src 'none'",
  "worker-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join('; ');

/** Cabeceras de las páginas propias. */
function cabecerasPuter(tipo) {
  return {
    'Content-Type': tipo,
    'Content-Security-Policy': CSP_PUTER,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Cross-Origin-Resource-Policy': 'same-origin',
  };
}

/**
 * Recurso propio para una petición interceptada (`{archivo, tipo}`) o `null` (→ 404). Solo GET o
 * HEAD a `http://amadeus-puter.localhost/<ruta conocida>`, sin credenciales ni puerto.
 */
function resolverRecurso(url, metodo = 'GET') {
  if (metodo !== 'GET' && metodo !== 'HEAD') return null;
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== 'http:' || u.hostname !== HOST_PUTER || u.port || u.username || u.password) return null;
  const r = Object.hasOwn(RECURSOS, u.pathname) ? RECURSOS[u.pathname] : null;
  return r ? { archivo: r[0], tipo: r[1] } : null;
}

/** ¿`url` es exactamente la página propia `pagina` (sin consulta ni fragmento)? */
function esPaginaPuter(url, pagina) {
  return typeof url === 'string' && (url === `${ORIGEN_PUTER}/${pagina}`);
}

/**
 * ¿Se puede abrir (window.open) o navegar a `url` desde el inicio de sesión? Solo `https:` en
 * puter.com o sus subdominios, sin credenciales ni puertos raros.
 */
function esUrlPuterPermitida(url) {
  if (typeof url !== 'string' || url.length > 2048) return false;
  let u;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol !== 'https:' || u.username || u.password || (u.port && u.port !== '443')) return false;
  const host = u.hostname.toLowerCase().replace(/\.$/, '');
  return host === 'puter.com' || host.endsWith('.puter.com');
}

module.exports = {
  HOST_PUTER,
  URL_PUTER_JS,
  URL_TRABAJO,
  URL_CONECTAR,
  RECURSOS,
  CSP_PUTER,
  cabecerasPuter,
  resolverRecurso,
  esPaginaPuter,
  esUrlPuterPermitida,
};
