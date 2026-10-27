# TankTrouble AI — GET 接口完成文档

> **⚠️ 历史记录**：本文档描述的是早期 `tamtam_prod.user.js`（Tampermonkey 脚本）
> + WebSocket 推送方案下的 GET 接口清单。当前项目已切换为 **CDP 自动化体系**
> （见 [01-游戏接口总览.md](01-游戏接口总览.md)），但本文档的**游戏状态字段结构**
> 仍具参考价值，故保留。

> 版本：tamtam_prod.user.js v3.3（Tampermonkey 脚本）
> 通过 WebSocket 发送 JSON 状态到 Python AI 服务器，Viewer 独立渲染

---

## 📡 GET 接口完成清单

### 玩家状态
| 数据 | 来源 | 说明 |
|------|------|------|
| 位置/朝向 | `tank.getX/Y/getRotation` | 角度归一化，`PPM=20` 映射 |
| 存活 | `tank.isAlive()` | ⚠️ **与当前代码不符**：现役坦克对象**没有** `isAlive()`；`cdp_tools/tankctl.py` 只在方法存在时才调，否则视为存活。现役判活看 id 是否仍在 `getTanks()`（`gameutils.tank_alive`） |
| 武器类型 | `gc.getActiveWeapon(playerId).getType()` | -1~5 七种武器 |
| 护盾 | `gc.getUpgradeByPlayerIdAndType(pid,2)` | 剩余寿命 `getFields()._lifetime`（此读取路径现役代码仍在使用） |

### 弹丸
| 数据 | 来源 |
|------|------|
| 位置/速度 | `getX/Y/SpeedX/SpeedY` |
| 类型 | `getType()`（颜色区分） |
| 所属玩家 | `getPlayerId()` |
| 已存活时间 | `timeAlive` |
| 可自伤 | `deadlyToOwner` + ☀ 标记 |

### 场景实体
| 实体 | 来源 |
|------|------|
| 道具箱 | `getCollectibles()` 位置+类型(0-6) |
| 地雷 | `getTraps()` 位置+激活+所属 |
| 墙体 | `getMaze().getTiles()` 线段 |
| 毒圈 | `getZones().getFields()` + 平滑插值 |

### 游戏状态
| 数据 | 来源 |
|------|------|
| 比分 | `getScoreByPlayerIdAndType()` 全玩家击杀+胜利 |
| 回合状态 | `addRoundEventListener` → betweenRounds |
| 毒圈模式 | `getStorm()` |

### 关键修复
1. **坐标错误**：实体坐标需 `×PPM`，方向映射 `0=右 1=下 2=左 3=上`
2. **毒圈插值**：`consumed = max(0, target-1+frac)` 平滑不反弹
3. **弹丸所属**：走 GC fallback 路径，用 `getPlayerId()`
4. **maze null 崩溃**：getState 全 try/catch

---

## 1. 玩家状态

## 1. 玩家状态

| 数据 | 来源 | 说明 |
|------|------|------|
| 位置/朝向 | `tank.getX/getY/getRotation` | 角度归一化到 [0, 2π)，`PPM=20` 像素映射 |
| 存活 | `tank.isAlive()` | |
| 武器类型 | `gc.getActiveWeapon(playerId).getType()` | -1~5 七种武器 |
| 护盾 | `gc.getUpgradeByPlayerIdAndType(pid, 2)` | 剩余寿命 `getFields()._lifetime` |

玩家 ID 为数字字符串（如 `"22476825"`），通过 `gc.getAllPlayerIds()` 获取完整列表（含死亡玩家）。

**武器类型枚举：**

| type | 名称 | 说明 |
|------|------|------|
| -1 | Bullet | 默认武器，永不销毁 |
| 0 | Laser | 激光，180 m/s，0.8s 寿命，发射后 0.2s 锁定 |
| 1 | DoubleBarrel | 双管枪，10 发 |
| 2 | Shotgun | 霰弹枪，3 发×20 钢珠 |
| 3 | HomingMissile | 追踪导弹 |
| 4 | Mine | 地雷，3 枚，后退投放 |
| 5 | Gatling | 加特林，20 发，充能 0.5s |

---

## 2. 弹丸状态

| 数据 | 来源 |
|------|------|
| 位置/速度 | `getX/getY/getSpeedX/getSpeedY` |
| 类型 | `getType()` |
| **所属玩家** | `getPlayerId()`（或属性 `playerId`） |
| **已存活时间** | `timeAlive` |
| **可自伤** | `deadlyToOwner` / `isDeadlyToOwner()` |

> 注意：弹丸实际从 **GC fallback**（`gc.getProjectiles()`）提取，不是 `getRoundModel().getProjectiles()`。在线模式下 RoundModel 路径返回空。

Viewer 中每个弹丸：
- 按类型着色（黄=Bullet, 粉=Laser, 橙=DblBrl, 青=Shotgun, 紫=Homing, 红=Mine, 绿=Gatling）
- 下方标注所属玩家 ID
- 可自伤弹丸加 ☀ 标记

---

## 3. 场景实体

| 实体 | 来源 | 内容 |
|------|------|------|
| 道具箱 | `getCollectibles()` | 位置 + 类型(0-6) |
| 地雷 | `getTraps()` | 位置 + 激活 + 所属玩家 |
| 墙体 | `getMaze().getTiles()` | 线段数组（含外边界） |
| 毒圈 | `getZones().getFields()` | 见下节 |

**道具类型枚举：**

| type | 名称 |
|------|------|
| 0 | Laser 激光箱 |
| 1 | DoubleBarrel 双管枪箱 |
| 2 | Shotgun 霰弹枪箱 |
| 3 | HomingMissile 追踪导弹箱 |
| 4 | Mine 地雷箱 |
| 5 | Gatling 加特林箱 |
| 6 | Shield 护盾箱（升级非武器） |

---

## 4. Radioactive Fog 毒圈（在线模式特有）

官方名称 **Radioactive Fog**（辐射雾）。随对局推进向内收缩，**接触即秒杀，无视护盾**（护盾仅反弹弹丸）。判定条件为**坦克中心点越过边界**，连续浮点坐标平滑移动。

数据来自 `getZones().getFields()`：

```json
{
  "_expansionTime": 5.234,
  "_expansionSequence": [0,3,1,1,2,2,0,2,2,2,3,0,...],
  "_tileBounds": { "minX": 0, "minY": 0, "maxX": 10, "maxY": 6 },
  "_expansionSide": 0,
  "_expansionTilesRight": 2,
  "_expansionTilesBottom": 1,
  "_expansionTilesLeft": 0,
  "_expansionTilesTop": 0
}
```

**`_expansionSide` 方向映射：**

| 值 | 方向 | 影响边界 |
|----|------|---------|
| 0 | 右 | maxX 减小 |
| 1 | 下 | maxY 减小 |
| 2 | 左 | minX 增大 |
| 3 | 上 | minY 增大 |
| -1 | 步间等待 | 无 |

**实时安全区计算（含平滑插值）：**

```javascript
// 整数 tile 边界
var minX = tb.minX + fields._expansionTilesLeft;
var maxX = tb.maxX - fields._expansionTilesRight;
var minY = tb.minY + fields._expansionTilesTop;
var maxY = tb.maxY - fields._expansionTilesBottom;

// 平滑插值（当前步内）
var frac = 1 - fields._expansionTime / stepMaxTime;
var side = fields._expansionSide;

// 活跃方向从 target-1 平滑到 target（不超调不反弹）
if (side === 0) maxX = tb.maxX - Math.max(0, fields._expansionTilesRight - 1 + frac);
if (side === 1) maxY = tb.maxY - Math.max(0, fields._expansionTilesBottom - 1 + frac);
if (side === 2) minX = tb.minX + Math.max(0, fields._expansionTilesLeft - 1 + frac);
if (side === 3) minY = tb.minY + Math.max(0, fields._expansionTilesTop - 1 + frac);
```

**`zones[].nTiles` 语义（2026-10-27 明确）**：`STATE_JS` 中 `nTiles = Zone.getTiles().length`，
是**毒圈雾/威胁 tile 数**（游戏 AI 的 threatMap 也用它），**不是安全区格数**，且全图覆盖时也可能非零
（实测 invalid safeTile 时 nTiles 仍=25）。判"全图覆盖/安全区归零"请用
`safeTile.minX > maxX || minY > maxY`（或 `safeSmooth` 宽/高 ≤0），不要用 `nTiles`。

**关于步内平滑**：`gameutils.extract_storm_zones` / `get_storm_zones` 仍只做整数 tile 边界；
但监控状态接口 **v6.6 起 `zones[].safeSmooth`** 已实现本文公式（仅活跃边插值），
并已在 docs/27 §4.4–§4.6 用在线像素验证（残余为雾贴图有机波动，见下）。
像素比对容差建议 ≥3 m（HALLOWEEN 等主题雾团边缘可内偏 1.5–2.9 m）。

**在线长读纪律（2026-10-27）**：在线房间中长时间 `state()`/截图时**游戏 tab 必须保持前台**；
后台 tab 会让 Phaser rAF 暂停（`raf_frame` 冻结）并最终导致掉线。监控页轮询不要长期抢占前台，
截图前 `activate_page`、截完立即切回。

**Box2D 物理结构**：`getB2DBody()` 返回 4 个 Fixture（边墙矩形条），顶点在 `GetShape().m_vertices`（世界米，×PPM=20 转像素）。

---

## 5. 游戏状态

| 数据 | 来源 | 说明 |
|------|------|------|
| 比分 | `gc.getScoreByPlayerIdAndType(pid, 0/1)` | 0=击杀分 1=胜利分，跨回合累加 |
| 全玩家列表 | `gc.getAllPlayerIds()` | 含已死亡玩家 |
| 回合状态 | `addRoundEventListener` | `data` 参数为事件名（小写），如 `'round started'`、`'round ended'` |
| betweenRounds | 事件驱动 | `ROUND_ENDED/ROUND_CREATED` → true，`ROUND_STARTED` → false |

**回合事件名**（来自 `addRoundEventListener` 的 `data` 参数，全小写）：
- `'round created'` → 回合创建
- `'round ended'` → 回合结束（进入结算）
- `'count down'` → 倒计时
- `'round started'` → 回合开始
- `'tank killed'` → 坦克击杀

---

## 6. 关键修复记录

1. **坐标系统**：实体坐标（米）需 `×PPM(20)` 转世界像素（200px/tile）
2. **毒圈方向**：`_expansionSide` 映射为 `0=右 1=下 2=左 3=上`
3. **毒圈插值**：`consumed = max(0, target-1+frac)`，从上一格平滑到目标格，避免"先跳到位再反弹"
4. **弹丸所属**：走 `gc.getProjectiles()` fallback 路径，用 `getPlayerId()`
5. **maze null 崩溃**：`getState()` 全函数 try/catch，任何异常不阻断数据发送
6. **事件参数**：`addRoundEventListener(id, evt, data)` 的 **`data`** 是事件名字符串，`evt` 才是事件对象

---

## 7. 数据流架构（历史）

> 下图的 `tamtam_prod.user.js` / `ws://localhost:8765` / `ai_server.py` /
> `tanktrouble_viewer.html` **在当前仓库里都不存在**，8765 端口也没有进程监听。
> 现役架构见 [01-游戏接口总览.md](01-游戏接口总览.md)：CDP 轮询 + `setInputState`
> 控制，坐标单位为米（1 tile = 10m），监控室前端用 `maze.w*10` 米推缩放系数。

```
TankTrouble 游戏页面 (tamtam_prod.user.js)
  │ 每 33ms 提取状态
  │ WebSocket ws://localhost:8765
  ▼
Python AI Server (ai_server.py)
  │ 广播 {type:'state', data:{...}} 给所有客户端
  │ 计算 AI 动作 {type:'input', actions:{p1:[...], p2:[...]}}
  ▼
Viewer (tanktrouble_viewer.html)     游戏页面键盘模拟
  60fps 渲染                             ← 接收动作指令

坐标转换: 世界像素 = tile坐标 × 200；画布像素 = ox + 世界像素 × scale
```

---

## 8. 下一步

- **SET 接口**：AI 控制坦克（键盘事件已实现基础版）
- **PPO 训练**：用 `timeAlive`/`deadlyToOwner`/毒圈数据构建奖励函数
- **在线对战**：处理网络延迟与多玩家对抗
