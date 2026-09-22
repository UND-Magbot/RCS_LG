<#
  RCS LG 현장 종합 진단 (2026-08-26)

  목적 — "로봇이 관제에 온라인으로 안 뜬다"의 원인을 다음 4개 중 하나로 확정한다.
      (A) 로봇이 라우터에 안 붙어 있거나 IP 불일치   → 우리 문제, 현장에서 해결
      (B) 포트포워딩 규칙 문제                        → 우리 문제, 현장에서 해결
      (C) MTU 블랙홀 (작은 패킷 O, 큰 응답 X)        → 우리 문제, 라우터에서 해결
      (D) U+ 전용망 인바운드 TCP 차단                → LG/U+ 협의 대상

  사용법
      [1단계] 노트북을 로봇 라우터 WiFi(TC700_5170086)에 붙이고
              .\field_diag.ps1 -Mode Robot

      [2단계] 노트북을 서버 라우터에 유선 연결하고 (WiFi 끌 것)
              .\field_diag.ps1 -Mode Server

  인터넷 없이 동작한다 (Windows 내장 curl / ping 만 사용).
  실행 결과는 tools\진단결과_날짜_시각.txt 로 자동 저장된다.
#>
param(
    [ValidateSet("Robot", "Server")]
    [string]$Mode = "Server",

    # 로봇 라우터 WAN 고정 IP (2단계에서 찌를 대상)
    [string]$RobotWan = "10.115.244.12",

    # 서버 라우터 WAN 고정 IP
    [string]$ServerWan = "10.115.244.11",

    # 로봇 사설 IP (1단계에서 확인할 기대값 = 포워딩 대상)
    [string]$RobotLan = "192.168.39.100",

    # 관제 백엔드 포트
    [int]$ServerPort = 8002
)

$ErrorActionPreference = "Continue"
$curl = "$env:SystemRoot\System32\curl.exe"

# ── 결과 로그 파일 ──
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$log = Join-Path $PSScriptRoot ("진단결과_" + $Mode + "_" + $stamp + ".txt")
try { Start-Transcript -Path $log -Force | Out-Null } catch { }

function Head($t) { Write-Host ""; Write-Host ("===== " + $t + " =====") -ForegroundColor Cyan }
function OK($t)   { Write-Host ("   [OK]   " + $t) -ForegroundColor Green }
function NG($t)   { Write-Host ("   [FAIL] " + $t) -ForegroundColor Red }
function Note($t) { Write-Host ("   " + $t) -ForegroundColor DarkGray }

$verdict = New-Object System.Collections.ArrayList

Write-Host ""
Write-Host "###########################################################" -ForegroundColor Yellow
Write-Host ("  RCS LG 현장 진단   모드: " + $Mode + "   " + (Get-Date -Format "yyyy-MM-dd HH:mm")) -ForegroundColor Yellow
Write-Host "###########################################################" -ForegroundColor Yellow

# ─────────────────────────────────────────────────────────────
# 0. 내 PC 네트워크 — 어디에 붙어 있는지부터 확정한다
# ─────────────────────────────────────────────────────────────
Head "0. 내 PC 네트워크"
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -notlike "*Loopback*" } |
    ForEach-Object {
        Write-Host ("   {0,-22} {1,-16} {2}" -f $_.InterfaceAlias, $_.IPAddress, $_.PrefixOrigin)
    }
Note "PrefixOrigin 이 Manual 이어야 고정 IP 적용됨 (Dhcp 면 미적용)"

$gw = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
      Sort-Object RouteMetric | Select-Object -First 1
if ($gw) { Write-Host ("   기본 게이트웨이       : " + $gw.NextHop) }

# 유선/무선 동시 연결이면 라우팅이 꼬여 "됐다 안 됐다" 증상이 난다
$upAdapters = @(Get-NetAdapter | Where-Object { $_.Status -eq "Up" -and $_.Virtual -eq $false })
if ($upAdapters.Count -gt 1) {
    NG ("활성 어댑터가 " + $upAdapters.Count + "개 — 유선/WiFi 동시 연결이면 WiFi 를 끄세요")
    $upAdapters | ForEach-Object { Note (" - " + $_.Name + " (" + $_.InterfaceDescription + ")") }
} else {
    OK "활성 어댑터 1개 — 정상"
}

# ─────────────────────────────────────────────────────────────
# 공용 함수
# ─────────────────────────────────────────────────────────────

# TCP 포트가 열려 있는지 (짧은 타임아웃)
function Test-Port([string]$ip, [int]$port, [int]$ms = 400) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $r = $c.BeginConnect($ip, $port, $null, $null)
        if ($r.AsyncWaitHandle.WaitOne($ms)) { $c.EndConnect($r); return $true }
        return $false
    } catch { return $false } finally { $c.Close() }
}

# 8090 응답의 '주체'를 판별한다.
#   로봇      → JSON 응답
#   라우터    → HTML(로그인 페이지)
# 관제 온라인 판정이 상태코드를 안 보기 때문에, 라우터가 답해도 Online 으로 뜬다.
# 그 오탐을 여기서 걸러낸다.
function Probe-Who([string]$ip) {
    $url = "http://" + $ip + ":8090/chassis/status"
    $raw = & $curl -s -i --max-time 12 $url
    if ([string]::IsNullOrWhiteSpace($raw)) { return "NORESP" }

    $text = ($raw -join "`n")
    if ($text -match "(?i)content-type:\s*application/json") { return "ROBOT" }
    if ($text -match "(?i)content-type:\s*text/html")        { return "ROUTER" }
    if ($text -match '\{\s*"')                                { return "ROBOT" }
    return "UNKNOWN"
}

# ─────────────────────────────────────────────────────────────
# [Robot 모드] 로봇 라우터 LAN 안에서 — 로봇이 실제로 있는지, IP 가 뭔지
# ─────────────────────────────────────────────────────────────
if ($Mode -eq "Robot") {

    Head ("1. 로봇 기대 IP 직접 확인 (" + $RobotLan + ")")
    if (Test-Port $RobotLan 8090 800) {
        $who = Probe-Who $RobotLan
        if ($who -eq "ROBOT") {
            OK ("로봇 확인 — IP = " + $RobotLan + " (포워딩 대상과 일치)")
            [void]$verdict.Add("로봇 IP 정상 (" + $RobotLan + ")")
        } else {
            NG ("8090 은 열려 있으나 로봇이 아님 (판정: " + $who + ")")
            [void]$verdict.Add("주의: " + $RobotLan + " 에 로봇 아닌 장비")
        }
    } else {
        NG ($RobotLan + ":8090 응답 없음 — 로봇이 이 IP 가 아닐 수 있음")
        [void]$verdict.Add("경고: 기대 IP 에 로봇 없음")
    }

    Head "2. 대역 전체 스캔 (192.168.39.1~254 : 8090)"
    Note "라우터(.1)도 8090 을 쓰므로 결과에 같이 나온다. 로봇은 별도 판별한다."
    $found = @()
    foreach ($i in 1..254) {
        $ip = "192.168.39." + $i
        if (Test-Port $ip 8090 200) { $found += $ip; Write-Host ("   발견: " + $ip) -ForegroundColor Yellow }
    }
    if ($found.Count -eq 0) {
        NG "8090 이 열린 장비가 하나도 없음 — 다른 대역에 붙어 있는지 확인 필요"
    }

    Head "3. 발견된 장비의 정체 판별"
    foreach ($ip in $found) {
        $who = Probe-Who $ip
        switch ($who) {
            "ROBOT"  { OK  ($ip + "  →  로봇 (JSON 응답)");        [void]$verdict.Add("로봇 실제 IP = " + $ip) }
            "ROUTER" { Note ($ip + "  →  라우터 관리페이지 (HTML)") }
            default  { Note ($ip + "  →  판별 불가 (" + $who + ")") }
        }
    }

    Head "4. 로봇 상세 (SN / 모델) — 로봇이 확인된 경우"
    $robotIp = ($verdict | Where-Object { $_ -like "로봇 실제 IP*" } | Select-Object -First 1)
    if ($robotIp) {
        $ip = $robotIp.Split("=")[1].Trim()
        $info = & $curl -s --max-time 12 ("http://" + $ip + ":8090/device/info")
        if ([string]::IsNullOrWhiteSpace($info)) {
            NG "device/info 무응답 — LAN 안인데도 큰 응답이 안 옴 (로봇 자체 이상)"
        } else {
            OK ("device/info 응답 " + $info.Length + " bytes")
            if ($info -match '"sn"\s*:\s*"([^"]+)"')    { Write-Host ("   SN    : " + $Matches[1]) }
            if ($info -match '"model"\s*:\s*"([^"]+)"') { Write-Host ("   model : " + $Matches[1]) }
        }
    } else {
        Note "로봇을 못 찾아 건너뜀"
    }
}

# ─────────────────────────────────────────────────────────────
# [Server 모드] 서버 라우터에서 LTE 를 넘어 로봇까지 — 여기가 본 진단
# ─────────────────────────────────────────────────────────────
if ($Mode -eq "Server") {

    # ── 1. 라우터 도달성 + 손실률 ──
    Head "1. 라우터 도달성 (ping 10회) — 무선 품질"
    Note "ping 은 라우터 WAN 인터페이스 대상이다. 포워딩·로봇과는 무관하다."
    $p = & ping -n 10 -w 1000 $RobotWan
    $p | Select-String -Pattern "(\d+)% (loss|손실)" | ForEach-Object { Write-Host ("   " + $_.ToString().Trim()) }
    if ($LASTEXITCODE -eq 0) { OK ("로봇 라우터 " + $RobotWan + " 도달 가능") ; [void]$verdict.Add("ICMP 도달 O") }
    else                     { NG ("로봇 라우터 " + $RobotWan + " 도달 불가") ; [void]$verdict.Add("ICMP 도달 X") }

    # ── 2. MTU 측정 ── 여기가 이번 진단의 핵심 중 하나
    Head "2. MTU 측정 (DF 비트, 크기 계단식)"
    Note "ping 은 되는데 데이터가 안 가는 전형적 원인이 MTU 블랙홀이다."
    Note "통과한 최대 크기 + 28 = 실제 MTU. 1472 통과 시 MTU 1500 (정상)."
    $sizes = @(1472, 1452, 1432, 1412, 1392, 1372, 1352, 1332, 1300, 1272, 1200)
    $mtuPass = 0
    foreach ($s in $sizes) {
        & ping -f -l $s -n 4 -w 1000 $RobotWan | Out-Null
        if ($LASTEXITCODE -eq 0) {
            OK ("크기 " + $s + " 통과  →  MTU = " + ($s + 28))
            $mtuPass = $s + 28
            break
        } else {
            Write-Host ("   크기 " + $s + " 실패") -ForegroundColor DarkYellow
        }
    }
    if ($mtuPass -eq 0) {
        NG "어떤 크기도 통과 못 함 — 경로 자체가 막혔거나 ICMP 차단"
        [void]$verdict.Add("MTU 측정 불가")
    } elseif ($mtuPass -lt 1500) {
        NG ("MTU 가 1500 미만 (" + $mtuPass + ") — MTU 블랙홀 가능성 높음")
        [void]$verdict.Add("MTU = " + $mtuPass + " (1500 미만!)")
    } else {
        OK "MTU 1500 정상 — MTU 블랙홀 가설 기각"
        [void]$verdict.Add("MTU 1500 정상")
    }

    # ── 3. TCP 연결 여부 ── ping 과 완전히 다른 경로다
    Head "3. TCP 8090 연결 (포워딩이 실제로 동작하는가)"
    $tcpOk = Test-Port $RobotWan 8090 3000
    if ($tcpOk) {
        OK ("TCP " + $RobotWan + ":8090 연결 성공 — 포워딩 동작함")
        [void]$verdict.Add("TCP 8090 연결 O")
    } else {
        NG ("TCP " + $RobotWan + ":8090 연결 실패")
        [void]$verdict.Add("TCP 8090 연결 X")
    }

    # ── 4. 응답 주체 판별 ── 로봇인가 라우터인가
    Head "4. 응답 주체 판별 (로봇 vs 라우터)"
    Note "관제는 상태코드를 안 봐서 라우터가 답해도 Online 으로 뜬다. 그 오탐을 가린다."
    $who = Probe-Who $RobotWan
    switch ($who) {
        "ROBOT"  { OK  "JSON 응답 — 진짜 로봇이 답하고 있다";              [void]$verdict.Add("응답 주체 = 로봇") }
        "ROUTER" { NG  "HTML 응답 — 라우터가 답하고 있다 (로봇까지 안 감)"; [void]$verdict.Add("응답 주체 = 라우터(오탐)") }
        "NORESP" { NG  "무응답 — 패킷이 돌아오지 않음";                     [void]$verdict.Add("응답 없음") }
        default  { NG  "판별 불가";                                          [void]$verdict.Add("판별 불가") }
    }

    # ── 5. 큰 응답 통과 여부 ── MTU 블랙홀의 결정적 증거
    Head "5. 큰 응답 통과 (device/info) — MTU 블랙홀 판정"
    Note "작은 응답(chassis/status)은 오는데 큰 응답만 0 bytes 면 MTU 문제 확정."
    $tmpFile = Join-Path $env:TEMP "_diag_devinfo.tmp"
    $size = & $curl -s -o $tmpFile --max-time 25 -w "%{size_download}" ("http://" + $RobotWan + ":8090/device/info")
    $size = ("" + $size).Trim()
    if ([string]::IsNullOrWhiteSpace($size)) { $size = "0" }
    Write-Host ("   수신 크기 : " + $size + " bytes")
    if ([int]$size -gt 0) {
        OK "큰 응답 정상 수신 — MTU 블랙홀 아님"
        [void]$verdict.Add("device/info 수신 O (" + $size + " bytes)")
    } elseif (-not $tcpOk) {
        # TCP 연결부터 실패했으면 크기가 0인 건 당연하다. MTU 판정 근거로 쓰면 안 된다.
        Note "0 bytes — 단, 3번에서 TCP 연결부터 실패했으므로 MTU 판정 근거가 아니다"
        [void]$verdict.Add("device/info 0 bytes (TCP 연결 자체 실패 — 판정 제외)")
    } else {
        NG "0 bytes — TCP 는 연결되는데 큰 응답만 안 온다 = MTU 블랙홀 시그니처"
        [void]$verdict.Add("device/info 0 bytes (★MTU 블랙홀 의심)")
    }

    # ── 6. WebSocket ── 관제가 배터리·주행상태를 받는 경로
    Head "6. WebSocket (배터리 / 주행상태 경로)"
    $wsArgs = @(
        "-s", "-i", "-N", "--max-time", "8",
        "-H", "Connection: Upgrade",
        "-H", "Upgrade: websocket",
        "-H", "Sec-WebSocket-Version: 13",
        "-H", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        ("http://" + $RobotWan + ":8090/ws/v2/topics")
    )
    $wsOut = & $curl $wsArgs
    $wsHead = ($wsOut | Select-Object -First 1)
    if ($wsHead -match "101") { OK ("WS 업그레이드 성공 — " + $wsHead); [void]$verdict.Add("WS O") }
    else                      { NG "WS 실패";                            [void]$verdict.Add("WS X") }

    # ── 7. 반대 방향 ── 서버 라우터 인바운드(8002)도 필요하다
    Head ("7. 반대 방향 확인 (서버 라우터 " + $ServerWan + ")")
    Note "태블릿이 서버에 접속하려면 이쪽 인바운드도 열려 있어야 한다."
    & ping -n 4 -w 1000 $ServerWan | Out-Null
    if ($LASTEXITCODE -eq 0) { OK ($ServerWan + " ping 정상") } else { NG ($ServerWan + " ping 실패") }

    # ── 8. 관제 백엔드가 보는 실제 판정 ── errors 필드가 원인을 직접 말해준다
    Head ("8. 관제 백엔드 판정 (localhost:" + $ServerPort + ")")
    Note "errors 필드에 실패 사유가 그대로 들어온다. SN 이 채워지면 진짜 로봇이다."
    $live = & $curl -s --max-time 60 ("http://localhost:" + $ServerPort + "/api/robots/live")
    if ([string]::IsNullOrWhiteSpace($live)) {
        NG "백엔드 무응답 — 서버가 실행 중인지 확인 (uvicorn)"
    } else {
        Write-Host $live
        if ($live -match '"ONLINE"\s*:\s*"Online"') { OK "관제 판정: Online" }
        else                                         { NG "관제 판정: Offline" }
    }
}

# ─────────────────────────────────────────────────────────────
# 종합
# ─────────────────────────────────────────────────────────────
Head "종합"
foreach ($v in $verdict) { Write-Host ("   - " + $v) }

Write-Host ""
Write-Host "  [판정 가이드]" -ForegroundColor Yellow
Write-Host "   TCP 연결 X + ICMP 도달 O      →  (D) U+ 인바운드 차단 의심" -ForegroundColor Gray
Write-Host "   TCP 연결 O + device/info 0B   →  (C) MTU 블랙홀 — 라우터 MTU/MSS 조정" -ForegroundColor Gray
Write-Host "   응답 주체 = 라우터            →  (B) 포워딩이 로봇으로 안 감" -ForegroundColor Gray
Write-Host "   Robot 모드에서 로봇 IP 불일치 →  (A) 로봇 고정 IP 재설정" -ForegroundColor Gray
Write-Host "   전부 O 인데 관제만 Offline    →  DB 등록 IP 확인" -ForegroundColor Gray

Write-Host ""
Write-Host ("  결과 저장됨 : " + $log) -ForegroundColor Green
Write-Host ""

try { Stop-Transcript | Out-Null } catch { }
