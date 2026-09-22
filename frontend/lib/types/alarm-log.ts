export type AlarmLogResponse = {
  id: number;
  error_code: string;
  error_type: string;
  error_type_name: string;
  severity: string;
  severity_name: string;
  message: string;
  description: string | null;
  source: string | null;
  robot_sn: string | null;
  is_read: boolean;
  created_at: string;
};

export type AlarmLogListResponse = {
  total: number;
  items: AlarmLogResponse[];
};

export type UnreadCountResponse = {
  count: number;
};

export type AlarmLogCreateRequest = {
  error_code: string;
  error_type: string;
  severity?: string;
  message: string;
  description?: string;
  source?: string;
  robot_sn?: string;
};
