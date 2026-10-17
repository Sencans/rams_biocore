# Amadeus Desktop

Amadeus (R.A.M.S. BIO-CORE) en tu escritorio: una ventana transparente siempre encima donde el
avatar 3D de VTuber AI Studio camina por la pantalla, se asoma por los bordes, mira el cursor y
te busca; una bandeja del sistema; atajos; y una ventana de chat para escribirle.

- **Quién decide qué hace** sigue siendo R.A.M.S. (conectoma + reglas, sin LLM). Esta app es un
  efector: muestra, oculta, mueve el click-through y pasa mensajes.
- **No gasta tokens.** Todo es local: el avatar lo pinta VTuber AI Studio (`:3939`) y el chat va por
  el WebSocket `/escritorio` de R.A.M.S.
- Especificación: `docs/COMPANION.md` §C2.7 (y §C1.1, §C1.2, §C1.11, §C1.12).

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
| `AMADEUS_AUTOARRANQUE` | (sin definir) | Solo Windows: `1` registra el inicio con sesión (`--hidden`), `0` lo quita. Sin la variable no se toca nada. En Hyprland usa `hl.on("hyprland.start", …)`. |

El token nunca se escribe en logs ni en la URL de ninguna página: lo usa solo el proceso principal.

## Bandeja, atajos y línea de órdenes

Bandeja: **Llamar a Amadeus** (o Esconder) · **Abrir chat** · **No molestar** · **Quieta** ·
**Pausar visión** · **Dejar pasar clics** · **Salir**. Un clic en el icono la llama o la esconde.

| Atajo (Windows/X11) | CLI (`amadeus-desktop …`) | Qué hace |
|---|---|---|
| `Ctrl+Alt+A` | `--amadeus=toggle` (`show`, `hide`) | Llamarla / que se vaya caminando |
| `Ctrl+Alt+Space` | `--amadeus=chat` | Abrir/cerrar el chat (toma el foco) |
| `Ctrl+Alt+Shift+Q` | `--amadeus=basta` | Quieta: sin travesuras con el ratón y se esconde |
| `Ctrl+Alt+V` | `--amadeus=vision_pausa` | Pausar la visión (cámara/pantalla) en R.A.M.S. |
| `Ctrl+Alt+Shift+A` | `--amadeus=clics` | **Emergencia**: la oculta ya y deja pasar todos los clics (no necesita a R.A.M.S.) |
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
  `window.amadeusDesktop` (v3) y `window.amadeusChat` (v1), nunca `ipcRenderer`.
- IPC del overlay solo desde el origen de `VTUBER_AI_URL`, que tiene que ser local; IPC del chat
  solo desde su ventana.
- El renderer solo **pide** ser clicable: el proceso principal decide con el cursor global
  (dentro de la caja + 8 px), valida la caja (≤ 600×900 dentro del área) y vuelve al click-through
  tras 1,5 s fuera, si el visor se cae o deja de responder.
- CSP por cabecera en el overlay (`script-src 'self' 'sha256-<importmap>'`, sin CDN de código;
  `connect-src` permite jsDelivr solo para el VRM de ejemplo), navegación y ventanas nuevas
  bloqueadas, permisos del navegador (cámara, micro, pantalla…) denegados.
- R.A.M.S. solo acepta a esta app por `/escritorio` con el token del archivo privado.

## Pruebas

```sh
npm test                          # node --test test/  (CLI, hit-test, mapeo WS↔IPC, token, CSP, preloads, estáticos)
npm run smoke                     # Electron oculto: sin bandeja, atajos, red ni ventanas visibles; "smoke OK"
node scripts/verificar-smoke.js   # lo mismo, comprobando además ≤ 5 s y código 0
```

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
