"use client";

import { useMemo, useEffect, useState } from "react";
import * as THREE from "three";

type Props = {
  mapSrc: string;
  width: number;
  height: number;
};

/**
 * Ground plane showing the map image with the outside background removed
 * (edge flood-fill, same approach as the 2D MonitoringMapCanvas).
 */
export function MapFloor({ mapSrc, width, height }: Props) {
  const [texture, setTexture] = useState<THREE.CanvasTexture | null>(null);

  useEffect(() => {
    if (!mapSrc) return;
    let cancelled = false;

    const img = new Image();
    img.crossOrigin = "anonymous";
    img.src = mapSrc;

    img.onload = () => {
      if (cancelled) return;
      setTimeout(() => {
        if (cancelled) return;
        try {
          const processed = removeOutsideBackground(img);
          const tex = new THREE.CanvasTexture(processed);
          tex.colorSpace = THREE.SRGBColorSpace;
          tex.minFilter = THREE.LinearFilter;
          tex.magFilter = THREE.LinearFilter;
          tex.generateMipmaps = false;
          setTexture(tex);
        } catch {
          // ignore — canvas 텍스처 처리 실패 시 맵 바닥 생략
        }
      }, 0);
    };

    return () => {
      cancelled = true;
    };
  }, [mapSrc]);

  if (!texture) return null;

  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.05, 0]}>
      <planeGeometry args={[width, height]} />
      <meshBasicMaterial
        map={texture}
        transparent
        alphaTest={0.5}
        depthWrite
      />
    </mesh>
  );
}

/** Edge flood-fill: make outside background pixels transparent */
function removeOutsideBackground(
  img: HTMLImageElement,
  tolerance = 30
): HTMLCanvasElement {
  const w = img.naturalWidth;
  const h = img.naturalHeight;

  const offscreen = document.createElement("canvas");
  offscreen.width = w;
  offscreen.height = h;
  const ctx = offscreen.getContext("2d")!;
  ctx.drawImage(img, 0, 0);

  const imageData = ctx.getImageData(0, 0, w, h);
  const { data } = imageData;

  // Determine background color from 4 corners
  const corners = [
    0,
    (w - 1) * 4,
    (h - 1) * w * 4,
    ((h - 1) * w + (w - 1)) * 4,
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

  const tolSq = tolerance * tolerance;
  const matches = (i: number) => {
    const dr = data[i] - bgR;
    const dg = data[i + 1] - bgG;
    const db = data[i + 2] - bgB;
    return dr * dr + dg * dg + db * db < tolSq;
  };

  // BFS flood fill from edges
  const visited = new Uint8Array(w * h);
  const queue: number[] = [];

  for (let x = 0; x < w; x++) {
    if (matches(x * 4)) { queue.push(x); visited[x] = 1; }
    const bi = (h - 1) * w + x;
    if (matches(bi * 4)) { queue.push(bi); visited[bi] = 1; }
  }
  for (let y = 1; y < h - 1; y++) {
    const li = y * w;
    if (matches(li * 4)) { queue.push(li); visited[li] = 1; }
    const ri = y * w + (w - 1);
    if (matches(ri * 4)) { queue.push(ri); visited[ri] = 1; }
  }

  let head = 0;
  while (head < queue.length) {
    const idx = queue[head++];
    const px = idx % w;
    const py = (idx - px) / w;
    const neighbors = [
      py > 0 ? idx - w : -1,
      py < h - 1 ? idx + w : -1,
      px > 0 ? idx - 1 : -1,
      px < w - 1 ? idx + 1 : -1,
    ];
    for (const ni of neighbors) {
      if (ni < 0 || visited[ni]) continue;
      if (matches(ni * 4)) {
        visited[ni] = 1;
        queue.push(ni);
      }
    }
  }

  // Outside → transparent, Inside → solid fill (#1e3045)
  for (let i = 0; i < visited.length; i++) {
    const p = i * 4;
    if (visited[i]) {
      data[p + 3] = 0;
    } else {
      data[p] = 30;      // R
      data[p + 1] = 48;  // G
      data[p + 2] = 69;  // B  (#1e3045)
      data[p + 3] = 255; // A (opaque)
    }
  }

  // 경계선 스무딩 — 모폴로지 closing (dilate → erode) 으로 노이즈 제거
  const smoothed = new Uint8Array(visited);
  const SMOOTH_R = 3;
  for (let pass = 0; pass < SMOOTH_R; pass++) {
    const prev = new Uint8Array(smoothed);
    for (let i = 0; i < prev.length; i++) {
      if (prev[i]) continue;
      const px = i % w;
      const py = (i - px) / w;
      if (
        (py > 0 && prev[i - w]) ||
        (py < h - 1 && prev[i + w]) ||
        (px > 0 && prev[i - 1]) ||
        (px < w - 1 && prev[i + 1])
      ) {
        smoothed[i] = 1;
      }
    }
  }
  for (let pass = 0; pass < SMOOTH_R; pass++) {
    const prev = new Uint8Array(smoothed);
    for (let i = 0; i < prev.length; i++) {
      if (!prev[i]) continue;
      const px = i % w;
      const py = (i - px) / w;
      if (
        (py > 0 && !prev[i - w]) ||
        (py < h - 1 && !prev[i + w]) ||
        (px > 0 && !prev[i - 1]) ||
        (px < w - 1 && !prev[i + 1])
      ) {
        smoothed[i] = 0;
      }
    }
  }

  // 경계선 탐지 — smoothed 기준
  const border = new Uint8Array(w * h);
  for (let i = 0; i < smoothed.length; i++) {
    if (smoothed[i]) continue;
    const px = i % w;
    const py = (i - px) / w;
    if (
      (py > 0 && smoothed[i - w]) ||
      (py < h - 1 && smoothed[i + w]) ||
      (px > 0 && smoothed[i - 1]) ||
      (px < w - 1 && smoothed[i + 1])
    ) {
      border[i] = 1;
    }
  }

  // 3px 두께 — 경계 2차 확장
  const borderFinal = new Uint8Array(border);
  for (let expand = 0; expand < 2; expand++) {
    const prev = new Uint8Array(borderFinal);
    for (let i = 0; i < prev.length; i++) {
      if (!prev[i]) continue;
      const px = i % w;
      const py = (i - px) / w;
      if (py > 0 && !borderFinal[i - w] && !smoothed[i - w]) borderFinal[i - w] = 1;
      if (py < h - 1 && !borderFinal[i + w] && !smoothed[i + w]) borderFinal[i + w] = 1;
      if (px > 0 && !borderFinal[i - 1] && !smoothed[i - 1]) borderFinal[i - 1] = 1;
      if (px < w - 1 && !borderFinal[i + 1] && !smoothed[i + 1]) borderFinal[i + 1] = 1;
    }
  }

  // 경계 픽셀 색상 적용 (#3a6a9a)
  for (let i = 0; i < borderFinal.length; i++) {
    if (!borderFinal[i]) continue;
    const p = i * 4;
    data[p] = 58;
    data[p + 1] = 106;
    data[p + 2] = 154;
    data[p + 3] = 255;
  }

  ctx.putImageData(imageData, 0, 0);
  return offscreen;
}
