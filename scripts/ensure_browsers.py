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
"""确保若干 CDP 浏览器实例已就绪（没有的就启动 launch_cdp.py）。

用法：
    python scripts/ensure_browsers.py 9222 9223 9224
    python scripts/ensure_browsers.py auto          # 解析当前可用端口，不新开
    python scripts/ensure_browsers.py 9222 --url about:blank

环境变量：
    TT_LAUNCH_WAIT  每个实例等待秒数（默认 25）
    TT_CDP_PORT     无参数时的默认端口
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'cdp_tools'))

from ego_browser import probe, resolve_port_cli  # noqa: E402


def launch_one(port, url):
    """同步调用 launch_cdp.py（其自身会等待 CDP 就绪并返回正确退出码）。"""
    import subprocess
    cmd = [sys.executable, os.path.join(ROOT, 'launch_cdp.py'), str(port)]
    if url:
        cmd.append(url)
    print('[ensure] 启动 CDP %d ...' % port, flush=True)
    try:
        r = subprocess.run(cmd, cwd=ROOT, timeout=90,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode
    except Exception as e:
        print('[ensure] 启动 %d 异常: %s: %s' % (port, type(e).__name__, e), flush=True)
        return 1


def main():
    args = [a for a in sys.argv[1:]]
    url = 'about:blank'          # 默认空白页（本地测试；需在线页请显式 --url）
    if '--url' in args:
        i = args.index('--url')
        if i + 1 < len(args):
            url = args[i + 1]
            del args[i:i + 2]
    if not args:
        args = [os.environ.get('TT_CDP_PORT') or 'auto']

    if args == ['auto']:
        port = resolve_port_cli('auto')
        print('[ensure] auto -> CDP %d（已在运行，无需新开）' % port, flush=True)
        return 0

    wait = float(os.environ.get('TT_LAUNCH_WAIT', '25'))
    alive, ok, bad = [], [], []
    for a in args:
        if not str(a).isdigit():
            print('[ensure] 跳过非端口参数: %s' % a, flush=True)
            continue
        port = int(a)
        if probe(port):
            alive.append(port)
            continue
        rc = launch_one(port, url)
        (ok if rc == 0 else bad).append(port)
    t0 = time.time()
    while time.time() - t0 < wait and any(not probe(p) for p in ok + bad):
        time.sleep(0.5)
    for p in ok + bad:
        if not probe(p):
            if p not in bad:
                bad.append(p)
            if p in ok:
                ok.remove(p)
    print('[ensure] 原本在线: %s' % (alive or '无'), flush=True)
    print('[ensure] 新启动就绪: %s' % (ok or '无'), flush=True)
    if bad:
        print('[ensure] ❌ 启动失败/未就绪: %s' % bad, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
