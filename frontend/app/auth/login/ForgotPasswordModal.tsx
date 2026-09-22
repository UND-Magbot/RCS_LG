"use client";

import { useState } from "react";
import { Modal } from "@/app/components/ui/Modal";

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface ForgotPasswordModalProps {
  open: boolean;
  onClose: () => void;
}

export function ForgotPasswordModal({ open, onClose }: ForgotPasswordModalProps) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");

  const handleChange = (value: string) => {
    setEmail(value);
    if (error) setError("");
  };

  const handleConfirm = () => {
    if (!email.trim()) {
      setError("이메일을 입력해 주세요.");
      return;
    }
    if (!EMAIL_REGEX.test(email)) {
      setError("올바르지 않은 이메일 형식입니다.");
      return;
    }
    // TODO: API 연동 - 임시 비밀번호 발급 요청
    console.log("임시 비밀번호 발급 요청:", email);
    setEmail("");
    setError("");
    onClose();
  };

  const handleClose = () => {
    setEmail("");
    setError("");
    onClose();
  };

  return (
    <Modal open={open} onClose={handleClose} title="비밀번호 찾기" width="420px">
      <div className="forgot-pw__body">
        <p className="forgot-pw__text">
          이메일 주소를 입력하시면 임시 비밀번호를 발송해 드립니다.
        </p>
        <div>
          <input
            className={`forgot-pw__input${error ? " login__input--error" : ""}`}
            type="email"
            placeholder="이메일 주소"
            value={email}
            onChange={(e) => handleChange(e.target.value)}
          />
          {error && <span className="login__error">{error}</span>}
        </div>
        <div className="forgot-pw__actions">
          <button
            className="forgot-pw__btn forgot-pw__btn--cancel"
            type="button"
            onClick={handleClose}
          >
            취소
          </button>
          <button
            className="forgot-pw__btn forgot-pw__btn--confirm"
            type="button"
            onClick={handleConfirm}
          >
            확인
          </button>
        </div>
      </div>
    </Modal>
  );
}
