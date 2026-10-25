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
TankTrouble — 在线消息探针（看服务端怎么回话）
=============================================
**为什么需要**：在线模式的很多限制（例如自建房间被拒）由**服务端**决定，
客户端 UI 往往只显示"没反应"。本脚本临时包装 `ClientManager.getClient().socket.send`
并监听 `message` 事件，记录一段时间内的收发帧 —— 服务端消息是紧凑 JSON，
带 `_typeId`，其中 `_typeId=53` 的 `message` 字段就是拒绝/提示理由
（实测例子：自建房间 → `"No premium users"`）。

**纪律**：这是**传输层临时观测**（不是替换 GWT 游戏方法），
脚本结束前一定还原 `socket.send` 与事件监听；失败路径也会还原。

用法：
    python scripts/probe_online_messages.py auto                 # 只记录 8s 基线
    python scripts/probe_online_messages.py auto --seconds 20
    python scripts/probe_online_messages.py auto --create-room 0 # 记录期间尝试建 CLASSIC 房
    python scripts/probe_online_messages.py auto --join          # 记录期间加入公共房间
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

INSTALL = """(() => {
  const c = ClientManager.getClient();
  if (window.__wsHooked) return JSON.stringify({ok: true, already: true});
  const sock = c.socket;
  window.__wsSent = []; window.__wsRecv = [];
  window.__wsOrigSend = sock.send;
  const orig = sock.send.bind(sock);
  sock.send = function (d) {
    try { window.__wsSent.push({t: Date.now(),
      len: (d && d.length !== undefined) ? d.length : -1}); } catch (e) {}
    return orig(d);
  };
  window.__wsOnMsg = function (ev) {
    try { const d = ev.data;
      window.__wsRecv.push({t: Date.now(), len: (d && d.length !== undefined) ? d.length : -1,
        text: (typeof d === 'string') ? d.slice(0, 400) : null}); } catch (e) {}
  };
  sock.addEventListener('message', window.__wsOnMsg);
  window.__wsHooked = true;
  return JSON.stringify({ok: true, hooked: true});
})()"""

RESTORE = """(() => {
  const c = ClientManager.getClient();
  const sock = c.socket;
  if (window.__wsOrigSend) sock.send = window.__wsOrigSend;
  if (window.__wsOnMsg) { try { sock.removeEventListener('message', window.__wsOnMsg); } catch (e) {} }
  window.__wsHooked = false;
  return JSON.stringify({ok: true, restored: true,
    sent: (window.__wsSent || []).length, recv: (window.__wsRecv || []).length});
})()"""

DUMP = """(() => JSON.stringify({
  sent: (window.__wsSent || []).map(f => ({t: f.t, len: f.len})),
  recv: (window.__wsRecv || []).map(f => ({t: f.t, len: f.len, text: f.text})),
  state: (function () { const c = ClientManager.getClient();
    return {gameId: c.getCurrentGameId(), joining: GameManager.phaserInstance.state.getCurrentState().joiningGame}; })()
}))()"""


async def main():
    ap = argparse.ArgumentParser(description='在线消息探针')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--seconds', type=float, default=8.0)
    ap.add_argument('--create-room', type=int, default=None, help='期间尝试自建房间（模式号）')
    ap.add_argument('--join', action='store_true', help='期间尝试加入公共房间')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)
    print('[ws] 安装帧记录: %s' % await cdp.eval(INSTALL), flush=True)

    try:
        if args.create_room is not None:
            js = ("(() => { const st = GameManager.phaserInstance.state.getCurrentState();"
                  " st.createNewGame(false,false,false,%d,3,[0,1,2,3,4,5]);"
                  " return JSON.stringify({did:'createNewGame', mode:%d}); })()"
                  % (args.create_room, args.create_room))
            print('[ws] 建房请求: %s' % await cdp.eval(js), flush=True)
        if args.join:
            js = ("(() => { const st = GameManager.phaserInstance.state.getCurrentState();"
                  " st._joinRandomGame(); return JSON.stringify({did:'_joinRandomGame'}); })()")
            print('[ws] 加入请求: %s' % await cdp.eval(js), flush=True)

        await asyncio.sleep(args.seconds)
        dump = json.loads(await cdp.eval(DUMP))
    finally:
        print('[ws] 还原: %s' % await cdp.eval(RESTORE), flush=True)
        await cdp.close()

    print('[ws] 发出 %d 帧 / 收到 %d 帧，状态=%s'
          % (len(dump['sent']), len(dump['recv']), json.dumps(dump['state'], ensure_ascii=False)),
          flush=True)
    interesting = [f for f in dump['recv'] if f.get('text') and '"message"' in f['text']]
    for f in interesting[-8:]:
        print('  ← %s' % f['text'], flush=True)
    if args.json:
        print(json.dumps(dump, ensure_ascii=False)[:3000])
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
