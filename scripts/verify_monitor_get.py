# =============================================================================
# TankTrouble AI Research Kit — 免责声明 / DISCLAIMER  [DISCLAIMER-STAMP]
# 仅供学习、教学与学术研究（AI/游戏交互接口、行为 AI、弹道与火控建模）使用。
# 与 TankTrouble 及其开发方无任何关联；不含任何游戏资源；
# 按 MIT 许可"原样"提供，不附带任何担保。详见 NOTICE / RISK-ASSESSMENT.md。
# 禁止用于公开在线对局作弊或任何违反官方社区规则的行为。
# For education & academic research only. No online public matches. No affiliation.
# =============================================================================
"""Lead 独立复核：直接从 game.png 检测墙线列/行，验证 state 坐标与渲染是否一致。

不需要 PIL（自解 PNG）。输出：
  * dark 像素按列的计数 → 竖直墙线中心；
  * dark 像素按行的计数 → 水平墙线中心；
  * 用"墙线中心 = 10m 网格"反解 world→screen，然后把 state 里坦克/收集物投影回画面，
    与颜色组件质心比较，给出残差（像素/米）。
用法：python scripts/verify_monitor_get.py <game.png> <analysis.json>
"""
import json
import math
import struct
import sys
import zlib


def read_png(path):
    raw = open(path, 'rb').read()
    assert raw[:8] == b'\x89PNG\r\n\x1a\n'
    pos, idat, w, h, bpp = 8, b'', None, None, None
    while pos < len(raw):
        ln = struct.unpack('>I', raw[pos:pos + 4])[0]
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        if typ == b'IHDR':
            w, h, bitd, color, comp, filt, inter = struct.unpack('>IIBBBBB', data)
            assert bitd == 8 and inter == 0 and color in (2, 6)
            bpp = 3 if color == 2 else 4
        elif typ == b'IDAT':
            idat += data
        elif typ == b'IEND':
            break
        pos += 12 + ln
    stream = zlib.decompress(idat)
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    off = 0
    for _ in range(h):
        f = stream[off]; off += 1
        line = bytearray(stream[off:off + stride]); off += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out[(_) * stride:(_ + 1) * stride] = line
        prev = line
    return w, h, bpp, bytes(out)


def main():
    png = sys.argv[1] if len(sys.argv) > 1 else r'D:\AI Agent\tt_monitor_box\game.png'
    ana = sys.argv[2] if len(sys.argv) > 2 else r'D:\AI Agent\tt_monitor_box\analysis.json'
    w, h, bpp, buf = read_png(png)
    d = json.load(open(ana, encoding='utf-8'))
    st = d['state']
    cw = d['game']['canvasRect']
    x0, y0 = int(cw['x']), int(cw['y'])
    x1, y1 = x0 + int(cw['w']), y0 + int(cw['h'])
    # 黑墙像素（dark 主题墙是近黑色）
    def dark(x, y):
        i = (y * w + x) * bpp
        return buf[i] < 70 and buf[i + 1] < 70 and buf[i + 2] < 70
    col = [0] * w
    row = [0] * h
    for y in range(y0, min(y1, h)):
        for x in range(x0, min(x1, w)):
            if dark(x, y):
                col[x] += 1
                row[y] += 1
    # 找竖直墙线：列计数局部峰
    def peaks(arr, lo, hi, thresh, min_gap=6):
        idx = [i for i in range(lo, hi) if arr[i] >= thresh]
        groups = []
        for i in idx:
            if groups and i - groups[-1][-1] <= min_gap:
                groups[-1].append(i)
            else:
                groups.append([i])
        best = []
        for g in groups:
            c = max(g, key=lambda i: arr[i])
            best.append((c, arr[c], sum(g) / len(g)))
        return best
    vpeaks = peaks(col, x0, min(x1, w), 120)
    hpeaks = peaks(row, y0, min(y1, h), 120)
    print('竖直墙线列（中心=像素, 峰计数, 组宽）:')
    for c, n, wid in vpeaks:
        print('   x=%d n=%d width=%.1f  world≈%.1fm (若10m网格)' % (c, n, wid, (c - vpeaks[0][0]) / 10.0))
    print('水平墙线行:')
    for c, n, wid in hpeaks:
        print('   y=%d n=%d width=%.1f' % (c, n, wid))
    if len(vpeaks) >= 2 and len(hpeaks) >= 2:
        # 假设：竖直线的第一条可能缺最左边界；水平线完整。用间距推 scale
        gaps_v = [vpeaks[i + 1][0] - vpeaks[i][0] for i in range(len(vpeaks) - 1)]
        gaps_h = [hpeaks[i + 1][0] - hpeaks[i][0] for i in range(len(hpeaks) - 1)]
        print('竖直间距:', gaps_v, '水平间距:', gaps_h)
        # 取中位间距 / 10m
        allg = sorted(gaps_v + gaps_h)
        s_med = allg[len(allg) // 2] / 10.0
        print('中位线间距 => %.2f px/m' % s_med)
        # 用水平线完整估计 oy（第一条=world0）
        sy = (hpeaks[-1][0] - hpeaks[0][0]) / 40.0
        oy = hpeaks[0][0]
        # 竖直线：若第一条不是 world0，则允许整格偏移 k=0..2
        print('\n== 检验：竖直线整体偏移 k 格时的坦克/收集物残差 ==')
        tanks = st['tanks']
        colls = st.get('collectibles') or []
        # 颜色组件质心（复用 analysis 里已检测到的坦克质心，避免重复实现）
        gt = d['game'].get('tanks') or []
        obs = []
        for e in gt:
            if e.get('matched') and e.get('hull'):
                obs.append((e['pid'], e['hull']['centroid']))
        for e in d['game'].get('extra_hull_components') or []:
            obs.append(('extra', e['centroid']))
        print('已检测坦克质心:', obs)
        # 已知 analysis: own 绿色 extra=(504.9,136.9)，opp purple=(644.65,351.09)
        best = None
        for k in range(0, 3):
            sx = 7.10
            ox = vpeaks[0][0] - k * (vpeaks[1][0] - vpeaks[0][0]) if len(vpeaks) > 1 else 397
            errs = []
            for (pid, cen), (wx, wy) in [
                    ((obs[0][0], obs[0][1]), (tanks[list(tanks)[0]]['x'], tanks[list(tanks)[0]]['y']))]:
                pass
            # 直接按两条坦克配对解
            cand = [('22841415', (504.9, 136.9)), ('22841416', (644.65, 351.09))]
            if all(p in tanks for p, _ in cand):
                (pa, ca), (pb, cb) = cand
                (x1w, y1w), (x2w, y2w) = (tanks[pa]['x'], tanks[pa]['y']), (tanks[pb]['x'], tanks[pb]['y'])
                if abs(x2w - x1w) > 1e-6 and abs(y2w - y1w) > 1e-6:
                    sx2 = (cb[0] - ca[0]) / (x2w - x1w)
                    sy2 = (cb[1] - ca[1]) / (y2w - y1w)
                    ox2 = ca[0] - sx2 * x1w
                    oy2 = ca[1] - sy2 * y1w
                    res = []
                    for c in colls:
                        pred = (ox2 + sx2 * c['x'], oy2 + sy2 * c['y'])
                        res.append((c, pred))
                    print('  用两辆坦克质心解: sx=%.3f sy=%.3f ox=%.1f oy=%.1f' % (sx2, sy2, ox2, oy2))
                    for c, pred in res:
                        print('     collectible world=(%.0f,%.0f) pred=(%.1f,%.1f)'
                              % (c['x'], c['y'], pred[0], pred[1]))
                    # 与墙线解比较
                    print('  墙线解: sx≈7.10, ox=%.1f (k=%d), sy=%.2f, oy=%.1f'
                          % (vpeaks[0][0] - k * 71, k, sy, oy))
        # 检查最左边界线是否在更靠左位置（找 x0..first_peak 之间的暗列峰）
        left_cols = [(x, col[x]) for x in range(x0, vpeaks[0][0]) if col[x] > 50]
        print('\n首条竖线左侧的暗列候选(>50):', left_cols[:20])


if __name__ == '__main__':
    main()
