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
TankTrouble Multi-Instance Monitor Server
==========================================
Unified monitor for BOTH CDP-driven windows AND Tampermonkey windows.

Sources:
  - CDP instances:  Edge with --remote-debugging-port (polled via CDP)
  - TM instances:   Tampermonkey script windows (push via WebSocket)

Each instance (from either source) is exposed on a state WS port:

  CDP instance 0 (port 9222) ─┐
  CDP instance 1 (port 9223) ─┼─► WS :9310 (instance 0)
                              ├─► WS :9311 (instance 1)
  TM window 0 (?ttport=9301) ─┼─► WS :9312 (instance 2)
  TM window 1 (?ttport=9302) ─┘    ... etc

Usage:
  python monitor_server.py 2 9222          # 2 CDP instances from port 9222
  python monitor_server.py 0 0 --tm 2      # 2 TM windows
  python monitor_server.py 2 9222 --tm 2   # 2 CDP + 2 TM (4 monitors)
"""
import asyncio
import json
import os
import sys

# Windows：把定时器分辨率提升到 1ms，让 asyncio.sleep 足够精准，
# 以支持 60fps 状态轮询（默认 15.6ms tick 只能到 ~30fps）。
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'cdp_tools'))
from cdp import CDP

# 各实例状态 WS 的基准端口
STATE_BASE_PORT = 9310
TM_BASE_PORT = 9301

# 回合事件跟踪 hook（通过 CDP 在页面里只安装一次）
ROUND_HOOK_JS = r"""
(() => {
  if (window._ttRoundHooked) return true;
  window._ttRoundState = {betweenRounds:false, countingDown:false, roundActive:false};
  const gc = GameManager.getGameController() || GameManager.gameController;
  if (!gc || !gc.addRoundEventListener) return false;
  window._ttRoundHooked = true;
  gc.addRoundEventListener(function(id, evt, data) {
    const ename = data || '';
    const rs = window._ttRoundState;
    if (ename === 'round ended' || ename === 'round created') { rs.betweenRounds = true; rs.roundActive = false; }
    else if (ename === 'count down') { rs.countingDown = true; }
    else if (ename === 'round started') { rs.betweenRounds = false; rs.countingDown = false; rs.roundActive = true; }
  }, this);
  return true;
})()
"""

# 角色识别（权威、静态）：读取游戏实时 InputManager 实例
# （window.Inputs.__metaclass.inputManagers）。每个实例持有 playerId 和它的
# 按键绑定 —— 即游戏自身"控制 -> 玩家"的绑定表。
# 回合切换、页面刷新后依然有效；无需行为探测。
ROLE_PROBE_JS = r"""
(() => {
  const out = {roles: {}};
  try {
    const ims = window.Inputs.__metaclass.inputManagers;
    for (let i=0;i<ims.length;i++) {
      const m = ims[i];
      const isK = m.__class === window.KeyboardInputManager;
      const isM = m.__class === window.MouseInputManager;
      if (!m.playerId) continue;
      if (isK) {
        out.roles[m.playerId] = (m.forwardKey === 38) ? 'arrows' : 'keyboard';
      } else if (isM) {
        out.roles[m.playerId] = 'mouse';
      }
    }
  } catch(e) { out.err = e.message; }
  return JSON.stringify(out);
})()
"""
STATE_JS = r"""
(() => {
  const out = {tanks:{}, projectiles:[], collectibles:[], mines:[], walls:[], zones:[], roles:{},
               maze:null, alive:0, active:false, allPlayerIds:[], storm:false,
               betweenRounds:false, countingDown:false, roundActive:false,
               scoresKill:{}, scoresVict:{}, winner:null};
  const gmObj = (typeof GameManager !== 'undefined') ? GameManager : null;
  const gc = gmObj ? (gmObj.getGameController ? gmObj.getGameController() : (gmObj.gameController || null)) : null;
  if (!gc) return {err: (gmObj ? 'no gc' : 'no GameManager')};
  out.t_ms = Date.now();
  try { if (gc.getState) out.phase = gc.getState(); } catch(e){}
  try { if (gc.getRoundId) out.roundId = gc.getRoundId(); } catch(e){}
  try { if (gc.countDownValue !== undefined) { out.countdown_raw = gc.countDownValue;
    out.countdown = (gc.getState && gc.getState() === 'counting down') ? gc.countDownValue : null; } } catch(e){}
  try { if (gc.getTheme) out.theme = gc.getTheme(); } catch(e){}
  // 游戏暂停/时钟（v6）：窗口失焦/后台时 Phaser 会暂停，此时 set_input 会被静默忽略，
  // 但 phase/accepts_input 仍可能是 in round —— 必须能检测到。
  try {
    const g = gmObj && gmObj.phaserInstance;
    if (g) {
      out.paused = !!g.paused;
      if (g.time) out.game_time_ms = g.time.now;   // 帧计数/fps 未开 advancedTiming，恒 0，不暴露
    }
    out.hidden = !!document.hidden;
  } catch(e){}
  try { if (gc.isRoundActive) out.roundActive = !!gc.isRoundActive(); } catch(e){}
  // active 语义修复：gc.isRoundActive() 实测恒 false，用 phase 推导
  out.active = (out.phase === 'in round');
  // allPlayerIds: use getActivePlayerIds() directly.
  // - Includes players who died but are still in the room (their kills still score).
  // - getActivePlayerIds() excludes spectators who left (the old ghost bug was in
  //   getQueuedPlayerIds()/getAllPlayerIds(), which retain leavers until next round).
  try {
    const activeIds = gc.getActivePlayerIds ? gc.getActivePlayerIds() : [];
    out.allPlayerIds = activeIds.slice();
  } catch(e){ out.allPlayerIds = []; }
  try { if (gc.getStorm) out.storm = gc.getStorm(); } catch(e){}

  // tanks + weapons + shield
  try {
    const ts = gc.getTanks ? gc.getTanks() : {};
    for (const id in ts) {
      const t = ts[id];
      let rot = 0;
      if (t.getRotation) { const a = t.getRotation(); if (typeof a === 'number') rot = a; }
      // weapon
      let wp = -1;
      try { const aw = gc.getActiveWeapon ? gc.getActiveWeapon(id) : null;
        if (aw && aw.getType) wp = aw.getType(); else if (aw && aw.type !== undefined) wp = aw.type; } catch(e){}
      // shield (UPGRADE_TYPE_SHIELD=2)
      let shield = false, shieldLife = 0;
      try { const su = gc.getUpgradeByPlayerIdAndType ? gc.getUpgradeByPlayerIdAndType(id, 2) : null;
        if (su) { shield = true; const flds = su.getFields ? su.getFields() : null;
          if (flds && typeof flds._lifetime === 'number') shieldLife = flds._lifetime; } } catch(e){}
      let vx = 0, vy = 0;
      try { const b = t.getB2DBody ? t.getB2DBody() : null;
        const v = b ? b.GetLinearVelocity() : null;
        if (v) { vx = v.x || 0; vy = v.y || 0; } } catch(e){}
      out.tanks[id] = {x: t.getX ? t.getX() : 0, y: t.getY ? t.getY() : 0,
        angle: rot, rot: rot, alive: true, weapon: wp, shield: shield, shieldLife: shieldLife,
        vx: vx, vy: vy};
      out.alive++;
    }
    // roles: authoritative static mapping from game input managers (every poll)
    try {
      const ims = window.Inputs.__metaclass.inputManagers;
      for (let i=0;i<ims.length;i++) {
        const m = ims[i];
        const isK = m.__class === window.KeyboardInputManager;
        const isM = m.__class === window.MouseInputManager;
        if (!m.playerId) continue;
        if (isK) {
          out.roles[m.playerId] = (m.forwardKey === 38) ? 'arrows' : 'keyboard';
        } else if (isM) {
          out.roles[m.playerId] = 'mouse';
        }
      }
    } catch(e){}
  } catch(e){}

  // maze + walls
  try {
    const m = gc.getMaze ? gc.getMaze() : null;
    if (m) {
      const mw = m.getWidth ? m.getWidth() : 0, mh = m.getHeight ? m.getHeight() : 0;
      out.maze = {w:mw, h:mh};
      if (m.getTiles && mw>0 && mh>0) {
        // T = 10 m/格（Constants.MAZE_TILE_SIZE）。必须与 tanks/projectiles 的
        // getX()/getY()（米）同一单位——前端 draw() 对 walls/tanks 用同一个
        // 缩放系数 s（由 maze.w*10 米推出）。历史上这里误用 T=200（像素），
        // 导致墙被放大 20 倍、几乎全部画到画布外（监控室"看不到地图"）。
        const td = m.getTiles(), T=10;
        out.walls.push({x1:0,y1:0,x2:mw*T,y2:0});
        out.walls.push({x1:0,y1:mh*T,x2:mw*T,y2:mh*T});
        out.walls.push({x1:0,y1:0,x2:0,y2:mh*T});
        out.walls.push({x1:mw*T,y1:0,x2:mw*T,y2:mh*T});
        for (let x=0;x<mw;x++) for (let y=0;y<mh;y++) {
          const tile = td[x] ? (td[x][y] || [0,0,0]) : [0,0,0];
          const present=tile[0], topWall=tile[1], leftWall=tile[2];
          const tx=x*T, ty=y*T;
          if (present) {
            if (topWall) out.walls.push({x1:tx,y1:ty,x2:tx+T,y2:ty});
            if (leftWall) out.walls.push({x1:tx,y1:ty,x2:tx,y2:ty+T});
          } else {
            out.walls.push({x1:tx,y1:ty,x2:tx+T,y2:ty});
            out.walls.push({x1:tx,y1:ty+T,x2:tx+T,y2:ty+T});
            out.walls.push({x1:tx,y1:ty,x2:tx,y2:ty+T});
            out.walls.push({x1:tx+T,y1:ty,x2:tx+T,y2:ty+T});
          }
        }
      }
    }
  } catch(e){}

  // projectiles (full: owner, timeAlive, deadlyToOwner, remaining lifetime)
  try {
    const rm = gc.getRoundModel ? gc.getRoundModel() : null;
    const raw = (rm && rm.getProjectiles) ? rm.getProjectiles() : (gc.getProjectiles ? gc.getProjectiles() : {});
    // max lifetime per weapon type from game Constants
    const MAXLIFE = {};
    try {
      MAXLIFE[-1] = Constants.C_BULLET_MAX_LIFETIME;
      MAXLIFE[0] = Constants.C_LASER_MAX_LIFETIME;
      MAXLIFE[1] = Constants.C_DOUBLE_BARREL_MAX_LIFETIME;
      MAXLIFE[2] = Constants.C_SHOTGUN_MAX_LIFETIME;
      MAXLIFE[3] = Constants.C_HOMING_MISSILE_MAX_LIFETIME;
      MAXLIFE[4] = Constants.C_BULLET_MAX_LIFETIME;
      MAXLIFE[5] = Constants.C_GATLING_GUN_MAX_LIFETIME;
    } catch(e){}
    for (const pid in raw) {
      const p = raw[pid];
      let owner = '';
      try { owner = p.getPlayerId ? p.getPlayerId() : (p.playerId || ''); } catch(e){}
      let timeAlive = -1, deadly = false;
      try { timeAlive = p.getTimeAlive ? p.getTimeAlive() : (p.timeAlive !== undefined ? p.timeAlive : -1); } catch(e){}
      try { deadly = p.isDeadlyToOwner ? p.isDeadlyToOwner() : (p.deadlyToOwner !== undefined ? p.deadlyToOwner : false); } catch(e){}
      const type = (p.getType ? p.getType() : -1);
      const maxLife = (MAXLIFE[type] !== undefined && MAXLIFE[type] != null) ? MAXLIFE[type] : 10;
      const lifetime = timeAlive >= 0 ? Math.max(0, maxLife - timeAlive) : maxLife;
      out.projectiles.push({id:pid, x:p.getX?p.getX():0, y:p.getY?p.getY():0,
        vx:p.getSpeedX?p.getSpeedX():0, vy:p.getSpeedY?p.getSpeedY():0,
        type:type, owner:owner, timeAlive:timeAlive, deadlyToOwner:deadly,
        lifetime:lifetime, maxLifetime:maxLife});
    }
  } catch(e){}

  // collectibles
  try {
    const rm = gc.getRoundModel ? gc.getRoundModel() : null;
    const raw = (rm && rm.getCollectibles) ? rm.getCollectibles() : (gc.getCollectibles ? gc.getCollectibles() : {});
    for (const cid in raw) {
      const c = raw[cid];
      out.collectibles.push({x:c.getX?c.getX():0, y:c.getY?c.getY():0, type:(c.getType?c.getType():-1)});
    }
  } catch(e){}

  // mines
  try {
    const rm = gc.getRoundModel ? gc.getRoundModel() : null;
    const raw = (rm && rm.getTraps) ? rm.getTraps() : (gc.getTraps ? gc.getTraps() : {});
    for (const mid in raw) {
      const m = raw[mid];
      let owner = '';
      try { owner = m.getPlayerId ? m.getPlayerId() : ''; } catch(e){}
      out.mines.push({x:m.getX?m.getX():0, y:m.getY?m.getY():0, activated:(m.isActivated?m.isActivated():false), owner:owner});
    }
  } catch(e){}

  // zones
  try {
    const zones = gc.getZones ? gc.getZones() : {};
    for (const zid in zones) {
      const z = zones[zid];
      const f = z.getFields ? z.getFields() : null;
      if (f && f._tileBounds) {
        const tb = f._tileBounds;
        out.zones.push({minX:(tb.minX||0)+(f._expansionTilesLeft||0), maxX:(tb.maxX||0)-(f._expansionTilesRight||0),
          minY:(tb.minY||0)+(f._expansionTilesTop||0), maxY:(tb.maxY||0)-(f._expansionTilesBottom||0)});
      }
    }
  } catch(e){}

  // scores (all players)
  try {
    const pids = out.allPlayerIds;
    for (let i=0;i<pids.length;i++) {
      const pid = pids[i];
      try { const s0 = gc.getScoreByPlayerIdAndType ? gc.getScoreByPlayerIdAndType(pid, 0) : null;
        out.scoresKill[pid] = (s0 && s0.getValue) ? s0.getValue() : 0; } catch(e){}
      try { const s1 = gc.getScoreByPlayerIdAndType ? gc.getScoreByPlayerIdAndType(pid, 1) : null;
        out.scoresVict[pid] = (s1 && s1.getValue) ? s1.getValue() : 0; } catch(e){}
    }
  } catch(e){}

  // round state: authoritative from GameModel.getState() via global GameModel._STATES
  try {
    const st = gc.model && gc.model.getState ? gc.model.getState() : null;
    const S = (typeof GameModel !== 'undefined' && GameModel._STATES) ? GameModel._STATES : null;
    if (st && S) {
      out.betweenRounds = (st === S.BETWEEN_ROUNDS);
      out.countingDown = (st === S.COUNTING_DOWN);
      out.roundActive = (st === S.IN_ROUND);
    } else if (st) {
      out.betweenRounds = (st === 'between rounds');
      out.countingDown = (st === 'counting down');
      out.roundActive = (st === 'in round');
    }
  } catch(e){}
  // Fallback inference if getState unavailable: round active if tanks present
  if (!out.roundActive && out.alive > 0) {
    out.roundActive = true;
    out.betweenRounds = false;
  }

  return out;
})()
"""


class InstanceMonitor:
    def __init__(self, index, state_port, source='cdp', cdp_port=None, tm_port=None):
        self.index = index
        self.state_port = state_port
        self.source = source
        self.cdp_port = cdp_port
        self.tm_port = tm_port
        self.cdp = None
        self.connected = False
        self.state = {'err': 'not connected'}
        self.clients = set()
        self._state_event = asyncio.Event()   # 提前创建，serve_state 才能随时引用
        self._reconnect_at = 0
        self.roles = {}
        self.roles_probed = False
        self.roles_retry_at = 0
        self.roles_stale = False

    async def connect(self):
        if self.source == 'cdp':
            self.cdp = CDP(port=self.cdp_port)
            try:
                page = await self.cdp.connect_page('tanktrouble.com', timeout=10)
            except Exception as e:
                # 浏览器未启动/端口不通：标记离线，不崩溃。run() 会持续重试。
                # 降噪：每 10 秒最多打印一次，避免刷屏
                now = asyncio.get_event_loop().time()
                if now - getattr(self, '_last_conn_err_at', 0) > 10.0:
                    self._last_conn_err_at = now
                    print(f'[monitor] instance {self.index}: CDP {self.cdp_port} 未就绪: {str(e)[:60]}（后台重试中）', flush=True)
                self.connected = False
                self._reconnect_at = 0
                return
            print(f'[monitor] instance {self.index}: CDP {self.cdp_port} → {page["url"]}', flush=True)
            self.connected = True
            self._last_conn_err_at = 0
            # 在页面里安装回合事件跟踪（与 TM 脚本相同）
            try:
                await asyncio.wait_for(self.cdp.eval(ROUND_HOOK_JS), timeout=3)
            except Exception:
                pass
        else:
            print(f'[monitor] instance {self.index}: TM window on :{self.tm_port}', flush=True)
            self.connected = True

    async def probe_roles(self):
        """Read authoritative static roles (game input managers). Kept for logging;
        STATE_JS already embeds the same mapping every poll. 返回 roles dict。"""
        if self.source != 'cdp':
            return {}
        try:
            raw = await asyncio.wait_for(self.cdp.eval(ROLE_PROBE_JS), timeout=3)
            if raw:
                d = json.loads(raw) if isinstance(raw, str) else raw
                self.roles = d.get('roles', {}) or {}
                return self.roles
        except Exception as e:
            if self._last_conn_err_at is None or \
               asyncio.get_event_loop().time() - getattr(self, '_last_role_err_at', 0) > 10:
                self._last_role_err_at = asyncio.get_event_loop().time()
                print(f'[monitor] instance {self.index} role probe err: {e}', flush=True)
        return self.roles if hasattr(self, 'roles') else {}

    async def poll(self):
        """Poll state from the game via CDP.
        Roles are read inline in STATE_JS every poll (authoritative static mapping),
        so they stay correct across kills/rounds/reloads with zero blocking."""
        if self.source != 'cdp':
            return
        try:
            t0 = asyncio.get_event_loop().time()
            self.state = await asyncio.wait_for(self.cdp.eval(STATE_JS), timeout=3)
            dt = (asyncio.get_event_loop().time() - t0) * 1000
            if isinstance(self.state, dict):
                self.state['_instance'] = self.index
                self.state['_source'] = 'cdp'
                self.state['_cdpPort'] = self.cdp_port
                self.state['_time'] = asyncio.get_event_loop().time()
                if hasattr(self, '_state_event'):
                    self._state_event.set()
                if dt > 100:
                    print(f'[monitor] instance {self.index} poll slow: {dt:.1f}ms '
                          f'@ {asyncio.get_event_loop().time():.2f}', flush=True)
        except Exception as e:
            self.state = {'err': str(e)[:100], '_instance': self.index, '_source': 'cdp'}
            self.connected = False  # 让 poll_loop 放慢重试节奏
            # 降噪：连接断开时 poll 每秒失败几十次，只打印状态变化
            now = asyncio.get_event_loop().time()
            last = getattr(self, '_last_err_at', 0)
            if now - last > 3.0:
                self._last_err_at = now
                print(f'[monitor] instance {self.index} poll err: {type(e).__name__} {str(e)[:80]}', flush=True)
            # 自动重连：重建 CDP 连接（reader 可能已死，connect_page 会重开 ws+reader）
            if now >= getattr(self, '_reconnect_at', 0):
                self._reconnect_at = now + 5.0
                try:
                    await self.cdp.connect_page('tanktrouble.com', timeout=5)
                    self.connected = True
                    print(f'[monitor] instance {self.index} reconnected', flush=True)
                    self._last_err_at = 0
                except Exception as re_err:
                    if now - last > 3.0:
                        print(f'[monitor] instance {self.index} reconnect err: {re_err}', flush=True)

    async def run(self):
        # 启动 WS 服务（无论 CDP 是否连上都先提供端口）
        tasks = [self.serve_state()]
        if self.source == 'cdp':
            tasks.append(self.role_probe_loop())
            tasks.append(self._heartbeat())
            # 后台连接重试（不阻塞 serve_state 启动）
            tasks.append(self._connect_with_retry())
        else:
            await self.connect()
            tasks.append(self.serve_tm())
        await asyncio.gather(*tasks)

    async def _heartbeat(self):
        """事件循环看门狗：每 0.5 秒记录一次 loop 时间。
        若两次间隔远超 0.5s，说明事件循环被同步阻塞（诊断用）。"""
        while True:
            t0 = asyncio.get_event_loop().time()
            await asyncio.sleep(0.5)
            dt = asyncio.get_event_loop().time() - t0
            if dt > 1.0:
                print(f'[monitor] instance {self.index} LOOP BLOCKED {dt:.1f}s', flush=True)

    async def _connect_with_retry(self):
        """后台尝试连接 CDP；失败则每 3 秒重试，直到成功。
        连接成功后额外启动 poll_loop（持续轮询状态）。
        期间 serve_state 照常运行，前端显示"离线"而非崩溃。"""
        while True:
            await self.connect()
            if self.connected:
                # 已连接：启动轮询，然后随 run() 的 gather 一起运行
                await self.poll_loop()
                return
            await asyncio.sleep(3.0)

    async def role_probe_loop(self):
        """后台任务：偶尔打印角色（仅信息用；STATE_JS 已包含角色）。
        只在已连接时探测，避免对离线实例（浏览器未开）反复发起 eval，
        与 poll 抢同一个 CDP 连接。仅在角色变化时打印（降噪）。"""
        if self.source != 'cdp':
            return
        last_sig = None
        while True:
            await asyncio.sleep(5.0)
            if not self.connected:
                continue
            try:
                roles = await self.probe_roles()
                if roles:
                    sig = tuple(sorted(roles.items()))
                    if sig != last_sig:
                        last_sig = sig
                        print(f'[monitor] instance {self.index} roles: {roles}', flush=True)
            except Exception:
                pass

    async def _tm_handler(self, ws):
        """接收 TM 脚本推送的状态并存储。"""
        print(f'[monitor] instance {self.index}: TM connected on :{self.tm_port}', flush=True)
        import websockets
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if data.get('type') == 'state':
                    self.state = data.get('data', {})
                    if isinstance(self.state, dict):
                        self.state['_instance'] = self.index
                        self.state['_source'] = 'tm'
                        self.state['_tmPort'] = self.tm_port
                        self.state['_time'] = asyncio.get_event_loop().time()
                        if hasattr(self, '_state_event'):
                            self._state_event.set()
        except websockets.ConnectionClosed:
            print(f'[monitor] instance {self.index}: TM disconnected', flush=True)
        except Exception as e:
            print(f'[monitor] instance {self.index}: TM handler err: {e}', flush=True)

    async def serve_state(self):
        """向查看者广播状态的 WebSocket 服务。
        事件驱动：handler 阻塞在 Event 上；poll()/TM 每次更新后 set 唤醒。
        客户端连接时立即发送当前状态，保证新客户端必有数据。"""
        import websockets
        async def handler(ws):
            self.clients.add(ws)
            ev = self._state_event
            try:
                # 立即发送当前状态（避免新客户端空白）
                try:
                    await ws.send(json.dumps({'type': 'state', 'data': self.state}))
                except Exception:
                    pass
                while True:
                    await ev.wait()
                    ev.clear()
                    msg = json.dumps({'type': 'state', 'data': self.state})
                    # Windows 下对已断开 socket 的 send 会抛 OSError
                    # (Errno 22 Invalid argument) 而非 ConnectionClosed，
                    # 统一按断开处理，避免 handler 崩溃引发重连风暴。
                    try:
                        await ws.send(msg)
                    except Exception:
                        break
            except websockets.ConnectionClosed:
                pass
            except Exception:
                pass
            finally:
                self.clients.discard(ws)
                try:
                    await ws.close()
                except Exception:
                    pass

        async with websockets.serve(handler, 'localhost', self.state_port):
            print(f'[monitor] instance {self.index} state WS on :{self.state_port}', flush=True)
            await asyncio.Future()

    async def serve_tm(self):
        """Accept TM script connections on this instance's TM port."""
        import websockets
        async with websockets.serve(self._tm_handler, 'localhost', self.tm_port):
            await asyncio.Future()

    async def poll_loop(self):
        while True:
            t0 = asyncio.get_event_loop().time()
            try:
                await self.poll()
            except Exception:
                pass
            elapsed = asyncio.get_event_loop().time() - t0
            if not self.connected:
                # 未连接：慢速重试（3s），不 60Hz 空转
                await asyncio.sleep(3.0)
                continue
            # 连接刚断开（最近有 poll err）：放慢到 ~2s，等自动重连
            last_err = getattr(self, '_last_err_at', 0)
            if last_err and asyncio.get_event_loop().time() - last_err < 3.0:
                await asyncio.sleep(2.0)
                continue
            # 正常：补偿轮询耗时，让节奏接近 ~16ms（60fps）
            wait = max(0.0, 0.016 - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)


async def main():
    args = sys.argv[1:]
    cdp_count = int(args[0]) if len(args) > 0 and args[0].isdigit() else 2
    if len(args) > 1 and args[1] == 'auto':
        # 端口写 'auto'：自动发现 dsh-ego-browser 的动态 CDP 端口
        # （browser.json），找不到再回退扫描 9222-9231。见 cdp_tools/ego_browser.py
        try:
            from ego_browser import resolve_port
            base_cdp = resolve_port('auto')
            print(f'[monitor] CDP 端口 auto -> {base_cdp}', flush=True)
            if cdp_count > 1:
                print(f'[monitor] ⚠️ auto 发现的是单例浏览器，count={cdp_count} '\
                      f'时其余实例会离线；如确需多实例请显式给基准端口', flush=True)
        except Exception as e:
            print(f'[monitor] auto 发现失败（{e}），回退 9222', flush=True)
            base_cdp = 9222
    else:
        base_cdp = int(args[1]) if len(args) > 1 and args[1].isdigit() else 9222
    tm_count = 0
    if '--tm' in args:
        i = args.index('--tm')
        if i + 1 < len(args):
            tm_count = int(args[i + 1])

    monitors = []
    idx = 0
    for i in range(cdp_count):
        monitors.append(InstanceMonitor(idx, STATE_BASE_PORT + idx, source='cdp', cdp_port=base_cdp + i))
        idx += 1
    for i in range(tm_count):
        monitors.append(InstanceMonitor(idx, STATE_BASE_PORT + idx, source='tm', tm_port=TM_BASE_PORT + i))
        idx += 1

    print(f'[monitor] {cdp_count} CDP + {tm_count} TM = {idx} monitors', flush=True)
    print(f'[monitor] state WS ports: {STATE_BASE_PORT}-{STATE_BASE_PORT+idx-1}', flush=True)
    if tm_count:
        print(f'[monitor] TM ports: {TM_BASE_PORT}-{TM_BASE_PORT+tm_count-1} (URL 加 ?ttport=N)', flush=True)
    await asyncio.gather(*[m.run() for m in monitors])


if __name__ == '__main__':
    asyncio.run(main())
