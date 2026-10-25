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
"""
观测向量（AI-B 上游契约，v1）
=============================
把一帧 `monitor_server.STATE_JS` 快照 + 我方 `pid` 变成一个**定长、相对、
有界**的浮点向量（`list[float]`，无第三方依赖）。

设计要点
--------
* **相对**：所有坐标都转成"相对我方坦克 + 按世界尺寸归一化"，
  这样模型学到的东西与地图尺寸/出生点无关。
* **定长**：实体数量按 `max_*` 截断并补零 → 可直接喂固定输入维度的网络。
* **有界**：所有分量裁剪到 [-1, 1]（距离按世界对角线归一化）。
* **可选栅格**：传 `terrain` 时附带以我方为中心 (2r+1)² 的墙体位图。

分段布局（顺序固定，改动必须升 `OBS_VERSION`）
---------------------------------------------
| 段 | 长度 | 含义 |
|----|------|------|
| self        | 10 | x/W, y/H, cos(rot), sin(rot), vx/18, vy/18, weapon/5, shield, shieldLife/10, alive |
| round       | 5  | roundActive, betweenRounds, countingDown, storm, 场上坦克数/max_tanks |
| score       | 4  | 我方击杀/10, 我方胜利/10, 对手最高击杀/10, 对手最高胜利/10 |
| others      | max_tanks×7 | 相对位置(dx/W,dy/H,dist/D)、**相对炮口方位**(cos,sin)、武器/5、存活 |
| projectiles | max_projectiles×7 | 相对位置、速度/18、剩余寿命比、deadlyToOwner、是否我方发射 |
| collectibles| max_collectibles×4 | 相对位置、距离/D、类型/6 |
| mines       | max_mines×4 | 相对位置、已激活、是否我方 |
| grid        | (2r+1)²×3 | present / topWall / leftWall（仅传 terrain 时） |

W/H = 世界宽高（米，= 格数×10）；D = 世界对角线（用于距离归一化）；
`rot` 用未归一化累积角的 sin/cos（见 02 文档 §4.2）。
"""
import math

OBS_VERSION = 1

BULLET_SPEED = 18.0      # 弹速恒定（m/s），用于速度归一化
MAX_WEAPON = 5           # 武器枚举 -1..5（-1 基础弹）
MAX_COLLECTIBLE = 6      # 道具枚举 0..6
TILE_M = 10.0            # 1 格 = 10 米

DEFAULT_MAX_TANKS = 4
DEFAULT_MAX_PROJECTILES = 12
DEFAULT_MAX_COLLECTIBLES = 6
DEFAULT_MAX_MINES = 4
DEFAULT_GRID_RADIUS = 3

SELF_LEN = 10
ROUND_LEN = 5
SCORE_LEN = 4
OTHER_LEN = 7
PROJ_LEN = 7
COLL_LEN = 4
MINE_LEN = 4


def _clamp(v, lo=-1.0, hi=1.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float('inf'), float('-inf')):   # NaN / inf
        return 0.0
    return lo if v < lo else (hi if v > hi else v)


def _f(d, key, default=0.0):
    v = d.get(key, default) if isinstance(d, dict) else default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _num(v, default=0.0):
    """标量转 float（非数值/None → default）。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def obs_size(grid_radius=DEFAULT_GRID_RADIUS, max_tanks=DEFAULT_MAX_TANKS,
             max_projectiles=DEFAULT_MAX_PROJECTILES,
             max_collectibles=DEFAULT_MAX_COLLECTIBLES,
             max_mines=DEFAULT_MAX_MINES):
    """该配置下的观测向量长度（不含栅格时把 grid_radius 传 0/None）。"""
    grid = 0 if not grid_radius else ((2 * int(grid_radius) + 1) ** 2) * 3
    return (SELF_LEN + ROUND_LEN + SCORE_LEN
            + max_tanks * OTHER_LEN
            + max_projectiles * PROJ_LEN
            + max_collectibles * COLL_LEN
            + max_mines * MINE_LEN
            + grid)


def encode_grid(terrain, x, y, radius=DEFAULT_GRID_RADIUS):
    """以世界坐标 (x,y) 为中心取 (2r+1)² 墙体位图，展平成二进制特征。

    tiles[y][x] = [present, topWall, leftWall]（present=1 可通行）。
    越界一律按"封闭格"(0,1,1) 处理。
    """
    r = int(radius)
    out = []
    tiles = (terrain or {}).get('tiles') or []
    h = len(tiles)
    w = len(tiles[0]) if h else 0
    if not w or not h:
        # 地形未知（页面未就绪/大厅）→ 全 0 表示"未知"，不要伪造成"被墙包围"。
        return [0.0] * (((2 * r + 1) ** 2) * 3)
    cx, cy = int(x // TILE_M), int(y // TILE_M)
    for gy in range(cy - r, cy + r + 1):
        for gx in range(cx - r, cx + r + 1):
            if 0 <= gx < w and 0 <= gy < h:
                t = tiles[gy][gx]
                out.extend([_clamp(t[0]), _clamp(t[1]), _clamp(t[2])])
            else:
                out.extend([0.0, 1.0, 1.0])
    return out


def build_obs(state, pid, terrain=None, grid_radius=DEFAULT_GRID_RADIUS,
              max_tanks=DEFAULT_MAX_TANKS, max_projectiles=DEFAULT_MAX_PROJECTILES,
              max_collectibles=DEFAULT_MAX_COLLECTIBLES, max_mines=DEFAULT_MAX_MINES):
    """构造观测。

    返回 dict：
      {'vector': [...], 'size': N, 'version': OBS_VERSION,
       'layout': {段名: [起, 止]}, 'meta': {...}}
    `state` 为 None 或非 dict 时返回全零向量（便于"页面未就绪"时占位）。
    """
    if not isinstance(state, dict):
        state = {}
    maze = state.get('maze') or {}
    w = _f(maze, 'w', 0) * TILE_M or 80.0
    h = _f(maze, 'h', 0) * TILE_M or 50.0
    diag = math.hypot(w, h)

    tanks = state.get('tanks') or {}
    me = tanks.get(str(pid)) or tanks.get(pid) or {}
    mx, my = _f(me, 'x'), _f(me, 'y')
    rot = _f(me, 'rot', _f(me, 'angle', 0.0))
    facing = rot - math.pi / 2.0        # 炮口世界角（见 02 文档 §4.2）

    vec = []
    layout = {}

    def section(name, values):
        layout[name] = [len(vec), len(vec) + len(values)]
        vec.extend(values)

    # ── self ──
    section('self', [
        _clamp(mx / w), _clamp(my / h),
        math.cos(rot), math.sin(rot),
        _clamp(_f(me, 'vx') / BULLET_SPEED), _clamp(_f(me, 'vy') / BULLET_SPEED),
        _clamp(_f(me, 'weapon', -1) / MAX_WEAPON),
        1.0 if me.get('shield') else 0.0,
        _clamp(_f(me, 'shieldLife') / 10.0),
        1.0 if me else 0.0,
    ])

    # ── round ──
    section('round', [
        1.0 if state.get('roundActive') else 0.0,
        1.0 if state.get('betweenRounds') else 0.0,
        1.0 if state.get('countingDown') else 0.0,
        1.0 if state.get('storm') else 0.0,
        _clamp(len(tanks) / max(1, max_tanks)),
    ])

    # ── score ──
    kills = state.get('scoresKill') or {}
    vict = state.get('scoresVict') or {}
    my_kill = _f(kills, str(pid))
    my_vict = _f(vict, str(pid))
    other_kill = max([_num(v) for k, v in kills.items() if str(k) != str(pid)] or [0.0])
    other_vict = max([_num(v) for k, v in vict.items() if str(k) != str(pid)] or [0.0])
    section('score', [
        _clamp(my_kill / 10.0), _clamp(my_vict / 10.0),
        _clamp(other_kill / 10.0), _clamp(other_vict / 10.0),
    ])

    # ── others（按距离升序） ──
    others = []
    for oid, t in tanks.items():
        if str(oid) == str(pid) or not isinstance(t, dict):
            continue
        dx = _f(t, 'x') - mx
        dy = _f(t, 'y') - my
        others.append((math.hypot(dx, dy), dx, dy, t))
    others.sort(key=lambda o: o[0])
    ovals = []
    for dist, dx, dy, t in others[:max_tanks]:
        bearing = math.atan2(dy, dx) - facing
        ovals.extend([
            _clamp(dx / w), _clamp(dy / h), _clamp(dist / diag),
            math.cos(bearing), math.sin(bearing),
            _clamp(_f(t, 'weapon', -1) / MAX_WEAPON),
            1.0 if t.get('alive', True) else 0.0,
        ])
    ovals.extend([0.0] * (OTHER_LEN * (max_tanks - len(others[:max_tanks]))))
    section('others', ovals)

    # ── projectiles（按剩余寿命升序：更紧急的在前） ──
    projs = []
    for p in (state.get('projectiles') or []):
        if not isinstance(p, dict):
            continue
        dx = _f(p, 'x') - mx
        dy = _f(p, 'y') - my
        projs.append((_f(p, 'lifetime', 999), dx, dy, p))
    projs.sort(key=lambda x: x[0])
    pvals = []
    for life, dx, dy, p in projs[:max_projectiles]:
        owner = p.get('owner')
        pvals.extend([
            _clamp(dx / w), _clamp(dy / h),
            _clamp(_f(p, 'vx') / BULLET_SPEED), _clamp(_f(p, 'vy') / BULLET_SPEED),
            _clamp(life / 10.0),
            1.0 if p.get('deadlyToOwner') else 0.0,
            1.0 if str(owner) == str(pid) else 0.0,
        ])
    pvals.extend([0.0] * (PROJ_LEN * (max_projectiles - len(projs[:max_projectiles]))))
    section('projectiles', pvals)

    # ── collectibles（按距离升序） ──
    colls = []
    for c in (state.get('collectibles') or []):
        if not isinstance(c, dict):
            continue
        dx = _f(c, 'x') - mx
        dy = _f(c, 'y') - my
        colls.append((math.hypot(dx, dy), dx, dy, c))
    colls.sort(key=lambda x: x[0])
    cvals = []
    for dist, dx, dy, c in colls[:max_collectibles]:
        cvals.extend([_clamp(dx / w), _clamp(dy / h), _clamp(dist / diag),
                      _clamp(_f(c, 'type', -1) / MAX_COLLECTIBLE)])
    cvals.extend([0.0] * (COLL_LEN * (max_collectibles - len(colls[:max_collectibles]))))
    section('collectibles', cvals)

    # ── mines（按距离升序） ──
    mines = []
    for m in (state.get('mines') or []):
        if not isinstance(m, dict):
            continue
        dx = _f(m, 'x') - mx
        dy = _f(m, 'y') - my
        mines.append((math.hypot(dx, dy), dx, dy, m))
    mines.sort(key=lambda x: x[0])
    mvals = []
    for dist, dx, dy, m in mines[:max_mines]:
        mvals.extend([_clamp(dx / w), _clamp(dy / h),
                      1.0 if m.get('activated') else 0.0,
                      1.0 if str(m.get('owner')) == str(pid) else 0.0])
    mvals.extend([0.0] * (MINE_LEN * (max_mines - len(mines[:max_mines]))))
    section('mines', mvals)

    # ── grid（可选） ──
    if grid_radius:
        section('grid', encode_grid(terrain, mx, my, grid_radius))

    return {
        'vector': vec,
        'size': len(vec),
        'version': OBS_VERSION,
        'layout': layout,
        'meta': {
            'pid': str(pid),
            'world': [w, h],
            'grid_radius': grid_radius,
            'counts': {
                'tanks': len(tanks), 'others_kept': min(len(others), max_tanks),
                'projectiles': len(projs), 'collectibles': len(colls), 'mines': len(mines),
            },
            'facing': facing,
        },
    }
