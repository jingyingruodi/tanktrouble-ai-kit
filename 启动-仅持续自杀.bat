@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 仅持续自杀（一键启动，不录制）
REM  双击即用：所有可控角色持续自杀，用于观战
REM  可调整：TT_PORT（CDP 端口）
REM  说明：在线模式自动走 setInputState（无回弹/开火有效）
REM =====================================================
setlocal

REM ---------- 配置区 ----------
set TT_PORT=9223
REM ----------------------------

cd /d "%~dp0"

echo ========================================
echo  TankTrouble AI — 持续自杀（不录制）
echo  端口: %TT_PORT%   模式: 在线安全(setInputState)
echo ========================================
echo.
echo [提示] 按 Ctrl+C 停止
echo.

python scripts\loop_suicide.py %TT_PORT%

echo.
echo [结束] 脚本已停止。
pause
