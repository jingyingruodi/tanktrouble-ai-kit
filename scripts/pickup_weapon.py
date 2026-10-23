#!/usr/bin/env python
"""
TankTrouble — 拾取指定类型的武器箱（地雷/激光/…）
=================================================
`pickup_mine.py` 的泛化版：把"找箱子 → 游戏自带寻路 → 导航 → 拾取 → 校验"
做成对**任意道具类型**通用，并加上**换图刷箱**循环（同一回合箱子满了就不再刷新，
所以需要 endRound 重开一局来重新随机道具）。

道具类型（`docs/05` 的道具表 + 实测）：
    0=激光 1=双管 2=霰弹 3=导弹 4=地雷 5=加特林 6=护盾

用法：
    python scripts/pickup_weapon.py 9222 --type 0            # 刷出并拾取激光
    python scripts/pickup_weapon.py 9222 --type 0 --spawn    # 用游戏自己的 spawnCrate 造箱子再拾取
    python scripts/pickup_weapon.py 9222 --type 0 --reroll 40
    python scripts/pickup_weapon.py 9222 --type 4 --no-reroll # 只拾取当前已有的地雷箱

两条获取路子：
  * 自然刷新：同一回合最多 3 只箱子、满了不再刷，且 `UIConstants.CRATE_SPAWN_TIME`
    决定刷新节奏（实测换图后不是立刻有箱子）→ 只能慢慢等，故 `--reroll` 只用于
    "换图碰运气"，命中率 = 1 - (1-1/9)^3 ≈ 30%/局（道具共 9 种）。
  * `--spawn`：调用游戏自己的 `roundController.spawnCrate(type, position)`
    （`pickUpCrate` 源码里 CRATE_LASER 会同时授予 `LaserWeapon` +
    `LaserAimerUpgrade` 瞄准线升级）——用于**研究**目的，箱子造出来后仍要
    正常开车过去拾取，走的是真实拾取流程。
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

from tankctl import TankCtl                     # noqa: E402
from ego_browser import resolve_port            # noqa: E402
from tank_behavior_template import TankBehavior  # noqa: E402

WEAPON_NAMES = {0: 'Laser', 1: 'DoubleBarrel', 2: 'Shotgun', 3: 'HomingMissile',
                4: 'Mine', 5: 'GatlingGun', 6: 'Shield'}

COLLECTIBLES_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const rm = gc.getRoundModel ? gc.getRoundModel() : null;
  const cs = (rm && rm.getCollectibles) ? rm.getCollectibles()
           : (gc.getCollectibles ? gc.getCollectibles() : {});
  const out = [];
  for (const cid in cs) {
    const c = cs[cid];
    out.push({id: cid, type: (c.getType ? c.getType() : -1),
              x: c.getX(), y: c.getY()});
  }
  return JSON.stringify(out);
})()"""

STATE_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  let round = null; try { round = gc.model.getState(); } catch (e) {}
  let rid = null; try { rid = gc.getRoundId ? gc.getRoundId() : null; } catch (e) {}
  return JSON.stringify({round: round, rid: rid});
})()"""


async def read_crates(ctl):
    raw = await ctl.cdp.eval(COLLECTIBLES_JS)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


async def read_round(ctl):
    raw = await ctl.cdp.eval(STATE_JS)
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


async def wait_new_round(ctl, old_rid, timeout=40.0):
    """等新一轮真正开始（roundId 变化 + 处于 in round）。"""
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        st = await read_round(ctl)
        if st.get('rid') and st.get('rid') != old_rid and st.get('round') == 'in round':
            return st
        await asyncio.sleep(0.5)
    return None


SPAWN_JS = """(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const rc = gc.roundController;
  const M = rc.model;
  const me = gc.getTanks()["__PID__"];
  // ① 优先用游戏自己的"随机未占用位置"（spawnGold/spawnDiamond 也用它）
  let pos = null;
  try { pos = M.getMaze().getRandomUnusedPosition(rc.getRoundState(true), 3); } catch (e) {}
  // ② 兜底：自己扫地盘，找离坦克 2~4 格、可通行且没箱子/坦克的格子中心（米）
  if (!pos) {
    const tiles = M.getMaze().getTiles();
    const w = M.getMaze().getWidth(), h = M.getMaze().getHeight();
    const occ = {};
    const cs = M.getCollectibles() || {};
    for (const k in cs) { occ[Math.floor(cs[k].getX()/10)+','+Math.floor(cs[k].getY()/10)] = 1; }
    const ts = gc.getTanks() || {};
    for (const k in ts) { occ[Math.floor(ts[k].getX()/10)+','+Math.floor(ts[k].getY()/10)] = 1; }
    const mx = Math.floor(me.getX()/10), my = Math.floor(me.getY()/10);
    let best = null, bestD = 1e9;
    for (let x = 0; x < w; x++) for (let y = 0; y < h; y++) {
      if (!tiles[x] || !tiles[x][y] || !tiles[x][y][0]) continue;      // 不可通行
      if (occ[x+','+y]) continue;
      const d = Math.max(Math.abs(x-mx), Math.abs(y-my));
      if (d < 2 || d > 4) continue;
      if (d < bestD) { bestD = d; best = {x: (x+0.5)*10, y: (y+0.5)*10, rotation: 0}; }
    }
    pos = best;
  }
  if (!pos) return JSON.stringify({ok: false, err: 'no free position'});
  if (pos.rotation === undefined) pos.rotation = 0;
  rc.spawnCrate(__TYPE__, {x: pos.x, y: pos.y, rotation: pos.rotation});
  return JSON.stringify({ok: true, x: pos.x, y: pos.y});
})()"""


async def wait_active_round(ctl, timeout=30.0):
    """等回合真正可操作（model 有 b2dworld 且处于 in round）。

    `spawnCrate` 在回合切换期间会因 `this.b2dworld` 为 null 而抛
    `Cannot read properties of null (reading 'CreateBody')` —— 先等它就绪。
    """
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        raw = await ctl.cdp.eval(
            '(() => { try { const gc = GameManager.getGameController()'
            ' || GameManager.gameController; const M = gc.roundController.model;'
            ' return JSON.stringify({round: gc.model.getState(),'
            ' world: !!M.b2dworld}); } catch (e) { return JSON.stringify({err: String(e)}); } })()')
        try:
            st = json.loads(raw) if raw else {}
        except Exception:
            st = {}
        if st.get('round') == 'in round' and st.get('world'):
            return True
        await asyncio.sleep(0.5)
    return False


async def spawn_crate(ctl, want, tank_id):
    """用游戏自己的 roundController.spawnCrate 造一个指定类型的箱子。"""
    await wait_active_round(ctl)
    js = SPAWN_JS.replace('__PID__', str(tank_id)).replace('__TYPE__', str(want))
    raw = await ctl.cdp.eval(js)
    try:
        return json.loads(raw) if raw else {'ok': False}
    except Exception:
        return {'ok': False, 'raw': str(raw)}


async def find_crate(ctl, want, reroll, tank_id):
    """返回目标类型箱子 {id,x,y}；必要时换图重开。"""
    for attempt in range(reroll + 1):
        crates = await read_crates(ctl)
        hit = [c for c in crates if int(c.get('type', -1)) == want]
        types = sorted(int(c.get('type', -1)) for c in crates)
        print('[find] 第%d次检查：场上道具类型=%s %s'
              % (attempt + 1, types, '← 命中!' if hit else ''), flush=True)
        if hit:
            hit.sort(key=lambda c: math.hypot(c['x'], c['y']))
            return hit[0]
        if attempt >= reroll:
            break
        st = await read_round(ctl)
        old_rid = st.get('rid')
        print('[find] 无 %s(%d) → 换图重开（endRound）' % (WEAPON_NAMES.get(want, '?'), want),
              flush=True)
        try:
            await ctl.cdp.eval('(GameManager.getGameController()).endRound()')
        except Exception as e:
            print('[find] endRound 失败: %s' % e, flush=True)
            return None
        nst = await wait_new_round(ctl, old_rid, timeout=40)
        if not nst:
            print('[find] 等待新回合超时', flush=True)
            return None
    return None


async def main():
    ap = argparse.ArgumentParser(description='拾取指定类型的武器箱')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--type', type=int, default=0, help='道具类型（0=激光）')
    ap.add_argument('--reroll', type=int, default=30, help='最多换图次数')
    ap.add_argument('--no-reroll', action='store_true', help='不换图，只用当前回合的箱子')
    ap.add_argument('--spawn', action='store_true',
                    help='用游戏自己的 spawnCrate 造出目标箱子再拾取（研究用）')
    ap.add_argument('--tank', default=None, help='指定坦克 id（默认取方向键角色）')
    ap.add_argument('--clear-others', action='store_true',
                    help='★2026-10-24：拾取前销毁其它箱子（只留目标箱，保证拿到指定武器）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()

    tank_id = args.tank
    if not tank_id:
        roles = await ctl.detect_roles()
        print('[pickup] roles=%s' % roles, flush=True)
        # 优先鼠标/键盘任选一个可控角色；否则取第一辆
        tank_id = next((p for p, r in roles.items() if r == 'arrows'), None) \
            or next(iter(roles), None)
    ts = await ctl.tanks()
    if not tank_id or tank_id not in ts:
        tank_id = list(ts.keys())[0] if ts else None
    if not tank_id:
        print('[pickup] 没有坦克（未进对局？）', flush=True)
        return 1
    print('[pickup] 使用坦克 %s' % tank_id, flush=True)

    reroll = 0 if args.no_reroll else args.reroll
    if args.spawn:
        pos = await spawn_crate(ctl, args.type, tank_id)
        print('[pickup] spawnCrate(%d, %s) → %s' % (args.type, pos, pos), flush=True)
        await asyncio.sleep(0.6)
    cr = await find_crate(ctl, args.type, reroll, tank_id)
    if not cr:
        print('[pickup] ❌ 刷不到 %s(type=%d) 箱子' % (WEAPON_NAMES.get(args.type, '?'), args.type),
              flush=True)
        return 1
    print('[pickup] 目标箱子 %s type=%d @(%.1f,%.1f)m'
          % (cr['id'], args.type, cr['x'], cr['y']), flush=True)

    if args.clear_others:
        # 销毁除目标箱以外的所有箱子（避免路上顺手捡到别的武器）
        js = ("""(() => { const M = GameManager.getGameController().roundController.model;
          const keep = %s; const destroyed = [];
          for (const id of Object.keys(M.getCollectibles())) {
            if (id === keep) continue;
            try { M.destroyCollectible({getCollectibleId: function () { return id; }}, true);
                  destroyed.push(id); } catch (e) {} }
          return JSON.stringify(destroyed); })()""" % json.dumps(cr['id']))
        gone = await ctl.cdp.eval(js)
        print('[pickup] 清理其它箱子: %s' % gone, flush=True)
        await asyncio.sleep(0.4)

    # 导航：用 TankBehavior.navigate_to（逐航点 replan + 撞墙滑行；比
    # pickup_mine 那套"只转向+前进"的简易导航更抗迷宫）
    bhv = TankBehavior(port=port)
    await bhv.connect()
    print('[pickup] 导航到箱子 (%.1f,%.1f)…' % (cr['x'], cr['y']), flush=True)
    nav_ok = await bhv.navigate_to(tank_id, (cr['x'], cr['y']),
                                   arrival=1.2, waypoint_arrival=2.0)
    print('[pickup] navigate_to → %s；检查武器…' % nav_ok, flush=True)

    # 校验武器类型
    def weapon_js():
        return (f'(GameManager.getGameController()).getActiveWeapon("{tank_id}").getType()')

    wp_now = await ctl.cdp.eval(weapon_js())
    for _ in range(6):
        if str(wp_now) == str(args.type):
            break
        await ctl.forward(0.4, tank_id)
        await asyncio.sleep(0.2)
        wp_now = await ctl.cdp.eval(weapon_js())
    ok = str(wp_now) == str(args.type)
    print('[pickup] 当前武器 type=%s（目标 %d）→ %s'
          % (wp_now, args.type, '✅ 拾取成功' if ok else '❌ 未拾取'), flush=True)
    print(json.dumps({'ok': ok, 'tank': tank_id, 'weapon_type': wp_now,
                      'crate': cr, 'port': port}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
