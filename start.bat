@echo off
rem 双击启动 job-stock：起服务并自动打开浏览器。
rem 已经有一个实例在跑时不会报错，只把页面重新打开。
cd /d %~dp0
python server.py
pause
