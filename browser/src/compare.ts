import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";

export type Mask = {
  x: number;
  y: number;
  width: number;
  height: number;
  reason: string;
};

export type CompareResult = {
  status: "pass" | "fail";
  reason?: string;
  width?: number;
  height?: number;
  designSize?: [number, number];
  appSize?: [number, number];
  mismatchedPixels?: number;
  diffRatio?: number;
  diffPng?: Buffer;
};

const MAX_MASK_RATIO = 0.02;

export function comparePng(
  design: Buffer,
  app: Buffer,
  options: { maxDiffRatio?: number; masks?: Mask[]; fit?: boolean } = {},
): CompareResult {
  let left: PNG = PNG.sync.read(design);
  let right: PNG = PNG.sync.read(app);
  if (left.width !== right.width || left.height !== right.height) {
    if (!options.fit) {
      return {
        status: "fail",
        reason: "size",
        width: right.width,
        height: right.height,
        designSize: [left.width, left.height],
        appSize: [right.width, right.height],
      };
    }
    const w = Math.max(left.width, right.width);
    const h = Math.max(left.height, right.height);
    left = padPng(left, w, h);
    right = padPng(right, w, h);
  }
  const masks = options.masks ?? [];
  if (masks.some((mask) => !mask.reason.trim())) {
    return { status: "fail", reason: "mask-without-reason", width: left.width, height: left.height };
  }
  const area = left.width * left.height;
  const maskArea = masks.reduce((sum, mask) => sum + mask.width * mask.height, 0);
  if (maskArea > area * MAX_MASK_RATIO) {
    return { status: "fail", reason: "mask-too-large", width: left.width, height: left.height };
  }
  for (const mask of masks) applyMask(left, mask);
  for (const mask of masks) applyMask(right, mask);
  const diff = new PNG({ width: left.width, height: left.height });
  const mismatchedPixels = pixelmatch(left.data, right.data, diff.data, left.width, left.height, {
    threshold: 0.1,
  });
  const diffRatio = area === 0 ? 1 : mismatchedPixels / area;
  const limit = options.maxDiffRatio ?? 0.002;
  return {
    status: diffRatio <= limit ? "pass" : "fail",
    reason: diffRatio <= limit ? undefined : "pixel-diff",
    width: left.width,
    height: left.height,
    mismatchedPixels,
    diffRatio,
    diffPng: PNG.sync.write(diff),
  };
}

function applyMask(png: PNG, mask: Mask): void {
  for (let y = mask.y; y < mask.y + mask.height; y += 1) {
    for (let x = mask.x; x < mask.x + mask.width; x += 1) {
      if (x < 0 || y < 0 || x >= png.width || y >= png.height) continue;
      const offset = (png.width * y + x) << 2;
      png.data[offset] = 0;
      png.data[offset + 1] = 0;
      png.data[offset + 2] = 0;
      png.data[offset + 3] = 255;
    }
  }
}

export function solidPng(width: number, height: number, rgba: [number, number, number, number]): Buffer {
  const png = new PNG({ width, height });
  for (let index = 0; index < width * height; index += 1) {
    png.data[index * 4] = rgba[0];
    png.data[index * 4 + 1] = rgba[1];
    png.data[index * 4 + 2] = rgba[2];
    png.data[index * 4 + 3] = rgba[3];
  }
  return PNG.sync.write(png);
}

/** Pad (top-left anchored) with transparent pixels to w x h; never resamples. */
export function padPng(png: PNG, width: number, height: number): PNG {
  if (png.width === width && png.height === height) return png;
  const out = new PNG({ width, height });
  PNG.bitblt(png, out, 0, 0, Math.min(png.width, width), Math.min(png.height, height), 0, 0);
  return out;
}
