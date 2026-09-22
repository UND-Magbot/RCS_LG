"use client";

import { useState } from "react";
import type { LineEditPopupProps, LineDirection } from "@/lib/types/map";

const directions: { value: LineDirection; label: string; icon: string }[] = [
  { value: "forward", label: "A \u2192 B (정방향)", icon: "\u2192" },
  { value: "backward", label: "B \u2192 A (역방향)", icon: "\u2190" },
  { value: "bidirectional", label: "A \u2194 B (양방향)", icon: "\u2194" },
];

export function LineEditPopup({
  line,
  fromPoiName,
  toPoiName,
  onUpdate,
  onDelete,
  onClose,
}: LineEditPopupProps) {
  const [direction, setDirection] = useState<LineDirection>(line.direction);

  const handleConfirm = () => {
    onUpdate(line.id, { direction });
    onClose();
  };

  return (
    <div className="poi-edit-overlay" onClick={onClose}>
      <div className="poi-edit-panel" onClick={(e) => e.stopPropagation()}>
        <h3 className="poi-edit-panel__title">라인 편집</h3>

        <div className="poi-edit-panel__body">
          {/* From / To (read-only) */}
          <div className="poi-edit-panel__field">
            <label className="poi-edit-panel__label">시작점</label>
            <input
              className="poi-edit-panel__input poi-edit-panel__input--readonly"
              value={fromPoiName}
              readOnly
            />
          </div>

          <div className="poi-edit-panel__field">
            <label className="poi-edit-panel__label">끝점</label>
            <input
              className="poi-edit-panel__input poi-edit-panel__input--readonly"
              value={toPoiName}
              readOnly
            />
          </div>

          {/* Line Type (read-only) */}
          <div className="poi-edit-panel__field">
            <label className="poi-edit-panel__label">유형</label>
            <input
              className="poi-edit-panel__input poi-edit-panel__input--readonly"
              value={line.lineType === "curve" ? "곡선" : "직선"}
              readOnly
            />
          </div>

          {/* Direction */}
          <div className="poi-edit-panel__section">
            <span className="poi-edit-panel__section-title">방향</span>
            <div className="poi-edit-panel__radio-group">
              {directions.map((d) => (
                <label key={d.value} className="poi-edit-panel__radio">
                  <input
                    type="radio"
                    name="lineDirection"
                    value={d.value}
                    checked={direction === d.value}
                    onChange={() => setDirection(d.value)}
                  />
                  <span>{d.icon} {d.label}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        {/* Actions */}
        <div className="poi-edit-panel__actions">
          <button
            className="poi-edit-panel__btn poi-edit-panel__btn--delete"
            onClick={() => onDelete(line.id)}
          >
            삭제
          </button>
          <button
            className="poi-edit-panel__btn poi-edit-panel__btn--confirm"
            onClick={handleConfirm}
          >
            확인
          </button>
        </div>
      </div>
    </div>
  );
}
