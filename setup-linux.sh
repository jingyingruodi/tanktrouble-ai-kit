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

# 解析脚本真实目录（支持软链接 / 相对路径调用）
resolve_self() {
  if command -v readlink >/dev/null 2>&1 && readlink -f "$0" >/dev/null 2>&1; then
    readlink -f "$0"
  else
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0"
  fi
}
SELF="$(resolve_self)"
DIR="$(cd "$(dirname "$SELF")" && pwd)"
cd "$DIR"

echo "[1/4] Python 版本"
python3 --version

echo "[2/4] 创建虚拟环境并安装依赖（避免 PEP 668 externally-managed 限制）"
if ! python3 -m venv .venv; then
  echo "[错误] 创建 venv 失败。Debian/Ubuntu 可能需要：sudo apt install python3-venv" >&2
  exit 1
fi
./.venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1 || true
./.venv/bin/python -m pip install -r requirements.txt

echo "[3/4] 创建运行时目录（data/、scripts/scratch/；均为 .gitignore 排除）"
mkdir -p data scripts/scratch

echo "[4/4] 浏览器探测"
./.venv/bin/python launch_cdp.py --check || true

cat <<'EOF'

下一步（.sh 会自动激活 .venv；手动跑 python 脚本前先 `source .venv/bin/activate`）：
  ./launch-browser.sh 9222                 # 启动带 CDP 的 Chrome/Chromium 并打开游戏页
  ./start-monitor.sh                       # 启动监控室（默认 auto 发现 CDP 端口）
  python3 scripts/selftest_platform.py     # 跨平台自检（应 PASS）
  python3 scripts/selftest_monitor.py      # 监控/录制/回放自检（需要游戏在局中）

无显示器的服务器：用【常驻 Xvfb】两步法（不要直接 xvfb-run，见 docs/20 §4）：
  Xvfb :99 -screen 0 1280x800x24 &
  export DISPLAY=:99
  ./launch-browser.sh 9222

环境变量（可选）：
  TT_BROWSER=/usr/bin/chromium            # 指定浏览器（flatpak 亦可）
  TT_CDP_PROFILE_BASE=~/.cache/tt-cdp     # 指定 profile 目录
  TT_CDP_PORT=9222                        # 固定 CDP 端口（auto 时也优先使用）
  TT_CHROME_NO_SANDBOX=1                  # 强制 --no-sandbox（root 运行时需要）
EOF
