"""주행 중 음성 안내 — 실동작 확인용 감시 스크립트.

로봇 9000 채널이 1초마다 흘려보내는 상태(moveState / speed)를 보고 있다가,
로봇이 움직이기 시작하면 음성을 틀고 멈추면 끈다.
로봇을 조종하지는 않는다. 소리만 낸다 — 로봇은 콘솔이나 태블릿으로 평소처럼 움직이면 된다.

이게 그대로 배차 워커에 넣을 로직의 프로토타입이다.

사용:
  python scripts/voice_on_move.py 192.168.30.110
  python scripts/voice_on_move.py 192.168.30.110 --audio-id 3512069 --interval 5
  python scripts/voice_on_move.py 192.168.30.110 --url http://192.168.30.2:8002/static/audio/moving.mp3
  python scripts/voice_on_move.py 192.168.30.110 --watch-only      # 소리 없이 상태만 관찰

Ctrl+C 로 끝내면 재생을 멈추고 나간다.

확인 포인트:
  · 로봇이 출발할 때 "주행 시작" 로그가 뜨고 음성이 나오는가
  · interval 초마다 반복되는가
  · 멈추면 "주행 종료" 와 함께 음성이 그치는가
  · moveState 가 실제로 어떤 값들을 오가는지 (idle 말고 뭐가 나오는지 실측된다)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import websocket as _ws  # noqa: E402

PORT = 9000
# 멈춤으로 볼 상태값. 이 밖의 값이면 주행 중으로 본다.
IDLE_STATES = {"idle", "", "none", "succeeded", "failed", "cancelled"}
# 속도가 이보다 크면 움직이는 것으로 본다 (제자리 회전도 잡으려고 낮게)
MOVING_SPEED = 0.02


def send_cmd(ip: str, sn: str, cmd: str, args: dict, wait: float = 1.5) -> int | None:
    """명령 한 건을 보내고 code 를 받아온다. 상태 스트림은 흘려보낸다."""
    code = None
    ws = None
    try:
        ws = _ws.create_connection(f"ws://{ip}:{PORT}/", timeout=5)
        frame = {
            "deviceId": sn,
            "id": str(uuid.uuid4()),
            "cmd": cmd,
            "args": args,
            "timeoutSec": 15,
            "needConfirm": True,
            "timestamp": int(time.time() * 1000),
        }
        ws.send(json.dumps(frame))
        ws.settimeout(1)
        end = time.time() + wait
        while time.time() < end:
            try:
                d = json.loads(ws.recv())
                if "code" in d:
                    code = d["code"]
                    break
            except Exception:
                pass
    except Exception as e:
        print(f"  [문제] {cmd} 전송 실패 — {e}")
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return code


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--sn", default=None, help="로봇 SN. 없으면 상태 스트림에서 자동으로 읽는다")
    ap.add_argument("--audio-id", default="3512069", dest="audio_id",
                    help="내장 음성 ID (기본 3512069 = 로봇이 임무중입니다, 안전에 주의하세요)")
    ap.add_argument("--url", default=None, help="쓰면 내장 대신 이 URL 음원을 재생")
    ap.add_argument("--mode", type=int, default=2, help="1=상위기 / 2=섀시 (기본 2)")
    ap.add_argument("--volume", type=int, default=100)
    ap.add_argument("--interval", type=float, default=5.0,
                    help="반복 간격 초 (기본 5). 서버가 이 주기로 재생 명령을 다시 보낸다 "
                         "— 로봇 자체 반복(interval 필드)은 동작하지 않음이 실측 확인됨. "
                         "음성 길이(3512069=2.5초)보다 길게 줄 것")
    ap.add_argument("--duration", type=int, default=8, help="1회 재생 허용 시간 초 (기본 8)")
    ap.add_argument("--stop-delay", type=float, default=3.0, dest="stop_delay",
                    help="정지가 이 시간(초) 이어져야 음성을 끈다. 감속 중 끊김 방지 (기본 3)")
    ap.add_argument("--watch-only", action="store_true", help="소리 내지 않고 상태만 본다")
    args = ap.parse_args()

    print(f"\n{args.ip}:{PORT} 감시 시작 — Ctrl+C 로 종료")
    if args.watch_only:
        print("관찰 전용 모드 (소리 안 냄)\n")
    else:
        src = f"url={args.url}" if args.url else f"audioId={args.audio_id}"
        print(f"주행 감지되면: mode={args.mode} {src} volume={args.volume} interval={args.interval}초\n")

    ws = _ws.create_connection(f"ws://{args.ip}:{PORT}/", timeout=6)
    ws.settimeout(3)
    sn = args.sn
    playing = False
    last_state = None
    last_moving = False
    started_at = 0.0
    stopped_since = 0.0
    last_sent = 0.0
    play_count = 0

    try:
        while True:
            try:
                msg = json.loads(ws.recv())
            except Exception:
                continue
            st = msg.get("state")
            if not st:
                continue
            if not sn:
                sn = msg.get("deviceId")
                print(f"로봇 SN 확인: {sn}\n")

            move_state = str(st.get("moveState", ""))
            speed = float(st.get("speed", 0) or 0)
            moving = (move_state.lower() not in IDLE_STATES) or abs(speed) > MOVING_SPEED

            # 원격제어로 밀면 moveState 는 idle 그대로이고 speed 만 오른다.
            # 그래서 "움직임 여부"가 바뀌는 순간도 같이 찍는다.
            if move_state != last_state or moving != last_moving:
                extra = []
                if st.get("isRemoteMode"):
                    extra.append("원격")
                if st.get("isManualMode"):
                    extra.append("수동")
                if st.get("hasObstruction"):
                    extra.append("장애물")
                if st.get("hasPersonAhead"):
                    extra.append("전방사람")
                tag = ("  [" + " ".join(extra) + "]") if extra else ""
                mark = "움직임" if moving else "정지  "
                print(f"[{time.strftime('%H:%M:%S')}] {mark}  moveState={move_state or '(빈값)'}"
                      f"  speed={speed:.2f}{tag}")
                last_state = move_state
                last_moving = moving

            if args.watch_only:
                continue

            if moving:
                stopped_since = 0.0          # 움직이는 동안은 정지 타이머 취소
                now = time.time()
                # 펌웨어의 interval(자체 반복)은 동작하지 않는 것으로 확인됐다(2026-09-03 실측).
                # 그래서 한 번에 1회 재생만 보내고, 반복은 여기서 주기적으로 다시 쏜다.
                if not playing or now - last_sent >= args.interval:
                    a = {
                        "mode": args.mode,
                        "url": args.url or "",
                        "audioId": "" if args.url else args.audio_id,
                        "volume": args.volume,
                        "interval": -1,
                        "num": 1,
                        "duration": args.duration,
                    }
                    code = send_cmd(args.ip, sn, "startPlayAudio", a)
                    last_sent = now
                    if not playing:
                        playing = True
                        started_at = now
                        play_count = 1
                        print(f"[{time.strftime('%H:%M:%S')}] ▶ 주행 시작 — 음성 켬 "
                              f"({args.interval}초마다 반복, code={code})")
                    else:
                        play_count += 1
                        print(f"[{time.strftime('%H:%M:%S')}]    ♪ {play_count}회 (code={code})")

            elif playing:
                # 감속·잠깐 멈춤에 소리가 뚝뚝 끊기지 않도록, 정지가 일정 시간 이어질 때만 끈다
                if stopped_since == 0.0:
                    stopped_since = time.time()
                elif time.time() - stopped_since >= args.stop_delay:
                    code = send_cmd(args.ip, sn, "stopPlayAudio", {"mode": args.mode})
                    dur = time.time() - started_at
                    print(f"[{time.strftime('%H:%M:%S')}] ■ 주행 종료 — 음성 끔 "
                          f"({dur:.0f}초 동안 {play_count}회, code={code})")
                    playing = False
                    stopped_since = 0.0

    except KeyboardInterrupt:
        print("\n종료 중...")
        if playing and sn:
            send_cmd(args.ip, sn, "stopPlayAudio", {"mode": args.mode})
            print("음성 정지 보냄")
    finally:
        try:
            ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
