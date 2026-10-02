"""Verify map (maze) retrieval against what is actually RENDERED.

Three-way comparison:
  A. our maze API  : gc.getMaze().getTiles()  -> passability grid
  B. rendered walls: Phaser `mazeWallGroup` children (what you SEE on screen)
  C. game's own pathfinder: getShortestPathWithGraph — cross-check connectivity
Plus: prints ASCII maps so a screenshot can be compared by eye.

Usage: python scripts/verify_maze_vs_visual.py auto [--keep] [--players 2]
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
from start_online_game import create_room_via_ui, leave_online, state  # noqa: E402

DUMP = r"""(() => {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const g = GameManager.phaserInstance;
  const st = g.state.getCurrentState();
  const out = {};
  const maze = gc.getMaze ? gc.getMaze() : gc.roundController.model.getMaze();
  out.w = maze.getWidth(); out.h = maze.getHeight();
  // A. 逐格数据（tiles[x][y]）
  const tiles = maze.getTiles();
  out.tiles = [];
  for (let x = 0; x < out.w; x++) {
    const col = [];
    for (let y = 0; y < out.h; y++) {
      const t = tiles[x] ? tiles[x][y] : null;
      col.push(t ? Array.prototype.slice.call(t) : null);
    }
    out.tiles.push(col);
  }
  // B. 画面上真实渲染的墙体精灵
  const grp = st.mazeWallGroup;
  out.wallSprites = grp ? grp.children.map(c => ({
    x: Math.round(c.x * 100) / 100, y: Math.round(c.y * 100) / 100,
    w: (c.width !== undefined) ? c.width : null, h: (c.height !== undefined) ? c.height : null,
    key: (typeof c.key === 'string') ? c.key : (c.key && c.key.frame ? 'atlas' : null),
    angle: c.angle, visible: c.visible,
    anchorX: c.anchor ? c.anchor.x : null, anchorY: c.anchor ? c.anchor.y : null,
  })) : null;
  out.wallSpriteCount = out.wallSprites ? out.wallSprites.length : null;
  // 装饰组（对照，不应算作墙）
  try { out.decorCount = st.mazeWallDecorationGroup ? st.mazeWallDecorationGroup.children.length : null; } catch (e) {}
  // 相机与缩放（把渲染坐标换算成世界米）
  try { out.camera = {x: g.camera.x, y: g.camera.y, zoom: g.camera.scale.x,
                      worldW: g.world.width, worldH: g.world.height}; } catch (e) {}
  try { out.tileW = maze.getTileWidth ? maze.getTileWidth() : null; } catch (e) {}
  // C. 寻路交叉验证：随机取若干"相邻可通行格"，看能否一步直达
  const checks = [];
  const dirs = [[1,0],[0,1],[-1,0],[0,-1]];
  let tried = 0;
  for (let x = 0; x < out.w && tried < 40; x++) {
    for (let y = 0; y < out.h && tried < 40; y++) {
      const t = tiles[x] && tiles[x][y];
      if (!t || !t[0]) continue;
      for (const d of dirs) {
        const nx = x + d[0], ny = y + d[1];
        const n = tiles[nx] && tiles[nx][ny];
        if (!n) continue;
        const bothPassable = !!(t[0] && n[0]);
        let direct = null;
        try { const p = maze.getShortestPathWithGraph({x:x,y:y},{x:nx,y:ny});
              direct = p ? (p.length <= 2) : null; } catch (e) { direct = 'err'; }
        checks.push({from: [x,y], to: [nx,ny], bothPassable: bothPassable, direct: direct});
        tried++;
        break;
      }
    }
  }
  out.pathChecks = checks;
  // 当前坦克所在格（用于人眼对位）
  out.tanks = {};
  try { const ts = gc.getTanks(); for (const id in ts) out.tanks[id] = {x: ts[id].getX(), y: ts[id].getY()}; } catch (e) {}
  return JSON.stringify(out);
})()"""


def ascii_from_grid(w, h, blocked):
    rows = []
    for y in range(h):
        line = ''
        for x in range(w):
            line += '#' if ((x, y) in blocked) else '.'
        rows.append(line)
    return rows


async def main():
    ap = argparse.ArgumentParser(description='地图 get 数据 vs 渲染核对')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--keep', action='store_true', help='不自动退房（便于随后截图）')
    ap.add_argument('--players', type=int, default=2)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    cdp = CDP(port=port)
    await cdp.connect_page('tanktrouble.com', timeout=20)
    st = await state(cdp)
    try:
        if not (st.get('gameId') and st.get('tanks')):
            if args.players > 1:
                await cdp.eval("(() => { try { GameManager.phaserInstance.state.start('Menu'); } catch (e) {} return 'm'; })()")
                await asyncio.sleep(4)
                await cdp.eval("(() => { const s = GameManager.phaserInstance.state.getCurrentState();"
                               " if (s._twoPlayer) s._twoPlayer(); return 'ok'; })()")
                for _ in range(16):
                    await asyncio.sleep(1)
                    if (await state(cdp)).get('state') == 'Lobby':
                        break
            info = await create_room_via_ui(cdp)
            if not info:
                print('[map] ❌ 建房失败', flush=True)
                return 1
            st = await state(cdp)
            print('[map] 房间 %s 坦克=%s' % (st.get('gameId'), st.get('tanks')), flush=True)

        d = json.loads(await cdp.eval(DUMP))
        if args.json:
            with open(args.json, 'w', encoding='utf-8') as fh:
                json.dump(d, fh, ensure_ascii=False, indent=1)

        w, h = d['w'], d['h']
        print('\n[map] 迷宫 %d×%d 格（每格 10m → 场地 %.0f×%.0fm）' % (w, h, w * 10, h * 10), flush=True)

        # A. 由 tiles 推"含墙格的集合"
        # ⚠️ 语义更正（2026-10-02，见 docs/10 §16）：
        #    tiles[x][y] = [present, topWall, leftWall]
        #    * present=0 → 整格封死（监控室四边画墙）
        #    * present=1 且 topWall/leftWall 为 1 → **该格内画了墙边**，
        #      但**格子内部仍可通行**（"有墙条目" ≠ "不可通行"）
        #    因此不要用"有没有墙"判断能不能走；判路请用 maze.getShortestPathWithGraph。
        blocked_api = set()
        for x in range(w):
            for y in range(h):
                t = d['tiles'][x][y]
                if not t:
                    continue
                present, top, left = t[0], t[1], t[2]
                if (not present) or (present and (top or left)):
                    blocked_api.add((x, y))
        print('\n[map] A. getMaze().getTiles() 推出的"含墙格"（# = 格内有墙边；≠ 不可通行）:', flush=True)
        for r in ascii_from_grid(w, h, blocked_api):
            print('      ' + r, flush=True)

        # B. 由渲染的墙体精灵推"有墙"格（像素→米→格）
        WS = d.get('wallSprites') or []
        print('\n[map] B. 画面上渲染的墙体精灵 %d 个；样例：%s'
              % (len(WS), json.dumps(WS[:4], ensure_ascii=False)), flush=True)
        blocked_vis = set()
        for s in WS:
            mx, my = s['x'] / 20.0, s['y'] / 20.0      # 20 px = 1 m
            blocked_vis.add((int(mx // 10), int(my // 10)))
        print('[map] B. 渲染墙体覆盖的格子集合大小=%d' % len(blocked_vis), flush=True)
        for r in ascii_from_grid(w, h, blocked_vis):
            print('      ' + r, flush=True)

        # 对比
        only_api = blocked_api - blocked_vis
        only_vis = blocked_vis - blocked_api
        print('\n[map] 对比: A∩B=%d  A\\B=%s  B\\A=%s'
              % (len(blocked_api & blocked_vis), sorted(only_api), sorted(only_vis)), flush=True)

        # C. 寻路交叉验证
        pc = d.get('pathChecks') or []
        bad = [c for c in pc if c['bothPassable'] and c['direct'] is False]
        print('[map] C. 寻路检查 %d 组相邻格：两格都可通行但寻路需绕行 = %d 组 %s'
              % (len(pc), len(bad), json.dumps(bad[:5], ensure_ascii=False)), flush=True)

        # 坦克位置
        print('[map] 坦克位置（米）：%s' % json.dumps(d.get('tanks'), ensure_ascii=False), flush=True)
        print('[map] 相机=%s 装饰=%s' % (json.dumps(d.get('camera'), ensure_ascii=False),
                                        d.get('decorCount')), flush=True)
        if not args.keep:
            lv = await leave_online(cdp)
            print('[map] 退房: %s' % json.dumps(lv, ensure_ascii=False), flush=True)
        else:
            print('[map] 保留房间（--keep），记得随后截图并退房', flush=True)
        return 0
    finally:
        await cdp.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
