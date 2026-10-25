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
逐武器运行时参数实测（游戏内）
================================
对每种武器：用游戏自身的 `RoundModel.setWeaponState(事件)` 创建该武器（原生创建路径），
开一枪，然后用页面内 60Hz 采样读**真实弹丸**的：
  * p2 碰撞体半径（`body.m_fixtureList.m_shape.m_radius`）
  * lifetime / timeoutBounceCount / initialSpeed
  * 弹丸数量（霰弹一次 20 发）

与官方常量（`game_source/constants.js`）对照输出表格。

用法：python scripts/measure_weapon_runtime.py auto [--json out.json]
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tankctl import TankCtl                          # noqa: E402
import tank_constants as C                           # noqa: E402

NAMES = {-1: 'BULLET', 0: 'LASER', 1: 'DOUBLE_BARREL', 2: 'SHOTGUN',
         3: 'HOMING_MISSILE', 4: 'MINE', 5: 'GATLING_GUN'}
# 官方常量（game_source/constants.js）里各武器的半径 m
EXPECTED_RADIUS = {'BULLET': 0.25, 'LASER': 0.0, 'DOUBLE_BARREL': 0.25, 'SHOTGUN': 0.10,
                   'HOMING_MISSILE': 0.20, 'MINE': 0.80, 'GATLING_GUN': 0.10}
# 各武器需要的下划线字段（_setFields 会按实例字段逐个拷贝；缺一个就 undefined）
FIELD_DEFAULTS = {
    '_bulletCount': 20, '_triggerPulled': False, '_weaponCharge': 0.0,
    '_timeSinceFire': 0.0, '_newBurst': True,
    '_launched': False, '_activationTime': 0.0,
    '_fired': False, '_shots': 0, '_detonated': False, '_armed': False,
}

GRANT = r"""(() => {
  const gc = GameManager.getGameController(); const M = gc.roundController.model;
  const pid = %(pid)s; const want = %(want)d;
  const ev = {
    getId: function () { return 'wprobe-' + want + '-' + Date.now(); },
    getPlayerId: function () { return pid; },
    getType: function () { return want; },
    getFields: function () { return %(fields)s; },
  };
  let err = null;
  try { M.setWeaponState(ev); } catch (e) { err = String(e); }
  // 清掉队列里其它武器，把目标武器提为当前
  const ids = (M.playerIdOtherWeaponIds[pid] || []).slice();
  for (const id of ids) {
    const w = M.weapons[id];
    if (!w) continue;
    if (w.getType() !== want) {
      try { M.destroyWeapon({getWeaponId: function () { return id; },
                             getPlayerId: function () { return pid; }}); } catch (e) {}
    }
  }
  let active = null;
  try { active = gc.getActiveWeapon(pid).getType(); } catch (e) { active = 'err:' + e; }
  return JSON.stringify({err: err, active: active,
    queue: (M.playerIdOtherWeaponIds[pid] || []).map(id => M.weapons[id] ? M.weapons[id].getType() : null)});
})()"""

SAMPLE = r"""(() => {
  window.__wp = {on: true, frames: []};
  const step = () => {
    if (!window.__wp.on || window.__wp.frames.length > 200) return;
    const g = GameManager.getGameController();
    if (g) { try { const M = g.roundController.model; const ps = M.getProjectiles();
      const ids = Object.keys(ps);
      if (ids.length) { const p = ps[ids[0]]; const b = p.getB2DBody();
        const sh = b && b.m_fixtureList && b.m_fixtureList.m_shape;
        window.__wp.frames.push({n: ids.length, radius: sh ? sh.m_radius : null,
          lifetime: p.lifetime, bounces: p.timeoutBounceCount, speed: p.initialSpeed}); } } catch (e) {} }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step); return 'ok'; })()"""

STOP = "(() => { window.__wp.on = false; return JSON.stringify(window.__wp.frames); })()"


async def main():
    ap = argparse.ArgumentParser(description='逐武器运行时参数实测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    ap.add_argument('--types', default='-1,0,1,2,3,4,5')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    pid = next(iter(roles), None)
    if not pid:
        print('[wp] 无坦克（先启动一局本地游戏）')
        return 1

    rows = []
    for t in [int(x) for x in args.types.split(',')]:
        name = NAMES.get(t, str(t))
        fields = dict(FIELD_DEFAULTS)
        if t == 0:      # 激光：单发
            fields.update({'_bulletCount': 1})
        elif t == 1:
            fields.update({'_bulletCount': 10})
        elif t == 2:
            fields.update({'_bulletCount': 60})
        elif t == 3:
            fields.update({'_launched': False, '_activationTime': 0.0})
        elif t == 4:
            fields.update({'_detonated': False})
        elif t == 5:
            fields.update({'_bulletCount': 20})
        else:
            fields.update({'_bulletCount': 5})
        js = GRANT % {'pid': json.dumps(pid), 'want': t, 'fields': json.dumps(fields)}
        r = json.loads(await ctl.cdp.eval(js))
        if str(r.get('active')) != str(t):
            rows.append({'type': t, 'name': name, 'grant': r, 'ok': False})
            print('[wp] %-14s 创建失败/未激活: %s' % (name, r), flush=True)
            continue
        await ctl.cdp.eval(SAMPLE)
        hold = 0.75 if t == 5 else 0.45
        await ctl.set_input(pid, fire=True, hold=hold)
        frames = json.loads(await ctl.cdp.eval(STOP) or '[]')
        best = None
        for f in frames:
            if f.get('radius') is not None:
                best = f
                break
        if best is None:
            # 再等一会（加特林蓄力/霰弹等）
            await asyncio.sleep(0.5)
            frames = json.loads(await ctl.cdp.eval(STOP) or '[]')
            for f in frames:
                if f.get('radius') is not None:
                    best = f
                    break
        ok = best is not None
        exp = EXPECTED_RADIUS.get(name)
        rows.append({'type': t, 'name': name, 'ok': ok, 'runtime': best,
                     'expected_radius': exp, 'grant': r})
        if ok:
            diff = abs((best['radius'] or 0) - (exp if exp is not None else 0))
            print('[wp] %-14s 运行时半径=%.3f m（常量 %.3f，差 %.3f） lifetime=%s bounces=%s speed=%s 弹丸数=%s'
                  % (name, best['radius'], exp if exp else 0, diff, best.get('lifetime'),
                     best.get('bounces'), best.get('speed'), best.get('n')), flush=True)
        else:
            print('[wp] %-14s 未采到弹丸（active=%s）' % (name, r.get('active')), flush=True)
        await asyncio.sleep(0.4)

    print('\n[wp] ===== 汇总 =====', flush=True)
    print('%-14s %-10s %-10s %-8s' % ('武器', '运行时半径', '常量半径', '半径一致'))
    for row in rows:
        rt = row.get('runtime') or {}
        r = rt.get('radius')
        exp = row.get('expected_radius')
        same = ('✅' if (r is not None and exp is not None and abs(r - exp) < 0.005)
                else ('—' if r is None else '❌'))
        print('%-14s %-10s %-10s %-8s' % (row['name'], r if r is not None else 'N/A',
                                          exp if exp is not None else 'N/A', same))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print('→ %s' % args.json)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
