import type { CSSProperties } from "react";
import type { RobotMarkerData } from "@/lib/types/map-markers";

type Props = {
  data: RobotMarkerData;
  leftPercent: number;
  topPercent: number;
};

function radToDeg(rad: number): number {
  return -(rad * 180) / Math.PI + 90;
}

export function RobotMarker({ data, leftPercent, topPercent }: Props) {
  const style: CSSProperties = {
    "--marker-left": `${leftPercent}%`,
    "--marker-top": `${topPercent}%`,
    "--robot-yaw": `${radToDeg(data.yaw)}deg`,
  } as CSSProperties;

  const statusModifier =
    data.status === "error" || data.status === "warning"
      ? `map-marker--robot-${data.status}`
      : "";
  const collisionModifier =
    data.collisionState && data.collisionState !== "none"
      ? `map-marker--robot-${data.collisionState}`
      : "";

  return (
    <div
      className={`map-marker map-marker--robot ${statusModifier} ${collisionModifier}`.trim()}
      style={style}
    >
      <div className="map-marker__body" aria-hidden="true">
        {/* 방향 화살표 (네비게이션 커서) */}
        <svg
          className="map-marker__robot-arrow"
          width="20"
          height="24"
          viewBox="0 0 20 24"
        >
          <path
            d="M10 0 L20 22 L10 15 L0 22 Z"
            className="map-marker__robot-dir"
          />
        </svg>
        {/* 바디 (둥근 직사각형) */}
        <svg
          className="map-marker__robot-icon"
          width="22"
          height="22"
          viewBox="0 0 22 22"
        >
          <rect
            x="1"
            y="0"
            width="20"
            height="22"
            rx="3"
            className="map-marker__robot-body"
          />
          {/* LED 스트립 (우측) */}
          <rect
            x="18"
            y="2"
            width="2.5"
            height="18"
            rx="1"
            className="map-marker__robot-led"
          />
        </svg>
      </div>
      <span className="map-marker__label">{data.robotName}</span>
    </div>
  );
}
