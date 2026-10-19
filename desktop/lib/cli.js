'use strict';
/**
 * Línea de órdenes de Amadeus Desktop (COMPANION.md §C2.7.5). Módulo puro: sin Electron.
 *
 *   amadeus-desktop --amadeus=toggle|show|hide|chat|quit|basta|vision_pausa|clics|puter
 *                   (`puter` abre la ventanita «Conectar Puter», COMPANION.md §C2.15)
 *   amadeus-desktop --hidden        (arranque en la bandeja, sin mostrar nada)
 *   amadeus-desktop --smoke         (comprobación oculta de arranque, §C2.7.8)
 *
 * La 2.ª instancia entrega la orden a la 1.ª con `app.requestSingleInstanceLock(additionalData)`:
 * `additionalData` es más fiable que `argv`, porque Chromium puede reordenar o añadir argumentos.
 */

const COMANDOS = Object.freeze(['toggle', 'show', 'hide', 'chat', 'quit', 'basta', 'vision_pausa', 'clics', 'puter']);
const PREFIJO = '--amadeus=';
const VERSION_DATOS = 1;

/** Devuelve el comando normalizado si está en la lista cerrada; si no, `null`. */
function normalizarComando(valor) {
  if (typeof valor !== 'string' || valor.length > 32) return null;
  const c = valor.trim().toLowerCase();
  return COMANDOS.includes(c) ? c : null;
}

/**
 * Analiza `argv` (process.argv o el de la 2.ª instancia). Solo mira los argumentos propios; los
 * de Chromium/Electron se ignoran. Si hay varios `--amadeus=`, manda el último.
 * @returns {{comando: string|null, invalido: string|null, oculto: boolean, smoke: boolean}}
 */
function parsearArgv(argv) {
  const res = { comando: null, invalido: null, oculto: false, smoke: false };
  if (!Array.isArray(argv)) return res;
  for (const a of argv) {
    if (typeof a !== 'string') continue;
    if (a === '--hidden') res.oculto = true;
    else if (a === '--smoke') res.smoke = true;
    else if (a.startsWith(PREFIJO)) {
      const crudo = a.slice(PREFIJO.length);
      const c = normalizarComando(crudo);
      res.comando = c;
      res.invalido = c ? null : crudo.slice(0, 32);
    }
  }
  return res;
}

/** Datos que la 2.ª instancia pasa a `requestSingleInstanceLock` (JSON simple). */
function datosSegundaInstancia(argv) {
  const p = parsearArgv(argv);
  return { v: VERSION_DATOS, amadeus: p.comando, oculto: p.oculto, invalido: p.invalido !== null };
}

/**
 * Qué hace la 1.ª instancia cuando arranca otra: prioriza `additionalData` y, si no sirve, `argv`.
 * - comando válido → ese comando;
 * - `--hidden` (p. ej. un autoarranque duplicado) o un comando inválido → `null` (no hacer nada);
 * - sin argumentos (doble clic en el lanzador) → `'show'`.
 */
function comandoSegundaInstancia(argv, datos) {
  if (datos && typeof datos === 'object' && datos.v === VERSION_DATOS) {
    const c = normalizarComando(datos.amadeus);
    if (c) return c;
    if (datos.oculto === true || datos.invalido === true) return null;
    return 'show';
  }
  const p = parsearArgv(argv);
  if (p.comando) return p.comando;
  if (p.oculto || p.invalido !== null) return null;
  return 'show';
}

module.exports = {
  COMANDOS,
  PREFIJO,
  normalizarComando,
  parsearArgv,
  datosSegundaInstancia,
  comandoSegundaInstancia,
};
