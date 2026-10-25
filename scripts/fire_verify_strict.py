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
严格火控验证：连续 N 回合，每回合必须在 3 分钟内击杀目标。
约束：最小射击距离（防怼脸），距离太近先机动拉开；无命中角回合自杀结束。

判定（满足全部才算合格）：
- 每回合统计攻击者击杀数（type0）。回合结束若击杀数未增 → 该回合失败
- 每回合从开始到击杀必须在 180s 内（超时 → 火控缺陷）
- 任何一回合自杀（击杀数未增但对面胜利数增）→ 火控缺陷
- 目标(键盘角色)击杀数不应增加（不能打队友）
- 无命中角（目标在几何死角，全迷宫无解）回合不计入、跳过

用法：
  python fire_verify_strict.py [port] [--out rec.jsonl] [--rounds 30] [--min-dist 15]
"""
import asyncio
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tank_behavior_template import TankBehavior
from recorder import CompressedRecorder

SCORE_KILL = 0


async def main():
    args = sys.argv[1:]
    # 支持 auto / TT_CDP_PORT：统一解析（见 cdp_tools/port_arg.py）
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cdp_tools')
    if _p not in sys.path:
        sys.path.insert(0, _p)
    from port_arg import port_from_args  # noqa: E402
    port = port_from_args(args)
    out = 'fire_strict.jsonl'
    target_rounds = 30
    min_dist = 15.0
    MAX_FIRE_DIST = 32.0  # 最大开火距离：长距离多反弹命中率低（实测 >35m 常失败）
    MIN_FIRE_DIST_NEAR = 20.0  # 贴脸拉开时，远距射击位至少距目标 20m
    # 弹丸【实际飞行长度】上限：多反弹路径越长，官方零宽射线与真实弹丸的偏差
    # 越大（实测 3 次反弹偏 >2m → 预测"安全"却回程自伤），且飞行时间越长
    # 移动目标越易躲开。超过此值宁可判无解去换位，不冒险开火。
    MAX_TRAVELED = 45.0
    # 反弹次数上限（默认 4 = 30 回合验收时的配置）。2026-10-16 在线实测：
    # 真实长弹道可达 180m / 十几次反弹（docs/10 §18）→ 用
    # `--max-bounces 25 --max-traveled 180` 做"放宽搜索"对比验收。
    MAX_BOUNCES_AIM = 4
    # 炮口出膛建模：真实弹丸从炮口（约 2m 前）出膛，而推演默认从车体中心。
    # 多反弹路径下这个起点偏移会连带平移反弹点 → "预测安全、真实自伤/打偏"。
    # 默认 0.0（=v3 验收行为）；用 --muzzle 2.0 开启模型改动做对比验收。
    MUZZLE_OFFSET = 0.0
    if '--out' in args:
        out = args[args.index('--out') + 1]
    if '--rounds' in args:
        target_rounds = int(args[args.index('--rounds') + 1])
    if '--min-dist' in args:
        min_dist = float(args[args.index('--min-dist') + 1])
    if '--muzzle' in args:
        MUZZLE_OFFSET = float(args[args.index('--muzzle') + 1])
    if '--max-bounces' in args:
        MAX_BOUNCES_AIM = int(args[args.index('--max-bounces') + 1])
    if '--max-traveled' in args:
        MAX_TRAVELED = float(args[args.index('--max-traveled') + 1])
    # ★ 2026-10-24 健壮性：全局截止时间 + 回合卡死保护
    #   背景：实测本脚本会在"单回合长时间不出结果"时一直等下去（20 分钟迷宫版本不变），
    #   而客户端并没有卡死（Phaser time 正常推进）——是脚本自身的等待/机动循环没有兜底。
    #   --deadline     整轮验证的总时长预算（秒，默认 900）
    #   --round-timeout 单回合超过这么多秒仍无"回合状态变化"→ 主动自杀结束该回合
    DEADLINE = 900.0
    ROUND_TIMEOUT = 240.0
    if '--deadline' in args:
        DEADLINE = float(args[args.index('--deadline') + 1])
    if '--round-timeout' in args:
        ROUND_TIMEOUT = float(args[args.index('--round-timeout') + 1])

    bhv = TankBehavior(port=port)
    await bhv.connect()
    await bhv.get_walls_cached(refresh=True)

    attacker = await bhv.pick_role_strict('arrows')
    target = await bhv.pick_role_strict('keyboard')
    if not attacker or not target:
        print(f'[strict] 缺少角色: arrows={attacker} keyboard={target}', flush=True)
        return 1
    print(f'[strict] 攻击者(arrows)={attacker} 目标(keyboard)={target}', flush=True)

    # 录制
    mpath = os.path.join(os.path.dirname(__file__), '..', 'monitor_server.py')
    m = re.search(r'STATE_JS = r"""(.*?)"""', open(mpath, encoding='utf-8').read(), re.S)
    state_js = m.group(1)
    rec = CompressedRecorder(out)

    async def recorder_task():
        while True:
            try:
                st = await bhv.ctl.cdp.eval(state_js)
                if isinstance(st, dict) and 'err' not in st:
                    rec.record(st, action=(bhv.ctl.current_actions() or None))
            except Exception:
                pass
            await asyncio.sleep(0.05)

    async def get_kills(pid):
        return await bhv.ctl.cdp.eval(
            f'(GameManager.getGameController().getScoreByPlayerIdAndType("{pid}",{SCORE_KILL}).getValue())')

    async def get_victories(pid):
        """胜利回合数（type1）。攻击者击杀数未增但对面胜利数增 = 攻击者自杀。"""
        return await bhv.ctl.cdp.eval(
            f'(GameManager.getGameController().getScoreByPlayerIdAndType("{pid}",1).getValue())')

    async def track_projectile(self, attacker_id, target_pos, target_id, duration=None):
        """开火后跟踪弹丸：返回弹丸距目标的最小距离（米）。

        用于诊断"预测命中但实际未中"——弹丸是否真的飞向目标。
        duration 默认按距离/弹速计算（覆盖全程飞行），或手动指定。
        返回 None 表示未检测到弹丸或目标已死。
        """
        if duration is None:
            ap = await self.get_position(attacker_id)
            if ap:
                duration = min(math.hypot(target_pos[0] - ap[0],
                                          target_pos[1] - ap[1]) / 18.0 + 1.5, 6.0)
            else:
                duration = 4.0
        track_js = """(() => {
          const gc = GameManager.getGameController() || GameManager.gameController;
          const ps = gc.getProjectiles ? gc.getProjectiles() : null;
          if (!ps) return '[]';
          const out = [];
          for (const k of Object.keys(ps)) {
            const p = ps[k];
            out.push({x: p.getX(), y: p.getY(), vx: p.getSpeedX(), vy: p.getSpeedY()});
          }
          return JSON.stringify(out);
        })()"""
        import json as _json
        min_d = 999
        deadline = asyncio.get_event_loop().time() + duration
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.1)
            try:
                raw = await bhv.ctl.cdp.eval(track_js)
                proj = _json.loads(raw)
            except Exception:
                proj = []
            if not proj:
                continue
            # 目标当前位置
            tp_now = await self.get_position(target_id)
            if not tp_now:
                return min_d if min_d < 999 else None
            for p in proj:
                dd = math.hypot(p['x'] - tp_now[0], p['y'] - tp_now[1])
                min_d = min(min_d, dd)
        return min_d if min_d < 999 else None

    async def find_shooting_positions(target_xy, min_dist, target_id=None):
        """生成目标周围多个候选射击位，并【用直线 RayCast 验证能看到目标】。

        只保留能直线看到目标的位置（无反弹、无墙挡），按距离升序。
        这样机动有的放矢，不会跑到死角浪费回合。
        """
        tiles = await bhv.get_maze_tiles()
        if not tiles:
            return []
        w, h, T = tiles['w'], tiles['h'], 10.0
        tx0, ty0 = target_xy
        cands = []
        # 生成候选：8 方向 × 3 距离（覆盖近中远）
        for ang_deg in range(0, 360, 45):
            for mult in (1.2, 1.6, 2.0):
                dist = max(min_dist * 1.2, 25) * mult
                best = None
                best_d = 1e9
                for gy in range(h):
                    for gx in range(w):
                        if tiles['tiles'][gy][gx][0] != 1:
                            continue
                        cx, cy = (gx + 0.5) * T, (gy + 0.5) * T
                        want = math.radians(ang_deg)
                        ang_diff = abs(((math.atan2(cy-ty0, cx-tx0) - want + math.pi) % (2*math.pi)) - math.pi)
                        d = math.hypot(cx-tx0, cy-ty0)
                        score = ang_diff * 100 + abs(d - dist) / 10
                        if score < best_d:
                            best_d = score
                            best = (cx, cy)
                if best and best not in cands:
                    cands.append(best)
        # 用 RayCast 验证：候选位能否【直线或反弹】命中目标
        # （含反弹，因为迷宫常需反弹路径）
        import math as _m
        valid = []
        for cx, cy in cands:
            d = math.hypot(cx - tx0, cy - ty0)
            if d < min_dist or d > MAX_FIRE_DIST:
                continue  # 距离约束（太近怼脸 / 太远多反弹命中率低）
            # 导航到该位后，用 find_aim_angle_at 检查能否反弹命中（要求≥1次反弹）
            ok_see = await bhv.find_aim_angle_at(cx, cy, tx0, ty0, target_id=target,
                                                 require_bounce=True)
            if ok_see is not None:
                valid.append((cx, cy))
        # 按距离升序（近的优先，减少机动耗时）
        valid.sort(key=lambda p: _m.hypot(p[0] - tx0, p[1] - ty0))
        return valid

    rec_task = asyncio.create_task(recorder_task())
    shot = 0
    try:
        successful_rounds = 0
        last_round = None
        round_base_kills = None  # 本回合开始时的击杀数
        round_kill_flag = False  # 本回合是否已击杀
        round_had_angle = False  # 本回合是否找到过命中角
        round_checked = 0        # 本回合尝试开火的次数
        round_skip_flag = False  # 本回合已判定"无解跳过"（主动自杀不计入）
        round_wait_spawn = 0     # 等待目标复活的循环计数
        round_force_maneuver = False  # 补射未击杀 → 强制机动换位
        maneuver_queue = []  # 待尝试的射击位队列
        tried_set = set()     # 已试过的位置

        # ★ 全局截止 + 单回合卡死保护（2026-10-24）
        run_t0 = asyncio.get_event_loop().time()
        last_round_change_t = run_t0

        while successful_rounds < target_rounds:
            rs = await bhv.get_round_state()
            now_t = asyncio.get_event_loop().time()
            if now_t - run_t0 > DEADLINE:
                print(f'[strict] ⏱ 达到全局截止 {DEADLINE:.0f}s：成功 {successful_rounds}/{target_rounds} 回合 → 结束',
                      flush=True)
                break
            if rs != last_round:
                last_round_change_t = now_t
            elif now_t - last_round_change_t > ROUND_TIMEOUT:
                print(f'[strict] ⏱ 回合已 {now_t - last_round_change_t:.0f}s 无状态变化 → 主动自杀结束本回合',
                      flush=True)
                try:
                    await bhv.suicide_by_bounce(attacker, timeout=20.0)
                except Exception as e:
                    print(f'[strict]   自杀结束失败: {e}', flush=True)
                last_round_change_t = asyncio.get_event_loop().time()
                continue
            # 回合状态变化检测
            if rs != last_round:
                if last_round == 'in round' and round_base_kills is not None:
                    # 上一回合结束，判定是否成功
                    if round_kill_flag:
                        successful_rounds += 1
                        print(f'[strict] ★ 回合 {successful_rounds}/{target_rounds} 有击杀 → 继续', flush=True)
                    else:
                        # 结算滞后：回合刚结束时击杀数/胜利数可能还没入账。
                        # 再查一次（等结算），确认真的没击杀才判定。
                        await asyncio.sleep(2.0)
                        ca2 = await get_kills(attacker)
                        va2 = await get_victories(attacker)
                        if ca2 > round_base_kills or va2 > round_base_vict_a:
                            successful_rounds += 1
                            print(f'[strict] ★ 回合 {successful_rounds}/{target_rounds} 有击杀（结算滞后，补确认）→ 继续', flush=True)
                        elif round_skip_flag or not round_had_angle:
                            # 无解回合（主动自杀跳过或从未找到角）→ 不计入
                            print(f'[strict] — 回合无命中角（目标在死角）→ 跳过（不计入）', flush=True)
                        elif not await bhv.is_alive(target):
                            # 本回合开过火且目标最终死亡 → 击杀（可能是自杀跳过误判）
                            # 攻击者胜利数若增加则为攻击者击杀；若对面胜利增加则是自杀
                            vt2 = await get_victories(target)
                            if vt2 > round_base_vict_t and va2 == round_base_vict_a:
                                print(f'[strict] ⚠️ 目标死亡但胜利归属对方 → 自杀', flush=True)
                                return 1
                            successful_rounds += 1
                            print(f'[strict] ★ 回合 {successful_rounds}/{target_rounds} 有击杀（目标死亡补确认）→ 继续', flush=True)
                        else:
                            # 有命中角但没击杀。先区分"真没打中"和"自己被打死"：
                            # 对面胜利数增加 = 攻击者死亡（本地局里对手不会主动
                            # 开火，所以基本就是自伤/自杀）。
                            vt2 = await get_victories(target)
                            if vt2 > round_base_vict_t and va2 == round_base_vict_a:
                                print(f'[strict] ✗ 攻击者死亡（对面胜利 {round_base_vict_t}→{vt2}）'
                                      f' → 意外自杀（自伤）', flush=True)
                                return 1
                            # 有命中角但没击杀 → 火控缺陷
                            da_diag = await bhv.is_alive(attacker)
                            at_diag = await bhv.get_position(attacker)
                            tt_diag = await bhv.get_position(target)
                            print(f'[strict] ✗ 回合有命中角但未击杀 → 火控缺陷  '
                                  f'aliveA={da_diag} posA={at_diag} posT={tt_diag} '
                                  f'killA={await get_kills(attacker)} killT={await get_kills(target)} '
                                  f'victA={await get_victories(attacker)} victT={vt2}',
                                  flush=True)
                            return 1
                last_round = rs
                if rs == 'in round':
                    # 新回合：记录基线，等坦克就位
                    round_base_kills = await get_kills(attacker)
                    round_base_kills_t = await get_kills(target)
                    round_base_vict_a = await get_victories(attacker)
                    round_base_vict_t = await get_victories(target)
                    round_kill_flag = False
                    round_had_angle = False
                    round_checked = 0
                    round_skip_flag = False
                    round_start_time = asyncio.get_event_loop().time()
                    round_wait_spawn = 0  # 等待目标复活的循环计数
                    round_need_baseline = True  # 目标就位后重读基线
                    round_force_maneuver = False  # 强制机动标志
                    globals()['_wp_suicide_n'] = 0  # 每回合重置自杀计数
                    maneuver_queue = []
                    tried_set = set()
                    print(f'[strict] 新回合 击杀(A={round_base_kills},T={round_base_kills_t}) 胜利(A={round_base_vict_a},T={round_base_vict_t})', flush=True)
                await asyncio.sleep(0.5)
                continue
            if rs != 'in round':
                await asyncio.sleep(0.5)
                continue

            # 新回合目标就位等待：目标不在场或位置(0,0) 说明上一回合
            # 击杀结算滞后，等待目标复活（最多 ~15s），避免误判当前回合
            tp_pos = await bhv.get_position(target)
            if round_wait_spawn < 30 and (not tp_pos or (abs(tp_pos[0]) < 0.1 and abs(tp_pos[1]) < 0.1)):
                round_wait_spawn += 1
                if round_wait_spawn == 1:
                    print(f'[strict] 等待目标就位...', flush=True)
                await asyncio.sleep(0.5)
                continue
            if round_wait_spawn >= 30 and (not tp_pos or (abs(tp_pos[0]) < 0.1 and abs(tp_pos[1]) < 0.1)):
                # 目标长时间不在 → 结算混乱，跳过本回合
                print(f'[strict] 目标长时间未就位 → 跳过本回合', flush=True)
                round_skip_flag = True
                round_had_angle = False
                round_checked = 0
                await asyncio.sleep(0.5)
                continue

            # 目标就位后重读基线：上一回合击杀此时应已完全入账，
            # 避免"跨回合滞后击杀"被误算进本回合（基线错位导致漏判击杀）
            if round_need_baseline:
                round_need_baseline = False
                # 再等 1 秒确保结算稳定
                await asyncio.sleep(1.0)
                round_base_kills = await get_kills(attacker)
                round_base_kills_t = await get_kills(target)
                round_base_vict_a = await get_victories(attacker)
                round_base_vict_t = await get_victories(target)
                print(f'[strict] 基线确认 击杀(A={round_base_kills},T={round_base_kills_t}) 胜利(A={round_base_vict_a},T={round_base_vict_t})', flush=True)

            # 回合中：检查是否已击杀 / 打队友 / 自杀
            ca = await get_kills(attacker)
            va_now = await get_victories(attacker)
            # 击杀判定：type0 击杀数增加 或 type1 攻击者胜利数增加（type0 滞后）
            if ca > round_base_kills or va_now > round_base_vict_a:
                round_kill_flag = True
                ct = await get_kills(target)
                if ct > round_base_kills_t:
                    print(f'[strict] ⚠️ 目标(队友)击杀数变化({round_base_kills_t}→{ct}) → 打队友了！', flush=True)
                    return 1
            # 自杀检测：攻击者死亡 → 对面胜利数增加
            # 注意：主动自杀跳过无解回合（round_skip_flag）时对面胜利数
            # 也会增加，这是预期的，不算火控缺陷。
            va = await get_victories(attacker)
            vt = await get_victories(target)
            if vt > round_base_vict_t and va == round_base_vict_a:
                if round_skip_flag:
                    print(f'[strict] (无解回合自杀已跳过，对面胜利数{round_base_vict_t}→{vt})', flush=True)
                else:
                    print(f'[strict] ⚠️ 攻击者死亡！对面胜利数({round_base_vict_t}→{vt}) → 意外自杀', flush=True)
                    return 1

            # 3 分钟超时检测：本回合从开始到击杀超过 180s → 火控缺陷
            if round_base_kills is not None and not round_kill_flag:
                elapsed = asyncio.get_event_loop().time() - round_start_time
                if elapsed > 180:
                    print(f'[strict] ⚠️ 回合超时 {elapsed:.0f}s 未击杀 → 火控缺陷', flush=True)
                    return 1

            # 火控
            ap = await bhv.get_position(attacker)
            tp = await bhv.get_position(target)
            if not ap or not tp:
                await asyncio.sleep(0.5)
                continue
            # 目标已在 (0,0)（死亡/冻结）但击杀数可能滞后 → 等击杀判定
            if abs(tp[0]) < 0.1 and abs(tp[1]) < 0.1:
                # 用胜利数判定是否已击杀
                if await get_victories(attacker) > round_base_vict_a:
                    round_kill_flag = True
                await asyncio.sleep(1.0)
                continue
            dist = math.hypot(tp[0]-ap[0], tp[1]-ap[1])
            if dist > MAX_FIRE_DIST:
                # 太远：长距离多反弹命中率低 → 机动到更近的射击位
                print(f'[strict] 距离 {dist:.0f}m 过远（>{MAX_FIRE_DIST}m）→ 机动靠近', flush=True)
                if not maneuver_queue:
                    maneuver_queue = await find_shooting_positions(tp, min_dist, target)
                    tried_set = set()
                # 选最近未试过的候选（候选本身已限制距目标 15-40m）
                cand = None
                while maneuver_queue:
                    c = maneuver_queue.pop(0)
                    key = (round(c[0]), round(c[1]))
                    if key in tried_set:
                        continue
                    cand = c
                    break
                if cand:
                    key = (round(cand[0]), round(cand[1]))
                    tried_set.add(key)
                    await bhv.navigate_to(attacker, cand, arrival=2.0)
                else:
                    # 无候选射击位：直接导航到目标附近的可通行格（~20m），
                    # 靠近后重新评估（比原地自杀更能推进验证）
                    print(f'[strict] 无近距射击位 → 导航到目标附近', flush=True)
                    moved = False
                    tiles = await bhv.get_maze_tiles()
                    if tiles:
                        w, h, T = tiles['w'], tiles['h'], 10.0
                        tgx, tgy = int(tp[0] // T), int(tp[1] // T)
                        # 找距目标 2 格的可通行格
                        best = None
                        best_d = 1e9
                        for gy in range(h):
                            for gx in range(w):
                                if tiles['tiles'][gy][gx][0] != 1:
                                    continue
                                d = math.hypot((gx+0.5)*T - tp[0], (gy+0.5)*T - tp[1])
                                if 15 <= d <= 30 and d < best_d:
                                    # 避开目标所在格（避免推挤目标）
                                    if abs(gx-tgx) <= 1 and abs(gy-tgy) <= 1:
                                        continue
                                    best_d = d
                                    best = ((gx+0.5)*T, (gy+0.5)*T)
                        if best:
                            await bhv.navigate_to(attacker, best, arrival=2.0)
                            moved = True
                    if not moved:
                        # 真无解 → 自杀跳过
                        print(f'[strict] 目标周围无近距射击位 → 自杀跳过本回合', flush=True)
                        round_skip_flag = True
                        round_had_angle = False
                        try:
                            await bhv.suicide_by_bounce(attacker, timeout=20.0)
                        except Exception as e:
                            print(f'[strict] 自杀失败: {e}', flush=True)
                        await asyncio.sleep(1.0)
                continue
            if dist < min_dist:
                # 太近：机动拉开（生成候选射击位队列）
                print(f'[strict] 距离 {dist:.0f}m 过近，拉开...', flush=True)
                if not maneuver_queue:
                    maneuver_queue = await find_shooting_positions(tp, min_dist, target)
                    tried_set = set()
                # 选一个离当前位置远的位置
                moved = False
                while maneuver_queue:
                    cand = maneuver_queue.pop(0)
                    key = (round(cand[0]), round(cand[1]))
                    if key in tried_set:
                        continue
                    cdist = math.hypot(cand[0]-ap[0], cand[1]-ap[1])
                    if cdist > min_dist:
                        tried_set.add(key)
                        await bhv.navigate_to(attacker, cand, arrival=2.0)
                        moved = True
                        break
                if not moved:
                    # 候选位都太近（攻击者贴脸目标）→ 导航到一个【远离目标、
                    # 距攻击者也远】的可通行格。优先找距目标最远且在 15-30m 的格。
                    print(f'[strict] 贴脸拉不开 → 找远距射击位导航', flush=True)
                    tiles = await bhv.get_maze_tiles()
                    if tiles:
                        w, h, T = tiles['w'], tiles['h'], 10.0
                        tgx, tgy = int(tp[0] // T), int(tp[1] // T)
                        best = None
                        best_score = -1e9
                        for gy in range(h):
                            for gx in range(w):
                                if tiles['tiles'][gy][gx][0] != 1:
                                    continue
                                # 避开目标所在格
                                if abs(gx-tgx) <= 1 and abs(gy-tgy) <= 1:
                                    continue
                                cx, cy = (gx+0.5)*T, (gy+0.5)*T
                                dt = math.hypot(cx-tp[0], cy-tp[1])
                                if dt < MIN_FIRE_DIST_NEAR:  # 远程区域至少 20m
                                    continue
                                da = math.hypot(cx-ap[0], cy-ap[1])
                                # 评分：距目标越远 + 距攻击者也远（拉得开）
                                score = dt + da
                                if score > best_score:
                                    best_score = score
                                    best = (cx, cy)
                        if best:
                            await bhv.navigate_to(attacker, best, arrival=2.0)
                            moved = True
                if not moved:
                    # 真拉不开 → 倒退（朝远离目标方向）
                    await bhv.turn_to(attacker, tp[0], tp[1])
                    await bhv.move_back(attacker, 2.0)
                    await bhv.stop(attacker)
                continue

            # 补射未击杀 → 强制换位：跳过同位置找角，直接生成新候选并机动
            if round_force_maneuver:
                round_force_maneuver = False
                round_had_angle = False  # 换位置后重新评估，不误判"打过没打死"
                if not maneuver_queue:
                    maneuver_queue = await find_shooting_positions(tp, min_dist, target)
                    tried_set = set()
                cand = None
                while maneuver_queue:
                    c = maneuver_queue.pop(0)
                    key = (round(c[0]), round(c[1]))
                    if key in tried_set:
                        continue
                    cand = c
                    break
                if cand:
                    key = (round(cand[0]), round(cand[1]))
                    tried_set.add(key)
                    print(f'[strict] 补射未中 → 机动到 ({cand[0]:.0f},{cand[1]:.0f}) 换位', flush=True)
                    await bhv.navigate_to(attacker, cand, arrival=2.0)
                else:
                    # 无候选 → 待无命中角分支自杀跳过
                    round_skip_flag = True
                    round_had_angle = False
                continue

            ang = await bhv.find_aim_angle_raycast(attacker, tp, target_id=target,
                                                   require_bounce=True,
                                                   max_traveled=MAX_TRAVELED,
                                                   max_bounces=MAX_BOUNCES_AIM,
                                                   muzzle_offset=MUZZLE_OFFSET)
            if ang is None:
                # 无命中角：尝试机动找射击位；都试过则标记"无角"等回合结束（跳过）
                if not maneuver_queue:
                    maneuver_queue = await find_shooting_positions(tp, min_dist, target)
                cand = None
                while maneuver_queue:
                    c = maneuver_queue.pop(0)
                    key = (round(c[0]), round(c[1]))
                    if key in tried_set:
                        continue
                    # 候选位需保持与目标的距离约束
                    cdist = math.hypot(c[0]-tp[0], c[1]-tp[1])
                    if cdist >= min_dist:
                        cand = c
                        break
                if cand:
                    key = (round(cand[0]), round(cand[1]))
                    tried_set.add(key)
                    print(f'[strict] 打不到，机动到 ({cand[0]:.0f},{cand[1]:.0f}) 距目标{math.hypot(cand[0]-tp[0],cand[1]-tp[1]):.0f}m', flush=True)
                    await bhv.navigate_to(attacker, cand, arrival=2.0)
                else:
                    # 机动后仍无命中角。
                    # 若从未找到过命中角 → 几何无解（跳过）；
                    # 若找到过但弹药耗尽/目标变化导致无解 → 已开火未击杀（缺陷）
                    if round_had_angle:
                        print(f'[strict] ✗ 曾找到命中角但未击杀（弹药耗尽或无解）→ 火控缺陷', flush=True)
                        return 1
                    if round_checked == 0:
                        print(f'[strict] 本回合无命中角（目标在死角）→ 等回合结束跳过', flush=True)
                    round_checked += 1
                    # 无解回合：60s 后主动自杀结束回合（本地局没人杀就不会结束）
                    if asyncio.get_event_loop().time() - round_start_time > 60:
                        print(f'[strict] 无解回合超时 → 撞墙自杀结束回合', flush=True)
                        round_skip_flag = True  # 标记跳过，自杀不算缺陷
                        try:
                            ok = await bhv.suicide_by_bounce(attacker, timeout=20.0)
                            if ok:
                                print(f'[strict] 自杀成功，回合结束', flush=True)
                            else:
                                print(f'[strict] 自杀未生效（坦克可能仍存活）', flush=True)
                        except Exception as e:
                            print(f'[strict] 自杀失败: {e}', flush=True)
                    await asyncio.sleep(1.0)
                continue
            # 找到命中角（实际开火成功才算 round_had_angle）
            round_checked += 1
            # 武器检查：只允许基础弹(-1)开火。其他武器（霰弹/加特林/导弹等）
            # 弹速/弹道不同，火控预测会失效 → 自杀结束本回合（不计入）。
            wp_now = await bhv.get_active_weapon(attacker)
            if wp_now not in (-1, None):
                print(f'[strict] 攻击者武器={wp_now}（非基础弹）→ 自杀结束本回合', flush=True)
                round_skip_flag = True
                round_had_angle = False
                # 持续重试自杀直到死亡（本地局回合不会自行结束，必须自杀）
                # 最多 5 次，每次 20 秒；全部失败则标记跳过等回合结束
                wp_suicide_n = globals().get('_wp_suicide_n', 0) + 1
                globals()['_wp_suicide_n'] = wp_suicide_n
                if wp_suicide_n > 5:
                    print(f'[strict] 自杀 5 次未生效 → 放弃本回合（对面将获胜）', flush=True)
                    await asyncio.sleep(5.0)
                    continue
                try:
                    ok = await bhv.suicide_by_bounce(attacker, timeout=25.0)
                    if ok:
                        globals()['_wp_suicide_n'] = 0  # 成功，重置
                        print(f'[strict] 自杀成功，回合结束', flush=True)
                    else:
                        print(f'[strict] 自杀未生效（武器可能需更久）→ 重试', flush=True)
                except Exception as e:
                    print(f'[strict] 自杀失败: {e}', flush=True)
                await asyncio.sleep(1.0)
                continue
            ok = await bhv.aim_and_fire(attacker, tp, raycast=True, lead=True, target_id=target,
                                        require_bounce=True, verbose=True,
                                        max_traveled=MAX_TRAVELED,
                                        max_bounces=MAX_BOUNCES_AIM,
                                        muzzle_offset=MUZZLE_OFFSET)
            if ok:
                round_had_angle = True
                shot += 1
                print(f'[strict] 开火 (距离 {dist:.0f}m)', flush=True)
                # 弹丸结果驱动补射：跟踪弹丸，接近目标则等击杀；偏离则重新瞄准补射
                for _ in range(2):  # 基础弹 5 发，打 2 发后换位置（长距离连续失败会耗尽弹药）
                    await asyncio.sleep(0.8)
                    ca = await get_kills(attacker)
                    if ca > round_base_kills:
                        round_kill_flag = True
                        break
                    va = await get_victories(attacker)
                    if va > round_base_vict_a:
                        round_kill_flag = True
                        break
                    if not await bhv.is_alive(target):
                        # 目标已死但击杀未入账 → 等结算（延迟可达 5s）
                        await asyncio.sleep(3.0)
                        ca = await get_kills(attacker)
                        va = await get_victories(attacker)
                        if ca > round_base_kills or va > round_base_vict_a:
                            round_kill_flag = True
                        break
                    tp2 = await bhv.get_position(target)
                    ap2 = await bhv.get_position(attacker)
                    if not tp2 or not ap2:
                        break
                    d2 = math.hypot(tp2[0]-ap2[0], tp2[1]-ap2[1])
                    if d2 < min_dist:
                        print(f'[strict] 补射停（目标逼近 {d2:.0f}m<{min_dist}m）', flush=True)
                        break
                    # 重新瞄准补射（每发都重新找角，目标位置最新）
                    o2 = await bhv.aim_and_fire(attacker, tp2, raycast=True, lead=True, target_id=target,
                                           require_bounce=True, verbose=True,
                                           max_traveled=MAX_TRAVELED,
                                           max_bounces=MAX_BOUNCES_AIM,
                                           muzzle_offset=MUZZLE_OFFSET)
                    print(f'[strict] 补射 (距离 {d2:.0f}m) ret={o2}', flush=True)
                    if not o2:
                        break
                    shot += 1
                # 检查是否已击杀（击杀数可能滞后：目标已死但 type0/type1 未更新）
                ca = await get_kills(attacker)
                va = await get_victories(attacker)
                if ca > round_base_kills or va > round_base_vict_a:
                    round_kill_flag = True
                else:
                    # 未击杀：目标可能还活着但本位置命中率低。
                    # 强制下一轮走机动换位（跳过同位置重复找角）。
                    round_force_maneuver = True
                    maneuver_queue = []
                    tried_set = set()
                    if not await bhv.is_alive(target):
                        # 目标已死但击杀未入账 → 等待结算（延迟可达 5s）
                        await asyncio.sleep(3.0)
                        ca = await get_kills(attacker)
                        va = await get_victories(attacker)
                        if ca > round_base_kills or va > round_base_vict_a:
                            round_kill_flag = True
                        else:
                            # 再等多一轮结算（击杀入账可能更慢）
                            await asyncio.sleep(2.0)
                            ca = await get_kills(attacker)
                            va = await get_victories(attacker)
                            if ca > round_base_kills or va > round_base_vict_a:
                                round_kill_flag = True
                            else:
                                print(f'[strict] ⚠️ 目标死亡但击杀数未增加 → 火控缺陷', flush=True)
                                return 1
            await asyncio.sleep(1.0)

        print(f'[strict] ✅ 连续 {target_rounds} 回合全部有击杀 → 火控合格！', flush=True)
    finally:
        rec_task.cancel()
        rec.close()


if __name__ == '__main__':
    try:
        # 必须把返回值作为退出码：否则"验证失败"也会以 0 退出，
        # 自动化/CI 会把失败当成功。
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\n[strict] 已停止')
        sys.exit(130)
