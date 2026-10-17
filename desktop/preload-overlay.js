'use strict';
/**
 * Preload del overlay (COMPANION.md §C2.7.2): expone `window.amadeusDesktop` (versión 3) al visor de
 * VTuber AI Studio. Corre con contextIsolation y sandbox: solo `contextBridge` e `ipcRenderer`, y
 * NUNCA se expone `ipcRenderer` ni nada de Node. El main valida el origen de cada mensaje
 * (`senderFrame.origin`) y es la autoridad del click-through: `setInteractive` es una SOLICITUD.
 */
const { contextBridge, ipcRenderer } = require('electron');

function numero(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

/** Copia plana de datos escalares (el main vuelve a validar). */
function datosPlanos(datos) {
  const out = {};
  if (!datos || typeof datos !== 'object' || Array.isArray(datos)) return out;
  let n = 0;
  for (const k of Object.keys(datos)) {
    if (++n > 16) break;
    const v = datos[k];
    if (typeof v === 'number' || typeof v === 'boolean' || v === null) out[String(k).slice(0, 32)] = v;
    else if (typeof v === 'string') out[String(k).slice(0, 32)] = v.slice(0, 200);
  }
  return out;
}

/** Suscripción a un canal; devuelve la función para cancelarla. */
function suscribir(canal, cb) {
  if (typeof cb !== 'function') return () => {};
  const manejador = (_evento, dato) => {
    try {
      cb(dato);
    } catch (err) {
      console.error(`[amadeusDesktop] error en el manejador de ${canal}:`, err);
    }
  };
  ipcRenderer.on(canal, manejador);
  return () => ipcRenderer.removeListener(canal, manejador);
}

contextBridge.exposeInMainWorld('amadeusDesktop', {
  version: 3,
  /** Solicitud de modo interactivo (decide el main con el cursor global y la caja). */
  setInteractive: (v) => ipcRenderer.send('overlay:interactive', v === true),
  /** Caja del avatar en px CSS relativos a la ventana. */
  reportAvatar: (r) => {
    if (!r || typeof r !== 'object') return;
    ipcRenderer.send('overlay:rect', { x: numero(r.x), y: numero(r.y), w: numero(r.w), h: numero(r.h) });
  },
  /** Terminó la salida por el borde: el main oculta la ventana. */
  hidden: () => ipcRenderer.send('overlay:hidden'),
  /** Clic derecho sobre el avatar: acción del usuario (el chat puede enfocarse). */
  openChat: () => ipcRenderer.send('overlay:chat'),
  /** Evento hacia R.A.M.S.: solo `toque`, `soltada{altura}` y `cursor_encima` (lo filtra el main). */
  event: (nombre, datos) => ipcRenderer.send('overlay:event', String(nombre).slice(0, 32), datosPlanos(datos)),
  onCursor: (cb) => suscribir('overlay:cursor', cb),
  onOrder: (cb) => suscribir('overlay:order', cb),
  onVisibility: (cb) => suscribir('overlay:visibility', cb),
});
