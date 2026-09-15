import { apiFetch, apiPost, apiPatch } from "@/lib/api";
import type {
  AlarmLogResponse,
  AlarmLogListResponse,
  UnreadCountResponse,
  AlarmLogCreateRequest,
} from "@/lib/types/alarm-log";

export async function createAlarmLog(
  data: AlarmLogCreateRequest
): Promise<AlarmLogResponse> {
  return apiPost<AlarmLogResponse>("/api/alarm-logs", data);
}

export async function getAlarmLogs(params?: {
  skip?: number;
  limit?: number;
  error_type?: string;
  severity?: string;
  robot_sn?: string;
  is_read?: boolean;
  error_code?: string;
  message?: string;
  hours?: number;
  date_from?: string;
  date_to?: string;
}): Promise<AlarmLogListResponse> {
  const sp = new URLSearchParams();
  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        sp.set(key, String(value));
      }
    });
  }
  const q = sp.toString();
  return apiFetch<AlarmLogListResponse>(
    `/api/alarm-logs${q ? `?${q}` : ""}`
  );
}

export async function getUnreadCount(): Promise<UnreadCountResponse> {
  return apiFetch<UnreadCountResponse>("/api/alarm-logs/unread-count");
}

export async function markAsRead(
  ids: number[]
): Promise<{ updated: number }> {
  return apiPatch<{ updated: number }>("/api/alarm-logs/mark-read", { ids });
}

export async function markAllAsRead(): Promise<{ updated: number }> {
  return apiPatch<{ updated: number }>("/api/alarm-logs/mark-all-read", {});
}

export async function getDistinctRobotSns(): Promise<string[]> {
  return apiFetch<string[]>("/api/alarm-logs/distinct-robot-sns");
}

export async function getDistinctErrorCodes(): Promise<string[]> {
  return apiFetch<string[]>("/api/alarm-logs/distinct-error-codes");
}
