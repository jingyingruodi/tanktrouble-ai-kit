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
RL 上游契约自检（离线，不连游戏）
=================================
默认跑**纯离线**检查：用已有录制文件里的一帧真实快照验证

  1. `rl.actions`  离散动作/位掩码互转一致性、动作数量
  2. `rl.obs`      观测向量定长、有界、分段布局连续且覆盖全长
  3. `recorder`    动作标签（记录顶层 `"a"`）写入/读回一致（含压缩录制）

用法：
    python scripts/rl_selftest.py [recording.jsonl]
    python scripts/rl_selftest.py --live 51109     # 额外跑一次真实 step（会动游戏！）
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'cdp_tools'))

from rl import (ACTION_NAMES, NUM_ACTIONS, action_to_inputs, bits_to_inputs,  # noqa: E402
                inputs_to_bits, build_obs, obs_size, OBS_VERSION)
from rl import actions as rl_actions  # noqa: E402
from recorder import Recorder, CompressedRecorder  # noqa: E402

FAILURES = []


def check(cond, label, detail=''):
    status = 'ok  ' if cond else 'FAIL'
    print(f'  [{status}] {label}' + (f'  {detail}' if detail else ''))
    if not cond:
        FAILURES.append(label)
    return cond


def load_sample_frame(path):
    """从录制文件里取一帧完整快照（压不压缩都能读）。"""
    if not path or not os.path.exists(path):
        return None
    try:
        gen = CompressedRecorder.iter_expanded(path)
    except Exception:
        return None
    for i, (rec, full) in enumerate(gen):
        if isinstance(full, dict) and full.get('tanks'):
            return full
        if i > 200:
            break
    return None


def test_actions():
    print('== rl.actions ==')
    check(NUM_ACTIONS == len(ACTION_NAMES) == len(rl_actions.DISCRETE_ACTIONS),
          '动作表长度一致', f'{NUM_ACTIONS}')
    bad = []
    for i in range(NUM_ACTIONS):
        d = action_to_inputs(i)
        if inputs_to_bits(d) != (1 if d['fwd'] else 0) | (2 if d['back'] else 0) | \
           (4 if d['left'] else 0) | (8 if d['right'] else 0) | (16 if d['fire'] else 0):
            bad.append(i)
    check(not bad, '离散动作 ↔ 位掩码一致', f'异常索引={bad}')
    seen = set()
    for m in range(32):
        d = bits_to_inputs(m)
        seen.add(inputs_to_bits(d))
    check(seen == set(range(32)), '32 种位掩码全覆盖')
    check(not any(a[1] and a[2] for a in rl_actions.DISCRETE_ACTIONS),
          '离散表无 fwd+back 冲突')
    check(not any(a[3] and a[4] for a in rl_actions.DISCRETE_ACTIONS),
          '离散表无 left+right 冲突')
    try:
        action_to_inputs(NUM_ACTIONS)
        check(False, '越界动作应报错')
    except ValueError:
        check(True, '越界动作报错')


def test_obs(frame):
    print('== rl.obs ==')
    expected = obs_size()
    check(expected == 10 + 5 + 4 + 4 * 7 + 12 * 7 + 6 * 4 + 4 * 4 + 49 * 3,
          'obs_size 分段求和', f'{expected}')
    if not frame:
        print('  [skip] 没有可用的录制样本（传一个 .jsonl 路径即可测真实帧）')
        return
    pid = sorted(frame.get('tanks') or {})[0]
    obs = build_obs(frame, pid, terrain=None)
    check(obs['size'] == len(obs['vector']) == expected, '向量长度 == obs_size()',
          f"{obs['size']}")
    oob = [v for v in obs['vector'] if not (-1.0001 <= v <= 1.0001)]
    check(not oob, '所有分量有界 [-1,1]', f'越界数={len(oob)}')
    check(all(isinstance(v, float) for v in obs['vector']), '分量都是 float')
    # 布局连续性 + 覆盖
    segs = obs['layout']
    order = ['self', 'round', 'score', 'others', 'projectiles', 'collectibles', 'mines', 'grid']
    check(list(segs) == order, '分段顺序固定', str(list(segs)))
    pos = 0
    contig = True
    for name in order:
        a, b = segs[name]
        if a != pos:
            contig = False
        pos = b
    check(contig and pos == expected, '各段连续且无缝覆盖全长', f'end={pos}')
    # 与不传 terrain 的 obs_size 对比
    check(obs_size(grid_radius=None) == expected - 49 * 3, 'grid_radius=None 时去掉栅格长度')
    # 空状态容错：长度正确、有界，且"实体段"全零（self 段的 cos/sin 仍为朝向 0）
    zero = build_obs(None, 'x')
    ent_zero = True
    for name in ('others', 'projectiles', 'collectibles', 'mines', 'grid'):
        a, b = zero['layout'][name]
        if any(zero['vector'][a:b]):
            ent_zero = False
    check(zero['size'] == expected and all(-1 <= v <= 1 for v in zero['vector']),
          '空状态：长度正确且有界')
    check(ent_zero, '空状态：实体段全零（不伪造实体）')
    print(f"  [info] 样本 pid={pid} 场上坦克={obs['meta']['counts']['tanks']} "
          f"弹丸={obs['meta']['counts']['projectiles']} 世界={obs['meta']['world']}")


def test_recorder_actions():
    print('== recorder 动作标签 ==')
    tmp = tempfile.mkdtemp(prefix='tt_rl_selftest_')
    state = {'tanks': {'p1': {'x': 1.0, 'y': 2.0, 'rot': 0.5}},
             'walls': [{'x1': 0, 'y1': 0, 'x2': 1, 'y2': 0}],
             'maze': {'w': 1, 'h': 1}, 'zones': []}
    act = {'p1': {'fwd': True, 'back': False, 'left': False, 'right': True, 'fire': False}}
    for cls, name in ((Recorder, 'plain'), (CompressedRecorder, 'compressed')):
        path = os.path.join(tmp, f'{name}.jsonl')
        rec = cls(path)
        rec.record(state)
        rec.record(state, action=act)
        rec.close()
        lines = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
        check(len(lines) == 2, f'{name}: 写入 2 帧')
        check('a' not in lines[0], f'{name}: 无动作帧不写 "a"')
        check(lines[1].get('a') == act, f'{name}: 动作标签读回一致')
        check('s' in lines[1] and 'a' not in lines[1]['s'],
              f'{name}: 动作在记录顶层，不污染状态 s')
    # 压缩录制 expand 仍可用（向后兼容）
    ok = True
    try:
        n = sum(1 for _ in CompressedRecorder.iter_expanded(os.path.join(tmp, 'compressed.jsonl')))
        ok = (n == 2)
    except Exception as e:
        ok = False
        print('   ', e)
    check(ok, '压缩录制 iter_expanded 向后兼容')


async def live_step(port):
    """真实一次 step（会操作游戏，仅在明确 --live 时调用）。"""
    from rl.env import TankTroubleEnv
    print('== live step ==')
    env = TankTroubleEnv(port=port)
    obs = await env.reset()
    print(f"  [info] obs.size={obs['size']} pid={env.meta['pid']}")
    out = await env.step(1)   # FORWARD
    print(f"  [info] reward={out['reward']:.3f} done={out['done']} "
          f"pos={out['info'].get('pos')}")
    await env.close()
    check(out['obs']['size'] == obs['size'], 'live: 观测长度稳定')


async def test_env_offline():
    """用 FakeBackend 离线验证 step/reset/奖励/done 语义（不需要浏览器）。"""
    print('== rl.env（离线 FakeBackend）==')
    import asyncio as _a
    from rl.env import TankTroubleEnv, FakeBackend, RewardConfig
    from rl.actions import action_to_inputs

    def st(x=0.0, alive=True, kill=0, vict=0, weapon=-1, round_active=True):
        tanks = {'opp': {'x': 50.0, 'y': 0.0, 'rot': 0.0, 'weapon': -1}}
        if alive:
            tanks['p1'] = {'x': x, 'y': 0.0, 'rot': 0.0, 'weapon': weapon}
        return {'tanks': tanks, 'roundActive': round_active,
                'scoresKill': {'p1': kill}, 'scoresVict': {'p1': vict},
                'maze': {'w': 8, 'h': 5}, 'projectiles': [], 'collectibles': [],
                'mines': [], 'walls': []}

    # 生存 → 击杀
    be = FakeBackend([st(0.0), st(1.0), st(2.0, kill=1)], pid='p1')
    env = TankTroubleEnv(backend=be, dt=0.0)
    obs = await env.reset()
    check(obs['size'] == obs_size(), 'reset 返回定长观测', f"{obs['size']}")
    out1 = await env.step(1)
    check(abs(out1['reward'] - 0.01) < 1e-9 and not out1['done'],
          '存活一步：reward=+0.01 done=False', f"{out1['reward']}")
    out2 = await env.step(1)
    check(abs(out2['reward'] - 10.0) < 1e-9 and out2['done'],
          '击杀：reward=+10 done=True', f"{out2['reward']}/{out2['done']}")
    check('kill' in out2['info']['events'], 'info.events 标注 kill')
    name, fwd, back, left, right, fire = rl_actions.DISCRETE_ACTIONS[1]
    check(be.applied and be.applied[0][0] == 'p1' and be.applied[0][1]['fwd'] is True,
          '动作经后端下发（FORWARD → fwd=True）', str(be.applied[0][1]))
    check(not any(be.applied[0][1][k] for k in ('back', 'left', 'right', 'fire')),
          'FORWARD 不同时按下其它键')
    check(be.resets == 1, 'reset 调用后端 ensure_round')

    # 死亡
    be2 = FakeBackend([st(0.0), st(0.0, alive=False)], pid='p1')
    env2 = TankTroubleEnv(backend=be2, dt=0.0)
    await env2.reset()
    out3 = await env2.step(0)
    check(abs(out3['reward'] - (-5.0)) < 1e-9 and out3['done'],
          '死亡：reward=-5 done=True', f"{out3['reward']}/{out3['done']}")
    check('death' in out3['info']['events'], 'info.events 标注 death')

    # max_steps 截断
    be3 = FakeBackend([st(0.0)] * 10, pid='p1')
    env3 = TankTroubleEnv(backend=be3, dt=0.0, max_steps=3)
    await env3.reset()
    outs = [await env3.step(0) for _ in range(3)]
    check(outs[-1]['done'] and outs[-1]['info'].get('truncated'), 'max_steps 截断 → done')

    # 位掩码动作也能用（带 fire）
    be4 = FakeBackend([st(0.0), st(0.5)], pid='p1')
    env4 = TankTroubleEnv(backend=be4, dt=0.0)
    await env4.reset()
    mask = 1 | 16          # fwd + fire
    await env4.step(bits_to_inputs(mask))
    got = be4.applied[0][1]
    check(got['fwd'] and got['fire'] and not got['back'],
          '位掩码动作（fwd+fire）正确下发', str(got))


async def test_dataset_offline():
    """离线验证数据集迭代器：合成带动作标签的录制 → 检查配对与动作归类。"""
    print('== rl.dataset（离线）==')
    from rl.dataset import iter_samples, dataset_stats
    tmp = tempfile.mkdtemp(prefix='tt_rl_ds_')
    path = os.path.join(tmp, 'labeled.jsonl')
    rec = CompressedRecorder(path)
    base = {'tanks': {'p1': {'x': 0.0, 'y': 0.0, 'rot': 0.0, 'weapon': -1},
                      'p2': {'x': 40.0, 'y': 0.0, 'rot': 1.0, 'weapon': -1}},
            'walls': [], 'maze': {'w': 8, 'h': 5}, 'zones': [],
            'roundActive': True, 'scoresKill': {'p1': 0}, 'scoresVict': {'p1': 0},
            'projectiles': [], 'collectibles': [], 'mines': []}
    cases = [
        {'fwd': False, 'back': False, 'left': False, 'right': False, 'fire': False},   # NOOP=0
        {'fwd': True, 'back': False, 'left': False, 'right': False, 'fire': False},    # FORWARD=1
        {'fwd': True, 'back': False, 'left': False, 'right': False, 'fire': True},     # FORWARD_FIRE=8
        {'fwd': True, 'back': True, 'left': False, 'right': False, 'fire': False},     # 非离散组合
        {'fwd': False, 'back': False, 'left': False, 'right': False, 'fire': True},    # FIRE=7
    ]
    for c in cases:
        rec.record(base, action={'p1': c})
    rec.record(base)          # 无标签帧（应被跳过）
    rec.record({**base, 'roundActive': False}, action={'p1': cases[0]})  # 非对局帧（应被跳过）
    rec.close()

    samples = list(iter_samples(path, pid='p1'))
    check(len(samples) == 5, '只产出"对局中且有动作标签"的样本', f'{len(samples)}')
    idxs = [s['action_index'] for s in samples]
    check(idxs[0] == 0 and idxs[1] == 1 and idxs[2] == 8 and idxs[4] == 7,
          '离散动作归类正确', str(idxs))
    check(idxs[3] is None, '非离散组合（fwd+back）归类为 None（仍保留 bits）', str(idxs[3]))
    check(samples[3]['bits'] == 3, 'bits 保留原始位掩码', str(samples[3]['bits']))
    check(all(len(s['obs']) == obs_size(grid_radius=None) for s in samples),
          '样本观测长度 == obs_size(no grid)')
    check(samples[0]['pid'] == 'p1' and samples[0]['alive'] is True, '样本带 pid/存活标记')

    st = dataset_stats(path, pid='p1')
    check(st['frames'] == 7 and st['labeled_frames'] == 6, '统计口径：帧数/带标签帧数', str(st))

    # 多智能体视角：同一帧两个 pid 各一条
    path2 = os.path.join(tmp, 'multi.jsonl')
    rec2 = CompressedRecorder(path2)
    for _ in range(3):
        rec2.record(base, action={'p1': cases[0], 'p2': cases[1]})
    rec2.close()
    alls = list(iter_samples(path2, pid=None))
    check(len(alls) == 6 and {s['pid'] for s in alls} == {'p1', 'p2'},
          'pid=None 时为每个有标签的 pid 各产出一条样本', f'{len(alls)}')


def main():
    args = sys.argv[1:]
    live_port = None
    if '--live' in args:
        i = args.index('--live')
        if i + 1 < len(args):
            live_port = args[i + 1]
            args = args[:i] + args[i + 2:]
    path = next((a for a in args if a.endswith('.jsonl')), None)
    if path is None:
        for cand in ('scripts/fire_strict_v3.jsonl', 'scripts/fire_strict_v4.jsonl',
                     'data/fire_strict.jsonl'):
            p = os.path.join(BASE, cand)
            if os.path.exists(p):
                path = p
                break
    print(f'[selftest] 样本录制: {path or "(无)"}')
    frame = load_sample_frame(path)
    test_actions()
    test_obs(frame)
    test_recorder_actions()
    import asyncio
    asyncio.run(test_env_offline())
    asyncio.run(test_dataset_offline())
    if live_port:
        asyncio.run(live_step(live_port))
    print()
    if FAILURES:
        print(f'[selftest] ❌ {len(FAILURES)} 项失败: {FAILURES}')
        return 1
    print('[selftest] ✅ 全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
