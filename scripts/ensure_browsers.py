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
    python scripts/ensure_browsers.py auto          # auto = 当前可用端口，不新开
环境变量：
    TT_LAUNCH_WAIT  每个实例等待秒数（默认 25）
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def probe(port, timeout=1.0):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/json/version' % port,
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    args = [a for a in sys.argv[1:] if a]
    if not args:
        args = ['9222']
    wait = float(os.environ.get('TT_LAUNCH_WAIT', '25'))
    alive, started = [], []
    for a in args:
        if a == 'auto':
            continue
        try:
            port = int(a)
        except ValueError:
            print('[ensure] 跳过非端口参数: %s' % a, flush=True)
            continue
        if probe(port):
            alive.append(port)
            continue
        print('[ensure] 启动 CDP %d ...' % port, flush=True)
        try:
            subprocess.Popen([sys.executable, os.path.join(ROOT, 'launch_cdp.py'),
                              str(port)],
                             cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started.append(port)
        except Exception as e:
            print('[ensure] 启动失败 %d: %s: %s' % (port, type(e).__name__, e), flush=True)
    t0 = time.time()
    while time.time() - t0 < wait and any(not probe(p) for p in started):
        time.sleep(0.5)
    ok, bad = [], []
    for p in started:
        (ok if probe(p) else bad).append(p)
    print('[ensure] 原本在线: %s' % (alive or '无'), flush=True)
    print('[ensure] 新启动: %s' % (ok or '无'), flush=True)
    if bad:
        print('[ensure] ❌ 启动后仍未就绪: %s' % bad, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
