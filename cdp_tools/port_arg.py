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
"""命令行端口解析：``<port>`` / ``auto`` / ``TT_CDP_PORT`` 统一入口。

    from port_arg import port_from_args
    port = port_from_args(args)          # args = sys.argv[1:]

规则（与引擎脚本一致）：
  1. 第一个参数是数字      -> 用该端口；
  2. 第一个参数是 'auto'   -> resolve_port_cli('auto')（内部会优先 TT_CDP_PORT）；
  3. 没有端口参数          -> 有 TT_CDP_PORT 则解析它，否则用默认 9222；
  4. 解析失败打印可读错误并以 exit code 2 退出。
"""
import os
import sys

_DEFAULT = 9222


def port_from_args(args, default=_DEFAULT):
    env = os.environ.get('TT_CDP_PORT')
    first = args[0] if args else None
    want_auto = False
    if first and first.isdigit():
        return int(first)
    if first == 'auto':
        want_auto = True
    elif first is None or first.startswith('-'):
        want_auto = bool(env)
    if want_auto:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from ego_browser import resolve_port_cli
        return resolve_port_cli('auto')
    if env and env.isdigit():
        return int(env)
    return default
