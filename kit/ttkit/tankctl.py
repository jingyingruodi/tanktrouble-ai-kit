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
TankTrouble 统一控制接口
========================
基于 CDP 的高层、人类友好的坦克控制封装。

适用场景：
  - 单实例：  cdp_ctl = TankCtl(port=9222); cdp_ctl.connect()
  - 多实例：  farm = EdgeFarm(3); [TankCtl.from_farm(farm, i) for i in range(3)]

控制路径（重要）
----------------
在线多人模式是【服务器权威】：服务器拥有坦克坐标。直接调用
tank.setForward/setRotation 等只修改本地对象，下一帧就被服务器覆盖
（回弹 rubber-banding），开火也被忽略。

唯一被服务器接受的路径，是键盘/鼠标输入使用的真实输入链路：
    InputState.withState(playerId, fwd, back, left, right, fire)
        -> gc.setInputState(...)          （setTankState + emitTankState 发服务器）

下面的所有高层动作（move_forward / turn_left / fire / aim_at ...）都走
TankCtl.set_input()，即上面这条链路 —— 在线安全。本地局行为一致，
所以这套 API 在线/本地通用。

API:
  c = TankCtl(port=9222)
  await c.connect()
  await c.state()            -> {tanks: {tankId: {x,y,rot,alive}}, projectiles: [...]}
  await c.tanks()            -> 坦克状态字典
  await c.my_tank()          -> 选一辆坦克（可按序号）
  # 基础行为（在线安全，全部走 setInputState）：
  await c.move_forward(sec, tank_id)   # 前进 `sec` 秒
  await c.move_back(sec, tank_id)      # 后退
  await c.turn_left(sec, tank_id)      # 左转（减小 getRotation()）
  await c.turn_right(sec, tank_id)     # 右转（增大 getRotation()）
  await c.fire(sec, tank_id)           # 开火（默认点按 0.15s）
  await c.aim_at(x, y, tank_id)        # 转向瞄准世界坐标
  await c.stop_all(tank_id)            # 松开所有移动输入
  await c.set_input(tank_id, fwd=False, back=False, left=False,
                    right=False, fire=False, hold=0.0, freq=30)  # 原始输入状态
"""
import asyncio
import json
import math
import sys

sys.path.insert(0, '.')

# 人类名称 -> Phaser 键码
KEYS = {
    # WASD 玩家
    'w': 87, 'a': 65, 's': 83, 'd': 68, 'q': 81,
    # 方向键玩家
    'up': 38, 'down': 40, 'left': 37, 'right': 39, 'space': 32,
    # 通用 / 鼠标
    'alt': 18, 'mouse': 255,
}

# 本地局的角色预设（3 个角色）
ROLE_KB = {'w': 87, 'a': 65, 's': 83, 'd': 68, 'q': 81}
ROLE_ARROWS = {'up': 38, 'down': 40, 'left': 37, 'right': 39, 'space': 32}

# 逐个探测按键类型并观察哪辆坦克响应。
# 返回 {playerId: role}，role 为 'keyboard'/'arrows'/'mouse' 之一。
ROLE_PROBE_JS = """(() => {
  const gc = GameManager.getGameController();
  if (!gc) return {err: 'no gc'};
  // 基线
  const read = () => {
    const ts = gc.getTanks();
    const out = {};
    for (const id in ts) {
      const t = ts[id];
      out[id] = [t.getForward(), t.getBack(), t.getLeft(), t.getRight()];
    }
    return out;
  };
  const before = read();
  // 通过 Phaser 的键盘分派事件，观察谁有反应
  const kb = GameManager.getGame().input.keyboard;
  const W = kb.addKey(Phaser.Keyboard.W), UP = kb.addKey(Phaser.Keyboard.UP);
  const fwdState = {kb: [], arrows: []};
  // 注意：这里是异步风格，立即返回，轮询由 Python 侧完成。
  return JSON.stringify({before, hasKeys: !!kb});
})()"""

STATE_JS = """(() => {
  const gc = GameManager.getGameController();
  if (!gc) return {err: 'no gc', tanks: {}};
  const ts = gc.getTanks ? gc.getTanks() : {};
  const r = {tanks: {}, roundActive: false, betweenRounds: false};
  if (gc.isRoundActive) r.roundActive = gc.isRoundActive();
  for (const id in ts) {
    const t = ts[id];
    let vx = 0, vy = 0;
    try {
      const b = t.getB2DBody ? t.getB2DBody() : null;
      if (b) { vx = b.GetLinearVelocity().x; vy = b.GetLinearVelocity().y; }
    } catch(e){}
    r.tanks[id] = {
      x: +(+t.getX()).toFixed(3),
      y: +(+t.getY()).toFixed(3),
      rot: +(+t.getRotation()).toFixed(3),
      vx: +(+vx).toFixed(3),
      vy: +(+vy).toFixed(3),
      alive: t.isAlive ? t.isAlive() : true,
    };
  }
  // 弹丸
  try {
    const rm = gc.getRoundModel ? gc.getRoundModel() : null;
    const ps = (rm && rm.getProjectiles) ? rm.getProjectiles() : (gc.getProjectiles ? gc.getProjectiles() : {});
    r.projectiles = [];
    for (const pid in ps) {
      const p = ps[pid];
      r.projectiles.push({
        id: pid,
        x: +(+p.getX()).toFixed(3),
        y: +(+p.getY()).toFixed(3),
        vx: +(+p.getSpeedX()).toFixed(3),
        vy: +(+p.getSpeedY()).toFixed(3),
        owner: (p.getPlayerId ? p.getPlayerId() : (p.playerId || '')),
      });
    }
  } catch (e) { r.projectiles = []; }
  return r;
})()"""


class TankCtl:
    def __init__(self, port=9222, role='keyboard'):
        from .cdp import CDP
        self.cdp = CDP(port=port)
        self.port = port
        self.role = role
        self.keymap = ROLE_KB if role == 'keyboard' else (ROLE_ARROWS if role == 'arrows' else ROLE_KB)
        # 每个坦克最近一次下发的输入：供"动作标签录制"用
        # （AI-B 训练需要 state+action 成对；见 docs/09-AI-B上游契约.md）
        self.last_inputs = {}

    def current_actions(self):
        """返回 {playerId: {fwd,back,left,right,fire}} —— 最近一次下发状态。

        录制时把它写进记录的顶层 `"a"` 字段即可得到带动作标签的数据集。
        未控制过的坦克不出现；已释放的输入是全 False（即 NOOP）。
        """
        return {pid: dict(v) for pid, v in self.last_inputs.items()}

    # ─── 连接 / 状态 ───
    async def connect(self, url='tanktrouble.com'):
        await self.cdp.connect_page(url, timeout=10)
        print(f'[ctl] connected to port {self.port} role={self.role}', flush=True)

    async def state(self):
        return await self.cdp.eval(STATE_JS)

    # ─── setInputState 驱动控制（在线安全）───
    # 在线多人模式中服务器拥有坦克坐标；直接调用 tank.setForward/setRotation 等
    # 会被服务器覆盖（回弹），开火被忽略。服务器唯一接受的路径是：
    #   InputState.withState(pid, fwd, back, left, right, fire) -> gc.setInputState
    # 这条链路会触发 emitTankState 把坦克状态发给服务器，因此移动/转向/开火
    # 都不回弹。在线模式请用这个方法。
    async def set_input(self, tank_id, fwd=False, back=False, left=False, right=False, fire=False, hold=0.0, freq=30):
        """通过 setInputState 下发输入。若 hold>0，则以 `freq` Hz 持续重发
        `hold` 秒后释放；否则只应用一次随即释放。"""
        def js(fwd, back, left, right, fire):
            f, ba, l, r, fi = ('true' if v else 'false' for v in (fwd, back, left, right, fire))
            return (
                f"(function(){{var gc=GameManager.getGameController()||GameManager.gameController;"
                f"gc.setInputState(InputState.withState('{tank_id}',{f},{ba},{l},{r},{fi}));return 1;}})()"
            )
        # 记录"当前生效的输入"，作为动作标签（AI-B 训练用）
        def _remember(fwd, back, left, right, fire):
            self.last_inputs[str(tank_id)] = {
                'fwd': bool(fwd), 'back': bool(back), 'left': bool(left),
                'right': bool(right), 'fire': bool(fire),
            }
        if hold <= 0:
            await self.cdp.eval(js(fwd, back, left, right, fire))
            _remember(fwd, back, left, right, fire)
            await self.cdp.eval(js(False, False, False, False, False))
            _remember(False, False, False, False, False)
            return
        end = asyncio.get_event_loop().time() + hold
        try:
            while asyncio.get_event_loop().time() < end:
                await self.cdp.eval(js(fwd, back, left, right, fire))
                _remember(fwd, back, left, right, fire)
                await asyncio.sleep(1.0 / freq)
        finally:
            # 必须保证释放：在线模式是服务器权威，一次 eval 抛异常（CDP 超时等）
            # 若跳过释放，服务器会一直保持"按住"状态 —— 坦克卡着动/一直开火。
            try:
                await self.cdp.eval(js(False, False, False, False, False))
                _remember(False, False, False, False, False)
            except Exception as e:
                print(f'[ctl] release input failed: {e}', flush=True)

    async def tanks(self):
        st = await self.state()
        return st.get('tanks', {}) if isinstance(st, dict) else {}

    async def my_tank(self, index=0):
        """返回本实例的坦克 id。默认取第一辆。"""
        ts = await self.tanks()
        ids = list(ts.keys())
        if not ids:
            return None
        return ids[index % len(ids)]

    # ─── 角色识别：读取游戏输入管理器的权威静态映射 ───
    # window.Inputs.__metaclass.inputManagers 持有游戏实时 InputManager 实例
    #（2 键盘 + 1 鼠标），每个都带 playerId 和按键绑定。这是游戏自身的权威
    # 绑定表 —— 无需探测/移动坦克，且回合切换、页面刷新后依然正确。
    # forwardKey=38（上方向键）= arrows 角色；forwardKey=87（W）= WASD。
    STATIC_ROLES_JS = """(() => {
      const out = {roles: {}};
      try {
        const ims = window.Inputs.__metaclass.inputManagers;
        for (let i=0;i<ims.length;i++) {
          const m = ims[i];
          const isK = m.__class === window.KeyboardInputManager;
          const isM = m.__class === window.MouseInputManager;
          if (!m.playerId) continue;
          if (isK) {
            out.roles[m.playerId] = (m.forwardKey === 38) ? 'arrows' : 'keyboard';
          } else if (isM) {
            out.roles[m.playerId] = 'mouse';
          }
        }
      } catch(e) { out.err = e.message; }
      return JSON.stringify(out);
    })()"""

    async def detect_roles(self):
        """权威静态角色识别（无需探测）。
        读取游戏实时 InputManager 实例。
        返回 {playerId: role}，role 为 'keyboard'/'arrows'/'mouse' 之一。"""
        roles = {}
        try:
            raw = await self.cdp.eval(self.STATIC_ROLES_JS)
            if raw:
                d = json.loads(raw) if isinstance(raw, str) else raw
                roles = d.get('roles', {}) or {}
        except Exception as e:
            print(f'[ctl] role detect failed: {e}', flush=True)
        return roles

    async def _probe_key(self, code, vk, baseline):
        """旧版行为按键探测（仅保留参考；请用 detect_roles 静态识别）。"""
        try:
            await self.cdp.send('Input.dispatchKeyEvent', {'type': 'keyDown', 'code': code, 'key': code.replace('Key', ''), 'windowsVirtualKeyCode': vk})
            await asyncio.sleep(0.15)
            now = json.loads(await self.cdp.eval("""(() => {
              const gc = GameManager.getGameController();
              const ts = gc.getTanks();
              const out = {};
              for (const id in ts) {
                const t = ts[id];
                out[id] = [t.getForward(), t.getBack(), t.getLeft(), t.getRight()];
              }
              return JSON.stringify(out);
            })()"""))
            await self.cdp.send('Input.dispatchKeyEvent', {'type': 'keyUp', 'code': code, 'key': code.replace('Key', ''), 'windowsVirtualKeyCode': vk})
            for tid in baseline:
                if now.get(tid) and baseline[tid] != now[tid]:
                    return tid
            return None
        except Exception:
            return None

    # ─── 高层基础行为（走 setInputState，在线安全）───
    # 遗留警告：旧的 `_tank_do` 路径直接调用 tank.setForward/setFireDown。
    # 在线模式下这些调用到不了服务器 -> 回弹 + 开火无效。下面所有方法
    # 一律改走 set_input()。
    async def move_forward(self, sec=0.5, tank_id=None):
        """前进 `sec` 秒（在线安全）。"""
        tid = tank_id or await self.my_tank()
        if tid:
            await self.set_input(tid, fwd=True, hold=sec, freq=30)

    async def move_back(self, sec=0.5, tank_id=None):
        """后退 `sec` 秒（在线安全）。"""
        tid = tank_id or await self.my_tank()
        if tid:
            await self.set_input(tid, back=True, hold=sec, freq=30)

    async def turn_left(self, sec=0.5, tank_id=None):
        """左转 `sec` 秒（减小 getRotation()，在线安全）。"""
        tid = tank_id or await self.my_tank()
        if tid:
            await self.set_input(tid, left=True, hold=sec, freq=30)

    async def turn_right(self, sec=0.5, tank_id=None):
        """右转 `sec` 秒（增大 getRotation()，在线安全）。"""
        tid = tank_id or await self.my_tank()
        if tid:
            await self.set_input(tid, right=True, hold=sec, freq=30)

    async def fire(self, sec=0.15, tank_id=None):
        """开火（默认快速点按 0.15s，在线安全）。"""
        tid = tank_id or await self.my_tank()
        if tid:
            await self.set_input(tid, fire=True, hold=sec, freq=30)

    async def stop_all(self, tank_id=None):
        """松开所有输入（停止移动/转向）。"""
        tid = tank_id or await self.my_tank()
        if tid:
            await self.set_input(tid)

    async def aim_at(self, x, y, tank_id=None, tol=0.05):
        """旋转坦克直到面向世界坐标 (x, y)。

        在线安全（使用 turn_left/right，即 setInputState）。当朝向与目标
        夹角进入 `tol` 弧度内即提前停止。坦克必须在场。
        返回 True 表示完成瞄准，False 表示出错/无坦克。
        """
        tid = tank_id or await self.my_tank()
        if not tid:
            return False
        try:
            for _ in range(100):  # 安全上限
                st = await self.tanks()
                t = st.get(tid)
                if not t:
                    return False
                cur = t['rot']
                dx, dy = x - t['x'], y - t['y']
                target = math.atan2(dy, dx)  # 游戏旋转角即 atan2(dy, dx)
                # 归一化到最短夹角 [-pi, pi]
                d = (target - cur + math.pi) % (2 * math.pi) - math.pi
                if abs(d) < tol:
                    await self.stop_all(tid)
                    return True
                # 实测约定：RIGHT 增大旋转值（游戏物理 + 实测验证）。
                # d>0 = 需要更大旋转值 = 右转。
                await (self.turn_right(0.08, tid) if d > 0 else self.turn_left(0.08, tid))
            await self.stop_all(tid)
            return True
        except Exception as e:
            print(f'[ctl] aim_at failed: {e}', flush=True)
            return False

    # ─── 遗留底层直接 setter（仅本地局，在线不安全）───
    # 仅保留作参考 / 本地调试。在线多人模式不要用：直接 setter 调用会被
    # 服务器覆盖（回弹）且开火被忽略。请优先用 set_input / move_forward /
    # turn_left / fire。
    async def _tank_do(self, tank_id, setter, value, sec):
        """在坦克上应用 setter 持续 `sec` 秒。每次重新获取对象以防空引用。"""
        val = 'true' if value else 'false'
        js = (
            f'(GameManager.getGameController()||GameManager.gameController)'
            f'.getTanks()["{tank_id}"] !== undefined && '
            f'(GameManager.getGameController()||GameManager.gameController)'
            f'.getTanks()["{tank_id}"].{setter}({val});'
        )
        await self.cdp.eval(js)
        await asyncio.sleep(sec)
        js_off = js.replace(f'.{setter}({val});', f'.{setter}(false);')
        await self.cdp.eval(js_off)

    # 向后兼容别名（同样在线安全）
    async def forward(self, sec=0.5, tank_id=None):
        """move_forward 的别名（在线安全）。"""
        await self.move_forward(sec, tank_id)

    async def back(self, sec=0.5, tank_id=None):
        """move_back 的别名（在线安全）。"""
        await self.move_back(sec, tank_id)

    # ─── 劫持控制：高频注入压过键盘/鼠标输入 ───
    async def hijack(self, tank_id, fwd=0, back=0, left=0, right=0, fire=0, duration=0.5, freq=50):
        """以高频重复应用控制位，压过键盘/鼠标输入。
        fwd/back/left/right/fire: 0 或 1（传 -1 表示不碰）。
        每 1/freq 秒重复应用一次，持续 `duration` 秒。
        已验证：~50Hz 时能压过游戏自身的输入事件。"""
        bits = {}
        if fwd >= 0: bits['setForward'] = bool(fwd)
        if back >= 0: bits['setBack'] = bool(back)
        if left >= 0: bits['setLeft'] = bool(left)
        if right >= 0: bits['setRight'] = bool(right)
        if fire >= 0: bits['setFireDown'] = bool(fire)
        if not bits:
            return
        js = self._hijack_js(tank_id, bits)
        end = asyncio.get_event_loop().time() + duration
        n = 0
        while asyncio.get_event_loop().time() < end:
            await self.cdp.eval(js)
            n += 1
            await asyncio.sleep(1.0 / freq)
        # 释放输入
        await self._release_hijack(tank_id, bits)

    def _hijack_js(self, tank_id, bits):
        parts = []
        for m, v in bits.items():
            parts.append(f't.{m}({"true" if v else "false"})')
        return (
            f'(function(){{var t=(GameManager.getGameController()||GameManager.gameController)'
            f'.getTanks()["{tank_id}"];if(!t)return false;'
            + ';'.join(parts) + ';return true;})()'
        )

    async def _release_hijack(self, tank_id, bits):
        # 关掉我们设过的位（但别强关鼠标用户正在控制的位）
        off = {m: False for m in bits if m != 'setFireDown'}
        js = self._hijack_js(tank_id, off)
        await self.cdp.eval(js)

    # ─── 验证辅助 ───
    async def diff(self, before, tank_id):
        """对比坦克当前状态与 `before` 字典。"""
        now = await self.tanks()
        if tank_id not in now or tank_id not in before:
            return None
        b, n = before[tank_id], now[tank_id]
        return {
            'dx': n['x'] - b['x'],
            'dy': n['y'] - b['y'],
            'drot': n['rot'] - b['rot'],
            'moved': abs(n['x']-b['x']) > 0.05 or abs(n['y']-b['y']) > 0.05,
            'rotated': abs(n['rot']-b['rot']) > 0.01,
        }
