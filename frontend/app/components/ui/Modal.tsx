"use client";

import { useEffect, useRef } from "react";
import { IconButton } from "./IconButton";
import type { ModalProps } from "@/lib/types/ui";
import "./Modal.css";

export function Modal({
  open,
  onClose,
  title,
  children,
  width = "480px",
  height,
}: ModalProps) {
  const overlayRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  const handleOverlayClick = (e: React.MouseEvent) => {
    if (e.target === overlayRef.current) onClose();
  };

  if (!open) return null;

  return (
    <div
      className="modal__overlay"
      ref={overlayRef}
      onClick={handleOverlayClick}
    >
      <div
        className="modal"
        style={{ width, height }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modal__header">
          <h2 className="modal__title">{title}</h2>
          <IconButton
            className="modal__close"
            variant="ghost"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </IconButton>
        </div>
        <div className="modal__body">{children}</div>
      </div>
    </div>
  );
}
