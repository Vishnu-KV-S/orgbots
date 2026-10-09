import * as THREE from "three";

/**
 * Textures, generated on a canvas the first time they are asked for — no image
 * download. The finishes are smooth by design (a clear coat, polished chrome, glass),
 * so all that is left to draw is the soft shadow a bot casts on the floor.
 */

let cache: Record<string, THREE.Texture> | null = null;

function canvas(size: number, draw: (ctx: CanvasRenderingContext2D, size: number) => void) {
  const el = document.createElement("canvas");
  el.width = el.height = size;
  const ctx = el.getContext("2d");
  if (ctx) draw(ctx, size);
  return el;
}

/** The soft contact shadow under a bot. */
function contactShadow(size: number) {
  return canvas(size, (ctx, n) => {
    const g = ctx.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2);
    g.addColorStop(0, "rgba(0,0,0,0.55)");
    g.addColorStop(0.35, "rgba(0,0,0,0.32)");
    g.addColorStop(0.7, "rgba(0,0,0,0.08)");
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, n, n);
  });
}

export function textures() {
  if (cache) return cache;
  const shadow = new THREE.CanvasTexture(contactShadow(128));
  shadow.colorSpace = THREE.SRGBColorSpace;
  cache = { shadow };
  return cache;
}
