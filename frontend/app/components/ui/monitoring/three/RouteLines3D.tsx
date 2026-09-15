"use client";

import { memo, useMemo } from "react";
import { Line } from "@react-three/drei";
import type {
  RouteSegment,
  WaypointMarkerData,
  MapPixelCoord,
} from "@/lib/types/map-markers";
import { mapPixelToWorld } from "./mapCoords";

type Props = {
  segments: RouteSegment[];
  routeWaypoints: WaypointMarkerData[];
  waypoints?: WaypointMarkerData[];
  imgW: number;
  imgH: number;
  showArrows: boolean;
};

function toWorld(
  p: MapPixelCoord,
  imgW: number,
  imgH: number,
  y = 0.5
): [number, number, number] {
  const [x, , z] = mapPixelToWorld(p, imgW, imgH);
  return [x, y, z];
}

/** Sample a quadratic/cubic bezier into line segments */
function sampleCurve(
  from: MapPixelCoord,
  to: MapPixelCoord,
  cps: MapPixelCoord[],
  imgW: number,
  imgH: number,
  steps: number
): [number, number, number][] {
  const pts: [number, number, number][] = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    let x: number, y: number;

    if (cps.length === 1) {
      // Quadratic
      const mt = 1 - t;
      x = mt * mt * from.x + 2 * mt * t * cps[0].x + t * t * to.x;
      y = mt * mt * from.y + 2 * mt * t * cps[0].y + t * t * to.y;
    } else {
      // Cubic
      const mt = 1 - t;
      x =
        mt * mt * mt * from.x +
        3 * mt * mt * t * cps[0].x +
        3 * mt * t * t * cps[1].x +
        t * t * t * to.x;
      y =
        mt * mt * mt * from.y +
        3 * mt * mt * t * cps[0].y +
        3 * mt * t * t * cps[1].y +
        t * t * t * to.y;
    }

    const [wx, , wz] = mapPixelToWorld({ x, y }, imgW, imgH);
    pts.push([wx, 0.5, wz]);
  }
  return pts;
}

function RouteLines3DInner({
  segments,
  routeWaypoints,
  waypoints,
  imgW,
  imgH,
  showArrows,
}: Props) {
  const lines = useMemo(() => {
    if (segments.length > 0) {
      return segments.filter((seg) => seg.lineType !== "firewall").map((seg) => {
        let points: [number, number, number][];
        if (
          seg.lineType === "curve" &&
          seg.controlPoints &&
          seg.controlPoints.length > 0
        ) {
          points = sampleCurve(seg.from, seg.to, seg.controlPoints, imgW, imgH, 20);
        } else {
          points = [toWorld(seg.from, imgW, imgH), toWorld(seg.to, imgW, imgH)];
        }
        return { id: seg.id, points, direction: seg.direction };
      });
    }

    // Fallback: polyline from waypoints
    const source =
      routeWaypoints.length > 0 ? routeWaypoints : (waypoints ?? []);
    const staticWps = source.filter((wp) => !wp.id.startsWith("alloc-"));
    if (staticWps.length < 2) return [];

    const points = staticWps.map((wp) => toWorld(wp.position, imgW, imgH));
    return [{ id: "polyline", points, direction: "forward" as const }];
  }, [segments, routeWaypoints, waypoints, imgW, imgH]);

  // Direction arrows at midpoints
  const arrows = useMemo(() => {
    if (!showArrows) return [];
    return lines
      .map((line) => {
        if (line.points.length < 2) return null;
        const mid = Math.floor(line.points.length / 2);
        const a = line.points[Math.max(0, mid - 1)];
        const b = line.points[Math.min(line.points.length - 1, mid)];
        const mx = (a[0] + b[0]) / 2;
        const mz = (a[2] + b[2]) / 2;
        const angle = Math.atan2(b[2] - a[2], b[0] - a[0]);
        return { mx, mz, angle, direction: line.direction, id: line.id };
      })
      .filter(Boolean) as {
      mx: number;
      mz: number;
      angle: number;
      direction: string;
      id: string;
    }[];
  }, [lines, showArrows]);

  return (
    <>
      {lines.map((line) => (
        <Line
          key={line.id}
          points={line.points}
          color="#19bc7e"
          lineWidth={3}
          transparent
          opacity={0.7}
        />
      ))}

      {arrows.map((a) => {
        const baseRotation = -a.angle;
        return (
          <group key={`arrow-${a.id}`}>
            {a.direction === "bidirectional" ? (
              <>
                <mesh
                  position={[a.mx + Math.sin(a.angle) * 5, 2, a.mz - Math.cos(a.angle) * 5]}
                  rotation={[0, baseRotation, -Math.PI / 2]}
                >
                  <coneGeometry args={[2, 6, 4]} />
                  <meshBasicMaterial color="#19bc7e" transparent opacity={0.9} />
                </mesh>
                <mesh
                  position={[a.mx - Math.sin(a.angle) * 5, 2, a.mz + Math.cos(a.angle) * 5]}
                  rotation={[0, baseRotation + Math.PI, -Math.PI / 2]}
                >
                  <coneGeometry args={[2, 6, 4]} />
                  <meshBasicMaterial color="#19bc7e" transparent opacity={0.9} />
                </mesh>
              </>
            ) : (
              <mesh
                position={[a.mx, 2, a.mz]}
                rotation={[
                  0,
                  a.direction === "backward" ? baseRotation + Math.PI : baseRotation,
                  -Math.PI / 2,
                ]}
              >
                <coneGeometry args={[2, 6, 4]} />
                <meshBasicMaterial color="#19bc7e" transparent opacity={0.9} />
              </mesh>
            )}
          </group>
        );
      })}
    </>
  );
}

export const RouteLines3D = memo(RouteLines3DInner);
