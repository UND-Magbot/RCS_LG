"use client";

import { useState, useEffect } from "react";
import { Modal } from "../Modal";
import { apiFetch } from "@/lib/api";
import { useAlert } from "@/lib/context/AlertContext";
import type { MappingSetupModalProps } from "@/lib/types/map";

let areaCounter = 6;

type AreaItem = { area_id: number; name: string };

export function MappingSetupModal({
  open,
  businesses,
  onClose,
  onConfirm,
}: MappingSetupModalProps) {
  const [selectedBusinessId, setSelectedBusinessId] = useState<number | null>(null);
  const [areaName, setAreaName] = useState("");
  const [existingAreas, setExistingAreas] = useState<AreaItem[]>([]);
  const [duplicateError, setDuplicateError] = useState(false);
  const { showInfo } = useAlert();

  // 사업장 변경 시 해당 영역 목록 로드
  useEffect(() => {
    if (!selectedBusinessId) {
      setExistingAreas([]);
      return;
    }
    apiFetch<{ items: AreaItem[] }>(
      `/api/map/businesses/${selectedBusinessId}/areas`
    )
      .then((data) => setExistingAreas(data.items))
      .catch((err) => {
        console.error("[매핑설정 영역 목록 로드 실패]", err);
        setExistingAreas([]);
      });
  }, [selectedBusinessId]);

  // 영역 이름 중복 검사
  useEffect(() => {
    const trimmed = areaName.trim().toLowerCase();
    if (!trimmed) {
      setDuplicateError(false);
      return;
    }
    const isDuplicate = existingAreas.some(
      (a) => a.name.toLowerCase() === trimmed
    );
    setDuplicateError(isDuplicate);
  }, [areaName, existingAreas]);

  const handleConfirm = () => {
    if (selectedBusinessId === null || !areaName.trim()) return;
    if (duplicateError) {
      showInfo("안내", "이미 존재하는 영역 이름입니다.");
      return;
    }
    const generatedAreaId = `area-${String(areaCounter++).padStart(3, "0")}`;
    onConfirm(selectedBusinessId, generatedAreaId, areaName.trim());
    setSelectedBusinessId(null);
    setAreaName("");
  };

  const handleClose = () => {
    setSelectedBusinessId(null);
    setAreaName("");
    setDuplicateError(false);
    onClose();
  };

  const isValid = selectedBusinessId !== null && areaName.trim().length > 0 && !duplicateError;

  return (
    <Modal open={open} onClose={handleClose} title="맵핑 설정" width="460px">
      <div className="mapping-setup">
        <div className="mapping-setup__field">
          <label className="mapping-setup__label">사업장</label>
          <select
            className="mapping-setup__select"
            value={selectedBusinessId ?? ""}
            onChange={(e) =>
              setSelectedBusinessId(e.target.value ? Number(e.target.value) : null)
            }
          >
            <option value="">-- 사업장 선택 --</option>
            {businesses.map((b) => (
              <option key={b.business_id} value={b.business_id}>
                {b.name}
              </option>
            ))}
          </select>
        </div>

        <div className="mapping-setup__field">
          <label className="mapping-setup__label">영역 이름</label>
          <input
            className={`mapping-setup__input${duplicateError ? " mapping-setup__input--error" : ""}`}
            type="text"
            placeholder="새 영역 이름 입력"
            value={areaName}
            onChange={(e) => setAreaName(e.target.value)}
          />
          {duplicateError && (
            <span className="mapping-setup__error">이미 존재하는 영역 이름입니다.</span>
          )}
        </div>

        <div className="mapping-setup__actions">
          <button className="mapping-setup__btn mapping-setup__btn--close" onClick={handleClose}>
            닫기
          </button>
          <button
            className="mapping-setup__btn mapping-setup__btn--confirm"
            onClick={handleConfirm}
            disabled={!isValid}
          >
            확인
          </button>
        </div>
      </div>
    </Modal>
  );
}
