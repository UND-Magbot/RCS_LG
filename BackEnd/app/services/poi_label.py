"""POI 표시 이름(한글) · 구역 매핑 — LGIT 요청 4·5번.

## 왜 이름을 그냥 바꾸지 않는가
현장에서 부르는 이름은 "자재실 출발"인데 시스템 안의 이름은 `R1` 이다.
그렇다고 `map_pois.name` 을 한글로 바꾸면 **시스템이 조용히 망가진다.**
`R1`/`J1` 이라는 문자열에 매달려 있는 곳이 한두 군데가 아니기 때문이다.

  · `job_points.r_poi_name` / `j_poi_name`  — J↔R 매핑이 이름으로 저장돼 있다
  · 진입점 규약 `"<작업지점>-1"`             — 이름을 조합해서 찾는다
  · 로봇 맵 overlay 동기화                   — 로봇 안의 POI 이름과 맞춰야 한다

그래서 **저장·조회는 영문 이름 그대로 두고, 화면에 내보낼 때만 한글로 바꾼다.**
이 모듈이 그 '번역기' 다. 매핑에 없는 이름은 **원래 이름을 그대로** 돌려준다
(= 설정 파일을 지우면 종전 화면으로 완전히 되돌아간다).

## 저장소
`BackEnd/static/poi_labels.json`
```json
{ "R1": { "label": "자재실 출발", "zone": "자재실" } }
```
`label` 은 화면에 보일 이름, `zone` 은 콘솔 2분할에서 어느 칸에 넣을지.
DB 를 건드리지 않으므로 마이그레이션이 필요 없다(운영 서버는 create_all 만 한다).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent.parent.parent / "static" / "poi_labels.json"

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_cache_mtime: Optional[float] = None
_cache_loaded = False


def _load_locked() -> dict[str, dict]:
    """파일 내용을 캐시에 채운다. mtime 이 그대로면 디스크를 읽지 않는다.

    콘솔·태블릿이 3초마다 폴링하고 POI 하나당 한 번씩 조회하므로
    매번 파일을 읽으면 디스크 I/O 가 쌓인다. mtime 만 보고 건너뛴다.
    """
    global _cache, _cache_mtime, _cache_loaded
    try:
        mtime = _PATH.stat().st_mtime if _PATH.exists() else None
    except OSError:
        mtime = None

    if _cache_loaded and mtime == _cache_mtime:
        return _cache

    data: dict[str, dict] = {}
    if mtime is not None:
        try:
            raw = json.loads(_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for name, v in raw.items():
                    if isinstance(v, dict):
                        data[str(name)] = {
                            "label": str(v.get("label") or "").strip(),
                            "zone": (str(v.get("zone")).strip()
                                     if v.get("zone") else None),
                        }
                    elif isinstance(v, str):
                        # 짧은 형식도 받아준다: {"R1": "자재실 출발"}
                        data[str(name)] = {"label": v.strip(), "zone": None}
        except Exception as e:
            logger.warning(f"[poi_label] poi_labels.json 읽기 실패(무시): {e}")
            data = {}

    _cache = data
    _cache_mtime = mtime
    _cache_loaded = True
    return _cache


def all_labels() -> dict[str, dict]:
    """전체 매핑(콘솔 편집 화면용)."""
    with _lock:
        return {k: dict(v) for k, v in _load_locked().items()}


def label_for(name: Optional[str]) -> Optional[str]:
    """표시용 이름. 매핑이 없으면 **원래 이름 그대로**.

    ★ 표시 계층에서만 쓸 것. 이 값을 다시 조회 키로 쓰면 안 된다.
    """
    if not name:
        return name
    with _lock:
        info = _load_locked().get(name)
    return (info or {}).get("label") or name


def zone_for(name: Optional[str]) -> Optional[str]:
    """그 POI 가 속한 구역 이름. 지정이 없으면 None(= 콘솔의 '기타' 영역)."""
    if not name:
        return None
    with _lock:
        info = _load_locked().get(name)
    return (info or {}).get("zone") or None


def zone_order() -> list[str]:
    """구역 표시 순서 — 설정 파일에 나온 순서를 그대로 쓴다.

    콘솔 좌/우 칸 순서가 폴링마다 바뀌면 안 되므로 사전순이 아니라 **파일 순서**다.
    (좌: 자재실, 우: EOL 처럼 현장 동선대로 적어두면 그대로 나온다)
    """
    out: list[str] = []
    with _lock:
        data = _load_locked()
    for info in data.values():
        z = info.get("zone")
        if z and z not in out:
            out.append(z)
    return out


def save_all(mapping: dict) -> dict[str, dict]:
    """전체 매핑을 덮어쓴다. 저장 후 캐시를 즉시 무효화한다.

    빈 dict 를 주면 매핑이 비워져 **전부 원래 이름으로 되돌아간다**(롤백 경로).
    """
    global _cache_loaded
    clean: dict[str, dict] = {}
    for name, v in (mapping or {}).items():
        key = str(name).strip()
        if not key:
            continue
        if isinstance(v, dict):
            label = str(v.get("label") or "").strip()
            zone = str(v.get("zone")).strip() if v.get("zone") else None
        else:
            label, zone = str(v).strip(), None
        if not label and not zone:
            continue
        clean[key] = {"label": label or key, "zone": zone or None}

    with _lock:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(clean, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        _cache_loaded = False          # 다음 조회 때 다시 읽는다
        _load_locked()
    logger.info(f"[poi_label] 표시 이름 {len(clean)}건 저장")
    return all_labels()
