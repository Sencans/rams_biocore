# Amadeus Desktop

Amadeus (R.A.M.S. BIO-CORE) en tu escritorio: una ventana transparente siempre encima donde el
avatar 3D de VTuber AI Studio camina por la pantalla, se asoma por los bordes, mira el cursor y
te busca; una bandeja del sistema; atajos; y una ventana de chat para escribirle.

- **Quién decide qué hace** sigue siendo R.A.M.S. (conectoma + reglas, sin LLM). Esta app es un
  efector: muestra, oculta, mueve el click-through y pasa mensajes.
- **No gasta tokens.** Todo es local: el avatar lo pinta VTuber AI Studio (`:3939`) y el chat va por
  el WebSocket `/escritorio` de R.A.M.S. La única excepción es opcional y la activas tú: el
  [respaldo Puter](#puter-ia-de-respaldo) del Área de Broca, que usa **tu** cuenta de Puter.
- Especificación: `docs/COMPANION.md` §C2.7 (y §C1.1, §C1.2, §C1.11, §C1.12; Puter: §C2.15).

## Requisitos

- Node.js ≥ 22.12 (probado con 24.18).
- Electron **44.8.0** (fijado en `package.json`; ~158 MB en Windows, ~123 MB en Linux).
- VTuber AI Studio con el **modo escritorio** (`?desktop=1`) y su código de terceros en
  `public/vendor/` (la CSP del overlay no permite cargar scripts de jsDelivr).
- R.A.M.S. con el endpoint `/escritorio` activo (`RAMS_ESCRITORIO=1`, por defecto).

## Instalación

```sh
cd rams_biocore/desktop
npm i
npx install-electron     # Electron 44 descarga el binario en el primer uso; así lo bajas ya
```

## Arranque

1. VTuber AI Studio: `cd vtuber-ai && node server.js`
2. R.A.M.S.: como siempre (`python main.py`).
3. Amadeus Desktop: `npm start` (en Wayland sin Hyprland: `npm run start:x11`).

`npm start -- --hidden` arranca en la bandeja sin mostrarla. Sin `--hidden`, aparece caminando en
cuanto el visor carga. El orden no importa: la app reintenta el visor cada 5 s y R.A.M.S. con
espera creciente (1 → 30 s), y relee el token en cada intento.

## Variables de entorno

| Variable | Defecto | Qué hace |
|---|---|---|
| `VTUBER_AI_URL` | `http://127.0.0.1:3939` | Origen de VTuber AI Studio, **solo local** (`http(s)://127.0.0.1`, `localhost` o `[::1]`, con puerto opcional y sin ruta; el mismo patrón que R.A.M.S.). Cualquier otro valor se ignora con un aviso y se usa el defecto. El overlay carga `/?obs=1&desktop=1&camera=full&theme=amadeus` solo tras un `GET /` correcto. |
| `RAMS_ESCRITORIO_TOKEN_FILE` | Windows: `%LOCALAPPDATA%\rams\escritorio.json`; Linux: `$XDG_RUNTIME_DIR/rams/escritorio.json` | Archivo `{url, token, pid}` que escribe R.A.M.S. En Linux debe ser tuyo y `0600` (si no, no conecta). **Sin `XDG_RUNTIME_DIR` no conecta** salvo que definas esta variable (nunca se usa `/tmp`). |
| `AMADEUS_EXCLUIR_CAPTURA` | `1` | `1` = el overlay **no sale en capturas, OBS ni Discord** (`setContentProtection`). Pon `0` si quieres capturarla en stream. |
| `VTUBER_AI_TEMA` | `amadeus` | `none` quita `&theme=amadeus` de la URL del overlay (igual que en R.A.M.S.). |
| `AMADEUS_ATAJO_LLAMAR`, `_CHAT`, `_BASTA`, `_VISION`, `_EMERGENCIA` | ver la tabla de atajos | Cambian un atajo global (Windows/X11), p. ej. `AMADEUS_ATAJO_BASTA=Control+Alt+Shift+F9`; `off` lo quita. Un valor inválido o repetido se ignora con un aviso. |
| `AMADEUS_PUTER` | `1` | `0` desactiva el respaldo Puter: no se crea la ventana oculta, no hay entradas en la bandeja y R.A.M.S. recibe `puter.estado` con `error: "deshabilitado"`. |
| `AMADEUS_PUTER_MODELO` | `google/gemini-3.5-flash-lite` | Modelo de Puter por defecto (se usa cuando R.A.M.S. pide `modelo: ""`, es decir, sin `PUTER_MODEL`). Debe cumplir el mismo patrón que `PUTER_MODEL` en R.A.M.S. (empieza por letra o cifra; hasta 100 caracteres de `A-Z a-z 0-9 _ . : / -`); si no, se ignora con un aviso que no repite el valor. |
| `AMADEUS_PUTER_ESFUERZO` | (sin definir) | `reasoning_effort` de `puter.ai.chat`: `none`, `minimal`, `low`, `medium` o `high` (si el modelo lo admite). Sin la variable no se envía. |
| `AMADEUS_AUTOARRANQUE` | (sin definir) | Solo Windows: `1` registra el inicio con sesión (`--hidden`), `0` lo quita. Sin la variable no se toca nada. En Hyprland usa `hl.on("hyprland.start", …)`. |

El token nunca se escribe en logs ni en la URL de ninguna página: lo usa solo el proceso principal.

## Bandeja, atajos y línea de órdenes

Bandeja: **Llamar a Amadeus** (o Esconder) · **Abrir chat** · **No molestar** · **Quieta** ·
**Pausar visión** · **Dejar pasar clics** · **Conectar Puter (IA de respaldo)…** (o **Desconectar
Puter**) · **Salir**. Un clic en el icono la llama o la esconde.

| Atajo (Windows/X11) | CLI (`amadeus-desktop …`) | Qué hace |
|---|---|---|
| `Ctrl+Alt+A` | `--amadeus=toggle` (`show`, `hide`) | Llamarla / que se vaya caminando |
| `Ctrl+Alt+Space` | `--amadeus=chat` | Abrir/cerrar el chat (toma el foco) |
| `Ctrl+Alt+Shift+Q` | `--amadeus=basta` | Quieta: sin travesuras con el ratón y se esconde |
| `Ctrl+Alt+V` | `--amadeus=vision_pausa` | Pausar la visión (cámara/pantalla) en R.A.M.S. |
| `Ctrl+Alt+Shift+A` | `--amadeus=clics` | **Emergencia**: la oculta ya y deja pasar todos los clics (no necesita a R.A.M.S.) |
| — | `--amadeus=puter` | Abrir la ventanita «Conectar Puter» (útil sin bandeja) |
| — | `--amadeus=quit` | Salir |

**Por qué «Quieta» lleva Shift:** en Windows AltGr llega como Ctrl+Alt, así que un atajo global
`Ctrl+Alt+<tecla>` se come el carácter AltGr de esa tecla en todas las apps. En el teclado
latinoamericano (y en el alemán) AltGr+Q es **@**: con `Ctrl+Alt+Q` cada arroba de un correo o una
@mención la escondería y apagaría las travesuras. `Ctrl+Alt+A`, `Ctrl+Alt+V` y `Ctrl+Alt+Space` no
escriben nada en el teclado latinoamericano; si tu teclado sí usa AltGr+A o AltGr+V (polaco ą,
húngaro o checo @), cámbialos con `AMADEUS_ATAJO_*` y añade Shift. En Hyprland se usa la misma
combinación por coherencia (allí AltGr no choca).

Con la app abierta, una segunda ejecución con `--amadeus=…` le entrega la orden a la primera y sale.
Lanzadores: `scripts/amadeus-desktop.cmd` (Windows) y `scripts/amadeus-desktop.sh` (Linux).

En el chat también vale escribir "ven", "vete", "basta", "deja de mirarme"… (lo interpreta R.A.M.S.).

## El chat

- Ventana aparte ("Amadeus Chat"). Ves tu mensaje al instante; las respuestas llegan como texto.
- Cuando R.A.M.S. abre el chat por su cuenta **no te roba el foco**; además, el chat ignora `Enter`
  durante 600 ms tras recibir el foco.
- Las confirmaciones (p. ej. "Abrir WhatsApp con un borrador…") salen con **botones** y una vista
  previa del texto. Escribir "sí" no aprueba nada. Nada se envía solo: tú pulsas Enviar en la app.
- Los enlaces que comparte (algo curioso) solo se abren si haces clic, y solo si vinieron de R.A.M.S.
- Si pulsas Aprobar/Rechazar sin conexión con R.A.M.S., el chat te lo dice y los botones vuelven a
  estar activos. Al reconectar, las confirmaciones abiertas se comprueban: la que sigue pendiente
  se puede responder y las demás se marcan «Ya no está pendiente.».
- Clic derecho sobre el avatar también abre el chat.

### Tarjetas: correo, Drive y scripts (v0.2.2)

Lo que Amadeus prepara para enviar, subir, compartir o ejecutar llega al chat como una **tarjeta**
(detalle en `docs/V022_CORREO_DRIVE_SCRIPTS.md` §12–§13). La misma tarjeta sale en el chat RINE del
HUD; la app móvil la ignora.

- **📧 Correo:** Para, CC, CCO, asunto, cuerpo y adjuntos completos. «Editar» lo cambia en la propia
  tarjeta (los campos tienen el mismo tope que R.A.M.S.) y «Enviar» lo manda a Gmail.
- **🗂 Drive:** la operación en una frase («Subir "plan.xlsx" (2,1 MB, Documentos) a "Trabajo"»).
  Compartir en público pide un segundo clic con el aviso «Cualquiera con el enlace podrá verlo».
- **🧪 Script:** el código **completo** (nunca recortado) con números de línea, el análisis local
  de riesgo (insignia verde, amarilla, naranja o roja) y sus hallazgos. Con riesgo alto hay que
  marcar «Leí el código y entiendo el riesgo»; lo crítico no se puede ejecutar desde aquí. «Copiar
  código» lo copia el proceso principal (solo el código que envió R.A.M.S.); «Guardar como
  habilidad» pide el nombre en la propia tarjeta. **▶ Resultado del script** muestra la salida.
- Solo se aprueba con los botones, ligados al id, a la huella y a la versión de lo que ves. Escribir
  "sí" no aprueba nada. Tras el clic los botones quedan bloqueados hasta que R.A.M.S. responde y,
  mientras R.A.M.S. lo ejecuta, la tarjeta sale «en curso» (sin botones) en todos los chats, también
  en uno que se reconecta; un clic sobre una versión que ya no es la vigente se ignora. Sin
  conexión, el chat dice «Sin conexión con R.A.M.S.» y los reactiva.
- El segundo clic de un doble clic nunca cuenta, y un botón de aprobar que acaba de aparecer (otra
  versión, o «Sí, hacerlo público» en el sitio de «Compartir») espera 1 s antes de poder pulsarse.
- Al editar un correo, un tabulador o un salto de línea pegado de Word en un campo no se envía: el
  chat dice en qué campo está y ofrece «Quitar caracteres no válidos» (nunca los quita sin pedirlo).
- «Guardar cambios» manda solo los campos cambiados; si el borrador marca problemas (p. ej. un nombre
  que R.A.M.S. no conoce y que por eso no sale en Para), manda además Para, CC, CCO y Adjuntos tal como
  se ven, para que el correo quede como lo ves en vez de seguir bloqueado.
- Al reconectar, las tarjetas se comprueban como las confirmaciones: las que ya no vuelven se
  marcan «Ya no está vigente.». Una tarjeta nueva abre el chat sin robar el foco, salvo con «No molestar».
- Ningún mensaje de tarjeta pasa de 60 KiB en ningún sentido. La decisión (`tarjeta_decision`) es
  el único mensaje que sale con ese tope; el resto sigue en 8 KiB.

## Puter (IA de respaldo)

El Área de Broca de R.A.M.S. habla con Gemini. Si Gemini falla (sin cuota, sin red o en el tope del
día), R.A.M.S. le pide el texto a esta app, que lo genera con [Puter.js](https://docs.puter.com) en
una **ventana oculta**: cadena **Gemini → Puter → banco local de frases**. Con
`RAMS_PUTER_PRIMERO=1` en R.A.M.S., Puter va primero.

- **Sin claves de API.** Puter usa el modelo *user-pays*: inicias sesión con **tu** cuenta de Puter
  y el uso se descuenta de **tu** saldo allí (no de la cuota de la clave de Gemini). Ni R.A.M.S. ni
  esta app guardan contraseñas: las escribes en la ventana de puter.com.
- **Nada se carga hasta que conectas.** Mientras no hayas iniciado sesión desde la bandeja, la
  ventana oculta no descarga puter.js ni hace ninguna petición a Puter, y cada pedido de R.A.M.S. se
  responde al instante con `sin_sesion` **sin abrir ninguna ventana**.

### Conectar y desconectar

1. Bandeja → **Conectar Puter (IA de respaldo)…** (o `amadeus-desktop --amadeus=puter`).
2. En la ventanita, pulsa **Iniciar sesión en Puter**: se abre la ventana de puter.com (necesita
   ese clic: los navegadores bloquean las ventanas emergentes sin un gesto del usuario).
3. Al terminar verás «¡Conectado!». La ventana oculta se recarga con la sesión nueva y R.A.M.S. pasa
   el diagnóstico «Respaldo Puter (Amadeus Desktop)» a **OK** con el modelo. La sesión se guarda
   entre arranques (partición `persist:puter`).
4. Bandeja → **Desconectar Puter** borra la sesión de Puter de esta app (la partición entera) y deja
   de usarla. Con la sesión iniciada, el botón de la ventanita pasa a **Cambiar de cuenta**.

En Hyprland la ventanita «Conectar Puter» y la de puter.com son ventanas normales; si quieres que
floten, añade `float` para esos títulos en tus reglas.

### Modelo por defecto

`google/gemini-3.5-flash-lite`, comprobado el 17-10-2026 en el listado oficial
<https://developer.puter.com/ai/models/>, en su ficha
<https://developer.puter.com/ai/google/gemini-3.5-flash-lite/> y en los ejemplos de
<https://docs.puter.com/AI/chat/>. Motivos: es el más rápido de los candidatos según las fichas de
Puter (5,79 s al primer token y ~411 tokens/s, frente a 16,29 s de `google/gemini-3.8-flash`), es de
la familia Gemini para la que está ajustado el prompt de Kurisu y ya forma parte de la cadena de
respaldo de Gemini de R.A.M.S. (`GEMINI_FALLBACK_MODELS`), así que su español con ese prompt está
probado. Cámbialo con `AMADEUS_PUTER_MODELO` (aquí) o con `PUTER_MODEL` (en R.A.M.S., que manda
sobre este). Alternativas: `google/gemini-3.8-flash` (el primario de R.A.M.S., más lento) y
`anthropic/claude-haiku-5-5` (otro proveedor, por si cae Google). Al conectar, la app comprueba el
modelo con `puter.ai.listModels()` y, si no aparece, lo avisa en la bandeja y se lo comunica a
R.A.M.S. (`puter.estado` con `error: "modelo_desconocido"`).

### Contrato con R.A.M.S. (WebSocket `/escritorio`, sobre `{tipo, datos}`)

| Dirección | Mensaje | Cuándo |
|---|---|---|
| app → R.A.M.S. | `puter.estado {listo, sesion, modelo, error}` | Tras el `hola` y en cada cambio. `listo` = puter.js cargado **y** sesión iniciada. |
| R.A.M.S. → app | `llm.pedir {id, modelo, system, mensajes:[{rol, texto}], max_tokens, temperatura}` | Cuando Broca usa el respaldo. `modelo: ""` = el de esta app. |
| app → R.A.M.S. | `llm.respuesta {id, ok, texto, error, modelo, ms}` | Siempre, en ≤ 45 s. `error` ∈ `sin_sesion`, `timeout`, `cuota`, `red`, `rechazado`, `otro`. |

Límites: `system` ≤ 60 000 caracteres, ≤ 40 mensajes de ≤ 20 000; como mucho **2 pedidos a la
vez** (el 3.º → `rechazado`); `max_tokens` ≤ 8192 y `temperatura` en 0–2. La ventana aborta
`puter.ai.chat` a los 38 s y el proceso principal responde `timeout` a los 42 s si la ventana no
contestó (R.A.M.S. espera ≤ 40 s). La respuesta se recorta a 32 000 caracteres y ≤ 60 KiB (R.A.M.S.
acepta `llm.respuesta` de ≤ 64 KiB; el resto de mensajes sigue en ≤ 8 KiB). Solo `llm.pedir` puede
llegar con más de 64 KiB (hasta 1 MiB).

### Cómo está hecho (y por qué es seguro)

- **Relevo en el proceso principal.** El WebSocket con el token de R.A.M.S. vive solo en el proceso
  principal; la ventana oculta recibe cada pedido por IPC (`puter:pedir`) y devuelve
  `puter:respuesta`. **El token nunca llega a las páginas de Puter.** El IPC se valida por ventana,
  frame principal y URL exacta.
- **Ventana oculta** (`puter/trabajo.html`): `show: false`, `sandbox`, `contextIsolation`, preload
  mínimo (`window.amadeusPuter` v1, nunca `ipcRenderer`), sin `window.open` ni navegación. Nunca
  inicia sesión: sin sesión no llama a `puter.ai.chat` (que abriría el diálogo de Puter). Si se cae,
  los pedidos en curso fallan con `otro`, el estado pasa a `ventana_caida` y se recarga con espera
  creciente (2 s, 4 s… hasta 60 s) que solo vuelve a 2 s cuando la página lleva 60 s cargada sin caerse.
- **La sesión de Puter la decide la ventana oculta** (`puter.auth.isSignedIn()`, releído tras cada
  fallo). Un 401 de la API con el token aún puesto se responde a R.A.M.S. como `sin_sesion`, pero
  el estado no se baja a ciegas: el proceso principal le pide a la página que relea la sesión
  (`puter:refrescar`) y publica lo que ella diga.
- **Origen propio sin red ni puertos:** las páginas se sirven en `http://amadeus-puter.localhost`
  con `protocol.handle` **solo** en la partición de Puter (lo demás → 404). Puter no admite
  `file://` para iniciar sesión (`unsupported_origin`) y guarda la sesión por origen.
- **CSP por cabecera:** scripts solo propios y de `https://js.puter.com`; conexiones solo a
  `https://api.puter.com`, `wss://api.puter.com`, `https://puter.com` y `https://js.puter.com`; sin
  marcos, objetos, formularios ni workers.
- **Ventanas emergentes:** solo la ventanita «Conectar Puter» puede abrir una, y solo a
  `https://puter.com` o sus subdominios, con `sandbox`; esa ventana no puede abrir otras ni salir de
  puter.com, y en ella `window.amadeusPuter` no existe.
- **Privacidad:** los prompts, los mensajes y las respuestas nunca se registran; de un fallo solo se
  registra su clasificación.

### Prueba manual (con tu cuenta de Puter)

1. Arranca R.A.M.S. y la app; en la bandeja debe aparecer **Conectar Puter (IA de respaldo)…** y el
   diagnóstico «Respaldo Puter» de R.A.M.S. debe estar **EN ESPERA (sin sesión de Puter)**.
2. Conecta (pasos de arriba). El diagnóstico pasa a **OK** y la bandeja dice «Puter: conectado».
3. Con `RAMS_PUTER_PRIMERO=1` (o sin clave de Gemini) escribe en el chat: la respuesta llega por
   Puter. En puter.com verás el consumo en tu cuenta.
4. **Desconectar Puter** → el diagnóstico vuelve a **EN ESPERA** y las respuestas vuelven a Gemini o
   al banco local.

## OBS, capturas y privacidad

La casilla **No molestar** de la bandeja muestra siempre el estado real de R.A.M.S. (también si lo
cambias desde el HUD). Si la cambias sin conexión, se le comunica al reconectar.

Cuando R.A.M.S. avisa de que su cámara está abierta (mensaje `vision` de `/escritorio`), la bandeja
lo muestra mientras dure: icono con punto rojo, «● Cámara activa» en el menú y en el texto del
icono, y el visor recibe `overlay:order {accion:'camara', activa}` para pintar su ojo.

Por defecto (`AMADEUS_EXCLUIR_CAPTURA=1`) el overlay se excluye de capturas de pantalla, OBS,
Discord y de la propia visión de R.A.M.S. en Windows. **Si haces stream con ella, pon `0`.** En
Hyprland no hay exclusión: R.A.M.S. enmascara la caja del avatar (la app le envía su posición).

## Linux / Arch / Hyprland

1. `npm i && npx install-electron` en `desktop/`.
2. Lanzador: `ln -s "$PWD/scripts/amadeus-desktop.sh" ~/.local/bin/amadeus-desktop` (y
   `chmod +x scripts/amadeus-desktop.sh`). En Wayland sin Hyprland añade `--ozone-platform=x11` solo.
3. Reglas y atajos: copia `hyprland.ejemplo.lua` (Hyprland 0.55+) o `hyprland.ejemplo.conf`
   (0.53–0.54). Llevan `float`, `pin`, `move`, `size`, `no_initial_focus` (overlay **y** chat) y
   `no_follow_mouse`, más los `bind` a la CLI y el arranque oculto.
4. Opcional: `scripts/amadeus-desktop.desktop` en `~/.local/share/applications/` (identidad para
   lanzadores y portales).
5. La bandeja necesita una barra con SNI (p. ej. el módulo `tray` de waybar). Sin ella, la app
   avisa y sigue funcionando por CLI y atajos.

En Hyprland el cursor global se lee del socket `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`
(15 Hz cerca de ella, 4 Hz lejos). En otros compositores Wayland no hay cursor global: usa
`npm run start:x11`.

### Prueba manual de la transparencia en Wayland

1. Arranca VTuber AI Studio y `npm start` (sin `--hidden`).
2. Debe verse **solo el avatar**, sin rectángulo negro o gris alrededor. Si ves fondo negro:
   prueba `npm run start:x11` (XWayland) y avisa de la versión de Hyprland y de la GPU.
3. Mueve el ratón por zonas vacías y haz clic en otras ventanas: los clics deben atravesar el
   overlay. Sobre el avatar, el clic debe "tocarla" (reacciona) y puedes arrastrarla.
4. Pasa el ratón por encima sin hacer clic: tu ventana activa no debe perder el foco
   (`no_follow_mouse`).
5. `amadeus-desktop --amadeus=clics`: desaparece al instante y todo vuelve a ser clicable.
6. Si la región clicable "se queda pegada" tras moverla, anota cuándo pasa (Wayland aplica la
   región de entrada en el siguiente fotograma).

## Seguridad (resumen)

- Ventanas con `contextIsolation`, `sandbox`, sin `nodeIntegration`; los preloads exponen solo
  `window.amadeusDesktop` (v3), `window.amadeusChat` (v2: la v1 más las tarjetas) y
  `window.amadeusPuter` (v1, solo en el origen propio de Puter), nunca `ipcRenderer`.
- IPC del overlay solo desde el origen de `VTUBER_AI_URL`, que tiene que ser local; IPC del chat
  (también los botones de las tarjetas y «Copiar código») solo desde su ventana. El portapapeles
  solo recibe el código de una tarjeta de script vigente: nunca texto que mande la página.
- El renderer solo **pide** ser clicable: el proceso principal decide con el cursor global
  (dentro de la caja + 8 px), valida la caja (≤ 600×900 dentro del área) y vuelve al click-through
  tras 1,5 s fuera, si el visor se cae o deja de responder.
- CSP por cabecera en el overlay (`script-src 'self' 'sha256-<importmap>'`, sin CDN de código;
  `connect-src` permite jsDelivr solo para el VRM de ejemplo), navegación y ventanas nuevas
  bloqueadas, permisos del navegador (cámara, micro, pantalla…) denegados.
- R.A.M.S. solo acepta a esta app por `/escritorio` con el token del archivo privado.
- Puter: ventana oculta en su propia partición, CSP propia, ventanas emergentes solo a puter.com y
  el token de R.A.M.S. fuera de su alcance (ver [Puter](#puter-ia-de-respaldo)).

## Pruebas

```sh
npm test                          # node --test test/  (CLI, hit-test, mapeo WS↔IPC, token, CSP, preloads, Puter, estáticos)
npm run smoke                     # Electron oculto: sin bandeja, atajos, red ni ventanas visibles; "smoke OK"
node scripts/verificar-smoke.js   # lo mismo, comprobando además ≤ 5 s y código 0
```

Las pruebas de Puter usan dobles de puter.js, del DOM y del WebSocket (sin red ni cuenta). El smoke
comprueba además la ventana oculta de Puter con Electron real: protocolo propio con CSP y 404,
`window.amadeusPuter` aislado, que sin sesión no se carga puter.js y que un `llm.pedir` falla al
instante con `sin_sesion` sin abrir ventanas.

Las tarjetas (v0.2.2) se prueban con un DOM falso (`chat.test.js`), el mapeo puro (`mapeo.test.js`),
el preload (`preload.test.js`) y `main.js` con un Electron simulado que tiene un **portapapeles falso**
(`main.test.js`). El smoke comprueba además que una tarjeta de script se pinta con el código
completo como texto y, si es crítica, sin «Ejecutar».

El smoke usa un perfil temporal nuevo en cada ejecución (`%TEMP%/amadeus-desktop-smoke-*`; borra
los de más de 2 min) y arranca sin GPU. `verificar-smoke.js` separa el momento de "smoke OK" del
cierre de Chromium, que no depende de `main.js` y crece con la carga de la máquina (medido en
Windows: 0,2–1,5 s en reposo, hasta ~2 s con cuatro smokes en paralelo). Si una ejecución solo se
pasa de 5 s, la repite una vez y la marca como LENTO; un código distinto de 0 o la falta de
"smoke OK" son siempre FALLO.

`scripts/generar-icono.js` regenera `assets/tray.png` y `assets/tray-camara.png`.

## Problemas frecuentes

- **No aparece**: mira la consola; si dice "VTuber AI Studio no responde", arráncalo. Si el visor
  no tiene el modo escritorio o le falta `public/vendor/`, la CSP bloqueará los scripts.
- **"R.A.M.S. no disponible: no existe"**: R.A.M.S. no está corriendo o no escribió el token
  (comprueba `RAMS_ESCRITORIO`, y en Linux `XDG_RUNTIME_DIR`).
- **Los atajos no funcionan**: otra app los usa (la consola lo avisa) o estás en Wayland (usa
  `hl.bind`).
- **«La ventana de Puter se bloqueó»**: vuelve a pulsar el botón (tiene que ser un clic tuyo). Si
  dice que Puter no acepta el origen, avisa: la app sirve sus páginas en
  `http://amadeus-puter.localhost` justamente para evitarlo.
- **«Puter: sin conexión con puter.com»** en la bandeja: no se pudo descargar puter.js; la app lo
  reintenta sola (30 s → 5 min).
- **R.A.M.S. marca «saldo de Puter agotado»**: la respuesta de Puter fue `cuota`; recarga saldo en tu
  cuenta de Puter o espera (R.A.M.S. no vuelve a pedir en 10 min).
