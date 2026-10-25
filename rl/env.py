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
固定 dt 的 step/reset 环境（AI-B 上游契约，最小可用版）
======================================================
把"读状态 → 下发动作 → 推进固定时间 → 算奖励/终止"包成 Gym/PettingZoo 风格的
循环。**不依赖 gym/pettingzoo**（本机没有这些包），接口形状对齐即可：

    env = TankTroubleEnv(port='auto')          # 活游戏后端
    obs = await env.reset()                    # {'vector': [...], ...}
    out = await env.step(1)                    # 离散动作索引
    out = {'obs':..., 'reward':..., 'done':..., 'info': {...}}

设计
----
* **后端可注入**：`TankTroubleEnv(backend=...)`。默认是 `LiveBackend`
  （CDP + TankBehavior）；`FakeBackend` 用于离线单测，不需要浏览器。
* **固定 dt**：动作按 `hold=dt` 下发，循环用 `dt` 节拍对齐（补偿读取耗时），
  这样"一步 = 游戏内 dt 秒"，与真实帧率解耦。
* **奖励**：击杀 +10 / 死亡 -5 / 存活 +0.01 每步（本地局无 CRATE 事件，
  拾取奖励按"武器类型变化"近似，默认开启但权重小）。
* **done**：我方坦克从 `getTanks()` 消失（死亡）或回合不再 active。
  `auto_reset=True` 时 done 后自动进入下一回合。

⚠️ 在线多人模式：reset 不会开新局（服务器权威），只等待当前对局；本地局才会
由 `start_local_game.ensure_local_game()` 负责开局与角色绑定。
"""
import asyncio
import math
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
if os.path.join(BASE, 'cdp_tools') not in sys.path:
    sys.path.insert(0, os.path.join(BASE, 'cdp_tools'))

from rl.actions import action_to_inputs, bits_to_inputs, NUM_ACTIONS  # noqa: E402
from rl.obs import build_obs, obs_size                                # noqa: E402


# ─────────────────────────── 奖励配置 ───────────────────────────

class RewardConfig:
    def __init__(self, kill=10.0, death=-5.0, survive=0.01,
                 weapon_pickup=1.0, time_penalty=0.0):
        self.kill = kill
        self.death = death
        self.survive = survive
        self.weapon_pickup = weapon_pickup
        self.time_penalty = time_penalty


# ─────────────────────────── 后端 ───────────────────────────

class FakeBackend:
    """离线测试后端：按脚本返回状态序列，记录收到的动作。"""

    def __init__(self, states, pid='p1', dt=0.05):
        self.states = list(states)
        self.pid = pid
        self.dt = dt
        self.i = 0
        self.terrain = None
        self.applied = []          # [(pid, inputs, hold)]
        self.resets = 0

    async def ensure_round(self):
        self.resets += 1
        self.i = 0

    async def read_state(self):
        idx = min(self.i, len(self.states) - 1)
        return self.states[idx]

    async def read_terrain(self):
        return self.terrain

    async def read_velocities(self):
        return {}

    async def apply_input(self, pid, inputs, hold):
        self.applied.append((pid, dict(inputs), hold))
        self.i += 1                # 一步 = 一帧状态推进

    def advance(self, dt):
        self.i += 1


class LiveBackend:
    """真实游戏后端：CDP（可自动发现 ego 浏览器端口）+ TankBehavior 控制。"""

    VEL_JS = r"""
    (() => { const gc = GameManager.getGameController() || GameManager.gameController;
      const ts = gc.getTanks() || {}; const out = {};
      for (const id in ts) {
        try { const b = ts[id].getB2DBody(); const v = b.GetLinearVelocity();
              out[id] = [v.x, v.y]; } catch (e) {}
      }
      return out; })()
    """

    def __init__(self, port='auto', players=2, bind=None, role_prefer='arrows',
                 auto_start=True):
        self.port = port
        self.players = players
        self.bind = bind
        self.role_prefer = role_prefer
        self.auto_start = auto_start
        self.bhv = None
        self.pid = None
        self._state_js = None

    def _load_state_js(self):
        if self._state_js:
            return self._state_js
        path = os.path.join(BASE, 'monitor_server.py')
        src = open(path, encoding='utf-8').read()
        m = re.search(r'STATE_JS = r"""(.*?)"""', src, re.S)
        if not m:
            raise RuntimeError('monitor_server.STATE_JS 未找到（上游契约变更？）')
        self._state_js = m.group(1)
        return self._state_js

    async def connect(self):
        if self.bhv is not None:
            return
        sys.path.insert(0, os.path.join(BASE, 'scripts'))
        from tank_behavior_template import TankBehavior
        from ego_browser import resolve_port
        port = resolve_port(self.port) if not isinstance(self.port, int) else self.port
        self.port = port
        self.bhv = TankBehavior(port=port)
        await self.bhv.connect()
        self._load_state_js()

    async def ensure_round(self):
        await self.connect()
        if self.auto_start:
            sys.path.insert(0, os.path.join(BASE, 'scripts'))
            from start_local_game import ensure_local_game
            info = await ensure_local_game(self.port, players=self.players,
                                           bind=self.bind,
                                           log=lambda *a, **k: None)
            self.pid = info['roles'] and self._pick(info)
        if not self.pid:
            self.pid = await self.bhv.pick_role_strict(self.role_prefer) \
                or await self.bhv.pick_role()
        # 等回合真正开始
        for _ in range(120):
            st = await self.read_state()
            if isinstance(st, dict) and st.get('roundActive') and st.get('tanks'):
                return
            await asyncio.sleep(0.25)

    def _pick(self, info):
        """从角色绑定结果里选我方 pid（默认优先方向键角色）。"""
        roles = info.get('roles') or {}
        for want in (self.role_prefer, 'keyboard', 'mouse'):
            for pid, r in roles.items():
                if r == want:
                    return pid
        return next(iter(roles), None)

    async def read_state(self):
        await self.connect()
        st = await self.bhv.ctl.cdp.eval(self._load_state_js())
        if isinstance(st, dict) and st.get('tanks'):
            try:
                vel = await self.bhv.ctl.cdp.eval(self.VEL_JS)
                if isinstance(vel, dict):
                    for pid, v in vel.items():
                        if pid in st['tanks'] and isinstance(v, list) and len(v) == 2:
                            st['tanks'][pid]['vx'], st['tanks'][pid]['vy'] = v
            except Exception:
                pass
        return st

    async def read_terrain(self):
        await self.connect()
        try:
            return await self.bhv.get_terrain()
        except Exception:
            return None

    async def read_velocities(self):
        return {}

    async def apply_input(self, pid, inputs, hold):
        await self.connect()
        await self.bhv.set_input(pid, fwd=inputs.get('fwd', False),
                                 back=inputs.get('back', False),
                                 left=inputs.get('left', False),
                                 right=inputs.get('right', False),
                                 fire=inputs.get('fire', False), hold=hold)

    def advance(self, dt):
        time.sleep(0)     # 真实时间由 step() 的节拍控制


# ─────────────────────────── 环境 ───────────────────────────

class TankTroubleEnv:
    def __init__(self, port='auto', backend=None, dt=0.05, max_steps=2000,
                 std_dt=True, reward=None, auto_reset=False, grid_radius=3,
                 players=2, bind=None, terrain_every=0):
        self.backend = backend or LiveBackend(port=port, players=players, bind=bind)
        self.dt = float(dt)
        self.std_dt = std_dt
        self.max_steps = int(max_steps)
        self.reward_cfg = reward or RewardConfig()
        self.auto_reset = auto_reset
        self.grid_radius = grid_radius
        self.terrain_every = int(terrain_every)
        self.pid = None
        self.terrain = None
        self.steps = 0
        self.episode = 0
        self.last_state = None      # 最近一帧原始快照（录制用）
        self._prev = {}            # 上一帧统计（击杀/胜利/武器/存活）
        self.meta = {}

    # ——— 生命周期 ———
    async def reset(self):
        await self.backend.ensure_round()
        self.pid = getattr(self.backend, 'pid', None) or self.pid
        self.terrain = await self.backend.read_terrain()
        state = await self.backend.read_state()
        self.steps = 0
        self.episode += 1
        self.last_state = state
        self._prev = self._stats(state)
        self.meta = {'pid': str(self.pid), 'episode': self.episode,
                     'obs_size': obs_size(self.grid_radius)}
        return self._obs(state)

    async def close(self):
        bhv = getattr(self.backend, 'bhv', None)
        if bhv is not None and getattr(bhv, 'ctl', None) is not None:
            try:
                await bhv.ctl.cdp.close()
            except Exception:
                pass

    # ——— 一步 ———
    async def step(self, action):
        t0 = time.perf_counter()
        inputs = action_to_inputs(action) if isinstance(action, int) else dict(action)
        await self.backend.apply_input(self.pid, inputs, self.dt)
        if self.std_dt:
            rest = self.dt - (time.perf_counter() - t0)
            if rest > 0:
                await asyncio.sleep(rest)

        self.steps += 1
        state = await self.backend.read_state()
        self.last_state = state
        if self.terrain_every and self.steps % self.terrain_every == 0:
            self.terrain = await self.backend.read_terrain()
        obs = self._obs(state)
        cur = self._stats(state)
        reward, done, info = self._transition(self._prev, cur, state)
        self._prev = cur
        if self.steps >= self.max_steps:
            done = True
            info['truncated'] = True
        if done and self.auto_reset:
            obs = await self.reset()
            info['auto_reset'] = True
        return {'obs': obs, 'reward': reward, 'done': done, 'info': info}

    # ——— 观测 / 统计 ———
    def _obs(self, state):
        return build_obs(state, self.pid, terrain=self.terrain,
                         grid_radius=self.grid_radius)

    def _stats(self, state):
        state = state if isinstance(state, dict) else {}
        tanks = state.get('tanks') or {}
        me = tanks.get(str(self.pid)) or {}
        sk = state.get('scoresKill') or {}
        sv = state.get('scoresVict') or {}
        return {
            'alive': str(self.pid) in tanks,
            'round': bool(state.get('roundActive')),
            'kills': float(sk.get(str(self.pid), 0) or 0),
            'vict': float(sv.get(str(self.pid), 0) or 0),
            'weapon': float(me.get('weapon', -1) or -1),
            'pos': (me.get('x'), me.get('y')) if me else None,
        }

    def _transition(self, prev, cur, state):
        r = 0.0
        done = False
        events = []
        if cur['kills'] > prev['kills'] or cur['vict'] > prev['vict']:
            r += self.reward_cfg.kill
            done = True
            events.append('kill')
        if prev['alive'] and not cur['alive']:
            r += self.reward_cfg.death
            done = True
            events.append('death')
        # 存活奖励只加在"非终止步"上：击杀/死亡那一步就不要再叠 +0.01，
        # 否则终局信号被稀释（测试也按纯 +10/-5 校验）。
        if not done and prev['alive'] and cur['alive'] and cur['round']:
            r += self.reward_cfg.survive
        if prev['weapon'] != cur['weapon'] and cur['weapon'] >= 0:
            r += self.reward_cfg.weapon_pickup
            events.append('weapon')
        if prev['round'] and not cur['round']:
            done = True
            events.append('round_end')
        r -= self.reward_cfg.time_penalty
        info = {'events': events, 'alive': cur['alive'], 'round': cur['round'],
                'pos': cur['pos'], 'weapon': cur['weapon'],
                'kills': cur['kills'], 'vict': cur['vict'],
                'step': self.steps}
        return r, done, info
