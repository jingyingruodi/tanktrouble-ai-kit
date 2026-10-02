# AI-B 上游契约（RL 接口）

> 面向：负责模型训练侧的 AI-B。
> 上游（我，AI-A）保证"游戏 → 状态/动作"的接口稳定；本页是唯一契约入口。
> 代码：`rl/`（Python 包，**零新依赖**，只用标准库）。

---

## 1. 三句话上手

```python
from rl import TankTroubleEnv          # 需要项目根目录在 sys.path
env = TankTroubleEnv(port='auto')      # 自动发现 ego 浏览器 CDP 端口并开一局本地 2 人局
obs = await env.reset()                # {'vector': [...318 个 float...], 'size': 318, ...}
out = await env.step(1)                # 动作索引 → {'obs','reward','done','info'}
```

* 观测 = **定长 318**（可配）相对向量，已归一化到 [-1,1]
* 动作 = **11 个离散动作**（或完整 32 种位掩码）
* 一步 = 固定 `dt`（默认 0.05s）的输入按住时间
* 奖励 = 击杀 +10 / 死亡 -5 / 存活 +0.01 每步 / 捡武器 +1（可配）

离线自检（不需要浏览器）：

```powershell
python scripts\rl_selftest.py                     # 动作/观测/录制/数据集/step 语义全查
python scripts\rl_demo.py --fake --steps 40 --record $env:TEMP\demo.jsonl
                                                  # 端到端演示：观测→动作→奖励→录制→数据集
python scripts\rl_selftest.py --live auto         # 额外跑一次真实 step（会操作游戏）
```

---

## 2. 观测契约（`rl.obs`，`OBS_VERSION = 1`）

`build_obs(state, pid, terrain=None, grid_radius=3, ...) -> {'vector','size','layout','meta'}`

| 段 | 长度 | 含义 |
|----|------|------|
| `self` | 10 | x/W, y/H, cos(rot), sin(rot), vx/18, vy/18, weapon/5, shield, shieldLife/10, alive |
| `round` | 5 | roundActive, betweenRounds, countingDown, storm, 场上坦克数/max_tanks |
| `score` | 4 | 我方击杀/10, 我方胜利/10, 对手最高击杀/10, 对手最高胜利/10 |
| `others` | 4×7 | 相对位置(dx/W,dy/H,dist/D)、**相对炮口方位**(cos,sin)、武器/5、存活 |
| `projectiles` | 12×7 | 相对位置、速度/18、剩余寿命比、deadlyToOwner、是否我方 |
| `collectibles` | 6×4 | 相对位置、距离/D、类型/6 |
| `mines` | 4×4 | 相对位置、已激活、是否我方 |
| `grid` | 49×3 | 以我方为中心 7×7 格的 present/topWall/leftWall（传 `terrain` 时） |

* 默认总长 **318**；`obs_size(grid_radius=None) == 318-147 == 171`。
* W/H = 世界宽高（米，格数×10）；D = 世界对角线；`rot` 是**未归一化累积角**的 sin/cos。
* 空状态（页面未就绪）→ 长度不变、实体段全零（**不伪造**"被墙包围"）。
* 上游数据源：`monitor_server.STATE_JS` 快照 + `gameutils.get_terrain()`。
  速度 `vx/vy` 由 `LiveBackend` 额外读 Box2D 线速度补进快照（STATE_JS 本身没有）。
* 改动任何分段必须升 `OBS_VERSION`；`layout` 给出每段 `[起, 止)`。

---

## 3. 动作契约（`rl.actions`）

控制原语（唯一在线有效路径）：

```
TankCtl.set_input(pid, fwd, back, left, right, fire, hold, freq)
  → InputState.withState(pid,...) → gc.setInputState   # 服务器权威，与键盘同路径
```

离散动作（`NUM_ACTIONS = 11`，`action_to_inputs(i)`）：

| idx | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|-----|---|---|---|---|---|---|---|---|---|---|----|
| 名称 | NOOP | FORWARD | BACKWARD | TURN_LEFT | TURN_RIGHT | FORWARD_LEFT | FORWARD_RIGHT | FIRE | FORWARD_FIRE | LEFT_FIRE | RIGHT_FIRE |

* 离散表里**不会**同时按 fwd+back 或 left+right（游戏内互相抵消）。
* 需要完整空间用位掩码：`bits_to_inputs(mask)` / `inputs_to_bits(d)`，
  位序 `fwd=1, back=2, left=4, right=8, fire=16`（32 种）。
* 加特林（weapon=5）按住 ≥0.5s 才出弹——固定 dt 太小时连续多步 FIRE 才有效。

---

## 4. 录制契约（`cdp_tools/recorder.py`）

每行一帧 JSON：`{"t": 相对秒, "wall_t": 游戏时间, "mv": 迷宫版本, "s": {状态}, "a": {动作}}`

* `a` 是**新增的可选字段**（顶层，不在 `s` 里）：`{pid: {fwd,back,left,right,fire}}`。
  老读取方忽略它；有它才能直接做模仿学习/离线 RL。
* `CompressedRecorder` 同样支持 `record(state, action=...)`；`iter_expanded()` 向后兼容。
* 静态字段（walls/maze/zones）只在 `mv` 变化时写一次（回合切换）。

```python
rec = CompressedRecorder('data/rollout.jsonl')
rec.record(state, action={'p1': {'fwd': True, 'fire': False, ...}})
```

---

## 5. 奖励与终止（`rl.env`）

| 事件 | 奖励 | 说明 |
|------|------|------|
| 击杀 / 胜局 | `+10` | `scoresKill` 或 `scoresVict` 增加 |
| 我方死亡 | `-5` | 我方 pid 从 `getTanks()` 消失 |
| 存活（非终止步） | `+0.01` | 回合进行中且存活 |
| 捡到武器 | `+1` | `weapon` 类型变化（近似 crate 拾取） |
| 时间惩罚 | `0` | `RewardConfig.time_penalty` 可调 |

* `done`：击杀 / 死亡 / 回合结束 / 达到 `max_steps`（`info['truncated']`）。
* `info['events']` 标注本步事件（`kill/death/round_end/weapon`）。
* `auto_reset=True` 时 done 后自动 `reset()`。

### 后端可注入（离线可测）

```python
TankTroubleEnv(backend=FakeBackend(states, pid='p1'))   # 不需要浏览器
TankTroubleEnv(port='auto')                             # LiveBackend：CDP + TankBehavior
```

---

## 6. reset 的边界（重要）

* **本地局**：`reset()` 会调用 `scripts/start_local_game.py` 的开局逻辑
  （Menu → Lobby → 对局 + `Inputs._assignInput` 绑定角色），可以反复 reset。
* **在线多人**：服务器权威，reset **不会**开新局，只能等待当前对局；
  此时应把 `LiveBackend(auto_start=False)` 并自行等到 `roundActive`。
* 多智能体（PettingZoo）尚未封装：当前 env 是"单智能体视角 + 其余实体当环境"。
  本地局可同时对多个 pid 下发输入（每个 pid 一个 env 实例），但**回合推进是共享的**，
  多智能体接口（共享 step、并行 reset）留待下一步。

---

## 7. 已验证 / 未验证

| 项 | 状态 |
|----|------|
| 动作表 ↔ 位掩码互转、越界报错 | ✅ `scripts/rl_selftest.py` |
| 观测定长/有界/分段连续/空状态容错 | ✅ 用真实录制帧（`fire_strict_v3.jsonl`）验证 |
| 录制动作标签（普通+压缩）读写一致 | ✅ |
| env step/reset/奖励/done（离线 FakeBackend） | ✅ 11 项断言 |
| 数据集迭代器 `rl.dataset`（标签配对、动作归类、多智能体） | ✅ 8 项断言 |
| 端到端演示 `scripts/rl_demo.py`（env→录制→数据集，离线） | ✅ 40 步：40 帧全带标签、40 样本可读 |
| env 真实游戏 step（LiveBackend） | ✅ 2026-10-02 实机：`obs.size=318`、`step(FORWARD)` → `reward=+0.010 done=False pos=(33.6,25.3)` |
| 动作标签的**真实**产出 | ✅ 实机跑 `loop_suicide.py 9222 --record … --fps 5`：**190/191 帧带 `"a"`**，`rl.dataset` 读出 332 个样本（动作分布 NOOP/FORWARD/TURN/FIRE 合理） |
| 多智能体 / PettingZoo 封装 | ⏳ 未做（当前是"单智能体视角 + 其余实体当环境"） |

---

## 8. 怎么产出带动作标签的数据

录制器已接线到会控制坦克的脚本，跑一次就有 `"a"` 字段：

```powershell
# 持续自杀 + 录制（多角色，压缩）
python scripts\loop_suicide.py 51109 --record data\rollout.jsonl --compress

# 火控示范（脚本策略的 state+action 对，适合模仿学习冷启动）
python scripts\fire_record.py 51109 --out data\fire_demo.jsonl
python scripts\fire_verify_strict.py 51109 --out scripts\fire_strict_v5.jsonl
```

读取（自动跳过旧的无标签帧）：

```python
from rl.dataset import iter_samples, dataset_stats
dataset_stats('data/rollout.jsonl')            # {'frames':..., 'labeled_frames':..., 'per_action':{...}}
for s in iter_samples('data/rollout.jsonl', pid='22768269'):
    s['obs'], s['action_index'], s['bits']    # 观测 / 离散动作(或 None) / 原始位掩码
```

* `pid=None` 时，同一帧里每个有标签的 pid 各产出一条样本（多智能体视角）。
* `obs` 默认**不含栅格**（录制里只有墙段、没有 `tiles`，无法还原位图）；
  需要栅格请用 `rl.env` 实时采集。
* 底层：`TankCtl.current_actions()` 暴露"最近一次下发的输入"，
  录制时写进记录顶层 `"a"`。
