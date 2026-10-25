"""穷举地图内所有 10m 候选边，用官方 projectile_path 逐条判真/假，与 state.walls 求 P/R。"""
import asyncio, json, math, sys

sys.path.insert(0, r'D:\AI Agent\tt_bb_lowlevel6')
from ttprim import Prim


async def main():
    p = Prim(); await p.connect()
    try:
        st = await p.state()
        me = await p.my_pid()
        mw = st['maze']['w']; mh = st['maze']['h']; T = 10.0
        walls = {(round(w['x1'], 3), round(w['y1'], 3), round(w['x2'], 3), round(w['y2'], 3)) for w in st['walls']}

        def norm(x1, y1, x2, y2):
            return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

        cand = set()
        for x in range(mw):
            for y in range(mh):
                tx, ty = x * T, y * T
                cand.add(norm(tx, ty, tx + T, ty))       # top edge
                cand.add(norm(tx, ty, tx, ty + T))       # left edge
        cand.add(norm(0, mh * T, mw * T, mh * T))        # bottom border
        cand.add(norm(mw * T, 0, mw * T, mh * T))        # right border
        cand = {norm(*c) for c in cand}

        async def real_edge(e):
            x1, y1, x2, y2 = e
            mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            if abs(x2 - x1) < 1e-6:
                probes = ((mx - 2.5, my, 0.0), (mx + 2.5, my, 180.0))
            else:
                probes = ((mx, my - 2.5, 90.0), (mx, my + 2.5, -90.0))
            for sx, sy, ang in probes:
                if not (0 <= sx <= mw * T and 0 <= sy <= mh * T):
                    continue
                r = await p.projectile_path(me, ang, max_bounces=2, max_length=30,
                                            from_xy=(sx, sy), from_muzzle=False)
                if (r.get('bounces') or 0) > 0 and len(r.get('path') or []) > 1:
                    q = r['path'][1]
                    if math.hypot(q['x'] - sx, q['y'] - sy) < 6.0:
                        return True
            return False

        truth = {}
        for e in sorted(cand):
            truth[e] = await real_edge(e)
        real = {e for e, v in truth.items() if v}
        tp = len(walls & real); fp = len(walls - real); fn = len(real - walls)
        print('maze=%dx%d candidates=%d physical_real=%d state_walls=%d' % (mw, mh, len(cand), len(real), len(walls)))
        print('TP=%d FP=%d FN=%d  precision=%.4f recall=%.4f' % (tp, fp, fn, tp / max(1, tp + fp), tp / max(1, tp + fn)))
        if fp:
            print('FP:', sorted(walls - real)[:5])
        if fn:
            print('FN:', sorted(real - walls)[:5])
        out = {'roundId': st.get('roundId'), 'maze': [mw, mh],
               'candidates': len(cand), 'physical_real': len(real), 'state_walls': len(walls),
               'tp': tp, 'fp': fp, 'fn': fn,
               'precision': tp / max(1, tp + fp), 'recall': tp / max(1, tp + fn),
               'phantom': sorted(walls - real), 'missing': sorted(real - walls)}
        open(r'D:\AI Agent\tanktrouble_ai\scripts\scratch\wall_exhaustive.json', 'w', encoding='utf-8').write(
            json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        await p.close()


asyncio.run(main())
