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
"""审计器：在**不改子智能体代码**的前提下复跑它的 agent_ai.py，并记录原语调用。

用法（在项目根目录）：
    python scripts/prim_audit.py --seconds 60
    python scripts/prim_audit.py --seconds 60 --script agent_ai.py

审计内容：
  * set_input 调用次数、按 pid 统计（自动判定是否误控别的角色）；
  * fire=True 的次数（开火数）；
  * state()/velocity()/projectile_path() 调用次数与耗时；
  * 每 0.5s 采样一次比分（kills/victories）用于事后归因；
  * 运行结束时输出 JSON 报告到 scripts/scratch/prim_audit_<ts>.json。
"""
import argparse
import asyncio
import json
import os
import runpy
import sys
import time

BB = os.environ.get('TT_BB_DIR') or r'D:\AI Agent\tt_bb_lowlevel'
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

if BB not in sys.path:
    sys.path.insert(0, BB)
os.chdir(BB)

import ttprim  # noqa: E402
from ttprim import Prim  # noqa: E402

STATS = {
    'calls': {'set_input': 0, 'state': 0, 'velocity': 0, 'projectile_path': 0,
              'position': 0, 'rotation': 0, 'release': 0},
    'pids': {},
    'fires': 0,
    'scores_samples': [],
    'latency_ms': {'state': [], 'projectile_path': []},
    'my_pid': None,
    'started': None,
}

_orig = {
    'set_input': Prim.set_input,
    'state': Prim.state,
    'velocity': Prim.velocity,
    'projectile_path': Prim.projectile_path,
    'position': Prim.position,
    'rotation': Prim.rotation,
    'release': Prim.release,
}


def _track(name):
    async def wrapper(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return await _orig[name](self, *a, **kw)
        finally:
            STATS['calls'][name] += 1
            ms = (time.perf_counter() - t0) * 1000.0
            if name in STATS['latency_ms']:
                STATS['latency_ms'][name].append(round(ms, 2))
    return wrapper


async def _set_input(self, pid, fwd=False, back=False, left=False,
                     right=False, fire=False):
    STATS['calls']['set_input'] += 1
    key = str(pid)
    STATS['pids'][key] = STATS['pids'].get(key, 0) + 1
    if fire:
        STATS['fires'] += 1
    return await _orig['set_input'](self, pid, fwd=fwd, back=back, left=left,
                                    right=right, fire=fire)


async def _state(self):
    STATS['calls']['state'] += 1
    st = await _orig['state'](self)
    try:
        if isinstance(st, dict) and st.get('roles') and STATS['my_pid'] is None:
            role = self.role_hint
            for pid, r in st['roles'].items():
                if r == role:
                    STATS['my_pid'] = pid
                    break
        if isinstance(st, dict):
            STATS['scores_samples'].append({
                't': round(time.time() - STATS['started'], 2),
                'kills': st.get('scoresKill') or {},
                'vict': st.get('scoresVict') or {},
                'alive': st.get('alive'),
                'active': st.get('roundActive'),
            })
    except Exception:
        pass
    return st


async def _sampler(seconds):
    """独立采样比分（即使子智能体不调 state 也能量化结果）。"""
    p = Prim()
    try:
        await p.connect()
        end = time.time() + seconds
        while time.time() < end:
            try:
                st = await _orig['state'](p)
                STATS['scores_samples'].append({
                    't': round(time.time() - STATS['started'], 2),
                    'kills': st.get('scoresKill') or {},
                    'vict': st.get('scoresVict') or {},
                    'alive': st.get('alive'),
                    'active': st.get('roundActive'),
                })
            except Exception:
                pass
            await asyncio.sleep(0.5)
    finally:
        try:
            await p.close()
        except Exception:
            pass


def install():
    Prim.set_input = _set_input
    Prim.state = _state
    for name in ('velocity', 'projectile_path', 'position', 'rotation', 'release'):
        setattr(Prim, name, _track(name))


def summarize(seconds):
    samples = [s for s in STATS['scores_samples'] if s.get('kills') is not None]
    base_k = (samples[0].get('kills') or {}) if samples else {}
    base_v = (samples[0].get('vict') or {}) if samples else {}
    kills = {}
    vict = {}
    for s in samples:
        for pid, v in (s.get('kills') or {}).items():
            kills[pid] = max(kills.get(pid, -10**9), v - (base_k.get(pid) or 0))
        for pid, v in (s.get('vict') or {}).items():
            vict[pid] = max(vict.get(pid, -10**9), v - (base_v.get(pid) or 0))
    for pid in set(base_k) | set(base_v):
        kills.setdefault(pid, 0)
        vict.setdefault(pid, 0)
    my = STATS['my_pid']
    opp = [p for p in (kills.keys() | vict.keys()) if p != my]
    out = {
        'my_pid': my,
        'control_by_pid': STATS['pids'],
        'violations': {k: v for k, v in STATS['pids'].items()
                       if my is not None and k != str(my)},
        'fires': STATS['fires'],
        'calls': STATS['calls'],
        'final_kills': kills,
        'final_victories': vict,
        'my_kills_est': kills.get(str(my), kills.get(my)) if my else None,
        'opp_kills_est': [kills.get(str(p)) for p in opp] if opp else None,
        'notes': '命中/自伤归因：我方 kills 增量=命中；对手 victories 增量≈我自伤（自杀送对手胜）',
    }
    lat = STATS['latency_ms']
    for k, arr in lat.items():
        if arr:
            arr_sorted = sorted(arr)
            out['latency_ms_%s' % k] = {
                'n': len(arr), 'p50': arr_sorted[len(arr_sorted) // 2],
                'p95': arr_sorted[int(len(arr_sorted) * 0.95)],
                'max': arr_sorted[-1],
            }
    return out


def main():
    ap = argparse.ArgumentParser(description='纯原语黑盒审计运行器')
    ap.add_argument('--seconds', type=float, default=60.0)
    ap.add_argument('--script', default='agent_ai.py')
    ns = ap.parse_args()
    script = os.path.join(BB, ns.script)
    if not os.path.exists(script):
        print('[audit] 找不到脚本: %s' % script)
        return 2
    install()
    STATS['started'] = time.time()
    sys.argv = [ns.script, '--seconds', str(int(ns.seconds))]

    async def runner():
        sampler = asyncio.create_task(_sampler(ns.seconds + 1.5))
        try:
            # 在同一个事件循环里执行子智能体的脚本
            await asyncio.to_thread(runpy.run_path, script, run_name='__main__')
        finally:
            sampler.cancel()
            try:
                await sampler
            except BaseException:
                pass

    rc = 0
    try:
        asyncio.run(runner())
    except SystemExit as e:
        rc = int(e.code or 0)

    summary = summarize(ns.seconds)
    outdir = os.path.join(ROOT, 'scripts', 'scratch')
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(outdir, 'prim_audit_%d.json'
                           % int(STATS['started']))
    with open(outfile, 'w', encoding='utf-8') as fh:
        json.dump({'seconds': ns.seconds, 'script': ns.script,
                   'summary': summary}, fh, ensure_ascii=False, indent=2)
    print('\n[audit] ===== 审计报告 =====')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('[audit] 写入: %s' % outfile)
    return rc


if __name__ == '__main__':
    sys.exit(main())
