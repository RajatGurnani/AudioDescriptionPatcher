@echo off
rem Starts the AD Patcher desktop app (its own window, works on your files
rem in place - no upload or download). For phones use start_web.bat.
rem First run creates .venv and installs dependencies automatically.
cd /d "%~dp0"
call "%~dp0tools\ensure_env.bat" || (pause & exit /b 1)
".venv\Scripts\python.exe" desktop.py
if errorlevel 1 pause
