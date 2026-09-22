export type ErrorCategory = "시스템" | "로봇" | "사용자";

export type LogItem = {
  id: string;
  display_category: string;
  action: string;
  message: string;
  detail: string | null;
  robot_id: number | null;
  robot_name: string | null;
  source: string | null;
  created_at: string;
};

export type LogFilterState = {
  message: string;
  errorType: ErrorCategory | "";
  startDate: string;
  endDate: string;
  startTime: string;
  endTime: string;
};

export type DatePickerProps = {
  value: string | null;
  onChange: (date: string | null) => void;
  popupAlign?: "left" | "right";
};

export type TimeRangePickerProps = {
  startTime: string;
  endTime: string;
  onChange: (start: string, end: string) => void;
  disabled?: boolean;
};
