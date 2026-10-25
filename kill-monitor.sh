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
# 清理 monitor_server / playback_server 残留进程（跨平台，内部走 platform_utils；
# 按 argv 脚本文件名精确匹配，不会误杀只是命令行里含关键词的其它进程）
#   ./kill-monitor.sh          # 只杀监控相关
#   ./kill-monitor.sh --all    # 额外杀 selftest_monitor / mover_harness
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

if [ -f "$DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "$DIR/.venv/bin/activate"
fi

python3 scripts/kill_monitor.py "$@"
