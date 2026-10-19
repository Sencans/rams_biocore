'use strict';
/**
 * Respaldo Puter del Área de Broca, lado Amadeus Desktop (COMPANION.md §C2.15). Módulo puro: no
 * importa Electron; todo efecto pasa por los `puertos` inyectados (como lib/mapeo.js).
 *
 * R.A.M.S. usa Gemini y, si falla (cuota, red, tope diario), le pide el texto a esta app, que lo
 * genera con Puter.js (https://docs.puter.com) en una ventana oculta con la cuenta de Puter del
 * usuario (modelo «user-pays»: el uso se descuenta de SU saldo; no hay claves de API).
 *
 * Contrato por el WebSocket `/escritorio` (sobre `{tipo, datos}`):
 *   app → R.A.M.S.  puter.estado  {listo, sesion, modelo, error}     tras el `hola` y en cada cambio
 *   R.A.M.S. → app  llm.pedir     {id, modelo, system, mensajes:[{rol, texto}], max_tokens, temperatura}
 *   app → R.A.M.S.  llm.respuesta {id, ok, texto, error, modelo, ms}
 * `listo` = puter.js cargado Y sesión iniciada. `error` de llm.respuesta ∈ ERRORES; el de
 * puter.estado es un código corto (`[a-z_]{1,24}`, lo exige R.A.M.S.) de ERRORES_ESTADO o `null`.
 *
 * Límites: system ≤ 60 000 caracteres, ≤ 40 mensajes de ≤ 20 000; como mucho 2 pedidos a la vez;
 * la ventana aborta `puter.ai.chat` a los 38 s y este relevo responde «timeout» a los 42 s si la
 * ventana no contestó (siempre hay respuesta en ≤ 45 s; R.A.M.S. espera ≤ 40 s). La respuesta va
 * recortada a 32 000 caracteres y ≤ 60 KiB de JSON (R.A.M.S. acepta ≤ 64 KiB para llm.respuesta).
 *
 * Privacidad: el prompt, los mensajes y las respuestas nunca se registran; de un fallo solo se
 * registra su clasificación.
 *
 * Puertos de `crearRelevoPuter(puertos, opciones)`:
 *   enviarWs(obj, maxBytes?): boolean        true si salió por el WebSocket
 *   enviarVentana(canal, datos): boolean     IPC a la ventana oculta de Puter (false si no está lista)
 *   temporizador(fn, ms) → cancelar()        setTimeout inyectable
 *   ahora(): number                          reloj inyectable (ms)
 *   alCambiarEstado?()                       p. ej. reconstruir el menú de la bandeja
 *   registrar?(texto)                        log sin contenido del usuario
 */

/** Origen propio de las páginas de Puter (servido por el main con `protocol.handle`, sin red). */
const ORIGEN_PUTER = 'http://amadeus-puter.localhost';
/**
 * Modelo por defecto. Verificado (17-10-2026) en el listado oficial
 * https://developer.puter.com/ai/models/, en su ficha
 * https://developer.puter.com/ai/google/gemini-3.5-flash-lite/ y en los ejemplos de
 * https://docs.puter.com/AI/chat/ (`gemini-3.5-flash-lite`). Es el más rápido de los candidatos
 * medidos en las fichas: 5,79 s al primer token y ~411 tokens/s, frente a 16,29 s de
 * `google/gemini-3.8-flash`, 90 s de `openai/gpt-6-luna` y 235 s de `anthropic/claude-haiku-5-5`
 * (cifras de Puter, con razonamiento por defecto). Es de la familia Gemini para la que está
 * ajustado el prompt de Amadeus (persona de Kurisu, etiquetas [EXP]/[MOTION]/[TONE]) y ya está en
 * la cadena de respaldo de Gemini de R.A.M.S. (`GEMINI_FALLBACK_MODELS`), así que su español está
 * probado con ese prompt. Con la cuenta de Puter no comparte la cuota de la clave de Gemini.
 * Alternativas (AMADEUS_PUTER_MODELO aquí o PUTER_MODEL en R.A.M.S.): `google/gemini-3.8-flash`
 * (el mismo modelo que el primario de R.A.M.S., más lento) o `anthropic/claude-haiku-5-5`
 * (otro proveedor, útil si cae Google).
 */
const MODELO_DEFECTO = 'google/gemini-3.5-flash-lite';
const RE_ID_PEDIDO = /^[0-9a-f]{16,32}$/;
/**
 * El mismo patrón que `_MODELO_PUTER_RE` de interface/escritorio_link.py y `_RE_MODELO_PUTER`
 * (`PUTER_MODEL`) de core/config.py: empieza por letra o cifra y tiene 1-100 caracteres de
 * `[A-Za-z0-9_.:/-]`. Si la app aceptara más, R.A.M.S. convertiría en `null` el modelo que se le
 * anuncia en `puter.estado` y el que se le devuelve en `llm.respuesta`.
 */
const RE_MODELO = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}$/;
const ERRORES = Object.freeze(['sin_sesion', 'timeout', 'cuota', 'red', 'rechazado', 'otro']);
const ERRORES_ESTADO = Object.freeze(['deshabilitado', 'carga_fallida', 'modelo_desconocido', 'ventana_caida']);
/** `reasoning_effort` de puter.ai.chat (opcional, AMADEUS_PUTER_ESFUERZO). */
const ESFUERZOS = Object.freeze(['none', 'minimal', 'low', 'medium', 'high']);
const ROLES = Object.freeze({ usuario: 'user', modelo: 'assistant' });

const MAX_SYSTEM = 60000;
const MAX_MENSAJES = 40;
const MAX_TEXTO_MENSAJE = 20000;
const MAX_TOKENS = 8192;
const MAX_TOKENS_DEFECTO = 1024;
const TEMPERATURA_DEFECTO = 1;
const MAX_EN_CURSO = 2;
const TIMEOUT_PAGINA_MS = 38000;
const TIMEOUT_RELEVO_MS = 42000;
const MAX_TEXTO_RESPUESTA = 32000;
const MAX_BYTES_RESPUESTA = 60 * 1024;
/** Tope del envío por el WebSocket para llm.respuesta (R.A.M.S.: MAX_BYTES_RESPUESTA_LLM). */
const MAX_BYTES_WS_RESPUESTA = 64 * 1024;
const APAGADO = Object.freeze(['0', 'false', 'no', 'off']);

// ---------------------------------------------------------------------------------------------
// Configuración
// ---------------------------------------------------------------------------------------------

/** `AMADEUS_PUTER`: habilitado salvo `0`/`false`/`no`/`off`. */
function puterHabilitado(env = {}) {
  const v = String(env.AMADEUS_PUTER ?? '').trim().toLowerCase();
  return !APAGADO.includes(v);
}

/**
 * Modelo por defecto del escritorio (`AMADEUS_PUTER_MODELO`). Un valor inválido se ignora con un
 * aviso que no lo repite.
 * @returns {{modelo: string, aviso: string|null}}
 */
function modeloDefecto(env = {}) {
  const v = String(env.AMADEUS_PUTER_MODELO ?? '').trim();
  if (!v) return { modelo: MODELO_DEFECTO, aviso: null };
  if (!RE_MODELO.test(v)) {
    return { modelo: MODELO_DEFECTO, aviso: `AMADEUS_PUTER_MODELO no es un nombre de modelo válido; uso ${MODELO_DEFECTO}` };
  }
  return { modelo: v, aviso: null };
}

/** `AMADEUS_PUTER_ESFUERZO` (opcional): `reasoning_effort` de puter.ai.chat o `null`. */
function esfuerzoRazonamiento(env = {}) {
  const v = String(env.AMADEUS_PUTER_ESFUERZO ?? '').trim().toLowerCase();
  return ESFUERZOS.includes(v) ? v : null;
}

// ---------------------------------------------------------------------------------------------
// Validaciones puras
// ---------------------------------------------------------------------------------------------

const sinNul = (s) => s.replace(/\u0000/g, '');

/**
 * Valida `datos` de `llm.pedir`.
 * @returns {{ok: true, pedido: object} | {ok: false, id: string|null, motivo: string}}
 */
function validarPedido(datos) {
  if (!datos || typeof datos !== 'object' || Array.isArray(datos)) return { ok: false, id: null, motivo: 'sin datos' };
  const id = typeof datos.id === 'string' && RE_ID_PEDIDO.test(datos.id) ? datos.id : null;
  const mal = (motivo) => ({ ok: false, id, motivo });
  if (!id) return mal('id inválido');
  const modelo = datos.modelo === undefined || datos.modelo === null ? '' : datos.modelo;
  if (typeof modelo !== 'string' || (modelo !== '' && !RE_MODELO.test(modelo))) return mal('modelo inválido');
  const system = datos.system === undefined || datos.system === null ? '' : datos.system;
  if (typeof system !== 'string' || system.length > MAX_SYSTEM) return mal('system inválido');
  if (!Array.isArray(datos.mensajes) || datos.mensajes.length > MAX_MENSAJES) return mal('mensajes inválidos');
  const mensajes = [];
  for (const m of datos.mensajes) {
    if (!m || typeof m !== 'object' || !Object.hasOwn(ROLES, m.rol)) return mal('rol inválido');
    if (typeof m.texto !== 'string' || m.texto.length > MAX_TEXTO_MENSAJE) return mal('texto inválido');
    mensajes.push({ rol: m.rol, texto: sinNul(m.texto) });
  }
  let maxTokens = MAX_TOKENS_DEFECTO;
  if (datos.max_tokens !== undefined && datos.max_tokens !== null) {
    if (!Number.isInteger(datos.max_tokens) || datos.max_tokens < 1) return mal('max_tokens inválido');
    maxTokens = Math.min(MAX_TOKENS, datos.max_tokens);
  }
  let temperatura = TEMPERATURA_DEFECTO;
  if (datos.temperatura !== undefined && datos.temperatura !== null) {
    if (typeof datos.temperatura !== 'number' || !Number.isFinite(datos.temperatura)) return mal('temperatura inválida');
    temperatura = Math.min(2, Math.max(0, datos.temperatura));
  }
  return { ok: true, pedido: { id, modelo, system: sinNul(system), mensajes, maxTokens, temperatura } };
}

/**
 * Mensajes en el formato de `puter.ai.chat([messages])`: `system` primero; `usuario` → `user`,
 * `modelo` → `assistant`; los consecutivos del mismo rol se unen (algunos proveedores exigen
 * alternar) y, si la conversación no empieza por el usuario, se antepone un turno mínimo suyo.
 */
function aMensajesPuter(system, mensajes) {
  const out = [];
  if (typeof system === 'string' && system.trim()) out.push({ role: 'system', content: system });
  const inicio = out.length;
  for (const m of mensajes || []) {
    const role = ROLES[m.rol];
    if (!role || typeof m.texto !== 'string') continue;
    const previo = out.length > inicio ? out[out.length - 1] : null;
    if (previo && previo.role === role) previo.content += `\n\n${m.texto}`;
    else out.push({ role, content: m.texto });
  }
  if (out.length === inicio || out[inicio].role !== 'user') out.splice(inicio, 0, { role: 'user', content: '…' });
  return out;
}

const minusculas = (v) => (typeof v === 'string' || typeof v === 'number' ? String(v).toLowerCase() : '');

/**
 * Clasifica el fallo que informa la ventana (`{code, status, message, name, abortado, sinSesion}`,
 * todo opcional) en uno de ERRORES. El orden importa: si tras el fallo ya no hay sesión, es
 * «sin_sesion» aunque haya vencido el plazo (puter.js se queda esperando un inicio de sesión que en
 * una ventana oculta nunca llega); un aborto propio es «timeout» diga lo que diga el mensaje.
 */
function clasificarFallo(f) {
  if (!f || typeof f !== 'object') return 'otro';
  if (f.sinSesion === true) return 'sin_sesion';
  if (f.abortado === true) return 'timeout';
  const status = typeof f.status === 'number' ? f.status
    : (typeof f.status === 'string' && /^\d{1,3}$/.test(f.status) ? Number(f.status) : Number.NaN);
  const texto = [f.code, f.name, f.message].map(minusculas).join(' ');
  if (status === 408 || status === 504 || /request_timeout|timed? ?out|timeout|aborterror/.test(texto)) return 'timeout';
  if (status === 401 || /unauthori[sz]ed|token_auth_failed|auth_canceled|reauth|not_signed_in|sin_sesion/.test(texto)) {
    return 'sin_sesion';
  }
  if (status === 402 || status === 429 ||
      /insufficient|funds|usage[-_ ]?limit|quota|rate[-_ ]?limit|credit|too many requests|saldo/.test(texto)) {
    return 'cuota';
  }
  if (status === 0 || /network|failed to fetch|econn|enotfound|offline|invalid_stream_response|net::err/.test(texto)) {
    return 'red';
  }
  if (status === 403 || /moderat|content[-_ ]?(policy|filter)|safety|flagged|forbidden|refus|rejected|verification_required|email_must_be_confirmed/.test(texto)) {
    return 'rechazado';
  }
  return 'otro';
}

/** Bytes UTF-8 de `texto` serializado como cadena JSON. */
function bytesJson(texto) {
  return Buffer.byteLength(JSON.stringify(texto), 'utf8');
}

/** Corta en `n` unidades UTF-16 sin partir un par sustituto. */
function cortar(texto, n) {
  if (texto.length <= n) return texto;
  let fin = n;
  const c = texto.charCodeAt(fin - 1);
  if (c >= 0xd800 && c <= 0xdbff) fin -= 1;
  return texto.slice(0, fin);
}

/** Texto de la respuesta sin NUL, ≤ 32 000 caracteres y ≤ 60 KiB como cadena JSON. */
function recortarTexto(texto) {
  if (typeof texto !== 'string') return '';
  let t = cortar(sinNul(texto), MAX_TEXTO_RESPUESTA);
  while (t.length > 0 && bytesJson(t) > MAX_BYTES_RESPUESTA) t = cortar(t, Math.floor(t.length * 0.9));
  return t;
}

/** Datos de `puter:estado` de la ventana: `{cargado, sesion, error}` o `null`. */
function validarEstadoVentana(d) {
  if (!d || typeof d !== 'object' || typeof d.cargado !== 'boolean' || typeof d.sesion !== 'boolean') return null;
  const error = ERRORES_ESTADO.includes(d.error) ? d.error : null;
  return { cargado: d.cargado, sesion: d.cargado && d.sesion, error };
}

// ---------------------------------------------------------------------------------------------
// Relevo WS ↔ ventana oculta
// ---------------------------------------------------------------------------------------------

function crearRelevoPuter(puertos, opciones = {}) {
  const p = puertos;
  const ahora = typeof p.ahora === 'function' ? p.ahora : Date.now;
  const temporizador = typeof p.temporizador === 'function'
    ? p.temporizador
    : (fn, ms) => { const t = setTimeout(fn, ms); return () => clearTimeout(t); };
  const llamar = (fn, ...args) => (typeof fn === 'function' ? fn(...args) : undefined);
  const registrar = (texto) => llamar(p.registrar, texto);
  const habilitado = opciones.habilitado !== false;
  const modelo = typeof opciones.modelo === 'string' && RE_MODELO.test(opciones.modelo) ? opciones.modelo : MODELO_DEFECTO;
  const esfuerzo = ESFUERZOS.includes(opciones.esfuerzo) ? opciones.esfuerzo : null;

  const st = {
    cargado: false,                       // puter.js cargado en la ventana oculta
    sesion: false,                        // puter.auth.isSignedIn()
    error: habilitado ? null : 'deshabilitado',
    enLinea: false,                       // llegó el `hola` de R.A.M.S.
    ultimoEnviado: null,                  // firma del último puter.estado que salió
  };
  /** id → {t0, modelo, cancelar} */
  const enCurso = new Map();
  const cuenta = { pedidos: 0, respuestas: 0, fallos: 0 };

  function estado() {
    return {
      listo: habilitado && st.cargado && st.sesion,
      sesion: habilitado && st.sesion,
      modelo: habilitado ? modelo : null,
      error: st.error,
    };
  }

  /** Manda puter.estado si R.A.M.S. está en línea y cambió (o si se fuerza). */
  function enviarEstado(forzar = false) {
    if (!st.enLinea) return false;
    const datos = estado();
    const firma = JSON.stringify(datos);
    if (!forzar && firma === st.ultimoEnviado) return false;
    const ok = llamar(p.enviarWs, { tipo: 'puter.estado', datos }) === true;
    st.ultimoEnviado = ok ? firma : null;
    return ok;
  }

  function cambiar(nuevo) {
    const antes = JSON.stringify(estado());
    Object.assign(st, nuevo);
    if (JSON.stringify(estado()) === antes) return;
    llamar(p.alCambiarEstado);
    enviarEstado();
  }

  function responder(id, r) {
    const datos = { id, ok: r.ok === true, texto: r.ok === true ? r.texto : null,
      error: r.ok === true ? null : r.error, modelo: r.modelo ?? null, ms: Math.max(0, Math.round(r.ms ?? 0)) };
    if (datos.ok) cuenta.respuestas += 1;
    else cuenta.fallos += 1;
    const salio = llamar(p.enviarWs, { tipo: 'llm.respuesta', datos }, MAX_BYTES_WS_RESPUESTA) === true;
    if (!datos.ok) registrar(`Puter: pedido sin respuesta (${datos.error})`);
    // R.A.M.S. marca «sin sesión» al recibir ese error: se le reenvía el estado real para resincronizar.
    if (datos.error === 'sin_sesion') enviarEstado(true);
    return salio;
  }

  function quitar(id) {
    const e = enCurso.get(id);
    if (!e) return null;
    enCurso.delete(id);
    e.cancelar();
    return e;
  }

  /** `llm.pedir` de R.A.M.S. Responde siempre que el `id` sea válido (también con error). */
  function alPedido(datos) {
    const v = validarPedido(datos);
    const id = v.ok ? v.pedido.id : v.id;
    if (!id) {
      registrar('Puter: llm.pedir sin id válido ignorado');
      return false;
    }
    if (enCurso.has(id)) return false; // duplicado: ya se está atendiendo
    cuenta.pedidos += 1;
    const modeloPedido = v.ok && v.pedido.modelo ? v.pedido.modelo : modelo;
    const fallo = (error) => responder(id, { ok: false, error, modelo: habilitado ? modeloPedido : null, ms: 0 });
    if (!habilitado) return fallo('rechazado');
    if (!v.ok) {
      registrar(`Puter: llm.pedir rechazado (${v.motivo})`);
      return fallo('rechazado');
    }
    if (!st.cargado || !st.sesion) return fallo('sin_sesion');
    if (enCurso.size >= MAX_EN_CURSO) return fallo('rechazado');
    const { pedido } = v;
    const enviado = llamar(p.enviarVentana, 'puter:pedir', {
      id,
      modelo: modeloPedido,
      mensajes: aMensajesPuter(pedido.system, pedido.mensajes),
      maxTokens: pedido.maxTokens,
      temperatura: pedido.temperatura,
      esfuerzo,
      timeoutMs: TIMEOUT_PAGINA_MS,
    }) === true;
    if (!enviado) return fallo('otro');
    const t0 = ahora();
    const cancelar = temporizador(() => {
      if (!quitar(id)) return;
      llamar(p.enviarVentana, 'puter:cancelar', id);
      responder(id, { ok: false, error: 'timeout', modelo: modeloPedido, ms: ahora() - t0 });
    }, TIMEOUT_RELEVO_MS);
    enCurso.set(id, { t0, modelo: modeloPedido, cancelar });
    return true;
  }

  /** `puter:respuesta` de la ventana: `{id, ok, texto?, modelo?, fallo?}`. */
  function alRespuestaVentana(d) {
    if (!d || typeof d !== 'object' || typeof d.id !== 'string') return false;
    const e = quitar(d.id);
    if (!e) return false; // tardía (ya se respondió «timeout») o ajena
    const ms = ahora() - e.t0;
    const modeloUsado = typeof d.modelo === 'string' && RE_MODELO.test(d.modelo) ? d.modelo : e.modelo;
    if (d.ok === true) {
      const texto = recortarTexto(d.texto);
      if (texto.trim()) return responder(d.id, { ok: true, texto, modelo: modeloUsado, ms });
      return responder(d.id, { ok: false, error: 'otro', modelo: modeloUsado, ms });
    }
    const error = clasificarFallo(d.fallo);
    // La sesión la decide la página (puter.auth.isSignedIn() releído tras el fallo), no la
    // clasificación: un 401 o un texto de «unauthorized» no implica que puter.js haya soltado el
    // token, y si aquí se bajara la sesión la página no volvería a informar (su estado no cambió).
    const paginaSinSesion = !!d.fallo && typeof d.fallo === 'object' && d.fallo.sinSesion === true;
    if (paginaSinSesion) {
      if (st.sesion) cambiar({ sesion: false });
    } else if (error === 'sin_sesion') {
      // La página aún ve sesión: que la vuelva a comprobar y publique su estado real (puter.js puede
      // borrar el token un instante después de rechazar la llamada).
      llamar(p.enviarVentana, 'puter:refrescar', null);
    }
    return responder(d.id, { ok: false, error, modelo: modeloUsado, ms });
  }

  /** `puter:estado` de la ventana: `{cargado, sesion, error}`. */
  function alEstadoVentana(d) {
    const v = validarEstadoVentana(d);
    if (!v || !habilitado) return false;
    cambiar(v);
    return true;
  }

  /** Falla los pedidos en curso con `error` (la ventana se recarga, se cae o se cierra la sesión). */
  function fallarEnCurso(error) {
    for (const id of [...enCurso.keys()]) {
      const e = quitar(id);
      if (e) responder(id, { ok: false, error, modelo: e.modelo, ms: ahora() - e.t0 });
    }
  }

  /**
   * La ventana oculta va a recargarse (sesión nueva o desconexión) o se cayó: lo que estaba en
   * curso falla con `error` y el estado vuelve a «sin cargar» hasta que la página informe.
   */
  function alReinicioVentana(error = 'otro', motivoEstado = null) {
    fallarEnCurso(ERRORES.includes(error) ? error : 'otro');
    if (!habilitado) return;
    cambiar({ cargado: false, sesion: false, error: ERRORES_ESTADO.includes(motivoEstado) ? motivoEstado : null });
  }

  function alHola() {
    st.enLinea = true;
    return enviarEstado(true);
  }

  /** Se cerró el WebSocket: las respuestas ya no pueden llegar; se cancelan en la ventana. */
  function alDesconectado() {
    st.enLinea = false;
    st.ultimoEnviado = null;
    for (const id of [...enCurso.keys()]) {
      if (quitar(id)) llamar(p.enviarVentana, 'puter:cancelar', id);
    }
  }

  return {
    estado,
    alHola,
    alDesconectado,
    alPedido,
    alRespuestaVentana,
    alEstadoVentana,
    alReinicioVentana,
    enCurso: () => enCurso.size,
    estadisticas: () => ({ ...cuenta }),
    habilitado,
    modelo,
  };
}

module.exports = {
  ORIGEN_PUTER,
  MODELO_DEFECTO,
  RE_ID_PEDIDO,
  RE_MODELO,
  ERRORES,
  ERRORES_ESTADO,
  ESFUERZOS,
  MAX_SYSTEM,
  MAX_MENSAJES,
  MAX_TEXTO_MENSAJE,
  MAX_TOKENS,
  MAX_EN_CURSO,
  TIMEOUT_PAGINA_MS,
  TIMEOUT_RELEVO_MS,
  MAX_TEXTO_RESPUESTA,
  MAX_BYTES_RESPUESTA,
  MAX_BYTES_WS_RESPUESTA,
  puterHabilitado,
  modeloDefecto,
  esfuerzoRazonamiento,
  validarPedido,
  aMensajesPuter,
  clasificarFallo,
  recortarTexto,
  validarEstadoVentana,
  crearRelevoPuter,
};
