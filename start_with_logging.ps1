# ─────────────────────────────────────────────────────────────
#  RCS LG - 관제 서버 + 주행 로그 기록을 한 번에 시작
#
#  start_with_logging.bat 이 이 파일을 부릅니다.
#
#  왜 기존 start_server.ps1 을 고치지 않고 이 파일을 따로 두는가
#    로깅이 잘못돼도 **서버는 정상으로 떠야** 하기 때문입니다.
#    기존 시작 파일은 한 글자도 건드리지 않았으므로, 이 파일에 문제가 생기면
#    그냥 예전처럼 서버시작_0828.bat 을 쓰면 됩니다.
#
#  하는 일
#    1) 8002 가 이미 떠 있으면 서버를 새로 띄우지 않는다 (중복 기동 방지)
#    2) 안 떠 있으면 기존 start_server.ps1 을 그대로 호출한다
#    3) 백엔드가 응답할 때까지 기다린다
#    4) 등록된 로봇마다 drive_log.py 를 하나씩 띄운다
#       - 주행 시작(충전소 이탈)은 drive_log 가 알아서 감지합니다.
#         사용자가 따로 켜고 끌 것이 없습니다.
# ─────────────────────────────────────────────────────────────
param(
    [switch]$SkipServer,          # 서버는 이미 떠 있다 - 로거만 띄운다
    [string]$Server = "http://127.0.0.1:8002",
    [int]$WaitSec = 180,
    [string]$Tag = "",
    [string]$Robot = ""          # 이름이나 IP 일부. 주면 그 로봇만 기록한다
)

$ROOT = $PSScriptRoot
$ErrorActionPreference = "Continue"

function Say($t)  { Write-Host $t }
function Warn($t) { Write-Host $t -ForegroundColor Yellow }
function Bad($t)  { Write-Host $t -ForegroundColor Red }
function Good($t) { Write-Host $t -ForegroundColor Green }

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  RCS LG   관제 서버 + 주행 로그 기록"                      -ForegroundColor Cyan
Write-Host "  위치: $ROOT"                                             -ForegroundColor DarkGray
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""

function Test-Backend {
    try {
        $null = Invoke-RestMethod -Uri "$Server/api/robots?limit=1" -TimeoutSec 3
        return $true
    } catch {
        return $false
    }
}

# ── 1) 이미 떠 있나 ──
$alive = Test-Backend
if ($alive) {
    Good "  [1/3] 백엔드가 이미 8002 에서 응답합니다 - 새로 띄우지 않습니다."
} elseif ($SkipServer) {
    Warn "  [1/3] -SkipServer 인데 백엔드가 응답하지 않습니다. 계속 기다립니다."
} else {
    $starter = Join-Path $ROOT "start_server.ps1"
    if (-not (Test-Path $starter)) {
        Bad "  [1/3] start_server.ps1 이 없습니다: $starter"
        Bad "        서버를 직접 띄운 뒤 이 파일을 다시 실행하세요."
        Write-Host ""
        Read-Host "엔터를 누르면 창을 닫습니다"
        exit 1
    }
    Say "  [1/3] 서버 시작 - start_server.ps1 호출"
    Start-Process powershell -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$starter`""
    ) | Out-Null
}

# ── 2) 백엔드가 응답할 때까지 대기 ──
Say "  [2/3] 백엔드 응답 대기 (최대 $WaitSec 초)..."
$sw = [Diagnostics.Stopwatch]::StartNew()
while (-not (Test-Backend)) {
    if ($sw.Elapsed.TotalSeconds -gt $WaitSec) {
        Bad "        $WaitSec 초 동안 응답이 없습니다."
        Bad "        MariaDB 가 떠 있는지, 백엔드 창에 오류가 없는지 확인하세요."
        Bad "        (서버는 그대로 둡니다 - 로거만 못 띄운 것입니다)"
        Write-Host ""
        Read-Host "엔터를 누르면 창을 닫습니다"
        exit 1
    }
    Start-Sleep -Seconds 2
}
Good "        백엔드 응답 확인 ($([int]$sw.Elapsed.TotalSeconds)초)"

# ── 3) 로봇마다 로거 기동 ──
$py = Join-Path $ROOT "BackEnd\venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Warn "        venv python 이 없어 시스템 python 을 씁니다: $py"
    $py = "python"
}
$script = Join-Path $ROOT "scripts\drive_log.py"
if (-not (Test-Path $script)) {
    Bad "  [3/3] drive_log.py 가 없습니다: $script"
    Write-Host ""
    Read-Host "엔터를 누르면 창을 닫습니다"
    exit 1
}

$robots = @()
try {
    $resp = Invoke-RestMethod -Uri "$Server/api/robots?limit=50" -TimeoutSec 5
    if ($resp.items) { $robots = $resp.items } else { $robots = $resp }
} catch {
    Bad "  [3/3] 로봇 목록 조회 실패: $($_.Exception.Message)"
}

$targets = @($robots | Where-Object { $_.ip_address })
if ($Robot) {
    $targets = @($targets | Where-Object {
        $_.ip_address -like "*$Robot*" -or $_.name -like "*$Robot*"
    })
    if ($targets.Count -eq 0) {
        Warn "  [3/3] '-Robot $Robot' 과 맞는 로봇이 없습니다. 등록된 로봇:"
        foreach ($r in $robots) { Warn "        $($r.name)  $($r.ip_address)" }
    } else {
        Say "        -Robot '$Robot' 지정 - $($targets.Count)대만 기록합니다"
    }
}
if ($targets.Count -eq 0) {
    Warn "  [3/3] IP 가 등록된 로봇이 없습니다 - 로거를 띄우지 않습니다."
    Warn "        관제 화면에서 로봇 IP 를 등록한 뒤 이 파일을 다시 실행하세요."
} else {
    Say "  [3/3] 로봇 $($targets.Count) 대 - 로거를 하나씩 띄웁니다"
    foreach ($r in $targets) {
        $ip   = $r.ip_address
        $name = $r.name
        if (-not $name) { $name = $ip }
        $safe = ($name -replace '[^\w\-]', '_')
        if ($Tag) { $safe = "${safe}_${Tag}" }
        $argList = @(
            "`"$script`"",
            "--ip", $ip,
            "--server", $Server,
            "--tag", $safe
        )
        Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $ROOT | Out-Null
        Good "        $name ($ip) 로거 기동"
    }
}

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Say "  준비 완료."
Say "  · 주행을 시작하면(충전소를 벗어나면) 로그가 자동으로 쌓입니다."
Say "  · 충전소로 돌아오면 그 왕복이 폴더 하나로 마감됩니다."
Say "  · 저장 위치:  $ROOT\_logs\run_<날짜시각>_<로봇>\c01_...\"
Write-Host ""
Say "  로거 창을 닫아도 서버는 안 죽습니다. 반대도 마찬가지입니다."
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "엔터를 누르면 이 창만 닫힙니다 (서버·로거는 계속 돕니다)"
