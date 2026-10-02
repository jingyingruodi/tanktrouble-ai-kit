@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 启动 CDP 浏览器窗口
REM  双击即用：打开启用了远程调试的 Edge 游戏窗口
REM
REM  可调整：
REM    PORT      CDP 端口（默认 9222）
REM    URL       要打开的网址
REM =====================================================
setlocal

REM ---------- 配置区 ----------
set PORT=9222
set URL=https://tanktrouble.com/game
REM ----------------------------

cd /d "D:\AI Agent\tanktrouble_ai"

echo ========================================
echo  TankTrouble AI — CDP 浏览器启动
echo  端口: %PORT%   网址: %URL%
echo ========================================
echo.

python launch_cdp.py %PORT% "%URL%"

echo.
echo [提示] 已启动。用 python tt.py list 查看实例。
echo        防止冻结：游戏窗口请保持在前台，或最大化。
echo.
pause
