@echo off
REM Start CostCop on http://localhost:8787
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -m costcop.server & goto :eof)
python -m costcop.server
pause
