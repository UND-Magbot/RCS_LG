import type { AlarmErrorType } from "@/lib/types/shell";

/** 에러 코드 접두사 → 에러 타입 매핑 */
export const ALARM_CODE_ERROR_TYPE_MAP: Record<string, AlarmErrorType> = {
  AUTH: "auth",
  TASK: "task",
  ROBOT: "robot",
  MAP: "map",
  NET: "net",
};

/** 에러 타입 → 한국어 라벨 */
export const ALARM_ERROR_TYPE_LABELS: Record<AlarmErrorType, string> = {
  auth: "인증 오류",
  task: "작업 오류",
  robot: "로봇 오류",
  map: "맵 오류",
  net: "통신 오류",
};

/** 에러 코드에서 에러 타입 추출 (e.g., "AUTH-001" → "auth") */
export function getErrorTypeFromCode(code: string): AlarmErrorType {
  const prefix = code.split("-")[0];
  return ALARM_CODE_ERROR_TYPE_MAP[prefix] ?? "task";
}
