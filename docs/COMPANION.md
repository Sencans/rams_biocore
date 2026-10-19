# R.A.M.S. BIO-CORE v0.2 "Amadeus Companion": especificación de arquitectura

> **Estado:** especificación normativa para la implementación en paralelo (tanda v0.2), **revisión 2** (incorpora la revisión crítica; lo aceptado, lo modificado y lo rechazado está en §C6). Complementa a `docs/ARQUITECTURA.md` (v0.1), que sigue siendo la base. Si algo de aquí contradice a la v0.1, **manda este documento** para los archivos que toca la v0.2; todo lo demás de la v0.1 sigue vigente. **Ampliaciones posteriores:** §C2.15 (respaldo Puter del Área de Broca dentro de Amadeus Desktop) y §C2.16 (v0.2.1: Telegram multimedia, apps del menú Inicio y capacidades reales).
> **Fecha:** 2026-10-17. **Idioma:** español (comentarios, docstrings, UI, identificadores). **Reglas de código:** UTF-8 + LF; Python ≥ 3.11 (vermin `-t=3.11-`); nada bloqueante en el event loop; sin `shell=True`; dependencias opcionales en `try/except`; Electron con `contextIsolation`, `sandbox` y preload mínimo; sin secretos en el código.
> **Investigación de apoyo** (solo lectura): `scratchpad/research2/` → `amadeus_desktop_overlay.md`, `control_pc_apps_raton.md`, `vision_continua_local.md`, `kurisu_look_vrm.md`. Este documento manda sobre los informes.

---

## Índice

- §C0. Cómo usar este documento y el principio No-LLM
- §C1. Arquitectura y flujos de cada pedido del usuario
- §C2. Contratos exactos entre grupos
- §C3. Grupos de implementación (archivos, brief y tests) + matrices de verificación
- §C4. Pruebas y smoke (sin hardware, sin tocar las instancias en vivo)
- §C5. Qué tiene que hacer el usuario y qué recursos hacen falta
- §C6. Decisiones de revisión

---

## §C0. Cómo usar este documento y el principio No-LLM

1. Un archivo pertenece a **un solo grupo** (§C3, matriz §C3.1). Un grupo puede **añadir** archivos nuevos dentro de su ámbito, pero no tocar los de otro grupo. Las APIs entre grupos son **exactas**: firmas, nombres de eventos, claves de diccionario, valores por defecto y rutas.
2. **El grupo `base` va primero.** Publica `core/config.py`, `core/contracts.py`, `.env.example`, `tests/conftest.py`, `requirements.txt` y `.gitignore`. Los demás programan contra esos contratos (§C2.1, §C2.2) desde el primer minuto, de forma defensiva (`getattr(settings, "campo", defecto)`, constantes importadas con *fallback*).
3. **No-LLM centric (sigue vigente, §8 de v0.1).** El conectoma LIF, la corteza motora y reglas deterministas de main deciden **cuándo** aparecer, pasear, perseguir el cursor, buscar al usuario o compartir algo curioso. Gemini (hoy sin clave) **solo verbaliza**: sin clave, banco local de frases (`persona/frases.json`). Ninguna pieza nueva introduce decisiones en el LLM. El overlay, ControlPC y VisionContinua son **efectores y sensores**: sus reflejos locales (seguir el cursor con la mirada, hit-test, física al soltar) no deciden nada.
4. **Seguridad por diseño (sigue vigente, §9).** Las acciones sobre el PC nacen del texto del usuario o del código de iniciativas, pasan **una sola vez** por `CompuertaPermisos.autorizar` y nunca usan `shell=True`. Nada "envía" (Enter). La cámara y la pantalla tienen interruptor, indicador visible y nunca guardan fotogramas.
5. Palabras normativas: **DEBE**, **NO DEBE**, **PUEDE**.

**EN VIVO (no tocar).** Durante el desarrollo hay instancias reales del usuario: R.A.M.S. (HUD `:8765`, control `:8766`) y VTuber AI Studio (`:3939`, panel conectado). Los tests y el smoke **no** les envían comandos, no las detienen, no abren ventanas visibles, no reproducen audio, no abren la webcam real, no teclean ni mueven el mouse real, no abren apps ni URLs reales: **todo con mocks**. Se pueden descargar dependencias de npm/pip de los registros oficiales. (Editar los archivos de `vtuber-ai/public/` afecta al panel en vivo solo cuando el usuario recargue la página; está permitido.)

---

## §C1. Arquitectura y flujos de cada pedido del usuario

### C1.0 Mapa de procesos

```
┌──────────────────────────── R.A.M.S. (Python, asyncio, un loop) ────────────────────────────┐
│ conectoma LIF ─► homeostasis/iniciativa ─► main (reglas deterministas de iniciativa)          │
│                         │                     │ motor.solicitud / escritorio.orden             │
│                         ▼                     ▼                                                │
│                 corteza_motora (decide CÓMO: pasea/asoma/persigue/busca/se esconde/travesea)   │
│                         │ comandos de cuerpo {type:"desktop"} ─► vtuber_ai_bridge ─► :3939     │
│  VisionContinua (cámara+pantalla) ─ vision.* / pantalla.* ─► main (conectoma, presencia,      │
│                                                              percepción) y corteza (mirada)    │
│  ControlPC ◄── AccionPC (abrir/buscar/prerrellenar/escribir/travesura) ◄── permisos ◄── main   │
│  main ◄── escritorio.chat / .confirmacion / .control / .evento / .app / .caja ──┐             │
│  main ──► escritorio.orden / escritorio.sistema / salida.subtitulo / permiso.* ──┤             │
│          ServidorHUD (interface/hud_server.py): /ws (HUD) + /escritorio (token propio)        │
└──────────────────┬──────────────────────────────────────────────────────▲───────────────────┘
       WS /escritorio (sin Origin)                                          │ subtítulos, órdenes,
                   ▼                                                        │ permisos, sistema
   ┌────────────────────────────── Amadeus Desktop (Electron) ─────────────────────────────────┐
   │ main.js: ÚNICO dueño de la visibilidad · overlay transparente · bandeja · atajos ·        │
   │          2.ª instancia · cursor global · autoridad del click-through · caja → R.A.M.S.     │
   │   ├─ preload-overlay ─► visor :3939 /?obs=1&desktop=1&camera=full&theme=amadeus            │
   │   └─ preload-chat    ─► chat.html (file://)                                                │
   └───────────────────────────────────────────────────────────────────────────────────────────┘
          HUD (:8765/?t=…) ── panel de chat RINE: abre /escritorio con Origin local
```

Dos caminos hacia el avatar, que **no se cruzan**:
- **Cuerpo y locomoción mientras la ventana es visible** (caminar, asomarse, perseguir el cursor, buscar al usuario, mirar): corteza_motora → `vtuber_ai_bridge.desktop(...)` → `POST :3939/api/command` → solo visores `?desktop=1`. Una única cola y los acks.
- **Visibilidad, entrada/salida por el borde y chat** (aparecer, esconderse, abrir chat, notificar, texto del chat, confirmaciones): main (o la corteza, solo para `esconderse`) → `escritorio.orden` → `/escritorio` → **main de Electron**, que es el único que muestra u oculta la ventana y le pide al renderer la animación de entrada/salida.

### C1.1 App en segundo plano / bandeja

Amadeus Desktop arranca **oculto** (`--hidden`), se queda en la **bandeja** y no roba el foco. El overlay transparente cubre el `workArea` del monitor; dentro, una **caja del avatar** (~340×600 px CSS) se mueve con `transform: translate3d()`. Por defecto `setIgnoreMouseEvents(true, {forward:true})`: los clics atraviesan la ventana salvo sobre la caja, y **el main de Electron es la autoridad** del modo interactivo (§C2.7.3). Nada visible en el escritorio hasta que la llamen o main decida que salga.

### C1.2 Llamarla

Entradas que la hacen **aparecer caminando** desde un borde:
- **Bandeja:** clic / "Llamar a Amadeus".
- **Atajo global:** `Ctrl+Alt+A` (Win/X11); en Hyprland, `hl.bind SUPER+A` → `amadeus-desktop --amadeus=toggle`.
- **CLI 2.ª instancia:** `amadeus-desktop --amadeus=show|toggle` (single-instance: `additionalData` a la 1.ª instancia y sale).
- **Chat** (no hay entrada de voz en R.A.M.S.): "ven", "aparece", "sal" → intención `escritorio_venir` → main publica `escritorio.orden{accion:"aparecer", borde, modo:"entrar"}`.

Flujo: el main de Electron hace `overlay.showInactive()` → `overlay:visibility(true)` y `overlay:order{accion:"aparecer", borde, modo}` al renderer → el renderer camina desde el borde → Electron envía `{tipo:"evento", nombre:"mostrada"}` → R.A.M.S. publica `escritorio.evento{mostrada}`. Si la orden llega de una iniciativa, la ventana **nunca** toma el foco.

### C1.3 Ella decide salir (iniciativa)

El conectoma publica `homeostasis.iniciativa`. `main._al_iniciativa` aplica, **antes de gastar Gemini**, esta regla única (§C2.14.6):
- **Presente y overlay disponible** (`escritorio.conectado.overlay` **y** `avatar.visores_escritorio_total > 0`) **y** sin "no molestar" (`pc.no_molestar.activo == False`): si el overlay está oculto, main publica antes `escritorio.orden{aparecer, modo:"asomarse"}`; después `motor.solicitud{conducta:"buscar_usuario", ttl_s:20}`; la frase sale por voz/terminal como en la v0.1 (y llega al chat como subtítulo).
- **Presente sin overlay:** comportamiento de la v0.1 (voz/terminal).
- **Ausente con confianza ≥ 0,8:** Telegram por la ruta proactiva de la v0.1 (§10.19).
- **Nunca** se usa `escritorio.conectado.clientes` para decidir.

### C1.4 Pasear, asomarse, mirar y perseguir el cursor

Reflejos de la corteza (sin LLM), modulados por `homeostasis.estado`, y **solo mientras la ventana es visible** (la corteza sigue `escritorio.evento mostrada/ocultada`):
- **pasear:** `desktop wander` / `walk_to`.
- **asomarse:** `desktop peek` por un borde.
- **mirar/perseguir el cursor:** Electron lee el cursor global (Windows 30 Hz; Hyprland 15/4 Hz, §C2.7.3) y lo manda al renderer (`overlay:cursor`), que lo "mira"; `desktop chase_cursor` lo persigue un rato si está aburrida.
- **travesuras de mouse (opt-in, OFF por defecto):** la corteza publica `motor.travesura` → main → compuerta → `ControlPC.empujon_cursor`. Guardas de §C2.12. En Hyprland no hay travesura con el cursor real (no se puede leer el estado de los botones): solo queda la persecución visual.

### C1.5 Chat (app de escritorio y HUD)

Dos clientes del mismo endpoint `/escritorio`:
- **Ventana de chat de Electron** (`preload-chat`): escribes → `{tipo:"chat", texto}` → `escritorio.chat` → main → `_turno_serializado(texto, "escritorio")`, que publica **una** `usuario.entrada{canal:"escritorio"}` ya redactada.
- **Panel de chat estilo RINE en el HUD** (`amadeus_hud.html`): igual, con el token de escritorio que el HUD recibe en un mensaje aparte de `/ws` (§C2.4.6), si `RAMS_HUD_CHAT=1`.

Las respuestas salen en **modo terminal** (texto en el chat, labios mudos; globo sobre el avatar solo con `VTUBER_AI_BURBUJA=1`; con `RAMS_ESCRITORIO_VOZ=1`, regla de voz de la v0.1). La salida de los comandos (`/estado`, `/ayuda`…) vuelve al chat como mensaje de sistema. El canal `escritorio` es **local** (presencia y capacidades de la terminal), pero **no** resuelve confirmaciones con texto: se confirman con **botones** ligados al `id` de la solicitud; `cerrar_ventana` solo por terminal.

### C1.6 "Búscame en 10 minutos"

Intención `recordatorio_personal` (agenda de la v0.1): "búscame / llámame en N minutos / a las HH:MM" → `ItemAgenda` tipo `recordatorio` marcado `buscar=True`. Al vencer, `_bucle_agenda` aplica la regla de C1.3 (aparece y te busca si estás; Telegram si estás ausente). Solo desde los canales locales: por Telegram es `bloqueada_canal` (§C2.12, "Telegram no gana capacidades"); allí sigue valiendo el `programar` de la v0.1 ("recuérdame en …").

### C1.7 Compartir algo curioso

Iniciativa **`curiosidad_web`** con regla determinista (§C2.14.7): una `curiosidad_visual` sin cámara utilizable, con el usuario presente y `tiene_web`, se convierte en `curiosidad_web` (≤ 3/día, ≥ 2 h entre una y otra). El tema sale de `persona/temas_curiosidad.json` ∪ recuerdos de categoría `preferencia` (**nunca** de `perfil.temas`); consulta ≤ 60 caracteres redactada; búsqueda con `web_researcher` (DuckDuckGo sin clave); frase con plantilla local `iniciativa_curiosidad_web`. Con overlay: `escritorio.orden{notificar, texto, url}` + `motor.solicitud{asomarse}`. El enlace se abre **solo con un clic** del usuario en el chat o la notificación.

### C1.8 Control de apps y redes

El usuario pide "abre WhatsApp y escríbele a mamá…". Reglas, en orden de seguridad:
1. **Abrir** la app o la web (`abrir_destino`) — permitida.
2. **Prerrellenar** con un enlace profundo (`prerrellenar`, p. ej. `https://wa.me/?text=…`) — **confirmar**; deja el borrador y **el usuario pulsa Enviar**.
3. **Teclear** (`escribir_texto`) donde no hay prerrelleno (Instagram, TikTok, Discord…) — **confirmar**. El objetivo es la **ventana previa** (la última en primer plano que no es de Amadeus: ni la terminal, ni el chat, ni el overlay, ni el HUD). Tras confirmar **no se fuerza el foco**: Amadeus dice "haz clic en la caja de texto de <app>" y espera ≤ 10 s a que esa ventana tenga el foco; si no, aborta. Nunca pulsa Enter.
4. **Borradores:** "escríbeme un tweet sobre…" produce un texto de Amadeus (dato, no acción). "Ponlo en X" / "pásalo a WhatsApp" (intención `publicar_borrador`) crea la acción con **el último borrador** de una respuesta a un turno local del usuario (≤ 10 min, ≤ 500 caracteres).
5. **Enviar** (Enter): **no existe** en la v0.2.

### C1.9 Visión en tiempo real local (sin tokens)

`VisionContinua` (opt-in, OFF por defecto) abre la cámara en un hilo propio (2–5 fps, YuNet/Haar) y, opcionalmente, mira la pantalla (0,2–2 fps, métricas escalares, excluyendo las ventanas propias). Produce flancos con histéresis (`vision.estimulo`, `pantalla.estimulo`), un tónico de atención (`vision.atencion`), el rostro (`vision.rostro`) y la mirada del avatar (`vision.mirada`). **Nunca guarda ni envía cuadros.** Sin tokens, Amadeus **ve la app, el título (si no es privado), la actividad (vídeo, pantalla completa) y tu presencia/atención**, pero **no el contenido** de la pantalla (OCR local bajo pedido: v0.3). Eso llega a Broca como `ContextoBroca.percepcion` ("¿Otra vez YouTube?").

### C1.10 Look de Kurisu / Amadeus

El tema "Amadeus" recolorea el VRM a la paleta de Kurisu por nombre de material, añade corbata roja y medias oscuras procedurales y, **solo fuera del modo escritorio**, un marco CSS de videollamada holograma (en escritorio: rim y scanlines dentro del canvas). Se aplica **dentro de `loadModel`** (`?theme=amadeus` o comando `theme`). Homenaje de fan, **sin assets oficiales**.

### C1.11 No molestar y segundo plano

- **"No molestar" del usuario** (bandeja/chat → `escritorio.control{no_molestar_on|off}`): `riko.pausar_proactividad(1440)` hasta recibir `off` (`pausar_proactividad(0)`), travesuras vetadas y `escritorio.orden{esconderse}`.
- **No molestar del sistema** (sin LLM): main sondea `ControlPC.no_molestar()` cada 5 s (Windows: QUNS ∈ {2,3,4}; Hyprland: ventana activa a pantalla completa / `contentType == "game"` / `inhibitingIdle`) y combina `pantalla.estado.no_molestar` si la visión está activa → publica `pc.no_molestar{activo, motivo}` al cambiar.
- **La corteza se esconde sola** con `pc.no_molestar.activo` o con fatiga > 0,8 sostenida 10 min, publicando `escritorio.orden{esconderse}`. Solo reaparece si la llaman o por una iniciativa de main.

### C1.12 Interruptores de emergencia (sin voz: R.A.M.S. no tiene STT)

- `Ctrl+Alt+Shift+Q` (o `--amadeus=basta`, bandeja "Quieta"): `basta_travesuras` + esconderse. Lleva Shift porque en Windows `Ctrl+Alt` es AltGr y en el teclado latinoamericano AltGr+Q escribe «@» (registrar `Ctrl+Alt+Q` le robaría la arroba al usuario); los atajos se cambian con `AMADEUS_ATAJO_*`.
- `Ctrl+Alt+V` (o `--amadeus=vision_pausa`, bandeja "Pausar visión"): `escritorio.control{pausar_vision}`.
- `Ctrl+Alt+Shift+A` (o bandeja "Dejar pasar clics"): oculta el overlay y fuerza el click-through (local en Electron, no necesita a R.A.M.S.).
- Chat/terminal: "basta", "quieta", "deja el mouse", "para ya", `/travesuras off`; "deja de mirarme", "no me mires", "cierra los ojos", `/vision pausa [min]`.

---

## §C2. Contratos exactos entre grupos

### C2.1 Settings nuevos (grupo `base`, `core/config.py`)

Campos nuevos de la dataclass `Settings` (frozen, hashable). Mismo patrón de parseo que v0.1 (`_Lector`, regla única de ausencia, `invalido` → defecto con `log.warning`). **Todos** se documentan en `.env.example`.

| Campo | Variable | Tipo | Defecto | Notas |
|---|---|---|---|---|
| `escritorio_habilitado` | `RAMS_ESCRITORIO` | `bool` | `True` | endpoint `/escritorio` y efectores de escritorio |
| `escritorio_token_file` | `RAMS_ESCRITORIO_TOKEN_FILE` | `Path` | Windows: `%LOCALAPPDATA%\rams\escritorio.json`; Linux: `$XDG_RUNTIME_DIR/rams/escritorio.json`; sin `XDG_RUNTIME_DIR`: `data_dir/escritorio.json` (Electron entonces solo conecta si la variable está definida, §C2.7.6) | archivo privado (0o600) con `{url, token, pid}` |
| `escritorio_voz` | `RAMS_ESCRITORIO_VOZ` | `bool` | `False` | `True` = las respuestas al chat siguen la regla de voz de la v0.1 |
| `hud_chat` | `RAMS_HUD_CHAT` | `bool` | `True` | `False` = el HUD no recibe el token de `/escritorio` |
| `travesuras_habilitadas` | `RAMS_TRAVESURAS` | `bool` | **`False`** | interruptor maestro del **cursor real**; la mirada y la persecución visual siempre están |
| `travesura_amplitud_max_px` | `RAMS_TRAVESURA_AMPLITUD` | `int` | `40` | rango 5..80 |
| `destinos_items` | `RAMS_DESTINOS` (`sinonimo=clave,…`) | `tuple[tuple[str,str], ...]` | `()` | sinónimos que se **suman** a `DESTINOS_BASE`; una `clave` que no esté en `DESTINOS_BASE` o un sinónimo que no cumpla `^[a-z0-9_]{1,24}$` se descarta con `log.warning` |
| `vtuber_ai_tema` | `VTUBER_AI_TEMA` | `str` | `"amadeus"` | `amadeus`/`none`; con `none` el puente nunca envía `theme` |
| `vision_continua` | `RAMS_VISION_CONTINUA` | `bool` | `False` | **opt-in**; además exige `camara_habilitada` |
| `vision_fps` | `RAMS_VISION_FPS` | `float` | `3.0` | rango 1..5 |
| `vision_detector` | `RAMS_VISION_DETECTOR` | `str` | `"auto"` | `auto`/`yunet`/`haar` |
| `vision_modelo` | `RAMS_VISION_MODELO` | `Path \| None` | `None` | ruta a YuNet; vacío = `data_dir/models/face_detection_yunet*.onnx` |
| `vision_hfov` | `RAMS_VISION_HFOV` | `float` | `70.0` | grados, rango 30..120 |
| `vision_espejo` | `RAMS_VISION_ESPEJO` | `bool` | `False` | la webcam entrega la imagen en espejo |
| `vision_apps_pausa` | `RAMS_VISION_APPS_PAUSA` (CSV) | `tuple[str, ...]` | `("zoom","teams","ms-teams","discord","obs64","obs")` | procesos que autopausan la cámara y vetan travesuras |
| `pantalla_modo` | `RAMS_PANTALLA_MODO` | `str` | `"off"` | `off`/`metadatos`/`pixeles` (`pixeles` es un permiso aparte) |
| `pantalla_fps` | `RAMS_PANTALLA_FPS` | `float` | `1.0` | rango **0,2..2** |
| `pantalla_excluir` | `RAMS_PANTALLA_EXCLUIR` (CSV) | `tuple[str, ...]` | `()` | patrones **extra**; `PATRON_TITULO_SENSIBLE` (§C2.2) se aplica **siempre** |
| `pantalla_excluir_avatar` | `RAMS_PANTALLA_EXCLUIR_AVATAR` | `bool` | `True` | enmascara la **caja del avatar** (`escritorio.caja`), no la ventana entera |

Propiedades nuevas de `Settings`:
```python
@property
def tiene_escritorio(self) -> bool: ...        # escritorio_habilitado  (NO depende de offline)
@property
def tiene_vision_continua(self) -> bool: ...   # vision_continua and camara_habilitada  (NO depende de offline)
@property
def destinos_permitidos(self) -> Mapping[str, str]: ...
    # MappingProxyType: {clave: clave for clave in DESTINOS_BASE} ∪ {sinonimo: clave de destinos_items}
@property
def pantalla_excluir_re(self) -> re.Pattern[str]: ...
    # PATRON_TITULO_SENSIBLE | extras escapados de pantalla_excluir; se aplica al título en minúsculas y sin tildes
```
`config.py` PUEDE importar `DESTINOS_BASE` y `PATRON_TITULO_SENSIBLE` de `core.contracts` (no hay ciclo: `contracts` no importa `config`).

Funciones puras nuevas en `core/config.py` (junto a `redactar`; las usan `core/permisos.py` y `lobes/control_pc.py`, así la heurística no se duplica):
```python
def parece_secreto(texto: str, secretos: Iterable[str] = ()) -> bool:
    """True si el texto parece un secreto (reglas "Texto" de §C2.12): redactar(texto, secretos,
    alta_entropia=True) != texto; ^\s*\d{4,8}\s*$ (OTP/PIN); 13–19 dígitos que pasan Luhn;
    (contraseña|clave|password|pin|código)\s*[:=]\s*\S+; palabra ≥ 12 caracteres con ≥ 3 clases."""
def titulo_sensible(titulo: str) -> bool:
    """True si el título (en minúsculas y sin tildes) casa con PATRON_TITULO_SENSIBLE."""
```

`asegurar_directorios()` DEBE crear también el directorio padre de `escritorio_token_file` con 0o700 (en `try/except OSError`).

### C2.2 `core/contracts.py` (grupo `base`): constantes y tipos nuevos

```python
# Canales: "escritorio" es local (presencia + todas las capacidades), como "terminal".
CANALES_ENTRADA: Final = ("terminal", "telegram", "nexus", "escritorio")
CANALES_LOCALES: Final = frozenset({"terminal", "escritorio"})
# CANALES_SALIDA NO cambia: el chat de escritorio recibe la salida en modo "terminal".

INICIATIVAS: Final = ("conversar", "curiosidad_visual", "descanso", "cambio_linea", "curiosidad_web")

# Tipos de acción del PC (13; cada uno con productor real en intenciones/main).
TIPOS_ACCION: Final = ("lanzar_app", "enfocar_nexus", "ir_workspace", "cerrar_ventana",
                       "volumen", "media", "captura_pantalla", "vistazo",
                       "abrir_destino", "buscar_en", "prerrellenar", "escribir_texto", "travesura_cursor")
TIPOS_ACCION_V02: Final = frozenset({"abrir_destino", "buscar_en", "prerrellenar", "escribir_texto",
                                     "travesura_cursor"})   # van a ControlPC, nunca a aura.ejecutar

# Catálogos cerrados de la v0.2.
PATRONES_TRAVESURA: Final = ("empujon", "circulo", "temblor", "hacia_avatar")
MODOS_PANTALLA: Final = ("off", "metadatos", "pixeles")
ESTADOS_VISION: Final = ("off", "continua", "pausada", "ocupada", "error", "degradada")
ACCIONES_ORDEN_ESCRITORIO: Final = ("aparecer", "esconderse", "abrir_chat", "notificar")
EVENTOS_ESCRITORIO: Final = ("toque", "soltada", "cursor_encima", "mostrada", "ocultada", "chat_abierto")
CONTROLES_ESCRITORIO: Final = ("no_molestar_on", "no_molestar_off", "basta_travesuras", "pausar_vision")
CONDUCTAS_SOLICITUD: Final = ("buscar_usuario", "asomarse", "esconderse", "pasear")
SAFE_ALIAS_RE: Final = re.compile(r"^[a-z0-9_]{1,24}$")

# Metadatos de destinos (cómo se ABRE cada uno en cada SO vive en lobes/control_pc.py).
DESTINOS_BASE: Final[Mapping[str, Mapping[str, bool]]] = MappingProxyType({
    "whatsapp":  {"prerrelleno": True,  "busqueda": False},
    "telegram":  {"prerrelleno": True,  "busqueda": False},
    "x":         {"prerrelleno": True,  "busqueda": True},
    "gmail":     {"prerrelleno": True,  "busqueda": False},
    "correo":    {"prerrelleno": True,  "busqueda": False},   # mailto:
    "instagram": {"prerrelleno": False, "busqueda": False},
    "tiktok":    {"prerrelleno": False, "busqueda": False},
    "facebook":  {"prerrelleno": False, "busqueda": False},
    "discord":   {"prerrelleno": False, "busqueda": False},
    "linkedin":  {"prerrelleno": False, "busqueda": False},
    "youtube":   {"prerrelleno": False, "busqueda": True},
    "spotify":   {"prerrelleno": False, "busqueda": True},
    "navegador": {"prerrelleno": False, "busqueda": True},    # búsqueda web en DuckDuckGo
    "archivos":  {"prerrelleno": False, "busqueda": False},
})   # (cada valor también es MappingProxyType)

# Títulos sensibles (se aplica a títulos en minúsculas y sin tildes; base de anti-contraseñas y de privacidad).
PATRON_TITULO_SENSIBLE: Final = (
    r"contrasena|password|iniciar sesion|sign ?in|log ?in|verificacion|codigo|otp|2fa|token|\bpin\b|"
    r"banco|bancolombia|nequi|daviplata|\bpse\b|pago|checkout|tarjeta|wallet|binance|"
    r"incognito|inprivate|private browsing|navegacion privada|keepass|bitwarden|1password")
```

`ContextoBroca` gana un campo (último, con defecto):
```python
percepcion: Mapping[str, Any] | None = field(default=None, hash=False)
# Claves permitidas (las demás se descartan en __post_init__; se guarda como MappingProxyType):
#   app: str ("" si no se sabe) · titulo_corto: str (≤ 60; "" si la ventana es privada)
#   video: bool | None · pantalla_completa: bool | None · rostro: bool | None
#   atencion: float | None (0..1) · dist_m: float | None
```

`ORIGENES_ACCION` NO cambia (acciones del canal `escritorio` con `origen="usuario"`; travesuras con `origen="iniciativa"`). `AccionPC`, `DecisionPermiso` y `ResultadoAccion` NO cambian de forma.

**Compatibilidad:** `len(TIPOS_ACCION) == 13`; `"escritorio" in CANALES_LOCALES`. El grupo base **actualiza** `tests/test_config_bus.py:917` (`CANALES_ENTRADA == ("terminal","telegram","nexus","escritorio")`) y `:918` (`len(TIPOS_ACCION) == 13`) y añade las comprobaciones nuevas.

### C2.3 Eventos del bus nuevos (ampliación de la tabla de §5)

v0.2.2: eventos y mensajes nuevos en `docs/V022_CORREO_DRIVE_SCRIPTS.md` §11–§12.

Payloads JSON-serializables; claves exactas; sin secretos, imágenes, rutas completas ni comandos completos. **Reglas del bus v0.2:** (a) ningún módulo se suscribe a `"*"` ni a `"escritorio.*"`; (b) `escritorio.chat` es el **único** evento con texto del usuario sin redactar: solo lo consume main, nadie lo registra (ni en INFO ni en DEBUG) y ni el HUD ni `/escritorio` lo reenvían; (c) `usuario.entrada` sigue siendo **exclusivo de main** (§5 de la v0.1).

| Evento | Emisor | Payload (`datos`) | Frecuencia | Consumidores |
|---|---|---|---|---|
| `escritorio.conectado` | escritorio-ws | `{"clientes": int, "overlay": bool, "hud": int}` (`overlay` = cliente Electron conectado; `hud` = nº de clientes con Origin del HUD) | al conectar/desconectar | main; motor |
| `escritorio.app` | escritorio-ws | `{"pid": int}` (del `hola_app` de Electron) | al conectar Electron | main (→ `ControlPC.excluir_pids`, `pids_extra` de la compuerta) |
| `escritorio.chat` | escritorio-ws | `{"texto": str}` (1..2000) | por mensaje | **solo** main |
| `escritorio.evento` | escritorio-ws | `{"nombre": str ∈ EVENTOS_ESCRITORIO, "datos": dict}` (`soltada`: `{"altura": float}` 0..1) | por evento; `cursor_encima` ≤ 1/10 s | main (visibilidad; conectoma); motor (visibilidad; reacciones) |
| `escritorio.control` | escritorio-ws | `{"accion": str ∈ CONTROLES_ESCRITORIO}` | por clic/atajo | main |
| `escritorio.confirmacion` | escritorio-ws | `{"id": str (^[0-9a-f]{8}$), "aprobado": bool}` | por botón | main (→ `permisos.resolver_por_id`) |
| `escritorio.caja` | escritorio-ws | `{"x": int, "y": int, "w": int, "h": int}` en **píxeles físicos globales** | ≤ 2 Hz y solo si cambia | main (→ `ControlPC.set_caja_avatar`, `VisionContinua.set_caja_avatar`) |
| `escritorio.orden` | main; motor (solo `esconderse`) | `{"accion": str ∈ ACCIONES_ORDEN_ESCRITORIO, ...}`: `aparecer{borde?: "left"\|"right", modo?: "entrar"\|"asomarse"}`, `esconderse{}`, `abrir_chat{}`, `notificar{texto ≤ 300, url?}` (`url` cumple `^https://`, ≤ 300) | por decisión | escritorio-ws (→ WS `orden`) |
| `escritorio.sistema` | main | `{"texto": str}` (≤ 4000) | salida de comandos y avisos del canal escritorio | escritorio-ws (→ WS `sistema`; **nunca** `/ws`) |
| `motor.travesura` | motor | `{"patron": str ∈ PATRONES_TRAVESURA, "amplitud_px": int, "segundos": float}` (≤ 0,6) | ≤ 1 cada 15 min | main (→ compuerta → ControlPC) |
| `motor.travesuras` | main | `{"activas": bool, "hasta_ts": float \| None}` (estado efectivo, §C2.14.4) | al cambiar y al arrancar | motor; escritorio-ws (→ WS `travesuras`) |
| `motor.solicitud` | main | `{"conducta": str ∈ CONDUCTAS_SOLICITUD, "motivo": str, "borde": "left"\|"right"\|None, "ttl_s": float}` (`ttl_s` ≤ 30) | por iniciativa | motor |
| `pc.no_molestar` | main | `{"activo": bool, "motivo": str}` (`motivo ∈ {"usuario","presentacion","pantalla_completa","ocupado","juego","pantalla",""}`) | al cambiar (sondeo 5 s) | motor; main; escritorio-ws (→ `/escritorio` `no_molestar`) |
| `vision.rostro` | vision | `{"presente": bool, "n": int, "dist_m": float \| None, "frontal": bool \| None, "atencion": float}` | ≤ 2 Hz y solo si cambia | main (**solo** presencia/percepción, nunca el conectoma); motor (mirar al usuario) |
| `vision.mirada` | vision | `{"x": float, "y": float, "z": float, "confianza": float}` (x∈[−3,3], y∈[0,3;2,2], z∈[−3,3]) | ≤ 5 Hz y solo si cambia | motor (≤ 1 `look` por segundo) |
| `vision.estimulo` | vision | `{"fuente": "camara", "tipo": str ∈ {"llegada","salida","movimiento","acercamiento","luz"}, "magnitud": float, "firma": str}` | fásico; `llegada` con histéresis y refractario ≥ 120 s | main (→ conectoma y presencia, tabla de abajo) |
| `vision.atencion` | vision | `{"nivel": float}` (0..1) | cada 2 s o si cambia > 0,1 | main (percepción; nada al conectoma) |
| `vision.estado` | vision | `EstadoVision.a_dict()` (§C2.9) | al cambiar | hud (`/ws` → `vision`); main |
| `vision.camara` (v0.1, ampliado) | aura (`fuente:"vistazo"`) y vision (`fuente:"continua"`, repetido cada 5 s mientras está abierta) | `{"activa": bool, "habilitada": bool, "duracion_ms": int, "fuente": "vistazo"\|"continua"}` (sin `fuente` = `"vistazo"`) | al abrir/cerrar; continua cada 5 s | hud (LED = OR de fuentes); main (consola = OR) |
| `pantalla.estado` | vision | `{"cambio": float, "brillo": float, "tono": int \| None, "video": bool, "pantalla_completa": bool \| None, "reproduciendo": bool \| None, "no_molestar": bool, "privada": bool, "clase": str, "titulo": str}` (`titulo == ""` si `privada`; ≤ 60) | 0,2–1 Hz y si cambia | main (percepción, `pc.no_molestar`, vetos); hud (`/ws` → `pantalla`) |
| `pantalla.estimulo` | vision | `{"firma": str, "magnitud": float}` | ≤ 1 cada 5 s | main (→ conectoma, tabla de abajo) |
| `pc.accion` | main | `{"tipo": str, "resultado": str, "destino": str \| None}` (auditoría; nunca el texto) | por acción de ControlPC | hud (`/ws` → `accion`) |

`permiso.solicitado` (v0.1, lo publica `core/permisos.py`) gana el campo opcional **`vista_previa: str`** (≤ 500, solo para `prerrellenar`/`escribir_texto`): se reenvía **solo** por `/escritorio`; `/ws` lo elimina; main no lo verbaliza y nadie lo registra. `descripcion` nunca lleva el texto. La vista previa y el recuento de caracteres de la `descripcion` usan **el mismo saneado** con que ControlPC escribe (`permisos.sanear_texto_accion` reutiliza `control_pc._sanear`: controles → espacio, invisibles borrados): lo confirmado es lo que se escribe.

**Traducción al conectoma (grupo `orquestacion`; `core/sincan_connectome.py` queda FUERA de alcance en la v0.2).** Solo con inyectables existentes:

| Evento | Inyección | Límite |
|---|---|---|
| `vision.estimulo{tipo:"llegada"}` | `inyectar("rostro", presente=True, n=…)` | ya trae refractario ≥ 120 s |
| `vision.estimulo{tipo:"salida"}` | `inyectar("rostro", presente=False)` + `inyectar("usuario_presencia", presente=False)` | — |
| `vision.estimulo{movimiento\|acercamiento\|luz}` | nada (hasta que exista `estimulo_visual`, v0.3) | — |
| `vision.rostro`, `vision.atencion`, `vision.mirada` | **nada** | — |
| `pantalla.estimulo` | `inyectar("ventana_cambia", clase=<pantalla.estado.clase real>, titulo=firma)` | ≤ 1 cada 30 s y solo si la firma cambió |
| `escritorio.evento{toque}` | `inyectar("recompensa", magnitud=0.3)`; ≥ 5 toques en 30 s → `inyectar("error", severidad=0.4)` (enfado) | ≤ 1 inyección cada 5 s |
| `escritorio.evento{soltada, altura > 0.3}` | `inyectar("error", severidad=0.2)` | ≤ 1 cada 5 s |

**Presencia (dueño único: main).** `presencia.registrar_rostro(True)` en cada `vision.estimulo{llegada}` y después **cada 30 s** mientras `vision.estado().presente is True`; `registrar_rostro(False)` solo con `vision.estimulo{salida}`. `VisionContinua` no escribe en la presencia. En `core/presence.py` la regla 2 de §7 gana un caso: un rostro visto hace **< 60 s**, si fue el último registro, vale aunque la inactividad sea ≥ 600 s (quien ve un vídeo largo sin tocar nada sigue presente y las iniciativas no se van a Telegram); un `registrar_rostro(False)` posterior lo anula.

### C2.4 Protocolo WS `/escritorio` (grupo `escritorio-ws`, en `interface/hud_server.py`)

v0.2.2: eventos y mensajes nuevos en `docs/V022_CORREO_DRIVE_SCRIPTS.md` §11–§12.

1. **Ubicación:** nuevo path `/escritorio` en el servidor WebSocket del HUD (mismo `ServidorHUD`, `settings.hud_puerto`, `127.0.0.1`, nunca `0.0.0.0`). El grupo PUEDE crear `interface/escritorio_link.py` y componerlo desde `ServidorHUD`.
2. **Arranque:** `ServidorHUD` arranca si `hud_habilitado or tiene_escritorio`; sirve `/` y `/ws` **solo** con `hud_habilitado` (si no, 404); sirve `/escritorio` solo con `tiene_escritorio`. Nuevo `diagnostico_escritorio() -> DiagnosticoModulo` (clave `"escritorio"`; `OK` con `ws://127.0.0.1:<p>/escritorio` **sin** token; `OFF` si `not tiene_escritorio`).
3. **Token:** `secrets.token_urlsafe(16)`, distinto del token del HUD, nuevo en cada arranque. Se escribe en `settings.escritorio_token_file` como `{"url":"ws://127.0.0.1:<puerto>/escritorio","token":"…","pid":<os.getpid()>}` de forma **atómica** (temporal en el mismo directorio + `os.replace`) con 0o600. `cerrar()` lo borra **solo** si su contenido sigue teniendo el token y el pid propios. El token nunca aparece en logs, en `repr`, en el `hola` de `/ws` ni en diagnósticos.
4. **Handshake:** query `?t=<token>`, `hmac.compare_digest`. **Origin:** ausente (Electron: el `WebSocket` global de Node 24 no envía Origin) **o** `http://127.0.0.1:<puerto>` / `http://localhost:<puerto>` (página del HUD, solo si `hud_chat`). Otro Origin o token malo → `403`.
5. **Límites:** por tipo de cliente — **1** sin Origin (Electron; uno nuevo **reemplaza** al anterior, que se cierra con código 4001) y **hasta 2** con Origin del HUD (el 3.º → 403). Ritmo por cliente **y por clase** (exceso: se descarta): `chat` y lo no reconocido ≤ 5/s; `caja` y `evento{toque,soltada,cursor_encima}` ≤ 5/s; `hola_app`, `control`, `confirmacion` y `evento{mostrada,ocultada,chat_abierto}` ≤ 20/s, con `log.info` al descartar, y de `mostrada`/`ocultada` nunca se pierde la última (si no cabe, se publica al vencer la ventana de 1 s: gana la más reciente). Así una ráfaga de toques no tira el estado de visibilidad que main usa para `_overlay_visible`; texto 1..2000 caracteres; ≤ 8 KiB por mensaje; JSON inválido o `tipo` desconocido → se ignora con `log.debug`.
6. **Cambio en `/ws`:** justo **después** del `hola` (que **no** cambia) y solo si `tiene_escritorio and hud_chat`, se envía `{"tipo":"escritorio","datos":{"token": "...", "url": "/escritorio"}}`. Además `/ws` traduce los eventos nuevos de §C2.3 (`vision.estado`→`vision`, `pantalla.estado`→`pantalla` sin `titulo` si `privada`, `pc.accion`→`accion`, `vision.camara` con `fuente`) y elimina `vista_previa` de `permiso`. Nunca reenvía `escritorio.chat` ni `escritorio.sistema`.

Mensajes (JSON en ambos sentidos):

| Dirección | Mensaje | Quién | Efecto |
|---|---|---|---|
| App → R.A.M.S. | `{"tipo":"hola_app","pid": int,"version": str}` | solo sin Origin | publica `escritorio.app{pid}` (pid > 0) |
| App → R.A.M.S. | `{"tipo":"chat","texto": str}` | ambos | publica `escritorio.chat{texto}` (**no** `usuario.entrada`) |
| App → R.A.M.S. | `{"tipo":"evento","nombre": str,"datos": dict}` | solo sin Origin | `nombre ∈ EVENTOS_ESCRITORIO`, `datos` solo escalares ≤ 1 KiB → `escritorio.evento` |
| App → R.A.M.S. | `{"tipo":"control","accion": str}` | ambos | `accion ∈ CONTROLES_ESCRITORIO` → `escritorio.control`; `"silenciar"` → `hud.control{accion:"silenciar"}` |
| App → R.A.M.S. | `{"tipo":"confirmacion","id": str,"aprobado": bool}` | ambos | valida `^[0-9a-f]{8}$` y bool → `escritorio.confirmacion` |
| App → R.A.M.S. | `{"tipo":"caja","x","y","w","h"}` (int, píxeles físicos) | solo sin Origin | `0 < w ≤ 2000`, `0 < h ≤ 2000`; ≤ 2 Hz → `escritorio.caja` |
| R.A.M.S. → App | `{"tipo":"hola","datos":{"version","nombre_ia","usuario"}}` | ambos | estado "en línea" |
| R.A.M.S. → App | `{"tipo":"subtitulo","datos":{"texto","exp","canal","origen"}}` | ambos | de `salida.subtitulo` |
| R.A.M.S. → App | `{"tipo":"voz","datos":{"hablando": bool}}` | ambos | de `voz.inicio`/`voz.fin` |
| R.A.M.S. → App | `{"tipo":"orden","datos":{"accion", ...}}` | solo sin Origin | de `escritorio.orden` |
| R.A.M.S. → App | `{"tipo":"permiso","datos":{"id","descripcion","vista_previa"?,"destructiva","pendiente": bool,"aprobado"?}}` | ambos | de `permiso.solicitado` (`pendiente:true`) / `permiso.resuelto` (`pendiente:false`) |
| R.A.M.S. → App | `{"tipo":"sistema","datos":{"texto"}}` | ambos | de `escritorio.sistema` |
| R.A.M.S. → App | `{"tipo":"estado","datos":{"aburrimiento","fatiga","valencia"}}` | solo sin Origin | de `homeostasis.estado` (`aburrimiento = estado.boredom`) a ≤ 1 Hz |
| R.A.M.S. → App | `{"tipo":"travesuras","datos":{"activas","hasta_ts"}}` | ambos | de `motor.travesuras` |
| R.A.M.S. → App | `{"tipo":"no_molestar","datos":{"activo": bool,"motivo": str,"usuario": bool}}` | ambos | de `pc.no_molestar` (`motivo` fuera de la lista → `""`; `usuario = activo and motivo == "usuario"`: el "no molestar" puesto a mano). **Siempre** en la instantánea al conectar (sin `pc.no_molestar` previo: todo `false`, que es el estado inicial de main) |
| R.A.M.S. → App | `{"tipo":"vision","datos":{"camara": bool}}` | solo sin Origin | OR de `vision.camara.activa` por `fuente` (§C2.13: ojo del overlay e icono de la bandeja); solo cuando cambia (la continua se repite cada 5 s) y **siempre** en la instantánea |

| App ↔ R.A.M.S. | `puter.estado`, `llm.pedir`, `llm.respuesta` | solo sin Origin | respaldo Puter del Área de Broca: contrato, límites y ritmo en §C2.15 |

**Instantánea al conectar** (en este orden, antes de cualquier difusión): `hola`, el `permiso` pendiente (si hay), `travesuras` (si se conoce), `no_molestar` y, solo para Electron, `vision` y el último `estado`.

La página del HUD maneja `hola`, `subtitulo`, `voz`, `permiso`, `sistema`, `travesuras` y `no_molestar` en el panel RINE (texto con `textContent`, botones de confirmación ligados al `id`, enlaces solo `https://` y abiertos con `rel="noopener noreferrer"` tras clic). El lanzador y el panel RINE están ocultos hasta que `/ws` envía el mensaje `escritorio`; si `/ws` vuelve a saludar y el mensaje siguiente al `hola` no es `escritorio`, se cierran y ocultan. El botón «No molestar» refleja **siempre** `no_molestar.usuario` (también tras recargar) y manda `on`/`off` según ese estado real. Al recibir `hola` de `/escritorio`, las confirmaciones mostradas pasan a revisión (botones bloqueados): las que la instantánea trae como pendientes vuelven a estar vigentes en la misma burbuja y el resto se marca «Ya no está pendiente.». **Amadeus Desktop** (grupo desktop) también debe tomar su `st.noMolestar` de `no_molestar.usuario` y no reafirmar al reconectar un estado que R.A.M.S. ya cambió (p. ej. un `off` enviado desde el HUD). **Gana R.A.M.S.:** main no persiste el "no molestar" del usuario, así que tras reiniciar R.A.M.S. la instantánea trae `usuario:false` y Electron lo adopta; solo reafirma (`no_molestar_on|off`) un cambio que el usuario hizo en la bandeja **mientras** estaba desconectado.

### C2.5 VTuber AI Studio: servidor, protocolo, MCP y modo escritorio (grupo `visor-escritorio`)

**C2.5.1 `lib/dispatch.js` (nuevo, puro, sin red):**
```js
export function normalizeCommands(body)            // lista validada contra COMMAND_TYPES; lanza Error
export function targetsFor(viewers, commands)      // lote con algún 'desktop' → solo viewers.desktop===true;
                                                   // un lote que mezcla 'desktop' con otros tipos → lanza Error
export function desktopCount(viewers)              // nº de viewers desktop con visible===true
export function desktopTotal(viewers)              // nº de viewers desktop (visibles u ocultos)
export function fuentePermitida(commands, source, env)  // 'desktop'/'theme' solo si source==='rams' o
                                                   // (source==='mcp' && env.MCP_DESKTOP==='1')
```

**C2.5.2 `server.js`:**
- `export function createServer({ port, host, env } = {})` → `{ server, listen(): Promise<{port}>, close(): Promise<void>, dispatch(body, source) }`; los viewers, `pending`, `history`, `avatarState` y `viewerSeq` son **por instancia**. Solo llama a `listen` si es el módulo principal (`import.meta.url === pathToFileURL(process.argv[1]).href`). `PORT`: si la variable está definida y `Number.isInteger(n) && n >= 0` → `n` (acepta `0`); si no, `3939`.
- **Viewer:** `{ id, res, obs, desktop, visible, theme }` con `desktop = url.searchParams.has('desktop')`, `visible = false`, `theme = null`; `viewerSeq += 1` por cada conexión SSE.
- **Seguridad de todo POST** (`/api/command`, `/api/ack`, `/api/state`, `/mcp`): `Content-Type` debe empezar por `application/json` (fuerza el preflight) y `Origin` debe faltar o ser `http://127.0.0.1:<puerto real>` / `http://localhost:<puerto real>`; si no → `403 {ok:false, code:'origen'}`. CORS: `Access-Control-Allow-Origin` solo devuelve el Origin permitido (nunca `*`), `Vary: Origin`, `Access-Control-Allow-Headers: Content-Type, X-Source`.
- **`/api/command`:** `normalizeCommands` → si el lote lleva `desktop`/`theme` y `!fuentePermitida` → `403 {ok:false, code:'fuente'}`; sin viewers → `409 {ok:false, code:'sin_visores', error}`; lote `desktop` sin ningún viewer desktop (`desktopTotal == 0`) → `409 {ok:false, code:'sin_overlay', error}`; lote mixto → `400`. El ack se espera de los viewers de `targetsFor`.
- **`POST /api/state`:** si `body.viewerId` coincide con un viewer, actualiza `viewer.visible = !!body.viewer?.visible` y `viewer.theme = body.viewer?.theme ?? null`; guarda `avatarState` **sin** las claves `viewers`, `desktopViewers`, `desktopViewersTotal`, `viewerSeq`, `viewerId`, `viewer`.
- **`GET /api/state`:** `{ ...avatarState, viewers, desktopViewers: desktopCount, desktopViewersTotal: desktopTotal, viewerSeq }` (los calculados **al final**). `mcpCtx.getState` devuelve lo mismo.

**C2.5.3 `lib/protocol.js`** (fuente de verdad; se documenta la regla "**como mucho un visor reproduce TTS**: el panel no-OBS si existe; los visores `?obs=1`/`?desktop=1` nunca, salvo `?tts=1`"):
```js
desktop: {
  desc: 'Solo visores de escritorio (?desktop=1) y solo con la ventana visible: moverse por la pantalla.',
  example: { type: 'desktop', action: 'walk_to', x: 0.75, speed: 1 },
  fields: 'action: walk_to|peek|chase_cursor|seek_user|wander|stop; x 0..1 (fracción del ancho) ' +
          'o px con unit:"px"; edge left|right; seconds ≤ 15; speed 0.3..3',
},
theme: {
  desc: 'Tema visual. "amadeus" = look de Kurisu (se aplica al cargar el modelo; recarga si cambia).',
  example: { type: 'theme', name: 'amadeus', rim: true, scanlines: 0.07 },
  fields: 'name amadeus|none; rim bool; scanlines 0..0.2',
},
```
`show`/`hide`/`enter`/`leave` **no** son acciones de `desktop`: la visibilidad y la entrada/salida por el borde pertenecen al main de Electron (`overlay:order`, §C2.7.2).

**C2.5.4 `lib/mcp.js`:** `avatar_command` rechaza (error JSON-RPC) los tipos `desktop` y `theme`; la herramienta `avatar_desktop` solo se registra con `process.env.MCP_DESKTOP === '1'`. `mcp-server.js` NO cambia.

**C2.5.5 `public/app.js` — modo escritorio (`?desktop=1`):**
- Implica lo de `?obs=1` (transparente, sin panel) y además `OrbitControls.enabled = false`; encuadre `full` salvo `?camera=` explícito; **ignora `c.tts`** salvo `?tts=1` (igual para `?obs=1`); el `move` ignora `x/z` (respeta `rotation` y `walk`) y lo anota en el log de actividad.
- **Caja** del avatar de 340×600 posicionable con `transform`; hit-test por raycast dentro de la caja; si existe `window.amadeusDesktop` (§C2.7.2): `reportAvatar(rect)` al moverse (≤ 30 Hz), `setInteractive(bool)` como **solicitud**, `onCursor`, `onOrder`, `onVisibility`.
- **Acciones `desktop`** (`walk_to`, `peek`, `chase_cursor`, `seek_user`, `wander`, `stop`) con la curva de caminar del visor; con la ventana oculta, ack `{ok:false, type:'desktop', error:'oculta'}`.
- **Órdenes del main de Electron** (`onOrder`), tabla normativa:

| `accion` | Campos | Qué hace el renderer |
|---|---|---|
| `aparecer` | `borde: 'left'\|'right'`, `modo: 'entrar'\|'asomarse'` | entra caminando desde el borde (o se queda asomada en él) |
| `esconderse` | — | sale caminando por el borde más cercano y al terminar llama a `amadeusDesktop.hidden()` |
| `estado` | `aburrimiento`, `fatiga`, `valencia` | modula reflejos de reposo (no decide nada) |
| `suelo` | `inferior: number` (px CSS) | recoloca la línea del suelo |
| `relayout` | — | recalcula tamaños tras cambio de monitor/DPI |
| `camara` | `activa: boolean` | muestra u oculta el ojo 👁 de cámara abierta en la caja (§C2.13; sin capturar clics) |

- **Eventos que emite** (`amadeusDesktop.event`): `toque` en un `pointerup` sin arrastre; `soltada{altura}` (0..1) al soltar tras arrastrar; `cursor_encima` como mucho una vez cada 10 s. `openChat()` en `contextmenu` sobre la caja.
- **Visibilidad y estado:** `onVisibility(v)` → con `false`, `renderer.setAnimationLoop(null)`; con `true`, lo reanuda. Sin `window.amadeusDesktop`, `visible = document.visibilityState === 'visible'`. `postState()` añade `viewerId` (del evento SSE `hello`) y `viewer: { desktop: DESKTOP, visible, theme: temaActivo ? 'amadeus' : 'none' }`.
- **Rendimiento:** en modo escritorio, render a ≤ 30 fps en reposo y ≤ 60 fps caminando o arrastrando.
- **Tema:** flag `temaDeseado` (`?theme=amadeus` o comando `theme`). El tema se aplica **solo dentro de `loadModel`**, tras `avatarRoot.add(vrm.scene)` y **antes del primer render**:
```js
const mod = await import('/amadeus_theme.js');             // import dinámico (archivo del grupo kurisu)
temaActivo = mod.aplicarTemaAmadeus(vrm, { THREE, rim, scanlines, escena: scene, avatarRoot,
                                           modoEscritorio: DESKTOP });   // THREE = el namespace que app.js ya importa
```
  El comando `theme` cambia `temaDeseado`; si difiere del tema activo, **recarga el modelo actual** (también para `none`); si es igual, no hace nada (idempotente). `temaActivo.update(t)` en `frame()`; `dispose()` al recargar. `snapshot().current.theme` = tema activo. Si el import falla, sigue sin tema y lo registra en su log.

**C2.5.6 Código de terceros local:** `public/vendor/` con `three@0.180.0` (`build/three.module.js`, `build/three.core.js` y el cierre transitivo de los `examples/jsm` que importa `app.js`) y `@pixiv/three-vrm@3.4.4` (`lib/three-vrm.module.min.js`), descargados del registro de npm, con versiones y SHA-256 en `public/vendor/VENDOR.md`. `index.html`: el importmap apunta a `/vendor/...`. El VRM de ejemplo **sigue** en `DEFAULT_MODEL_URL` (CDN) como **dato** (el usuario puede poner su modelo en `models/`).

### C2.6 API JS del tema Amadeus (grupo `kurisu`, `public/amadeus_theme.js`)

```js
// public/amadeus_theme.js  (ESM; NO importa three: lo recibe en opciones.THREE)
export const AMADEUS_PALETA;   // { hair:[...], iris:[...], boots:[...], skinShade, coatShade, tights, tie, tieShade, rim }
                               // todos los colores '#rrggbb'
export function clasificarMaterial(nombre);   // → 'hair'|'skin'|'eye'|'iris'|'tops'|'bottoms'|'shoes'|'socks'|null
                                              //   (tolera '(Outline)', '(Instance)', prefijos 'N00_…')
/**
 * @param vrm  instancia de @pixiv/three-vrm ya añadida a la escena, ANTES del primer render
 * @param opciones { THREE (obligatorio), rim?: boolean=true, scanlines?: number=0.07,
 *                   escena?: THREE.Scene, avatarRoot?: THREE.Object3D, modoEscritorio?: boolean=false }
 * @returns { update(t:number): void, dispose(): void }
 */
export function aplicarTemaAmadeus(vrm, opciones);
```
Hace: recoloreado MToon por **nombre de material** (surface **y** outline); si la imagen de una textura no está disponible (ImageBitmap cerrado), omite el gradient map de ese material y sigue (sin lanzar). Corbata roja procedural atada a `upperChest` (salvo si una malla o material ya se llama *tie*/*corbata*) y medias oscuras en UV de `Body_00` (triángulos bajo el borde de los shorts, todos con la misma orientación para que el relleno `nonzero` no anule las islas UV en espejo). Un modelo cuyo `meta` (`name`/`title` o `references`/`reference`) contiene *Kurisu/Makise/紅莉栖/牧瀬/Amadeus* ya es de Kurisu: no se recolorea ni recibe corbata o medias (solo rim, scanlines y marco). **Idempotente:** si `vrm.userData.amadeusTema` ya existe, devuelve ese handle (no duplica la corbata). Marco: con `modoEscritorio:false`, inyecta `<link rel="stylesheet" href="/amadeus_theme.css">` (si no existe) y el marco CSS (si no hay `document`, p. ej. en Node, lo omite sin lanzar); con `modoEscritorio:true`, **sin** marco CSS: solo rim y el quad de scanlines (`CustomBlending` que preserva alpha). `dispose()` libera canvas/geometrías/materiales propios y quita el quad. **Sin logos oficiales.**

### C2.7 Amadeus Desktop (grupo `desktop`, `desktop/**`)

**C2.7.1 Procesos y archivos.** `main.js` (proceso principal), `preload-overlay.js`, `preload-chat.js`, `chat.html` (+css/js), y módulos **puros** sin Electron en `desktop/lib/` (testeables con `node --test`): `cli.js` (parser de `--amadeus=`), `hit.js` (decisión de interactividad y validación de la caja), `mapeo.js` (WS ↔ IPC), `token.js` (lectura y validación del archivo de token), `csp.js` (CSP y hash del importmap).

**C2.7.2 IPC (preloads con `contextBridge`, sin exponer `ipcRenderer`):**
```js
// preload-overlay.js → window.amadeusDesktop
{
  version: 3,
  setInteractive(v: boolean): void,                // overlay:interactive (SOLICITUD; decide main.js)
  reportAvatar(r: {x,y,w,h}): void,                // overlay:rect (px CSS relativos a la ventana)
  hidden(): void,                                  // overlay:hidden (terminó la salida por el borde)
  openChat(): void,                                // overlay:chat (acción del usuario: puede enfocar)
  event(nombre: string, datos?: object): void,     // overlay:event → R.A.M.S. (solo EVENTOS_ESCRITORIO del renderer)
  onCursor(cb: (p:{x,y}) => void): () => void,     // overlay:cursor
  onOrder(cb: (o:object) => void): () => void,     // overlay:order (tabla de §C2.5.5)
  onVisibility(cb: (visible:boolean) => void): () => void,  // overlay:visibility
}
// preload-chat.js → window.amadeusChat
{
  version: 1,
  send(texto: string): Promise<boolean>,                                         // chat:send (invoke)
  onMessage(cb: (m:{de:'amadeus'|'yo'|'sistema', texto:string, url?:string}) => void): () => void, // chat:msg
  onStatus(cb: (s:'en_linea'|'desconectada') => void): () => void,              // chat:status
  onVoz(cb: (hablando:boolean) => void): () => void,                            // chat:voz
  onPermiso(cb: (p:{id, descripcion, vista_previa?, pendiente:boolean, aprobado?, caducada?}) => void): () => void, // chat:permiso
                                                   // caducada:true = ya no está pendiente tras reconectar
  confirmar(id: string, aprobado: boolean): Promise<boolean>,                   // chat:confirm (invoke):
                                                   // true si salió por el WS; false (sin R.A.M.S.) → botones otra vez activos
  control(accion: 'no_molestar_on'|'no_molestar_off'|'basta_travesuras'): void, // chat:control
  abrirEnlace(url: string): void,                                               // chat:open-url
  hide(): void,                                                                 // chat:hide
}
```
Validación en `main.js`: IPC del overlay solo si `e.senderFrame.origin === new URL(VTUBER_AI_URL).origin`; IPC del chat solo si `e.sender === chat.webContents`. `chat:confirm`: `id` cumple `^[0-9a-f]{8}$` y `aprobado` es booleano → WS `{tipo:'confirmacion', id, aprobado}`. `chat:control`: lista cerrada. `chat:open-url`: `^https://`, ≤ 300, y la URL tiene que haber llegado antes en un `chat:msg`/notificación de R.A.M.S. (conjunto de URLs ofrecidas) → `shell.openExternal`. `setWindowOpenHandler` **siempre** deniega; `will-navigate` fuera del origen del visor → `preventDefault`.

Mapeo WS → IPC (`lib/mapeo.js`, puro): `hola`→`chat:status 'en_linea'`; `subtitulo`→`chat:msg{de:'amadeus'}`; `sistema`→`chat:msg{de:'sistema'}`; `voz`→`chat:voz`; `permiso`→`chat:permiso`; `orden{aparecer}`→`overlay.showInactive()` + `overlay:visibility(true)` + `overlay:order{aparecer}` + WS `evento{mostrada}`; `orden{esconderse}`→`overlay:order{esconderse}` (y al recibir `overlay:hidden`: `overlay.hide()`, `overlay:visibility(false)`, WS `evento{ocultada}`); `orden{abrir_chat}`→`chat.showInactive()`; `orden{notificar}`→`Notification` + `chat:msg{de:'amadeus', texto, url}`; `estado`→`overlay:order{estado}`; `travesuras`→ casilla "Quieta" de la bandeja; `no_molestar`→ casilla "No molestar" = `datos.usuario` (§C2.4); `vision`→ icono de cámara de la bandeja + `overlay:order{accion:'camara', activa}`. Al abrir `/escritorio`, Electron envía `{tipo:'hola_app', pid: process.pid, version}`; `chat_abierto` se envía cuando el **usuario** abre el chat.

**C2.7.3 Autoridad del overlay (`lib/hit.js`, puro).** El renderer solo **pide** ser interactivo; `main.js` pasa a interactivo (`setIgnoreMouseEvents(false)`) **solo** si el cursor global está dentro de `lastRect` con 8 px de margen. `lastRect` se acepta solo si `w ≤ 600`, `h ≤ 900` y cae dentro del `workArea`; si no, se ignora. **Watchdog:** cursor > 1,5 s fuera de la caja, `render-process-gone` o `unresponsive` → `setIgnoreMouseEvents(true, {forward:true})`. Cursor global: Windows `screen.getCursorScreenPoint()` a 30 Hz; Hyprland por `.socket.sock` a **15 Hz** con la caja visible y **4 Hz** si el cursor está a > 300 px, **una** petición en curso y timeout 150 ms; otros Wayland → sin cursor global (se recomienda `start:x11`). La caja se envía a R.A.M.S. (`{tipo:'caja'}`) en **píxeles físicos globales** (Windows: `screen.dipToScreenRect`; Hyprland: coordenadas de layout + origen del monitor), ≤ 2 Hz y solo si cambia.

**C2.7.4 Ventanas y foco.** Overlay: `transparent:true, frame:false, alwaysOnTop:true` (nivel `screen-saver`), `skipTaskbar:true, focusable:false` (Win), `show:false`, `contextIsolation:true, sandbox:true, nodeIntegration:false`; carga `${VTUBER_AI_URL}/?obs=1&desktop=1&camera=full&theme=amadeus` **solo** tras un `GET ${VTUBER_AI_URL}/` correcto (si falla, reintenta cada 5 s). Con `AMADEUS_EXCLUIR_CAPTURA=1` (defecto) → `overlay.setContentProtection(true)` (no sale en capturas, OBS ni Discord; README lo avisa). Chat: ventana enfocable aparte; **las aperturas que pide R.A.M.S. usan `showInactive()` y nunca `focus()`**; el foco solo llega por acción del usuario (bandeja, atajo, clic derecho sobre el avatar, clic en la ventana); `chat.html` ignora Enter durante 600 ms tras recibir el foco.

**C2.7.5 Bandeja, atajos y CLI.** Bandeja: Llamar a Amadeus · Abrir chat · No molestar (casilla) · Quieta (casilla, `basta_travesuras`) · Pausar visión · Dejar pasar clics · Salir. Atajos (`globalShortcut`, Win/X11): `Ctrl+Alt+A` llamar/alternar; `Ctrl+Alt+Space` chat (acción del usuario: enfoca); `Ctrl+Alt+Shift+Q` `basta_travesuras` + esconderse (§C1.12); `Ctrl+Alt+V` `pausar_vision`; `Ctrl+Alt+Shift+A` emergencia (oculta y click-through, sin R.A.M.S.). CLI `--amadeus=toggle|show|hide|chat|quit|basta|vision_pausa|clics` (single-instance + `additionalData`), para los `hl.bind` de Hyprland.

**C2.7.6 Conexión a R.A.M.S.** La hace el **main** de Electron (`WebSocket` global de Node), nunca el renderer. Ruta del token: variable de entorno `RAMS_ESCRITORIO_TOKEN_FILE` si existe; si no, Windows `%LOCALAPPDATA%\rams\escritorio.json`; Linux `$XDG_RUNTIME_DIR/rams/escritorio.json`, y **sin `XDG_RUNTIME_DIR` no conecta** (nunca `/tmp`). En Linux exige dueño = `process.getuid()` y `(mode & 0o077) === 0`. Relee el token en cada reintento (backoff 1 → 30 s).

**C2.7.7 CSP y código remoto.** CSP por cabecera para el overlay: `script-src 'self' 'sha256-<importmap>'` (**sin** jsDelivr), `connect-src 'self' https://cdn.jsdelivr.net` (solo el VRM de ejemplo, que es dato), `img-src 'self' data: blob:`, `style-src 'self' 'unsafe-inline'`. El hash del importmap se calcula en cada navegación del frame principal (`did-start-navigation`) con un `GET` del `index.html`; si cambió respecto al de la carga, recarga una vez.

**C2.7.8 Modo `--smoke`.** Sin Tray, sin `globalShortcut`, sin `setLoginItemSettings`, sin conexión a R.A.M.S. y sin `fetch` a `VTUBER_AI_URL`. Crea las ventanas con `show:false` y carga stubs `file://` (`desktop/test/stub-overlay.html`, `chat.html`) que responden "ok" por IPC; comprueba que existen `main.js`, los preloads y `chat.html` y que `typeof WebSocket === 'function'`. Escribe `smoke OK` y hace `app.exit(0)` en ≤ 5 s; un `setTimeout` de seguridad hace `app.exit(1)`.

**C2.7.9 Plataformas.** Scripts `start` (por defecto), `start:x11` (`--ozone-platform=x11`, por defecto en Wayland sin Hyprland), `smoke`, `test` (`node --test test/`). Reglas de Hyprland de ejemplo (float, pin, move, size, `no_initial_focus` en overlay **y chat**, `no_follow_mouse`) y `hl.bind` a la CLI. `README.md` incluye una prueba manual de la transparencia en Wayland y la alternativa X11.

### C2.8 API Python de `ControlPC` (grupo `control-pc`, `lobes/control_pc.py`)

```python
PATRONES_TRAVESURA: Final            # re-export de core.contracts
SAFE_ALIAS_RE: Final                 # re-export de core.contracts
TITULOS_PROPIOS_RE: Final = re.compile(r"^(Amadeus Chat|Amadeus Overlay|Amadeus)( [-—–] .*)?$")

def activar_dpi_por_monitor() -> bool:
    """Windows: SetProcessDpiAwarenessContext(-4) (idempotente). Fuera de Windows: False. Nunca lanza.
    main la llama al arrancar, ANTES de construir ControlPC o VisionContinua (mss cambia la consciencia de DPI)."""

@dataclass(frozen=True)
class ObjetivoVentana:
    hwnd: int | None          # Windows
    pid: int | None
    exe: str                  # nombre del ejecutable (Win) o clase (Hyprland)
    direccion: str | None     # address 0x… (Hyprland)
    clase: str
    def a_args(self) -> dict[str, Any]: ...      # {"hwnd","pid","exe","direccion","clase"}
    @classmethod
    def desde_args(cls, args: Mapping[str, Any]) -> "ObjetivoVentana | None": ...   # valida tipos; None si no casa

@dataclass(frozen=True)
class ResultadoControl:
    ok: bool
    mensaje: str              # en español, sin el texto tecleado
    datos: Mapping[str, Any] = field(default_factory=dict)  # {"destino","len","sha256_12","abortada_usuario"}

class ControlPC:
    def __init__(self, settings: Settings, *, reloj: Callable[[], float] = time.monotonic,
                 plataforma: str | None = None) -> None          # plataforma inyectable en tests ("win"|"hypr"|"otro")
    disponible: bool
    async def iniciar(self) -> DiagnosticoModulo                  # clave "control_pc"; arranca el sondeo de ventana
    def destinos(self) -> Mapping[str, Mapping[str, Any]]         # clave → cómo abrirla en este SO (solo DESTINOS_BASE)
    async def abrir_destino(self, alias: str) -> ResultadoControl          # alias = CLAVE de DESTINOS_BASE
    async def buscar_en(self, alias: str, consulta: str) -> ResultadoControl
    def enlace_prerrelleno(self, alias: str, texto: str) -> str | None     # puro
    async def prerrellenar(self, alias: str, texto: str) -> ResultadoControl
    def ventana_activa(self) -> ObjetivoVentana | None            # caché (Win: llamada directa, < 1 ms)
    def ventana_previa(self) -> ObjetivoVentana | None            # última en primer plano que NO es propia
    def excluir_pids(self, pids: Iterable[int]) -> None           # Amadeus Desktop + descendientes (psutil si hay)
    def pids_excluidos(self) -> frozenset[int]
    def set_caja_avatar(self, caja: tuple[int, int, int, int] | None) -> None   # px físicos (hacia_avatar)
    def inactividad_s(self) -> float | None                       # Win: GetLastInputInfo; Hyprland: None
    def quieto(self) -> bool | None                               # Hyprland: archivo-bandera de hypridle
    def cursor(self) -> tuple[int, int] | None                    # caché
    async def no_molestar(self) -> tuple[bool, str]               # (activo, motivo) — §C1.11
    async def puede_teclear(self, objetivo: ObjetivoVentana) -> tuple[bool, str]
    async def escribir_texto(self, texto: str, *, objetivo: ObjetivoVentana,
                             espera_foco_s: float = 10.0) -> ResultadoControl   # NUNCA pulsa Enter
    async def puede_travesura(self, *, presente: bool, voz_activa: bool, confirmacion_pendiente: bool,
                              video_activo: bool) -> tuple[bool, str]
    async def empujon_cursor(self, patron: str, amplitud_px: int, *, segundos: float = 0.5) -> ResultadoControl
    def anti_contrasena(self, texto: str, *, titulo: str = "") -> bool
        # True = rechazar: config.parece_secreto(texto, settings.secretos()) or config.titulo_sensible(titulo)
    def ts_ultima_inyeccion(self) -> float | None
    async def cerrar(self) -> None
```
Reglas:
- **Ventana previa:** anillo de las últimas 8 ventanas en primer plano que excluye `pids_propios` (R.A.M.S. y sus ancestros, `core.permisos.calcular_pids_propios()`), `pids_excluidos()` y los títulos `TITULOS_PROPIOS_RE`. Windows: tarea asyncio a 2 Hz con `GetForegroundWindow`/`GetWindowThreadProcessId`/`QueryFullProcessImageNameW` (ctypes, no bloquea). Hyprland: caché refrescada por una tarea asyncio con `asyncio.open_unix_connection` al `.socket.sock` (`j/activewindow`, `j/cursorpos`), timeout 250 ms, ≤ 2 Hz. Nunca `subprocess.run` ni sockets bloqueantes en el loop.
- **escribir_texto:** sanea `\r\n\t`; **no fuerza el foco**: espera hasta `espera_foco_s` a que `GetForegroundWindow() == hwnd` (o `activewindow.address == direccion`); si no ocurre, `ResultadoControl(False, "No tuviste el foco en <app> a tiempo; no escribí nada.")`. Revalida `puede_teclear` antes de cada trozo (foco, no elevado, no `ES_PASSWORD`, sin modificadores, título no sensible) y aborta si el usuario teclea.
- **Travesuras** (§C2.12): solo `SetCursorPos` (Win). En Hyprland `puede_travesura` devuelve `(False, "sin estado de botones en Wayland")`. `empujon_cursor` devuelve `datos["abortada_usuario"] = True` si el usuario movió el ratón > 6 px; si es Windows quien recoloca el cursor (`ClipCursor`, fuera de todo monitor) para con `ok=False`, `datos["recolocado"] = True` y `abortada_usuario` en `False` (no se culpa al usuario ni se apagan 2 h).
- **Tasas** (§C2.12) aplicadas aquí. Windows con `ctypes`; Linux con `wtype` (texto por stdin), `xdg-open`/`gtk-launch`. Ningún método público lanza; sin `shell=True`; `create_subprocess_exec` + `shutil.which`. Importable sin ctypes/psutil/hyprctl (`disponible=False`).

### C2.9 API Python de `VisionContinua` (grupo `vision`, `lobes/vision_continua.py`) y cambio en `aura`

```python
@dataclass(frozen=True)
class MuestraCamara:   # solo escalares
    ts: float; rostro: bool; n: int; u: float | None; v: float | None; dist_m: float | None
    yaw_r: float | None; pitch_r: float | None; frontal: bool | None; mov: float; brillo: float
    score: float | None; ms_proc: float

@dataclass(frozen=True)
class MuestraPantalla:
    ts: float; cambio: float; brillo: float; tono: int | None; corte: bool; video: bool
    pantalla_completa: bool | None; reproduciendo: bool | None; clase: str; titulo: str
    privada: bool; ms_proc: float

@dataclass(frozen=True)
class EstadoVision:
    camara: str          # ∈ ESTADOS_VISION
    pantalla: str        # "off" | "metadatos" | "pixeles" | "pausada"
    fps_camara: float; fps_pantalla: float; nivel: int; detector: str | None
    presente: bool | None; atencion: float
    def a_dict(self) -> dict[str, Any]: ...

class VisionContinua:
    def __init__(self, settings: Settings, bus: BusEventos | None, *, presencia: Any | None = None,
                 reloj: Callable[[], float] = time.monotonic,
                 fabrica_captura=None, fabrica_pantalla=None) -> None
        # presencia: SOLO LECTURA (autopausa por ausencia ≥ 0,8); nunca registra rostros
    async def iniciar(self) -> list[DiagnosticoModulo]     # claves "vision_camara" y "vision_pantalla"
    async def cerrar(self) -> None                          # señala y await asyncio.to_thread(hilo.join, 2.0)
    def set_camara(self, activa: bool) -> str               # respeta camara_habilitada; devuelve estado efectivo
    def set_pantalla(self, modo: str) -> str                # off|metadatos|pixeles
    def pausar(self, segundos: float | None = None, motivo: str = "usuario") -> None
    def reanudar(self) -> None
    def estado(self) -> EstadoVision
    def calibrar_atencion(self) -> None                     # main la llama en cada turno local
    def como_vistazo(self, motivo: str) -> ResultadoVistazo | None
        # con la cámara continua abierta: ResultadoVistazo de la última muestra (< 1 s), sin abrir el
        # dispositivo, sin cupo ni backoff; None si no hay muestra reciente o la cámara no está abierta.
        # También ResultadoVistazo con error "ocupada" (el hilo está abriendo o soltando el dispositivo,
        # o hay una llamada) o "denegada" (pausa del usuario, bloqueo o juego, salvo motivo "usuario":
        # pedirlo en ese momento es consentimiento); así aura nunca abre un dispositivo ocupado.
    def set_caja_avatar(self, caja: tuple[int, int, int, int] | None) -> None   # máscara (px físicos)
```
Notas de la implementación (rev. de integración): `vision.mirada` se publica si el punto cambia > 4 cm o como latido cada 2 s (≤ 5 Hz); `pantalla.estado.no_molestar` sale solo de señales del sistema (pantalla completa, juego, llamada), **no** de la heurística de vídeo; en X11 la pantalla queda `OFF` (sin metadatos de ventanas no se pueden excluir las privadas: falla cerrada).
Reglas:
- **Hilos y release:** `pausar()`, `set_camara(False)` y `cerrar()` **solo** activan un `threading.Event` y vuelven (< 5 ms). El hilo de cámara sale de su bucle, hace `release()` en su `finally` y publica `vision.camara{activa:false, fuente:"continua"}` por `loop.call_soon_threadsafe`. Nunca `release()` ni `join()` desde el loop.
- Cámara: pide `CAP_PROP_FPS` 10–15 y procesa a `vision_fps`; YuNet preferido, Haar de respaldo (rutas propias, incluidas las de `opencv5`). Pantalla: `mss` (Windows) submuestreado / `grim -s 0.1 -t ppm -` (Hyprland, subproceso async), a `pantalla_fps` ∈ [0,2; 2]; máscara de la caja del avatar (`pantalla_excluir_avatar`); `privada` si el título casa con `settings.pantalla_excluir_re` → sin píxeles y `titulo=""`.
- Integrador asyncio a 2 Hz: flancos con histéresis, `llegada` con refractario ≥ 120 s, EMA. `vision.camara{activa:true, fuente:"continua"}` cada 5 s mientras está abierta. **Nunca** `imwrite`/`imencode`/`VideoWriter`/`ImageGrab`/`pytesseract`/`base64`/`httpx`/`open(...,"wb")` (test de código fuente). Degradación en escalera (CPU/temperatura/batería/juego/ausencia). Al bus solo escalares.

**`lobes/aura_vision_pc.py` (mismo grupo, cambio mínimo):** `set_fuente_continua(fn: Callable[[str], ResultadoVistazo | None] | None) -> None`; si está fijada y `fn(motivo)` devuelve algo, `vistazo()` lo devuelve sin abrir el dispositivo, sin contar cupo ni backoff y sin publicar `vision.camara`. `vision.camara` de aura lleva `fuente:"vistazo"`. `_DIRS_HAAR_SISTEMA` incluye las rutas de `opencv5`.

### C2.10 Corteza motora y puente (grupo `motor-escritorio`)

**`lobes/vtuber_ai_bridge.py`:**
- Propiedades nuevas leídas de `GET /api/state`: `visores_escritorio: int` (`desktopViewers`, solo visibles), `visores_escritorio_total: int` (`desktopViewersTotal`) y `viewer_seq: int` (`viewerSeq`).
- `aplicar_tema(nombre: str = "amadeus", *, rim: bool = True, scanlines: float = 0.07) -> None` encola `{"type":"theme","name":…, "rim":…, "scanlines":…}`. Con `settings.vtuber_ai_tema == "amadeus"`, el puente llama a `aplicar_tema()` cada vez que `viewer_seq` cambia entre dos consultas (nuevo visor o recarga); el visor es idempotente. Con `"none"`, nunca.
- `desktop(accion: str, **campos) -> bool`: `True` si encoló `{"type":"desktop","action":accion, **campos}`; `False` si el puente no está en línea, `visores_escritorio_total == 0` o la acción no es válida.
- `409` con `code == "sin_overlay"` → fallo de ese comando **sin** cambiar de estado; `409` con `code == "sin_visores"` (o sin `code`) → `EN_ESPERA` como en la v0.1. Todas las peticiones siguen llevando `X-Source: rams` y `Content-Type: application/json` (ya lo hacen).

**`lobes/corteza_motora.py`:**
- `TIPOS_PERMITIDOS` añade `"desktop"` con saneador propio: `action ∈ {walk_to, peek, chase_cursor, seek_user, wander, stop}`, `x ∈ [0,1]` (o `unit:"px"` con `0 ≤ x ≤ 10000`), `edge ∈ {left,right}`, `seconds ≤ 15`, `speed ∈ [0,3; 3]`. `CANALES_ATENCION` añade `"escritorio"` con atención propia de escritorio: girarse de frente y mirar al ratón, **sin acercarse** (sin `move` x/z).
- **Visibilidad:** `_escritorio_visible` = último `escritorio.evento` `mostrada`(True)/`ocultada`(False), y False con `escritorio.conectado.overlay == False`. Las conductas de escritorio (`desktop(...)`) **solo** se emiten con `_escritorio_visible` y `avatar.visores_escritorio_total > 0`; si no, usa las conductas de escenario de la v0.1 (para el panel).
- **`motor.solicitud`:** `buscar_usuario` → `desktop seek_user`; `asomarse` → `desktop peek{edge}`; `pasear` → `desktop wander{seconds ≤ 15}`; `esconderse` → publica `escritorio.orden{esconderse}`. Se guarda la última solicitud hasta `ttl_s` esperando visibilidad y silencio; al caducar se descarta con `log.debug`.
- **Esconderse sola:** con `pc.no_molestar.activo` (una vez por activación) o fatiga > 0,8 sostenida 10 min → `escritorio.orden{esconderse}`; mientras `pc.no_molestar.activo`, ninguna conducta de escritorio ni travesura. La corteza **nunca** publica `aparecer`.
- **Mirada:** con `vision.mirada` reciente y confianza > 0,6, `{"type":"look","at":"point",x,y,z,"head":0.3}` como mucho **1 por segundo** y solo si el punto se movió > 0,15 m (o la conducta lo pide), contado en `LIMITE_COMANDOS_MIN`. `vision.rostro` frontal → mirar al usuario. `escritorio.evento{toque|soltada}` → sobresalto/sonrojo breve.
- **Travesuras:** publica `motor.travesura` solo con `motor.travesuras.activas`, visible, sin habla, sin `pc.no_molestar`, aburrimiento (`boredom`) > 0,6, ≤ 1 cada 15 min y ≤ 6/día. Nunca llama a ControlPC.
- Cede el cuerpo al habla igual que hoy. Habilitación: `getattr(settings, "tiene_escritorio", False)`.
- Notas de la implementación (rev. de integración): la regla de fatiga (> 0,8 sostenida 10 min) cuenta solo el tiempo **visible** y actúa una vez por episodio (si la llaman aún cansada, se queda hasta que la fatiga baje de 0,8); la mirada tiene su propio sub-tope de **12 `look`/min** dentro de `LIMITE_COMANDOS_MIN`; un `409 sin_overlay` no toca los contadores y activa un sondeo rápido (2 s) de `/api/state` hasta volver a ver el overlay o pasar 20 s.

### C2.11 Compuerta de permisos (grupo `orquestacion`, `core/permisos.py`)

Constructor: `CompuertaPermisos(..., pids_extra: Callable[[], frozenset[int]] | None = None, travesuras_activas: Callable[[], bool] | None = None)` (por defecto: `frozenset()` y `False`).

La fila de iniciativa cambia a: **`origen == "iniciativa"` con `tipo ∉ {vistazo, travesura_cursor}` → denegada**. Filas nuevas (en orden, tras las de origen):

| `tipo` | Requisitos de `args` | Veredicto |
|---|---|---|
| `abrir_destino` | `alias` cumple `SAFE_ALIAS_RE` y `∈ DESTINOS_BASE` (clave); `origen == usuario` | **permitida** |
| `buscar_en` | `DESTINOS_BASE[alias]["busqueda"]`; `consulta` 1..120 y `redactar(consulta, secretos, alta_entropia=True) == consulta`; `origen == usuario` | **permitida** |
| `prerrellenar` | `DESTINOS_BASE[alias]["prerrelleno"]`; `texto` 1..500 sin `\r\n\t` tras sanear; `not parece_secreto(texto, secretos)`; `origen == usuario` | **confirmar** — `descripcion`: "Abrir WhatsApp con un borrador de 42 caracteres (no se enviará)"; `vista_previa` = texto |
| `escribir_texto` | `args = {texto, hwnd, pid, exe, direccion, clase}`; `pid` entero `∉ pids_propios ∪ pids_extra()`; `hwnd` entero **o** `direccion` `^0x[0-9a-f]+$`; `texto` 1..500; `not parece_secreto(texto, secretos)`; `origen == usuario` (el título lo revalida ControlPC con `titulo_sensible`) | **confirmar** — "Escribir 42 caracteres en chrome.exe (sin enviar)"; `vista_previa` = texto |
| `travesura_cursor` | `settings.travesuras_habilitadas and travesuras_activas()`; `patron ∈ PATRONES_TRAVESURA`; `amplitud_px ≤ travesura_amplitud_max_px`; `segundos ≤ 0.6`; `origen ∈ {usuario, iniciativa}` | **permitida** (guardas de estado: ControlPC, §C2.12) |

`permiso.solicitado` = `{id, descripcion, destructiva, tipo, vista_previa?}`. La consola local PUEDE mostrar la vista previa (pantalla, no log); ningún `log.*` la incluye.

```python
def resolver_por_id(self, id: str, aprobado: bool) -> bool
```
Síncrono y thread-safe: `True` solo si hay pendiente, `hmac.compare_digest(id, pendiente().id)` y `pendiente().accion.tipo != "cerrar_ventana"`; nunca toca `_lock_turno` de main. `resolver(texto)` (terminal) sigue igual. Nunca hay más de una confirmación pendiente.

### C2.12 Política de seguridad (normativa)

**Orígenes y canales.**
- Se mantiene la denegación de `origen ∈ {llm, telegram}`. El contenido que redacta el LLM es **dato**: nunca se convierte en `AccionPC` por sí mismo; la acción nace de un mensaje del usuario (`publicar_borrador` referencia el último borrador, §C2.14.8).
- `escritorio` es canal local: acciones con `origen="usuario"`; confirma **solo con botón** (y la terminal sigue pudiendo responder). El texto del chat **nunca** resuelve una confirmación (paso 0 de `procesar_entrada` limitado a `canal == "terminal"`).
- Telegram **no** gana capacidades (v0.2.1: salvo lo que enumera §C2.16, cada cosa con su interruptor).

**Prerrellenar antes que teclear; nunca enviar.** No existe `enviar`/`pulsar_enter`. El objetivo de `escribir_texto` (`hwnd+pid+exe` / `address+pid+clase`) sale de `ControlPC.ventana_previa()` al crear la acción; no se fuerza el foco; se revalida antes de cada trozo.

**Orígenes de red.** VTuber AI Studio: todo POST exige `Content-Type: application/json` y Origin ausente o local; CORS solo al Origin local; `desktop`/`theme` exigen `X-Source: rams` (o `mcp` con `MCP_DESKTOP=1`). Ninguna acción sensible pasa por `:3939`. `/escritorio`: token de archivo privado; sin Origin (Electron) u Origin local (HUD); nunca `0.0.0.0`.

**Travesuras (todas obligatorias).**
- Interruptor maestro `RAMS_TRAVESURAS` (**OFF por defecto**) **y** estado de main `travesuras_activas()` (§C2.14.4).
- Nunca clics, rueda, arrastre ni teclas; solo `SetCursorPos` (Win). **Hyprland: deshabilitadas con el cursor real.**
- **Antes de cada paso** (Win): abortar si `GetAsyncKeyState(vk) & 0x8000` para `0x01, 0x02, 0x04, 0x05, 0x06` (botones) o modificadores (`0x10, 0x11, 0x12, 0x5B, 0x5C`).
- Vetos: `LockApp/LogonUI/consent/CredentialUIBroker` en foco; `QUNS != 5` (incluye 2, 3, 4); ventana que cubre el monitor (excluida la propia); proceso en primer plano ∈ `vision_apps_pausa`; `pantalla.estado.video` o `reproduciendo`; `pc.no_molestar.activo`; usuario escribiendo (inactividad < 4 s); usuario no presente (inactividad > 10 min o presencia ausente); confirmación pendiente; voz activa; `escribir_texto` en curso.
- Aborto si el usuario mueve el ratón > 6 px → main las apaga 2 h. Topes: ≤ `travesura_amplitud_max_px`, ≤ 600 ms, ≤ 1 cada 15 min, ≤ 6/día, nunca dos seguidas sin entrada del usuario entre medias.
- **"basta"** (chat/terminal: "quieta", "deja el mouse", "para ya", "basta"; `/travesuras off`; `escritorio.control{basta_travesuras}`; bandeja "Quieta"; `Ctrl+Alt+Shift+Q`) → un único método de main (§C2.14.4).

**Anti-contraseñas (deterministas, rechazar si se cumple alguno).**
- Contexto: `exe ∈ {LockApp,LogonUI,consent,CredentialUIBroker}` o control con `ES_PASSWORD`; en Hyprland polkit/pinentry/gcr-prompter/kwalletd; `config.titulo_sensible(titulo)`.
- Texto: `config.parece_secreto(texto, secretos)` (`redactar(texto, secretos, alta_entropia=True) != texto`; `^\s*\d{4,8}\s*$`; 13–19 dígitos que pasen Luhn; `(contraseña|clave|password|pin|código)\s*[:=]\s*\S+`; palabra ≥ 12 caracteres con ≥ 3 clases).
- Origen: nunca de títulos, de la web ni de Nexus; solo de la línea del usuario o de un borrador que ya vio.

**Tasas.** `prerrellenar` ≤ 1/20 s, ≤ 30/día; `escribir_texto` ≤ 1/30 s, ≤ 20/día, 1 pendiente; `abrir_destino`/`buscar_en` ≤ 6/min. Longitudes: prerrellenar/escribir ≤ 500; consulta ≤ 120.

**Auditoría.** `data/acciones_pc.jsonl` (append-only, 0o600, escrito con `asyncio.to_thread`) con `ts, tipo, origen, canal, veredicto, motivo, alias|exe|clase, texto_len, texto_sha256[:12], resultado`; además `pc.accion`. **No** se guardan el texto ni los títulos completos.

### C2.13 Privacidad de la visión (normativa)

- **Indicador visible permanente** mientras la cámara está abierta (cualquier fuente): LED del HUD y línea de consola `● CÁMARA ACTIVA` = **OR** de `vision.camara` por `fuente`; `vision.estado` (indicador 👁 del HUD); ícono de ojo en el overlay. El LED físico se enciende de verdad.
- **Pausa:** `/vision pausa [min]`, botón "Cámara" del HUD y `/camara off` (también llaman a `vision.set_camara(False)`), "deja de mirarme"/"no me mires"/"cierra los ojos", `Ctrl+Alt+V` y la bandeja. Pausar = el hilo hace `release()`. Autopausa: sesión bloqueada, ausente con confianza ≥ 0,8, apps de llamada, batería < 20 % sin cargador, juego a pantalla completa.
- **Nunca guardar ni enviar cuadros.** En memoria, como mucho miniaturas 80×60 (cámara) y 64×36 (pantalla). A Broca/Gemini solo `ContextoBroca.percepcion` (escalares, app y título no privado ≤ 60).
- **Ventanas propias:** Windows: `setContentProtection(true)` del overlay (defecto) + máscara de la caja; Hyprland: máscara de la caja (`escritorio.caja`).
- **Terceros:** con `n ≥ 2` solo se publica el número de caras. Títulos sensibles (`pantalla_excluir_re`): sin píxeles, `privada:true`, `titulo:""`.
- **Opt-in de dos niveles:** `RAMS_CAMERA=1` **y** `RAMS_VISION_CONTINUA=1`; `RAMS_PANTALLA_MODO=off` por defecto. No se activa desde Telegram. Funciona sin red (no depende de `offline`).

### C2.14 Orquestación en main (grupo `orquestacion`, `main.py`) — reglas normativas

1. **Arranque:** lo primero, `control_pc.activar_dpi_por_monitor()` (si el módulo importa). Construye `ControlPC`, `VisionContinua` (si `tiene_vision_continua`) y `ServidorHUD` (si `hud_habilitado or tiene_escritorio`), cada uno en `try/except` (si falla, `None`) e `iniciar()` dentro de `wait_for(…, 4)`. Registra los diagnósticos `control_pc`, `vision_camara`, `vision_pantalla`, `escritorio`. `aura.set_fuente_continua(vision.como_vistazo)` si ambos existen. Apagado: `vision.cerrar()`, `control_pc.cerrar()`, HUD `cerrar()` (borra el token).
2. **Chat (`escritorio.chat`, manejador síncrono):** texto vacío o apagando → nada. Si `permisos.hay_pendiente()`: guarda el texto en `_chat_en_espera` (deque, `maxlen=3`) y publica `escritorio.sistema{"Tienes una confirmación pendiente: usa los botones. Leo tu mensaje después."}`. Si no: `self._crear_tarea(self._turno_serializado(texto, "escritorio"), "turno_escritorio")`. Suscripción nueva a `permiso.resuelto`: vacía `_chat_en_espera` creando los turnos en orden.
3. **Turno con `canal == "escritorio"`:** paso 0 (resolver confirmación con texto) **solo** si `canal == "terminal"`; `texto_seguro = redactar(texto, secretos, alta_entropia=True)` para bus, memoria y Broca (la intención se calcula sobre el texto crudo, como en la v0.1); los argumentos de la intención que citan al usuario (`consulta`, `contenido`, `mensaje`; también `/buscar`, `/recordar` y `/logro`) pasan por la **misma** redacción antes de llegar a la memoria, la agenda, el bus, la red o Broca; la confirmación de una acción pedida por el chat no se dice en voz alta (`permiso.solicitado` sin voz salvo `escritorio_voz`); `presencia.registrar_entrada("escritorio")`; publica **una** `usuario.entrada{texto_seguro, canal:"escritorio"}`; `_modo_salida_respuesta("escritorio") = "terminal"` salvo `escritorio_voz` (regla de voz v0.1). `_salida_comando(texto, "escritorio")` imprime en consola **y** publica `escritorio.sistema{texto[:4000]}`; `/hud` por el chat responde "La URL del HUD solo se muestra en la terminal." El error de turno en `escritorio` va a `terminal`.
4. **Travesuras (`EstadoTravesuras`, dueño main):** `{activas: bool, apagadas_hasta_ts: float|None, basta_hoy: int, fecha: "YYYY-MM-DD"}` persistido en `data/travesuras.json` (0o600, escrito con `asyncio.to_thread`). Efectivo = `settings.travesuras_habilitadas and activas and (apagadas_hasta_ts is None or ahora ≥ apagadas_hasta_ts) and not no_molestar_usuario`. Único método `_set_travesuras(evento: "basta"|"off"|"on"|"aborto")`: `basta` → 1.º del día: apagadas 2 h; 2.º del día: hasta las 00:00 locales del día siguiente; `off` → `activas=False` (hasta `on`); `on` → `activas=True` y borra `apagadas_hasta_ts` (si el maestro está OFF, responde "desactivadas en la configuración: RAMS_TRAVESURAS=1"); `aborto` (usuario > 6 px) → apagadas 2 h. Publica `motor.travesuras{activas, hasta_ts}` al cambiar, al arrancar y cuando vence `apagadas_hasta_ts`. `CompuertaPermisos(travesuras_activas=<efectivo>)`.
   `motor.travesura` → si efectivo y `pc.no_molestar` no está activo: `ok, motivo = await control_pc.puede_travesura(presente=…, voz_activa=riko.hablando, confirmacion_pendiente=permisos.hay_pendiente(), video_activo=…)`; si `ok`: `AccionPC("travesura_cursor", {patron, amplitud_px, segundos}, "travesura de cursor", origen="iniciativa")` → `_accion_pc`.
5. **`_accion_pc`:** `tipo ∈ TIPOS_ACCION_V02` → `await permisos.autorizar(accion)` **una vez** y luego el método de ControlPC; **nunca** `aura.ejecutar`. Los tipos de la v0.1 siguen como hoy. `escribir_texto`: si `args` no trae objetivo, main lo rellena con `control_pc.ventana_previa().a_args()` (sin ventana previa → "No sé en qué ventana escribir: haz clic en la caja de texto de la app y vuelve a pedírmelo."); tras aprobar, avisa por consola/`escritorio.sistema` ("Haz clic en la caja de texto de <exe>; escribo en cuanto tenga el foco") y llama a `escribir_texto(texto, objetivo=ObjetivoVentana.desde_args(args))`. Audita y publica `pc.accion`. `escritorio.confirmacion` (síncrono) → `permisos.resolver_por_id(id, aprobado)`; si devuelve `False`: `escritorio.sistema` ("Cerrar ventanas solo se confirma en la terminal." si la pendiente es `cerrar_ventana`; si no, "Esa confirmación ya no está pendiente."). `escritorio.app{pid}` → `control_pc.excluir_pids([pid])`; `pids_extra = control_pc.pids_excluidos`. `escritorio.caja` → `control_pc.set_caja_avatar` y `vision.set_caja_avatar`.
6. **Iniciativas** (regla de C1.3): `overlay_disponible = _overlay_conectado and avatar.visores_escritorio_total > 0`; `_overlay_visible` sigue `escritorio.evento mostrada/ocultada` (False al desconectar). Presente + disponible + sin `pc.no_molestar`: si no visible, `escritorio.orden{aparecer, borde, modo:"asomarse"}`; luego `motor.solicitud{buscar_usuario, motivo, ttl_s:20}` y la frase por el modo proactivo de la v0.1. Presente sin overlay: v0.1. Ausente ≥ 0,8: Telegram (v0.1). Igual para el recordatorio `buscar=True` (§C1.6).
7. **`curiosidad_web`:** en `_al_iniciativa`, una `curiosidad_visual` con el usuario presente (no `ausente_seguro`) y sin cámara utilizable (aura `None`, cámara deshabilitada o sin rostro reciente) pasa a `curiosidad_web` si `settings.tiene_web`, `≤ 3` en el día y `≥ 2 h` desde la última; si no, `conversar` (v0.1). Tema: `persona/temas_curiosidad.json` (lista curada) ∪ recuerdos `categoria == "preferencia"`; **nunca** `perfil.temas`. Consulta ≤ 60 caracteres y `redactar(alta_entropia=True)`. Primer resultado con URL `^https://` ≤ 300. Frase: Broca con `iniciativa="curiosidad_web"` (local: `iniciativa_curiosidad_web`). Con overlay: `escritorio.orden{notificar, texto, url}` + `motor.solicitud{asomarse}`.
8. **Borrador:** `_ultimo_borrador = (reply.texto_plano[:500], ts)` solo de respuestas de Broca a un turno **local** del usuario (`terminal`/`escritorio`), nunca de Nexus, iniciativas, Telegram ni la web (intención `ninguna`/`memoria_consulta`, sin `busqueda_web`, vistazo ni captura; un turno local con alguno de ellos y respuesta del LLM **olvida** el borrador anterior); caduca a los 10 min. `publicar_borrador{alias}` → `AccionPC("prerrellenar", {alias, texto})` si el alias admite prerrelleno, o `AccionPC("escribir_texto", {texto})` (objetivo de la ventana previa) si no; sin borrador → frase `sin_borrador` (con Gemini disponible) o `sin_borrador_sin_llm` (sin Gemini: ofrece «pon en X: …»).
9. **No molestar:** `escritorio.control{no_molestar_on}` → `riko.pausar_proactividad(1440)`, `_no_molestar_usuario=True`, `escritorio.orden{esconderse}`; `off` → `pausar_proactividad(0)`. Tarea de 5 s: `control_pc.no_molestar()` OR `pantalla.estado.no_molestar` OR usuario → `pc.no_molestar{activo, motivo}` al cambiar. `pausar_vision` → `vision.pausar(None, "usuario")`.
10. **Visión:** tabla de traducción y presencia de §C2.3. `percepcion` de `ContextoBroca` en turnos locales e iniciativas con la última muestra (`pantalla.estado` ≤ 10 s, `vision.estado`). `hud.control{camara}` y `/camara off` → además `vision.set_camara(False)`. Consola `● CÁMARA ACTIVA` = OR de fuentes. `mirar_pantalla` en Windows (sin captura) → `ResultadoAccion(False, "Mirar la pantalla no está disponible en este sistema.")` **sin** `error_propio`.
11. **Comandos nuevos:** `/vision on|off|pausa [min]|estado`, `/pantalla off|metadatos|pixeles`, `/travesuras on|off`. Intenciones `escritorio_venir` → `escritorio.orden{aparecer, borde, modo:"entrar"}`; `escritorio_vete` → `escritorio.orden{esconderse}`; `basta_travesuras` → `_set_travesuras("basta")` + esconderse; `vision_pausa` → `vision.pausar(min*60 o None)`.

### C2.15 Respaldo Puter del Área de Broca (tanda posterior a la rev. 2)

**Qué es.** [Puter.js](https://developer.puter.com/tutorials/free-unlimited-ai-api/) es una biblioteca de **navegador** (`<script src="https://js.puter.com/v2/">`) que da acceso a cientos de modelos con `puter.ai.chat(mensajes, {model, max_tokens, temperature…})` **sin clave de API**, con el modelo *user-pays*: el usuario inicia sesión con **su** cuenta de Puter y el uso se descuenta de **su** saldo. Como necesita un navegador, se ejecuta **dentro de Amadeus Desktop** (Chromium) y R.A.M.S. se lo pide por `/escritorio`. Es el **proveedor de respaldo** del Área de Broca: cadena `Gemini → Puter → banco local` (con `RAMS_PUTER_PRIMERO=1`, `Puter → Gemini → banco local`). El principio No-LLM (§C0.3) no cambia: Puter, como Gemini, **solo verbaliza**.

**1. Settings (grupo `base`, §C2.1).**

| Variable | Campo | Defecto | Notas |
|---|---|---|---|
| `RAMS_PUTER` | `puter_habilitado: bool` | `True` | `0` = nunca usar Puter |
| `PUTER_MODEL` | `puter_modelo: str` | `""` | nombre exacto de Puter (`proveedor/modelo`); empieza por letra o cifra, ≤ 100 caracteres; `""` = el modelo por defecto de Amadeus Desktop |
| `RAMS_PUTER_PRIMERO` | `puter_primero: bool` | `False` | `True` = Puter antes que Gemini |
| `PUTER_MAX_CALLS_DAY` | `puter_max_llamadas_dia: int` | `300` | tope diario propio de pedidos a Puter (los paga el usuario); `0` = nunca usar Puter |

`Settings.tiene_puter = puter_habilitado and puter_max_llamadas_dia > 0 and escritorio_habilitado and not offline` (a diferencia del resto de la v0.2, **sí** depende de `offline`: Puter es un servicio en línea). Que la app esté conectada y con sesión lo dice el proveedor en tiempo de ejecución.

**2. Mensajes nuevos de `/escritorio`** (sobre `{tipo, datos}`, mismas reglas de §C2.4; **solo** el cliente sin Origin, y solo el activo: lo que llegue del chat del HUD o de una conexión reemplazada se ignora):

| Dirección | Mensaje | Cuándo |
|---|---|---|
| App → R.A.M.S. | `{"tipo":"puter.estado","datos":{"listo": bool,"sesion": bool,"modelo": str\|null,"error": str\|null}}` | justo después de recibir el `hola` y en cada cambio. `listo` = puter.js cargado **y** sesión iniciada (R.A.M.S. aplica `listo and sesion`). `error`: código corto `[a-z_]{1,24}` (`deshabilitado`, `carga_fallida`, `modelo_desconocido`, `ventana_caida`) o `null` |
| R.A.M.S. → App | `{"tipo":"llm.pedir","datos":{"id": str,"modelo": str,"system": str,"mensajes":[{"rol":"usuario"\|"modelo","texto": str}],"max_tokens": int,"temperatura": float}}` | un turno de Broca. `id` hex en minúsculas de 16-32 (R.A.M.S. usa 24); `modelo: ""` = el defecto del escritorio. **Nunca** sale hacia el HUD (lleva el system prompt) |
| App → R.A.M.S. | `{"tipo":"llm.respuesta","datos":{"id": str,"ok": bool,"texto": str\|null,"error": str\|null,"modelo": str\|null,"ms": int}}` | **siempre** una por pedido con `id` válido, en ≤ 45 s. `ok` exige texto no vacío; `error ∈ {"sin_sesion","timeout","cuota","red","rechazado","otro"}` (otro valor → `"otro"`) |

Límites (los dos lados los validan): `system` ≤ 60 000 caracteres, ≤ 40 mensajes, `texto` ≤ 20 000 cada uno, contados como `String.length` de JavaScript (unidades UTF-16: un emoji cuenta 2; R.A.M.S. recorta así para que el escritorio nunca rechace un pedido por longitud); `max_tokens` 1..8192; `temperatura` 0..2. R.A.M.S. deja el JSON de `llm.pedir` en ≤ 768 KiB quitando los mensajes más antiguos (el último siempre queda y la conversación sigue empezando por el usuario) y el escritorio acepta `llm.pedir` de hasta 1 MiB (el resto de entrantes, ≤ 64 KiB). `llm.respuesta` puede llegar hasta 64 KiB (`max_size` del servidor; el escritorio la deja en ≤ 60 KiB y el texto en ≤ 32 000 caracteres); el resto de mensajes de la app sigue en ≤ 8 KiB. Como mucho **2** pedidos en curso en el escritorio (el escritorio rechaza el 3.º con `rechazado`). El semáforo de R.A.M.S. cuenta lo que el escritorio tiene **en curso**, no lo que R.A.M.S. sigue esperando: un pedido enviado conserva su turno hasta que llega **su** `llm.respuesta` (aunque llegue cuando R.A.M.S. ya dejó de esperarla), hasta que la app se desconecta o la reemplaza otra, o como tope 46 s después del envío (45 s garantizados + 1). R.A.M.S. espera **≤ 40 s** contando la espera de turno, y si tras esperar turno le quedan menos de 5 s (o de la mitad de su plazo, si es corto) no envía el pedido (`completar` → `None`): Puter no llegaría a tiempo y se cobraría igual. El escritorio responde **siempre en ≤ 45 s** (aborta `puter.ai.chat` a los 38 s y el relevo contesta `timeout` a los 42 s). El contrato no tiene cancelación: un pedido que R.A.M.S. abandona por su plazo se sigue procesando (y cobrando) en el escritorio hasta esos 38-42 s.

Ritmo (§C2.4.5): `puter.estado` va en la clase "estado" (≤ 20/s) y, si no cabe, **no se pierde**: se aplica el último al vencer la ventana de 1 s (la app solo lo reenvía cuando cambia). `llm.respuesta` tiene su clase (≤ 10/s), pero la que responde a un pedido **en curso** nunca se descarta por ritmo (perderla dejaría a Broca esperando 40 s o un turno ocupado hasta 46 s), tampoco si llega tarde; las ajenas, repetidas o inventadas sí gastan cupo y se ignoran.

Semántica: una respuesta tardía (tras el plazo de R.A.M.S.) a un pedido propio libera su turno y aplica el efecto de su error (`cuota`, `sin_sesion`), sin texto para nadie ni cambio en los contadores; una con un `id` desconocido o ya respondido se ignora. `sin_sesion` → R.A.M.S. marca "sin sesión" **al recibirla** y el escritorio reenvía justo detrás su `puter.estado` real (resincronización: gana ese estado). `cuota` (saldo de Puter agotado o límite) → R.A.M.S. no le pide nada durante 10 min (diagnóstico `DEGRADADO`, «saldo de Puter agotado · pausa de 10 min»: un detalle que no caduca, porque el HUD y la tabla de `/estado` muestran el último publicado; la cuenta atrás va en la línea de `/estado`) y vuelve solo. `timeout`, `red`, `rechazado`, `otro` → ese turno sin texto (Broca sigue con la cadena). Si la app se desconecta o la reemplaza otra instancia, los pedidos en vuelo devuelven `None` **al momento**, sus turnos quedan libres y el estado vuelve a "sin app"/"sin sesión" hasta el siguiente `puter.estado`. Sin sesión, el escritorio responde `{ok:false, error:"sin_sesion"}` al instante **sin abrir nada**.

**3. Python.** `lobes/broca_llm.py` (grupo `orquestacion`) define el protocolo y la cadena; `interface/escritorio_link.py` (grupo `escritorio-ws`) lo implementa sobre `/escritorio`:
```python
@runtime_checkable
class ProveedorRespaldo(Protocol):          # lobes/broca_llm.py
    nombre: str                              # "puter": es la `fuente` de sus BrocaReply
    @property
    def disponible(self) -> bool: ...        # barato, sin red, nunca lanza
    async def completar(self, system: str, mensajes: list[dict[str, str]], *, modelo: str,
                        max_tokens: int, temperatura: float, timeout_s: float) -> str | None: ...
BrocaLLM.set_respaldo(proveedor: ProveedorRespaldo | None) -> None   # nunca lanza
BrocaLLM.respaldo_disponible -> bool         # tiene_puter + bajo el tope diario + sin pausa + proveedor.disponible
BrocaLLM.llamadas_hoy() -> {"gemini": int, "respaldo": int, "respaldo_ok": int}

class RespaldoPuter:                         # interface/escritorio_link.py
    nombre = "puter"
    def __init__(self, enlace: EnlaceEscritorio, *, modelo: str = "", reloj=time.monotonic): ...  # se vincula al enlace
    disponible: bool          # /escritorio activo, app conectada, último puter.estado con listo y sin enfriamiento por "cuota"
    modelo: str               # PUTER_MODEL válido o ""
    modelo_efectivo: str | None   # el configurado o el que anuncia la app
    ultimo_modelo: str | None     # el de la última respuesta (BrocaReply.modelo)
    async def completar(...) -> str | None   # min(timeout_s, 40) s con la espera de turno; None ante cualquier fallo; nunca lanza por la red
    def estado(self) -> dict      # {conectado, listo, sesion, modelo, error, enfriamiento_s, pendientes, en_curso}
    def estadisticas(self) -> dict  # {pedidos, respuestas, fallos, ultimo_error} de la sesión
    def diagnostico(self) -> DiagnosticoModulo   # clave "puter"
    def desvincular(self) -> None
EnlaceEscritorio.puter -> RespaldoPuter | None;  EnlaceEscritorio.vincular_puter(respaldo | None)
```
Broca le pasa **lo mismo que a Gemini** (system prompt de Amadeus, historial ya filtrado por modo público y el turno con `<contexto>`); la imagen del turno **no** viaja (si la había, se le avisa de que no puede verla) y su texto pasa por el mismo `postprocesar`. Un 429 de Gemini con Puter listo no se reintenta: Gemini queda en pausa y los turnos van a Puter. Las llamadas a Puter se cuentan **aparte** (no gastan `GEMINI_MAX_CALLS_DAY`) y tienen su tope (`PUTER_MAX_CALLS_DAY`); dos fallos seguidos lo enfrían. `resumir` y `escribir_bitacora` siguen siendo solo Gemini. El detalle normativo de la cadena está en el docstring de `lobes/broca_llm.py`.

**4. main (`orquestacion`).** Paso 3b del arranque, justo después de construir Broca (el HUD ya existe): si `puter_habilitado`, tope > 0, sin `--offline` y con `hud.escritorio`, crea `RespaldoPuter(hud.escritorio, modelo=puter_modelo)` y llama a `broca.set_respaldo(respaldo)`; si Broca no tiene `set_respaldo` → `OFF` «Broca sin respaldo»; si lanza → `DEGRADADO` y el respaldo se desvincula. Diagnóstico clave `"puter"`, nombre «Respaldo Puter (Amadeus Desktop)», con su línea en el arranque (después de `escritorio`) y en `/estado`:

| Estado | Detalle |
|---|---|
| `OFF` | `deshabilitado (RAMS_PUTER=0)` · `tope diario 0 (PUTER_MAX_CALLS_DAY=0)` · `modo offline` · `sin /escritorio (RAMS_ESCRITORIO=0)` · `Broca sin respaldo` |
| `EN ESPERA` | `sin app de escritorio` · `Puter desactivado en Amadeus Desktop (AMADEUS_PUTER=0)` (`error: deshabilitado`) · `sin conexión con puter.com` (`carga_fallida`) · `ventana de Puter caída (se recarga sola)` (`ventana_caida`) · `sin sesión de Puter` · `Puter sin cargar (<error>)` (sesión sin `listo`: no lo produce el escritorio actual) |
| `DEGRADADO` | `saldo de Puter agotado · pausa de 10 min` · `Broca rechazó el respaldo` |
| `OK` | el modelo (`modelo_efectivo`; sin él, «modelo por defecto de Amadeus Desktop»); con `error: modelo_desconocido` y sin `PUTER_MODEL`, «… · no aparece en el listado de Puter» (aviso: puede ser un alias que `listModels` no enumera) |

Con `listo: false`, el código de `error` manda sobre `sesion` (con el escritorio real, sin puter.js cargado `sesion` siempre es `false`, y «sin sesión» mandaría al usuario a iniciar una sesión que no arregla nada).

`RespaldoPuter` publica `sistema.modulo` (clave `"puter"`, origen `escritorio`) **solo cuando cambia** su diagnóstico; main lo recoge como el resto. `/estado` añade una línea: `IA de respaldo (Puter): lista|en espera|tope diario alcanzado|en pausa|en pausa (saldo de Puter agotado, reintento en N min) · si Gemini no puede|antes que Gemini · hoy N de TOPE pedidos, M con respuesta` (la última forma de «en pausa» es la del saldo agotado, con la cuenta atrás del momento), y su fila de la tabla de módulos sale de `diagnostico()` en ese momento, no del último `sistema.modulo`.

**5. Amadeus Desktop (grupo `desktop`).** Archivos nuevos: `desktop/lib/puter.js` (relevo WS ↔ IPC, puro: validación de `llm.pedir`, clasificación de fallos, plazos, `puter.estado`), `desktop/lib/puter-sitio.js` (páginas, CSP y dominios permitidos, puro), `desktop/preload-puter.js` (`window.amadeusPuter`, versión 1, solo en el origen propio) y `desktop/puter/` (`trabajo.html/js`: la ventana oculta que llama a `puter.ai.chat`; `conectar.html/js` + `puter.css`: la ventanita de inicio de sesión).
- **Ventana oculta** dedicada (nunca se muestra), partición **`persist:puter`** (la sesión de Puter se conserva entre arranques), `sandbox`, `contextIsolation`, preload mínimo, sin `nodeIntegration`. Sus páginas se sirven en el origen `http://amadeus-puter.localhost` con `protocol.handle` **solo** en esa partición y **sin abrir puertos** (`puter.auth.signIn()` rechaza `file://`); cualquier otra ruta → 404.
- **CSP** por cabecera: `script-src 'self' https://js.puter.com`; `connect-src 'self' https://api.puter.com wss://api.puter.com https://puter.com https://js.puter.com`; sin marcos, workers, objetos ni formularios.
- **Relevo en el proceso principal:** `llm.pedir` → IPC `puter:pedir` → `puter:respuesta` → `llm.respuesta`, y `puter.estado` tras el `hola`. El **token de R.A.M.S. nunca llega** a la ventana de Puter (solo el main abre el WebSocket). Nunca registra prompts ni respuestas; de un fallo solo su clasificación.
- **Bandeja:** «Conectar Puter (IA de respaldo)…» abre una ventanita con un **botón** (el inicio de sesión de Puter abre una ventana emergente que necesita el gesto del usuario); `window.open` solo a `https://puter.com` y subdominios, en una ventana sandbox que no puede salir de puter.com. Con sesión: «Desconectar Puter» (borra los datos de la partición y recarga). También `--amadeus=puter` (CLI, para `hl.bind`).
- **Entorno:** `AMADEUS_PUTER=0` lo apaga (responde `rechazado` y anuncia `error:"deshabilitado"`); `AMADEUS_PUTER_MODELO` cambia el modelo por defecto; `AMADEUS_PUTER_ESFUERZO` (`none|minimal|low|medium|high`, opcional) fija el `reasoning_effort`.
- **Modelo por defecto:** `MODELO_DEFECTO` de `desktop/lib/puter.js`, verificado en el listado oficial de Puter (developer.puter.com/ai/models y docs.puter.com): hoy `google/gemini-3.5-flash-lite`, el más rápido de los candidatos, bueno en español y de la familia Gemini para la que está afinado el prompt de Amadeus. `PUTER_MODEL` en R.A.M.S. (por pedido) gana a `AMADEUS_PUTER_MODELO` (por defecto del escritorio).

**6. Privacidad.** A Puter (y, a través de Puter, al proveedor del modelo) le llega lo mismo que a Gemini: system prompt de Amadeus, turnos recientes, recuerdos y contexto del turno y, solo con `RAMS_PERFIL_EN_PROMPT=1`, el resumen del perfil de WhatsApp; **nunca** imágenes ni fotogramas. `--offline`, `RAMS_PUTER=0` o `PUTER_MAX_CALLS_DAY=0` lo cortan del todo. R.A.M.S. no guarda ninguna credencial de Puter: la sesión vive en la partición `persist:puter` de Amadeus Desktop.

**7. Pruebas** (sin red, sin Puter real ni inicio de sesión, sin ventanas visibles): `tests/test_escritorio_link.py` con un **Amadeus Desktop falso** (cliente `websockets` sin Origin que contesta los `llm.pedir`): estado y diagnóstico, ida y vuelta solo a la app (el HUD nunca ve `llm.pedir`; ni el prompt ni la respuesta en `caplog`), cada código de error, `sin_sesion` + resincronización, `cuota` y fin del enfriamiento, plazo y respuesta tardía, desconexión y reemplazo con pedidos en vuelo, máximo 2 a la vez, ráfagas sobre el cupo (`puter.estado` y `llm.respuesta`), 64 KiB solo para `llm.respuesta` de la app, límites en unidades UTF-16 y recorte por tamaño, coherencia del patrón de modelo con `core/config.py` y `desktop/lib/puter.js`, y que `RespaldoPuter` cumple `ProveedorRespaldo`. `tests/test_main.py`: cableado con el HUD real y un escritorio falso (EN ESPERA → OK → EN ESPERA), `OFF` por configuración, Broca sin soporte o que lo rechaza, y la línea de `/estado`. `tests/test_broca_llm.py`: la cadena con un proveedor falso. `desktop/test/`: relevo y sitio puros con `node --test`; el smoke oculto carga la ventana de Puter con una sesión en memoria y comprueba que un pedido sin sesión falla al instante sin abrir ventanas.

**8. Archivos por frente** (un archivo, un frente): **broca** — `lobes/broca_llm.py`, `core/config.py`, `.env.example`, `tests/test_broca_llm.py`, `tests/test_config_bus.py`; **desktop** — `desktop/main.js`, `desktop/lib/cli.js`, **`desktop/lib/puter.js`**, **`desktop/lib/puter-sitio.js`**, **`desktop/preload-puter.js`**, **`desktop/puter/**`**, `desktop/test/**`, `desktop/README.md`; **enlace** — `interface/escritorio_link.py`, `main.py`, `tests/test_escritorio_link.py`, `tests/test_main.py`, `docs/COMPANION.md`, `README.md`.

### C2.16 Telegram multimedia, apps del menú Inicio y capacidades reales (v0.2.1)

Pedido del usuario: quitar el globo de texto del avatar, que Amadeus deje de decir que «no puede abrir nada ni tiene control sobre el PC», y que por Telegram mande fotos de la cámara o de la pantalla y audios, y que entienda los suyos. **Manda sobre §C2.12 («Telegram no gana capacidades») solo en lo que se lista aquí.**

**1. Settings** (`core/config.py`; documentados en `.env.example`).

| Variable | Campo | Defecto | Notas |
|---|---|---|---|
| `VTUBER_AI_BURBUJA` | `vtuber_ai_burbuja: bool` | `False` | globo de texto del visor en cada `speak` |
| `RAMS_TELEGRAM_FOTOS` | `telegram_fotos: bool` | `False` | fotos de la cámara o capturas pedidas **desde** Telegram (desde el PC no hace falta) |
| `RAMS_TELEGRAM_AUDIO` | `telegram_audio: str` | `"auto"` | `off` · `auto` (nota de voz si llegó una o la pidió) · `siempre` |
| `RAMS_TELEGRAM_TRANSCRIBIR` | `telegram_transcribir: bool` | `True` | notas de voz entrantes → Gemini (solo con Gemini disponible) |
| `RAMS_TELEGRAM_ACCIONES` | `telegram_acciones: bool` | `True` | acciones de bajo riesgo pedidas por Telegram |
| `RAMS_APPS_MENU_INICIO` | `apps_menu_inicio: bool` | `True` | apps del menú Inicio por nombre (solo Windows) |

**2. Globo del visor.** `speak` acepta `"bubble": bool` (defecto `true`, por compatibilidad: el panel y MCP sin el campo siguen con globo); con `false` no muestra el globo ni oculta el de otro `speak`. `lobes/vtuber_ai_bridge.py` envía `bubble = settings.vtuber_ai_burbuja` en cada `speak`.

**3. Enrutador** (`core/intenciones.py`). Quita vocativos («asistente», «amadeus», «kurisu», «christina», «oye») y cortesías («por favor», «porfa», «pls», «plis», «xfa», también estiradas o con erratas) y entiende peticiones («¿puedes abrir X?», «ábreme X», «me abres X», «abre X en el pc»). En Windows, «abre X» que no es del catálogo → `lanzar_app{nombre, local: True, explicito}` (R18 del diseño v0.3; `tentativa` para verbos ambiguos como «lanza una moneda»). Intenciones nuevas: `enviar_foto{fuente: camara|pantalla}` («mándame una foto», «mándame captura», «pantallazo») y `pedir_audio` («mándame un audio», «dímelo con tu voz»; `destino: "telegram"` si lo pide desde el PC). Por Telegram, con `telegram_acciones`, pasan `abrir_destino`, `buscar_en`, `lanzar_app` y `media` con `args["canal"] = "telegram"`; lo demás sigue en `bloqueada_canal`.

**4. Apps del menú Inicio** (`lobes/control_pc.py`). Índice en un hilo (se refresca cada 5 min) de los `.lnk`/`.url` de `Start Menu\Programs` en `%APPDATA%` y `%ProgramData%`, más unas pocas apps de la Store. **Excluidas** (nunca se abren): desinstaladores, instaladores, ayudas y accesos a páginas web. **Sensibles** (confirmación cada vez, nunca se recuerdan): consolas y herramientas del sistema. API: `resolver_app_local(nombre) -> ResolucionApp{estado: unica|varias|ninguna|excluida|desactivada, app, opciones, nivel, motivo}`, `lanzar_app_local(clave)` (`os.startfile` del acceso, que debe seguir dentro del menú Inicio; comparte el cupo de 6/min con `abrir_destino`) y `aprobar_app_local(clave)` (`data/apps_aprobadas.json`, 0o600). La compuerta (`nivel_app`) pide confirmación la primera vez para una app `descubierta`; si el usuario acepta, main la recuerda. Por Telegram nada se confirma: solo se abren las ya aprobadas.

**5. Capturas** (`lobes/capturas.py`, nuevo). `capturar_jpeg(fuente, *, settings, vision=None, ventana_titulo=None, calidad=85, max_lado=1600, bus=None) -> ResultadoCaptura(ok, jpeg, ancho, alto, fuente, motivo)`, con `motivo ∈ {deshabilitada, sin_camara, camara_ocupada, ventana_sensible, sin_backend, error}`. Cámara: `VisionContinua.instantanea_jpeg()` si la tiene abierta; si no, la abre un instante en el executor (descarta los cuadros de exposición y hace `release()` siempre) y publica `vision.camara{activa, fuente: "vistazo"}` en `bus` mientras está abierta (main pasa siempre su bus). Pantalla: el monitor de la ventana activa (Windows: mss; Hyprland: `grim` a stdout); se cancela si **cualquier** ventana visible en ese monitor es sensible o no se puede comprobar, y se vuelve a revisar tras capturar; en X11, otros Wayland y macOS → `sin_backend`. Nunca disco ni log.

**6. Telegram** (`lobes/riko_megumin_io.py`). `enviar_foto_telegram(jpeg, *, leyenda="")` (`sendPhoto` ≤ 10 MB), `enviar_voz_telegram(texto, *, tone="calido")` (`sendVoice` MP3 de ElevenLabs o edge-tts; si el texto no cabe en la leyenda, va completo aparte) y `set_transcriptor(fn)`. Notas de voz o audios entrantes (≤ 2 MB y ≤ 180 s): `getFile` → descarga en memoria → transcriptor → `manejar(texto, voz=True)`; sin transcriptor o si falla, responde por texto que no pudo escucharla. `telegram.enviado.tipo` suma `foto` y `audio`.

**7. Broca** (`lobes/broca_llm.py`). `transcribir_audio(audio, mime) -> str | None` (modelo ligero de la cadena, `inline_data`, cuenta en el tope diario). El system prompt lleva el bloque `CAPACIDADES REALES` (`<capacidades canal="local|telegram|nexus">`), generado por código a partir de Settings y del canal, con la frase exacta de cada acción y la regla de no negar lo listado; si el usuario pidió una acción que no se ejecutó, Broca explica la frase exacta en vez de decir que no puede.

**8. main.py.** `_manejar_telegram(texto, *, voz=False)`; `riko.set_transcriptor(broca.transcribir_audio)` si `telegram_transcribir` y hay Gemini. `enviar_foto` pasa por la compuerta (solo `origen="usuario"`, ≤ 10 por hora; por Telegram exige `telegram_fotos`), respeta la pausa de la cámara pedida por el usuario y avisa en el PC («📸 …»). Toda acción pedida por Telegram se anuncia en el PC («📱 Desde Telegram: …», en el chat y como notificación de Amadeus Desktop). Respuesta en nota de voz si `telegram_audio == "siempre"` o (`"auto"` y (llegó una nota de voz o pidió audio)); si la nota falla, va el texto.

**9. Pruebas.** `tests/test_capturas.py` (dobles de mss, Win32, cámara y grim), `test_intenciones.py`, `test_control_pc.py`, `test_permisos.py`, `test_riko_megumin_io.py`, `test_broca_llm.py`, `test_main.py` y `vtuber-ai/test/speak_bubble.test.js`. E2E de integración: un R.A.M.S. aislado (datos y puertos propios) con Telegram, Gemini y VTuber AI Studio simulados por `httpx.MockTransport`, el chat por el WebSocket real `/escritorio` y cámara, mss y `os.startfile` falsos.

---

## §C3. Grupos de implementación

El grupo **base** va primero. El resto en paralelo contra sus contratos. Un archivo = un grupo.

### C3.1 Matriz de archivos (verificada: ningún archivo en dos grupos)

| Grupo | Archivos (existentes que modifica · **nuevos**) |
|---|---|
| `base` | `core/config.py`, `core/contracts.py`, `.env.example`, `tests/test_config_bus.py`, `tests/conftest.py`, `requirements.txt`, `.gitignore` |
| `desktop` | **`desktop/**`**: `desktop/package.json`, `desktop/main.js`, `desktop/preload-overlay.js`, `desktop/preload-chat.js`, `desktop/chat.html` (+ `desktop/chat.css`, `desktop/chat.js`), `desktop/lib/*.js`, `desktop/test/**`, `desktop/assets/tray.png`, `desktop/scripts/**`, `desktop/hyprland.ejemplo.conf` (y `.lua`), `desktop/README.md` |
| `visor-escritorio` | `vtuber-ai/server.js`, `vtuber-ai/lib/protocol.js`, `vtuber-ai/lib/mcp.js`, `vtuber-ai/public/app.js`, `vtuber-ai/public/index.html`, `vtuber-ai/public/style.css`, `vtuber-ai/README.md`, `vtuber-ai/package.json` · **`vtuber-ai/lib/dispatch.js`**, **`vtuber-ai/public/vendor/**`**, **`vtuber-ai/test/dispatch.test.js`**, **`vtuber-ai/test/server.test.js`** |
| `kurisu` | **`vtuber-ai/public/amadeus_theme.js`**, **`vtuber-ai/public/amadeus_theme.css`**, **`vtuber-ai/models/README.md`**, **`vtuber-ai/test/amadeus_theme.test.js`** |
| `escritorio-ws` | `interface/hud_server.py`, `interface/amadeus_hud.html`, `tests/test_hud_server.py` · **`interface/escritorio_link.py`** (opcional), **`tests/test_escritorio_link.py`** (opcional) |
| `control-pc` | **`lobes/control_pc.py`**, **`tests/test_control_pc.py`** |
| `vision` | **`lobes/vision_continua.py`**, **`tests/test_vision_continua.py`**, `lobes/aura_vision_pc.py`, `tests/test_aura_vision_pc.py` |
| `motor-escritorio` | `lobes/corteza_motora.py`, `lobes/vtuber_ai_bridge.py`, `tests/test_corteza_motora.py`, `tests/test_vtuber_ai_bridge.py` |
| `orquestacion` | `main.py`, `core/intenciones.py`, `core/permisos.py`, `core/presence.py`, `lobes/broca_llm.py`, `persona/frases.json`, `README.md`, `tests/test_main.py`, `tests/test_intenciones.py`, `tests/test_permisos.py`, `tests/test_presence.py`, `tests/test_broca_llm.py` · **`persona/temas_curiosidad.json`** |
| fuera de alcance v0.2 | `core/sincan_connectome.py`, `vtuber-ai/mcp-server.js`, `lobes/riko_megumin_io.py`, `core/bus.py`, `core/memory_engine.py`, `lobes/web_researcher.py`, `core/wsp_ingestor.py`, `core/control_server.py`, `docs/**` |

### C3.2 Matriz de contratos (emisor → consumidor, ambos con dueño)

| Contrato | Produce (grupo) | Consume (grupo) |
|---|---|---|
| `escritorio.{conectado,app,chat,evento,control,confirmacion,caja}` | escritorio-ws | orquestacion (todos); motor-escritorio (`conectado`, `evento`) |
| `escritorio.orden` | orquestacion; motor-escritorio (`esconderse`) | escritorio-ws → desktop (`lib/mapeo.js`) → visor-escritorio (`onOrder`) |
| `escritorio.sistema`, `motor.travesuras` | orquestacion | escritorio-ws (→ desktop chat / HUD); motor-escritorio (`motor.travesuras`) |
| `motor.travesura` | motor-escritorio | orquestacion → control-pc |
| `motor.solicitud`, `pc.no_molestar` | orquestacion | motor-escritorio |
| `vision.{rostro,mirada,estimulo,atencion,estado}`, `pantalla.*` | vision | orquestacion; motor-escritorio (`rostro`, `mirada`); escritorio-ws (`vision.estado`, `pantalla.estado`) |
| `vision.camara` (+`fuente`) | vision (continua y aura) | escritorio-ws (LED); orquestacion (consola) |
| `pc.accion` | orquestacion | escritorio-ws |
| `permiso.solicitado{vista_previa}` / `permiso.resuelto` | orquestacion (`permisos.py`) | escritorio-ws; orquestacion |
| WS `/escritorio` (§C2.4) | escritorio-ws | desktop (`main.js`, `lib/mapeo.js`); escritorio-ws (`amadeus_hud.html`) |
| WS `/escritorio`: `puter.estado`, `llm.respuesta` (§C2.15) | desktop (`lib/puter.js`) | escritorio-ws (`RespaldoPuter`) |
| WS `/escritorio`: `llm.pedir` (§C2.15) | escritorio-ws (`RespaldoPuter`) | desktop (`lib/puter.js` → ventana oculta `desktop/puter/trabajo.js`) |
| `ProveedorRespaldo` / `set_respaldo` (§C2.15) | orquestacion (`broca_llm.py`) | escritorio-ws (`RespaldoPuter`); orquestacion (`main.py`) |
| `sistema.modulo{clave:"puter"}` (§C2.15) | escritorio-ws (`RespaldoPuter`) | orquestacion (arranque y `/estado`); HUD |
| IPC `amadeusDesktop` / `amadeusChat` (§C2.7.2) | desktop | visor-escritorio (`app.js`); desktop (`chat.html`) |
| HTTP `:3939` (`desktop`, `theme`, `/api/state` con `desktopViewers*`, `viewerSeq`, códigos 409) | visor-escritorio | motor-escritorio (puente) |
| `aplicarTemaAmadeus` / `clasificarMaterial` (§C2.6) | kurisu | visor-escritorio (`loadModel`) |
| `ControlPC` / `ObjetivoVentana` / `activar_dpi_por_monitor` (§C2.8) | control-pc | orquestacion |
| `VisionContinua` / `set_fuente_continua` (§C2.9) | vision | orquestacion |
| `Settings` nuevos, `DESTINOS_BASE`, `PATRON_TITULO_SENSIBLE`, `parece_secreto`, `titulo_sensible`, `ContextoBroca.percepcion`, `INICIATIVAS`, `TIPOS_ACCION_V02` | base | todos los grupos Python |
| `tests/conftest.py` (`_entorno_prueba`, `sin_efectos_pc`) | base | todos los tests Python |

### Grupo `base` — contratos compartidos
**Archivos:** C3.1. **Brief:** §C2.1 (campos, propiedades, `asegurar_directorios`, `parece_secreto`, `titulo_sensible`), §C2.2 (constantes, `DESTINOS_BASE`, `PATRON_TITULO_SENSIBLE`, `ContextoBroca.percepcion`, `INICIATIVAS`), `.env.example` (secciones "Amadeus Desktop / escritorio", "Control del PC y travesuras", "Visión continua (cámara + pantalla)"), `tests/conftest.py`: `_entorno_prueba` añade `RAMS_ESCRITORIO_TOKEN_FILE=<tmp_path>/escritorio.json`, `RAMS_TRAVESURAS=0`, `RAMS_VISION_CONTINUA=0`, `RAMS_PANTALLA_MODO=off`; fixture **autouse** `sin_efectos_pc` que sustituye por funciones que hacen `pytest.fail(...)`: `os.startfile`, `webbrowser.open`, y en Windows `ctypes.windll.user32.{SendInput,SetCursorPos,keybd_event,mouse_event,SetForegroundWindow}`; y envuelve `subprocess.Popen` y `asyncio.create_subprocess_exec` para fallar si **algún** argumento tiene como nombre base `xdg-open`, `gtk-launch`, `wtype`, `ydotool`, `hyprctl`, `explorer`/`explorer.exe`, `cmd`/`cmd.exe`, `powershell`/`powershell.exe`, `start` (lo demás pasa; un test que parchea encima tiene prioridad). `requirements.txt`: línea comentada `# mss==10.2.0   # opcional: visión de pantalla en Windows`. `.gitignore`: `desktop/node_modules/`, `desktop/out/`, `desktop/*.log`.
**Tests (`tests/test_config_bus.py`):** defectos nuevos (`escritorio_habilitado True`, `travesuras_habilitadas False`, `vision_continua False`, `pantalla_modo "off"`, `travesura_amplitud_max_px 40`, `vtuber_ai_tema "amadeus"`, `hud_chat True`, `escritorio_voz False`); rangos (`pantalla_fps` fuera de 0,2..2 → defecto); CSV; `RAMS_DESTINOS` suma sinónimos válidos y descarta claves desconocidas; `tiene_escritorio`/`tiene_vision_continua` **no** dependen de `offline`; `pantalla_excluir_re` casa "Bitwarden" y "Nueva pestaña InPrivate"; `parece_secreto` detecta `"482915"`, una tarjeta Luhn, `"AIza…"` y `"password: x"` y deja pasar "hola, ¿vienes mañana?"; `titulo_sensible("Bancolombia - Sucursal Virtual")`; líneas 917/918 actualizadas; `len(TIPOS_ACCION) == 13`; `"curiosidad_web" in INICIATIVAS`; `ContextoBroca(percepcion={"app":"chrome","x":1}).percepcion == {"app":"chrome"}`; `hash(settings)` no lanza; el `.env.example` leído como `entorno` da los defectos sin `log.warning`; la fixture `sin_efectos_pc` falla ante `asyncio.create_subprocess_exec("xdg-open", …)`.

### Grupo `desktop` — Amadeus Desktop (Electron)
**Archivos:** `desktop/**`. **Brief:** todo §C2.7 (procesos, IPC exacto, autoridad del overlay y watchdog, foco del chat, bandeja/atajos/CLI, conexión y token, CSP, smoke, plataformas), §C1.1, §C1.2, §C1.11, §C1.12. `package.json` con `electron` **`44.8.0`** fijado como devDependency y scripts `start`, `start:x11`, `smoke`, `test`. `VTUBER_AI_URL` (defecto `http://127.0.0.1:3939`), `RAMS_ESCRITORIO_TOKEN_FILE` y `AMADEUS_EXCLUIR_CAPTURA` (defecto `1`) por entorno (documentados en `desktop/README.md`).
**Tests:** `node --check` de `main.js`, preloads y `chat.js`; `node --test test/` con: parser de `--amadeus=` (`lib/cli.js`); decisión de interactividad (`lib/hit.js`: dentro/fuera con margen 8 px, rect inválido ignorado, watchdog 1,5 s, crash → click-through); mapeo WS↔IPC (`lib/mapeo.js`: `permiso`→`chat:permiso`, `sistema`→`chat:msg{de:'sistema'}`, `orden{abrir_chat}` → `showInactive` y **nunca** `focus`); validación de `chat:confirm` (id y booleano) y de URLs; `lib/token.js` (sin `XDG_RUNTIME_DIR` → no conecta; modo 0644 → rechaza); `npm run smoke` termina en ≤ 5 s con código 0 y "smoke OK" (tras `npm i`, permitido), sin Tray, sin atajos ni red.

### Grupo `visor-escritorio` — VTuber AI Studio en modo escritorio
**Archivos:** C3.1. **Brief:** §C2.5 completo (dispatch puro, `createServer`, `PORT=0`, seguridad de POST y CORS, códigos 409, `desktopViewers`/`desktopViewersTotal`/`viewerSeq`, `POST /api/state` por viewer, protocolo sin `show/hide/enter/leave`, MCP, modo escritorio de `app.js` con la tabla de órdenes y eventos, visibilidad, TTS, encuadre, `move` sin x/z, rendimiento, tema dentro de `loadModel` con `THREE` inyectado, vendor local). `package.json`: script `"test": "node --test test/"`. README: `?desktop=1`, `desktop`, `theme`, `?tts=1`, seguridad y vendor. **No** rompas el protocolo existente ni el panel.
**Tests:** `node --check` de todos los `.js`; `test/dispatch.test.js` (lote `desktop` solo a viewers desktop; lote mixto → error; visor desktop oculto no cuenta en `desktopCount` pero sí en `desktopTotal`; `fuentePermitida`; `normalizeCommands` acepta `desktop`/`theme`); `test/server.test.js` con `createServer({port:0, host:'127.0.0.1'})` (nunca `:3939`): `text/plain` → 403; Origin ajeno → 403; `desktop` sin `X-Source: rams` → 403; sin viewers → 409 `sin_visores`; `desktop` sin overlay → 409 `sin_overlay`; `GET /api/state` con los campos calculados al final y `POST /api/state` que no puede fijarlos; todo import de `three/addons/` de `app.js` resuelve a un archivo de `public/vendor/`.

### Grupo `kurisu` — tema Amadeus
**Archivos:** C3.1. **Brief:** §C2.6 completo (API exacta, `THREE` inyectado, idempotencia, ImageBitmap cerrado tolerado, sin marco CSS en modo escritorio) y §C1.10; nombres reales del modelo de ejemplo (`*_SKIN/_HAIR/_EYE/EyeIris/Tops/Bottoms/Shoes`, prefijos VRoid `N00_…`, `(Instance)`, `(Outline)`). `models/README.md`: VRoid Hub ShiningFinger / VRoid Studio (VRM 1.0 **sin reducir materiales**), licencias, crédito obligatorio, homenaje de fan sin assets oficiales.
**Tests (`node --test test/amadeus_theme.test.js`):** `node --check`; claves de `AMADEUS_PALETA` y colores `#rrggbb`; `clasificarMaterial('Hair_00_HAIR (Outline)') === 'hair'`, `'EyeIris_00_EYE' → 'iris'`; `aplicarTemaAmadeus` con un `vrm` y un `THREE` falsos mínimos devuelve `{update, dispose}` sin lanzar; segunda llamada devuelve el mismo handle (una sola corbata); con `modoEscritorio:true` no crea `<link>` (sin `document` en Node: no debe tocarlo).

### Grupo `escritorio-ws` — endpoint `/escritorio` y chat en el HUD
**Archivos:** C3.1. **Brief:** §C2.4 completo (arranque condicionado, token atómico y borrado seguro, handshake, límites por tipo de cliente, mensajes y validaciones, traducciones, mensaje `escritorio` en `/ws`, `diagnostico_escritorio`), reglas del bus de §C2.3 (no reenviar `escritorio.chat`/`escritorio.sistema` por `/ws`; quitar `vista_previa` en `/ws`), `EVENTOS_SUSCRITOS` + `traducir_evento` para `vision.estado`, `pantalla.estado`, `pc.accion`, `vision.camara{fuente}`. Panel RINE en `amadeus_hud.html` (lista con `textContent`, input, botones de confirmación por `id`, mensajes de sistema, enlaces solo tras clic), LED de cámara = OR de fuentes e indicador 👁 visible.
**Tests:** los de v0.1 siguen pasando (el `hola` de `/ws` no cambia; el mensaje `escritorio` llega después); `/escritorio`: sin Origin → OK; Origin local → OK; Origin ajeno → 403; token malo → 403; 2.º cliente sin Origin reemplaza al 1.º; 3.er cliente del HUD → 403; `{tipo:"chat"}` publica **solo** `escritorio.chat` (ningún `usuario.entrada`); `hola_app`/`evento`/`caja` desde un cliente con Origin → ignorados; `salida.subtitulo` → `subtitulo`; `permiso.solicitado` con `vista_previa` → `/escritorio` la recibe y `/ws` no; `{tipo:"confirmacion"}` con id inválido → nada; `escritorio.sistema` no sale por `/ws`; `escritorio.conectado` al conectar/desconectar; token distinto del de `/ws`, ausente del `hola` y de `caplog`; archivo escrito al iniciar (0o600 en Linux) y borrado al cerrar solo si sigue siendo propio; con `RAMS_HUD=0` y escritorio activo, `/escritorio` funciona y `/` da 404. Servidor en `127.0.0.1:0`, token en `tmp_path`.

### Grupo `control-pc` — control de apps, teclado y cursor
**Archivos:** C3.1. **Brief:** §C2.8 completo (incluidos `activar_dpi_por_monitor`, `ventana_previa` y exclusiones, caché asíncrona en Hyprland, `no_molestar`, `puede_travesura` con parámetros de contexto, espera de foco sin forzarlo, `GetAsyncKeyState`), §C2.12 (travesuras, anti-contraseñas, tasas), destinos de `DESTINOS_BASE` (cómo abrir cada clave en Windows y Linux; enlaces de prerrelleno: `wa.me/?text=`, `t.me/share/url?url=&text=`, `x.com/intent/post?text=`, Gmail `view=cm&fs=1&body=`, `mailto:?body=`; búsqueda: YouTube, Spotify, X, DuckDuckGo), `ts_ultima_inyeccion`. **Nunca** `shell=True`; nunca Enter.
**Tests (sin tocar el PC real; la fixture `sin_efectos_pc` está activa):** `enlace_prerrelleno("whatsapp", "hola ñ 😀\n(enter)")` urlcodifica y quita saltos; `anti_contrasena` detecta OTP, Luhn, `AIza…`, "password: x", título "Bancolombia" y deja pasar textos normales; `abrir_destino`/`buscar_en`/`prerrellenar`/`escribir_texto`/`empujon_cursor` con subprocesos y ctypes **parcheados** devuelven `ResultadoControl`; `escribir_texto` sanea `\r\n\t`, espera el foco (reloj falso) y aborta sin foco en 10 s; `ventana_previa` excluye pids propios, pids de `excluir_pids` y títulos "Amadeus Chat"/"Amadeus - Google Chrome"; `empujon_cursor` aborta con un `GetAsyncKeyState` falso que marca LBUTTON y respeta `amplitud_px`/`patron`; con plataforma `"hypr"`, `puede_travesura` → `(False, "sin estado de botones en Wayland")`; vetos de `video_activo`, `voz_activa`, proceso en `vision_apps_pausa`, QUNS 3; `ObjetivoVentana.desde_args(o.a_args()) == o`; `activar_dpi_por_monitor()` fuera de Windows → `False`; el código no contiene `shell=True`; `disponible is False` sin ctypes/wtype simulados; vermin limpio.

### Grupo `vision` — visión continua local
**Archivos:** C3.1. **Brief:** §C2.9 completo (hilos con `threading.Event` y `release()` en el hilo, `cerrar` con `to_thread(join)`, `como_vistazo`, `set_caja_avatar`, `vision.camara` con `fuente` cada 5 s, `llegada` con refractario ≥ 120 s, privacidad de títulos con `pantalla_excluir_re`, `pantalla_fps` 0,2..2, `CAP_PROP_FPS` 10–15), §C2.13; cambio mínimo en `aura_vision_pc.py` (`set_fuente_continua`, `fuente:"vistazo"`, rutas `opencv5`). Dependencias opcionales (`cv2`, `numpy`, `mss`) en `try/except`; sin ellas, degradación a metadatos/off. No escribe en la presencia.
**Tests (sin hardware):** `analizar` con detector falso (fila de 15 columnas) y sin cara; `punto_mirada` (signos, límites); `MetricasPantalla` (corte, vídeo por región, tono, máscara de la caja); integrador con reloj falso (llegada tras 1 s; salida tras 20 s; 3 fps de rostro durante 300 s → un solo flanco `llegada`); `pausar()` vuelve en < 5 ms y la captura falsa recibe `release()` desde el hilo en < 1 s; `como_vistazo` sin muestra → `None`, con muestra < 1 s → `ResultadoVistazo` sin abrir nada; `vision.camara{fuente:"continua"}` se repite cada 5 s (reloj falso); título "KeePassXC" → `privada:true, titulo:""`; degradación por telemetría falsa; escaneo del código fuente con la lista prohibida; ningún payload con `bytes`/`ndarray`. `test_aura_vision_pc.py`: con `set_fuente_continua` fijada, `vistazo()` no abre la cámara ni cuenta cupo; `vision.camara` lleva `fuente:"vistazo"`; los tests de v0.1 siguen pasando. vermin limpio.

### Grupo `motor-escritorio` — la corteza decide la vida en el escritorio
**Archivos:** C3.1. **Brief:** §C2.10 completo (puente: `visores_escritorio`, `visores_escritorio_total`, `viewer_seq`, `aplicar_tema` por cambio de `viewerSeq` según `vtuber_ai_tema`, `desktop() -> bool`, códigos 409; corteza: `desktop` en `TIPOS_PERMITIDOS` con saneador, `escritorio` en `CANALES_ATENCION` sin acercarse, visibilidad por `escritorio.evento`, `motor.solicitud` con `ttl_s`, esconderse sola, mirada ≤ 1/s, travesuras solo con `motor.travesuras.activas`), §C1.4, §C1.11. Principio No-LLM y `LIMITE_COMANDOS_MIN`.
**Tests:** los de v0.1 siguen pasando; puente con `httpx.MockTransport`: `theme amadeus` al cambiar `viewerSeq` (no con `VTUBER_AI_TEMA=none`); `desktop("walk_to", x=0.5)` encola `{type:"desktop",action:"walk_to",x:0.5}` y devuelve `False` sin visor; `409 {code:"sin_overlay"}` no cambia el estado y `409 {code:"sin_visores"}` → `EN_ESPERA`; corteza: con overlay visible usa `desktop(...)` y oculto no; `motor.solicitud{buscar_usuario}` con overlay oculto se retiene hasta `ttl_s` y se ejecuta al llegar `mostrada`; `pc.no_molestar{activo:true}` → publica `escritorio.orden{esconderse}` una vez y no publica `motor.travesura`; 5 muestras/s de `vision.mirada` durante 10 s → ≤ 10 `look`; `usuario.entrada{canal:"escritorio"}` → atención sin `move` x/z; saneador rechaza `desktop{action:"show"}` y `seconds:60`; nunca llama a ControlPC; sigue callando mientras habla.

### Grupo `orquestacion` — cableado de todo
**Archivos:** C3.1. **Brief:** §C2.14 completo (arranque, chat y cola de espera, turno `escritorio`, `EstadoTravesuras`, `_accion_pc` para tipos v0.2, iniciativas y overlay, `curiosidad_web`, borrador, no molestar, visión y presencia, comandos), §C2.3 (traducción al conectoma y presencia), §C2.11 (`permisos.py`: filas, `vista_previa`, `pids_extra`, `travesuras_activas`, `resolver_por_id`), §C2.12, §C1.
- **intenciones.py:** `enrutar(..., destinos: Mapping[str, str] | None = None)`; intenciones nuevas sobre texto normalizado y anclado: `abrir_destino` ("abre|pon|lanza <alias>": sinónimo → clave de `destinos`; si no es clave del catálogo, alias de `RAMS_APPS` → `lanzar_app`, solo Linux), `buscar_en` ("busca <consulta> en <alias>"), `prerrellenar` ("escríbele a X por <alias>: …", "pon en <alias>: …"; si el alias no admite prerrelleno → `escribir_texto`), `escribir_aqui` ("escribe ahí|aquí: …" → `escribir_texto{texto}`), `publicar_borrador` ("ponlo|pásalo|publícalo en <alias>"), `recordatorio_personal` ("búscame|llámame en N minutos|a las HH:MM"), `escritorio_venir` ("ven", "aparece", "sal" como mensaje completo), `escritorio_vete` ("vete", "escóndete"), `basta_travesuras` ("basta", "quieta", "deja el mouse", "para ya" como mensaje completo), `vision_pausa` ("deja de mirarme", "no me mires", "cierra los ojos"). `args["alias"]` siempre es la **clave**. `AccionPC` con `origen="usuario"`. `escritorio` hereda las capacidades de `terminal`; Telegram → `bloqueada_canal`. `_RE_LANZAR` acepta "abre whatsapp y escríbele…" partido en dos intenciones sobre mensajes consecutivos.
- **presence.py:** `escritorio` cuenta como entrada local; integra la inactividad de Windows (callable inyectado desde `ControlPC.inactividad_s`); un reinicio de inactividad a ±1 s de `ControlPC.ts_ultima_inyeccion()` se ignora.
- **broca_llm.py:** nada que decida; usa `ContextoBroca.percepcion` (una línea en el prompt de Gemini; en local, `iniciativa_conversar_app` con `{app}` cuando `percepcion.app` no está vacío) y `iniciativa_curiosidad_web`.
- **persona/frases.json** (≥ 3 entradas cada una, etiquetas `[EXP][MOTION][TONE]` válidas, voz de Kurisu): `iniciativa_curiosidad_web` (`{motivo}`), `iniciativa_conversar_app` (`{app}`), `escritorio_aparece`, `escritorio_te_busca`, `travesura_cursor` (tsundere), `basta_travesuras`, `vision_pausada`, `prerrellenar_confirma`, `escribir_confirma`, `escribir_haz_clic` (`{app}`), `accion_abrir_ok` (`{destino}`), `confirmacion_pendiente_chat`, `sin_borrador`, `sin_borrador_sin_llm`. **persona/temas_curiosidad.json:** lista curada (≥ 20 temas: ciencia, neurociencia, física, Steins;Gate…).
- **README.md:** instalación de Amadeus Desktop (Node ≥ 22, `npm i` en `desktop/`), arranque de VTuber AI Studio, atajos y emergencias (§C1.12), bandeja, reglas de Hyprland y fragmento de hypridle para el archivo-bandera `rams_quieto`, opt-in de visión, catálogo de destinos y `RAMS_DESTINOS`, travesuras OFF por defecto, "basta", que no hace falta ninguna clave nueva.
**Tests:** `test_intenciones.py` (cada intención nueva; sinónimos de `RAMS_DESTINOS`; precedencia catálogo → `RAMS_APPS`; Telegram bloquea); `test_permisos.py` (5 filas; `vista_previa` en `permiso.solicitado` y fuera de `descripcion` y de `caplog`; `travesura_cursor` por iniciativa permitida solo con maestro **y** `travesuras_activas()`; `escribir_texto` con `pid` en `pids_extra` → denegada; `resolver_por_id`: id viejo → `False`, `cerrar_ventana` → `False`, no toca ningún lock asyncio); `test_presence.py`; `test_broca_llm.py` (categorías nuevas sin `{x}` sin rellenar; `percepcion` en el prompt solo con app y título corto); `test_main.py`: un `escritorio.chat` produce **exactamente 1** `usuario.entrada{canal:"escritorio"}` redactado y 1 turno; "sí" por chat con una confirmación pendiente **no** aprueba y el texto se procesa tras `permiso.resuelto`; botón con id viejo no aprueba; `cerrar_ventana` por botón → `escritorio.sistema`; `/estado` por chat → `escritorio.sistema`; respuesta del chat en modo `terminal`; 60 s de `vision.rostro` + 1 `llegada` → como mucho 1 inyección `rostro`; `pantalla.estimulo` cada 5 s con la misma firma → ninguna inyección extra; `motor.travesura` → compuerta → `ControlPC` parcheado; aborto → travesuras apagadas 2 h y `motor.travesuras` publicado; `basta` dos veces → hasta mañana, persistido en `travesuras.json`; `escribir_texto` usa `ventana_previa` y nunca `aura.ejecutar`; iniciativa con overlay oculto → `escritorio.orden{aparecer}` antes de `motor.solicitud`; presente sin overlay → v0.1; `curiosidad_web` con `web` parcheado (cupo y separación) y sin `perfil.temas`; `publicar_borrador` usa el último borrador local y no uno de Nexus; `hud.control{camara}` llama también a `vision.set_camara(False)`; `/vision`, `/pantalla`, `/travesuras`. Todo offline y con mocks.

---

## §C4. Pruebas y smoke (sin hardware ni tocar las instancias en vivo)

Intérprete y comandos:
- Pytest: `cd /c/proyectos/rams_biocore && PYTHONPATH=. "<venv>/Scripts/python.exe" -m pytest tests/<archivo> -q -p no:cacheprovider`.
- vermin: `"<venv>/Scripts/vermin.exe" -t=3.11- --no-tips --violations <archivos>`.
- JS: `node --check <archivo>`; `node --test test/` en `vtuber-ai/` y en `desktop/`.

Cómo probar cada pieza:
- **Aislamiento global (base):** `_entorno_prueba` fija token de escritorio en `tmp_path`, travesuras/visión/pantalla apagadas; la fixture autouse `sin_efectos_pc` hace fallar cualquier efecto real del PC (§C3 base). Ningún test escribe ni borra `%LOCALAPPDATA%\rams\escritorio.json` real.
- **Python (control-pc, vision, orquestacion, motor-escritorio):** `monkeypatch`/fakes; ctypes, `cv2`, `mss`, `wtype`, `xdg-open`, `hyprctl` y subprocesos **parcheados**; relojes y fuentes de inactividad inyectados; `httpx.MockTransport`; servidores WS en `127.0.0.1:0`. **Nunca** webcam real, cursor real, tecleo, apps/URLs reales ni `:8765/:8766/:3939`.
- **escritorio-ws:** cliente `websockets` local contra un `ServidorHUD` propio en `127.0.0.1:0`.
- **visor-escritorio / kurisu:** `node --test` con `createServer({port:0})` y objetos mock; nunca `:3939`.
- **desktop:** módulos puros con `node --test`; `--smoke` oculto (§C2.7.8). Descargar `electron@44.8.0` de npm está permitido.
- **Privacidad:** test de código fuente en `vision_continua.py` (lista prohibida) y `control_pc.py` (`shell=True`).

- **Respaldo Puter (§C2.15):** nunca se carga Puter.js contra la cuenta del usuario ni se inicia sesión en Puter, y no se gasta cuota de Gemini: en Python, un Amadeus Desktop falso (cliente `websockets` sin Origin) contesta los `llm.pedir` y Broca usa un `PuterFalso`; en `desktop/`, el relevo y el sitio son puros (`node --test`) y el smoke usa una partición en memoria.

El smoke global (manual, opcional) usa un VTuber AI Studio y un R.A.M.S. arrancados aparte en puertos libres, nunca las instancias del usuario.

---

## §C5. Qué tiene que hacer el usuario y qué recursos hacen falta

**No hace falta ninguna clave de API nueva.** Todo funciona en local (Gemini sigue opcional; hoy sin clave → banco local de frases). La visión y el chat de escritorio funcionan aunque R.A.M.S. esté en modo offline.

1. **Amadeus Desktop:** `cd rams_biocore/desktop && npm i` (Electron 44.8.0, ~158 MB en Windows). Arranca VTuber AI Studio (`cd vtuber-ai && node server.js`), luego R.A.M.S., y `npm start` en `desktop/`. Atajos: `Ctrl+Alt+A` llamar, `Ctrl+Alt+Space` chat, `Ctrl+Alt+Shift+Q` quieta, `Ctrl+Alt+V` pausar visión, `Ctrl+Alt+Shift+A` dejar pasar clics. Con `AMADEUS_EXCLUIR_CAPTURA=1` (defecto) el overlay **no** sale en OBS/Discord; pon `0` si quieres capturarlo. En Arch/Hyprland, copia las reglas de ejemplo y los `hl.bind`; en Wayland sin Hyprland, `npm run start:x11`.
2. **Modelo de Kurisu:** pon un `.vrm` en `vtuber-ai/models/` (VRoid Hub ShiningFinger, o VRoid Studio exportando VRM 1.0 **sin reducir materiales** y con *Kurisu* en el *Avatar name* o en *References*). Sin modelo propio, el tema recolorea el de ejemplo. Crédito obligatorio; homenaje de fan. Si la licencia prohíbe modificarlo, `VTUBER_AI_TEMA=none` en el `.env` de R.A.M.S. **y** en el entorno con que se lanza Amadeus Desktop (Electron no lee el `.env`); ver `vtuber-ai/models/README.md`.
3. **Visión continua (opcional, OFF):** `RAMS_CAMERA=1` **y** `RAMS_VISION_CONTINUA=1`; YuNet (`face_detection_yunet_2023mar.onnx`, 232 KB, MIT) en `data/models/`; en Windows `pip install mss==10.2.0`. Pantalla: `RAMS_PANTALLA_MODO=metadatos` (o `pixeles`). Dinos dónde está la webcam, si da imagen en espejo y su ángulo (`RAMS_VISION_HFOV`). En Windows 11, para videollamadas con la cámara continua encendida, activa "Permitir que varias apps usen la cámara".
4. **Control del PC:** Windows no necesita nada; Arch: `sudo pacman -S wtype xdg-utils gtk3`. Sinónimos propios con `RAMS_DESTINOS` (p. ej. `wasap=whatsapp,twitter=x`).
5. **Respaldo Puter (opcional, §C2.15):** con Amadeus Desktop abierto, bandeja → «Conectar Puter (IA de respaldo)…» → botón → inicia sesión con **tu** cuenta de Puter (o crea una). No hay clave que copiar: el uso se descuenta de **tu saldo de Puter** y R.A.M.S. lo limita con `PUTER_MAX_CALLS_DAY` (300 por defecto). Por defecto Puter entra solo cuando Gemini no puede (cuota, red, tope diario, sin clave); `RAMS_PUTER_PRIMERO=1` lo pone delante; `PUTER_MODEL` elige otro modelo; `RAMS_PUTER=0` lo apaga. «Desconectar Puter» cierra la sesión.
6. **Decisiones pendientes:** ¿activar las travesuras con el cursor real (`RAMS_TRAVESURAS=1`; por defecto OFF, solo mirada y persecución visual)? ¿Dar el chat al HUD (`RAMS_HUD_CHAT=1`, defecto) o solo a la app? Recuerda: "basta"/`Ctrl+Alt+Shift+Q` las apaga; "deja de mirarme"/`Ctrl+Alt+V` pausan la cámara.

---

## §C6. Decisiones de revisión

Revisión crítica de la rev. 1 (43 hallazgos). Todos los hallazgos se aceptan salvo lo indicado; las modificaciones respecto a la corrección propuesta son estas:

| # | Hallazgo | Decisión |
|---|---|---|
| 1 | `usuario.entrada` desde escritorio-ws | **Aceptado** (`escritorio.chat`). Se añade la regla de bus "nadie se suscribe a `*` ni a `escritorio.*`". |
| 2 | Paso 0 y confirmaciones por chat | **Aceptado**. El texto en espera se guarda en una deque de 3 y se procesa tras `permiso.resuelto`. |
| 3 | Contrato de `amadeusChat` | **Aceptado**; además `abrirEnlace` solo abre URLs que R.A.M.S. ofreció antes. |
| 4 | Objetivo de `escribir_texto` | **Aceptado con cambio:** en Windows se usa un **sondeo a 2 Hz** (sin `SetWinEventHook`, que exige bucle de mensajes propio); el filtro de títulos admite el sufijo del navegador (`Amadeus - Google Chrome`). |
| 5 | Dueño del interruptor de travesuras | **Aceptado**; 1.er "basta" = 2 h, 2.º del día = hasta las 00:00. |
| 6 | Botones del ratón y vetos | **Aceptado parcialmente:** el "cursor falso dibujado en el overlay" para Hyprland se **pospone a la v0.3** (en Hyprland solo queda la persecución visual `chase_cursor`). El veto QUNS ∈ {2,3,4} ya lo cubre la regla más estricta `QUNS != 5`. |
| 7 | Mapeo al conectoma | **Aceptado**; `vision.atencion` tampoco se inyecta (solo percepción). |
| 8 | Dueño de la visibilidad | **Aceptado con cambio:** además de `show/hide`, se quitan **`enter`/`leave`** de `COMMANDS.desktop` (la entrada/salida por el borde va por `overlay:order` del main de Electron) para que no haya dos caminos. Se añade `desktopViewersTotal` (visores cargados, visibles u ocultos) porque main necesita saber que el overlay existe aunque esté oculto para poder sacarlo (reconcilia 8, 14 y 21). La corteza sigue la visibilidad por `escritorio.evento` (inmediato), no por el sondeo de 15 s del puente. |
| 9 | Seguridad de `server.js` | **Aceptado**; `desktop`/`theme` también se permiten con `source==='mcp'` si `MCP_DESKTOP=1` (para que `avatar_desktop` funcione cuando se habilita). |
| 10 | conftest y efectos reales | **Aceptado**; `subprocess.Popen` y `create_subprocess_exec` solo fallan con programas de la lista (no se rompen tests que lanzan `sys.executable`). |
| 11 | Cámara compartida | **Aceptado con cambio:** además del enrutado en main, `aura.set_fuente_continua` delega dentro de aura (defensa en profundidad; ningún camino abre un dispositivo ocupado). |
| 12 | Foco del chat | **Aceptado**. |
| 13 | Autoridad del overlay | **Aceptado**. |
| 14 | `motor.solicitud` | **Aceptado**; la corteza retiene la solicitud hasta `ttl_s` en vez de descartarla al instante (la visibilidad llega unos cientos de ms después de `aparecer`). |
| 15 | Refactor de `server.js` | **Aceptado**; el estado del servidor pasa a ser por instancia. |
| 16 | Carrera de `release()` | **Aceptado**. |
| 17 | Métodos síncronos en Hyprland | **Aceptado con la opción de caché**; `puede_teclear` y `puede_travesura` pasan a `async`. |
| 18 | Caja del avatar y DPI | **Aceptado**; `activar_dpi_por_monitor()` vive en `control_pc.py` y la llama main. |
| 19 | 409 y estado del puente | **Aceptado**. |
| 20 | TTS duplicado y encuadre | **Aceptado** (ambas: URL con `camera=full` y `desktop=1` fuerza `full`). La regla de TTS se aplica también a `?obs=1`. |
| 21 | Iniciativas sin overlay | **Aceptado**, con "overlay disponible" = `escritorio.conectado.overlay` **y** `visores_escritorio_total > 0` (ver 8). |
| 22 | Metadatos de destinos | **Aceptado**. |
| 23 | Salida del canal escritorio | **Aceptado**. |
| 24 | Borrador y vista previa | **Aceptado**. |
| 25 | Tema tardío/duplicado | **Aceptado con cambio:** el servidor difunde a todos los visores, así que el puente no puede elegir destinatario; en su lugar envía `theme` cuando cambia `viewerSeq` y el visor es idempotente. Nuevo `VTUBER_AI_TEMA=none` para no forzarlo nunca. |
| 26 | `move` x/z y atención | **Aceptado**. |
| 27 | Smoke | **Aceptado**. |
| 28 | Tabla de órdenes/eventos del renderer | **Aceptado**; `mostrada`/`ocultada`/`chat_abierto` los emite el main de Electron, no el renderer. |
| 29 | Curiosidad web | **Aceptado**; la curiosidad web exige además usuario presente. |
| 30 | Percepción en Broca | **Aceptado**. |
| 31 | `offline` | **Aceptado**. |
| 32 | Token en el `hola` | **Aceptado**. |
| 33 | Dueño de la presencia por rostro | **Aceptado**. |
| 34 | "No molestar" | **Aceptado con añadido:** como la visión está OFF por defecto, el "no molestar" del sistema sale de `ControlPC.no_molestar()` (QUNS / pantalla completa en Hyprland) y se publica como un único `pc.no_molestar`. |
| 35 | Archivos sin dueño | **Aceptado**. |
| 36 | CDN | **Aceptado parcialmente:** el **código** (three, three-vrm) se copia a `public/vendor/` desde npm; el **VRM de ejemplo** sigue en el CDN como dato (no está en el registro de npm y no ejecuta código), permitido solo en `connect-src`. |
| 37 | Cursor en Hyprland | **Aceptado**. |
| 38 | Token en Electron y límites | **Aceptado**. |
| 39 | Sin STT | **Aceptado**; atajos y CLI equivalentes para Hyprland. |
| 40 | `pantalla_excluir` | **Aceptado con cambio de forma:** el patrón sensible vive en `contracts.PATRON_TITULO_SENSIBLE`, se aplica **siempre**, y `RAMS_PANTALLA_EXCLUIR` solo añade extras (así el usuario no puede borrarlo por accidente). |
| 41 | Rendimiento | **Aceptado**. |
| 42 | Tasa de `look` | **Aceptado**. |
| 43 | Suscripciones del HUD | **Aceptado**. |

Otros ajustes del arquitecto en esta revisión: (a) `CANALES_SALIDA` **no** cambia (el chat recibe la salida en modo `terminal`; evita tocar la validación de `Utterance`); (b) el estado homeostático para el overlay lo traduce escritorio-ws directamente desde `homeostasis.estado` (≤ 1 Hz) en lugar de pasar por `escritorio.orden{estado}`, que se elimina; (c) la categoría local de la curiosidad web es `iniciativa_curiosidad_web`, que es la que Broca busca para las iniciativas; (d) `escritorio.evento{toque|soltada}` se traduce a `recompensa`/`error` (inyectables existentes) con límites de ritmo.

**Ajustes de la integración (ronda 1).** (1) «Quieta» pasa a `Ctrl+Alt+Shift+Q` (§C1.12: `Ctrl+Alt+Q` = AltGr+Q = «@» en el teclado latinoamericano). (2) `/escritorio` añade el mensaje `vision{camara}` para Electron (§C2.4) y el visor la orden `camara` (§C2.5.5): el ojo del overlay de §C2.13. (3) Presencia: un rostro de < 60 s gana a la inactividad (§C2.3). (4) La vista previa de las confirmaciones usa el saneado de ControlPC (§C2.3). (5) En el "no molestar" del usuario gana el estado de R.A.M.S. (§C2.4). (6) `abrir_destino` sigue **permitida sin confirmación** (§C1.8, §C2.11): "abre youtube" abre YouTube al momento; las acciones que confirman son `prerrellenar`, `escribir_texto` y `cerrar_ventana`. (7) Notas de implementación en §C2.8, §C2.9 y §C2.10; `amadeusChat.confirmar` devuelve `Promise<boolean>` y `chat:permiso` lleva `caducada?` (§C2.7.2).

**Respaldo Puter (§C2.15).** (1) Puter es **respaldo** de Gemini y no sustituto: Gemini sigue primero salvo `RAMS_PUTER_PRIMERO=1`, porque el prompt de Amadeus está afinado para él y Puter lo paga el usuario de su saldo. (2) Puter.js se ejecuta **dentro de Amadeus Desktop** y no en un navegador aparte ni en Node: es una biblioteca de navegador cuyo inicio de sesión necesita una ventana emergente y un gesto del usuario; la ventana oculta usa su propia partición y el token de R.A.M.S. no sale del proceso principal. (3) Sin la app, sin sesión o con `--offline`, todo sigue como antes (Gemini y banco local): el respaldo nunca bloquea un turno más de 40 s y nunca lanza. (4) Ajustes de la integración: los límites de `llm.pedir` se miden en unidades UTF-16 (como los valida JavaScript); el efecto de `sin_sesion`/`cuota` se aplica al **recibir** la respuesta (para que el `puter.estado` de resincronización que llega detrás gane); un `puter.estado` sobre el cupo se aplica al vencer la ventana y una `llm.respuesta` a un pedido pendiente nunca se descarta por ritmo; el patrón de `PUTER_MODEL` es el mismo en `core/config.py`, en el enlace y en el escritorio (`RE_MODELO`), en los dos sentidos. (5) Revisión del enlace: el semáforo de R.A.M.S. cuenta los pedidos **en curso en el escritorio** (antes soltaba el turno al vencer su propio plazo y el escritorio, aún ocupado, rechazaba el siguiente, que Broca contaba como fallo y acababa enfriando Puter); el diagnóstico explica los códigos de `puter.estado.error` en vez de decir «sin sesión»; y el detalle de la pausa por saldo no lleva cuenta atrás (se quedaba congelado en el HUD y en `/estado`). Rechazado: medir el plazo desde el envío (rompería «`completar` no tarda más de `timeout_s`», que Broca corta a los 42 s) y añadir `plazo_ms` o un mensaje de cancelación al contrato (cambia el contrato del escritorio; el coste de un pedido abandonado queda acotado por el aborto de 38 s).
