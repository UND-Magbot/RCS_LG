# ─────────────────────────────────────────────────────────────
#  RCS LG 백엔드 — 운영(현장) 실행
#
#  run_local.ps1 과의 차이는 딱 하나: --reload 가 없다.
#    개발용 --reload 는 파일을 계속 감시해 CPU 를 먹고,
#    파일이 저장 중인 순간에 재시작이 걸리면 서버가 죽는다.
#  나머지(포트 8002 / 로그 설정 / DB 접속)는 run_local.ps1 과 동일하다.
#
#  DB 비밀번호가 현장과 다르면 아래 DB_PASSWORD 만 고치면 된다.
# ─────────────────────────────────────────────────────────────
Set-Location $PSScriptRoot

$env:DB_NAME     = "rcs_lg_db"
$env:DB_HOST     = "127.0.0.1"
$env:DB_USER     = "root"
$env:DB_PASSWORD = "1234"

$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host ""
    Write-Host "[중단] 가상환경이 없습니다: $py" -ForegroundColor Red
    Write-Host "       BackEnd 폴더에서 아래를 먼저 실행하세요." -ForegroundColor Yellow
    Write-Host "         python -m venv venv" -ForegroundColor Yellow
    Write-Host "         .\venv\Scripts\pip.exe install fastapi `"uvicorn[standard]`" sqlalchemy pymysql apscheduler passlib `"bcrypt==4.0.1`" python-jose requests websocket-client" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "엔터를 누르면 닫습니다"
    exit 1
}

# 로그 폴더가 없으면 uvicorn 이 시작 시 죽는다
if (-not (Test-Path "$PSScriptRoot\_logs")) {
    New-Item -ItemType Directory "$PSScriptRoot\_logs" | Out-Null
}

Write-Host ""
Write-Host "  RCS LG 백엔드 (운영 모드 · reload 없음)" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:8002/ping  로 확인" -ForegroundColor DarkGray
Write-Host ""

& $py -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --log-config "$PSScriptRoot\log_config.json"
