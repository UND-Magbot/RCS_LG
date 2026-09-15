"""경광등(LightBelt) 제어 실기 확인 — 현장 실측용 단독 스크립트.

## 배경
비상정지 알림(LGIT 요청 6번)에서 "태블릿 문구 + 경광등" 을 같이 하기로 했지만,
**경광등이 실제로 켜지는지 확인된 바가 없다.** 로봇 8090(섀시 REST)에는 조명
API 가 없고, AutoXing JS-SDK 가 LAN 모드에서 쓰는 `ws://<ip>:9000/` 명령 채널에
`openLightBelt` / `closeLightBelt` 가 있다(오디오 `startPlayAudio` 와 같은 채널).
그 채널에 우리 로봇이 응답하는지, 색이 바뀌는지를 **눈으로 보고 판단**하려고
만든 도구다. 서버 코드는 건드리지 않는다.

## 사용
    python scripts/light_probe.py 192.168.39.100
    python scripts/light_probe.py 192.168.39.100 --color red --sec 8
    python scripts/light_probe.py 192.168.39.100 --color green --no-close   # 켜둔 채 종료

  · `--sec` 초 뒤에 `closeLightBelt` 로 원래대로 되돌린다(기본 5초).
  · `--no-close` 를 주면 되돌리지 않는다. 색이 실제로 바뀌는지 오래 보고 싶을 때.
  · `--sn` 으로 시리얼을 직접 줄 수 있다. 안 주면 `GET /device/info` 로 읽는다.

## 현장 조건
**인터넷이 없다.** 그래서 외부 패키지를 새로 받지 않는다 —
`requests` 와 `websocket-client` 는 이미 서버가 쓰고 있는 것들이다.
프레임 규격은 `BackEnd/app/services/robot_voice.py` 의 `_send_cmd` 와 동일하다.

## 보는 법
  · `code: 0`        → 명령을 받아들였다. **눈으로 등이 켜졌는지 확인할 것.**
                       (오디오 때도 `interval` 은 code 0 을 주고 실제로는 안 먹었다.
                        code 0 은 '거부하지 않았다' 는 뜻이지 '동작했다' 가 아니다)
  · `code: 0` 아님    → 거부. 메시지를 그대로 적어두면 다음 판단에 쓸 수 있다.
  · 응답 없음         → 그 채널에 이 명령이 없거나, 9000 이 안 열려 있다.
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

import requests
from websocket import create_connection

CMD_PORT = 9000        # 명령 채널 (8090 섀시 REST 와 별개)
CHASSIS_PORT = 8090

# SDK 의 LightColor 열거값. 실제로 어떤 값이 먹는지가 이 스크립트로 확인할 대상이다.
COLORS = {
    "red": 1,
    "green": 2,
    "blue": 3,
    "yellow": 4,
    "purple": 5,
    "cyan": 6,
    "white": 7,
}


def get_sn(ip: str, timeout: float = 5.0) -> str:
    """로봇 시리얼(deviceId). robot_voice 와 같은 출처를 쓴다.

    LTE 에서 /device/info 가 느릴 수 있다 — 실패하면 빈 문자열로 진행한다.
    (deviceId 가 비어 있어도 명령이 먹는 기종이 있어 한 번은 시도해볼 가치가 있다)
    """
    try:
        r = requests.get(f"http://{ip}:{CHASSIS_PORT}/device/info", timeout=timeout)
        d = r.json()
        # 키 이름이 펌웨어마다 달라서 후보를 훑는다. 최상위 → device 하위 순.
        keys = ("serial_number", "sn", "device_sn", "serialNumber")
        for src in (d, d.get("device") or {}):
            for key in keys:
                if isinstance(src, dict) and src.get(key):
                    return str(src[key])
        return ""
    except Exception as e:
        print(f"  ! 시리얼 조회 실패({e}) — 빈 deviceId 로 진행한다")
        return ""


def send_cmd(ip: str, sn: str, cmd: str, args: dict, wait: float = 2.0):
    """명령 한 건 보내고 code 를 받아온다. (robot_voice._send_cmd 와 같은 프레임)"""
    ws = None
    code = None
    raw_reply = None
    try:
        ws = create_connection(f"ws://{ip}:{CMD_PORT}/", timeout=5)
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
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                d = json.loads(ws.recv())
            except Exception:
                continue
            if "code" in d:          # 상태 스트림 메시지는 흘려보낸다
                code = d["code"]
                raw_reply = d
                break
    except Exception as e:
        print(f"  ! {cmd} 전송 실패: {e}")
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return code, raw_reply


def main() -> int:
    ap = argparse.ArgumentParser(description="경광등(LightBelt) 실기 확인")
    ap.add_argument("ip", help="로봇 IP")
    ap.add_argument("--color", default="red", choices=sorted(COLORS),
                    help="켤 색 (기본 red)")
    ap.add_argument("--sec", type=float, default=5.0,
                    help="이 시간 뒤 closeLightBelt 로 되돌린다 (기본 5초)")
    ap.add_argument("--no-close", action="store_true",
                    help="되돌리지 않고 켜둔 채 끝낸다")
    ap.add_argument("--sn", default="", help="시리얼을 직접 지정 (기본: 로봇에서 조회)")
    a = ap.parse_args()

    color_val = COLORS[a.color]
    print(f"[1] 로봇 {a.ip} 시리얼 확인")
    sn = a.sn or get_sn(a.ip)
    print(f"    deviceId = {sn or '(비어 있음)'}")

    print(f"[2] openLightBelt — color={a.color}({color_val})")
    code, reply = send_cmd(a.ip, sn, "openLightBelt", {"color": color_val})
    print(f"    code = {code}   응답 = {reply}")
    if code is None:
        print("    → 응답이 없다. 9000 채널에 이 명령이 없거나 포트가 막혀 있다.")
    elif code == 0:
        print("    → 거부되지 않았다. ★ 지금 로봇 등을 눈으로 확인할 것.")
    else:
        print("    → 거부됨. 위 응답 내용을 그대로 기록해 둘 것.")

    if a.no_close:
        print("[3] --no-close — 끄지 않고 끝낸다")
        return 0

    print(f"[3] {a.sec:.0f}초 대기 후 closeLightBelt")
    time.sleep(max(0.0, a.sec))
    code2, reply2 = send_cmd(a.ip, sn, "closeLightBelt", {})
    print(f"    code = {code2}   응답 = {reply2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
