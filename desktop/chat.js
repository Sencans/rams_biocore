'use strict';
/**
 * Ventana de chat de Amadeus Desktop (COMPANION.md §C1.5, §C2.7.2, §C2.7.4).
 * - Todo el texto se pinta con `textContent` (nunca innerHTML).
 * - El usuario ve su mensaje ('yo') al instante, en local; R.A.M.S. no lo reenvía.
 * - Enter envía, salvo durante 600 ms tras recibir el foco (evita envíos accidentales si la ventana
 *   aparece mientras escribes en otra app). Los botones de confirmación ignoran la activación por
 *   teclado en esa misma ventana de tiempo.
 * - Las confirmaciones van por BOTONES ligados al `id` de la solicitud; el texto del chat nunca
 *   aprueba nada.
 * - Los enlaces solo se abren con un clic (y el main solo abre URLs que R.A.M.S. ofreció).
 */
(() => {
  const ESPERA_ENTER_MS = 600;
  const MAX_MENSAJES = 300;
  const MAX_TEXTO = 2000;
  const RE_ID = /^[0-9a-f]{8}$/;

  const api = window.amadeusChat;
  const $ = (id) => document.getElementById(id);
  const lista = $('lista');
  const vacio = $('vacio');
  const texto = $('texto');
  const form = $('form');
  const botonEnviar = $('enviar');
  const estado = $('estado');
  const estadoTexto = $('estado-texto');
  const hablando = $('hablando');
  const contador = $('contador');
  const menu = $('menu');

  const SIN_CONEXION = 'Sin conexión con R.A.M.S.: se comprobará al volver.';

  let focoDesde = performance.now();
  let enviando = false;
  let conectada = null; // null = aún no se sabe (la página acaba de cargar)
  const permisos = new Map(); // id → {el, pendiente, enviando, botones, resultado}

  const marcarFoco = () => {
    focoDesde = performance.now();
  };
  const recienEnfocada = () => performance.now() - focoDesde < ESPERA_ENTER_MS;
  window.addEventListener('focus', marcarFoco);
  texto.addEventListener('focus', marcarFoco);

  // ---------------------------------------------------------------- utilidades de la lista

  function cercaDelFinal() {
    return lista.scrollHeight - lista.scrollTop - lista.clientHeight < 80;
  }

  function agregar(el, forzarScroll = false) {
    const bajar = forzarScroll || cercaDelFinal();
    if (vacio && vacio.parentNode) vacio.remove();
    lista.append(el);
    while (lista.children.length > MAX_MENSAJES) {
      const primero = lista.firstElementChild;
      for (const [id, p] of permisos) if (p.el === primero) permisos.delete(id);
      primero.remove();
    }
    if (bajar) lista.scrollTop = lista.scrollHeight;
  }

  function hora() {
    const t = document.createElement('time');
    const d = new Date();
    t.dateTime = d.toISOString();
    t.textContent = d.toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' });
    return t;
  }

  function etiquetaUrl(url) {
    try {
      const u = new URL(url);
      const ruta = u.pathname.length > 1 ? u.pathname : '';
      return `Abrir ${u.hostname}${ruta}`;
    } catch {
      return 'Abrir enlace';
    }
  }

  function agregarMensaje(m) {
    if (!m || typeof m.texto !== 'string' || !m.texto.trim()) return null;
    const de = m.de === 'yo' || m.de === 'sistema' ? m.de : 'amadeus';
    const art = document.createElement('article');
    art.className = `msg ${de}`;
    if (de === 'amadeus') {
      const autor = document.createElement('span');
      autor.className = 'autor';
      autor.textContent = 'Amadeus';
      art.append(autor);
    }
    const cuerpo = document.createElement('p');
    cuerpo.className = 'cuerpo';
    cuerpo.textContent = m.texto;
    art.append(cuerpo);
    if (de === 'amadeus' && typeof m.url === 'string' && /^https:\/\//.test(m.url) && m.url.length <= 300) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'enlace';
      b.textContent = etiquetaUrl(m.url);
      b.title = m.url;
      b.addEventListener('click', () => api.abrirEnlace(m.url));
      art.append(b);
    }
    art.append(hora());
    agregar(art);
    return art;
  }

  const agregarSistema = (t) => agregarMensaje({ de: 'sistema', texto: t });

  // ---------------------------------------------------------------- confirmaciones
  //
  // Estados de una tarjeta pendiente: botones activos → (clic) "Aprobando…" con los botones
  // bloqueados hasta que R.A.M.S. la resuelva. Si la orden no sale (sin conexión), los botones
  // vuelven a activarse y se avisa. Al desconectarse, las pendientes se bloquean; al reconectar,
  // la instantánea de R.A.M.S. las reactiva (`pendiente:true` con el mismo id) o el main las cierra
  // (`caducada:true`).

  function fijarResultado(p, texto) {
    p.resultado.textContent = texto;
    p.resultado.hidden = !texto;
  }

  function fijarBotones(p, activos) {
    for (const b of p.botones) b.disabled = !activos;
  }

  function cerrarTarjeta(p, resultado) {
    if (!p || !p.pendiente) return;
    p.pendiente = false;
    p.el.classList.add('cerrada');
    fijarBotones(p, false);
    fijarResultado(p, resultado);
    if (cercaDelFinal()) p.el.scrollIntoView({ block: 'nearest' });
  }

  /** R.A.M.S. confirma que sigue pendiente: se puede volver a responder. */
  function reactivarTarjeta(p) {
    if (!p.pendiente || p.enviando || conectada === false) return;
    p.enviada = false; // si la respuesta anterior se perdió en la caída, hay que darla otra vez
    fijarBotones(p, true);
    fijarResultado(p, '');
  }

  function crearTarjeta(m) {
    const el = document.createElement('section');
    el.className = `permiso${m.destructiva ? ' destructiva' : ''}`;
    el.setAttribute('aria-label', 'Confirmación pendiente');
    const h = document.createElement('h2');
    h.textContent = 'Confirmación';
    const desc = document.createElement('p');
    desc.textContent = m.descripcion;
    el.append(h, desc);
    if (typeof m.vista_previa === 'string' && m.vista_previa) {
      const et = document.createElement('p');
      et.className = 'etiqueta';
      et.textContent = 'Vista previa (no se enviará: tú pulsas Enviar en la app):';
      const pre = document.createElement('pre');
      pre.textContent = m.vista_previa;
      el.append(et, pre);
    }
    const botones = document.createElement('div');
    botones.className = 'botones';
    const aprobar = document.createElement('button');
    aprobar.type = 'button';
    aprobar.className = 'aprobar';
    aprobar.textContent = 'Aprobar';
    const rechazar = document.createElement('button');
    rechazar.type = 'button';
    rechazar.className = 'rechazar';
    rechazar.textContent = 'Rechazar';
    botones.append(aprobar, rechazar);
    const resultado = document.createElement('p');
    resultado.className = 'resultado';
    resultado.hidden = true;
    el.append(botones, resultado);
    // enviando: esperando al main; enviada: salió y falta que R.A.M.S. la resuelva.
    const p = { el, pendiente: true, enviando: false, enviada: false, botones: [aprobar, rechazar], resultado };
    const responder = (aprobado) => async (e) => {
      // Activación por teclado justo después de recibir el foco: se ignora.
      if (e.detail === 0 && recienEnfocada()) return;
      if (!p.pendiente || p.enviando || p.enviada) return;
      p.enviando = true;
      fijarBotones(p, false);
      fijarResultado(p, aprobado ? 'Aprobando…' : 'Rechazando…');
      el.scrollIntoView({ block: 'nearest' });
      let ok = false;
      try {
        ok = (await api.confirmar(m.id, aprobado)) === true;
      } catch {
        ok = false;
      }
      p.enviando = false;
      if (ok) p.enviada = true;
      if (ok || !p.pendiente) return; // salió: la tarjeta se cierra cuando R.A.M.S. la resuelva
      if (conectada === false) {
        fijarResultado(p, SIN_CONEXION);
      } else {
        fijarBotones(p, true);
        fijarResultado(p, '');
      }
      agregarSistema('No se envió la confirmación: R.A.M.S. no está conectada. Pulsa otra vez cuando vuelva.');
    };
    aprobar.addEventListener('click', responder(true));
    rechazar.addEventListener('click', responder(false));
    agregar(el, true); // la confirmación siempre a la vista
    return p;
  }

  function mostrarPermiso(m) {
    if (!m || typeof m.id !== 'string' || !RE_ID.test(m.id)) return;
    const actual = permisos.get(m.id);
    if (m.pendiente === true) {
      // Solo puede haber una confirmación pendiente: las demás ya no valen.
      for (const [id, p] of permisos) if (id !== m.id) cerrarTarjeta(p, 'Ya no está pendiente.');
      // La misma pendiente otra vez (instantánea al reconectar, recarga del chat): sigue vigente.
      if (actual && actual.pendiente) {
        reactivarTarjeta(actual);
      } else {
        const p = crearTarjeta(m); // nueva, o R.A.M.S. dice que una ya cerrada sigue pendiente
        permisos.set(m.id, p);
        if (conectada === false) {
          fijarBotones(p, false);
          fijarResultado(p, SIN_CONEXION);
        }
      }
      return;
    }
    if (actual) {
      let r = 'Resuelta.';
      if (m.caducada === true) r = 'Ya no está pendiente.';
      else if (m.aprobado === true) r = 'Aprobada.';
      else if (m.aprobado === false) r = 'Rechazada.';
      cerrarTarjeta(actual, r);
    }
  }

  // ---------------------------------------------------------------- estado

  function fijarEstado(s) {
    const enLinea = s === 'en_linea';
    estado.classList.toggle('en_linea', enLinea);
    estado.classList.toggle('desconectada', !enLinea);
    estadoTexto.textContent = enLinea ? 'en línea' : 'desconectada';
    const antes = conectada;
    conectada = enLinea;
    if (!enLinea && antes !== false) {
      // Sin conexión no se puede responder; al volver, la instantánea dirá si siguen pendientes.
      for (const p of permisos.values()) {
        if (!p.pendiente) continue;
        fijarBotones(p, false);
        if (!p.enviando) fijarResultado(p, SIN_CONEXION);
      }
    }
  }

  // ---------------------------------------------------------------- entrada

  function ajustarAltura() {
    texto.style.height = 'auto';
    texto.style.height = `${Math.min(texto.scrollHeight, 140)}px`;
    const n = texto.value.length;
    contador.textContent = n > MAX_TEXTO * 0.8 ? `${n}/${MAX_TEXTO}` : '';
  }

  async function enviar() {
    const t = texto.value.trim();
    if (!t || enviando) return;
    enviando = true;
    botonEnviar.disabled = true;
    const el = agregarMensaje({ de: 'yo', texto: t });
    texto.value = '';
    ajustarAltura();
    let ok = false;
    try {
      ok = (await api.send(t)) === true;
    } catch {
      ok = false;
    }
    enviando = false;
    botonEnviar.disabled = false;
    if (!ok) {
      if (el) {
        el.classList.add('fallido');
        el.title = 'No enviado';
      }
      agregarSistema('No se pudo enviar: R.A.M.S. no está conectada. Tu mensaje volvió a la caja de texto.');
      if (!texto.value) {
        texto.value = t;
        ajustarAltura();
      }
    }
  }

  // ---------------------------------------------------------------- arranque

  if (!api || api.version !== 1) {
    agregarSistema('Esta ventana debe abrirse desde Amadeus Desktop.');
    texto.disabled = true;
    botonEnviar.disabled = true;
    return;
  }

  api.onMessage(agregarMensaje);
  api.onStatus(fijarEstado);
  api.onVoz((h) => {
    const bajar = cercaDelFinal();
    hablando.hidden = h !== true; // cambia el alto de la lista: se mantiene pegada al final
    if (bajar) lista.scrollTop = lista.scrollHeight;
  });
  api.onPermiso(mostrarPermiso);

  texto.addEventListener('input', ajustarAltura);
  texto.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (recienEnfocada()) return; // Enter ignorado 600 ms tras recibir el foco
      enviar();
    }
  });
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    enviar();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (menu.open) menu.open = false;
      else api.hide();
    }
  });
  $('btn-cerrar').addEventListener('click', () => api.hide());

  for (const b of menu.querySelectorAll('[data-control]')) {
    b.addEventListener('click', () => {
      api.control(b.dataset.control);
      menu.open = false;
    });
  }
  document.addEventListener('click', (e) => {
    if (menu.open && !menu.contains(e.target)) menu.open = false;
  });

  ajustarAltura();
  if (document.hasFocus()) marcarFoco();
})();
