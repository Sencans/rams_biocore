'use strict';
/**
 * Ventana OCULTA de Puter de Amadeus Desktop (COMPANION.md §C2.15). Carga puter.js
 * (https://js.puter.com/v2/) y atiende los pedidos de texto que R.A.M.S. le hace llegar por el
 * main (`window.amadeusPuter`, preload-puter.js) con `puter.ai.chat`.
 *
 * - Solo carga puter.js si el usuario conectó Puter alguna vez (marca propia en localStorage que
 *   pone la ventanita de inicio de sesión): sin eso, Amadeus Desktop no hace ninguna petición a Puter.
 * - NUNCA inicia sesión ni abre nada: sin sesión responde «sin sesión» y no llama a puter.ai.chat
 *   (que, sin token, abriría el diálogo de Puter). El main además deniega cualquier ventana nueva.
 * - Cada llamada lleva un AbortController con el plazo que manda el main (38 s).
 * - Informa `{cargado, sesion, error}` al cargar, al cambiar y tras cada fallo, y lo reenvía cuando
 *   el main se lo pide (`puter:refrescar`): la sesión que vale es la que ve esta página.
 *
 * Se carga como script clásico en el navegador; en Node (tests) exporta sus funciones.
 */
(function preparar(raiz, fabrica) {
  const api = fabrica();
  if (typeof module === 'object' && module && module.exports) module.exports = api;
  else api.iniciar(raiz);
}(typeof window !== 'undefined' ? window : globalThis, function fabrica() {
  const URL_PUTER_JS = 'https://js.puter.com/v2/';
  const MARCA = 'amadeus.puter.conectado';
  const REINTENTOS_MS = [30000, 60000, 120000, 300000];
  const PLAZO_MODELOS_MS = 15000;
  const PLAZO_DEFECTO_MS = 38000;
  const RECHAZOS = ['content_filter', 'safety', 'refusal', 'blocked'];

  /** Texto de un ChatResponse (`message.content` en texto o en bloques; otras formas por si acaso). */
  function extraerTexto(res) {
    if (typeof res === 'string') return res;
    if (!res || typeof res !== 'object') return '';
    const de = (c) => {
      if (typeof c === 'string') return c;
      if (!Array.isArray(c)) return null;
      return c.map((b) => {
        if (typeof b === 'string') return b;
        if (b && typeof b === 'object' && (b.type === 'text' || b.type === undefined) && typeof b.text === 'string') return b.text;
        return ''; // razonamiento, herramientas, imágenes…
      }).join('');
    };
    const candidatos = [
      res.message && typeof res.message === 'object' ? res.message.content : undefined,
      res.content,
      Array.isArray(res.choices) && res.choices[0] && res.choices[0].message ? res.choices[0].message.content : undefined,
    ];
    for (const c of candidatos) {
      const t = de(c);
      if (t !== null) return t;
    }
    return typeof res.text === 'string' ? res.text : '';
  }

  /** ¿La respuesta vino vacía por un filtro del proveedor? */
  function respuestaRechazada(res) {
    if (!res || typeof res !== 'object') return false;
    const fin = typeof res.finish_reason === 'string' ? res.finish_reason.toLowerCase() : '';
    return RECHAZOS.includes(fin) || !!(res.message && typeof res.message === 'object' && res.message.refusal);
  }

  /** `{code, status, message, name}` de lo que rechace puter.js (objeto, Error, XHR o texto). */
  function describirFallo(err) {
    if (err === null || err === undefined) return {};
    if (typeof err === 'string') return { message: err };
    if (typeof err !== 'object') return {};
    // Error de red: puter.js rechaza con el propio XMLHttpRequest.
    if (typeof err.readyState === 'number' && typeof err.status === 'number' && typeof err.open === 'function') {
      return { code: err.status === 0 ? 'network_error' : `http_${err.status}`, status: err.status };
    }
    const anidado = err.error && typeof err.error === 'object' ? err.error : {};
    const elegir = (...v) => v.find((x) => (typeof x === 'string' && x !== '') || (typeof x === 'number' && Number.isFinite(x)));
    const out = {
      code: elegir(err.code, anidado.code, typeof err.error === 'string' ? err.error : undefined, anidado.type),
      status: elegir(err.status, err.statusCode, anidado.status, anidado.statusCode),
      message: elegir(err.message, err.msg, anidado.message, anidado.msg),
      name: elegir(err.name),
    };
    for (const k of Object.keys(out)) if (out[k] === undefined) delete out[k];
    return out;
  }

  /** ¿`modelo` aparece en `puter.ai.listModels()`? (id, `proveedor/id` o alias; sin sufijo `:free`…). */
  function modeloDisponible(lista, modelo) {
    if (!Array.isArray(lista) || typeof modelo !== 'string') return false;
    const base = modelo.replace(/:(free|flex|priority)$/, '');
    const corto = base.includes('/') ? base.slice(base.indexOf('/') + 1) : base;
    return lista.some((m) => {
      if (!m || typeof m !== 'object') return false;
      const ids = [m.id, ...(Array.isArray(m.aliases) ? m.aliases : [])].filter((x) => typeof x === 'string');
      if (typeof m.provider === 'string' && typeof m.id === 'string') ids.push(`${m.provider}/${m.id}`);
      return ids.some((x) => x === modelo || x === base || x === corto || x.endsWith(`/${corto}`));
    });
  }

  const motivoAborto = () => ({ name: 'AbortError', code: 'aborted' });

  /** La promesa, o un rechazo en cuanto se aborte `senal` (por si puter.js no la respeta). */
  function abortable(promesa, senal) {
    return new Promise((resolver, rechazar) => {
      if (senal.aborted) {
        rechazar(motivoAborto());
        return;
      }
      const alAbortar = () => rechazar(motivoAborto());
      senal.addEventListener('abort', alAbortar, { once: true });
      Promise.resolve(promesa).then((v) => {
        senal.removeEventListener('abort', alAbortar);
        resolver(v);
      }, (err) => {
        senal.removeEventListener('abort', alAbortar);
        rechazar(err);
      });
    });
  }

  /**
   * Trabajador. `e`: {puter(): objeto global de puter.js o undefined, puente: window.amadeusPuter,
   * documento, leerMarca(): boolean, temporizador(fn, ms) → cancelar()}.
   */
  function crearTrabajador(e) {
    const st = { cargado: false, sesion: false, error: null, modelo: null, comprobado: false, intento: 0, script: null };
    const enCurso = new Map(); // id → AbortController
    let ultimo = null;

    const lib = () => {
      const p = e.puter();
      return p && typeof p === 'object' && p.ai && p.auth ? p : null;
    };

    function publicar() {
      const datos = { cargado: st.cargado, sesion: st.cargado && st.sesion, error: st.error };
      const firma = JSON.stringify(datos);
      if (firma === ultimo) return;
      ultimo = firma;
      e.puente.estado(datos);
    }

    async function sesionActiva() {
      const p = lib();
      if (!p || typeof p.auth.isSignedIn !== 'function') return false;
      try {
        const r = p.auth.isSignedIn();
        return (r && typeof r.then === 'function' ? await r : r) === true;
      } catch {
        return false;
      }
    }

    /** Relee la sesión, publica si cambió y devuelve si hay sesión. */
    async function refrescarSesion() {
      const s = await sesionActiva();
      if (s !== st.sesion) {
        st.sesion = s;
        publicar();
      }
      if (s) comprobarModelo();
      return s;
    }

    function plazo(promesa, ms) {
      let cancelar = () => {};
      const vencida = new Promise((_r, rechazar) => {
        cancelar = e.temporizador(() => rechazar({ code: 'request_timeout' }), ms);
      });
      return Promise.race([promesa, vencida]).finally(() => cancelar());
    }

    /** Avisa (una vez por sesión) si el modelo por defecto no aparece en puter.ai.listModels(). */
    async function comprobarModelo() {
      const p = lib();
      if (st.comprobado || !st.modelo || !st.cargado || !st.sesion || !p || typeof p.ai.listModels !== 'function') return;
      st.comprobado = true;
      let lista;
      try {
        lista = await plazo(p.ai.listModels(), PLAZO_MODELOS_MS);
      } catch {
        return; // sin red o sin respuesta: no es motivo para avisar
      }
      if (Array.isArray(lista) && lista.length > 0 && !modeloDisponible(lista, st.modelo)) {
        st.error = 'modelo_desconocido';
        publicar();
      }
    }

    async function alCargar() {
      st.cargado = true;
      st.error = null;
      st.intento = 0;
      st.sesion = await sesionActiva();
      publicar();
      if (st.sesion) comprobarModelo();
    }

    function alFallarCarga() {
      if (st.script) {
        st.script.remove();
        st.script = null;
      }
      st.cargado = false;
      st.sesion = false;
      st.error = 'carga_fallida';
      publicar();
      const espera = REINTENTOS_MS[Math.min(st.intento, REINTENTOS_MS.length - 1)];
      st.intento += 1;
      e.temporizador(cargarPuter, espera);
    }

    function cargarPuter() {
      if (lib()) {
        alCargar();
        return;
      }
      const s = e.documento.createElement('script');
      s.src = URL_PUTER_JS;
      s.async = true;
      s.addEventListener('load', () => (lib() ? alCargar() : alFallarCarga()));
      s.addEventListener('error', () => alFallarCarga());
      st.script = s;
      e.documento.head.appendChild(s);
    }

    async function alPedido(pd) {
      if (!pd || typeof pd !== 'object' || typeof pd.id !== 'string') return;
      const { id } = pd;
      const p = lib();
      if (!p || !(await refrescarSesion())) {
        e.puente.responder({ id, ok: false, fallo: { sinSesion: true } });
        return;
      }
      const ctrl = new AbortController();
      enCurso.set(id, ctrl);
      const cancelar = e.temporizador(() => ctrl.abort(), pd.timeoutMs > 0 ? pd.timeoutMs : PLAZO_DEFECTO_MS);
      const opciones = {
        model: pd.modelo,
        max_tokens: pd.maxTokens,
        temperature: pd.temperatura,
        stream: false,
        normalize: true, // formato OpenAI: message.content en texto
        signal: ctrl.signal,
      };
      if (typeof pd.esfuerzo === 'string') opciones.reasoning_effort = pd.esfuerzo;
      try {
        const res = await abortable(p.ai.chat(pd.mensajes, false, opciones), ctrl.signal);
        const texto = extraerTexto(res);
        if (texto.trim()) e.puente.responder({ id, ok: true, texto, modelo: pd.modelo });
        else e.puente.responder({ id, ok: false, fallo: { code: respuestaRechazada(res) ? 'content_filter' : 'respuesta_vacia' } });
      } catch (err) {
        const abortado = ctrl.signal.aborted;
        const sinSesion = !(await refrescarSesion());
        e.puente.responder({ id, ok: false, fallo: { ...describirFallo(err), abortado, sinSesion } });
      } finally {
        cancelar();
        enCurso.delete(id);
      }
    }

    function alCancelar(id) {
      const ctrl = typeof id === 'string' ? enCurso.get(id) : null;
      if (ctrl) ctrl.abort();
    }

    function alInfo(i) {
      if (!i || typeof i !== 'object' || typeof i.modelo !== 'string') return;
      st.modelo = i.modelo;
      comprobarModelo();
    }

    /**
     * El main recibió un fallo de autenticación (401…) mientras esta página aún veía sesión: relee
     * la sesión y vuelve a publicar el estado aunque no haya cambiado, para que el main lo tome de
     * aquí (la página es la fuente de verdad de la sesión).
     */
    async function alRefrescar() {
      ultimo = null;
      if (st.cargado) await refrescarSesion(); // publica si cambió
      publicar(); // y, si no cambió, también (ultimo sigue en null)
    }

    function arrancar() {
      e.puente.onPedido((pd) => { alPedido(pd); });
      e.puente.onCancelar(alCancelar);
      e.puente.onInfo(alInfo);
      if (typeof e.puente.onRefrescar === 'function') e.puente.onRefrescar(() => { alRefrescar(); });
      publicar();
      if (e.leerMarca()) cargarPuter();
    }

    return {
      arrancar, alPedido, alCancelar, alInfo, alRefrescar,
      estado: () => ({ ...st, script: !!st.script }), enCurso: () => enCurso.size,
    };
  }

  function iniciar(g) {
    const puente = g.amadeusPuter;
    if (!puente || puente.version !== 1) return null;
    const t = crearTrabajador({
      puter: () => g.puter,
      puente,
      documento: g.document,
      leerMarca: () => {
        try {
          return g.localStorage.getItem(MARCA) === '1';
        } catch {
          return false;
        }
      },
      temporizador: (fn, ms) => {
        const h = g.setTimeout(fn, ms);
        return () => g.clearTimeout(h);
      },
    });
    t.arrancar();
    return t;
  }

  return {
    URL_PUTER_JS, MARCA, REINTENTOS_MS, extraerTexto, respuestaRechazada, describirFallo, modeloDisponible,
    abortable, crearTrabajador, iniciar,
  };
}));
