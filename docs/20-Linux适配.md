# 20 · Linux 适配（Linux 设备运行指南）

> 本页说明如何在 Linux 上运行本项目。
>
> **验证状态（2026-10-25，四轮 Linux 实机复检 · 已关闭）**：
> - [docs/21](21-Linux适配审查-2026-10-25.md)（@68db7b0）：1 BLOCKER / 9 MAJOR；
> - [docs/22](22-Linux适配审查-复检2-2026-10-25.md)（@a9e5ac0）：6 PASS / 1 PARTIAL / 2 FAIL；
> - [docs/23](23-Linux适配审查-复检3-2026-10-25.md)（@369e8c9）：M4 闭环（24 项 PASS）、e2e 再次 PASS；
>   剩余 M6-B（旧 CDP + 浏览器存活仍假成功）、N9（`terminate` 重复定义死代码）、N10（record_only rc）等
>   **已在本轮修复**（§11），并用 docs/23 的复现用例回归验证。
>   - [docs/24](24-Linux适配审查-复检4-2026-10-25.md)（@7e83d6b）：**12/12 PASS，e2e 连续第三轮 PASS，
>     0 BLOCKER / 0 MAJOR**，仅 4 个 NIT，**Linux 适配已关闭**。
>   **Linux 实机最终复跑仍以你机器上的 §7 输出为准**。
> - Windows 侧另有 8 个 `.bat` 实机记录（[03 §9](03-监控室与数据采集.md)）。
> 桌面 X11 主链路（浏览器→CDP→监控→录制→回放）已在 Debian 实测可跑通；
> **无显示器（Xvfb）路径请按 §4 的常驻 Xvfb 两步法**（不要直接 `xvfb-run` 包住 launcher）。

## 1. 已适配的内容

| 模块 | 改动 |
|---|---|
| `cdp_tools/platform_utils.py` | 浏览器探测（Linux: google-chrome/chromium/microsoft-edge/brave/snap/flatpak…）、profile 目录（Linux `$XDG_DATA_HOME`、macOS `~/Library/Application Support`、Windows `%LOCALAPPDATA%`）、Python 进程枚举/终止（POSIX: `/proc` + 信号；Windows: CIM + taskkill） |
| `launch_cdp.py` | 跨平台浏览器探测；`--check` 只检查不启动；`--hold`（Xvfb 必需）；Linux root 自动 `--no-sandbox`；进程存活校验 + IPv4/IPv6 探测；stdio 重定向 |
| `cdp_tools/edge_multi.py` | 跨平台多实例；no-sandbox；`TT_FARM_PORT_BASE` |
| `scripts/kill_monitor.py` | 跨平台；**按 argv 脚本位置精确匹配**（不会误杀命令行含关键词的编辑器/日志/包装 shell） |
| `cdp_tools/ego_browser.py` | state 目录跨平台（Linux **先 XDG state** 再 XDG data）；`TT_CDP_PORT` 在 `auto` 下也生效 |
| `cdp_tools/recorder.py` | 自动创建输出目录（干净 clone 直接可录） |
| `setup-linux.sh` / `launch-browser.sh` / `start-monitor.sh` / `kill-monitor.sh` | Linux 快捷入口（`.bat` 仅 Windows 用） |

## 2. 环境要求

- Python 3.10+；依赖仅 `websockets`（由 `setup-linux.sh` 装进 `.venv`）
- Chrome / Chromium / Edge（任一，支持远程调试；flatpak 版本也支持）
- 有图形界面：X11 或 Wayland 均可；**无显示器服务器用 Xvfb（§4）**
- 可选：`xdg-open`（自动打开监控页）
- Debian/Ubuntu 需要 `python3-venv`（`sudo apt install python3-venv`）

## 3. 一次性准备

```bash
chmod +x setup-linux.sh launch-browser.sh start-monitor.sh kill-monitor.sh
./setup-linux.sh
```

`setup-linux.sh` 会：创建 `.venv` 并安装依赖 → 创建 `data/`、`scripts/scratch/` →
打印探测到的浏览器路径。若探测不到：

```bash
export TT_BROWSER=/usr/bin/chromium      # 或 google-chrome / microsoft-edge / flatpak 包装器
```

> 之后手动运行 Python 脚本前请先 `source .venv/bin/activate`（`.sh` 会自动激活）。

## 4. 启动浏览器（带 CDP）

```bash
./launch-browser.sh 9222                 # 打开游戏页并等待 CDP 就绪
./launch-browser.sh 9222 about:blank     # 只开空白页（本地测试/合规）
TT_BROWSER=/usr/bin/chromium ./launch-browser.sh 9223
```

确认：`curl -s http://127.0.0.1:9222/json/version | head -c 200`

**无显示器（服务器）**：用**常驻 Xvfb** 两步法——直接 `xvfb-run ... python3 launch_cdp.py` 会在
launcher 退出时把 Xvfb 一起回收，浏览器随即死亡（审查报告 `M3` 实测）：

```bash
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
./launch-browser.sh 9222
```

或者让 launcher 保持前台直到浏览器退出（`--hold`）：

```bash
xvfb-run -a -s "-screen 0 1280x800x24" python3 launch_cdp.py 9222 --hold
```

若游戏黑屏/无法渲染，安装 Mesa（`libgl1-mesa-dri`）并加 `--use-gl=swiftshader`；
不建议 `--headless`（WebGL/音频行为不稳定）。

## 5. 常用命令（Linux）

```bash
./start-monitor.sh                       # 监控室：monitor_server 1 auto + 打开监控页
./start-monitor.sh 3 9222                # 3 实例，基准端口 9222

python3 scripts/selftest_monitor.py      # 监控→录制→回放→清理 全链路自检（需游戏在局中）
python3 scripts/record_only.py auto --out data/demo.jsonl --fps 20 --compress
python3 playback_server.py data/demo.jsonl 9314 --fps 20 --loop
./kill-monitor.sh                        # 清理 monitor/playback 进程
```

本地对局/控制/火控脚本与 Windows 完全相同（都是 CDP + Python）：

```bash
python3 scripts/start_local_game.py auto --players 2 --restart
python3 scripts/measure_tank_dynamics.py auto
python3 scripts/verify_online_control_full.py auto --skip-move --shots 6
```

## 6. 环境变量

| 变量 | 作用 |
|---|---|
| `TT_BROWSER` | 指定浏览器可执行文件（优先级最高；`--check` 会校验可执行性） |
| `TT_CDP_PROFILE_BASE` | profile 根目录（默认 Linux `$XDG_DATA_HOME/tt-cdp`≈`~/.local/share/tt-cdp`、macOS `~/Library/Application Support/tt-cdp`、Windows `%LOCALAPPDATA%\tt-cdp`）。**从旧版升级**：旧 profile 在 `~/.cache/tt-cdp`，需 `mv ~/.cache/tt-cdp ~/.local/share/tt-cdp` 才保留登录态 |
| `TT_CDP_PORT` | 固定 CDP 端口；**`auto` 发现时也优先使用**（已修 `M2`） |
| `TT_CHROME_NO_SANDBOX=1` | 强制 `--no-sandbox`（root 运行 Chromium 需要；普通用户不建议） |
| `EGO_BROWSER_STATE_DIR` | 使用 DSH/ego-browser 时指定其 state 目录（Linux 默认 `~/.local/state/ego-lite-linux`） |
| `TT_FARM_PORT_BASE` | `edge_multi` 多实例起始端口（默认 9222） |
| `TT_LAUNCH_READY_TRIES` | `launch_cdp` 就绪轮询次数（默认 120；端口已有旧 CDP 时 40；测试/CI 可调） |

## 7. 自检（Linux 实机请逐项跑）

```bash
python3 launch_cdp.py --check            # 浏览器/路径/flags（不启动浏览器；失败 rc=2）
python3 scripts/selftest_platform.py     # 平台分支/路径/进程/ego state/TT_CDP_PORT
python3 scripts/selftest_monitor.py      # 监控/录制/回放（需游戏在局中）
```

预期输出（Linux 实机请以 docs/22 的复现方式为准）：

```
[1] 平台识别 ... [2] 三平台浏览器候选 ... [3] 当前平台浏览器 ...
[4] profile 目录（注入 env 断言三平台） ... [5] 浏览器覆盖与可执行性校验 ...
[6] Linux root no-sandbox ... [7] 进程枚举与 kill 精确匹配 ...
[8] ego state 目录（Linux 优先 XDG_STATE_HOME）...
[9] auto 发现与 TT_CDP_PORT（真 discover 路径）...
结论: ✅ PASS（约 24–25 项检查，失败 0；无 CDP 时 [9] 跳过，项数略少）
```

> `[9]` 在有 CDP 时断言 `discover('auto')` 采用 `TT_CDP_PORT` 且 `source=explicit`；
> 没有 CDP 时该项跳过（项数相应减少）。

### 7.1 回归基线（docs/24 §4 建议，按序执行）

```bash
cp -a . /tmp/e2e && cd /tmp/e2e && rm -rf .venv data scripts/scratch
bash setup-linux.sh                                     # 期望 rc=0
Xvfb :92 -screen 0 1280x800x24 & export DISPLAY=:92     # 无显示器时
./launch-browser.sh 9701 about:blank                    # 期望 CDP ready、rc=0
. .venv/bin/activate
python3 scripts/selftest_platform.py                    # 期望 24–25 项 PASS、rc=0
TT_CDP_PORT=9701 python3 scripts/selftest_monitor.py    # 需游戏在局中，期望 PASS
```

## 8. 已知限制 / 排障

| 现象 | 处理 |
|---|---|
| `--check` 找不到浏览器 / rc=2 | 设置 `TT_BROWSER`；或安装 `chromium`/`google-chrome` |
| 启动后 CDP 端口没起来 | 检查 profile 目录权限；确认没有同 profile 的浏览器实例在跑 |
| root 下浏览器立刻退出 | `TT_CHROME_NO_SANDBOX=1` 或加 `--no-sandbox` |
| 游戏 WebGL 报错/黑屏 | 装 Mesa + 常驻 Xvfb + `--use-gl=swiftshader`；避免 `--headless` |
| Xvfb 下 launcher 退出后浏览器消失 | 用 §4 的常驻 Xvfb 两步法，或 `--hold` |
| Wayland 下窗口聚焦失败 | 无碍（聚焦仅 Windows 实现）；监控数据不受影响 |
| `.bat`/`dev.ps1` 不能用 | Linux 用 `.sh` 脚本 |
| `monitor` 显示"加载中" | 监控页与游戏页需在**同一浏览器不同标签页**；同标签页导航会让监控源失去游戏上下文 |
| `ModuleNotFoundError: websockets` | 运行 `./setup-linux.sh` 或先 `source .venv/bin/activate` |
| 相对路径调用 `.sh` 没激活 venv | 已修（脚本用 `readlink -f "$0"` 解析自身目录） |

## 9. 与 Windows 的差异

- 无 `%~dp0`/PowerShell 依赖：Linux 脚本用 `readlink -f "$0"` 解析自身目录；
- profile 目录从 `%LOCALAPPDATA%` 改为 `$XDG_DATA_HOME/tt-cdp`（默认 `~/.local/share/tt-cdp`；XDG 相对路径会被忽略）；
- 进程管理从 CIM/taskkill 改为 `/proc` + SIGTERM/SIGKILL；
- 浏览器候选从 Edge/Chrome 安装路径改为常见发行版路径 + `PATH` 查找 + flatpak 导出路径。

## 10. 依赖安装（PEP 668 说明）

Ubuntu 23.04+ / Debian 12+ 的系统 Python 处于 "externally-managed" 状态，
`pip install --user` 会报 `externally-managed-environment`。请用 `setup-linux.sh`：
它会创建 `.venv` 并安装依赖；`launch-browser.sh` / `start-monitor.sh` / `kill-monitor.sh`
会自动 `source .venv/bin/activate`。手动运行 Python 脚本前请先：

```bash
source .venv/bin/activate
```

## 11. 审查发现与修复状态（对应 [docs/21](21-Linux适配审查-2026-10-25.md)）

| 编号 | 问题 | 状态 |
|---|---|---|
| M1 | ego state 目录取错（Linux 在 XDG **state**） | ✅ 已修（`state_dirs()` 优先 `XDG_STATE_HOME`） |
| M2 | `TT_CDP_PORT` 在 `auto` 下被忽略 | ✅ 已修（`preferred in (None,'','auto')`） |
| M3 | `xvfb-run` 包 launcher → 浏览器随 Xvfb 死亡 | ✅ 文档改为常驻 Xvfb / `--hold` |
| M4 | `selftest_platform` 误报 FAIL（复检2 发现 Linux 上恒 FAIL 1/21） | ✅ **二次修复**：`[4]` 改为注入 env 断言三平台；`[9]` 走真 `discover(auto)` 并断言 `source=explicit`；`[3]` 用 `_check_browser` |
| M5 | 干净 clone 缺 `data/`、`scripts/scratch/` → 录制/自检必失败 | ✅ 已修（recorder 建父目录、自检建目录、setup 建目录） |
| M6 | `launch_cdp` 假成功（复检2/复检3 发现"端口已有旧 CDP"仍会假成功） | ✅ **三次修复**：`owned` 标志 + 循环耗尽必失败；Popen 前记录端口指纹；同 profile 复用（新进程 exit 0 且旧实例仍健康）按成功处理；失败分支 terminate 收尾 |
| M7 | `launch_cdp` 丢 URL（把 URL 当端口） | ✅ 已修（argparse；`about:blank` 可用） |
| M8 | `kill_monitor` 按子串误杀非目标进程 | ✅ 已修（只匹配解释器后的脚本位置） |
| M9 | 文档"已验证"措辞夸大 | ✅ 已按实机复检改写 |
| m1 | `.sh` 未设置可执行位 | ✅ 已 `git update-index --chmod=+x` |
| m2–m6 | sh 脚本失败分支/清理/Wayland/软链 | ✅ 已重写 4 个 `.sh`；本机用 **ShellCheck 0.10（`-s bash -S error`）验证语法通过**（Linux 实机未复测） |
| m7–m20、n1–n4 | 其余 MINOR/NIT（可执行性校验、stdio、退出码、flatpak、`.venv` 忽略等） | ✅ 已处理；详见 docs/21 §3 |
| N3–N11（docs/22） | `--hold` Ctrl+C 清理、端口范围校验、`read_browser_json` 类型守卫、`ensure_browsers` argparse/快速失败、`edge_multi` 入口健壮性、缺依赖提示、profile 放 XDG data | ✅ 已处理 |
| docs/23 追加 | M6-B 循环耗尽假成功；`terminate`/`script_token_match` 重复定义（旧版覆盖新版）；`record_only` 缺依赖 rc=0；`TT_FARM_PORT_BASE` 范围校验；`ensure_browsers` 全非法端口 rc=0；裸主机名 URL；profile 迁移提示；`[10]` 无重复 def 自检 | ✅ 已修复 |
| docs/24（NIT） | `TT_LAUNCH_READY_TRIES` 未校验/未文档化；旧 CDP 场景默认等待 ~30s；`tries=0` 文案；`[3]` 错误分支自检提示 | ✅ 已处理（本轮） |

> 说明：以上修复已在本机（Windows）做编译 + 单测 + 回归（含 docs/22 给出的 M4/M6 复现用例）；
> **Linux 实机最终复跑仍需按 §7 命令确认**，若结果与预期不符请把输出贴回仓库 issue。
