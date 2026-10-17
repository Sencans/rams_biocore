'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  COMANDOS, normalizarComando, parsearArgv, datosSegundaInstancia, comandoSegundaInstancia,
} = require('../lib/cli');

const base = ['C:\\electron\\electron.exe', 'C:\\proyectos\\rams_biocore\\desktop'];

test('cli: lista cerrada de comandos de §C2.7.5', () => {
  assert.deepEqual([...COMANDOS], ['toggle', 'show', 'hide', 'chat', 'quit', 'basta', 'vision_pausa', 'clics']);
});

test('cli: --amadeus=<cmd> reconoce cada comando (sin distinguir mayúsculas)', () => {
  for (const c of COMANDOS) {
    assert.equal(parsearArgv([...base, `--amadeus=${c}`]).comando, c);
    assert.equal(parsearArgv([...base, `--amadeus=${c.toUpperCase()}`]).comando, c);
  }
});

test('cli: comando desconocido → null e inválido anotado (recortado)', () => {
  const p = parsearArgv([...base, '--amadeus=rm -rf']);
  assert.equal(p.comando, null);
  assert.equal(p.invalido, 'rm -rf');
  assert.equal(parsearArgv([...base, `--amadeus=${'x'.repeat(100)}`]).invalido.length, 32);
  assert.equal(normalizarComando('x'.repeat(40)), null);
  assert.equal(normalizarComando(undefined), null);
});

test('cli: manda el último --amadeus=; --hidden y --smoke; ignora argumentos ajenos', () => {
  const p = parsearArgv([...base, '--amadeus=show', '--no-sandbox', '--amadeus=chat', '--hidden', '--smoke', 42]);
  assert.equal(p.comando, 'chat');
  assert.equal(p.oculto, true);
  assert.equal(p.smoke, true);
  assert.equal(parsearArgv([...base, '--amadeus=show', '--amadeus=nope']).comando, null);
  assert.deepEqual(parsearArgv(null), { comando: null, invalido: null, oculto: false, smoke: false });
  assert.equal(parsearArgv(['--amadeus', 'toggle']).comando, null); // solo la forma con "="
});

test('cli: 2.ª instancia prioriza additionalData', () => {
  const datos = datosSegundaInstancia([...base, '--amadeus=toggle']);
  assert.deepEqual(datos, { v: 1, amadeus: 'toggle', oculto: false, invalido: false });
  // argv reordenado o modificado por Chromium: manda additionalData
  assert.equal(comandoSegundaInstancia([...base, '--amadeus=quit'], datos), 'toggle');
});

test('cli: 2.ª instancia sin argumentos → show; --hidden o inválido → nada', () => {
  assert.equal(comandoSegundaInstancia(base, datosSegundaInstancia(base)), 'show');
  assert.equal(comandoSegundaInstancia(base, datosSegundaInstancia([...base, '--hidden'])), null);
  assert.equal(comandoSegundaInstancia(base, datosSegundaInstancia([...base, '--amadeus=zzz'])), null);
});

test('cli: additionalData ausente o ajeno → se usa argv', () => {
  assert.equal(comandoSegundaInstancia([...base, '--amadeus=basta'], undefined), 'basta');
  assert.equal(comandoSegundaInstancia([...base, '--amadeus=clics'], { cmd: 'quit' }), 'clics');
  assert.equal(comandoSegundaInstancia([...base, '--hidden'], null), null);
  assert.equal(comandoSegundaInstancia(base, 'basura'), 'show');
  // additionalData con comando inventado no se ejecuta
  assert.equal(comandoSegundaInstancia(base, { v: 1, amadeus: 'format_c', oculto: false, invalido: true }), null);
});
