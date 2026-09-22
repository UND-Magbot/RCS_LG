"use client";

import type { LayerButtonProps } from "@/lib/types/monitoring";

export function LayerButton({ isActive, onToggle }: LayerButtonProps) {
  return (
    <button
      className={isActive ? "overlay-btn overlay-btn--active" : "overlay-btn"}
      onClick={onToggle}
    >
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <polygon points="12 2 2 7 12 12 22 7 12 2" />
        <polyline points="2 17 12 22 22 17" />
        <polyline points="2 12 12 17 22 12" />
      </svg>
    </button>
  );
}
