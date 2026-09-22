"use client";

import { useAlert } from "@/lib/context/AlertContext";
import { Modal } from "../ui/Modal";
import "./GlobalAlertModal.css";

const ERROR_TYPE_LABEL: Record<string, string> = {
  auth: "인증 오류",
  task: "작업 오류",
  robot: "로봇 오류",
  map: "맵 오류",
  net: "네트워크 오류",
  data: "데이터 오류",
};

export function GlobalAlertModal() {
  const { alertModal, closeAlert } = useAlert();
  if (!alertModal) return null;

  const isError = !!alertModal.errorCode;
  const typeLabel = alertModal.errorType
    ? ERROR_TYPE_LABEL[alertModal.errorType] ?? alertModal.errorType
    : "";

  return (
    <Modal
      open
      onClose={closeAlert}
      title="알림"
      width="460px"
    >
      {isError ? (
        <div className="alert-modal">
          <div className="alert-modal__header-row">
            <span className="alert-modal__code">{alertModal.errorCode}</span>
            <span className="alert-modal__type">{typeLabel}</span>
          </div>

          <div className="alert-modal__detail-row">
            {alertModal.robotSn && (
              <span className="alert-modal__tag">{alertModal.robotSn}</span>
            )}
            <span className="alert-modal__tag">{typeLabel}</span>
            <span className="alert-modal__message">{alertModal.message}</span>
          </div>

          {alertModal.timestamp && (
            <p className="alert-modal__timestamp">{alertModal.timestamp}</p>
          )}

          <div className="alert-modal__actions">
            <button className="btn btn--primary" onClick={closeAlert}>
              확인
            </button>
          </div>
        </div>
      ) : (
        <div className="confirm-modal">
          <p className="confirm-modal__message">{alertModal.message}</p>
          <div className="confirm-modal__actions">
            <button className="btn btn--primary" onClick={closeAlert}>
              확인
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
