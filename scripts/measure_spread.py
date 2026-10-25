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
TankTrouble — 逐武器散布（精度）实测
====================================
对每个武器测两件事：
  1. **每次扣扳机出几发**（双管 2、霰弹 5、加特林连发…）
  2. **散布**：每发弹丸的初始方向与炮管方向（`forward = rot − π/2`）的夹角
     → 给 `cdp_tools/official_ballistics.WEAPONS[name]['spread_deg']` 提供实测值

流程：拿武器（`pickup_weapon.py` 的 spawnCrate + 导航）→ 提升为激活武器
（游戏自己的 `destroyWeapon` 丢掉队列里排在后面的武器）→ 页面内 rAF 逐帧采样
→ 按"扣扳机批次"分组统计。

用法：
    python scripts/measure_spread.py auto --type 5 --shots 4      # 加特林
    python scripts/measure_spread.py auto --type 2 --shots 4      # 霰弹
    python scripts/measure_spread.py auto --type 1 --shots 4      # 双管
    python scripts/measure_spread.py auto --type -1 --shots 4     # 基础弹（对照）
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

from ego_browser import resolve_port            # noqa: E402
from tankctl import TankCtl                     # noqa: E402
from pickup_weapon import spawn_crate, find_crate  # noqa: E402

NAMES = {-1: 'Bullet', 0: 'Laser', 1: 'DoubleBarrel', 2: 'Shotgun', 3: 'HomingMissile',
         4: 'Mine', 5: 'GatlingGun'}

SAMPLER = r"""(() => {
  const M = (GameManager.getGameController() || GameManager.gameController).roundController.model;
  window.__sp = {frames: [], on: true, t0: performance.now()};
  const tick = () => {
    if (!window.__sp.on || window.__sp.frames.length > 900) return;
    const gc = GameManager.getGameController() || GameManager.gameController;
    const ts = gc.getTanks() || {};
    const rots = {};
    for (const id in ts) rots[id] = ts[id].getRotation();
    const ps = M.getProjectiles ? M.getProjectiles() : {};
    const rec = {t: Math.round(performance.now() - window.__sp.t0), rot: rots, p: {}};
    for (const k in ps) {
      const p = ps[k];
      rec.p[k] = {x: p.getX(), y: p.getY(),
                  vx: p.getSpeedX ? p.getSpeedX() : 0, vy: p.getSpeedY ? p.getSpeedY() : 0,
                  owner: p.getPlayerId ? p.getPlayerId() : null,
                  ta: p.getTimeAlive ? p.getTimeAlive() : null};
    }
    window.__sp.frames.push(rec);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return 'ok';
})()"""

STOP = ("(() => { window.__sp.on = false;"
        " return JSON.stringify({t0: window.__sp.t0, frames: window.__sp.frames}); })()")

PROMOTE_JS = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const M = gc.roundController.model;
  const pid = "%(pid)s", want = %(want)d;
  const ids = (M.playerIdOtherWeaponIds[pid] || []).slice();
  const types = ids.map(id => M.weapons[id] ? M.weapons[id].getType() : null);
  if (types.indexOf(want) < 0) return JSON.stringify({ok: false, queue: types});
  // getActiveWeapon 取末位 → 丢掉所有排在目标武器之后的
  const idx = types.indexOf(want);
  for (let i = idx + 1; i < ids.length; i++) {
    M.destroyWeapon({getWeaponId: function () { return ids[i]; },
                     getPlayerId: function () { return pid; }});
  }
  return JSON.stringify({ok: true, queue: types,
                         active: gc.getActiveWeapon(pid).getType()});
})()"""


def ang_diff_deg(a, b):
    """a-b 的角度差，归一化到 [-180,180]"""
    d = math.degrees(a - b)
    return (d + 180) % 360 - 180


async def main():
    ap = argparse.ArgumentParser(description='逐武器散布实测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--type', type=int, required=True, help='武器类型（-1 基础弹，不拿箱子）')
    ap.add_argument('--shots', type=int, default=4)
    ap.add_argument('--hold', type=float, default=0.12, help='扣扳机时长')
    ap.add_argument('--hold-list', default=None,
                    help='按逗号分隔的多个扣扳机时长依次测（用于量加特林蓄力时间），如 0.1,0.3,0.5,0.7,1.0')
    ap.add_argument('--gap', type=float, default=1.2, help='两批之间间隔')
    ap.add_argument('--json', default=None, help='结果写 json')
    ap.add_argument('--frames-out', default=None,
                    help='把页面内 60Hz 原始采样帧写成 jsonl（供弹道离线验证用）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    tid = next((p for p, r in roles.items() if r == 'arrows'), None) or next(iter(roles))
    print('[spread] port=%s tank=%s 目标武器=%s(%d)'
          % (port, tid, NAMES.get(args.type, '?'), args.type), flush=True)

    # ---- 拿武器（基础弹不用拿）----
    if args.type >= 0:
        acquired = False
        for attempt in range(3):
            pos = await spawn_crate(ctl, args.type, tid)
            print('[spread] spawnCrate → %s（第 %d 次尝试）' % (pos, attempt + 1), flush=True)
            await asyncio.sleep(0.5)
            cr = await find_crate(ctl, args.type, 0, tid)
            if not cr:
                print('[spread] 找不到箱子，重试…', flush=True)
                continue
            from tank_behavior_template import TankBehavior
            bhv = TankBehavior(port=port)
            await bhv.connect()
            print('[spread] 驱车拾取 (%.0f,%.0f)…' % (cr['x'], cr['y']), flush=True)
            await bhv.navigate_to(tid, (cr['x'], cr['y']), arrival=1.0, waypoint_arrival=2.0)
            await asyncio.sleep(0.4)
            r = json.loads(await ctl.cdp.eval(PROMOTE_JS % {'pid': tid, 'want': args.type}))
            print('[spread] 提升武器: %s' % r, flush=True)
            if r.get('ok') and str(r.get('active')) == str(args.type):
                acquired = True
                break
            print('[spread] 未成功（可能回合被自己的弹丸打结束、武器已重置），重试…', flush=True)
            await asyncio.sleep(1.0)
        if not acquired:
            print('[spread] ❌ 未能把 %s 设为激活武器' % NAMES.get(args.type), flush=True)
            return 1
    # 记录当前回合，便于判断测量期间是否换局（换局会清空武器）
    rid0 = await ctl.cdp.eval(
        '(() => { const gc = GameManager.getGameController();'
        ' return String(gc.getRoundId ? gc.getRoundId() : "?"); })()')
    print('[spread] 当前回合=%s' % rid0, flush=True)

    # ---- 采样 + 开火 ----
    holds = ([float(x) for x in args.hold_list.split(',')] if args.hold_list else [args.hold])
    await ctl.cdp.eval(SAMPLER)
    fires = []
    for i in range(args.shots):
        hold = holds[i % len(holds)]
        ts_ = await ctl.tanks()
        rot = ts_[tid]['rot'] if (tid in ts_ and 'rot' in ts_[tid]) else None
        t_mark = await ctl.cdp.eval('performance.now()')
        fires.append({'page_t': t_mark, 'rot': rot, 'hold': hold})
        await ctl.set_input(tid, fire=True, hold=hold)
        if rot is None:
            print('[spread] 第 %d 批开火（按住 %.2fs，坦克当前不在场）' % (i + 1, hold), flush=True)
        else:
            print('[spread] 第 %d 批开火（按住 %.2fs，炮管 rot=%.3f → 前进角 %.1f°）'
                  % (i + 1, hold, rot, math.degrees(rot - math.pi / 2) % 360), flush=True)
        await asyncio.sleep(args.gap)
    payload = json.loads(await ctl.cdp.eval(STOP))
    frames = payload.get('frames', [])
    t0 = payload.get('t0')
    print('[spread] 采样帧数=%d' % len(frames), flush=True)
    if args.frames_out:
        with open(args.frames_out, 'w', encoding='utf-8') as fh:
            for f in frames:
                fh.write(json.dumps(f, ensure_ascii=False) + '\n')
        print('[spread] 原始帧已存 %s' % args.frames_out, flush=True)

    # ---- 分析：每发弹丸的初始方向 + 全程速度（看弹速是否恒定/是否随蓄力变化）----
    first = {}
    traces = {}
    for f in frames:
        for k, p in (f.get('p') or {}).items():
            sp = math.hypot(p.get('vx') or 0, p.get('vy') or 0)
            if sp > 0:
                traces.setdefault(k, []).append(sp)
            if k not in first and (p.get('vx') or p.get('vy')):
                first[k] = {'t': f['t'], 'x': p['x'], 'y': p['y'], 'vx': p['vx'], 'vy': p['vy'],
                            'owner': p['owner'], 'rot': f['rot'].get(str(p['owner']))}
    shots = []
    for k, p in first.items():
        if p['owner'] != tid:
            continue
        ang = math.atan2(p['vy'], p['vx'])
        barrel = (p['rot'] - math.pi / 2) if p['rot'] is not None else None
        dev = ang_diff_deg(ang, barrel) if barrel is not None else None
        tr = traces.get(k) or []
        shots.append({'id': k, 't': p['t'], 'angle': math.degrees(ang), 'dev': dev,
                      'speed': math.hypot(p['vx'], p['vy']),
                      'speed_min': min(tr) if tr else None, 'speed_max': max(tr) if tr else None,
                      'spawn': (round(p['x'], 2), round(p['y'], 2))})
    shots.sort(key=lambda s: s['t'])
    # 按"扣扳机批次"分组（同一批次内的弹丸时间接近）
    bursts, cur = [], []
    for s in shots:
        if cur and s['t'] - cur[-1]['t'] > 250:
            bursts.append(cur); cur = []
        cur.append(s)
    if cur:
        bursts.append(cur)

    print('[spread] 共 %d 发弹丸，分成 %d 批' % (len(shots), len(bursts)), flush=True)
    # 开火指令（页面时钟）→ 首发出膛延迟（= 蓄力/预热时间的证据）
    stopwatch = 0
    for i, b in enumerate(bursts):
        devs = [x['dev'] for x in b if x['dev'] is not None]
        delay = None
        if i < len(fires) and t0 is not None:
            delay = round((t0 + b[0]['t']) - fires[i]['page_t'], 1)
        print('  第%d批（按住%ss）：%d 发  偏差(°) %s  弹速首帧=%s 全程范围=%s  开火→首发出膛=%sms'
              % (i + 1, (fires[i]['hold'] if i < len(fires) else '?'), len(b),
                 ['%+.2f' % d for d in devs],
                 sorted({round(x['speed'], 1) for x in b}),
                 sorted({(round(x['speed_min'] or 0, 1), round(x['speed_max'] or 0, 1)) for x in b}),
                 delay if delay is not None else '?'), flush=True)
        if delay is not None:
            stopwatch = delay
    all_dev = [x['dev'] for x in shots if x['dev'] is not None]
    result = {'weapon': NAMES.get(args.type), 'type': args.type,
              'shots_fired': args.shots, 'projectiles': len(shots),
              'per_burst': [len(b) for b in bursts],
              'deviations_deg': [round(d, 3) for d in all_dev],
              'per_burst_devs': [[round(x['dev'], 3) for x in b] for b in bursts],
              'speed': sorted({round(x['speed'], 1) for x in shots})}
    if all_dev:
        result['dev_mean'] = round(statistics.mean(all_dev), 3)
        result['dev_stdev'] = round(statistics.pstdev(all_dev), 3) if len(all_dev) > 1 else 0.0
        result['dev_absmax'] = round(max(abs(d) for d in all_dev), 3)
        print('[spread] 偏差统计：平均 %+.3f°  标准差 %.3f°  最大绝对值 %.3f°'
              % (result['dev_mean'], result['dev_stdev'], result['dev_absmax']), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print('[spread] 结果已存 %s' % args.json, flush=True)
    after = await ctl.cdp.eval(
        '(() => { try { const w = (GameManager.getGameController()).getActiveWeapon("%s");'
        ' return w ? String(w.getType()) : "none"; } catch (e) { return "err:" + e; } })()' % tid)
    print('[spread] 打完后激活武器=%s' % after, flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
