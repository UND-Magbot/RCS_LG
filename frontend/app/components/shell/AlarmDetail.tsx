"use client";

import { useEffect, useRef } from "react";
import { IconButton } from "../ui/IconButton";
import { ALARM_ERROR_TYPE_LABELS } from "@/lib/constants/alarm";
import type { AlarmDetailProps } from "@/lib/types/shell";

export function AlarmDetail({ alarm, onClose }: AlarmDetailProps) {
  const overlayRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose();
  };

  return (
    <div
      className="alarm-detail__overlay"
      ref={overlayRef}
      onClick={handleOverlayClick}
    >
      <div className="alarm-detail" role="dialog" aria-label="Alarm detail">
        <div className="alarm-detail__header">
          <h2 className="alarm-detail__title">
            <span className={`alarm-item__severity--${alarm.severity}`}>
              [{alarm.code}] {ALARM_ERROR_TYPE_LABELS[alarm.errorType]}
            </span>
          </h2>
          <IconButton
            className="alarm-detail__close"
            variant="ghost"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </IconButton>
        </div>
        <div className="alarm-detail__body">
          <div className="alarm-detail__row">
            <span className="alarm-detail__label">Message</span>
            <span className="alarm-detail__value">{alarm.message}</span>
          </div>
          <div className="alarm-detail__row">
            <span className="alarm-detail__label">로봇 SN</span>
            <span className="alarm-detail__value">{alarm.robotSn}</span>
          </div>
          <div className="alarm-detail__row">
            <span className="alarm-detail__label">Occurred</span>
            <span className="alarm-detail__value">{alarm.occurredAt}</span>
          </div>
          {alarm.clearedAt ? (
            <div className="alarm-detail__row">
              <span className="alarm-detail__label">Cleared</span>
              <span className="alarm-detail__value">{alarm.clearedAt}</span>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
