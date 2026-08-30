@echo off
cd /d %~dp0
start "" http://localhost:8770
python server.py
pause
