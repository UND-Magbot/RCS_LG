#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""현장 점검 콘솔 — bat 여러 개를 웹 화면 하나로 묶은 것.

    BackEnd\\venv\\Scripts\\python.exe scripts\\field_console.py
    또는  현장작업\\현장콘솔.bat  더블클릭

왜 만들었나
  현장은 방진복을 입고 들어가는 생산라인 내부라 왕복 비용이 크다.
  bat 을 8개 왔다갔다 하면서 창을 번갈아 보는 것보다, 한 화면에서
  순서대로 누르는 편이 빠르고 빠뜨릴 일이 없다.

  ★ 무엇보다 **[비이상적 정지] 표시 버튼을 폰으로 누를 수 있다.**
    종전에는 drive_log 터미널에 포커스를 두고 스페이스바를 쳐야 했는데,
    로봇을 따라다니며 그러기는 어렵다. 같은 망의 폰에서 이 화면을 열면
    로봇 옆을 걸으며 누를 수 있다.

무엇을 하나
  1) 상태      git · 백엔드 · 로봇 응답 · 현재 맵 영역
  2) 망 측정   ping 3종 + tracert + ipconfig  ★ 현장에서만 잴 수 있다
  3) 사전 점검 drive_log --probe. 로그가 실제로 남는지 먼저 본다
  4) 로그 기록 시작 한 번 / 종료·추출 한 번 (field_session) + 비이상적 정지 표시
     종료·추출이 서버 로그 · 알람 · 로봇 bag 까지 zip 하나로 묶는다

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
    """명령 하나를 돌리며 출력을 job 에 넣는다.

    ★ 파이썬을 부를 때는 -u 를 붙인다.
      파이프로 받으면 블록 버퍼링이 걸려서, 스크립트가 출력해도 화면에
      한참 뒤에야(또는 끝나야) 나온다. drive_log 는 print 96곳 중 22곳만
      flush=True 라 -u 가 없으면 "멈춘 것처럼" 보인다.
    """
    if args and args[0].lower().endswith("python.exe") and "-u" not in args:
        args = [args[0], "-u"] + args[1:]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="cp949")
    try:
        p = subprocess.Popen(
            args, cwd=cwd or ROOT, env=env,
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
        job.put("[%d/5] %s (%s) ping 20회 … (응답이 없으면 최대 25초)" % (i, name, ip))
        try:
            # -w 1000: 한 번에 1초까지만 기다린다.
            # 기본값(4초)이면 응답 없을 때 한 대상에 80초, 세 대상이면 4분이 넘는다.
            # 현장에서 정상 응답하면 이 값은 영향이 없다.
            out = dec(subprocess.run(
                ["ping", ip, "-n", "20", "-w", "1000"], capture_output=True, timeout=60,
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
            ["tracert", "-d", "-h", "10", "-w", "1000", CFG["ROBOT_IP"]],
            capture_output=True, timeout=120,
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


# ── 3) 로그 세션 (2026-09-29) ───────────────────────────────────
#   종전 [주행 기록]·[표시]·[로그 수집] 을 scripts/field_session.py 하나로 합쳤다.
#   시작 한 번 → 주행 기록기·망 상시 기록·시작 상태가 같이 돈다.
#   종료 한 번 → 서버 로그 구간·DB 로그·로봇 bag 까지 zip 하나로 나온다.
#   L1/L2 bat 과 같은 함수를 부르므로 어느 쪽으로 시작하고 끝내도 된다.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import field_session  # noqa: E402


def job_session_start(job: Job) -> None:
    r = field_session.start(log=job.put)
    if not r.get("ok"):
        job.put(r.get("msg", "시작 실패"))


def job_session_stop(job: Job) -> None:
    r = field_session.stop(log=job.put)
    if not r.get("ok"):
        job.put(r.get("msg", "종료 실패"))


def job_session_bags(job: Job) -> None:
    r = field_session.bags(log=job.put)
    if not r.get("ok"):
        job.put(r.get("msg", "실패"))


# ── 5) 상태 ─────────────────────────────────────────────────────
def get_status() -> dict:
    # config.bat 을 고쳤을 때 [새로고침]만으로 반영되게 매번 다시 읽는다.
    # (현장에서 로봇 IP 가 다르면 콘솔을 재시작해야 하는 게 번거롭다)
    load_config()
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
    st["session"] = field_session.status()
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
        elif p == "/monitor":
            # tools/robot_monitor.html 을 이 서버가 직접 서빙한다.
            # file:// 로 열면 폰에서는 아예 못 열고, 브라우저마다 동작이 갈린다.
            mp = os.path.join(ROOT, "tools", "robot_monitor.html")
            if os.path.exists(mp):
                self._send(200, open(mp, "rb").read(), "text/html; charset=utf-8")
            else:
                msg = ("tools/robot_monitor.html 이 없습니다.\n\n"
                       "받는 법:\n"
                       "  git fetch origin\n"
                       "  git checkout origin/feature/lg_luke -- tools/robot_monitor.html\n")
                self._send(404, msg.encode("utf-8"), "text/plain; charset=utf-8")
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
        elif p == "/api/session":
            self._json(field_session.status())
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
            self._json(field_session.mark(body.get("note", ""), "폰·웹"))
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
                "sstart": ("기록 시작", job_session_start),
                "sstop": ("종료·추출", job_session_stop),
                "sbags": ("bag 다시 받기", job_session_bags),
            }
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

  <div class="card" id="sess-card">
    <h2><span class="num">2</span> 로그 기록 &mdash; 시작 한 번, 종료 한 번
      <span id="sess-badge" class="chip" style="margin-left:auto">확인 중…</span></h2>
    <p class="hint"><b>[기록 시작]</b> 을 누르면 주행 기록 · 망 상시 기록(ping 1초) · 시작 상태가 한꺼번에 켜집니다.
      검은 창(주행 기록기)이 하나 열리는데 <b>닫지 마세요.</b></p>
    <p class="hint"><b>[종료·추출]</b> 을 누르면 서버 로그 · 알람 · 로봇 bag 까지 <b>zip 하나</b>로 나옵니다.
      <span class="mono">로그추출\</span> 폴더. 나올 때 그 zip 하나만 챙기면 됩니다.</p>
    <div class="row" style="margin-bottom:12px">
      <button class="primary" data-run="sstart">기록 시작</button>
      <button class="danger" data-run="sstop">종료·추출</button>
      <button data-run="sbags" title="마지막 세션의 로봇 bag 을 이어받고 zip 을 갱신">bag 다시 받기</button>
    </div>
    <div class="row" style="margin-bottom:8px">
      <input id="note" placeholder="메모 (선택) — 예: 랙 앞, 코너" style="flex:1;min-width:160px;font:inherit;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)">
    </div>
    <button class="big mark" id="mark">비이상적 정지 &mdash; 지금 불편하게 섰다</button>
    <p class="hint" style="margin:10px 0 0">서버PC 앞이면 검은 창에서 <b>스페이스바</b>, 로봇 옆이면 폰으로 이 버튼.
      둘 다 같은 파일에 모이고 스냅샷도 똑같이 찍힙니다.</p>
    <div class="marks" id="marks"></div>
    <pre id="out-sstart" hidden></pre>
    <pre id="out-sstop" hidden></pre>
    <pre id="out-sbags" hidden></pre>
  </div>

  <div class="card">
    <h2><span class="num">3</span> 망 측정 (1회)</h2>
    <p class="hint"><b>현장에서만 잴 수 있습니다.</b> 서버PC 를 빼오면 못 잽니다.
      ping 3종 + 경로 추적 + 네트워크 정보를 <span class="mono">_logs/망측정_&lt;시각&gt;/</span> 에 저장합니다.
      약 2분.</p>
    <p class="hint">비이상적 정지(급감속)의 원인이 <b>망</b>인지 <b>로봇</b>인지가 이 한 번으로 갈립니다.
      현장 실측 RTT 가 446 ms 인데, ping 이 200 ms 이상이면 망 문제로 확정됩니다.</p>
    <div class="row">
      <button class="primary" data-run="netcheck">망 측정 시작</button>
    </div>
    <pre id="out-netcheck" hidden></pre>
  </div>

  <div class="card">
    <h2><span class="num">4</span> 사전 점검</h2>
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
    <h2><span class="num">5</span> 부하 모니터</h2>
    <p class="hint">속도·부하·잭 상태를 실시간으로 봅니다. 별도 창으로 열립니다.
      먼저 <b>부하 0</b> 으로 몇 바퀴 돌려 기준선을 잡고,
      <span class="mono">load average</span> 경고가 <b>실제로 뜨는지</b> 보세요.
      사무실 crawler 에서는 3단계에서도 0건이었습니다.</p>
    <div class="row"><button id="monitor">부하 모니터 열기</button></div>
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
      <tr><td>로그 기록</td><td>${s.session && s.session.active ? cell('기록 중 ('+s.session.start+' 시작)', true) : '안 함'}</td></tr>`;
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
      delete jobs[what];
      loadStatus(); loadSession();
      return;
    }
  }catch(e){}
  setTimeout(()=>poll(what), 900);
}

document.querySelectorAll('[data-run]').forEach(b=>{
  b.onclick = ()=>{
    const what = b.dataset.run;
    if(what === 'sstop' && !confirm('기록을 끝내고 로그를 추출할까요?\n로봇 bag 을 받느라 몇 분 걸릴 수 있습니다.')) return;
    ['sstart','sstop','sbags'].forEach(w=>{ if(w!==what) outEl(w).hidden = true; });
    start(what);
  };
});

// ── 세션 상태 ──
async function loadSession(){
  try{
    const s = await api('/api/session');
    const bd = $('#sess-badge');
    if(s.active){
      bd.textContent = `기록 중 · ${s.start} 시작 · ${s.elapsed} · 표시 ${s.marks}건`
        + (s.drive_log ? '' : ' · ⚠ 주행 기록기 꺼짐');
      bd.style.background = 'var(--red)'; bd.style.color = '#fff';
    }else{
      bd.textContent = '기록 안 함' + (s.last_zip ? ' · 마지막 zip 있음' : '');
      bd.style.background = ''; bd.style.color = '';
    }
    $('[data-run="sstart"]').disabled = !!s.active || !!jobs['sstart'];
    $('[data-run="sstop"]').disabled = !s.active || !!jobs['sstop'];
    $('#mark').disabled = !s.active;
  }catch(e){}
}

// ── 비이상적 정지 표시 ──
$('#mark').onclick = async ()=>{
  const r = await api('/api/mark', {note: $('#note').value.trim()});
  const btn = $('#mark');
  const old = btn.textContent;
  if(!r.ok){ btn.textContent = r.msg || '실패'; setTimeout(()=>{ btn.textContent = old; }, 1500); return; }
  const d = document.createElement('span');
  d.className = 'chip'; d.textContent = r.ts + (r.note ? ' ' + r.note : '');
  $('#marks').prepend(d);
  $('#note').value = '';
  btn.textContent = `기록했습니다  ${r.ts}  (${r.count}건째)`;
  setTimeout(()=>{ btn.textContent = old; }, 1100);
  loadSession();
};

$('#monitor').onclick = ()=>{
  window.open('/monitor', '_blank');
};

$('#refresh').onclick = ()=>{ loadStatus(); loadSession(); };
loadStatus(); loadSession();
setInterval(loadSession, 5000);
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
