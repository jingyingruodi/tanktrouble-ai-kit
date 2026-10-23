#!/usr/bin/env python
"""
地雷触发机制探针（游戏内）
==========================
用户观察：地雷是"**按住布设、松开释放弹片**"的触发方式。
本脚本验证并记录时间线：
  1. 按住 fire → 陷阱出现（M.traps），`_activated` 何时变 true
  2. 松开 fire → `_detonated`/`_tripped` 变化？弹片（projectiles）何时出现、多少个、半径多少

用法：
    python scripts/pickup_weapon.py auto --type 4 --spawn --clear-others
    python scripts/probe_mine_release.py auto [--hold 1.2] [--after 2.5]
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

SAMPLER = """(() => {
  window.__mn = {t0: performance.now(), frames: [], on: true};
  const step = () => {
    if (!window.__mn.on || window.__mn.frames.length > 400) return;
    const g = GameManager.getGameController();
    if (g) { try {
      const M = g.roundController.model;
      const tr = M.traps || {}; const tks = Object.keys(tr);
      const trapList = tks.map(k => { const t = tr[k]; return {
        id: k, type: t.getType ? t.getType() : null,
        x: t.getX ? +t.getX().toFixed(2) : null, y: t.getY ? +t.getY().toFixed(2) : null,
        activated: t._activated, tripped: t._tripped, detonated: t._detonated,
        detT: t._detonationTime }; });
      const ps = M.getProjectiles(); const pids = Object.keys(ps);
      let p0 = null;
      if (pids.length) { const p = ps[pids[0]]; const b = p.getB2DBody();
        const sh = b && b.m_fixtureList && b.m_fixtureList.m_shape;
        p0 = { type: p.getType ? p.getType() : null, radius: sh ? sh.m_radius : null,
               speed: p.initialSpeed }; }
      window.__mn.frames.push({t: Math.round(performance.now() - window.__mn.t0),
        traps: trapList, nProj: pids.length, proj0: p0});
    } catch (e) {} }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step); return 'ok'; })()"""

STOP = "(() => { window.__mn.on = false; return JSON.stringify(window.__mn.frames); })()"


async def main():
    ap = argparse.ArgumentParser(description='地雷触发机制探针')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--hold', type=float, default=1.2, help='按住 fire 的时长（秒）')
    ap.add_argument('--after', type=float, default=2.5, help='松开后继续采样时长（秒）')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    pid = next(iter(roles), None)
    if not pid:
        print('[mine] 无坦克')
        return 1
    wt = await ctl.cdp.eval(
        "(GameManager.getGameController()).getActiveWeapon('%s').getType()" % pid)
    print('[mine] 当前武器 type=%s（4=MINE）' % wt, flush=True)
    if str(wt) != '4':
        print('[mine] ⚠️ 不是地雷，请先 pickup_weapon.py --type 4 --spawn --clear-others')
        return 1

    await ctl.cdp.eval(SAMPLER)
    t_hold0 = asyncio.get_event_loop().time()
    print('[mine] 按住 fire %.2fs …' % args.hold, flush=True)
    await ctl.set_input(pid, fire=True, hold=args.hold)
    t_release = asyncio.get_event_loop().time() - t_hold0
    print('[mine] 已松开（t≈%.2fs），继续采样 %.1fs …' % (t_release, args.after), flush=True)
    await asyncio.sleep(args.after)
    frames = json.loads(await ctl.cdp.eval(STOP) or '[]')

    # 压缩时间线：只在状态变化时打印
    print('\n[mine] ===== 时间线（状态变化点）=====', flush=True)
    prev = None
    seen_proj = 0
    for f in frames:
        tr = f['traps']
        key = (len(tr), f['nProj'],
               tuple((t['activated'], t['tripped'], t['detonated']) for t in tr))
        if key != prev:
            prev = key
            desc = []
            for t in tr:
                desc.append('陷阱 type=%s @(%.1f,%.1f) act=%s trip=%s det=%s'
                            % (t['type'], t['x'] or 0, t['y'] or 0, t['activated'],
                               t['tripped'], t['detonated']))
            print('  t=%5dms  陷阱=%d 弹丸=%d%s%s'
                  % (f['t'], len(tr), f['nProj'],
                     ('  ' + '; '.join(desc)) if desc else '',
                     ('  弹丸0:%s' % json.dumps(f['proj0'], ensure_ascii=False)) if f['proj0'] else ''),
                  flush=True)
            seen_proj = max(seen_proj, f['nProj'])

    # 汇总
    max_proj = max((f['nProj'] for f in frames), default=0)
    radii = sorted({(f['proj0'] or {}).get('radius') for f in frames if f.get('proj0')})
    det_any = any(t['detonated'] for f in frames for t in f['traps'])
    print('\n[mine] 汇总：最大弹丸数=%d  出现过的弹丸半径=%s  有过 detonated=%s'
          % (max_proj, radii, det_any), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(frames, fh, ensure_ascii=False)
        print('→ %s' % args.json)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
