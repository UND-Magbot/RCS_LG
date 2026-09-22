"use client";

import { DatePicker } from "../ui/logs/DatePicker";
import { TimeRangePicker } from "../ui/logs/TimeRangePicker";
import type { AlarmSearchFilterProps } from "@/lib/types/alarm-search";

export function AlarmSearchFilter({
  filters,
  robotSns,
  errorTypes,
  codes,
  onFilterChange,
  onSearch,
  onReset,
}: AlarmSearchFilterProps) {
  const update = (patch: Partial<typeof filters>) => {
    onFilterChange({ ...filters, ...patch });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") onSearch();
  };

  return (
    <div className="alarm-search-filter">
      <div className="alarm-search-filter__row">
        <div className="alarm-search-filter__group alarm-search-filter__group--half">
          <div className="alarm-search-filter__field alarm-search-filter__field--grow">
            <input
              type="text"
              className="alarm-search-filter__input"
              placeholder="오류 메시지"
              value={filters.message}
              onChange={(e) => update({ message: e.target.value })}
              onKeyDown={handleKeyDown}
            />
          </div>
        </div>

        <div className="alarm-search-filter__group alarm-search-filter__group--half">
          <div className="alarm-search-filter__field alarm-search-filter__field--grow">
            <label className="alarm-search-filter__label">오류 코드</label>
            <select
              className="alarm-search-filter__select"
              value={filters.code}
              onChange={(e) => update({ code: e.target.value })}
            >
              <option value="">전체</option>
              {codes.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>

          <div className="alarm-search-filter__field alarm-search-filter__field--grow">
            <label className="alarm-search-filter__label">오류 타입</label>
            <select
              className="alarm-search-filter__select"
              value={filters.errorType}
              onChange={(e) => update({ errorType: e.target.value })}
            >
              <option value="">전체</option>
              {errorTypes.map((et) => (
                <option key={et.value} value={et.value}>
                  {et.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="alarm-search-filter__row">
        <div className="alarm-search-filter__group alarm-search-filter__group--half">
          <div className="alarm-search-filter__field alarm-search-filter__field--grow">
            <label className="alarm-search-filter__label">로봇 SN</label>
            <select
              className="alarm-search-filter__select"
              value={filters.robotSn}
              onChange={(e) => update({ robotSn: e.target.value })}
            >
              <option value="">전체</option>
              {robotSns.map((sn) => (
                <option key={sn} value={sn}>
                  {sn}
                </option>
              ))}
            </select>
          </div>

          <div className="alarm-search-filter__field">
            <label className="alarm-search-filter__label">날짜</label>
            <DatePicker
              value={filters.date}
              onChange={(date) => update({ date })}
            />
          </div>
        </div>

        <div className="alarm-search-filter__group alarm-search-filter__group--half">
          <div className="alarm-search-filter__field alarm-search-filter__field--grow">
            <label className="alarm-search-filter__label">시간</label>
            <TimeRangePicker
              startTime={filters.startTime}
              endTime={filters.endTime}
              onChange={(startTime, endTime) => update({ startTime, endTime })}
            />
          </div>

          <button type="button" className="btn btn--outline" onClick={onReset}>
            초기화
          </button>
          <button type="button" className="btn btn--primary" onClick={onSearch}>
            검색
          </button>
        </div>
      </div>
    </div>
  );
}
