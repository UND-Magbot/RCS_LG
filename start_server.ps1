# ─────────────────────────────────────────────────────────────
#  RCS LG — 백엔드(8002) + 관제 화면 기동
#
#  서버시작_0828.bat 이 이 파일을 부릅니다.
#  로직을 .bat 이 아니라 여기에 둔 이유:
#    cmd.exe 는 배치파일 안의 한글(멀티바이트) 때문에 읽는 위치가 어긋나,
#    goto 로 건너뛴 뒤 엉뚱한 줄을 실행하는 일이 있습니다(실측 확인).
#    PowerShell 은 그 문제가 없어 안내 문구를 마음껏 한글로 쓸 수 있습니다.
#
#  관제 화면 서빙 방식이 두 가지라 자동으로 갈라집니다.
#    (A) 8002 단독  : BackEnd\web 이 있으면 백엔드가 관제 화면까지 서빙 → 프론트 안 띄움
#    (B) 3000 별도  : BackEnd\web 이 없으면 frontend 를 npm run start 로 띄움
#  판단 근거는 BackEnd\app\main.py 의 _web_dir 마운트 로직과 같습니다.
# ─────────────────────────────────────────────────────────────

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
Write-Host "  RCS LG   관제 서버 시작"                                  -ForegroundColor Cyan
Write-Host "  위치: $ROOT"                                             -ForegroundColor DarkGray
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host ""

# ── 0. DB 서비스 ── 안 떠 있으면 백엔드가 기동 직후 죽는다
$svc = Get-Service -Name "MariaDB", "MySQL*" -ErrorAction SilentlyContinue |
       Where-Object { $_.Status -eq "Running" } | Select-Object -First 1
if ($svc) {
    Say "  [DB] $($svc.Name) 실행 중"
} else {
    Warn "  [주의] MariaDB 서비스가 실행 중이 아닙니다."
    Warn "         관리자 PowerShell 에서:  Start-Service MariaDB"
    Warn "         서비스 이름 확인:        Get-Service *maria*, *mysql*"
    Warn "         이대로 진행하면 백엔드가 DB 접속 실패로 죽습니다."
    Write-Host ""
    $go = Read-Host "  그래도 계속할까요? (y/N)"
    if ($go -ne "y") { Stop-Here 1 }
}

# ── 1. 백엔드 ──
$py = Join-Path $ROOT "BackEnd\venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Bad  "  [중단] 가상환경이 없습니다."
    Bad  "         $ROOT\BackEnd\venv"
    Write-Host ""
    Warn "  BackEnd 폴더에서 아래를 먼저 실행하세요."
    Warn "      python -m venv venv"
    Warn "      venv\Scripts\pip.exe install fastapi uvicorn[standard] sqlalchemy pymysql apscheduler passlib bcrypt==4.0.1 python-jose requests websocket-client"
    Write-Host ""
    Warn "  bcrypt 는 4.0.1 로 고정해야 합니다."
    Warn "  4.1 이상은 passlib 1.7.4 와 충돌해 로그인이 깨집니다."
    Stop-Here 1
}

$runps = Join-Path $ROOT "BackEnd\run_server.ps1"
if (-not (Test-Path $runps)) {
    Bad "  [중단] BackEnd\run_server.ps1 이 없습니다. 압축을 덮어쓰기로 풀었는지 확인하세요."
    Stop-Here 1
}

Say "  [1/2] 백엔드 시작 ... http://127.0.0.1:8002"
Start-Process powershell -ArgumentList @(
    "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$runps`""
) | Out-Null

# 백엔드가 DB 붙고 스케줄러 올라올 시간을 준다
Start-Sleep -Seconds 12

# ── 2. 관제 화면 ── 서빙 방식 자동 판별
$webDir = Join-Path $ROOT "BackEnd\web"
if (Test-Path $webDir) {
    Say  "  [2/2] 관제 화면은 백엔드가 함께 서빙합니다 (BackEnd\web 있음 → 8002 단독 모드)"
    Say  "        프론트(3000)는 띄우지 않습니다. Node.js 도 필요 없습니다."
    Write-Host ""
    Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
    Say "  관제 화면   : http://localhost:8002"
    Say "  배차 콘솔   : http://localhost:8002/api/dispatch/console"
    Say "  백엔드 확인 : http://localhost:8002/ping"
    Write-Host ""
    Say "  끄려면 열린 창(RCS Backend)을 닫으세요."
    Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
    Stop-Here 0
}

$fe = Join-Path $ROOT "frontend"

# 2-1. .env.local ── 없으면 프론트가 통째로 깨진다(폴백 없이 쓰는 곳이 20군데 넘음)
$envFile = Join-Path $fe ".env.local"
if (-not (Test-Path $envFile)) {
    Warn "  [보정] frontend\.env.local 이 없어 기본값으로 만들었습니다."
    Warn "         NEXT_PUBLIC_API_URL=http://localhost:8002"
    # BOM 없이 써야 한다. Set-Content -Encoding UTF8 은 BOM 을 붙이는데,
    # 그러면 첫 키 이름이 "﻿NEXT_PUBLIC_API_URL" 이 되어 변수를 못 찾을 수 있다.
    [IO.File]::WriteAllText($envFile,
        "NEXT_PUBLIC_API_URL=http://localhost:8002`r`n",
        (New-Object System.Text.UTF8Encoding $false))
    Warn "         이 값은 npm run build 시점에 코드에 박힙니다."
    Warn "         관제 화면을 다른 PC 브라우저에서도 열 거라면"
    Warn "         localhost 대신 서버 PC 고정 IP 로 바꾸고 다시 빌드하세요."
    Write-Host ""
} else {
    $cur = (Get-Content $envFile | Where-Object { $_ -match "NEXT_PUBLIC_API_URL" } | Select-Object -First 1)
    Say  "  [설정] $cur"
    if ($cur -match "localhost|127\.0\.0\.1") {
        Warn "         → 이 값이면 관제 화면은 이 노트북에서만 열립니다."
        Warn "            다른 PC 에서도 볼 거면 서버 고정 IP 로 바꾸고 npm run build 를 다시 하세요."
    }
}

# 2-2. 빌드 결과
if (-not (Test-Path (Join-Path $fe ".next"))) {
    Bad  "  [중단] 프론트 빌드 결과가 없습니다."
    Bad  "         $fe\.next"
    Write-Host ""
    Warn "  먼저 아래를 실행하세요."
    Warn "      cd `"$fe`""
    Warn "      npm install"
    Warn "      npm run build"
    Write-Host ""
    Say  "  참고 — Node.js 없이 8002 하나로 운영하려면 정적 빌드를 쓰세요."
    Say  "      cmd /c `"set BUILD_STATIC=1 && npm run build`""
    Say  "      그 뒤 frontend\out 폴더를 BackEnd\web 으로 복사하면"
    Say  "      이 스크립트가 자동으로 8002 단독 모드로 인식합니다."
    Write-Host ""
    Say  "  백엔드는 이미 떠 있습니다. 빌드가 끝나면 이 파일을 다시 실행하세요."
    Stop-Here 1
}

Say "  [2/2] 프론트 시작 ... http://127.0.0.1:3000"
Start-Process cmd -ArgumentList "/k", "npm run start" -WorkingDirectory $fe | Out-Null

Write-Host ""
Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
Say "  관제 화면   : http://localhost:3000"
Say "  배차 콘솔   : http://localhost:8002/api/dispatch/console"
Say "  백엔드 확인 : http://localhost:8002/ping"
Write-Host ""
Say "  끄려면 열린 창 2개(RCS Backend / RCS Frontend)를 닫으세요."
Write-Host "-----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""
Stop-Here 0
