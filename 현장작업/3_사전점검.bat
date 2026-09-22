@echo off
chcp 949 > nul
title [3] 30초 사전 점검 - 로그가 실제로 남는지
cd /d "%~dp0.."
call "%~dp0config.bat"

echo.
echo ============================================================
echo  [3] 30초 사전 점검
echo ============================================================
echo.
echo  주행 기록 전에 반드시 이것부터 하세요.
echo.
echo  왜 필요한가:
echo    09-18 : motion.jsonl 이 0~1 줄 (데이터 없음)
echo    09-21 : 사이클 폴더 자체가 0개
echo    -^> 속도 데이터가 없는 상태로 원인을 찾고 있었습니다.
echo.
echo    ebf8084 에서 고쳤지만 아직 실기 검증이 안 됐습니다.
echo    이 점검이 그 검증입니다.
echo.
echo  로봇: %ROBOT_IP%
echo ------------------------------------------------------------
echo.
echo  확인할 것 (하나라도 0%% 면 주행하지 마세요):
echo    [ ] /motion_metrics       속도 - 제일 중요
echo    [ ] /tracked_pose         위치
echo    [ ] /planning_state       90%% 이상
echo    [ ] /scan_matched_points2 라이다
echo    [ ] rest_rtt              통신 왕복시간
echo.
pause

"BackEnd\venv\Scripts\python.exe" scripts\drive_log.py --ip %ROBOT_IP% --probe --server %BACKEND%

echo.
echo ============================================================
echo  위 결과에서 수신율이 0%% 인 항목이 있습니까?
echo.
echo    있다  -^> 여기서 멈추세요. 주행해도 빈 데이터만 쌓입니다.
echo    없다  -^> 4번으로 넘어가세요.
echo ============================================================
pause
