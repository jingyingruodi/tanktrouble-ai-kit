@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 仅录制（一键启动）
REM  双击即用：被动录制游戏状态（不控制任何坦克）
REM  用于：示范/演示/数据采集
REM =====================================================
setlocal

REM ---------- 配置区 ----------
set TT_PORT=9222
set REC_FILE=%~dp0data\demo3.jsonl
set REC_FPS=60
set COMPRESS=1
REM   COMPRESS=1 压缩（地形只在回合切换写），0=全量
REM ----------------------------

cd /d "%~dp0"
if not exist "%~dp0data" mkdir "%~dp0data"

echo ========================================
echo  TankTrouble AI — 仅录制
echo  端口: %TT_PORT%   录制: %REC_FILE%
echo  帧率: %REC_FPS%   压缩: %COMPRESS%
echo ========================================
echo.
echo [提示] 按 Ctrl+C 停止
echo.

if "%COMPRESS%"=="1" (
    python scripts\record_only.py %TT_PORT% --out "%REC_FILE%" --fps %REC_FPS% --compress
) else (
    python scripts\record_only.py %TT_PORT% --out "%REC_FILE%" --fps %REC_FPS%
)

echo.
echo [结束] 录制已停止: %REC_FILE%
echo [可选] 回放: 启动-回放录制.bat  (或 python playback_server.py "%REC_FILE%" 9314 --loop)
pause
