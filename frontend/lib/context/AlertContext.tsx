"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  useEffect,
  useRef,
  type ReactNode,
} from "react";
import { createAlarmLog, getUnreadCount } from "@/lib/api/alarm-log";
import type { AlarmLogCreateRequest } from "@/lib/types/alarm-log";

/* ── 타입 ──────────────────────────────────────────────── */

type AlertModalData = {
  title: string;
  message: string;
  errorCode?: string;
  errorType?: string;
  severity?: string;
  robotSn?: string;
  timestamp?: string;
};

export type ShowAlertOptions = {
  title: string;
  message: string;
  /** 에러 코드 (e.g., "AUTH-001"). 지정 시 DB에 저장 */
  errorCode?: string;
  /** 에러 타입 (auth | task | robot | map | net | data) */
  errorType?: string;
  /** 심각도 (info | warning | error). 기본 "error" */
  severity?: string;
  /** 부설명 */
  description?: string;
  /** 발생 위치 (e.g., "모니터링 > 작업 시작") */
  source?: string;
  /** 관련 로봇 SN */
  robotSn?: string;
  /** true이면 모달 없이 DB만 저장 */
  silent?: boolean;
};

type AlertContextValue = {
  showAlert: (options: ShowAlertOptions) => void;
  /** 안내 팝업 (DB 저장·알림 벨 연동 없이 모달만 표시) */
  showInfo: (title: string, message: string) => void;
  alertModal: AlertModalData | null;
  closeAlert: () => void;
  unreadCount: number;
  refreshUnreadCount: () => void;
  incrementUnreadCount: () => void;
  decrementUnreadCount: (by?: number) => void;
  resetUnreadCount: () => void;
};

/* ── Context ───────────────────────────────────────────── */

const AlertContext = createContext<AlertContextValue | null>(null);

export function AlertProvider({ children }: { children: ReactNode }) {
  const [alertModal, setAlertModal] = useState<AlertModalData | null>(null);
  const [unreadCount, setUnreadCount] = useState(0);
  const initialFetchRef = useRef(false);

  // 마운트 시 읽지 않은 알림 수 조회
  useEffect(() => {
    if (initialFetchRef.current) return;
    initialFetchRef.current = true;
    getUnreadCount()
      .then((res) => setUnreadCount(res.count))
      .catch(() => {});
  }, []);

  const refreshUnreadCount = useCallback(() => {
    getUnreadCount()
      .then((res) => setUnreadCount(res.count))
      .catch(() => {});
  }, []);

  const incrementUnreadCount = useCallback(() => {
    setUnreadCount((prev) => prev + 1);
  }, []);

  const decrementUnreadCount = useCallback((by = 1) => {
    setUnreadCount((prev) => Math.max(0, prev - by));
  }, []);

  const resetUnreadCount = useCallback(() => {
    setUnreadCount(0);
  }, []);

  const closeAlert = useCallback(() => {
    setAlertModal(null);
  }, []);

  const showInfo = useCallback(
    (title: string, message: string) => {
      setAlertModal({ title, message });
    },
    []
  );

  const showAlert = useCallback(
    (options: ShowAlertOptions) => {
      // 1. 모달 표시 (silent가 아닐 때)
      if (!options.silent) {
        setAlertModal({
          title: options.title,
          message: options.message,
          errorCode: options.errorCode,
          errorType: options.errorType,
          severity: options.severity,
          robotSn: options.robotSn,
          timestamp: new Date().toLocaleString("ko-KR", {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
            hour12: false,
          }),
        });
      }

      // 2. DB 저장 (errorCode + errorType 있을 때만)
      if (options.errorCode && options.errorType) {
        const logData: AlarmLogCreateRequest = {
          error_code: options.errorCode,
          error_type: options.errorType,
          severity: options.severity ?? "error",
          message: options.message,
          description: options.description,
          source: options.source,
          robot_sn: options.robotSn,
        };

        createAlarmLog(logData)
          .then(() => incrementUnreadCount())
          .catch((err) =>
            console.error("[AlertContext] 알람 로그 저장 실패:", err)
          );
      }
    },
    [incrementUnreadCount]
  );

  const contextValue = useMemo(
    () => ({
      showAlert,
      showInfo,
      alertModal,
      closeAlert,
      unreadCount,
      refreshUnreadCount,
      incrementUnreadCount,
      decrementUnreadCount,
      resetUnreadCount,
    }),
    [showAlert, showInfo, alertModal, closeAlert, unreadCount, refreshUnreadCount, incrementUnreadCount, decrementUnreadCount, resetUnreadCount]
  );

  return (
    <AlertContext.Provider value={contextValue}>
      {children}
    </AlertContext.Provider>
  );
}

export function useAlert(): AlertContextValue {
  const ctx = useContext(AlertContext);
  if (!ctx) {
    throw new Error("useAlert must be used within an AlertProvider");
  }
  return ctx;
}
