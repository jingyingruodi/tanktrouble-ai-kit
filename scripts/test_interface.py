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
TankTrouble 接口测试
====================
对单辆坦克的 SET/GET 自动验证：
  1. 左转 0.5s   -> 验证旋转发生变化（LEFT 减小旋转值）
  2. 右转 0.5s   -> 验证旋转发生变化（RIGHT 增大旋转值）
  3. 前进 0.5s   -> 验证位置移动
  4. 后退 0.5s   -> 验证位置移动回来
  5. 开火 0.15s  -> 验证生成了一颗弹丸

用法：python test_interface.py [port] [tank_index]
"""
import asyncio
import os
import sys

# 无论从 scripts/ 还是项目根目录运行，都能 import cdp_tools
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from tankctl import TankCtl


async def main(port=None, tank_idx=None):
    # 既支持命令行 `python test_interface.py 9222 0`，也支持 tt.py 传参调用。
    if port is None:
        port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 9222
    if tank_idx is None:
        tank_idx = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0

    ctl = TankCtl(port=port)
    await ctl.connect()
    tank_id = None

    # 等待可控坦克出现
    print('[test] waiting for tanks...', flush=True)
    for _ in range(30):
        ts = await ctl.tanks()
        if ts:
            tank_id = await ctl.my_tank(tank_idx)
            break
        await asyncio.sleep(1)
    if not tank_id:
        print('[test] FAIL: no tanks found', flush=True)
        return 1
    print(f'[test] controlling tank {tank_id}', flush=True)

    results = []

    # ─── 1. 左转 ───
    before = await ctl.tanks()
    await ctl.turn_left(0.5, tank_id)
    d = await ctl.diff(before, tank_id)
    rotated_l = bool(d and d['rotated'])
    results.append(('turn_left', rotated_l))
    print(f'[test] turn_left -> rotated={rotated_l} drot={d["drot"]:.3f}' if d else '[test] turn_left FAIL', flush=True)

    # ─── 2. 右转（转回起点方向）───
    before2 = await ctl.tanks()
    await ctl.turn_right(0.5, tank_id)
    d2 = await ctl.diff(before2, tank_id)
    rotated_r = bool(d2 and d2['rotated'])
    results.append(('turn_right', rotated_r))
    print(f'[test] turn_right -> rotated={rotated_r} drot={d2["drot"]:.3f}' if d2 else '[test] turn_right FAIL', flush=True)

    # ─── 3. 前进 ───
    before3 = await ctl.tanks()
    await ctl.forward(0.5, tank_id)
    d3 = await ctl.diff(before3, tank_id)
    moved_f = bool(d3 and d3['moved'])
    results.append(('forward', moved_f))
    print(f'[test] forward -> moved={moved_f} d=({d3["dx"]:.3f},{d3["dy"]:.3f})' if d3 else '[test] forward FAIL', flush=True)

    # ─── 4. 后退 ───
    before4 = await ctl.tanks()
    await ctl.back(0.5, tank_id)
    d4 = await ctl.diff(before4, tank_id)
    moved_b = bool(d4 and d4['moved'])
    results.append(('backward', moved_b))
    print(f'[test] backward -> moved={moved_b} d=({d4["dx"]:.3f},{d4["dy"]:.3f})' if d4 else '[test] backward FAIL', flush=True)

    # ─── 5. 开火 ───
    st = await ctl.state()
    proj_before = st.get('projectiles', []) if isinstance(st, dict) else []
    pcount_before = len(proj_before)
    await ctl.fire(0.15, tank_id)
    await asyncio.sleep(0.3)
    st2 = await ctl.state()
    proj_after = st2.get('projectiles', []) if isinstance(st2, dict) else []
    pcount_after = len(proj_after)
    fired = pcount_after > pcount_before
    results.append(('fire', fired))
    print(f'[test] fire -> projectiles {pcount_before} -> {pcount_after} fired={fired}', flush=True)

    # ─── 汇总 ───
    print('\n=== TEST SUMMARY ===', flush=True)
    ok = 0
    for name, passed in results:
        print(f'  {name}: {"PASS" if passed else "FAIL"}', flush=True)
        ok += 1 if passed else 0
    print(f'  {ok}/{len(results)} passed', flush=True)
    return 0 if ok == len(results) else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
