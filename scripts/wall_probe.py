# -*- coding: utf-8 -*-
"""가상벽 속성 검증 도구 — 로봇 맵 overlay 의 LineString 속성을 바꿔보고 되돌린다.

배경 (2026-09-04):
  우리 백엔드는 가상벽을 LineString + properties.type="1" 로 내보내고 있는데,
  로봇에 실제로 탑재된 파서는 LineString 에서 properties.lineType 만 읽는다.
  (rb-admin 번들 / axbot-ts-sdk mapInfo.ts / overlays 규격 3곳 모두 동일)
  따라서 지금 형태로는 "가상벽"으로 분류되지 않아 경로계획을 막지 못한다.

  이 스크립트로 실기에서 확정한 뒤 map.py 를 고친다.

사용법:
  python scripts/wall_probe.py show                 # 현재 overlay 덤프 (저장본 + 라이브 비교)
  python scripts/wall_probe.py fix                  # type:"1" → lineType:"2" (백업 자동 저장)
  python scripts/wall_probe.py reload               # 로봇이 새 overlay 를 읽게 함 (포즈 보존)
  python scripts/wall_probe.py clear                # 로봇 overlay 에서 가상벽만 제거 (백업 자동)
  python scripts/wall_probe.py restore <백업파일>   # 백업으로 되돌리기

★ PATCH /maps/{id} 는 맵 '레코드'만 고친다. 주행 중인 로봇 메모리의 맵은 그대로다.
  그래서 fix 뒤에는 반드시 reload 를 해야 rb-admin·경로계획에 반영된다. (2026-09-04 실증)

★ 백엔드 '맵 동기화'를 다시 돌리면 안 된다 — DB 기준으로 type:"1" 을 재생성해 fix 가 원복된다.
"""
import json
import sys
import time

import requests
import websocket

ROBOT_IP = "192.168.30.110"
MAP_ID = 2
BASE = f"http://{ROBOT_IP}:8090"
BACKUP_DIR = "_backup"


def get_map() -> dict:
    r = requests.get(f"{BASE}/maps/{MAP_ID}", timeout=15)
    r.raise_for_status()
    return r.json()


def dump(overlays: dict) -> None:
    for f in overlays.get("features", []):
        p = f.get("properties", {}) or {}
        g = (f.get("geometry") or {}).get("type")
        print(f"  {g:11} name={str(p.get('name')):22} "
              f"type={p.get('type')} lineType={p.get('lineType')} regionType={p.get('regionType')}")


def save_backup(m: dict) -> str:
    import os
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = f"{BACKUP_DIR}/robot_map{MAP_ID}_overlay_{time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(m, fp, ensure_ascii=False, indent=1)
    return path


def cmd_show() -> None:
    m = get_map()
    ov = json.loads(m["overlays"])
    print(f"overlays_version={m['overlays_version']}  features={len(ov['features'])}")
    dump(ov)


def cmd_fix() -> None:
    m = get_map()
    print(f"[현재] overlays_version={m['overlays_version']}")
    ov = json.loads(m["overlays"])
    dump(ov)

    path = save_backup(m)
    print(f"\n[백업] {path}")

    changed = 0
    for f in ov.get("features", []):
        if (f.get("geometry") or {}).get("type") != "LineString":
            continue
        p = f.setdefault("properties", {})
        p.pop("type", None)          # 규격에 없는 값 — 제거
        p["lineType"] = "2"          # MapPolylineType.virtualWall
        p["mapOverlay"] = True
        changed += 1

    if not changed:
        print("교정할 LineString 이 없다. 중단.")
        return

    body = {"overlays": json.dumps(ov),
            "overlays_version": m["overlays_version"] + 1}
    r = requests.patch(f"{BASE}/maps/{MAP_ID}", json=body, timeout=20)
    print(f"\n[PATCH] {changed}건 교정 → HTTP {r.status_code} {r.text[:200]}")

    time.sleep(1.5)
    after = get_map()
    ov2 = json.loads(after["overlays"])
    print(f"\n[확인] overlays_version={after['overlays_version']}  features={len(ov2['features'])}")
    dump(ov2)


def read_tracked_pose(timeout: float = 6.0):
    """WS /tracked_pose 로 현재 포즈를 읽는다. (이 펌웨어는 GET /chassis/pose 가 안 된다)"""
    ws = None
    try:
        ws = websocket.create_connection(f"ws://{ROBOT_IP}:8090/ws/v2/topics", timeout=4)
        ws.send(json.dumps({"enable_topic": "/tracked_pose"}))
        ws.settimeout(2)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                pkt = json.loads(ws.recv())
            except Exception:
                continue
            if pkt.get("topic") == "/tracked_pose" and pkt.get("pos"):
                p = pkt["pos"]
                return {"position": [float(p[0]), float(p[1]), 0],
                        "ori": float(pkt.get("ori", 0.0))}
    except Exception as e:
        print(f"  포즈 읽기 실패: {e}")
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return None


def live_overlay_version():
    """로봇이 실제로 쓰는 맵(/map/info)의 overlays_version 과 가상벽 속성."""
    ws = None
    try:
        ws = websocket.create_connection(f"ws://{ROBOT_IP}:8090/ws/v2/topics", timeout=6)
        ws.send(json.dumps({"enable_topic": "/map/info"}))
        ws.settimeout(4)
        deadline = time.time() + 10
        while time.time() < deadline:
            m = json.loads(ws.recv())
            if m.get("topic") != "/map/info":
                continue
            ov = m.get("overlays")
            ov = json.loads(ov) if isinstance(ov, str) else (ov or {})
            walls = [f.get("properties", {}) for f in ov.get("features", [])
                     if (f.get("geometry") or {}).get("type") == "LineString"]
            return m.get("overlays_version"), walls
    except Exception as e:
        print(f"  /map/info 읽기 실패: {e}")
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
    return None, []


def cmd_reload() -> None:
    """current-map 재선택으로 로봇이 새 overlay 를 읽게 한다. 포즈는 저장했다가 복원."""
    saved = read_tracked_pose()
    if not saved:
        print("포즈를 못 읽었다. 재선택하면 위치추정이 리셋될 수 있으므로 중단한다.")
        return
    print(f"[포즈 저장] pos={saved['position'][:2]} ori={saved['ori']:.3f}")

    ok = False
    for attempt in range(3):
        try:
            requests.post(f"{BASE}/chassis/current-map", json={"map_id": MAP_ID}, timeout=15)
        except Exception as e:
            print(f"  current-map 설정 요청 실패({attempt + 1}): {e}")
        deadline = time.time() + 6
        while time.time() < deadline:
            try:
                if requests.get(f"{BASE}/chassis/current-map", timeout=8).json().get("id") == MAP_ID:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if ok:
            break
        print(f"  미반영 — 재시도 {attempt + 1}/3")
    print(f"[current-map] 재선택 {'성공' if ok else '실패'}")

    time.sleep(1.0)
    try:
        requests.post(f"{BASE}/chassis/pose", json=saved, timeout=8)
        print("[포즈 복원] 완료")
    except Exception as e:
        print(f"[포즈 복원] 실패: {e}  ← 위치 재조정 필요할 수 있다")

    time.sleep(1.5)
    ver, walls = live_overlay_version()
    print(f"\n[라이브 확인] /map/info overlays_version={ver}")
    for p in walls:
        print(f"   {p.get('name')}  type={p.get('type')}  lineType={p.get('lineType')}")
    if walls and all(str(p.get("lineType")) == "2" for p in walls):
        print("\n→ 로봇이 새 overlay 를 읽었다. rb-admin F5 하면 빨간색이어야 한다.")
    else:
        print("\n→ 아직 옛 overlay 다. 재확인 필요.")


def cmd_clear() -> None:
    """로봇 맵 overlay 에서 가상벽(LineString) 만 전부 제거. POI·충전소는 그대로 둔다.

    ★ 맵 편집기에서 지우고 동기화해도 로봇에서는 안 지워진다 —
      동기화 코드가 "DB 에 가상벽이 없으면 로봇의 기존 것을 보존" 하기 때문.
      그래서 로봇 쪽은 이 명령으로 따로 지워야 한다.
    """
    m = get_map()
    ov = json.loads(m["overlays"])
    print(f"[현재] overlays_version={m['overlays_version']}  features={len(ov['features'])}")
    dump(ov)

    path = save_backup(m)
    print(f"\n[백업] {path}")

    before = len(ov["features"])
    ov["features"] = [f for f in ov["features"]
                      if (f.get("geometry") or {}).get("type") != "LineString"]
    removed = before - len(ov["features"])
    if not removed:
        print("지울 가상벽이 없다.")
        return

    body = {"overlays": json.dumps(ov), "overlays_version": m["overlays_version"] + 1}
    r = requests.patch(f"{BASE}/maps/{MAP_ID}", json=body, timeout=20)
    print(f"\n[PATCH] 가상벽 {removed}개 제거 → HTTP {r.status_code}")
    print("★ 로봇이 읽게 하려면 이어서:  python scripts/wall_probe.py reload")


def cmd_restore(path: str) -> None:
    with open(path, encoding="utf-8") as fp:
        m = json.load(fp)
    body = {"overlays": m["overlays"], "overlays_version": m["overlays_version"]}
    r = requests.patch(f"{BASE}/maps/{MAP_ID}", json=body, timeout=20)
    print(f"[RESTORE] {path} → HTTP {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        cmd_show()
    elif cmd == "fix":
        cmd_fix()
    elif cmd == "reload":
        cmd_reload()
    elif cmd == "clear":
        cmd_clear()
    elif cmd == "restore":
        cmd_restore(sys.argv[2])
    else:
        print(__doc__)
