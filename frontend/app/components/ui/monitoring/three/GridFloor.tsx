"use client";

import { useMemo } from "react";
import * as THREE from "three";

/**
 * Static ground plane with a dark grid texture matching the reference design.
 * Uses CanvasTexture (no shader) to avoid Moiré / flicker on camera rotation.
 */
export function GridFloor() {
  const texture = useMemo(() => {
    const size = 256;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d")!;

    // Background — lighter than map interior (#0d1525) for clear contrast
    ctx.fillStyle = "#141e2e";
    ctx.fillRect(0, 0, size, size);

    // Grid lines
    const step = 32; // 8 cells per tile
    ctx.strokeStyle = "#2a3f55";
    ctx.lineWidth = 1;
    for (let i = 0; i <= size; i += step) {
      ctx.beginPath();
      ctx.moveTo(i, 0);
      ctx.lineTo(i, size);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, i);
      ctx.lineTo(size, i);
      ctx.stroke();
    }

    const tex = new THREE.CanvasTexture(canvas);
    tex.wrapS = THREE.RepeatWrapping;
    tex.wrapT = THREE.RepeatWrapping;
    tex.repeat.set(60, 60);
    tex.colorSpace = THREE.SRGBColorSpace;
    // Prevent mipmap shimmer
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    return tex;
  }, []);

  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.1, 0]}>
      <planeGeometry args={[10000, 10000]} />
      <meshBasicMaterial map={texture} />
    </mesh>
  );
}
