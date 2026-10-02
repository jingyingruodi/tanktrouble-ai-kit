"""
dsh-ego-browser 接入层 —— 动态 CDP 端口发现
============================================
dsh-ego-browser（DSH 的浏览器插件）用 Edge 启动一个**常驻单例浏览器**：
  - 持久 profile（登录态跨重启保留），不会像后台标签页那样被冻结
  - CDP 端口由系统随机分配（`--remote-debugging-port=0`），真实端口写在其
    state 目录的 `browser.json` 里：
        {"port":51109,"wsUrl":"ws://127.0.0.1:51109/devtools/browser/...",
         "pid":25780,"binary":"...msedge.exe","headless":false,
         "profileDir":"...\\ego-lite-linux\\profile"}

因此本模块把"端口"变成一个可自动解析的值，项目里所有脚本都可以传 `auto`：

    from ego_browser import resolve_port, discover
    port = resolve_port('auto')      # 先找 ego 浏览器，再兜底扫描 9222-9231
    print(discover())                # {port, source:'ego'|'edge', browser, pages,...}

对项目自带 Edge（launch_cdp.py，9222+）同样适用——发现逻辑是超集：
`browser.json` 找不到/失效时回退到端口扫描。

命令行自检：
    python cdp_tools/ego_browser.py
"""
import json
import os
import urllib.request

# ego 浏览器 state 目录候选（不同 runtime 命名/平台）
STATE_DIR_NAMES = ('ego-lite-linux', 'ego-lite', 'ego-linux')
DEFAULT_SCAN_START = 9222
DEFAULT_SCAN_END = 9231          # 含 9231
GAME_URL_SUBSTR = 'tanktrouble.com'


def state_dirs():
    """返回 ego 浏览器 state 目录候选列表（带环境变量覆盖）。"""
    dirs = []
    env = os.environ.get('EGO_BROWSER_STATE_DIR')
    if env:
        dirs.append(env)
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
    for name in STATE_DIR_NAMES:
        dirs.append(os.path.join(base, name))
    return dirs


def read_browser_json():
    """读取 ego 浏览器运行信息；未运行返回 None。"""
    for d in state_dirs():
        path = os.path.join(d, 'browser.json')
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue
        if isinstance(data.get('port'), int) and data['port'] > 0:
            data['_state_dir'] = d
            return data
    return None


def http_json(port, path='/json/version', timeout=1.0):
    """GET 一个 CDP HTTP 端点，返回解析后的 JSON；失败返回 None。"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def probe(port, timeout=1.0):
    """端口上是否有可用的 CDP；返回 version 信息或 None。"""
    return http_json(port, '/json/version', timeout=timeout)


def list_pages(port, timeout=1.0):
    """列出该 CDP 端口的所有 page 目标。"""
    targets = http_json(port, '/json', timeout=timeout)
    if not isinstance(targets, list):
        return []
    return [t for t in targets if t.get('type') == 'page']


def find_game_page(port, url_substr=GAME_URL_SUBSTR):
    """找一个 URL 含 url_substr 的页面；找不到返回 None。"""
    for t in list_pages(port):
        if url_substr in (t.get('url') or ''):
            return t
    return None


def discover(preferred=None, scan_start=DEFAULT_SCAN_START, scan_end=DEFAULT_SCAN_END,
             timeout=1.0):
    """
    解析真实可用的 CDP 端口。

    优先级：
      1. preferred（显式传入的端口/数字字符串）能连通就用它
      2. ego 浏览器 browser.json 里的端口（source='ego'）
      3. 依次扫描 scan_start..scan_end（source='edge'）
    返回 dict：{port, source, browser, info|None, pages|[], game_page|None, error|None}
    找不到时 port=None。
    """
    result = {'port': None, 'source': None, 'browser': None, 'info': None,
              'pages': [], 'game_page': None, 'error': None}

    def fill(port, source, extra=None):
        info = probe(port, timeout=timeout)
        if not info:
            return False
        pages = list_pages(port, timeout=timeout)
        result.update(port=port, source=source, info=info,
                      browser=info.get('Browser'),
                      pages=[{'id': p.get('id'), 'url': p.get('url'), 'title': p.get('title')}
                             for p in pages],
                      game_page=next((p for p in pages
                                      if GAME_URL_SUBSTR in (p.get('url') or '')), None))
        if extra:
            result.update(extra)
        return True

    if preferred not in (None, '', 'auto'):
        try:
            port = int(preferred)
        except (TypeError, ValueError):
            port = None
        if port and fill(port, 'explicit'):
            return result

    bj = read_browser_json()
    if bj and fill(bj['port'], 'ego', {'state_dir': bj.get('_state_dir'),
                                       'pid': bj.get('pid'),
                                       'headless': bj.get('headless'),
                                       'profile_dir': bj.get('profileDir')}):
        return result

    for port in range(scan_start, scan_end + 1):
        if fill(port, 'edge'):
            return result

    result['error'] = ('未发现可用 CDP 端口：ego 浏览器未运行'
                       + ('（browser.json 端口 %s 不通）' % bj['port'] if bj else '（无 browser.json）')
                       + f'，且 {scan_start}-{scan_end} 无监听')
    return result


def resolve_port(value=None, **kw):
    """把 None/'auto'/端口 统一解析为 int 端口；失败抛 RuntimeError。"""
    if isinstance(value, int):
        if probe(value):
            return value
        info = discover(preferred=value, **kw)
        if info['port']:
            return info['port']
        raise RuntimeError(info['error'] or f'端口 {value} 不可用')
    info = discover(preferred=value, **kw)
    if info['port']:
        return info['port']
    raise RuntimeError(info['error'])


def describe(info):
    """把 discover() 结果格式化成一行人类可读文本。"""
    if not info.get('port'):
        return '[cdp] ' + str(info.get('error'))
    bits = [f'[cdp] port={info["port"]} source={info["source"]} browser={info["browser"]}']
    if info.get('pid'):
        bits.append(f'pid={info["pid"]}')
    if info.get('profile_dir'):
        bits.append(f'profile={info["profile_dir"]}')
    if info.get('game_page'):
        bits.append('game=yes')
    return ' '.join(bits)


def main():
    info = discover()
    print(describe(info), flush=True)
    for p in info.get('pages', []):
        print('   page %s  %s' % ((p['id'] or '')[:16], (p['url'] or '')[:80]), flush=True)
    bj = read_browser_json()
    print('browser.json: %s' % (bj if bj else '(无 — ego 浏览器未运行)'), flush=True)
    return 0 if info.get('port') else 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
