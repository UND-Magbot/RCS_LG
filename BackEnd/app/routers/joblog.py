"""배송 작업 기록 조회 API — 배차 콘솔 [로그] 화면용 (2026-11-06).

  GET /api/dispatch/joblog          기간 목록(최신순)
  GET /api/dispatch/joblog/summary  기간 칸별 전체·연속·비율 + 방향별
  GET /api/dispatch/joblog/areas    기록이 있는 구역 목록

area_id 인자:
  (생략) = 기본 구역(맵 관리의 '메인 적용' 구역)
  all    = 전 구역 (시뮬레이션 포함)
  real   = 전 구역 (시뮬레이션 구역 제외)
  숫자   = 그 구역만

읽기 전용 — 배차 동작에 영향 없음.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import job_log

router = APIRouter(prefix="/api/dispatch", tags=["배송 작업 기록"])


def _default_area() -> Optional[str]:
    from app.routers import map as map_mod
    a = map_mod._current_area_id
    if a is None:
        a = map_mod._load_persisted_default_area()
    return str(a) if a is not None else "all"


def _area(area_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(필터 값, 화면에 알려 줄 실제 구역)."""
    if area_id in (None, "", "default"):
        a = _default_area()
        return a, a
    if area_id in ("all", "real"):
        return area_id, area_id
    try:
        return str(int(area_id)), str(int(area_id))
    except ValueError:
        raise HTTPException(400, "area_id 는 숫자 / all / real 중 하나")


def _unit(unit: str) -> str:
    if unit not in job_log.UNITS:
        raise HTTPException(400, "unit 은 day / week / month / all 중 하나")
    return unit


def _date(s: Optional[str], default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"날짜 형식 오류: {s} (YYYY-MM-DD)")


@router.get("/joblog")
def api_joblog(unit: str = "day", d: Optional[str] = Query(None, alias="date"),
               continued_only: int = 0, area_id: Optional[str] = None):
    """그 기간(일·주·월)의 작업 목록. date 생략 = 오늘이 속한 기간."""
    a, shown = _area(area_id)
    out = job_log.list_jobs(_unit(unit), _date(d, date.today()), bool(continued_only), a)
    out["area_id"] = shown
    return out


@router.get("/joblog/summary")
def api_joblog_summary(unit: str = "day", area_id: Optional[str] = None,
                       d_from: Optional[str] = Query(None, alias="from"),
                       d_to: Optional[str] = Query(None, alias="to")):
    """기간 칸별 요약. from/to 생략 시 일=최근 14일 · 주=최근 8주 · 월=최근 6개월."""
    unit = _unit(unit)
    today = date.today()
    back = {"day": timedelta(days=13), "week": timedelta(weeks=7), "month": timedelta(days=31 * 5),
            "all": timedelta(0)}[unit]   # all 은 한 칸뿐이라 범위가 의미 없다
    t = _date(d_to, today)
    f = _date(d_from, t - back)
    if f > t:
        f, t = t, f
    a, shown = _area(area_id)
    out = job_log.summary(unit, f, t, a)
    out["area_id"] = shown
    return out


@router.get("/joblog/areas")
def api_joblog_areas():
    return {"default_area_id": _default_area(), "areas": job_log.areas_with_jobs()}
