"""
잭킹 작업 공용 서비스
- 로봇 REST API 헬퍼 함수
- 잭킹 흐름 실행 (align_with_rack → jack_up → to_unload_point → jack_down → standby)
"""
import logging
import math
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

ROBOT_PORT = 8090
HTTP_TIMEOUT = 15  # LTE 지연 대응 (명령/조회 유실 방지). WiFi 복귀 시 10으로 낮춰도 됨
import json as _json


def get_docking_point_coords(ip: str, charger_name: str):
    """로봇 맵에서 충전소 도킹포인트(type=36) 좌표 조회 → (x, y, yaw) 또는 None"""
    try:
        r = requests.get(f"http://{ip}:{ROBOT_PORT}/chassis/current-map", timeout=HTTP_TIMEOUT)
        map_id = r.json().get("id")
        if not map_id:
            return None
        r = requests.get(f"http://{ip}:{ROBOT_PORT}/maps/{map_id}", timeout=HTTP_TIMEOUT)
        overlays = _json.loads(r.json().get("overlays", "{}"))
        features = overlays.get("features", [])

        # 충전소(type=9)에서 도킹포인트 ID 찾기
        docking_point_id = None
        for feat in features:
            props = feat.get("properties", {})
            if str(props.get("type")) == "9" and props.get("name") == charger_name:
                docking_point_id = props.get("dockingPointId")
                break

        if not docking_point_id:
            return None

        # 도킹포인트(type=36) 좌표 조회
        for feat in features:
            if feat.get("id") == docking_point_id:
                coords = feat.get("geometry", {}).get("coordinates", [])
                props = feat.get("properties", {})
                yaw = float(props.get("yaw", 0))
                if len(coords) >= 2:
                    logger.info(f"[{ip}] '{charger_name}' 도킹포인트: ({coords[0]}, {coords[1]}, yaw={yaw})")
                    return (coords[0], coords[1], yaw)
        return None
    except Exception as e:
        logger.warning(f"[{ip}] 도킹포인트 좌표 조회 실패: {e}")
        return None
# 이동 상태 폴링 주기(초) — "다 갔니?" 를 묻는 간격이다.
#
# ★ 2.0 → 0.5 (2026-09-15). 이 값은 안전 파라미터가 아니라 **관찰 주기**다.
#   로봇이 폴링 사이에 도착하면 그동안 다음 명령이 없어 **그냥 서 있는다.**
#   평균 대기 = 주기/2 이므로 2.0 은 이동 경계마다 평균 1초를 버렸다.
#   배송 1사이클은 이동 명령 13개로 쪼개져 있어(회전·경유지체인·진입점·
#   align·잭·이탈·복귀) 누적 평균 약 13초, 최악 26초가 '멈칫' 으로 보였다.
#
# ⚠️ 종전 2.0 의 근거는 "LTE 데이터·부하 절감" 이었다. GET 횟수가 4배가 되므로
#    현장(M2M 전용망) 이관 전에 **요금제 데이터 한도를 반드시 확인할 것.**
#    되돌리려면 이 값만 2.0 으로 되돌리면 된다(다른 로직 변경 없음).
POLL_INTERVAL = 0.5

# 짧은 이동 전용 주기 — 제자리 회전 · 진입점 · 랙 자리 이탈 · 하차 이탈.
# 이런 이동은 실제 소요가 1~5초라 0.5 주기로도 대기 비중이 5~25% 로 크다.
# 원래 폴링 횟수가 적은 구간이라 더 조여도 트래픽 증가는 미미하다.
POLL_INTERVAL_SHORT = 0.2
MOVE_TIMEOUT = 180     # LTE 지연 감안해 이동 완료 대기 상향 (120 → 180초)

# 실행 중인 작업 추적 (robot_ip → stop flag)
_stop_flags: dict[str, bool] = {}

# 일시정지 플래그 (robot_ip → bool). True 인 동안 wait_move/safe_move/_interruptible_sleep 가 대기.
_paused_flags: dict[str, bool] = {}

# 실행 중인 작업 상태 (robot_ip → job info)
_job_status: dict[str, dict] = {}

# 수동 확인 대기 (robot_ip → threading.Event)
import threading as _threading
_confirm_events: dict[str, _threading.Event] = {}

# 다음 포인트 (robot_ip → poi dict or "return")
_next_poi: dict[str, dict | str | None] = {}

# ── 자동 재시도용 작업 메타데이터 ──────────────────────────────
# 진행 중인 route 작업의 입력값 (양보 후 재실행 가능하도록 보관)
# robot_ip → {robot_id, waypoints, manual_confirm, area_id, work_mode}
_active_route_jobs: dict[str, dict] = {}
# 양보로 일시정지된 작업 (deadlock_monitor 가 시간 경과 후 재실행)
# robot_ip → {**meta, paused_at, retries_left}
_paused_route_jobs: dict[str, dict] = {}
# create_move 의 zone 사전 락에 robot_id 를 라우팅하기 위한 매핑
# (한 로봇은 동시에 한 route 작업만 실행되도록 보장되므로 안전)
_running_robot_id_by_ip: dict[str, int] = {}


def _get_standby_poi(area_id: int | None = None, robot_ip: str | None = None) -> dict | None:
    """로봇의 standby_id 우선, 없으면 영역의 standby POI(W1) 조회"""
    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap
    from app.models.robot import Robot
    db = SessionLocal()
    try:
        # 1) 로봇에 standby_id가 지정되어 있으면 그것 사용
        if robot_ip:
            robot = db.query(Robot).filter(Robot.ip_address == robot_ip).first()
            if robot and robot.standby_id:
                poi = db.query(MapPOI).filter(
                    MapPOI.id == robot.standby_id, MapPOI.is_active == True
                ).first()
                if poi and poi.world_x is not None:
                    return {"name": poi.name, "x": poi.world_x, "y": poi.world_y, "ori": poi.angle or 0}

        # 2) 폴백: area_id 또는 최신 맵의 standby POI
        if area_id:
            active_map = db.query(RobotMap).filter(
                RobotMap.area_id == area_id, RobotMap.is_active == True
            ).order_by(RobotMap.id.desc()).first()
        else:
            active_map = db.query(RobotMap).filter(RobotMap.is_active == True).order_by(RobotMap.id.desc()).first()
        if not active_map:
            return None
        poi = db.query(MapPOI).filter(
            MapPOI.map_id == active_map.id,
            MapPOI.poi_type == "standby",
            MapPOI.is_active == True,
        ).first()
        if poi and poi.world_x is not None:
            return {"name": poi.name, "x": poi.world_x, "y": poi.world_y, "ori": poi.angle or 0}
        return None
    finally:
        db.close()


# 사용자 확인 대기 기본 한도 (좀비 작업 방지)
CONFIRM_TIMEOUT_SEC = 1800  # 30분


def wait_for_confirm(robot_ip: str, timeout: int | None = None) -> bool:
    """사용자 확인 버튼을 기다림.
    timeout=None 이면 CONFIRM_TIMEOUT_SEC(30분) 적용 — 무한 대기 방지.

    반환:
      - True  : 정상 confirm 또는 stop 신호로 깨어남
                (stop 으로 깬 경우 호출자는 이어지는 _check_stop 에서 RuntimeError 로 종료됨)
      - False : 타임아웃

    Event 객체는 try/finally 로 _confirm_events 에서 정리 보장 — 예외 시 좀비 entry 누적 방지.
    """
    evt = _threading.Event()
    _confirm_events[robot_ip] = evt
    effective_timeout = timeout if timeout is not None else CONFIRM_TIMEOUT_SEC
    try:
        result = evt.wait(timeout=effective_timeout)
        if not result:
            logger.warning(f"[wait_for_confirm] {robot_ip} 확인 대기 타임아웃 ({effective_timeout}s)")
        return result
    finally:
        _confirm_events.pop(robot_ip, None)


def confirm_robot(robot_ip: str):
    """사용자가 확인 버튼을 눌렀을 때 호출"""
    evt = _confirm_events.get(robot_ip)
    if evt:
        evt.set()
        logger.info(f"[jack_service] confirm received for {robot_ip}")


def set_next_poi(robot_ip: str, poi: dict | str):
    """다음 포인트 설정 (poi dict 또는 'return') + confirm 트리거"""
    _next_poi[robot_ip] = poi
    confirm_robot(robot_ip)
    logger.info(f"[jack_service] next poi set for {robot_ip}: {poi if isinstance(poi, str) else poi.get('name')}")


def get_next_poi(robot_ip: str) -> dict | str | None:
    """다음 포인트 가져오기 (한 번 읽으면 제거)"""
    return _next_poi.pop(robot_ip, None)


def stop_robot_job(robot_ip: str):
    """특정 로봇의 진행 중인 작업에 중지 플래그 설정 + 상태 제거.

    wait_for_confirm 안에서 대기 중인 작업도 즉시 깨우기 위해 _confirm_events 의 event 를 set.
    깨어난 작업은 곧이은 _check_stop() 에서 RuntimeError 로 정상 종료된다.
    (set 안 하면 최대 CONFIRM_TIMEOUT_SEC=30분 좀비 thread 가 됨)
    """
    _stop_flags[robot_ip] = True
    _job_status.pop(robot_ip, None)
    _next_poi.pop(robot_ip, None)
    evt = _confirm_events.get(robot_ip)
    if evt is not None:
        evt.set()
    logger.info(f"[jack_service] stop flag set for {robot_ip}")


def yield_robot_job(robot_ip: str, retries: int = 1) -> bool:
    """양보 — 현재 작업을 멈추되, 재시도용 메타를 _paused_route_jobs 로 옮김.
    deadlock_monitor 가 N초 후 같은 입력으로 run_route_job 을 다시 호출하게 된다.
    반환: 양보 가능 여부 (메타가 있으면 True).
    """
    meta = _active_route_jobs.get(robot_ip)
    if meta is None:
        # 등록된 메타가 없으면 일반 stop 으로 폴백
        stop_robot_job(robot_ip)
        return False
    _paused_route_jobs[robot_ip] = {
        **meta,
        "paused_at": time.time(),
        "retries_left": retries,
    }
    stop_robot_job(robot_ip)
    logger.info(f"[jack_service] yielded {robot_ip} (retries_left={retries})")
    return True


def get_paused_jobs() -> dict[str, dict]:
    """양보로 일시정지된 작업 스냅샷 (deadlock_monitor 가 폴링)."""
    return dict(_paused_route_jobs)


def consume_paused_job(robot_ip: str) -> dict | None:
    """양보된 메타를 꺼내가며 제거 (재실행 직전 호출)."""
    return _paused_route_jobs.pop(robot_ip, None)


def force_return_and_dock(robot_ip: str, robot_id: int | None = None):
    """강제 종료 — 현재 진행 중 작업을 중단하고 충전소 도킹까지 수행.

    work_mode 별 분기:
      - rack_pickup        : 현재 위치에서 잭 업 → 랙 위치(standby POI) 복귀 → 잭 다운 → 충전소
      - delivery_no_rack   : 안전을 위해 잭 다운 한 번 시도 → 충전소
      - simple_move        : 잭 조작 없이 곧장 충전소 복귀

    work_mode 가 명시되지 않으면 _active_route_jobs 메타에서 추정, 그것도 없으면
    rack_pickup 으로 폴백(랙을 들고 있을 가능성을 가정 — 안전한 디폴트).
    """
    from app.services.scheduler import _return_to_charger

    # work_mode 캡쳐 — stop_robot_job 전에 메타에서 읽어둠 (작업 스레드가 finally 에서 pop 할 수 있어 선캡쳐)
    meta = _active_route_jobs.get(robot_ip) or {}
    work_mode = (meta.get("work_mode") or "rack_pickup")
    is_rack_pickup = (work_mode == "rack_pickup")
    is_delivery_no_rack = (work_mode == "delivery_no_rack")

    logger.warning(f"[force_return] {robot_ip} 강제 종료 시작 (work_mode={work_mode})")

    # 1) 진행 중 작업 중단 (현재 thread 가 RuntimeError 로 빠져나옴)
    stop_robot_job(robot_ip)
    # 2) 진행 중 이동 cancel
    try:
        cancel_current_move(robot_ip)
    except Exception:
        pass
    # 3) 진행 중 thread 가 정리될 시간 확보 + paused 풀기
    time.sleep(3)
    _stop_flags.pop(robot_ip, None)
    _paused_flags.pop(robot_ip, None)

    try:
        if is_rack_pickup:
            update_job_status(robot_ip, status="returning", message="강제 종료 — 랙 보관 후 충전소 복귀")

            # 4) 현재 위치에서 잭 업 (이미 들고 있으면 일부 펌웨어는 noop, 일부는 에러 — 무시)
            try:
                jack_up(robot_ip)
                time.sleep(JACK_WAIT_SEC)
            except Exception as e:
                logger.warning(f"[force_return] jack_up: {e}")

            # 5) 랙 위치(standby POI) 로 이동 + 잭 다운
            standby = _get_standby_poi(robot_ip=robot_ip)
            if standby:
                update_job_status(robot_ip, status="moving",
                                  message=f"랙 위치({standby['name']})로 복귀 중")
                try:
                    safe_move(robot_ip, "to_unload_point",
                              standby["x"], standby["y"], standby.get("ori", 0),
                              max_attempts=30, timeout=120)
                except Exception as e:
                    logger.warning(f"[force_return] standby 이동 실패: {e}")
                try:
                    update_job_status(robot_ip, status="jacking_down",
                                      message=f"랙 위치({standby['name']}) 잭 내리는 중")
                    jack_down(robot_ip)
                    time.sleep(JACK_WAIT_SEC)
                except Exception as e:
                    logger.warning(f"[force_return] jack_down: {e}")
            else:
                logger.info(f"[force_return] {robot_ip} 랙 위치 POI 없음 — 충전소만 복귀")
        elif is_delivery_no_rack:
            # delivery_no_rack — 잭이 들려 있을 수 있어 안전을 위해 한 번 잭다운 시도 후 충전소
            update_job_status(robot_ip, status="jacking_down",
                              message="강제 종료 — 잭 내리는 중")
            try:
                jack_down(robot_ip)
                time.sleep(JACK_WAIT_SEC)
            except Exception as e:
                logger.warning(f"[force_return] jack_down: {e}")
            update_job_status(robot_ip, status="returning",
                              message="강제 종료 — 충전소로 복귀")
        else:
            # simple_move — 잭 조작 없이 곧장 충전소
            update_job_status(robot_ip, status="returning",
                              message=f"강제 종료 — 충전소로 복귀 ({work_mode})")

        # 6) 충전소 복귀
        try:
            _return_to_charger(robot_ip, [])
        except Exception as e:
            logger.warning(f"[force_return] 충전소 복귀 실패: {e}")

        from app.crud.activity_log import log_activity
        log_activity("robot", "force_return",
                     f"강제 종료 완료: {robot_ip} (work_mode={work_mode})",
                     source="force_return_and_dock")
    finally:
        # 7) 락/상태 정리
        if robot_id is not None:
            try:
                from app.services import poi_lock as _pl, zone_lock as _zl
                _pl.release_all_by_robot(robot_id)
                _zl.release_all_by_robot(robot_id)
            except Exception:
                pass
        clear_job_status(robot_ip)
        _stop_flags.pop(robot_ip, None)
        _paused_flags.pop(robot_ip, None)
        _active_route_jobs.pop(robot_ip, None)
        _running_robot_id_by_ip.pop(robot_ip, None)
        logger.info(f"[force_return] {robot_ip} 강제 종료 종료")


def _check_stop(robot_ip: str):
    """중지 플래그 확인 — True면 예외 발생"""
    if _stop_flags.get(robot_ip):
        _stop_flags.pop(robot_ip, None)
        raise RuntimeError(f"작업 중지됨 (robot={robot_ip})")


def is_paused(robot_ip: str) -> bool:
    return bool(_paused_flags.get(robot_ip))


def _wait_if_paused(robot_ip: str, poll: float = 1.0):
    """paused 동안 폴링 대기 — 중지가 들어오면 즉시 RuntimeError."""
    while _paused_flags.get(robot_ip):
        _check_stop(robot_ip)
        time.sleep(poll)


def pause_robot_job(robot_ip: str):
    """일시정지 — 현재 이동 즉시 cancel + paused 플래그 set.
    재개될 때까지 safe_move/wait_move/_interruptible_sleep 가 대기."""
    _paused_flags[robot_ip] = True
    try:
        cancel_current_move(robot_ip)
    except Exception:
        pass
    update_job_status(robot_ip, message="일시정지됨")
    logger.info(f"[pause] {robot_ip} 일시정지")


def resume_robot_job(robot_ip: str):
    """일시정지 해제 — 이전에 cancel 된 이동은 safe_move 가 자동 재시도."""
    _paused_flags.pop(robot_ip, None)
    update_job_status(robot_ip, message="작업 재개")
    logger.info(f"[resume] {robot_ip} 재개")


def clear_stop_flag(robot_ip: str) -> None:
    """중지 플래그를 **소비 없이** 걷어낸다.

    `_check_stop()` 은 플래그를 pop 하면서 RuntimeError 를 던진다. 그래서 강제
    종료 뒤에 새 이동을 걸려면 그 전에 플래그를 비워야 한다 — 안 그러면
    새로 건 이동이 첫 체크에서 바로 취소된다 (2026-09-17 강제 종료 후속 동작).
    """
    _stop_flags.pop(robot_ip, None)
    _paused_flags.pop(robot_ip, None)


def _interruptible_sleep(robot_ip: str, seconds: float):
    """중지/일시정지 가능한 대기 — 1초 간격으로 plain check.
    paused 중에는 elapsed 가 진행되지 않음 (재개 시점부터 다시 카운트)."""
    elapsed = 0.0
    while elapsed < seconds:
        _check_stop(robot_ip)
        _wait_if_paused(robot_ip)
        sleep_time = min(1.0, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time


def update_job_status(robot_ip: str, **kwargs):
    """작업 상태 업데이트"""
    if robot_ip not in _job_status:
        _job_status[robot_ip] = {}
    _job_status[robot_ip].update(kwargs)


def clear_job_status(robot_ip: str):
    """작업 상태 제거"""
    _job_status.pop(robot_ip, None)


def get_job_status(robot_ip: str) -> dict | None:
    """작업 상태 조회"""
    return _job_status.get(robot_ip)


def get_all_job_status() -> dict:
    """모든 로봇 작업 상태 조회"""
    return dict(_job_status)


# ── 로봇 REST API 헬퍼 ──

def robot_url(ip: str, path: str) -> str:
    return f"http://{ip}:{ROBOT_PORT}{path}"


def robot_get(ip: str, path: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(robot_url(ip, path), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < retries - 1:
                time.sleep(3)
            else:
                raise


def robot_post(ip: str, path: str, json_body: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.post(robot_url(ip, path), json=json_body or {}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < retries - 1:
                time.sleep(3)
            else:
                raise


def robot_patch(ip: str, path: str, json_body: dict, timeout: Optional[float] = None) -> dict:
    r = requests.patch(robot_url(ip, path), json=json_body,
                       timeout=(HTTP_TIMEOUT if timeout is None else timeout))
    r.raise_for_status()
    return r.json()


class ZoneBlocked(Exception):
    """이동 경로의 zone 이 다른 로봇에 점유되어 이동을 보류해야 함 (safe_move 재시도 대상)."""


def create_move(ip: str, move_type: str, target_x: float, target_y: float,
                target_ori: float = 0, retries: int = 5, **extra) -> int:
    # Zone 사전 락 — 이번 이동이 zone(좁은 통로 등)을 지나가면 다른 로봇 점유 해제까지 대기.
    rid = _running_robot_id_by_ip.get(ip)
    if rid is not None and move_type != "charge":
        proceed = True
        try:
            from app.services.zone_guard import acquire_zones_for_move
            proceed, _zones = acquire_zones_for_move(
                ip, rid, target_x, target_y,
                is_stop_fn=lambda: _stop_flags.get(ip, False),
            )
        except Exception as e:
            logger.warning(f"[zone_guard] acquire 실패(무시): {e}")
            proceed = True  # zone 가드 자체 오류는 이동을 막지 않음(기존 동작 보존)
        if not proceed:
            # 점유 미해제(타임아웃) 또는 중지 요청 — 강제 진입하지 않는다.
            if _stop_flags.get(ip, False):
                raise RuntimeError("zone 대기 중 중지 요청")
            raise ZoneBlocked(f"zone 점유 — 이동 보류 (robot {rid})")
    body = {
        "creator": "rcs",
        "type": move_type,
        "target_x": target_x,
        "target_y": target_y,
        "target_ori": target_ori,
        **extra,
    }
    for attempt in range(retries):
        try:
            resp = robot_post(ip, "/chassis/moves", body)
            return resp.get("id")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and attempt < retries - 1:
                logger.warning(f"[move] 400 에러, {5}초 후 재시도 ({attempt+1}/{retries})")
                time.sleep(5)
            else:
                from app.crud.activity_log import log_activity
                log_activity("robot", "move_error", f"이동 명령 실패 ({move_type}): {str(e)}", source="jack_service")
                raise


# 좌표 자체가 잘못된 경우 영원히 도는 것 방지용 대형 한도 (≈ 200 * 5s = 17분)
SAFE_MOVE_DEFAULT_MAX_ATTEMPTS = 200

# 이동 실패 코드 — 로봇 펌웨어 값.
# 재시도가 잦은데(2026-09-07 실측: 2~5회차) 사유가 로그에 안 남아 원인을 못 짚었다.
# 재시도 1회는 retry_delay 5초를 그냥 버리므로, 무엇 때문인지 보이게 한다.
MOVE_FAIL_REASONS = {
    0: "없음", 1: "알 수 없음", 2: "맵 획득 실패",
    3: "출발점이 맵 밖", 4: "도착점이 맵 밖",
    5: "출발점이 이동 불가 영역", 6: "도착점이 이동 불가 영역",
    7: "출발점과 도착점이 동일", 8: "글로벌 경로 확장데이터 계산 실패",
    9: "경로 연결 안 됨", 10: "경로 계산 타임아웃", 11: "글로벌 경로 없음",
    12: "글로벌 경로에서 출발점 잡기 실패", 13: "글로벌 경로에서 도착점 잡기 실패",
    14: "경로 계획 장시간 실패", 15: "이동 타임아웃",
    16: "센서 데이터 이상", 17: "충전 케이블 연결됨", 18: "회전 타임아웃",
    100: "충전 재시도 초과", 101: "충전독 감지 오류", 102: "도킹 신호 미수신",
    103: "유효하지 않은 충전독 위치", 104: "이미 충전 중", 105: "충전 전류 미감지",
    400: "유효하지 않은 트랙 포인트", 401: "트랙 시작점에서 너무 멀리 있음",
    500: "유효하지 않은 랙 감지 위치", 501: "랙 감지 오류",
    502: "랙 도킹 재시도 초과", 503: "언로드 지점 점유됨", 504: "언로드 지점 도달 불가",
}


def describe_fail(result: dict) -> str:
    """이동 결과에서 실패 사유를 사람이 읽을 수 있게. 없으면 빈 문자열."""
    fr = result.get("fail_reason")
    if fr in (None, 0):
        return ""
    name = MOVE_FAIL_REASONS.get(fr, "정의되지 않은 코드")
    extra = result.get("fail_reason_str")
    tail = f" / {extra}" if extra and extra != "None - None" else ""
    return f" fail_reason={fr}({name}){tail}"


def safe_move(ip: str, move_type: str, target_x: float, target_y: float,
              target_ori: float = 0, retry_delay: float = 5.0,
              max_attempts: int | None = None, timeout: int = MOVE_TIMEOUT,
              poll: float | None = None, **extra) -> dict:
    """create_move + wait_move 통합 + 자동 재시도.

    "작업이 무조건 이어가도록" 설계:
      - 통신 실패(ConnectionError 등) → 짧은 대기 후 재시도
      - wait_move 결과 'failed' / 'timeout' → 대기 후 재시도
      - 사용자 중지(_check_stop) → RuntimeError 로 즉시 빠져나감
      - 'succeeded' / 'cancelled' → 결과 반환
      - max_attempts=None 이면 SAFE_MOVE_DEFAULT_MAX_ATTEMPTS(200회 ≈ 17분) 까지 재시도.
        그 이상은 좌표 자체 오류 가능성 — 영원히 도는 것 방지.
    """
    if max_attempts is None:
        max_attempts = SAFE_MOVE_DEFAULT_MAX_ATTEMPTS
    attempt = 0
    last_result: dict = {}
    while True:
        _check_stop(ip)
        _wait_if_paused(ip)
        attempt += 1
        # 1) 이동 명령 발행
        try:
            move_id = create_move(ip, move_type, target_x, target_y, target_ori, **extra)
        except RuntimeError:
            raise
        except ZoneBlocked as e:
            # 통로 점유 — 강제 진입 대신 보류 후 재시도 (통로가 빌 때까지 대기)
            logger.info(f"[safe_move] {ip} 통로 점유로 이동 보류 (시도 {attempt}): {e}")
            update_job_status(ip, message=f"통로 혼잡 — 양보 대기 중 ({attempt}회차)")
            if max_attempts is not None and attempt >= max_attempts:
                return {"state": "failed", "fail_message": "zone 점유 지속 — 이동 보류 한도 초과"}
            _interruptible_sleep(ip, retry_delay)
            continue
        except Exception as e:
            logger.warning(f"[safe_move] {ip} create_move 실패 (시도 {attempt}): {e}")
            update_job_status(ip, message=f"통신 오류 — 재시도 중 ({attempt}회차)")
            if max_attempts is not None and attempt >= max_attempts:
                return {"state": "failed", "fail_message": f"create_move 통신 실패: {e}"}
            _interruptible_sleep(ip, retry_delay)
            continue
        # 2) 이동 완료 대기
        try:
            result = wait_move(ip, move_id, timeout=timeout, poll=poll)
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"[safe_move] {ip} wait_move 통신 실패 (시도 {attempt}): {e}")
            update_job_status(ip, message=f"통신 오류 — 재시도 중 ({attempt}회차)")
            if max_attempts is not None and attempt >= max_attempts:
                return {"state": "failed", "fail_message": f"wait_move 통신 실패: {e}"}
            _interruptible_sleep(ip, retry_delay)
            continue
        last_result = result
        state = str(result.get("state", "")).lower()
        if state == "succeeded":
            if attempt > 1:
                logger.info(f"[safe_move] {ip} {attempt}회차 만에 성공 ({move_type})")
                update_job_status(ip, message=f"재시도 {attempt}회차에 이동 성공")
            return result
        if state == "cancelled":
            # paused/resumed 로 인한 cancel — 재개 후 같은 이동 재시도
            if result.get("_paused_cancel") or _paused_flags.get(ip):
                logger.info(f"[safe_move] {ip} pause/resume 으로 cancel — 같은 이동 재시도")
                _wait_if_paused(ip)
                _check_stop(ip)
                continue
            return result
        # 3) failed / timeout / unknown → 재시도
        fail_msg = result.get("fail_message") or state or "unknown"
        logger.warning(
            f"[safe_move] {ip} 이동 미완료 (시도 {attempt}, state={state}, "
            f"type={move_type}, id={move_id}, 목표=({target_x:.2f},{target_y:.2f})): "
            f"{fail_msg}{describe_fail(result)}")
        update_job_status(ip, message=f"이동 미완료 ({fail_msg}) — 재시도 중 ({attempt}회차)")
        if max_attempts is not None and attempt >= max_attempts:
            return last_result
        _interruptible_sleep(ip, retry_delay)


def wait_move(ip: str, move_id: int, timeout: int = MOVE_TIMEOUT,
              poll: float | None = None) -> dict:
    """`poll` 은 상태 확인 주기(초). None 이면 POLL_INTERVAL.

    짧은 이동은 `POLL_INTERVAL_SHORT` 를 넘겨 경계 대기를 줄인다.
    """
    poll = POLL_INTERVAL if poll is None else float(poll)
    deadline = time.time() + timeout
    saw_pause = False  # 이번 wait 동안 paused 들어간 적 있는지 추적
    while time.time() < deadline:
        _check_stop(ip)
        if _paused_flags.get(ip):
            saw_pause = True
        _wait_if_paused(ip)
        resp = robot_get(ip, f"/chassis/moves/{move_id}")
        state = resp.get("state", "")
        if state in ("succeeded", "failed", "cancelled"):
            # paused 때문에 발생한 cancel 임을 표시 — safe_move 가 재시도하도록
            if state == "cancelled" and saw_pause:
                resp["_paused_cancel"] = True
            return resp
        time.sleep(poll)
    return {"state": "timeout", "fail_message": f"Move {move_id} timed out after {timeout}s"}


# 잭 상태 판정 임계값 (2026-08-18 실측: 랙 적재 progress=1.0/weight=85, 빈 상태 0/0)
JACK_UP_PROGRESS_MIN = 0.9      # 이 이상이면 잭이 올라간 것
JACK_LOADED_WEIGHT_MIN = 10.0   # 이 이상이면 하중이 실린 것 = 랙을 들고 있음


# ── 적재 여부(랙을 들고 있는가) — LGIT 요청 1번 2단 속도용 ──────
#
# ★ 여기(jack_service)에서 갱신하는 것이 핵심이다.
#   잭을 올리는 경로가 배차 워커·원격제어·잭 테스트로 여러 갈래인데, 전부 아래
#   jack_up/jack_down 을 거친다. 여기서 한 번 갱신하면 **모든 호출자가 자동으로
#   커버된다.** 호출부마다 플래그를 세우면 언젠가 한 군데를 빠뜨린다.
#
# 서버 재시작 시 이 값은 사라진다 → boot_recovery 가 is_rack_loaded() 로 1회 보정한다.
_laden_flags: dict[str, bool] = {}


def is_laden(ip: str) -> bool:
    """지금 랙을 들고 있는 것으로 보는가. 모르면 False(= 공차, 종전 속도)."""
    return bool(_laden_flags.get(ip, False))


def set_laden(ip: str, laden: bool, *, apply_speed: bool = True) -> None:
    """적재 상태를 바꾸고, 바뀌었으면 그 즉시 해당 속도를 로봇에 1회 적용한다.

    안전존(safety_zone)이 꺼져 있을 수도 있으므로 여기서도 직접 보낸다.
    켜져 있으면 안전존이 `_base_speed()` 로 같은 값을 계속 유지한다.
    """
    before = _laden_flags.get(ip)
    _laden_flags[ip] = bool(laden)
    if before == bool(laden) or not apply_speed:
        return
    logger.info(f"[speed] {ip} 적재 상태 변경: {before} → {bool(laden)}")
    apply_state_speed(ip)


def apply_state_speed(ip: str) -> bool:
    """지금 적재 상태에 맞는 속도를 로봇에 적용한다. 실패해도 예외를 올리지 않는다."""
    try:
        from app.services import speed_settings
        v = speed_settings.speed_for(ip, is_laden(ip))
        r = requests.post(robot_url(ip, "/robot-params"),
                          json={"/wheel_control/max_forward_velocity": float(v)},
                          timeout=5)
        ok = r.status_code < 400
        logger.info(f"[speed] {ip} {'적재' if is_laden(ip) else '공차'} 속도 "
                    f"{v} m/s 적용 {'성공' if ok else f'거부(HTTP {r.status_code})'}")
        return ok
    except Exception as e:
        logger.warning(f"[speed] {ip} 속도 적용 실패(무시): {e}")
        return False


def jack_up(ip: str) -> dict:
    # robot_post 는 실패하면 예외를 던진다 → 여기까지 오면 명령이 접수된 것이다
    res = robot_post(ip, "/services/jack_up")
    set_laden(ip, True)
    return res


def jack_down(ip: str) -> dict:
    res = robot_post(ip, "/services/jack_down")
    set_laden(ip, False)
    return res


def read_jack_state(ip: str, timeout: float = 6.0) -> Optional[dict]:
    """WS /jack_state 로 잭 상태 1건 읽기. 실패 시 None.

    반환 예: {'state':'hold', 'progress':1.0, 'weight':85}
    실측(2026-08-18, LG 랙): 잭업+랙적재 progress=1.0 weight=85 / 빈 상태 progress=0.0 weight=0
    state 는 두 경우 모두 'hold' 라 구분에 쓸 수 없다. progress 와 weight 로 판정할 것.
    """
    import websocket  # 선택 의존성 — 여기서만 사용
    ws = None
    try:
        ws = websocket.create_connection(f"ws://{ip}:{ROBOT_PORT}/ws/v2/topics", timeout=timeout)
        ws.send(_json.dumps({"enable_topic": "/jack_state"}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            pkt = _json.loads(ws.recv())
            if pkt.get("topic") == "/jack_state":
                return pkt
    except Exception as e:
        logger.warning(f"[jack] /jack_state 읽기 실패 ({ip}): {e}")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return None


def is_rack_loaded(ip: str, timeout: float = 6.0) -> Optional[bool]:
    """지금 랙을 들고 있는가? 판정 불가(통신 실패)면 None.

    잭이 올라가 있고(progress) 하중이 실려 있어야(weight) 랙을 든 것으로 본다.
    잭만 올리고 랙이 없으면 weight 가 0 에 가깝다.
    """
    st = read_jack_state(ip, timeout=timeout)
    if not st:
        return None
    try:
        progress = float(st.get("progress") or 0)
        weight = float(st.get("weight") or 0)
    except (TypeError, ValueError):
        return None
    return progress >= JACK_UP_PROGRESS_MIN and weight >= JACK_LOADED_WEIGHT_MIN


def cancel_current_move(ip: str, timeout: Optional[float] = None) -> dict:
    return robot_patch(ip, "/chassis/moves/current", {"state": "cancelled"}, timeout=timeout)


JACK_WAIT_SEC = 10  # 잭 업/다운 고정 대기 시간(초)
JACK_IDLE_TIMEOUT = 30  # 잭 다운 후 로봇 idle 대기 최대 시간


def recover_positioning(ip: str, max_wait_sec: int = 15) -> bool:
    """SLAM 위치 복구 — start_global_positioning 호출 후 global_positioning_state 구독.

    1) start_global_positioning (use_barcode+use_base_map_match)
    2) /global_positioning_state WebSocket 구독하여 성공 대기
    3) 실패 시 current-map 재선택 후 재시도

    반환: True=성공, False=실패
    """
    import websocket as _ws
    import json as _json

    def _attempt() -> bool:
        try:
            requests.post(
                f"http://{ip}:{ROBOT_PORT}/services/start_global_positioning",
                json={"use_barcode": True, "use_base_map_match": True},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"[recover] start_global_positioning 호출 실패: {e}")
            return False

        # /global_positioning_state 또는 /slam/state 구독
        deadline = time.time() + max_wait_sec
        try:
            ws = _ws.create_connection(f"ws://{ip}:{ROBOT_PORT}/ws/v2/topics", timeout=3)
            try:
                ws.send(_json.dumps({"enable_topic": "/global_positioning_state"}))
                ws.send(_json.dumps({"enable_topic": "/slam/state"}))
            except Exception:
                pass
            ws.settimeout(2)
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except Exception:
                    continue
                try:
                    pkt = _json.loads(raw)
                except Exception:
                    continue
                topic = pkt.get("topic", "")
                if topic == "/global_positioning_state":
                    state = pkt.get("state", "")
                    if state in ("succeeded", "success", "done"):
                        ws.close()
                        logger.info(f"[recover] 위치 복구 성공 ({ip})")
                        return True
                    if state in ("failed", "error"):
                        ws.close()
                        logger.warning(f"[recover] 위치 복구 실패 state={state}")
                        return False
                elif topic == "/slam/state":
                    if pkt.get("lidar_matched") or pkt.get("reliable"):
                        ws.close()
                        logger.info(f"[recover] SLAM 매칭 확인 ({ip})")
                        return True
            ws.close()
        except Exception as e:
            logger.warning(f"[recover] WebSocket 오류: {e}")
        return False

    # 1차 시도
    if _attempt():
        return True

    # 2차: current-map 재선택 후 재시도
    try:
        cur = requests.get(f"http://{ip}:{ROBOT_PORT}/chassis/current-map", timeout=5)
        if cur.status_code == 200:
            map_id = cur.json().get("id")
            if map_id:
                requests.post(
                    f"http://{ip}:{ROBOT_PORT}/chassis/current-map",
                    json={"map_id": map_id}, timeout=10,
                )
                time.sleep(3)
                logger.info(f"[recover] current-map 재선택 후 재시도")
    except Exception as e:
        logger.warning(f"[recover] current-map 재선택 실패: {e}")

    return _attempt()


# 랙 인식 실패 방어용 — 단계별 회복 후 재시도
ALIGN_MAX_RETRIES = 4
# (2026-09-08 제거) ALIGN_BACKOFF_M / ALIGN_LATERAL_OFFSET_M
#   랙 인식 실패 시 후진 0.5m·측면 0.3m 로 물러나던 값이다.
#   좁은 통로에서 뒤로 물러나는 동작 자체가 위험하다는 현장 판단으로 제거했다.
#   지금은 제자리에서 기다렸다 다시 시도한다.
ALIGN_WAIT_SEC = 10              # 재시도 전 제자리 대기 (초)


# ── 도킹(랙 정렬) 실패 알림 — LGIT 요청 2번 ───────────────────
# 동작(4회 재시도 후 충전소 복귀)은 **바꾸지 않는다.** 알림만 얹는다.
#
# ★ 알림은 배송 모드의 R 픽업에서만 켠다(`notify_ctx` 를 준 호출만).
#   인터랙티브 모드·경유지 순회의 align 에서도 뜨면 "대차를 정위치해 주세요" 가
#   랙과 상관없는 화면에 뜬다.
DOCK_NOTICE_TTL_SEC = 1800.0     # 해제 코드가 어떤 이유로 안 돌아도 30분이면 스스로 사라진다


def _dock_notice_key(ip: str) -> str:
    """로봇 1대당 도킹 알림 1건. 회차가 올라가면 같은 키로 갈아끼운다."""
    return f"dock_fail:{ip}"


def clear_dock_notice(ip: str) -> None:
    """도킹 실패 알림 해제. 정렬 성공·충전소 복귀 완료 시 호출한다."""
    try:
        from app.services import notice_service
        notice_service.clear(_dock_notice_key(ip))
    except Exception:
        pass


def set_dock_outcome_notice(ip: str, poi_id, where: str, text: str) -> None:
    """4회 실패 뒤 **실제로 무엇을 하는지**를 같은 자리에 덮어쓴다.

    `_push_dock_notice(final=True)` 는 "이 작업을 중단합니다" 까지만 말한다.
    그다음 행선지(다음 작업 / 충전소)는 배차 쪽만 알기 때문에 거기서 부른다.
    같은 key 를 쓰므로 줄이 늘지 않고 갈아끼워진다.
    """
    try:
        from app.services import notice_service
        targets = [notice_service.TARGET_CONSOLE]
        if poi_id:
            targets.append(notice_service.poi_target(poi_id))
        head = f"[{where}] " if where else ""
        notice_service.push(notice_service.KIND_DOCK_FAIL_FINAL,
                            f"{head}{text}", targets=targets,
                            key=_dock_notice_key(ip), ttl_sec=DOCK_NOTICE_TTL_SEC)
    except Exception:
        logger.warning(f"[align_retry] {ip}: 도킹 결과 알림 전송 실패(무시)")


def _push_dock_notice(ip: str, ctx: dict, attempt: int, max_retries: int,
                      final: bool = False) -> None:
    """도킹 실패 안내를 콘솔 + 그 위치 태블릿에 띄운다. 실패해도 주행에 영향 없음."""
    try:
        from app.services import notice_service
        targets = [notice_service.TARGET_CONSOLE]
        poi_id = ctx.get("poi_id")
        if poi_id:
            targets.append(notice_service.poi_target(poi_id))
        where = ctx.get("poi_label") or ctx.get("poi_name") or ""
        head = f"[{where}] " if where else ""
        if final:
            # 여기서 목적지를 단정하지 않는다. 재시도를 다 쓴 뒤 로봇이 어디로
            # 갈지는 **대기 예약 유무**에 따라 갈리는데(dispatch_service 가 판단),
            # 이 함수는 그걸 모른다. 예전 문구는 항상 "충전소로 복귀합니다" 라
            # 대기 호출이 있어 다음 작업으로 넘어간 경우 화면이 거짓말을 했다.
            # 실제 목적지는 dispatch_service 가 곧바로 덮어쓴다(같은 key).
            msg = f"{head}⚠ Docking 4회 실패 — 이 작업을 중단합니다."
            kind = notice_service.KIND_DOCK_FAIL_FINAL
        else:
            msg = (f"{head}⚠ Docking에 실패했습니다. 대차를 정위치해 주세요. "
                   f"({attempt}/{max_retries}회)")
            kind = notice_service.KIND_DOCK_FAIL
        notice_service.push(kind, msg, targets=targets,
                            key=_dock_notice_key(ip), ttl_sec=DOCK_NOTICE_TTL_SEC)
    except Exception:
        logger.warning(f"[align_retry] {ip}: 도킹 실패 알림 전송 실패(무시)")


def align_with_retry(ip: str, x: float, y: float, ori: float = 0,
                     max_retries: int = ALIGN_MAX_RETRIES,
                     notify_ctx: Optional[dict] = None) -> dict:
    """align_with_rack 재시도 — "정말 랙이 없는 게 아닌 한 인식되도록" 방어 로직.

    단계:
      1차) 그대로 시도
      2차) 재로컬화 후 시도       — 위치 추정 오류 대응 (로봇은 움직이지 않는다)
      3차) 제자리 대기 후 재시도
      4차) 제자리 대기 후 재시도
    모든 단계 실패 시 마지막 결과 반환. cancel(중지)은 즉시 반환.

    ★ 2026-09-08 현장 요구 — **랙을 못 찾아도 뒤로 물러나지 않는다.**
      예전 3차 후진 0.5m·4차 측면 0.3m 우회를 제거했다.
      다 쓰고도 실패하면 호출부가 '랙 없음' 으로 처리한다 —
      대기 예약이 있으면 그 작업으로 넘어가고, 없으면 충전소로 복귀한다(기존 그대로).
    """
    last_result: dict = {}
    for attempt in range(1, max_retries + 1):
        # 같은 시도 내에서 paused/resumed 로 인한 cancel 은 재시도 (attempt 카운트 X)
        while True:
            _check_stop(ip)
            _wait_if_paused(ip)
            try:
                move_id = create_move(ip, "align_with_rack", x, y, ori)
                result = wait_move(ip, move_id, timeout=120)
            except RuntimeError:
                raise
            except Exception as e:
                result = {"state": "failed", "fail_message": str(e)}
            # paused 로 인한 cancel 이면 resume 대기 후 같은 시도 그대로 다시
            if result.get("state") == "cancelled" and result.get("_paused_cancel"):
                logger.info(f"[align_retry] {ip}: pause/resume — 같은 시도 재실행")
                continue
            break
        last_result = result

        state = result.get("state", "")
        if state == "succeeded":
            if attempt > 1:
                logger.info(f"[align_retry] {ip}: 재시도 {attempt}회차에 성공")
                update_job_status(ip, message=f"랙 정렬 재시도 {attempt}회차에 성공")
            if notify_ctx:
                clear_dock_notice(ip)      # 성공 = 즉시 해제
            return result
        if state == "cancelled":
            return result

        fail_msg = result.get("fail_message") or state
        logger.warning(f"[align_retry] {ip}: 시도 {attempt}/{max_retries} 실패 — {fail_msg}")
        # 실패 회차를 화면에 알린다(같은 키로 갈아끼우므로 줄이 쌓이지 않는다)
        if notify_ctx:
            _push_dock_notice(ip, notify_ctx, attempt, max_retries,
                              final=(attempt >= max_retries))

        if attempt >= max_retries:
            break

        # 단계별 회복 액션
        _check_stop(ip)
        try:
            if attempt == 1:
                # 2차 시도 전: 재로컬화만
                update_job_status(ip, message="랙 인식 실패 — 위치 재보정 후 재시도")
                _interruptible_sleep(ip, 2)
                try:
                    recover_positioning(ip, max_wait_sec=10)
                except Exception:
                    pass
                _interruptible_sleep(ip, 2)
            else:
                # 3·4차: **제자리에서 대기 후 재시도.** 로봇을 움직이지 않는다.
                #   (2026-09-08 현장 요구 — 후진 0.5m·측면 우회 0.3m 제거)
                #   좁은 통로에서 뒤로 물러나는 동작 자체가 위험하다는 판단이다.
                update_job_status(
                    ip,
                    message=f"랙 인식 실패 — 제자리 대기 후 재시도 ({attempt + 1}회차)")
                logger.info(f"[align_retry] {ip}: 제자리에서 {ALIGN_WAIT_SEC}초 대기 후 재시도")
                _interruptible_sleep(ip, ALIGN_WAIT_SEC)
        except RuntimeError:
            raise

    return last_result


def wait_robot_idle(ip: str, timeout: int = JACK_IDLE_TIMEOUT):
    """로봇이 idle(현재 이동 없음) 상태가 될 때까지 폴링"""
    deadline = time.time() + timeout
    time.sleep(3)  # 최소 대기
    while time.time() < deadline:
        try:
            r = requests.get(robot_url(ip, "/chassis/moves/current"), timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                # Not found = 현재 이동 없음 = idle
                return True
            data = r.json()
            state = data.get("state", "")
            if state in ("succeeded", "failed", "cancelled", ""):
                return True
        except Exception:
            pass
        time.sleep(1)
    logger.warning(f"[jack] 로봇 idle 대기 타임아웃 ({timeout}초)")
    return False


# ── 잭킹 작업 실행 ──

def run_jack_job(
    ip: str,
    pickup: dict,
    dropoff: dict,
    standby: Optional[dict] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """잭킹 작업 동기 실행
    pickup/dropoff/standby: {"name": str, "x": float, "y": float, "ori": float}
    on_status(status, message): 상태 변경 콜백
    반환: {"status": "done"|"error", "message": str}
    """
    def _notify(status: str, message: str):
        if on_status:
            on_status(status, message)
        logger.info(f"[jack-job] {status}: {message}")

    def _fail(msg: str) -> dict:
        """에러 복구 + 에러 리턴"""
        _notify("error", msg)
        try:
            cancel_current_move(ip)
        except Exception:
            pass
        try:
            jack_down(ip)
        except Exception:
            pass
        return {"status": "error", "message": msg}

    try:
        # 1) align_with_rack (재시도 포함)
        _notify("aligning", f"픽업 위치({pickup['name']})로 랙 정렬 이동 중...")
        result = align_with_retry(ip, pickup["x"], pickup["y"], pickup.get("ori", 0))
        if result["state"] != "succeeded":
            msg = f"랙 정렬 실패: {result.get('fail_message', result['state'])}"
            _notify("error", msg)
            return _fail(msg)

        # 2) 잭 업
        _notify("jacking_up", "잭 올리는 중...")
        jack_up(ip)
        time.sleep(JACK_WAIT_SEC)

        # 3) to_unload_point
        _notify("moving_to_dropoff", f"드롭오프 위치({dropoff['name']})로 이동 중...")
        result = safe_move(ip, "to_unload_point", dropoff["x"], dropoff["y"], dropoff.get("ori", 0), timeout=120)
        if result["state"] != "succeeded":
            msg = f"드롭오프 이동 실패: {result.get('fail_message', result['state'])}"
            _notify("error", msg)
            return _fail(msg)

        # 4) 잭 다운
        _notify("jacking_down", "잭 내리는 중...")
        jack_down(ip)
        time.sleep(JACK_WAIT_SEC)

        # 5) 대기장소 복귀
        if standby:
            _notify("returning", f"대기장소({standby['name']})로 복귀 중...")
            result = safe_move(ip, "standard", standby["x"], standby["y"], standby.get("ori", 0))
            if result["state"] != "succeeded":
                msg = f"복귀 실패: {result.get('fail_message', result['state'])}"
                _notify("error", msg)
                return _fail(msg)

        summary = f"완료: {pickup['name']} → {dropoff['name']} → {standby['name'] if standby else '정지'}"
        _notify("done", summary)
        return {"status": "done", "message": summary}

    except Exception as e:
        msg = f"오류: {str(e)}"
        _notify("error", msg)
        return _fail(msg)


def run_route_job(
    ip: str,
    waypoints: list[dict],
    on_status: Optional[Callable[[str, str], None]] = None,
    manual_confirm: bool = False,
    skip_standby_pickup: bool = False,
    skip_standby_return: bool = False,
    area_id: int | None = None,
    work_mode: str = "rack_pickup",
    robot_id: int | None = None,
    start_jacked: bool = False,
    end_jacked: bool = False,
) -> dict:
    """경로 기반 작업 실행
    waypoints: [{"name", "x", "y", "ori", "waypoint_type", "poi_type", "wait_sec"}, ...]
    waypoint_type: pickup / dropoff / standby / charging
    poi_type: jack / standby / charging / general 등
    robot_id 가 전달되면 양보 후 자동 재시도용 메타가 등록됨.
    """
    # 좀비 entry 정리 — 이전 작업이 비정상 종료되어 같은 ip 에 남아있을 수 있는 전역 상태 청소.
    # (정상 작업은 finally 에서 정리되지만, 강제 종료/크래시 시 남을 수 있음)
    _stop_flags.pop(ip, None)
    _paused_flags.pop(ip, None)
    _job_status.pop(ip, None)
    _next_poi.pop(ip, None)
    _confirm_events.pop(ip, None)
    _active_route_jobs.pop(ip, None)
    # 자동 재시도용 메타데이터 등록 + zone guard 라우팅
    if robot_id is not None:
        _active_route_jobs[ip] = {
            "robot_ip": ip,
            "robot_id": robot_id,
            "waypoints": waypoints,
            "manual_confirm": manual_confirm,
            "skip_standby_pickup": skip_standby_pickup,
            "skip_standby_return": skip_standby_return,
            "area_id": area_id,
            "work_mode": work_mode,
        }
        _running_robot_id_by_ip[ip] = robot_id
    total_steps = len(waypoints)
    route_names = " → ".join(w["name"] for w in waypoints)
    # 강제 종료 분기용 work_mode 노출 (프론트가 job-status 로 읽음)
    update_job_status(ip, work_mode=work_mode)

    def _notify(status: str, message: str, step: int = 0):
        if on_status:
            on_status(status, message)
        logger.info(f"[route-job] {status}: {message}")
        # route / total_steps / started_at 은 머지로 보존 — 추가 목적지/복귀 단계에서
        # 외부 update_job_status 로 갱신한 값이 _notify 다음 호출에 덮어쓰이는 버그 방지.
        update_job_status(ip,
            status=status,
            message=message,
            current_step=step,
        )

    def _fail(msg: str) -> dict:
        """에러 복구 + 에러 리턴"""
        _notify("error", msg)
        clear_job_status(ip)
        try:
            cancel_current_move(ip)
        except Exception:
            pass
        try:
            jack_down(ip)
        except Exception:
            pass
        from app.crud.activity_log import log_activity as _log
        _log("robot", "task_error", f"작업 실패: {msg}", source="jack_service")
        return {"status": "error", "message": msg}

    jacked_up = bool(start_jacked)  # 잭 올림 상태 추적 (반복 사이클 사이 유지용)
    # start_jacked=True 면 standby 픽업 자동 skip (이미 랙 들고 있는 상태)
    if start_jacked:
        skip_standby_pickup = True
    update_job_status(ip, status="started", route=route_names, current_step=0,
                      total_steps=total_steps, started_at=time.time(), message="작업 시작")

    # 활동 로그 기록
    from app.crud.activity_log import log_activity
    log_activity("robot", "task_start", f"작업 시작: {route_names}", source="jack_service")

    _move_with_rack = "to_unload_point"

    try:
        # ── 위치 보정 (start_global_positioning + /global_positioning_state 대기) ──
        _notify("aligning", "위치 보정 중...", 0)
        if recover_positioning(ip, max_wait_sec=15):
            logger.info(f"[route-job] 위치 보정 완료 ({ip})")
        else:
            logger.warning(f"[route-job] 위치 보정 미확인 (진행 계속)")

        # ── work_mode: simple_move / delivery_no_rack 분기 ──
        if work_mode in ("simple_move", "delivery_no_rack"):
            for i, wp in enumerate(waypoints):
                name = wp["name"]
                wtype = wp["waypoint_type"]
                wait_sec = wp.get("wait_sec", 0)

                # 이동
                _notify("moving", f"[{i+1}/{total_steps}] {name} 이동 중...", i+1)
                result = safe_move(ip, "standard", wp["x"], wp["y"], wp.get("ori", 0), timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 이동 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "move_error", msg, source="jack_service")
                    return _fail(msg)

                # delivery_no_rack: 픽업에서 잭 업, 드롭오프에서 잭 다운
                if work_mode == "delivery_no_rack":
                    if wtype == "pickup":
                        _notify("jacking_up", f"{name} 잭 올리는 중...", i+1)
                        jack_up(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)
                    elif wtype == "dropoff":
                        _notify("jacking_down", f"{name} 잭 내리는 중...", i+1)
                        jack_down(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)

                # 대기 / 확인 버튼 (마지막 포인트는 스킵 — 다음/복귀 선택으로 바로 진입)
                is_last = (i == len(waypoints) - 1)
                if manual_confirm and not is_last:
                    _notify("waiting_confirm", f"{name} 도착. 출발 버튼을 눌러주세요", i+1)
                    if not wait_for_confirm(ip):
                        return _fail("출발 확인 타임아웃")
                    _check_stop(ip)
                elif not manual_confirm and wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

            # ── 마지막 포인트 후: 다음 포인트 / 복귀 선택 (수동만) ──
            if manual_confirm:
                last_wp = waypoints[-1]
                current_wp = last_wp
                while True:
                    _check_stop(ip)
                    update_job_status(ip,
                        status="waiting_next_or_return",
                        message="다음 포인트를 선택하거나 복귀 버튼을 눌러주세요",
                        route=f"{current_wp['name']} → ?",
                        current_step=0, total_steps=1,
                        started_at=_job_status.get(ip, {}).get("started_at", time.time()),
                    )
                    if on_status:
                        on_status("waiting_next_or_return", "다음 포인트를 선택하거나 복귀 버튼을 눌러주세요")
                    if not wait_for_confirm(ip):
                        return _fail("선택 타임아웃")
                    _check_stop(ip)
                    next_poi = get_next_poi(ip)
                    if next_poi is None or next_poi == "return":
                        break
                    # delivery_no_rack: 잭업 → 이동 → 잭다운 사이클 (simple_move 는 그대로 이동만)
                    is_delivery = (work_mode == "delivery_no_rack")
                    total = 3 if is_delivery else 1
                    update_job_status(ip,
                        route=f"{current_wp['name']} → {next_poi['name']}",
                        current_step=0, total_steps=total,
                    )

                    if is_delivery:
                        # 잭업 (현재 위치에서)
                        _notify("jacking_up", f"{current_wp['name']} 잭 올리는 중...", 1)
                        jack_up(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)

                    # 이동
                    _notify("moving", f"{next_poi['name']} 이동 중...", 2 if is_delivery else 1)
                    result = safe_move(ip, "standard", next_poi["x"], next_poi["y"], next_poi.get("ori", 0), timeout=120)
                    if result["state"] != "succeeded":
                        msg = f"{next_poi['name']} 이동 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    if is_delivery:
                        # 잭다운 (도착 후)
                        _notify("jacking_down", f"{next_poi['name']} 잭 내리는 중...", 3)
                        jack_down(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)

                    current_wp = next_poi

            _notify("done", f"완료: {route_names}", total_steps)
            clear_job_status(ip)
            log_activity("robot", "task_complete", f"작업 완료: {route_names}", source="jack_service")
            return {"status": "done", "message": f"완료: {route_names}"}

        # ── 시작: 랙 위치 (standby POI) 에서 랙 픽업 (첫 회차만) ──
        # 랙 위치(standby) 와 작업 위치(jack) 는 별개. 충전소 → 랙 위치 → 작업 위치 → 랙 위치 → 충전소 흐름.
        standby_poi = _get_standby_poi(area_id, robot_ip=ip)
        if standby_poi and not skip_standby_pickup:
            sname = standby_poi["name"]
            _check_stop(ip)
            _notify("aligning", f"랙 위치({sname})에서 랙 픽업 중...", 0)
            result = align_with_retry(ip, standby_poi["x"], standby_poi["y"], standby_poi.get("ori", 0))
            if result["state"] == "succeeded":
                _notify("jacking_up", f"랙 위치({sname}) 잭 올리는 중...", 0)
                jack_up(ip)
                _interruptible_sleep(ip, JACK_WAIT_SEC)
                jacked_up = True
            else:
                msg = f"랙 위치({sname}) 랙 픽업 실패: {result.get('fail_message', '')}"
                return _fail(msg)

        for i, wp in enumerate(waypoints):
            name = wp["name"]
            wtype = wp["waypoint_type"]
            ptype = wp.get("poi_type", "general")
            wait_sec = wp.get("wait_sec", 0)

            if wtype == "pickup":
                if jacked_up:
                    # 잭 올린 상태 → 픽업 위치로 이동 → 잭 다운 → 물건 올림 → 잭 업
                    _notify("moving_to_dropoff", f"[{i+1}/{total_steps}] {name} 랙 배달 중...", i+1)
                    result = safe_move(ip, _move_with_rack, wp["x"], wp["y"], wp.get("ori", 0), timeout=120)
                    if result["state"] != "succeeded":
                        msg = f"{name} 이동 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    _notify("jacking_down", f"{name} 잭 내리는 중...", i+1)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = False

                    # 수동: 출발 버튼 누르면 자동으로 잭 업 → 출발 / 자동: wait_sec 대기
                    if manual_confirm:
                        _notify("waiting_confirm", f"{name} 물건 적재 후 출발 버튼을 눌러주세요", i+1)
                        if not wait_for_confirm(ip):
                            return _fail("출발 확인 타임아웃 (5분)")
                        _check_stop(ip)
                    elif wait_sec > 0:
                        _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                        _interruptible_sleep(ip, wait_sec)

                    # 잭 업 → 바로 출발 (출발 대기 없음)
                    _notify("aligning", f"{name} 랙 재정렬 중...", i+1)
                    result = align_with_retry(ip, wp["x"], wp["y"], wp.get("ori", 0))
                    if result["state"] != "succeeded":
                        msg = f"{name} 랙 재정렬 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    _notify("jacking_up", f"{name} 잭 올리는 중...", i+1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = True
                else:
                    # 잭이 내려간 상태 → align_with_rack로 랙 픽업
                    _notify("aligning", f"[{i+1}/{total_steps}] {name} 랙 정렬 이동 중...", i+1)
                    result = align_with_retry(ip, wp["x"], wp["y"], wp.get("ori", 0))
                    if result["state"] != "succeeded":
                        msg = f"{name} 랙 정렬 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    _notify("jacking_up", f"{name} 잭 올리는 중...", i+1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = True

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

            elif wtype == "dropoff":
                # 드롭오프: 이동 → jack_down → 대기 → (마지막이고 end_jacked=False 가 아니면) align + jack_up
                _notify("moving_to_dropoff", f"[{i+1}/{total_steps}] {name} 드롭오프 이동 중...", i+1)
                result = safe_move(ip, _move_with_rack, wp["x"], wp["y"], wp.get("ori", 0), timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 드롭오프 이동 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "move_error", msg, source="jack_service")
                    return _fail(msg)

                _notify("jacking_down", f"{name} 잭 내리는 중...", i+1)
                jack_down(ip)
                _interruptible_sleep(ip, JACK_WAIT_SEC)
                jacked_up = False

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

                # 다음 waypoint 가 있거나, 마지막이지만 end_jacked=True (다음 회차에서 잭업 상태 필요) → 다시 잭업
                is_last = (i == len(waypoints) - 1)
                need_jack_up_again = (not is_last) or end_jacked
                if need_jack_up_again:
                    _notify("aligning", f"{name} 다음 단계 위해 랙 재정렬 중...", i+1)
                    align_result = align_with_retry(ip, wp["x"], wp["y"], wp.get("ori", 0))
                    if align_result["state"] != "succeeded":
                        msg = f"{name} 재정렬 실패: {align_result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)
                    _notify("jacking_up", f"{name} 잭 다시 올리는 중...", i+1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = True

            elif ptype == "charging" or wtype == "charging":
                # 충전소: 사전 접근 POI("<charger_name>-1") 가 있으면 그쪽으로 먼저 이동 후 charge.
                # 없으면 충전소 POI 좌표 그대로 standard → charge.
                _notify("charging", f"[{i+1}/{total_steps}] {name} 충전소 접근 중...", i+1)
                cx, cy = wp["x"], wp["y"]
                cyaw = wp.get("ori", 0)
                # 사전 접근 POI 검색 (같은 영역의 활성 맵에서 이름 매칭)
                _ap = None
                _db_ap = None
                try:
                    from app.database import SessionLocal as _SL
                    from app.models.map import MapPOI as _MP, RobotMap as _RM
                    from app.models.robot import Robot as _RB
                    _db_ap = _SL()
                    _r = _db_ap.query(_RB).filter(_RB.ip_address == ip).first()
                    _map_q = None
                    if _r and _r.area_id:
                        _map_q = _db_ap.query(_RM).filter(
                            _RM.area_id == int(_r.area_id), _RM.is_active == True
                        ).order_by(_RM.id.desc()).first()
                    if _map_q:
                        _apoi = _db_ap.query(_MP).filter(
                            _MP.map_id == _map_q.id,
                            _MP.name == f"{name}-1",
                            _MP.is_active == True,
                        ).first()
                        if _apoi and _apoi.world_x is not None:
                            _ap = {
                                "name": _apoi.name,
                                "x": _apoi.world_x,
                                "y": _apoi.world_y,
                                "ori": _apoi.angle if _apoi.angle is not None else cyaw,
                            }
                except Exception as e:
                    logger.warning(f"[charge-approach] {ip} 사전 접근 POI 조회 예외: {e}")
                finally:
                    if _db_ap is not None:
                        _db_ap.close()

                if _ap:
                    sx, sy, syaw = _ap["x"], _ap["y"], _ap["ori"]
                    _label = f"사전 접근({_ap['name']})"
                else:
                    sx, sy, syaw = cx, cy, cyaw
                    _label = "사전 접근"

                # 1단계: 사전 접근 — best-effort. 실패해도 charge 단계로 진행.
                try:
                    _std_id = create_move(ip, "standard", sx, sy, syaw)
                    _std_res = wait_move(ip, _std_id, timeout=60)
                    if _std_res.get("state") != "succeeded":
                        logger.warning(f"[charge-approach] {ip} {_label} 실패({_std_res.get('fail_message')}) — 직접 도킹")
                except RuntimeError:
                    raise
                except Exception as e:
                    logger.warning(f"[charge-approach] {ip} {_label} 예외: {e} — 직접 도킹")
                time.sleep(2)
                # 2단계: 도킹
                _notify("charging", f"[{i+1}/{total_steps}] {name} 충전소 도킹 중...", i+1)
                move_id = create_move(ip, "charge", cx, cy, cyaw, charge_retry_count=3)
                result = wait_move(ip, move_id, timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 충전 도킹 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "dock_error", msg, source="jack_service")
                    return _fail(msg)

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

            else:
                # 대기/일반: standard 이동
                _notify("moving", f"[{i+1}/{total_steps}] {name} 이동 중...", i+1)
                result = safe_move(ip, "standard", wp["x"], wp["y"], wp.get("ori", 0), timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 이동 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "move_error", msg, source="jack_service")
                    return _fail(msg)

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

        # 마지막 드롭오프 후: 다음 포인트 / 복귀 선택 루프 (수동) 또는 바로 복귀 (자동)
        # 복귀 대상은 랙 위치 (standby POI).
        if jacked_up is False and not skip_standby_return:
            _check_stop(ip)
            standby_poi = _get_standby_poi(area_id, robot_ip=ip)
            last_dropoff_wp = None
            for wp in reversed(waypoints):
                if wp["waypoint_type"] == "dropoff":
                    last_dropoff_wp = wp
                    break

            # ── 수동: 다음 포인트 / 복귀 루프 ──
            if manual_confirm and standby_poi and last_dropoff_wp:
                current_wp = last_dropoff_wp
                extra_step = 0
                while True:
                    _check_stop(ip)
                    update_job_status(ip,
                        status="waiting_next_or_return",
                        message="다음 포인트를 선택하거나 복귀 버튼을 눌러주세요",
                        route=f"{current_wp['name']} → ?",
                        current_step=0, total_steps=1,
                        started_at=_job_status.get(ip, {}).get("started_at", time.time()),
                    )
                    if on_status:
                        on_status("waiting_next_or_return", "다음 포인트를 선택하거나 복귀 버튼을 눌러주세요")
                    if not wait_for_confirm(ip):
                        return _fail("선택 타임아웃")
                    _check_stop(ip)

                    next_poi = get_next_poi(ip)
                    if next_poi is None or next_poi == "return":
                        break

                    # 경로 업데이트
                    extra_step += 1
                    new_route = f"{current_wp['name']} → {next_poi['name']}"
                    update_job_status(ip, route=new_route, current_step=0, total_steps=2)

                    # 현재 위치에서 잭 업 → 다음 포인트 → 잭 다운
                    update_job_status(ip, status="aligning", message=f"{current_wp['name']} 랙 재정렬 중...", current_step=0)
                    _notify("aligning", f"{current_wp['name']} 랙 재정렬 중...", 0)
                    result = align_with_retry(ip, current_wp["x"], current_wp["y"], current_wp.get("ori", 0))
                    if result["state"] != "succeeded":
                        msg = f"랙 재정렬 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    update_job_status(ip, status="jacking_up", message="잭 올리는 중...", current_step=1)
                    _notify("jacking_up", "잭 올리는 중...", 1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

                    update_job_status(ip, status="moving_to_dropoff", message=f"{next_poi['name']} 이동 중...", current_step=1)
                    _notify("moving_to_dropoff", f"{next_poi['name']} 이동 중...", 1)
                    result = safe_move(ip, _move_with_rack, next_poi["x"], next_poi["y"], next_poi.get("ori", 0), timeout=120)
                    if result["state"] != "succeeded":
                        msg = f"{next_poi['name']} 이동 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    update_job_status(ip, status="jacking_down", message=f"{next_poi['name']} 잭 내리는 중...", current_step=2)
                    _notify("jacking_down", f"{next_poi['name']} 잭 내리는 중...", 2)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    current_wp = next_poi
                    log_activity("robot", "dropoff", f"드롭오프: {next_poi['name']}", source="jack_service")

                # 복귀
                sname = standby_poi["name"]
                update_job_status(ip, route=f"{current_wp['name']} → {sname}", current_step=0, total_steps=2)
                _notify("aligning", f"랙 위치로 복귀 위해 재정렬 중...", 0)
                result = align_with_retry(ip, current_wp["x"], current_wp["y"], current_wp.get("ori", 0))
                if result["state"] == "succeeded":
                    _notify("jacking_up", "랙 위치로 복귀 위해 잭 올리는 중...", total_steps)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

                    _notify("moving", f"랙 위치({sname})로 복귀 중...", total_steps)
                    safe_move(ip, _move_with_rack, standby_poi["x"], standby_poi["y"], standby_poi.get("ori", 0), timeout=120)

                    _notify("jacking_down", f"랙 위치({sname}) 잭 내리는 중...", total_steps)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

            # ── 자동: 바로 복귀 ──
            elif standby_poi and last_dropoff_wp:
                sname = standby_poi["name"]
                _notify("aligning", f"랙 위치로 복귀 위해 재정렬 중...", total_steps)
                result = align_with_retry(ip, last_dropoff_wp["x"], last_dropoff_wp["y"], last_dropoff_wp.get("ori", 0))
                if result["state"] == "succeeded":
                    _notify("jacking_up", "랙 위치로 복귀 위해 잭 올리는 중...", total_steps)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

                    _notify("moving", f"랙 위치({sname})로 복귀 중...", total_steps)
                    safe_move(ip, _move_with_rack, standby_poi["x"], standby_poi["y"], standby_poi.get("ori", 0), timeout=120)

                    _notify("jacking_down", f"랙 위치({sname}) 잭 내리는 중...", total_steps)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

        _notify("done", f"완료: {route_names}", total_steps)
        clear_job_status(ip)
        log_activity("robot", "task_complete", f"작업 완료: {route_names}", source="jack_service")
        return {"status": "done", "message": f"완료: {route_names}"}

    except Exception as e:
        return _fail(f"오류: {str(e)}")
    finally:
        _active_route_jobs.pop(ip, None)
        _running_robot_id_by_ip.pop(ip, None)
        if robot_id is not None:
            try:
                from app.services import zone_lock as _zl
                _zl.release_all_by_robot(robot_id)
            except Exception:
                pass
