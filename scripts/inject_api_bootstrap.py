#!/usr/bin/env python
"""
TankTrouble — 新前端（<script type="module">）API 引导
=====================================================
背景（2026-10-16 实测）：官网前端已改成打包的 ES module
（`/build/js/index-XXXX.js`），GWT 时期挂在 window 上的
`GameManager / ClientManager / Users / Constants / Inputs / OverlayManager / TankTrouble`
**全部不再导出**（模块私有），我们整套自动化因此失效。

本工具用 CDP **调试器**绕过模块私有作用域：
  1. Debugger.enable → Debugger.pause（游戏 rAF 循环正在跑，必停）
  2. 在暂停的调用帧里沿 scopeChain 找到 **module 作用域**
  3. Runtime.getProperties 枚举模块变量，按"静态字段特征"识别出各类
     （ClientManager: static client/clientId；Users: static guestUsers；
       GameManager: static phaserInstance/getGameController；...）
  4. 用 Runtime.callFunctionOn 把识别到的类**赋值回 window**
  5. Debugger.resume → 之后老工具链（window.GameManager 等）照旧可用

用法：
    python scripts/inject_api_bootstrap.py auto            # 引导并打印结果
    python scripts/inject_api_bootstrap.py auto --json out.json
注意：**刷新页面后需重新引导**（模块作用域每次都是新的）。
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

import websockets                                   # noqa: E402
from ego_browser import resolve_port                # noqa: E402

# 识别规则：静态字段/方法名特征 → 我们想要的全局名
SIGNATURES = {
    'ClientManager':  ['client', 'clientId', 'availableServers', 'getClient'],
    'GameManager':    ['phaserInstance', 'getGameController', 'gameController'],
    'Users':          ['guestUsers', 'authenticatedUsers', 'getAllPlayerIds'],
    'Constants':      ['GAME_MODES', 'MAZE_TILE_SIZE', 'METERS_PER_PIXEL'],
    'Inputs':         ['inputManagers', '_assignInput', '_playerIdInputSetId'],
    'OverlayManager': ['pushOverlay', 'popOverlay', 'overlay'],
    'TankTrouble':    ['NewGameOverlay', 'LoginOverlay', 'AccountOverlay'],
}

CLASSIFY_JS = """function () {
  const st = Object.getOwnPropertyNames(this);
  const proto = this.prototype ? Object.getOwnPropertyNames(this.prototype) : [];
  return JSON.stringify({name: this.name || null, statics: st.slice(0, 120), proto: proto.slice(0, 120)});
}"""


class Session:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.id = 0
        self.pending = {}

    async def connect(self):
        self.ws = await websockets.connect(self.url, max_size=64 * 1024 * 1024)
        return self

    async def send(self, method, params=None):
        self.id += 1
        mid = self.id
        await self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=25))
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError('%s → %s' % (method, msg['error']))
                return msg.get('result', {})

    async def wait_event(self, method, timeout=15):
        end = asyncio.get_event_loop().time() + timeout
        while True:
            left = end - asyncio.get_event_loop().time()
            if left <= 0:
                raise TimeoutError('no event %s' % method)
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=left))
            if msg.get('method') == method:
                return msg['params']
            if 'id' in msg and msg['id'] in self.pending:
                self.pending[msg['id']] = msg

    async def close(self):
        if self.ws:
            await self.ws.close()


async def classify(fr, obj_id):
    r = await fr.send('Runtime.callFunctionOn', {
        'functionDeclaration': CLASSIFY_JS, 'objectId': obj_id, 'returnByValue': True})
    try:
        return json.loads(r['result']['value'])
    except Exception:
        return {'name': None, 'statics': [], 'proto': []}


def match(info):
    statics = set(info.get('statics') or []) | set(info.get('proto') or [])
    hits = []
    for name, sig in SIGNATURES.items():
        if all(s in statics for s in sig[:2]) or (len(sig) > 2 and sum(s in statics for s in sig) >= 2):
            hits.append(name)
    return hits


async def main():
    ap = argparse.ArgumentParser(description='新前端 API 引导（window 全局恢复）')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    # 用 CDP HTTP 端点拿 page 的 ws url
    import urllib.request
    targets = json.loads(urllib.request.urlopen('http://127.0.0.1:%d/json' % port, timeout=10).read())
    page = next((t for t in targets if t.get('type') == 'page' and 'tanktrouble' in (t.get('url') or '')), None)
    if not page:
        print('[boot] ❌ 找不到 tanktrouble 页面')
        return 1
    print('[boot] 页面: %s' % page['url'], flush=True)

    s = await Session(page['webSocketDebuggerUrl']).connect()
    await s.send('Runtime.enable')
    await s.send('Debugger.enable')

    # 找到打包文件 URL + 挑一行"每个循环都会执行"的代码下断点
    # （Debugger.pause 常停在非模块帧，拿不到 module 作用域；下断点则必停在模块内）
    ev = await s.send('Runtime.evaluate', {
        'expression': "(() => Array.from(document.scripts).map(s => s.src).filter(u => /index-.*\\.js/.test(u))[0] || '')()",
        'returnByValue': True})
    bundle_url = ev['result'].get('value') or ''
    print('[boot] bundle: %s' % bundle_url, flush=True)
    bundle_path = os.path.join(HERE, 'scratch', bundle_url.split('/')[-1])
    if not os.path.exists(bundle_path):
        import urllib.request
        urllib.request.urlretrieve(bundle_url, bundle_path)
    with open(bundle_path, encoding='utf-8', errors='replace') as fh:
        lines = fh.readlines()
    bp_line = None
    for i, ln in enumerate(lines):
        if 'requestAnimationFrame' in ln:
            bp_line = i
            break
    if bp_line is None:
        bp_line = min(100, len(lines) - 1)
    print('[boot] 包 %d 行；断点行 %d' % (len(lines), bp_line), flush=True)
    bp = await s.send('Debugger.setBreakpointByUrl', {'lineNumber': bp_line, 'url': bundle_url})
    bid = (bp.get('breakpointId') or '')
    await s.send('Debugger.resume')      # 若之前是 paused 状态
    try:
        paused = await s.wait_event('Debugger.paused', timeout=20)
    except TimeoutError:
        print('[boot] 断点未命中，退回 Debugger.pause…', flush=True)
        await s.send('Debugger.pause')
        try:
            paused = await s.wait_event('Debugger.paused', timeout=10)
        except TimeoutError:
            print('[boot] ❌ 无法暂停页面')
            await s.close()
            return 1

    frames = paused.get('callFrames') or []
    print('[boot] 暂停帧 %d 个: %s' % (len(frames), [f.get('url', '').split('/')[-1] for f in frames[:5]]), flush=True)
    target = next((f for f in frames if 'index-' in (f.get('url') or '')), frames[0] if frames else None)
    if not target:
        print('[boot] ❌ 没有调用帧')
        await s.close()
        return 1
    print('[boot] 选用帧: %s:%s' % ((target.get('url') or '').split('/')[-1], target.get('location', {}).get('lineNumber')), flush=True)

    # 枚举作用域链里的变量
    found = {}
    for scope in (target.get('scopeChain') or []):
        oid = (scope.get('object') or {}).get('objectId')
        if not oid:
            continue
        try:
            props = await s.send('Runtime.getProperties', {'objectId': oid, 'ownProperties': True})
        except Exception:
            continue
        names = [p for p in (props.get('result') or []) if p.get('enumerable') is not False and 'value' in p]
        funcs = [p for p in names if (p['value'] or {}).get('type') == 'function']
        print('[boot] 作用域 %-8s 变量 %d 个（函数 %d）' % (scope.get('type'), len(names), len(funcs)), flush=True)
        for p in funcs:
            oid2 = (p.get('value') or {}).get('objectId')
            if not oid2:
                continue
            info = await classify(s, oid2)
            hits = match(info)
            for h in hits:
                if h not in found:
                    found[h] = {'var': p['name'], 'objectId': oid2,
                                'statics': info.get('statics', [])[:12], 'cls': info.get('name')}

    print('\n[boot] 识别结果:', flush=True)
    for k, v in found.items():
        print('  %-16s ← 模块变量 %-8s (class %s) 静态字段样例=%s'
              % (k, v['var'], v['cls'], v['statics'][:6]), flush=True)

    # 注入 window
    injected = []
    for name, v in found.items():
        js = 'function () { window.%s = this; window.__TT = window.__TT || {}; window.__TT.%s = this; return "ok"; }' % (name, name)
        try:
            r = await s.send('Runtime.callFunctionOn', {
                'functionDeclaration': js, 'objectId': v['objectId'], 'returnByValue': True})
            injected.append(name)
        except Exception as e:
            print('  ⚠️ 注入 %s 失败: %s' % (name, e), flush=True)

    await s.send('Debugger.resume')
    if bid:
        try:
            await s.send('Debugger.removeBreakpoint', {'breakpointId': bid})
        except Exception:
            pass
    await s.send('Debugger.disable')
    print('\n[boot] 已注入 window: %s' % injected, flush=True)

    # 验证
    verify = await s.send('Runtime.evaluate', {
        'expression': """(() => { const out = {};
            for (const n of %s) { try { out[n] = typeof window[n]; } catch (e) { out[n] = 'err'; } }
            try { out.phaserInstance = !!(window.GameManager && window.GameManager.phaserInstance); } catch (e) {}
            try { out.client = !!(window.ClientManager && window.ClientManager.client); } catch (e) {}
            try { out.users = window.Users ? window.Users.getAllPlayerIds() : null; } catch (e) {}
            return JSON.stringify(out); })()""" % json.dumps(injected),
        'returnByValue': True})
    print('[boot] 验证: %s' % verify['result'].get('value'), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'found': {k: {kk: vv for kk, vv in v.items() if kk != 'objectId'} for k, v in found.items()},
                       'injected': injected, 'verify': verify['result'].get('value')}, fh, ensure_ascii=False, indent=1)
    await s.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
