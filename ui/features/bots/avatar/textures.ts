import * as THREE from "three";

/**
 * Surface textures, generated on a canvas the first time they are asked for.
 *
 * Real objects are not perfectly smooth: injection-moulded plastic has micro-grain,
 * machined aluminium has brush lines, glass has faint smudges. Those variations in
 * roughness and height are most of what separates a product render from a CG toy,
 * and none of them needs an image download — they are noise, drawn once, shared by
 * every bot, and tiled.
 */

let cache: Record<string, THREE.Texture> | null = null;

function canvas(size: number, draw: (ctx: CanvasRenderingContext2D, size: number) => void) {
  const el = document.createElement("canvas");
  el.width = el.height = size;
  const ctx = el.getContext("2d");
  if (ctx) draw(ctx, size);
  return el;
}

function dataTexture(el: HTMLCanvasElement, repeat = 1): THREE.Texture {
  const t = new THREE.CanvasTexture(el);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.repeat.set(repeat, repeat);
  t.colorSpace = THREE.NoColorSpace;
  t.anisotropy = 4;
  t.needsUpdate = true;
  return t;
}

/** Value noise: random cells drawn small, then scaled up smoothly, plus fine grain. */
function noise(size: number, cells: number, lo: number, hi: number, grain: number) {
  const small = canvas(cells, (ctx, n) => {
    const img = ctx.createImageData(n, n);
    for (let i = 0; i < n * n; i++) {
      const v = lo + Math.random() * (hi - lo);
      img.data.set([v, v, v, 255], i * 4);
    }
    ctx.putImageData(img, 0, 0);
  });
  return canvas(size, (ctx, n) => {
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(small, 0, 0, n, n);
    if (grain > 0) {
      const img = ctx.getImageData(0, 0, n, n);
      for (let i = 0; i < img.data.length; i += 4) {
        const d = (Math.random() - 0.5) * grain;
        img.data[i] += d;
        img.data[i + 1] += d;
        img.data[i + 2] += d;
      }
      ctx.putImageData(img, 0, 0);
    }
  });
}

function brushed(size: number) {
  return canvas(size, (ctx, n) => {
    ctx.fillStyle = "rgb(150,150,150)";
    ctx.fillRect(0, 0, n, n);
    for (let i = 0; i < 1400; i++) {
      const y = Math.random() * n;
      const v = 110 + Math.random() * 90;
      ctx.strokeStyle = `rgba(${v},${v},${v},${0.15 + Math.random() * 0.35})`;
      ctx.lineWidth = Math.random() < 0.9 ? 0.6 : 1.4;
      const x = Math.random() * n;
      const len = n * (0.3 + Math.random() * 0.9);
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + len, y + (Math.random() - 0.5) * 1.5);
      ctx.stroke();
      // Wrap so the texture tiles without a visible seam.
      if (x + len > n) {
        ctx.beginPath();
        ctx.moveTo(x - n, y);
        ctx.lineTo(x + len - n, y);
        ctx.stroke();
      }
    }
  });
}

/** An LED as seen through glass: a bright core falling off to the rim. */
function ledFalloff(size: number) {
  return canvas(size, (ctx, n) => {
    const g = ctx.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2);
    g.addColorStop(0, "rgb(255,255,255)");
    g.addColorStop(0.45, "rgb(230,230,230)");
    g.addColorStop(1, "rgb(95,95,95)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, n, n);
  });
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
  cache = {
    /** Moulded plastic: gentle roughness variation, very fine grain for bump. */
    plasticRough: dataTexture(noise(256, 24, 200, 255, 10), 3),
    plasticBump: dataTexture(noise(256, 96, 110, 145, 26), 6),
    /** Soft-touch matte: a coarser, more visible grain. */
    matteBump: dataTexture(noise(256, 128, 90, 165, 40), 8),
    /** Brushed aluminium, along one axis. */
    brushedRough: dataTexture(brushed(512), 2),
    /** Glass: faint low-frequency smudges in the roughness. */
    smudge: dataTexture(noise(256, 10, 0, 70, 0), 1),
    led: dataTexture(ledFalloff(64), 1),
    shadow,
  };
  return cache;
}
