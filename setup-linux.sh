#!/usr/bin/env bash
# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：
#   零容忍，可永久封号且无警告）。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
# Linux 环境准备：安装依赖 + 检查浏览器 + 打印下一步
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Python 版本"
python3 --version

echo "[2/3] 安装 Python 依赖（websockets）"
python3 -m pip install --user -r requirements.txt

echo "[3/3] 浏览器探测"
python3 launch_cdp.py --check || true

cat <<'EOF'

下一步：
  ./launch-browser.sh 9222                 # 启动带 CDP 的 Chrome/Chromium 并打开游戏
  ./start-monitor.sh                       # 启动监控室（自动发现 CDP 端口）
  python3 scripts/selftest_platform.py     # 跨平台自检
  python3 scripts/selftest_monitor.py      # 监控/录制/回放全链路自检（需要游戏在局中）

无显示器的服务器请用 Xvfb：
  xvfb-run -s "-screen 0 1280x800x24" python3 launch_cdp.py 9222

环境变量（可选）：
  TT_BROWSER=/usr/bin/chromium            # 指定浏览器
  TT_CDP_PROFILE_BASE=~/.cache/tt-cdp     # 指定 profile 目录
  TT_CDP_PORT=9222                        # 固定 CDP 端口（auto 时也优先用它）
  TT_CHROME_NO_SANDBOX=1                  # 强制 --no-sandbox（root 运行时需要）
EOF
