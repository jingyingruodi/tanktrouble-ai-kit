"""
TankTrouble 共享游戏工具（Shared game helpers）
===============================================
供脚本（loop_suicide, record_only, ...）使用的公共游戏状态查询。

所有 helper 都通过 CDP 与游戏通信，且【在线安全】：它们只 READ 游戏状态
（不做直接 setter 修改），因此永远不会触发服务端的回弹。

关键概念：
  - 回合状态机（权威）：GameModel.getState() 返回以下之一
        'between rounds' | 'counting down' | 'in round' | 'ended'
  - 可控坦克：读取游戏自身的输入管理器
        (window.Inputs.__metaclass.inputManagers) —— 权威静态绑定，
        回合切换和页面刷新后依然稳定。
  - 迷宫每回合重新生成，所以尺寸必须实时重新读取。
"""
import json
import math

# 坦克转向速度（rad/s），用于估算转向时长
# ★ 2026-10-24 游戏内实测修正：4.5 → 5.0
#   实测右转 4.80 / 左转 5.17 rad/s（帧定时量化误差），官方常量 TANK.ROTATION_SPEED=5.0；
#   前进 15.95 m/s、后退 12.8 m/s（实测物理体线速度恒定、无加速段），见 cdp_tools/tank_constants.py
TURN_RATE = 5.0


def wrap_pi(a):
    """把角度归一化到 [-pi, pi)。"""
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def parse(raw, default=None):
    """解析 CDP eval 结果（可能是 JSON 字符串或已是 dict）。"""
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return default


# ─── 回合状态 ───
async def get_round_state(ctl):
    """返回游戏的回合状态字符串，出错时返回 None。
    取值：'in round' | 'between rounds' | 'counting down' | 'ended' | 'none'。"""
    try:
        return await ctl.cdp.eval(
            '(() => { try { const gc=GameManager.getGameController()||GameManager.gameController;'
            ' if(!gc) return "none"; const st=gc.model && gc.model.getState ? gc.model.getState() : null;'
            ' return st || "none"; } catch(e){ return "none"; } })()'
        )
    except Exception:
        return None


async def round_active(ctl):
    """回合进行中（'in round'）时返回 True。"""
    return await get_round_state(ctl) == 'in round'


# ─── 可控坦克（权威输入管理器绑定）───
CONTROLLABLE_JS = """(() => {
  const out = {};
  const ims = window.Inputs.__metaclass.inputManagers;
  for (let i=0;i<ims.length;i++) {
    const m = ims[i];
    const isK = m.__class === window.KeyboardInputManager;
    const isM = m.__class === window.MouseInputManager;
    if (!m.playerId) continue;
    if (isK) out[m.playerId] = (m.forwardKey === 38) ? 'arrows' : 'keyboard';
    else if (isM) out[m.playerId] = 'mouse';
  }
  return JSON.stringify(out);
})()"""


async def get_controllable_tanks(ctl):
    """返回所有本地可控坦克 {playerId: role}。

    读取游戏自身的输入管理器（window.Inputs.__metaclass.inputManagers）。
    - 本地局：3 个角色（keyboard/arrows/mouse）
    - 在线局：只有本地玩家的坦克才有输入管理器
    role 为 'keyboard' | 'arrows' | 'mouse'。出错时返回空字典。"""
    try:
        d = parse(await ctl.cdp.eval(CONTROLLABLE_JS), {})
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ─── 坦克存活 ───
async def tank_alive(ctl, tank_id):
    """getTanks() 中仍存在该坦克对象时返回 True。

    死亡坦克最终会被移除；冻结但仍残留的"尸体"由调用方的冻结检测 /
    死亡后冷却逻辑处理。"""
    try:
        ts = await ctl.tanks()
        return tank_id in ts
    except Exception:
        return False


# ─── 迷宫 ───
MAZE_DIMS_JS = ('(() => { const gc=GameManager.getGameController()||GameManager.gameController;'
                ' const m=gc&&gc.getMaze?gc.getMaze():null;'
                ' return m ? JSON.stringify({w:m.getWidth(),h:m.getHeight()}) : null; })()')


async def get_maze_dims(ctl, fallback=None):
    """返回世界尺寸 (world_w, world_h)（米），迷宫不可读时返回 `fallback`。
    迷宫每回合重新生成，所以必须实时重新读取。
    fallback 传 None 可区分"无迷宫"与真实尺寸。

    每格 10m，所以世界尺寸 = 格子数 * 10。"""
    try:
        md = parse(await ctl.cdp.eval(MAZE_DIMS_JS), None)
        if md and md.get('w') and md.get('h'):
            return md['w'] * 10.0, md['h'] * 10.0
    except Exception:
        pass
    return fallback


def _pt_seg_dist(px, py, x1, y1, x2, y2):
    """点 (px,py) 到线段 (x1,y1)-(x2,y2) 的距离，以及从线段最近点到该点的
    （带符号）偏移向量。"""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    cx, cy = x1 + t * dx, y1 + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5, (px - cx, py - cy)


async def find_nearest_wall(ctl, x, y, world_w, world_h):
    """找出离 (x, y) 最近的墙段（外边界 OR 内部迷宫墙）。

    返回 (wall, dist, target_rot)：
      - wall:       'top'|'bottom'|'left'|'right'（仅外墙回退）或 'nearest'
      - dist:       到墙的距离（米）
      - target_rot: 坦克应对着的朝向，使子弹垂直射入墙并直反弹回。

    迷宫不可达时，退化为只用外边界。"""
    # 从迷宫格子 + 外边界构建墙段（复用 get_wall_segments 的完整墙段）
    try:
        segs = await get_wall_segments(ctl)
    except Exception:
        segs = None

    if not segs:
        # 回退：只用外边界
        dists = {'top': max(0.0, y), 'bottom': max(0.0, world_h - y),
                 'left': max(0.0, x), 'right': max(0.0, world_w - x)}
        wall = min(dists, key=dists.get)
        fwd = {'top': -math.pi / 2, 'bottom': math.pi / 2, 'left': math.pi, 'right': 0.0}[wall]
        return wall, dists[wall], fwd + math.pi / 2

    # 最近墙段：点到线段距离
    best_d, best_norm = 1e9, (0, 0)
    for x1, y1, x2, y2 in segs:
        d, norm = _pt_seg_dist(x, y, x1, y1, x2, y2)
        if d < best_d:
            best_d, best_norm = d, norm
    # 朝向墙的方向：取墙最近点到坦克的偏移向量，反向（即从坦克指向墙）。
    nx, ny = best_norm
    ln = (nx * nx + ny * ny) ** 0.5
    fwd = 0.0 if ln < 0.001 else math.atan2(-ny, -nx)
    return 'nearest', best_d, fwd + math.pi / 2


# ═══════════ 地形 / 墙体数据（静态，每回合读一次即可）═══════════
# ⚠️ 读取时机：地形只在【回合切换/回合创建】时变化（迷宫重新生成）。
#    对局中全程不变，因此【每个回合开始时读一次】即可，无需每帧读取。
#    可通过 get_round_state() 检测到 'in round' 时刷新缓存。
#
# 数据结构（来自游戏 Maze 对象，GWT 反编译确认）：
#   m.getTiles() -> data.tiles，二维数组 tiles[x][y] = [present, topWall, leftWall]
#     present (tile[0]) : 1 = 该格是可通过的地面（开放格），0 = 封闭格
#     topWall (tile[1]) : 1 = 该格上边界（-y 方向）有墙
#     leftWall(tile[2]) : 1 = 该格左边界（-x 方向）有墙
#   墙只记录在【面向它的开放格】上：开放格四周若邻接封闭格/地图外，则该
#   方向的 topWall/leftWall 置 1。因此遍历全部开放格即可还原所有内部墙。
#
#   其他渲染数据（非碰撞、仅视觉）：
#     m.getWalls()      -> 内墙贴图 [{x, y, number, rotate, flipX, flipY}]
#     m.getBorders()    -> 边框贴图 [{x, y, number, orientation, flip}]
#     m.getFloors()     -> 地板贴图 [{x, y, number, orientation}]
#     m.getSpaces()     -> 封闭空间贴图 [{x, y, number, orientation}]
#
#   单位：1 格 = 10 米（Constants.MAZE_TILE_SIZE）。所有坐标输出为米。

TILE_M = 10.0  # 每格米数

# 获取原始 tile 网格 + 尺寸（一次 CDP 往返）
MAZE_TILES_JS = """(() => {
  const gc = GameManager.getGameController()||GameManager.gameController;
  const m = gc && gc.getMaze ? gc.getMaze() : null;
  if (!m) return null;
  const w = m.getWidth(), h = m.getHeight();
  const td = m.getTiles();
  // 压缩为 {w,h,tiles:[[[p,top,left],...],...]}（按行组织的扁平数组）
  const rows = [];
  for (let y=0;y<h;y++) {
    const row = [];
    for (let x=0;x<w;x++) {
      const t = td[x] ? (td[x][y] || [0,0,0]) : [0,0,0];
      row.push([t[0]?1:0, t[1]?1:0, t[2]?1:0]);
    }
    rows.push(row);
  }
  return JSON.stringify({w: w, h: h, tiles: rows});
})()"""


async def get_maze_tiles(ctl):
    """获取迷宫原始格数据。返回 dict：
        {w: 宽度(格), h: 高度(格),
         tiles: 二维数组 tiles[y][x] = [present, topWall, leftWall]}
    注意索引顺序是【tiles[y][x]】（先 y 后 x，便于按行遍历）。
    迷宫不存在时返回 None。迷宫每回合重新生成，务必每次实时读取。"""
    try:
        return parse(await ctl.cdp.eval(MAZE_TILES_JS), None)
    except Exception:
        return None


# 构建全部墙段（外边界 + 内部墙），米为单位。与 monitor 渲染同规则。
WALL_SEGS_FULL_JS = """(() => {
  const gc = GameManager.getGameController()||GameManager.gameController;
  const m = gc && gc.getMaze ? gc.getMaze() : null;
  if (!m) return null;
  const w = m.getWidth(), h = m.getHeight();
  const td = m.getTiles();
  const T = 10; // 每格米数
  const segs = [];
  // 外边界（地图外框）
  segs.push([0,0,w*T,0],[0,h*T,w*T,h*T],[0,0,0,h*T],[w*T,0,w*T,h*T]);
  for (let x=0;x<w;x++) for (let y=0;y<h;y++) {
    const t = td[x]?(td[x][y]||[0,0,0]):[0,0,0];
    const tx=x*T, ty=y*T;
    if (t[0]) {
      // 开放格：看自身 top/left 墙（墙记录在面向它的开放格上）
      if (t[1]) segs.push([tx,ty,tx+T,ty]);   // 上墙（水平）
      if (t[2]) segs.push([tx,ty,tx,ty+T]);   // 左墙（垂直）
    } else {
      // 封闭格：四面都是墙
      segs.push([tx,ty,tx+T,ty],[tx,ty+T,tx+T,ty+T],[tx,ty,tx,ty+T],[tx+T,ty,tx+T,ty+T]);
    }
  }
  return JSON.stringify(segs);
})()"""


async def get_wall_segments(ctl):
    """获取全部墙体墙段列表（米为单位），供碰撞/寻路/渲染使用。
    返回 [[x1,y1,x2,y2], ...]：每条是线段端点，含外边界 + 内部墙。
    已去重（封闭格四面墙可能与外边界/相邻封闭格产生重复线段）。
    迷宫不存在时返回 None。"""
    try:
        segs = parse(await ctl.cdp.eval(WALL_SEGS_FULL_JS), None)
        if not segs:
            return None
        # 去重：保持原始顺序，去掉重复线段
        seen = set()
        out = []
        for s in segs:
            key = (s[0], s[1], s[2], s[3])
            if key not in seen:
                seen.add(key)
                out.append(s)
        return out
    except Exception:
        return None


async def get_terrain(ctl):
    """获取完整地形摘要（一次拿到网格 + 墙段 + 毒圈）。返回 dict：
        {w, h,                        # 格子宽高
         world_w, world_h,            # 世界尺寸（米）
         tiles: [[present,top,left],...],   # tiles[y][x]，同 get_maze_tiles
         walls: [[x1,y1,x2,y2],...],  # 全部墙段（米），同 get_wall_segments
         zones: [{id, minX...}, ...]} # 毒圈列表，同 get_storm_zones
    迷宫不存在时返回 None。
    注意：tiles/walls 每回合静态，zones 随时间动态收缩。
    只需毒圈时可调用 extract_storm_zones(terrain) 筛出。"""
    tiles = await get_maze_tiles(ctl)
    if not tiles:
        return None
    walls = await get_wall_segments(ctl) or []
    zones = await get_storm_zones(ctl)
    return {
        'w': tiles['w'],
        'h': tiles['h'],
        'world_w': tiles['w'] * TILE_M,
        'world_h': tiles['h'] * TILE_M,
        'tiles': tiles['tiles'],
        'walls': walls,
        'zones': zones,
    }


def extract_storm_zones(terrain):
    """从 get_terrain() 返回的完整地形 dict 里【筛出毒圈数据】。

    参数 terrain：get_terrain() 的返回值（含 'zones' 键）。
    返回毒圈列表（同 get_storm_zones 的结构），terrain 为空/无 zones 时返回 []。

    示例：
        terr = await bhv.get_terrain()
        zones = extract_storm_zones(terr)   # 单独取毒圈
        walls = terr['walls']               # 单独取墙段
    """
    if not isinstance(terrain, dict):
        return []
    zones = terrain.get('zones')
    return zones if isinstance(zones, list) else []


async def is_open_tile(ctl, tx, ty):
    """判断迷宫格 (tx, ty)（格子坐标）是否可通行（present=1 且在地图内）。
    返回 True/False；迷宫不可读时返回 False。"""
    try:
        md = await get_maze_tiles(ctl)
        if not md:
            return False
        if tx < 0 or ty < 0 or tx >= md['w'] or ty >= md['h']:
            return False
        return md['tiles'][ty][tx][0] == 1
    except Exception:
        return False


async def open_tile_center(ctl, tx, ty):
    """返回可通行格 (tx, ty) 的【中心世界坐标】(x, y)（米）。
    该格不可通行或迷宫不可读时返回 None。
    用途：传送/寻路目标点必须落在开放格中心，避免卡进墙里。"""
    if not await is_open_tile(ctl, tx, ty):
        return None
    return (tx * TILE_M + TILE_M / 2, ty * TILE_M + TILE_M / 2)


# ═══════════ 迷宫寻路（复用游戏 getShortestPathWithGraph）═══════════
# 游戏自带 Dijkstra/A* 寻路（Maze 构建时预计算 distances 表 + graph）。
# 直接调用权威 API，比自己在 Python 重建寻路可靠得多。

async def plan_maze_path(ctl, start_xy, end_xy):
    """规划从 start_xy 到 end_xy 的迷宫格子路径（复用游戏寻路）。

    参数：start_xy/end_xy 为世界坐标（米），会内部转成格子坐标。
    返回 [(gx, gy), ...] 格子序列（含起点、不含终点所在格的下一格？）
    —— 实际返回游戏给出的完整路径。无路径/出错返回 []。
    """
    try:
        sx, sy = int(start_xy[0] // TILE_M), int(start_xy[1] // TILE_M)
        ex, ey = int(end_xy[0] // TILE_M), int(end_xy[1] // TILE_M)
        js = (f'(GameManager.getGameController().getMaze().getShortestPathWithGraph('
              f'{{x:{sx},y:{sy}}},{{x:{ex},y:{ey}}}))'
              f'.map(function(p){{return {{x:p.x,y:p.y}}}})')
        raw = await ctl.cdp.eval(js)
        path = parse(raw, [])
        if not isinstance(path, list):
            return []
        # 去重相邻重复点
        way = []
        for p in path:
            if isinstance(p, dict) and 'x' in p and 'y' in p:
                pt = (int(p['x']), int(p['y']))
                if not way or way[-1] != pt:
                    way.append(pt)
        return way
    except Exception:
        return []


async def maze_distance(ctl, from_xy, to_xy):
    """用迷宫 Dijkstra 距离场查两点间的迷宫距离（格数）。

    复用游戏预计算的 distances 表（getDistancesFromPosition）。
    返回距离（格）或 None（不可达/出错）。"""
    try:
        fx, fy = int(from_xy[0] // TILE_M), int(from_xy[1] // TILE_M)
        tx, ty = int(to_xy[0] // TILE_M), int(to_xy[1] // TILE_M)
        js = (f'(function(){{var m=GameManager.getGameController().getMaze();'
              f'var d=m.getDistancesFromPosition({{x:{fx},y:{fy}}});'
              f'if(!d)return null;var v=m.getDistanceToPosition(d,{{x:{tx},y:{ty}}});'
              f'return v===false?null:v;}})()')
        raw = await ctl.cdp.eval(js)
        return raw
    except Exception:
        return None


# ═══════════ 毒圈（Radioactive Fog / Storm Zone）【动态，需实时读取】═══════════
# ⚠️ 与地形不同：毒圈是【随时间动态变化】的——
#    回合开始后才出现，随后持续收缩，直到安全区缩到最小。
#    因此每次决策都要实时读取（如每帧/每次寻路前），不能缓存。
#    需要安全区信息的 AI 行为应在移动前调用 is_in_safe_area() 检查。
#
# 数据结构（来自 Zone 对象的 getFields()，详见 docs/06-毒圈技术文档.md）：
#   _tileBounds:          地图 tile 边界 {minX,minY,maxX,maxY}
#   _expansionTilesLeft/Top/Right/Bottom: 四边已收缩的 tile 数（整数）
#   _expansionSide:       当前活跃收缩方向 0=右 1=下 2=左 3=上，-1=步间等待
#   _expansionTime/_expansionSequence: 缩圈节奏（步长倒计时 + 方向序列）
#
# 安全区（可存活区域）tile 范围：
#   left   = _tileBounds.minX + _expansionTilesLeft
#   right  = _tileBounds.maxX - _expansionTilesRight
#   top    = _tileBounds.minY + _expansionTilesTop
#   bottom = _tileBounds.maxY - _expansionTilesBottom
# 毒圈随时间从外向里收缩，每步消耗一个方向序列元素。

STORM_ZONES_JS = """(() => {
  const gc = GameManager.getGameController()||GameManager.gameController;
  const zones = gc && gc.getZones ? gc.getZones() : {};
  const out = [];
  for (const zid in zones) {
    try {
      const f = zones[zid].getFields ? zones[zid].getFields() : null;
      if (!f || !f._tileBounds) continue;
      const tb = f._tileBounds;
      out.push({
        id: zid,
        minX: tb.minX, minY: tb.minY, maxX: tb.maxX, maxY: tb.maxY,
        expLeft: f._expansionTilesLeft || 0, expTop: f._expansionTilesTop || 0,
        expRight: f._expansionTilesRight || 0, expBottom: f._expansionTilesBottom || 0,
        side: (f._expansionSide !== undefined) ? f._expansionSide : -1,
      });
    } catch(e) {}
  }
  return JSON.stringify(out);
})()"""


async def get_storm_zones(ctl):
    """获取所有毒圈（storm zone）当前状态。返回列表：
        [{id, minX, minY, maxX, maxY,          # 地图 tile 边界
          expLeft, expTop, expRight, expBottom, # 四边已收缩 tile 数
          side}, ...]                           # 当前活跃收缩方向
    side: 0=右 1=下 2=左 3=上，-1=步间等待。
    没有毒圈/非缩圈模式返回空列表。"""
    try:
        z = parse(await ctl.cdp.eval(STORM_ZONES_JS), [])
        return z if isinstance(z, list) else []
    except Exception:
        return []


async def get_storm_safe_area(ctl):
    """计算当前毒圈【安全区】的 tile 范围（可存活区域）。
    返回 dict：{minX, minY, maxX, maxY}（tile 坐标，含边界），
    或 None（无毒圈 / 数据缺失）。
    多个毒圈时取第一个（游戏通常只有一个 storm zone）。"""
    zones = await get_storm_zones(ctl)
    if not zones:
        return None
    z = zones[0]
    return {
        'minX': z['minX'] + z['expLeft'],
        'minY': z['minY'] + z['expTop'],
        'maxX': z['maxX'] - z['expRight'],
        'maxY': z['maxY'] - z['expBottom'],
    }


async def is_in_safe_area(ctl, x, y):
    """判断世界坐标 (x, y)（米）是否在毒圈安全区内。
    返回 True/False。无安全区数据时保守返回 True（不误判致死）。
    tile 判定：坐标除以每格米数取整，落在安全区 tile 范围内即可。"""
    safe = await get_storm_safe_area(ctl)
    if not safe:
        return True
    tx, ty = int(x / TILE_M), int(y / TILE_M)
    return (safe['minX'] <= tx <= safe['maxX']
            and safe['minY'] <= ty <= safe['maxY'])


# ─── 摧毁检测 ───
DESTROYED_JS = """(() => {
  const rc = GameManager.getGameController().roundController;
  const m = rc && rc.model;
  return JSON.stringify(m && m.destroyedPlayerIds ? m.destroyedPlayerIds : []);
})()"""


async def destroyed_tanks(ctl):
    """返回回合模型中已标记摧毁的 playerId 列表。"""
    try:
        d = parse(await ctl.cdp.eval(DESTROYED_JS), [])
        return d if isinstance(d, list) else []
    except Exception:
        return []
