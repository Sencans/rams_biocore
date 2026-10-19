'use strict';
/**
 * Electron FALSO para probar `main.js` en Node puro: registra lo que el proceso principal hace con
 * ventanas, IPC, bandeja, atajos, sesiones, pantalla y notificaciones, sin abrir nada real (sin
 * ventanas, sin bandeja, sin atajos globales, sin red). Lo usa `main.test.js`.
 * Este archivo no ejecuta nada al cargarse.
 */
const { EventEmitter } = require('node:events');
const Module = require('node:module');
const path = require('node:path');

function crearElectronFalso({ cursor = { x: 1700, y: 900 }, escala = 1.5 } = {}) {
  const r = {
    ventanas: [],
    ipcOn: new Map(),
    ipcHandle: new Map(),
    atajos: [],
    bandejas: [],
    notificaciones: [],
    abiertas: [],
    loginItems: [],
    portapapeles: [], // textos que main.js copió (v0.2.2: solo el código de una tarjeta de script)
    sesiones: new Map(),
    app: { locks: [], quit: 0, exit: [], userModelId: null },
    cursor,
  };

  const PANTALLA = {
    id: 1,
    scaleFactor: escala,
    bounds: { x: 0, y: 0, width: 1920, height: 1080 },
    workArea: { x: 0, y: 0, width: 1920, height: 1040 },
  };

  class WebContentsFalso extends EventEmitter {
    constructor(win) {
      super();
      this.win = win;
      this.enviados = [];
      this.url = '';
      this.mainFrame = { parent: null, url: '', origin: 'null' };
      this.throttling = null;
      this.openHandler = null;
      this.recargas = 0;
    }

    send(canal, datos) { this.enviados.push([canal, datos]); }
    setBackgroundThrottling(v) { this.throttling = v; }
    setWindowOpenHandler(fn) { this.openHandler = fn; }
    getURL() { return this.url; }
    reloadIgnoringCache() { this.recargas += 1; }
    reload() { this.recargas += 1; }
    executeJavaScript() { return Promise.resolve(true); }

    /** Simula que el documento `url` empieza a navegar y termina de cargar. */
    simularCarga(url) {
      const u = new URL(url);
      this.url = url;
      this.mainFrame = { parent: null, url, origin: u.protocol === 'file:' ? 'file://' : u.origin };
      this.emit('did-start-navigation', { url, isMainFrame: true, isSameDocument: false, frame: this.mainFrame });
      this.emit('did-finish-load');
      this.win.emit('ready-to-show');
    }

    /** Evento IPC tal como lo vería ipcMain si viniera de este webContents. */
    evento(frame = this.mainFrame) {
      return { sender: this, senderFrame: frame };
    }

    canal(canal) { return this.enviados.filter((e) => e[0] === canal).map((e) => e[1]); }
  }

  class BrowserWindowFalsa extends EventEmitter {
    constructor(opciones) {
      super();
      this.opciones = opciones;
      this.webContents = new WebContentsFalso(this);
      this.llamadas = [];
      this.visible = false;
      this.enfocada = false;
      this.destruida = false;
      this.bounds = { x: opciones.x ?? 100, y: opciones.y ?? 100, width: opciones.width, height: opciones.height };
      r.ventanas.push(this);
    }

    reg(nombre, ...args) { this.llamadas.push([nombre, ...args]); }
    nombres() { return this.llamadas.map((l) => l[0]); }
    isDestroyed() { return this.destruida; }
    setAlwaysOnTop(...a) { this.reg('setAlwaysOnTop', ...a); }
    setVisibleOnAllWorkspaces(...a) { this.reg('setVisibleOnAllWorkspaces', ...a); }
    setIgnoreMouseEvents(...a) { this.reg('setIgnoreMouseEvents', ...a); }
    setContentProtection(...a) { this.reg('setContentProtection', ...a); }
    getContentSize() { return [this.bounds.width, this.bounds.height]; }
    getContentBounds() { return { ...this.bounds }; }
    getBounds() { return { ...this.bounds }; }
    getSize() { return [this.bounds.width, this.bounds.height]; }
    setBounds(b) { this.bounds = { ...b }; this.reg('setBounds', { ...b }); }
    setPosition(x, y) { this.bounds.x = x; this.bounds.y = y; this.reg('setPosition', x, y); }
    loadURL(url) { this.reg('loadURL', url); return Promise.resolve(); }
    loadFile(f) { this.reg('loadFile', f); return Promise.resolve(); }
    show() { this.visible = true; this.reg('show'); }
    showInactive() { this.visible = true; this.reg('showInactive'); }
    hide() { this.visible = false; this.enfocada = false; this.reg('hide'); }
    focus() { this.enfocada = true; this.reg('focus'); }
    removeMenu() { this.reg('removeMenu'); }
    /** Como Electron: 'close' se puede cancelar; si no, se destruye y emite 'closed'. */
    close() {
      this.reg('close');
      let cancelado = false;
      this.emit('close', { preventDefault: () => { cancelado = true; } });
      if (cancelado) return;
      this.destroy();
    }
    destroy() {
      if (this.destruida) return;
      this.destruida = true;
      this.visible = false;
      this.emit('closed');
    }
    isVisible() { return this.visible; }
    isFocused() { return this.enfocada; }
  }

  class SesionFalsa extends EventEmitter {
    constructor(nombre) {
      super();
      this.nombre = nombre;
      this.handlers = {};
      this.webRequest = { onHeadersReceived: (fn) => { this.handlers.headers = fn; } };
      this.protocolos = new Map();
      this.protocol = { handle: (esquema, fn) => { this.protocolos.set(esquema, fn); } };
      this.borrados = 0;
    }

    clearStorageData() { this.borrados += 1; return Promise.resolve(); }

    setPermissionRequestHandler(fn) { this.handlers.request = fn; }
    setPermissionCheckHandler(fn) { this.handlers.check = fn; }
    setDevicePermissionHandler(fn) { this.handlers.device = fn; }
    setDisplayMediaRequestHandler(fn) { this.handlers.display = fn; }
  }
  const sesion = (nombre) => {
    if (!r.sesiones.has(nombre)) r.sesiones.set(nombre, new SesionFalsa(nombre));
    return r.sesiones.get(nombre);
  };

  class TrayFalsa extends EventEmitter {
    constructor(img) {
      super();
      this.img = img;
      this.menu = null;
      this.tooltip = '';
      r.bandejas.push(this);
    }

    setContextMenu(m) { this.menu = m; }
    setToolTip(t) { this.tooltip = t; }
    setImage(img) { this.img = img; }
    isDestroyed() { return false; }
    destroy() {}
  }

  class NotificationFalsa extends EventEmitter {
    static isSupported() { return true; }
    constructor(o) { super(); this.opciones = o; }
    show() { r.notificaciones.push(this); }
  }

  const app = new EventEmitter();
  Object.assign(app, {
    commandLine: { getSwitchValue: () => '' },
    isPackaged: false,
    getVersion: () => '0.2.0',
    getAppPath: () => path.join(__dirname, '..'),
    whenReady: () => Promise.resolve(),
    requestSingleInstanceLock: (datos) => { r.app.locks.push(datos); return true; },
    setAppUserModelId: (id) => { r.app.userModelId = id; },
    setLoginItemSettings: (o) => r.loginItems.push(o),
    quit: () => { r.app.quit += 1; },
    exit: (c) => r.app.exit.push(c),
    setPath: () => {},
    disableHardwareAcceleration: () => {},
  });

  const electron = {
    app,
    BrowserWindow: BrowserWindowFalsa,
    Tray: TrayFalsa,
    Menu: { buildFromTemplate: (t) => t },
    globalShortcut: {
      register: (acel, fn) => { r.atajos.push([acel, fn]); return true; },
      unregisterAll: () => { r.atajos.length = 0; },
    },
    screen: Object.assign(new EventEmitter(), {
      getCursorScreenPoint: () => ({ ...r.cursor }),
      getDisplayNearestPoint: () => PANTALLA,
      getPrimaryDisplay: () => PANTALLA,
      getDisplayMatching: () => PANTALLA,
      dipToScreenRect: (_w, x) => ({ x: x.x * escala, y: x.y * escala, width: x.width * escala, height: x.height * escala }),
      dipToScreenPoint: (p) => ({ x: p.x * escala, y: p.y * escala }),
    }),
    ipcMain: {
      on: (canal, fn) => r.ipcOn.set(canal, fn),
      handle: (canal, fn) => r.ipcHandle.set(canal, fn),
    },
    nativeImage: { createFromPath: (p) => ({ ruta: p, isEmpty: () => false }) },
    session: {
      get defaultSession() { return sesion('(defecto)'); },
      fromPartition: (n) => sesion(n),
    },
    shell: { openExternal: (u) => { r.abiertas.push(u); return Promise.resolve(); } },
    Notification: NotificationFalsa,
    // Portapapeles FALSO: nunca toca el del sistema.
    clipboard: {
      writeText: (t) => { r.portapapeles.push(t); },
      readText: () => (r.portapapeles.length ? r.portapapeles.at(-1) : ''),
    },
  };

  BrowserWindowFalsa.getAllWindows = () => r.ventanas.filter((v) => !v.destruida);

  const ayuda = {
    overlay: () => r.ventanas.find((v) => v.opciones.title === 'Amadeus Overlay'),
    chat: () => r.ventanas.find((v) => v.opciones.title === 'Amadeus Chat'),
    puter: () => r.ventanas.find((v) => v.opciones.title === 'Amadeus Puter'),
    conectar: () => r.ventanas.filter((v) => v.opciones.title === 'Conectar Puter').at(-1),
    ipc: (canal, evento, ...args) => {
      const fn = r.ipcOn.get(canal);
      if (!fn) throw new Error(`sin manejador para ${canal}`);
      return fn(evento, ...args);
    },
    invocar: (canal, evento, ...args) => r.ipcHandle.get(canal)(evento, ...args),
  };
  return { electron, r, ...ayuda };
}

/** WebSocket falso (global) con el que `main.js` cree hablar con R.A.M.S. */
function crearWebSocketFalso() {
  const instancias = [];
  class WebSocketFalso {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.enviados = [];
      this.oyentes = {};
      instancias.push(this);
    }

    addEventListener(tipo, fn) { (this.oyentes[tipo] ||= []).push(fn); }
    emitir(tipo, ev) { for (const fn of this.oyentes[tipo] || []) fn(ev); }
    send(s) { this.enviados.push(JSON.parse(s)); }
    close() { this.readyState = 3; }
    abrir() { this.readyState = 1; this.emitir('open', {}); }
    recibir(obj) { this.emitir('message', { data: JSON.stringify(obj) }); }
    cerrar(code = 1006) { this.readyState = 3; this.emitir('close', { code }); }
  }
  return { WebSocketFalso, instancias };
}

/**
 * Carga `main.js` con el Electron falso. Sustituye `process.argv` durante la carga y, mientras dure
 * el escenario, `fetch`, `WebSocket`, variables de entorno y la consola. Devuelve `restaurar()`.
 */
function cargarMain({ electron, argv = [], env = {}, fetch, WebSocket }) {
  const rutaMain = require.resolve('../main.js');
  const original = {
    load: Module._load,
    argv: process.argv,
    fetch: globalThis.fetch,
    WebSocket: globalThis.WebSocket,
    log: console.log,
    warn: console.warn,
    env: Object.fromEntries(Object.keys(env).map((k) => [k, process.env[k]])),
  };
  const logs = [];
  Module._load = function cargar(peticion, ...resto) {
    if (peticion === 'electron') return electron;
    return original.load.call(this, peticion, ...resto);
  };
  process.argv = [process.execPath, path.join(__dirname, '..'), ...argv];
  globalThis.fetch = fetch;
  globalThis.WebSocket = WebSocket;
  console.log = (...a) => logs.push(a.join(' '));
  console.warn = (...a) => logs.push(a.join(' '));
  for (const [k, v] of Object.entries(env)) process.env[k] = v;
  delete require.cache[rutaMain];
  try {
    require(rutaMain);
  } finally {
    Module._load = original.load;
    process.argv = original.argv;
  }
  return {
    logs,
    restaurar() {
      globalThis.fetch = original.fetch;
      globalThis.WebSocket = original.WebSocket;
      console.log = original.log;
      console.warn = original.warn;
      for (const [k, v] of Object.entries(original.env)) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
      }
      delete require.cache[rutaMain];
    },
  };
}

/** Deja correr promesas y E/S pendientes (sin depender de setTimeout, que puede estar simulado). */
async function asentar(vueltas = 30) {
  for (let i = 0; i < vueltas; i++) await new Promise((res) => setImmediate(res));
}

/**
 * Espera (con tiempo real, máx. `ms`) a que se cumpla `cond`, p. ej. una lectura de disco. Usa
 * `performance.now()` porque `Date` puede estar simulado.
 */
async function esperar(cond, ms = 3000) {
  const t0 = performance.now();
  while (!cond() && performance.now() - t0 < ms) await new Promise((res) => setImmediate(res));
  await asentar(5);
  return cond();
}

module.exports = { crearElectronFalso, crearWebSocketFalso, cargarMain, asentar, esperar };
