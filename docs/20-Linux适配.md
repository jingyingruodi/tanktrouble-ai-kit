# 20 · Linux 适配（Linux 设备运行指南）

> 本页说明如何在 Linux 上运行本项目。**适配代码已实现并在 Windows 上验证了平台分支逻辑**
> （`scripts/selftest_platform.py` 覆盖三平台候选/路径/进程/端口环境变量）；
> Linux 实机请按 §7 的自检命令逐项确认。

## 1. 已适配的内容

| 模块 | 改动 |
|---|---|
| `cdp_tools/platform_utils.py`（新增） | 浏览器探测（Linux: google-chrome/chromium/microsoft-edge/brave/snap…）、profile 目录、Python 进程列表/终止 |
| `launch_cdp.py` | 跨平台浏览器探测；`--check` 只打印不启动；Linux root 自动加 `--no-sandbox`；窗口聚焦仅 Windows |
| `cdp_tools/edge_multi.py` | 同上（多实例启动） |
| `scripts/kill_monitor.py` | 跨平台：Windows 用 PowerShell(CIM)+taskkill；POSIX 用 `ps` + SIGTERM/SIGKILL |
| `cdp_tools/ego_browser.py` | state 目录跨平台（XDG / Library / LOCALAPPDATA）；支持 `TT_CDP_PORT` 固定端口 |
| `setup-linux.sh` / `launch-browser.sh` / `start-monitor.sh` / `kill-monitor.sh`（新增） | Linux 快捷入口（`.bat` 仅 Windows 用） |

## 2. 环境要求

- Python 3.10+；`pip install -r requirements.txt`（仅 `websockets`）
- Chrome / Chromium / Edge（任一，支持远程调试）
- 有图形界面：X11 或 Wayland 均可；**无显示器服务器用 Xvfb**
- 可选：`xdg-open`（自动打开监控页）

## 3. 一次性准备

```bash
chmod +x setup-linux.sh launch-browser.sh start-monitor.sh kill-monitor.sh
./setup-linux.sh
```

它会安装依赖并打印探测到的浏览器路径；若探测不到，设置：

```bash
export TT_BROWSER=/usr/bin/chromium      # 或 google-chrome / microsoft-edge
```

## 4. 启动浏览器（带 CDP）

```bash
./launch-browser.sh 9222                 # 打开游戏页并等待 CDP 就绪
# 或：TT_BROWSER=/usr/bin/chromium ./launch-browser.sh 9223
```

确认：`curl -s http://127.0.0.1:9222/json/version | head -c 200`

> 无显示器：
> ```bash
> xvfb-run -s "-screen 0 1280x800x24" python3 launch_cdp.py 9222
> ```
> 若游戏黑屏/无法渲染，加 `--use-gl=swiftshader` 或安装 Mesa（`libgl1-mesa-dri`），
> 不建议 `--headless`（WebGL/音频行为不稳定）。

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
| `TT_BROWSER` | 指定浏览器可执行文件（优先级最高） |
| `TT_CDP_PROFILE_BASE` | profile 根目录（默认 Linux `$XDG_CACHE_HOME/tt-cdp` 或 `~/.cache/tt-cdp`） |
| `TT_CDP_PORT` | 固定 CDP 端口（`auto` 发现时也优先使用） |
| `TT_CDP_PROFILE_BASE` | 同上 |
| `TT_CHROME_NO_SANDBOX=1` | 强制 `--no-sandbox`（root 运行 Chromium 需要；普通用户不建议） |
| `EGO_BROWSER_STATE_DIR` | 若使用 DSH/ego-browser，可指定其 state 目录 |

## 7. 自检（Linux 实机请逐项跑）

```bash
python3 launch_cdp.py --check            # 浏览器/路径/flags（--check 不启动浏览器）
python3 scripts/selftest_platform.py     # 三平台候选/路径/进程/环境变量（应 PASS）
python3 scripts/selftest_monitor.py      # 监控/录制/回放（需游戏在局中，应 PASS）
```

预期输出示例（Windows 实测；Linux 除路径不同外一致）：

```
[1] 平台识别 ... [2] 三平台浏览器候选 ... [3] 当前平台浏览器 ...
[4] profile 目录 ... [5] 环境变量覆盖 ... [6] 进程列表 ...
[7] TT_CDP_PORT ... 结论: ✅ PASS（14 项检查，失败 0）
```

## 8. 已知限制 / 排障

| 现象 | 处理 |
|---|---|
| `--check` 找不到浏览器 | 设置 `TT_BROWSER`；或安装 `chromium`/`google-chrome` |
| 启动后 CDP 端口没起来 | 检查 profile 目录权限；确认没有同 profile 的浏览器实例在跑 |
| root 下浏览器立刻退出 | `TT_CHROME_NO_SANDBOX=1` 或加 `--no-sandbox` |
| 游戏 WebGL 报错/黑屏 | 装 Mesa，使用 Xvfb + `--use-gl=swiftshader`；避免 `--headless` |
| Wayland 下窗口聚焦失败 | 无碍（聚焦仅 Windows 实现）；监控数据不受影响 |
| `.bat`/`dev.ps1` 不能用 | Linux 用 `.sh` 脚本；`pwsh` 可选装但不必需 |
| `monitor` 显示"加载中" | 监控页与游戏页需在**同一浏览器不同标签页**；同标签页导航会让监控源失去游戏上下文 |

## 9. 与 Windows 的差异

- 无 `%~dp0`/PowerShell 依赖：Linux 脚本用 `$(dirname "$0")`；
- profile 目录从 `%LOCALAPPDATA%` 改为 XDG cache；
- 进程管理从 wmic/taskkill 改为 `ps` + 信号；
- 浏览器候选从 Edge/Chrome 安装路径改为常见发行版路径 + `PATH` 查找。
