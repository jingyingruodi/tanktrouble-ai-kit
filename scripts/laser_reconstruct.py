#!/usr/bin/env python
# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：
#   零容忍，可永久封号且无警告）。在线相关脚本另见文件内附加说明。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
"""
TankTrouble — 激光弹道精确重建（解决"180m/s 采样不足"）
======================================================
问题：激光 180 m/s，而仿真每 tick 才更新一次位置（60fps → 3.0m/tick），
      靠提高采样率**没用**（同一 tick 内位置不变，只会重复采样）。

办法：**不用采样去追位置，而是用相邻两个采样点做"反射拟合"**——
      已知弹丸匀速 180 m/s，且只会在墙上镜面反射，于是把两个采样点之间的
      真实路径当成"初始方向未知、总长已知"的几何题：

        对每个 leg：搜索初始角 θ（以弦方向为中心 ±15°），
        用**游戏自己的 p2 射线**（与 `UIAimerGraphics._castRay` 同参数）逐步前进、
        撞墙就镜面反射、走满 L = 180 × ΔtimeAlive 为止，得到终点 E(θ)；
        取 |E(θ) − B| 最小的 θ，再细搜到 0.05°。

      这样即使一个采样间隔里发生了两次反弹，也能被精确还原。

用法：
    python scripts/laser_reconstruct.py scripts/scratch/laser_shot1.jsonl
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port  # noqa: E402
from tankctl import TankCtl           # noqa: E402

FIT_JS = r"""(() => {
  // ---- 与 UIAimerGraphics._castRay 完全同参数的射线（p2.js）----
  function castRay(sx, sy, ex, ey) {
    const U = window.UIUtils;
    const game = GameManager.phaserInstance;
    const world = game.physics.p2.world;
    const ray = new p2.Ray({
      mode: p2.Ray.CLOSEST,
      from: [-sx, -sy], to: [-ex, -ey], skipBackfaces: true,
      collisionMask: U.wallCollisionGroup.mask | U.tankCollisionGroup.mask |
                     U.shieldCollisionGroup.mask | U.spawnCollisionGroup.mask,
      collisionGroup: U.rayCollisionGroup.mask,
    });
    const res = new p2.RaycastResult();
    world.raycast(res, ray);
    if (!res.hasHit()) return {hit: false};
    const hp = [0, 0];
    res.getHitPoint(hp, ray);
    return {hit: true, x: -hp[0], y: -hp[1], nx: -res.normal[0], ny: -res.normal[1],
            dist: res.getHitDistance(ray),
            isTank: res.shape.collisionGroup === U.tankCollisionGroup.mask,
            isSpawn: res.shape.collisionGroup === U.spawnCollisionGroup.mask};
  }

  // ---- 从 A 出发、初始角 theta、总长 L，逐步前进+镜面反射 ----
  function walk(ax, ay, theta, L) {
    let x = ax, y = ay, dx = Math.cos(theta), dy = Math.sin(theta);
    const pts = [{x: x, y: y}], bounces = [];
    let remain = L, guard = 0;
    while (remain > 1e-6 && guard++ < 60) {
      const r = castRay(x, y, x + dx * remain, y + dy * remain);
      if (r.hit && r.dist < remain - 1e-6) {
        x = r.x; y = r.y; pts.push({x: x, y: y});
        bounces.push({x: x, y: y, d: r.dist});
        remain -= Math.max(1e-3, r.dist);
        const dot = dx * r.nx + dy * r.ny;
        let nx2 = dx - 2 * dot * r.nx, ny2 = dy - 2 * dot * r.ny;
        const m = Math.hypot(nx2, ny2) || 1;
        dx = nx2 / m; dy = ny2 / m;
      } else {
        x += dx * remain; y += dy * remain; remain = 0;
        pts.push({x: x, y: y});
      }
    }
    return {pts: pts, bounces: bounces};
  }

  const legs = LEGS;   // [{ax,ay,bx,by,L}, ...]
  const out = [];
  for (const leg of legs) {
    const theta0 = Math.atan2(leg.by - leg.ay, leg.bx - leg.ax);
    let best = null;
    const scan = (centre, span, step) => {
      for (let a = centre - span; a <= centre + span + 1e-9; a += step) {
        const w = walk(leg.ax, leg.ay, a, leg.L);
        const e = w.pts[w.pts.length - 1];
        const err = Math.hypot(e.x - leg.bx, e.y - leg.by);
        if (!best || err < best.err) best = {err: err, theta: a, w: w};
      }
    };
    scan(theta0, 15 * Math.PI / 180, 1.0 * Math.PI / 180);
    if (best) scan(best.theta, 1.5 * Math.PI / 180, 0.05 * Math.PI / 180);
    out.push({err: best ? best.err : null, theta_deg: best ? best.theta * 180 / Math.PI : null,
              pts: best ? best.w.pts : [], bounces: best ? best.w.bounces.length : 0,
              bounce_pts: best ? best.w.bounces : []});
  }
  return JSON.stringify(out);
})()"""


def load_samples(path):
    frames = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
    per = {}
    for f in frames:
        for pid, p in (f.get('p') or {}).items():
            per.setdefault(pid, []).append((f['t'], p))
    for k in per:
        per[k].sort()
    return per


async def main():
    ap = argparse.ArgumentParser(description='激光弹道精确重建（反射拟合）')
    ap.add_argument('samples', help='laser_measure.py --out 生成的 jsonl')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--speed', type=float, default=180.0)
    args = ap.parse_args()

    per = load_samples(args.samples)
    if not per:
        print('采样文件里没有弹丸', flush=True)
        return 1
    pid, samples = max(per.items(), key=lambda kv: len(kv[1]))
    print('[rec] 弹丸 %s：%d 个采样点，timeAlive %s → %s'
          % (pid, len(samples), samples[0][1]['ta'], samples[-1][1]['ta']), flush=True)

    # 每个相邻采样点对 → 一个 leg（总长由 timeAlive 差 × 速度给出，与位置无关）
    legs = []
    for i in range(1, len(samples)):
        a, b = samples[i-1][1], samples[i][1]
        dta = (b['ta'] or 0) - (a['ta'] or 0)
        if dta <= 0:
            continue
        legs.append({'ax': a['x'], 'ay': a['y'], 'bx': b['x'], 'by': b['y'],
                     'L': args.speed * dta})
    print('[rec] 待拟合 leg 数：%d（总时长 %.3fs → 真实路径长 %.1fm）'
          % (len(legs), (samples[-1][1]['ta'] or 0) - (samples[0][1]['ta'] or 0),
             args.speed * ((samples[-1][1]['ta'] or 0) - (samples[0][1]['ta'] or 0))), flush=True)

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    js = FIT_JS.replace('LEGS', json.dumps(legs))
    res = json.loads(await ctl.cdp.eval(js))
    await ctl.cdp.close()

    total_b, total_len, full = 0, 0.0, []
    for i, r in enumerate(res):
        total_b += r['bounces']
        seg = [r['pts'][0]] + r['pts'][1:]
        for j in range(1, len(seg)):
            total_len += ((seg[j]['x'] - seg[j-1]['x']) ** 2 + (seg[j]['y'] - seg[j-1]['y']) ** 2) ** 0.5
        if i == 0:
            full = list(r['pts'])
        else:
            full.extend(r['pts'][1:])
        print('[rec] leg%02d 拟合误差=%.3fm 初始角=%.1f° 段内反弹=%d 段长=%.1fm'
              % (i, r['err'] if r['err'] is not None else -1,
                 r['theta_deg'] if r['theta_deg'] is not None else -999, r['bounces'], legs[i]['L']),
              flush=True)

    print('[rec] ===== 精确重建结果 =====', flush=True)
    print('[rec] 反弹总次数 = %d    路径总长 = %.1f m（理论 %.1f m）'
          % (total_b, total_len, args.speed * ((samples[-1][1]['ta'] or 0) - (samples[0][1]['ta'] or 0))), flush=True)
    print('[rec] 折线（米）：', flush=True)
    for i, p in enumerate(full):
        print('   p%-3d (%.2f, %.2f)' % (i, p['x'], p['y']), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
