#!/usr/bin/env node
// Servidor MCP por stdio: permite que Claude (Desktop o Code) u otro cliente MCP
// controle el avatar. Reenvía cada herramienta al servidor local (server.js).
//
//   Claude Code:     claude mcp add vtuber -- node "RUTA/vtuber-ai/mcp-server.js"
//   Claude Desktop:  ver README.md
//
// Variable opcional: VTUBER_URL (por defecto http://127.0.0.1:3939)
import { createInterface } from 'node:readline';
import { handleRpc } from './lib/mcp.js';

const BASE = (process.env.VTUBER_URL || 'http://127.0.0.1:3939').replace(/\/$/, '');
const log = (...a) => console.error('[vtuber-mcp]', ...a); // stdout es solo para JSON-RPC

const notRunning = () =>
  new Error(`No se pudo conectar con el estudio VTuber en ${BASE}. Inicia el servidor con "npm start" en la carpeta vtuber-ai y abre ${BASE} en el navegador.`);

const ctx = {
  async dispatch(commands) {
    let res;
    try {
      res = await fetch(`${BASE}/api/command`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Source': 'mcp' },
        body: JSON.stringify(commands),
      });
    } catch { throw notRunning(); }
    return res.json();
  },
  async getState() {
    try { return await (await fetch(`${BASE}/api/state`)).json(); }
    catch { throw notRunning(); }
  },
};

const write = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', async (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try { msg = JSON.parse(line); } catch {
    write({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
    return;
  }
  const msgs = Array.isArray(msg) ? msg : [msg];
  const out = (await Promise.all(msgs.map((m) => handleRpc(m, ctx)))).filter(Boolean);
  if (!out.length) return;
  write(Array.isArray(msg) ? out : out[0]);
});
rl.on('close', () => process.exit(0));
log(`listo, reenviando a ${BASE}`);
