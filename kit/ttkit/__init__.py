"""
ttkit —— TankTrouble 角色信息获取 / 操作封装包（黑盒 API **v2**）
================================================================
把"读取游戏状态"和"操作角色"封装成稳定、带文档的方法。所有控制都走游戏自己的
`setInputState`（与人类按键等价，在线/本地都安全）。

v2 相比 v1 的关键改进（来自两轮黑盒测试的反馈）：
  * `turn_to_angle()` 变成**闭环**：迭代转到目标角，返回**实测角/残差**（不再只回 bool）；
  * `aim_and_fire()` 先转向，再在**实际朝向**上复核弹道（含 ±鲁棒性检查），通过才开火；
  * `scan()` 增加 `robust`（±robust_deg 邻域仍命中）与 `quality`（越小越可靠）字段；
  * 射程按**当前武器**自动取（基础弹 180m、加特林 54m、霰弹 64m、激光 144m、双管 108m）；
  * `raycast()` 增加 `hit` 布尔；未命中时 `traveled` 截断到射程，不再虚高到 1.5×射程；
  * 所有控制方法统一校验"必须是自己可控的角色"；
  * `my_pid()` 找不到指定角色时**抛 RuntimeError**（不再静默回退）；
  * `rotation()` 返回**原始累积 rot**（另给 `rotation_norm()`）；
  * `close()` 关闭两条 CDP 连接（v1 会泄漏一条）；
  * 补齐地形/寻路/武器/弹丸/停稳查询 API。

快速开始
--------
    import asyncio
    from ttkit import Kit

    async def main():
        kit = await Kit().connect()
        pid = await kit.my_pid()                       # 默认 arrows 角色
        print(await kit.snapshot())
        r = await kit.turn_to_angle(pid, 1.2)          # 世界方向角（弧度）
        print(r)                                       # {'ok','actual_deg','err_deg','iters'}
        await kit.close()

    asyncio.run(main())

坐标/角度约定
-------------
* 世界坐标单位：米（1 格 = 10 米）。
* "炮口朝向"用**世界方向角**（弧度，0=+X，逆时针）；游戏内部 `rot = 角度 + π/2`。
* `rotation(pid)` = 原始累积 rot（可超出 ±π）；`rotation_norm(pid)` = 归一化值。

自伤标签（deadlyToOwner）
------------------------
弹丸**第一次撞墙/撞盾后**才变成"可伤发射者"；在那之前对发射者无害（会穿透）。
游戏内实测：第一次反弹瞬间 `deadlyToOwner` False→True。
因此 `scan()` 里 `self_hit` 只在 `bounces >= 1` 时才算；0 反弹命中自己记
`self_hit_harmless=True`。

可靠性提示（务必读）
--------------------
1. 转向精度约 ±1~2°（输入按物理帧量化）。**1° 的角度差就可能让官方弹道拓扑突变**
   （命中距离/反弹数完全改变），所以扫描到的角度必须用**实际朝向**复核后再开火。
2. 官方零宽射线推演与实际弹丸（半径 0.25m）在**远距/多反弹**路径上会累积偏差
   （实测 6.5s 后横向偏差 7m+）。**短距（<60m）、少反弹（≤4）最可靠**。
3. `scan()` 返回的 `robust`（±1.5° 邻域仍命中）与 `quality`（越小越好）可用来挑稳的解。
"""
import asyncio
import json
import math
import os
import time

from .cdp import CDP                      # noqa: F401
from .tankctl import TankCtl
from .behavior import TankBehavior
from .ballistics import BULLET_SPEED, BULLET_LIFETIME, TANK_RADIUS
from .tank_constants import (TANK_FORWARD_SPEED, TANK_BACK_SPEED, TANK_ROTATION_SPEED)
from . import gameutils as _gu

_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'config.json')

__all__ = ['Kit', 'BULLET_SPEED', 'BULLET_LIFETIME', 'TANK_RADIUS',
           'TANK_FORWARD_SPEED', 'TANK_BACK_SPEED', 'TANK_ROTATION_SPEED',
           'WEAPON_NAMES', 'MUZZLE_OFFSET', 'WEAPON_RANGE']

WEAPON_NAMES = {-1: 'BULLET', 0: 'LASER', 1: 'DOUBLE_BARREL', 2: 'SHOTGUN',
                3: 'HOMING_MISSILE', 4: 'MINE', 5: 'GATLING_GUN'}
# 炮口偏移（米）：真实弹丸从炮口出膛，不是车体中心
MUZZLE_OFFSET = {-1: 2.50, 0: 2.50, 1: 2.25, 2: 2.45, 3: 2.50, 4: 1.45, 5: 3.00}
# 射程（米）= 弹速 × 寿命（游戏内实测）
WEAPON_RANGE = {-1: 180.0,   # 18 × 10
                0: 144.0,    # 180 × 0.8
                1: 108.0,    # 18 × 6（双管 lifetime=6）
                2: 64.0,     # 32 × 2
                3: 180.0,    # 18 × 10（追踪弹）
                4: 0.0,      # 地雷（部署型）
                5: 54.0}     # 27 × 2

MAX_PATH_BOUNCES = 100       # 官方路径算法安全上限（**不是游戏规则**：反弹无实际上限）


class Kit:
    """角色信息获取 + 操作 门面。所有方法均为 async。"""

    def __init__(self, port=None):
        self.port = port
        self.ctl = None
        self.bhv = None
        self.gu = _gu                  # 地形/迷宫工具模块（进阶）
        self._roles_cache = {}
        self._roles_ts = 0.0

    # ────────────────────────── 连接 ──────────────────────────
    async def connect(self, port=None):
        if port is None:
            port = self.port
        if port is None:
            try:
                with open(_DEFAULT_CONFIG, encoding='utf-8') as fh:
                    port = json.load(fh).get('port')
            except Exception:
                port = None
        if port is None:
            port = 62202
        self.port = int(port)
        self.ctl = TankCtl(port=self.port)
        await self.ctl.connect()
        self.bhv = TankBehavior(port=self.port)
        await self.bhv.connect()
        self._roles_ts = 0.0
        return self

    async def close(self):
        """释放连接（**两条** CDP 连接都关）。"""
        for obj in (getattr(self.bhv, 'ctl', None), self.ctl):
            try:
                cdp = getattr(obj, 'cdp', None)
                if cdp is not None:
                    await cdp.close()
            except Exception:
                pass
        self.bhv = None
        self.ctl = None

    # ────────────────────────── 信息（只读） ──────────────────────────
    async def roles(self, refresh=False):
        """可控角色：``{pid: role}``（role ∈ arrows/keyboard/mouse）。"""
        now = time.time()
        if refresh or not self._roles_cache or now - self._roles_ts > 2.0:
            self._roles_cache = await self.bhv.get_roles()
            self._roles_ts = now
        return dict(self._roles_cache)

    async def my_pid(self, role='arrows'):
        """选出"自己的角色"（默认 arrows）。找不到该角色时**抛 RuntimeError**。"""
        rs = await self.roles()
        for pid, r in rs.items():
            if r == role:
                return pid
        if role is None and rs:
            return sorted(rs)[0]
        raise RuntimeError('没有 role=%r 的可控角色（当前可控=%r）' % (role, rs))

    async def _require(self, pid):
        """校验 pid 是本 AI 的可控角色，否则抛 ValueError。"""
        if not isinstance(pid, str) or not pid:
            raise ValueError('pid 必须是非空字符串（用 roles()/my_pid() 获取）')
        rs = await self.roles()
        if str(pid) not in rs:
            raise ValueError('拒绝操作：pid=%s 不是可控角色（可控=%s）' % (pid, list(rs.keys())))

    async def snapshot(self):
        """局面快照：
        ``{round_state, controllable, tanks: {pid: {x,y,rot,alive,weapon}}}``"""
        out = {'round_state': await self.bhv.get_round_state(),
               'controllable': await self.roles(), 'tanks': {}}
        for pid in await self._all_pids():
            pos = await self.bhv.get_position(pid)
            if pos is None:
                continue
            out['tanks'][pid] = {
                'x': pos[0], 'y': pos[1],
                'rot': await self.rotation(pid),
                'alive': await self.bhv.is_alive(pid),
                'weapon': await self.weapon(pid),
            }
        return out

    async def _all_pids(self):
        try:
            return list((await self.ctl.tanks()).keys())
        except Exception:
            return []

    async def round_state(self):
        """回合状态：``in round`` / ``counting down`` / ``between rounds`` / ..."""
        return await self.bhv.get_round_state()

    async def position(self, pid):
        """世界坐标 (x, y)；不在场返回 None。"""
        return await self.bhv.get_position(pid)

    async def rotation(self, pid):
        """**原始累积 rot**（弧度，可能超出 ±π；游戏 getRotation 的累加值）。"""
        try:
            raw = await self.bhv._raw_rot(pid)
            if raw is not None:
                return raw
        except Exception:
            pass
        return await self.bhv.get_rotation(pid)

    async def rotation_norm(self, pid):
        """归一化到 [-π, π) 的 rot。"""
        r = await self.rotation(pid)
        if r is None:
            return None
        return (r + math.pi) % (2 * math.pi) - math.pi

    async def alive(self, pid):
        return await self.bhv.is_alive(pid)

    async def opponents(self, my_pid):
        """除自己外的所有坦克 pid。"""
        return [p for p in await self._all_pids() if str(p) != str(my_pid)]

    async def nearest_opponent(self, my_pid):
        """最近的其他坦克 → ``(pid, (x,y), distance)`` 或 None。"""
        me = await self.position(my_pid)
        if me is None:
            return None
        best = None
        for pid in await self.opponents(my_pid):
            pos = await self.position(pid)
            if pos is None:
                continue
            d = math.hypot(pos[0] - me[0], pos[1] - me[1])
            if best is None or d < best[2]:
                best = (pid, pos, d)
        return best

    async def weapon(self, pid):
        """当前武器类型（-1 基础弹 / 0 激光 / 1 双管 / 2 霰弹 / 3 追踪 / 4 地雷 / 5 加特林）。"""
        try:
            return await self.bhv.get_active_weapon(pid)
        except Exception:
            return None

    def range_of(self, weapon):
        """该武器射程（米）= 弹速 × 寿命；未知按基础弹 180m。"""
        try:
            return WEAPON_RANGE.get(int(weapon), 180.0)
        except Exception:
            return 180.0

    async def bullet_range(self, pid):
        """当前武器射程（米）。"""
        return self.range_of(await self.weapon(pid))

    async def terrain(self):
        """完整地形摘要（tiles / walls / zones）。"""
        return await _gu.get_terrain(self.ctl)

    async def open_tiles(self, ctl=None):
        """所有可通行格的**中心世界坐标**列表 [(x,y), ...]。"""
        terr = await self.terrain()
        out = []
        tiles = (terr or {}).get('tiles') or []
        for gy in range(len(tiles)):
            for gx in range(len(tiles[gy])):
                if tiles[gy][gx] and tiles[gy][gx][0]:
                    out.append((gx * 10.0 + 5.0, gy * 10.0 + 5.0))
        return out

    async def plan_path(self, start_xy, end_xy):
        """迷宫寻路：返回格子序列 ``[(gx,gy), ...]``（无路返回 []）。"""
        return await _gu.plan_maze_path(self.ctl, tuple(start_xy), tuple(end_xy))

    async def maze_distance(self, from_xy, to_xy):
        """两点迷宫距离（格数）或 None。"""
        return await _gu.maze_distance(self.ctl, tuple(from_xy), tuple(to_xy))

    async def projectiles(self):
        """场上弹丸：``[{id, playerId, type, x, y, vx, vy, timeAlive, deadlyToOwner}]``。"""
        js = """(() => { const gc = GameManager.getGameController();
          if (!gc) return '[]';
          const ps = gc.roundController.model.getProjectiles(); const out = [];
          for (const id in ps) { const p = ps[id];
            out.push({id: id, playerId: p.getPlayerId ? p.getPlayerId() : null,
              type: p.getType ? p.getType() : null,
              x: p.getX(), y: p.getY(), vx: p.getSpeedX ? p.getSpeedX() : null,
              vy: p.getSpeedY ? p.getSpeedY() : null,
              timeAlive: p.timeAlive, deadlyToOwner: !!p.deadlyToOwner}); }
          return JSON.stringify(out); })()"""
        try:
            return json.loads(await self.ctl.cdp.eval(js))
        except Exception:
            return []

    # ────────────────────────── 操作（只允许自己的角色） ──────────────────────────
    async def hold(self, pid, seconds, fwd=False, back=False, left=False, right=False, fire=False):
        """按住输入 ``seconds`` 秒（内部 30Hz 重发，结束自动松开）。
        在线输入上报粒度 0.5s，重要动作建议按住 ≥0.6s。"""
        await self._require(pid)
        await self.ctl.set_input(pid, fwd=fwd, back=back, left=left, right=right,
                                 fire=fire, hold=seconds)

    async def stop(self, pid):
        """停止移动/转动/开火，并等物理速度衰减（稳）。"""
        await self._require(pid)
        await self.ctl.set_input(pid, hold=0.12)
        try:
            await self.bhv.wait_still(pid, max_wait=0.8)
        except Exception:
            await asyncio.sleep(0.08)

    async def move_forward(self, pid, seconds=0.4):
        """前进 ``seconds`` 秒（实测 15.95 m/s）。"""
        await self.hold(pid, seconds, fwd=True)

    async def move_back(self, pid, seconds=0.4):
        """后退 ``seconds`` 秒（实测 12.8 m/s）。"""
        await self.hold(pid, seconds, back=True)

    async def turn_to_rot(self, pid, rot, tol=0.03):
        """按**游戏内部 rot** 转向（进阶；一般用 turn_to_angle）。"""
        await self._require(pid)
        return await self.bhv.turn_to_rot(pid, float(rot), tol=tol)

    async def turn_to_angle(self, pid, world_angle_rad, tol_deg=1.2, max_iters=None):
        """闭环转向到**世界方向角**（弧度）。

        内部委托 `behavior.turn_to_rot`（高频小脉冲 + 逐帧读原始 rot，容差默认 1.2°），
        返回 ``{'ok', 'actual_deg', 'err_deg', 'iters'}``。

        * 转向输入按物理帧量化（单次脉冲约 4.8°），``tol_deg`` 建议 1.0~2.0；
          设太小（<1°）可能来回振荡。
        * ``actual_deg`` 是**实测**炮口世界角（供开火前复核用）。
        """
        await self._require(pid)
        target = float(world_angle_rad) + math.pi / 2
        tol = math.radians(float(tol_deg))
        ok = False
        try:
            ok = await self.bhv.turn_to_rot(pid, target, tol=tol)
        except Exception:
            ok = False
        cur = await self.rotation(pid)
        if cur is None:
            return {'ok': False, 'actual_deg': None, 'err_deg': None, 'iters': None}
        err = (target - cur + math.pi) % (2 * math.pi) - math.pi
        return {'ok': bool(ok) or abs(err) <= tol + math.radians(0.8),
                'actual_deg': math.degrees(cur - math.pi / 2),
                'err_deg': math.degrees(-err), 'iters': None}

    async def navigate_to(self, pid, target_xy, arrival=1.5, timeout=None):
        """迷宫寻路移动到 target_xy=(x,y)（米）。
        ``timeout`` 秒内未到达则取消（内部会正确松键）。返回是否到达。"""
        await self._require(pid)
        coro = self.bhv.navigate_to(pid, tuple(target_xy), arrival=arrival)
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=float(timeout))
        except asyncio.TimeoutError:
            return False

    async def fire(self, pid, seconds=0.6):
        """开一枪（按住 fire ``seconds`` 秒）。"""
        await self.hold(pid, seconds, fire=True)

    # ────────────────────────── 弹道 / 扫描 / 火控 ──────────────────────────
    async def raycast(self, pid, world_angle_rad, max_traveled=None, max_bounces=None):
        """沿世界方向角推演**官方弹道**（B2DUtils，从炮口起算）。

        返回 ``{angle_rad, path, bounces, traveled, hit, hit_pid, self_hit,
        self_hit_harmless, reachable}``：
        * ``hit``：是否命中某辆坦克（且射程内）
        * ``hit_pid``：第一个被命中的坦克（无则 None）
        * ``self_hit``：命中自己且 bounces≥1（deadlyToOwner 已翻转 → 真自伤）
        * ``self_hit_harmless``：0 反弹命中自己（会穿透，无害）
        * ``reachable``：traveled ≤ 射程；未命中时 ``traveled`` 截断到射程
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return None
        off = float(MUZZLE_OFFSET.get(int(await self.weapon(pid)), 2.50))
        if max_traveled is None:
            max_traveled = await self.bullet_range(pid)
        cap = MAX_PATH_BOUNCES if max_bounces is None else int(max_bounces)
        d = await self.bhv._raycast_path(
            pos[0], pos[1], math.degrees(float(world_angle_rad)), 0.0, 0.0,
            max_bounces=max(1, cap), max_dist=float(max_traveled) * 1.5,
            target_tank_id=None, origin_offset=off)
        return self._mk_ray(pid, world_angle_rad, d, float(max_traveled))

    @staticmethod
    def _mk_ray(pid, angle_rad, d, max_traveled):
        nb = int(d.get('bounces') or 0)
        traveled = float(d.get('traveled') or 0)
        reach = traveled <= max_traveled
        raw_hit = d.get('hitTank')
        hit = bool(raw_hit) and reach
        self_pid = str(raw_hit) == str(pid)
        return {'angle_rad': float(angle_rad),
                'path': d.get('path') or [],
                'bounces': nb,
                'traveled': traveled if reach else max_traveled,
                'hit': bool(hit),
                'hit_pid': raw_hit if hit else None,
                'self_hit': bool(hit and self_pid and nb >= 1),
                'self_hit_harmless': bool(hit and self_pid and nb == 0),
                'reachable': reach}

    async def scan(self, pid, step_deg=30, max_traveled=None, robust_deg=None):
        """**静默扫描 360°**：每个方向角取官方弹道第一个命中的坦克。

        ``robust_deg``：给出时（如 1.5），对"命中对手"的候选额外检查 ±robust_deg
        是否仍命中同一对手（转向误差 ±1~2°，只有鲁棒解可靠）。

        返回每项::

            {'angle_rad','hit_pid','self_hit','self_hit_harmless',
             'bounces','traveled','reachable','robust','quality'}

        ``quality`` 越小越好（= traveled + 30×bounces；短距/少反弹最可靠）。
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return []
        off = float(MUZZLE_OFFSET.get(int(await self.weapon(pid)), 2.50))
        if max_traveled is None:
            max_traveled = await self.bullet_range(pid)
        out = []
        for deg in range(0, 360, max(1, int(step_deg))):
            d = await self.bhv._raycast_path(
                pos[0], pos[1], float(deg), 0.0, 0.0,
                max_bounces=MAX_PATH_BOUNCES, max_dist=float(max_traveled) * 1.5,
                target_tank_id=None, origin_offset=off)
            item = self._mk_ray(pid, math.radians(deg), d, float(max_traveled))
            item['robust'] = None
            if robust_deg and item['hit_pid'] and not item['self_hit']:
                ok = True
                for delta in (float(robust_deg), -float(robust_deg)):
                    d2 = await self.bhv._raycast_path(
                        pos[0], pos[1], float(deg) + delta, 0.0, 0.0,
                        max_bounces=MAX_PATH_BOUNCES, max_dist=float(max_traveled) * 1.5,
                        target_tank_id=None, origin_offset=off)
                    it2 = self._mk_ray(pid, math.radians(deg + delta), d2, float(max_traveled))
                    if not (it2['hit_pid'] and str(it2['hit_pid']) == str(item['hit_pid'])
                            and not it2['self_hit']):
                        ok = False
                        break
                item['robust'] = ok
            item['quality'] = float(item['traveled']) + 30.0 * float(item['bounces'])
            out.append(item)
        return out

    async def find_aim(self, pid, target_xy, target_id=None, require_bounce=False,
                       max_traveled=None, max_bounces=None):
        """找能命中目标的**世界方向角**（官方弹道，自动排除先打自己的解）。"""
        await self._require(pid)
        off = float(MUZZLE_OFFSET.get(int(await self.weapon(pid)), 2.50))
        if max_traveled is None:
            max_traveled = await self.bullet_range(pid)
        cap = MAX_PATH_BOUNCES if max_bounces is None else int(max_bounces)
        return await self.bhv.find_aim_angle_raycast(
            pid, tuple(target_xy), target_id=target_id, require_bounce=require_bounce,
            max_traveled=float(max_traveled), max_bounces=cap, muzzle_offset=off)

    async def aim_and_fire(self, pid, target_xy, target_id=None, fire_sec=0.6,
                           tol_deg=1.2, robust_deg=1.5, max_attempts=3):
        """转向 + **在实际朝向上复核弹道**（含鲁棒性检查），通过才开火。

        返回 ``{'fired','reason','angle_deg','verify','attempt'}``。
        ``reason``: ``no_aim`` / ``no_pose`` / ``verify_failed`` / None(成功)。
        """
        await self._require(pid)
        for attempt in range(1, int(max_attempts) + 1):
            ang = await self.find_aim(pid, target_xy, target_id=target_id)
            if ang is None:
                return {'fired': False, 'reason': 'no_aim', 'angle_deg': None,
                        'verify': None, 'attempt': attempt}
            await self.turn_to_angle(pid, ang, tol_deg=tol_deg)
            cur = await self.rotation(pid)
            if cur is None:
                return {'fired': False, 'reason': 'no_pose', 'angle_deg': None,
                        'verify': None, 'attempt': attempt}
            actual = cur - math.pi / 2
            v = await self.raycast(pid, actual)
            ok = bool(v and v.get('hit') and str(v.get('hit_pid')) == str(target_id)
                      and not v.get('self_hit'))
            if ok and robust_deg:
                for delta in (float(robust_deg), -float(robust_deg)):
                    s = await self.raycast(pid, actual + math.radians(delta))
                    if not (s and s.get('hit') and str(s.get('hit_pid')) == str(target_id)
                            and not s.get('self_hit')):
                        ok = False
                        break
            if not ok:
                continue                     # 复核失败 → 重新找角/换解
            await self.fire(pid, fire_sec)
            return {'fired': True, 'reason': None, 'angle_deg': math.degrees(actual),
                    'verify': v, 'attempt': attempt}
        return {'fired': False, 'reason': 'verify_failed', 'angle_deg': None,
                'verify': None, 'attempt': int(max_attempts)}
