@echo off
chcp 949 > nul
title [A1] 사용폴더를 git 저장소로 - 백업 + .git 연결
setlocal EnableDelayedExpansion

rem ============================================================
rem  업로드폴더의 .git 만 사용폴더로 옮긴다.
rem  git init + checkout 은 절대 쓰지 않는다 - 현장 수정분이 덮인다.
rem  .git 만 옮기면 작업 파일은 손대지 않은 채 연결만 붙는다.
rem
rem  ★ 이 bat 은 [업로드폴더]\현장작업\ 에서 실행한다.
rem ============================================================

cd /d "%~dp0.."
set UPDIR=%CD%

set CFG=%~dp0사용폴더경로.txt
set USEDIR=
if exist "%CFG%" set /p USEDIR=<"%CFG%"

echo.
echo ============================================================
echo  [A1] 사용폴더를 git 저장소로 - 1단계
echo ============================================================
echo.
echo  업로드폴더(여기) : %UPDIR%
echo.
if "%USEDIR%"=="" (
  echo  사용폴더 = 실제로 백엔드가 돌아가는 폴더
  set /p USEDIR=사용폴더 전체 경로:
) else (
  echo  사용폴더(저장됨) : %USEDIR%
  echo.
  set /p YN=이 경로가 맞습니까? [Y/n]:
  if /i "!YN!"=="n" set /p USEDIR=사용폴더 전체 경로:
)

if not exist "!USEDIR!\BackEnd" (
  echo.
  echo  [오류] !USEDIR!\BackEnd 가 없습니다. 경로를 확인하세요.
  del "%CFG%" 2>nul
  pause & exit /b 1
)
echo !USEDIR!>"%CFG%"

rem ── 이미 .git 이 있으면 중단 ─────────────────────────────
if exist "!USEDIR!\.git" (
  echo.
  echo  [중단] 사용폴더에 이미 .git 이 있습니다.
  echo         이미 전환됐거나 다른 저장소입니다. 확인이 먼저입니다.
  echo.
  pushd "!USEDIR!"
  git log --oneline -3
  git remote -v
  popd
  echo.
  pause & exit /b 1
)

rem ── 업로드폴더 git 상태 기록 ─────────────────────────────
echo.
echo ------------------------------------------------------------
echo  업로드폴더의 현재 git 상태 (복사 후 이것과 같아야 합니다)
echo ------------------------------------------------------------
git log --oneline -3
echo.
git remote -v
echo.
echo ------------------------------------------------------------
echo.
echo  이 화면을 기억하거나 캡처해 두세요.
echo.
pause

rem ── 1) 백업 ──────────────────────────────────────────────
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set BKZIP=%USERPROFILE%\Desktop\사용폴더백업_%TS%.zip

echo.
echo ============================================================
echo  [1/3] 사용폴더 백업
echo ============================================================
echo.
echo  만들 파일: %BKZIP%
echo  venv / node_modules / .next / __pycache__ 는 제외합니다.
echo  몇 분 걸릴 수 있습니다.
echo.

set BKTMP=%TEMP%\usebk_%TS%
robocopy "!USEDIR!" "%BKTMP%" /E /XD venv node_modules .next __pycache__ _logs /NFL /NDL /NJH /NJS /nc /ns /np
powershell -NoProfile -Command "Compress-Archive -Path '%BKTMP%\*' -DestinationPath '%BKZIP%' -Force"
rmdir /S /Q "%BKTMP%" 2>nul

if not exist "%BKZIP%" (
  echo.
  echo  [중단] 백업이 안 만들어졌습니다. 여기서 멈춥니다.
  pause & exit /b 1
)
powershell -NoProfile -Command "$f=Get-Item '%BKZIP%'; Write-Host ('  백업 완료: ' + [math]::Round($f.Length/1MB,1) + ' MB')"

rem ── 2) .git 복사 ─────────────────────────────────────────
echo.
echo ============================================================
echo  [2/3] .git 복사  (작업 파일은 건드리지 않습니다)
echo ============================================================
echo.
robocopy "%UPDIR%\.git" "!USEDIR!\.git" /E /COPYALL /NFL /NDL /NJH /NJS /nc /ns /np
if not exist "!USEDIR!\.git\HEAD" (
  echo.
  echo  [중단] .git 복사 실패. HEAD 파일이 없습니다.
  pause & exit /b 1
)
echo   복사 완료

rem ── 3) 차이 확인 ─────────────────────────────────────────
echo.
echo ============================================================
echo  [3/3] 차이 확인  ★ 여기서 멈춥니다
echo ============================================================
echo.
pushd "!USEDIR!"

echo --- git log (위에서 본 것과 같아야 합니다) ---
git log --oneline -3
echo.
echo --- 원격 ---
git remote -v
echo.

echo --- 수정된 파일 (M) ---
echo.
git status --short | findstr /R "^.M ^M"
echo.
echo --- 삭제된 것처럼 보이는 파일 (D) ---
git status --short | findstr /R "^.D ^D"
echo.

for /f %%c in ('git status --short ^| find /c /v ""') do set NTOTAL=%%c
for /f %%c in ('git status --short ^| findstr /R "^??" ^| find /c /v ""') do set NUNTRACKED=%%c
echo --- 개수 ---
echo   전체 차이   : %NTOTAL%
echo   추적 안 됨  : %NUNTRACKED%   (venv/node_modules 등. A2 에서 정리)
echo.

git diff --stat > "%~dp0차이_%TS%.txt" 2>&1
git status --short >> "%~dp0차이_%TS%.txt" 2>&1
git diff > "%~dp0현장수정분_%TS%.patch" 2>&1
popd

echo ============================================================
echo  ★ 지금 반드시 확인할 것
echo ------------------------------------------------------------
echo.
echo   위 [M] 목록에 소스 코드(.py .html .tsx)가 있습니까?
echo.
echo     있다  -^> 현장에서 고친 것일 수 있습니다.
echo              내용을 보고 살릴지 정하세요. 확인 전에는 A2 로 넘어가지 마세요.
echo              (판단이 어려우면 일단 살립니다. 날아간 건 못 되살립니다)
echo.
echo     없다  -^> default_area.json / poi_labels.json / maps 만 다르면 정상입니다.
echo              A2 로 넘어가세요.
echo.
echo   내용을 보려면 사용폴더에서:
echo     git diff BackEnd\app\services\dispatch_service.py
echo.
echo ------------------------------------------------------------
echo  자동 저장됨 (만약을 대비한 백업)
echo    차이 요약 : 현장작업\차이_%TS%.txt
echo    패치 파일 : 현장작업\현장수정분_%TS%.patch
echo    폴더 백업 : %BKZIP%
echo ============================================================
echo.
pause
