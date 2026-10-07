"""인터랙티브 배차 (VESA 모드) — 워커 스레드 + Event 기반 next/end

운영 흐름:
  start  → standby에서 align_with_rack → jack_up
         → first POI로 이동 → 도착 (잭 유지)
  next   → target POI로 이동 → 도착 (잭 유지)
  end    → standby로 이동 → jack_down → 충전소 도킹

핵심:
  - 로봇당 워커 스레드 1개 (동시 세션 1개 보장)
  - next/end 명령은 threading.Event 로 워커에 전달
  - 종료(returning) 상태가 되면 그 이후 next/end 명령은 모두 무시
  - 서버 재시작 시 awaiting_next 세션은 워커만 재기동 (로봇은 이미 도착 상태)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.database import SessionLocal
from app.models.dispatch import DispatchSession
from app.models.map import MapPOI, RobotMap
from app.models.robot import Robot, RobotStatus
from app.crud import dispatch as dispatch_crud
from app.services import jack_service
from app.services import notice_service
from app.services import waypoint_route
from app.services.thread_utils import safe_thread
# 로그·알림 문구에 쓸 **표시용** 이름. 조회 키로는 절대 쓰지 않는다
# (job_points 매핑과 진입점 "<이름>-1" 은 전부 원래 이름 기준이다).
from app.services.poi_label import label_for as _poi_label

logger = logging.getLogger(__name__)

# 잭 업/다운 후 안정화 대기 (jack_service의 JACK_WAIT_SEC와 동일 기준)
JACK_SETTLE_SEC = 8

# 접근 경유지가 랙의 '진입축' 위에 있다고 볼 각도 허용치.
# 진입축 = 랙 POI 의 각도(ori) 가 만드는 직선. 그 위(앞이든 뒤든)에 접근점이 있으면
# 도착 자세를 랙 각도로 맞춰 두는 게 이득이다 — 아래 `_approach_before_align` 참조.
APPROACH_AXIS_TOL_RAD = math.radians(45)

# 접근점을 생략할 '목표 코앞' 거리(m).
# 로봇이 목표에서 이 거리 안이고, 접근점이 목표에서 **로봇보다 멀면** 접근
# 이동을 건너뛰고 바로 정렬한다 — 아래 `_approach_before_align` 참조.
# 3.5 인 이유: 공장 실측(2026-09-09) J1↔R2 직선 3.02 m 는 생략하고,
#   접근점을 정상적으로 써야 하는 C1→R1 4.48 m 는 그대로 두는 경계.
APPROACH_SKIP_MAX_M = 3.5

# 진입점("<작업지점>-1") 으로 들어가는 마지막 구간의 이동 방식.
# 경유지 체인 주행은 detour_tolerance=0 (준 경로대로, 회피 없이 정지) 그대로 두고,
# **마지막 경유지 → 진입점** 구간만 로봇 자율 주행(standard)으로 보낸다.
# standard 는 로봇이 스스로 경로를 만들고 장애물을 **회피**한다.
# 이렇게 나누는 이유 — 통로에서는 지정한 길로만 다녀야 하지만, 작업지점 앞
# 마지막 몇 m 는 주차된 다른 랙을 피해 들어가야 하기 때문이다.
ENTRY_AUTONOMOUS_LAST_LEG = True

# 재시작 복구 시 '로봇이 이 POI 에 도착해 있다' 로 인정할 최대 거리(m).
# 이 안이면 실측 포즈로 도착지를 확정하고, 넘으면 목적지 가정으로 폴백한다.
RECOVER_POSE_MATCH_M = 1.5

# 재시작 복구 중 로봇에 거는 조회/명령 타임아웃(초).
# recover_on_startup 은 startup 이벤트에서 동기 실행되어 이 시간만큼 서버 기동이 늦어진다.
# 로봇이 꺼져 있거나 응답이 없을 때(재부팅 직후 등) 기본 타임아웃(WS 6s, HTTP 15s)을 그대로 쓰면
# 세션 하나당 최대 25초가 걸린다. 복구는 실패해도 안전한 폴백이 있으므로 짧게 끊는다.
RECOVER_ROBOT_TIMEOUT = 5.0


@dataclass
class _Worker:
    robot_id: int
    session_id: int
    robot_ip: str
    area_id: Optional[int]
    with_rack: bool = True   # (리프팅) True=렉 픽업 후 작업, False=렉 없이
    use_jack: bool = True    # False=서빙 로봇 (잭 동작 전혀 없음, 단순 이동만)
    thread: Optional[threading.Thread] = None
    next_event: threading.Event = field(default_factory=threading.Event)       # 경유지 등록+출발 신호
    confirm_event: threading.Event = field(default_factory=threading.Event)    # 로봇 태블릿 [확인] 신호
    end_flag: bool = False
    abort_flag: bool = False   # 강제 정리 — 복귀 시퀀스 없이 세션만 종료 (로봇 이동/잭 동작 없음)
    # [전체 강제 종료] 전용 — 이 워커가 죽을 때 **대기 예약을 이어받지 않는다** (2026-09-23).
    #   현장 정의: 전체 강제 종료 = 현재 작업 + 예약된 작업 **전부** 종료 → 충전소 복귀.
    #   종전에는 죽는 워커의 finally 가 try_fulfill_reservations() 를 불러
    #   방금 취소한 작업 대신 **대기 예약을 즉시 새로 시작**했다.
    #   (2026-09-23 17:04:35 실측 — R2 작업을 끊었더니 0.2초 만에 J1 예약을 물고 출발)
    no_takeover: bool = False
    pending_route: Optional[list[int]] = None  # 콘솔이 등록한 경유지 큐 (출발 시 워커가 소비)
    # 충전소 복귀 도중 이어받을 작업 (j_poi_id, reservation_id).
    # `_stop_flags` 는 사용자 중지에도 쓰이므로, 이 값이 있을 때만 '예약 이어받기로 인한
    # 이동 중단' 으로 해석해 둘을 구분한다.
    divert_to: Optional[tuple[int, int]] = None


# 워커 레지스트리 — robot_id 기준
_workers: dict[int, _Worker] = {}

# 강제 종료 후속 동작(랙 내려놓기 → 충전소)이 도는 로봇 (2026-09-23).
# 워커가 아니라 별도 스레드라 워커 레지스트리에 안 잡힌다. 그동안 로봇은
# 바쁜데 화면에는 이유가 안 보여서 24초 동안 [예약하기] 만 떴다.
# 라우터가 스레드로 돌리는 수동 이동(충전소 복귀·강제 복귀) (2026-09-23).
# 워커도 후속 동작도 아니라서 가용 판정에 안 잡혔다 — 그 사이 호출이 들어오면
# 두 명령이 같은 로봇을 동시에 몰게 된다.
_manual_move_robots: set[int] = set()
_manual_move_lock = threading.Lock()


def manual_move_begin(robot_id: int, what: str) -> None:
    with _manual_move_lock:
        _manual_move_robots.add(robot_id)
    logger.info("[dispatch] 수동 이동 시작 — robot=%s (%s)", robot_id, what)


def manual_move_end(robot_id: int, what: str = "") -> None:
    with _manual_move_lock:
        _manual_move_robots.discard(robot_id)
    logger.info("[dispatch] 수동 이동 종료 — robot=%s %s", robot_id, what)


_followup_robots: set[int] = set()
_followup_cancel: set[int] = set()     # 사용자 명령으로 중단 요청된 후속 동작
_followup_lock = threading.Lock()


_followup_to_charger: set[int] = set()   # 중단 뒤 충전소로 보낼 로봇


def cancel_followup(robot_id: int, why: str = "사용자 명령",
                    then_charge: bool = False) -> bool:
    """**돌고 있는 강제 종료 후속 동작을 중단시킨다** (2026-09-23).

    현장 정의 — 후속 동작 중에 사용자가 다른 명령을 내리면 **사용자가 이긴다.**
    사람이 개입하는 건 뭔가 잘못됐을 때이므로 그게 우선이다.

    2026-09-23 18:39 실측 — 이게 없어서 이런 일이 났다.
      18:39:04  후속 동작이 '대기 예약 J2 의 R 지점 R2' 를 목적지로 잡음
      18:39:31  사용자가 그 예약을 취소  → 후속 동작은 못 본다
      18:39:35  사용자가 충전소 복귀      → 그 이동이 곧바로 cancelled
      18:40:29  후속 동작이 이겨서 R2 도착. 랙도 없는데 들어가 섰다

    돌고 있지 않으면 아무 일도 하지 않는다(False).
    """
    with _followup_lock:
        if robot_id not in _followup_robots:
            return False
        _followup_cancel.add(robot_id)
        # 충전소 복귀처럼 **뒤이어 다른 명령이 로봇을 움직이는** 경우가 아니면
        # 그냥 중단만 하면 로봇이 통로 한가운데 서 버린다. 그때는 충전소로 보낸다.
        if then_charge:
            _followup_to_charger.add(robot_id)
    logger.warning("[dispatch] 후속 동작 중단 요청 — robot=%s (%s)", robot_id, why)
    try:
        r = _load_robot(robot_id)
        if r and r.ip_address:
            # 여기도 사용자가 세우는 경우다 — 급정거하지 않게 먼저 감속시킨다
            # (force_clear 와 같은 이유. 그 주석 참조)
            try:
                jack_service.decelerate_to_stop(r.ip_address, why="후속 동작 중단")
            except Exception as e2:
                logger.warning("[dispatch] 후속 동작 중단 — 감속 정지 실패(그대로 취소): %s", e2)
            # 순서 주의 — 중지 플래그를 **취소보다 먼저**. 그 사이에 틈이 있으면
            # safe_move 가 취소를 재시도로 해석해 같은 이동을 다시 발행한다
            # (force_clear 의 같은 자리 주석에 실측 기록이 있다)
            jack_service.stop_robot_job(r.ip_address)   # 진행 중 이동에서 빠져나오게
            jack_service.cancel_current_move(r.ip_address, timeout=3)
            try:
                jack_service.apply_state_speed(r.ip_address)    # 속도 복구 — 맨 마지막
            except Exception as e2:
                logger.warning("[dispatch] 후속 동작 중단 — 속도 복구 실패: %s", e2)
    except Exception as e:
        logger.warning("[dispatch] 후속 동작 중단 — 로봇에 전달 실패(무시): %s", e)
    return True


def _followup_cancelled(robot_id: int) -> bool:
    with _followup_lock:
        return robot_id in _followup_cancel
_workers_lock = threading.Lock()

# 예약 자동 처리 직렬화 — 여러 워커가 동시에 종료해도 예약 배정은 한 번에 하나씩
_reservation_lock = threading.Lock()

# ── POI 배정 선점 ────────────────────────────────────────────
# 로봇 슬롯은 start_session 이 _workers_lock 안에서 원자적으로 선점하지만(동시 호출이
# 한 로봇을 이중 배정하지 못하게), POI 쪽에는 같은 방어가 없었다. 그래서 같은 POI 를
# 동시에 호출하면 "점유 검증 → 실제 배정" 사이의 틈으로 여러 대가 배정된다.
#
# ⚠️ 이 락은 집합 조작에만 쓰고, 잡은 채로 DB·네트워크를 호출하지 않는다.
#    가용 로봇 라이브 체크(online_ips_cached)는 캐시 미스 시 수 초가 걸리므로,
#    그걸 락 안에 두면 콘솔/태블릿 응답이 통째로 밀린다(과거 폴링 지연 이슈와 같은 원인).
_claim_lock = threading.Lock()
_claimed_pois: set[int] = set()


def _try_claim_poi(poi_id: int) -> bool:
    """이 POI 의 배정 처리를 선점. 이미 다른 요청이 처리 중이면 False."""
    with _claim_lock:
        if poi_id in _claimed_pois:
            return False
        _claimed_pois.add(poi_id)
        return True


def _release_poi(poi_id: int) -> None:
    """선점 해제. 세션이 만들어진 뒤에는 occupied_poi_ids 가 점유를 이어받으므로
    배정 처리가 끝나면 바로 풀어야 한다(오래 들고 있으면 그 POI 가 호출 불가가 된다)."""
    with _claim_lock:
        _claimed_pois.discard(poi_id)


# ── 경유지 등록(출발) 직렬화 ──────────────────────────────────
# send_route 는 경로를 두 곳에 쓴다: DB(태블릿이 읽음) + worker.pending_route(워커가 읽음).
# 상태 검사와 이 두 쓰기가 원자적이지 않으면, 콘솔에서 [출발]이 겹쳐 들어올 때
# 여러 건이 모두 수락되어 **DB에 남은 경로와 로봇이 실제 가는 경로가 달라진다.**
# (탭 2개 / 응답이 느려 경로를 바꿔 다시 누르는 경우 — LTE 환경에서 특히)
#
# 로봇별로 잠근다(다른 로봇의 출발까지 막을 이유가 없다).
# ⚠️ 이 락 안에서는 DB만 만지고 로봇 통신은 하지 않는다(_claim_lock 과 같은 원칙).
_route_locks_guard = threading.Lock()
_route_locks: dict[int, threading.Lock] = {}


def _route_lock(robot_id: int) -> threading.Lock:
    with _route_locks_guard:
        lk = _route_locks.get(robot_id)
        if lk is None:
            lk = threading.Lock()
            _route_locks[robot_id] = lk
        return lk

# ── 로봇 라이브(ONLINE) 상태 캐시 ────────────────────────────
# fetch_all_robots_live 는 로봇당 REST(3s)+WS(최대 4s) 를 태워 수 초가 걸린다.
# 콘솔 폴링(2초)과 호출/예약 요청마다 이걸 그대로 태우면 응답이 밀려 UI 반영이 늦어지므로
# IP 별로 짧은 TTL 캐시를 둔다.
LIVE_CACHE_TTL = 15.0  # LTE: 라이브 체크가 무거우므로 캐시를 길게 (온·오프라인은 급변 안 함)
# 오프라인 로봇은 재조회 비용이 훨씬 크다 — 응답이 아니라 **타임아웃을 기다리는** 것이라
# 한 대만 있어도 조회가 20초 넘게 걸린다. 그래서 오프라인 판정은 캐시를 길게 잡는다.
LIVE_CACHE_TTL_OFFLINE = 60.0

# 충전소 복귀 도중 대기 예약을 확인하는 주기.
# 실제 이탈까지는 여기에 wait_move 의 POLL_INTERVAL(2s) 이 더해진다 → 최대 약 5초.
RESERVATION_WATCH_SEC = 3.0
_live_ip_cache: dict[str, tuple[float, bool]] = {}  # ip -> (조회시각, online)
_live_cache_lock = threading.Lock()
_live_bg_lock = threading.Lock()
_live_bg_inflight: set[str] = set()   # 백그라운드 갱신 중인 IP (중복 기동 방지)


def _parse_power(raw) -> Optional[int]:
    """라이브 응답의 'POWER(%)' ('85%' / '-' / 'N/A') → 0~100 정수. 값 없으면 None."""
    s = str(raw or "").strip()
    if not s.endswith("%"):
        return None
    try:
        return max(0, min(100, int(float(s[:-1]))))
    except ValueError:
        return None


def _persist_live_battery(items: list[dict]) -> None:
    """라이브 조회에 실려온 배터리를 robot_status 에 반영.

    콘솔 사이드바·POI 타일·배차 선정(find_available_robot)·예약 이어받기가
    모두 robot_status.battery_level 을 읽는데, 지금까지 이 값을 채우는 곳이 없어
    항상 비어 있었다(표시 '-', 배터리 조건 검사도 건너뜀).

    라이브 조회는 이미 WS 로 배터리를 받아오므로 추가 통신 없이 그 값을 저장한다.
    robot_status 행이 없는 로봇(sync-live/register-by-ip 로 등록된 경우)은 새로 만든다.
    실패해도 온라인 판정에는 영향을 주지 않는다.
    """
    by_ip: dict[str, int] = {}
    for it in items:
        ip = it.get("IP")
        val = _parse_power(it.get("POWER(%)"))
        if ip and val is not None:
            by_ip[ip] = val
    if not by_ip:
        return

    db = SessionLocal()
    try:
        robots = db.query(Robot).filter(Robot.ip_address.in_(list(by_ip.keys()))).all()
        if not robots:
            return
        stats = {
            st.robot_id: st
            for st in db.query(RobotStatus)
                       .filter(RobotStatus.robot_id.in_([r.id for r in robots])).all()
        }
        changed = False
        for r in robots:
            val = by_ip.get(r.ip_address)
            if val is None:
                continue
            st = stats.get(r.id)
            if st is None:
                db.add(RobotStatus(robot_id=r.id, battery_level=val))
                changed = True
            elif st.battery_level != val:
                st.battery_level = val
                changed = True
        if changed:
            db.commit()
    except Exception as e:
        logger.warning(f"[live] 배터리 반영 실패(무시): {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _live_probe(ips: list[str]) -> set[str]:
    """실제 라이브 조회 → 캐시·배터리 반영. 온라인으로 판정된 IP 집합을 돌려준다."""
    try:
        from app.robot_api.robot_live_service import fetch_all_robots_live
        from app.routers.robot import DEFAULT_SECRET
        live = fetch_all_robots_live([{"ip": ip, "secret": DEFAULT_SECRET} for ip in ips])
        items = live.get("items", [])
        online = {it.get("IP") for it in items if it.get("ONLINE") == "Online"}
        _persist_live_battery(items)   # 조회한 김에 배터리도 DB 반영
    except Exception as e:
        logger.warning(f"[live] 라이브 체크 실패(무시 — 전부 온라인 취급): {e}")
        online = set(ips)

    ts = time.time()
    with _live_cache_lock:
        for ip in ips:
            _live_ip_cache[ip] = (ts, ip in online)
    return online


def _live_probe_bg(ips: list[str]) -> None:
    try:
        _live_probe(ips)
    except Exception:
        logger.exception("[live] 백그라운드 갱신 실패")
    finally:
        with _live_bg_lock:
            for ip in ips:
                _live_bg_inflight.discard(ip)


def online_ips_cached(ips: list[str], block: bool = True) -> set[str]:
    """주어진 IP 중 온라인인 것 (TTL 캐시). 만료된 IP만 실제 라이브 조회.

    block=True  (배차 경로) : 만료됐으면 실제로 조회해 **정확한** 값을 돌려준다.
    block=False (화면 폴링) : 만료돼도 **기다리지 않는다.** 갱신은 백그라운드에 맡기고
        직전 캐시 값으로 즉시 답한다.

    화면 폴링을 비블로킹으로 뺀 이유(2026-08-24 실측):
      오프라인 로봇(.100)이 등록돼 있으면 조회가 타임아웃을 기다리느라 20초 넘게 걸린다.
      그동안 `console/status` 가 통째로 멈춰, 콘솔이 수십 초 정지했다가 한꺼번에 갱신되며
      버튼이 튀는 것처럼 보였다. 화면은 최신성보다 응답성이 중요하고, 실제 배차는
      여전히 block=True 로 정확히 판정하므로 안전하다.
    """
    if not ips:
        return set()

    now = time.time()
    result: set[str] = set()
    stale: list[str] = []
    with _live_cache_lock:
        for ip in ips:
            entry = _live_ip_cache.get(ip)
            if entry:
                ttl = LIVE_CACHE_TTL if entry[1] else LIVE_CACHE_TTL_OFFLINE
                if (now - entry[0]) < ttl:
                    if entry[1]:
                        result.add(ip)
                    continue
            stale.append(ip)

    if not stale:
        return result

    if not block:
        # 직전 값으로 답하고, 갱신은 백그라운드로
        with _live_cache_lock:
            for ip in stale:
                entry = _live_ip_cache.get(ip)
                if entry and entry[1]:
                    result.add(ip)
        todo: list[str] = []
        with _live_bg_lock:
            for ip in stale:
                if ip not in _live_bg_inflight:
                    _live_bg_inflight.add(ip)
                    todo.append(ip)
        if todo:
            threading.Thread(target=_live_probe_bg, args=(todo,), daemon=True).start()
        return result

    online = _live_probe(stale)
    result |= {ip for ip in stale if ip in online}
    return result


def _get_worker(robot_id: int) -> Optional[_Worker]:
    with _workers_lock:
        return _workers.get(robot_id)


def _set_worker(worker: _Worker) -> None:
    with _workers_lock:
        _workers[worker.robot_id] = worker


def _remove_worker(robot_id: int) -> None:
    with _workers_lock:
        _workers.pop(robot_id, None)


def clearing_robot_ids() -> set[int]:
    """**중지 요청을 받았지만 아직 워커가 안 빠진** 로봇들 (2026-09-23).

    왜 필요한가 — 현장 지적.
      [전체 작업 종료] 를 눌러도 워커가 빠져나가는 데 수 초가 걸린다.
      그동안 가용 로봇이 0대라 R 태블릿에 **[예약하기]** 가 떴다가
      [로봇 호출] 로 바뀌었다. 작업자는 "로봇 노는데 왜 예약?" 이 된다.

      가용으로 세는 건 틀렸다 — 그 순간 로봇은 정말 바쁘다.
      대신 화면이 **"정리 중 — 잠시 후 호출 가능"** 을 보여주게 한다.

    ★ 무엇을 보는가 — `abort_flag` 다.

      처음에는 `jack_service` 의 중지 플래그를 봤는데 **틀렸다.**
      `_check_stop()` 이 그 플래그를 **pop 하면서** 예외를 던진다. 그래서
      워커가 첫 체크(약 0.5초)에서 가져가 버리고, 정작 덮어야 할
      나머지 수 초가 비었다.

      `force_clear` 가 세우는 `worker.abort_flag` 는 **워커가 사라질 때까지**
      살아 있다. 이게 "강제 종료를 눌렀고 아직 정리 중" 과 정확히 같다.

    ★ `end_flag` 는 보지 않는다.
      정상 [작업 종료] 도 그걸 세우는데, 그때는 복귀·도킹까지 수 분이 걸린다.
      "잠시 후 호출" 이라고 하면 거짓말이 된다. 그 경우는 종전대로
      [예약하기] 가 맞다 — 실제로 예약해 두는 게 작업자에게 이득이다.
    """
    out: set[int] = set()
    # 강제 종료 후속 동작 중 — 워커는 없지만 로봇은 랙을 내려놓고 충전소로 가는 중이다
    with _followup_lock:
        out |= set(_followup_robots)
    with _workers_lock:
        items = list(_workers.items())
    for rid, w in items:
        if w is None:
            continue
        if w.thread is not None and not w.thread.is_alive():
            continue
        if getattr(w, "abort_flag", False):
            out.add(rid)
            continue
        # 단일 이동 중지·[모든 작업 정지] 처럼 abort_flag 를 안 세우는 경로 보완.
        # 플래그가 곧 소비되므로 이것만으로는 부족하다 — 위 abort_flag 가 본체다.
        try:
            if jack_service.is_stopping(w.robot_ip):
                out.add(rid)
        except Exception:
            pass
    return out


def is_robot_busy(robot_id: int) -> bool:
    """이 로봇에게 **새 작업을 줄 수 있나** — 배차 가용 판정의 단일 기준 (2026-09-23).

    `has_active_worker` 만 보면 안 된다. [전체 강제 종료] 뒤에는
      워커는 이미 사라졌는데 → has_active_worker = False
      후속 동작(랙 내려놓기 → 충전소)은 30~40초 더 돈다
    가 되어 **로봇이 움직이는 중인데 가용 1대**로 잡혔다.

    2026-09-23 17:36 실측
      17:36:25  가용 1 · 정리중 1     ← 두 값이 모순
      화면은 가용을 먼저 보고 [로봇 호출] 을 활성으로 그렸다.
      눌러도 로봇은 충전소로 가는 중이라 작업이 진행되지 않았고,
      새로고침하면 화면만 원래대로 돌아갔다.

    그래서 **워커 + 후속 동작** 둘 다 본다.
    """
    if has_active_worker(robot_id):
        return True
    with _followup_lock:
        if robot_id in _followup_robots:
            return True
    # 단일 이동(연구용) — 워커가 아니라 별도 스레드다
    with _goto_lock:
        if robot_id in _goto:
            return True
    # 충전소 복귀 · 강제 복귀 — 라우터가 스레드로 돌린다
    with _manual_move_lock:
        if robot_id in _manual_move_robots:
            return True
    # ★ 2026-09-29 — 작업 없이 서 있는데 **랙을 들고 있거나 잭 정지를 확인 못 한** 로봇.
    #   하차 중 잭 내림을 확인하지 못해 멈춘 경우, [그 자리 정지] 로 랙을 든 채 선 경우가
    #   여기에 든다. 새 작업은 R 에서 랙을 드는 것부터 시작하므로, 이미 들고 있거나
    #   잭이 어정쩡한 로봇에게 주면 안 된다. 원격제어 잭다운(완료 확인)이나
    #   강제 종료 후속 처리가 끝나면 풀린다.
    return _held_by_jack(robot_id)


def _held_by_jack(robot_id: int) -> bool:
    try:
        robot = _load_robot(robot_id)
        ip = robot.ip_address if robot else None
    except Exception:
        return False
    if not ip:
        return False
    try:
        if jack_service.is_laden(ip) or not jack_service.jack_settled(ip):
            # 콘솔이 몇 초마다 가용을 물으므로 info 로 찍으면 로그가 덮인다
            logger.debug("[dispatch] robot=%s 배차 제외 — 랙을 든 채 서 있거나 잭 정지 미확인 "
                        "(적재=%s, 잭확인=%s)", robot_id, jack_service.is_laden(ip),
                        jack_service.jack_settled(ip))
            return True
    except Exception:
        pass
    return False


def is_followup_running(robot_id: int) -> bool:
    """강제 종료 후속 처리가 이 로봇에서 도는 중인가 (is_robot_busy 보다 좁다).

    2026-09-29 — is_robot_busy 가 '랙을 든 채 정지' 도 바쁨으로 보게 되면서,
    '후속 처리가 빠질 때까지만' 기다려야 하는 곳은 이것을 쓴다.
    """
    with _followup_lock:
        return robot_id in _followup_robots


def has_active_worker(robot_id: int) -> bool:
    w = _get_worker(robot_id)
    if w is None:
        return False
    # thread 미시작(예약 상태) 또는 실행 중이면 활성 — 동시 호출의 중복 배정 방지
    return w.thread is None or w.thread.is_alive()


# ── POI 조회 헬퍼 ─────────────────────────────────────────────


def _load_poi(poi_id: int) -> Optional[dict]:
    """POI id → {id, name, x, y, ori, map_id}"""
    db = SessionLocal()
    try:
        p = db.query(MapPOI).filter(MapPOI.id == poi_id, MapPOI.is_active == True).first()
        if not p or p.world_x is None or p.world_y is None:
            return None
        return {
            "id": p.id,
            "name": p.name,
            "x": float(p.world_x),
            "y": float(p.world_y),
            "ori": float(p.angle or 0),
            # 2026-09-19 — 진입점("<이름>-1")을 **같은 맵**에서 찾기 위해 필요하다.
            "map_id": p.map_id,
        }
    finally:
        db.close()


def _load_robot(robot_id: int) -> Optional[Robot]:
    db = SessionLocal()
    try:
        return db.query(Robot).filter(Robot.id == robot_id).first()
    finally:
        db.close()


def _load_charging_poi(robot: Robot) -> Optional[dict]:
    if not robot.charging_id:
        return None
    return _load_poi(robot.charging_id)


def _find_entry_poi(area_id: Optional[int], poi_name: str,
                    map_id: Optional[int] = None) -> Optional[dict]:
    """사전 진입 POI("<이름>-1") 검색 — 충전소·작업지점 공통 규약.

    같은 area 의 활성 맵에서 name=`{poi_name}-1` 인 활성 POI 를 찾는다.
    없으면 None — 호출자는 **기존 동작 그대로** 진행한다(하위 호환).

    `map_id` 를 주면 **그 맵에서만** 찾는다.
      2026-09-19 — 충전소는 `robots.charging_id` 로 잡히므로 구 맵의 POI 일 수
      있다. 그때 area 기준 '활성 맵 중 id 최대' 에서 진입점을 찾으면 **다른 맵**
      을 뒤지게 된다. 현장 실측: 충전소는 map 27 의 C1, 진입점 C1-1 도 map 27
      에 있는데 여기서는 map 30 을 보고 있었다.
      `scheduler._return_to_charger` · `scripts/entry_poi_move.py` 와 같은 기준.

    쓰는 곳: C1-1(충전 도킹) · R1-1/R2-1(랙 픽업) · J1-1/J2-1(랙 하차).
    POI 종류는 `waypoint` 로 찍는다 — `jack`/`standby` 로 찍으면 안전존
    판정 제외 반경(2 m)이 하나 더 생겨 통로 감시가 넓게 꺼진다.
    """
    if not poi_name:
        return None
    from app.models.map import RobotMap
    db = SessionLocal()
    try:
        if map_id is not None:
            poi = (
                db.query(MapPOI)
                .filter(MapPOI.map_id == map_id,
                        MapPOI.name == f"{poi_name}-1",
                        MapPOI.is_active == True)
                .first()
            )
            if poi and poi.world_x is not None:
                return {"name": poi.name, "x": float(poi.world_x),
                        "y": float(poi.world_y), "ori": float(poi.angle or 0)}
            logger.info(f"[entry] map {map_id} 에 '{poi_name}-1' 이 없다 "
                        f"— area 활성 맵에서 다시 찾는다")
        # 2026-09-21 — **활성 맵을 하나만 보던 것**을 고쳤다.
        #   현장은 맵이 27·30 둘로 쪼개져 있다. 진입점 `C1-1` 은 map 27 에만,
        #   경유지와 `C1-2` 는 map 30 에만 있다. 그런데 여기는 'id 가 가장 큰
        #   활성 맵' **하나**만 뒤지고 없으면 None 을 돌려줬다.
        #   `_park_at_charger` 는 진입점이 없으면 **경유지 이동을 통째로 건너뛰고**
        #   `charge` 한 방으로 충전소까지 자유주행한다 — 현장에서 관찰된
        #   "복귀할 때 경유지 안 쓰고 회피주행" 이 이것이다(2026-09-21 보고).
        #
        #   이제 **활성 맵 전체를 id 큰 것부터** 훑는다. 이름(`<POI>-1`)이
        #   유일해서 맵을 섞어도 엉키지 않는다.
        maps = []
        if area_id is not None:
            maps = (
                db.query(RobotMap)
                .filter(RobotMap.area_id == area_id, RobotMap.is_active == True)
                .order_by(RobotMap.id.desc())
                .all()
            )
        if not maps:
            maps = (
                db.query(RobotMap)
                .filter(RobotMap.is_active == True)
                .order_by(RobotMap.id.desc())
                .all()
            )
        if not maps:
            return None
        poi = (
            db.query(MapPOI)
            .filter(
                MapPOI.map_id.in_([m.id for m in maps]),
                MapPOI.name == f"{poi_name}-1",
                MapPOI.is_active == True,
            )
            .order_by(MapPOI.map_id.desc())
            .first()
        )
        if poi and poi.world_x is not None:
            if map_id is not None and poi.map_id != map_id:
                logger.info(f"[entry] '{poi_name}-1' 을 map {poi.map_id} 에서 찾았다 "
                            f"(충전소는 map {map_id})")
            return {
                "name": poi.name,
                "x": float(poi.world_x),
                "y": float(poi.world_y),
                "ori": float(poi.angle if poi.angle is not None else 0),
            }
        logger.warning(f"[entry] 활성 맵 {len(maps)}개 어디에도 '{poi_name}-1' 이 없다 "
                       f"— 진입점 없이 진행한다")
        return None
    finally:
        db.close()


# ── 이동 + 잭 헬퍼 (jack_service 재활용) ─────────────────────


def _current_xy(robot_ip: str) -> Optional[tuple[float, float]]:
    """로봇 현재 위치. 못 읽으면 None."""
    try:
        from app.routers.map import _read_tracked_pose  # 순환 import 방지 — 함수 안에서
        pose = _read_tracked_pose(robot_ip, timeout=3.0)
        if pose and pose.get("position"):
            return float(pose["position"][0]), float(pose["position"][1])
    except Exception as e:
        logger.warning(f"[route] 현재 위치 읽기 실패 ({robot_ip}): {e}")
    return None


def _current_pose(robot_ip: str) -> Optional[tuple[float, float, float]]:
    """로봇 현재 (x, y, ori). 못 읽으면 None.

    `_current_xy` 와 달리 **방향까지** 준다 — 출발 자세를 판단하려면 필요하다.
    """
    try:
        from app.routers.map import _read_tracked_pose  # 순환 import 방지
        pose = _read_tracked_pose(robot_ip, timeout=3.0)
        if pose and pose.get("position"):
            return (float(pose["position"][0]), float(pose["position"][1]),
                    float(pose.get("ori", 0.0)))
    except Exception as e:
        logger.warning(f"[route] 현재 포즈 읽기 실패 ({robot_ip}): {e}")
    return None


# 경유지 체인에 들어가기 전 제자리 회전을 시킬 최소 각도(도).
# 이보다 작으면 로봇이 경로선 안에서 스스로 흡수하므로 굳이 돌리지 않는다.
FACE_ROUTE_MIN_DEG = 15.0
# 그 회전에 쓸 시간 상한(초). 제자리 회전이라 길 필요가 없다.
FACE_ROUTE_TIMEOUT = 40

# 랙을 든 직후 진입점으로 빠져나오는 동작(_escape_after_pickup).
# 이보다 가까우면 이미 나와 있는 것으로 보고 생략한다.
PICKUP_ESCAPE_MIN_M = 0.30
PICKUP_ESCAPE_TIMEOUT = 40


def _route_first_heading(x: float, y: float, route_coords: Optional[str],
                         min_dist: Optional[float] = None) -> Optional[float]:
    """좌표열에서 (x, y) 로부터 `min_dist` 이상 떨어진 **첫 점의 방향**(rad). 없으면 None.

    출발 정렬(`_face_route_start`)과 강제 종료 선회(`_turn_before_unload`)가 같이 쓴다.

    min_dist
      기본(None) = ARRIVED_EPS. 출발 정렬용 — 바로 다음 경유지를 본다.
      선회는 이탈 거리보다 길게 준다. 이탈 전진(1.2 m) 안에 있는 경유지는 **어차피
      지나치므로** 그 방향으로 돌면 나온 뒤 반대로 또 돈다(2026-10-07 연구소 실측 —
      W3 0.88 m 앞에서 종료 → '돌 필요 없음' 판정 → 이탈 뒤 -176.9° 회전).
      그보다 먼 점이 없으면 마지막 점(목적지 쪽)을 본다.
    """
    if not route_coords:
        return None
    try:
        v = [float(a) for a in route_coords.split(",")]
    except ValueError:
        return None
    pts = [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]
    if not pts:
        return None
    eps = waypoint_route.ARRIVED_EPS
    need = eps if min_dist is None else max(eps, min_dist)
    # ★ 2026-09-28 — 좌표열은 **로봇의 현재 위치부터** 시작한다(waypoint_route 주석 참조).
    #   그래서 첫 점은 보통 자기 자리다 — 그 점은 건너뛰고 다음 점을 본다.
    #   이 처리가 없으면 정렬 회전을 통째로 건너뛰고, 로봇이 제자리에서 알아서 돌게 된다.
    for fx, fy in pts:
        if math.hypot(fx - x, fy - y) >= need:
            return math.atan2(fy - y, fx - x)
    fx, fy = pts[-1]
    if math.hypot(fx - x, fy - y) < eps:
        return None                 # 목적지 위 — 방향을 정할 수 없다
    return math.atan2(fy - y, fx - x)


def _face_route_start(worker: _Worker, route_coords: Optional[str]) -> None:
    """경유지 체인으로 출발하기 전에 **첫 경유지 쪽으로 제자리 회전**한다.

    왜 필요한가 (2026-09-14 실측)
      `along_given_route` 는 `detour_tolerance = 0` 이라 **경로선을 1cm 도 벗어날 수
      없다.** 출발 자세가 경로 방향과 크게 다르면 로봇은 자세를 고치려는 움직임조차
      이탈로 판정돼 **돌지도 가지도 못하고 선다.**
        · R1 에서 잭업 직후 자세와 첫 구간 방향이 **58° 달랐다**
        · `alert 1007 (Not moving for too long)`, 남은거리 25 m 가 3 분간 그대로
        · costmap 확인 결과 **진행 방향 1.6 m 까지 장애물 없음** — 공간 문제가 아니었다

    회전을 경로 진입 **전에** 제자리에서 끝내므로 경로 이탈은 여전히 0 이다.
    `detour_tolerance` 를 여는 것과 달리 "지정한 길로만 · 장애물이면 정지" 규칙을
    하나도 건드리지 않는다.

    실패해도 조용히 넘어간다 — 그 경우 종전과 똑같이 동작할 뿐이다(하위 호환).
    """
    if not route_coords:
        return
    xy = _current_xy(worker.robot_ip)
    if xy is None:
        return
    cur = _current_pose(worker.robot_ip)
    if cur is None:
        return
    want = _route_first_heading(xy[0], xy[1], route_coords)
    if want is None:
        return                      # 첫 경유지 위 — 방향을 정할 수 없다
    diff = math.degrees(math.atan2(math.sin(want - cur[2]), math.cos(want - cur[2])))
    if abs(diff) < FACE_ROUTE_MIN_DEG:
        return
    logger.info(
        f"[route] {worker.robot_ip} 경로 진입 전 제자리 회전 {diff:+.1f}° "
        f"(현재 {math.degrees(cur[2]):.1f}° → 첫 경유지 방향 {math.degrees(want):.1f}°)")
    jack_service.update_job_status(worker.robot_ip, message="출발 방향 정렬 중")

    # ★ 2026-09-23 — **제자리 회전으로 먼저 시도한다.**
    #   종전에는 곧바로 `safe_move("standard", 현재위치, want)` 였다.
    #   "지금 이 자리로 가라, 방향만 바꿔서" 인데 로봇은 거리 0인 목표를
    #   주행으로 풀어서 **앞뒤로 왔다갔다 하는 3점 선회**를 한다.
    #
    #   18:30:04 실측 — 77.6° 회전에 13초, 그동안
    #     전진 0.36 → 후진 0.38 → 전진 0.15 → 후진 0.11 → 전진 0.29 → 후진 0.28
    #   현장 지적 "출발 보내면 앞으로 갔다 불필요한 후진하고 다시 출발" 이 이것이다.
    #
    #   /twist 는 속도만 주는 명령이라 경로 계획이 없다 — 제자리에서 돈다.
    #   실패하면 종전 방식으로 떨어진다(하위 호환).
    try:
        # 타임아웃은 짧게 — 안 먹는 상황이면 빨리 포기하고 종전 방식으로 넘어간다.
        # (2026-09-23: 40초를 통째로 버리는 바람에 현장에서 '아무 동작 없음' 으로 보였다)
        #
        # 2026-09-28 현장 — 12초를 줬더니 11번 **전부** 타임아웃해서 매번 그 시간을
        #   버렸다. 상한을 `ROTATE_TIMEOUT`(8초)으로 낮추고, 한 번 실패하면
        #   `_twist_unfit` 에 기록해 이후로는 시도조차 하지 않는다.
        #   그래서 이 시간은 로봇당 한 번만 든다.
        if jack_service.rotate_in_place(worker.robot_ip, want):
            return
        logger.warning("[route] 제자리 회전이 안 끝났다 — 종전 방식(standard)으로 재시도")
    except RuntimeError:
        raise                       # 사용자 중지
    except Exception as e:
        logger.warning(f"[route] 제자리 회전 실패({e}) — 종전 방식으로 재시도")

    try:
        jack_service.safe_move(worker.robot_ip, "standard", xy[0], xy[1], want,
                               max_attempts=3, timeout=FACE_ROUTE_TIMEOUT,
                               poll=jack_service.POLL_INTERVAL_SHORT)
    except RuntimeError:
        raise                       # 사용자 중지는 그대로 올린다
    except Exception as e:
        logger.warning(f"[route] 출발 방향 정렬 실패(무시하고 진행): {e}")
        return
    # 실제로 돌았는지 확인 — 안 돌았으면 종전과 같은 상황이므로 알아볼 수 있게 남긴다
    after = _current_pose(worker.robot_ip)
    if after is not None:
        left = math.degrees(math.atan2(math.sin(want - after[2]),
                                       math.cos(want - after[2])))
        if abs(left) >= FACE_ROUTE_MIN_DEG:
            logger.warning(
                f"[route] {worker.robot_ip} 출발 방향 정렬 후에도 {left:+.1f}° 남음 "
                f"— 경로 진입에서 멈출 수 있다")


class _MiniWorker:
    """워커가 이미 사라진 뒤(강제 종료 후속)에 경유지 주행을 쓰기 위한 최소 객체.

    `_move_via_waypoints` 와 `_face_route_start` 는 `robot_ip` 와 `area_id`
    두 개만 본다. 진짜 `_Worker` 를 되살리면 배차 상태머신이 다시 도는
    부작용이 있으므로 필요한 두 값만 들고 있는 껍데기를 쓴다.
    """
    __slots__ = ("robot_ip", "area_id")

    def __init__(self, robot_ip: str, area_id: Optional[int]) -> None:
        self.robot_ip = robot_ip
        self.area_id = area_id


def _move_via_waypoints(worker, x: float, y: float, ori: float, **kw):
    """경유지(W1, W2…)를 거쳐 (x, y) 로 이동.

    경유지가 없거나 현재 위치를 못 읽으면 기존 standard 이동과 완전히 같다.
    """
    xy = _current_xy(worker.robot_ip)
    if xy is None:
        # ★ 종전에는 여기서 조용히 standard(자율주행)로 나갔다.
        #   LTE 망(RTT 446ms)에서 위치 조회가 한 번 실패하면 그대로 자율주행이었다.
        #   "회피하지 않고 정지" 가 요구사항인데 예외 경로로 회피가 살아 있었던 것.
        if waypoint_route.strict_route():
            logger.error(f"[route] {worker.robot_ip} 현재 위치를 못 읽어 경로를 만들 수 없다 "
                         f"— 경로강제가 켜져 있어 이동하지 않는다")
            raise waypoint_route.RouteUnavailable("현재 위치를 못 읽음")
        logger.warning(f"[route] {worker.robot_ip} 현재 위치를 못 읽어 경유지를 건너뜀 → standard")
        return jack_service.safe_move(worker.robot_ip, "standard", x, y, ori, **kw)
    mv, extra = waypoint_route.plan(worker.area_id, xy[0], xy[1], x, y)
    if waypoint_route.is_routed(extra):
        _face_route_start(worker, extra.get("route_coordinates"))
        # 경로 길이에 맞춰 타임아웃을 올린다(내리지는 않는다). 고정값이면
        # 긴 체인이 서행 한 번에 타임아웃으로 죽고 처음부터 다시 간다.
        kw["timeout"] = waypoint_route.route_timeout(
            extra.get("route_coordinates"), xy[0], xy[1],
            base=int(kw.get("timeout") or 0))
    return jack_service.safe_move(worker.robot_ip, mv, x, y, ori, **extra, **kw)


def _approach_before_align(worker: _Worker, poi: dict) -> None:
    """랙 정렬 전에 경유지 경로로 랙 근처까지 먼저 간다.

    `align_with_rack` 은 로봇이 랙을 라이다로 찾아 **스스로 접근**하는 동작이라
    먼 거리에서 걸면 통로를 못 찾고 그 자리에 선다(2026-09-07 alert 1007,
    충전소에서 4.5m 떨어진 R1 을 바로 align 목표로 잡았던 건).
    랙 POI 에 가장 가까운 경유지가 접근점 역할을 한다.

    ★ align_with_rack 에 route_coordinates 를 실어 이 단계를 없앨 수는 없다.
      2026-09-07 실기 확인 — POST 는 201 로 받지만 로봇이 저장한 이동 기록에
      route_coordinates / detour_tolerance 필드 자체가 없다(조용히 무시).
      그래서 '경유지로 접근 → 짧은 거리만 align' 2단 구조가 반드시 필요하다.

    경유지가 없으면 아무것도 하지 않는다 — 기존 동작 그대로.
    """
    # ★ 진입점("<이름>-1") 이 있으면 그것을 우선한다 (2026-09-09).
    #   최근접 경유지는 통로 위에 있어서, 목표로 들어가는 직선이 **옆 작업지점의
    #   주차된 랙을 밟고** 지나갈 수 있다(공장 실측 — W6→R2 직선이 J1 중심에서
    #   0.38 m). 진입점을 찍어 두면 그 자리를 우리가 정한다.
    entry = _find_entry_poi(worker.area_id, poi["name"])
    if entry:
        _move_to_entry_poi(worker, entry, poi)
        return

    ap = waypoint_route.approach_point(worker.area_id, poi["x"], poi["y"])
    if not ap:
        # 경유지가 하나도 없다. 종전에는 그대로 빠져나가 멀리서 align 을 걸었다
        # (2026-09-07 alert 1007). 경로강제면 그 자리에서 실패시킨다.
        if waypoint_route.strict_route():
            raise waypoint_route.RouteUnavailable(
                f"{poi['name']} 접근점을 만들 경유지가 없다")
        return
    xy = _current_xy(worker.robot_ip)
    if xy is None:
        if waypoint_route.strict_route():
            raise waypoint_route.RouteUnavailable("현재 위치를 못 읽음")
        return
    if math.hypot(ap["x"] - xy[0], ap["y"] - xy[1]) < waypoint_route.ARRIVED_EPS:
        return  # 이미 접근점에 있다

    # ★ 접근점이 '되돌아가는' 자리면 생략한다 (2026-09-09).
    #   접근점은 "먼 거리에서 align 이 스스로 통로를 못 찾는 것" 을 막으려고 둔다.
    #   그런데 로봇이 이미 목표 코앞인데 접근점이 목표에서 **더 멀면**, 거기까지
    #   되돌아가는 건 align 을 더 어렵게 만들 뿐이다.
    #   공장 실측 — J1 반납 후 R2 로 갈 때 접근점 W6 이 J1 기준 R2 반대편이라
    #   직선 3.02 m 를 8.39 m 로 돌았다.
    d_target = math.hypot(poi["x"] - xy[0], poi["y"] - xy[1])
    d_ap = math.hypot(poi["x"] - ap["x"], poi["y"] - ap["y"])
    if d_target <= APPROACH_SKIP_MAX_M and d_ap >= d_target:
        logger.info(
            f"[route] {poi['name']} 접근점({ap['name']}) 생략 — 로봇이 이미 "
            f"{d_target:.2f} m 앞, 접근점은 {d_ap:.2f} m. 바로 정렬한다")
        return
    # ★ 도착 방향은 접근점이 랙의 '진입축' 위에 있느냐로 갈린다.
    #
    #   [축 위] → **랙 각도 그대로.**
    #     랙 각도는 로봇이 랙 밑에 들어갔을 때의 최종 자세다. 접근점에서 미리 그
    #     자세를 만들어 두면 접근점 → 랙이 **회전 없는 직선 이동**이 된다
    #     (앞쪽에 있으면 그대로 후진, 뒤쪽이면 그대로 전진 — 자세는 어느 쪽이든 같다).
    #     2026-09-07 저녁 현장: 작업지점 앞 진입축 위에 경유지를 찍었는데도 로봇이
    #     접근점에서 랙을 쳐다본 뒤 다시 돌아 진입 지점으로 가서 후진했다. 그 헛동작이
    #     이 분기로 없어진다.
    #
    #   [축 밖] → 예전대로 '랙을 바라보는 쪽'.
    #     랙 각도를 주면 접근점에서 크게 돌고 align 이 진행방향으로 또 돌아 제자리
    #     회전이 두 번 생긴다(2026-09-07 낮 실측 — R1 215°, W2→R1 진행방향 -16°).
    #
    #   ── 2026-09-08 현장 재개정 ──
    #   위 두 방식 다 접근점에서 **불필요한 제자리 회전**을 만들었다.
    #     · '랙 각도' 로 주면 : 랙이 후진 진입이라 진행 방향과 정반대 → 접근점에서
    #       180° 돌고, align 이 진입하려고 또 180° 되돌린다(현장 실측 W2).
    #     · '랙을 바라보는 쪽' 으로 주면 : align 이 다시 돌린다(9/7 실측).
    #
    #   ★ 정답은 **아무 회전도 시키지 않는 것** — 오던 방향 그대로 도착시킨다.
    #     회전은 랙 앞에서 align 이 한 번만 하면 된다. 그게 원래 정상 동작이다.
    mv, extra = waypoint_route.plan(worker.area_id, xy[0], xy[1], ap["x"], ap["y"])

    # 마지막 구간의 진행 방향 = (직전 경유지 또는 현재 위치) → 접근점
    prev = xy
    rc = extra.get("route_coordinates")
    if rc:
        v = rc.split(",")
        if len(v) >= 4:                       # 좌표열 끝이 접근점, 그 앞이 직전 경유지
            prev = (float(v[-4]), float(v[-3]))
    if math.hypot(ap["x"] - prev[0], ap["y"] - prev[1]) < 1e-3:
        # 이미 접근점 위 — 방향을 못 구하니 랙을 바라보는 쪽으로 폴백
        face = math.atan2(poi["y"] - ap["y"], poi["x"] - ap["x"])
    else:
        face = math.atan2(ap["y"] - prev[1], ap["x"] - prev[0])
    logger.info(
        f"[route] {poi['name']} 접근({ap['name']}) — 도착 방향 = 진행 방향 "
        f"{math.degrees(face):.1f}° (랙 {math.degrees(poi['ori']):.1f}° — 회전은 align 이 한다)")
    jack_service.update_job_status(
        worker.robot_ip, message=f"{poi['name']} 접근 이동({ap['name']})")
    if waypoint_route.is_routed(extra):
        _face_route_start(worker, extra.get("route_coordinates"))
    _ap_timeout = (waypoint_route.route_timeout(rc, xy[0], xy[1], base=90)
                   if waypoint_route.is_routed(extra) else 90)
    try:
        jack_service.safe_move(worker.robot_ip, mv, ap["x"], ap["y"], face,
                               **extra, max_attempts=12, timeout=_ap_timeout)
    except RuntimeError:
        raise  # 중지 신호는 그대로 올려보낸다
    except Exception as e:
        # 접근에 실패해도 정렬은 시도해 본다 (기존 동작으로 자연 폴백)
        logger.warning(f"[route] {poi['name']} 접근 이동 실패(무시): {e}")


def _escape_after_pickup(worker: _Worker, poi: dict) -> None:
    """랙을 든 직후 **좁은 랙 자리에서 진입점으로 빠져나온다** (2026-09-14).

    왜 필요한가 (실측 근거)
      `align_with_rack` → `jack_up` 이 끝나면 로봇은 **랙 자리 정중앙**에 서 있다.
        · 2026-09-14 19:43 R1 실측 — 잭업 직후 로봇(-4.09,17.82), R1(-4.13,17.79)
          까지 **0.05 m**. 말 그대로 그 자리다.
      그 상태로 `_face_route_start` 가 경로 방향으로 돌리는데, 랙을 들면 풋프린트가
      **0.95 m 사각형**이 되어 회전 시 대각선이 **반경 0.672 m** 를 휩쓴다.
      랙 자리는 랙이 딱 들어가는 공간이라 그 여유가 없다. 그래서 로봇이 제자리에서
      못 돌고 **앞으로 나갔다 되돌아오며** 조금씩 돌린다(현장에서 눈으로 확인).

      같은 날 로그가 랙 유무로 **9배** 차이를 보여준다.
        | 이동 | 상태   | 각도   | 소요  | 각속도    |
        | 4721 | 랙 적재 | 66.3° | 26초 |  2.5 °/s |
        | 4727 | 공차   | 91.3° |  4초 | 22.8 °/s |
      로봇 최대 각속도가 68.8 °/s 이니 적재 회전은 정상의 **1/28** 로 기어간 셈이다.

    무엇을 하는가
      회전하기 **전에** 진입점("<R이름>-1")으로 먼저 나온다. 거기는 통로라 공간이
      나오고, 회전이 4초에 끝난다. R1 실측에서 진입점은 1.28 m 앞이고 로봇이 이미
      그쪽(각도차 0.5°)을 보고 있어 **그대로 직진**이면 된다.

    ★ 진입점이 없으면 **아무것도 하지 않는다.**
      랙 각도로 이탈 거리를 추정하는 폴백은 넣지 않았다 — 방향을 잘못 잡으면
      랙을 든 채 엉뚱한 데로 가기 때문이다. 진입점이 없으면 종전 동작 그대로다.

    실패해도 조용히 넘어간다(하위 호환). 그 경우 종전처럼 랙 자리에서 돈다.
    """
    ip = worker.robot_ip
    name = poi.get("name")
    entry = _find_entry_poi(worker.area_id, name)
    if not entry:
        return                      # 진입점 없음 — 종전 동작
    cur = _current_pose(ip)
    if cur is None:
        logger.warning(f"[pickup] {ip} 현재 포즈를 못 읽어 랙 자리 이탈 생략")
        return
    d = math.hypot(entry["x"] - cur[0], entry["y"] - cur[1])
    if d < PICKUP_ESCAPE_MIN_M:
        return                      # 이미 진입점 근처 — 움직일 필요 없다
    logger.info(f"[pickup] {ip} 랙 자리({name}) 이탈 → {entry['name']} "
                f"{d:.2f} m (여기서 경로 방향으로 회전한다)")
    jack_service.update_job_status(ip, message=f"{entry['name']} 로 이탈")
    try:
        # 자세는 **오던 방향 그대로** 둔다. 여기서 돌리면 좁은 자리에서 도는 것과
        # 같아지고, 방향은 어차피 다음 단계(_face_route_start)가 잡는다.
        jack_service.safe_move(ip, "standard", entry["x"], entry["y"], cur[2],
                               max_attempts=2, timeout=PICKUP_ESCAPE_TIMEOUT,
                               poll=jack_service.POLL_INTERVAL_SHORT)
    except RuntimeError:
        raise                       # 사용자 중지는 그대로 올린다
    except Exception as e:
        logger.warning(f"[pickup] 랙 자리 이탈 실패(무시하고 진행): {e}")


def _pickup_at_standby(worker: _Worker) -> bool:
    """standby로 가서 align_with_rack → jack_up. 성공 시 True."""
    standby = jack_service._get_standby_poi(area_id=worker.area_id, robot_ip=worker.robot_ip)
    if not standby:
        logger.error(f"[dispatch] robot_id={worker.robot_id} standby POI 조회 실패")
        return False

    jack_service.update_job_status(worker.robot_ip, message="대기장소로 이동 (랙 정렬)")
    result = jack_service.safe_move(
        worker.robot_ip,
        "align_with_rack",
        standby["x"], standby["y"], standby["ori"],
    )
    if str(result.get("state", "")).lower() != "succeeded":
        logger.error(f"[dispatch] align_with_rack 실패: {result}")
        return False

    jack_service.update_job_status(worker.robot_ip, message="잭 업 진행 중")
    try:
        jack_service.jack_up(worker.robot_ip)
    except Exception as e:
        logger.error(f"[dispatch] jack_up 실패: {e}")
        return False
    time.sleep(JACK_SETTLE_SEC)
    # 좁은 랙 자리에서 돌지 않도록 진입점으로 먼저 나온다 (2026-09-14)
    _escape_after_pickup(worker, standby)
    return True


# 진입점이 작업지점의 '후진 진입축' 위에 있다고 볼 허용 오차.
# LG 현장 실측(2026-09-17) — R1 179.4° · R2 -178.6° · J1 -176.6° · J2 179.7°
# 로 네 곳 모두 180°에서 3.4° 안쪽이다. 12° 면 충분히 여유롭다.
ENTRY_AXIS_TOL_DEG = 12.0


def _entry_arrival_face(entry: dict, target: dict,
                        drive_face: float) -> tuple[float, str]:
    """진입점에 **어떤 자세로 도착시킬지** 정한다.

    ## 왜 이 판정이 필요한가 (2026-09-17 LG 로그 실측)
    진입점은 작업지점의 정후방 1.1 m 에 찍혀 있다. 즉 로봇이 작업지점 각도를
    보고 서 있기만 하면 **그대로 후진**하면 들어간다. 그런데 종전 코드는 항상
    '오던 진행 방향' 으로 도착시켰고, 그 결과 도착 자세가 필요값과 매번
    **80~108° 어긋났다.** 뒤이은 정밀 동작(align_with_rack / to_unload_point)이
    그걸 되돌리느라 1.1 m 를 가는 데 **누적 회전 247~424°, 19~26초**를 썼다.
    10회 전수 조사에서 예외가 없었다(R·J 모두).

    ## 판정
    `진입점 → 작업지점` 방위가 `작업지점 각도 + 180°` 와 같으면(오차
    `ENTRY_AXIS_TOL_DEG` 안) **축 위**로 본다 → 작업지점 각도로 도착시킨다.

    ## 축 밖이면 종전 그대로인 이유
    통로 경유지처럼 축에서 벗어난 자리에 작업지점 각도를 주면, 거기서 크게 돌고
    정밀 동작이 또 되돌려 **제자리 회전이 두 번** 생긴다(2026-09-08 실측 W2,
    180° 2회). 그때 얻은 교훈은 '축 밖' 에만 해당한다.
    """
    ori = target.get("ori")
    if ori is None:
        return drive_face, "작업지점 각도 없음 → 진행 방향 유지"
    axis = math.atan2(target["y"] - entry["y"], target["x"] - entry["x"])
    # 로봇이 target["ori"] 를 향하면 작업지점은 등 뒤에 있어야 한다.
    back = math.atan2(math.sin(axis - (float(ori) + math.pi)),
                      math.cos(axis - (float(ori) + math.pi)))
    off = abs(math.degrees(back))
    if off <= ENTRY_AXIS_TOL_DEG:
        return float(ori), (f"진입축 위(오차 {off:.1f}°) → 작업지점 각도 "
                            f"{math.degrees(float(ori)):.1f}° 로 도착, 그대로 후진")
    return drive_face, f"진입축 밖(오차 {off:.1f}°) → 진행 방향 유지(종전 동작)"


def _move_to_entry_poi(worker: _Worker, entry: dict, target: dict) -> bool:
    """진입점("<작업지점>-1") 까지 이동한다.

    ★ 2026-09-23 — **경로강제(strict_route)가 켜져 있으면 자율 구간이 없다.**
      진입점까지 좌표열 그대로 간다. 진입점부터가 정밀 동작
      (align_with_rack / to_unload_point)이고, 그게 유일한 자율 동작이다.

      왜 바꿨나 — 마지막 구간을 로봇 자율로 두었더니 사람이나 임시 구조물을
      피하려다 **이동 불가 경로로 들어가거나 충돌 위험**이 생겼다.
      LG 요구는 "회피하지 않고 정지" 다.

    경로강제를 끄면 종전 2단 동작으로 돌아간다.
      1) 경유지 체인으로 마지막 경유지까지 — detour_tolerance 0
      2) 마지막 경유지 → 진입점 — `standard`, 로봇이 스스로 회피해서 들어간다

    도착 자세는 **오던 진행 방향** 그대로다. 진입점의 angle 을 쓰지 않는다 —
    거기서 미리 돌려두면 그다음 정밀 동작(align_with_rack / to_unload_point)이
    다시 돌려서 제자리 회전이 두 번 생긴다(2026-09-08 현장 실측 W2, 180° 2회).
    회전은 정밀 동작이 한 번만 한다.

    반환: 진입점 도착 성공 여부.
    """
    ip = worker.robot_ip
    xy = _current_xy(ip)
    if xy is None:
        logger.warning(f"[route] {ip} 현재 위치를 못 읽어 진입점으로 바로 이동")
        r = jack_service.safe_move(ip, "standard", entry["x"], entry["y"], 0,
                                   max_attempts=12, timeout=90, poll=jack_service.POLL_INTERVAL_SHORT)
        return str(r.get("state", "")).lower() == "succeeded"

    mv, extra = waypoint_route.plan(worker.area_id, xy[0], xy[1],
                                    entry["x"], entry["y"])
    prev = xy
    rc = extra.get("route_coordinates") if waypoint_route.is_routed(extra) else None
    v = rc.split(",") if rc else []

    # ── 경로강제: 진입점까지 좌표열 그대로. 자율 구간이 없다 ──
    if waypoint_route.strict_route():
        if len(v) >= 4:
            prev = (float(v[-4]), float(v[-3]))     # 좌표열 끝이 진입점, 그 앞이 직전 경유지
        if math.hypot(entry["x"] - prev[0], entry["y"] - prev[1]) < 1e-3:
            # 이미 진입점 위 — 진행 방향을 못 구하니 작업지점을 바라보는 쪽으로
            drive_face = math.atan2(target["y"] - entry["y"], target["x"] - entry["x"])
        else:
            drive_face = math.atan2(entry["y"] - prev[1], entry["x"] - prev[0])
        face, why = _entry_arrival_face(entry, target, drive_face)
        jack_service.update_job_status(
            ip, message=f"{target['name']} 진입({entry['name']}) — 경유지 주행")
        logger.info(
            f"[route] {target['name']} 진입({entry['name']}) — 경로강제 "
            f"{mv} 로 진입점까지 직접 ({len(v) // 2}점), "
            f"도착 방향 {math.degrees(face):.1f}° — {why}")
        if rc:
            # 경로선을 못 벗어나므로 **들어가기 전에** 자세를 맞춘다
            _face_route_start(worker, rc)
        try:
            r = jack_service.safe_move(
                ip, mv, entry["x"], entry["y"], face, max_attempts=12,
                timeout=(waypoint_route.route_timeout(rc, xy[0], xy[1], base=90)
                         if rc else 90),
                poll=jack_service.POLL_INTERVAL_SHORT, **extra)
        except RuntimeError:
            raise                            # 사용자 중지 · 경로 없음
        except Exception as e:
            logger.warning(f"[route] 진입점 이동 실패: {e}")
            return False
        return str(r.get("state", "")).lower() == "succeeded"

    # ── 1단계: 마지막 경유지까지 (좌표열에서 진입점 좌표를 뺀 나머지) ──
    if ENTRY_AUTONOMOUS_LAST_LEG and len(v) >= 4:
        lead = v[:-2]                       # 진입점 좌표 제거 → 끝이 마지막 경유지
        lx, ly = float(lead[-2]), float(lead[-1])
        face = math.atan2(ly - xy[1], lx - xy[0])
        # 경유지가 하나뿐이면 좌표열이 목적지 한 점만 남는다. 그런 '경로' 는
        # 로봇이 거부할 수 있어 그냥 standard 로 보낸다 — 어차피 짧은 구간이다.
        #
        # ★ 이동 종류 이름으로 판정하면 안 된다. route_move_type 설정이
        #   "standard" 면 경로를 실어도 mv 가 "standard" 다(2026-09-23).
        _lead_routed = len(lead) >= 4
        lead_mv = mv if _lead_routed else "standard"
        jack_service.update_job_status(
            ip, message=f"{target['name']} 접근 — 경유지 주행")
        logger.info(
            f"[route] {target['name']} 진입({entry['name']}) 1단계 — "
            f"{lead_mv} 로 경유지 {len(lead) // 2}개 → 마지막 경유지({lx:.2f}, {ly:.2f})")
        try:
            kw = ({"route_coordinates": ",".join(lead),
                   "detour_tolerance": extra.get(
                       "detour_tolerance", waypoint_route.DETOUR_TOLERANCE)}
                  if _lead_routed else {})
            if _lead_routed:
                # 경로선을 못 벗어나므로 **들어가기 전에** 자세를 맞춘다.
                # (2026-09-14 — 잭업 직후 58° 어긋나 여기서 멈췄다)
                _face_route_start(worker, ",".join(lead))
            jack_service.safe_move(
                ip, lead_mv, lx, ly, face, max_attempts=12,
                timeout=waypoint_route.route_timeout(lead, xy[0], xy[1], base=90),
                **kw)
            prev = (lx, ly)
        except RuntimeError:
            raise                            # 사용자 중지
        except Exception as e:
            logger.warning(f"[route] 경유지 주행 실패(진입점으로 계속): {e}")

    # ── 2단계: 진입점까지 로봇 자율(회피) ──
    if math.hypot(entry["x"] - prev[0], entry["y"] - prev[1]) < waypoint_route.ARRIVED_EPS:
        return True                          # 이미 진입점 위
    drive_face = math.atan2(entry["y"] - prev[1], entry["x"] - prev[0])
    face, why = _entry_arrival_face(entry, target, drive_face)
    jack_service.update_job_status(
        ip, message=f"{target['name']} 진입점({entry['name']}) 이동")
    logger.info(
        f"[route] {target['name']} 진입({entry['name']}) 2단계 — "
        f"standard 자율 주행 {math.hypot(entry['x'] - prev[0], entry['y'] - prev[1]):.2f} m "
        f"(회피 허용), 도착 방향 {math.degrees(face):.1f}° — {why}")
    try:
        r = jack_service.safe_move(ip, "standard", entry["x"], entry["y"], face,
                                   max_attempts=12, timeout=90, poll=jack_service.POLL_INTERVAL_SHORT)
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"[route] 진입점 이동 실패: {e}")
        return False
    return str(r.get("state", "")).lower() == "succeeded"

def _move_to_poi(worker: _Worker, poi: dict) -> bool:
    """POI로 standard 이동."""
    jack_service.update_job_status(worker.robot_ip, message=f"이동 중: {poi['name']}")
    result = _move_via_waypoints(worker, poi["x"], poi["y"], poi["ori"])
    return str(result.get("state", "")).lower() == "succeeded"


def _jack_up_step(worker: _Worker, label: str = "잭 업") -> None:
    """잭 업 + 안정화 대기 — with_rack 무관 항상 동작 (렉 없는 모드여도 잭은 올림)."""
    jack_service.update_job_status(worker.robot_ip, message=label)
    try:
        jack_service.jack_up(worker.robot_ip)
    except Exception as e:
        logger.warning(f"[dispatch] jack_up 실패: {e}")
    time.sleep(JACK_SETTLE_SEC)


def _jack_down_step(worker: _Worker, label: str = "잭 다운") -> None:
    """잭 다운 + 안정화 대기 — with_rack 무관 항상 동작."""
    jack_service.update_job_status(worker.robot_ip, message=label)
    try:
        jack_service.jack_down(worker.robot_ip)
    except Exception as e:
        logger.warning(f"[dispatch] jack_down 실패: {e}")
    time.sleep(JACK_SETTLE_SEC)


def _return_to_standby_and_park(worker: _Worker) -> None:
    """종료 시퀀스:
      - 서빙(use_jack=False) : 잭/대기장소 없이 바로 충전소
      - 리프팅 + 렉  : 잭 든 채 standby 이동 → 잭다운(렉 반납) → 충전소 도킹
      - 리프팅 + 렉없이 : 그 자리에서 잭다운 → 충전소 도킹
    """
    robot = _load_robot(worker.robot_id)
    if not robot:
        return

    if not worker.use_jack:
        # 서빙 로봇 : 잭 없음, 대기장소 경유 없이 바로 충전소
        logger.info(f"[dispatch] {worker.robot_ip} 서빙 로봇 — 잭/대기장소 생략, 바로 충전소")
    elif worker.with_rack:
        # 도착 상태에서 잭을 든 채로 대기장소까지 운반 (잭업 불필요 — 작업 내내 들고 있음)
        standby = jack_service._get_standby_poi(area_id=worker.area_id, robot_ip=worker.robot_ip)
        if standby:
            jack_service.update_job_status(worker.robot_ip, message="복귀 중: 대기장소")
            _move_via_waypoints(worker, standby["x"], standby["y"], standby["ori"])

        # standby에 도착 후 잭 다운 (렉 반납) — 작업 전체에서 유일한 잭다운
        _jack_down_step(worker, label="잭 다운 — 대기장소 반납")
    else:
        # 리프팅 + 렉없이 : standby 경유 생략, 그 자리에서 잭 다운 후 충전소로
        _jack_down_step(worker, label="잭 다운 — 작업 종료")
        logger.info(f"[dispatch] {worker.robot_ip} 렉 없이 모드 — standby 경유 생략, 바로 충전소")

    # 충전소 도킹 (선택) — 2단계: 사전 접근 POI("<name>-1") → charge
    charging = _load_charging_poi(robot)
    if charging:
        cx, cy, cori = charging["x"], charging["y"], charging["ori"]
        # 충전소 POI 와 같은 맵에서 진입점을 찾는다 (위 _find_entry_poi 주석 참조)
        approach = _find_entry_poi(worker.area_id, charging["name"],
                                   map_id=charging.get("map_id"))

        # 1단계: 사전 접근 (best-effort — 실패해도 charge 단계로 진행)
        sx, sy, sori = (approach["x"], approach["y"], approach["ori"]) if approach else (cx, cy, cori)
        label = f"사전 접근({approach['name']})" if approach else "충전소 사전 접근"
        jack_service.update_job_status(worker.robot_ip, message=label)
        try:
            _move_via_waypoints(worker, sx, sy, sori, max_attempts=12, timeout=60)
        except Exception as e:
            logger.warning(f"[dispatch] {label} 실패(무시): {e}")
        time.sleep(2)

        # 2단계: 도킹
        jack_service.update_job_status(worker.robot_ip, message="충전소 도킹 중")
        try:
            jack_service.safe_move(
                worker.robot_ip,
                "charge",
                cx, cy, cori,
                charge_retry_count=5,
            )
        except Exception as e:
            logger.warning(f"[dispatch] 충전소 도킹 실패: {e}")


# ── 워커 메인 루프 ─────────────────────────────────────────────


def _worker_loop(worker: _Worker, *, skip_pickup: bool = False) -> None:
    """로봇 1대의 인터랙티브 배차 전 생명주기.

    skip_pickup=True: 서버 재시작 복구 — 이미 잭 들고 도착해 있으므로 픽업 단계 스킵.
    worker.with_rack=False: 렉 없이 운영 — standby 픽업(align_with_rack) 건너뜀, 종료 시 standby 경유 안 함.
                           단 잭 동작은 항상 수행 (안전한 이동 + 작업 대기를 위해).
    """
    try:
        jack_service._running_robot_id_by_ip[worker.robot_ip] = worker.robot_id
        jack_service._stop_flags[worker.robot_ip] = False
        jack_service._paused_flags[worker.robot_ip] = False

        db = SessionLocal()
        try:
            session = db.query(DispatchSession).filter(DispatchSession.id == worker.session_id).first()
            if not session:
                logger.error(f"[dispatch] session {worker.session_id} not found")
                return
            first_target_id = session.target_poi_id
        finally:
            db.close()

        # 1) 픽업 단계
        if not skip_pickup:
            if not worker.use_jack:
                # 서빙 로봇 : 잭/픽업 없음 — 바로 첫 POI로 이동
                pass
            elif worker.with_rack:
                # 리프팅 + 렉 : standby에서 align_with_rack + jack_up
                _set_db_status(worker.session_id, "picking_up")
                ok = _pickup_at_standby(worker)
                if not ok:
                    _set_db_status(worker.session_id, "failed", error="픽업 실패")
                    return
            else:
                # 리프팅 + 렉 없이 : standby 안 거치고 잭만 올림 (안전한 이동을 위해)
                _set_db_status(worker.session_id, "picking_up")
                _jack_up_step(worker, label="잭 업 — 이동 준비")

        # 2) 첫 POI 이동 — 재시작 복구일 땐 스킵 (이미 도착 가정)
        if not skip_pickup:
            if first_target_id is None:
                _set_db_status(worker.session_id, "failed", error="first_poi_id 없음")
                return
            poi = _load_poi(first_target_id)
            if not poi:
                _set_db_status(worker.session_id, "failed", error="first POI 조회 실패")
                return
            _set_db_status(worker.session_id, "moving")
            ok = _move_to_poi(worker, poi)
            if not ok:
                _set_db_status(worker.session_id, "failed", error="첫 POI 이동 실패")
                return
            _set_current_poi(worker.session_id, first_target_id)
            # 도착 — 잭을 든 채로 작업 대기 (잭 사이클 없음: 픽업 시 1회만 업)
            _set_db_status(worker.session_id, "awaiting_next")
            jack_service.update_job_status(worker.robot_ip, message=f"{poi['name']} 도착 — 작업 대기")

        # 3) 메인 루프 — 경유지 등록(awaiting_next) → 경유지 순회(moving/awaiting_confirm)
        while True:
            # === 경유지 등록 + 출발 대기 (호출 위치 도착 직후, 콘솔이 경로 등록) ===
            # 지나간 출발 신호/경로가 남아 있으면 이번 대기를 즉시 통과해
            # 의도치 않은 경로로 출발한다. 상태를 열기 전에 비운다.
            worker.next_event.clear()
            worker.pending_route = None
            if worker.end_flag:
                break

            _set_db_status(worker.session_id, "awaiting_next")
            jack_service.update_job_status(worker.robot_ip, message="경유지 등록 대기 중")

            worker.next_event.wait()
            worker.next_event.clear()
            if worker.end_flag:
                break

            route = worker.pending_route
            worker.pending_route = None
            if not route:
                continue  # 잘못 깨움 — 다시 대기

            # === 경유지를 순서대로 진행 (각 도착마다 로봇 태블릿 [확인] 대기) ===
            aborted = False
            for idx, next_id in enumerate(route):
                poi = _load_poi(next_id)
                if not poi:
                    logger.warning(f"[dispatch] 경유지 POI {next_id} 조회 실패 — 건너뜀")
                    _mark_waypoint(worker.session_id, idx, "done")
                    continue

                _set_target_poi(worker.session_id, next_id)
                _mark_waypoint(worker.session_id, idx, "current")
                # 잭을 든 채로 이동 (잭 사이클 없음). 이 시점이 "출발"
                _set_db_status(worker.session_id, "moving")
                ok = _move_to_poi(worker, poi)
                if not ok:
                    logger.warning(f"[dispatch] {poi['name']} 이동 실패")
                _set_current_poi(worker.session_id, next_id)

                is_last = (idx == len(route) - 1)

                # 지나간 [확인] 신호가 남아 있으면 이번 대기를 즉시 통과해
                # 사람 확인 없이 다음 위치로 출발해 버린다(연타/재시도 시 발생).
                # 상태를 열기 전에 반드시 비운다.
                worker.confirm_event.clear()
                if worker.end_flag:
                    aborted = True
                    break

                _set_db_status(worker.session_id, "awaiting_confirm")
                label = f"{poi['name']} 도착 — 확인 대기" + (" (마지막 위치)" if is_last else "")
                jack_service.update_job_status(worker.robot_ip, message=label)

                # 로봇 부착 태블릿의 [확인] 대기 (사람이 누를 때까지)
                worker.confirm_event.wait()
                worker.confirm_event.clear()
                _mark_waypoint(worker.session_id, idx, "done")
                if worker.end_flag:
                    aborted = True
                    break

            if worker.end_flag or aborted:
                break

            # 경유지 전체 완료 — 배터리 충분 + 대기 예약이 있으면
            # 충전소/렉 반납 없이 렉을 든 채 바로 다음 예약 장소로 이어서 진행
            cont = _take_next_reservation_if_continuable(worker)
            if cont is None:
                break  # 이어받을 예약 없음(또는 배터리 부족) → 종료 시퀀스로
            cont_poi, cont_res_id = cont

            poi = _load_poi(cont_poi)
            if not poi:
                logger.warning(f"[dispatch] 이어받기 POI {cont_poi} 조회 실패 — 예약 복구 후 종료")
                _release_reservation(worker, cont_res_id, cont_poi,
                                  reason="이어받기 POI 조회 실패 — 선점 롤백")
                break
            logger.info(f"[dispatch] 예약 이어받기 — robot={worker.robot_id} → poi={cont_poi} "
                        f"(충전소/렉반납 생략)")
            _clear_waypoints(worker.session_id)
            _set_target_poi(worker.session_id, cont_poi)
            _set_db_status(worker.session_id, "moving")
            jack_service.update_job_status(worker.robot_ip, message=f"이어서 작업 — {poi['name']}로 이동")
            ok = _move_to_poi(worker, poi)
            if not ok:
                # 이동 실패 — 예약을 되돌려 다른 로봇/다음 기회에 처리되게 하고 종료 시퀀스로
                logger.warning("[dispatch] 이어받기 이동 실패 — 예약 복구 후 종료 시퀀스로 전환")
                _release_reservation(worker, cont_res_id, cont_poi,
                                  reason="이어받기 이동 실패 — 선점 롤백")
                break
            _set_current_poi(worker.session_id, cont_poi)
            _set_db_status(worker.session_id, "awaiting_next")
            jack_service.update_job_status(worker.robot_ip, message=f"{poi['name']} 도착 — 경유지 등록 대기")
            continue  # 메인 루프 재진입 (새 경유지 등록 대기)

        # 4) 종료 시퀀스
        if worker.abort_flag:
            # 강제 정리 — 로봇 이동/잭 동작 없이 세션만 종료 (물리 위치·잭 상태 그대로).
            # 로봇을 충전소로 보내려면 원격제어의 '충전소 복귀'를 별도로 사용한다.
            logger.info(f"[dispatch] 강제 정리 — robot={worker.robot_id} 세션 종료(이동 없음)")
            _clear_waypoints(worker.session_id)
            _set_db_status(worker.session_id, "failed", error="작업 강제 정리")
        else:
            # 중간 종료로 남은 경유지가 있으면 점유가 계속 잡히므로 비운다
            _clear_waypoints(worker.session_id)
            _set_db_status(worker.session_id, "returning")
            _set_target_poi(worker.session_id, None)
            jack_service.update_job_status(worker.robot_ip, message="작업 종료 — 복귀 시퀀스 시작")
            _return_to_standby_and_park(worker)
            _set_db_status(worker.session_id, "completed")
            jack_service.update_job_status(worker.robot_ip, message="작업 완료")

    except Exception as e:
        logger.exception(f"[dispatch] 워커 예외 — robot_id={worker.robot_id}")
        _set_db_status(worker.session_id, "failed", error=str(e))
    finally:
        jack_service._running_robot_id_by_ip.pop(worker.robot_ip, None)
        try:
            from app.services.zone_lock import release_all_by_robot as _release_zones
            _release_zones(worker.robot_id)
        except Exception:
            pass
        _remove_worker(worker.robot_id)
        # 이 로봇이 가용해졌으니 대기 중 예약이 있으면 FIFO로 자동 호출.
        # ★ 단 [전체 강제 종료] 로 끊긴 것이면 건너뛴다 — 전부 취소가 그 버튼의 뜻이다.
        if getattr(worker, "no_takeover", False):
            logger.info("[dispatch] 전체 강제 종료 — robot=%s 예약 이어받기 건너뜀",
                        worker.robot_id)
        else:
            try:
                try_fulfill_reservations()
            except Exception:
                logger.exception("[dispatch] 종료 후 예약 자동 처리 실패")


# ── DB 상태 갱신 헬퍼 ─────────────────────────────────────────


def _set_db_status(session_id: int, status: str, *, error: Optional[str] = None) -> None:
    db = SessionLocal()
    try:
        dispatch_crud.update_status(db, session_id, status, error=error)
    finally:
        db.close()


def _set_target_poi(session_id: int, poi_id: Optional[int]) -> None:
    db = SessionLocal()
    try:
        dispatch_crud.set_target(db, session_id, poi_id)
    finally:
        db.close()


def _set_current_poi(session_id: int, poi_id: int) -> None:
    db = SessionLocal()
    try:
        dispatch_crud.set_current(db, session_id, poi_id)
    finally:
        db.close()


def _set_route_waypoints(session_id: int, poi_ids: list[int]) -> None:
    db = SessionLocal()
    try:
        dispatch_crud.set_route_waypoints(db, session_id, poi_ids)
    finally:
        db.close()


def _mark_waypoint(session_id: int, seq: int, status: str) -> None:
    db = SessionLocal()
    try:
        dispatch_crud.mark_waypoint(db, session_id, seq, status)
    finally:
        db.close()


def _clear_waypoints(session_id: int) -> None:
    db = SessionLocal()
    try:
        dispatch_crud.clear_route_waypoints(db, session_id)
    finally:
        db.close()


def _take_next_reservation_if_continuable(worker: "_Worker") -> Optional[tuple[int, int]]:
    """작업 종료 시점에 '충전소 안 가고 이어서' 가능한지 판단.

    조건: 종료 명령 없음 + 현재 배터리 >= robot.min_battery + 점유되지 않은 대기 예약 존재.
    만족하면 그 예약을 선점(fulfilled)하고 (poi_id, reservation_id) 반환, 아니면 None.
    호출자는 실제 이동에 실패하면 reservation_id 를 'waiting' 으로 되돌려야 한다.
    (잭/렉은 든 상태 그대로 유지 — 반납·충전소 생략)
    """
    if worker.end_flag:
        return None

    # 배터리 체크
    db = SessionLocal()
    try:
        robot = db.query(Robot).filter(Robot.id == worker.robot_id).first()
        if not robot:
            return None
        stat = db.query(RobotStatus).filter(RobotStatus.robot_id == worker.robot_id).first()
        battery = stat.battery_level if (stat and stat.battery_level is not None) else None
        min_batt = robot.min_battery if robot.min_battery is not None else 20
    finally:
        db.close()

    # 배터리 정보가 '있고' 기준 미만일 때만 충전 우선. 정보가 없으면(None) 이어받기 허용
    # (find_available_robot 의 배정 정책과 일관 — 모르면 진행).
    if battery is not None and battery < min_batt:
        logger.info(f"[dispatch] 이어받기 보류 — robot={worker.robot_id} 배터리 {battery}% < {min_batt}% → 충전 먼저")
        return None

    # 예약 소비 (try_fulfill_reservations 와 직렬화)
    with _reservation_lock:
        db = SessionLocal()
        try:
            reservations = dispatch_crud.list_waiting_reservations(db)
            if not reservations:
                logger.info(f"[dispatch] 이어받기 없음 — robot={worker.robot_id} 대기 예약 0 (battery={battery})")
                return None

            # 점유된 POI(다른 로봇이 가 있거나 경유지로 잡아둔 곳)는 건너뜀.
            # 단 내 현재 위치는 곧 떠나므로 제외한다.
            occupied = dispatch_crud.occupied_poi_ids(db)
            own = db.query(DispatchSession).filter(DispatchSession.id == worker.session_id).first()
            if own and own.current_poi_id:
                occupied.discard(own.current_poi_id)

            # 로봇 점유 + 랙 점유 둘 다 통과한 예약만 이어받는다.
            # 랙이 남아 있는 작업지점은 작업자가 [확인]을 누를 때까지 예약을 그대로 둔다.
            res = next(
                (r for r in reservations
                 if r.poi_id not in occupied and not rack_occupied_at_poi(r.poi_id)),
                None,
            )
            if not res:
                logger.info(f"[dispatch] 이어받기 없음 — robot={worker.robot_id} 대기 예약이 모두 점유 중(로봇 또는 랙)")
                return None

            dispatch_crud.mark_reservation(db, res.id, "fulfilled")
            logger.info(f"[dispatch] 이어받기 — robot={worker.robot_id} battery={battery} min={min_batt} → poi={res.poi_id}")
            _reserve_log("fulfilled", res.id, res.poi_id,
                         reason=f"robot={worker.robot_id} 이어받기 선점")
            return res.poi_id, res.id
        finally:
            db.close()


# ── 외부 API ──────────────────────────────────────────────────


def start_session(robot_id: int, first_poi_id: int, with_rack: bool = True) -> tuple[bool, str]:
    """세션 시작. 이미 활성(또는 예약 중)인 워커가 있으면 거부.

    with_rack=False 면 standby 픽업 단계를 건너뛰고 바로 first_poi로 이동.
    동시 호출이 같은 로봇을 중복 배정하지 못하도록 워커 슬롯을 _workers_lock 안에서
    원자적으로 예약(thread=None 상태로 등록)한 뒤 DB 세션 생성/스레드 기동을 진행한다.
    """
    robot = _load_robot(robot_id)
    if not robot or not robot.ip_address:
        return False, "로봇 정보 없음 또는 IP 미설정"
    if not robot.is_active:
        return False, "비활성 로봇"

    poi = _load_poi(first_poi_id)
    if not poi:
        return False, "첫 작업 POI 조회 실패"

    # 서빙 로봇은 잭 동작 없음 + 렉 개념 무의미
    use_jack = (robot.robot_type != "serving")
    eff_with_rack = with_rack if use_jack else False

    worker = _Worker(
        robot_id=robot_id,
        session_id=0,  # DB 세션 생성 후 채움
        robot_ip=robot.ip_address,
        area_id=int(robot.area_id) if robot.area_id else None,
        with_rack=eff_with_rack,
        use_jack=use_jack,
    )

    # ── 원자적 슬롯 예약: 확인과 등록 사이의 틈(TOCTOU)을 제거 ──
    with _workers_lock:
        existing = _workers.get(robot_id)
        if existing is not None and (existing.thread is None or existing.thread.is_alive()):
            return False, "이미 진행 중인 배차가 있습니다"
        _workers[robot_id] = worker  # thread=None 상태로 슬롯 점유(예약)

    # ── 예약 확보 후 DB 세션 생성 + 워커 스레드 기동 ──
    try:
        db = SessionLocal()
        try:
            session = dispatch_crud.create_session(db, robot_id, first_poi_id, with_rack=with_rack)
            worker.session_id = session.id
        finally:
            db.close()
    except Exception as e:
        _remove_worker(robot_id)  # 예약 롤백
        logger.exception("[dispatch] 세션 생성 실패")
        return False, f"세션 생성 실패: {e}"

    # ── 모드 선택 ──
    # 호출된 POI 에 job_points 매핑(J→R)이 있으면 2026-08-24 배송 시나리오로,
    # 없으면 종전 인터랙티브(콘솔 경유지 등록) 모드로 돈다.
    mapping = job_mapping_for_poi(first_poi_id) if with_rack else None
    if mapping:
        logger.info(f"[dispatch] 배송 모드 — robot={robot_id} "
                    f"{mapping['r_name']} → {mapping['j_name']}")
        t = safe_thread(target=_worker_loop_delivery, args=(worker,),
                        kwargs={"first_j_poi_id": first_poi_id},
                        name=f"delivery-{robot_id}")
    else:
        t = safe_thread(target=_worker_loop, args=(worker,), name=f"dispatch-{robot_id}")
    worker.thread = t
    t.start()
    return True, "ok"


def send_route(robot_id: int, poi_ids: list[int]) -> tuple[bool, str]:
    """호출 위치에 도착(awaiting_next)한 로봇에 경유지 경로를 등록하고 출발시킨다.

    poi_ids 순서대로 이동하며, 각 위치 도착 후 로봇 태블릿 [확인](send_confirm)을
    눌러야 다음으로 진행한다. 마지막 확인 후 자동 종료.
    """
    worker = _get_worker(robot_id)
    if not worker:
        return False, "활성 배차 없음"
    if worker.end_flag:
        return False, "종료 진행 중 — 명령 무시"

    # 유효한 POI만 추림 — 중복 제거(순서 유지) + 조회 가능한 것만.
    # 연속으로 같은 위치가 들어오면 제자리 이동이 되므로 서비스 계층에서도 방어한다.
    # (POI 조회가 여러 번이라 락 밖에서 미리 끝낸다)
    valid: list[int] = []
    seen: set[int] = set()
    for pid in poi_ids:
        if pid in seen:
            continue
        if _load_poi(pid):
            seen.add(pid)
            valid.append(pid)
    if not valid:
        return False, "유효한 경유지가 없습니다"

    # ── 상태 검사 → DB 반영 → 워커 신호를 한 덩어리로 ──
    # 이 구간이 쪼개져 있으면 동시 [출발] 이 모두 awaiting_next 를 보고 전부 통과한다.
    # 먼저 들어온 요청이 락 안에서 상태를 moving 으로 바꾸므로, 뒤따라온 요청은 거부된다.
    with _route_lock(robot_id):
        worker = _get_worker(robot_id)
        if not worker:
            return False, "활성 배차 없음"
        if worker.end_flag:
            return False, "종료 진행 중 — 명령 무시"

        db = SessionLocal()
        try:
            s = db.query(DispatchSession).filter(DispatchSession.id == worker.session_id).first()
            if not s or s.status in ("returning", "completed", "failed"):
                return False, "종료/완료된 세션 — 명령 무시"
            if s.status != "awaiting_next":
                return False, f"현재 상태({s.status})에서는 경유지를 등록할 수 없습니다"
        finally:
            db.close()

        _set_route_waypoints(worker.session_id, valid)  # DB 영속 (로봇 태블릿 조회용)
        # 출발 상태를 동기적으로 반영 — 워커가 이벤트를 처리하기 전에 폴링이 이전 상태(awaiting_next)를
        # 보여 콘솔 UI가 롤백되는 것을 막는다. 워커는 깨어나 같은 값으로 재설정 후 실제 이동을 수행한다.
        _set_target_poi(worker.session_id, valid[0])
        _mark_waypoint(worker.session_id, 0, "current")
        _set_db_status(worker.session_id, "moving")
        worker.pending_route = valid
        worker.next_event.set()
    return True, "ok"


def send_next(robot_id: int, next_poi_id: int) -> tuple[bool, str]:
    """레거시 단일 다음 위치 — 경유지 1개짜리 경로로 처리."""
    return send_route(robot_id, [next_poi_id])


def send_confirm(robot_id: int) -> tuple[bool, str]:
    """로봇 부착 태블릿의 [확인] — 다음 경유지로 진행(또는 마지막이면 종료 유도)."""
    worker = _get_worker(robot_id)
    if not worker:
        return False, "활성 배차 없음"
    if worker.end_flag:
        return False, "종료 진행 중 — 명령 무시"

    db = SessionLocal()
    try:
        s = db.query(DispatchSession).filter(DispatchSession.id == worker.session_id).first()
        if not s or s.status != "awaiting_confirm":
            return False, f"확인할 수 있는 상태가 아닙니다 ({s.status if s else 'none'})"
    finally:
        db.close()

    worker.confirm_event.set()
    return True, "ok"


def send_end(robot_id: int) -> tuple[bool, str]:
    worker = _get_worker(robot_id)
    if not worker:
        return False, "활성 배차 없음"
    if worker.end_flag:
        return True, "이미 종료 처리 중"
    worker.end_flag = True
    # 일시정지 중이면 먼저 풀어준다.
    # _wait_if_paused() 는 _paused_flags 만 보는 폴링 루프라 아래 이벤트로는 깨지 않는다.
    # 안 풀면 워커가 거기 갇혀 종료 시퀀스(랙 반납 → 충전소)로 넘어가지 못한다.
    # (2026-08-19: [작업 정지]를 stop-all → pause 로 바꾸면서 생긴 문제)
    try:
        if jack_service.is_paused(worker.robot_ip):
            jack_service.resume_robot_job(worker.robot_ip)
    except Exception:
        logger.warning(f"[dispatch] send_end — 일시정지 해제 실패(무시): robot={robot_id}")
    # 어느 대기 상태든 깨어나도록 두 이벤트 모두 set
    worker.next_event.set()
    worker.confirm_event.set()
    return True, "ok"


def force_clear(robot_id: int, after: str = "hold") -> tuple[bool, str]:
    """실행 중인 배차 작업을 로봇 이동 없이 강제 정리.

    - 메모리 워커에 abort 신호 → 복귀(충전소) 시퀀스 없이 세션을 종료하고 워커 슬롯 해제.
      (로봇은 물리적으로 그대로 — 잭/위치 유지.)
    - 워커가 없으면(서버 재시작 후 유실 등) DB 활성 세션만 정리한다.

    `after` — 정리가 끝난 뒤 로봇에게 시킬 일 (LGIT 요청, 2026-09-17)
      "hold"    : 아무것도 안 함. 그 자리 그대로 (종전 동작 = 로봇별 원격제어 패널)
      "reserve" : **멈춘 자리에서 랙 내려놓고** → 대기 예약이 있으면 그 R 지점으로,
                  없으면 충전소로. (콘솔 [현재 JOB 강제 종료])
      "charge"  : **멈춘 자리에서 랙 내려놓고** → 충전소로. (콘솔 [전체 강제 종료])

    ※ 랙은 **멈춘 그 자리**에 내려놓는다. 통로를 막을 수 있지만 현장 합의 사항이다
      — 놓인 랙은 작업자가 직접 치운다. 랙을 들고 있지 않으면 잭 조작을 건너뛴다.
    """
    worker = _get_worker(robot_id)
    if worker:
        # ★ 2026-09-29 현장 사고 — 강제 종료 뒤 로봇을 움직이는 주체가 둘이 됐다.
        #   종전에는 [현재 JOB 강제 종료]에서 워커가 죽으면서(finally) **곧바로 대기
        #   예약을 물어 새 작업을 시작**했다. 동시에 후속 처리 스레드는 '워커가 빠지길'
        #   기다렸다가 잭다운을 보냈는데, 그 사이 생긴 **새 워커를 옛 워커로 착각**하고
        #   10초를 채운 뒤 **달리는 로봇에 잭다운**을 보냈다.
        #     14:58:39 취소 → 14:58:42 랙을 든 채 R1 로 출발 → 14:58:55 잭다운(0.8 m/s 주행 중)
        #
        #   이제 규칙은 하나다 — **강제 종료 뒤 로봇을 움직이는 것은 후속 처리뿐이다.**
        #     ① 워커는 어떤 강제 종료에서도 예약을 이어받지 않는다(no_takeover)
        #     ② 후속 처리가 랙을 내려놓고(완료 확인) 빠져나온 **뒤에** 예약을 넘긴다
        #     ③ 그 사이 다른 배차가 붙지 않도록 **지금 즉시** '후속 처리 중' 으로 표시한다
        #        (종전에는 후속 스레드가 뜬 뒤에 표시해서, 워커 finally 와 경합했다)
        if after != "hold" and worker.robot_ip:
            with _followup_lock:
                _followup_robots.add(robot_id)
        worker.abort_flag = True
        worker.end_flag = True  # 대기 루프를 깨워 종료로 진입시킴 (abort_flag 로 복귀 스킵)
        # 워커가 죽으면서 대기 예약을 물어 새 작업을 시작하지 않게 한다 — 위 ① 참조.
        #   "charge" 는 원래부터 그랬고, "reserve" 는 후속 처리가 넘긴다.
        #   "hold"(그 자리 정지)도 로봇이 서 있어야 하는 것이므로 이어받지 않는다.
        worker.no_takeover = True
        # 이동 중이거나 일시정지 중이면 아래 두 이벤트로는 깨울 수 없다.
        #   - 이동 중      : safe_move 안에서 로봇 응답을 기다리는 중
        #   - 일시정지 중  : _wait_if_paused() 폴링 루프에 갇혀 있음
        # jack_service 의 중지 플래그를 세우면 두 경우 모두 _check_stop() 에서
        # RuntimeError 로 즉시 빠져나온다 → 워커가 정리되고 로봇은 그 자리에 선다.
        # ※ 여기서 resume(일시정지 해제)만 하면 safe_move 가 '같은 이동 재시도' 로
        #   목적지까지 가버려 "이동 없이 정리" 라는 이 기능의 전제가 깨진다.
        #
        # ★ 로봇에게도 이동 취소를 보내야 한다.
        #   stop_robot_job() 은 서버 메모리 플래그만 세운다 — 로봇 통신이 없다.
        #   그래서 워커는 죽지만 로봇은 이미 받은 이동 명령을 자기가 끝까지 수행해
        #   "세션은 정리됐는데 로봇은 목적지까지 가는" 상태가 된다 (2026-08-19 실기 확인).
        #   문서 §9 루틴 ⑦ "로봇은 물리적으로 그 자리 그대로" 를 만족시키려면
        #   여기서 현재 이동을 취소해야 한다.
        # ★ 2026-09-28 — 취소 **전에 감속해서 세운다.**
        #   곧바로 취소하면 주행 명령이 버려져 로봇이 제동으로 선다(현장 지적 "급정거").
        #   랙을 들고 있으면 더 위험하다. safety_zone 이 RED 에서 쓰는 것과 같은
        #   방식으로 속도 0 을 걸어 로봇이 자기 감속도로 서게 한 뒤 취소한다.
        #   그러면 취소 시점에는 이미 속도가 0 이라 세울 것이 없다.
        try:
            jack_service.decelerate_to_stop(worker.robot_ip, why=f"{after} 종료")
        except Exception as e:
            logger.warning(f"[dispatch] force_clear — 감속 정지 실패(그대로 취소): {e}")
        # ★★ 순서가 중요하다 — 중지 플래그를 **취소 직전에** 세운다.
        #
        #   `resume_robot_job` 의 설명대로 **"cancel 된 이동은 safe_move 가 자동
        #   재시도"** 한다. 그래서 취소와 중지 플래그 사이에 틈이 생기면, 워커가
        #   아직 살아 있어서 같은 이동을 그대로 다시 발행한다.
        #
        #   2026-09-28 12:46 실측 — 취소와 플래그 사이에 속도 복구(HTTP 왕복 약
        #   0.5초)를 끼워 넣었더니 그 틈에 재시도가 들어갔다.
        #     12:46:03  감속 정지 완료          (5.46, 5.27) 에 섰다
        #     12:46:04  속도 1.2 복구            ← 여기서 재발행된 이동이 살아났다
        #     12:46:05~08  다시 전진 2.4 m → (7.20, 6.68)
        #   "정지 후 경로를 조금 더 가서 다시 돌아온다" 가 이것이었다.
        #
        #   safety_zone 이 RED 에서 이동을 취소하지 않는 이유(그 파일 주석)와 같은
        #   함정이다. 플래그를 먼저 세우면 safe_move 가 `_check_stop` 에서 즉시
        #   RuntimeError 로 빠져나가 재시도하지 않는다.
        try:
            jack_service.stop_robot_job(worker.robot_ip)   # 워커 즉시 탈출
        except Exception:
            logger.warning(f"[dispatch] force_clear — 중지 신호 전달 실패(무시): robot={robot_id}")
        try:
            jack_service.cancel_current_move(worker.robot_ip, timeout=RECOVER_ROBOT_TIMEOUT)
        except Exception as e:
            # 통신 실패해도 세션 정리는 계속한다 (로봇이 꺼졌을 수도 있다)
            logger.warning(f"[dispatch] force_clear — 이동 취소 실패(무시): robot={robot_id}: {e}")
        try:
            jack_service.resume_robot_job(worker.robot_ip)  # 정지 표시 해제 (화면 정합성)
        except Exception:
            logger.warning(f"[dispatch] force_clear — 정지 표시 해제 실패(무시): robot={robot_id}")
        # ★ 속도는 **맨 마지막에** 되돌린다 — 0 으로 두면 뒤따르는 후속 동작
        #   (랙 이탈·복귀)이 한 발도 못 나간다. 여기까지 오면 워커가 죽고 이동도
        #   취소된 뒤이므로 되살아날 이동이 없다.
        try:
            jack_service.apply_state_speed(worker.robot_ip)
        except Exception as e:
            logger.warning(f"[dispatch] force_clear — 속도 복구 실패: {e}")
        worker.next_event.set()
        worker.confirm_event.set()
        _start_force_followup(robot_id, worker.robot_ip, after, old_worker=worker)
        return True, "ok"
    # 워커 없음 — DB 활성 세션만 정리
    ip = None
    db = SessionLocal()
    try:
        robot = db.query(Robot).filter(Robot.id == robot_id).first()
        ip = robot.ip_address if robot else None
        sess = dispatch_crud.get_active_session(db, robot_id)
        if not sess:
            _start_force_followup(robot_id, ip, after)
            return True, "정리할 작업 없음"
        dispatch_crud.clear_route_waypoints(db, sess.id)
        dispatch_crud.update_status(db, sess.id, "failed", error="작업 강제 정리")
    finally:
        db.close()
    _start_force_followup(robot_id, ip, after)
    return True, "ok"


# 강제 종료 후속 동작 — 워커가 빠져나가기를 기다리는 최대 시간
#   2026-09-29 — 10 → 60초. 워커는 잭 완료 대기(최대 25초 + 재확인 15초)와
#   안정화 대기(8초) 중에는 중지 신호를 못 본다. 10초로는 워커가 살아 있는 채로
#   후속 처리가 로봇을 움직이기 시작했다. 넘겨도 이제는 진행하지 않고 멈춘다.
FOLLOWUP_WORKER_WAIT_SEC = 60.0


def _start_force_followup(robot_id: int, robot_ip: Optional[str], after: str,
                          old_worker: Optional["_Worker"] = None) -> None:
    """강제 종료 뒤 '랙 놓기 → 이동' 을 **별도 스레드**에서 수행한다.

    왜 스레드인가
      `force_clear` 는 워커를 죽이는 함수다. 그 안에서 바로 이동을 시키면
      자기가 방금 죽인 스레드가 정리되기를 기다리게 되고, 중지 플래그도 아직
      살아 있어 새 이동이 곧바로 취소된다. 그래서 밖으로 뺀다.

    실패해도 조용히 끝낸다 — 강제 종료 자체(세션 정리)는 이미 끝난 상태다.
    """
    if after == "hold" or not robot_ip:
        return
    try:
        safe_thread(target=_force_followup, args=(robot_id, robot_ip, after, old_worker),
                    name=f"force-followup-{robot_ip}").start()
    except Exception:
        logger.exception(f"[dispatch] 강제 종료 후속 스레드 시작 실패 robot={robot_id}")
        with _followup_lock:
            _followup_robots.discard(robot_id)      # force_clear 가 미리 세운 표시를 걷는다


def _halt_followup(robot_id: int, robot_ip: str, after: str, label: str,
                   reason: str, notify: bool = True) -> None:
    """후속 처리를 **로봇을 움직이지 않고** 끝낸다 (2026-09-29).

    잭 내림을 확인하지 못했거나, 이전 작업이 끝나지 않아 로봇을 누가 쥐고 있는지
    불확실할 때 쓴다. 이탈·다음 작업·충전소 복귀를 **하지 않는다.**
    대기 예약은 그대로 남는다 — 사람이 확인한 뒤 원격제어로 정리한다.
    """
    logger.error("[dispatch] ★ %s 후속 동작 중단 — %s. 로봇 %s 을(를) 그 자리에 세운다",
                 label, reason, robot_ip)
    try:
        jack_service.update_job_status(robot_ip, status="error",
                                       message=f"{label} — {reason}. 현장 확인 필요")
    except Exception:
        pass
    if not notify:
        return
    try:
        robot = _load_robot(robot_id)
        name = (robot.name if robot else None) or f"robot {robot_id}"
        head = "현재 작업을 중지했습니다" if after == "reserve" else "작업을 전부 중지했습니다"
        msg = "\n".join([
            f"⚠ [{name}] {head}.",
            f"{reason} — 로봇을 그 자리에 세웠습니다.",
            "현장에서 로봇과 대차 상태를 확인한 뒤 원격제어로 정리해 주세요.",
        ])
        kind = (notice_service.KIND_JOB_FORCE_CLEAR if after == "reserve"
                else notice_service.KIND_JOB_FORCE_CLEAR_ALL)
        notice_service.push(kind, msg, targets=force_clear_notice_targets(),
                            ack_required=True, key=f"force_clear_r{robot_id}")
    except Exception:
        logger.exception("[dispatch] 후속 동작 중단 알림 실패")


def _followup_destination(after: str) -> Optional[dict]:
    """후속 이동 목적지. None 이면 충전소로 간다.

    "reserve" 일 때만 대기 예약을 본다. 예약은 **J POI** 로 걸리므로
    job_points 매핑으로 **R 지점**을 찾는다 — 현장에서 호출은 R 에서 하고,
    강제 종료 후에도 로봇은 R 에서 다시 시작해야 한다(2026-09-17 합의).
    """
    if after != "reserve":
        return None
    db = SessionLocal()
    try:
        rows = dispatch_crud.list_waiting_reservations(db)
    except Exception:
        logger.warning("[dispatch] 후속 이동 — 대기 예약 조회 실패, 충전소로 간다")
        return None
    finally:
        db.close()
    for res in rows or []:
        mapping = job_mapping_for_poi(res.poi_id)
        if not mapping:
            continue
        r_poi = _load_poi_by_name(mapping["area_id"], mapping["r_name"])
        if r_poi:
            logger.info(f"[dispatch] 후속 이동 목적지 — 대기 예약 {mapping['j_name']} "
                        f"의 R 지점 {mapping['r_name']}")
            return r_poi
    return None


# 강제 종료 후 랙 아래에서 빠져나오는 거리.
#   진입점 규약이 쓰는 1.1 m(작업지점 정후방)와 같은 수준으로 잡았다.
#   랙 depth 0.50 + 로봇 길이 0.76 → 뒤끝이 랙 밖으로 나오는 데 0.63 m 가 필요하고,
#   여유를 둔 값이다.
UNLOAD_ESCAPE_M = 1.2


def _load_any_charging_poi(area_id: Optional[int]) -> Optional[dict]:
    """로봇에 충전소가 지정돼 있지 않을 때의 폴백 — 첫 번째 충전소 POI.

    `scheduler._return_to_charger` 의 3단계 폴백과 같은 기준이다. 방향 판정에만
    쓰므로 진입점(C1-1)이 아니라 충전소 자체 좌표면 충분하다.
    """
    db = SessionLocal()
    try:
        q = db.query(MapPOI).filter(MapPOI.poi_type == "charging",
                                    MapPOI.is_active == True)
        poi = q.first()
        if poi and poi.world_x is not None:
            return {"name": poi.name, "x": poi.world_x, "y": poi.world_y,
                    "ori": poi.angle or 0}
    except Exception:
        logger.warning("[dispatch] 충전소 POI 폴백 조회 실패")
    finally:
        db.close()
    return None


def _followup_dest_or_charger(robot_id: int, after: str) -> tuple[Optional[dict], Optional[int]]:
    """후속 동작이 **다음에 갈 곳**과 area_id.

    예약 인계 대상(R 지점)이 있으면 그쪽, 없으면 충전소.
    선회(`_turn_before_unload`)와 이탈(`_escape_after_unload`)이 같은 목적지를 봐야
    선회한 방향과 실제 출발 방향이 어긋나지 않는다.
    """
    _robot = _load_robot(robot_id)
    try:
        area = int(_robot.area_id) if (_robot and _robot.area_id) else None
    except (TypeError, ValueError):
        area = None
    dest = _followup_destination(after)
    if not dest:
        dest = _load_charging_poi(_robot) if _robot else None
        if not dest:
            dest = _load_any_charging_poi(area)
    return dest, area


# ── 강제 종료 후 '랙 든 채 선회 → 하차' (2026-10-07 구현, 사양 2026-10-01 확정) ──
#
# 왜 필요한가
#   종전 순서는 잭다운 → 전진 이탈 → (랙 밖에서) 경로 방향으로 회전이었다.
#   그런데 통로 한가운데서 벽을 보고 종료되면 랙을 내린 뒤 **앞으로 나갈 수가 없다.**
#   랙을 내린 뒤에는 그 아래에서 돌 수도 없다 — 로봇 외접원 0.519 m 가
#   랙 다리 거리 0.42~0.43 m 보다 커서 다리에 닿는다.
#   그래서 **랙을 든 채 가고자 하는 방향으로 먼저 돌고**, 내려놓고, 그 방향으로 나온다.
#
# 방향 = 다음 경로의 **첫 구간 방향** (목적지 직선 방향이 아니다).
#   출발 정렬(`_face_route_start`)과 같은 계산이라 나온 뒤 다시 돌지 않는다.
UNLOAD_TURN_MIN_DEG = 15.0      # 이 이하면 돌지 않는다 (출발 정렬 FACE_ROUTE_MIN_DEG 와 같은 값)
# 랙 적재 회전은 로봇 자체 제한으로 2.5~5 °/s (2026-09-28 현장 실측).
#   180° 면 최대 72초 → 출발 정렬용 상한(8초·40초)으로는 매번 실패한다.
UNLOAD_TURN_TIMEOUT = 90
# 작업지점(R·J) 칸 반경. 이 안에서는 랙을 든 채 돌면 옆 랙·설비에 닿는다.
UNLOAD_TURN_CELL_M = 1.5
# 선회 방향을 볼 때 이탈 거리(UNLOAD_ESCAPE_M) 에 더하는 여유 — 1.2 + 0.3 = 1.5 m 너머 점을 본다.
UNLOAD_TURN_LOOKAHEAD_MARGIN_M = 0.3


def _near_work_point(area_id: Optional[int], x: float, y: float) -> Optional[str]:
    """(x, y) 가 작업지점(R·J) 칸 안이면 그 작업지점 이름. 아니면 None.

    작업지점 = poi_type `jack`(J) · `standby`(R). 진입점(`R1-1` 등)은 waypoint 라 제외된다.
    조회에 실패하면 **칸 안으로 본다** — 모르는 채로 랙을 든 채 돌지 않는다.
    """
    db = SessionLocal()
    try:
        q = db.query(RobotMap).filter(RobotMap.is_active == True)        # noqa: E712
        if area_id is not None:
            q = q.filter(RobotMap.area_id == area_id)
        active = q.order_by(RobotMap.id.desc()).first()
        if active is None:
            return None
        rows = (db.query(MapPOI)
                .filter(MapPOI.map_id == active.id,
                        MapPOI.is_active == True,                        # noqa: E712
                        MapPOI.poi_type.in_(("jack", "standby")))
                .all())
        best = None
        for p in rows:
            if p.world_x is None or p.world_y is None:
                continue
            d = math.hypot(float(p.world_x) - x, float(p.world_y) - y)
            if d <= UNLOAD_TURN_CELL_M and (best is None or d < best[0]):
                best = (d, p.name or "?")
        return best[1] if best else None
    except Exception:
        logger.exception("[dispatch] 작업지점 칸 판정 실패 — 칸 안으로 본다")
        return "(판정 실패)"
    finally:
        db.close()


def _unload_turn_heading(robot_id: int, after: str, x: float, y: float
                         ) -> tuple[Optional[float], Optional[dict], Optional[int]]:
    """(x, y) 에서 출발할 **다음 경로의 첫 구간 방향**(rad), 목적지, area_id.

    경로가 안 나오면(경유지 없음 등) 목적지 직선 방향. 목적지를 모르면 방향은 None.
    """
    dest, area = _followup_dest_or_charger(robot_id, after)
    if not dest:
        return None, None, area
    want = None
    try:
        _mv, extra = waypoint_route.plan(area, x, y, dest["x"], dest["y"])
        if waypoint_route.is_routed(extra):
            # 이탈 전진 거리 안의 경유지는 어차피 지나친다 — 그 너머 점을 본다
            want = _route_first_heading(x, y, extra.get("route_coordinates"),
                                        min_dist=UNLOAD_ESCAPE_M + UNLOAD_TURN_LOOKAHEAD_MARGIN_M)
    except Exception as e:
        logger.warning("[dispatch] 선회 방향 — 경로 계산 실패(%s), 목적지 직선 방향을 쓴다", e)
    if want is None:
        want = math.atan2(dest["y"] - y, dest["x"] - x)
    return want, dest, area


def _turn_before_unload(robot_id: int, robot_ip: str, after: str, label: str,
                        loaded: Optional[bool], flagged: bool) -> None:
    """랙을 내려놓기 **전에** 다음 경로 첫 방향으로 랙을 든 채 돈다.

    규칙 (2026-10-01 확정, 2026-10-07 수정)
      · 다음 경로 첫 방향과 지금 방향의 차이가 15° 이하면 돌지 않는다
      · 15° 를 넘으면 랙을 든 채 그쪽으로 돈다
      · 돌 수 없거나 돌면 안 되는 경우는 **돌지 않고 종전 순서로 넘긴다**
        (잭다운 → 전진 이탈 → 랙 밖에서 회전 → 복귀)
          - 작업지점 칸 안(반경 1.5 m) — 후진으로 들어가 있어 앞이 열려 있다
          - 잭업 완료가 불확실 — 든 채 돌지 않는다
          - 현재 자세를 못 읽음 / 회전 실패

    ★ 2026-10-07 사용자 지시 — **어떤 경우에도 그 자리에 세우지 않는다.**
      강제 종료 = 충전소 복귀, JOB 종료 = 예약 R 지점(없으면 충전소)이 반드시 이어져야 한다.
      처음 구현은 위 예외에서 정지·알림(_halt_followup)으로 끝냈는데, J1-1 에서 종료하자
      'J1 칸 안' 에 걸려 랙을 든 채 서 버렸다(11:27:15). 그래서 이 함수는 로봇을 세우지 않는다.
    """
    pose = _current_pose(robot_ip) or _current_pose(robot_ip)    # LTE 순간 끊김 대비 1회 재시도
    if pose is None:
        logger.warning("[dispatch] %s 선회 생략 — 현재 자세를 못 읽음. 종전 순서로 진행", robot_ip)
        return
    x, y, ori = pose

    want, dest, area = _unload_turn_heading(robot_id, after, x, y)
    if want is None:
        # 충전소조차 등록이 안 된 설정 문제. 방향을 모르니 돌 근거가 없다 — 종전대로 진행.
        logger.warning("[dispatch] %s 선회 생략 — 다음 목적지를 모른다(충전소 미지정?)", robot_ip)
        return
    diff = math.degrees(math.atan2(math.sin(want - ori), math.cos(want - ori)))
    if abs(diff) <= UNLOAD_TURN_MIN_DEG:
        logger.info("[dispatch] %s 선회 불필요 — 다음 경로(%s) 첫 방향 %.1f°, 현재 %.1f° (차 %+.1f°)",
                    robot_ip, dest.get("name"), math.degrees(want), math.degrees(ori), diff)
        return

    # ── 돌아야 한다. 든 채 돌면 안 되는 경우는 종전 순서로 넘긴다 ──
    if loaded is None or (loaded is False and flagged):
        logger.warning("[dispatch] %s 선회 생략 — 잭업 완료 불확실(필요 회전 %+.0f°). "
                       "내린 뒤 밖에서 돈다", robot_ip, diff)
        return
    cell = _near_work_point(area, x, y)
    if cell:
        logger.warning("[dispatch] %s 선회 생략 — 작업지점 %s 칸 안(필요 회전 %+.0f°). "
                       "내리고 전진으로 나온 뒤 돈다", robot_ip, cell, diff)
        return

    logger.warning("[dispatch] %s 선회 — 대차를 든 채 %+.1f° (현재 %.1f° → 다음 경로(%s) 첫 방향 %.1f°)",
                   robot_ip, diff, math.degrees(ori), dest.get("name"), math.degrees(want))
    jack_service.update_job_status(robot_ip, status="moving",
                                   message=f"{label} — 대차를 든 채 갈 방향으로 회전합니다")
    # 제자리 회전(/twist) 먼저. 안 먹는 로봇(현장 longjack)은 즉시 False → 제자리 standard.
    try:
        ok = jack_service.rotate_in_place(robot_ip, want, timeout=UNLOAD_TURN_TIMEOUT)
        if not ok:
            jack_service.safe_move(robot_ip, "standard", x, y, want,
                                   max_attempts=2, timeout=UNLOAD_TURN_TIMEOUT,
                                   poll=jack_service.POLL_INTERVAL_SHORT)
    except RuntimeError:
        raise                       # 사용자 중지는 그대로 올린다
    except Exception as e:
        logger.warning("[dispatch] %s 선회 명령 실패: %s", robot_ip, e)

    # 결과는 기록만 한다. 덜 돌았어도 종전 순서(내림 → 전진 이탈 → 밖에서 회전)로 이어간다.
    now = _current_pose(robot_ip)
    if now is None:
        logger.warning("[dispatch] %s 선회 후 자세를 못 읽음 — 그대로 진행", robot_ip)
        return
    left = math.degrees(math.atan2(math.sin(want - now[2]), math.cos(want - now[2])))
    if abs(left) > UNLOAD_TURN_MIN_DEG:
        logger.warning("[dispatch] %s 선회 미완(남은 %+.1f°) — 내린 뒤 밖에서 마저 돈다", robot_ip, left)
    else:
        logger.info("[dispatch] %s 선회 완료 — 남은 %+.1f°", robot_ip, left)


def _escape_after_unload(robot_id: int, robot_ip: str, after: str, label: str) -> None:
    """랙을 제자리에 내려놓은 직후 **랙 아래에서 직선으로 빠져나온다** (2026-09-28).

    왜 필요한가 (실기 근거)
      강제 종료는 멈춘 그 자리에서 잭다운한다 → 랙이 로봇 위에 내려앉고
      **로봇은 랙 아래에 갇힌다.** 그 상태로 다음 이동을 걸면
      `_move_via_waypoints` → `_face_route_start` 가 경로 첫 방향으로 돌리는데,
      복귀는 왔던 길을 되돌아가는 것이라 그 각도가 거의 180° 다.
        2026-09-28 10:04:15 — 제자리 회전 +169.2°
        2026-09-28 10:08:19 — 제자리 회전 +173.4°
      랙을 든 풋프린트는 0.95 m 사각형이라 회전이 반경 0.672 m 를 휩쓴다
      (`_escape_after_pickup` 주석의 실측과 같은 근거). 그래서 랙 다리를 긁고,
      결국 **회전 → 이탈 → 다시 회전** 으로 동선을 낭비했다.

    무엇을 하는가
      회전하기 **전에** 랙 밖으로 **전진**해서 빠져나온다.

    ★ 2026-09-28 현장 — **무조건 전진이다.** 종전에는 목적지 방향을 보고
      전진/후진을 골랐는데, 그건 연구소 로봇(crawler_s300_op5)이 목이 없어
      전후 대칭이라 가능했던 것이다. **현장 longjack 은 목이 있어 후진으로
      나오면 안 된다.**

      그리고 전진이 원래 맞는 방향이다. 로봇은 작업지점에 **후진으로 진입**하므로
      (진입점 규약 — `_entry_arrival_face` 참조) 랙 안에서 머리가 통로 쪽을 향한다.
      전진이 곧 통로로 나오는 방향이다.

      목적지가 뒤쪽이면 나온 뒤에 돌아야 하지만, 그 회전은 랙 밖에서 하므로
      랙 아래에서 도는 것보다 낫다.

    ★ 픽업 쪽(`_escape_after_pickup`)과 달리 진입점을 쓸 수 없다.
      강제 종료는 작업지점이 아니라 **통로 한가운데**에서도 일어나기 때문이다.
      그래서 현재 자세 기준 직선 이동(`jack_service.drive_straight`)을 쓴다.

    실패해도 조용히 넘어간다 — 그 경우 종전처럼 랙 아래에서 돈다.
    """
    pose = _current_pose(robot_ip)
    if pose is None:
        logger.warning("[dispatch] %s 현재 포즈를 못 읽어 랙 이탈 생략", robot_ip)
        return
    # 목적지 — 예약 인계 대상(R 지점)이 있으면 그쪽, 없으면 충전소.
    #   ※ 여기서는 방향만 쓴다. 실제 이동 목적지는 아래에서 다시 정한다.
    dest, _ = _followup_dest_or_charger(robot_id, after)
    if not dest:
        logger.warning("[dispatch] %s 목적지를 몰라 랙 이탈 방향을 못 정한다 — 생략", robot_ip)
        return
    # ★ 방향은 고르지 않는다 — **항상 전진**이다(위 주석 참조).
    #   목적지는 로그에만 남긴다. 나온 뒤 어디로 갈지 읽을 때 쓴다.
    to_dest = math.atan2(dest["y"] - pose[1], dest["x"] - pose[0])
    diff = math.degrees(math.atan2(math.sin(to_dest - pose[2]), math.cos(to_dest - pose[2])))
    logger.info("[dispatch] %s 랙 이탈 — 전진 %.2f m "
                "(현재 %.1f° / 목적지 %s 방향 %.1f°, 차 %.1f° — 나온 뒤 이만큼 돈다)",
                robot_ip, UNLOAD_ESCAPE_M, math.degrees(pose[2]),
                dest.get("name"), math.degrees(to_dest), diff)
    jack_service.update_job_status(robot_ip, status="moving",
                                   message=f"{label} — 대차 밖으로 빠져나옵니다(전진)")
    try:
        ok = jack_service.drive_straight(robot_ip, UNLOAD_ESCAPE_M, forward=True)
    except RuntimeError:
        raise                       # 사용자 중지는 그대로 올린다
    except Exception as e:
        logger.warning("[dispatch] 랙 이탈 실패: %s", e)
        ok = False
    if not ok:
        # ★ 2026-09-29 — /twist 가 안 먹는 로봇(현장 longjack)은 위 직선 이동을
        #   건너뛴다. 종전에는 그대로 넘어가서 **로봇이 랙 아래에 남고**, 다음 이동이
        #   랙 아래에서 180° 돌았다(현장 상황 1·2).
        #   대신 **현재 방향 그대로** 앞 지점을 **같은 방향**으로 준다. 목표 방향이
        #   지금 방향과 같으므로 회전할 이유가 없다 — 정상 하차 이탈과 같은 방식이다.
        _escape_forward_move(robot_ip, pose, UNLOAD_ESCAPE_M, label)


def _escape_forward_move(robot_ip: str, pose, dist: float, label: str) -> bool:
    """현재 자세 기준 `dist` m 앞을 **현재 방향 그대로** 목표로 주는 일반 이동.

    `pose` = (x, y, ori). 회전 없이 전진으로 빠져나오게 하는 것이 목적이다.
    실패하면 False — 호출부는 계속 진행한다(종전 동작).
    """
    x, y, ori = pose
    fx, fy = x + dist * math.cos(ori), y + dist * math.sin(ori)
    logger.info("[dispatch] %s 랙 이탈(대체) — 현재 방향 %.1f° 그대로 %.2f m 앞 (%.2f, %.2f)",
                robot_ip, math.degrees(ori), dist, fx, fy)
    jack_service.update_job_status(robot_ip, status="moving",
                                   message=f"{label} — 대차 밖으로 빠져나옵니다(전진)")
    try:
        r = jack_service.safe_move(robot_ip, "standard", fx, fy, ori,
                                   max_attempts=2, timeout=40,
                                   poll=jack_service.POLL_INTERVAL_SHORT)
        return str(r.get("state", "")).lower() == "succeeded"
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("[dispatch] 랙 이탈(대체) 실패(무시하고 진행): %s", e)
        return False


def _force_followup(robot_id: int, robot_ip: str, after: str,
                    old_worker: Optional["_Worker"] = None) -> None:
    label = {"reserve": "현재 JOB 강제 종료", "charge": "전체 강제 종료"}.get(after, after)
    logger.warning(f"[dispatch] ★ {label} 후속 동작 시작 — robot={robot_id} {robot_ip}")
    # 이 스레드가 도는 동안 로봇은 랙을 내려놓고 충전소로 간다 — **바쁘다.**
    # 그런데 워커가 아니라 화면에는 "가용 0대" 인데 이유가 안 보였다.
    #   2026-09-23 실측 — 그 공백이 24초. 그동안 [예약하기] 만 떴다.
    with _followup_lock:
        _followup_robots.add(robot_id)
        _followup_cancel.discard(robot_id)
        _followup_to_charger.discard(robot_id)
    try:
        _force_followup_body(robot_id, robot_ip, after, label, old_worker)
    finally:
        with _followup_lock:
            was_cancelled = robot_id in _followup_cancel
            _followup_robots.discard(robot_id)
            _followup_cancel.discard(robot_id)
            _followup_to_charger.discard(robot_id)
        # ★ 불변식 — **중지 플래그를 세운 쪽이 반드시 걷는다.**
        #   cancel_followup() 이 진행 중 이동을 끊으려고 플래그를 세운다.
        #   안 걷으면 뒤이어 오는 명령(충전소 복귀 등)의 첫 이동이
        #   _check_stop() 에서 즉시 취소된다 — 2026-09-23 18:39 에 실제로 그랬다.
        if was_cancelled:
            try:
                jack_service.clear_stop_flag(robot_ip)
                jack_service.resume_robot_job(robot_ip)
            except Exception:
                pass


def _force_followup_body(robot_id: int, robot_ip: str, after: str, label: str,
                         old_worker: Optional["_Worker"] = None) -> None:

    # 1) **그 워커**가 완전히 빠질 때까지 대기 (2026-09-29)
    #   종전에는 `_get_worker(robot_id) is not None` 으로 봤다. 그래서 워커가 죽으면서
    #   만든 **새 워커**를 옛 워커로 착각해 10초를 채우고, 새 워커가 달리는 중에
    #   잭다운을 보냈다. 이제는 넘겨받은 그 객체가 사라지는지를 본다.
    #   워커는 잭 완료 대기(최대 25초+재확인 15초)와 안정화 대기(8초) 중일 수 있어
    #   상한을 넉넉히 둔다. 그래도 안 빠지면 **움직이지 않는다** — 주체가 둘이 된다.
    if old_worker is not None:
        deadline = time.time() + FOLLOWUP_WORKER_WAIT_SEC
        while time.time() < deadline and _get_worker(robot_id) is old_worker:
            time.sleep(0.3)
        if _get_worker(robot_id) is old_worker:
            _halt_followup(robot_id, robot_ip, after, label,
                           f"이전 작업이 {FOLLOWUP_WORKER_WAIT_SEC:.0f}초 안에 끝나지 않았다")
            return
    other = _get_worker(robot_id)
    if other is not None and other is not old_worker:
        # 강제 종료 중에는 배차가 붙지 않아야 한다(_followup_robots). 그래도 붙었으면
        # 그 워커가 로봇을 쥐고 있으므로 후속 처리는 로봇을 건드리지 않는다.
        _halt_followup(robot_id, robot_ip, after, label,
                       "다른 작업이 이미 로봇을 쓰고 있다", notify=False)
        return

    # 2) 중지 플래그를 걷어낸다 — 안 걷으면 아래 이동이 즉시 취소된다
    try:
        jack_service.resume_robot_job(robot_ip)
        jack_service.clear_stop_flag(robot_ip)
    except Exception:
        logger.warning(f"[dispatch] 후속 동작 — 중지 플래그 해제 실패(무시) {robot_ip}")

    # 3) 랙을 들고 있으면 **그 자리에서** 내려놓는다
    try:
        loaded = jack_service.is_rack_loaded(robot_ip)
    except Exception:
        loaded = None
    # ★ 판정을 **두 갈래로** 본다 (2026-09-28 실기 사고 후).
    #
    #   is_rack_loaded() 는 이름과 달리 "잭이 올라가 있나" 만 본다(그 함수 주석 참조).
    #   잭업이 미완이면 랙이 부분적으로 들려 있어도 **False** 가 나온다.
    #   실제로 그랬다 — 09-28 09:25, 랙을 든 채였는데 두 번 다 False 로 읽혀
    #   잭 조작을 건너뛰고 **랙을 든 채로 다음 예약 작업에 나섰고 충전소에 도킹했다.**
    #
    #   그래서 서버가 잭업을 보낸 기록(`is_laden`)도 같이 본다.
    #   **둘 다 "없다" 고 할 때만** 생략한다. 한쪽이라도 들고 있다고 하면 내려놓는다 —
    #   안 들고 있는데 잭다운을 해도 잭만 내려갈 뿐 해가 없지만, 들고 있는데
    #   건너뛰면 랙을 든 채 충전소로 가버린다.
    try:
        flagged = jack_service.is_laden(robot_ip)
    except Exception:
        flagged = True          # 모르면 들고 있다고 본다
    if loaded is False and not flagged:
        logger.info(f"[dispatch] {robot_ip} 후속 동작 — 랙 없음 확인(잭 내려감+기록 없음), 잭 조작 생략")
    else:
        if loaded:
            how = "적재 확인"
        elif flagged:
            how = "잭 상태는 '내려감' 이지만 잭업 기록이 있다 — 안전하게"
        else:
            how = "적재 여부 불명 — 안전하게"
        # ★ 2026-10-07 — 내려놓기 **전에** 랙을 든 채 다음 경로 방향으로 돈다.
        #   내린 뒤에는 랙 아래에서 돌 수 없고, 벽을 보고 섰으면 앞으로 나갈 수도 없다.
        #   못 돌면 세우지 않고 종전 순서로 이어간다(_turn_before_unload 주석).
        _turn_before_unload(robot_id, robot_ip, after, label, loaded, flagged)
        jack_service.update_job_status(robot_ip, status="unloading",
                                       message=f"{label} — 이 자리에 랙을 내려놓습니다")
        logger.warning(f"[dispatch] {robot_ip} 후속 동작 — 제자리 잭다운({how})")
        try:
            # jack_down() 이 완료(progress)까지 기다리므로 고정 대기를 덧붙이지 않는다.
            # 사용자가 화면 앞에서 기다리는 경로라 불필요한 10초를 얹지 않는다.
            jack_service.jack_down(robot_ip)
        except Exception as e:
            logger.warning(f"[dispatch] 후속 동작 — 잭다운 명령 실패: {e}")
        # ★ 2026-09-29 — 내림 완료를 확인하지 못했으면 **여기서 멈춘다.**
        #   종전에는 "잭다운 실패(계속 진행)" 로 이탈·다음 작업까지 나갔다.
        #   반쯤 내려온 랙을 끌고 가는 것보다 그 자리에 서서 사람을 부르는 게 낫다.
        if not jack_service.jack_settled(robot_ip):
            _halt_followup(robot_id, robot_ip, after, label,
                           "랙 내림 완료를 확인하지 못했다")
            return
        # ★ 랙을 내려놓았으면 로봇은 지금 **랙 아래**에 있다 — 돌기 전에 빠져나온다.
        #   예약 인계보다 **먼저** 해야 한다. 인계하면 새 워커가 곧바로 이동을
        #   시작하고, 그 이동의 첫 동작이 다시 '랙 아래 회전' 이 되기 때문이다.
        _escape_after_unload(robot_id, robot_ip, after, label)

    # 4) 목적지로 이동
    if _followup_cancelled(robot_id):
        logger.warning("[dispatch] %s 후속 동작 — 사용자 명령으로 중단 (목적지 결정 전)", label)
        return

    # ★ 2026-09-23 현장 지적 — "취소한 순간 R 로 가는 게 작업인데 예약으로 남는 건 틀렸다."
    #
    #   현장 정의: 현재 JOB 강제 종료 = 지금 작업 종료 + **예약된 작업이 있으면 그걸 한다.**
    #   그런데 종전에는 후속 동작이 R 까지 **자기가 직접** 몰고 가서 세워두고,
    #   예약은 `waiting` 그대로 남겼다. 그래서
    #     · 화면에는 [예약 취소] 가 떠 있고 (이미 그 작업을 하는 중인데)
    #     · 눌러버리면 로봇이 가던 길에 멈춘다
    #     · 후속 동작이 끝나도 아무도 예약을 소진하지 않아 그대로 남는다
    #
    #   그래서 후속 동작이 직접 몰지 않고 **정식 배차로 넘긴다.**
    #   배송 모드가 R 에서 픽업해 J 로 배송하므로 "R 에서 다시 시작" 합의와도 맞다.
    #   화면에도 처음부터 '작업 중' 으로 뜬다.
    if after == "reserve":
        with _followup_lock:
            _followup_robots.discard(robot_id)      # 가용으로 풀어야 배차가 붙는다
        try:
            try_fulfill_reservations()
        except Exception:
            logger.exception("[dispatch] %s 후속 동작 — 예약 인계 실패", label)
        if has_active_worker(robot_id):
            logger.warning("[dispatch] ★ %s 후속 동작 종료 — 예약을 정식 작업으로 넘겼다 "
                           "(robot=%s)", label, robot_id)
            return
        logger.info("[dispatch] %s 후속 동작 — 넘길 예약이 없다. 충전소로 간다", label)
        with _followup_lock:
            _followup_robots.add(robot_id)          # 충전소 갈 동안 다시 바쁨

    dest = _followup_destination(after)

    # ★ 2026-09-19 — 목적지가 정해진 **지금** 확정 문구로 알림을 갱신한다.
    #   라우터는 종료 버튼을 누른 즉시 "있으면 …/없으면 …" 조건문으로 띄운다.
    #   어디로 가는지는 여기서야 알 수 있어서, 같은 key 로 덮어쓴다.
    #   2026-09-21 — 대차를 안 들고 있었으면 "대차를 내리고" 를 뺀다.
    #     위에서 이미 읽어둔 `loaded` 를 그대로 쓴다(True/False/None).
    #     None(통신 실패)이면 들고 있었다고 본다 — 잭다운 판단과 같은 기준.
    _push_followup_notice(robot_id, after, dest, had_rack=(loaded is not False))

    # 경유지 주행에 필요한 area_id — 워커는 이미 사라졌으므로 DB 에서 읽는다.
    #   ※ Robot.area_id 는 문자열 컬럼이고 RobotMap.area_id 는 정수다.
    #      워커를 만들 때와 **똑같이** int 로 바꿔야 맵 조회가 맞는다.
    _robot = _load_robot(robot_id)
    try:
        _area = int(_robot.area_id) if (_robot and _robot.area_id) else None
    except (TypeError, ValueError):
        _area = None
    mini = _MiniWorker(robot_ip, _area)
    try:
        if _followup_cancelled(robot_id):
            with _followup_lock:
                to_charger = robot_id in _followup_to_charger
            if not to_charger:
                logger.warning("[dispatch] %s 후속 동작 — 사용자 명령으로 중단 (이동 전)", label)
                return
            logger.warning("[dispatch] %s 후속 동작 — 예약이 사라져 충전소로 바꾼다", label)
            dest = None
            with _followup_lock:
                _followup_cancel.discard(robot_id)      # 충전소 이동은 계속해야 한다
                _followup_to_charger.discard(robot_id)
            try:
                jack_service.clear_stop_flag(robot_ip)  # 위에서 세운 중지 플래그를 걷는다
                jack_service.resume_robot_job(robot_ip)
            except Exception:
                pass
        if dest:
            jack_service.update_job_status(
                robot_ip, status="moving",
                message=f"{label} — 대기 호출이 있어 {dest['name']} 로 이동합니다")
            # 2026-09-19 — 종전에는 standard(자유주행)였다. 현장에서 "예약한 곳으로
            #   갈 때 경유지를 안 거친다"는 지적. 통로에 내려놓은 랙은 작업자가
            #   바로 치우기로 합의돼(2026-09-19) 경로가 막힐 위험을 감수한다.
            #   경유지가 없으면 _move_via_waypoints 가 알아서 standard 로 떨어진다.
            try:
                _move_via_waypoints(mini, dest["x"], dest["y"], dest.get("ori") or 0,
                                    max_attempts=8, timeout=180)
            except RuntimeError:
                # ★ 2026-09-23 — 이동 **도중에** 취소가 들어온 경우.
                #   위의 '이동 직전 확인' 은 이미 지나간 뒤라 여기서 다시 본다.
                #   종전에는 여기서 그냥 끝나서 로봇이 통로 한가운데 섰다
                #   (19:03:57 실측 — 예약 취소 → 중단은 됐는데 충전소로 안 갔다).
                if not _followup_wants_charger(robot_id):
                    raise
                logger.warning("[dispatch] %s 후속 이동이 중단됐다 — 충전소로 보낸다", label)
                _followup_go_charger(robot_id, robot_ip, label)
                logger.warning(f"[dispatch] ★ {label} 후속 동작 종료 — robot={robot_id}")
                return
            jack_service.update_job_status(robot_ip, status="idle",
                                           message=f"{dest['name']} 대기 중")
        else:
            jack_service.update_job_status(robot_ip, status="returning",
                                           message=f"{label} — 충전소로 복귀합니다")
            from app.services.scheduler import _return_to_charger
            _return_to_charger(robot_ip, [])
    except Exception as e:
        logger.warning(f"[dispatch] {robot_ip} 후속 이동 실패(무시): {e}")
        # 취소로 끊긴 것이면 세워두지 말고 충전소로 보낸다
        if _followup_wants_charger(robot_id):
            logger.warning("[dispatch] %s 후속 이동이 중단됐다 — 충전소로 보낸다", label)
            _followup_go_charger(robot_id, robot_ip, label)
    logger.warning(f"[dispatch] ★ {label} 후속 동작 종료 — robot={robot_id}")


def _followup_wants_charger(robot_id: int) -> bool:
    with _followup_lock:
        return robot_id in _followup_to_charger


def _followup_go_charger(robot_id: int, robot_ip: str, label: str) -> None:
    """중단된 후속 동작을 충전소 복귀로 이어붙인다.

    중지 플래그를 먼저 걷어야 새 이동이 첫 확인에서 바로 취소되지 않는다.
    """
    with _followup_lock:
        _followup_cancel.discard(robot_id)
        _followup_to_charger.discard(robot_id)
    try:
        jack_service.clear_stop_flag(robot_ip)
        jack_service.resume_robot_job(robot_ip)
    except Exception:
        pass
    try:
        jack_service.update_job_status(robot_ip, status="returning",
                                       message=f"{label} — 충전소로 복귀합니다")
        from app.services.scheduler import _return_to_charger
        _return_to_charger(robot_ip, [])
    except Exception as e:
        logger.warning("[dispatch] %s 충전소 복귀 실패(무시): %s", robot_ip, e)


def _josa_ro(word: str) -> str:
    """받침에 맞는 `로` / `으로`.

    작업지점 표시 이름을 현장에서 바꿀 수 있게 되어(설정 → 작업지점 이름)
    "자재실 도착로" 같은 어색한 문구가 나올 수 있다. 마지막 글자의 받침을 본다.
    받침이 없거나 `ㄹ` 이면 `로`, 그 외에는 `으로`.
    """
    w = (word or "").strip()
    if not w:
        return "로"
    code = ord(w[-1])
    if 0xAC00 <= code <= 0xD7A3:                 # 한글 음절
        return "로" if (code - 0xAC00) % 28 in (0, 8) else "으로"
    return "로"


def force_clear_notice_targets() -> list[str]:
    """강제 종료 안내를 띄울 대상 — 콘솔 + **모든 작업지점 태블릿**.

    2026-09-19 LGIT 요청(개선사항 16번). 종전에는 콘솔에만 떴다.
    PoC 기간에는 어느 태블릿에서 봐도 알 수 있도록 전 지점에 띄운다
    (운영 전환 시 예약이 걸린 지점으로 좁히는 것을 검토).

    조회에 실패해도 **콘솔은 반드시 남긴다** — 알림이 통째로 사라지면 안 된다.
    """
    out = [notice_service.TARGET_CONSOLE]
    db = SessionLocal()
    try:
        map_ids = [m.id for m in db.query(RobotMap)
                   .filter(RobotMap.is_active == True).all()]      # noqa: E712
        if map_ids:
            rows = (db.query(MapPOI)
                    .filter(MapPOI.map_id.in_(map_ids),
                            MapPOI.is_active == True,               # noqa: E712
                            MapPOI.poi_type.in_(("jack", "standby")))
                    .all())
            for r in rows:
                t = notice_service.poi_target(r.id)
                if t not in out:
                    out.append(t)
    except Exception:
        logger.exception("[dispatch] 태블릿 알림 대상 조회 실패 — 콘솔에만 띄운다")
    finally:
        db.close()
    return out


def _push_followup_notice(robot_id: int, after: str, dest: Optional[dict],
                          had_rack: bool = True) -> None:
    """강제 종료 뒤 **실제 목적지가 정해진 시점**의 확정 문구 알림.

    라우터가 먼저 띄운 조건형 문구를 같은 `key` 로 덮어쓴다.

    `had_rack` — 멈출 때 **대차를 들고 있었는가**. 2026-09-21 현장 요청으로,
      안 들고 있었으면 "대차를 내리고" 와 "내려놓은 대차를 치워 주세요" 를 뺀다.
      없는 대차를 치우러 가게 만드는 문구였다. 모르면 True(들고 있다고 본다).
    """
    robot = _load_robot(robot_id)
    name = (robot.name if robot else None) or f"robot {robot_id}"
    # 표시 이름(자재실 출발 …)이 있으면 그걸로. 없으면 원래 이름 그대로.
    where = ((_poi_label(dest["name"]) or dest["name"]) if dest else "충전소")
    head = "현재 작업을 중지했습니다" if after == "reserve" else "작업을 전부 중지했습니다"
    move = f"{where}{_josa_ro(where)} 이동합니다."
    msg = "\n".join(
        [f"⚠ [{name}] {head}."]
        + ([f"대차를 내리고 {move}", "내려놓은 대차를 치워 주세요."]
           if had_rack else [move]))
    kind = (notice_service.KIND_JOB_FORCE_CLEAR if after == "reserve"
            else notice_service.KIND_JOB_FORCE_CLEAR_ALL)
    notice_service.push(kind, msg, targets=force_clear_notice_targets(),
                        ack_required=True, key=f"force_clear_r{robot_id}")


# ── 전역 강제 종료 (LGIT 요청 3번) ────────────────────────────
#
# 로봇별 원격제어 패널의 [작업 강제 종료](force_clear)를 **콘솔 상단에서 한 번에**
# 쓰는 것이다. 두 버튼의 차이는 '대기 중인 호출(예약)을 어떻게 하느냐' 하나뿐이다.
#
#   현재 JOB 강제 종료 : 진행 중인 작업만 끊는다. 대기 호출은 살아 있으므로
#                        로봇이 놓이면 그 호출부터 다시 나간다.
#   전체 강제 종료     : 진행 중 작업 + 대기 호출까지 전부 없앤다. 처음부터 다시 호출.
#
# ★ 로봇은 그 자리에 선다. 충전소로 보내지 않는다(force_clear 의 기존 계약).


def _active_robot_ids() -> list[int]:
    """지금 작업 중인 로봇 id — 메모리 워커 + DB 활성 세션을 합친다.

    워커만 보면 서버 재시작으로 워커가 유실된 '유령 세션' 이 남고,
    DB 만 보면 세션 만들기 직전의 워커를 놓친다. 둘 다 본다.
    """
    ids: set[int] = set()
    with _workers_lock:
        ids |= set(_workers.keys())
    db = SessionLocal()
    try:
        for s in dispatch_crud.list_active_sessions(db):
            if s.robot_id:
                ids.add(s.robot_id)
    except Exception:
        logger.exception("[dispatch] 활성 세션 조회 실패 — 메모리 워커만 정리한다")
    finally:
        db.close()
    return sorted(ids)


def force_clear_current() -> int:
    """진행 중인 작업을 전부 강제 종료. **대기 예약은 그대로 둔다.**

    반환: 정리한 로봇 수
    """
    ids = _active_robot_ids()
    for rid in ids:
        try:
            # 랙을 그 자리에 내려놓고 → 대기 예약의 R 지점(없으면 충전소)으로
            force_clear(rid, after="reserve")
        except Exception:
            logger.exception(f"[dispatch] 강제 종료 실패(계속 진행) robot={rid}")
    logger.warning(f"[dispatch] ★ 현재 JOB 강제 종료 — 로봇 {len(ids)}대 정리 "
                   f"(대기 예약은 유지)")
    return len(ids)


# 워커가 빠져나가기를 기다리는 시간. 아래 force_clear_all 주석 참조.
FORCE_CLEAR_DRAIN_SEC = 6.0


def force_clear_all() -> dict:
    """진행 중 작업 + 대기 중인 호출(예약)까지 전부 취소.

    ★ 순서가 중요하다 — **먼저 끊고, 워커가 빠져나간 뒤에 예약을 지운다.**
      배송 워커는 강제 정리를 감지하면 자기가 선점했던 예약을 `waiting` 으로
      **되돌린다**(`_worker_loop_delivery` 의 abort 처리 — 호출 자체는 살려두는 게
      원래 의도다). 예약을 먼저 지우면 그 롤백이 뒤에 실행돼 취소한 호출이
      되살아난다. 그래서 워커가 정리될 때까지 잠깐 기다렸다가 지운다.

    반환: {"cleared": 로봇 수, "cancelled": 취소한 예약 수}
    """
    ids = _active_robot_ids()
    for rid in ids:
        try:
            # 랙을 그 자리에 내려놓고 → 충전소로
            force_clear(rid, after="charge")
        except Exception:
            logger.exception(f"[dispatch] 강제 종료 실패(계속 진행) robot={rid}")

    # 워커가 빠져나갈 시간을 준다. 다 빠지면 즉시 넘어간다(최대 FORCE_CLEAR_DRAIN_SEC).
    deadline = time.time() + FORCE_CLEAR_DRAIN_SEC
    while time.time() < deadline:
        with _workers_lock:
            if not _workers:
                break
        time.sleep(0.2)
    with _workers_lock:
        remaining = list(_workers.keys())

    db = SessionLocal()
    try:
        poi_ids = dispatch_crud.cancel_all_waiting_reservations(db)
    finally:
        db.close()
    for pid in poi_ids:
        _reserve_log("cancelled", None, pid, reason="전체 강제 종료(콘솔)")

    if remaining:
        # 워커가 제때 안 죽었다 — 그 워커가 뒤늦게 예약을 되살릴 수 있으므로
        # 잠시 뒤 한 번 더 지운다. (정상 상황에서는 여기까지 오지 않는다)
        logger.warning(f"[dispatch] 전체 강제 종료 — 워커 {remaining} 가 아직 살아 있음. "
                       f"5초 뒤 예약을 한 번 더 정리한다")
        safe_thread(target=_recancel_reservations_later, args=(5.0,),
                    name="force-clear-recancel").start()

    logger.warning(f"[dispatch] ★ 전체 강제 종료 — 로봇 {len(ids)}대 정리, "
                   f"대기 호출 {len(poi_ids)}건 취소")
    return {"cleared": len(ids), "cancelled": len(poi_ids)}


# ══════════════════════════════════════════════════════════
# 로봇 한 대만 강제 종료 (2026-09-17 콘솔 UI 개편)
#
#   종전에는 콘솔 상단에 전역 버튼 두 개가 있었고, 둘 다 _active_robot_ids() 로
#   **작업 중인 로봇 전부**를 돌았다. 로봇이 한 대일 때만 안전했던 구조다.
#   현장에서 여러 대가 섞여 돌면 "지금 누르면 어느 로봇이 멈추는지" 를 알 수 없어
#   버튼을 로봇별 원격제어 패널로 옮겼다. 아래가 그 진입점이다.
# ══════════════════════════════════════════════════════════


def _all_waiting_reservation_pois() -> set[int]:
    """지금 대기 중인 예약의 POI 전부 (2026-09-23).

    [전체 강제 종료] 는 "현재 작업 + 예약된 작업 전부 종료" 다. 그 로봇이
    물고 있던 것만이 아니라 **대기열에 남아 있는 예약 전부**를 지워야 한다.
    """
    ids: set[int] = set()
    db = SessionLocal()
    try:
        for r in dispatch_crud.list_waiting_reservations(db):
            if r.poi_id:
                ids.add(int(r.poi_id))
    except Exception:
        logger.exception("[dispatch] 대기 예약 목록 조회 실패")
    finally:
        db.close()
    return ids


def _session_poi_ids(robot_id: int) -> set[int]:
    """이 로봇의 활성 세션이 물고 있는 POI id 들.

    강제 종료 시 '이 로봇이 잡았던 호출' 을 찾는 데 쓴다. 배송 워커는 abort 를
    감지하면 자기 예약을 `waiting` 으로 **되돌리므로**(_worker_loop_delivery 참조),
    그 예약이 어느 POI 것인지 미리 알아둬야 뒤에서 지울 수 있다.
    """
    ids: set[int] = set()
    db = SessionLocal()
    try:
        sess = dispatch_crud.get_active_session(db, robot_id)
        if sess:
            for v in (sess.first_poi_id, sess.current_poi_id, sess.target_poi_id):
                if v:
                    ids.add(int(v))
    except Exception:
        logger.exception(f"[dispatch] 세션 POI 조회 실패 robot={robot_id}")
    finally:
        db.close()
    return ids


def force_clear_robot(robot_id: int, scope: str = "current") -> dict:
    """**이 로봇 한 대만** 강제 종료한다. 다른 로봇은 건드리지 않는다.

    scope
      "current" : 진행 중인 작업만 중지. 멈춘 자리에 랙을 내려놓고 대기 호출이
                  있으면 그 R 지점, 없으면 충전소로. **호출(예약)은 유지** —
                  다른 로봇이 이어받을 수 있다.
      "all"     : 위와 같되 충전소로 가고, **이 로봇이 잡고 있던 호출까지** 취소.
                  다른 로봇의 작업과 그 로봇이 잡은 호출은 그대로 둔다.

    반환: {"cleared": 0|1, "cancelled": 취소한 예약 수}
    """
    if scope not in ("current", "all"):
        raise ValueError(f"scope 는 current|all 이어야 한다: {scope!r}")

    # ★ 순서 주의 — 예약을 지우기 전에 **먼저 어느 POI 인지 기억**해 둔다.
    #   force_clear 가 세션을 failed 로 바꾸면 poi 를 더 못 읽는다.
    # ★ 2026-09-23 — 범위를 넓혔다.
    #   종전에는 **그 로봇 세션이 물고 있던 POI** 만 취소했다. 그런데 현장에서
    #   문제가 된 건 **다른 호출로 만들어진 대기 예약**이었다 (17:04:11 poi=1609).
    #   그건 세션 POI 가 아니라 한 건도 안 지워졌고("대기 호출 0건 취소"),
    #   워커가 죽으면서 그 예약을 물고 새 작업을 시작했다.
    #
    #   현장 정의 — 전체 강제 종료 = 현재 작업 + **예약된 작업 전부** 종료.
    #   그래서 대기 중인 예약을 전부 대상으로 한다.
    poi_ids = (_session_poi_ids(robot_id) | _all_waiting_reservation_pois())         if scope == "all" else set()

    ok, msg = force_clear(robot_id, after=("charge" if scope == "all" else "reserve"))
    cleared = 1 if ok else 0

    if scope != "all" or not poi_ids:
        logger.warning(f"[dispatch] ★ 로봇 강제 종료(scope={scope}) robot={robot_id} — {msg}")
        return {"cleared": cleared, "cancelled": 0}

    # 워커가 빠져나가기를 기다린다. 먼저 지우면 워커의 롤백이 뒤에 실행돼
    # 취소한 호출이 되살아난다 (force_clear_all 의 같은 주석 참조).
    deadline = time.time() + FORCE_CLEAR_DRAIN_SEC
    while time.time() < deadline:
        if _get_worker(robot_id) is None:
            break
        time.sleep(0.2)
    alive = _get_worker(robot_id) is not None

    cancelled = _cancel_reservations_for(poi_ids, "로봇 강제 종료(전체)")
    if alive:
        # 워커가 제때 안 죽었다 — 뒤늦게 예약을 되살릴 수 있으므로 한 번 더 지운다.
        logger.warning(f"[dispatch] 로봇 강제 종료 — 워커 robot={robot_id} 가 아직 살아 있음. "
                       f"5초 뒤 예약을 한 번 더 정리한다")
        safe_thread(target=_recancel_robot_reservations_later, args=(5.0, poi_ids),
                    name=f"force-clear-recancel-{robot_id}").start()

    logger.warning(f"[dispatch] ★ 로봇 강제 종료(전체) robot={robot_id} — "
                   f"대기 호출 {cancelled}건 취소 (다른 로봇은 그대로)")
    return {"cleared": cleared, "cancelled": cancelled}


def _cancel_reservations_for(poi_ids: set[int], reason: str) -> int:
    """지정한 POI 들의 대기 예약만 취소한다. 없으면 조용히 0."""
    if not poi_ids:
        return 0
    n = 0
    db = SessionLocal()
    try:
        for pid in sorted(poi_ids):
            try:
                if dispatch_crud.cancel_reservation(db, pid):
                    n += 1
                    _reserve_log("cancelled", None, pid, reason=reason)
            except Exception:
                logger.exception(f"[dispatch] 예약 취소 실패(계속) poi={pid}")
    finally:
        db.close()
    return n


def _recancel_robot_reservations_later(delay: float, poi_ids: set[int]) -> None:
    """로봇별 전체 종료 뒤 되살아난 예약을 한 번 더 지운다 (위 주석 참조)."""
    time.sleep(delay)
    try:
        _cancel_reservations_for(poi_ids, "로봇 강제 종료(전체) — 지연 정리")
    except Exception:
        logger.exception("[dispatch] 로봇별 지연 예약 정리 실패")


def _recancel_reservations_later(delay: float) -> None:
    """전체 강제 종료 뒤 되살아난 예약을 한 번 더 지운다 (위 주석 참조)."""
    time.sleep(delay)
    db = SessionLocal()
    try:
        poi_ids = dispatch_crud.cancel_all_waiting_reservations(db)
    except Exception:
        logger.exception("[dispatch] 지연 예약 정리 실패")
        return
    finally:
        db.close()
    for pid in poi_ids:
        _reserve_log("cancelled", None, pid, reason="전체 강제 종료 — 지연 정리")


# ── 재시작 복구 ───────────────────────────────────────────────


def recover_on_startup() -> None:
    """서버 재시작 시 미완료 세션 복구.

    상태별 처리:
      returning            → 복귀 시퀀스 재개 (랙 반납 → 충전소). 랙을 든 채 방치하지 않는다.
      starting/picking_up  → 랙 픽업 전이므로 대기장소에서 픽업부터 재개.
                             (단 잭에 하중이 이미 있으면 픽업은 끝난 것으로 보고 생략)
      moving 이후          → awaiting_next 로 정상화. 로봇을 세우고 실제 위치로 도착지를 복원해
                             콘솔이 '도착'으로 표시할 수 있게 한다(사람이 다음 명령 결정).

    ⚠ 이 함수는 startup 이벤트에서 동기 실행되므로 서버 기동을 그만큼 지연시킨다.
      로봇 조회는 RECOVER_ROBOT_TIMEOUT 으로 짧게 끊고, 실패 시 안전한 폴백을 쓴다.
    """
    db = SessionLocal()
    try:
        active = dispatch_crud.list_active_sessions(db)
    finally:
        db.close()

    for session in active:
        robot = _load_robot(session.robot_id)
        if not robot or not robot.ip_address:
            _set_db_status(session.id, "failed", error="복구 시 로봇 정보 없음")
            continue

        if session.status == "returning":
            # 복귀(랙 반납 → 충전소) 도중에 끊긴 것.
            # 세션만 failed 로 끝내면 로봇이 랙을 든 채 그 자리에 방치되고, 콘솔에는
            # 호출 버튼이 살아난다. 그 상태로 호출하면 랙을 든 채 align_with_rack 을 시도해
            # "jack is in up state" 로 실패한다 (2026-08-18 실기 확인).
            # → 워커를 end_flag 로 띄워 복귀 시퀀스만 다시 수행시킨다.
            #   _worker_loop 는 end_flag 를 보면 메인 루프를 건너뛰고 바로 종료 시퀀스
            #   (_return_to_standby_and_park → 랙 반납 → 충전소 도킹)로 진입한 뒤 completed 로 마감한다.
            try:
                jack_service.cancel_current_move(robot.ip_address, timeout=RECOVER_ROBOT_TIMEOUT)
            except Exception as e:
                logger.warning(f"[dispatch] 복귀 재개 — 이동 취소 실패(무시): {e}")

            ret_use_jack = (robot.robot_type != "serving")
            ret_with_rack = bool(session.with_rack) if session.with_rack is not None else True
            if not ret_use_jack:
                ret_with_rack = False
            # 잭이 내려가 있으면 랙은 이미 반납된 것 → 대기장소 경유를 생략하고 충전소만 간다.
            # (조회 실패(None)면 원래 값을 유지 — 랙을 든 채 두는 것보다 반납을 시도하는 편이 안전)
            #
            # ★ 2026-09-28 — 여기서도 잭업 기록(`is_laden`)을 같이 본다.
            #   is_rack_loaded() 는 잭업이 미완이면 랙을 들고 있어도 False 를 준다.
            #   그 False 를 그대로 믿으면 **랙을 든 채 충전소로 직행**한다.
            if ret_with_rack and jack_service.is_rack_loaded(
                robot.ip_address, timeout=RECOVER_ROBOT_TIMEOUT) is False:
                if jack_service.is_laden(robot.ip_address):
                    logger.warning(f"[dispatch] 복귀 재개 — session={session.id} 잭은 '내려감' 이지만 "
                                   f"잭업 기록이 있다 → 랙 반납 경로를 유지한다")
                else:
                    ret_with_rack = False
                    logger.info(f"[dispatch] 복귀 재개 — session={session.id} 랙 이미 반납됨 → 충전소만")

            ret_worker = _Worker(
                robot_id=session.robot_id,
                session_id=session.id,
                robot_ip=robot.ip_address,
                area_id=int(robot.area_id) if robot.area_id else None,
                with_rack=ret_with_rack,
                use_jack=ret_use_jack,
            )
            ret_worker.end_flag = True  # 메인 루프 건너뛰고 곧바로 종료(복귀) 시퀀스로
            rt = safe_thread(
                target=_worker_loop, args=(ret_worker,), kwargs={"skip_pickup": True},
                name=f"dispatch-return-{session.robot_id}",
            )
            ret_worker.thread = rt
            _set_worker(ret_worker)
            rt.start()
            logger.warning(
                f"[dispatch] 재시작 복구 — session={session.id} 복귀 중 끊김 → "
                f"복귀 시퀀스 재개 (랙 반납={ret_with_rack})"
            )
            continue

        # 서빙 로봇은 잭 동작 없음 — 복구 시에도 동일하게 반영해야 종료 때 잭다운을 시도하지 않음
        use_jack = (robot.robot_type != "serving")
        rec_with_rack = bool(session.with_rack) if session.with_rack is not None else True
        if not use_jack:
            rec_with_rack = False

        # ── 랙 픽업을 마쳤는지 판정 ──
        # 잭 업 전에 끊기면 랙을 안 들고 있는데, 그대로 awaiting_next 로 넘기면
        # 픽업 단계를 건너뛰어 빈 몸으로 경유지를 돌아버린다 (2026-08-18 실기 확인).
        #
        # 판정 기준은 세션 상태값. starting/picking_up = 잭 업 이전 단계 = 랙 없음.
        # (moving 이후는 픽업이 끝났다는 뜻이므로 건드리지 않는다. 작업 중 잭이 내려간 것을
        #  픽업 미완료로 오인해 로봇이 갑자기 대기장소로 가버리는 것을 막기 위함)
        # 잭 하중은 '이미 들고 있으면 재픽업하지 않는다'는 안전장치로만 쓴다.
        # 잭 업 직후 상태 기록 전에 끊긴 드문 경우, 랙을 든 채 align_with_rack 을 재시도해
        # 충돌(506)이 나는 것을 막는다.
        resume_pickup = False
        # first_poi_id 가 없으면 어디로 보낼지 알 수 없어 픽업 재개가 성립하지 않는다.
        if rec_with_rack and session.first_poi_id and session.status in ("starting", "picking_up"):
            loaded = jack_service.is_rack_loaded(robot.ip_address, timeout=RECOVER_ROBOT_TIMEOUT)
            resume_pickup = (loaded is not True)
            if loaded is True:
                logger.warning(
                    f"[dispatch] 재시작 복구 — session={session.id} 상태는 {session.status} 지만 "
                    f"잭에 하중이 있음 → 픽업은 끝난 것으로 보고 재픽업 생략"
                )
            elif loaded is None:
                logger.warning(
                    f"[dispatch] 재시작 복구 — 잭 상태 조회 실패, 세션 상태({session.status})만으로 판정"
                )

        if resume_pickup:
            # 랙을 안 들고 있다 → 픽업부터 재개.
            # _worker_loop 는 첫 목적지를 target_poi_id 에서 읽으므로 그것부터 세운다.
            _clear_waypoints(session.id)
            _set_target_poi(session.id, session.first_poi_id)
            _set_db_status(session.id, "picking_up")
            logger.warning(
                f"[dispatch] 재시작 복구 — session={session.id} 랙 픽업 전에 끊김 → "
                f"대기장소에서 픽업부터 재개 (첫 목적지 poi={session.first_poi_id})"
            )

        # 아래는 픽업이 끝난 세션만 — awaiting_next 로 정상화하고 도착 위치를 복원한다.
        # 남은 경유지/이동 목표는 정리한다 (안 그러면 그 POI들이 계속 점유로 잡힘)

        # 재시작 시점에 로봇이 '이동 중'이었으면 로봇을 세우고 실제 위치로 도착지를 다시 잡는다.
        #
        # 두 경우를 모두 잡아야 한다.
        #  (a) status == "moving"  — 경유지 이동 중. current_poi_id 에는 직전 POI 가 남아 있어
        #      그대로 두면 콘솔이 '직전 POI 에 도착'으로 표시하는데 로봇은 다음 POI 로 가버린다.
        #      (2026-08-18 실기 확인: J1→J2 이동 중 재시작 시 화면은 J1, 로봇은 J2)
        #  (b) current_poi_id 가 비어 있음 — 첫 이동 중이라 도착 기록이 아예 없다.
        #      이대로 두면 콘솔이 '도착'으로 못 띄워 경유지 등록 창이 영영 안 뜬다.
        #
        # awaiting_next / awaiting_confirm 은 로봇이 이미 멈춰 있고 current_poi_id 도 정확하므로
        # 건드리지 않는다 (불필요하게 로봇 조회로 기동을 늦추지 않기 위함).
        #
        # 로봇 실제 포즈를 읽어 가장 가까운 POI 로 판정하고, 실패하면 목적지로 가정한다.
        # ⚠ _set_current_poi 는 target_poi_id 도 함께 비우므로 _set_target_poi 보다 먼저 호출할 것.
        if not resume_pickup and (session.status == "moving" or session.current_poi_id is None):
            # 재시작 시점에 로봇이 아직 이동 중일 수 있다. 세우지 않으면 백엔드는
            # '여기 있다'고 기록하는데 로봇은 목적지까지 계속 가버려 화면과 실제가 어긋난다.
            try:
                jack_service.cancel_current_move(robot.ip_address, timeout=RECOVER_ROBOT_TIMEOUT)
                time.sleep(1.0)  # 정지가 반영된 뒤에 포즈를 읽어야 정확
            except Exception as e:
                logger.warning(f"[dispatch] 복구 시 이동 취소 실패(무시): {e}")

            recovered = None
            try:
                import math
                from app.routers.map import _read_tracked_pose  # 순환 import 방지 — 함수 안에서
                pose = _read_tracked_pose(robot.ip_address, timeout=RECOVER_ROBOT_TIMEOUT)
                if pose and pose.get("position"):
                    px, py = pose["position"][0], pose["position"][1]
                    pdb = SessionLocal()
                    try:
                        # ⚠ 맵/영역으로 좁히지 않으면 옛 맵의 POI 가 잡힌다.
                        #   구 맵도 is_active=1 로 남아 있어 전체 조회하면 죽은 좌표를 고른다.
                        from app.models.map import RobotMap
                        active_map = (
                            pdb.query(RobotMap)
                            .filter(
                                RobotMap.area_id == robot.area_id,
                                RobotMap.is_active == True,  # noqa: E712
                            )
                            .order_by(RobotMap.updated_at.desc())
                            .first()
                        )
                        cands = (
                            pdb.query(MapPOI).filter(
                                MapPOI.map_id == active_map.id,
                                MapPOI.is_active == True,  # noqa: E712
                                MapPOI.poi_type == "jack",
                                MapPOI.world_x.isnot(None),
                                MapPOI.world_y.isnot(None),
                            ).all()
                            if active_map else []
                        )
                        near = sorted(
                            (math.hypot(px - p.world_x, py - p.world_y), p.id) for p in cands
                        )
                        if near and near[0][0] <= RECOVER_POSE_MATCH_M:
                            recovered = near[0][1]
                    finally:
                        pdb.close()
            except Exception as e:
                logger.warning(f"[dispatch] 복구 포즈 판정 실패 — 목적지 가정으로 폴백: {e}")

            fallback = recovered or session.target_poi_id or session.first_poi_id
            if fallback:
                _set_current_poi(session.id, fallback)
                logger.warning(
                    f"[dispatch] 재시작 복구 — session={session.id} 도착 위치 미기록 → "
                    f"poi={fallback} ({'실측 포즈' if recovered else '목적지 가정 — 실제 위치 확인 필요'})"
                )

        if not resume_pickup:
            _clear_waypoints(session.id)
            _set_target_poi(session.id, None)
            _set_db_status(session.id, "awaiting_next")

        worker = _Worker(
            robot_id=session.robot_id,
            session_id=session.id,
            robot_ip=robot.ip_address,
            area_id=int(robot.area_id) if robot.area_id else None,
            with_rack=rec_with_rack,
            use_jack=use_jack,
        )
        t = safe_thread(target=_worker_loop, args=(worker,), kwargs={"skip_pickup": not resume_pickup},
                        name=f"dispatch-recover-{session.robot_id}")
        worker.thread = t
        _set_worker(worker)
        t.start()
        logger.info(f"[dispatch] 재시작 복구 — robot_id={session.robot_id} session={session.id}")

    # 복구 후, 가용 로봇이 남아있고 대기 예약이 있으면 자동 호출
    try:
        try_fulfill_reservations()
    except Exception:
        logger.exception("[dispatch] 복구 후 예약 자동 처리 실패")


# ── POI 호출 (위치별 태블릿용) ────────────────────────────────


def find_available_robot(area_id: Optional[int] = None,
                         robot_type: Optional[str] = None) -> Optional[Robot]:
    """가용 로봇 1대 선정.

    조건:
      - is_active=True, ip_address 있음
      - 같은 area_id (있을 경우)
      - 같은 robot_type (있을 경우 — 'lifting'/'serving')
      - 활성 워커 없음 (= 다른 호출에 배정 안 됨)
      - **현재 온라인** (관제와 동일한 라이브 체크 — `fetch_all_robots_live`)

    정렬: 배터리 내림차순 → robot_id 오름차순
    """
    db = SessionLocal()
    try:
        q = db.query(Robot, RobotStatus).outerjoin(RobotStatus, RobotStatus.robot_id == Robot.id) \
              .filter(Robot.is_active == True, Robot.ip_address != None)
        if area_id is not None:
            q = q.filter(Robot.area_id == str(area_id))
        if robot_type is not None:
            q = q.filter(Robot.robot_type == robot_type)
        rows = q.all()
    finally:
        db.close()

    candidates = []
    for robot, stat in rows:
        if is_robot_busy(robot.id):      # 워커 + 강제 종료 후속 동작
            continue
        battery_val = stat.battery_level if (stat and stat.battery_level is not None) else None
        if battery_val is not None:
            min_batt = robot.min_battery if robot.min_battery is not None else 20
            if battery_val < min_batt:
                continue  # 배터리 부족 — 충전 우선, 배정에서 제외
        battery = battery_val if battery_val is not None else -1
        candidates.append((battery, robot.id, robot))

    if not candidates:
        return None
    # 배터리 내림차순, 동률이면 ID 오름차순
    candidates.sort(key=lambda x: (-x[0], x[1]))

    # 라이브 ONLINE 체크 — TTL 캐시 사용 (매 요청마다 수 초 걸리는 것 방지)
    online_ips = online_ips_cached([r.ip_address for _, _, r in candidates])

    for battery, rid, robot in candidates:
        if robot.ip_address in online_ips:
            return robot
        logger.info(f"[find_available_robot] {robot.name} ({robot.ip_address}) Offline — 제외")
    return None


def get_session_at_poi(poi_id: int) -> Optional[DispatchSession]:
    """그 POI에 도착해서 awaiting_next 상태로 있는 세션."""
    db = SessionLocal()
    try:
        return (
            db.query(DispatchSession)
            .filter(
                DispatchSession.current_poi_id == poi_id,
                DispatchSession.status == "awaiting_next",
            )
            .order_by(DispatchSession.id.desc())
            .first()
        )
    finally:
        db.close()


def get_arrived_session_at_poi(poi_id: int) -> Optional[DispatchSession]:
    """그 POI에 **도착해 있는** 세션 — 대기 종류를 가리지 않는다.

    get_session_at_poi() 는 awaiting_next 만 인정하므로, 경유지 순회 중
    (awaiting_confirm / moving+target NULL) 에는 세션을 못 찾아 404 가 난다.
    화면(_poi_status)은 이 세 가지를 모두 "도착"으로 그리므로 불일치가 생긴다.

    종료처럼 **어떤 대기 상태에서도 가능해야 하는 조작**에만 사용할 것.
    다음 위치 지정/경로 등록은 진행 중인 경유지와 꼬이므로 여기에 쓰지 않는다.
    """
    db = SessionLocal()
    try:
        from sqlalchemy import and_, or_
        return (
            db.query(DispatchSession)
            .filter(
                DispatchSession.current_poi_id == poi_id,
                or_(
                    DispatchSession.status == "awaiting_next",
                    DispatchSession.status == "awaiting_confirm",
                    and_(
                        DispatchSession.status == "moving",
                        DispatchSession.target_poi_id.is_(None),
                    ),
                ),
            )
            .order_by(DispatchSession.id.desc())
            .first()
        )
    finally:
        db.close()


def get_session_heading_to_poi(poi_id: int) -> Optional[DispatchSession]:
    """그 POI로 이동/준비 중인 세션."""
    db = SessionLocal()
    try:
        return (
            db.query(DispatchSession)
            .filter(
                DispatchSession.target_poi_id == poi_id,
                DispatchSession.status.in_(("starting", "picking_up", "moving")),
            )
            .order_by(DispatchSession.id.desc())
            .first()
        )
    finally:
        db.close()


def _poi_area_id(poi_id: int) -> Optional[int]:
    """POI가 속한 area_id (robot_maps.area_id 경유). 없으면 None."""
    db = SessionLocal()
    try:
        p = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
        if p and p.map_id:
            from app.models.map import RobotMap
            rm = db.query(RobotMap).filter(RobotMap.id == p.map_id).first()
            if rm and rm.area_id is not None:
                return int(rm.area_id)
    finally:
        db.close()
    return None


def _assign_available_robot(poi_id: int, with_rack: bool,
                            robot_type: Optional[str] = None) -> tuple[bool, str, Optional[int]]:
    """가용 로봇 1대를 그 POI에 배정(start_session). 경쟁 시 다음 후보 재시도.

    robot_type 지정 시 그 타입('lifting'/'serving')만 배정.

    반환: (성공, 메시지, robot_id)
      - 성공: (True, "ok", robot_id)
      - 가용 로봇 없음: (False, "no_robot", None)  ← 호출자가 예약으로 전환
      - 기타 실패: (False, 메시지, None)
    """
    poi_area_id = _poi_area_id(poi_id)
    for _ in range(5):
        robot = find_available_robot(area_id=poi_area_id, robot_type=robot_type)
        if not robot:
            # 같은 area 없으면 전체에서 한번 더 시도 (필요 시)
            robot = find_available_robot(area_id=None, robot_type=robot_type)
        if not robot:
            return False, "no_robot", None

        ok, msg = start_session(robot.id, poi_id, with_rack=with_rack)
        if ok:
            return True, "ok", robot.id
        if msg == "이미 진행 중인 배차가 있습니다":
            # 다른 호출이 방금 이 로봇을 가져감 — 다음 후보 재선정
            time.sleep(0.05)
            continue
        return False, msg, None

    return False, "가용 로봇 배정 경합 — 잠시 후 다시 시도하세요", None


def call_to_poi(poi_id: int, robot_type: Optional[str] = None,
                with_rack: bool = True) -> tuple[bool, str, Optional[int], bool]:
    """그 POI로 가용 로봇 1대 배정해서 호출. 가용 로봇이 없으면 예약 생성.

    robot_type: 'lifting'/'serving' 지정 시 그 타입만. None이면 아무 타입.
    with_rack : (리프팅) True=렉 픽업 후 이동, False=렉 없이. 서빙은 무시(잭 없음).

    반환: (성공 여부, 메시지, 배정된 robot_id, 예약 여부)
      - 즉시 배차: (True, "ok", robot_id, False)
      - 예약 생성: (True, "reserved", None, True)
      - 거부:      (False, 사유, None, False)
    """
    # 0) 같은 POI 동시 호출 선점 — 이게 없으면 아래 점유 검증을 여러 요청이 동시에
    #    통과해(모두 "비어있음"으로 보고) 서로 다른 로봇이 같은 자리로 배정된다.
    if not _try_claim_poi(poi_id):
        return False, "다른 요청이 이 위치를 처리하는 중입니다. 잠시 후 다시 시도하세요", None, False

    try:
        # 1) 점유 검증 — current/target 뿐 아니라 '다른 로봇의 미방문 경유지'도 거부
        db = SessionLocal()
        try:
            occupied = dispatch_crud.occupied_poi_ids(db)
        finally:
            db.close()
        if poi_id in occupied:
            # 거부 이유가 로그에 안 남으면 "왜 예약이 안 생겼지" 를 나중에 추적할 수 없다.
            logger.info(f"[dispatch] 호출 거부 — poi={poi_id} 다른 로봇이 점유 중 (occupied={sorted(occupied)})")
            return False, "다른 로봇이 점유(또는 경유지로 예약) 중인 위치입니다", None, False

        poi = _load_poi(poi_id)
        if not poi:
            return False, "POI 조회 실패", None, False

        # 2) 가용 로봇 배정 시도
        ok, msg, robot_id = _assign_available_robot(poi_id, with_rack, robot_type=robot_type)
        if ok:
            # 이 POI에 남아있던 대기 예약이 있으면 소진 처리 (직접 배차로 충족됨)
            db = SessionLocal()
            try:
                r = dispatch_crud.get_waiting_reservation(db, poi_id)
                if r:
                    dispatch_crud.mark_reservation(db, r.id, "fulfilled")
                    _reserve_log("fulfilled", r.id, poi_id,
                                 reason="같은 POI 직접 호출로 충족")
            finally:
                db.close()
            return True, "ok", robot_id, False
        if msg != "no_robot":
            return False, msg, None, False

        # 3) 가용 로봇 없음 → 예약 생성 (FIFO 대기열). 로봇이 종료되면 자동 호출됨.
        db = SessionLocal()
        try:
            r = dispatch_crud.create_reservation(db, poi_id, with_rack=with_rack)
            logger.info(f"[dispatch] 예약 생성 — poi={poi_id} with_rack={with_rack}")
            _reserve_log("created", r.id, poi_id,
                         reason=f"가용 로봇 없음 with_rack={with_rack}")
        finally:
            db.close()
        return True, "reserved", None, True
    finally:
        # 세션이 생겼으면 이후 점유는 occupied_poi_ids 가 맡는다 — 여기서 반드시 푼다.
        _release_poi(poi_id)


# ── 예약 자동 처리 ────────────────────────────────────────────


def cancel_reservation_at_poi(poi_id: int) -> bool:
    """그 POI의 대기 중 예약을 취소. 취소된 게 있으면 True.

    ★ 2026-09-07 까지 무로그였다. 콘솔·태블릿의 [예약 취소] 버튼이 여기로 오는데,
      눌렀는지 안 눌렀는지 서버 로그로 구분할 방법이 없었다.
    """
    db = SessionLocal()
    try:
        done = dispatch_crud.cancel_reservation(db, poi_id)
        _reserve_log("cancelled" if done else "cancel-시도(대상없음)",
                     None, poi_id, reason="사용자 취소 요청(API)")
    finally:
        db.close()

    # ★ 2026-09-23 — 강제 종료 후속 동작이 **이 예약의 R 지점으로 가는 중**일 수 있다.
    #   대기 예약이 하나도 안 남았으면 그 이동은 갈 이유가 없다.
    #   종전에는 그대로 가서 **빈 자리에 들어가 섰다**(18:58:05 취소 → 18:58:21 R2 도착).
    if done:
        try:
            _stop_followups_without_reservation()
        except Exception:
            logger.exception("[dispatch] 예약 취소 후 후속 동작 정리 실패")
    return done


def _stop_followups_without_reservation() -> None:
    """대기 예약이 다 없어졌는데 그 예약 때문에 가던 후속 동작이 있으면 충전소로 돌린다."""
    with _followup_lock:
        running = list(_followup_robots)
    if not running:
        return
    db = SessionLocal()
    try:
        left = dispatch_crud.list_waiting_reservations(db) or []
    except Exception:
        return
    finally:
        db.close()
    if left:
        return                                   # 아직 갈 곳이 남아 있다
    for rid in running:
        cancel_followup(rid, "예약이 모두 취소됨", then_charge=True)


def _reserve_log(action: str, res_id: Optional[int], poi_id: Optional[int],
                 reason: str = "") -> None:
    """예약의 생성·소진·취소·롤백을 한 형식으로 남긴다.

    2026-09-07 추가. 그날 19:01 에 만든 예약이 1분 뒤 사라졌는데
    **어디서 없어졌는지 로그가 한 줄도 없어 원인을 못 짚었다.**
    예약을 조용히 없애는 경로가 두 곳이나 있었다 —
    `try_fulfill_reservations` 의 점유 소진과 취소 API. 둘 다 무로그였다.
    이제 `grep "[reserve]"` 하나로 예약 하나의 일생이 전부 보인다.
    """
    parts = [f"[reserve] {action}"]
    if res_id is not None:
        parts.append(f"id={res_id}")
    if poi_id is not None:
        parts.append(f"poi={poi_id}({_poi_name_for_log(poi_id)})")
    if reason:
        parts.append(f"사유={reason}")
    logger.info(" ".join(parts))


def _poi_name_for_log(poi_id: int) -> str:
    """로그용 POI 이름. 조회에 실패해도 로그 자체는 반드시 남아야 하므로 삼킨다."""
    db = SessionLocal()
    try:
        p = db.query(MapPOI).filter(MapPOI.id == poi_id).first()
        return p.name if p and p.name else "?"
    except Exception:
        return "?"
    finally:
        db.close()


def _mark_reservation(reservation_id: int, status: str, *,
                      poi_id: Optional[int] = None, reason: str = "") -> None:
    db = SessionLocal()
    try:
        dispatch_crud.mark_reservation(db, reservation_id, status)
    finally:
        db.close()
    _reserve_log(status, reservation_id, poi_id, reason)


def _release_reservation(worker: "_Worker", reservation_id: Optional[int],
                         poi_id: Optional[int], reason: str) -> None:
    """작업이 중간에 끝났을 때, 그 작업이 물고 있던 예약을 정리한다 (2026-09-29).

    ★ 현장 사고 — [현재 JOB 강제 종료]를 눌렀는데 **취소한 작업이 다시 실행됐다.**
      종전에는 이 자리가 무조건 `waiting` 으로 되돌렸다("호출 자체는 살려 다른 로봇이
      받게"). 그런데 되살아난 예약은 **원래 생성 시각을 그대로 가져서** 뒤에 건 예약보다
      앞선다. 후속 처리가 선착순으로 예약을 넘기니 방금 취소한 R1 을 다시 집었다.
        R1→J1 (복귀 중 호출을 이어받은 작업 — 예약 id 보유) + R2 예약
        → JOB 종료 → R1 예약 되살아남 → R1 '작업 중', R2 '예약됨' → 다시 R1 로
      직접 호출로 시작한 작업은 예약 id 가 없어 재현 때는 정상이었다.

      현장 정의(2026-09-23) — JOB 종료 = **지금 작업은 끝** + 예약된 작업이 있으면 그걸 한다.
      그래서 **강제 종료(abort)로 끝났으면 취소**로 닫는다. 강제 종료가 아닌 실패
      (이동 실패·매핑 오류 등)는 종전대로 되돌려 다음 기회에 다시 하게 한다.
    """
    if not reservation_id:
        return
    if getattr(worker, "abort_flag", False):
        _mark_reservation(reservation_id, "cancelled", poi_id=poi_id,
                          reason=f"강제 종료로 취소된 작업 — 다시 하지 않는다 ({reason})")
    else:
        _mark_reservation(reservation_id, "waiting", poi_id=poi_id, reason=reason)


def try_fulfill_reservations() -> None:
    """가용 로봇이 생겼을 때 대기 중 예약을 FIFO로 자동 호출.

    - 여러 로봇이 동시에 가용해질 수 있으므로 가용 로봇이 떨어질 때까지 반복.
    - 이미 그 POI에 로봇이 도착/이동 중이면 그 예약은 소진(fulfilled) 처리.
    - 동시 호출은 _reservation_lock 으로 직렬화 (한쪽이 처리 중이면 스킵).
    """
    if not _reservation_lock.acquire(blocking=False):
        return  # 이미 다른 스레드가 처리 중 — 그쪽이 끝까지 소화함
    try:
        while True:
            db = SessionLocal()
            try:
                reservations = dispatch_crud.list_waiting_reservations(db)
                # 점유(도착/이동 중/다른 로봇의 미방문 경유지) 상태
                occupied = dispatch_crud.occupied_poi_ids(db)
            finally:
                db.close()
            if not reservations:
                return

            progressed = False
            for res in reservations:
                # 그새 그 POI가 점유되면(로봇이 이미 가 있거나 가기로 정해짐) 예약 소진 처리.
                # ★ 이 경로는 2026-09-07 까지 로그가 한 줄도 없어서, 예약이 사라져도
                #   아무 흔적이 남지 않았다. 반드시 사유와 점유 목록을 함께 남긴다.
                if res.poi_id in occupied:
                    _mark_reservation(res.id, "fulfilled", poi_id=res.poi_id,
                                      reason=f"이미 점유된 POI — occupied={sorted(occupied)}")
                    progressed = True
                    break

                # 작업지점에 아직 랙이 있으면 배차하지 않는다 — 예약은 waiting 으로 유지.
                # 작업자가 J 태블릿에서 [확인]을 눌러 치움을 알리면 그때 풀린다.
                # (소진 처리하면 안 된다. 예약이 사라져 호출이 통째로 없어진다)
                if rack_occupied_at_poi(res.poi_id):
                    continue

                # 콘솔 직접 호출(call_to_poi)과 같은 POI 를 동시에 잡지 않도록 선점.
                # 이미 처리 중이면 그 예약은 이번 회차에서 건너뛴다(다음 기회에 재시도).
                if not _try_claim_poi(res.poi_id):
                    continue
                try:
                    ok, msg, robot_id = _assign_available_robot(res.poi_id, bool(res.with_rack))
                finally:
                    _release_poi(res.poi_id)
                if ok:
                    _mark_reservation(res.id, "fulfilled", poi_id=res.poi_id,
                                      reason=f"예약 자동 호출 robot={robot_id}")
                    logger.info(f"[dispatch] 예약 자동 호출 — poi={res.poi_id} robot={robot_id}")
                    progressed = True
                    break
                if msg == "no_robot":
                    return  # 가용 로봇 없음 — 다음 종료 때 다시 시도
                # 기타 실패 — 그 예약은 건너뛰고 다음 예약 시도
                logger.warning(f"[dispatch] 예약 자동 호출 실패 poi={res.poi_id}: {msg}")

            if not progressed:
                return  # 처리할 수 있는 예약이 없음
    finally:
        _reservation_lock.release()


# ══════════════════════════════════════════════════════════════════
#  2026-08-24 변경 시나리오 — R→J 단방향 배송 모드
#
#    [충전소] ─(J 호출)→ 매핑된 R 에서 랙 적재 → J 로 이동 → 하차 → 이탈
#              → 대기 예약 있으면 그 J 의 R 로 이어서 / 없으면 충전소 복귀
#
#  기존 인터랙티브(콘솔 경유지 등록) 모드는 건드리지 않는다. 호출된 POI 에
#  job_points 매핑이 있을 때만 이 모드로 들어오므로, 매핑이 없는 POI 의
#  동작은 종전과 완전히 동일하다.
# ══════════════════════════════════════════════════════════════════

# 로봇 뒤끝까지 거리 (m). /robot_model 실측 — footprint y 최소 -0.369.
# (/robot_model 은 0.08 Hz 라 매번 읽을 수 없어 상수로 둔다)
ROBOT_REAR_M = 0.369

# 랙 밑에서 빠져나오는 전진 거리 (m).
#
# ★ 0.7 → 계산값 (2026-09-07 현장 사고).
#   0.7 m 로는 **로봇 뒤끝이 아직 랙 안에 남는다.**
#     S300 기준 이탈 후 뒤끝 = 0.7 - 0.369 = 0.331 m,  랙 다리는 0.3825 m
#     → 5 cm 앞에 다리를 두고 서 있다가 다음 이동이 조금만 후진해도 들이받는다.
#       (`control.backward_movement_behavior = "when_necessary"` 라 후진은 흔하다)
#   필요 = 랙 반깊이 + 로봇 뒤끝 + 여유.
#   등록된 규격 중 가장 깊은 것을 기준으로 잡아야 어떤 랙에도 안전하다.
def _rack_escape_distance() -> float:
    from app.constants.rack_specs import RACK_SPECS
    rear = 0.0
    for sp in RACK_SPECS.values():
        mg = sp.get("margin")
        m0 = mg[0] if isinstance(mg, list) else (mg or 0.0)
        rear = max(rear, float(sp["depth"]) / 2 + float(m0))
    return round(rear + ROBOT_REAR_M + 0.15, 2)      # 0.15 = 여유


RACK_ESCAPE_M = _rack_escape_distance()


def job_mapping_for_poi(poi_id: int) -> Optional[dict]:
    """J POI id → {'j_name', 'r_name', 'area_id'}. 매핑이 없으면 None.

    None 이면 호출자는 기존 인터랙티브 모드로 진행한다.
    """
    db = SessionLocal()
    try:
        poi = db.query(MapPOI).filter(MapPOI.id == poi_id, MapPOI.is_active == True).first()
        if not poi:
            return None
        area_id = None
        if poi.map_id:
            m = db.query(RobotMap).filter(RobotMap.id == poi.map_id).first()
            area_id = int(m.area_id) if (m and m.area_id is not None) else None
        row = dispatch_crud.get_job_point(db, area_id, poi.name)
        if not row:
            return None
        return {"j_name": row.j_poi_name, "r_name": row.r_poi_name, "area_id": area_id}
    finally:
        db.close()


def rack_occupied_at_poi(poi_id: int) -> bool:
    """그 작업지점(J)에 아직 랙이 놓여 있나.

    매핑이 없는 POI(기존 인터랙티브 모드)는 항상 False — 종전 동작을 바꾸지 않는다.
    HTTP 호출 경로(routers.poi_call)에는 이미 같은 검사가 있으나, **예약이 자동으로
    소화되는 경로**에는 없었다. 그쪽은 로봇 점유(occupied_poi_ids)만 보기 때문에
    랙 점유는 통과해버린다. 이름이 비슷하지만 완전히 다른 값이다:
      occupied_poi_ids()   = 다른 로봇이 그 자리를 목표로 잡고 있다 (dispatch_sessions)
      job_points.occupied  = 그 자리에 랙이 놓여 있다               (job_points)
    """
    db = None
    try:
        m = job_mapping_for_poi(poi_id)   # try 안에 둔다 — 여기서도 DB 를 친다
        if not m:
            return False
        db = SessionLocal()
        row = dispatch_crud.get_job_point(db, m["area_id"], m["j_name"])
        return bool(row and row.occupied)
    except Exception:
        # 조회 실패로 배차를 막지는 않는다. 여기서 예외를 흘리면 배송 워커가
        # 랙을 든 채 죽어(세션 failed) 사람이 수동 복구해야 한다.
        logger.exception(f"[dispatch] 랙 점유 조회 실패(무시) — poi={poi_id}")
        return False
    finally:
        if db is not None:
            db.close()


def _load_poi_by_name(area_id: Optional[int], name: str) -> Optional[dict]:
    """이름으로 POI 조회. 매핑을 이름으로 저장하므로 여기서 실제 좌표를 찾는다."""
    db = SessionLocal()
    try:
        q = (db.query(MapPOI)
             .join(RobotMap, MapPOI.map_id == RobotMap.id)
             .filter(MapPOI.name == name, MapPOI.is_active == True,
                     RobotMap.is_active == True))
        if area_id is not None:
            q = q.filter(RobotMap.area_id == area_id)
        p = q.order_by(MapPOI.id.desc()).first()
        if not p or p.world_x is None or p.world_y is None:
            return None
        return {"id": p.id, "name": p.name, "x": float(p.world_x),
                "y": float(p.world_y), "ori": float(p.angle or 0)}
    finally:
        db.close()


def _set_j_occupied(area_id: Optional[int], j_name: str, occupied: bool) -> None:
    """J 에 랙을 놓았으면 True. 작업자가 치우고 [확인]을 누르면 False 로 풀린다."""
    db = SessionLocal()
    try:
        dispatch_crud.set_job_point_occupied(db, area_id, j_name, occupied)
    except Exception:
        logger.exception(f"[delivery] 점유 상태 기록 실패 — {j_name}={occupied}")
    finally:
        db.close()


def _pickup_at_poi(worker: _Worker, poi: dict) -> bool:
    """지정 POI(랙 보관 위치 R)에서 align_with_rack → jack_up.

    align 은 jack_service.align_with_retry 가 최대 4회(재로컬화·후진·측면우회)까지
    시도한다. 그걸 다 쓰고도 실패하면 **랙이 없는 것으로 간주**한다.
    """
    # 랙에서 멀리 떨어져 있으면 align 이 스스로 통로를 못 찾는다 — 경유지로 먼저 접근.
    _approach_before_align(worker, poi)

    jack_service.update_job_status(worker.robot_ip, message=f"{poi['name']} 이동 — 랙 정렬")
    # 도킹(랙 정렬) 실패 안내를 콘솔 + 이 R 태블릿에 띄운다 (LGIT 요청 2번).
    # ★ 이 경로(배송 모드 R 픽업)에서만 켠다. 인터랙티브 모드의 align 에서 뜨면
    #   "대차를 정위치해 주세요" 가 랙과 무관한 화면에 뜬다.
    notify_ctx = {
        "poi_id": poi.get("id"),
        "poi_name": poi.get("name"),
        "poi_label": _poi_label(poi.get("name")),
    }
    try:
        result = jack_service.align_with_retry(worker.robot_ip, poi["x"], poi["y"], poi["ori"],
                                               notify_ctx=notify_ctx)
    except RuntimeError:
        # 사용자 중지(_check_stop / zone 대기 중 중지) 신호. jack_service 에서
        # RuntimeError 를 쓰는 곳은 이 둘뿐이다.
        #
        # 여기서 삼키면 두 가지가 망가진다(2026-08-24 19:50 실측):
        #   ① 중지를 "랙이 없습니다" 로 잘못 기록한다 (거짓 rack_missing 이력)
        #   ② _check_stop 이 플래그를 pop 해버려 **중지가 소비된다** — 대기 예약이
        #      있었다면 로봇이 중지 명령을 받고도 다음 작업을 시작한다.
        # 그대로 올려보내 워커 루프가 중지로 처리하게 한다(세션 failed + 예약 롤백).
        raise
    except Exception as e:
        logger.error(f"[delivery] align 예외 — {poi['name']}: {e}")
        return False
    if str(result.get("state", "")).lower() != "succeeded":
        logger.warning(f"[delivery] {poi['name']} 랙 정렬 실패(재시도 소진) — 랙 없음으로 간주: {result}")
        return False

    jack_service.update_job_status(worker.robot_ip, message="잭 업 — 랙 적재")
    try:
        jack_service.jack_up(worker.robot_ip)
    except RuntimeError:
        raise                      # 중지 신호 — 위와 같은 이유로 삼키지 않는다
    except Exception as e:
        logger.error(f"[delivery] jack_up 실패: {e}")
        return False
    time.sleep(JACK_SETTLE_SEC)
    # 좁은 랙 자리에서 돌지 않도록 진입점으로 먼저 나온다 (2026-09-14)
    _escape_after_pickup(worker, poi)
    return True


def _move_to_jack_point(worker: _Worker, poi: dict) -> bool:
    """랙을 싣고 하차 지점 J 로 이동한다.

    진입점("<J이름>-1") 이 있으면 **2단계**로 간다.
      1) 진입점까지 — 경유지 주행 + 마지막 구간 자율(회피)
      2) 진입점 → J — `to_unload_point`. 로봇이 랙을 내려놓을 자세로
         **스스로 진입**한다(후진 진입 포함). 안전존은 이 동작을 정밀 동작으로
         보고 판정을 쉰다(safety_zone.PRECISE_ACTIONS).

    진입점이 없으면 **기존 동작 그대로** — 경유지 경로로 J 좌표까지 직행.
    """
    entry = _find_entry_poi(worker.area_id, poi["name"])
    if not entry:
        return _move_to_poi(worker, poi)

    if not _move_to_entry_poi(worker, entry, poi):
        logger.warning(
            f"[delivery] {poi['name']} 진입점({entry['name']}) 도착 실패 "
            f"— to_unload_point 는 그대로 시도한다")

    jack_service.update_job_status(
        worker.robot_ip, message=f"{poi['name']} 하차 위치 진입")
    logger.info(
        f"[delivery] {poi['name']} to_unload_point 진입 "
        f"(진입점 {entry['name']} 에서, 랙 각도 {math.degrees(poi['ori']):.1f}°)")
    try:
        r = jack_service.safe_move(worker.robot_ip, "to_unload_point",
                                   poi["x"], poi["y"], poi["ori"],
                                   max_attempts=8, timeout=120)
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning(f"[delivery] to_unload_point 실패: {e}")
        return False
    return str(r.get("state", "")).lower() == "succeeded"

def _dropoff_at_poi(worker: _Worker, poi: dict) -> None:
    """작업지점 J 에서 잭 다운(하차) 후 랙 밑에서 전진 이탈.

    이탈하지 않으면 로봇이 랙 아래에 남아 다음 이동이 막힌다.
    실패해도 다음 단계로 진행한다(이동 자체가 이탈을 겸하는 경우가 있음).
    """
    _jack_down_step(worker, label=f"잭 다운 — {poi['name']} 하차")

    # ★ 2026-09-29 — 내림 완료를 확인하지 못했으면 **이탈하지 않고 멈춘다.**
    #   종전에는 경고만 남기고 이탈·다음 작업으로 넘어갔다. 반쯤 내려온 랙 아래에서
    #   움직이면 랙을 끌고 간다. 이 워커는 예약을 이어받지 않게 하고 빠져나간다 —
    #   이어받으면 멈춰야 할 로봇이 곧바로 다시 출발한다. 적재 표시가 남아 있어
    #   사람이 확인하기 전에는 새 배차도 붙지 않는다(_busy_robot_ids 참조).
    if not jack_service.jack_settled(worker.robot_ip):
        worker.no_takeover = True
        msg = f"{poi['name']} 하차 — 랙 내림 완료를 확인하지 못해 멈춥니다. 현장 확인 필요"
        jack_service.update_job_status(worker.robot_ip, status="error", message=msg)
        raise jack_service.JackBusy(msg)

    jack_service.update_job_status(worker.robot_ip, message="랙 아래에서 이탈 중")
    # ★ 2026-09-29 — 이탈 방향을 POI 방향이 아니라 **로봇의 지금 방향**으로 잡는다.
    #   POI 방향으로 주면 실제 자세가 조금만 달라도 로봇이 **랙 아래에서 먼저 돈 뒤**
    #   전진한다(강제 종료 쪽 `drive_straight` 주석과 같은 문제). 지금 방향 그대로
    #   앞 지점을 주면 회전할 이유가 없다. 포즈를 못 읽으면 종전대로 POI 기준.
    pose = _current_pose(worker.robot_ip)
    if pose is not None:
        dev = math.degrees(math.atan2(math.sin(pose[2] - poi["ori"]),
                                      math.cos(pose[2] - poi["ori"])))
        logger.info(f"[delivery] {poi['name']} 이탈 — 지금 방향 {math.degrees(pose[2]):.1f}° "
                    f"그대로 전진 (POI 방향과 차 {dev:+.1f}°)")
        ex_ori = pose[2]
        fx = pose[0] + RACK_ESCAPE_M * math.cos(ex_ori)
        fy = pose[1] + RACK_ESCAPE_M * math.sin(ex_ori)
    else:
        ex_ori = poi["ori"]
        fx = poi["x"] + RACK_ESCAPE_M * math.cos(ex_ori)
        fy = poi["y"] + RACK_ESCAPE_M * math.sin(ex_ori)
    try:
        jack_service.safe_move(worker.robot_ip, "standard", fx, fy, ex_ori,
                               max_attempts=6, timeout=60, poll=jack_service.POLL_INTERVAL_SHORT)
    except jack_service.JackBusy:
        raise                       # 잭 정지를 확인 못 했다 — 움직이지 않고 멈춘다
    except Exception as e:
        logger.warning(f"[delivery] 전진 이탈 실패(무시): {e}")


def _park_at_charger(worker: _Worker) -> bool:
    """충전소 복귀 — 사전 접근 POI(<충전소>-1) 경유 후 도킹.

    랙은 이미 J 에 두고 왔으므로 **대기장소 반납 단계가 없다**(기존 시퀀스와 다름).

    복귀 도중 호출 수락 (2026-08-26 구현 — 그 전에는 이 독스트링에만 있고 코드가 없었다):
      - **사전 접근 이동 중**에는 감시 스레드가 `RESERVATION_WATCH_SEC` 마다 대기 예약을
        확인하고, 있으면 `_stop_flags` 로 이동을 끊는다(최대 약 5초 내 이탈).
      - **도킹은 끊지 않는다.** 충전기 정렬·진입 중에 끊으면 도크와 겹친 자세로 서게 되어
        복구 비용이 크고, 그 시점엔 이미 충전소에 도착해 있어 아끼는 시간이 10~30초뿐이다.
        대신 도킹 **직전·직후**에 확인해서, 직전이면 도킹 자체를 건너뛴다.
      - 배터리가 `min_battery` 미만이면 이어받지 않는다
        (`_take_next_reservation_if_continuable` 안의 기존 체크가 그대로 적용된다).

    반환: True = 복귀를 취소하고 `worker.divert_to` 의 작업을 이어받아야 함.
    """
    worker.divert_to = None

    robot = _load_robot(worker.robot_id)
    if not robot:
        return False
    charging = _load_charging_poi(robot)
    if not charging:
        logger.warning(f"[delivery] 충전소 POI 없음 — robot={worker.robot_id}")
        return False

    cx, cy, cori = charging["x"], charging["y"], charging["ori"]
    # 진입점(`C1-1`)은 **충전소 POI 와 같은 맵**에서 찾는다.
    #   2026-09-19 — 현장 로그로 드러난 문제. 로봇의 충전소(`robots.charging_id`)는
    #   구 맵(27)의 C1 을 가리키는데, 여기서는 area 의 '활성 맵 중 id 최대'(30)
    #   에서 `C1-1` 을 찾고 있었다. C1-1 은 27 에만 있어 못 찾았고, 그래서
    #   경유지 접근을 통째로 건너뛰고 charge 한 방으로 50 m 를 자유주행했다.
    #   `scheduler._return_to_charger` 와 `scripts/entry_poi_move.py` 는 이미
    #   충전소 POI 의 map_id 를 기준으로 삼는다 — 판단 기준을 그쪽에 맞춘다.
    approach = _find_entry_poi(worker.area_id, charging["name"],
                               map_id=charging.get("map_id"))

    def _check_divert() -> bool:
        """지금 이어받을 예약이 있으면 선점하고 True. (이동을 끊지는 않는다)"""
        if worker.divert_to is not None:
            return True
        if worker.end_flag or worker.abort_flag:
            return False
        nxt = _take_next_reservation_if_continuable(worker)
        if nxt is None:
            return False
        worker.divert_to = nxt
        return True

    # ── 1) 사전 접근 이동 — 이 구간에서만 이동을 중단시킨다 ──
    if approach:
        jack_service.update_job_status(worker.robot_ip, message=f"복귀 — 사전 접근({approach['name']})")

        stop_watch = threading.Event()

        def _watch() -> None:
            while not stop_watch.wait(RESERVATION_WATCH_SEC):
                if worker.end_flag or worker.abort_flag or worker.divert_to is not None:
                    return
                nxt = _take_next_reservation_if_continuable(worker)
                if nxt is not None:
                    worker.divert_to = nxt
                    # 진행 중인 이동을 끊는다 — wait_move 가 POLL_INTERVAL 마다 확인한다.
                    jack_service._stop_flags[worker.robot_ip] = True
                    logger.info(f"[delivery] 복귀 중 호출 감지 — 이동 중단 요청 poi={nxt[0]}")
                    return

        watcher = safe_thread(target=_watch, name=f"park-watch-{worker.robot_id}")
        watcher.start()
        try:
            _move_via_waypoints(worker, approach["x"], approach["y"], approach["ori"],
                                max_attempts=12, timeout=60)
        except Exception as e:
            # divert 로 인한 중단이면 정상 흐름이다 — 아래에서 divert_to 로 구분한다.
            logger.warning(f"[delivery] 사전 접근 중단/실패: {e}")
        finally:
            stop_watch.set()
            watcher.join(timeout=3)
            # 감시가 세운 stop 이 소비되지 않고 남아 있으면 정리한다.
            # 사용자 중지로 세워진 플래그는 divert_to 가 없으므로 건드리지 않는다.
            if worker.divert_to is not None:
                jack_service._stop_flags.pop(worker.robot_ip, None)

        if worker.divert_to is not None:
            return True
        time.sleep(2)

    # ── 2) 도킹 직전 확인 — 있으면 도킹 자체를 생략한다 ──
    if _check_divert():
        logger.info(f"[delivery] 도킹 직전 호출 감지 — 도킹 생략 poi={worker.divert_to[0]}")
        return True

    # ── 3) 도킹 — 중단하지 않고 끝까지 진행한다 ──
    jack_service.update_job_status(worker.robot_ip, message="충전소 도킹 중")
    try:
        jack_service.safe_move(worker.robot_ip, "charge", cx, cy, cori, charge_retry_count=5)
    except Exception as e:
        logger.warning(f"[delivery] 충전소 도킹 실패: {e}")

    # ── 4) 도킹 직후 확인 — 도킹하는 동안 들어온 호출을 여기서 받는다 ──
    if _check_divert():
        logger.info(f"[delivery] 도킹 직후 호출 감지 — 즉시 출발 poi={worker.divert_to[0]}")
        return True

    return False


def _worker_loop_delivery(worker: _Worker, *, first_j_poi_id: int) -> None:
    """R→J 단방향 배송 워커 (2026-08-24 시나리오).

    한 번의 호출 = R 에서 적재 → J 에 하차 → 이탈. 확인 버튼 없음.
    끝나면 대기 예약(FIFO)을 확인해서 있으면 충전소를 생략하고 이어서 진행한다.
    """
    j_poi_id = first_j_poi_id
    res_id: Optional[int] = None

    try:
        jack_service._running_robot_id_by_ip[worker.robot_ip] = worker.robot_id
        jack_service._stop_flags[worker.robot_ip] = False
        jack_service._paused_flags[worker.robot_ip] = False

        while True:
            mapping = job_mapping_for_poi(j_poi_id)
            j_poi = _load_poi(j_poi_id)
            if not mapping or not j_poi:
                _set_db_status(worker.session_id, "failed", error="작업지점 매핑/좌표 조회 실패")
                # 이어받은 예약이면 선점을 되돌린다. 안 그러면 그 호출이 fulfilled 로 남아
                # 아무 로봇도 받지 못하고 조용히 유실된다(작업자는 다시 눌러야 한다).
                if res_id:
                    _release_reservation(worker, res_id, j_poi_id,
                                      reason="작업지점 매핑/좌표 조회 실패 — 선점 롤백")
                return

            r_poi = _load_poi_by_name(mapping["area_id"], mapping["r_name"])
            if not r_poi:
                _set_db_status(worker.session_id, "failed",
                               error=f"랙 보관 위치 {mapping['r_name']} 를 맵에서 찾을 수 없음")
                if res_id:
                    _release_reservation(worker, res_id, j_poi_id,
                                      reason=f"랙 보관 위치 {mapping['r_name']} 조회 실패 — 선점 롤백")
                return

            logger.info(f"[delivery] robot={worker.robot_id} {mapping['r_name']} → {mapping['j_name']}")

            # ── 1) R 에서 랙 적재 ──
            _set_db_status(worker.session_id, "picking_up")
            _set_target_poi(worker.session_id, r_poi["id"])
            picked = _pickup_at_poi(worker, r_poi)

            if not picked:
                # 재시도를 다 쓰고도 정렬 실패 = 랙이 없는 것으로 간주.
                # 예약을 되돌리지 않고(같은 실패 반복 방지) 다음 대기 작업으로 넘어간다.
                msg = f"{mapping['r_name']} 에 랙이 없습니다 — {mapping['j_name']} 작업을 건너뜁니다"
                logger.warning(f"[delivery] {msg}")
                jack_service.update_job_status(worker.robot_ip, message=msg)
                _log_rack_missing(worker, mapping["r_name"], mapping["j_name"])

                nxt = _take_next_reservation_if_continuable(worker)
                if nxt is not None:
                    # 화면에는 "4회 실패 — 이 작업을 중단합니다" 가 떠 있다.
                    # 충전소로 가지 않고 다음 작업으로 넘어가므로 그렇게 고쳐 쓴다.
                    # (예전에는 항상 "충전소로 복귀합니다" 라 화면이 거짓말을 했다)
                    jack_service.set_dock_outcome_notice(
                        worker.robot_ip, r_poi.get("id"),
                        _poi_label(mapping["r_name"]),
                        "⚠ Docking 실패 — 대기 중인 다음 호출로 넘어갑니다. "
                        "대차를 정위치해 주세요.")
                    j_poi_id, res_id = nxt
                    continue
                jack_service.set_dock_outcome_notice(
                    worker.robot_ip, r_poi.get("id"),
                    _poi_label(mapping["r_name"]),
                    "⚠ Docking 실패로 충전소로 복귀합니다. 대차를 정위치해 주세요.")

                # 대기 작업 없음 → 충전소 복귀.
                # 예전에는 여기서 그냥 break 했다. 주석은 '충전소 복귀' 였지만
                # 실제로는 **아무 일도 일어나지 않았다** — 복귀도 안 하고 세션 상태도
                # 그대로 picking_up 으로 남아, 그 세션의 target(R) 때문에
                # R 타일이 로봇이 충전소에 있는데도 계속 '작업 중' 으로 보였다.
                # (2026-08-24 19:50 실측 — 유령 세션 106)
                _clear_waypoints(worker.session_id)
                _set_db_status(worker.session_id, "returning")
                _set_target_poi(worker.session_id, None)
                jack_service.update_job_status(worker.robot_ip, message="랙 없음 — 충전소 복귀")
                _diverted = _park_at_charger(worker)
                # "Docking 실패로 충전소로 복귀합니다" 안내는 **여기까지** 떠 있어야 한다.
                # 복귀가 끝났거나 새 작업으로 전환했으면 역할을 다한 것이므로 내린다.
                jack_service.clear_dock_notice(worker.robot_ip)
                if _diverted and worker.divert_to is not None:
                    j_poi_id, res_id = worker.divert_to
                    worker.divert_to = None
                    logger.info(f"[delivery] 복귀 중 새 호출 이어받음 poi={j_poi_id}")
                    jack_service.update_job_status(worker.robot_ip, message="복귀 취소 — 새 작업 시작")
                    _clear_waypoints(worker.session_id)
                    continue
                _set_db_status(worker.session_id, "completed")
                jack_service.update_job_status(worker.robot_ip, message="작업 완료 (랙 없음)")
                break

            # ── 2) J 로 이동 ──
            _set_db_status(worker.session_id, "moving")
            _set_target_poi(worker.session_id, j_poi_id)
            if not _move_to_jack_point(worker, j_poi):
                _set_db_status(worker.session_id, "failed",
                               error=f"{mapping['j_name']} 이동 실패")
                if res_id:
                    _release_reservation(worker, res_id, j_poi_id,
                                      reason=f"{mapping['j_name']} 이동 실패 — 선점 롤백")
                return
            _set_current_poi(worker.session_id, j_poi_id)

            # ── 3) 하차 + 이탈 ── (확인 버튼 없음 — 바로 다음 단계)
            _dropoff_at_poi(worker, j_poi)
            _set_j_occupied(mapping["area_id"], mapping["j_name"], True)
            logger.info(f"[delivery] {mapping['j_name']} 하차 완료 — 점유 표시")

            if worker.abort_flag:
                _clear_waypoints(worker.session_id)
                _set_db_status(worker.session_id, "failed", error="작업 강제 정리")
                # ★ 2026-09-29 — 강제 종료로 끝난 작업의 예약은 **취소**로 닫는다
                #   (종전: 되살려 다른 로봇이 받게 → 단일 로봇 현장에서 취소한 작업이
                #   다시 실행됐다. _release_reservation 주석 참조)
                if res_id:
                    _release_reservation(worker, res_id, j_poi_id,
                                         reason="작업 강제 정리")
                return

            # ── 4) 대기 예약이 있으면 충전소 생략하고 이어서 ──
            if not worker.end_flag:
                nxt = _take_next_reservation_if_continuable(worker)
                if nxt is not None:
                    j_poi_id, res_id = nxt
                    _clear_waypoints(worker.session_id)
                    continue

            # ── 5) 충전소 복귀 (랙 반납 단계 없음) ──
            _clear_waypoints(worker.session_id)
            _set_db_status(worker.session_id, "returning")
            _set_target_poi(worker.session_id, None)

            # 복귀 직전 재확인 — 이 사이에 호출이 들어왔으면 충전소를 포기하고 이어받는다.
            if not worker.end_flag:
                nxt = _take_next_reservation_if_continuable(worker)
                if nxt is not None:
                    logger.info(f"[delivery] 복귀 취소 — 새 호출 이어받음 poi={nxt[0]}")
                    jack_service.update_job_status(worker.robot_ip, message="복귀 취소 — 새 작업 시작")
                    j_poi_id, res_id = nxt
                    continue

            jack_service.update_job_status(worker.robot_ip, message="작업 종료 — 충전소 복귀")
            if _park_at_charger(worker) and worker.divert_to is not None:
                j_poi_id, res_id = worker.divert_to
                worker.divert_to = None
                logger.info(f"[delivery] 복귀 중 새 호출 이어받음 poi={j_poi_id}")
                jack_service.update_job_status(worker.robot_ip, message="복귀 취소 — 새 작업 시작")
                _clear_waypoints(worker.session_id)
                continue
            _set_db_status(worker.session_id, "completed")
            jack_service.update_job_status(worker.robot_ip, message="작업 완료")
            break

    except Exception as e:
        logger.exception(f"[delivery] 워커 예외 — robot_id={worker.robot_id}")
        _set_db_status(worker.session_id, "failed", error=str(e))
        if res_id:
            _release_reservation(worker, res_id, j_poi_id,
                              reason=f"배송 워커 예외 — 선점 롤백 ({e})")
    finally:
        jack_service._running_robot_id_by_ip.pop(worker.robot_ip, None)
        try:
            from app.services.zone_lock import release_all_by_robot as _release_zones
            _release_zones(worker.robot_id)
        except Exception:
            pass
        _remove_worker(worker.robot_id)
        # ★ 강제 종료로 끊긴 것이면 예약을 이어받지 않는다 — 후속 처리가 랙을 내려놓은 뒤 넘긴다
        #   (2026-09-29 — force_clear 주석 참조. 종전에는 [전체 강제 종료]만 그랬다)
        if getattr(worker, "no_takeover", False):
            logger.info("[delivery] 강제 종료·하차 중단 — robot=%s 워커는 예약을 이어받지 않는다",
                        worker.robot_id)
        else:
            try:
                try_fulfill_reservations()
            except Exception:
                logger.exception("[delivery] 종료 후 예약 자동 처리 실패")


def _log_rack_missing(worker: _Worker, r_name: str, j_name: str) -> None:
    """랙 없음을 활동 로그에 남긴다 — 조용히 지나가지 않게."""
    try:
        from app.crud.activity_log import log_activity
        log_activity("dispatch", "rack_missing",
                     f"{r_name} 에 랙이 없어 {j_name} 작업을 건너뜀 (robot_id={worker.robot_id})",
                     source="dispatch_delivery")
    except Exception:
        logger.warning(f"[delivery] 랙 없음 로그 기록 실패 — {r_name}/{j_name}")


# ══════════════════════════════════════════════════════════════════════
# 단일 이동 (연구용) — 2026-09-23
# ══════════════════════════════════════════════════════════════════════
#
# 왜 있나
#   지금 콘솔은 배차 시나리오(호출 → 적재 → 배송)에 묶여 있어서
#   "저 POI 로 한 번 가봐" 를 시킬 수가 없다. 주행을 한 번 시험하려고
#   배송 사이클을 통째로 태워야 했다.
#
#   비이상적 정지(급감속) 원인을 좁히려면 **같은 구간을 방식만 바꿔가며**
#   반복해야 한다 — 경유지 경유 vs 직행, 로봇 파라미터 변경 전후.
#   그 비교를 위한 것이다.
#
# 배차 코드는 건드리지 않는다
#   세션을 만들지 않고 jack_service.safe_move 를 직접 부른다.
#   대신 **진행 중인 배차가 있으면 거부**한다(워커 레지스트리 확인).
#   서버 안전존은 활성 로봇 전체를 상시 감시하므로 이 이동에도 그대로 걸린다.

_goto: dict[int, dict] = {}
_goto_lock = threading.Lock()


def goto_status(robot_id: int) -> dict:
    with _goto_lock:
        st = _goto.get(robot_id)
        if not st:
            return {"running": False, "robot_id": robot_id}
        return {
            "running": True,
            "robot_id": robot_id,
            "poi_name": st.get("poi_name"),
            "mode": st.get("mode"),
            "move_type": st.get("move_type"),
            "phase": st.get("phase"),
            "elapsed_sec": int(time.time() - st["started"]),
            "waypoints": st.get("waypoints"),
        }


def goto_stop(robot_id: int) -> tuple[bool, str]:
    """이동 중지. jack_service 의 중지 플래그를 세우면 safe_move 가 빠져나온다."""
    with _goto_lock:
        st = _goto.get(robot_id)
    if not st:
        return False, "진행 중인 단일 이동이 없습니다"
    try:
        jack_service.stop_robot_job(st["ip"])
    except Exception as e:
        return False, f"중지 실패: {e}"
    return True, "ok"


def goto_poi(robot_id: int, poi_id: int, mode: str = "route") -> tuple[bool, str]:
    """POI 하나로 이동시킨다.

    mode="route"    경유지(W) 를 거쳐 간다. 경유지가 없으면 직행으로 떨어진다
    mode="direct"   경유지를 쓰지 않고 목표까지 바로 (standard)
    """
    robot = _load_robot(robot_id)
    if not robot or not robot.ip_address:
        return False, "로봇 정보 없음 또는 IP 미설정"
    if not robot.is_active:
        return False, "비활성 로봇"

    poi = _load_poi(poi_id)
    if not poi:
        return False, "POI 조회 실패"

    cancel_followup(robot_id, "단일 이동")     # 후속 동작이 돌면 사용자가 이긴다
    # 배차가 돌고 있으면 건드리지 않는다
    with _workers_lock:
        w = _workers.get(robot_id)
        if w is not None and (w.thread is None or w.thread.is_alive()):
            return False, "진행 중인 배차가 있습니다. 먼저 종료하세요"
    with _goto_lock:
        if robot_id in _goto:
            return False, "이미 이동 중입니다"
        _goto[robot_id] = {
            "ip": robot.ip_address, "poi_name": poi["name"], "mode": mode,
            "move_type": "-", "phase": "준비", "started": time.time(),
            "waypoints": None,
        }

    t = safe_thread(target=_goto_worker, args=(robot_id, robot.ip_address, poi, mode),
                    name=f"goto-{robot_id}")
    t.start()
    return True, "ok"


def _goto_worker(robot_id: int, ip: str, poi: dict, mode: str) -> None:
    def setst(**kw):
        with _goto_lock:
            if robot_id in _goto:
                _goto[robot_id].update(kw)

    try:
        jack_service._running_robot_id_by_ip[ip] = robot_id
        jack_service._stop_flags[ip] = False
        jack_service._paused_flags[ip] = False

        area_id = None
        try:
            r = _load_robot(robot_id)
            area_id = int(r.area_id) if (r and r.area_id) else None
        except Exception:
            pass

        if mode == "direct":
            move_type, extra = "standard", {}
            setst(move_type="standard", phase="직행 이동 중", waypoints=0)
            logger.info("[goto] robot=%s → %s 직행(standard)", robot_id, poi["name"])
        else:
            xy = _current_xy(ip)
            if xy is None:
                move_type, extra = "standard", {}
                setst(move_type="standard", phase="위치 못 읽음 — 직행", waypoints=0)
                logger.warning("[goto] %s 현재 위치를 못 읽어 직행으로 간다", ip)
            else:
                move_type, extra = waypoint_route.plan(area_id, xy[0], xy[1],
                                                       poi["x"], poi["y"])
                n = 0
                rc = extra.get("route_coordinates")
                if rc:
                    n = max(len(rc.split(",")) // 2 - 1, 0)
                setst(move_type=move_type, waypoints=n,
                      phase=("경유지 %d개 경유" % n) if waypoint_route.is_routed(extra)
                            else "경유지 없음 — 직행")
                logger.info("[goto] robot=%s → %s  %s (경유 %d)",
                            robot_id, poi["name"], move_type, n)

        jack_service.update_job_status(ip, message="단일 이동 — %s" % poi["name"])
        setst(phase="이동 중")
        jack_service.safe_move(ip, move_type, poi["x"], poi["y"], poi["ori"],
                               max_attempts=12, timeout=300, **extra)
        setst(phase="도착")
        logger.info("[goto] robot=%s → %s 도착", robot_id, poi["name"])

    except RuntimeError as e:
        logger.warning("[goto] robot=%s 중지됨: %s", robot_id, e)
    except Exception as e:
        logger.exception("[goto] robot=%s 예외", robot_id)
    finally:
        jack_service._running_robot_id_by_ip.pop(ip, None)
        jack_service.clear_job_status(ip)
        with _goto_lock:
            _goto.pop(robot_id, None)


def all_pois_for_robot(robot_id: int) -> list[dict]:
    """그 로봇의 활성 맵에 있는 **모든** POI. 단일 이동 목적지 후보.

    콘솔의 기존 목록(_current_area_active_pois)은 jack/standby 만 준다.
    여기서는 경유지(W)·진입점(<이름>-1)·충전소까지 전부 필요하다.
    """
    robot = _load_robot(robot_id)
    if not robot:
        return []
    db = SessionLocal()
    try:
        q = db.query(RobotMap).filter(RobotMap.is_active == True)
        if robot.area_id:
            q = q.filter(RobotMap.area_id == int(robot.area_id))
        m = q.order_by(RobotMap.id.desc()).first()
        if not m:
            return []
        rows = (db.query(MapPOI)
                .filter(MapPOI.map_id == m.id, MapPOI.is_active == True,
                        MapPOI.world_x.isnot(None))
                .order_by(MapPOI.name.asc()).all())
        return [{"poi_id": p.id, "name": p.name, "poi_type": p.poi_type,
                 "x": float(p.world_x), "y": float(p.world_y)} for p in rows]
    finally:
        db.close()
