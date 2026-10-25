#!/usr/bin/env python
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
TankTrouble — 激光武器参数测量
==============================
三部分：
  P0 静态：`Constants.SERVER` 里的武器/激光常量 + 手上激光武器的实例字段 + 武器状态
  P1 单发：页面内 rAF 采样器逐帧记录弹丸（位置/速度/timeAlive/反弹数）→ 弹速、寿命、是否反弹
  P2 连发：打 N 发测射击间隔与弹药耗尽（耗尽后武器会掉回基础弹）

用法：
    python scripts/laser_measure.py auto                 # 只做 P0 + 单发
    python scripts/laser_measure.py auto --shots 5       # 再做 5 连发
"""
import argparse
import asyncio
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port  # noqa: E402
from tankctl import TankCtl           # noqa: E402

PROJ_ACCESSORS = """function pickProj() {
  const gc = GameManager.getGameController() || GameManager.gameController;
  const M = gc.roundController && gc.roundController.model;
  const cands = [
    ['model.getProjectiles', M && M.getProjectiles],
    ['gc.getProjectiles', gc.getProjectiles],
    ['rc.getProjectiles', gc.roundController && gc.roundController.getProjectiles],
    ['model.getProjectileStates', M && M.getProjectileStates],
  ];
  for (const c of cands) { try { const v = c[1] ? c[1].call(c[0].startsWith('model') ? M : gc) : null;
      if (v) return {name: c[0], value: v}; } catch (e) {} }
  return {name: null, value: {}};
}"""

P0_JS = """(() => {
  %s
  const gc = GameManager.getGameController() || GameManager.gameController;
  const M = gc.roundController.model;
  const C = window.Constants;
  const out = {};
  // 常量：SERVER 组 + 其它可能的名字
  const groups = ['SERVER', 'WEAPON_TYPES', 'COLLECTIBLE_TYPES', 'UPGRADE_TYPES'];
  out.constants = {};
  for (const g of groups) {
    try {
      const o = C[g]; if (!o) continue;
      const rec = {};
      for (const k of Object.getOwnPropertyNames(o)) {
        try { const v = o[k];
          if (typeof v === 'number' || typeof v === 'string') rec[k] = v;
          else if (v && typeof v === 'object') {
            const r2 = {};
            for (const k2 of Object.getOwnPropertyNames(v)) {
              try { const v2 = v[k2]; if (typeof v2 === 'number' || typeof v2 === 'string') r2[k2] = v2; } catch (e) {}
            }
            if (Object.keys(r2).length) rec[k] = r2;
          }
        } catch (e) {}
      }
      out.constants[g] = rec;
    } catch (e) { out.constants[g] = 'ERR'; }
  }
  // 手上武器实例字段
  const pid = Object.keys(gc.getTanks())[0];
  out.pid = pid;
  try { out.weaponType = gc.getActiveWeapon(pid).getType(); } catch (e) {}
  try {
    const w = gc.getActiveWeapon(pid);
    out.weaponFields = {};
    for (const k of Object.keys(w)) if (!/^__/.test(k)) {
      const v = w[k];
      out.weaponFields[k] = (v === null || typeof v !== 'object') ? v : String(v);
    }
    out.weaponGetters = {};
    for (const k of Object.getOwnPropertyNames(Object.getPrototypeOf(w))) {
      if (!/^(get|is|has)/.test(k)) continue;
      try { const v = w[k]; if (typeof v === 'function') { const r = v.call(w);
        if (typeof r === 'number' || typeof r === 'boolean' || typeof r === 'string') out.weaponGetters[k] = r; } } catch (e) {}
    }
  } catch (e) { out.wErr = String(e); }
  // 武器状态对象
  try {
    const w = gc.getActiveWeapon(pid);
    let st = null;
    for (const m of ['getWeaponState', 'getState', 'toObj']) {
      try { if (typeof w[m] === 'function') { st = w[m](); break; } } catch (e) {}
    }
    if (st) out.weaponState = (typeof st.toObj === 'function') ? st.toObj() : JSON.parse(JSON.stringify(st));
  } catch (e) { out.stErr = String(e); }
  out.projectileAccessor = pickProj().name;
  return JSON.stringify(out);
})()""" % PROJ_ACCESSORS

SAMPLER_START = """(() => {
  %s
  window.__ls = {frames: [], on: true, t0: performance.now()};
  const maxFrames = 600;
  const tick = () => {
    if (!window.__ls.on || window.__ls.frames.length >= maxFrames) return;
    const gc = GameManager.getGameController() || GameManager.gameController;
    const ps = pickProj().value || {};
    const rec = {t: Math.round(performance.now() - window.__ls.t0), p: {}};
    for (const k in ps) {
      const p = ps[k];
      rec.p[k] = {
        x: Math.round(p.getX() * 100) / 100, y: Math.round(p.getY() * 100) / 100,
        vx: p.getSpeedX ? Math.round(p.getSpeedX() * 10) / 10 : null,
        vy: p.getSpeedY ? Math.round(p.getSpeedY() * 10) / 10 : null,
        ta: p.getTimeAlive ? Math.round(p.getTimeAlive() * 1000) / 1000 : null,
        owner: p.getPlayerId ? p.getPlayerId() : null,
        bounces: p.getNumberOfBounces ? p.getNumberOfBounces() : null,
      };
    }
    window.__ls.frames.push(rec);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return 'ok';
})()""" % PROJ_ACCESSORS

SAMPLER_STOP = """(() => { window.__ls.on = false;
  return JSON.stringify({t0: window.__ls.t0, frames: window.__ls.frames}); })()"""


def tick_report(frames, label):
    """报告游戏真实仿真步长（用弹丸 timeAlive 的增量推断）——这决定采样率上限。"""
    from collections import Counter
    per = {}
    for f in frames:
        for pid_, p in (f.get('p') or {}).items():
            if p.get('ta') is not None:
                per.setdefault(pid_, []).append((f['t'], p['ta']))
    for pid_, s in per.items():
        s.sort()
        d = [round(s[i+1][1] - s[i][1], 4) for i in range(len(s) - 1)]
        d = [x for x in d if x > 0]
        if d:
            c = Counter(d)
            print('[%s] %s 的 timeAlive 步长（=仿真 tick）：%s  → 180m/s 下每 tick 飞 %.2fm'
                  % (label, pid_, c.most_common(4), 180.0 * c.most_common(1)[0][0]), flush=True)


def analyse(frames, label, t0=None, fire_at=None):
    """从逐帧采样里恢复每一发弹丸的轨迹（含反弹次数与真实路径长度）。"""
    per = {}
    for f in frames:
        for pid_, p in (f.get('p') or {}).items():
            per.setdefault(pid_, []).append((f['t'], p))
    out = []
    for pid_, samples in per.items():
        samples.sort()
        first, last = samples[0][1], samples[-1][1]
        speeds = [((s[1]['vx'] or 0) ** 2 + (s[1]['vy'] or 0) ** 2) ** 0.5 for s in samples]
        # 真实路径长度 + 反弹检测（相邻两段方向夹角 > 25° 记一次反弹）
        plen, bounces = 0.0, 0
        prev_ang = None
        for i in range(1, len(samples)):
            p0, p1 = samples[i-1][1], samples[i][1]
            dx, dy = p1['x'] - p0['x'], p1['y'] - p0['y']
            d = (dx * dx + dy * dy) ** 0.5
            if d < 0.05:
                continue
            plen += d
            ang = math.atan2(dy, dx)
            if prev_ang is not None:
                dev = abs((ang - prev_ang + math.pi) % (2 * math.pi) - math.pi)
                if dev > math.radians(25):
                    bounces += 1
            prev_ang = ang
        seen_s = (samples[-1][0] - samples[0][0]) / 1000.0
        delay = None
        if t0 is not None and fire_at is not None:
            delay = round((t0 + samples[0][0]) - fire_at, 1)   # 开火指令 → 弹丸首帧(ms)
        out.append({
            'id': pid_, 'frames': len(samples),
            'spawn_t_ms': samples[0][0], 'seen_ms': samples[-1][0] - samples[0][0],
            'fire_to_spawn_ms': delay,
            'speed_first': round(speeds[0], 1), 'speed_max': round(max(speeds), 1),
            'speed_last': round(speeds[-1], 1),
            'timeAlive_first': first['ta'], 'timeAlive_last': last['ta'],
            'path_len_m': round(plen, 1),
            'path_speed': round(plen / seen_s, 1) if seen_s > 0 else None,
            'bounces_from_track': bounces,
            'start': (first['x'], first['y']), 'end': (last['x'], last['y']),
            'owner': first['owner'],
        })
    out.sort(key=lambda r: r['spawn_t_ms'])
    print('[%s] 采到 %d 发弹丸：' % (label, len(out)), flush=True)
    for r in out:
        print('    %s owner=%s 首帧t=%dms(开火后 %sms) 存活=%.0fms 报告弹速=%s→%s(m/s) '
              'timeAlive %s→%s 路径长=%.1fm(实测均值 %s m/s) 反弹(轨迹推断)=%d '
              '(%.1f,%.1f)→(%.1f,%.1f)'
              % (r['id'], r['owner'], r['spawn_t_ms'], r['fire_to_spawn_ms'], r['seen_ms'],
                 r['speed_first'], r['speed_last'], r['timeAlive_first'], r['timeAlive_last'],
                 r['path_len_m'], r['path_speed'], r['bounces_from_track'],
                 r['start'][0], r['start'][1], r['end'][0], r['end'][1]), flush=True)
    if len(out) > 1:
        gaps = [out[i+1]['spawn_t_ms'] - out[i]['spawn_t_ms'] for i in range(len(out) - 1)]
        print('[%s] 射击间隔: %s ms（中位 %.0f）'
              % (label, [round(g) for g in gaps], statistics.median(gaps)), flush=True)
    return out


async def main():
    ap = argparse.ArgumentParser(description='激光参数测量')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--shots', type=int, default=1, help='连发发数（每发一次开火脉冲）')
    ap.add_argument('--out', default=None, help='把逐帧采样存成 jsonl')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    tid = next((p for p, r in roles.items() if r == 'arrows'), None) or next(iter(roles))

    print('===== P0 静态 =====', flush=True)
    d = json.loads(await ctl.cdp.eval(P0_JS))
    print('tank=%s weaponType=%s projectileAccessor=%s' % (d.get('pid'), d.get('weaponType'),
                                                           d.get('projectileAccessor')), flush=True)
    srv = (d.get('constants') or {}).get('SERVER') or {}
    hot = {k: v for k, v in srv.items() if not isinstance(v, dict) or True}
    print('Constants.SERVER = %s' % json.dumps(hot, ensure_ascii=False)[:1200], flush=True)
    print('weaponFields = %s' % json.dumps(d.get('weaponFields'), ensure_ascii=False), flush=True)
    print('weaponGetters = %s' % json.dumps(d.get('weaponGetters'), ensure_ascii=False), flush=True)
    print('weaponState = %s' % json.dumps(d.get('weaponState'), ensure_ascii=False), flush=True)

    if str(d.get('weaponType')) != '0':
        print('[warn] 当前激活武器不是激光(type=%s)，先跑 laser_inspect.py' % d.get('weaponType'), flush=True)

    print('===== P1 实弹采样 =====', flush=True)
    await ctl.cdp.eval(SAMPLER_START)
    fire_at = await ctl.cdp.eval('performance.now()')      # 页面时钟：开火指令时刻
    for i in range(max(1, args.shots)):
        await ctl.set_input(tid, fire=True, hold=0.12)
        await asyncio.sleep(0.55 if args.shots > 1 else 0.2)
    await asyncio.sleep(1.6)
    payload = json.loads(await ctl.cdp.eval(SAMPLER_STOP))
    frames = payload.get('frames', [])
    t0 = payload.get('t0')
    print('[P1] 采样帧数=%d  采样器时钟 t0=%.0f  开火指令时刻=%.0f'
          % (len(frames), t0 or 0, fire_at), flush=True)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            for f in frames:
                fh.write(json.dumps(f, ensure_ascii=False) + '\n')
        print('[P1] 采样已存 %s（%d 帧）' % (args.out, len(frames)), flush=True)
    rows = analyse(frames, 'P1', t0=t0, fire_at=fire_at)
    tick_report(frames, 'P1')

    after = json.loads(await ctl.cdp.eval(P0_JS))
    print('[P1] 射击后 激活武器=%s weaponFields=%s'
          % (after.get('weaponType'), json.dumps(after.get('weaponFields'), ensure_ascii=False)), flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
