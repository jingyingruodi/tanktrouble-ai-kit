#!/usr/bin/env python
"""
TankTrouble — 激光武器与官方瞄准线检查
======================================
用途：把指定坦克的**激光武器**推到激活位（丢掉排在它后面的武器），然后读取
游戏自己的瞄准线（`LaserAimerUpgrade` → `UIAimerGraphics`）：

  * 武器队列与激活武器（`model.playerIdOtherWeaponIds` / `getActiveWeapon`）
  * 激光升级是否存在（`UPGRADE_TYPES.LASER_AIMER = 0`）
  * 瞄准线对象：`playerId / maxLength / activated / colour`
  * **官方预测折线**：从 Phaser `Graphics` 的 `graphicsData` 里把画出来的点还原成米
    （绘制时用 `UIUtils.mpx()` 米→像素，还原用 `UIUtils.pxm()` 像素→米 = ×0.05）

丢武器用的是游戏自己的 `roundController.model.destroyWeapon(deactivation)`：
它只调用传入对象的 `getWeaponId()/getPlayerId()`，所以鸭子类型对象即可 —— 属于
**调用游戏自己的方法**，不是 hook/替换（项目红线）。

用法：
    python scripts/laser_inspect.py auto                  # 把激光推到激活位并检查
    python scripts/laser_inspect.py auto --no-promote     # 只检查，不改武器
"""
import argparse
import asyncio
import json
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

from ego_browser import resolve_port  # noqa: E402
from tankctl import TankCtl           # noqa: E402

WEAPON_NAMES = {-1: 'Bullet', 0: 'Laser', 1: 'DoubleBarrel', 2: 'Shotgun',
                3: 'HomingMissile', 4: 'Mine', 5: 'GatlingGun'}
UPGRADE_NAMES = {0: 'LaserAimer', 1: 'SpawnShield', 2: 'Shield', 3: 'Aimer', 4: 'SpeedBoost'}

STATUS_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const M = gc.roundController.model;
  const out = {tanks: {}};
  for (const id in gc.getTanks()) {
    const rec = {};
    try { rec.weapon = gc.getActiveWeapon(id).getType(); } catch (e) {}
    try {
      const ids = M.playerIdOtherWeaponIds[id] || [];
      rec.queue = ids.map(wid => ({id: wid, type: M.weapons[wid] ? M.weapons[wid].getType() : null}));
    } catch (e) {}
    try {
      rec.upgrades = {};
      for (const t of [0, 1, 2, 3, 4]) {
        try { const u = M.getUpgradeByPlayerIdAndType(id, t); if (u) rec.upgrades[UPGRADE_NAMES_JS[t]] = true; } catch (e) {}
      }
    } catch (e) {}
    out.tanks[id] = rec;
  }
  return JSON.stringify(out);
})()""".replace('UPGRADE_NAMES_JS',
                '{0:"LaserAimer",1:"SpawnShield",2:"Shield",3:"Aimer",4:"SpeedBoost"}')

AIMER_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const g = gc.roundController;
  const st = GameManager.phaserInstance.state.getCurrentState();
  const out = {groups: {}};
  for (const gname of ['aimerGroup', 'tankNameGroup']) {
    const grp = st[gname];
    out.groups[gname] = grp ? grp.children.map(c => {
      const rec = {visible: c.visible, exists: c.exists, playerId: c.playerId};
      if (c.maxLength !== undefined) rec.maxLength = c.maxLength;
      if (c.activated !== undefined) rec.activated = c.activated;
      if (c.colour !== undefined) rec.colour = c.colour;
      if (c.laserId !== undefined) rec.laserId = c.laserId;
      if (c.timeAlive !== undefined) rec.timeAlive = c.timeAlive;
      if (c.expanding !== undefined) rec.expanding = c.expanding;
      if (c.retracting !== undefined) rec.retracting = c.retracting;
      // 画出来的点（Phaser CE 的 Graphics 把点存成扁平数组 [x0,y0,x1,y1,...]，单位像素）
      try {
        const data = c.graphicsData || (c._graphics && c._graphics.graphicsData) || [];
        const pts = [];
        for (const p of data) {
          const arr = p.points;
          if (!arr || !arr.length) continue;
          if (typeof arr[0] === 'object' && arr[0] !== null) {
            for (const q of arr) pts.push({x: q.x, y: q.y});
          } else {
            for (let i = 0; i + 1 < arr.length; i += 2) pts.push({x: arr[i], y: arr[i + 1]});
          }
        }
        if (pts.length) {
          rec.drawnPointsPx = pts.length;
          rec.drawnRawFirst = data.length ? (data[0].points || []).slice(0, 6) : null;
          rec.drawnPointsM = pts.map(q => ({x: Math.round(q.x * 0.05 * 100) / 100,
                                            y: Math.round(q.y * 0.05 * 100) / 100}));
          const xs = pts.map(q => q.x), ys = pts.map(q => q.y);
          rec.drawnExtentPx = {minX: Math.round(Math.min.apply(null, xs)),
                               maxX: Math.round(Math.max.apply(null, xs)),
                               minY: Math.round(Math.min.apply(null, ys)),
                               maxY: Math.round(Math.max.apply(null, ys))};
        }
      } catch (e) { rec.gErr = String(e); }
      return rec;
    }) : null;
  }
  return JSON.stringify(out);
})()"""


async def main():
    ap = argparse.ArgumentParser(description='激光武器与官方瞄准线检查')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--tank', default=None)
    ap.add_argument('--no-promote', action='store_true', help='不丢武器，只检查')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    tid = args.tank or next((p for p, r in roles.items() if r == 'arrows'), None) \
        or next(iter(roles), None)
    if not tid:
        print('[laser] 没有坦克', flush=True)
        return 1
    print('[laser] port=%s tank=%s roles=%s' % (port, tid, roles), flush=True)

    st = json.loads(await ctl.cdp.eval(STATUS_JS))
    me = st['tanks'].get(tid, {})
    print('[laser] 激活武器=%s(%s) 队列=%s 升级=%s'
          % (me.get('weapon'), WEAPON_NAMES.get(me.get('weapon'), '?'),
             me.get('queue'), me.get('upgrades')), flush=True)

    if not args.no_promote and me.get('weapon') != 0:
        queue = me.get('queue') or []
        laser = [w for w in queue if w.get('type') == 0]
        if not laser:
            print('[laser] ❌ 队列里没有激光（先用 pickup_weapon.py --type 0 拿一把）', flush=True)
            return 1
        # 丢掉所有"排在激光后面"的武器 → 激光成为 getActiveWeapon 的末位
        behind = False
        for w in queue:
            if w.get('type') == 0:
                behind = True
                continue
            if behind:
                js = ('(() => { const gc = GameManager.getGameController() || GameManager.gameController;'
                      ' const M = gc.roundController.model;'
                      ' M.destroyWeapon({getWeaponId: function(){return "%s";},'
                      ' getPlayerId: function(){return "%s";}});'
                      ' return String(gc.getActiveWeapon("%s").getType()); })()'
                      % (w['id'], tid, tid))
                now = await ctl.cdp.eval(js)
                print('[laser] 丢掉 %s(type=%s) → 激活武器=%s'
                      % (w['id'], w.get('type'), now), flush=True)
                if str(now) == '0':
                    break
        await asyncio.sleep(0.8)

    st = json.loads(await ctl.cdp.eval(STATUS_JS))
    me = st['tanks'].get(tid, {})
    active = me.get('weapon')
    print('[laser] 最终激活武器=%s(%s) 升级=%s'
          % (active, WEAPON_NAMES.get(active, '?'), me.get('upgrades')), flush=True)
    has_aimer = bool((me.get('upgrades') or {}).get('LaserAimer'))
    print('[laser] LaserAimerUpgrade 存在：%s' % has_aimer, flush=True)

    aimers = json.loads(await ctl.cdp.eval(AIMER_JS))
    grp = aimers['groups'].get('aimerGroup') or []
    print('[laser] aimerGroup 子对象数=%d' % len(grp), flush=True)
    for c in grp:
        draw = c.pop('drawnPointsM', None)
        print('[laser]   aimer: %s' % json.dumps(c, ensure_ascii=False), flush=True)
        if draw:
            print('[laser]   画出的折线点数=%d，前 8 点(米)=%s'
                  % (len(draw), json.dumps(draw[:8], ensure_ascii=False)), flush=True)
    print(json.dumps({'ok': active == 0, 'tank': tid, 'weapon': active,
                      'laser_aimer_upgrade': has_aimer,
                      'aimer_objects': len(grp), 'port': port}, ensure_ascii=False))
    await ctl.cdp.close()
    return 0 if active == 0 else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
