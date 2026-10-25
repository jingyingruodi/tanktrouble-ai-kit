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
TankTrouble 多实例训练管理器
============================
启动 N 个独立 Edge 实例（通过 --user-data-dir 各自独立 profile），
每个都有自己的 CDP 调试端口和独立的游戏登录会话。
支持针对真实游戏服务器的并行训练，互不冲突。

重要：--user-data-dir 路径【不能含空格】。
每个实例独立端口（9222, 9223, ...）和独立 profile 目录（<TT_CDP_PROFILE_BASE>/0, ...）。

用法：
    from edge_multi import EdgeFarm
    farm = EdgeFarm(count=2)
    farm.launch_all()              # 启动实例
    farm.navigate_all('https://tanktrouble.com/')
    # 逐实例控制：
    cdp = farm.get_cdp(0)          # 实例 0 的 CDP 句柄
"""
import os
import time
import urllib.request
import json
import subprocess
import asyncio

try:  # 包内导入（python -m cdp_tools.edge_multi）
    from .platform_utils import find_browser, profile_base, no_sandbox_flags
except ImportError:  # 脚本目录直接导入（python edge_multi.py）
    from platform_utils import find_browser, profile_base, no_sandbox_flags

EDGE = find_browser()
PROFILE_BASE = profile_base()
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PORT_BASE = _env_int('TT_FARM_PORT_BASE', 9222)

class EdgeInstance:
    def __init__(self, index, port, profile_dir):
        self.index = index
        self.port = port
        self.profile_dir = profile_dir
        self.proc = None
        self.cdp = None  # 惰性导入：在 get_cdp 里 import cdp，避免循环依赖

    def launch(self):
        """在该实例端口上启动带 CDP 的浏览器。"""
        if not EDGE or not os.path.isfile(EDGE):
            raise RuntimeError('浏览器不可用（TT_BROWSER=%r）；请设置正确的可执行文件路径'
                               % EDGE)
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--new-window',
            *no_sandbox_flags(),
            'about:blank',
        ]
        try:
            self.proc = subprocess.Popen([EDGE] + args)
        except OSError as e:
            raise RuntimeError("启动浏览器失败: %s" % e)
        # 等待 CDP 端点就绪
        for _ in range(40):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/version', timeout=2) as r:
                    info = json.loads(r.read().decode())
                    return info
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f'Instance {self.index}: CDP not ready on port {self.port}')

    def is_ready(self):
        for host in ('127.0.0.1', '[::1]', 'localhost'):
            try:
                with urllib.request.urlopen(
                        f'http://{host}:{self.port}/json/version', timeout=2) as r:
                    return True
            except Exception:
                continue
        return False

    def get_cdp(self):
        """返回已连接的 CDP 句柄（惰性 import cdp 模块）。"""
        if self.cdp is None:
            from cdp import CDP
            self.cdp = CDP(port=self.port)
        return self.cdp

    def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc = None


class EdgeFarm:
    def __init__(self, count=1):
        self.instances = []
        for i in range(count):
            self.instances.append(EdgeInstance(
                index=i,
                port=PORT_BASE + i,
                profile_dir=os.path.join(PROFILE_BASE, str(i)),
            ))

    def launch_all(self):
        """启动所有实例，返回 index -> 浏览器信息 的字典。"""
        results = {}
        for inst in self.instances:
            results[inst.index] = inst.launch()
            print(f'[Farm] instance {inst.index} ready on port {inst.port}', flush=True)
        return results

    def get(self, index):
        return self.instances[index]

    def get_cdp(self, index):
        return self.instances[index].get_cdp()

    def close_all(self):
        for inst in self.instances:
            inst.close()

    @staticmethod
    def is_instance_running(port):
        for host in ('127.0.0.1', '[::1]', 'localhost'):
            try:
                with urllib.request.urlopen(
                        f'http://{host}:{port}/json/version', timeout=2):
                    return True
            except Exception:
                continue
        return False


if __name__ == '__main__':
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    farm = EdgeFarm(count=n)
    try:
        farm.launch_all()
        print(f'[{n}] instances running. Ports: {[i.port for i in farm.instances]}')
    finally:
        # 想保持常驻就注释掉下面几行；非交互/关闭 stdin 时也不会泄漏浏览器
        try:
            input('Press Enter to close all instances...')
        except (EOFError, RuntimeError, OSError, KeyboardInterrupt):
            pass
        farm.close_all()
