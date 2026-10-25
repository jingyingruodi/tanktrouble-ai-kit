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
# 启动：监控室（monitor_server 1 auto）+ 可选打开监控页
#   ./start-monitor.sh              # 前台运行，Ctrl+C 停止
#   ./start-monitor.sh 3 9222       # 3 个 CDP 实例，基准端口 9222
set -euo pipefail
cd "$(dirname "$0")"

CDP_COUNT="${1:-1}"
CDP_PORT="${2:-auto}"

echo "== 启动监控室：${CDP_COUNT} 个 CDP 实例，基准端口 ${CDP_PORT} =="
python3 monitor_server.py "${CDP_COUNT}" "${CDP_PORT}" &
PID=$!
trap 'echo; echo "[start-monitor] 停止 (pid=$PID)"; kill "$PID" 2>/dev/null || true' INT TERM EXIT
sleep 2

MONITOR_PAGE="file://$(pwd)/tanktrouble_monitor.html"
if command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  xdg-open "$MONITOR_PAGE" >/dev/null 2>&1 || true
  echo "[start-monitor] 已尝试打开监控页：$MONITOR_PAGE"
else
  echo "[start-monitor] 无图形界面/无 xdg-open，请在浏览器打开：$MONITOR_PAGE"
fi
echo "[start-monitor] 状态 WS 端口 9310..$((9310 + CDP_COUNT - 1))，Ctrl+C 停止"
wait "$PID"
