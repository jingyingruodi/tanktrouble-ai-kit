"""
TankTrouble 仅录制脚本（不自杀，纯被动）
========================================
持续把完整游戏状态（monitor STATE_JS）录制到 JSONL 文件。
不控制任何坦克 —— 纯被动录制，用于观战/演示/数据采集。

用法：
  python record_only.py [port] [--out file.jsonl] [--fps N] [--duration S] [--compress]
示例：
  python record_only.py 9222
  python record_only.py 9222 --out demo.jsonl --fps 30 --duration 60
  python record_only.py 9222 --out demo.jsonl --compress
"""
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from recorder import Recorder, CompressedRecorder


async def main():
    args = sys.argv[1:]
    port = int(args[0]) if len(args) > 0 and args[0].isdigit() else 9222
    out = 'record.jsonl'
    fps = 20
    duration = 0
    compress = False
    if '--out' in args:
        out = args[args.index('--out') + 1]
    if '--fps' in args:
        fps = int(args[args.index('--fps') + 1])
    if '--duration' in args:
        duration = float(args[args.index('--duration') + 1])
    if '--compress' in args:
        compress = True
    append = '--append' in args

    # 完整状态快照 JS（与 monitor 相同）
    mpath = os.path.join(os.path.dirname(__file__), '..', 'monitor_server.py')
    m = re.search(r'STATE_JS = r"""(.*?)"""', open(mpath, encoding='utf-8').read(), re.S)
    state_js = m.group(1)

    from cdp import CDP
    c = CDP(port=port)
    await c.connect_page('tanktrouble.com', timeout=10)

    rec = CompressedRecorder(out, append) if compress else Recorder(out, append)
    mode = 'COMPRESSED' if compress else 'full'
    dur_str = 'infinite' if duration <= 0 else f'{duration:g}s'
    print(f'[record-only] {fps}fps [{mode}] :{port} -> {out} ({dur_str}) — Ctrl+C stop', flush=True)
    try:
        t0 = asyncio.get_event_loop().time()
        while True:
            t = asyncio.get_event_loop().time()
            if duration > 0 and t - t0 > duration:
                break
            try:
                st = await asyncio.wait_for(c.eval(state_js), timeout=2)
                if isinstance(st, dict) and 'err' not in st:
                    rec.record(st)
            except Exception as e:
                print(f'[record-only] eval err: {e}', flush=True)
            await asyncio.sleep(1.0 / fps)
    finally:
        rec.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[record-only] stopped')
