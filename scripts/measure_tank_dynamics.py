#!/usr/bin/env python
"""
坦克动力学实测（游戏内，密集采样）
==================================
测量项：
  * 前进/后退稳态速度（物理体 m_linearVelocity，逐帧）
  * 左右转向角速度（getRotation 逐帧差分）
  * 起步是否有加速段（看前几帧速度）
回避障碍：先按迷宫找一个前方 ≥6 格（60m）无墙的方向再测；被挡则换方向重试。

输出：每项"稳态中位数 + 最大"与**官方常量**对照表；`--json` 落盘原始采样。

用法：python scripts/measure_tank_dynamics.py auto [--json out.json]
"""
import argparse
import asyncio
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tankctl import TankCtl                          # noqa: E402
import tank_constants as C                           # noqa: E402

STATE = """(() => { const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({err: 'no game'});
  const ts = gc.getTanks(); const ids = Object.keys(ts);
  if (!ids.length) return JSON.stringify({round: String(gc.model.getState()), tanks: []});
  const t = ts[ids[0]];
  return JSON.stringify({round: String(gc.model.getState()), pid: ids[0],
    x: t.getX(), y: t.getY(), rot: t.getRotation()}); })()"""

SAMPLE = """(() => { const gc = GameManager.getGameController();
  if (!gc) return 'no game';
  const pid = %s;
  window.__dyn = {pid: pid, on: true, frames: []};
  const step = () => {
    if (!window.__dyn.on || window.__dyn.frames.length > 300) return;
    const g = GameManager.getGameController();
    if (g) { const T = g.getTank(window.__dyn.pid);
      if (T) { const b = T.getB2DBody();
        const lv = b.m_linearVelocity || (b.GetLinearVelocity ? b.GetLinearVelocity() : null);
        const vx = lv ? (lv.x !== undefined ? lv.x : lv[0]) : 0;
        const vy = lv ? (lv.y !== undefined ? lv.y : lv[1]) : 0;
        window.__dyn.frames.push([T.getX(), T.getY(), vx, vy, T.getRotation()]); } }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step); return 'ok'; })()"""

STOP = "(() => { window.__dyn.on = false; return JSON.stringify(window.__dyn.frames); })()"

MAZE = """(() => { try { const gc = GameManager.getGameController();
  const m = gc.getMaze(); const t = m.getTiles ? m.getTiles() : null;
  if (!t) return 'null';
  return JSON.stringify({w: t.length, h: t.length ? t[0].length : 0}); } catch (e) { return 'null'; } })()"""

CLEAR_DIR = """(() => { const gc = GameManager.getGameController();
  const pid = %s; const T = gc.getTank(pid);
  if (!T) return 'null';
  // 用游戏官方射线，找前方 80m 内无墙的方向（0..2π 每 15°）
  const U = window.UIUtils; const world = GameManager.phaserInstance.physics.p2.world;
  const WALL = U.wallCollisionGroup.mask, SH = U.shieldCollisionGroup.mask, SP = U.spawnCollisionGroup.mask;
  const res = new p2.RaycastResult();
  const out = [];
  for (let deg = 0; deg < 360; deg += 15) {
    const th = deg * Math.PI / 180;
    const ray = new p2.Ray({mode: p2.Ray.CLOSEST,
      from: [-T.getX(), -T.getY()],
      to: [-(T.getX() + Math.cos(th) * 80), -(T.getY() + Math.sin(th) * 80)],
      skipBackfaces: true, collisionMask: WALL | SH | SP,
      collisionGroup: U.rayCollisionGroup.mask});
    world.raycast(res, ray);
    out.push([deg, res.hasHit ? Math.round(res.getHitDistance(ray)) : 999]);
  }
  return JSON.stringify(out); })()"""


async def one_trial(ctl, pid, action, hold):
    await ctl.cdp.eval(SAMPLE % pid)
    await ctl.set_input(pid, hold=hold, **{action: True})
    raw = await ctl.cdp.eval(STOP)
    return json.loads(raw) if raw else []


async def main():
    ap = argparse.ArgumentParser(description='坦克动力学实测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    ap.add_argument('--hold', type=float, default=1.2)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    st = json.loads(await ctl.cdp.eval(STATE))
    print('[dyn] 状态: %s' % st, flush=True)
    pid = st.get('pid')
    if not pid:
        print('[dyn] 没有可控坦克（先开一局本地游戏）')
        return 1

    # 找一个前方开阔的方向（优先用官方射线；失败则用 0°）
    dirs = json.loads(await ctl.cdp.eval(CLEAR_DIR % pid)) if True else None
    best_dir = 0.0
    if dirs:
        open_dirs = [d for d, dist in dirs if dist > 60]
        if open_dirs:
            best_dir = open_dirs[0]
    print('[dyn] 开阔方向=%s°（80m 内无墙）' % best_dir, flush=True)

    results = {}
    for action, label in (('fwd', '前进'), ('back', '后退')):
        best = None
        for attempt in range(4):
            # 先把朝向对准开阔方向
            st = json.loads(await ctl.cdp.eval(STATE))
            cur = st['rot']
            # 游戏约定：forward 方向 = rot - π/2
            target_rot = math.radians(best_dir) + math.pi / 2
            delta = (target_rot - cur + math.pi) % (2 * math.pi) - math.pi
            if abs(delta) > 0.05:
                turn = 'right' if delta > 0 else 'left'
                await ctl.set_input(pid, hold=abs(delta) / C.TANK_ROTATION_SPEED, **{turn: True})
                await asyncio.sleep(0.15)
            frames = await one_trial(ctl, pid, action, args.hold)
            await asyncio.sleep(0.3)
            if len(frames) < 20:
                continue
            speeds = [(f[2] ** 2 + f[3] ** 2) ** 0.5 for f in frames]
            # 稳态：跳过前 10 帧
            steady = speeds[10:]
            med = statistics.median(steady)
            # 若速度掉到 0 附近说明撞墙
            if med < 3.0:
                print('[dyn]   %s 第%d次撞墙（稳态 %.2f m/s），换位重试' % (label, attempt + 1, med), flush=True)
                best_dir = (best_dir + 90) % 360
                continue
            best = {'steady_median': med, 'max': max(speeds), 'first5': speeds[:5],
                    'frames': len(frames), 'dir_deg': best_dir}
            break
        results[action] = best
        if best:
            print('[dyn] %s：稳态 %.2f m/s  最大 %.2f  前5帧 %s  （常量 %.2f）'
                  % (label, best['steady_median'], best['max'],
                     ['%.2f' % s for s in best['first5']], C.TANK_FORWARD_SPEED if action == 'fwd' else C.TANK_BACK_SPEED),
                  flush=True)

    # 转向：左右各测一次
    for action, sign in (('right', 1), ('left', -1)):
        frames = await one_trial(ctl, pid, action, args.hold)
        await asyncio.sleep(0.3)
        if len(frames) < 20:
            results[action] = None
            print('[dyn] %s：采样不足' % action, flush=True)
            continue
        # 角速度：每帧差分（去掉首尾 3 帧）
        rates = []
        for i in range(3, len(frames) - 3):
            d = frames[i][4] - frames[i - 1][4]
            while d > math.pi:
                d -= 2 * math.pi
            while d < -math.pi:
                d += 2 * math.pi
            rates.append(d)
        # 帧间隔未知 → 用总时长估（hold 秒 / 帧数）
        dt = args.hold / max(1, len(frames))
        rad = [r / dt for r in rates]
        med = statistics.median(rad)
        results[action] = {'median_rad_s': med, 'dt': dt, 'frames': len(frames)}
        print('[dyn] %s：角速度中位 %.2f rad/s（dt≈%.4fs, %d 帧，常量 ±%.1f）'
              % (action, med, dt, len(frames), C.TANK_ROTATION_SPEED), flush=True)

    print('\n[dyn] ===== 与官方常量对照 =====', flush=True)
    print('  前进: 实测 %s  常量 %.2f' % (results.get('fwd', {}).get('steady_median') if results.get('fwd') else 'N/A', C.TANK_FORWARD_SPEED))
    print('  后退: 实测 %s  常量 %.2f' % (results.get('back', {}).get('steady_median') if results.get('back') else 'N/A', C.TANK_BACK_SPEED))
    print('  右转: 实测 %s  常量 %.2f' % (results.get('right', {}).get('median_rad_s') if results.get('right') else 'N/A', C.TANK_ROTATION_SPEED))
    print('  左转: 实测 %s  常量 %.2f' % (results.get('left', {}).get('median_rad_s') if results.get('left') else 'N/A', C.TANK_ROTATION_SPEED))

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'results': results, 'open_dirs': dirs}, fh, ensure_ascii=False, indent=1)
        print('→ %s' % args.json)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
