"use client";

import { useEffect, useState } from "react";

export type OccupancyResult = {
  /** Center positions [x, y, z] for each merged wall block (world coords, centered) */
  boxes: { x: number; y: number; z: number; w: number; h: number; d: number }[];
  imageWidth: number;
  imageHeight: number;
  wallHeight: number;
};

const DOWNSAMPLE = 4;
const LUMINANCE_THRESHOLD = 60;

/**
 * Load map image, classify wall pixels, greedy-merge into boxes.
 * Returns data for InstancedMesh rendering.
 */
export function useOccupancyGrid(mapSrc: string): OccupancyResult | null {
  const [result, setResult] = useState<OccupancyResult | null>(null);

  useEffect(() => {
    if (!mapSrc) {
      setResult(null);
      return;
    }

    let cancelled = false;

    const img = new Image();
    img.crossOrigin = "anonymous";
    img.src = mapSrc;

    img.onload = () => {
      if (cancelled) return;
      // Defer heavy processing so WebGL Canvas can initialize first
      setTimeout(() => {
        if (cancelled) return;
        try {
          const data = processImage(img);
          if (!cancelled) setResult(data);
        } catch {
          if (!cancelled) setResult(null);
        }
      }, 0);
    };

    img.onerror = () => {
      if (!cancelled) setResult(null);
    };

    return () => {
      cancelled = true;
    };
  }, [mapSrc]);

  return result;
}

function processImage(img: HTMLImageElement): OccupancyResult {
  const natW = img.naturalWidth;
  const natH = img.naturalHeight;

  // Draw to offscreen canvas
  const offscreen = document.createElement("canvas");
  offscreen.width = natW;
  offscreen.height = natH;
  const ctx = offscreen.getContext("2d")!;
  ctx.drawImage(img, 0, 0);

  const imageData = ctx.getImageData(0, 0, natW, natH);
  const { data } = imageData;

  // Downsampled grid dimensions
  const gw = Math.ceil(natW / DOWNSAMPLE);
  const gh = Math.ceil(natH / DOWNSAMPLE);
  const grid = new Uint8Array(gw * gh); // 1 = wall

  // --- Edge flood-fill to find outside background (same approach as 2D) ---
  const corners = [
    0,
    (natW - 1) * 4,
    (natH - 1) * natW * 4,
    ((natH - 1) * natW + (natW - 1)) * 4,
  ];
  let bgR = 0, bgG = 0, bgB = 0;
  for (const idx of corners) {
    bgR += data[idx];
    bgG += data[idx + 1];
    bgB += data[idx + 2];
  }
  bgR = Math.round(bgR / 4);
  bgG = Math.round(bgG / 4);
  bgB = Math.round(bgB / 4);

  const bgTolSq = 30 * 30;
  const isBg = (i: number) => {
    const dr = data[i] - bgR;
    const dg = data[i + 1] - bgG;
    const db = data[i + 2] - bgB;
    return dr * dr + dg * dg + db * db < bgTolSq;
  };

  // BFS from edges to mark outside pixels
  const outside = new Uint8Array(natW * natH);
  const queue: number[] = [];

  for (let x = 0; x < natW; x++) {
    if (isBg(x * 4)) { queue.push(x); outside[x] = 1; }
    const bi = (natH - 1) * natW + x;
    if (isBg(bi * 4)) { queue.push(bi); outside[bi] = 1; }
  }
  for (let y = 1; y < natH - 1; y++) {
    const li = y * natW;
    if (isBg(li * 4)) { queue.push(li); outside[li] = 1; }
    const ri = y * natW + (natW - 1);
    if (isBg(ri * 4)) { queue.push(ri); outside[ri] = 1; }
  }

  let head = 0;
  while (head < queue.length) {
    const idx = queue[head++];
    const px = idx % natW;
    const py = (idx - px) / natW;
    const neighbors = [
      py > 0 ? idx - natW : -1,
      py < natH - 1 ? idx + natW : -1,
      px > 0 ? idx - 1 : -1,
      px < natW - 1 ? idx + 1 : -1,
    ];
    for (const ni of neighbors) {
      if (ni < 0 || outside[ni]) continue;
      if (isBg(ni * 4)) {
        outside[ni] = 1;
        queue.push(ni);
      }
    }
  }

  // Classify downsampled cells as wall
  for (let gy = 0; gy < gh; gy++) {
    for (let gx = 0; gx < gw; gx++) {
      // Sample the center pixel of this cell
      const sx = Math.min(gx * DOWNSAMPLE + Math.floor(DOWNSAMPLE / 2), natW - 1);
      const sy = Math.min(gy * DOWNSAMPLE + Math.floor(DOWNSAMPLE / 2), natH - 1);
      const pi = sy * natW + sx;

      // Skip outside background pixels
      if (outside[pi]) continue;

      const i = pi * 4;
      const lum = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
      if (lum < LUMINANCE_THRESHOLD) {
        grid[gy * gw + gx] = 1;
      }
    }
  }

  // Greedy meshing: merge adjacent wall cells into rectangles
  const used = new Uint8Array(gw * gh);
  const rects: { x: number; y: number; w: number; h: number }[] = [];

  for (let gy = 0; gy < gh; gy++) {
    for (let gx = 0; gx < gw; gx++) {
      const gi = gy * gw + gx;
      if (!grid[gi] || used[gi]) continue;

      // Extend right
      let rw = 1;
      while (gx + rw < gw && grid[gy * gw + gx + rw] && !used[gy * gw + gx + rw]) {
        rw++;
      }

      // Extend down
      let rh = 1;
      outer: while (gy + rh < gh) {
        for (let dx = 0; dx < rw; dx++) {
          const ci = (gy + rh) * gw + gx + dx;
          if (!grid[ci] || used[ci]) break outer;
        }
        rh++;
      }

      // Mark used
      for (let dy = 0; dy < rh; dy++) {
        for (let dx = 0; dx < rw; dx++) {
          used[(gy + dy) * gw + gx + dx] = 1;
        }
      }

      rects.push({ x: gx, y: gy, w: rw, h: rh });
    }
  }

  // Convert grid rects → world-space boxes
  const wallHeight = Math.max(natW, natH) * 0.03;
  const halfW = natW / 2;
  const halfH = natH / 2;

  const boxes = rects.map((r) => {
    const worldX = (r.x + r.w / 2) * DOWNSAMPLE - halfW;
    const worldZ = (r.y + r.h / 2) * DOWNSAMPLE - halfH;
    return {
      x: worldX,
      y: wallHeight / 2,
      z: worldZ,
      w: r.w * DOWNSAMPLE,
      h: wallHeight,
      d: r.h * DOWNSAMPLE,
    };
  });

  return { boxes, imageWidth: natW, imageHeight: natH, wallHeight };
}
