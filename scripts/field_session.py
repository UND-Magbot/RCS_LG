#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""현장 로그 세션 — **시작 한 번, 종료 한 번**으로 필요한 로그를 전부 남기고 뽑는다.

    python scripts/field_session.py start          기록 시작
    python scripts/field_session.py mark "메모"     비이상적 정지 표시 (웹·폰 버튼과 같다)
    python scripts/field_session.py stop           기록 종료 + 추출(zip)
    python scripts/field_session.py bags           마지막 세션의 로봇 bag 만 다시 받고 zip 갱신
    python scripts/field_session.py status
    python scripts/field_session.py open           로그추출 폴더 열기

    현장작업\\L1_기록시작.bat · L2_종료및추출.bat · L3_bag다시받기.bat 가 이걸 부른다.
    현장콘솔(field_console) 의 [기록 시작]/[표시]/[종료·추출] 버튼도 같은 함수를 쓴다.

왜 만들었나 (2026-09-29)
  로그 도구가 bat 4개(주행기록·로그모으기·bag받기·망측정)로 흩어져 있었다.
    · 비이상적 정지 표시가 두 파일로 갈렸다(스페이스바 / 웹 버튼)
    · 로그 모으기가 _logs 전체를 묶어 이번 구간을 찾기 어려웠다
    · bag 은 시각을 손으로 넣어야 했고, 파일명이 로봇 시간대라
      2026-09-28 현장에서 18시 이후 bag 을 "없다" 고 보고 못 받았다
    · 망은 2분짜리 1회 측정뿐이라 주행 중 상태가 안 남았다

세션 하나 = 폴더 하나 = zip 하나
  _logs/세션/<시작시각>/
    session.json         시작·종료 시각, 로봇, 서버, git
    marks.jsonl          비이상적 정지 표시 (스페이스바 + 웹·폰 버튼 한 파일)
    시계.jsonl           로봇-서버 시계 차이 (시작·종료)
    drive_log/           주행 기록 (속도·센서·코스트맵·스냅샷·요약)
    drive_log_화면.txt   기록기 창에 찍힌 내용
    망/                  ping 1초 · HTTP 5초 상시 기록, tracert·ipconfig
    서버/                backend.log 세션 구간 · 경고/오류 · 안전존 · 알람/활동 로그(DB) · 설정 사본
    로봇/                시작·종료 상태 · bag 목록 · bag/ (세션 앞뒤 5분)
    인덱스.txt           ★ 사람이 먼저 보는 파일. 무엇이 있고 무엇이 빠졌나
  로그추출/로그추출_<날짜>_<시작>-<끝>.zip

★ 로봇에 명령을 보내지 않는다. GET 조회와 파일 다운로드, ping 뿐이다.
★ 표준 라이브러리만 쓴다 (현장은 인터넷이 없어 pip 설치를 못 한다).
★ 단계마다 따로 실패한다. 한 단계가 안 돼도 나머지는 zip 에 들어가고,
  빠진 것은 인덱스.txt 에 이유와 함께 적는다.
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

SROOT = os.path.join(ROOT, "_logs", "세션")
CUR = os.path.join(SROOT, "_진행중.json")      # 지금 기록 중인 세션
LAST = os.path.join(SROOT, "_마지막.json")     # 마지막으로 끝낸 세션 (bag 다시 받기용)
ZIPDIR = os.path.join(ROOT, "로그추출")

PY = os.path.join(ROOT, "BackEnd", "venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

BAG_MARGIN = 300          # bag 은 세션 앞뒤 5분까지 받는다
BAG_WAIT_MAX = 12 * 60    # 마지막 bag 녹화가 끝나길 기다리는 최대 시간
NET_MAX_SEC = 12 * 3600   # 망 기록기가 종료 신호를 못 받아도 12시간 뒤엔 스스로 끝낸다

CFG = {
    "ROBOT_IP": "10.14.182.126",
    "ROBOT_ROUTER": "10.115.244.12",
    "SERVER_ROUTER": "10.115.244.11",
    "BACKEND": "http://127.0.0.1:8002",
}

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── 공통 ────────────────────────────────────────────────────────
def load_config() -> None:
    """현장작업/config.bat 의 set 줄을 읽는다 (콘솔·bat 과 같은 값을 쓰게)."""
    p = os.path.join(ROOT, "현장작업", "config.bat")
    try:
        raw = open(p, "rb").read()
    except Exception:
        return
    for enc in ("utf-8", "cp949"):
        try:
            txt = raw.decode(enc)
            break
        except Exception:
            continue
    else:
        txt = raw.decode("cp949", "replace")
    for m in re.finditer(r"^\s*set\s+([A-Z_]+)=(.+?)\s*$", txt, re.M):
        k, v = m.group(1), m.group(2).strip()
        if k in CFG and v:
            CFG[k] = v


def hm(t: float, fmt: str = "%H:%M:%S") -> str:
    return time.strftime(fmt, time.localtime(t))


def dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, m = sec // 3600, sec % 3600 // 60
    return ("%d시간 %d분" % (h, m)) if h else ("%d분 %d초" % (m, sec % 60))


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def jload(path: str, default=None):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jlines(path: str) -> list[dict]:
    out = []
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def get_json(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def dec(b: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("cp949", "replace")


def run_text(args: list[str], timeout: float = 60) -> str:
    try:
        r = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=timeout,
                           creationflags=_NOWIN)
        return dec(r.stdout) + (dec(r.stderr) if r.stderr else "")
    except Exception as e:
        return "실행 실패: %s" % e


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))      # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259                          # STILL_ACTIVE
    except Exception:
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False


# ── 시작·종료 스냅샷 ────────────────────────────────────────────
ROBOT_GETS = [
    ("장치정보", "/device/info"),
    ("파라미터", "/robot-params"),
    ("섀시상태", "/chassis/status"),
    ("현재맵", "/chassis/current-map"),
    ("현재이동", "/chassis/moves/current"),
]
SERVER_GETS = [
    ("health", "/health"),
    ("맵영역", "/api/map/default-area"),
    ("안전설정", "/api/settings/safety"),
    ("안전존상태", "/api/settings/safety/status"),
]


def snapshot(tag: str, d: str, log) -> dict:
    """로봇·서버 상태와 시계 차이를 남긴다. 돌려주는 값은 인덱스에 쓴다."""
    ip, be = CFG["ROBOT_IP"], CFG["BACKEND"].rstrip("/")
    res = {"robot_ok": 0, "server_ok": 0, "clock": None}
    for name, path in ROBOT_GETS:
        try:
            jdump(os.path.join(d, "로봇", "%s_%s.json" % (tag, name)),
                  get_json("http://%s:8090%s" % (ip, path)))
            res["robot_ok"] += 1
        except Exception as e:
            jdump(os.path.join(d, "로봇", "%s_%s.json" % (tag, name)), {"error": str(e)[:200]})
    for name, path in SERVER_GETS:
        try:
            jdump(os.path.join(d, "서버", "%s_%s.json" % (tag, name)), get_json(be + path))
            res["server_ok"] += 1
        except Exception as e:
            jdump(os.path.join(d, "서버", "%s_%s.json" % (tag, name)), {"error": str(e)[:200]})
    log("  로봇 상태 %d/%d · 서버 상태 %d/%d"
        % (res["robot_ok"], len(ROBOT_GETS), res["server_ok"], len(SERVER_GETS)))
    try:
        import clock_sync
        rec = clock_sync.measure(ip, precise=True)
        rec["source"] = "field_session_" + tag
        clock_sync.append_log(rec, os.path.join(d, "시계.jsonl"))
        clock_sync.append_log(rec)
        res["clock"] = rec.get("offset_sec")
        if rec.get("offset_sec") is not None:
            log("  시계 차이: 로봇이 %+.2f초 (로봇 - 서버)" % rec["offset_sec"])
        else:
            log("  시계 차이: 못 쟀음 (%s)" % rec.get("error", "로봇 응답 없음"))
    except Exception as e:
        log("  시계 차이: 못 쟀음 (%s)" % str(e)[:80])
    return res


def start_extras(d: str) -> None:
    """시작할 때만 남기는 것: git 상태, 현장 설정 파일 사본, ipconfig."""
    g = []
    for args in (["git", "rev-parse", "--abbrev-ref", "HEAD"],
                 ["git", "log", "--oneline", "-5"],
                 ["git", "status", "--short"]):
        g.append("$ " + " ".join(args) + "\n" + run_text(args, 15))
    with io.open(os.path.join(d, "서버", "git.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(g))
    for rel in ("BackEnd/default_area.json", "BackEnd/static/robot_speed.json",
                "BackEnd/static/system_settings.json"):
        src = os.path.join(ROOT, rel)
        if os.path.exists(src):
            dst = os.path.join(d, "서버", "설정사본", os.path.basename(rel))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    with io.open(os.path.join(d, "망", "ipconfig.txt"), "w", encoding="utf-8") as f:
        f.write(run_text(["ipconfig", "/all"], 20))


# ── 망 상시 기록기 (별도 프로세스) ──────────────────────────────
PING_MS = re.compile(r"(?:시간|time)\s*[=<]\s*(\d+)\s*ms", re.I)


def ping_once(ip: str) -> float | None:
    """ping 1회. 응답(TTL 있는 줄)이 없으면 None."""
    try:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", ip],
                           capture_output=True, timeout=5, creationflags=_NOWIN)
        out = dec(r.stdout)
    except Exception:
        return None
    if "TTL=" not in out.upper():
        return None
    m = PING_MS.search(out)
    return float(m.group(1)) if m else 0.0


def netwatch(d: str) -> None:
    """1초마다 ping 3곳, 5초마다 HTTP 왕복 2곳. STOP 파일이 생기면 끝낸다."""
    load_config()
    _use_session_cfg(d)
    nd = os.path.join(d, "망")
    os.makedirs(nd, exist_ok=True)
    stopf = os.path.join(d, "STOP")
    stop = threading.Event()
    lk = threading.Lock()
    fp = io.open(os.path.join(nd, "ping.jsonl"), "a", encoding="utf-8")
    fh = io.open(os.path.join(nd, "http.jsonl"), "a", encoding="utf-8")

    def put(f, obj):
        with lk:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

    def pinger(name, ip):
        while not stop.is_set():
            t = time.time()
            put(fp, {"t": round(t, 3), "name": name, "ip": ip, "ms": ping_once(ip)})
            stop.wait(max(0.0, 1.0 - (time.time() - t)))

    def http_loop():
        targets = [("로봇 REST", "http://%s:8090/device/info" % CFG["ROBOT_IP"]),
                   ("서버 백엔드", CFG["BACKEND"].rstrip("/") + "/ping")]
        while not stop.is_set():
            for name, url in targets:
                t = time.time()
                try:
                    with urllib.request.urlopen(url, timeout=5) as r:
                        r.read()
                    ms, err = round((time.time() - t) * 1000, 1), None
                except Exception as e:
                    ms, err = None, str(e)[:80]
                put(fh, {"t": round(t, 3), "name": name, "ms": ms, "err": err})
            stop.wait(5.0)

    def tracert():
        out = run_text(["tracert", "-d", "-h", "10", "-w", "1000", CFG["ROBOT_IP"]], 150)
        with io.open(os.path.join(nd, "tracert_시작.txt"), "w", encoding="utf-8") as f:
            f.write(out)

    ths = [threading.Thread(target=pinger, args=(n, ip), daemon=True) for n, ip in (
        ("로봇 본체", CFG["ROBOT_IP"]),
        ("로봇 라우터", CFG["ROBOT_ROUTER"]),
        ("서버 라우터", CFG["SERVER_ROUTER"]))]
    ths += [threading.Thread(target=http_loop, daemon=True),
            threading.Thread(target=tracert, daemon=True)]
    for t in ths:
        t.start()
    t0 = time.time()
    while not os.path.exists(stopf) and time.time() - t0 < NET_MAX_SEC:
        time.sleep(0.5)
    stop.set()
    time.sleep(1.5)
    with io.open(os.path.join(nd, "netwatch.done"), "w", encoding="utf-8") as f:
        f.write(hm(time.time()) + "\n")


def net_summary(d: str) -> list[str]:
    """ping/HTTP 기록을 사람이 읽는 몇 줄로."""
    lines = []

    def stat(rows, label):
        if not rows:
            return
        ok = sorted(r["ms"] for r in rows if r.get("ms") is not None)
        lost = len(rows) - len(ok)
        # 가장 길게 연속으로 끊긴 구간
        best, cur, cur_t, best_t = 0, 0, None, None
        for r in rows:
            if r.get("ms") is None:
                cur += 1
                cur_t = cur_t or r["t"]
                if cur > best:
                    best, best_t = cur, cur_t
            else:
                cur, cur_t = 0, None
        if ok:
            p95 = ok[min(len(ok) - 1, int(len(ok) * 0.95))]
            s = "평균 %4.0fms  p95 %4.0fms  최대 %5.0fms" % (sum(ok) / len(ok), p95, ok[-1])
        else:
            s = "응답 없음" + " " * 26
        s += "  손실 %4.1f%% (%d/%d)" % (lost / len(rows) * 100, lost, len(rows))
        if best >= 2:
            s += "  최장 끊김 %d회 연속(%s~)" % (best, hm(best_t))
        lines.append("  %-10s %s" % (label, s))

    ping = jlines(os.path.join(d, "망", "ping.jsonl"))
    for name in ("로봇 본체", "로봇 라우터", "서버 라우터"):
        stat([r for r in ping if r.get("name") == name], name)
    http = jlines(os.path.join(d, "망", "http.jsonl"))
    for name in ("로봇 REST", "서버 백엔드"):
        stat([r for r in http if r.get("name") == name], name)
    return lines


# ── 서버 로그 ───────────────────────────────────────────────────
LINE_MD = re.compile(r"^(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\s")
LINE_T = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s")


def backend_log_dir() -> str | None:
    best, bm = None, 0.0
    for cand in (os.path.join(ROOT, "BackEnd", "_logs"), os.path.join(ROOT, "_logs")):
        p = os.path.join(cand, "backend.log")
        if os.path.exists(p) and os.path.getmtime(p) > bm:
            best, bm = cand, os.path.getmtime(p)
    return best


def slice_backend(d: str, t0: float, t1: float, log) -> dict:
    """backend.log (+ 순환된 .1~.3) 에서 세션 구간만 잘라 낸다.

    줄 머리 형식이 두 가지다 — "09-28 17:54:18" (현행) / "17:54:18" (날짜 없음).
    날짜가 없으면 세션 시작일로 보고, 12시간 넘게 앞이면 다음 날로 넘긴다.
    시각이 없는 줄(Traceback 등)은 바로 앞 줄을 따라간다.
    """
    res = {"all": 0, "warn": 0, "safety": 0, "error": None, "rotated_gap": False}
    ld = backend_log_dir()
    if not ld:
        res["error"] = "backend.log 를 못 찾음 (BackEnd/_logs)"
        return res
    # 오래된 순환 파일부터 (2026-09-29 순환 개수 3 → 5)
    files = [os.path.join(ld, n) for n in
             [f"backend.log.{i}" for i in range(9, 0, -1)] + ["backend.log"]]
    files = [f for f in files if os.path.exists(f)]
    lo, hi = t0 - 30, t1 + 30
    y = time.localtime(t0).tm_year
    day0 = time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(t0)), "%Y-%m-%d"))
    sd = os.path.join(d, "서버")
    os.makedirs(sd, exist_ok=True)
    fa = io.open(os.path.join(sd, "backend_세션구간.log"), "w", encoding="utf-8")
    fw = io.open(os.path.join(sd, "backend_경고오류.log"), "w", encoding="utf-8")
    fs = io.open(os.path.join(sd, "backend_안전존.log"), "w", encoding="utf-8")
    first_seen = None
    inside = False
    for fp in files:
        with io.open(fp, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LINE_MD.match(line)
                st = None
                if m:
                    mo, dd, hh, mi, ss = (int(x) for x in m.groups())
                    try:
                        st = time.mktime((y, mo, dd, hh, mi, ss, 0, 0, -1))
                    except Exception:
                        st = None
                else:
                    m = LINE_T.match(line)
                    if m:
                        hh, mi, ss = (int(x) for x in m.groups())
                        st = day0 + hh * 3600 + mi * 60 + ss
                        if st < t0 - 43200:
                            st += 86400
                if st is not None:
                    if first_seen is None:
                        first_seen = st
                    inside = lo <= st <= hi
                if not inside:
                    continue
                fa.write(line)
                res["all"] += 1
                if " WARNING " in line or " ERROR " in line or "Traceback" in line \
                        or (st is None and line.startswith((" ", "\t"))):
                    fw.write(line)
                    res["warn"] += 1
                if "[safety]" in line:
                    fs.write(line)
                    res["safety"] += 1
    for f in (fa, fw, fs):
        f.close()
    if first_seen is not None and first_seen > t0 + 5:
        res["rotated_gap"] = True
        res["first"] = first_seen
    log("  서버 로그 %d줄 (경고·오류 %d · 안전존 %d)" % (res["all"], res["warn"], res["safety"]))
    if res["rotated_gap"]:
        log("  ! 서버 로그가 %s 부터만 남아 있습니다 (10MB 순환으로 앞부분 삭제)" % hm(first_seen))
    return res


def db_logs(d: str, t0: float, t1: float, log) -> dict:
    """백엔드 DB 의 알람·활동 로그를 세션 구간으로 받아 둔다."""
    be = CFG["BACKEND"].rstrip("/")
    df = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0 - 60))
    dt = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t1 + 60))
    res = {}
    for name, path, lim in (("알람로그", "/api/alarm-logs", 200),
                            ("활동로그", "/api/activity-logs", 500)):
        items, skip = [], 0
        try:
            for _ in range(50):
                r = get_json("%s%s?skip=%d&limit=%d&date_from=%s&date_to=%s"
                             % (be, path, skip, lim, df, dt), 10)
                got = r.get("items") or []
                items += got
                if not got or len(items) >= int(r.get("total") or 0):
                    break
                skip += lim
            jdump(os.path.join(d, "서버", name + ".json"), items)
            res[name] = len(items)
        except Exception as e:
            res[name] = None
            res[name + "_err"] = str(e)[:100]
    log("  알람 로그 %s · 활동 로그 %s" % tuple(
        ("%d건" % res[k]) if res.get(k) is not None else "실패 (백엔드 응답 없음)"
        for k in ("알람로그", "활동로그")))
    return res


# ── 로봇 bag ────────────────────────────────────────────────────
def get_bags(d: str, t0: float, t1: float, log) -> dict:
    import fetch_bags
    log("  로봇 bag 받는 중 (세션 앞뒤 %d분) — 10분에 25~45MB 라 시간이 걸립니다" % (BAG_MARGIN // 60))
    try:
        jdump(os.path.join(d, "로봇", "bag_목록.json"), fetch_bags.fetch_list(CFG["ROBOT_IP"]))
    except Exception:
        pass
    r = fetch_bags.fetch_range(CFG["ROBOT_IP"], t0 - BAG_MARGIN, t1 + BAG_MARGIN,
                               os.path.join(d, "로봇", "bag"), log)
    jdump(os.path.join(d, "로봇", "bag_결과.json"), r)
    return r


def bag_done(r: dict, t1: float) -> bool:
    """세션 끝 시각까지 bag 이 덮였는가."""
    return bool(r.get("covered_to")) and r["covered_to"] >= t1


# ── 인덱스 · zip ────────────────────────────────────────────────
def write_index(d: str) -> str:
    s = jload(os.path.join(d, "session.json"), {})
    t0, t1 = s.get("start_t"), s.get("end_t") or time.time()
    marks = jlines(os.path.join(d, "marks.jsonl"))
    clocks = [c for c in jlines(os.path.join(d, "시계.jsonl")) if c.get("offset_sec") is not None]
    off = clocks[-1]["offset_sec"] if clocks else None
    bag = jload(os.path.join(d, "로봇", "bag_결과.json"), {}) or {}
    srv = s.get("server_log") or {}
    db = s.get("db_logs") or {}
    dl_dir = os.path.join(d, "drive_log")
    cycles = sorted(n for n in os.listdir(dl_dir) if n.startswith("c")) if os.path.isdir(dl_dir) else []
    dl_done = jload(os.path.join(d, "session.json"), {}).get("drive_log_end", "")

    L = []
    L.append("=" * 72)
    L.append(" RCS LG 로그 추출 — 세션 %s" % s.get("id", "?"))
    L.append("=" * 72)
    L.append(" 기간   %s ~ %s  (%s)   ※ 모든 시각은 한국 시각(서버PC)"
             % (hm(t0, "%Y-%m-%d %H:%M:%S"), hm(t1), dur(t1 - t0)))
    L.append(" 로봇   %s        서버 %s" % (s.get("robot_ip"), s.get("backend")))
    L.append(" git    %s" % s.get("git", "-"))
    L.append("")
    L.append("[1] 들어 있는 것")
    ok, no = "  O", "  X"

    def row(good, label, detail, where):
        L.append("%s %-18s %-40s %s" % (ok if good else no, label, detail, where))

    row(True, "비이상적 정지 표시", "%d건" % len(marks), "marks.jsonl")
    row(bool(cycles), "주행 기록", ("사이클 %d개" % len(cycles)) if cycles
        else "사이클 없음 — drive_log_화면.txt 확인", "drive_log/")
    row(not srv.get("error"), "서버 로그",
        srv.get("error") or "%d줄 (경고·오류 %d, 안전존 %d)"
        % (srv.get("all", 0), srv.get("warn", 0), srv.get("safety", 0)), "서버/")
    row(db.get("알람로그") is not None, "알람·활동 로그(DB)",
        ("%s건 / %s건" % (db.get("알람로그"), db.get("활동로그")))
        if db.get("알람로그") is not None else "실패 — 백엔드 응답 없음", "서버/")
    npings = len(jlines(os.path.join(d, "망", "ping.jsonl")))
    row(npings > 0, "망 상시 기록", "ping %d회" % npings, "망/")
    nb = len(bag.get("ok") or [])
    if nb:
        mb = sum(b.get("size_mb", 0) for b in bag["ok"])
        cov = "%s~%s" % (hm(min(b["start"] for b in bag["ok"]), "%H:%M"),
                         hm(max(b["end"] for b in bag["ok"]), "%H:%M"))
        row(True, "로봇 bag", "%d개 %.0fMB (%s 덮음)" % (nb, mb, cov), "로봇/bag/")
    else:
        row(False, "로봇 bag", bag.get("error") or "받지 못함", "로봇/bag/")
    row(bool(clocks), "시계 차이",
        ("로봇 %+.2f초 (로봇-서버)" % off) if off is not None else "못 쟀음", "시계.jsonl")
    L.append("")

    L.append("[2] 비이상적 정지 표시  (서버 시각 / 로봇 시각 / 들어 있는 bag)")
    if not marks:
        L.append("  (없음)")
    for i, m in enumerate(marks, 1):
        t = float(m.get("t") or 0)
        rt = hm(t + off) if off is not None else "-"
        bf = next((b["file"] for b in (bag.get("ok") or []) if b["start"] <= t < b["end"]), "-")
        L.append("  %2d  %s  %-6s  로봇 %s  %s%s" % (
            i, hm(t), m.get("source", ""), rt, bf, ("   메모: " + m["note"]) if m.get("note") else ""))
    L.append("  ※ 사람은 느끼고 나서 누른다(실측 0.9~4.2초 늦음). 표시 몇 초 앞을 보세요.")
    L.append("")

    L.append("[3] 망 요약  (ping 1초 · HTTP 5초 간격)")
    L += net_summary(d) or ["  (기록 없음)"]
    L.append("")

    L.append("[4] 타임라인")
    ev = [(t0, "기록 시작")]
    ev += [(float(m["t"]), "비이상적 정지 표시 (%s)%s" % (m.get("source", ""),
            (" " + m["note"]) if m.get("note") else "")) for m in marks if m.get("t")]
    for a in (jload(os.path.join(d, "서버", "알람로그.json"), []) or []):
        try:
            at = time.mktime(time.strptime(str(a.get("created_at"))[:19], "%Y-%m-%dT%H:%M:%S"))
            ev.append((at, "알람 %s %s" % (a.get("error_code") or "", (a.get("message") or "")[:60])))
        except Exception:
            pass
    for c in cycles:
        mt = re.match(r"c(\d+)_(\d{2})(\d{2})(\d{2})", c)
        if mt:
            base = time.strftime("%Y-%m-%d", time.localtime(t0))
            try:
                ct = time.mktime(time.strptime("%s %s:%s:%s" % ((base,) + mt.groups()[1:]),
                                               "%Y-%m-%d %H:%M:%S"))
                if ct < t0 - 60:
                    ct += 86400
                ev.append((ct, "주행 사이클 %s 시작" % mt.group(1)))
            except Exception:
                pass
    ev.append((t1, "기록 종료"))
    for t, what in sorted(ev):
        L.append("  %s  %s" % (hm(t), what))
    L.append("")

    L.append("[5] 주의 / 빠진 것")
    warns = []
    if srv.get("rotated_gap"):
        warns.append("서버 로그가 %s 부터만 남음 — 10MB 순환으로 앞부분 삭제" % hm(srv.get("first", t0)))
    if bag.get("recording") and not bag_done(bag, t1):
        warns.append("로봇 bag: %s 이후는 녹화 중이라 못 받음 → L3_bag다시받기.bat 로 이어받기"
                     % hm(bag["recording"], "%H:%M"))
    if bag.get("fail"):
        warns.append("로봇 bag 실패 %d개: %s" % (len(bag["fail"]), ", ".join(bag["fail"])))
    if bag.get("tz") is not None:
        warns.append("참고: 이 로봇의 bag 파일명은 UTC%+g 기준 (%s). 파일명 시각 ≠ 한국 시각"
                     % (bag["tz"], bag.get("tz_how", "")))
    if dl_done and "rc=0" not in dl_done:
        warns.append("주행 기록기(drive_log)가 비정상 종료 (%s) — drive_log_화면.txt 확인" % dl_done)
    if s.get("killed"):
        warns.append("종료 신호에 응답이 없어 강제로 끈 기록기: %s" % ", ".join(s["killed"]))
    L += ["  - " + w for w in warns] or ["  (없음)"]
    L.append("")
    L.append("[6] 어디부터 보나")
    L.append("  1) 위 [2] 표시 시각 → drive_log/c??_*/summary.md 의 같은 시각")
    L.append("  2) 같은 시각 서버/backend_안전존.log — 서버가 속도를 눌렀는지")
    L.append("  3) 같은 시각 망/ping.jsonl — 그때 통신이 끊겼는지")
    L.append("  4) 원인이 로봇 안쪽이면 로봇/bag/ 의 해당 파일을 제조사(AutoXing)에 전달")
    p = os.path.join(d, "인덱스.txt")
    with io.open(p, "w", encoding="utf-8-sig") as f:     # 메모장 호환(BOM)
        f.write("\n".join(L) + "\n")
    return p


def make_zip(d: str, log) -> str:
    s = jload(os.path.join(d, "session.json"), {})
    t0, t1 = s.get("start_t", time.time()), s.get("end_t", time.time())
    os.makedirs(ZIPDIR, exist_ok=True)
    name = "로그추출_%s_%s-%s.zip" % (hm(t0, "%Y%m%d"), hm(t0, "%H%M"), hm(t1, "%H%M"))
    dst = os.path.join(ZIPDIR, name)
    tmp = dst + ".part"
    top = "세션_" + os.path.basename(d)
    with zipfile.ZipFile(tmp, "w") as z:
        for base, _dirs, files in os.walk(d):
            for fn in files:
                if fn in ("STOP",):
                    continue
                full = os.path.join(base, fn)
                arc = os.path.join(top, os.path.relpath(full, d))
                # bag·png 는 이미 압축돼 있어 다시 압축하면 시간만 든다
                comp = zipfile.ZIP_STORED if fn.lower().endswith((".bag", ".png", ".zip")) \
                    else zipfile.ZIP_DEFLATED
                z.write(full, arc, compress_type=comp)
    os.replace(tmp, dst)
    log("  zip: %s (%.1f MB)" % (dst, os.path.getsize(dst) / 1048576))
    return dst


# ── 명령 ────────────────────────────────────────────────────────
def status() -> dict:
    cur = jload(CUR)
    if not cur or not os.path.isdir(cur.get("dir", "")):
        last = jload(LAST) or {}
        return {"active": False, "last_zip": last.get("zip")}
    d = cur["dir"]
    return {"active": True, "dir": d, "id": os.path.basename(d),
            "start": hm(cur["start_t"]), "elapsed": dur(time.time() - cur["start_t"]),
            "marks": len(jlines(os.path.join(d, "marks.jsonl"))),
            "drive_log": pid_alive(cur.get("pid_drive")),
            "netwatch": pid_alive(cur.get("pid_net"))}


def start(log=print, ip: str | None = None) -> dict:
    load_config()
    if ip:
        CFG["ROBOT_IP"] = ip
    cur = jload(CUR)
    if cur and os.path.isdir(cur.get("dir", "")):
        return {"ok": False, "msg": "이미 기록 중입니다 (시작 %s). 먼저 종료하세요." % hm(cur["start_t"])}
    t0 = time.time()
    sid = hm(t0, "%Y%m%d_%H%M%S")
    d = os.path.join(SROOT, sid)
    for sub in ("망", "서버", "로봇"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    git = run_text(["git", "log", "--oneline", "-1"], 10).strip()
    br = run_text(["git", "rev-parse", "--abbrev-ref", "HEAD"], 10).strip()
    sess = {"id": sid, "start_t": t0, "robot_ip": CFG["ROBOT_IP"], "backend": CFG["BACKEND"],
            "routers": [CFG["ROBOT_ROUTER"], CFG["SERVER_ROUTER"]],
            "git": "%s  %s" % (br, git)}
    jdump(os.path.join(d, "session.json"), sess)
    io.open(os.path.join(d, "marks.jsonl"), "a", encoding="utf-8").close()

    log("세션 %s 시작 — 로봇 %s" % (sid, CFG["ROBOT_IP"]))
    log("[1/3] 시작 상태 기록")
    snapshot("시작", d, log)
    start_extras(d)

    log("[2/3] 망 상시 기록 시작 (ping 1초 · HTTP 5초)")
    DETACHED = 0x00000008 | 0x00000200            # DETACHED_PROCESS | NEW_PROCESS_GROUP
    pn = subprocess.Popen([PY, "-u", os.path.abspath(__file__), "_netwatch", d], cwd=ROOT,
                          stdout=io.open(os.path.join(d, "망", "netwatch_화면.txt"), "a"),
                          stderr=subprocess.STDOUT, creationflags=DETACHED)

    log("[3/3] 주행 기록기 시작 — 새 검은 창이 열립니다 (닫지 마세요)")
    dl = os.path.join(HERE, "drive_log.py")
    args = [PY, "-u", dl, "--ip", CFG["ROBOT_IP"], "--cycles", "0",
            "--server", CFG["BACKEND"], "--session-dir", d]
    bl = os.path.join(backend_log_dir() or "", "backend.log")
    if os.path.exists(bl):
        args += ["--backend-log", bl]
    pd = subprocess.Popen(args, cwd=ROOT, env=dict(os.environ, PYTHONUNBUFFERED="1"),
                          creationflags=0x00000010 | 0x00000200)   # NEW_CONSOLE | NEW_GROUP
    jdump(CUR, {"dir": d, "start_t": t0, "pid_net": pn.pid, "pid_drive": pd.pid})
    log("")
    log("기록 중입니다. 비이상적 정지를 보면:")
    log("  · 서버PC 앞  → 주행 기록기 창에서 [스페이스바]")
    log("  · 로봇 옆    → 폰으로 현장콘솔 열고 [비이상적 정지] 버튼")
    log("끝나면 L2_종료및추출.bat (또는 현장콘솔 [종료·추출])")
    log("")
    log("이 창은 닫아도 됩니다. 새로 열린 검은 창(주행 기록기)만 닫지 마세요.")
    return {"ok": True, "dir": d, "id": sid}


def mark(note: str = "", source: str = "웹") -> dict:
    cur = jload(CUR)
    if not cur or not os.path.isdir(cur.get("dir", "")):
        return {"ok": False, "msg": "기록 중이 아닙니다 — 먼저 [기록 시작]"}
    t = time.time()
    rec = {"t": round(t, 3), "ts": hm(t), "source": source, "note": note}
    with io.open(os.path.join(cur["dir"], "marks.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    rec["ok"] = True
    rec["count"] = len(jlines(os.path.join(cur["dir"], "marks.jsonl")))
    return rec


def _finish_bags(d: str, t0: float, t1: float, log, wait: bool) -> None:
    """bag 받기 → 인덱스·zip. 마지막 bag 이 녹화 중이면 기다렸다가 zip 을 갱신한다."""
    r = get_bags(d, t0, t1, log)
    write_index(d)
    z = make_zip(d, log)
    jdump(LAST, {"dir": d, "zip": z})
    if bag_done(r, t1) or not wait or r.get("error"):
        return
    log("")
    log("  로봇이 마지막 bag(10분 단위)을 아직 녹화 중입니다.")
    log("  zip 은 이미 만들어졌습니다. 녹화가 끝나면 받아서 zip 을 갱신합니다 (최대 %d분)."
        % (BAG_WAIT_MAX // 60))
    log("  기다리기 싫으면 창을 닫고, 나중에 L3_bag다시받기.bat 를 누르세요.")
    tw = time.time()
    while time.time() - tw < BAG_WAIT_MAX:
        time.sleep(30)
        log("  … 기다리는 중 %s" % dur(time.time() - tw))
        r = get_bags(d, t0, t1, lambda *_: None)
        if bag_done(r, t1):
            write_index(d)
            z = make_zip(d, log)
            jdump(LAST, {"dir": d, "zip": z})
            log("  마지막 bag 까지 받았습니다.")
            return
    log("  기다려도 녹화가 끝나지 않았습니다 — 나중에 L3_bag다시받기.bat")


def _use_session_cfg(d: str) -> None:
    """시작할 때의 로봇·서버를 쓴다 (도중에 config.bat 을 고쳐도 같은 로봇에서 받게)."""
    s = jload(os.path.join(d, "session.json"), {})
    if s.get("robot_ip"):
        CFG["ROBOT_IP"] = s["robot_ip"]
    if s.get("backend"):
        CFG["BACKEND"] = s["backend"]


def stop(log=print, wait_bag: bool = True) -> dict:
    load_config()
    cur = jload(CUR)
    if not cur or not os.path.isdir(cur.get("dir", "")):
        return {"ok": False, "msg": "기록 중인 세션이 없습니다"}
    d, t0 = cur["dir"], cur["start_t"]
    t1 = time.time()
    _use_session_cfg(d)
    with io.open(os.path.join(d, "STOP"), "w", encoding="utf-8") as f:
        f.write(hm(t1) + "\n")
    sp = os.path.join(d, "session.json")
    sess = jload(sp, {})
    sess["end_t"] = t1
    jdump(sp, sess)
    log("세션 %s 종료 — %s ~ %s (%s)" % (os.path.basename(d), hm(t0), hm(t1), dur(t1 - t0)))

    log("[1/5] 기록기 정리 (주행 요약 작성에 수십 초 걸릴 수 있습니다)")
    done_d, done_n = os.path.join(d, "drive_log.done"), os.path.join(d, "망", "netwatch.done")
    tw = time.time()
    while time.time() - tw < 90:
        a = os.path.exists(done_d) or not pid_alive(cur.get("pid_drive"))
        b = os.path.exists(done_n) or not pid_alive(cur.get("pid_net"))
        if a and b:
            break
        time.sleep(1)
    killed = []
    for key, name in (("pid_drive", "주행 기록기"), ("pid_net", "망 기록기")):
        if pid_alive(cur.get(key)):
            run_text(["taskkill", "/PID", str(cur[key]), "/T", "/F"], 15)
            killed.append(name)
    sess["killed"] = killed
    if os.path.exists(done_d):
        sess["drive_log_end"] = io.open(done_d, encoding="utf-8").read().strip()
    try:
        os.remove(CUR)                                # 이 시점부터 새 세션을 시작할 수 있다
    except Exception:
        pass
    jdump(LAST, {"dir": d})                          # 도중에 실패해도 L3 로 이어갈 수 있게

    log("[2/5] 종료 상태 기록")
    snapshot("종료", d, log)
    log("[3/5] 서버 로그 잘라내기")
    sess["server_log"] = slice_backend(d, t0, t1, log)
    log("[4/5] 알람·활동 로그(DB)")
    sess["db_logs"] = db_logs(d, t0, t1, log)
    jdump(sp, sess)
    log("[5/5] 로봇 bag → 인덱스 → zip")
    _finish_bags(d, t0, t1, log, wait_bag)
    last = jload(LAST, {})
    log("")
    log("완료. 이 파일 하나만 챙기면 됩니다:")
    log("  " + (last.get("zip") or "(zip 실패)"))
    return {"ok": True, "zip": last.get("zip"), "dir": d}


def bags(log=print) -> dict:
    """마지막 세션의 bag 을 다시 받고(이어받기) zip 을 갱신한다."""
    load_config()
    last = jload(LAST) or {}
    d = last.get("dir")
    if not d or not os.path.isdir(d):
        return {"ok": False, "msg": "끝낸 세션이 없습니다"}
    s = jload(os.path.join(d, "session.json"), {})
    t0, t1 = s["start_t"], s.get("end_t") or time.time()
    _use_session_cfg(d)
    log("세션 %s (%s ~ %s) 의 bag 을 다시 받습니다" % (os.path.basename(d), hm(t0), hm(t1)))
    _finish_bags(d, t0, t1, log, wait=False)
    return {"ok": True, "zip": (jload(LAST) or {}).get("zip")}


def main() -> int:
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "_netwatch":
        netwatch(a[1])
        return 0
    if cmd == "start":
        ip = a[a.index("--ip") + 1] if "--ip" in a else None
        r = start(ip=ip)
    elif cmd == "stop":
        r = stop(wait_bag="--no-wait" not in a)
    elif cmd == "mark":
        r = mark(" ".join(a[1:]), "명령")
        print("표시 %s (%s건째)" % (r.get("ts"), r.get("count")) if r.get("ok") else r["msg"])
    elif cmd == "bags":
        r = bags()
    elif cmd == "open":                    # bat 에 한글 경로를 못 쓰므로 여기서 연다
        if os.path.isdir(ZIPDIR):
            try:
                os.startfile(ZIPDIR)
            except Exception:
                pass
        r = {"ok": True}
    else:
        r = status()
        print(json.dumps(r, ensure_ascii=False, indent=1))
    if not r.get("ok", True) and r.get("msg"):
        print(r["msg"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
