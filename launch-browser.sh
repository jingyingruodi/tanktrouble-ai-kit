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
# 启动带远程调试（CDP）的 Chrome/Chromium/Edge，并打开游戏页。
#   ./launch-browser.sh                 # 端口 9222
#   ./launch-browser.sh 9223            # 端口 9223
#   TT_BROWSER=/usr/bin/chromium ./launch-browser.sh 9222
set -euo pipefail
cd "$(dirname "$0")"
# 若存在 .venv 则自动激活（setup-linux.sh 会创建）
if [ -f "$(dirname "$0")/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "$(dirname "$0")/.venv/bin/activate"
fi

PORT="${1:-9222}"
URL="${2:-https://tanktrouble.com/game}"
python3 launch_cdp.py "$PORT" "$URL"
RC=$?
if [ $RC -eq 0 ]; then
  echo "[launch-browser] CDP 就绪：http://127.0.0.1:${PORT}/json/version"
  echo "[launch-browser] 可用 'TT_CDP_PORT=${PORT}' 或脚本的 auto 机制接入"
fi
exit $RC
