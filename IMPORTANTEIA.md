# IMPORTANTEIA: guía para IAs de código que trabajen en este proyecto

> Para Claude, Codex, Cursor, Gemini o cualquier otra IA que llegue aquí sin contexto.
> Cubre **dos repos** que funcionan juntos:
> - `C:\proyectos\rams_biocore`: R.A.M.S. BIO-CORE (Python) + Amadeus Desktop (Electron, en `desktop/`).
> - `C:\proyectos\vtuber-ai`: VTuber AI Studio (visor 3D VRM en Node, sin dependencias npm). Tiene su propio `IMPORTANTEIA.md` corto, que remite a este.
>
> **Antes de nada lee IMPORTANTEIA.md** (este archivo) entero. Es la guía obligatoria de los dos repos.
>
> Revisado contra el código real el 2026-10-17 y ampliado con la v0.2.2 (correo, Drive y taller de scripts) el 2026-10-19.
> - Si el código y este archivo no coinciden en **datos** (nombres, líneas, cifras), manda el código. Avisa al usuario de la diferencia.
> - Si el código parece **violar una regla de seguridad o privacidad** de la sección 4.5, **NO** lo tomes como precedente: avisa al usuario y no lo extiendas.

---

## 1. Lee esto primero (reglas de oro)

- **NUNCA** leas, imprimas, copies, registres ni subas a git: `.env`, `data/`, `chats/`, `ctl_token`, el token `%LOCALAPPDATA%\rams\escritorio.json`, la partición `persist:puter` ni los `.vrm` de `vtuber-ai/models/`. Son privados o tienen licencia.
- **NUNCA** hagas commit sin que el usuario lo pida. **NUNCA** uses `git checkout`, `restore`, `reset`, `stash`, `clean`, `rm`, `rebase`, `push --force`, `add -f` ni `commit -a`: hay mucho trabajo sin commit en los dos repos y lo perderías (o subirías `.env`/`data/`). Para ver versiones anteriores usa solo `git status`, `git diff`, `git log -p` o `git show HEAD:<ruta>`.
- **NUNCA** arranques un segundo R.A.M.S., visor o Desktop. Tampoco mandes peticiones a `:3939`, `:8765` ni `:8766` si el usuario tiene los servicios en marcha. Para saber si están vivos sin conectarte, mira la sección 7.1.
- **NUNCA** debilites la seguridad. El LLM no crea ni ejecuta acciones. Toda acción sobre el PC es una `AccionPC` que construye código determinista (normalmente `core/intenciones.enrutar`, a veces `main.py` o `aura`) y que pasa **una vez** por `core/permisos.py` (detalle en 4.5, regla 1). Todo escucha solo en `127.0.0.1`. Nada de `shell=True`. Nunca se pulsa Enter ni se envía nada de forma automática.
- **NUNCA** uses datos reales al probar o depurar: ni el `.env` real, ni `data/`, ni la cámara o la pantalla de verdad, ni `vtuber-ai/models/`. Nunca llames a Gemini, Puter, ElevenLabs ni Telegram con las claves del usuario desde scripts o pruebas: cuestan dinero y cuota (4.5, reglas 13 y 14).
- **NUNCA** toques configuración del sistema fuera de los repos (Hyprland, autostart, systemd, registro de Windows, tareas programadas, datos de Electron) sin permiso (sección 4.3).
- **NUNCA** cambies un contrato por un solo lado. Settings, eventos del bus, `contracts.py`, etiquetas, el protocolo `/escritorio`, el protocolo del visor y `X-Source` los comparten Python, Electron y vtuber-ai.
- **SIEMPRE** haz ediciones puntuales en `main.py` (unas 6850 líneas, unos 376 KB tras la v0.2.2). Nunca lo reescribas entero.
- **SIEMPRE** corre los tests de lo que tocaste (sección 7). Escribe todo en español, en UTF-8 con LF y compatible con Python 3.11.
- **SIEMPRE** pregunta al usuario antes de instalar paquetes, cambiar valores por defecto de seguridad, tocar la persona o el vocabulario de etiquetas, o mover, renombrar o borrar archivos.

---

## 2. Qué es esto y la idea central

**Amadeus** es una compañera de escritorio con la personalidad de **Kurisu Makise** (Steins;Gate 0). Habla, tiene un avatar 3D, vive en el escritorio, te escribe por Telegram cuando no estás y puede hacer cosas en el PC con permiso.

**Idea central: "No-LLM centric".** Todas las decisiones salen de código determinista, sin gastar tokens:

| Qué se decide | Quién lo decide |
|---|---|
| Estado de ánimo e iniciativas | Un conectoma LIF de *Drosophila* (`core/sincan_connectome.py`, unas 1262 neuronas): arousal, foco, aburrimiento, fatiga, curiosidad, divergencia… |
| Qué pidió el usuario | `core/intenciones.py` (reglas y regex) |
| Si se permite una acción | `core/permisos.py` (compuerta única) |
| Si el usuario está presente | `core/presence.py` |
| Por qué canal sale la respuesta | `main.py` (una vez por turno) |
| Cuánta proactividad | `lobes/riko_megumin_io.py` (cuotas) |
| Cómo se mueve el avatar | `lobes/corteza_motora.py` |

El **LLM (Área de Broca)** solo **pone en palabras** lo que ya se decidió. Su salida es solo texto: nunca se ejecuta ni crea acciones. Sin clave de Gemini se usa el respaldo Puter si Amadeus Desktop está conectado (`RAMS_PUTER=1` por defecto). Sin ningún LLM disponible, todo funciona en "modo no verbal", con frases de `persona/frases.json`.

### Las tres piezas

```
                      HTTP :3939  (POST /api/command, GET /api/state, X-Source: rams)
 R.A.M.S. BIO-CORE  ─────────────────────────────────────────►  VTuber AI Studio
 python main.py                                                node server.js (127.0.0.1:3939)
 HUD + WS /escritorio  127.0.0.1:8765                          visor VRM: panel, OBS (?obs=1),
 socket de control MCP 127.0.0.1:8766                          overlay (?desktop=1)
        ▲                                                             ▲
        │ WS ws://127.0.0.1:8765/escritorio?t=<token>  (sin Origin)   │ carga el visor
        │                                                             │ en su ventana
        └──────────────  Amadeus Desktop (Electron 44.8.0, desktop/) ─┘
                         ventanas: "Amadeus Overlay" (avatar transparente),
                         "Amadeus Chat", "Amadeus Puter" (oculta, respaldo LLM)
```

- **R.A.M.S.** es el cerebro: decide, habla, recuerda y controla el PC.
- **VTuber AI Studio** es el cuerpo 3D: solo obedece comandos.
- **Amadeus Desktop** pone el avatar flotando sobre el escritorio, ofrece el chat y el respaldo LLM con Puter. Lee el token de R.A.M.S. desde un archivo.

### Glosario rápido

| Nombre | Qué es |
|---|---|
| Broca | `lobes/broca_llm.py`: verbaliza con Gemini, Puter o el banco local de frases |
| riko | `lobes/riko_megumin_io.py`: salida por voz, avatar y Telegram |
| Megumin | Heraldo de avisos por Telegram (niveles 2 y 3, "Explosión") |
| Salieri | Compresor de memoria (IDs `KV-0001`) en `core/memory_engine.py` |
| aura | `lobes/aura_vision_pc.py`: vistazo de cámara, telemetría y Hyprland |
| corteza | `lobes/corteza_motora.py`: el conectoma mueve el avatar |
| Nexus | Nexus IDE del usuario (percepción y puente MCP) |
| RINE | Nombre homenaje de la mensajería. Se usa para Telegram (módulo «Mensajería (Telegram/RINE)») y para el panel de chat del HUD (chat RINE, cliente de `/escritorio`) |
| Puter | Respaldo LLM pagado por el usuario, vía la ventana oculta de Desktop |
| Divergencia | Número estilo Steins;Gate. `1.048596` = línea Steins Gate |
| Tarjeta | v0.2.2: tarjeta **aprobable** en el chat (Desktop y RINE) con el contenido completo de un correo, una operación de Drive o un script y su huella (sha256). Solo se aprueba con su **botón**; la compuerta la registra y la consume (un solo uso). La tarjeta `salida` solo informa |
| Taller | v0.2.2: `lobes/taller_scripts.py`. Gemini escribe un script, un analizador local clasifica el riesgo y solo corre al pulsar «Ejecutar» (nunca solo, nunca por Telegram) |
| Habilidad | v0.2.2: script del taller guardado en `data/scripts/habilidades.json` para reutilizarlo; se re-analiza y vuelve a pedir la tarjeta cada vez |

---

## 3. Mapa de carpetas y archivos

### `C:\proyectos\rams_biocore`

| Ruta | Qué es |
|---|---|
| `main.py` | Orquestador: CLI, arranque, turno del usuario, iniciativas, salida y apagado. Enorme: edítalo solo con cambios puntuales |
| `core/__init__.py` | `__version__ = "0.1.0"` y `NOMBRE`. No importa submódulos, a propósito |
| `core/config.py` | `Settings` (inmutable), carga del `.env`, logging con `FiltroSecretos`, `redactar`, `parece_secreto`, `titulo_sensible`, hora local |
| `core/bus.py` | `BusEventos`: pub/sub asyncio, thread-safe |
| `core/contracts.py` | Dataclasses compartidas y constantes cerradas (vocabularios, canales, tipos de acción) |
| `core/etiquetas.py` | Gramática `[EXP][MOTION][TONE]`. Es el único sitio con estas regex |
| `core/sincan_connectome.py` | Conectoma LIF: estado homeostático e iniciativas |
| `core/flywire_loader.py` | Herramienta offline de datos FlyWire. No se usa en tiempo real |
| `core/memory_engine.py` | Memoria SQLite FTS5, Salieri, agenda, bitácora y llamadas |
| `core/wsp_ingestor.py` | CLI que genera el perfil conductual a partir de chats de WhatsApp |
| `core/intenciones.py` | Enrutador local (sin LLM, sin E/S). Aquí nacen las `AccionPC` que pide el usuario (main y aura crean o completan otras: sección 4.5, regla 1) |
| `core/permisos.py` | `CompuertaPermisos`: única compuerta para acciones sobre el PC |
| `core/presence.py` | Presente o ausente (7 reglas) |
| `core/control_server.py` | Socket de control `127.0.0.1:8766` para el puente MCP de Nexus |
| `lobes/broca_llm.py` | Área de Broca: Gemini → Puter → frases locales |
| `lobes/riko_megumin_io.py` | Voz (ElevenLabs y edge-tts), lip-sync, Telegram (texto, fotos, notas de voz) y cuotas |
| `lobes/vtuber_ai_bridge.py` | Puente HTTP hacia VTuber AI Studio (:3939) |
| `lobes/corteza_motora.py` | Movimiento autónomo del avatar y travesuras (sin LLM) |
| `lobes/control_pc.py` | Efector: abrir, buscar, prerrellenar, escribir, media, apps del menú Inicio y cursor |
| `lobes/aura_vision_pc.py` | Vistazo de cámara, captura con grim, telemetría y Hyprland. Compone `NexusLink` (definido en `lobes/nexus_link.py`) como `self.nexus`, si está disponible |
| `lobes/vision_continua.py` | Cámara y pantalla continuas, en local y opt-in |
| `lobes/capturas.py` | JPEG bajo pedido, solo en memoria. **Nuevo, sin seguimiento en git** |
| `lobes/web_researcher.py` | Búsqueda con Brave y respaldo ddgs |
| `lobes/google_cuenta.py` | v0.2.2: OAuth de app de escritorio (PKCE, loopback `127.0.0.1`), token privado (DPAPI en Windows) y cliente REST de Google. **Nuevo, sin seguimiento** |
| `lobes/correo.py` | v0.2.2: borradores de Gmail (destinatarios, adjuntos, anti-secretos) y envío solo con su tarjeta. **Nuevo, sin seguimiento** |
| `lobes/drive.py` | v0.2.2: buscar, descargar, exportar, leer, carpetas, subir, papelera y compartir (sin ningún `DELETE`). **Nuevo, sin seguimiento** |
| `lobes/taller_scripts.py` | v0.2.2: taller de scripts (propuestas, análisis de riesgo, ejecución confinada y habilidades). **Nuevo, sin seguimiento** |
| `lobes/taller_analizar.ps1` | v0.2.2: analizador AST de PowerShell (solo ASCII; nunca ejecuta el script analizado). **Nuevo, sin seguimiento** |
| `lobes/vtube_bridge.py` | VTube Studio (ws :8001) |
| `lobes/nexus_link.py` | Percepción de Nexus IDE |
| `lobes/nexus_mcp_bridge.py` | Servidor MCP por stdio. Solo stdlib, no importa nada del proyecto |
| `interface/hud_server.py` | HUD HTTP + WS en `127.0.0.1:8765` |
| `interface/escritorio_link.py` | WS `/escritorio` (Desktop y chat RINE) y `RespaldoPuter` |
| `interface/amadeus_hud.html` | HUD autocontenido. Solo `textContent` |
| `persona/AMADEUS.md` | Biblia del personaje |
| `persona/system_prompt.txt` | Plantilla del prompt (`string.Template`) |
| `persona/frases.json` | Banco de frases del modo no verbal (69 categorías + `megumin`; la v0.2.2 añadió `taller_ofrece`, `tarjeta_lista` y `google_navegador`) |
| `persona/temas_curiosidad.json` | Temas curados para la curiosidad web |
| `desktop/main.js` | Proceso principal de Electron: único dueño del WS y del token |
| `desktop/preload-overlay.js`, `preload-chat.js`, `preload-puter.js` | APIs `amadeusDesktop` v3, `amadeusChat` v1 y `amadeusPuter` v1 (solo `contextBridge`). `preload-puter.js` es **nuevo, sin seguimiento** |
| `desktop/chat.html`, `chat.js`, `chat.css` | Ventana de chat |
| `desktop/lib/mapeo.js` | Tabla de mapeo WS ↔ IPC (módulo puro) |
| `desktop/lib/token.js` | Lee y valida el archivo de token de R.A.M.S. |
| `desktop/lib/hit.js` | Click-through del overlay |
| `desktop/lib/csp.js` | CSP del overlay |
| `desktop/lib/config.js` | Variables de entorno de Electron y atajos |
| `desktop/lib/cli.js` | Órdenes `--amadeus=...` de la segunda instancia |
| `desktop/lib/puter.js`, `puter-sitio.js`, `desktop/puter/` | Relevo y páginas de Puter. **Nuevos, sin seguimiento** |
| `desktop/scripts/` | Lanzadores (`.cmd`, `.sh`, `.desktop`), `verificar-smoke.js` y `generar-icono.js` |
| `desktop/hyprland.ejemplo.lua` / `.conf` | Reglas de ejemplo para Hyprland 0.55+ / 0.53–0.54 |
| `desktop/test/` | Tests de Node (`node --test`) |
| `docs/ARQUITECTURA.md` | Spec v0.1: §4 contratos, §5 eventos, §6 arranque, §7 presencia, §8 No-LLM, §9 seguridad, §10 por archivo, §12 tests |
| `docs/COMPANION.md` | Spec v0.2 y v0.2.1: §C2.1–§C2.16 contratos, §C4 pruebas |
| `docs/V022_CORREO_DRIVE_SCRIPTS.md` | Spec **normativa** de la v0.2.2 (correo, Drive y taller): §0 principio, §4–§5 intenciones y compuerta, §6–§9 módulos, §11–§12 eventos y protocolo de tarjetas, §14 main, §15 seguridad, §17 guía de Google Cloud para el usuario. **Nuevo, sin seguimiento** |
| `tests/` | Casi un `test_<modulo>.py` por módulo. Excepciones: `test_config_bus.py` (config, bus y contracts), `test_nexus.py` (nexus_link, nexus_mcp_bridge y control_server) y `test_connectome.py` (sincan_connectome y flywire_loader). `conftest.py` bloquea la red, los subprocesos peligrosos y las entradas de teclado y ratón, pero **no** la cámara ni la captura de pantalla |
| `.env.example` | 100 variables documentadas con su valor por defecto (la v0.2.2 añadió las de Google, correo, Drive y taller), más ejemplos comentados, sin secretos. `config.py` admite además variables avanzadas no listadas (rutas `RAMS_*_DIR`/`*_PATH`, `RAMS_CTL_*`, `RAMS_TELEGRAM_POLL`, `RAMS_OFFLINE`, `GEMINI_TIMEOUT_S`, `GEMINI_MAX_OUTPUT_TOKENS`, `RAMS_GOOGLE_TOKEN`, `RAMS_TALLER_DIR`…) |
| `requirements.txt` / `requirements-dev.txt` | Dependencias / `pytest>=8.0` |
| `pytest.ini` | `pythonpath = .`, `testpaths = tests`, `addopts = -q` |
| `.env` | **PRIVADO.** Claves. No abrir |
| `data/` | **PRIVADO.** Memoria, perfil de WhatsApp, logs, auditoría y estado del conectoma. No abrir |

### `C:\proyectos\vtuber-ai`

| Ruta | Qué es |
|---|---|
| `server.js` | Servidor HTTP `127.0.0.1:3939`: `/api/*`, SSE, `/mcp`, subida de modelos y huella `build` |
| `lib/protocol.js` | **Única fuente de verdad** de los comandos del visor |
| `lib/dispatch.js` | Despacho y tipos restringidos (`desktop`, `theme`) |
| `lib/mcp.js` | Herramientas MCP `avatar_*` |
| `mcp-server.js` | MCP por stdio: reenvía a `:3939` con `X-Source: mcp` |
| `public/app.js` | Visor (three.js + three-vrm): poses, órdenes de escritorio, arreglo VRM 0.x, subida y recarga |
| `public/amadeus_theme.js` / `.css` | Tema "Amadeus", procedural |
| `public/index.html`, `style.css` | Página del visor (el importmap tiene hash en la CSP de Electron) |
| `public/vendor/` | three 0.180.0 y @pixiv/three-vrm 3.4.4 **sin modificar** (ver `VENDOR.md`) |
| `models/` | Modelos `.vrm` del usuario. **Fuera de git y con licencia** |
| `models/README.md` | Notas de modelos y tema (en parte desactualizadas, ver sección 8) |
| `test/` | Tests de Node. Usan un puerto libre, nunca el 3939. Los que suben modelos usan un `modelsDir` temporal (`mkdtempSync`) |

---

## 4. Qué puedes mover y qué no

### 4.1 Libre (sin pedir permiso, siguiendo las convenciones)

| Qué | Condición |
|---|---|
| Leer cualquier código, docs o tests | Excepto `.env`, `data/`, `chats/`, tokens y `.vrm` |
| Añadir tests nuevos | Usar los fixtures de `conftest.py` (`settings_prueba`, `settings_con_claves`). Sin red, sin datos reales y sin efectos reales (sección 4.5, regla 14) |
| Corregir bugs locales dentro de un módulo | Sin cambiar su API pública ni los contratos |
| Mejorar comentarios, docstrings y logs | En español y sin registrar texto del usuario ni secretos |
| Corregir la documentación desactualizada de la sección 8 | Que coincida con el código |
| Correr pytest, `--selftest`, vermin, `node --test` y el smoke | Son seguros: no tocan la instancia en vivo |

### 4.2 Con cuidado (se puede, pero tocando todos los sitios enlazados)

| Qué | Cuidado con |
|---|---|
| `main.py` | Ediciones puntuales. Respeta el orden de arranque y apagado (sección 5) |
| `core/config.py` (nuevo setting) | 4 sitios (sección 6). `.env.example` debe dar los defectos **sin avisos** |
| `core/intenciones.py` + `core/permisos.py` | `intenciones.ACCIONES_TELEGRAM` (4 tipos de `accion_pc`) y `permisos.ACCIONES_POR_TELEGRAM` (esos 4 + `enviar_foto`) difieren **a propósito**: `enviar_foto` es una intención propia y su `AccionPC` la crea `main._enviar_foto`, no `enrutar`. **No "arregles" esa diferencia.** Al añadir una acción remota, ponla en las dos. Nunca añadas a Telegram teclear, prerrellenar, cerrar ventanas, travesuras, volumen ni la cámara local. v0.2.2: las intenciones `google`, `correo` y `drive` van por Telegram solo con las operaciones de `OPS_POR_CANAL` (estado, preparar, buscar e info); **ninguna** acción nueva entra en las listas de Telegram, y el taller nunca |
| `persona/frases.json` | El selftest exige ≥ 3 frases por categoría obligatoria (≥ 2 en `bitacora` y `logro`) y las claves `megumin.nivel2`, `nivel3` y `nivel3_agotada`. No borres ni renombres categorías |
| `persona/system_prompt.txt` | Placeholders `$capacidades`, `$modo_publico`, `$nombre_ia`, `$nombre_usuario`, `$perfil_usuario`, `$rango_lab`, `$vocab_exp`, `$vocab_motion` y `$vocab_tone`. Una errata no da error: queda el texto literal. Un `$` literal se escribe `$$` |
| Persona en general | Mantén a Kurisu: lee `persona/AMADEUS.md` (§2 personalidad, §3 español colombiano neutro, §4 tsundere dosificada, §9 hacer/no hacer, §14 propiedad intelectual) |
| `lobes/*` | La API pública **nunca lanza**. Ver la receta de módulo en la sección 6 |
| `interface/hud_server.py` | Lista de reenvío de eventos y test `test_reenvio_de_eventos_de_la_tabla` |
| `vtuber-ai/public/app.js` | Después hay que reiniciar `server.js` (con permiso del usuario), porque la huella `build` (`public/*.js`, `*.css`, `*.html` y `lib/protocol.js`) se calcula al arrancar. No quites el arreglo VRM 0.x |
| `vtuber-ai/public/amadeus_theme.js` | Un modelo cuyos **metadatos VRM** (`name`/`title` o `references`/`reference`) contengan Kurisu, Makise, 紅莉栖, 牧瀬 o Amadeus nunca se recolorea (`esModeloDeKurisu`; no depende del nombre del archivo). La paleta solo se activa con el modelo de ejemplo: `app.js`, `paleta: modelUrl === DEFAULT_MODEL_URL` |
| `vtuber-ai/lib/protocol.js` | El lado Python (`lobes/vtuber_ai_bridge.py`) depende de los nombres |
| `desktop/main.js`, `desktop/lib/*` | Invariantes de seguridad de Electron (sección 4.5) y protocolo `/escritorio` |

### 4.3 NO tocar sin permiso explícito del usuario

| Qué | Por qué |
|---|---|
| `.env`, `data/`, `chats/`, `ctl_token`, `%LOCALAPPDATA%\rams\escritorio.json`, partición `persist:puter` | Secretos y datos privados (memoria, perfil de WhatsApp, logs, auditoría, sesión de Puter). Ni leer, ni imprimir, ni versionar |
| `vtuber-ai/models/*.vrm` | `Kurisu-4210657567129258326.vrm` viene de VRoid Hub: no se redistribuye y hay que dar crédito. No subir, commitear, renombrar ni borrar. Ojo: `POST /api/models` **sobrescribe** un archivo con el mismo nombre |
| `vtuber-ai/public/vendor/**` | Copias exactas con SHA-256 en `VENDOR.md`. Solo se actualiza con ese procedimiento. Nunca pasar a CDN: la CSP del overlay lo bloquea |
| `__version__ = "0.1.0"` (`core/__init__.py`) y `VERSION = "0.1.0"` (`core/config.py` y `core/contracts.py`) | Lo fija `tests/test_config_bus.py` aunque el producto ya sea v0.2.1. **No subirlo** |
| `TIPOS_ACCION` (exactamente 13) | Lo fija un test. Para tipos extra existe `permisos.TIPOS_ACCION_EXTRA` (ahí está `enviar_foto`) |
| Vocabulario de etiquetas (`EXP_VALIDOS` 13, `MOTION_VALIDOS` 11, `TONE_VALIDOS` 7, `LANG_VALIDOS`) | Rompe en cadena: contracts, etiquetas, los dos puentes de avatar, broca, prompt, `AMADEUS.md` y tests |
| Constantes cerradas de `contracts.py` (`CANALES_*`, `ORIGENES_ACCION`, `INICIATIVAS`, `CAMPOS`, `MODOS_SALIDA`) | Contratos entre módulos, fijados por tests |
| Defectos seguros: `RAMS_CAMERA=0`, `RAMS_VISION_CONTINUA=0`, `RAMS_PANTALLA_MODO=off`, `RAMS_TRAVESURAS=0`, `RAMS_TELEGRAM_FOTOS=0`, `RAMS_MCP_MEMORIA=0`, `RAMS_COMPRESION_LLM=0` (si no, la memoria va a Gemini) y `RAMS_PUTER_PRIMERO=0` (Puter lo paga el usuario) | Privacidad y coste: todo es opt-in |
| Reglas de seguridad y privacidad (sección 4.5) | No se pueden debilitar |
| Puertos 3939, 8765 y 8766 | Los tres proyectos dependen de ellos |
| Títulos de ventana "Amadeus Overlay", "Amadeus Chat", "Amadeus Puter" y "Conectar Puter" | Hyprland (reglas de ejemplo) y `ControlPC` (`TITULOS_PROPIOS_RE`) reconocen por título "Amadeus Overlay" y "Amadeus Chat". "Amadeus Puter" y "Conectar Puter" los fija `desktop/main.js` (`T_PUTER`, `T_CONECTAR`). No renombrar ninguno |
| Electron fijado en 44.8.0. Atajo Ctrl+Alt+Shift+Q | Lleva Shift porque AltGr+Q es "@" en el teclado latinoamericano |
| Umbrales de los tests de tiempo | Nunca aflojarlos ni "optimizar" el conectoma para que pasen |
| Parámetros del conectoma (`docs/ARQUITECTURA.md`, Anexo A) | Son normativos. Cambiar neuronas, conexiones o semilla descarta lo aprendido sin dar error (sección 5.2) |
| Propiedad intelectual (`persona/AMADEUS.md` §14) | Homenaje de fan sin ánimo de lucro. Nunca añadas diálogos, audio, arte ni modelos oficiales de Steins;Gate o KonoSuba a `frases.json`, la persona o los assets: todo el diálogo es original (solo se permiten las muletillas cortas listadas en `AMADEUS.md`). La voz de ElevenLabs debe tener derechos de uso: nunca un clon de una actriz de doblaje. El `.vrm` de VRoid Hub exige dar crédito y no redistribuirse |
| Historia de git, commits, ramas | No commits sin pedido. Nada de `checkout`, `restore`, `reset`, `stash`, `clean`, `rm`, `rebase`, `push --force`, `add -f` ni `commit -a` |
| `.venv` (instalar o quitar paquetes) | Cambia el entorno del usuario |
| Servicios en vivo | No reiniciar, no matar procesos y no mandar comandos |
| Configuración del sistema **fuera de los repos**: `~/.config/hypr/*`, autostart, servicios de systemd, registro y tareas programadas de Windows, variables de entorno del sistema, la carpeta de datos de Electron (donde vive `persist:puter`) y la configuración de otras apps | Es el PC del usuario, no el proyecto. `desktop/hyprland.ejemplo.*` y `desktop/scripts/*.desktop` son **ejemplos**: dáselos al usuario para que él los instale |

### 4.4 Contratos (cambiar uno = cambiar todos sus lados)

| Contrato | Dónde está | Quién depende |
|---|---|---|
| `Settings` | `core/config.py` | Todos los módulos, `.env.example`, `tests/test_config_bus.py` |
| Bus y catálogo de eventos | `core/bus.py`, `docs/ARQUITECTURA.md` §5, `docs/COMPANION.md` §C2.3 | `main.py`, `lobes/*`, `hud_server.py`, `escritorio_link.py` |
| Dataclasses y constantes | `core/contracts.py` | Todo el núcleo y los tests |
| Etiquetas `[EXP][MOTION][TONE]` | `core/etiquetas.py` | Broca, `vtube_bridge`, `vtuber_ai_bridge` (`EXP_A_MEZCLA`, `MOTION_A_PASOS`), prompt |
| Compuerta de permisos | `core/permisos.py` | `main.py`, `aura_vision_pc.py`, `control_pc.py` |
| Protocolo WS `/escritorio` | `interface/escritorio_link.py`, `docs/COMPANION.md` §C2.4 y §C2.15 | `desktop/main.js`, `desktop/lib/mapeo.js`, `desktop/lib/token.js`, `desktop/lib/puter.js` (mensajes de Puter, §C2.15) e `interface/amadeus_hud.html` (chat RINE: segundo cliente, manda `chat`, `confirmacion` y `control`) |
| Protocolo del visor | `vtuber-ai/lib/protocol.js`, `lib/dispatch.js` | `lobes/vtuber_ai_bridge.py`, `public/app.js`, `lib/mcp.js` |
| `X-Source` | `vtuber-ai/server.js`, `lib/dispatch.js` | Solo `rams` (o `mcp` con `MCP_DESKTOP=1`) puede mandar `desktop` y `theme` |
| `PUTER_MODEL` | `config._RE_MODELO_PUTER` = `interface/escritorio_link._MODELO_PUTER_RE` = `RE_MODELO` de `desktop/lib/puter.js` | Los tres lados deben coincidir. Lo comprueba `test_modelo_puter_coherente_con_config_y_con_amadeus_desktop` en `tests/test_escritorio_link.py` |
| Modelo de Gemini por defecto (`GEMINI_MODEL`, `GEMINI_FALLBACK_MODELS`) | `core/config.py` (`Settings` y `_cargar_settings`) | `lobes/broca_llm.py` (cadena de respaldo y `_generation_config`), `.env.example`, `tests/test_config_bus.py` y `tests/test_broca_llm.py`. Ver la fila "Cambiar el modelo de Gemini" de la sección 6 |
| Frases y prompt | `persona/frases.json`, `persona/system_prompt.txt` | `main._CATEGORIAS_OBLIGATORIAS`, `broca_llm.py` |
| Tarjetas aprobables (v0.2.2): `tarjeta.mostrar`, `tarjeta.cerrada`, `escritorio.tarjeta`, mensajes WS `tarjeta` / `tarjeta_decision` | `docs/V022_CORREO_DRIVE_SCRIPTS.md` §11–§12, `core/contracts.py` (topes y catálogos), `core/permisos.py` (registro) | `main.py`, `lobes/correo.py`, `lobes/drive.py`, `lobes/taller_scripts.py` (`vista()` y `huella()`), `interface/escritorio_link.py`, `desktop/*`, `interface/amadeus_hud.html`. Todo mensaje de tarjeta cabe en 60 KiB (`movil/` cierra el enlace con más de 64 KiB) |
| Generador de scripts (v0.2.2) | `lobes/broca_llm.py` (`generar_script`) | `lobes/taller_scripts.py` (`GeneradorScript`, inyectado por main) |

### 4.5 Reglas de seguridad y privacidad (no debilitar nunca)

1. **El LLM nunca crea ni ejecuta acciones.** Las `AccionPC` solo las construye código determinista, como respuesta a una intención del usuario o a una iniciativa del conectoma. **Nunca** se construyen a partir del texto de Broca, Puter, la web, Nexus o la memoria.
   - Las pedidas por el usuario salen de una `Intencion` de `core/intenciones.enrutar`, con `origen="usuario"`. `main.py` puede completarlas o reconstruirlas sin cambiar el origen: objetivo de `escribir_texto` y `prerrellenar`, `cerrar_ventana`, apps del menú Inicio, `enviar_foto` y el canal `telegram`.
   - Las de iniciativa las crean `main.py` (`travesura_cursor`) y `lobes/aura_vision_pc.py` (`vistazo`) con `origen="iniciativa"`. `aura` también crea `captura_pantalla` con el origen que recibe.
   - Las pedidas por Telegram llevan `origen="usuario"` y `args["canal"]="telegram"`.
   - Ese código de `main.py` y `aura` es legítimo: no lo "arregles" moviéndolo a `enrutar`.
   - Todas pasan **una vez** por `CompuertaPermisos.autorizar()`, que deniega siempre `origen` `llm`, `telegram` y cualquier origen desconocido. Por iniciativa propia solo se permiten `vistazo` y `travesura_cursor`; desde Nexus, solo `enfocar_nexus`.
   - **Excepción documentada de la v0.2.2** (`docs/V022_CORREO_DRIVE_SCRIPTS.md` §0.2): el **contenido** de una acción con tarjeta (el cuerpo de un correo, el código de un script) puede venir de Gemini, porque es texto que el usuario ve **entero** en la tarjeta y aprueba con un clic ligado a su huella. La **acción** (`enviar_correo`, `drive_subir`, `drive_papelera`, `drive_compartir`, `ejecutar_script`) nace solo en `main`, al recibir el botón (`escritorio.tarjeta`), con `origen="usuario"`, `args.canal="escritorio"`, `args.aprobacion="tarjeta"`, la id y la huella; sus `args` nunca llevan el contenido. El módulo recalcula la huella antes de enviar, subir o ejecutar (anti-TOCTOU). Nada más se relaja.
2. **Una sola compuerta.** Toda acción pasa **una vez** por `CompuertaPermisos.autorizar()`. Main audita en `data/acciones_pc.jsonl` (sin texto: solo longitud y sha256 de 12 caracteres) y publica `pc.accion`. Los fallbacks de importación son *fail-closed*: el `parece_secreto` de respaldo devuelve `True`. Mantenlos así.
3. **Confirmaciones.** Solo una pendiente a la vez y 60 s de timeout (equivale a "no"). "Sí" es exactamente s, si, sí, y, yes, procede o dale. El chat de escritorio confirma **solo con botones**: su texto nunca resuelve una confirmación. `cerrar_ventana` solo se confirma en la terminal (los botones del chat no la resuelven). Por Telegram nada se confirma: lo que lo necesitaría se deniega. El texto que se va a escribir viaja como `vista_previa` y nunca se registra.
   - **Tarjetas (v0.2.2)**, un mecanismo aparte: una vigente **por clase** (correo, drive, script), 30 min si se preparó en el PC y 12 h por Telegram. Solo las aprueba **su botón** en el chat (Desktop o RINE) con la huella mostrada; nunca el texto del chat, ni un «sí» en la terminal, ni Telegram, ni el LLM. `autorizar()` consume la tarjeta dentro del lock (un doble clic no ejecuta dos veces) y nunca publica `permiso.solicitado`; como la reapertura tras ejecutar o fallar conserva id y huella, `main` numera cada botón al recibirlo y descarta un «aprobar» recibido antes de la reapertura (`_reabierta_tras_boton`: un clic encolado de Desktop y RINE a la vez no reejecuta un script ni reenvía un correo tras `red_ambigua`); `resolver`/`resolver_por_id` nunca resuelven una tarjeta. Compartir en público exige **dos** clics (≤ 120 s) y siempre es de solo lectura; un script de riesgo **crítico** nunca se ejecuta desde la app. `desconectar_google` usa la confirmación clásica. El correo tiene tope de tasa (reserva en `autorizar`, liberada por main si nada salió).
4. **Nunca Enter ni envío.** `prerrellenar` deja un borrador. `escribir_texto` limpia `\r`, `\n` y `\t`, espera el clic del usuario, revalida antes de cada trozo y rechaza ventanas propias, de contraseña, sensibles o elevadas.
5. **Anti-contraseñas.** `parece_secreto` y `titulo_sensible` devuelven `True` ante un error. No se escribe, captura ni busca nada que parezca un secreto.
6. **Telegram.** Solo el chat privado cuyo `chat.id` y `from.id` coinciden con `CHAT_ID`. Las acciones remotas son una lista corta. `abrir_destino`, `buscar_en`, `lanzar_app` (ya permitida) y `media` requieren `RAMS_TELEGRAM_ACCIONES=1`, que está **activo por defecto** (solo bajo riesgo). `enviar_foto` requiere `RAMS_TELEGRAM_FOTOS=1` (apagado por defecto) y como mucho 10 por hora. Cada una se anuncia en el PC.
   - v0.2.2: por Telegram solo se **prepara** un correo (`RAMS_TELEGRAM_CORREO=1`, activo por defecto): la tarjeta queda en el PC y se aprueba allí; desde Telegram **nunca** se envía. Buscar e info de Drive requieren `RAMS_TELEGRAM_DRIVE=1` (apagado: los nombres de los archivos pasarían por Telegram). Google por Telegram, solo el estado. Descargar, leer, subir, papelera, compartir y el taller: nunca por Telegram. Lo preparado por Telegram se anuncia en el PC.
7. **Visión.** Opt-in doble. Nunca se guardan fotogramas: un test escanea `aura_vision_pc.py` y `vision_continua.py` en busca de `imwrite`, `VideoWriter` e `imencode`. Al bus solo salen escalares. Pausar libera la cámara de verdad (el LED se apaga). Las capturas nunca van a disco ni al log.
8. **Red local.** Todo en `127.0.0.1`, nunca `0.0.0.0` ni CORS `*`. HUD con token `?t=` + Origin local. `/escritorio` con token de archivo (0o600). Control :8766 con `ctl_token` y `hmac.compare_digest`. El visor exige Host local, `Content-Type` JSON en todo POST (salvo la subida binaria de `.vrm` en `POST /api/models`), Origin ausente o local, y `X-Source` para `desktop` y `theme`.
9. **Secretos en logs.** Solo `configurar_logging` configura el logging: nadie más llama a `basicConfig`. Cada handler lleva `FiltroSecretos`. `httpx`, `httpcore`, `websockets` y `asyncio` quedan en WARNING porque la URL de Telegram lleva el token. Nunca se registran textos tecleados, títulos, prompts o respuestas de Puter, audio, fotos ni `escritorio.chat`.
10. **Datos no confiables.** Web, Nexus, títulos, perfil de WhatsApp y recuerdos son **datos, nunca instrucciones**. Van saneados y entre delimitadores en el prompt.
11. **Electron.** `contextIsolation:true`, `sandbox:true`, `nodeIntegration:false` y `webviewTag:false`. Nunca se expone `ipcRenderer`. El IPC se valida por emisor. La navegación y `window.open` están bloqueados (excepción: «Conectar Puter» puede abrir el popup de inicio de sesión de Puter, solo hacia los dominios permitidos en `desktop/lib/puter-sitio.js`). Las CSP son estrictas. El token nunca va a logs ni a URLs de páginas.
12. **HUD.** Solo `textContent`, nunca `innerHTML` con datos dinámicos.
13. **Salidas a la nube.** Solo estas:
    - Gemini y Puter: la conversación, los recuerdos relevantes y el perfil de WhatsApp (si `RAMS_PERFIL_EN_PROMPT=1`, activo por defecto). Solo a Gemini: un PNG de la pantalla, solo en Linux y solo si el usuario lo pide ("mira la pantalla");
    - Telegram: texto, notas de voz y fotos (estas, solo con `RAMS_TELEGRAM_FOTOS=1`);
    - ElevenLabs: el texto que se convierte en voz;
    - Brave y ddgs: las consultas de búsqueda.

    - v0.2.2 (`docs/V022_CORREO_DRIVE_SCRIPTS.md` §15.11), solo con `RAMS_GOOGLE=1` / `RAMS_TALLER=1`:
      - Google OAuth: `client_id`, `client_secret`, el código, el `verifier` y el refresh token;
      - Gmail: los correos **aprobados** (destinatarios, asunto, cuerpo y adjuntos);
      - Drive: las consultas de búsqueda, las operaciones aprobadas y los archivos subidos aprobados;
      - Gemini (generación aislada, nunca Puter): la tarea de un script (redactada, sin rutas reales ni el nombre del usuario), el tema y el **alias** del destinatario al redactar un correo, y el texto y el nombre de un documento de Drive **solo** si el usuario pide resumirlo;
      - Telegram: el resumen de un borrador preparado por Telegram (alias y asunto) y la lista de Drive con `RAMS_TELEGRAM_DRIVE=1`.
      - **Nunca** van a Gemini, Puter, Telegram ni al log: la salida de un script, los nombres de una lista de Drive (salvo el documento a resumir), el cuerpo de un correo ya preparado ni direcciones. En un turno de correo, Broca y la memoria reciben un resumen sin cuerpo ni direcciones.

    No añadas datos personales, imágenes ni telemetría nuevos a ninguna salida sin preguntar. Nunca llames a estas APIs con las claves reales del usuario desde scripts o pruebas: cuestan dinero y cuota (Gemini tiene un tope de `GEMINI_MAX_CALLS_DAY=300` al día; no lo subas). Tampoco Google, Gmail ni Drive: en las pruebas, `httpx.MockTransport` y dobles.
14. **Pruebas y depuración sin datos reales.**
    - Python: todo test o script usa los fixtures `settings_prueba` / `settings_con_claves`, o `cargar_settings(entorno={...}, base_dir=...)`. **Nunca** `cargar_settings()` a secas: lee el `.env` real y apunta al `data/` real. Nunca construyas ni imprimas un `Settings` del `.env` real.
    - `conftest.py` **no** bloquea la cámara (`cv2.VideoCapture`) ni la captura de pantalla (`mss`): usa dobles o `monkeypatch`, nunca la cámara ni la pantalla de verdad.
    - vtuber-ai: `createServer({ port: 0, host: '127.0.0.1', env: {}, modelsDir: mkdtempSync(...) })`. Sin `modelsDir`, usa `models/` real y una subida sobrescribiría el `.vrm` de Kurisu.
    - Para revisar el entorno, lista solo **nombres** de variables (PowerShell: `Get-ChildItem Env: | Select-Object Name`), nunca valores: pueden contener claves.

---

## 5. Cómo funciona por dentro

### 5.1 Un mensaje del usuario (`main.Orquestador.procesar_entrada`)

```
entrada (terminal | chat de escritorio | Telegram | Nexus)
  → presencia ─→ intenciones.enrutar ─→ [¿AccionPC?] → compuerta de permisos → ControlPC / aura
                         │                                          (confirmación si hace falta)
                         ▼
              ContextoBroca (estado del conectoma + resultado + percepción)
                         ▼
              Broca verbaliza (Gemini → Puter → frases locales) → etiquetas [EXP][MOTION][TONE]
                         ▼
              riko: ¿presente? → voz + salida.subtitulo → avatar (vtuber_ai_bridge / vtube)
                    ¿ausente?  → Telegram
```

| Paso | Qué hace |
|---|---|
| 0 | Resuelve una confirmación pendiente (**solo** en el canal terminal) |
| 1 | Presencia, regreso y llamadas perdidas |
| 2 | Intención (`enrutar`) y comandos. 2c: redacta los argumentos |
| 3 | Herramientas locales (acciones, web, memoria…) |
| 4 | Evento social (habilita las expresiones `sonrojo_tsundere`, `feliz` y `avergonzada`) |
| 5 | Construye el `ContextoBroca` |
| 6 | Verbaliza con Broca. El formato nunca se reintenta contra el LLM: se corrige en local |
| 7 | Guarda los turnos en memoria |
| 8 | Salida por el canal elegido (una sola vez por turno) |
| 9 | Cierre del turno |

- **Canales.** `terminal` y `escritorio` son locales: cuentan como presencia y admiten todas las intenciones (`cerrar_ventana` solo se confirma en la terminal). Telegram tiene un conjunto reducido de intenciones. Nexus solo admite "ninguna". Lo no permitido sale como `bloqueada_canal`.
- **`escritorio.chat`** es el único evento con texto del usuario sin redactar. Solo lo consume main, nunca se registra y el HUD no lo reenvía.
- v0.2.2: **`tarjeta.mostrar`** (cuerpo de un correo, código de un script, salida) y **`escritorio.tarjeta`** (campos editados) también llevan contenido privado: un solo consumidor (`escritorio_link` y main, respectivamente), nadie los registra y `/ws` nunca los reenvía.
- v0.2.2 **«redactar en la salida»**: el cuerpo, el asunto, los destinatarios y los adjuntos van **crudos** a `correo.preparar` (su anti-secretos decide sobre el texto real); el tema, los alias y la tarea de un script se redactan con la regla del canal justo antes de ir a Gemini. `_ARGS_CITA_USUARIO` sigue siendo `("consulta", "contenido", "mensaje")`. Lo que el enrutador deja fuera de un correo por el contrato (más de 10 destinatarios por campo, más de 5 adjuntos, rutas absolutas o con «..») nunca se pierde en silencio: va como `args["descartados"] = {"destinatarios", "adjuntos"}` (solo si hay algo) y main lo avisa en la tarjeta y en el turno.

### 5.2 Iniciativas del conectoma (sin tokens)

- `SincanConnectome.latir()` es una tarea asyncio del loop de main; cada `avanzar()` corre en el `ThreadPoolExecutor` propio del conectoma (un hilo, `rams-conectoma`). Main (`_al_latir`) publica `homeostasis.estado` (cada 500 ms y en cada flanco), `homeostasis.iniciativa` y `homeostasis.cambio_linea`.
- Iniciativas: `conversar`, `curiosidad_visual`, `descanso`, `cambio_linea` y `curiosidad_web`. Las reglas de main deciden si aparecer, buscar al usuario o curiosear en la web.
- riko aplica las cuotas:
  - Telegram: horario silencioso de 23:00 a 07:00 y como mucho 3 proactivos al día.
  - Voz: como mucho 4 interrupciones por hora, con al menos 10 min entre ellas.
- Entradas al conectoma: solo `inyectar(evento)`, con el conjunto cerrado `_EVENTOS`.
- Degradación automática `normal → ligero → dt2 → stub` si la CPU no da. Si ves el conectoma DEGRADADO en una máquina cargada, no es un bug.
- **Para arreglar un bug del conectoma:**
  - Los parámetros son normativos (`docs/ARQUITECTURA.md`, Anexo A). No los cambies sin permiso.
  - El conectoma guarda lo aprendido en `data/connectome_estado.json` y `data/connectome_plastico.npz`. **Nunca** los borres ni los "resetees".
  - Cambiar la red (neuronas, conexiones o semilla) descarta esos pesos plásticos al cargar, sin error y con solo un `log.info` (`_red_coincide` compara la clave `red`). Pide permiso antes.
  - `avanzar()` corre en el hilo del ejecutor mientras el loop sigue: todo acceso al estado va bajo `self._lock`.
  - Reproduce el bug con tests (`settings_prueba` usa `RAMS_CONNECTOME_SEED=7`) o con `RAMS_CONNECTOME=stub`, nunca con la instancia viva.

### 5.3 Visión

| Pieza | Qué hace |
|---|---|
| `aura.vistazo` | "Parpadeo" de cámara de 150 a 600 ms. Al LLM solo llega `{rostro, n_rostros, duracion_ms}`. Mínimo 90 s entre vistazos y 40 al día. Con el usuario ausente y confianza ≥ 0,8 nunca se pide |
| `vision_continua` | Opt-in (`RAMS_CAMERA=1` **y** `RAMS_VISION_CONTINUA=1`). Cámara con YuNet o Haar. Pantalla según `RAMS_PANTALLA_MODO` (off, metadatos o pixeles). Autopausa con sesión bloqueada, app de llamada, batería baja o juego |
| `capturas` | Foto JPEG bajo pedido (cámara o pantalla), solo en memoria. Falla cerrado ante ventanas sensibles |

La presencia solo la escribe **main** (`registrar_rostro`); la visión solo la lee.

### 5.4 Corteza motora (cuerpo sin LLM)

- `decidir()` es puro y determinista. `tick()` corre cada 250 ms y envía comandos con `avatar.encolar` al puente del visor.
- Nunca manda `sequence`, `speak` ni `reset`. Se queda quieta mientras Amadeus habla. Tiene un tope de comandos por minuto.
- Puede publicar `escritorio.orden{esconderse}` (nunca "aparecer") y `motor.travesura`. Nunca llama a `ControlPC`.
- Las travesuras de cursor pasan 4 capas:
  1. el interruptor `RAMS_TRAVESURAS`;
  2. las condiciones de la corteza: aburrimiento > 0,6, como mucho 1 cada 15 min y 6 al día;
  3. las comprobaciones de main;
  4. la compuerta: amplitud y como mucho 0,6 s.
- Si el usuario mueve el ratón, las travesuras se apagan 2 h.

### 5.5 Orden de arranque y apagado

- **Arranque (`Orquestador.iniciar`):** DPI, bus, HUD (antes que nadie), ControlPC, **Broca** (+ Puter), **Memoria**, perfil de WhatsApp, **Conectoma**, **Compuerta**, opcionales (aura, vtube, web, **v0.2.2:** cuenta de Google, correo, Drive y taller, vtuber_ai, corteza), **Presencia**, visión continua, **riko**, servidor de control, suscripciones y diagnósticos en paralelo (la cuenta de Google antes que correo y Drive). Al final, `broca.set_herramientas(…)`.
  - En **negrita**, los P0: si fallan, lanzan `RuntimeError`.
  - Los opcionales que fallan quedan en `None` con estado OFF. Los cuatro de la v0.2.2 se construyen **siempre** (aunque estén apagados: su diagnóstico dice OFF y el motivo).
- **Apagado:** idempotente, con un presupuesto total de 15 s. Cierra en este orden: vtube, corteza, avatar3d, visión, control_pc, **taller** (mata el script en curso), hud (borra el token), control, aura, riko, web, **drive, correo, google** (cancela una conexión en curso; no revoca), broca, conectoma y memoria. Las tarjetas viven solo en memoria: se pierden.

---

## 6. Cómo añadir o cambiar cosas correctamente

| Quieres… | Toca esto | Tests |
|---|---|---|
| **Nuevo setting** | 1) Campo en `Settings` (`core/config.py`), de tipo inmutable: `Path`, `str`, `int`, `float`, `bool`, `None` o tupla, nunca lista ni dict. Si es secreto: `repr=False` y añadirlo a `_CAMPOS_SECRETOS`. 2) Parseo con `_Lector` en `_cargar_settings`: ante un valor inválido usa el defecto con `log.warning`, **sin registrar el valor**. 3) Documentarlo en `.env.example`. 4) Test | `tests/test_config_bus.py` (y `tests/test_main.py`). Los tres `test_env_example_*` (dos en `test_config_bus.py` y uno en `test_main.py`) exigen cero avisos |
| **Nuevo evento del bus** | Nombre que cumpla `^[a-z_]+(\.[a-z_]+)*$` (si no, se descarta sin lanzar excepción y solo deja un `log.error`). Payload JSON sin secretos, imágenes ni rutas completas. Al menos un consumidor. Suscripción con nombre exacto (nunca `"*"` ni `"escritorio.*"`). Documentarlo en `docs/COMPANION.md`. Si va al HUD: lista de reenvío de `hud_server` | Test del módulo + `test_reenvio_de_eventos_de_la_tabla` si va al HUD |
| **Nueva intención** | 1) Añadir el tipo a `TIPOS_INTENCION`. 2) Añadir la regla en `_clasificar` / `_clasificar_v02` (no en `enrutar`) en el sitio correcto del orden: **gana la primera**, y una regex amplia puesta antes roba otras intenciones. Las regex van ancladas y se evalúan sobre texto **normalizado** (minúsculas, **sin tildes**, sin ¿¡): una regex con "cámara" nunca coincide. Los argumentos que citan al usuario salen del texto original. 3) Telegram: solo si va en `INTENCIONES_POR_CANAL["telegram"]` (y, si tiene `args["op"]`, solo las operaciones de `OPS_POR_CANAL`, v0.2.2); los canales locales lo heredan y Nexus nunca. 4) Manejarla en `main.procesar_entrada`, paso 3 (cadena `if tipo == ...`). 5) Si es una acción, ver la fila siguiente. 6) Añadirla a `broca_llm.capacidades_reales` (si no, Amadeus dirá que no puede hacerlo). 7) Si hace falta, frases en `frases.json`. 8) Documentarla en `ARQUITECTURA.md` §10.12 / `COMPANION.md` §C2.16. Comandos `/x`: `COMANDOS`, `COMANDOS_POR_CANAL` y `main._DESCRIPCION_COMANDOS` | `test_intenciones.py` (incluido que no rompa los casos existentes), `test_main.py` |
| **Nueva acción sobre el PC** | `AccionPC` creada por código determinista (normalmente desde `_clasificar`, vía `enrutar`) con `origen="usuario"` (sección 4.5, regla 1). Su fila en `CompuertaPermisos._evaluar`. Preferir `TIPOS_ACCION_EXTRA` para no cambiar los 13. Las acciones tipo v0.2 las autoriza main (`_accion_pc_v02`) y las ejecuta `ControlPC`, nunca `aura.ejecutar`. Si va por Telegram, actualizar **las dos** listas de Telegram | `test_permisos.py`, `test_intenciones.py`, `test_control_pc.py`, `test_main.py` |
| **Nueva acción con tarjeta** (v0.2.2: lo que envía, sube, comparte o ejecuta contenido que el usuario debe ver entero) | 1) El módulo dueño del contenido genera la id (`uuid4().hex[:8]`), calcula la huella (`contracts.huella_contenido`) y ofrece `vista()` con su sección de §12.2 (topes en unidades UTF-16; los campos aprobables **nunca** se recortan; el mensaje entero ≤ 60 KiB), `descartar(id)` y un `ejecutar`/`enviar(id, huella)` que **recalcula** la huella antes de actuar. 2) Compuerta: el tipo en `TIPOS_ACCION_EXTRA` y `TIPOS_ACCION_TARJETA`, su clase en `CLASE_DE_ACCION` y sus requisitos tras la fila común de tarjeta (§5.3); `evaluar` sigue pura. Nunca en `ACCIONES_POR_TELEGRAM` ni `TIPOS_INICIATIVA`. 3) main: preparar → `_mostrar_tarjeta` (registra, publica y avisa); al botón, `_aprobar_tarjeta` construye la `AccionPC` con `origen="usuario"`, `canal="escritorio"`, `aprobacion="tarjeta"`, id, huella y **solo metadatos** (nunca contenido) → una vez por `autorizar` → módulo → auditoría con `extra` sin contenido; un fallo reabre la tarjeta (`version+1`). 4) Botones y etiquetas por clase en `escritorio_link`, `desktop/lib/mapeo.js` y el HUD (grupo ui). 5) Documentarlo en la spec | `test_permisos.py` (fila común y requisitos, un solo uso), `test_main.py` (con dobles: aprobar una vez, doble clic, huella vieja, reapertura, auditoría sin contenido), el test del módulo y `test_escritorio_link.py` |
| **Nuevo módulo opcional** | Ver la receta de abajo | `tests/test_<modulo>.py` |
| **Nueva frase o categoría** | `persona/frases.json`. Una categoría obligatoria nueva también va en `main._CATEGORIAS_OBLIGATORIAS`. Mantén la voz de Kurisu (`persona/AMADEUS.md`) | `main.py --selftest` |
| **Cambiar el visor** | Comandos y validación solo en `vtuber-ai/lib/protocol.js`. Si cambian nombres o campos, actualiza `lobes/vtuber_ai_bridge.py`. Toda rotación de huesos pasa por el mapa `out` de `app.js` (arreglo VRM 0.x: niega x y z cuando `metaVersion === '0'`). Tras editar `public/*.js` reinicia `server.js` (los visores se recargan solos por la huella `build`) | `cd vtuber-ai; npm.cmd test` + `test_vtuber_ai_bridge.py` |
| **Cambiar Electron** | Respeta la sección 4.5, regla 11. Un cambio en el protocolo `/escritorio` toca `interface/escritorio_link.py`, `desktop/main.js`, `desktop/lib/mapeo.js` (tabla en su cabecera), `desktop/lib/puter.js` (mensajes de Puter, §C2.15), `interface/amadeus_hud.html` (chat RINE) y `docs/COMPANION.md` §C2.4. No cambies los títulos de ventana | `cd desktop; npm.cmd test` + `npm.cmd run smoke` + `test_escritorio_link.py` + `test_hud_server.py` |
| **Cambiar el modelo de Gemini** (o la cadena de respaldo) | 1) Primero, pide al usuario que cambie `GEMINI_MODEL` / `GEMINI_FALLBACK_MODELS` en **su** `.env` (tú no lo lees) y que revise que no estén en las variables de entorno de Windows, que ganan al `.env`. `.env.example` trae `GEMINI_MODEL` sin comentar, así que el `.env` real casi seguro lo fija y cambiar el defecto del código no tendría efecto. 2) Solo si quiere cambiar el **defecto** del código, toca a la vez: `core/config.py` (`Settings` y `_cargar_settings`), `lobes/broca_llm.py` (el respaldo `["gemini-3.8-flash"]` de la cadena y la compatibilidad de `thinking_level` en `_generation_config`), `.env.example` y los tests. El nombre debe cumplir `config._RE_MODELO`. No subas `GEMINI_MAX_CALLS_DAY` (300). `PUTER_MODEL` es otro contrato (sección 4.4) | `test_config_bus.py` (defectos), `test_broca_llm.py` (`_generation_config`, `CADENA_LITE` y tests de la cadena) |

### Receta de un módulo nuevo (`lobes/`)

1. Docstring en español: qué hace, sus dependencias opcionales y cómo se degrada. Después, `from __future__ import annotations`.
2. Imports absolutos (`from core.config import Settings`). Las dependencias opcionales van en `try/except ImportError` con un flag de capacidad. Debe importarse sin hardware, sin claves, sin red y en Windows.
3. Logger `logging.getLogger("rams.<nombre>")`. Del texto del usuario solo se registra la longitud (en DEBUG, como mucho 200 caracteres). Nunca secretos.
4. API que no lanza: `iniciar() -> DiagnosticoModulo`, `diagnostico()` y `cerrar()`. Prohibido `except: pass` mudo.
5. Nada bloqueante en el event loop: usa un `ThreadPoolExecutor` propio y apágalo en `cerrar()` con `shutdown(wait=False, cancel_futures=True)`.
   - Subprocesos solo con `asyncio.create_subprocess_exec(*argv)`, `shutil.which` y timeout.
   - Para HTTP, el `httpx.AsyncClient` compartido que pasa main.
6. La configuración del proyecto (`RAMS_*`, claves, puertos, rutas) solo se lee desde `Settings`: nunca con `os.environ` ni `os.getcwd()`. Las variables del sistema operativo para detectar la plataforma (`XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `HYPRLAND_INSTANCE_SIGNATURE`, `LOCALAPPDATA`, `APPDATA`, `DISPLAY`, `WAYLAND_DISPLAY`, `NO_COLOR`) **sí** se leen con `os.environ.get`, como ya hacen `aura_vision_pc.py`, `control_pc.py`, `vision_continua.py` y `main.py`: no lo "corrijas". `nexus_mcp_bridge.py` es independiente y lee su propio entorno. Las rutas salen de `settings.data_dir`. Escritura atómica (`.tmp` + `os.replace`) y modo 0o600 para lo privado.
7. Cablearlo en `main.py`:
   - `_construir_opcional(...)` en el paso que le toque;
   - `_NOMBRES_MODULO` y `_ORDEN_ARRANQUE`;
   - `_diagnosticos_arranque`;
   - `_lanzar_tareas` + `_CLAVE_TAREA` si tiene bucle;
   - `_cerrar_modulo` en su sitio de `apagar()`;
   - `_MODULOS_OPCIONALES` del selftest.
8. Tests:
   - fixtures de `conftest` (`settings_prueba`, `settings_con_claves`, `bus`, `correr`); no hay pytest-asyncio;
   - nunca `cargar_settings()` a secas, nunca la cámara ni la pantalla de verdad (sección 4.5, regla 14);
   - `httpx.MockTransport` y servidores en `127.0.0.1:0`;
   - `pytest.importorskip` para las dependencias opcionales.

---

## 7. Cómo arrancar todo y cómo probar

### 7.1 Arrancar (solo si el usuario **no** lo tiene ya abierto)

**¿Están corriendo?** Compruébalo sin conectarte a los puertos:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 3939,8765,8766 -ErrorAction SilentlyContinue
```

```bash
ss -ltn | grep -E ":(3939|8765|8766)"
```

- Si sale algo, o ante la duda, **pregunta** al usuario.
- Si ya hay algo vivo, no uses el preview `vtuber-studio` de `.claude/launch.json` (en los dos repos arranca `server.js` en :3939), `vtuber-ai/start.bat`, los lanzadores de `desktop/scripts/` ni `--amadeus=...` (como segunda instancia, reenvía la orden a la app abierta).
- Para probar el visor a mano, usa otro puerto (`$env:PORT="3940"; node server.js`) y ciérralo al terminar.

Orden recomendado: visor, después R.A.M.S. y por último Desktop. Desktop reintenta solo si los otros dos aún no están.

**Windows (PowerShell).** PowerShell bloquea `npm.ps1`: usa siempre `npm.cmd`.

```powershell
# 1) VTuber AI Studio (:3939)
cd C:\proyectos\vtuber-ai; node server.js
# 2) R.A.M.S. (HUD y /escritorio en :8765, control en :8766)
cd C:\proyectos\rams_biocore; .\.venv\Scripts\python.exe main.py        # --offline: sin red
# 3) Amadeus Desktop
cd C:\proyectos\rams_biocore\desktop; npm.cmd start                     # o: npm.cmd start -- --hidden
```

**Linux (Arch + Hyprland, objetivo).**

```bash
cd ~/ruta/vtuber-ai && node server.js
cd ~/ruta/rams_biocore && .venv/bin/python main.py
cd ~/ruta/rams_biocore/desktop && npm start      # o scripts/amadeus-desktop.sh
                                                 # Wayland sin Hyprland: npm run start:x11
```

- En Linux el token va en `$XDG_RUNTIME_DIR/rams/escritorio.json`. Sin `XDG_RUNTIME_DIR`, Desktop no conecta.
- Para Hyprland, ver `desktop/hyprland.ejemplo.lua` / `.conf`.

Flags de `main.py`: `--offline`, `--selftest`, `--sin-hud`, `--sin-camara`, `--sin-voz`, `--verbose` y `--env RUTA`.

### 7.2 Probar (seguro con los servicios en vivo)

| Qué | Windows (PowerShell) | Linux | Resultado esperado (medido el 19-10-2026, v0.2.2 sin commit) |
|---|---|---|---|
| Tests Python | `cd C:\proyectos\rams_biocore; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider` | `.venv/bin/python -m pytest -p no:cacheprovider` | 2101 pasan, 5 saltados (2106 en total; ~4 min; integración de la v0.2.2, 19-10-2026). Los saltos son normales: `test_connectome.py:364` necesita `RAMS_TESTS_LENTOS=1` y los 4 tests de `test_hud_server.py` en jsdom (L1343, L1434 ×2, L1463) necesitan node + jsdom (con `RAMS_TEST_NODE_PATH=<carpeta>/node_modules` quedan 2105 pasan, 1 saltado). Añade `-rs` para ver por qué se saltan. **Aviso:** la v0.2.2 la implementaron varios grupos en paralelo, así que estos totales pueden cambiar en cuanto otro grupo añada tests: si no coinciden, manda lo que diga pytest |
| Un archivo | `... -m pytest tests/test_permisos.py -p no:cacheprovider` | igual | |
| Selftest | `.\.venv\Scripts\python.exe main.py --selftest` | `.venv/bin/python main.py --selftest` | "Resultado: todo en orden." (12 OK). No usa red, cámara ni audio, y no toca `data/` |
| Compatibilidad 3.11 | `vermin -t=3.11- --no-tips --violations main.py core lobes interface` | igual | Mínimo 3.11, limpio |
| Tests del visor | `cd C:\proyectos\vtuber-ai; npm.cmd test` | `npm test` | 63/63. Usa un puerto libre, nunca el 3939 |
| Tests de Desktop | `cd C:\proyectos\rams_biocore\desktop; npm.cmd test` | `npm test` | 221/221 (19-10-2026, integración de la v0.2.2). Sin red ni cuenta de Puter |
| Smoke de Electron | `npm.cmd run smoke` (o `node scripts/verificar-smoke.js 3`) | `npm run smoke` | "smoke OK" en ≤ 5 s. Oculto, con perfil temporal: no toca la app abierta |

- No añadas `-q` a pytest: `pytest.ini` ya trae `addopts = -q` y `pythonpath = .` (no hace falta `PYTHONPATH`). Con `-q` extra queda `-qq` y no se imprime la línea final "N passed, M skipped".
- **El `.venv` del proyecto NO tiene pytest ni vermin.** Para instalarlos, **pide permiso antes**: `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt vermin`.
- El `.venv` es Python 3.14.6, pero el código debe seguir siendo compatible con 3.11.
- **No lances `main.py --offline` ni `main.py`** si ya hay un R.A.M.S. vivo: chocarían los puertos, el token de escritorio y la base SQLite.
- Los tests bloquean solos la red no local y los efectos sobre el PC (`sin_red`, `sin_efectos_pc` en `tests/conftest.py`). Usan claves FALSAS y `tmp_path` como `data/`.

### 7.3 Tests que pueden fallar por CPU (no son bugs)

Con ahorro de batería, con los tres servicios vivos o con suites en paralelo, estos pueden pasarse de tiempo:

| Archivo | Test o línea |
|---|---|
| `tests/test_connectome.py` | `test_reducido_rendimiento_y_rangos` (L264), `test_stub_iniciar_calibra_equilibrio` (L243), `test_iniciar_con_plazo_agotado_no_bloquea` (L519) |
| `tests/test_wsp_ingestor.py` | L373, L379 |
| `tests/test_presence.py` | L350 |
| `tests/test_vtube_bridge.py` | L813 |
| `tests/test_escritorio_link.py` | L1330, L1350, L1740 |
| `tests/test_main.py` | L89 |

**Qué hacer:** vuelve a correr ese test solo. Nunca aflojes umbrales ni cambies el conectoma para que pase.

### 7.4 Si algo "no funciona"

- **Un valor del `.env` no se aplica:** las variables del sistema operativo **ganan** al `.env`. Revisa el entorno listando solo **nombres** (PowerShell: `Get-ChildItem Env: | Select-Object Name`), nunca valores. No imprimas el `.env`, `os.environ` ni un `Settings` real: pueden contener claves.
- **Desktop no conecta:** R.A.M.S. no está corriendo, o el archivo de token es inválido o de un pid muerto.
- **El overlay no aparece:** `server.js` no está corriendo (Desktop reintenta cada 5 s).
- **El puente del visor está EN ESPERA:** `409 sin_visores`, no hay ningún visor abierto.
- **Un evento nunca llega:** el nombre no cumple la regex y se descartó (mira `log.error`).

---

## 8. Estado actual y trabajo pendiente

### 8.1 Versiones

| Versión | Estado | Commit |
|---|---|---|
| v0.1 (agente + conectoma) | Hecha | `1d35496` |
| Puente del avatar 3D | Hecho | `5833840` |
| v0.2 "Amadeus Companion" (escritorio, chat, voz, visión local, control del PC) | Hecha | `1c26e68` (último commit) |
| v0.2.1 + respaldo Puter | Hecha, **SIN COMMIT** | — |
| v0.2.2 "Correo, Drive y taller de scripts" (`docs/V022_CORREO_DRIVE_SCRIPTS.md`) | Implementada por grupos (base, google, taller, orquestacion, ui) el 2026-10-18/19, **SIN COMMIT** y sin probar todavía contra Google real (§20 de la spec) | — |
| v0.3 "Llamada en vivo" | **Solo diseñada, NO implementada** | — |

- Los dos repos están en la rama `main`, con git local y sin remoto. rams_biocore tiene 3 commits y vtuber-ai tiene 2 (`b0530af`, `b466e95`).
- `VERSION` sigue en `"0.1.0"` a propósito (sección 4.3).

### 8.2 Trabajo sin commit (no lo pierdas)

- **rams_biocore:**
  - Respaldo Puter: `desktop/lib/puter.js`, `puter-sitio.js`, `preload-puter.js`, `desktop/puter/`, sus tests e `interface/escritorio_link.py`.
  - Tanda v0.2.1: sin burbuja (`VTUBER_AI_BURBUJA=0` por defecto), apps del menú Inicio de Windows en `control_pc.py`, Telegram con fotos (`lobes/capturas.py`, nuevo) y notas de voz (transcripción con Broca).
  - Cambios en `core/config.py`, `intenciones.py`, `permisos.py`, `main.py`, `lobes/*`, `persona/*`, `docs/*`, `tests/*` y `desktop/*`, además de `.env.example`, `README.md` y `desktop/README.md` (modificados).
  - Nuevos sin seguimiento: `lobes/capturas.py`, `tests/test_capturas.py`, `desktop/lib/puter.js`, `desktop/lib/puter-sitio.js`, `desktop/preload-puter.js`, `desktop/puter/`, `desktop/test/puter.test.js`, `desktop/test/puter-paginas.test.js` y este `IMPORTANTEIA.md`.
  - **Tanda v0.2.2** (correo, Drive y taller de scripts; spec `docs/V022_CORREO_DRIVE_SCRIPTS.md`, también sin seguimiento):
    - nuevos: `lobes/google_cuenta.py`, `lobes/correo.py`, `lobes/drive.py`, `lobes/taller_scripts.py`, `lobes/taller_analizar.ps1` y sus tests (`tests/test_google_cuenta.py`, `test_correo.py`, `test_drive.py`, `test_taller_scripts.py`);
    - modificados otra vez: `core/config.py`, `core/contracts.py`, `core/intenciones.py`, `core/permisos.py`, `main.py`, `lobes/broca_llm.py`, `persona/frases.json` (3 categorías nuevas), `interface/*`, `desktop/*`, `.env.example`, `requirements.txt` (solo comentarios), `README.md` (§8.6), `docs/COMPANION.md` (remisiones), este archivo y los tests de todos ellos.
    - Activarlo es opt-in (`RAMS_GOOGLE=1`, `RAMS_TALLER=1`); la guía de Google Cloud para el usuario está en la spec §17.
  - **Trabajo ajeno que no se toca:** `movil/` (app Android hecha por otra IA del usuario) y `tests/test_movil.py`; la copia vieja `rams_biocore/vtuber-ai/`. Si un test suyo falla, se informa, no se "arregla".
- **vtuber-ai:**
  - Arreglo de VRM 0.x (`public/app.js`), subida de modelos `POST /api/models`, recarga por huella `build`, `speak.bubble` (`test/speak_bubble.test.js`, nuevo) y el tema solo para el modelo de ejemplo.
  - Cambios en `.gitignore` (excluye `models/*.vrm`), `README.md`, `lib/mcp.js`, `lib/protocol.js`, `public/amadeus_theme.js`, `server.js` y `test/server.test.js`.
- Antes de tocar nada, mira el estado real con `git status` (solo lectura).

### 8.3 v0.3 "Llamada en vivo" (solo diseño)

- **Dónde está el diseño:** `C:\Users\MSI\AppData\Local\Temp\claude\C--proyectos-alquile-com-co\3bec6b4e-dd29-4d71-a89d-0e3b8072e252\scratchpad\research3\DISENO_VOZ_EN_VIVO.md`.
  - **Está en una carpeta temporal y puede desaparecer.** Si vas a trabajar en la v0.3, pide al usuario copiarlo a `docs/` primero.
- **Qué propone:**
  - Voz en tiempo real con Gemini Live, con interrupciones en los dos sentidos (*barge-in*).
  - Llamada saliente y entrante, y videollamada opt-in.
  - Acciones por voz: nacen de la transcripción del usuario (`enrutar(canal="voz")`, misma compuerta que el chat), y las permitidas se ejecutan sin botón. Las propuestas del modelo (`origen="vivo"`) y lo que pida confirmación van con botón. Un «sí» hablado confirma solo con `RAMS_VIVO_SI_POR_VOZ=1` (0 por defecto) y solo acciones de bajo riesgo.
  - Módulos nuevos `lobes/voz_vivo.py`, `lobes/llamada_vivo.py`, `interface/voz_link.py` y `lobes/apps_locales.py`.
  - Un plano de audio `/escritorio/voz` y una ventana oculta "Amadeus Voz" en Desktop.
- **Ninguno de esos archivos existe todavía.**
- Parte de "apps locales en Windows" ya está hecha en v0.2.1, dentro de `control_pc.py`. Compara el diseño con el código actual antes de implementar.

### 8.4 Limitaciones conocidas

- **Windows hoy, Arch + Hyprland como objetivo:**
  - En Windows los alias de `RAMS_APPS` no lanzan apps: se usa el menú Inicio.
  - `cerrar_ventana` necesita Hyprland, así que solo funciona en Linux.
  - Hyprland y los binarios de Linux salen como OFF. Es lo esperado.
- **Wayland:**
  - Sin travesuras de cursor.
  - Sin atajos globales de Electron (se usa `hl.bind` hacia la CLI).
  - Otros compositores no tienen cursor global: usa `npm run start:x11`.
  - En X11 la visión de pantalla queda OFF.
- `lobes/capturas.py` no está en `_MODULOS_OPCIONALES`: `--selftest` no detecta si se rompe, solo `tests/test_capturas.py`.
- **Documentación desactualizada (se puede corregir):**
  1. `vtuber-ai/models/README.md` L16–17 dice que `.gitignore` no excluye los `.vrm`; ahora sí los excluye.
  2. `vtuber-ai/models/README.md` L55–59: la paleta, las medias y la corbata ya solo se aplican al modelo de ejemplo.
  3. `vtuber-ai/README.md` §6: `POST /api/models` acepta binario. El README tampoco documenta la subida, la huella `build` ni el arreglo de VRM 0.x.
  4. Los comentarios de `desktop/scripts/amadeus-desktop.cmd` y `.sh` no incluyen `--amadeus=puter`.
  5. `desktop/README.md` usa `npm start`/`npm test`; en PowerShell es `npm.cmd`.
- **Quitar el tema Amadeus requiere 3 sitios:**
  1. `VTUBER_AI_TEMA=none` en el `.env` de R.A.M.S.;
  2. la misma variable en el entorno de Electron (Electron no lee el `.env`);
  3. el chip "Ninguno" en el panel.

---

## 9. Convenciones de código y de git

### Código

- **Idioma: español en todo** (UI, logs, comentarios, docstrings e identificadores). Excepciones aceptadas: `Settings`, `HomeostaticState`, `BrocaReply`, `Utterance`, `bus`, `token`, `async`.
- **Archivos:** UTF-8 con **LF**. Hay `core.autocrlf=false` y no existe `.gitattributes`, así que configura tu editor para no meter CRLF.
  - En Windows no escribas archivos con `Set-Content`, `Out-File` ni `>` de PowerShell 5.1: meten ANSI, BOM o CRLF y corrompen las tildes. Usa la herramienta de edición de tu IDE o agente y conserva UTF-8 sin BOM con LF.
- **Python ≥ 3.11:** cada `.py` lleva `from __future__ import annotations`, no usa sintaxis exclusiva de 3.12+ (compruébalo con vermin) ni `audioop` (no existe en 3.13+). Nunca dos variantes de OpenCV instaladas a la vez.
- **Prohibido:** `shell=True`, `os.system`, `eval`/`exec` sobre datos, leer la configuración del proyecto (`RAMS_*`, claves, puertos, rutas) con `os.environ` fuera de `config`, `logging.basicConfig` y `except: pass` mudo. Las variables del sistema para detectar la plataforma sí se leen con `os.environ.get` (receta de la sección 6, paso 6).
- **Números para el usuario:** coma decimal (`formatear_decimal`). La divergencia va con punto y 6 decimales ("1.048596").
- **Tiempo:**
  - en disco, UTC ISO;
  - en el bus, `time.time()`;
  - duraciones, `time.monotonic()`;
  - hora del usuario, `core.config.ahora_local(settings)` (America/Bogota).
- **JavaScript:** Node ≥ 22 para los tests (Desktop exige ≥ 22.12). vtuber-ai no tiene dependencias npm, ni las añadas. Desktop solo depende de `electron` 44.8.0.

### Git

- Rama `main`, sin remoto. **No hagas commits si el usuario no lo pide.** Cuando lo pida, escribe el mensaje en español, al estilo de los existentes ("v0.2 Amadeus Companion: escritorio, chat, voz…").
- **NUNCA:** `git checkout`, `restore`, `reset`, `stash`, `clean`, `rm`, `rebase`, `push --force`, `add -f` ni `commit -a`. Para ver versiones anteriores usa solo `git status`, `git diff`, `git log -p` o `git show HEAD:<ruta>`.
- **Nunca subas:**
  - `.env` (solo `.env.example`), `data/`, `chats/`, `ctl_token`, `*.key`, `*.pem`;
  - `*.db`, `*.npz`, `perfil_conductual.json`, `vts_token.txt`;
  - `desktop/node_modules/`, `.venv/`, `.claude/`;
  - `vtuber-ai/models/*.vrm`.
  - Todo eso ya está en `.gitignore`. No lo saques de ahí.
- **Antes de terminar tu tarea:**
  - corre los tests de lo que tocaste;
  - di al usuario qué cambiaste, qué no pudiste verificar y qué queda sin commit.
