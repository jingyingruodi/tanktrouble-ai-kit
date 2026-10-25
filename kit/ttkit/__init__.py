"""
ttkit —— TankTrouble 角色信息获取 / 操作封装包（黑盒 API **v12**）
================================================================
把"读取游戏状态"和"操作角色"封装成稳定、带文档的方法。所有控制都走游戏自己的
`setInputState`（与人类按键等价，在线/本地都安全）。

v12（第十一轮黑盒反馈，移动目标场景）：`lead_target` 增加 `interceptable/converged/shift/reason`
（目标速度≥0.95×弹速或迭代发散 → 判不可拦截，不再给假提前量）；新增
`path_dist_to_point()` / `find_aim_at_point()`（对**预测点**选角，不要求命中坦克本体）；
`reachable_windows` 的 feasible 改为"窗口宽度≥min_width_deg 即可命中"（不再被 4.775° 网格硬否决）。

v6（第五/六轮黑盒反馈）：`ammo()` 增加 `remaining = left - fired`（left 是弹匣容量！）；
新增 `scores()/kills_of()/round_id()` 用于**比分归因**；`scan()` 增加 `include_targets`
（补"正对目标"的直线角，固定网格会漏掉 1~3° 的窗口）；`fine_aim(profile=None)` 自动
多档并用 `same_family` 防止换成更长路径解；`rotation()` 瞬时 None 重试；
`navigate_to_ex()` 返回 `from/moved`；`turn_precise()` 多轮补偿 + `outlier` 标记。

v4 相比 v3 的关键改进（来自第四轮黑盒测试的反馈）：
  * `turn_to_angle()` 的 `actual_deg/err_deg` 改为**停稳后重新读取的最终朝向**
    （v3 返回"过程最优值"，实测可差 9~14°）；另给 `final_err_deg`；
  * `scan_adaptive()` 默认**合并所有步长的候选**（粗扫的长解不再掩盖近角短解）；
  * `is_good_candidate()` 增加策略参数（默认 `bounces<=6`；长弹道要求 `self_risk>=20m`）；
  * `quality` 纳入 `self_risk`（离自己越近越不可靠）；
  * 文档补充：官方推演是**零宽射线**（`b2dworld.RayCast`，不含 0.25m 弹丸半径），
    掠射反弹点必然有偏差；`calculateFiringPath` 起点是车体中心沿 -Y 偏 2m，
    而实弹出生点是 2.5m（`BULLET.OFFSET`）。霰弹（weapon=2）的散射**未建模**。

v3 相比 v2 的关键改进（来自第三轮黑盒测试的反馈）：
  * **自伤风险检测** `self_risk`：预测路径在**第一次反弹之后**离自己过近时报警
    （官方零宽射线与实际弹丸有偏差，实测曾"预测命中对手、实际偏 16.5m 打死自己"）；
  * **`aim_and_fire` 的鲁棒检查改为可选**（v2 默认 ±1.5° 在窄窗口图上几乎永不开火）；
  * `turn_to_angle()` 改为**自适应脉冲 + 过冲回退**；
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

def _wrap_pi(a):
    """把角度归一化到 [-π, π)。"""
    return (a + math.pi) % (2 * math.pi) - math.pi


WEAPON_SPEED = {-1: 18.0, 0: 180.0, 1: 18.0, 2: 32.0, 3: 18.0, 4: 0.0, 5: 27.0}

MAX_PATH_BOUNCES = 100
# 反弹后路径离自己多近算"自伤风险"（米）。实测预测偏差可达 16m，取 4m 保守。
SELF_RISK_RADIUS = 4.0
# 转向量子：5 rad/s / 60fps = 4.775 度/帧。转角只能是 起始角 + k*量子 的网格。
TURN_QUANTUM_DEG = 4.775


class Kit:
    """角色信息获取 + 操作 门面。所有方法均为 async。"""

    def __init__(self, port=None):
        self.port = port
        self.ctl = None
        self.bhv = None
        self.gu = _gu
        self._roles_cache = {}
        self._roles_ts = 0.0
        # 自伤阈值缩放系数 (bounces, traveled)；None = 关闭几何放大
        self.risk_scale = (0.6, 0.03)

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

    async def my_pid(self, role='arrows', wait=False, timeout=10.0):
        """选出"自己的角色"（默认 arrows）。

        v14：``wait=True`` 时在回合间隙（roles() 为空/角色未出生）轮询等待，
        最多 ``timeout`` 秒；仍找不到才抛 RuntimeError。
        """
        if wait:
            t0 = time.time()
            while time.time() - t0 < float(timeout):
                try:
                    return await self.my_pid(role, wait=False)
                except RuntimeError:
                    await asyncio.sleep(0.3)
            raise RuntimeError('等待 %.1fs 仍没有 role=%r 的可控角色' % (float(timeout), role))
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
        for _ in range(2):
            try:
                p = await self.bhv.get_position(pid)
                if p is not None:
                    return p
            except Exception:
                pass
            await asyncio.sleep(0.08)
        return None

    async def rotation(self, pid):
        """**原始累积 rot**（弧度）。"""
        try:
            raw = await self.bhv._raw_rot(pid)
            if raw is not None:
                return raw
        except Exception:
            pass
        r = await self.bhv.get_rotation(pid)
        if r is None:
            await asyncio.sleep(0.1)
            r = await self.bhv.get_rotation(pid)
        return r

    async def rotation_norm(self, pid):
        r = await self.rotation(pid)
        if r is None:
            return None
        return (r + math.pi) % (2 * math.pi) - math.pi

    async def destroyed_pids(self):
        """本回合已被摧毁的 pid 列表（回合重置后可能为空）。"""
        try:
            return list(await self.bhv.get_destroyed())
        except Exception:
            return []

    async def alive(self, pid):
        """是否存活（v5：先查 destroyed 列表，再查 position，短暂 None 会重试一次）。"""
        try:
            if str(pid) in [str(d) for d in await self.destroyed_pids()]:
                return False
        except Exception:
            pass
        try:
            if await self.bhv.is_alive(pid):
                return True
        except Exception:
            pass
        pos = await self.position(pid)
        if pos is not None:
            return True
        await asyncio.sleep(0.2)
        return (await self.position(pid)) is not None

    async def position_confirm(self, pid, retries=3, delay=0.2):
        """读取位置并确认（短暂 None 重试）；仍为 None 返回 None。"""
        for _ in range(max(1, int(retries))):
            pos = await self.position(pid)
            if pos is not None:
                return pos
            await asyncio.sleep(float(delay))
        return None

    async def ammo(self, pid):
        """本回合弹药：``{'left': int, 'fired': int, 'weapon': int}`` 或 None。

        默认武器每回合 5 发；打完后再 fire() **静默无效**（游戏不报错）。
        """
        js = """(() => {
          try {
            const gc = GameManager.getGameController();
            if (!gc) return 'null';
            const rs = gc.roundController.model.getRoundState();
            const d = rs && rs.data ? rs.data : null;
            if (!d || !d.weaponStates) return 'null';
            const out = [];
            for (let i = 0; i < d.weaponStates.length; i++) {
              const st = d.weaponStates[i];
              let fields = st ? (st.fieldsJSON || st.fields || st) : null;
              if (typeof fields === 'string') { try { fields = JSON.parse(fields); } catch (e) {} }
              out.push({playerId: (st && st.playerId !== undefined) ? st.playerId
                          : (fields && fields.playerId !== undefined ? fields.playerId : null),
                        bulletCount: fields ? fields._bulletCount : null,
                        bulletsFired: fields ? fields._bulletsFired : null,
                        type: fields ? fields._type : null});
            }
            return JSON.stringify(out);
          } catch (e) { return 'null'; }
        })()"""
        try:
            raw = json.loads(await self.ctl.cdp.eval(js))
            if not raw:
                return None
            weapon = await self.weapon(pid)
            cands = [e for e in raw
                     if e.get('playerId') is not None and str(e['playerId']) == str(pid)]
            if not cands:
                return None
            pick = None
            if weapon is not None:
                for e in cands:
                    if e.get('type') is not None and int(e['type']) == int(weapon):
                        pick = e
                        break
            if pick is None:
                pick = cands[0]
            for e in [pick]:
                if True:
                    left = e.get('bulletCount')
                    fired = e.get('bulletsFired')
                    remain = None
                    try:
                        if left is not None and fired is not None:
                            remain = max(0, int(left) - int(fired))
                    except Exception:
                        remain = None
                    return {'left': left, 'fired': fired, 'remaining': remain,
                            'weapon': weapon, 'type': e.get('type')}
            return None
        except Exception:
            return None

    async def round_id(self):
        """当前回合 id（``gc.getRoundId()``）；跨回合切换/计分归因比状态字符串可靠。"""
        js = """(() => { try { const gc = GameManager.getGameController();
          if (!gc) return 'null';
          return JSON.stringify(gc.getRoundId ? gc.getRoundId() : null); }
          catch (e) { return 'null'; } })()"""
        try:
            raw = await self.ctl.cdp.eval(js)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def scores(self):
        """比分/击杀（``gc.getScores()`` 原始结构）；用于**开火后归因**。

        本地局一击必杀后回合会立刻重置，`alive()/destroyed_pids()` 来不及观察，
        用比分增量（击杀 +1）判断命中才是最可靠的。
        """
        js = """(() => { try { const gc = GameManager.getGameController();
          if (!gc || !gc.getScores) return 'null';
          return JSON.stringify(gc.getScores()); } catch (e) { return 'null'; } })()"""
        try:
            raw = await self.ctl.cdp.eval(js)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def kills_of(self, pid):
        """某 pid 的计分：``{'kills': int, 'victories': int}`` 或 None。

        游戏 ``getScores()`` 结构为 ``{scoreId: {id, playerId, type, value}}``，
        type=0 击杀、type=1 胜局（实测 9→13→17 与命中数一致）。
        """
        sc = await self.scores()
        if not sc:
            return None
        kills = 0
        wins = 0
        found = False
        entries = sc.values() if isinstance(sc, dict) else sc
        try:
            for e in entries:
                if not isinstance(e, dict):
                    continue
                if e.get('playerId') is None or str(e['playerId']) != str(pid):
                    continue
                found = True
                t = e.get('type')
                v = e.get('value')
                try:
                    v = int(v)
                except Exception:
                    continue
                if int(t) == 0:
                    kills = v
                elif int(t) == 1:
                    wins = v
        except Exception:
            return None
        return {'kills': kills, 'victories': wins} if found else None

    async def bullets_in_flight(self, pid=None):
        """场上弹丸（可按发射者过滤）；可用于"一发一结算"归因。"""
        ps = await self.projectiles()
        if pid is None:
            return ps
        return [p for p in ps if str(p.get('playerId')) == str(pid)]

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

    def _muzzle(self, weapon):
        """当前武器的炮口偏移（米）；weapon 为 None/未知时回退基础弹 2.5m。"""
        try:
            return float(MUZZLE_OFFSET.get(int(weapon), 2.50))
        except Exception:
            return 2.50

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
        for _ in range(2):
            try:
                await self.ctl.set_input(pid, hold=0.12)
                break
            except Exception:
                await asyncio.sleep(0.1)
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

    async def _press_until(self, pid, key, limit_raw, sign, timeout=3.0):
        """**长按住** key，每 8ms 读一次 raw rot，到达 limit_raw 就取消任务松键。

        利用 `set_input` 的 finally 保证取消时松键；比"30ms 分块"精细约 4 倍。
        """
        task = asyncio.create_task(
            self.ctl.set_input(pid, **{key: True}, hold=min(3.0, float(timeout))))
        t0 = time.time()
        try:
            while time.time() - t0 < float(timeout):
                cur = await self.rotation(pid)
                if cur is None:
                    break
                if (sign > 0 and cur >= limit_raw) or (sign < 0 and cur <= limit_raw):
                    break
                await asyncio.sleep(0.008)
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            try:
                await self.ctl.set_input(pid, hold=0.0)
            except Exception:
                pass

    def snap_angle_deg(self, ang_deg, quantum_deg=TURN_QUANTUM_DEG):
        """把世界方向角(度)吸附到最近的可达角（转向量子网格）。"""
        q = float(quantum_deg)
        return round(float(ang_deg) / q) * q

    async def snap_angle(self, pid, world_angle_rad, quantum_deg=TURN_QUANTUM_DEG):
        """把世界方向角(弧度)吸附到「从当前 rot 出发可达」的最近网格角。

        返回 ``{'angle_rad','angle_deg','offset_deg'}``（offset 是吸附偏移）。
        转向量子约 4.775 度：2~9 度的小修正不可达，必须先吸附再复核。
        """
        await self._require(pid)
        cur = await self.rotation(pid)
        if cur is None:
            return {'angle_rad': float(world_angle_rad),
                    'angle_deg': math.degrees(world_angle_rad), 'offset_deg': 0.0}
        q = math.radians(float(quantum_deg))
        cur_world = _wrap_pi(cur - math.pi / 2)
        delta = _wrap_pi(float(world_angle_rad) - cur_world)
        snapped = cur_world + round(delta / q) * q
        snapped_n = cur_world + round(delta / q) * q
        snapped_n = _wrap_pi(snapped_n)
        return {'angle_rad': snapped_n, 'angle_deg': math.degrees(snapped_n),
                'offset_deg': math.degrees(_wrap_pi(snapped_n - float(world_angle_rad)))}

    async def turn_precise(self, pid, world_angle_rad, tol_deg=0.8, lead_deg=1.2,
                           max_rounds=6, timeout=6.0):
        """**连续按住 + 高频读 rot + 提前释放 + 过冲补偿**的精准转向。

        比脉冲法精度高得多（实测残差 mean 0.8~1.2°、max <4°；脉冲法 3~6°、max 12°+）。
        返回 ``{'ok','actual_deg'(归一化 [-180,180)),'err_deg','best_err_deg',
        'final_err_deg','precise'}``。
        """
        await self._require(pid)
        t0 = time.time()
        target_world = float(world_angle_rad) + math.pi / 2
        for _i in range(int(max_rounds)):
            if time.time() - t0 > float(timeout):
                break
            cur = await self.rotation(pid)
            if cur is None:
                break
            target_raw = cur + _wrap_pi(target_world - cur)
            err = target_raw - cur
            if abs(err) <= math.radians(float(tol_deg)):
                break
            key = 'right' if err > 0 else 'left'
            sign = 1 if err > 0 else -1
            lead = math.radians(float(lead_deg) * (0.6 ** _i))
            limit = target_raw - lead if err > 0 else target_raw + lead
            await self._press_until(pid, key, limit, sign,
                                    timeout=min(3.0, abs(err) / TANK_ROTATION_SPEED + 1.2))
        try:
            await self.ctl.set_input(pid, hold=0.0)
        except Exception:
            pass
        await asyncio.sleep(0.06)
        cur = await self.rotation(pid)
        if cur is None:
            return {'ok': False, 'actual_deg': None, 'err_deg': None,
                    'best_err_deg': None, 'final_err_deg': None, 'precise': True}
        target_raw = cur + _wrap_pi(target_world - cur)
        err = target_raw - cur
        return {'ok': abs(err) <= math.radians(float(tol_deg) + 0.6),
                'actual_deg': math.degrees(_wrap_pi(cur - math.pi / 2)),
                'err_deg': -math.degrees(err),
                'best_err_deg': abs(math.degrees(err)),
                'final_err_deg': math.degrees(err),
                'outlier': bool(abs(math.degrees(err)) > 3.0),
                'precise': True}

    async def turn_to_angle(self, pid, world_angle_rad, tol_deg=1.2, max_pulses=40,
                            settle=True):
        """转向到**世界方向角**：优先连续按住精准转向，失败退回脉冲自适应。

        ``actual_deg`` **归一化到 [-180,180)**；`ok` 基于停稳后最终朝向。
        """
        res = await self.turn_precise(pid, world_angle_rad, tol_deg=tol_deg)
        if res.get('ok'):
            return res
        res2 = await self._turn_adaptive(pid, world_angle_rad, tol_deg=tol_deg,
                                         max_pulses=max_pulses, settle=settle)
        res2['fallback'] = True
        return res2

    async def _turn_adaptive(self, pid, world_angle_rad, tol_deg=1.8, max_pulses=40,
                             settle=True):
        """自适应闭环转向到**世界方向角**（弧度）。

        接近目标时自动缩小脉冲；检测到过冲就回退（避免极限环振荡）。
        **v4：`actual_deg` / `err_deg` 返回"停稳后重新读取"的最终朝向**
        （v3 返回的是过程最优值，实测可差 9~14°）。`best_err_deg` 仍给过程最优值。

        返回 ``{'ok','actual_deg','err_deg','best_err_deg','final_err_deg'}``。
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
        final_err = None
        final_rot = None
        if settle:
            try:
                await self.bhv.wait_still(pid, max_wait=0.5)
            except Exception:
                await asyncio.sleep(0.1)
            final_rot = await self.rotation(pid)
            if final_rot is not None:
                final_err = (target - final_rot + math.pi) % (2 * math.pi) - math.pi
        use = final_rot if final_rot is not None else (best[1] if best else None)
        ue = final_err if final_err is not None else (-best[0] if best else None)
        if use is None:
            return {'ok': False, 'actual_deg': None, 'err_deg': None,
                    'best_err_deg': None, 'final_err_deg': None}
        ok = abs(ue) <= tol + math.radians(1.0)
        return {'ok': bool(ok),
                'actual_deg': math.degrees(_wrap_pi(use - math.pi / 2)),
                'err_deg': -math.degrees(ue),
                'best_err_deg': math.degrees(best[0]) if best else None,
                'final_err_deg': math.degrees(ue),
                'fallback': True}

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
        start = await self.position(pid)
        arrived = await self.navigate_to(pid, target_xy, arrival=arrival, timeout=timeout)
        pos = await self.position(pid)
        dist = None
        if pos is not None:
            dist = math.hypot(pos[0] - target_xy[0], pos[1] - target_xy[1])
        moved = None
        if start is not None and pos is not None:
            moved = math.hypot(pos[0] - start[0], pos[1] - start[1])
        return {'arrived': bool(arrived), 'pos': pos, 'from': start, 'dist': dist,
                'moved': moved, 'elapsed': round(_t.time() - t0, 2)}

    async def navigate_segment(self, pid, target_xy, max_seconds=2.2, arrival=1.5):
        """有界单段寻路：最多走 max_seconds 秒（超时取消并松键），返回结果字典。"""
        return await self.navigate_to_ex(pid, target_xy, arrival=arrival,
                                         timeout=float(max_seconds))

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
        off = self._muzzle(await self.weapon(pid))
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
        quality = (float(traveled) + 30.0 * float(nb)
                   + max(0.0, 20.0 - min(float(risk), 20.0)) * 5.0)
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
                'quality': float(quality),
                'reachable': reach}

    async def scan(self, pid, step_deg=30, max_traveled=None, robust_deg=None,
                   include_targets=True):
        """静默扫描 360°：每项含 ``hit_pid/self_hit/bounces/traveled/robust/quality/
        path/self_risk/self_risk_ok``。

        ``quality = traveled + 30×bounces``（越小越可靠）。
        ``self_risk < 4m`` 表示反弹后路径贴近自己 → 有自伤风险。
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return []
        off = self._muzzle(await self.weapon(pid))
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
            item['quality'] = (float(item['traveled']) + 30.0 * float(item['bounces'])
                               + max(0.0, 20.0 - min(float(item['self_risk']), 20.0)) * 5.0)
            out.append(item)
        # ★ v6：固定步长网格不含"正对目标"的直线角（命中窗口 1~3° 时会整窗漏掉）
        if include_targets:
            seen = {round(math.degrees(it['angle_rad']), 2) for it in out}
            for tp in await self._all_pids():
                if str(tp) == str(pid):
                    continue
                tpos = await self.bhv.get_position(tp)
                if tpos is None:
                    continue
                base = math.degrees(math.atan2(tpos[1] - pos[1], tpos[0] - pos[0]))
                for delta in (0.0, 1.5, -1.5):
                    deg = base + delta
                    key = round(deg, 2)
                    if key in seen:
                        continue
                    seen.add(key)
                    d = await self.bhv._raycast_path(
                        pos[0], pos[1], float(deg), 0.0, 0.0,
                        max_bounces=MAX_PATH_BOUNCES, max_dist=float(max_traveled) * 1.5,
                        target_tank_id=None, origin_offset=off)
                    item = self._mk_ray(pid, math.radians(deg), d, float(max_traveled), pos)
                    item['robust'] = None
                    item['quality'] = (float(item['traveled']) + 30.0 * float(item['bounces'])
                                       + max(0.0, 20.0 - min(float(item['self_risk']), 20.0)) * 5.0)
                    out.append(item)
        return out

    # 开火策略分级：strict（默认，保守）/ relaxed（多反弹仍可搏）/ loose（更宽）
    PROFILES = {
        'strict':  {'max_bounces': 5, 'max_traveled': 110.0, 'long_bounces': 4,
                    'long_traveled': 80.0, 'risk_short': 4.0, 'risk_long': 20.0},
        'relaxed': {'max_bounces': 8, 'max_traveled': 140.0, 'long_bounces': 4,
                    'long_traveled': 80.0, 'risk_short': 4.0, 'risk_long': 6.0},
        'loose':   {'max_bounces': 12, 'max_traveled': 180.0, 'long_bounces': 4,
                    'long_traveled': 80.0, 'risk_short': 3.0, 'risk_long': 4.0},
    }

    def is_good_candidate(self, item, pid, target_id=None, profile='strict',
                          max_bounces=None, max_traveled=None, min_self_risk=None):
        """候选是否"可安全开火"（按 profile 分级；参数可覆盖）：

        * 命中对手（非自己）且 `self_hit == False`；
        * `bounces <= max_bounces`；`traveled <= max_traveled`；
        * 自伤风险：短弹道需 `>= risk_short`，长弹道（bounces>=long_bounces 或
          traveled>=long_traveled）需 `>= risk_long`。

        profile：``strict``(默认) / ``relaxed`` / ``loose``。
        窄地图里 strict 常全灭，可用 relaxed 搏一搏（实测多 2 杀）。
        """
        pol = dict(self.PROFILES.get(profile, self.PROFILES['strict']))
        if max_bounces is not None:
            pol['max_bounces'] = int(max_bounces)
        if max_traveled is not None:
            pol['max_traveled'] = float(max_traveled)
        if min_self_risk is not None:
            pol['risk_short'] = float(min_self_risk)
            pol['risk_long'] = float(min_self_risk)
        if not item or not item.get('hit_pid'):
            return False
        if str(item['hit_pid']) == str(pid):
            return False
        if target_id is not None and str(item['hit_pid']) != str(target_id):
            return False
        if item.get('self_hit'):
            return False
        nb = int(item.get('bounces') or 0)
        tv = float(item.get('traveled') or 0)
        scale = None if min_self_risk is not None else 'default'
        return bool(nb <= int(pol['max_bounces']) and tv <= float(pol['max_traveled'])
                    and self.candidate_tier(item, pid, target_id, profile,
                                            risk_scale=scale) is not None)

    def candidate_tier(self, item, pid, target_id=None, profile='strict', risk_scale='default'):
        """判定候选所属档位并检查自伤风险 → 返回档位字符串或 None。"""
        pol = dict(self.PROFILES.get(profile, self.PROFILES['strict']))
        nb = int(item.get('bounces') or 0)
        tv = float(item.get('traveled') or 0)
        risk = float(item.get('self_risk') or 0.0)
        need = float(pol['risk_long']) if (nb >= int(pol['long_bounces']) or tv >= float(pol['long_traveled'])) else float(pol['risk_short'])
        # v7: 多反弹/长行程的自伤阈值随几何放大（可配置/可关闭）
        scale = self.risk_scale if risk_scale == 'default' else risk_scale
        if scale is not None:
            try:
                need = max(need, 4.0 + float(scale[0]) * nb + float(scale[1]) * tv)
            except Exception:
                pass
        if risk < need:
            return None
        if nb > int(pol['max_bounces']) or tv > float(pol['max_traveled']):
            return None
        return profile

    async def scan_adaptive(self, pid, target_id=None, steps=(30, 10, 5, 2),
                            robust_deg=None, profiles=('strict', 'relaxed', 'loose'),
                            merge=True, stop_early=False, steps_limit=None):
        """粗→细 + **多档策略**扫描。

        依次对每个 profile 扫描全部步长并合并候选；一旦某档找到候选就返回该档
        （默认 strict → relaxed → loose），每个候选带 ``tier`` 字段。
        返回 ``(candidates, used_steps)``；候选按 quality 升序、按角度去重。
        """
        used = []
        for prof in profiles:
            merged = {}
            for st in steps:
                if steps_limit is not None and len([u for u in used if u == int(st)]) >= 0 and int(st) > int(steps_limit) and merged:
                    break
                if int(st) not in used:
                    used.append(int(st))
                res = await self.scan(pid, step_deg=st, robust_deg=robust_deg)
                cands = [r for r in res
                         if self.is_good_candidate(r, pid, target_id, profile=prof)]
                if cands and stop_early:
                    cands.sort(key=lambda r: r['quality'])
                    for c in cands:
                        c['tier'] = prof
                    return cands, used
                for c in cands:
                    key = round(math.degrees(c['angle_rad']), 2)
                    prev = merged.get(key)
                    if prev is None or c['quality'] < prev['quality']:
                        c['tier'] = prof
                        merged[key] = c
                if stop_early and not merge and cands:
                    break
            if merged:
                out = sorted(merged.values(), key=lambda r: r['quality'])
                return out, used
        return [], used

    async def find_aim(self, pid, target_xy, target_id=None, require_bounce=False,
                       max_traveled=None, max_bounces=None):
        await self._require(pid)
        off = self._muzzle(await self.weapon(pid))
        if max_traveled is None:
            max_traveled = await self.bullet_range(pid)
        cap = MAX_PATH_BOUNCES if max_bounces is None else int(max_bounces)
        return await self.bhv.find_aim_angle_raycast(
            pid, tuple(target_xy), target_id=target_id, require_bounce=require_bounce,
            max_traveled=float(max_traveled), max_bounces=cap, muzzle_offset=off)

    async def target_velocity(self, target_id):
        """目标坦克的线速度 (vx, vy)（米/秒）；读不到返回 None。

        v14：B2D 速度偶发读到 (0,0)（抽样瞬间），此时用两点位移兜底。
        """
        v = None
        try:
            v = await self.bhv.get_tank_velocity(target_id)
        except Exception:
            v = None
        try:
            zero = (not v) or (abs(float(v[0])) < 1e-6 and abs(float(v[1])) < 1e-6)
        except Exception:
            zero = True
        if not zero:
            return (float(v[0]), float(v[1]))
        p1 = await self.position(target_id)
        await asyncio.sleep(0.2)
        p2 = await self.position(target_id)
        if p1 is None or p2 is None:
            return v
        dt = 0.2
        return ((p2[0] - p1[0]) / dt, (p2[1] - p1[1]) / dt)

    async def lead_target(self, pid, target_id, bullet_speed=None, max_iter=3,
                          max_shift_m=90.0, use_path=True, tol_m=1.7):
        """移动目标提前量（含收敛/可拦截判定）。

        返回 ``{'pos','velocity','shift','interceptable','converged','reason'}``：
        * ``pos``：预测命中点；``shift``：预测点相对目标当前位置的位移（米）
        * ``interceptable``：False 表示"追不上/发散"（目标速度 >= 0.95×弹速，或迭代发散），
          此时 ``pos`` 为目标当前位置，调用方应换位/等对方转向，而不是硬打；
        * ``converged``：迭代是否稳定（相邻两次 shift 变化 < 0.5m）。
        """
        await self._require(pid)
        tpos = await self.position_confirm(target_id)
        if tpos is None:
            return None
        if bullet_speed is None:
            bullet_speed = BULLET_SPEED
        bs = float(bullet_speed)
        vel = await self.target_velocity(target_id)
        speed = 0.0
        if vel:
            try:
                speed = math.hypot(float(vel[0]), float(vel[1]))
            except Exception:
                speed = 0.0
        if bs > 0 and speed >= bs * 0.95:
            return {'pos': (float(tpos[0]), float(tpos[1])), 'velocity': vel,
                    'shift': 0.0, 'interceptable': False, 'converged': False,
                    'reason': 'target_too_fast'}
        try:
            pred = await self.bhv.predict_lead_target(pid, tuple(tpos),
                                                      target_id=target_id,
                                                      bullet_speed=bs,
                                                      max_iter=int(max_iter))
        except Exception:
            pred = None
        if pred is None:
            return {'pos': (float(tpos[0]), float(tpos[1])), 'velocity': vel,
                    'shift': 0.0, 'interceptable': speed <= bs * 0.5,
                    'converged': False, 'reason': 'no_velocity'}
        # v14：用**弹道路径长度**（而不是直线距离）迭代修正飞行时间
        # （反弹路径 40m 时飞行时间可能比直线估算长 1~4s；忽略会把提前量打少）
        if use_path and speed > 0.5:
            try:
                for _ in range(int(max_iter)):
                    cands = await self.find_aim_at_point(
                        pid, tuple(pred), tol_m=float(tol_m) * 1.5,
                        steps=(5,), profile='relaxed', require_hit_tank=False)
                    if not cands:
                        break
                    trav = float(cands[0].get('traveled') or 0.0)
                    if trav <= 0:
                        break
                    t_flight = trav / bs
                    new_pred = (tpos[0] + float(vel[0]) * t_flight,
                                tpos[1] + float(vel[1]) * t_flight)
                    if math.hypot(new_pred[0] - pred[0], new_pred[1] - pred[1]) < 0.4:
                        pred = new_pred
                        break
                    pred = new_pred
            except Exception:
                pass
        shift = math.hypot(float(pred[0]) - tpos[0], float(pred[1]) - tpos[1])
        # 发散保护：提前量不应超过"目标沿速度跑 3 倍飞行时间"的常识上界
        if shift > float(max_shift_m) or (speed > 0 and shift > 3.0 * speed):
            return {'pos': (float(tpos[0]), float(tpos[1])), 'velocity': vel,
                    'shift': shift, 'interceptable': False, 'converged': False,
                    'reason': 'diverge'}
        return {'pos': (float(pred[0]), float(pred[1])), 'velocity': vel,
                'shift': float(shift),
                'interceptable': True,
                'converged': bool(shift < 0.5 * max(1.0, speed)),
                'reason': None}

    async def aim_at_moving(self, pid, target_id, fire_sec=0.6, bullet_speed=None,
                            tol_m=1.7, max_attempts=3, **kw):
        """对**移动目标**开火：lead_target → **对提前点选角** → 实际朝向复核（路径经过提前点）→ fire。

        与 ``aim_and_fire`` 的区别：瞄准的是**预测命中点**而不是目标当前位置
        （打移动靶时后者几乎必空）。返回 ``{'fired','reason','angle_deg','lead',
        'path_dist','traveled','flight_time','verify','attempt'}``。
        """
        await self._require(pid)
        lead = await self.lead_target(pid, target_id, bullet_speed=bullet_speed)
        if lead is None:
            return {'fired': False, 'reason': 'no_target', 'lead': None}
        if not lead.get('interceptable'):
            return {'fired': False, 'reason': 'not_interceptable', 'lead': lead,
                    'angle_deg': None, 'path_dist': None, 'traveled': None,
                    'flight_time': None, 'verify': None, 'attempt': 0}
        point = lead['pos']
        for attempt in range(1, int(max_attempts) + 1):
            cands = await self.find_aim_at_point(pid, point, tol_m=tol_m,
                                                 profile='relaxed',
                                                 require_hit_tank=False)
            if not cands:
                cands = await self.find_aim_at_point(pid, point, tol_m=float(tol_m) * 2.0,
                                                     profile='relaxed',
                                                     require_hit_tank=False)
            if not cands:
                return {'fired': False, 'reason': 'no_aim_at_lead', 'lead': lead,
                        'angle_deg': None, 'path_dist': None, 'traveled': None,
                        'flight_time': None, 'verify': None, 'attempt': attempt}
            best = cands[0]
            await self.turn_to_angle(pid, best['angle_rad'], tol_deg=float(kw.get('tol_deg', 1.2)))
            cur = await self.rotation(pid)
            if cur is None:
                continue
            actual = cur - math.pi / 2
            v = await self.raycast(pid, actual)
            if v is None:
                continue
            pd = self.path_dist_to_point(v, point)
            ok = bool(pd <= float(tol_m) * 1.3 and not v.get('self_hit')
                      and v.get('self_risk_ok', True))
            if not ok:
                continue
            ft = await self.flight_time(pid, float(v.get('traveled') or 0.0))
            await self.fire(pid, fire_sec)
            return {'fired': True, 'reason': None,
                    'angle_deg': math.degrees(actual), 'lead': lead,
                    'path_dist': float(pd), 'traveled': float(v.get('traveled') or 0.0),
                    'flight_time': ft, 'verify': v, 'attempt': attempt}
        return {'fired': False, 'reason': 'verify_failed', 'lead': lead,
                'angle_deg': None, 'path_dist': None, 'traveled': None,
                'flight_time': None, 'verify': None, 'attempt': int(max_attempts)}

    async def flight_time(self, pid, traveled_m):
        """按当前武器弹速估算飞行时间（秒）。"""
        try:
            w = await self.weapon(pid)
            sp = WEAPON_SPEED.get(int(w), 18.0) if w is not None else 18.0
        except Exception:
            sp = 18.0
        if not sp or sp <= 0:
            return None
        return float(traveled_m) / float(sp)

    @staticmethod
    def path_dist_to_point(path_or_item, point_xy):
        """折线路径到某点的最小距离（米）。传 ``raycast`` 返回项或 path 列表均可。"""
        try:
            path = path_or_item.get('path') if isinstance(path_or_item, dict) else path_or_item
            if not path or len(path) < 2:
                if path and len(path) == 1:
                    return math.hypot(path[0]['x'] - point_xy[0], path[0]['y'] - point_xy[1])
                return 1e9
            best = 1e9
            for a, b in zip(path, path[1:]):
                d = Kit._seg_dist(point_xy[0], point_xy[1], a['x'], a['y'], b['x'], b['y'])
                if d < best:
                    best = d
            return best
        except Exception:
            return 1e9

    async def find_aim_at_point(self, pid, point_xy, tol_m=1.7, steps=(30, 10, 5, 2),
                               max_traveled=None, profile='relaxed',
                               require_hit_tank=True):
        """对**任意预测点**找开火角（移动目标必需）。

        与 ``find_aim`` 的区别：不要求射线命中目标坦克本体，而是要求
        **弹道折线经过 point_xy 附近**（``path_dist_to_point <= tol_m``）。
        返回候选列表（按 path 距离 + quality 排序），每项含 ``path_dist``。

        ``require_hit_tank=False`` 时允许"路径经过但没命中坦克"的解（打提前量时正常）。
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return []
        off = self._muzzle(await self.weapon(pid))
        if max_traveled is None:
            max_traveled = await self.bullet_range(pid)
        out = []
        for st in steps:
            for deg in range(0, 360, max(1, int(st))):
                d = await self.bhv._raycast_path(
                    pos[0], pos[1], float(deg), 0.0, 0.0,
                    max_bounces=MAX_PATH_BOUNCES, max_dist=float(max_traveled) * 1.5,
                    target_tank_id=None, origin_offset=off)
                it = self._mk_ray(pid, math.radians(deg), d, float(max_traveled), pos)
                pd = self.path_dist_to_point(it, tuple(point_xy))
                if pd > float(tol_m):
                    continue
                if it.get('self_hit'):
                    continue
                if not it.get('self_risk_ok', True):
                    continue
                # v14：预测点弹道没有 hit_pid，不能用 candidate_tier；
                # 直接用几何放大阈值，避免 70m+ 多反弹迟到自伤。
                _nb = int(it.get('bounces') or 0)
                _tv = float(it.get('traveled') or 0)
                _need = 4.0 + 0.6 * _nb + 0.03 * _tv
                if float(it.get('self_risk') or 0.0) < _need:
                    continue
                if require_hit_tank and not it.get('hit'):
                    continue
                it['path_dist'] = float(pd)
                it['tier'] = profile
                out.append(it)
            if out:
                break
        # 去重（同角度保留最优）后按 (路径距离, quality) 排序
        seen = {}
        for it in out:
            key = round(math.degrees(it['angle_rad']), 1)
            if key not in seen or (it['path_dist'], it['quality']) < (seen[key]['path_dist'], seen[key]['quality']):
                seen[key] = it
        res = sorted(seen.values(), key=lambda x: (x['path_dist'], x['quality']))
        return res

    async def reachable_windows(self, pid, target_id=None, center_deg=0.0,
                                span_deg=360.0, step_deg=0.5,
                                quantum_deg=TURN_QUANTUM_DEG, profile=None,
                                sort='distance', min_width_deg=1.5):
        """扫描命中窗口，**并判断每个窗口里是否有"转向可达角"**（v8 核心）。

        转向量子约 4.775°：若窗口宽度 < 量子且不含网格点，则**物理上打不中**。
        返回按"可达网格角离当前朝向最近"排序的窗口列表，每项::

            {'window_start_deg','window_end_deg','width_deg','grid_deg',
             'tier','item'|None,'feasible': bool}

        ``item`` 是网格角上的 ``raycast`` 结果（保证命中，或 None 不可达）。
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return []
        cur = await self.rotation(pid)
        cur_world = math.degrees(_wrap_pi(cur - math.pi / 2)) if cur is not None else 0.0
        off = self._muzzle(await self.weapon(pid))
        rng = await self.bullet_range(pid)
        profs = ([profile] if profile else ['strict', 'relaxed', 'loose'])
        n = max(1, int(round(float(span_deg) / float(step_deg))))
        hits = []
        for i in range(n):
            deg = (float(center_deg) - float(span_deg) / 2.0 + i * float(step_deg)) % 360.0
            d = await self.bhv._raycast_path(
                pos[0], pos[1], deg, 0.0, 0.0,
                max_bounces=MAX_PATH_BOUNCES, max_dist=rng * 1.5,
                target_tank_id=None, origin_offset=off)
            it = self._mk_ray(pid, math.radians(deg), d, rng, pos)
            tier = None
            for prof in profs:
                if self.is_good_candidate(it, pid, target_id, profile=prof):
                    tier = prof
                    break
            if tier:
                hits.append([deg, tier])
        if not hits:
            return []
        hits.sort(key=lambda h: h[0])
        groups = []
        curg = [hits[0]]
        for prev, nxt in zip(hits, hits[1:]):
            if nxt[0] - prev[0] <= float(step_deg) * 1.5:
                curg.append(nxt)
            else:
                groups.append(curg)
                curg = [nxt]
        groups.append(curg)
        # 跨 0/360 合并（v10 修复：必须保持 [deg, tier] 结构；v9 之前会把 float 混进列表）
        try:
            if len(groups) > 1:
                first_deg = groups[0][0][0]
                last_deg = groups[-1][-1][0]
                if (360.0 - last_deg) + first_deg <= float(step_deg) * 1.5:
                    merged = [[(d - 360.0), t] for d, t in groups[-1]] + groups[0]
                    groups = [merged] + groups[1:-1]
        except Exception:
            pass
        q = float(quantum_deg)
        out = []
        for g in groups:
            lo = g[0][0]
            hi = g[-1][0]
            width = (hi - lo) + float(step_deg)
            # 从当前朝向出发的可达角网格： cur_world + k*q
            k0 = math.ceil((lo - cur_world) / q - 1e-9)
            k1 = math.floor((hi - cur_world) / q + 1e-9)
            best = None
            center = (lo + hi) / 2.0
            for k in range(int(k0), int(k1) + 1):
                gd = cur_world + k * q
                dist = abs(gd - center)
                if best is None or dist < best[0]:
                    best = (dist, gd)
            tier = g[0][1]
            item = None
            # 无网格点也允许：窗口宽度 >= min_width_deg 时取窗口中心角（turn_precise 可达 ~1°）
            if best is None and width >= float(min_width_deg):
                best = (0.0, (lo + hi) / 2.0)
            if best is not None:
                gd = best[1] % 360.0
                d = await self.bhv._raycast_path(
                    pos[0], pos[1], gd, 0.0, 0.0,
                    max_bounces=MAX_PATH_BOUNCES, max_dist=rng * 1.5,
                    target_tank_id=None, origin_offset=off)
                item = self._mk_ray(pid, math.radians(gd), d, rng, pos)
                item['tier'] = tier
            out.append({'window_start_deg': lo % 360.0, 'window_end_deg': hi % 360.0,
                        'width_deg': width,
                        'grid_deg': (_wrap_pi(math.radians(best[1])) * 180.0 / math.pi
                                     if best else None),
                        'tier': tier,
                        'quality': (item.get('quality') if item else None),
                        'bounces': (item.get('bounces') if item else None),
                        'traveled': (item.get('traveled') if item else None),
                        'item': item,
                        'feasible': bool(item and item.get('hit')),
                        'grid_point': bool(best is not None and
                                           abs((lo + hi) / 2.0 - best[1]) < float(step_deg)),
                        'width_ok': bool(width >= float(min_width_deg))})
        def key(w):
            if w['grid_deg'] is None:
                return (2, 1e9, 1e9, 0)
            dx = abs(((w['grid_deg'] - cur_world + 180.0) % 360.0) - 180.0)
            q = w['quality'] if w['quality'] is not None else 1e9
            if sort == 'quality':
                return (0 if w['feasible'] else 1, q, dx, -w['width_deg'])
            return (0 if w['feasible'] else 1, dx, q, -w['width_deg'])
        out.sort(key=key)
        return out

    async def scan_region(self, pid, center_deg, span_deg=20.0, step_deg=0.5,
                          target_id=None, profile=None):
        """在指定角度区域细扫（0.5 度级），返回命中候选（按 quality 升序）。

        用于 scan_adaptive 的 2 度网格漏掉 0.5~1 度窄窗口的情况。
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return []
        off = self._muzzle(await self.weapon(pid))
        rng = await self.bullet_range(pid)
        profs = ([profile] if profile else ['strict', 'relaxed', 'loose'])
        out = []
        n = max(1, int(round(float(span_deg) / float(step_deg))))
        for k in range(-n, n + 1):
            deg = float(center_deg) + k * float(step_deg)
            d = await self.bhv._raycast_path(
                pos[0], pos[1], deg, 0.0, 0.0,
                max_bounces=MAX_PATH_BOUNCES, max_dist=rng * 1.5,
                target_tank_id=None, origin_offset=off)
            it = self._mk_ray(pid, math.radians(deg), d, rng, pos)
            for prof in profs:
                if self.is_good_candidate(it, pid, target_id, profile=prof):
                    it['tier'] = prof
                    out.append(it)
                    break
        out.sort(key=lambda r: r['quality'])
        return out

    async def fine_aim(self, pid, target_id, center_rad, span_deg=10.0, step_deg=0.25,
                       profile=None, max_bounces=None, max_traveled=None,
                       min_self_risk=None, prefer='window_center',
                       ref_bounces=None, ref_traveled=None, same_family=True,
                       snap=True, max_span_deg=120.0, expand_step_deg=1.0):
        """在 ``center_rad`` ±span_deg 内细采样，找**命中窗口**并返回窗口中心角。

        v5：不再返回"离中心最近的角"（窗口 1~3°、瞄准误差 1~2°，瞄边缘≈瞄外面），
        而是按 ``step_deg``（默认 0.25°）建窗口，选**离中心最近的窗口的中心**。

        ``prefer='nearest'`` 可恢复旧行为（返回离中心最近的可命中角）。
        找不到返回 None；返回项带 ``window``（窗口宽度，度）。
        """
        await self._require(pid)
        pos = await self.bhv.get_position(pid)
        if pos is None:
            return None
        profs = ([profile] if profile else ['strict', 'relaxed', 'loose'])
        off = self._muzzle(await self.weapon(pid))
        rng = await self.bullet_range(pid)
        n = max(1, int(round(float(span_deg) / float(step_deg))))
        hits = []          # (k, item)
        for k in range(-n, n + 1):
            ang = float(center_rad) + math.radians(k * float(step_deg))
            d = await self.bhv._raycast_path(
                pos[0], pos[1], math.degrees(ang), 0.0, 0.0,
                max_bounces=MAX_PATH_BOUNCES, max_dist=rng * 1.5,
                target_tank_id=None, origin_offset=off)
            it = self._mk_ray(pid, ang, d, rng, pos)
            ok_prof = None
            for prof in profs:
                if self.is_good_candidate(it, pid, target_id, profile=prof,
                                          max_bounces=max_bounces,
                                          max_traveled=max_traveled,
                                          min_self_risk=min_self_risk):
                    ok_prof = prof
                    break
            if ok_prof is None:
                continue
            it['tier'] = ok_prof
            # 同族约束：避免"转向没对准 → fine_aim 换成更长的另一条解"
            if same_family and ref_bounces is not None:
                if int(it['bounces']) > int(ref_bounces) + 1:
                    continue
            if same_family and ref_traveled is not None:
                if float(it['traveled']) > float(ref_traveled) * 1.25 + 10.0:
                    continue
            hits.append((k, it))
        if not hits:
            return None
        if prefer == 'nearest':
            k, it = min(hits, key=lambda p: (abs(p[0]), p[1]['quality']))
            it['window'] = float(step_deg)
            return it
        # 分组成连续窗口
        hits.sort(key=lambda p: p[0])
        windows = []
        cur = [hits[0]]
        for prev, nxt in zip(hits, hits[1:]):
            if nxt[0] - prev[0] == 1:
                cur.append(nxt)
            else:
                windows.append(cur)
                cur = [nxt]
        windows.append(cur)
        # 选"离中心最近的窗口的中心"
        def win_key(w):
            ks = [p[0] for p in w]
            c = (min(ks) + max(ks)) / 2.0
            return (abs(c), -(len(w)))
        w = min(windows, key=win_key)
        ks = [p[0] for p in w]
        mid_k = (min(ks) + max(ks)) / 2.0
        ang = float(center_rad) + math.radians(mid_k * float(step_deg))
        d = await self.bhv._raycast_path(
            pos[0], pos[1], math.degrees(ang), 0.0, 0.0,
            max_bounces=MAX_PATH_BOUNCES, max_dist=rng * 1.5,
            target_tank_id=None, origin_offset=off)
        it = self._mk_ray(pid, ang, d, rng, pos)
        it['window'] = float((max(ks) - min(ks) + 1) * float(step_deg))
        it['window_center_deg'] = math.degrees(ang)
        if snap:
            # v8：必须返回"转向网格角上、且该角 raycast 命中"的项；
            # 否则调用方会拿到 hit=False 的假解（v7 实测踩坑）。
            # v9：若在给定 span 内找不到，自动扩大搜索半径再试一次（实测转向
            #      可能偏出 ±10°，此时原窗口仍有可行网格角，只是不在附近）。
            spans = [min(float(span_deg) * 2.0, 60.0)]
            for extra in (120.0, float(max_span_deg) if max_span_deg else 0.0):
                if extra > spans[-1] + 1e-9:
                    spans.append(extra)
            for _span in spans:
                _step = (min(float(step_deg), 0.25) if _span <= 60.0
                         else float(expand_step_deg))
                wins = await self.reachable_windows(
                    pid, target_id, center_deg=it['window_center_deg'],
                    span_deg=_span, step_deg=_step, profile=profile,
                    sort='quality')
                for w in wins:
                    item = w.get('item')
                    if w.get('feasible') and item is not None and item.get('hit'):
                        item['window'] = w['width_deg']
                        item['window_center_deg'] = it['window_center_deg']
                        item['snapped_deg'] = w['grid_deg']
                        item['snap_offset_deg'] = None
                        item['search_span_deg'] = _span
                        return item
            return None
        return it

    async def aim_and_fire(self, pid, target_xy, target_id=None, fire_sec=0.6,
                           tol_deg=1.8, robust_deg=None, self_risk_ok=True,
                           max_attempts=3, fine_search=True, fine_span_deg=10.0):
        """转向 + 在实际朝向复核弹道（含自伤风险检查）→ 通过才开火。

        v4 在复核失败时增加**就近微调**：在当前实际朝向上细搜 ±fine_span_deg，
        找到"命中目标且安全"的最近角度 → 小角度转过去 → 再复核 → 开火。
        （命中窗口 1~3° 而转向分辨率 1~5°，只靠一次转向很难正好对准。）

        返回 ``{'fired','reason','angle_deg','verify','attempt','fine'}``。
        """
        await self._require(pid)
        for attempt in range(1, int(max_attempts) + 1):
            ang = await self.find_aim(pid, target_xy, target_id=target_id)
            if ang is None:
                return {'fired': False, 'reason': 'no_aim', 'angle_deg': None,
                        'verify': None, 'attempt': attempt, 'fine': False}
            await self.turn_to_angle(pid, ang, tol_deg=tol_deg)
            cur = await self.rotation(pid)
            if cur is None:
                return {'fired': False, 'reason': 'no_pose', 'angle_deg': None,
                        'verify': None, 'attempt': attempt, 'fine': False}
            actual = cur - math.pi / 2
            v = await self.raycast(pid, actual)
            ok = bool(v and v.get('hit') and str(v.get('hit_pid')) == str(target_id)
                      and not v.get('self_hit'))
            if ok and self_risk_ok and not v.get('self_risk_ok', True):
                ok = False
            fine_used = False
            if not ok and fine_search:
                best = await self.fine_aim(pid, target_id, actual, span_deg=fine_span_deg)
                if best is not None:
                    await self.turn_to_angle(pid, best['angle_rad'], tol_deg=tol_deg)
                    cur = await self.rotation(pid)
                    if cur is not None:
                        actual = cur - math.pi / 2
                        v = await self.raycast(pid, actual)
                        ok = bool(v and v.get('hit') and str(v.get('hit_pid')) == str(target_id)
                                  and not v.get('self_hit'))
                        if ok and self_risk_ok and not v.get('self_risk_ok', True):
                            ok = False
                        fine_used = True
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
                    'verify': v, 'attempt': attempt, 'fine': fine_used}
        return {'fired': False, 'reason': 'verify_failed', 'angle_deg': None,
                'verify': None, 'attempt': int(max_attempts), 'fine': False}
