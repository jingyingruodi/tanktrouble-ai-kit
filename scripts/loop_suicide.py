"""
TankTrouble 持续自杀脚本（Loop Suicide）
========================================
让所有本地可控坦克（游戏输入管理器里的 keyboard/arrows/mouse 角色）
每回合都自杀。等待重生后重复。

用途：观战 / 采集数据，同时保持可控的死亡循环。

适用场景：
  - 本地局（3 个本地角色）
  - 在线多人（本地玩家的坦克——只有本地可控坦克才有输入管理器）

用法：
  python loop_suicide.py [port] [--record out.jsonl] [--fps 20] [--allow-teleport]
  python loop_suicide.py [port] [--record out.jsonl] [--compress]
示例：
  python loop_suicide.py 9222
  python loop_suicide.py 9222 --record data.jsonl --fps 20
  python loop_suicide.py 9222 --compress --record data.jsonl

注意：传送（SetPositionAndAngle）已禁用/不再使用 —— 在线多人中服务器拥有
坐标，本地传送会被忽略/回弹。`--allow-teleport` 保留为 no-op 兼容参数，
仅为向后兼容旧启动脚本，只打印一条提示。

所有控制都走 TankCtl.set_input()（InputState -> setInputState），即服务器
接受的输入链路。直接调用 tank.setXxx 会被服务器覆盖（回弹）——详见
tankctl.py 文档。
转向约定（已实测）：RIGHT 增大旋转值，LEFT 减小。
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from tankctl import TankCtl
from recorder import Recorder, CompressedRecorder
from gameutils import (
    wrap_pi,
    get_round_state,
    get_controllable_tanks,
    tank_alive,
    get_maze_dims,
    find_nearest_wall,
    destroyed_tanks,
    TURN_RATE,
)


async def suicide_once(ctl, tank_id, world_w, world_h):
    """执行一次自杀。坦克死亡（被移除或冻结）时返回 True。

    可靠的贴墙反弹自杀：
      1. 找最近的墙（外墙 OR 内部迷宫墙）+ 正对墙法线的朝向
      2. 转向正对那面墙（收敛检查，物理转向）
      3. 行驶到离墙 ~3m（子弹出膛 ~2.5m，因此几乎立刻反弹回来）
      4. 开火

    控制全程在线安全：每个移动/转向/开火都走 ctl.set_input()
    （setInputState），绝不直接调坦克 setter。"""
    ts = await ctl.tanks()
    if tank_id not in ts:
        return True  # 已经不在了
    x, y, rot = ts[tank_id]['x'], ts[tank_id]['y'], ts[tank_id]['rot']

    # 实时读取迷宫尺寸 —— 迷宫每回合重新生成，尺寸会变化。
    world_w, world_h = await get_maze_dims(ctl, (world_w, world_h))

    # 可控性检查：回合模型中是否已标记摧毁？
    try:
        destroyed = await destroyed_tanks(ctl)
        if tank_id in destroyed:
            print(f'[suicide] tank {tank_id} marked destroyed — treating as dead', flush=True)
            return True
    except Exception:
        pass

    # 最近的墙（外墙 OR 内部迷宫墙）+ 正对墙的朝向
    wall, wall_dist, target_rot = await find_nearest_wall(ctl, x, y, world_w, world_h)

    # ── 1) 转向正对墙 —— 通过 setInputState 物理转向 ──
    ts_now = await ctl.tanks()
    if tank_id not in ts_now:
        return True
    cur_rot = ts_now[tank_id]['rot']
    diff = wrap_pi(target_rot - cur_rot)
    print(f'[suicide] wall={wall} dist={wall_dist:.1f}m cur_rot={cur_rot:.2f} target={target_rot:.2f} diff={diff:.2f}', flush=True)

    facing_ok = False
    for _attempt in range(4):
        ts_now = await ctl.tanks()
        if tank_id not in ts_now:
            return True
        cur = ts_now[tank_id]['rot']
        d = wrap_pi(target_rot - cur)
        if abs(d) < 0.35:
            facing_ok = True
            break
        # 通过 setInputState 物理转向（服务器确认）
        # 已实测约定：RIGHT 增大旋转值（游戏物理 + 实测：
        # LEFT→drot<0, RIGHT→drot>0）。d>0 = 需要更大旋转值 = 右转。
        turn_sec = min(abs(d) / TURN_RATE, 0.9)
        if d > 0:
            await ctl.set_input(tank_id, right=True, hold=turn_sec, freq=30)
        else:
            await ctl.set_input(tank_id, left=True, hold=turn_sec, freq=30)
        await asyncio.sleep(0.15)
    print(f'[suicide] facing={"OK" if facing_ok else "MISALIGNED"} after turn', flush=True)

    # ── 2) 分段短促前进驶向墙，每段后重校准朝向（setInputState 前进会让
    #       朝向轻微漂移），直到 ~3m。──
    TARGET = 3.0
    for drive_rounds in range(4):
        ts_now = await ctl.tanks()
        if tank_id not in ts_now:
            return True
        x2, y2 = ts_now[tank_id]['x'], ts_now[tank_id]['y']
        cur_dist = (await find_nearest_wall(ctl, x2, y2, world_w, world_h))[1]
        cur_rot2 = ts_now[tank_id]['rot']
        cur_facing = abs(wrap_pi(target_rot - cur_rot2))
        # 够近就停止行驶
        if cur_dist <= TARGET + 0.8:
            break
        # 朝向漂移则重新对准
        if cur_facing > 0.25:
            align_sec = min(cur_facing / 4.7, 0.5)
            if wrap_pi(target_rot - cur_rot2) > 0:
                await ctl.set_input(tank_id, right=True, hold=align_sec, freq=30)
            else:
                await ctl.set_input(tank_id, left=True, hold=align_sec, freq=30)
            await asyncio.sleep(0.1)
        # 短促前进（0.5s ~ 4m）
        print(f'[suicide] drive toward wall {cur_dist:.1f}m (0.5s)', flush=True)
        await ctl.set_input(tank_id, fwd=True, hold=0.5, freq=30)
        if drive_rounds >= 3:
            break

    # ── 3) 最终重读位置 + 朝向；大致对准才开火。──
    ts_now = await ctl.tanks()
    if tank_id not in ts_now:
        return True
    x3, y3 = ts_now[tank_id]['x'], ts_now[tank_id]['y']
    final_dist = (await find_nearest_wall(ctl, x3, y3, world_w, world_h))[1]
    final_rot = ts_now[tank_id]['rot']
    facing = abs(wrap_pi(target_rot - final_rot)) < 0.6 or wall_dist <= 6.0

    if not facing:
        print(f'[suicide] SKIP fire: misaligned (dist={final_dist:.1f}m) — retry next round', flush=True)
        return False

    print(f'[suicide] FINAL wall={wall} dist={final_dist:.1f}m facing=OK — FIRING', flush=True)
    await ctl.set_input(tank_id, fire=True, hold=0.3, freq=30)

    # ── 4) 等待死亡：子弹应在几秒内反弹回来。──
    # 死亡判定：坦克被从 getTanks() 中移除，或原地冻结（冻结尸体——
    # 速度归零但对象仍存在）。
    last_pos = None
    for _ in range(8):  # 最多 ~4s
        await asyncio.sleep(0.5)
        ts2 = await ctl.tanks()
        if tank_id not in ts2:
            return True  # 死亡（被移除）
        pos = (ts2[tank_id]['x'], ts2[tank_id]['y'])
        if last_pos is not None and abs(pos[0] - last_pos[0]) < 0.01 and abs(pos[1] - last_pos[1]) < 0.01:
            return True  # 死亡（冻结）
        last_pos = pos
    return False


async def main():
    args = sys.argv[1:]
    port = int(args[0]) if len(args) > 0 and args[0].isdigit() else 9222

    record_out = None
    rec_fps = 20
    allow_teleport = False
    compress = False
    if '--record' in args:
        record_out = args[args.index('--record') + 1]
    if '--fps' in args:
        rec_fps = int(args[args.index('--fps') + 1])
    if '--allow-teleport' in args:
        allow_teleport = True
    if '--compress' in args:
        compress = True

    ctl = TankCtl(port=port)
    await ctl.connect()

    # 录制时使用 monitor_server 的完整 STATE_JS（丰富快照）
    full_state_js = None
    mpath = os.path.join(os.path.dirname(__file__), '..', 'monitor_server.py')
    if os.path.exists(mpath):
        src = open(mpath, encoding='utf-8').read()
        m = re.search(r'STATE_JS = r"""(.*?)"""', src, re.S)
        if m:
            full_state_js = m.group(1)

    rec = (CompressedRecorder(record_out) if compress else Recorder(record_out)) if record_out else None
    if rec:
        mode = 'COMPRESSED' if compress else 'full'
        print(f'[loop-suicide] recording [{mode}] -> {record_out} @{rec_fps}fps', flush=True)
    if allow_teleport:
        print('[loop-suicide] teleport ALLOWED (single-player opt-in)', flush=True)
    else:
        print('[loop-suicide] teleport DISABLED (server-owned coords, online-safe)', flush=True)

    async def recorder_task():
        """持续状态采样 —— 与自杀主循环并行运行。
        以 rec_fps 采样完整游戏状态，回放流畅，而不是每回合才一帧。"""
        if not rec:
            return
        while True:
            try:
                if full_state_js:
                    st = await ctl.cdp.eval(full_state_js)
                    if not isinstance(st, dict) or 'err' in st:
                        st = await ctl.state()
                else:
                    st = await ctl.state()
                if isinstance(st, dict):
                    # 动作标签：把最近下发的输入一并写入（AI-B 模仿学习/离线 RL
                    # 需要 state+action 成对；无标签的老数据只能做状态分析）。
                    acts = ctl.current_actions() if hasattr(ctl, 'current_actions') else None
                    rec.record(st, action=acts or None)
            except Exception:
                pass
            await asyncio.sleep(1.0 / rec_fps)

    # 等待游戏 + 迷宫
    maze_dims = await get_maze_dims(ctl, None)
    if not maze_dims:
        for _ in range(30):
            maze_dims = await get_maze_dims(ctl, None)
            if maze_dims:
                break
            await asyncio.sleep(1)
    if not maze_dims:
        print('[loop-suicide] no maze (game not loaded?)', flush=True)
        return 1
    world_w, world_h = maze_dims
    print(f'[loop-suicide] maze {world_w/10:.0f}x{world_h/10:.0f} tiles = {world_w:.0f}x{world_h:.0f}m', flush=True)

    # 启动采样任务（并行运行；随进程自行结束）
    asyncio.create_task(recorder_task())

    suicide_count = 0
    state_log = None

    # 跟踪所有本地可控坦克：{playerId: role}
    # 周期性刷新，以便重生 / 新回合保持同步。
    controllable = {}  # playerId -> role
    while True:
        # 状态转换
        rs = await get_round_state(ctl)
        if rs != state_log:
            print(f'[loop-suicide] round state: {rs}', flush=True)
            state_log = rs

        # 刷新可控集合（输入管理器是权威来源）
        ctl_tanks = await get_controllable_tanks(ctl)
        if ctl_tanks and set(ctl_tanks.keys()) != set(controllable.keys()):
            print(f'[loop-suicide] controllable tanks: {ctl_tanks}', flush=True)
            controllable = ctl_tanks

        if rs == 'in round':
            any_done = False
            for tank_id, role in list(controllable.items()):
                if not await tank_alive(ctl, tank_id):
                    continue  # 已死亡/移除，等待重生
                suicide_count += 1
                print(f'[loop-suicide] #{suicide_count} role={role} tank={tank_id} -> suicide', flush=True)
                await suicide_once(ctl, tank_id, world_w, world_h)
                print(f'[loop-suicide] #{suicide_count} done', flush=True)
                any_done = True
                # 击杀不同坦克之间留小间隔（避免武器/回合冲突）
                await asyncio.sleep(0.8)
            if any_done:
                # 等待重生后再进行下一轮（回合切换也会重置）
                await asyncio.sleep(3.0)
            else:
                await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(0.3)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\n[loop-suicide] stopped')
