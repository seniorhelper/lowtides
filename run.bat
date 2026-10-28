@echo off
REM Start Lowtides on http://localhost:8787
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m lowtides.server & goto :eof)
python -m lowtides.server
pause
