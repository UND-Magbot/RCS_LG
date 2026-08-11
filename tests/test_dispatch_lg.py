"""LG 배차(콘솔 + 로봇 부착 태블릿) 반복·동시 조작 모의 테스트

목적: 사람이 콘솔/태블릿을 여러 번, 빠르게, 겹쳐서 누를 때
      **DB(dispatch_sessions / dispatch_waypoints / dispatch_reservations)와
      백엔드 메모리(_workers)가 꼬이는 구간**이 있는지 찾는다.

VESA 의 tests/test_dispatch_race.py 는 DB 까지 전부 가짜(메모리)로 두고
배정 로직만 검증했다. LG 는 검증 대상이 "경유지 순회 + 태블릿 확인" 흐름이라
DB 정합성 자체가 관심사이므로 **진짜 MariaDB(복제본)를 그대로 쓴다.**

실기 안전장치 (2겹):
  1) DB 는 운영 `rcs_lg_db` 가 아니라 복제본 `rcs_lg_test_db`
  2) 그 DB 안의 로봇 IP 는 도달 불가 주소(10.255.255.x)이고,
     로봇과 통신하는 함수(safe_move/jack_up/jack_down/robot_*)는 전부 가짜로 교체한다.
     실제 통신이 한 번이라도 시도되면 NET_CALLS 가 올라가고 테스트가 실패한다.

실행: BackEnd\venv\Scripts\python.exe tests\test_dispatch_lg.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "BackEnd"))

# ── DB 를 테스트 복제본으로 고정 (import 전에 반드시 설정) ──
os.environ["DB_HOST"] = "127.0.0.1"
os.environ["DB_PORT"] = "3306"
os.environ["DB_USER"] = "root"
os.environ["DB_PASSWORD"] = "1234"
os.environ["DB_NAME"] = "rcs_lg_test_db"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging
logging.disable(logging.CRITICAL)  # 워커 로그로 결과가 묻히지 않게

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal, engine, DATABASE_URL  # noqa: E402
from app.services import dispatch_service as ds  # noqa: E402
from app.services import jack_service as js  # noqa: E402

assert "rcs_lg_test_db" in DATABASE_URL, f"테스트 DB가 아님! {DATABASE_URL}"

# POI (테스트 DB, map_id=22)
C1, C1_1, R1, J2, J3, J4, J1 = 979, 980, 981, 982, 983, 984, 985
JACKS = [J1, J2, J3, J4]
ROBOT_MAIN = 12          # 실 로봇 행(IP는 도달 불가로 바꿔둠)
ROBOTS_ALL = [12, 13, 14]

_passed = 0
_failed = 0
_findings: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}   {detail}")
        _findings.append(f"{name} :: {detail}")


# ══════════════════════════════════════════════════════════
# 로봇 통신 차단 + 가짜 이동
# ══════════════════════════════════════════════════════════

NET_CALLS: list[str] = []          # 진짜 통신이 시도되면 여기 쌓인다(=사고)
MOVES: list[tuple] = []            # (robot_ip, move_type, x, y)
JACK_OPS: list[tuple] = []         # (robot_ip, 'up'|'down')
_move_lock = threading.Lock()
MOVE_DELAY = 0.02                  # 이동 1회에 걸리는 가짜 시간


def fake_safe_move(ip, move_type, target_x, target_y, target_ori=0, **kw):
    time.sleep(MOVE_DELAY)
    with _move_lock:
        MOVES.append((ip, move_type, round(float(target_x), 3), round(float(target_y), 3)))
    return {"state": "succeeded"}


def fake_jack_up(ip):
    with _move_lock:
        JACK_OPS.append((ip, "up"))
    return {"ok": True}


def fake_jack_down(ip):
    with _move_lock:
        JACK_OPS.append((ip, "down"))
    return {"ok": True}


def _net_guard(name):
    def _blocked(*a, **k):
        NET_CALLS.append(f"{name}{a[:2]}")
        return {}
    return _blocked


def install_fakes():
    js.safe_move = fake_safe_move
    js.jack_up = fake_jack_up
    js.jack_down = fake_jack_down
    # 혹시 다른 경로로 로봇에 나가는 호출이 있으면 잡아낸다
    for fn in ("robot_get", "robot_post", "robot_patch", "robot_delete"):
        if hasattr(js, fn):
            setattr(js, fn, _net_guard(fn))
    ds.JACK_SETTLE_SEC = 0
    js.JACK_WAIT_SEC = 0
    # 온라인 라이브 체크(REST/WS) 차단 — 전부 온라인 취급
    ds.online_ips_cached = lambda ips: set(ips)


# ══════════════════════════════════════════════════════════
# DB 헬퍼
# ══════════════════════════════════════════════════════════


def q(sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).fetchall()


def q1(sql, **params):
    r = q(sql, **params)
    return r[0] if r else None


def exec_(sql, **params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def session_status(robot_id):
    r = q1("SELECT status FROM dispatch_sessions WHERE robot_id=:r "
           "ORDER BY id DESC LIMIT 1", r=robot_id)
    return r[0] if r else None


def active_session_id(robot_id):
    r = q1("SELECT id FROM dispatch_sessions WHERE robot_id=:r "
           "AND status NOT IN ('completed','failed') ORDER BY id DESC LIMIT 1", r=robot_id)
    return r[0] if r else None


def waypoints_of(session_id):
    return [(w[0], w[1], w[2]) for w in q(
        "SELECT seq,poi_id,status FROM dispatch_waypoints WHERE session_id=:s ORDER BY seq", s=session_id)]


def wait_status(robot_id, want, timeout=15.0):
    """세션이 원하는 상태가 될 때까지 대기. 성공 True."""
    if isinstance(want, str):
        want = (want,)
    end = time.time() + timeout
    while time.time() < end:
        if session_status(robot_id) in want:
            return True
        time.sleep(0.02)
    return False


def wait_no_workers(timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        if not any(ds.has_active_worker(r) for r in ROBOTS_ALL):
            return True
        time.sleep(0.05)
    return False


def reset_all(label=""):
    """워커 정리 + 배차 관련 테이블 비우기."""
    for rid in list(ds._workers.keys()):
        try:
            ds.force_clear(rid)
        except Exception:
            pass
    wait_no_workers()
    with ds._workers_lock:
        ds._workers.clear()
    exec_("DELETE FROM dispatch_waypoints")
    exec_("DELETE FROM dispatch_sessions")
    exec_("DELETE FROM dispatch_reservations")
    MOVES.clear()
    JACK_OPS.clear()


def run_concurrent(fn, n):
    """n개 스레드를 배리어로 동시에 출발 — 경합을 최대화."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def runner(i):
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as e:
            results[i] = ("EXC", repr(e))

    ts = [threading.Thread(target=runner, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


def visited_jacks(since=0):
    """가짜 이동 기록에서 방문한 jack POI 순서를 복원."""
    coords = {}
    for pid in JACKS:
        r = q1("SELECT ROUND(world_x,3),ROUND(world_y,3) FROM map_pois WHERE id=:p", p=pid)
        coords[(float(r[0]), float(r[1]))] = pid
    out = []
    for ip, mt, x, y in MOVES[since:]:
        pid = coords.get((x, y))
        if pid:
            out.append(pid)
    return out


# ══════════════════════════════════════════════════════════
# 시나리오
# ══════════════════════════════════════════════════════════


def confirm_view(robot_id):
    """로봇 부착 태블릿이 보는 것과 같은 정보: (session_id, status, current_poi, 확인대기 seq)

    '확인을 몇 번 보냈나'가 아니라 '어느 경유지(seq)의 확인인가'로 판정해야 한다.
    확인 직후 워커가 status 를 moving 으로 바꾸기까지 DB 왕복이 있어, 그 사이에는
    status 만 보면 직전 지점의 awaiting_confirm 이 그대로 읽히기 때문이다.
    """
    r = q1("SELECT id,status,current_poi_id FROM dispatch_sessions WHERE robot_id=:r "
           "AND status NOT IN ('completed','failed') ORDER BY id DESC LIMIT 1", r=robot_id)
    if not r:
        return None
    sid, st, cur = r
    seq = q1("SELECT seq FROM dispatch_waypoints WHERE session_id=:s AND status='current'", s=sid)
    return sid, st, cur, (seq[0] if seq else None)


def drive_confirms(robot_id, taps=1, timeout=30):
    """각 경유지 도착마다 [확인]을 눌러 끝까지 진행시킨다.

    같은 지점에서 두 번 누르지 않도록 seq 로 구분한다(사람은 화면이 바뀐 걸 보고 누른다).
    taps>1 이면 '한 지점에서 동시에 여러 번 연타'를 재현한다.
    """
    tapped: set[int] = set()
    end = time.time() + timeout
    while time.time() < end:
        st = session_status(robot_id)
        if st in ("completed", "failed", "returning"):
            return True, sorted(tapped)
        info = confirm_view(robot_id)
        if info:
            _sid, sstat, _cur, seq = info
            if sstat == "awaiting_confirm" and seq is not None and seq not in tapped:
                tapped.add(seq)
                if taps == 1:
                    ds.send_confirm(robot_id)
                else:
                    run_concurrent(lambda k: ds.send_confirm(robot_id), taps)
        time.sleep(0.02)
    return False, sorted(tapped)


def full_cycle(robot_id, first_poi, route, confirm_repeat=1, timeout=30):
    """호출 → 경유지 등록/출발 → 각 도착마다 확인 → 자동 종료. 성공 True."""
    if not wait_status(robot_id, "awaiting_next", timeout):
        return False, f"첫 도착(awaiting_next) 실패 — 현재 {session_status(robot_id)}"
    ok, msg = ds.send_route(robot_id, route)
    if not ok:
        return False, f"send_route 실패: {msg}"
    done, tapped = drive_confirms(robot_id, taps=confirm_repeat, timeout=timeout)
    if not done:
        return False, f"확인 진행 실패 — 누른 seq={tapped}, 현재 {session_status(robot_id)}"
    if not wait_status(robot_id, ("completed", "failed"), timeout):
        return False, f"종료 실패 — 현재 {session_status(robot_id)}"
    return True, "ok"


def s1_repeat_cycle(rounds=20):
    print(f"\n[S1] 정상 사이클 {rounds}회 반복 — DB 잔여물/워커 누수 검사")
    reset_all()
    bad = []
    for i in range(rounds):
        route = [J2, J3] if i % 2 == 0 else [J3, J4]
        ok, msg, rid, reserved = ds.call_to_poi(J1, with_rack=True)
        if not ok or rid is None:
            bad.append(f"round{i}: 호출 실패 ok={ok} msg={msg} reserved={reserved}")
            break
        done, why = full_cycle(rid, J1, route)
        if not done:
            bad.append(f"round{i}: {why}")
            break
        # 라운드 종료 시점 잔여 검사
        if not wait_no_workers(timeout=10):
            bad.append(f"round{i}: 워커가 남음 {list(ds._workers)}")
            break
    check(f"{rounds}회 전부 완주", not bad, "; ".join(bad[:3]))

    st = q("SELECT status, COUNT(*) FROM dispatch_sessions GROUP BY status")
    stat = {s: c for s, c in st}
    check("모든 세션이 completed (failed 0건)",
          stat.get("failed", 0) == 0 and stat.get("completed", 0) == rounds, str(stat))
    leftover_wp = q1("SELECT COUNT(*) FROM dispatch_waypoints")[0]
    check("경유지 테이블 잔여 0건", leftover_wp == 0, f"{leftover_wp}건 남음")
    leftover_res = q1("SELECT COUNT(*) FROM dispatch_reservations WHERE status='waiting'")[0]
    check("대기 예약 잔여 0건", leftover_res == 0, f"{leftover_res}건 남음")
    check("메모리 워커 0개", len(ds._workers) == 0, str(list(ds._workers)))
    check("로봇 실통신 0회", not NET_CALLS, str(NET_CALLS[:3]))


def s2_route_spam(rounds=15):
    print(f"\n[S2] [출발] 동시 연타 — DB 경유지와 실제 이동 경로가 어긋나는지 ({rounds}회)")
    mismatch = []
    multi_accept = []
    for i in range(rounds):
        reset_all()
        ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
        if not ok or rid is None:
            mismatch.append(f"round{i}: 호출 실패 {msg}")
            break
        if not wait_status(rid, "awaiting_next"):
            mismatch.append(f"round{i}: awaiting_next 안 됨")
            break
        sid = active_session_id(rid)
        mark = len(MOVES)

        # 서로 다른 경로 3개를 동시에 등록 시도 (콘솔에서 [출발] 연타/중복 클릭)
        routes = [[J2, J3], [J4], [J3, J2]]
        res = run_concurrent(lambda k: ds.send_route(rid, routes[k]), 3)
        accepted = [routes[k] for k, r in enumerate(res) if isinstance(r, tuple) and r[0] is True]
        if len(accepted) > 1:
            multi_accept.append(f"round{i}: {len(accepted)}건 수락 {accepted}")

        # 워커가 경로를 다 돌 때까지 확인 눌러주기
        drive_confirms(rid, taps=1, timeout=30)
        wait_status(rid, ("completed", "failed"), 20)

        db_wps = [p for _, p, _ in waypoints_of(sid)]
        actually = visited_jacks(mark)
        # 실제로 방문한 jack 중 '경유지로 간 곳'만 비교 (첫 도착 J1 은 mark 이후에 없음)
        if db_wps and actually and db_wps != actually:
            mismatch.append(f"round{i}: DB경유지={db_wps} 실제방문={actually}")

    check("동시 [출발] 중 1건만 수락", not multi_accept, "; ".join(multi_accept[:3]))
    check("DB 경유지 == 실제 이동 경로", not mismatch, "; ".join(mismatch[:3]))


def s3_confirm_spam(rounds=15, taps=6):
    print(f"\n[S3] [확인] 연타({taps}회 동시) — 경유지를 건너뛰는지 ({rounds}회)")
    skipped = []
    for i in range(rounds):
        reset_all()
        ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
        if not ok or rid is None:
            skipped.append(f"round{i}: 호출 실패 {msg}")
            break
        if not wait_status(rid, "awaiting_next"):
            skipped.append(f"round{i}: awaiting_next 안 됨")
            break
        sid = active_session_id(rid)
        mark = len(MOVES)
        route = [J2, J3, J4]
        ok, msg = ds.send_route(rid, route)
        if not ok:
            skipped.append(f"round{i}: send_route {msg}")
            break

        # 한 번 도착할 때마다 사람이 [확인]을 taps회 연타
        drive_confirms(rid, taps=taps, timeout=30)
        wait_status(rid, ("completed", "failed"), 20)
        actually = visited_jacks(mark)
        if actually != route:
            skipped.append(f"round{i}: 기대={route} 실제={actually}")
        wp_states = [s for _, _, s in waypoints_of(sid)]
        if wp_states and any(s != "done" for s in wp_states):
            skipped.append(f"round{i}: 경유지 상태 미완 {wp_states}")

    check("연타해도 경유지를 순서대로 전부 방문", not skipped, "; ".join(skipped[:3]))


def s4_same_poi_call(rounds=15, threads=8):
    print(f"\n[S4] 같은 POI 동시 호출 {threads}스레드 × {rounds}회 (로봇 3대) — 2대가 같은 곳에 가는지")
    over = []
    for i in range(rounds):
        reset_all()
        res = run_concurrent(lambda k: ds.call_to_poi(J1, with_rack=True), threads)
        assigned = [r for r in res if isinstance(r, tuple) and len(r) == 4 and r[0] and r[2] is not None]
        reserved = [r for r in res if isinstance(r, tuple) and len(r) == 4 and r[0] and r[3]]
        if len(assigned) > 1:
            over.append(f"round{i}: {len(assigned)}대 배정 {[r[2] for r in assigned]}")
        # DB 로도 확인 — 같은 POI 를 목표/현재로 가진 활성 세션이 2개 이상인가
        rows = q("SELECT COUNT(*) FROM dispatch_sessions WHERE status NOT IN ('completed','failed') "
                 "AND (target_poi_id=:p OR current_poi_id=:p)", p=J1)
        if rows[0][0] > 1:
            over.append(f"round{i}: 같은 POI 활성세션 {rows[0][0]}개")
        if len(reserved) > 0 and len(assigned) == 0:
            over.append(f"round{i}: 아무도 배정 안 되고 예약만 {len(reserved)}건")
    check("같은 POI 에는 항상 1대만 배정", not over, "; ".join(over[:3]))
    reset_all()


def s5_commands_after_end(rounds=10):
    print(f"\n[S5] 종료 진행 중 [확인]/[출발] 명령 — 무시되는지 ({rounds}회)")
    leaks = []
    for i in range(rounds):
        reset_all()
        ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
        if not ok or rid is None:
            leaks.append(f"round{i}: 호출 실패 {msg}")
            break
        wait_status(rid, "awaiting_next")
        ds.send_route(rid, [J2])
        wait_status(rid, "awaiting_confirm", 10)
        mark = len(MOVES)
        ds.send_end(rid)                       # 태블릿 [작업 종료]
        # 종료 처리 중에 확인/출발을 계속 눌러댄다
        r1 = run_concurrent(lambda k: ds.send_confirm(rid), 4)
        r2 = run_concurrent(lambda k: ds.send_route(rid, [J3, J4]), 4)
        accepted = [r for r in (r1 + r2) if isinstance(r, tuple) and r[0] is True]
        if accepted:
            leaks.append(f"round{i}: 종료 후 명령 {len(accepted)}건 수락됨")
        if not wait_status(rid, ("completed", "failed"), 20):
            leaks.append(f"round{i}: 종료 안 됨 ({session_status(rid)})")
            break
        after = visited_jacks(mark)
        if J3 in after or J4 in after:
            leaks.append(f"round{i}: 종료 후에도 경유지로 이동함 {after}")
    check("종료 중 명령은 전부 거부", not leaks, "; ".join(leaks[:3]))


def s6_force_clear(rounds=12):
    print(f"\n[S6] [작업 강제 종료] 반복 — 세션/워커/경유지 잔여물 ({rounds}회)")
    bad = []
    for i in range(rounds):
        reset_all()
        ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
        if not ok or rid is None:
            bad.append(f"round{i}: 호출 실패 {msg}")
            break
        wait_status(rid, "awaiting_next")
        sid = active_session_id(rid)
        ds.send_route(rid, [J2, J3, J4])       # 경유지 3개 등록
        time.sleep(0.05)
        ds.force_clear(rid)                    # 도중에 강제 정리
        if not wait_status(rid, ("failed", "completed"), 20):
            bad.append(f"round{i}: 강제 정리 후에도 세션 활성 ({session_status(rid)})")
            break
        if not wait_no_workers(10):
            bad.append(f"round{i}: 워커 잔존")
            break
        left = waypoints_of(sid)
        if left:
            bad.append(f"round{i}: 경유지 {len(left)}건 잔존 {left}")
        # 강제 정리 직후 바로 재호출이 되는가 (운영에서 제일 흔한 다음 동작)
        ok2, msg2, rid2, _ = ds.call_to_poi(J1, with_rack=True)
        if not ok2 or rid2 is None:
            bad.append(f"round{i}: 강제 정리 후 재호출 실패 {msg2}")
            break
        ds.force_clear(rid2)
        wait_no_workers(10)
    check("강제 종료 후 잔여물 없음 + 즉시 재호출 가능", not bad, "; ".join(bad[:3]))
    # 강제 정리는 로봇을 움직이면 안 된다
    reset_all()


def s7_reservation_chain(rounds=8):
    print(f"\n[S7] 예약 대기열 + 이어받기 반복 — 예약이 유실/중복되는지 ({rounds}회)")
    bad = []
    # 로봇 1대만 남기고 나머지 비활성화 → 예약이 쌓이는 상황 재현
    exec_("UPDATE robots SET is_active=0 WHERE id IN (13,14)")
    try:
        for i in range(rounds):
            reset_all()
            ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
            if not ok or rid is None:
                bad.append(f"round{i}: 첫 호출 실패 {msg}")
                break
            # 가용 로봇이 없으므로 아래 둘은 예약으로 들어가야 한다
            ok2, _, _, resv2 = ds.call_to_poi(J2, with_rack=True)
            ok3, _, _, resv3 = ds.call_to_poi(J3, with_rack=True)
            if not (ok2 and resv2 and ok3 and resv3):
                bad.append(f"round{i}: 예약 전환 실패 {ok2}/{resv2} {ok3}/{resv3}")
                break
            waiting = q1("SELECT COUNT(*) FROM dispatch_reservations WHERE status='waiting'")[0]
            if waiting != 2:
                bad.append(f"round{i}: 대기 예약 {waiting}건(기대 2)")
                break
            # 같은 POI 재호출 → 중복 예약이 생기면 안 된다
            ds.call_to_poi(J2, with_rack=True)
            waiting2 = q1("SELECT COUNT(*) FROM dispatch_reservations WHERE status='waiting'")[0]
            if waiting2 != 2:
                bad.append(f"round{i}: 중복 예약 발생 {waiting2}건")
                break
            # 첫 작업을 끝내면 예약이 FIFO 로 자동 소화되어야 한다
            wait_status(rid, "awaiting_next")
            ds.send_route(rid, [J4])
            wait_status(rid, "awaiting_confirm", 10)
            ds.send_confirm(rid)
            # 이어받기(충전소 안 가고 다음 예약지로) 또는 종료 후 자동 호출
            if not wait_status(rid, ("awaiting_next", "completed", "failed"), 25):
                bad.append(f"round{i}: 이어받기/종료 대기 실패 ({session_status(rid)})")
                break
            time.sleep(0.4)
            left = q1("SELECT COUNT(*) FROM dispatch_reservations WHERE status='waiting'")[0]
            if left > 2:
                bad.append(f"round{i}: 예약이 늘어남 {left}건")
                break
            # 정리
            for r in ROBOTS_ALL:
                ds.force_clear(r)
            wait_no_workers(15)
    finally:
        exec_("UPDATE robots SET is_active=1 WHERE id IN (13,14)")
    check("예약 중복 없음 + FIFO 소화", not bad, "; ".join(bad[:3]))


def s8_restart_recovery(rounds=8):
    print(f"\n[S8] 서버 재시작 복구 반복 — 세션/경유지가 꼬이는지 ({rounds}회)")
    bad = []
    for i in range(rounds):
        reset_all()
        ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
        if not ok or rid is None:
            bad.append(f"round{i}: 호출 실패 {msg}")
            break
        wait_status(rid, "awaiting_next")
        sid = active_session_id(rid)
        ds.send_route(rid, [J2, J3])
        wait_status(rid, ("moving", "awaiting_confirm"), 10)

        # ── 서버 재시작 흉내: 메모리 워커만 사라지고 DB 세션은 남는다 ──
        with ds._workers_lock:
            ghosts = list(ds._workers.values())
            ds._workers.clear()
        for g in ghosts:                    # 남은 유령 스레드가 DB를 더 만지지 않게 종료 유도
            g.end_flag = True
            g.abort_flag = True
            g.next_event.set()
            g.confirm_event.set()
        time.sleep(0.3)
        # 재시작 직후 DB에 남아있던 세션을 복구
        exec_("UPDATE dispatch_sessions SET status='moving' WHERE id=:s "
              "AND status NOT IN ('completed','failed')", s=sid)
        ds.recover_on_startup()
        time.sleep(0.3)

        st = session_status(rid)
        if st not in ("awaiting_next", "moving", "returning", "completed", "failed"):
            bad.append(f"round{i}: 복구 후 이상 상태 {st}")
        # 복구 시 남은 경유지는 비워져야 한다(안 그러면 그 POI가 영원히 점유로 잡힘)
        occ = q1("SELECT COUNT(*) FROM dispatch_waypoints WHERE session_id=:s "
                 "AND status IN ('pending','current')", s=sid)[0]
        if occ:
            bad.append(f"round{i}: 복구 후 경유지 {occ}건이 점유로 남음")
        # 복구된 세션이 정상 종료되는지
        for r in ROBOTS_ALL:
            ds.force_clear(r)
        if not wait_no_workers(20):
            bad.append(f"round{i}: 복구 워커가 안 끝남")
            break
        stuck = q1("SELECT COUNT(*) FROM dispatch_sessions WHERE status NOT IN ('completed','failed')")[0]
        if stuck:
            bad.append(f"round{i}: 활성 세션 {stuck}건 잔존")
            break
    check("재시작 복구 후 세션·경유지 정상", not bad, "; ".join(bad[:3]))


def s9_orphan_poi_occupancy():
    print("\n[S9] 점유 계산 정합 — 끝난 세션이 POI를 계속 붙잡고 있는지")
    reset_all()
    ok, msg, rid, _ = ds.call_to_poi(J1, with_rack=True)
    check("호출 성공", ok and rid is not None, str(msg))
    if not (ok and rid):
        return
    wait_status(rid, "awaiting_next")
    ds.send_route(rid, [J2, J3])
    done, tapped = drive_confirms(rid, taps=1, timeout=30)
    check("경유지 2곳 모두 확인 소비", done and tapped == [0, 1], f"done={done} tapped={tapped}")
    check("세션이 정상 종료", wait_status(rid, ("completed", "failed"), 25),
          f"현재 {session_status(rid)}")
    wait_no_workers(15)

    db = SessionLocal()
    try:
        from app.crud import dispatch as dcrud
        occ = dcrud.occupied_poi_ids(db)
    finally:
        db.close()
    check("작업 종료 후 점유 POI 0개", not occ, f"남은 점유: {sorted(occ)}")

    # 종료 직후 같은 자리로 곧바로 다시 호출되는지
    ok2, msg2, rid2, _ = ds.call_to_poi(J1, with_rack=True)
    check("종료 직후 같은 POI 재호출 가능", ok2 and rid2 is not None, str(msg2))
    reset_all()


# ══════════════════════════════════════════════════════════
def main():
    print("=" * 62)
    print("LG 배차 반복·동시 조작 모의 테스트")
    print(f"DB   : {DATABASE_URL.split('@')[-1]}")
    print("로봇 : 통신 전면 차단(가짜 이동) — 실기에 명령 나가지 않음")
    print("=" * 62)

    install_fakes()
    exec_("DELETE FROM robot_status")   # 배터리 미상 = 배정 허용 (find_available_robot 정책)
    reset_all()

    t0 = time.time()
    s1_repeat_cycle(rounds=12)
    s2_route_spam(rounds=10)
    s3_confirm_spam(rounds=10)
    s4_same_poi_call(rounds=10)
    s5_commands_after_end(rounds=8)
    s6_force_clear(rounds=8)
    s7_reservation_chain(rounds=5)
    s8_restart_recovery(rounds=5)
    s9_orphan_poi_occupancy()
    reset_all()

    print("\n" + "=" * 62)
    print(f"결과: {_passed} passed, {_failed} failed   ({time.time()-t0:.1f}s)")
    if NET_CALLS:
        print(f"⚠ 실제 로봇 통신 시도 {len(NET_CALLS)}회 — {NET_CALLS[:5]}")
    if _findings:
        print("\n[발견 사항]")
        for f in _findings:
            print(f"  - {f}")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
