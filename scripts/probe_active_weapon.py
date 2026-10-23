#!/usr/bin/env python
"""
探测当前激活武器的弹丸运行时参数（开火 + 页面内采样）
======================================================
不创建/不修改武器，只读当前激活武器并开一枪，读真实弹丸的：
  p2 碰撞体半径、lifetime、timeoutBounceCount、initialSpeed、弹丸数量

配合 `pickup_weapon.py --type N --spawn --clear-others` 使用：
先官方拾取到指定武器，再用本脚本实测该武器弹丸参数。

用法：python scripts/probe_active_weapon.py auto [--hold 0.6]
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

NAMES = {-1: 'BULLET', 0: 'LASER', 1: 'DOUBLE_BARREL', 2: 'SHOTGUN',
         3: 'HOMING_MISSILE', 4: 'MINE', 5: 'GATLING_GUN'}
EXPECTED = {'BULLET': 0.25, 'LASER': 0.0, 'DOUBLE_BARREL': 0.25, 'SHOTGUN': 0.10,
            'HOMING_MISSILE': 0.20, 'MINE': 0.80, 'GATLING_GUN': 0.10}

ACTIVE = """(() => { const gc = GameManager.getGameController();
  const pid = %s; const w = gc.getActiveWeapon(pid);
  return JSON.stringify({pid: pid, type: w.getType(), fields: Object.keys(w).filter(k => k[0] === '_')}); })()"""

SAMPLE = """(() => {
  window.__aw = {on: true, best: null, maxN: 0};
  const step = () => {
    if (!window.__aw.on || window.__aw.frames > 300) return;
    window.__aw.frames = (window.__aw.frames || 0) + 1;
    const g = GameManager.getGameController();
    if (g) { try { const ps = g.roundController.model.getProjectiles();
      const ids = Object.keys(ps);
      if (ids.length) {
        if (ids.length > window.__aw.maxN) window.__aw.maxN = ids.length;
        const p = ps[ids[0]]; const b = p.getB2DBody();
        const sh = b && b.m_fixtureList && b.m_fixtureList.m_shape;
        window.__aw.best = {radius: sh ? sh.m_radius : null, lifetime: p.lifetime,
          bounces: p.timeoutBounceCount, speed: p.initialSpeed, n: ids.length};
      } } catch (e) {} }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step); return 'ok'; })()"""

STOP = "(() => { window.__aw.on = false; return JSON.stringify({best: window.__aw.best, maxN: window.__aw.maxN}); })()"


async def main():
    ap = argparse.ArgumentParser(description='探测激活武器弹丸运行时参数')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--hold', type=float, default=0.7)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    pid = next(iter(roles), None)
    if not pid:
        print('[aw] 无坦克')
        return 1
    info = json.loads(await ctl.cdp.eval(ACTIVE % json.dumps(pid)))
    name = NAMES.get(info['type'], str(info['type']))
    await ctl.cdp.eval(SAMPLE)
    await ctl.set_input(pid, fire=True, hold=args.hold)
    await asyncio.sleep(0.25)
    res = json.loads(await ctl.cdp.eval(STOP))
    best = res.get('best')
    if best and best.get('radius') is not None:
        exp = EXPECTED.get(name)
        same = '✅' if (exp is not None and abs(best['radius'] - exp) < 0.005) else '❌'
        print('[aw] 武器=%-14s(type=%s) 半径=%.3f m 常量=%.3f %s  lifetime=%s bounces=%s speed=%s 弹丸数=%s'
              % (name, info['type'], best['radius'], exp if exp is not None else -1, same,
                 best.get('lifetime'), best.get('bounces'), best.get('speed'), best.get('n')))
    else:
        print('[aw] 武器=%s(type=%s) 未采到弹丸: %s' % (name, info['type'], res))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'active': info, 'sample': res}, fh, ensure_ascii=False, indent=1)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
