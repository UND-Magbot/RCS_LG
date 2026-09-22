import { ALARM_ERROR_TYPE_LABELS } from "@/lib/constants/alarm";
import type { AlarmListItemProps } from "@/lib/types/shell";

export function AlarmListItem({
  code,
  severity,
  errorType,
  timestamp,
  robotSn,
  onSpeak,
  onRead,
  onClick,
}: AlarmListItemProps) {
  const errorLabel = ALARM_ERROR_TYPE_LABELS[errorType];

  return (
    <div
      className={`alarm-item alarm-item--severity-${severity}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
    >
      <div className="alarm-item__header">
        <span
          className={`alarm-item__code-severity alarm-item__severity--${severity}`}
        >
          [{code}] {errorLabel}
        </span>
      </div>
      <p className="alarm-item__message">
        [{robotSn}] [{errorLabel}] 발생했으니 확인부탁드립니다.
      </p>
      <div className="alarm-item__footer">
        <span className="alarm-item__timestamp">{timestamp}</span>
        <div className="alarm-item__footer-actions">
          {onRead ? (
            <button
              type="button"
              className="alarm-item__read-btn"
              aria-label="읽음"
              onClick={(e) => {
                e.stopPropagation();
                onRead();
              }}
            >
              읽음
            </button>
          ) : null}
          {onSpeak ? (
            <button
              type="button"
              className="alarm-item__tts-btn"
              aria-label="알람 읽기"
              onClick={(e) => {
                e.stopPropagation();
                onSpeak();
              }}
            >
              <img
                src="/icon/sound-btn.png"
                alt=""
                className="alarm-item__tts-icon"
              />
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
