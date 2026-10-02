"""
TankTrouble CDP Edge Launcher (CDP 启动浏览器窗口)
====================================================
Launches an Edge window with remote debugging enabled, then opens a URL.

Key features:
  - Configurable CDP port + profile dir
  - Opens tanktrouble.com (or any URL)
  - Anti-freeze: Edge aggressively suspends background/inactive tabs to save
    memory. For a game tab this stops the game. This launcher:
      1. opens the URL in a NEW WINDOW (not a background tab)
      2. adds flags to discourage tab discarding/sleeping
      3. focuses the new window (via PowerShell) so the tab stays active

Usage:
  python launch_cdp.py [port] [url]
  python launch_cdp.py 9222
  python launch_cdp.py 9223 "https://tanktrouble.com/game"
"""
import os
import subprocess
import sys
import time

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE_BASE = r"C:\tt-cdp"          # 不能含空格！
PORT_DEFAULT = 9222
URL_DEFAULT = "https://tanktrouble.com/game"

# 降低游戏标签页冻结 / 后台挂起的 flags。
#（行为可能随 Edge 版本而异；即使被忽略也无害。）
ANTI_FREEZE_FLAGS = [
    "--disable-backgrounding-occluded-windows",   # 不限制被遮挡的隐藏窗口
    "--disable-renderer-backgrounding",           # 保持渲染进程活跃
    "--disable-background-timer-throttling",      # 后台不限制定时器
    "--disable-features=IntensiveWakeUpThrottling",
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-features=msTabSleeping",           # 关闭 Edge 标签页睡眠
    "--no-first-run",
    "--no-default-browser-check",
]


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT_DEFAULT
    url = sys.argv[2] if len(sys.argv) > 2 else URL_DEFAULT
    profile_dir = os.path.join(PROFILE_BASE, f'p{port - 9222}') if port >= 9222 else os.path.join(PROFILE_BASE, f'p{port}')

    os.makedirs(profile_dir, exist_ok=True)
    args = [
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile_dir}',
        *ANTI_FREEZE_FLAGS,
        '--new-window',
        url,
    ]
    print(f'[launch] Edge CDP :{port} profile={profile_dir}', flush=True)
    print(f'[launch] url: {url}', flush=True)
    subprocess.Popen([EDGE] + args)

    # 等待 CDP 就绪
    import urllib.request
    import json
    ready = False
    for i in range(40):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=2) as r:
                info = json.loads(r.read().decode())
                print(f'[launch] CDP ready on :{port} ({info.get("Browser", "?")})', flush=True)
                ready = True
                break
        except Exception:
            time.sleep(0.5)

    # 聚焦新 Edge 窗口，让游戏标签页保持 ACTIVE（Edge 会冻结不可见/
    # 后台标签页 -> 游戏暂停）。把匹配的窗口带到前台。
    if ready:
        time.sleep(1.5)
        try:
            ps = (
                "Add-Type -AssemblyName Microsoft.VisualBasic;"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$p = Get-Process msedge -ErrorAction SilentlyContinue | "
                "  Where-Object { $_.MainWindowTitle -match 'TankTrouble|game' } | "
                "  Select-Object -Last 1;"
                "if ($p) { [Microsoft.VisualBasic.Interaction]::AppActivate($p.Id) }"
            )
            subprocess.run(['powershell', '-NoProfile', '-Command', ps], timeout=8)
            print('[launch] focused game window', flush=True)
        except Exception:
            pass
    return 0 if ready else 1


if __name__ == '__main__':
    sys.exit(main())
