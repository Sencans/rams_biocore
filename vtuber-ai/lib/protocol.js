// Protocolo compartido entre el servidor, el visor (navegador) y el servidor MCP.
// Es la única fuente de verdad para los nombres de poses, gestos, etc.
// Todos los ángulos están en GRADOS.

export const DEFAULT_MODEL_URL =
  'https://cdn.jsdelivr.net/gh/pixiv/three-vrm@v3.4.4/packages/three-vrm/examples/models/VRM1_Constraint_Twist_Sample.vrm';

export const EMOTIONS = ['happy', 'angry', 'sad', 'relaxed', 'surprised'];
export const EMOTION_LABELS = {
  happy: 'Feliz', angry: 'Enojo', sad: 'Triste', relaxed: 'Tranquila', surprised: 'Sorpresa', neutral: 'Neutral',
};
export const VISEMES = ['aa', 'ih', 'ou', 'ee', 'oh'];

// Poses estáticas. Los huesos que no aparecen vuelven a 0.
// Convención (vista desde el avatar, que mira hacia la cámara):
//   x+ = inclinar hacia delante / mirar abajo
//   y+ = girar hacia SU izquierda
//   z+ = ladear hacia SU derecha (en brazos: el brazo izquierdo SUBE con z+, el derecho BAJA con z+)
export const POSES = {
  relaxed: {
    label: 'Relajada',
    hands: 'relaxed',
    bones: {
      leftUpperArm: { z: -72 }, rightUpperArm: { z: 72 },
      leftLowerArm: { y: -14 }, rightLowerArm: { y: 14 },
      leftHand: { z: -6 }, rightHand: { z: 6 },
    },
  },
  tpose: { label: 'Pose T', hands: 'open', bones: {} },
  apose: {
    label: 'Pose A',
    hands: 'open',
    bones: { leftUpperArm: { z: -40 }, rightUpperArm: { z: 40 } },
  },
  arms_up: {
    label: 'Brazos arriba',
    hands: 'open',
    bones: {
      leftUpperArm: { z: 62 }, rightUpperArm: { z: -62 },
      leftLowerArm: { z: 12 }, rightLowerArm: { z: -12 },
      head: { x: -8 },
    },
  },
  hands_on_hips: {
    label: 'Manos en cadera',
    hands: 'relaxed',
    bones: {
      leftUpperArm: { z: -48, y: 12 }, rightUpperArm: { z: 48, y: -12 },
      leftLowerArm: { z: -92, y: 0 }, rightLowerArm: { z: 92, y: 0 },
      leftHand: { z: -20 }, rightHand: { z: 20 },
      spine: { z: 2 },
    },
  },
  arms_crossed: {
    label: 'Brazos cruzados',
    hands: 'relaxed',
    bones: {
      leftUpperArm: { z: -62, y: -34 }, rightUpperArm: { z: 62, y: 38 },
      leftLowerArm: { y: -118 }, rightLowerArm: { y: 112 },
      head: { x: -4, z: 3 },
    },
  },
  thinking: {
    label: 'Pensando',
    hands: 'relaxed',
    bones: {
      leftUpperArm: { z: -66, y: -30 }, leftLowerArm: { y: -100 },
      rightUpperArm: { z: 62, y: 30 }, rightLowerArm: { y: 140 },
      rightHand: { z: -20 },
      head: { x: 4, z: -7 },
    },
  },
  shy: {
    label: 'Tímida',
    hands: 'relaxed',
    bones: {
      leftUpperArm: { z: -76, y: -18 }, rightUpperArm: { z: 76, y: 18 },
      leftLowerArm: { y: -62 }, rightLowerArm: { y: 62 },
      head: { x: 12, z: -8 }, spine: { x: 4 },
    },
  },
};

// Gestos animados (la implementación vive en el visor). duration = segundos por defecto.
export const GESTURES = {
  wave: { label: 'Saludar', duration: 2.6, sided: true },
  nod: { label: 'Asentir', duration: 1.2 },
  shake_head: { label: 'Negar', duration: 1.4 },
  bow: { label: 'Reverencia', duration: 2.2 },
  tilt_head: { label: 'Ladear cabeza', duration: 1.8, sided: true },
  jump: { label: 'Saltar', duration: 1.1 },
  clap: { label: 'Aplaudir', duration: 2.0 },
  shrug: { label: 'Encogerse', duration: 1.6 },
  cheer: { label: 'Celebrar', duration: 2.4 },
  point: { label: 'Señalar', duration: 2.5, sided: true },
  dance: { label: 'Bailar', duration: 6.0 },
  laugh: { label: 'Reír', duration: 2.2 },
  look_around: { label: 'Mirar alrededor', duration: 3.2 },
  think: { label: 'Pensar', duration: 3.5 },
  surprise: { label: 'Sorprenderse', duration: 1.5 },
};

export const HAND_SHAPES = ['open', 'relaxed', 'fist', 'point', 'peace', 'thumbs_up'];

export const LOOK_TARGETS = [
  'camera', 'mouse', 'forward', 'left', 'right', 'up', 'down',
  'up_left', 'up_right', 'down_left', 'down_right', 'point', 'none',
];
export const CAMERA_PRESETS = ['face', 'bust', 'full', 'wide'];
export const BACKGROUNDS = ['gradient', 'green', 'blue', 'magenta', 'transparent'];

export const BONE_GUIDE =
  'Huesos humanoides VRM (los disponibles dependen del modelo): hips, spine, chest, upperChest, neck, head, ' +
  'leftShoulder, leftUpperArm, leftLowerArm, leftHand, rightShoulder, rightUpperArm, rightLowerArm, rightHand, ' +
  'leftUpperLeg, leftLowerLeg, leftFoot, rightUpperLeg, rightLowerLeg, rightFoot, y dedos ' +
  '(leftIndexProximal, leftThumbProximal...). Rotaciones en grados, relativas a la pose T. ' +
  'Vista desde el avatar: x+ = inclinar hacia delante / mirar abajo; y+ = girar hacia SU izquierda; ' +
  'z+ = ladear hacia SU derecha. Brazos: leftUpperArm z=-70 baja el brazo izquierdo, rightUpperArm z=70 baja el derecho; ' +
  'z positivo en el izquierdo / negativo en el derecho los sube. Codos: leftLowerArm y negativo y rightLowerArm y positivo doblan el antebrazo hacia delante.';

// Documentación de comandos: la usa /api/help y las herramientas MCP.
export const COMMANDS = {
  expression: {
    desc: 'Cambia la expresión facial. Las emociones son excluyentes salvo exclusive:false.',
    example: { type: 'expression', name: 'happy', value: 1, duration: 0.3, hold: 2 },
    fields: 'name (happy|angry|sad|relaxed|surprised|neutral o cualquier expresión del modelo), value 0-1, duration s, hold s (vuelve a neutral después), values {nombre: peso} para mezclar',
  },
  pose: {
    desc: 'Aplica una pose estática.',
    example: { type: 'pose', name: 'thinking', duration: 0.6 },
    fields: `name: ${Object.keys(POSES).join('|')}`,
  },
  gesture: {
    desc: 'Reproduce un gesto animado (se mezcla sobre la pose actual).',
    example: { type: 'gesture', name: 'wave', side: 'right', duration: 2.5 },
    fields: `name: ${Object.keys(GESTURES).join('|')}; side left|right (wave, point, tilt_head); duration s; loop true`,
  },
  bones: {
    desc: 'Rota huesos libremente (grados). Ejes omitidos se conservan.',
    example: { type: 'bones', rotations: { head: { y: 20 }, rightUpperArm: { z: -20 } }, duration: 0.5, relative: false },
    fields: 'rotations {hueso: {x,y,z}}, duration s, relative bool',
  },
  hand: {
    desc: 'Forma de la mano.',
    example: { type: 'hand', side: 'right', shape: 'peace' },
    fields: `side left|right|both; shape ${HAND_SHAPES.join('|')}`,
  },
  speak: {
    desc: 'Habla con globo de texto, sincronización labial y voz del navegador (TTS).',
    example: { type: 'speak', text: '¡Hola a todos!', emotion: 'happy', gesture: 'wave' },
    fields: 'text, emotion, gesture, rate 0.5-2, pitch 0-2, lang (es-ES...), tts bool',
  },
  look: {
    desc: 'Dirige la mirada (ojos y un poco la cabeza).',
    example: { type: 'look', at: 'camera' },
    fields: `at: ${LOOK_TARGETS.join('|')} (point requiere x,y,z en metros); head 0-1 cuánto sigue la cabeza`,
  },
  move: {
    desc: 'Mueve/gira el avatar por el suelo (camina automáticamente).',
    example: { type: 'move', x: 0.5, z: 0, rotation: 0, duration: 1.5 },
    fields: 'x (+ = derecha del espectador, ±3 m), z (+ = hacia la cámara, ±3 m), rotation grados (+ = gira a SU izquierda), relative bool, walk bool, duration s',
  },
  camera: {
    desc: 'Encuadre de cámara.',
    example: { type: 'camera', preset: 'bust' },
    fields: `preset ${CAMERA_PRESETS.join('|')}, duration s`,
  },
  background: {
    desc: 'Fondo (verde/azul/magenta para chroma; transparent para OBS).',
    example: { type: 'background', mode: 'green' },
    fields: `mode ${BACKGROUNDS.join('|')} o color #rrggbb`,
  },
  idle: {
    desc: 'Animación de reposo (respiración, parpadeo, balanceo).',
    example: { type: 'idle', enabled: true, intensity: 1 },
    fields: 'enabled bool, intensity 0-2, blink bool',
  },
  sequence: {
    desc: 'Ejecuta pasos en orden, esperando a que termine cada uno (await:false en un paso para no esperarlo).',
    example: {
      type: 'sequence',
      steps: [
        { type: 'speak', text: 'Déjame pensar...', emotion: 'relaxed' },
        { type: 'gesture', name: 'think' },
        { type: 'expression', name: 'happy' },
        { type: 'speak', text: '¡Ya lo tengo!', gesture: 'cheer' },
      ],
    },
    fields: 'steps [comandos], loop bool',
  },
  wait: { desc: 'Pausa dentro de una secuencia.', example: { type: 'wait', seconds: 1 }, fields: 'seconds' },
  stop: {
    desc: 'Detiene acciones en curso.',
    example: { type: 'stop', what: 'all' },
    fields: 'what all|gestures|speech|sequence|move',
  },
  reset: { desc: 'Vuelve a la pose, expresión, mirada y posición iniciales.', example: { type: 'reset' }, fields: '' },
  load_model: {
    desc: 'Carga otro modelo VRM.',
    example: { type: 'load_model', url: 'sample' },
    fields: 'url: "sample", nombre de archivo en /models, o URL a un .vrm',
  },
  state: { desc: 'Devuelve el estado actual del avatar.', example: { type: 'state' }, fields: '' },
};

export const COMMAND_TYPES = Object.keys(COMMANDS);

export const HELP = {
  name: 'VTuber AI Studio',
  summary:
    'Controla un avatar VTuber 3D (VRM). Envía un comando JSON (o una lista, que se ejecuta en paralelo) a POST /api/command. ' +
    'Usa "sequence" para encadenar acciones en orden.',
  endpoints: {
    'POST /api/command': 'Cuerpo: comando | [comandos] | {commands:[...]}',
    'GET /api/state': 'Estado actual (modelo, huesos y expresiones disponibles, posición...)',
    'GET /api/help': 'Esta ayuda',
    'GET /api/events': 'Stream SSE que usa el visor',
    'POST /mcp': 'Endpoint MCP (Streamable HTTP, respuestas JSON)',
  },
  bones: BONE_GUIDE,
  commands: COMMANDS,
};
