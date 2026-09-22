#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""현장 점검 콘솔 — bat 여러 개를 웹 화면 하나로 묶은 것.

    BackEnd\\venv\\Scripts\\python.exe scripts\\field_console.py
    또는  현장작업\\현장콘솔.bat  더블클릭

왜 만들었나
  현장은 방진복을 입고 들어가는 생산라인 내부라 왕복 비용이 크다.
  bat 을 8개 왔다갔다 하면서 창을 번갈아 보는 것보다, 한 화면에서
  순서대로 누르는 편이 빠르고 빠뜨릴 일이 없다.

  ★ 무엇보다 **[멈칫] 버튼을 폰으로 누를 수 있다.**
    종전에는 drive_log 터미널에 포커스를 두고 스페이스바를 쳐야 했는데,
    로봇을 따라다니며 그러기는 어렵다. 같은 망의 폰에서 이 화면을 열면
    로봇 옆을 걸으며 누를 수 있다.

무엇을 하나
  1) 상태      git · 백엔드 · 로봇 응답 · 현재 맵 영역
  2) 망 측정   ping 3종 + tracert + ipconfig  ★ 현장에서만 잴 수 있다
  3) 사전 점검 drive_log --probe. 로그가 실제로 남는지 먼저 본다
  4) 주행 기록 drive_log 본 측정 + 멈칫 표시
  5) 로그 수집 _logs + BackEnd/_logs 를 zip 으로

★ 로봇에 명령을 보내지 않는다
  ping 과 GET 조회뿐이다. 이동·잭·속도 명령은 한 줄도 없다.
  주행 기록은 drive_log.py 를 그대로 실행하는 것이고, 그쪽도 구독만 한다.

★ 표준 라이브러리만 쓴다
  현장은 인터넷이 없어 pip install 을 못 한다. 그래서 외부 패키지를 안 쓴다.
  웹 화면도 외부 리소스가 0개라 파일만 있으면 열린다.

폰에서 보려면
  서버PC 와 같은 망에 붙어서  http://<서버PC IP>:8777  로 연다.
  (실행하면 화면에 주소가 뜬다)
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime

PORT = 8777
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOGS = os.path.join(ROOT, "_logs")
PY = os.path.join(ROOT, "BackEnd", "venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

# 설정 — 현장작업/config.bat 에서 읽는다 (한 곳만 고치면 되게)
CFG = {
    "ROBOT_IP": "10.14.182.126",
    "ROBOT_ROUTER": "10.115.244.12",
    "SERVER_ROUTER": "10.115.244.11",
    "BACKEND": "http://127.0.0.1:8002",
}


def safe(s: str) -> str:
    """깨진 바이트가 섞여도 JSON 직렬화가 죽지 않게 정리한다.

    ping/tracert 출력은 콘솔 코드페이지 그대로라 CP949 로 못 읽는 바이트가
    섞일 수 있다. 그대로 두면 json.dumps 가 UnicodeEncodeError 로 죽고
    화면이 통째로 안 뜬다(500).
    """
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


def dec(b: bytes) -> str:
    """서브프로세스 출력 → 안전한 문자열."""
    return safe(b.decode("cp949", errors="replace"))


def load_config() -> None:
    """현장작업/config.bat 의 set 줄을 읽는다. 없으면 기본값."""
    p = os.path.join(ROOT, "현장작업", "config.bat")
    if not os.path.exists(p):
        return
    try:
        raw = dec(open(p, "rb").read())
    except Exception:
        return
    for m in re.finditer(r"^\s*set\s+([A-Z_]+)=(.+?)\s*$", raw, re.M):
        k, v = m.group(1), m.group(2).strip()
        if k in CFG and v:
            CFG[k] = v


# ── 작업 실행기 ─────────────────────────────────────────────────
class Job:
    """외부 명령 하나. 출력을 줄 단위로 모아 웹이 폴링해 간다."""

    def __init__(self, jid: str, title: str) -> None:
        self.id = jid
        self.title = title
        self.lines: list[str] = []
        self.done = False
        self.rc: int | None = None
        self.proc: subprocess.Popen | None = None
        self.started = time.time()
        self.lock = threading.Lock()

    def put(self, s: str) -> None:
        with self.lock:
            self.lines.append(s)
            if len(self.lines) > 4000:        # 너무 길면 앞을 버린다
                del self.lines[:1000]

    def tail(self, frm: int) -> tuple[list[str], int]:
        with self.lock:
            return self.lines[frm:], len(self.lines)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


_job_seq = [0]


def new_job(title: str) -> Job:
    # id 는 URL 경로에 들어가므로 ASCII 로만 만든다.
    # (한글 title 을 그대로 쓰면 경로가 깨져 조회가 404 가 된다)
    _job_seq[0] += 1
    jid = "job%d_%d" % (_job_seq[0], int(time.time() * 1000) % 100000)
    j = Job(jid, title)
    with JOBS_LOCK:
        JOBS[jid] = j
    return j


def run_cmd(job: Job, args: list[str], cwd: str | None = None) -> int:
    """명령 하나를 돌리며 출력을 job 에 넣는다."""
    try:
        p = subprocess.Popen(
            args, cwd=cwd or ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        job.put("[실행 실패] %s" % e)
        return -1
    job.proc = p
    assert p.stdout is not None
    for raw in p.stdout:
        try:
            job.put(dec(raw).rstrip("\r\n"))
        except Exception:
            job.put(repr(raw)[:200])
    p.wait()
    return p.returncode


# ── 1) 망 측정 ──────────────────────────────────────────────────
PING_AVG = re.compile(r"(?:평균|Average)\s*=\s*(\d+)\s*ms")
PING_LOSS = re.compile(r"\((\d+)%\s*(?:손실|loss)\)")


def job_netcheck(job: Job) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(LOGS, "망측정_%s" % ts)
    os.makedirs(outdir, exist_ok=True)
    job.put("저장 위치: %s" % outdir)
    job.put("")

    targets = [
        ("로봇 본체", CFG["ROBOT_IP"], "ping_로봇.txt"),
        ("로봇 라우터", CFG["ROBOT_ROUTER"], "ping_로봇라우터.txt"),
        ("서버 라우터", CFG["SERVER_ROUTER"], "ping_서버라우터.txt"),
    ]
    summary = []
    for i, (name, ip, fn) in enumerate(targets, 1):
        job.put("[%d/5] %s (%s) ping 20회 ..." % (i, name, ip))
        try:
            out = dec(subprocess.run(
                ["ping", ip, "-n", "20"], capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout)
        except Exception as e:
            out = "실행 실패: %s" % e
        open(os.path.join(outdir, fn), "w", encoding="utf-8").write(out)
        avg = PING_AVG.search(out)
        loss = PING_LOSS.search(out)
        if avg:
            summary.append((name, int(avg.group(1)), loss.group(1) if loss else "?"))
            job.put("      평균 %s ms · 손실 %s%%" % (avg.group(1), loss.group(1) if loss else "?"))
        else:
            summary.append((name, None, loss.group(1) if loss else "100"))
            job.put("      응답 없음")

    job.put("")
    job.put("[4/5] 경로 추적 ...")
    try:
        out = dec(subprocess.run(
            ["tracert", "-d", "-h", "10", CFG["ROBOT_IP"]], capture_output=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout)
    except Exception as e:
        out = "실행 실패: %s" % e
    open(os.path.join(outdir, "tracert_로봇.txt"), "w", encoding="utf-8").write(out)
    hops = [l for l in out.splitlines() if re.match(r"^\s*\d+\s", l)]
    for l in hops:
        job.put("      " + l.strip())

    job.put("")
    job.put("[5/5] 서버PC 네트워크 정보 ...")
    try:
        out = dec(subprocess.run(
            ["ipconfig", "/all"], capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout)
        open(os.path.join(outdir, "ipconfig.txt"), "w", encoding="utf-8").write(out)
    except Exception as e:
        job.put("      실패: %s" % e)

    # 판정
    job.put("")
    job.put("=" * 52)
    robot_avg = summary[0][1] if summary else None
    if robot_avg is None:
        job.put("판정: 로봇에서 응답이 없습니다 (ICMP 차단이거나 미연결)")
        job.put("      → HTTP 측정으로 다시 재야 합니다")
    elif robot_avg >= 200:
        job.put("판정: 망이 원인입니다 (평균 %d ms)" % robot_avg)
        job.put("      → 라우터 통합이 1순위. 코드 수정 없이 해결됩니다")
    elif robot_avg <= 20:
        job.put("판정: 망은 빠릅니다 (평균 %d ms)" % robot_avg)
        job.put("      → 446ms 는 로봇 REST 처리가 느린 것입니다")
        job.put("      → 통신 왕복 횟수를 줄이는 코드 작업이 필요합니다")
    else:
        job.put("판정: 망이 좀 느립니다 (평균 %d ms) — 둘 다 영향" % robot_avg)
    job.put("홉 수 %d개%s" % (len(hops), " (4 이상이면 통신사 망을 왕복)" if len(hops) >= 4 else ""))
    job.put("=" * 52)
    job.put("")
    job.put("★ 이 폴더를 통째로 챙겨 나오세요: %s" % outdir)


# ── 2) 사전 점검 / 주행 기록 ────────────────────────────────────
def job_probe(job: Job) -> None:
    dl = os.path.join(ROOT, "scripts", "drive_log.py")
    if not os.path.exists(dl):
        job.put("[없음] scripts/drive_log.py 가 없습니다")
        return
    job.put("drive_log --probe (약 30초)")
    job.put("확인할 것: motion_metrics · tracked_pose · planning_state · rest_rtt")
    job.put("하나라도 0%% 면 주행해도 빈 데이터만 쌓입니다")
    job.put("-" * 52)
    run_cmd(job, [PY, dl, "--ip", CFG["ROBOT_IP"], "--probe", "--server", CFG["BACKEND"]])


def job_record(job: Job, cycles: int) -> None:
    dl = os.path.join(ROOT, "scripts", "drive_log.py")
    if not os.path.exists(dl):
        job.put("[없음] scripts/drive_log.py 가 없습니다")
        return
    blog = os.path.join(ROOT, "BackEnd", "_logs", "backend.log")
    job.put("사이클 %d회 기록 시작" % cycles)
    job.put("불편한 정지를 볼 때마다 위의 [멈칫] 버튼을 누르세요")
    job.put("-" * 52)
    args = [PY, dl, "--ip", CFG["ROBOT_IP"], "--cycles", str(cycles),
            "--server", CFG["BACKEND"]]
    if os.path.exists(blog):
        args += ["--backend-log", blog]
    run_cmd(job, args)


# ── 3) 멈칫 표시 ────────────────────────────────────────────────
MARK_FILE = {"path": None}
MARKS: list[dict] = []


def mark_now(note: str = "") -> dict:
    """사람이 누른 시각을 기록. drive_log 와 같은 단일 시계(epoch)를 쓴다."""
    if MARK_FILE["path"] is None:
        os.makedirs(LOGS, exist_ok=True)
        MARK_FILE["path"] = os.path.join(
            LOGS, "marks_%s.jsonl" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    rec = {"t": time.time(), "ts": datetime.now().strftime("%H:%M:%S"), "note": note}
    MARKS.append(rec)
    try:
        with open(MARK_FILE["path"], "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


# ── 4) 로그 수집 ────────────────────────────────────────────────
def job_collect(job: Job) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = os.path.join(os.environ.get("TEMP", ROOT), "logpack_%s" % ts)
    os.makedirs(stage, exist_ok=True)
    n = 0
    for src, dst in ((LOGS, "_logs"),
                     (os.path.join(ROOT, "BackEnd", "_logs"), "backend_logs")):
        if os.path.isdir(src):
            job.put("모으는 중: %s" % src)
            shutil.copytree(src, os.path.join(stage, dst), dirs_exist_ok=True)
            n += 1
    if n == 0:
        job.put("[없음] 모을 로그가 없습니다")
        return
    out = os.path.join(ROOT, "로그수집_%s" % ts)
    zip_path = shutil.make_archive(out, "zip", stage)
    shutil.rmtree(stage, ignore_errors=True)
    mb = os.path.getsize(zip_path) / 1048576
    job.put("")
    job.put("만들어졌습니다: %s" % zip_path)
    job.put("크기: %.1f MB" % mb)
    job.put("")
    job.put("★ 나올 때 이 파일과 망측정 폴더를 챙기세요")


# ── 5) 상태 ─────────────────────────────────────────────────────
def get_status() -> dict:
    st: dict = {"time": datetime.now().strftime("%H:%M:%S")}

    def sh(args):
        try:
            return dec(subprocess.run(
                args, cwd=ROOT, capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout).strip()
        except Exception:
            return ""

    st["git_log"] = sh(["git", "log", "--oneline", "-3"])
    st["git_branch"] = sh(["git", "branch", "--show-current"])
    st["git_dirty"] = sh(["git", "status", "--short"])[:600]

    # 백엔드
    try:
        with urllib.request.urlopen(CFG["BACKEND"] + "/ping", timeout=4) as r:
            st["backend"] = "OK %d" % r.status
    except Exception as e:
        st["backend"] = "응답 없음 (%s)" % str(e)[:60]

    # 현재 맵 영역
    try:
        with urllib.request.urlopen(CFG["BACKEND"] + "/api/map/default-area", timeout=4) as r:
            st["area"] = json.loads(r.read().decode("utf-8"))
    except Exception:
        st["area"] = None

    # 로봇 ping 2회
    try:
        out = dec(subprocess.run(
            ["ping", CFG["ROBOT_IP"], "-n", "2"], capture_output=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout)
        m = PING_AVG.search(out)
        st["robot"] = ("평균 %s ms" % m.group(1)) if m else "응답 없음"
    except Exception:
        st["robot"] = "측정 실패"

    # 로봇이 로드한 맵
    try:
        with urllib.request.urlopen(
                "http://%s:8090/chassis/current-map" % CFG["ROBOT_IP"], timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
            st["robot_map"] = {"id": d.get("id"), "name": d.get("map_name")}
    except Exception:
        st["robot_map"] = None

    st["files"] = {
        "drive_log.py": os.path.exists(os.path.join(ROOT, "scripts", "drive_log.py")),
        "load_test.py": os.path.exists(os.path.join(ROOT, "scripts", "load_test.py")),
        "robot_monitor.html": os.path.exists(os.path.join(ROOT, "tools", "robot_monitor.html")),
        "venv python": os.path.exists(os.path.join(ROOT, "BackEnd", "venv", "Scripts", "python.exe")),
    }
    st["cfg"] = CFG
    st["marks"] = len(MARKS)
    return st


# ── HTTP ────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):        # 콘솔을 조용하게
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200) -> None:
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            # 깨진 문자가 남아 있어도 화면이 통째로 죽지는 않게 한다
            body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/status":
            self._json(get_status())
        elif p.startswith("/api/job/"):
            jid = p.rsplit("/", 1)[-1]
            frm = 0
            if "?" in self.path:
                q = self.path.split("?", 1)[1]
                for kv in q.split("&"):
                    if kv.startswith("from="):
                        frm = int(kv[5:] or 0)
            j = JOBS.get(jid)
            if not j:
                self._json({"error": "no job"}, 404)
                return
            lines, total = j.tail(frm)
            self._json({"lines": lines, "total": total, "done": j.done,
                        "rc": j.rc, "title": j.title,
                        "elapsed": int(time.time() - j.started)})
        elif p == "/api/marks":
            self._json({"marks": MARKS[-50:], "count": len(MARKS),
                        "file": MARK_FILE["path"]})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        body = {}
        if n:
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                body = {}

        if p == "/api/mark":
            self._json(mark_now(body.get("note", "")))
            return

        if p == "/api/stop":
            j = JOBS.get(body.get("id", ""))
            if j:
                j.stop()
            self._json({"ok": True})
            return

        if p == "/api/run":
            what = body.get("what")
            spec = {
                "netcheck": ("망 측정", job_netcheck),
                "probe": ("사전 점검", job_probe),
                "collect": ("로그 수집", job_collect),
            }
            if what == "record":
                cycles = int(body.get("cycles") or 2)
                job = new_job("주행 기록")
                threading.Thread(target=self._wrap, args=(job, job_record, cycles),
                                 daemon=True).start()
                self._json({"id": job.id})
                return
            if what in spec:
                title, fn = spec[what]
                job = new_job(title)
                threading.Thread(target=self._wrap, args=(job, fn), daemon=True).start()
                self._json({"id": job.id})
                return
            self._json({"error": "unknown"}, 400)
            return

        self._send(404, b"not found", "text/plain")

    @staticmethod
    def _wrap(job: Job, fn, *a) -> None:
        try:
            fn(job, *a)
        except Exception as e:
            job.put("[오류] %s" % e)
        finally:
            job.done = True
            job.put("")
            job.put("— 끝 —")


def my_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


HTML = r"""<!doctype html>
<html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>현장 점검 콘솔</title>
<style>
:root{
  --bg:#fcfcfb; --surface:#fff; --line:#dcdcd8; --line-soft:#eaeae6;
  --ink:#1b1b19; --ink-2:#5c5c57; --ink-3:#8a8a83;
  --blue:#3d63dd; --blue-soft:#eef2fe;
  --green:#2f7d4f; --amber:#a8700a; --red:#c0392b;
  --mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#16161a; --surface:#1e1e24; --line:#32323a; --line-soft:#26262d;
  --ink:#f0f0ee; --ink-2:#b4b4ae; --ink-3:#85857e;
  --blue:#7b9bff; --blue-soft:#232a3d;
  --green:#5cc98a; --amber:#e0a43d; --red:#ef6b5c;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 -apple-system,"Segoe UI",system-ui,sans-serif}
header{padding:16px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:19px;margin:0;font-weight:650}
.sub{color:var(--ink-3);font-size:13px}
main{max-width:1000px;margin:0 auto;padding:16px}
.card{background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 12px;font-weight:650;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.num{display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:6px;background:var(--blue-soft);
  color:var(--blue);font-size:12px;font-weight:700;flex:none}
.hint{color:var(--ink-2);font-size:13px;margin:0 0 12px}
.hint b{color:var(--ink)}
button{font:inherit;font-weight:600;border-radius:9px;padding:11px 16px;
  border:1px solid var(--line);background:var(--surface);color:var(--ink);
  cursor:pointer}
button:hover:not(:disabled){border-color:var(--blue);color:var(--blue)}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
button.primary:hover:not(:disabled){filter:brightness(1.08);color:#fff}
button.big{font-size:19px;padding:22px;width:100%}
button.mark{background:var(--amber);border-color:var(--amber);color:#fff}
button.mark:hover:not(:disabled){filter:brightness(1.08);color:#fff}
button.danger{color:var(--red);border-color:var(--red)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
input[type=number]{font:inherit;width:70px;padding:10px;border-radius:8px;
  border:1px solid var(--line);background:var(--bg);color:var(--ink)}
pre{font-family:var(--mono);font-size:12.5px;line-height:1.55;
  background:var(--bg);border:1px solid var(--line-soft);border-radius:9px;
  padding:12px;max-height:340px;overflow:auto;white-space:pre-wrap;
  word-break:break-all;margin:12px 0 0}
table{border-collapse:collapse;width:100%;font-size:13.5px}
td{padding:6px 8px;border-bottom:1px solid var(--line-soft);vertical-align:top}
td:first-child{color:var(--ink-2);width:120px;white-space:nowrap}
.ok{color:var(--green);font-weight:600}
.bad{color:var(--red);font-weight:600}
.warn{color:var(--amber);font-weight:600}
.mono{font-family:var(--mono);font-size:12.5px}
.marks{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.chip{font-family:var(--mono);font-size:12px;padding:4px 9px;border-radius:99px;
  background:var(--blue-soft);color:var(--blue)}
.foot{color:var(--ink-3);font-size:12.5px;text-align:center;padding:20px 16px 40px}
@media(max-width:560px){main{padding:12px}.card{padding:13px}
  td:first-child{width:92px}}
</style>

<header>
  <h1>현장 점검 콘솔</h1>
  <span class="sub" id="hdr">읽기 전용 — 로봇에 명령을 보내지 않습니다</span>
</header>

<main>
  <div class="card">
    <h2><span class="num">1</span> 지금 상태
      <button id="refresh" style="margin-left:auto;padding:7px 12px;font-size:13px">새로고침</button>
    </h2>
    <table id="st"><tr><td>불러오는 중…</td><td></td></tr></table>
  </div>

  <div class="card">
    <h2><span class="num">2</span> 망 측정</h2>
    <p class="hint"><b>현장에서만 잴 수 있습니다.</b> 서버PC 를 빼오면 못 잽니다.
      ping 3종 + 경로 추적 + 네트워크 정보를 <span class="mono">_logs/망측정_&lt;시각&gt;/</span> 에 저장합니다.
      약 2분.</p>
    <p class="hint">멈칫의 원인이 <b>망</b>인지 <b>로봇</b>인지가 이 한 번으로 갈립니다.
      현장 실측 RTT 가 446 ms 인데, ping 이 200 ms 이상이면 망 문제로 확정됩니다.</p>
    <div class="row">
      <button class="primary" data-run="netcheck">망 측정 시작</button>
    </div>
    <pre id="out-netcheck" hidden></pre>
  </div>

  <div class="card">
    <h2><span class="num">3</span> 사전 점검</h2>
    <p class="hint"><b>주행 전에 반드시.</b> 30초.
      09-18 과 09-21 에 현장 기록이 <b>통째로 비어 있었습니다</b>
      (motion.jsonl 0~1줄, 사이클 폴더 0개).
      속도 데이터가 없는 상태로 "속도가 언제 떨어졌나"를 찾고 있었던 겁니다.</p>
    <p class="hint">수신율이 <b>하나라도 0%</b> 면 여기서 멈추세요. 주행해도 빈 데이터만 쌓입니다.</p>
    <div class="row">
      <button class="primary" data-run="probe">사전 점검 시작</button>
    </div>
    <pre id="out-probe" hidden></pre>
  </div>

  <div class="card">
    <h2><span class="num">4</span> 주행 기록</h2>
    <p class="hint">자동 감지는 "속도가 떨어졌다"만 압니다.
      <b>어느 게 불편한 정지인지는 사람만 압니다.</b>
      로봇을 따라다니며 아래 버튼을 누르세요 — 같은 망의 폰으로 이 화면을 열면 됩니다.</p>
    <div class="row" style="margin-bottom:12px">
      <span class="hint" style="margin:0">사이클</span>
      <input type="number" id="cycles" value="2" min="1" max="20">
      <button class="primary" data-run="record">기록 시작</button>
      <button class="danger" id="stop-record" hidden>기록 중지</button>
    </div>
    <button class="big mark" id="mark">멈칫 &mdash; 지금 불편하게 섰다</button>
    <div class="marks" id="marks"></div>
    <pre id="out-record" hidden></pre>
  </div>

  <div class="card">
    <h2><span class="num">5</span> 부하 모니터</h2>
    <p class="hint">속도·부하·잭 상태를 실시간으로 봅니다. 별도 창으로 열립니다.
      먼저 <b>부하 0</b> 으로 몇 바퀴 돌려 기준선을 잡고,
      <span class="mono">load average</span> 경고가 <b>실제로 뜨는지</b> 보세요.
      사무실 crawler 에서는 3단계에서도 0건이었습니다.</p>
    <div class="row"><button id="monitor">부하 모니터 열기</button></div>
  </div>

  <div class="card">
    <h2><span class="num">6</span> 로그 수집</h2>
    <p class="hint">주행 기록과 백엔드 로그를 zip 하나로 묶습니다.
      <b>나올 때 이 zip 과 망측정 폴더를 챙기세요.</b> 그 둘이면 밖에서 분석됩니다.</p>
    <div class="row"><button data-run="collect">zip 만들기</button></div>
    <pre id="out-collect" hidden></pre>
  </div>

  <p class="foot">
    로봇에 이동·잭·속도 명령을 보내지 않습니다. 조회와 기록뿐입니다.<br>
    이 창을 닫으면 서버가 계속 돕니다 — 검은 창에서 Ctrl+C 로 종료하세요.
  </p>
</main>

<script>
const $ = s => document.querySelector(s);
const jobs = {};          // what -> {id, from, timer}

async function api(path, body){
  const o = body ? {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify(body)} : {};
  const r = await fetch(path, o);
  return r.json();
}

// ── 상태 ──
function cell(v, good){
  const cls = good === undefined ? '' : (good ? 'ok' : 'bad');
  return `<span class="${cls}">${v}</span>`;
}
async function loadStatus(){
  const t = $('#st');
  try{
    const s = await api('/api/status');
    const f = s.files || {};
    const area = s.area ? `area_id ${s.area.area_id}` : '조회 실패';
    const rmap = s.robot_map ? `id ${s.robot_map.id} · ${s.robot_map.name||''}` : '조회 실패';
    const okBack = /^OK/.test(s.backend||'');
    const okRobot = !/없음|실패/.test(s.robot||'');
    const miss = Object.entries(f).filter(([,v])=>!v).map(([k])=>k);
    t.innerHTML = `
      <tr><td>시각</td><td class="mono">${s.time}</td></tr>
      <tr><td>로봇</td><td>${cell(s.robot, okRobot)} <span class="mono" style="color:var(--ink-3)">${s.cfg.ROBOT_IP}</span></td></tr>
      <tr><td>백엔드</td><td>${cell(s.backend, okBack)}</td></tr>
      <tr><td>현재 맵 영역</td><td>${area}</td></tr>
      <tr><td>로봇이 쓰는 맵</td><td>${rmap}</td></tr>
      <tr><td>git 브랜치</td><td class="mono">${s.git_branch||'-'}</td></tr>
      <tr><td>최근 커밋</td><td class="mono" style="font-size:12px">${(s.git_log||'-').replace(/\n/g,'<br>')}</td></tr>
      <tr><td>필요한 파일</td><td>${miss.length ? cell('없음: '+miss.join(', '), false) : cell('전부 있음', true)}</td></tr>
      <tr><td>멈칫 표시</td><td>${s.marks} 건</td></tr>`;
  }catch(e){
    t.innerHTML = `<tr><td>오류</td><td class="bad">서버에 못 붙었습니다</td></tr>`;
  }
}

// ── 작업 실행 ──
function outEl(what){ return $('#out-'+what); }

async function start(what, extra){
  const btn = document.querySelector(`[data-run="${what}"]`);
  const pre = outEl(what);
  if(btn) btn.disabled = true;
  pre.hidden = false; pre.textContent = '시작하는 중…\n';
  const r = await api('/api/run', Object.assign({what}, extra||{}));
  if(!r.id){ pre.textContent = '시작 실패'; if(btn) btn.disabled=false; return; }
  jobs[what] = {id:r.id, from:0};
  if(what === 'record'){ $('#stop-record').hidden = false; }
  poll(what);
}

async function poll(what){
  const j = jobs[what]; if(!j) return;
  const pre = outEl(what);
  try{
    const r = await api(`/api/job/${j.id}?from=${j.from}`);
    if(r.lines && r.lines.length){
      if(j.from === 0) pre.textContent = '';
      pre.textContent += r.lines.join('\n') + '\n';
      pre.scrollTop = pre.scrollHeight;
      j.from = r.total;
    }
    if(r.done){
      const btn = document.querySelector(`[data-run="${what}"]`);
      if(btn) btn.disabled = false;
      if(what === 'record') $('#stop-record').hidden = true;
      delete jobs[what];
      loadStatus();
      return;
    }
  }catch(e){}
  setTimeout(()=>poll(what), 900);
}

document.querySelectorAll('[data-run]').forEach(b=>{
  b.onclick = ()=>{
    const what = b.dataset.run;
    if(what === 'record') start(what, {cycles: +$('#cycles').value || 2});
    else start(what);
  };
});

$('#stop-record').onclick = async ()=>{
  const j = jobs['record']; if(!j) return;
  await api('/api/stop', {id:j.id});
};

// ── 멈칫 표시 ──
$('#mark').onclick = async ()=>{
  const r = await api('/api/mark', {note:''});
  const d = document.createElement('span');
  d.className = 'chip'; d.textContent = r.ts;
  $('#marks').prepend(d);
  const btn = $('#mark');
  const old = btn.textContent;
  btn.textContent = '기록했습니다  ' + r.ts;
  setTimeout(()=>{ btn.textContent = old; }, 1100);
};

$('#monitor').onclick = ()=>{
  window.open('/../tools/robot_monitor.html', '_blank');
  alert('브라우저가 막으면 tools\\robot_monitor.html 을 직접 여세요.');
};

$('#refresh').onclick = loadStatus;
loadStatus();
setInterval(()=>{ if(!Object.keys(jobs).length) loadStatus(); }, 15000);
</script>
</html>
"""


def main() -> None:
    load_config()
    os.makedirs(LOGS, exist_ok=True)
    ip = my_ip()
    print("=" * 60)
    print(" 현장 점검 콘솔")
    print("=" * 60)
    print("")
    print("  이 PC       :  http://127.0.0.1:%d" % PORT)
    print("  폰/태블릿   :  http://%s:%d" % (ip, PORT))
    print("")
    print("  로봇        :  %s" % CFG["ROBOT_IP"])
    print("  백엔드      :  %s" % CFG["BACKEND"])
    print("  파이썬      :  %s" % PY)
    print("")
    print("  종료는 이 창에서 Ctrl+C")
    print("=" * 60)

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        webbrowser.open("http://127.0.0.1:%d" % PORT)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        for j in list(JOBS.values()):
            j.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
