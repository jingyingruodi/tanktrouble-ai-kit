"""
坦克行为控制模板（Tank Behavior Template）
============================================
这是编写 TankTrouble AI 行为脚本的【空白模板】。已封装好常用行为
函数（可操控角色获取 / set 控制 / get 查询 / 地形墙体数据），
每个函数带完整中文注释。写新脚本时复制本文件，在 main() 里组合行为即可。

核心 API 分层：
  - TankCtl（cdp_tools/tankctl.py）：单实例高层控制，全部走 setInputState（在线安全）
  - gameutils（cdp_tools/gameutils.py）：共享游戏状态查询（回合/角色/迷宫/墙体）
  - ballistics（cdp_tools/ballistics.py）：纯 Python 弹道模拟器（反弹/命中预测）
  - 本模板：在它们之上再包一层"行为层"，方便组合复用

地形数据（gameutils 提供）：
  get_maze_tiles()   原始格 tiles[y][x] = [present, topWall, leftWall]
  get_wall_segments()全部墙段（米，含外边界+内部墙）
  get_terrain()      完整地形摘要（网格+墙段+毒圈）
  extract_storm_zones(terrain)  从地形摘要里单独筛出毒圈
  is_open_tile()     格子可通行性
  open_tile_center() 可通行格中心坐标（传送/寻路目标）
  plan_maze_path()   迷宫寻路（游戏 getShortestPathWithGraph）
  maze_distance()    迷宫 Dijkstra 距离（格数）
毒圈（实时）：get_storm_zones() / get_storm_safe_area() / is_in_safe_area()
弹道（ballistics 提供，纯计算无 IO）：
  trace_ray()        推演弹道路径（含镜面反弹）
  predict_hit()      判断某朝向能否命中目标
  aim_angle_straight() 直线瞄准方向角
火控（权威 Box2D RayCast）：
  find_aim_angle_raycast()  用游戏官方弹道找命中角（选 traveled 最短，bounces 1~4）
  find_aim_angle_at()       任意位置检查能否命中（机动射击位评估）
  predict_lead_target()     移动目标预测（lead targeting）
  aim_and_fire(..., lead=True)  自动瞄准开火（官方弹道+防超圈转向+朝向校验）
  get_active_weapon()       查询坦克当前武器类型（加特林预热适配）
转向（防超圈）：
  turn_to_rot()      用【未归一化原始累积角】引导转向（误差≤1.7°，不超圈）
  _raw_rot()         读未归一化原始旋转值（游戏 getRotation 是累积值）
导航：
  navigate_to()      迷宫寻路导航（绕墙，卡墙沿墙滑行）
其他：
  suicide_by_bounce()  朝最近外墙开火自杀（武器感知，结束本地局回合）

在线安全说明：
  所有 set 行为（移动/转向/开火）都通过 TankCtl.set_input()
  （InputState -> setInputState），与真实键盘输入同路径，服务器认可。
  直接调 tank.setXxx 在线模式会回弹/开火无效——本模板一律不用。

用法：
  python tank_behavior_template.py [port] [tank_index]
"""
import asyncio
import json
import math
import os
import sys

# 无论从项目根目录还是 scripts/ 下运行，都能 import 到 cdp_tools
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))
from tankctl import TankCtl
from gameutils import (
    wrap_pi,                 # 角度归一化到 [-pi, pi)
    get_round_state,         # 回合状态（权威）
    round_active,            # 是否回合进行中
    get_controllable_tanks,  # 可操控角色（权威静态识别）
    tank_alive,              # 坦克是否在场
    get_maze_dims,           # 迷宫世界尺寸（米）
    find_nearest_wall,       # 最近墙（含内部墙）+ 正对朝向
    destroyed_tanks,         # 已标记摧毁的玩家
    get_maze_tiles,          # 迷宫原始格数据 tiles[y][x]
    get_wall_segments,       # 全部墙体墙段（米，含外边界）
    get_terrain,             # 完整地形摘要（网格+墙段）
    is_open_tile,            # 格子是否可通行
    open_tile_center,        # 可通行格的中心世界坐标
    plan_maze_path,          # 迷宫寻路（游戏 getShortestPathWithGraph）
    maze_distance,           # 迷宫 Dijkstra 距离（格数）
    get_storm_zones,         # 毒圈状态列表
    get_storm_safe_area,     # 毒圈安全区 tile 范围
    is_in_safe_area,         # 坐标是否在安全区内
    extract_storm_zones,     # 从地形摘要里筛出毒圈数据
    TILE_M,                  # 每格米数（10m）
    TURN_RATE,               # 转向速度 rad/s
)
from ballistics import (
    BULLET_SPEED,            # 炮弹速度 18 m/s
    BULLET_LIFETIME,         # 弹丸寿命 10s
    MAX_BOUNCES,             # 反弹上限 5 次
    TANK_RADIUS,             # 坦克命中半径 2m
    trace_ray,               # 弹道路径推演（含镜面反弹）
    predict_hit,             # 某朝向能否命中目标
    aim_angle_straight,      # 直线瞄准方向角
    path_hits_self,          # 路径是否反弹回来打到自己
)

# ─── 武器类型（与游戏 Constants.WEAPON_TYPES 对应）───
WEAPON_TYPES = {
    -1: 'BULLET', 0: 'LASER', 1: 'DOUBLE_BARREL', 2: 'SHOTGUN',
    3: 'HOMING_MISSILE', 4: 'MINE', 5: 'GATLING_GUN',
}
GATLING_CHARGE_TIME = 0.5    # 加特林预热充能时间（秒）
GATLING_FIRE_RATE = 0.12     # 加特林出弹间隔（秒）
GATLING_SPEED = 27.5         # 加特林弹速中值（25~30 m/s 随机）


class TankBehavior:
    """坦克行为封装：把所有常用动作包成方法，组合即可写 AI 行为。

    用法：
        bhv = TankBehavior(port=9222)
        await bhv.connect()
        roles = await bhv.get_roles()           # {playerId: 'keyboard'|...}
        await bhv.fire(pid)                      # 指定角色开火
        await bhv.move_toward(pid, x, y)         # 指定角色驶向某点
    """

    # ───────────── 连接 / 生命周期 ─────────────

    def __init__(self, port=9222, role='keyboard'):
        """初始化。port 是 CDP 调试端口；role 仅在极少数场景用于键位预设。"""
        self.ctl = TankCtl(port=port, role=role)
        self.port = port
        self._roles = {}

    async def connect(self, url='tanktrouble.com'):
        """连接游戏页面。url 通常保持默认。"""
        await self.ctl.connect(url)

    async def close(self):
        """释放连接（如果有需要的话）。目前 CDP 无需显式关闭。"""
        pass

    # ───────────── 可操控角色获取（GET）─────────────

    async def get_roles(self, refresh=True):
        """获取所有本地可控坦克：{playerId: 'keyboard'|'arrows'|'mouse'}。

        权威来源：游戏自身的输入管理器（window.Inputs.__metaclass.inputManagers）。
        - 本地局：3 个角色；在线局：只有本地玩家的坦克。
        - 刷新稳定，回合切换/页面刷新后依然正确。
        返回 {playerId: role} 字典。
        """
        if refresh or not self._roles:
            self._roles = await get_controllable_tanks(self.ctl)
        return self._roles

    async def pick_role(self, prefer='keyboard'):
        """从可控角色里选一辆坦克的 playerId。

        prefer 优先要的角色名（'keyboard'/'arrows'/'mouse'）；没有则取第一个。
        返回 playerId 字符串；无可控坦克返回 None。
        """
        roles = await self.get_roles()
        if not roles:
            return None
        if prefer in roles.values():
            for pid, role in roles.items():
                if role == prefer:
                    return pid
        return next(iter(roles))

    async def pick_role_strict(self, role='arrows'):
        """【严格】按控制类型选角色，找不到就返回 None（不回退到别的）。

        role 取值：'keyboard'（WASD/Q 键）、'arrows'（方向键/Space）、'mouse'。
        用于需要固定测试变量/指定操控方式的场景。

        示例：
          arrows_pid = await bhv.pick_role_strict('arrows')   # 方向键角色
          wasd_pid   = await bhv.pick_role_strict('keyboard') # WASD 角色
          if not arrows_pid or not wasd_pid:
              print('缺少指定控制类型的角色')
        """
        roles = await self.get_roles()
        for pid, r in roles.items():
            if r == role:
                return pid
        return None

    # ───────────── 状态查询（GET）─────────────

    async def get_tanks(self):
        """获取所有坦克状态：{playerId: {x, y, rot, alive}}（米/弧度）。"""
        return await self.ctl.tanks()

    async def get_tank(self, tank_id):
        """获取指定坦克的状态字典；不存在返回 None。

        注意：rot 会归一化到 [-π, π)。游戏里 getRotation() 是累积值
        （可到几百上千度），而火控/转向用的目标角是 [-π,π]，
        必须统一归一化才能正确比较。"""
        ts = await self.get_tanks()
        t = ts.get(tank_id)
        if not t:
            return None
        # 复制并归一化 rot（累积角 → [-π, π)）
        t = dict(t)
        if 'rot' in t:
            t['rot'] = (t['rot'] + math.pi) % (2 * math.pi) - math.pi
        return t

    async def get_tank_velocity(self, tank_id):
        """精确读指定坦克的 B2D 线速度 (vx, vy)（米/秒）。

        从 game 直接取 body.GetLinearVelocity()，比两点采样滞后小、精度高。
        目标不在场返回 None；读失败返回 None（调用方会退化到采样）。"""
        try:
            v = await self.ctl.cdp.eval(
                f'(() => {{ const gc=GameManager.getGameController()||GameManager.gameController;'
                f' const t=gc.getTanks()["{tank_id}"];'
                f' const b=(t&&t.getB2DBody)?t.getB2DBody():null;'
                f' const v=b?b.GetLinearVelocity():null;'
                f' return v ? JSON.stringify({{x: v.x, y: v.y}}) : "null"; }})()')
            import json as _json
            d = _json.loads(v) if isinstance(v, str) else v
            if d and 'x' in d:
                return (float(d.get('x', 0)), float(d.get('y', 0)))
            return None
        except Exception:
            return None

    async def get_round_state(self):
        """获取回合状态字符串（权威）：
        'in round' | 'between rounds' | 'counting down' | 'ended' | 'none'。"""
        return await get_round_state(self.ctl)

    async def is_round_active(self):
        """当前是否在对局中（in round）。"""
        return await round_active(self.ctl)

    async def is_alive(self, tank_id):
        """指定坦克是否仍在场（getTanks() 中存在）。"""
        return await tank_alive(self.ctl, tank_id)

    async def get_position(self, tank_id):
        """获取坦克位置：返回 (x, y)（米）；不在场返回 None。"""
        t = await self.get_tank(tank_id)
        if not t:
            return None
        return (t['x'], t['y'])

    async def get_rotation(self, tank_id):
        """获取坦克朝向弧度；不在场返回 None。"""
        t = await self.get_tank(tank_id)
        return t['rot'] if t else None

    async def get_active_weapon(self, tank_id):
        """获取指定坦克当前武器类型（Constants.WEAPON_TYPES）：
        -1=基础弹 0=激光 1=双管 2=霰弹 3=导弹 4=地雷 5=加特林。

        加特林有 0.5s 预热，点射打不出子弹，火控需感知武器
        （见 aim_and_fire 的武器分支）。查询失败返回 None。"""
        try:
            v = await self.ctl.cdp.eval(
                f'(GameManager.getGameController()).getActiveWeapon("{tank_id}").getType()')
            return int(v)
        except Exception:
            return None

    async def get_maze_size(self):
        """获取迷宫世界尺寸 (width, height)（米）。每格 10m。"""
        return await get_maze_dims(self.ctl)

    async def get_projectiles(self):
        """获取当前弹丸列表 [{id, x, y, vx, vy, owner}]。"""
        st = await self.ctl.state()
        return st.get('projectiles', []) if isinstance(st, dict) else []

    async def get_destroyed(self):
        """获取本回合已标记摧毁的玩家 id 列表。"""
        return await destroyed_tanks(self.ctl)

    async def get_nearest_wall(self, tank_id):
        """获取指定坦克最近的墙信息：
        返回 (wall, dist_m, target_rot)。wall 为 'top'/'bottom'/'left'/
        'right'/'nearest'，target_rot 是正对墙的朝向。"""
        pos = await self.get_position(tank_id)
        if not pos:
            return None
        w, h = await self.get_maze_size()
        x, y = pos
        return await find_nearest_wall(self.ctl, x, y, w, h)

    # ───────────── 地形 / 墙体数据（GET）─────────────

    async def get_maze_tiles(self):
        """获取迷宫原始格数据。

        返回 dict：
          {w: 宽(格), h: 高(格),
           tiles: 二维数组 tiles[y][x] = [present, topWall, leftWall]}
          - present  : 1 = 可通行地面格，0 = 封闭格
          - topWall  : 1 = 该格上边界（-y）有墙
          - leftWall : 1 = 该格左边界（-x）有墙
        迷宫不存在时返回 None。每回合重新生成，务必实时读取。"""
        return await get_maze_tiles(self.ctl)

    async def get_wall_segments(self):
        """获取全部墙体墙段列表（米为单位）。

        返回 [[x1, y1, x2, y2], ...]，每条为一条线段端点，
        包含外边界 + 所有内部墙。迷宫不存在时返回 None。
        可用于碰撞检测 / 寻路 / 渲染。"""
        return await get_wall_segments(self.ctl)

    async def get_terrain(self):
        """获取完整地形摘要（一次拿网格 + 墙段 + 毒圈）。

        返回 dict：
          {w, h,                       # 格子宽高
           world_w, world_h,           # 世界尺寸（米）
           tiles: [[present,top,left],...],  # tiles[y][x]
           walls: [[x1,y1,x2,y2],...],       # 全部墙段（米）
           zones: [{id, minX...}, ...]}      # 毒圈列表（动态收缩）
        迷宫不存在时返回 None。
        只需毒圈时用 self.extract_storm_zones(terrain) 筛出。"""
        return await get_terrain(self.ctl)

    @staticmethod
    def extract_storm_zones(terrain):
        """从 get_terrain() 结果里筛出毒圈数据（纯函数，无需连接）。

        参数 terrain：get_terrain() 的返回值。
        返回毒圈列表；terrain 为空/无 zones 时返回 []。

        示例：
            terr = await bhv.get_terrain()
            zones = bhv.extract_storm_zones(terr)   # 单独取毒圈
            walls = terr['walls']                   # 单独取墙段
        """
        return extract_storm_zones(terrain)

    async def is_open_tile(self, tx, ty):
        """判断迷宫格 (tx, ty)（格子坐标）是否可通行。
        返回 True/False。"""
        return await is_open_tile(self.ctl, tx, ty)

    async def open_tile_center(self, tx, ty):
        """返回可通行格 (tx, ty) 的中心世界坐标 (x, y)（米）。
        该格不可通行时返回 None。传送/寻路目标应落在开放格中心。"""
        return await open_tile_center(self.ctl, tx, ty)

    # ───────────── 毒圈（Radioactive Fog）数据（GET，实时）─────────────
    # ⚠️ 与地形不同：毒圈随时间动态收缩，每次决策都要实时读取，不能缓存。

    async def get_storm_zones(self):
        """获取所有毒圈当前状态。返回列表：
          [{id, minX, minY, maxX, maxY,        # 地图 tile 边界
            expLeft, expTop, expRight, expBottom,  # 四边已收缩 tile 数
            side}, ...]                        # 0=右 1=下 2=左 3=上，-1=等待
        没有毒圈返回空列表。动态变化，实时读取。"""
        return await get_storm_zones(self.ctl)

    async def get_storm_safe_area(self):
        """计算当前毒圈安全区的 tile 范围（可存活区域）。
        返回 {minX, minY, maxX, maxY}（tile 坐标）或 None（无毒圈）。
        安全区随缩圈动态缩小，需实时读取。"""
        return await get_storm_safe_area(self.ctl)

    async def is_in_safe_area(self, x, y):
        """判断世界坐标 (x, y)（米）是否在毒圈安全区内。
        返回 True/False。无毒圈数据时保守返回 True。"""
        return await is_in_safe_area(self.ctl, x, y)

    # ───────────── 火控（弹道计算，纯数学无 IO）─────────────

    async def get_walls_cached(self, refresh=False):
        """获取墙段并缓存（每回合只读一次，之后复用）。
        refresh=True 强制重读（回合切换时调用）。"""
        if refresh or getattr(self, '_walls_cache', None) is None:
            self._walls_cache = await get_wall_segments(self.ctl)
        return self._walls_cache

    def predict_shot(self, px, py, angle, tx, ty, radius=TANK_RADIUS):
        """判断从 (px,py) 以 `angle`（方向角）开火能否命中目标 (tx,ty)。

        纯计算，不连接游戏。返回 (hit, path, hit_point)：
          hit       是否命中
          path      弹道路径折点（可调试/可视化）
          hit_point 命中坐标（未命中 None）
        使用 ballistics 的镜面反弹推演（最多 5 次反弹）。"""
        return predict_hit(px, py, angle, self._walls_cache or [], tx, ty, radius)

    def aim_straight(self, px, py, tx, ty):
        """计算从 (px,py) 直线射向 (tx,ty) 的方向角。"""
        return aim_angle_straight(px, py, tx, ty)

    async def find_aim_angle(self, tank_id, target_pos, radius=TANK_RADIUS,
                             sample_count=180):
        """为指定坦克寻找能命中目标的开火方向角。

        策略：先试直线；直线被墙挡则按 2° 步进扫描 360°，找第一个
        能命中的反弹角度。返回方向角（开火时需 +π/2 转成游戏 rot）；
        找不到返回 None。

        安全：会跳过【反弹回来打到自己】的角度（弹丸能伤害发射者，
        反弹路径若经过自己位置会自伤）。

        参数：
          tank_id       开火坦克
          target_pos    目标 (tx, ty)
          sample_count  角度采样数（默认 180 = 每 2°）
        """
        walls = await self.get_walls_cached()
        if not walls:
            return None
        pos = await self.get_position(tank_id)
        if not pos:
            return None
        px, py = pos
        # 先试直线（命中概率最高，且直线不会打自己）
        straight = aim_angle_straight(px, py, target_pos[0], target_pos[1])
        hit, path, _ = predict_hit(px, py, straight, walls, target_pos[0], target_pos[1], radius)
        if hit and not path_hits_self(path, px, py, radius):
            return straight
        # 扫描反弹角度（排除自伤角）
        for i in range(sample_count):
            ang = (math.pi * 2 * i) / sample_count
            hit, path, _ = predict_hit(px, py, ang, walls, target_pos[0], target_pos[1], radius)
            if hit and not path_hits_self(path, px, py, radius):
                return ang
        return None

    async def _raycast_path(self, px, py, ang_deg, tx, ty, radius=TANK_RADIUS,
                            max_bounces=MAX_BOUNCES, max_dist=BULLET_SPEED * BULLET_LIFETIME,
                            proj_radius=0.25, target_tank_id=None, origin_offset=0.0):
        """用游戏官方弹道算法（B2DUtils.calculateProjectilePath）做权威推演。

        官方算法 = 游戏实际使用的弹道路径计算（AI/瞄准线/服务器都用它），
        与真实弹丸物理完全一致（Box2D RayCast + 镜面反射 + 坦克碰撞判定），
        零误差。比 tiles 墙段推导精确得多。

        target_tank_id 传目标坦克 id 时：
          命中目标坦克 → hit=True；撞到其他坦克 → hit=False（角度不可用）。

        origin_offset 弹丸**出膛点**相对车体中心的偏移（米，沿射击方向）。
        真实弹丸从炮口出膛（约 2m 前），而这里默认从车体中心算 —— 对多次
        反弹路径，起点偏移会连带平移每个反弹点，是"预测安全、真实自伤/打偏"
        的主要误差源。传 ~2.0 可让推演与真实弹道对齐（炮口出膛建模）。

        返回 dict：{hit, hitPoint, path, traveled, hitTank, bounces}。
        bounces = 反弹次数（官方 path 的 [起点,反弹点...,命中点] 结构，
        命中点不计入反弹，所以 bounces = 点数-2，直线命中 = 0）。
        """
        target_arg = f'"{target_tank_id}"' if target_tank_id else 'null'
        # 出膛点：车体中心沿射击方向前移 origin_offset（默认 0 = 车体中心）
        if origin_offset:
            _r = math.radians(ang_deg)
            px = px + origin_offset * math.cos(_r)
            py = py + origin_offset * math.sin(_r)
        # 官方弹道：B2DUtils.calculateProjectilePath(world, proj, maxBounces, maxLength, collideWithTanks)
        # 用速度向量方向表示角度（弹速 18m/s）
        js = f"""(() => {{
  const gc = GameManager.getGameController()||GameManager.gameController;
  const ts = gc.getTanks();
  const tank = ts[Object.keys(ts)[0]];
  const world = tank.getB2DBody().GetWorld();
  const ang = {ang_deg} * Math.PI / 180;
  const speed = {BULLET_SPEED};
  const vx = Math.cos(ang) * speed, vy = Math.sin(ang) * speed;
  const proj = {{
    getB2DBody: () => ({{
      GetPosition: () => Box2D.Common.Math.b2Vec2.Make({px}, {py}),
      GetLinearVelocity: () => Box2D.Common.Math.b2Vec2.Make(vx, vy)
    }})
  }};
  const res = B2DUtils.calculateProjectilePath(world, proj, {max_bounces}, {max_dist}, true);
  const out = {{hit: !!res.hit, hitPoint: null, path: [], traveled: 0, hitTank: null, bounces: 0}};
  for (const p of res.path) out.path.push({{x: p.x, y: p.y}});
  // 实际旅行长度 = 相邻路径点间距之和（弹丸沿此路径飞行，飞行时间=traveled/弹速）
  if (res.path.length >= 2) {{
    let tr = 0;
    for (let i = 1; i < res.path.length; i++) {{
      tr += Math.hypot(res.path[i].x - res.path[i-1].x, res.path[i].y - res.path[i-1].y);
    }}
    out.traveled = tr;
  }}
  if (res.hit && res.hit.getPlayerId) {{
    out.hitTank = res.hit.getPlayerId();
    const last = res.path[res.path.length-1];
    out.hitPoint = last ? {{x: last.x, y: last.y}} : null;
    // 官方 path = [起点, 反弹点..., 命中点]；反弹次数 = 点数-2（去掉起点和命中点）
    out.bounces = Math.max(0, res.path.length - 2);
    const targetId = {target_arg};
    out.hit = (targetId === null || out.hitTank === targetId);
  }}
  return JSON.stringify(out);
}})()"""
        res = await self.ctl.cdp.eval(js)
        d = json.loads(res) if isinstance(res, str) else res
        return d if isinstance(d, dict) else {'hit': False, 'path': []}

    async def find_aim_angle_raycast(self, tank_id, target_pos, radius=TANK_RADIUS,
                                     sample_count=180, pre_filter=True, target_id=None,
                                     require_bounce=False, verbose=False,
                                     max_traveled=None, self_radius_slope=0.0,
                                     rotated_hit_check=False,
                                     exhaustive_fallback=False, muzzle_offset=0.0):
        """权威自动瞄准：用 Box2D RayCast 找能命中目标的开火角。

        比 find_aim_angle（tiles 推导）精确，适合对命中率要求高的场景。
        先试直线；直线不通则扫描 360°（每 sample_count 步）用物理射线
        验证反弹命中。返回方向角（需 +π/2 转游戏 rot）；无解返回 None。

        选角策略（本次优化后）：枚举所有【可靠命中】角度（bounces 1~4，
        排除自伤、排除弹丸中心擦边未真正进入目标矩形），选【旅行距离
        traveled 最短】的一条返回。理由：
          - 路径越短 -> 飞行时间越短 -> 弹丸越不易错过移动目标
            （配合 predict_lead_target 的移动预判，命中率更高）
          - 路径越短 -> 越不易反弹回到射手自身 -> 降低自杀风险
          - 用户要求"原地多次反弹能命中就别移动"：一次在当前位置找全
            所有可靠命中角，把最短路径挂到"原地优先开火"上；只有全无
            命中角才机动换位。

        参数：
          pre_filter  先用 tiles 快速筛出候选角（减少 RayCast 次数），
                      再对候选做权威验证。默认开启。
          target_id   目标坦克 id（可选）。传了则射线撞到坦克 body 直接判定
                      （命中目标坦克才算命中，撞到别的坦克算不可用）。
          require_bounce  为 True 时要求至少 1 次墙反弹（排除直射）。
          max_traveled    可选：弹丸**实际飞行长度**上限（米）。多反弹路径越
                          长，官方零宽射线与真实弹丸的偏差越大、飞行时间越长
                          （移动目标越容易躲开）→ 超过该值的命中角直接丢弃，
                          宁可判定"此处无解"去换位。None = 不限制（默认，
                          保持历史行为）。
          self_radius_slope 自伤判定半径 = min(4.0 + slope×反弹数, 8.0)。
                          默认 0.0 = 固定 4.0m（**30 回合验收通过时的配置**）。
                          取 0.5 可多挡一类"反弹累积偏差"自伤，但会减少可用角
                          （实测跳过率 14%→25%），属**未重新验收**的调优项。
          rotated_hit_check 命中判定是否用坦克本地坐标系（真实矩形朝向）。
                          True 更准确（能救回斜置坦克上本可命中的角），但会改变
                          可选角集合，属**未重新验收**的调优项；默认 False =
                          与 30 回合验收时一致。
          exhaustive_fallback 预筛无解时是否再做全量权威扫描。默认 False =
                          与验收时一致；True 能救回预筛漏掉的解，但同样改变
                          可选角集合。
          muzzle_offset   弹丸出膛点相对车体中心的偏移（米，沿射击方向）。
                          默认 0.0 = 从车体中心推演（=验收时的行为）；
                          取 ~2.0 = **炮口出膛建模**，让推演与真实弹道起点一致，
                          减少"多反弹路径预测偏差 → 打偏/自伤"。属模型正确性
                          改动，需重新跑 30 回合验收后才能转默认。
        """
        pos = await self.get_position(tank_id)
        if not pos:
            return None
        px, py = pos
        tx, ty = target_pos

        # 候选角度：直线（可选）+ tiles 预筛（按反弹次数升序，优先少反弹）
        cands = []
        straight_deg = math.degrees(aim_angle_straight(px, py, tx, ty))
        if not require_bounce:
            cands.append((straight_deg, 0))  # 直射允许时直线先试（命中率最高）
        if pre_filter:
            walls = await self.get_walls_cached()
            if walls:
                for i in range(sample_count):
                    ang = (math.pi * 2 * i) / sample_count
                    hit, path, _ = predict_hit(px, py, ang, walls, tx, ty, radius)
                    if hit and not path_hits_self(path, px, py, radius):
                        # 反弹次数 ≈ path 中 bounce 点数（含起点）
                        bounces = max(0, len(path) - 1)
                        if require_bounce and bounces < 1:
                            continue  # 要求反弹，过滤直射
                        cands.append((math.degrees(ang), bounces))
            # 按反弹次数升序（少反弹的路径对物理误差更鲁棒）
            cands.sort(key=lambda c: c[1])
            # 预筛候选不足时补全角度（避免 tiles 误判漏掉真角度）
            if len(cands) < 16:
                cands = [(i * 360.0 / sample_count, 0) for i in range(sample_count)]
                if not require_bounce:
                    cands.insert(0, (straight_deg, 0))
        else:
            cands = [(i * 360.0 / sample_count, 0) for i in range(sample_count)]

        # 官方弹道验证。优先选【旅行距离最短】的命中角：
        # 路径越短旅行时间越短，弹丸越不易错过移动目标，也越不易被反弹自伤。
        # 用户要求：原地多次反弹能命中就不要移动——这里在当前位置一次找全所有
        # 可靠命中角，再把最短路径挂到"原地优先开火"上；只有全无命中角才机动。
        best = None          # (traveled, deg)
        best_path = None
        best_bounces = None
        best_d = None

        # 目标坦克朝向：仅在 rotated_hit_check=True 时使用。
        # 坦克是 3×4m 可旋转矩形；轴对齐盒子会把"斜着的坦克"上真实可命中的
        # 入射点判成擦边而丢弃（实测能救回角度）。但这会改变可选角集合，
        # 而 30 回合验收是在轴线盒子下取得的，故默认关闭、按需开启。
        target_rot = None
        if rotated_hit_check and target_id is not None:
            try:
                target_rot = await self.get_rotation(target_id)
            except Exception:
                target_rot = None

        def _enters_target(fx, fy):
            """弹丸中心点是否进入【坦克矩形 + 弹丸半径】的膨胀矩形。"""
            dx, dy = fx - tx, fy - ty
            if target_rot is None:
                return abs(dx) < 1.75 and abs(dy) < 2.25
            c, s = math.cos(-target_rot), math.sin(-target_rot)
            lx = dx * c - dy * s
            ly = dx * s + dy * c
            return abs(lx) < 1.75 and abs(ly) < 2.25

        async def _scan(candidates):
            """对候选角度做权威弹道验证，更新 best*（就近选 traveled 最短）。"""
            nonlocal best, best_path, best_bounces, best_d
            for deg, _b in candidates:
                d = await self._raycast_path(px, py, deg, tx, ty, radius,
                                             target_tank_id=target_id,
                                             origin_offset=muzzle_offset)
                if not d.get('hit'):
                    continue
                # 排除反弹回来打自己的角度（弹丸能伤害发射者）。
                # 半径 = 4.0 + slope×反弹数（默认 slope=0 → 固定 4.0，即 30 回合
                # 验收通过时的配置）。slope>0 可多挡"多反弹累积偏差"造成的自伤
                # （实测 3 反弹可偏 >2m），代价是可用角变少、跳过率上升。
                path = d.get('path', [])
                _bn = int(d.get('bounces', max(0, len(path) - 2)) or 0)
                _sr = min(4.0 + float(self_radius_slope) * max(0, _bn), 8.0)
                if path and path_hits_self([(p['x'], p['y']) for p in path], px, py,
                                           radius, self_radius=_sr):
                    continue
                # 反弹次数：可靠命中区间 ≤4 次；是否要求"至少 1 次反弹"（排除直射）
                # 由 require_bounce 决定。require_bounce=True 时就是 1~4 次反弹，
                # =False 时允许直射（与 docstring "先试直线" 一致）。
                bounces = d.get('bounces', max(0, len(path) - 1))
                if bounces > 4:
                    continue
                if require_bounce and bounces < 1:
                    continue  # 要求反弹，排除直射
                traveled = d.get('traveled', 0) or 0
                if max_traveled is not None and traveled > max_traveled:
                    continue  # 飞行太长：偏差大 + 移动目标易躲，宁可换位
                # 命中可靠性检查：官方 RayCast 用零宽射线判定，但实际弹丸有
                # 0.25m 半径。射线擦过矩形边缘（弹丸圆未达碰撞面）时不会命中。
                # 验证弹丸【中心路径】是否进入【膨胀矩形】——坦克矩形加弹丸
                # 半径（半宽 1.5+0.25=1.75，半高 2.0+0.25=2.25）。只要路径某段
                # 进入膨胀矩形，弹丸圆必然与坦克碰撞（可靠命中）。
                hw, hh = 1.75, 2.25  # 坦克矩形 + 弹丸半径（弹丸圆心能命中的边界）
                path_pts = [(p.get('x', 0), p.get('y', 0)) for p in path]
                if path_pts and len(path_pts) >= 2:
                    pen = False
                    # 检查路径各段是否进入膨胀矩形（弹丸圆必然命中坦克）
                    for a, b in zip(path_pts, path_pts[1:]):
                        seg = (b[0] - a[0], b[1] - a[1])
                        seg_len = math.hypot(*seg)
                        steps = max(1, int(seg_len / 0.3))
                        for i in range(steps + 1):
                            fx = a[0] + seg[0] * i / steps
                            fy = a[1] + seg[1] * i / steps
                            if _enters_target(fx, fy):
                                pen = True
                                break
                        if pen:
                            break
                    if not pen:
                        continue  # 弹丸中心未进入目标 → 实际会擦过
                if best is None or traveled < best[0]:
                    best = (traveled, deg)
                    best_path = path
                    best_bounces = bounces
                    best_d = d

        await _scan(cands)

        # 兜底全量扫描（默认关闭，保持验收时的行为）。tiles 预筛用的墙标记
        # 与真实 Box2D 墙有偏差，可能整体漏掉真实解 → 打开后会在第一轮完全
        # 无解时再做一次全角度权威扫描。会改变可选角集合，属未重新验收项。
        if best is None and exhaustive_fallback:
            tried = {round(deg % 360.0, 2) for deg, _b in cands}
            extra = [(i * 360.0 / sample_count, 0) for i in range(sample_count)
                     if round((i * 360.0 / sample_count) % 360.0, 2) not in tried]
            if extra:
                if verbose:
                    print(f'[aim] 预筛无解 → 全量权威扫描 {len(extra)} 个角度…', flush=True)
                await _scan(extra)
        if best is not None:
            if verbose and best_d:
                print(f'[aim] 命中角 {best[1]:.0f}° bounces={best_bounces} '
                      f'traveled={best[0]:.0f}m pts={len(best_path)}', flush=True)
            return math.radians(best[1])
        return None
    async def find_aim_angle_at(self, px, py, tx, ty, radius=TANK_RADIUS,
                                sample_count=360, target_id=None, require_bounce=False):
        """从【任意位置】(px,py) 检查能否命中 (tx,ty)（无需坦克在场）。

        用于预判"机动到某个位置后能否开火命中"（机动火控的射击位评估）。
        先试直线，直线不通则扫描全角度找反弹命中角。
        require_bounce=True 时要求至少 1 次墙反弹（排除直射）。
        返回方向角（deg 转弧度）；无解返回 None。
        """
        # 先试直线（允许直射时才考虑）
        straight = aim_angle_straight(px, py, tx, ty)
        if not require_bounce:
            res = await self._raycast_path(px, py, math.degrees(straight), tx, ty, radius,
                                           target_tank_id=target_id)
            if res.get('hit'):
                path = res.get('path', [])
                if not path_hits_self([(p['x'], p['y']) for p in path], px, py, radius):
                    bounces = res.get('bounces', max(0, len(path) - 1))
                    if bounces < 3:
                        return straight
        # 全角度扫描
        for i in range(sample_count):
            deg = 360.0 * i / sample_count
            res = await self._raycast_path(px, py, deg, tx, ty, radius,
                                           target_tank_id=target_id)
            if not res.get('hit'):
                continue
            path = res.get('path', [])
            if path and path_hits_self([(p['x'], p['y']) for p in path], px, py, radius):
                continue
            bounces = res.get('bounces', max(0, len(path) - 1))
            if bounces >= 3:
                continue
            if require_bounce and bounces < 1:
                continue  # 要求反弹，排除直射
            return math.radians(deg)
        return None

    async def predict_lead_target(self, tank_id, target_pos, target_id=None,
                                  bullet_speed=BULLET_SPEED, max_iter=3):
        """移动目标预测（lead targeting）：估算弹丸飞行时间内目标的新位置。

        原理：先算弹丸到目标的飞行时间 t = 距离/弹速；预测目标沿其速度
        移动 t 秒后的位置；再以此位置迭代 2-3 次收敛（因为目标移动会改变
        距离和飞行时间）。

        参数：
          target_pos    目标当前位置 (x, y)（米）
          target_id     目标坦克 id（可空，用于读速度；空则用目标位置采样速度）
          bullet_speed  弹速（默认基础弹 18m/s，激光 180m/s 传参）
          max_iter      迭代收敛次数

        返回预测位置 (px, py)，无法预测时返回 None（目标不动/无速度信息）。
        """
        pos = await self.get_position(tank_id)
        if not pos:
            return None
        px, py = pos
        # 目标速度：优先从 game 读 B2D 线速度（精确），否则两点采样
        tvx = tvy = 0.0
        if target_id:
            vel = await self.get_tank_velocity(target_id)
            if vel is not None:
                tvx, tvy = vel
        if tvx == 0 and tvy == 0 and target_id:
            t = await self.get_tank(target_id)
            if t and 'vx' in t:
                tvx, tvy = t.get('vx', 0), t.get('vy', 0)
        if tvx == 0 and tvy == 0:
            # 采样两次位置估速度（需要 target_id）
            if not target_id:
                return None
            p1 = target_pos
            await asyncio.sleep(0.1)
            p2 = await self.get_position(target_id)
            if p2:
                tvx = (p2[0] - p1[0]) / 0.1
                tvy = (p2[1] - p1[1]) / 0.1
            else:
                return None
        if tvx == 0 and tvy == 0:
            return None  # 目标静止，无需预测
        # 迭代收敛
        pred_x, pred_y = target_pos
        for _ in range(max_iter):
            dist = math.hypot(pred_x - px, pred_y - py)
            if dist < 0.1:
                break
            flight_t = dist / bullet_speed
            new_x = target_pos[0] + tvx * flight_t
            new_y = target_pos[1] + tvy * flight_t
            if abs(new_x - pred_x) < 0.05 and abs(new_y - pred_y) < 0.05:
                pred_x, pred_y = new_x, new_y
                break
            pred_x, pred_y = new_x, new_y
        return (pred_x, pred_y)

    async def aim_and_fire(self, tank_id, target_pos, fire_sec=0.15,
                           radius=TANK_RADIUS, tol=0.03, raycast=True,
                           lead=False, target_id=None, require_bounce=False,
                           verbose=False, max_traveled=None, muzzle_offset=0.0,
                           self_radius_slope=0.0, rotated_hit_check=False,
                           exhaustive_fallback=False):
        """自动火控：瞄准能命中 target_pos 的角度并开火。

        返回 True 表示已开火（命中角度已找到），False 表示找不到角度。
        转向后等待停稳再开火，避免弹丸沿旋转惯性方向偏出。

        raycast=True 时用 Box2D 物理射线权威判定命中角（更准，稍慢）；
        否则用 tiles 墙段推导（快，但 tiles 墙标记不完整会不准）。

        lead=True 时启用【移动目标预测】：根据目标当前速度估算弹丸
        飞行时间内的目标位置，瞄准预测点（对移动中的目标命中率高）。
        需要传 target_id 以读取目标速度（或采样）。

        require_bounce=True 时要求至少 1 次墙反弹（排除直射）。

        verbose=True 时打印实际选中的命中角详情（调试用）。

        武器感知：开火前自动查询当前武器。加特林(type5)有 0.5s 预热，
        点射打不出子弹 → 自动把按住时间加长到 0.7s+ 并启用 27.5m/s
        弹速预测；其他武器保持 fire_sec 不变。
        """
        # 武器感知（加特林预热；霰弹/其他高弹速武器适配弹速预测）
        weapon = await self.get_active_weapon(tank_id)
        gatling = (weapon == 5)
        eff_fire_sec = fire_sec
        if gatling:
            eff_fire_sec = max(fire_sec, GATLING_CHARGE_TIME + 0.2)
        if weapon == 5:
            bullet_speed = GATLING_SPEED          # 加特林 25~30 m/s
        elif weapon == 2:
            bullet_speed = 32.5                   # 霰弹 30~35 m/s
        elif weapon == 0:
            bullet_speed = 180.0                  # 激光 180 m/s
        else:
            bullet_speed = BULLET_SPEED           # 基础弹 18 m/s

        aim_pos = target_pos
        if lead:
            lead_pos = await self.predict_lead_target(
                tank_id, target_pos, target_id=target_id, bullet_speed=bullet_speed)
            if lead_pos:
                aim_pos = lead_pos
        if raycast:
            ang = await self.find_aim_angle_raycast(tank_id, aim_pos, radius,
                                                    target_id=target_id,
                                                    require_bounce=require_bounce,
                                                    verbose=verbose,
                                                    max_traveled=max_traveled,
                                                    muzzle_offset=muzzle_offset,
                                                    self_radius_slope=self_radius_slope,
                                                    rotated_hit_check=rotated_hit_check,
                                                    exhaustive_fallback=exhaustive_fallback)
        else:
            ang = await self.find_aim_angle(tank_id, aim_pos, radius)
        if ang is None:
            return False
        # 方向角转游戏 rot（游戏约定：forward_angle = rot - π/2）
        rot = ang + math.pi / 2
        aligned = await self.turn_to_rot(tank_id, rot, tol=tol)
        # 停稳：松手 + 等待物理角速度衰减
        await self.stop(tank_id)
        await self.wait_still(tank_id, max_wait=1.5)
        if not aligned:
            # 未对准就开火会让弹丸方向偏出命中角（曾导致 89° 偏差未命中）
            if verbose:
                print(f'[aim] ⚠️ 转向未对准（目标 rot {rot:.2f}），放弃开火', flush=True)
            return False
        # 最终朝向校验：用原始累积 rot 确认真的对准（wrap_pi 在超圈时
        # 可能误报 aligned=True，但实际朝向偏出）。差 >2° 则不开火。
        raw_check = await self._raw_rot(tank_id)
        if raw_check is not None:
            err_rad = abs(wrap_pi(rot - wrap_pi(raw_check)))
            if math.degrees(err_rad) > 2.0:
                if verbose:
                    print(f'[aim] ⚠️ 最终朝向偏差 {math.degrees(err_rad):.1f}°>2°，放弃开火', flush=True)
                return False
        # 武器复核：转向期间可能捡到新武器，弹速/弹道改变会使瞄准角失效
        w2 = await self.get_active_weapon(tank_id)
        if w2 is not None and w2 != weapon:
            if verbose:
                print(f'[aim] ⚠️ 武器变化（{weapon}→{w2}），放弃开火', flush=True)
            return False
        # 目标位置校验：转向期间目标可能被推走/移动，若偏离命中点过远
        # （>2m）则放弃开火（避免弹丸打旧位置）。调用方会重新找角。
        if target_id:
            tp_now = await self.get_position(target_id)
            if tp_now is None:
                # 目标已死/不在场 → 不开火（避免打空/打尸体）
                if verbose:
                    print(f'[aim] ⚠️ 目标已不在场，放弃开火', flush=True)
                return False
            if abs(tp_now[0]) < 0.1 and abs(tp_now[1]) < 0.1:
                # 目标在 (0,0)（重生中/刚死）→ 不开火
                if verbose:
                    print(f'[aim] ⚠️ 目标在 (0,0)（重生/死亡），放弃开火', flush=True)
                return False
            # lead=True 时 aim_pos 是【预测的未来位置】，目标此刻还在移动中，
            # 当前位置与预测位置必然有偏移（=目标速度×飞行时间），不能当作
            # "目标已移动"误判放弃。仅 lead=False 时做该校验。
            if not lead and math.hypot(tp_now[0] - aim_pos[0], tp_now[1] - aim_pos[1]) > 2.0:
                if verbose:
                    print(f'[aim] ⚠️ 目标已移动（{aim_pos[0]:.0f},{aim_pos[1]:.0f}→'
                          f'{tp_now[0]:.0f},{tp_now[1]:.0f}），放弃开火', flush=True)
                return False
        await self.fire(tank_id, eff_fire_sec)
        return True

    async def wait_still(self, tank_id, max_wait=1.5, speed_tol=0.3):
        """等待指定坦克完全停稳（平移速度接近 0）。

        开火前调用：坦克若还在惯性滑行，弹丸会沿移动方向偏出命中角。
        读取坦克 vx/vy（tankctl STATE_JS 提供），直到速度低于 tol 或超时。
        """
        try:
            deadline = asyncio.get_event_loop().time() + max_wait
            while asyncio.get_event_loop().time() < deadline:
                t = await self.get_tank(tank_id)
                if not t:
                    return
                vx = t.get('vx', 0) or 0
                vy = t.get('vy', 0) or 0
                if math.hypot(vx, vy) < speed_tol:
                    return
                await self.stop(tank_id)
                await asyncio.sleep(0.1)
            await self.stop(tank_id)
        except Exception:
            pass

    async def suicide_by_bounce(self, tank_id, timeout=20.0):
        """让指定角色朝最近的外墙开火自杀（弹丸反弹自伤）。

        用于需要"结束本回合"的场景（本地局无人击杀时回合永不结束）。
        流程：转向最近外墙 → 留出 ~8m 反弹空间 → 向墙开火 → 等反弹自伤。
        返回 True 表示坦克已被击杀/冻结（回合可结束），False 表示失败。

        注意：击杀数(type0)不因自杀增加（自杀不算击杀），胜利回合数
        (type1)会加给对手——检测自杀用"对面胜利数增加"。
        """
        try:
            # 迷宫尺寸（外墙位置）
            maze_js = await self.ctl.cdp.eval(
                '(() => { const gc=GameManager.getGameController()||GameManager.gameController;'
                ' const m=gc&&gc.getMaze?gc.getMaze():null;'
                ' return m ? JSON.stringify({w:m.getWidth(),h:m.getHeight()}) : null; })()')
            if not maze_js:
                return False
            import json as _json
            md = _json.loads(maze_js)
            world_w, world_h = md['w'] * TILE_M, md['h'] * TILE_M

            t = await self.get_tank(tank_id)
            if not t:
                return False
            x, y, rot = t['x'], t['y'], t['rot']
            # 最近的墙（仅外墙）
            dists = {'top': y, 'bottom': world_h - y, 'left': x, 'right': world_w - x}
            wall = min(dists, key=dists.get)
            target_fwd = {'top': -math.pi/2, 'bottom': math.pi/2,
                          'left': math.pi, 'right': 0.0}[wall]
            await self.turn_to_rot(tank_id, target_fwd + math.pi / 2, tol=0.05)
            # 太贴墙则后退留出反弹空间（目标 ~8m 远）
            t2 = await self.get_tank(tank_id)
            if not t2:
                return False
            x2, y2 = t2['x'], t2['y']
            new_dist = {'top': y2, 'bottom': world_h - y2,
                        'left': x2, 'right': world_w - x2}[wall]
            if new_dist < 6:
                await self.move_back(tank_id, min((6 - new_dist) / 10.0, 1.0))
                await asyncio.sleep(0.3)
            # 向墙开火（武器感知：加特林需持续按住预热才出弹；其他武器点射）
            w = await self.get_active_weapon(tank_id)
            if w == 5:  # 加特林：按住 0.7s（0.5s 预热 + 出弹）
                await self.fire(tank_id, 0.7)
            elif w == 2:  # 霰弹：一组散射
                await self.fire(tank_id, 0.4)
            else:
                await self.fire(tank_id, 0.25)
            # 等反弹自伤
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.5)
                alive = await self.is_alive(tank_id)
                if not alive:
                    return True
            return False
        except Exception:
            return False

    # ───────────── 行为控制（SET，全部在线安全）─────────────

    async def set_input(self, tank_id, fwd=False, back=False, left=False,
                        right=False, fire=False, hold=0.0, freq=30):
        """底层输入状态设置（最通用的 set 接口）。

        参数与 InputState 一一对应：
          fwd/back/left/right/fire: 布尔，是否按下
          hold: 持续时长（秒）；>0 时按 freq Hz 重复下发再释放
          freq: 重复频率（Hz），默认 30
        在线安全：走 setInputState（与键盘同路径）。
        """
        await self.ctl.set_input(tank_id, fwd=fwd, back=back, left=left,
                                 right=right, fire=fire, hold=hold, freq=freq)

    async def move_forward(self, tank_id, sec=0.5):
        """指定角色前进 `sec` 秒。"""
        await self.ctl.move_forward(sec, tank_id)

    async def move_back(self, tank_id, sec=0.5):
        """指定角色后退 `sec` 秒。"""
        await self.ctl.move_back(sec, tank_id)

    async def turn_left(self, tank_id, sec=0.5):
        """指定角色左转 `sec` 秒（减小 getRotation()）。"""
        await self.ctl.turn_left(sec, tank_id)

    async def turn_right(self, tank_id, sec=0.5):
        """指定角色右转 `sec` 秒（增大 getRotation()）。"""
        await self.ctl.turn_right(sec, tank_id)

    async def fire(self, tank_id, sec=0.15):
        """指定角色开火 `sec` 秒（默认点按 0.15s）。"""
        await self.ctl.fire(sec, tank_id)

    async def stop(self, tank_id):
        """松开指定角色所有输入（停止移动/转向/开火）。"""
        await self.ctl.stop_all(tank_id)

    # ───────────── 组合行为（更高层）─────────────

    async def turn_to(self, tank_id, tx, ty, tol=0.08):
        """让指定角色转向面向世界坐标 (tx, ty)。对准后返回 True。

        实现：算方向角（atan2）转成游戏 rot 约定，交给 turn_to_rot 闭环逼近。
        角色不在场返回 False。
        """
        if not await self.is_alive(tank_id):
            return False
        try:
            t = await self.get_tank(tank_id)
            if not t:
                return False
            dx, dy = tx - t['x'], ty - t['y']
            # 方向角 → 游戏 rot 约定（forward_angle = rot - π/2）
            target = math.atan2(dy, dx) + math.pi / 2
            return await self.turn_to_rot(tank_id, target, tol=tol)
        except Exception:
            return False

    async def move_toward(self, tank_id, tx, ty, arrival=1.0):
        """让指定角色驶向世界坐标 (tx, ty)，到达 `arrival` 米内返回 True。

        实现：先转向目标 -> 短促前进 -> 重复，直到距离足够。
        撞墙/被卡时靠 move_forward 无位移判断（简单版不做 wiggle，
        复杂导航可参考 scripts/pickup_mine.py 的 Navigator）。
        """
        for _ in range(80):  # 安全上限
            t = await self.get_tank(tank_id)
            if not t:
                return False
            d = math.hypot(tx - t['x'], ty - t['y'])
            if d < arrival:
                await self.stop(tank_id)
                return True
            if await self.turn_to(tank_id, tx, ty):
                await self.move_forward(tank_id, min(d / 7.5, 0.3))
            await asyncio.sleep(0.1)
        return False

    async def navigate_to(self, tank_id, target_xy, arrival=1.5,
                          waypoint_arrival=2.0, max_steps_per_waypoint=40,
                          replan=True):
        """迷宫寻路导航：让指定角色沿寻路路径驶向目标点。

        用游戏自身寻路（getShortestPathWithGraph）规划格子路径，再逐格
        中心导航（转向 + 前进 + 撞墙 wiggle）。比 move_toward（直线）
        能绕过墙体，适合迷宫环境。

        参数：
          target_xy      目标世界坐标 (x, y)（米）
          arrival        到达目标的判定距离（米）
          waypoint_arrival 到达每个中间格中心的判定距离
          max_steps_per_waypoint 每个航点的最大前进步数（防卡死）
          replan         True 时每个航点都重新规划（防累积偏离/绕路）

        返回 True 表示到达目标，False 表示寻路失败/坦克不在场。
        用法：
          await bhv.navigate_to(pid, (100, 50))
        """
        for round_idx in range(60):  # 全局安全上限
            cur = await self.get_position(tank_id)
            if not cur:
                return False
            # 到目标足够近
            if math.hypot(target_xy[0] - cur[0], target_xy[1] - cur[1]) <= arrival:
                await self.stop(tank_id)
                return True
            path = await plan_maze_path(self.ctl, cur, target_xy)
            if not path:
                # 退化为直线移动（无路径）
                await self.move_toward(tank_id, target_xy[0], target_xy[1], arrival)
                return True
            # 逐航点导航（本轮只走第一个航点，然后 replan 重新确认）
            gx, gy = path[0]
            pos = cur
            wx, wy = (gx + 0.5) * TILE_M, (gy + 0.5) * TILE_M
            d = math.hypot(wx - pos[0], wy - pos[1])
            if d <= waypoint_arrival:
                # 已在当前航点，跳到下一个
                if len(path) > 1:
                    gx2, gy2 = path[1]
                    wx, wy = (gx2 + 0.5) * TILE_M, (gy2 + 0.5) * TILE_M
                else:
                    wx, wy = target_xy[0], target_xy[1]
            print(f'[nav] #{round_idx} -> 格({gx},{gy}) 目标({wx:.0f},{wy:.0f}) 距{d:.1f}m', flush=True)
            # 转向航点
            ang = math.atan2(wy - pos[1], wx - pos[0])
            await self.turn_to_rot(tank_id, ang + math.pi / 2, tol=0.15)
            advanced = False
            stuck_count = 0
            for _ in range(max_steps_per_waypoint):
                pos2 = await self.get_position(tank_id)
                if not pos2:
                    return False
                d2 = math.hypot(wx - pos2[0], wy - pos2[1])
                if d2 <= waypoint_arrival:
                    advanced = True
                    break
                # 每步前进前校正朝向（防止漂移/转向误差累积）
                ang2 = math.atan2(wy - pos2[1], wx - pos2[0])
                await self.turn_to_rot(tank_id, ang2 + math.pi / 2, tol=0.25)
                before = pos2
                await self.move_forward(tank_id, 0.25)
                await asyncio.sleep(0.15)
                pos3 = await self.get_position(tank_id)
                if not pos3:
                    return False
                moved = math.hypot(pos3[0] - before[0], pos3[1] - before[1])
                if moved < 0.15:
                    # 卡墙：沿墙滑行绕开（转向90°沿墙走一段，再回正）
                    stuck_count += 1
                    await self.move_back(tank_id, 0.25)
                    await asyncio.sleep(0.1)
                    # 顺时针试（右转90°沿墙）
                    await self.turn_to_rot(tank_id, ang2 + math.pi / 2 + math.pi / 2, tol=0.2)
                    await self.move_forward(tank_id, 0.4)
                    await asyncio.sleep(0.2)
                    if stuck_count >= 2:
                        # 逆时针试（另一侧沿墙）
                        await self.move_back(tank_id, 0.4)
                        await self.turn_to_rot(tank_id, ang2 + math.pi / 2 - math.pi / 2, tol=0.2)
                        await self.move_forward(tank_id, 0.4)
                        await asyncio.sleep(0.2)
                        stuck_count = 0
            if not advanced:
                print('[nav] 航点未到达，重新规划...', flush=True)
        return False

    async def aim_at_nearest_wall(self, tank_id):
        """让指定角色正对最近的墙（用于贴墙自杀/预瞄）。
        返回 target_rot；失败返回 None。"""
        info = await self.get_nearest_wall(tank_id)
        if not info:
            return None
        _, _, target_rot = info
        await self.turn_to_rot(tank_id, target_rot)
        return target_rot

    async def turn_to_rot(self, tank_id, target_rot, tol=0.03):
        """让指定角色转向指定朝向弧度 target_rot。对准返回 True。

        实现（闭环，无惯性）：持续按住转向键，每 30ms 读一次 rot，
        一旦到达目标角容差内立即松开。实测游戏转向无惯性（松开即停），
        所以按住逼近不会过冲，比固定脉冲（0.05s=28° 太粗）精确得多。

        精度：先用大脉冲快速逼近，接近目标后换小脉冲（0.01s）微调，
        目标容差 tol=0.03 rad（1.7°）。角色不在场/出错/超时未对准返回 False。

        关键（防超圈）：游戏 getRotation() 是【累积值】（可到几百上千度，
        曾观测到 -1077°）。若用归一化 [-π,π) 的 rot 计算转向方向，在 ±180°
        边界跳变时会选错方向 → 绕远路 → 累积超圈 → 弹丸方向错。
        因此这里【直接读未归一化原始 rot】，把目标角转为最近的累积等效角，
        再按最短方向转，永不超圈。

        注意：返回 True 表示【已对准】。未对准返回 False（调用方应停止开火）。
        """
        if not await self.is_alive(tank_id):
            return False
        try:
            # 先松手，确保从静止开始
            await self.stop(tank_id)
            await asyncio.sleep(0.05)
            # 读【未归一化】原始累积 rot
            raw = await self._raw_rot(tank_id)
            if raw is None:
                return False
            # 把归一化目标角换算成【最近的累积等效角】：
            # raw 可能累积到几圈，target_rot 是 [-π,π) 目标，需对齐到 raw 附近
            # 使 d = target_raw - raw 落在 [-π, π]（最短方向，不绕远路）
            target_raw = raw + wrap_pi(target_rot - wrap_pi(raw))
            d = wrap_pi(target_raw - raw)
            if abs(d) < tol:
                return True
            direction = 'right' if d > 0 else 'left'
            # 每帧用原始累积值读实际方向，闭环逼近（不会因归一化跳变误判）
            for _ in range(150):  # 安全上限（~5s）
                raw = await self._raw_rot(tank_id)
                if raw is None:
                    await self.stop(tank_id)
                    return False
                # target_raw 固定；raw 单调逼近 target_raw
                remaining = target_raw - raw
                # 累计误差：若 remaining 超出 ±2π 说明绕圈了，重新对齐
                if abs(remaining) > math.pi * 2:
                    target_raw = raw + wrap_pi(target_rot - wrap_pi(raw))
                    remaining = target_raw - raw
                if abs(remaining) < tol:
                    await self.stop(tank_id)
                    return True
                direction = 'right' if remaining > 0 else 'left'
                # 大脉冲快速逼近（0.02s 覆盖一物理帧 0.033s）
                await self.set_input(tank_id, **{direction: True}, hold=0.02, freq=60)
                await asyncio.sleep(0.03)
            # 超时：未对准返回 False（不静默成功）
            await self.stop(tank_id)
            raw = await self._raw_rot(tank_id)
            if raw is not None and abs(target_raw - raw) < tol:
                return True
            return False
        except Exception:
            return False

    async def _raw_rot(self, tank_id):
        """读取指定坦克的【未归一化原始累积】rot（弧度）。

        游戏 getRotation() 是累积值（可到几百上千度）。归一化到 [-π,π)
        会在 ±180° 边界跳变，导致转向方向误判 → 绕远路累积超圈。
        火控/转向直接用它可避免此问题。返回 None 表示坦克不在场。
        """
        try:
            v = await self.ctl.cdp.eval(
                f'(GameManager.getGameController().getTanks()["{tank_id}"].getRotation())')
            return float(v) if v is not None else None
        except Exception:
            return None


# ───────────── 示例主流程（写新脚本时替换这个函数）─────────────

async def main():
    """模板示例：连接 -> 列出可控角色 -> 让第一辆坦克转一圈。

    写新脚本时：复制本文件，把 main() 换成你的行为逻辑，
    再用 get_roles / move_toward / fire / turn_to 等组合。
    """
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 9222

    bhv = TankBehavior(port=port)
    await bhv.connect()

    # 1) 获取可操控角色
    roles = await bhv.get_roles()
    print(f'[template] 可控角色: {roles}')
    if not roles:
        print('[template] 没有可控坦克（游戏未就绪？）')
        return 1
    pid = await bhv.pick_role('keyboard') or next(iter(roles))
    print(f'[template] 使用坦克: {pid}')

    # 2) 等待回合开始
    while not await bhv.is_round_active():
        await asyncio.sleep(0.5)
    print('[template] 回合开始')

    # 3) 示例行为：前进 -> 停 -> 开火
    await bhv.move_forward(pid, 0.5)
    await bhv.stop(pid)
    await bhv.fire(pid, 0.15)
    print('[template] 已执行：前进0.5s -> 停 -> 开火')

    # 4) 打印状态
    pos = await bhv.get_position(pid)
    rot = await bhv.get_rotation(pid)
    print(f'[template] 坦克状态: pos={pos} rot={rot:.2f}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print('\n[template] 已停止')
