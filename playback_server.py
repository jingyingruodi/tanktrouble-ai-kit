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
TankTrouble Recording Playback Server (录制回放)
================================================
Reads a recorded JSONL file (from loop_suicide --record or recorder.py)
and replays it over a WebSocket as if it were live monitor state.

The monitor room (tanktrouble_monitor.html) can then render the recording —
useful for verifying the recorder, and for diagnosing past sessions.

Usage:
  python playback_server.py <recording.jsonl> [ws_port] [--fps N] [--loop]
Examples:
  python playback_server.py recordings/demo.jsonl 9314 --fps 20
    -> replay on ws://localhost:9314 (the "replay instance" in the monitor room)
  python playback_server.py data.jsonl 9315 --fps 30 --loop
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'cdp_tools'))
from cdp_tools.recorder import CompressedRecorder

FRAME_KEYS = ('t', 'wall_t')  # recorder wrapper keys; real state lives in 's'


def load_frames(path):
    """Load all frames; returns list of (rec, full_state). Supports both plain
    (Recorder) and compressed (CompressedRecorder) formats."""
    frames = []
    try:
        it = CompressedRecorder.iter_expanded(path)
        for rec, full in it:
            if isinstance(full, dict) and 'tanks' in full:
                frames.append({'t': rec.get('t', 0.0), 's': full})
    except Exception:
        # 回退到普通格式
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                s = rec.get('s', rec)
                if isinstance(s, dict) and 'tanks' in s:
                    frames.append({'t': rec.get('t', 0.0), 's': s})
    return frames


async def serve(frames, port, fps, loop):
    import websockets
    delay = 1.0 / fps

    async def handler(ws):
        print(f'[playback] client connected on :{port}', flush=True)
        try:
            pass_n = 0
            while True:
                # 按帧时间戳计算墙钟节奏
                t0 = frames[0]['t'] if frames else 0.0
                start_wall = asyncio.get_event_loop().time()
                for i, fr in enumerate(frames):
                    # 按录制时间戳控制节奏（为 0/相等时回退到 fps）
                    target_t = fr['t'] - t0
                    now_wall = asyncio.get_event_loop().time() - start_wall
                    if target_t > now_wall:
                        await asyncio.sleep(min(target_t - now_wall, delay * 2))
                    elif target_t > 0 and i > 0 and target_t < now_wall - delay * 4:
                        # 落后太多：快进跳过，但保持间隔合理
                        pass
                    try:
                        await ws.send(json.dumps({'type': 'state', 'data': fr['s']}))
                    except Exception:
                        return
                    await asyncio.sleep(delay * 0.2)  # 每帧之间留小间隔
                pass_n += 1
                print(f'[playback] finished pass {pass_n} ({len(frames)} frames)', flush=True)
                if not loop:
                    # 保持最后一帧，连接保持存活
                    await asyncio.sleep(3600)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            print(f'[playback] err: {e}', flush=True)

    print(f'[playback] serving {len(frames)} frames on ws://localhost:{port} @{fps}fps'
          + (' (loop)' if loop else ''), flush=True)
    async with websockets.serve(handler, 'localhost', port):
        await asyncio.Future()


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print('[playback] ❌ 缺少 websockets：先运行 setup-linux.sh，'
              '或 source .venv/bin/activate', flush=True)
        return 2
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    path = args[0]
    port = int(args[1]) if len(args) > 1 and args[1].isdigit() else 9314
    fps = 20
    loop = False
    if '--fps' in args:
        fps = int(args[args.index('--fps') + 1])
    if '--loop' in args:
        loop = True
    if '--auto-port' in args:
        # 从请求的端口起找一个空闲端口
        import socket
        for p in range(port, port + 20):
            with socket.socket() as s:
                try:
                    s.bind(('127.0.0.1', p))
                    port = p
                    break
                except OSError:
                    continue
        print(f'[playback] auto-selected free port: {port}', flush=True)
    if not os.path.exists(path):
        print(f'[playback] file not found: {path}', flush=True)
        return 1
    frames = load_frames(path)
    if not frames:
        print(f'[playback] no valid frames in {path}', flush=True)
        return 1
    print(f'[playback] loaded {len(frames)} frames from {os.path.basename(path)}', flush=True)
    await serve(frames, port, fps, loop)


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
