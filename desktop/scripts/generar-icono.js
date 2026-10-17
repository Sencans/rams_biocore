'use strict';
/**
 * Genera `assets/tray.png` y `assets/tray-camara.png` (32×32, RGBA) sin dependencias: disco oscuro
 * con aro carmesí y una "A" clara, al estilo del sistema Amadeus; la variante "cámara" añade un punto
 * rojo con borde blanco abajo a la derecha (indicador de cámara activa en la bandeja, §C2.13).
 * Rasteriza con supermuestreo 4×4 y escribe el PNG a mano (zlib + CRC32).
 * Uso: `npm run icono` (los dos) o `node scripts/generar-icono.js [ruta] [tamaño] [--camara]`.
 */
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

const ARGS = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const TAM = Number(ARGS[1]) || 32;
const SOLO_CAMARA = process.argv.includes('--camara');
const SS = 4; // supermuestreo por eje

const FONDO = [20, 22, 30];
const ARO = [208, 52, 60];
const LETRA = [242, 236, 240];
const PUNTO = [255, 48, 48];
const BORDE_PUNTO = [255, 255, 255];
const CENTRO_PUNTO = [0.76, 0.76];

function distSegmento(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/** Color de un punto en coordenadas normalizadas 0..1 (`null` = transparente). */
function muestra(u, v, camara) {
  if (camara) {
    const rp = Math.hypot(u - CENTRO_PUNTO[0], v - CENTRO_PUNTO[1]);
    if (rp < 0.16) return PUNTO;
    if (rp < 0.23) return BORDE_PUNTO;
  }
  const r = Math.hypot(u - 0.5, v - 0.5);
  if (r > 0.49) return null;
  // "A": dos trazos y un travesaño.
  const grosor = 0.055;
  const cima = [0.5, 0.2];
  const izq = [0.29, 0.76];
  const der = [0.71, 0.76];
  const enA = distSegmento(u, v, cima[0], cima[1], izq[0], izq[1]) < grosor ||
              distSegmento(u, v, cima[0], cima[1], der[0], der[1]) < grosor ||
              distSegmento(u, v, 0.38, 0.56, 0.62, 0.56) < grosor * 0.85;
  if (enA) return LETRA;
  if (r > 0.42) return ARO;
  return FONDO;
}

function rasterizar(tam, camara) {
  const px = Buffer.alloc(tam * tam * 4);
  for (let y = 0; y < tam; y++) {
    for (let x = 0; x < tam; x++) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const c = muestra((x + (sx + 0.5) / SS) / tam, (y + (sy + 0.5) / SS) / tam, camara);
          if (!c) continue;
          r += c[0];
          g += c[1];
          b += c[2];
          a += 1;
        }
      }
      const i = (y * tam + x) * 4;
      if (a > 0) {
        px[i] = Math.round(r / a);
        px[i + 1] = Math.round(g / a);
        px[i + 2] = Math.round(b / a);
        px[i + 3] = Math.round((255 * a) / (SS * SS));
      }
    }
  }
  return px;
}

const TABLA_CRC = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = TABLA_CRC[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function bloque(tipo, datos) {
  const largo = Buffer.alloc(4);
  largo.writeUInt32BE(datos.length);
  const td = Buffer.concat([Buffer.from(tipo, 'ascii'), datos]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([largo, td, crc]);
}

function png(tam, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(tam, 0);
  ihdr.writeUInt32BE(tam, 4);
  ihdr[8] = 8; // bits por canal
  ihdr[9] = 6; // RGBA
  const filas = Buffer.alloc((tam * 4 + 1) * tam);
  for (let y = 0; y < tam; y++) {
    filas[y * (tam * 4 + 1)] = 0; // filtro "none"
    rgba.copy(filas, y * (tam * 4 + 1) + 1, y * tam * 4, (y + 1) * tam * 4);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    bloque('IHDR', ihdr),
    bloque('IDAT', zlib.deflateSync(filas, { level: 9 })),
    bloque('IEND', Buffer.alloc(0)),
  ]);
}

function escribir(salida, camara) {
  fs.mkdirSync(path.dirname(salida), { recursive: true });
  fs.writeFileSync(salida, png(TAM, rasterizar(TAM, camara)));
  console.log(`icono escrito en ${salida} (${TAM}×${TAM}${camara ? ', cámara' : ''})`);
}

if (ARGS[0]) {
  escribir(ARGS[0], SOLO_CAMARA);
} else {
  const assets = path.join(__dirname, '..', 'assets');
  escribir(path.join(assets, 'tray.png'), false);
  escribir(path.join(assets, 'tray-camara.png'), true);
}
