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
火控验证测试：方向键角色(arrows) 打 WASD角色(keyboard)。
固定测试变量 + 录制 + 监控击杀分变化。

判定：
- 每轮记录双方击杀分(type0)
- 若 arrows 击杀分增加 → 火控有效（打了对面）
- 若 keyboard 击杀分增加 → 自己人打自己（火控缺陷）
- 超过3分钟击杀分无变化 → 火控缺陷，需分析录制

用法：
  python fire_verify.py [port] [--out rec.jsonl] [--dur 180]
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

SCORE_KILL = 0  # getScoreByPlayerIdAndType 的击杀类型号


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
    out = 'fire_verify.jsonl'
    dur = 180.0
    if '--out' in args:
        out = args[args.index('--out') + 1]
    if '--dur' in args:
        dur = float(args[args.index('--dur') + 1])

    bhv = TankBehavior(port=port)
    await bhv.connect()
    await bhv.get_walls_cached(refresh=True)

    # 严格指定角色：方向键打 WASD
    attacker = await bhv.pick_role_strict('arrows')
    target = await bhv.pick_role_strict('keyboard')
    if not attacker or not target:
        print(f'[verify] 缺少角色: arrows={attacker} keyboard={target}', flush=True)
        return 1
    print(f'[verify] 攻击者(arrows)={attacker} 目标(keyboard)={target}', flush=True)

    # 录制器
    mpath = os.path.join(os.path.dirname(__file__), '..', 'monitor_server.py')
    m = re.search(r'STATE_JS = r"""(.*?)"""', open(mpath, encoding='utf-8').read(), re.S)
    state_js = m.group(1)
    rec = CompressedRecorder(out)
    print(f'[verify] 录制 -> {out}', flush=True)

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

    rec_task = asyncio.create_task(recorder_task())
    try:
        t_start = asyncio.get_event_loop().time()
        base_a = await get_kills(attacker)
        base_t = await get_kills(target)
        print(f'[verify] 基线击杀: attacker={base_a} target={base_t}', flush=True)
        shot = 0
        no_change_since = None
        while asyncio.get_event_loop().time() - t_start < dur:
            # 每 10 发刷新墙段
            if shot % 10 == 0:
                await bhv.get_walls_cached(refresh=True)
            if not await bhv.is_round_active():
                await asyncio.sleep(0.5)
                continue
            ap = await bhv.get_position(attacker)
            tp = await bhv.get_position(target)
            if not ap or not tp:
                await asyncio.sleep(0.5)
                continue
            # 火控：raycast + lead 打键盘角色
            ang = await bhv.find_aim_angle_raycast(attacker, tp)
            if ang is None:
                # 机动火控：当前位置打不到，导航到目标上方候选射击位
                print(f'[verify] 当前位置打不到，机动到目标上方...', flush=True)
                # 候选射击位：目标上方 10m（避开墙）
                moved = await bhv.navigate_to(attacker, (tp[0], tp[1] - 10), arrival=2.0)
                print(f'[verify] 机动结果: {moved}', flush=True)
                await asyncio.sleep(0.5)
                continue
            ok = await bhv.aim_and_fire(attacker, tp, raycast=True, lead=True, target_id=target)
            if ok:
                shot += 1
                print(f'[verify] #{shot} 开火', flush=True)
            await asyncio.sleep(1.0)
            # 检查击杀分
            ca = await get_kills(attacker)
            ct = await get_kills(target)
            if ca != base_a or ct != base_t:
                print(f'[verify] ★ 分数变化! attacker={ca}(原{base_a}) target={ct}(原{base_t})', flush=True)
                if ca > base_a:
                    print(f'[verify] ★★ 攻击者击杀数增加 → 火控有效！', flush=True)
                if ct > base_t:
                    print(f'[verify] ⚠️ 目标(自己人)击杀数增加 → 火控打到队友！', flush=True)
                base_a, base_t = ca, ct
                no_change_since = None     # 分数变了 → 重新开始 3 分钟计时
            # 3 分钟无变化判定（只在分数长期不变时才判失败）
            if no_change_since is None:
                no_change_since = asyncio.get_event_loop().time()
            if asyncio.get_event_loop().time() - no_change_since > 180:
                print('[verify] ⚠️ 3分钟无击杀分变化 → 火控可能存在缺陷', flush=True)
                break
    finally:
        rec_task.cancel()
        rec.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[verify] 已停止')
