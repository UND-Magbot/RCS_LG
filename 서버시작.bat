@echo off
chcp 65001 > nul
setlocal

rem === RCS_LG 서버 시작 스크립트 ===
rem
rem  %~dp0 = 이 bat 파일이 있는 폴더. 경로가 박혀 있지 않으므로
rem  폴더를 옮기거나 이름을 바꿔도 그대로 동작한다.
rem
rem  [주의] 이 파일은 반드시 CRLF 줄바꿈으로 저장할 것. LF 로 저장하면
rem         cmd 가 줄을 엉뚱한 위치에서 잘라 "'S_LG'은(는) 내부 또는 외부
rem         명령이 아닙니다" 같은 오류가 쏟아진다. (한글 인코딩 문제가 아니다)
rem  [주의] 인코딩은 UTF-8(BOM 없음) + 위의 chcp 65001 조합이면 된다.
rem         BOM 을 붙이면 cmd 가 첫 줄(@echo off)을 못 읽는다.

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

echo ============================================
echo  RCS_LG 서버 시작
echo  위치: %ROOT%
echo ============================================
echo.

rem --- 1) 백엔드 (FastAPI :8002) ---
if not exist "%ROOT%\BackEnd\venv\Scripts\python.exe" goto no_venv
echo [1/2] 백엔드 시작...
start "RCS Backend" powershell -NoExit -ExecutionPolicy Bypass -File "%ROOT%\BackEnd\run_local.ps1"

rem --- 2) 프론트 (Next.js :3000) ---
rem  BackEnd\web 폴더가 있으면 백엔드가 8002 에서 관제 화면까지 서빙하므로
rem  프론트를 따로 띄우지 않는다. (포트 통합 모드 - 현재 이 모드)
if exist "%ROOT%\BackEnd\web\index.html" goto unified
if not exist "%ROOT%\frontend\node_modules" goto no_modules
if not exist "%ROOT%\frontend\.next" goto no_build

echo       백엔드가 뜰 때까지 10초 대기...
timeout /t 10 /nobreak > nul
echo [2/2] 프론트 시작...
start "RCS Frontend" cmd /k "cd /d "%ROOT%\frontend" && npm run start"
echo.
echo 완료. 브라우저에서 http://localhost:3000 으로 접속하세요.
goto done

:unified
echo [2/2] 포트 통합 모드 - 프론트를 따로 띄우지 않습니다.
echo.
echo 완료. 브라우저에서 http://localhost:8002 으로 접속하세요.
goto done

:no_venv
echo [오류] venv 가 없습니다.
echo        cd "%ROOT%\BackEnd"
echo        py -3.11 -m venv venv
pause
exit /b 1

:no_modules
echo [오류] frontend\node_modules 가 없습니다. 먼저 npm install 을 실행하세요.
pause
exit /b 1

:no_build
echo [오류] frontend\.next 가 없습니다. 먼저 npm run build 를 실행하세요.
pause
exit /b 1

:done
endlocal
