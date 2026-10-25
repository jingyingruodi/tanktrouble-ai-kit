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
"""跨平台适配自检（Linux / macOS / Windows）。

检查 `cdp_tools/platform_utils.py` 与 `cdp_tools/ego_browser.py` 的关键分支：
  1. 平台识别；
  2. 三个平台的浏览器候选列表非空；
  3. 当前平台能否找到浏览器（找不到不算失败，提示 TT_BROWSER）；
  4. profile 目录（三平台）+ `TT_CDP_PROFILE_BASE` 覆盖 + XDG 相对路径被忽略；
  5. `TT_BROWSER` 覆盖 + `launch_cdp._check_browser()` 对不存在路径报错；
  6. `TT_CHROME_NO_SANDBOX=1` → `--no-sandbox`；
  7. 进程枚举能看到自己 + kill 精确匹配（不误杀含关键词的非目标进程）；
  8. ego state 目录优先 XDG_STATE_HOME（Linux）；
  9. `TT_CDP_PORT` 生效（`resolve_port('auto')` 采用它；需有 CDP 在跑，否则跳过）。

用法：python scripts/selftest_platform.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'cdp_tools'))
sys.path.insert(0, ROOT)

import platform_utils as pu  # noqa: E402
import ego_browser as eb  # noqa: E402

results = []
skipped = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print('  %s %-52s %s' % ('✅' if cond else '❌', name, detail), flush=True)


def skip(name, why):
    skipped.append(name)
    print('  ⏭️  %-52s %s' % (name, why), flush=True)


def main():
    print('[1] 平台识别')
    check('is_windows/is_macos/is_linux 至少一个为真',
          pu.is_windows() or pu.is_macos() or pu.is_linux(), sys.platform)

    print('[2] 三平台浏览器候选')
    for plat in ('win32', 'darwin', 'linux'):
        cands = pu.browser_candidates(plat)
        check('browser_candidates(%s) 非空' % plat, len(cands) > 0, 'n=%d' % len(cands))

    print('[3] 当前平台浏览器')
    b = pu.find_browser()
    if b and os.path.exists(b):
        check('find_browser() 返回存在路径', True, b)
    elif b:
        try:
            import launch_cdp
            err = launch_cdp._check_browser(b)
            check('TT_BROWSER 指向不存在路径 → _check_browser 报错',
                  err is not None, str(err)[:70])
        except Exception as e:
            skip('find_browser()', '路径不存在且校验失败: %s' % e)
    else:
        skip('find_browser()', '未找到（Linux 可设 TT_BROWSER=/usr/bin/chromium）')

    print('[4] profile 目录（注入 env 断言三平台，避免宿主平台差异）')
    saved = {k: os.environ.get(k) for k in
             ('TT_CDP_PROFILE_BASE', 'LOCALAPPDATA', 'HOME', 'XDG_DATA_HOME')}
    for k in saved:
        os.environ.pop(k, None)
    try:
        os.environ['LOCALAPPDATA'] = r'C:\Users\u\AppData\Local'
        os.environ['HOME'] = '/home/u'
        p_win = pu.profile_base('win32')
        p_mac = pu.profile_base('darwin')
        p_lin = pu.profile_base('linux')
        check('profile_base(win32) 用 %LOCALAPPDATA%',
              p_win.startswith(r'C:\Users\u\AppData\Local'), p_win)
        check('profile_base(darwin) 用 ~/Library/Application Support',
              p_mac.startswith('/home/u/Library/Application Support'), p_mac)
        check('profile_base(linux) 用 ~/.local/share（XDG data）',
              p_lin.startswith('/home/u/.local/share') and p_lin.endswith('tt-cdp'), p_lin)
        os.environ['XDG_DATA_HOME'] = '/xdg/data'
        check('XDG_DATA_HOME 绝对路径生效', pu.profile_base('linux') == '/xdg/data/tt-cdp',
              pu.profile_base('linux'))
        os.environ['XDG_DATA_HOME'] = 'relative/data'
        check('XDG_DATA_HOME 相对路径被忽略',
              not pu.profile_base('linux').startswith('relative'), pu.profile_base('linux'))
        os.environ['TT_CDP_PROFILE_BASE'] = '/tmp/tt-profile'
        check('TT_CDP_PROFILE_BASE 覆盖', pu.profile_base() == '/tmp/tt-profile',
              pu.profile_base())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print('[5] 浏览器覆盖与可执行性校验')
    os.environ['TT_BROWSER'] = '/tmp/fake-browser'
    check('TT_BROWSER 覆盖', pu.find_browser() == '/tmp/fake-browser', pu.find_browser())
    try:
        import launch_cdp
        err = launch_cdp._check_browser('/tmp/fake-browser')
        check('launch_cdp._check_browser(不存在路径) 报错', err is not None, str(err)[:60])
    except Exception as e:
        print('  ⚠️  导入 launch_cdp 失败: %s: %s' % (type(e).__name__, e), flush=True)
    del os.environ['TT_BROWSER']

    print('[6] Linux root no-sandbox')
    os.environ['TT_CHROME_NO_SANDBOX'] = '1'
    check('TT_CHROME_NO_SANDBOX=1', '--no-sandbox' in pu.no_sandbox_flags(),
          str(pu.no_sandbox_flags()))
    del os.environ['TT_CHROME_NO_SANDBOX']

    print('[7] 进程枚举与 kill 精确匹配')
    procs = pu.list_processes()
    if procs is None:
        check('list_processes() 枚举成功', False, '枚举返回 None（ps/PowerShell 不可用）')
    else:
        mine = [p for p in procs if p[0] == os.getpid()]
        check('list_processes() 能看到自己', bool(mine), 'total=%d' % len(procs))
    names = ('monitor_server.py', 'playback_server.py')
    check('精确匹配: python x/monitor_server.py',
          pu.script_token_match(['python', '/repo/scripts/monitor_server.py'], names))
    check('精确匹配: python -X dev x/monitor_server.py',
          pu.script_token_match(['python', '-X', 'dev',
                                 '/repo/scripts/monitor_server.py'], names))
    check('不误杀: vim python_monitor_server_notes.txt',
          not pu.script_token_match(['vim', 'python_monitor_server_notes.txt'], names))
    check('不误杀: tail -f python/monitor_server.log',
          not pu.script_token_match(['tail', '-f', 'python/monitor_server.log'], names))

    print('[8] ego state 目录（Linux 优先 XDG_STATE_HOME）')
    env = {'LOCALAPPDATA': '/win/local', 'XDG_STATE_HOME': '/xdg/state',
           'XDG_DATA_HOME': '/xdg/data', 'HOME': '/home/u'}
    lin_dirs = eb.state_dirs(platform='linux', env=env)
    check('state_dirs(linux) 含 XDG_STATE_HOME',
          any(d.startswith('/xdg/state') for d in lin_dirs), str(lin_dirs[:2]))
    check('state_dirs(linux) 含 XDG_DATA_HOME fallback',
          any(d.startswith('/xdg/data') for d in lin_dirs), str(lin_dirs[3:5]))
    check('state_dirs(win32) 用 LOCALAPPDATA',
          any(d.startswith('/win/local') for d in eb.state_dirs(platform='win32', env=env)),
          str(eb.state_dirs(platform='win32', env=env)[:1]))

    print('[9] auto 发现与 TT_CDP_PORT（真 discover 路径）')
    live = None
    cand = os.environ.get('TT_CDP_PORT')
    if cand and cand.isdigit() and eb.probe(int(cand)):
        live = int(cand)
    if live is None:
        bj = eb.read_browser_json()          # ego 浏览器（Linux 常见路径）
        if bj and eb.probe(bj['port']):
            live = bj['port']
    if live is None:
        for port in list(range(9222, 9232)) + [62202]:
            if eb.probe(port):
                live = port
                break
    if live is None:
        skip('auto 采用 TT_CDP_PORT', '当前没有 CDP 在跑')
    else:
        os.environ['TT_CDP_PORT'] = str(live)
        try:
            d = eb.discover(preferred='auto')
            check('discover(auto) 采用 TT_CDP_PORT',
                  d.get('port') == live and d.get('source') == 'explicit',
                  'port=%s source=%s' % (d.get('port'), d.get('source')))
        finally:
            del os.environ['TT_CDP_PORT']
        d2 = eb.discover(preferred='auto')
        check('discover(auto) 无环境变量也能解析端口', d2.get('port') is not None,
              'port=%s source=%s' % (d2.get('port'), d2.get('source')))

    print('[10] 模块无重复定义')
    import ast as _ast
    pu_src = open(os.path.join(ROOT, 'cdp_tools', 'platform_utils.py'),
                  encoding='utf-8').read()
    _tree = _ast.parse(pu_src)
    _fns = [n.name for n in _tree.body if isinstance(n, _ast.FunctionDef)]
    _dups = sorted({n for n in _fns if _fns.count(n) > 1})
    check('platform_utils 无重复 def', not _dups,
          (', '.join(_dups) if _dups else '%d 个函数' % len(_fns)))

    bad = [r for r in results if not r[1]]
    if skipped:
        print('  跳过项: %s' % ', '.join(skipped), flush=True)
    print('\n结论: %s（%d 项检查，失败 %d，跳过 %d）'
          % ('✅ PASS' if not bad else '❌ FAIL', len(results), len(bad), len(skipped)),
          flush=True)
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
