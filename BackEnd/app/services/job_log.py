"""배송 작업 기록(dispatch_job_log) — 기록 · 조회 · 요약 (2026-11-06).

## 무엇을 하나
배차 콘솔 [로그] 화면이 보여 줄 "작업 1건 = R 에서 랙 적재 → 짝 J 하차" 기록을 남긴다.
배송 워커(`dispatch_service._worker_loop_delivery`)가 세 지점에서 부른다.

  begin()     — 작업 출발 (세션 시작 · 이어받기 시작)
  unloaded()  — J 하차 완료 → result=completed
  close()     — 하차 전에 끝남(랙 없음 · 이동 실패 · 강제 종료 · 예외)

## 비유
배차 워커가 "일꾼"이면 이 모듈은 옆에서 **작업 일지만 적는 서기**다.
서기가 펜을 떨어뜨려도(DB 오류) 일꾼은 하던 일을 계속해야 한다 —
그래서 **모든 공개 함수는 예외를 밖으로 내보내지 않는다**(로그만 남긴다).

## 연속 작업
J 하차 **직후**, 충전소 복귀를 시작하기 전에 바로 다음 작업으로 넘어간 경우만 continued=True.
판정은 워커가 한다(begin 의 continued 인자). 복귀 중 이어받기·배터리 보류 뒤 작업은 False.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Optional

from app.database import SessionLocal, engine
from app.models.dispatch import DispatchJobLog, DispatchReservation

logger = logging.getLogger(__name__)

# 로봇별 진행 중 작업 id / 마지막 완료 작업 id (연속 작업의 prev_job_id 용)
_lock = threading.Lock()
_open: dict[int, int] = {}
_last_done: dict[int, int] = {}

_table_ready = False


def _ensure_table() -> None:
    """테이블이 없으면 만든다.

    정상 경로는 서버 기동 때 create_all 이 만든다. 이건 서버를 재시작하기 전에
    코드만 바뀐 경우(--reload 누락·격리 시험의 DB 사본 등)의 보험이다.
    """
    global _table_ready
    if _table_ready:
        return
    DispatchJobLog.__table__.create(bind=engine, checkfirst=True)
    _table_ready = True


def _robot_name(db, robot_id: int) -> Optional[str]:
    from app.models.robot import Robot
    r = db.query(Robot).filter(Robot.id == robot_id).first()
    return r.name if r else None


def _call_time(db, j_poi_id: Optional[int], res_id: Optional[int]) -> Optional[datetime]:
    """호출 시각 = 예약 생성 시각. 예약이 없으면(바로 배차) None → 호출자가 출발 시각을 쓴다.

    ※ dispatch_reservations.fulfilled_at 은 utcnow(), created_at 은 DB now()(로컬) 다.
      그래서 '방금 소화된 예약' 은 UTC 기준으로 찾고, 돌려주는 값은 created_at(로컬)이다.
    """
    if res_id:
        r = db.query(DispatchReservation).filter(DispatchReservation.id == res_id).first()
        return r.created_at if r else None
    if j_poi_id:
        # 예약 자동 호출(try_fulfill_reservations → start_session) 은 워커에 res_id 를 안 넘긴다.
        # 그 POI 에서 방금(15초 안) 소화된 예약이 있으면 그것을 호출로 본다.
        since = datetime.utcnow() - timedelta(seconds=15)
        r = (db.query(DispatchReservation)
             .filter(DispatchReservation.poi_id == j_poi_id,
                     DispatchReservation.status == "fulfilled",
                     DispatchReservation.fulfilled_at >= since)
             .order_by(DispatchReservation.fulfilled_at.desc())
             .first())
        return r.created_at if r else None
    return None


# ── 기록 (워커가 부른다 — 예외를 절대 밖으로 내보내지 않는다) ──────────────


def begin(*, robot_id: int, session_id: Optional[int], area_id: Optional[int],
          r_name: str, j_name: str, j_poi_id: Optional[int] = None,
          res_id: Optional[int] = None, continued: bool = False) -> None:
    """작업 출발 기록."""
    try:
        _ensure_table()
        now = datetime.now()
        db = SessionLocal()
        try:
            # 이 로봇의 끝나지 않은 live 기록(서버 재시작·놓친 종료)은 실패로 닫는다.
            # 남겨 두면 '진행 중' 으로 영영 보인다.
            stale = (db.query(DispatchJobLog)
                     .filter(DispatchJobLog.robot_id == robot_id,
                             DispatchJobLog.source == "live",
                             DispatchJobLog.result.is_(None))
                     .all())
            for s in stale:
                s.result = "failed"
                s.ended_at = s.ended_at or now
                s.note = s.note or "종료 기록 없음(중단)"
            with _lock:
                prev = _last_done.get(robot_id) if continued else None
            row = DispatchJobLog(
                area_id=area_id, robot_id=robot_id, robot_name=_robot_name(db, robot_id),
                r_name=r_name, j_name=j_name, route_key=f"{r_name}→{j_name}",
                call_at=_call_time(db, j_poi_id, res_id) or now,
                started_at=now, continued=bool(continued), prev_job_id=prev,
                source="live", session_id=session_id,
            )
            db.add(row)
            db.commit()
            with _lock:
                _open[robot_id] = row.id
        finally:
            db.close()
    except Exception:
        logger.exception(f"[joblog] 출발 기록 실패(무시) — robot={robot_id} {r_name}→{j_name}")


def unloaded(robot_id: int) -> None:
    """J 하차 완료 — 이 작업은 완료다(이후 복귀·충전은 작업에 넣지 않는다)."""
    try:
        with _lock:
            jid = _open.pop(robot_id, None)
        if not jid:
            return
        now = datetime.now()
        db = SessionLocal()
        try:
            row = db.query(DispatchJobLog).filter(DispatchJobLog.id == jid).first()
            if row:
                row.unloaded_at = now
                row.ended_at = now
                row.result = "completed"
                db.commit()
        finally:
            db.close()
        with _lock:
            _last_done[robot_id] = jid
    except Exception:
        logger.exception(f"[joblog] 하차 기록 실패(무시) — robot={robot_id}")


def close(robot_id: int, result: str, note: Optional[str] = None) -> None:
    """하차 전에 끝난 작업을 닫는다. 진행 중 작업이 없으면 아무 일도 안 한다."""
    try:
        with _lock:
            jid = _open.pop(robot_id, None)
        if not jid:
            return
        db = SessionLocal()
        try:
            row = db.query(DispatchJobLog).filter(DispatchJobLog.id == jid).first()
            if row and row.result is None:
                row.result = result
                row.ended_at = datetime.now()
                row.note = (note or "")[:200] or None
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception(f"[joblog] 종료 기록 실패(무시) — robot={robot_id} {result}")


# ── 조회 (라우터가 부른다) ─────────────────────────────────────────


UNITS = ("day", "week", "month")


def period_start(d: date, unit: str) -> date:
    """그 날짜가 속한 기간의 첫날. 주는 월요일 시작."""
    if unit == "week":
        return d - timedelta(days=d.weekday())
    if unit == "month":
        return d.replace(day=1)
    return d


def period_next(d: date, unit: str) -> date:
    """기간 첫날 → 다음 기간 첫날."""
    if unit == "week":
        return d + timedelta(days=7)
    if unit == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d + timedelta(days=1)


def period_label(d: date, unit: str) -> str:
    if unit == "week":
        e = d + timedelta(days=6)
        return f"{d:%m-%d}~{e:%m-%d}"
    if unit == "month":
        return f"{d:%Y-%m}"
    wk = "월화수목금토일"[d.weekday()]
    return f"{d:%m-%d}({wk})"


def sim_area_ids(db) -> set[int]:
    """시뮬레이션 구역 — 이름이 SIM 으로 시작하거나 SIM-* 로봇이 속한 구역."""
    from app.models.map import Area
    from app.models.robot import Robot
    out: set[int] = set()
    for a in db.query(Area).all():
        if (a.name or "").upper().startswith("SIM"):
            out.add(int(a.area_id))
    for r in db.query(Robot).all():
        if (r.serial_number or "").upper().startswith("SIM") and r.area_id is not None:
            try:
                out.add(int(r.area_id))
            except (TypeError, ValueError):
                pass
    return out


def _area_filter(q, db, area: Optional[str]):
    """area: None/'' = 호출자가 기본 구역으로 바꿔서 넘긴다 · 'all' = 전부 · 'real' = 시뮬레이션 제외 · 숫자."""
    if area in (None, "", "all"):
        return q
    if area == "real":
        sims = sim_area_ids(db)
        if sims:
            q = q.filter((DispatchJobLog.area_id.is_(None)) | (~DispatchJobLog.area_id.in_(sims)))
        return q
    return q.filter(DispatchJobLog.area_id == int(area))


def _route_label(r: Optional[str], j: Optional[str]) -> str:
    from app.services.poi_label import label_for
    return f"{label_for(r) or '?'} → {label_for(j) or '?'}"


def list_jobs(unit: str, d: date, continued_only: bool, area: Optional[str]) -> dict:
    _ensure_table()
    start = period_start(d, unit)
    end = period_next(start, unit)
    db = SessionLocal()
    try:
        q = (db.query(DispatchJobLog)
             .filter(DispatchJobLog.started_at >= datetime.combine(start, datetime.min.time()),
                     DispatchJobLog.started_at < datetime.combine(end, datetime.min.time())))
        q = _area_filter(q, db, area)
        rows = q.order_by(DispatchJobLog.started_at.desc(), DispatchJobLog.id.desc()).limit(3000).all()
        # 연속 작업의 직전 작업은 화면에서 묶어 보여 준다 — '연속만' 필터여도 직전 작업 id 는 알려 준다
        items = []
        for r in rows:
            if continued_only and not r.continued:
                continue
            fin = r.unloaded_at or r.ended_at
            items.append({
                "id": r.id, "area_id": r.area_id, "robot_id": r.robot_id,
                "robot_name": r.robot_name, "r_name": r.r_name, "j_name": r.j_name,
                "route_key": r.route_key, "route_label": _route_label(r.r_name, r.j_name),
                "call_at": r.call_at.isoformat() if r.call_at else None,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "unloaded_at": r.unloaded_at.isoformat() if r.unloaded_at else None,
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "duration_sec": (int((fin - r.started_at).total_seconds())
                                 if (fin and r.started_at) else None),
                "result": r.result, "note": r.note,
                "continued": bool(r.continued), "prev_job_id": r.prev_job_id,
                "source": r.source, "session_id": r.session_id,
            })
        return {"unit": unit, "from": start.isoformat(), "to": end.isoformat(),
                "label": period_label(start, unit), "count": len(items), "items": items}
    finally:
        db.close()


def _ratio(c: int, t: int) -> Optional[float]:
    return round(c * 100.0 / t, 1) if t else None


def summary(unit: str, d_from: date, d_to: date, area: Optional[str]) -> dict:
    """기간 칸별 완료 작업 수 · 연속 작업 수 · 비율. 완료(completed)만 센다 — 작업 1건 = 하차 1건."""
    _ensure_table()
    start = period_start(d_from, unit)
    end = period_next(period_start(d_to, unit), unit)
    db = SessionLocal()
    try:
        q = (db.query(DispatchJobLog)
             .filter(DispatchJobLog.started_at >= datetime.combine(start, datetime.min.time()),
                     DispatchJobLog.started_at < datetime.combine(end, datetime.min.time())))
        q = _area_filter(q, db, area)
        rows = q.all()
    finally:
        db.close()

    buckets: dict[date, dict] = {}
    p = start
    while p < end:
        buckets[p] = {"total": 0, "continued": 0, "other": 0, "routes": {}}
        p = period_next(p, unit)
    tot = {"total": 0, "continued": 0, "other": 0, "routes": {}}
    labels: dict[str, str] = {}

    for r in rows:
        b = buckets.get(period_start(r.started_at.date(), unit))
        if b is None:
            continue
        key = r.route_key or "?"
        labels.setdefault(key, _route_label(r.r_name, r.j_name))
        for acc in (b, tot):
            rt = acc["routes"].setdefault(key, {"total": 0, "continued": 0, "other": 0})
            if r.result == "completed":
                acc["total"] += 1
                rt["total"] += 1
                if r.continued:
                    acc["continued"] += 1
                    rt["continued"] += 1
            else:
                acc["other"] += 1      # 미완료(실패·중단·진행 중) — 화면 표에는 안 쓴다
                rt["other"] += 1

    def _routes(m: dict) -> list[dict]:
        return [{"route_key": k, "route_label": labels.get(k, k), "total": v["total"],
                 "continued": v["continued"], "ratio": _ratio(v["continued"], v["total"]),
                 "other": v["other"]}
                for k, v in sorted(m.items())]

    periods = [{"from": k.isoformat(), "label": period_label(k, unit),
                "total": v["total"], "continued": v["continued"],
                "ratio": _ratio(v["continued"], v["total"]), "other": v["other"],
                "routes": _routes(v["routes"])}
               for k, v in sorted(buckets.items(), reverse=True)]
    return {"unit": unit, "from": start.isoformat(), "to": end.isoformat(),
            "periods": periods,
            "total": {"total": tot["total"], "continued": tot["continued"],
                      "ratio": _ratio(tot["continued"], tot["total"]), "other": tot["other"],
                      "routes": _routes(tot["routes"])}}


def areas_with_jobs() -> list[dict]:
    """기록이 있는 구역 목록(화면의 구역 선택용)."""
    _ensure_table()
    from sqlalchemy import func as _f
    from app.models.map import Area
    db = SessionLocal()
    try:
        sims = sim_area_ids(db)
        rows = (db.query(DispatchJobLog.area_id, _f.count(DispatchJobLog.id))
                .group_by(DispatchJobLog.area_id).all())
        names = {int(a.area_id): a.name for a in db.query(Area).all()}
        return [{"area_id": a, "name": names.get(a) if a is not None else None,
                 "count": int(n), "sim": (a in sims)}
                for a, n in sorted(rows, key=lambda x: (x[0] is None, x[0] or 0))]
    finally:
        db.close()
