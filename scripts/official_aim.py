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
TankTrouble — 官方弹道 + 我们自己的命中预测
==========================================
分工（依据 2026-10-02 的逆向结论）：

  * **弹道用官方的**：`UIAimerGraphics._castRay` 的同一套 p2 射线
    （炮口起点 2.5m、镜面反射、撞墙/护盾/出生点即止、坐标镜像），实测与真实弹丸
    吻合到 0.005m（见 `laser_reconstruct.py`）。
  * **命中预测我们自己加**，因为官方只有几何布尔值：
      - 官方 `hasHitTank` 认不出是**哪辆**坦克（所有坦克共用一个 collisionGroup，
        实测 shape.body 上没有 playerId/sprite 可映射）；
      - 官方是**零半径射线**（不建模弹丸粗细），擦边会判 miss；
      - 官方不含**目标移动提前量**；
      - 官方长度被 `Constants.AIMER_LENGTH=60m` 截断，不是武器真实射程。

本脚本的做法：
  1. 用官方射线（**只挡墙/护盾/出生点**，不挡坦克）走出整条反弹折线；
  2. 在折线上做我们自己的**半径感知命中判定**：对每辆坦克算"圆心到各段折线的最短距离"，
     ≤ `TANK_R + BULLET_R` 记为命中，取**沿路径最早**的那个 → 自然给出身份（我/目标/其它）；
  3. 自车判定跳过炮口出膛段（弹丸在炮口外生成，不会立刻打到自己）；
  4. 在页面内做角度扫描（1° 粗扫 + 0.1° 细扫），返回最佳角度、预测折线、命中的是谁、
     自伤风险、以及是否被护盾/出生点挡住。

用法：
    python scripts/official_aim.py auto --target 22768270                 # 只算角度
    python scripts/official_aim.py auto --target 22768270 --fire          # 算了就开火验证
    python scripts/official_aim.py auto --path-only                       # 只看当前朝向的预测折线
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

from ego_browser import resolve_port  # noqa: E402
from tankctl import TankCtl           # noqa: E402

# 武器参数（实测）：弹速 m/s，寿命 s
WEAPONS = {
    'bullet': {'speed': 18.0, 'lifetime': 10.0},
    'laser': {'speed': 180.0, 'lifetime': 0.8},
}

SCAN_JS = r"""(() => {
  const P = PARAMS;
  const gc = GameManager.getGameController() || GameManager.gameController;
  const M = gc.roundController.model;
  const U = window.UIUtils;
  const world = GameManager.phaserInstance.physics.p2.world;
  const WALL = U.wallCollisionGroup.mask, TANK = U.tankCollisionGroup.mask;
  const SHIELD = U.shieldCollisionGroup.mask, SPAWN = U.spawnCollisionGroup.mask;

  // 官方同款射线；blockTanks=true 时连坦克一起挡（用于还原官方虚线行为）
  function cast(sx, sy, ex, ey, blockTanks) {
    const mask = WALL | SHIELD | SPAWN | (blockTanks ? TANK : 0);
    const ray = new p2.Ray({mode: p2.Ray.CLOSEST, from: [-sx, -sy], to: [-ex, -ey],
      skipBackfaces: true, collisionMask: mask, collisionGroup: U.rayCollisionGroup.mask});
    const res = new p2.RaycastResult();
    world.raycast(res, ray);
    if (!res.hasHit()) return {hit: false};
    const hp = [0, 0]; res.getHitPoint(hp, ray);
    const cg = res.shape.collisionGroup;
    return {hit: true, x: -hp[0], y: -hp[1], nx: -res.normal[0], ny: -res.normal[1],
            dist: res.getHitDistance(ray), isTank: cg === TANK, isShield: cg === SHIELD,
            isSpawn: cg === SPAWN};
  }

  // 官方弹道：炮口起点 + 镜面反射，走满预算 L（撞护盾/出生点即止）
  function walk(ax, ay, theta, L, blockTanks) {
    let x = ax, y = ay, dx = Math.cos(theta), dy = Math.sin(theta);
    const pts = [{x: x, y: y, s: 0}], bounces = [];
    let remain = L, travelled = 0, guard = 0, blocked = null;
    while (remain > 1e-6 && guard++ < 80) {
      const r = cast(x, y, x + dx * remain, y + dy * remain, blockTanks);
      if (r.hit && r.dist < remain - 1e-6) {
        x = r.x; y = r.y; travelled += Math.max(0, r.dist);
        pts.push({x: x, y: y, s: travelled});
        if (r.isShield || r.isSpawn) { blocked = r.isShield ? 'shield' : 'spawn'; break; }
        bounces.push({x: x, y: y, s: travelled});
        remain -= Math.max(1e-3, r.dist);
        const dot = dx * r.nx + dy * r.ny;
        let nx2 = dx - 2 * dot * r.nx, ny2 = dy - 2 * dot * r.ny;
        const m = Math.hypot(nx2, ny2) || 1;
        dx = nx2 / m; dy = ny2 / m;
      } else {
        x += dx * remain; y += dy * remain; travelled += remain; remain = 0;
        pts.push({x: x, y: y, s: travelled});
      }
    }
    return {pts: pts, bounces: bounces.length, bouncePts: bounces, blocked: blocked};
  }

  // 圆-线段最近点距离
  function distToSeg(px, py, ax, ay, bx, by) {
    const vx = bx - ax, vy = by - ay, wx = px - ax, wy = py - ay;
    const L2 = vx * vx + vy * vy;
    let t = L2 > 1e-12 ? (wx * vx + wy * vy) / L2 : 0;
    t = Math.max(0, Math.min(1, t));
    const cx = ax + t * vx, cy = ay + t * vy;
    return {d: Math.hypot(px - cx, py - cy), t: t};
  }

  // 我们自己的命中判定：沿路径最早命中的坦克（含自车）
  // ★ 自伤规则（2026-10-02 从 v7 真实录制标定）：弹丸字段 `deadlyToOwner` 在
  //   **第一次撞墙反弹**时由 false 翻转为 true（43 个可观测翻转里 23 个翻转帧
  //   == 首次反弹帧，其余多为采样边界差 1 帧）。即：**直射弹打不死自己，
  //   只有反弹之后才可能自伤** —— 因此自车只检查"首次反弹之后"的路径段。
  function firstHit(path, bounces, tanks, shooterId, tankR, bulletR) {
    const firstBounceS = bounces.length ? bounces[0].s : Infinity;
    let best = null;
    for (let i = 1; i < path.length; i++) {
      const a = path[i-1], b = path[i];
      const segLen = b.s - a.s;
      for (const tid in tanks) {
        const T = tanks[tid], R = tankR + bulletR;
        const r = distToSeg(T.x, T.y, a.x, a.y, b.x, b.y);
        if (r.d > R) continue;
        const hitS = a.s + r.t * segLen;
        if (tid === shooterId) {
          if (hitS < SELF_EXIT) continue;              // 炮口出膛段
          if (hitS <= firstBounceS) continue;          // ★ 反弹前对自己无害
        }
        if (!best || hitS < best.s) best = {pid: tid, s: hitS, seg: i,
                                            point: {x: a.x + r.t * (b.x - a.x), y: a.y + r.t * (b.y - a.y)}};
      }
    }
    return best;
  }

  const shooter = gc.getTank(P.shooter);
  const sx = shooter.getX(), sy = shooter.getY();
  const rot = shooter.getRotation();
  const off = U.pxm(window.UIConstants.AIMER_OFFSET);
  const tanksNow = {};
  const ts = gc.getTanks();
  for (const id in ts) tanksNow[id] = {x: ts[id].getX(), y: ts[id].getY()};

  function evaluate(theta) {
    const ax = sx + Math.cos(theta) * off, ay = sy + Math.sin(theta) * off;
    const w = walk(ax, ay, theta, P.budget, false);
    const hit = firstHit(w.pts, w.bouncePts || [], tanksNow, P.shooter, P.tankR, P.bulletR);
    return {theta: theta, path: w.pts, bounces: w.bounces, blocked: w.blocked, hit: hit};
  }

  if (P.pathOnly) {
    const theta = rot - Math.PI / 2;
    const e = evaluate(theta);
    return JSON.stringify({mode: 'path', theta_deg: theta * 180 / Math.PI, path: e.path,
                           bounces: e.bounces, blocked: e.blocked, hit: e.hit,
                           origin: {x: sx, y: sy}, offset: off, budget: P.budget});
  }

  // 角度扫描：先 1°，再在最优附近 0.1°
  let best = null;
  const consider = (theta, step) => {
    const e = evaluate(theta);
    let score = null;
    if (e.blocked) score = null;                                   // 被护盾/出生点挡住 → 不可用
    else if (e.hit && e.hit.pid === P.target) score = 1e6 - e.hit.s;  // 命中目标：路径越短越好
    else if (e.hit && e.hit.pid === P.shooter) score = -1e6 + e.hit.s; // 自伤：最差
    if (score !== null && (!best || score > best.score)) best = {score: score, e: e, step: step};
  };
  for (let d = -180; d < 180; d += 1) consider(d * Math.PI / 180, 1);
  if (best) {
    const c = best.e.theta;
    for (let d = -1.5; d <= 1.5; d += 0.1) consider(c + d * Math.PI / 180, 0.1);
  }
  return JSON.stringify({mode: 'scan', origin: {x: sx, y: sy}, offset: off, budget: P.budget,
                         best: best ? {theta_deg: best.e.theta * 180 / Math.PI,
                                       bounces: best.e.bounces, hit: best.e.hit,
                                       blocked: best.e.blocked, step_deg: best.step,
                                       path: best.e.path} : null});
})()"""


async def main():
    ap = argparse.ArgumentParser(description='官方弹道 + 自定义命中预测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--shooter', default=None)
    ap.add_argument('--target', default=None)
    ap.add_argument('--weapon', default='laser', choices=list(WEAPONS))
    ap.add_argument('--budget', type=float, default=None, help='路径长度预算（米）')
    ap.add_argument('--tank-r', type=float, default=2.2)
    ap.add_argument('--bullet-r', type=float, default=0.3)
    ap.add_argument('--self-exit', type=float, default=3.5, help='自伤判定跳过炮口出射段长度')
    ap.add_argument('--path-only', action='store_true')
    ap.add_argument('--fire', action='store_true', help='算完直接开火验证')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    ts = await ctl.tanks()
    shooter = args.shooter or next((p for p, r in roles.items() if r == 'arrows'), None) \
        or next(iter(ts))
    target = args.target or next((p for p in ts if p != shooter), None)
    w = WEAPONS[args.weapon]
    budget = args.budget if args.budget is not None else w['speed'] * w['lifetime']

    params = {'shooter': shooter, 'target': target, 'budget': budget,
              'tankR': args.tank_r, 'bulletR': args.bullet_r, 'selfExit': args.self_exit,
              'pathOnly': args.path_only}
    js = SCAN_JS.replace('PARAMS', json.dumps(params)).replace('SELF_EXIT', str(args.self_exit))
    res = json.loads(await ctl.cdp.eval(js))

    print('[aim] shooter=%s target=%s weapon=%s 弹速=%.0f 寿命=%.1fs → 预算=%.1fm'
          % (shooter, target, args.weapon, w['speed'], w['lifetime'], budget), flush=True)
    print('[aim] 炮口偏移=%.2fm 坦克半径=%.2f 弹丸半径=%.2f 自伤跳过段=%.1fm'
          % (res['offset'], args.tank_r, args.bullet_r, args.self_exit), flush=True)

    if args.path_only:
        print('[aim] 当前朝向 %.1f°，预测折线 %d 点，反弹 %d 次，阻挡=%s'
              % (res['theta_deg'], len(res['path']), res['bounces'], res['blocked']), flush=True)
        for p in res['path']:
            print('      (%.2f, %.2f)  s=%.1fm' % (p['x'], p['y'], p['s']), flush=True)
        print('[aim] 命中判定 = %s' % json.dumps(res['hit'], ensure_ascii=False), flush=True)
    else:
        b = res['best']
        if not b:
            print('[aim] ❌ 扫遍 360° 没有可用角度（全部自伤/被挡）', flush=True)
        else:
            hit = b['hit']
            who = ('目标' if hit and hit['pid'] == target else
                   ('自车' if hit and hit['pid'] == shooter else '其它') if hit else '无')
            print('[aim] ✅ 最佳角度 = %.1f°（粗扫步长 %s°）反弹 %d 次 路径命中=%s(%s) '
                  '命中距离=%.1fm'
                  % (b['theta_deg'], b['step_deg'], b['bounces'], who,
                     hit['pid'] if hit else '-', hit['s'] if hit else -1), flush=True)
            print('[aim] 折线 %d 点：' % len(b['path']), flush=True)
            for p in b['path']:
                print('      (%.2f, %.2f)  s=%.1fm' % (p['x'], p['y'], p['s']), flush=True)
            if args.fire:
                before = json.loads(await ctl.cdp.eval(
                    '(() => { const m = (GameManager.getGameController().roundController.model);'
                    ' return JSON.stringify({vict: m.playerIdVictoryCounts || null}); })()'))
                await ctl.fire(0.15, shooter)
                await asyncio.sleep(1.6)
                after = json.loads(await ctl.cdp.eval(
                    '(() => { const m = (GameManager.getGameController().roundController.model);'
                    ' return JSON.stringify({vict: m.playerIdVictoryCounts || null,'
                    ' round: GameManager.getGameController().model.getState()}); })()'))
                print('[aim] 开火前=%s' % json.dumps(before, ensure_ascii=False), flush=True)
                print('[aim] 开火后=%s' % json.dumps(after, ensure_ascii=False), flush=True)
    if args.json:
        print(json.dumps(res, ensure_ascii=False)[:2000])
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
