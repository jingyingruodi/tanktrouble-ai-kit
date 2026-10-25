#!/usr/bin/env python3
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
# -*- coding: utf-8 -*-
"""
agent_ai.py —— TankTrouble 基础行为 AI（单文件）
================================================

行为循环（严格按任务书）：

  1. 确定"自己的角色"：``kit.roles()`` + ``kit.my_pid(role)``（默认 arrows）。
     之后**所有**控制指令只对这个 pid 下发 —— 见 ``_own()`` 硬保险。
  2. 主循环：
     a. **静默扫描** ``kit.scan(pid, step_deg=30)``：一圈里找
        ``hit_pid 属于其他角色 且 self_hit == False`` 的朝向，多个候选取
        ``traveled`` 最短的。
     b. 找到 → 立刻 ``stop``（停止移动）→ ``turn_to_angle`` → ``fire`` →
        回到 2a。
     c. 没找到 → ``nearest_opponent`` 找最近的其他角色，``navigate_to``
        **分段**寻路靠近；每段结束立刻回到 2a 重新静默扫描，一旦发现可命中
        方向就停/转/开火。
  3. 循环到运行时长（``--seconds``，默认 60）为止。
  4. 回合切换 / 角色暂时不在场：``position() is None``、回合非 in round、
     ``scan()`` 返回空 —— 打印日志，等 1 秒重试，绝不崩溃。

安全约定（只控自己的角色）
--------------------------
* ``self.pid`` 只在 ``resolve_me()`` 里由 ``kit.roles()`` / ``kit.my_pid()``
  赋值，且必须出现在 "可控角色" 表里。
* 每个控制动作都经过 ``self._own(pid)``：pid 与本 AI 的角色不一致就抛
  ``RuntimeError``，请求根本到不了游戏。日志里每条动作都带 pid，便于核对。
* 开火前用官方弹道 ``raycast(pid, angle)`` 复核一次（转向期间目标可能移动），
  复核不通过就不开火、重新扫描。

用法
----
    python agent_ai.py --seconds 60
    python agent_ai.py --seconds 60 --role arrows --step-deg 30 --fire-sec 0.6
"""
import argparse
import asyncio
import math
import sys
import time

from ttkit import Kit

# ─────────────────────────── 小工具 ───────────────────────────

def deg(rad):
    """弧度 → [0,360) 的度数（只用于日志）。"""
    return math.degrees(float(rad)) % 360.0


def now():
    return time.monotonic()


# ─────────────────────────── 行为 AI ───────────────────────────

class AgentAI:
    """基础行为 AI：扫描 → 停/转/开火，否则寻路逼近最近对手。"""

    def __init__(self, kit, role='arrows', step_deg=30,
                 fire_sec=0.6, post_fire_wait=0.35, verbose=True):
        self.kit = kit
        self.role = role
        self.step_deg = int(step_deg)
        self.fire_sec = float(fire_sec)
        self.post_fire_wait = float(post_fire_wait)
        self.verbose = verbose

        self.pid = None          # ← 唯一的"我的角色"，只有这里能出现我的 pid
        self.roles = {}
        self.stats = {
            'scans': 0, 'shots': 0, 'moves': 0, 'skips': 0,
            'cmds': 0, 'waits': 0, 'errors': 0, 'rounds': 0,
        }
        self.cmds_by_pid = {}     # 控制指令计数（用于证明"只控了自己的 pid"）
        self._last_round = None
        # ★ 本回合"已试过但没打中"的角度（度，取整到 step_deg）：避免对同一条
        #   打不中的解无限开火（黑盒测试中曾 70s 对同一角度打 55 发）。
        self._tried = set()

    # ── 日志 ──
    def log(self, msg):
        print(msg, flush=True)

    # ── 角色识别 ──
    async def resolve_me(self):
        """确定"自己的角色"：只用 kit.roles() / kit.my_pid()，并做交叉校验。"""
        roles = await self.kit.roles()
        self.roles = roles
        if not roles:
            self.log('[role] 暂无可控角色（回合切换中？）')
            return None
        # 任务要求：默认 arrows 角色
        pid = await self.kit.my_pid(self.role)
        if pid is None:
            self.log('[role] my_pid() 返回 None')
            return None
        pid = str(pid)
        real_role = roles.get(pid)
        if real_role != self.role:
            self.log(f'[role] ⚠️ 没有 role={self.role} 的角色，my_pid() 给了 '
                     f'pid={pid} (role={real_role})')
        self.pid = pid
        self.log(f'[role] 可控角色={roles} → 本 AI 只操作 pid={pid} '
                 f'(role={real_role})')
        return pid

    async def ensure_role(self):
        """每轮复核：自己的 pid 是否仍可控；丢了就重新解析（只重新解析，不乱发指令）。"""
        try:
            roles = await self.kit.roles()
        except Exception as e:
            self.log(f'[role] roles() 失败：{type(e).__name__}: {e}')
            return False
        self.roles = roles
        if self.pid in roles:
            return True
        self.log(f'[role] ⚠️ pid={self.pid} 不在可控角色表 {roles} 中，重新解析')
        return bool(await self.resolve_me())

    # ── 硬保险：唯一控制入口 ──
    def _own(self, pid):
        """所有控制指令的唯一入口：不是"我"的 pid 一律拒绝（请求到不了游戏）。"""
        if self.pid is None:
            raise RuntimeError('拒绝操作：自己的 pid 还没确定')
        if str(pid) != str(self.pid):
            raise RuntimeError(f'拒绝操作：pid={pid} 不是本 AI 的角色 {self.pid}')
        if self.roles and str(pid) not in self.roles:
            raise RuntimeError(f'拒绝操作：pid={pid} 不在可控角色表 {list(self.roles)} 中')
        self.stats['cmds'] += 1
        self.cmds_by_pid[str(pid)] = self.cmds_by_pid.get(str(pid), 0) + 1
        return str(pid)

    # ── 控制包装（一律走 _own） ──
    async def ctl_stop(self):
        await self.kit.stop(self._own(self.pid))

    async def ctl_turn(self, ang):
        return await self.kit.turn_to_angle(self._own(self.pid), ang)

    async def ctl_fire(self):
        await self.kit.fire(self._own(self.pid), self.fire_sec)

    async def ctl_navigate(self, xy):
        return await self.kit.navigate_to(self._own(self.pid), xy)

    async def verify_shot(self, ang):
        """开火前复核：用**炮口出膛模型**推演一次官方弹道，确认第一个命中的还是目标。

        为什么不用 `kit.raycast()`（origin_offset=0，从车体中心出发）：
        实测发现射线从车体中心发出时，**沿着炮管方向**打出去的那一条会先在
        1.3m 处"命中自己"（bounces=0, traveled≈1.3），而转向到目标方向后炮管
        正好就是这个方向 —— 于是每次转向完成后复核都会误判成"会打自己"。
        真实弹丸是从炮口（车体中心前 ~2m）出膛、且不与自己的车体碰撞，
        所以这里用 origin_offset=2.0 复核：转向到 240° 后它给出
        hit=对手 / bounces=1 / traveled=44m，与扫描结果一致。
        复核失败（读不到弹道等）时保守地返回 None，由调用方决定。
        """
        pid = self._own(self.pid)
        pos = await self.kit.position(pid)
        if pos is None:
            return None
        try:
            d = await self.kit.bhv._raycast_path(
                pos[0], pos[1], math.degrees(float(ang)), 0.0, 0.0,
                origin_offset=2.0, max_bounces=26, max_dist=270.0,
                target_tank_id=None)
        except Exception as e:
            self.log(f'[aim] 炮口模型复核失败：{type(e).__name__}: {e}')
            return None
        if not isinstance(d, dict) or 'hitTank' not in d:
            return None
        hit_tank = d.get('hitTank')
        return {'hit_pid': hit_tank,
                'self_hit': str(hit_tank) == pid,
                'traveled': float(d.get('traveled') or 0),
                'bounces': int(d.get('bounces') or 0)}

    # ── 2a 静默扫描 ──
    async def scan_for_shot(self):
        """静默扫描一圈，返回可开火的候选（按 traveled 升序），没有则 []。"""
        pid = self._own(self.pid)
        # 对手集合（读失败时退化为 "非自己的 pid 即对手"）
        others = set()
        try:
            others = {str(p) for p in await self.kit.opponents(pid)}
        except Exception as e:
            self.log(f'[scan] opponents() 失败（退化判定）：{type(e).__name__}: {e}')

        scan = await self.kit.scan(pid, step_deg=self.step_deg)
        self.stats['scans'] += 1
        if not scan:
            return []
        if not others:
            self.log('[scan] ⚠️ 对手集合为空，退化为"hit_pid != 自己"')

        cands, self_hits, miss = [], 0, 0
        for c in scan:
            hp = c.get('hit_pid')
            if hp is None:
                miss += 1
                continue
            if str(hp) == pid:
                self_hits += 1
                continue
            # 判定：能击中其他角色，且自己不在弹道中间先被命中
            if c.get('self_hit'):
                self_hits += 1
                continue
            if others and str(hp) not in others:
                self_hits += 1          # 命中了自己人/未知坦克，不用
                continue
            # ★ 跳过本回合已试过的角度（避免对打不中的解无限开火）
            key = int(round(deg(c['angle_rad']) / max(1, self.step_deg)))
            if key in self._tried:
                continue
            cands.append(c)

        # 多个候选 → 选 traveled 最短（飞行时间短，最不易被躲 / 反弹自伤）
        cands.sort(key=lambda c: float(c.get('traveled') or 1e9))
        for c in cands[:3]:
            self.log(f"[scan] found angle={deg(c['angle_rad']):.1f}° hit={c['hit_pid']} "
                     f"traveled={float(c.get('traveled') or 0):.1f}m "
                     f"bounces={c.get('bounces')} self_hit={c.get('self_hit')}")
        if not cands:
            self.log(f'[scan] 一圈无解：{len(scan)} 个朝向中没有可命中对手且不自伤的'
                     f'（self_hit={self_hits} miss={miss}）')
        return cands

    # ── 2b 停 → 转 → 开火 ──
    async def engage(self, cands):
        """对候选角执行：立刻停止移动 → 转向 → 复核 → 开火。返回是否开火。"""
        pid = self._own(self.pid)
        # ① 立即停止移动
        await self.ctl_stop()
        self.log(f'[stop] pid={pid} ← 找到可命中朝向，立即停止移动')
        for cand in cands[:3]:
            ang = float(cand['angle_rad'])
            target = str(cand['hit_pid'])
            # ② 转向到该世界方向角
            aligned = await self.ctl_turn(ang)
            if not aligned:
                self.log(f'[turn] ✗ pid={pid} angle={deg(ang):.1f}° 未对准，换下一个候选')
                self.stats['skips'] += 1
                continue
            self.log(f'[turn] pid={pid} angle={deg(ang):.1f}° aligned={aligned}')
            # ③ 开火前复核：转向这段时间目标可能移动/自己被推走。
            #    用炮口出膛模型（见 verify_shot 的注释），确认第一个命中的仍是目标。
            chk = await self.verify_shot(ang)
            if chk is None:
                # 复核不可用（读不到弹道）：退化为"只确认目标还在场"，仍按任务开火
                now_pos = await self.kit.position(target)
                if now_pos is None:
                    self.log(f'[aim] 目标 {target} 已不在场 → 不开火')
                    self.stats['skips'] += 1
                    continue
                self.log(f'[aim] pid={pid} angle={deg(ang):.1f}° 复核不可用（保守放行）')
            elif str(chk['hit_pid']) != target or chk['self_hit']:
                self.log(f"[aim] 复核失效 pid={pid} angle={deg(ang):.1f}° "
                         f"hit={chk['hit_pid']} self_hit={chk['self_hit']} "
                         f"traveled={chk['traveled']:.1f}m → 不开火")
                self.stats['skips'] += 1
                continue
            # ④ 开火
            await self.ctl_fire()
            self.stats['shots'] += 1
            # ★ 记录"已试角度"：这一枪若没中，下一轮扫描不再选它（换解/换位）
            self._tried.add(int(round(deg(ang) / max(1, self.step_deg))))
            self.log(f"[fire] pid={pid} angle={deg(ang):.1f}° target={target} "
                     f"traveled={float(cand.get('traveled') or 0):.1f}m "
                     f"bounces={cand.get('bounces')}")
            # 等弹丸结算一下，避免在同一位置狂按（也顺便观测是否命中）
            await asyncio.sleep(self.post_fire_wait)
            try:
                gone = await self.kit.position(target) is None
            except Exception:
                gone = False
            if gone:
                self.log(f'[hit] 目标 target={target} 已不在场（本枪命中/击杀）')
            return True
        return False

    # ── 2c 没找到 → 寻路逼近最近对手（分段），段间继续扫描 ──
    async def hunt(self, deadline):
        pid = self._own(self.pid)
        near = await self.kit.nearest_opponent(pid)
        if not near:
            self.log('[move] 找不到最近的对手（回合切换/对手不在场），等 1s')
            self.stats['waits'] += 1
            await asyncio.sleep(1.0)
            return
        opid, (ox, oy), dist = near
        me = await self.kit.position(pid)
        if me is None:
            self.log('[move] 自己的位置读不到，等 1s')
            await asyncio.sleep(1.0)
            return
        dist = float(dist) or 1e-6
        # 分段寻路：目标直接取"对手当前所在格"（坦克一定站在可通行格中心附近，
        # 不会像自插值点那样落在墙里导致寻路绕远/原地打转）。
        # 用 asyncio.wait_for 把一次 navigate_to 切成 ~2-3s 的一段：一段走完
        # （或走超时）就松手回到 2a 重新静默扫描 —— 这就是"移动过程中继续扫描"。
        budget = max(0.8, min(3.0 if dist > 10 else 1.8, deadline - now()))
        self.log(f'[move] pid={pid} nearest={opid} dist={dist:.1f}m '
                 f'→ navigate_to ({ox:.0f},{oy:.0f}) 分段={budget:.1f}s')
        self.stats['moves'] += 1
        try:
            ok = await asyncio.wait_for(self.ctl_navigate((ox, oy)), timeout=budget)
            self.log(f'[move] navigate_to={ok}（回到静默扫描）')
        except asyncio.TimeoutError:
            self.log(f'[move] navigate_to 分段走完({budget:.1f}s) → 松手，回扫描')
            try:
                await self.ctl_stop()
            except Exception:
                pass

    # ── 单步 ──
    async def step(self, deadline):
        # 0) 回合状态：切回合时等待，不崩
        try:
            rstate = await self.kit.round_state()
        except Exception:
            rstate = None
        if rstate != self._last_round:
            self.log(f'[round] state={rstate}')
            self._last_round = rstate
            self.stats['rounds'] += 1
            # ★ 新回合：清空"已试角度"黑名单（避免对同一条打不中的解无限开火）
            self._tried.clear()
            self.log(f'[round] 黑名单已清空')
        if rstate in ('between rounds', 'counting down', 'ended', None):
            self.log(f'[wait] 回合状态 {rstate!r} → 等 1s')
            self.stats['waits'] += 1
            await asyncio.sleep(1.0)
            return

        # 1) 自己在场吗
        me = await self.kit.position(self._own(self.pid))
        if me is None:
            self.log(f'[wait] pid={self.pid} position()=None（角色不在场）→ 等 1s')
            self.stats['waits'] += 1
            await asyncio.sleep(1.0)
            return

        # 2a) 静默扫描
        cands = await self.scan_for_shot()
        if cands:
            # 2b) 有解 → 停/转/开火 → 回 2a
            if await self.engage(cands):
                return
            # 所有候选都被复核否决：按 2c 换个位置（否则会在原地反复扫不到结果）
            self.log('[aim] 本轮候选全部被复核否决 → 机动换位')

        # 2c) 无解 → 向最近对手分段寻路；段间回来重新扫描
        await self.hunt(deadline)

    # ── 主循环 ──
    async def run(self, seconds):
        t_start = now()
        deadline = t_start + float(seconds)
        self.log(f'[agent] start seconds={seconds} role={self.role} '
                 f'step_deg={self.step_deg} fire_sec={self.fire_sec}')

        # 先确定自己的角色（roles + my_pid），拿不到就等（不控任何角色）
        while now() < deadline:
            try:
                if await self.resolve_me():
                    break
            except Exception as e:
                self.log(f'[role] 解析失败：{type(e).__name__}: {e}')
            self.log('[role] 等 1s 后重试')
            await asyncio.sleep(1.0)

        while now() < deadline:
            try:
                if not await self.ensure_role():
                    self.stats['waits'] += 1
                    await asyncio.sleep(1.0)
                    continue
                await self.step(deadline)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 任何异常都不退出：等 1s 重试（回合切换/角色不在场是常态）
                self.stats['errors'] += 1
                self.log(f'[error] {type(e).__name__}: {e} → 等 1s 重试')
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    raise

        # 收尾：停住自己的角色（不是别人的）
        try:
            if self.pid:
                await self.ctl_stop()
                self.log(f'[agent] stop pid={self.pid}')
        except Exception as e:
            self.log(f'[agent] 收尾 stop 失败：{type(e).__name__}: {e}')

        elapsed = now() - t_start
        s = self.stats
        self.log(f'[sum] elapsed={elapsed:.1f}s pid={self.pid} role={self.role} '
                 f'scans={s["scans"]} shots={s["shots"]} moves={s["moves"]} '
                 f'skips={s["skips"]} waits={s["waits"]} errors={s["errors"]} '
                 f'rounds={s["rounds"]} 控制指令={s["cmds"]}')
        self.log(f'[sum] 控制指令按 pid 统计: {self.cmds_by_pid} ← '
                 f'只应出现 pid={self.pid}')
        return 0


# ─────────────────────────── 入口 ───────────────────────────

async def _amain(args):
    kit = await Kit(port=args.port).connect() if args.port else await Kit().connect()
    try:
        agent = AgentAI(kit, role=args.role, step_deg=args.step_deg,
                        fire_sec=args.fire_sec)
        return await agent.run(args.seconds)
    finally:
        try:
            await kit.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description='TankTrouble 基础行为 AI')
    ap.add_argument('--seconds', type=float, default=60.0, help='运行时长（秒），默认 60')
    ap.add_argument('--role', default='arrows', help='自己的角色名，默认 arrows')
    ap.add_argument('--step-deg', type=int, default=30, help='静默扫描步长，默认 30°')
    ap.add_argument('--fire-sec', type=float, default=0.6, help='开火按住时长，默认 0.6s')
    ap.add_argument('--port', type=int, default=None, help='CDP 端口（默认读 config.json）')
    args = ap.parse_args(argv)

    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print('\n[agent] 已手动停止', flush=True)
        return 130


if __name__ == '__main__':
    sys.exit(main())
