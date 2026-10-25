#!/usr/bin/env python
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
监控室 / 录制 / 回放 全链路自检（自检脚本）
============================================
前置：浏览器已开游戏（本地局即可），且 `cdp_tools/ego_browser.resolve_port('auto')` 可用。

流程：
  1) 启动 monitor_server.py（1 个 CDP 实例，auto 端口）→ 等 WS 9310 起；
  2) 连 WS 9310 读 5 帧，断言有坦克（监控室数据源 OK）；
  3) 用 record_only.py 录 4s（压缩格式）；
  4) 用 playback_server.py 在 9316 回放该文件，连 WS 读帧，断言回放有坦克；
  5) 调 kill_monitor.py 清理；输出 PASS/FAIL 汇总。

用法：python scripts/selftest_monitor.py [--keep]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'cdp_tools'))

from ego_browser import resolve_port  # noqa: E402


def port_open(port, timeout=15.0):
    """TCP 探测端口（WebSocket 端口不能用 HTTP GET 探测）。"""
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


async def ws_read(port, n=5, timeout=6.0):
    import websockets
    out = []
    async with websockets.connect('ws://127.0.0.1:%d' % port, open_timeout=timeout) as ws:
        for _ in range(n):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            out.append(msg)
    return out


def start(cmd):
    return subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def main():
    ap = argparse.ArgumentParser(description='监控室/录制/回放 全链路自检')
    ap.add_argument('--keep', action='store_true', help='测完不清理进程（调试用）')
    args = ap.parse_args()

    cdp = resolve_port('auto')
    proc = {}
    ok = True

    # 先清理上一轮残留（否则 9310/9316 被占会导致误判）
    try:
        subprocess.run([sys.executable, os.path.join(HERE, 'kill_monitor.py')],
                       cwd=ROOT, capture_output=True, timeout=60)
    except Exception:
        pass

    print('[1/5] CDP 端口 auto -> %d' % cdp, flush=True)
    print('[2/5] 启动 monitor_server ...', flush=True)
    proc['monitor'] = start([sys.executable, os.path.join(ROOT, 'monitor_server.py'),
                             '1', 'auto'])
    if not port_open(9310, timeout=15):
        print('     ❌ monitor_server 未监听 9310（启动失败，见其输出）', flush=True)
        ok = False
    try:
        frames = asyncio.run(ws_read(9310, 5))
        tanks = [(f.get('data') or {}).get('tanks') or {} for f in frames]
        n = max(len(t) for t in tanks)
        print('     WS 9310 frames=%d max_tanks=%d' % (len(frames), n), flush=True)
        if n == 0:
            print('     ❌ 监控室数据源没有坦克（游戏是否在局中？）', flush=True)
            ok = False
    except Exception as e:
        print('     ❌ WS 9310 失败: %s: %s' % (type(e).__name__, e), flush=True)
        ok = False

    scratch = os.path.join(ROOT, 'scripts', 'scratch')
    os.makedirs(scratch, exist_ok=True)
    out = os.path.join(scratch, 'selftest_rec.jsonl')
    print('[3/5] 录制 4s -> %s' % out, flush=True)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, 'record_only.py'), 'auto',
                            '--out', out, '--fps', '15', '--duration', '4', '--compress'],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        lines = sum(1 for _ in open(out, encoding='utf-8')) if os.path.exists(out) else 0
        print('     录制行数=%d rc=%d' % (lines, r.returncode), flush=True)
        if lines <= 0:
            print('     ❌ 录制无输出: %s' % (r.stderr.strip()[-300:]), flush=True)
            ok = False
    except Exception as e:
        print('     ❌ 录制失败: %s: %s' % (type(e).__name__, e), flush=True)
        ok = False

    print('[4/5] 回放（9316）...', flush=True)
    proc['playback'] = start([sys.executable, os.path.join(ROOT, 'playback_server.py'),
                              out, '9316', '--fps', '15'])
    if not port_open(9316, timeout=15):
        print('     ❌ playback_server 未监听 9316', flush=True)
        ok = False
    try:
        rframes = asyncio.run(ws_read(9316, 5))
        rtanks = [(f.get('data') or {}).get('tanks') or {} for f in rframes]
        rn = max(len(t) for t in rtanks)
        print('     回放 frames=%d max_tanks=%d' % (len(rframes), rn), flush=True)
        if rn == 0:
            print('     ❌ 回放数据无坦克', flush=True)
            ok = False
    except Exception as e:
        print('     ❌ 回放 WS 失败: %s: %s' % (type(e).__name__, e), flush=True)
        ok = False

    print('[5/5] 清理 ...', flush=True)
    if not args.keep:
        subprocess.run([sys.executable, os.path.join(HERE, 'kill_monitor.py')],
                       cwd=ROOT, capture_output=True, timeout=60)
    print('\n结论: %s' % ('✅ PASS（监控室/录制/回放全链路正常）' if ok else '❌ FAIL（见上）'),
          flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
