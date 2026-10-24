# ttkit —— TankTrouble 角色控制黑盒工具包（v7）

本目录是**受限工作目录**：你只能读写本目录内的文件。`ttkit/` 用于**读取游戏状态**与
**操作角色**；游戏已在运行（本地/在线），CDP 端口见 `config.json`（默认 62202）。

## 1. 快速开始

```python
import asyncio
from ttkit import Kit

async def main():
    kit = await Kit().connect()
    pid = await kit.my_pid()               # 默认 arrows；找不到抛 RuntimeError
    print(await kit.ammo(pid))             # {'left':5,'fired':0,'remaining':5,'weapon':-1}
    print(await kit.kills_of(pid))         # {'kills':0,'victories':0}（比分归因，最可靠）
    print(await kit.round_id())            # 'rc-021b-6'
    r = await kit.turn_to_angle(pid, 1.2)  # 世界方向角（弧度）
    print(r)  # {'ok','actual_deg'(归一化),'err_deg','final_err_deg','outlier','precise'}
    await kit.close()

asyncio.run(main())
```

## 2. 关键机制与约定

* 世界坐标：**米**（1 格=10m）；炮口朝向 = **世界方向角**（弧度，0=+X 逆时针）；
  游戏内部 `rot = 角度 + π/2`。
* `rotation(pid)` = **原始累积 rot**；`rotation_norm(pid)` = 归一化值；
  `turn_to_angle.actual_deg` 已归一化到 [-180,180)。
* **自伤标签 `deadlyToOwner`**：弹丸第一次撞墙/盾后才可能伤发射者（之前穿透自己）。
* **`self_risk`**：官方射线第一次反弹后离自己车体中心的最小距离（米）；`<4m` 视为有自伤风险。
* **弹丸上限是存活时间**（不是反弹次数）；射程 = 弹速×寿命（基础弹 180m / 激光 144m /
  双管 108m / 霰弹 64m / 加特林 54m）。
* **弹药（v6 修正）**：`ammo()['left']` 是**弹匣容量**（恒 5，不递减！），
  `fired` 是已发射数，**真正剩余 = `remaining` = left - fired**；`remaining<=0` 时
  `fire()` **静默无效**（游戏不报错）。
* **预测为何会偏**：官方 `calculateProjectilePath` 是**零宽射线**（无 0.25m 弹丸半径），
  掠射反弹点有偏差；`calculateFiringPath` 起点 -Y 偏 2m、实弹出生 2.5m。
  实测单发锁定对比：首反弹点误差 0.3~0.6m；**打偏主因是瞄准量化 ±1° × 命中窗口 1~3°**。
* **转向是量化网格（v7 关键）**：5 rad/s ÷ 60fps = **4.775°/帧**，转角只能是
  「起始角 + k×4.775°」；**2~9° 的小修正不可达**（死区），大角度可达 0.1~1°。
  用 `snap_angle(pid, ang)` 把目标角吸附到可达角，再复核；
* **转向精度**：`turn_precise` mean≈1°，但**有离群值**（实测 −6.29°~+3.03°）；
  离群一次就足以让弹道拓扑突变（窗口 1° 时直接出窗）。所以**开火前必须用实际朝向复核**，
  复核失败先原地重转一次，再决定换解/微调。
* **回合**：本地局一击必杀后回合**立即重置**，`alive()/destroyed_pids()` 来不及观察 →
  用 `kills_of()`/`scores()` 的**比分增量**判断命中（实测 9→13→17 与命中数一致）。

## 3. API（全部 async）

### 信息
| 方法 | 说明 |
|---|---|
| `roles(refresh=False)` / `my_pid(role='arrows')` | 可控角色 / 自己的 pid |
| `snapshot()` / `round_state()` / `round_id()` | 局面 / 回合状态 / **回合 id（v6，比字符串可靠）** |
| `position(pid)` / `position_confirm(pid)` / `rotation(pid)` / `rotation_norm(pid)` | 状态（`rotation` 瞬时 None 会重试） |
| `alive(pid)` / `destroyed_pids()` | 存活 / 已摧毁列表 |
| `opponents(my_pid)` / `nearest_opponent(my_pid)` | 对手 / 最近对手 |
| `weapon(pid)`（可能 None）/ `bullet_range(pid)` / `range_of(w)` | 武器与射程 |
| `ammo(pid)` | **v7**：`{'left','fired','remaining','weapon','type'}`；`left` 是弹匣容量、`fired` 是本回合已发射数、**真正剩余 = `remaining`**；多武器时按当前武器类型匹配条目 |
| `scores()` / `kills_of(pid)` | **v6：比分/击杀（开火后归因最可靠）** |
| `bullets_in_flight(pid=None)` | 场上弹丸（可按发射者过滤） |
| `terrain()` / `open_tiles()` / `plan_path(a,b)` / `maze_distance(a,b)` | 地形/寻路 |
| `wait_target_gone(target_id, timeout=8)` | 等待目标离场（回合切换也会 True，需搭配比分判断） |
| `reset_maze_cache()` | 清空行为层墙缓存（每回合迷宫重生成，跨回合要清） |

### 操作（**所有控制方法都校验 pid 必须是你的可控角色**）
| 方法 | 说明 |
|---|---|
| `hold(pid, sec, fwd/back/left/right/fire)` | 按住输入；**`hold(pid,0.0)` = 一次全松**（最快） |
| `stop(pid)` / `move_forward` / `move_back` | 停止等稳 / 前进 15.95 / 后退 12.8 m/s |
| `snap_angle(pid, ang)` | **v7**：把世界角吸附到「从当前朝向可达」的网格角（返回 `offset_deg`） |
| `turn_precise(pid, ang, tol_deg=0.8)` | 连续按住+8ms 读 rot+提前释放+多轮补偿；返回 `outlier` |
| `turn_to_angle(pid, ang, tol_deg=1.2)` | 优先 precise，失败退回脉冲法；返回停稳后归一化 `actual_deg` |
| `navigate_to(...)` / `navigate_to_ex(...)` | 寻路 / 带 `{arrived,pos,from,dist,moved,elapsed}` |
| `navigate_segment(pid,(x,y),max_seconds=3.0)` | 有界单段（段间可扫描；首段可能位移 0，属正常） |
| `fire(pid, sec=0.6)` | 开一枪（先看 `ammo().remaining`） |

### 弹道与火控
| 方法 | 说明 |
|---|---|
| `raycast(pid, ang)` | `{hit,hit_pid,path,bounces,traveled,self_hit,self_hit_harmless,self_risk,self_risk_ok,quality,reachable}` |
| `scan(pid, step_deg=30, include_targets=True)` | 360° 扫描；**v6 额外补"正对目标"直线角**（固定网格会漏 1~3° 窗口） |
| `scan_adaptive(pid, target_id, steps=(30,10,5,2), profiles=(...))` | 粗→细 + 多档（strict→relaxed→loose），候选带 `tier`，首档有解即返回 |
| `is_good_candidate(item, pid, target_id, profile='strict')` | 策略判定；`Kit.PROFILES` 可查 |
| `find_aim(pid,(tx,ty),target_id=opp)` | 找命中角 |
| `scan_region(pid, center_deg, span_deg=20, step_deg=0.5, target_id=None)` | **v7**：指定区域 0.5° 细扫（补 2° 网格漏掉的 0.5~1° 窄窗口） |
| `fine_aim(pid, target_id, center_rad, span_deg=10, step_deg=0.25, profile=None, ref_bounces=?, ref_traveled=?, same_family=True, snap=True)` | **v7**：profile 自动多档；返回窗口中心并**吸附到可达角**（`snapped_deg/snap_offset_deg`）；`same_family` 防止换成更长解 |
| `aim_and_fire(pid,(tx,ty),target_id=opp,...)` | 转向 → 实际朝向复核 →（失败）fine_aim 微调 → 通过才开火 |

`quality = traveled + 30×bounces + 5×max(0, 20-self_risk)`（越小越可靠）。

## 4. 任务：写 `agent_ai.py`

1. `roles()` / `my_pid()` 锁定自己的角色；**所有控制只对这个 pid**（统一 `_own()` 断言 + 按 pid 统计）。
2. 每回合 `reset_maze_cache()`；开火前查 `ammo().remaining`；用 `round_id()` 判断回合切换。
3. **静默扫描**：`scan_adaptive(pid, target_id=opp)`（自动多档），候选按 `quality` 升序。
4. **找到** → 立即 `stop` → `turn_to_angle` → **重读实际朝向 `raycast` 复核**
   → 失败先原地**重转一次**；仍失败用 `fine_aim`（传候选 b/trav 限制同族）微调再复核
   → 通过才 `fire`；否则换解/机动。
5. **没找到** → `nearest_opponent` + `navigate_segment` 分段逼近，段间继续扫描。
6. 开火后用 **比分增量**（`kills_of`）判断命中；同位置同方向打偏过就别重复打。
7. 循环到 `--seconds`（默认 90）。回合切换/不在场 → 等 1s；每轮日志带 **pid**。

## 5. 交付与验收

* `python agent_ai.py --seconds 90`，贴关键日志与统计（扫描/开火/命中/寻路/错误数）。
* 证明**只控自己 pid**（按 pid 统计；`ttkit._require` 只校验"是不是可控角色"，护栏自己加）。
* 报告使用 ttkit 时发现的**任何问题/不便**，以及"预测能中但实际没中/误伤自己"的现场日志。

## 6. 已知限制

* 转向离群值 >3° 时命中率会掉；复核失败**先重转**再考虑换解。
* **小角度修正（2~9°）不可达**：先 `snap_angle` 吸附到网格角，或在 `fine_aim` 里用 `snap=True`（默认）。
* 自伤阈值按几何放大（v7）：`need = max(profile, 4 + 0.6×bounces + 0.03×traveled)`——
  实测 3 反弹/97m/risk=5.2 仍被自己跳弹打死，所以别再只看 4m。
* `navigate_segment` 默认 2.2s（0.9~1.5s 分段几乎全耗在转向，位移≈0）。
* 零宽射线 vs 0.25m 弹丸：掠射/多反弹有偏差；短距、少反弹最可靠。
* 霰弹（weapon=2）散射**未建模**（raycast 按单发直线）。
* 弹丸"未中"只能等飞行时间+余量后判断；迟到的跳弹可能回头打到自己（`self_risk` 已缓解）。
* 死亡判定 0.2~0.4s 滞后；`position()/rotation()` 可能瞬时 None（已重试）。
* `navigate_to` 慢（~1.5~7 m/s）且打印 `[nav]`；用 `navigate_segment` 分段。
* 在线输入上报粒度 0.5s（hold ≥0.6s）；本地可更短。
