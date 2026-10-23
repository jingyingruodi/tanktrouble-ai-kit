"""
TankTrouble 权威常量（单一数据源）
==================================
来源优先级（从高到低）：
  1. **官方反混淆源码** `D:/AI Agent/game_source/constants.js`
     —— 本文件所有数值都与该文件逐项对齐（px 与 m 两套单位，PIXELS_PER_METER=20）
  2. **运行时实测**（我们的探针脚本，作为交叉校验）
     - 前进速度：实测 15.95 m/s（物理体线速度恒定，无加速段）✅ 与常量一致
     - 转向速度：实测 4.66~5.17 rad/s ✅ 与常量 5.0 一致
     - 弹丸半径：基础弹运行时 p2 circle 实测 0.25m ✅ 与常量一致

⚠️ 历史错误：早期文档/脚本曾用 `TANK 前进 7.5 m/s、转向 4.5 rad/s`
（实测与官方常量都表明**前进是 15.95 m/s、转向 5.0 rad/s**），
本模块是修正后的唯一来源，新代码不要再硬编码这些数字。
"""

# ── 单位 ───────────────────────────────────────────────
PIXELS_PER_METER = 20.0
METERS_PER_PIXEL = 0.05
PATH_MIN_STEP_LENGTH = 0.01

# ── 坦克 ───────────────────────────────────────────────
TANK_WIDTH_M = 3.0            # 60 px
TANK_HEIGHT_M = 4.0           # 80 px
TANK_ROTATION_SPEED = 5.0     # rad/s（实测 4.66~5.17）
TANK_FORWARD_SPEED = 15.95    # m/s（319 px/s，实测物理体线速度恒定 15.95）★旧文档误写 7.5
TANK_BACK_SPEED = 12.8        # m/s（256 px/s）

# ── 迷宫 ───────────────────────────────────────────────
MAZE_TILE_SIZE_M = 10.0       # 200 px
MAZE_WALL_WIDTH_M = 0.8       # 16 px
MAZE_MINIMUM_TILES_PER_TANK = 5
MAZE_MINIMUM_TILES_BETWEEN_TANKS = 4
MAZE_MINIMUM_REACHABLE_RATIO = 1.0

# ── 弹道全局规则 ────────────────────────────────────────
PROJECTILE_BOUNCE_TIMEOUT_COUNT = 5   # 弹丸最多反弹次数（到时消亡）
PROJECTILE_BOUNCE_TIMEOUT_WINDOW = 35 # 反弹计数窗口（秒）

# 每武器参数见 cdp_tools/official_ballistics.py 的 WEAPONS 表
# （radius / muzzle_offset / speed / lifetime / ammo / charge_ms ...）

# ── 便捷派生值 ─────────────────────────────────────────
TILES_PER_SECOND_FORWARD = TANK_FORWARD_SPEED / MAZE_TILE_SIZE_M   # ≈1.595 格/秒
TILES_PER_SECOND_BACK = TANK_BACK_SPEED / MAZE_TILE_SIZE_M         # ≈1.28 格/秒


def ticks_to_travel(distance_m, dt=1.0):
    """以固定 dt 计算前进走完 distance_m 需要的步数（无加速段）。"""
    return int(distance_m / (TANK_FORWARD_SPEED * dt)) + 1


def ticks_to_turn(angle_rad, dt=1.0):
    """以固定 dt 计算转完 angle_rad 需要的步数。"""
    return int(abs(angle_rad) / (TANK_ROTATION_SPEED * dt)) + 1
