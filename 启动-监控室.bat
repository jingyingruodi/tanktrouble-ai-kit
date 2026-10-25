@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 监控室一键启动
REM  双击即用：先启动 3 个 CDP 浏览器（9222,9223,9224）
REM  再启动 monitor_server（CDP 轮询 + TM 推送），
REM  然后自动打开浏览器监控室页面
REM  如需同时播放录制回放，另开窗口运行:
REM    python playback_server.py <录制.jsonl> 9314 --loop
REM =====================================================
setlocal

cd /d "%~dp0"

echo ========================================
echo  TankTrouble AI — 监控室
echo  实例: 3 CDP (9222,9223,9224) + 1 TM
echo  状态端口: 9310/9311/9312/9313
echo  回放端口: 9314 (可选)
echo ========================================
echo.

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

REM ---------- 1. 启动 CDP 浏览器（未启动的才启动） ----------
echo [1/3] 检查/启动 CDP 浏览器实例...
python scripts\ensure_browsers.py %CDP_PORTS%
ping -n 3 127.0.0.1 >nul

REM ---------- 2. 启动 monitor_server ----------
echo [2/3] 启动 monitor_server...
start "TT-Monitor" cmd /k "python monitor_server.py %MONITOR_COUNT% %MONITOR_CDP% --tm %MONITOR_TM%"

ping -n 5 127.0.0.1 >nul

REM ---------- 3. 打开监控室页面 ----------
echo [3/3] 打开监控室页面...
if not "%TT_SKIP_OPEN%"=="1" start "" "%~dp0tanktrouble_monitor.html"

echo.
echo [提示] 页面已打开。点 "🔍自动发现" 自动配置实例数。
echo        回放: 另开窗口运行  python playback_server.py data.jsonl 9314 --loop
echo        停止监控：关闭 "TT-Monitor" 窗口即可。
echo        若某实例浏览器未启动，监控室会显示"离线"并自动重连。
echo.
pause

