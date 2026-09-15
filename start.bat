@echo off
rem Jobstock launcher for Windows. Keep this file ASCII-only:
rem cmd.exe parses .bat files through the system OEM codepage (GBK on
rem zh-CN), and multi-byte lines can be mis-split into garbage commands.
rem All Chinese user-facing text lives in the Python scripts.
cd /d %~dp0
rem First run (no config.json yet): create data dirs, config and index.
if not exist config.json (
    python install.py
    if errorlevel 1 (
        echo install.py did not finish - see the message above, then retry.
        pause
        exit /b 1
    )
)
python server.py
pause
