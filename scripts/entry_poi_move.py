# -*- coding: utf-8 -*-
"""진입점(`<이름>-1`)을 본 POI 쪽으로 N cm 옮긴다.

"C1-1 이 얼마나 떨어져 있어야 회전 없이 충전에 들어가는가" 를 찾을 때,
매번 맵 편집기를 열지 않고 거리만 바꿔가며 시험하기 위한 도구다.

무엇을 고치나
  map_pois 의 **world_x / world_y** (백엔드가 실제로 쓰는 값) 와
  **x / y** (픽셀 — 프론트 맵 표시용) 를 **같이** 고친다.
  둘 중 하나만 고치면 화면과 실제가 어긋난다.

왜 재시작이 필요 없나
  백엔드는 충전 명령을 받을 때마다 DB 에서 진입점을 새로 읽는다
  (`routers/robot.py` 의 `/remote/dock`, `jack_service` 의 charge-approach).
  C1-1 은 **로봇에 동기화하지 않는** 규약이라 맵 동기화도 필요 없다.

되돌리기
  바꿀 때마다 원래 값을 `_logs/entry_poi_history.json` 에 쌓는다.
  `--restore` 로 **맨 처음 값**으로 되돌린다.

쓰는 법
  python scripts/entry_poi_move.py --base C1 --by-cm 20 --dry-run   # 미리보기
  python scripts/entry_poi_move.py --base C1 --by-cm 20             # 적용
  python scripts/entry_poi_move.py --base C1 --set-dist 0.9         # 거리를 0.9m 로
  python scripts/entry_poi_move.py --base C1 --show                 # 현재 값만
  python scripts/entry_poi_move.py --base C1 --restore              # 최초값 복원
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime

try:
    import pymysql
except ModuleNotFoundError:
    # PowerShell 기본 python 에는 pymysql 이 없다. 백엔드 venv 에는 있다.
    # 현장은 인터넷이 없어 pip 로 푸는 건 피한다 — venv 를 쓰는 게 맞다.
    sys.stderr.write(
        "\n[중단] pymysql 이 없다.\n"
        "  이 스크립트는 DB 를 읽으므로 **백엔드 venv 의 python** 으로 실행해야 한다:\n\n"
        "    BackEnd\\venv\\Scripts\\python.exe scripts\\entry_poi_move.py ...\n\n"
        "  (지금 쓰는 python: %s)\n\n" % sys.executable)
    sys.exit(2)


def _force_utf8():
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8()

DB = dict(host="127.0.0.1", user="root", password="1234",
          database="rcs_lg_db", charset="utf8mb4")
HIST = os.path.join("_logs", "entry_poi_history.json")
# 진입점이 본 POI 에 이보다 가까워지지 못하게 한다 (지나침 방지)
MIN_DIST_M = 0.10

# ★ 충전 진입점(C1-1)의 표준 거리 (2026-09-14 실측 11회로 확정)
#
#   로봇은 충전 접점이 뒤에 있어 **후진 도킹**한다. 그래서 C1-1 은 충전기를 등지고
#   서는 자리이고, 거기서 그대로 뒤로 들어가는 것이 정상이다.
#
#   ※ docs/10 §1 의 "36cm 재시도 잦음 / 1.2m 한 번에 도킹" 기록은 **실측과 반대**였다.
#     2026-09-14 에 0.15 ~ 1.20 m 를 11회 돌려본 결과:
#       · 0.66 m 와 0.45 m 사이에 절벽이 있다 — 멀수록 충전기 앞에서 자세를 다시 잡는다
#       · 0.15 m 가 가장 깔끔했고(추가 회전 1.1°, 7초)
#       · 0.20 m 는 편차가 커(46°) 불안정
#       · **0.25 m** 가 안정성과 여유의 균형이 가장 좋았다
#   그래서 기본값을 0.25 로 고정한다.
DEFAULT_ENTRY_M = 0.25


def connect():
    return pymysql.connect(**DB)


def active_map_id(cu):
    """⚠ 최후의 수단이다. 활성 맵이 여러 개면 엉뚱한 맵을 고른다.

    가능하면 `map_for_robot()` 을 써라 — 2026-09-14 에 이걸로 사고가 났다.
    활성 맵이 10개(18·19·20·22·24·26·27·29·30·32)인데 여기서 32 를 골랐고,
    로봇이 실제로 쓰는 건 30 이었다. 그래서 진입점 거리를 세 번 바꿨는데
    **로봇 동작이 한 번도 안 바뀌었다.**
    """
    cu.execute("SELECT id FROM robot_maps WHERE is_active=1 ORDER BY id DESC LIMIT 1")
    r = cu.fetchone()
    return r[0] if r else None


def map_for_robot(cu, ip):
    """그 로봇이 **실제로 쓰는** 충전소 POI 와 맵을 찾는다.

    백엔드(`/remote/dock`)는 `robots.charging_id` 로 충전소 POI 를 잡고,
    그 POI 의 `map_id` 에서 `<이름>-1` 을 찾는다. 판단 기준을 똑같이 맞춘다.
    좌표로 맵을 추정하면 안 된다 — 맵끼리 C1 좌표가 3cm 밖에 차이 안 나는 경우가 있다.

    반환: (map_id, charger_name) / 못 찾으면 (None, None)
    """
    cu.execute("SELECT charging_id FROM robots WHERE ip_address=%s", (ip,))
    r = cu.fetchone()
    if not r or not r[0]:
        return None, None
    cu.execute("SELECT map_id, name FROM map_pois WHERE id=%s", (r[0],))
    p = cu.fetchone()
    return (p[0], p[1]) if p else (None, None)


def fit_pixel_transform(cu, map_id):
    """같은 맵의 POI 들로 world→pixel 변환을 역산한다.

    맵 테이블의 grid_origin 만으로는 이 DB 의 픽셀 좌표계가 재현되지 않아
    (원점이 다른 기준), **실제 POI 값에서 직접 맞춘다.**
      px =  wx/res + ax
      py = -wy/res + by
    """
    cu.execute("SELECT grid_resolution FROM robot_maps WHERE id=%s", (map_id,))
    row = cu.fetchone()
    res = float(row[0]) if row and row[0] else 0.05
    cu.execute("SELECT x,y,world_x,world_y FROM map_pois "
               "WHERE map_id=%s AND world_x IS NOT NULL AND x IS NOT NULL", (map_id,))
    rows = cu.fetchall()
    if not rows:
        return None
    axs, bys = [], []
    for x, y, wx, wy in rows:
        axs.append(float(x) - float(wx) / res)
        bys.append(float(y) + float(wy) / res)
    ax = sum(axs) / len(axs)
    by = sum(bys) / len(bys)
    # 잔차 확인 — 크면 좌표계 가정이 틀린 것이니 쓰지 않는다
    err = max(max(abs(a - ax) for a in axs), max(abs(b - by) for b in bys))
    return {"res": res, "ax": ax, "by": by, "err": err, "n": len(rows)}


def get_poi(cu, map_id, name):
    cu.execute("SELECT id,name,x,y,world_x,world_y,angle FROM map_pois "
               "WHERE map_id=%s AND name=%s AND is_active=1", (map_id, name))
    r = cu.fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "x": r[2], "y": r[3],
            "wx": float(r[4]) if r[4] is not None else None,
            "wy": float(r[5]) if r[5] is not None else None,
            "angle": r[6]}


def create_entry_poi(cu, cn, map_id, base, dist_m, dry_run=False):
    """진입점('<본이름>-1')을 **새로 만든다.**

    위치 = 본 POI 에서 **그 POI 의 angle 방향**으로 `dist_m` 만큼.

    왜 angle 방향인가 — 충전소는 후진 도킹이라, 로봇이 도킹을 끝냈을 때의 자세가
    곧 그 POI 의 angle 이다. 로봇 앞쪽(= angle 방향)이 곧 '들어온 쪽' 이므로
    진입점은 그 방향에 있어야 한다.

    2026-09-14 map33 실측으로 검증:
        C1 world=(0.8233, 5.3664)  angle=0.54 rad (30.9°)
        C1-1 이 있는 방향 +35.3°, 거리 0.2500 m   → 공식과 4.4° 차이로 일치

    poi_type 은 반드시 **waypoint** 다. charging/jack 으로 만들면 안전존
    판정 제외 반경(2 m)이 하나 더 생겨 통로 감시가 넓게 꺼진다.
    """
    if base.get("angle") is None:
        print("[실패] 본 POI '{}' 에 angle 이 없다 — 방향을 정할 수 없다".format(base["name"]))
        return None
    ang = float(base["angle"])
    wx = base["wx"] + dist_m * math.cos(ang)
    wy = base["wy"] + dist_m * math.sin(ang)
    tf = fit_pixel_transform(cu, map_id)
    if not tf or tf["err"] > 0.5:
        print("[실패] 픽셀 변환을 못 구했다 (잔차 {}) — 화면 좌표가 어긋나므로 "
              "만들지 않는다".format(tf["err"] if tf else "n/a"))
        return None
    px = wx / tf["res"] + tf["ax"]
    py = -wy / tf["res"] + tf["by"]
    name = base["name"] + "-1"
    print("  [생성] {} = world({:.4f}, {:.4f})  px({:.2f}, {:.2f})".format(
        name, wx, wy, px, py))
    print("         본 POI 에서 angle {:+.1f}° 방향으로 {:.3f} m".format(
        math.degrees(ang), dist_m))
    if dry_run:
        print("  [dry-run] DB 를 바꾸지 않았다")
        return {"id": None, "name": name, "x": px, "y": py,
                "wx": wx, "wy": wy, "angle": ang}
    cu.execute(
        "INSERT INTO map_pois (map_id, name, x, y, world_x, world_y, poi_type,"
        " angle, is_active) VALUES (%s,%s,%s,%s,%s,%s,'waypoint',%s,1)",
        (map_id, name, px, py, wx, wy, ang))
    cn.commit()
    new_id = cu.lastrowid
    print("  [완료] id={} 로 만들었다 (poi_type=waypoint)".format(new_id))
    return {"id": new_id, "name": name, "x": px, "y": py,
            "wx": wx, "wy": wy, "angle": ang}


def load_hist():
    try:
        with open(HIST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_hist(h):
    os.makedirs(os.path.dirname(HIST), exist_ok=True)
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="진입점을 본 POI 쪽으로 옮긴다")
    ap.add_argument("--base", default="C1", help="본 POI 이름 (진입점은 '<이름>-1')")
    ap.add_argument("--ip", default=None,
                    help="★로봇 IP. 주면 그 로봇이 실제로 쓰는 맵(robots.charging_id 기준)"
                         "을 고른다. 안 주면 활성 맵 중 하나를 찍는데, 활성 맵이 여러 개면"
                         " 엉뚱한 맵을 고칠 수 있다")
    ap.add_argument("--map-id", type=int, default=None, help="맵을 직접 지정")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--by-cm", type=float, help="본 POI 쪽으로 이만큼 당긴다(cm). 음수면 멀어진다")
    g.add_argument("--set-dist", type=float, help="본 POI 와의 거리를 이 값(m)으로 맞춘다")
    g.add_argument("--ensure", type=float, nargs="?", const=DEFAULT_ENTRY_M,
                   metavar="M",
                   help="진입점을 **표준 거리로 고정**한다. 없으면 만들고, 있으면 그 거리로"
                        " 맞춘다. 값을 안 주면 {} m (실측 확정값)".format(DEFAULT_ENTRY_M))
    g.add_argument("--show", action="store_true", help="현재 값만 보여준다")
    g.add_argument("--restore", action="store_true", help="최초값으로 되돌린다")
    ap.add_argument("--dry-run", action="store_true", help="DB 를 바꾸지 않고 결과만 보여준다")
    args = ap.parse_args()

    cn = connect()
    cu = cn.cursor()
    base_name = args.base
    if args.map_id:
        map_id = args.map_id
    elif args.ip:
        map_id, cname = map_for_robot(cu, args.ip)
        if not map_id:
            print("[실패] {} 의 charging_id 가 비어 있다 — 로봇 관리에서"
                  " 충전소를 지정하라".format(args.ip))
            return 1
        if cname and cname != base_name:
            print("  (로봇이 쓰는 충전소 이름은 '{}' 다 — 그걸로 진행)".format(cname))
            base_name = cname
        print("  [맵] {} 이 쓰는 맵 = {} (robots.charging_id 기준)".format(args.ip, map_id))
    else:
        map_id = active_map_id(cu)
        print("  [경고] --ip 를 안 줬다. 활성 맵 중 하나(map {})를 찍었다 —"
              " 로봇이 쓰는 맵과 다를 수 있다".format(map_id))
    if not map_id:
        print("[실패] 맵을 찾을 수 없다")
        return 1
    args.base = base_name

    base = get_poi(cu, map_id, args.base)
    entry = get_poi(cu, map_id, args.base + "-1")
    if not base or base["wx"] is None:
        print("[실패] 본 POI '{}' 를 찾을 수 없다 (map_id={})".format(args.base, map_id))
        return 1
    if not entry or entry["wx"] is None:
        if args.ensure is not None:
            # 없으면 만든다 — 맵을 새로 만들 때마다 손으로 찍지 않아도 된다
            print("진입점 '{}-1' 이 없다 → {} m 로 새로 만든다".format(
                args.base, args.ensure))
            entry = create_entry_poi(cu, cn, map_id, base, args.ensure, args.dry_run)
            cn.close()
            return 0 if entry else 1
        print("[실패] 진입점 '{}-1' 을 찾을 수 없다 (map_id={})".format(args.base, map_id))
        print("       --ensure 를 주면 {} m 거리에 자동으로 만든다.".format(DEFAULT_ENTRY_M))
        return 1

    # 있으면 --ensure 는 "그 거리로 맞춰라" 와 같다 (방향은 지금 것을 유지)
    if args.ensure is not None:
        args.set_dist = args.ensure

    def dist(e):
        return math.hypot(base["wx"] - e[0], base["wy"] - e[1])

    cur = (entry["wx"], entry["wy"])
    print("맵 {}  ·  {} = ({:.4f}, {:.4f})".format(map_id, base["name"], base["wx"], base["wy"]))
    print("현재 {} = ({:.4f}, {:.4f})   거리 {:.3f} m   angle={}".format(
        entry["name"], cur[0], cur[1], dist(cur), entry["angle"]))

    hist = load_hist()
    key = "{}:{}".format(map_id, entry["name"])
    if key not in hist:
        hist[key] = {"original": {"wx": cur[0], "wy": cur[1],
                                  "x": float(entry["x"]), "y": float(entry["y"])},
                     "changes": []}
        save_hist(hist)
        print("  (최초값을 {} 에 기록했다)".format(HIST))

    if args.show:
        o = hist[key]["original"]
        print("최초값 = ({:.4f}, {:.4f})   거리 {:.3f} m".format(
            o["wx"], o["wy"], dist((o["wx"], o["wy"]))))
        cn.close()
        return 0

    # ── 새 좌표 계산 ──
    if args.restore:
        o = hist[key]["original"]
        new = (o["wx"], o["wy"])
        new_px, new_py = o["x"], o["y"]
        how = "최초값 복원"
    else:
        d = dist(cur)
        if d < 1e-6:
            print("[실패] 진입점이 본 POI 와 겹쳐 있어 방향을 정할 수 없다")
            cn.close()
            return 1
        ux = (base["wx"] - cur[0]) / d      # 진입점 → 본 POI 단위벡터
        uy = (base["wy"] - cur[1]) / d
        if args.set_dist is not None:
            move = d - args.set_dist        # 양수면 가까워지는 방향
            how = "거리를 {:.3f} m 로".format(args.set_dist)
        elif args.by_cm is not None:
            move = args.by_cm / 100.0
            how = "{:+.1f} cm 이동".format(args.by_cm)
        else:
            print("[실패] --by-cm / --set-dist / --show / --restore 중 하나가 필요하다")
            cn.close()
            return 1
        # ★ 본 POI 를 지나쳐 반대편으로 넘어가는 것을 막는다.
        #   2026-09-14 실측에서 0.06m 남은 상태로 20cm 를 더 당겨 **C1 반대편 14cm**
        #   로 넘어갔다. 그 상태는 진입점 구실을 못 한다.
        if move > d - MIN_DIST_M:
            print("[중단] 그만큼 당기면 본 POI 를 지나친다"
                  " (현재 {:.3f} m, 요청 {:+.3f} m, 최소 {:.2f} m).".format(
                      d, move, MIN_DIST_M))
            print("       ※ docs/10 §1 의 '진입점은 멀어야 한다' 기록은 **틀렸다**."
                  " 2026-09-14 실측 11회 결과는 정반대로, 가까울수록 깔끔했다"
                  " (표준값 {} m).".format(DEFAULT_ENTRY_M))
            cn.close()
            return 1
        new = (cur[0] + ux * move, cur[1] + uy * move)
        tf = fit_pixel_transform(cu, map_id)
        if not tf or tf["err"] > 0.5:
            print("[실패] 픽셀 변환을 못 구했다 (잔차 {}). world 만 고치면 화면이"
                  " 어긋나므로 중단한다".format(tf["err"] if tf else "n/a"))
            cn.close()
            return 1
        new_px = new[0] / tf["res"] + tf["ax"]
        new_py = -new[1] / tf["res"] + tf["by"]
        print("  픽셀 변환 res={} ax={:.3f} by={:.3f} (POI {}개, 잔차 {:.4f})".format(
            tf["res"], tf["ax"], tf["by"], tf["n"], tf["err"]))

    nd = dist(new)
    print("-" * 58)
    print("{}".format(how))
    print("  world  ({:.4f}, {:.4f}) -> ({:.4f}, {:.4f})".format(
        cur[0], cur[1], new[0], new[1]))
    print("  pixel  ({:.4f}, {:.4f}) -> ({:.4f}, {:.4f})".format(
        float(entry["x"]), float(entry["y"]), new_px, new_py))
    print("  거리   {:.3f} m -> **{:.3f} m**".format(dist(cur), nd))
    # ※ 예전에는 "0.3 m 보다 가까우면 진입점 의미가 없다" 고 경고했는데,
    #   2026-09-14 실측 11회에서 정반대였다 — 가까울수록 충전기 앞 추가 회전이 없었다.
    #   지금은 표준값(DEFAULT_ENTRY_M)에서 많이 벗어날 때만 알린다.
    if nd < MIN_DIST_M + 0.02:
        print("  ⚠ {:.2f} m — 너무 가깝다. 본 POI 를 지나칠 위험이 있다".format(nd))
    elif nd > 0.45:
        print("  ⚠ {:.2f} m — 0.45 m 를 넘으면 충전기 앞에서 자세를 다시 잡는다"
              " (실측: 0.66 m 와 0.45 m 사이에 절벽). 표준값은 {:.2f} m".format(
                  nd, DEFAULT_ENTRY_M))

    if args.dry_run:
        print("\n[dry-run] DB 를 바꾸지 않았다.")
        cn.close()
        return 0

    cu.execute("UPDATE map_pois SET world_x=%s, world_y=%s, x=%s, y=%s WHERE id=%s",
               (new[0], new[1], new_px, new_py, entry["id"]))
    cn.commit()
    hist[key]["changes"].append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "how": how, "from": list(cur), "to": list(new), "dist_m": round(nd, 4)})
    save_hist(hist)
    cn.close()
    print("\n[적용] DB 반영 완료. 백엔드 재시작 불필요 — 다음 충전 명령부터 이 값을 쓴다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
