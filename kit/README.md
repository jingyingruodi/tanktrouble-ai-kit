# ttkit —— TankTrouble 角色控制黑盒工具包

本目录是一个**受限工作目录**：你只能读写本目录内的文件。目录里有一个已封装好的 Python 包
`ttkit/`，用于**读取游戏状态**与**操作角色**。请用它在**正在运行的游戏**里完成基础行为 AI。

## 1. 运行环境

* 游戏已经打开并在一局对战中（本地/在线均可），CDP 端口写在 `config.json`（默认 62202）。
* Python 3.10+，依赖 `websockets`（已装）。
* 连接失败时：确认游戏页面开着、端口正确；本目录外的东西不要碰。

## 2. 快速开始

```python
import asyncio
from ttkit import Kit

async def main():
    kit = await Kit().connect()      # 自动读 config.json
    print(await kit.roles())          # {'22841260': 'arrows', '22841261': 'keyboard'}
    pid = await kit.my_pid()          # 选择"自己的角色"（默认 arrows）
    print(await kit.snapshot())       # 局面快照
    await kit.close()

asyncio.run(main())
```

## 3. API 一览（全部 async）

### 信息
| 方法 | 说明 |
|---|---|
| `await kit.roles()` | 可控角色 `{pid: role}`；**只有这些 pid 能操作** |
| `await kit.my_pid(role='arrows')` | 选出自己的角色 pid |
| `await kit.snapshot()` | `{round, controllable, tanks:{pid:{x,y,rot,alive,weapon}}}` |
| `await kit.round_state()` | `in round` / `counting down` / `between rounds` |
| `await kit.position(pid)` | 世界坐标 `(x,y)`（米），不在场为 None |
| `await kit.rotation(pid)` | 游戏内部累积 rot（弧度） |
| `await kit.alive(pid)` | 是否在场 |
| `await kit.opponents(my_pid)` | 其他坦克 pid 列表 |
| `await kit.nearest_opponent(my_pid)` | `(pid, (x,y), distance)` 或 None |

### 操作（只允许操作自己的 pid）
| 方法 | 说明 |
|---|---|
| `await kit.hold(pid, 0.4, fwd=True)` | 按住输入（fwd/back/left/right/fire 任意组合） |
| `await kit.stop(pid)` | 停止 |
| `await kit.move_forward(pid, 0.4)` | 前进（实测 15.95 m/s） |
| `await kit.turn_to_angle(pid, ang)` | 炮口转向**世界方向角**（弧度，0=+X，逆时针） |
| `await kit.navigate_to(pid, (x, y))` | 迷宫寻路移动 |
| `await kit.fire(pid, 0.6)` | 开一枪 |

### 弹道 / 扫描 / 火控
| 方法 | 说明 |
|---|---|
| `await kit.raycast(pid, ang)` | 官方弹道推演：`{path, bounces, traveled, hit_pid, self_hit}` |
| `await kit.scan(pid, step_deg=30)` | **静默扫描 360°**：每个方向角的第一个命中坦克 |
| `await kit.find_aim(pid, (tx,ty), target_id=pid2)` | 找能命中目标的**世界方向角**（已排除先打自己的解） |
| `await kit.aim_and_fire(pid, (tx,ty), target_id=pid2)` | 转向 + 开火 |

`scan()` 返回列表，每项：`{angle_rad, hit_pid, self_hit, bounces, traveled}`。
**判定"可命中对手且自己不在弹道上"**：`hit_pid` 属于对手 且 `self_hit == False`。

> ★ 弹道从**炮口**起算（按当前武器自动取偏移：基础弹 2.5m、加特林 3.0m、霰弹 2.45m、双管 2.25m），
> 与真实弹丸一致 —— 所以"直线出膛"不会被误判成打自己；`self_hit=True` 只可能是**反弹回来**的弹道。
>
> ★ **弹丸消亡只由存活时间决定**：基础弹 `lifetime=10s`（18 m/s → 射程 180m）；加特林/霰弹 2s。
> **反弹次数没有实际上限**（客户端 `timeoutWindow=35` 本意是秒却与毫秒时间戳相减，
> 有效窗口只有 35ms，反弹计数随清随空，5 次上限几乎不触发）。
> 因此 `scan()` 只需保证 `traveled ≤ 弹速×寿命`，不要自己加"反弹次数"过滤。
>
> ★ **自伤标签（deadlyToOwner）**：弹丸**第一次撞墙/撞盾后才变成"可伤发射者"**
> （源码 `PreSolve`：MAZE|PROJECTILE → `makeDeadlyToOwner()`；TANK|PROJECTILE 只有
> `deadlyToOwner` 为真或对方不是发射者才致命，否则穿透）。
> 游戏内实测：`deadlyToOwner` 在第一次反弹瞬间由 False 翻 True。
> 所以 `scan()` 的 `self_hit` **只在 `bounces ≥ 1` 时才算**（0 反弹命中自己会穿透、无害，
> 记为 `self_hit_harmless=True`）。

## 4. 任务：写 `agent_ai.py`

实现如下**循环行为**（用上面的 API）：

1. 先确定**自己的角色**（`my_pid()`，默认 arrows），后续所有控制只对这个 pid 发；**绝不能操作其他角色**。
2. **静默扫描**：`scan()` 遍历一圈射击朝向，检查"哪个朝向能击中其他角色，且自己不会挡在弹道中间先被命中"
   （即 `hit_pid` 是其他角色、且 `self_hit` 为 False；可优先选 `traveled` 最短的）。
3. **发现了** → **立即停止移动**（`stop`），并**转向到该朝向**（`turn_to_angle`），然后开火（`fire`）。
   之后回到第 2 步继续循环。
4. **没发现** → 朝**最近的其他角色**（`nearest_opponent`）**寻路移动**（`navigate_to`，可分段），
   同时**继续**第 2 步的静默扫描；只要发现可命中方向就立刻停止移动并转向/开火。
5. 一直循环。每轮打印日志（例如：`[scan] found angle=... hit=pid traveled=...`、`[move] -> nearest ...`）。

要求：
* 单文件 `agent_ai.py`，可直接 `python agent_ai.py --seconds 60` 运行指定时长。
* 处理"回合切换/角色不在场"：`position()` 返回 None 时等待 1s 重试，不要崩溃。
* 不要修改 `ttkit/` 里的文件；只写 `agent_ai.py`（以及可选的自测脚本）。

## 5. 交付与验收

* 运行 `python agent_ai.py --seconds 60`，把**关键日志**贴回报告：
  * 是否找到了可命中方向（角度/目标/行程）
  * 是否停止移动并转向/开火
  * 没找到时是否朝最近角色移动
  * 确认**只操作了自己的 pid**（日志里打印 pid）
* 最终回答请简述：实现思路、遇到的坑、运行结果。

## 6. 调试技巧

* `scan(step_deg=30)` 约 12 次弹道推演；`step_deg=15` 更细但更慢。
* 炮口朝向 ≠ 车体朝向：`turn_to_angle` 已处理换算，直接用世界方向角即可。
* 近距离（<14m）射击可能因炮口偏移越过目标；远距离（>100m）飞行时间长。
* 在线模式输入上报粒度 0.5s，`hold` 时间建议 ≥0.6s（本地可以更短）。
