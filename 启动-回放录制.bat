@echo off
chcp 65001 >nul
REM =====================================================
REM  TankTrouble AI — 录制回放（一键启动）
REM  双击即用：播放指定录制文件到 9314 端口
REM  监控室（点"🔍自动发现"后）格子 "▶播放:9314" 显示回放
REM  或独立页面 tanktrouble_playback.html
REM =====================================================
setlocal

REM ---------- 配置区 ----------
set REC_FILE=D:\AI Agent\tanktrouble_ai\data\fire_demo.jsonl
set PLAY_PORT=9314
set PLAY_FPS=20
REM ----------------------------

cd /d "D:\AI Agent\tanktrouble_ai"

echo ========================================
echo  TankTrouble AI — 录制回放
echo  录制: %REC_FILE%
echo  端口: %PLAY_PORT%   帧率: %PLAY_FPS%
echo ========================================
echo.

if not exist "%REC_FILE%" (
    echo [错误] 录制文件不存在: %REC_FILE%
    echo 请先运行 "启动-持续自杀录数据.bat" 生成数据，或修改 REC_FILE 路径。
    echo.
    pause
    exit /b 1
)

echo [启动] 回放服务器（Ctrl+C 停止）...
echo [提示] 监控室点"🔍自动发现" → 格子 "▶播放:%PLAY_PORT%" 显示回放
echo.
python playback_server.py "%REC_FILE%" %PLAY_PORT% --fps %PLAY_FPS% --loop

echo.
echo [结束] 回放已停止。
pause

