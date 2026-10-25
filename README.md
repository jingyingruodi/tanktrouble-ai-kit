# TankTrouble AI Research Kit

> **非官方研究/教育项目**，与 TankTrouble 及其开发方**没有任何关联**，未获得其授权或背书。
> 本项目**不包含**游戏客户端、美术、音频、地图或任何解包的游戏资源。
> 使用本项目可能违反游戏服务条款（ToS/EULA），并可能导致账号被封禁。
> **请勿在公开在线对局中使用**。详见 [NOTICE](NOTICE) 与 [RISK-ASSESSMENT.md](RISK-ASSESSMENT.md)。

一套围绕 TankTrouble 网页游戏的研究工具：通过 Chrome DevTools Protocol（CDP）读取游戏状态、
以"与人类按键等价"的方式控制角色，并在此基础上做**弹道建模、火控与行为 AI** 的实证研究。

## 这是什么 / 不是什么

**是**：
- 一套可复用的 Python 封装包 `kit/ttkit`：读状态、控制角色、官方弹道推演、扫描/选角/开火门；
- 一批**实测脚本**与**实测数据结论**（速度、转向量化、弹丸寿命、自伤标签、毒圈序列等）；
- 11 轮**黑盒测试**记录：把包交给独立 AI 写"基础行为 AI"，用它暴露包的缺陷并逐轮修复（[docs/18](docs/18-黑盒测试与弹丸规则-2026-10-24.md)）。

**不是**：
- 不是外挂/自动瞄准分发物（无注入、无内存修改、不绕过反作弊）；
- 不含任何游戏资源或客户端代码；
- 不保证对任何游戏版本有效（本项目针对的客户端 bundle 固定版本，游戏更新后可能失效）。

## 目录结构

```
kit/               封装包 ttkit（对外 API；见 kit/README.md）
examples/          行为 AI 示例（agent_ai.py）
cdp_tools/         CDP 连接、输入控制、弹道、地形、常量
scripts/           实测/验证脚本（弹道、武器、毒圈、地雷、控制测试等）
docs/              研究文档 01–18（含 11 轮黑盒测试记录）
rl/                观测/动作/数据集雏形（未训练）
```

## 快速开始（仅本地研究环境）

前置：Python 3.10+、Chrome/Edge、TankTrouble 网页；以 `--remote-debugging-port` 启动浏览器。

```bash
pip install -r requirements.txt
python scripts/start_local_game.py auto --players 2 --restart   # 本地双人对局
cd kit && python -c "import asyncio; from ttkit import Kit; ..." # 见 kit/README.md
```

> 在线房间相关脚本仅用于**受控自建房间**的研究；请勿在公开对局使用。

## 主要实测结论（摘要）

| 主题 | 结论 |
|---|---|
| 弹丸上限 | **存活时间**（基础弹 10s ≈ 180m），**反弹次数无实际上限** |
| 自伤标签 | `deadlyToOwner` 在**第一次撞墙/盾**时翻转；之前对自己无害 |
| 转向 | 5 rad/s ≈ 4.775°/帧；转向量化/离群使"±1° 精度"成为命中关键 |
| 官方弹道 vs 实弹 | 官方 `calculateProjectilePath` 是**零宽射线**，掠射/多反弹有累积偏差 |
| 行为 AI 命中率 | 静止靶（小图）最高 **100%**；移动靶：朴素打法 16% → 提前量+对点选角 **67%** |
| 控错角色 | 11 轮黑盒测试全部"只控自己 pid"（需应用层护栏，包只校验"可控角色"） |

## 许可

代码采用 **MIT License**（见 [LICENSE](LICENSE)）。MIT **只覆盖本仓库作者原创的代码**，
不授予任何 TankTrouble 相关的知识产权或商标使用权，也不包含任何游戏资源。
第三方依赖：`websockets`（BSD-3-Clause）。

## 更多

- 包使用文档：[kit/README.md](kit/README.md)
- 研究索引：[docs/README.md](docs/README.md)
- 风险与合规：[RISK-ASSESSMENT.md](RISK-ASSESSMENT.md)
- 安全提示：[SECURITY.md](SECURITY.md)
