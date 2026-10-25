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
动作空间（AI-B 上游契约）
=========================
游戏的**唯一**控制原语是 5 个布尔输入：

    fwd, back, left, right, fire

经 `TankCtl.set_input` → `InputState.withState(pid, ...)` → `gc.setInputState`
下发（服务器权威路径；直接 setter 在线会被回弹）。

本模块提供两种等价表示：

1. **位掩码**（完整 32 种组合）——`bits_to_inputs(mask)` / `inputs_to_bits(d)`
   位序：`fwd=1, back=2, left=4, right=8, fire=16`（与下面 DISCRETE 表一致）。
2. **离散动作表**（默认 11 个常用动作）——`action_to_inputs(i)`，
   适合先用小动作集起步；需要完整空间时直接用位掩码。

⚠️ 语义提醒：
- `fwd` 与 `back` 同时为真、`left` 与 `right` 同时为真都是无意义的组合
  （游戏内互相抵消/抖动），离散表里已避免；用位掩码时请自行保证。
- `fire` 是"按住开火"，加特林(type5)需要按住 ≥0.5s 才出弹（见 02 文档）。
"""
from enum import IntEnum

# 位序（勿改：录制文件里的动作标签按此编码）
BIT_FWD, BIT_BACK, BIT_LEFT, BIT_RIGHT, BIT_FIRE = 1, 2, 4, 8, 16
ALL_BITS = 31


class DiscreteAction(IntEnum):
    """默认离散动作集（11 个）。索引即 `action_to_inputs` 的入参。"""
    NOOP = 0
    FORWARD = 1
    BACKWARD = 2
    TURN_LEFT = 3
    TURN_RIGHT = 4
    FORWARD_LEFT = 5
    FORWARD_RIGHT = 6
    FIRE = 7
    FORWARD_FIRE = 8
    LEFT_FIRE = 9
    RIGHT_FIRE = 10


# (名称, fwd, back, left, right, fire)
DISCRETE_ACTIONS = [
    ('NOOP',          False, False, False, False, False),
    ('FORWARD',       True,  False, False, False, False),
    ('BACKWARD',      False, True,  False, False, False),
    ('TURN_LEFT',     False, False, True,  False, False),
    ('TURN_RIGHT',    False, False, False, True,  False),
    ('FORWARD_LEFT',  True,  False, True,  False, False),
    ('FORWARD_RIGHT', True,  False, False, True,  False),
    ('FIRE',          False, False, False, False, True),
    ('FORWARD_FIRE',  True,  False, False, False, True),
    ('LEFT_FIRE',     False, False, True,  False, True),
    ('RIGHT_FIRE',    False, False, False, True,  True),
]

ACTION_NAMES = [a[0] for a in DISCRETE_ACTIONS]
NUM_ACTIONS = len(DISCRETE_ACTIONS)


def action_to_inputs(action):
    """离散动作索引 → `set_input` 关键字参数 dict。"""
    idx = int(action)
    if not 0 <= idx < NUM_ACTIONS:
        raise ValueError(f'action {idx} 越界（0..{NUM_ACTIONS - 1}）')
    name, fwd, back, left, right, fire = DISCRETE_ACTIONS[idx]
    return {'fwd': fwd, 'back': back, 'left': left, 'right': right, 'fire': fire,
            'name': name, 'index': idx}


def bits_to_inputs(mask):
    """5 位掩码 → `set_input` 关键字参数 dict（完整 32 组合）。"""
    mask = int(mask) & ALL_BITS
    return {
        'fwd': bool(mask & BIT_FWD),
        'back': bool(mask & BIT_BACK),
        'left': bool(mask & BIT_LEFT),
        'right': bool(mask & BIT_RIGHT),
        'fire': bool(mask & BIT_FIRE),
        'name': format(mask, '05b'),
        'index': mask,
    }


def inputs_to_bits(inputs):
    """`set_input` 关键字参数 dict（或任一含同名字段的 dict）→ 5 位掩码。"""
    mask = 0
    if inputs.get('fwd'):
        mask |= BIT_FWD
    if inputs.get('back'):
        mask |= BIT_BACK
    if inputs.get('left'):
        mask |= BIT_LEFT
    if inputs.get('right'):
        mask |= BIT_RIGHT
    if inputs.get('fire'):
        mask |= BIT_FIRE
    return mask


def is_firing(inputs):
    """该动作是否按下开火。"""
    return bool(inputs.get('fire'))
