"use client";

import { useEffect, useRef } from "react";

type DrawCallback = (
  ctx: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  timestamp: number
) => void;

export function useCanvasLoop(
  canvasRef: React.RefObject<HTMLCanvasElement | null>,
  draw: DrawCallback
) {
  const drawRef = useRef(draw);
  drawRef.current = draw;

  useEffect(() => {
    let frameId = 0;

    const loop = (timestamp: number) => {
      const canvas = canvasRef.current;
      if (!canvas) {
        frameId = requestAnimationFrame(loop);
        return;
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        frameId = requestAnimationFrame(loop);
        return;
      }
      drawRef.current(ctx, canvas, timestamp);
      frameId = requestAnimationFrame(loop);
    };

    frameId = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frameId);
  }, [canvasRef]);
}
