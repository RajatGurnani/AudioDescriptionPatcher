@echo off
rem Makes sure .venv exists and has requirements.txt installed.
rem Called by start_app.bat and patch.bat; exit code 0 = ready.
rem Uses uv when it's on PATH (much faster, and can fetch Python itself),
rem otherwise the Python launcher / python + venv + pip.
setlocal
set "ROOT=%~dp0.."
set "VENV=%ROOT%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "REQ=%ROOT%\requirements.txt"
set "STAMP=%VENV%\.req-stamp"
set "UV="
where uv >nul 2>nul && set "UV=uv"

if exist "%VPY%" goto deps

rem --- create the venv -------------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if defined PY (
    echo First run: creating the Python environment in .venv ...
    %PY% -m venv "%VENV%" || goto fail
    goto deps
)
if defined UV (
    echo First run: Python not found - letting uv set one up in .venv ...
    uv venv --python 3.13 "%VENV%" || goto fail
    goto deps
)
echo.
echo   Python 3.10 or newer is required but was not found.
echo   Install it from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH" in the installer), then run this again.
exit /b 1

rem --- install / refresh dependencies --------------------------------
rem The stamp is a copy of the requirements.txt last installed, so pip
rem only runs on first launch or after requirements.txt changes.
:deps
fc /b "%REQ%" "%STAMP%" >nul 2>nul && goto check
call :install || goto fail
copy /y "%REQ%" "%STAMP%" >nul

:check
"%VPY%" -c "import numpy, imageio_ffmpeg, fastapi, uvicorn, python_multipart" >nul 2>nul && exit /b 0
echo Some dependencies are missing or broken - reinstalling ...
call :install || goto fail
"%VPY%" -c "import numpy, imageio_ffmpeg, fastapi, uvicorn, python_multipart" || goto fail
exit /b 0

:install
echo Installing dependencies (needs internet the first time) ...
if defined UV (
    uv pip install --python "%VPY%" -r "%REQ%"
) else (
    "%VPY%" -m pip install --disable-pip-version-check -r "%REQ%"
)
exit /b %errorlevel%

:fail
echo.
echo   Setup failed - see the messages above.
exit /b 1
