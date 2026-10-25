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
- 11 轮**行为 AI 黑盒测试**：[docs/18](docs/18-黑盒测试与弹丸规则-2026-10-24.md)。
- **纯原语黑盒（两角度）**：① 只给原语让 AI 自己写火控，5 轮均 0 自伤、0 误控（前 4 轮 5/5 命中；第 5 轮 18 发 4 杀）（[docs/26](docs/26-纯原语黑盒测试-2026-10-25.md)）；② 只读原语写单页监控网页核对 GET 接口（坦克 0.004m/道具 0.006m/弹丸 0.19px；第 2 轮复测抓出并修掉 walls 幽灵墙，31/31 可碰撞墙 P=R=1.0；第 3 轮动态核对坦克移动/弹丸飞行，冻结与弹丸通过、v6.4 补 raf 帧戳对齐）（[docs/27](docs/27-监控get接口黑盒测试-2026-10-26.md)）。

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
docs/              研究文档 01–27（含 Linux 审查、11 轮行为 AI 黑盒 + 5 轮纯原语火控黑盒 + 监控 GET 黑盒）
rl/                观测/动作/数据集雏形（未训练）
```

## 快速开始（仅本地研究环境）

前置：Python 3.10+、Chrome/Edge、TankTrouble 网页；以 `--remote-debugging-port` 启动浏览器。

**Windows**
```powershell
python -m pip install -r requirements.txt
python scripts/start_local_game.py auto --players 2 --restart   # 本地双人对局
```

**Linux / macOS**
```bash
./setup-linux.sh                                                 # 建 .venv + 装依赖 + 探测浏览器
./launch-browser.sh 9222                                         # 带 CDP 启动并打开游戏
python3 scripts/start_local_game.py auto --players 2 --restart
```

API 用法见 [kit/README.md](kit/README.md)。

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


## 监控室 / 录制 / 回放

```bash
python launch_cdp.py --check                        # 先检查浏览器/路径（不启动）
python monitor_server.py 1 auto                     # 监控室数据源（WS 9310+，auto 端口）
python scripts/selftest_monitor.py                  # 全链路自检：监控→录制→回放→清理
python scripts/record_only.py auto --out data/demo.jsonl --fps 20 --compress
python playback_server.py data/demo.jsonl 9314 --fps 20   # 作为"回放实例"接入监控室
python scripts/kill_monitor.py                      # 清理 monitor/playback 进程
```

打开 `tanktrouble_monitor.html`（自动发现 9310–9317 实例）即可看到实时状态与回放；
监控室页面与游戏页请放在**同一浏览器的不同标签页**（同标签页导航会让监控源失去游戏上下文）。
bat 快捷入口使用 `%~dp0` 相对路径，可移植。
> 命令里的 `python` 在 Windows 上可用；Linux/macOS 请用 `python3`（`.sh` 脚本已处理）。


## Linux 支持

跨平台适配（Windows / macOS / Linux）：浏览器自动探测、profile 目录按平台取
（Linux `~/.local/share/tt-cdp`）、进程清理跨平台、`TT_BROWSER` / `TT_CDP_PORT` /
`TT_CDP_PROFILE_BASE` / `TT_CHROME_NO_SANDBOX` 环境变量。
经过 **四轮 Linux 实机复检**（[docs/21](docs/21-Linux适配审查-2026-10-25.md) →
[docs/24](docs/24-Linux适配审查-复检4-2026-10-25.md)）：最终 **12/12 PASS、0 BLOCKER / 0 MAJOR**，
Linux 适配已关闭；干净副本「setup → Xvfb → 监控→录制→回放」e2e 连续三轮实测 PASS。

```bash
chmod +x setup-linux.sh launch-browser.sh start-monitor.sh kill-monitor.sh
./setup-linux.sh                    # 依赖 + 浏览器探测
./launch-browser.sh 9222            # 带 CDP 启动并打开游戏
./start-monitor.sh                  # 监控室
python3 scripts/selftest_platform.py   # 跨平台自检（约 24–25 项，期望 PASS）
python3 scripts/selftest_monitor.py    # 监控/录制/回放自检（需游戏在局中）
```

无显示器的服务器请用**常驻 Xvfb**：`Xvfb :99 -screen 0 1280x800x24 & export DISPLAY=:99`，
再 `./launch-browser.sh 9222`（直接用 `xvfb-run` 包 launcher 会在其退出时回收 Xvfb，见 docs/20 §4）。
详见 [docs/20-Linux适配.md](docs/20-Linux适配.md)。`.bat`/`dev.ps1` 仅 Windows；Linux 用 `.sh`。

## 许可

代码采用 **MIT License**（见 [LICENSE](LICENSE)）。MIT **只覆盖本仓库作者原创的代码**，
不授予任何 TankTrouble 相关的知识产权或商标使用权，也不包含任何游戏资源。
第三方依赖：`websockets`（BSD-3-Clause）。

## 更多

- 包使用文档：[kit/README.md](kit/README.md)
- 研究索引：[docs/README.md](docs/README.md)
- 风险与合规：[RISK-ASSESSMENT.md](RISK-ASSESSMENT.md)
- 安全提示：[SECURITY.md](SECURITY.md)
