#!/usr/bin/env python
"""
连发工具：对指定坦克按固定间隔打出 N 串（每串按住 hold 秒）
======================================================
配合 `scripts/record_only.py` 做"边打边录"（沿用上个 agent 的录制管线）。

在线要点：
* 输入上报粒度 0.5s（TANKSTATE_EMISSION_INTERVAL）→ 基础弹建议 hold ≥0.6s；
* 加特林蓄力 486ms → 在线要 hold ≥0.6s（见 docs/10 §17）；
* 驻留期间本工具本身就是活动，天然躲挂机；结束请用 leave_online_game.py 退房。

用法：
    python scripts/fire_burst.py auto --shots 6 --hold 0.8 --gap 2.5
    python scripts/fire_burst.py auto --shots 4 --hold 1.0 --gap 3 --pid 22811932
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tankctl import TankCtl                          # noqa: E402


async def main():
    ap = argparse.ArgumentParser(description='定点连发（配合录制）')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--shots', type=int, default=6)
    ap.add_argument('--hold', type=float, default=0.8)
    ap.add_argument('--gap', type=float, default=2.5)
    ap.add_argument('--duration', type=float, default=0.0,
                    help='>0 时改为"持续作战模式"：在 duration 秒内见缝插针地打（坦克死了就等重生）')
    ap.add_argument('--pid', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    pid = args.pid or next((p for p, r in roles.items() if r == 'arrows'), None) or next(iter(roles), None)
    if not pid:
        print('[burst] ❌ 找不到可控坦克', flush=True)
        return 1
    print('[burst] port=%s pid=%s %s' % (port, pid,
          ('持续 %.0fs' % args.duration) if args.duration > 0
          else ('%d 串 × 按住 %.2fs，间隔 %.2fs' % (args.shots, args.hold, args.gap))), flush=True)

    fired = 0
    if args.duration > 0:
        # ★ 持续作战模式：房间里全是我们自己的坦克，己方弹丸会打死自己人导致回合重置，
        #   所以不能"固定串数"，而要"坦克在场就打、不在场就等重生"。
        end = asyncio.get_event_loop().time() + args.duration
        while asyncio.get_event_loop().time() < end:
            tanks = await ctl.tanks()
            if pid not in tanks:
                await asyncio.sleep(0.5)
                continue
            await ctl.set_input(pid, fire=True, hold=args.hold)
            fired += 1
            pos = (await ctl.tanks()).get(pid)
            print('[burst] #%d 已开火 位置=%s'
                  % (fired, (round(pos['x'], 1), round(pos['y'], 1)) if pos else None), flush=True)
            await asyncio.sleep(args.gap)
        print('[burst] 持续作战结束：共 %d 串' % fired, flush=True)
        await ctl.cdp.close()
        return 0

    for i in range(args.shots):
        tanks = await ctl.tanks()
        if pid not in tanks:
            print('[burst] 第 %d 串：坦克不在场（被打死/回合切换）→ 等待重生…' % (i + 1), flush=True)
            for _ in range(20):
                await asyncio.sleep(0.5)
                if pid in (await ctl.tanks()):
                    break
            else:
                print('[burst] 放弃剩余串', flush=True)
                break
        await ctl.set_input(pid, fire=True, hold=args.hold)
        fired += 1
        pos = (await ctl.tanks()).get(pid)
        print('[burst] 第 %d 串已开火（hold=%.2fs）位置=%s'
              % (i + 1, args.hold, (round(pos['x'], 1), round(pos['y'], 1)) if pos else None), flush=True)
        await asyncio.sleep(args.gap)
    print('[burst] 完成：共 %d 串' % fired, flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
