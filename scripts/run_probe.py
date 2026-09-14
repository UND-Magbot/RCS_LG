"""주행 중 '멈칫·정지' 의 원인을 한 번에 기록한다 — **읽기 전용**.

    python scripts/run_probe.py --robot 10.14.182.126 --server 127.0.0.1:8002

켜 둔 채로 R1·R2 호출을 평소대로 누르면 된다. Ctrl+C 로 종료하면 정리해서 저장한다.
로봇에도 서버에도 **아무 명령을 보내지 않는다.** 조회만 한다.


== 필드명 근거 ==
AutoXing WebSocket Reference 원문과, 실기에서 검증된 우리 코드
(services/safety_zone.py · scripts/robot_check.py) 를 대조해 확정했다.

  /tracked_pose    pos[2] · ori          ★ **speed 필드가 없다**(문서 명시).
                                         safety_zone.py 도 위치 차분으로 속도를 구한다.
  /robot_model     footprint · expanded_footprint · width
                                         ★ expanded_footprint 가 **로봇이 회피에 실제로
                                           쓰는 크기**다. footprint 와의 차이가 곧
                                           footprint_expansion 이다.
  /planning_state  move_state · stuck_state · fail_reason · fail_reason_str ·
                   action_type · remaining_distance · viewport_blocked
                                         ★ stuck_state 가 "move_stucked" 면 로봇 스스로
                                           막혔다고 판단한 것이다.
  /slam/state      reliable · lidar_reliable · position_quality ·
                   lidar_matching_score · lidar_matched · wheel_slipping
                                         ★ wheel_slipping 은 바퀴 미끄러짐이다.
  /jack_state      progress(0=내림,1=올림) · weight(적재 하중) · state
                                         ★ 랙을 실었는지, 하중이 얼마인지가 여기 있다.
  /wheel_state     control_mode · emergency_stop_pressed · wheels_released
  /maps/1cm/1hz    resolution · size[2] · origin[2] · data(base64 PNG)
                                         진한 빨강=실제 장애물 / 연한 빨강=팽창 구역

  REST /chassis/status         control_mode · emergency_stop_pressed · wheel_overloaded
  REST /chassis/moves/current  type · state · fail_reason · fail_reason_str
  서버 /api/settings/safety/status   zone(clear/yellow/red/skip) · dist · poi


== 무엇이 갈리는가 ==
  정지 순간 서버 zone = red    -> **서버 안전존**이 멈춘 것
  정지 순간 zone = clear/None  -> 서버는 관여 안 함 -> **로봇**이 멈춘 것
     그때 아래를 보면 이유가 나온다
       costmap 정면 값        로봇이 앞에 뭔가를 보고 있었다
       stuck_state            로봇 스스로 '막혔다' 고 판단했다
       wheel_overloaded       구동계 과부하
       wheel_slipping         바퀴 미끄러짐
       position_quality 저하  위치추정이 흔들렸다
       expanded_footprint     회피에 쓰는 폭이 실제보다 부풀어 있다


== 한계 ==
  · costmap 은 1 Hz — 1초보다 짧은 멈칫은 프레임 사이에 묻힐 수 있다
  · /planning_state · /robot_model 은 약 0.08 Hz(12초 주기) — 낡은 값일 수 있다
  · 서버 폴링 1초 — 그보다 짧은 zone 전이는 놓칠 수 있다
"""
import argparse
import base64
import csv
import json
import math
import os
import sys
import threading
import time

import requests
import websocket

try:                       # 콘솔이 cp949 면 한글이 깨진다
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
# 저장 위치를 저장소 기준으로 고정한다. cwd 기준이면 어디서 실행하느냐에 따라
# 로그가 흩어져 현장에서 '어디 저장됐냐' 를 매번 찾게 된다.
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
try:
    import numpy as np
    from costmap_watch import masks, to_robot_frame, SIDE_FWD
    COSTMAP_READ = True
    _IMPORT_ERR = None
except Exception as _e:     # Pillow/numpy 없어도 PNG 저장은 계속한다
    COSTMAP_READ = False
    _IMPORT_ERR = _e

TOPICS = ["/tracked_pose", "/maps/1cm/1hz", "/slam/state", "/planning_state",
          "/robot_model", "/wheel_state", "/jack_state"]
POLL_SEC = 1.0
STOP_SPEED = 0.05          # 병진 속도가 이 아래면 멈춘 것으로 본다 (m/s)
TURN_STOP = 0.15           # 이 이상 돌고 있으면 제자리 회전 — 정지로 안 센다 (rad/s)
STOP_MIN_SEC = 1.0         # 이만큼 이어져야 '정지 이벤트' 로 센다
FWD_MAX = 4.0              # 정면 판정 상한 (m)
DEFAULT_HALF = 0.44        # /robot_model 을 못 받았을 때만 쓰는 폭 (LG 랙 기준)
NO_STUCK = ("", "none", "no_stuck", "not_stucked")


def poly_half_front(poly):
    """[[x,y], ...] -> (반폭, 앞끝). 로봇 중심 원점, X=좌우 / Y=전후."""
    try:
        return (max(abs(float(q[0])) for q in poly),
                max(float(q[1]) for q in poly))
    except Exception:
        return None, None


class Probe:
    def __init__(self, a):
        self.a = a
        self.run = time.strftime("%Y%m%d_%H%M%S")
        self.dir = os.path.join(a.out, "run_" + self.run)
        os.makedirs(os.path.join(self.dir, "costmap"), exist_ok=True)
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.n_map = 0
        self.events = []
        self.cur = None
        self.moving = None
        self.prev_pose = None
        self.prev_t = 0.0
        self.speed_src = None
        self.notes = []             # 실행 중 알게 된 것 (요약 머리말에 넣는다)
        self.st = {
            "pose": None, "speed": None, "turn": None,
            "zone": None, "zone_dist": None, "zone_poi": None,
            "slam": None, "plan": None, "wheel": None, "jack": None,
            "status": None, "move": None, "cost": None, "model": None,
        }
        self.tl = open(os.path.join(self.dir, "timeline.txt"), "w", encoding="utf-8")
        self.raw = open(os.path.join(self.dir, "raw.jsonl"), "w", encoding="utf-8")
        self.csvf = open(os.path.join(self.dir, "events.csv"), "w",
                         encoding="utf-8-sig", newline="")
        self.csv = csv.writer(self.csvf)
        self.csv.writerow([
            "t", "x", "y", "ori", "speed", "turn",
            "server_zone", "zone_dist",
            "wheel_overloaded", "estop", "wheel_slipping",
            "slam_reliable", "slam_quality", "lidar_matched", "match_score",
            "move_state", "stuck_state", "fail_reason", "fail_reason_str",
            "jack_progress", "jack_weight",
            "cost_front", "cost_left", "cost_right",
        ])

    # -- 기록 --------------------------------------------------
    def log(self, tag, msg):
        s = "[{:8.2f}s] {:<9} {}".format(time.time() - self.t0, tag, msg)
        print(s, flush=True)
        self.tl.write(s + "\n")
        self.tl.flush()

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)
            self.log("INFO", msg)

    def snapshot_row(self):
        with self.lock:
            s = self.st
            p = s["pose"] or (None, None, None)
            c = s["cost"] or {}
            sl = s["slam"] or {}
            pl = s["plan"] or {}
            mv = s["move"] or {}
            stt = s["status"] or {}
            jk = s["jack"] or {}
            row = {
                "t": time.time() - self.t0,
                "zone": s["zone"], "zone_dist": s["zone_dist"],
                "overload": stt.get("wheel_overloaded"),
                "estop": stt.get("emergency_stop_pressed"),
                "slipping": sl.get("wheel_slipping"),
                "reliable": sl.get("reliable"),
                "quality": sl.get("position_quality"),
                "matched": sl.get("lidar_matched"),
                "score": sl.get("lidar_matching_score"),
                "mstate": pl.get("move_state") or mv.get("state"),
                "stuck": pl.get("stuck_state"),
                "fail": pl.get("fail_reason", mv.get("fail_reason")),
                "fail_str": pl.get("fail_reason_str") or mv.get("fail_reason_str"),
                "mtype": mv.get("type") or pl.get("action_type"),
                "jack_prog": jk.get("progress"), "jack_weight": jk.get("weight"),
                "front": c.get("front"), "left": c.get("left"), "right": c.get("right"),
                "turn": s["turn"],
            }
            self.csv.writerow([
                "{:.2f}".format(row["t"]), p[0], p[1], p[2], s["speed"], s["turn"],
                row["zone"], row["zone_dist"],
                row["overload"], row["estop"], row["slipping"],
                row["reliable"], row["quality"], row["matched"], row["score"],
                row["mstate"], row["stuck"], row["fail"], row["fail_str"],
                row["jack_prog"], row["jack_weight"],
                row["front"], row["left"], row["right"],
            ])
            self.csvf.flush()
            if self.cur is not None:
                # ★ 정지 '시작 순간' 만 찍으면 원인 데이터가 아직 안 와 있다 —
                #   wheel_overloaded 는 최대 1초, costmap 은 최대 1초 늦게 도착한다.
                #   그래서 정지가 이어지는 동안 계속 모은다.
                self.cur["samples"].append(row)

    # -- 폴링 스레드 -------------------------------------------
    def poller(self):
        rip, sv = self.a.robot, self.a.server
        last_status = last_zone = None
        while not self.stop_ev.is_set():
            try:
                r = requests.get("http://{}:8090/chassis/status".format(rip), timeout=3)
                if r.ok:
                    d = r.json()
                    with self.lock:
                        self.st["status"] = d
                    k = (d.get("wheel_overloaded"), d.get("emergency_stop_pressed"))
                    if k != last_status:
                        self.log("STATUS", "wheel_overloaded={} estop={}{}".format(
                            k[0], k[1], "   *** 과부하" if k[0] else ""))
                        last_status = k
            except Exception:
                pass
            try:
                r = requests.get(
                    "http://{}:8090/chassis/moves/current".format(rip), timeout=3)
                if r.ok:
                    with self.lock:
                        self.st["move"] = r.json()
            except Exception:
                pass
            try:
                r = requests.get(
                    "http://{}/api/settings/safety/status".format(sv), timeout=3)
                if r.ok:
                    body = r.json() or {}
                    d = body.get(rip) or {}
                    if not d and body:
                        self.note("서버 safety/status 에 {} 키가 없다. 있는 키: {}".format(
                            rip, list(body)[:4]))
                    with self.lock:
                        self.st["zone"] = d.get("zone")
                        self.st["zone_dist"] = d.get("dist")
                        self.st["zone_poi"] = d.get("poi")
                    if d.get("zone") != last_zone:
                        self.log("SERVER", "안전존 zone={} dist={} poi={}".format(
                            d.get("zone"), d.get("dist"), d.get("poi")))
                        last_zone = d.get("zone")
            except Exception:
                pass
            try:
                r = requests.get(
                    "http://{}/api/settings/safety/alerts".format(sv), timeout=3)
                if r.ok and r.json():
                    self.log("ALERT", json.dumps(r.json(), ensure_ascii=False)[:200])
            except Exception:
                pass
            self.snapshot_row()
            self.stop_ev.wait(POLL_SEC)

    # -- 정지 이벤트 -------------------------------------------
    def on_speed(self, spd, pose, turn):
        # 제자리 회전은 정지로 세지 않는다 — 병진만 보면 회전을 멈춤으로 오인한다.
        mv = abs(spd) > STOP_SPEED or turn > TURN_STOP
        now = time.time()
        if self.moving is None:
            self.moving = mv
            return
        if mv == self.moving:
            return
        self.moving = mv
        if not mv:
            self.cur = {"t_start": now, "pose": pose, "samples": []}
            self.log("STOP", "[정지]  위치({:.2f},{:.2f}) 방향{:.1f}도".format(
                pose[0], pose[1], math.degrees(pose[2])))
        elif self.cur:
            self.cur["t_end"] = now
            dur = now - self.cur["t_start"]
            if dur >= STOP_MIN_SEC:
                self.events.append(self.cur)
                self.log("GO", "[출발] (정지 {:.1f}초) -> STOP #{}".format(
                    dur, len(self.events)))
            else:
                self.log("GO", "[출발] (정지 {:.1f}초 - 짧아서 제외)".format(dur))
            self.cur = None
        else:
            self.log("GO", "[출발] (관측 시작 시점부터 멈춰 있었음)")

    # -- 요약 --------------------------------------------------
    def write_summary(self):
        p = os.path.join(self.dir, "summary.txt")
        with open(p, "w", encoding="utf-8") as f:
            def w(s=""):
                f.write(s + "\n")

            w("진단 기록  " + self.run)
            w("로봇 {} · 서버 {}".format(self.a.robot, self.a.server))
            w("총 {:.0f}초 · costmap {}장 · 정지 이벤트 {}건".format(
                time.time() - self.t0, self.n_map, len(self.events)))
            w("속도 판정 : {}".format(self.speed_src or "(포즈를 못 받아 판정 불가)"))

            md = self.st.get("model") or {}
            if md:
                w()
                w("[로봇이 보고한 크기]  <- 랙 적재 여부가 반영된 값이다")
                w("   footprint           반폭 {} cm · 앞끝 {} cm".format(
                    md.get("half_cm"), md.get("front_cm")))
                if md.get("exp_half_cm") is not None:
                    w("   expanded_footprint  반폭 {} cm · 앞끝 {} cm"
                      "   <- 로봇이 회피에 실제로 쓰는 크기".format(
                          md.get("exp_half_cm"), md.get("exp_front_cm")))
                    w("   -> footprint_expansion = 좌우 {} cm / 앞뒤 {} cm".format(
                        md.get("exp_dx_cm"), md.get("exp_dy_cm")))
                else:
                    w("   expanded_footprint  (로봇이 안 보냄)")
                if md.get("width_cm") is not None:
                    w("   width               {} cm".format(md.get("width_cm")))
            if not COSTMAP_READ:
                w()
                w("※ Pillow/numpy 가 없어 costmap 판독을 못 했다. PNG 는 저장돼 있다.")
            if self.notes:
                w()
                w("[실행 중 확인된 것]")
                for n in self.notes:
                    w("   · " + n)
            w()

            if not self.events:
                w("정지 이벤트 없음.")
                return

            for i, e in enumerate(self.events, 1):
                po = e["pose"]
                t_end = e.get("t_end", time.time())
                dur = t_end - e["t_start"]
                sm = e.get("samples") or []

                def vals(key):
                    return [x[key] for x in sm if x.get(key) is not None]

                def uniq(key):
                    out = []
                    for x in sm:
                        v = x.get(key)
                        if not out or out[-1] != v:
                            out.append(v)
                    return out

                w("STOP #{}   {:.1f}s ~ {:.1f}s  ({:.1f}초 정지)  샘플 {}개".format(
                    i, e["t_start"] - self.t0, t_end - self.t0, dur, len(sm)))
                w("   위치            ({:.2f}, {:.2f})  방향 {:.1f}도".format(
                    po[0], po[1], math.degrees(po[2])))

                zs = uniq("zone")
                red = any(z == "red" for z in zs)
                w("   서버 zone       {}{}".format(
                    " -> ".join(str(z) for z in zs) or "(없음)",
                    "        <- 서버 안전존이 멈춘 것" if red
                    else "     <- 서버는 관여 안 함"))

                ov = vals("overload")
                w("   wheel_overload  {}".format(
                    "True ({}/{} 회)  *** 과부하".format(
                        sum(1 for x in ov if x), len(ov))
                    if any(ov) else ("False" if ov else "(관측 없음)")))

                sp = vals("slipping")
                if any(sp):
                    w("   wheel_slipping  True  *** 바퀴 미끄러짐")
                es = vals("estop")
                if any(es):
                    w("   비상정지        눌림  ***")

                q, rl, mt = vals("quality"), vals("reliable"), vals("matched")
                w("   slam            reliable={} quality 최저={} lidar_matched={}".format(
                    "모두 True" if rl and all(rl) else (rl if rl else "(관측 없음)"),
                    min(q) if q else "-",
                    "모두 True" if mt and all(mt) else (mt if mt else "-")))

                fr, lf, rt = vals("front"), vals("left"), vals("right")
                w("   costmap 정면    {}{}".format(
                    "{} m (최소)".format(min(fr)) if fr else "비었음",
                    "   *** 장애물 있음" if fr else ""))
                w("   costmap 좌/우   {} / {}  (최소)".format(
                    min(lf) if lf else "-", min(rt) if rt else "-"))

                stk = [x for x in vals("stuck") if str(x).lower() not in NO_STUCK]
                if stk:
                    w("   stuck_state     {}  *** 로봇이 '막혔다' 고 판단".format(stk[0]))

                fails = [x for x in vals("fail") if x not in (0, "0", "none")]
                fstr = [x for x in vals("fail_str") if x not in ("", "none")]
                w("   move            {} / {} / fail={} {}".format(
                    (vals("mtype") or ["-"])[-1], (vals("mstate") or ["-"])[-1],
                    fails[0] if fails else "없음", fstr[0] if fstr else ""))

                jw, jp = vals("jack_weight"), vals("jack_prog")
                if jw or jp:
                    w("   jack            progress={} weight={}".format(
                        jp[-1] if jp else "-", jw[-1] if jw else "-"))

                bits = []
                if red:
                    bits.append("서버 안전존(RED)")
                if any(ov):
                    bits.append("바퀴 과부하")
                if any(sp):
                    bits.append("바퀴 미끄러짐")
                if any(es):
                    bits.append("비상정지")
                if stk:
                    bits.append("로봇 stuck({})".format(stk[0]))
                if fr and min(fr) < 1.5:
                    bits.append("정면 장애물 {} m".format(min(fr)))
                if q and min(q) < 5:
                    bits.append("위치추정 품질 저하 {}".format(min(q)))
                if mt and not all(mt):
                    bits.append("라이다 매칭 실패")
                if fails:
                    bits.append("이동 실패 {}".format(fails[0]))
                w("   => 짚이는 것     {}".format(
                    ", ".join(bits) if bits else "없음 (위 항목 모두 정상)"))
                w()
        print("\n요약 -> " + p)

    # -- 토픽 처리 ---------------------------------------------
    def on_pose(self, m):
        p = m.get("pos") or [m.get("x"), m.get("y")]
        if not p or p[0] is None:
            return
        pose = (float(p[0]), float(p[1]), float(m.get("ori", m.get("yaw", 0))))
        now_p = time.time()
        # ★ /tracked_pose 에는 speed 필드가 **없다**(문서 명시 · safety_zone.py 도
        #   위치 차분으로 구한다). 혹시 오면 그걸 쓰고, 없으면 계산한다.
        spd = m.get("speed")
        turn = 0.0
        if self.prev_pose is not None and now_p > self.prev_t:
            dt = max(1e-3, now_p - self.prev_t)
            dx = pose[0] - self.prev_pose[0]
            dy = pose[1] - self.prev_pose[1]
            calc = math.hypot(dx, dy) / dt
            d_o = (pose[2] - self.prev_pose[2] + math.pi) % (2 * math.pi) - math.pi
            turn = abs(d_o) / dt
            if spd is None:
                spd = calc
                if self.speed_src is None:
                    self.speed_src = "계산(위치 차분) - tracked_pose 에 speed 없음"
                    self.note(self.speed_src)
            elif self.speed_src is None:
                self.speed_src = "speed 필드"
                self.note("tracked_pose 의 speed 필드를 사용한다")
        self.prev_pose, self.prev_t = pose, now_p
        with self.lock:
            self.st["pose"] = pose
            self.st["turn"] = round(turn, 3)
            if spd is not None:
                self.st["speed"] = round(float(spd), 3)
        if spd is not None:
            self.on_speed(float(spd), pose, turn)

    def on_model(self, m):
        half, front = poly_half_front(m.get("footprint") or [])
        eh, ef = poly_half_front(m.get("expanded_footprint") or [])
        if half is None:
            return
        md = {"half": half, "front": front,
              "half_cm": round(half * 100, 1), "front_cm": round(front * 100, 1)}
        if m.get("width") is not None:
            md["width_cm"] = round(float(m["width"]) * 100, 1)
        if eh is not None:
            md.update({"exp_half": eh, "exp_front": ef,
                       "exp_half_cm": round(eh * 100, 1),
                       "exp_front_cm": round(ef * 100, 1),
                       "exp_dx_cm": round((eh - half) * 100, 1),
                       "exp_dy_cm": round((ef - front) * 100, 1)})
        with self.lock:
            self.st["model"] = md
        msg = "footprint 반폭 {} cm · 앞끝 {} cm".format(md["half_cm"], md["front_cm"])
        if eh is not None:
            msg += "  |  expanded 반폭 {} cm (+{} cm)".format(
                md["exp_half_cm"], md["exp_dx_cm"])
        self.log("MODEL", msg)

    def analyze(self, png, m, name):
        try:
            obs, _ = masks(png)
            res, origin = float(m["resolution"]), m["origin"]
            with self.lock:
                pose = self.st["pose"]
                md = self.st.get("model") or {}
            # 회피에 실제로 쓰는 크기가 있으면 그걸 쓴다
            half = md.get("exp_half") or md.get("half") or self.a.half
            fwd, lat = to_robot_frame(obs, res, origin, pose)
            band = (np.abs(lat) <= half) & (fwd > 0) & (fwd < FWD_MAX)
            near = np.abs(fwd) < SIDE_FWD
            L = lat[near & (lat > 0)]
            R = -lat[near & (lat < 0)]

            def g(arr):
                return round(float(arr.min()), 2) if arr.size else None

            c = {"front": g(fwd[band]), "left": g(L), "right": g(R), "png": name}
            with self.lock:
                self.st["cost"] = c
            if c["front"] is not None and c["front"] < 1.5:
                self.log("COSTMAP", "정면 {} m  좌 {}  우 {}   *** 정면 근접  ({})".format(
                    c["front"], c["left"], c["right"], name))
        except Exception as e:
            self.log("COSTMAP", "판독 실패: {}".format(e))

    # -- 메인 --------------------------------------------------
    def start(self):
        a = self.a
        print("기록 위치 : " + self.dir)
        print("로봇 {} · 서버 {}".format(a.robot, a.server))
        if not COSTMAP_READ:
            print("※ costmap 판독 비활성 (PNG 저장만) - {}".format(_IMPORT_ERR))
        print("Ctrl+C 로 종료\n")

        threading.Thread(target=self.poller, daemon=True).start()
        ws = websocket.create_connection(
            "ws://{}:8090/ws/v2/topics".format(a.robot), timeout=10)
        for t in TOPICS:
            ws.send(json.dumps({"enable_topic": t}))
        ws.settimeout(3.0)
        seen = set()

        try:
            while a.sec == 0 or time.time() - self.t0 < a.sec:
                try:
                    txt = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                m = json.loads(txt)
                topic = m.get("topic", "")
                seen.add(topic)
                if not topic.startswith("/maps/"):        # costmap 은 너무 커서 뺀다
                    rec = {"t": round(time.time() - self.t0, 2)}
                    rec.update(m)
                    self.raw.write(json.dumps(rec, ensure_ascii=False) + "\n")

                if topic == "/tracked_pose":
                    self.on_pose(m)
                elif topic == "/slam/state":
                    with self.lock:
                        self.st["slam"] = m
                    self.log("SLAM", "state={} reliable={} quality={} "
                                     "matched={} slipping={}".format(
                                         m.get("state"), m.get("reliable"),
                                         m.get("position_quality"),
                                         m.get("lidar_matched"),
                                         m.get("wheel_slipping")))
                elif topic == "/planning_state":
                    with self.lock:
                        self.st["plan"] = m
                    self.log("PLAN", "move_state={} stuck={} remaining={} "
                                     "fail={} {}".format(
                                         m.get("move_state"), m.get("stuck_state"),
                                         m.get("remaining_distance"),
                                         m.get("fail_reason"),
                                         m.get("fail_reason_str") or ""))
                elif topic == "/wheel_state":
                    with self.lock:
                        self.st["wheel"] = m
                    self.log("WHEEL", "mode={} estop={} released={}".format(
                        m.get("control_mode"), m.get("emergency_stop_pressed"),
                        m.get("wheels_released")))
                elif topic == "/jack_state":
                    with self.lock:
                        self.st["jack"] = m
                    self.log("JACK", "progress={} weight={} state={}".format(
                        m.get("progress"), m.get("weight"), m.get("state")))
                elif topic == "/robot_model":
                    self.on_model(m)
                elif topic.startswith("/maps/"):
                    self.n_map += 1
                    png = base64.b64decode(m["data"])
                    name = "{:04d}_{:07.2f}s.png".format(
                        self.n_map, time.time() - self.t0)
                    with open(os.path.join(self.dir, "costmap", name), "wb") as f:
                        f.write(png)
                    if COSTMAP_READ and self.st.get("pose"):
                        self.analyze(png, m, name)
        except KeyboardInterrupt:
            print("\n중지 요청")
        finally:
            self.stop_ev.set()
            if self.cur:                 # 종료 시점에 멈춰 있었으면 그것도 기록
                self.cur["t_end"] = time.time()
                self.events.append(self.cur)
            missing = [t for t in TOPICS if t not in seen]
            if missing:
                self.note("로봇이 안 보낸 토픽: {}".format(", ".join(missing)))
            try:
                ws.close()
            except Exception:
                pass
            self.write_summary()
            for h in (self.tl, self.raw, self.csvf):
                h.close()
            print("전체 기록 -> " + self.dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--robot", required=True, help="로봇 IP")
    ap.add_argument("--server", default="127.0.0.1:8002", help="RCS 서버 host:port")
    ap.add_argument("--half", type=float, default=DEFAULT_HALF,
                    help="로봇 반폭(m) 폴백. 로봇이 /robot_model 을 보내면 그 실측값을 쓴다")
    ap.add_argument("--sec", type=int, default=0, help="기록 시간(초). 0 = 무한")
    ap.add_argument("--out", default=os.path.join(_ROOT, "_logs"),
                    help="기본값: <저장소>/_logs (실행 위치와 무관)")
    Probe(ap.parse_args()).start()


if __name__ == "__main__":
    main()
