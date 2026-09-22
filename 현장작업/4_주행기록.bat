@echo off
chcp 949 > nul
title [4] 주행 기록 - 멈칫마다 스페이스바
cd /d "%~dp0.."
call "%~dp0config.bat"

set CYCLES=2
set /p CYCLES=기록할 사이클 수 [%CYCLES%]:

echo.
echo ============================================================
echo  [4] 주행 기록
echo ============================================================
echo.
echo  로봇      : %ROBOT_IP%
echo  사이클    : %CYCLES% 회
echo  백엔드로그: BackEnd\_logs\backend.log
echo.
echo ------------------------------------------------------------
echo  ** 기록 중에 할 일 **
echo.
echo    이 창을 맨 앞에 두고,
echo    불편한 정지를 볼 때마다  [스페이스바]  를 누르세요.
echo.
echo    사람이 느끼고 누르는 지연(0.9~4.2초)까지 보정해서
echo    요약에 "사용자가 표시한 멈칫" 으로 따로 나옵니다.
echo.
echo    이 표시가 있어야 자동 분류의 "미설명" 과 대조해서
echo    진짜 미제를 좁힐 수 있습니다.
echo ------------------------------------------------------------
echo.
echo  준비되면 아무 키나 누르고, 콘솔에서 작업을 시작하세요.
pause

"BackEnd\venv\Scripts\python.exe" scripts\drive_log.py --ip %ROBOT_IP% --cycles %CYCLES% --server %BACKEND% --backend-log "%CD%\BackEnd\_logs\backend.log"

echo.
echo  기록이 끝났습니다. _logs 폴더에 사이클별로 저장됐습니다.
echo  6번으로 모아서 챙기세요.
pause
