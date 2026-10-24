# ttkit —— TankTrouble 角色控制黑盒工具包（v2）

本目录是一个**受限工作目录**：你只能读写本目录内的文件。目录里有已封装好的 Python 包
`ttkit/`，用于**读取游戏状态**与**操作角色**。游戏已经在运行（本地/在线均可），
CDP 端口写在 `config.json`（默认 62202）。

## 1. 快速开始

```python
import asyncio
from ttkit import Kit

async def main():
    kit = await Kit().connect()
    print(await kit.roles())          # {'22841260': 'arrows', '22841261': 'keyboard'}
    pid = await kit.my_pid()          # 默认 arrows 角色；找不到会抛 RuntimeError
    print(await kit.snapshot())
    r = await kit.turn_to_angle(pid, 1.2)   # 世界方向角（弧度）
    print(r)                          # {'ok': True, 'actual_deg': 68.8, 'err_deg': -0.7, ...}
    await kit.close()

asyncio.run(main())
```

## 2. 约定

* 世界坐标：**米**（1 格 = 10m）。"炮口朝向"用**世界方向角**（弧度，0=+X，逆时针）；
  游戏内部 `rot = 角度 + π/2`。
* `rotation(pid)` = **原始累积 rot**（可超 ±π）；`rotation_norm(pid)` = 归一化值。
* **自伤标签 `deadlyToOwner`**：弹丸第一次撞墙/撞盾后才变成"可伤发射者"，之前会穿透自己。
  因此 `scan()` 的 `self_hit` 只在 `bounces ≥ 1` 时才算；0 反弹命中自己记 `self_hit_harmless=True`。
* **弹丸上限是存活时间，不是反弹次数**：射程 = 弹速×寿命（基础弹 180m、激光 144m、双管 108m、
  霰弹 64m、加特林 54m）。`scan/raycast` 默认按**当前武器**取射程。
* **可靠性**：转向精度约 ±1°，而 1° 误差就可能让弹道拓扑突变；所以**扫描到的角度必须用
  实际朝向复核后再开火**（`aim_and_fire()` 已内置）。远距/多反弹路径与真实弹丸会累积偏差，
  **短距、少反弹最可靠**（看 `quality`，越小越好；`robust=True` 表示 ±1.5° 邻域仍命中）。

## 3. API（全部 async）

### 信息
| 方法 | 说明 |
|---|---|
| `roles()` | 可控角色 `{pid: role}`；**只有这些 pid 能操作** |
| `my_pid(role='arrows')` | 自己的角色 pid；找不到抛 `RuntimeError` |
| `snapshot()` | `{round_state, controllable, tanks:{pid:{x,y,rot,alive,weapon}}}` |
| `round_state()` | `in round` / `counting down` / `between rounds` |
| `position(pid)` / `rotation(pid)` / `rotation_norm(pid)` / `alive(pid)` | 位置/朝向/存活 |
| `opponents(my_pid)` / `nearest_opponent(my_pid)` | 对手列表 / `(pid,(x,y),dist)` |
| `weapon(pid)` / `bullet_range(pid)` / `range_of(weapon)` | 武器类型与射程 |
| `terrain()` / `open_tiles()` / `plan_path(a,b)` / `maze_distance(a,b)` | 地形与寻路 |
| `projectiles()` | 场上弹丸（含 `deadlyToOwner`） |

### 操作（**所有控制方法都会校验 pid 必须是你的可控角色**）
| 方法 | 说明 |
|---|---|
| `hold(pid, sec, fwd/back/left/right/fire)` | 按住输入 |
| `stop(pid)` | 停止并等稳 |
| `move_forward(pid, sec)` / `move_back(pid, sec)` | 前进（15.95 m/s）/后退（12.8 m/s） |
| `turn_to_angle(pid, ang, tol_deg=1.2)` | 闭环转向到世界角，返回 `{ok, actual_deg, err_deg}` |
| `navigate_to(pid, (x,y), arrival=1.5, timeout=None)` | 寻路移动（可超时取消） |
| `fire(pid, sec=0.6)` | 开一枪 |

### 弹道与火控
| 方法 | 说明 |
|---|---|
| `raycast(pid, ang)` | 官方弹道推演：`{hit, hit_pid, path, bounces, traveled, self_hit, self_hit_harmless, reachable}`（从炮口起算；命中自身且 bounces≥1 才算自伤） |
| `scan(pid, step_deg=30, robust_deg=None)` | 360° 静默扫描，每项含 `hit_pid/self_hit/bounces/traveled/robust/quality` |
| `find_aim(pid, (tx,ty), target_id=opp)` | 找命中角（世界弧度） |
| `aim_and_fire(pid, (tx,ty), target_id=opp, fire_sec=0.6)` | **转向→实际朝向复核（±鲁棒）→开火**；返回 `{fired, reason, angle_deg, verify, attempt}` |

`scan()` 快（12 方向约 10~20ms）；`quality = traveled + 30×bounces`，越小越可靠。

## 4. 任务：写 `agent_ai.py`

实现**循环行为**：

1. 用 `roles()` / `my_pid()` 锁定自己的角色；**所有控制只对这个 pid**，绝不能操作其他角色
   （建议做一个 `_own(pid)` 断言入口）。
2. **静默扫描**：`scan(pid, step_deg=30, robust_deg=1.5)`，找 `hit_pid` 属于对手、
   `self_hit == False` 的解；优先 `robust=True`、`quality` 小（短距/少反弹）的。
3. **找到** → **立即停止移动**（`stop`）→ `turn_to_angle` 转向该方向 →
   **用实际朝向复核**（`raycast(actual)` 或直接用 `aim_and_fire`）→ 通过才 `fire`；
   不通过就不要开枪，换解或机动。
4. **没找到** → `nearest_opponent` + `navigate_to` **分段**逼近（每段几秒，带 timeout），
   **段间继续扫描**；一发现可命中方向立刻停下转向/开火。
5. 循环到 `--seconds`（默认 60）。回合切换/角色不在场 → 等 1s 重试，不崩溃。
6. 每轮打印带 **pid** 的日志。

## 5. 交付与验收

* `python agent_ai.py --seconds 60`，报告关键日志与统计（扫描/开火/命中/寻路/错误数）。
* 证明**只控自己 pid**（按 pid 统计控制指令）。
* 报告使用 ttkit 时发现的**任何问题/不便**（API、判定、时序、文档）。

## 6. 已知限制（写代码时注意）

* 转向输入按物理帧量化，`tol_deg` 建议 1.0~2.0；太小会振荡。
* 官方推演与实际弹丸在**长多反弹路径**上有累积偏差；短距/少反弹才可靠。
* `navigate_to` 较慢（每个航点要停稳转向，有效 ~5m/s），务必分段 + 超时。
* 杀敌后回合可能仍显示 `in round` 数秒且只剩自己；`position()` 间隙会短暂 `None`。
* 在线模式输入上报粒度 0.5s（hold 建议 ≥0.6s）；本地可更短。
