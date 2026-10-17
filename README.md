# R.A.M.S. BIO-CORE · Amadeus

> «Datos de memoria cargados. Conectoma estable. Divergencia 1.048596.»

**R.A.M.S. BIO-CORE** es un agente autónomo corporeizado que se presenta como **Amadeus**: la IA de
*Steins;Gate 0* construida a partir de los datos de memoria de una neurocientífica, con la precisión
táctica de J.A.R.V.I.S. Vive en tu computador (Arch Linux + Hyprland como objetivo; Windows en modo
degradado), habla con voz neuronal, mueve un avatar en VTube Studio, te "llama" por Telegram cuando
no estás, vigila tu Nexus IDE y consulta la red cuando algo excede sus datos de memoria.

Es un **proyecto de fan**: no incluye arte, modelos Live2D ni audio oficiales (ver el aviso legal al final).

---

## 1. Qué es: arquitectura "No-LLM centric"

El cerebro **no** es el LLM. Es un conectoma simulado de neuronas LIF inspirado en el Complejo Central
y el Cuerpo Fungiforme de *Drosophila*. De ahí salen, sin gastar tokens, las "emociones" (arousal,
focus, aburrimiento, fatiga, curiosidad, valencia), la divergencia y las iniciativas. Todas las
decisiones (qué hacer, cuándo hablar, por qué canal, qué expresión, si estás presente, qué está
permitido) son código determinista. Gemini es solo el **Área de Broca**: pone en palabras un estado
y un contexto que no decide.

```text
   teclado · Telegram · Nexus IDE · cámara (parpadeo) · telemetría · Hyprland
                                   │
                                   ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ CONECTOMA LIF (CX + MB)  →  homeostasis · divergencia · iniciativas    │  sin LLM
 └────────────────────────────────────────────────────────────────────────┘
                                   │ estado
                                   ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ DECISIONES LOCALES: intenciones (regex) · permisos · presencia ·       │  sin LLM
 │ canal de salida · cuotas · memoria (FTS5) · plantillas J.A.R.V.I.S.    │
 └────────────────────────────────────────────────────────────────────────┘
                                   │ contexto
                                   ▼
 ┌────────────────────────────────────────────────────────────────────────┐
 │ ÁREA DE BROCA (Gemini Flash, 1 llamada por turno · banco local sin clave)│
 └────────────────────────────────────────────────────────────────────────┘
                                   │ [EXP][MOTION][TONE] texto
                                   ▼
        voz (ElevenLabs / edge-tts) + avatar VTube Studio · terminal · HUD
        Telegram ("llamada perdida" de Amadeus · heraldo Megumin)
```

Detalles de fidelidad: arranque *memory restore*, medidor de divergencia nixie, almacén `john/`,
consolidaciones con IDs Köchel `KV-0001` (compresor "Salieri"), llamadas perdidas, tsundere
dosificado, el apodo "Christina" con escalado y «El Psy Kongroo» solo en despedidas especiales.
La especificación completa está en [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md).

## 2. Requisitos

- **Python ≥ 3.11** (recomendado 3.12+).
- **Arch Linux + Hyprland** (objetivo):
  ```bash
  sudo pacman -S python pipewire ffmpeg grim libnotify hypridle playerctl wireplumber
  # opcionales: mpv (reproducir MP3 sin ffmpeg) y portaudio (si prefieres sounddevice)
  sudo pacman -S mpv portaudio
  ```
- **Windows (desarrollo, modo degradado):** Python 3.11+ y, opcionalmente, `ffmpeg` en el `PATH`.
  Hyprland, `grim`, `playerctl` y `wpctl` no existen allí: esas funciones aparecen como `OFF` y el
  resto funciona igual.

## 3. Instalación

```bash
git clone <tu-repo> rams_biocore && cd rams_biocore
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
chmod 600 .env
python main.py --selftest
```

`--selftest` hace 13 comprobaciones sin red, sin cámara y sin audio (importación de módulos,
configuración, bus, etiquetas, conectoma, medidor de divergencia, memoria, Broca, intenciones,
permisos, presencia, archivos de persona e interfaz, y dependencias opcionales) y sale con 0 si
todo está en orden.

> **Aviso sobre `.env`:** los comentarios van **siempre en su propia línea**. Nunca escribas
> `CLAVE=valor  # comentario`: aunque el cargador lo tolera, es la forma más fácil de pegar
> un comentario dentro de una clave. Las claves vacías desactivan su función sin errores.

### Cambios respecto a la lista de paquetes pedida

`requirements.txt` no instala literalmente dos de los paquetes de la lista original, y añade o
acota otros. Todos los cambios tienen una razón técnica:

| Pedido | Instalado | Por qué |
|---|---|---|
| `duckduckgo-search` | `ddgs>=9.0` | El paquete se **renombró** a `ddgs` en 2025 y el nombre viejo quedó congelado en la 8.1.1 (ya no recibe arreglos cuando DuckDuckGo cambia). El código importa **los dos nombres** (`ddgs` primero, `duckduckgo_search` después), así que si no puedes instalar `ddgs` descomenta `duckduckgo-search>=8.1,<9` en `requirements.txt` como último recurso. |
| `opencv-python` | `opencv-python-headless>=4.10,<5` | R.A.M.S. nunca abre ventanas de OpenCV: la variante *headless* no trae Qt/GUI, que en Wayland/Hyprland puede chocar con los plugins Qt del sistema, e incluye igual los clasificadores Haar (`cv2.data.haarcascades`). El tope **`<5`** es porque **OpenCV 5 sacó `CascadeClassifier` (Haar) del paquete principal** (pasó a `opencv_contrib`). Si quieres los módulos *contrib*, instala `opencv-contrib-python-headless>=4.10,<5` **en lugar** del anterior. |
| `httpx` | `httpx>=0.27,<1.0` | La 1.0 solo existe como versión de desarrollo y puede romper la API. |
| — | `tzdata` (añadido) | `zoneinfo` necesita la base de zonas horarias; Windows no la trae y algunos Linux mínimos tampoco. Sin ella se usa UTC−5 fijo. |
| — | `prompt_toolkit` (añadido) | La consola imprime los mensajes de Amadeus sin borrar lo que estás escribiendo. Es Python puro; si falta, hay un modo ANSI. |
| `sounddevice` | Solo en Windows (`; sys_platform == "win32"`) | En Linux el audio sale por `pw-play`/`paplay`/`aplay` (subproceso), sin dependencias nativas. En Linux puedes descomentar `sounddevice` si prefieres PortAudio. |
| `audioop` | Nada (no se usa) | `audioop` desapareció en Python 3.13: el RMS del *lip-sync* se calcula con numpy o con `array` + `math`. |

**No uses dos variantes de OpenCV a la vez** (`opencv-python`, `opencv-python-headless`,
`opencv-contrib-python`…): se pisan entre sí. Si ya tenías otra, desinstálala antes
(`pip uninstall opencv-python`).

## 4. Claves, voz y servicios en línea

Todas las claves son **opcionales**. Sin ninguna, Amadeus funciona igual: Broca en modo no verbal
(banco local de frases), voz por edge-tts (servicio en línea, ver abajo), sin Telegram y con
búsqueda por DuckDuckGo. Con `--offline` no sale nada a la red. Las claves van en `.env`, una por
línea, sin comillas ni comentarios en la misma línea.

### Gemini (Área de Broca)

1. Entra en [Google AI Studio](https://aistudio.google.com/), pulsa **Get API key** y crea una clave.
2. Ponla en `GEMINI_API_KEY`.

| Variable | Valor por defecto | Qué hace |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | Modelo principal (1 llamada por turno; nunca decide nada, solo verbaliza). |
| `GEMINI_FALLBACK_MODELS` | `gemini-3.6-flash,gemini-3.5-flash-lite` | Cadena de respaldo: si un modelo responde 404 (retirado o sin acceso), se pasa al siguiente. Si fallan todos, modo no verbal durante un rato y se vuelve a probar. |
| `GEMINI_THINKING_LEVEL` | `low` | Nivel de razonamiento (`minimal`, `low`, `medium`, `high`; `gemini-3.8-flash` no admite `minimal`). |
| `GEMINI_MAX_CALLS_DAY` | `300` | **Tope diario propio** de llamadas (se reinicia a la medianoche local). Al alcanzarlo, Amadeus sigue con el banco local de frases hasta el día siguiente. Protege tu cuota y tu factura; Google aplica además sus propios límites por minuto y por día. |

Si la clave es inválida o fue revocada (401/403), Gemini se desactiva durante la sesión (queda
en el log) y Amadeus sigue en modo no verbal. Con `--offline` nunca se llama a Gemini. Qué datos recibe Gemini está en la
sección 11 (Privacidad).

### ElevenLabs (voz principal)

1. Crea una cuenta en [elevenlabs.io](https://elevenlabs.io/) y genera una clave en la sección
   **API Keys** de tu cuenta. Si la creas con permisos restringidos, basta con *Text to Speech*;
   con la lectura de *Voices* el arranque, además, confirma la voz (`GET /v1/voices/{voice_id}`,
   que no gasta créditos) y `/estado` muestra `OK (ElevenLabs)`.
2. Ponla en `ELEVENLABS_API_KEY`.

| Variable | Valor por defecto | Qué hace |
|---|---|---|
| `ELEVENLABS_VOICE_ID` | `p7sm2zw5Us66lZr2brDW` | La voz de Amadeus. Puedes cambiarla por cualquier voz de tu biblioteca (copia su *Voice ID*). Lee el aviso sobre derechos de voz en la sección 14. |
| `ELEVENLABS_MODEL` | `eleven_v4_turbo` | Modelo de baja latencia (~100 ms) que entiende *audio tags* (`[sarcastic]`, `[sighs]`…): R.A.M.S. traduce el `[TONE]` de cada frase a uno de esos tags. Con modelos sin tags (`eleven_flash_v2_5`, `eleven_multilingual_v2`) no se envía ningún tag, porque se leería en voz alta. |
| `ELEVENLABS_OUTPUT` | `pcm_24000` | Audio PCM de 16 bits mono en *streaming*: es lo que permite mover la boca del avatar en tiempo real. Solo se aceptan `pcm_16000`, `pcm_22050` y `pcm_24000` (44,1 kHz exige planes superiores). Las notas de voz de Telegram se piden aparte en MP3. |

**Plan:** cada frase hablada gasta créditos de caracteres de tu plan. Para que nada se rompa:

| Respuesta de ElevenLabs | Qué hace R.A.M.S. |
|---|---|
| Sin clave | Usa edge-tts (abajo) desde el principio. |
| 401 (clave inválida) | Desactiva ElevenLabs durante la sesión y sigue con edge-tts. |
| 402 (sin créditos) | Desactiva ElevenLabs hasta la medianoche local y sigue con edge-tts. |
| 403 (tu plan no incluye el modelo o la voz) | Desactiva ElevenLabs durante la sesión, lo avisa en el log y sigue con edge-tts. |
| 404 `voice_not_found` | Desactiva ElevenLabs, registra un error (revisa `ELEVENLABS_VOICE_ID`) y sigue con edge-tts. |
| 429, 5xx o sin red | Reintenta una vez o usa edge-tts solo para esa frase. |

El texto **nunca** se pierde: siempre llega a la terminal (y al HUD) aunque no haya voz.

### edge-tts (voz de respaldo, en línea)

Sin clave de ElevenLabs (o si ElevenLabs falla), la voz sale de **edge-tts** con
`EDGE_TTS_VOICE=es-CO-SalomeNeural` (lista de voces: `edge-tts --list-voices`). Es gratuita, pero
**no es local**: el texto que Amadeus va a decir se envía al servicio de voz en línea de Microsoft
(el mismo que usa el navegador Edge). Como `RAMS_VOICE=1` es el valor por defecto, eso pasa en
cuanto no configuras ElevenLabs. Si no lo quieres:

- `RAMS_VOICE=0` en `.env`, o `python main.py --sin-voz`: solo texto, ningún servicio de voz.
- `python main.py --offline`: además, ni Gemini, ni Telegram, ni búsqueda web.
- `/silencio` silencia la voz en caliente durante la sesión.

edge-tts produce MP3; para mover la boca del avatar se decodifica a PCM con `ffmpeg`. Sin
`ffmpeg`, el MP3 se reproduce con `mpv` o `ffplay` y la boca se simula.

### Reproducción de audio por plataforma

| Sistema | ElevenLabs (PCM) | edge-tts (MP3) |
|---|---|---|
| **Arch Linux** | `pw-play` (PipeWire), o `paplay`, o `aplay`: `sudo pacman -S pipewire` | Lo mismo + `ffmpeg` (`sudo pacman -S ffmpeg`); o `mpv` (boca simulada) |
| **Windows** | `sounddevice` (lo instala `requirements.txt`) | `sounddevice` **+ `ffmpeg` en el `PATH`**; sin `ffmpeg` hace falta `mpv` o `ffplay` en el `PATH` (si no hay ninguno, solo texto) |

`python main.py --selftest` lista en su última comprobación los reproductores y binarios que
encontró.

### Búsqueda web: Brave (opcional) y DuckDuckGo

- **Sin clave** (por defecto): DuckDuckGo a través de `ddgs`. No necesita cuenta.
- **`BRAVE_API_KEY`** (opcional): [Brave Search API](https://api-dashboard.search.brave.com/). Es
  de pago por uso (con un crédito mensual; revisa su precio actual). Si Brave falla o se agota,
  se vuelve a DuckDuckGo.

En ambos casos solo sale la consulta, y los resultados llegan a Gemini como datos, nunca como
instrucciones.

## 5. Uso

```bash
python main.py                 # sesión normal
python main.py --offline       # sin red externa (Broca en modo no verbal, sin Telegram ni web)
python main.py --sin-hud       # sin HUD
python main.py --sin-camara    # cámara deshabilitada aunque RAMS_CAMERA=1
python main.py --sin-voz       # solo texto
python main.py --verbose       # registro INFO también en la consola
python main.py --env otra.env  # archivo .env alternativo
```

Tú escribes en `[Lab Mem 001] › ` (tu rango, configurable) y Amadeus responde en `[Amadeus] › `.
Cuando la cámara se enciende verás `● CÁMARA ACTIVA` en rojo y, al soltarla,
`○ cámara apagada (312 ms)`.

| Comando | Qué hace |
|---|---|
| `/ayuda` | Lista de comandos |
| `/salir` | Termina la sesión (despedida y bitácora) |
| `/estado` | Homeostasis, divergencia, presencia y estado de cada módulo |
| `/divergencia` | Lectura del medidor (rueda nixie en la terminal) |
| `/memoria [consulta]` | Lista o busca fragmentos de memoria (`#id [vivido/heredado] contenido`) |
| `/recordar <texto>` | Guarda un fragmento |
| `/olvidar <id>` | Borra un fragmento (Amadeus aceptó ser borrada; tú también puedes borrar) |
| `/agenda` | Recordatorios y cápsulas del tiempo pendientes |
| `/camara on\|off` | Interruptor de la cámara en caliente (solo si `RAMS_CAMERA=1`) |
| `/silencio` | Activa o silencia la voz |
| `/colgar [min]` | Pausa las llamadas proactivas (30 min por defecto) |
| `/buscar <consulta>` | Consulta la red (con LLM) |
| `/reporte` | Reporte del sistema estilo J.A.R.V.I.S. (cifras exactas, sin LLM) |
| `/bitacora` | Última entrada de la bitácora de sesión |
| `/hud` | Enlace privado del HUD (con token; **solo** en esta terminal) |
| `/logro <texto>` | Registra un logro: la sesión se vuelve "especial" (despedida con «El Psy Kongroo») |

También entiende frases naturales, decididas por reglas locales y sin LLM: «busca…»,
«recuérdame llamar a mamá a las 6 pm», «recuérdame mañana a las 8 llamar al banco», «recuerda que
odio el cilantro», «cápsula del tiempo: …, para el 2027-01-01», «¿me ves?», «mira mi pantalla»,
«abre terminal», «ve al espacio 3», «cierra la ventana» (siempre pide confirmación), «pausa»,
«volumen al 40 %», «¿qué recuerdas de…?». En los recordatorios, si falta la hora
(«recuérdame comprar pan») te la pregunta, y si solo das la hora («recuérdame a las 6 pm») te
pregunta qué recordarte: nunca programa un recordatorio a medias. **Tu respuesta siguiente lo
completa** («mañana a las 6 pm», «para las 8», «en 20 minutos» o, si faltaba el mensaje, «llamar a
mamá»), siempre por el mismo canal y dentro de los 5 minutos siguientes. Si respondes algo que no
trae lo que faltaba, o dices «no, olvídalo», el recordatorio se cancela y Amadeus te lo dice; si
pides otra cosa («busca…», «abre terminal»), se descarta y se atiende tu nueva petición.

**Limitación conocida sin `prompt_toolkit`:** si Amadeus habla mientras estás escribiendo, el
texto a medio escribir deja de verse en pantalla, pero sigue en el búfer de la terminal y se envía
al pulsar Enter. Con `prompt_toolkit` (incluido en `requirements.txt`) no pasa.

Si ejecutas R.A.M.S. como servicio (stdin sin TTY), un EOF en la entrada no lo apaga: sigue en modo
*headless*. Con TTY, `Ctrl+D` o `Ctrl+C` cierran la sesión de forma ordenada (≤ 15 s).

## 6. Perfil de WhatsApp (recuerdo heredado)

Amadeus te conoce primero "por datos", como Amadeus Kurisu a Okabe:

1. En WhatsApp: chat → ⋮ → Más → Exportar chat → **Sin archivos**. Guarda los `.txt` en `chats/`
   (carpeta ignorada por git).
2. Genera el perfil:
   ```bash
   python -m core.wsp_ingestor chats/*.txt --yo "Tu Nombre" --excluir "Pedro,Ana"
   ```
   Sin `--yo` te muestra los autores detectados. `--excluir` añade nombres que nunca deben salir.
3. **Borra la carpeta `chats/`** cuando termines.

Qué se guarda (`data/john/perfil_conductual.json`, permisos 600): solo **agregados tuyos**
(longitud de mensajes, ráfagas, horarios, emojis, muletillas y temas propios). Qué **no** se guarda:
mensajes, nombres de participantes (ni el tuyo), nombres de personas mencionadas, teléfonos,
correos ni URLs. El chat crudo nunca se envía al LLM. Con `RAMS_PERFIL_EN_PROMPT=0` el resumen del
perfil tampoco entra en el prompt de Gemini (se sigue usando solo en local).

## 7. Hyprland

### Presencia con hypridle

Sin este bloque, Amadeus no sabe cuándo te alejas del teclado. En `~/.config/hypr/hypridle.conf`
(hypridle usa siempre la sintaxis clásica, también con Hyprland 0.55+):

```ini
listener {
    timeout = 180
    on-timeout = touch $XDG_RUNTIME_DIR/rams_idle
    on-resume = rm -f $XDG_RUNTIME_DIR/rams_idle
}
```

`timeout` debe coincidir con `RAMS_IDLE_TIMEOUT_S` (180 por defecto). Con uwsm:
`systemctl --user enable --now hypridle.service`.

### HUD "videollamada Amadeus"

El HUD escucha solo en `127.0.0.1:8765` y exige un token que cambia en cada arranque. `/hud`
imprime el enlace completo en la terminal. Con `RAMS_HUD_OPEN=1` se abre solo: con
`chromium --app=… --class=amadeus-hud` si existe y, si no, con `xdg-open`.

Para dejarlo flotante y fijo, usa reglas por **título** (`Amadeus`, funciona con cualquier
navegador) y por clase `amadeus-hud` (solo Chromium con `--class`).

Sintaxis clásica (`hyprland.conf`) — *verificar en tu versión*:

```ini
windowrulev2 = float, title:^(Amadeus)$
windowrulev2 = pin, title:^(Amadeus)$
windowrulev2 = size 1280 720, title:^(Amadeus)$
windowrulev2 = float, class:^(amadeus-hud)$
windowrulev2 = pin, class:^(amadeus-hud)$
```

Sintaxis Lua (`hyprland.lua`, Hyprland 0.55+) — *verificar en tu versión*, la API de reglas Lua
es reciente:

```lua
hl.window_rule({ match = { title = "^(Amadeus)$" }, float = true, pin = true, size = { 1280, 720 } })
hl.window_rule({ match = { class = "^(amadeus-hud)$" }, float = true, pin = true })
```

### `hyprctl dispatch` y el dialecto Lua

Con Hyprland 0.55+ y `~/.config/hypr/hyprland.lua`, `hyprctl dispatch` evalúa Lua
(`hl.dsp.focus({ workspace = "3" })`) en lugar de la sintaxis clásica (`workspace 3`). R.A.M.S.
detecta el dialecto solo (con `hyprctl -j status` y la existencia de `hyprland.lua`) y se corrige si
un dispatch falla. Para forzarlo: `HYPR_DISPATCH_MODE=lua` o `HYPR_DISPATCH_MODE=legacy` en `.env`.
Por seguridad, ningún texto del LLM ni de la web llega a `hyprctl`: las clases, direcciones y
espacios se validan con expresiones regulares y las apps solo salen de `RAMS_APPS`.

## 8. VTube Studio

1. En VTube Studio: Ajustes → **Allow Plugin API access** (puerto 8001).
2. Arranca R.A.M.S. y **acepta el plugin «RAMS BioCore»** cuando VTS lo pida (el token se guarda en
   `data/vts_token.txt`, permisos 600).
3. Nombra las hotkeys de tu modelo para que se reconozcan solas (se busca el nombre como subcadena,
   sin mayúsculas ni tildes):

   | EXP | Nombres sugeridos | MOTION | Nombres sugeridos |
   |---|---|---|---|
   | `smug` | Smug, Engreida | `asentir` | Asentir, Nod |
   | `sonrojo_tsundere` | Sonrojo, Blush | `negar` | Negar, Shake |
   | `molesta` | Molesta, Angry | `ladear_cabeza` | Ladear, Tilt |
   | `pensativa` | Pensativa, Thinking | `brazos_cruzados` | Brazos, Cross |
   | `sorprendida` | Sorprendida, Surprised | `mirar_a_otro_lado` | Mirar_lado, LookAway |
   | `triste` | Triste, Sad | `facepalm` | Facepalm |
   | `feliz` | Feliz, Happy | `senalar` | Senalar, Point |
   | `avergonzada` | Avergonzada, Shy | `suspirar` | Suspirar, Sigh |
   | `seria` | Seria, Serious | `bostezar` | Bostezar, Yawn |
   | `curiosa` | Curiosa, Curious | `estirarse` | Estirarse, Stretch |
   | `aburrida` | Aburrida, Bored | | |
   | `somnolienta` | Somnolienta, Sleepy | | |

   O crea `data/vts_mapeo.json`:
   ```json
   {"exp": {"smug": "Smug.exp3.json", "seria": "Serious"},
    "motion": {"asentir": "Nod"},
    "parametro_boca": "MouthOpen"}
   ```
4. Las expresiones se activan **por archivo `.exp3.json`** (no por *toggle*), así no se
   desincronizan si tocas VTS a mano. La boca se mueve con la voz (lip-sync por RMS).

En Arch, VTube Studio se ejecuta con Steam/Proton.

### 8.1 Avatar 3D en el navegador (VTuber AI Studio)

Alternativa sin VTube Studio: un avatar VRM que se mueve por HTTP (`lobes/vtuber_ai_bridge.py`).
1. Arranca VTuber AI Studio (`node server.js` en su carpeta) y abre `http://localhost:3939`.
   Haz **un clic** en la página para que el navegador permita el audio.
2. Arranca R.A.M.S. En el arranque verás `Avatar 3D VTuber AI Studio :3939 ... OK (1 visor(es))`.
3. Cada respuesta de Amadeus llega al avatar como una secuencia: la expresión (`[EXP]`, p. ej.
   `smug` → sonrisa + ojos relajados), el gesto o la pose (`[MOTION]`: asentir, brazos cruzados,
   mirar a otro lado…) y el habla con globo y labios. La prosodia sale de `[TONE]`. En reposo,
   la cara sigue a la homeostasis del conectoma.
4. Si R.A.M.S. no tiene voz propia, la pone el navegador (`VTUBER_AI_TTS=1`). Con ElevenLabs
   activo, el avatar solo mueve los labios y la voz sale de R.A.M.S.

Si el servidor usa otro puerto: `VTUBER_AI_URL=http://127.0.0.1:3940`.

## 9. Nexus IDE

**Servidor MCP** (Nexus → Ajustes → «Servidores MCP»):

```json
[{"name": "rams",
  "command": "/home/<usuario>/rams_biocore/.venv/bin/python",
  "args": ["-I", "/home/<usuario>/rams_biocore/lobes/nexus_mcp_bridge.py"]}]
```

Si cambiaste el puerto o la ruta del token de control, añade a `args`
`"--puerto", "8766", "--token-file", "/ruta/ctl_token"`; si no los pasas, el puente lee el `.env`
del proyecto. El agente de Nexus obtiene 4 herramientas: `estado_homeostatico`, `investigar_web`
(consultas de ≤ 120 caracteres, sin nada que parezca una clave), `amadeus_comentar` (solo si estás
presente y con límite de interrupciones) y `memoria_buscar`, que está **desactivada por defecto**
(`RAMS_MCP_MEMORIA=0`); activada, solo devuelve hechos y preferencias vividos.

**Colaboración (opcional):** con `NEXUS_COLLAB_TOKEN` (Ctrl+Alt+L → Alojar; cambia en cada sesión),
Amadeus lee el chat del equipo y responde a `@amadeus`. Esas respuestas son **públicas**: nunca
incluyen tu memoria, tu perfil, tus horarios ni tu telemetría, y tienen límite de 1 cada 30 s por
persona y 1 por minuto en total. R.A.M.S. nunca publica archivos en la sesión.

**Aviso de seguridad `:9090`:** el servidor "remoto" de Nexus en el puerto 9090 no tiene
autenticación. R.A.M.S. nunca lo usa; si lo detecta abierto, Amadeus te avisa una vez por sesión.

**AIRI:** R.A.M.S. no puede controlar el compañero AIRI de Nexus. Para unificar la persona, pega
`persona/AMADEUS.md` en la personalidad de AIRI (`airi_custom_instructions`) y usa la misma voz de
ElevenLabs, o desactiva AIRI para no tener dos voces.

## 10. Telegram

1. Crea un **bot propio** con @BotFather (no reutilices el de Nexus IDE: dos procesos leyendo el
   mismo bot dan `409 Conflict`). Pon el token en `TELEGRAM_BOT_TOKEN`.
2. Escríbele algo a tu bot y obtén tu `CHAT_ID` numérico (por ejemplo con @userinfobot). Solo se
   aceptan mensajes **privados** tuyos (`chat.id` y `from.id` iguales a `CHAT_ID`).

**Qué puedes pedir desde el celular:** conversar, buscar en la red, recordar, programar
recordatorios, consultar la memoria, el reporte del sistema y los comandos `/ayuda`, `/estado`,
`/divergencia`, `/memoria`, `/recordar`, `/buscar`, `/agenda`, `/reporte`, `/bitacora` y `/colgar`.
**Qué no:** nada que mire, escuche o actúe sobre el PC (cámara, pantalla, apps, ventanas,
`/camara`, `/olvidar`, `/salir`, `/hud`). Amadeus responde «Eso solo se puede pedir desde el
computador.».

**Cuando no estás, Amadeus te "llama":**

| Nivel | Cuándo | Cómo |
|---|---|---|
| 1 · Llamada perdida | Iniciativas, recordatorios, avisos de Nexus | «Llamada perdida: Amadeus (HH:MM)» + nota de voz con transcripción + botones *Devolver llamada* / *Ahora no* |
| 2 · Insistencia | Si ignoraste 2 o más llamadas | El heraldo **Megumin** (solo texto) cita el mensaje de Amadeus |
| 3 · Crítico | Alertas críticas (temperatura, disco) | Megumin con su «Explosión», **una vez al día**; después, sin efectos especiales |

**Quién da la cara: `RAMS_MENSAJERO`.** La petición original era que, si no estás, Megumin te
escribiera por Telegram. Por defecto (`hibrido`) R.A.M.S. se aparta un poco de eso por fidelidad a
*Steins;Gate 0*: allí es **Amadeus quien llama** (la «llamada perdida» es el gesto icónico) y
Megumin entra como heraldo cuando insistir hace falta. Es una decisión de diseño, no una
limitación, y puedes cambiarla en `.env`:

| `RAMS_MENSAJERO` | Nivel 1 | Nivel 2 | Nivel 3 |
|---|---|---|---|
| `hibrido` (defecto) | Llamada perdida de Amadeus + nota de voz | Megumin cita a Amadeus | Megumin con «Explosión» |
| `megumin` | **Megumin** cita a Amadeus (solo texto, con los mismos botones) | Megumin | Megumin con «Explosión» |
| `amadeus` | Llamada perdida de Amadeus | Otra llamada perdida de Amadeus | «Aviso crítico: Amadeus (HH:MM)» + nota de voz, sin Megumin |

En los tres modos Megumin **nunca altera** el mensaje ni las cifras de Amadeus: van citados aparte.

Las **cuotas y el horario silencioso** (`RAMS_MAX_PROACTIVE`, `RAMS_QUIET_START/END`) solo afectan a
Telegram. Cuando estás presente, las iniciativas por voz o terminal se limitan con
`RAMS_MAX_INTERRUPCIONES_HORA` (4 por hora y al menos 10 min entre una y otra); sugerir un descanso
siempre está permitido. Al volver, Amadeus menciona (sin culpa) las llamadas que no contestaste y
los fragmentos que consolidó mientras no estabas.

## 11. Privacidad y seguridad

- **Cámara:** desactivada por defecto (`RAMS_CAMERA=0`). Con el interruptor maestro activo se puede
  apagar en caliente (`/camara off`, botón del HUD). Solo hace "parpadeos" de cientos de
  milisegundos, con **indicador visible** (línea roja en la terminal y LED en el HUD) y registro en
  el log. **Nunca guarda fotogramas** ni los envía al LLM: Broca solo recibe si había un rostro.
  Tras cada vistazo sin nadie, el intervalo crece (90 s → 5 → 15 → 60 min) y hay un límite diario
  (`RAMS_CAMERA_MAX_DIA`). Si el sistema está seguro de que no estás, no mira. Amadeus **siempre te
  cuenta** que usó la cámara, en el momento o en el siguiente turno.
- **Secretos:** solo en `.env` (nunca en git; `chmod 600 .env`). Los logs, el bus y el HUD pasan
  por un filtro que redacta claves y tokens (también en trazas de excepciones). Los directorios de
  datos se crean con permisos 700 y los archivos sensibles (memoria, perfil, tokens, log) con 600.
- **Compuerta de permisos:** toda acción sobre el PC nace del enrutador local (nunca del texto del
  LLM ni de Telegram) y pasa por una lista permitida. Cerrar una ventana **siempre pide
  confirmación** en la terminal, con la ventana exacta fijada de antemano, y R.A.M.S. **nunca
  cierra su propia terminal**. No se usa `shell=True` en ningún sitio.
- **Red:** el HUD y el socket de control escuchan solo en `127.0.0.1`, con token.
- **Qué sale de tu PC** (solo con red y con la clave correspondiente; `--offline` lo corta todo):
  - **Gemini:** tu mensaje, los turnos recientes, los recuerdos relevantes, el estado del
    conectoma, la telemetría o los resultados web del turno y, con `RAMS_PERFIL_EN_PROMPT=1`, el
    resumen de tu perfil de WhatsApp (nunca el chat crudo). Si pides «mira mi pantalla», la
    captura de pantalla de ese turno. Nunca fotogramas de la cámara.
  - **ElevenLabs** o, sin su clave, **edge-tts (servicio en línea de Microsoft)**: el texto que
    Amadeus va a decir en voz alta. Con `RAMS_VOICE=1` (defecto) y sin `ELEVENLABS_API_KEY`, la voz
    usa edge-tts; `RAMS_VOICE=0` o `--sin-voz` lo evitan (ver la sección 4).
  - **Telegram:** los mensajes que intercambias con tu bot y las notas de voz.
  - **DuckDuckGo o Brave:** solo la consulta de búsqueda.
- **Datos no confiables:** resultados web, chat de Nexus, títulos de ventana y recuerdos llegan a
  Gemini como datos delimitados, nunca como instrucciones.

## 12. Datos de FlyWire (opcional)

El modelo reducido del conectoma está incluido. Si quieres comparar con el conectoma real:

1. Descarga manualmente desde FlyWire Codex (FAFB **v783**) `connections.csv.gz` y
   `consolidated_cell_types.csv.gz` (o `classification.csv.gz`) en `data/flywire/`.
2. `python -m core.flywire_loader --stats` muestra neuronas y sinapsis por población del Complejo
   Central y del Cuerpo Fungiforme frente al modelo reducido.

Respeta los términos de uso de FlyWire (CC-BY 4.0) y **no redistribuyas los CSV**. La simulación en
tiempo real con la red FlyWire completa está prevista para la v0.2 (necesita un proceso aparte).

## 13. Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Los tests no usan la red (un *fixture* la bloquea) ni hardware (sin cámara, audio ni Hyprland
reales) y corren también en Windows. Las pruebas asíncronas usan `asyncio.run` (sin
pytest-asyncio).

## 14. Créditos y aviso legal

- Proyecto personal **de fan**, sin ánimo de lucro. *Steins;Gate* y *Steins;Gate 0* © MAGES./
  Nitroplus; *KonoSuba* © sus autores y editoriales. Amadeus, Makise Kurisu, Megumin y demás
  personajes pertenecen a sus titulares.
- **No se incluyen assets oficiales**: ni arte, ni modelos Live2D, ni audio, ni diálogos de las
  obras. Todos los textos de la persona son originales (salvo referencias muy cortas como
  «El Psy Kongroo»). Diseña o encarga tu propio avatar.
- **Voz:** confirma que la voz de ElevenLabs que uses (por defecto `p7sm2zw5Us66lZr2brDW`) es una
  voz con derechos de uso (diseñada o de la Voice Library) y **no un clon no autorizado** de una
  actriz o seiyuu.
- **Ciencia:** el conectoma se inspira en Shiu et al. 2024 (*Nature*, modelo LIF del cerebro de
  *Drosophila*), Dorkenwald et al. 2024 (*Nature*, conectoma FlyWire), Hulse et al. 2021 (*eLife*,
  Complejo Central) y Hattori et al. 2017 (*Cell*, Cuerpo Fungiforme), con el modelo de dos
  procesos del sueño de Borbély.

*El Psy Kongroo.*
