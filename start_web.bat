@echo off
rem Starts the AD Patcher web app for phones. Open the printed URL on this PC or
rem scan the QR code with your phone (same Wi-Fi network).
rem First run creates .venv and installs dependencies automatically.
cd /d "%~dp0"
call "%~dp0tools\ensure_env.bat" || (pause & exit /b 1)
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
