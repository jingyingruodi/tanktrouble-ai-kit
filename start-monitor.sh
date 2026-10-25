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
# 启动：监控室（monitor_server）+ 可选打开监控页
#   ./start-monitor.sh              # 1 个 CDP 实例，auto 端口
#   ./start-monitor.sh 3 9222       # 3 个 CDP 实例，基准端口 9222
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

CDP_COUNT="${1:-1}"
CDP_PORT="${2:-auto}"

# 预检依赖，避免启动一半才报 traceback
if ! python3 -c 'import websockets' 2>/dev/null; then
  echo "[start-monitor] ❌ 缺少 websockets：先运行 ./setup-linux.sh（或 pip install -r requirements.txt）" >&2
  exit 2
fi

echo "== 启动监控室：${CDP_COUNT} 个 CDP 实例，基准端口 ${CDP_PORT} =="
python3 monitor_server.py "${CDP_COUNT}" "${CDP_PORT}" &
PID=$!

cleanup() {
  trap - INT TERM EXIT
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.2
    done
    # 10 次（2s）仍不退 → SIGKILL 兜底，避免变孤儿占端口
    kill -9 "$PID" 2>/dev/null || true
  fi
  echo "[start-monitor] 已停止"
}
trap cleanup INT TERM EXIT

sleep 2
if ! kill -0 "$PID" 2>/dev/null; then
  echo "[start-monitor] ❌ monitor_server 启动失败（见上方错误）" >&2
  exit 1
fi

MONITOR_PAGE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve().as_uri())' \
  "$DIR/tanktrouble_monitor.html")"
if command -v xdg-open >/dev/null 2>&1 \
   && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
  xdg-open "$MONITOR_PAGE" >/dev/null 2>&1 || true
  echo "[start-monitor] 已尝试打开监控页：$MONITOR_PAGE"
else
  echo "[start-monitor] 无图形界面/无 xdg-open，请在浏览器打开：$MONITOR_PAGE"
fi
echo "[start-monitor] 状态 WS 端口 9310..$((9310 + CDP_COUNT - 1))，Ctrl+C 停止"
wait "$PID"
