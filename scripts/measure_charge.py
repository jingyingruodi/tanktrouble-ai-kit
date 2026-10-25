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
TankTrouble — 武器"蓄力/预热"精确测量（读武器内部状态，不依赖弹丸存活）
=====================================================================
为什么不用数弹丸：测量期间自己的弹丸会打死人 → 回合重置 → 武器清空，
采样被打断（实测多次失败）。改成**按住扳机时逐帧采样武器自身字段**：

    _weaponCharge / _triggerPulled / _newBurst / _numBullets
    _timeSinceFire / _bulletsFired

即可得到：蓄力起止时间、首次出弹时刻（`_numBullets` 减少或弹丸数出现的时刻）、
出弹节奏，而不依赖弹丸能飞多久。

用法：
    python scripts/measure_charge.py auto --type 5 --hold 1.5     # 加特林
    python scripts/measure_charge.py auto --type 1 --hold 0.8     # 双管
"""
import argparse
import asyncio
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                 # noqa: E402
from tankctl import TankCtl                          # noqa: E402
from pickup_weapon import spawn_crate, find_crate    # noqa: E402

NAMES = {-1: 'Bullet', 0: 'Laser', 1: 'DoubleBarrel', 2: 'Shotgun', 3: 'HomingMissile',
         4: 'Mine', 5: 'GatlingGun'}

PROMOTE = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const M = gc.roundController.model;
  const pid = "%(pid)s", want = %(want)d;
  const ids = (M.playerIdOtherWeaponIds[pid] || []).slice();
  const types = ids.map(id => M.weapons[id] ? M.weapons[id].getType() : null);
  if (types.indexOf(want) < 0) return JSON.stringify({ok: false, queue: types});
  const idx = types.indexOf(want);
  for (let i = idx + 1; i < ids.length; i++) {
    M.destroyWeapon({getWeaponId: function () { return ids[i]; },
                     getPlayerId: function () { return pid; }});
  }
  return JSON.stringify({ok: true, queue: types, active: gc.getActiveWeapon(pid).getType()});
})()"""

SAMPLER = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  window.__ch = {frames: [], on: true, t0: performance.now()};
  const tick = () => {
    if (!window.__ch.on || window.__ch.frames.length > 600) return;
    const g = GameManager.getGameController() || GameManager.gameController;
    const M = g.roundController.model;
    const rec = {t: Math.round(performance.now() - window.__ch.t0)};
    try {
      const pid = Object.keys(g.getTanks())[0];
      const w = g.getActiveWeapon(pid);
      rec.type = w.getType();
      rec.charge = (w._weaponCharge !== undefined) ? w._weaponCharge : null;
      rec.trigger = (w._triggerPulled !== undefined) ? w._triggerPulled : null;
      rec.newBurst = (w._newBurst !== undefined) ? w._newBurst : null;
      // ★ 2026-10-23 复核：RELEASE-2026-10-21-01 起弹药数字段改名
      //   `_numBullets` → `_bulletCount`（基础弹/加特林都是），保留旧名回退。
      rec.numBullets = (w._bulletCount !== undefined) ? w._bulletCount
                      : ((w._numBullets !== undefined) ? w._numBullets : null);
      rec.bulletsFired = (w._bulletsFired !== undefined) ? w._bulletsFired : null;
      rec.sinceFire = (w._timeSinceFire !== undefined) ? w._timeSinceFire : null;
      rec.bulletCount = (w._bulletCount !== undefined) ? w._bulletCount : null;
      rec.fired = (w._fired !== undefined) ? w._fired : null;
      rec.proj = Object.keys(M.getProjectiles()).length;
    } catch (e) { rec.err = String(e); }
    window.__ch.frames.push(rec);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return 'ok';
})()"""

STOP = "(() => { window.__ch.on = false; return JSON.stringify(window.__ch.frames); })()"


async def main():
    ap = argparse.ArgumentParser(description='武器蓄力精确测量')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--type', type=int, required=True)
    ap.add_argument('--hold', type=float, default=1.5)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    tid = next((p for p, r in roles.items() if r == 'arrows'), None) or next(iter(roles))
    print('[charge] port=%s tank=%s 目标武器=%s(%d)'
          % (port, tid, NAMES.get(args.type), args.type), flush=True)

    if args.type >= 0:
        acquired = False
        for attempt in range(4):
            await spawn_crate(ctl, args.type, tid)
            await asyncio.sleep(0.4)
            cr = await find_crate(ctl, args.type, 0, tid)
            if cr:
                from tank_behavior_template import TankBehavior
                bhv = TankBehavior(port=port)
                await bhv.connect()
                await bhv.navigate_to(tid, (cr['x'], cr['y']), arrival=1.0, waypoint_arrival=2.0)
                await asyncio.sleep(0.3)
                r = json.loads(await ctl.cdp.eval(PROMOTE % {'pid': tid, 'want': args.type}))
                print('[charge] 尝试%d 提升: %s' % (attempt + 1, json.dumps(r, ensure_ascii=False)), flush=True)
                if r.get('ok') and str(r.get('active')) == str(args.type):
                    acquired = True
                    break
            await asyncio.sleep(0.8)
        if not acquired:
            print('[charge] ❌ 未能拿到目标武器', flush=True)
            await ctl.cdp.close()
            return 1

    # ★ 读活体武器实例：内部字段 + 原型链上 fire/charge 的实现（权威口径）
    impl = await ctl.cdp.eval("""(() => {
      const gc = GameManager.getGameController() || GameManager.gameController;
      const pid = "%s";
      const w = gc.getActiveWeapon(pid);
      const out = {type: w.getType ? w.getType() : null, fields: {}, proto: {}};
      for (const k of Object.getOwnPropertyNames(w)) {
        try { const v = w[k];
          if (typeof v === 'function') continue;
          out.fields[k] = (v === null || ['number','string','boolean'].indexOf(typeof v) >= 0) ? v : 'obj';
        } catch (e) {}
      }
      let p = Object.getPrototypeOf(w), lvl = 0;
      while (p && lvl < 6) {
        for (const m of Object.getOwnPropertyNames(p)) {
          if (out.proto[m]) continue;
          try { const f = p[m];
            if (typeof f === 'function' && /fire|charge|trigger|burst|update|bullet|reload|shoot/i.test(m))
              out.proto[m] = String(f).slice(0, 520);
          } catch (e) {}
        }
        p = Object.getPrototypeOf(p); lvl++;
      }
      return JSON.stringify(out);
    })()""" % tid)
    print('[charge] 活体武器实现: %s' % impl[:2200], flush=True)

    await ctl.cdp.eval(SAMPLER)
    await asyncio.sleep(0.3)
    t_fire_page = await ctl.cdp.eval('performance.now()')
    await ctl.set_input(tid, fire=True, hold=args.hold)
    await asyncio.sleep(1.2)
    frames = json.loads(await ctl.cdp.eval(STOP))

    # 分析：触发时刻、首次出弹时刻（numBullets 减少或 proj 出现）、蓄力曲线
    trig = next((f for f in frames if f.get('trigger')), None)
    first_shot_idx = None
    base_proj = frames[0].get('proj') if frames else None
    for i, f in enumerate(frames):
        if f.get('proj') is not None and base_proj is not None and f['proj'] > base_proj:
            first_shot_idx = i
            break
    if first_shot_idx is None:
        # 退而求其次：numBullets 首次减少
        nb0 = frames[0].get('numBullets') if frames else None
        for i, f in enumerate(frames):
            if nb0 is not None and f.get('numBullets') is not None and f['numBullets'] < nb0:
                first_shot_idx = i
                break
    print('[charge] 采样帧数=%d  扳机按下帧=%s  首次出弹帧=%s'
          % (len(frames), (frames.index(trig) if trig in frames else None), first_shot_idx), flush=True)
    if trig and first_shot_idx is not None:
        print('[charge] ★ 从扳机按下到首次出弹 = %d ms（蓄力/预热时间）'
              % (frames[first_shot_idx]['t'] - trig['t']), flush=True)
    ch = [f.get('charge') for f in frames if f.get('charge') is not None]
    if ch:
        print('[charge] _weaponCharge: min=%s max=%s 非零帧数=%d/%d'
              % (min(ch), max(ch), sum(1 for c in ch if c), len(ch)), flush=True)
    print('[charge] 前 20 帧（t, charge, trigger, numBullets, proj）:', flush=True)
    for f in frames[:20]:
        print('      t=%-5s charge=%-8s trigger=%-6s numBullets=%-5s proj=%s'
              % (f.get('t'), f.get('charge'), f.get('trigger'), f.get('numBullets'), f.get('proj')), flush=True)
    print('[charge] 末 5 帧:', flush=True)
    for f in frames[-5:]:
        print('      t=%-5s type=%s numBullets=%s proj=%s' % (f.get('t'), f.get('type'), f.get('numBullets'), f.get('proj')), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'type': args.type, 'name': NAMES.get(args.type), 'hold': args.hold,
                       'frames': frames}, fh, ensure_ascii=False)
        print('[charge] 原始帧 → %s' % args.json, flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
