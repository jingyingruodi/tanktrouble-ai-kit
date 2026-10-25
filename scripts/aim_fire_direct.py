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
"""
瞄准链路定位实验：直接 setRotation vs 渐进转向
==============================================
目的：区分"打空"的原因是【弹道/角度算错】还是【转向不准】。
  A) 直接 setRotation(rot = aim_angle + π/2) 再开火 —— 排除转向误差
  B) 用 aim_and_fire（turn_to_rot 渐进转向）再开火
两者各打若干发（每次重开一局，目标静止），比较命中率（击杀分是否增加）。

用法：python scripts/aim_fire_direct.py auto --mode direct|turn
"""
import argparse
import asyncio
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tank_behavior_template import TankBehavior      # noqa: E402

STATE = """(() => { const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({err: 'no game'});
  return JSON.stringify({round: String(gc.model.getState()),
                         tanks: Object.keys(gc.getTanks() || {})}); })()"""


async def kills(bhv, pid):
    v = await bhv.ctl.cdp.eval(
        "(() => { try { const s = GameManager.getGameController()"
        ".getScoreByPlayerIdAndType('%s', 0); return s ? s.getValue() : -1; } catch (e) { return -2; } })()" % pid)
    return int(v) if v is not None else -1


async def main():
    ap = argparse.ArgumentParser(description='瞄准链路定位')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--mode', choices=['direct', 'turn'], default='direct')
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    # 等 in round
    st = None
    for _ in range(40):
        st = json.loads(await bhv.ctl.cdp.eval(STATE))
        if st.get('round') == 'in round' and len(st.get('tanks') or []) >= 2:
            break
        await asyncio.sleep(1)
    if not st or st.get('round') != 'in round':
        print('[aimtest] MISS(no round)')
        await bhv.ctl.cdp.close()
        return 0
    roles = await bhv.get_roles()
    ids = list(roles.keys())
    a, b = ids[0], ids[1]
    tp = await bhv.get_position(b)
    k0 = await kills(bhv, a)
    ang = await bhv.find_aim_angle_raycast(a, tp, target_id=b, require_bounce=True,
                                            max_traveled=180.0, max_bounces=25)
    if ang is None:
        print('[aimtest] SKIP(no angle)')
        await bhv.ctl.cdp.close()
        return 0
    if args.mode == 'direct':
        # 方向角 → 游戏 rot：forward = rot - π/2 → rot = ang + π/2
        rot = ang + math.pi / 2
        await bhv.ctl.cdp.eval(
            "(() => { const t = GameManager.getGameController().getTank('%s');"
            " if (!t) return 'no tank'; t.setRotation(%f); return 'ok'; })()" % (a, rot))
        await asyncio.sleep(0.25)
        await bhv.ctl.set_input(a, fire=True, hold=0.3)
        fired = 'direct'
        actual_rot = await bhv.get_rotation(a)
    else:
        fired = await bhv.aim_and_fire(a, tp, fire_sec=0.3, target_id=b, raycast=True,
                                       lead=False, require_bounce=True, max_traveled=180.0,
                                       max_bounces=25)
        actual_rot = await bhv.get_rotation(a)
    await asyncio.sleep(2.5)
    k1 = await kills(bhv, a)
    hit = (k1 > k0) if (k0 >= 0 and k1 >= 0) else False
    print('[aimtest] mode=%s ang=%.3f rot_target=%.3f rot_actual=%s k=%s→%s %s'
          % (args.mode, ang, ang + math.pi / 2, actual_rot, k0, k1, 'HIT' if hit else 'MISS'))
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
