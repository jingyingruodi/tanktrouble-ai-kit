# ttkit —— TankTrouble 角色控制黑盒工具包（v5）

本目录是**受限工作目录**：你只能读写本目录内的文件。`ttkit/` 用于**读取游戏状态**与
**操作角色**；游戏已在运行（本地/在线），CDP 端口见 `config.json`（默认 62202）。

## 1. 快速开始

```python
import asyncio
from ttkit import Kit

async def main():
    kit = await Kit().connect()
    print(await kit.roles())            # {'22841415': 'arrows', '22841416': 'keyboard'}
    pid = await kit.my_pid()            # 默认 arrows；找不到抛 RuntimeError
    print(await kit.ammo(pid))          # {'left': 5, 'fired': 0, 'weapon': -1}
    r = await kit.turn_to_angle(pid, 1.2)   # 世界方向角（弧度）
    print(r)  # {'ok','actual_deg'(归一化),'err_deg','best_err_deg','final_err_deg','precise'}
    await kit.close()

asyncio.run(main())
```

## 2. 关键机制与约定

* 世界坐标：**米**（1 格=10m）；炮口朝向 = **世界方向角**（弧度，0=+X 逆时针）；
  游戏内部 `rot = 角度 + π/2`。
* `rotation(pid)` = **原始累积 rot**（可几百上千度）；`rotation_norm(pid)` = 归一化值。
  **`raycast/turn_to_angle` 用世界方向角**，`turn_to_angle.actual_deg` 已归一化到 [-180,180)。
* **自伤标签 `deadlyToOwner`**：弹丸第一次撞墙/盾后才可能伤发射者（之前穿透自己）。
* **`self_risk`**：官方射线第一次反弹后离自己车体中心的最小距离（米）；`<4m` 视为有自伤风险。
* **弹丸上限是存活时间**（不是反弹次数）；射程 = 弹速×寿命（基础弹 180m / 激光 144m /
  双管 108m / 霰弹 64m / 加特林 54m）。
* **隐藏弹药**：默认武器**每回合 5 发**；弹药耗尽后 `fire()` **静默无效**（不报错），
  必须先用 `ammo(pid)` 检查 `left`（见 API）。
* **预测为何会偏**：官方 `calculateProjectilePath` 是 **零宽射线**（无 0.25m 弹丸半径），
  掠射反弹点必然有偏差；`calculateFiringPath` 起点是 -Y 偏 **2m**，实弹出生 **2.5m**。
  但实测**单发锁定对比**：首反弹点误差仅 0.3~0.6m、多反弹路径吻合良好——
  **本机打偏的主因是瞄准量化 ±1° × 命中窗口 1~3°**，所以先练准，再谈策略。
* **可靠性**：短距、少反弹、`self_risk` 大最可靠；长多反弹路径（≥5 反弹/≥100m）需要
  更宽的自伤余量（默认策略已内置）。

## 3. API（全部 async）

### 信息
| 方法 | 说明 |
|---|---|
| `roles(refresh=False)` / `my_pid(role='arrows')` | 可控角色（1s 缓存）/ 自己的 pid |
| `snapshot()` | `{round_state, controllable, tanks:{pid:{x,y,rot,alive,weapon}}}` |
| `round_state()` / `position(pid)` / `rotation(pid)` / `rotation_norm(pid)` | 状态 |
| `position_confirm(pid, retries=3)` | 位置（短暂 None 重试） |
| `alive(pid)` | **v5**：先查 destroyed，再查 position，短暂 None 重试 |
| `destroyed_pids()` | 本回合已摧毁 pid 列表（重置后可能空） |
| `opponents(my_pid)` / `nearest_opponent(my_pid)` | 对手 / 最近对手 |
| `weapon(pid)`（可能 None）/ `bullet_range(pid)` / `range_of(w)` | 武器与射程 |
| `ammo(pid)` | **v5**：`{'left','fired','weapon'}`；`left<=0` 时别再开火 |
| `bullets_in_flight(pid=None)` | **v5**：场上弹丸（可按发射者过滤，用于归因） |
| `terrain()` / `open_tiles()` / `plan_path(a,b)` / `maze_distance(a,b)` | 地形/寻路 |
| `wait_target_gone(target_id, timeout=8)` | 开火后确认击杀（回合切换也会 True） |
| `reset_maze_cache()` | 清空行为层墙缓存（**每回合迷宫重生成，跨回合要清**） |

### 操作（**所有控制方法都校验 pid 必须是你的可控角色**）
| 方法 | 说明 |
|---|---|
| `hold(pid, sec, fwd/back/left/right/fire)` | 按住输入；**`hold(pid,0.0)` = 一次全松**（比 stop 快） |
| `stop(pid)` | 停止并等稳 |
| `move_forward(pid, sec)` / `move_back(pid, sec)` | 前进 15.95 m/s / 后退 12.8 m/s |
| `turn_precise(pid, ang, tol_deg=0.8)` | **v5 连续按住 + 8ms 读 rot + 提前释放 + 补偿**（实测残差 mean≈1°） |
| `turn_to_angle(pid, ang, tol_deg=1.2)` | 优先 `turn_precise`，失败退回脉冲自适应；返回停稳后归一化 `actual_deg` |
| `navigate_to(...)` / `navigate_to_ex(...)` | 寻路 / 带 `{arrived,pos,dist,elapsed}` |
| `navigate_segment(pid,(x,y),max_seconds=3.0)` | **v5**：有界单段（段间可扫描） |
| `fire(pid, sec=0.6)` | 开一枪（先查 `ammo`） |

### 弹道与火控
| 方法 | 说明 |
|---|---|
| `raycast(pid, ang)` | `{hit,hit_pid,path,bounces,traveled,self_hit,self_hit_harmless,self_risk,self_risk_ok,quality,reachable}` |
| `scan(pid, step_deg=30, robust_deg=None)` | 360° 扫描 |
| `scan_adaptive(pid, target_id, steps=(30,10,5,2), profiles=('strict','relaxed','loose'))` | **v5 多档**：先 strict，全灭再 relaxed/loose；候选带 `tier` |
| `is_good_candidate(item, pid, target_id, profile='strict')` | 策略判定（strict/relaxed/loose，见 `Kit.PROFILES`） |
| `find_aim(pid,(tx,ty),target_id=opp)` | 找命中角 |
| `fine_aim(pid, target_id, center_rad, span_deg=10, step_deg=0.25)` | **v5 窗口中心**：0.25° 采样建命中窗口，返回窗口中心角（带 `window` 宽度） |
| `aim_and_fire(pid,(tx,ty),target_id=opp,...)` | 转向 → 实际朝向复核 →（失败）`fine_aim` 微调 → 通过才开火 |

`quality = traveled + 30×bounces + 5×max(0, 20-self_risk)`（越小越可靠）。

## 4. 任务：写 `agent_ai.py`

1. `roles()` / `my_pid()` 锁定自己的角色；**所有控制只对这个 pid**（统一 `_own()` 断言 + 按 pid 统计）。
2. 每回合开始/结束时 `reset_maze_cache()`；开火前查 `ammo(pid)`。
3. **静默扫描**：`scan_adaptive(pid, target_id=opp)`（自动多档：strict→relaxed→loose），
   候选按 `quality` 升序、优先短距/少反弹/`self_risk` 大。
4. **找到** → **立即停止移动** → `turn_to_angle` → **重读实际朝向 `raycast` 复核**（含 `self_risk`）
   → 不通过就 `fine_aim` 微调再复核 → 通过才 `fire`；否则换解/机动。
5. **没找到** → `nearest_opponent` + `navigate_segment` 分段逼近（段间继续扫描）。
6. 循环到 `--seconds`（默认 90）。回合切换/角色不在场 → 等 1s 重试；每轮日志带 **pid**。

## 5. 交付与验收

* `python agent_ai.py --seconds 90`，贴关键日志与统计（扫描/开火/命中/寻路/错误数）。
* 证明**只控自己 pid**（按 pid 统计；`ttkit._require` 只校验"是不是可控角色"，护栏自己加）。
* 报告使用 ttkit 时发现的**任何问题/不便**，以及"预测能中但实际没中/误伤自己"的现场日志。

## 6. 已知限制

* 转向分辨率约 1~2°（`turn_precise` 可达 ~1°）；命中窗口可能 1~3° → 必须复核 + `fine_aim`。
* 零宽射线 vs 0.25m 弹丸：掠射/多反弹有偏差；**短距、少反弹最可靠**。
* 霰弹（weapon=2）的**散射未建模**（raycast 按单发直线）。
* 弹丸"未中"只能等飞行时间+余量后判断；迟到的跳弹可能回来打到自己（`self_risk` 已缓解）。
* 死亡判定有 0.2~0.4s 滞后（`alive()` 已缓解）；`position()` 短暂 None。
* `navigate_to` 较慢（~1.5~7 m/s）且会打印 `[nav]`；用 `navigate_segment` 分段。
* 在线输入上报粒度 0.5s（hold ≥0.6s）；本地可更短。
