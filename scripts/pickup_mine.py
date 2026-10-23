"""
TankTrouble — 拾取地雷箱子并部署地雷
====================================
找到最近的地雷箱子（type=4），用游戏自身的寻路
（getShortestPathWithGraph）规划格子路径，逐航点导航坦克，
拾取箱子，然后部署地雷。

单位：getX()/getY() 返回【米】。1 格 = 10m。

用法：python pickup_mine.py [port] [tank_id]
"""
import asyncio
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from tankctl import TankCtl
from gameutils import wrap_pi

TILE = 10.0  # 每格米数
SPEED = 15.95  # 前进速度 m/s（★2026-10-24 实测；旧值 7.5 有误，官方 TANK.FORWARD_SPEED=15.95）
ARRIVE = 1.0  # 米：离目标多近算到达
CRAWL = 0.35  # 米：拾取范围


class Navigator:
    def __init__(self, ctl, tank_id):
        self.ctl = ctl
        self.tid = tank_id

    async def state(self):
        ts = await self.ctl.tanks()
        t = ts.get(self.tid)
        if not t:
            return None
        return {'x': t['x'], 'y': t['y'], 'rot': t['rot']}

    async def turn_to(self, tx, ty):
        """转向让前进方向对准 (tx,ty)。对准时返回 True。
        比例控制：转向时长随角度差缩放。
        已实测约定（游戏物理 + 实测）：RIGHT 增大旋转值 -> diff 为正即右转。"""
        st = await self.state()
        if not st:
            return False
        fwd = math.atan2(ty - st['y'], tx - st['x'])  # 前进角
        target_rot = wrap_pi(fwd + math.pi / 2)        # rot = fwd + pi/2
        diff = wrap_pi(target_rot - wrap_pi(st['rot']))
        if abs(diff) < 0.04:
            return True
        # 比例控制：dur = diff / 转向速率，但给个最小脉冲保证物理生效
        dur = max(abs(diff) / 5.0, 0.06)  # ★2026-10-24 实测转向 5.0 rad/s（旧值 4.5）
        dur = min(dur, 0.4)
        if diff > 0:
            await self.ctl.turn_right(dur, self.tid)
        else:
            await self.ctl.turn_left(dur, self.tid)
        return False

    async def go_to(self, tx, ty, pickup=False):
        """以米为单位行驶到 (tx,ty)。转向后前进，带撞墙检测。"""
        stuck = 0
        last = None
        while True:
            st = await self.state()
            if not st:
                return False
            d = math.hypot(tx - st['x'], ty - st['y'])
            if d < (CRAWL if pickup else ARRIVE):
                return True
            if await self.turn_to(tx, ty):
                # 前进距离与剩余距离成正比，且设上限避免在航点附近过冲
                dur = min(d / SPEED, 0.3)
                before = (st['x'], st['y'])
                await self.ctl.forward(dur, self.tid)
                st2 = await self.state()
                moved = math.hypot(st2['x'] - before[0], st2['y'] - before[1]) if st2 else 0
                if moved < 0.1:
                    # 可能卡在墙上：wiggle 挣脱
                    stuck += 1
                    if stuck > 3:
                        print(f'  [nav] stuck ({stuck}), wiggling', flush=True)
                        await self.ctl.back(0.3, self.tid)
                        await self.ctl.turn_right(0.2, self.tid)
                        await self.ctl.turn_left(0.3, self.tid)
                        stuck = 0
                else:
                    stuck = 0
            last = st


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    tank_id = sys.argv[2] if len(sys.argv) > 2 else None

    ctl = TankCtl(port=port)
    await ctl.connect()

    # 等待坦克出现
    ts = {}
    for _ in range(20):
        ts = await ctl.tanks()
        if ts:
            break
        await asyncio.sleep(1)
    if not ts:
        print('[pickup] no tanks', flush=True)
        return 1
    if not tank_id:
        tank_id = list(ts.keys())[0]
    print(f'[pickup] using tank {tank_id}', flush=True)

    # 定位地雷箱子（type=4）
    info = await ctl.cdp.eval('''(() => {
      const gc = GameManager.getGameController() || GameManager.gameController;
      const rm = gc.getRoundModel ? gc.getRoundModel() : null;
      const cs = (rm && rm.getCollectibles) ? rm.getCollectibles() : (gc.getCollectibles ? gc.getCollectibles() : {});
      const me = gc.getTanks()["''' + tank_id + '''"];
      let best = null, bestDist = 1e9;
      for (const cid in cs) {
        const c = cs[cid];
        const t = c.getType ? c.getType() : -1;
        if (t === 4) {
          const d = Math.hypot(c.getX() - me.getX(), c.getY() - me.getY());
          if (d < bestDist) { bestDist = d; best = {id: cid, x: c.getX(), y: c.getY()}; }
        }
      }
      return JSON.stringify(best ? best : null);
    })()''')
    if not info or info == 'null':
        print('[pickup] no mine crate found (type=4)', flush=True)
        return 1
    cr = json.loads(info)
    print(f'[pickup] mine crate {cr["id"]} at ({cr["x"]:.1f},{cr["y"]:.1f})m', flush=True)

    # 规划路径：坦克格 -> 箱子格
    st = await ctl.tanks()
    me = st[tank_id]
    start_tile = {'x': int(me['x'] // TILE), 'y': int(me['y'] // TILE)}
    end_tile = {'x': int(cr['x'] // TILE), 'y': int(cr['y'] // TILE)}
    path = await ctl.cdp.eval(
        f'(GameManager.getGameController().getMaze().getShortestPathWithGraph('
        f'{{x:{start_tile["x"]},y:{start_tile["y"]}}},{{x:{end_tile["x"]},y:{end_tile["y"]}}}))'
        f'.map(function(p){{return {{x:p.x,y:p.y}}}})'
    )
    if not path:
        print('[pickup] no path found', flush=True)
        return 1
    # 去重相邻重复点
    way = []
    for p in path:
        if not way or way[-1] != p:
            way.append(p)
    print(f'[pickup] path: {way}', flush=True)

    nav = Navigator(ctl, tank_id)
    # 逐个航点中心导航
    for i, wp in enumerate(way):
        wx = (wp['x'] + 0.5) * TILE
        wy = (wp['y'] + 0.5) * TILE
        d = math.hypot(wx - me['x'], wy - me['y'])
        if i == 0 and d < ARRIVE:
            continue
        print(f'[pickup] -> waypoint {wp} ({wx:.0f},{wy:.0f})', flush=True)
        ok = await nav.go_to(wx, wy)
        if not ok:
            print('[pickup] nav failed at waypoint', wp, flush=True)
            return 1

    # 接近箱子（进入拾取范围）
    print('[pickup] approaching crate for pickup...', flush=True)
    await nav.go_to(cr['x'], cr['y'], pickup=True)

    # 检查武器
    wp = await ctl.cdp.eval(f'(GameManager.getGameController()).getActiveWeapon("{tank_id}").getType()')
    print(f'[pickup] weapon now: {wp} (4=mine)', flush=True)
    if str(wp) != '4':
        # 再慢慢贴近
        for _ in range(4):
            await ctl.forward(0.4, tank_id)
            await asyncio.sleep(0.2)
            wp = await ctl.cdp.eval(f'(GameManager.getGameController()).getActiveWeapon("{tank_id}").getType()')
            if str(wp) == '4':
                break
    if str(wp) != '4':
        print('[pickup] failed to pick up mine', flush=True)
        return 1

    print('[pickup] MINE PICKED UP! Deploying...', flush=True)
    # 地雷武器：开火 = 扔地雷
    await ctl.fire(0.25, tank_id)
    await asyncio.sleep(0.6)

    # 验证地雷已部署
    mines = await ctl.cdp.eval('''(() => {
      const gc = GameManager.getGameController() || GameManager.gameController;
      const rm = gc.getRoundModel ? gc.getRoundModel() : null;
      const ts = (rm && rm.getTraps) ? rm.getTraps() : (gc.getTraps ? gc.getTraps() : {});
      const out = [];
      for (const tid in ts) { const t = ts[tid]; out.push({id: tid, x: t.getX(), y: t.getY(), active: t.isActivated ? t.isActivated() : false}); }
      return JSON.stringify(out);
    })()''')
    print(f'[pickup] mines after deploy: {mines}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
