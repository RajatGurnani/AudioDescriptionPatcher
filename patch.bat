@echo off
rem Drag a video file and its audio-description file onto this .bat
rem (or run: patch.bat video.mp4 description.mp3)
rem First run creates .venv and installs dependencies automatically.
setlocal
call "%~dp0tools\ensure_env.bat" || (pause & exit /b 1)
"%~dp0.venv\Scripts\python.exe" "%~dp0adpatch.py" %*
echo.
pause
