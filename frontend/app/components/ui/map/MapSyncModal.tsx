"use client";

import { useEffect, useState } from "react";
import { Modal } from "../Modal";
import { apiFetch } from "@/lib/api";
import type { MapSyncModalProps } from "@/lib/types/map";

type RobotItem = {
  sn: string;
  name: string;
  ip_address: string | null;
};

type RobotMapItem = {
  id: number;
  map_name: string;
};

type SyncResult = {
  sn: string;
  name: string;
  status: "pending" | "loading" | "success" | "error";
  message?: string;
};

export function MapSyncModal({
  open,
  onClose,
  mappingId,
  mapId,
  areaName,
  onSyncComplete,
  mode: propMode = "poi",
}: MapSyncModalProps) {
  const [search, setSearch] = useState("");
  const [selectedSns, setSelectedSns] = useState<Set<string>>(new Set());
  const [robots, setRobots] = useState<RobotItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"map" | "poi">(propMode);
  const [results, setResults] = useState<SyncResult[]>([]);

  // 로봇 맵 목록 (대상 선택용)
  const [robotMaps, setRobotMaps] = useState<RobotMapItem[]>([]);
  const [selectedRobotMapId, setSelectedRobotMapId] = useState<number | null>(null);
  const [loadingMaps, setLoadingMaps] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    setResults([]);
    setSelectedSns(new Set());
    setRobotMaps([]);
    setSelectedRobotMapId(null);
    apiFetch<{ total: number; items: RobotItem[] }>("/api/map/robots")
      .then((data) => setRobots(data.items))
      .catch((err) => setError(err.message ?? "로봇 목록을 불러오지 못했습니다."))
      .finally(() => setLoading(false));
  }, [open]);

  // 로봇 선택 시 해당 로봇의 맵 목록 로드
  useEffect(() => {
    const selectedArr = Array.from(selectedSns);
    if (selectedArr.length !== 1) {
      setRobotMaps([]);
      setSelectedRobotMapId(null);
      return;
    }
    const robot = robots.find((r) => r.sn === selectedArr[0]);
    if (!robot?.ip_address) return;

    setLoadingMaps(true);
    apiFetch<RobotMapItem[]>(`/api/map/${robot.ip_address}/maps`)
      .then((maps) => {
        // sync 맵 제외
        const filtered = maps.filter((m) => !m.map_name?.includes("-sync-"));
        setRobotMaps(filtered);
        if (filtered.length > 0) setSelectedRobotMapId(filtered[0].id);
      })
      .catch(() => setRobotMaps([]))
      .finally(() => setLoadingMaps(false));
  }, [selectedSns, robots]);

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

  const handleSync = async (syncMode: "map" | "poi") => {
    setMode(syncMode);
    if (selectedSns.size === 0) return;
    setSyncing(true);
    setError(null);

    const targets = robots.filter(
      (r) => selectedSns.has(r.sn) && r.ip_address
    );

    const initialResults: SyncResult[] = targets.map((r) => ({
      sn: r.sn,
      name: r.name,
      status: "pending",
    }));
    setResults(initialResults);

    const promises = targets.map(async (robot) => {
      setResults((prev) =>
        prev.map((r) =>
          r.sn === robot.sn ? { ...r, status: "loading" as const } : r
        )
      );

      try {
        if (syncMode === "map") {
          // ── 맵 동기화 : SLAM 맵(carto_map) 자체를 교체한다.
          //    로봇이 새 맵을 읽으려면 서비스 재시작이 필요해 60~90초 걸린다.
          //    POI·가상벽은 백엔드가 재시작 완료를 확인한 뒤 자동으로 다시 넣는다.
          const syncBody: Record<string, any> = {
            robot_ip: robot.ip_address,
            area_name: areaName,
            method: "full",
          };
          if (selectedRobotMapId) syncBody.target_robot_map_id = selectedRobotMapId;

          await apiFetch(`/api/map/maps/${mapId}/sync-to-robot`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(syncBody),
          });
        } else {
          // ── POI 동기화 : 충전소·작업위치·가상벽만 전송. 재시작 없음(수 초).
          await apiFetch(`/api/map/maps/${mapId}/sync-overlays`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ robot_ip: robot.ip_address }),
          });
        }

        setResults((prev) =>
          prev.map((r) =>
            r.sn === robot.sn
              ? {
                  ...r,
                  status: "success" as const,
                  message:
                    syncMode === "map"
                      ? "맵 업로드 완료 — 로봇 재시작 후 POI·가상벽 자동 재적용(약 90초)"
                      : "POI·가상벽 적용 완료",
                }
              : r
          )
        );
      } catch (err: any) {
        setResults((prev) =>
          prev.map((r) =>
            r.sn === robot.sn
              ? {
                  ...r,
                  status: "error" as const,
                  message: err.message ?? "동기화에 실패했습니다.",
                }
              : r
          )
        );
      }
    });

    await Promise.allSettled(promises);
    setSyncing(false);
    onSyncComplete?.();
  };

  const handleClose = () => {
    if (syncing) return;
    setSearch("");
    setSelectedSns(new Set());
    setError(null);
    setResults([]);
    setRobotMaps([]);
    setSelectedRobotMapId(null);
    onClose();
  };

  // 툴바에서 어떤 버튼으로 열었는지에 따라 실행 모드를 맞춘다
  useEffect(() => {
    if (open) setMode(propMode);
  }, [open, propMode]);

  const hasResults = results.length > 0;

  return (
    <Modal open={open} onClose={handleClose} title="로봇 동기화" width="600px">
      {!hasResults ? (
        <>
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

          {/* 로봇 맵 선택 (대상 맵) */}
          {selectedSns.size > 0 && (
            <div style={{ marginTop: "var(--space-3)", padding: "8px 0", borderTop: "1px solid var(--border-color)" }}>
              <label style={{ fontSize: "13px", fontWeight: 600, marginBottom: 4, display: "block" }}>
                대상 로봇 맵
              </label>
              {loadingMaps ? (
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>맵 목록 로딩 중...</span>
              ) : robotMaps.length > 0 ? (
                <select
                  className="input"
                  style={{ width: "100%", fontSize: "13px" }}
                  value={selectedRobotMapId ?? ""}
                  onChange={(e) => setSelectedRobotMapId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">새 맵 생성</option>
                  {robotMaps.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.map_name} (ID: {m.id})
                    </option>
                  ))}
                </select>
              ) : (
                <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>
                  {selectedSns.size > 1 ? "로봇 1대만 선택하면 대상 맵을 지정할 수 있습니다" : "로봇 맵이 없습니다"}
                </span>
              )}
            </div>
          )}
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
                {r.status === "pending" && "⏸ "}
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

      {!hasResults && (
        <div style={{
          marginTop: 12, padding: "8px 10px", fontSize: 12, lineHeight: 1.6,
          background: "var(--bg-surface-2)", border: "1px solid var(--border)",
          borderRadius: 6, color: "var(--text-secondary)",
        }}>
          {mode === "poi" ? (
            <div><b>POI 동기화</b> — 충전소 · 작업위치 · <b>가상벽</b>만 전송합니다. 로봇 재시작 없이 몇 초면 끝납니다.</div>
          ) : (
            <div><b>맵 동기화</b> — SLAM 맵 자체를 교체합니다. <b>로봇이 재시작되어 60~90초</b> 걸리며,
              POI·가상벽은 재시작 완료 후 자동으로 다시 적용됩니다.</div>
          )}
          <div style={{ marginTop: 4, opacity: 0.8 }}>선택된 로봇 {selectedSns.size}대</div>
        </div>
      )}

      <div className="robot-connect__actions" style={{ flexWrap: "wrap" }}>
        <button className="btn" onClick={handleClose} disabled={syncing}>
          {hasResults ? "닫기" : "취소"}
        </button>
        {!hasResults && (
          <button
            className="btn btn--primary"
            onClick={() => handleSync(mode)}
            disabled={selectedSns.size === 0 || syncing}
          >
            {syncing
              ? (mode === "map" ? "맵 동기화 중..." : "POI 동기화 중...")
              : (mode === "map" ? "맵 동기화 실행" : "POI 동기화 실행")}
          </button>
        )}
      </div>
    </Modal>
  );
}
