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
# 启动带远程调试（CDP）的 Chrome/Chromium/Edge，并打开指定页面。
#   ./launch-browser.sh                       # 端口 9222，游戏页
#   ./launch-browser.sh 9223                  # 端口 9223
#   ./launch-browser.sh 9222 about:blank      # 只开空白页（本地测试）
#   ./launch-browser.sh 9222 about:blank --hold   # Xvfb 场景：保持前台直到退出
#   TT_BROWSER=/usr/bin/chromium ./launch-browser.sh 9222
set -euo pipefail

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

# 若存在 .venv 则自动激活（setup-linux.sh 会创建）
if [ -f "$DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "$DIR/.venv/bin/activate"
fi

PORT="${1:-9222}"
URL="${2:-https://tanktrouble.com/game}"
EXTRA=("${@:3}")

if python3 launch_cdp.py "$PORT" "$URL" "${EXTRA[@]}"; then
  echo "[launch-browser] CDP 就绪：http://127.0.0.1:${PORT}/json/version"
  echo "[launch-browser] 接入方式：脚本传 auto（会优先读取 TT_CDP_PORT），或直接指定端口"
else
  echo "[launch-browser] ❌ 启动失败（见上方错误；可用 TT_BROWSER 指定浏览器）" >&2
  exit 1
fi
