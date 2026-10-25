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

检查 `cdp_tools/platform_utils.py` 的各分支与端口环境变量：
  1. 三个平台的浏览器候选列表是否非空；
  2. 当前平台能否找到浏览器（找不到不算失败，但会提示 TT_BROWSER）；
  3. profile 目录在三个平台下取值是否合理（Linux=XDG cache、macOS=~/Library/Caches）；
  4. `TT_BROWSER` / `TT_CDP_PROFILE_BASE` / `TT_CHROME_NO_SANDBOX` 环境变量覆盖；
  5. `list_python_processes()` 能否看到当前进程；
  6. `TT_CDP_PORT` 是否能被 `ego_browser.discover()` 采用（有 CDP 在跑时）。

用法：python scripts/selftest_platform.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'cdp_tools'))

import platform_utils as pu  # noqa: E402

results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print('  %s %-46s %s' % ('✅' if cond else '❌', name, detail), flush=True)


def main():
    print('[1] 平台识别')
    check('sys.platform', True, sys.platform)
    check('is_windows/is_macos/is_linux 至少一个为真',
          pu.is_windows() or pu.is_macos() or pu.is_linux(),
          'win=%s mac=%s linux=%s' % (pu.is_windows(), pu.is_macos(), pu.is_linux()))

    print('[2] 三平台浏览器候选')
    for plat in ('win32', 'darwin', 'linux'):
        cands = pu.browser_candidates(plat)
        check('browser_candidates(%s)' % plat, len(cands) > 0, 'n=%d' % len(cands))

    print('[3] 当前平台浏览器')
    b = pu.find_browser()
    if b:
        check('find_browser()', True, b)
    else:
        print('  ⚠️  find_browser() 未找到浏览器（Linux 可设 TT_BROWSER=/usr/bin/chromium）',
              flush=True)

    print('[4] profile 目录')
    for plat, marker in (('win32', 'tt-cdp'), ('darwin', 'Library/Caches'),
                         ('linux', '.cache')):
        old = os.environ.pop('TT_CDP_PROFILE_BASE', None)
        try:
            p = pu.profile_base(plat)
        finally:
            if old is not None:
                os.environ['TT_CDP_PROFILE_BASE'] = old
        check('profile_base(%s)' % plat, p.endswith('tt-cdp'), p)

    print('[5] 环境变量覆盖')
    os.environ['TT_BROWSER'] = '/tmp/fake-browser'
    check('TT_BROWSER', pu.find_browser() == '/tmp/fake-browser', pu.find_browser())
    del os.environ['TT_BROWSER']
    os.environ['TT_CDP_PROFILE_BASE'] = '/tmp/tt-profile'
    check('TT_CDP_PROFILE_BASE', pu.profile_base() == '/tmp/tt-profile', pu.profile_base())
    del os.environ['TT_CDP_PROFILE_BASE']
    os.environ['TT_CHROME_NO_SANDBOX'] = '1'
    check('TT_CHROME_NO_SANDBOX', '--no-sandbox' in pu.no_sandbox_flags(),
          str(pu.no_sandbox_flags()))
    del os.environ['TT_CHROME_NO_SANDBOX']

    print('[6] 进程列表')
    procs = pu.list_python_processes()
    mine = [p for p in procs if p[0] == os.getpid()]
    check('list_python_processes() 能看到自己', bool(mine),
          'total=%d' % len(procs))

    print('[7] TT_CDP_PORT（有 CDP 时验证）')
    try:
        from ego_browser import discover
        os.environ['TT_CDP_PORT'] = '62202'
        d = discover(preferred=None)
        found = d.get('port')
        if found:
            check('discover() 采用 TT_CDP_PORT', found == 62202, str(d.get('port')))
        else:
            print('  ⚠️  当前没有 CDP 在 62202 上（跳过此项；设 TT_CDP_PORT=<端口> 即可）',
                  flush=True)
        del os.environ['TT_CDP_PORT']
    except Exception as e:
        print('  ⚠️  discover 测试失败: %s: %s' % (type(e).__name__, e), flush=True)

    bad = [r for r in results if not r[1]]
    print('\n结论: %s（%d 项检查，失败 %d）'
          % ('✅ PASS' if not bad else '❌ FAIL', len(results), len(bad)), flush=True)
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
