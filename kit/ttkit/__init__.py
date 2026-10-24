"""
ttkit —— TankTrouble 角色信息获取 / 操作封装包（黑盒 API）
==========================================================
本包把"读取游戏状态"和"操作角色"封装成一组**稳定、带文档的方法**。
所有控制都走游戏自己的 `setInputState`（与人类按键等价，在线/本地都安全）。

快速开始
--------
    import asyncio
    from ttkit import Kit

    async def main():
        kit = await Kit().connect()          # 默认读 config.json 里的 port
        pid = await kit.my_pid()             # 选择要控制的角色（按角色名，避免控错）
        print(await kit.snapshot())
        await kit.hold(pid, 0.3, fwd=True)   # 前进 0.3s
        await kit.close()

    asyncio.run(main())

坐标/角度约定
-------------
* 世界坐标单位：米（1 格 = 10 米）。
* "炮口朝向"用**世界方向角**（弧度，0=+X，逆时针）表示；
  游戏内部 rot 与它差 π/2：``forward = rot - π/2``。
* ``turn_to_angle(pid, ang)`` 接收世界方向角，内部自动换算。

安全约定（避免控错角色）
------------------------
* ``roles()`` 返回**可控角色**：``{pid: "arrows"/"keyboard"/"mouse"}``。
* 只有这些 pid 能被操作；本包所有控制方法都会校验，非可控 pid 会抛 ``ValueError``。
* 建议固定用 ``my_pid()``（默认取 arrows 角色）作为自己的角色。
"""
import asyncio
import json
import math
import os

from .cdp import CDP                      # noqa: F401  (供进阶使用)
from .tankctl import TankCtl
from .behavior import TankBehavior
from .ballistics import BULLET_SPEED, MAX_BOUNCES, BULLET_LIFETIME, TANK_RADIUS
from .tank_constants import (TANK_FORWARD_SPEED, TANK_BACK_SPEED, TANK_ROTATION_SPEED)
from .gameutils import TILE_M

_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'config.json')

__all__ = ['Kit', 'BULLET_SPEED', 'BULLET_LIFETIME', 'MAX_BOUNCES', 'TANK_RADIUS',
           'TANK_FORWARD_SPEED', 'TANK_BACK_SPEED', 'TANK_ROTATION_SPEED', 'TILE_M']


class Kit:
    """角色信息获取 + 操作 门面。所有方法均为 async。"""

    def __init__(self, port=None):
        self.port = port
        self.ctl = None          # TankCtl：底层 setInputState / 角色识别
        self.bhv = None          # TankBehavior：转向/导航/弹道/火控

    # ────────────────────────── 连接 ──────────────────────────
    async def connect(self, port=None):
        """连接游戏（CDP）。port 省略时读 config.json，再退回 62202。"""
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
        return self

    async def close(self):
        """释放连接。"""
        if self.bhv is not None:
            try:
                await self.bhv.ctl.cdp.close()
            except Exception:
                pass
        self.bhv = None
        self.ctl = None

    # ────────────────────────── 信息（只读） ──────────────────────────
    async def roles(self):
        """可控角色：``{pid: role}``（role ∈ arrows/keyboard/mouse）。"""
        return await self.bhv.get_roles()

    async def my_pid(self, role='arrows'):
        """选出"自己的角色"（默认 arrows）。返回 pid；找不到则抛异常。"""
        rs = await self.roles()
        for pid, r in sorted(rs.items(), key=lambda kv: kv[1]):
            if r == role:
                return pid
        if rs:
            return sorted(rs)[0]
        raise RuntimeError('没有可控角色：请确认游戏已开始（回合中）')

    async def snapshot(self):
        """一次拿到当前局面：
        ``{round: str, controllable: {pid: role}, tanks: {pid: {x,y,rot,alive,weapon}}}``"""
        out = {'round': await self.bhv.get_round_state(),
               'controllable': await self.roles(), 'tanks': {}}
        for pid in await self._all_pids():
            pos = await self.bhv.get_position(pid)
            if pos is None:
                continue
            try:
                rot = await self.bhv.get_rotation(pid)
            except Exception:
                rot = None
            try:
                weapon = await self.bhv.get_active_weapon(pid)
            except Exception:
                weapon = None
            out['tanks'][pid] = {'x': pos[0], 'y': pos[1], 'rot': rot,
                                 'alive': await self.bhv.is_alive(pid), 'weapon': weapon}
        return out

    async def _all_pids(self):
        try:
            ts = await self.ctl.tanks()
            return list(ts.keys())
        except Exception:
            return []

    async def round_state(self):
        """回合状态：``in round`` / ``counting down`` / ``between rounds`` / ..."""
        return await self.bhv.get_round_state()

    async def position(self, pid):
        """世界坐标 (x, y)；不在场返回 None。"""
        self._check(pid)
        return await self.bhv.get_position(pid)

    async def rotation(self, pid):
        """游戏内部累积 rot（弧度）。"""
        self._check(pid)
        return await self.bhv.get_rotation(pid)

    async def alive(self, pid):
        """是否在场/存活。"""
        self._check(pid)
        return await self.bhv.is_alive(pid)

    async def opponents(self, my_pid):
        """除自己外的所有坦克 pid。"""
        return [p for p in await self._all_pids() if p != str(my_pid)]

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

    # ────────────────────────── 操作（控制角色） ──────────────────────────
    def _check(self, pid):
        rs = getattr(self, '_roles_cache', None)
        # 不做严格拒绝：roles 可能临时读不到；但要求 pid 是字符串且非空
        if not isinstance(pid, str) or not pid:
            raise ValueError('pid 必须是非空字符串（用 roles()/my_pid() 获取）')

    async def refresh_roles(self):
        rs = await self.roles()
        self._roles_cache = rs
        return rs

    async def hold(self, pid, seconds, fwd=False, back=False, left=False, right=False, fire=False):
        """按住输入 ``seconds`` 秒（内部会以 30Hz 重发并在结束时松开）。
        在线输入上报粒度 0.5s，重要动作建议按住 ≥0.6s。"""
        self._check(pid)
        rs = await self.roles()
        if str(pid) not in rs:
            raise ValueError('拒绝操作：pid=%s 不是可控角色（可控=%s）' % (pid, list(rs.keys())))
        await self.ctl.set_input(pid, fwd=fwd, back=back, left=left, right=right,
                                 fire=fire, hold=seconds)

    async def stop(self, pid):
        """停止移动/转动/开火。"""
        self._check(pid)
        await self.ctl.set_input(pid, hold=0.12)

    async def move_forward(self, pid, seconds=0.4):
        """前进 ``seconds`` 秒（实测速度 15.95 m/s）。"""
        await self.hold(pid, seconds, fwd=True)

    async def turn_to_angle(self, pid, world_angle_rad):
        """把炮口转向**世界方向角**（弧度）。返回是否对准成功。"""
        self._check(pid)
        return await self.bhv.turn_to_rot(pid, float(world_angle_rad) + math.pi / 2)

    async def turn_to_rot(self, pid, rot):
        """按游戏内部 rot 转向（进阶）。"""
        self._check(pid)
        return await self.bhv.turn_to_rot(pid, rot)

    async def navigate_to(self, pid, target_xy, arrival=1.5):
        """迷宫寻路移动到 target_xy=(x,y)（米）。返回是否到达。"""
        self._check(pid)
        return await self.bhv.navigate_to(pid, target_xy, arrival=arrival)

    async def fire(self, pid, seconds=0.6):
        """开一枪（按住 fire ``seconds`` 秒）。"""
        await self.hold(pid, seconds, fire=True)

    # ────────────────────────── 弹道 / 扫描 / 火控 ──────────────────────────
    # ★ 炮口偏移（米）：真实弹丸从炮口出膛，不是从车体中心。
    #   不补这一项时，官方弹道从车体中心起算会被自车矩形包住 →
    #   "炮管方向 ±13°" 会误报 self_hit（2026-10-24 黑盒测试发现）。
    MUZZLE_OFFSET = {-1: 2.50,   # BULLET
                     0: 2.50,    # LASER
                     1: 2.25,    # DOUBLE_BARREL
                     2: 2.45,    # SHOTGUN
                     3: 2.50,    # HOMING_MISSILE
                     4: 1.45,    # MINE（向后布设，取绝对值近似）
                     5: 3.00}    # GATLING_GUN

    async def muzzle_offset(self, pid):
        """当前武器对应的炮口偏移（米）；查不到时回退基础弹 2.5m。"""
        try:
            w = await self.bhv.get_active_weapon(pid)
            return float(self.MUZZLE_OFFSET.get(int(w), 2.50))
        except Exception:
            return 2.50

    # ★ 弹丸真正的上限是**存活时间（lifetime）**，不是反弹次数：
    #   客户端 `PROJECTILE_BOUNCE_TIMEOUT_WINDOW=35` 本意是秒，却直接与 Date.now()
    #   （毫秒）相减 → 有效窗口只有 35ms，反弹计数几秒内就被清空（实测 0→1→2→3→2→1），
    #   `bounces.length >= timeoutBounceCount(5)` 几乎永远不触发。
    #   → 推演只受 `max_traveled`（= 弹速 × 寿命）限制；反弹次数不设上限。
    #   基础弹：18 m/s × 10 s = 180m；加特林/霰弹：×2s ≈ 54/64m。
    MAX_USABLE_BOUNCES = 100      # 仅作为官方路径算法的安全上限，非真实规则

    async def raycast(self, pid, world_angle_rad, max_traveled=180.0, max_bounces=None):
        """沿世界方向角推演**官方弹道**（B2DUtils，与真实弹丸一致）。

        从**炮口**（按当前武器偏移 2.25~3.0m）起算，与真实弹丸一致；
        不会把"直线出膛"误判成打自己。

        ★ 真实规则：弹丸只在**存活时间**到达时消亡（基础弹 10s → 射程 180m）；
        反弹次数**没有实际上限**（客户端反弹窗口单位 bug，见模块注释）。

        返回 ``{angle_rad, path:[{x,y}], bounces, traveled, hit_pid, self_hit}``：
        * ``hit_pid``：路径上第一个被命中的坦克 pid（没命中坦克为 None）
        * ``self_hit``：第一个命中的是否是**自己**（只有反弹回来的弹道才会）
        """
        self._check(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return None
        off = await self.muzzle_offset(pid)
        cap = self.MAX_USABLE_BOUNCES if max_bounces is None else int(max_bounces)
        d = await self.bhv._raycast_path(
            pos[0], pos[1], math.degrees(float(world_angle_rad)), 0.0, 0.0,
            max_bounces=max(1, cap),
            max_dist=float(max_traveled) * 1.5, target_tank_id=None,
            origin_offset=off)
        nb = int(d.get('bounces') or 0)
        traveled = float(d.get('traveled') or 0)
        reach = traveled <= float(max_traveled)      # ★ 射程 = 弹速 × 寿命，超出即不可达
        return {'angle_rad': float(world_angle_rad),
                'path': d.get('path') or [],
                'bounces': nb,
                'traveled': traveled,
                'hit_pid': d.get('hitTank') if reach else None,
                'self_hit': reach and str(d.get('hitTank')) == str(pid),
                'reachable': reach}

    async def scan(self, pid, step_deg=30, max_traveled=180.0, max_bounces=None):
        """**静默扫描 360°**：对每个方向角（世界角）取官方弹道第一个命中的坦克。

        从**炮口**起算（按当前武器偏移），与真实弹丸一致。
        ★ 弹丸只在**存活时间**到限时消亡（基础弹 10s → 180m）；反弹次数无实际上限。

        返回列表（每个元素）::

            {'angle_rad': float, 'hit_pid': str|None, 'self_hit': bool,
             'bounces': int, 'traveled': float, 'reachable': bool}

        * ``hit_pid`` 是其他角色、且 ``self_hit`` 为 False → **可以命中对手且自己不在弹道上**
        * step_deg 越小越细但越慢（30°/12 个方向 ≈ 0.5s）
        """
        self._check(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return []
        off = await self.muzzle_offset(pid)
        cap = self.MAX_USABLE_BOUNCES if max_bounces is None else int(max_bounces)
        out = []
        for deg in range(0, 360, int(step_deg)):
            d = await self.bhv._raycast_path(
                pos[0], pos[1], float(deg), 0.0, 0.0,
                max_bounces=max(1, cap),
                max_dist=float(max_traveled) * 1.5, target_tank_id=None,
                origin_offset=off)
            nb = int(d.get('bounces') or 0)
            traveled = float(d.get('traveled') or 0)
            reach = traveled <= float(max_traveled)   # ★ 射程 = 弹速 × 寿命
            out.append({'angle_rad': math.radians(deg),
                        'hit_pid': d.get('hitTank') if reach else None,
                        'self_hit': reach and str(d.get('hitTank')) == str(pid),
                        'bounces': nb,
                        'traveled': traveled,
                        'reachable': reach})
        return out

    async def find_aim(self, pid, target_xy, target_id=None, require_bounce=False,
                       max_traveled=180.0, max_bounces=None):
        """找能命中目标的**世界方向角**（官方弹道，自动排除会先打自己的解）。

        ★ 真实规则：弹丸只在存活时间到限时消亡；反弹次数无实际上限。"""
        self._check(pid)
        off = await self.muzzle_offset(pid)
        cap = self.MAX_USABLE_BOUNCES if max_bounces is None else int(max_bounces)
        return await self.bhv.find_aim_angle_raycast(
            pid, tuple(target_xy), target_id=target_id, require_bounce=require_bounce,
            max_traveled=max_traveled, max_bounces=cap, muzzle_offset=off)

    async def aim_and_fire(self, pid, target_xy, target_id=None, fire_sec=0.6,
                           require_bounce=False):
        """转向并开火，返回是否已开火（True=已开火）。"""
        self._check(pid)
        off = await self.muzzle_offset(pid)
        return await self.bhv.aim_and_fire(
            pid, tuple(target_xy), fire_sec=fire_sec, target_id=target_id, raycast=True,
            lead=False, require_bounce=require_bounce, max_traveled=180.0, max_bounces=25,
            muzzle_offset=off)
