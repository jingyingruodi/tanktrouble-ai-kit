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
数据集迭代器（AI-B 上游契约）
=============================
把 `recorder` 产出的 JSONL（含**动作标签**的记录）直接变成训练样本流：

    for s in iter_samples('data/rollout.jsonl', pid='<your-pid>'):
        s['obs']          # 观测向量（定长）
        s['action_index'] # 离散动作索引（0..10），无法归类时 None
        s['bits']         # 原始 5 位掩码（完整动作空间用这个）

要点
----
* 只认带 `"a"` 标签的帧（`cdp_tools/recorder.py` 写入）；没有标签的帧按
  `skip_unlabeled=True`（默认）跳过——老录制（v3 及以前）没有动作，只能做状态分析。
* `obs` 默认**不含栅格**（录制里只有墙段 `walls`、没有 `tiles`，无法还原
  `(2r+1)²` 位图）；需要栅格请自行实时采集（见 `rl.env`）。
* 多智能体：一条记录里每个 pid 都有动作标签 → 用 `pid=None` 会为**每个**有标签的
  pid 各产出一个样本（同一帧的联合动作可自行按 `t` 聚合）。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
if os.path.join(BASE, 'cdp_tools') not in sys.path:
    sys.path.insert(0, os.path.join(BASE, 'cdp_tools'))

from rl.actions import DISCRETE_ACTIONS, inputs_to_bits, bits_to_inputs  # noqa: E402
from rl.obs import build_obs                                             # noqa: E402


def _bits_to_discrete(bits):
    """5 位掩码 → 离散动作索引；不在离散表里返回 None。"""
    for i, (name, fwd, back, left, right, fire) in enumerate(DISCRETE_ACTIONS):
        if inputs_to_bits({'fwd': fwd, 'back': back, 'left': left,
                           'right': right, 'fire': fire}) == bits:
            return i
    return None


def iter_records(path):
    """逐条产出 (record, full_state)；压缩/普通录制都支持。"""
    from recorder import CompressedRecorder
    return CompressedRecorder.iter_expanded(path)


def iter_samples(path, pid=None, include_grid=False, skip_unlabeled=True,
                 only_round_active=True, min_others=0):
    """产出训练样本 dict。

    pid=None  → 为每帧里每个"有动作标签"的 pid 各产出一个样本
    pid='xxx' → 只产出该 pid 的样本
    """
    for rec, full in iter_records(path):
        acts = rec.get('a')
        if not acts:
            if skip_unlabeled:
                continue
            acts = {}
        if only_round_active and not full.get('roundActive'):
            continue
        tanks = full.get('tanks') or {}
        pids = [pid] if pid is not None else list(acts.keys())
        for p in pids:
            act = acts.get(str(p))
            if act is None:
                if skip_unlabeled:
                    continue
                act = {}
            if min_others and len(tanks) - 1 < min_others:
                continue
            obs = build_obs(full, p, terrain=None,
                            grid_radius=(3 if include_grid else None))
            bits = inputs_to_bits(act)
            yield {
                't': rec.get('t'),
                'mv': rec.get('mv', 0),
                'pid': str(p),
                'obs': obs['vector'],
                'action_index': _bits_to_discrete(bits),
                'bits': bits,
                'action': bits_to_inputs(bits),
                'alive': str(p) in tanks,
                'roundActive': bool(full.get('roundActive')),
                'scoresKill': full.get('scoresKill'),
                'scoresVict': full.get('scoresVict'),
            }


def dataset_stats(path, pid=None):
    """快速统计：总帧、带标签帧、每个动作的样本数。"""
    total = labeled = 0
    per_action = {}
    for rec, full in iter_records(path):
        total += 1
        if rec.get('a'):
            labeled += 1
    for s in iter_samples(path, pid=pid):
        per_action[s['action_index']] = per_action.get(s['action_index'], 0) + 1
    return {'frames': total, 'labeled_frames': labeled,
            'samples': sum(per_action.values()), 'per_action': per_action}


if __name__ == '__main__':
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    path = args[0]
    pid = args[args.index('--pid') + 1] if '--pid' in args else None
    stats = dataset_stats(path, pid=pid)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
