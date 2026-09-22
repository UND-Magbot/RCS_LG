"use client";

import { useState, useEffect, useCallback } from "react";
import { apiFetch } from "@/lib/api";

interface BrowseResult {
  current: string;
  parent: string | null;
  dirs: string[];
}

interface DirPickerModalProps {
  initialPath: string;
  onConfirm: (path: string) => void;
  onClose: () => void;
}

export function DirPickerModal({ initialPath, onConfirm, onClose }: DirPickerModalProps) {
  const [current, setCurrent] = useState(initialPath);
  const [dirs, setDirs] = useState<string[]>([]);
  const [parent, setParent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const browse = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    try {
      const res = await apiFetch<BrowseResult>(
        `/api/backup/browse?path=${encodeURIComponent(path)}`
      );
      setCurrent(res.current);
      setParent(res.parent);
      setDirs(res.dirs);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "경로 조회 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    browse(initialPath);
  }, [initialPath, browse]);

  const handleEnter = (dir: string) => {
    browse(`${current}/${dir}`.replace("//", "/"));
  };

  const handleUp = () => {
    if (parent) browse(parent);
  };

  return (
    <div className="dir-picker__overlay" onClick={onClose}>
      <div className="dir-picker__modal" onClick={(e) => e.stopPropagation()}>
        <div className="dir-picker__header">
          <span className="dir-picker__title">폴더 선택</span>
          <button className="dir-picker__close" onClick={onClose}>✕</button>
        </div>

        <div className="dir-picker__breadcrumb">
          <span>{current}</span>
        </div>

        <div className="dir-picker__list">
          {loading && <div className="dir-picker__loading">로딩 중...</div>}
          {error && <div className="dir-picker__error">{error}</div>}

          {!loading && parent && (
            <button className="dir-picker__item dir-picker__item--up" onClick={handleUp}>
              <span className="dir-picker__icon">📁</span>
              <span>..</span>
            </button>
          )}

          {!loading && dirs.length === 0 && !error && (
            <div className="dir-picker__empty">하위 폴더 없음</div>
          )}

          {!loading && dirs.map((dir) => (
            <button
              key={dir}
              className="dir-picker__item"
              onClick={() => handleEnter(dir)}
            >
              <span className="dir-picker__icon">📁</span>
              <span>{dir}</span>
            </button>
          ))}
        </div>

        <div className="dir-picker__footer">
          <span className="dir-picker__selected">{current}</span>
          <button
            className="dir-picker__btn dir-picker__btn--confirm"
            onClick={() => onConfirm(current.endsWith("/") ? current : current + "/")}
          >
            선택
          </button>
        </div>
      </div>
    </div>
  );
}
