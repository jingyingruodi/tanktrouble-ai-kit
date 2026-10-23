#!/usr/bin/env python
"""
TankTrouble — 进入**在线房间**（服务端权威）
==========================================
与 `start_local_game.py` 的区别：
  * 本地局（`Constants.getMode() == 'client local'`）：**客户端自己就是权威**，
    所以"客户端碰撞世界 vs 权威仿真"这类问题在本地局里根本测不出来；
  * 在线房间：服务端权威、客户端只做镜像/插值 —— 要验证弹道预测是否与权威一致，
    **必须在在线房间里做**（用户的意见，2026-10-02）。

流程：刷新页面 → Menu →(`_onePlayer`) Lobby → `ClientManager.getClient().createGame(...)`
      → 等对局开始 → 绑定 input set（`Inputs._assignInput`）→ 校验可控

用法：
    python scripts/start_online_game.py auto                 # 自建未排位在线房间
    python scripts/start_online_game.py auto --quickjoin     # 加入公共房间
    python scripts/start_online_game.py auto --no-reload     # 不刷新（已在 Menu/Lobby 时用）
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from cdp import CDP                     # noqa: E402
from ego_browser import resolve_port    # noqa: E402

STATES_JS = """(() => {
  const g = GameManager.phaserInstance;
  const c = g && g.state ? g.state.current : null;
  const gc = GameManager.getGameController ? GameManager.getGameController() : null;
  const client = (typeof ClientManager !== 'undefined') ? ClientManager.getClient() : null;
  let tanks = [];
  try { tanks = gc ? Object.keys(gc.getTanks() || {}) : []; } catch (e) {}
  let mode = null; try { mode = String(Constants.getMode()); } catch (e) {}
  return JSON.stringify({state: c, hasController: !!gc, tanks: tanks, mode: mode,
                         gameId: client && client.getCurrentGameId ? client.getCurrentGameId() : null,
                         gameModes: (function () { try { return JSON.parse(JSON.stringify(Constants.GAME_MODES)); } catch (e) { return null; } })()});
})()"""

ADVANCE_MENU_JS = """(() => {
  const g = GameManager.phaserInstance;
  const st = g.state.getCurrentState();
  if (!st._onePlayer) return JSON.stringify({ok: false, err: 'Menu._onePlayer missing',
                                             keys: Object.getOwnPropertyNames(Object.getPrototypeOf(st)).slice(0, 40)});
  st._onePlayer();
  return JSON.stringify({ok: true, did: 'Menu -> _onePlayer (Lobby)'});
})()"""

CREATE_ONLINE_JS = """(() => {
  const g = GameManager.phaserInstance;
  const st = g.state.getCurrentState();
  if (!st || !st.createNewGame) return JSON.stringify({ok: false, err: 'Lobby.createNewGame missing',
                                                       state: g.state.current});
  let gmi = null;
  try { gmi = Constants.GAME_MODE_INFO[MODE]; } catch (e) {}
  const crates = gmi ? gmi.DEFAULT_AVAILABLE_CRATES : null;
  const maxP = (Constants.CLIENT && Constants.CLIENT.MAX_PLAYERS) || 3;
  st.createNewGame(RANKED, false, false, MODE, maxP, crates);   // ← 会先 setMode(MODE_CLIENT_ONLINE)
  return JSON.stringify({ok: true, did: 'Lobby.createNewGame', mode: MODE, ranked: RANKED,
                         max: maxP, crates: crates,
                         availableOnline: gmi ? gmi.AVAILABLE_ONLINE : null,
                         minPlayers: gmi ? gmi.MIN_PLAYERS : null,
                         activeHours: gmi ? gmi.ACTIVE_HOURS : null,
                         utcHour: new Date().getUTCHours()});
})()"""

JOIN_RANDOM_JS = """(() => {
  const st = GameManager.phaserInstance.state.getCurrentState();
  if (!st || !st._joinRandomGame) return JSON.stringify({ok: false, err: '_joinRandomGame missing'});
  st._joinRandomGame();
  return JSON.stringify({ok: true, did: 'Lobby._joinRandomGame (quickjoin)'});
})()"""

BIND_JS = """(() => {
  const I = window.Inputs, mc = I.__metaclass;
  const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({ok: false, err: 'no controller'});
  const pids = Object.keys(gc.getTanks() || {});
  if (!pids.length) return JSON.stringify({ok: false, err: 'no tanks'});
  const plan = BINDPLAN;
  const used = {};
  for (const k in mc._playerIdInputSetId) used[mc._playerIdInputSetId[k]] = k;
  const done = [];
  for (const item of plan) {
    const pid = item[0], setId = item[1];
    if (mc._playerIdInputSetId[pid] === setId) { done.push({pid: pid, set: setId, action: 'already'}); continue; }
    if (used[setId] && used[setId] !== pid) { done.push({pid: pid, set: setId, action: 'skip-in-use'}); continue; }
    I._assignInput(pid, setId);
    used[setId] = pid;
    done.push({pid: pid, set: setId, action: 'assigned'});
  }
  return JSON.stringify({ok: true, done: done, managers: mc.inputManagers.length,
                         assigned: JSON.parse(JSON.stringify(mc._playerIdInputSetId))});
})()"""


async def state(cdp):
    return json.loads(await cdp.eval(STATES_JS))


async def create_room_via_ui(cdp, timeout=40.0):
    """用**游戏自己的建房面板**创建在线房间（真人路径，免费可用）。

    为什么必须走 UI 而不是直接调 `createNewGame`：
      `NewGameOverlay` 里 Game mode / Max tanks / Crate 选择都在 `<div class='premium'>` 区块内，
      非付费用户这些控件是 disabled 的；**免费建房必须用默认值**
      （`selectedMode = Constants.GAME_MODES.CURRENT = -1`、`selectedMax = 6`、
      `crates = [0..6]`）。我们直接手填"显式模式/人数/箱子"会被服务端以
      `CreateGameResultMessage{message:"No premium users"}` 拒绝（实测，见 docs/10 §14）。
    推送面板 + 点 `startButton` 等于让游戏自己按默认值发请求，稳。
    """
    push = """(() => {
      try { OverlayManager.pushOverlay(TankTrouble.NewGameOverlay, {}); }
      catch (e) { return JSON.stringify({ok: false, err: String(e)}); }
      return JSON.stringify({ok: true});
    })()"""
    read = """(() => { const O = TankTrouble.NewGameOverlay;
      let crates = null; try { crates = O._getSelectedCrates(); } catch (e) {}
      let mode = null; try { mode = O._getSelectedMode(); } catch (e) {}
      let premium = null; try { premium = PremiumManager.isPremiumPresent(); } catch (e) {}
      return JSON.stringify({selectedMode: O.selectedMode, resolvedMode: mode,
                             selectedMax: O.selectedMaxNumberOfPlayers, crates: crates,
                             premiumPresent: premium}); })()"""
    click = """(() => { const O = TankTrouble.NewGameOverlay;
      if (!O.startButton) return JSON.stringify({ok: false, err: 'no startButton'});
      O.startButton.click();
      return JSON.stringify({ok: true, did: 'startButton.click()'}); })()"""

    r = json.loads(await cdp.eval(push))
    print('[create] 推送建房面板: %s' % json.dumps(r, ensure_ascii=False), flush=True)
    if not r.get('ok'):
        return None
    await asyncio.sleep(1.5)
    print('[create] 面板默认设置: %s' % await cdp.eval(read), flush=True)
    print('[create] 点击 Start: %s' % await cdp.eval(click), flush=True)

    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        st = await state(cdp)
        if st.get('gameId') or st.get('tanks'):
            return st
        await asyncio.sleep(1)
    return None


async def keepalive(ctl, pid, interval=20.0, duration=None):
    """在线模式**防挂机保活**：周期性发一个极短的方向脉冲。

    背景（用户提醒 + `Constants.SERVER` 实测）：
      * `MAX_INACTIVITY_TIME = 60000` —— 60s 不活动会被踢、可能顺带登出；
      * 追踪校验 `TRACKING_*`/`TANKSTATE_VIOLATION_REASONS`
        （EMISSION_TOO_OFTEN / MOVED_TOO_FAST / TURNED_TOO_FAST）也会累计违规。
    所以：**在线房间里不要长时间只读不操作**；需要长读时用本协程保活。
    注：输入脉冲要 ≥0.5s 才能被服务器看到（`TANKSTATE_EMISSION_INTERVAL=0.5`），
    但保活只需要"有操作"，用 0.55s 的极短转向即可（不改变航向由调用方决定）。
    """
    import time as _t
    t0 = _t.time()
    while duration is None or (_t.time() - t0) < duration:
        try:
            await ctl.set_input(pid, right=True, hold=0.55)
        except Exception:
            pass
        await asyncio.sleep(max(5.0, interval))


async def leave_online(cdp, timeout=25.0, force=True):
    """离开在线房间。

    实测：`leaveGame(false)` 是**优雅退出**（打完本回合才走，可能几十秒都不生效），
    `leaveGame(true)` 是**立即退出**（2 秒内回 Lobby）→ 默认用 force=True。
    """
    js = """(() => {
      const c = ClientManager.getClient();
      if (!c) return JSON.stringify({ok: false, err: 'no client'});
      const gid = c.getCurrentGameId ? c.getCurrentGameId() : null;
      if (!gid) return JSON.stringify({ok: true, did: 'not in online game', gameId: null});
      c.leaveGame(%s);
      return JSON.stringify({ok: true, did: 'leaveGame(%s)', gameId: gid});
    })()""" % ('true' if force else 'false', 'true' if force else 'false')
    try:
        r = json.loads(await cdp.eval(js))
    except Exception as e:
        return {'ok': False, 'err': str(e)}
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        st = await state(cdp)
        if not st['gameId'] and not st['tanks']:
            return {'ok': True, 'left': True, 'gameId': r.get('gameId')}
        await asyncio.sleep(0.5)
    st = await state(cdp)
    return {'ok': False, 'left': False, 'gameId': st['gameId']}


async def main():
    ap = argparse.ArgumentParser(description='进入在线房间')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--quickjoin', action='store_true', help='加入公共房间而不是自建')
    ap.add_argument('--create', action='store_true',
                    help='用游戏自己的建房面板创建在线房间（免费路径：模式用 CURRENT 默认值）')
    ap.add_argument('--ranked', action='store_true', help='自建时是否排位（默认否，避免影响战绩）')
    ap.add_argument('--players', type=int, default=4, help='房间最大玩家数')
    ap.add_argument('--mode', type=int, default=2,
                    help='游戏模式（0=CLASSIC 1=BOOT_CAMP 2=DEATHMATCH 3=TEAM_CLASSIC 4=TEAM_DEATHMATCH）')
    ap.add_argument('--no-reload', action='store_true')
    ap.add_argument('--hold', type=float, default=0.0,
                    help='进房后驻留秒数（期间自动保活防挂机），到时自动退出房间')
    ap.add_argument('--keepalive', action='store_true',
                    help='驻留期间保活（--hold 时默认开启）')
    ap.add_argument('--no-leave', action='store_true', help='结束时不要自动退出房间')
    ap.add_argument('--timeout', type=float, default=90.0)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)

    st = await state(cdp)
    print('[online] 起始: state=%s mode=%s gameId=%s tanks=%s'
          % (st['state'], st['mode'], st['gameId'], st['tanks']), flush=True)
    if st.get('gameModes'):
        print('[online] Constants.GAME_MODES = %s' % json.dumps(st['gameModes'], ensure_ascii=False), flush=True)

    # 1) 回到 Menu
    if not args.no_reload and (st['state'] in ('Game', 'Lobby') or st['tanks']):
        print('[online] 刷新页面回到 Menu（会丢失当前本地局）…', flush=True)
        try:
            await cdp.eval('location.reload()')
        except Exception:
            pass
        await asyncio.sleep(6)
        await cdp.close()
        await asyncio.sleep(1)
        cdp = CDP(port=port)
        await cdp.connect_page('tanktrouble.com', timeout=20)

    # 2) 等启动完成
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < args.timeout:
        st = await state(cdp)
        if st['state']:
            break
        await asyncio.sleep(1)
    print('[online] 启动后: state=%s' % st['state'], flush=True)

    # 3) Menu -> Lobby
    if st['state'] == 'Menu':
        r = json.loads(await cdp.eval(ADVANCE_MENU_JS))
        print('[online] %s' % r, flush=True)
        await asyncio.sleep(2.0)
        st = await state(cdp)
    print('[online] 现在: state=%s' % st['state'], flush=True)

    # 3.5) ★ 先创角色（和单机多人一模一样）：Menu._twoPlayer()/_threePlayer()
    #      实测：这样会在这条连接上多注册若干个访客角色并完成认证，
    #      随后自建的在线房间里就是**我们自己的多辆坦克、没有人机对手**（理想受控沙箱）。
    if args.players > 1:
        await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Menu'); } catch (e) {} return 'menu'; })()")
        await asyncio.sleep(4)
        fn = '_twoPlayer' if args.players == 2 else '_threePlayer'
        r = await cdp.eval(
            "(() => { const st = GameManager.phaserInstance.state.getCurrentState();"
            " if (typeof st.%s !== 'function') return 'missing'; st.%s(); return 'ok'; })()" % (fn, fn))
        print('[online] 创角色 %s → %s' % (fn, r), flush=True)
        # 创角色后通常会进 Lobby；有时会停在 Menu（菜单资源未就绪）→ 轮询 + 兜底直接启 Lobby
        for _ in range(16):
            await asyncio.sleep(1)
            if (await state(cdp)).get('state') == 'Lobby':
                break
        else:
            print('[online] 仍在 Menu → 直接启动 Lobby 状态', flush=True)
            await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Lobby'); }"
                           " catch (e) {} return 'lobby'; })()")
            await asyncio.sleep(4)
        st2 = await state(cdp)
        uu = await cdp.eval("(() => { try { return JSON.stringify(Users.getAllPlayerIds()); } catch (e) { return '[]'; } })()")
        print('[online] 角色数=%s 状态=%s' % (uu, st2.get('state')), flush=True)

    # 4) 建/加入在线房间
    if args.create:
        # ★ 自建房间要保证"房里全是我们的角色"（单人建房时服务端会塞一个人机）。
        #   若发现非我们的坦克 → 退房重建（最多 3 次），必要时再补创角色。
        info = None
        for attempt in range(3):
            cur = await state(cdp)
            if cur.get('gameId') or cur.get('tanks'):
                print('[online] 当前已在房间 %s → 先退出' % cur.get('gameId'), flush=True)
                await leave_online(cdp)
            n_own = len(json.loads(await cdp.eval(
                "(() => { try { return JSON.stringify(Users.getAllPlayerIds()); } catch (e) { return '[]'; } })()")))
            if n_own < 2:
                print('[online] 角色数=%d < 2 → 再创角色（Menu._twoPlayer）' % n_own, flush=True)
                await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Menu'); } catch (e) {} return 'm'; })()")
                await asyncio.sleep(4)
                await cdp.eval("(() => { const s = GameManager.phaserInstance.state.getCurrentState();"
                               " if (s._twoPlayer) s._twoPlayer(); return 'ok'; })()")
                for _ in range(16):
                    await asyncio.sleep(1)
                    if (await state(cdp)).get('state') == 'Lobby':
                        break
                else:
                    await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Lobby'); } catch (e) {} return 'l'; })()")
                    await asyncio.sleep(4)
            info = await create_room_via_ui(cdp)
            if not info:
                print('[online] ❌ 建房失败（第 %d 次）' % (attempt + 1), flush=True)
                continue
            own_ids = json.loads(await cdp.eval(
                "(() => { try { return JSON.stringify(Users.getAllPlayerIds()); } catch (e) { return '[]'; } })()"))
            tanks = sorted(info.get('tanks') or [])
            others = [p for p in tanks if p not in own_ids]
            print('[online] 第 %d 次建房 %s 坦克=%s 我们的=%s 非我们=%s'
                  % (attempt + 1, info.get('gameId'), tanks,
                     [p for p in tanks if p in own_ids], others), flush=True)
            if not others:
                print('[online] ✅ 房间里全是我们的角色（无人机对手）', flush=True)
                break
            print('[online] ⚠️ 混入了非我们的坦克（人机/他人）→ 退房重建', flush=True)
        if not info:
            await cdp.close()
            return 1
    elif args.quickjoin:
        js = JOIN_RANDOM_JS
    else:
        js = (CREATE_ONLINE_JS.replace('MODE', str(args.mode))
              .replace('RANKED', 'true' if args.ranked else 'false'))
    if not args.create:
        r = json.loads(await cdp.eval(js))
        print('[online] %s' % json.dumps(r, ensure_ascii=False), flush=True)

    # 5) 等对局开始（服务端往返）；20s 没动静就退化为"加入公共房间"
    async def wait_game(timeout):
        t0_ = asyncio.get_event_loop().time()
        last = None
        while asyncio.get_event_loop().time() - t0_ < timeout:
            last = await state(cdp)
            if last['tanks']:
                return last
            await asyncio.sleep(1)
        return None
    info = await wait_game(min(25.0, args.timeout))
    if not info and not args.quickjoin:
        print('[online] 自建房间 25s 无响应 → 改尝试加入公共房间', flush=True)
        r2 = json.loads(await cdp.eval(JOIN_RANDOM_JS))
        print('[online] %s' % json.dumps(r2, ensure_ascii=False), flush=True)
        info = await wait_game(args.timeout)
    if not info:
        st = await state(cdp)
        print('[online] ❌ 超时：没等到对局开始（最后 state=%s gameId=%s mode=%s）'
              % (st['state'], st['gameId'], st['mode']), flush=True)
        await cdp.close()
        return 1
    print('[online] ✅ 对局开始: state=%s mode=%s gameId=%s tanks=%s'
          % (info['state'], info['mode'], info['gameId'], info['tanks']), flush=True)

    # 6) 绑定 input set —— ★ 只绑【我们自己认证的 playerId】
    #    单人自建房间时服务端会**配一个人机**（实测：房间里 2 辆坦克 = 我们 + bot），
    #    把按键绑到 bot 的坦克上会"看似可控但位移 0"。
    ours = await cdp.eval("(() => { try { return JSON.stringify(Users.getAllPlayerIds()); }"
                          " catch (e) { return '[]'; } })()")
    try:
        own_ids = json.loads(ours)
    except Exception:
        own_ids = []
    tanks_here = [p for p in sorted(info['tanks']) if p in own_ids]
    others = [p for p in sorted(info['tanks']) if p not in own_ids]
    if not tanks_here:
        tanks_here = sorted(info['tanks'])[:1]
    print('[online] 房间坦克: %s → 我们=%s 其它(人机/他人)=%s'
          % (sorted(info['tanks']), tanks_here, others), flush=True)
    pids = tanks_here
    _sets = ['arrowKeys', 'WASDKeys', 'mouse']
    plan = [[p, _sets[i % 3]] for i, p in enumerate(pids)]
    r = json.loads(await cdp.eval(BIND_JS.replace('BINDPLAN', json.dumps(plan))))
    print('[online] 绑定: %s' % json.dumps(r, ensure_ascii=False), flush=True)

    # 7) 校验可控：等到回合真正开始（新房间会有倒计时，倒计时期间输入被忽略）
    from tankctl import TankCtl
    ctl = TankCtl(port=port)
    await ctl.connect()
    rnd = None
    for _ in range(30):
        rnd = await ctl.cdp.eval(
            "(() => { try { return String(GameManager.getGameController().model.getState()); }"
            " catch (e) { return 'none'; } })()")
        if rnd == 'in round':
            break
        await asyncio.sleep(1)
    print('[online] 回合状态=%s' % rnd, flush=True)
    # ★ 健壮性：等坦克真正进坦克表（新服务端下房间刚建好时可能短暂缺席，
    #   直接取 [pids[0]] 会 KeyError: '22838087'）
    before = None
    for _ in range(20):
        t = await ctl.tanks()
        if pids[0] in t:
            before = t[pids[0]]
            break
        await asyncio.sleep(0.5)
    if before is None:
        print('[online] ⚠️ 坦克 %s 迟迟未上场，跳过控制校验' % pids[0], flush=True)
        out = {'ok': False, 'port': port, 'state': info['state'], 'mode': info['mode'],
               'gameId': info.get('gameId'), 'pids': pids, 'moved_m': 0.0, 'err': 'tank not spawned'}
        print(json.dumps(out, ensure_ascii=False), flush=True)
        await cdp.close()
        return 1
    await ctl.set_input(pids[0], fwd=True, hold=0.8)
    await asyncio.sleep(0.5)
    after = (await ctl.tanks()).get(pids[0]) or before
    moved = ((after['x'] - before['x']) ** 2 + (after['y'] - before['y']) ** 2) ** 0.5
    print('[online] 控制校验: (%.1f,%.1f) → (%.1f,%.1f)  位移 %.2fm'
          % (before['x'], before['y'], after['x'], after['y'], moved), flush=True)
    out = {'ok': moved > 0.5, 'port': port, 'state': info['state'], 'mode': info['mode'],
           'gameId': info['gameId'], 'pids': pids, 'moved_m': round(moved, 2),
           'bind': r}
    print(json.dumps(out, ensure_ascii=False))

    # 8) ⚠️ 在线模式有挂机检测（MAX_INACTIVITY_TIME=60s）→ 需要驻留时保活
    if args.hold > 0:
        print('[online] ⚠️ 在线模式挂机检测：60s 无操作会被踢；驻留 %.0fs 期间自动保活…'
              % args.hold, flush=True)
        await keepalive(ctl, pids[0], interval=20.0, duration=args.hold)
        print('[online] 驻留结束', flush=True)
    if not args.no_leave:
        lv = await leave_online(cdp)
        print('[online] 退出房间: %s' % json.dumps(lv, ensure_ascii=False), flush=True)
        out['left'] = lv.get('left')
    await ctl.cdp.close()
    return 0 if moved > 0.5 else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
