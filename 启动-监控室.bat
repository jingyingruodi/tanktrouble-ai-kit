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

cd /d "D:\AI Agent\tanktrouble_ai"

echo ========================================
echo  TankTrouble AI — 监控室
echo  实例: 3 CDP (9222,9223,9224) + 1 TM
echo  状态端口: 9310/9311/9312/9313
echo  回放端口: 9314 (可选)
echo ========================================
echo.

REM ---------- 1. 启动 CDP 浏览器（未启动的才启动） ----------
echo [1/3] 检查/启动 CDP 浏览器实例...
python -c "import urllib.request,subprocess,os
alive=[]
for p in (9222,9223,9224):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{p}/json/version', timeout=1)
        alive.append(p)
    except Exception:
        pass
for p in (9222,9223,9224):
    if p not in alive:
        print(f'启动 CDP {p} ...')
        subprocess.Popen(['python','launch_cdp.py',str(p),'https://tanktrouble.com/game'], cwd=os.getcwd())
print('已就绪:', alive)
"
timeout /t 6 /nobreak >nul

REM ---------- 2. 启动 monitor_server ----------
echo [2/3] 启动 monitor_server...
start "TT-Monitor" cmd /k "cd /d D:\AI Agent\tanktrouble_ai && python monitor_server.py 3 9222 --tm 1"

timeout /t 4 /nobreak >nul

REM ---------- 3. 打开监控室页面 ----------
echo [3/3] 打开监控室页面...
start "" "D:\AI Agent\tanktrouble_monitor.html"

echo.
echo [提示] 页面已打开。点 "🔍自动发现" 自动配置实例数。
echo        回放: 另开窗口运行  python playback_server.py data.jsonl 9314 --loop
echo        停止监控：关闭 "TT-Monitor" 窗口即可。
echo        若某实例浏览器未启动，监控室会显示"离线"并自动重连。
echo.
pause

