@echo off
chcp 65001 >nul
cd /d %~dp0
rem 网页版控制台: 双击后自动打开浏览器 http://127.0.0.1:8765
rem 注意: 后端进程与本窗口绑定, 关闭本窗口即关闭后端
python -X utf8 -u topwar_web.py
if errorlevel 1 pause
