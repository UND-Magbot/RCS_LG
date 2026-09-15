"use client";

import { memo, useMemo } from "react";
import type { RouteSegment } from "@/lib/types/map-markers";
import { mapPixelToWorld } from "./mapCoords";

type Props = {
  segments: RouteSegment[];
  imgW: number;
  imgH: number;
};

function FirewallWall3DInner({ segments, imgW, imgH }: Props) {
  const walls = useMemo(() => {
    const base = Math.max(imgW, imgH);
    const wallHeight = base * 0.04;
    const wallThickness = base * 0.004; // 위에서 봤을 때 굵기
    return segments
      .filter((seg) => seg.lineType === "firewall")
      .map((seg) => {
        const [sx, , sz] = mapPixelToWorld(seg.from, imgW, imgH);
        const [ex, , ez] = mapPixelToWorld(seg.to, imgW, imgH);
        const midX = (sx + ex) / 2;
        const midZ = (sz + ez) / 2;
        const length = Math.hypot(ex - sx, ez - sz);
        const angle = Math.atan2(ez - sz, ex - sx);
        return { id: seg.id, midX, midZ, length, angle, wallHeight, wallThickness };
      });
  }, [segments, imgW, imgH]);

  return (
    <>
      {walls.map((w) => (
        <mesh
          key={w.id}
          position={[w.midX, w.wallHeight / 2, w.midZ]}
          rotation={[0, -w.angle, 0]}
        >
          <boxGeometry args={[w.length, w.wallHeight, w.wallThickness]} />
          <meshStandardMaterial
            color="#ff8c00"
            transparent
            opacity={0.75}
            roughness={0.4}
            metalness={0.1}
          />
        </mesh>
      ))}
    </>
  );
}

export const FirewallWall3D = memo(FirewallWall3DInner);
