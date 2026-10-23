#!/usr/bin/env python
"""
瞄准角 ↔ 坦克 rot 标定
=======================
要回答三个问题：
  A. `setRotation(v)` 之后 `getRotation()` 实际是多少？（是否归一化/有偏移/随时间收敛）
  B. 开火方向与 rot 的关系到底是什么？（我们文档假设 forward = rot − π/2）
  C. 渐进转向 `turn_to_rot` 的误差有多大？（决定反弹弹为何打空）

用法：python scripts/calibrate_aim_rot.py auto [--json out.json]
"""
import argparse
import asyncio
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tank_behavior_template import TankBehavior      # noqa: E402

GET_ROT = """(() => { const gc = GameManager.getGameController();
  const pid = %s; const t = gc.getTank(pid);
  return t ? JSON.stringify({rot: t.getRotation(), x: t.getX(), y: t.getY()}) : 'null'; })()"""

SET_ROT = """(() => { const gc = GameManager.getGameController();
  const pid = %s; const t = gc.getTank(pid);
  if (!t) return 'null';
  t.setRotation(%f);
  return JSON.stringify({set: %f, readback: t.getRotation()}); })()"""

SAMPLER = """(() => { window.__cal = {on: true, frames: []};
  const step = () => { if (!window.__cal.on || window.__cal.frames.length > 120) return;
    const g = GameManager.getGameController();
    if (g) { try { const ps = g.roundController.model.getProjectiles(); const ids = Object.keys(ps);
      if (ids.length) { const p = ps[ids[0]];
        window.__cal.frames.push([p.getX(), p.getY()]); } } catch (e) {} }
    requestAnimationFrame(step); };
  requestAnimationFrame(step); return 'ok'; })()"""

STOP = "(() => { window.__cal.on = false; return JSON.stringify(window.__cal.frames); })()"


async def main():
    ap = argparse.ArgumentParser(description='瞄准角↔rot 标定')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    bhv = TankBehavior(port=port)
    await bhv.connect()
    roles = await bhv.get_roles()
    pid = next(iter(roles), None)
    if not pid:
        print('[cal] 无坦克（先开一局本地游戏）')
        return 1
    print('[cal] 坦克=%s roles=%s' % (pid, roles), flush=True)

    # ---------- A. setRotation 映射 ----------
    print('\n[cal] ===== A. setRotation(v) → getRotation() =====', flush=True)
    mapping = []
    for v in (-2 * math.pi, -math.pi, -1.0, 0.0, 0.5, 1.0, math.pi / 2, 2.0, 3.0,
              math.pi, 4.0, 5.0, 6.0, 2 * math.pi, 6.912, 12.5):
        r = json.loads(await bhv.ctl.cdp.eval(SET_ROT % (json.dumps(pid), v, v)))
        await asyncio.sleep(0.06)
        r2 = json.loads(await bhv.ctl.cdp.eval(GET_ROT % json.dumps(pid)))
        norm = (v + math.pi) % (2 * math.pi) - math.pi
        mapping.append({'set': v, 'immediate': r['readback'], 'after60ms': r2['rot'],
                        'expected_normalized': norm})
        print('  set=%8.3f  立即读=%8.3f  60ms后=%8.3f  (归一化预期=%8.3f)'
              % (v, r['readback'], r2['rot'], norm), flush=True)
        await asyncio.sleep(0.05)

    # ---------- B. 开火方向 vs rot ----------
    print('\n[cal] ===== B. 开火方向 vs rot（forward = rot − π/2 ?）=====', flush=True)
    directions = []
    for rot in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
        await bhv.ctl.cdp.eval(SET_ROT % (json.dumps(pid), rot, rot))
        await asyncio.sleep(0.15)
        cur = json.loads(await bhv.ctl.cdp.eval(GET_ROT % json.dumps(pid)))
        await bhv.ctl.cdp.eval(SAMPLER)
        await bhv.ctl.set_input(pid, fire=True, hold=0.35)
        frames = json.loads(await bhv.ctl.cdp.eval(STOP) or '[]')
        if len(frames) < 3:
            print('  rot=%.3f：采样不足' % rot, flush=True)
            directions.append({'rot': rot, 'ok': False})
            continue
        f0, f1 = frames[0], frames[1]
        ang = math.degrees(math.atan2(f1[1] - f0[1], f1[0] - f0[0]))
        expect_deg = math.degrees(rot - math.pi / 2)
        err = (ang - expect_deg + 180) % 360 - 180
        directions.append({'rot': rot, 'readback': cur['rot'], 'bullet_dir_deg': ang,
                           'rot_minus_90_deg': expect_deg, 'err_deg': err, 'ok': True})
        print('  set rot=%6.3f 读回=%6.3f → 弹道方向=%7.2f°  预期(rot−90°)=%7.2f°  误差=%6.2f°'
              % (rot, cur['rot'], ang, expect_deg, err), flush=True)
        await asyncio.sleep(0.5)

    # ---------- C. 渐进转向精度 ----------
    print('\n[cal] ===== C. 渐进转向 turn_to_rot 精度 =====', flush=True)
    turns = []
    for target_deg in (30.0, 90.0, 150.0, 210.0, 300.0):
        target = math.radians(target_deg) + math.pi / 2
        ok = await bhv.turn_to_rot(pid, target)
        await asyncio.sleep(0.15)
        cur = json.loads(await bhv.ctl.cdp.eval(GET_ROT % json.dumps(pid)))
        d = (cur['rot'] - target + math.pi) % (2 * math.pi) - math.pi
        turns.append({'target': target, 'actual': cur['rot'], 'err_rad': d, 'ok': ok})
        print('  目标 rot=%7.3f → 实际=%7.3f  误差=%7.4f rad (%5.2f°)  turn_to_rot=%s'
              % (target, cur['rot'], d, math.degrees(d), ok), flush=True)
        await asyncio.sleep(0.2)

    # ---------- 汇总 ----------
    print('\n[cal] ===== 汇总 =====', flush=True)
    errs = [abs(x['err_deg']) for x in directions if x.get('ok')]
    if errs:
        print('  B. 弹道方向 vs (rot−90°)：平均误差 %.2f°，最大 %.2f°'
              % (sum(errs) / len(errs), max(errs)))
    terrs = [abs(x['err_rad']) for x in turns if x.get('actual') is not None]
    if terrs:
        print('  C. turn_to_rot 残差：平均 %.4f rad（%.2f°），最大 %.4f rad（%.2f°）'
              % (sum(terrs) / len(terrs), math.degrees(sum(terrs) / len(terrs)),
                 max(terrs), math.degrees(max(terrs))))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump({'mapping': mapping, 'directions': directions, 'turns': turns},
                      fh, ensure_ascii=False, indent=1)
        print('→ %s' % args.json)
    await bhv.ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
