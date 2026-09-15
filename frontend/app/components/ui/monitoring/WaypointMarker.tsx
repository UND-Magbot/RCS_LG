import type { CSSProperties } from "react";
import type { WaypointMarkerData } from "@/lib/types/map-markers";

type Props = {
  data: WaypointMarkerData;
  leftPercent: number;
  topPercent: number;
};

export function WaypointMarker({ data, leftPercent, topPercent }: Props) {
  const style: CSSProperties = {
    "--marker-left": `${leftPercent}%`,
    "--marker-top": `${topPercent}%`,
  } as CSSProperties;
  const isAllocated = data.id.startsWith("alloc-");
  const className = isAllocated
    ? "map-marker map-marker--waypoint map-marker--waypoint-allocated"
    : "map-marker map-marker--waypoint";

  return (
    <div
      className={className}
      data-waypoint-type={isAllocated ? "allocated" : "navigation"}
      style={style}
      title={data.label}
    >
      <div className="map-marker__dot" />
    </div>
  );
}
