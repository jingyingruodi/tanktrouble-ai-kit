#!/usr/bin/env python
"""
TankTrouble — 在线模式：控制方法与 get 方法核实
==============================================
在**自建在线房间**里逐项验证（服务端权威，客户端只是镜像）：

A. 控制方法（每个动作 ≥0.6s，因在线输入每 0.5s 才上报一次）
   forward / back / turn_left / turn_right / fire
   → 用"位移/转角/弹丸数"判断动作是否真的被服务端接受

B. get 方法（读数是否可信）
   * getTanks() vs 服务端 GameState 的 playerStates（id 集合是否一致）
   * 坦克位姿：我们封装的 tanks() vs 直接 gc.getTank(pid).getX/getY/getRotation()
   * 弹丸：M.getProjectiles() 的字段是否齐全（x/y/vx/vy/owner/timeAlive/lifetime/deadlyToOwner）
   * getActiveWeapon(pid).getType()
   * Inputs 角色绑定（inputManagers / _playerIdInputSetId）
   * 监控用快照（walls/collectibles/mines/zones/tanks/projectiles）是否在线可用

C. 挂机防护：后台保活任务（>35s 无输入就发一个 0.55s 脉冲）＋ 结束时强制退房

用法：
    python scripts/verify_online_control.py auto            # 自建房间跑全套
    python scripts/verify_online_control.py auto --json
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from cdp import CDP                      # noqa: E402
from ego_browser import resolve_port     # noqa: E402
from start_online_game import create_room_via_ui, leave_online, state  # noqa: E402
from tankctl import TankCtl              # noqa: E402

BIND_JS = r"""(() => {
  const I = window.Inputs, mc = I.__metaclass;
  const plan = BINDP;
  const done = [];
  const used = {};
  for (const k in mc._playerIdInputSetId) used[mc._playerIdInputSetId[k]] = k;
  for (const it of plan) {
    if (used[it[1]] && used[it[1]] !== it[0]) { done.push([it[0], it[1], 'skip-in-use']); continue; }
    try { I._assignInput(it[0], it[1]); used[it[1]] = it[0]; done.push([it[0], it[1], 'ok']); }
    catch (e) { done.push([it[0], it[1], 'ERR']); }
  }
  return JSON.stringify({done: done, managers: mc.inputManagers.length,
                         assigned: mc._playerIdInputSetId});
})()"""

GET_JS = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const client = ClientManager.getClient();
  const out = {};
  // 1) getTanks vs 服务端 GameState
  try { out.tanks = Object.keys(gc.getTanks() || {}); } catch (e) { out.tanksErr = String(e); }
  try { const gs = client.getGameState();
    out.serverPlayers = gs.getPlayerStates ? gs.getPlayerStates().map(p => { try { return p.getPlayerId(); } catch (e) { return '?'; } }) : null;
    out.gameMode = gs.getMode ? gs.getMode() : null;
    out.gameMax = gs.getMaxActivePlayerCount ? gs.getMaxActivePlayerCount() : null;
    out.ranked = gs.getRanked ? gs.getRanked() : null; } catch (e) { out.gsErr = String(e); }
  // 2) 我们自己的 pid
  try { out.ourIds = Users.getAllPlayerIds(); } catch (e) {}
  try { out.authIds = client.authenticatedPlayerIds; } catch (e) {}
  // 3) 直接读坦克位姿（绕过我们的封装）
  try {
    const ts = gc.getTanks(); out.tankRaw = {};
    for (const id in ts) { const t = ts[id];
      out.tankRaw[id] = {x: t.getX(), y: t.getY(), rot: t.getRotation()}; }
  } catch (e) { out.tankRawErr = String(e); }
  // 4) 弹丸字段完整性
  try {
    const M = gc.roundController.model;
    const ps = M.getProjectiles ? M.getProjectiles() : {};
    const ids = Object.keys(ps);
    out.projCount = ids.length;
    if (ids.length) { const p = ps[ids[0]];
      out.projFields = {hasX: typeof p.getX === 'function', hasV: typeof p.getSpeedX === 'function',
        hasOwner: typeof p.getPlayerId === 'function', hasTimeAlive: typeof p.getTimeAlive === 'function',
        hasLifetime: typeof p.getLifetime === 'function',
        hasDeadly: typeof p.isDeadlyToOwner === 'function' || p.deadlyToOwner !== undefined,
        type: p.getType ? p.getType() : null, owner: p.getPlayerId ? p.getPlayerId() : null,
        ta: p.getTimeAlive ? p.getTimeAlive() : null,
        lifetime: p.getLifetime ? p.getLifetime() : null};
    }
  } catch (e) { out.projErr = String(e); }
  // 5) 武器
  try { const our = (out.ourIds && out.ourIds[0]) || out.tanks[0];
    out.weapon = gc.getActiveWeapon(our).getType();
    out.weaponModel = gc.roundController.model.getActiveWeapon(our).getType(); } catch (e) { out.wErr = String(e); }
  // 6) Inputs 绑定
  try { const mc = window.Inputs.__metaclass;
    out.managers = (mc.inputManagers || []).length;
    out.assigned = JSON.parse(JSON.stringify(mc._playerIdInputSetId)); } catch (e) {}
  // 7) 监控快照可用性
  const snap = {};
  try { const M = gc.roundController.model;
    snap.maze = typeof M.getMaze === 'function';
    snap.collectibles = M.getCollectibles ? Object.keys(M.getCollectibles()).length : null;
    snap.mines = M.getTraps ? Object.keys(M.getTraps()).length : null;
    snap.zones = M.getZones ? Object.keys(M.getZones()).length : null;
    snap.round = gc.model.getState();
  } catch (e) { snap.err = String(e); }
  out.snapshot = snap;
  return JSON.stringify(out);
})()"""


class KeepAlive:
    """后台保活：>idle 秒没有输入就发一个 0.55s 脉冲（躲 60s 挂机检测）。"""

    def __init__(self, ctl, pid, idle=35.0, check=5.0):
        self.ctl = ctl
        self.pid = pid
        self.idle = idle
        self.check = check
        self.last = time.time()
        self.pulses = 0
        self.task = None
        self._stop = False

    def touch(self):
        self.last = time.time()

    async def _run(self):
        while not self._stop:
            await asyncio.sleep(self.check)
            if self._stop:
                break
            if time.time() - self.last > self.idle:
                try:
                    await self.ctl.set_input(self.pid, right=True, hold=0.55)
                    self.pulses += 1
                    self.last = time.time()
                except Exception:
                    pass

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop = True
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=3)
            except Exception:
                self.task.cancel()


async def wait_tank(ctl, pid, timeout=25.0):
    """等我们的坦克回到场上（在线房间里会被打死后重生）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        ts = await ctl.tanks()
        if pid in ts:
            return ts[pid]
        await asyncio.sleep(0.5)
    return None


async def measure(ctl, pid, action, hold=0.7, settle=0.35, retries=3):
    """执行一个控制动作并返回前后状态差异。

    * 在线房间里我们可能**被打死→重生传送**，那会把位移污染成"超速"；
      超过坦克物理上限的位移按 `suspect` 标记并重试；
    * 坦克不在场（死亡）时先等重生。
    """
    TANK_SPEED = 7.5          # 项目实测坦克前进速度 m/s
    for attempt in range(retries):
        b0 = await wait_tank(ctl, pid)
        if not b0:
            return {'ok': False, 'err': 'tank never respawned'}
        b = b0
        kw = {action: True}
        await ctl.set_input(pid, hold=hold, **kw)
        await asyncio.sleep(settle)
        t1 = await ctl.tanks()
        if pid not in t1:
            await wait_tank(ctl, pid)          # 死了 → 等重生后重试
            continue
        a = t1[pid]
        dx, dy = a['x'] - b['x'], a['y'] - b['y']
        moved = math.hypot(dx, dy)
        window = hold + settle
        if action in ('fwd', 'back') and moved > TANK_SPEED * window * 1.6:
            # 位移远超物理上限 ⇒ 期间发生了重生传送，不可作为控制证据
            continue
        return {'ok': True, 'attempt': attempt + 1,
                'before': (round(b['x'], 2), round(b['y'], 2), round(b['rot'], 3)),
                'after': (round(a['x'], 2), round(a['y'], 2), round(a['rot'], 3)),
                'moved_m': round(moved, 3), 'drot': round(a['rot'] - b['rot'], 3),
                'window_s': round(window, 2),
                'speed_mps': round(moved / window, 2) if action in ('fwd', 'back') else None}
    return {'ok': False, 'err': 'all attempts contaminated by respawn/death'}


async def main():
    ap = argparse.ArgumentParser(description='在线模式控制与 get 方法核实')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--skip-create', action='store_true', help='已在房间时跳过建房')
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)

    st = await state(cdp)
    print('[ver] 起始: state=%s gameId=%s' % (st.get('state'), st.get('gameId')), flush=True)
    entered_here = False
    try:
        if not (st.get('gameId') and st.get('tanks')):
            if args.skip_create:
                print('[ver] 不在房间且 --skip-create，退出', flush=True)
                return 1
            # ★ 先在本地创角色（同单机多人），再自建在线房间 → 房里全是我们的坦克、无人机
            print('[ver] 创角色（Menu._twoPlayer）…', flush=True)
            await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Menu'); } catch (e) {} return 'menu'; })()")
            await asyncio.sleep(4)
            print('[ver] %s' % await cdp.eval(
                "(() => { const st = GameManager.phaserInstance.state.getCurrentState();"
                " if (typeof st._twoPlayer !== 'function') return 'missing'; st._twoPlayer(); return 'ok'; })()"),
                flush=True)
            for _ in range(16):
                await asyncio.sleep(1)
                if (await state(cdp)).get('state') == 'Lobby':
                    break
            else:
                await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Lobby'); } catch (e) {} return 'l'; })()")
                await asyncio.sleep(4)
            info = await create_room_via_ui(cdp)
            if not info:
                print('[ver] ❌ 建房失败', flush=True)
                return 1
            entered_here = True
            st = await state(cdp)
            print('[ver] ✅ 已自建房间 %s 坦克=%s' % (st.get('gameId'), st.get('tanks')), flush=True)

        ctl = TankCtl(port=port)
        await ctl.connect()

        # 等 in round
        rnd = None
        for _ in range(30):
            rnd = await ctl.cdp.eval("(() => { try { return String(GameManager.getGameController().model.getState()); } catch (e) { return 'none'; } })()")
            if rnd == 'in round':
                break
            await asyncio.sleep(1)
        print('[ver] 回合状态=%s' % rnd, flush=True)

        g = json.loads(await ctl.cdp.eval(GET_JS))
        own_all = g.get('ourIds') or []
        our = [p for p in own_all if p in (g.get('tanks') or [])]
        pid = our[0] if our else (g.get('tanks') or [None])[0]
        print('[ver] 我们=%s 房间坦克=%s 服务端 players=%s'
              % (our, g.get('tanks'), g.get('serverPlayers')), flush=True)
        if not pid:
            print('[ver] ❌ 找不到自己的坦克', flush=True)
            return 1
        # 三套输入集全部用上（arrowKeys / WASDKeys / mouse），多角色才都能控
        if len(our) > 1:
            sets = ['arrowKeys', 'WASDKeys', 'mouse']
            plan = [[p, sets[i % 3]] for i, p in enumerate(our)]
            rb = json.loads(await ctl.cdp.eval(BIND_JS.replace('BINDP', json.dumps(plan))))
            print('[ver] 多角色绑定: %s' % json.dumps(rb, ensure_ascii=False), flush=True)

        ka = KeepAlive(ctl, pid)
        ka.start()

        results = {'port': port, 'gameId': st.get('gameId'), 'pid': pid, 'get': g, 'control': {}}

        # ---- B. get 方法核对 ----
        print('\n[ver] ===== B. get 方法 =====', flush=True)
        tank_ids = set(g.get('tanks') or [])
        srv_ids = set(g.get('serverPlayers') or [])
        print('[ver] tanks=%s serverPlayers=%s 一致=%s'
              % (sorted(tank_ids), sorted(srv_ids), tank_ids == srv_ids), flush=True)
        raw = (g.get('tankRaw') or {}).get(pid)
        ours = (await ctl.tanks()).get(pid)
        if raw and ours:
            # 注意：我们封装的 tanks() 会四舍五入，故用容差比较
            same = (abs(raw['x'] - ours['x']) < 0.02 and abs(raw['y'] - ours['y']) < 0.02
                    and abs(raw['rot'] - ours['rot']) < 0.01)
            print('[ver] 位姿 封装=%s 直读=%s 一致(容差0.02)=%s' % (
                (ours['x'], ours['y'], round(ours['rot'], 3)),
                (round(raw['x'], 2), round(raw['y'], 2), round(raw['rot'], 3)), same), flush=True)
            results['get']['pose_consistent'] = same
            results['get']['pose_raw'] = raw
        # 弹丸字段：先开火，趁弹丸在场时读字段
        await ctl.set_input(pid, fire=True, hold=0.8)
        await asyncio.sleep(0.4)
        pf = json.loads(await ctl.cdp.eval(
            "(() => { try { const M = GameManager.getGameController().roundController.model;"
            " const ps = M.getProjectiles(); const ids = Object.keys(ps);"
            " if (!ids.length) return JSON.stringify({count: 0});"
            " const p = ps[ids[0]]; return JSON.stringify({count: ids.length,"
            " id: ids[0], x: p.getX(), y: p.getY(),"
            " vx: p.getSpeedX ? p.getSpeedX() : null, vy: p.getSpeedY ? p.getSpeedY() : null,"
            " owner: p.getPlayerId ? p.getPlayerId() : null,"
            " ta: p.getTimeAlive ? p.getTimeAlive() : null,"
            " lifetime: p.getLifetime ? p.getLifetime() : null,"
            " maxLifetime: p.getMaxLifetime ? p.getMaxLifetime() : null,"
            " deadlyToOwner: (p.isDeadlyToOwner ? p.isDeadlyToOwner() : p.deadlyToOwner),"
            " type: p.getType ? p.getType() : null}); } catch (e) { return JSON.stringify({err: String(e)}); } })()"))
        print('[ver] 在线弹丸字段: %s' % json.dumps(pf, ensure_ascii=False), flush=True)
        results['get']['projectile_online'] = pf
        ka.touch()
        print('[ver] 武器 getActiveWeapon=%s 直接读 model=%s' % (g.get('weapon'), g.get('weaponModel')), flush=True)
        print('[ver] Inputs: managers=%s assigned=%s' % (g.get('managers'), g.get('assigned')), flush=True)
        print('[ver] 监控快照: %s' % json.dumps(g.get('snapshot'), ensure_ascii=False), flush=True)
        print('[ver] 弹丸(%d 发) 字段: %s' % (g.get('projCount', 0), json.dumps(g.get('projFields'), ensure_ascii=False)), flush=True)
        ka.touch()

        # ---- A. 控制方法 ----
        print('\n[ver] ===== A. 控制方法（每个 ≥0.7s，在线需 ≥0.5s）=====', flush=True)
        for action in ('fwd', 'back', 'left', 'right', 'fire'):
            if action == 'fire':
                c0 = g.get('projCount', 0)
                r = await measure(ctl, pid, 'fire', hold=0.8, settle=1.2)
                c1 = json.loads(await ctl.cdp.eval(
                    "(() => { try { return String(Object.keys(GameManager.getGameController()"
                    ".roundController.model.getProjectiles()).length); } catch (e) { return '-1'; } })()"))
                r['projectiles'] = (c0, int(c1) if str(c1).lstrip('-').isdigit() else None)
                r['ok'] = r.get('ok') and (r['projectiles'][1] or 0) > (r['projectiles'][0] or 0)
            else:
                r = await measure(ctl, pid, action)
                if action in ('fwd', 'back'):
                    r['ok'] = r.get('ok') and r.get('moved_m', 0) > 0.3
                else:
                    r['ok'] = r.get('ok') and abs(r.get('drot', 0)) > 0.05
            results['control'][action] = r
            print('[ver] %-6s → %s' % (action, json.dumps(r, ensure_ascii=False)), flush=True)
            ka.touch()

        ok_ctrl = sum(1 for a in results['control'].values() if a.get('ok'))
        print('\n[ver] ===== 汇总 =====', flush=True)
        print('[ver] 控制动作通过 %d/5；保活脉冲 %d 次' % (ok_ctrl, ka.pulses), flush=True)
        print('[ver] get 一致性: tanks==serverPlayers %s；位姿一致 %s'
              % (tank_ids == srv_ids, results['get'].get('pose_consistent')), flush=True)
        await ka.stop()
        results['control_ok'] = ok_ctrl
        results['keepalive_pulses'] = ka.pulses
        if args.json:
            print(json.dumps(results, ensure_ascii=False)[:2500])
        return 0
    finally:
        # 结束时保证退房（若本次是自己建的）
        try:
            st2 = await state(cdp)
            if st2.get('gameId'):
                lv = await leave_online(cdp)
                print('[ver] 退出房间: %s' % json.dumps(lv, ensure_ascii=False), flush=True)
        except Exception as e:
            print('[ver] 退房异常: %s' % e, flush=True)
        await cdp.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
