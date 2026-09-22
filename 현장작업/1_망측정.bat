@echo off
chcp 949 > nul
title [1] 망 측정 - 현장을 나오기 전에 반드시
cd /d "%~dp0.."
call "%~dp0config.bat"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set OUTDIR=_logs\망측정_%TS%
mkdir "%OUTDIR%" 2>nul

echo.
echo ============================================================
echo  [1] 망 왕복시간 측정
echo ============================================================
echo.
echo  이것은 현장에서만 잴 수 있습니다.
echo  나온 뒤에는 다시 못 재고, 다시 들어가려면 방진복을 또 입어야 합니다.
echo.
echo  로봇        : %ROBOT_IP%
echo  로봇 라우터 : %ROBOT_ROUTER%
echo  서버 라우터 : %SERVER_ROUTER%
echo.
echo  저장 위치   : %OUTDIR%
echo.
echo  약 2분 걸립니다. 창을 닫지 마세요.
echo ------------------------------------------------------------
echo.

echo [1/5] 로봇 본체 ping ...
ping %ROBOT_IP% -n 20 > "%OUTDIR%\ping_로봇.txt" 2>&1

echo [2/5] 로봇 라우터 ping ...
ping %ROBOT_ROUTER% -n 20 > "%OUTDIR%\ping_로봇라우터.txt" 2>&1

echo [3/5] 서버 라우터 ping ...
ping %SERVER_ROUTER% -n 20 > "%OUTDIR%\ping_서버라우터.txt" 2>&1

echo [4/5] 경로 추적 ...
tracert -d -h 10 %ROBOT_IP% > "%OUTDIR%\tracert_로봇.txt" 2>&1

echo [5/5] 서버PC 네트워크 정보 ...
ipconfig /all > "%OUTDIR%\ipconfig.txt" 2>&1

echo.
echo ============================================================
echo  결과 요약
echo ============================================================
echo.
echo --- 로봇 본체 ---
findstr /C:"평균" /C:"Average" /C:"손실" /C:"Lost" "%OUTDIR%\ping_로봇.txt"
echo.
echo --- 로봇 라우터 ---
findstr /C:"평균" /C:"Average" /C:"손실" /C:"Lost" "%OUTDIR%\ping_로봇라우터.txt"
echo.
echo --- 경로 (홉 수) ---
type "%OUTDIR%\tracert_로봇.txt" | findstr /R "^ *[0-9]"
echo.
echo ============================================================
echo  평균값 판정
echo ------------------------------------------------------------
echo    1 ~  20 ms : 망은 빠름 -^> 로봇 REST 처리가 느린 것
echo   50 ~ 200 ms : 망이 좀 느림 -^> 둘 다 영향
echo  200 ~ 500 ms : 망이 원인 (확정) -^> 라우터 통합이 1순위
echo   응답 없음   : ICMP 차단 -^> 2번 스크립트로 HTTP 측정
echo.
echo  홉 수 4 이상이면 통신사 망을 타고 나갔다 오는 것입니다.
echo ============================================================
echo.
echo  결과 폴더: %OUTDIR%
echo  이 폴더를 통째로 챙겨 나오세요.
echo.
pause
