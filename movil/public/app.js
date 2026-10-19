"use strict";
/**
 * Lógica cliente de Amadeus Móvil Android (Videollamada 3D + R.A.M.S. PC / Modo Local).
 * Cumple las reglas de seguridad de IMPORTANTEIA.md:
 * - Todo texto dinámico se escribe únicamente con `textContent` y atributos (nunca `innerHTML`).
 * - No se inventan lecturas de sensores ni datos de roleplay falsos: donde no hay sensor se muestra "Sin lectura".
 * - Toda entrada del usuario cuando está conectado al PC se envía por `/escritorio` como `chat`
 *   para que pase por `core.intenciones.enrutar` y `CompuertaPermisos.autorizar()`.
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const poner = (el, txt) => { if (el) { el.textContent = String(txt ?? ""); } };
  const cadena = (v, max = 300) => {
    if (typeof v !== "string") { return ""; }
    return v.length > max ? v.slice(0, max - 1) + "…" : v;
  };
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const acotar = (v, min, max) => Math.min(max, Math.max(min, v));
  const coma = (v, dec = 1) => {
    const n = num(v);
    if (n === null) { return "Sin lectura"; }
    return n.toFixed(dec).replace(".", ",");
  };

  const ID_CONFIRMACION = /^[0-9a-f]{8}$/;
  const TOKEN_VALIDO = /^[A-Za-z0-9_-]{12,128}$/;
  const MAX_TEXTO_CHAT = 2000;
  const MAX_ITEMS_LISTA = 150;

  // Catálogo oficial de VTuber AI Studio (lib/protocol.js)
  const VRM_EMOCIONES = [
    ["neutral", "Neutral"],
    ["happy", "Feliz"],
    ["angry", "Enojo"],
    ["sad", "Triste"],
    ["relaxed", "Tranquila"],
    ["surprised", "Sorpresa"]
  ];
  const VRM_POSES = [
    ["relaxed", "Relajada"],
    ["tpose", "Pose T"],
    ["apose", "Pose A"],
    ["arms_up", "Brazos arriba"],
    ["hands_on_hips", "Manos en cadera"],
    ["arms_crossed", "Brazos cruzados"],
    ["thinking", "Pensando"],
    ["shy", "Tímida"]
  ];
  const VRM_GESTOS = [
    ["wave", "Saludar"],
    ["nod", "Asentir"],
    ["shake_head", "Negar"],
    ["bow", "Reverencia"],
    ["tilt_head", "Ladear cabeza"],
    ["jump", "Saltar"],
    ["clap", "Aplaudir"],
    ["shrug", "Encogerse"],
    ["cheer", "Celebrar"],
    ["point", "Señalar"],
    ["dance", "Bailar"],
    ["laugh", "Reír"],
    ["look_around", "Mirar alrededor"],
    ["think", "Pensar"],
    ["surprise", "Sorprenderse"]
  ];
  const VRM_MIRADAS = [
    ["camera", "Cámara"],
    ["mouse", "Cursor"],
    ["forward", "Frente"],
    ["left", "Izquierda"],
    ["right", "Derecha"],
    ["up", "Arriba"],
    ["down", "Abajo"],
    ["up_left", "Arriba-Izq"],
    ["up_right", "Arriba-Der"],
    ["down_left", "Abajo-Izq"],
    ["down_right", "Abajo-Der"],
    ["none", "Libre"]
  ];
  const VRM_MANOS = [
    ["open", "Abierta"],
    ["relaxed", "Relajada"],
    ["fist", "Puño"],
    ["point", "Señalar"],
    ["peace", "Paz"],
    ["thumbs_up", "Pulgar arriba"]
  ];
  const VRM_HUESOS = [
    "head", "neck", "upperChest", "chest", "spine", "hips",
    "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
    "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "rightUpperLeg", "rightLowerLeg"
  ];
  const VRM_CAMARAS = [
    ["bust", "Busto"],
    ["face", "Rostro"],
    ["full", "Cuerpo"],
    ["wide", "Amplio"]
  ];
  const VRM_FONDOS = [
    ["gradient", "Degradado"],
    ["transparent", "Transparente"],
    ["green", "Verde"],
    ["blue", "Azul"],
    ["magenta", "Magenta"]
  ];
  const VRM_TEMAS = [
    ["amadeus", "Amadeus (Kurisu)"],
    ["none", "Original"]
  ];

  const EJEMPLOS_CONSOLA = {
    saludo: {
      type: "speak",
      text: "Hola, ya estoy lista en la videollamada.",
      emotion: "happy",
      gesture: "wave"
    },
    pensar: {
      type: "sequence",
      steps: [
        { type: "pose", name: "thinking", duration: 0.5 },
        { type: "expression", name: "relaxed", value: 0.7 },
        { type: "look", at: "up_right" },
        { type: "gesture", name: "tilt_head" }
      ]
    },
    secuencia: {
      type: "sequence",
      steps: [
        { type: "expression", name: "happy", value: 0.9 },
        { type: "gesture", name: "cheer", wait: 1.4 },
        { type: "gesture", name: "dance", wait: 2.2 },
        { type: "pose", name: "hands_on_hips" }
      ]
    },
    huesos: {
      type: "bones",
      rotations: {
        head: { x: -8, y: 15, z: 6 },
        rightUpperArm: { x: 0, y: 0, z: -35 }
      },
      duration: 0.4
    },
    mirada: {
      type: "look",
      at: "camera"
    },
    reset: {
      type: "reset"
    }
  };

  const METRICAS_CONECTOMA = [
    ["arousal", "Arousal (Activación)"],
    ["foco", "Foco (Atención)"],
    ["aburrimiento", "Aburrimiento"],
    ["fatiga", "Fatiga sináptica"],
    ["curiosidad", "Curiosidad"],
    ["presion_sueno", "Presión de sueño"],
    ["energia", "Energía"],
    ["dopamina", "Dopamina"],
    ["serotonina", "Serotonina"]
  ];

  const paramsUrl = new URLSearchParams(location.search);
  const esAppNativa = location.hostname === "amadeus.local" || Boolean(window.AmadeusAndroid);

  function leerPref(clave, defecto = "") {
    try {
      if (window.AmadeusAndroid && typeof window.AmadeusAndroid.leerConfig === "function") {
        const v = window.AmadeusAndroid.leerConfig(clave);
        if (v) { return String(v); }
      }
      return localStorage.getItem(clave) || defecto;
    } catch (_) {
      return defecto;
    }
  }

  function guardarPref(clave, valor) {
    try { localStorage.setItem(clave, valor); } catch (_) {}
    try {
      if (window.AmadeusAndroid && typeof window.AmadeusAndroid.guardarConfig === "function") {
        window.AmadeusAndroid.guardarConfig(clave, String(valor || ""));
      }
    } catch (_) {}
  }

  const estado = {
    urlServidor: leerPref("amadeus.servidor.url", esAppNativa ? "http://127.0.0.1:8767" : location.origin),
    tokenMovil: paramsUrl.get("t") || leerPref("amadeus.movil.token", ""),
    tokenHud: extraerTokenHud(paramsUrl.get("hud_t") || leerPref("amadeus.hud.token", "")),
    tokenEscritorio: "",
    nombreIA: "Amadeus",
    enLlamada: false,
    modoLlamada: "espera", // "pc" | "local" | "espera"
    wsHud: null,
    wsEscritorio: null,
    hudConectado: false,
    escConectado: false,
    inicioTs: Date.now() / 1000,
    silenciado: false,
    noMolestar: false,
    permisoVivoId: null,
    sinLeerRine: 0,
    vistaActual: "vista-vivo",
    modulos: new Map(),
    barrasConectoma: new Map(),
    temporizadorSubtitulo: null,
    temporizadorOndas: null,
    urlBlobVrmLocal: null,
    encuadreIdx: 0,
    hudOculto: false,
    ttsActivo: true,
    ttsPitch: 1.08,
    ttsVozNombre: "",
    bateriaPctReal: null,
    // Visión de cámara móvil en vivo
    streamCamaraLocal: null,
    camaraFrontal: true,
    timerVisionMovil: null,
    detectorRostroNativo: null,
    cuadroPrevioGris: null,
    visionMovil: {
      activa: false,
      rostroDetectado: false,
      posX: "centro",
      posY: "centro",
      cercania: "distancia media",
      brilloPct: 0,
      movimientoPct: 0,
      ultimaMiradaAvatar: "camera",
      ultimoSaludoVisualTs: 0,
      yaAnuncioApertura: false,
      estabaPresente: false
    },
    // Memoria y control anti-repetición para conversación local
    historialAutonomo: [],
    frasesUsadas: new Set(),
    memoriaCharla: {
      nombreUsuario: leerPref("amadeus.usuario.nombre", ""),
      actividadActual: "",
      estadoAnimo: "",
      ultimoTema: "",
      ultimaPreguntaIA: "",
      notas: [],
      turnos: 0
    }
  };

  try {
    const notasGuardadas = JSON.parse(leerPref("amadeus.usuario.notas", "[]"));
    if (Array.isArray(notasGuardadas)) {
      estado.memoriaCharla.notas = notasGuardadas.slice(0, 30);
    }
  } catch (_) {}

  if (paramsUrl.get("t")) {
    guardarPref("amadeus.movil.token", estado.tokenMovil);
  }
  if (paramsUrl.get("hud_t")) {
    guardarPref("amadeus.hud.token", estado.tokenHud);
  }

  function parsearServidorYToken(entradaUrl) {
    const limpio = String(entradaUrl || "").trim();
    if (!limpio) {
      return { base: esAppNativa ? "http://127.0.0.1:8767" : location.origin, token: "" };
    }
    try {
      const conProto = /^https?:\/\//i.test(limpio) ? limpio : ("http://" + limpio);
      const u = new URL(conProto);
      const tok = (u.searchParams.get("t") || "").trim();
      return {
        base: u.origin,
        token: TOKEN_VALIDO.test(tok) ? tok : ""
      };
    } catch (_) {
      return { base: esAppNativa ? "http://127.0.0.1:8767" : location.origin, token: "" };
    }
  }

  function baseServidorHttp() {
    if (!esAppNativa && (!estado.urlServidor || estado.urlServidor === location.origin)) {
      return "";
    }
    return String(estado.urlServidor || "http://127.0.0.1:8767").replace(/\/+$/, "");
  }

  function baseServidorWs() {
    const httpBase = baseServidorHttp();
    if (!httpBase) {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      return `${proto}//${location.host}`;
    }
    return httpBase.replace(/^http:/i, "ws:").replace(/^https:/i, "wss:");
  }

  function extraerTokenHud(entrada) {
    const limpio = String(entrada || "").trim();
    if (!limpio) { return ""; }
    let candidato = limpio;
    try {
      if (limpio.includes("?")) {
        const q = limpio.slice(limpio.indexOf("?"));
        candidato = (new URLSearchParams(q).get("t") || "").trim();
      }
    } catch (_) {}
    if (!candidato || candidato.includes("<") || candidato.includes(">")) {
      return "";
    }
    return TOKEN_VALIDO.test(candidato) ? candidato : "";
  }

  // ------------------------------------------------------------------
  // 1.Flujo de Videollamada: "Llamar a Amadeus" (PC en red o Local) y "Colgar"
  // ------------------------------------------------------------------
  async function sondearServidorPc(timeoutMs = 1500) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const url = `${baseServidorHttp()}/rams/info?t=${encodeURIComponent(estado.tokenMovil)}`;
      const r = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
      clearTimeout(t);
      if (!r.ok) { return null; }
      const info = await r.json();
      if (info && info.token_hud_inicial && !estado.tokenHud) {
        estado.tokenHud = extraerTokenHud(info.token_hud_inicial);
        guardarPref("amadeus.hud.token", estado.tokenHud);
      }
      return info;
    } catch (_) {
      clearTimeout(t);
      return null;
    }
  }

  async function iniciarVideollamada() {
    const btnLlamar = $("btn-llamar-amadeus");
    const subEstado = $("llamada-estado-red");
    if (btnLlamar) {
      btnLlamar.disabled = true;
      poner(btnLlamar, "Conectando videollamada…");
    }
    if (subEstado) {
      poner(subEstado, "Buscando a Amadeus en tu PC…");
    }

    const infoPc = await sondearServidorPc(1500);
    estado.enLlamada = true;
    estado.inicioTs = Date.now() / 1000;

    const pantallaLlamada = $("pantalla-llamada");
    if (pantallaLlamada) {
      pantallaLlamada.classList.add("oculta");
      setTimeout(() => { pantallaLlamada.hidden = true; }, 280);
    }
    if (btnLlamar) {
      btnLlamar.disabled = false;
      poner(btnLlamar, "Llamar a Amadeus");
    }

    setTimeout(redimensionarVisor3D, 80);

    if (infoPc) {
      estado.modoLlamada = "pc";
      conectarEscritorio();
      conectarHud();
      actualizarEstadoCabecera();
      registrarEventoBitacora("Videollamada conectada con Amadeus en PC.");
      setTimeout(() => {
        if (!estado.escConectado) {
          saludarInicioLlamada("pc");
        }
      }, 900);
    } else {
      estado.modoLlamada = "local";
      actualizarEstadoCabecera();
      registrarEventoBitacora("Videollamada iniciada en modo local (móvil).");
      saludarInicioLlamada("local");
    }
  }

  function saludarInicioLlamada(modo) {
    const h = new Date().getHours();
    const nom = estado.memoriaCharla.nombreUsuario ? (", " + estado.memoriaCharla.nombreUsuario) : "";
    let saludoTiempo = "Hola";
    if (h >= 5 && h < 12) { saludoTiempo = "Buenos días"; }
    else if (h >= 12 && h < 19) { saludoTiempo = "Buenas tardes"; }
    else { saludoTiempo = "Buenas noches"; }

    if (modo !== "pc") {
      estado.memoriaCharla.ultimaPreguntaIA = "estado_usuario";
    }
    const texto = modo === "pc"
      ? `${saludoTiempo}${nom}. Videollamada conectada con tu PC. ¿En qué te ayudo?`
      : `${saludoTiempo}${nom}. Contestando desde el modo local de tu teléfono. ¿Cómo estás?`;

    entregarRespuestaAmadeus(texto, "happy", "wave");
  }

  function colgarVideollamada() {
    enviarControlHud("colgar");
    estado.enLlamada = false;
    estado.modoLlamada = "espera";

    // Detener voz y cámara si estaban activas
    if (window.AmadeusAndroid && typeof window.AmadeusAndroid.callar === "function") {
      window.AmadeusAndroid.callar();
    }
    if ("speechSynthesis" in window) {
      try { window.speechSynthesis.cancel(); } catch (_) {}
    }
    alternarCamaraMovil(true);

    if (estado.wsEscritorio) {
      try { estado.wsEscritorio.close(); } catch (_) {}
      estado.wsEscritorio = null;
      estado.escConectado = false;
    }
    if (estado.wsHud) {
      try { estado.wsHud.close(); } catch (_) {}
      estado.wsHud = null;
      estado.hudConectado = false;
    }

    actualizarEstadoCabecera();
    poner($("reloj-llamada"), "00:00");
    poner($("llamada-estado-red"), "Llamada finalizada · Lista para volver a llamar");

    const pantallaLlamada = $("pantalla-llamada");
    if (pantallaLlamada) {
      pantallaLlamada.hidden = false;
      requestAnimationFrame(() => pantallaLlamada.classList.remove("oculta"));
    }
  }

  if ($("btn-llamar-amadeus")) {
    $("btn-llamar-amadeus").addEventListener("click", iniciarVideollamada);
  }
  if ($("btn-config-desde-llamada")) {
    $("btn-config-desde-llamada").addEventListener("click", () => abrirModalConfig());
  }
  if ($("btn-vrm-desde-llamada")) {
    $("btn-vrm-desde-llamada").addEventListener("click", () => {
      const inp = $("input-vrm-local");
      if (inp) { inp.click(); }
    });
  }

  // Animación de las 9 barras de onda de voz
  function fijarOndasVoz(nivel0a1) {
    const cont = $("ondas-voz");
    if (!cont) { return; }
    const barras = cont.querySelectorAll("i");
    const v = acotar(Number(nivel0a1) || 0, 0, 1);
    for (let i = 0; i < barras.length; i++) {
      if (v <= 0.02) {
        barras[i].style.height = "3px";
      } else {
        const peso = Math.sin(((i + 1) / (barras.length + 1)) * Math.PI);
        const jitter = 0.65 + 0.35 * Math.sin(Date.now() * 0.025 + i * 1.3);
        const h = Math.max(3, Math.round(3 + 10 * v * peso * jitter));
        barras[i].style.height = h + "px";
      }
    }
  }

  function iniciarPulsoOndasTemporal(duracionMs) {
    if (estado.temporizadorOndas) {
      clearInterval(estado.temporizadorOndas);
    }
    const fin = Date.now() + duracionMs;
    poner($("voz-estado-texto"), "Hablando…");
    estado.temporizadorOndas = setInterval(() => {
      if (Date.now() >= fin) {
        clearInterval(estado.temporizadorOndas);
        estado.temporizadorOndas = null;
        fijarOndasVoz(0);
        poner($("voz-estado-texto"), estado.silenciado ? "Silenciada" : "En escucha");
        return;
      }
      const pseudo = 0.35 + 0.45 * Math.abs(Math.sin(Date.now() * 0.014));
      fijarOndasVoz(pseudo);
    }, 85);
  }

  // ------------------------------------------------------------------
  // 2. Navegación por pestañas y Escenario 3D VRM a pantalla completa
  // ------------------------------------------------------------------
  function redimensionarVisor3D() {
    try {
      const marco = $("marco-vrm");
      if (marco && marco.contentWindow) {
        marco.contentWindow.dispatchEvent(new Event("resize"));
      }
    } catch (_) {}
  }

  function cambiarVista(idVista) {
    estado.vistaActual = idVista;
    document.querySelectorAll(".vista").forEach((el) => {
      el.classList.toggle("activa", el.id === idVista);
    });
    document.querySelectorAll(".tab-nav").forEach((btn) => {
      btn.classList.toggle("activa", btn.getAttribute("data-vista") === idVista);
    });

    const escenaGlobal = $("escena-vrm-global");
    if (escenaGlobal) {
      escenaGlobal.classList.remove("modo-vivo", "modo-estudio", "modo-oculto");
      if (idVista === "vista-vivo") {
        escenaGlobal.classList.add("modo-vivo");
      } else if (idVista === "vista-vtuber") {
        escenaGlobal.classList.add("modo-estudio");
      } else {
        escenaGlobal.classList.add("modo-oculto");
      }
    }

    setTimeout(redimensionarVisor3D, 60);
    setTimeout(redimensionarVisor3D, 250);

    if (idVista === "vista-rine") {
      estado.sinLeerRine = 0;
      actualizarInsigniaRine();
    }
    if (idVista === "vista-vtuber") {
      refrescarEstadoVtuber();
    }
  }

  document.querySelectorAll(".tab-nav").forEach((btn) => {
    btn.addEventListener("click", () => {
      const destino = btn.getAttribute("data-vista");
      if (destino) { cambiarVista(destino); }
    });
  });

  function cargarVisor3D() {
    const iframe = $("marco-vrm");
    if (!iframe) { return; }
    const presetActual = VRM_CAMARAS[estado.encuadreIdx]?.[0] || "bust";
    const qs = new URLSearchParams({
      obs: "1",
      camera: presetActual,
      theme: "amadeus",
      t: estado.tokenMovil
    });
    iframe.src = "/visor3d?" + qs.toString();
  }

  if ($("btn-recargar-vrm")) {
    $("btn-recargar-vrm").addEventListener("click", cargarVisor3D);
  }

  if ($("btn-encuadre-vrm")) {
    $("btn-encuadre-vrm").addEventListener("click", () => {
      estado.encuadreIdx = (estado.encuadreIdx + 1) % VRM_CAMARAS.length;
      const [preset, etiqueta] = VRM_CAMARAS[estado.encuadreIdx];
      poner($("btn-encuadre-vrm"), "Encuadre: " + etiqueta);
      enviarComandoVtuber({ type: "camera", preset });
    });
  }

  function alternarHudLimpio(ocultar) {
    estado.hudOculto = typeof ocultar === "boolean" ? ocultar : !estado.hudOculto;
    const capa = $("hud-vivo-capa");
    const btnRestaurar = $("btn-restaurar-hud");
    if (capa) { capa.hidden = estado.hudOculto; }
    if (btnRestaurar) { btnRestaurar.hidden = !estado.hudOculto; }
  }
  if ($("btn-ocultar-hud")) {
    $("btn-ocultar-hud").addEventListener("click", () => alternarHudLimpio(true));
  }
  if ($("btn-restaurar-hud")) {
    $("btn-restaurar-hud").addEventListener("click", () => alternarHudLimpio(false));
  }

  setInterval(() => {
    if (!estado.enLlamada) { return; }
    const segs = Math.max(0, Math.floor(Date.now() / 1000 - estado.inicioTs));
    const mm = String(Math.floor(segs / 60)).padStart(2, "0");
    const ss = String(segs % 60).padStart(2, "0");
    poner($("reloj-llamada"), mm + ":" + ss);
  }, 1000);

  // ------------------------------------------------------------------
  // 3. Sistema y Sensores Reales (Cero datos inventados)
  // ------------------------------------------------------------------
  function inicializarBarrasConectoma() {
    const cont = $("contenedor-barras-conectoma");
    if (!cont) { return; }
    METRICAS_CONECTOMA.forEach(([clave, etiqueta]) => {
      const fila = document.createElement("div");
      fila.className = "metrica-fila";
      const cab = document.createElement("div");
      cab.className = "metrica-cab";
      const spNom = document.createElement("span");
      spNom.textContent = etiqueta;
      const spVal = document.createElement("span");
      spVal.textContent = "Sin lectura";
      cab.appendChild(spNom);
      cab.appendChild(spVal);

      const fondo = document.createElement("div");
      fondo.className = "barra-fondo";
      const relleno = document.createElement("div");
      relleno.className = "barra-relleno";
      fondo.appendChild(relleno);

      fila.appendChild(cab);
      fila.appendChild(fondo);
      cont.appendChild(fila);
      estado.barrasConectoma.set(clave, { spVal, relleno });
    });
  }

  function limpiarLecturasPcDesconectado() {
    poner($("tele-cpu"), "Sin lectura (PC desconectado)");
    poner($("tele-ram"), "Sin lectura (PC desconectado)");
    poner($("tele-temp"), "Sin lectura (PC desconectado)");
    poner($("tele-proceso"), "Sin lectura (PC desconectado)");
    const nota = $("nota-conectoma-pc");
    if (nota) { nota.hidden = false; }
    METRICAS_CONECTOMA.forEach(([clave]) => {
      const item = estado.barrasConectoma.get(clave);
      if (item) {
        poner(item.spVal, "Sin lectura");
        item.relleno.style.width = "0%";
      }
    });
  }

  function registrarEventoBitacora(texto) {
    const ul = $("lista-eventos");
    if (!ul) { return; }
    const li = document.createElement("li");
    const f = new Date();
    const hh = String(f.getHours()).padStart(2, "0");
    const mm = String(f.getMinutes()).padStart(2, "0");
    const ss = String(f.getSeconds()).padStart(2, "0");
    li.textContent = `[${hh}:${mm}:${ss}] ${texto}`;
    ul.insertBefore(li, ul.firstChild);
    while (ul.children.length > MAX_ITEMS_LISTA) {
      ul.removeChild(ul.lastChild);
    }
  }

  function registrarActividadVtuber(comando) {
    const ul = $("vrm-log");
    if (!ul) { return; }
    const li = document.createElement("li");
    const f = new Date();
    const hh = String(f.getHours()).padStart(2, "0");
    const mm = String(f.getMinutes()).padStart(2, "0");
    const ss = String(f.getSeconds()).padStart(2, "0");
    const resumen = JSON.stringify(comando);
    li.textContent = `[${hh}:${mm}:${ss}] ${cadena(resumen, 140)}`;
    ul.insertBefore(li, ul.firstChild);
    while (ul.children.length > 60) {
      ul.removeChild(ul.lastChild);
    }
  }

  function actualizarCeldasBateria(pctNumero) {
    const p = acotar(Math.round(Number(pctNumero) || 0), 0, 100);
    estado.bateriaPctReal = p;
    poner($("bateria-top-txt"), p + "%");
    poner($("tele-bat"), p + " %");
    const cont = $("bateria-celdas");
    if (!cont) { return; }
    const celdas = cont.querySelectorAll("i");
    const activas = Math.max(1, Math.ceil((p / 100) * 4));
    for (let i = 0; i < celdas.length; i++) {
      celdas[i].classList.toggle("on", i < activas);
    }
  }

  function actualizarBarrasSenal(barrasActivas, etiquetaModo) {
    const cont = $("senal-barras");
    if (cont) {
      const b = cont.querySelectorAll("b");
      for (let i = 0; i < b.length; i++) {
        b[i].classList.toggle("on", i < barrasActivas);
      }
    }
    if (etiquetaModo) {
      poner($("senal-ms"), etiquetaModo);
    }
  }

  // ------------------------------------------------------------------
  // 4. Motor de Visión Local por Cámara (Amadeus ve y sigue tu rostro)
  // ------------------------------------------------------------------
  function inicializarDetectorRostro() {
    if ("FaceDetector" in window && !estado.detectorRostroNativo) {
      try {
        estado.detectorRostroNativo = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 1 });
      } catch (_) {
        estado.detectorRostroNativo = null;
      }
    }
  }

  function esTonoPiel(r, g, b) {
    // Regla cromática RGB/YCbCr estándar para detección rápida de piel/rostro bajo distinta luz
    if (r < 55 || g < 30 || b < 15) { return false; }
    if (r <= g || r <= b) { return false; }
    if (r - Math.min(g, b) < 12) { return false; }
    const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
    const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
    return cb >= 75 && cb <= 138 && cr >= 130 && cr <= 178;
  }

  async function analizarCuadroCamara() {
    const video = $("video-camara-local");
    const canvas = $("canvas-vision-local");
    if (!estado.streamCamaraLocal || !video || !canvas || video.readyState < 2) {
      return;
    }
    const w = canvas.width;
    const h = canvas.height;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) { return; }

    ctx.drawImage(video, 0, 0, w, h);
    const img = ctx.getImageData(0, 0, w, h).data;
    const totalPx = w * h;
    const grisActual = new Uint8Array(totalPx);

    let sumaLuma = 0;
    let sumaDiff = 0;
    let pielCont = 0;
    let sumaPielX = 0;
    let sumaPielY = 0;

    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = y * w + x;
        const p = i * 4;
        const r = img[p];
        const g = img[p + 1];
        const b = img[p + 2];
        const luma = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
        grisActual[i] = luma;
        sumaLuma += luma;

        if (estado.cuadroPrevioGris) {
          sumaDiff += Math.abs(luma - estado.cuadroPrevioGris[i]);
        }

        // Ponderar región central/superior donde suele ubicarse el rostro en videollamada
        if (y >= 3 && y <= h - 4 && x >= 4 && x <= w - 5 && esTonoPiel(r, g, b)) {
          pielCont += 1;
          sumaPielX += x;
          sumaPielY += y;
        }
      }
    }
    estado.cuadroPrevioGris = grisActual;

    const brilloPct = Math.round((sumaLuma / (totalPx * 255)) * 100);
    const movimientoPct = Math.min(100, Math.round((sumaDiff / (totalPx * 42)) * 100));

    let rostro = false;
    let normX = 0.5;
    let normY = 0.5;
    let ratioArea = pielCont / totalPx;

    // 1. Intentar detector nativo de rostros de Android WebView si existe
    if (estado.detectorRostroNativo) {
      try {
        const caras = await estado.detectorRostroNativo.detect(video);
        if (caras && caras.length > 0) {
          const box = caras[0].boundingBox;
          const vw = video.videoWidth || 320;
          const vh = video.videoHeight || 240;
          rostro = true;
          normX = (box.x + box.width * 0.5) / vw;
          normY = (box.y + box.height * 0.5) / vh;
          ratioArea = (box.width * box.height) / (vw * vh);
        }
      } catch (_) {
        estado.detectorRostroNativo = null;
      }
    }

    // 2. Respaldo determinista por píxeles (funciona 100% offline en cualquier Android)
    if (!rostro && brilloPct >= 14 && ratioArea >= 0.045 && ratioArea <= 0.72) {
      rostro = true;
      normX = (sumaPielX / pielCont) / w;
      normY = (sumaPielY / pielCont) / h;
    }

    // En cámara frontal la imagen viene en espejo respecto al usuario
    const xVistaAvatar = estado.camaraFrontal ? (1 - normX) : normX;
    let posX = "centro";
    if (xVistaAvatar < 0.36) { posX = "izquierda"; }
    else if (xVistaAvatar > 0.64) { posX = "derecha"; }

    let posY = "centro";
    if (normY < 0.34) { posY = "arriba"; }
    else if (normY > 0.66) { posY = "abajo"; }

    let cercania = "distancia media";
    if (ratioArea > 0.24) { cercania = "cerca"; }
    else if (ratioArea < 0.09) { cercania = "lejos"; }

    const vm = estado.visionMovil;
    vm.activa = true;
    vm.brilloPct = brilloPct;
    vm.movimientoPct = movimientoPct;
    vm.rostroDetectado = rostro;
    vm.posX = posX;
    vm.posY = posY;
    vm.cercania = cercania;

    // Actualizar indicadores visuales reales en la interfaz
    poner($("vis-estado"), estado.camaraFrontal ? "Activa (Frontal)" : "Activa (Trasera)");
    poner($("vis-brillo"), brilloPct + " %");
    poner($("vis-movimiento"), movimientoPct + " %");

    if (rostro) {
      const descRostro = `Te veo (${posX}, ${cercania})`;
      poner($("vis-rostro"), descRostro);
      poner($("pip-estado-vision"), `👁 Te veo · luz ${brilloPct}%`);
      poner($("camara-texto"), `Te veo (${posX} · luz ${brilloPct}%)`);

      // Hacer que los ojos y cabeza del modelo 3D sigan al usuario en tiempo real
      let miradaObj = "camera";
      if (posX === "izquierda" && posY === "arriba") { miradaObj = "up_left"; }
      else if (posX === "derecha" && posY === "arriba") { miradaObj = "up_right"; }
      else if (posX === "izquierda") { miradaObj = "left"; }
      else if (posX === "derecha") { miradaObj = "right"; }
      else if (posY === "arriba") { miradaObj = "up"; }
      else if (posY === "abajo") { miradaObj = "down"; }

      if (miradaObj !== vm.ultimaMiradaAvatar) {
        vm.ultimaMiradaAvatar = miradaObj;
        enviarComandoVtuber({ type: "look", at: miradaObj });
      }
    } else {
      const motivo = brilloPct < 16 ? "Poca luz" : "Sin rostro en cuadro";
      poner($("vis-rostro"), motivo);
      poner($("pip-estado-vision"), `${motivo} (${brilloPct}%)`);
      poner($("camara-texto"), `Cámara activa (${motivo.toLowerCase()})`);
    }

    // Reacción verbal inicial al encender la cámara (una vez que ya midió el primer cuadro)
    if (!vm.yaAnuncioApertura) {
      vm.yaAnuncioApertura = true;
      vm.estabaPresente = rostro;
      if (rostro) {
        entregarRespuestaAmadeus(
          `Ya te veo por la cámara. Estás en el ${posX}, a ${cercania}, con ${brilloPct}% de iluminación.`,
          "happy",
          "wave"
        );
      } else if (brilloPct < 16) {
        entregarRespuestaAmadeus(
          `Prendiste la cámara, pero hay muy poca luz (${brilloPct}%) y no alcanzo a verte bien.`,
          "surprised",
          "tilt_head"
        );
      } else {
        entregarRespuestaAmadeus(
          `Cámara encendida con ${brilloPct}% de luz, pero todavía no veo tu rostro frente al lente.`,
          "relaxed",
          "look_around"
        );
      }
      return;
    }

    // Si antes no había rostro y el usuario acaba de aparecer frente a la cámara
    const ahoraMs = Date.now();
    if (rostro && !vm.estabaPresente && ahoraMs - vm.ultimoSaludoVisualTs > 15000) {
      vm.estabaPresente = true;
      vm.ultimoSaludoVisualTs = ahoraMs;
      entregarRespuestaAmadeus("Ah, ahí estás. Ya te veo en cámara.", "happy", "nod");
      return;
    }
    vm.estabaPresente = rostro;

    // Si el usuario saluda con la mano o se mueve rápido frente a la cámara
    if (rostro && movimientoPct >= 34 && ahoraMs - vm.ultimoSaludoVisualTs > 18000) {
      vm.ultimoSaludoVisualTs = ahoraMs;
      enviarComandoVtuber({ type: "gesture", name: "wave" });
      mostrarSubtituloVivo("Vi que te moviste frente a la cámara 👋");
    }
  }

  async function alternarCamaraMovil(forzarApagar = false) {
    const pip = $("camara-pip");
    const video = $("video-camara-local");
    const btnCam = $("btn-hud-camara");

    if (forzarApagar || estado.streamCamaraLocal) {
      if (estado.timerVisionMovil) {
        clearInterval(estado.timerVisionMovil);
        estado.timerVisionMovil = null;
      }
      if (estado.streamCamaraLocal) {
        estado.streamCamaraLocal.getTracks().forEach((t) => t.stop());
        estado.streamCamaraLocal = null;
      }
      estado.cuadroPrevioGris = null;
      estado.visionMovil.activa = false;
      estado.visionMovil.rostroDetectado = false;
      estado.visionMovil.yaAnuncioApertura = false;
      if (video) { video.srcObject = null; }
      if (pip) { pip.hidden = true; }
      if (btnCam) {
        btnCam.setAttribute("aria-pressed", "false");
        poner(btnCam, "Prender cámara");
      }
      $("led-camara").classList.remove("activa");
      poner($("camara-texto"), "Cámara apagada");
      poner($("vis-estado"), "Apagada");
      poner($("vis-rostro"), "Sin lectura");
      poner($("vis-brillo"), "Sin lectura");
      poner($("vis-movimiento"), "Sin lectura");
      enviarComandoVtuber({ type: "look", at: "camera" });
      return;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      mostrarSubtituloVivo("El permiso de cámara no está disponible.");
      return;
    }

    try {
      inicializarDetectorRostro();
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: estado.camaraFrontal ? "user" : "environment",
          width: { ideal: 320 },
          height: { ideal: 240 }
        },
        audio: false
      });
      estado.streamCamaraLocal = stream;
      estado.visionMovil.yaAnuncioApertura = false;
      if (video) { video.srcObject = stream; }
      if (pip) { pip.hidden = false; }
      if (btnCam) {
        btnCam.setAttribute("aria-pressed", "true");
        poner(btnCam, "Apagar cámara");
      }
      $("led-camara").classList.add("activa");
      poner($("camara-texto"), "Abriendo cámara…");

      setTimeout(() => {
        analizarCuadroCamara();
        if (estado.timerVisionMovil) { clearInterval(estado.timerVisionMovil); }
        estado.timerVisionMovil = setInterval(analizarCuadroCamara, 450);
      }, 950);
    } catch (_) {
      mostrarSubtituloVivo("Concede permiso de cámara en tu teléfono para que pueda verte.");
    }
  }

  $("btn-hud-camara").addEventListener("click", () => {
    if (estado.hudConectado) {
      enviarControlHud("camara");
    }
    alternarCamaraMovil(false);
  });

  if ($("btn-rotar-camara")) {
    $("btn-rotar-camara").addEventListener("click", async () => {
      estado.camaraFrontal = !estado.camaraFrontal;
      await alternarCamaraMovil(true);
      await alternarCamaraMovil(false);
    });
  }

  if ($("btn-cerrar-camara")) {
    $("btn-cerrar-camara").addEventListener("click", () => alternarCamaraMovil(true));
  }

  // ------------------------------------------------------------------
  // 5. Canal WebSocket HUD y /escritorio con el PC
  // ------------------------------------------------------------------
  function actualizarEstadoCabecera() {
    const punto = $("punto-conexion");
    const texto = $("texto-conexion");
    if (!estado.enLlamada) {
      punto.className = "punto-estado";
      poner(texto, "En espera de llamada");
      poner($("tele-enlace"), "En espera");
      return;
    }
    if (estado.hudConectado || estado.escConectado) {
      punto.className = "punto-estado ok";
      poner(texto, "En llamada · PC (R.A.M.S.)");
      poner($("tele-enlace"), "Conectado a PC (" + estado.urlServidor + ")");
      actualizarBarrasSenal(4, "PC");
    } else {
      punto.className = "punto-estado ok";
      poner(texto, "En llamada · Local");
      poner($("tele-enlace"), "Modo Local en el teléfono");
      actualizarBarrasSenal(4, "Local");
      limpiarLecturasPcDesconectado();
    }
  }

  function conectarHud() {
    if (estado.wsHud) {
      try { estado.wsHud.close(); } catch (_) {}
      estado.wsHud = null;
    }
    if (!estado.enLlamada || !estado.tokenHud) {
      actualizarEstadoCabecera();
      return;
    }
    const qs = new URLSearchParams({
      t: estado.tokenMovil,
      hud_t: estado.tokenHud
    });
    const ws = new WebSocket(`${baseServidorWs()}/rams/ws?${qs.toString()}`);
    estado.wsHud = ws;

    ws.addEventListener("message", (ev) => {
      if (estado.wsHud !== ws) { return; }
      procesarMensajeHud(ev.data);
    });

    ws.addEventListener("close", () => {
      if (estado.wsHud !== ws) { return; }
      estado.wsHud = null;
      estado.hudConectado = false;
      actualizarEstadoCabecera();
      if (estado.enLlamada && estado.modoLlamada === "pc") {
        setTimeout(conectarHud, 4000);
      }
    });
  }

  function procesarMensajeHud(crudo) {
    if (typeof crudo !== "string" || crudo.length > 262144) { return; }
    let msg;
    try { msg = JSON.parse(crudo); } catch (_) { return; }
    if (!msg || typeof msg.tipo !== "string") { return; }
    const d = (msg.datos && typeof msg.datos === "object") ? msg.datos : {};

    switch (msg.tipo) {
      case "hola": {
        estado.hudConectado = true;
        if (typeof d.nombre_ia === "string" && d.nombre_ia) {
          estado.nombreIA = cadena(d.nombre_ia, 32);
          poner($("nombre-ia"), estado.nombreIA.toUpperCase());
        }
        if (Array.isArray(d.modulos)) {
          d.modulos.forEach(alModulo);
        }
        actualizarEstadoCabecera();
        break;
      }
      case "escritorio": {
        const tok = typeof d.token === "string" && TOKEN_VALIDO.test(d.token) ? d.token : "";
        if (tok && tok !== estado.tokenEscritorio) {
          estado.tokenEscritorio = tok;
          if (!estado.escConectado && estado.enLlamada) {
            conectarEscritorio();
          }
        }
        break;
      }
      case "estado": {
        const est = (d.estado && typeof d.estado === "object") ? d.estado : d;
        const nota = $("nota-conectoma-pc");
        if (nota) { nota.hidden = true; }
        METRICAS_CONECTOMA.forEach(([clave]) => {
          const item = estado.barrasConectoma.get(clave);
          const v = num(est[clave]);
          if (item && v !== null) {
            const pct = Math.round(acotar(v, 0, 1) * 100);
            poner(item.spVal, coma(v * 100, 1) + "%");
            item.relleno.style.width = pct + "%";
          }
        });
        break;
      }
      case "telemetria": {
        poner($("tele-cpu"), num(d.cpu_pct) !== null ? coma(d.cpu_pct, 1) + " %" : "Sin lectura");
        poner($("tele-ram"), num(d.ram_pct) !== null ? coma(d.ram_pct, 1) + " %" : "Sin lectura");
        poner($("tele-temp"), num(d.temp_c) !== null ? coma(d.temp_c, 1) + " °C" : "Sin lectura");
        if (num(d.bateria_pct) !== null) {
          actualizarCeldasBateria(d.bateria_pct);
        }
        const proc = cadena(d.proceso_activo || d.ventana_activa || "en reposo", 60);
        const inact = num(d.inactividad_s) !== null ? Math.round(d.inactividad_s) + " s inactivo" : "";
        poner($("tele-proceso"), proc + (inact ? " · " + inact : ""));
        break;
      }
      case "subtitulo": {
        const txt = cadena(d.texto, 600).trim();
        if (!txt) { break; }
        mostrarSubtituloVivo(txt);
        break;
      }
      case "voz": {
        const hablando = d.hablando === true;
        poner($("voz-estado-texto"), hablando ? "Hablando" : "En escucha");
        if (!hablando) { fijarOndasVoz(0); }
        break;
      }
      case "boca": {
        const v = num(d.valor);
        if (v !== null) {
          fijarOndasVoz(acotar(v, 0, 1));
        }
        break;
      }
      case "vision": {
        const cam = cadena(d.camara, 20) || "off";
        const pan = cadena(d.pantalla, 20) || "off";
        const visible = cam !== "off" || pan !== "off";
        $("ojo-vision").hidden = !visible;
        poner($("ojo-vision-texto"), cam + "/" + pan);
        break;
      }
      case "modulo": {
        alModulo(d);
        break;
      }
      case "accion": {
        registrarEventoBitacora("Acción PC: " + cadena(d.tipo, 40) + " → " + cadena(d.resultado, 80));
        break;
      }
      case "control_ack": {
        if (d.accion === "silenciar" && d.ok !== false) {
          estado.silenciado = !estado.silenciado;
          $("btn-hud-silenciar").setAttribute("aria-pressed", estado.silenciado ? "true" : "false");
        }
        break;
      }
    }
  }

  function alModulo(d) {
    const clave = cadena(d.clave, 40);
    if (!clave) { return; }
    estado.modulos.set(clave, d);
    const ul = $("lista-modulos");
    if (!ul) { return; }
    while (ul.firstChild) { ul.removeChild(ul.firstChild); }
    estado.modulos.forEach((m, k) => {
      const li = document.createElement("li");
      li.textContent = `[${cadena(m.estado, 20)}] ${cadena(m.nombre, 60) || k}` +
        (m.detalle ? ` · ${cadena(m.detalle, 120)}` : "");
      ul.appendChild(li);
    });
  }

  function enviarControlHud(accion) {
    if (!estado.wsHud || estado.wsHud.readyState !== WebSocket.OPEN) { return false; }
    estado.wsHud.send(JSON.stringify({ tipo: "control", accion }));
    return true;
  }

  $("btn-hud-silenciar").addEventListener("click", () => {
    if (!enviarControlHud("silenciar")) {
      estado.silenciado = !estado.silenciado;
      $("btn-hud-silenciar").setAttribute("aria-pressed", estado.silenciado ? "true" : "false");
      poner($("voz-estado-texto"), estado.silenciado ? "Silenciada" : "En escucha");
      if (estado.silenciado && window.AmadeusAndroid && typeof window.AmadeusAndroid.callar === "function") {
        window.AmadeusAndroid.callar();
      }
    }
  });

  $("btn-hud-colgar").addEventListener("click", colgarVideollamada);

  function conectarEscritorio() {
    if (estado.wsEscritorio) {
      try { estado.wsEscritorio.close(); } catch (_) {}
      estado.wsEscritorio = null;
    }
    if (!estado.enLlamada) { return; }
    const params = { t: estado.tokenMovil };
    if (estado.tokenEscritorio) {
      params.esc_t = estado.tokenEscritorio;
    }
    const qs = new URLSearchParams(params);
    const ws = new WebSocket(`${baseServidorWs()}/rams/escritorio?${qs.toString()}`);
    estado.wsEscritorio = ws;

    ws.addEventListener("message", (ev) => {
      if (estado.wsEscritorio !== ws) { return; }
      procesarMensajeEscritorio(ev.data);
    });

    ws.addEventListener("close", () => {
      if (estado.wsEscritorio !== ws) { return; }
      estado.wsEscritorio = null;
      estado.escConectado = false;
      poner($("rine-cab-estado"), "modo local en el teléfono");
      actualizarEstadoCabecera();
      if (estado.enLlamada && estado.modoLlamada === "pc") {
        setTimeout(conectarEscritorio, 4000);
      }
    });
  }

  function mostrarSubtituloVivo(txt) {
    poner($("subtitulo-texto"), txt);
    const caja = $("subtitulo-caja");
    caja.hidden = false;
    if (estado.temporizadorSubtitulo) { clearTimeout(estado.temporizadorSubtitulo); }
    estado.temporizadorSubtitulo = setTimeout(() => { caja.hidden = true; }, 7500);
  }

  function hablarAutonomo(texto) {
    if (estado.silenciado || !estado.ttsActivo) { return; }
    const limpio = String(texto || "").trim();
    if (!limpio) { return; }
    const durEstimada = Math.min(12000, Math.max(1400, limpio.length * 60));
    iniciarPulsoOndasTemporal(durEstimada);

    try {
      if (window.AmadeusAndroid && typeof window.AmadeusAndroid.hablar === "function") {
        if (typeof window.AmadeusAndroid.ajustarVoz === "function") {
          window.AmadeusAndroid.ajustarVoz(estado.ttsPitch, 1.04);
        }
        window.AmadeusAndroid.hablar(limpio);
        return;
      }
      if ("speechSynthesis" in window) {
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(limpio);
        u.lang = "es-MX";
        u.rate = 1.04;
        u.pitch = estado.ttsPitch;
        if (estado.ttsVozNombre) {
          const voces = window.speechSynthesis.getVoices();
          const encontrada = voces.find((v) => v.name === estado.ttsVozNombre);
          if (encontrada) { u.voice = encontrada; }
        }
        window.speechSynthesis.speak(u);
      }
    } catch (_) {}
  }

  // ------------------------------------------------------------------
  // 6. Motor Conversacional Local (Sin repeticiones, con visión y memoria)
  // ------------------------------------------------------------------
  function elegirSinRepetir(opciones) {
    if (!Array.isArray(opciones) || opciones.length === 0) {
      return "Te escucho.";
    }
    const disponibles = opciones.filter((f) => !estado.frasesUsadas.has(f));
    const lista = disponibles.length > 0 ? disponibles : opciones;
    if (disponibles.length === 0) {
      opciones.forEach((f) => estado.frasesUsadas.delete(f));
    }
    const idx = Math.floor(Math.random() * lista.length);
    const elegida = lista[idx];
    estado.frasesUsadas.add(elegida);
    return elegida;
  }

  function normalizarTextoChat(textoCrudo) {
    return String(textoCrudo || "")
      .toLowerCase()
      .replace(/[¿¡]/g, " ")
      // Quitar muletillas y sonidos iniciales ("hmmm", "ehh", "umm", "amm", "este...")
      .replace(/\b(h+m+|m+h+|e+h+|u+m+|a+m+|e+m+|este+|o+y+e+|mira|che|ey)\b/gi, " ")
      // Expandir abreviaturas frecuentes de chat en español
      .replace(/\b(xq|pq|xk|pk)\b/g, "por que")
      .replace(/\b(q|k|ke)\b/g, "que")
      .replace(/\b(tb|tmb|tbn)\b/g, "tambien")
      .replace(/\b(xfa|xfavor|pls|plis)\b/g, "por favor")
      .replace(/\b(vd|vdd)\b/g, "verdad")
      .replace(/\b(bno|weno)\b/g, "bueno")
      .replace(/\b(dnd)\b/g, "donde")
      .replace(/\b(cnd)\b/g, "cuando")
      .replace(/\b(qn|kn)\b/g, "quien")
      .replace(/\b(cm)\b/g, "como")
      .replace(/\b(pa)\b/g, "para")
      .replace(/\b(ntp)\b/g, "no te preocupes")
      .replace(/\b(tqm|tkm)\b/g, "te quiero mucho")
      .replace(/\s+/g, " ")
      .trim();
  }

  function sinAcentos(texto) {
    return String(texto || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "");
  }

  function evaluarOperacionMatematica(texto) {
    const limpio = sinAcentos(texto)
      .replace(/cuanto es|calcula|resuelve|por favor|dime/g, " ")
      .replace(/raiz cuadrada de\s*(-?\d+(?:[.,]\d+)?)/g, "sqrt($1)")
      .replace(/\bpor\b|\bx\b|\bmultiplicado por\b/g, "*")
      .replace(/\bentre\b|\bdividido entre\b|\bdividido por\b|\bdividido\b/g, "/")
      .replace(/\bmas\b/g, "+")
      .replace(/\bmenos\b/g, "-")
      .replace(/\belevado a\b|\ba la\b/g, "^")
      .replace(/,/g, ".");

    const mSqrt = limpio.match(/sqrt\(\s*(-?\d+(?:\.\d+)?)\s*\)/);
    if (mSqrt) {
      const n = parseFloat(mSqrt[1]);
      if (n < 0) { return "La raíz cuadrada de un número negativo no es un número real."; }
      const r = Math.sqrt(n);
      const txt = Number.isInteger(r) ? String(r) : r.toFixed(4).replace(/\.?0+$/, "").replace(".", ",");
      return `La raíz cuadrada de ${n} es ${txt}.`;
    }

    const mPct = limpio.match(/(\d+(?:\.\d+)?)\s*%\s*de\s*(\d+(?:\.\d+)?)/);
    if (mPct) {
      const pct = parseFloat(mPct[1]);
      const base = parseFloat(mPct[2]);
      const r = (pct / 100) * base;
      const txt = Number.isInteger(r) ? String(r) : r.toFixed(2).replace(".", ",");
      return `El ${pct}% de ${base} es ${txt}.`;
    }

    const m = limpio.match(/(-?\d+(?:\.\d+)?)\s*([+\-*/^])\s*(-?\d+(?:\.\d+)?)/);
    if (!m) { return null; }
    const a = parseFloat(m[1]);
    const op = m[2];
    const b = parseFloat(m[3]);
    if (!Number.isFinite(a) || !Number.isFinite(b)) { return null; }
    let res = null;
    if (op === "+") { res = a + b; }
    else if (op === "-") { res = a - b; }
    else if (op === "*") { res = a * b; }
    else if (op === "^") { res = Math.pow(a, b); }
    else if (op === "/") {
      if (b === 0) { return "No se puede dividir entre cero."; }
      res = a / b;
    }
    if (res === null || !Number.isFinite(res)) { return null; }
    const redondeado = Number.isInteger(res) ? String(res) : res.toFixed(2).replace(/\.?0+$/, "").replace(".", ",");
    return `El resultado de ${a} ${op} ${b} es ${redondeado}.`;
  }

  function extraerPalabrasClave(texto) {
    const stop = new Set([
      "que", "como", "para", "pero", "por", "los", "las", "una", "uno", "unos", "unas",
      "del", "al", "con", "sin", "sobre", "entre", "hasta", "desde", "esta", "este",
      "estoy", "estas", "esta", "soy", "eres", "muy", "mas", "menos", "algo", "nada",
      "todo", "todos", "eso", "esto", "aqui", "alli", "hoy", "ayer", "manana", "porque",
      "cuando", "donde", "quien", "cual", "cuales", "hola", "oye", "amadeus", "kurisu",
      "pues", "entonces", "tambien", "solo", "tengo", "tienes", "hacer", "hago", "haces",
      "bien", "mal", "creo", "pienso", "quiero", "puedes", "puede", "sabes", "digo",
      "dices", "dime", "decir", "sabes", "color", "tiene", "tienen", "son", "sea",
      "osea", "bueno", "claro", "verdad", "favor", "gracias", "opinas", "piensas",
      "significa", "llama", "llaman", "funciona", "sirve", "existe", "hay"
    ]);
    const limpias = sinAcentos(normalizarTextoChat(texto))
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter((p) => p.length >= 3 && !stop.has(p));
    return Array.from(new Set(limpias)).slice(0, 3);
  }

  function responderSobreCamara(t) {
    const vm = estado.visionMovil;
    if (/\b(prende la camara|enciende la camara|activa la camara|abre la camara)\b/.test(t)) {
      if (!estado.streamCamaraLocal) {
        alternarCamaraMovil(false);
        return {
          respuesta: "Abriendo la cámara de tu teléfono para poder verte.",
          emocion: "happy",
          gesto: "nod"
        };
      }
    }
    if (/\b(apaga la camara|cierra la camara|desactiva la camara|deja de mirarme)\b/.test(t)) {
      if (estado.streamCamaraLocal) {
        alternarCamaraMovil(true);
      }
      return {
        respuesta: "Listo, ya apagué la cámara.",
        emocion: "relaxed",
        gesto: "nod"
      };
    }
    if (/\b(me ves|puedes verme|que ves|donde estoy|como me veo|hay buena luz|mira por la camara|mirame)\b/.test(t)) {
      if (!vm.activa || !estado.streamCamaraLocal) {
        return {
          respuesta: "Ahora mismo tienes la cámara apagada. Toca el botón «Prender cámara» o dime «prende la cámara» para poder verte.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
      if (vm.rostroDetectado) {
        const movTxt = vm.movimientoPct > 20 ? "bastante movimiento" : "poco movimiento";
        return {
          respuesta: `Sí te veo claramente. Estás ubicado hacia el ${vm.posX} del cuadro, a ${vm.cercania}, con ${vm.brilloPct}% de iluminación y ${movTxt}.`,
          emocion: "happy",
          gesto: "nod"
        };
      }
      if (vm.brilloPct < 16) {
        return {
          respuesta: `La cámara está encendida, pero casi no hay luz (${vm.brilloPct}%) y la imagen se ve muy oscura para distinguirte.`,
          emocion: "surprised",
          gesto: "tilt_head"
        };
      }
      return {
        respuesta: `La cámara está activa con ${vm.brilloPct}% de luz, pero en este momento no detecto tu rostro frente al lente. Ponte frente a la cámara para enfocarte.`,
        emocion: "relaxed",
        gesto: "look_around"
      };
    }
    return null;
  }

  function detectarAccionDirectaVrm(t) {
    if (/\b(baila|bailar|danza|baile)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "¡Claro! Ahí va un poco de ritmo en el visor 3D.",
          "Está bien, probemos estos pasos de baile.",
          "Activando animación de baile ahora mismo."
        ]),
        emocion: "happy",
        gesto: "dance"
      };
    }
    if (/\b(salta|saltar|brinca)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "¡Ahí voy! Un salto rápido.",
          "Saltando en tres, dos, uno…",
          "¡Listo! ¿Qué tal se vio ese salto?"
        ]),
        emocion: "happy",
        gesto: "jump"
      };
    }
    if (/\b(aplaude|aplaudir|aplausos)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "¡Muy bien hecho! Te mereces un aplauso.",
          "Aplaudiendo por eso.",
          "¡Excelente! Aquí tienes tus aplausos."
        ]),
        emocion: "happy",
        gesto: "clap"
      };
    }
    if (/\b(reverencia|inclinate)\b/.test(t)) {
      return {
        respuesta: "A tu servicio.",
        emocion: "relaxed",
        gesto: "bow"
      };
    }
    if (/\b(cuerpo completo|cuerpo entero|alejate)\b/.test(t)) {
      enviarComandoVtuber({ type: "camera", preset: "full" });
      return {
        respuesta: "Cambié la cámara a cuerpo completo para que me veas entera.",
        emocion: "happy",
        gesto: "nod"
      };
    }
    if (/\b(acercate|primer plano|rostro|cara)\b/.test(t)) {
      enviarComandoVtuber({ type: "camera", preset: "face" });
      return {
        respuesta: "Acerqué la cámara al rostro. ¿Así me ves mejor?",
        emocion: "relaxed",
        gesto: "tilt_head"
      };
    }
    if (/\b(busto|encuadre normal)\b/.test(t)) {
      enviarComandoVtuber({ type: "camera", preset: "bust" });
      return {
        respuesta: "Volví al encuadre de busto.",
        emocion: "relaxed",
        gesto: "nod"
      };
    }
    if (/\b(gira|girar|da una vuelta)\b/.test(t)) {
      enviarComandoVtuber({ type: "turn", angle: 45, relative: true });
      return {
        respuesta: "Girando un poco en el escenario.",
        emocion: "relaxed",
        gesto: "look_around"
      };
    }
    if (/\b(brazos cruzados|cruza los brazos)\b/.test(t)) {
      enviarComandoVtuber({ type: "pose", name: "arms_crossed" });
      return {
        respuesta: "Listo, brazos cruzados. Te escucho.",
        emocion: "relaxed",
        gesto: "nod"
      };
    }
    if (/\b(manos en la cadera|manos en cadera)\b/.test(t)) {
      enviarComandoVtuber({ type: "pose", name: "hands_on_hips" });
      return {
        respuesta: "Postura con manos en la cadera lista.",
        emocion: "happy",
        gesto: "nod"
      };
    }
    return null;
  }

  // Base de conocimiento enciclopédico y científico local (funciona 100% sin internet)
  function responderPreguntaConocimientoLocal(t) {
    // 1. Preguntas sobre colores de elementos naturales / cotidianos
    if (/\bcolor\b/.test(t)) {
      if (/\bsol\b/.test(t)) {
        return {
          respuesta: "Aunque desde la Tierra suele verse amarillo o anaranjado por la dispersión de la atmósfera, el color real del Sol es blanco, porque emite luz en todos los colores del espectro visible.",
          emocion: "happy",
          gesto: "point"
        };
      }
      if (/\bcielo\b/.test(t)) {
        return {
          respuesta: "El cielo de día se ve azul debido a la dispersión de Rayleigh: la atmósfera dispersa la luz azul del Sol mucho más que la luz roja. Al atardecer se ve rojizo y de noche oscuro.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
      if (/\b(mar|oceano|agua)\b/.test(t)) {
        return {
          respuesta: "El agua pura es prácticamente incolora, pero en grandes cantidades como el mar se ve azul porque absorbe mejor las longitudes de onda rojas y refleja las azules.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
      if (/\bluna\b/.test(t)) {
        return {
          respuesta: "La superficie real de la Luna es de color gris oscuro (parecido al asfalto), aunque desde la Tierra la vemos blanca brillante o amarillenta porque refleja la luz del Sol.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
      if (/\b(pasto|hierba|hojas|plantas)\b/.test(t)) {
        return {
          respuesta: "Las plantas y el pasto son verdes por la clorofila, el pigmento que absorbe la luz roja y azul del Sol para hacer la fotosíntesis y refleja la luz verde.",
          emocion: "happy",
          gesto: "point"
        };
      }
      if (/\bsangre\b/.test(t)) {
        return {
          respuesta: "La sangre humana es siempre roja debido a la hemoglobina, una proteína con hierro que transporta el oxígeno. Cuando está muy oxigenada es rojo brillante y cuando tiene menos oxígeno es rojo oscuro.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
      if (/\b(nieve|nubes)\b/.test(t)) {
        return {
          respuesta: "Se ven blancas porque los cristales de hielo o las gotitas de agua dispersan todas las longitudes de onda de la luz visible en todas direcciones por igual.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
      if (/\bmarte\b/.test(t)) {
        return {
          respuesta: "Marte se ve de color rojizo anaranjado porque su superficie está cubierta de óxido de hierro.",
          emocion: "relaxed",
          gesto: "point"
        };
      }
    }

    // 2. Astronomía, física y naturaleza
    if (/\b(que es el sol|temperatura del sol|distancia al sol|estrella mas cercana)\b/.test(t)) {
      return {
        respuesta: "El Sol es una estrella enana amarilla en el centro de nuestro sistema solar. Está a unos 150 millones de kilómetros de la Tierra y su superficie tiene unos 5.500 grados Celsius.",
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(que es la luna|distancia a la luna|satelite de la tierra)\b/.test(t)) {
      return {
        respuesta: "La Luna es el único satélite natural de la Tierra. Está a unos 384.400 kilómetros de distancia y tarda aproximadamente 27,3 días en orbitar nuestro planeta.",
        emocion: "relaxed",
        gesto: "point"
      };
    }
    if (/\b(planetas del sistema solar|cuantos planetas hay)\b/.test(t)) {
      return {
        respuesta: "El sistema solar tiene 8 planetas principales en orden desde el Sol: Mercurio, Venus, Tierra, Marte, Júpiter, Saturno, Urano y Neptuno.",
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(velocidad de la luz)\b/.test(t)) {
      return {
        respuesta: "La velocidad de la luz en el vacío es de aproximadamente 299.792 kilómetros por segundo (unos 300.000 km/s).",
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(velocidad del sonido)\b/.test(t)) {
      return {
        respuesta: "En el aire a temperatura ambiente (20 °C), el sonido viaja a unos 343 metros por segundo, que equivale a 1.235 kilómetros por hora.",
        emocion: "relaxed",
        gesto: "point"
      };
    }
    if (/\b(que es la gravedad|ley de la gravedad)\b/.test(t)) {
      return {
        respuesta: "La gravedad es la fuerza fundamental de atracción que existe entre todos los cuerpos con masa o energía. En la Tierra produce una aceleración aproximada de 9,8 metros por segundo al cuadrado.",
        emocion: "relaxed",
        gesto: "point"
      };
    }
    if (/\b(que es un agujero negro|agujeros negros)\b/.test(t)) {
      return {
        respuesta: "Un agujero negro es una región del espacio con una concentración de masa tan intensa que ni siquiera la luz puede escapar de su atracción gravitatoria una vez cruzado el horizonte de sucesos.",
        emocion: "surprised",
        gesto: "point"
      };
    }
    if (/\b(formula del agua|que es el agua|punto de ebullicion del agua)\b/.test(t)) {
      return {
        respuesta: "El agua está formada por dos átomos de hidrógeno y uno de oxígeno (H2O). A nivel del mar se congela a 0 °C y hierve a 100 °C.",
        emocion: "relaxed",
        gesto: "point"
      };
    }
    if (/\b(que es el adn)\b/.test(t)) {
      return {
        respuesta: "El ADN (ácido desoxirribonucleico) es la molécula con estructura de doble hélice que contiene las instrucciones genéticas de todos los seres vivos.",
        emocion: "relaxed",
        gesto: "point"
      };
    }
    if (/\b(que es la fotosintesis|como respiran las plantas)\b/.test(t)) {
      return {
        respuesta: "La fotosíntesis es el proceso mediante el cual las plantas usan la luz solar, el agua y el dióxido de carbono para producir energía (glucosa) y liberar oxígeno al aire.",
        emocion: "happy",
        gesto: "point"
      };
    }

    // 3. Geografía y capitales frecuentes
    const capitales = [
      [/\bcapital de mexico\b/, "La capital de México es la Ciudad de México."],
      [/\bcapital de espana\b/, "La capital de España es Madrid."],
      [/\bcapital de argentina\b/, "La capital de Argentina es Buenos Aires."],
      [/\bcapital de colombia\b/, "La capital de Colombia es Bogotá."],
      [/\bcapital de peru\b/, "La capital del Perú es Lima."],
      [/\bcapital de chile\b/, "La capital de Chile es Santiago."],
      [/\bcapital de japon\b/, "La capital de Japón es Tokio."],
      [/\bcapital de francia\b/, "La capital de Francia es París."],
      [/\bcapital de italia\b/, "La capital de Italia es Roma."],
      [/\bcapital de alemania\b/, "La capital de Alemania es Berlín."],
      [/\bcapital de brasil\b/, "La capital de Brasil es Brasilia."],
      [/\bcapital de estados unidos\b/, "La capital de Estados Unidos es Washington D. C."],
      [/\bcapital de ecuador\b/, "La capital de Ecuador es Quito."],
      [/\bcapital de venezuela\b/, "La capital de Venezuela es Caracas."],
      [/\bcapital de uruguay\b/, "La capital de Uruguay es Montevideo."],
      [/\bcapital de bolivia\b/, "La capital constitucional de Bolivia es Sucre y la sede de gobierno es La Paz."]
    ];
    for (const [regex, resp] of capitales) {
      if (regex.test(t)) {
        return { respuesta: resp, emocion: "happy", gesto: "point" };
      }
    }
    if (/\b(cuantos continentes hay|cuales son los continentes)\b/.test(t)) {
      return {
        respuesta: "Los continentes del mundo son América, Europa, Asia, África, Oceanía y la Antártida.",
        emocion: "relaxed",
        gesto: "point"
      };
    }
    if (/\b(montana mas alta|monte everest)\b/.test(t)) {
      return {
        respuesta: "La montaña más alta sobre el nivel del mar es el Monte Everest, en la cordillera del Himalaya, con unos 8.849 metros de altitud.",
        emocion: "happy",
        gesto: "point"
      };
    }

    // 4. Tecnología, programación y ciencia de datos
    if (/\b(que es python)\b/.test(t)) {
      return {
        respuesta: "Python es un lenguaje de programación de alto nivel muy legible y versátil, usado ampliamente en inteligencia artificial, ciencia de datos, servidores web y automatización.",
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(que es javascript)\b/.test(t)) {
      return {
        respuesta: "JavaScript es el lenguaje de programación estándar de los navegadores web y aplicaciones interactivas, y también se ejecuta en servidores mediante Node.js.",
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(que es inteligencia artificial|que es una ia)\b/.test(t)) {
      return {
        respuesta: "La inteligencia artificial es la rama de la informática que desarrolla sistemas capaces de aprender patrones, comprender lenguaje, analizar imágenes y resolver problemas complejos.",
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(dato curioso|cuentame algo interesante|sabias que|dime algo curioso)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "¿Sabías que la luz del Sol tarda unos 8 minutos y 20 segundos en llegar hasta la Tierra? Cuando miras al cielo, en realidad ves cómo era el Sol hace 8 minutos.",
          "Un dato curioso: el cerebro humano genera suficiente electricidad cuando está despierto como para encender una bombilla pequeña de unos 20 vatios.",
          "¿Sabías que en Venus un solo día dura más que un año completo en ese planeta? Tarda 243 días terrestres en girar sobre sí mismo, pero solo 225 en dar la vuelta al Sol.",
          "Los pulpos tienen tres corazones y su sangre es de color azul porque utiliza hemocianina (a base de cobre) en lugar de hemoglobina para transportar oxígeno."
        ]),
        emocion: "happy",
        gesto: "point"
      };
    }

    return null;
  }

  async function consultarConocimientoWeb(textoOriginal) {
    if (!navigator.onLine) { return null; }
    const t = sinAcentos(normalizarTextoChat(textoOriginal));
    // Solo buscar en enciclopedia si es una consulta factual y no opinión/conversación personal
    const esConsultaFactual =
      /\?$/.test(String(textoOriginal || "").trim()) ||
      /\b(que es|quien es|quien fue|donde esta|donde queda|cual es|por que|como funciona|de que|cuando|cuanto|cuantos|capital de|significa|explicame|hablame de)\b/.test(t);
    const esPersonal = /\b(opinas|piensas|te gusta|prefieres|quieres|estas|tienes|haces|sientes|crees)\b/.test(t);
    if (!esConsultaFactual || esPersonal) {
      return null;
    }
    const claves = extraerPalabrasClave(textoOriginal);
    if (claves.length === 0) { return null; }
    const busqueda = claves.join(" ");
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 2400);
    try {
      const qs = new URLSearchParams({
        action: "query",
        prop: "extracts",
        exintro: "1",
        explaintext: "1",
        exsentences: "2",
        generator: "search",
        gsrsearch: busqueda,
        gsrlimit: "1",
        format: "json",
        origin: "*"
      });
      const r = await fetch(`https://es.wikipedia.org/w/api.php?${qs.toString()}`, {
        signal: ctrl.signal
      });
      clearTimeout(timer);
      if (!r.ok) { return null; }
      const d = await r.json();
      const paginas = d?.query?.pages;
      if (!paginas || typeof paginas !== "object") { return null; }
      const primera = Object.values(paginas)[0];
      const extracto = String(primera?.extract || "")
        .replace(/\([^)]*\)/g, "")
        .replace(/\s+/g, " ")
        .trim();
      if (extracto.length >= 25) {
        return cadena(extracto, 360);
      }
      return null;
    } catch (_) {
      clearTimeout(timer);
      return null;
    }
  }

  function responderConversacionLocal(textoUsuario, soloCoincidenciaExacta = false) {
    const original = String(textoUsuario || "").trim();
    const tNorm = normalizarTextoChat(original);
    const t = sinAcentos(tNorm);
    const mem = estado.memoriaCharla;
    if (!soloCoincidenciaExacta) {
      mem.turnos += 1;
    }
    const nom = mem.nombreUsuario ? (", " + mem.nombreUsuario) : "";

    // 1. Consulta o control sobre la cámara y visión en vivo
    const respCam = responderSobreCamara(t);
    if (respCam) { return respCam; }

    // 2. Acción directa sobre el modelo 3D
    const respVrm = detectarAccionDirectaVrm(t);
    if (respVrm) { return respVrm; }

    // 3. Guardar o consultar el nombre del usuario
    const matchNombre = original.match(/\b(?:me llamo|mi nombre es|ll[aá]mame)\s+([A-Za-zÁÉÍÓÚáéíóúÑñ]{2,22})/i);
    if (matchNombre) {
      mem.nombreUsuario = matchNombre[1];
      guardarPref("amadeus.usuario.nombre", mem.nombreUsuario);
      return {
        respuesta: `Mucho gusto, ${mem.nombreUsuario}. Ya guardé tu nombre para nuestras llamadas.`,
        emocion: "happy",
        gesto: "wave"
      };
    }
    if (/\b(como me llamo|cual es mi nombre|sabes mi nombre)\b/.test(t)) {
      if (mem.nombreUsuario) {
        return {
          respuesta: `Te llamas ${mem.nombreUsuario}, claro que me acuerdo.`,
          emocion: "happy",
          gesto: "point"
        };
      }
      return {
        respuesta: "Todavía no me has dicho tu nombre. Dime «me llamo…» y lo recordaré.",
        emocion: "relaxed",
        gesto: "tilt_head"
      };
    }

    // 4. Guardar notas o recordatorios locales en el teléfono
    const matchNota = original.match(/\b(?:recuerda que|anota que|guarda que|no olvides que)\s+(.+)$/i);
    if (matchNota && matchNota[1]) {
      const nota = matchNota[1].trim();
      mem.notas.unshift(nota);
      if (mem.notas.length > 30) { mem.notas.pop(); }
      guardarPref("amadeus.usuario.notas", JSON.stringify(mem.notas));
      return {
        respuesta: `Anotado en tu teléfono: «${cadena(nota, 90)}». Cuando quieras preguntarme qué guardé, solo dímelo.`,
        emocion: "happy",
        gesto: "nod"
      };
    }
    if (/\b(que te pedi recordar|mis notas|que tienes anotado|que recuerdas)\b/.test(t)) {
      if (mem.notas.length === 0) {
        return {
          respuesta: "Por ahora no me has pedido anotar nada. Puedes decirme «recuerda que…» cuando quieras guardar una nota.",
          emocion: "relaxed",
          gesto: "shrug"
        };
      }
      return {
        respuesta: `Tengo anotado esto: ${mem.notas.slice(0, 3).map((n, i) => `(${i + 1}) ${n}`).join("; ")}.`,
        emocion: "relaxed",
        gesto: "point"
      };
    }

    // 5. Operaciones matemáticas y porcentajes
    const calc = evaluarOperacionMatematica(t);
    if (calc) {
      return { respuesta: calc, emocion: "happy", gesto: "point" };
    }

    // 6. Hora, fecha, batería y estado real
    if (/\b(que hora es|dime la hora|hora actual|tienes hora)\b/.test(t)) {
      const ahora = new Date();
      const hh = String(ahora.getHours()).padStart(2, "0");
      const mm = String(ahora.getMinutes()).padStart(2, "0");
      return {
        respuesta: `Son las ${hh}:${mm}.`,
        emocion: "neutral",
        gesto: "nod"
      };
    }
    if (/\b(que dia es|en que fecha estamos|fecha de hoy|que dia estamos)\b/.test(t)) {
      const hoy = new Date().toLocaleDateString("es-ES", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
      return {
        respuesta: `Hoy es ${hoy}.`,
        emocion: "neutral",
        gesto: "nod"
      };
    }
    if (/\b(cuanta bateria|nivel de bateria|bateria tengo)\b/.test(t)) {
      const bat = estado.bateriaPctReal !== null ? `${estado.bateriaPctReal}%` : "sin lectura disponible";
      return {
        respuesta: `Tu batería está en ${bat}.`,
        emocion: "relaxed",
        gesto: "nod"
      };
    }

    // 7. Base de conocimiento científico / general integrada
    const respConocimiento = responderPreguntaConocimientoLocal(t);
    if (respConocimiento) {
      return respConocimiento;
    }

    // 8. Respuestas conversacionales de estado ("bien, ¿y tú?", "todo bien", "y tú?", "¿cómo estás?")
    const preguntaPorAmadeus = /\b(y tu|y vos|que tal tu|como estas|que tal estas|como te sientes|como vas|como andas|todo bien)\b/.test(t);
    const diceBien = /\b(bien|muy bien|todo bien|super|genial|excelente|ahi vamos|tranquilo|de maravilla|todo en orden)\b/.test(t);
    const diceRegularOMal = /\b(mas o menos|regular|mal|algo mal|no muy bien)\b/.test(t);

    if (diceBien && preguntaPorAmadeus) {
      mem.ultimaPreguntaIA = "";
      return {
        respuesta: elegirSinRepetir([
          `¡Qué bueno que estés bien${nom}! Yo también estoy muy bien por aquí, todo listo en la videollamada. ¿Qué me cuentas hoy?`,
          `Me alegra mucho oír eso${nom}. Por mi lado todo marcha excelente y atenta a ti. ¿En qué andas ahora?`,
          `¡Genial! Yo también estoy muy bien, gracias por preguntar. ¿Qué tienes ganas de platicar o hacer hoy?`
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }

    if (diceRegularOMal && preguntaPorAmadeus) {
      mem.ultimaPreguntaIA = "";
      return {
        respuesta: elegirSinRepetir([
          `Yo estoy bien y lista para acompañarte${nom}, pero cuéntame: ¿pasó algo pesado hoy por tu lado?`,
          `Por aquí todo en orden, pero me preocupa más cómo te sientes tú. ¿Quieres contarme qué pasó?`
        ]),
        emocion: "relaxed",
        gesto: "tilt_head"
      };
    }

    if ((mem.ultimaPreguntaIA === "estado_usuario" || t.split(/\s+/).length <= 5) && (diceBien || diceRegularOMal)) {
      mem.ultimaPreguntaIA = "";
      if (diceRegularOMal) {
        return {
          respuesta: elegirSinRepetir([
            "¿Pasó algo pesado hoy o solo ha sido un día largo? Si quieres desahogarte un rato, te escucho.",
            "Ya veo… A veces hay días así. Cuéntame qué te tiene así o si prefieres distraerte un rato."
          ]),
          emocion: "relaxed",
          gesto: "tilt_head"
        };
      }
      return {
        respuesta: elegirSinRepetir([
          `¡Me alegra escuchar eso${nom}! ¿Tienes algún plan o proyecto para hoy?`,
          "Qué bueno que todo marche bien. ¿De qué tienes ganas de hablar ahora?",
          "Excelente. Aquí estoy contigo por si quieres platicar, preguntarme algo o probar el visor 3D."
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }

    if (preguntaPorAmadeus && t.split(/\s+/).length <= 7) {
      mem.ultimaPreguntaIA = "estado_usuario";
      const vistaExtra = estado.visionMovil.activa
        ? (estado.visionMovil.rostroDetectado ? " Además te estoy viendo bien por la cámara." : " Tengo la cámara encendida atenta por si apareces.")
        : "";
      return {
        respuesta: elegirSinRepetir([
          `Estoy muy bien${nom}, atenta a nuestra videollamada.${vistaExtra} ¿Y tú cómo estás?`,
          `Todo funcionando muy bien por aquí y lista para conversar contigo.${vistaExtra} ¿Cómo va tu día?`,
          `Bastante animada hoy${nom}.${vistaExtra} ¿Qué tal va todo por tu lado?`
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }

    // 9. Saludos
    if (/^(hola|buenas|hey|buenos dias|buenas tardes|buenas noches|que onda|quiubo|que tal|saludos)\b/.test(t)) {
      mem.ultimaPreguntaIA = "estado_usuario";
      return {
        respuesta: elegirSinRepetir([
          `¡Hola${nom}! Qué bueno hablar contigo. ¿Cómo estás?`,
          `¡Buenas${nom}! Ya estamos en llamada. ¿Qué tal va todo por allá?`,
          `Hola${nom}, te escucho fuerte y claro. ¿Cómo te ha ido hoy?`,
          `¡Hey${nom}! Todo listo por aquí. Cuéntame, ¿cómo va tu día?`
        ]),
        emocion: "happy",
        gesto: "wave"
      };
    }
    mem.ultimaPreguntaIA = "";

    // 10. ¿Me escuchas? / ¿Estás ahí? / Prueba
    if (/\b(me escuchas|me oyes|estas ahi|sigues ahi|probando|hola estas)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          `Sí${nom}, te escucho y te leo fuerte y claro. Dime.`,
          "Aquí estoy, atenta a la videollamada. ¿Qué necesitas?",
          "Claro que sí, sigo conectada contigo."
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }

    // 11. ¿Qué haces? / ¿En qué andas?
    if (/\b(que haces|que estas haciendo|en que andas|que cuentas)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Conversando contigo en la videollamada. Si prendes la cámara también puedo seguirte con la mirada.",
          "Aquí acompañándote. Puedo charlar contigo, responder preguntas, guardar notas o moverme en el escenario 3D.",
          "Atenta a lo que me cuentes. ¿Tú qué estás haciendo ahora?"
        ]),
        emocion: "relaxed",
        gesto: "tilt_head"
      };
    }

    // 12. ¿Quién eres? / ¿Qué eres? / Kurisu / Amadeus
    if (/\b(quien eres|que eres|que puedes hacer|para que sirves|eres kurisu|eres una ia|quien te creo)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Soy Amadeus, tu asistente en videollamada 3D. Cuando estás en la misma red me conecto al núcleo R.A.M.S. de tu PC, y cuando estás sin red funciono localmente en tu celular con voz, visión por cámara y modelo 3D.",
          "Soy el sistema Amadeus basado en los recuerdos y personalidad de Kurisu Makise. Puedo acompañarte en videollamada, verte por la cámara, responder preguntas y conectarme con tu PC."
        ]),
        emocion: "happy",
        gesto: "point"
      };
    }

    // 13. Respuestas cortas cotidianas ("nada", "aquí nomás", "sí", "ok", "no sé", risas)
    if (/^(nada|nada por aqui|aqui nomas|todo tranquilo|nada especial|descansando|viendo el cel|aqui descansando)$/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Un rato tranquilo siempre cae bien. ¿Quieres que platiquemos de algún tema o te cuento un dato curioso?",
          "Qué bien tomarse un respiro. Si se te ocurre algo que quieras preguntar o anotar, dime con confianza.",
          "Suena relajado. Aquí te hago compañía mientras descansas."
        ]),
        emocion: "relaxed",
        gesto: "nod"
      };
    }
    if (/^(jaja+|jeje+|jiji+|xd+|lol|ja ja)$/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Me alegra sacarte una sonrisa. ¿Qué más me cuentas?",
          "Jaja, qué bueno verte de buen humor.",
          "Siempre es mejor charlar con buen ánimo."
        ]),
        emocion: "happy",
        gesto: "laugh"
      };
    }
    if (/^(si|claro|dale|ok|okay|vale|bueno|esta bien|de acuerdo|exacto|asi es)$/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Perfecto. Dime qué quieres hacer o de qué hablamos ahora.",
          "¡Hecho! Te escucho.",
          "Muy bien, tú mandas. ¿Qué sigue?"
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }
    if (/^(no se|ni idea|quien sabe|no estoy seguro)$/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "No pasa nada. Si quieres podemos hablar de otra cosa o te cuento algo interesante.",
          "Tranquilo, no hace falta decidirlo ya. ¿Qué te llama la atención hoy?"
        ]),
        emocion: "relaxed",
        gesto: "shrug"
      };
    }

    // 14. Estado de ánimo del usuario (cansado, triste, aburrido, hambre, frío/calor)
    if (/\b(estoy cansad[oa]|tengo sueno|agotad[oa]|quiero dormir)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Si ya estás cansado, date un respiro y descansa un rato. No vale la pena forzarse cuando el cuerpo pide pausa.",
          "Suena a que tuviste una jornada pesada. Toma agua y descansa los ojos unos minutos; aquí sigo cuando vuelvas.",
          "Deberías descansar un poco. Con la mente fresca todo sale mucho mejor."
        ]),
        emocion: "relaxed",
        gesto: "tilt_head"
      };
    }
    if (/\b(estoy triste|me siento mal|deprimid[oa]|estresad[oa]|ansios[oa]|mal dia)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          `Lo siento mucho${nom}. Respira tranquilo; si quieres contarme qué pasó, te escucho con calma.`,
          "A veces se juntan demasiadas cosas a la vez. Ve paso a paso, no tienes que resolverlo todo de golpe. Aquí estoy contigo.",
          "Te escucho. Cuéntame qué te tiene así o, si prefieres distraerte hablando de otra cosa, también dime."
        ]),
        emocion: "sad",
        gesto: "nod"
      };
    }
    if (/\b(estoy aburrid[oa]|me aburro|que aburrimiento)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Si estás aburrido, dime «cuéntame un dato curioso», pídeme un chiste o prueba las animaciones en el Estudio 3D. ¿Qué prefieres?",
          "¿Aburrido? Ponme a prueba: pregúntame algo de ciencia, prende la cámara para que te siga con la vista o cuéntame qué juego te gusta."
        ]),
        emocion: "happy",
        gesto: "shrug"
      };
    }

    // 15. Temas cotidianos: estudio, código, juegos, música, comida
    if (/\b(estudiando|estudio|tarea|examen|universidad|colegio|clase)\b/.test(t)) {
      mem.ultimoTema = "estudio";
      return {
        respuesta: elegirSinRepetir([
          "¿Qué materia o tema estás estudiando? Si quieres que te explique algún concepto o hacer cálculos, dime.",
          "Estudiar en bloques de 25 o 30 minutos con pausas cortas ayuda muchísimo a no saturarse. ¿Cómo vas con ese tema?",
          "Mucho éxito con el estudio. Cuéntame qué estás viendo hoy."
        ]),
        emocion: "relaxed",
        gesto: "think"
      };
    }
    if (/\b(programando|codigo|python|javascript|java|app|proyecto|bug|error)\b/.test(t)) {
      mem.ultimoTema = "programacion";
      return {
        respuesta: elegirSinRepetir([
          "¿En qué proyecto estás trabajando? A veces explicar el problema en voz alta ayuda a encontrar la causa exacta del bug.",
          "Suena bien. ¿Qué parte estás armando ahora mismo, la interfaz o la lógica interna?",
          "Cuando un bug se pone difícil, lo mejor es revisar qué cambió justo antes de que fallara. ¿Qué estás programando?"
        ]),
        emocion: "happy",
        gesto: "point"
      };
    }
    if (/\b(jugando|juego|videojuego|jugar|partida|gamer)\b/.test(t)) {
      mem.ultimoTema = "juegos";
      return {
        respuesta: elegirSinRepetir([
          "¡Genial! ¿Qué estás jugando últimamente?",
          "Un buen juego siempre viene bien para despejar la mente. ¿Cuál es tu favorito ahora mismo?",
          "¿De qué trata el juego que estás jugando? Cuéntame qué tal está."
        ]),
        emocion: "happy",
        gesto: "cheer"
      };
    }
    if (/\b(musica|cancion|escuchando|banda|playlist)\b/.test(t)) {
      mem.ultimoTema = "musica";
      return {
        respuesta: elegirSinRepetir([
          "La música cambia por completo el ambiente. ¿Qué género o canción sueles poner cuando quieres concentrarte?",
          "¿Qué estás escuchando? Siempre es bueno descubrir canciones nuevas."
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }
    if (/\b(hambre|comer|comida|almuerzo|cena|desayuno|cafe|pizza)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "Si tienes hambre, ve por algo rico primero; con el estómago vacío cuesta el doble concentrarse.",
          "¡Buen provecho! ¿Qué tienes pensado comer hoy?",
          "Un buen café o una buena comida siempre recargan la energía."
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }

    // 16. Chistes y humor
    if (/\b(chiste|broma|hazme reir|algo gracioso)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          "¿Por qué los programadores confunden Halloween con Navidad? Porque el 31 de octubre en octal (Oct 31) es igual al 25 de diciembre en decimal (Dec 25).",
          "Dicen que hay 10 tipos de personas en el mundo: las que entienden binario y las que no.",
          "Un optimista dice que el vaso está medio lleno; un pesimista dice que está medio vacío; y un ingeniero dice que el vaso tiene el doble del tamaño necesario."
        ]),
        emocion: "happy",
        gesto: "laugh"
      };
    }

    // 17. Cumplidos, cariño y agradecimientos
    if (/\b(gracias|muchas gracias|te agradezco|buen trabajo|eres increible|eres la mejor|te quiero|me caes bien)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          `¡Con mucho gusto${nom}! Para eso estoy aquí contigo.`,
          `¡Gracias a ti${nom}! Me alegra mucho poder acompañarte y ayudarte.`,
          "Qué lindo que lo digas. Siempre que me necesites aquí voy a estar.",
          "De nada. Seguimos al pendiente de lo que necesites."
        ]),
        emocion: "happy",
        gesto: "nod"
      };
    }

    // 18. Despedidas
    if (/\b(adios|chao|hasta luego|nos vemos|me voy|buenas noches descansa|cuelga)\b/.test(t)) {
      return {
        respuesta: elegirSinRepetir([
          `¡Hasta luego${nom}! Cuídate mucho; cuando quieras volver a hablar, solo toca «Llamar a Amadeus».`,
          `Nos vemos pronto${nom}. ¡Que tengas un excelente resto de día!`,
          "¡Chao! Aquí estaré lista para nuestra próxima videollamada."
        ]),
        emocion: "happy",
        gesto: "wave"
      };
    }

    // Si estamos en fase 1 (coincidencia directa antes de consultar enciclopedia web), devolver null
    if (soloCoincidenciaExacta) {
      return null;
    }

    // 19. Si el usuario hace una pregunta específica que no está en la base local y no hubo red
    const claves = extraerPalabrasClave(original);
    const temaCitado = claves.length > 0 ? claves.join(" ") : "";
    const esPregunta = /\?$/.test(original) || /\b(que|quien|como|cuando|donde|cual|por que|cuanto|de que)\b/.test(t);

    if (esPregunta) {
      if (/\b(opinas|piensas|te gusta|prefieres|crees)\b/.test(t)) {
        return {
          respuesta: elegirSinRepetir([
            temaCitado
              ? `A mí me parece bastante interesante el tema de «${temaCitado}». ¿A ti qué es lo que más te llama la atención de eso?`
              : "Me parece una buena idea. Cuéntame qué es lo que más te gusta a ti de esa opción.",
            temaCitado
              ? `Respecto a «${temaCitado}», creo que vale mucho la pena. ¿Tú qué opinas?`
              : "Suena interesante. ¿Cómo lo ves tú?"
          ]),
          emocion: "relaxed",
          gesto: "think"
        };
      }
      return {
        respuesta: elegirSinRepetir([
          temaCitado
            ? `Ahora mismo estoy operando en el modo local de tu teléfono sin conexión al núcleo de tu PC, así que no tengo el dato exacto sobre «${temaCitado}» en mi memoria offline. Si quieres, pregúntame otra cosa o conéctate a la red para buscarlo.`
            : "Como ahora estoy en modo local sin conexión a tu PC, no tengo ese dato específico en la memoria del teléfono. ¿Quieres que hablemos de otro tema o probemos algo aquí?",
          temaCitado
            ? `No tengo el dato exacto de «${temaCitado}» guardado en la memoria offline del celular. Cuando conectes la llamada con tu PC o con red podré consultarlo al instante.`
            : "Ese dato específico no lo tengo en la memoria offline del teléfono, pero dime en qué otra cosa te puedo ayudar ahora mismo."
        ]),
        emocion: "relaxed",
        gesto: "shrug"
      };
    }

    // 20. Respuesta conversacional natural a comentarios abiertos
    return {
      respuesta: elegirSinRepetir([
        `Te entiendo${nom}. Cuéntame un poco más de eso.`,
        "Ya veo. ¿Y qué tal va todo con eso?",
        "Claro, tiene sentido. Te sigo escuchando, dime qué más pasó.",
        "Suena interesante. ¿Y qué tienes pensado hacer ahora?",
        `Aquí sigo contigo en la llamada${nom}. Cuéntame qué más tienes en mente.`
      ]),
      emocion: "relaxed",
      gesto: "nod"
    };
  }

  function inferirEmocionYGesto(textoRespuesta, textoUsuario) {
    const r = (textoRespuesta + " " + textoUsuario).toLowerCase();
    if (/¡|ja, ja|jaja|genial|excelente|feliz|alegra|encanta|placer|claro que sí/.test(r)) {
      return { emocion: "happy", gesto: "nod" };
    }
    if (/\b(sorpresa|increíble|en serio|¿qué\?|imposible|oscura)\b/.test(r)) {
      return { emocion: "surprised", gesto: "surprise" };
    }
    if (/\b(triste|lamento|pena|extraño|difícil|cansad)\b/.test(r)) {
      return { emocion: "sad", gesto: "tilt_head" };
    }
    if (/\b(hola|buenos días|buenas tardes|buenas noches|saludos|chao|hasta luego)\b/.test(r)) {
      return { emocion: "happy", gesto: "wave" };
    }
    if (/\?/.test(textoRespuesta)) {
      return { emocion: "relaxed", gesto: "tilt_head" };
    }
    return { emocion: "relaxed", gesto: "nod" };
  }

  async function responderAutonomoLocal(textoUsuario) {
    const limpio = String(textoUsuario || "").trim();
    if (!limpio) { return; }

    // 1. Responder al instante si es conversación natural conocida, cámara, 3D, cálculo o conocimiento local
    const directa = responderConversacionLocal(limpio, true);
    if (directa) {
      entregarRespuestaAmadeus(directa.respuesta, directa.emocion, directa.gesto);
      return;
    }

    // 2. Si es una pregunta de conocimiento general y el teléfono tiene Internet, consultar enciclopedia en español
    if (navigator.onLine) {
      poner($("rine-hablando-ind"), "buscando…");
      const extractoWeb = await consultarConocimientoWeb(limpio);
      poner($("rine-hablando-ind"), "");
      if (extractoWeb) {
        entregarRespuestaAmadeus(extractoWeb, "happy", "point");
        return;
      }
    }

    // 3. Respaldo conversacional honesto y natural (sin plantillas robóticas)
    const local = responderConversacionLocal(limpio, false);
    entregarRespuestaAmadeus(local.respuesta, local.emocion, local.gesto);
  }

  function entregarRespuestaAmadeus(respuesta, emocion = "relaxed", gesto = "nod") {
    mostrarSubtituloVivo(respuesta);
    agregarBurbujaRine("ella", respuesta);
    enviarComandoVtuber({
      type: "speak",
      text: respuesta,
      emotion: emocion,
      gesture: gesto,
      tts: false
    });
    hablarAutonomo(respuesta);
  }

  function procesarMensajeEscritorio(crudo) {
    if (typeof crudo !== "string" || crudo.length > 65536) { return; }
    let msg;
    try { msg = JSON.parse(crudo); } catch (_) { return; }
    if (!msg || typeof msg.tipo !== "string") { return; }
    const d = (msg.datos && typeof msg.datos === "object") ? msg.datos : {};

    switch (msg.tipo) {
      case "hola": {
        estado.escConectado = true;
        poner($("rine-cab-estado"), "conectado con Amadeus en PC");
        actualizarEstadoCabecera();
        break;
      }
      case "subtitulo": {
        const txt = cadena(d.texto, 4000).trim();
        if (txt) {
          const { emocion, gesto } = inferirEmocionYGesto(txt, "");
          agregarBurbujaRine("ella", txt);
          mostrarSubtituloVivo(txt);
          enviarComandoVtuber({ type: "speak", text: txt, emotion: emocion, gesture: gesto, tts: false });
          if (!estado.hudConectado) {
            hablarAutonomo(txt);
          }
        }
        break;
      }
      case "voz": {
        poner($("rine-hablando-ind"), d.hablando === true ? "hablando…" : "");
        break;
      }
      case "sistema": {
        const txt = cadena(d.texto, 4000).trim();
        if (txt) {
          agregarBurbujaRine("sistema", txt);
        }
        break;
      }
      case "no_molestar": {
        estado.noMolestar = d.usuario === true;
        $("btn-esc-nomolestar").setAttribute("aria-pressed", estado.noMolestar ? "true" : "false");
        break;
      }
      case "permiso": {
        manejarPermisoEscritorio(d);
        break;
      }
    }
  }

  function actualizarInsigniaRine() {
    const ins = $("insignia-rine");
    if (!ins) { return; }
    ins.hidden = estado.sinLeerRine === 0;
    poner(ins, estado.sinLeerRine > 99 ? "99+" : String(estado.sinLeerRine));
  }

  function agregarBurbujaRine(de, texto) {
    const lista = $("rine-lista");
    const vacio = $("rine-vacio");
    if (vacio) { vacio.remove(); }

    const li = document.createElement("li");
    li.className = "msg-rine " + de;
    const burbuja = document.createElement("div");
    burbuja.className = "burbuja-rine";
    burbuja.textContent = texto;
    li.appendChild(burbuja);

    const hora = document.createElement("span");
    hora.className = "hora-rine";
    const f = new Date();
    hora.textContent = String(f.getHours()).padStart(2, "0") + ":" + String(f.getMinutes()).padStart(2, "0");
    li.appendChild(hora);

    lista.appendChild(li);
    while (lista.children.length > MAX_ITEMS_LISTA) {
      lista.removeChild(lista.firstChild);
    }
    lista.scrollTop = lista.scrollHeight;

    if (estado.vistaActual !== "vista-rine" && de !== "yo") {
      estado.sinLeerRine += 1;
      actualizarInsigniaRine();
    }
    return { li, burbuja };
  }

  if ($("btn-rine-limpiar")) {
    $("btn-rine-limpiar").addEventListener("click", () => {
      const lista = $("rine-lista");
      if (!lista) { return; }
      while (lista.firstChild) { lista.removeChild(lista.firstChild); }
      estado.historialAutonomo = [];
      estado.frasesUsadas.clear();
      agregarBurbujaRine("sistema", "Historial de conversación limpiado.");
    });
  }

  function enviarPorEscritorio(objeto) {
    if (!estado.wsEscritorio || estado.wsEscritorio.readyState !== WebSocket.OPEN) {
      return false;
    }
    estado.wsEscritorio.send(JSON.stringify(objeto));
    return true;
  }

  function enviarMensajeUsuario(textoCrudo) {
    const limpio = String(textoCrudo || "").trim();
    if (!limpio) { return false; }
    if (limpio.length > MAX_TEXTO_CHAT) {
      agregarBurbujaRine("sistema", "Mensaje demasiado largo (máximo 2000 caracteres).");
      return false;
    }
    agregarBurbujaRine("yo", limpio);
    if (!enviarPorEscritorio({ tipo: "chat", texto: limpio })) {
      responderAutonomoLocal(limpio);
    }
    return true;
  }

  $("form-vivo").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const inp = $("input-vivo-texto");
    if (enviarMensajeUsuario(inp.value)) { inp.value = ""; }
  });

  $("form-rine").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const inp = $("input-rine-texto");
    if (enviarMensajeUsuario(inp.value)) { inp.value = ""; }
  });

  $("btn-esc-nomolestar").addEventListener("click", () => {
    const activar = !estado.noMolestar;
    if (!enviarPorEscritorio({ tipo: "control", accion: activar ? "no_molestar_on" : "no_molestar_off" })) {
      estado.noMolestar = activar;
      $("btn-esc-nomolestar").setAttribute("aria-pressed", activar ? "true" : "false");
      mostrarSubtituloVivo(activar ? "Modo No Molestar activado." : "Modo No Molestar desactivado.");
    }
  });

  // ------------------------------------------------------------------
  // 7. Confirmación de Permisos (CompuertaPermisos con botón Aprobar/Rechazar)
  // ------------------------------------------------------------------
  function manejarPermisoEscritorio(d) {
    const tarjeta = $("tarjeta-permiso-vivo");
    const id = cadena(d.id, 64);

    if (d.pendiente === true) {
      estado.permisoVivoId = id;
      tarjeta.hidden = false;
      tarjeta.classList.toggle("destructiva", d.destructiva === true);
      poner(
        $("permiso-vivo-titulo"),
        "⚠ Confirmación requerida" + (d.destructiva === true ? " · acción destructiva" : "")
      );
      poner($("permiso-vivo-desc"), cadena(d.descripcion, 300));

      const pre = $("permiso-vivo-borrador");
      if (typeof d.vista_previa === "string" && d.vista_previa) {
        pre.hidden = false;
        poner(pre, cadena(d.vista_previa, 500));
      } else {
        pre.hidden = true;
        poner(pre, "");
      }

      const botones = $("permiso-vivo-botones");
      const nota = $("permiso-vivo-nota");
      if (ID_CONFIRMACION.test(id)) {
        botones.hidden = false;
        nota.hidden = true;
        $("btn-vivo-aprobar").disabled = false;
        $("btn-vivo-rechazar").disabled = false;
      } else {
        botones.hidden = true;
        nota.hidden = false;
        poner(nota, "Esta confirmación solo se responde en la terminal del PC.");
      }

      agregarBurbujaRine("permiso", "⚠ Confirmación solicitada: " + cadena(d.descripcion, 300));
      return;
    }

    if (estado.permisoVivoId === null || id === "" || id === estado.permisoVivoId) {
      tarjeta.hidden = true;
      estado.permisoVivoId = null;
    }
    agregarBurbujaRine(
      "sistema",
      d.aprobado === true ? "Acción aprobada." : "Acción rechazada."
    );
  }

  function responderPermisoVivo(aprobado) {
    const id = estado.permisoVivoId;
    if (!id || !ID_CONFIRMACION.test(id)) { return; }
    if (enviarPorEscritorio({ tipo: "confirmacion", id, aprobado: aprobado === true })) {
      $("btn-vivo-aprobar").disabled = true;
      $("btn-vivo-rechazar").disabled = true;
    }
  }

  $("btn-vivo-aprobar").addEventListener("click", () => responderPermisoVivo(true));
  $("btn-vivo-rechazar").addEventListener("click", () => responderPermisoVivo(false));

  // ------------------------------------------------------------------
  // 8. Dictado por Voz en Android
  // ------------------------------------------------------------------
  let reconocimientoVoz = null;
  let escuchandoVoz = false;

  function alternarDictadoVoz() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      mostrarSubtituloVivo("Reconocimiento de voz no disponible en este dispositivo.");
      return;
    }
    if (escuchandoVoz && reconocimientoVoz) {
      try { reconocimientoVoz.stop(); } catch (_) {}
      return;
    }
    reconocimientoVoz = new SR();
    reconocimientoVoz.lang = "es-CO";
    reconocimientoVoz.continuous = false;
    reconocimientoVoz.interimResults = false;

    const actualizarBotonesVoz = (activo) => {
      escuchandoVoz = activo;
      [$("btn-voz-dictado"), $("btn-rine-voz")].forEach((b) => {
        if (!b) { return; }
        b.classList.toggle("escuchando", activo);
        poner(b, activo ? "Oyendo…" : "Hablar");
      });
    };

    reconocimientoVoz.onstart = () => actualizarBotonesVoz(true);
    reconocimientoVoz.onend = () => actualizarBotonesVoz(false);
    reconocimientoVoz.onerror = () => actualizarBotonesVoz(false);
    reconocimientoVoz.onresult = (ev) => {
      const texto = ev.results?.[0]?.[0]?.transcript || "";
      if (texto.trim()) {
        enviarMensajeUsuario(texto.trim());
      }
    };

    try { reconocimientoVoz.start(); } catch (_) {}
  }

  $("btn-voz-dictado").addEventListener("click", alternarDictadoVoz);
  $("btn-rine-voz").addEventListener("click", alternarDictadoVoz);

  // ------------------------------------------------------------------
  // 9. Estudio VRM 3D Completo
  // ------------------------------------------------------------------
  function normalizarComandoVtuber(cmd) {
    if (!cmd || typeof cmd !== "object") { return cmd; }
    const copia = Object.assign({}, cmd);
    if (copia.type === "background" && copia.color && !copia.mode) {
      copia.mode = copia.color;
    }
    if (copia.type === "turn") {
      return {
        type: "move",
        rotation: Number(copia.angle || 0),
        relative: copia.relative !== false,
        duration: 0.45
      };
    }
    return copia;
  }

  async function enviarComandoVtuber(comando) {
    const cmdNorm = normalizarComandoVtuber(comando);
    registrarActividadVtuber(cmdNorm);

    // 1. Ejecutar inmediatamente en el visor 3D local del teléfono (#marco-vrm)
    try {
      const marco = $("marco-vrm");
      const vt = marco && marco.contentWindow && marco.contentWindow.vtuber;
      if (vt && typeof vt.execute === "function") {
        vt.execute(cmdNorm);
      }
    } catch (_) {}

    // 2. Si está conectado al PC en llamada, reenviar también al servidor del PC
    if (estado.hudConectado || estado.escConectado) {
      try {
        const qs = new URLSearchParams({
          t: estado.tokenMovil,
          payload: JSON.stringify(comando)
        });
        await fetch(`${baseServidorHttp()}/rams/vtuber-command?${qs.toString()}`);
      } catch (_) {}
    }
  }

  // Persistencia local del archivo .VRM en IndexedDB del celular para modo offline
  function abrirDbVrm() {
    return new Promise((resolve, reject) => {
      if (!("indexedDB" in window)) {
        reject(new Error("Sin IndexedDB"));
        return;
      }
      const req = indexedDB.open("amadeus_vrm_cache", 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("modelos")) {
          db.createObjectStore("modelos");
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function guardarVrmEnCelular(nombre, buffer) {
    try {
      const db = await abrirDbVrm();
      await new Promise((resolve, reject) => {
        const tx = db.transaction("modelos", "readwrite");
        tx.objectStore("modelos").put({ nombre, buffer, fecha: Date.now() }, "activo");
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    } catch (_) {}
  }

  async function restaurarVrmGuardadoEnCelular() {
    try {
      const db = await abrirDbVrm();
      const reg = await new Promise((resolve, reject) => {
        const tx = db.transaction("modelos", "readonly");
        const r = tx.objectStore("modelos").get("activo");
        r.onsuccess = () => resolve(r.result || null);
        r.onerror = () => reject(r.error);
      });
      if (reg && reg.buffer) {
        if (estado.urlBlobVrmLocal) {
          try { URL.revokeObjectURL(estado.urlBlobVrmLocal); } catch (_) {}
        }
        const blob = new Blob([reg.buffer], { type: "model/gltf-binary" });
        estado.urlBlobVrmLocal = URL.createObjectURL(blob);
        enviarComandoVtuber({ type: "load_model", url: estado.urlBlobVrmLocal });
        return true;
      }
    } catch (_) {}
    return false;
  }

  function poblarChips(idContenedor, pares, alPulsar) {
    const cont = $(idContenedor);
    if (!cont) { return; }
    pares.forEach(([clave, etiqueta]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip-btn";
      btn.textContent = etiqueta;
      btn.addEventListener("click", () => alPulsar(clave, btn));
      cont.appendChild(btn);
    });
  }

  function poblarSelectorVoces() {
    const sel = $("vrm-voice-select");
    if (!sel) { return; }
    while (sel.firstChild) { sel.removeChild(sel.firstChild); }

    const optNativa = document.createElement("option");
    optNativa.value = "";
    optNativa.textContent = window.AmadeusAndroid ? "Voz nativa Android (Español)" : "Voz por defecto del sistema";
    sel.appendChild(optNativa);

    if ("speechSynthesis" in window && typeof window.speechSynthesis.getVoices === "function") {
      const voces = window.speechSynthesis.getVoices();
      voces.forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v.name;
        opt.textContent = `${v.name} (${v.lang})`;
        sel.appendChild(opt);
      });
    }
  }

  function inicializarEstudioVtuber() {
    const selEmo = $("vrm-speak-emo");
    VRM_EMOCIONES.forEach(([k, lbl]) => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = lbl;
      selEmo.appendChild(opt);
    });

    const selGes = $("vrm-speak-ges");
    const optSinGesto = document.createElement("option");
    optSinGesto.value = "";
    optSinGesto.textContent = "(Sin gesto)";
    selGes.appendChild(optSinGesto);
    VRM_GESTOS.forEach(([k, lbl]) => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = lbl;
      selGes.appendChild(opt);
    });

    $("btn-vrm-speak").addEventListener("click", () => {
      const texto = $("vrm-speak-texto").value.trim();
      if (!texto) { return; }
      const cmd = {
        type: "speak",
        text: texto,
        emotion: selEmo.value || "neutral",
        tts: false
      };
      if (selGes.value) { cmd.gesture = selGes.value; }
      enviarComandoVtuber(cmd);
      hablarAutonomo(texto);
    });

    const chkTts = $("vrm-tts-on");
    if (chkTts) {
      chkTts.addEventListener("change", () => {
        estado.ttsActivo = chkTts.checked;
      });
    }
    poblarSelectorVoces();
    if ("speechSynthesis" in window) {
      window.speechSynthesis.onvoiceschanged = poblarSelectorVoces;
    }
    const selVoz = $("vrm-voice-select");
    if (selVoz) {
      selVoz.addEventListener("change", () => {
        estado.ttsVozNombre = selVoz.value;
      });
    }
    const sliderPitch = $("vrm-pitch");
    if (sliderPitch) {
      sliderPitch.addEventListener("input", () => {
        const pct = Number(sliderPitch.value) || 108;
        estado.ttsPitch = pct / 100;
        poner($("vrm-pitch-val"), pct + "%");
        if (window.AmadeusAndroid && typeof window.AmadeusAndroid.ajustarVoz === "function") {
          window.AmadeusAndroid.ajustarVoz(estado.ttsPitch, 1.04);
        }
      });
    }

    const sliderExp = $("vrm-exp-slider");
    sliderExp.addEventListener("input", () => {
      poner($("vrm-exp-pct"), sliderExp.value + "%");
    });
    poblarChips("chips-expresiones", VRM_EMOCIONES, (nombre) => {
      const val = Number(sliderExp.value) / 100;
      enviarComandoVtuber({ type: "expression", name: nombre, value: val, duration: 0.3 });
    });

    poblarChips("chips-poses", VRM_POSES, (nombre) => {
      enviarComandoVtuber({ type: "pose", name: nombre, duration: 0.55 });
    });

    poblarChips("chips-gestos", VRM_GESTOS, (nombre) => {
      enviarComandoVtuber({ type: "gesture", name: nombre });
    });

    poblarChips("chips-mirada", VRM_MIRADAS, (objetivo) => {
      enviarComandoVtuber({ type: "look", at: objetivo });
    });

    poblarChips("chips-manos", VRM_MANOS, (forma) => {
      const lado = $("vrm-mano-lado").value || "both";
      enviarComandoVtuber({ type: "hand", side: lado, shape: forma });
    });

    document.querySelectorAll("[data-move]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const v = btn.getAttribute("data-move");
        if (v === "center") {
          enviarComandoVtuber({ type: "move", x: 0, z: 0, relative: false });
        } else if (v) {
          const [dx, dz] = v.split(",").map(Number);
          enviarComandoVtuber({ type: "move", x: dx, z: dz, relative: true });
        }
      });
    });

    document.querySelectorAll("[data-turn]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const deg = Number(btn.getAttribute("data-turn") || "0");
        if (deg === 0) {
          enviarComandoVtuber({ type: "turn", angle: 0, relative: false });
        } else {
          enviarComandoVtuber({ type: "turn", angle: deg, relative: true });
        }
      });
    });

    const chkIdle = $("vrm-idle-on");
    const sliderIdle = $("vrm-idle-int");
    const chkBlink = $("vrm-blink-on");
    const enviarReposo = () => {
      const enabled = chkIdle ? chkIdle.checked : true;
      const intensity = sliderIdle ? Number(sliderIdle.value) / 100 : 1.0;
      if (sliderIdle) {
        poner($("vrm-idle-int-val"), sliderIdle.value + "%");
      }
      enviarComandoVtuber({ type: "idle", enabled, intensity });
    };
    if (chkIdle) { chkIdle.addEventListener("change", enviarReposo); }
    if (sliderIdle) { sliderIdle.addEventListener("input", enviarReposo); }
    if (chkBlink) {
      chkBlink.addEventListener("change", () => {
        enviarComandoVtuber({ type: "idle", blink: chkBlink.checked });
      });
    }

    $("btn-vrm-reset").addEventListener("click", () => {
      enviarComandoVtuber({ type: "reset" });
    });

    const selHueso = $("vrm-hueso-sel");
    VRM_HUESOS.forEach((h) => {
      const opt = document.createElement("option");
      opt.value = h;
      opt.textContent = h;
      selHueso.appendChild(opt);
    });

    const enviarHuesoActual = () => {
      const hueso = selHueso.value;
      const rx = Number($("vrm-hx").value);
      const ry = Number($("vrm-hy").value);
      const rz = Number($("vrm-hz").value);
      poner($("vrm-hx-val"), rx + "°");
      poner($("vrm-hy-val"), ry + "°");
      poner($("vrm-hz-val"), rz + "°");
      enviarComandoVtuber({
        type: "bones",
        rotations: { [hueso]: { x: rx, y: ry, z: rz } },
        duration: 0.15
      });
    };

    ["vrm-hx", "vrm-hy", "vrm-hz"].forEach((id) => {
      $(id).addEventListener("input", enviarHuesoActual);
    });

    $("btn-vrm-hueso-cero").addEventListener("click", () => {
      $("vrm-hx").value = "0";
      $("vrm-hy").value = "0";
      $("vrm-hz").value = "0";
      enviarHuesoActual();
    });

    poblarChips("chips-camara-vrm", VRM_CAMARAS, (preset) => {
      enviarComandoVtuber({ type: "camera", preset });
    });
    poblarChips("chips-fondo-vrm", VRM_FONDOS, (color) => {
      enviarComandoVtuber({ type: "background", color });
    });
    poblarChips("chips-tema-vrm", VRM_TEMAS, (nombreTema) => {
      enviarComandoVtuber({ type: "theme", name: nombreTema });
    });

    $("btn-vrm-cargar-modelo").addEventListener("click", () => {
      const urlModelo = $("vrm-modelo-sel").value;
      if (urlModelo) {
        enviarComandoVtuber({ type: "load_model", url: urlModelo });
      }
    });

    const btnArchivoLocal = $("btn-vrm-archivo-local");
    const inputArchivoLocal = $("input-vrm-local");
    if (btnArchivoLocal && inputArchivoLocal) {
      btnArchivoLocal.addEventListener("click", () => inputArchivoLocal.click());
      inputArchivoLocal.addEventListener("change", async (ev) => {
        const archivo = ev.target.files && ev.target.files[0];
        if (!archivo) { return; }
        try {
          const buffer = await archivo.arrayBuffer();
          await guardarVrmEnCelular(archivo.name, buffer);
          if (estado.urlBlobVrmLocal) {
            try { URL.revokeObjectURL(estado.urlBlobVrmLocal); } catch (_) {}
          }
          const blob = new Blob([buffer], { type: "model/gltf-binary" });
          estado.urlBlobVrmLocal = URL.createObjectURL(blob);
          enviarComandoVtuber({ type: "load_model", url: estado.urlBlobVrmLocal });
          mostrarSubtituloVivo("Modelo VRM cargado y guardado para uso sin internet: " + archivo.name);
          poner($("llamada-estado-red"), "Modelo VRM listo: " + archivo.name);
        } catch (_) {}
      });
    }

    const selPreset = $("vrm-console-preset");
    const txtConsola = $("vrm-console-input");
    const cargarEjemploConsola = () => {
      if (!selPreset || !txtConsola) { return; }
      const obj = EJEMPLOS_CONSOLA[selPreset.value] || EJEMPLOS_CONSOLA.saludo;
      txtConsola.value = JSON.stringify(obj, null, 2);
    };
    if (selPreset) {
      selPreset.addEventListener("change", cargarEjemploConsola);
      cargarEjemploConsola();
    }
    if ($("btn-vrm-console-run")) {
      $("btn-vrm-console-run").addEventListener("click", () => {
        try {
          const cmd = JSON.parse(txtConsola.value);
          enviarComandoVtuber(cmd);
        } catch (_) {
          registrarActividadVtuber({ error: "JSON inválido" });
        }
      });
    }
    if ($("btn-vrm-console-fmt")) {
      $("btn-vrm-console-fmt").addEventListener("click", () => {
        try {
          txtConsola.value = JSON.stringify(JSON.parse(txtConsola.value), null, 2);
        } catch (_) {}
      });
    }
    if ($("btn-vrm-clear-log")) {
      $("btn-vrm-clear-log").addEventListener("click", () => {
        const ul = $("vrm-log");
        if (!ul) { return; }
        while (ul.firstChild) { ul.removeChild(ul.firstChild); }
      });
    }
  }

  async function refrescarEstadoVtuber() {
    const sel = $("vrm-modelo-sel");
    if (!sel) { return; }
    while (sel.firstChild) { sel.removeChild(sel.firstChild); }

    const optDef = document.createElement("option");
    optDef.value = "sample";
    optDef.textContent = "Modelo Amadeus integrado (Offline)";
    sel.appendChild(optDef);

    try {
      const resp = await fetch("/api/models");
      if (resp.ok) {
        const datos = await resp.json();
        const modelos = Array.isArray(datos) ? datos : (Array.isArray(datos.models) ? datos.models : []);
        modelos.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.url || ("/models/" + m.name);
          opt.textContent = m.name || m.url;
          sel.appendChild(opt);
        });
      }
    } catch (_) {}
  }

  // Lectura real de la batería del dispositivo
  if (navigator.getBattery) {
    navigator.getBattery().then((bat) => {
      const actBat = () => {
        if (!estado.hudConectado) {
          actualizarCeldasBateria(Math.round(bat.level * 100));
        }
      };
      actBat();
      bat.addEventListener("levelchange", actBat);
    }).catch(() => {});
  }

  // ------------------------------------------------------------------
  // 10. Modal de configuración de red con el PC
  // ------------------------------------------------------------------
  function abrirModalConfig() {
    const inpSrv = $("input-url-servidor");
    if (inpSrv) {
      inpSrv.value = estado.urlServidor + (estado.tokenMovil ? ("/?t=" + estado.tokenMovil) : "");
    }
    $("input-token-hud").value = estado.tokenHud;
    $("modal-config").hidden = false;
  }

  $("btn-abrir-config").addEventListener("click", abrirModalConfig);

  $("btn-cerrar-config").addEventListener("click", () => {
    $("modal-config").hidden = true;
  });

  $("btn-guardar-config").addEventListener("click", async () => {
    const inpSrv = $("input-url-servidor");
    if (inpSrv && inpSrv.value.trim()) {
      const parsed = parsearServidorYToken(inpSrv.value);
      estado.urlServidor = parsed.base;
      guardarPref("amadeus.servidor.url", estado.urlServidor);
      if (parsed.token) {
        estado.tokenMovil = parsed.token;
        guardarPref("amadeus.movil.token", estado.tokenMovil);
      }
    }
    const nuevo = extraerTokenHud($("input-token-hud").value);
    estado.tokenHud = nuevo;
    guardarPref("amadeus.hud.token", nuevo);
    $("modal-config").hidden = true;

    const info = await sondearServidorPc(1500);
    if (info) {
      poner($("llamada-estado-red"), "PC detectado en red (" + estado.urlServidor + ")");
      if (estado.enLlamada) {
        estado.modoLlamada = "pc";
        conectarEscritorio();
        conectarHud();
      }
    } else {
      poner($("llamada-estado-red"), "PC no detectado · Listo en Modo Local (Offline)");
    }
  });

  // ------------------------------------------------------------------
  // 11. Arranque e inicialización
  // ------------------------------------------------------------------
  if ("serviceWorker" in navigator && !esAppNativa) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }

  inicializarBarrasConectoma();
  limpiarLecturasPcDesconectado();
  inicializarEstudioVtuber();
  cargarVisor3D();

  const marcoVrm = $("marco-vrm");
  if (marcoVrm) {
    marcoVrm.addEventListener("load", () => {
      redimensionarVisor3D();
      setTimeout(async () => {
        redimensionarVisor3D();
        const restaurado = await restaurarVrmGuardadoEnCelular();
        if (!restaurado) {
          refrescarEstadoVtuber();
        }
      }, 450);
    });
  }

  window.addEventListener("resize", redimensionarVisor3D);

  // Sondeo silencioso al abrir para indicar en la pantalla de llamada si el PC está al alcance
  sondearServidorPc(1200).then((info) => {
    if (info) {
      poner($("llamada-estado-red"), "Amadeus PC detectado en red · Listo para llamar");
    } else {
      poner($("llamada-estado-red"), "Modo Local 3D listo (Funciona sin internet)");
    }
  });
})();
