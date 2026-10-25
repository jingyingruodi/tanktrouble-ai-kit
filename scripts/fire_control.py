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
TankTrouble 自动火控脚本（Fire Control）
========================================
基于模板 + 弹道模拟器，自动瞄准能命中的敌方坦克开火。

功能：
  - 识别可控坦克（本地角色）和敌方坦克（其他玩家）
  - 对每个敌方，计算直线/反弹命中角度（ballistics 镜面反射推演）
  - 转向命中角并开火
  - 周期刷新墙段（每回合一次）、敌方位置（实时）

这是"算法派"第一个落地示例：弹道是确定性物理，精确计算优于盲射。

用法：
  python fire_control.py [port] [--fps 20]
示例：
  python fire_control.py 9222
"""
import asyncio
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from tank_behavior_template import TankBehavior  # 复用模板行为层
from gameutils import wrap_pi


async def main():
    args = sys.argv[1:]
    # 支持 auto / TT_CDP_PORT：统一解析（见 cdp_tools/port_arg.py）
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cdp_tools')
    if _p not in sys.path:
        sys.path.insert(0, _p)
    from port_arg import port_from_args  # noqa: E402
    port = port_from_args(args)

    bhv = TankBehavior(port=port)
    await bhv.connect()

    # 1) 等待回合开始，缓存墙段
    while not await bhv.is_round_active():
        await asyncio.sleep(0.5)
    await bhv.get_walls_cached(refresh=True)
    print('[firectl] 回合开始，墙段已加载', flush=True)

    # 2) 识别可控坦克（本地角色）
    roles = await bhv.get_roles()
    if not roles:
        print('[firectl] 没有可控坦克', flush=True)
        return 1
    my_pids = list(roles.keys())
    print(f'[firectl] 可控: {roles}', flush=True)

    # 3) 火控主循环
    shot_count = 0
    while True:
        # 回合切换时刷新墙段
        if not await bhv.is_round_active():
            await asyncio.sleep(0.5)
            continue
        await bhv.get_walls_cached(refresh=False)

        ts = await bhv.get_tanks()
        # 找敌方：在场且不是我方可控的坦克
        enemies = [pid for pid in ts if pid not in my_pids]
        if not enemies:
            await asyncio.sleep(0.3)
            continue

        # 对每个可控坦克，尝试打最近的敌人
        fired = False
        for me in my_pids:
            if not await bhv.is_alive(me):
                continue
            pos = await bhv.get_position(me)
            if not pos:
                continue
            # 找最近的敌方
            best_enemy = None
            best_d = 1e9
            for e in enemies:
                ep = await bhv.get_position(e)
                if not ep:
                    continue
                d = math.hypot(ep[0] - pos[0], ep[1] - pos[1])
                if d < best_d:
                    best_d = d
                    best_enemy = ep
            if best_enemy is None:
                continue
            # 自动火控：找命中角并开火
            ok = await bhv.aim_and_fire(me, best_enemy)
            if ok:
                shot_count += 1
                print(f'[firectl] #{shot_count} {me} → 命中 ({best_enemy[0]:.1f},{best_enemy[1]:.1f})', flush=True)
                fired = True
            else:
                print(f'[firectl] {me} 找不到命中角（直线被墙挡，无反弹路径）', flush=True)
            # 短暂间隔，避免连续开火
            await asyncio.sleep(0.8)

        await asyncio.sleep(0.3)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[firectl] 已停止')
