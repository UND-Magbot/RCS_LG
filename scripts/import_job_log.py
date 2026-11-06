"""과거 서버 로그(backend.log*)에서 배송 작업 기록을 재구성해 dispatch_job_log 에 넣는다 (2026-11-06).

작업 1건 = R 에서 랙 적재 → 짝 J 하차 (배송 모드). 인터랙티브 모드 세션은 대상이 아니다.

실행 (백엔드 venv)
  BackEnd\\venv\\Scripts\\python.exe scripts\\import_job_log.py --dry-run    # 집계만 출력, DB 손대지 않음
  BackEnd\\venv\\Scripts\\python.exe scripts\\import_job_log.py              # 백업(_backup/) 후 넣기
  옵션: --logs <폴더>  --db <DB이름>  --no-backup  --replace(이전 가져오기분 지우고 다시)

다시 돌려도 안전하다
  · 작업마다 dedup_key("log:<robot>:<출발시각>:<J>") — 이미 있으면 건너뛴다
  · live 기록(서버가 직접 남긴 것)과 같은 작업(같은 로봇·출발 ±5초)도 건너뛴다

로그에서 쓰는 단서 (dispatch_service.py 의 실제 문구)
  [dispatch] 배송 모드 — robot=N R → J          새 세션 시작
  [delivery] robot=N R → J                       작업 출발(세션 시작·이어받기 공통)
  [delivery] J 하차 완료 — 점유 표시             하차 = 작업 완료 (로봇 id 없음 → 그 J 로 가던 작업에 붙인다)
  [dispatch] 이어받기 — robot=N … → poi=P        이어받기. 하차 직후(그 사이 '이어받기 없음/보류' 없음)면 연속
  [dispatch] 이어받기 없음/보류 — robot=N        하차 직후 이어받지 못함 → 그 뒤 이어받기는 복귀 중(연속 아님)
  [delivery] R 에 랙이 없습니다 — J 작업을 …     실패(랙 없음)
  [delivery] 워커 예외 — robot_id=N              + 다음 줄 traceback 마지막 줄로 중지/실패 구분
  강제 종료 / 강제 정리 … robot=N                 ±60초 안이면 force_stopped
  Started server process                          서버 재시작 — 진행 중 작업은 실패로 닫는다
  예약 생성 — poi=P / [reserve] created|fulfilled|cancelled|waiting   호출 시각(call_at)

로그 시각은 "MM-DD HH:MM:SS"(연도 없음) → 마지막 파일의 수정일 연도에서 거꾸로 세며
월이 거꾸로 커지면(1월 → 12월) 한 해를 뺀다.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "BackEnd"
MARIADB_BIN = Path(r"C:\Program Files\MariaDB 12.3\bin")

LINE = re.compile(r"^(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\s+([A-Z]+)\s+(\S+)\s+(.*)$")
RE_SESSION = re.compile(r"\[dispatch\] 배송 모드 — robot=(\d+) (\S+) → (\S+)")
RE_START = re.compile(r"\[delivery\] robot=(\d+) (\S+) → (\S+)\s*$")
RE_UNLOAD = re.compile(r"\[delivery\] (\S+) 하차 완료")
RE_TAKE = re.compile(r"\[dispatch\] 이어받기 — robot=(\d+) .*poi=(\d+)")
RE_TAKE_NO = re.compile(r"\[dispatch\] 이어받기 (?:없음|보류) — robot=(\d+)")
RE_RACK = re.compile(r"\[delivery\] (\S+) 에 랙이 없습니다 — (\S+) 작업을")
RE_EXC = re.compile(r"\[delivery\] 워커 예외 — robot_id=(\d+)")
RE_FORCE = re.compile(r"(?:강제 종료|강제 정리|force_clear).*?robot=(\d+)")
RE_RES_NEW = re.compile(r"\[dispatch\] 예약 생성 — poi=(\d+)")
RE_RES_CREATED = re.compile(r"\[reserve\] created id=(\d+) poi=(\d+)")
RE_RES_FULFILLED = re.compile(r"\[reserve\] fulfilled id=(\d+) poi=(\d+).*?사유=(.*)$")
RE_RES_WAITING = re.compile(r"\[reserve\] waiting id=(\d+) poi=(\d+)")
RE_RES_CANCEL = re.compile(r"\[reserve\] cancelled poi=(\d+)")
RE_AUTO_CALL = re.compile(r"\[dispatch\] 예약 자동 호출 — poi=(\d+) robot=(\d+)")
RE_ROBOT_IN = re.compile(r"robot=(\d+)")

FORCE_WINDOW = timedelta(seconds=60)


# ── 로그 읽기 ────────────────────────────────────────────────────


def log_files(folder: Path) -> list[Path]:
    """backend.log.N … backend.log.1, backend.log 순(오래된 것부터)."""
    files = list(folder.glob("backend.log*"))

    def order(p: Path):
        suf = p.name[len("backend.log"):].lstrip(".")
        return -(int(suf) if suf.isdigit() else 0)
    return sorted(files, key=order)


def read_lines(files: list[Path]) -> list[tuple[datetime | None, str]]:
    """(시각, 원문) 목록. traceback 처럼 시각이 없는 줄은 시각 None."""
    raw: list[tuple[tuple[int, int, int, int, int] | None, str]] = []
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.rstrip("\n")
                m = LINE.match(ln)
                raw.append(((tuple(int(x) for x in m.groups()[:5]) if m else None), ln))
    if not raw:
        return []
    # 연도 추정 — 뒤에서부터. 마지막 파일 수정일의 연도가 마지막 줄의 연도다.
    year = datetime.fromtimestamp(files[-1].stat().st_mtime).year
    out: list[tuple[datetime | None, str]] = [(None, "")] * len(raw)
    later_month = None
    for i in range(len(raw) - 1, -1, -1):
        t, ln = raw[i]
        if t is None:
            out[i] = (None, ln)
            continue
        mo, dd, hh, mi, ss = t
        if later_month is not None and mo > later_month + 6:   # 1월 줄 앞에 12월 줄 = 해가 바뀜
            year -= 1
        later_month = mo
        try:
            out[i] = (datetime(year, mo, dd, hh, mi, ss), ln)
        except ValueError:          # 2/29 등
            out[i] = (None, ln)
    return out


# ── DB 보조 정보 ─────────────────────────────────────────────────


def db_connect(name: str):
    import pymysql
    return pymysql.connect(host=os.getenv("DB_HOST", "127.0.0.1"), user=os.getenv("DB_USER", "root"),
                           password=os.getenv("DB_PASSWORD", "1234"), database=name, charset="utf8mb4")


def load_db_info(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT p.id, p.name, m.area_id FROM map_pois p LEFT JOIN robot_maps m ON p.map_id = m.id")
    pois = {int(i): (n, (int(a) if a is not None else None)) for i, n, a in cur.fetchall()}
    cur.execute("SELECT id, name FROM robots")
    robots = {int(i): n for i, n in cur.fetchall()}
    cur.execute("SELECT id, robot_id, started_at, first_poi_id FROM dispatch_sessions")
    sessions = defaultdict(list)
    for sid, rid, st, fp in cur.fetchall():
        sessions[int(rid)].append((st, int(sid), (int(fp) if fp is not None else None)))
    return {"pois": pois, "robots": robots, "sessions": sessions}


def find_session(info: dict, robot_id: int, t: datetime):
    """세션 시작 로그 시각 ±3초 안의 dispatch_sessions 행 → (session_id, area_id)."""
    best = None
    for st, sid, fp in info["sessions"].get(robot_id, []):
        if st is None:
            continue
        d = abs((st - t).total_seconds())
        if d <= 3 and (best is None or d < best[0]):
            area = info["pois"].get(fp, (None, None))[1] if fp else None
            best = (d, sid, area)
    return (best[1], best[2]) if best else (None, None)


# ── 재구성 ──────────────────────────────────────────────────────


class Job:
    __slots__ = ("robot_id", "r_name", "j_name", "call_at", "started_at", "unloaded_at", "ended_at",
                 "result", "note", "continued", "prev", "session_id", "area_id", "take_poi", "idx")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def rebuild(lines, info: dict) -> tuple[list[Job], Counter]:
    stats = Counter()
    jobs: list[Job] = []
    open_job: dict[int, Job] = {}          # robot → 진행 중 작업
    phase: dict[int, str] = {}             # robot → after_unload / other
    pending_take: dict[int, tuple] = {}    # robot → (kind, poi, t) 다음 출발에 쓸 이어받기 정보
    new_session: dict[int, tuple] = {}     # robot → (t, session_id, area)
    last_done: dict[int, Job] = {}
    last_area: dict[int, int] = {}
    res_fifo: dict[int, deque] = defaultdict(deque)   # poi → 예약 생성 시각들
    res_by_id: dict[int, datetime] = {}
    pending_call: dict[int, datetime] = {}            # robot → 다음 작업의 호출 시각
    force_at: dict[int, datetime] = {}                # robot → 마지막 강제 종료 로그 시각
    exc: list = []                                    # [robot, job, t, traceback 줄들]
    closed_cancel: list[tuple[Job, datetime]] = []    # 중지로 닫은 작업 — 뒤따르는 강제 종료 로그면 승격

    def close(rid: int, t: datetime, result: str, note: str):
        j = open_job.pop(rid, None)
        if j is None:
            return None
        j.result, j.ended_at, j.note = result, t, note
        return j

    def finish_exc():
        if not exc:
            return
        rid, j, t, tb = exc
        exc.clear()
        if j is None or j.result is not None:
            return
        last = next((s.strip() for s in reversed(tb) if s.strip()), "")
        f = force_at.get(rid)
        if f is not None and abs((t - f)) <= FORCE_WINDOW:
            res = "force_stopped"
        elif last.startswith("RuntimeError"):
            res = "cancelled"
            closed_cancel.append((j, t))
        else:
            res = "failed"
        open_job.pop(rid, None)
        j.result, j.ended_at, j.note = res, t, (last[:200] or "워커 예외")

    def pop_call(poi: int, t: datetime):
        q = res_fifo.get(poi)
        while q and q[0] > t:     # 미래 것은 아님(시각 꼬임 대비)
            break
        return q.popleft() if q else None

    for t, ln in lines:
        if t is None:
            if exc:
                exc[3].append(ln)
            continue
        finish_exc()
        msg = ln

        if "Started server process" in msg:
            for rid in list(open_job):
                close(rid, t, "failed", "서버 재시작으로 중단")
                stats["close_restart"] += 1
            phase.clear(); pending_take.clear(); new_session.clear()
            continue

        m = RE_FORCE.search(msg)
        if m and ("app.services.dispatch_service" in msg):
            rid = int(m.group(1))
            force_at[rid] = t
            # 직전 '중지(cancelled)' 로 닫은 같은 로봇 작업이 60초 안이면 강제 종료로 고친다
            for j, ct in closed_cancel[-5:]:
                if j.robot_id == rid and j.result == "cancelled" and abs(t - ct) <= FORCE_WINDOW:
                    j.result = "force_stopped"
            continue

        # 예약(호출 시각)
        m = RE_RES_NEW.search(msg)
        if m:
            res_fifo[int(m.group(1))].append(t)
            continue
        m = RE_RES_CREATED.search(msg)
        if m:
            res_by_id[int(m.group(1))] = t
            continue
        m = RE_RES_FULFILLED.search(msg)
        if m:
            rid_ = int(m.group(1)); poi = int(m.group(2)); why = m.group(3)
            ct = res_by_id.get(rid_)
            q = res_fifo.get(poi)
            if q:
                v = q.popleft()
                ct = ct or v
            mr = RE_ROBOT_IN.search(why)
            if mr and ct:
                pending_call[int(mr.group(1))] = ct
            continue
        m = RE_RES_WAITING.search(msg)
        if m:   # 선점 롤백 — 호출이 대기열로 돌아온다
            ct = res_by_id.get(int(m.group(1)))
            if ct:
                res_fifo[int(m.group(2))].appendleft(ct)
            continue
        m = RE_RES_CANCEL.search(msg)
        if m:
            res_fifo.pop(int(m.group(1)), None)
            continue
        m = RE_AUTO_CALL.search(msg)
        if m:
            poi, rid = int(m.group(1)), int(m.group(2))
            if rid not in pending_call:
                ct = pop_call(poi, t)
                if ct:
                    pending_call[rid] = ct
            continue

        m = RE_SESSION.search(msg)
        if m:
            rid = int(m.group(1))
            if rid in open_job:
                close(rid, t, "failed", "종료 기록 없음(중단)")
                stats["close_nolog"] += 1
            sid, area = find_session(info, rid, t)
            new_session[rid] = (t, sid, area)
            phase[rid] = "other"
            pending_take.pop(rid, None)
            continue

        m = RE_TAKE.search(msg)
        if m:
            rid, poi = int(m.group(1)), int(m.group(2))
            kind = "continued" if phase.get(rid) == "after_unload" else "other"
            pending_take[rid] = (kind, poi, t)
            if rid not in pending_call:
                ct = pop_call(poi, t)
                if ct:
                    pending_call[rid] = ct
            phase[rid] = "other"
            continue

        m = RE_TAKE_NO.search(msg)
        if m:
            phase[int(m.group(1))] = "other"     # 하차 직후 못 이어받음 → 이후 이어받기는 복귀 중
            continue

        m = RE_START.search(msg)
        if m:
            rid, r, jn = int(m.group(1)), m.group(2), m.group(3)
            if rid in open_job:
                close(rid, t, "failed", "종료 기록 없음(중단)")
                stats["close_nolog"] += 1
            ns = new_session.pop(rid, None)
            tk = pending_take.pop(rid, None)
            area = None
            sid = None
            continued = False
            take_poi = None
            if ns is not None:
                sid, area = ns[1], ns[2]
            elif tk is not None:
                continued = (tk[0] == "continued")
                take_poi = tk[1]
                pinfo = info["pois"].get(take_poi)
                if pinfo:
                    area = pinfo[1]
                    if pinfo[0] != jn:
                        stats["take_poi_name_mismatch"] += 1
                else:
                    stats["take_poi_unknown"] += 1
                prev_job = last_done.get(rid)
                sid = prev_job.session_id if prev_job else None
                stats["take_" + tk[0]] += 1
            else:
                stats["start_without_marker"] += 1
            if area is None:
                area = last_area.get(rid)
            if area is not None:
                last_area[rid] = area
            j = Job(robot_id=rid, r_name=r, j_name=jn, started_at=t,
                    call_at=pending_call.pop(rid, None) or t,
                    continued=continued, prev=(last_done.get(rid) if continued else None),
                    session_id=sid, area_id=area, take_poi=take_poi, idx=len(jobs))
            jobs.append(j)
            open_job[rid] = j
            phase[rid] = "other"
            continue

        m = RE_UNLOAD.search(msg)
        if m:
            jn = m.group(1)
            cands = [j for j in open_job.values() if j.j_name == jn]
            if not cands:
                stats["unload_unmatched"] += 1
                continue
            j = min(cands, key=lambda x: x.started_at)
            j.unloaded_at = j.ended_at = t
            j.result = "completed"
            open_job.pop(j.robot_id, None)
            last_done[j.robot_id] = j
            phase[j.robot_id] = "after_unload"
            stats["unload"] += 1
            continue

        m = RE_RACK.search(msg)
        if m:
            r, jn = m.group(1), m.group(2)
            cands = [j for j in open_job.values() if j.r_name == r and j.j_name == jn]
            if cands:
                j = min(cands, key=lambda x: x.started_at)
                close(j.robot_id, t, "failed", "랙 없음(정렬 실패)")
                phase[j.robot_id] = "other"
            continue

        m = RE_EXC.search(msg)
        if m:
            rid = int(m.group(1))
            exc[:] = [rid, open_job.get(rid), t, []]
            continue

    finish_exc()
    for rid in list(open_job):
        j = open_job.pop(rid)
        j.result, j.note = "failed", "종료 기록 없음(로그 끝)"
        stats["open_at_end"] += 1
    return jobs, stats


# ── 넣기 ────────────────────────────────────────────────────────


def backup(db: str) -> Path:
    out_dir = ROOT / "_backup"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{db}_before_joblog_import_{datetime.now():%Y%m%d_%H%M%S}.sql"
    cmd = [str(MARIADB_BIN / "mysqldump.exe"), "-uroot", f"-p{os.getenv('DB_PASSWORD', '1234')}",
           "--single-transaction", "--routines", "--default-character-set=utf8mb4", db]
    with open(out, "wb") as fh:
        subprocess.run(cmd, stdout=fh, check=True)
    return out


def ensure_table(db: str) -> None:
    os.environ["DB_NAME"] = db
    os.environ.setdefault("DB_HOST", "127.0.0.1")
    sys.path.insert(0, str(BACKEND))
    from app.database import engine            # noqa: E402
    from app.models.dispatch import DispatchJobLog   # noqa: E402
    DispatchJobLog.__table__.create(bind=engine, checkfirst=True)


def insert(conn, jobs: list[Job], info: dict, replace: bool) -> tuple[int, int, int]:
    cur = conn.cursor()
    if replace:
        cur.execute("DELETE FROM dispatch_job_log WHERE source='log_import'")
    cur.execute("SELECT robot_id, started_at FROM dispatch_job_log WHERE source='live'")
    live = defaultdict(list)
    for rid, st in cur.fetchall():
        live[rid].append(st)
    ids: dict[int, int] = {}         # Job.idx → DB id (prev_job_id 연결용)
    added = skipped_dup = skipped_live = 0
    for j in jobs:
        key = f"log:{j.robot_id}:{j.started_at:%Y%m%d%H%M%S}:{j.j_name}"
        if any(abs((st - j.started_at).total_seconds()) <= 5 for st in live.get(j.robot_id, [])):
            skipped_live += 1
            continue
        cur.execute("SELECT id FROM dispatch_job_log WHERE dedup_key=%s", (key,))
        row = cur.fetchone()
        if row:
            ids[j.idx] = row[0]
            skipped_dup += 1
            continue
        prev_id = ids.get(j.prev.idx) if j.prev is not None else None
        cur.execute(
            "INSERT INTO dispatch_job_log (area_id, robot_id, robot_name, r_name, j_name, route_key,"
            " call_at, started_at, unloaded_at, ended_at, result, note, continued, prev_job_id,"
            " source, session_id, dedup_key) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (j.area_id, j.robot_id, info["robots"].get(j.robot_id) or f"robot {j.robot_id}",
             j.r_name, j.j_name, f"{j.r_name}→{j.j_name}", j.call_at, j.started_at,
             j.unloaded_at, j.ended_at, j.result, j.note, 1 if j.continued else 0, prev_id,
             "log_import", j.session_id, key))
        ids[j.idx] = cur.lastrowid
        added += 1
    conn.commit()
    return added, skipped_dup, skipped_live


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=str(BACKEND / "_logs"))
    ap.add_argument("--db", default=os.getenv("DB_NAME", "rcs_lg_db"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--replace", action="store_true", help="이전 log_import 분을 지우고 다시 넣는다")
    a = ap.parse_args()

    files = log_files(Path(a.logs))
    if not files:
        print(f"로그 없음: {a.logs}")
        return 1
    print("로그:", ", ".join(f.name for f in files))
    lines = read_lines(files)
    conn = db_connect(a.db)
    info = load_db_info(conn)
    jobs, st = rebuild(lines, info)

    # ── 집계 출력 ──
    first = next((t for t, _ in lines if t), None)
    last = next((t for t, _ in reversed(lines) if t), None)
    print(f"기간: {first} ~ {last}  (줄 {len(lines):,})")
    print(f"작업(출발) {len(jobs)}건 · 하차 {st['unload']} · 하차 직후 이어받기(연속) {st['take_continued']}"
          f" · 복귀 중/기타 이어받기 {st['take_other']}")
    print("결과:", dict(Counter(j.result for j in jobs)))
    cont = Counter(f"{j.prev.j_name if j.prev else '?'}→{j.j_name}" for j in jobs if j.continued)
    print("연속 (직전 J → 이번 J):", dict(cont))
    print("연속 이어받기 poi 이름 확인:",
          dict(Counter((info['pois'].get(j.take_poi, ('알 수 없음', None))[0]) for j in jobs if j.continued)))
    by_route = defaultdict(lambda: [0, 0])
    for j in jobs:
        if j.result == "completed":
            by_route[f"{j.r_name}→{j.j_name}"][0] += 1
            by_route[f"{j.r_name}→{j.j_name}"][1] += 1 if j.continued else 0
    print("경로별 완료/연속:", {k: tuple(v) for k, v in sorted(by_route.items())})
    print("구역별:", dict(Counter(j.area_id for j in jobs)))
    print("기타:", {k: v for k, v in st.items() if k not in ("unload", "take_continued", "take_other")})

    if a.dry_run:
        print("--dry-run — DB 에 넣지 않았습니다")
        return 0
    if not a.no_backup:
        print("백업:", backup(a.db))
    ensure_table(a.db)
    added, dup, lv = insert(conn, jobs, info, a.replace)
    print(f"넣음 {added} · 이미 있음 {dup} · live 기록과 겹쳐 건너뜀 {lv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
