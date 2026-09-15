import type { DevicePower, DeviceStatus } from "@/lib/types/monitoring";

/**
 * Backend status codes:
 *   0=IDLE(대기), 1=WORKING(작업중), 2=CHARGING(충전중), 3=ERROR(에러), 4=OFFLINE(오프라인)
 *
 * Live RUNSTATE values:
 *   "IDLE", "EXECUTING", "CHARGING", "OFFLINE", "N/A"
 */

/** Backend status code(0-4) → DeviceStatus */
export function mapBackendStatusToDeviceStatus(
  statusCode: number | null | undefined
): DeviceStatus {
  switch (statusCode) {
    case 0:
      return "idle";
    case 1:
      return "running";
    case 2:
      return "charging";
    case 3:
      return "error";
    case 4:
      return "disable";
    default:
      return "disable";
  }
}

/** Backend status code → DevicePower */
export function mapBackendStatusToPower(
  statusCode: number | null | undefined
): DevicePower {
  return statusCode === 4 ? "offline" : "online";
}

/** Live RUNSTATE string → DeviceStatus */
export function mapLiveRunStateToDeviceStatus(runState: string): DeviceStatus {
  switch (runState) {
    case "EXECUTING":
      return "running";
    case "IDLE":
      return "idle";
    case "CHARGING":
      return "charging";
    case "OFFLINE":
      return "disable";
    default:
      return "disable";
  }
}

/** Live ONLINE string → DevicePower */
export function mapLiveOnlineToPower(online: string): DevicePower {
  return online === "Online" ? "online" : "offline";
}

/** 배터리 표시 포맷 (DB 기반) */
export function formatBattery(
  level: number | null | undefined,
  power: DevicePower
): string {
  if (power === "offline" || level == null) return "--";
  return `${level}%`;
}

/** 배터리 표시 포맷 (Live 기반) */
export function formatLiveBattery(powerStr: string, online: string): string {
  if (online !== "Online" || powerStr === "-" || powerStr === "N/A") return "--";
  return powerStr;
}
