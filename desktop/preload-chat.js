'use strict';
/**
 * Preload de la ventana de chat (COMPANION.md §C2.7.2): expone `window.amadeusChat` (versión 1).
 * contextIsolation + sandbox; NUNCA se expone `ipcRenderer`. El main solo acepta estos mensajes si
 * vienen de `chat.webContents` y vuelve a validar todo (id de confirmación, controles, URLs).
 */
const { contextBridge, ipcRenderer } = require('electron');

function suscribir(canal, cb) {
  if (typeof cb !== 'function') return () => {};
  const manejador = (_evento, dato) => {
    try {
      cb(dato);
    } catch (err) {
      console.error(`[amadeusChat] error en el manejador de ${canal}:`, err);
    }
  };
  ipcRenderer.on(canal, manejador);
  return () => ipcRenderer.removeListener(canal, manejador);
}

const primitivo = (v) => (typeof v === 'string' || typeof v === 'boolean' || typeof v === 'number' ? v : null);

contextBridge.exposeInMainWorld('amadeusChat', {
  version: 1,
  /** Envía el texto a R.A.M.S.; `true` si salió por el WebSocket. */
  send: (texto) => ipcRenderer.invoke('chat:send', String(texto ?? '').slice(0, 2000)),
  /** `{de:'amadeus'|'yo'|'sistema', texto, url?}` */
  onMessage: (cb) => suscribir('chat:msg', cb),
  /** `'en_linea' | 'desconectada'` */
  onStatus: (cb) => suscribir('chat:status', cb),
  /** `true` mientras Amadeus habla. */
  onVoz: (cb) => suscribir('chat:voz', cb),
  /** `{id, descripcion, vista_previa?, pendiente, aprobado?, caducada?}` */
  onPermiso: (cb) => suscribir('chat:permiso', cb),
  /**
   * Botón de confirmación ligado al `id` de la solicitud. `true` si salió por el WebSocket; con
   * `false` (R.A.M.S. desconectada) el chat vuelve a habilitar los botones y lo avisa.
   */
  confirmar: (id, aprobado) => ipcRenderer.invoke('chat:confirm', primitivo(id), primitivo(aprobado)),
  /** `'no_molestar_on' | 'no_molestar_off' | 'basta_travesuras'` */
  control: (accion) => ipcRenderer.send('chat:control', String(accion).slice(0, 32)),
  /** Abre en el navegador una URL que R.A.M.S. ofreció antes (solo tras un clic). */
  abrirEnlace: (url) => ipcRenderer.send('chat:open-url', String(url).slice(0, 301)),
  /** Oculta la ventana del chat. */
  hide: () => ipcRenderer.send('chat:hide'),
});
