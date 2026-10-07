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
        self.fail_rotate = False                # True 면 제자리 회전(같은 좌표 standard)을 실패시킨다
        self.jack_down_ori = None               # 마지막 잭다운 명령 순간의 방향(rad)
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
            # 제자리 회전 = 지금 자리를 목표로 준 standard. 실패 시나리오용.
            if self.fail_rotate and body.get("type") == "standard" \
                    and math.hypot(tx - self.x, ty - self.y) < 0.05:
                m.update(state="failed", path=[], fail_reason=1001,
                         fail_reason_str="rotate_failed - 모의 회전 실패")
                self.ev("제자리 회전 실패(모의)", f"id={mid}")
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
            if not up:
                self.jack_down_ori = self.ori
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
    last_before = (q("SELECT MAX(id) FROM dispatch_sessions") or [(0,)])[0][0] or 0
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
    if action == "hold":
        # [그 자리 정지] — 로봇은 랙을 든 채 선다. 새 규칙(2026-09-29)상 **새 작업이 붙으면 안 된다.**
        #   종전에는 워커가 죽으면서 R2 예약을 물고 랙을 든 채 출발했다.
        time.sleep(20)
        newer = (q("SELECT COUNT(*) FROM dispatch_sessions WHERE id>:s", s=last_before) or [(0,)])[0][0]
        v = ROBOT.violations
        record(label, not v and newer == 0,
               f"위반 {len(v)}건 · 정지 후 새 작업 {newer}건 (0 이어야 함 — 랙을 든 채라 배차 제외)")
        js._laden_flags.pop(FAKE_IP, None)          # 다음 판을 위해 사람이 잭을 내린 것으로
        settle(60)
        return ROBOT.events
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


def run_case_divert():
    """2026-09-29 현장 버그 — JOB 종료했는데 **취소한 작업(R1)이 다시 실행**됐다.

    재현 순서
      ① R2→J2 를 한 번 보내 끝낸다 → 로봇이 충전소로 **복귀 중**
      ② 복귀 중에 R1 호출 → 가용 로봇이 없어 예약 → 복귀하던 워커가 **이어받는다**(예약 id 보유)
      ③ R2 예약
      ④ R1→J1 주행 중 [현재 JOB 강제 종료]
    기대 — 다음 작업은 **R2(J2)**, R1 예약은 **cancelled**.
    종전 — 워커 예외 처리가 R1 예약을 waiting 으로 되살려, 먼저 생긴 R1 을 다시 집었다.
    """
    label = "divert  JOB종료 → 다음은 R2"
    reset()
    call_j("J2")
    ok = wait_until(lambda: (q("SELECT status FROM dispatch_sessions WHERE robot_id=:r "
                               "ORDER BY id DESC LIMIT 1", r=ROBOT_ID) or [("",)])[0][0]
                    == "returning", 240)
    if not ok:
        record(label, False, "복귀 상태를 못 만남")
        settle()
        return
    q("UPDATE job_points SET occupied=0 WHERE area_id=:a", a=AREA)   # 작업자가 J2 [확인]
    ok1, msg1, rid1, res1 = call_j("J1")                               # 복귀 중 → 예약 → 이어받기
    time.sleep(1.0)
    ok2, msg2, rid2, res2 = call_j("J2")                               # R2 예약
    j1_res = q("SELECT id FROM dispatch_reservations WHERE poi_id=:p ORDER BY id DESC LIMIT 1",
               p=POI["J1"]["id"])
    if not wait_until(lambda: phase_ready("주행중") and any(
            e[1] == "이동 명령" and "along_given_route" in e[2] for e in ROBOT.events[-3:]), 240):
        record(label, False, f"J1 주행을 못 만남 (J1 호출 reserved={res1}, J2 reserved={res2})")
        settle()
        return
    last_sid = (q("SELECT MAX(id) FROM dispatch_sessions") or [(0,)])[0][0] or 0
    ds.force_clear(ROBOT_ID, after="reserve")
    wait_until(lambda: (q("SELECT COUNT(*) FROM dispatch_sessions WHERE id>:s", s=last_sid)
                        or [(0,)])[0][0] > 0, 150)
    new = q("SELECT first_poi_id FROM dispatch_sessions WHERE id>:s ORDER BY id LIMIT 1", s=last_sid)
    first = new[0][0] if new else None
    st = q("SELECT status FROM dispatch_reservations WHERE id=:i", i=j1_res[0][0])[0][0] if j1_res else None
    fin = settle(240)
    name = {POI["J1"]["id"]: "J1(R1 — 취소한 작업)", POI["J2"]["id"]: "J2(R2)"}.get(first, str(first))
    good = first == POI["J2"]["id"] and st == "cancelled" and not ROBOT.violations
    record(label, good and fin,
           f"J1호출 예약여부={res1} · 다음 작업={name} · R1 예약상태={st} · 위반 {len(ROBOT.violations)}건"
           + ("" if fin else " · 정리 안 됨"))


def run_case_turn(kind):
    """강제 종료 후 '랙 든 채 선회 → 하차' (2026-10-07).

    배차를 거치지 않고, 랙을 든 로봇을 원하는 자세에 세운 뒤 후속 처리만 돌린다.
    방향은 실제 코드(`_unload_turn_heading`)로 구해 그 기준으로 자세를 틀어 놓는다.
      turn   통로, 90° 어긋남  → 회전 → 잭다운(그때 방향이 다음 경로 쪽) → 이탈
      noturn 통로, 5° 어긋남   → 회전 없이 잭다운
      fail   통로, 90° + 회전 실패 → 세우지 않고 종전 순서(잭다운 → 이탈 → 복귀)
      cell   J1 칸 안, 90° 어긋남 → 돌지 않고 종전 순서(잭다운 → 이탈 → 복귀)
      ※ 2026-10-07 사용자 지시 — 강제 종료 뒤 로봇을 **그 자리에 세우는 일은 없어야 한다**
      near   W2→W3 주행 중 W3 0.9 m 앞에서 종료(2026-10-07 연구소 실측 재현)
             → 바로 앞 W3 가 아니라 그 너머(충전소 쪽)로 돌아야 한다
    """
    label = {"turn": "선회  회전 후 하차", "noturn": "선회  15° 이하 → 회전 없음",
             "fail": "선회  회전 실패 → 종전 순서로 복귀", "cell": "선회  작업지점 칸 안 → 돌지 않고 복귀",
             "near": "선회  경유지 바로 앞 → 그 너머로 회전"}[kind]
    reset()
    if kind == "cell":
        x, y = POI["J1"]["x"], POI["J1"]["y"]
    elif kind == "near":
        w2, w3 = POI.get("W2"), POI.get("W3")
        if not (w2 and w3):
            record(label, False, "맵에 W2·W3 없음")
            return
        travel = math.atan2(w3["y"] - w2["y"], w3["x"] - w2["x"])
        x, y = w3["x"] - 0.9 * math.cos(travel), w3["y"] - 0.9 * math.sin(travel)
    else:
        w1, w2 = POI.get("W1"), POI.get("W2")
        if not (w1 and w2):
            record(label, False, "맵에 W1·W2 없음")
            return
        x, y = (w1["x"] + w2["x"]) / 2, (w1["y"] + w2["y"]) / 2      # 통로 한가운데
    want, dest, _ = ds._unload_turn_heading(ROBOT_ID, "charge", x, y)
    if want is None:
        record(label, False, "다음 경로 방향을 못 구함")
        return
    off = math.radians(5 if kind == "noturn" else 90)
    start_ori = want + off
    if kind == "near":
        start_ori = travel                      # W3 를 향해 달리던 자세 그대로
    with ROBOT.lock:
        ROBOT.x, ROBOT.y, ROBOT.ori = x, y, start_ori
        ROBOT.progress = 1.0                                         # 잭 올라간 상태
        ROBOT.fail_rotate = (kind == "fail")
    js.set_laden(FAKE_IP, True, apply_speed=False)
    t_start = time.time() - ROBOT.t0
    th = threading.Thread(target=ds._force_followup, args=(ROBOT_ID, FAKE_IP, "charge"), daemon=True)
    th.start()
    th.join(240)
    fin = not th.is_alive() and settle(240)
    evs = [e for e in ROBOT.events if e[0] >= t_start]
    down = [e for e in evs if e[1] == "잭 명령" and e[2] == "down"]
    t_down = down[0][0] if down else None
    # 잭다운 전에 나간 '제자리 회전'(같은 좌표 standard) 명령 수
    rot = [e for e in evs if e[1] == "이동 명령" and e[2].startswith("standard")
           and (t_down is None or e[0] < t_down) and f"({x:.1f},{y:.1f})" in e[2]]
    v = ROBOT.violations
    if kind in ("turn", "noturn", "near"):
        d_ori = None
        if ROBOT.jack_down_ori is not None:
            d_ori = abs(math.degrees(math.atan2(math.sin(want - ROBOT.jack_down_ori),
                                                math.cos(want - ROBOT.jack_down_ori))))
        want_rot = 0 if kind == "noturn" else 1
        ok = bool(down) and len(rot) == want_rot and d_ori is not None and d_ori <= 15 and not v and fin
        detail = (f"잭다운 {len(down)}회 · 잭다운 전 회전 {len(rot)}회(기대 {want_rot}) · "
                  f"잭다운 때 경로 방향과 차 {d_ori if d_ori is None else round(d_ori, 1)}° · "
                  f"목적지 {dest.get('name') if dest else None} · 위반 {len(v)}건")
        if kind == "near":
            # 선회 목표가 바로 앞 W3 쪽(=달리던 방향)이면 종전 버그 그대로다
            back = abs(math.degrees(math.atan2(math.sin(want - travel), math.cos(want - travel))))
            detail += f" · 선회 각 {back:.0f}°(W3 쪽이면 0)"
            ok = ok and back > 15
    else:
        # 세우지 않고 끝까지 갔나 — 잭다운 1회 + 충전소(C1) 도착 + 오류 상태 아님
        c1 = POI["C1"]
        home = math.hypot(ROBOT.x - c1["x"], ROBOT.y - c1["y"]) < 0.5
        want_rot = 0 if kind == "cell" else 1          # cell 은 회전 시도조차 없어야 한다
        ok = len(down) == 1 and home and len(rot) >= want_rot and             (kind != "cell" or len(rot) == 0) and not v and fin
        detail = (f"잭다운 {len(down)}회(기대 1) · 충전소 도착 {home} · "
                  f"잭다운 전 회전 시도 {len(rot)}회 · 위반 {len(v)}건")
    if not fin:
        detail += " · 정리 안 됨"
    record(label, ok, detail)
    with ROBOT.lock:
        ROBOT.fail_rotate = False


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
    if not only or "divert" in only:
        run_case_divert()
    for kind in ("turn", "noturn", "fail", "cell", "near"):
        if not only or "선회" in only or kind in only:
            run_case_turn(kind)
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
