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
TankTrouble 弹道模拟器（Ballistics）
======================================
纯 Python 的炮弹物理推演模块 —— 不连接游戏，可离线调试/调参。

基于游戏源码 + 实测确认的物理参数：
  - 炮弹速度恒定 18 m/s（update() 每帧强制速度大小 = initialSpeed）
  - 弹丸寿命 10 秒（timeAlive >= lifetime 即消亡）
  - 反弹次数上限 5 次（bounces >= timeoutBounceCount 即消亡）
  - 撞墙 = 理想镜面反射（入射角 = 反射角）
  - 坦克碰撞箱近似半径 2m（3m × 4m 矩形取内切圆）
  - 1 格 = 10m（Constants.MAZE_TILE_SIZE）

用途：
  - 自动火控：计算命中目标的瞄准角（含反弹命中）
  - 弹道预演：判断某角度射击是否会被墙挡住
  - 数据生成：给模仿学习/奖励塑形提供"正确弹道"标签

墙段格式：[[x1, y1, x2, y2], ...]（米），与 gameutils.get_wall_segments() 一致。
"""
import math

# ─── 物理常量（游戏权威值）───
BULLET_SPEED = 18.0        # m/s 炮弹恒定速度
BULLET_LIFETIME = 10.0     # 秒 弹丸最大存活时间
MAX_BOUNCES = 5            # 次 最多反弹次数
TANK_RADIUS = 2.0          # m 坦克命中判定半径（3x4m 矩形近似）
MAZE_TILE_M = 10.0         # m 每格尺寸


def _seg_intersect(px, py, dx, dy, x1, y1, x2, y2):
    """射线(px,py 出发，方向(dx,dy))与线段(x1,y1)-(x2,y2)求交。
    返回 (t, ix, iy)：t=沿射线的距离，交点坐标；无交返回 None。
    t>0 表示交点在射线前方。"""
    rx, ry = x2 - x1, y2 - y1
    denom = dx * ry - dy * rx
    if abs(denom) < 1e-9:
        return None  # 平行
    t = ((x1 - px) * ry - (y1 - py) * rx) / denom
    u = ((x1 - px) * dy - (y1 - py) * dx) / denom
    if t < 1e-6 or u < 0.0 or u > 1.0:
        return None
    return t, px + t * dx, py + t * dy


def reflect(vx, vy, nx, ny):
    """向量 (vx,vy) 沿法线 (nx,ny) 镜面反射。法线应已归一化。
    返回反射后的 (rx, ry)。"""
    dot = vx * nx + vy * ny
    return vx - 2 * dot * nx, vy - 2 * dot * ny


WALL_HALF_THICK = 0.4  # 墙半厚（米）。Box2D 实测：x=50 墙右表面 50.4、左表面 49.6 → 厚 0.8m


def inflate_walls(walls, half=WALL_HALF_THICK):
    """把墙段从【中心线】膨胀成【两条碰撞面】。

    tiles 墙段是中心线（实测墙厚 0.8m，半厚 0.4m），弹丸真实碰撞在
    中心线 ±0.4m 处。用膨胀后的墙段做弹道推演可消除 ~0.4m 误差
    （多次反弹会累积成大偏移 → 打不中）。

    只处理轴对齐墙（游戏墙全是水平/垂直）。返回新的墙段列表：
    - 水平墙 [x1,y,x2,y] → [x1,y-h,x2,y-h] + [x1,y+h,x2,y+h]
    - 垂直墙 [x,y1,x,y2] → [x-h,y1,x-h,y2] + [x+h,y1,x+h,y2]
    """
    out = []
    for x1, y1, x2, y2 in walls:
        if y1 == y2:  # 水平
            out.append([x1, y1 - half, x2, y1 - half])
            out.append([x1, y1 + half, x2, y1 + half])
        elif x1 == x2:  # 垂直
            out.append([x1 - half, y1, x1 - half, y2])
            out.append([x1 + half, y1, x1 + half, y2])
        else:
            out.append([x1, y1, x2, y2])  # 非轴对齐，原样保留
    return out


def _seg_normal(seg, ix, iy, dx, dy):
    """求线段在交点处、指向射线【来向一侧】的法线（归一化）。

    用于镜面反射：法线应指向射线来向（这样反射后远离墙面）。
    两条候选法线中，取与入射方向 (dx,dy) 点积为负的那个。
    """
    x1, y1, x2, y2 = seg
    sx, sy = x2 - x1, y2 - y1
    ln = math.hypot(sx, sy)
    if ln < 1e-9:
        return 0.0, 1.0
    sx, sy = sx / ln, sy / ln
    # 两条候选法线（±90° 旋转）
    cands = ((-sy, sx), (sy, -sx))
    # 选与入射方向反向的那条（点积 < 0 → 反射后远离）
    best = cands[0]
    best_dot = dx * best[0] + dy * best[1]
    for nx, ny in cands[1:]:
        d = dx * nx + dy * ny
        if d < best_dot:
            best, best_dot = (nx, ny), d
    return best


def trace_ray(px, py, angle, walls, max_bounces=MAX_BOUNCES,
              max_dist=BULLET_SPEED * BULLET_LIFETIME, max_steps=8):
    """推演一条弹道路径。

    参数：
      px, py    起点（米）
      angle     初始朝向角（弧度，游戏 getRotation 约定）
      walls     墙段列表 [[x1,y1,x2,y2],...]（中心线，自动膨胀补偿墙厚）
      max_bounces 最大反弹次数
      max_dist  最大飞行距离（默认 18m/s * 10s = 180m）
      max_steps 步数上限保护（防死循环）

    返回：
      [(x1,y1), (x2,y2), ...]  弹道折点路径（含起点和每个反弹点）
      None                      参数错误
    """
    if walls is None:
        return None
    # 墙段膨胀：中心线 → 两条碰撞面（补偿 Box2D 墙厚 0.8m）
    walls = inflate_walls(walls)
    dirx, diry = math.cos(angle), math.sin(angle)
    path = [(px, py)]
    bounce = 0
    traveled = 0.0
    for _ in range(max_steps):
        # 找最近的交点
        best_t = None
        best_seg = None
        for seg in walls:
            hit = _seg_intersect(px, py, dirx, diry, *seg)
            if hit and (best_t is None or hit[0] < best_t):
                best_t, ix, iy = hit
                best_seg = seg
        if best_t is None:
            # 无交点：直线飞到底（受 max_dist 限制）
            if traveled >= max_dist:
                break
            rem = max_dist - traveled
            path.append((px + dirx * rem, py + diry * rem))
            break
        if best_t > max_dist - traveled:
            # 交点超出最大距离：在 max_dist 处终止
            path.append((px + dirx * (max_dist - traveled), py + diry * (max_dist - traveled)))
            break
        # 到达交点
        traveled += best_t
        path.append((ix, iy))
        if bounce >= max_bounces:
            break  # 反弹次数用完，弹丸在此消亡
        # 镜面反射（Box2D 理想反弹）
        nx, ny = _seg_normal(best_seg, ix, iy, dirx, diry)
        dirx, diry = reflect(dirx, diry, nx, ny)
        # 沿法线方向微推远离墙面，避免数值误差立即再撞同一面墙
        px = ix + nx * 1e-2
        py = iy + ny * 1e-2
        # 防止反射后仍指向墙内（法线选错或共线）→ 循环保护
        if abs(dirx) < 1e-9 and abs(diry) < 1e-9:
            break
        bounce += 1
    return path


def point_to_seg_dist(px, py, x1, y1, x2, y2):
    """点 (px,py) 到线段 (x1,y1)-(x2,y2) 的距离。"""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - cx, py - cy)


def path_hits_target(path, tx, ty, radius=TANK_RADIUS, sample_step=0.5):
    """判断弹道路径是否命中目标（点 (tx,ty) 附近 radius 内）。

    对路径每段做密集采样，若某采样点到目标距离 < radius 则命中。
    返回 (hit: bool, hit_point: (x,y) 或 None)。
    """
    for i in range(len(path) - 1):
        x1, y1 = path[i]
        x2, y2 = path[i + 1]
        seg_len = math.hypot(x2 - x1, y2 - y1)
        if seg_len < 1e-6:
            continue
        n = max(1, int(seg_len / sample_step))
        for k in range(n + 1):
            fx = x1 + (x2 - x1) * k / n
            fy = y1 + (y2 - y1) * k / n
            if math.hypot(fx - tx, fy - ty) <= radius:
                return True, (fx, fy)
    return False, None


def path_hits_self(path, px, py, radius=TANK_RADIUS, sample_step=0.5,
                   self_radius=None):
    """判断弹道路径是否会【反弹回来打到发射者自己】。

    排除起点附近（弹丸从发射者炮口出发，起点即自身位置），检查后续
    路径段（尤其是反弹折点之后）是否回到发射者附近。

    坦克命中判据是 3m×4m 的【可旋转矩形】。用固定圆(radius)判断会在坦克
    转过 45°（角顶伸到 sqrt(1.5²+2²)=2.5m）时漏判自伤。len(path) 沿任意
    朝向的最大内切半径 = 半对角线（2.5m），再加弹丸半径 0.25m 得保守半径
    self_radius（默认 4.0m，见下），任何朝向的坦克碰撞都会被覆盖。

    但对多反弹"回程贴近射手"的路径，官方射线(零宽)与真实弹丸(有半径)在
    坦克转向/目标移动时会产生 ~1m 偏差。实测命中点在 ~2.7~2.8m 的路径
    (理论 2.75m) 也会自伤。因此留安全余量：任何回程进入
    self_radius(默认 4.0m) 的自伤一律拒绝，宁可不开火也绝不自杀。

    注意：形参 `radius` 为历史遗留，函数体不使用它——自伤半径由
    `self_radius` 决定（默认 4.0）。传 `radius` 不会改变判定。

    返回 True 表示该角度有自伤风险，火控应避开。"""
    sr = self_radius if self_radius is not None else 4.0
    # 出膛段跳过（muzzle clearance）：弹丸从坦克中心出发，需飞过 ~坦克
    # 半对角线(2.5m)才脱离自身身位。skip 必须 >= sr，否则出发段的点
    # (euclidean 距离随弧长线性增长) 会被误判为自伤。取 sr+0.2 余量。
    skip = sr + 0.2
    acc = 0.0  # 累计弧长
    for i in range(len(path) - 1):
        x1, y1 = path[i]
        x2, y2 = path[i + 1]
        seg_len = math.hypot(x2 - x1, y2 - y1)
        if seg_len < 1e-6:
            continue
        n = max(1, int(seg_len / sample_step))
        for k in range(n + 1):
            fx = x1 + (x2 - x1) * k / n
            fy = y1 + (y2 - y1) * k / n
            seg_acc = seg_len * k / n
            if acc + seg_acc < skip:
                continue
            if math.hypot(fx - px, fy - py) <= sr:
                return True
        acc += seg_len
    return False


def predict_hit(px, py, angle, walls, tx, ty, radius=TANK_RADIUS):
    """预测从 (px,py) 以 `angle` 开火能否命中目标 (tx,ty)。

    返回 (hit, path, hit_point)：
      hit       True/False 是否命中
      path      弹道路径（供调试/渲染）
      hit_point 命中坐标（未命中为 None）
    """
    path = trace_ray(px, py, angle, walls)
    if path is None:
        return False, None, None
    hit, hp = path_hits_target(path, tx, ty, radius)
    return hit, path, hp


def aim_angle_straight(px, py, tx, ty):
    """直线瞄准【方向角】：从 (px,py) 射向 (tx,ty) 的方向角。

    返回 forward_angle（弹道方向角，trace_ray 用这个）。
    开火时需转成游戏 rot：rot = forward_angle + π/2。
    """
    return math.atan2(ty - py, tx - px)
