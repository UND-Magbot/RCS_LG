@echo off
chcp 949 > nul
title [0] 업로드폴더 -^> 사용폴더 동기화
setlocal EnableDelayedExpansion

rem ============================================================
rem  서버PC 는 폴더가 둘이다.
rem    업로드폴더 = git 연결 O  (여기서 pull 받는다)
rem    사용폴더   = git 연결 X  (실제로 돌아가는 것)
rem
rem  이 bat 은 [업로드폴더] 안의 현장작업\ 에서 실행한다.
rem  pull 로 받은 것 중 필요한 것만 사용폴더로 복사한다.
rem ============================================================

cd /d "%~dp0.."
set SRC=%CD%

set CFG=%~dp0사용폴더경로.txt
set USEDIR=
if exist "%CFG%" set /p USEDIR=<"%CFG%"

echo.
echo ============================================================
echo  [0] 서버PC 동기화
echo ============================================================
echo.
echo  업로드폴더(여기) : %SRC%
if not "%USEDIR%"=="" echo  사용폴더(저장됨) : %USEDIR%
echo.

if "%USEDIR%"=="" (
  echo  사용폴더 = 실제로 백엔드가 돌아가는 폴더입니다.
  echo  예^) C:\RCS_LG  또는  D:\운영\RCS_LG
  echo.
  set /p USEDIR=사용폴더 전체 경로:
)

if not exist "!USEDIR!\BackEnd" (
  echo.
  echo  [오류] !USEDIR!\BackEnd 가 없습니다.
  echo         사용폴더 경로가 맞는지 확인하세요.
  echo.
  del "%CFG%" 2>nul
  pause
  exit /b 1
)

echo !USEDIR!>"%CFG%"

echo.
echo ------------------------------------------------------------
echo  복사할 것 (신규 파일만. 서버 코드는 안 바뀌었습니다)
echo ------------------------------------------------------------
echo    scripts\load_test.py
echo    tools\robot_monitor.html
echo    현장작업\  (폴더 통째)
echo.
echo ------------------------------------------------------------
echo  절대 복사하지 않는 것
echo ------------------------------------------------------------
echo    BackEnd\default_area.json        현재 사용 맵 번호
echo    BackEnd\static\robot_speed.json  로봇별 속도
echo    BackEnd\config.env               DB 접속정보
echo    BackEnd\static\maps\             맵 이미지
echo.
echo    ^(덮으면 로봇이 엉뚱한 좌표로 갑니다^)
echo.
pause

echo.
echo [1/3] scripts\load_test.py ...
if not exist "!USEDIR!\scripts" mkdir "!USEDIR!\scripts"
copy /Y "%SRC%\scripts\load_test.py" "!USEDIR!\scripts\" >nul && echo       완료 || echo       [실패]

echo [2/3] tools\robot_monitor.html ...
if not exist "!USEDIR!\tools" mkdir "!USEDIR!\tools"
copy /Y "%SRC%\tools\robot_monitor.html" "!USEDIR!\tools\" >nul && echo       완료 || echo       [실패]

echo [3/3] 현장작업\ ...
if not exist "!USEDIR!\현장작업" mkdir "!USEDIR!\현장작업"
xcopy /Y /I /Q "%SRC%\현장작업\*.bat" "!USEDIR!\현장작업\" >nul && echo       완료 || echo       [실패]
xcopy /Y /I /Q "%SRC%\현장작업\*.txt" "!USEDIR!\현장작업\" >nul

echo.
echo ============================================================
echo  결과 확인
echo ============================================================
if exist "!USEDIR!\scripts\load_test.py"      (echo   [O] load_test.py)      else (echo   [X] load_test.py)
if exist "!USEDIR!\tools\robot_monitor.html"  (echo   [O] robot_monitor.html) else (echo   [X] robot_monitor.html)
if exist "!USEDIR!\현장작업\1_망측정.bat"      (echo   [O] 현장작업 bat)      else (echo   [X] 현장작업 bat)
echo.
echo  이제 사용폴더의 현장작업\ 에서 작업하세요:
echo    !USEDIR!\현장작업\
echo.
echo  다음: config.bat 에 로봇 IP 확인 -^> 현장 들어가서 1_망측정.bat
echo.
pause
