"use client";

import type { OverlayCardProps } from "@/lib/types/monitoring";

export function OverlayCard({ items, onItemToggle }: OverlayCardProps) {
  return (
    <div className="overlay-card">
      <div className="overlay-card__list">
        {items.map((item) => (
          <label className="overlay-item" key={item.label}>
            <input
              type="checkbox"
              checked={item.checked}
              onChange={(e) => onItemToggle(item.label, e.target.checked)}
            />
            <span>{item.label}</span>
          </label>
        ))}
      </div>
    </div>
  );
}
