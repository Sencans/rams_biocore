'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {
  extraerImportmaps, hashScript, hashesImportmap, mismosHashes, construirCsp, conCsp, esDocumentoDelVisor,
} = require('../lib/csp');

const IMPORTMAP = `
  {
    "imports": {
      "three": "/vendor/three/build/three.module.js",
      "@pixiv/three-vrm": "/vendor/three-vrm/lib/three-vrm.module.min.js"
    }
  }
  `;
const HTML = `<!doctype html><html><head>
<script type="importmap">${IMPORTMAP}</script>
<script>console.log('otro script en línea')</script>
<script type="module" src="/app.js"></script>
</head><body></body></html>`;

const sha = (s) => `'sha256-${crypto.createHash('sha256').update(s, 'utf8').digest('base64')}'`;

test('csp: extrae solo el importmap en línea, exacto', () => {
  assert.deepEqual(extraerImportmaps(HTML), [IMPORTMAP]);
  assert.deepEqual(extraerImportmaps('<script type=importmap>{"imports":{}}</script>'), ['{"imports":{}}']);
  assert.deepEqual(extraerImportmaps("<SCRIPT TYPE='importmap'>{}</SCRIPT >"), ['{}']);
  assert.deepEqual(extraerImportmaps('<script type="importmap" src="/m.json"></script>'), []);
  assert.deepEqual(extraerImportmaps('<script type="importmapx">{}</script>'), []);
  assert.deepEqual(extraerImportmaps(null), []);
});

test('csp: hash sha256 del importmap (CRLF normalizado como el parser HTML)', () => {
  assert.deepEqual(hashesImportmap(HTML), [sha(IMPORTMAP)]);
  assert.equal(hashScript('{}'), sha('{}'));
  assert.deepEqual(hashesImportmap(HTML.replace(/\n/g, '\r\n')), [sha(IMPORTMAP)]);
  assert.deepEqual(hashesImportmap('<p>sin importmap</p>'), []);
});

test('csp: política del overlay (§C2.7.7)', () => {
  const h = sha(IMPORTMAP);
  const csp = construirCsp({ hashes: [h, "'unsafe-inline'", 'https://evil.example'] });
  const directivas = Object.fromEntries(csp.split('; ').map((d) => {
    const [k, ...v] = d.split(' ');
    return [k, v.join(' ')];
  }));
  assert.equal(directivas['script-src'], `'self' ${h}`); // sin jsDelivr ni unsafe-inline
  assert.ok(!directivas['script-src'].includes('jsdelivr'));
  assert.equal(directivas['connect-src'], "'self' https://cdn.jsdelivr.net blob: data:");
  assert.equal(directivas['img-src'], "'self' data: blob:");
  assert.equal(directivas['style-src'], "'self' 'unsafe-inline'");
  assert.equal(directivas['default-src'], "'self'");
  assert.equal(directivas['object-src'], "'none'");
  assert.equal(directivas['frame-ancestors'], "'none'");
  assert.equal(construirCsp().split('; ')[1], "script-src 'self'");
});

test('csp: conCsp sustituye cualquier CSP previa', () => {
  const out = conCsp({
    'content-type': ['text/html'],
    'Content-Security-Policy': ["script-src *"],
    'content-security-policy-report-only': ['x'],
  }, 'default-src none');
  assert.deepEqual(out, { 'content-type': ['text/html'], 'Content-Security-Policy': ['default-src none'] });
  assert.deepEqual(conCsp(undefined, 'a'), { 'Content-Security-Policy': ['a'] });
});

test('csp: solo a documentos del origen del visor', () => {
  const o = 'http://127.0.0.1:3939';
  assert.equal(esDocumentoDelVisor('http://127.0.0.1:3939/?obs=1&desktop=1', o, 'mainFrame'), true);
  assert.equal(esDocumentoDelVisor('http://127.0.0.1:3939/app.js', o, 'script'), false);
  assert.equal(esDocumentoDelVisor('http://127.0.0.1:39390/', o, 'mainFrame'), false);
  assert.equal(esDocumentoDelVisor('http://localhost:3939/', o, 'mainFrame'), false);
  assert.equal(esDocumentoDelVisor('no es url', o, 'mainFrame'), false);
});

test('csp: mismosHashes compara conjuntos', () => {
  assert.equal(mismosHashes(['a', 'b'], ['b', 'a']), true);
  assert.equal(mismosHashes(['a'], ['a', 'b']), false);
  assert.equal(mismosHashes([], null), true);
});
