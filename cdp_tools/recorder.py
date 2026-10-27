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
TankTrouble 数据录制器
======================
把游戏状态快照（坦克/弹丸/道具/地雷/比分/回合）记录到 JSONL 文件，
用于回放或 AI 训练数据集。

用法：
  from recorder import Recorder
  rec = Recorder(outfile='data.jsonl')
  await rec.record(state_dict)     # 追加一条快照
  rec.close()

或独立运行：
  python recorder.py 9222 --out data.jsonl --fps 20 --duration 60
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'cdp_tools'))


class Recorder:
    def __init__(self, outfile='tanktrouble_recording.jsonl', append=False):
        self.outfile = outfile
        # 自动创建父目录（干净 clone 里 data/、scripts/scratch/ 不存在）
        parent = os.path.dirname(os.path.abspath(outfile))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 默认覆盖（'w'），避免重录时混入旧帧。
        # 只有明确要延续上一次录制时才用 append=True。
        self.fh = open(outfile, 'a' if append else 'w', encoding='utf-8')
        self.count = 0
        self.start_t = time.time()

    def record(self, state, action=None):
        """追加一条状态快照。`state` 是 dict（monitor STATE_JS 输出）。

        action 可选：本帧下发的动作标签 `{pid: {fwd,back,left,right,fire}}`。
        写入记录顶层字段 `"a"`（**不在** `s` 里），老读取方会忽略它；
        有了它录制才能直接用于模仿学习/离线 RL（否则只有状态没有动作）。
        """
        rec = {
            't': round(time.time() - self.start_t, 3),
            'wall_t': state.get('_time'),
            's': state,
        }
        if action is not None:
            rec['a'] = action
        self.fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self.count += 1

    def close(self):
        self.fh.close()
        print(f'[recorder] wrote {self.count} frames to {self.outfile}', flush=True)


class CompressedRecorder(Recorder):
    """增量压缩录制器：静态/变化缓慢的字段（walls, maze, zones）只在
    【变化时】（如回合切换）写入。每帧只存动态字段（坦克/弹丸/...）
    外加 'mv'（迷宫版本号）标记。

    回放：mv 变化时重放迷宫帧；否则沿用上一份墙体。这能大幅缩小文件
    （迷宫每帧可含 100+ 墙段），也让回合切换在诊断时一眼可辨。"""

    # zones 含 expansionTime/safeSmooth.frac，在线毒圈时**每帧都在变**：
    # 若放 static，md5 每帧变化 → maze 版本每帧 +1 → 每帧重写全部 walls/maze，
    # 压缩完全失效（2026-10-27 审计实测 20 帧=20 次 maze 写）。故 zones 归入动态字段。
    STATIC_KEYS = ('walls', 'maze')

    def __init__(self, outfile='tanktrouble_recording.jsonl', append=False):
        super().__init__(outfile, append)
        self.maze_version = 0          # 静态数据变化时自增
        self.last_static = None        # 上一份静态块的哈希
        self.maze_frame = None         # 写入迷宫时的帧序号

    def record(self, state, action=None):
        # 拆分静态与动态字段
        static = {}
        dynamic = {}
        for k, v in state.items():
            if k in self.STATIC_KEYS and v is not None:
                static[k] = v
            else:
                dynamic[k] = v
        # 哈希静态块以检测变化
        import hashlib
        h = hashlib.md5(json.dumps(static, sort_keys=True).encode()).hexdigest()[:12]
        changed = (h != self.last_static)
        if changed:
            self.maze_version += 1
            self.last_static = h
            self.maze_frame = self.count
        # 写一条紧凑记录
        rec = {
            't': round(time.time() - self.start_t, 3),
            'wall_t': state.get('_time'),
            'mv': self.maze_version,      # 迷宫版本号
            's': dynamic,                  # 仅动态字段
        }
        if changed:
            rec['maze'] = static           # 仅变化时写完整静态数据
        if action is not None:
            rec['a'] = action              # 动作标签（与 Recorder 同契约）
        self.fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        self.count += 1

    @staticmethod
    def iter_expanded(path):
        """遍历压缩录制，跨迷宫版本变化重放静态数据，还原完整状态字典。
        产出 (rec, full_state)。"""
        static_cache = {}
        cur_mv = 0
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                mv = rec.get('mv', 0)
                if mv != cur_mv or 'maze' in rec:
                    cur_mv = mv
                    static_cache.update(rec.get('maze', {}))
                full = dict(static_cache)
                full.update(rec.get('s', {}))
                yield rec, full


async def main():
    args = sys.argv[1:]
    port = 9222
    if len(args) > 0:
        if args[0].isdigit():
            port = int(args[0])
        else:
            # 支持 'auto'：自动发现 dsh-ego-browser 动态 CDP 端口
            try:
                from ego_browser import resolve_port
                port = resolve_port(args[0])
                print(f'[recorder] CDP 端口 {args[0]} -> {port}', flush=True)
            except Exception as e:
                print(f'[recorder] 端口解析失败（{e}），回退 9222', flush=True)
                port = 9222
    out = 'data.jsonl'
    fps = 20
    duration = 0  # 0 = 无限
    if '--out' in args:
        out = args[args.index('--out') + 1]
    if '--fps' in args:
        fps = int(args[args.index('--fps') + 1])
    if '--duration' in args:
        duration = float(args[args.index('--duration') + 1])
    compress = '--compress' in args
    append = '--append' in args

    # 复用 monitor 的 STATE_JS 获取完整快照
    import re
    src = open(os.path.join(os.path.dirname(__file__), '..', 'monitor_server.py'), encoding='utf-8').read()
    m = re.search(r'STATE_JS = r"""(.*?)"""', src, re.S)
    state_js = m.group(1)

    from cdp import CDP
    c = CDP(port=port)
    await c.connect_page('tanktrouble.com', timeout=10)

    rec = (CompressedRecorder(out, append) if compress else Recorder(out, append))
    dur_str = 'infinite' if duration <= 0 else f'{duration:g}s'
    mode = 'COMPRESSED' if compress else 'full'
    print(f'[recorder] recording {fps}fps [{mode}] from :{port} -> {out} ({dur_str})', flush=True)
    try:
        t0 = asyncio.get_event_loop().time()
        while True:
            t = asyncio.get_event_loop().time()
            if duration > 0 and t - t0 > duration:
                break
            try:
                st = await asyncio.wait_for(c.eval(state_js), timeout=2)
                if isinstance(st, dict) and 'err' not in st:
                    rec.record(st)
            except Exception as e:
                print(f'[recorder] eval err: {e}', flush=True)
            await asyncio.sleep(1.0 / fps)
    finally:
        rec.close()


if __name__ == '__main__':
    asyncio.run(main())
