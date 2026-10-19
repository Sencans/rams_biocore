// VTuber AI Studio — visor y motor de animación.
// Recibe comandos de la IA por SSE (/api/events) y los ejecuta sobre un modelo VRM.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import {
  DEFAULT_MODEL_URL, EMOTIONS, EMOTION_LABELS, VISEMES, POSES, GESTURES, HAND_SHAPES,
  LOOK_TARGETS, CAMERA_PRESETS, BACKGROUNDS, COMMANDS, COMMAND_TYPES,
} from '/protocol.js';

// ===================================================================== utilidades
const DEG = Math.PI / 180;
const AXES = ['x', 'y', 'z'];
const now = () => performance.now() / 1000;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, k) => a + (b - a) * k;
const ease = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const smooth = (e0, e1, x) => { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); };
const plateau = (p, a = 0.2) => smooth(0, a, p) * (1 - smooth(1 - a, 1, p));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const num = (v, d) => {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(+v)) return +v;
  return d;
};
const $ = (s) => document.querySelector(s);
const S = Math.sin;
const TAU = Math.PI * 2;

class CommandError extends Error {}
const fail = (msg) => { throw new CommandError(msg); };

const params = new URLSearchParams(location.search);
const OBS = params.has('obs');
if (OBS) document.body.classList.add('obs');

// ===================================================================== escena
const canvas = $('#scene');
const stage = $('#stage');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setClearColor(0x000000, 0);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 100);
camera.position.set(0, 1.3, 3.6);
const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 1.0, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.screenSpacePanning = true;
controls.minDistance = 0.3;
controls.maxDistance = 12;
controls.update();

const keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
keyLight.position.set(1, 2.2, 2.5);
const rimLight = new THREE.DirectionalLight(0xc9c2ff, 1.4);
rimLight.position.set(-1.8, 1.6, -2);
scene.add(keyLight, rimLight, new THREE.AmbientLight(0xffffff, 0.5));

const avatarRoot = new THREE.Group();
scene.add(avatarRoot);

// sombra suave bajo el avatar
const ground = (() => {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, 'rgba(0,0,0,0.45)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(1.1, 1.1),
    new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }),
  );
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = 0.002;
  avatarRoot.add(mesh);
  return mesh;
})();

function resize() {
  const { clientWidth: w, clientHeight: h } = stage;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(stage);
resize();

// ===================================================================== estado del avatar
let vrm = null;
let modelName = '';
let modelSource = '';
let availableBones = [];
let availableExpressions = [];
let currentPose = 'relaxed';

const boneState = new Map();   // hueso -> { cur, from, to, t0, dur }  (radianes)
const exprState = new Map();   // expresión -> { cur, from, to, t0, dur }
const gestures = new Map();    // nombre -> { name, impl, t0, dur, fade, mirror, loop, resolve }
const root = { cur: { x: 0, z: 0, ry: 0 }, from: null, to: null, t0: 0, dur: 0, walk: false, resolve: null, phase: 0 };
const camTween = { active: false };
const idle = { enabled: !params.has('noidle'), intensity: 1, blink: true };
const look = { mode: 'camera', head: 0.35, dir: null, yaw: 0, pitch: 0 };
const lookTarget = new THREE.Object3D();
scene.add(lookTarget);
const mouseNdc = new THREE.Vector2(0, 0);
let gestureExpr = {};
let exprHoldToken = 0;
let seqToken = 0;
let background = 'gradient';
let cameraPreset = 'full';

const tmpV = new THREE.Vector3();
const tmpV2 = new THREE.Vector3();

function tweenStep(s, t) {
  if (s.to == null) return;
  const k = s.dur <= 0 ? 1 : clamp((t - s.t0) / s.dur, 0, 1);
  const e = ease(k);
  if (typeof s.to === 'number') s.cur = lerp(s.from, s.to, e);
  else for (const a of AXES) s.cur[a] = lerp(s.from[a], s.to[a], e);
  if (k >= 1) { s.cur = typeof s.to === 'number' ? s.to : { ...s.to }; s.to = null; }
}

function boneEntry(name) {
  let s = boneState.get(name);
  if (!s) { s = { cur: { x: 0, y: 0, z: 0 }, to: null }; boneState.set(name, s); }
  return s;
}

function assertBone(name) {
  if (!vrm) fail('No hay ningún modelo cargado todavía.');
  if (!vrm.humanoid.getNormalizedBoneNode(name)) {
    fail(`El hueso "${name}" no existe en este modelo. Disponibles: ${availableBones.join(', ')}`);
  }
}

/** Fija el objetivo de un hueso (grados). Ejes omitidos se conservan. */
function setBone(name, deg, dur = 0.4, relative = false) {
  const s = boneEntry(name);
  const target = s.to ? { ...s.to } : { ...s.cur };
  for (const a of AXES) {
    const v = num(deg?.[a], null);
    if (v === null) continue;
    target[a] = relative ? target[a] + v * DEG : v * DEG;
  }
  s.from = { ...s.cur };
  s.to = target;
  s.t0 = now();
  s.dur = Math.max(0, dur);
}

function setExpr(name, value, dur = 0.3) {
  let s = exprState.get(name);
  if (!s) { s = { cur: 0, to: null }; exprState.set(name, s); }
  s.from = s.cur;
  s.to = clamp(value, 0, 1);
  s.t0 = now();
  s.dur = Math.max(0, dur);
}

function clearEmotions(dur = 0.3, except = null) {
  for (const e of [...EMOTIONS, ...exprState.keys()]) {
    if (e === except || VISEMES.includes(e) || e.startsWith('blink') || e.startsWith('look')) continue;
    if (exprState.has(e) || EMOTIONS.includes(e)) setExpr(e, 0, dur);
  }
}

// ===================================================================== manos
const FINGERS = ['Thumb', 'Index', 'Middle', 'Ring', 'Little'];
const SHAPES = {
  open:      { Thumb: 0,   Index: 0,    Middle: 0,    Ring: 0,    Little: 0 },
  relaxed:   { Thumb: 0.25, Index: 0.22, Middle: 0.28, Ring: 0.34, Little: 0.4 },
  fist:      { Thumb: 1,   Index: 1,    Middle: 1,    Ring: 1,    Little: 1 },
  point:     { Thumb: 0.9, Index: 0,    Middle: 1,    Ring: 1,    Little: 1 },
  peace:     { Thumb: 0.9, Index: 0,    Middle: 0,    Ring: 1,    Little: 1 },
  thumbs_up: { Thumb: 0,   Index: 1,    Middle: 1,    Ring: 1,    Little: 1 },
};
/** Devuelve {hueso: {x,y,z}} en grados para una forma de mano. */
function handShape(side, shape) {
  const c = SHAPES[shape] || fail(`Forma de mano desconocida "${shape}". Opciones: ${HAND_SHAPES.join(', ')}`);
  const sg = side === 'left' ? -1 : 1;
  const out = {};
  for (const f of FINGERS) {
    const k = c[f];
    if (f === 'Thumb') {
      out[`${side}ThumbMetacarpal`] = { x: 0, y: -sg * 38 * k, z: sg * 22 * k };
      out[`${side}ThumbProximal`] = { x: 0, y: -sg * 18 * k, z: sg * 26 * k };
      out[`${side}ThumbDistal`] = { x: 0, y: 0, z: sg * 40 * k };
    } else {
      out[`${side}${f}Proximal`] = { x: 0, y: 0, z: sg * 75 * k };
      out[`${side}${f}Intermediate`] = { x: 0, y: 0, z: sg * 90 * k };
      out[`${side}${f}Distal`] = { x: 0, y: 0, z: sg * 60 * k };
    }
  }
  return out;
}
const handsShape = (shape) => ({ ...handShape('left', shape), ...handShape('right', shape) });

function applyHand(side, shape, dur = 0.25) {
  const sides = side === 'both' || !side ? ['left', 'right'] : [side];
  for (const sd of sides) {
    if (!['left', 'right'].includes(sd)) fail('side debe ser left, right o both');
    for (const [b, r] of Object.entries(handShape(sd, shape))) {
      if (vrm?.humanoid.getNormalizedBoneNode(b)) setBone(b, r, dur);
    }
  }
}

const isFinger = (b) => /(Thumb|Index|Middle|Ring|Little)/.test(b);

function applyPose(name, dur = 0.6) {
  const pose = POSES[name] || fail(`Pose desconocida "${name}". Opciones: ${Object.keys(POSES).join(', ')}`);
  const names = new Set([...boneState.keys(), ...Object.keys(pose.bones)]);
  for (const b of names) {
    if (isFinger(b)) continue;
    setBone(b, { x: 0, y: 0, z: 0, ...(pose.bones[b] || {}) }, dur);
  }
  if (pose.hands) applyHand('both', pose.hands, dur);
  currentPose = name;
}

// ===================================================================== gestos
function squat(deg) {
  return {
    add: {
      leftUpperLeg: { x: -deg }, rightUpperLeg: { x: -deg },
      leftLowerLeg: { x: deg * 2 }, rightLowerLeg: { x: deg * 2 },
      leftFoot: { x: -deg }, rightFoot: { x: -deg },
    },
    rootY: -0.8 * (1 - Math.cos(deg * DEG)),
  };
}
const merge = (...fs) => {
  const out = { set: {}, add: {}, expr: {}, rootY: 0 };
  for (const f of fs) {
    if (!f) continue;
    Object.assign(out.set, f.set);
    for (const [b, r] of Object.entries(f.add || {})) {
      const o = (out.add[b] ||= {});
      for (const a of AXES) if (r[a] != null) o[a] = (o[a] || 0) + r[a];
    }
    Object.assign(out.expr, f.expr);
    out.rootY += f.rootY || 0;
  }
  return out;
};

const GESTURE_IMPL = {
  wave: (t) => {
    const o = S(t * 9);
    return {
      set: {
        rightUpperArm: { x: 0, y: 12, z: 14 },
        rightLowerArm: { x: 0, y: 0, z: -100 + 20 * o },
        rightHand: { x: 0, y: 0, z: -8 * o },
        ...handShape('right', 'open'),
      },
      add: { head: { z: -5 }, spine: { z: -2 } },
      expr: { happy: 0.6 },
    };
  },
  nod: (t, p) => {
    const k = Math.pow(S(TAU * p), 2);
    return { add: { head: { x: 15 * k }, neck: { x: 5 * k } } };
  },
  shake_head: (t, p) => {
    const k = S(TAU * 2.5 * p) * (1 - 0.3 * p);
    return { add: { head: { y: 22 * k }, neck: { y: 6 * k } } };
  },
  bow: (t, p) => {
    const k = plateau(p, 0.3);
    return {
      add: { spine: { x: 26 * k }, chest: { x: 10 * k }, head: { x: 12 * k } },
      set: { leftUpperArm: { z: -80 }, rightUpperArm: { z: 80 } },
    };
  },
  tilt_head: (t, p) => {
    const k = plateau(p, 0.25);
    return { add: { head: { z: 14 * k }, neck: { z: 4 * k } }, expr: { relaxed: 0.3 } };
  },
  jump: (t, p) => {
    if (p < 0.28) { const k = S((p / 0.28) * Math.PI); return merge(squat(26 * k), { add: { leftUpperArm: { x: 25 * k }, rightUpperArm: { x: 25 * k } } }); }
    if (p < 0.78) {
      const q = (p - 0.28) / 0.5;
      return {
        rootY: 0.22 * S(q * Math.PI),
        add: {
          leftUpperArm: { z: 40 * S(q * Math.PI) }, rightUpperArm: { z: -40 * S(q * Math.PI) },
          leftLowerLeg: { x: 20 * S(q * Math.PI) }, rightLowerLeg: { x: 20 * S(q * Math.PI) },
        },
        expr: { happy: 0.8 },
      };
    }
    const k = S(((p - 0.78) / 0.22) * Math.PI);
    return squat(18 * k);
  },
  clap: (t) => {
    const c = 0.5 + 0.5 * S(t * 13 - Math.PI / 2);
    return {
      set: {
        leftUpperArm: { x: 0, y: -72, z: -28 }, rightUpperArm: { x: 0, y: 72, z: 28 },
        leftLowerArm: { x: 0, y: -(38 + 24 * c), z: 32 }, rightLowerArm: { x: 0, y: 38 + 24 * c, z: -32 },
        leftHand: { x: -70, y: 0, z: 0 }, rightHand: { x: -70, y: 0, z: 0 },
        ...handsShape('open'),
      },
      expr: { happy: 0.8 },
    };
  },
  shrug: (t, p) => {
    const k = plateau(p, 0.3);
    return {
      add: { leftShoulder: { z: 14 * k }, rightShoulder: { z: -14 * k }, head: { z: 8 * k } },
      set: {
        leftUpperArm: { x: 0, y: -10, z: -70 }, rightUpperArm: { x: 0, y: 10, z: 70 },
        leftLowerArm: { x: 0, y: -70, z: 0 }, rightLowerArm: { x: 0, y: 70, z: 0 },
        leftHand: { x: 110, y: 0, z: 0 }, rightHand: { x: 110, y: 0, z: 0 },
        ...handsShape('open'),
      },
      expr: { relaxed: 0.4 },
    };
  },
  cheer: (t, p) => {
    const o = S(t * 8);
    return {
      set: {
        leftUpperArm: { x: 0, y: 0, z: 58 + 10 * o }, rightUpperArm: { x: 0, y: 0, z: -58 - 10 * o },
        leftLowerArm: { x: 0, y: 0, z: 14 }, rightLowerArm: { x: 0, y: 0, z: -14 },
        ...handsShape('fist'),
      },
      add: { head: { x: -8 } },
      expr: { happy: 1 },
      rootY: 0.035 * Math.abs(S(t * 8)) * plateau(p, 0.15),
    };
  },
  point: () => ({
    set: {
      rightUpperArm: { x: 0, y: 72, z: 12 }, rightLowerArm: { x: 0, y: 8, z: 0 },
      rightHand: { x: 0, y: 0, z: 0 },
      ...handShape('right', 'point'),
    },
    add: { head: { y: -4 } },
  }),
  dance: (t) => {
    const b = t * TAU * 1.1;
    const bounce = 0.5 + 0.5 * S(2 * b);
    return merge(
      {
        add: {
          hips: { z: 7 * S(b), y: 10 * S(b / 2) },
          spine: { z: -5 * S(b) }, chest: { y: -8 * S(b / 2) },
          head: { z: 6 * S(b + 0.6), x: 4 * S(2 * b) },
        },
        set: {
          leftUpperArm: { x: -22 * S(b), y: -10, z: -66 + 8 * S(2 * b) },
          rightUpperArm: { x: 22 * S(b), y: 10, z: 66 - 8 * S(2 * b) },
          leftLowerArm: { x: 0, y: -85, z: 20 }, rightLowerArm: { x: 0, y: 85, z: -20 },
          ...handsShape('fist'),
        },
        expr: { happy: 0.7 },
      },
      squat(10 * bounce),
    );
  },
  laugh: (t, p) => {
    const k = plateau(p, 0.2);
    const o = S(t * 22);
    return {
      add: {
        chest: { x: 3 * o * k }, spine: { x: -4 * k },
        head: { x: -10 * k + 2 * o * k, z: 5 * k },
        leftShoulder: { z: 2 * o * k }, rightShoulder: { z: -2 * o * k },
      },
      expr: { happy: 1, aa: (0.3 + 0.3 * Math.abs(S(t * 11))) * k },
    };
  },
  look_around: (t, p) => ({
    add: { head: { y: 32 * S(TAU * p), x: -4 * S(TAU * 2 * p) }, neck: { y: 12 * S(TAU * p) } },
  }),
  think: (t, p) => {
    const k = plateau(p, 0.2);
    const th = POSES.thinking.bones;
    return {
      set: { ...th, ...handShape('right', 'relaxed') },
      add: { head: { z: -6 * k, x: -6 * k + 3 * S(t * 2) * k } },
      expr: { relaxed: 0.3 },
    };
  },
  surprise: (t, p) => {
    const k = plateau(p, 0.15);
    return {
      add: {
        spine: { x: -8 * k }, head: { x: -6 * k },
        leftUpperArm: { z: 25 * k }, rightUpperArm: { z: -25 * k },
        leftLowerArm: { y: -40 * k }, rightLowerArm: { y: 40 * k },
      },
      expr: { surprised: 1 },
      rootY: 0.03 * S(clamp(p / 0.25, 0, 1) * Math.PI),
    };
  },
};

function mirrorMap(m) {
  const o = {};
  for (const [b, r] of Object.entries(m || {})) {
    const nb = b.startsWith('left') ? 'right' + b.slice(4) : b.startsWith('right') ? 'left' + b.slice(5) : b;
    o[nb] = { x: r.x, y: r.y != null ? -r.y : undefined, z: r.z != null ? -r.z : undefined };
  }
  return o;
}

function startGesture(name, opts = {}) {
  const impl = GESTURE_IMPL[name] || fail(`Gesto desconocido "${name}". Opciones: ${Object.keys(GESTURES).join(', ')}`);
  const meta = GESTURES[name];
  const prev = gestures.get(name);
  prev?.resolve?.();
  const dur = clamp(num(opts.duration, meta.duration), 0.3, 120);
  const side = opts.side || 'right';
  if (!['left', 'right'].includes(side)) fail('side debe ser left o right');
  let resolve;
  const done = new Promise((r) => { resolve = r; });
  gestures.set(name, {
    name, impl, t0: now(), dur, fade: Math.min(0.35, dur * 0.25),
    mirror: meta.sided && side === 'left', loop: !!opts.loop, resolve,
  });
  return { dur, done };
}

function stopGestures() {
  for (const g of gestures.values()) g.resolve?.();
  gestures.clear();
}

// ===================================================================== movimiento por el escenario
function moveAvatar(c) {
  const cur = root.cur;
  const target = { ...cur };
  const rel = !!c.relative;
  const x = num(c.x, null), z = num(c.z, null), rot = num(c.rotation ?? c.rotationY ?? c.angle, null);
  if (x !== null) target.x = clamp(rel ? cur.x + x : x, -3, 3);
  if (z !== null) target.z = clamp(rel ? cur.z + z : z, -3, 3);
  if (rot !== null) target.ry = rel ? cur.ry + rot * DEG : rot * DEG;
  const dist = Math.hypot(target.x - cur.x, target.z - cur.z);
  const dur = clamp(num(c.duration, dist > 0.01 ? Math.max(0.6, dist / 0.8) : 0.7), 0.05, 30);
  root.resolve?.();
  root.from = { ...cur };
  root.to = target;
  root.t0 = now();
  root.dur = dur;
  root.walk = c.walk ?? dist > 0.05;
  return { dur, done: new Promise((r) => { root.resolve = r; }) };
}

// ===================================================================== mirada
const LOOK_DIRS = {
  forward: [0, 0], left: [1, 0], right: [-1, 0], up: [0, 1], down: [0, -1],
  up_left: [1, 1], up_right: [-1, 1], down_left: [1, -1], down_right: [-1, -1],
};
function setLook(c) {
  const at = c.at || c.target || 'camera';
  if (!LOOK_TARGETS.includes(at)) fail(`Mirada desconocida "${at}". Opciones: ${LOOK_TARGETS.join(', ')}`);
  look.mode = at;
  look.head = clamp(num(c.head, look.head), 0, 1);
  if (at === 'point') {
    const x = num(c.x, null), y = num(c.y, null), z = num(c.z, null);
    if (x === null || y === null || z === null) fail('at:"point" requiere x, y, z (metros).');
    lookTarget.position.set(x, y, z);
  }
  look.dir = LOOK_DIRS[at] || null;
  if (vrm?.lookAt) {
    if (at === 'none') {
      vrm.lookAt.target = null;
      vrm.lookAt.yaw = 0;
      vrm.lookAt.pitch = 0;
    } else {
      vrm.lookAt.target = at === 'camera' ? camera : lookTarget;
    }
  }
}

function headWorld(out) {
  const node = vrm?.humanoid.getRawBoneNode('head');
  if (node) node.getWorldPosition(out);
  else out.set(root.cur.x, 1.4, root.cur.z);
  return out;
}

const raycaster = new THREE.Raycaster();
const lookPlane = new THREE.Plane();
function updateLookTarget() {
  if (!vrm) return;
  if (look.mode === 'mouse') {
    headWorld(tmpV);
    lookPlane.setFromNormalAndCoplanarPoint(new THREE.Vector3(0, 0, 1), tmpV2.set(0, 0, tmpV.z + 0.8));
    raycaster.setFromCamera(mouseNdc, camera);
    raycaster.ray.intersectPlane(lookPlane, lookTarget.position);
  } else if (look.dir) {
    headWorld(tmpV);
    const [h, v] = look.dir;
    tmpV2.set(h * 1.6, v * 0.9, 2).applyAxisAngle(new THREE.Vector3(0, 1, 0), root.cur.ry);
    lookTarget.position.copy(tmpV).add(tmpV2);
  }
}

// ===================================================================== cámara y fondo
function setCamera(preset, dur = 0.9) {
  if (!CAMERA_PRESETS.includes(preset)) fail(`Encuadre desconocido "${preset}". Opciones: ${CAMERA_PRESETS.join(', ')}`);
  cameraPreset = preset;
  // encuadra la posición final si el avatar se está moviendo
  const head = headWorld(new THREE.Vector3());
  if (root.to) { head.x += root.to.x - root.cur.x; head.z += root.to.z - root.cur.z; }
  const h = Math.max(0.6, head.y + 0.15);
  const fov = Math.tan((camera.fov * DEG) / 2) * 2;
  const portrait = camera.aspect < 0.9 ? 0.9 / camera.aspect : 1;
  let target, dist;
  if (preset === 'face') { target = head.clone().add(new THREE.Vector3(0, 0.02, 0)); dist = 0.36 / fov; }
  else if (preset === 'bust') { target = head.clone().add(new THREE.Vector3(0, -0.2, 0)); dist = (0.75 / fov) * Math.sqrt(portrait); }
  else if (preset === 'full') { target = new THREE.Vector3(head.x, h * 0.53, head.z); dist = ((h * 1.3) / fov) * portrait; }
  else { target = new THREE.Vector3(head.x, h * 0.5, head.z); dist = ((h * 2.1) / fov) * portrait; }
  const fwd = new THREE.Vector3(0, 0, 1); // la cámara siempre mira el escenario de frente
  const pos = target.clone().add(fwd.multiplyScalar(dist)).add(new THREE.Vector3(0, dist * 0.04, 0));
  Object.assign(camTween, {
    active: true, t0: now(), dur: Math.max(0.01, dur),
    fromPos: camera.position.clone(), fromTarget: controls.target.clone(), toPos: pos, toTarget: target,
  });
}

function setBackground(mode) {
  const isHex = typeof mode === 'string' && /^#[0-9a-f]{3,8}$/i.test(mode);
  if (!isHex && !BACKGROUNDS.includes(mode)) fail(`Fondo desconocido "${mode}". Opciones: ${BACKGROUNDS.join(', ')} o #rrggbb`);
  background = mode;
  document.body.dataset.bg = isHex ? 'custom' : mode;
  stage.style.background = isHex ? mode : '';
  ground.visible = mode === 'gradient' || isHex;
  syncChips('#bg-chips', mode);
}

// ===================================================================== voz y labios
const speech = { active: false, text: '', start: 0, cps: 14, tts: false, boundaryIdx: 0, boundaryAt: 0, offset: 0, token: 0 };
const visemes = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
const tts = { enabled: !OBS || params.has('tts'), voiceURI: '', pitch: 1.1, unlocked: false };
try {
  const saved = JSON.parse(localStorage.getItem('vtuber-tts') || '{}');
  if (typeof saved.enabled === 'boolean' && !OBS) tts.enabled = saved.enabled;
  if (saved.voiceURI) tts.voiceURI = saved.voiceURI;
  if (saved.pitch) tts.pitch = saved.pitch;
} catch {}
const saveTts = () => { try { localStorage.setItem('vtuber-tts', JSON.stringify({ enabled: tts.enabled, voiceURI: tts.voiceURI, pitch: tts.pitch })); } catch {} };

const bubble = $('#bubble');
let bubbleTimer = 0;
function showBubble(text) {
  clearTimeout(bubbleTimer);
  bubble.textContent = text;
  bubble.hidden = false;
  bubble.classList.remove('fade');
}
function hideBubble(delay = 1200) {
  clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => {
    bubble.classList.add('fade');
    bubbleTimer = setTimeout(() => { bubble.hidden = true; }, 300);
  }, delay);
}

function pickVoice(lang) {
  if (!('speechSynthesis' in window)) return null;
  const voices = speechSynthesis.getVoices();
  if (tts.voiceURI && !lang) {
    const v = voices.find((v) => v.voiceURI === tts.voiceURI);
    if (v) return v;
  }
  const want = (lang || 'es').toLowerCase();
  return voices.find((v) => v.lang.toLowerCase() === want)
    || voices.find((v) => v.lang.toLowerCase().startsWith(want.slice(0, 2)))
    || null;
}

function splitChunks(text) {
  const parts = text.match(/[^.!?¡¿…]+[.!?…]*|[¡¿]+[^.!?…]*[.!?…]*/g) || [text];
  const chunks = [];
  let buf = '';
  for (const p of parts) {
    if ((buf + p).length > 180 && buf) { chunks.push(buf); buf = ''; }
    buf += p;
  }
  if (buf.trim()) chunks.push(buf);
  return chunks;
}

function stopSpeech() {
  speech.token++;
  if (speech.active) {
    speech.active = false;
    hideBubble(200);
  }
  try { if ('speechSynthesis' in window) speechSynthesis.cancel(); } catch {}
}

/** Dice un fragmento con TTS. Resuelve 'ok' al terminar o 'blocked' si el navegador no lo permite. */
function speakChunk(chunk, offset, opts) {
  return new Promise((res) => {
    let started = false;
    const u = new SpeechSynthesisUtterance(chunk);
    const voice = pickVoice(opts.lang);
    if (voice) { u.voice = voice; u.lang = voice.lang; } else if (opts.lang) u.lang = opts.lang;
    u.rate = opts.rate;
    u.pitch = opts.pitch;
    u.volume = opts.volume;
    u.onstart = () => {
      started = true;
      speech.tts = true; speech.offset = offset; speech.boundaryIdx = 0; speech.boundaryAt = now();
    };
    u.onboundary = (e) => { speech.boundaryIdx = e.charIndex; speech.boundaryAt = now(); };
    u.onend = () => res('ok');
    u.onerror = (e) => res(!started && !['interrupted', 'canceled'].includes(e.error) ? 'blocked' : 'ok');
    // si no arranca (sin permiso de audio, sin voces), seguimos con labios en silencio
    setTimeout(() => { if (!started) { try { speechSynthesis.cancel(); } catch {} res('blocked'); } }, 2500);
    // y si nunca llega onend, no nos quedamos colgados
    setTimeout(() => res('ok'), (chunk.length / speech.cps + 5) * 2000);
    speechSynthesis.speak(u);
  });
}

function speak(c) {
  const text = String(c.text ?? '').trim();
  if (!text) fail('speak requiere "text".');
  if (text.length > 2000) fail('El texto es demasiado largo (máx. 2000 caracteres).');
  if (c.gesture && !GESTURE_IMPL[c.gesture]) fail(`Gesto desconocido "${c.gesture}". Opciones: ${Object.keys(GESTURES).join(', ')}`);
  if (c.emotion && c.emotion !== 'neutral' && vrm && !vrm.expressionManager.getExpression(c.emotion)) {
    fail(`Emoción "${c.emotion}" no existe. Disponibles: ${availableExpressions.join(', ')}`);
  }
  stopSpeech();
  const token = ++speech.token;
  const rate = clamp(num(c.rate, 1), 0.5, 2);
  const pitch = clamp(num(c.pitch, tts.pitch), 0, 2);
  Object.assign(speech, { active: true, text, start: now(), cps: 14 * rate, tts: false, boundaryIdx: 0, boundaryAt: 0, offset: 0 });
  showBubble(text);
  if (c.emotion && vrm) applyExpression({ name: c.emotion, value: num(c.emotionValue, 0.85) });
  if (c.gesture && vrm) startGesture(c.gesture, { side: c.side });

  const est = Math.max(1, text.length / speech.cps + 0.3);
  const useTts = (c.tts ?? tts.enabled) && 'speechSynthesis' in window;
  let done;
  if (useTts) {
    done = (async () => {
      let offset = 0;
      const opts = { lang: c.lang, rate, pitch, volume: clamp(num(c.volume, 1), 0, 1) };
      for (const chunk of splitChunks(text)) {
        if (token !== speech.token) return;
        const r = await speakChunk(chunk, offset, opts);
        if (r === 'blocked') {
          // sin audio: completamos la animación de labios con el tiempo estimado
          speech.tts = false;
          speech.start = now() - offset / speech.cps;
          await wait(((text.length - offset) / speech.cps + 0.3) * 1000);
          return;
        }
        offset += chunk.length;
      }
    })();
  } else {
    done = wait(est * 1000);
  }
  const finished = done.then(() => {
    if (token !== speech.token) return;
    speech.active = false;
    hideBubble();
    if (c.emotion && c.keepEmotion !== true) clearEmotions(0.6);
  });
  return { result: { estimatedSeconds: +est.toFixed(1), voice: useTts ? 'tts' : 'silent' }, done: finished };
}

const VOWEL_MAP = { a: 'aa', á: 'aa', e: 'ee', é: 'ee', i: 'ih', í: 'ih', y: 'ih', o: 'oh', ó: 'oh', u: 'ou', ú: 'ou', ü: 'ou' };
function updateVisemes(t, dt) {
  const target = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
  if (speech.active) {
    let idx = speech.tts && speech.boundaryAt
      ? speech.offset + speech.boundaryIdx + (t - speech.boundaryAt) * speech.cps
      : (t - speech.start) * speech.cps;
    const len = speech.text.length;
    if (idx >= len) idx = speech.tts ? idx % len : len - 1;
    const ch = (speech.text[Math.floor(idx)] || ' ').toLowerCase();
    const v = VOWEL_MAP[ch];
    const jitter = 0.75 + 0.25 * S(t * 31) * S(t * 17);
    if (v) target[v] = 0.85 * jitter;
    else if (/[mbp]/.test(ch)) { /* boca cerrada */ }
    else if (/[a-zñ]/.test(ch)) target.aa = 0.22 * jitter;
  }
  const k = Math.min(1, dt * 22);
  for (const v of VISEMES) visemes[v] = lerp(visemes[v], target[v], k);
}

// ===================================================================== expresiones
function applyExpression(c) {
  if (!vrm) fail('No hay ningún modelo cargado todavía.');
  const dur = clamp(num(c.duration, 0.3), 0, 10);
  const exclusive = c.exclusive !== false;
  const token = ++exprHoldToken;
  if (c.values && typeof c.values === 'object') {
    if (exclusive) clearEmotions(dur);
    for (const [n, v] of Object.entries(c.values)) {
      if (!vrm.expressionManager.getExpression(n)) fail(`Expresión "${n}" no existe. Disponibles: ${availableExpressions.join(', ')}`);
      setExpr(n, clamp(num(v, 1), 0, 1), dur);
    }
  } else {
    const name = String(c.name || '');
    if (!name) fail('expression requiere "name" o "values".');
    if (name === 'neutral') {
      clearEmotions(dur);
    } else {
      if (!vrm.expressionManager.getExpression(name)) {
        fail(`Expresión "${name}" no existe en este modelo. Disponibles: ${availableExpressions.join(', ')}`);
      }
      if (exclusive && EMOTIONS.includes(name)) clearEmotions(dur, name);
      setExpr(name, clamp(num(c.value, 1), 0, 1), dur);
    }
    syncChips('#emotion-chips', name);
  }
  const hold = num(c.hold, 0);
  let done = wait(dur * 1000);
  if (hold > 0) {
    done = wait((dur + hold) * 1000).then(() => {
      if (token === exprHoldToken) { clearEmotions(0.5); syncChips('#emotion-chips', 'neutral'); }
    });
  }
  return { done };
}

// parpadeo automático
const blink = { next: now() + 2, start: -10, double: false };
function blinkValue(t) {
  if (!idle.blink) return 0;
  if (t >= blink.next) {
    blink.start = t;
    blink.double = Math.random() < 0.18;
    blink.next = t + (blink.double ? 0.35 : 2 + Math.random() * 4);
  }
  const bt = t - blink.start;
  if (bt < 0.06) return bt / 0.06;
  if (bt < 0.16) return 1 - (bt - 0.06) / 0.1;
  return 0;
}

// ===================================================================== bucle de animación por fotograma
const followSmooth = { yaw: 0, pitch: 0 };
function composeBones(t, dt) {
  for (const s of boneState.values()) tweenStep(s, t);
  const out = new Map();
  for (const [b, s] of boneState) out.set(b, { ...s.cur });
  const get = (b) => {
    let v = out.get(b);
    if (!v) { boneEntry(b); v = { x: 0, y: 0, z: 0 }; out.set(b, v); }
    return v;
  };
  const add = (b, x = 0, y = 0, z = 0) => { const v = get(b); v.x += x * DEG; v.y += y * DEG; v.z += z * DEG; };

  // gestos
  gestureExpr = {};
  let rootY = 0;
  for (const [name, g] of gestures) {
    let gt = t - g.t0;
    if (gt >= g.dur) {
      if (g.loop) { g.t0 = t; gt = 0; } else { gestures.delete(name); g.resolve?.(); continue; }
    }
    const w = g.loop ? smooth(0, g.fade, t - (g.firstT0 ??= g.t0)) : smooth(0, g.fade, gt) * smooth(0, g.fade, g.dur - gt);
    const f = g.impl(gt, gt / g.dur, g) || {};
    const set = g.mirror ? mirrorMap(f.set) : f.set;
    const ad = g.mirror ? mirrorMap(f.add) : f.add;
    for (const [b, r] of Object.entries(set || {})) {
      const v = get(b);
      for (const a of AXES) if (r[a] != null) v[a] = lerp(v[a], r[a] * DEG, w);
    }
    for (const [b, r] of Object.entries(ad || {})) {
      const v = get(b);
      for (const a of AXES) if (r[a] != null) v[a] += r[a] * DEG * w;
    }
    for (const [e, val] of Object.entries(f.expr || {})) gestureExpr[e] = Math.max(gestureExpr[e] || 0, val * w);
    rootY += (f.rootY || 0) * (g.loop ? w : 1);
  }

  // caminar
  let walkW = 0;
  if (root.to && root.walk) {
    const k = clamp((t - root.t0) / root.dur, 0, 1);
    walkW = smooth(0, 0.12, k) * (1 - smooth(0.88, 1, k));
    root.phase += dt * TAU * 1.7;
    const ph = root.phase, a = 24 * walkW;
    add('leftUpperLeg', -a * S(ph)); add('rightUpperLeg', a * S(ph));
    add('leftLowerLeg', 34 * walkW * Math.max(0, -S(ph - 0.6))); add('rightLowerLeg', 34 * walkW * Math.max(0, S(ph - 0.6)));
    add('leftUpperArm', a * 0.6 * S(ph)); add('rightUpperArm', -a * 0.6 * S(ph));
    add('spine', 0, 4 * walkW * S(ph));
    rootY += 0.018 * walkW * Math.abs(S(ph));
  }

  // reposo: respiración, balanceo y micro-movimientos
  if (idle.enabled) {
    const i = idle.intensity * (1 - walkW * 0.7);
    const br = S(t * TAU / 4.2);
    add('spine', 0.8 * br * i);
    add('chest', 1.2 * br * i);
    add('upperChest', 0.8 * br * i);
    add('leftShoulder', 0, 0, 0.8 * br * i); add('rightShoulder', 0, 0, -0.8 * br * i);
    add('hips', 0, 1.2 * S(t * 0.37) * i, 1.0 * S(t * 0.5) * i);
    add('spine', 0, 0, -1.0 * S(t * 0.5) * i);
    add('neck', 1.2 * S(t * 0.61) * i, 2.0 * S(t * 0.23 + 1) * i, 0.8 * S(t * 0.31) * i);
    add('head', 1.0 * S(t * 0.7 + 2) * i, 1.5 * S(t * 0.29) * i, 1.2 * S(t * 0.43 + 0.5) * i);
    add('leftUpperArm', 0, 0, -1.5 * br * i); add('rightUpperArm', 0, 0, 1.5 * br * i);
  }

  // la cabeza acompaña a la mirada
  let tYaw = 0, tPitch = 0;
  if (look.mode !== 'none' && look.head > 0) {
    const target = look.mode === 'camera' ? camera.position : lookTarget.position;
    headWorld(tmpV);
    tmpV2.copy(target).sub(tmpV).applyAxisAngle(new THREE.Vector3(0, 1, 0), -root.cur.ry);
    if (tmpV2.z > 0.05) {
      tYaw = clamp(Math.atan2(tmpV2.x, tmpV2.z), -0.9, 0.9);
      tPitch = clamp(Math.atan2(tmpV2.y, Math.hypot(tmpV2.x, tmpV2.z)), -0.5, 0.5);
    }
  }
  const fk = Math.min(1, dt * 4);
  followSmooth.yaw = lerp(followSmooth.yaw, tYaw * look.head, fk);
  followSmooth.pitch = lerp(followSmooth.pitch, tPitch * look.head, fk);
  const hv = get('head'), nv = get('neck');
  hv.y += followSmooth.yaw * 0.6; nv.y += followSmooth.yaw * 0.4;
  hv.x -= followSmooth.pitch * 0.6; nv.x -= followSmooth.pitch * 0.4;

  // aplicar al modelo
  for (const [b, v] of out) {
    const node = vrm.humanoid.getNormalizedBoneNode(b);
    if (node) node.rotation.set(v.x, v.y, v.z);
  }
  vrm.scene.position.y = rootY;
}

function updateRoot(t) {
  if (root.to) {
    const k = clamp((t - root.t0) / root.dur, 0, 1);
    const e = root.walk ? k * k * (3 - 2 * k) : ease(k);
    root.cur = { x: lerp(root.from.x, root.to.x, e), z: lerp(root.from.z, root.to.z, e), ry: lerp(root.from.ry, root.to.ry, e) };
    if (k >= 1) { root.cur = { ...root.to }; root.to = null; root.resolve?.(); root.resolve = null; }
  }
  avatarRoot.position.set(root.cur.x, 0, root.cur.z);
  avatarRoot.rotation.y = root.cur.ry;
}

function updateExpressions(t, dt) {
  for (const s of exprState.values()) tweenStep(s, t);
  updateVisemes(t, dt);
  const em = vrm.expressionManager;
  const values = {};
  for (const n of availableExpressions) values[n] = 0; // lo que nadie controla vuelve a 0
  for (const [n, s] of exprState) values[n] = s.cur;
  for (const [n, v] of Object.entries(gestureExpr)) values[n] = Math.max(values[n] || 0, v);
  for (const v of VISEMES) values[v] = Math.max(values[v] || 0, visemes[v]);
  values.blink = Math.max(values.blink || 0, blinkValue(t));
  for (const [n, v] of Object.entries(values)) {
    if (em.getExpression(n)) em.setValue(n, clamp(v, 0, 1));
  }
}

function updateCamera(t) {
  if (!camTween.active) return;
  const k = clamp((t - camTween.t0) / camTween.dur, 0, 1);
  const e = ease(k);
  camera.position.lerpVectors(camTween.fromPos, camTween.toPos, e);
  controls.target.lerpVectors(camTween.fromTarget, camTween.toTarget, e);
  if (k >= 1) camTween.active = false;
}

function updateBubble() {
  if (bubble.hidden || !vrm) return;
  headWorld(tmpV).add(new THREE.Vector3(0, 0.22, 0)).project(camera);
  const r = stage.getBoundingClientRect();
  const x = clamp((tmpV.x * 0.5 + 0.5) * r.width, 120, r.width - 120);
  const y = clamp((-tmpV.y * 0.5 + 0.5) * r.height, 90, r.height - 20);
  bubble.style.left = `${x}px`;
  bubble.style.top = `${y}px`;
}

let last = now();
function frame() {
  requestAnimationFrame(frame);
  const t = now();
  const dt = Math.min(0.1, t - last);
  last = t;
  if (vrm) {
    updateRoot(t);
    updateLookTarget();
    composeBones(t, dt);
    updateExpressions(t, dt);
    vrm.update(dt);
  }
  updateCamera(t);
  controls.update();
  renderer.render(scene, camera);
  updateBubble();
}
requestAnimationFrame(frame);

// ===================================================================== carga de modelos
const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));
const overlay = $('#overlay');
const overlayText = $('#overlay-text');
function setOverlay(text, { error = false, show = true } = {}) {
  overlayText.textContent = text;
  overlay.classList.toggle('error', error);
  overlay.classList.toggle('hidden', !show);
}

let loadToken = 0;
async function loadModel(url, label) {
  const token = ++loadToken;
  setOverlay('Cargando modelo…');
  let gltf;
  try {
    gltf = await loader.loadAsync(url, (e) => {
      if (token !== loadToken) return;
      if (e.total && e.loaded <= e.total) setOverlay(`Cargando modelo… ${Math.round((e.loaded / e.total) * 100)}%`);
      else setOverlay(`Cargando modelo… ${(e.loaded / 1048576).toFixed(1)} MB`);
    });
  } catch (err) {
    setOverlay(`No se pudo cargar el modelo: ${err?.message || err}`, { error: true });
    throw new CommandError(`No se pudo cargar el modelo: ${err?.message || err}`);
  }
  if (token !== loadToken) return null;
  const next = gltf.userData.vrm;
  if (!next) {
    setOverlay('El archivo no es un modelo VRM válido.', { error: true });
    throw new CommandError('El archivo no es un modelo VRM válido.');
  }
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  if (VRMUtils.combineSkeletons) VRMUtils.combineSkeletons(gltf.scene);
  else if (VRMUtils.removeUnnecessaryJoints) VRMUtils.removeUnnecessaryJoints(gltf.scene);
  VRMUtils.rotateVRM0(next);
  next.scene.traverse((o) => { o.frustumCulled = false; });

  if (vrm) { avatarRoot.remove(vrm.scene); VRMUtils.deepDispose(vrm.scene); }
  vrm = next;
  avatarRoot.add(vrm.scene);

  modelName = vrm.meta?.name || vrm.meta?.title || label || 'Modelo VRM';
  modelSource = label || url;
  availableBones = Object.keys(vrm.humanoid.rawHumanBones || vrm.humanoid.humanBones)
    .filter((b) => vrm.humanoid.getNormalizedBoneNode(b));
  availableExpressions = vrm.expressionManager.expressions.map((e) => e.expressionName);

  boneState.clear(); exprState.clear(); stopGestures(); stopSpeech();
  applyPose('relaxed', 0);
  setLook({ at: look.mode === 'point' ? 'camera' : look.mode });
  $('#model-name').textContent = modelName;
  populateBoneSelect();
  setOverlay('', { show: false });
  setCamera(cameraPreset, 0.01);
  postState();
  logEntry('sistema', { type: 'load_model', name: modelName }, { ok: true });
  return { model: modelName, vrmVersion: vrm.meta?.metaVersion, bones: availableBones.length, expressions: availableExpressions };
}

async function resolveModelUrl(url) {
  if (!url || url === 'sample' || url === 'default') return [DEFAULT_MODEL_URL, 'Modelo de ejemplo'];
  if (/^(https?:|blob:|data:|\/)/.test(url)) return [url, url.split('/').pop()];
  return [`/models/${encodeURIComponent(url)}`, url];
}

// ===================================================================== ejecución de comandos
function snapshot() {
  const exprs = {};
  for (const [n, s] of exprState) if (s.cur > 0.01) exprs[n] = +s.cur.toFixed(2);
  return {
    ready: !!vrm,
    model: modelName || null,
    vrmVersion: vrm?.meta?.metaVersion ?? null,
    expressionsAvailable: availableExpressions,
    bonesAvailable: availableBones,
    poses: Object.keys(POSES),
    gestures: Object.keys(GESTURES),
    handShapes: HAND_SHAPES,
    commandTypes: COMMAND_TYPES,
    current: {
      pose: currentPose,
      expressions: exprs,
      gestures: [...gestures.keys()],
      speaking: speech.active,
      look: look.mode,
      position: { x: +root.cur.x.toFixed(2), z: +root.cur.z.toFixed(2) },
      rotation: +(root.cur.ry / DEG).toFixed(1),
      camera: cameraPreset,
      background,
      idle: idle.enabled,
    },
  };
}

let stateTimer = 0;
function postState() {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(() => {
    fetch('/api/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(snapshot()) }).catch(() => {});
  }, 250);
}

const HANDLERS = {
  expression: (c) => applyExpression(c),
  pose: (c) => {
    if (!vrm) fail('No hay ningún modelo cargado todavía.');
    const dur = clamp(num(c.duration, 0.6), 0, 10);
    applyPose(c.name, dur);
    syncChips('#pose-chips', c.name);
    return { done: wait(dur * 1000) };
  },
  gesture: (c) => {
    if (!vrm) fail('No hay ningún modelo cargado todavía.');
    const { dur, done } = startGesture(c.name, c);
    return { result: { seconds: dur }, done: c.loop ? Promise.resolve() : done };
  },
  bones: (c) => {
    const rot = c.rotations || c.bones;
    if (!rot || typeof rot !== 'object') fail('bones requiere "rotations": {hueso: {x,y,z}} en grados.');
    const dur = clamp(num(c.duration, 0.4), 0, 10);
    for (const b of Object.keys(rot)) assertBone(b);
    for (const [b, r] of Object.entries(rot)) setBone(b, r, dur, !!c.relative);
    currentPose = 'custom';
    return { done: wait(dur * 1000) };
  },
  hand: (c) => {
    if (!vrm) fail('No hay ningún modelo cargado todavía.');
    applyHand(c.side || 'both', c.shape, clamp(num(c.duration, 0.25), 0, 5));
    return { done: wait(250) };
  },
  speak: (c) => speak(c),
  look: (c) => { setLook(c); syncChips('#look-chips', look.mode); return {}; },
  move: (c) => { const { dur, done } = moveAvatar(c); return { result: { seconds: +dur.toFixed(2) }, done }; },
  camera: (c) => {
    const dur = clamp(num(c.duration, 0.9), 0, 10);
    setCamera(c.preset || 'bust', dur);
    syncChips('#camera-chips', c.preset || 'bust');
    return { done: wait(dur * 1000) };
  },
  background: (c) => { setBackground(c.mode || c.color || 'gradient'); return {}; },
  idle: (c) => {
    if (c.enabled != null) idle.enabled = !!c.enabled;
    if (c.intensity != null) idle.intensity = clamp(num(c.intensity, 1), 0, 2);
    if (c.blink != null) idle.blink = !!c.blink;
    $('#idle-on').checked = idle.enabled;
    $('#blink-on').checked = idle.blink;
    return {};
  },
  wait: (c) => ({ done: wait(clamp(num(c.seconds, 1), 0, 120) * 1000) }),
  stop: (c) => {
    const what = c.what || 'all';
    if (what === 'all' || what === 'sequence') seqToken++;
    if (what === 'all' || what === 'gestures') stopGestures();
    if (what === 'all' || what === 'speech') stopSpeech();
    if (what === 'all' || what === 'move') { if (root.to) { root.to = null; root.resolve?.(); root.resolve = null; } }
    return {};
  },
  reset: () => {
    seqToken++;
    stopGestures(); stopSpeech();
    if (vrm) { applyPose('relaxed', 0.6); clearEmotions(0.4); }
    setLook({ at: 'camera', head: 0.35 });
    const m = moveAvatar({ x: 0, z: 0, rotation: 0, walk: false, duration: 0.6 });
    idle.enabled = true; idle.intensity = 1; idle.blink = true;
    syncChips('#emotion-chips', 'neutral'); syncChips('#pose-chips', 'relaxed'); syncChips('#look-chips', 'camera');
    return { done: m.done };
  },
  sequence: (c) => {
    const steps = c.steps;
    if (!Array.isArray(steps) || !steps.length) fail('sequence requiere "steps": [comandos].');
    steps.forEach((s, i) => {
      if (!s || !COMMAND_TYPES.includes(s.type)) fail(`Paso ${i + 1}: tipo inválido "${s?.type}". Válidos: ${COMMAND_TYPES.join(', ')}`);
    });
    const token = ++seqToken;
    const done = (async () => {
      do {
        for (const [i, step] of steps.entries()) {
          if (token !== seqToken) return;
          try {
            const r = await execute(step, 'secuencia');
            if (step.await !== false) await r.done;
          } catch (err) {
            console.warn(`Secuencia detenida en el paso ${i + 1}:`, err.message);
            return;
          }
        }
      } while (c.loop && token === seqToken);
    })();
    return { result: { steps: steps.length }, done };
  },
  load_model: async (c) => {
    const [url, label] = await resolveModelUrl(c.url || c.name);
    const info = await loadModel(url, label);
    return { result: info || {} };
  },
  state: () => ({ result: snapshot() }),
};

async function execute(cmd, source = 'manual', { quiet = false } = {}) {
  if (!cmd || typeof cmd !== 'object') fail('El comando debe ser un objeto JSON.');
  const h = HANDLERS[cmd.type] || fail(`Tipo desconocido "${cmd.type}". Válidos: ${COMMAND_TYPES.join(', ')}`);
  let r;
  try {
    r = (await h(cmd)) || {};
  } catch (err) {
    if (!quiet) logEntry(source, cmd, { ok: false, error: err.message });
    throw err;
  }
  if (!quiet) logEntry(source, cmd, { ok: true });
  postState();
  return { result: r.result || {}, done: r.done || Promise.resolve() };
}

async function runLocal(cmds, source = 'manual') {
  for (const c of Array.isArray(cmds) ? cmds : [cmds]) {
    try { await execute(c, source); } catch (err) { console.warn(err); }
  }
}

// ===================================================================== conexión con la IA (SSE)
const conn = $('#conn');
function setConn(state, text) {
  conn.className = `pill ${state}`;
  conn.querySelector('span').textContent = text;
}
function connect() {
  const es = new EventSource(`/api/events${OBS ? '?obs=1' : ''}`);
  es.addEventListener('hello', () => { setConn('ok', 'IA conectada'); postState(); });
  es.addEventListener('command', async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    const results = [];
    for (const c of msg.commands || []) {
      try {
        const { result } = await execute(c, msg.source || 'ia');
        results.push({ ok: true, type: c.type, ...result });
      } catch (err) {
        results.push({ ok: false, type: c?.type, error: err.message });
      }
    }
    fetch('/api/ack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: msg.id, results }),
    }).catch(() => {});
  });
  es.onerror = () => setConn('err', 'Servidor desconectado — reintentando…');
}
connect();

// ===================================================================== interfaz
const logEl = $('#log');
function logEntry(source, cmd, res) {
  if (OBS) return;
  logEl.querySelector('.empty')?.remove();
  const li = document.createElement('li');
  const time = new Date().toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const src = String(source);
  const label = { api: 'IA', mcp: 'IA · MCP', ia: 'IA', manual: 'Manual', secuencia: 'Secuencia', sistema: 'Sistema' }[src] || src;
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.innerHTML = `<span class="src ${src}"></span><span></span>`;
  meta.children[0].textContent = label;
  meta.children[1].textContent = time;
  const code = document.createElement('code');
  const short = { ...cmd };
  if (short.steps) short.steps = `[${short.steps.length} pasos]`;
  code.textContent = JSON.stringify(short);
  li.append(meta, code);
  if (!res.ok) {
    const e = document.createElement('div');
    e.className = 'err';
    e.textContent = res.error;
    li.append(e);
  }
  logEl.prepend(li);
  while (logEl.children.length > 80) logEl.lastChild.remove();
}
function emptyLog() {
  logEl.innerHTML = '<li class="empty">Aún no hay actividad. Envía un comando desde la consola o desde una IA.</li>';
}
emptyLog();
$('#log-clear').onclick = emptyLog;

function chips(sel, items, onClick) {
  const el = $(sel);
  el.innerHTML = '';
  for (const [value, label] of items) {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.value = value;
    b.textContent = label;
    b.onclick = () => onClick(value);
    el.append(b);
  }
}
function syncChips(sel, value) {
  for (const b of document.querySelectorAll(`${sel} button`)) b.classList.toggle('on', b.dataset.value === value);
}

const LOOK_LABELS = {
  camera: 'Cámara', mouse: 'Ratón', forward: 'Al frente', left: 'Su izquierda', right: 'Su derecha',
  up: 'Arriba', down: 'Abajo', none: 'Libre',
};
const HAND_LABELS = { open: 'Abierta', relaxed: 'Relajada', fist: 'Puño', point: 'Señalar', peace: 'Paz ✌', thumbs_up: 'Pulgar 👍' };
const CAMERA_LABELS = { face: 'Rostro', bust: 'Busto', full: 'Cuerpo', wide: 'Amplio' };
const BG_LABELS = { gradient: 'Degradado', green: 'Verde (chroma)', blue: 'Azul', magenta: 'Magenta', transparent: 'Transparente' };

const intensity = () => +$('#expr-int').value / 100;
$('#expr-int').oninput = () => { $('#expr-int-out').textContent = `${$('#expr-int').value}%`; };
chips('#emotion-chips', [...EMOTIONS, 'neutral'].map((e) => [e, EMOTION_LABELS[e]]), (e) => runLocal({ type: 'expression', name: e, value: intensity() }));
chips('#gesture-chips', Object.entries(GESTURES).map(([k, g]) => [k, g.label]), (g) => runLocal({ type: 'gesture', name: g }));
chips('#pose-chips', Object.entries(POSES).map(([k, p]) => [k, p.label]), (p) => runLocal({ type: 'pose', name: p }));
chips('#look-chips', Object.entries(LOOK_LABELS), (l) => runLocal({ type: 'look', at: l }));
chips('#hand-chips', HAND_SHAPES.map((h) => [h, HAND_LABELS[h]]), (h) => runLocal({ type: 'hand', side: $('#hand-side').value, shape: h }));
chips('#camera-chips', CAMERA_PRESETS.map((c) => [c, CAMERA_LABELS[c]]), (c) => runLocal({ type: 'camera', preset: c }));
chips('#bg-chips', BACKGROUNDS.map((b) => [b, BG_LABELS[b]]), (b) => runLocal({ type: 'background', mode: b }));
syncChips('#emotion-chips', 'neutral'); syncChips('#pose-chips', 'relaxed'); syncChips('#look-chips', 'camera');
syncChips('#camera-chips', cameraPreset); syncChips('#bg-chips', 'gradient');

// hablar
function fillSelect(sel, items) {
  const el = $(sel);
  el.replaceChildren(...items.map(([v, l]) => { const o = document.createElement('option'); o.value = v; o.textContent = l; return o; }));
}
fillSelect('#say-emotion', [['', 'Sin emoción'], ...EMOTIONS.map((e) => [e, EMOTION_LABELS[e]])]);
fillSelect('#say-gesture', [['', 'Sin gesto'], ...Object.entries(GESTURES).map(([k, g]) => [k, g.label])]);
$('#say-btn').onclick = () => {
  const c = { type: 'speak', text: $('#say-text').value };
  if ($('#say-emotion').value) c.emotion = $('#say-emotion').value;
  if ($('#say-gesture').value) c.gesture = $('#say-gesture').value;
  runLocal(c);
};

// huesos
function populateBoneSelect() {
  const order = ['hips', 'spine', 'chest', 'upperChest', 'neck', 'head'];
  const sorted = [...availableBones].sort((a, b) => {
    const ia = order.indexOf(a), ib = order.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return isFinger(a) - isFinger(b) || a.localeCompare(b);
  });
  fillSelect('#bone-select', sorted.map((b) => [b, b]));
  $('#bone-select').value = availableBones.includes('head') ? 'head' : sorted[0];
  syncBoneSliders();
}
function syncBoneSliders() {
  const s = boneState.get($('#bone-select').value);
  const v = s ? (s.to || s.cur) : { x: 0, y: 0, z: 0 };
  for (const a of AXES) {
    const deg = Math.round(v[a] / DEG);
    $(`#b${a}`).value = deg;
    $(`#b${a}-out`).textContent = `${deg}°`;
  }
}
$('#bone-select').onchange = syncBoneSliders;
for (const a of AXES) {
  $(`#b${a}`).oninput = () => {
    const bone = $('#bone-select').value;
    if (!bone || !vrm) return;
    $(`#b${a}-out`).textContent = `${$(`#b${a}`).value}°`;
    setBone(bone, { [a]: +$(`#b${a}`).value }, 0.08);
    currentPose = 'custom';
  };
  $(`#b${a}`).onchange = () => logEntry('manual', { type: 'bones', rotations: { [$('#bone-select').value]: { [a]: +$(`#b${a}`).value } } }, { ok: true });
}
$('#bone-zero').onclick = () => {
  const bone = $('#bone-select').value;
  if (bone) runLocal({ type: 'bones', rotations: { [bone]: { x: 0, y: 0, z: 0 } } }).then(syncBoneSliders);
};

// mover
for (const b of document.querySelectorAll('[data-move]')) {
  b.onclick = () => {
    const v = b.dataset.move;
    if (v === 'center') return runLocal({ type: 'move', x: 0, z: 0 });
    const [x, z] = v.split(',').map(Number);
    runLocal({ type: 'move', x, z, relative: true });
  };
}
for (const b of document.querySelectorAll('[data-turn]')) {
  b.onclick = () => {
    const deg = +b.dataset.turn;
    runLocal(deg === 0 ? { type: 'move', rotation: 0 } : { type: 'move', rotation: deg, relative: true });
  };
}

// consola
const EXAMPLES = {
  'Saludo completo': {
    type: 'sequence',
    steps: [
      { type: 'speak', text: '¡Hola! Bienvenidos al stream.', emotion: 'happy', gesture: 'wave' },
      { type: 'gesture', name: 'nod' },
      { type: 'speak', text: 'Hoy vamos a hacer algo muy divertido.', emotion: 'relaxed' },
      { type: 'gesture', name: 'cheer' },
    ],
  },
  'Mover huesos libremente': { type: 'bones', rotations: { head: { y: 25, z: 10 }, rightUpperArm: { z: -30 }, rightLowerArm: { z: -60 } }, duration: 0.6 },
  'Paseo por el escenario': {
    type: 'sequence',
    steps: [
      { type: 'move', rotation: 90, duration: 0.5 },
      { type: 'move', x: 0.8 },
      { type: 'move', rotation: 0, duration: 0.5 },
      { type: 'gesture', name: 'point', side: 'left' },
      { type: 'move', rotation: -90, duration: 0.5 },
      { type: 'move', x: 0 },
      { type: 'move', rotation: 0, duration: 0.5 },
    ],
  },
  'Mezcla de expresiones': { type: 'expression', values: { happy: 0.5, surprised: 0.6 }, hold: 2 },
  'Bailar en bucle': { type: 'gesture', name: 'dance', loop: true },
  'Detener todo': { type: 'stop', what: 'all' },
  'Modo OBS (fondo verde)': [{ type: 'background', mode: 'green' }, { type: 'camera', preset: 'bust' }],
};
fillSelect('#example-select', [['', 'Ejemplos…'], ...Object.keys(EXAMPLES).map((k) => [k, k])]);
$('#console-input').value = JSON.stringify(EXAMPLES['Saludo completo'], null, 2);
$('#example-select').onchange = () => {
  const ex = EXAMPLES[$('#example-select').value];
  if (ex) $('#console-input').value = JSON.stringify(ex, null, 2);
};
$('#console-run').onclick = () => {
  let data;
  try { data = JSON.parse($('#console-input').value); } catch (e) {
    logEntry('manual', { error: 'JSON inválido' }, { ok: false, error: e.message });
    return;
  }
  runLocal(data);
};
$('#console-format').onclick = () => {
  try { $('#console-input').value = JSON.stringify(JSON.parse($('#console-input').value), null, 2); } catch {}
};
$('#console-input').addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); $('#console-run').click(); }
});

const endpoint = `${location.origin}/api/command`;
$('#endpoint').textContent = `POST ${endpoint}`;
$('#copy-curl').onclick = async () => {
  const body = JSON.stringify({ type: 'speak', text: 'Hola desde la IA', emotion: 'happy', gesture: 'wave' });
  const cmd = `curl -X POST ${endpoint} -H "Content-Type: application/json" -d '${body}'`;
  try { await navigator.clipboard.writeText(cmd); $('#copy-curl').textContent = '¡Copiado!'; }
  catch { $('#copy-curl').textContent = 'No se pudo copiar'; }
  setTimeout(() => { $('#copy-curl').textContent = 'Copiar ejemplo curl'; }, 1500);
};

// pestañas
for (const tab of document.querySelectorAll('.tab')) {
  tab.onclick = () => {
    for (const t of document.querySelectorAll('.tab')) t.classList.toggle('active', t === tab);
    for (const p of document.querySelectorAll('.tab-page')) p.classList.toggle('active', p.dataset.page === tab.dataset.tab);
    try { localStorage.setItem('vtuber-tab', tab.dataset.tab); } catch {}
  };
}
try { const t = localStorage.getItem('vtuber-tab'); if (t) document.querySelector(`.tab[data-tab="${t}"]`)?.click(); } catch {}

// mostrar/ocultar panel
const togglePanel = () => { document.body.classList.toggle('panel-hidden'); setTimeout(resize, 0); };
$('#toggle-panel').onclick = togglePanel;
window.addEventListener('keydown', (e) => {
  if (e.key.toLowerCase() === 'h' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) togglePanel();
});

// voz
const voiceSel = $('#voice-select');
function populateVoices() {
  if (!('speechSynthesis' in window)) { voiceSel.innerHTML = '<option>TTS no disponible</option>'; return; }
  const voices = speechSynthesis.getVoices().slice().sort((a, b) => (b.lang.startsWith('es') - a.lang.startsWith('es')) || a.lang.localeCompare(b.lang));
  fillSelect('#voice-select', [['', 'Automática (español)'], ...voices.map((v) => [v.voiceURI, `${v.name} — ${v.lang}`])]);
  voiceSel.value = tts.voiceURI;
}
if ('speechSynthesis' in window) { populateVoices(); speechSynthesis.onvoiceschanged = populateVoices; }
voiceSel.onchange = () => { tts.voiceURI = voiceSel.value; saveTts(); };
$('#tts-on').checked = tts.enabled;
$('#tts-on').onchange = () => { tts.enabled = $('#tts-on').checked; saveTts(); updateAudioHint(); };
$('#pitch').value = Math.round(tts.pitch * 100);
$('#pitch-out').textContent = tts.pitch.toFixed(1);
$('#pitch').oninput = () => { tts.pitch = +$('#pitch').value / 100; $('#pitch-out').textContent = tts.pitch.toFixed(1); saveTts(); };
function updateAudioHint() { $('#audio-hint').hidden = tts.unlocked || !tts.enabled; }
const unlock = () => {
  if (tts.unlocked) return;
  tts.unlocked = true;
  updateAudioHint();
};
window.addEventListener('pointerdown', unlock, { once: true });
window.addEventListener('keydown', unlock, { once: true });
updateAudioHint();

// reposo y reset
$('#idle-on').onchange = () => runLocal({ type: 'idle', enabled: $('#idle-on').checked });
$('#blink-on').onchange = () => runLocal({ type: 'idle', blink: $('#blink-on').checked });
$('#reset-btn').onclick = () => runLocal({ type: 'reset' });

// ratón (para mirar al cursor)
canvas.addEventListener('pointermove', (e) => {
  const r = canvas.getBoundingClientRect();
  mouseNdc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
});

// modelos
async function refreshModelList() {
  let list = [];
  try { list = await (await fetch('/api/models')).json(); } catch {}
  fillSelect('#model-select', [['', 'Elegir modelo…'], ['sample', 'Modelo de ejemplo'], ...list.map((m) => [m.name, m.name])]);
}
refreshModelList();
$('#model-select').onchange = () => {
  const v = $('#model-select').value;
  if (v) runLocal({ type: 'load_model', url: v });
};
function loadFile(file) {
  if (!file || !/\.vrm$/i.test(file.name)) {
    logEntry('manual', { type: 'load_model', file: file?.name }, { ok: false, error: 'Elige un archivo .vrm' });
    return;
  }
  loadModel(URL.createObjectURL(file), file.name).catch(() => {});
}
$('#file-input').onchange = (e) => loadFile(e.target.files[0]);
let dragDepth = 0;
window.addEventListener('dragenter', (e) => { e.preventDefault(); dragDepth++; document.body.classList.add('dragging'); });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('dragging');
  loadFile(e.dataTransfer.files[0]);
});

// parámetros de URL: ?bg=green&camera=bust&model=archivo.vrm
if (params.get('bg')) {
  try { setBackground(params.get('bg')); } catch {}
} else if (OBS) {
  setBackground('transparent');
}
if (params.get('camera') && CAMERA_PRESETS.includes(params.get('camera'))) cameraPreset = params.get('camera');
else if (OBS) cameraPreset = 'bust';
syncChips('#camera-chips', cameraPreset);

// expuesto para depuración y para IAs que corren dentro de la página
window.vtuber = { execute: (c) => runLocal(c, 'api'), state: snapshot, COMMANDS, get vrm() { return vrm; } };

// arranque
const [startUrl, startLabel] = await resolveModelUrl(params.get('model') || 'sample');
loadModel(startUrl, startLabel).catch((err) => console.error(err));
