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
在线房间控制测试：移动 → 火控
==============================
在线与本地局的关键差异：
  * 输入粒度 = `Constants.CLIENT.TANKSTATE_EMISSION_INTERVAL`（0.5s）——客户端每 0.5s 才上报一次
    坦克状态，**短于 0.5s 的输入可能被吞掉**。
  * 服务端权威：非法移动/转向会被判 `MOVED_TOO_FAST` / `TURNED_TOO_FAST` 并拉回。
  * 火控：按住 fire 必须 ≥0.5s 才保证服务端收到一次开火。

本脚本（全程在**在线房间**内）：
  A. 移动：不同按住时长（0.2 / 0.6 / 1.0s）的前进位移 + 后退/左右转角度，并检查"是否被服务端拉回"
  B. 火控：对另一辆（静止的）坦克找命中角并开火，统计命中率（击杀分变化）

用法：
    python scripts/start_online_game.py auto --create --players 2 --no-leave --no-reload
    python scripts/verify_online_control_full.py auto
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

STATE = """(() => { const gc = GameManager.getGameController(); if (!gc) return JSON.stringify({err: 'no game'});
  const out = {state: String(gc.model.getState()), mode: String(Constants.getMode()), tanks: {}};
  try { const ts = gc.getTanks();
    for (const pid in ts) { const t = ts[pid];
      out.tanks[pid] = {x: t.getX(), y: t.getY(), rot: t.getRotation(),
                        alive: t.isAlive ? t.isAlive() : null}; } } catch (e) {}
  return JSON.stringify(out); })()"""

GRAN = """(() => { try { return String(Constants.CLIENT.TANKSTATE_EMISSION_INTERVAL); } catch (e) { return '?'; } })()"""

KILLS = """(() => { try { const s = GameManager.getGameController()
  .getScoreByPlayerIdAndType('%s', 0); return String(s ? s.getValue() : -1); } catch (e) { return '-2'; } })()"""

CLEAR_DIR = """(() => { const gc = GameManager.getGameController();
  const pid = %s; const T = gc.getTank(pid);
  if (!T) return 'null';
  const U = window.UIUtils; const world = GameManager.phaserInstance.physics.p2.world;
  const WALL = U.wallCollisionGroup.mask, SH = U.shieldCollisionGroup.mask, SP = U.spawnCollisionGroup.mask;
  const res = new p2.RaycastResult(); const out = [];
  for (let deg = 0; deg < 360; deg += 15) {
    const th = deg * Math.PI / 180;
    const ray = new p2.Ray({mode: p2.Ray.CLOSEST,
      from: [-T.getX(), -T.getY()],
      to: [-(T.getX() + Math.cos(th) * 70), -(T.getY() + Math.sin(th) * 70)],
      skipBackfaces: true, collisionMask: WALL | SH | SP,
      collisionGroup: U.rayCollisionGroup.mask});
    world.raycast(res, ray);
    out.push([deg, res.hasHit ? Math.round(res.getHitDistance(ray)) : 999]);
  }
  return JSON.stringify(out); })()"""


async def snap(bhv):
    return json.loads(await bhv.ctl.cdp.eval(STATE))


async def wait_round(bhv, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = await snap(bhv)
        if s.get('state') == 'in round':
            return s
        await asyncio.sleep(1)
    return None


async def main():
    ap = argparse.ArgumentParser(description='在线控制测试（移动→火控）')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--shots', type=int, default=6)
    ap.add_argument('--skip-move', action='store_true', help='跳过移动测试，只测火控（用干净房间）')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    s = await snap(bhv)
    if s.get('err'):
        print('[ctl2] %s' % s['err'])
        return 1
    if s.get('mode') != 'client online':
        print('[ctl2] ❌ 当前模式=%s（需要在线房间）' % s.get('mode'))
        return 1
    gran = await bhv.ctl.cdp.eval(GRAN)
    print('[ctl2] 模式=%s 输入粒度=%s s' % (s.get('mode'), gran), flush=True)
    roles = await bhv.get_roles()
    ids = list(roles.keys())
    if len(ids) < 2:
        print('[ctl2] 需要 ≥2 辆可控坦克，当前 %s' % ids)
        return 1
    shooter, target = ids[0], ids[1]
    print('[ctl2] 射手=%s 目标=%s roles=%s' % (shooter, target, roles), flush=True)

    out = {'mode': s.get('mode'), 'granularity': gran, 'movement': [], 'fire': []}

    # ---------- A. 移动 ----------
    print('\n[ctl2] ===== A. 移动（在线）=====', flush=True)
    # 先把坦克转到"前方 70m 无墙"的方向（否则前进被墙挡住会测成 0）
    clear_dir_deg = None
    if not args.skip_move:
        try:
            dirs = json.loads(await bhv.ctl.cdp.eval(CLEAR_DIR % json.dumps(shooter)))
            open_dirs = [d for d, dist in (dirs or []) if dist > 45]
            if open_dirs:
                clear_dir_deg = open_dirs[0]
                # forward = rot - π/2 → 朝该方向要先转到 rot = dir + π/2
                target_rot = math.radians(clear_dir_deg) + math.pi / 2
                await bhv.turn_to_rot(shooter, target_rot)
                print('[ctl2] 已朝向开阔方向 %s°（70m 无墙）' % clear_dir_deg, flush=True)
        except Exception as e:
            print('[ctl2] 找开阔方向失败: %s' % e, flush=True)

    for hold in ([] if args.skip_move else [0.2, 0.4, 0.6, 1.0]):
        st = await wait_round(bhv, 60)
        if not st:
            print('[ctl2] 等不到回合，跳过移动测试')
            break
        t = st['tanks'].get(shooter)
        if not t:
            await asyncio.sleep(2)
            continue
        p0 = (t['x'], t['y'])
        await bhv.ctl.set_input(shooter, fwd=True, hold=hold)
        await asyncio.sleep(0.35)
        s1 = await snap(bhv)
        t1 = s1['tanks'].get(shooter) or t
        d1 = math.hypot(t1['x'] - p0[0], t1['y'] - p0[1])
        await asyncio.sleep(0.8)
        s2 = await snap(bhv)
        t2 = s2['tanks'].get(shooter) or t1
        d2 = math.hypot(t2['x'] - p0[0], t2['y'] - p0[1])
        rec = {'hold': hold, 'disp_0.35s': round(d1, 2), 'disp_1.15s': round(d2, 2),
               'speed_est': round(d2 / hold, 2) if hold else None,
               'state': s1.get('state'), 'clear_dir': clear_dir_deg}
        out['movement'].append(rec)
        print('[ctl2] 前进 hold=%.1fs → 位移 0.35s后=%.2fm、1.15s后=%.2fm（推速≈%.2f m/s）'
              % (hold, d1, d2, d2 / hold if hold else 0), flush=True)
        await asyncio.sleep(0.4)

    # 转向/后退（各 0.6s）
    for act, label in ([] if args.skip_move else
                       [('right', '右转'), ('left', '左转'), ('back', '后退')]):
        st = await wait_round(bhv, 60)
        if not st:
            break
        t = st['tanks'].get(shooter)
        if not t:
            continue
        p0 = (t['x'], t['y'], t['rot'])
        await bhv.ctl.set_input(shooter, hold=0.6, **{act: True})
        await asyncio.sleep(0.35)
        s1 = await snap(bhv)
        t1 = s1['tanks'].get(shooter) or t
        if act == 'back':
            d = math.hypot(t1['x'] - p0[0], t1['y'] - p0[1])
            rec = {'act': act, 'hold': 0.6, 'disp': round(d, 2)}
            print('[ctl2] 后退 hold=0.6s → 位移 %.2fm' % d, flush=True)
        else:
            dr = t1['rot'] - p0[2]
            rec = {'act': act, 'hold': 0.6, 'drot': round(dr, 3), 'rate': round(dr / 0.6, 2)}
            print('[ctl2] %s hold=0.6s → Δrot=%.3f rad（≈%.2f rad/s）' % (label, dr, dr / 0.6), flush=True)
        out['movement'].append(rec)
        await asyncio.sleep(0.4)

    # ---------- B. 火控 ----------
    print('\n[ctl2] ===== B. 火控（在线：找角 → 转向 → 按住 0.6s 开火）=====', flush=True)
    hits = 0
    no_angle_streak = 0
    for i in range(args.shots):
        # 等到 in round 且两辆坦克都真正在场（非 (0,0)）
        st = None
        for _ in range(30):
            s0 = await snap(bhv)
            if s0.get('state') == 'in round':
                tp0 = (s0.get('tanks') or {}).get(target)
                sp0 = (s0.get('tanks') or {}).get(shooter)
                if tp0 and sp0 and not (abs(tp0['x']) < 0.1 and abs(tp0['y']) < 0.1) \
                        and not (abs(sp0['x']) < 0.1 and abs(sp0['y']) < 0.1):
                    st = s0
                    break
            await asyncio.sleep(1)
        if not st:
            print('[ctl2] 第%d发：30s 内没等到"双方都在场"的回合' % (i + 1), flush=True)
            out['fire'].append({'shot': i + 1, 'err': 'no ready round'})
            continue
        tp = st['tanks'][target]
        sp = st['tanks'][shooter]
        dist = math.hypot(tp['x'] - sp['x'], tp['y'] - sp['y'])
        # ★ 在线火控必须有交战距离：贴脸时炮口偏移 2.5m 会让弹丸直接越过目标
        if dist < 14.0:
            print('[ctl2] 第%d发：距离仅 %.1fm（过近）→ 先拉开距离' % (i + 1, dist), flush=True)
            far = None
            try:
                terr = await bhv.get_terrain()
                best_d = 0.0
                for ty_ in range(len(terr.get('tiles') or [])):
                    for tx_ in range(len(terr['tiles'][ty_])):
                        if not terr['tiles'][ty_][tx_][0]:
                            continue
                        wx, wy = (tx_ + 0.5) * 10.0, (ty_ + 0.5) * 10.0
                        d_ = math.hypot(wx - tp['x'], wy - tp['y'])
                        if d_ > best_d:
                            best_d, far = d_, (wx, wy)
            except Exception:
                far = None
            if far:
                try:
                    await asyncio.wait_for(
                        bhv.navigate_to(shooter, far, arrival=2.0, waypoint_arrival=3.0),
                        timeout=25.0)
                except Exception as e:
                    print('[ctl2]   拉开距离失败: %s' % e, flush=True)
            s2 = await snap(bhv)
            tp = (s2.get('tanks') or {}).get(target) or tp
            sp = (s2.get('tanks') or {}).get(shooter) or sp
            dist = math.hypot(tp['x'] - sp['x'], tp['y'] - sp['y'])
            print('[ctl2]   拉开后距离 %.1fm' % dist, flush=True)
        k0 = int(float(await bhv.ctl.cdp.eval(KILLS % shooter)))
        # 优先直线解（0 反弹）——先确认在线火控本身能命中，再谈反弹弹
        best = None
        sp_now = await bhv.get_position(shooter)
        for req in (False, True):
            ang = await bhv.find_aim_angle_raycast(shooter, (tp['x'], tp['y']), target_id=target,
                                                    require_bounce=req, max_traveled=180.0,
                                                    max_bounces=25)
            if ang is None or sp_now is None:
                continue
            d = await bhv._raycast_path(sp_now[0], sp_now[1], ang,
                                         tp['x'], tp['y'], target_tank_id=target, max_bounces=8)
            nb = int(d.get('bounces', 0))
            best = {'ang': ang, 'bounces': nb, 'traveled': d.get('traveled')}
            if nb == 0:
                break
        if not best:
            # ★ 贴墙时炮口会伸进墙里 → 全部射线被挡 → 无命中角。
            #   先机动到离目标 20~45m 的开阔格，再重试一次找角。
            no_angle_streak += 1
            print('[ctl2] 第%d发：无命中角（连续 %d）→ 机动到开阔点重试' % (i + 1, no_angle_streak),
                  flush=True)
            moved = False
            try:
                terr = await bhv.get_terrain()
                cand = []
                for ty_ in range(len(terr.get('tiles') or [])):
                    for tx_ in range(len(terr['tiles'][ty_])):
                        if not terr['tiles'][ty_][tx_][0]:
                            continue
                        wx, wy = (tx_ + 0.5) * 10.0, (ty_ + 0.5) * 10.0
                        d_ = math.hypot(wx - tp['x'], wy - tp['y'])
                        if 20.0 <= d_ <= 45.0:
                            cand.append((d_, wx, wy))
                if cand:
                    cand.sort()
                    _, wx, wy = cand[0]
                    await asyncio.wait_for(
                        bhv.navigate_to(shooter, (wx, wy), arrival=2.0, waypoint_arrival=3.0),
                        timeout=20.0)
                    moved = True
            except Exception as e:
                print('[ctl2]   机动失败: %s' % e, flush=True)
            if moved:
                s3 = await snap(bhv)
                tp3 = (s3.get('tanks') or {}).get(target)
                sp3 = (s3.get('tanks') or {}).get(shooter)
                if tp3 and sp3:
                    tp = tp3
                    ang2 = await bhv.find_aim_angle_raycast(shooter, (tp3['x'], tp3['y']),
                                                            target_id=target, require_bounce=False,
                                                            max_traveled=180.0, max_bounces=25)
                    sp_now = (sp3['x'], sp3['y'])
                    if ang2 is not None:
                        d2 = await bhv._raycast_path(sp_now[0], sp_now[1], ang2,
                                                     tp3['x'], tp3['y'], target_tank_id=target,
                                                     max_bounces=8)
                        best = {'ang': ang2, 'bounces': int(d2.get('bounces', 0)),
                                'traveled': d2.get('traveled')}
                        print('[ctl2]   机动后重新找到角 %.1f°（反弹 %d）'
                              % (math.degrees(ang2), best['bounces']), flush=True)
            if not best:
                out['fire'].append({'shot': i + 1, 'angle': None})
                if no_angle_streak >= 2:
                    print('[ctl2] 连续无角 → 等待新回合', flush=True)
                    await asyncio.sleep(8)
                    no_angle_streak = 0
                continue
        no_angle_streak = 0
        ang = best['ang']
        ok = await bhv.aim_and_fire(shooter, (tp['x'], tp['y']), fire_sec=0.6, target_id=target,
                                    raycast=True, lead=False, require_bounce=(best['bounces'] > 0),
                                    max_traveled=180.0, max_bounces=25)
        await asyncio.sleep(2.4)
        k1 = int(float(await bhv.ctl.cdp.eval(KILLS % shooter)))
        hit = k1 > k0
        hits += 1 if hit else 0
        out['fire'].append({'shot': i + 1, 'angle': round(ang, 3), 'bounces': best['bounces'],
                            'traveled': best['traveled'], 'fired': ok,
                            'k0': k0, 'k1': k1, 'hit': hit})
        print('[ctl2] 第%d发：角=%.1f° 反弹=%d 行程=%.0fm 开火=%s 击杀分 %s→%s → %s'
              % (i + 1, math.degrees(ang), best['bounces'], best['traveled'] or 0,
                 ok, k0, k1, 'HIT' if hit else 'MISS'), flush=True)
        await asyncio.sleep(0.5)

    print('\n[ctl2] ===== 汇总 =====', flush=True)
    print('  输入粒度: %s s' % gran)
    for m in out['movement']:
        print('  移动: %s' % json.dumps(m, ensure_ascii=False))
    n_fire = sum(1 for f in out['fire'] if f.get('angle') is not None)
    print('  火控: %d/%d 命中（有效尝试 %d）' % (hits, n_fire, n_fire))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print('→ %s' % args.json)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
