@echo off
cd /d %~dp0
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate
pip install -r requirements.txt -q
:loop
python -m roshd_bot.main
if errorlevel 3 exit /b
echo Bot stopped. Restarting in 10 seconds... (close this window to quit)
timeout /t 10 >nul
goto loop
