"use client";

import { DatePicker } from "./DatePicker";
import { TimeRangePicker } from "./TimeRangePicker";
import { useAlert } from "@/lib/context/AlertContext";
import type { LogFilterState, ErrorCategory } from "@/lib/types/logs";
import "./LogFilter.css";

type LogFilterProps = {
  filters: LogFilterState;
  onFilterChange: (filters: LogFilterState) => void;
  onSearch: () => void;
  onReset: () => void;
};

const ERROR_CATEGORIES: ErrorCategory[] = ["시스템", "로봇", "사용자"];

export function LogFilter({
  filters,
  onFilterChange,
  onSearch,
  onReset,
}: LogFilterProps) {
  const { showInfo } = useAlert();

  const update = (patch: Partial<LogFilterState>) => {
    onFilterChange({ ...filters, ...patch });
  };

  const handleStartDateChange = (date: string | null) => {
    const newStart = date ?? filters.startDate;
    if (newStart > filters.endDate) {
      showInfo("알림", "시작일은 종료일 이후로 설정할 수 없습니다.");
      return;
    }
    update({ startDate: newStart });
  };

  const handleEndDateChange = (date: string | null) => {
    const newEnd = date ?? filters.endDate;
    if (newEnd < filters.startDate) {
      showInfo("알림", "종료일은 시작일 이전으로 설정할 수 없습니다.");
      return;
    }
    update({ endDate: newEnd });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") onSearch();
  };

  return (
    <div className="log-filter">
      <div className="log-filter__field log-filter__field--search">
        <input
          type="text"
          className="log-filter__input"
          placeholder="로그 메세지를 입력하세요."
          value={filters.message}
          onChange={(e) => update({ message: e.target.value })}
          onKeyDown={handleKeyDown}
        />
      </div>

      <div className="log-filter__field">
        <label className="log-filter__label">로그 타입</label>
        <select
          className="log-filter__select"
          value={filters.errorType}
          onChange={(e) =>
            update({ errorType: e.target.value as LogFilterState["errorType"] })
          }
        >
          <option value="">전체</option>
          {ERROR_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>

      <div className="log-filter__field">
        <label className="log-filter__label">시작일</label>
        <DatePicker
          value={filters.startDate}
          onChange={handleStartDateChange}
        />
      </div>

      <div className="log-filter__field">
        <label className="log-filter__label">종료일</label>
        <DatePicker
          value={filters.endDate}
          onChange={handleEndDateChange}
          popupAlign="right"
        />
      </div>

      <div className="log-filter__field">
        <label className="log-filter__label">조회 시간</label>
        <TimeRangePicker
          startTime={filters.startTime}
          endTime={filters.endTime}
          onChange={(startTime, endTime) => update({ startTime, endTime })}
        />
      </div>

      <button type="button" className="btn" onClick={onReset}>
        초기화
      </button>
      <button type="button" className="btn btn--primary" onClick={onSearch}>
        조회
      </button>
    </div>
  );
}
