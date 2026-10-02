@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 持续自杀 + 数据录制（一键启动）
REM  ---------------------------------------------------
REM  双击即用：启动监控室 + 所有可控角色持续自杀 + 录制数据
REM
REM  可调整项（改下面的变量即可）：
REM    TT_PORT       CDP 端口（默认 9222）
REM    REC_FILE      录制文件路径
REM    REC_FPS       录制帧率（默认 20）
REM    COMPRESS      压缩录制（1=是 0=否，地形只在回合切换写）
REM    MONITOR       是否启动监控室（1=是 0=否）
REM    PLAYBACK      是否在结束后回放录制（1=是 0=否）
REM =====================================================
setlocal

REM ---------- 配置区（改这里） ----------
set TT_PORT=9222
set REC_FILE=D:\AI Agent\tanktrouble_ai\data\record.jsonl
set REC_FPS=20
set COMPRESS=1
set MONITOR=0
set PLAYBACK=0
REM --------------------------------------

cd /d "D:\AI Agent\tanktrouble_ai"

REM 创建数据目录
if not exist "D:\AI Agent\tanktrouble_ai\data" mkdir "D:\AI Agent\tanktrouble_ai\data"

echo ========================================
echo  TankTrouble AI — 持续自杀 + 录制
echo  端口: %TT_PORT%   录制: %REC_FILE%
echo ========================================
echo.

REM 可选：启动监控室（后台窗口）
if "%MONITOR%"=="1" (
    echo [启动] 监控室 monitor_server...
    start "TT-Monitor" cmd /c "cd /d D:\AI Agent\tanktrouble_ai && python monitor_server.py 3 9222 --tm 1"
    timeout /t 3 /nobreak >nul
    echo [提示] 浏览器打开 tanktrouble_monitor.html 查看监控画面（自动发现，回放端口9314）
    echo.
)

echo [启动] 持续自杀 + 录制...
echo [提示] 按 Ctrl+C 停止
echo.
cd /d "D:\AI Agent\tanktrouble_ai"
if "%COMPRESS%"=="1" (
    python scripts\loop_suicide.py %TT_PORT% --record "%REC_FILE%" --fps %REC_FPS% --compress
) else (
    python scripts\loop_suicide.py %TT_PORT% --record "%REC_FILE%" --fps %REC_FPS%
)

echo.
echo [结束] 脚本已停止。录制数据在: %REC_FILE%

REM 可选：回放录制数据（独立端口 9314，与监控共存）
if "%PLAYBACK%"=="1" (
    echo.
    echo [启动] 回放录制数据...
    echo [提示] 监控室刷新后，格子 "▶播放:9314" 即显示回放。Ctrl+C 停止回放。
    echo.
    python playback_server.py "%REC_FILE%" 9314 --fps %REC_FPS% --loop
)
pause

