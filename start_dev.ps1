# -------------------------------------------------------------
#  RCS LG - 개발용 런처 : 백엔드(8002) + 프론트 dev 서버(3000)
#
#  start_dev.bat 이 이 파일을 부릅니다.
#
#  start_local.bat 과의 차이
#    start_local  = 8002 통합 모드. BackEnd\web 정적 빌드를 백엔드가 서빙 (현장용)
#    start_dev    = 백엔드 + next dev 를 **각자 창**으로 띄움 (연구소 개발용)
#                   화면 수정이 바로 반영되고, 창마다 로그가 그대로 보인다.
#
#  끄는 법 : 각 창에서 Ctrl+C (또는 창 닫기). 이 런처 창은 닫아도 서버는 계속 돈다.
# -------------------------------------------------------------
param(
    [switch]$NoBrowser,        # 브라우저 자동 실행 안 함
    [switch]$BackendOnly,      # 백엔드만
    [switch]$FrontendOnly      # 프론트만
)

$ROOT = $PSScriptRoot
function Warn($t) { Write-Host $t -ForegroundColor Yellow }
function Bad($t)  { Write-Host $t -ForegroundColor Red }
function Good($t) { Write-Host $t -ForegroundColor Green }

# 새 PowerShell 창을 제목을 붙여 띄운다. -NoExit 라 서버가 죽어도 마지막 로그가 창에 남는다.
# 명령을 -EncodedCommand 로 넘겨 경로·따옴표가 깨지지 않게 한다.
function Open-Window($title, $workDir, $command) {
    $script = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$workDir'; $command"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc
    ) | Out-Null
}

# 포트를 이미 누가 쓰고 있으면 PID 를 돌려준다 (중복 기동 방지)
function Get-PortOwner($port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.OwningProcess } else { return $null }
}

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  RCS LG   개발 런처  (백엔드 8002 + 프론트 3000)"          -ForegroundColor Cyan
Write-Host "  위치: $ROOT"                                             -ForegroundColor DarkGray
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""

$startBack  = -not $FrontendOnly
$startFront = -not $BackendOnly

# -- 백엔드 --
if ($startBack) {
    # DB 가 안 떠 있으면 백엔드가 기동 직후 죽는다
    $svc = Get-Service -Name "MariaDB", "MySQL*" -ErrorAction SilentlyContinue |
           Where-Object { $_.Status -eq "Running" } | Select-Object -First 1
    $runner = Join-Path $ROOT "BackEnd\run_local.ps1"
    $owner = Get-PortOwner 8002
    if (-not $svc) {
        Bad  "  [백엔드] MariaDB 가 실행 중이 아닙니다 - 백엔드를 띄우지 않습니다."
        Warn "           관리자 PowerShell 에서:  Start-Service MariaDB"
        $startBack = $false
    } elseif (-not (Test-Path $runner)) {
        Bad  "  [백엔드] run_local.ps1 이 없습니다: $runner"
        $startBack = $false
    } elseif ($owner) {
        Warn "  [백엔드] 8002 를 이미 쓰고 있습니다 (PID $owner) - 새로 띄우지 않습니다."
        $startBack = $false
    } else {
        Open-Window "RCS backend 8002" (Join-Path $ROOT "BackEnd") "& '$runner'"
        Good "  [백엔드] 새 창에서 시작 - 'RCS backend 8002'"
    }
}

# -- 프론트 --
if ($startFront) {
    $fe = Join-Path $ROOT "frontend"
    $owner = Get-PortOwner 3000
    if (-not (Test-Path (Join-Path $fe "node_modules"))) {
        Bad  "  [프론트] node_modules 가 없습니다 - frontend 에서 npm install 먼저."
        $startFront = $false
    } elseif ($owner) {
        Warn "  [프론트] 3000 을 이미 쓰고 있습니다 (PID $owner) - 새로 띄우지 않습니다."
        $startFront = $false
    } else {
        Open-Window "RCS frontend 3000" $fe "npm run dev"
        Good "  [프론트] 새 창에서 시작 - 'RCS frontend 3000'"
    }
}

# -- 백엔드 응답 대기 후 브라우저 --
if ($startBack) {
    Write-Host ""
    Write-Host "  백엔드 응답 대기 (최대 90초)..."
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $ok = $false
    while ($sw.Elapsed.TotalSeconds -lt 90) {
        try { $null = Invoke-RestMethod -Uri "http://127.0.0.1:8002/api/robots?limit=1" -TimeoutSec 3; $ok = $true; break }
        catch { Start-Sleep -Seconds 2 }
    }
    if ($ok) { Good "  백엔드 준비 완료 ($([int]$sw.Elapsed.TotalSeconds)초)" }
    else     { Bad  "  90초 동안 응답 없음 - 'RCS backend 8002' 창의 오류를 확인하세요." }
}

Write-Host ""
Write-Host "   관제 화면 : http://localhost:3000/monitoring"
Write-Host "   콘솔      : http://localhost:8002/api/dispatch/console"
Write-Host "   API 문서  : http://localhost:8002/docs"
if ($startFront -and -not $NoBrowser) { Start-Process "http://localhost:3000/monitoring" }

Write-Host ""
Write-Host "  끄기: 각 서버 창에서 Ctrl+C. 이 창은 닫아도 서버는 계속 돕니다." -ForegroundColor DarkGray
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "엔터를 누르면 이 창만 닫습니다"
