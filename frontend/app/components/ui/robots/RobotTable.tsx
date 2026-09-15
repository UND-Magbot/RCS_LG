"use client";

import type { RobotTableProps } from "@/lib/types/robots";
import "./RobotTable.css";

const RUN_STATE_LABEL: Record<string, string> = {
  EXECUTING: "운영중",
  CHARGING:  "충전중",
  IDLE:      "대기중",
  OFFLINE:      "오프라인",
};

function PowerCell({ power }: { power: number | string | null }) {
  if (power == null) return <span className="robot-table__muted">-</span>;

  const value =
    typeof power === "string" ? Number(power.replace("%", "").trim()) : power;

  if (!Number.isFinite(value)) return <span className="robot-table__muted">-</span>;

  const className =
    value <= 30 ? "robot-table__power robot-table__power--danger" : "robot-table__power";

  return <span className={className}>{value}%</span>;
}

export function RobotTable({
  devices,
  onEnableToggle,
  onInfoClick,
  togglingDeviceId,
}: RobotTableProps) {
  return (
    <div className="robot-table__wrapper">
      <table className="robot-table">
        <colgroup>
          <col />{/* SN */}
          <col />{/* RobotName */}
          <col />{/* Model */}
          <col />{/* RunState */}
          <col />{/* Online */}
          <col />{/* Signal */}
          <col />{/* Power */}
          <col />{/* Operation */}
        </colgroup>
        <thead>
          <tr>
            <th>로봇 SN</th>
            <th>로봇 명</th>
            <th>모델</th>
            <th>운행 상태</th>
            <th>전원</th>
            <th>신호 상태</th>
            <th>배터리 (%)</th>
            <th>운영사</th>
          </tr>
        </thead>
        <tbody>
          {devices.length === 0 ? (
            <tr>
              <td colSpan={8} className="robot-table__empty">
                등록된 로봇이 없습니다.
              </td>
            </tr>
          ) : (
            devices.map((device) => {
              const rowClass = `robot-table__row${!device.online ? " robot-table__row--offline" : ""}`;

              let runStateClass = "robot-table__run-state";
              if (device.runState === "EXECUTING")
                runStateClass += " robot-table__run-state--executing";
              else if (device.runState === "CHARGING")
                runStateClass += " robot-table__run-state--charging";

              const isToggleDisabled = togglingDeviceId === device.id;

              return (
                <tr key={device.id} className={rowClass}>
                  <td>{device.sn}</td>
                  <td className="robot-table__name">{device.robotName}</td>
                  <td>{device.model}</td>
                  <td>
                    <span className={runStateClass}>
                      {device.runState ? (RUN_STATE_LABEL[device.runState] ?? device.runState) : "-"}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`robot-table__online robot-table__online--${device.online ? "on" : "off"}`}
                    >
                      {device.online ? "온라인" : "오프라인"}
                    </span>
                  </td>
                  <td>
                    {device.signal != null ? (
                      String(device.signal).toLowerCase().includes("%")
                        ? String(device.signal)
                        : `${device.signal}`
                    ) : (
                      <span className="robot-table__muted" title="Signal data unavailable">N/A</span>
                    )}
                  </td>
                  <td>
                    <PowerCell power={device.power} />
                  </td>
                  <td>
                    <button
                      className="robot-table__info-btn"
                      onClick={() => onInfoClick(device.id)}
                    >
                      정보
                    </button>
                  </td>
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
