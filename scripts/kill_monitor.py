# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为
# （官方 Community Standards 第 8 条 "No hacking on the website"：
#   零容忍，可永久封号且无警告）。在线相关脚本另见文件内附加说明。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
"""
重启监控室辅助：杀掉残留的 monitor_server / playback_server 进程。

跨平台（Linux / macOS / Windows）；**按 argv 中的脚本文件名精确匹配**，
不会因为命令行里恰好出现 "monitor_server" 子串而误杀编辑器/日志/包装 shell。

用法：python scripts/kill_monitor.py [--all]
  --all  额外把 selftest_monitor / mover_harness 也杀掉（默认不动）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))

from platform_utils import list_processes, terminate, script_token_match  # noqa: E402

NAMES = ('monitor_server.py', 'playback_server.py')
NAMES_ALL = NAMES + ('selftest_monitor.py', 'mover_harness.py')


def main():
    names = NAMES_ALL if '--all' in sys.argv else NAMES
    procs = list_processes()
    if procs is None:
        print('[kill] ⚠️ 无法枚举进程（ps/PowerShell 不可用），请手动检查', flush=True)
        return 2
    me = os.getpid()
    targets = [(pid, argv) for pid, argv in procs
               if pid != me and script_token_match(argv, names)]
    if not targets:
        print('[kill] 无残留监控进程', flush=True)
        return 0
    killed = []
    for pid, argv in targets:
        cmd = ' '.join(argv)
        print('[kill] 终止 PID %d: %s' % (pid, cmd.strip()[:90]), flush=True)
        if terminate(pid):
            killed.append(pid)
        else:
            print('[kill] 终止 %d 失败（权限/已退出）' % pid, flush=True)
    print('[kill] 已终止: %s' % killed, flush=True)
    return 0 if killed else 1


if __name__ == '__main__':
    sys.exit(main())
