# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：
#   零容忍，可永久封号且无警告）。在线相关脚本另见文件内附加说明。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp_tools'))
from platform_utils import find_browser, profile_base, no_sandbox_flags, is_windows  # noqa: E402

EDGE = find_browser()
PROFILE_BASE = profile_base()
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
EXTRA_FLAGS = no_sandbox_flags()   # Linux root 自动 --no-sandbox（或 TT_CHROME_NO_SANDBOX=1）


def main():
    if '--check' in sys.argv:
        print('[check] platform =', sys.platform)
        print('[check] browser  =', EDGE)
        print('[check] exists   =', bool(EDGE and os.path.exists(EDGE)))
        print('[check] profile  =', PROFILE_BASE)
        print('[check] flags    =', EXTRA_FLAGS)
        if not EDGE:
            print('[check] ❌ 未找到 Edge/Chrome/Chromium；请设置 TT_BROWSER 指向可执行文件')
            return 2
        return 0
    if not EDGE:
        print('[launch] ❌ 未找到浏览器，请设置 TT_BROWSER（Edge/Chrome/Chromium 路径）', flush=True)
        return 2
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else PORT_DEFAULT
    url = sys.argv[2] if len(sys.argv) > 2 else URL_DEFAULT
    profile_dir = os.path.join(PROFILE_BASE, f'p{port - 9222}') if port >= 9222 else os.path.join(PROFILE_BASE, f'p{port}')

    os.makedirs(profile_dir, exist_ok=True)
    args = [
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile_dir}',
        *ANTI_FREEZE_FLAGS,
        *EXTRA_FLAGS,
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
    if ready and is_windows():
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
