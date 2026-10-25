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
火控 + 录制整合脚本（Fire Control + Record）
============================================
在单人/多人模式中执行自动火控（瞄准目标开火），同时把完整游戏
状态录制到 JSONL（压缩格式），便于事后回放/分析弹道是否正确命中。

用法：
  python fire_record.py [port] [--out file.jsonl] [--fps 20] [--dur 120]
  python fire_record.py 9222 --out fire_data.jsonl --dur 60
"""
import asyncio
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tank_behavior_template import TankBehavior
from recorder import CompressedRecorder


async def main():
    args = sys.argv[1:]
    # 支持 auto：自动发现 dsh-ego-browser 的动态 CDP 端口（见 cdp_tools/ego_browser.py）
    if len(args) > 0 and args[0] == 'auto':
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cdp_tools')
        if _p not in sys.path:
            sys.path.insert(0, _p)
        from ego_browser import resolve_port
        port = resolve_port('auto')
    else:
        port = int(args[0]) if len(args) > 0 and args[0].isdigit() else 9222
    out = 'fire_data.jsonl'
    fps = 20
    dur = 0  # 0 = 无限
    if '--out' in args:
        out = args[args.index('--out') + 1]
    if '--fps' in args:
        fps = int(args[args.index('--fps') + 1])
    if '--dur' in args:
        dur = float(args[args.index('--dur') + 1])

    bhv = TankBehavior(port=port)
    await bhv.connect()
    await bhv.get_walls_cached(refresh=True)

    # 录制器（复用 monitor 完整 STATE_JS）
    mpath = os.path.join(os.path.dirname(__file__), '..', 'monitor_server.py')
    m = re.search(r'STATE_JS = r"""(.*?)"""', open(mpath, encoding='utf-8').read(), re.S)
    state_js = m.group(1)
    rec = CompressedRecorder(out)
    print(f'[fire-record] 录制 -> {out} @{fps}fps', flush=True)

    async def recorder_task():
        """后台采样完整状态，与火控并行。"""
        while True:
            try:
                st = await bhv.ctl.cdp.eval(state_js)
                if isinstance(st, dict) and 'err' not in st:
                    rec.record(st, action=(bhv.ctl.current_actions() or None))
            except Exception:
                pass
            await asyncio.sleep(1.0 / fps)

    # 识别可控/目标坦克
    roles = await bhv.get_roles()
    if not roles:
        print('[fire-record] 无可控坦克', flush=True)
        return 1
    attackers = list(roles.keys())
    # 目标：另一辆可控（单人局自测）或场上的其他坦克
    print(f'[fire-record] 可控: {roles}', flush=True)

    rec_task = asyncio.create_task(recorder_task())
    shot = 0
    t0 = asyncio.get_event_loop().time()
    try:
        while True:
            if dur > 0 and asyncio.get_event_loop().time() - t0 > dur:
                break
            if not await bhv.is_round_active():
                await asyncio.sleep(0.5)
                continue
            # 每 10 次开火刷新墙段（回合切换时迷宫变了）
            if shot % 10 == 0:
                await bhv.get_walls_cached(refresh=True)
            ts = await bhv.get_tanks()
            if not ts:
                await asyncio.sleep(0.3)
                continue
            # 找在场坦克作为目标（取第一个不是攻击者的）
            target = None
            for pid in ts:
                if pid not in attackers:
                    target = pid
                    break
            if target is None:
                # 单人局：用另一辆可控坦克当靶子
                others = [p for p in attackers if p != attackers[0]]
                if others:
                    target = others[0]
                else:
                    await asyncio.sleep(0.3)
                    continue
            tp = await bhv.get_position(target)
            if not tp:
                await asyncio.sleep(0.5)
                continue
            # 每个攻击者开火
            for me in attackers:
                if not await bhv.is_alive(me):
                    continue
                ap = await bhv.get_position(me)
                if not ap:
                    continue
                ang = await bhv.find_aim_angle_raycast(me, tp)
                if ang is None:
                    # 无命中角：尝试直线盲射（演示）
                    ang = math.atan2(tp[1] - ap[1], tp[0] - ap[0])
                rot = ang + math.pi / 2
                await bhv.turn_to_rot(me, rot, tol=0.12)
                await bhv.fire(me, 0.25)
                shot += 1
                print(f'[fire-record] #{shot} {me} → 目标({tp[0]:.0f},{tp[1]:.0f}) '
                      f'角度={math.degrees(ang):.0f}°', flush=True)
                await asyncio.sleep(0.8)
            await asyncio.sleep(0.3)
    finally:
        rec_task.cancel()
        rec.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[fire-record] 已停止')
