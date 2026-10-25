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
TankTrouble — 给新"模块化"前端打补丁，把游戏 API 重新挂回 window
=================================================================
背景（2026-10-16）：官网前端改成 `<script type="module" src="/build/js/index-XXXX.js">`，
GWT 时期挂在 window 上的 `GameManager/ClientManager/Users/Constants/Inputs/OverlayManager/TankTrouble`
变成模块私有 → 老自动化全部失效。

本工具用 CDP `Fetch` 域拦截该 module 请求，把**打过补丁的包**回填给浏览器：
补丁在模块作用域里用 direct eval 测试所有候选类名（从包文本提取），
按静态字段特征识别 → 挂到 `window.<Name>` 与 `window.__TT`。

效果：页面里 `window.GameManager.phaserInstance` 等老 API 恢复可用
（属性名在压缩时是保留的，所以老工具代码基本可原样运行）。

用法：
    python scripts/patch_frontend_api.py auto            # 打补丁并重载页面，最后验证
    python scripts/patch_frontend_api.py auto --verify   # 只验证当前页面
注意：补丁是**每次页面加载**通过 Fetch 拦截生效的；脚本退出后新开的加载就没补丁了。
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

import websockets                                   # noqa: E402
from ego_browser import resolve_port                # noqa: E402

SCRATCH = os.path.join(HERE, 'scratch')

SIGNATURES = """
  const SIG = {
    ClientManager:  c => c && c.client !== undefined && c.clientId !== undefined,
    GameManager:    c => c && (c.phaserInstance !== undefined || typeof c.getGameController === 'function'),
    Users:          c => c && c.guestUsers !== undefined && c.authenticatedUsers !== undefined,
    Constants:      c => c && c.GAME_MODES !== undefined && (c.MAZE_TILE_SIZE !== undefined || c.METERS_PER_PIXEL !== undefined || c.SERVER !== undefined),
    Inputs:         c => c && (c.inputManagers !== undefined || c._playerIdInputSetId !== undefined || typeof c._assignInput === 'function'),
    OverlayManager: c => c && (typeof c.pushOverlay === 'function' || c.overlay !== undefined),
    TankTrouble:    c => c && c.NewGameOverlay !== undefined,
    Weapon:         c => c && typeof c.getType === 'function' && typeof c.getPlayerId === 'function',
    Tank:           c => c && typeof c.getX === 'function' && typeof c.getRotation === 'function',
    GameController: c => c && typeof c.getGameController === 'function'
  };
  // 类（构造器）签名 —— ★ 必须按原型方法判定：包里有多个 withState，只有
  //   "按键版 InputState" 才是我们要的（实例有 getForward/getBack/getLeft/getRight/getFire）
  const CSIG = {
    InputState:      c => c && c.prototype && typeof c.prototype.getForward === 'function'
                          && typeof c.prototype.getFire === 'function'
                          && typeof c.prototype.getLeft === 'function',
    TankState:       c => c && c.prototype && typeof c.prototype.getSpeedX === 'function'
                          && typeof c.prototype.getX === 'function'
                          && typeof c.prototype.getForward !== 'function',
    UIAimerGraphics: c => c && c.prototype && typeof c.prototype.addPoint === 'function',
    B2DUtils:        c => c && typeof c.calculateProjectilePath === 'function',
    UIUtils:         c => c && typeof c.mpx === 'function',
    // ★ 2026-10-23 补：UIConstants（像素单位常量，AIMER_OFFSET=50px=2.5m 等）。
    //   official_ballistics / official_aim / aimer_* 都读 window.UIConstants.AIMER_OFFSET，
    //   漏导出会让弹药/瞄准线模块直接崩（"Cannot read properties of undefined"）。
    UIConstants:     c => c && c.AIMER_OFFSET !== undefined && c.AIMER_WIDTH !== undefined,
    PremiumManager:  c => c && typeof c.isPremiumPresent === 'function'
  };
  // 命名空间对象签名
  const NSIG = {
    TankTrouble:    o => o && o.NewGameOverlay !== undefined,
    OverlayNamespace: o => o && o.NewGameOverlay !== undefined && o.AccountOverlay !== undefined,
    UIUtils:        o => o && typeof o.mpx === 'function',
    Aimer:          o => o && (typeof o.getAimAngle === 'function' || typeof o.castRay === 'function')
  };
"""


def extract_names(text):
    names = set()
    for m in re.finditer(r'(?:var|let|const)\s+([A-Za-z_$][A-Za-z0-9_$]{0,8})\s*=\s*class\b', text):
        names.add(m.group(1))
    for m in re.finditer(r'\bclass\s+([A-Za-z_$][A-Za-z0-9_$]{0,20})\b', text):
        names.add(m.group(1))
    # 普通命名空间对象（如 TankTrouble = { NewGameOverlay: ... }）
    for m in re.finditer(r'(?:var|let|const)\s+([A-Za-z_$][A-Za-z0-9_$]{0,8})\s*=\s*\{\s*[A-Za-z_$][A-Za-z0-9_$]{0,20}\s*:', text):
        names.add(m.group(1))
    # 命名空间的另一种写法：先建对象，再 X.NewGameOverlay = {...}
    for marker in ('NewGameOverlay', 'AccountOverlay', 'LoginOverlay', 'GarageOverlay'):
        for m in re.finditer(r'([A-Za-z_$][A-Za-z0-9_$]{0,6})\.' + marker + r'\s*=', text):
            names.add(m.group(1))
    return sorted(names)


def build_patch(text):
    names = extract_names(text)
    literal = json.dumps(names)
    patch = """
;(function () {
  try {
%s
    const names = %s;
    const out = {};
    const objs = {};
    for (const nm of names) {
      let v;
      try { v = eval(nm); } catch (e) { continue; }
      if (typeof v === 'function') {
        for (const key in SIG) { if (out[key]) continue;
          try { if (SIG[key](v)) out[key] = v; } catch (e) {} }
        for (const key in CSIG) { if (out[key]) continue;
          try { if (CSIG[key](v)) out[key] = v; } catch (e) {} }
        try { window.__TT_ALL = window.__TT_ALL || {}; if (!window.__TT_ALL[nm]) window.__TT_ALL[nm] = v; } catch (e) {}
        // 消息类：按 typeId 汇总，便于传输层探针
        try {
          const tid = (v.typeId !== undefined) ? v.typeId : v.TYPE_ID;
          if (tid !== undefined) {
            window.__TT_MSG = window.__TT_MSG || {};
            if (!window.__TT_MSG[tid]) window.__TT_MSG[tid] = v;
          }
        } catch (e) {}
      } else if (v && typeof v === 'object') {
        try { if (!objs[nm]) objs[nm] = v; } catch (e) {}
        for (const key in NSIG) { if (out[key]) continue;
          try { if (NSIG[key](v)) out[key] = v; } catch (e) {} }
        try { window.__TT_OBJ = window.__TT_OBJ || {}; if (!window.__TT_OBJ[nm]) window.__TT_OBJ[nm] = v; } catch (e) {}
      }
    }
    window.__TT = out;
    for (const k in out) { try { window[k] = out[k]; } catch (e) {} }
    // 兼容别名：老代码读 Inputs.__metaclass.inputManagers / ._playerIdInputSetId
    try { if (out.Inputs && !out.Inputs.__metaclass) out.Inputs.__metaclass = out.Inputs; } catch (e) {}
    window.__TT_STATUS = {names: names.length, found: Object.keys(out),
                          objNamespaces: Object.keys(objs).length};
  } catch (e) { window.__TT_STATUS = {error: String(e)}; }
})();
""" % (SIGNATURES, literal)
    return patch


class Session:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.id = 0

    async def connect(self):
        self.ws = await websockets.connect(self.url, max_size=128 * 1024 * 1024)
        return self

    async def send(self, method, params=None, timeout=30):
        self.id += 1
        mid = self.id
        await self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError('%s → %s' % (method, msg['error']))
                return msg.get('result', {})

    async def recv(self, timeout=30):
        return json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))

    async def close(self):
        if self.ws:
            await self.ws.close()


VERIFY_JS = """(() => {
  const out = {};
  for (const n of ['GameManager','ClientManager','Users','Constants','Inputs','OverlayManager','TankTrouble','Weapon','Tank'])
    out[n] = typeof window[n];
  out.status = window.__TT_STATUS || null;
  out.allCount = window.__TT_ALL ? Object.keys(window.__TT_ALL).length : 0;
  try { out.phaserInstance = !!(window.GameManager && window.GameManager.phaserInstance);
        out.state = window.GameManager.phaserInstance ? window.GameManager.phaserInstance.state.current : null; } catch (e) { out.gErr = String(e); }
  try { out.clientState = window.ClientManager.client ? window.ClientManager.client.state : null; } catch (e) {}
  try { out.users = window.Users.getAllPlayerIds(); } catch (e) {}
  try { out.gameModes = window.Constants.GAME_MODES; } catch (e) {}
  try { out.controllerOk = typeof window.GameManager.getGameController === 'function'; } catch (e) {}
  return JSON.stringify(out);
})()"""


async def main():
    ap = argparse.ArgumentParser(description='给模块化前端重挂游戏 API')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--verify', action='store_true', help='只验证，不重载')
    ap.add_argument('--keep-open', action='store_true', help='保持 Fetch 拦截（脚本挂起）')
    ap.add_argument('--bundle', default=None, help='本地 bundle 副本路径')
    args = ap.parse_args()

    port = resolve_port(args.port)
    targets = json.loads(urllib.request.urlopen('http://127.0.0.1:%d/json' % port, timeout=10).read())
    page = next((t for t in targets if t.get('type') == 'page' and 'tanktrouble' in (t.get('url') or '')), None)
    if not page:
        print('[patch] ❌ 找不到 tanktrouble 页面')
        return 1
    s = await Session(page['webSocketDebuggerUrl']).connect()

    if args.verify:
        r = await s.send('Runtime.evaluate', {'expression': VERIFY_JS, 'returnByValue': True})
        print('[patch] 验证: %s' % r['result'].get('value'))
        await s.close()
        return 0

    # 1) 取 bundle 文本
    ev = await s.send('Runtime.evaluate', {
        'expression': "(() => Array.from(document.scripts).map(s => s.src).filter(u => /index-.*\\.js/.test(u))[0] || '')()",
        'returnByValue': True})
    bundle_url = ev['result'].get('value') or ''
    name = bundle_url.split('/')[-1]
    path = args.bundle or os.path.join(SCRATCH, name)
    if not os.path.exists(path):
        urllib.request.urlretrieve(bundle_url, path)
    text = open(path, encoding='utf-8', errors='replace').read()
    patched = text + build_patch(text)
    body = base64.b64encode(patched.encode('utf-8')).decode('ascii')
    print('[patch] bundle=%s 原 %d KB → 补丁后 %d KB' % (name, len(text) // 1024, len(patched) // 1024), flush=True)

    # 2) 开 Fetch 拦截并重载
    await s.send('Fetch.enable', {'patterns': [
        {'urlPattern': '*%s*' % name, 'requestStage': 'Request'}]})
    print('[patch] Fetch 拦截已开，重载页面…', flush=True)
    await s.send('Page.enable')
    await s.send('Page.reload', {'ignoreCache': True})

    done = False
    end = asyncio.get_event_loop().time() + 60
    while not done and asyncio.get_event_loop().time() < end:
        try:
            msg = await s.recv(timeout=20)
        except asyncio.TimeoutError:
            break
        if msg.get('method') == 'Fetch.requestPaused':
            p = msg['params']
            try:
                await s.send('Fetch.fulfillRequest', {
                    'requestId': p['requestId'], 'responseCode': 200,
                    'responseHeaders': [
                        {'name': 'Content-Type', 'value': 'application/javascript; charset=utf-8'},
                        {'name': 'Cache-Control', 'value': 'no-store'}],
                    'body': body})
                print('[patch] 已回填补丁包: %s' % (p.get('request') or {}).get('url', '')[-40:], flush=True)
                done = True
            except Exception as e:
                print('[patch] 回填失败: %s' % e, flush=True)
    await asyncio.sleep(8 if not args.keep_open else 0)

    if not args.keep_open:
        await s.send('Fetch.disable')
        await asyncio.sleep(6)
        r = await s.send('Runtime.evaluate', {'expression': VERIFY_JS, 'returnByValue': True})
        print('\n[patch] 验证: %s' % r['result'].get('value'), flush=True)
        await s.close()
        return 0
    print('[patch] Fetch 保持开启：持续回填每一次 bundle 请求（Ctrl+C 结束）…', flush=True)
    while True:
        try:
            msg = await s.recv(timeout=600)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            print('[patch] 连接结束: %s' % e, flush=True)
            break
        if msg.get('method') == 'Fetch.requestPaused':
            p = msg['params']
            try:
                await s.send('Fetch.fulfillRequest', {
                    'requestId': p['requestId'], 'responseCode': 200,
                    'responseHeaders': [
                        {'name': 'Content-Type', 'value': 'application/javascript; charset=utf-8'},
                        {'name': 'Cache-Control', 'value': 'no-store'}],
                    'body': body})
                print('[patch] 已回填: %s' % (p.get('request') or {}).get('url', '')[-36:], flush=True)
            except Exception as e:
                print('[patch] 回填失败: %s' % e, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
