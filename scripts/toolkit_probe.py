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
TankTrouble — 工具链 API 兼容性体检
==================================
新前端（ES module 打包）把 API 变成模块私有后，我们靠 `scripts/patch_frontend_api.py`
把它们重新挂回 window。本工具逐项检查**我们工具链实际依赖的 API**是否还在、
形状是否一致（属性名在压缩时保留，所以多数应完好），并给出缺失清单。

用法：
    python scripts/toolkit_probe.py auto            # 逐项体检
    python scripts/toolkit_probe.py auto --json out.json
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

from cdp import CDP                                  # noqa: E402
from ego_browser import resolve_port                 # noqa: E402

PROBE = r"""(() => {
  const out = {ok: {}, missing: [], notes: {}};
  const has = (path, val) => { out.ok[path] = val; return val; };
  const chk = (path, fn) => { let v = null; try { v = fn(); } catch (e) { v = 'ERR:' + e.message; }
    if (v === undefined || v === null || v === false || (typeof v === 'string' && v.indexOf('ERR:') === 0))
      out.missing.push(path + '=' + v); else out.ok[path] = v; };
  const G = window.GameManager, C = window.ClientManager, U = window.Users, K = window.Constants;

  // ---- GameManager ----
  chk('GameManager', () => typeof G);
  chk('GameManager.phaserInstance', () => !!G.phaserInstance);
  chk('GameManager.phaserInstance.state.current', () => G.phaserInstance.state.current);
  chk('GameManager.getGameController', () => typeof G.getGameController);
  chk('GameManager.gameController', () => typeof G.gameController);
  // ---- GameController ----
  chk('gc.getTanks', () => typeof G.getGameController().getTanks);
  chk('gc.getMaze', () => typeof G.getGameController().getMaze);
  chk('gc.getActiveWeapon', () => typeof G.getGameController().getActiveWeapon);
  chk('gc.model.getState', () => String(G.getGameController().model.getState()));
  chk('gc.roundController.model.getProjectiles', () => typeof G.getGameController().roundController.model.getProjectiles);
  // ---- ClientManager ----
  chk('ClientManager.getClient', () => typeof C.getClient);
  chk('CM.client.state', () => C.client && C.client.state);
  chk('CM.getClient().getCurrentGameId', () => typeof C.getClient().getCurrentGameId);
  chk('CM.getClient().leaveGame', () => typeof C.getClient().leaveGame);
  chk('CM.getClient().authenticatedPlayerIds', () => JSON.stringify(C.getClient().authenticatedPlayerIds));
  chk('CM.getClient().getGameState', () => typeof C.getClient().getGameState);
  chk('CM.getClient().getAvailableGameStates', () => typeof C.getClient().getAvailableGameStates);
  chk('CM.getClient().createNewGame', () => typeof C.getClient().createNewGame);
  chk('CM.getClient().quickjoinGame', () => typeof C.getClient().quickjoinGame);
  // ---- Users ----
  chk('Users.getAllPlayerIds', () => JSON.stringify(U.getAllPlayerIds()));
  chk('Users.guestUsers', () => JSON.stringify(Object.keys(U.guestUsers || {})));
  chk('Users.addGuestUser', () => typeof U.addGuestUser);
  chk('Users.getGuestMultiplayerTokens', () => typeof U.getGuestMultiplayerTokens);
  // ---- Inputs ----
  chk('Inputs._assignInput', () => typeof window.Inputs._assignInput);
  chk('Inputs.__metaclass', () => !!window.Inputs.__metaclass);
  chk('Inputs.inputManagers', () => (window.Inputs.inputManagers || (window.Inputs.__metaclass && window.Inputs.__metaclass.inputManagers) || []).length);
  chk('Inputs._playerIdInputSetId', () => JSON.stringify(window.Inputs._playerIdInputSetId ||
      (window.Inputs.__metaclass && window.Inputs.__metaclass._playerIdInputSetId) || null));
  // ---- OverlayManager ----
  chk('OverlayManager.pushOverlay', () => typeof window.OverlayManager.pushOverlay);
  chk('OverlayManager.popOverlay', () => typeof window.OverlayManager.popOverlay);
  chk('OverlayManager.overlay', () => !!window.OverlayManager.overlay);
  // ---- Constants ----
  for (const k of ['GAME_MODES','COLLECTIBLE_TYPES','SERVER','CLIENT','MAZE_TILE_SIZE','METERS_PER_PIXEL','GAME_MODE_INFO','GAME','TANK','AIMER_LENGTH']) {
    chk('Constants.' + k, () => {
      const v = K[k];
      if (v === undefined) return undefined;
      return (typeof v === 'object') ? ('obj:' + Object.keys(v).length) : String(v);
    });
  }
  // ---- TankTrouble（新房面板所在命名空间）；顺便在 __TT_ALL 里找 NewGameOverlay ----
  chk('window.TankTrouble', () => typeof window.TankTrouble);
  try {
    const all = window.__TT_ALL || {};
    const hits = [];
    for (const nm in all) {
      const v = all[nm];
      try {
        if (v && (v.NewGameOverlay || v.LoginOverlay || v.AccountOverlay)) hits.push(nm);
        else if (typeof v === 'function' && v.name && /Overlay/.test(v.name)) hits.push(nm + ':' + v.name);
      } catch (e) {}
    }
    out.notes.overlayCandidates = hits.slice(0, 12);
  } catch (e) { out.notes.overlayErr = String(e); }
  // ---- 消息类（探针用）----
  try {
    const all = window.__TT_ALL || {};
    const msgs = [];
    for (const nm in all) {
      try { const v = all[nm];
        if (typeof v === 'function' && (v.typeId !== undefined || v.TYPE_ID !== undefined)) msgs.push(nm); } catch (e) {}
    }
    out.notes.messageClasses = msgs.slice(0, 12);
    out.notes.messageClassCount = msgs.length;
  } catch (e) {}
  out.notes.ttAllCount = window.__TT_ALL ? Object.keys(window.__TT_ALL).length : 0;
  out.notes.patchStatus = window.__TT_STATUS || null;
  return JSON.stringify(out);
})()"""


async def main():
    ap = argparse.ArgumentParser(description='工具链 API 兼容性体检')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()
    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=25)
    d = json.loads(await cdp.eval(PROBE))
    print('=== 体检结果 ===')
    for k, v in d['ok'].items():
        print('  ✅ %-46s %s' % (k, v))
    if d['missing']:
        print('\n=== ⚠️ 缺失/异常 (%d) ===' % len(d['missing']))
        for m in d['missing']:
            print('  ❌ %s' % m)
    print('\n=== 备注 ===')
    for k, v in (d.get('notes') or {}).items():
        print('  %-22s %s' % (k, json.dumps(v, ensure_ascii=False)[:300]))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        print('\n→ %s' % args.json)
    await cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
