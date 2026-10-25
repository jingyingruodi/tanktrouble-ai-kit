"""移动对手（mover harness）——本地双人局里驱动 keyboard 角色随机游走。

只控制 keyboard 角色（对手），不碰 arrows（黑盒 AI 的角色）。
用法：
    python scripts/mover_harness.py auto --seconds 240 [--seed 7] [--fire-rate 0.15]

行为：随机方向段（0.25~0.9s 前进/后退/转），按 fire-rate 概率开火；
持续发送输入（本地局无 AFK，但保持"在动"更接近真实对手）。
"""
import argparse
import asyncio
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

from ego_browser import resolve_port     # noqa: E402
from tankctl import TankCtl              # noqa: E402


async def main():
    ap = argparse.ArgumentParser(description='移动对手（只控 keyboard 角色）')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--seconds', type=float, default=240.0)
    ap.add_argument('--seed', type=int, default=None)
    ap.add_argument('--role', default='keyboard', help='要驱动的角色（默认 keyboard）')
    ap.add_argument('--fire-rate', type=float, default=0.15)
    args = ap.parse_args()

    ctl = TankCtl(port=resolve_port(args.port))
    await ctl.connect()
    roles = await ctl.detect_roles()
    pid = next((p for p, r in roles.items() if r == args.role), None)
    if pid is None:
        print('[mover] 找不到 role=%s 的角色，roles=%r' % (args.role, roles), flush=True)
        return 2
    print('[mover] 驱动对手 pid=%s role=%s 时长 %.0fs seed=%s fire_rate=%.2f'
          % (pid, args.role, args.seconds, args.seed, args.fire_rate), flush=True)

    rnd = random.Random(args.seed)
    t0 = time.time()
    moves = 0
    while time.time() - t0 < args.seconds:
        try:
            choice = rnd.choice(['fwd', 'fwd', 'back', 'left', 'right'])
            dur = rnd.uniform(0.25, 0.9)
            fire = rnd.random() < args.fire_rate
            await ctl.set_input(pid, hold=dur, fire=fire, **{choice: True})
            moves += 1
        except Exception as e:
            print('[mover] 出错（继续）: %s: %s' % (type(e).__name__, e), flush=True)
            await asyncio.sleep(0.5)
    print('[mover] 结束，共 %d 段移动' % moves, flush=True)
    try:
        await ctl.set_input(pid, hold=0.1)
    except Exception:
        pass
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
