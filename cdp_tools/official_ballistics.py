"""
TankTrouble — 自建弹道与命中预测（以官方 p2 射线为基础）
=======================================================
设计取舍（来自 2026-10-02 的逆向与实测，详见 `docs/10-激光武器与瞄准线.md`）：

**直接复用官方的部分**（`UIAimerGraphics._castRay` 的那套 p2 射线）：
  * 炮口起点 = 车体中心 + 朝向 × `UIUtils.pxm(UIConstants.AIMER_OFFSET)`（实测 2.50m）
  * 镜面反射、p2 镜像坐标系、`skipBackfaces`
  * 实测与真实弹丸吻合到 0.005m（`scripts/laser_reconstruct.py`）

**我们必须自己加的部分**（官方只有几何布尔值）：
  1. **身份**：官方所有坦克共用一个 collisionGroup，`shape` 上无 playerId → 用我们自己的
     "圆心到折线段最短距离 ≤ 坦克半径 + 弹丸半径" 判定，取沿路径最早命中；
  2. **弹丸半径**：官方是零半径射线，擦边/贴墙会误判；
  3. **真实射程**：官方被 `AIMER_LENGTH=60m` 截断 → 改用 `弹速 × 寿命`
     （基础弹 18×10=180m，激光 180×0.8=144m，v7 录制里 `maxLifetime=10` 已确认）；
  4. **自伤规则**：游戏字段 `deadlyToOwner` 在**第一次撞墙反弹**时翻转为 true
     （v7 录制标定）→ 直射弹不会自伤，只检查首次反弹之后的路径；
  5. **目标提前量**：可传入"虚拟目标圆"（预测的未来位置）。

用法（库）：
    from official_ballistics import OfficialBallistics
    ob = OfficialBallistics(ctl)                       # ctl 需有 .cdp.eval
    r = await ob.find_angle(shooter_pid, ['enemy_pid'], weapon='bullet')
    r['angle_deg'], r['hit'], r['bounces'], r['path']

用法（自检 CLI）：
    python cdp_tools/official_ballistics.py auto [--weapon bullet|laser] [--shooter PID --target PID]
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 武器参数表：弹速 m/s、寿命 s、出膛延迟 ms、**蓄力/预热 ms**、散布（度）、弹药
#   speed/lifetime 为实测或录制标定值；None 表示尚未实测（留待逐武器采集）
#   charge_ms：扣扳机后需要"预热"多久才开始出弹（加特林最典型，实测见 docs/10 §12）
#
# ★ 2026-10-24 新增（权威值，取自游戏常量 `Constants.<WEAPON>`，见 docs/14）：
#   radius        弹丸碰撞半径（米）——**各武器不同**，用于命中判定
#   muzzle_offset 炮口偏移（米）——**各武器不同**，弹丸从车体中心沿朝向偏移这么多出膛
#   来源：BULLET {RADIUS:{px:5,m:0.25}, OFFSET:{px:50,m:2.5}, SPEED:18}
#         LASER {RADIUS:0, OFFSET:2.5, SPEED:180}
#         SHOTGUN {RADIUS:0.1, OFFSET:2.45, SPEED:30~35, MAX_LIFETIME:2, AMMO_COUNT:3, BUCKSHOT:20}
#         HOMING_MISSILE {RADIUS:0.2, OFFSET:2.5, SPEED:18, ACCELERATION:40}
#         GATLING_GUN {RADIUS:0.1, OFFSET:3.0, SPEED:25~30, BULLET_SPREAD:0.1}
#         DOUBLE_BARREL {RADIUS:0.25, OFFSET:2.25, SPEED:18, SPACE:0.45}
#         MINE {RADIUS:0.8, LAUNCH_SPEED:12.5, MIN_SPEED:25}
#   全局规则：PROJECTILE_BOUNCE_TIMEOUT_COUNT=5（**弹丸最多反弹 5 次即消亡**）、WINDOW=35s
WEAPONS = {
    'bullet':        {'speed': 18.0,  'lifetime': 10.0, 'fire_delay_ms': None, 'charge_ms': 0,
                      'spread_deg': 0.0, 'ammo': 5, 'radius': 0.25, 'muzzle_offset': 2.50,
                      'bounces_max': 5,
                      'notes': '基础弹；半径 0.25m（运行时 p2 circle 实测一致）、炮口 2.5m、'
                               '弹仓 5、寿命 10s、反弹上限 5；实测偏差恒 0.00°'},
    'laser':         {'speed': 180.0, 'lifetime': 0.8,  'fire_delay_ms': 9.8,  'charge_ms': 0,
                      'spread_deg': 0.0, 'ammo': 1, 'radius': 0.0, 'muzzle_offset': 2.50,
                      'notes': '半径 **0**（零宽射线，无碰撞半径）、炮口 2.5m；实测 180m/s、0.8s、1 发、无蓄力'},
    'double_barrel': {'speed': 18.0,  'lifetime': 10.0, 'fire_delay_ms': 9.0,  'charge_ms': 0,
                      'spread_deg': 0.0, 'ammo': 10, 'radius': 0.25, 'muzzle_offset': 2.25,
                      'bounces_max': 5, 'space_m': 0.45,
                      'notes': '★ 实测：一次扳机只出 **1 发**；半径 0.25m、炮口 **2.25m**、弹速 18.0、'
                               '偏差恒 0.00°、出膛延迟 6.9~9.8ms、弹仓 10、无蓄力'},
    'shotgun':       {'speed': 32.0,  'lifetime': 2.0,  'fire_delay_ms': 6.3,  'charge_ms': 0,
                      'spread_deg': 4.0, 'ammo': 60, 'radius': 0.10, 'muzzle_offset': 2.45,
                      'space_m': 0.40,
                      'notes': '★ 实测：一次扣扳机 20 发（常数 BUCKSHOT_COUNT=20）；半径 **0.10m**、'
                               '炮口 2.45m；每发弹速 30.5~34.9（常数 30~35）各不相同；'
                               '**寿命 2s**（常数 SHOTGUN_MAX_LIFETIME=2 → 射程约 64m，不是 10s！）；'
                               '弹仓 3 次扳机（AMMO_COUNT=3）；偏差 σ≈4.0°（常数 SPREAD=0.3）'},
    'homing_missile': {'speed': 18.0, 'lifetime': 10.0, 'fire_delay_ms': None, 'charge_ms': 0,
                       'spread_deg': 0.0, 'ammo': 1, 'radius': 0.20, 'muzzle_offset': 2.50,
                       'acceleration': 40.0, 'activation_time': 2.0,
                       'notes': '追踪弹：半径 **0.20m**、炮口 2.5m、初速 18 + 加速度 40、'
                                '激活时间 2s、寿命 10s → 需单独建模（会转向）'},
    'mine':          {'speed': 12.5,  'lifetime': None, 'fire_delay_ms': None, 'charge_ms': 0,
                      'spread_deg': 0.0, 'ammo': 1, 'radius': 0.80, 'min_speed': 25.0,
                      'notes': '部署型/抛射：半径 **0.8m**、发射速度 12.5、最低速度 25'},
    # 加特林：★ 2026-10-02 用"武器内部状态逐帧采样"精确标定（不再靠数弹丸）
    'gatling_gun':   {'speed': 27.0,  'lifetime': 2.0,  'fire_delay_ms': None, 'charge_ms': 486,
                      'spread_deg': 1.6, 'ammo': 20, 'fire_interval_ms': 119,
                      'radius': 0.10, 'muzzle_offset': 3.00, 'space_m': 0.40, 'bounces_max': 5,
                      'notes': '★ 精确实测：蓄力 486ms（`GATLING_GUN_CHARGE_TIME`）、'
                               '射速 119ms/发、弹仓 20、打空切回基础弹；'
                               '**寿命 2s（常数 GATLING_GUN_MAX_LIFETIME=2）→ 射程≈54m，不是 10s**；'
                               '**半径 0.10m（比基础弹小）、炮口 3.0m**；'
                               '弹速 25.4~29.6（常数 25~30）、散布 σ≈1.58°（常数 BULLET_SPREAD=0.1）。'
                               '⚠️ 在线输入粒度 0.5s → 在线要按住 **≥0.6s** 才可能出弹'},
}

# 默认几何参数（坦克半径按 p2 车体实测 ~2.2m；弹丸半径待精测，先给保守 0.3m）
TANK_R = 2.2
BULLET_R = 0.3
MUZZLE_EXIT = 3.5          # 炮口出膛段豁免（米）
SCAN_STEP = 1.0            # 粗扫步长（度）
REFINE_STEP = 0.1          # 细扫步长（度）

_BALLISTICS_JS = r"""(() => {
  const P = PARAMS;
  const gc = GameManager.getGameController() || GameManager.gameController;
  const U = window.UIUtils;
  const world = GameManager.phaserInstance.physics.p2.world;
  const WALL = U.wallCollisionGroup.mask, TANK = U.tankCollisionGroup.mask;
  const SHIELD = U.shieldCollisionGroup.mask, SPAWN = U.spawnCollisionGroup.mask;

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
            dist: res.getHitDistance(ray), isShield: cg === SHIELD, isSpawn: cg === SPAWN};
  }

  // 官方弹道：炮口起点 + 镜面反射，走满预算（护盾/出生点即止；坦克由我们自己的判定处理）
  function walk(ax, ay, theta, L) {
    let x = ax, y = ay, dx = Math.cos(theta), dy = Math.sin(theta);
    const pts = [{x: x, y: y, s: 0}], bounces = [];
    let remain = L, travelled = 0, guard = 0, blocked = null;
    while (remain > 1e-6 && guard++ < 80) {
      const r = cast(x, y, x + dx * remain, y + dy * remain, false);
      if (r.hit && r.dist < remain - 1e-6) {
        x = r.x; y = r.y; travelled += Math.max(0, r.dist);
        pts.push({x: x, y: y, s: travelled});
        if (r.isShield || r.isSpawn) { blocked = r.isShield ? 'shield' : 'spawn'; break; }
        bounces.push({x: x, y: y, s: travelled});
        remain -= Math.max(1e-3, r.dist);
        const dot = dx * r.nx + dy * r.ny;
        const nx2 = dx - 2 * dot * r.nx, ny2 = dy - 2 * dot * r.ny;
        const m = Math.hypot(nx2, ny2) || 1;
        dx = nx2 / m; dy = ny2 / m;
      } else {
        x += dx * remain; y += dy * remain; travelled += remain; remain = 0;
        pts.push({x: x, y: y, s: travelled});
      }
    }
    return {pts: pts, bounces: bounces, bounceCount: bounces.length,
            blocked: blocked, travel: travelled};
  }

  function distToSeg(px, py, ax, ay, bx, by) {
    const vx = bx - ax, vy = by - ay, wx = px - ax, wy = py - ay;
    const L2 = vx * vx + vy * vy;
    let t = L2 > 1e-12 ? (wx * vx + wy * vy) / L2 : 0;
    t = Math.max(0, Math.min(1, t));
    return {d: Math.hypot(px - (ax + t * vx), py - (ay + t * vy)), t: t};
  }

  // 自建命中判定：最早命中的目标圆（含自车；自车只在首次反弹后算）
  function firstHit(path, bounces, targets, shooterId, tankR, bulletR) {
    const firstBounceS = bounces.length ? bounces[0].s : Infinity;
    let best = null;
    for (let i = 1; i < path.length; i++) {
      const a = path[i-1], b = path[i], segLen = b.s - a.s;
      for (const tid in targets) {
        const T = targets[tid], R = (T.r !== undefined ? T.r : tankR) + bulletR;
        const r = distToSeg(T.x, T.y, a.x, a.y, b.x, b.y);
        if (r.d > R) continue;
        const hitS = a.s + r.t * segLen;
        if (tid === shooterId) {
          if (hitS < P.muzzleExit) continue;        // 炮口出膛段
          if (hitS <= firstBounceS) continue;       // ★ 反弹前对自己无害（deadlyToOwner 规则）
        }
        if (!best || hitS < best.s) {
          best = {pid: tid, s: hitS, point: {x: a.x + r.t * (b.x - a.x), y: a.y + r.t * (b.y - a.y)}};
        }
      }
    }
    return best;
  }

  const shooter = gc.getTank(P.shooter);
  if (!shooter && !P.origin) return JSON.stringify({err: 'no shooter ' + P.shooter});
  const sx = P.origin ? P.origin.x : shooter.getX();
  const sy = P.origin ? P.origin.y : shooter.getY();
  const off = P.origin ? 0.0
      : (P.muzzleOffset !== undefined && P.muzzleOffset !== null
          ? P.muzzleOffset : U.pxm(window.UIConstants.AIMER_OFFSET));

  function evaluate(theta) {
    const ax = sx + Math.cos(theta) * off, ay = sy + Math.sin(theta) * off;
    const w = walk(ax, ay, theta, P.budget);
    const hit = firstHit(w.pts, w.bounces, P.targets, P.shooter, P.tankR, P.bulletR);
    return {theta: theta, path: w.pts, bounces: w.bounceCount, blocked: w.blocked,
            travel: w.travel, hit: hit};
  }

  if (P.mode === 'path') {
    const theta = P.angleDeg * Math.PI / 180;
    const e = evaluate(theta);
    return JSON.stringify({mode: 'path', origin: {x: sx, y: sy}, offset: off,
                           budget: P.budget, theta_deg: P.angleDeg, path: e.path,
                           bounces: e.bounces, travel: e.travel, blocked: e.blocked, hit: e.hit});
  }

  // 角度扫描：粗扫 + 局部细扫
  let best = null;
  const ok = (e) => {
    if (e.blocked) return false;                                  // 被护盾/出生点挡
    if (!e.hit) return false;                                     // 什么都没打中
    if (e.hit.pid === P.shooter) return false;                     // 打自己
    if (P.prefer.length && P.prefer.indexOf(e.hit.pid) < 0) return false;
    if (P.requireBounce && e.bounces < 1) return false;
    if (P.maxBounces > 0 && e.bounces > P.maxBounces) return false;
    if (P.minDist > 0 && e.hit.s < P.minDist) return false;
    if (e.hit.s > P.maxTravel) return false;
    return true;
  };
  const consider = (deg) => {
    const e = evaluate(deg * Math.PI / 180);
    if (!ok(e)) return;
    const score = 1e6 - e.hit.s - e.bounces * 5;                  // 越短越好，其次越少反弹越好
    if (!best || score > best.score) best = {score: score, e: e, deg: deg};
  };
  for (let d = -180; d < 180; d += P.scanStep) consider(d);
  if (best) {
    const c = best.deg;
    for (let d = c - P.scanStep; d <= c + P.scanStep; d += P.refineStep) consider(d);
  }
  return JSON.stringify({mode: 'scan', origin: {x: sx, y: sy}, offset: off, budget: P.budget,
                         best: best ? {angle_deg: best.e.theta * 180 / Math.PI,
                                       bounces: best.e.bounces, travel: best.e.travel,
                                       hit: best.e.hit, path: best.e.path,
                                       blocked: best.e.blocked} : null});
})()"""


class OfficialBallistics:
    """以官方 p2 射线为弹道基础、命中判定自建的预测器。"""

    def __init__(self, ctl, tank_r=TANK_R, bullet_r=BULLET_R, muzzle_exit=MUZZLE_EXIT):
        self.ctl = ctl
        self.tank_r = tank_r
        self.bullet_r = bullet_r
        self.muzzle_exit = muzzle_exit

    # ---------- 内部 ----------
    async def _tanks(self):
        return await self.ctl.tanks()

    async def _roles(self):
        try:
            return await self.ctl.detect_roles()
        except Exception:
            return {}

    def _params(self, shooter, targets, weapon, mode, **kw):
        w = WEAPONS[weapon] if isinstance(weapon, str) else weapon
        budget = kw.get('budget')
        if budget is None:
            budget = (w['speed'] * w['lifetime']) if (w.get('speed') and w.get('lifetime')) else 1e9
        return {
            'mode': mode, 'shooter': shooter, 'targets': targets, 'prefer': kw.get('prefer') or [],
            'budget': budget,
            # ★ 每武器不同：半径与炮口偏移都取武器表（缺省回退到类默认）
            #   实测来源：游戏常量 Constants.<WEAPON>.RADIUS / OFFSET（2026-10-24，见 docs/14）
            'tankR': self.tank_r,
            'bulletR': (w.get('radius') if w.get('radius') is not None else self.bullet_r),
            'muzzleOffset': w.get('muzzle_offset'),
            'muzzleExit': self.muzzle_exit,
            'requireBounce': bool(kw.get('require_bounce')),
            'maxBounces': int(kw.get('max_bounces') or 0),
            'minDist': float(kw.get('min_dist') or 0.0),
            'maxTravel': float(kw.get('max_travel') or 1e9),
            'scanStep': float(kw.get('scan_step') or SCAN_STEP),
            'refineStep': float(kw.get('refine_step') or REFINE_STEP),
            'angleDeg': kw.get('angle_deg'),
            'origin': kw.get('origin'),
        }

    async def _run(self, params):
        js = _BALLISTICS_JS.replace('PARAMS', json.dumps(params))
        raw = await self.ctl.cdp.eval(js)
        return json.loads(raw) if raw else {}

    # ---------- 公开 API ----------
    async def find_angle(self, shooter, targets=None, weapon='bullet', prefer=None,
                         require_bounce=False, max_bounces=0, min_dist=0.0,
                         max_travel=None, budget=None, scan_step=None, refine_step=None):
        """扫描 360° 找最佳射击角。

        返回 {'angle_deg','bounces','travel','hit':{'pid','s','point'},'path':[...]} 或 None。
        """
        if targets is None:
            ts = await self._tanks()
            targets = [p for p in ts if p != shooter]
        extra = {}
        if isinstance(targets, dict):            # 允许 {pid: {x,y,r}} 形式（虚拟目标/提前量）
            circles, prefer = targets, list(targets)
        else:
            ts = await self._tanks()
            circles = {p: {'x': ts[p]['x'], 'y': ts[p]['y']} for p in targets if p in ts}
        res = await self._run(self._params(shooter, circles, weapon, 'scan',
                                          prefer=prefer, budget=budget,
                                          require_bounce=require_bounce, max_bounces=max_bounces,
                                          min_dist=min_dist, max_travel=max_travel,
                                          scan_step=scan_step, refine_step=refine_step, **extra))
        return res.get('best')

    async def predict_path(self, shooter, angle_deg, weapon='bullet', budget=None, origin=None):
        """给定角度，返回官方弹道折线 + 我们自己的命中判定（调试/离线验证用）。

        origin 给定时从该点出发（炮口偏移不再叠加）——离线验证真实弹丸时用它的出膛点。
        """
        ts = await self._tanks()
        circles = {p: {'x': t['x'], 'y': t['y']} for p, t in ts.items()}
        return await self._run(self._params(shooter, circles, weapon, 'path',
                                            angle_deg=angle_deg, budget=budget,
                                            origin=origin))

    @staticmethod
    def range_of(weapon):
        """真实射程（米）= 弹速 × 寿命；数据缺失返回 None。"""
        w = WEAPONS.get(weapon) or {}
        if w.get('speed') is None or w.get('lifetime') is None:
            return None
        return w['speed'] * w['lifetime']

    @staticmethod
    def weapon_info(weapon):
        w = dict(WEAPONS.get(weapon) or {})
        w['name'] = weapon
        w['range_m'] = OfficialBallistics.range_of(weapon)
        return w

    # ---------- 时间参数化查询 ----------
    @staticmethod
    def timeline(path, speed):
        """给折线每个顶点补上到达时间 t = s / speed（秒）。"""
        out = []
        for p in path or []:
            q = dict(p)
            q['t'] = (p.get('s', 0.0) / speed) if speed else None
            out.append(q)
        return out

    @staticmethod
    def position_at(path, speed, t, weapon=None):
        """**查询 t 秒时弹丸在哪里**（线性插值）。

        返回 {'x','y','s','leg'}；t 超出射程/寿命返回 None。
        说明：真实弹丸有散布（如加特林），这里是理想弹道上的位置。
        """
        if not path or not speed:
            return None
        if weapon:
            info = WEAPONS.get(weapon) or {}
            if info.get('lifetime') is not None and t > info['lifetime']:
                return None
        s_target = speed * t
        total = path[-1].get('s', 0.0)
        if s_target > total:
            return None
        for i in range(1, len(path)):
            a, b = path[i-1], path[i]
            if b.get('s', 0.0) >= s_target:
                seg = b['s'] - a['s']
                f = (s_target - a['s']) / seg if seg > 1e-9 else 0.0
                return {'x': a['x'] + f * (b['x'] - a['x']),
                        'y': a['y'] + f * (b['y'] - a['y']),
                        's': s_target, 'leg': i}
        return None

    @staticmethod
    def hit_radius_with_spread(base_radius, distance, weapon):
        """把武器散布折算成等效命中半径：r_eff = r + d·tan(spread)。"""
        sp = (WEAPONS.get(weapon) or {}).get('spread_deg')
        if not sp:
            return base_radius
        return base_radius + distance * math.tan(math.radians(sp))


# ----------------------------- 自检 CLI -----------------------------
async def _selftest(port_arg='auto', weapon='bullet', shooter=None, target=None, angle=None):
    from ego_browser import resolve_port
    from tankctl import TankCtl

    port = resolve_port(port_arg)
    ctl = TankCtl(port=port)
    await ctl.connect()
    ob = OfficialBallistics(ctl)
    ts = await ctl.tanks()
    if not ts:
        print('[self] 没有坦克（未进对局？）', flush=True)
        return 1
    shooter = shooter or next(iter(ts))
    target = target or next((p for p in ts if p != shooter), None)
    print('[self] port=%s shooter=%s target=%s weapon=%s 射程=%.0fm'
          % (port, shooter, target, weapon, ob.range_of(weapon)), flush=True)

    if angle is not None:
        r = await ob.predict_path(shooter, angle, weapon=weapon)
        print('[self] 角度 %.1f° → 折线 %d 点 反弹 %d 次 路径 %.1fm 阻挡=%s 命中=%s'
              % (angle, len(r.get('path') or []), r.get('bounces'), r.get('travel'),
                 r.get('blocked'), json.dumps(r.get('hit'), ensure_ascii=False)), flush=True)
        return 0

    if target:
        best = await ob.find_angle(shooter, [target], weapon=weapon)
        if not best:
            print('[self] ❌ 360° 扫描没有可用角度', flush=True)
            return 1
        print('[self] ✅ 最佳角度 %.1f° 反弹 %d 次 路径 %.1fm 命中 %s 于 %.1fm'
              % (best['angle_deg'], best['bounces'], best['travel'],
                 best['hit']['pid'], best['hit']['s']), flush=True)
        print('[self] 折线: %s' % ' → '.join('(%.1f,%.1f)' % (p['x'], p['y']) for p in best['path']),
              flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description='自建弹道与命中预测（自检）')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--weapon', default='bullet', choices=list(WEAPONS))
    ap.add_argument('--shooter', default=None)
    ap.add_argument('--target', default=None)
    ap.add_argument('--angle', type=float, default=None)
    a = ap.parse_args()
    sys.exit(asyncio.run(_selftest(a.port, a.weapon, a.shooter, a.target, a.angle)))
