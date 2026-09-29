#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""로봇이 자동 녹화한 주행 로그(bag)를 받아온다 — **읽기 전용**.

    python scripts/fetch_bags.py --ip 10.14.182.126
    python scripts/fetch_bags.py --ip 10.14.182.126 --date 2026-09-28 --from 18:00 --to 19:30

    ★ --date/--from/--to 는 **한국 시각(이 PC 시각)** 으로 준다.

무엇인가
  AutoXing 로봇은 주행 로그를 **10분 단위 rosbag 으로 계속 녹화**한다.
  화면의 파란 버튼(→고객센터)을 누르지 않아도 이미 저장돼 있다.
  용량이 차면 오래된 것부터 지워지므로, 필요한 시간대를 빨리 받아둬야 한다.

  2026-09-22 확인 — 로봇 한 대에 453개 3.0GB, 8일치 보관 중이었다.

★ 2026-09-29 — bag 파일명은 한국 시각이 아니다
  파일명의 시각은 **로봇의 시간대**로 붙는다. 로봇마다 다르다.
      사무실 crawler  …_2026-09-23_09-00-00.bag → 실제 KST 10:00  (UTC+8)
      현장 longjack   18시 이후 작업분이 09~12시 파일에 있었다      (UTC 추정)
  종전에는 파일명을 한국 시각으로 보고 걸렀기 때문에, 2026-09-28 현장에서
  "18시 이후 bag 이 없다" 고 판단해 못 받았다(실제로는 09~12시 이름으로 있었다).

  그래서 이제는 **bag 안의 실제 기록 시각(epoch)** 을 읽어 로봇 시간대를 알아낸다.
    rosbag v2 는 파일 앞 4KB 에 index_pos 가, 파일 끝 인덱스에 chunk 별
    start/end time 이 있다. HTTP Range 로 그 두 조각만 받으므로 수 KB 로 끝난다.
  알아낸 시간대는 _logs/bags/bag_tz.json 에 로봇별로 기억한다.

★ 로봇에 아무 명령도 보내지 않는다. 목록 조회와 파일 다운로드뿐이다.

받은 파일은 _logs/bags/ 에 저장된다. 이어받기를 지원하므로 중간에 끊겨도
다시 실행하면 이어서 받는다.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTDIR = os.path.join(ROOT, "_logs", "bags")
TZ_CACHE = os.path.join(OUTDIR, "bag_tz.json")

BAG_SEC = 600            # 로봇은 10분 단위로 끊는다
TS = re.compile(r"_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.bag$")


# ── 목록 ────────────────────────────────────────────────────────
def fetch_list(ip: str, timeout: float = 30.0) -> list[dict]:
    url = "http://%s:8090/bags/" % ip
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def name_clock(fn: str):
    """파일명의 시각을 '시간대 없는 벽시계' 로 읽어 UTC 로 가정한 epoch 로 돌려준다.

    실제 epoch = name_clock - tz_hours*3600  (tz_hours 는 로봇 시간대, UTC+8 이면 8)
    """
    m = TS.search(fn)
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    return calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0))


# ── bag 내부 시각 읽기 (Range 로 조각만) ─────────────────────────
def _fields(h: bytes) -> dict:
    d, i = {}, 0
    while i + 4 <= len(h):
        n = struct.unpack_from("<I", h, i)[0]
        i += 4
        f = h[i:i + n]
        i += n
        if b"=" in f:
            k, v = f.split(b"=", 1)
            d[k.decode("ascii", "replace")] = v
    return d


def _range(url: str, lo: int, hi: int | None = None, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("Range", "bytes=%d-%s" % (lo, "" if hi is None else hi))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        # Range 를 무시하고 전체를 주는 서버면 잘라 쓴다
        if r.status == 200 and lo:
            data = data[lo:] if hi is None else data[lo:hi + 1]
        return data


def _chunk_times(buf: bytes, p: int) -> tuple[float, float] | None:
    """buf[p:] 부터 레코드를 훑어 chunk info(op=0x06) 의 시각 범위를 모은다."""
    lo = hi = None
    while p + 4 <= len(buf):
        hl = struct.unpack_from("<I", buf, p)[0]
        if p + 4 + hl + 4 > len(buf):
            break
        h = _fields(buf[p + 4:p + 4 + hl])
        p += 4 + hl
        dl = struct.unpack_from("<I", buf, p)[0]
        p += 4 + dl
        if h.get("op") == b"\x06" and "start_time" in h and "end_time" in h:
            s, sn = struct.unpack("<II", h["start_time"])
            e, en = struct.unpack("<II", h["end_time"])
            s, e = s + sn / 1e9, e + en / 1e9
            lo = s if lo is None else min(lo, s)
            hi = e if hi is None else max(hi, e)
    return (lo, hi) if lo is not None else None


def bag_times_url(url: str) -> tuple[float, float] | None:
    """원격 bag 의 실제 기록 구간(epoch). 녹화 중이라 인덱스가 없으면 None."""
    head = _range(url, 0, 4095)
    if not head.startswith(b"#ROSBAG V2.0\n"):
        return None
    p = 13
    hl = struct.unpack_from("<I", head, p)[0]
    h = _fields(head[p + 4:p + 4 + hl])
    ip = h.get("index_pos")
    if not ip:
        return None
    index_pos = struct.unpack("<Q", ip)[0]
    if index_pos == 0:                     # 아직 녹화 중 — 인덱스가 안 써졌다
        return None
    return _chunk_times(_range(url, index_pos), 0)


def bag_times_file(path: str) -> tuple[float, float] | None:
    """받은 bag 파일의 실제 기록 구간(epoch)."""
    with open(path, "rb") as f:
        head = f.read(4096)
        if not head.startswith(b"#ROSBAG V2.0\n"):
            return None
        hl = struct.unpack_from("<I", head, 13)[0]
        h = _fields(head[17:17 + hl])
        ip = h.get("index_pos")
        if not ip:
            return None
        index_pos = struct.unpack("<Q", ip)[0]
        if index_pos == 0:
            return None
        f.seek(index_pos)
        return _chunk_times(f.read(), 0)


# ── 로봇 시간대 알아내기 ─────────────────────────────────────────
def _load_tz_cache() -> dict:
    try:
        return json.load(open(TZ_CACHE, encoding="utf-8"))
    except Exception:
        return {}


def _save_tz_cache(c: dict) -> None:
    try:
        os.makedirs(os.path.dirname(TZ_CACHE), exist_ok=True)
        json.dump(c, open(TZ_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def detect_tz(ip: str, items: list[dict], log=print) -> tuple[float | None, str]:
    """로봇의 파일명 시간대(시간 단위)를 돌려준다. (값, 근거 문장)

    1순위  완성된 최근 bag 의 내부 시각과 파일명을 비교 (정확)
    2순위  지난번에 알아낸 값 (bag_tz.json)
    3순위  가장 최근 파일명과 지금 시각의 차이 (녹화가 계속되고 있을 때만 맞다)
    """
    rows = sorted(((name_clock(it.get("filename", "")), it) for it in items
                   if name_clock(it.get("filename", "")) is not None),
                  key=lambda x: x[0], reverse=True)
    for nc, it in rows[:4]:
        url = it.get("download_url")
        if not url:
            continue
        try:
            tr = bag_times_url(url)
        except Exception:
            tr = None
        if not tr:
            continue
        tz = round((nc - tr[0]) / 1800.0) / 2.0        # 30분 단위까지 허용
        c = _load_tz_cache()
        c[ip] = {"tz_hours": tz, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "by": it.get("filename")}
        _save_tz_cache(c)
        return tz, "bag 내부 시각으로 확인 (%s)" % it.get("filename")
    c = _load_tz_cache().get(ip)
    if c and c.get("tz_hours") is not None:
        return float(c["tz_hours"]), "지난번 확인값 사용 (%s)" % c.get("at")
    if rows:
        tz = round((rows[0][0] - time.time() + BAG_SEC / 2) / 3600.0)
        return float(tz), "추정 — 가장 최근 파일명과 현재 시각 비교"
    return None, "알 수 없음"


def real_start(fn: str, tz: float) -> float | None:
    nc = name_clock(fn)
    return None if nc is None else nc - tz * 3600


def kst(t: float, fmt: str = "%m-%d %H:%M") -> str:
    return time.strftime(fmt, time.localtime(t))


# ── 다운로드 ────────────────────────────────────────────────────
def download(url: str, dest: str, size: int, log=print) -> bool:
    """이어받기 지원 다운로드. 이미 다 받았으면 건너뛴다."""
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    if size and have >= size:
        log("      이미 있음 (%.1f MB)" % (have / 1048576))
        return True
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", "bytes=%d-" % have)
        log("      이어받기 %.1f MB 부터" % (have / 1048576))
    t0 = time.time()
    last_note = 0.0
    try:
        with urllib.request.urlopen(req, timeout=60) as r, \
                open(dest, "ab" if have else "wb") as f:
            got = have
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if size and log is print:
                    sys.stdout.write("\r      %5.1f%%  %6.1f MB  %4.1f MB/s   "
                                     % (got / size * 100, got / 1048576,
                                        (got - have) / 1048576 / max(time.time() - t0, .01)))
                    sys.stdout.flush()
                elif size and time.time() - last_note > 10:
                    last_note = time.time()
                    log("      %5.1f%%  %6.1f MB" % (got / size * 100, got / 1048576))
    except urllib.error.HTTPError as e:
        log("\n      [실패] HTTP %s" % e.code)
        return False
    except Exception as e:
        log("\n      [실패] %s" % str(e)[:80])
        return False
    if log is print:
        print("")
    log("      완료  %.1f MB" % (os.path.getsize(dest) / 1048576))
    return True


def fetch_range(ip: str, t0: float, t1: float, out: str, log=print) -> dict:
    """[t0, t1] (epoch) 에 걸치는 bag 을 전부 받는다. 세션 추출기가 쓴다.

    돌려주는 것
      ok        받은 파일 목록 [{file, start, end}]  (start/end 는 내부 실제 시각)
      recording 아직 녹화 중이라 못 받은 구간이 있으면 그 bag 의 실제 시작 시각
      covered_to 받은 bag 들이 덮는 마지막 시각
      tz, tz_how, error
    """
    res: dict = {"ok": [], "fail": [], "recording": None, "covered_to": None,
                 "tz": None, "tz_how": "", "error": None}
    try:
        items = fetch_list(ip)
    except Exception as e:
        res["error"] = "목록 조회 실패: %s" % str(e)[:100]
        return res
    tz, how = detect_tz(ip, items, log)
    res["tz"], res["tz_how"] = tz, how
    if tz is None:
        res["error"] = "로봇 시간대를 알 수 없음"
        return res
    log("  로봇 bag 시간대: UTC%+g  (%s)" % (tz, how))

    pick = []
    for it in items:
        s = real_start(it.get("filename", ""), tz)
        if s is not None and s + BAG_SEC > t0 and s < t1:
            pick.append((s, it))
    pick.sort(key=lambda x: x[0])
    if not pick:
        res["error"] = "그 시간대(%s ~ %s)에 bag 이 없음" % (kst(t0), kst(t1))
        return res

    os.makedirs(out, exist_ok=True)
    now = time.time()
    for i, (s, it) in enumerate(pick, 1):
        fn = it["filename"]
        if s + BAG_SEC > now - 20:             # 아직 녹화 중일 수 있다
            try:
                tr = bag_times_url(it["download_url"])
            except Exception:
                tr = None
            if tr is None:
                log("  [%d/%d] %s — 녹화 중이라 건너뜀 (KST %s~)" % (i, len(pick), fn, kst(s)))
                res["recording"] = s if res["recording"] is None else min(res["recording"], s)
                continue
        log("  [%d/%d] %s  (KST %s ~ %s)" % (i, len(pick), fn, kst(s), kst(s + BAG_SEC, "%H:%M")))
        dest = os.path.join(out, fn)
        if download(it["download_url"], dest, it.get("size_bytes", 0), log):
            try:
                tr = bag_times_file(dest)
            except Exception:
                tr = None
            if tr is None:
                # 인덱스가 없다 = 녹화가 덜 끝난 파일. 받은 것으로 치지 않고 지운다
                # (남겨 두면 다음에 '이미 있음' 으로 건너뛰어 반쪽 파일이 남는다)
                log("      녹화가 덜 끝난 파일이라 버림 — 나중에 다시 받습니다")
                try:
                    os.remove(dest)
                except Exception:
                    pass
                res["recording"] = s if res["recording"] is None else min(res["recording"], s)
                continue
            st, en = tr
            res["ok"].append({"file": fn, "start": st, "end": en,
                              "size_mb": round(os.path.getsize(dest) / 1048576, 1)})
            res["covered_to"] = en if res["covered_to"] is None else max(res["covered_to"], en)
        else:
            res["fail"].append(fn)
    return res


# ── CLI ─────────────────────────────────────────────────────────
def to_min(s: str) -> int:
    s = s.strip().replace("：", ":")
    if ":" not in s:
        raise ValueError("HH:MM 형식으로 주세요 (예: 15:50)")
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", required=True, help="로봇 IP (현장 10.14.182.126)")
    ap.add_argument("--date", default="", help="YYYY-MM-DD (한국 시각). 생략하면 목록만")
    ap.add_argument("--from", dest="frm", default="", help="시작 시각 HH:MM (한국 시각)")
    ap.add_argument("--to", dest="to", default="", help="끝 시각 HH:MM (한국 시각)")
    ap.add_argument("--out", default=OUTDIR)
    a = ap.parse_args()

    print("로봇 %s 에서 bag 목록을 받습니다…" % a.ip)
    try:
        items = fetch_list(a.ip)
    except Exception as e:
        print("실패: %s" % e)
        print("로봇과 같은 망에 있는지 확인하세요.")
        return 1

    tz, how = detect_tz(a.ip, items)
    if tz is None:
        print("bag 이 없습니다.")
        return 1
    print("  로봇 파일명 시간대: UTC%+g  (%s)" % (tz, how))
    print("  아래 시각은 전부 **한국 시각**으로 바꿔 보여줍니다.")

    rows = []
    for it in items:
        s = real_start(it.get("filename", ""), tz)
        if s is not None:
            rows.append((s, it))
    rows.sort(key=lambda x: x[0])
    total = sum(it.get("size_bytes", 0) for _, it in rows)
    print("  %d개 · %.1f GB" % (len(rows), total / 1073741824))

    days: dict[str, list] = {}
    for s, it in rows:
        days.setdefault(kst(s, "%Y-%m-%d"), []).append(s)
    print()
    print("  날짜(KST)   개수   시간대(KST)")
    for d in sorted(days):
        ss = days[d]
        print("  %s  %3d개   %s ~ %s" % (d, len(ss), kst(min(ss), "%H:%M"),
                                          kst(max(ss) + BAG_SEC, "%H:%M")))

    if not a.date:
        print()
        print("받으려면 --date 와 --from/--to 를 한국 시각으로 주세요. 예:")
        print("  python scripts/fetch_bags.py --ip %s --date %s --from 15:50 --to 16:30"
              % (a.ip, sorted(days)[-1]))
        return 0

    base = time.mktime(time.strptime(a.date, "%Y-%m-%d"))
    t0 = base + (to_min(a.frm) * 60 if a.frm else 0)
    t1 = base + (to_min(a.to) * 60 if a.to else 86400)
    print()
    print("=" * 60)
    print("  받을 구간 (KST) %s ~ %s" % (kst(t0), kst(t1)))
    print("=" * 60)

    # bag 은 로봇 시계 기준 — 서버 로그와 겹쳐 보려면 두 시계 차이도 남긴다
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import clock_sync as _clock
        _rec = _clock.measure(a.ip, precise=True)
        _rec["source"] = "fetch_bags"
        print(_clock.describe(_rec))
        os.makedirs(a.out, exist_ok=True)
        _clock.append_log(_rec, os.path.join(a.out, "clock_offset.jsonl"))
        _clock.append_log(_rec)
        print()
    except Exception as _e:                             # noqa: BLE001
        print("  [경고] 시계 차이 측정 생략: %s\n" % _e)

    r = fetch_range(a.ip, t0, t1, a.out)
    print()
    print("=" * 60)
    if r["error"]:
        print("  " + r["error"])
    print("  %d개 받았습니다%s" % (len(r["ok"]), (" · 실패 %d" % len(r["fail"])) if r["fail"] else ""))
    if r["recording"]:
        print("  %s 이후는 아직 녹화 중입니다. 10분쯤 뒤 다시 실행하면 이어서 받습니다."
              % kst(r["recording"]))
    print("  저장 위치: %s" % a.out)
    print("=" * 60)
    return 0 if r["ok"] and not r["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
