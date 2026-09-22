import type { MapPixelCoord } from "@/lib/types/map-markers";

/** Map image pixel coordinate → Three.js world coordinate (centered) */
export function mapPixelToWorld(
  px: MapPixelCoord,
  imgW: number,
  imgH: number
): [number, number, number] {
  return [px.x - imgW / 2, 0, px.y - imgH / 2];
}
