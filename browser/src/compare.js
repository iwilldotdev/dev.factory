// Pixel work of the `visual` command: crop the design PNG, mask, fit, diff. Pure functions over pngjs bitmaps [L-54, L-55].
import { PNG } from "pngjs";
import pixelmatch from "pixelmatch";

export const decode = (buf) => PNG.sync.read(buf);
export const encode = (png) => PNG.sync.write(png);

export function crop(png, x, y, w, h) {
  if (x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > png.width || y + h > png.height) {
    throw new Error(`crop ${x},${y},${w},${h} does not fit inside the ${png.width}x${png.height} design image`);
  }
  const out = new PNG({ width: w, height: h });
  PNG.bitblt(png, out, x, y, w, h, 0, 0);
  return out;
}

/** Bilinear resize, used only when a check declares `fit`: the capture is scaled to the design crop. */
export function scaleTo(png, w, h) {
  const out = new PNG({ width: w, height: h });
  for (let y = 0; y < h; y++) {
    const sy = Math.min(png.height - 1, ((y + 0.5) * png.height) / h - 0.5);
    const y0 = Math.max(0, Math.floor(sy)), y1 = Math.min(png.height - 1, y0 + 1), fy = Math.max(0, sy - y0);
    for (let x = 0; x < w; x++) {
      const sx = Math.min(png.width - 1, ((x + 0.5) * png.width) / w - 0.5);
      const x0 = Math.max(0, Math.floor(sx)), x1 = Math.min(png.width - 1, x0 + 1), fx = Math.max(0, sx - x0);
      for (let c = 0; c < 4; c++) {
        const at = (px, py) => png.data[(py * png.width + px) * 4 + c];
        const top = at(x0, y0) * (1 - fx) + at(x1, y0) * fx, bottom = at(x0, y1) * (1 - fx) + at(x1, y1) * fx;
        out.data[(y * w + x) * 4 + c] = Math.round(top * (1 - fy) + bottom * fy);
      }
    }
  }
  return out;
}

export function paint(png, rects, rgba = [255, 0, 255, 255]) {
  for (const { x, y, w, h } of rects) {
    for (let j = Math.max(0, y); j < Math.min(png.height, y + h); j++) {
      for (let i = Math.max(0, x); i < Math.min(png.width, x + w); i++) png.data.set(rgba, (j * png.width + i) * 4);
    }
  }
}

export function parseMask(text) {
  const [x, y, w, h] = String(text).split(",").map(Number);
  if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) throw new Error(`bad mask ${text}: expected x,y,w,h`);
  return { x, y, w, h };
}

/**
 * Compare a capture with the design crop. Returns { ok, reason, ratio, scale, images } where images are the PNGs to keep.
 * A size mismatch fails with reason `size` unless `fit` scales the capture to the design [L-55].
 */
export function compare(app, design, { masks = [], fit = false, maxDiffRatio = 0.03 }) {
  let scale = null;
  if (app.width !== design.width || app.height !== design.height) {
    if (!fit) {
      return { ok: false, reason: "size", ratio: null, scale, text: `size mismatch: capture ${app.width}x${app.height}, design ${design.width}x${design.height}`, images: { app, design } };
    }
    scale = { x: design.width / app.width, y: design.height / app.height };
    app = scaleTo(app, design.width, design.height);
  }
  const a = new PNG({ width: app.width, height: app.height }), d = new PNG({ width: design.width, height: design.height });
  app.data.copy(a.data);
  design.data.copy(d.data);
  paint(a, masks);
  paint(d, masks);
  const diff = new PNG({ width: design.width, height: design.height });
  const count = pixelmatch(a.data, d.data, diff.data, design.width, design.height, { threshold: 0.1, includeAA: false });
  const ratio = count / (design.width * design.height);
  const ok = ratio <= maxDiffRatio;
  return { ok, reason: ok ? null : "diff", ratio, scale, text: `diff ratio ${ratio.toFixed(4)} ${ok ? "within" : "above"} ${maxDiffRatio}`, images: { app, design, diff } };
}
