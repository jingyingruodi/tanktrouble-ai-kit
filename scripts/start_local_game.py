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
自动开 Local 局 + 角色(input set)绑定
====================================
把"人手点两下菜单"变成一条命令，并解决程序化开局的**致命坑**：

  坑：用游戏菜单处理器程序化开 Local 局后，`window.Inputs.__metaclass.inputManagers`
      是**空数组** —— 因为 input set（WASDKeys/arrowKeys/mouse）没有分配给任何
      playerId。此时 `get_roles()` 返回 {}，`pick_role()` 拿不到坦克，
      `setInputState` 不能确认玩家身份 → 所有控制脚本"没有可控坦克"。

  解法：调用游戏自己的分配 API `Inputs._assignInput(playerId, inputSetId)`
      （与 UI 选择控制方式走同一函数），把 input set 显式绑定到本次对局的
      playerId 上。绑定后静态角色识别立即生效，setInputState 链路可用。

流程：Menu →(菜单处理器) Lobby →(localGameButton 处理器) 对局 → 绑定 input set → 校验

用法：
    python scripts/start_local_game.py                       # ego 浏览器 + 2 人局
    python scripts/start_local_game.py --players 3           # 3 人局（WASD/方向键/鼠标）
    python scripts/start_local_game.py --port 9222           # 指定端口
    python scripts/start_local_game.py --restart             # 先刷新页面再开局（干净复现）
    python scripts/start_local_game.py --json                # 只输出机器可读结果

作为库使用：
    from start_local_game import ensure_local_game
    info = await ensure_local_game('auto', players=2)   # {'port','pids','roles',...}
"""
import argparse
import asyncio
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'cdp_tools'))

from cdp import CDP                                    # noqa: E402
from ego_browser import resolve_port, describe, discover  # noqa: E402

# 默认绑定顺序：先方向键、后 WASD、最后鼠标——与火控验证脚本的
# "方向键角色 vs 英文键角色"约定一致。
DEFAULT_BIND = ['arrowKeys', 'WASDKeys', 'mouse']
MENU_HANDLER = {1: '_onePlayer', 2: '_twoPlayer', 3: '_threePlayer'}

PROBE_JS = r"""
(() => {
  const out = {phaser: null, gc: false, model: null, tanks: [], roles: {},
               assigned: {}, managers: 0, inputSets: null, ready: false};
  try { out.phaser = GameManager.phaserInstance ? GameManager.phaserInstance.state.current : null; }
  catch (e) { out.phaserErr = String(e); }
  try {
    const gc = GameManager.getGameController();
    out.gc = !!gc;
    if (gc) {
      try { out.model = gc.model.getState(); } catch (e) {}
      out.tanks = Object.keys(gc.getTanks() || {});
      out.ready = (out.tanks.length > 0);
    }
  } catch (e) {}
  try {
    const mc = window.Inputs.__metaclass;
    out.inputSets = window.Inputs.getAllInputSetIds ? window.Inputs.getAllInputSetIds() : null;
    out.assigned = JSON.parse(JSON.stringify(mc._playerIdInputSetId || {}));
    const ims = mc.inputManagers || [];
    out.managers = ims.length;
    for (let i = 0; i < ims.length; i++) {
      const m = ims[i];
      if (!m.playerId) continue;
      const isK = m.__class === window.KeyboardInputManager;
      const isM = m.__class === window.MouseInputManager;
      out.roles[m.playerId] = isK ? ((m.forwardKey === 38) ? 'arrows' : 'keyboard')
                                  : (isM ? 'mouse' : '?');
    }
  } catch (e) { out.inputErr = String(e); }
  return out;
})()
"""

ADVANCE_JS = r"""
(() => {
  const gm = GameManager;
  const cur = gm.phaserInstance && gm.phaserInstance.state ? gm.phaserInstance.state.current : null;
  const gc = gm.getGameController ? gm.getGameController() : null;
  if (gc) return {ok: true, did: 'already in game'};
  if (!cur) return {ok: false, err: 'game not booted yet'};
  const st = gm.phaserInstance.state.getCurrentState();
  if (cur === 'Menu') {
    const fn = %s;
    if (!st[fn]) return {ok: false, err: 'menu handler missing: ' + fn};
    st[fn]();
    return {ok: true, did: 'Menu -> ' + fn};
  }
  if (cur === 'Lobby') {
    if (!st._createLocalGame) return {ok: false, err: 'Lobby._createLocalGame missing'};
    st._createLocalGame();
    return {ok: true, did: 'Lobby -> _createLocalGame'};
  }
  return {ok: true, did: 'waiting in ' + cur};
})()
"""

BIND_JS = r"""
(() => {
  const I = window.Inputs, mc = I.__metaclass;
  const gc = GameManager.getGameController();
  const pids = Object.keys(gc.getTanks() || {});
  const plan = %s;                       // [[pid, inputSetId], ...] 由 Python 决定
  const used = {};
  for (const k in mc._playerIdInputSetId) { used[mc._playerIdInputSetId[k]] = k; }
  const done = [];
  for (const item of plan) {
    const pid = item[0], setId = item[1];
    if (mc._playerIdInputSetId[pid] === setId) { done.push({pid: pid, set: setId, action: 'already'}); continue; }
    if (used[setId] && used[setId] !== pid) { done.push({pid: pid, set: setId, action: 'skipped-in-use-by-' + used[setId]}); continue; }
    I._assignInput(pid, setId);
    used[setId] = pid;
    done.push({pid: pid, set: setId, action: 'assigned'});
  }
  return {done: done, managers: mc.inputManagers.length,
          assigned: JSON.parse(JSON.stringify(mc._playerIdInputSetId))};
})()
"""


HIDE_OVERLAY_JS = r"""
(() => {
  // 本地局的 "Select your controls" 是网站 DOM 遮罩（#overlay，背景 rgba(0,0,0,0.8)）。
  // 自动开局走 API 绑定 input set，绕过了网站 UI 流程，遮罩不会被自动收起，会盖住游戏画面。
  // 这里去掉遮罩的灰暗背景并隐藏按键设置表单；不影响 canvas 内的真实游戏渲染。
  const out = {overlay: false, controls: 0, dimBefore: null, dimAfter: null};
  const o = document.getElementById('overlay');
  if (!o) return out;
  out.overlay = true;
  try { out.dimBefore = getComputedStyle(o).backgroundColor; } catch (e) {}
  o.style.background = 'transparent';
  const cs = [...o.querySelectorAll('.controls')];
  cs.forEach(e => { e.style.display = 'none'; });
  out.controls = cs.length;
  try { out.dimAfter = getComputedStyle(o).backgroundColor; } catch (e) {}
  // 若 overlay 仍有可见子节点，说明还有其他菜单层，交给调用方决定
  out.visibleKids = [...o.children].filter(c =>
      (c.offsetWidth || c.offsetHeight) && getComputedStyle(c).display !== 'none').length;
  return out;
})()
"""


async def _probe(cdp):
    return await cdp.eval(PROBE_JS)


async def _wait(cdp, predicate, timeout, interval=0.5, log=None, label=''):
    """轮询 PROBE_JS 直到 predicate(state) 为真；返回最后的 state。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        try:
            last = await _probe(cdp)
        except Exception as e:
            last = {'error': str(e)}
        if isinstance(last, dict) and predicate(last):
            return last
        await asyncio.sleep(interval)
    if log:
        tag = (' (%s)' % label) if label else ''
        log('[local] 等待超时%s：%s'
            % (tag, json.dumps(last, ensure_ascii=False)[:300]))
    return last


async def ensure_local_game(port='auto', players=2, bind=None, restart=False,
                            timeout=90.0, log=print):
    """
    确保目标浏览器处于**本地对局中**且 input set 已绑定。

    返回 dict：{port, players, pids, roles, bind, actions, state}
    失败抛 RuntimeError。
    """
    bind = list(bind or DEFAULT_BIND)[:max(1, players)]
    port = resolve_port(port) if not isinstance(port, int) else port
    cdp = CDP(port=port)
    page = await cdp.connect_page('tanktrouble.com', timeout=15)
    log('[local] CDP :%d page=%s' % (port, page.get('url')))

    if restart:
        log('[local] 刷新页面（干净复现）…')
        await cdp.send('Page.reload', {'ignoreCache': True})
        await asyncio.sleep(3)

    # 1) 等游戏引擎就绪（phaserInstance 出现）。首次冷加载有时停在半初始化状态，
    #    这时 eval 不到 phaserInstance —— 刷新一次即可（实测有效）。
    st = await _wait(cdp, lambda s: s.get('phaser'), timeout=min(timeout, 40), log=log,
                     label='游戏引擎启动')
    if not st or not st.get('phaser'):
        log('[local] 引擎未就绪，刷新页面重试…')
        await cdp.send('Page.reload', {'ignoreCache': True})
        await asyncio.sleep(3)
        st = await _wait(cdp, lambda s: s.get('phaser'), timeout=60, log=log, label='游戏引擎启动(重试)')
    log('[local] phaser=%s' % (st.get('phaser') if st else None))

    # 2) 沿着 Menu → Lobby → 对局 推进（每轮走一步，直到拿到 gameController）
    handler = MENU_HANDLER.get(players, '_twoPlayer')
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        st = await _probe(cdp)
        if st.get('gc') and st.get('tanks'):
            break
        cur = st.get('phaser')
        if cur in ('Preload', 'Boot', 'Loader', None):
            await asyncio.sleep(1.0)
            continue
        try:
            adv = await cdp.eval(ADVANCE_JS % json.dumps(handler))
            log('[local] %s' % json.dumps(adv, ensure_ascii=False))
        except Exception as e:
            log('[local] advance err: %s' % e)
        await asyncio.sleep(1.5)

    st = await _wait(cdp, lambda s: s.get('gc') and s.get('tanks'), timeout=30, log=log,
                     label='进入对局')
    if not st or not st.get('gc') or not st.get('tanks'):
        raise RuntimeError('未能进入对局：%s' % json.dumps(st, ensure_ascii=False)[:300])

    pids = list(st['tanks'])
    plan = [[pid, bind[i % len(bind)]] for i, pid in enumerate(pids)]
    res = await cdp.eval(BIND_JS % json.dumps(plan))
    log('[local] 绑定结果: %s' % json.dumps(res, ensure_ascii=False))

    # 3) 隐藏"按键设置"DOM 遮罩（否则截图/监视画面会被灰暗层盖住）
    try:
        hid = await cdp.eval(HIDE_OVERLAY_JS)
        log('[local] 隐藏按键设置遮罩: %s' % json.dumps(hid, ensure_ascii=False))
    except Exception as e:
        log('[local] 隐藏遮罩失败（不影响对局状态）: %s' % e)

    st2 = await _probe(cdp)
    roles = st2.get('roles') or {}
    info = {'port': port, 'players': players, 'bind': bind,
            'pids': pids, 'roles': roles, 'actions': res.get('done', []),
            'model': st2.get('model'), 'inputSets': st2.get('inputSets'),
            'maze': None}
    try:
        maze = await cdp.eval("(() => { const gc=GameManager.getGameController();"
                              "const m=gc.getMaze(); return {w:m.getWidth(), h:m.getHeight()}; })()")
        info['maze'] = maze
    except Exception:
        pass
    return info


async def main():
    ap = argparse.ArgumentParser(description='自动开 TankTrouble 本地局并绑定控制角色')
    ap.add_argument('port', nargs='?', default='auto', help="CDP 端口或 'auto'（默认）")
    ap.add_argument('--players', type=int, default=2, choices=[1, 2, 3], help='本地玩家数（默认 2）')
    ap.add_argument('--bind', default=','.join(DEFAULT_BIND),
                    help='input set 绑定顺序，逗号分隔（默认 arrowKeys,WASDKeys,mouse）')
    ap.add_argument('--restart', action='store_true', help='先刷新页面再开局')
    ap.add_argument('--timeout', type=float, default=90.0, help='总超时秒数')
    ap.add_argument('--json', action='store_true', help='只输出 JSON 结果')
    args = ap.parse_args()

    bind = [b.strip() for b in args.bind.split(',') if b.strip()]
    quiet = args.json
    log = (lambda *a, **k: None) if quiet else (lambda *a, **k: print(*a, flush=True, **k))

    info = None
    if not quiet:
        print(describe(discover()), flush=True)
    try:
        info = await ensure_local_game(args.port, players=args.players, bind=bind,
                                       restart=args.restart, timeout=args.timeout, log=log)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        return 1

    ok = bool(info['roles']) and len(info['roles']) >= 1
    out = {'ok': ok, **info}
    if quiet:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print('[local] ✅ 对局中 port=%d 玩家=%d 迷宫=%s' % (info['port'], len(info['pids']), info['maze']), flush=True)
        for pid in info['pids']:
            print('        pid=%s role=%s' % (pid, info['roles'].get(pid, '?')), flush=True)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
