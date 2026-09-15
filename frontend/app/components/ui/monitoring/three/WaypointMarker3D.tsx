"use client";

import { memo } from "react";
import type { WaypointMarkerData } from "@/lib/types/map-markers";
import { mapPixelToWorld } from "./mapCoords";

type Props = {
  waypoint: WaypointMarkerData;
  imgW: number;
  imgH: number;
};

function WaypointMarker3DInner({ waypoint, imgW, imgH }: Props) {
  const [x, , z] = mapPixelToWorld(waypoint.position, imgW, imgH);
  const isAllocated = waypoint.id.startsWith("alloc-");

  const radius = isAllocated ? 3 : 4;
  const color = isAllocated ? "#aaedff" : "#8eddff";

  return (
    <mesh position={[x, 1.5, z]}>
      <sphereGeometry args={[radius, 8, 8]} />
      <meshStandardMaterial
        color={color}
        emissive={color}
        emissiveIntensity={0.6}
        transparent
        opacity={0.9}
      />
    </mesh>
  );
}

export const WaypointMarker3D = memo(WaypointMarker3DInner);
