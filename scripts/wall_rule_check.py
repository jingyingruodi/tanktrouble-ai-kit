"""判定真实墙体规则：dump tiles，比较候选墙集合，用官方 projectile_path 物理探针分类真/假墙。"""
import asyncio, json, math, sys

sys.path.insert(0, r'D:\AI Agent\tt_bb_lowlevel6')
from ttprim import Prim

T = 10.0
HALF = 0.4


def edges_from_tiles(td, mw, mh, rule):
    """rule: 'A'=current; 'B'=present flags only; 'C'=present flags + outer border."""
    def key(x1, y1, x2, y2):
        a, b = (round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))
        return (a, b) if a <= b else (b, a)
    es = {}
    if rule in ('A', 'C'):
        for x1, y1, x2, y2 in ((0, 0, mw * T, 0), (0, mh * T, mw * T, mh * T),
                               (0, 0, 0, mh * T), (mw * T, 0, mw * T, mh * T)):
            es[key(x1, y1, x2, y2)] = (x1, y1, x2, y2)
    for x in range(mw):
        for y in range(mh):
            tile = td[x][y] if x < len(td) and y < len(td[x]) else [0, 0, 0]
            present, top, left = tile
            tx, ty = x * T, y * T
            if present:
                if top:
                    es[key(tx, ty, tx + T, ty)] = (tx, ty, tx + T, ty)
                if left:
                    es[key(tx, ty, tx, ty + T)] = (tx, ty, tx, ty + T)
            elif rule == 'A':
                for e in ((tx, ty, tx + T, ty), (tx, ty + T, tx + T, ty + T),
                          (tx, ty, tx, ty + T), (tx + T, ty, tx + T, ty + T)):
                    es[key(*e)] = e
    return es


async def main():
    p = Prim(); await p.connect()
    try:
        st = await p.state()
        me = await p.my_pid()
        geo = await p.cdp.eval("""(() => {
          const gc = GameManager.getGameController();
          const m = gc.getMaze ? gc.getMaze() : null;
          if (!m) return null;
          const mw = m.getWidth(), mh = m.getHeight();
          const td = m.getTiles();
          const out = [];
          for (let x=0;x<mw;x++) { out.push([]); for (let y=0;y<mh;y++) {
            const t = td[x] && td[x][y] ? td[x][y] : [0,0,0];
            out[x].push([t[0]?1:0, t[1]?1:0, t[2]?1:0]);
          } }
          return {mw, mh, td: out};
        })()""")
        mw, mh = geo['mw'], geo['mh']
        sets = {r: edges_from_tiles(geo['td'], mw, mh, r) for r in 'ABC'}
        print('maze %dx%d  rules:' % (mw, mh), {r: len(v) for r, v in sets.items()})

        # 物理探针：每条候选边取中点，两侧各 2.5m 起点沿法线穿过
        all_edges = {}
        for r, es in sets.items():
            for k, e in es.items():
                all_edges[k] = e

        def first_seg(path, origin):
            if not path or len(path) < 2:
                return 999.0
            q = path[1]
            return math.hypot(q['x'] - origin[0], q['y'] - origin[1])

        async def probe(origin, ang):
            r = await p.projectile_path(me, ang, max_bounces=2, max_length=30,
                                        from_xy=origin, from_muzzle=False)
            if (r.get('bounces') or 0) > 0:
                d = first_seg(r.get('path') or [], origin)
                if d < 6.0:
                    return True
            return False

        async def real_edge(e):
            x1, y1, x2, y2 = e
            mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if abs(x2 - x1) < 1e-6:  # vertical
                for sx, ang in ((mx - 2.5, 0.0), (mx + 2.5, 180.0)):
                    if await probe((sx, my), ang):
                        return True
            else:  # horizontal
                for sy, ang in ((my - 2.5, 90.0), (my + 2.5, -90.0)):
                    if await probe((mx, sy), ang):
                        return True
            return False

        truth = {}
        for k, e in all_edges.items():
            truth[k] = await real_edge(e)
        real = sum(truth.values()); ph = len(truth) - real
        print('physics truth over union: real=%d phantom=%d total=%d' % (real, ph, len(truth)))
        for r, es in sets.items():
            tp = sum(1 for k in es if truth[k]); fp = sum(1 for k in es if not truth[k])
            fn = sum(1 for k in truth if truth[k] and k not in es)
            prec = tp / max(1, tp + fp); rec = tp / max(1, tp + fn)
            print('rule %s: edges=%3d  real=%3d phantom=%3d  precision=%.3f recall=%.3f' % (r, len(es), tp, fp, prec, rec))
        # 规则 B 的幽灵墙样例
        ghostsB = [k for k in sets['B'] if not truth[k]]
        print('rule B phantom examples:', ghostsB[:6])
        out = {'roundId': st.get('roundId'), 'theme': st.get('theme'), 'maze': [mw, mh],
               'tiles': geo['td'],
               'counts': {r: len(v) for r, v in sets.items()},
               'truth': {str(k): truth[k] for k in truth},
               'ruleB_phantom': [list(k) for k in ghostsB]}
        open(r'D:\AI Agent\tanktrouble_ai\scripts\scratch\wall_rule_check.json', 'w', encoding='utf-8').write(
            json.dumps(out, ensure_ascii=False))
    finally:
        await p.close()


asyncio.run(main())
