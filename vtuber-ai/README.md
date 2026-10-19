# VTuber AI Studio

Interfaz gráfica para que **una IA mueva libremente un avatar VTuber 3D** (formato VRM).
La IA puede hacerlo hablar (voz + movimiento de labios), cambiar expresiones, hacer gestos y poses,
rotar cualquier hueso, mover las manos y los dedos, caminar por el escenario, mirar a distintos puntos y cambiar la cámara.

- Sin dependencias: solo necesitas **Node.js 18+**.
- Funciona con **cualquier IA**: por HTTP (JSON), o por **MCP** para Claude (Desktop / Code).
- Listo para **OBS**: fondo transparente o chroma verde.

---

## 1. Arrancar

```bash
npm start
```

o haz doble clic en `start.bat` (Windows). Luego abre **http://localhost:3939**.

Se carga un modelo de ejemplo. Para usar el tuyo:
- arrastra un archivo `.vrm` a la ventana, o
- cópialo a la carpeta `models/` y elígelo en la pestaña **Escena**.

> ¿No tienes modelo? Créalo gratis con **VRoid Studio** y expórtalo como VRM.

## 2. Conectar una IA

### Opción A — Claude Code (MCP)

```bash
claude mcp add vtuber -- node "C:/ruta/a/vtuber-ai/mcp-server.js"
```

o, con el servidor ya corriendo, por HTTP:

```bash
claude mcp add --transport http vtuber http://localhost:3939/mcp
```

Después dile a Claude, por ejemplo: *"Saluda al chat con entusiasmo, baila un poco y luego despídete"*.

### Opción B — Claude Desktop (MCP)

Edita `%APPDATA%\Claude\claude_desktop_config.json` y añade:

```json
{
  "mcpServers": {
    "vtuber": {
      "command": "node",
      "args": ["C:/ruta/a/vtuber-ai/mcp-server.js"]
    }
  }
}
```

Reinicia Claude Desktop. Herramientas disponibles: `avatar_speak`, `avatar_expression`, `avatar_gesture`,
`avatar_pose`, `avatar_bones`, `avatar_hand`, `avatar_look`, `avatar_move`, `avatar_camera`,
`avatar_sequence`, `avatar_stop`, `avatar_get_state`, `avatar_command`.

### Opción C — Cualquier otra IA (ChatGPT, Gemini, LLM local, tu propio bot…)

Envía JSON a `POST http://localhost:3939/api/command`:

```bash
curl -X POST http://localhost:3939/api/command -H "Content-Type: application/json" -d '{"type":"speak","text":"¡Hola!","emotion":"happy","gesture":"wave"}'
```

En Python:

```python
import requests
requests.post("http://localhost:3939/api/command", json={
    "type": "sequence",
    "steps": [
        {"type": "speak", "text": "Déjame pensar…", "emotion": "relaxed"},
        {"type": "gesture", "name": "think"},
        {"type": "speak", "text": "¡Ya lo tengo!", "emotion": "happy", "gesture": "cheer"},
    ],
})
```

Truco: pásale a tu IA el contenido de `GET /api/help` como instrucciones del sistema; ahí está el protocolo completo.
La respuesta de cada comando dice si funcionó y, si no, por qué (por ejemplo, qué huesos existen).

## 3. Comandos

| type | Para qué | Ejemplo |
|---|---|---|
| `speak` | Hablar con voz, globo y labios | `{"type":"speak","text":"Hola","emotion":"happy","gesture":"wave"}` |
| `expression` | Cara: happy, angry, sad, relaxed, surprised, neutral | `{"type":"expression","name":"surprised","hold":2}` |
| `gesture` | wave, nod, shake_head, bow, tilt_head, jump, clap, shrug, cheer, point, dance, laugh, look_around, think, surprise | `{"type":"gesture","name":"dance","loop":true}` |
| `pose` | relaxed, tpose, apose, arms_up, hands_on_hips, arms_crossed, thinking, shy | `{"type":"pose","name":"hands_on_hips"}` |
| `bones` | Control libre de cualquier hueso (grados) | `{"type":"bones","rotations":{"head":{"y":20},"rightUpperArm":{"z":-30}}}` |
| `hand` | open, relaxed, fist, point, peace, thumbs_up | `{"type":"hand","side":"right","shape":"peace"}` |
| `look` | camera, mouse, left, right, up, down, point… | `{"type":"look","at":"mouse"}` |
| `move` | Caminar / girar por el escenario | `{"type":"move","x":0.8,"rotation":30}` |
| `camera` | face, bust, full, wide | `{"type":"camera","preset":"bust"}` |
| `background` | gradient, green, blue, magenta, transparent, #hex | `{"type":"background","mode":"green"}` |
| `sequence` | Varios pasos en orden | `{"type":"sequence","steps":[...]}` |
| `idle`, `stop`, `reset`, `wait`, `load_model`, `state` | Utilidades | `{"type":"stop","what":"all"}` |

Si envías una **lista** de comandos, se ejecutan **a la vez**; usa `sequence` para que vayan **en orden**.

**Convención de ejes** (vista desde el avatar): `x+` inclinar adelante / mirar abajo, `y+` girar hacia **su** izquierda,
`z+` ladear hacia **su** derecha. Brazos: `leftUpperArm z:-70` baja el brazo izquierdo, `rightUpperArm z:70` baja el derecho.

## 4. Usarlo en OBS

Añade una fuente **Navegador** con `http://localhost:3939/?obs=1` (oculta el panel, fondo transparente).
Otras opciones de URL: `?bg=green`, `?camera=face|bust|full|wide`, `?model=mi_modelo.vrm`, `?tts=1` (voz también en OBS).

Atajo: tecla **H** para ocultar o mostrar el panel.

## 5. Estructura

```
server.js        servidor local (HTTP + SSE + endpoint MCP), sin dependencias
mcp-server.js    servidor MCP por stdio para Claude
lib/protocol.js  nombres de poses, gestos y documentación de comandos (compartido)
lib/mcp.js       definición de herramientas MCP
public/          visor 3D (three.js + three-vrm) y panel de control
models/          pon aquí tus .vrm
```

## Notas

- La voz usa la síntesis del navegador (gratis, sin claves). El navegador puede pedir un clic en la página antes de permitir audio;
  mientras tanto el avatar mueve los labios en silencio.
- El servidor solo escucha en `127.0.0.1` y rechaza peticiones con otro `Host`.
- **2D (Live2D)**: este proyecto usa VRM 3D porque es un formato abierto. Live2D se puede añadir con `pixi-live2d-display`,
  pero requiere el *Cubism Core* de Live2D, que tiene su propia licencia.
