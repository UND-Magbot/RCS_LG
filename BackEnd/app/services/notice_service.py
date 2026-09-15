"""관제 알림 허브 — 화면(콘솔·태블릿)에 띄울 안내 문구를 모아두는 메모리 게시판.

## 왜 DB 가 아닌가
`safety_zone.active_alerts()` 와 같은 성격이다. "지금 화면에 떠 있어야 하는 것"만
담으며, **서버가 재시작되면 사라지는 것이 맞다**. 서버가 다시 뜬 시점에는
로봇 상태도 원점으로 돌아가므로, 예전 알림이 살아나면 오히려 거짓 정보가 된다.
이력이 필요한 건 `alarm_logs`(DB)가 따로 담당한다.

## 구성 요소
- `kind`   : 알림 종류. 자동 해제(`clear_by_kind`)를 종류 단위로 하려고 둔다.
- `targets`: 이 알림을 볼 화면. `"console"` 또는 `"poi:<poi_id>"`.
             한 알림이 여러 화면에 동시에 뜰 수 있다(도킹 실패 = 콘솔 + 그 R 태블릿).
- `key`    : 같은 key 로 push 하면 **기존 알림을 갈아끼운다.** 도킹 실패 회차
             (1/4 → 2/4 → …)가 쌓이지 않고 한 줄로 갱신되게 하는 장치다.
- `ack_required`: True 면 사람이 [확인]을 눌러야(`ack`) 사라진다.
             강제 종료처럼 "봤다는 사실"이 중요한 알림에만 쓴다.
- `ttl_sec`: 마지막 안전망. 해제 코드가 어떤 이유로든 실행되지 않아도
             이 시간이 지나면 조회 시점에 스스로 사라진다.

## 규칙
조회(`list_for`)는 **절대 예외를 밖으로 던지지 않는다.** 이 알림 하나 때문에
태블릿 폴링이 통째로 죽으면 작업자는 호출 버튼조차 못 누른다.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 알림 종류 — 문자열 오타를 막으려고 상수로 둔다
KIND_DOCK_FAIL = "dock_fail"                    # 랙 정렬(도킹) 실패 — 회차 안내
KIND_DOCK_FAIL_FINAL = "dock_fail_final"        # 재시도 소진 → 충전소 복귀
KIND_JOB_FORCE_CLEAR = "job_force_clear"        # 현재 JOB 강제 종료
KIND_JOB_FORCE_CLEAR_ALL = "job_force_clear_all"  # 전체 강제 종료

TARGET_CONSOLE = "console"


def poi_target(poi_id: int) -> str:
    """POI 태블릿 대상 문자열. 여기저기서 f-string 을 직접 쓰면 오타가 난다."""
    return f"poi:{int(poi_id)}"


_lock = threading.Lock()
_notices: dict[int, dict] = {}      # id → 알림
_key_index: dict[str, int] = {}     # key → id (교체용)
_seq = itertools.count(1)


def _expired(n: dict, now: float) -> bool:
    ttl = n.get("ttl_sec")
    return bool(ttl) and (now - n["created_at"] > float(ttl))


def _prune_locked(now: float) -> None:
    """만료된 알림 정리. **반드시 _lock 을 잡은 상태에서** 호출한다."""
    dead = [nid for nid, n in _notices.items() if _expired(n, now)]
    for nid in dead:
        n = _notices.pop(nid, None)
        if n and n.get("key") and _key_index.get(n["key"]) == nid:
            _key_index.pop(n["key"], None)


def push(kind: str, message: str, targets: list[str] | tuple[str, ...],
         ack_required: bool = False, key: Optional[str] = None,
         ttl_sec: Optional[float] = None) -> Optional[int]:
    """알림 등록. 같은 `key` 가 이미 있으면 그것을 교체한다. 실패해도 예외를 안 던진다.

    반환: 알림 id (실패 시 None)
    """
    try:
        now = time.time()
        with _lock:
            _prune_locked(now)
            if key:
                old = _key_index.pop(key, None)
                if old is not None:
                    _notices.pop(old, None)
            nid = next(_seq)
            _notices[nid] = {
                "id": nid,
                "kind": kind,
                "message": message,
                "targets": list(targets or []),
                "created_at": now,
                "ack_required": bool(ack_required),
                "acked": False,
                "key": key,
                "ttl_sec": ttl_sec,
            }
            if key:
                _key_index[key] = nid
        logger.info(f"[notice] + {kind} → {list(targets or [])} : "
                    f"{message.splitlines()[0] if message else ''}")
        return nid
    except Exception:
        logger.exception("[notice] push 실패")
        return None


def clear(key: str) -> bool:
    """key 로 등록한 알림을 내린다(자동 해제용). 없으면 False."""
    try:
        with _lock:
            nid = _key_index.pop(key, None)
            if nid is None:
                return False
            n = _notices.pop(nid, None)
        if n:
            logger.info(f"[notice] - {n['kind']} (key={key}) 자동 해제")
        return n is not None
    except Exception:
        logger.exception("[notice] clear 실패")
        return False


def clear_by_kind(kind: str, target: Optional[str] = None) -> int:
    """종류(+대상)로 일괄 해제. 해제한 개수를 돌려준다."""
    try:
        with _lock:
            dead = [
                nid for nid, n in _notices.items()
                if n["kind"] == kind and (target is None or target in n["targets"])
            ]
            for nid in dead:
                n = _notices.pop(nid, None)
                if n and n.get("key") and _key_index.get(n["key"]) == nid:
                    _key_index.pop(n["key"], None)
        if dead:
            logger.info(f"[notice] - {kind} {len(dead)}건 해제 (target={target})")
        return len(dead)
    except Exception:
        logger.exception("[notice] clear_by_kind 실패")
        return 0


def ack(notice_id: int) -> bool:
    """사람이 [확인] 을 눌렀다. `ack_required` 인 알림만 내려간다.

    ack_required 가 아닌 알림(도킹 실패 등)은 **조건이 풀려야** 사라지는 것이므로
    여기서 지우지 않는다. 지워봐야 다음 실패 회차에 다시 뜬다.
    """
    try:
        with _lock:
            n = _notices.get(int(notice_id))
            if n is None:
                return False
            if not n["ack_required"]:
                return False
            _notices.pop(int(notice_id), None)
            if n.get("key") and _key_index.get(n["key"]) == int(notice_id):
                _key_index.pop(n["key"], None)
        logger.info(f"[notice] - {n['kind']} 확인 처리(ack)")
        return True
    except Exception:
        logger.exception("[notice] ack 실패")
        return False


def _to_out(n: dict, now: float) -> dict:
    return {
        "id": n["id"],
        "kind": n["kind"],
        "message": n["message"],
        "ack_required": n["ack_required"],
        "seconds": int(now - n["created_at"]),
    }


def list_for(target: str) -> list[dict]:
    """그 화면에 지금 떠 있어야 하는 알림. 오래된 것부터.

    ★ 어떤 경우에도 예외를 던지지 않는다 — 화면 폴링이 이것 때문에 죽으면 안 된다.
    """
    try:
        now = time.time()
        with _lock:
            _prune_locked(now)
            items = [n for n in _notices.values() if target in n["targets"]]
        items.sort(key=lambda n: n["id"])
        return [_to_out(n, now) for n in items]
    except Exception:
        logger.exception("[notice] list_for 실패")
        return []


def list_all() -> list[dict]:
    """디버그용 — 지금 살아 있는 알림 전부(대상 포함)."""
    try:
        now = time.time()
        with _lock:
            _prune_locked(now)
            items = sorted(_notices.values(), key=lambda n: n["id"])
        return [{**_to_out(n, now), "targets": list(n["targets"])} for n in items]
    except Exception:
        logger.exception("[notice] list_all 실패")
        return []


def reset() -> None:
    """전부 비운다 (테스트·수동 정리용)."""
    with _lock:
        _notices.clear()
        _key_index.clear()
