"""Find the CORRECT way to read the maze walls (tile flags are wrong) + keep-alive + leave.

Compares:
  A. tile arrays (raw)                     -- what we wrongly used
  B. pathfinder connectivity (authoritative) -- getShortestPathWithGraph per neighbour pair
  C. rendered wall sprites (visual truth)
Also dumps the maze API surface to find wall-specific getters.

Usage: python scripts/verify_maze_walls.py auto [--leave]
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from cdp import CDP                      # noqa: E402
from ego_browser import resolve_port     # noqa: E402
from start_online_game import leave_online, state  # noqa: E402
from tankctl import TankCtl              # noqa: E402

WALLS_JS = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const st = GameManager.phaserInstance.state.getCurrentState();
  const maze = gc.getMaze ? gc.getMaze() : gc.roundController.model.getMaze();
  const out = {w: maze.getWidth(), h: maze.getHeight()};
  // 1) maze 与 tile 的 API 面貌（找墙体专用 getter）
  out.mazeProto = Object.getOwnPropertyNames(Object.getPrototypeOf(maze))
    .filter(n => /wall|pass|tile|neigh|edge|graph|open/i.test(n));
  const tiles = maze.getTiles();
  const t0 = tiles[1] && tiles[1][1];
  out.tileProto = t0 ? Object.getOwnPropertyNames(Object.getPrototypeOf(t0)) : null;
  out.tileGetters = {};
  if (t0) for (const n of out.tileProto) {
    if (!/^(get|is|has)/.test(n)) continue;
    try { const f = t0[n]; if (typeof f === 'function' && f.length === 0) out.tileGetters[n] = f.call(t0); } catch (e) {}
  }
  // 2) 原始 tile 数组（全部）
  out.tiles = [];
  for (let x = 0; x < out.w; x++) { const col = [];
    for (let y = 0; y < out.h; y++) { const t = tiles[x] ? tiles[x][y] : null;
      col.push(t ? Array.prototype.slice.call(t) : null); }
    out.tiles.push(col); }
  // 3) 寻路连通性：相邻格能否一步直达（权威）
  const conn = {right: [], down: []};         // conn.right[x][y] = 与右邻格直连?
  for (let x = 0; x < out.w; x++) { conn.right.push([]); conn.down.push([]); }
  const direct = (a, b) => { try { const p = maze.getShortestPathWithGraph({x:a[0],y:a[1]},{x:b[0],y:b[1]});
    return !!(p && p.length <= 2); } catch (e) { return null; } };
  for (let x = 0; x < out.w; x++) for (let y = 0; y < out.h; y++) {
    conn.right[x][y] = (x + 1 < out.w) ? direct([x,y],[x+1,y]) : null;
    conn.down[x][y]  = (y + 1 < out.h) ? direct([x,y],[x,y+1]) : null;
  }
  out.conn = conn;
  // 4) 渲染墙体精灵
  const grp = st.mazeWallGroup;
  out.wallSprites = grp ? grp.children.map(c => ({x: Math.round(c.x*100)/100, y: Math.round(c.y*100)/100,
    w: c.width, h: c.height, angle: c.angle, visible: c.visible, key: (typeof c.key === 'string') ? c.key : 'atlas'})) : null;
  return JSON.stringify(out);
})()"""


def ascii(w, h, blocked):
    return [''.join('#' if (x, y) in blocked else '.' for x in range(w)) for y in range(h)]


async def main():
    ap = argparse.ArgumentParser(description='正确的迷宫墙体读取方式')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--leave', action='store_true')
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)
    st = await state(cdp)
    if not (st.get('gameId') and st.get('tanks')):
        print('[walls] 不在房间，退出', flush=True)
        return 1
    ctl = TankCtl(port=port)
    await ctl.connect()
    pid = list(await ctl.tanks())[0]

    d = json.loads(await ctl.cdp.eval(WALLS_JS))
    await ctl.set_input(pid, right=True, hold=0.55)      # 保活（防挂机）
    w, h = d['w'], d['h']
    print('[walls] 迷宫 %d×%d' % (w, h), flush=True)
    print('[walls] maze 相关 API: %s' % d.get('mazeProto'), flush=True)
    print('[walls] tile 相关 API: %s' % d.get('tileProto'), flush=True)
    print('[walls] tile 零参 getter: %s' % json.dumps(d.get('tileGetters'), ensure_ascii=False), flush=True)
    print('\n[walls] 原始 tile 数组（前 3 列）:', flush=True)
    for x in range(min(3, w)):
        print('   x=%d: %s' % (x, json.dumps(d['tiles'][x], ensure_ascii=False)), flush=True)

    # B. 寻路连通性 → 墙
    conn = d['conn']
    blocked_path = set()
    for x in range(w):
        for y in range(h):
            r = conn['right'][x][y]
            dn = conn['down'][x][y]
            if r is False:
                blocked_path.add((x, y))          # 右邻不通 → 该格右侧有墙（视觉上常表现为该格及右侧）
            if dn is False:
                blocked_path.add((x, y))
    print('\n[walls] B. 由寻路连通性推出的"有墙格"占用:', flush=True)
    for r in ascii(w, h, blocked_path):
        print('      ' + r, flush=True)

    # C. 渲染墙体精灵 → 用像素/米换算（20px = 1m）
    blocked_vis = set()
    for s in (d.get('wallSprites') or []):
        blocked_vis.add((int((s['x'] / 20.0) // 10), int((s['y'] / 20.0) // 10)))
    print('\n[walls] C. 由渲染精灵推出的"有墙格"占用:', flush=True)
    for r in ascii(w, h, blocked_vis):
        print('      ' + r, flush=True)

    print('\n[walls] 寻路说"直连"的相邻对数=%d，说"需绕行"的=%d'
          % (sum(1 for x in range(w) for y in range(h) if conn['right'][x][y] is True)
             + sum(1 for x in range(w) for y in range(h) if conn['down'][x][y] is True),
             sum(1 for x in range(w) for y in range(h) if conn['right'][x][y] is False)
             + sum(1 for x in range(w) for y in range(h) if conn['down'][x][y] is False)), flush=True)
    print('[walls] 渲染精灵 %d 个，样例 %s'
          % (len(d.get('wallSprites') or []), json.dumps((d.get('wallSprites') or [])[:3], ensure_ascii=False)),
          flush=True)
    with open(os.path.join(HERE, 'scratch', 'maze_walls_dump.json'), 'w', encoding='utf-8') as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    print('[walls] 原始 dump → scripts/scratch/maze_walls_dump.json', flush=True)

    if args.leave:
        lv = await leave_online(cdp)
        print('[walls] 退房: %s' % json.dumps(lv, ensure_ascii=False), flush=True)
    await cdp.cdp.close() if hasattr(cdp, 'cdp') else await cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
