import logging

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from app.robot_api.robot_live_service import fetch_all_robots_live

from app.database import get_db
from app.models.robot import Robot
from app.models.map import RobotMap, MapPOI
from app.schemas.robot import (
    RobotCreate,
    RobotUpdate,
    RobotResponse,
    RobotListResponse,
    RobotStatusUpdate,
    RobotStatusResponse,
    MinBatteryUpdate,
)
from app.crud.robot import (
    create_robot,
    get_robot,
    get_robots,
    update_robot,
    delete_robot,
    update_robot_status,
    get_robot_status,
    get_min_battery_by_sn,
    update_min_battery_by_sn,
)

from app.crud.activity_log import log_activity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/robots", tags=["로봇 관리"])


# ── 로봇 시크릿 (AutoXing 기본값) ──
DEFAULT_SECRET = "19a11878aaab420fba94577ce3620dce"


def _get_robot_list(db: Session) -> list[dict]:
    """DB에서 활성 로봇 IP 목록 → fetch_all_robots_live용 리스트"""
    robots = db.query(Robot).filter(Robot.is_active == True, Robot.ip_address != None).all()
    return [{"ip": r.ip_address, "secret": DEFAULT_SECRET} for r in robots if r.ip_address]

@router.get("/quick-status/{robot_ip}")
def api_get_robot_quick_status(robot_ip: str):
    """단일 로봇 상태 빠른 조회 (태블릿용)"""
    import requests as req
    result = {"online": False, "run_state": "OFFLINE", "battery": "-"}
    try:
        r = req.get(f"http://{robot_ip}:8090/chassis/status", timeout=8)
        if r.status_code == 200:
            result["online"] = True
            data = r.json()
            mode = data.get("control_mode", "auto")
            if data.get("emergency_stop_pressed"):
                result["run_state"] = "ESTOP"
            elif mode == "remote":
                result["run_state"] = "REMOTE"
            else:
                try:
                    mr = req.get(f"http://{robot_ip}:8090/chassis/moves/current", timeout=8)
                    if mr.status_code == 200 and mr.json().get("state") == "moving":
                        result["run_state"] = "MOVING"
                    else:
                        result["run_state"] = "IDLE"
                except Exception:
                    result["run_state"] = "IDLE"
    except Exception:
        return result
    # 배터리 (WebSocket 1회 조회)
    ws = None
    try:
        import websocket as _ws, json as _json, time as _time
        ws = _ws.create_connection(f"ws://{robot_ip}:8090/ws/v2/topics", timeout=8)
        ws.send(_json.dumps({"enable_topic": "/battery_state"}))
        deadline = _time.time() + 2
        while _time.time() < deadline:
            raw = ws.recv()
            pkt = _json.loads(raw)
            if pkt.get("topic") == "/battery_state":
                pct = pkt.get("percentage") or pkt.get("level") or pkt.get("power_percent")
                if pct is not None:
                    pct = float(pct)
                    if pct <= 1.0:
                        pct = pct * 100
                    result["battery"] = f"{int(pct)}%"
                break
    except Exception:
        pass
    finally:
        if ws:
            try: ws.close()
            except Exception: pass
    return result


# ── /live 응답 5초 TTL 캐시 ──
import time as _time_mod
import threading as _threading_mod
_live_cache: dict = {"data": None, "ts": 0.0}
_live_cache_lock = _threading_mod.Lock()
_LIVE_CACHE_TTL = 5.0


@router.get("/live")
def api_get_robots_live(db: Session = Depends(get_db)):
    """DB 로봇 목록을 기반으로 실시간 API 정보를 병합하여 반환.
    5초 TTL 캐시 적용 — 여러 클라이언트가 동시에 폴링해도 로봇에 중복 호출 안 함.
    """
    # 캐시 체크
    with _live_cache_lock:
        cached = _live_cache["data"]
        age = _time_mod.time() - _live_cache["ts"]
        if cached is not None and age < _LIVE_CACHE_TTL:
            return cached

    # ── 1차: DB 로봇 목록 가져오기 ──
    db_robots = db.query(Robot).filter(Robot.is_active == True).all()

    # DB 로봇 → 기본 아이템 생성 (SN 기준 매핑)
    sn_to_item: dict[str, dict] = {}
    ip_to_sn: dict[str, str] = {}
    ip_to_robot_id: dict[str, int] = {}

    items = []
    for r in db_robots:
        item = {
            "ID": r.id,
            "IP": r.ip_address or "",
            "SN": r.serial_number,
            "ROBOTNAME": r.name,
            "MODEL": r.model or "-",
            "NICKNAME": None,
            "AXBOT_VERSION": None,
            "PLATFORM": None,
            "RUNSTATE": "OFFLINE",
            "ONLINE": "Offline",
            "SIGNAL": "N/A",
            "POWER(%)": "-",
            "ROBOT_TYPE": getattr(r, "robot_type", "lifting") or "lifting",
        }
        items.append(item)
        sn_to_item[r.serial_number] = item
        if r.ip_address:
            ip_to_sn[r.ip_address] = r.serial_number
            ip_to_robot_id[r.ip_address] = r.id

    # ── 2차: 실시간 API로 상태 오버레이 ──
    live = fetch_all_robots_live(_get_robot_list(db))
    for live_item in live.get("items", []):
        live_ip = live_item.get("IP", "")
        live_sn = live_item.get("SN", "")

        # IP로 DB 로봇 매칭
        matched_sn = ip_to_sn.get(live_ip)
        # SN으로도 매칭 시도
        if not matched_sn and live_sn and live_sn != "N/A":
            matched_sn = live_sn if live_sn in sn_to_item else None

        if matched_sn and matched_sn in sn_to_item:
            # DB에 있는 로봇 → 실시간 정보 오버레이
            target = sn_to_item[matched_sn]
            if live_item.get("ROBOTNAME") and live_item["ROBOTNAME"] != "N/A":
                target["ROBOTNAME"] = live_item["ROBOTNAME"]
            if live_item.get("MODEL") and live_item["MODEL"] != "N/A":
                target["MODEL"] = live_item["MODEL"]
            target["NICKNAME"] = live_item.get("NICKNAME")
            target["AXBOT_VERSION"] = live_item.get("AXBOT_VERSION")
            target["PLATFORM"] = live_item.get("PLATFORM")
            target["RUNSTATE"] = live_item.get("RUNSTATE", "OFFLINE")
            target["ONLINE"] = live_item.get("ONLINE", "Offline")
            target["SIGNAL"] = live_item.get("SIGNAL", "N/A")
            target["POWER(%)"] = live_item.get("POWER(%)", "-")
            if not target["IP"] and live_ip:
                target["IP"] = live_ip

        else:
            # DB에 없는 새 로봇 → 리스트에 추가
            if live_sn and live_sn != "N/A":
                new_item = {
                    "ID": None,
                    "IP": live_ip,
                    "SN": live_sn,
                    "ROBOTNAME": live_item.get("ROBOTNAME", live_sn),
                    "MODEL": live_item.get("MODEL", "-"),
                    "NICKNAME": live_item.get("NICKNAME"),
                    "AXBOT_VERSION": live_item.get("AXBOT_VERSION"),
                    "PLATFORM": live_item.get("PLATFORM"),
                    "RUNSTATE": live_item.get("RUNSTATE", "OFFLINE"),
                    "ONLINE": live_item.get("ONLINE", "Offline"),
                    "SIGNAL": live_item.get("SIGNAL", "N/A"),
                    "POWER(%)": live_item.get("POWER(%)", "-"),
                }
                items.append(new_item)
                sn_to_item[live_sn] = new_item

    items.sort(key=lambda x: str(x.get("IP", "")))
    result = {"total": len(items), "items": items}
    # 캐시 업데이트
    with _live_cache_lock:
        _live_cache["data"] = result
        _live_cache["ts"] = _time_mod.time()
    return result


@router.post("/sync-live")
def api_sync_live_robots(db: Session = Depends(get_db)):
    """라이브 로봇 정보를 DB robots 테이블에 동기화 (upsert by serial_number)"""
    live = fetch_all_robots_live(_get_robot_list(db))
    items = live.get("items", [])

    created = 0
    updated = 0
    skipped = 0
    synced = []

    for item in items:
        sn = item.get("SN", "")
        if not sn or sn == "N/A":
            skipped += 1
            continue

        ip = item.get("IP", "")
        name = item.get("ROBOTNAME", "") or sn
        model = item.get("MODEL", "")

        existing = db.query(Robot).filter(Robot.serial_number == sn).first()

        if existing:
            if name and name != "N/A":
                existing.name = name
            if model and model != "N/A":
                existing.model = model
            if ip:
                existing.ip_address = ip
            existing.is_active = True
            updated += 1
            synced.append({"sn": sn, "name": name, "action": "updated"})
        else:
            new_robot = Robot(
                name=name if name and name != "N/A" else sn,
                serial_number=sn,
                model=model if model and model != "N/A" else None,
                ip_address=ip or None,
            )
            db.add(new_robot)
            created += 1
            synced.append({"sn": sn, "name": name, "action": "created"})

    db.commit()
    logger.info(f"sync-live: created={created}, updated={updated}, skipped={skipped}")
    log_activity("robot", "robot_sync",
                 f"로봇 동기화 완료: 생성 {created}, 갱신 {updated}, 건너뜀 {skipped}",
                 source="api_sync_live_robots")

    return {
        "message": f"동기화 완료: 생성 {created}, 갱신 {updated}, 건너뜀 {skipped}",
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "synced": synced,
    }


@router.post("/register-by-ip", status_code=201)
def api_register_by_ip(ip: str = Query(...), db: Session = Depends(get_db)):
    """IP 입력으로 로봇 자동 등록 — 로봇 API에서 SN/이름/모델을 가져와 DB에 저장"""
    from app.robot_api.robot_live_service import fetch_robot_live

    # 이미 등록된 IP인지 확인
    existing = db.query(Robot).filter(Robot.ip_address == ip, Robot.is_active == True).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"이미 등록된 로봇입니다 (IP: {ip}, 이름: {existing.name})")

    # 로봇에서 정보 가져오기
    live = fetch_robot_live(ip, DEFAULT_SECRET)
    if live.get("ONLINE") != "Online":
        raise HTTPException(status_code=502, detail=f"로봇에 연결할 수 없습니다 (IP: {ip})")

    sn = live.get("SN", "")
    if not sn or sn == "N/A":
        raise HTTPException(status_code=502, detail=f"로봇 SN을 가져올 수 없습니다 (IP: {ip})")

    # SN 중복 확인
    existing_sn = db.query(Robot).filter(Robot.serial_number == sn, Robot.is_active == True).first()
    if existing_sn:
        raise HTTPException(status_code=409, detail=f"이미 등록된 SN입니다 ({sn})")

    name = live.get("ROBOTNAME", "") or sn
    model = live.get("MODEL", "")

    new_robot = Robot(
        name=name if name != "N/A" else sn,
        serial_number=sn,
        model=model if model and model != "N/A" else None,
        ip_address=ip,
    )
    db.add(new_robot)
    db.commit()
    db.refresh(new_robot)

    log_activity("robot", "robot_create",
                 f"로봇 등록 (IP 자동): {new_robot.name} (SN: {sn}, IP: {ip})",
                 source="api_register_by_ip")

    return {
        "id": new_robot.id,
        "name": new_robot.name,
        "serial_number": sn,
        "model": new_robot.model,
        "ip_address": ip,
        "message": f"로봇 등록 완료: {new_robot.name}",
    }


@router.post("", response_model=RobotResponse, status_code=201)
def api_create_robot(data: RobotCreate, db: Session = Depends(get_db)):
    """RB-01 로봇 등록"""
    result = create_robot(db, data)
    log_activity("robot", "robot_create",
                 f"로봇 등록: {data.name} (SN: {data.serial_number})",
                 source="api_create_robot")
    return result


@router.get("", response_model=RobotListResponse)
def api_get_robots(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    business_id: str | None = Query(None),
    area_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    """로봇 목록 조회"""
    items = get_robots(db, skip=skip, limit=limit, business_id=business_id, area_id=area_id)
    return RobotListResponse(total=len(items), items=items)


# ── 최소 배터리 (SN 기반) ──

@router.get("/sn/{sn}/min-battery")
def api_get_min_battery(sn: str, db: Session = Depends(get_db)):
    """SN 기반 최소 배터리 조회"""
    return get_min_battery_by_sn(db, sn)


@router.patch("/sn/{sn}/min-battery")
def api_update_min_battery(sn: str, data: MinBatteryUpdate, db: Session = Depends(get_db)):
    """SN 기반 최소 배터리 수정"""
    result = update_min_battery_by_sn(db, sn, data)
    changes = [f"최소배터리={data.min_battery}%"]
    if data.charging_id is not None:
        changes.append(f"충전소 변경(ID={data.charging_id})")
    if data.standby_id is not None:
        changes.append(f"귀환장소 변경(ID={data.standby_id})")
    log_activity("robot", "battery_setting",
                 f"로봇 {sn} 설정 변경 — {', '.join(changes)}",
                 source="api_update_min_battery")
    return result


@router.get("/sn/{sn}/charging-pois")
def api_get_charging_pois(sn: str, db: Session = Depends(get_db)):
    """SN 기반 충전소 POI 조회 — 로봇이 속한 영역의 맵에서 충전소 검색.
    area_id가 없으면 전체 활성 맵에서 충전소를 검색한다.
    """
    robot = db.query(Robot).filter(Robot.serial_number == sn, Robot.is_active == True).first()
    if not robot:
        return []

    # area_id가 있으면 해당 영역의 맵만, 없으면 전체 활성 맵
    if robot.area_id:
        try:
            area_id = int(robot.area_id)
        except (ValueError, TypeError):
            area_id = None
    else:
        area_id = None

    if area_id is not None:
        maps = (
            db.query(RobotMap)
            .filter(RobotMap.area_id == area_id, RobotMap.is_active == True)
            .all()
        )
    else:
        maps = db.query(RobotMap).filter(RobotMap.is_active == True).all()

    if not maps:
        return []

    result = []
    for m in maps:
        pois = (
            db.query(MapPOI)
            .filter(
                MapPOI.map_id == m.id,
                MapPOI.poi_type == "charging",
                MapPOI.is_active == True,
            )
            .all()
        )
        for poi in pois:
            result.append({"id": poi.id, "name": poi.name})

    return result


@router.get("/sn/{sn}/standby-pois")
def api_get_standby_pois(sn: str, db: Session = Depends(get_db)):
    """SN 기반 대기장소 POI 조회 — 전체 활성 맵에서 standby 타입 POI 검색"""
    robot = db.query(Robot).filter(Robot.serial_number == sn, Robot.is_active == True).first()
    if not robot:
        return []

    if robot.area_id:
        try:
            area_id = int(robot.area_id)
        except (ValueError, TypeError):
            area_id = None
    else:
        area_id = None

    if area_id is not None:
        maps = (
            db.query(RobotMap)
            .filter(RobotMap.area_id == area_id, RobotMap.is_active == True)
            .all()
        )
    else:
        maps = db.query(RobotMap).filter(RobotMap.is_active == True).all()

    if not maps:
        return []

    result = []
    for m in maps:
        pois = (
            db.query(MapPOI)
            .filter(
                MapPOI.map_id == m.id,
                MapPOI.poi_type == "standby",
                MapPOI.is_active == True,
            )
            .all()
        )
        for poi in pois:
            result.append({"id": poi.id, "name": poi.name})

    return result


@router.get("/job-status")
def api_get_all_job_status():
    """모든 로봇의 현재 작업 상태 조회"""
    from app.services.jack_service import get_all_job_status
    return get_all_job_status()


@router.get("/job-status/{robot_ip}")
def api_get_job_status(robot_ip: str):
    """특정 로봇의 현재 작업 상태 조회"""
    from app.services.jack_service import get_job_status
    status = get_job_status(robot_ip)
    if not status:
        return {"status": "idle"}
    return status


@router.post("/{robot_id}/switch-floor")
def api_switch_floor(robot_id: int, body: dict, db: Session = Depends(get_db)):
    """로봇 층(Area) 전환 — 맵 동기화 + area_id 업데이트"""
    from app.services.jack_service import get_job_status
    from app.models.map import RobotMap, MapPOI

    area_id = body.get("area_id")
    if not area_id:
        raise HTTPException(400, "area_id 필수")

    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    if not robot or not robot.ip_address:
        raise HTTPException(404, "로봇을 찾을 수 없습니다")

    # 작업 중이면 거부
    if get_job_status(robot.ip_address):
        raise HTTPException(409, "로봇이 작업 중입니다. 작업 완료 후 전환하세요.")

    # 해당 area의 활성 맵 조회
    area_map = db.query(RobotMap).filter(
        RobotMap.area_id == area_id, RobotMap.is_active == True
    ).order_by(RobotMap.id.desc()).first()
    if not area_map:
        raise HTTPException(404, "해당 층에 활성 맵이 없습니다")

    # robot area_id 업데이트
    robot.area_id = str(area_id)
    # 충전소/대기장소 자동 재설정
    charging = db.query(MapPOI).filter(
        MapPOI.map_id == area_map.id, MapPOI.poi_type == "charging", MapPOI.is_active == True
    ).first()
    standby = db.query(MapPOI).filter(
        MapPOI.map_id == area_map.id, MapPOI.poi_type == "standby", MapPOI.is_active == True
    ).first()
    robot.charging_id = charging.id if charging else None
    robot.standby_id = standby.id if standby else None
    db.commit()

    # 맵 전환 — 로봇 맵 ID로 current-map만 전환
    robot_ip = robot.ip_address
    map_id = area_map.id

    import requests as req
    robot_map_id = area_map.robot_map_id
    if not robot_map_id:
        raise HTTPException(400, "해당 맵에 로봇 맵 ID가 설정되지 않았습니다. 맵 동기화를 먼저 해주세요.")

    try:
        r = req.post(f"http://{robot_ip}:8090/chassis/current-map",
                     json={"map_id": robot_map_id}, timeout=10)
        if r.status_code != 200:
            raise HTTPException(500, f"맵 전환 실패: {r.text[:200]}")
    except req.exceptions.RequestException as e:
        raise HTTPException(500, f"맵 전환 실패: {str(e)}")

    # LiDAR 위치 자동 탐색
    try:
        req.post(f"http://{robot_ip}:8090/services/start_global_positioning",
                 json={}, timeout=10)
        logger.info(f"[switch-floor] LiDAR 위치 자동 탐색 시작")
    except Exception as e:
        logger.warning(f"[switch-floor] LiDAR 위치 탐색 실패: {e}")

    # 기본 영역 업데이트 (새로고침해도 이 맵 유지)
    import app.routers.map as _map_mod
    _map_mod._current_area_id = int(area_id)

    log_activity("robot", "switch_floor",
                 f"층 전환: {robot.name} → area_id={area_id} (맵: {area_map.name})",
                 robot_id=robot_id, source="api_switch_floor")

    return {
        "ok": True,
        "message": f"층 전환 완료 (맵: {area_map.name})",
        "area_id": area_id,
        "map_id": map_id,
        "map_name": area_map.name,
        "charging_poi": charging.name if charging else None,
        "standby_poi": standby.name if standby else None,
    }


@router.get("/{robot_id}", response_model=RobotResponse)
def api_get_robot(robot_id: int, db: Session = Depends(get_db)):
    """로봇 단건 조회"""
    return get_robot(db, robot_id)


@router.put("/{robot_id}", response_model=RobotResponse)
def api_update_robot(robot_id: int, data: RobotUpdate, db: Session = Depends(get_db)):
    """RB-02 로봇 정보 수정"""
    result = update_robot(db, robot_id, data)
    log_activity("robot", "robot_update",
                 f"로봇 정보 수정: {result.name} (SN: {result.serial_number})",
                 robot_id=robot_id, source="api_update_robot")
    return result


@router.delete("/{robot_id}")
def api_delete_robot(robot_id: int, db: Session = Depends(get_db)):
    """RB-03 로봇 삭제 (Soft Delete)"""
    robot = db.query(Robot).filter(Robot.id == robot_id).first()
    robot_label = f"{robot.name} (SN: {robot.serial_number})" if robot else f"ID:{robot_id}"
    result = delete_robot(db, robot_id)
    log_activity("robot", "robot_delete",
                 f"로봇 삭제: {robot_label}",
                 robot_id=robot_id, source="api_delete_robot")
    return result


# ── 로봇 상태 관련 ──

@router.get("/{robot_id}/status", response_model=RobotStatusResponse)
def api_get_robot_status(robot_id: int, db: Session = Depends(get_db)):
    """RB-05 로봇 상태 조회"""
    return get_robot_status(db, robot_id)


@router.put("/{robot_id}/status", response_model=RobotStatusResponse)
def api_update_robot_status(
    robot_id: int, data: RobotStatusUpdate, db: Session = Depends(get_db)
):
    """RB-04 로봇 상태 수집/업데이트
    ※ AutoXing SDK/API 연동 지점: 이 엔드포인트로 로봇 상태 데이터를 전송합니다.
    """
    return update_robot_status(db, robot_id, data)


@router.post("/cancel-move/{robot_ip}")
def api_cancel_robot_move(robot_ip: str):
    """로봇의 현재 이동 취소"""
    import requests as http_req
    try:
        r = http_req.patch(
            f"http://{robot_ip}:8090/chassis/moves/current",
            json={"state": "cancelled"}, timeout=10
        )
        return {"message": "이동 취소 완료", "status": r.status_code}
    except Exception as e:
        return {"message": f"취소 실패: {e}", "status": 500}


@router.get("/target/{robot_ip}")
def api_get_robot_target(robot_ip: str):
    """로봇의 현재 이동 목표 조회"""
    import requests as http_req
    try:
        r = http_req.get(f"http://{robot_ip}:8090/chassis/moves/current", timeout=8)
        if r.status_code == 404:
            return {"state": "idle", "target_x": None, "target_y": None}
        data = r.json()
        return {
            "state": data.get("state", ""),
            "type": data.get("type", ""),
            "target_x": data.get("target_x"),
            "target_y": data.get("target_y"),
        }
    except Exception:
        return {"state": "error", "target_x": None, "target_y": None}


# ── 원격 제어 API ──

@router.post("/remote/control-mode/{robot_ip}")
def api_set_control_mode(robot_ip: str, body: dict):
    """로봇 제어 모드 변경 (auto/manual/remote)"""
    import requests as req
    mode = body.get("mode", "auto")
    try:
        r = req.post(
            f"http://{robot_ip}:8090/services/wheel_control/set_control_mode",
            json={"control_mode": mode},
            timeout=10,
        )
        return {"status": r.status_code, "mode": mode}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 원격 조종용 WebSocket 연결 재사용 풀 ──
# twist 는 300ms 마다 들어오는데, 매번 새 WebSocket 을 열면 LTE 에서 연결 수립(0.4s)만으로
# 명령이 밀린다. 로봇별로 연결을 하나 유지해 재사용하면 전송만(수 ms) 하면 된다.
import threading as _threading

_twist_ws: dict = {}          # robot_ip -> websocket connection
_twist_lock = _threading.Lock()


def _get_twist_ws(robot_ip: str):
    import websocket
    with _twist_lock:
        ws = _twist_ws.get(robot_ip)
        if ws is not None:
            return ws
        ws = websocket.create_connection(
            f"ws://{robot_ip}:8090/ws/v2/topics", timeout=8
        )
        _twist_ws[robot_ip] = ws
        return ws


def _drop_twist_ws(robot_ip: str):
    with _twist_lock:
        ws = _twist_ws.pop(robot_ip, None)
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass


@router.post("/remote/twist/{robot_ip}")
def api_send_twist(robot_ip: str, body: dict):
    """WebSocket /twist 명령을 프록시로 전송 (연결 재사용)."""
    import json as _json
    lv = body.get("linear_velocity", 0)
    av = body.get("angular_velocity", 0)
    payload = _json.dumps({"topic": "/twist", "linear_velocity": lv, "angular_velocity": av})

    # 유지 중인 연결로 전송 → 실패(끊김)면 1회 재연결 후 재시도
    for attempt in (1, 2):
        try:
            ws = _get_twist_ws(robot_ip)
            ws.send(payload)
            return {"ok": True}
        except Exception as e:
            _drop_twist_ws(robot_ip)
            if attempt == 2:
                raise HTTPException(status_code=500, detail=str(e))


@router.post("/remote/twist-close/{robot_ip}")
def api_close_twist(robot_ip: str):
    """원격 조종 종료 — 유지하던 WebSocket 연결 정리 (모달 닫을 때 호출)."""
    _drop_twist_ws(robot_ip)
    return {"ok": True}


@router.post("/remote/cancel-move/{robot_ip}")
def api_cancel_move(robot_ip: str):
    """현재 이동 취소"""
    import requests as req
    try:
        r = req.patch(
            f"http://{robot_ip}:8090/chassis/moves/current",
            json={"state": "cancelled"},
            timeout=10,
        )
        return {"status": r.status_code}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/speed/{robot_ip}")
def api_get_speed(robot_ip: str, db: Session = Depends(get_db)):
    """로봇 속도 조회.

    ★ 2026-09-14 — 잭 상태별 2단 속도(LGIT 요청 1번)로 확장됐다.
      `empty_speed`(공차) / `laden_speed`(적재) 가 실제 설정값이고,
      `max_forward_velocity` 는 **공차 속도와 같은 값**으로 계속 내려준다 —
      이 키를 읽던 기존 화면·스크립트가 그대로 동작하게 하기 위한 호환 필드다.
    """
    import requests as req
    from app.services import speed_settings, jack_service

    cfg = speed_settings.get(robot_ip)
    out = {
        "max_forward_velocity": cfg["empty"],   # 하위호환 (= 공차 속도)
        "empty_speed": cfg["empty"],
        "laden_speed": cfg["laden"],
        "laden": jack_service.is_laden(robot_ip),   # 지금 랙을 들고 있나
        "max_backward_velocity": 0.5,
        "max_angular_velocity": 1.2,
    }
    # ★ 2026-09-15 — 로봇에 **실제로 들어있는** 전진 속도와 안전존 상태를 같이 준다.
    #   종전에는 저장값만 내려줘서, 안전존이 RED 로 0.0 을 써둔 상태에도 화면엔
    #   설정값(예: 1.2)이 보였다. 슬라이더를 올려도 안 바뀌는 것처럼 보이는 원인.
    #   `robot_velocity` 가 `empty_speed`/`laden_speed` 와 다르면 안전존이 제한 중이다.
    from app.services import safety_zone
    out["safety_zone"] = (safety_zone.status().get(robot_ip) or {}).get("zone")
    out["robot_velocity"] = None
    try:
        params = req.get(f"http://{robot_ip}:8090/robot-params", timeout=8).json()
        out["max_backward_velocity"] = abs(
            params.get("/wheel_control/max_backward_velocity", -0.5))
        out["max_angular_velocity"] = params.get("/wheel_control/max_angular_velocity", 1.2)
        out["robot_velocity"] = params.get("/wheel_control/max_forward_velocity")
    except Exception:
        pass          # 로봇이 오프라인이어도 설정값은 보여줘야 한다
    return out


@router.post("/speed/{robot_ip}")
def api_set_speed(robot_ip: str, body: dict, db: Session = Depends(get_db)):
    """로봇 속도 변경 — 공차/적재 둘 다 다룬다.

    받는 키 (전부 선택):
      · `max_forward_velocity` / `empty_speed` — 공차 속도(랙 없음)
      · `laden_speed`                          — 적재 속도(랙 있음)

    `max_forward_velocity` 를 공차로 받는 이유는, 기존 콘솔 슬라이더가 그 키로
    보내고 있었고 그 값이 **랙 없이 달릴 때의 속도**였기 때문이다.
    공차 값은 DB `robots.max_speed` 에도 같이 저장된다(서버 시작 시 적용 경로 유지).

    로봇에는 **지금 잭 상태에 맞는 쪽만** 보낸다. 적재 중인데 공차 속도를 쏘면
    랙을 든 채로 빨라진다.
    """
    import logging
    from app.services import speed_settings, jack_service

    empty = body.get("empty_speed", body.get("max_forward_velocity"))
    laden = body.get("laden_speed")
    if empty is None and laden is None:
        raise HTTPException(status_code=400,
                            detail="empty_speed 또는 laden_speed 가 필요합니다")

    cfg = speed_settings.set_speeds(
        robot_ip,
        empty=float(empty) if empty is not None else None,
        laden=float(laden) if laden is not None else None,
    )
    # ★ 2026-09-15 — 안전존이 서행·정지를 걸어둔 동안에는 **로봇에 직접 쓰지 않는다.**
    #   종전에는 무조건 즉시 적용해서, RED(속도 0)로 세워둔 로봇에 슬라이더를
    #   만지면 그 값이 그대로 들어가 **정지해야 할 상황에서 다시 움직일 수 있었다.**
    #   (safety_zone 의 SPEED_REASSERT_SEC=3.0 이 3초 뒤 되돌리지만, 그 사이가 열려 있다)
    #
    #   저장만 해두면 충분하다 — 안전존이 해제할 때 `_base_speed()` 로 이 값을 읽어
    #   복구한다("해제 — … 목표 1.2 m/s" 로그가 그것이다).
    #   CLEAR/SKIP 에서는 종전과 완전히 같다.
    from app.services import safety_zone
    zone = (safety_zone.status().get(robot_ip) or {}).get("zone")
    held = zone in ("yellow", "red")
    applied = False if held else jack_service.apply_state_speed(robot_ip)
    logging.getLogger(__name__).info(
        f"[speed] {robot_ip} 저장 공차={cfg['empty']} 적재={cfg['laden']} "
        f"(현재 {'적재' if jack_service.is_laden(robot_ip) else '공차'}, 적용={applied}"
        + (f" — 안전존 {zone} 이라 즉시 적용 보류, 해제 시 반영)" if held else ")"))
    return {
        "ok": True,
        "max_forward_velocity": cfg["empty"],   # 하위호환
        "empty_speed": cfg["empty"],
        "laden_speed": cfg["laden"],
        "laden": jack_service.is_laden(robot_ip),
        "applied": applied,
        "safety_zone": zone,        # 화면에서 "안전존 제한 중" 을 띄우라고 주는 값
    }


@router.post("/voice/test/{robot_ip}")
def api_voice_test(robot_ip: str, body: dict | None = None, db: Session = Depends(get_db)):
    """음성 미리듣기 — 콘솔에서 지금 설정으로 한 번 들어본다.

    오디오는 8090 이 아니라 9000 채널이다. 배경은 services/robot_voice.py 참조.
    """
    from app.routers.settings import get_voice_settings
    from app.services import robot_voice

    robot = db.query(Robot).filter(Robot.ip_address == robot_ip, Robot.is_active == True).first()
    if not robot:
        raise HTTPException(404, "등록되지 않은 로봇입니다")

    cfg = get_voice_settings()
    body = body or {}
    code = robot_voice.play_once(
        robot_ip,
        robot.serial_number or "",
        audio_id=body.get("audio_id", cfg["audio_id"]),
        url=body.get("url", cfg["url"]),
        volume=int(body.get("volume", cfg["volume"])),
        mode=int(body.get("mode", cfg["mode"])),
        # 미리듣기도 실제 재생과 같은 주소로 보내야 "미리듣기는 되는데 주행 중엔 안 난다"
        # (또는 그 반대)가 안 생긴다
        server_port=int(cfg.get("server_port", 8002)),
        base_url=str(cfg.get("server_url", "")),
    )
    # code 0 이 곧 "소리가 났다"는 뜻은 아니다 — 명령을 받았다는 뜻이다
    return {"ok": code == 0, "code": code}


@router.post("/voice/stop/{robot_ip}")
def api_voice_stop(robot_ip: str, db: Session = Depends(get_db)):
    """재생 중인 음성 즉시 정지"""
    from app.routers.settings import get_voice_settings
    from app.services import robot_voice

    robot = db.query(Robot).filter(Robot.ip_address == robot_ip, Robot.is_active == True).first()
    if not robot:
        raise HTTPException(404, "등록되지 않은 로봇입니다")
    code = robot_voice.stop_play(robot_ip, robot.serial_number or "", int(get_voice_settings()["mode"]))
    return {"ok": code == 0, "code": code}


@router.post("/remote/stop-all/{robot_ip}")
def api_stop_all(robot_ip: str):
    """모든 작업 정지 (이동 취소 + 잭 다운 + 작업 중단)"""
    import requests as req
    # 1) 백엔드 스케줄/수동 배차 작업 중단 (먼저 — 새 명령 방지)
    from app.services.jack_service import stop_robot_job
    stop_robot_job(robot_ip)
    # 2) 로봇 이동 취소
    try:
        req.patch(
            f"http://{robot_ip}:8090/chassis/moves/current",
            json={"state": "cancelled"},
            timeout=10,
        )
    except Exception:
        pass
    # 3) 잭이 올라가 있으면 잭 다운
    try:
        req.post(f"http://{robot_ip}:8090/services/jack_down", json={}, timeout=10)
        from app.services import jack_service
        jack_service.set_laden(robot_ip, False)   # 2단 속도 플래그도 같이 내린다
    except Exception:
        pass
    return {"ok": True, "message": "모든 작업이 정지되었습니다"}


@router.post("/remote/pause/{robot_ip}")
def api_pause_robot(robot_ip: str):
    """일시정지 — 현재 이동 즉시 cancel + paused 플래그 set.
    재개될 때까지 작업 thread 가 대기."""
    from app.services.jack_service import pause_robot_job
    pause_robot_job(robot_ip)
    return {"ok": True, "message": "일시정지"}


@router.post("/remote/resume/{robot_ip}")
def api_resume_robot(robot_ip: str):
    """일시정지 해제 — cancel 된 이동은 safe_move 가 자동 재시도."""
    from app.services.jack_service import resume_robot_job
    resume_robot_job(robot_ip)
    return {"ok": True, "message": "재개"}


@router.get("/remote/paused/{robot_ip}")
def api_is_paused(robot_ip: str):
    """일시정지 상태 조회 — 화면이 [정지]/[재개] 중 무엇을 보여줄지 판단용.

    로봇에 통신하지 않고 서버 메모리 플래그만 읽으므로 폴링해도 부담이 없다.
    """
    from app.services.jack_service import is_paused
    return {"paused": is_paused(robot_ip)}


@router.post("/remote/force-return/{robot_ip}")
def api_force_return(robot_ip: str, db: Session = Depends(get_db)):
    """강제 종료 — 현재 위치에서 잭 업 → 랙 위치 복귀 → 충전소 도킹.
    별도 thread 로 전체 절차를 수행하며 즉시 응답."""
    from app.services.jack_service import force_return_and_dock
    from app.services.thread_utils import safe_thread

    robot = db.query(Robot).filter(Robot.ip_address == robot_ip).first()
    robot_id = robot.id if robot else None
    safe_thread(
        target=force_return_and_dock,
        args=(robot_ip, robot_id),
        name=f"force-return-{robot_ip}",
    ).start()
    from app.crud.activity_log import log_activity
    log_activity("user", "force_return_request", f"강제 종료 요청: {robot_ip}", source="api_force_return")
    return {"ok": True, "message": "강제 종료 시작 — 랙 보관 후 충전소 복귀합니다"}


@router.post("/remote/relocalize/{robot_ip}")
def api_relocalize(robot_ip: str):
    """위치 복구 — start_global_positioning + 실패 시 시스템 재시작 옵션"""
    from app.services.jack_service import recover_positioning
    try:
        ok = recover_positioning(robot_ip, max_wait_sec=15)
        if ok:
            return {"ok": True, "message": "위치 복구 완료"}
        # 복구 실패 시 시스템 재시작
        import requests as req
        req.post(f"http://{robot_ip}:8090/services/restart_service",
                 headers={"Authorization": f"Secret {DEFAULT_SECRET}"},
                 json={}, timeout=10)
        return {"ok": True, "message": "위치 복구 실패 → 시스템 재시작 시작 (약 90초)"}
    except Exception as e:
        raise HTTPException(500, f"위치 복구 실패: {str(e)}")


@router.post("/remote/confirm/{robot_ip}")
def api_confirm_robot(robot_ip: str):
    """잭 업 후 출발 확인"""
    from app.services.jack_service import confirm_robot
    confirm_robot(robot_ip)
    return {"ok": True, "message": "출발 확인됨"}


@router.post("/remote/next-point/{robot_ip}")
def api_set_next_point(robot_ip: str, body: dict, db: Session = Depends(get_db)):
    """다음 포인트 설정 (드롭오프 후 계속 이동)"""
    from app.services.jack_service import set_next_poi
    poi_id = body.get("poi_id")
    if not poi_id:
        # 복귀
        set_next_poi(robot_ip, "return")
        return {"ok": True, "action": "return"}
    poi = db.query(MapPOI).filter(MapPOI.id == poi_id, MapPOI.is_active == True).first()
    if not poi:
        raise HTTPException(404, "POI를 찾을 수 없습니다")
    set_next_poi(robot_ip, {"name": poi.name, "x": poi.world_x, "y": poi.world_y, "ori": poi.angle or 0})
    return {"ok": True, "action": "next", "poi_name": poi.name}


@router.post("/remote/return/{robot_ip}")
def api_return_to_standby(robot_ip: str):
    """복귀 선택"""
    from app.services.jack_service import set_next_poi
    set_next_poi(robot_ip, "return")
    return {"ok": True, "action": "return"}


@router.post("/remote/return-to-standby/{robot_ip}")
def api_return_to_standby_now(robot_ip: str):
    """랙 위치 즉시 복귀 — 현재 위치에서 jack_up → 랙 위치 이동 → jack_down"""
    import threading
    from app.services.jack_service import (
        _get_standby_poi, align_with_retry, jack_up, jack_down,
        create_move, wait_move, update_job_status, clear_job_status,
        JACK_WAIT_SEC,
    )

    standby = _get_standby_poi(robot_ip=robot_ip)
    if not standby:
        raise HTTPException(400, "랙 위치 POI가 없습니다")

    def _run():
        try:
            update_job_status(robot_ip, status="returning", message="랙 위치 복귀 중...")
            # 1) 잭 업 (이미 올려져 있을 수 있으나 안전하게)
            try:
                jack_up(robot_ip)
                import time; time.sleep(JACK_WAIT_SEC)
            except Exception:
                pass
            # 2) W1으로 이동
            move_id = create_move(robot_ip, "to_unload_point",
                                  standby["x"], standby["y"], standby.get("ori", 0))
            result = wait_move(robot_ip, move_id, timeout=120)
            # 3) 잭 다운
            jack_down(robot_ip)
            import time; time.sleep(JACK_WAIT_SEC)
            clear_job_status(robot_ip)
        except Exception as e:
            logger.warning(f"[return-to-standby] 실패: {e}")
            clear_job_status(robot_ip)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"대기장소({standby['name']}) 복귀 시작"}


@router.post("/remote/clear-dispatch/{robot_ip}")
def api_clear_dispatch(robot_ip: str, db: Session = Depends(get_db)):
    """실행 중인 배차 작업을 로봇 이동 없이 강제 정리 (원격제어에서 호출).

    로봇은 물리적으로 그대로 두고(잭/위치 유지) 배차 세션만 종료 → 로봇이 다시 가용해진다.
    충전소로 보내려면 '충전소 복귀'를 별도로 사용.
    """
    from app.services import dispatch_service
    robot = db.query(Robot).filter(Robot.ip_address == robot_ip).first()
    if not robot:
        raise HTTPException(status_code=404, detail="등록되지 않은 로봇입니다")
    ok, msg = dispatch_service.force_clear(robot.id)
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return {"ok": True, "message": msg, "robot_id": robot.id}


@router.post("/remote/dock/{robot_ip}")
def api_dock_to_charger(robot_ip: str, db: Session = Depends(get_db)):
    """충전소로 복귀"""
    import requests as req
    # 로봇의 현재 영역 맵에서 충전소 POI 찾기
    robot = db.query(Robot).filter(Robot.ip_address == robot_ip).first()
    charger_query = db.query(MapPOI).filter(
        MapPOI.poi_type == "charging",
        MapPOI.is_active == True,
    )
    if robot and robot.charging_id:
        charger = db.query(MapPOI).filter(MapPOI.id == robot.charging_id).first()
    elif robot and robot.area_id:
        from app.models.map import RobotMap
        area_map = db.query(RobotMap).filter(
            RobotMap.area_id == int(robot.area_id), RobotMap.is_active == True
        ).order_by(RobotMap.id.desc()).first()
        charger = charger_query.filter(MapPOI.map_id == area_map.id).first() if area_map else charger_query.first()
    else:
        charger = charger_query.first()
    if not charger or charger.world_x is None or charger.world_y is None:
        raise HTTPException(status_code=404, detail="충전소 POI를 찾을 수 없습니다")
    try:
        cx = charger.world_x
        cy = charger.world_y
        cyaw = charger.angle if charger.angle is not None else 0

        # 사전 접근 POI ("<charger_name>-1") 같은 맵에서 검색 — 있으면 그쪽으로 먼저 이동
        ax, ay, ayaw = cx, cy, cyaw
        approach_name_used = None
        try:
            ap = db.query(MapPOI).filter(
                MapPOI.map_id == charger.map_id,
                MapPOI.name == f"{charger.name}-1",
                MapPOI.is_active == True,
            ).first()
            if ap and ap.world_x is not None and ap.world_y is not None:
                ax, ay = ap.world_x, ap.world_y
                ayaw = ap.angle if ap.angle is not None else cyaw
                approach_name_used = ap.name
        except Exception:
            pass

        # ── 접근 → 도킹을 백그라운드에서 순차 진행 ──
        #
        # 2026-09-01 변경 — 이전에는 standard 이동을 쏜 뒤 `sleep(8)` 후 무조건 charge 를
        # 보냈다. 로봇은 새 move 를 받으면 이전 move 를 취소하므로, 8초 안에 도착하지
        # 못하는 거리에서는 이동이 중간에 끊기고 그 자리에 멈춰버린다.
        # (사무실은 경로가 짧아 8초로 맞았지만 현장은 거리가 달라 재현됐다.)
        # → 시간이 아니라 **실제 도착(state=succeeded)** 을 확인한 뒤 charge 를 보낸다.
        #
        # 응답은 지금까지처럼 즉시 돌려준다. 태블릿이 이동 끝날 때까지 기다리면 안 된다.
        import threading

        DOCK_MOVE_TIMEOUT = 300   # 접근 이동 최대 대기(초). LTE 지연·먼 거리 감안
        DOCK_POLL_SEC = 2.0       # 상태 폴링 주기

        def _approach_then_charge():
            import time as _t
            from app.services import jack_service as _js

            # 이전 작업이 남긴 중지 플래그가 있으면 폴링이 즉시 죽는다 → 정리하고 시작
            _js._stop_flags.pop(robot_ip, None)

            # 1) 사전 접근 POI(있으면) 또는 충전소 좌표로 standard 이동
            try:
                resp = _js.robot_post(robot_ip, "/chassis/moves", {
                    "creator": "rcs",
                    "type": "standard",
                    "target_x": ax,
                    "target_y": ay,
                    "target_ori": ayaw,
                })
                move_id = resp.get("id")
            except Exception as e:
                logger.error(f"[dock] 접근 이동 생성 실패 ({robot_ip}): {e}")
                return
            logger.info(f"[dock] 접근 이동 시작 ({robot_ip}) move_id={move_id} "
                        f"target={approach_name_used or charger.name}")

            # 2) 도착할 때까지 폴링 — 배차 상태(pause/stop)에 얽히지 않도록 여기서 직접 본다
            state = "timeout"
            if move_id is not None:
                deadline = _t.time() + DOCK_MOVE_TIMEOUT
                while _t.time() < deadline:
                    try:
                        state = _js.robot_get(robot_ip, f"/chassis/moves/{move_id}").get("state", "")
                    except Exception as e:
                        logger.warning(f"[dock] 이동 상태 조회 실패 (재시도) ({robot_ip}): {e}")
                        state = ""
                    if state in ("succeeded", "failed", "cancelled"):
                        break
                    _t.sleep(DOCK_POLL_SEC)
                else:
                    state = "timeout"

            # 3) 도착했을 때만 charge — 엉뚱한 위치에서 charge 하면 충전기를 못 찾고 멈춘다
            if state != "succeeded":
                logger.warning(f"[dock] 접근 이동 미완료 ({robot_ip}) state={state} → 도킹 생략")
                return
            logger.info(f"[dock] 접근 완료 ({robot_ip}) → 도킹 명령 전송")
            try:
                _js.robot_post(robot_ip, "/chassis/moves", {
                    "creator": "rcs",
                    "type": "charge",
                    "target_x": cx,
                    "target_y": cy,
                    "target_ori": cyaw,
                    "charge_retry_count": 3,
                })
            except Exception as e:
                logger.error(f"[dock] 도킹 명령 실패 ({robot_ip}): {e}")

        threading.Thread(target=_approach_then_charge, daemon=True).start()
        return {"ok": True, "charger": charger.name, "approach": approach_name_used}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/remote/jack/{robot_ip}/{action}")
def api_jack_control(robot_ip: str, action: str):
    """잭 업/다운 제어"""
    import requests as req
    if action not in ("jack_up", "jack_down"):
        raise HTTPException(status_code=400, detail="action must be jack_up or jack_down")
    try:
        r = req.post(
            f"http://{robot_ip}:8090/services/{action}",
            json={},
            timeout=10,
        )
        # 이 경로는 jack_service 를 거치지 않고 로봇을 직접 친다.
        # 2단 속도(요청 1번)의 적재 플래그가 여기서 어긋나면, 원격으로 잭을 올린
        # 로봇이 공차 속도로 달리게 된다 → 여기서도 같이 갱신한다.
        if r.status_code < 400:
            from app.services import jack_service
            jack_service.set_laden(robot_ip, action == "jack_up")
        return {"status": r.status_code}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/remote/shutdown/{robot_ip}")
def api_shutdown_robot(robot_ip: str):
    """로봇 전원 종료"""
    import requests as req
    try:
        r = req.post(
            f"http://{robot_ip}:8090/services/baseboard/shutdown",
            json={"target": "main_power_supply", "reboot": False},
            timeout=10,
        )
        return {"status": r.status_code, "message": "로봇 종료 명령 전송"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
