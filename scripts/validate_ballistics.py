#!/usr/bin/env python
"""
TankTrouble — 弹道模型离线验证（用真实录制逐条比对）
====================================================
拿录制里**真实弹丸**的观测点，去比对我们模型的预测折线，量化误差：

  对每一发弹丸：
    1. 取它的第一帧观测（位置 vx/vy timeAlive）作为"出膛点 + 初始方向"
    2. 用我们的模型从**该出膛点**按**实测初始角**预测整条折线
       （弹道 = 官方 p2 射线；命中判定 = 我们自建，见 official_ballistics.py）
    3. 计算真实观测点到预测折线的距离（双向：观测→预测 与 预测→观测）
    4. 统计：路径长度差、反弹次数差、平均/最大偏差

不需要实机，只用浏览器里的 p2 碰撞世界做射线（离线跑，秒级）。

用法：
    python scripts/validate_ballistics.py scripts/fire_strict_v7.jsonl [--limit 40]
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

from recorder import CompressedRecorder           # noqa: E402
from official_ballistics import OfficialBallistics  # noqa: E402
from tankctl import TankCtl                       # noqa: E402
from ego_browser import resolve_port              # noqa: E402

SPEED = 18.0


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    L2 = vx * vx + vy * vy
    t = (wx * vx + wy * vy) / L2 if L2 > 1e-12 else 0.0
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def poly_dist(px, py, poly):
    return min(seg_dist(px, py, poly[i-1]['x'], poly[i-1]['y'], poly[i]['x'], poly[i]['y'])
               for i in range(1, len(poly))) if len(poly) > 1 else 1e9


def load_shots(path, limit=None):
    """收集每发弹丸的观测序列（第一帧 + 后续帧）。

    支持两种输入：
      * 压缩录制（`CompressedRecorder`）：`recorder.iter_expanded`
      * **页面内 60Hz 采样帧**（`measure_spread.py --frames-out`）：
        每行 {t, rot, p:{id:{x,y,vx,vy,ta,owner}}} —— 无丢帧，弹道验证首选
    """
    per = {}
    first_line = ''
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                first_line = line
                break
    import json as _json
    try:
        probe = _json.loads(first_line) if first_line else {}
    except Exception:
        probe = {}
    fast = 'p' in probe and 't' in probe

    if fast:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                if not line.strip():
                    continue
                f = _json.loads(line)
                rots = f.get('rot') or {}
                for pid, p in (f.get('p') or {}).items():
                    if p.get('vx') is None:
                        continue
                    own = str(p.get('owner'))
                    per.setdefault(pid, []).append({
                        'x': p['x'], 'y': p['y'], 'vx': p.get('vx'), 'vy': p.get('vy'),
                        'ta': p.get('ta'), 'owner': own, 'dead': None,
                        'tank': (rots.get(own, {}).get('x'), rots.get(own, {}).get('y'))
                                if isinstance(rots.get(own), dict) else None,
                    })
    else:
        for rec, full in CompressedRecorder.iter_expanded(path):
            tanks = full.get('tanks') or {}
            for p in (full.get('projectiles') or []):
                pid = p.get('id')
                if not pid:
                    continue
                own = str(p.get('owner'))
                t = tanks.get(own)
                per.setdefault(pid, []).append({
                    'x': p['x'], 'y': p['y'], 'vx': p.get('vx'), 'vy': p.get('vy'),
                    'ta': p.get('timeAlive'), 'owner': own, 'dead': p.get('deadlyToOwner'),
                    'tank': (t['x'], t['y']) if t else None,
                })

    shots = []
    for pid, s in per.items():
        s = [x for x in s if x['vx'] is not None]
        if len(s) < 3:
            continue
        s.sort(key=lambda x: (x['ta'] if x['ta'] is not None else 0))
        shots.append({'id': pid, 'obs': s})
    shots.sort(key=lambda s: -len(s['obs']))
    return shots[:limit] if limit else shots


async def main():
    ap = argparse.ArgumentParser(description='弹道模型离线验证')
    ap.add_argument('recording')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--limit', type=int, default=25)
    args = ap.parse_args()

    shots = load_shots(args.recording, args.limit)
    print('[val] 参与验证的弹丸：%d 发' % len(shots), flush=True)
    if not shots:
        return 1

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    ob = OfficialBallistics(ctl)

    obs_to_pred, pred_to_obs, bounce_err, len_err = [], [], [], []
    for i, sh in enumerate(shots):
        s0 = sh['obs'][0]
        angle = math.degrees(math.atan2(s0['vy'], s0['vx']))
        # 真实飞行时长 → 预算长度
        ta_max = max(x['ta'] or 0 for x in sh['obs'])
        budget = SPEED * (ta_max + 0.1)
        r = await ob.predict_path(s0['owner'], angle, weapon='bullet', budget=budget,
                                 origin={'x': s0['x'], 'y': s0['y']})
        poly = r.get('path') or []
        if len(poly) < 2:
            continue
        # 观测点 → 预测折线
        d_obs = [poly_dist(x['x'], x['y'], poly) for x in sh['obs']]
        # 预测折线端点 → 真实观测点集
        d_pred = [min(math.hypot(p['x'] - x['x'], p['y'] - x['y']) for x in sh['obs']) for p in poly]
        obs_to_pred.append(statistics.mean(d_obs))
        pred_to_obs.append(statistics.mean(d_pred))
        # 反弹次数：真实(轨迹方向变化) vs 预测
        b_real = 0
        prev = None
        for j in range(1, len(sh['obs'])):
            dx = sh['obs'][j]['x'] - sh['obs'][j-1]['x']
            dy = sh['obs'][j]['y'] - sh['obs'][j-1]['y']
            if math.hypot(dx, dy) < 0.2:
                continue
            a = math.atan2(dy, dx)
            if prev is not None and abs((a - prev + math.pi) % (2 * math.pi) - math.pi) > math.radians(25):
                b_real += 1
            prev = a
        bounce_err.append(abs(b_real - (r.get('bounces') or 0)))
        real_len = SPEED * (ta_max - (s0['ta'] or 0))
        len_err.append(abs(real_len - (r.get('travel') or 0)))
        if i < 8:
            print('  [%s] 观测%2d 帧 真实反弹≈%2d 预测反弹=%2d 真实长≈%5.1fm 预测长=%5.1fm '
                  '观测→预测 平均偏差=%.3fm 最大=%.3fm'
                  % (sh['id'], len(sh['obs']), b_real, r.get('bounces') or 0, real_len,
                     r.get('travel') or 0, statistics.mean(d_obs), max(d_obs)), flush=True)

    print()
    print('[val] ===== 汇总（%d 发）=====' % len(obs_to_pred), flush=True)
    if obs_to_pred:
        print('[val] 真实观测点 → 预测折线 距离：中位 %.3fm  平均 %.3fm  p90 %.3fm'
              % (statistics.median(obs_to_pred), statistics.mean(obs_to_pred),
                 sorted(obs_to_pred)[int(0.9 * (len(obs_to_pred) - 1))]), flush=True)
        print('[val] 预测折线 → 真实观测点 距离：中位 %.3fm  平均 %.3fm'
              % (statistics.median(pred_to_obs), statistics.mean(pred_to_obs)), flush=True)
        print('[val] 反弹次数绝对误差：中位 %.1f  平均 %.2f  完全一致 %d/%d'
              % (statistics.median(bounce_err), statistics.mean(bounce_err),
                 sum(1 for e in bounce_err if e == 0), len(bounce_err)), flush=True)
        print('[val] 路径长度绝对误差(m)：中位 %.2f  平均 %.2f'
              % (statistics.median(len_err), statistics.mean(len_err)), flush=True)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
