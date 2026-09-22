"use client";

import type { AlarmSearchItemProps } from "@/lib/types/alarm-search";
import { ALARM_ERROR_TYPE_LABELS } from "@/lib/constants/alarm";
import type { AlarmErrorType } from "@/lib/types/shell";

export function AlarmSearchItem({ item }: AlarmSearchItemProps) {
  const errorType = item.error_type as AlarmErrorType;
  const errorLabel = ALARM_ERROR_TYPE_LABELS[errorType] ?? item.error_type_name;
  const severityClass =
    item.severity === "error"
      ? "alarm-search-item__status--warning"
      : "alarm-search-item__status--resume";

  return (
    <div className="alarm-search-item">
      <div className="alarm-search-item__row">
        <span className="alarm-search-item__dot" />
        <span className="alarm-search-item__line">
          <span className="alarm-search-item__code">[{item.error_code}]</span>{" "}
          <span className={severityClass}>{errorLabel}</span>{" "}
          {item.robot_sn ? (
            <span className="alarm-search-item__sn">{item.robot_sn}</span>
          ) : null}{" "}
          <span className="alarm-search-item__message">{item.message}</span>
        </span>
      </div>
      <div className="alarm-search-item__timestamp">
        {item.created_at.replace("T", " ").substring(0, 19)}
      </div>
    </div>
  );
}
