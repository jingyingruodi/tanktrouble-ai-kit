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
炮口偏移 A/B 实验：预测起点用【车体中心(0m)】还是【真实炮口(2.5m)】
==================================================================
背景：严格火控验证里 26~35m 的反弹弹反复打空，而弹道模型本身准确（预测 vs 实测 0.2m）。
推断：真实弹丸从炮口前 2.5m 出膛（Constants.BULLET.OFFSET=2.5），而预测从车体中心算，
      直线弹道不受影响，但反弹路径的反射点会整体平移 → 远距反弹弹偏出。
本实验对同一静止目标，两种起点各打 K 发，比较命中率（目标死亡/击杀分增加）。

用法：python scripts/muzzle_offset_ab.py auto [--tries 8]
"""
import argparse
import asyncio
import json
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
    try:
        v = await bhv.ctl.cdp.eval(
            "(() => { try { const s = GameManager.getGameController()"
            ".getScoreByPlayerIdAndType('%s', 0); return s ? s.getValue() : -1; }"
            " catch (e) { return -2; } })()" % pid)
        return int(v) if v is not None else -1
    except Exception:
        return -1


async def wait_round(bhv, timeout=90):
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        st = json.loads(await bhv.ctl.cdp.eval(STATE))
        if st.get('round') == 'in round' and len(st.get('tanks') or []) >= 2:
            return st
        await asyncio.sleep(1)
    return None


async def main():
    ap = argparse.ArgumentParser(description='炮口偏移 A/B')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--tries', type=int, default=8, help='每种配置尝试次数')
    ap.add_argument('--cfg', type=float, default=None,
                    help='单次试验模式：用该 muzzle_offset 打一发，打印 HIT/MISS 后退出')
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()

    if args.cfg is not None:
        st = await wait_round(bhv, timeout=60)
        if not st:
            print('[ab] MISS(no round)')
            await bhv.ctl.cdp.close()
            return 0
        roles = await bhv.get_roles()
        ids = list(roles.keys())
        if len(ids) < 2:
            print('[ab] MISS(no 2 pids)')
            await bhv.ctl.cdp.close()
            return 0
        a, b = ids[0], ids[1]
        tp = await bhv.get_position(b)
        if tp is None:
            print('[ab] MISS(no target pos)')
            await bhv.ctl.cdp.close()
            return 0
        k0 = await kills(bhv, a)
        ang = await bhv.find_aim_angle_raycast(a, tp, target_id=b, require_bounce=True,
                                               max_traveled=180.0, max_bounces=25,
                                               muzzle_offset=args.cfg)
        if ang is None:
            print('[ab] SKIP(no angle)')
            await bhv.ctl.cdp.close()
            return 0
        await bhv.aim_and_fire(a, tp, fire_sec=0.3, target_id=b, raycast=True,
                               lead=False, require_bounce=True, max_traveled=180.0,
                               max_bounces=25, muzzle_offset=args.cfg)
        await asyncio.sleep(2.5)
        k1 = await kills(bhv, a)
        hit = (k1 > k0) if (k0 >= 0 and k1 >= 0) else False
        print('[ab] cfg=%.1f ang=%.3f k=%s→%s %s' % (args.cfg, ang, k0, k1, 'HIT' if hit else 'MISS'))
        await bhv.ctl.cdp.close()
        return 0
    results = {}
    for cfg in (0.0, 2.5):
        hits = 0
        tries = 0
        for i in range(args.tries):
            st = await wait_round(bhv, timeout=60)
            if not st:
                print('[ab] 等不到新回合，停止 cfg=%.1f' % cfg, flush=True)
                break
            roles = await bhv.get_roles()
            ids = list(roles.keys())
            if len(ids) < 2:
                await asyncio.sleep(2)
                continue
            a, b = ids[0], ids[1]
            tp = await bhv.get_position(b)
            if tp is None:
                await asyncio.sleep(1)
                continue
            k0 = await kills(bhv, a)
            ang = await bhv.find_aim_angle_raycast(a, tp, target_id=b, require_bounce=True,
                                                   max_traveled=180.0, max_bounces=25,
                                                   muzzle_offset=cfg)
            tries += 1
            if ang is None:
                print('[ab] cfg=%.1f 第%d次：无命中角（跳过）' % (cfg, i + 1), flush=True)
                continue
            ok = await bhv.aim_and_fire(a, tp, fire_sec=0.3, target_id=b, raycast=True,
                                        lead=False, require_bounce=True, max_traveled=180.0,
                                        max_bounces=25, muzzle_offset=cfg)
            await asyncio.sleep(2.5)
            k1 = await kills(bhv, a)
            hit = (k1 > k0) if (k0 >= 0 and k1 >= 0) else False
            if hit:
                hits += 1
            print('[ab] cfg=%.1f 第%d次：角=%s 开火=%s 击杀分变化=%s→%s → %s'
                  % (cfg, i + 1, None if ang is None else round(ang, 3), ok, k0, k1,
                     '命中' if hit else '未中'), flush=True)
            # 若回合没结束（未命中），继续下一次（同一回合再打）
            if not hit:
                await asyncio.sleep(1)
        results['%.1f' % cfg] = {'hits': hits, 'tries': tries}
        print('\n[ab] cfg=%.1f 结果：%d/%d 命中' % (cfg, hits, tries), flush=True)

    print('\n[ab] ===== 汇总 =====', flush=True)
    for k, v in results.items():
        print('  muzzle_offset=%s m → %d/%d' % (k, v['hits'], v['tries']), flush=True)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
