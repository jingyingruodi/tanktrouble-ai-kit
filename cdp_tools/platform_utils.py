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
"""跨平台工具（Linux / macOS / Windows）。

解决三件平台相关的事：
  1. **浏览器探测**：Edge / Chrome / Chromium 的可执行文件路径（可用 ``TT_BROWSER`` 覆盖）；
  2. **profile 目录**：Windows 用 ``%LOCALAPPDATA%\\tt-cdp``、macOS 用 ``~/Library/Caches/tt-cdp``、
     Linux 用 ``$XDG_CACHE_HOME/tt-cdp``（默认 ``~/.cache/tt-cdp``）；可用
     ``TT_CDP_PROFILE_BASE`` 覆盖；
  3. **Python 进程列表 / 终止**：Windows 用 PowerShell(CIM)+taskkill，POSIX 用 ``ps``+信号。

Linux 额外：
  * 以 root 运行 Chromium 需要 ``--no-sandbox``（本模块自动判断 ``os.geteuid()==0``，
    也可用 ``TT_CHROME_NO_SANDBOX=1`` 强制开启）；
  * 无显示器的服务器请用 Xvfb：``xvfb-run -s "-screen 0 1280x800x24" python launch_cdp.py 9222``。
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

WIN = sys.platform.startswith('win')
MAC = sys.platform == 'darwin'
LINUX = sys.platform.startswith('linux')


def is_windows():
    return WIN


def is_macos():
    return MAC


def is_linux():
    return LINUX


try:  # pragma: no cover - 仅用于旧版 Windows 兼容提示
    import ctypes
except Exception:
    ctypes = None


# ────────────────────────────── 浏览器 ──────────────────────────────
def _win_candidates():
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    pfx = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    lad = os.environ.get('LOCALAPPDATA', os.path.expanduser(r'~\AppData\Local'))
    return [
        os.path.join(pfx, r'Microsoft\Edge\Application\msedge.exe'),
        os.path.join(pf, r'Microsoft\Edge\Application\msedge.exe'),
        os.path.join(pf, r'Google\Chrome\Application\chrome.exe'),
        os.path.join(pfx, r'Google\Chrome\Application\chrome.exe'),
        os.path.join(lad, r'Google\Chrome\Application\chrome.exe'),
    ]


def _mac_candidates():
    return [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
        '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    ]


def _linux_candidates():
    return [
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/microsoft-edge',
        '/usr/bin/microsoft-edge-stable',
        '/usr/bin/brave-browser',
        '/snap/bin/chromium',
        '/opt/google/chrome/chrome',
        '/usr/lib/chromium/chromium',
        '/var/lib/flatpak/exports/bin/com.google.Chrome',
        '/var/lib/flatpak/exports/bin/com.microsoft.Edge',
        '/var/lib/flatpak/exports/bin/org.chromium.Chromium',
        os.path.expanduser('~/.local/share/flatpak/exports/bin/com.google.Chrome'),
        os.path.expanduser('~/.local/share/flatpak/exports/bin/com.microsoft.Edge'),
        os.path.expanduser('~/.local/share/flatpak/exports/bin/org.chromium.Chromium'),
    ]


def browser_candidates(platform=None):
    """返回候选浏览器可执行文件列表（按平台；不检查是否存在）。

    ``platform`` 只用于测试（可传 ``'win32'``/``'darwin'``/``'linux'``）。
    """
    p = platform or sys.platform
    if p.startswith('win'):
        return _win_candidates()
    if p == 'darwin':
        return _mac_candidates()
    return _linux_candidates()


def find_browser(explicit=None, platform=None):
    """找到可用的浏览器可执行文件；找不到返回 None。

    优先级：``explicit`` → 环境变量 ``TT_BROWSER`` → 平台候选 → PATH 里的常见名字。
    """
    cand = explicit or os.environ.get('TT_BROWSER')
    if cand:
        return cand          # 显式指定优先（是否存在/可执行由调用方校验）
    for c in browser_candidates(platform):
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
                 'microsoft-edge', 'microsoft-edge-stable', 'msedge', 'brave-browser',
                 'chrome'):
        p = shutil.which(name)
        if p:
            return p
    return None


# ────────────────────────────── profile 目录 ──────────────────────────────
def profile_base(platform=None):
    """CDP 浏览器 profile 根目录（可用 ``TT_CDP_PROFILE_BASE`` 覆盖）。"""
    env = os.environ.get('TT_CDP_PROFILE_BASE')
    if env:
        return env
    p = platform or sys.platform
    if p.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser(r'~\AppData\Local')
        return os.path.join(base, 'tt-cdp')
    if p == 'darwin':
        return os.path.join(os.path.expanduser('~/Library/Caches'), 'tt-cdp')
    base = os.environ.get('XDG_CACHE_HOME') or ''
    if not os.path.isabs(base):            # XDG 规范：相对路径应被忽略
        base = os.path.expanduser('~/.cache')
    return os.path.join(base, 'tt-cdp')


def no_sandbox_flags(platform=None):
    """Linux 以 root 运行时 Chromium 需要 ``--no-sandbox``。"""
    p = platform or sys.platform
    if os.environ.get('TT_CHROME_NO_SANDBOX', '').lower() in ('1', 'true', 'yes'):
        return ['--no-sandbox']
    if p.startswith('linux') and hasattr(os, 'geteuid') and os.geteuid() == 0:
        return ['--no-sandbox']
    return []


# ────────────────────────────── 进程 ──────────────────────────────
def _is_python_binary(path_or_name):
    """按可执行文件/argv[0] 的 basename 判断是否是 Python 解释器。"""
    base = os.path.basename(path_or_name or '').lower()
    return bool(base) and (base.startswith('python') or base.startswith('pypy'))


def _proc_argv(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            raw = f.read()
        parts = [p for p in raw.split(b'\0') if p]
        return [p.decode('utf-8', 'replace') for p in parts] or None
    except Exception:
        return None


def _proc_exe(pid):
    try:
        return os.readlink('/proc/%d/exe' % pid)
    except Exception:
        return None


def _split_win_cmdline(cmd):
    """Windows 命令行分词；并去掉 token 两侧的引号（路径含空格时 cmdline 会加引号）。"""
    import shlex
    try:
        raw = shlex.split(cmd, posix=False)
    except Exception:
        raw = cmd.split()
    out = []
    for tok in raw:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
            tok = tok[1:-1]
        out.append(tok)
    return out


def list_processes():
    """返回 ``[(pid, argv:list[str]), ...]``；枚举失败返回 ``None``（区别于空列表）。"""
    if WIN:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -in "
              "@('python.exe','pythonw.exe','python3.exe') } | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                               capture_output=True, text=True, timeout=25)
            data = json.loads((r.stdout or '').strip() or '[]')
            if isinstance(data, dict):
                data = [data]
            out = []
            for d in data:
                pid = d.get('ProcessId')
                cmd = (d.get('CommandLine') or '').strip()
                if pid:
                    out.append((int(pid), _split_win_cmdline(cmd)))
            return out
        except Exception:
            return None
    # Linux：直接读 /proc，精度最高
    if os.path.isdir('/proc'):
        out = []
        try:
            names = os.listdir('/proc')
        except Exception:
            return None
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            argv = _proc_argv(pid)
            if not argv:
                continue
            exe = _proc_exe(pid) or argv[0]
            if _is_python_binary(argv[0]) or _is_python_binary(exe):
                out.append((pid, argv))
        return out
    # macOS / 无 /proc：ps 兜底
    try:
        r = subprocess.run(['ps', '-eo', 'pid=,args='], capture_output=True,
                           text=True, timeout=15)
    except Exception:
        return None
    out = []
    for line in (r.stdout or '').splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        import shlex
        argv = shlex.split(parts[1])
        if argv and _is_python_binary(argv[0]):
            out.append((pid, argv))
    return out


def list_python_processes():
    """兼容旧接口：返回 ``[(pid, cmdline_str)]``；枚举失败返回 ``None``。"""
    procs = list_processes()
    if procs is None:
        return None
    return [(pid, ' '.join(argv)) for pid, argv in procs]


def script_token_match(argv, names):
    """判断进程是否**在脚本位置**运行了 names 中的某个脚本（避免子串/参数误杀）。

    只检查：
      * argv[0] 的 basename（直接执行脚本，如 `./monitor_server.py`）；
      * 解释器之后**第一个非选项 token**（即 `python <script> ...` 的 <script>）；
        `-c` / `-m` 视为"无脚本文件位置"，不再往后匹配参数。
    """
    if not argv:
        return False
    names = set(names)
    if os.path.basename(argv[0]) in names:
        return True
    for tok in argv[1:]:
        if tok in ('-c', '-m'):
            return False
        if tok.startswith('-'):
            continue
        return os.path.basename(tok) in names
    return False


def terminate(pid, grace=3.0):
    """终止进程：Windows 用 taskkill /F；POSIX 先 SIGTERM 再 SIGKILL。返回是否成功。"""
    try:
        if WIN:
            subprocess.run(['taskkill', '/pid', str(pid), '/f'],
                           capture_output=True, timeout=10)
            return True
        os.kill(int(pid), signal.SIGTERM)
        t0 = time.time()
        while time.time() - t0 < float(grace):
            try:
                os.kill(int(pid), 0)
            except OSError:
                return True
            time.sleep(0.1)
        os.kill(int(pid), signal.SIGKILL)
        return True
    except Exception:
        return False
