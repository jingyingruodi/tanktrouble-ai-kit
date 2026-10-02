"""
TankTrouble RL 上游契约（供 AI-B 模型训练侧使用）
=================================================
本包把"游戏接口"翻译成训练侧需要的东西，**不引入 numpy/torch 等新依赖**
（返回 dict / list[float]，上层要张量自己包）。

| 模块 | 内容 |
|------|------|
| `rl.actions` | 离散动作空间 + 5 位输入位掩码互转（动作原语 = `TankCtl.set_input`） |
| `rl.obs`     | 固定长度的相对观测向量（含分段布局与 `obs_size()`） |
| `rl.env`     | 固定 dt 的 step/reset + 奖励 + done/info（可注入后端便于离线测试） |

数据来源（上游契约，勿改语义）：
- 观测快照 = `monitor_server.STATE_JS` 的输出（米制、已 JSON 化）
- 动作下发 = `cdp_tools/tankctl.py` 的 `set_input`（`InputState.withState` →
  `gc.setInputState`，服务器权威路径，在线唯一有效）
- 地形 = `gameutils.get_maze_tiles()` 的 `tiles[y][x] = [present, topWall, leftWall]`
- 录制 = `cdp_tools/recorder.py`，动作标签写在记录的顶层字段 `"a"`
"""

from .actions import (  # noqa: F401
    ACTION_NAMES, NUM_ACTIONS, DISCRETE_ACTIONS,
    action_to_inputs, bits_to_inputs, inputs_to_bits, is_firing,
)
from .obs import OBS_VERSION, build_obs, obs_size, encode_grid  # noqa: F401
from .env import TankTroubleEnv, LiveBackend, FakeBackend, RewardConfig  # noqa: F401
from .dataset import iter_samples, dataset_stats, iter_records  # noqa: F401

__all__ = [
    'ACTION_NAMES', 'NUM_ACTIONS', 'DISCRETE_ACTIONS',
    'action_to_inputs', 'bits_to_inputs', 'inputs_to_bits', 'is_firing',
    'OBS_VERSION', 'build_obs', 'obs_size', 'encode_grid',
    'TankTroubleEnv', 'LiveBackend', 'FakeBackend', 'RewardConfig',
    'iter_samples', 'dataset_stats', 'iter_records',
]
