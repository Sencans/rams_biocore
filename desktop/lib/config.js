'use strict';
/**
 * Configuración por entorno de Amadeus Desktop. Módulo puro: sin Electron ni E/S.
 *
 * - `origenVisor(crudo)`: `VTUBER_AI_URL` solo puede ser un origen LOCAL (§C2.12), con el mismo patrón
 *   que R.A.M.S. (`core/config.py`, `_RE_URL_LOCAL`). El overlay carga ese origen y le da el puente
 *   `window.amadeusDesktop`; un origen remoto podría inyectar eventos en R.A.M.S. y abrir el chat.
 * - `resolverAtajos(env, plataforma)`: atajos globales (Win/X11) con defecto y anulables por
 *   variable (`AMADEUS_ATAJO_*`).
 *
 * Por qué «Quieta» es `Ctrl+Alt+Shift+Q` y no `Ctrl+Alt+Q`: en Windows AltGr llega como
 * Ctrl izquierdo + Alt derecho, así que un atajo global `Ctrl+Alt+<tecla>` se come el carácter
 * AltGr de esa tecla en TODAS las apps. En el teclado latinoamericano (y el alemán) AltGr+Q es «@»:
 * cada arroba dispararía «basta». Con Shift no hay carácter en esos teclados. En Hyprland AltGr es
 * ISO_Level3_Shift y no choca, pero se usa la misma combinación para no tener dos atajos distintos.
 */

const VISOR_DEFECTO = 'http://127.0.0.1:3939';
/** Igual que `_RE_URL_LOCAL` de R.A.M.S.: esquema http(s), host local, puerto opcional, sin ruta. */
const RE_URL_LOCAL = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?\/?$/;

/**
 * Origen del visor (VTuber AI Studio). Sin la variable → defecto, sin aviso. Si no es un origen
 * local válido → defecto y un aviso (sin repetir el valor, que podría llevar credenciales).
 * @returns {{origen: string, aviso: string|null}}
 */
function origenVisor(crudo) {
  if (crudo === undefined || crudo === null || String(crudo).trim() === '') {
    return { origen: VISOR_DEFECTO, aviso: null };
  }
  const v = String(crudo).trim();
  const rechazo = {
    origen: VISOR_DEFECTO,
    aviso: 'VTUBER_AI_URL debe ser un origen local (http://127.0.0.1, localhost o [::1] con puerto ' +
      `opcional y sin ruta); uso ${VISOR_DEFECTO}`,
  };
  if (v.length > 64 || !RE_URL_LOCAL.test(v)) return rechazo;
  let u;
  try {
    u = new URL(v);
  } catch {
    return rechazo; // p. ej. puerto > 65535
  }
  if (u.port === '0') return rechazo;
  return { origen: u.origin, aviso: null };
}

// ---------------------------------------------------------------------------------------------
// Atajos globales
// ---------------------------------------------------------------------------------------------

/** Acción → [variable de entorno, acelerador por defecto]. El orden es el de registro. */
const ATAJOS_DEFECTO = Object.freeze({
  llamar: Object.freeze(['AMADEUS_ATAJO_LLAMAR', 'Control+Alt+A']),
  chat: Object.freeze(['AMADEUS_ATAJO_CHAT', 'Control+Alt+Space']),
  basta: Object.freeze(['AMADEUS_ATAJO_BASTA', 'Control+Alt+Shift+Q']),
  vision: Object.freeze(['AMADEUS_ATAJO_VISION', 'Control+Alt+V']),
  emergencia: Object.freeze(['AMADEUS_ATAJO_EMERGENCIA', 'Control+Alt+Shift+A']),
});
const ACCIONES_ATAJO = Object.freeze(Object.keys(ATAJOS_DEFECTO));
const VALORES_APAGADO = Object.freeze(['off', 'no', 'none', 'ninguno', '0', 'false']);

/** Modificadores de Electron, en forma canónica. */
const MODIFICADORES = Object.freeze({
  control: 'Control', ctrl: 'Control',
  commandorcontrol: 'CommandOrControl', cmdorctrl: 'CommandOrControl',
  alt: 'Alt', option: 'Alt', altgr: 'AltGr',
  shift: 'Shift',
  super: 'Super', meta: 'Super',
});
const TECLAS_NOMBRADAS = Object.freeze([
  'Space', 'Tab', 'Backspace', 'Delete', 'Insert', 'Return', 'Enter', 'Up', 'Down', 'Left', 'Right',
  'Home', 'End', 'PageUp', 'PageDown', 'Escape', 'Esc', 'Plus', 'PrintScreen',
]);
const PUNTUACION = Object.freeze([')', '!', '@', '#', '$', '%', '^', '&', '*', '(', ':', ';', '+', '=', '<', ',',
  '_', '-', '>', '.', '?', '/', '~', '`', '{', ']', '[', '|', '\\', '}', '"', "'"]);

/** Tecla final canónica o `null`. */
function teclaCanonica(t) {
  if (/^[A-Za-z]$/.test(t)) return t.toUpperCase();
  if (/^[0-9]$/.test(t)) return t;
  const f = /^[Ff]([1-9]|1[0-9]|2[0-4])$/.exec(t);
  if (f) return `F${f[1]}`;
  const nombrada = TECLAS_NOMBRADAS.find((n) => n.toLowerCase() === t.toLowerCase());
  if (nombrada) return nombrada;
  return PUNTUACION.includes(t) ? t : null;
}

/**
 * Normaliza un acelerador de Electron (`Ctrl+Alt+Shift+Q` → `Control+Alt+Shift+Q`). Exige al
 * menos un modificador (un atajo global sin modificador se comería esa tecla en todo el sistema).
 * @returns {string|null}
 */
function normalizarAcelerador(crudo) {
  if (typeof crudo !== 'string') return null;
  const v = crudo.trim();
  if (!v || v.length > 64) return null;
  const partes = v.split('+').map((p) => p.trim());
  if (v.endsWith('++')) partes.splice(-2, 2, '+'); // la tecla «+» escrita tal cual
  if (partes.length < 2 || partes.some((p) => !p)) return null;
  const tecla = teclaCanonica(partes.at(-1));
  if (!tecla) return null;
  const mods = [];
  for (const p of partes.slice(0, -1)) {
    const m = MODIFICADORES[p.toLowerCase()];
    if (!m || mods.includes(m)) return null;
    mods.push(m);
  }
  return [...mods, tecla].join('+');
}

/** En Windows, `Ctrl+Alt+<tecla imprimible>` sin Shift es también AltGr+<tecla>. */
function chocaConAltGr(acelerador) {
  const partes = acelerador.split('+');
  const tecla = partes.at(-1);
  const mods = partes.slice(0, -1);
  const ctrl = mods.includes('Control') || mods.includes('CommandOrControl');
  const imprimible = tecla.length === 1 || tecla === 'Plus';
  return ctrl && mods.includes('Alt') && !mods.includes('Shift') && imprimible;
}

/**
 * Atajos a registrar según el entorno. Variable sin definir o vacía → defecto; `off`/`0`/`none`
 * → sin atajo; valor inválido → defecto con aviso; un acelerador repetido solo se registra una vez.
 * @returns {{atajos: Array<{accion: string, acelerador: string}>, avisos: string[]}}
 */
function resolverAtajos(env = {}, plataforma = process.platform) {
  const atajos = [];
  const avisos = [];
  const usados = new Map();
  for (const accion of ACCIONES_ATAJO) {
    const [variable, defecto] = ATAJOS_DEFECTO[accion];
    const crudo = typeof env[variable] === 'string' ? env[variable].trim() : '';
    let acelerador = defecto;
    if (crudo) {
      if (VALORES_APAGADO.includes(crudo.toLowerCase())) continue;
      const n = normalizarAcelerador(crudo);
      if (n) {
        acelerador = n;
        if (plataforma === 'win32' && chocaConAltGr(n)) {
          avisos.push(`${variable}=${n}: en Windows Ctrl+Alt es AltGr; si tu teclado escribe un carácter ` +
            'con AltGr y esa tecla, el atajo lo tapará en todas las apps (añade Shift)');
        }
      } else {
        avisos.push(`${variable} no es un atajo válido (p. ej. Control+Alt+Shift+Q); uso ${defecto}`);
      }
    }
    if (usados.has(acelerador)) {
      avisos.push(`${acelerador} ya es el atajo de «${usados.get(acelerador)}»; «${accion}» queda sin atajo`);
      continue;
    }
    usados.set(acelerador, accion);
    atajos.push({ accion, acelerador });
  }
  return { atajos, avisos };
}

module.exports = {
  VISOR_DEFECTO,
  RE_URL_LOCAL,
  ATAJOS_DEFECTO,
  ACCIONES_ATAJO,
  origenVisor,
  normalizarAcelerador,
  chocaConAltGr,
  resolverAtajos,
};
