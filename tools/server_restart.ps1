# ─────────────────────────────────────────────────────────────
#  RCS LG — 서버 재시작 (현장 서버PC 바탕화면 버튼용)
#
#  2026-11-06 — 현장 서버PC 가 업데이트·강제 재부팅 뒤 아무것도 안 켜진 채로 발견됨.
#  바탕화면에서 한 번 눌러 "떠 있는 백엔드·프론트를 모두 끄고 다시 띄우기" 를 하려고 만든다.
#
#  하는 일
#    1. 8002(백엔드)·3000(프론트)을 쓰는 프로세스와, uvicorn·이 폴더의 next 프로세스를 끈다
#    2. 포트가 빌 때까지 기다린다(최대 20초)
#    3. 기존 start_server.ps1 을 새 창으로 실행한다 (8002 단독 / 3000 별도 판단은 그쪽이 그대로 함)
#    4. /ping 응답으로 정상 여부를 알려 준다(최대 90초)
#  start_server.ps1 은 고치지 않는다 — 현장에서 8월부터 쓰던 그대로 부른다.
#
#  위치: <RCS_LGIT>\tools\server_restart.ps1   바탕화면 버튼: tools\server_restart.bat 복사
# ─────────────────────────────────────────────────────────────

$ROOT  = Split-Path $PSScriptRoot -Parent
$START = Join-Path $ROOT "start_server.ps1"

Write-Host ""
Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  RCS LG   서버 재시작"                                       -ForegroundColor Cyan
Write-Host "  위치: $ROOT"                                               -ForegroundColor DarkGray
Write-Host "===========================================================" -ForegroundColor Cyan

if (-not (Test-Path $START)) {
    Write-Host "  [중단] start_server.ps1 이 없습니다: $START" -ForegroundColor Red
    Read-Host "엔터를 누르면 창을 닫습니다"; exit 1
}

# ── 1. 끄기 — 포트 주인 + uvicorn(app.main) + 이 폴더에서 띄운 next (다른 python/node 는 건드리지 않음)
$pids = @()
$pids += Get-NetTCPConnection -State Listen -LocalPort 8002, 3000 -ErrorAction SilentlyContinue |
         Select-Object -ExpandProperty OwningProcess
$rootEsc = [regex]::Escape($ROOT)
$pids += Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'" |
         Where-Object { $_.CommandLine -match "uvicorn\s+app\.main" -or
                        ($_.CommandLine -match $rootEsc -and $_.CommandLine -match "next") } |
         Select-Object -ExpandProperty ProcessId
$pids = $pids | Where-Object { $_ -and $_ -ne 0 } | Sort-Object -Unique
if ($pids) {
    Write-Host "  [1/3] 실행 중인 서버 끄기: $($pids -join ', ')"
    foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
} else {
    Write-Host "  [1/3] 떠 있는 서버 없음"
}

# ── 2. 포트가 빌 때까지 대기
$deadline = (Get-Date).AddSeconds(20)
while ((Get-NetTCPConnection -State Listen -LocalPort 8002, 3000 -ErrorAction SilentlyContinue) -and
       (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
if (Get-NetTCPConnection -State Listen -LocalPort 8002, 3000 -ErrorAction SilentlyContinue) {
    Write-Host "  [주의] 20초 지나도 포트가 비지 않았습니다 — 그래도 시작합니다" -ForegroundColor Yellow
}

# ── 3. 기존 시작 스크립트를 새 창으로 (그 창에서 안내·확인 질문이 나오면 거기서 답한다)
Write-Host "  [2/3] start_server.ps1 실행"
Start-Process powershell -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-File", "`"$START`"") `
    -WorkingDirectory $ROOT | Out-Null

# ── 4. 확인
Write-Host "  [3/3] 백엔드 응답 확인 (최대 90초)"
$ok = $false
$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    try { Invoke-RestMethod -Uri "http://127.0.0.1:8002/ping" -TimeoutSec 3 | Out-Null; $ok = $true; break }
    catch { Start-Sleep -Seconds 2 }
}
Write-Host ""
if ($ok) {
    Write-Host "  정상 — 서버가 다시 떴습니다.  콘솔: http://127.0.0.1:8002/api/dispatch/console" -ForegroundColor Green
} else {
    Write-Host "  실패 — 90초 안에 백엔드가 응답하지 않습니다. 새로 뜬 창의 메시지를 확인하세요." -ForegroundColor Red
}
Read-Host "엔터를 누르면 이 창을 닫습니다"
