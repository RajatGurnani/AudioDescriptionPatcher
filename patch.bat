@echo off
rem Drag a video file and its audio-description file onto this .bat
rem (or run: patch.bat video.mp4 description.mp3)
setlocal
"%~dp0.venv\Scripts\python.exe" "%~dp0adpatch.py" %*
echo.
pause
