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
 * - Tarjetas aprobables (v0.2.2, docs/V022_CORREO_DRIVE_SCRIPTS.md §13.4): correo, Drive, script y
 *   resultado de un script. Se pintan con textContent; el código de un script va COMPLETO (nunca se
 *   recorta) en `pre > code` con números de línea. Solo se aprueban con sus botones, ligados al `id`, a
 *   la huella y a la VERSIÓN mostradas; tras el clic quedan bloqueados hasta la versión nueva o el
 *   cierre. El segundo clic de un doble clic nunca cuenta, y un «aprobar»/«aprobar_publico» recién
 *   repintado (tras un clic o con otra versión) no se puede pulsar durante ARMADO_MS: así un doble
 *   clic sobre «Compartir» no completa también «Sí, hacerlo público». Las tarjetas cuentan en
 *   MAX_MENSAJES.
 */
(() => {
  const ESPERA_ENTER_MS = 600;
  /** Tras repintar una tarjeta (otra versión o respuesta a un clic), sus botones de aprobar esperan esto. */
  const ARMADO_MS = 1000;
  const MAX_MENSAJES = 300;
  const MAX_TEXTO = 2000;
  const RE_ID = /^[0-9a-f]{8}$/;
  const CLASES_TARJETA = ['correo', 'drive', 'script', 'salida'];
  /** Topes de los campos de edición del correo (los de R.A.M.S., §12.3): toda decisión cabe en 60 KiB. */
  const TOPES_CAMPOS = { para: 2600, cc: 2600, cco: 2600, asunto: 200, cuerpo: 10000, adjuntos: 1000 };
  const ETIQUETAS_CAMPOS = { para: 'Para', cc: 'CC', cco: 'CCO', asunto: 'Asunto', cuerpo: 'Cuerpo', adjuntos: 'Adjuntos' };
  const CAMPOS_LISTA = ['para', 'cc', 'cco', 'adjuntos'];   // campos con pendientes que el formulario no muestra
  const MAX_NOMBRE_HABILIDAD = 40;
  // Los mismos controles que rechazan lib/mapeo.js y R.A.M.S. (§12.3): se comprueban ANTES de enviar
  // para decir qué pasa (nunca «Sin conexión» por un dato inválido) y nunca se quitan sin pedirlo.
  // eslint-disable-next-line no-control-regex
  const RE_CONTROLES = /[\u0000-\u001f\u007f]/;
  // eslint-disable-next-line no-control-regex
  const RE_CONTROLES_CUERPO = /[\u0000-\u0008\u000b-\u001f\u007f]/;   // en el cuerpo valen \t y \n
  const RE_SUSTITUTO_SUELTO = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

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
  const tarjetas = new Map(); // id → estado de la burbuja de una tarjeta (ver crearBurbujaTarjeta)

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
      for (const [id, t] of tarjetas) if (t.el === primero) tarjetas.delete(id); // también cuentan
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

  // ---------------------------------------------------------------- tarjetas aprobables (v0.2.2)
  //
  // Una burbuja `article.msg.tarjeta.<clase>` por id. Si llega una versión mayor (o la misma con otro
  // aviso o botones), se repinta la MISMA burbuja; una versión menor se ignora. Tras pulsar un botón
  // queda bloqueada («Enviando…»/«Ejecutando…») hasta la versión nueva o el cierre; si la orden no
  // sale, se reactiva con «Sin conexión con R.A.M.S.». Nada se aprueba con texto.

  const SIN_CONEXION_TARJETA = 'Sin conexión con R.A.M.S.';
  const NOMBRE_LENGUAJE = { powershell: 'PowerShell', python: 'Python', bash: 'Bash' };
  const NOMBRE_NIVEL = { bajo: 'riesgo bajo', medio: 'riesgo medio', alto: 'riesgo alto', critico: 'riesgo crítico' };
  const NOMBRE_CARPETA = {
    descargas: 'Descargas', documentos: 'Documentos', escritorio: 'Escritorio', imagenes: 'Imágenes',
    musica: 'Música', videos: 'Vídeos',
  };
  const NOMBRE_ROL = { reader: 'lector', commenter: 'comentarista', writer: 'editor' };
  const NOMBRE_OP = { subir: 'subir', papelera: 'papelera', compartir: 'compartir' };
  const RESULTADO = {
    enviado: 'Enviado a Gmail', hecho: 'Hecho', ejecutado: 'Ejecutado', descartado: 'Descartado',
    caducado: 'Caducó', reemplazado: 'Reemplazado por uno nuevo',
  };
  const EN_CURSO = {
    aprobar: 'Enviando…', aprobar_publico: 'Enviando…', editar: 'Guardando cambios…',
    guardar: 'Guardando la habilidad…', descartar: 'Descartando…',
  };

  const texto_ = (v) => (typeof v === 'string' ? v : '');
  const lista_ = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []);
  const numero = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

  /** Crea un elemento con clase y texto (siempre `textContent`). */
  function nodo(tag, clase, contenido) {
    const e = document.createElement(tag);
    if (clase) e.className = clase;
    if (contenido !== undefined) e.textContent = contenido;
    return e;
  }

  const coma = (x, d = 1) => x.toFixed(d).replace('.', ',');
  function tamano(bytes) {
    const n = numero(bytes);
    if (n === null) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${coma(n / 1024)} KB`;
    if (n < 1024 * 1024 * 1024) return `${coma(n / (1024 * 1024))} MB`;
    return `${coma(n / (1024 * 1024 * 1024))} GB`;
  }
  const carpeta = (c) => NOMBRE_CARPETA[c] || texto_(c);

  function encabezado(d) {
    if (d.clase === 'correo') return '📧 Correo';
    if (d.clase === 'drive') return `🗂 Drive · ${NOMBRE_OP[d.drive.op] || 'operación'}`;
    if (d.clase === 'script') return `🧪 Script · ${NOMBRE_LENGUAJE[d.script.lenguaje] || 'script'}`;
    return '▶ Resultado del script';
  }

  function etiquetaBoton(d, boton, t) {
    switch (boton) {
      case 'aprobar':
        if (d.clase === 'correo') return 'Enviar';
        if (d.clase === 'drive') return { subir: 'Subir', papelera: 'Mover a la papelera', compartir: 'Compartir' }[d.drive.op] || 'Aprobar';
        if (d.clase === 'script') return numero(d.script.ejecuciones) > 0 ? 'Ejecutar otra vez' : 'Ejecutar';
        return 'Aprobar';
      case 'aprobar_publico': return 'Sí, hacerlo público';
      case 'editar': return 'Editar';
      case 'guardar': return 'Guardar como habilidad';
      case 'copiar': return t.copiado ? 'Copiado' : 'Copiar código';
      case 'descartar': return d.clase === 'salida' ? 'Cerrar' : 'Descartar';
      default: return null;
    }
  }

  /** ¿Se puede pulsar «aprobar» según el contenido? (el freno real es la compuerta de R.A.M.S.) */
  function puedeAprobar(t) {
    const d = t.datos;
    if (d.clase === 'correo') return d.correo.listo === true;
    if (d.clase === 'drive') return d.drive.listo === true;
    if (d.clase === 'script') {
      const s = d.script;
      return s.ejecutable === true && s.nivel !== 'critico' && (s.nivel !== 'alto' || t.riesgoOk);
    }
    return false;
  }

  function campo(etiqueta, valor, clase = '') {
    const fila = nodo('div', `campo${clase ? ` ${clase}` : ''}`);
    fila.append(nodo('span', 'etiqueta', etiqueta), nodo('span', 'valor', valor));
    return fila;
  }

  function listaDe(clase, items, titulo) {
    const caja = nodo('div', clase);
    if (titulo) caja.append(nodo('p', 'subtitulo', titulo));
    const ul = nodo('ul');
    for (const i of items) ul.append(nodo('li', '', i));
    caja.append(ul);
    return caja;
  }

  function contenidoCorreo(c) {
    const caja = nodo('div', 'contenido');
    const dirs = (l) => lista_(l).join(', ');
    caja.append(campo('Para', dirs(c.para) || '—'));
    if (lista_(c.cc).length) caja.append(campo('CC', dirs(c.cc)));
    if (lista_(c.cco).length) caja.append(campo('CCO', dirs(c.cco)));
    caja.append(campo('Asunto', texto_(c.asunto) || '(sin asunto)'));
    caja.append(nodo('div', 'cuerpo-correo', texto_(c.cuerpo)));
    const adjuntos = Array.isArray(c.adjuntos) ? c.adjuntos : [];
    if (adjuntos.length) {
      caja.append(listaDe('adjuntos', adjuntos.map((a) => [texto_(a && a.nombre), tamano(a && a.bytes),
        carpeta(a && a.carpeta)].filter(Boolean).join(' · ')), 'Adjuntos'));
    }
    if (lista_(c.problemas).length) caja.append(listaDe('problemas', lista_(c.problemas)));
    if (lista_(c.avisos).length) caja.append(listaDe('avisos', lista_(c.avisos)));
    return caja;
  }

  function contenidoDrive(dr) {
    const caja = nodo('div', 'contenido');
    const nombre = (dr.archivo && texto_(dr.archivo.nombre)) || '';
    let frase;
    if (dr.op === 'subir') {
      const l = dr.local || {};
      const detalle = [tamano(l.bytes), carpeta(l.carpeta)].filter(Boolean).join(', ');
      frase = `Subir «${texto_(l.nombre)}»${detalle ? ` (${detalle})` : ''} a «${texto_(dr.carpeta_destino) || 'Mi unidad'}»`;
    } else if (dr.op === 'papelera') {
      const detalle = [dr.archivo && texto_(dr.archivo.tipo), dr.archivo && tamano(dr.archivo.tamano)].filter(Boolean).join(', ');
      frase = `Mover a la papelera de Drive «${nombre}»${detalle ? ` (${detalle})` : ''}. No se borra para siempre.`;
    } else {
      const c = dr.compartir || {};
      frase = c.publico === true
        ? `Compartir «${nombre}» con cualquiera que tenga el enlace (solo lectura)`
        : `Compartir «${nombre}» con ${texto_(c.con)} como ${NOMBRE_ROL[c.rol] || 'lector'}`;
    }
    caja.append(nodo('p', 'frase', frase));
    if (dr.paso === 2) caja.append(nodo('p', 'aviso-grande', 'Cualquiera con el enlace podrá verlo'));
    if (lista_(dr.problemas).length) caja.append(listaDe('problemas', lista_(dr.problemas)));
    return caja;
  }

  function bloqueCodigo(codigo) {
    const pre = nodo('pre', 'codigo');
    const lineas = codigo.split('\n');
    if (lineas.length > 1 && lineas[lineas.length - 1] === '') lineas.pop();
    const numeros = nodo('span', 'lineas', lineas.map((_l, i) => String(i + 1)).join('\n'));
    numeros.setAttribute('aria-hidden', 'true');
    const code = nodo('code', '', codigo); // COMPLETO: nunca se recorta al mostrarlo
    pre.append(numeros, code);
    return pre;
  }

  function contenidoScript(t) {
    const s = t.datos.script;
    const caja = nodo('div', 'contenido');
    if (texto_(s.explicacion)) caja.append(nodo('p', 'explicacion', s.explicacion));
    if (lista_(s.efectos).length) caja.append(listaDe('efectos', lista_(s.efectos), 'Qué hará'));
    if (lista_(s.requisitos).length) caja.append(listaDe('requisitos', lista_(s.requisitos), 'Necesita'));
    const hallazgos = (Array.isArray(s.hallazgos) ? s.hallazgos : []).map((h) => [
      numero(h && h.linea) !== null ? `línea ${h.linea}` : '', texto_(h && h.categoria), texto_(h && h.nivel),
      texto_(h && h.detalle)].filter(Boolean).join(' · '));
    const omitidos = numero(s.hallazgos_omitidos) || 0;
    if (omitidos > 0) hallazgos.push(`… y ${omitidos} hallazgos más`);
    if (hallazgos.length) {
      caja.append(listaDe('hallazgos', hallazgos, `Análisis ${s.metodo === 'heuristico' ? 'heurístico' : 'local'}`));
    }
    if (texto_(s.habilidad)) {
      caja.append(nodo('p', 'habilidad', `Habilidad «${s.habilidad}»${s.habilidad_cambiada === true
        ? ': el código cambió desde que la guardaste; revísalo' : ''}`));
    }
    caja.append(bloqueCodigo(texto_(s.codigo)));
    if (s.ejecutable !== true || s.nivel === 'critico') {
      caja.append(nodo('p', 'motivo', `No se puede ejecutar desde aquí${texto_(s.motivo_no_ejecutable)
        ? `: ${s.motivo_no_ejecutable}` : '.'}`));
    } else if (s.nivel === 'alto' && !t.cerrada) {
      const etiqueta = nodo('label', 'riesgo');
      const casilla = document.createElement('input');
      casilla.type = 'checkbox';
      casilla.checked = t.riesgoOk === true;
      casilla.addEventListener('change', () => {
        t.riesgoOk = casilla.checked === true;
        actualizarBotonesTarjeta(t);
      });
      t.casilla = casilla;
      etiqueta.append(casilla, nodo('span', '', 'Leí el código y entiendo el riesgo'));
      caja.append(etiqueta);
    }
    return caja;
  }

  function contenidoSalida(sa) {
    const caja = nodo('div', 'contenido');
    const partes = [`Código de salida: ${numero(sa.codigo_salida) === null ? '—' : sa.codigo_salida}`];
    const dur = numero(sa.duracion_s);
    if (dur !== null) partes.push(`${coma(dur)} s`);
    caja.append(nodo('p', 'frase', partes.join(' · ')));
    if (sa.tiempo_agotado === true) caja.append(nodo('p', 'aviso', 'Terminado por tiempo límite'));
    if (texto_(sa.stdout)) {
      caja.append(nodo('p', 'subtitulo', 'Salida'), nodo('pre', 'salida', sa.stdout));
    }
    if (texto_(sa.stderr)) {
      caja.append(nodo('p', 'subtitulo', 'Errores'), nodo('pre', 'salida error', sa.stderr));
    }
    if (sa.recortada === true) caja.append(nodo('p', 'nota', '… (salida recortada)'));
    if (texto_(sa.carpeta)) caja.append(nodo('p', 'nota', `Carpeta: ${sa.carpeta}`));
    return caja;
  }

  /** Formulario de edición del correo (textarea e input con maxlength = topes de R.A.M.S.). */
  function formularioCorreo(t) {
    const c = t.datos.correo;
    const caja = nodo('div', 'contenido edicion');
    const original = {
      para: lista_(c.para).join(', '), cc: lista_(c.cc).join(', '), cco: lista_(c.cco).join(', '),
      asunto: texto_(c.asunto), cuerpo: texto_(c.cuerpo),
      adjuntos: (Array.isArray(c.adjuntos) ? c.adjuntos : []).map((a) => texto_(a && a.nombre)).join(', '),
    };
    const entradas = {};
    const ETIQUETAS = ETIQUETAS_CAMPOS;
    for (const clave of ['para', 'cc', 'cco', 'asunto', 'cuerpo', 'adjuntos']) {
      const etiqueta = nodo('label', 'campo-edicion');
      const entrada = document.createElement(clave === 'cuerpo' ? 'textarea' : 'input');
      if (clave !== 'cuerpo') entrada.type = 'text';
      else entrada.rows = 8;
      entrada.maxLength = TOPES_CAMPOS[clave];
      entrada.setAttribute('maxlength', String(TOPES_CAMPOS[clave]));
      entrada.spellcheck = clave === 'cuerpo';
      entrada.value = original[clave];
      entrada.dataset.campo = clave;
      etiqueta.append(nodo('span', 'etiqueta', ETIQUETAS[clave]), entrada);
      caja.append(etiqueta);
      entradas[clave] = entrada;
    }
    t.edicion = { original, entradas };
    return caja;
  }

  function fijarResultadoTarjeta(t, txt) {
    t.textoResultado = txt;
    if (t.resultado) {
      t.resultado.textContent = txt;
      t.resultado.hidden = !txt;
    }
  }

  const esAprobar = (nombre) => nombre === 'aprobar' || nombre === 'aprobar_publico';
  const armando = (t) => performance.now() < t.armadoHasta;

  /**
   * Los botones de aprobar de `t` no se pueden pulsar durante ARMADO_MS: se acaban de pintar donde
   * quizá estaba el puntero (p. ej. «Sí, hacerlo público» justo donde estaba «Compartir»).
   */
  function armar(t) {
    t.armadoHasta = performance.now() + ARMADO_MS;
    setTimeout(() => { if (!t.cerrada) actualizarBotonesTarjeta(t); }, ARMADO_MS + 20);
  }

  /** Activa o bloquea los controles según el estado (enviando, esperando, conexión, contenido). */
  function actualizarBotonesTarjeta(t) {
    const libre = !t.cerrada && !t.enviando && !t.esperando && conectada !== false;
    const ofrecidos = Array.isArray(t.datos.botones) ? t.datos.botones : [];
    for (const b of t.botones) {
      const local = b.dataset.local === '1';  // copiar, cancelar: no dependen de R.A.M.S.
      const nombre = b.dataset.boton;
      // Un botón que manda una decisión solo vale si la tarjeta la ofrece AHORA (una «en curso», ninguna).
      const ofrecido = !nombre || ofrecidos.includes(nombre);
      b.disabled = local ? t.cerrada : !libre || !ofrecido || (esAprobar(nombre) && (armando(t) || !puedeAprobar(t)));
    }
    for (const e of t.entradas) e.disabled = !libre;
  }

  function boton(t, texto, clase, accion, { local = false, nombre = '' } = {}) {
    const b = nodo('button', clase, texto);
    b.type = 'button';
    b.dataset.boton = nombre;
    b.dataset.local = local ? '1' : '0';
    b.addEventListener('click', (e) => {
      // El 2.º clic de un doble clic nunca cuenta: caería sobre lo que el 1.º acaba de hacer aparecer.
      if (e && e.detail > 1) return undefined;
      // Activación por teclado justo después de recibir el foco: se ignora (como las confirmaciones).
      if (e && e.detail === 0 && recienEnfocada()) return undefined;
      if (esAprobar(nombre) && armando(t)) return undefined; // recién pintado: aún no
      return accion();
    });
    t.botones.push(b);
    return b;
  }

  function acciones(t) {
    const d = t.datos;
    const caja = nodo('div', 'botones-tarjeta');
    if (t.editando) {
      caja.append(boton(t, 'Guardar cambios', 'aprobar', () => guardarEdicion(t), { nombre: 'editar' }),
        boton(t, 'Cancelar', 'secundario', () => { t.editando = false; pintarTarjeta(t); }, { local: true }));
      t.cajaAcciones = caja; // «Quitar caracteres no válidos» se añade aquí solo si hace falta
      return caja;
    }
    if (t.nombrando) {
      const entrada = document.createElement('input');
      entrada.type = 'text';
      entrada.maxLength = MAX_NOMBRE_HABILIDAD;
      entrada.setAttribute('maxlength', String(MAX_NOMBRE_HABILIDAD));
      entrada.placeholder = 'Nombre de la habilidad';
      entrada.setAttribute('aria-label', 'Nombre de la habilidad');
      t.entradas.push(entrada);
      t.entradaNombre = entrada;
      caja.append(entrada,
        boton(t, 'Guardar', 'aprobar', () => guardarHabilidad(t), { nombre: 'guardar' }),
        boton(t, 'Cancelar', 'secundario', () => { t.nombrando = false; pintarTarjeta(t); }, { local: true }));
      return caja;
    }
    for (const nombre of Array.isArray(d.botones) ? d.botones : []) {
      const texto = etiquetaBoton(d, nombre, t);
      if (!texto) continue;
      if (nombre === 'aprobar' && d.clase === 'script' && (d.script.ejecutable !== true || d.script.nivel === 'critico')) continue;
      if (nombre === 'copiar' && d.clase !== 'script') continue;
      if (nombre === 'editar' && d.clase !== 'correo') continue;
      let accion;
      if (nombre === 'editar') accion = () => { t.editando = true; pintarTarjeta(t); };
      else if (nombre === 'guardar') {
        accion = () => {
          t.nombrando = true;
          pintarTarjeta(t);
          if (t.entradaNombre && typeof t.entradaNombre.focus === 'function') t.entradaNombre.focus();
        };
      } else if (nombre === 'copiar') {
        accion = () => {
          api.copiarCodigo(d.id); // el main copia el código que envió R.A.M.S., nunca texto del renderer
          t.copiado = true;
          const b = t.botones.find((x) => x.dataset.boton === 'copiar');
          if (b) b.textContent = 'Copiado';
        };
      } else accion = () => decidir(t, nombre);
      const clase = nombre === 'aprobar' || nombre === 'aprobar_publico' ? 'aprobar'
        : (nombre === 'descartar' ? 'rechazar' : 'secundario');
      caja.append(boton(t, texto, clase, accion, { local: nombre === 'copiar', nombre }));
    }
    return caja;
  }

  /** Repinta la burbuja entera (mismo elemento) con el estado actual. */
  function pintarTarjeta(t) {
    const d = t.datos;
    t.botones = [];
    t.entradas = [];
    t.casilla = null;
    t.entradaNombre = null;
    t.cajaAcciones = null;
    t.botonLimpiar = null;
    const cabecera = nodo('header', 'cabecera-tarjeta');
    cabecera.append(nodo('span', 'tipo', encabezado(d)));
    if (d.clase === 'script') {
      const nivel = NOMBRE_NIVEL[d.script.nivel] ? d.script.nivel : 'critico';
      cabecera.append(nodo('span', `nivel ${nivel}`, NOMBRE_NIVEL[nivel]));
    }
    const partes = [cabecera];
    if (texto_(d.titulo)) partes.push(nodo('p', 'titulo-tarjeta', d.titulo));
    if (texto_(d.resumen)) partes.push(nodo('p', 'resumen', d.resumen));
    if (texto_(d.aviso)) partes.push(nodo('p', 'aviso', d.aviso));
    if (t.editando && d.clase === 'correo') partes.push(formularioCorreo(t));
    else if (d.clase === 'correo') partes.push(contenidoCorreo(d.correo));
    else if (d.clase === 'drive') partes.push(contenidoDrive(d.drive));
    else if (d.clase === 'script') partes.push(contenidoScript(t));
    else partes.push(contenidoSalida(d.salida));
    if (!t.cerrada) partes.push(acciones(t));
    t.resultado = nodo('p', 'resultado', t.textoResultado || '');
    t.resultado.hidden = !t.textoResultado;
    partes.push(t.resultado, t.hora);
    t.el.replaceChildren(...partes);
    if (t.edicion && !t.editando) t.edicion = null;
    for (const e of Object.values(t.edicion ? t.edicion.entradas : {})) t.entradas.push(e);
    actualizarBotonesTarjeta(t);
  }

  async function decidir(t, decision, extra) {
    if (t.cerrada || t.enviando || t.esperando) return;
    if (conectada === false) {
      fijarResultadoTarjeta(t, SIN_CONEXION_TARJETA);
      return;
    }
    const d = t.datos;
    t.enviando = true;
    actualizarBotonesTarjeta(t);
    fijarResultadoTarjeta(t, decision === 'aprobar' && d.clase === 'script' ? 'Ejecutando…' : EN_CURSO[decision] || 'Enviando…');
    t.el.scrollIntoView({ block: 'nearest' });
    let ok = false;
    try {
      // La versión que se VE: R.A.M.S. ignora el clic si mientras tanto llegó otra (o sigue en curso).
      ok = (await api.decidirTarjeta(d.id, d.sha256, decision, { ...(extra || {}), version: d.version })) === true;
    } catch {
      ok = false;
    }
    t.enviando = false;
    if (ok) {
      t.esperando = true; // bloqueada hasta la versión nueva o el cierre
      t.ultimaDecision = decision;
      actualizarBotonesTarjeta(t);
      return;
    }
    if (t.cerrada) return;
    // Los datos ya se validaron aquí (guardarEdicion, guardarHabilidad): un false es que no salió.
    fijarResultadoTarjeta(t, SIN_CONEXION_TARJETA);
    actualizarBotonesTarjeta(t);
  }

  const controlesDe = (clave) => (clave === 'cuerpo' ? RE_CONTROLES_CUERPO : RE_CONTROLES);

  /**
   * Quita los caracteres de control de un campo de edición (solo al pulsar «Quitar caracteres no
   * válidos», y a la vista en el propio campo): en el cuerpo, \r\n, \r, \v y \f pasan a \n; en los de
   * una línea, cualquier salto o tabulador pasa a un espacio. El resto de controles se quita.
   */
  function sinControles(clave, valor) {
    // eslint-disable-next-line no-control-regex
    if (clave === 'cuerpo') return valor.replace(/\r\n?|[\u000b\u000c]/g, '\n').replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '');
    // eslint-disable-next-line no-control-regex
    return valor.replace(/[\t\n\r\u000b\u000c]+/g, ' ').replace(/[\u0000-\u001f\u007f]/g, '');
  }

  function limpiarEdicion(t) {
    if (!t.edicion) return;
    for (const [clave, entrada] of Object.entries(t.edicion.entradas)) {
      const antes = String(entrada.value ?? '');
      const despues = sinControles(clave, antes);
      if (despues !== antes) entrada.value = despues;
    }
    if (t.botonLimpiar) t.botonLimpiar.hidden = true;
    fijarResultadoTarjeta(t, 'Quité los caracteres no válidos: revisa el texto y pulsa «Guardar cambios».');
  }

  /** «Guardar cambios»: solo los campos que cambiaron; nunca se recortan (topes = maxlength). */
  function guardarEdicion(t) {
    if (!t.edicion) return;
    const campos = {};
    // Con problemas en el borrador, los destinatarios y adjuntos van SIEMPRE tal como se ven en el formulario:
    // lo que R.A.M.S. no pudo resolver (p. ej. un alias desconocido) no sale en él y sigue bloqueando hasta
    // que se edite su campo; así «Guardar cambios» deja el correo como se ve (sin eso, quedaba atascado).
    const problemas = Array.isArray(t.datos.correo && t.datos.correo.problemas) && t.datos.correo.problemas.length > 0;
    for (const [clave, entrada] of Object.entries(t.edicion.entradas)) {
      const valor = String(entrada.value ?? '');
      if (valor !== t.edicion.original[clave] || (problemas && CAMPOS_LISTA.includes(clave))) campos[clave] = valor;
    }
    if (Object.keys(campos).length === 0) {
      t.editando = false;
      pintarTarjeta(t);
      return;
    }
    if (Object.entries(campos).some(([clave, valor]) => valor.length > TOPES_CAMPOS[clave])) {
      fijarResultadoTarjeta(t, 'Es demasiado largo para guardarlo: acórtalo.');
      return;
    }
    // Un tabulador en el asunto o un salto de línea de Word (\v) en el cuerpo no pasan la validación
    // de R.A.M.S.: se dice dónde están y se ofrece quitarlos (nunca se quitan en silencio).
    const conControles = Object.keys(campos).filter((clave) => controlesDe(clave).test(campos[clave]));
    if (conControles.length) {
      fijarResultadoTarjeta(t, `Hay caracteres invisibles no válidos (tabuladores o saltos de línea pegados) en ${
        conControles.map((c) => ETIQUETAS_CAMPOS[c]).join(', ')}. Pulsa «Quitar caracteres no válidos» y revisa antes de guardar.`);
      if (!t.botonLimpiar && t.cajaAcciones) {
        t.botonLimpiar = boton(t, 'Quitar caracteres no válidos', 'secundario', () => limpiarEdicion(t), { local: true });
        t.cajaAcciones.append(t.botonLimpiar);
      }
      if (t.botonLimpiar) t.botonLimpiar.hidden = false;
      actualizarBotonesTarjeta(t);
      return;
    }
    decidir(t, 'editar', { campos });
  }

  /** «Guardar como habilidad»: el nombre se pide en línea (≤ 40), nunca con prompt(). */
  function guardarHabilidad(t) {
    const nombre = String((t.entradaNombre && t.entradaNombre.value) || '').trim();
    if (!nombre || nombre.length > MAX_NOMBRE_HABILIDAD) {
      fijarResultadoTarjeta(t, `Ponle un nombre (máximo ${MAX_NOMBRE_HABILIDAD} caracteres).`);
      return;
    }
    if (RE_CONTROLES.test(nombre) || RE_SUSTITUTO_SUELTO.test(nombre)) {
      fijarResultadoTarjeta(t, 'El nombre tiene caracteres no válidos (tabuladores o saltos de línea): quítalos.');
      return;
    }
    decidir(t, 'guardar', { nombre });
  }

  function crearBurbujaTarjeta(m) {
    const el = document.createElement('article');
    el.className = `msg tarjeta ${m.clase}`;
    el.dataset.id = m.id;
    el.setAttribute('aria-label', 'Tarjeta para aprobar');
    const t = {
      el, datos: m, hora: hora(), cerrada: false, enviando: false, esperando: false, editando: false,
      nombrando: false, riesgoOk: false, copiado: false, textoResultado: '', botones: [], entradas: [],
      resultado: null, casilla: null, edicion: null, entradaNombre: null, ultimaDecision: null,
      armadoHasta: 0, cajaAcciones: null, botonLimpiar: null,
    };
    // Una 2.ª confirmación de compartir en público nunca se puede pulsar en cuanto aparece.
    if (lista_(m.botones).includes('aprobar_publico')) armar(t);
    pintarTarjeta(t);
    agregar(el, true); // una tarjeta para aprobar siempre a la vista
    return t;
  }

  function cerrarTarjetaAprobable(t, m) {
    t.cerrada = true;
    t.enviando = false;
    t.esperando = false;
    t.editando = false;
    t.nombrando = false;
    t.el.classList.add('cerrada');
    let r;
    if (m.caducada === true) r = 'Ya no está vigente.';
    else if (m.resultado === 'denegado' || m.resultado === 'fallo') r = `No se pudo${texto_(m.motivo) ? `: ${m.motivo}` : '.'}`;
    else if (m.resultado === 'descartado' && t.datos.clase === 'salida') r = 'Cerrado.';
    else r = `${RESULTADO[m.resultado] || 'Cerrada'}${texto_(m.motivo) ? ` · ${m.motivo}` : ''}`;
    t.textoResultado = r;
    pintarTarjeta(t);
  }

  /** `chat:tarjeta` del main (ya validada por lib/mapeo.js; aquí solo se comprueba la forma). */
  function mostrarTarjeta(m) {
    if (!m || typeof m !== 'object' || typeof m.id !== 'string' || !RE_ID.test(m.id)) return;
    const actual = tarjetas.get(m.id);
    if (m.estado === 'cerrada') {
      if (actual && !actual.cerrada) cerrarTarjetaAprobable(actual, m);
      return;
    }
    if (!CLASES_TARJETA.includes(m.clase) || !m[m.clase] || typeof m[m.clase] !== 'object') return;
    if (typeof m.sha256 !== 'string' || !Number.isInteger(m.version)) return;
    if (actual && !actual.cerrada) {
      if (m.version < actual.datos.version) return; // versión vieja: se ignora
      const misma = m.version === actual.datos.version;
      const trasClic = actual.ultimaDecision !== null;
      // La respuesta a «Guardar como habilidad» llega con la misma versión (§14.4): sale del modo nombre.
      if (actual.ultimaDecision === 'guardar') actual.nombrando = false;
      actual.ultimaDecision = null;
      actual.datos = m;
      actual.enviando = false;
      actual.esperando = false;
      actual.textoResultado = '';
      // Repintada en respuesta a un clic o con otro contenido: lo que quede bajo el puntero no se puede
      // aprobar al instante (un doble clic nunca aprueba lo que su primer clic hizo aparecer).
      if (trasClic || !misma) armar(actual);
      if (misma && (actual.editando || actual.nombrando)) {
        // Si la tarjeta ya no ofrece guardar (está «en curso»), se dice qué hace R.A.M.S.
        const sigue = lista_(m.botones).includes(actual.editando ? 'editar' : 'guardar');
        actual.textoResultado = conectada === false ? SIN_CONEXION_TARJETA : (sigue ? '' : texto_(m.aviso));
        fijarResultadoTarjeta(actual, actual.textoResultado);
        actualizarBotonesTarjeta(actual); // no se pierde lo que el usuario estaba escribiendo
        return;
      }
      if (!misma) {
        actual.editando = false;
        actual.nombrando = false;
        actual.riesgoOk = false;
        actual.copiado = false;
      }
      if (conectada === false) actual.textoResultado = SIN_CONEXION_TARJETA;
      pintarTarjeta(actual);
      if (cercaDelFinal()) actual.el.scrollIntoView({ block: 'nearest' });
      return;
    }
    const t = crearBurbujaTarjeta(m); // nueva, o R.A.M.S. dice que una ya cerrada vuelve a estar vigente
    tarjetas.set(m.id, t);
    if (conectada === false) {
      fijarResultadoTarjeta(t, SIN_CONEXION_TARJETA);
      actualizarBotonesTarjeta(t);
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
      // Las tarjetas igual: bloqueadas hasta que la instantánea las vuelva a traer (o las cierre).
      for (const t of tarjetas.values()) {
        if (t.cerrada) continue;
        t.esperando = true;
        actualizarBotonesTarjeta(t);
        if (!t.enviando) fijarResultadoTarjeta(t, SIN_CONEXION_TARJETA);
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

  if (!api || typeof api.version !== 'number' || api.version < 1) {
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
  // Versión 2 (v0.2.2): tarjetas aprobables. Con un preload de la versión 1 simplemente no llegan.
  if (api.version >= 2 && typeof api.onTarjeta === 'function') api.onTarjeta(mostrarTarjeta);

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
