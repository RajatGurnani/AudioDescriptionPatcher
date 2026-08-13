@echo off
rem Starts the AD Patcher web app. Open the printed URL on this PC or
rem scan the QR code with your phone (same Wi-Fi network).
cd /d "%~dp0"
".venv\Scripts\python.exe" app.py
pause
