# -*- coding: utf-8 -*-
"""로봇 상태 스냅샷 — 설정을 바꾸기 전에 현재 상태를 통째로 저장하고, 필요하면 되돌린다.

저장 대상
  system/settings/user     (footprint_expansion, rack.specs, 속도 등 — 영구 저장되는 설정)
  robot-params             (구 API. 속도 계열)
  device/info              (풋프린트 실측·caps — 참고용)
  maps/{id}                (overlay 포함 맵 레코드)
  chassis/current-map

사용법
  python scripts/robot_snapshot.py save                 # 현재 상태 저장
  python scripts/robot_snapshot.py show <스냅샷폴더>     # 저장된 내용 보기
  python scripts/robot_snapshot.py restore <스냅샷폴더>  # user 설정 + overlay 되돌리기

★ restore 는 'user 설정'과 '맵 overlay'만 되돌린다.
  carto_map(SLAM 맵) 은 건드리지 않는다 — 그건 맵 동기화 영역이다.
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

ROBOT_IP = os.getenv("ROBOT_IP", "192.168.30.110")
MAP_ID = int(os.getenv("ROBOT_MAP_ID", "2"))
BASE = f"http://{ROBOT_IP}:8090"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_ROOT = os.path.join(ROOT, "_backup")


def _get(path: str, timeout: int = 15):
    r = requests.get(f"{BASE}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def cmd_save() -> None:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(BACKUP_ROOT, f"robot_snapshot_{stamp}")
    os.makedirs(out, exist_ok=True)

    items = {
        "settings_user": "/system/settings/user",
        "robot_params": "/robot-params",
        "device_info": "/device/info",
        "current_map": "/chassis/current-map",
        f"map_{MAP_ID}": f"/maps/{MAP_ID}",
    }
    saved = []
    for name, path in items.items():
        try:
            data = _get(path)
            with open(os.path.join(out, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            saved.append(name)
        except Exception as e:
            print(f"  ! {name} 저장 실패: {e}")

    with open(os.path.join(out, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"robot_ip": ROBOT_IP, "map_id": MAP_ID, "saved_at": stamp,
                   "items": saved}, f, ensure_ascii=False, indent=1)

    print(f"저장 완료: {out}")
    for n in saved:
        print(f"  - {n}.json")
    print("\n되돌리려면:")
    print(f"  python scripts/robot_snapshot.py restore {os.path.relpath(out, ROOT)}")


def _load(folder: str, name: str):
    p = os.path.join(folder, f"{name}.json")
    if not os.path.exists(p):
        p = os.path.join(ROOT, folder, f"{name}.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def cmd_show(folder: str) -> None:
    su = _load(folder, "settings_user") or {}
    print("[settings/user]")
    for k, v in su.items():
        print(f"  {k} = {json.dumps(v, ensure_ascii=False)[:120]}")
    m = _load(folder, f"map_{MAP_ID}")
    if m:
        ov = json.loads(m.get("overlays") or "{}")
        print(f"\n[map {MAP_ID}] overlays_version={m.get('overlays_version')} "
              f"features={len(ov.get('features', []))}")


def cmd_restore(folder: str) -> None:
    su = _load(folder, "settings_user")
    if su is None:
        sys.exit("settings_user.json 을 찾을 수 없다")

    print("[settings/user] 되돌리는 중...")
    r = requests.patch(f"{BASE}/system/settings/user", json=su, timeout=20)
    print(f"  HTTP {r.status_code}")

    m = _load(folder, f"map_{MAP_ID}")
    if m and m.get("overlays"):
        print(f"[map {MAP_ID}] overlay 되돌리는 중...")
        r = requests.patch(f"{BASE}/maps/{MAP_ID}",
                           json={"overlays": m["overlays"],
                                 "overlays_version": m.get("overlays_version", 1)},
                           timeout=20)
        print(f"  HTTP {r.status_code}")
        print("  ※ 로봇이 읽게 하려면: python scripts/wall_probe.py reload")

    print("\n되돌리기 완료. 확인:")
    print("  python scripts/robot_snapshot.py save   (다시 떠서 비교)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "save"
    if cmd == "save":
        cmd_save()
    elif cmd == "show":
        cmd_show(sys.argv[2])
    elif cmd == "restore":
        cmd_restore(sys.argv[2])
    else:
        print(__doc__)
