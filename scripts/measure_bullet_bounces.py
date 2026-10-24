#!/usr/bin/env python
"""
弹丸反弹/寿命实测（游戏内）
============================
目的：确认弹丸消亡的真实条件——是"反弹次数上限"还是"存活时间上限"。
做法：开一枪，页面内 60Hz 采样弹丸对象：
  p.timeAlive / p.lifetime / p.timeoutWindow / p.timeoutBounceCount /
  p.bounces.length（反弹时间戳数量）/ 位置
记录到弹丸消失为止，报告：最大反弹数、消亡时的 timeAlive、以及各字段实际值。

用法：python scripts/measure_bullet_bounces.py auto [--json out.json]
"""
import argparse
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'cdp_tools'))
sys.path.insert(0, HERE)

from ego_browser import resolve_port                # noqa: E402
from tankctl import TankCtl                          # noqa: E402

SAMPLER = """(() => {
  window.__bb = {on: true, frames: [], seen: false};
  const step = () => {
    if (!window.__bb.on || window.__bb.frames.length > 900) return;
    const g = GameManager.getGameController();
    if (g) { try { const ps = g.roundController.model.getProjectiles();
      const ids = Object.keys(ps);
      if (ids.length) {
        window.__bb.seen = true;
        const p = ps[ids[0]];
        window.__bb.frames.push({
          t: Math.round(performance.now()),
          timeAlive: p.timeAlive, lifetime: p.lifetime,
          win: p.timeoutWindow, bmax: p.timeoutBounceCount,
          bounces: (p.bounces || []).length,
          x: +p.getX().toFixed(2), y: +p.getY().toFixed(2)});
      } else if (window.__bb.seen) {
        window.__bb.frames.push({gone: true, t: Math.round(performance.now())});
      } } catch (e) {}
    }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step); return 'ok'; })()"""

STOP = "(() => { window.__bb.on = false; return JSON.stringify(window.__bb.frames); })()"
READ = "(() => { return JSON.stringify(window.__bb ? window.__bb.frames : []); })()"


async def main():
    ap = argparse.ArgumentParser(description='弹丸反弹/寿命实测')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--angle', type=float, default=None, help='可选：强制炮管角（世界角，度）')
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    port = resolve_port(args.port)
    ctl = TankCtl(port=port)
    await ctl.connect()
    pid = next(iter(await ctl.detect_roles()))
    await ctl.cdp.eval(SAMPLER)
    await ctl.set_input(pid, fire=True, hold=0.5)
    # 等到弹丸消失或超时（READ 不停止采样）
    frames = []
    for _ in range(40):
        await asyncio.sleep(0.5)
        raw = await ctl.cdp.eval(READ)
        frames = json.loads(raw) if raw else []
        if frames and frames[-1].get('gone'):
            break
        if frames and frames[-1].get('timeAlive', 0) > 20:
            break
    await ctl.cdp.eval(STOP)
    data = [f for f in frames if not f.get('gone')]
    if not data:
        print('[bb] 没采到弹丸')
        return 1
    print('[bb] 采样帧数=%d  寿命字段=%s  窗口=%s  反弹上限字段=%s'
          % (len(data), data[0].get('lifetime'), data[0].get('win'), data[0].get('bmax')))
    max_b = max(f['bounces'] for f in data)
    last = data[-1]
    t_first = data[0]['t']
    print('[bb] 最大反弹计数=%d  末帧: timeAlive=%.2fs bounces=%d' %
          (max_b, last.get('timeAlive') or 0, last.get('bounces') or 0))
    # 时间线（反弹计数变化点）
    prev = None
    for f in data:
        b = f['bounces']
        if b != prev:
            prev = b
            print('  t=%6.2fs timeAlive=%5.2fs bounces=%d pos=(%.1f,%.1f)'
                  % ((f['t'] - t_first) / 1000.0, f.get('timeAlive') or 0, b, f['x'], f['y']))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        print('→ %s' % args.json)
    await ctl.cdp.close()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
