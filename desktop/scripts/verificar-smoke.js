'use strict';
/**
 * Ejecuta `electron . --smoke` y comprueba lo que exige COMPANION.md §C2.7.8: el proceso termina en
 * ≤ 5 s, con código 0 y escribiendo "smoke OK". No abre ventanas visibles, ni bandeja, ni red.
 * Uso: `node scripts/verificar-smoke.js [repeticiones]` (desde desktop/).
 *
 * Muestra por separado cuándo llegó "smoke OK" y cuánto tardó después Chromium en cerrar el proceso
 * (eso último no depende de main.js: crece con la carga de la máquina). Si una ejecución solo falla
 * por tiempo (código 0 y "smoke OK" a tiempo), se repite UNA vez y se informa como LENTO; un código
 * distinto de 0, la falta de "smoke OK" o dos lentas seguidas son FALLO.
 */
const { spawn } = require('node:child_process');
const path = require('node:path');

const LIMITE_MS = 5000;
const raiz = path.join(__dirname, '..');
const repeticiones = Math.max(1, Math.min(20, Number(process.argv[2]) || 1));

let electron;
try {
  electron = require('electron'); // en Node devuelve la ruta del binario
} catch {
  console.error('No encuentro el paquete electron: ejecuta "npm i" en desktop/.');
  process.exit(2);
}

function una() {
  return new Promise((resolve) => {
    const t0 = Date.now();
    let salida = '';
    let tOk = null;
    const hijo = spawn(electron, [raiz, '--smoke'], { cwd: raiz, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    hijo.stdout.on('data', (d) => {
      salida += d;
      if (tOk === null && /smoke OK/.test(salida)) tOk = Date.now() - t0;
    });
    hijo.stderr.on('data', (d) => { salida += d; });
    const matar = setTimeout(() => hijo.kill(), LIMITE_MS + 10000);
    hijo.on('exit', (codigo) => {
      clearTimeout(matar);
      const ms = Date.now() - t0;
      const funciona = codigo === 0 && tOk !== null && tOk <= LIMITE_MS;
      resolve({ codigo, tOk, ms, funciona, aTiempo: funciona && ms <= LIMITE_MS, salida });
    });
  });
}

function describir(r) {
  const cierre = r.tOk === null ? '—' : `${r.ms - r.tOk} ms`;
  return `código ${r.codigo}, "smoke OK" a los ${r.tOk ?? '—'} ms, fin a los ${r.ms} ms (cierre de Chromium: ${cierre})`;
}

(async () => {
  let fallos = 0;
  for (let i = 0; i < repeticiones; i++) {
    let r = await una();
    if (r.funciona && !r.aTiempo) {
      console.log(`LENTO: ${describir(r)}; repito una vez (¿máquina cargada?)`);
      r = await una();
    }
    console.log(`${r.aTiempo ? 'CORRECTO' : 'FALLO'}: ${describir(r)}`);
    if (!r.aTiempo) {
      fallos += 1;
      process.stdout.write(r.salida);
    }
  }
  process.exit(fallos ? 1 : 0);
})();
