#!/usr/bin/env python
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
# -----------------------------------------------------------------------------
# ⚠️ 在线房间工具附加声明 / ONLINE-ROOM NOTICE  [DISCLAIMER-STAMP-ONLINE]
# 本脚本仅限【受控自建私人房间 / 本地研究环境】使用。
# 在公开在线对局使用自动化/辅助工具违反官方社区规则，可能导致账号被永久封禁
# （官方规则第 8 条 "No hacking on the website"，零容忍、无警告）。
# 请勿将本脚本用于干扰他人游戏体验或任何商业作弊服务。
# -----------------------------------------------------------------------------
"""
长时间毒圈追踪（在线，带"跟随安全区"存活策略）
================================================
目的：观察毒圈持续收缩的完整过程——安全区缩到多小、序列耗尽后怎样、坦克何时死亡、回合如何结束。

关键设计：
  * 每 6s 给**所有可控角色**发保活脉冲（否则 60s AFK 踢人 → Last Tank Standing 提前结束）
  * 每 3s 让安全区外的坦克寻路回安全区中心（尽量维持 ≥2 存活以保持回合）
  * 每 2s 记录：回合状态、Zone 字段、安全区 tile 边界/world 坐标、各坦克位置与存活

用法：
    python scripts/start_online_game.py auto --create --players 2 --no-leave --no-reload
    python scripts/probe_storm_long.py auto --dur 420 --out scripts/scratch/storm_long.jsonl
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tank_behavior_template import TankBehavior      # noqa: E402

SNAP = """(() => { const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({err: 'no game'});
  const out = {state: String(gc.model.getState()), stormFlag: null, zones: [], tanks: {}};
  try { out.stormFlag = gc.getStorm ? gc.getStorm() : null; } catch (e) {}
  try { const zs = gc.getZones ? gc.getZones() : {};
    for (const zid in zs) { const z = zs[zid];
      let f = null; try { f = z.getFields ? z.getFields() : null; } catch (e) {}
      let nTiles = null; try { nTiles = z.getTiles ? z.getTiles().length : null; } catch (e) {}
      out.zones.push({id: zid, type: z.getType ? z.getType() : null, nTiles: nTiles,
        fields: f ? JSON.parse(JSON.stringify(f)) : null}); } } catch (e) { out.zErr = String(e); }
  try { const ts = gc.getTanks();
    for (const pid in ts) { const t = ts[pid];
      out.tanks[pid] = {x: +t.getX().toFixed(2), y: +t.getY().toFixed(2)}; } } catch (e) {}
  return JSON.stringify(out); })()"""


def safe_area(zone):
    f = zone.get('fields') or {}
    tb = f.get('_tileBounds') or {}
    if not tb:
        return None
    l = tb.get('minX', 0) + (f.get('_expansionTilesLeft') or 0)
    r = tb.get('maxX', 0) - (f.get('_expansionTilesRight') or 0)
    t = tb.get('minY', 0) + (f.get('_expansionTilesTop') or 0)
    b = tb.get('maxY', 0) - (f.get('_expansionTilesBottom') or 0)
    return {'l': l, 'r': r, 't': t, 'b': b,
            'w': r - l + 1, 'h': b - t + 1, 'n': max(0, r - l + 1) * max(0, b - t + 1)}


def world_center(sa):
    # tile → world：格心 = (idx+0.5)*10
    return ((sa['l'] + sa['r'] + 1) / 2.0 * 10.0, (sa['t'] + sa['b'] + 1) / 2.0 * 10.0)


async def main():
    ap = argparse.ArgumentParser(description='长时间毒圈追踪')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--dur', type=float, default=420)
    ap.add_argument('--out', default=None)
    ap.add_argument('--keepalive', type=float, default=6.0)
    ap.add_argument('--follow', type=float, default=3.0, help='跟随安全区检查间隔（秒）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    roles = await bhv.get_roles()
    if not roles:
        print('[long] 无坦克（先进在线房间）')
        return 1
    print('[long] 角色=%s 最长 %.0fs' % (roles, args.dur), flush=True)

    recs = []
    fh = open(args.out, 'w', encoding='utf-8') if args.out else None
    t0 = time.time()
    last_keep = t0
    last_follow = t0
    round_t0 = None
    last_round = None
    max_steps = 0
    last_sa = None
    try:
        while time.time() - t0 < args.dur:
            now = time.time() - t0
            s = json.loads(await bhv.ctl.cdp.eval(SNAP))
            if s.get('err'):
                print('[long] %s' % s['err'], flush=True)
                break
            state = s.get('state')
            if state == 'in round' and last_round != 'in round':
                round_t0 = now
                print('[long] t=%6.1fs 进入回合（毒圈倒计时起点）' % now, flush=True)
            if state != last_round:
                if last_round == 'in round':
                    print('[long] t=%6.1fs 回合结束（%s）' % (now, state), flush=True)
                last_round = state

            zones = s.get('zones') or []
            storm = next((z for z in zones if z.get('type') == 3
                          or (z.get('fields') or {}).get('_tileBounds')), None)
            sa = safe_area(storm) if storm else None
            tanks = s.get('tanks') or {}
            n_tanks = len(tanks)
            rec = {'t': round(now, 1), 'state': state, 'stormFlag': s.get('stormFlag'),
                   'nZones': len(zones), 'nTanks': n_tanks,
                   'safe': sa, 'tanks': tanks}
            if storm:
                f = storm.get('fields') or {}
                rec['storm'] = {
                    'side': f.get('_expansionSide'), 'expTime': f.get('_expansionTime'),
                    'seqLen': len(f.get('_expansionSequence') or []),
                    'seqHead': (f.get('_expansionSequence') or [])[:10],
                    'r': f.get('_expansionTilesRight'), 'b': f.get('_expansionTilesBottom'),
                    'l': f.get('_expansionTilesLeft'), 't': f.get('_expansionTilesTop'),
                }
            recs.append(rec)
            if fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                fh.flush()

            # 打印关键变化
            if sa:
                if last_sa != sa:
                    last_sa = sa
                    f = rec.get('storm') or {}
                    print('[long] t=%6.1fs 安全区 l=%s r=%s t=%s b=%s (%dx%d=%d格) side=%s expT=%.1f '
                          '已缩 R%s/B%s/L%s/T%s 序列长=%s 坦克=%d'
                          % (now, sa['l'], sa['r'], sa['t'], sa['b'], sa['w'], sa['h'], sa['n'],
                             f.get('side'), f.get('expTime') or 0, f.get('r'), f.get('b'),
                             f.get('l'), f.get('t'), f.get('seqLen'), n_tanks), flush=True)

            # 存活策略：毒圈是【瞬间秒杀】，必须提前把坦克挪到中心（留 2 格余量），
            # 不能等"已在安全区外"——那时已经死了（2026-10-24 实测教训）。
            if sa and now - last_follow >= args.follow:
                last_follow = time.time() - t0
                cx, cy = world_center(sa)
                margin = 2
                for pid in list(tanks.keys()):
                    t = tanks[pid]
                    tx, ty = int(t['x'] // 10), int(t['y'] // 10)
                    near_edge = (tx <= sa['l'] + margin or tx >= sa['r'] - margin
                                 or ty <= sa['t'] + margin or ty >= sa['b'] - margin)
                    outside = not (sa['l'] <= tx <= sa['r'] and sa['t'] <= ty <= sa['b'])
                    if not (outside or near_edge):
                        continue
                    d_center = math.hypot(t['x'] - cx, t['y'] - cy)
                    if d_center <= 4.0:
                        continue
                    print('[long] t=%6.1fs 坦克 %s (%.0f,%.0f)%s → 回中心 (%.0f,%.0f) d=%.1f'
                          % (now, pid, t['x'], t['y'], '【区外!】' if outside else '(近边)', cx, cy, d_center),
                          flush=True)
                    # 先快速直冲（比寻路快），不够再寻路
                    try:
                        await bhv.turn_to(pid, cx, cy)
                        await bhv.move_forward(pid, 0.35)
                        pos2 = await bhv.get_position(pid)
                        if pos2 is None:
                            continue
                        tx2, ty2 = int(pos2[0] // 10), int(pos2[1] // 10)
                        still = not (sa['l'] <= tx2 <= sa['r'] and sa['t'] <= ty2 <= sa['b'])
                        if still:
                            await asyncio.wait_for(
                                bhv.navigate_to(pid, (cx, cy), arrival=2.0, waypoint_arrival=3.0),
                                timeout=8.0)
                    except Exception as e:
                        print('[long]   机动中断: %s' % e, flush=True)

            # 全程保活（所有角色）
            if time.time() - last_keep >= args.keepalive:
                last_keep = time.time()
                for p in list(roles.keys()):
                    try:
                        await bhv.ctl.set_input(p, right=True, hold=0.1)
                        await bhv.ctl.set_input(p, left=True, hold=0.1)
                    except Exception:
                        pass

            if n_tanks == 0:
                print('[long] t=%6.1fs 坦克全部消失 → 观察 10s 后结束' % now, flush=True)
                await asyncio.sleep(10)
                break
            await asyncio.sleep(2.0)
    finally:
        if fh:
            fh.close()

    # 汇总
    storms = [r for r in recs if r.get('safe')]
    print('\n[long] ===== 汇总 =====', flush=True)
    if not storms:
        print('  未观察到毒圈')
    else:
        print('  毒圈出现后共 %d 个采样' % len(storms))
        areas = [r['safe']['n'] for r in storms]
        print('  安全区格数：首次 %d → 最小 %d（末次 %d）' % (areas[0], min(areas), areas[-1]))
        sizes = [(r['safe']['w'], r['safe']['h']) for r in storms]
        print('  尺寸序列（去重）: %s' % sorted(set(sizes), key=lambda x: -x[0] * x[1]))
        sides = []
        for r in storms:
            sd = (r.get('storm') or {}).get('side')
            if not sides or sides[-1] != sd:
                sides.append(sd)
        print('  方向序列: %s' % sides)
        print('  最后安全区: %s' % json.dumps(storms[-1]['safe'], ensure_ascii=False))
        dies = [r['t'] for r in recs if r.get('nTanks') == 0]
        print('  坦克全消失时刻: %s' % (dies[:3] if dies else '未出现'))
    if args.out:
        print('→ %s（%d 条记录）' % (args.out, len(recs)))
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
