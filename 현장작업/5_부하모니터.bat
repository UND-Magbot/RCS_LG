@echo off
chcp 949 > nul
title [5] 부하 / 멈칫 모니터
cd /d "%~dp0.."
call "%~dp0config.bat"

echo.
echo ============================================================
echo  [5] 부하 / 멈칫 모니터
echo ============================================================
echo.
echo  브라우저 창이 열립니다. 로봇 IP 를 넣고 [연결] 하세요.
echo.
echo    로봇 IP: %ROBOT_IP%
echo.
echo  읽기 전용입니다. 로봇에 명령을 보내지 않습니다.
echo.
echo ------------------------------------------------------------
echo  ** 쓰는 순서 **
echo    1) 부하 단계를 [0 끔] 으로 두고 몇 바퀴  (기준선)
echo    2) load average 경고가 실제로 뜨는지 확인
echo         사무실 crawler 에서는 3단계에서도 0건이었습니다.
echo         현장 longjack 에서 뜨는지가 관건입니다.
echo    3) 경고가 뜨면 단계를 올려가며 비교
echo.
echo  주의: 4번 주행기록을 같이 켜면 그 자체가 부하 1~2단계입니다.
echo        기준선을 잴 때 감안하세요.
echo ============================================================
echo.

start "" "%CD%\tools\robot_monitor.html"

echo  브라우저가 안 열리면 이 파일을 직접 여세요:
echo    %CD%\tools\robot_monitor.html
echo.
pause
