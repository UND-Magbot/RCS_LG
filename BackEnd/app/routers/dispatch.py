"""인터랙티브 배차 (VESA 모드) 라우터

엔드포인트:
  POST   /api/dispatch/start              — 여러 로봇 동시 시작
  POST   /api/dispatch/{robot_id}/next    — 다음 POI 지시
  POST   /api/dispatch/{robot_id}/end     — 종료 (랙 반납 + 충전소 복귀)
  GET    /api/dispatch/status             — 전체 상태 + 점유 POI
  GET    /api/dispatch/tablet/{slot_number}  — 슬롯 기반 태블릿 페이지 (메인)
  GET    /api/dispatch/tablet/poi/{poi_id}   — POI 기반 태블릿 페이지 (호환)
  GET    /api/dispatch/tablet             — 태블릿 페이지 HTML (로봇 선택)
"""
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.dispatch import DispatchSession
from app.models.map import MapPOI, RobotMap
from app.models.robot import Robot
from app.schemas.dispatch import (
    DispatchStartRequest, DispatchNextRequest,
    DispatchSessionOut, DispatchStatusOut, POIBrief,
    DispatchPOIStatusOut, DispatchCallResult, DispatchCallRequest,
    TabletSlotIn, TabletSlotOut,
    POIConsoleItem, ConsoleRobotItem, ConsoleStatusOut,
    DispatchRouteRequest, WaypointBrief, RobotTabletStatus,
    JobPointIn, JobPointOut,
)
from app.crud import dispatch as dispatch_crud
from app.services import dispatch_service
# 화면에 보일 이름만 한글로 바꾼다. **조회 키로는 절대 쓰지 말 것** —
# job_points 매핑·진입점("<이름>-1")·로봇 overlay 가 전부 원래 이름에 묶여 있다.
from app.services.poi_label import label_for as _label, zone_for as _zone, zone_order as _zone_order
from app.services import notice_service
from app.models.dispatch import DispatchSession as DispatchSessionModel
from app.models.robot import Robot as RobotModel, RobotStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dispatch", tags=["인터랙티브 배차 (VESA)"])

_TABLET_TEMPLATE = Path(__file__).parent.parent / "templates" / "dispatch_tablet.html"
_CONSOLE_TEMPLATE = Path(__file__).parent.parent / "templates" / "dispatch_console.html"
_ROBOT_TABLET_TEMPLATE = Path(__file__).parent.parent / "templates" / "dispatch_robot_tablet.html"


# ── 헬퍼 ─────────────────────────────────────────────────────


def _session_to_out(session: DispatchSession, db: Session) -> DispatchSessionOut:
    robot = db.query(Robot).filter(Robot.id == session.robot_id).first()
    current = (
        db.query(MapPOI).filter(MapPOI.id == session.current_poi_id).first()
        if session.current_poi_id else None
    )
    target = (
        db.query(MapPOI).filter(MapPOI.id == session.target_poi_id).first()
        if session.target_poi_id else None
    )
    return DispatchSessionOut(
        id=session.id,
        robot_id=session.robot_id,
        robot_name=robot.name if robot else None,
        status=session.status,
        with_rack=bool(session.with_rack) if session.with_rack is not None else True,
        first_poi_id=session.first_poi_id,
        current_poi_id=session.current_poi_id,
        current_poi_name=current.name if current else None,
        target_poi_id=session.target_poi_id,
        target_poi_name=target.name if target else None,
        last_error=session.last_error,
        started_at=session.started_at,
        updated_at=session.updated_at,
        ended_at=session.ended_at,
    )


def _list_available_pois(db: Session, robot_id: Optional[int] = None) -> list[POIBrief]:
    """대상 로봇이 속한 area의 활성 맵에서 'jack' 타입 POI 목록.

    robot_id가 None이면 전체 활성 맵 POI 중 jack 타입을 모아서 반환.
    """
    if robot_id:
        robot = db.query(Robot).filter(Robot.id == robot_id).first()
        if not robot:
            return []
        if robot.area_id:
            active_map = (
                db.query(RobotMap)
                .filter(RobotMap.area_id == int(robot.area_id), RobotMap.is_active == True)
                .order_by(RobotMap.id.desc())
                .first()
            )
        else:
            active_map = (
                db.query(RobotMap)
                .filter(RobotMap.is_active == True)
                .order_by(RobotMap.id.desc())
                .first()
            )
        if not active_map:
            return []
        pois = (
            db.query(MapPOI)
            .filter(
                MapPOI.map_id == active_map.id,
                MapPOI.is_active == True,
                MapPOI.poi_type == "jack",
            )
            .order_by(MapPOI.name.asc())
            .all()
        )
    else:
        # 전체 (status 페이지용)
        pois = (
            db.query(MapPOI)
            .filter(MapPOI.is_active == True, MapPOI.poi_type == "jack")
            .order_by(MapPOI.name.asc())
            .all()
        )
    # name 은 **표시용**이다 — 호출부(태블릿 경유지 선택)는 id 로 보내므로 안전하다.
    return [
        POIBrief(id=p.id, name=_label(p.name), poi_type=p.poi_type,
                 world_x=p.world_x, world_y=p.world_y)
        for p in pois
    ]


def _current_area_active_pois(db: Session, include_standby: bool = False) -> list[POIBrief]:
    """현재 [메인 적용]된 area의 활성 맵에서 jack POI 목록.

    맵관리에서 적용한 area_id 기준. 다른 층/다른 맵의 POI는 제외한다.
    (콘솔/슬롯 모두 이 목록을 써서 현재 맵 POI만 노출)

    include_standby=True 면 랙 보관 위치(R)도 함께 반환한다. 2026-08-24 현장 배치
    확정으로 **호출 버튼이 R 에 놓이게** 되어 콘솔/태블릿이 R 타일을 그려야 하기
    때문. 단 아무 standby 나 노출하면 혼란스러우므로 **job_points 매핑에 등장하는
    R 만** 남긴다(충전 대기용 등 배차와 무관한 standby 는 제외).
    """
    from app.routers import map as map_mod
    area_id = map_mod._current_area_id
    if area_id is None:
        # 파일에서 한번 더 시도 (서버 부팅 직후 등)
        area_id = map_mod._load_persisted_default_area()
    if area_id is None:
        return []

    active_map = (
        db.query(RobotMap)
        .filter(RobotMap.area_id == area_id, RobotMap.is_active == True)
        .order_by(RobotMap.updated_at.desc())
        .first()
    )
    if not active_map:
        return []

    types = ["jack", "standby"] if include_standby else ["jack"]
    pois = (
        db.query(MapPOI)
        .filter(
            MapPOI.map_id == active_map.id,
            MapPOI.is_active == True,
            MapPOI.poi_type.in_(types),
        )
        .order_by(MapPOI.name.asc())
        .all()
    )
    if include_standby:
        jps = _job_points_for_current_area(db, area_id)
        if not jps:
            # 매핑이 하나도 없으면 배송 모드가 아니다 → 종전 콘솔 그대로(jack 만).
            # R 을 띄워봐야 호출하면 "연결된 작업지점이 없습니다" 로 막힌다.
            pois = [p for p in pois if p.poi_type != "standby"]
        else:
            # 배송 모드 — **매핑된 것만** 남긴다.
            # 매핑 없는 jack POI(J3·J4 등)를 그대로 두면 legacy 타일로 그려져
            # J 에 [로봇 호출]/[예약하기]/[경유지 등록] 버튼이 살아난다. 호출은 R 에서만
            # 해야 하므로 콘솔에서 아예 뺀다. (쓰려면 매핑을 등록하면 된다)
            r_names = {jp.r_poi_name for jp in jps}
            j_names = {jp.j_poi_name for jp in jps}
            pois = [
                p for p in pois
                if (p.name in r_names if (p.poi_type or "") == "standby" else p.name in j_names)
            ]
    return [
        POIBrief(id=p.id, name=p.name, poi_type=p.poi_type, world_x=p.world_x, world_y=p.world_y)
        for p in pois
    ]


def _job_points_for_current_area(db: Session, area_id) -> list:
    """현재 area 의 J↔R 매핑 목록. area 로 못 찾으면 전체로 폴백.

    crud.get_job_point 이 area_id 로 먼저 찾고 없으면 이름만으로 폴백하는 것과 같은
    규칙. (매핑을 area 없이 등록했거나 area_id 타입이 어긋난 경우에도 R 타일이
    사라지지 않도록)
    """
    rows = dispatch_crud.list_job_points(db, area_id)
    return rows if rows else dispatch_crud.list_job_points(db, None)


# ── API ──────────────────────────────────────────────────────


@router.post("/start")
def start(body: DispatchStartRequest, db: Session = Depends(get_db)):
    """여러 로봇에 첫 작업 POI를 지정해서 동시 시작."""
    if not body.robots:
        raise HTTPException(status_code=400, detail="시작할 로봇이 없습니다")

    # 사전 검증: POI 중복 / 이미 진행 중인 로봇
    poi_ids: set[int] = set()
    for item in body.robots:
        if item.first_poi_id in poi_ids:
            raise HTTPException(status_code=400, detail="여러 로봇이 같은 첫 POI를 가질 수 없습니다")
        poi_ids.add(item.first_poi_id)
        if dispatch_service.has_active_worker(item.robot_id):
            raise HTTPException(
                status_code=409,
                detail=f"로봇 {item.robot_id} 이미 진행 중인 배차가 있습니다",
            )

    # 다른 활성 세션이 점유 중인 POI와도 충돌 검사
    occupied = dispatch_crud.occupied_poi_ids(db)
    conflict = poi_ids & occupied
    if conflict:
        raise HTTPException(status_code=409, detail=f"점유 중인 POI 충돌: {sorted(conflict)}")

    results = []
    for item in body.robots:
        ok, msg = dispatch_service.start_session(item.robot_id, item.first_poi_id)
        results.append({"robot_id": item.robot_id, "ok": ok, "message": msg})
    return {"results": results}


@router.post("/{robot_id:int}/next")
def next_position(robot_id: int, body: DispatchNextRequest, db: Session = Depends(get_db)):
    # 다른 세션이 점유 중이면 거부
    occupied = dispatch_crud.occupied_poi_ids(db)
    # 자기 세션의 current는 제외 (해제 처리는 워커가 함)
    own = dispatch_crud.get_active_session(db, robot_id)
    if own:
        occupied.discard(own.current_poi_id or 0)
        occupied.discard(own.target_poi_id or 0)
    if body.next_poi_id in occupied:
        raise HTTPException(status_code=409, detail="다른 로봇이 점유 중인 POI 입니다")

    ok, msg = dispatch_service.send_next(robot_id, body.next_poi_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True}


@router.post("/{robot_id:int}/end")
def end(robot_id: int):
    ok, msg = dispatch_service.send_end(robot_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True}


@router.get("/sessions/today", response_model=list[DispatchSessionOut])
def sessions_today(db: Session = Depends(get_db)):
    """오늘 시작된 모든 dispatch 세션 (활성 + 종료된 것 포함).

    관제 대시보드의 KPI / 최근 이벤트 집계용.
    """
    from datetime import datetime, date as _date
    today_start = datetime.combine(_date.today(), datetime.min.time())
    rows = (
        db.query(DispatchSessionModel)
        .filter(DispatchSessionModel.started_at >= today_start)
        .order_by(DispatchSessionModel.id.desc())
        .all()
    )
    return [_session_to_out(r, db) for r in rows]


@router.get("/status", response_model=DispatchStatusOut)
def status(db: Session = Depends(get_db)):
    sessions = dispatch_crud.list_active_sessions(db)
    occupied = sorted(dispatch_crud.occupied_poi_ids(db))
    return DispatchStatusOut(
        sessions=[_session_to_out(s, db) for s in sessions],
        occupied_poi_ids=occupied,
        available_pois=_list_available_pois(db, robot_id=None),
    )


@router.get("/{robot_id:int}/status", response_model=DispatchSessionOut)
def robot_status(robot_id: int, db: Session = Depends(get_db)):
    session = dispatch_crud.get_active_session(db, robot_id)
    if not session:
        raise HTTPException(status_code=404, detail="활성 세션 없음")
    return _session_to_out(session, db)


@router.get("/{robot_id:int}/pois", response_model=list[POIBrief])
def robot_pois(robot_id: int, db: Session = Depends(get_db)):
    """해당 로봇이 갈 수 있는 POI 목록 (자기 area 기준)."""
    return _list_available_pois(db, robot_id=robot_id)


# ── 태블릿 페이지 ────────────────────────────────────────────


# 레거시 로봇 기준 태블릿 라우터(/tablet/{robot_id})는 슬롯 라우터(/tablet/{n})와
# 경로 충돌하므로 제거됨. 호환이 필요한 경우 /tablet/poi/{poi_id}를 사용.


# ══════════════════════════════════════════════════════════
# POI 기준 API (위치별 태블릿 — VESA 신 운영 방식)
# ══════════════════════════════════════════════════════════


def _battery_ok(robot: Robot, stat) -> bool:
    """배차 가능한 배터리인지 — find_available_robot() 과 동일 판정.

    배터리 값이 없으면(아직 한 번도 수집 안 됨) 통과시킨다.
    find_available_robot 도 값이 None 이면 검사를 건너뛰므로 기준을 맞춘 것.
    """
    level = stat.battery_level if (stat and stat.battery_level is not None) else None
    if level is None:
        return True
    min_batt = robot.min_battery if robot.min_battery is not None else 20
    return level >= min_batt


def _count_available_robots(db: Session) -> int:
    """실제 호출 가능한 로봇 수.

    조건:
      - is_active=True, ip_address 있음
      - 활성 워커 없음 (=다른 호출에 배정 안 됨)
      - **배터리 >= min_battery** (2026-08-19 추가)
      - **현재 온라인** (관제와 동일한 라이브 체크 — fetch_all_robots_live)

    ⚠ 이 조건은 dispatch_service.find_available_robot() 과 반드시 같아야 한다.
      한쪽만 바꾸면 "화면엔 가용 1대인데 호출하면 안 온다" 가 된다.
      (area_id / robot_type 필터는 여기 없음 — 아래 _available_robot_counts 는 타입별로 나눠 집계)
    """
    rows = (db.query(Robot, RobotStatus)
              .outerjoin(RobotStatus, RobotStatus.robot_id == Robot.id)
              .filter(Robot.is_active == True, Robot.ip_address != None)
              .all())
    # 1) 인메모리 필터 (활성 워커 제외 + 배터리)
    idle_robots = [r for r, st in rows
                   if not dispatch_service.has_active_worker(r.id)
                   and _battery_ok(r, st)]
    if not idle_robots:
        return 0
    # 2) 라이브 ONLINE 체크 (TTL 캐시 — 배차 선정 로직과 동일)
    #
    # ★ 2026-09-14 — **기다리지 않는다**(block=False).
    #   이 값은 POI 태블릿의 "가용 로봇 N대" 표시와 [호출]/[예약하기] 라벨에만 쓴다.
    #   태블릿은 2초마다 폴링하는데, 오프라인 로봇이 한 대라도 등록돼 있으면
    #   캐시 만료(LIVE_CACHE_TTL_OFFLINE=60초)마다 조회가 타임아웃을 끝까지
    #   기다려 폴링이 수십 초 멈췄다. 그동안 버튼 상태가 얼어 작업자가 여러 번 누른다.
    #   콘솔(console/status)은 2026-08-24 에 이미 block=False 로 뺐는데
    #   **이 경로만 빠져 있었다.**
    #
    #   필터 조건(is_active / 워커 없음 / 배터리)은 그대로다 — 위 docstring 의
    #   "find_available_robot() 과 같아야 한다" 는 조건에 대한 것이고, 여기서
    #   바뀌는 건 온라인 판정의 **최신성**뿐이다. 실제 배차는 여전히 block=True
    #   로 정확히 판정한다(dispatch_service.find_available_robot).
    #   캐시가 뒤처져도 결과는 무해하다 — 화면이 '1대' 인데 실제 0대면 호출이
    #   예약으로 잡히고(기존 정상 경로), '0대' 인데 실제 1대면 예약 후 자동 호출된다.
    try:
        online_ips = dispatch_service.online_ips_cached(
            [r.ip_address for r in idle_robots], block=False)
        return sum(1 for r in idle_robots if r.ip_address in online_ips)
    except Exception:
        # 라이브 서비스 자체가 죽었으면 폴백 — 등록된 idle 수 그대로
        return len(idle_robots)


def _poi_area_id(db: Session, poi: MapPOI) -> Optional[int]:
    """POI 가 속한 영역 id. 매핑(job_points)을 영역 단위로 찾기 위해 필요."""
    if not poi or not poi.map_id:
        return None
    m = db.query(RobotMap).filter(RobotMap.id == poi.map_id).first()
    return int(m.area_id) if (m and m.area_id is not None) else None


def _poi_state_only(db: Session, poi_id: int) -> str:
    """그 POI 의 state 만 계산 — empty / reserved / calling / arrived.

    R 태블릿이 짝 J 의 상황(예약됨/오는 중/도착)을 보여주기 위한 경량 조회.
    _poi_status 를 그대로 재귀 호출하면 사용 가능 POI 목록·가용 로봇 수까지
    매번 다시 계산해서 폴링 비용이 커지므로 필요한 부분만 뽑았다.
    """
    from sqlalchemy import and_, or_
    arrived = (
        db.query(DispatchSessionModel)
        .filter(
            DispatchSessionModel.current_poi_id == poi_id,
            or_(
                DispatchSessionModel.status == "awaiting_next",
                DispatchSessionModel.status == "awaiting_confirm",
                and_(
                    DispatchSessionModel.status == "moving",
                    DispatchSessionModel.target_poi_id.is_(None),
                ),
            ),
        )
        .first()
    )
    if arrived:
        return "arrived"
    heading = (
        db.query(DispatchSessionModel)
        .filter(
            DispatchSessionModel.target_poi_id == poi_id,
            DispatchSessionModel.status.in_(("starting", "picking_up", "moving")),
        )
        .first()
    )
    if heading:
        return "calling"
    if dispatch_crud.get_waiting_reservation(db, poi_id):
        return "reserved"
    return "empty"


def _safety_alerts() -> list[dict]:
    """전방 장애물로 멈춰 있는 로봇 목록. 조회 실패로 태블릿이 죽으면 안 되므로 삼킨다."""
    try:
        from app.services import safety_zone
        return safety_zone.active_alerts()
    except Exception:
        return []


def _estop_pressed(robot_ip: Optional[str]) -> bool:
    """비상정지가 눌려 있는가 (요청 6번). **캐시 조회일 뿐 로봇을 직접 치지 않는다.**

    태블릿이 3초마다 폴링하는 경로라 여기서 로봇에 REST 를 걸면 LTE 지연이
    그대로 화면 멈춤이 된다. 실제 조회는 estop_monitor 백그라운드 스레드가 한다.
    '모름(None)'은 False 로 내린다 — 통신 장애를 비상정지로 보여주면 거짓 경보다.
    """
    if not robot_ip:
        return False
    try:
        from app.services import estop_monitor
        return estop_monitor.is_pressed(robot_ip) is True
    except Exception:
        return False


def _poi_status(db: Session, poi_id: int) -> DispatchPOIStatusOut:
    poi = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
    poi_name = poi.name if poi else f"POI #{poi_id}"

    # 1) 이 위치에 "도착해 있는" 세션
    #    - awaiting_next  : 다음 명령 대기
    #    - moving + target=NULL : 도착 직후 잭다운 처리 중 (set_current가 target을 NULL로 비움)
    from sqlalchemy import and_, or_
    arrived = (
        db.query(DispatchSessionModel)
        .filter(
            DispatchSessionModel.current_poi_id == poi_id,
            or_(
                DispatchSessionModel.status == "awaiting_next",
                DispatchSessionModel.status == "awaiting_confirm",
                and_(
                    DispatchSessionModel.status == "moving",
                    DispatchSessionModel.target_poi_id.is_(None),
                ),
            ),
        )
        .order_by(DispatchSessionModel.id.desc())
        .first()
    )
    # 2) 이 위치로 오는 중인 세션 (다음 POI로 이동 중인 prev POI는 자동 제외 — target 매치 안 함)
    heading = (
        db.query(DispatchSessionModel)
        .filter(
            DispatchSessionModel.target_poi_id == poi_id,
            DispatchSessionModel.status.in_(("starting", "picking_up", "moving")),
        )
        .order_by(DispatchSessionModel.id.desc())
        .first()
    )

    session = arrived or heading
    state = "empty"
    if arrived:
        state = "arrived"
    elif heading:
        state = "calling"

    # 예약 상태 (가용 로봇 없어 대기 중) — 로봇이 없을 때만 의미 있음
    reservation = dispatch_crud.get_waiting_reservation(db, poi_id)
    reserved = reservation is not None
    reserved_with_rack = bool(reservation.with_rack) if reservation else None
    if state == "empty" and reserved:
        state = "reserved"

    robot_id = robot_name = robot_ip = battery = None
    target_id = target_name = None
    session_status = None
    with_rack_val: Optional[bool] = None
    if session:
        session_status = session.status
        with_rack_val = bool(session.with_rack) if session.with_rack is not None else True
        target_id = session.target_poi_id
        if target_id:
            tp = db.query(MapPOI).filter(MapPOI.id == target_id).first()
            target_name = tp.name if tp else None
        robot = db.query(Robot).filter(Robot.id == session.robot_id).first()
        if robot:
            robot_id = robot.id
            robot_name = robot.name
            robot_ip = robot.ip_address
            st = db.query(RobotStatus).filter(RobotStatus.robot_id == robot.id).first()
            if st:
                battery = st.battery_level

    # 랙 점유 + 짝 POI (2026-08-24 현장 배치)
    #   J 태블릿 → 자기 자신의 점유. True 면 [확인 — 랙을 치웠습니다] 만 보여준다.
    #   R 태블릿 → 짝 J 의 점유. True 면 호출 버튼을 그리지 않는다.
    # 두 태블릿이 같은 값을 보고 서로 반대되는 UI 를 그린다.
    rack_present = False
    rack_at = None
    poi_type = (poi.poi_type or "jack") if poi else "jack"
    paired_id = paired_name = paired_state = None
    if poi:
        _area = _poi_area_id(db, poi)
        jp = dispatch_crud.get_job_point(db, _area, poi.name)
        if jp:
            paired_name = jp.r_poi_name          # 나는 J — 짝은 R
        else:
            jp = dispatch_crud.get_job_point_by_r(db, _area, poi.name)
            if jp:
                paired_name = jp.j_poi_name      # 나는 R — 짝은 J
        if jp:
            rack_present = bool(jp.occupied)
            rack_at = jp.occupied_at
            prow = (
                db.query(MapPOI)
                .filter(
                    MapPOI.map_id == poi.map_id,
                    MapPOI.name == paired_name,
                    MapPOI.is_active == True,
                )
                .first()
            )
            if prow:
                paired_id = prow.id
                if poi_type == "standby":
                    # R 태블릿은 짝 J 가 예약/이동 중인지 보여줘야 한다
                    paired_state = _poi_state_only(db, prow.id)

    # 사용 가능한 다음 위치 (점유 안 된 POI). POI 자기 자신은 제외
    available = _list_available_pois(db, robot_id=robot_id)
    occupied = sorted(dispatch_crud.occupied_poi_ids(db))
    available = [p for p in available if p.id != poi_id]

    # ★ 여기서부터가 '표시 계층' 이다. 위쪽 조회(paired_name 으로 MapPOI 검색,
    #   job_point 매핑 조회)는 전부 **원래 이름** 으로 끝났고, 이제 화면에 내보낼
    #   문자열만 한글로 바꾼다. 순서를 뒤집으면 매핑이 통째로 끊긴다.
    return DispatchPOIStatusOut(
        poi_id=poi_id,
        poi_name=_label(poi_name),
        state=state,
        reserved=reserved,
        reserved_with_rack=reserved_with_rack,
        robot_id=robot_id,
        robot_name=robot_name,
        robot_ip=robot_ip,
        robot_battery=battery,
        with_rack=with_rack_val,
        session_status=session_status,
        target_poi_id=target_id,
        target_poi_name=_label(target_name),
        available_pois=available,
        occupied_poi_ids=occupied,
        available_robot_count=_count_available_robots(db),
        rack_present=rack_present,
        poi_type=poi_type,
        paired_poi_id=paired_id,
        paired_poi_name=_label(paired_name),
        paired_state=paired_state,
        rack_occupied_at=rack_at,
        safety_alerts=_safety_alerts(),
        notices=notice_service.list_for(notice_service.poi_target(poi_id)),
    )


@router.get("/poi/{poi_id}/status", response_model=DispatchPOIStatusOut)
def poi_status(poi_id: int, db: Session = Depends(get_db)):
    return _poi_status(db, poi_id)


@router.post("/poi/{poi_id}/call", response_model=DispatchCallResult)
def poi_call(poi_id: int, body: DispatchCallRequest | None = None, db: Session = Depends(get_db)):
    """이 POI로 가용 로봇 1대 호출 (배터리 많은 순).

    가용 로봇이 없으면 예약을 생성한다 (reserved=True). 이후 어떤 로봇이
    작업을 종료해 가용해지면 먼저 예약한 위치부터 자동으로 호출된다.

    body.with_rack (기본 True):
      True  → standby에서 렉 픽업 후 이 POI로 이동
      False → 잭 조작 없이 바로 이 POI로 이동
    """
    with_rack = body.with_rack if body is not None else True
    robot_type = body.robot_type if body is not None else None

    # 2026-08-24 현장 배치 — 호출 버튼은 랙이 눈앞에 있는 R(랙 보관 위치)에 있다.
    # (J 옆 작업자는 100m 떨어진 R 에 랙이 있는지 볼 수 없기 때문)
    # 실제 배차 목적지는 매핑된 작업지점(J) 이므로 여기서 바꿔 끼운다.
    # J POI 로 들어온 호출은 종전 경로 그대로 — 아래 블록을 그냥 지나간다.
    _req_row = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
    if _req_row is not None and (_req_row.poi_type or "") == "standby":
        jp_r = dispatch_crud.get_job_point_by_r(db, _poi_area_id(db, _req_row), _req_row.name)
        if not jp_r:
            raise HTTPException(
                status_code=404,
                detail=f"{_req_row.name} 에 연결된 작업지점이 없습니다",
            )
        j_row = (
            db.query(MapPOI)
            .filter(
                MapPOI.map_id == _req_row.map_id,
                MapPOI.name == jp_r.j_poi_name,
                MapPOI.is_active == True,
            )
            .first()
        )
        if not j_row:
            raise HTTPException(
                status_code=404,
                detail=f"작업지점 {jp_r.j_poi_name} 을 현재 맵에서 찾을 수 없습니다",
            )
        poi_id = j_row.id

    # 2026-08-24 시나리오 — 작업지점에 이미 랙이 놓여 있으면 배차하지 않는다.
    # 로봇이 랙을 든 채 가봐야 자리를 못 잡고 조용히 재시도만 반복하기 때문.
    # 작업자가 랙을 치우고 [확인](rack-cleared)을 눌러야 다시 호출된다.
    poi_row = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
    if poi_row:
        jp = dispatch_crud.get_job_point(db, _poi_area_id(db, poi_row), poi_row.name)
        if jp and jp.occupied:
            raise HTTPException(
                status_code=409,
                detail="작업지역에 랙이 있어요. 치운 후 확인 버튼을 눌러주세요.",
            )

    ok, msg, robot_id, reserved = dispatch_service.call_to_poi(
        poi_id, robot_type=robot_type, with_rack=with_rack
    )
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    robot_name = None
    if robot_id:
        r = db.query(Robot).filter(Robot.id == robot_id).first()
        robot_name = r.name if r else None
    return DispatchCallResult(
        ok=True,
        message="reserved" if reserved else "ok",
        robot_id=robot_id,
        robot_name=robot_name,
        reserved=reserved,
    )


@router.delete("/poi/{poi_id}/reserve")
def poi_cancel_reservation(poi_id: int):
    """이 위치의 대기 중 예약을 취소."""
    cancelled = dispatch_service.cancel_reservation_at_poi(poi_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="취소할 예약이 없습니다")
    return {"ok": True}


# ══════════════════════════════════════════════════════════
# 콘솔 (범용 단말 — 모든 POI를 한 화면에서 호출/예약/제어)
# ══════════════════════════════════════════════════════════


def _available_robot_counts(db: Session) -> dict:
    """가용 로봇 수를 타입별로 집계 (라이브 ONLINE 체크 1회).

    ⚠ 조건은 dispatch_service.find_available_robot() 과 같게 유지할 것 (배터리 포함).
    """
    rows = (db.query(Robot, RobotStatus)
              .outerjoin(RobotStatus, RobotStatus.robot_id == Robot.id)
              .filter(Robot.is_active == True, Robot.ip_address != None)
              .all())
    idle = [r for r, st in rows
            if not dispatch_service.has_active_worker(r.id) and _battery_ok(r, st)]
    if not idle:
        return {"total": 0, "lifting": 0, "serving": 0}
    # 라이브 ONLINE 체크 — 화면 폴링이므로 **기다리지 않는다**(block=False).
    # 오프라인 로봇이 등록돼 있으면 실조회가 20초 넘게 걸려 콘솔이 통째로 멈춘다.
    online_ips = dispatch_service.online_ips_cached(
        [r.ip_address for r in idle], block=False
    )
    online = [r for r in idle if r.ip_address in online_ips]
    counts = {
        "total": len(online),
        "lifting": sum(1 for r in online if (r.robot_type or "lifting") == "lifting"),
        "serving": sum(1 for r in online if r.robot_type == "serving"),
    }

    # 대기 중인 예약은 **이미 임자가 있는 로봇**이다 — 가용에서 뺀다.
    # 안 빼면, 로봇이 유휴가 된 순간부터 예약이 실제로 배차될 때까지의 공백 동안
    # "가용 1대" 로 보여 다른 R 타일에 [로봇 호출] 이 떠 버린다(실측으로 확인).
    waiting = len(dispatch_crud.list_waiting_reservations(db))
    if waiting:
        counts["total"] = max(0, counts["total"] - waiting)
        counts["lifting"] = min(counts["lifting"], counts["total"])
        counts["serving"] = min(counts["serving"], counts["total"])
    return counts


def _console_status(db: Session) -> ConsoleStatusOut:
    """모든 작업 POI(jack)의 상태를 한 번에 집계. (콘솔 폴링용)

    POI별 상태 계산은 _poi_status 와 동일 규칙이되, 세션/로봇/예약을 일괄
    조회해 N+1 쿼리를 피한다.
    """
    # 작업지점(J) + 매핑된 랙 보관 위치(R). R 타일에 호출 버튼이 붙는다.
    pois = _current_area_active_pois(db, include_standby=True)

    sessions = (
        db.query(DispatchSessionModel)
        .filter(DispatchSessionModel.status.notin_(("completed", "failed")))
        .all()
    )
    arrived_by_poi: dict[int, DispatchSessionModel] = {}
    heading_by_poi: dict[int, DispatchSessionModel] = {}
    for s in sessions:
        if s.current_poi_id and (
            s.status == "awaiting_next"
            or s.status == "awaiting_confirm"
            or (s.status == "moving" and s.target_poi_id is None)
        ):
            arrived_by_poi[s.current_poi_id] = s
        if s.target_poi_id and s.status in ("starting", "picking_up", "moving"):
            heading_by_poi[s.target_poi_id] = s

    reservations = {r.poi_id: r for r in dispatch_crud.list_waiting_reservations(db)}
    occupied = sorted(dispatch_crud.occupied_poi_ids(db))
    poi_name_by_id = {p.id: p.name for p in pois}

    # 2026-08-24 현장 배치 — J↔R 짝과 랙 점유 상태를 한 번에 읽어 타일에 실어준다.
    # R 타일과 J 타일 모두 **같은 J 의 occupied** 를 본다:
    #   R 타일 → True 면 호출 버튼을 그리지 않음(짝 J 에 아직 랙이 있음)
    #   J 타일 → True 면 [확인 — 랙을 치웠습니다] 만 표시
    from app.routers import map as map_mod
    _job_points = _job_points_for_current_area(db, map_mod._current_area_id)
    jp_by_j = {jp.j_poi_name: jp for jp in _job_points}
    jp_by_r = {jp.r_poi_name: jp for jp in _job_points}
    poi_id_by_name = {p.name: p.id for p in pois}

    # "경유지 예약" 배지용 집합 — 미래 목적지(이동 목표 target + 아직 안 간 경유지)만.
    # 로봇이 떠나는 중인 current_poi_id 는 제외 → 출발지가 잘못 "예약"으로 뜨는 것 방지.
    from app.models.dispatch import DispatchWaypoint as _DWp
    reserved_ids: set[int] = set()
    for s in sessions:
        if s.target_poi_id:
            reserved_ids.add(s.target_poi_id)
    _sids = [s.id for s in sessions]
    if _sids:
        for (pid,) in (
            db.query(_DWp.poi_id)
            .filter(_DWp.session_id.in_(_sids), _DWp.status.in_(("pending", "current")))
            .all()
        ):
            if pid:
                reserved_ids.add(pid)

    robot_ids = {s.robot_id for s in sessions if s.robot_id}
    robots: dict[int, Robot] = {}
    statuses: dict[int, RobotStatus] = {}
    if robot_ids:
        robots = {r.id: r for r in db.query(Robot).filter(Robot.id.in_(robot_ids)).all()}
        statuses = {
            st.robot_id: st
            for st in db.query(RobotStatus).filter(RobotStatus.robot_id.in_(robot_ids)).all()
        }

    items: list[POIConsoleItem] = []
    for p in pois:
        arrived = arrived_by_poi.get(p.id)
        heading = heading_by_poi.get(p.id)
        session = arrived or heading

        state = "empty"
        if arrived:
            state = "arrived"
        elif heading:
            state = "calling"

        reservation = reservations.get(p.id)
        reserved = reservation is not None
        reserved_with_rack = bool(reservation.with_rack) if reservation else None
        if state == "empty" and reserved:
            state = "reserved"

        robot_id = robot_name = robot_ip = battery = None
        with_rack_val = session_status = target_id = target_name = None
        can_confirm = False
        next_poi_name = None
        is_last = False
        if session:
            session_status = session.status
            with_rack_val = bool(session.with_rack) if session.with_rack is not None else True
            target_id = session.target_poi_id
            # poi_name_by_id 는 **원래 이름** 사전이다(매핑 조회에도 쓰인다).
            # 화면에 나가는 값만 여기서 한글로 바꾼다.
            target_name = _label(poi_name_by_id.get(target_id)) if target_id else None
            r = robots.get(session.robot_id)
            if r:
                robot_id = r.id
                robot_name = r.name
                robot_ip = r.ip_address
                st = statuses.get(r.id)
                if st:
                    battery = st.battery_level
            # 경유지 진행 중이면 콘솔에서도 [확인] 가능 — 다음 경유지/마지막 여부 계산
            if session.status == "awaiting_confirm":
                can_confirm = True
                wps = dispatch_crud.list_waypoints(db, session.id)
                cur_seq = next((w.seq for w in wps if w.status == "current"), None)
                nxt = next(
                    (w for w in wps if w.status == "pending"
                     and (cur_seq is None or w.seq > cur_seq)),
                    None,
                )
                if nxt:
                    next_poi_name = poi_name_by_id.get(nxt.poi_id)
                    # 콘솔 POI 목록에 없는(다른 층) 이름 폴백
                    if next_poi_name is None and nxt.poi_id:
                        _np = db.query(MapPOI).filter(MapPOI.id == nxt.poi_id).first()
                        next_poi_name = _np.name if _np else None
                    next_poi_name = _label(next_poi_name)
                else:
                    is_last = True

        # 짝 POI / 랙 점유 (매핑이 없으면 전부 기본값 — 기존 인터랙티브 모드와 동일)
        jp = jp_by_j.get(p.name) or jp_by_r.get(p.name)
        paired_name = None
        role = None
        if jp:
            # 이 POI 가 매핑의 J 쪽인지 R 쪽인지 — 이름으로 확정된다.
            # POI 종류값(poi_type)이 잘못 저장돼 있어도 여기는 흔들리지 않는다.
            is_job = (jp.j_poi_name == p.name)
            role = "job" if is_job else "rack"
            paired_name = jp.r_poi_name if is_job else jp.j_poi_name

        # paired_name 으로 id 를 찾는 것까지는 **원래 이름** 으로 해야 한다.
        # 화면에 실어 보내는 문자열만 아래에서 한글로 바꾼다.
        paired_id = poi_id_by_name.get(paired_name) if paired_name else None

        items.append(POIConsoleItem(
            poi_id=p.id, poi_name=_label(p.name), state=state,
            poi_type=p.poi_type or "jack",
            role=role,
            zone=_zone(p.name),
            paired_poi_id=paired_id,
            paired_poi_name=_label(paired_name),
            rack_occupied=bool(jp.occupied) if jp else False,
            rack_occupied_at=jp.occupied_at if jp else None,
            robot_id=robot_id, robot_name=robot_name, robot_ip=robot_ip,
            robot_battery=battery, with_rack=with_rack_val,
            session_status=session_status, target_poi_id=target_id,
            target_poi_name=target_name,
            reserved=reserved, reserved_with_rack=reserved_with_rack,
            can_confirm=can_confirm, next_poi_name=next_poi_name, is_last=is_last,
        ))

    counts = _available_robot_counts(db)
    robot_items = _console_robot_items(db, sessions, poi_name_by_id)
    # 구역 순서는 설정 파일 순서를 따르되, 지금 화면에 실제로 있는 구역만 남긴다.
    # (설정에만 있고 POI 가 없는 구역이 빈 칸으로 남으면 화면이 반쪽이 된다)
    present = {it.zone for it in items if it.zone}
    zones = [z for z in _zone_order() if z in present]
    return ConsoleStatusOut(
        pois=items,
        robots=robot_items,
        occupied_poi_ids=occupied,
        reserved_poi_ids=sorted(reserved_ids),
        available_robot_count=counts["total"],
        available_lifting_count=counts["lifting"],
        available_serving_count=counts["serving"],
        zones=zones,
        notices=notice_service.list_for(notice_service.TARGET_CONSOLE),
    )


def _console_robot_items(db: Session, sessions: list, poi_name_by_id: dict) -> list[ConsoleRobotItem]:
    """콘솔 사이드바용 로봇 목록 — 등록된 활성 로봇 전체 + 상태/배터리/원격제어 IP.

    online 판정은 배차 선정과 동일한 라이브 TTL 캐시를 재사용한다.
    """
    robots = (
        db.query(Robot)
        .filter(Robot.is_active == True)
        .order_by(Robot.name.asc())
        .all()
    )
    if not robots:
        return []

    ips = [r.ip_address for r in robots if r.ip_address]
    try:
        # 화면 폴링 — 기다리지 않는다 (위 _available_robot_counts 와 같은 이유)
        online_ips = dispatch_service.online_ips_cached(ips, block=False)
    except Exception:
        online_ips = set(ips)  # 라이브 서비스 장애 시 온라인 취급

    # 배터리 일괄 조회
    ids = [r.id for r in robots]
    statuses = {
        st.robot_id: st
        for st in db.query(RobotStatus).filter(RobotStatus.robot_id.in_(ids)).all()
    } if ids else {}
    # 로봇별 현재 세션 (진행 상태/위치 요약)
    session_by_robot = {s.robot_id: s for s in sessions if s.robot_id}

    out: list[ConsoleRobotItem] = []
    for r in robots:
        st = statuses.get(r.id)
        sess = session_by_robot.get(r.id)
        cur_name = None
        if sess:
            cur = sess.current_poi_id or sess.target_poi_id
            cur_name = _label(poi_name_by_id.get(cur)) if cur else None
        out.append(ConsoleRobotItem(
            robot_id=r.id,
            robot_name=r.name,
            robot_ip=r.ip_address,
            robot_type=r.robot_type or "lifting",
            online=(r.ip_address in online_ips) if r.ip_address else False,
            battery=st.battery_level if st else None,
            busy=dispatch_service.has_active_worker(r.id),
            session_status=sess.status if sess else None,
            current_poi_name=cur_name,
        ))
    return out


# ══════════════════════════════════════════════════════════
#  화면(HTML)은 캐시하지 않는다 (2026-09-16)
#
#  왜 — 응답에 Cache-Control · ETag · Last-Modified 가 **하나도 없었다.**
#    그러면 Android WebView(태블릿 APK)가 휴리스틱 캐싱으로 옛 화면을 계속
#    띄울 수 있다. 화면을 고쳐도 태블릿에 언제 반영될지 알 수 없었다.
#
#    2026-09-14 에 R 타일과 J 타일을 나누기 전 화면은 **R 타일에도 [확인]
#    버튼이 그려졌다.** 그 화면이 캐시에 남아 있으면 "R 에 확인 버튼이 뜬다"
#    는 현장 보고가 그대로 재현된다.
#
#  ※ 헤더만 붙인다. 화면 내용과 동작은 바뀌지 않는다.
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@router.get("/console/status", response_model=ConsoleStatusOut)
def console_status(db: Session = Depends(get_db)):
    """콘솔 폴링 — 모든 POI 상태 + 점유 목록 + 가용 로봇 수."""
    return _console_status(db)


@router.get("/console", response_class=HTMLResponse)
def console_page():
    """범용 콘솔 페이지 (모든 POI를 한 화면에서 호출/예약/다음/종료)."""
    if not _CONSOLE_TEMPLATE.exists():
        return HTMLResponse(content="<h1>console template not found</h1>", status_code=500)
    return HTMLResponse(content=_CONSOLE_TEMPLATE.read_text(encoding="utf-8"),
                        headers=_NO_CACHE)


# ══════════════════════════════════════════════════════════
# 경유지 경로 + 로봇 부착 태블릿
# ══════════════════════════════════════════════════════════


@router.post("/poi/{poi_id:int}/route")
def poi_route(poi_id: int, body: DispatchRouteRequest, db: Session = Depends(get_db)):
    """이 위치에 도착한 로봇에 경유지 경로(순서)를 등록하고 출발시킨다.

    이후 각 경유지 도착마다 로봇 부착 태블릿의 [확인]으로 다음 진행, 마지막 확인 후 자동 종료.
    """
    session = dispatch_service.get_session_at_poi(poi_id)
    if not session:
        raise HTTPException(status_code=404, detail="이 위치에 대기 중인 로봇이 없습니다")
    if not body.waypoints:
        raise HTTPException(status_code=400, detail="경유지를 1개 이상 지정하세요")

    # 입력 정리: 출발 위치 자신 제외 + 중복 제거(순서 유지)
    # (같은 곳을 연속 방문하거나 제자리 이동하는 비정상 동작 방지)
    seen: set[int] = set()
    waypoints: list[int] = []
    for w in body.waypoints:
        if w == poi_id or w in seen:
            continue
        seen.add(w)
        waypoints.append(w)
    if not waypoints:
        raise HTTPException(
            status_code=400,
            detail="유효한 경유지가 없습니다 (출발 위치이거나 중복만 지정됨)",
        )

    # 점유 검증: 경유지가 다른 로봇 점유 POI면 거부
    occupied = dispatch_crud.occupied_poi_ids(db)
    occupied.discard(poi_id)  # 자기 현재 위치
    for wp in dispatch_crud.list_waypoints(db, session.id):
        if wp.poi_id:
            occupied.discard(wp.poi_id)  # 자기 세션의 기존 경유지(새 경로로 덮어씀)
    conflict = sorted({w for w in waypoints if w in occupied})
    if conflict:
        raise HTTPException(status_code=409, detail=f"점유 중인 경유지가 있습니다: {conflict}")

    ok, msg = dispatch_service.send_route(session.robot_id, waypoints)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "robot_id": session.robot_id}


def _robot_tablet_status(db: Session, robot_id: int) -> RobotTabletStatus:
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    robot_name = robot.name if robot else None
    # 비상정지 표시(요청 6번). 배차가 없을 때도 눌려 있으면 알려야 하므로
    # **세션 유무와 무관하게** 먼저 읽는다.
    estop = _estop_pressed(robot.ip_address if robot else None)

    session = dispatch_crud.get_active_session(db, robot_id)
    if not session:
        return RobotTabletStatus(robot_id=robot_id, robot_name=robot_name,
                                 active=False, estop=estop)

    wps = dispatch_crud.list_waypoints(db, session.id)
    poi_ids = {w.poi_id for w in wps if w.poi_id}
    if session.current_poi_id:
        poi_ids.add(session.current_poi_id)
    poi_names: dict[int, str] = {}
    if poi_ids:
        for p in db.query(MapPOI).filter(MapPOI.id.in_(poi_ids)).all():
            # 로봇 태블릿은 순수 표시 화면이다 — 여기 이름은 조회에 쓰이지 않는다
            poi_names[p.id] = _label(p.name)

    current_poi_id = session.current_poi_id
    current_poi_name = poi_names.get(current_poi_id) if current_poi_id else None

    # 현재 진행 중(current) 경유지의 다음 pending = [확인] 시 갈 곳
    cur_seq = next((w.seq for w in wps if w.status == "current"), None)
    next_wp = next(
        (w for w in wps if w.status == "pending" and (cur_seq is None or w.seq > cur_seq)),
        None,
    )
    next_poi_id = next_wp.poi_id if next_wp else None
    next_poi_name = poi_names.get(next_poi_id) if next_poi_id else None

    can_confirm = (session.status == "awaiting_confirm")
    is_last = can_confirm and next_wp is None

    return RobotTabletStatus(
        robot_id=robot_id,
        robot_name=robot_name,
        active=True,
        session_status=session.status,
        current_poi_id=current_poi_id,
        current_poi_name=current_poi_name,
        next_poi_id=next_poi_id,
        next_poi_name=next_poi_name,
        is_last=is_last,
        can_confirm=can_confirm,
        estop=estop,
        waypoints=[
            WaypointBrief(seq=w.seq, poi_id=w.poi_id,
                          poi_name=poi_names.get(w.poi_id), status=w.status)
            for w in wps
        ],
    )


@router.get("/robot/{robot_id:int}/tablet-status", response_model=RobotTabletStatus)
def robot_tablet_status(robot_id: int, db: Session = Depends(get_db)):
    """로봇 부착 태블릿 폴링 — 현재/다음 경유지 + 확인 가능 여부."""
    return _robot_tablet_status(db, robot_id)


@router.post("/robot/{robot_id:int}/confirm")
def robot_confirm(robot_id: int):
    """로봇 부착 태블릿 [확인] — 다음 경유지로 진행 (마지막이면 종료로)."""
    ok, msg = dispatch_service.send_confirm(robot_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True}


@router.post("/robot/{robot_id:int}/end")
def robot_end(robot_id: int):
    """로봇 부착 태블릿 [작업 종료] — 즉시 종료 시퀀스(복귀+충전소)."""
    ok, msg = dispatch_service.send_end(robot_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True}


@router.get("/robot-tablet/{robot_id:int}", response_class=HTMLResponse)
def robot_tablet_page(robot_id: int, db: Session = Depends(get_db)):
    """로봇 부착 태블릿 페이지 (현재/다음 경유지 + [확인]/[종료])."""
    if not _ROBOT_TABLET_TEMPLATE.exists():
        return HTMLResponse(content="<h1>robot tablet template not found</h1>", status_code=500)
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    html = _ROBOT_TABLET_TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("{{ROBOT_ID}}", str(robot_id))
    html = html.replace("{{ROBOT_NAME}}", (robot.name if robot else f"로봇 #{robot_id}"))
    return HTMLResponse(content=html, headers=_NO_CACHE)


@router.post("/poi/{poi_id}/next")
def poi_send_next(poi_id: int, body: DispatchNextRequest, db: Session = Depends(get_db)):
    """이 위치에 도착한 로봇을 비어있는 다음 POI로 보냄."""
    session = dispatch_service.get_session_at_poi(poi_id)
    if not session:
        raise HTTPException(status_code=404, detail="이 위치에 대기 중인 로봇이 없습니다")

    # 점유 검증
    occupied = dispatch_crud.occupied_poi_ids(db)
    occupied.discard(poi_id)  # 자기 위치 (출발)
    if body.next_poi_id in occupied:
        raise HTTPException(status_code=409, detail="다른 로봇이 점유 중인 위치입니다")

    ok, msg = dispatch_service.send_next(session.robot_id, body.next_poi_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "robot_id": session.robot_id}


@router.post("/poi/{poi_id}/end")
def poi_send_end(poi_id: int):
    """이 위치에 있는 로봇을 종료 시퀀스로 (standby → 잭다운 → 충전소).

    "여기서 그만" 은 어떤 대기 상태에서든 가능해야 하므로
    awaiting_next 뿐 아니라 경유지 순회 중(awaiting_confirm / moving+target NULL)에도
    받아준다. send_end() 가 end_flag 와 두 이벤트를 모두 세워
    어떤 wait() 에 걸려 있든 깨우므로 안전하다.
    (이전에는 get_session_at_poi 가 awaiting_next 만 보아, 확인 대기 구간 내내
     화면에는 [종료] 가 보이는데 누르면 404 가 났다.)
    """
    session = dispatch_service.get_arrived_session_at_poi(poi_id)
    if not session:
        raise HTTPException(status_code=404, detail="이 위치에 대기 중인 로봇이 없습니다")
    ok, msg = dispatch_service.send_end(session.robot_id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "robot_id": session.robot_id}


@router.get("/tablet/poi/{poi_id}", response_class=HTMLResponse)
def tablet_poi_page(poi_id: int, db: Session = Depends(get_db)):
    """[호환] 위치(POI ID)별 태블릿 페이지."""
    poi = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
    # 제목은 표시용이므로 한글 이름이 있으면 그걸 쓴다(요청 4번)
    poi_name = _label(poi.name) if poi else f"POI #{poi_id}"
    return _render_tablet_html(slot_number=0, poi_id=poi_id, poi_name=poi_name)


# ══════════════════════════════════════════════════════════
# 슬롯 (1, 2, 3 …) — 메인 운영 방식
# ══════════════════════════════════════════════════════════


def _render_tablet_html(slot_number: int, poi_id: int, poi_name: str,
                         alias: str = "") -> HTMLResponse:
    if not _TABLET_TEMPLATE.exists():
        return HTMLResponse(content="<h1>tablet template not found</h1>", status_code=500)
    html = _TABLET_TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("{{SLOT_NUMBER}}", str(slot_number))
    html = html.replace("{{POI_ID}}", str(poi_id))
    html = html.replace("{{POI_NAME}}", poi_name or "")
    html = html.replace("{{SLOT_ALIAS}}", alias or "")
    html = html.replace("{{ROBOT_ID}}", "0")
    html = html.replace("{{ROBOT_NAME}}", "")
    html = html.replace("{{MODE}}", "poi" if slot_number == 0 else "slot")
    return HTMLResponse(content=html, headers=_NO_CACHE)


@router.get("/tablet/{slot_number}", response_class=HTMLResponse)
def tablet_slot_page(slot_number: int, db: Session = Depends(get_db)):
    """슬롯 번호로 접속. 매핑 없으면 페이지 내에서 설정 UI 표시."""
    slot = dispatch_crud.get_slot(db, slot_number)
    if not slot:
        # 매핑 없음 — 설정 모드 (POI_ID=0)
        return _render_tablet_html(slot_number=slot_number, poi_id=0,
                                    poi_name=f"슬롯 {slot_number}")
    poi = db.query(MapPOI).filter(MapPOI.id == slot.poi_id).first()
    # 슬롯에 별칭을 직접 적어뒀으면 그것이 우선, 없으면 poi_labels.json 의 한글 이름
    poi_name = (slot.alias or (_label(poi.name) if poi else f"POI #{slot.poi_id}"))
    return _render_tablet_html(slot_number=slot_number,
                                poi_id=slot.poi_id,
                                poi_name=poi_name,
                                alias=slot.alias or "")


@router.get("/slots", response_model=list[TabletSlotOut])
def list_slots(db: Session = Depends(get_db)):
    rows = dispatch_crud.list_slots(db)
    out = []
    for s in rows:
        poi = db.query(MapPOI).filter(MapPOI.id == s.poi_id).first()
        out.append(TabletSlotOut(
            slot_number=s.slot_number, poi_id=s.poi_id,
            poi_name=(poi.name if poi else None), alias=s.alias,
        ))
    return out


@router.get("/slots/available-pois", response_model=list[POIBrief])
def slots_available_pois(db: Session = Depends(get_db)):
    """슬롯에 매핑 가능한 POI 목록 (작업 위치 = jack 타입).

    맵관리에서 [메인 적용]된 area_id의 활성 맵의 POI만 반환.
    맵 적용을 바꾸면 자동으로 여기 결과도 바뀜.

    주의: /slots/{slot_number}보다 먼저 정의돼야 한다 (FastAPI 라우팅 순서).
    """
    return _current_area_active_pois(db)


@router.get("/slots/{slot_number}", response_model=TabletSlotOut)
def get_slot(slot_number: int, db: Session = Depends(get_db)):
    s = dispatch_crud.get_slot(db, slot_number)
    if not s:
        raise HTTPException(status_code=404, detail="슬롯 매핑 없음")
    poi = db.query(MapPOI).filter(MapPOI.id == s.poi_id).first()
    return TabletSlotOut(
        slot_number=s.slot_number, poi_id=s.poi_id,
        poi_name=(poi.name if poi else None), alias=s.alias,
    )


@router.put("/slots/{slot_number}", response_model=TabletSlotOut)
def set_slot(slot_number: int, body: TabletSlotIn, db: Session = Depends(get_db)):
    # POI 존재 검증
    poi = db.query(MapPOI).filter(MapPOI.id == body.poi_id, MapPOI.is_active == True).first()
    if not poi:
        raise HTTPException(status_code=400, detail="유효하지 않은 POI")
    s = dispatch_crud.upsert_slot(db, slot_number, body.poi_id, body.alias)
    return TabletSlotOut(
        slot_number=s.slot_number, poi_id=s.poi_id,
        poi_name=poi.name, alias=s.alias,
    )


@router.delete("/slots/{slot_number}")
def remove_slot(slot_number: int, db: Session = Depends(get_db)):
    ok = dispatch_crud.delete_slot(db, slot_number)
    if not ok:
        raise HTTPException(status_code=404, detail="슬롯 매핑 없음")
    return {"ok": True}


# ── 작업지점(J) ↔ 랙 보관(R) 매핑 (2026-08-24 시나리오) ─────────
#
# POI id 가 아니라 이름으로 저장한다. 맵 재동기화로 POI 가 재생성돼도
# 매핑이 끊기지 않게 하기 위함. 자세한 내용은 models/dispatch.py 참조.


@router.get("/job-points", response_model=list[JobPointOut])
def list_job_points(area_id: Optional[int] = None, db: Session = Depends(get_db)):
    """작업지점 매핑 목록 (+ 현재 랙 점유 상태)."""
    return dispatch_crud.list_job_points(db, area_id)


@router.post("/job-points", response_model=JobPointOut)
def upsert_job_point(body: JobPointIn, db: Session = Depends(get_db)):
    """매핑 등록/수정. 예) J1 → R1"""
    if body.j_poi_name == body.r_poi_name:
        raise HTTPException(status_code=400, detail="작업지점과 랙 보관 위치가 같습니다")
    return dispatch_crud.upsert_job_point(db, body.area_id, body.j_poi_name, body.r_poi_name)


@router.delete("/job-points/{j_poi_name}")
def delete_job_point(j_poi_name: str, area_id: Optional[int] = None,
                     db: Session = Depends(get_db)):
    if not dispatch_crud.delete_job_point(db, area_id, j_poi_name):
        raise HTTPException(status_code=404, detail="매핑이 없습니다")
    return {"ok": True}


@router.post("/poi/{poi_id:int}/rack-cleared")
def poi_rack_cleared(poi_id: int, db: Session = Depends(get_db)):
    """작업자가 랙을 치우고 태블릿에서 [확인]을 눌렀을 때 — 점유 해제.

    작업지점(J) id 로 부르는 것이 정상 경로지만, **랙 보관 위치(R) id 로 불러도**
    짝 J 의 점유를 푼다. 호출 버튼이 R 에 있어서 R 화면의 안전망 모달이 R id 로
    이 API 를 부르기 때문. 이 치환이 없으면 조회가 빗나가 '매핑 없음'으로 조용히
    성공 응답하고 점유가 그대로 남는다.
    """
    poi = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
    if not poi:
        raise HTTPException(status_code=404, detail="POI를 찾을 수 없습니다")

    area_id = _poi_area_id(db, poi)
    j_name = poi.name
    if (poi.poi_type or "") == "standby":
        jp_r = dispatch_crud.get_job_point_by_r(db, area_id, poi.name)
        if jp_r:
            j_name = jp_r.j_poi_name

    row = dispatch_crud.set_job_point_occupied(db, area_id, j_name, False)
    if not row:
        # 매핑이 없는 POI — 점유를 추적하지 않으므로 성공으로 응답한다
        return {"ok": True, "message": "매핑되지 않은 위치입니다 (점유 미추적)"}
    logger.info(f"[dispatch] 랙 치움 확인 — poi={j_name} 점유 해제 (요청: {poi.name})")
    return {"ok": True, "j_poi_name": j_name}


# ══════════════════════════════════════════════════════════
# 관제 알림 (도킹 실패 · 강제 종료 안내)  — services/notice_service.py
#   대상(target) 규약:  "console"  또는  "poi:<poi_id>"
#   POI 태블릿은 /poi/{id}/status 응답의 notices 로도 같은 값을 받는다.
# ══════════════════════════════════════════════════════════


@router.get("/notices")
def list_notices(target: str = "console"):
    """그 화면에 지금 떠 있어야 하는 알림 목록."""
    return notice_service.list_for(target)


@router.post("/notices/{notice_id:int}/ack")
def ack_notice(notice_id: int):
    """[확인] — `ack_required` 알림만 내려간다.

    도킹 실패처럼 '조건이 풀려야 사라지는' 알림은 여기서 지워도 다음 회차에 다시 뜬다.
    그래서 404 대신 ok=False 로 알려주고 화면은 조용히 넘어가게 한다.
    """
    return {"ok": notice_service.ack(notice_id)}


# ══════════════════════════════════════════════════════════
# 전역 강제 종료 (LGIT 요청 3번)
#   로봇별 원격제어 패널의 [작업 강제 종료] 와 달리 **콘솔 상단에서 한 번에** 끊는다.
#   2026-09-17 LGIT 요청 — 로봇은 **그 자리에 랙을 내려놓은 뒤** 다음 자리로 간다.
#     · 현재 JOB 강제 종료 : 랙 놓기 → 대기 호출이 있으면 그 R 지점, 없으면 충전소
#     · 전체 강제 종료     : 랙 놓기 → 충전소
#   놓인 랙은 작업자가 직접 치운다(현장 합의). 로봇별 패널의 [작업 강제 종료]는
#   종전대로 '그 자리 정지' 이다 — force_clear(after="hold").
# ══════════════════════════════════════════════════════════


@router.post("/force-clear/current")
def force_clear_current():
    """진행 중인 작업만 중지. **대기 중인 호출(예약)은 그대로 둔다.**"""
    n = dispatch_service.force_clear_current()
    notice_service.push(
        notice_service.KIND_JOB_FORCE_CLEAR,
        "⚠ 현재 작업이 중지되었습니다.\n"
        "로봇은 멈춘 자리에 랙을 내려놓고, 대기 호출이 있으면 그 R 지점으로 "
        "없으면 충전소로 이동합니다.\n"
        "놓인 랙은 작업자가 정리해 주세요.",
        targets=[notice_service.TARGET_CONSOLE],
        ack_required=True,
        key="force_clear",
    )
    return {"ok": True, "cleared": n}


@router.post("/robot/{robot_id}/force-clear/{scope}")
def force_clear_one_robot(robot_id: int, scope: str, db: Session = Depends(get_db)):
    """**로봇 한 대만** 강제 종료 (2026-09-17 콘솔 UI 개편).

    콘솔 상단의 전역 버튼 2개를 없애고 로봇별 원격제어 패널로 옮기면서 만든 것이다.
    전역 버튼은 작업 중인 로봇을 전부 돌아서, 여러 대가 섞여 돌면 어느 로봇이
    멈추는지 알 수 없었다.

    scope
      current — 이 로봇의 작업만 중지. 랙은 멈춘 자리에, 호출(예약)은 유지.
      all     — 위와 같되 충전소로 가고 **이 로봇이 잡았던 호출까지** 취소.
    """
    if scope not in ("current", "all"):
        raise HTTPException(status_code=400, detail="scope 는 current 또는 all 이어야 합니다")
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    if not robot:
        raise HTTPException(status_code=404, detail="등록되지 않은 로봇입니다")

    # 2026-09-21 — 대차를 **안 들고 있으면** "대차를 내리고" 를 뺀다(현장 요청).
    #   ★ `force_clear_robot()` **앞에서** 읽는다. 그 뒤에는 후속 동작 스레드가
    #     이미 제자리 잭다운을 시작해 "없음" 으로 보일 수 있다.
    #   ★ 모르면(통신 실패) **들고 있다고 본다** — 후속 동작이 쓰는 기준과 같다
    #     ("안 들고 있는데 잭다운은 해가 없지만, 들고 있는데 건너뛰면 랙을 든 채
    #       충전소로 가버린다").
    from app.services import jack_service as _js
    try:
        had_rack = _js.is_rack_loaded(robot.ip_address) is not False
    except Exception:
        had_rack = True

    result = dispatch_service.force_clear_robot(robot_id, scope)
    # 2026-09-19 — Robot 모델의 컬럼 이름은 `name` 이다. `robot_name` 은 없다.
    #   이 줄이 AttributeError 를 내서 현장에서 "강제 종료 실패 / Internal Server
    #   Error" 가 떴다. 로봇을 세우는 건 바로 위 force_clear_robot() 이 이미 끝낸
    #   뒤라 **동작은 정상이고 응답만 500** 이었다(랙 내리고 충전소 복귀는 됨).
    name = robot.name or f"robot {robot_id}"

    # 알림은 **그 로봇 이름을 박아서** 띄운다 — 어느 로봇을 세웠는지가 핵심이다.
    if scope == "current":
        # 2026-09-19 — 문구 정리(현장 요청). "(다른 로봇은 그대로 진행합니다)" 삭제,
        #   용어를 '랙' → '대차' 로 통일. 이 알림은 목적지가 정해지면
        #   dispatch_service._push_followup_notice 가 같은 key 로 덮어쓴다.
        msg = "\n".join([f"⚠ [{name}] 현재 작업을 중지했습니다."]
                        + (["대차를 내리고 이동합니다.", "내려놓은 대차를 치워 주세요."]
                           if had_rack else ["곧 이동합니다."]))
        kind = notice_service.KIND_JOB_FORCE_CLEAR
    else:
        # 2026-09-21 — 취소된 호출이 **0건이면 그 줄을 뺀다**(현장 요청).
        #   종전에는 "호출 0건도 취소되었습니다" 가 그대로 떴다.
        cancelled = int(result.get("cancelled", 0) or 0)
        msg = "\n".join(
            [f"⚠ [{name}] 작업을 전부 중지했습니다."]
            + ([f"이 로봇이 잡고 있던 호출 {cancelled}건도 취소되었습니다."]
               if cancelled else [])
            + (["대차를 내리고 충전소로 이동합니다.", "내려놓은 대차를 치워 주세요."]
               if had_rack else ["충전소로 이동합니다."]))
        kind = notice_service.KIND_JOB_FORCE_CLEAR_ALL

    notice_service.push(
        kind, msg,
        # 2026-09-19 — 콘솔뿐 아니라 **모든 작업지점 태블릿**에도 띄운다(LGIT 16번).
        targets=dispatch_service.force_clear_notice_targets(),
        ack_required=True,
        # ★ key 를 로봇별로 나눈다. 전역 "force_clear" 로 두면 로봇 A 를 세운 알림이
        #   로봇 B 를 세울 때 덮어써져, 먼저 세운 걸 못 보고 지나간다.
        key=f"force_clear_r{robot_id}",
    )
    return {"ok": True, "robot_id": robot_id, "robot_name": name, **result}


@router.post("/force-clear/all")
def force_clear_all():
    """진행 중 작업 + 대기 중인 호출(예약)까지 전부 취소."""
    result = dispatch_service.force_clear_all()
    notice_service.push(
        notice_service.KIND_JOB_FORCE_CLEAR_ALL,
        "⚠ 전체 작업이 중지되었습니다.\n"
        "대기 중이던 호출도 모두 취소되었습니다.\n"
        "로봇은 멈춘 자리에 랙을 내려놓고 충전소로 복귀합니다.\n"
        "놓인 랙은 작업자가 정리해 주세요.",
        targets=[notice_service.TARGET_CONSOLE],
        ack_required=True,
        key="force_clear",
    )
    return {"ok": True, **result}
