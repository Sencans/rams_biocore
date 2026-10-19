// VTuber AI Studio — servidor local sin dependencias.
//   - Sirve el visor (public/) y los modelos (models/)
//   - API HTTP para que cualquier IA mande comandos:  POST /api/command
//   - Empuja los comandos al navegador por SSE:       GET  /api/events
//   - Endpoint MCP (Streamable HTTP, JSON):           POST /mcp
import http from 'node:http';
import { readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { HELP, COMMAND_TYPES } from './lib/protocol.js';
import { handleRpc } from './lib/mcp.js';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(ROOT, 'public');
const MODELS_DIR = path.join(ROOT, 'models');
const PORT = Number(process.env.PORT) || 3939;
const HOST = process.env.HOST || '127.0.0.1';
const MAX_BODY = 1024 * 1024;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.vrm': 'model/gltf-binary',
  '.glb': 'model/gltf-binary',
};

const viewers = new Set();      // { id, res, obs }
const pending = new Map();      // commandId -> resolve(ack)
const history = [];             // últimos comandos (para depurar)
let avatarState = { ready: false, note: 'Todavía no hay ningún visor conectado.' };

// ---------------------------------------------------------------- utilidades
function send(res, status, body, headers = {}) {
  const isObj = typeof body === 'object' && !(body instanceof Buffer);
  res.writeHead(status, {
    'Content-Type': isObj ? 'application/json; charset=utf-8' : 'text/plain; charset=utf-8',
    'Cache-Control': 'no-store',
    ...headers,
  });
  res.end(isObj ? JSON.stringify(body, null, 2) : body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(new Error('Cuerpo demasiado grande')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim();
      if (!raw) return resolve(null);
      try { resolve(JSON.parse(raw)); } catch { reject(new Error('JSON inválido')); }
    });
    req.on('error', reject);
  });
}

// Protección contra DNS rebinding: solo aceptamos Host local.
function isLocalHost(req) {
  const host = (req.headers.host || '').replace(/:\d+$/, '').replace(/^\[|\]$/g, '');
  return ['localhost', '127.0.0.1', '::1'].includes(host) || host === HOST;
}

function broadcast(event, data) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const v of viewers) v.res.write(payload);
}

function normalizeCommands(body) {
  if (body && !Array.isArray(body) && Array.isArray(body.commands)) body = body.commands;
  const list = Array.isArray(body) ? body : [body];
  if (!list.length) throw new Error('No se recibió ningún comando.');
  for (const c of list) {
    if (!c || typeof c !== 'object' || typeof c.type !== 'string') {
      throw new Error('Cada comando debe ser un objeto con "type". Ver GET /api/help');
    }
    if (!COMMAND_TYPES.includes(c.type)) {
      throw new Error(`Tipo desconocido "${c.type}". Tipos válidos: ${COMMAND_TYPES.join(', ')}`);
    }
  }
  return list;
}

/** Envía comandos a los visores y espera la confirmación del primero que responda. */
export async function dispatch(body, source = 'api') {
  const commands = normalizeCommands(body);
  if (viewers.size === 0) {
    return {
      ok: false,
      error: `No hay ningún visor abierto. Abre http://localhost:${PORT} en el navegador para ver y mover el avatar.`,
    };
  }
  const id = randomUUID();
  const waitMs = commands.some((c) => c.type === 'load_model') ? 60000 : 2500;
  const ack = new Promise((resolve) => {
    const timer = setTimeout(() => { pending.delete(id); resolve(null); }, waitMs);
    pending.set(id, (data) => { clearTimeout(timer); pending.delete(id); resolve(data); });
  });
  broadcast('command', { id, source, commands });
  history.push({ at: new Date().toISOString(), source, commands });
  if (history.length > 100) history.shift();

  const result = await ack;
  if (!result) return { ok: true, delivered: viewers.size, confirmed: false, note: 'Enviado, pero el visor no confirmó a tiempo.' };
  const results = result.results || [];
  return { ok: results.every((r) => r.ok), delivered: viewers.size, results };
}

const mcpCtx = {
  dispatch: (cmds) => dispatch(cmds, 'mcp'),
  getState: async () => ({ viewers: viewers.size, ...avatarState }),
};

async function serveFile(res, filePath, baseDir) {
  const resolved = path.resolve(baseDir, filePath);
  if (!resolved.startsWith(path.resolve(baseDir) + path.sep) && resolved !== path.resolve(baseDir)) {
    return send(res, 403, 'Prohibido');
  }
  try {
    const info = await stat(resolved);
    if (!info.isFile()) return send(res, 404, 'No encontrado');
    const data = await readFile(resolved);
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(resolved).toLowerCase()] || 'application/octet-stream',
      'Cache-Control': 'no-cache',
    });
    res.end(data);
  } catch {
    send(res, 404, 'No encontrado');
  }
}

// ---------------------------------------------------------------- rutas
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const p = url.pathname;

  if (!isLocalHost(req)) return send(res, 403, { ok: false, error: 'Host no permitido' });

  // CORS solo para la API de comandos/estado (útil para IAs que corren en otras páginas locales)
  if (p.startsWith('/api/')) {
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    if (req.method === 'OPTIONS') return send(res, 204, '');
  }

  try {
    // --- SSE para los visores
    if (p === '/api/events' && req.method === 'GET') {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-store',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
      });
      const viewer = { id: randomUUID(), res, obs: url.searchParams.has('obs') };
      viewers.add(viewer);
      res.write(`event: hello\ndata: ${JSON.stringify({ id: viewer.id, viewers: viewers.size })}\n\n`);
      const ping = setInterval(() => res.write(': ping\n\n'), 15000);
      req.on('close', () => { clearInterval(ping); viewers.delete(viewer); });
      return;
    }

    if (p === '/api/command' && req.method === 'POST') {
      let body;
      try { body = await readBody(req); } catch (e) { return send(res, 400, { ok: false, error: e.message }); }
      let result;
      try { result = await dispatch(body, req.headers['x-source'] || 'api'); }
      catch (e) { return send(res, 400, { ok: false, error: e.message }); }
      return send(res, result.ok === false && !result.results ? 409 : 200, result);
    }

    if (p === '/api/ack' && req.method === 'POST') {
      const body = await readBody(req);
      const resolve = body && pending.get(body.id);
      if (resolve) resolve(body);
      return send(res, 200, { ok: true });
    }

    if (p === '/api/state') {
      if (req.method === 'POST') {
        const body = await readBody(req);
        if (body && typeof body === 'object') avatarState = { ...body, updatedAt: new Date().toISOString() };
        return send(res, 200, { ok: true });
      }
      return send(res, 200, { viewers: viewers.size, ...avatarState });
    }

    if (p === '/api/help') return send(res, 200, HELP);
    if (p === '/api/log') return send(res, 200, history);

    if (p === '/api/models') {
      let files = [];
      try { files = (await readdir(MODELS_DIR)).filter((f) => /\.vrm$/i.test(f)); } catch {}
      return send(res, 200, files.map((f) => ({ name: f, url: `/models/${encodeURIComponent(f)}` })));
    }

    // --- MCP (Streamable HTTP, solo respuestas JSON)
    if (p === '/mcp') {
      if (req.method !== 'POST') return send(res, 405, { error: 'Usa POST' }, { Allow: 'POST' });
      let body;
      try { body = await readBody(req); } catch (e) {
        return send(res, 400, { jsonrpc: '2.0', id: null, error: { code: -32700, message: e.message } });
      }
      const msgs = Array.isArray(body) ? body : [body];
      const out = (await Promise.all(msgs.map((m) => handleRpc(m, mcpCtx)))).filter(Boolean);
      if (!out.length) return send(res, 202, '');
      return send(res, 200, Array.isArray(body) ? out : out[0]);
    }

    // --- archivos estáticos
    if (req.method !== 'GET' && req.method !== 'HEAD') return send(res, 405, 'Método no permitido');
    if (p === '/protocol.js') return serveFile(res, 'protocol.js', path.join(ROOT, 'lib'));
    if (p.startsWith('/models/')) return serveFile(res, decodeURIComponent(p.slice('/models/'.length)), MODELS_DIR);
    const rel = p === '/' ? 'index.html' : decodeURIComponent(p.slice(1));
    return serveFile(res, rel, PUBLIC_DIR);
  } catch (err) {
    console.error(err);
    if (!res.headersSent) send(res, 500, { ok: false, error: String(err?.message || err) });
  }
});

server.listen(PORT, HOST, () => {
  const base = `http://localhost:${PORT}`;
  console.log('');
  console.log('  VTuber AI Studio');
  console.log(`  Visor ............ ${base}`);
  console.log(`  Vista para OBS ... ${base}/?obs=1`);
  console.log(`  API de comandos .. POST ${base}/api/command`);
  console.log(`  Ayuda ............ ${base}/api/help`);
  console.log(`  MCP (HTTP) ....... ${base}/mcp`);
  console.log('');
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`El puerto ${PORT} está ocupado. Cierra la otra instancia o usa: PORT=4000 npm start`);
  } else {
    console.error(err);
  }
  process.exit(1);
});
