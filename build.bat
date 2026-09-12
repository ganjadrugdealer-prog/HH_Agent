@echo off
set PYTHONIOENCODING=utf-8
set APPDIR=%~dp0
set VPY=%APPDIR%.venv\Scripts\python.exe

if not exist "%VPY%" (
    echo Virtual environment not found. Please create it and install requirements.
    exit /b 1
)

cd /d "%APPDIR%"
echo === building ===
"%VPY%" -m PyInstaller --noconfirm --clean hh_agent.spec
echo === result ===
dir /b "%APPDIR%dist"
for %%F in ("%APPDIR%dist\HH-Agent.exe") do echo SIZE_BYTES=%%~zF
echo DONE
