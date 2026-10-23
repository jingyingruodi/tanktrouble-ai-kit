#!/usr/bin/env python
"""
射弹参数核实：每种武器的半径 / 炮口偏移 / 弹速 / 寿命
====================================================
来源双保险：
  A. **游戏常量** `Constants.<WEAPON>`（权威定义，含 px 与 m 两种单位）
  B. **运行时碰撞体**：打出该武器的一发，读 p2 body 的 fixture shape 半径
     （验证常量与实际物理体一致）

用法：
    python scripts/probe_projectile_radii.py auto
    python scripts/probe_projectile_radii.py auto --json scripts/scratch/projectile_radii.json
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port            # noqa: E402
from tankctl import TankCtl                      # noqa: E402

CONST_JS = """(() => {
  const K = window.Constants;
  const out = {};
  for (const n of ['BULLET','LASER','SHOTGUN','HOMING_MISSILE','GATLING_GUN','DOUBLE_BARREL','MINE']) {
    try { if (K[n] !== undefined) out[n] = JSON.parse(JSON.stringify(K[n])); } catch (e) {}
  }
  const top = {};
  for (const k of Object.getOwnPropertyNames(K)) {
    if (/BULLET_|SHOTGUN_|HOMING_|GATLING_|PROJECTILE_/i.test(k) && typeof K[k] !== 'object') top[k] = K[k];
  }
  out._topLevel = top;
  return JSON.stringify(out);
})()"""

RUNTIME_JS = """(() => {
  const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({err: 'no game'});
  const ps = gc.roundController.model.getProjectiles();
  const ids = Object.keys(ps);
  if (!ids.length) return JSON.stringify({count: 0});
  const p = ps[ids[0]];
  const out = {count: ids.length, type: p.getType ? p.getType() : null};
  try {
    const b = p.getB2DBody();
    const fx = b.m_fixtureList;
    const sh = fx && fx.m_shape;
    out.radius = sh ? (sh.m_radius !== undefined ? sh.m_radius : sh.radius) : null;
    out.shapeType = sh ? sh.m_type : null;
    out.position = b.GetPosition ? [b.GetPosition().x, b.GetPosition().y] : null;
  } catch (e) { out.bodyErr = String(e); }
  for (const k of ['lifetime','timeoutWindow','timeoutBounceCount','initialSpeed','timeAlive','deadlyToOwner']) {
    try { if (p[k] !== undefined) out[k] = p[k]; } catch (e) {}
  }
  return JSON.stringify(out);
})()"""


def m_of(v):
    """常量里 {px, m} 取米值；否则原样"""
    if isinstance(v, dict):
        return v.get('m')
    return v


async def main():
    ap = argparse.ArgumentParser(description='射弹半径/炮口偏移核实')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    ap.add_argument('--no-fire', action='store_true', help='只读常量，不开火验证运行时')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    consts = json.loads(await ctl.cdp.eval(CONST_JS))

    rows = []
    for name in ('BULLET', 'LASER', 'SHOTGUN', 'HOMING_MISSILE', 'GATLING_GUN', 'DOUBLE_BARREL', 'MINE'):
        c = consts.get(name)
        if not c:
            continue
        rows.append({
            'weapon': name,
            'radius_m': m_of(c.get('RADIUS')),
            'offset_m': m_of(c.get('OFFSET')),
            'speed_mps': m_of(c.get('SPEED')) if c.get('SPEED') is not None else None,
            'min_speed_mps': m_of(c.get('MIN_SPEED')),
            'max_speed_mps': m_of(c.get('MAX_SPEED')),
            'launch_speed_mps': m_of(c.get('LAUNCH_SPEED')),
            'space_m': m_of(c.get('SPACE')),
            'acceleration': c.get('ACCELERATION'),
        })

    print('=== A. 游戏常量（权威）===')
    print('%-16s %8s %8s %12s %12s %8s' % ('武器', '半径m', '炮口m', '弹速m/s', '范围m/s', '间距m'))
    for r in rows:
        sp = ('%.0f' % r['speed_mps']) if r['speed_mps'] is not None else (
            ('%.0f~%.0f' % (r['min_speed_mps'], r['max_speed_mps']))
            if r['min_speed_mps'] is not None else
            (('%.1f(发射)' % r['launch_speed_mps']) if r['launch_speed_mps'] is not None else '-'))
        print('%-16s %8s %8s %12s %12s %8s' % (
            r['weapon'], r['radius_m'], r['offset_m'], sp,
            ('%.0f~%.0f' % (r['min_speed_mps'], r['max_speed_mps']))
            if r['min_speed_mps'] is not None else '-',
            r['space_m'] if r['space_m'] is not None else '-'))

    print('\n=== 全局弹道规则 ===')
    for k, v in (consts.get('_topLevel') or {}).items():
        if any(x in k for x in ('BOUNCE', 'AMMO', 'LIFETIME', 'SPREAD', 'BUCKSHOT', 'ACCELERATION')):
            print('  %-40s = %s' % (k, v))

    rt = None
    if not args.no_fire:
        roles = await ctl.detect_roles()
        pid = next(iter(roles), None)
        if pid:
            await ctl.set_input(pid, fire=True, hold=0.4)
            await asyncio.sleep(0.2)
            rt = json.loads(await ctl.cdp.eval(RUNTIME_JS))
            print('\n=== B. 运行时碰撞体（当前武器）===')
            print('  弹丸数=%s type=%s 半径=%s shapeType=%s lifetime=%s '
                  'timeoutBounceCount=%s initialSpeed=%s'
                  % (rt.get('count'), rt.get('type'), rt.get('radius'), rt.get('shapeType'),
                     rt.get('lifetime'), rt.get('timeoutBounceCount'), rt.get('initialSpeed')))
            if rt.get('radius') is not None and rows:
                base = next((r for r in rows if r['weapon'] == 'BULLET'), None)
                if base:
                    ok = abs((rt['radius'] or 0) - (base['radius_m'] or 0)) < 1e-6
                    print('  → 与 BULLET 常量半径 %.2fm 一致: %s' % (base['radius_m'], '✅' if ok else '❌'))

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'constants': consts, 'rows': rows, 'runtime': rt}, fh, ensure_ascii=False, indent=1)
        print('\n→ %s' % args.json)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
