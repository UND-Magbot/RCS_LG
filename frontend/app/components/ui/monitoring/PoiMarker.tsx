import type { CSSProperties } from "react";
import type { PoiMarkerData } from "@/lib/types/map-markers";

type Props = {
  data: PoiMarkerData;
  leftPercent: number;
  topPercent: number;
};

export function PoiMarker({ data, leftPercent, topPercent }: Props) {
  const style: CSSProperties = {
    "--marker-left": `${leftPercent}%`,
    "--marker-top": `${topPercent}%`,
  } as CSSProperties;
  const isCharging = data.type === "charging";
  const renderKind = data.renderKind ?? (isCharging ? "circle" : "triangle");
  const isTriangle = renderKind === "triangle";
  const className = isTriangle
    ? "map-marker map-marker--poi map-marker--poi-triangle"
    : "map-marker map-marker--poi";

  return (
    <div
      className={className}
      data-poi-type={data.type}
      style={style}
    >
      {isTriangle ? (
        <div className="map-marker__triangle" />
      ) : (
        <div className="map-marker__dot">
          <span className="map-marker__dot-core" />
        </div>
      )}
      <span className="map-marker__label">{data.label}</span>
    </div>
  );
}
