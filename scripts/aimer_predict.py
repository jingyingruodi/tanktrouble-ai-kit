#!/usr/bin/env python
"""
TankTrouble — 官方瞄准线预测 vs 我们的弹道模型
==============================================
持激光时游戏会渲染一条预测虚线（`LaserAimerUpgrade` → `UIAimerGraphics.update()`）。
本脚本**用游戏自己的 `_castRay` 复跑那条预测**（照抄已还原的 update() 循环：
炮口起点 → 逐段 p2 射线 → 撞墙镜面反射 → 长度预算 maxLength → 撞坦克/出生点即停），
再对同一角度调用我们自己的 Box2D 实现（`TankBehavior._raycast_path`）做对照。

两个引擎不同（游戏渲染侧 p2.js / 我们用 Box2D），这正是要量化的地方。

用法：python scripts/aimer_predict.py auto [--angle 度]
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port           # noqa: E402
from tank_behavior_template import TankBehavior  # noqa: E402

PREDICT_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const st = GameManager.phaserInstance.state.getCurrentState();
  const grp = st.aimerGroup;
  if (!grp) return JSON.stringify({err: 'no aimerGroup'});
  const aimer = grp.children.filter(c => c.visible && c.exists)[0];
  if (!aimer) return JSON.stringify({err: 'no visible aimer (需要持激光)'});
  const tank = gc.getTank(aimer.playerId);
  if (!tank) return JSON.stringify({err: 'no tank'});
  const U = window.UIUtils, UC = window.UIConstants;
  const off = U.pxm(UC.AIMER_OFFSET);
  const rot = tank.getRotation();
  const pts = [{x: tank.getX() + Math.sin(rot) * off, y: tank.getY() - Math.cos(rot) * off}];
  let remaining = aimer.maxLength;
  let dir = {x: Math.sin(rot), y: -Math.cos(rot)};
  const events = [];
  let guard = 0;
  while (remaining > 0 && guard++ < 40) {
    const start = pts[pts.length - 1];
    const end = {x: start.x + dir.x * remaining, y: start.y + dir.y * remaining};
    const r = aimer._castRay(start, end);
    if (r.hasHit) {
      pts.push({x: r.hitPoint.x, y: r.hitPoint.y});
      if (r.hasHitTank || r.hasHitSpawn) { events.push('STOP(tank/spawn)'); break; }
      remaining -= Math.max(UC.AIMER_MIN_STEP_LENGTH, r.hitDistance);
      const d = dir.x * r.hitNormal.x + dir.y * r.hitNormal.y;
      let nx = dir.x - 2 * d * r.hitNormal.x, ny = dir.y - 2 * d * r.hitNormal.y;
      const m = Math.hypot(nx, ny) || 1;
      dir = {x: nx / m, y: ny / m};
      events.push('bounce@' + r.hitDistance.toFixed(1));
    } else { pts.push(end); events.push('END(no-hit)'); break; }
  }
  return JSON.stringify({tank: aimer.playerId, rot: rot, maxLength: aimer.maxLength,
                         offset_m: off, points: pts, events: events,
                         remaining: remaining, tankPos: [tank.getX(), tank.getY()]});
})()"""


async def main():
    ap = argparse.ArgumentParser(description='官方瞄准线 vs Box2D 模型')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--angle', type=float, default=None, help='对照用的世界角度（默认用坦克朝向）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    raw = await bhv.ctl.cdp.eval(PREDICT_JS)
    d = json.loads(raw)
    if d.get('err'):
        print('[aimer] %s' % d['err'], flush=True)
        return 1

    pts = d['points']
    print('===== 官方瞄准线预测（用游戏自己的 _castRay 复跑）=====')
    print('坦克 %s @(%.1f,%.1f) 朝向=%.1f°  炮口偏移=%.2fm  长度预算 maxLength=%s'
          % (d['tank'], d['tankPos'][0], d['tankPos'][1],
             (d['rot'] - 1.5707963) * 57.2958, d['offset_m'], d['maxLength']))
    print('事件: %s' % ' → '.join(d['events']))
    total = 0.0
    for i, p in enumerate(pts):
        if i:
            total += ((p['x'] - pts[i-1]['x']) ** 2 + (p['y'] - pts[i-1]['y']) ** 2) ** 0.5
        print('  p%d (%.2f, %.2f)m%s' % (i, p['x'], p['y'],
                                         '   ← 炮口起点' if i == 0 else ''))
    print('官方预测总长 = %.2f m，反弹 %d 次'
          % (total, len([e for e in d['events'] if e.startswith('bounce')])))

    # 与我们自己的 Box2D 射线对照：用官方列表里的中间点判断是"纯墙反射"还是"被坦克挡住"
    angle = args.angle if args.angle is not None else (d['rot'] - 1.5707963) * 57.2958
    res = await bhv._raycast_path(d['tankPos'][0], d['tankPos'][1], angle, None, None,
                                  target_tank_id=None, origin_offset=0.0)
    print()
    print('===== 我们的 Box2D 实现（同角度 %.0f°）=====' % angle)
    print(json.dumps(res, ensure_ascii=False)[:600])
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
