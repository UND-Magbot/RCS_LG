"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { LogItem } from "@/lib/types/logs";
import { Modal } from "../Modal";
import "./LogTable.css";

type LogTableProps = {
  logs: LogItem[];
};

function formatCreatedAt(raw: string): string {
  const d = new Date(raw);
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${min}`;
}


export function LogTable({ logs }: LogTableProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [overflowIds, setOverflowIds] = useState<Set<string>>(new Set());
  const [selectedLog, setSelectedLog] = useState<LogItem | null>(null);
  const msgRefs = useRef<Map<string, HTMLElement>>(new Map());

  const checkOverflows = useCallback(() => {
    const next = new Set<string>();
    msgRefs.current.forEach((el, id) => {
      if (el.scrollHeight > el.clientHeight) next.add(id);
    });
    setOverflowIds(next);
  }, []);

  useEffect(() => {
    checkOverflows();
  }, [logs, checkOverflows]);

  const handleRowClick = (id: string) => {
    if (!overflowIds.has(id)) return;
    setExpandedId((prev) => (prev === id ? null : id));
  };

  return (
    <>
      <div className="log-table__wrapper">
        <table className="log-table">
          <colgroup>
            <col style={{ width: "15%" }} />
            <col style={{ width: "10%" }} />
            <col style={{ width: "65%" }} />
            <col style={{ width: "10%" }} />
          </colgroup>
          <thead>
            <tr>
              <th>발생 일시</th>
              <th>로그 타입</th>
              <th>메세지</th>
              <th>상세</th>
            </tr>
          </thead>
          <tbody>
            {logs.length === 0 ? (
              <tr>
                <td colSpan={4} className="log-table__empty">
                  조회된 로그가 없습니다.
                </td>
              </tr>
            ) : (
              logs.map((log) => {
                const isOverflow = overflowIds.has(log.id);
                const isExpanded = expandedId === log.id;
                const categoryLabel = log.display_category;

                return (
                  <tr
                    key={log.id}
                    className={`log-table__row${isOverflow ? " log-table__row--expandable" : ""}`}
                    onClick={() => handleRowClick(log.id)}
                  >
                    <td>{formatCreatedAt(log.created_at)}</td>
                    <td>
                      <span
                        className={`log-table__error-type log-table__error-type--${categoryLabel}`}
                      >
                        {categoryLabel}
                      </span>
                    </td>
                    <td className="log-table__message-cell">
                      <div
                        className={`log-table__message${isExpanded ? " log-table__message--expanded" : ""}`}
                        ref={(el) => {
                          if (el) msgRefs.current.set(log.id, el);
                          else msgRefs.current.delete(log.id);
                        }}
                      >
                        {log.message}
                      </div>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="log-table__view-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          setSelectedLog(log);
                        }}
                      >
                        상세보기
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      <Modal
        open={selectedLog !== null}
        onClose={() => setSelectedLog(null)}
        title="Data"
        width="600px"
        height="380px"
      >
        {selectedLog && (
          <div className="log-detail">
            <div className="log-table__json-viewer">
              <pre>{JSON.stringify(selectedLog, null, 2)}</pre>
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}
