"""
ttkit —— TankTrouble 角色信息获取 / 操作封装包（黑盒 API **v3**）
================================================================
把"读取游戏状态"和"操作角色"封装成稳定、带文档的方法。所有控制都走游戏自己的
`setInputState`（与人类按键等价，在线/本地都安全）。

v3 相比 v2 的关键改进（来自第三轮黑盒测试的反馈）：
  * **自伤风险检测** `self_risk`：预测路径在**第一次反弹之后**离自己过近时报警
    （官方零宽射线与实际弹丸有偏差，实测曾"预测命中对手、实际偏 16.5m 打死自己"）；
  * **`aim_and_fire` 的鲁棒检查改为可选**（v2 默认 ±1.5° 在窄窗口图上几乎永不开火）；
  * `turn_to_angle()` 改为**自适应脉冲 + 过冲回退**，返回最佳可达角（避免容差附近极限环振荡）；
  * `scan_adaptive()`：粗扫→细扫（30→10→5→2°），命中窗口只有 1~3° 时也能找到解；
  * `scan()` 每项新增 `path` / `self_risk`（米）/ `self_risk_ok`；
  * 新增 `reset_maze_cache()`（迷宫每回合重生成，避免旧回合墙缓存污染寻路预筛）、
    `wait_target_gone()`、`navigate_to_ex()`；`roles()` 缓存缩短到 1s。

坐标/角度约定
-------------
* 世界坐标：米（1 格 = 10 米）。"炮口朝向"用**世界方向角**（弧度，0=+X，逆时针）；
  游戏内部 `rot = 角度 + π/2`。
* `rotation(pid)` = 原始累积 rot（可超 ±π）；`rotation_norm(pid)` = 归一化值。

自伤标签与自伤风险
------------------
* `deadlyToOwner`：弹丸第一次撞墙/盾后才可能伤发射者（之前穿透自己）。
* `self_hit`：官方射线第一个命中的坦克是**自己**且 bounces≥1（理论自伤）。
* `self_risk`：官方射线在**第一次反弹后**离自己车体中心的最小距离（米）。
  由于推演/实弹有偏差，`self_risk < SELF_RISK_RADIUS(4m)` 时应视为**有自伤风险**，
  即使 `self_hit=False` 也不要开火。

可靠性提示
----------
1. 转向精度约 ±1~2°（输入按物理帧量化）；1° 差就可能让弹道拓扑突变，开火前必须用**实际朝向**复核。
2. 官方零宽射线与实际 0.25m 弹丸在远距/多反弹路径上偏差会累积（实测 10 反弹/156m 偏 25m+）。
   **短距、少反弹、`self_risk` 大**的解最可靠。
3. `scan()` 很快（12 方向 ~10~20ms；2° 全扫 180 条 ~150ms）。
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
           'WEAPON_NAMES', 'MUZZLE_OFFSET', 'WEAPON_RANGE', 'SELF_RISK_RADIUS']

WEAPON_NAMES = {-1: 'BULLET', 0: 'LASER', 1: 'DOUBLE_BARREL', 2: 'SHOTGUN',
                3: 'HOMING_MISSILE', 4: 'MINE', 5: 'GATLING_GUN'}
# 炮口偏移（米）
MUZZLE_OFFSET = {-1: 2.50, 0: 2.50, 1: 2.25, 2: 2.45, 3: 2.50, 4: 1.45, 5: 3.00}
# 射程（米）= 弹速 × 寿命
WEAPON_RANGE = {-1: 180.0, 0: 144.0, 1: 108.0, 2: 64.0, 3: 180.0, 4: 0.0, 5: 54.0}

MAX_PATH_BOUNCES = 100
# 反弹后路径离自己多近算"自伤风险"（米）。实测预测偏差可达 16m，取 4m 保守。
SELF_RISK_RADIUS = 4.0


class Kit:
    """角色信息获取 + 操作 门面。所有方法均为 async。"""

    def __init__(self, port=None):
        self.port = port
        self.ctl = None
        self.bhv = None
        self.gu = _gu
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
        """释放连接（两条 CDP 连接都关）。"""
        for obj in (getattr(self.bhv, 'ctl', None), self.ctl):
            try:
                cdp = getattr(obj, 'cdp', None)
                if cdp is not None:
                    await cdp.close()
            except Exception:
                pass
        self.bhv = None
        self.ctl = None

    async def reset_maze_cache(self):
        """清空行为层缓存的墙/迷宫数据（迷宫每回合重生成，跨回合必须清）。"""
        for attr in ('_walls_cache', '_maze_cache', '_tiles_cache', '_wall_cache'):
            try:
                if hasattr(self.bhv, attr):
                    setattr(self.bhv, attr, None)
            except Exception:
                pass

    # ────────────────────────── 信息（只读） ──────────────────────────
    async def roles(self, refresh=False):
        """可控角色：``{pid: role}``。默认 1s 缓存。"""
        now = time.time()
        if refresh or not self._roles_cache or now - self._roles_ts > 1.0:
            self._roles_cache = await self.bhv.get_roles()
            self._roles_ts = now
        return dict(self._roles_cache)

    async def my_pid(self, role='arrows'):
        """选出"自己的角色"（默认 arrows）。找不到该角色时**抛 RuntimeError**。"""
        rs = await self.roles(refresh=True)
        for pid, r in rs.items():
            if r == role:
                return pid
        if role is None and rs:
            return sorted(rs)[0]
        raise RuntimeError('没有 role=%r 的可控角色（当前可控=%r）' % (role, rs))

    async def _require(self, pid):
        if not isinstance(pid, str) or not pid:
            raise ValueError('pid 必须是非空字符串（用 roles()/my_pid() 获取）')
        rs = await self.roles()
        if str(pid) not in rs:
            raise ValueError('拒绝操作：pid=%s 不是可控角色（可控=%s）' % (pid, list(rs.keys())))

    async def snapshot(self):
        """局面快照：``{round_state, controllable, tanks:{pid:{x,y,rot,alive,weapon}}}``"""
        out = {'round_state': await self.bhv.get_round_state(),
               'controllable': await self.roles(), 'tanks': {}}
        for pid in await self._all_pids():
            pos = await self.bhv.get_position(pid)
            if pos is None:
                continue
            out['tanks'][pid] = {'x': pos[0], 'y': pos[1], 'rot': await self.rotation(pid),
                                 'alive': await self.bhv.is_alive(pid),
                                 'weapon': await self.weapon(pid)}
        return out

    async def _all_pids(self):
        try:
            return list((await self.ctl.tanks()).keys())
        except Exception:
            return []

    async def round_state(self):
        return await self.bhv.get_round_state()

    async def position(self, pid):
        return await self.bhv.get_position(pid)

    async def rotation(self, pid):
        """**原始累积 rot**（弧度）。"""
        try:
            raw = await self.bhv._raw_rot(pid)
            if raw is not None:
                return raw
        except Exception:
            pass
        return await self.bhv.get_rotation(pid)

    async def rotation_norm(self, pid):
        r = await self.rotation(pid)
        if r is None:
            return None
        return (r + math.pi) % (2 * math.pi) - math.pi

    async def alive(self, pid):
        return await self.bhv.is_alive(pid)

    async def opponents(self, my_pid):
        return [p for p in await self._all_pids() if str(p) != str(my_pid)]

    async def nearest_opponent(self, my_pid):
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
        try:
            return await self.bhv.get_active_weapon(pid)
        except Exception:
            return None

    def range_of(self, weapon):
        try:
            return WEAPON_RANGE.get(int(weapon), 180.0)
        except Exception:
            return 180.0

    async def bullet_range(self, pid):
        return self.range_of(await self.weapon(pid))

    async def terrain(self):
        return await _gu.get_terrain(self.ctl)

    async def open_tiles(self):
        terr = await self.terrain()
        out = []
        tiles = (terr or {}).get('tiles') or []
        for gy in range(len(tiles)):
            for gx in range(len(tiles[gy])):
                if tiles[gy][gx] and tiles[gy][gx][0]:
                    out.append((gx * 10.0 + 5.0, gy * 10.0 + 5.0))
        return out

    async def plan_path(self, start_xy, end_xy):
        return await _gu.plan_maze_path(self.ctl, tuple(start_xy), tuple(end_xy))

    async def maze_distance(self, from_xy, to_xy):
        return await _gu.maze_distance(self.ctl, tuple(from_xy), tuple(to_xy))

    async def projectiles(self):
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

    async def wait_target_gone(self, target_id, timeout=8.0, poll=0.2):
        """轮询等待目标不在场（击杀/离场）→ True。用于开火后确认命中。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if await self.position(target_id) is None:
                return True
            await asyncio.sleep(poll)
        return False

    # ────────────────────────── 操作（只允许自己的角色） ──────────────────────────
    async def hold(self, pid, seconds, fwd=False, back=False, left=False, right=False, fire=False):
        await self._require(pid)
        await self.ctl.set_input(pid, fwd=fwd, back=back, left=left, right=right,
                                 fire=fire, hold=seconds)

    async def stop(self, pid):
        await self._require(pid)
        await self.ctl.set_input(pid, hold=0.12)
        try:
            await self.bhv.wait_still(pid, max_wait=0.8)
        except Exception:
            await asyncio.sleep(0.08)

    async def move_forward(self, pid, seconds=0.4):
        await self.hold(pid, seconds, fwd=True)

    async def move_back(self, pid, seconds=0.4):
        await self.hold(pid, seconds, back=True)

    async def turn_to_rot(self, pid, rot, tol=0.03):
        await self._require(pid)
        return await self.bhv.turn_to_rot(pid, float(rot), tol=tol)

    async def turn_to_angle(self, pid, world_angle_rad, tol_deg=1.8, max_pulses=40):
        """自适应闭环转向到**世界方向角**（弧度）。

        接近目标时自动缩小脉冲；检测到过冲就回退并接受最佳可达角（避免极限环振荡）。
        返回 ``{'ok','actual_deg','err_deg','best_err_deg'}``。
        """
        await self._require(pid)
        target = float(world_angle_rad) + math.pi / 2
        tol = math.radians(float(tol_deg))
        best = None            # (abs_err, cur_rot)
        prev_sign = 0
        flips = 0
        pulse = 0.02
        for _ in range(int(max_pulses)):
            cur = await self.rotation(pid)
            if cur is None:
                break
            err = (target - cur + math.pi) % (2 * math.pi) - math.pi
            ae = abs(err)
            if best is None or ae < best[0]:
                best = (ae, cur)
            if ae <= tol:
                break
            s = 1 if err > 0 else -1
            if prev_sign and s != prev_sign:
                flips += 1
                pulse = max(0.008, pulse * 0.5)
                if flips >= 3:
                    break
            prev_sign = s
            await self.hold(pid, pulse, **({'right': True} if s > 0 else {'left': True}))
            await asyncio.sleep(0.03)
            if pulse < 0.01 and ae > math.radians(10):
                pulse = 0.02
        try:
            await self.ctl.set_input(pid, hold=0.05)
        except Exception:
            pass
        if best is None:
            return {'ok': False, 'actual_deg': None, 'err_deg': None, 'best_err_deg': None}
        ok = best[0] <= tol + math.radians(1.0)
        return {'ok': bool(ok), 'actual_deg': math.degrees(best[1] - math.pi / 2),
                'err_deg': -math.degrees(best[0]), 'best_err_deg': math.degrees(best[0])}

    async def navigate_to(self, pid, target_xy, arrival=1.5, timeout=None):
        await self._require(pid)
        coro = self.bhv.navigate_to(pid, tuple(target_xy), arrival=arrival)
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=float(timeout))
        except asyncio.TimeoutError:
            return False

    async def navigate_to_ex(self, pid, target_xy, arrival=1.5, timeout=None):
        """带信息的寻路：返回 ``{arrived, pos, dist, elapsed}``。"""
        import time as _t
        t0 = _t.time()
        arrived = await self.navigate_to(pid, target_xy, arrival=arrival, timeout=timeout)
        pos = await self.position(pid)
        dist = None
        if pos is not None:
            dist = math.hypot(pos[0] - target_xy[0], pos[1] - target_xy[1])
        return {'arrived': bool(arrived), 'pos': pos, 'dist': dist,
                'elapsed': round(_t.time() - t0, 2)}

    async def fire(self, pid, seconds=0.6):
        await self.hold(pid, seconds, fire=True)

    # ────────────────────────── 弹道 / 扫描 / 火控 ──────────────────────────
    @staticmethod
    def _seg_dist(px, py, ax, ay, bx, by):
        vx, vy = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        L2 = vx * vx + vy * vy
        t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, (wx * vx + wy * vy) / L2))
        return math.hypot(px - (ax + t * vx), py - (ay + t * vy))

    def _self_risk_of_path(self, pos, path):
        """第一次反弹后，路径离自己车体中心的最小距离（米）。无风险 → 999。"""
        if not path or len(path) < 3 or pos is None:
            return 999.0
        best = 999.0
        # path[0]=起点、path[1]=第一次反弹点；deadlyToOwner 从 path[1] 之后生效
        for i in range(1, len(path) - 1):
            a, b = path[i], path[i + 1]
            d = self._seg_dist(pos[0], pos[1], a['x'], a['y'], b['x'], b['y'])
            if d < best:
                best = d
        return best

    async def raycast(self, pid, world_angle_rad, max_traveled=None, max_bounces=None):
        """官方弹道推演（从炮口起算）。

        返回 ``{angle_rad, path, bounces, traveled, hit, hit_pid, self_hit,
        self_hit_harmless, self_risk, self_risk_ok, reachable}``。
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
        return self._mk_ray(pid, world_angle_rad, d, float(max_traveled), pos)

    def _mk_ray(self, pid, angle_rad, d, max_traveled, pos=None):
        nb = int(d.get('bounces') or 0)
        traveled = float(d.get('traveled') or 0)
        reach = traveled <= max_traveled
        raw_hit = d.get('hitTank')
        hit = bool(raw_hit) and reach
        self_pid = str(raw_hit) == str(pid)
        path = d.get('path') or []
        risk = self._self_risk_of_path(pos, path)
        return {'angle_rad': float(angle_rad),
                'path': path,
                'bounces': nb,
                'traveled': traveled if reach else max_traveled,
                'hit': bool(hit),
                'hit_pid': raw_hit if hit else None,
                'self_hit': bool(hit and self_pid and nb >= 1),
                'self_hit_harmless': bool(hit and self_pid and nb == 0),
                'self_risk': float(risk),
                'self_risk_ok': bool(risk >= SELF_RISK_RADIUS),
                'reachable': reach}

    async def scan(self, pid, step_deg=30, max_traveled=None, robust_deg=None):
        """静默扫描 360°：每项含 ``hit_pid/self_hit/bounces/traveled/robust/quality/
        path/self_risk/self_risk_ok``。

        ``quality = traveled + 30×bounces``（越小越可靠）。
        ``self_risk < 4m`` 表示反弹后路径贴近自己 → 有自伤风险。
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
            item = self._mk_ray(pid, math.radians(deg), d, float(max_traveled), pos)
            item['robust'] = None
            if robust_deg and item['hit_pid'] and not item['self_hit']:
                ok = True
                for delta in (float(robust_deg), -float(robust_deg)):
                    d2 = await self.bhv._raycast_path(
                        pos[0], pos[1], float(deg) + delta, 0.0, 0.0,
                        max_bounces=MAX_PATH_BOUNCES, max_dist=float(max_traveled) * 1.5,
                        target_tank_id=None, origin_offset=off)
                    it2 = self._mk_ray(pid, math.radians(deg + delta), d2, float(max_traveled), pos)
                    if not (it2['hit_pid'] and str(it2['hit_pid']) == str(item['hit_pid'])
                            and not it2['self_hit']):
                        ok = False
                        break
                item['robust'] = ok
            item['quality'] = float(item['traveled']) + 30.0 * float(item['bounces'])
            out.append(item)
        return out

    def is_good_candidate(self, item, pid, target_id=None):
        """候选是否"可安全开火"：命中对手、不自伤、无自伤风险。"""
        if not item or not item.get('hit_pid'):
            return False
        if str(item['hit_pid']) == str(pid):
            return False
        if target_id is not None and str(item['hit_pid']) != str(target_id):
            return False
        return bool(not item.get('self_hit') and item.get('self_risk_ok', True))

    async def scan_adaptive(self, pid, target_id=None, steps=(30, 10, 5, 2),
                            robust_deg=None):
        """粗→细自适应扫描：依次用 steps 扫描，一旦找到安全候选就返回。

        返回 ``(candidates, used_steps)``；候选已按 quality 升序。
        """
        used = []
        for st in steps:
            used.append(int(st))
            res = await self.scan(pid, step_deg=st, robust_deg=robust_deg)
            cands = [r for r in res if self.is_good_candidate(r, pid, target_id)]
            if cands:
                cands.sort(key=lambda r: r['quality'])
                return cands, used
        return [], used

    async def find_aim(self, pid, target_xy, target_id=None, require_bounce=False,
                       max_traveled=None, max_bounces=None):
        await self._require(pid)
        off = float(MUZZLE_OFFSET.get(int(await self.weapon(pid)), 2.50))
        if max_traveled is None:
            max_traveled = await self.bullet_range(pid)
        cap = MAX_PATH_BOUNCES if max_bounces is None else int(max_bounces)
        return await self.bhv.find_aim_angle_raycast(
            pid, tuple(target_xy), target_id=target_id, require_bounce=require_bounce,
            max_traveled=float(max_traveled), max_bounces=cap, muzzle_offset=off)

    async def aim_and_fire(self, pid, target_xy, target_id=None, fire_sec=0.6,
                           tol_deg=1.8, robust_deg=None, self_risk_ok=True, max_attempts=3):
        """转向 + 在实际朝向复核弹道（含自伤风险检查）→ 通过才开火。

        v3 默认**不做** ±robust_deg 邻域检查（窄窗口图上会永不开火），
        但默认要求 `self_risk_ok`（反弹后路径不贴近自己）。

        返回 ``{'fired','reason','angle_deg','verify','attempt'}``。
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
            if ok and self_risk_ok and not v.get('self_risk_ok', True):
                ok = False
            if ok and robust_deg:
                for delta in (float(robust_deg), -float(robust_deg)):
                    s = await self.raycast(pid, actual + math.radians(delta))
                    if not (s and s.get('hit') and str(s.get('hit_pid')) == str(target_id)
                            and not s.get('self_hit')):
                        ok = False
                        break
            if not ok:
                continue
            await self.fire(pid, fire_sec)
            return {'fired': True, 'reason': None, 'angle_deg': math.degrees(actual),
                    'verify': v, 'attempt': attempt}
        return {'fired': False, 'reason': 'verify_failed', 'angle_deg': None,
                'verify': None, 'attempt': int(max_attempts)}
