'use strict';
/**
 * Preload de la ventana de chat (COMPANION.md §C2.7.2; v0.2.2: docs/V022_CORREO_DRIVE_SCRIPTS.md §13.2):
 * expone `window.amadeusChat` (versión 2 = la API de la versión 1 + tarjetas aprobables).
 * contextIsolation + sandbox; NUNCA se expone `ipcRenderer`. El main solo acepta estos mensajes si
 * vienen de `chat.webContents` y vuelve a validar todo (id de confirmación, controles, URLs, decisiones
 * de tarjeta). El renderer nunca escribe texto arbitrario en el portapapeles: `copiarCodigo` solo pasa
 * el id y el main copia el código de ESA tarjeta de script, tal como lo envió R.A.M.S.
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

const CLAVES_CAMPOS = ['para', 'cc', 'cco', 'asunto', 'cuerpo', 'adjuntos'];
const MAX_CAMPO = 10000;
const MAX_NOMBRE = 40;
const MAX_VERSION = 2 ** 31 - 1;

/**
 * Copia plana de `extra = {version?, nombre?, campos?}` de una decisión de tarjeta. `version` (la que
 * muestra el chat) solo si es un entero 1..2³¹−1; si no, null y el main rechaza toda decisión que no
 * sea `descartar`. NUNCA recorta los campos (son contenido aprobable): si alguno no es string o pasa
 * de 10 000 unidades, `campos` = null y el main rechaza la decisión `editar`. Solo pasan las 6 claves
 * conocidas.
 */
function extraPlano(extra) {
  const out = {};
  if (!extra || typeof extra !== 'object' || Array.isArray(extra)) return out;
  if ('version' in extra) {
    const v = extra.version;
    out.version = Number.isInteger(v) && v >= 1 && v <= MAX_VERSION ? v : null;
  }
  if ('nombre' in extra) out.nombre = typeof extra.nombre === 'string' && extra.nombre.length <= MAX_NOMBRE ? extra.nombre : null;
  if ('campos' in extra) {
    const c = extra.campos;
    let campos = c && typeof c === 'object' && !Array.isArray(c) ? {} : null;
    if (campos) {
      for (const clave of CLAVES_CAMPOS) {
        if (!Object.prototype.hasOwnProperty.call(c, clave)) continue;
        const v = c[clave];
        if (typeof v !== 'string' || v.length > MAX_CAMPO) {
          campos = null;
          break;
        }
        campos[clave] = v;
      }
    }
    out.campos = campos;
  }
  return out;
}

contextBridge.exposeInMainWorld('amadeusChat', {
  version: 2,
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
  // ---- versión 2: tarjetas aprobables (correo, Drive, scripts) ----
  /** Tarjeta `{…, estado:'pendiente'|'info'}` o `{id, clase, estado:'cerrada', resultado, motivo, caducada?}`. */
  onTarjeta: (cb) => suscribir('chat:tarjeta', cb),
  /**
   * Botón de una tarjeta, ligado a su `id`, a la huella y a la versión mostradas. `extra = {version,
   * nombre?}` (guardar), `{version, campos?}` (editar) o `{version}`. `true` si salió por el
   * WebSocket; con `false` el chat reactiva los botones.
   */
  decidirTarjeta: (id, sha256, decision, extra) => ipcRenderer.invoke('chat:tarjeta', primitivo(id), primitivo(sha256),
    primitivo(decision), extraPlano(extra)),
  /** Copia al portapapeles el código de la tarjeta de script `id` (lo pone el main, nunca el renderer). */
  copiarCodigo: (id) => ipcRenderer.send('chat:copiar', String(id).slice(0, 8)),
});
