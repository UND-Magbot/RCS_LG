"use client";

import type { RobotFilterProps, RobotFilterState } from "@/lib/types/robots";
import "./RobotFilter.css";

export function RobotFilter({
  filters,
  models,
  onFilterChange,
  onSearch,
}: RobotFilterProps) {
  const update = (patch: Partial<RobotFilterState>) => {
    onFilterChange({ ...filters, ...patch });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") onSearch();
  };

  return (
    <div className="robot-filter">
      <div className="robot-filter__field robot-filter__field--search">
        <input
          type="text"
          className="robot-filter__input"
          placeholder="로봇 SN / 로봇 명"
          value={filters.searchText}
          onChange={(e) => update({ searchText: e.target.value })}
          onKeyDown={handleKeyDown}
        />
      </div>

      <div className="robot-filter__field">
        <label className="robot-filter__label">모델</label>
        <select
          className="robot-filter__select"
          value={filters.model}
          onChange={(e) => update({ model: e.target.value })}
        >
          <option value="">전체</option>
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>

      <div className="robot-filter__field">
        <label className="robot-filter__label">상태</label>
        <select
          className="robot-filter__select"
          value={filters.runState}
          onChange={(e) =>
            update({ runState: e.target.value as RobotFilterState["runState"] })
          }
        >
          <option value="">전체</option>
          <option value="EXECUTING">운영중</option>
          <option value="IDLE">대기중</option>
          <option value="CHARGING">충전중</option>
        </select>
      </div>

      <div className="robot-filter__field">
        <label className="robot-filter__label">전원</label>
        <select
          className="robot-filter__select"
          value={filters.online}
          onChange={(e) =>
            update({ online: e.target.value as RobotFilterState["online"] })
          }
        >
          <option value="">전체</option>
          <option value="Online">온라인</option>
          <option value="Offline">오프라인</option>
        </select>
      </div>

      <button type="button" className="btn btn--primary" onClick={onSearch}>
        조회
      </button>
    </div>
  );
}
