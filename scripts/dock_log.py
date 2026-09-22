"""충전 도킹 이력 조회 — 왜 도킹에 실패했는지 볼 때.

백엔드 로그에는 "도킹 명령 전송" 까지만 남는다. **실패 사유는 로봇이 들고 있다.**
`GET /chassis/moves` 는 목표 좌표를 안 주므로, 실패 건만 골라 `/chassis/moves/{id}` 로
한 번 더 물어봐야 좌표까지 나온다. 그 두 단계를 대신 해준다.

사용:
  python scripts/dock_log.py 192.168.30.110              # 최근 charge 이동 이력
  python scripts/dock_log.py 192.168.30.110 --all        # charge 말고 전부
  python scripts/dock_log.py 192.168.30.110 --limit 40

DB 의 충전소 POI 좌표와 나란히 찍어주므로, 실패한 좌표가 지금 등록된 값과
다르면 바로 눈에 보인다 (맵을 새로 만들면 POI 좌표가 바뀐다 — 실패의 단골 원인).

자주 나오는 실패 사유:
  101 charge_dock_detection_error  그 자리엔 갔는데 센서가 독을 못 봄
                                   → 각도 틀림 / 너무 멂 / 반사판 가림·오염
  103 invalid_charge_dock          그 좌표에 등록된 독이 아예 없음
                                   → POI 좌표가 실제와 다름 (맵 갱신 후 단골)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

PORT = 8090
DETAIL_KEYS = ("target_x", "target_y", "target_ori", "fail_reason",
               "fail_reason_str", "fail_message", "create_time")


def ts(v) -> str:
    try:
        return datetime.fromtimestamp(int(v)).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "-"


def charging_pois() -> list[tuple]:
    """DB 에 등록된 충전소 POI. 실패해도 조회는 계속한다."""
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "BackEnd"))
        import os
        os.environ.setdefault("DB_NAME", "rcs_lg_db")
        os.environ.setdefault("DB_HOST", "127.0.0.1")
        os.environ.setdefault("DB_USER", "root")
        os.environ.setdefault("DB_PASSWORD", "1234")
        import pymysql
        c = pymysql.connect(host=os.environ["DB_HOST"], user=os.environ["DB_USER"],
                            password=os.environ["DB_PASSWORD"], database=os.environ["DB_NAME"],
                            charset="utf8mb4")
        cur = c.cursor()
        cur.execute("SELECT p.id, p.map_id, p.name, p.world_x, p.world_y, p.angle "
                    "FROM map_pois p WHERE p.poi_type='charging' AND p.is_active=1 "
                    "ORDER BY p.map_id DESC")
        rows = cur.fetchall()
        c.close()
        return rows
    except Exception as e:
        print(f"  (DB 조회 실패 — 좌표 대조 생략: {e})")
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ip")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all", action="store_true", help="charge 외 이동도 전부 표시")
    args = ap.parse_args()

    print(f"\n=== 등록된 충전소 POI (DB) ===")
    pois = charging_pois()
    for pid, map_id, name, wx, wy, ang in pois:
        print(f"  id={pid:<5} map={map_id:<4} {name:<6} world=({wx:.3f}, {wy:.3f})  angle={ang}")
    if not pois:
        print("  (없음)")

    print(f"\n=== 이동 이력 — {args.ip} ===")
    try:
        moves = requests.get(f"http://{args.ip}:{PORT}/chassis/moves", timeout=8).json()
    except Exception as e:
        print(f"  로봇 조회 실패: {e}")
        return

    shown = 0
    for m in moves:
        if not args.all and m.get("type") != "charge":
            continue
        if shown >= args.limit:
            break
        shown += 1
        mid, state = m.get("id"), m.get("state")
        mark = {"succeeded": "성공", "failed": "실패", "cancelled": "취소"}.get(state, state)
        line = f"  [{mark}] id={mid:<6} {m.get('type'):<16}"

        # 목표 좌표와 실패 사유는 상세 조회로만 나온다
        if state in ("failed", "succeeded"):
            try:
                d = requests.get(f"http://{args.ip}:{PORT}/chassis/moves/{mid}", timeout=6).json()
            except Exception:
                d = {}
            tx, ty = d.get("target_x"), d.get("target_y")
            if tx is not None:
                line += f" → ({tx:.3f}, {ty:.3f}) ori={d.get('target_ori')}"
            line += f"  {ts(d.get('create_time'))}"
            print(line)
            if state == "failed":
                print(f"        사유: {d.get('fail_reason')} {d.get('fail_reason_str')}")
                if d.get("fail_message"):
                    print(f"        {d['fail_message'][:110]}")
                # 등록 좌표와 얼마나 어긋났는지
                if tx is not None and pois:
                    near = min(pois, key=lambda p: (p[3] - tx) ** 2 + (p[4] - ty) ** 2)
                    dist = ((near[3] - tx) ** 2 + (near[4] - ty) ** 2) ** 0.5
                    verdict = "일치" if dist < 0.15 else f"★ {dist:.2f}m 어긋남 — 옛 좌표로 시도한 것"
                    print(f"        가장 가까운 등록 충전소 {near[2]}(map {near[1]}): {verdict}")
        else:
            print(line)

    if shown == 0:
        print("  (해당 이동 없음)")
    print()


if __name__ == "__main__":
    main()
