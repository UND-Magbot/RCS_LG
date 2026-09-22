"""로봇 음성 재생 지원 여부 조사 — S300 에서 먼저 재고, L150 과 비교하기 위한 도구.

AutoXing 공식 REST 문서(8090)에는 오디오 API 가 없다.
그런데 JS-SDK(@autoxing/robot-js-sdk) 번들을 뜯어보면 LAN_APP 모드에서
    ws://<robot_ip>:9000/   ← 여기로 {"cmd":"startPlayAudio", ...} 를 보낸다
는 별도 제어 채널이 있다. 이 채널이 우리 로봇에 실제로 떠 있는지가 관건이다.

그래서 이 스크립트는 세 가지를 순서대로 본다.
  1) 포트    8090(섀시 REST) 과 9000~9004(상위기 채널) 가 열려 있는가
  2) caps    /device/info 의 caps 는 모델마다 다르다. 통째로 저장해 S300↔L150 을 나중에 diff 한다
  3) 재생    9000 이 열려 있으면 WS 로 startPlayAudio 를 실제로 던져본다 (--play 일 때만)

사용:
  python scripts/audio_probe.py 192.168.30.101              # 조사만 (로봇을 건드리지 않음)
  python scripts/audio_probe.py 192.168.30.101 --play       # 실제 재생 시도까지
  python scripts/audio_probe.py 192.168.30.101 --play --url http://192.168.0.28:8002/static/audio/moving.mp3
  python scripts/audio_probe.py --diff                      # 저장된 결과끼리 모델 비교

결과는 _logs/audio_probe_<model>_<ip>.json 에 저장된다. 이 파일이 있어야 --diff 가 된다.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "_logs"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests  # noqa: E402

CHASSIS_PORT = 8090
# SDK 가 LAN_APP 모드에서 쓰는 포트들 (index.js 에서 추출)
HOST_PORTS = {
    9000: "명령 WS (startPlayAudio 가 가는 곳)",
    9001: "logger / chassisUrl",
    9002: "serverUrl",
    9003: "queueTask",
    9004: "outlineStatis",
}

# 8090 에 미문서화 오디오 엔드포인트가 있는지 떠보는 후보들.
# 404 = 없음 / 400·405·422 = 있는데 요청이 틀림 → 존재 증거
AUDIO_PROBES = [
    ("POST", "/services/play_audio"),
    ("POST", "/services/audio/play"),
    ("POST", "/services/start_play_audio"),
    ("POST", "/services/tts"),
    ("POST", "/services/speaker/play"),
    ("GET", "/audio"),
    ("GET", "/device/audio"),
]

OK, NG, WARN = "[정상]", "[문제]", "[주의]"


def port_open(ip: str, port: int, timeout: float = 2.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((ip, port)) == 0


def get_json(ip: str, path: str, timeout: float = 5.0):
    try:
        r = requests.get(f"http://{ip}:{CHASSIS_PORT}{path}", timeout=timeout)
        if r.status_code != 200:
            return {"_status": r.status_code}
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def probe_audio_endpoints(ip: str) -> list[dict]:
    """8090 에 오디오 엔드포인트가 숨어 있는지. 404 아닌 응답이 나오면 그게 단서다."""
    rows = []
    for method, path in AUDIO_PROBES:
        try:
            url = f"http://{ip}:{CHASSIS_PORT}{path}"
            if method == "GET":
                r = requests.get(url, timeout=4)
            else:
                r = requests.post(url, json={}, timeout=4)
            body = (r.text or "")[:200].replace("\n", " ")
            rows.append({"method": method, "path": path, "status": r.status_code, "body": body})
        except Exception as e:
            rows.append({"method": method, "path": path, "status": None, "body": str(e)[:120]})
    return rows


def ws_9000_handshake(ip: str, seconds: float = 5.0) -> dict:
    """9000 에 붙어서 뭐라도 내려오는지 본다. 붙기만 해도 채널 존재는 확정이다."""
    import websocket as _ws
    out: dict = {"connected": False, "messages": []}
    ws = None
    try:
        ws = _ws.create_connection(f"ws://{ip}:9000/", timeout=5)
        out["connected"] = True
        ws.settimeout(2)
        end = time.time() + seconds
        while time.time() < end:
            try:
                out["messages"].append(str(ws.recv())[:300])
            except Exception:
                pass
            if len(out["messages"]) >= 5:
                break
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return out


def ws_play_audio(ip: str, robot_sn: str, url: str | None, audio_id: str | None,
                  mode: int, volume: int, interval: int, duration: int) -> dict:
    """SDK 가 만드는 것과 같은 프레임을 직접 보낸다.

    JS-SDK setPlayAudio 구현:
        {deviceId, id, cmd:"startPlayAudio", args:<PlayAudio>, timeoutSec, needConfirm, timestamp}
    """
    import websocket as _ws
    args = {
        "mode": mode,            # 1=상위기 / 2=섀시
        "url": url or "",
        "audioId": audio_id or "",
        "volume": volume,        # 0~100
        "interval": interval,    # 반복 간격(초). -1 이면 1회만
        "num": -1,
        "duration": duration,    # 총 재생 시간(초)
    }
    frame = {
        "deviceId": robot_sn,
        "id": str(uuid.uuid4()),
        "cmd": "startPlayAudio",
        "args": args,
        "timeoutSec": 15,
        "needConfirm": True,
        "timestamp": int(time.time() * 1000),
    }
    out = {"sent": frame, "replies": []}
    ws = None
    try:
        ws = _ws.create_connection(f"ws://{ip}:9000/", timeout=5)
        ws.send(json.dumps(frame))
        ws.settimeout(2)
        end = time.time() + 10
        while time.time() < end:
            try:
                out["replies"].append(str(ws.recv())[:400])
            except Exception:
                pass
            if len(out["replies"]) >= 8:
                break
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return out


def run(ip: str, args) -> dict:
    print(f"\n===== {ip} 조사 시작 =====\n")
    result: dict = {"ip": ip, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 1) 포트
    print("[1] 포트")
    ports = {}
    alive = port_open(ip, CHASSIS_PORT)
    ports[CHASSIS_PORT] = alive
    print(f"  {CHASSIS_PORT:5d}  {OK if alive else NG}  섀시 REST")
    if not alive:
        print(f"\n{NG} 8090 이 안 열림 — 로봇에 도달 못 하는 상태다. 네트워크부터 확인할 것.\n")
        result["ports"] = ports
        return result
    for p, desc in HOST_PORTS.items():
        o = port_open(ip, p)
        ports[p] = o
        print(f"  {p:5d}  {OK if o else '[닫힘]'}  {desc}")
    result["ports"] = ports

    has_9000 = ports.get(9000, False)
    print()
    if has_9000:
        print(f"  {OK} 9000 이 열려 있다 → SDK 의 LAN_APP 채널을 그대로 쓸 수 있다")
    else:
        print(f"  {WARN} 9000 이 닫혀 있다 → 상위기 서비스가 없는 섀시 단품일 가능성")

    # 2) device/info + caps
    print("\n[2] 기기 정보")
    info = get_json(ip, "/device/info")
    result["device_info"] = info
    dev = (info or {}).get("device", {})
    model = dev.get("model", "?")
    sn = dev.get("sn", "?")
    print(f"  model            {model}")
    print(f"  sn               {sn}")
    print(f"  axbot_version    {info.get('axbot_version', '?')}")
    caps = (info or {}).get("caps", {})
    print(f"  caps  {len(caps)} 개")
    for k in sorted(caps):
        print(f"    {k:45s} {caps[k]}")
    # 오디오 냄새가 나는 caps 만 따로
    audio_caps = {k: v for k, v in caps.items()
                  if any(w in k.lower() for w in ("audio", "sound", "speak", "voice", "tts", "volume"))}
    result["audio_caps"] = audio_caps
    print(f"\n  오디오 관련 caps: {audio_caps if audio_caps else '없음'}")

    # 3) 8090 미문서화 엔드포인트 탐색
    print("\n[3] 8090 오디오 엔드포인트 탐색 (404=없음 / 그 외=단서)")
    probes = probe_audio_endpoints(ip)
    result["audio_probes"] = probes
    for row in probes:
        mark = "     " if row["status"] == 404 else "  <<<"
        print(f"  {row['method']:4s} {row['path']:34s} {row['status']}{mark}")
    hits = [r for r in probes if r["status"] not in (404, None)]
    print(f"\n  단서 {len(hits)} 건" + (f" → {[h['path'] for h in hits]}" if hits else ""))

    # 4) 9000 채널
    if has_9000:
        print("\n[4] 9000 WS 접속")
        hs = ws_9000_handshake(ip)
        result["ws9000"] = hs
        if hs.get("connected"):
            print(f"  {OK} 접속됨. 수신 메시지 {len(hs['messages'])} 건")
            for m in hs["messages"][:3]:
                print(f"    {m[:160]}")
        else:
            print(f"  {NG} 접속 실패 — {hs.get('error')}")

        if args.play and hs.get("connected"):
            print("\n[5] startPlayAudio 전송")
            play = ws_play_audio(ip, sn, args.url, args.audio_id, args.mode,
                                 args.volume, args.interval, args.duration)
            result["play"] = play
            print(f"  보냄: {json.dumps(play['sent']['args'], ensure_ascii=False)}")
            if play.get("error"):
                print(f"  {NG} {play['error']}")
            for r in play["replies"]:
                print(f"  응답: {r[:200]}")
            if not play["replies"]:
                print(f"  {WARN} 응답 없음 — 소리가 났는지 귀로 확인할 것")
    else:
        result["ws9000"] = {"connected": False, "skipped": "port closed"}

    # 저장
    OUT_DIR.mkdir(exist_ok=True)
    safe_model = str(model).replace("/", "_")
    out_path = OUT_DIR / f"audio_probe_{safe_model}_{ip.replace('.', '-')}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out_path}")
    return result


def do_diff() -> None:
    """저장된 결과들을 모델끼리 비교 — S300 에서 되던 게 L150 에서도 되는지 판단하는 근거."""
    files = sorted(OUT_DIR.glob("audio_probe_*.json"))
    if len(files) < 2:
        print(f"{WARN} 비교하려면 결과 파일이 2개 이상 필요하다. 현재 {len(files)} 개.")
        for f in files:
            print(f"  {f.name}")
        return
    loaded = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        loaded.append((f.name, d))

    print("\n===== 모델 비교 =====\n")
    print(f"{'항목':38s}" + "".join(f"{n.split('_')[2]:>18s}" for n, _ in loaded))
    print("-" * (38 + 18 * len(loaded)))

    rows = ["model", "axbot_version"]
    for key in rows:
        vals = []
        for _, d in loaded:
            info = d.get("device_info", {})
            v = info.get("device", {}).get("model") if key == "model" else info.get(key)
            vals.append(str(v)[:16])
        print(f"{key:38s}" + "".join(f"{v:>18s}" for v in vals))

    for p in [CHASSIS_PORT] + list(HOST_PORTS):
        vals = ["O" if d.get("ports", {}).get(str(p), d.get("ports", {}).get(p)) else "X"
                for _, d in loaded]
        print(f"{'port ' + str(p):38s}" + "".join(f"{v:>18s}" for v in vals))

    # caps 는 합집합으로 놓고 비교해야 빠진 게 보인다
    all_caps: set[str] = set()
    for _, d in loaded:
        all_caps |= set(d.get("device_info", {}).get("caps", {}) or {})
    print("\n-- caps --")
    for c in sorted(all_caps):
        vals = []
        same = set()
        for _, d in loaded:
            v = (d.get("device_info", {}).get("caps", {}) or {}).get(c, "-")
            vals.append(str(v))
            same.add(str(v))
        mark = "   <<< 차이" if len(same) > 1 else ""
        print(f"{c:38s}" + "".join(f"{v:>18s}" for v in vals) + mark)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip", nargs="?", help="로봇 IP (예: 192.168.30.101)")
    ap.add_argument("--play", action="store_true", help="실제로 startPlayAudio 를 보낸다")
    ap.add_argument("--url", default=None, help="재생할 음원 URL (로봇이 접근 가능해야 함)")
    ap.add_argument("--audio-id", default=None, dest="audio_id",
                    help="내장 음성 ID. 한국어는 2번째 자리 5 (예: 3152047)")
    ap.add_argument("--mode", type=int, default=2, help="1=상위기 / 2=섀시 (기본 2)")
    ap.add_argument("--volume", type=int, default=80, help="0~100 (기본 80)")
    ap.add_argument("--interval", type=int, default=-1, help="반복 간격 초. -1 이면 1회 (기본 -1)")
    ap.add_argument("--duration", type=int, default=10, help="총 재생 시간 초 (기본 10)")
    ap.add_argument("--diff", action="store_true", help="저장된 결과끼리 비교")
    args = ap.parse_args()

    if args.diff:
        do_diff()
        return
    if not args.ip:
        ap.error("로봇 IP 를 주거나 --diff 를 쓸 것")
    if args.play and not args.url and not args.audio_id:
        print(f"{WARN} --play 인데 --url 도 --audio-id 도 없다. 빈 음원으로 시도한다.\n")
    run(args.ip, args)


if __name__ == "__main__":
    main()
