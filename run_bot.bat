@echo off
REM Double-click to start the bot. It restarts automatically if it ever stops.
cd /d "%~dp0"
:loop
python main.py run
echo Bot stopped. Restarting in 30 seconds... (close this window to quit)
timeout /t 30 /nobreak >nul
goto loop
