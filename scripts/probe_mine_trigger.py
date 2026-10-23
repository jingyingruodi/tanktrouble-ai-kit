#!/usr/bin/env python
"""
地雷触发机制验证（游戏内）：坦克进入触发区→离开→引爆
=====================================================
用户说明：地雷的"松开触发"指**坦克离开触发区域**时引爆（不是松开按键）。
本脚本验证并量化：
  1. 布设后陷阱 `_activated` 何时变 True（应≈MINE_ACTIVATION_DELAY=0.5s）
  2. 另一辆坦克**驶入**触发区 → `_tripped` / `_trippingTankIds` 变化，记录触发距离
  3. 坦克**驶出** → `_detonated` 变 True、弹片（projectiles）数量与半径

用法：
    python scripts/start_local_game.py auto --players 2 --restart
    python scripts/pickup_weapon.py auto --type 4 --spawn --clear-others
    python scripts/probe_mine_trigger.py auto
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

from ego_browser import resolve_port                # noqa: E402
from tank_behavior_template import TankBehavior      # noqa: E402

SNAP = """(() => {
  const gc = GameManager.getGameController(); const M = gc.roundController.model;
  const out = {traps: [], proj: 0, proj0: null, fire: %s};
  try { const tr = M.traps || {};
    for (const k in tr) { const t = tr[k];
      out.traps.push({id: k, type: t.getType ? t.getType() : null,
        x: t.getX ? t.getX() : null, y: t.getY ? t.getY() : null,
        activated: t._activated, tripped: t._tripped, detonated: t._detonated,
        tripping: t._trippingTankIds ? Array.from(t._trippingTankIds) : null}); } } catch (e) {}
  try { const ps = M.getProjectiles(); const ids = Object.keys(ps); out.proj = ids.length;
    if (ids.length) { const p = ps[ids[0]]; const b = p.getB2DBody();
      const sh = b && b.m_fixtureList && b.m_fixtureList.m_shape;
      out.proj0 = {type: p.getType ? p.getType() : null, radius: sh ? sh.m_radius : null,
                   speed: p.initialSpeed}; } } catch (e) {}
  return JSON.stringify(out); })()"""


async def main():
    ap = argparse.ArgumentParser(description='地雷触发机制验证')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    ap.add_argument('--mine-tank', default=None, help='布设地雷的坦克（默认第一个可控）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    roles = await bhv.get_roles()
    ids = list(roles.keys())
    if len(ids) < 2:
        print('[mine2] 需要 2 辆可控坦克')
        return 1
    planter = args.mine_tank or ids[0]
    trigger = next((p for p in ids if p != planter), ids[1])
    print('[mine2] 布设坦克=%s 触发坦克=%s roles=%s' % (planter, trigger, roles), flush=True)

    wt = await bhv.ctl.cdp.eval(
        "(GameManager.getGameController()).getActiveWeapon('%s').getType()" % planter)
    print('[mine2] 布设坦克武器 type=%s（4=MINE）' % wt, flush=True)
    if str(wt) != '4':
        print('[mine2] ⚠️ 请先 pickup_weapon.py --type 4 --spawn --clear-others')
        return 1

    # 1) 布设：按住 0.4s（发射地雷），松开后等激活
    await bhv.ctl.set_input(planter, fire=True, hold=0.4)
    await asyncio.sleep(0.8)
    snap = json.loads(await bhv.ctl.cdp.eval(SNAP % 'false'))
    traps = [t for t in snap['traps'] if t['type'] == 4]
    if not traps:
        print('[mine2] 没有地雷陷阱：%s' % snap)
        return 1
    mine = traps[0]
    print('[mine2] 地雷 @(%.2f,%.2f) activated=%s' % (mine['x'], mine['y'], mine['activated']), flush=True)

    # 2) 触发坦克驶入（用迷宫寻路，避免被墙挡住）
    tp = await bhv.get_position(trigger)
    print('[mine2] 触发坦克当前 @%s，寻路到地雷…' % (tp,), flush=True)
    hist = []
    nav_ok = await bhv.navigate_to(trigger, (mine['x'], mine['y']),
                                   arrival=1.0, waypoint_arrival=2.0)
    print('[mine2] navigate_to(地雷) → %s' % nav_ok, flush=True)
    # 贴上去：继续小步前进到 <0.5m（触发区可能只有地雷半径量级）
    for step in range(30):
        pos = await bhv.get_position(trigger)
        if pos is None:
            print('[mine2] 触发坦克已不在场'); break
        d = math.hypot(pos[0] - mine['x'], pos[1] - mine['y'])
        s = json.loads(await bhv.ctl.cdp.eval(SNAP % 'false'))
        tr = next((t for t in s['traps'] if t['type'] == 4), None)
        hist.append({'step': 'in%d' % step, 'pos': pos, 'dist': d,
                     'tripped': tr['tripped'] if tr else None,
                     'tripping': tr['tripping'] if tr else None,
                     'detonated': tr['detonated'] if tr else None,
                     'nProj': s['proj'], 'proj0': s['proj0']})
        print('  in step=%2d 距地雷 %.2fm  tripped=%s tripping=%s det=%s proj=%d'
              % (step, d, tr['tripped'] if tr else None, tr['tripping'] if tr else None,
                 tr['detonated'] if tr else None, s['proj']), flush=True)
        if tr and tr['tripped']:
            print('[mine2] ★ 已进入触发区（tripped=True, tripping=%s）' % tr['tripping'], flush=True)
            break
        if d < 0.45:
            print('[mine2] 已贴到地雷 %.2fm（tripped=%s）' % (d, tr['tripped'] if tr else None), flush=True)
            break
        # 朝地雷方向再走一小步
        ang = math.atan2(mine['y'] - pos[1], mine['x'] - pos[0])
        await bhv.turn_to_rot(trigger, ang + math.pi / 2)
        await bhv.move_forward(trigger, 0.25)
        await asyncio.sleep(0.05)

    # 3) 驶离（触发"离开区域"→引爆）
    print('\n[mine2] 驶离触发区…', flush=True)
    # 找一个远离地雷的可达点
    far = None
    try:
        tiles = await bhv.get_terrain()
        best = 0.0
        for ty in range(len(tiles.get('tiles') or [])):
            for tx in range(len(tiles['tiles'][ty])):
                if not tiles['tiles'][ty][tx][0]:
                    continue
                wx, wy = (tx + 0.5) * 10.0, (ty + 0.5) * 10.0
                d = math.hypot(wx - mine['x'], wy - mine['y'])
                if d > best:
                    best, far = d, (wx, wy)
    except Exception:
        far = None
    print('[mine2] 撤离目标点=%s' % (far,), flush=True)
    # 驶离并逐帧采样：离开触发区应引爆（坦克若被炸消失，继续观察陷阱/弹片）
    first_tripped_dist = next((h['dist'] for h in hist if h.get('tripped')), None)
    dead_since = None
    for step in range(80):
        pos = await bhv.get_position(trigger)
        s = json.loads(await bhv.ctl.cdp.eval(SNAP % 'false'))
        tr = next((t for t in s['traps'] if t['type'] == 4), None)
        if pos is None:
            if dead_since is None:
                dead_since = step
                print('[mine2] 触发坦克已不在场（被炸？）→ 继续观察地雷/弹片', flush=True)
            d = float('nan')
        else:
            d = math.hypot(pos[0] - mine['x'], pos[1] - mine['y'])
        hist.append({'step': 'out%d' % step, 'pos': pos, 'dist': d,
                     'tripped': tr['tripped'] if tr else None,
                     'tripping': tr['tripping'] if tr else None,
                     'detonated': tr['detonated'] if tr else None,
                     'nProj': s['proj'], 'proj0': s['proj0'],
                     'traps': len(s['traps'])})
        if dead_since is not None and step - dead_since > 6:
            break
        if step % 2 == 0 or s['proj']:
            print('  out step=%2d 距地雷 %5.2fm  tripped=%s det=%s proj=%d%s'
                  % (step, d, tr['tripped'] if tr else None,
                     tr['detonated'] if tr else None, s['proj'],
                     ('  proj0=%s' % json.dumps(s['proj0'], ensure_ascii=False)) if s['proj0'] else ''),
                  flush=True)
        if tr and tr['detonated']:
            print('[mine2] ★★ 引爆！detonated=True（离开触发区触发）', flush=True)
            await asyncio.sleep(0.3)
            s2 = json.loads(await bhv.ctl.cdp.eval(SNAP % 'false'))
            t2 = next((t for t in s2['traps'] if t['type'] == 4), None)
            print('[mine2] 引爆后：弹片=%d proj0=%s trap=%s'
                  % (s2['proj'], json.dumps(s2['proj0'], ensure_ascii=False),
                     json.dumps({k: t2[k] for k in ('tripped', 'detonated')}, ensure_ascii=False) if t2 else None),
                  flush=True)
            hist.append({'step': 'after', 'pos': pos, 'dist': d, 'nProj': s2['proj'],
                         'proj0': s2['proj0'], 'detonated': True, 'tripped': None,
                         'tripping': None, 'traps': len(s2['traps'])})
            break
        if s['proj'] >= 5:
            print('[mine2] ★ 弹片出现：%d 个' % s['proj'], flush=True)
            break
        # 朝远点走一小步
        ang = math.atan2(far[1] - pos[1], far[0] - pos[0]) if far else 0.0
        await bhv.turn_to_rot(trigger, ang + math.pi / 2)
        await bhv.move_forward(trigger, 0.3)
        await asyncio.sleep(0.05)
    if first_tripped_dist is not None:
        print('[mine2] 触发时距地雷 %.2fm' % first_tripped_dist, flush=True)

    # 汇总
    trip_pts = [h for h in hist if h.get('tripped')]
    print('\n[mine2] ===== 汇总 =====', flush=True)
    print('  tripped 首次为 True 时距地雷 %.2fm（触发半径参考）'
          % (trip_pts[0]['dist'] if trip_pts else float('nan')), flush=True)
    maxp = max((h['nProj'] for h in hist), default=0)
    radii = sorted({(h['proj0'] or {}).get('radius') for h in hist if h.get('proj0')})
    print('  引爆时弹片最大数量=%d  弹片半径=%s' % (maxp, radii), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'mine': mine, 'hist': hist}, fh, ensure_ascii=False, indent=1)
        print('→ %s' % args.json)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
