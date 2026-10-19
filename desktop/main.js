'use strict';
/**
 * Amadeus Desktop — proceso principal de Electron (COMPANION.md §C2.7, §C1.1, §C1.2, §C1.11, §C1.12).
 *
 * - Overlay transparente siempre encima que cubre el workArea; dentro, el visor de VTuber AI Studio
 *   (`/?obs=1&desktop=1&camera=full&theme=amadeus`) mueve la caja del avatar con `transform`.
 * - Este proceso es el ÚNICO dueño de la visibilidad (lib/mapeo.js) y la AUTORIDAD del
 *   click-through (lib/hit.js): el renderer solo pide.
 * - Lee el cursor global (Windows/X11: `screen.getCursorScreenPoint` a 30 Hz; Hyprland: socket de
 *   peticiones a 15/4 Hz) y se lo pasa al visor para mirarlo y hacer el hit-test.
 * - Se conecta a R.A.M.S. por WebSocket `/escritorio` (WebSocket global de Node, sin Origin) con el
 *   token del archivo privado (lib/token.js). Nunca el renderer.
 * - Bandeja, atajos globales (Win/X11), CLI de 2.ª instancia (`--amadeus=…`), chat aparte.
 * - Respaldo Puter del Área de Broca (§C2.15, lib/puter.js): ventana OCULTA con Puter.js en la
 *   partición `persist:puter` y relevo WS `/escritorio` ↔ IPC en este proceso (`llm.pedir` →
 *   `puter:pedir`, `puter:respuesta` → `llm.respuesta`, `puter.estado`). El token de R.A.M.S. nunca
 *   llega a esas páginas. Inicio de sesión desde la bandeja con una ventanita y su botón.
 * - `--smoke`: comprobación oculta (sin bandeja, atajos, red ni ventanas visibles), ≤ 5 s.
 *
 * - Tarjetas aprobables (v0.2.2, docs/V022_CORREO_DRIVE_SCRIPTS.md §13.3): `chat:tarjeta` (botones)
 *   y `chat:copiar` (portapapeles) solo desde el chat; el portapapeles solo recibe el código de una
 *   tarjeta de script vigente que envió R.A.M.S. La decisión sale con su propio tope (≤ 60 KiB,
 *   MAX_WS_SALIENTE_TARJETA); todo lo demás sigue en 8 KiB.
 *
 * Seguridad: contextIsolation + sandbox + preload mínimo; IPC validado por origen/emisor; CSP por
 * cabecera; navegación y ventanas nuevas bloqueadas (salvo el inicio de sesión de puter.com);
 * permisos del navegador denegados.
 */

const {
  app, BrowserWindow, Tray, Menu, globalShortcut, screen, ipcMain, nativeImage, session, shell, Notification, clipboard,
} = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const net = require('node:net');
const { pathToFileURL } = require('node:url');

const { parsearArgv, datosSegundaInstancia, comandoSegundaInstancia } = require('./lib/cli');
const { AutoridadClicks, LimitadorCaja, intervaloCursorHyprMs } = require('./lib/hit');
const { crearEscritorio } = require('./lib/mapeo');
const { leerToken, urlConexion, urlSinSecreto } = require('./lib/token');
const { hashesImportmap, mismosHashes, construirCsp, conCsp, esDocumentoDelVisor } = require('./lib/csp');
const { origenVisor, resolverAtajos } = require('./lib/config');
const { crearRelevoPuter, puterHabilitado, modeloDefecto, esfuerzoRazonamiento } = require('./lib/puter');
const {
  URL_TRABAJO, URL_CONECTAR, cabecerasPuter, resolverRecurso, esPaginaPuter, esUrlPuterPermitida,
} = require('./lib/puter-sitio');

// ---------------------------------------------------------------------------------------------
// Configuración y plataforma
// ---------------------------------------------------------------------------------------------

const ARGS = parsearArgv(process.argv);
const SMOKE = ARGS.smoke;
const WIN = process.platform === 'win32';
const LINUX = process.platform === 'linux';
const X11_FORZADO = process.argv.includes('--ozone-platform=x11') ||
  app.commandLine.getSwitchValue('ozone-platform') === 'x11';
const WAYLAND = LINUX && !!process.env.WAYLAND_DISPLAY && !X11_FORZADO;
const HIS = process.env.HYPRLAND_INSTANCE_SIGNATURE || '';
const HYPR = LINUX && /^[A-Za-z0-9_.-]{1,128}$/.test(HIS) && path.isAbsolute(process.env.XDG_RUNTIME_DIR || '');

const T_OVERLAY = 'Amadeus Overlay'; // las reglas de Hyprland y ControlPC buscan por título
const T_CHAT = 'Amadeus Chat';
const PART_OVERLAY = SMOKE ? 'amadeus-smoke-overlay' : 'amadeus-overlay'; // sesiones en memoria
const PART_CHAT = SMOKE ? 'amadeus-smoke-chat' : 'amadeus-chat';
// Puter (§C2.15): la sesión de Puter se guarda entre arranques; el smoke usa una en memoria.
const T_PUTER = 'Amadeus Puter';      // ventana oculta: nunca se muestra
const T_CONECTAR = 'Conectar Puter';
const PART_PUTER = SMOKE ? 'amadeus-smoke-puter' : 'persist:puter';
const PUTER = puterHabilitado(process.env);
const CONFIG_PUTER = modeloDefecto(process.env);
const DIR_PUTER = path.join(__dirname, 'puter');

// Solo orígenes locales (§C2.12): el overlay carga este origen y le da window.amadeusDesktop.
const CONFIG_VISOR = origenVisor(process.env.VTUBER_AI_URL);
const VTUBER = CONFIG_VISOR.origen;
const TEMA = String(process.env.VTUBER_AI_TEMA || 'amadeus').trim().toLowerCase() === 'none' ? null : 'amadeus';
const URL_OVERLAY = `${VTUBER}/?obs=1&desktop=1&camera=full${TEMA ? '&theme=amadeus' : ''}`;
const EXCLUIR_CAPTURA = !['0', 'false', 'no', 'off']
  .includes(String(process.env.AMADEUS_EXCLUIR_CAPTURA ?? '1').trim().toLowerCase());

const RUTA_CHAT = path.join(__dirname, 'chat.html');
const URL_CHAT = pathToFileURL(RUTA_CHAT).href;
const RUTA_STUB = path.join(__dirname, 'test', 'stub-overlay.html');
const URL_STUB = pathToFileURL(RUTA_STUB).href;
const RUTA_ICONO = path.join(__dirname, 'assets', 'tray.png');
const RUTA_ICONO_CAMARA = path.join(__dirname, 'assets', 'tray-camara.png'); // con punto de cámara activa

const ESPERA_CARGA_MS = 5000;     // reintento del GET al visor
const CICLO_GEOMETRIA_MS = 10000; // Hyprland: refresco de posición/suelo mientras está visible
const TIMEOUT_HYPR_MS = 150;      // petición de cursor al socket de Hyprland
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;
const MAX_WS_ENTRANTE = 65536;            // entrantes de R.A.M.S. (caracteres)
const MAX_WS_PEDIDO_LLM = 1024 * 1024;    // solo `llm.pedir` (R.A.M.S. lo limita a 768 KiB de JSON)
const MAX_WS_SALIENTE = 8192;             // bytes UTF-8 (R.A.M.S. acepta ≤ 8 KiB; llm.respuesta ≤ 64 KiB)
// v0.2.2 (§13.3): tope de lo que el mapeo puede pedir con `maxBytes` (solo tarjeta_decision, ≤ 60 KiB).
const MAX_WS_SALIENTE_TARJETA = 61440;
const RECARGA_PUTER_MIN_MS = 2000;
const RECARGA_PUTER_MAX_MS = 60000;
/** La ventana oculta lleva este tiempo cargada sin caerse: la espera de recarga vuelve al mínimo. */
const RECARGA_PUTER_ESTABLE_MS = 60000;

const log = (...a) => console.log('[amadeus]', ...a);
const aviso = (...a) => console.warn('[amadeus]', ...a);
const vivo = (w) => !!w && !w.isDestroyed();
const mismoOrigen = (url, origen) => {
  try {
    return new URL(url).origin === origen;
  } catch {
    return false;
  }
};

// ---------------------------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------------------------

let overlay = null;
let chat = null;
let tray = null;
let overlayListo = false;
let chatListo = false;
const colaChat = [];
let saliendoApp = false;
let pantalla = null;                  // Display de Electron del overlay (Windows/X11)
let geomHypr = { x: 0, y: 0 };        // origen del overlay en coordenadas de layout (Hyprland)
let sueloEnviado = null;
let cspActual = construirCsp({ hashes: [] });
let hashesCarga = null;
let navegacionPorCsp = false;
let cargaTimer = null;
let avisadoVisor = false;
let cajaTimer = null;
let ultimaCajaRel = null;
let chatPosicionado = false;
const notificaciones = new Set();
let puterWin = null;                  // ventana oculta de Puter (trabajo.html)
let puterListo = false;               // trabajo.html cargado: ya se le pueden mandar pedidos
let conectar = null;                  // ventanita «Conectar Puter»
const recargaPuter = { timer: null, espera: RECARGA_PUTER_MIN_MS, estable: null };

const autoridad = new AutoridadClicks();
const limitadorCaja = new LimitadorCaja({ intervaloMs: 500 });

// ---------------------------------------------------------------------------------------------
// Puertos del controlador de escritorio (lib/mapeo.js)
// ---------------------------------------------------------------------------------------------

function aplicarCambio(c) {
  if (!c || !vivo(overlay)) return;
  if (c.interactivo) overlay.setIgnoreMouseEvents(false);
  else overlay.setIgnoreMouseEvents(true, { forward: true });
}

function forzarClickThrough(motivo) {
  aplicarCambio(autoridad.forzar(motivo));
}

function enviarOverlay(canal, datos) {
  if (vivo(overlay) && overlayListo) overlay.webContents.send(canal, datos);
}

function enviarChat(canal, datos) {
  if (!vivo(chat)) return;
  if (!chatListo) {
    colaChat.push([canal, datos]);
    if (colaChat.length > 200) colaChat.shift();
    return;
  }
  chat.webContents.send(canal, datos);
}

function mostrarOverlay() {
  if (!vivo(overlay)) return;
  if (!WAYLAND) moverOverlayAPantallaDelCursor();
  overlay.webContents.setBackgroundThrottling(false);
  if (WIN) overlay.setAlwaysOnTop(true, 'screen-saver');
  if (WAYLAND) overlay.show(); // showInactive no existe en Wayland: el foco lo evita la regla no_initial_focus
  else overlay.showInactive();
}

function ocultarOverlay() {
  if (!vivo(overlay)) return;
  overlay.hide();
  overlay.webContents.setBackgroundThrottling(true);
}

function mostrarChat(enfocar) {
  if (!vivo(chat)) return;
  posicionarChat();
  if (enfocar || WAYLAND) chat.show(); // Wayland: la regla no_initial_focus evita el foco
  else chat.showInactive();
}

function posicionarChat() {
  if (chatPosicionado || WAYLAND || !vivo(chat)) return;
  try {
    const d = pantalla || screen.getPrimaryDisplay();
    const wa = d.workArea;
    const [w, h] = chat.getSize();
    chat.setPosition(Math.round(wa.x + wa.width - w - 24), Math.round(wa.y + wa.height - h - 24));
    chatPosicionado = true;
  } catch {
    /* sin pantalla: que la coloque el sistema */
  }
}

function bordePreferido() {
  try {
    if (HYPR || WAYLAND) {
      const c = autoridad.cursor;
      const ancho = autoridad.area ? autoridad.area.width : 0;
      return c && ancho ? (c.x < ancho / 2 ? 'left' : 'right') : 'right';
    }
    const p = screen.getCursorScreenPoint();
    const wa = screen.getDisplayNearestPoint(p).workArea;
    return p.x < wa.x + wa.width / 2 ? 'left' : 'right';
  } catch {
    return 'right';
  }
}

function alCambiarVisibilidad(visible) {
  if (visible) {
    autoridad.habilitar();
    limitadorCaja.reiniciar();
    iniciarCursor();
    if (HYPR) iniciarGeometriaHypr();
  } else {
    aplicarCambio(autoridad.deshabilitar());
    detenerCursor();
    detenerGeometriaHypr();
  }
  reconstruirMenu();
}

function mostrarNotificacion({ titulo, texto, url }) {
  if (SMOKE) return;
  try {
    if (!Notification.isSupported()) return;
    const n = new Notification({ title: titulo, body: texto, icon: RUTA_ICONO });
    n.on('click', () => {
      if (url) escritorio.alAbrirEnlace(url); // clic del usuario en la notificación
      else escritorio.abrirChatUsuario();
    });
    n.on('close', () => notificaciones.delete(n));
    notificaciones.add(n);
    while (notificaciones.size > 10) notificaciones.delete(notificaciones.values().next().value);
    n.show();
  } catch (err) {
    aviso('no se pudo mostrar la notificación:', err && err.message);
  }
}

const escritorio = crearEscritorio({
  overlay: { showInactive: mostrarOverlay, hide: ocultarOverlay },
  chat: {
    showInactive: () => mostrarChat(false),
    show: () => mostrarChat(true),
    focus: () => { if (vivo(chat)) chat.focus(); },
    hide: () => { if (vivo(chat)) chat.hide(); },
    isVisible: () => vivo(chat) && chat.isVisible(),
    isFocused: () => vivo(chat) && chat.isFocused(),
  },
  overlayListo: () => overlayListo,
  enviarOverlay,
  enviarChat,
  // Sin `maxBytes` (todo salvo tarjeta_decision), 8 KiB como siempre; con él, nunca más de 60 KiB.
  enviarWs: (obj, maxBytes) => enlace.enviar(obj, Math.min(Number.isInteger(maxBytes) && maxBytes > 0 ? maxBytes : MAX_WS_SALIENTE, MAX_WS_SALIENTE_TARJETA)),
  forzarClickThrough,
  notificar: mostrarNotificacion,
  abrirExterno: (url) => {
    shell.openExternal(url).catch((err) => aviso('no se pudo abrir el enlace:', err && err.message));
  },
  temporizador: (fn, ms) => {
    const t = setTimeout(fn, ms);
    return () => clearTimeout(t);
  },
  ahora: Date.now,
  bordePreferido,
  alCambiarVisibilidad,
  alCambiarEstado: () => reconstruirMenu(),
}, { pid: process.pid, version: app.getVersion() });

/** Relevo WS ↔ ventana oculta de Puter (lib/puter.js). Nunca registra prompts ni respuestas. */
const relevo = crearRelevoPuter({
  enviarWs: (obj, maxBytes) => enlace.enviar(obj, maxBytes),
  enviarVentana: enviarPuter,
  temporizador: (fn, ms) => {
    const t = setTimeout(fn, ms);
    return () => clearTimeout(t);
  },
  ahora: Date.now,
  alCambiarEstado: () => reconstruirMenu(),
  registrar: (texto) => log(texto),
}, { habilitado: PUTER, modelo: CONFIG_PUTER.modelo, esfuerzo: esfuerzoRazonamiento(process.env) });

// ---------------------------------------------------------------------------------------------
// Cursor global y caja
// ---------------------------------------------------------------------------------------------

/** Petición al socket de Hyprland (`$XDG_RUNTIME_DIR/hypr/$HIS/.socket.sock`), con timeout. */
function pedirHypr(cmd, timeoutMs = TIMEOUT_HYPR_MS) {
  return new Promise((resolve, reject) => {
    const ruta = path.join(process.env.XDG_RUNTIME_DIR, 'hypr', HIS, '.socket.sock');
    let buf = '';
    let hecho = false;
    const c = net.createConnection(ruta);
    const fin = (err, val) => {
      if (hecho) return;
      hecho = true;
      clearTimeout(t);
      c.destroy();
      if (err) reject(err);
      else resolve(val);
    };
    const t = setTimeout(() => fin(new Error('timeout')), timeoutMs);
    c.setEncoding('utf8');
    c.on('connect', () => c.write(cmd));
    c.on('data', (d) => {
      buf += d;
      if (buf.length > 2_000_000) fin(new Error('respuesta demasiado grande'));
    });
    c.on('end', () => fin(null, buf));
    c.on('error', (e) => fin(e));
  });
}

function cursorRelativoWin() {
  if (!vivo(overlay)) return null;
  const p = screen.getCursorScreenPoint();
  const b = overlay.getContentBounds();
  return { x: p.x - b.x, y: p.y - b.y };
}

async function leerCursor() {
  if (HYPR) {
    const r = JSON.parse(await pedirHypr('j/cursorpos'));
    if (!r || !Number.isFinite(r.x) || !Number.isFinite(r.y)) return null;
    return { x: r.x - geomHypr.x, y: r.y - geomHypr.y }; // layout global → relativo al overlay
  }
  if (WAYLAND) return null; // otro compositor Wayland: sin cursor global (usa npm run start:x11)
  return cursorRelativoWin();
}

const cursorLoop = { activo: false, gen: 0, timer: null, ultimoEnviado: null };

function iniciarCursor() {
  if (cursorLoop.activo || (WAYLAND && !HYPR) || SMOKE) return;
  cursorLoop.activo = true;
  const gen = ++cursorLoop.gen;
  cursorLoop.timer = setTimeout(() => tickCursor(gen), 0);
}

function detenerCursor() {
  cursorLoop.activo = false;
  cursorLoop.gen += 1;
  clearTimeout(cursorLoop.timer);
  cursorLoop.timer = null;
  cursorLoop.ultimoEnviado = null;
}

async function tickCursor(gen) {
  let espera = HYPR ? intervaloCursorHyprMs(null, null) : 33; // 30 Hz en Windows/X11
  try {
    const p = await leerCursor(); // en Hyprland: una sola petición en curso
    if (gen !== cursorLoop.gen) return;
    const u = cursorLoop.ultimoEnviado;
    if (p && !(u && u.x === p.x && u.y === p.y)) {
      enviarOverlay('overlay:cursor', p);
      cursorLoop.ultimoEnviado = p;
    }
    aplicarCambio(autoridad.moverCursor(p, Date.now()));
    if (HYPR) espera = intervaloCursorHyprMs(p, autoridad.rect);
  } catch {
    if (gen !== cursorLoop.gen) return;
    aplicarCambio(autoridad.moverCursor(null, Date.now()));
  }
  if (gen !== cursorLoop.gen || !cursorLoop.activo) return;
  cursorLoop.timer = setTimeout(() => tickCursor(gen), espera);
}

/** Caja en píxeles FÍSICOS globales para R.A.M.S. (`null` si no se puede calcular). */
function cajaFisica(r) {
  if (!vivo(overlay) || !r) return null;
  if (HYPR) {
    return { x: Math.round(geomHypr.x + r.x), y: Math.round(geomHypr.y + r.y), w: Math.round(r.w), h: Math.round(r.h) };
  }
  if (WAYLAND) return null;
  const b = overlay.getContentBounds();
  const dip = {
    x: Math.round(b.x + r.x), y: Math.round(b.y + r.y),
    width: Math.max(1, Math.round(r.w)), height: Math.max(1, Math.round(r.h)),
  };
  if (WIN) {
    const f = screen.dipToScreenRect(overlay, dip);
    return { x: f.x, y: f.y, w: f.width, h: f.height };
  }
  const a = screen.dipToScreenPoint({ x: dip.x, y: dip.y });
  const z = screen.dipToScreenPoint({ x: dip.x + dip.width, y: dip.y + dip.height });
  return { x: a.x, y: a.y, w: z.x - a.x, h: z.y - a.y };
}

function enviarCaja(c) {
  enlace.enviar({ tipo: 'caja', x: c.x, y: c.y, w: c.w, h: c.h });
}

/** Caja → R.A.M.S. a ≤ 2 Hz y solo si cambia (con un envío final diferido). */
function ofrecerCaja(r) {
  if (!escritorio.estado().visible || !r) return;
  let fis;
  try {
    fis = cajaFisica(r);
  } catch {
    return;
  }
  if (!fis || fis.w <= 0 || fis.h <= 0 || fis.w > 2000 || fis.h > 2000) return;
  const res = limitadorCaja.ofrecer(fis, Date.now());
  if (res.enviar) enviarCaja(res.enviar);
  else if (res.esperaMs !== null && cajaTimer === null) {
    cajaTimer = setTimeout(() => {
      cajaTimer = null;
      const v = limitadorCaja.vencer(Date.now());
      if (v.enviar) enviarCaja(v.enviar);
    }, res.esperaMs);
  }
}

// ---------------------------------------------------------------------------------------------
// Geometría: monitor, suelo y relayout
// ---------------------------------------------------------------------------------------------

function elegirPantalla() {
  try {
    if (WAYLAND) return screen.getPrimaryDisplay(); // en Wayland decide el compositor (reglas)
    return screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  } catch {
    return null;
  }
}

function colocarOverlayEn(d) {
  if (!vivo(overlay) || !d || WAYLAND) return;
  pantalla = d;
  overlay.setBounds(d.workArea);
  const [w, h] = overlay.getContentSize();
  aplicarCambio(autoridad.fijarArea({ width: w, height: h }));
  enviarOverlay('overlay:order', { accion: 'relayout' });
  sueloEnviado = 0;
  enviarOverlay('overlay:order', { accion: 'suelo', inferior: 0 }); // el workArea ya excluye la barra de tareas
}

function moverOverlayAPantallaDelCursor() {
  let d;
  try {
    d = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  } catch {
    return;
  }
  const wa = d.workArea;
  const actual = pantalla && pantalla.workArea;
  if (pantalla && d.id === pantalla.id && actual.x === wa.x && actual.y === wa.y &&
      actual.width === wa.width && actual.height === wa.height) return;
  colocarOverlayEn(d);
}

function recolocarOverlay() {
  if (!vivo(overlay) || WAYLAND) return;
  try {
    colocarOverlayEn(screen.getDisplayMatching(overlay.getBounds()));
  } catch {
    /* pantalla en transición: se recoloca en el siguiente evento */
  }
}

const geometria = { timer: null, activa: false };

async function refrescarGeometriaHypr() {
  try {
    const clientes = JSON.parse(await pedirHypr('j/clients', 400));
    const monitores = JSON.parse(await pedirHypr('j/monitors', 400));
    const propio = Array.isArray(clientes)
      ? clientes.find((c) => c && c.pid === process.pid && (c.title === T_OVERLAY || c.initialTitle === T_OVERLAY))
      : null;
    let mon = null;
    if (Array.isArray(monitores)) {
      if (propio) mon = monitores.find((m) => m && m.id === propio.monitor) || null;
      if (!mon) mon = monitores.find((m) => m && m.focused) || monitores[0] || null;
    }
    const at = propio && Array.isArray(propio.at) ? propio.at : null;
    const size = propio && Array.isArray(propio.size) ? propio.size : null;
    if (at) geomHypr = { x: Number(at[0]) || 0, y: Number(at[1]) || 0 };
    else if (mon) geomHypr = { x: Number(mon.x) || 0, y: Number(mon.y) || 0 };
    if (size) aplicarCambio(autoridad.fijarArea({ width: Number(size[0]), height: Number(size[1]) }));
    if (mon) {
      // reserved = [izquierda, arriba, derecha, abajo] (barras como waybar); el suelo es lo que
      // queda del overlay por debajo del área útil del monitor.
      const escala = Number(mon.scale) > 0 ? Number(mon.scale) : 1;
      const girado = (Number(mon.transform) || 0) % 2 === 1;
      const altoLayout = (girado ? Number(mon.width) : Number(mon.height)) / escala;
      const abajo = Array.isArray(mon.reserved) ? Number(mon.reserved[3]) || 0 : 0;
      let inferior = abajo;
      if (at && size) {
        const bordeVentana = Number(at[1]) + Number(size[1]);
        const bordeUtil = Number(mon.y) + altoLayout - abajo;
        inferior = Math.max(0, Math.round(bordeVentana - bordeUtil));
      }
      if (Number.isFinite(inferior) && inferior !== sueloEnviado) {
        sueloEnviado = inferior;
        enviarOverlay('overlay:order', { accion: 'suelo', inferior });
      }
    }
  } catch {
    /* Hyprland no respondió: se reintenta en el próximo ciclo */
  }
}

function iniciarGeometriaHypr() {
  if (!HYPR || geometria.activa) return;
  geometria.activa = true;
  const ciclo = async () => {
    if (!geometria.activa) return;
    await refrescarGeometriaHypr();
    if (geometria.activa) geometria.timer = setTimeout(ciclo, CICLO_GEOMETRIA_MS);
  };
  geometria.timer = setTimeout(ciclo, 300); // deja que el compositor mapee la ventana
}

function detenerGeometriaHypr() {
  geometria.activa = false;
  clearTimeout(geometria.timer);
  geometria.timer = null;
}

// ---------------------------------------------------------------------------------------------
// Enlace con R.A.M.S. (WebSocket /escritorio)
// ---------------------------------------------------------------------------------------------

function procesoVivo(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return !!err && err.code === 'EPERM';
  }
}

const enlace = {
  ws: null,
  backoff: BACKOFF_MIN_MS,
  timer: null,
  detenido: SMOKE,
  ultimoMotivo: null,

  registrar(motivo) {
    if (motivo !== this.ultimoMotivo) {
      this.ultimoMotivo = motivo;
      log(`R.A.M.S. no disponible: ${motivo}`);
    }
  },

  async conectar() {
    this.timer = null;
    if (this.detenido || this.ws) return;
    // Se relee el archivo en CADA intento: R.A.M.S. genera un token nuevo en cada arranque.
    const t = await leerToken({
      env: process.env,
      plataforma: process.platform,
      getuid: typeof process.getuid === 'function' ? () => process.getuid() : undefined,
    });
    if (this.detenido || this.ws) return;
    if (!t.ok) {
      this.registrar(t.motivo);
      this.reintentar();
      return;
    }
    if (t.pid && !procesoVivo(t.pid)) {
      this.registrar('el archivo de token es de un R.A.M.S. que ya no está corriendo');
      this.reintentar();
      return;
    }
    let ws;
    try {
      ws = new WebSocket(urlConexion(t)); // Node no envía Origin: R.A.M.S. lo distingue del HUD
    } catch (err) {
      this.registrar(`no se pudo abrir ${urlSinSecreto(t.url)} (${err && err.message})`);
      this.reintentar();
      return;
    }
    this.ws = ws;
    const apertura = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) ws.close();
    }, 10000);
    ws.addEventListener('open', () => {
      clearTimeout(apertura);
      this.backoff = BACKOFF_MIN_MS;
      this.ultimoMotivo = null;
      log(`conectada a R.A.M.S. (${urlSinSecreto(t.url)})`);
      escritorio.alConectado();
      limitadorCaja.reiniciar();
      if (ultimaCajaRel) ofrecerCaja(ultimaCajaRel);
    });
    ws.addEventListener('message', (ev) => {
      if (this.ws !== ws || typeof ev.data !== 'string' || ev.data.length > MAX_WS_PEDIDO_LLM) return;
      let m;
      try {
        m = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (!m || typeof m !== 'object' || Array.isArray(m)) return;
      // `llm.pedir` (≤ 1 MiB) va al relevo de Puter y no pasa por el mapeo: no cierra la revisión
      // de confirmaciones ni toca el chat. Todo lo demás sigue limitado a 64 KiB.
      if (m.tipo === 'llm.pedir') {
        relevo.alPedido(m.datos);
        return;
      }
      if (ev.data.length > MAX_WS_ENTRANTE) return;
      escritorio.alMensajeWs(m);
      if (m.tipo === 'hola') relevo.alHola(); // puter.estado justo después del `hola`
    });
    ws.addEventListener('close', (ev) => {
      clearTimeout(apertura);
      if (this.ws !== ws) return;
      this.ws = null;
      relevo.alDesconectado();
      escritorio.alDesconectado();
      if (ev && ev.code === 4001) this.registrar('otra instancia de Amadeus Desktop ocupó el enlace');
      else this.registrar(`conexión cerrada (${ev ? ev.code : '?'})`);
      this.reintentar();
    });
    ws.addEventListener('error', () => { /* llega 'close' después */ });
  },

  reintentar() {
    if (this.detenido || this.timer) return;
    const espera = this.backoff;
    this.backoff = Math.min(this.backoff * 2, BACKOFF_MAX_MS);
    this.timer = setTimeout(() => this.conectar(), espera);
  },

  /** Envía `obj` como JSON si cabe en `maxBytes` (UTF-8). true si salió por el WebSocket. */
  enviar(obj, maxBytes = MAX_WS_SALIENTE) {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    let s;
    try {
      s = JSON.stringify(obj);
    } catch {
      return false;
    }
    if (typeof s !== 'string' || Buffer.byteLength(s, 'utf8') > maxBytes) return false;
    try {
      ws.send(s);
      return true;
    } catch {
      return false;
    }
  },

  detener() {
    this.detenido = true;
    clearTimeout(this.timer);
    this.timer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      try {
        ws.close(1000);
      } catch {
        /* nada */
      }
    }
  },
};

// ---------------------------------------------------------------------------------------------
// Sesiones, CSP y endurecimiento
// ---------------------------------------------------------------------------------------------

function endurecerSesion(ses) {
  ses.setPermissionRequestHandler((_wc, _permiso, cb) => cb(false)); // ni cámara, ni micro, ni nada
  ses.setPermissionCheckHandler(() => false);
  ses.setDevicePermissionHandler(() => false);
  ses.setDisplayMediaRequestHandler((_req, cb) => cb(null));
  ses.on('will-download', (e) => e.preventDefault());
}

/**
 * Páginas propias de Puter servidas en `http://amadeus-puter.localhost` SIN red ni puertos: el
 * main intercepta `http:` solo en la partición de Puter. Lo que no está en la lista → 404 (Puter.js
 * habla con puter.com por `https:`, que no se intercepta).
 */
async function servirPuter(req) {
  const metodo = req && typeof req.method === 'string' ? req.method : 'GET';
  const r = resolverRecurso(req && req.url, metodo);
  const vacio = (status) => new Response(null, {
    status,
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' },
  });
  if (!r) return vacio(404);
  try {
    const cuerpo = metodo === 'HEAD' ? null : await fs.promises.readFile(path.join(DIR_PUTER, r.archivo));
    return new Response(cuerpo, { status: 200, headers: cabecerasPuter(r.tipo) });
  } catch {
    return vacio(500);
  }
}

function prepararSesionPuter() {
  if (!PUTER) return;
  const ses = session.fromPartition(PART_PUTER);
  endurecerSesion(ses); // sin cámara, micro, descargas…: Puter.js solo necesita red y localStorage
  ses.protocol.handle('http', servirPuter);
}

function prepararSesiones() {
  const sesOverlay = session.fromPartition(PART_OVERLAY);
  for (const ses of [session.defaultSession, sesOverlay, session.fromPartition(PART_CHAT)]) endurecerSesion(ses);
  prepararSesionPuter();
  if (SMOKE) return; // el stub de smoke es file:// (sin cabeceras)
  sesOverlay.webRequest.onHeadersReceived((det, cb) => {
    if (esDocumentoDelVisor(det.url, VTUBER, det.resourceType)) {
      cb({ responseHeaders: conCsp(det.responseHeaders, cspActual) });
    } else {
      cb({});
    }
  });
}

function endurecerContenidos(wc, permitido) {
  wc.setWindowOpenHandler(() => ({ action: 'deny' })); // siempre: los enlaces van por abrirEnlace
  const bloquear = (e, urlVieja) => {
    const url = (e && e.url) || urlVieja;
    if (!permitido(url)) e.preventDefault();
  };
  wc.on('will-navigate', bloquear);
  wc.on('will-redirect', bloquear);
  wc.on('will-attach-webview', (e) => e.preventDefault());
}

async function obtenerIndex() {
  try {
    const r = await fetch(`${VTUBER}/`, {
      signal: AbortSignal.timeout(3000),
      redirect: 'error',
      headers: { accept: 'text/html' },
    });
    if (!r.ok) return null;
    const tipo = r.headers.get('content-type') || '';
    if (!tipo.includes('text/html')) return null;
    const texto = await r.text();
    return texto.length > 2_000_000 ? null : texto;
  } catch {
    return null;
  }
}

/** Recalcula el hash del importmap en cada navegación del frame principal; si cambió, recarga una vez. */
async function revisarCsp() {
  const recargaPermitida = !navegacionPorCsp;
  navegacionPorCsp = false;
  const html = await obtenerIndex();
  if (html === null) return;
  const h = hashesImportmap(html);
  if (hashesCarga !== null && mismosHashes(h, hashesCarga)) return;
  const cambio = hashesCarga !== null;
  hashesCarga = h;
  cspActual = construirCsp({ hashes: h });
  if (cambio && recargaPermitida && vivo(overlay)) {
    log('el importmap del visor cambió: recargo una vez con la CSP nueva');
    navegacionPorCsp = true;
    overlay.webContents.reloadIgnoringCache();
  }
}

function programarCargaOverlay(ms) {
  if (cargaTimer || saliendoApp || SMOKE) return;
  cargaTimer = setTimeout(cargarOverlay, ms);
}

/** Carga el visor SOLO tras un `GET /` correcto; si falla, reintenta cada 5 s. */
async function cargarOverlay() {
  cargaTimer = null;
  if (!vivo(overlay) || saliendoApp) return;
  const html = await obtenerIndex();
  if (html === null) {
    if (!avisadoVisor) {
      aviso(`VTuber AI Studio no responde en ${VTUBER}; reintento cada ${ESPERA_CARGA_MS / 1000} s`);
      avisadoVisor = true;
    }
    programarCargaOverlay(ESPERA_CARGA_MS);
    return;
  }
  avisadoVisor = false;
  hashesCarga = hashesImportmap(html);
  cspActual = construirCsp({ hashes: hashesCarga });
  overlay.loadURL(URL_OVERLAY).catch(() => { /* did-fail-load reprograma */ });
}

// ---------------------------------------------------------------------------------------------
// Ventanas
// ---------------------------------------------------------------------------------------------

function crearOverlay() {
  pantalla = elegirPantalla();
  const wa = pantalla ? pantalla.workArea : { x: 0, y: 0, width: 1280, height: 720 };
  overlay = new BrowserWindow({
    x: wa.x, y: wa.y, width: wa.width, height: wa.height, // en Wayland lo fijan las reglas
    title: T_OVERLAY,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    thickFrame: false,
    roundedCorners: false,
    focusable: WAYLAND, // Windows/X11: nunca roba el foco; Wayland: lo gobierna la regla
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload-overlay.js'),
      partition: PART_OVERLAY,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      nodeIntegrationInSubFrames: false,
      webviewTag: false,
      backgroundThrottling: false,
      spellcheck: false,
    },
  });
  overlay.setAlwaysOnTop(true, 'screen-saver');
  if (LINUX) overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlay.setIgnoreMouseEvents(true, { forward: true });
  if (EXCLUIR_CAPTURA && WIN) overlay.setContentProtection(true); // fuera de capturas, OBS y Discord
  overlay.webContents.setBackgroundThrottling(true); // oculta hasta que aparezca
  const [w, h] = overlay.getContentSize();
  aplicarCambio(autoridad.fijarArea({ width: w, height: h }));

  overlay.on('page-title-updated', (e) => e.preventDefault()); // conserva "Amadeus Overlay"
  overlay.on('close', (e) => {
    if (!saliendoApp) {
      e.preventDefault();
      escritorio.emergencia();
    }
  });
  overlay.on('unresponsive', () => forzarClickThrough('unresponsive'));

  const wc = overlay.webContents;
  endurecerContenidos(wc, (url) => (SMOKE ? url === URL_STUB : mismoOrigen(url, VTUBER)));
  wc.on('render-process-gone', (_e, det) => {
    aviso(`el visor del overlay se cayó (${det && det.reason}); lo recargo`);
    overlayListo = false;
    aplicarCambio(autoridad.reiniciar());
    forzarClickThrough('caida');
    escritorio.alOverlayCaido();
    programarCargaOverlay(2000);
  });
  wc.on('did-fail-load', (_e, codigo, descripcion, _url, principal) => {
    if (!principal || codigo === -3) return; // -3 = ERR_ABORTED (navegación sustituida)
    overlayListo = false;
    aviso(`no se pudo cargar el visor (${codigo} ${descripcion}); reintento en ${ESPERA_CARGA_MS / 1000} s`);
    programarCargaOverlay(ESPERA_CARGA_MS);
  });
  wc.on('did-start-navigation', (d) => {
    if (!d || !d.isMainFrame || d.isSameDocument) return;
    overlayListo = false;
    aplicarCambio(autoridad.reiniciar());
    if (!SMOKE && mismoOrigen(d.url, VTUBER)) revisarCsp();
  });
  wc.on('did-finish-load', () => {
    const url = wc.getURL();
    if (SMOKE ? url !== URL_STUB : !mismoOrigen(url, VTUBER)) return;
    overlayListo = true;
    const [ancho, alto] = overlay.getContentSize();
    aplicarCambio(autoridad.fijarArea({ width: ancho, height: alto }));
    enviarOverlay('overlay:order', { accion: 'relayout' });
    if (!HYPR) {
      sueloEnviado = 0;
      enviarOverlay('overlay:order', { accion: 'suelo', inferior: 0 });
    } else {
      sueloEnviado = null;
    }
    escritorio.alOverlayListo();
  });
}

function crearChat() {
  chat = new BrowserWindow({
    width: 380,
    height: 560,
    minWidth: 300,
    minHeight: 380,
    title: T_CHAT,
    show: false,
    frame: false,
    backgroundColor: '#0b0d12',
    alwaysOnTop: true,
    fullscreenable: false,
    maximizable: false,
    skipTaskbar: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload-chat.js'),
      partition: PART_CHAT,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webviewTag: false,
      spellcheck: false, // sin descargas de diccionarios
    },
  });
  chat.setAlwaysOnTop(true, 'floating');
  chat.on('page-title-updated', (e) => e.preventDefault());
  chat.on('close', (e) => {
    if (!saliendoApp) {
      e.preventDefault();
      chat.hide();
    }
  });
  const wc = chat.webContents;
  endurecerContenidos(wc, () => false); // página estática: nunca navega
  wc.on('did-start-navigation', (d) => {
    if (d && d.isMainFrame && !d.isSameDocument) chatListo = false;
  });
  wc.on('did-finish-load', () => {
    if (wc.getURL() !== URL_CHAT) return;
    chatListo = true;
    while (colaChat.length) {
      const [canal, datos] = colaChat.shift();
      wc.send(canal, datos);
    }
    escritorio.alChatCargado(); // el estado actual, después de lo encolado
  });
  wc.on('render-process-gone', () => {
    chatListo = false;
    if (!saliendoApp && !SMOKE) setTimeout(() => vivo(chat) && chat.loadFile(RUTA_CHAT), 1500);
  });
  chat.loadFile(RUTA_CHAT);
}

// ---------------------------------------------------------------------------------------------
// Puter: ventana oculta, ventanita de inicio de sesión y relevo (§C2.15)
// ---------------------------------------------------------------------------------------------

/** Preferencias de las páginas propias de Puter (ventana oculta y ventanita de conexión). */
function preferenciasPuter(extra = {}) {
  return {
    preload: path.join(__dirname, 'preload-puter.js'),
    partition: PART_PUTER,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    nodeIntegrationInSubFrames: false,
    webviewTag: false,
    spellcheck: false,
    ...extra,
  };
}

/** IPC a la ventana oculta; false si no está cargada (el relevo responde «otro» al instante). */
function enviarPuter(canal, datos) {
  if (!vivo(puterWin) || !puterListo) return false;
  try {
    puterWin.webContents.send(canal, datos);
    return true;
  } catch {
    return false;
  }
}

function cargarPuter() {
  if (!vivo(puterWin) || saliendoApp) return;
  puterListo = false;
  puterWin.loadURL(URL_TRABAJO).catch(() => { /* did-fail-load reprograma */ });
}

/**
 * Recarga tras un fallo, con espera creciente (2 s → 60 s) para no entrar en un bucle de caídas.
 * La espera solo vuelve a 2 s cuando la página lleva RECARGA_PUTER_ESTABLE_MS cargada sin caerse
 * (armarEstabilidadPuter): el primer `puter:estado` de la página NO cuenta, porque trabajo.js lo
 * manda al arrancar, antes incluso de cargar puter.js, y lo que suele tumbar la ventana viene después.
 */
function programarRecargaPuter() {
  cancelarEstabilidadPuter();
  if (recargaPuter.timer || saliendoApp || SMOKE || !vivo(puterWin)) return;
  const espera = recargaPuter.espera;
  recargaPuter.espera = Math.min(espera * 2, RECARGA_PUTER_MAX_MS);
  recargaPuter.timer = setTimeout(() => {
    recargaPuter.timer = null;
    cargarPuter();
  }, espera);
}

/** trabajo.html terminó de cargar: si aguanta RECARGA_PUTER_ESTABLE_MS, la espera vuelve al mínimo. */
function armarEstabilidadPuter() {
  cancelarEstabilidadPuter();
  if (SMOKE || saliendoApp) return;
  recargaPuter.estable = setTimeout(() => {
    recargaPuter.estable = null;
    recargaPuter.espera = RECARGA_PUTER_MIN_MS;
  }, RECARGA_PUTER_ESTABLE_MS);
}

function cancelarEstabilidadPuter() {
  clearTimeout(recargaPuter.estable);
  recargaPuter.estable = null;
}

/**
 * Recarga la ventana oculta (sesión nueva o cerrada): lo que estaba en curso falla con `error` y
 * el estado vuelve a «sin cargar» hasta que la página informe.
 */
function recargarPuter(error = 'otro') {
  relevo.alReinicioVentana(error);
  clearTimeout(recargaPuter.timer);
  recargaPuter.timer = null;
  cargarPuter();
}

function crearPuter() {
  if (!PUTER) return;
  puterWin = new BrowserWindow({
    width: 480,
    height: 360,
    title: T_PUTER,
    show: false,
    skipTaskbar: true,
    focusable: false,
    paintWhenInitiallyHidden: false,
    webPreferences: preferenciasPuter({ backgroundThrottling: false }), // oculta pero con plazos fiables
  });
  puterWin.on('page-title-updated', (e) => e.preventDefault());
  puterWin.on('close', (e) => {
    if (!saliendoApp) e.preventDefault(); // vive mientras viva la app
  });
  const wc = puterWin.webContents;
  endurecerContenidos(wc, (url) => esPaginaPuter(url, 'trabajo.html')); // nunca abre ventanas
  wc.on('did-start-navigation', (d) => {
    if (!d || !d.isMainFrame || d.isSameDocument) return;
    cancelarEstabilidadPuter(); // la estabilidad se mide por documento, desde su did-finish-load
    if (!puterListo) return;
    puterListo = false;
    relevo.alReinicioVentana('otro');
  });
  wc.on('did-finish-load', () => {
    if (wc.getURL() !== URL_TRABAJO) return;
    puterListo = true;
    armarEstabilidadPuter();
    wc.send('puter:info', { modelo: relevo.modelo });
  });
  wc.on('did-fail-load', (_e, codigo, descripcion, _url, principal) => {
    if (!principal || codigo === -3) return;
    puterListo = false;
    aviso(`no se pudo cargar la ventana de Puter (${codigo} ${descripcion}); reintento`);
    programarRecargaPuter();
  });
  wc.on('render-process-gone', (_e, det) => {
    puterListo = false;
    aviso(`la ventana de Puter se cayó (${det && det.reason}); la recargo`);
    relevo.alReinicioVentana('otro', 'ventana_caida');
    programarRecargaPuter();
  });
  cargarPuter();
}

/**
 * La ventana emergente de inicio de sesión (puter.com): sin más ventanas y sin salir de puter.com.
 * Hereda la partición de su opener (la sesión queda en `persist:puter`).
 */
function endurecerPopupPuter(win) {
  if (!win || typeof win.isDestroyed !== 'function' || win.isDestroyed()) return;
  const wc = win.webContents;
  wc.setWindowOpenHandler(() => ({ action: 'deny' }));
  const bloquear = (e, urlVieja) => {
    const url = (e && e.url) || urlVieja;
    if (!esUrlPuterPermitida(url)) e.preventDefault();
  };
  wc.on('will-navigate', bloquear);
  wc.on('will-redirect', bloquear);
  wc.on('will-attach-webview', (e) => e.preventDefault());
}

/** window.open desde la ventanita: solo `https://puter.com` y subdominios, en una ventana sandbox. */
function abrirVentanaPuter({ url } = {}) {
  if (!esUrlPuterPermitida(url)) {
    aviso('Puter: bloqueada una ventana fuera de puter.com');
    return { action: 'deny' };
  }
  return {
    action: 'allow',
    overrideBrowserWindowOptions: {
      width: 520,
      height: 720,
      title: 'Puter',
      autoHideMenuBar: true,
      backgroundColor: '#ffffff',
      webPreferences: {
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        nodeIntegrationInSubFrames: false,
        webviewTag: false,
        spellcheck: false,
      },
    },
  };
}

/**
 * Ventanita «Conectar Puter» (bandeja o `--amadeus=puter`, siempre por acción del usuario): el
 * login de Puter abre una ventana emergente que necesita el gesto de su botón.
 */
function abrirConectarPuter() {
  if (!PUTER || SMOKE) return false;
  if (vivo(conectar)) {
    conectar.show();
    conectar.focus();
    return true;
  }
  conectar = new BrowserWindow({
    width: 440,
    height: 400,
    minWidth: 360,
    minHeight: 320,
    title: T_CONECTAR,
    show: false,
    resizable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    backgroundColor: '#0b0d12',
    autoHideMenuBar: true,
    webPreferences: preferenciasPuter(),
  });
  const ventana = conectar;
  ventana.removeMenu();
  ventana.on('page-title-updated', (e) => e.preventDefault());
  ventana.once('ready-to-show', () => {
    if (vivo(ventana)) {
      ventana.show();
      ventana.focus();
    }
  });
  ventana.on('closed', () => {
    if (conectar === ventana) conectar = null;
  });
  const wc = ventana.webContents;
  endurecerContenidos(wc, (url) => esPaginaPuter(url, 'conectar.html'));
  wc.setWindowOpenHandler(abrirVentanaPuter); // sustituye al «deny» de endurecerContenidos
  wc.on('did-create-window', (win) => endurecerPopupPuter(win));
  wc.on('did-finish-load', () => {
    if (wc.getURL() === URL_CONECTAR) wc.send('puter:info', { modelo: relevo.modelo });
  });
  ventana.loadURL(URL_CONECTAR).catch(() => { /* la ventanita muestra su propio error */ });
  return true;
}

/** `puter:conexion` de la ventanita: `{ok, nueva, error}`. */
function alConexionPuter(d) {
  if (!d || typeof d !== 'object') return;
  if (d.ok !== true) {
    const codigo = typeof d.error === 'string' && /^[a-z_]{1,24}$/.test(d.error) ? d.error : 'otro';
    log(`Puter: no se pudo iniciar sesión (${codigo})`);
    return;
  }
  // Sesión nueva o cambio de cuenta: la ventana oculta tiene que leer el token nuevo. Si ya estaba
  // iniciada pero la ventana oculta no la ve (p. ej. no tenía la marca), también se recarga.
  if (d.nueva === true || !relevo.estado().listo) {
    log(d.nueva === true ? 'Puter: sesión iniciada' : 'Puter: sesión ya iniciada; recargo la ventana oculta');
    recargarPuter('otro');
  }
}

/** «Desconectar Puter»: borra la sesión de Puter (y la marca) de la partición y recarga. */
async function desconectarPuter() {
  if (!PUTER) return false;
  if (vivo(conectar)) conectar.close();
  relevo.alReinicioVentana('sin_sesion');
  puterListo = false;
  try {
    await session.fromPartition(PART_PUTER).clearStorageData();
  } catch (err) {
    aviso('Puter: no se pudo borrar la sesión:', err && err.message);
  }
  log('Puter: sesión cerrada');
  cargarPuter(); // sin la marca no vuelve a cargar puter.js
  return true;
}

/** Entradas de Puter en la bandeja. */
function menuPuter() {
  if (!PUTER) return [];
  const p = relevo.estado();
  if (p.sesion) {
    return [
      {
        label: p.listo
          ? `Puter: conectado (${p.modelo}${p.error === 'modelo_desconocido' ? ', no aparece en Puter' : ''})`
          : 'Puter: conectado (cargando…)',
        enabled: false,
      },
      { label: 'Desconectar Puter', click: () => { desconectarPuter(); } },
    ];
  }
  const items = [{ label: 'Conectar Puter (IA de respaldo)…', click: () => abrirConectarPuter() }];
  if (p.error === 'carga_fallida') items.unshift({ label: 'Puter: sin conexión con puter.com', enabled: false });
  return items;
}

// ---------------------------------------------------------------------------------------------
// IPC (validado por emisor)
// ---------------------------------------------------------------------------------------------

/** IPC del overlay: solo del frame principal del visor (origen de VTUBER_AI_URL). */
function desdeOverlay(e) {
  if (!vivo(overlay) || !e || e.sender !== overlay.webContents) return false;
  const f = e.senderFrame;
  if (!f || f.parent) return false;
  return SMOKE ? f.url === URL_STUB : f.origin === VTUBER;
}

/** IPC del chat: solo de `chat.webContents` con chat.html cargado. */
function desdeChat(e) {
  if (!vivo(chat) || !e || e.sender !== chat.webContents) return false;
  const f = e.senderFrame;
  return !!f && !f.parent && f.url === URL_CHAT;
}

/** IPC de la ventana oculta de Puter: solo de su webContents con trabajo.html (origen propio). */
function desdePuter(e) {
  if (!vivo(puterWin) || !e || e.sender !== puterWin.webContents) return false;
  const f = e.senderFrame;
  return !!f && !f.parent && f.url === URL_TRABAJO;
}

/** IPC de la ventanita «Conectar Puter»: solo de su webContents con conectar.html. */
function desdeConectar(e) {
  if (!vivo(conectar) || !e || e.sender !== conectar.webContents) return false;
  const f = e.senderFrame;
  return !!f && !f.parent && f.url === URL_CONECTAR;
}

const smoke = { marcas: new Set(), esperas: new Map() };

function smokeMarcar(nombre) {
  smoke.marcas.add(nombre);
  const r = smoke.esperas.get(nombre);
  if (r) r();
}

function smokeEsperar(nombre) {
  if (smoke.marcas.has(nombre)) return Promise.resolve();
  return new Promise((resolve) => smoke.esperas.set(nombre, resolve));
}

function registrarIpc() {
  ipcMain.on('overlay:interactive', (e, v) => {
    if (!desdeOverlay(e)) return;
    let cursor;
    if (!HYPR && !WAYLAND) {
      try {
        cursor = cursorRelativoWin(); // lectura fresca: evita el retraso del bucle de 33 ms
      } catch {
        cursor = undefined;
      }
    }
    aplicarCambio(autoridad.solicitar(v === true, Date.now(), cursor));
  });
  ipcMain.on('overlay:rect', (e, r) => {
    if (!desdeOverlay(e)) return;
    const { aceptado, cambio } = autoridad.fijarRect(r, Date.now());
    aplicarCambio(cambio);
    if (!aceptado) return;
    ultimaCajaRel = autoridad.rect;
    escritorio.alCajaRecibida();
    if (SMOKE) smokeMarcar('rect');
    else ofrecerCaja(ultimaCajaRel);
  });
  ipcMain.on('overlay:hidden', (e) => {
    if (desdeOverlay(e)) escritorio.alHidden();
  });
  ipcMain.on('overlay:chat', (e) => {
    if (desdeOverlay(e) && !SMOKE) escritorio.abrirChatUsuario(); // clic derecho: acción del usuario
  });
  ipcMain.on('overlay:event', (e, nombre, datos) => {
    if (desdeOverlay(e)) escritorio.alEventoOverlay(nombre, datos);
  });
  ipcMain.handle('chat:send', (e, texto) => {
    if (!desdeChat(e)) return false;
    const ok = escritorio.alChatSend(texto);
    if (SMOKE) smokeMarcar('chat');
    return ok;
  });
  ipcMain.handle('chat:confirm', (e, id, aprobado) => {
    if (!desdeChat(e)) return false;
    return escritorio.alConfirmar(id, aprobado); // false = no salió: el chat reactiva los botones
  });
  ipcMain.on('chat:control', (e, accion) => {
    if (desdeChat(e)) escritorio.control(accion, 'chat');
  });
  ipcMain.on('chat:open-url', (e, url) => {
    if (desdeChat(e)) escritorio.alAbrirEnlace(url);
  });
  ipcMain.on('chat:hide', (e) => {
    if (desdeChat(e) && vivo(chat)) chat.hide();
  });
  // v0.2.2 (§13.3): botones de las tarjetas aprobables. Solo desde el chat.
  ipcMain.handle('chat:tarjeta', (e, id, sha, decision, extra) => {
    if (!desdeChat(e)) return false;
    return escritorio.alDecidirTarjeta(id, sha, decision, extra); // false = no salió: el chat reactiva los botones
  });
  ipcMain.on('chat:copiar', (e, id) => {
    if (!desdeChat(e)) return;
    // Solo el código de una tarjeta de SCRIPT vigente que envió R.A.M.S.; nunca texto del renderer.
    const c = escritorio.codigoDeTarjeta(id);
    if (c) clipboard.writeText(c);
  });
  // Puter (§C2.15): cada canal solo desde la ventana que le corresponde.
  ipcMain.on('puter:estado', (e, d) => {
    // No reinicia la espera de recarga: eso solo lo hace la estabilidad (armarEstabilidadPuter).
    if (!desdePuter(e) || !relevo.alEstadoVentana(d)) return;
    if (SMOKE) smokeMarcar('puter');
  });
  ipcMain.on('puter:respuesta', (e, d) => {
    if (desdePuter(e)) relevo.alRespuestaVentana(d);
  });
  ipcMain.on('puter:conexion', (e, d) => {
    if (desdeConectar(e)) alConexionPuter(d);
  });
  ipcMain.on('puter:cerrar', (e) => {
    if (desdeConectar(e) && vivo(conectar)) conectar.close();
  });
}

// ---------------------------------------------------------------------------------------------
// Bandeja, atajos, CLI
// ---------------------------------------------------------------------------------------------

const iconos = { normal: null, camara: null, actual: null };

/** Icono de la bandeja: con la cámara de R.A.M.S. abierta lleva un punto rojo (§C2.13). */
function fijarIconoBandeja(camara) {
  const img = camara && iconos.camara ? iconos.camara : iconos.normal;
  if (!img || img === iconos.actual) return;
  try {
    tray.setImage(img);
    iconos.actual = img;
  } catch {
    /* sin bandeja utilizable: queda el texto del menú */
  }
}

function reconstruirMenu() {
  if (!tray || tray.isDestroyed()) return;
  const e = escritorio.estado();
  const fuera = e.visible && !e.saliendo;
  const camara = e.camara === true;
  const menu = Menu.buildFromTemplate([
    ...(camara ? [{ label: '● Cámara activa', enabled: false }, { type: 'separator' }] : []),
    { label: fuera ? 'Esconder a Amadeus' : 'Llamar a Amadeus', click: () => escritorio.alternar() },
    { label: 'Abrir chat', click: () => escritorio.abrirChatUsuario() },
    { type: 'separator' },
    {
      label: 'No molestar',
      type: 'checkbox',
      checked: e.noMolestar,
      click: (item) => escritorio.control(item.checked ? 'no_molestar_on' : 'no_molestar_off', 'local'),
    },
    {
      label: 'Quieta',
      type: 'checkbox',
      checked: e.quieta === true,
      click: (item) => {
        if (item.checked) {
          escritorio.control('basta_travesuras', 'local');
        } else {
          enviarChat('chat:msg', {
            de: 'sistema',
            texto: 'Las travesuras se reactivan desde R.A.M.S.: escribe /travesuras on.',
          });
          reconstruirMenu();
        }
      },
    },
    { label: 'Pausar visión', click: () => escritorio.control('pausar_vision', 'local') },
    { label: 'Dejar pasar clics', click: () => escritorio.emergencia() },
    { type: 'separator' },
    ...(PUTER ? [...menuPuter(), { type: 'separator' }] : []),
    { label: e.enLinea ? 'R.A.M.S.: en línea' : 'R.A.M.S.: desconectada', enabled: false },
    { label: 'Salir', click: () => salir() },
  ]);
  tray.setContextMenu(menu); // en Linux hay que volver a fijarlo tras cada cambio
  const base = e.enLinea ? 'Amadeus (en línea)' : 'Amadeus (R.A.M.S. desconectada)';
  tray.setToolTip(camara ? `● Cámara activa · ${base}` : base);
  fijarIconoBandeja(camara);
}

function crearBandeja() {
  try {
    const img = nativeImage.createFromPath(RUTA_ICONO);
    iconos.normal = img;
    iconos.actual = img;
    try {
      const conCamara = nativeImage.createFromPath(RUTA_ICONO_CAMARA);
      iconos.camara = conCamara.isEmpty() ? null : conCamara;
    } catch {
      iconos.camara = null;
    }
    tray = new Tray(img);
    tray.on('click', () => escritorio.alternar()); // en Linux, 'click' = activación SNI
    reconstruirMenu();
  } catch (err) {
    tray = null;
    aviso('no hay bandeja del sistema (¿barra sin SNI?); usa la CLI --amadeus=… o los atajos:', err && err.message);
  }
}

/**
 * Acción de cada atajo global. Las combinaciones salen de lib/config.js (defecto + AMADEUS_ATAJO_*):
 * «Quieta» es Ctrl+Alt+Shift+Q porque en Windows Ctrl+Alt+Q es AltGr+Q = «@» en el teclado
 * latinoamericano, y el atajo se comería cada arroba en todas las apps.
 */
const ACCIONES_ATAJO = {
  llamar: () => escritorio.alternar(),
  chat: () => escritorio.abrirChatUsuario({ alternar: true }),
  basta: () => escritorio.control('basta_travesuras', 'local'),
  vision: () => escritorio.control('pausar_vision', 'local'),
  emergencia: () => escritorio.emergencia(),
};

function registrarAtajos() {
  if (WAYLAND) {
    log('Wayland: atajos globales desactivados; usa hl.bind con amadeus-desktop --amadeus=… (README)');
    return;
  }
  const { atajos, avisos } = resolverAtajos(process.env, process.platform);
  for (const a of avisos) aviso(a);
  for (const { accion, acelerador } of atajos) {
    let ok = false;
    try {
      ok = globalShortcut.register(acelerador, ACCIONES_ATAJO[accion]);
    } catch {
      ok = false;
    }
    if (!ok) aviso(`no pude registrar el atajo ${acelerador} (¿lo usa otra app?)`);
  }
}

function ejecutarComando(cmd) {
  switch (cmd) {
    case 'toggle': return escritorio.alternar();
    case 'show': return escritorio.aparecer({ modo: 'entrar' });
    case 'hide': return escritorio.esconderse();
    case 'chat': return escritorio.abrirChatUsuario({ alternar: true });
    case 'quit': return salir();
    case 'basta': return escritorio.control('basta_travesuras', 'local');
    case 'vision_pausa': return escritorio.control('pausar_vision', 'local');
    case 'clics': return escritorio.emergencia();
    case 'puter': return abrirConectarPuter();
    default: return false;
  }
}

function aplicarAutoarranque() {
  const v = String(process.env.AMADEUS_AUTOARRANQUE || '').trim();
  if (v !== '1' && v !== '0') return; // sin la variable no se toca la configuración del sistema
  if (!WIN && process.platform !== 'darwin') {
    log('AMADEUS_AUTOARRANQUE solo aplica en Windows/macOS; en Hyprland usa hl.on("hyprland.start", …)');
    return;
  }
  const args = app.isPackaged ? ['--hidden'] : [app.getAppPath(), '--hidden'];
  app.setLoginItemSettings({ openAtLogin: v === '1', path: process.execPath, args });
}

function salir() {
  saliendoApp = true;
  clearTimeout(recargaPuter.timer);
  recargaPuter.timer = null;
  cancelarEstabilidadPuter();
  enlace.detener();
  detenerCursor();
  detenerGeometriaHypr();
  app.quit();
}

// ---------------------------------------------------------------------------------------------
// Arranque normal
// ---------------------------------------------------------------------------------------------

function arrancar() {
  if (CONFIG_VISOR.aviso) aviso(CONFIG_VISOR.aviso);
  if (CONFIG_PUTER.aviso) aviso(CONFIG_PUTER.aviso);
  if (WIN) app.setAppUserModelId('co.rams.amadeus-desktop');
  prepararSesiones();
  registrarIpc();
  crearOverlay();
  crearChat();
  crearPuter();
  crearBandeja();
  registrarAtajos();
  if (!WAYLAND) {
    for (const ev of ['display-added', 'display-removed', 'display-metrics-changed']) screen.on(ev, recolocarOverlay);
  }
  aplicarAutoarranque();
  if (WAYLAND && !HYPR) {
    aviso('Wayland sin Hyprland: no hay cursor global, así que el avatar no será clicable. Usa: npm run start:x11');
  }
  cargarOverlay();
  enlace.conectar();
  if (ARGS.invalido) aviso(`orden desconocida --amadeus=${ARGS.invalido}`);
  if (ARGS.comando) ejecutarComando(ARGS.comando);
  else if (!ARGS.oculto) escritorio.aparecer({ modo: 'entrar' });
}

function principal() {
  app.on('web-contents-created', (_e, wc) => {
    wc.setWindowOpenHandler(() => ({ action: 'deny' }));
    wc.on('will-attach-webview', (ev) => ev.preventDefault());
  });
  if (SMOKE) {
    iniciarSmoke();
    return;
  }
  if (!app.requestSingleInstanceLock(datosSegundaInstancia(process.argv))) {
    app.quit(); // la 2.ª instancia ya entregó su orden a la 1.ª
    return;
  }
  app.on('second-instance', (_e, argv, _cwd, datos) => {
    const cmd = comandoSegundaInstancia(argv, datos);
    if (cmd) ejecutarComando(cmd);
  });
  app.on('window-all-closed', () => { /* se queda en la bandeja */ });
  app.on('before-quit', () => { saliendoApp = true; });
  app.on('will-quit', () => {
    globalShortcut.unregisterAll();
    enlace.detener();
  });
  app.whenReady().then(arrancar).catch((err) => {
    console.error('[amadeus] fallo al arrancar:', err);
    app.exit(1);
  });
}

// ---------------------------------------------------------------------------------------------
// Smoke (§C2.7.8): oculto, sin bandeja, atajos, autoarranque, R.A.M.S. ni VTuber AI Studio
// ---------------------------------------------------------------------------------------------

function escribir(fd, texto) {
  try {
    fs.writeSync(fd, `${texto}\n`);
  } catch {
    /* sin consola */
  }
}

const PREFIJO_PERFIL_SMOKE = 'amadeus-desktop-smoke';
const EDAD_PERFIL_SMOKE_MS = 2 * 60 * 1000;

/**
 * Borra los perfiles de smokes anteriores (`%TEMP%/amadeus-desktop-smoke*` de más de 2 min, para
 * no tocar el de un smoke que corre en paralelo). Lo que esté en uso o falle se deja para la próxima.
 */
function limpiarPerfilesSmoke() {
  const tmp = os.tmpdir();
  let nombres;
  try {
    nombres = fs.readdirSync(tmp);
  } catch {
    return;
  }
  const limite = Date.now() - EDAD_PERFIL_SMOKE_MS;
  let borrados = 0;
  for (const n of nombres) {
    if (borrados >= 20) break; // acotado: el smoke tiene que terminar en ≤ 5 s
    if (n !== PREFIJO_PERFIL_SMOKE && !n.startsWith(`${PREFIJO_PERFIL_SMOKE}-`)) continue;
    const ruta = path.join(tmp, n);
    try {
      const st = fs.lstatSync(ruta);
      if (!st.isDirectory() || st.isSymbolicLink() || st.mtimeMs > limite) continue;
      fs.rmSync(ruta, { recursive: true, force: true, maxRetries: 0 });
      borrados += 1;
    } catch {
      /* en uso o sin permiso: la próxima vez */
    }
  }
}

/**
 * Smoke de Puter (§C2.15): la ventana oculta carga trabajo.html por el protocolo propio (con CSP
 * y 404 para lo demás), expone solo `amadeusPuter` aislado, informa su estado y, sin sesión, NO
 * carga puter.js; un `llm.pedir` falla al instante con «sin_sesion» sin abrir nada.
 */
async function smokePuter(cargado) {
  await cargado;
  await smokeEsperar('puter'); // primer puter:estado de la página, validado por emisor
  const r = await puterWin.webContents.executeJavaScript(`(async () => {
    const api = window.amadeusPuter;
    if (!api || api.version !== 1 || typeof window.require !== 'undefined' ||
        typeof window.ipcRenderer !== 'undefined' || typeof window.process !== 'undefined') return 'sin puente aislado';
    if (typeof window.puter !== 'undefined' || document.querySelector('script[src*="puter.com"]')) {
      return 'cargó puter.js sin que el usuario conectara Puter';
    }
    const pagina = await fetch('/trabajo.html', { cache: 'no-store' });
    const csp = pagina.headers.get('content-security-policy') || '';
    if (!csp.includes("script-src 'self' https://js.puter.com;") || !csp.includes("default-src 'none'")) return 'sin CSP';
    const otra = await fetch('/main.js', { cache: 'no-store' });
    return otra.status === 404 ? 'ok' : 'sirve archivos fuera de la lista';
  })()`);
  if (r !== 'ok') throw new Error(`ventana de Puter: ${r}`);
  const e = relevo.estado();
  if (e.listo || e.sesion || e.modelo !== relevo.modelo) throw new Error('estado de Puter incorrecto sin sesión');
  const ventanas = BrowserWindow.getAllWindows().length;
  relevo.alPedido({ id: '0123456789abcdef', modelo: '', system: '', mensajes: [{ rol: 'usuario', texto: 'smoke' }] });
  const est = relevo.estadisticas();
  if (est.fallos !== 1 || relevo.enCurso() !== 0) throw new Error('llm.pedir sin sesión debería fallar al instante');
  if (BrowserWindow.getAllWindows().length !== ventanas) throw new Error('llm.pedir sin sesión abrió una ventana');
}

function iniciarSmoke() {
  const t0 = Date.now();
  const terminar = (codigo, texto) => {
    clearTimeout(guardia);
    escribir(codigo === 0 ? 1 : 2, texto);
    saliendoApp = true;
    for (const w of [overlay, chat, puterWin]) {
      if (vivo(w)) w.destroy(); // termina los renderers antes de salir: cierre más rápido
    }
    app.exit(codigo);
  };
  const guardia = setTimeout(() => terminar(1, 'smoke FALLÓ: tiempo agotado'), 4500);

  // Perfil propio del smoke (nunca el del usuario) y NUEVO en cada ejecución: con uno compartido,
  // dos smokes seguidos o en paralelo se pisan el perfil mientras los hijos de Chromium del anterior
  // aún lo cierran. No se borra al salir (los hijos aún lo usan y borrarlo bajo sus pies puede
  // colgar el cierre): cada smoke borra los de ejecuciones anteriores (> 2 min).
  limpiarPerfilesSmoke();
  let perfil;
  try {
    perfil = fs.mkdtempSync(path.join(os.tmpdir(), `${PREFIJO_PERFIL_SMOKE}-`));
  } catch {
    perfil = path.join(os.tmpdir(), `${PREFIJO_PERFIL_SMOKE}-${process.pid}`);
  }
  app.setPath('userData', perfil);
  app.setPath('sessionData', perfil);
  // Sin GPU: el smoke no pinta nada visible. Medido en Windows con 4 smokes en paralelo: sin el
  // proceso de GPU ni su caché de shaders, p50 de 2,4 s → 1,6 s hasta el fin del proceso.
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-gpu');
  app.commandLine.appendSwitch('disable-gpu-shader-disk-cache');
  app.commandLine.appendSwitch('disable-features', 'SpareRendererForSitePerProcess');
  app.on('window-all-closed', () => { /* nada */ });

  const requeridos = ['main.js', 'preload-overlay.js', 'preload-chat.js', 'preload-puter.js', 'chat.html', 'chat.js',
    'chat.css', path.join('test', 'stub-overlay.html'),
    ...['trabajo.html', 'trabajo.js', 'conectar.html', 'conectar.js', 'puter.css'].map((f) => path.join('puter', f))];
  for (const f of requeridos) {
    if (!fs.existsSync(path.join(__dirname, f))) {
      terminar(1, `smoke FALLÓ: falta ${f}`);
      return;
    }
  }
  if (typeof WebSocket !== 'function') {
    terminar(1, 'smoke FALLÓ: no hay WebSocket global en el proceso principal');
    return;
  }

  app.whenReady().then(async () => {
    prepararSesiones();
    registrarIpc();
    crearOverlay();
    crearChat();
    crearPuter(); // perfil nuevo y en memoria: sin la marca de «conectado», no carga puter.js (sin red)
    const puterCargado = !PUTER ? Promise.resolve() : new Promise((resolve, reject) => {
      puterWin.webContents.once('did-finish-load', resolve);
      puterWin.webContents.once('did-fail-load', (_e, c, d) => reject(new Error(`trabajo.html: ${c} ${d}`)));
    });
    const rect = smokeEsperar('rect');
    const enviado = smokeEsperar('chat');
    const overlayCargado = new Promise((resolve, reject) => {
      overlay.webContents.once('did-finish-load', resolve);
      overlay.webContents.once('did-fail-load', (_e, c, d) => reject(new Error(`stub del overlay: ${c} ${d}`)));
    });
    const chatCargado = new Promise((resolve, reject) => {
      chat.webContents.once('did-finish-load', resolve);
      chat.webContents.once('did-fail-load', (_e, c, d) => reject(new Error(`chat.html: ${c} ${d}`)));
    });
    overlay.loadURL(URL_STUB).catch(() => {});

    await overlayCargado; // did-finish-load (main.js) ya mandó overlay:order{relayout}
    const aislado = await overlay.webContents.executeJavaScript(
      "typeof window.amadeusDesktop === 'object' && window.amadeusDesktop.version === 3 && " +
      "typeof window.require === 'undefined' && typeof window.process === 'undefined' && " +
      "typeof window.ipcRenderer === 'undefined'");
    if (aislado !== true) throw new Error('amadeusDesktop no está expuesto o el renderer no está aislado');
    await rect; // el stub respondió con reportAvatar → IPC validado y caja aceptada

    await chatCargado;
    const chatOk = await chat.webContents.executeJavaScript(
      "typeof window.amadeusChat === 'object' && window.amadeusChat.version === 2 && " +
      "typeof window.amadeusChat.decidirTarjeta === 'function' && " +
      "typeof window.require === 'undefined' && typeof window.ipcRenderer === 'undefined' && " +
      "!!document.getElementById('lista') && !!document.getElementById('texto')");
    if (chatOk !== true) throw new Error('amadeusChat no está expuesto o chat.html no cargó bien');
    const resultado = await chat.webContents.executeJavaScript("window.amadeusChat.send('smoke')");
    await enviado;
    if (resultado !== false) throw new Error('chat:send no debería salir sin R.A.M.S.');
    // Confirmación sin R.A.M.S.: `chat:confirm` (invoke) responde false, y la tarjeta vuelve a poder
    // pulsarse (no se queda en «Aprobando…»), como en el chat real.
    const confirmado = await chat.webContents.executeJavaScript("window.amadeusChat.confirmar('deadbeef', true)");
    if (confirmado !== false) throw new Error('chat:confirm debería responder false sin R.A.M.S.');
    chat.webContents.send('chat:status', 'en_linea');
    chat.webContents.send('chat:permiso', { id: 'deadbeef', descripcion: 'smoke', pendiente: true });
    const tarjeta = await chat.webContents.executeJavaScript(`new Promise((fin) => {
      const t0 = performance.now();
      let pulsado = false;
      const mirar = () => {
        const s = document.querySelector('section.permiso');
        const b = s && [...s.querySelectorAll('button')];
        if (b && b.length === 2 && !pulsado) {
          pulsado = true;
          b[0].dispatchEvent(new MouseEvent('click', { detail: 1, bubbles: true }));
        }
        const aviso = [...document.querySelectorAll('.msg.sistema')].some((m) => m.textContent.includes('No se envió'));
        if (pulsado && aviso && b.every((x) => !x.disabled)) return fin('ok');
        if (performance.now() - t0 > 1500) return fin(pulsado ? 'botones bloqueados' : 'sin tarjeta');
        setTimeout(mirar, 10);
      };
      mirar();
    })`);
    if (tarjeta !== 'ok') throw new Error(`confirmación sin R.A.M.S.: ${tarjeta}`);
    // Tarjeta de script (v0.2.2): el código COMPLETO como texto (nunca HTML) y, crítico, sin «Ejecutar».
    const codigoSmoke = 'print("<b>smoke</b>")\n# línea 2\n';
    chat.webContents.send('chat:tarjeta', {
      id: '5c1a7e00', clase: 'script', version: 1, sha256: 'a'.repeat(64), titulo: 'smoke', resumen: '', aviso: '',
      botones: ['copiar', 'descartar'], caduca_ts: null, canal_origen: 'escritorio', estado: 'pendiente',
      correo: null, drive: null, salida: null,
      script: {
        lenguaje: 'python', plataforma: process.platform === 'win32' ? 'windows' : 'linux', codigo: codigoSmoke,
        explicacion: '', efectos: [], requisitos: [], nivel: 'critico', ejecutable: false,
        motivo_no_ejecutable: 'smoke', metodo: 'ast', hallazgos: [], hallazgos_omitidos: 0, habilidad: null,
        habilidad_cambiada: false, ejecuciones: 0,
      },
    });
    const tarjetaScript = await chat.webContents.executeJavaScript(`new Promise((fin) => {
      const t0 = performance.now();
      const mirar = () => {
        const a = document.querySelector('article.msg.tarjeta.script[data-id="5c1a7e00"]');
        const c = a && a.querySelector('pre code');
        if (c) {
          const ejecutar = [...a.querySelectorAll('button')].some((b) => /Ejecutar/.test(b.textContent));
          return fin(c.textContent === ${JSON.stringify(codigoSmoke)} && !a.querySelector('b') && !ejecutar ? 'ok' : 'mal pintada');
        }
        if (performance.now() - t0 > 1500) return fin('sin tarjeta');
        setTimeout(mirar, 10);
      };
      mirar();
    })`);
    if (tarjetaScript !== 'ok') throw new Error(`tarjeta de script: ${tarjetaScript}`);
    if (PUTER) await smokePuter(puterCargado);
    if (overlay.isVisible() || chat.isVisible() || (vivo(puterWin) && puterWin.isVisible())) {
      throw new Error('una ventana quedó visible');
    }
    if (tray !== null) throw new Error('se creó la bandeja');
    terminar(0, `smoke OK (${Date.now() - t0} ms)`);
  }).catch((err) => terminar(1, `smoke FALLÓ: ${err && err.message}`));
}

principal();
