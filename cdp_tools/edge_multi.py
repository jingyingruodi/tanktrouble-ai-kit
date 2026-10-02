"""
TankTrouble 多实例训练管理器
============================
启动 N 个独立 Edge 实例（通过 --user-data-dir 各自独立 profile），
每个都有自己的 CDP 调试端口和独立的游戏登录会话。
支持针对真实游戏服务器的并行训练，互不冲突。

重要：--user-data-dir 路径【不能含空格】。
每个实例独立端口（9222, 9223, ...）和独立 profile 目录（C:\tt-cdp-0, ...）。

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

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE_BASE = r"C:\tt-cdp"       # 不能含空格！
PORT_BASE = 9222

class EdgeInstance:
    def __init__(self, index, port, profile_dir):
        self.index = index
        self.port = port
        self.profile_dir = profile_dir
        self.proc = None
        self.cdp = None  # 惰性导入：在 get_cdp 里 import cdp，避免循环依赖

    def launch(self):
        """在该实例端口上启动带 CDP 的 Edge。"""
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--new-window',
            'about:blank',
        ]
        self.proc = subprocess.Popen([EDGE] + args)
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
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/version', timeout=2) as r:
                return True
        except Exception:
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
                profile_dir=f'{PROFILE_BASE}-{i}',
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
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=2):
                return True
        except Exception:
            return False


if __name__ == '__main__':
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    farm = EdgeFarm(count=n)
    try:
        farm.launch_all()
        print(f'[{n}] instances running. Ports: {[i.port for i in farm.instances]}')
    finally:
        # 想保持常驻就注释掉下面这行
        input('Press Enter to close all instances...')
        farm.close_all()
