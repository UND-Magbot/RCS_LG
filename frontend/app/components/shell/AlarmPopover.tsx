"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Image from "next/image";
import { IconButton } from "../ui/IconButton";
import { AlarmSearchPopup } from "./AlarmSearchPopup";
import { ALARM_ERROR_TYPE_LABELS, getErrorTypeFromCode } from "@/lib/constants/alarm";
import { getAlarmLogs, markAsRead, markAllAsRead } from "@/lib/api/alarm-log";
import { useAlert } from "@/lib/context/AlertContext";
import type { AlarmLogResponse } from "@/lib/types/alarm-log";
import type { AlarmErrorType } from "@/lib/types/shell";

function speakAlarm(alarm: AlarmLogResponse): void {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return;

  window.speechSynthesis.cancel();

  const errorType = alarm.error_type as AlarmErrorType;
  const errorLabel = ALARM_ERROR_TYPE_LABELS[errorType] ?? alarm.error_type_name;
  const sn = alarm.robot_sn ?? "";
  const text = sn
    ? `${sn} ${errorLabel} 발생했으니 확인부탁드립니다.`
    : `${errorLabel} 발생했으니 확인부탁드립니다.`;

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "ko-KR";
  utterance.rate = 1.0;
  utterance.pitch = 1.0;

  const voices = window.speechSynthesis.getVoices();
  const koreanVoice = voices.find((v) => v.lang.startsWith("ko"));
  if (koreanVoice) utterance.voice = koreanVoice;

  window.speechSynthesis.speak(utterance);
}

type AlarmPopoverProps = {
  iconSrc?: string;
};

export function AlarmPopover({
  iconSrc = "/icon/Icon_v2 (41).png",
}: AlarmPopoverProps = {}) {
  const [open, setOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [soundOn, setSoundOn] = useState(true);
  const [keyword, setKeyword] = useState("");
  const [alarms, setAlarms] = useState<AlarmLogResponse[]>([]);
  const triggerRef = useRef<HTMLDivElement>(null);

  const {
    unreadCount,
    refreshUnreadCount,
    decrementUnreadCount,
    resetUnreadCount,
  } = useAlert();

  // ── 알람 목록 조회 (당일 KST, 읽지 않은 것만) ──
  const fetchAlarms = useCallback(() => {
    getAlarmLogs({ is_read: false, limit: 100 })
      .then((res) => setAlarms(res.items))
      .catch(() => {});
  }, []);

  // 마운트 + 30초 폴링
  useEffect(() => {
    fetchAlarms();
    const timer = setInterval(() => {
      fetchAlarms();
      refreshUnreadCount();
    }, 30_000);
    return () => clearInterval(timer);
  }, [fetchAlarms, refreshUnreadCount]);

  const hasUnread = unreadCount > 0;

  // Neon effect
  useEffect(() => {
    document.body.removeAttribute("data-alarm-urgent");
    if (hasUnread) {
      document.body.setAttribute("data-alarm-neon", "true");
    } else {
      document.body.removeAttribute("data-alarm-neon");
    }
    return () => {
      document.body.removeAttribute("data-alarm-neon");
    };
  }, [hasUnread]);

  // Preload voices for TTS
  useEffect(() => {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.getVoices();
      const handler = () => window.speechSynthesis.getVoices();
      window.speechSynthesis.addEventListener("voiceschanged", handler);
      return () =>
        window.speechSynthesis.removeEventListener("voiceschanged", handler);
    }
  }, []);

  const handleClose = useCallback(() => {
    setOpen(false);
  }, []);

  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };

    const handleClickOutside = (e: MouseEvent) => {
      if (
        triggerRef.current &&
        !triggerRef.current.contains(e.target as Node)
      ) {
        handleClose();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [open, handleClose]);

  const handleSearchKeyword = useCallback(() => {
    // keyword 필터는 useMemo에서 자동 적용
  }, []);

  // 키워드 필터
  const displayAlarms = useMemo(() => {
    if (!keyword) return alarms;
    const k = keyword.toLowerCase();
    return alarms.filter(
      (a) =>
        a.message.toLowerCase().includes(k) ||
        a.error_code.toLowerCase().includes(k) ||
        (a.robot_sn ?? "").toLowerCase().includes(k) ||
        (a.source ?? "").toLowerCase().includes(k)
    );
  }, [alarms, keyword]);

  const noAlarms = displayAlarms.length === 0;

  const handleOpenSearch = () => {
    if (alarms.length === 0) return;
    setOpen(false);
    setSearchOpen(true);
  };

  const handleReadAlarm = useCallback(
    (id: number) => {
      markAsRead([id])
        .then(() => {
          setAlarms((prev) => prev.filter((a) => a.id !== id));
          decrementUnreadCount();
        })
        .catch(() => {});
    },
    [decrementUnreadCount]
  );

  const handleReadAll = useCallback(() => {
    markAllAsRead()
      .then(() => {
        setAlarms([]);
        resetUnreadCount();
      })
      .catch(() => {});
  }, [resetUnreadCount]);

  return (
    <>
      <div className="alarm-trigger" ref={triggerRef}>
        <IconButton
          className="alarm-trigger__btn"
          variant="ghost"
          aria-label="Open alarms"
          onClick={() => {
            setOpen((v) => !v);
            if (!open) fetchAlarms();
          }}
        >
          <Image
            src={iconSrc}
            alt=""
            width={20}
            height={20}
            className="alarm-trigger__icon"
          />
        </IconButton>
        {unreadCount > 0 ? (
          <span className="alarm-badge alarm-badge--error">
            {unreadCount > 99 ? "99+" : unreadCount}
          </span>
        ) : null}

        {open ? (
          <div className="alarm-popover">
            <div className="alarm-popover__toolbar">
              <div className="alarm-popover__toolbar-row">
                <input
                  type="text"
                  className="alarm-popover__search-input"
                  placeholder="오류 메시지, 오류코드, 로봇 SN"
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") handleSearchKeyword(); }}
                />
                <IconButton
                  className="alarm-popover__toolbar-btn"
                  variant="ghost"
                  aria-label="검색"
                  onClick={handleSearchKeyword}
                >
                  <img
                    src="/icon/search.png"
                    alt=""
                    className="alarm-popover__search-icon"
                  />
                </IconButton>
              </div>
              <div className="alarm-popover__toolbar-row">
                <button
                  type="button"
                  className={`alarm-popover__action-btn${noAlarms ? " alarm-popover__action-btn--disabled" : ""}`}
                  onClick={() => {
                    if (!noAlarms) handleReadAll();
                  }}
                  aria-disabled={noAlarms}
                >
                  전체 읽음
                </button>
                <button
                  type="button"
                  className={`alarm-popover__action-btn${alarms.length === 0 ? " alarm-popover__action-btn--disabled" : ""}`}
                  onClick={handleOpenSearch}
                  aria-disabled={alarms.length === 0}
                >
                  알림 이력
                </button>
                <IconButton
                  className={`alarm-popover__toolbar-btn${noAlarms ? " alarm-popover__toolbar-btn--disabled" : ""}`}
                  variant="ghost"
                  aria-label={soundOn ? "Mute alarm sound" : "Enable alarm sound"}
                  aria-disabled={noAlarms}
                  onClick={() => {
                    if (!noAlarms) setSoundOn((v) => !v);
                  }}
                >
                  <img
                    src={
                      soundOn
                        ? "/icon/sound-btn.png"
                        : "/icon/sound-btn-off.png"
                    }
                    alt={soundOn ? "Sound on" : "Sound off"}
                    className="alarm-popover__sound-icon"
                  />
                </IconButton>
              </div>
            </div>
            <div className="alarm-popover__list">
              {displayAlarms.length === 0 ? (
                <div className="alarm-popover__empty">
                  오늘 자 알람이 없습니다.
                </div>
              ) : (
                displayAlarms.map((alarm) => {
                  const errorType = alarm.error_type as AlarmErrorType;
                  const errorLabel =
                    ALARM_ERROR_TYPE_LABELS[errorType] ??
                    alarm.error_type_name;
                  const severity = alarm.severity === "error" ? "warning" : "info";

                  return (
                    <div
                      key={alarm.id}
                      className={`alarm-item alarm-item--severity-${severity}`}
                    >
                      <div className="alarm-item__header">
                        <span
                          className={`alarm-item__code-severity alarm-item__severity--${severity}`}
                        >
                          [{alarm.error_code}] {errorLabel}
                        </span>
                      </div>
                      <p className="alarm-item__message">
                        {alarm.robot_sn
                          ? `[${alarm.robot_sn}] ${alarm.message}`
                          : alarm.message}
                      </p>

                      <div className="alarm-item__footer">
                        <span className="alarm-item__timestamp">
                          {alarm.created_at.replace("T", " ")}
                          {alarm.source ? ` · ${alarm.source}` : ""}
                        </span>
                        <div className="alarm-item__footer-actions">
                          <button
                            type="button"
                            className="alarm-item__read-btn"
                            aria-label="읽음"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleReadAlarm(alarm.id);
                            }}
                          >
                            읽음
                          </button>
                          <button
                            type="button"
                            className={`alarm-item__tts-btn${!soundOn ? " alarm-item__tts-btn--disabled" : ""}`}
                            aria-label="알람 읽기"
                            aria-disabled={!soundOn}
                            onClick={(e) => {
                              e.stopPropagation();
                              if (soundOn) speakAlarm(alarm);
                            }}
                          >
                            <img
                              src={
                                soundOn
                                  ? "/icon/sound-btn.png"
                                  : "/icon/sound-btn-off.png"
                              }
                              alt={soundOn ? "Sound on" : "Sound off"}
                              className="alarm-item__tts-icon"
                            />
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        ) : null}
      </div>

      {searchOpen ? (
        <AlarmSearchPopup onClose={() => setSearchOpen(false)} />
      ) : null}
    </>
  );
}
