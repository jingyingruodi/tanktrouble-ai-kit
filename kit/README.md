# ttkit —— TankTrouble 角色控制黑盒工具包（v3）

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
    print(r)                          # {'ok': True, 'actual_deg': ..., 'err_deg': ..., 'best_err_deg': ...}
    await kit.close()

asyncio.run(main())
```

## 2. 约定与关键机制

* 世界坐标：**米**（1 格 = 10m）；"炮口朝向"用**世界方向角**（弧度，0=+X，逆时针）。
* `rotation(pid)` = **原始累积 rot**；`rotation_norm(pid)` = 归一化值。
* **自伤标签 `deadlyToOwner`**：弹丸第一次撞墙/撞盾后才可能伤发射者（之前穿透自己）。
* **`self_risk`（v3 新增）**：官方射线**第一次反弹后**离自己车体中心的最小距离（米）。
  推演与实弹有偏差（实测曾"预测命中对手、实际偏 16.5m 打死自己"），
  所以 **`self_risk < 4m` 视为有自伤风险**，即使 `self_hit=False` 也别开火。
* **弹丸上限是存活时间**（不是反弹次数）；射程=弹速×寿命（基础弹 180m、激光 144m、
  双管 108m、霰弹 64m、加特林 54m），`scan/raycast` 默认按当前武器取。
* **可靠性**：转向精度约 ±1~2°，1° 差就可能让弹道拓扑突变 → 开火前必须用**实际朝向**复核；
  长多反弹路径与实弹会累积偏差（10 反弹/156m 曾偏 25m+），**短距/少反弹/self_risk 大**最可靠。

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
| `wait_target_gone(target_id, timeout=8)` | 开火后确认击杀 |
| `reset_maze_cache()` | 清空行为层墙缓存（**每回合迷宫里重生成，跨回合要清**） |

### 操作（**所有控制方法都校验 pid 必须是你的可控角色**）
| 方法 | 说明 |
|---|---|
| `hold(pid, sec, fwd/back/left/right/fire)` | 按住输入 |
| `stop(pid)` | 停止并等稳 |
| `move_forward(pid, sec)` / `move_back(pid, sec)` | 前进（15.95 m/s）/后退（12.8 m/s） |
| `turn_to_angle(pid, ang, tol_deg=1.8)` | **自适应闭环**转向；返回 `{ok, actual_deg, err_deg, best_err_deg}`（过冲会回退接受最佳角，不再极限环振荡） |
| `navigate_to(pid,(x,y),arrival=1.5,timeout=None)` | 寻路（bool） |
| `navigate_to_ex(...)` | 寻路并返回 `{arrived,pos,dist,elapsed}` |
| `fire(pid, sec=0.6)` | 开一枪 |

### 弹道与火控
| 方法 | 说明 |
|---|---|
| `raycast(pid, ang)` | `{hit, hit_pid, path, bounces, traveled, self_hit, self_hit_harmless, self_risk, self_risk_ok, reachable}`（炮口起算） |
| `scan(pid, step_deg=30, robust_deg=None)` | 360° 扫描，每项含 `hit_pid/self_hit/self_risk/self_risk_ok/robust/quality/path` |
| `scan_adaptive(pid, target_id=None, steps=(30,10,5,2))` | **粗→细扫描**，一找到安全候选就返回 `(candidates, used_steps)`；候选按 `quality` 升序 |
| `is_good_candidate(item, pid, target_id=None)` | 判定"命中对手 + 不自伤 + self_risk 安全" |
| `find_aim(pid,(tx,ty),target_id=opp)` | 找命中角 |
| `aim_and_fire(pid,(tx,ty),target_id=opp,fire_sec=0.6,robust_deg=None,self_risk_ok=True)` | 转向→实际朝向复核（+自伤风险检查）→通过才开火；返回 `{fired,reason,angle_deg,verify,attempt}` |

`quality = traveled + 30×bounces`（越小越可靠）。`scan` 很快（12 方向 10~20ms；2° 全扫 180 条约 150ms）。

## 4. 任务：写 `agent_ai.py`

实现**循环行为**：

1. 用 `roles()` / `my_pid()` 锁定自己的角色；**所有控制只对这个 pid**（建议统一 `_own()` 断言 + 按 pid 统计指令）。
2. **静默扫描**：`scan_adaptive(pid, target_id=opp, steps=(30,10,5,2))`（命中窗口可能只有 1~3°，需要细扫）；
   候选按 `quality` 升序，优先 `robust=True`（若有）、`self_risk` 大、短距/少反弹。
3. **找到** → **立即停止移动** → `turn_to_angle` 转向 → **用实际朝向 `raycast` 复核**
   （含 `self_risk`）→ 通过才 `fire`；不通过不开枪，换解或机动。
4. **没找到** → `nearest_opponent` + `navigate_to`（分段 + timeout）逼近，**段间继续扫描**。
5. 循环到 `--seconds`（默认 60~90）。回合切换/角色不在场 → 等 1s 重试；**回合切换时调用 `reset_maze_cache()`**。
6. 每轮打印带 **pid** 的日志。

## 5. 交付与验收

* `python agent_ai.py --seconds 90`，报告关键日志与统计（扫描/开火/命中/寻路/错误数）。
* 证明**只控自己 pid**（按 pid 统计控制指令；注意 `ttkit._require` 只校验"是不是可控角色"，不区分是谁的，护栏要自己加）。
* 报告使用 ttkit 时发现的**任何问题/不便**（API、判定、时序、文档），以及"预测能中但实际没中/自伤"的现场日志。

## 6. 已知限制

* 转向按物理帧量化（单脉冲约 4.8°）；`turn_to_angle` 已自适应，但太小 tol 仍可能来回。
* 官方推演与实际弹丸在长多反弹路径上有累积偏差；短距/少反弹 + `self_risk` 大才可靠。
* `navigate_to` 较慢（每航点停稳转向，有效 ~5m/s），务必分段 + 超时。
* 杀敌后回合可能仍显示 `in round` 数秒且只剩自己；`position()` 间隙短暂 `None`。
* 在线模式输入上报粒度 0.5s（hold 建议 ≥0.6s）；本地可更短。
