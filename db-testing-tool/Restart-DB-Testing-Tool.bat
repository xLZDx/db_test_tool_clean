@echo off
setlocal

set "RESTART_PS1=%~dp0Restart-DB-Testing-Tool.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%RESTART_PS1%'"
exit /b %errorlevel%

endlocal
