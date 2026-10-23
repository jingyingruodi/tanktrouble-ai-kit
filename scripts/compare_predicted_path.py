#!/usr/bin/env python
"""
预测路径 vs 真实弹道 对照（验证 _raycast_path / 官方弹道模型）
==============================================================
步骤：
 1. 等回合开始、找到我方坦克
 2. 用**固定炮管角**（当前朝向 + 可选偏移）开一枪，页面内 60Hz 密集采样弹丸位置
 3. 用实测首段方向调用 `TankBehavior._raycast_path` 预测同起点路径
 4. 逐点比对：实测点 → 预测折线距离、反弹点数

用途：Box2D/官方弹道接口改动后的回归检查（2026-10-24 用来排查严格火控失败）。
用法：
    python scripts/compare_predicted_path.py auto [--offset-deg 0] [--hold 0.5]
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

SAMPLER = """(() => {
  window.__tr = {on: true, t0: performance.now(), frames: []};
  const step = () => {
    if (!window.__tr.on || window.__tr.frames.length > 500) return;
    const g = GameManager.getGameController();
    if (g) { try { const M = g.roundController.model; const ps = M.getProjectiles();
      const ids = Object.keys(ps);
      if (ids.length) { const p = ps[ids[0]];
        window.__tr.frames.push({t: Math.round(performance.now() - window.__tr.t0),
                                 x: p.getX(), y: p.getY()}); } } catch (e) {} }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  return 'ok';
})()"""

STOP = "(() => { window.__tr.on = false; return JSON.stringify(window.__tr.frames); })()"

STATE = """(() => { const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({err: 'no game'});
  return JSON.stringify({round: String(gc.model.getState()),
                         tanks: Object.keys(gc.getTanks() || {})}); })()"""

SET_ROT_FIRE = """(() => { const gc = GameManager.getGameController();
  const pid = "%s";
  const t = gc.getTank(pid);
  if (!t) return JSON.stringify({ok: false, err: 'no tank'});
  // 直接设定朝向（游戏内部 rot 累积值），再给一枪
  if (t.setRotation) t.setRotation(%f);
  return JSON.stringify({ok: true, rot: t.getRotation()});
})()"""


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, (wx * vx + wy * vy) / L2))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


async def main():
    ap = argparse.ArgumentParser(description='预测路径 vs 真实弹道对照')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--offset-deg', type=float, default=0.0, help='相对当前炮管角的偏移')
    ap.add_argument('--hold', type=float, default=0.5)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    # 等 in round
    for _ in range(40):
        st = json.loads(await bhv.ctl.cdp.eval(STATE))
        if st.get('round') == 'in round' and st.get('tanks'):
            break
        await asyncio.sleep(1)
    roles = await bhv.get_roles()
    pids = list(roles.keys())
    if not pids:
        print('[cmp] 无可控坦克')
        return 1
    pid = pids[0]
    pos = await bhv.get_position(pid)
    rot = await bhv.get_rotation(pid)
    print('[cmp] 坦克=%s pos=%s rot=%s roles=%s' % (pid, pos, rot, roles), flush=True)
    if pos is None or rot is None:
        print('[cmp] 坦克不在场，退出')
        return 1
    fire_rot = float(rot) + math.radians(args.offset_deg)
    await bhv.ctl.cdp.eval(SET_ROT_FIRE % (pid, fire_rot))
    await bhv.ctl.cdp.eval(SAMPLER)
    await bhv.ctl.set_input(pid, fire=True, hold=args.hold)
    await asyncio.sleep(1.8)
    raw = await bhv.ctl.cdp.eval(STOP)
    frames = json.loads(raw) if raw else []
    print('[cmp] 弹丸采样帧数=%d' % len(frames), flush=True)
    if len(frames) < 4:
        print('[cmp] 采样不足（坦克被打死/没出弹？）')
        await bhv.ctl.cdp.close()
        return 1

    # 实测首段方向（用前 3 帧最小二乘方向）
    f0 = frames[0]
    tail = frames[min(3, len(frames) - 1)]
    ang = math.degrees(math.atan2(tail['y'] - f0['y'], tail['x'] - f0['x']))
    print('[cmp] 实测首段角=%.2f° 起点=(%.2f,%.2f)' % (ang, f0['x'], f0['y']), flush=True)

    d = await bhv._raycast_path(f0['x'], f0['y'], ang, f0['x'] + 1, f0['y'], max_bounces=6)
    poly = d.get('path') or []
    print('[cmp] 预测: hit=%s bounces=%s traveled=%.1f 折线点=%d'
          % (d.get('hit'), d.get('bounces'), d.get('traveled') or 0, len(poly)), flush=True)
    if len(poly) >= 2:
        ds = [min(seg_dist(f['x'], f['y'], poly[i]['x'], poly[i]['y'],
                           poly[i + 1]['x'], poly[i + 1]['y']) for i in range(len(poly) - 1))
              for f in frames]
        print('[cmp] 实测点→预测折线: 中位 %.2fm 平均 %.2fm 最大 %.2fm'
              % (sorted(ds)[len(ds) // 2], sum(ds) / len(ds), max(ds)), flush=True)
        print('[cmp] 预测折线前段:', [(round(p['x'], 1), round(p['y'], 1)) for p in poly[:8]], flush=True)
        print('[cmp] 实测前段:', [(round(f['x'], 1), round(f['y'], 1)) for f in frames[:8]], flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'pos': pos, 'rot': rot, 'fire_rot': fire_rot, 'frames': frames,
                       'predicted': d}, fh, ensure_ascii=False)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
