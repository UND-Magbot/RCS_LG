"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Image from "next/image";
import type { UserDropdownProps } from "@/lib/types/shell";
import "./user-dropdown.css";

export function UserDropdown({
  userName,
  iconSrc = "/icon/Icon (8).png",
}: UserDropdownProps) {
  const [open, setOpen] = useState(false);
  const [displayName, setDisplayName] = useState(userName ?? "");
  const triggerRef = useRef<HTMLDivElement>(null);
  const router = useRouter();

  useEffect(() => {
    const stored = localStorage.getItem("user_login_id");
    if (stored) setDisplayName(stored);
  }, []);

  const handleClose = useCallback(() => {
    setOpen(false);
  }, []);

  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") handleClose();
    };

    const handleClickOutside = (e: MouseEvent) => {
      if (
        triggerRef.current &&
        !triggerRef.current.contains(e.target as Node)
      ) {
        handleClose();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [open, handleClose]);

  const handleLogout = () => {
    handleClose();
    localStorage.removeItem("auth_token");
    localStorage.removeItem("user_login_id");
    localStorage.removeItem("user_role");
    router.push("/auth/login");
  };

  return (
    <div className="user-dropdown" ref={triggerRef}>
      <button
        type="button"
        className="user-dropdown__trigger"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="user-dropdown__icon" aria-hidden="true">
          <Image
            src={iconSrc}
            alt=""
            width={18}
            height={18}
            className="user-dropdown__icon-img"
          />
        </span>
        <span>{displayName}</span>
        <span
          className={
            open
              ? "user-dropdown__arrow user-dropdown__arrow--open"
              : "user-dropdown__arrow"
          }
        >
          ▾
        </span>
      </button>

      {open ? (
        <div className="user-dropdown__menu" role="menu">
          <button
            type="button"
            className="user-dropdown__item"
            role="menuitem"
            onClick={handleLogout}
          >
            로그아웃
          </button>
        </div>
      ) : null}
    </div>
  );
}
