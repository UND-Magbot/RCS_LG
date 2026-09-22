"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import type { TimeRangePickerProps } from "@/lib/types/logs";
import "./TimeRangePicker.css";

/** Generate ["00:00", "00:30", "01:00", … "23:30"] */
function buildTimeOptions(): string[] {
  const opts: string[] = [];
  for (let h = 0; h < 24; h++) {
    for (let m = 0; m < 60; m += 30) {
      opts.push(
        `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`
      );
    }
  }
  return opts;
}

const TIME_OPTIONS = buildTimeOptions();

type TimeDropdownProps = {
  value: string;
  onChange: (v: string) => void;
  hasError: boolean;
  disabled?: boolean;
};

function TimeDropdown({ value, onChange, hasError, disabled }: TimeDropdownProps) {
  const [isOpen, setIsOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    function handleClick(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen || !listRef.current) return;
    const active = listRef.current.querySelector(
      ".trp__option--active"
    ) as HTMLElement | null;
    if (active) {
      active.scrollIntoView({ block: "center" });
    }
  }, [isOpen]);

  const handleSelect = useCallback(
    (time: string) => {
      onChange(time);
      setIsOpen(false);
    },
    [onChange]
  );

  return (
    <div className="trp__dropdown" ref={wrapperRef}>
      <button
        type="button"
        className={`trp__trigger${hasError ? " trp__trigger--error" : ""}${disabled ? " trp__trigger--disabled" : ""}`}
        onClick={() => !disabled && setIsOpen((v) => !v)}
        disabled={disabled}
      >
        {value}
        <span className="trp__arrow" />
      </button>

      {isOpen && !disabled && (
        <div className="trp__popup" ref={listRef}>
          {TIME_OPTIONS.map((t) => (
            <button
              key={t}
              type="button"
              className={`trp__option${t === value ? " trp__option--active" : ""}`}
              onClick={() => handleSelect(t)}
            >
              {t}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function TimeRangePicker({ startTime, endTime, onChange, disabled }: TimeRangePickerProps) {
  const hasError = !disabled && endTime < startTime;

  return (
    <div className={`trp${disabled ? " trp--disabled" : ""}`}>
      <TimeDropdown
        value={startTime}
        onChange={(v) => onChange(v, endTime)}
        hasError={hasError}
        disabled={disabled}
      />
      <span className="trp__separator">&ndash;</span>
      <TimeDropdown
        value={endTime}
        onChange={(v) => onChange(startTime, v)}
        hasError={hasError}
        disabled={disabled}
      />
    </div>
  );
}
