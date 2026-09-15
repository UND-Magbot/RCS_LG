import type { AlarmLogResponse } from "@/lib/types/alarm-log";

export type AlarmSearchItem = {
  id: string;
  code: string;
  errorType: string;
  status: string;
  robotSn: string;
  message: string;
  timestamp: string;
};

export type AlarmSearchParams = {
  message?: string;
  errorType?: string;
  code?: string;
  robotSn?: string;
  date?: string | null;
  startTime: string;
  endTime: string;
  page: number;
  pageSize: number;
};

export type AlarmSearchResponse = {
  items: AlarmSearchItem[];
  total: number;
  page: number;
  pageSize: number;
};

export type AlarmSearchFilterState = {
  message: string;
  errorType: string;
  code: string;
  robotSn: string;
  date: string | null;
  startTime: string;
  endTime: string;
};

export type AlarmSearchFilterProps = {
  filters: AlarmSearchFilterState;
  robotSns: string[];
  errorTypes: { value: string; label: string }[];
  codes: string[];
  onFilterChange: (filters: AlarmSearchFilterState) => void;
  onSearch: () => void;
  onReset: () => void;
};

export type AlarmSearchItemProps = {
  item: AlarmLogResponse;
};

export type AlarmSearchPaginationProps = {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
};
