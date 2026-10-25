@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 重启监控室（休眠/唤醒后恢复用）
REM  用途：电脑休眠唤醒后 monitor_server 进程失效时，
REM        双击本脚本即可恢复监控室（无需重启浏览器）。
REM
REM  行为：
REM    1. 检查 CDP 浏览器（9222/9223/9224）——缺失的才启动
REM    2. 杀掉残留的 monitor_server / playback_server
REM    3. 启动新的 monitor_server
REM    4. 打开监控室页面
REM
REM  注意：不会动自杀脚本 / 录制脚本（只恢复"监控"相关进程）。
REM =====================================================
setlocal

cd /d "%~dp0"

echo ========================================
echo  TankTrouble AI — 重启监控室
echo  （休眠唤醒恢复专用）
echo ========================================
echo.

REM ---------- 1. 检查/启动 CDP 浏览器 ----------
REM ---------- 配置区（可用环境变量覆盖） ----------
set CDP_PORTS=9222 9223 9224
set MONITOR_CDP=9222
set MONITOR_COUNT=3
set MONITOR_TM=1
if not "%TT_CDP_PORT%"=="" (
    set CDP_PORTS=%TT_CDP_PORT%
    set MONITOR_CDP=%TT_CDP_PORT%
    set MONITOR_COUNT=1
)
REM --------------------------------------

echo [1/4] 检查 CDP 浏览器实例...
python scripts\ensure_browsers.py %CDP_PORTS%
ping -n 3 127.0.0.1 >nul

REM ---------- 2. 清理残留监控进程 ----------
echo [2/4] 清理残留监控进程...
python scripts\kill_monitor.py
ping -n 3 127.0.0.1 >nul

REM ---------- 3. 启动 monitor_server ----------
echo [3/4] 启动 monitor_server（3 CDP + 1 TM）...
start "TT-Monitor" cmd /k "python monitor_server.py %MONITOR_COUNT% %MONITOR_CDP% --tm %MONITOR_TM%"

ping -n 5 127.0.0.1 >nul

REM ---------- 4. 打开监控室页面 ----------
echo [4/4] 打开监控室页面...
if not "%TT_SKIP_OPEN%"=="1" start "" "%~dp0tanktrouble_monitor.html"

echo.
echo [提示] 监控室已重启。
echo        若同时想恢复回放，另开窗口运行:
echo           python playback_server.py data\xxx.jsonl 9314 --loop
echo        自杀/录制脚本不受影响（未动）。
echo.
pause
