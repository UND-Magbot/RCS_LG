"use client";

import type { MapModeButtonProps } from "@/lib/types/monitoring";

export function MapModeButton({ mapMode, onMapModeChange }: MapModeButtonProps) {
  const is3d = mapMode === "3d";

  return (
    <button
      className={"overlay-btn"}
      onClick={() => onMapModeChange(is3d ? "2d" : "3d")}
    >
      {is3d ? "2D" : "3D"}
    </button>
  );
}
