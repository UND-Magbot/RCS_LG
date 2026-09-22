"use client";

import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import { IconButton } from "../ui/IconButton";
import { AlarmSearchFilter } from "./AlarmSearchFilter";
import { AlarmSearchItem } from "./AlarmSearchItem";
import { AlarmSearchPagination } from "./AlarmSearchPagination";
import {
  getAlarmLogs,
  getDistinctRobotSns,
  getDistinctErrorCodes,
} from "@/lib/api/alarm-log";
import { ALARM_ERROR_TYPE_LABELS } from "@/lib/constants/alarm";
import type { AlarmLogResponse } from "@/lib/types/alarm-log";
import type { AlarmSearchFilterState } from "@/lib/types/alarm-search";
import "./alarm-search.css";

type AlarmSearchPopupProps = {
  onClose: () => void;
};

const defaultFilters: AlarmSearchFilterState = {
  message: "",
  errorType: "",
  code: "",
  robotSn: "",
  date: null,
  startTime: "00:00",
  endTime: "23:59",
};

export function AlarmSearchPopup({ onClose }: AlarmSearchPopupProps) {
  const [filters, setFilters] =
    useState<AlarmSearchFilterState>(defaultFilters);
  const [appliedFilters, setAppliedFilters] =
    useState<AlarmSearchFilterState>(defaultFilters);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(6);
  const [items, setItems] = useState<AlarmLogResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [robotSns, setRobotSns] = useState<string[]>([]);
  const [codes, setCodes] = useState<string[]>([]);

  const dialogRef = useRef<HTMLDivElement>(null);

  const errorTypes = useMemo(
    () =>
      Object.entries(ALARM_ERROR_TYPE_LABELS).map(([value, label]) => ({
        value,
        label,
      })),
    []
  );

  // Load distinct values for filter dropdowns
  useEffect(() => {
    getDistinctRobotSns().then(setRobotSns).catch(() => {});
    getDistinctErrorCodes().then(setCodes).catch(() => {});
  }, []);

  const doSearch = useCallback(
    (f: AlarmSearchFilterState, p: number, ps: number) => {
      getAlarmLogs({
        skip: (p - 1) * ps,
        limit: ps,
        ...(f.message ? { message: f.message } : {}),
        ...(f.errorType ? { error_type: f.errorType } : {}),
        ...(f.code ? { error_code: f.code } : {}),
        ...(f.robotSn ? { robot_sn: f.robotSn } : {}),
        ...(f.date
          ? {
              date_from: `${f.date}T${f.startTime}:00`,
              date_to: `${f.date}T${f.endTime}:59`,
            }
          : { hours: 0 }),
      })
        .then((res) => {
          setItems(res.items);
          setTotal(res.total);
        })
        .catch(() => {});
    },
    []
  );

  // Initial load + refetch on filter/page change
  useEffect(() => {
    doSearch(appliedFilters, page, pageSize);
  }, [appliedFilters, page, pageSize, doSearch]);

  // ESC to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (dialogRef.current && !dialogRef.current.contains(e.target as Node)) {
      onClose();
    }
  };

  const handleSearch = () => {
    setAppliedFilters({ ...filters });
    setPage(1);
  };

  const handleReset = () => {
    setFilters(defaultFilters);
    setAppliedFilters(defaultFilters);
    setPage(1);
  };

  const handlePageSizeChange = (newSize: number) => {
    setPageSize(newSize);
    setPage(1);
  };

  return (
    <div className="alarm-search__overlay" onMouseDown={handleOverlayClick}>
      <div
        className="alarm-search"
        ref={dialogRef}
        role="dialog"
        aria-label="Alarm search"
      >
        <div className="alarm-search__header">
          <h2 className="alarm-search__title">알람 검색</h2>
          <IconButton
            className="alarm-search__close"
            variant="ghost"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </IconButton>
        </div>

        <AlarmSearchFilter
          filters={filters}
          robotSns={robotSns}
          errorTypes={errorTypes}
          codes={codes}
          onFilterChange={setFilters}
          onSearch={handleSearch}
          onReset={handleReset}
        />

        <div className="alarm-search__list">
          {items.length === 0 ? (
            <div className="alarm-search__empty">검색 결과가 없습니다.</div>
          ) : (
            items.map((item) => <AlarmSearchItem key={item.id} item={item} />)
          )}
        </div>

        {total > 0 ? (
          <AlarmSearchPagination
            page={page}
            pageSize={pageSize}
            total={total}
            onPageChange={setPage}
            onPageSizeChange={handlePageSizeChange}
          />
        ) : null}
      </div>
    </div>
  );
}
