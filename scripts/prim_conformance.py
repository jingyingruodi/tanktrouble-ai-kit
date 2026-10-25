# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：
#   零容忍，可永久封号且无警告）。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
"""原语一致性验证（v3）：逐条对应两轮纯原语黑盒测试报告的接口缺口。

用法（游戏需在局中）：
    python scripts/prim_conformance.py
    TT_BB_DIR="D:\\AI Agent\\tt_bb_lowlevel2" python scripts/prim_conformance.py

覆盖：
  1. state 新字段（phase/roundId/countdown/t_ms/坦克 vx,vy）
  2. 炮口起算 vs 车体中心的零反弹自伤假阳性（121 → 0）
  3. 批量弹道速度
  4. walls_rects / walls_unique / point_blocked / line_of_sight
  5. input_state / weapon_state / weapon_info / accepts_input
  6. from_xy（假设位置）与当前位置推演的一致性
  7. ignore_tanks（只算墙）
  8. first_bounce_index / deadly_from_index / end 非空
  9. simulate_shot（带半径）与官方零宽射线的对比
 10. round_age_ms（换场稳定）
"""
import asyncio
import math
import os
import sys
import time

BB = os.environ.get('TT_BB_DIR') or r'D:\AI Agent\tt_bb_lowlevel6'
if BB not in sys.path:
    sys.path.insert(0, BB)

from ttprim import Prim  # noqa: E402


async def main():
    p = Prim()
    await p.connect()
    ok = True
    me = None
    try:
        me = await p.my_pid()
        st = await p.state()
        print('== 1) state 新字段 ==')
        for k in ('phase', 'roundId', 'countdown', 't_ms'):
            print('  %-10s = %r' % (k, st.get(k)))
        t = (st.get('tanks') or {}).get(str(me)) or {}
        ok &= bool(st.get('roundId')) and bool(st.get('phase')) and 'vx' in t

        print('== 2) 炮口起算：零反弹自伤假阳性 ==')
        rot = t.get('rot') or 0.0
        heading = math.degrees(rot) - 90.0
        angles = [heading - 60 + i * 0.25 for i in range(481)]
        muzzle = await p.projectile_paths(me, angles, max_bounces=8, from_muzzle=True)
        center = await p.projectile_paths(me, angles, max_bounces=8, from_muzzle=False)

        def self0(batch):
            return sum(1 for it in batch
                       if isinstance(it, dict) and it.get('hitTank') == me
                       and it.get('bounces') == 0)

        print('  from_muzzle=True  零反弹自伤角 = %d' % self0(muzzle))
        print('  from_muzzle=False 零反弹自伤角 = %d' % self0(center))
        ok &= self0(muzzle) == 0 and self0(center) > 0

        print('== 3) 批量速度 ==')
        t0 = time.perf_counter()
        batch = await p.projectile_paths(me, [-180 + i for i in range(361)], max_bounces=2)
        dt_batch = time.perf_counter() - t0
        print('  361 角批量 = %.2fs' % dt_batch)
        ok &= dt_batch < 2.0 and len(batch) == 361

        print('== 4) 几何原语 ==')
        walls = st.get('walls') or []
        uniq = p.walls_unique(walls)
        rects = await p.walls_rects(0.25, walls=walls)
        los_self = await p.line_of_sight(t['x'], t['y'], t['x'], t['y'], walls=walls)
        print('  walls=%d unique=%d rects=%d LOS(同点)=%s'
              % (len(walls), len(uniq), len(rects), los_self))
        ok &= len(uniq) <= len(walls) and len(rects) == len(walls) and los_self

        print('== 5) 输入/武器回读 ==')
        await p.set_input(me, left=True)
        await asyncio.sleep(0.08)
        inp = await p.input_state(me)
        await p.release(me)
        await asyncio.sleep(0.08)
        inp2 = await p.input_state(me)
        wi = await p.weapon_info(me)
        acc = await p.accepts_input()
        print('  left 按下=%s 释放后=%s' % (inp.get('left'), inp2.get('left')))
        print('  weapon_info =', {k: wi.get(k) for k in ('name', 'speed', 'range', 'ammo_left')})
        print('  accepts_input =', acc)
        ok &= bool(inp.get('left')) and not inp2.get('left') and acc is True
        ok &= isinstance(wi, dict) and 'speed' in wi

        print('== 6) from_xy（假设位置）一致性 ==')
        pos = (t['x'], t['y'])
        d_cur = await p.projectile_path(me, 0.0, from_muzzle=False)
        d_xy = await p.projectile_path(me, 0.0, from_xy=pos)
        same = (abs(d_cur.get('traveled', 0) - d_xy.get('traveled', 0)) < 0.5
                and d_cur.get('bounces') == d_xy.get('bounces'))
        print('  from_xy=当前位置: traveled %.2f vs %.2f same=%s'
              % (d_cur.get('traveled', 0), d_xy.get('traveled', 0), same))
        ok &= same

        print('== 7) ignore_tanks（只算墙）==')
        d_it = await p.projectile_path(me, 0.0, ignore_tanks=True)
        print('  hitTank=%r（应为 None，只算墙）' % d_it.get('hitTank'))
        ok &= d_it.get('hitTank') is None

        print('== 8) 路径标记/end ==')
        for label, d in (('path', d_cur), ('batch0', batch[0] if batch else {})):
            print('  %-6s first_bounce=%r deadly_from=%r end=%r'
                  % (label, d.get('first_bounce_index'), d.get('deadly_from_index'),
                     d.get('end')))
            ok &= 'first_bounce_index' in d and d.get('end') is not None

        print('== 9) simulate_shot（带半径）vs 官方零宽 ==')
        opp = [pid for pid in (st.get('tanks') or {}) if pid != str(me)]
        if opp:
            ot = st['tanks'][opp[0]]
            ang = math.degrees(math.atan2(ot['y'] - t['y'], ot['x'] - t['x']))
            off = await p.projectile_path(me, ang)
            sim = p.simulate_shot(me, ang, st)
            print('  angle=%.1f° 官方: hit=%s hitTank=%s b=%s | sim: hit=%s hitTank=%s b=%s'
                  % (ang, off.get('hit'), str(off.get('hitTank'))[:8],
                     off.get('bounces'), sim.get('hit'), str(sim.get('hitTank'))[:8],
                     sim.get('bounces')))
            if off.get('hitTank') == opp[0] and off.get('bounces') == 0:
                ok &= sim.get('hitTank') == opp[0] or sim.get('wall_hit_dist') is not None

        print('== 10) round_age_ms ==')
        age = p.round_age_ms(st)
        print('  round_age_ms =', age)
        ok &= isinstance(age, (int, float))

        print('== 11) v4 原语（第三轮报告修复）==')
        sim0 = p.simulate_shot(me, 0.0, st, max_bounces=0, ignore_self=True)
        sim1 = p.simulate_shot(me, 0.0, st, max_bounces=1, ignore_self=True)
        print('  sim mb0: b=%s trunc=%s | mb1: b=%s trunc=%s'
              % (sim0.get('bounces'), sim0.get('truncated'),
                 sim1.get('bounces'), sim1.get('truncated')))
        ok &= sim0.get('bounces') == 0 and (sim1.get('bounces') or 0) <= 1
        ok &= 'complete' in sim1 and 'truncated' in sim1
        simr = p.simulate_shot(me, 0.0, st, from_rot=(t.get('rot') or 0.0),
                               ignore_self=True)
        print('  from_rot/ignore_self: hitTank=%r（不应为自己）' % simr.get('hitTank'))
        ok &= simr.get('hitTank') != me
        simt = p.simulate_shot(me, 0.0, st, ignore_tanks=True)
        print('  ignore_tanks=True: hitTank=%r' % simt.get('hitTank'))
        ok &= simt.get('hitTank') is None
        obb = p.tank_obb(me, st, radius=0.25)
        print('  tank_obb: half=%.2fx%.2f corners=%d' % (obb['half_len'], obb['half_wid'],
                                                         len(obb['corners'])))
        ok &= len(obb['corners']) == 4
        batch_sim = p.simulate_shots(me, [0, 90, 180, 270], st, max_bounces=1)
        print('  simulate_shots: n=%d' % len(batch_sim))
        ok &= len(batch_sim) == 4
        merged = p.walls_unique([{'x1': 0, 'y1': 0, 'x2': 10, 'y2': 0},
                                 {'x1': 10, 'y1': 0, 'x2': 20, 'y2': 0}],
                                merge_collinear=True)
        print('  walls_unique(merge): %d 段（2 段合并为 1）' % len(merged))
        ok &= len(merged) == 1
        inp_alias = await p.input_state(me)
        print('  input_state 别名: forward=%r fwd=%r' % (inp_alias.get('forward'),
                                                          inp_alias.get('fwd')))
        ok &= inp_alias.get('forward') == inp_alias.get('fwd')

        print('== 12) v5 原语（第四轮报告修复）==')
        sim60 = p.simulate_shot(me, 90.0, st, max_bounces=16, max_length=60,
                                ignore_self=True)
        print('  max_length=60: traveled=%.1f applied=%.1f reason=%s'
              % (sim60.get('traveled', -1), sim60.get('max_length_applied', -1),
                 sim60.get('truncation_reason')))
        ok &= (sim60.get('traveled') or 0) <= 60.5
        ok &= sim60.get('max_length_applied') == 60
        pose = p.simulate_shot(me, 0.0, st, from_pose=(t['x'], t['y'], t.get('rot') or 0.0),
                               ignore_self=True)
        exp_x = t['x'] + 2.5
        print('  from_pose origin=(%.1f,%.1f) 期望 x≈%.1f' % (pose['origin']['x'],
                                                              pose['origin']['y'], exp_x))
        ok &= abs(pose['origin']['x'] - exp_x) < 0.3
        ok &= 'origin_blocked_point' in sim60 and 'angle' in sim60
        ok &= isinstance(st.get('round_seen_age_ms'), (int, float))
        wi2 = await p.weapon_info(me)
        print('  weapon_info: capacity=%r fired=%r ammo_left=%r'
              % (wi2.get('ammo_capacity'), wi2.get('bullets_fired'), wi2.get('ammo_left')))
        ok &= ('ammo_capacity' in wi2) and wi2.get('ammo_left') is None
        import math as _m
        bsim = p.simulate_shots(me, [0, 90], st,
                                from_rots=[0.0, _m.radians(90)], ignore_self=True)
        print('  simulate_shots from_rots: n=%d' % len(bsim))
        ok &= len(bsim) == 2

        print('== 13) v6 原语（第五轮报告修复）==')
        # from_pose：假设位姿时自车 OBB 跟随（用合成状态单测，稳定可复现）
        st_syn = {'tanks': {'me': {'x': 5, 'y': 5, 'rot': 0.0},
                            'opp': {'x': 45, 'y': 45, 'rot': 0.0}},
                  'walls': [{'x1': 0, 'y1': 0, 'x2': 0, 'y2': 50},
                            {'x1': 0, 'y1': 0, 'x2': 50, 'y2': 0},
                            {'x1': 0, 'y1': 50, 'x2': 50, 'y2': 50},
                            {'x1': 50, 'y1': 0, 'x2': 50, 'y2': 50}]}
        syn_pid = 'me'   # st_syn 用固定 pid 名
        r_p = p.simulate_shot(syn_pid, 90.0, st_syn, from_pose=(15.0, 15.0, 0.0),
                              max_bounces=16, ignore_tanks=['opp'])
        print('  from_pose=(15,15): hitTank=%r traveled=%.1f（应命中自己）'
              % (r_p.get('hitTank'), r_p.get('traveled') or 0))
        ok &= r_p.get('hitTank') == syn_pid
        # 官方弹道派生字段
        ann = p._annotate_official_path({'hit': False, 'bounces': 8,
                                         'traveled': 180.0}, 180, 8)
        print('  official annotate: reason=%r max_len=%r'
              % (ann.get('truncation_reason'), ann.get('max_length_applied')))
        ok &= ann.get('truncation_reason') == 'bounces'
        # 暂停/时钟字段（真实 state）
        for k in ('paused', 'game_time_ms', 'frames', 'hidden'):
            print('  state[%s] = %r' % (k, st.get(k)))
        ok &= 'paused' in st and 'game_time_ms' in st
        # round_seen_age 仍在
        ok &= isinstance(st.get('round_seen_age_ms'), (int, float))

        print('== 14) v6.1（监控/get 测试修复）==')
        print('  active=%r phase=%r countdown=%r theme=%r' % (
            st.get('active'), st.get('phase'), st.get('countdown'), st.get('theme')))
        ok &= st.get('active') == (st.get('phase') == 'in round')
        if st.get('phase') == 'in round':
            ok &= st.get('countdown') is None
        ok &= isinstance(st.get('theme'), (int, float))
        print('  round_seen_age_ms=%r round_age_ms=%r' % (
            st.get('round_seen_age_ms'), st.get('round_age_ms')))
        ok &= isinstance(st.get('round_seen_age_ms'), (int, float))
        # vx_fd：前进 0.35s 后两次采样应为正速度
        me2 = me
        await p.set_input(me2, fwd=True)
        await asyncio.sleep(0.35)
        s_a = await p.state()
        await asyncio.sleep(0.08)
        s_b = await p.state()
        await p.release(me2)
        tb = (s_b.get('tanks') or {}).get(str(me2)) or {}
        sp = tb.get('speed_fd')
        print('  vx_fd/vy_fd/speed_fd = %r/%r/%r' % (tb.get('vx_fd'), tb.get('vy_fd'), sp))
        ok &= isinstance(sp, (int, float)) and sp > 3.0

        print('\n结论:', '✅ PASS' if ok else '❌ FAIL')
        return 0 if ok else 1
    finally:
        try:
            if me:
                await p.release(me)
        except Exception:
            pass
        await p.close()


sys.exit(asyncio.run(main()))
