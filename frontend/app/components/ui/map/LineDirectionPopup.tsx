"use client";

import type { LineDirectionPopupProps } from "@/lib/types/map";

export function LineDirectionPopup({
  position,
  onSelect,
  onCancel,
}: LineDirectionPopupProps) {
  return (
    <div
      className="line-direction-popup"
      style={{ left: position.x, top: position.y }}
    >
      <div className="line-direction-popup__title">방향 선택</div>
      <button
        className="line-direction-popup__option"
        onClick={() => onSelect("forward")}
      >
        <span className="line-direction-popup__option-icon">→</span>
        A → B (정방향)
      </button>
      <button
        className="line-direction-popup__option"
        onClick={() => onSelect("backward")}
      >
        <span className="line-direction-popup__option-icon">←</span>
        B → A (역방향)
      </button>
      <button
        className="line-direction-popup__option"
        onClick={() => onSelect("bidirectional")}
      >
        <span className="line-direction-popup__option-icon">↔</span>
        A ↔ B (양방향)
      </button>
      <button className="line-direction-popup__cancel" onClick={onCancel}>
        취소
      </button>
    </div>
  );
}
