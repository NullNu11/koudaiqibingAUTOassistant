@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   口袋奇兵 - 自动一键领取奖励
echo ============================================
python -u topwar_checkin.py
echo.
pause
