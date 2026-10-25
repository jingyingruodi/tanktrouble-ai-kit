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
# -----------------------------------------------------------------------------
# ⚠️ 在线房间工具附加声明 / ONLINE-ROOM NOTICE  [DISCLAIMER-STAMP-ONLINE]
# 本脚本仅限【受控自建私人房间 / 本地研究环境】使用。
# 在公开在线对局使用自动化/辅助工具违反官方社区规则，可能导致账号被永久封禁
# （官方规则第 8 条 "No hacking on the website"，零容忍、无警告）。
# 请勿将本脚本用于干扰他人游戏体验或任何商业作弊服务。
# -----------------------------------------------------------------------------
"""
TankTrouble — 退出在线房间（并确认没被踢/未被登出）
==================================================
**为什么必须显式退出**：在线模式有两套服务端校验，长时间挂机很危险：
  * `Constants.SERVER.MAX_INACTIVITY_TIME = 60000` —— 60s 不活动会被踢、可能顺带登出；
  * 追踪校验 `TRACKING_*` + `TANKSTATE_VIOLATION_REASONS`
    （EMISSION_TOO_OFTEN / MOVED_TOO_FAST / TURNED_TOO_FAST），
    `TRACKING_WINDOW_SIZE=5`、`TRACKING_VIOLATION_DURATION=60000`、`TRACKING_VIOLATION_THRESHOLD=5`。

因此**任何进过在线房间的脚本，结束前都应调用本脚本退出**（或使用
`start_online_game.py --keepalive` 保活）。

用法：
    python scripts/leave_online_game.py auto
    python scripts/leave_online_game.py auto --json
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

from cdp import CDP                    # noqa: E402
from ego_browser import resolve_port   # noqa: E402

STATE_JS = """(() => {
  const g = GameManager.phaserInstance;
  const c = g && g.state ? g.state.current : null;
  const gc = GameManager.getGameController ? GameManager.getGameController() : null;
  const client = (typeof ClientManager !== 'undefined') ? ClientManager.getClient() : null;
  let mode = null; try { mode = String(Constants.getMode()); } catch (e) {}
  let tanks = []; try { tanks = gc ? Object.keys(gc.getTanks() || {}) : []; } catch (e) {}
  return JSON.stringify({state: c, mode: mode, tanks: tanks,
    gameId: client && client.getCurrentGameId ? client.getCurrentGameId() : null,
    clientState: client ? client.state : null});
})()"""

LEAVE_JS = """(() => {
  const client = ClientManager.getClient();
  if (!client) return JSON.stringify({ok: false, err: 'no client'});
  const gid = client.getCurrentGameId ? client.getCurrentGameId() : null;
  if (!gid) return JSON.stringify({ok: true, did: 'not in an online game', gameId: null});
  // force=true 立即退出；force=false 是"打完本回合再走"的优雅退出（可能几十秒不生效）
  client.leaveGame(%s);
  return JSON.stringify({ok: true, did: 'client.leaveGame(%s)', gameId: gid});
})()"""


async def main():
    ap = argparse.ArgumentParser(description='退出在线房间')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--timeout', type=float, default=30.0)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)
    st = json.loads(await cdp.eval(STATE_JS))
    print('[leave] 退出前: state=%s mode=%s gameId=%s clientState=%s tanks=%s'
          % (st['state'], st['mode'], st['gameId'], st['clientState'], st['tanks']), flush=True)

    r = json.loads(await cdp.eval(LEAVE_JS % ('true', 'true')))
    print('[leave] %s' % json.dumps(r, ensure_ascii=False), flush=True)

    t0 = asyncio.get_event_loop().time()
    gone = False
    while asyncio.get_event_loop().time() - t0 < args.timeout:
        st = json.loads(await cdp.eval(STATE_JS))
        if not st['gameId'] and not st['tanks']:
            gone = True
            break
        await asyncio.sleep(1)
    st = json.loads(await cdp.eval(STATE_JS))
    out = {'left': gone, 'state': st['state'], 'mode': st['mode'], 'gameId': st['gameId'],
           'clientState': st['clientState'], 'tanks': st['tanks'], 'port': port}
    print('[leave] 退出后: state=%s mode=%s gameId=%s clientState=%s tanks=%s → %s'
          % (st['state'], st['mode'], st['gameId'], st['clientState'], st['tanks'],
             '✅ 已离开在线房间' if gone else '⚠️ 仍在房间/未确认'))
    print(json.dumps(out, ensure_ascii=False))
    await cdp.close()
    return 0 if gone else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
