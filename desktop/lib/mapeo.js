'use strict';
/**
 * Mapeo WS `/escritorio` ↔ IPC y dueño de la visibilidad (COMPANION.md §C2.7.2, §C2.4, §C1.2, §C1.11,
 * §C1.12). Módulo puro: no importa Electron; todo efecto pasa por los `puertos` inyectados, así los
 * tests verifican qué se llama (p. ej. que una apertura del chat pedida por R.A.M.S. usa
 * `showInactive()` y NUNCA `focus()`).
 *
 * Tabla WS → IPC:
 *   hola        → chat:status 'en_linea'
 *   subtitulo   → chat:msg {de:'amadeus', texto}
 *   sistema     → chat:msg {de:'sistema', texto}
 *   voz         → chat:voz (bool)
 *   permiso     → chat:permiso {id, descripcion, vista_previa?, pendiente, aprobado?}
 *   orden aparecer   → overlay.showInactive() + overlay:visibility(true) + overlay:order{aparecer} + WS evento{mostrada}
 *   orden esconderse → overlay:order{esconderse}; al llegar overlay:hidden → overlay.hide() +
 *                      overlay:visibility(false) + WS evento{ocultada}
 *   orden abrir_chat → chat.showInactive()   (nunca focus: lo pidió R.A.M.S., no el usuario)
 *   orden notificar  → Notification + chat:msg {de:'amadeus', texto, url?}
 *   estado      → overlay:order {accion:'estado', aburrimiento, fatiga, valencia}
 *   travesuras  → casilla "Quieta" de la bandeja
 *   no_molestar → casilla "No molestar" de la bandeja = `datos.usuario` (el estado REAL de R.A.M.S.,
 *                 también si se cambió desde el HUD; §C2.4)
 *   vision      → {camara: bool}: indicador de cámara (bandeja + overlay:order{accion:'camara'},
 *                 §C2.13). Lo envía escritorio-ws; sin ese mensaje no se muestra nada.
 *
 * Confirmaciones tras reconectar (como el panel RINE del HUD, §C2.4): al llegar `hola`, las que
 * estaban pendientes pasan a revisión; si la instantánea las trae como pendientes siguen vigentes
 * (el chat reactiva sus botones) y si no, al primer mensaje que no es `permiso` se cierran con
 * `chat:permiso {id, pendiente:false, caducada:true}`.
 *
 * Puertos que espera `crearEscritorio(puertos, opciones)`:
 *   overlay: { showInactive(), hide() }                 ventana del overlay (adaptada por main.js)
 *   chat:    { showInactive(), show(), focus(), hide(), isVisible(), isFocused() }
 *   overlayListo(): boolean                             el visor está cargado en el overlay
 *   enviarOverlay(canal, datos), enviarChat(canal, datos)
 *   enviarWs(obj): boolean                              true si salió por el WebSocket
 *   forzarClickThrough(motivo)                          autoridad de hit.js + setIgnoreMouseEvents(true)
 *   notificar({titulo, texto, url})                     notificación del sistema
 *   abrirExterno(url)                                   shell.openExternal (ya validada)
 *   temporizador(fn, ms) → cancelar()                   setTimeout inyectable
 *   ahora(): number                                     reloj inyectable (ms)
 *   bordePreferido?(): 'left'|'right'                   borde más cercano al cursor
 *   alCambiarVisibilidad?(visible), alCambiarEstado?(), fijarQuieta?(bool), fijarNoMolestar?(bool)
 */

const ACCIONES_ORDEN = Object.freeze(['aparecer', 'esconderse', 'abrir_chat', 'notificar']);
const EVENTOS_ESCRITORIO = Object.freeze(['toque', 'soltada', 'cursor_encima', 'mostrada', 'ocultada', 'chat_abierto']);
/** Eventos que puede emitir el renderer; `mostrada`/`ocultada`/`chat_abierto` los emite main. */
const EVENTOS_RENDERER = Object.freeze(['toque', 'soltada', 'cursor_encima']);
const CONTROLES = Object.freeze(['no_molestar_on', 'no_molestar_off', 'basta_travesuras', 'pausar_vision']);
/** Controles que expone el chat (`amadeusChat.control`). */
const CONTROLES_CHAT = Object.freeze(['no_molestar_on', 'no_molestar_off', 'basta_travesuras']);
const BORDES = Object.freeze(['left', 'right']);
const MODOS = Object.freeze(['entrar', 'asomarse']);

const RE_ID = /^[0-9a-f]{8}$/;
const MAX_URL = 300;
const MAX_TEXTO_CHAT = 2000;
const MAX_TEXTO_MSG = 4000;
const MAX_NOTIFICAR = 300;
const MAX_DESCRIPCION = 500;
const MAX_VISTA_PREVIA = 500;
const MAX_DATOS_EVENTO = 1024;
const INTERVALO_CURSOR_ENCIMA_MS = 10000;
/** Si el renderer no confirma la salida por el borde (`overlay:hidden`), se oculta igual. */
const ESPERA_SALIDA_MS = 12000;
/** Una orden `aparecer` que llega con el visor sin cargar se guarda este tiempo. */
const ESPERA_APARECER_MS = 60000;
/** Tras `aparecer`, si el renderer no informa ninguna caja en este tiempo, se reenvía la orden una vez. */
const ESPERA_CAJA_MS = 2000;
/**
 * Red de seguridad de la revisión de confirmaciones: la instantánea siempre termina en
 * `no_molestar` (§C2.4), pero si no llegara nada más, la revisión se cierra igual.
 */
const ESPERA_REVISION_MS = 3000;
const MAX_PERMISOS_SEGUIDOS = 32;

const AVISOS = Object.freeze({
  no_molestar_on: 'No molestar activado: me escondo y no te busco hasta que lo quites.',
  no_molestar_off: 'No molestar desactivado.',
  basta_travesuras: 'Vale, quieta: nada de travesuras con el ratón.',
  pausar_vision: 'Visión en pausa.',
});
const AVISOS_SIN_CONEXION = Object.freeze({
  no_molestar_on: 'No molestar activado aquí; se lo diré a R.A.M.S. en cuanto se conecte.',
  no_molestar_off: 'No molestar desactivado aquí; se lo diré a R.A.M.S. en cuanto se conecte.',
  basta_travesuras: 'Vale, quieta (R.A.M.S. no está conectada).',
  pausar_vision: 'R.A.M.S. no está conectada: no pude pausar la visión.',
});

// ---------------------------------------------------------------------------------------------
// Validaciones puras
// ---------------------------------------------------------------------------------------------

/** Texto seguro para mostrar: string, sin NUL ni controles (salvo \n y \t), recortado. */
function textoSeguro(v, max) {
  if (typeof v !== 'string') return '';
  // eslint-disable-next-line no-control-regex
  const limpio = v.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
  return limpio.length > max ? limpio.slice(0, max) : limpio;
}

/** `chat:confirm`: id `^[0-9a-f]{8}$` y booleano estricto. */
function validarConfirmacion(id, aprobado) {
  if (typeof id !== 'string' || !RE_ID.test(id)) return null;
  if (typeof aprobado !== 'boolean') return null;
  return { id, aprobado };
}

/** URL `https://` de ≤ 300 caracteres, bien formada, sin credenciales. Devuelve la original o `null`. */
function validarUrlHttps(url) {
  if (typeof url !== 'string' || url.length === 0 || url.length > MAX_URL) return null;
  if (!/^https:\/\//.test(url)) return null;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000- \u007f]/.test(url)) return null;
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' || !u.hostname || u.username || u.password) return null;
  return url;
}

/** `datos` de un evento del renderer: solo claves cortas con valores escalares, ≤ 1 KiB. */
function datosEscalares(datos) {
  if (datos === undefined || datos === null) return {};
  if (typeof datos !== 'object' || Array.isArray(datos)) return null;
  const out = {};
  let n = 0;
  for (const [k, v] of Object.entries(datos)) {
    if (++n > 16 || typeof k !== 'string' || k.length > 32) return null;
    if (typeof v === 'number') {
      if (!Number.isFinite(v)) return null;
      out[k] = v;
    } else if (typeof v === 'boolean' || v === null) {
      out[k] = v;
    } else if (typeof v === 'string') {
      out[k] = v.slice(0, 200);
    } else {
      return null;
    }
  }
  return JSON.stringify(out).length <= MAX_DATOS_EVENTO ? out : null;
}

function numeroEn(v, min, max) {
  const n = typeof v === 'number' ? v : Number.NaN;
  if (!Number.isFinite(n)) return null;
  return Math.min(max, Math.max(min, n));
}

/** Conjunto acotado de URLs que R.A.M.S. ofreció (solo esas se pueden abrir desde el chat). */
class UrlsOfrecidas {
  constructor(max = 64) {
    this.max = max;
    this.urls = new Set();
  }

  agregar(url) {
    const v = validarUrlHttps(url);
    if (!v) return false;
    this.urls.delete(v);
    this.urls.add(v);
    while (this.urls.size > this.max) this.urls.delete(this.urls.values().next().value);
    return true;
  }

  contiene(url) {
    return typeof url === 'string' && this.urls.has(url);
  }
}

// ---------------------------------------------------------------------------------------------
// Controlador
// ---------------------------------------------------------------------------------------------

function crearEscritorio(puertos, opciones = {}) {
  const p = puertos;
  const ahora = typeof p.ahora === 'function' ? p.ahora : Date.now;
  const temporizador = typeof p.temporizador === 'function'
    ? p.temporizador
    : (fn, ms) => { const t = setTimeout(fn, ms); return () => clearTimeout(t); };
  const pid = Number.isInteger(opciones.pid) ? opciones.pid : 0;
  const version = typeof opciones.version === 'string' ? opciones.version : '0.0.0';
  const urls = new UrlsOfrecidas();

  const st = {
    visible: false,          // la ventana del overlay está mostrada
    saliendo: false,         // se pidió esconderse y falta overlay:hidden
    cancelarSalida: null,
    pendienteAparecer: null, // {borde, modo, ts}
    conectado: false,        // WebSocket abierto
    enLinea: false,          // llegó el `hola` de R.A.M.S.
    noMolestar: false,       // "no molestar" del USUARIO: el último `no_molestar.usuario` de R.A.M.S.
    noMolestarPendiente: false, // se cambió aquí sin conexión: se le dice a R.A.M.S. al reconectar
    quieta: null,            // null = aún no se sabe
    camara: false,           // cámara de R.A.M.S. abierta (mensaje `vision`); se conserva sin conexión
    ultimoCursorEncima: -Infinity,
    esperandoCaja: false,    // tras `aparecer`, hasta la primera caja del renderer
    cancelarReenvio: null,
  };

  /** Confirmaciones que el chat muestra como pendientes (ids) y su revisión tras reconectar. */
  const perm = {
    pendientes: new Set(),
    revision: new Set(),
    revisando: false,
    cancelar: null,
    ultimo: null, // último `chat:permiso` pendiente: se reenvía si la página del chat se recarga
  };

  const llamar = (fn, ...args) => (typeof fn === 'function' ? fn(...args) : undefined);
  const notificarEstado = () => llamar(p.alCambiarEstado);
  const enviarWs = (obj) => llamar(p.enviarWs, obj) === true;
  const evento = (nombre, datos = {}) => enviarWs({ tipo: 'evento', nombre, datos });
  const avisar = (texto) => llamar(p.enviarChat, 'chat:msg', { de: 'sistema', texto });

  // ---- confirmaciones: revisión al reconectar ----

  function cancelarTemporizadorRevision() {
    if (perm.cancelar) {
      perm.cancelar();
      perm.cancelar = null;
    }
  }

  /** `hola`: las pendientes mostradas pasan a revisión hasta ver la instantánea. */
  function abrirRevision() {
    for (const id of perm.pendientes) perm.revision.add(id);
    perm.pendientes.clear();
    perm.ultimo = null;
    if (perm.revision.size === 0) return;
    perm.revisando = true;
    cancelarTemporizadorRevision();
    perm.cancelar = temporizador(() => {
      perm.cancelar = null;
      if (perm.revisando) cerrarRevision();
    }, ESPERA_REVISION_MS);
  }

  /** Fin de la instantánea: lo que no volvió como pendiente ya no lo está. */
  function cerrarRevision() {
    cancelarTemporizadorRevision();
    perm.revisando = false;
    for (const id of perm.revision) p.enviarChat('chat:permiso', { id, pendiente: false, caducada: true });
    perm.revision.clear();
  }

  function seguirPermiso(m) {
    perm.revision.delete(m.id);
    if (m.pendiente) {
      perm.pendientes.clear(); // R.A.M.S. solo tiene una pendiente a la vez (el chat cierra las demás)
      perm.pendientes.add(m.id);
      perm.ultimo = m;
    } else {
      perm.pendientes.delete(m.id);
      if (perm.ultimo && perm.ultimo.id === m.id) perm.ultimo = null;
    }
    while (perm.revision.size > MAX_PERMISOS_SEGUIDOS) perm.revision.delete(perm.revision.values().next().value);
  }

  // ---- cámara (§C2.13) ----

  function ordenarCamara() {
    if (llamar(p.overlayListo)) p.enviarOverlay('overlay:order', { accion: 'camara', activa: st.camara });
  }

  function cancelarSalida() {
    if (st.cancelarSalida) {
      st.cancelarSalida();
      st.cancelarSalida = null;
    }
  }

  function borde(b) {
    if (BORDES.includes(b)) return b;
    const pref = llamar(p.bordePreferido);
    return BORDES.includes(pref) ? pref : 'right';
  }

  // ---- visibilidad (main.js es el ÚNICO dueño) ----

  function aparecer({ borde: b, modo } = {}) {
    const bordeFinal = borde(b);
    const modoFinal = MODOS.includes(modo) ? modo : 'entrar';
    if (!llamar(p.overlayListo)) {
      st.pendienteAparecer = { borde: bordeFinal, modo: modoFinal, ts: ahora() };
      avisar('El visor de Amadeus (VTuber AI Studio) aún no está listo: apareceré en cuanto cargue.');
      return false;
    }
    st.pendienteAparecer = null;
    if (st.visible && !st.saliendo) {
      evento('mostrada'); // ya está fuera: solo resincroniza a R.A.M.S.
      return true;
    }
    cancelarSalida();
    st.saliendo = false;
    st.visible = true;
    p.overlay.showInactive();
    ordenarAparecer(bordeFinal, modoFinal);
    evento('mostrada');
    llamar(p.alCambiarVisibilidad, true);
    notificarEstado();
    return true;
  }

  /**
   * Manda al renderer la visibilidad y la orden de entrada. Si en 2 s no informa ninguna caja (p. ej.
   * el visor aún no había registrado `onOrder`), la reenvía UNA vez.
   */
  function ordenarAparecer(b, modo) {
    p.enviarOverlay('overlay:visibility', true);
    p.enviarOverlay('overlay:order', { accion: 'aparecer', borde: b, modo });
    if (st.cancelarReenvio) st.cancelarReenvio();
    st.esperandoCaja = true;
    st.cancelarReenvio = temporizador(() => {
      st.cancelarReenvio = null;
      if (!st.esperandoCaja || !st.visible || st.saliendo || !llamar(p.overlayListo)) return;
      st.esperandoCaja = false;
      p.enviarOverlay('overlay:visibility', true);
      p.enviarOverlay('overlay:order', { accion: 'aparecer', borde: b, modo });
    }, ESPERA_CAJA_MS);
  }

  /** El renderer informó una caja válida: está vivo y siguiendo las órdenes. */
  function alCajaRecibida() {
    st.esperandoCaja = false;
    if (st.cancelarReenvio) {
      st.cancelarReenvio();
      st.cancelarReenvio = null;
    }
  }

  function esconderse() {
    st.pendienteAparecer = null;
    if (!st.visible || st.saliendo) return false;
    st.saliendo = true;
    p.enviarOverlay('overlay:order', { accion: 'esconderse' });
    cancelarSalida();
    st.cancelarSalida = temporizador(() => {
      st.cancelarSalida = null;
      if (st.saliendo) ocultarYa('sin_confirmacion');
    }, ESPERA_SALIDA_MS);
    notificarEstado();
    return true;
  }

  function ocultarYa(motivo) {
    cancelarSalida();
    alCajaRecibida(); // cancela un reenvío de `aparecer` pendiente
    const estaba = st.visible;
    st.visible = false;
    st.saliendo = false;
    p.overlay.hide();
    llamar(p.forzarClickThrough, motivo);
    p.enviarOverlay('overlay:visibility', false);
    if (estaba) evento('ocultada');
    llamar(p.alCambiarVisibilidad, false);
    notificarEstado();
  }

  /** `overlay:hidden`: el renderer terminó de salir por el borde. Se ignora si no se esperaba. */
  function alHidden() {
    if (!st.visible || !st.saliendo) return false;
    ocultarYa('renderer');
    return true;
  }

  /** Emergencia (Ctrl+Alt+Shift+A, bandeja, CLI `clics`): oculta ya y fuerza el click-through. */
  function emergencia() {
    st.pendienteAparecer = null;
    if (st.visible) ocultarYa('emergencia');
    else llamar(p.forzarClickThrough, 'emergencia');
    return true;
  }

  function alternar() {
    return st.visible && !st.saliendo ? esconderse() : aparecer({});
  }

  // ---- chat ----

  /** Apertura pedida por R.A.M.S.: sin foco, NUNCA `focus()`. */
  function abrirChatRams() {
    p.chat.showInactive();
    return true;
  }

  /** Apertura por acción del usuario (bandeja, atajo, clic derecho, CLI): puede enfocar. */
  function abrirChatUsuario({ alternar: alt = false } = {}) {
    if (alt && llamar(p.chat.isVisible) && llamar(p.chat.isFocused)) {
      p.chat.hide();
      return true;
    }
    p.chat.show();
    p.chat.focus();
    evento('chat_abierto');
    return true;
  }

  // ---- WS → app ----

  function alOrden(d) {
    switch (d.accion) {
      case 'aparecer':
        return aparecer({ borde: d.borde, modo: d.modo });
      case 'esconderse':
        return esconderse();
      case 'abrir_chat':
        return abrirChatRams();
      case 'notificar': {
        const texto = textoSeguro(d.texto, MAX_NOTIFICAR).trim();
        if (!texto) return false;
        const url = d.url === undefined || d.url === null ? null : validarUrlHttps(d.url);
        const msg = { de: 'amadeus', texto };
        if (url) {
          urls.agregar(url);
          msg.url = url;
        }
        llamar(p.notificar, { titulo: 'Amadeus', texto, url });
        p.enviarChat('chat:msg', msg);
        return true;
      }
      default:
        return false;
    }
  }

  function alMensajeWs(msg) {
    if (!msg || typeof msg !== 'object' || Array.isArray(msg) || typeof msg.tipo !== 'string') return false;
    const d = msg.datos && typeof msg.datos === 'object' && !Array.isArray(msg.datos) ? msg.datos : {};
    // La instantánea trae primero el permiso vigente: el primer mensaje que no es `permiso` (siempre
    // llega `no_molestar`) cierra la revisión de las confirmaciones.
    if (perm.revisando && msg.tipo !== 'permiso' && msg.tipo !== 'hola') cerrarRevision();
    switch (msg.tipo) {
      case 'hola':
        st.enLinea = true;
        abrirRevision();
        p.enviarChat('chat:status', 'en_linea');
        notificarEstado();
        return true;
      case 'subtitulo': {
        const texto = textoSeguro(d.texto, MAX_TEXTO_MSG);
        if (!texto.trim()) return false;
        p.enviarChat('chat:msg', { de: 'amadeus', texto });
        return true;
      }
      case 'sistema': {
        const texto = textoSeguro(d.texto, MAX_TEXTO_MSG);
        if (!texto.trim()) return false;
        p.enviarChat('chat:msg', { de: 'sistema', texto });
        return true;
      }
      case 'voz':
        p.enviarChat('chat:voz', d.hablando === true);
        return true;
      case 'permiso': {
        if (typeof d.id !== 'string' || !RE_ID.test(d.id)) return false;
        const m = {
          id: d.id,
          descripcion: textoSeguro(d.descripcion, MAX_DESCRIPCION) || 'Acción pendiente de confirmación',
          pendiente: d.pendiente === true,
        };
        const vista = textoSeguro(d.vista_previa, MAX_VISTA_PREVIA);
        if (vista) m.vista_previa = vista;
        if (d.destructiva === true) m.destructiva = true;
        if (!m.pendiente && typeof d.aprobado === 'boolean') m.aprobado = d.aprobado;
        seguirPermiso(m);
        p.enviarChat('chat:permiso', m);
        return true;
      }
      case 'orden':
        return ACCIONES_ORDEN.includes(d.accion) ? alOrden(d) : false;
      case 'estado': {
        if (!llamar(p.overlayListo)) return false;
        const o = { accion: 'estado' };
        const ab = numeroEn(d.aburrimiento, 0, 1);
        const fa = numeroEn(d.fatiga, 0, 1);
        const va = numeroEn(d.valencia, -1, 1);
        if (ab !== null) o.aburrimiento = ab;
        if (fa !== null) o.fatiga = fa;
        if (va !== null) o.valencia = va;
        p.enviarOverlay('overlay:order', o);
        return true;
      }
      case 'travesuras': {
        st.quieta = d.activas !== true;
        llamar(p.fijarQuieta, st.quieta);
        notificarEstado();
        return true;
      }
      case 'no_molestar': {
        // Estado REAL del "no molestar" del usuario (también si lo cambió el HUD). El automático
        // del sistema (pantalla completa, juego…) no marca la casilla.
        st.noMolestar = d.usuario === true;
        st.noMolestarPendiente = false;
        llamar(p.fijarNoMolestar, st.noMolestar);
        notificarEstado();
        return true;
      }
      case 'vision': {
        if (typeof d.camara !== 'boolean') return false;
        if (d.camara !== st.camara) {
          st.camara = d.camara;
          ordenarCamara();
          notificarEstado();
        }
        return true;
      }
      default:
        return false;
    }
  }

  // ---- app → WS ----

  /** `overlay:event` del renderer: solo `toque`, `soltada`, `cursor_encima` (≤ 1 cada 10 s). */
  function alEventoOverlay(nombre, datos) {
    if (typeof nombre !== 'string' || !EVENTOS_RENDERER.includes(nombre)) return false;
    let out;
    if (nombre === 'cursor_encima') {
      const t = ahora();
      if (t - st.ultimoCursorEncima < INTERVALO_CURSOR_ENCIMA_MS) return false;
      st.ultimoCursorEncima = t;
      out = {};
    } else if (nombre === 'soltada') {
      const a = numeroEn(datos && typeof datos === 'object' ? datos.altura : undefined, 0, 1);
      out = { altura: a === null ? 0 : a };
    } else {
      out = datosEscalares(datos);
      if (out === null) out = {};
    }
    return evento(nombre, out);
  }

  /** `chat:confirm` → `{tipo:'confirmacion', id, aprobado}`. */
  function alConfirmar(id, aprobado) {
    const v = validarConfirmacion(id, aprobado);
    if (!v) return false;
    return enviarWs({ tipo: 'confirmacion', id: v.id, aprobado: v.aprobado });
  }

  /** `chat:send` → `{tipo:'chat', texto}` (1..2000). */
  function alChatSend(texto) {
    if (typeof texto !== 'string') return false;
    const t = texto.trim();
    if (!t) return false;
    return enviarWs({ tipo: 'chat', texto: t.slice(0, MAX_TEXTO_CHAT) });
  }

  /**
   * Controles: del chat (`origen:'chat'`, lista cerrada sin `pausar_vision`) o locales (bandeja,
   * atajos, CLI). `basta_travesuras` y `no_molestar_on` además la esconden aquí mismo, sin esperar
   * a R.A.M.S. (funciona aunque esté desconectada).
   */
  function control(accion, origen = 'local') {
    const permitidos = origen === 'chat' ? CONTROLES_CHAT : CONTROLES;
    if (typeof accion !== 'string' || !permitidos.includes(accion)) return false;
    const esNoMolestar = accion === 'no_molestar_on' || accion === 'no_molestar_off';
    if (esNoMolestar) {
      st.noMolestar = accion === 'no_molestar_on';
      llamar(p.fijarNoMolestar, st.noMolestar);
    }
    const enviado = enviarWs({ tipo: 'control', accion });
    if (esNoMolestar) st.noMolestarPendiente = !enviado;
    if (accion === 'basta_travesuras' || accion === 'no_molestar_on') esconderse();
    avisar(enviado ? AVISOS[accion] : AVISOS_SIN_CONEXION[accion]);
    notificarEstado();
    return true;
  }

  /** `chat:open-url`: solo `https://` ≤ 300 y solo si R.A.M.S. la ofreció antes. */
  function alAbrirEnlace(url) {
    const v = validarUrlHttps(url);
    if (!v || !urls.contiene(v)) return false;
    llamar(p.abrirExterno, v);
    return true;
  }

  // ---- ciclo de vida ----

  function alConectado() {
    st.conectado = true;
    enviarWs({ tipo: 'hola_app', pid, version });
    evento(st.visible && !st.saliendo ? 'mostrada' : 'ocultada');
    // El "no molestar" manda R.A.M.S. (mensaje `no_molestar` de la instantánea). Solo se le dice
    // algo si el usuario lo cambió AQUÍ sin conexión; nunca se reafirma un estado que R.A.M.S. ya
    // cambió (p. ej. un "off" desde el HUD), porque `no_molestar_on` vuelve a pausar 24 h (§C2.14.9).
    if (st.noMolestarPendiente) {
      if (enviarWs({ tipo: 'control', accion: st.noMolestar ? 'no_molestar_on' : 'no_molestar_off' })) {
        st.noMolestarPendiente = false;
      }
    }
    notificarEstado();
  }

  function alDesconectado() {
    const estaba = st.conectado || st.enLinea;
    st.conectado = false;
    st.enLinea = false;
    // Sin conexión la revisión no avanza: sigue abierta y se cierra tras el próximo `hola`.
    cancelarTemporizadorRevision();
    perm.revisando = false;
    if (estaba) p.enviarChat('chat:status', 'desconectada');
    notificarEstado();
  }

  /** El visor terminó de cargar en el overlay (primera carga o recarga). */
  function alOverlayListo() {
    const pend = st.pendienteAparecer;
    st.pendienteAparecer = null;
    if (st.camara) ordenarCamara(); // el visor recién cargado no sabe que la cámara está abierta
    if (st.visible && !st.saliendo) {
      // Se recargó estando fuera: el renderer nuevo empieza de cero y hay que volver a sacarla.
      ordenarAparecer(borde(pend && pend.borde), 'entrar');
      return true;
    }
    if (st.visible && st.saliendo) {
      ocultarYa('recarga');
      return false;
    }
    if (pend && ahora() - pend.ts <= ESPERA_APARECER_MS) return aparecer(pend);
    p.enviarOverlay('overlay:visibility', false);
    return false;
  }

  /** El renderer del overlay murió o se recarga: fuera de pantalla y click-through. */
  function alOverlayCaido() {
    if (st.visible) ocultarYa('caida');
    else llamar(p.forzarClickThrough, 'caida');
  }

  /**
   * Estado actual del chat al (re)cargarse su página: la confirmación pendiente (si la página se
   * recargó, la había perdido) y después el estado de la conexión.
   */
  function alChatCargado() {
    if (perm.ultimo) p.enviarChat('chat:permiso', perm.ultimo);
    p.enviarChat('chat:status', st.enLinea ? 'en_linea' : 'desconectada');
  }

  function estado() {
    return {
      visible: st.visible,
      saliendo: st.saliendo,
      conectado: st.conectado,
      enLinea: st.enLinea,
      noMolestar: st.noMolestar,
      quieta: st.quieta,
      camara: st.camara,
      aparecerPendiente: st.pendienteAparecer !== null,
    };
  }

  return {
    aparecer,
    esconderse,
    alternar,
    emergencia,
    alHidden,
    alCajaRecibida,
    abrirChatRams,
    abrirChatUsuario,
    alMensajeWs,
    alEventoOverlay,
    alConfirmar,
    alChatSend,
    control,
    alAbrirEnlace,
    alConectado,
    alDesconectado,
    alOverlayListo,
    alOverlayCaido,
    alChatCargado,
    estado,
    urlsOfrecidas: urls,
  };
}

module.exports = {
  ACCIONES_ORDEN,
  EVENTOS_ESCRITORIO,
  EVENTOS_RENDERER,
  CONTROLES,
  CONTROLES_CHAT,
  RE_ID,
  MAX_URL,
  MAX_TEXTO_CHAT,
  ESPERA_SALIDA_MS,
  ESPERA_APARECER_MS,
  ESPERA_CAJA_MS,
  ESPERA_REVISION_MS,
  INTERVALO_CURSOR_ENCIMA_MS,
  textoSeguro,
  validarConfirmacion,
  validarUrlHttps,
  datosEscalares,
  UrlsOfrecidas,
  crearEscritorio,
};
