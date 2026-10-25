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
# -----------------------------------------------------------------------------
# ⚠️ 在线房间工具附加声明 / ONLINE-ROOM NOTICE  [DISCLAIMER-STAMP-ONLINE]
# 本脚本仅限【受控自建私人房间 / 本地研究环境】使用。
# 在公开在线对局使用自动化/辅助工具违反官方社区规则，可能导致账号被永久封禁
# （官方规则第 8 条 "No hacking on the website"，零容忍、无警告）。
# 请勿将本脚本用于干扰他人游戏体验或任何商业作弊服务。
# -----------------------------------------------------------------------------
"""
毒圈（Radioactive Fog / Storm Zone）在线实测探针
================================================
背景：毒圈只在**在线房间**出现（服务端机制，用于强制回合结束）。官方常量：
  CLASSIC_STORM_APPEAR_DURATION=60      经典模式回合开始 60s 后出现毒圈
  DEATHMATCH_STORM_APPEAR_DURATION=30   死斗 30s 后出现
  STORM_ZONE_START_GROW_TIME=5          出现前生长动画 5s
  STORM_ZONE_EXPANSION_TIME=10          每步缩圈 10s

本脚本在线房间内：
  1. 逐 2s 采样 `gc.getZones()`（Zone 字段）与我们的 `get_storm_zones` 输出
  2. 每 8s 发一次轻微转向脉冲（**防止 60s AFK 踢线**，`MAX_INACTIVITY_TIME=60000`）
  3. 记录：毒圈出现时刻、方向序列消耗、每步缩圈耗时、tile 边界变化、坦克安全区内外

用法：
    python scripts/start_online_game.py auto --create --players 1 --no-leave --no-reload
    python scripts/probe_storm.py auto --dur 150
"""
import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tankctl import TankCtl                          # noqa: E402

SNAP = """(() => { const gc = GameManager.getGameController();
  if (!gc) return JSON.stringify({err: 'no game'});
  const out = {state: String(gc.model.getState()), zones: [], tanks: {}, safe: null};
  try {
    const zs = gc.getZones ? gc.getZones() : {};
    for (const zid in zs) {
      const z = zs[zid];
      let f = null;
      try { f = z.getFields ? z.getFields() : null; } catch (e) {}
      let nTiles = null;
      try { nTiles = z.getTiles ? z.getTiles().length : null; } catch (e) {}
      out.zones.push({id: zid, type: z.getType ? z.getType() : null, nTiles: nTiles,
        fields: f ? JSON.parse(JSON.stringify(f)) : null});
    }
  } catch (e) { out.zErr = String(e); }
  try { const ts = gc.getTanks();
    for (const pid in ts) { const t = ts[pid];
      out.tanks[pid] = {x: +t.getX().toFixed(1), y: +t.getY().toFixed(1),
                        alive: t.isAlive ? t.isAlive() : null}; } } catch (e) {}
  return JSON.stringify(out); })()"""


def safe_of(zone):
    f = zone.get('fields') or {}
    tb = f.get('_tileBounds') or {}
    if not tb:
        return None
    return {
        'minX': tb.get('minX', 0) + (f.get('_expansionTilesLeft') or 0),
        'minY': tb.get('minY', 0) + (f.get('_expansionTilesTop') or 0),
        'maxX': tb.get('maxX', 0) - (f.get('_expansionTilesRight') or 0),
        'maxY': tb.get('maxY', 0) - (f.get('_expansionTilesBottom') or 0),
    }


async def main():
    ap = argparse.ArgumentParser(description='毒圈在线实测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--dur', type=float, default=150, help='最长采样秒数')
    ap.add_argument('--json', default=None)
    ap.add_argument('--keepalive', type=float, default=8.0, help='保活脉冲间隔（秒）')
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    roles = await ctl.detect_roles()
    pids = list(roles.keys())
    pid = pids[0] if pids else None
    if not pid:
        print('[storm] 无坦克（需先进在线房间）')
        return 1
    print('[storm] 坦克=%s roles=%s，最长采样 %.0fs' % (pid, roles, args.dur), flush=True)
    print('[storm] ⚠️ 保活必须覆盖【所有】角色：只脉冲一个角色的其余角色会被 60s AFK 踢掉，'
          '而 Last Tank Standing 会因"最后存活者"直接结束回合（2026-10-24 实测踩坑）', flush=True)

    t0 = time.time()
    last_pulse = t0
    round_t0 = None
    hist = []
    prev_key = None
    last_round = None
    while time.time() - t0 < args.dur:
        raw = await ctl.cdp.eval(SNAP)
        s = json.loads(raw)
        now = time.time() - t0
        if s.get('state') == 'in round':
            if round_t0 is None:
                round_t0 = now
                print('[storm] t=%5.1fs 进入回合（毒圈倒计时起点）' % now, flush=True)
            if last_round != 'in round':
                last_round = 'in round'
        else:
            last_round = s.get('state')

        zones = s.get('zones') or []
        storm = next((z for z in zones if (z.get('fields') or {}).get('_tileBounds')), None)
        key = (s.get('state'), len(zones),
               tuple(sorted((z['id'], z.get('type')) for z in zones)),
               (storm.get('fields') or {}).get('_expansionSide') if storm else None,
               tuple(sorted(((storm.get('fields') or {}).get(k) or 0) for k in
                            ('_expansionTilesRight', '_expansionTilesBottom',
                             '_expansionTilesLeft', '_expansionTilesTop'))) if storm else None)
        rec = {'t': round(now, 1), 'state': s.get('state'), 'nZones': len(zones),
               'storm': None, 'safe': None, 'tanks': s.get('tanks')}
        if storm:
            f = storm.get('fields') or {}
            rec['storm'] = {
                'type': storm.get('type'), 'nTiles': storm.get('nTiles'),
                'side': f.get('_expansionSide'),
                'expansionTime': f.get('_expansionTime'),
                'seqLen': len(f.get('_expansionSequence') or []),
                'seqHead': (f.get('_expansionSequence') or [])[:8],
                'r': f.get('_expansionTilesRight'), 'b': f.get('_expansionTilesBottom'),
                'l': f.get('_expansionTilesLeft'), 't': f.get('_expansionTilesTop'),
                'bounds': f.get('_tileBounds'),
            }
            rec['safe'] = safe_of(storm)
        hist.append(rec)
        if key != prev_key:
            prev_key = key
            if storm:
                print('[storm] t=%5.1fs 状态=%s 毒圈出现! type=%s tiles=%s side=%s expTime=%s 序列长=%d 安全区=%s'
                      % (now, s.get('state'), storm.get('type'), storm.get('nTiles'),
                         (storm.get('fields') or {}).get('_expansionSide'),
                         (storm.get('fields') or {}).get('_expansionTime'),
                         len((storm.get('fields') or {}).get('_expansionSequence') or []),
                         json.dumps(rec['safe'], ensure_ascii=False)), flush=True)
            else:
                print('[storm] t=%5.1fs 状态=%s zones=%d（无毒圈）'
                      % (now, s.get('state'), len(zones)), flush=True)

        # 保活脉冲（防止 AFK 踢线）——★ 必须覆盖所有可控角色
        if time.time() - last_pulse >= args.keepalive:
            last_pulse = time.time()
            alive_roles = await ctl.detect_roles()
            for p in list(alive_roles.keys()):
                try:
                    await ctl.set_input(p, right=True, hold=0.12)
                    await ctl.set_input(p, left=True, hold=0.12)
                except Exception as e:
                    print('[storm] 保活失败(%s): %s' % (p, e), flush=True)
        await asyncio.sleep(2.0)

    # 汇总
    storms = [h for h in hist if h.get('storm')]
    print('\n[storm] ===== 汇总 =====', flush=True)
    if not storms:
        print('  采样 %.0fs 内没有出现毒圈（经典模式需 60s，死斗 30s；可能回合提前结束）' % args.dur,
              flush=True)
    else:
        first = storms[0]
        print('  首次出现: t=%ss（回合开始 t=%s → 延迟 %.1fs）'
              % (first['t'], round_t0, first['t'] - (round_t0 or 0)), flush=True)
        sides = []
        for h in storms:
            sd = h['storm']['side']
            if not sides or sides[-1] != sd:
                sides.append(sd)
        print('  方向变化序列: %s' % sides, flush=True)
        exp_times = [h['storm']['expansionTime'] for h in storms if h['storm'].get('expansionTime') is not None]
        if exp_times:
            print('  _expansionTime 范围: %.2f ~ %.2f（每步 %.0fs 倒计时）'
                  % (min(exp_times), max(exp_times), 10.0), flush=True)
        print('  最后安全区: %s' % json.dumps(storms[-1]['safe'], ensure_ascii=False), flush=True)
        print('  毒圈数量: %d（tiles=%s）' % (len({h['storm']['nTiles'] for h in storms}),
                                             sorted({h['storm']['nTiles'] for h in storms})), flush=True)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'round_t0': round_t0, 'hist': hist}, fh, ensure_ascii=False, indent=1)
        print('→ %s' % args.json)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
