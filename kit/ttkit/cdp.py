"""
TankTrouble CDP 自动化 — 通过 Chrome DevTools Protocol 驱动 Edge/Chrome。
无需 Selenium/Playwright。纯 websockets。

特性：
  - 连接带 --remote-debugging-port=9222 的 Edge
  - Runtime.evaluate：在页面上下文注入/读取 JS（直接 GET 游戏状态）
  - Input.dispatchKeyEvent：模拟键盘（SET 坦克控制）
  - Console 捕获：读取页面 console.log 输出

用法：
    from cdp import CDP
    c = CDP(port=9222)
    c.connect_page()            # 附加到第一个页面（或按 URL 查找）
    val = c.eval('GameManager.getGameController().getMaze().getWidth()')
    c.key('KeyW', 'keyDown')
"""
import asyncio
import json
import urllib.request

class CDP:
    def __init__(self, port=9222, host='127.0.0.1'):
        self.port = port
        self.host = host
        self.ws = None
        self.msg_id = 0
        self.page_ws_url = None
        self.pending = {}
        self.console_lines = []
        self.reader_task = None
        self.connected = False

    # ─── HTTP 端点 ───
    def get_targets(self, timeout=2.0):
        # 注意：同步 urllib，在 asyncio 事件循环里会阻塞整个循环！
        # 调用方必须用 asyncio.to_thread 包住（见 connect_page），
        # 否则连不存在的端口时会阻塞 2 秒，冻结所有其他任务
        # （monitor 多个实例的 poll/serve 会同时卡顿）。
        import socket
        orig = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            with urllib.request.urlopen(f'http://{self.host}:{self.port}/json') as r:
                return json.loads(r.read().decode())
        finally:
            socket.setdefaulttimeout(orig)

    def find_page(self, url_substr=None, timeout=2.0):
        targets = self.get_targets(timeout=timeout)
        # 优先找 tanktrouble.com
        for t in targets:
            if t['type'] == 'page' and 'tanktrouble.com' in t['url']:
                return t
        if url_substr:
            for t in targets:
                if t['type'] == 'page' and url_substr in t['url']:
                    return t
        # 优先 about:blank（干净的导航页面）
        for t in targets:
            if t['type'] == 'page' and t['url'] == 'about:blank':
                return t
        # 回退：第一个 page 目标
        for t in targets:
            if t['type'] == 'page':
                return t
        return None

    # ─── WebSocket 连接 ───
    async def connect_page(self, url_substr=None, timeout=10):
        import websockets
        # 重连前先收掉上一条连接：否则每次重连都会泄漏一个 WebSocket、
        # 一个 reader 任务和文件描述符（monitor 长时间运行会累积）。
        if self.ws is not None:
            await self.close()
        page = None
        for _ in range(int(timeout / 0.5)):
            # to_thread：把同步 HTTP 放到线程池，避免阻塞事件循环。
            page = await asyncio.wait_for(
                asyncio.to_thread(self.find_page, url_substr, 1.0),
                timeout=1.5)
            if page:
                break
            await asyncio.sleep(0.5)
        if not page:
            raise RuntimeError('No page target found')
        self.page_ws_url = page['webSocketDebuggerUrl']
        print(f'[CDP] connecting to {self.page_ws_url}', flush=True)
        self.ws = await websockets.connect(self.page_ws_url)
        self.connected = True
        # 先启动 reader，响应才能被正确路由
        self.reader_task = asyncio.get_event_loop().create_task(self._reader())
        await self.send('Runtime.enable')
        await self.send('Page.enable')
        return page

    async def _reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if 'id' in msg and msg['id'] in self.pending:
                    fut = self.pending.pop(msg['id'])
                    # future 可能已被调用方 wait_for 超时取消：
                    # 这时 set_result 会抛 InvalidStateError，绝不能让
                    # _reader 循环死掉（否则整个 CDP 连接后续响应无人处理，
                    # 所有 eval 永久卡住 = 监控室规律性 2 秒停顿）。
                    if not fut.done():
                        try:
                            fut.set_result(msg)
                        except Exception:
                            pass
                elif msg.get('method') == 'Runtime.consoleAPICalled':
                    try:
                        args = msg['params']['args']
                        text = ' '.join(a.get('value', str(a.get('description',''))) for a in args)
                        self.console_lines.append(text)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 不能静默退出：reader 一死，后续所有 eval 都只会等到超时，
            # 表现为"监控/脚本莫名其妙卡住"。至少留下可诊断的痕迹。
            print('[CDP] reader stopped: %s: %s' % (type(e).__name__, e), flush=True)
        finally:
            self.connected = False

    async def close(self):
        """关闭 WebSocket 与 reader 任务（可安全重复调用）。"""
        task = getattr(self, 'reader_task', None)
        self.reader_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        ws, self.ws = self.ws, None
        self.connected = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def send(self, method, params=None, timeout=15):
        self.msg_id += 1
        mid = self.msg_id
        fut = asyncio.get_event_loop().create_future()
        self.pending[mid] = fut
        try:
            await self.ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f'CDP timeout waiting for {method}')
        finally:
            # 无论成功/超时/被外层取消，都从 pending 清掉，避免残留。
            # 残留的 pending 会在响应到达时被 _reader pop 后 set_result，
            # 若 fut 已取消会抛 InvalidStateError（现已在 _reader 防护）。
            self.pending.pop(mid, None)

    # ─── 高层操作 ───
    async def eval(self, expr):
        """执行 JS 表达式，返回 JSON 值（或原始对象转储）。"""
        resp = await self.send('Runtime.evaluate', {
            'expression': expr,
            'returnByValue': True,
            'awaitPromise': True,
        })
        if 'error' in resp:
            raise RuntimeError(f"CDP error: {resp['error']}")
        result = resp.get('result', {}).get('result', {})
        if result.get('subtype') == 'error':
            raise RuntimeError(f"JS error: {result.get('description')}")
        return result.get('value')

    async def key(self, code, key='', type='keyDown'):
        """模拟键盘事件。type: keyDown / keyUp / rawKeyDown。"""
        await self.send('Input.dispatchKeyEvent', {
            'type': type,
            'code': code,
            'key': key,
            'windowsVirtualKeyCode': self._code_to_vk(code),
            'nativeVirtualKeyCode': self._code_to_vk(code),
        })

    async def eval_async(self, expr):
        """执行返回 Promise 的表达式并 await 它。"""
        return await self.eval(f'(async () => {{ {expr} }})()')

    def get_console(self):
        lines = self.console_lines
        self.console_lines = []
        return lines

    @staticmethod
    def _code_to_vk(code):
        """把 JS KeyboardEvent.code 映射为 Windows 虚拟键码（近似）。"""
        table = {
            'KeyW': 87, 'KeyA': 65, 'KeyS': 83, 'KeyD': 68,
            'KeyQ': 81, 'KeyE': 69, 'KeyR': 82, 'KeyF': 70,
            'Space': 32, 'ArrowUp': 38, 'ArrowDown': 40,
            'ArrowLeft': 37, 'ArrowRight': 39, 'Enter': 13, 'Escape': 27,
            'ShiftLeft': 16, 'ShiftRight': 16, 'ControlLeft': 17,
            'ControlRight': 17, 'AltLeft': 18, 'AltRight': 18,
            'Tab': 9, 'Backspace': 8, 'Delete': 46,
        }
        return table.get(code, 0)
