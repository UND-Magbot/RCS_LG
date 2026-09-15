"use client";

import { Modal } from "../Modal";
import type { ConfirmModalProps } from "@/lib/types/robots";
import "./ConfirmModal.css";

export function ConfirmModal({
  open,
  title,
  message,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  return (
    <Modal open={open} onClose={onCancel} title={title} width="400px">
      <div className="confirm-modal">
        <p className="confirm-modal__message">{message}</p>
        <div className="confirm-modal__actions">
          <button className="btn" onClick={onCancel}>
            취소
          </button>
          <button className="btn btn--primary" onClick={onConfirm}>
            확인
          </button>
        </div>
      </div>
    </Modal>
  );
}
