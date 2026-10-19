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

### Novedades de la v0.2 "Amadeus Companion"

- **Amadeus Desktop** (sección 8.2): una app de escritorio que vive en la bandeja. Amadeus aparece
  caminando desde el borde de la pantalla cuando la llamas (o cuando **ella** decide buscarte porque
  está aburrida, tiene un recordatorio para ti o encontró algo curioso), pasea, se asoma, sigue el
  cursor con la mirada y, si tú lo activas, hace pequeñas **travesuras con el mouse**.
- **Chat**: escríbele desde la ventana de chat de Amadeus Desktop o desde el panel RINE del HUD.
- **Control de apps y redes** (sección 8.3): abre apps y webs de un catálogo cerrado, busca dentro de
  ellas, deja **borradores** listos en WhatsApp/Telegram/X/Gmail y, donde no hay borrador, teclea en
  la ventana que elijas. **Nunca pulsa Enter**: enviar siempre lo decides tú.
- **Visión en tiempo real local** (sección 8.4, opt-in): cámara y pantalla procesadas en tu PC, sin
  tokens y sin guardar ni enviar imágenes.
- **Look de Kurisu/Amadeus** para el avatar VRM (tema `amadeus` de VTuber AI Studio).
- **IA de respaldo con Puter** (sección 4): si Gemini se queda sin cuota, sin red o sin clave,
  Amadeus sigue conversando con [Puter](https://developer.puter.com) desde Amadeus Desktop, con
  **tu** cuenta de Puter y sin ninguna clave de API.
- Todo sigue siendo **No-LLM**: cuándo aparece, cuándo te busca, qué app abre o cuándo hace una
  travesura lo deciden el conectoma y reglas deterministas; Gemini (opcional) solo pone las palabras.
  **No hace falta ninguna clave de API nueva.**

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
| — | `soundfile` (añadido) | Decodifica el MP3 de edge-tts sin `ffmpeg` (la wheel trae libsndfile con MP3): boca real del avatar también en Windows. Si falta, se usa `mpv`/`ffplay`. |
| `audioop` | Nada (no se usa) | `audioop` desapareció en Python 3.13: el RMS del *lip-sync* se calcula con numpy o con `array` + `math`. |

**No uses dos variantes de OpenCV a la vez** (`opencv-python`, `opencv-python-headless`,
`opencv-contrib-python`…): se pisan entre sí. Si ya tenías otra, desinstálala antes
(`pip uninstall opencv-python`).

## 4. Claves, voz y servicios en línea

Todas las claves son **opcionales**. Sin ninguna, Amadeus funciona igual: Broca en modo no verbal
(banco local de frases; o con Puter, que no usa clave, si conectas tu cuenta en Amadeus Desktop),
voz por edge-tts (servicio en línea, ver abajo), sin Telegram y con búsqueda por DuckDuckGo. Con `--offline` no sale nada a la red. Las claves van en `.env`, una por
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
en el log) y Amadeus sigue con Puter si lo conectaste (abajo) o, si no, en modo no verbal. Con
`--offline` nunca se llama a Gemini. Qué datos recibe Gemini está en la sección 11 (Privacidad).

### Puter (IA de respaldo, sin clave de API)

[Puter](https://developer.puter.com/tutorials/free-unlimited-ai-api/) da acceso a cientos de modelos
de IA (Gemini, Claude, GPT, Grok…) desde el navegador **sin clave de API**: inicias sesión con tu
cuenta de Puter y el uso **se descuenta de tu saldo de Puter** (modelo *user-pays*: R.A.M.S. no
guarda ninguna credencial de Puter ni paga nada por ti). Como es una biblioteca de navegador, corre
**dentro de Amadeus Desktop** (sección 8.2), en una ventana oculta, y R.A.M.S. le pide el texto por
el enlace `/escritorio`. Es el **respaldo del Área de Broca**: Gemini → Puter → banco local de frases.
Puter tampoco decide nada: solo pone en palabras lo que ya decidió el conectoma.

**Conectar tu cuenta** (una vez; la sesión se recuerda entre arranques):

1. Arranca R.A.M.S. y Amadeus Desktop (sección 8.2).
2. Bandeja de Amadeus Desktop → **«Conectar Puter (IA de respaldo)…»** → pulsa el botón de la
   ventanita e inicia sesión en puter.com (o crea una cuenta) en la ventana que se abre. En Hyprland,
   lo mismo con `amadeus-desktop --amadeus=puter`.
3. Comprueba con `/estado`: el módulo `Respaldo Puter (Amadeus Desktop)` pasa a **OK** con el
   modelo y aparece la línea `IA de respaldo (Puter): lista · si Gemini no puede · hoy N de 300
   pedidos, …`. En el arranque de R.A.M.S. el módulo sale **EN ESPERA** hasta que la app se conecta;
   es normal. Para cerrar la sesión: bandeja → **«Desconectar Puter»**.

Sin la app abierta o sin sesión, el diagnóstico queda **EN ESPERA** y Broca sigue con Gemini y el
banco local: nada se bloquea. El detalle dice qué falta: «sin app de escritorio», «sin sesión de
Puter» (conéctala desde la bandeja), «sin conexión con puter.com» (la app no pudo cargar Puter:
revisa la red; lo reintenta sola), «ventana de Puter caída (se recarga sola)» o «Puter desactivado
en Amadeus Desktop (AMADEUS_PUTER=0)». Si tu saldo de Puter se agota, Amadeus deja de pedirle
durante 10 minutos (diagnóstico **DEGRADADO**, «saldo de Puter agotado · pausa de 10 min»; en
`/estado`, `IA de respaldo (Puter): en pausa (saldo de Puter agotado, reintento en N min)`) y luego
vuelve a probar.

| Variable (`.env` de R.A.M.S.) | Valor por defecto | Qué hace |
|---|---|---|
| `RAMS_PUTER` | `1` | `0` = no usar nunca Puter. |
| `PUTER_MODEL` | *(vacío)* | Modelo de Puter con su nombre exacto de [docs.puter.com](https://docs.puter.com/AI/chat/) (p. ej. `google/gemini-3.8-flash` o `anthropic/claude-haiku-5-5`). Vacío = el modelo por defecto de Amadeus Desktop. |
| `RAMS_PUTER_PRIMERO` | `0` | `1` = Puter **antes** que Gemini (Gemini pasa a ser el respaldo). Con `0`, Puter solo entra cuando Gemini no puede (cuota 429, red, `GEMINI_MAX_CALLS_DAY`, sin clave). |
| `PUTER_MAX_CALLS_DAY` | `300` | Tope diario propio de pedidos a Puter (cuentan aparte de `GEMINI_MAX_CALLS_DAY`), porque los pagas tú. `0` = no usar Puter. |

En el entorno con que lanzas Amadeus Desktop (no lee el `.env`): `AMADEUS_PUTER=0` lo apaga del
lado de la app y `AMADEUS_PUTER_MODELO` cambia su modelo por defecto (ver `desktop/README.md`). El
modelo por defecto de la app es `google/gemini-3.5-flash-lite` (verificado en el listado oficial de
modelos de Puter): el más rápido de los candidatos, bueno en español y de la misma familia para la
que está afinado el prompt de Amadeus. Con `--offline` Puter no se usa.

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

edge-tts produce MP3; para mover la boca del avatar se decodifica a PCM con `ffmpeg` o, sin
`ffmpeg`, con `soundfile` (lo instala `requirements.txt`; su wheel trae libsndfile con MP3). Si
ninguno sirve, el MP3 se reproduce con `mpv` o `ffplay` y la boca se simula.

### Reproducción de audio por plataforma

| Sistema | ElevenLabs (PCM) | edge-tts (MP3) |
|---|---|---|
| **Arch Linux** | `pw-play` (PipeWire), o `paplay`, o `aplay`: `sudo pacman -S pipewire` | Lo mismo + `ffmpeg` (`sudo pacman -S ffmpeg`); o `mpv` (boca simulada) |
| **Windows** | `sounddevice` (lo instala `requirements.txt`) | `sounddevice` + `soundfile` (los dos los instala `requirements.txt`; **no hace falta `ffmpeg`**). Con `ffmpeg` en el `PATH` se usa en streaming; sin `soundfile` ni `ffmpeg`, `mpv` o `ffplay` en el `PATH` (si no hay ninguno, solo texto) |

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
| `/vision on\|off\|pausa [min]\|estado` | Visión continua local (cámara; opt-in, sección 8.4) |
| `/pantalla off\|metadatos\|pixeles` | Qué mira la visión de la pantalla |
| `/travesuras on\|off` | Travesuras con el cursor real (opt-in, sección 8.5) |

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

Frases nuevas de la v0.2 (también por el chat): «abre WhatsApp», «busca lofi en YouTube», «pon
Despacito en Spotify», «escríbele a mamá por WhatsApp: llego tarde», «pon en X: …», «escribe ahí:
…», «escríbeme un tweet sobre…» y luego «ponlo en X», «búscame en 10 minutos», «llámame a las 6 pm
para revisar el horno», «ven», «vete», «basta» / «quieta» / «deja el mouse», «deja de mirarme» /
«no me mires por 20 minutos». Las frases cortas («ven», «basta»…) solo cuentan si son el mensaje
completo.

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

Para las **travesuras** (sección 8.5) Amadeus necesita saber si dejaste quietos el teclado y el
ratón unos segundos. Añade este segundo listener (archivo-bandera `rams_quieto`):

```ini
listener {
    timeout = 5                                   # 5 s sin teclado ni ratón ⇒ "quieto"
    on-timeout = touch $XDG_RUNTIME_DIR/rams_quieto
    on-resume = rm -f $XDG_RUNTIME_DIR/rams_quieto
}
```

En Hyprland no se puede leer el estado de los botones del ratón, así que las travesuras con el
**cursor real** quedan deshabilitadas allí (Amadeus solo persigue el cursor con la mirada y el
cuerpo); la bandera sirve para la presencia fina y para los vetos.

### Amadeus Desktop en Hyprland

Amadeus Desktop crea dos ventanas: el overlay transparente (título `Amadeus Overlay`), que debe
cubrir el monitor, y el chat (título `Amadeus Chat`). Ninguna debe robar el foco ni seguir al
ratón. Las reglas completas están en `desktop/hyprland.ejemplo.lua` (Hyprland 0.55+, configuración
en Lua) y `desktop/hyprland.ejemplo.conf` (0.53–0.54, hyprlang); cópialas de ahí. El antiguo
`windowrulev2 = …` (< 0.53) ya no es la sintaxis vigente. Extracto de la versión hyprlang:

```ini
# Overlay: flotante, en todos los workspaces, cubriendo el monitor y sin robar el foco.
windowrule = match:title ^(Amadeus Overlay)$, float on, pin on, move 0 0, size monitor_w monitor_h, no_initial_focus on, no_follow_mouse on, focus_on_activate off, no_blur on, no_shadow on, no_dim on, no_anim on, border_size 0, rounding 0, decorate off
# Chat: flotante abajo a la derecha; R.A.M.S. puede abrirlo sin foco.
windowrule = match:title ^(Amadeus Chat)$, float on, pin on, size 380 560, move (monitor_w-window_w-24) (monitor_h-window_h-24), no_initial_focus on, no_follow_mouse on, focus_on_activate off, no_shadow on, border_size 0
# Atajos (los mismos que en Windows; en Wayland Amadeus Desktop no registra atajos globales).
bind = SUPER, A, exec, amadeus-desktop --amadeus=toggle
bind = CTRL ALT, A, exec, amadeus-desktop --amadeus=toggle
bind = CTRL ALT, SPACE, exec, amadeus-desktop --amadeus=chat
bind = CTRL ALT SHIFT, Q, exec, amadeus-desktop --amadeus=basta
bind = CTRL ALT, V, exec, amadeus-desktop --amadeus=vision_pausa
bind = CTRL ALT SHIFT, A, exec, amadeus-desktop --amadeus=clics
exec-once = amadeus-desktop --hidden
```

`amadeus-desktop` es el lanzador de `desktop/`; ponlo en el PATH con
`ln -s ~/proyectos/rams_biocore/desktop/scripts/amadeus-desktop.sh ~/.local/bin/amadeus-desktop`
(ajusta la ruta a tu copia). En Wayland sin Hyprland, si la transparencia falla, usa
`npm run start:x11`.

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
   mirar a otro lado…) y el habla con labios (sin globo de texto; `VTUBER_AI_BURBUJA=1` lo
   vuelve a mostrar). La prosodia sale de `[TONE]`. En reposo, la cara sigue a la homeostasis
   del conectoma.
4. Si R.A.M.S. no tiene voz propia, la pone el navegador (`VTUBER_AI_TTS=1`). Con ElevenLabs
   activo, el avatar solo mueve los labios y la voz sale de R.A.M.S.

Si el servidor usa otro puerto: `VTUBER_AI_URL=http://127.0.0.1:3940`.

### 8.2 Amadeus Desktop: Amadeus en tu escritorio

Una app de escritorio (Electron) que se queda en segundo plano, en la bandeja, y deja que Amadeus
salga a caminar por tu pantalla. Requiere **Node ≥ 22**.

1. **Instalación** (una vez): `cd desktop && npm i` (descarga Electron 44.8.0, ~158 MB).
2. **Orden de arranque:**
   1. VTuber AI Studio: `cd vtuber-ai && node server.js` (el avatar; ver 8.1).
   2. R.A.M.S.: `python main.py`. En el arranque verás `Amadeus Desktop (/escritorio) ... OK`: el
      HUD abre el endpoint `ws://127.0.0.1:<puerto>/escritorio` y escribe un token privado nuevo en
      `%LOCALAPPDATA%\rams\escritorio.json` (Windows) o `$XDG_RUNTIME_DIR/rams/escritorio.json`
      (Linux), permisos 600. Nunca escucha fuera de `127.0.0.1`.
   3. Amadeus Desktop: `cd desktop && npm start`. Se queda en la **bandeja** y no roba el foco;
      sin más opciones, en cuanto carga el visor Amadeus entra caminando una vez para que veas que
      funciona. Para arrancarla **escondida** (p. ej. al iniciar sesión): `npm start -- --hidden`,
      o `AMADEUS_AUTOARRANQUE=1` (Windows) para que Windows la lance así sola en cada inicio.

   El orden no es estricto: la app reintenta el visor cada 5 s y a R.A.M.S. con espera creciente.

   **`.env` para la v0.2:** no hace falta ninguna clave nueva. Si tu `.env` es anterior a esta
   versión, todo lo nuevo funciona con los valores por defecto (chat y escritorio activos); lo
   opcional se enciende añadiendo estas líneas (cada una en su propia línea):

   | Quieres… | Añade a `.env` |
   |---|---|
   | Que te vea por la cámara de forma continua (local, sin tokens) | `RAMS_CAMERA=1` y `RAMS_VISION_CONTINUA=1` |
   | Que además sepa qué app tienes delante | `RAMS_PANTALLA_MODO=metadatos` (o `pixeles` tras `pip install mss==10.2.0`) |
   | Que moleste con el mouse (empujoncitos del cursor real, solo Windows) | `RAMS_TRAVESURAS=1` |
   | Sinónimos propios para abrir apps | `RAMS_DESTINOS=wasap=whatsapp,pajarito=x` |
   | Que las respuestas del chat también se digan en voz alta | `RAMS_ESCRITORIO_VOZ=1` |
   | Quitar el look de Kurisu (licencia del modelo) | `VTUBER_AI_TEMA=none` (y lo mismo en el entorno de Amadeus Desktop) |
   | IA de respaldo cuando Gemini no puede (tu cuenta de Puter, sin clave) | nada: viene activada (`RAMS_PUTER=1`); conecta tu cuenta desde la bandeja (sección 4) |
   | Que nunca use Puter | `RAMS_PUTER=0` |

   La lista completa, comentada, está en `.env.example` (secciones «Amadeus Desktop / escritorio»,
   «Control del PC y travesuras» y «Visión continua»). Gemini sigue siendo opcional: sin
   `GEMINI_API_KEY` responde con el banco local de frases (sin borradores de tweets ni charla libre).
3. **Llamarla:** clic en la bandeja («Llamar a Amadeus»), `Ctrl+Alt+A`, o escribe «ven» /
   «aparece» / «sal» en el chat. Entra caminando desde un borde. «vete» o «escóndete» la devuelven
   a la bandeja.
4. **Ella sale sola** (sin LLM): cuando el conectoma dispara una iniciativa (aburrimiento,
   descanso, cambio de línea, curiosidad) y estás frente al computador, Amadeus se asoma por un
   borde y te busca; si no estás, te llama por Telegram como en la v0.1. «Búscame en 10 minutos»
   (solo desde el computador; por Telegram usa «recuérdame en …») programa que vaya a buscarte a
   esa hora. Con «no molestar» (bandeja o chat) no aparece ni
   interrumpe; tampoco durante una presentación, un juego o una app a pantalla completa
   ("no molestar" del sistema, que se comprueba cada 5 s).
5. **Chat:** clic derecho sobre Amadeus o `Ctrl+Alt+Space` abre la ventana de chat (también hay
   un panel de chat estilo RINE en el HUD, con `RAMS_HUD_CHAT=1`, el defecto). Las respuestas
   llegan por texto (labios mudos); con `RAMS_ESCRITORIO_VOZ=1` siguen la regla de voz normal. La
   salida de los comandos (`/estado`, `/ayuda`…) vuelve al chat. `/hud` nunca muestra el enlace por
   el chat: solo en la terminal.
6. **Confirmaciones:** si una acción necesita tu visto bueno (un borrador, escribir en otra ventana)
   aparecen **botones** en el chat con la vista previa del texto. Escribir «sí» en el chat **no**
   aprueba nada (el mensaje queda en cola y se lee después); la terminal puede responder igual que
   en la v0.1. Cerrar ventanas solo se confirma en la terminal.
7. **Bandeja:** Llamar a Amadeus · Abrir chat · No molestar · Quieta (sin travesuras) · Pausar
   visión · Dejar pasar clics · Conectar Puter (IA de respaldo)… / Desconectar Puter (sección 4) ·
   Salir.
8. **Atajos y emergencias:**

   | Atajo | Qué hace |
   |---|---|
   | `Ctrl+Alt+A` | Llamar / esconder a Amadeus |
   | `Ctrl+Alt+Space` | Abrir el chat (con foco) |
   | `Ctrl+Alt+Shift+Q` | **Quieta**: corta las travesuras y la esconde (igual que «basta» en el chat) |
   | `Ctrl+Alt+V` | Pausar la cámara (igual que «deja de mirarme») |
   | `Ctrl+Alt+Shift+A` | Emergencia: oculta el overlay y deja pasar todos los clics (no necesita a R.A.M.S.) |

   «Quieta» lleva Shift porque en Windows `Ctrl+Alt` es AltGr y, en el teclado latinoamericano,
   AltGr+Q escribe «@». Los atajos se cambian con `AMADEUS_ATAJO_*` (ver `desktop/README.md`).

   En Hyprland, los mismos comandos por CLI: `amadeus-desktop --amadeus=toggle|show|hide|chat|basta|vision_pausa|clics|puter|quit`
   (sección 7; `puter` abre la ventanita «Conectar Puter»).
9. **Capturas de pantalla:** con `AMADEUS_EXCLUIR_CAPTURA=1` (defecto) el overlay **no** sale en
   capturas, OBS ni Discord. Pon `0` si quieres capturarla.
10. **Look de Kurisu:** el tema `amadeus` de VTuber AI Studio recolorea el modelo VRM a la paleta de
    Kurisu (cabello castaño rojizo, chaqueta, corbata roja, medias oscuras) y, fuera del escritorio,
    añade el marco de videollamada holográfica. Pon tu propio `.vrm` en `vtuber-ai/models/` (VRoid
    Hub o VRoid Studio exportando VRM 1.0 **sin reducir materiales**; ver `vtuber-ai/models/README.md`).
    `VTUBER_AI_TEMA=none` desactiva el tema. Homenaje de fan: sin assets oficiales.

### 8.3 Control de apps y redes (sin enviar nunca nada)

Pídele «abre WhatsApp», «busca lofi en YouTube» o «escríbele a mamá por WhatsApp: llego tarde».
Las reglas, en orden de seguridad:

1. **Abrir** una app o web del **catálogo cerrado** (no hay comandos libres): `whatsapp`,
   `telegram`, `x`, `gmail`, `correo`, `instagram`, `tiktok`, `facebook`, `discord`, `linkedin`,
   `youtube`, `spotify`, `navegador` y `archivos`. Buscar dentro: YouTube, Spotify, X y el navegador
   (DuckDuckGo). Sinónimos de serie: «twitter», «wasap», «insta», «yt», «mail»…; añade los tuyos con
   `RAMS_DESTINOS=wasap=whatsapp,pajarito=x` (cada sinónimo `^[a-z0-9_]{1,24}$` y siempre hacia una
   clave del catálogo). Si el alias no está en el catálogo, en Linux se usa `RAMS_APPS` (sección 3).
   **En Windows (v0.2.1)** abre además las **apps instaladas** por el nombre que tienen en el menú
   Inicio («abre Steam», «Amadeus, ¿me abres el bloc de notas?», «oye, abre Epic Games porfa»):
   entiende que la llames por su nombre y las cortesías («porfa», «pls», «por favor»). La primera
   vez te pide confirmación (botón del chat o `s` en la terminal) y, si aceptas, la recuerda en
   `data/apps_aprobadas.json`: las siguientes la abre directo. Los desinstaladores, instaladores y
   accesos a páginas web del menú nunca se abren; las consolas y herramientas del sistema
   (PowerShell, CMD, WSL, Editor del Registro…) piden confirmación cada vez y nunca se recuerdan.
   Los nombres del catálogo (YouTube, Discord, Spotify…) siguen abriendo su web.
   `RAMS_APPS_MENU_INICIO=0` lo desactiva.
2. **Borrador prerrellenado** (WhatsApp, Telegram, X, Gmail, correo): Amadeus abre la app con el
   texto ya escrito y **tú pulsas Enviar**. Siempre pide confirmación con la vista previa.
3. **Teclear** donde no hay prerrelleno (Instagram, TikTok, Discord…): «escribe ahí: …». El
   objetivo es la última ventana en primer plano que no es de Amadeus. Tras confirmar, Amadeus te
   dice «haz clic en la caja de texto de <app>» y escribe en cuanto esa ventana tenga el foco (si no
   en 10 s, no escribe nada). **Nunca pulsa Enter.** Si mueves el teclado durante el tecleo, aborta.
4. **Borradores de Amadeus:** «escríbeme un tweet sobre la mosca de la fruta» (con Gemini) y luego
   «ponlo en X» o «pásalo a WhatsApp»: usa el último borrador que te escribió **en este computador**
   (≤ 10 min, ≤ 500 caracteres; nunca uno de Nexus, Telegram, una iniciativa ni una respuesta con
   resultados de la web, de la cámara o de una captura). Sin Gemini no hay borradores: escribe tú el
   texto con «pon en X: …» o «escríbele a mamá por WhatsApp: …».
5. **Anti-contraseñas:** nunca escribe textos que parezcan una contraseña, un PIN/OTP, una tarjeta
   o una clave, ni en ventanas de inicio de sesión, banca o pago. Topes: borradores ≤ 1 cada 20 s,
   tecleo ≤ 1 cada 30 s.
6. **Auditoría:** `data/acciones_pc.jsonl` (permisos 600) registra cada acción con su veredicto,
   el destino y la **longitud y el hash** del texto (nunca el texto ni títulos completos).

Windows no necesita nada más. En Arch: `sudo pacman -S wtype xdg-utils gtk3`.

### 8.4 Visión en tiempo real, local y sin tokens (opt-in, OFF por defecto)

Con `RAMS_CAMERA=1` **y** `RAMS_VISION_CONTINUA=1`, Amadeus mira por la cámara de forma continua
(2–5 fps) y, si quieres, la pantalla (`RAMS_PANTALLA_MODO=metadatos` o `pixeles`, 0,2–2 fps). Todo
se procesa en tu PC: **ninguna imagen se guarda ni se envía** (tampoco a Gemini). Lo que Amadeus
"ve" son números: si estás, cuántas caras hay, a qué distancia, si miras la pantalla, qué app
tienes delante y su título (salvo títulos privados: banca, contraseñas, navegación privada…), si hay
vídeo o pantalla completa. Con eso comenta lo que haces («¿Otra vez YouTube?») y sabe cuándo no
molestar.

- **Instalación:** el detector YuNet (`face_detection_yunet_2023mar.onnx`, 232 KB, MIT) en
  `data/models/` (sin él usa Haar); en Windows `pip install mss==10.2.0` para la pantalla.
- **Calibración:** `RAMS_VISION_HFOV` (ángulo de tu webcam), `RAMS_VISION_ESPEJO=1` si da la imagen
  en espejo. En Windows 11, para videollamadas con la cámara continua encendida, activa "Permitir que
  varias apps usen la cámara"; si no, se pausa sola con Zoom, Teams, Discord u OBS.
- **Indicador permanente:** `● CÁMARA ACTIVA` en la terminal, LED y ojo en el HUD y en el overlay.
- **Pausar:** «deja de mirarme» / «no me mires por 20 minutos» / «cierra los ojos», `/vision pausa
  [min]`, `/camara off`, el botón Cámara del HUD, `Ctrl+Alt+V` o la bandeja. Se pausa sola si
  bloqueas la sesión, si te vas, con apps de llamada, con batería baja o en juegos a pantalla
  completa. No se activa nunca desde Telegram.

### 8.5 Travesuras con el mouse (opt-in, OFF por defecto)

Si está aburrida, Amadeus puede dar un pequeño empujón a tu cursor (como mucho
`RAMS_TRAVESURA_AMPLITUD` px, 40 por defecto, ≤ 0,6 s). Solo con `RAMS_TRAVESURAS=1` y en Windows
(en Hyprland solo persigue el cursor con la mirada). Nunca hace clic, nunca usa la rueda ni
teclas, y no lo intenta si estás escribiendo, en una videollamada, viendo un vídeo, con una
confirmación pendiente, mientras habla o en "no molestar". Si mueves el ratón durante la
travesura, aborta y se apaga 2 h. Topes: ≤ 1 cada 15 min y ≤ 6 al día.

**«basta»** (o «quieta», «deja el mouse», «para ya», `Ctrl+Alt+Shift+Q`, bandeja "Quieta"): el primero
del día las apaga 2 h; el segundo, hasta mañana. `/travesuras off` las apaga hasta `/travesuras on`.
El estado se guarda en `data/travesuras.json`.

### 8.6 Correo, Drive y scripts (v0.2.2, opt-in, OFF por defecto)

Amadeus puede **enviar correos desde tu Gmail**, **usar tu Google Drive** y, cuando le pides algo que
no sabe hacer, **escribirte un script** para hacerlo. Nada de eso pasa sin ti: todo lo que envía, sube,
comparte o ejecuta se muestra antes en una **tarjeta** en el chat (Amadeus Desktop o el chat del HUD)
con el contenido completo, y solo ocurre cuando **tú pulsas el botón** (Enviar, Subir, Compartir,
Ejecutar…). Ni el texto del chat, ni un «sí» en la terminal, ni Telegram, ni Gemini pueden aprobar una
tarjeta. Si no hay ningún chat abierto, Amadeus te pide que abras uno.

**Cómo activarlo**

1. **Google (correo y Drive):** crea tu propio cliente OAuth de «App de escritorio» en Google Cloud,
   guarda el JSON como `data/google_cliente.json`, pon `RAMS_GOOGLE=1` en tu `.env` y reinicia. La guía
   paso a paso está en `docs/V022_CORREO_DRIVE_SCRIPTS.md` §17. Después dile **«conecta mi cuenta de
   Google»**: se abre tu navegador; como es tu propia app, Google avisa de que no está verificada
   (Configuración avanzada → Ir a Amadeus) y marcas las casillas de correo y Drive. Permisos mínimos:
   Gmail **solo enviar** (nunca lee tu bandeja) y Drive **leer todo + modificar solo lo que cree
   Amadeus** (`RAMS_DRIVE_COMPLETO=1` para modificar cualquier archivo; hay que reconectar).
   «desconecta google» revoca el permiso y borra el token local (con confirmación).
2. **Contactos (opcional):** `RAMS_CORREO_CONTACTOS=mama=mama@gmail.com,jefe=jefe@empresa.com` para
   escribir «mándale un correo a mamá …». Límites: 10 destinatarios por correo, 5 correos cada 10 min
   y 30 al día (ajustables en el `.env`).
3. **Taller de scripts:** `RAMS_TALLER=1` (Windows o Linux). Los scripts los escribe Gemini (hace
   falta su clave); sin Gemini solo funcionan las habilidades que ya guardaste.

**Frases de ejemplo**

| Quieres | Dile |
|---|---|
| Enviar un correo | «mándale un correo a mamá diciendo que llegué bien», «envía un email a juan@empresa.com con asunto Reunión: mañana no puedo» |
| Que Amadeus lo redacte | «redacta un correo a mi jefe pidiendo vacaciones la semana del 3 de noviembre» (a Gemini solo le llega el tema y el alias, nunca la dirección) |
| Adjuntar | «… adjunta informe.pdf: aquí va el informe» (se busca por nombre en Descargas, Documentos y Escritorio) |
| Buscar en Drive | «busca en mi drive el informe de ventas», «¿qué tengo en drive sobre impuestos?», «lista mi drive» |
| Detalles, descargar, resumir | «detalles del 2», «descarga el 2 como pdf en documentos», «resume el documento 1 de drive» |
| Carpetas y subir | «crea una carpeta Facturas 2026 en drive», «sube informe.pdf a drive» (con tarjeta) |
| Papelera y compartir | «manda a la papelera el 3» (nunca borra para siempre), «comparte el 2 con ana@x.com como editora», «haz público el 2» (dos confirmaciones y siempre de solo lectura) |
| Un script | «hazme un script que renombre las fotos de Descargas con la fecha», `/script …` |
| Habilidades | «¿qué habilidades tienes?», «ejecuta la habilidad renombrar fotos», «olvida la habilidad …», `/habilidades` |
| Estado de Google | «¿está conectado google?», `/google` |

**Cómo se usa cada cosa**

- **Correo:** la tarjeta muestra Para, CC, CCO, asunto, cuerpo y adjuntos; puedes **Editar** y
  **Guardar cambios** antes de pulsar **Enviar**. Si el texto parece una contraseña o un código (o
  una palabra de 24+ caracteres, como un enlace largo), no lo envía hasta que lo cambies. Si el envío
  falla, la tarjeta se reabre para reintentar con otro clic; si la red falló a mitad, te avisa de que
  revises Enviados antes de reintentar. Nunca reintenta solo.
- **Desde Telegram** solo se **prepara** el correo (`RAMS_TELEGRAM_CORREO=1`, por defecto): queda la
  tarjeta en el computador para que la apruebes allí. Buscar en Drive por Telegram exige
  `RAMS_TELEGRAM_DRIVE=1` (apagado: los nombres de tus archivos pasarían por Telegram). Scripts por
  Telegram, nunca.
- **Scripts:** un analizador local (no Gemini) clasifica el riesgo (bajo, medio, alto, crítico) y la
  tarjeta muestra el **código completo**. Solo corre al pulsar **Ejecutar**, sin permisos de
  administrador, con tiempo límite y en su propia carpeta (`data/scripts/`). Los de riesgo crítico no
  se pueden ejecutar desde la app (puedes copiarlos). La salida aparece en otra tarjeta y nunca va a
  Gemini ni a Telegram. «Guardar como habilidad» lo deja listo para reutilizarlo (siempre con tarjeta;
  si el código cambió, se revisa de nuevo).
- **Cuando no sabe hacer algo** de tu PC («organiza mis descargas por tipo»), Amadeus te **ofrece**
  escribir un script; contesta «sí» o «dale» y te deja la tarjeta.

Seguridad y privacidad en detalle: `docs/V022_CORREO_DRIVE_SCRIPTS.md` §15 (y §16 para lo que pasa
sin red, sin Gemini o con permisos parciales).

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
Desde la v0.2.1, además:

- **Abrir cosas en el computador** (`RAMS_TELEGRAM_ACCIONES=1`, por defecto): webs del catálogo
  («puedes abrir YouTube en el PC»), buscar dentro de ellas («busca lofi en YouTube»), pausar o
  pasar la música y abrir apps del menú Inicio **ya aprobadas** desde el computador. Por Telegram
  no hay botón de confirmar: una app nueva se aprueba abriéndola una vez en el PC. Todo lo que
  pidas así se anuncia en el PC («📱 Desde Telegram: …», en el chat y como notificación).
- **Fotos** (`RAMS_TELEGRAM_FOTOS=1`, apagado por defecto): «mándame una foto» (cámara; exige
  `RAMS_CAMERA=1` y respeta el «deja de mirarme») y «mándame captura» (la pantalla donde está la
  ventana activa; se cancela si en ella se ve una ventana de bancos, pagos o contraseñas, aunque
  no tenga el foco). Como mucho 10 por hora; en el PC queda el aviso «📸 …» y la imagen solo vive
  en memoria (nunca se guarda en disco ni va a Gemini).
- **Notas de voz:** las que le mandas se transcriben con Gemini (`RAMS_TELEGRAM_TRANSCRIBIR=1`,
  por defecto; ≤ 3 min) y te responde con una nota de voz cuando le mandas una o se la pides
  («mándame un audio»): `RAMS_TELEGRAM_AUDIO=auto` (defecto), `siempre` u `off`. La voz sale de
  ElevenLabs o edge-tts, así que necesita `RAMS_VOICE=1`.

**Qué no:** teclear o dejar borradores en otras apps, cerrar ventanas, subir o bajar el volumen,
mover el avatar, mirarte por la cámara para comentarlo, `/camara`, `/olvidar`, `/salir` y `/hud`.
Amadeus responde «Eso solo se puede pedir desde el computador.».

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
- **Visión continua (v0.2):** opt-in de dos niveles (`RAMS_CAMERA=1` y `RAMS_VISION_CONTINUA=1`),
  procesada en local, sin guardar ni enviar imágenes; a Gemini solo llegan la app y el título corto
  de la ventana (si no es privada). Ver la sección 8.4.
- **Chat de escritorio (v0.2):** el texto del chat, y lo que se saca de él (consultas, recuerdos,
  recordatorios, `/recordar`, `/logro`), se redacta (claves, tokens y cadenas largas tipo clave)
  antes de llegar a la memoria, la agenda, el bus, la búsqueda web y Gemini; nunca se registra en
  los logs. Las respuestas del chat, incluidas sus confirmaciones, no se dicen en voz alta salvo
  con `RAMS_ESCRITORIO_VOZ=1`. `/escritorio` exige un token de
  archivo privado que cambia en cada arranque.
- **Secretos:** solo en `.env` (nunca en git; `chmod 600 .env`). Los logs, el bus y el HUD pasan
  por un filtro que redacta claves y tokens (también en trazas de excepciones). Los directorios de
  datos se crean con permisos 700 y los archivos sensibles (memoria, perfil, tokens, log) con 600.
- **Compuerta de permisos:** toda acción sobre el PC nace del enrutador local (nunca del texto del
  LLM ni de Telegram) y pasa por una lista permitida. Cerrar una ventana **siempre pide
  confirmación** en la terminal, con la ventana exacta fijada de antemano, y R.A.M.S. **nunca
  cierra su propia terminal**. No se usa `shell=True` en ningún sitio.
- **Control del PC (v0.2):** abrir, buscar, prerrellenar y teclear pasan por la misma compuerta;
  prerrellenar y teclear **siempre** se confirman con la vista previa del texto y nunca se pulsa
  Enter. Ver la sección 8.3.
- **Red:** el HUD y el socket de control escuchan solo en `127.0.0.1`, con token.
- **Qué sale de tu PC** (solo con red y con la clave correspondiente; `--offline` lo corta todo):
  - **Gemini:** tu mensaje, los turnos recientes, los recuerdos relevantes, el estado del
    conectoma, la telemetría o los resultados web del turno y, con `RAMS_PERFIL_EN_PROMPT=1`, el
    resumen de tu perfil de WhatsApp (nunca el chat crudo). Si pides «mira mi pantalla», la
    captura de pantalla de ese turno. Nunca fotogramas de la cámara. Con
    `RAMS_TELEGRAM_TRANSCRIBIR=1`, el audio de las notas de voz que le mandas por Telegram (solo
    para transcribirlas; no se guarda en disco ni en los logs).
  - **Puter** (solo si conectaste tu cuenta en Amadeus Desktop y Gemini no pudo, o con
    `RAMS_PUTER_PRIMERO=1`): lo mismo que Gemini en ese turno (system prompt, turnos recientes,
    recuerdos, contexto y, con `RAMS_PERFIL_EN_PROMPT=1`, el resumen del perfil), que Puter pasa al
    proveedor del modelo elegido. **Nunca** imágenes: la captura de «mira mi pantalla» no viaja a
    Puter. Los prompts y las respuestas no se registran en los logs, y el token de `/escritorio`
    nunca llega a la ventana de Puter. `RAMS_PUTER=0` o `PUTER_MAX_CALLS_DAY=0` lo cortan.
  - **ElevenLabs** o, sin su clave, **edge-tts (servicio en línea de Microsoft)**: el texto que
    Amadeus va a decir en voz alta. Con `RAMS_VOICE=1` (defecto) y sin `ELEVENLABS_API_KEY`, la voz
    usa edge-tts; `RAMS_VOICE=0` o `--sin-voz` lo evitan (ver la sección 4).
  - **Telegram:** los mensajes que intercambias con tu bot y las notas de voz; con
    `RAMS_TELEGRAM_FOTOS=1`, las fotos de la cámara y las capturas que **tú** le pidas.
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
pytest-asyncio). Tampoco tocan el PC: la *fixture* `sin_efectos_pc` hace fallar cualquier
apertura de apps, tecleo o movimiento del cursor reales.

Amadeus Desktop y VTuber AI Studio tienen sus propias pruebas (Node ≥ 22):

```bash
cd desktop && npm test && npm run smoke     # módulos puros + Electron oculto ("smoke OK")
cd ../../vtuber-ai && npm test               # servidor, protocolo, tema Amadeus y vendor local
```

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
