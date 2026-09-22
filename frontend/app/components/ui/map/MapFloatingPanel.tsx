"use client";

import { useState, useEffect } from "react";
import type { MapFloatingPanelProps } from "@/lib/types/map";

export function MapFloatingPanel({
  open,
  onToggle,
  onStartMapping,
  onClearMap,
}: MapFloatingPanelProps) {
  const [visible, setVisible] = useState(open);
  const [animating, setAnimating] = useState<"in" | "out" | null>(null);

  useEffect(() => {
    if (open) {
      setVisible(true);
      setAnimating("in");
    } else if (visible) {
      setAnimating("out");
    }
  }, [open]);

  const handleAnimationEnd = () => {
    if (animating === "out") {
      setVisible(false);
    }
    setAnimating(null);
  };

  const contentClass = [
    "map-floating-panel__content",
    animating === "in" ? "map-floating-panel__content--slide-in" : "",
    animating === "out" ? "map-floating-panel__content--slide-out" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="map-floating-panel">
      {visible ? (
        <div className={contentClass} onAnimationEnd={handleAnimationEnd}>
          <button
            className="map-floating-panel__toggle"
            onClick={onToggle}
            title="패널 닫기"
          >
            ▸
          </button>

          {/* Header */}
          <div className="map-floating-panel__header">
            <h2 className="map-floating-panel__title">맵 편집</h2>
          </div>

          {/* Action buttons */}
          <div className="map-floating-panel__actions">
            <button
              className="btn btn--primary"
              onClick={onStartMapping}
            >
              맵핑 시작
            </button>
            <button
              className="btn btn--danger"
              onClick={onClearMap}
            >
              맵 초기화
            </button>
          </div>
        </div>
      ) : (
        <div
          className="map-floating-panel__collapsed-area"
          onMouseDown={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
        >
          <button
            className="map-floating-panel__toggle map-floating-panel__toggle--collapsed"
            onClick={onToggle}
            title="패널 열기"
          >
            ◂
          </button>
        </div>
      )}
    </div>
  );
}
