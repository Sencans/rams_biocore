'use strict';
/**
 * Punto de entrada de `node --test test/` (script `npm test`).
 *
 * En Node 22+/24, `node --test <directorio>` ya no recorre la carpeta: trata el argumento como un
 * módulo y resuelve `test/index.js`. Este archivo carga todas las pruebas `*.test.js` de la carpeta
 * en el mismo proceso, así `node --test test/` ejecuta la suite completa. (También funciona
 * `node --test "test/*.test.js"`.)
 */
const fs = require('node:fs');
const path = require('node:path');

for (const nombre of fs.readdirSync(__dirname).filter((n) => n.endsWith('.test.js')).sort()) {
  require(path.join(__dirname, nombre));
}
