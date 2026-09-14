"""장애물 감지 실시간 관찰 (2026-08-27)

목적 - "장애물 감지 중입니다" 안내음을 어떤 신호로 판정할지 정하기 위해,
로봇 앞을 막았을 때 어떤 필드가 반응하는지 실측한다.

사용법
    python obstacle_watch.py 192.168.30.120
    python obstacle_watch.py 192.168.30.120 --sec 120

    Ctrl+C 로 중단하면 요약이 나온다.

읽는 필드 (값이 바뀔 때만 출력한다)
    /planning_state    stuck_state      <- 막힘 상태. 가장 유력한 후보
                       move_state       moving / succeeded / failed
                       fail_reason_str  실패 사유 문자열
                       remaining_distance
    /fused_sensor_state collide_dir     <- 충돌 방향 (범퍼 없이도 감지될 수 있음)
                       suggested_speed  장애물 근처면 낮아질 것으로 예상
                       accelerability   가속 여지
                       pushed           밀림 감지
    /robot_signal      turn_left/right/brake  <- 회전 신호 (정지 중엔 발행 안 됨)
    /wheel_state       emergency_stop_pressed
"""
import argparse
import json
import sys
import time
from collections import defaultdict

try:
    import websocket
except ImportError:
    sys.exit("websocket-client 가 필요합니다:  pip install websocket-client")

# 콘솔이 cp949 여도 한글/기호가 깨지지 않도록 출력 인코딩을 맞춘다
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

PORT = 8090

# 토픽별로 감시할 필드
WATCH = {
    "/planning_state": ["stuck_state", "move_state", "fail_reason", "fail_reason_str",
                        "remaining_distance", "action_type", "move_intent",
                        "going_back_to_charger"],
    "/fused_sensor_state": ["collide_dir", "suggested_speed", "accelerability",
                            "pushed", "slipping", "is_still"],
    "/robot_signal": ["turn_left", "turn_right", "brake", "reverse"],
    "/wheel_state": ["emergency_stop_pressed", "control_mode", "error_msg"],
    "/slam/state": ["lidar_matched", "position_quality"],
    "/bumper_state": ["front_bumper_pressed", "rear_bumper_pressed"],
}

# 실수 필드는 이 폭 이상 변했을 때만 출력한다 (미세 떨림으로 화면이 넘치는 것 방지)
FLOAT_EPS = {
    "remaining_distance": 0.15,
    "suggested_speed": 0.02,
    "accelerability": 0.02,
    "position_quality": 1.0,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--sec", type=int, default=180, help="관찰 시간(초), 기본 180")
    args = ap.parse_args()

    url = f"ws://{args.ip}:{PORT}/ws/v2/topics"
    print(f"\n연결 : {url}")
    ws = websocket.create_connection(url, timeout=10)
    for topic in WATCH:
        ws.send(json.dumps({"enable_topic": topic}))

    print("구독 :", ", ".join(WATCH))
    print("\n" + "=" * 78)
    print("  로봇을 이동시킨 뒤 경로 앞을 막아보세요. 값이 바뀔 때만 출력됩니다.")
    print("  Ctrl+C 로 중단하면 요약이 나옵니다.")
    print("=" * 78 + "\n")

    last: dict = {}
    changes: dict = defaultdict(int)
    values: dict = defaultdict(set)
    t0 = time.time()
    deadline = t0 + args.sec

    try:
        while time.time() < deadline:
            try:
                pkt = json.loads(ws.recv())
            except Exception as e:
                print(f"[수신 종료] {e}")
                break

            topic = pkt.get("topic")
            fields = WATCH.get(topic)
            if not fields:
                continue

            for f in fields:
                if f not in pkt:
                    continue
                new = pkt[f]
                key = f"{topic}.{f}"
                old = last.get(key, "__none__")

                if old == "__none__":
                    last[key] = new
                    values[key].add(str(new))
                    continue

                # 실수는 임계 이상 변했을 때만
                eps = FLOAT_EPS.get(f)
                if eps is not None and isinstance(new, (int, float)) and isinstance(old, (int, float)):
                    if abs(new - old) < eps:
                        continue

                if new != old:
                    t = time.time() - t0
                    print(f"[{t:7.1f}s] {topic:22s} {f:24s} {old}  ->  {new}")
                    last[key] = new
                    changes[key] += 1
                    values[key].add(str(new))

    except KeyboardInterrupt:
        print("\n(중단됨)")
    finally:
        try:
            ws.close()
        except Exception:
            pass

    # ── 요약 ──
    elapsed = time.time() - t0
    print("\n" + "=" * 78)
    print(f"  관찰 {elapsed:.0f}초 요약")
    print("=" * 78)

    if not changes:
        print("\n  변화 없음 - 로봇이 정지 상태였거나 장애물이 감지되지 않았습니다.")
    else:
        print(f"\n  {'필드':46s} {'변화':>5s}  관측된 값")
        print("  " + "-" * 74)
        for key in sorted(changes, key=lambda k: -changes[k]):
            vals = sorted(values[key])
            shown = ", ".join(vals[:6]) + (" ..." if len(vals) > 6 else "")
            print(f"  {key:46s} {changes[key]:>5d}  {shown}")

    print("\n  [판정 기준]")
    print("   stuck_state 가 none 이외로 바뀜   ->  장애물 판정에 사용 가능 (최우선)")
    print("   collide_dir 가 0 이외로 바뀜      ->  충돌 방향 감지 가능")
    print("   suggested_speed 가 눈에 띄게 하락  ->  간접 신호로 사용 가능")
    print("   fail_reason_str 에 장애물 관련 값  ->  이동 실패 시점에만 판정 가능")
    print("   robot_signal 이 한 번도 안 옴      ->  이동 중에도 미발행이면 회전 판정 불가")
    print()


if __name__ == "__main__":
    main()
