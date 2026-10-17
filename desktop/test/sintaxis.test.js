'use strict';
/**
 * `node --check` de main.js, preloads y chat.js, y comprobaciones estáticas de seguridad y de
 * contrato sobre el código fuente (sin arrancar Electron).
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const raiz = path.join(__dirname, '..');
const leer = (f) => fs.readFileSync(path.join(raiz, f), 'utf8');

test('sintaxis: node --check de main.js, preloads, chat.js, lib y scripts', () => {
  const archivos = ['main.js', 'preload-overlay.js', 'preload-chat.js', 'chat.js',
    ...fs.readdirSync(path.join(raiz, 'lib')).map((f) => path.join('lib', f)),
    ...fs.readdirSync(path.join(raiz, 'scripts')).filter((f) => f.endsWith('.js')).map((f) => path.join('scripts', f))];
  for (const f of archivos) {
    const r = spawnSync(process.execPath, ['--check', path.join(raiz, f)], { encoding: 'utf8' });
    assert.equal(r.status, 0, `${f}: ${r.stderr}`);
  }
});

test('estático: archivos en UTF-8 sin BOM y con LF', () => {
  const textos = ['main.js', 'preload-overlay.js', 'preload-chat.js', 'chat.html', 'chat.css', 'chat.js',
    'package.json', 'README.md', 'hyprland.ejemplo.conf', 'hyprland.ejemplo.lua',
    ...fs.readdirSync(path.join(raiz, 'lib')).map((f) => path.join('lib', f)),
    ...fs.readdirSync(path.join(raiz, 'scripts')).map((f) => path.join('scripts', f)),
    ...fs.readdirSync(__dirname).map((f) => path.join('test', f))];
  for (const f of textos) {
    const buf = fs.readFileSync(path.join(raiz, f));
    assert.ok(!(buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf), `${f} tiene BOM`);
    assert.ok(!buf.includes(Buffer.from('\r\n')), `${f} tiene CRLF`);
    assert.doesNotThrow(() => new TextDecoder('utf-8', { fatal: true }).decode(buf), `${f} no es UTF-8`);
  }
});

test('estático: ventanas con contextIsolation, sandbox y sin nodeIntegration', () => {
  const main = leer('main.js');
  assert.equal((main.match(/contextIsolation: true/g) || []).length, 2);
  assert.equal((main.match(/sandbox: true/g) || []).length, 2);
  assert.ok(!/nodeIntegration:\s*true/.test(main));
  assert.ok(!/webSecurity:\s*false/.test(main));
  assert.ok(!/contextIsolation:\s*false/.test(main));
  assert.ok(!/shell:\s*true/.test(main));
  assert.ok(main.includes("setWindowOpenHandler(() => ({ action: 'deny' }))"));
  assert.ok(main.includes("'screen-saver'"));
  assert.ok(main.includes('focusable: WAYLAND'));
  assert.ok(main.includes('setContentProtection(true)'));
  assert.ok(main.includes('requestSingleInstanceLock(datosSegundaInstancia(process.argv))'));
});

test('estático: los preloads no exponen ipcRenderer ni cargan Node', () => {
  for (const f of ['preload-overlay.js', 'preload-chat.js']) {
    const src = leer(f);
    const requires = [...src.matchAll(/require\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => m[1]);
    assert.deepEqual(requires, ['electron'], f);
    assert.ok(!/ipcRenderer\s*[,}]/.test(src.slice(src.indexOf('exposeInMainWorld'))), `${f} expone ipcRenderer`);
  }
});

test('estático: la apertura del chat pedida por R.A.M.S. nunca llama a focus()', () => {
  const mapeo = leer('lib/mapeo.js');
  const ini = mapeo.indexOf('function abrirChatRams');
  const cuerpo = mapeo.slice(ini, mapeo.indexOf('\n  }\n', ini));
  assert.ok(cuerpo.includes('showInactive()'));
  assert.ok(!cuerpo.includes('focus'));
});

test('estático: chat.html con CSP estricta y chat.js sin innerHTML', () => {
  const html = leer('chat.html');
  assert.match(html, /http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self';/);
  assert.ok(!/unsafe-inline|unsafe-eval/.test(html));
  assert.ok(!/<script>/.test(html)); // sin scripts en línea
  const js = leer('chat.js');
  assert.ok(!/\.(innerHTML|outerHTML)\s*[+]?=|insertAdjacentHTML\s*\(|document\.write(ln)?\s*\(/.test(js));
  assert.ok(js.includes('.textContent = m.texto'));
  assert.ok(js.includes('ESPERA_ENTER_MS = 600'));
});

test('estático: package.json con electron 44.8.0 fijado y los scripts de §C2.7.9', () => {
  const pkg = JSON.parse(leer('package.json'));
  assert.equal(pkg.devDependencies.electron, '44.8.0');
  assert.equal(pkg.main, 'main.js');
  assert.equal(pkg.scripts.start, 'electron .');
  assert.equal(pkg.scripts['start:x11'], 'electron . --ozone-platform=x11');
  assert.equal(pkg.scripts.smoke, 'electron . --smoke');
  assert.equal(pkg.scripts.test, 'node --test test/');
  assert.ok(!pkg.dependencies, 'sin dependencias de ejecución');
});

test('estático: reglas de Hyprland con no_initial_focus en overlay y chat', () => {
  for (const f of ['hyprland.ejemplo.conf', 'hyprland.ejemplo.lua']) {
    const src = leer(f);
    assert.ok(src.includes('Amadeus Overlay') && src.includes('Amadeus Chat'), f);
    const n = (src.match(/no_initial_focus/g) || []).length;
    assert.ok(n >= 2, `${f}: no_initial_focus en overlay y chat`);
    assert.ok(src.includes('no_follow_mouse'), f);
    assert.ok(src.includes('--amadeus=toggle'), f);
  }
});
