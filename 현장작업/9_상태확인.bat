@echo off
chcp 949 > nul
title [9] 지금 상태 확인
cd /d "%~dp0.."
call "%~dp0config.bat"

echo.
echo ============================================================
echo  [9] 지금 상태 확인
echo ============================================================
echo.

echo --- git 위치 ---
git log --oneline -3
echo.
git branch --show-current
echo.
echo   ebf8084 이후 커밋이 보여야 최신입니다.
echo.

echo --- 안 올린 변경 ---
git status --short
echo.

echo --- 백엔드 살아있나 ---
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -Uri '%BACKEND%/ping' -TimeoutSec 5 -UseBasicParsing; Write-Host ('  OK  ' + $r.StatusCode) } catch { Write-Host '  응답 없음 - 백엔드가 꺼져 있습니다' }"
echo.

echo --- 로봇 닿나 ---
ping %ROBOT_IP% -n 2 | findstr /C:"평균" /C:"Average" /C:"요청 시간이 만료" /C:"Request timed out" /C:"대상 호스트"
echo.

echo --- 현재 적용된 맵 영역 ---
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri '%BACKEND%/api/map/default-area' -TimeoutSec 5 -UseBasicParsing).Content } catch { Write-Host '  조회 실패' }"
echo.
echo   이 area_id 가 로봇이 쓰는 맵과 같아야 합니다.
echo   다르면 로봇이 엉뚱한 좌표로 갑니다.
echo.
pause
