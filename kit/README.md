# ttkit —— TankTrouble 角色控制黑盒工具包（v4）

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
    pid = await kit.my_pid()          # 默认 arrows 角色；找不到抛 RuntimeError
    r = await kit.turn_to_angle(pid, 1.2)   # 世界方向角（弧度）
    print(r)   # {'ok','actual_deg'(停稳后实测),'err_deg','best_err_deg','final_err_deg'}
    await kit.close()

asyncio.run(main())
```

## 2. 约定与关键机制

* 世界坐标：**米**（1 格 = 10m）；"炮口朝向"用**世界方向角**（弧度，0=+X，逆时针）。
* `rotation(pid)` = **原始累积 rot**；`rotation_norm(pid)` = 归一化值。
* **自伤标签 `deadlyToOwner`**：弹丸第一次撞墙/撞盾后才可能伤发射者（之前穿透自己）。
* **`self_risk`**：官方射线**第一次反弹后**离自己车体中心的最小距离（米）。
  推演与实弹有偏差（实测曾"预测命中对手、实际偏 16.5m 打死自己"），
  `self_risk < 4m` 视为有自伤风险，**即使 `self_hit=False` 也别开火**。
* **弹丸上限是存活时间**（不是反弹次数）；射程=弹速×寿命（基础弹 180m、激光 144m、
  双管 108m、霰弹 64m、加特林 54m），`scan/raycast` 默认按当前武器取。
* **为什么预测不可靠**：官方 `calculateProjectilePath` 用的是
  `b2dworld.RayCast` **零宽射线**（不含 0.25m 弹丸半径），掠射时反弹点必然有偏差；
  且 `calculateFiringPath` 的起点是车体中心沿 -Y 偏 **2m**，而实弹出生点是
  **2.5m**（`BULLET.OFFSET`）。这些偏差会随反弹次数累积（实测 10 反弹/156m 偏 25m+）。
* **可靠性排序**：短距、少反弹（≤4）、`self_risk` 大、`robust=True` 的解最可靠；
  长多反弹路径（≥5 反弹或 ≥100m）默认要求 `self_risk ≥ 20m`。

## 3. API（全部 async）

### 信息
| 方法 | 说明 |
|---|---|
| `roles(refresh=False)` | 可控角色 `{pid: role}`（1s 缓存）；**只有这些 pid 能操作** |
| `my_pid(role='arrows')` | 自己的角色 pid；找不到抛 `RuntimeError` |
| `snapshot()` | `{round_state, controllable, tanks:{pid:{x,y,rot,alive,weapon}}}` |
| `round_state()` / `position(pid)` / `rotation(pid)` / `rotation_norm(pid)` / `alive(pid)` | 状态 |
| `opponents(my_pid)` / `nearest_opponent(my_pid)` | 对手 / 最近对手 `(pid,(x,y),dist)` |
| `weapon(pid)` / `bullet_range(pid)` / `range_of(w)` | 武器与射程 |
| `terrain()` / `open_tiles()` / `plan_path(a,b)` / `maze_distance(a,b)` | 地形/寻路 |
| `projectiles()` | 场上弹丸（含 `deadlyToOwner`） |
| `wait_target_gone(target_id, timeout=8)` | 开火后确认击杀（注意回合切换也会让它为 True） |
| `reset_maze_cache()` | 清空行为层墙缓存（**每回合迷宫重生成，跨回合要清**） |

### 操作（**所有控制方法都校验 pid 必须是你的可控角色**）
| 方法 | 说明 |
|---|---|
| `hold(pid, sec, fwd/back/left/right/fire)` | 按住输入 |
| `stop(pid)` | 停止并等稳 |
| `move_forward(pid, sec)` / `move_back(pid, sec)` | 前进（15.95 m/s）/后退（12.8 m/s） |
| `turn_to_angle(pid, ang, tol_deg=1.8)` | **自适应闭环**转向；`actual_deg/err_deg` 是**停稳后重读的最终值**；过冲回退，不振荡 |
| `navigate_to(pid,(x,y),arrival=1.5,timeout=None)` | 寻路（bool） |
| `navigate_to_ex(...)` | 寻路并返回 `{arrived,pos,dist,elapsed}` |
| `fire(pid, sec=0.6)` | 开一枪 |

### 弹道与火控
| 方法 | 说明 |
|---|---|
| `raycast(pid, ang)` | `{hit, hit_pid, path, bounces, traveled, self_hit, self_hit_harmless, self_risk, self_risk_ok, quality, reachable}`（炮口 2.5m 起算） |
| `scan(pid, step_deg=30, robust_deg=None)` | 360° 扫描，每项含 `hit_pid/self_hit/self_risk/self_risk_ok/robust/quality/path` |
| `scan_adaptive(pid, target_id=None, steps=(30,10,5,2))` | **粗→细并合并所有步长**的候选（默认不会因粗扫找到长解就停），返回 `(candidates, used_steps)` |
| `is_good_candidate(item, pid, target_id=None, max_bounces=6, max_traveled=None, min_self_risk=None)` | 默认安全策略：命中对手+不自伤+`bounces≤6`+长弹道 `self_risk≥20` |
| `find_aim(pid,(tx,ty),target_id=opp)` | 找命中角 |
| `fine_aim(pid, target_id, center_rad, span_deg=10, step_deg=1)` | **就近微调**：±span 内 1° 细搜，返回命中且安全的最佳角（转向分辨率 1~5° > 命中窗口 1~3° 时必备） |
| `aim_and_fire(pid,(tx,ty),target_id=opp,...)` | 转向 → 实际朝向复核 →（可选）**fine_aim 微调** → 通过才开火；返回 `{fired,reason,angle_deg,verify,attempt,fine}` |

`quality = traveled + 30×bounces + 5×max(0, 20-self_risk)`（越小越可靠）。
`scan` 很快（12 方向 10~20ms；2° 全扫 180 条 ~150ms）。

## 4. 任务：写 `agent_ai.py`

实现**循环行为**：

1. `roles()` / `my_pid()` 锁定自己的角色；**所有控制只对这个 pid**（统一 `_own()` 断言 + 按 pid 统计）。
2. **静默扫描**：`scan_adaptive(pid, target_id=opp, steps=(30,10,5,2))`；候选按 `quality` 升序，
   优先 `robust`/短距/少反弹/`self_risk` 大。
3. **找到** → **立即停止移动** → `turn_to_angle` → **重读实际朝向 `raycast` 复核**（含 `self_risk`）
   → 不通过就 `fine_aim` 微调再复核 → 通过才 `fire`；否则换解或机动。
4. **没找到** → `nearest_opponent` + `navigate_to`（分段 + timeout）逼近，**段间继续扫描**。
5. 循环到 `--seconds`（默认 90）。回合切换/角色不在场 → 等 1s；**回合切换调用 `reset_maze_cache()`**。
6. 每轮打印带 **pid** 的日志。

## 5. 交付与验收

* `python agent_ai.py --seconds 90`，报告关键日志与统计（扫描/开火/命中/寻路/错误数）。
* 证明**只控自己 pid**（按 pid 统计；`ttkit._require` 只校验"是不是可控角色"，不区分是谁的，护栏自己加）。
* 报告使用 ttkit 时发现的**任何问题/不便**，以及"预测能中但实际没中/自伤"的现场日志。

## 6. 已知限制（重要）

* 转向分辨率约 1~5°（物理帧量化）；命中窗口可能 1~3° → 必须复核 + `fine_aim`。
* 官方零宽射线 vs 真实 0.25m 弹丸：掠射/多反弹偏差大；**短距、少反弹最可靠**。
* 长多反弹路径（≥5 反弹 / ≥100m）即使 `self_risk` 较大也可能偏 10m+ 甚至自伤。
* 霰弹（weapon=2）的**散射未建模**（raycast 仍按单发直线）。
* `navigate_to` 较慢（有效 ~1.5~5 m/s），会打印 `[nav]`；务必分段 + 超时。
* 杀敌后回合可能仍显示 `in round` 数秒且只剩自己；`position()` 间隙短暂 `None`。
* 在线模式输入上报粒度 0.5s（hold 建议 ≥0.6s）；本地可更短。
