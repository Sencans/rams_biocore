'use strict';
/**
 * CSP del overlay (COMPANION.md §C2.7.7). Módulo puro (solo `node:crypto`).
 *
 * El overlay carga el visor de VTuber AI Studio por HTTP local. La CSP va por CABECERA (preferida a
 * `<meta>`), solo en los documentos del origen del visor:
 * - `script-src 'self' 'sha256-<importmap>'`: sin jsDelivr; el código (three, three-vrm) se sirve
 *   desde `public/vendor/`. El importmap en línea necesita su hash, que se recalcula en cada
 *   navegación del frame principal (si cambió, el main recarga una vez).
 * - `connect-src 'self' https://cdn.jsdelivr.net`: solo para el VRM de ejemplo, que es un DATO.
 *   Se añaden `blob:` y `data:`: el GLTFLoader de three crea URLs `blob:` para las texturas
 *   embebidas en el .vrm y las lee con `fetch` (ImageBitmapLoader en Chromium), que cae en
 *   connect-src; sin ellas el avatar sale sin texturas. Ninguna de las dos es un origen remoto.
 * - `img-src 'self' data: blob:`, `style-src 'self' 'unsafe-inline'`.
 */

const crypto = require('node:crypto');

const CDN_DATOS = 'https://cdn.jsdelivr.net';
const RE_IMPORTMAP = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
const RE_TIPO_IMPORTMAP = /\btype\s*=\s*(["']?)importmap\1(?=[\s>]|$)/i;

/**
 * Contenidos (exactos, sin recortar) de los `<script type="importmap">` en línea del HTML.
 * Los saltos CRLF/CR se normalizan a LF, como hace el parser HTML antes de calcular el hash.
 */
function extraerImportmaps(html) {
  if (typeof html !== 'string') return [];
  const texto = html.replace(/\r\n?/g, '\n');
  const out = [];
  RE_IMPORTMAP.lastIndex = 0;
  let m;
  while ((m = RE_IMPORTMAP.exec(texto)) !== null) {
    const atributos = m[1] || '';
    if (!RE_TIPO_IMPORTMAP.test(atributos)) continue;
    if (/\bsrc\s*=/i.test(atributos)) continue; // un importmap externo no lleva hash
    out.push(m[2]);
  }
  return out;
}

/** Fuente CSP `'sha256-…'` del contenido de un script en línea. */
function hashScript(contenido) {
  const b64 = crypto.createHash('sha256').update(String(contenido), 'utf8').digest('base64');
  return `'sha256-${b64}'`;
}

/** Hashes (únicos, en orden) de los importmaps en línea del `index.html` del visor. */
function hashesImportmap(html) {
  return [...new Set(extraerImportmaps(html).map(hashScript))];
}

/** ¿Dos listas de hashes son iguales (mismo conjunto)? */
function mismosHashes(a, b) {
  const x = Array.isArray(a) ? [...a].sort() : [];
  const y = Array.isArray(b) ? [...b].sort() : [];
  return x.length === y.length && x.every((v, i) => v === y[i]);
}

/** CSP del overlay. */
function construirCsp({ hashes = [] } = {}) {
  const validos = (Array.isArray(hashes) ? hashes : []).filter((h) => /^'sha256-[A-Za-z0-9+/]+=*'$/.test(h));
  return [
    "default-src 'self'",
    `script-src 'self'${validos.map((h) => ` ${h}`).join('')}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    `connect-src 'self' ${CDN_DATOS} blob: data:`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-src 'none'",
    "frame-ancestors 'none'",
  ].join('; ');
}

/**
 * Cabeceras de respuesta con NUESTRA CSP: quita cualquier CSP previa (sin distinguir mayúsculas,
 * también la `-Report-Only`) y añade la nuestra.
 */
function conCsp(responseHeaders, csp) {
  const out = {};
  for (const [k, v] of Object.entries(responseHeaders || {})) {
    const kl = k.toLowerCase();
    if (kl === 'content-security-policy' || kl === 'content-security-policy-report-only') continue;
    out[k] = v;
  }
  out['Content-Security-Policy'] = [csp];
  return out;
}

/** ¿La respuesta es un documento del visor (al que hay que ponerle la CSP)? */
function esDocumentoDelVisor(url, origenVisor, resourceType) {
  if (typeof url !== 'string' || typeof origenVisor !== 'string') return false;
  if (resourceType !== 'mainFrame' && resourceType !== 'subFrame') return false;
  try {
    return new URL(url).origin === origenVisor;
  } catch {
    return false;
  }
}

module.exports = {
  CDN_DATOS,
  extraerImportmaps,
  hashScript,
  hashesImportmap,
  mismosHashes,
  construirCsp,
  conCsp,
  esDocumentoDelVisor,
};
