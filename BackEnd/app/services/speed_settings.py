"""잭 상태별 2단 주행 속도 — LGIT 요청 1번.

랙을 들고 있을 때(적재)와 빈 몸일 때(공차)의 안전한 속도는 다르다.
랙을 들면 무게중심이 높아지고 제동거리가 길어지므로 더 천천히 가야 한다.
지금까지는 로봇당 속도가 **하나뿐**(`robots.max_speed`)이라 그 구분이 없었다.

## 저장소 — `BackEnd/static/robot_speed.json`
```json
{ "192.168.39.100": { "empty": 1.2, "laden": 0.8 } }
```
**DB 스키마를 바꾸지 않는다.** 운영 서버는 `Base.metadata.create_all` 만 돌기 때문에
모델에 컬럼을 추가해도 기존 테이블에는 생기지 않고, 그 상태로 뜨면
`unknown column` 으로 **부팅 자체가 실패**한다. 그래서 설정 파일로 뺀다.

## 하위호환
  · `empty` 값이 없으면 **DB `robots.max_speed`** 를 쓴다 (종전 단일 속도 그대로).
  · `empty` 를 바꾸면 DB `robots.max_speed` 에도 같이 쓴다 →
    서버 시작 시 속도를 밀어넣는 기존 경로(`main.py` `_apply_saved_speeds`)가
    그대로 동작한다. 그 경로를 건드리지 않으려고 일부러 이중으로 저장한다.
  · `laden` 기본값은 0.8 m/s.

## 지금 어느 쪽인가 (적재 판정)
`jack_service.is_laden(ip)` 이 들고 있다. 잭 업 성공 → 적재, 잭 다운 성공 → 공차.
`jack_service` 안에서 갱신하므로 **어느 경로로 잭을 올리든 자동으로 따라간다**
(배차 워커·원격제어·잭 테스트 전부 같은 함수를 쓴다).
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent.parent.parent / "static" / "robot_speed.json"

# 안전 기본값.
#   공차 : 값이 없으면 DB robots.max_speed → 그것도 없으면 아래 값 (종전과 같음)
#   적재 : 신규. 랙을 들면 제동거리가 길어지므로 더 낮게 잡는다
DEFAULT_EMPTY = 1.2
DEFAULT_LADEN = 0.8

# 허용 범위 — 콘솔 슬라이더와 같은 폭. 0 을 쓰면 로봇이 아예 못 움직인다.
MIN_SPEED = 0.1
MAX_SPEED = 1.2

_lock = threading.Lock()


def _read() -> dict:
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[speed] robot_speed.json 읽기 실패(기본값 사용): {e}")
        return {}


def _write(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _db_max_speed(ip: str) -> Optional[float]:
    """DB 에 저장된 종전 단일 속도. 조회 실패는 None (기본값으로 떨어진다)."""
    try:
        from app.database import SessionLocal
        from app.models.robot import Robot
        db = SessionLocal()
        try:
            r = db.query(Robot).filter(Robot.ip_address == ip).first()
            return float(r.max_speed) if (r and r.max_speed) else None
        finally:
            db.close()
    except Exception:
        return None


def _clamp(v: float) -> float:
    return max(MIN_SPEED, min(MAX_SPEED, float(v)))


def get(ip: str) -> dict:
    """이 로봇의 {empty, laden}. 값이 없으면 하위호환 기본값으로 채워 돌려준다."""
    with _lock:
        saved = (_read().get(ip) or {}) if ip else {}
    try:
        empty = float(saved["empty"])
    except (KeyError, TypeError, ValueError):
        empty = _db_max_speed(ip) or DEFAULT_EMPTY
    try:
        laden = float(saved["laden"])
    except (KeyError, TypeError, ValueError):
        # ★ 기본값은 0.8 이되 **공차 속도를 넘지 않게** 깎는다.
        #   현장 로봇의 DB max_speed 가 0.7 인 경우가 실제로 있었다(2026-09-14 실측).
        #   그대로 0.8 을 쓰면 "랙을 들면 오히려 빨라지는" 설정이 된다 —
        #   기본값 때문에 안전이 나빠지는 일은 없어야 한다.
        laden = min(DEFAULT_LADEN, empty)
    return {"empty": _clamp(empty), "laden": _clamp(laden)}


def speed_for(ip: str, laden: bool) -> float:
    """지금 상태에 맞는 속도 한 값. 안전존 해제 시 복구할 기준값이기도 하다."""
    cfg = get(ip)
    return cfg["laden"] if laden else cfg["empty"]


def set_speeds(ip: str, empty: Optional[float] = None,
               laden: Optional[float] = None) -> dict:
    """값을 저장한다. 준 항목만 바꾼다.

    `empty` 를 바꿀 때는 **DB `robots.max_speed` 에도 같이 쓴다** — 서버 시작 때
    로봇에 속도를 밀어넣는 기존 경로를 그대로 살려두기 위해서다(모듈 독스트링 참조).
    """
    cur = get(ip)
    new_empty = _clamp(empty) if empty is not None else cur["empty"]
    new_laden = _clamp(laden) if laden is not None else cur["laden"]

    with _lock:
        data = _read()
        data[ip] = {"empty": new_empty, "laden": new_laden}
        try:
            _write(data)
        except Exception as e:
            logger.exception(f"[speed] robot_speed.json 저장 실패: {e}")
            raise

    if empty is not None:
        _save_db_max_speed(ip, new_empty)
    logger.info(f"[speed] {ip} 공차 {new_empty} / 적재 {new_laden} m/s 저장")
    if new_laden > new_empty:
        # 막지는 않는다 — 현장에서 일부러 그렇게 두어야 할 사정이 있을 수 있다.
        # 다만 사고 후 로그에서 "왜 랙 들고 더 빨랐나" 를 추적할 수 있어야 한다.
        logger.warning(f"[speed] {ip} ★ 적재 속도({new_laden})가 공차({new_empty})보다 "
                       f"빠르게 설정됨 — 랙을 들면 제동거리가 길어진다. 의도한 값인지 확인할 것")
    return {"empty": new_empty, "laden": new_laden}


def _save_db_max_speed(ip: str, v: float) -> None:
    try:
        from app.database import SessionLocal
        from app.models.robot import Robot
        db = SessionLocal()
        try:
            r = db.query(Robot).filter(Robot.ip_address == ip).first()
            if r:
                r.max_speed = v
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning(f"[speed] {ip} DB max_speed 저장 실패(파일에는 저장됨)")


def all_settings() -> dict:
    with _lock:
        return dict(_read())
