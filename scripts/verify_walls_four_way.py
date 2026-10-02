"""Four-way wall comparison to settle which map read is CORRECT.

References:
  REF  maze.getWalls()          -- game's own wall list (59 == rendered sprite count)
  TILE tiles[x][y]=[present,topWall,leftWall] -- what monitor_server.py derives from
  SPR  rendered wall sprites    -- what you SEE (px -> m -> tile)
  PATH getShortestPathWithGraph -- per-neighbour-pair connectivity

Prints ASCII maps + pairwise agreement. Optional screenshot + leave.

Usage: python scripts/verify_walls_four_way.py auto [--shot] [--leave]
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

DUMP = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const st = GameManager.phaserInstance.state.getCurrentState();
  const maze = gc.getMaze ? gc.getMaze() : gc.roundController.model.getMaze();
  const out = {w: maze.getWidth(), h: maze.getHeight()};
  // REF: 游戏自己的墙体列表
  const wl = maze.getWalls();
  out.walls = Array.prototype.slice.call(wl).map(v => ({x: v.x, y: v.y,
    flipX: v.flipX, flipY: v.flipY, rotate: v.rotate, number: v.number}));
  // TILE: 监控室的解释
  const td = maze.getTiles();
  out.tiles = [];
  for (let x = 0; x < out.w; x++) { const c = [];
    for (let y = 0; y < out.h; y++) { const t = td[x] ? td[x][y] : null;
      c.push(t ? [t[0], t[1], t[2]] : null); }
    out.tiles.push(c); }
  // SPR: 渲染精灵
  const grp = st.mazeWallGroup;
  out.sprites = grp ? grp.children.map(c => ({x: c.x, y: c.y, w: c.width, h: c.height,
    angle: c.angle, visible: c.visible})) : [];
  // PATH: 邻接连通性
  const direct = (a, b) => { try { const p = maze.getShortestPathWithGraph({x:a[0],y:a[1]},{x:b[0],y:b[1]});
    return !!(p && p.length <= 2); } catch (e) { return null; } };
  out.right = []; out.down = [];
  for (let x = 0; x < out.w; x++) { out.right.push([]); out.down.push([]); }
  for (let x = 0; x < out.w; x++) for (let y = 0; y < out.h; y++) {
    out.right[x][y] = (x + 1 < out.w) ? direct([x,y],[x+1,y]) : null;
    out.down[x][y]  = (y + 1 < out.h) ? direct([x,y],[x,y+1]) : null;
  }
  return JSON.stringify(out);
})()"""


def show(name, w, h, cells):
    print('[cmp] %s（%d 格）:' % (name, len(cells)), flush=True)
    for y in range(h):
        print('      ' + ''.join('#' if (x, y) in cells else '.' for x in range(w)), flush=True)


def agree(a, b, w, h):
    tot = w * h
    same = sum(1 for x in range(w) for y in range(h) if ((x, y) in a) == ((x, y) in b))
    return same, tot, round(100.0 * same / tot, 1)


async def main():
    ap = argparse.ArgumentParser(description='四方墙体对照')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--shot', action='store_true')
    ap.add_argument('--leave', action='store_true')
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)
    st = await state(cdp)
    if not (st.get('gameId') and st.get('tanks')):
        print('[cmp] 不在房间，退出', flush=True)
        return 1
    ctl = TankCtl(port=port)
    await ctl.connect()
    pid = list(await ctl.tanks())[0]
    d = json.loads(await ctl.cdp.eval(DUMP))
    await ctl.set_input(pid, right=True, hold=0.55)          # 保活
    w, h = d['w'], d['h']

    ref = {(v['x'], v['y']) for v in d['walls']}
    tile_walls = set()
    for x in range(w):
        for y in range(h):
            t = d['tiles'][x][y]
            if not t:
                continue
            present, top, left = t[0], t[1], t[2]
            if present and (top or left):
                tile_walls.add((x, y))
            elif not present:
                tile_walls.add((x, y))
    spr = set()
    for s in d['sprites']:
        # 精灵中心 px → 米 → 格（墙角可能在格边界，故按 5m 偏移归一）
        mx, my = s['x'] / 20.0, s['y'] / 20.0
        spr.add((int(mx // 10), int(my // 10)))
    path_walls = set()
    for x in range(w):
        for y in range(h):
            if d['right'][x][y] is False:
                path_walls.add((x, y))
            if d['down'][x][y] is False:
                path_walls.add((x, y))

    print('[cmp] 迷宫 %d×%d' % (w, h), flush=True)
    show('REF getWalls()', w, h, ref)
    show('TILE present/top/left（监控室用法）', w, h, tile_walls)
    show('SPR 渲染精灵', w, h, spr)
    show('PATH 寻路连通性', w, h, path_walls)

    for n1, s1 in (('REF', ref), ('TILE', tile_walls), ('SPR', spr), ('PATH', path_walls)):
        for n2, s2 in (('REF', ref), ('TILE', tile_walls), ('SPR', spr), ('PATH', path_walls)):
            if n1 >= n2:
                continue
            same, tot, pct = agree(s1, s2, w, h)
            print('[cmp] %s vs %s：一致 %d/%d 格 = %s%%' % (n1, n2, same, tot, pct), flush=True)

    print('[cmp] getWalls() 数量=%d  渲染精灵数量=%d  → 数量一致=%s'
          % (len(d['walls']), len(d['sprites']), len(d['walls']) == len(d['sprites'])), flush=True)
    with open(os.path.join(HERE, 'scratch', 'walls_four_way.json'), 'w', encoding='utf-8') as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)

    if args.shot:
        # 先关掉可能遮挡的 overlay，再截图由调用方处理
        await ctl.cdp.eval("(() => { try { OverlayManager.popOverlay(); } catch (e) {}"
                           " try { if (OverlayManager.overlay) OverlayManager._hideOverlay(); } catch (e) {}"
                           " return 'overlay hidden'; })()")
        await asyncio.sleep(1)
        print('[cmp] 已请求隐藏 overlay，可截图', flush=True)
    if args.leave:
        lv = await leave_online(cdp)
        print('[cmp] 退房: %s' % json.dumps(lv, ensure_ascii=False), flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
