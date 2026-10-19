'use strict';
/**
 * Ventanita «Conectar Puter» de Amadeus Desktop (COMPANION.md §C2.15). Se abre desde la bandeja
 * («Conectar Puter (IA de respaldo)…») o con `--amadeus=puter`.
 *
 * `puter.auth.signIn()` abre una ventana emergente de puter.com y los navegadores solo la permiten
 * tras un gesto del usuario (https://docs.puter.com/Auth/signIn/): por eso hay un botón. El main
 * solo deja abrir ventanas a puter.com y sus subdominios. Al terminar, pone la marca que hace que
 * la ventana oculta cargue puter.js y avisa al main (`amadeusPuter.conexion`) para que la recargue.
 *
 * Se carga como script clásico en el navegador; en Node (tests) exporta sus funciones.
 */
(function preparar(raiz, fabrica) {
  const api = fabrica();
  if (typeof module === 'object' && module && module.exports) module.exports = api;
  else if (raiz.document && raiz.document.readyState === 'loading') {
    raiz.document.addEventListener('DOMContentLoaded', () => api.iniciar(raiz), { once: true });
  } else api.iniciar(raiz);
}(typeof window !== 'undefined' ? window : globalThis, function fabrica() {
  const URL_PUTER_JS = 'https://js.puter.com/v2/';
  const MARCA = 'amadeus.puter.conectado';

  const MENSAJES = Object.freeze({
    popup_blocked: 'La ventana de Puter se bloqueó. Vuelve a pulsar el botón.',
    auth_window_closed: 'Cerraste la ventana de Puter antes de terminar. Puedes volver a intentarlo.',
    unsupported_origin: 'Puter no acepta el origen de esta ventana. Revisa la sección Puter del README.',
    not_available_in_app: 'Puter no permite iniciar sesión desde aquí.',
  });

  /** Código corto (`[a-z_]{1,24}`) del error de signIn (`{error, msg}`, Error o texto). */
  function codigoError(err) {
    let c = '';
    if (err && typeof err === 'object') c = typeof err.error === 'string' ? err.error : (typeof err.code === 'string' ? err.code : '');
    else if (typeof err === 'string') c = err;
    c = c.toLowerCase().replace(/[^a-z_]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24);
    return c || 'otro';
  }

  /**
   * `e`: {puter(): global de puter.js, puente: window.amadeusPuter, documento, marcar(): void}.
   */
  function crearConexion(e) {
    const $ = (id) => e.documento.getElementById(id);
    const boton = $('entrar');
    const estado = $('estado');
    const modelo = $('modelo');
    const st = { cargado: false, sesionPrevia: false, ocupado: false };

    const lib = () => {
      const p = e.puter();
      return p && typeof p === 'object' && p.auth && typeof p.auth.signIn === 'function' ? p : null;
    };

    function mostrar(texto, tipo = '') {
      estado.textContent = texto;
      estado.className = `estado${tipo ? ` ${tipo}` : ''}`;
    }

    function sesionIniciada() {
      try {
        return lib().auth.isSignedIn() === true;
      } catch {
        return false;
      }
    }

    function alCargar() {
      if (!lib()) {
        alFallarCarga();
        return;
      }
      st.cargado = true;
      boton.disabled = false;
      if (sesionIniciada()) {
        st.sesionPrevia = true;
        e.marcar();
        boton.textContent = 'Cambiar de cuenta';
        mostrar('Ya tienes la sesión de Puter iniciada: Amadeus la usa como respaldo.', 'ok');
        e.puente.conexion({ ok: true });
      } else {
        mostrar('Pulsa el botón: se abrirá puter.com para que inicies sesión.');
      }
    }

    function alFallarCarga() {
      boton.disabled = true;
      mostrar('No se pudo cargar Puter (¿sin conexión?). Cierra esta ventana y vuelve a intentarlo.', 'error');
      e.puente.conexion({ ok: false, error: 'carga_fallida' });
    }

    function cargarPuter() {
      if (lib()) {
        alCargar();
        return;
      }
      const s = e.documento.createElement('script');
      s.src = URL_PUTER_JS;
      s.async = true;
      s.addEventListener('load', alCargar);
      s.addEventListener('error', alFallarCarga);
      e.documento.head.appendChild(s);
    }

    /** Clic en el botón: signIn en el mismo gesto (si no, el navegador bloquea la ventana). */
    function alPulsar() {
      const p = lib();
      if (!p || st.ocupado) return;
      st.ocupado = true;
      boton.disabled = true;
      mostrar('Esperando a que inicies sesión en la ventana de Puter…');
      let promesa;
      try {
        promesa = st.sesionPrevia ? p.auth.signIn({ request_auth: true }) : p.auth.signIn();
      } catch (err) {
        promesa = Promise.reject(err);
      }
      return Promise.resolve(promesa).then(() => {
        e.marcar();
        st.sesionPrevia = true;
        boton.textContent = 'Cambiar de cuenta';
        mostrar('¡Conectado! Amadeus ya puede usar Puter como respaldo. Puedes cerrar esta ventana.', 'ok');
        e.puente.conexion({ ok: true, nueva: true });
      }, (err) => {
        const c = codigoError(err);
        mostrar(MENSAJES[c] || `No se pudo iniciar sesión en Puter (${c}).`, 'error');
        e.puente.conexion({ ok: false, error: c });
      }).finally(() => {
        st.ocupado = false;
        boton.disabled = false;
      });
    }

    function arrancar() {
      boton.disabled = true; // hasta que cargue puter.js (también lo dice el HTML)
      boton.addEventListener('click', alPulsar);
      $('cerrar').addEventListener('click', () => e.puente.cerrar());
      e.puente.onInfo((i) => {
        if (!i || typeof i.modelo !== 'string' || !modelo) return;
        modelo.textContent = `Modelo por defecto: ${i.modelo}`;
        modelo.hidden = false;
      });
      cargarPuter();
    }

    return { arrancar, alPulsar, estado: () => ({ ...st }) };
  }

  function iniciar(g) {
    const puente = g.amadeusPuter;
    if (!puente || puente.version !== 1) return null;
    const c = crearConexion({
      puter: () => g.puter,
      puente,
      documento: g.document,
      marcar: () => {
        try {
          g.localStorage.setItem(MARCA, '1');
        } catch {
          /* sin almacenamiento: la ventana oculta no cargará puter.js */
        }
      },
    });
    c.arrancar();
    return c;
  }

  return { URL_PUTER_JS, MARCA, MENSAJES, codigoError, crearConexion, iniciar };
}));
