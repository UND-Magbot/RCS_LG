"use client";

import { useEffect, useState } from "react";
import { Modal } from "../Modal";
import { apiFetch } from "@/lib/api";
import { useAlert } from "@/lib/context/AlertContext";

type RobotItem = {
  sn: string;
  name: string;
  ip_address: string | null;
};

type RelocalizeResult = {
  sn: string;
  name: string;
  status: "pending" | "loading" | "success" | "error";
  message?: string;
};

type MapRelocalizeModalProps = {
  open: boolean;
  onClose: () => void;
};

export function MapRelocalizeModal({ open, onClose }: MapRelocalizeModalProps) {
  const { showAlert } = useAlert();
  const [search, setSearch] = useState("");
  const [selectedSns, setSelectedSns] = useState<Set<string>>(new Set());
  const [robots, setRobots] = useState<RobotItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<RelocalizeResult[]>([]);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    setResults([]);
    setSelectedSns(new Set());
    apiFetch<{ total: number; items: RobotItem[] }>("/api/map/robots")
      .then((data) => setRobots(data.items))
      .catch((err) => {
        setError(err.message ?? "로봇 목록을 불러오지 못했습니다.");
        showAlert({ title: "알림", message: "위치 재조정용 로봇 목록을 불러오는 데 실패했습니다.", errorCode: "MAP-014", errorType: "map", source: "맵 관리 > 위치 재조정", description: "MapRelocalizeModal — 로봇 목록 로드 실패" });
      })
      .finally(() => setLoading(false));
  }, [open]);

  const filtered = search
    ? robots.filter(
        (r) =>
          r.sn.toLowerCase().includes(search.toLowerCase()) ||
          r.name.toLowerCase().includes(search.toLowerCase())
      )
    : robots;

  const toggleSelect = (sn: string) => {
    setSelectedSns((prev) => {
      const next = new Set(prev);
      if (next.has(sn)) next.delete(sn);
      else next.add(sn);
      return next;
    });
  };

  const handleSelectAll = () => {
    const allFilteredSns = filtered
      .filter((r) => r.ip_address)
      .map((r) => r.sn);
    const allSelected = allFilteredSns.every((sn) => selectedSns.has(sn));
    if (allSelected) {
      setSelectedSns(new Set());
    } else {
      setSelectedSns(new Set(allFilteredSns));
    }
  };

  const handleRelocalize = async () => {
    if (selectedSns.size === 0) return;
    setRunning(true);
    setError(null);

    const targets = robots.filter(
      (r) => selectedSns.has(r.sn) && r.ip_address
    );

    // 진행 상태 초기화
    const initialResults: RelocalizeResult[] = targets.map((r) => ({
      sn: r.sn,
      name: r.name,
      status: "loading",
    }));
    setResults(initialResults);

    try {
      const robotIps = targets.map((r) => r.ip_address as string);
      const response = await apiFetch<{
        results: { robot_ip: string; success: boolean; message: string }[];
      }>("/api/map/relocalize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ robot_ips: robotIps }),
      });

      // IP → SN 매핑
      const ipToSn = new Map(
        targets.map((r) => [r.ip_address, r.sn])
      );

      setResults(
        response.results.map((r) => ({
          sn: ipToSn.get(r.robot_ip) ?? r.robot_ip,
          name:
            targets.find((t) => t.ip_address === r.robot_ip)?.name ??
            r.robot_ip,
          status: r.success ? ("success" as const) : ("error" as const),
          message: r.message,
        }))
      );
    } catch (err: any) {
      setResults(
        initialResults.map((r) => ({
          ...r,
          status: "error" as const,
          message: err.message ?? "위치 재조정에 실패했습니다.",
        }))
      );
    }

    setRunning(false);
  };

  const handleClose = () => {
    if (running) return;
    setSearch("");
    setSelectedSns(new Set());
    setError(null);
    setResults([]);
    onClose();
  };

  const hasResults = results.length > 0;

  return (
    <Modal open={open} onClose={handleClose} title="위치 재조정" width="460px">
      {!hasResults ? (
        <>
          <div style={{ marginBottom: "var(--space-2)", color: "var(--text-muted)", fontSize: "13px" }}>
            선택한 로봇의 위치를 충전소 도킹 포인트 좌표로 재조정합니다.
          </div>

          <div className="robot-connect__search">
            <input
              className="input"
              placeholder="SN 또는 로봇명으로 검색..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <div style={{ marginBottom: "var(--space-2)" }}>
            <button
              className="btn"
              style={{ fontSize: "12px", padding: "4px 8px" }}
              onClick={handleSelectAll}
            >
              {filtered.filter((r) => r.ip_address).every((r) => selectedSns.has(r.sn)) && filtered.length > 0
                ? "전체 해제"
                : "전체 선택"}
            </button>
            <span style={{ marginLeft: "var(--space-2)", color: "var(--text-muted)", fontSize: "13px" }}>
              {selectedSns.size}대 선택됨
            </span>
          </div>

          <div className="robot-connect__list">
            {loading ? (
              <div style={{ color: "var(--text-muted)", textAlign: "center", padding: "16px" }}>
                로봇 목록을 불러오는 중...
              </div>
            ) : filtered.length === 0 ? (
              <div style={{ color: "var(--text-muted)", textAlign: "center", padding: "16px" }}>
                검색 결과가 없습니다.
              </div>
            ) : (
              filtered.map((robot) => {
                const noIp = !robot.ip_address;
                return (
                  <button
                    key={robot.sn}
                    className={
                      selectedSns.has(robot.sn)
                        ? "robot-connect__item robot-connect__item--selected"
                        : "robot-connect__item"
                    }
                    onClick={() => !noIp && toggleSelect(robot.sn)}
                    disabled={noIp}
                    style={noIp ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
                  >
                    <span className="robot-connect__item-name">
                      {selectedSns.has(robot.sn) ? "☑ " : "☐ "}
                      {robot.name}
                    </span>
                    <span className="robot-connect__item-sn">
                      {robot.sn}
                      {noIp && " (IP 미등록)"}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </>
      ) : (
        <div className="robot-connect__list">
          {results.map((r) => (
            <div
              key={r.sn}
              className="robot-connect__item"
              style={{ cursor: "default" }}
            >
              <span className="robot-connect__item-name">
                {r.status === "loading" && "⏳ "}
                {r.status === "success" && "✅ "}
                {r.status === "error" && "❌ "}
                {r.name}
              </span>
              <span
                className="robot-connect__item-sn"
                style={{
                  color:
                    r.status === "success"
                      ? "var(--color-success, #4caf50)"
                      : r.status === "error"
                      ? "var(--danger, #f44336)"
                      : undefined,
                }}
              >
                {r.message ?? r.sn}
              </span>
            </div>
          ))}
        </div>
      )}

      {error && (
        <div style={{ color: "var(--danger)", fontSize: "13px", padding: "4px 0" }}>
          {error}
        </div>
      )}

      <div className="robot-connect__actions">
        <button className="btn" onClick={handleClose} disabled={running}>
          {hasResults ? "닫기" : "취소"}
        </button>
        {!hasResults && (
          <button
            className="btn btn--primary"
            onClick={handleRelocalize}
            disabled={selectedSns.size === 0 || running}
          >
            {running ? "재조정 중..." : `위치 재조정 (${selectedSns.size}대)`}
          </button>
        )}
      </div>
    </Modal>
  );
}
