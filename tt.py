#!/usr/bin/env python
"""
TankTrouble AI — CDP 自动化统一入口
====================================
单命令管理：启动 Edge 实例、列出 CDP 窗口、连接测试、运行测试脚本。

用法：
  python tt.py farm 2          # 启动 2 个 Edge CDP 实例 (端口 9222, 9223)
  python tt.py list            # 列出所有活跃 CDP 窗口及其 id
  python tt.py scan            # 扫描并定位所有在游戏中的窗口
  python tt.py info [port]     # 查看某实例的窗口信息 (默认 9222)
  python tt.py test [port] [idx]  # 运行接口测试 (SET/GET 闭环)
  python tt.py suicide [port] [idx]  # 运行自杀脚本
  python tt.py ping [port]     # 检查实例连通性 + 游戏状态
  python tt.py local [players] [port]  # 自动开 Local 局 + 绑定控制角色

端口参数可写 'auto'：先找 dsh-ego-browser（browser.json 动态端口），
再回退扫描 9222-9231。见 cdp_tools/ego_browser.py。
"""
import asyncio
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, 'cdp_tools'))


def resolve_port(value, default=9222):
    """端口解析：'auto'/None -> 自动发现（ego 浏览器优先），失败回退 default。"""
    if value in (None, '', 'auto'):
        try:
            from ego_browser import resolve_port as _rp
            return _rp('auto')
        except Exception as e:
            print(f'[port] auto 发现失败（{e}），回退 {default}')
            return default
    return int(value)



def cmd_farm(count):
    """启动 N 个 Edge 实例。"""
    from edge_multi import EdgeFarm
    farm = EdgeFarm(count=int(count))
    info = farm.launch_all()
    for idx, browser in info.items():
        print(f'[farm] instance {idx}: port={farm.instances[idx].port} profile={farm.instances[idx].profile_dir}')
    print(f'[farm] {count} instances running. Ports: {[i.port for i in farm.instances]}')


def cmd_list():
    """List all running CDP instances on common ports + all windows."""
    import ctypes
    print('[list] Scanning ports 9222-9231...')
    for port in range(9222, 9232):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as r:
                info = json.loads(r.read().decode())
                print(f'  port {port}: {info["Browser"]}')
                # 列出页面
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=1) as r2:
                        pages = json.loads(r2.read().decode())
                        for p in pages:
                            if p['type'] == 'page':
                                print(f'    [page id={p["id"][:16]}...] {p["url"][:60]}')
                except Exception:
                    pass
        except Exception:
            pass


def cmd_info(port):
    """Show detailed info about one instance's windows."""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=2) as r:
            targets = json.loads(r.read().decode())
        print(f'[info] Instance on port {port}:')
        for t in targets:
            print(f'  type={t["type"]:<15} id={t["id"][:24]}...')
            if t['type'] == 'page':
                print(f'    url: {t["url"]}')
    except Exception as e:
        print(f'[info] error: {e}')


async def cmd_ping(port):
    """Check connectivity + game state of an instance."""
    from cdp import CDP
    port = resolve_port(port)
    cdp = CDP(port=int(port))
    try:
        page = await cdp.connect_page('tanktrouble.com', timeout=5)
        print(f'[ping] connected to: {page["url"]}')
        try:
            gm = await cdp.eval('typeof GameManager')
            print(f'[ping] GameManager: {gm}')
            if gm == 'object':
                gc = await cdp.eval('!!(GameManager.getGameController()||GameManager.gameController)')
                print(f'[ping] gameController: {gc}')
                if gc:
                    tanks = await cdp.eval('Object.keys((GameManager.getGameController()||GameManager.gameController).getTanks()||{}).length')
                    print(f'[ping] tanks on field: {tanks}')
                    state = await cdp.eval('(GameManager.phaserInstance.state ? GameManager.phaserInstance.state.current : "?")')
                    print(f'[ping] phaser state: {state}')
        except Exception as e:
            print(f'[ping] eval error: {str(e)[:100]}')
    except Exception as e:
        print(f'[ping] connection error: {str(e)[:100]}')


async def cmd_test(port, idx):
    """Run interface test."""
    sys.path.insert(0, os.path.join(BASE, 'scripts'))
    import test_interface
    return await test_interface.main(port=resolve_port(port), tank_idx=int(idx) if idx else 0)


async def cmd_suicide(port, idx):
    """Run suicide script."""
    sys.path.insert(0, os.path.join(BASE, 'scripts'))
    import suicide_script
    return await suicide_script.main(port=resolve_port(port), tank_idx=int(idx) if idx else 0)


async def cmd_local(players, port):
    """Start a local game and bind input sets (角色绑定)."""
    sys.path.insert(0, os.path.join(BASE, 'scripts'))
    import start_local_game
    info = await start_local_game.ensure_local_game(
        resolve_port(port), players=int(players) if players else 2)
    print('[local] port=%s pids=%s roles=%s'
          % (info['port'], info['pids'], info['roles']), flush=True)
    return 0 if info['roles'] else 1


async def cmd_scan():
    """Scan all CDP instances, find windows actually in a TankTrouble game."""
    from cdp import CDP
    print('[scan] Scanning ports 9222-9241 for game instances...')
    for port in range(9222, 9242):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as r:
                info = json.loads(r.read().decode())
            # 检查每个页面目标是否有游戏状态
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=1) as r2:
                targets = json.loads(r2.read().decode())
            for t in targets:
                if t['type'] != 'page':
                    continue
                print(f'[scan] port {port} id={t["id"][:16]}... url={t["url"][:50]}')
                if 'tanktrouble' in t['url']:
                    try:
                        cdp = CDP(port=port)
                        page = await cdp.connect_page('tanktrouble.com', timeout=3)
                        gm = await asyncio.wait_for(cdp.eval('typeof GameManager'), timeout=3)
                        in_game = False
                        tanks = 0
                        state = '?'
                        if gm == 'object':
                            gc_ok = await asyncio.wait_for(cdp.eval('!!(GameManager.getGameController()||GameManager.gameController)'), timeout=3)
                            if gc_ok:
                                tanks = await asyncio.wait_for(cdp.eval('Object.keys((GameManager.getGameController()||GameManager.gameController).getTanks()||{}).length'), timeout=3)
                                state = await asyncio.wait_for(cdp.eval('(GameManager.phaserInstance.state ? GameManager.phaserInstance.state.current : "?")'), timeout=3)
                                in_game = state == 'Game'
                        print(f'        -> GameManager={gm} inGame={in_game} tanks={tanks} state={state}')
                    except Exception as e:
                        print(f'        -> check err: {str(e)[:80]}')
        except Exception:
            pass


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]
    if cmd == 'farm':
        cmd_farm(args[1] if len(args) > 1 else '2')
    elif cmd == 'list':
        cmd_list()
    elif cmd == 'scan':
        asyncio.run(cmd_scan())
    elif cmd == 'info':
        cmd_info(args[1] if len(args) > 1 else '9222')
    elif cmd == 'ping':
        asyncio.run(cmd_ping(args[1] if len(args) > 1 else 'auto'))
    elif cmd == 'test':
        sys.exit(asyncio.run(cmd_test(args[1] if len(args) > 1 else 'auto',
                                      args[2] if len(args) > 2 else '0')))
    elif cmd == 'suicide':
        sys.exit(asyncio.run(cmd_suicide(args[1] if len(args) > 1 else 'auto',
                                         args[2] if len(args) > 2 else '0')))
    elif cmd == 'local':
        sys.exit(asyncio.run(cmd_local(args[1] if len(args) > 1 else '2',
                                       args[2] if len(args) > 2 else 'auto')))
    else:
        print(f'Unknown command: {cmd}')
        print(__doc__)


if __name__ == '__main__':
    main()
