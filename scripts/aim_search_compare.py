#!/usr/bin/env python
"""
火控搜索上限对比：收紧（bounces≤4 / traveled≤45m） vs 放宽（≤25 / 180m）
=====================================================================
背景（2026-10-16）：在线实测真实弹道可达 180m、十几次反弹（docs/10 §18），
而 `find_aim_angle_raycast` 的默认筛选是 `bounces ≤ 4` + `max_traveled = 45m`。
本工具量化"收太紧会漏多少解"：

* 在**同一张迷宫**上取 N 组 (射手位, 目标位)（当前位置 + 环绕采样点），
  分别用两套上限求命中角；
* 统计：两套都能解 / 只有放宽能解 / 只有收紧能解 / 都无解；
* 对"只有放宽能解"的角度，报告其反弹次数与飞行长度分布。

用法：
    python scripts/aim_search_compare.py auto [--samples 12] [--json out.json]
"""
import argparse
import asyncio
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                 # noqa: E402
from tank_behavior_template import TankBehavior      # noqa: E402

STRICT = {'max_bounces': 4, 'max_traveled': 45.0}
WIDE = {'max_bounces': 25, 'max_traveled': 180.0}


async def main():
    ap = argparse.ArgumentParser(description='火控搜索上限对比')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--samples', type=int, default=12, help='采样组数')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    await bhv.get_walls_cached(refresh=True)
    # ★ 用"当前对局里活着的我方坦克"，而不是 Inputs 绑定表里的历史 id
    live = json.loads(await bhv.ctl.cdp.eval(
        "(() => { const gc = GameManager.getGameController();"
        " if (!gc) return '[]';"
        " const tanks = Object.keys(gc.getTanks() || {});"
        " const users = Users.getAllPlayerIds();"
        " return JSON.stringify(tanks.filter(p => users.indexOf(p) >= 0)); })()"))
    if len(live) < 2:
        print('[cmp] 当前对局里我方存活坦克不足 2 辆（%s）→ 请先开一局两人本地局'
              % live, flush=True)
        return 1
    shooter, target = live[0], live[1]
    tpos = await bhv.get_position(target)
    spos = await bhv.get_position(shooter)
    if not tpos or not spos:
        print('[cmp] 取位置失败: shooter=%s target=%s' % (spos, tpos), flush=True)
        return 1
    maze = await bhv.ctl.cdp.eval(
        "(() => { const m = GameManager.getGameController().getMaze();"
        " return JSON.stringify({w: m.getWidth(), h: m.getHeight()}); })()")
    mz = json.loads(maze)
    print('[cmp] 迷宫 %dx%d  射手=%s@%s 目标=%s@%s'
          % (mz['w'], mz['h'], shooter, spos, target, tpos), flush=True)

    # 采样：★ 全场网格（覆盖远距离/多次反弹才有解的位置，才能看出收紧上限的代价）
    W, H = mz['w'] * 10, mz['h'] * 10
    samples = []
    nx, ny = 4, 3
    for i in range(nx):
        for j in range(ny):
            x = 5.0 + (W - 10.0) * i / max(1, nx - 1)
            y = 5.0 + (H - 10.0) * j / max(1, ny - 1)
            if math.hypot(x - tpos[0], y - tpos[1]) < 8.0:
                continue                      # 离目标太近，没有意义
            samples.append((round(x, 1), round(y, 1)))
    samples = samples[:args.samples] if args.samples else samples

    res = {'strict_only': [], 'wide_only': [], 'both': [], 'none': [], 'detail': []}
    for (x, y) in samples:
        row = {'from': [x, y], 'to': [round(tpos[0], 1), round(tpos[1], 1)]}
        for name, cfg in (('strict', STRICT), ('wide', WIDE)):
            ang = await bhv.find_aim_angle_at(x, y, tpos[0], tpos[1],
                                              target_id=target, require_bounce=True,
                                              max_traveled=cfg['max_traveled'],
                                              max_bounces=cfg['max_bounces'])
            row[name] = None if ang is None else round(math.degrees(ang), 2)
            if ang is not None:
                d = await bhv._raycast_path(x, y, math.degrees(ang), tpos[0], tpos[1],
                                            target_tank_id=target,
                                            max_bounces=max(5, cfg['max_bounces'] + 1))
                row[name + '_bounces'] = d.get('bounces')
                row[name + '_travel'] = round(d.get('traveled') or 0, 1)
        s_ok, w_ok = row['strict'] is not None, row['wide'] is not None
        key = 'both' if (s_ok and w_ok) else ('strict_only' if s_ok else ('wide_only' if w_ok else 'none'))
        res[key].append(row)
        res['detail'].append(row)
        print('[cmp] 从 (%5.1f,%5.1f) 打 (%5.1f,%5.1f)：收紧=%-7s 放宽=%-7s → %s'
              % (x, y, tpos[0], tpos[1], row['strict'], row['wide'], key), flush=True)

    print('\n[cmp] ===== 汇总（%d 组）=====' % len(samples), flush=True)
    for k in ('both', 'wide_only', 'strict_only', 'none'):
        print('[cmp] %-12s %d 组' % (k, len(res[k])), flush=True)
    wo = res['wide_only']
    if wo:
        bs = [r.get('wide_bounces') for r in wo if r.get('wide_bounces') is not None]
        ts = [r.get('wide_travel') for r in wo if r.get('wide_travel') is not None]
        if bs:
            print('[cmp] 只有放宽能解的角度：反弹次数 %s' % bs, flush=True)
        if ts:
            print('[cmp] 只有放宽能解的角度：飞行长度 %s m' % ts, flush=True)
        over45 = [t for t in ts if t and t > 45]
        over4 = [b for b in bs if b and b > 4]
        print('[cmp] 其中：飞行>45m 的有 %d 组、反弹>4 次的有 %d 组 → 收紧上限直接排除了它们'
              % (len(over45), len(over4)), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(res, fh, ensure_ascii=False, indent=1)
        print('[cmp] 明细 → %s' % args.json, flush=True)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
