# -------------------------------------------------------------
#  RCS LG - 로컬 관제 서버 시작 (8002 통합 모드)
#
#  start_local.bat 이 이 파일을 부릅니다.
#
#  8002 통합 모드란
#    BackEnd\web 폴더가 있으면 백엔드(main.py)가 관제 화면까지 8002 에서
#    같이 서빙합니다. 프론트를 3000 번으로 따로 띄울 필요가 없습니다.
#    (근거: BackEnd\app\main.py 의 _web_dir 마운트 로직)
#
#  화면을 고쳤을 때는 다시 빌드해서 넣어야 반영됩니다:
#    cd frontend
#    $env:BUILD_STATIC="1"; npm run build
#    robocopy out ..\BackEnd\web /E
#
#  되돌리기 - BackEnd\web 폴더를 지우면 예전처럼 프론트 3000 별도 모드가 됩니다.
# -------------------------------------------------------------
param(
    [switch]$NoBrowser,        # 브라우저 자동 실행 안 함
    [int]$Port = 8002
)

$ROOT = $PSScriptRoot
function Say($t)  { Write-Host $t }
function Warn($t) { Write-Host $t -ForegroundColor Yellow }
function Bad($t)  { Write-Host $t -ForegroundColor Red }
function Good($t) { Write-Host $t -ForegroundColor Green }
function Stop-Here($code) {
    Write-Host ""
    Read-Host "엔터를 누르면 창을 닫습니다"
    exit $code
}

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  RCS LG   로컬 관제 서버 (8002 통합)"                      -ForegroundColor Cyan
Write-Host "  위치: $ROOT"                                             -ForegroundColor DarkGray
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""

# -- 1) DB -- 안 떠 있으면 백엔드가 기동 직후 죽는다
$svc = Get-Service -Name "MariaDB", "MySQL*" -ErrorAction SilentlyContinue |
       Where-Object { $_.Status -eq "Running" } | Select-Object -First 1
if ($svc) {
    Good "  [1/4] DB  $($svc.Name) 실행 중"
} else {
    Bad  "  [1/4] MariaDB 가 실행 중이 아닙니다."
    Warn "        관리자 PowerShell 에서:  Start-Service MariaDB"
    Warn "        서비스 이름 확인:        Get-Service *maria*, *mysql*"
    Warn "        이대로 두면 백엔드가 DB 접속 실패로 죽습니다."
    Stop-Here 1
}

# -- 2) 파이썬 --
$py = Join-Path $ROOT "BackEnd\venv\Scripts\python.exe"
if (Test-Path $py) {
    Good "  [2/4] venv 파이썬 확인"
} else {
    Bad  "  [2/4] venv 가 없습니다: $py"
    Warn "        BackEnd 폴더에서:"
    Warn "          python -m venv venv"
    Warn "          venv\Scripts\pip.exe install -r requirements.txt"
    Stop-Here 1
}

# -- 3) 관제 화면 정적 빌드 --
$web = Join-Path $ROOT "BackEnd\web\index.html"
if (Test-Path $web) {
    $n = (Get-ChildItem (Split-Path $web) -Recurse -File).Count
    Good "  [3/4] 관제 화면 정적 빌드 확인 ($n 개 파일) - 8002 통합 모드"
} else {
    Warn "  [3/4] BackEnd\web 이 없습니다 - 관제 화면은 안 뜹니다(API 만 동작)."
    Warn "        화면까지 쓰려면:"
    Warn "          cd frontend"
    Warn "          `$env:BUILD_STATIC=`"1`"; npm run build"
    Warn "          robocopy out ..\BackEnd\web /E"
    Write-Host ""
}

# -- 4) 포트 점유 확인 --
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $pid0 = ($busy | Select-Object -First 1).OwningProcess
    $pname = (Get-Process -Id $pid0 -ErrorAction SilentlyContinue).ProcessName
    Warn "  [4/4] 포트 $Port 를 이미 쓰고 있습니다 (PID $pid0 / $pname)"
    Warn "        이미 서버가 떠 있는 것일 수 있습니다. 브라우저만 열어봅니다."
    if (-not $NoBrowser) { Start-Process "http://localhost:$Port/" }
    Stop-Here 0
} else {
    Good "  [4/4] 포트 $Port 비어 있음"
}

Write-Host ""
Say "  백엔드를 새 창에서 시작합니다..."
$runner = Join-Path $ROOT "BackEnd\run_local.ps1"
if (-not (Test-Path $runner)) {
    Bad "  run_local.ps1 이 없습니다: $runner"
    Stop-Here 1
}
Start-Process powershell -ArgumentList @(
    "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$runner`""
) | Out-Null

# 응답할 때까지 대기
Say "  응답 대기 (최대 90초)..."
$sw = [Diagnostics.Stopwatch]::StartNew()
$ok = $false
while ($sw.Elapsed.TotalSeconds -lt 90) {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/robots?limit=1" -TimeoutSec 3
        $ok = $true
        break
    } catch { Start-Sleep -Seconds 2 }
}

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
if ($ok) {
    Good "  서버 준비 완료  ($([int]$sw.Elapsed.TotalSeconds)초)"
    Say  ""
    Say  "   관제 화면 : http://localhost:$Port/"
    Say  "   API 문서  : http://localhost:$Port/docs"
    if (-not $NoBrowser) { Start-Process "http://localhost:$Port/" }
} else {
    Bad  "  90초 동안 응답이 없습니다."
    Warn "  백엔드 창에 뜬 오류를 확인하세요. 흔한 원인:"
    Warn "    - MariaDB 에 rcs_lg_db 가 없음"
    Warn "    - run_local.ps1 의 DB 비밀번호가 실제와 다름"
    Warn "    - 파이썬 패키지 누락"
}
Say  ""
Say  "  서버 창을 닫으면 서버가 꺼집니다. 이 창은 닫아도 됩니다."
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""
Read-Host "엔터를 누르면 이 창만 닫습니다"
