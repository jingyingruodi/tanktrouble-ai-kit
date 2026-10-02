#!/usr/bin/env python
"""
RL 契约端到端演示（AI-B 集成样例）
==================================
把整条链路串一遍：**观测 → 动作 → step → 奖励/终止 → 带动作标签的录制 → 数据集**。

用法：
  python scripts\\rl_demo.py --fake --steps 40 --record %TEMP%\\demo.jsonl
      # 离线：用 FakeBackend 造一段合理状态序列，不需要浏览器（可随时自检）

  python scripts\\rl_demo.py 51109 --steps 200 --record data\\rl_demo.jsonl
      # 真机：连 CDP（auto 也行），跑随机策略，录制 state+action 对

  python scripts\\rl_demo.py auto --policy noop --steps 20 --json
"""
import argparse
import asyncio
import json
import math
import os
import random
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'cdp_tools'))
sys.path.insert(0, os.path.join(BASE, 'scripts'))

from rl.actions import NUM_ACTIONS, action_to_inputs, ACTION_NAMES  # noqa: E402
from rl.env import TankTroubleEnv, FakeBackend, LiveBackend         # noqa: E402


def _state(x, y, rot, alive=True, kill=0, vict=0, weapon=-1, round_active=True,
           opp=(60.0, 20.0), proj=None):
    tanks = {'opp': {'x': opp[0], 'y': opp[1], 'rot': 0.5, 'weapon': -1}}
    if alive:
        tanks['p1'] = {'x': x, 'y': y, 'rot': rot, 'weapon': weapon, 'vx': 0.0, 'vy': 0.0}
    return {'tanks': tanks, 'roundActive': round_active,
            'scoresKill': {'p1': kill}, 'scoresVict': {'p1': vict},
            'maze': {'w': 8, 'h': 5}, 'projectiles': proj or [],
            'collectibles': [{'x': 20.0, 'y': 20.0, 'type': 3}], 'mines': [],
            'walls': [], 'roles': {'p1': 'arrows'}}


def fake_script(steps):
    """造一段状态：前进 → 捡武器 → 击杀（终止）→ 新回合。"""
    states = []
    for i in range(steps):
        x = 5.0 + i * 1.2
        if i == steps // 3:
            states.append(_state(x, 10.0, 0.0, weapon=0))          # 捡到激光
        elif i == steps - 2:
            states.append(_state(x, 10.0, 0.0, kill=1))            # 击杀（下一步 done）
        elif i == steps - 1:
            states.append(_state(5.0, 5.0, 0.0, kill=1))          # 新回合开局
        else:
            states.append(_state(x, 10.0, 0.0,
                                 proj=[{'x': x + 6, 'y': 10.0, 'vx': -18, 'vy': 0,
                                        'type': -1, 'owner': 'opp', 'timeAlive': 0.4,
                                        'deadlyToOwner': False, 'lifetime': 9.6}]))
    return states


async def run(args):
    rng = random.Random(args.seed)
    rec = None
    if args.record:
        from recorder import Recorder
        os.makedirs(os.path.dirname(os.path.abspath(args.record)) or '.', exist_ok=True)
        rec = Recorder(args.record)

    if args.fake:
        backend = FakeBackend(fake_script(max(6, args.steps + 2)), pid='p1', dt=0.0)
        env = TankTroubleEnv(backend=backend, dt=0.0, max_steps=max(6, args.steps))
    else:
        env = TankTroubleEnv(port=args.port, dt=args.dt, max_steps=args.steps,
                             players=args.players)

    obs = await env.reset()
    total = 0.0
    done_count = 0
    action_counts = {}
    for i in range(args.steps):
        if args.policy == 'noop':
            a = 0
        elif args.policy == 'forward':
            a = 1
        else:
            a = rng.randrange(NUM_ACTIONS)
        out = await env.step(a)
        total += out['reward']
        action_counts[a] = action_counts.get(a, 0) + 1
        if rec is not None and env.last_state is not None:
            inputs = action_to_inputs(a)
            rec.record(env.last_state,
                       action={str(env.pid): {k: inputs[k] for k in
                                              ('fwd', 'back', 'left', 'right', 'fire')}})
        if out['done']:
            done_count += 1
            if args.verbose:
                print('  [step %d] done=%s reward=%.2f events=%s'
                      % (i, out['done'], out['reward'], out['info']['events']), flush=True)
            if env.auto_reset:
                obs = out['obs']
    if rec is not None:
        rec.close()

    summary = {
        'steps': args.steps,
        'policy': args.policy,
        'obs_size': obs['size'],
        'total_reward': round(total, 3),
        'dones': done_count,
        'action_histogram': {ACTION_NAMES[k]: v for k, v in sorted(action_counts.items())},
        'record': args.record,
        'pid': str(env.pid),
    }
    if args.record and os.path.exists(args.record):
        from rl.dataset import dataset_stats, iter_samples
        from rl.obs import obs_size
        summary['dataset'] = dataset_stats(args.record)
        first = next(iter_samples(args.record), None)
        # 数据集样本默认不含栅格（录制里只有墙段、没有 tiles）
        summary['first_sample_ok'] = bool(
            first and len(first['obs']) == obs_size(grid_radius=None))
    await env.close()
    return summary


def main():
    ap = argparse.ArgumentParser(description='RL 契约端到端演示')
    ap.add_argument('port', nargs='?', default='auto')
    ap.add_argument('--fake', action='store_true', help='离线 FakeBackend（不需要浏览器）')
    ap.add_argument('--steps', type=int, default=40)
    ap.add_argument('--policy', choices=['random', 'noop', 'forward'], default='random')
    ap.add_argument('--record', default=None, help='把 state+action 写成录制 JSONL')
    ap.add_argument('--dt', type=float, default=0.05)
    ap.add_argument('--players', type=int, default=2)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--json', action='store_true', help='只输出 JSON 汇总')
    args = ap.parse_args()

    summary = asyncio.run(run(args))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print('[demo] 策略=%s 步数=%d 观测=%d 累计奖励=%.3f 终止次数=%d'
              % (summary['policy'], summary['steps'], summary['obs_size'],
                 summary['total_reward'], summary['dones']), flush=True)
        print('[demo] 动作分布: %s' % summary['action_histogram'], flush=True)
        if summary.get('record'):
            ds = summary.get('dataset', {})
            print('[demo] 录制 %s → 帧=%s 带标签=%s 样本=%s 首样本长度正确=%s'
                  % (summary['record'], ds.get('frames'), ds.get('labeled_frames'),
                     ds.get('samples'), summary.get('first_sample_ok')), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
