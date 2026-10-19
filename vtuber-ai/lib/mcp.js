// Lógica MCP compartida (JSON-RPC 2.0). La usan:
//   - mcp-server.js  (transporte stdio, para Claude Desktop / Claude Code)
//   - server.js      (endpoint POST /mcp, transporte Streamable HTTP)
import {
  POSES, GESTURES, HAND_SHAPES, EMOTIONS, LOOK_TARGETS, CAMERA_PRESETS, BACKGROUNDS, BONE_GUIDE, COMMAND_TYPES,
} from './protocol.js';

export const SERVER_INFO = { name: 'vtuber-ai-studio', version: '1.0.0' };

export const INSTRUCTIONS =
  'Controlas un avatar VTuber 3D (VRM) que se ve en un navegador. Puedes cambiar su expresión, pose, gestos, ' +
  'mirada, posición, cámara y hacerlo hablar. Para acciones encadenadas usa avatar_sequence. ' +
  'Llama a avatar_get_state si necesitas saber qué huesos o expresiones tiene el modelo. ' +
  'Actúa con naturalidad: combina speak con emotion y gesture para que el avatar parezca vivo.';

const num = (description, extra = {}) => ({ type: 'number', description, ...extra });
const str = (description, extra = {}) => ({ type: 'string', description, ...extra });

const commandItem = {
  type: 'object',
  description: `Un comando con campo "type" (${COMMAND_TYPES.join(', ')}) y sus parámetros.`,
  properties: { type: { type: 'string', enum: COMMAND_TYPES } },
  required: ['type'],
  additionalProperties: true,
};

export const TOOLS = [
  {
    name: 'avatar_get_state',
    description: 'Devuelve el estado del avatar: modelo cargado, expresiones y huesos disponibles, poses, gestos, posición, mirada y si está hablando.',
    inputSchema: { type: 'object', properties: {} },
    toCommand: null,
  },
  {
    name: 'avatar_speak',
    description: 'Hace que el avatar diga un texto en voz alta (TTS del navegador) con globo de texto y movimiento de boca. Opcionalmente con emoción y gesto a la vez.',
    inputSchema: {
      type: 'object',
      properties: {
        text: str('Lo que dirá el avatar.'),
        emotion: str(`Emoción mientras habla: ${EMOTIONS.join(', ')}, neutral.`),
        gesture: str(`Gesto mientras habla: ${Object.keys(GESTURES).join(', ')}.`),
        rate: num('Velocidad de voz 0.5-2 (1 = normal).'),
        pitch: num('Tono de voz 0-2 (1 = normal).'),
        lang: str('Idioma de la voz, p. ej. es-ES, es-MX, en-US.'),
      },
      required: ['text'],
    },
    toCommand: (a) => ({ type: 'speak', ...a }),
  },
  {
    name: 'avatar_expression',
    description: 'Cambia la expresión facial. Las emociones se reemplazan entre sí; usa "neutral" para quitarlas.',
    inputSchema: {
      type: 'object',
      properties: {
        name: str(`${EMOTIONS.join(', ')}, neutral, o una expresión propia del modelo (blink, aa, ou...).`),
        value: num('Intensidad 0-1 (por defecto 1).', { minimum: 0, maximum: 1 }),
        duration: num('Segundos de transición (por defecto 0.3).'),
        hold: num('Si se indica, vuelve a neutral tras estos segundos.'),
      },
      required: ['name'],
    },
    toCommand: (a) => ({ type: 'expression', ...a }),
  },
  {
    name: 'avatar_gesture',
    description: 'Reproduce un gesto animado que se mezcla con la pose actual.',
    inputSchema: {
      type: 'object',
      properties: {
        name: str('Gesto.', { enum: Object.keys(GESTURES) }),
        side: str('Mano para wave/point/tilt_head.', { enum: ['left', 'right'] }),
        duration: num('Segundos (opcional).'),
        loop: { type: 'boolean', description: 'Repetir hasta avatar_stop.' },
      },
      required: ['name'],
    },
    toCommand: (a) => ({ type: 'gesture', ...a }),
  },
  {
    name: 'avatar_pose',
    description: 'Aplica una pose corporal estática.',
    inputSchema: {
      type: 'object',
      properties: {
        name: str('Pose.', { enum: Object.keys(POSES) }),
        duration: num('Segundos de transición (por defecto 0.6).'),
      },
      required: ['name'],
    },
    toCommand: (a) => ({ type: 'pose', ...a }),
  },
  {
    name: 'avatar_bones',
    description: 'Control libre: rota huesos individuales en grados. ' + BONE_GUIDE,
    inputSchema: {
      type: 'object',
      properties: {
        rotations: {
          type: 'object',
          description: 'Mapa hueso -> {x, y, z} en grados. Ejes omitidos se conservan. Ej: {"head":{"y":25},"rightUpperArm":{"z":-30}}',
          additionalProperties: {
            type: 'object',
            properties: { x: { type: 'number' }, y: { type: 'number' }, z: { type: 'number' } },
          },
        },
        duration: num('Segundos de transición (por defecto 0.4).'),
        relative: { type: 'boolean', description: 'Sumar a la rotación actual en lugar de reemplazarla.' },
      },
      required: ['rotations'],
    },
    toCommand: (a) => ({ type: 'bones', ...a }),
  },
  {
    name: 'avatar_hand',
    description: 'Cambia la forma de una o ambas manos.',
    inputSchema: {
      type: 'object',
      properties: {
        side: str('Mano.', { enum: ['left', 'right', 'both'] }),
        shape: str('Forma.', { enum: HAND_SHAPES }),
      },
      required: ['shape'],
    },
    toCommand: (a) => ({ type: 'hand', ...a }),
  },
  {
    name: 'avatar_look',
    description: 'Dirige la mirada. Direcciones desde el punto de vista del avatar (left = su izquierda).',
    inputSchema: {
      type: 'object',
      properties: {
        at: str('Objetivo.', { enum: LOOK_TARGETS }),
        x: num('Solo con at=point: posición en metros.'),
        y: num('Solo con at=point.'),
        z: num('Solo con at=point.'),
        head: num('0-1: cuánto acompaña la cabeza (por defecto 0.35).'),
      },
      required: ['at'],
    },
    toCommand: (a) => ({ type: 'look', ...a }),
  },
  {
    name: 'avatar_move',
    description: 'Mueve o gira al avatar en el escenario. Camina solo si se desplaza.',
    inputSchema: {
      type: 'object',
      properties: {
        x: num('Posición lateral en metros (-3 a 3). Positivo = derecha del espectador (izquierda del avatar).'),
        z: num('Profundidad en metros (-3 a 3). Positivo = hacia la cámara.'),
        rotation: num('Orientación en grados. 0 = de frente a la cámara, + = gira hacia SU izquierda.'),
        relative: { type: 'boolean', description: 'Valores relativos a la posición actual.' },
        walk: { type: 'boolean', description: 'Animación de caminar (por defecto sí al desplazarse).' },
        duration: num('Segundos (opcional).'),
      },
    },
    toCommand: (a) => ({ type: 'move', ...a }),
  },
  {
    name: 'avatar_camera',
    description: 'Cambia el encuadre de la cámara o el fondo.',
    inputSchema: {
      type: 'object',
      properties: {
        preset: str('Encuadre.', { enum: CAMERA_PRESETS }),
        background: str(`Fondo: ${BACKGROUNDS.join(', ')} o #rrggbb.`),
      },
    },
    toCommand: (a) => {
      const cmds = [];
      if (a.preset) cmds.push({ type: 'camera', preset: a.preset });
      if (a.background) cmds.push({ type: 'background', mode: a.background });
      return cmds.length ? cmds : { type: 'camera', preset: 'bust' };
    },
  },
  {
    name: 'avatar_sequence',
    description: 'Ejecuta una lista de comandos en orden, esperando a que termine cada uno (habla, gestos, movimientos, {type:"wait",seconds}). Ideal para pequeñas actuaciones.',
    inputSchema: {
      type: 'object',
      properties: {
        steps: { type: 'array', items: commandItem, description: 'Pasos. Añade "await": false a un paso para no esperarlo.' },
        loop: { type: 'boolean', description: 'Repetir hasta avatar_stop.' },
      },
      required: ['steps'],
    },
    toCommand: (a) => ({ type: 'sequence', ...a }),
  },
  {
    name: 'avatar_stop',
    description: 'Detiene acciones en curso (o todo con reset=true para volver al estado inicial).',
    inputSchema: {
      type: 'object',
      properties: {
        what: str('Qué detener.', { enum: ['all', 'gestures', 'speech', 'sequence', 'move'] }),
        reset: { type: 'boolean', description: 'Además, volver a pose/expresión/posición iniciales.' },
      },
    },
    toCommand: (a) => (a.reset ? { type: 'reset' } : { type: 'stop', what: a.what || 'all' }),
  },
  {
    name: 'avatar_command',
    description: 'Envía comandos crudos del protocolo (uno o una lista que se ejecuta en paralelo). Útil para background, idle, load_model o combinaciones.',
    inputSchema: {
      type: 'object',
      properties: {
        commands: { type: 'array', items: commandItem },
      },
      required: ['commands'],
    },
    toCommand: (a) => a.commands,
  },
];

const PROTOCOL_FALLBACK = '2025-06-18';

/**
 * Procesa un mensaje JSON-RPC. Devuelve la respuesta, o null para notificaciones.
 * ctx = { dispatch(commands) -> Promise<result>, getState() -> Promise<state> }
 */
export async function handleRpc(msg, ctx) {
  if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0') {
    return { jsonrpc: '2.0', id: msg?.id ?? null, error: { code: -32600, message: 'Invalid Request' } };
  }
  const { id, method, params } = msg;
  const isNotification = id === undefined || id === null;
  const reply = (result) => (isNotification ? null : { jsonrpc: '2.0', id, result });
  const fail = (code, message) => (isNotification ? null : { jsonrpc: '2.0', id, error: { code, message } });

  try {
    switch (method) {
      case 'initialize':
        return reply({
          protocolVersion: params?.protocolVersion || PROTOCOL_FALLBACK,
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER_INFO,
          instructions: INSTRUCTIONS,
        });
      case 'notifications/initialized':
      case 'notifications/cancelled':
        return null;
      case 'ping':
        return reply({});
      case 'tools/list':
        return reply({
          tools: TOOLS.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
        });
      case 'tools/call': {
        const tool = TOOLS.find((t) => t.name === params?.name);
        if (!tool) return fail(-32602, `Herramienta desconocida: ${params?.name}`);
        const args = params?.arguments || {};
        let data;
        try {
          data = tool.toCommand ? await ctx.dispatch(tool.toCommand(args)) : await ctx.getState();
        } catch (err) {
          return reply({ content: [{ type: 'text', text: String(err?.message || err) }], isError: true });
        }
        const isError = data && data.ok === false;
        return reply({ content: [{ type: 'text', text: JSON.stringify(data, null, 2) }], isError: !!isError });
      }
      default:
        if (typeof method === 'string' && method.startsWith('notifications/')) return null;
        return fail(-32601, `Método no soportado: ${method}`);
    }
  } catch (err) {
    return fail(-32603, String(err?.message || err));
  }
}
