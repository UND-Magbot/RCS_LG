"""로봇 상태 한 번에 확인 — 테스트 전/후 점검용.

REST 로는 못 읽는 값들(위치추정·잭·랙검출)이 많아서 WebSocket 을 같이 본다.
백엔드가 꺼져 있어도 동작한다 (로봇에 직접 붙음).

사용:
  python scripts/robot_check.py                 # 한 번 확인
  python scripts/robot_check.py --watch         # 3초마다 계속 (Ctrl+C 로 종료)
  python scripts/robot_check.py --rack          # 랙 검출까지 (감지 서비스를 켰다 끔)

확인 항목:
  위치추정  lidar_matched / matching_score  ← 이게 False 면 로봇이 자기 위치를 잘못 안다
  섀시      비상정지 / 제어모드 / 바퀴과부하
  잭        progress(0=내려감, 1=올라감) / weight(랙 적재 하중)
  위치      현재 좌표 + 등록된 R1 과의 차이
  랙검출    --rack 옵션일 때만. 로봇이 랙 아래에 있어야 잡힌다
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "BackEnd"))
os.environ.setdefault("DB_NAME", "rcs_lg_db")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_USER", "root")
os.environ.setdefault("DB_PASSWORD", "1234")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests  # noqa: E402

PORT = 8090
OK, NG, WARN = "[정상]", "[문제]", "[주의]"


def ws_snapshot(ip: str, topics: list[str], seconds: float = 6.0) -> dict:
    """여러 토픽을 한 번에 구독해 마지막 값을 모은다."""
    import websocket as _ws
    out: dict = {}
    racks = defaultdict(list)
    ws = None
    try:
        ws = _ws.create_connection(f"ws://{ip}:{PORT}/ws/v2/topics", timeout=4)
        for t in topics:
            ws.send(json.dumps({"enable_topic": t}))
        ws.settimeout(2)
        end = time.time() + seconds
        while time.time() < end:
            try:
                pkt = json.loads(ws.recv())
            except Exception:
                continue
            tp = pkt.get("topic")
            if tp == "/detected_rack":
                out.setdefault("_rack_packets", 0)
                out["_rack_packets"] += 1
                if pkt.get("rack_detected"):
                    b = pkt.get("rack_box_aligned") or pkt.get("rack_box")
                    if b and "pose" in b:
                        racks[pkt.get("frame", "?")].append(
                            (b["pose"]["pos"][0], b["pose"]["pos"][1], b["pose"]["ori"],
                             b.get("width"), b.get("height")))
            elif tp:
                out[tp] = pkt
    except Exception as e:
        out["_error"] = f"{type(e).__name__}: {e}"
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    if racks:
        out["_racks"] = dict(racks)
    return out


def load_r1() -> dict | None:
    try:
        from app.database import SessionLocal
        from app.models.map import MapPOI, RobotMap
        db = SessionLocal()
        try:
            m = (db.query(RobotMap).filter(RobotMap.is_active == True)   # noqa: E712
                 .order_by(RobotMap.id.desc()).first())
            if not m:
                return None
            p = (db.query(MapPOI)
                 .filter(MapPOI.map_id == m.id, MapPOI.poi_type == "standby",
                         MapPOI.is_active == True)                        # noqa: E712
                 .first())
            if not p or p.world_x is None:
                return None
            return {"name": p.name, "x": p.world_x, "y": p.world_y,
                    "ori": p.angle or 0, "rack_size": p.rack_size}
        finally:
            db.close()
    except Exception:
        return None


def norm_deg(rad_a: float, rad_b: float) -> float:
    d = math.degrees(rad_a - rad_b)
    while d > 180:
        d -= 360
    while d < -180:
        d += 360
    return d


def report(ip: str, r1: dict | None, with_rack: bool) -> None:
    topics = ["/slam/state", "/jack_state", "/tracked_pose"]
    if with_rack:
        topics.append("/detected_rack")
        try:
            requests.post(f"http://{ip}:{PORT}/services/start_rack_size_detection", timeout=8)
        except Exception:
            pass

    snap = ws_snapshot(ip, topics, seconds=10 if with_rack else 6)

    if with_rack:
        try:
            requests.post(f"http://{ip}:{PORT}/services/stop_rack_size_detection", timeout=8)
        except Exception:
            pass

    print("=" * 66)
    print(f"  로봇 {ip}   {time.strftime('%H:%M:%S')}")
    print("=" * 66)

    if snap.get("_error"):
        print(f"  {NG} 로봇 연결 실패 — {snap['_error']}")
        return

    # ── 위치추정 ──
    s = snap.get("/slam/state")
    if s:
        matched = bool(s.get("lidar_matched"))
        score = s.get("lidar_matching_score")
        mark = OK if matched else NG
        print(f"  위치추정  {mark}  lidar_matched={matched}  score={score}")
        print(f"            reliable={s.get('reliable')} "
              f"quality={s.get('position_quality')} "
              f"wheel_slipping={s.get('wheel_slipping')}")
        if not matched:
            print("            ↳ 로봇이 자기 위치를 잘못 알고 있을 수 있습니다.")
            print("              충전소 복귀 후 위치재조정 필요 (POST /api/map/relocalize)")
    else:
        print(f"  위치추정  {WARN}  /slam/state 미수신")

    # ── 섀시 ──
    try:
        st = requests.get(f"http://{ip}:{PORT}/chassis/status", timeout=8).json()
        estop = bool(st.get("emergency_stop_pressed"))
        mode = str(st.get("control_mode", "?"))
        over = bool(st.get("wheel_overloaded"))
        bad = estop or mode != "auto" or over
        print(f"  섀시      {NG if bad else OK}  비상정지={estop} 제어모드={mode} 바퀴과부하={over}")
    except Exception as e:
        print(f"  섀시      {NG}  조회 실패: {e}")

    # ── 잭 ──
    j = snap.get("/jack_state")
    if j:
        prog = j.get("progress")
        wt = j.get("weight")
        try:
            up = float(prog) >= 0.9
        except (TypeError, ValueError):
            up = None
        label = "올라감(랙 적재중)" if up else ("내려감" if up is False else "판정불가")
        mark = WARN if up else OK
        print(f"  잭        {mark}  {label}   state={j.get('state')} "
              f"progress={prog} weight={wt}")
        if up and (wt or 0) < 30:
            print("            ↳ 잭은 올라갔는데 하중이 낮습니다 — 랙을 제대로 못 받쳤을 수 있음")
    else:
        print(f"  잭        {WARN}  /jack_state 미수신")

    # ── 위치 ──
    p = snap.get("/tracked_pose")
    if p and p.get("pos"):
        px, py, po = p["pos"][0], p["pos"][1], p.get("ori", 0)
        print(f"  현재위치  ({px:+.3f}, {py:+.3f})  ori={math.degrees(po):+.1f}도")
        if r1:
            dist = math.hypot(px - r1["x"], py - r1["y"])
            print(f"            {r1['name']} 까지 {dist*100:.0f}cm, "
                  f"방향차 {norm_deg(po, r1['ori']):+.1f}도")
    else:
        print("  현재위치  (정지 중이면 /tracked_pose 가 발행되지 않습니다 — 정상)")

    if r1:
        print(f"  랙 위치   {r1['name']}  ({r1['x']:+.3f}, {r1['y']:+.3f}) "
              f"ori={math.degrees(r1['ori']):+.1f}도  rack_size={r1['rack_size']}")

    # ── 랙 검출 ──
    if with_rack:
        pk = snap.get("_rack_packets", 0)
        racks = snap.get("_racks") or {}
        n = sum(len(v) for v in racks.values())
        mark = OK if n else WARN
        print(f"  랙검출    {mark}  패킷 {pk}개 / 검출 {n}개")
        if not n:
            print("            ↳ 로봇이 랙 아래에 있어야 검출됩니다. 앞에서 봐서는 안 잡힙니다")
        for fr, v in racks.items():
            mx = statistics.median([a[0] for a in v])
            my = statistics.median([a[1] for a in v])
            mo = statistics.median([a[2] for a in v])
            mw = statistics.median([a[3] for a in v if a[3] is not None] or [0])
            mh = statistics.median([a[4] for a in v if a[4] is not None] or [0])
            print(f"            frame={fr} n={len(v)} pos=({mx:+.3f},{my:+.3f}) "
                  f"ori={math.degrees(mo):+.1f}도 size={mw:.3f}x{mh:.3f}")
            if fr == "map" and r1:
                print(f"              ★ {r1['name']} 대비 "
                      f"위치 {math.hypot(mx-r1['x'], my-r1['y'])*100:.1f}cm "
                      f"각도 {norm_deg(mo, r1['ori']):+.1f}도")
    print("=" * 66)


def main() -> int:
    ap = argparse.ArgumentParser(description="로봇 상태 한 번에 확인")
    ap.add_argument("--ip", default="192.168.30.100")
    ap.add_argument("--watch", action="store_true", help="3초마다 반복")
    ap.add_argument("--rack", action="store_true", help="랙 검출도 확인 (감지 서비스 on/off)")
    cfg = ap.parse_args()

    r1 = load_r1()
    if r1 is None:
        print("(R1 조회 실패 — DB가 꺼져 있으면 좌표 비교는 생략됩니다)")

    try:
        while True:
            report(cfg.ip, r1, cfg.rack)
            if not cfg.watch:
                break
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
