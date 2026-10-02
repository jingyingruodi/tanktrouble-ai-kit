"""
TankTrouble 一次性自杀脚本（墙反弹自伤）- FINAL
==============================================
已验证：转向正对最近的墙，开火，弹丸撞墙反弹回来击杀坦克。

已验证映射：forward_angle = tank_rot - PI/2
  rot=0     -> 朝上 (-y)
  rot=PI/2  -> 朝右 (+x)

转向速率：约 1.35 rad / 0.3s（按住 setLeft/setRight）
  （转向速度约 4.5 rad/s）

用法：python suicide_script.py [port] [tank_index]
"""
import asyncio
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from tankctl import TankCtl
from gameutils import wrap_pi, TURN_RATE


async def main(port=None, tank_idx=None):
    # 既支持命令行 `python suicide_script.py 9222 0`，也支持 tt.py 传参调用。
    if port is None:
        port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 9222
    if tank_idx is None:
        tank_idx = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0

    ctl = TankCtl(port=port)
    await ctl.connect()

    tank_id = None
    for _ in range(20):
        ts = await ctl.tanks()
        if ts:
            tank_id = await ctl.my_tank(tank_idx)
            break
        await asyncio.sleep(1)
    if not tank_id:
        print('[suicide] no tanks', flush=True)
        return 1

    # 迷宫尺寸
    maze_js = await ctl.cdp.eval('(() => { const gc=GameManager.getGameController()||GameManager.gameController; const m=gc.getMaze?gc.getMaze():null; return m ? JSON.stringify({w:m.getWidth(),h:m.getHeight()}) : null; })()')
    if not maze_js:
        print('[suicide] no maze', flush=True)
        return 1
    md = json.loads(maze_js)
    world_w, world_h = md['w'] * 10.0, md['h'] * 10.0

    ts = await ctl.tanks()
    x, y, rot = ts[tank_id]['x'], ts[tank_id]['y'], ts[tank_id]['rot']
    print(f'[suicide] tank {tank_id} @({x:.1f},{y:.1f}) rot={rot:.2f}', flush=True)

    # 最近的墙（仅外墙）
    dists = {'top': y, 'bottom': world_h - y, 'left': x, 'right': world_w - x}
    wall = min(dists, key=dists.get)
    wall_dist = dists[wall]
    target_fwd = {'top': -math.pi/2, 'bottom': math.pi/2, 'left': math.pi, 'right': 0.0}[wall]
    target_rot = target_fwd + math.pi / 2
    print(f'[suicide] wall={wall} dist={wall_dist:.1f}m', flush=True)

    # 转向正对墙
    # 已实测约定（游戏物理 + 实测）：RIGHT 增大旋转值，LEFT 减小。
    # diff 为正（需要更大旋转值）-> 右转。
    diff = wrap_pi(target_rot - rot)
    turn_time = min(abs(diff) / TURN_RATE, 3.0)
    if abs(diff) > 0.05:
        if diff > 0:
            await ctl.turn_right(turn_time, tank_id)
        else:
            await ctl.turn_left(turn_time, tank_id)
        await asyncio.sleep(0.3)
    ts2 = await ctl.tanks()
    print(f'[suicide] after turn rot={ts2[tank_id]["rot"]:.2f}', flush=True)

    # 太贴墙则后退（留出反弹空间，目标 ~8m 远）
    x3, y3 = ts2[tank_id]['x'], ts2[tank_id]['y']
    new_dist = {'top': y3, 'bottom': world_h - y3, 'left': x3, 'right': world_w - x3}[wall]
    if new_dist < 6:
        back_t = min((6 - new_dist) / 10.0, 1.0)
        await ctl.back(back_t, tank_id)
        await asyncio.sleep(0.3)

    # 向墙开火
    print('[suicide] FIRING at wall...', flush=True)
    await ctl.fire(0.25, tank_id)
    await asyncio.sleep(0.5)
    st = await ctl.state()
    projs = st.get('projectiles', []) if isinstance(st, dict) else []
    if projs:
        p = projs[-1]
        print(f'[suicide] proj @({p["x"]:.1f},{p["y"]:.1f}) v=({p["vx"]:.1f},{p["vy"]:.1f})', flush=True)
    else:
        print('[suicide] no projectile spawned!', flush=True)

    # 等待反弹回来 + 自伤击杀
    print('[suicide] waiting for bounce-back kill...', flush=True)
    for i in range(40):
        await asyncio.sleep(0.5)
        ts4 = await ctl.tanks()
        if tank_id not in ts4:
            print(f'[suicide] SUCCESS: tank destroyed at t={i*0.5:.0f}s!', flush=True)
            return 0
        if i % 6 == 0:
            cur = ts4[tank_id]
            print(f'[suicide] t={i*0.5:.0f}s @({cur["x"]:.1f},{cur["y"]:.1f})', flush=True)

    # 检查冻结（已死但对象仍在）
    ts5 = await ctl.tanks()
    if tank_id in ts5:
        p1 = (ts5[tank_id]['x'], ts5[tank_id]['y'])
        await asyncio.sleep(1.5)
        ts6 = await ctl.tanks()
        if tank_id in ts6:
            p2 = (ts6[tank_id]['x'], ts6[tank_id]['y'])
            if abs(p2[0]-p1[0]) < 0.01 and abs(p2[1]-p1[1]) < 0.01:
                print(f'[suicide] tank frozen @{p1} - dead (object persists)', flush=True)
                return 0
    print('[suicide] suicide failed', flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
