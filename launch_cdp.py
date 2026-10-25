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
TankTrouble CDP 浏览器启动器（Edge / Chrome / Chromium；Windows / macOS / Linux）
================================================================================
启动一个带远程调试（CDP）的浏览器窗口并打开指定 URL。

用法：
    python launch_cdp.py                     # 默认 :9222 打开游戏页
    python launch_cdp.py 9223                # 指定端口
    python launch_cdp.py about:blank         # 只开空白页（本地测试/合规）
    python launch_cdp.py 9222 about:blank
    python launch_cdp.py --check             # 只检查浏览器/路径/flags，不启动
    python launch_cdp.py 9222 --hold         # 就绪后保持前台直到浏览器退出
                                             #（xvfb-run 等场景必须用 --hold）

环境变量：
    TT_BROWSER              指定浏览器可执行文件
    TT_CDP_PROFILE_BASE     指定 profile 根目录
    TT_CHROME_NO_SANDBOX=1  强制 --no-sandbox（Linux root）

安全/健壮性：
  * 启动后轮询 `/json/version`（IPv4 → IPv6 → localhost），并检查子进程是否还活着；
    浏览器秒退不会再"假成功"。
  * 浏览器的 stdio 全部重定向到 DEVNULL，避免 `| tail` / `$(...)` / CI 抓日志时挂住。
  * 显式指定 TT_BROWSER 时会校验存在性与可执行权限，失败给出明确错误码（2）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp_tools'))
from platform_utils import (  # noqa: E402
    find_browser, profile_base, no_sandbox_flags, is_windows,
)

PORT_DEFAULT = 9222
URL_DEFAULT = 'https://tanktrouble.com/game'

# 降低游戏标签页冻结 / 后台挂起的 flags。
# 注意：同一 switch（--disable-features）重复传时后者覆盖前者，必须合并成一条。
ANTI_FREEZE_FLAGS = [
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-background-timer-throttling',
    '--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion,msTabSleeping',
    '--no-first-run',
    '--no-default-browser-check',
]

CDP_HOSTS = ('127.0.0.1', '[::1]', 'localhost')


def probe_version(port, timeout=2.0):
    """探测端口的 CDP 版本信息（IPv4/IPv6/localhost 都试）；不可达返回 None。"""
    for host in CDP_HOSTS:
        try:
            with urllib.request.urlopen('http://%s:%d/json/version' % (host, port),
                                        timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception:
            continue
    return None


def profile_dir_for(port):
    """profile 目录：9222..9321 用 p0..p99，其它端口用 p<port>。"""
    if PORT_DEFAULT <= port < PORT_DEFAULT + 100:
        name = 'p%d' % (port - PORT_DEFAULT)
    else:
        name = 'p%d' % port
    return os.path.join(profile_base(), name)


def _check_browser(path):
    if not path:
        return '未找到 Edge/Chrome/Chromium；请设置 TT_BROWSER 指向可执行文件'
    if not os.path.exists(path):
        return '浏览器路径不存在: %s' % path
    if not os.path.isfile(path):
        return '浏览器路径不是文件: %s' % path
    if not os.access(path, os.X_OK):
        return '浏览器文件不可执行: %s' % path
    return None


def build_args(port, url, profile_dir):
    return [
        '--remote-debugging-port=%d' % port,
        '--user-data-dir=%s' % profile_dir,
        *ANTI_FREEZE_FLAGS,
        *no_sandbox_flags(),
        '--new-window',
        url,
    ]


def focus_game_window():
    """Windows：把游戏窗口带到前台（避开 Edge 后台标签页冻结）。"""
    if not is_windows():
        return
    try:
        ps = ('Add-Type -AssemblyName Microsoft.VisualBasic;'
              'Add-Type -AssemblyName System.Windows.Forms;'
              '$p = Get-Process msedge -ErrorAction SilentlyContinue | '
              "  Where-Object { $_.MainWindowTitle -match 'TankTrouble|game' } | "
              '  Select-Object -Last 1;'
              'if ($p) { [Microsoft.VisualBasic.Interaction]::AppActivate($p.Id) }')
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], timeout=8)
    except Exception:
        pass


def parse_args(argv):
    ap = argparse.ArgumentParser(description='启动带 CDP 的浏览器窗口')
    ap.add_argument('port', nargs='?', default=None,
                    help='CDP 端口（默认 %d）；也可以只给 URL' % PORT_DEFAULT)
    ap.add_argument('url', nargs='?', default=None, help='要打开的 URL')
    ap.add_argument('--check', action='store_true', help='只检查环境，不启动浏览器')
    ap.add_argument('--hold', action='store_true',
                    help='就绪后保持前台直到浏览器退出（xvfb-run 场景必需）')
    ns = ap.parse_args(argv)

    port = PORT_DEFAULT
    url = URL_DEFAULT
    if ns.port is not None:
        if str(ns.port).isdigit():
            port = int(ns.port)
            url = ns.url or URL_DEFAULT
        else:
            # 第一个位置参数不是数字 → 视为 URL（默认端口）
            if ns.url:
                ap.error('端口无效且给出了两个 URL: %r %r' % (ns.port, ns.url))
            if ':' not in ns.port and '/' not in ns.port:
                ap.error('第一个参数既不是端口也不是 URL: %r' % ns.port)
            url = ns.port
    elif ns.url:
        url = ns.url
    if not (1 <= int(port) <= 65535):
        ap.error('端口超出范围 1..65535: %r' % port)
    return ns, port, url


def main():
    opts, port, url = parse_args(sys.argv[1:])
    browser = find_browser()
    err = _check_browser(browser)
    profile_dir = profile_dir_for(port)

    if opts.check:
        print('[check] platform   =', sys.platform)
        print('[check] browser    =', browser)
        print('[check] executable =', bool(browser and os.path.isfile(browser)
                                          and os.access(browser, os.X_OK)))
        print('[check] profile    =', profile_dir)
        print('[check] flags      =', no_sandbox_flags())
        print('[check] url        =', url)
        if err:
            print('[check] ❌', err)
            return 2
        print('[check] ✅ 环境可用')
        return 0

    if err:
        print('[launch] ❌', err, flush=True)
        return 2

    os.makedirs(profile_dir, exist_ok=True)
    args = build_args(port, url, profile_dir)
    print('[launch] browser=%s' % browser, flush=True)
    print('[launch] CDP :%d profile=%s url=%s' % (port, profile_dir, url), flush=True)

    # Popen 前记录端口指纹：若端口上已有旧的 CDP，必须确认"新实例接管了它"
    pre = probe_version(port, timeout=0.5)
    pre_ws = (pre or {}).get('webSocketDebuggerUrl')
    if pre:
        print('[launch] ⚠️ 端口 :%d 已有 CDP 在监听（%s）；将校验是否由本次实例接管'
              % (port, pre.get('Browser', '?')), flush=True)

    popen_kw = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                'stderr': subprocess.DEVNULL}
    if not is_windows():
        popen_kw['start_new_session'] = True
    proc = subprocess.Popen([browser] + args, **popen_kw)

    def _fail(msg):
        print('[launch] ❌ %s' % msg, flush=True)
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        return 1

    # 等待 CDP 就绪：必须"子进程活着"且"端口上的 CDP 是本次实例的"
    info = None
    for _ in range(120):
        if proc.poll() is not None:
            return _fail('浏览器提前退出（exit code=%s）' % proc.returncode)
        info = probe_version(port, timeout=0.8)
        if info:
            ws = info.get('webSocketDebuggerUrl')
            if pre_ws and ws == pre_ws:
                # 端口仍被旧实例占着：给新实例一点时间；若它绑定失败会自行退出
                time.sleep(0.25)
                continue
            break
        info = None
        time.sleep(0.25)
    if not info:
        return _fail('CDP 在 %d 上未就绪/未由本次实例接管（浏览器仍在运行: %s）'
                     % (port, proc.poll() is None))
    print('[launch] ✅ CDP ready on :%d (%s)' % (port, info.get('Browser', '?')),
          flush=True)

    if is_windows():
        time.sleep(1.5)
        focus_game_window()
        print('[launch] focused game window', flush=True)

    if opts.hold:
        print('[launch] --hold：等待浏览器退出 …（Ctrl+C 会同时关闭浏览器）', flush=True)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            print('[launch] 收到 Ctrl+C，关闭浏览器 …', flush=True)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
