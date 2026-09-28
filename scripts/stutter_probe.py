"""주행 중 '멈칫' 의 원인을 한 번에 잡는 진단기 — 읽기만 한다.

    python scripts/stutter_probe.py <로봇IP> [--sec 120] [--out _logs/stutter]

랙을 싣고 문제 구간을 주행시키면서 켜 두면, 아래를 **같은 타임라인**으로 기록한다.

    /maps/1cm/1hz    로봇이 '장애물' 이라고 판단한 결과 (costmap, PNG 로 저장)
    /slam/state      위치추정 신뢰도 (reliable · position_quality · matching_score)
    /planning_state  move_state · 남은거리
    /tracked_pose    실제 위치·방향
    /wheel_state     control_mode · 비상정지
    GET /chassis/status   wheel_overloaded (1초 폴링)

멈춘 순간의 costmap PNG 를 열어 **로봇 자리 앞이 빨간지**만 보면 갈린다.
    빨감  → 어떤 센서가 뭔가를 보고 있다 (그 지점을 눈으로 확인)
    비었음 → 장애물 문제가 아니다 (구동계·위치추정·경로추종)

※ 서버 안전존(우리 코드)이 멈춘 것인지는 **백엔드 로그의 [safety]** 로 따로 본다.
  이 스크립트는 로봇이 스스로 무엇을 보고 있는지만 본다.
"""
import argparse
import base64
import json
import os
import threading
import time

import requests
import websocket

TOPICS = ["/maps/1cm/1hz", "/slam/state", "/planning_state",
          "/tracked_pose", "/wheel_state"]
STATUS_POLL_SEC = 1.0
SPEED_STOP = 0.05          # 이 아래면 '멈춤' 으로 본다


def poll_status(ip: str, log, stop: threading.Event) -> None:
    """`wheel_overloaded` 는 토픽이 없어 REST 로 폴링해야 한다."""
    last = None
    while not stop.is_set():
        try:
            r = requests.get(f"http://{ip}:8090/chassis/status", timeout=3)
            if r.status_code == 200:
                d = r.json()
                cur = (d.get("wheel_overloaded"), d.get("emergency_stop_pressed"))
                if cur != last:
                    log("STATUS", f"wheel_overloaded={cur[0]}  estop={cur[1]}"
                                  + ("   ★ 과부하 발생" if cur[0] else ""))
                    last = cur
        except Exception:
            pass
        stop.wait(STATUS_POLL_SEC)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--sec", type=int, default=120, help="기록 시간(초)")
    ap.add_argument("--out", default="_logs/stutter")
    a = ap.parse_args()

    run = time.strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(a.out, run)
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    lines = []

    def log(tag: str, msg: str) -> None:
        s = f"[{time.time()-t0:7.2f}s] {tag:<8} {msg}"
        print(s, flush=True)
        lines.append(s)

    print(f"기록 위치: {outdir}\n로봇 {a.ip} · {a.sec}초\n")
    stop = threading.Event()
    threading.Thread(target=poll_status, args=(a.ip, log, stop), daemon=True).start()

    ws = websocket.create_connection(f"ws://{a.ip}:8090/ws/v2/topics", timeout=10)
    for t in TOPICS:
        ws.send(json.dumps({"enable_topic": t}))
    ws.settimeout(2.0)

    n_map, moving, last_pose = 0, None, None
    try:
        while time.time() - t0 < a.sec:
            try:
                m = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                log("WS", f"수신 오류: {e}")
                break
            topic = m.get("topic", "")

            if topic.startswith("/maps/"):
                n_map += 1
                name = f"costmap_{n_map:04d}_{time.time()-t0:07.2f}s.png"
                with open(os.path.join(outdir, name), "wb") as f:
                    f.write(base64.b64decode(m["data"]))
                meta = {k: m.get(k) for k in ("resolution", "size", "origin")}
                if n_map == 1:
                    log("COSTMAP", f"{meta}  → {name}")
                    with open(os.path.join(outdir, "costmap_meta.json"), "w",
                              encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=1)

            elif topic == "/slam/state":
                log("SLAM", f"state={m.get('state')} reliable={m.get('reliable')} "
                            f"quality={m.get('position_quality')} "
                            f"match={m.get('lidar_matching_score')}")

            elif topic == "/planning_state":
                log("PLAN", f"move_state={m.get('move_state')} "
                            f"remaining={m.get('remaining_distance')} "
                            f"action={m.get('action_type')} fail={m.get('fail_reason')}")

            elif topic == "/wheel_state":
                log("WHEEL", f"mode={m.get('control_mode')} "
                             f"estop={m.get('emergency_stop_pressed')} "
                             f"released={m.get('wheels_released')}")

            elif topic == "/tracked_pose":
                p = m.get("pos") or [m.get("x"), m.get("y")]
                spd = m.get("speed")
                last_pose = (p, m.get("ori"), spd)
                if spd is not None:
                    mv = abs(float(spd)) > SPEED_STOP
                    if mv != moving:
                        log("MOVE", ("▶ 출발" if mv else "■ 정지")
                                   + f"  속도={spd}  위치={p}")
                        moving = mv
    finally:
        stop.set()
        try:
            ws.close()
        except Exception:
            pass
        with open(os.path.join(outdir, "timeline.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\ncostmap {n_map}장 · timeline.txt 저장 → {outdir}")
        if last_pose:
            print(f"마지막 위치 {last_pose[0]}  방향 {last_pose[1]}")


if __name__ == "__main__":
    main()
