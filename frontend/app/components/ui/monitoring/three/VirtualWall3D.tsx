"use client";

import { memo, useMemo } from "react";
import * as THREE from "three";
import type { VirtualWallData } from "@/lib/types/map-markers";
import { mapPixelToWorld } from "./mapCoords";

type Props = {
  wall: VirtualWallData;
  imgW: number;
  imgH: number;
};

function VirtualWall3DInner({ wall, imgW, imgH }: Props) {
  const [sx, , sz] = mapPixelToWorld(wall.start, imgW, imgH);
  const [ex, , ez] = mapPixelToWorld(wall.end, imgW, imgH);

  const { midX, midZ, length, angle, wallHeight } = useMemo(() => {
    const mx = (sx + ex) / 2;
    const mz = (sz + ez) / 2;
    const len = Math.hypot(ex - sx, ez - sz);
    const a = Math.atan2(ez - sz, ex - sx);
    const wh = Math.max(imgW, imgH) * 0.025;
    return { midX: mx, midZ: mz, length: len, angle: a, wallHeight: wh };
  }, [sx, sz, ex, ez, imgW, imgH]);

  return (
    <mesh
      position={[midX, wallHeight / 2, midZ]}
      rotation={[0, -angle, 0]}
    >
      <planeGeometry args={[length, wallHeight]} />
      <meshBasicMaterial
        color="#ff5050"
        transparent
        opacity={0.45}
        side={THREE.DoubleSide}
      />
    </mesh>
  );
}

export const VirtualWall3D = memo(VirtualWall3DInner);
