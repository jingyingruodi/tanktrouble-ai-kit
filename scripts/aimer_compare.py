#!/usr/bin/env python
"""
TankTrouble — 官方瞄准线 vs 我们模型：角度扫描对照
=================================================
持激光时，每隔一个角度同时采两份预测：

  * **官方**：游戏自己的 `UIAimerGraphics._castRay`（p2.js 射线 + 镜面反射 +
    炮口起点 2.5m + 长度预算 maxLength）
  * **我们**：`TankBehavior._raycast_path`（Box2D `B2DUtils.calculateProjectilePath`，车体中心起点）

输出对比表：反弹次数 / 总长 / 终点，用来量化"两个引擎"的差异。

用法：python scripts/aimer_compare.py auto [--turns 6] [--turn-sec 0.35]
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

from ego_browser import resolve_port            # noqa: E402
from tank_behavior_template import TankBehavior  # noqa: E402

PREDICT_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const st = GameManager.phaserInstance.state.getCurrentState();
  const grp = st.aimerGroup;
  if (!grp) return JSON.stringify({err: 'no aimerGroup'});
  const aimer = grp.children.filter(c => c.visible && c.exists)[0];
  if (!aimer) return JSON.stringify({err: 'no visible aimer'});
  const tank = gc.getTank(aimer.playerId);
  if (!tank) return JSON.stringify({err: 'no tank'});
  const U = window.UIUtils, UC = window.UIConstants;
  const off = U.pxm(UC.AIMER_OFFSET);
  const rot = tank.getRotation();
  const pts = [{x: tank.getX() + Math.sin(rot) * off, y: tank.getY() - Math.cos(rot) * off}];
  let remaining = aimer.maxLength;
  let dir = {x: Math.sin(rot), y: -Math.cos(rot)};
  let events = [], g = 0;
  while (remaining > 0 && g++ < 40) {
    const s = pts[pts.length - 1];
    const e = {x: s.x + dir.x * remaining, y: s.y + dir.y * remaining};
    const r = aimer._castRay(s, e);
    if (r.hasHit) {
      pts.push({x: r.hitPoint.x, y: r.hitPoint.y});
      if (r.hasHitTank || r.hasHitSpawn) { events.push('STOP'); break; }
      remaining -= Math.max(UC.AIMER_MIN_STEP_LENGTH, r.hitDistance);
      const d = dir.x * r.hitNormal.x + dir.y * r.hitNormal.y;
      let nx = dir.x - 2 * d * r.hitNormal.x, ny = dir.y - 2 * d * r.hitNormal.y;
      const m = Math.hypot(nx, ny) || 1;
      dir = {x: nx / m, y: ny / m};
      events.push('B');
    } else { pts.push(e); events.push('E'); break; }
  }
  return JSON.stringify({rot: rot, maxLength: aimer.maxLength, offset: off, points: pts,
                         events: events.join(''), tankPos: [tank.getX(), tank.getY()]});
})()"""


def plen(pts):
    t = 0.0
    for i in range(1, len(pts)):
        t += math.hypot(pts[i]['x'] - pts[i-1]['x'], pts[i]['y'] - pts[i-1]['y'])
    return t


async def main():
    ap = argparse.ArgumentParser(description='官方瞄准线 vs 我们的 Box2D 预测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--turns', type=int, default=6)
    ap.add_argument('--turn-sec', type=float, default=0.35, help='每次右转时长（≈4.5rad/s）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    roles = await bhv.get_roles()
    tid = next((p for p, r in roles.items() if r == 'arrows'), None) or next(iter(roles))
    print('[cmp] port=%s tank=%s roles=%s' % (port, tid, roles), flush=True)

    hdr = '%-4s %-8s %-26s %-8s %-26s %-8s' % ('#', '朝向°', '官方(p2) 反弹/长度/终点',
                                               '', '我们(Box2D) 反弹/长度/终点', '')
    print(hdr)
    print('-' * len(hdr))
    for i in range(args.turns):
        raw = await bhv.ctl.cdp.eval(PREDICT_JS)
        d = json.loads(raw)
        if d.get('err'):
            print('[cmp] %s' % d['err'], flush=True)
            break
        pts = d['points']
        off_len = plen(pts)
        off_b = d['events'].count('B')
        end = pts[-1]
        angle = math.degrees(d['rot'] - math.pi / 2)
        ours = await bhv._raycast_path(d['tankPos'][0], d['tankPos'][1], angle, None, None,
                                       target_tank_id=None, origin_offset=0.0)
        op = ours.get('path') or []
        # 我们的实现从车体中心出发：长度要减掉中心→炮口那段
        o_len = ours.get('traveled', 0.0) or 0.0
        o_b = ours.get('bounces', 0) or 0
        o_end = op[-1] if op else None
        print('%-4d %-8.0f %-26s %-8s %-26s %-8s'
              % (i, (d['rot'] * 57.2958) % 360,
                 '%d 次 / %.1fm / (%.1f,%.1f)' % (off_b, off_len, end['x'], end['y']),
                 '',
                 '%s 次 / %.1fm / %s' % (o_b, o_len,
                                         '(%.1f,%.1f)' % (o_end['x'], o_end['y']) if o_end else '-'),
                 ''), flush=True)
        if i != args.turns - 1:
            await bhv.ctl.turn_right(args.turn_sec, tid)
            await asyncio.sleep(0.25)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
