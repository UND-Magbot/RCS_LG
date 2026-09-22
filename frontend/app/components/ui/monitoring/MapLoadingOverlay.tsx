"use client";

import "./MapLoadingOverlay.css";

type Props = {
  visible: boolean;
};

export function MapLoadingOverlay({ visible }: Props) {
  if (!visible) return null;

  return (
    <div className="map-loading-overlay">
      <div className="map-loading-overlay__content">
        <div className="map-loading-overlay__spinner">
          <div className="map-loading-overlay__spinner-ring" />
        </div>
        <div className="map-loading-overlay__label">맵 전환 중...</div>
      </div>
    </div>
  );
}
