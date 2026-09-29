"""강제 종료 · 잭 · 이동 상호 배제 모의 시험 (2026-09-29 현장 사고 재발 방지)

    BackEnd\\venv\\Scripts\\python.exe tests\\test_force_stop_jack.py

무엇을 확인하나
  2026-09-29 현장에서 [현재 JOB 강제 종료] 뒤 로봇이 **달리면서 랙을 내렸다.**
    14:58:56~59:06  잭 내림 10초 동안 0.80 m/s 주행
  원인은 강제 종료 뒤 '잭 내림(후속 처리)' 과 '다음 작업 이동(워커)' 이 서로를
  기다리지 않은 것이다. 이 시험은 **실제 배차 코드를 그대로** 돌리고, 로봇만
  가짜로 바꿔서 여러 시점에 종료 버튼을 눌러 본다.

  판정 (가짜 로봇이 0.02초마다 본다)
    ★ 위반 = 잭이 움직이는 순간 로봇이 실제로 이동했다
    ★ 위반 = 로봇이 이동 중일 때 잭 명령이 들어왔다

가짜 로봇
  `requests.get/post/patch` 와 `websocket.create_connection` 을 가로채서
  FAKE_IP 로 가는 것만 받는다. 이동·잭·자세·잭 상태 토픽을 흉내 낸다.
    잭 한 번 동작 2초 (실물 10~12초)   주행 8 m/s (실물 0.8)   LTE 지연 0.05~0.15초
  `/twist` 는 안 먹는 로봇으로 둔다 — 현장 longjack 과 같다(직진 이탈이 대체 경로로 간다).

DB
  운영 DB 가 아니라 복제본 `rcs_lg_test_db` 를 쓴다(없으면 만들 것 — 이 파일 아래 주석).
  로봇 12 의 IP 를 FAKE_IP 로 바꾸고 나머지 로봇은 비활성으로 둔다.
"""
import json
import math
import os
import queue
import random
import sys
import threading
import time

sys.path.insert(0, os.environ.get("BACKEND_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "BackEnd"))
os.environ["DB_HOST"] = "127.0.0.1"
os.environ["DB_PORT"] = "3306"
os.environ["DB_USER"] = "root"
os.environ["DB_PASSWORD"] = "1234"
os.environ["DB_NAME"] = "rcs_lg_test_db"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import logging  # noqa: E402

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_force_stop_jack.log")
logging.basicConfig(level=logging.INFO, filename=LOG_PATH, filemode="w", encoding="utf-8",
                    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s",
                    datefmt="%H:%M:%S")

import requests  # noqa: E402
import websocket  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal, DATABASE_URL  # noqa: E402
from app.services import dispatch_service as ds  # noqa: E402
from app.services import jack_service as js  # noqa: E402

assert "rcs_lg_test_db" in DATABASE_URL, f"테스트 DB 가 아니다: {DATABASE_URL}"

FAKE_IP = "10.255.0.12"
ROBOT_ID = 12
AREA = 36
MAP_ID = 36

SIM_SPEED = float(os.environ.get("SIM_SPEED", "0.8"))   # m/s — 현장 값. 빠르게 하면 경합이 안 드러난다
JACK_TIME = float(os.environ.get("JACK_TIME", "10"))    # 초 (0 → 1) — 현장 실측 10~12초
ROT_TIME = 0.3           # 제자리 방향 전환
TICK = 0.02


# ══════════════════════════════════════════════════════════════════
#  가짜 로봇
# ══════════════════════════════════════════════════════════════════
class FakeRobot:
    def __init__(self, x, y, ori):
        self.lock = threading.RLock()
        self.x, self.y, self.ori = x, y, ori
        self.moves: dict[int, dict] = {}
        self.cur_id = None
        self.next_id = 1000
        self.jack_state, self.progress, self.jack_dir = "hold", 0.0, 0
        self.cap = 1.0                          # /robot-params max_forward_velocity
        self.subs: list["FakeWS"] = []
        self.events: list[tuple] = []           # (t, kind, detail)
        self.violations: list[tuple] = []
        self.t0 = time.time()
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    # ── 기록 ──
    def ev(self, kind, detail=""):
        self.events.append((round(time.time() - self.t0, 2), kind, detail))

    def bad(self, kind, detail=""):
        self.violations.append((round(time.time() - self.t0, 2), kind, detail))
        self.ev("★위반 " + kind, detail)

    # ── 물리 ──
    def _run(self):
        last_pub = 0.0
        while not self.stop.is_set():
            time.sleep(TICK)
            with self.lock:
                moved = self._step_move(TICK)
                jack_moving = self.jack_state.startswith("jacking")
                if jack_moving:
                    self.progress = min(1.0, max(0.0, self.progress + self.jack_dir * TICK / JACK_TIME))
                    done = (self.progress >= 1.0) if self.jack_dir > 0 else (self.progress <= 0.0)
                    if done:
                        self.jack_state, self.jack_dir = "hold", 0
                        self.ev("잭 정지", f"progress={self.progress:.2f}")
                if moved and jack_moving:
                    self.bad("잭 동작 중 이동", f"progress={self.progress:.2f} move={self.cur_id}")
                now = time.time()
                if jack_moving and now - last_pub > 0.2 or (not jack_moving and last_pub and now - last_pub > 0.2 and self._pub_pending):
                    last_pub = now
                    self._publish_jack()

    _pub_pending = False

    def _step_move(self, dt) -> bool:
        m = self.moves.get(self.cur_id) if self.cur_id else None
        if not m or m["state"] != "moving":
            return False
        if self.cap <= 0.0:
            return False
        step = SIM_SPEED * dt
        moved = False
        while step > 0 and m["path"]:
            tx, ty = m["path"][0]
            d = math.hypot(tx - self.x, ty - self.y)
            if d < 1e-6:
                m["path"].pop(0)
                continue
            if d <= step:
                self.ori = math.atan2(ty - self.y, tx - self.x)
                self.x, self.y = tx, ty
                step -= d
                m["path"].pop(0)
                moved = True
            else:
                self.ori = math.atan2(ty - self.y, tx - self.x)
                self.x += (tx - self.x) * step / d
                self.y += (ty - self.y) * step / d
                step = 0
                moved = True
        if not m["path"]:
            if m.get("rot_left", 0) > 0:
                m["rot_left"] -= dt
                return moved
            self.ori = m["target_ori"]
            m["state"] = "succeeded"
            self.ev("이동 완료", f"{m['type']} id={m['id']}")
        return moved

    def _publish_jack(self):
        pkt = {"topic": "/jack_state", "state": self.jack_state,
               "progress": round(self.progress, 3), "weight": 87 if self.progress > 0.5 else 0}
        for ws in list(self.subs):
            if "/jack_state" in ws.topics:
                ws.q.put(pkt)
        self._pub_pending = self.jack_state.startswith("jacking")

    # ── 명령 ──
    def create_move(self, body):
        with self.lock:
            if self.jack_state.startswith("jacking"):
                self.ev("(참고) 잭 동작 중 이동 명령 접수", body.get("type"))
            old = self.moves.get(self.cur_id) if self.cur_id else None
            if old and old["state"] == "moving":
                old["state"] = "cancelled"
            mid = self.next_id
            self.next_id += 1
            tx, ty = float(body.get("target_x", self.x)), float(body.get("target_y", self.y))
            path = []
            rc = body.get("route_coordinates")
            if rc:
                v = [float(a) for a in str(rc).split(",")]
                path = [(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]
            if not path or math.hypot(path[-1][0] - tx, path[-1][1] - ty) > 1e-3:
                path.append((tx, ty))
            tori = float(body.get("target_ori", self.ori) or 0.0)
            m = {"id": mid, "type": body.get("type"), "state": "moving", "path": path,
                 "target_x": tx, "target_y": ty, "target_ori": tori,
                 "rot_left": ROT_TIME, "fail_reason": 0, "fail_reason_str": "None - None"}
            self.moves[mid] = m
            self.cur_id = mid
            self.ev("이동 명령", f"{m['type']} id={mid} → ({tx:.1f},{ty:.1f})")
            return mid

    def cancel(self):
        with self.lock:
            m = self.moves.get(self.cur_id) if self.cur_id else None
            if m and m["state"] == "moving":
                m["state"] = "cancelled"
                self.ev("이동 취소", f"id={m['id']}")

    def jack(self, up: bool):
        with self.lock:
            m = self.moves.get(self.cur_id) if self.cur_id else None
            if m and m["state"] == "moving" and self.cap > 0:
                self.bad("이동 중 잭 명령", f"{'up' if up else 'down'} move={m['id']}")
            self.jack_state = "jacking_up" if up else "jacking_down"
            self.jack_dir = 1 if up else -1
            self.ev("잭 명령", "up" if up else "down")
            self._publish_jack()

    def pose_pkt(self):
        with self.lock:
            return {"topic": "/tracked_pose", "pos": [self.x, self.y], "ori": self.ori}

    def jack_pkt(self):
        with self.lock:
            return {"topic": "/jack_state", "state": self.jack_state,
                    "progress": round(self.progress, 3), "weight": 87 if self.progress > 0.5 else 0}

    def current_move(self):
        with self.lock:
            m = self.moves.get(self.cur_id) if self.cur_id else None
            return None if m is None else {k: v for k, v in m.items() if k != "path"}


ROBOT: FakeRobot = None     # noqa


# ══════════════════════════════════════════════════════════════════
#  requests / websocket 가로채기
# ══════════════════════════════════════════════════════════════════
class _Resp:
    def __init__(self, code, data):
        self.status_code = code
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            e = requests.exceptions.HTTPError(f"{self.status_code}")
            e.response = self
            raise e


_real = {"get": requests.get, "post": requests.post, "patch": requests.patch}


def _lat():
    time.sleep(random.uniform(0.05, 0.15))


def _handle(method, url, body):
    path = url.split(":8090", 1)[1] if ":8090" in url else url
    _lat()
    r = ROBOT
    if method == "POST" and path == "/chassis/moves":
        return _Resp(200, {"id": r.create_move(body or {})})
    if method == "GET" and path == "/chassis/moves/current":
        m = r.current_move()
        return _Resp(404, {}) if m is None else _Resp(200, m)
    if method == "GET" and path.startswith("/chassis/moves/"):
        mid = int(path.rsplit("/", 1)[1])
        with r.lock:
            m = r.moves.get(mid)
            return _Resp(404, {}) if m is None else _Resp(200, {k: v for k, v in m.items() if k != "path"})
    if method == "PATCH" and path == "/chassis/moves/current":
        r.cancel()
        return _Resp(200, {})
    if method == "POST" and path == "/services/jack_up":
        r.jack(True)
        return _Resp(200, {})
    if method == "POST" and path == "/services/jack_down":
        r.jack(False)
        return _Resp(200, {})
    if method == "POST" and path == "/robot-params":
        v = (body or {}).get("/wheel_control/max_forward_velocity")
        if v is not None:
            with r.lock:
                r.cap = float(v)
            r.ev("속도 상한", str(v))
        return _Resp(200, {})
    if method == "GET" and path == "/chassis/current-map":
        return _Resp(200, {"id": MAP_ID})
    return _Resp(200, {})


def _mk(method):
    def f(url, *a, **kw):
        if FAKE_IP in str(url):
            return _handle(method.upper(), str(url), kw.get("json"))
        return _real[method](url, *a, **kw)
    return f


class FakeWS:
    def __init__(self, url):
        self.url = url
        self.topics = set()
        self.q = queue.Queue()
        self.timeout = 2.0
        self._last_pose = 0.0
        if ROBOT is not None:
            ROBOT.subs.append(self)

    def settimeout(self, t):
        self.timeout = t if t is not None else 2.0

    def send(self, msg):
        try:
            d = json.loads(msg)
        except Exception:
            return
        t = d.get("enable_topic")
        if t:
            self.topics.add(t)
            if t == "/jack_state":
                self.q.put(ROBOT.jack_pkt())     # 구독하면 최신 1건을 준다

    def recv(self):
        deadline = time.time() + (self.timeout or 2.0)
        while True:
            try:
                return json.dumps(self.q.get(timeout=0.02))
            except queue.Empty:
                pass
            if "/tracked_pose" in self.topics and time.time() - self._last_pose > 0.1:
                self._last_pose = time.time()
                return json.dumps(ROBOT.pose_pkt())
            if time.time() >= deadline:
                raise websocket.WebSocketTimeoutException("timeout")

    def close(self):
        try:
            ROBOT.subs.remove(self)
        except ValueError:
            pass


_real_ws = websocket.create_connection


def _fake_cc(url, *a, **kw):
    if FAKE_IP in str(url):
        _lat()
        return FakeWS(url)
    return _real_ws(url, *a, **kw)


def install():
    requests.get, requests.post, requests.patch = _mk("get"), _mk("post"), _mk("patch")
    websocket.create_connection = _fake_cc
    # 시간 압축 — 실물 대기값을 그대로 쓰면 한 판에 수 분이 걸린다
    ds.JACK_SETTLE_SEC = 0.3
    js.JACK_WAIT_SEC = 0.3
    ds._live_probe = lambda ips: set(ips)            # 온라인 판정 생략
    js.mark_twist_unfit(FAKE_IP, "모의 시험 — 현장 longjack 과 같게")


# ══════════════════════════════════════════════════════════════════
#  DB 준비
# ══════════════════════════════════════════════════════════════════
def q(sql, **p):
    db = SessionLocal()
    try:
        r = db.execute(text(sql), p)
        if r.returns_rows:
            return r.fetchall()
        db.commit()
        return None
    finally:
        db.close()


POI = {}


def prepare_db():
    for row in q("SELECT id, name, world_x, world_y, angle FROM map_pois "
                 "WHERE map_id=:m AND is_active=1", m=MAP_ID):
        POI[row[1]] = {"id": row[0], "x": row[2], "y": row[3], "ori": row[4] or 0.0}
    need = ["C1", "C1-1", "R1", "R2", "J1", "J2", "R1-1", "R2-1", "J1-1", "J2-1"]
    missing = [n for n in need if n not in POI]
    assert not missing, f"맵 {MAP_ID} 에 POI 없음: {missing}"
    q("UPDATE robots SET is_active=0 WHERE id<>:r", r=ROBOT_ID)
    q("UPDATE robots SET ip_address=:ip, is_active=1, area_id=:a, charging_id=:c, standby_id=NULL "
      "WHERE id=:r", ip=FAKE_IP, a=str(AREA), c=POI["C1"]["id"], r=ROBOT_ID)
    if not q("SELECT id FROM job_points WHERE area_id=:a", a=AREA):
        q("INSERT INTO job_points (area_id, j_poi_name, r_poi_name, occupied, created_at, updated_at) "
          "VALUES (:a,'J1','R1',0,NOW(),NOW()), (:a,'J2','R2',0,NOW(),NOW())", a=AREA)
    try:
        q("UPDATE robot_status SET battery_level=100 WHERE robot_id=:r", r=ROBOT_ID)
    except Exception:
        pass


def reset():
    """판마다 깨끗한 상태로 — 세션·예약·점유·서버 메모리 전부."""
    t = time.time()
    while ds.is_robot_busy(ROBOT_ID) and time.time() - t < 90:
        time.sleep(0.2)
    q("UPDATE dispatch_sessions SET status='failed' WHERE status NOT IN ('completed','failed')")
    q("DELETE FROM dispatch_reservations")
    q("UPDATE job_points SET occupied=0 WHERE area_id=:a", a=AREA)
    js._laden_flags.pop(FAKE_IP, None)
    getattr(js, "_jack_unconfirmed", set()).discard(FAKE_IP)
    js.clear_stop_flag(FAKE_IP)
    global ROBOT
    if ROBOT is not None:
        ROBOT.stop.set()
    c = POI["C1"]
    ROBOT = FakeRobot(c["x"], c["y"], c["ori"])


def wait_until(cond, timeout, step=0.05):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def settle(timeout=120):
    """워커·후속 처리가 모두 끝나고 로봇이 멈출 때까지."""
    t = time.time()
    while time.time() - t < timeout:
        busy = ds.is_robot_busy(ROBOT_ID)
        m = ROBOT.current_move()
        moving = m is not None and m["state"] == "moving"
        if not busy and not moving and not ROBOT.jack_state.startswith("jacking"):
            time.sleep(1.0)
            if not ds.is_robot_busy(ROBOT_ID):
                return True
        time.sleep(0.2)
    return False


# ══════════════════════════════════════════════════════════════════
#  시나리오
# ══════════════════════════════════════════════════════════════════
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def call_j(j_name):
    ok, msg, rid, reserved = ds.call_to_poi(POI[j_name]["id"])
    return ok, msg, rid, reserved


def phase_ready(phase):
    """강제 종료를 누를 순간인가."""
    r = ROBOT
    m = r.current_move()
    if phase == "적재중":
        return r.jack_state == "jacking_up"
    if phase == "주행중":
        return m is not None and m["state"] == "moving" and m["type"] == "along_given_route" \
            and js.is_laden(FAKE_IP)
    if phase == "하차중":
        return r.jack_state == "jacking_down"
    if phase == "하차직후":
        return r.jack_state == "hold" and r.progress <= 0.0 and any(
            e[1] == "잭 명령" and e[2] == "down" for e in r.events)
    raise ValueError(phase)


def run_case(label, phase, action, with_reservation=True):
    reset()
    ok, msg, rid, _ = call_j("J1")                      # R1 → J1
    if not ok:
        record(label, False, f"호출 실패 {msg}")
        return
    if with_reservation:
        time.sleep(0.3)
        call_j("J2")                                   # R2 → J2 예약
    if not wait_until(lambda: phase_ready(phase), 90):
        record(label, False, f"'{phase}' 시점을 못 만남")
        settle()
        return
    t_press = time.time()
    if action == "reserve":
        ds.force_clear(ROBOT_ID, after="reserve")
    elif action == "charge":
        ds.force_clear(ROBOT_ID, after="charge")
    elif action == "hold":
        ds.force_clear(ROBOT_ID, after="hold")
    elif action == "tablet_end":
        ds.send_end(ROBOT_ID)
    elif action == "stop_all":
        from app.routers.robot import api_stop_all
        api_stop_all(FAKE_IP)
    fin = settle(150)
    v = ROBOT.violations
    detail = f"위반 {len(v)}건"
    if v:
        detail += " " + "; ".join(f"{a[0]}s {a[1]}({a[2]})" for a in v[:3])
    if not fin:
        detail += " · 150초 안에 정리 안 됨"
    # 부가 확인 — 종료 뒤 랙이 내려갔는가 (hold 는 그 자리 정지라 제외)
    if action in ("reserve", "charge", "stop_all") and phase != "하차직후":
        down_after = [e for e in ROBOT.events if e[1] == "잭 명령" and e[2] == "down"
                      and e[0] >= t_press - ROBOT.t0 - 0.01]
        if not down_after and phase != "적재중":
            detail += " · 종료 뒤 잭다운 없음(확인)"
    record(label, not v and fin, detail)
    return ROBOT.events


def main():
    random.seed(7)
    install()
    prepare_db()
    print(f"로그: {LOG_PATH}")
    print("=" * 70)
    cases = []
    for phase in ("적재중", "주행중", "하차중", "하차직후"):
        for action in ("reserve", "charge"):
            cases.append((f"{action:7s} @ {phase}", phase, action))
    cases += [("tablet_end @ 주행중", "주행중", "tablet_end"),
              ("stop_all  @ 주행중", "주행중", "stop_all"),
              ("hold      @ 주행중", "주행중", "hold")]
    only = os.environ.get("ONLY")
    for label, phase, action in cases:
        if only and only not in label:
            continue
        evs = run_case(label, phase, action)
        if evs and ROBOT.violations:
            print("     ── 사건 기록 ──")
            for e in evs[-40:]:
                print("     ", e)
    # 무작위 반복 — 누르는 시점을 흩뜨린다
    for i in range(int(os.environ.get("RANDOM_ROUNDS", "6"))):
        phase = random.choice(["적재중", "주행중", "하차중"])
        action = random.choice(["reserve", "charge"])
        run_case(f"무작위{i + 1} {action} @ {phase}", phase, action)
    ROBOT.stop.set()
    bad = [r for r in RESULTS if not r[1]]
    print("=" * 70)
    print(f"통과 {len(RESULTS) - len(bad)} / {len(RESULTS)}")
    sys.stdout.flush()
    os._exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
