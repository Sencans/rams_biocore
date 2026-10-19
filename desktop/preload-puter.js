'use strict';
/**
 * Preload de las páginas de Puter (COMPANION.md §C2.15): la ventana oculta (`trabajo.html`) y la
 * ventanita de inicio de sesión (`conectar.html`). Expone `window.amadeusPuter` (versión 1).
 * contextIsolation + sandbox; NUNCA se expone `ipcRenderer`. El main solo acepta cada canal de la
 * ventana que le corresponde y vuelve a validar todo. El token de R.A.M.S. nunca llega aquí.
 *
 * Solo se expone en el origen propio (`http://amadeus-puter.localhost`): la ventana emergente de
 * inicio de sesión de puter.com hereda las preferencias de su opener (también este preload), y en
 * ella `window.amadeusPuter` no debe existir.
 */
const { contextBridge, ipcRenderer } = require('electron');

const ORIGEN_PROPIO = 'http://amadeus-puter.localhost';

function suscribir(canal, cb) {
  if (typeof cb !== 'function') return () => {};
  const manejador = (_evento, dato) => {
    try {
      cb(dato);
    } catch (err) {
      console.error(`[amadeusPuter] error en el manejador de ${canal}:`, err);
    }
  };
  ipcRenderer.on(canal, manejador);
  return () => ipcRenderer.removeListener(canal, manejador);
}

const esObjeto = (v) => !!v && typeof v === 'object';
const cadena = (v, max) => (typeof v === 'string' ? v.slice(0, max) : null);
const codigo = (v) => (typeof v === 'string' && /^[a-z_]{1,24}$/.test(v) ? v : null);
const escalar = (v) => {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  return cadena(v, 200);
};

function origenActual() {
  try {
    return typeof location === 'object' && location ? String(location.origin) : '';
  } catch {
    return '';
  }
}

if (origenActual() === ORIGEN_PROPIO) contextBridge.exposeInMainWorld('amadeusPuter', {
  version: 1,
  /** Ventana oculta: pedido `{id, modelo, mensajes, maxTokens, temperatura, esfuerzo, timeoutMs}`. */
  onPedido: (cb) => suscribir('puter:pedir', cb),
  /** Ventana oculta: el main ya respondió «timeout» a ese `id`; aborta la llamada. */
  onCancelar: (cb) => suscribir('puter:cancelar', cb),
  /** `{modelo}`: modelo por defecto del escritorio. */
  onInfo: (cb) => suscribir('puter:info', cb),
  /** Ventana oculta: el main pide releer la sesión y volver a publicar el estado (sin datos). */
  onRefrescar: (cb) => suscribir('puter:refrescar', typeof cb === 'function' ? () => cb() : cb),
  /** Ventana oculta: `{cargado, sesion, error}`. */
  estado: (e) => ipcRenderer.send('puter:estado', {
    cargado: esObjeto(e) && e.cargado === true,
    sesion: esObjeto(e) && e.sesion === true,
    error: esObjeto(e) ? codigo(e.error) : null,
  }),
  /** Ventana oculta: `{id, ok, texto?, modelo?, fallo?}` (el main clasifica el fallo). */
  responder: (r) => {
    if (!esObjeto(r)) return;
    const ok = r.ok === true;
    const f = !ok && esObjeto(r.fallo) ? r.fallo : {};
    ipcRenderer.send('puter:respuesta', {
      id: cadena(r.id, 40),
      ok,
      texto: ok ? cadena(r.texto, 200000) : null,
      modelo: cadena(r.modelo, 120),
      fallo: ok ? null : {
        code: escalar(f.code),
        status: escalar(f.status),
        message: cadena(f.message, 200),
        name: cadena(f.name, 60),
        abortado: f.abortado === true,
        sinSesion: f.sinSesion === true,
      },
    });
  },
  /**
   * Ventanita de inicio de sesión: `{ok, nueva?, error?}` (`nueva` = se acaba de iniciar sesión o de
   * cambiar de cuenta con `puter.auth.signIn()`; sin ella, ya estaba iniciada).
   */
  conexion: (r) => ipcRenderer.send('puter:conexion', {
    ok: esObjeto(r) && r.ok === true,
    nueva: esObjeto(r) && r.nueva === true,
    error: esObjeto(r) ? codigo(r.error) : null,
  }),
  /** Ventanita de inicio de sesión: cerrarla. */
  cerrar: () => ipcRenderer.send('puter:cerrar'),
});
