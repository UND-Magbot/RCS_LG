@echo off
rem  RCS LG - restart server: stop backend/frontend, then run start_server.ps1
rem  Copy this file to the Desktop. Logic is in tools\server_restart.ps1 (batch must stay ASCII).
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrator\Desktop\RCS_LGIT\tools\server_restart.ps1"
