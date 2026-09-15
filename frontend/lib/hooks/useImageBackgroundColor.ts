"use client";

import { useEffect, useState } from "react";

const SAMPLE_SIZE = 100;

function sampleCornerColor(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number
): string {
  const offsets: [number, number][] = [
    [0, 0],
    [w - 1, 0],
    [0, h - 1],
    [w - 1, h - 1],
  ];

  let r = 0;
  let g = 0;
  let b = 0;

  for (const [x, y] of offsets) {
    const px = ctx.getImageData(x, y, 1, 1).data;
    r += px[0];
    g += px[1];
    b += px[2];
  }

  return `rgb(${Math.round(r / 4)}, ${Math.round(g / 4)}, ${Math.round(b / 4)})`;
}

export function useImageBackgroundColor(src: string): string | null {
  const [color, setColor] = useState<string | null>(null);

  useEffect(() => {
    if (!src) {
      setColor(null);
      return;
    }

    let cancelled = false;

    const img = new Image();

    img.onload = () => {
      if (cancelled) return;

      const canvas = document.createElement("canvas");
      canvas.width = SAMPLE_SIZE;
      canvas.height = SAMPLE_SIZE;

      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      ctx.drawImage(img, 0, 0, SAMPLE_SIZE, SAMPLE_SIZE);

      if (!cancelled) {
        setColor(sampleCornerColor(ctx, SAMPLE_SIZE, SAMPLE_SIZE));
      }
    };

    img.onerror = () => {
      if (!cancelled) setColor(null);
    };

    img.src = src;

    return () => {
      cancelled = true;
    };
  }, [src]);

  return color;
}
