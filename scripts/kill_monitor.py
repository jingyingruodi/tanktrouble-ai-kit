"""
重启监控室辅助：杀掉残留的 monitor_server / playback_server 进程。
只杀监控相关，保留自杀/录制等其它 python 进程。
"""
import subprocess
import sys


def main():
    killed = []
    try:
        out = subprocess.run(
            ['wmic', 'process', 'where', "name='python.exe'", 'get', 'ProcessId,CommandLine', '/format:csv'],
            capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        print(f'[kill] wmic 失败: {e}', flush=True)
        return 1

    for line in out.splitlines():
        # CSV 格式: Node,CommandLine,ProcessId
        parts = line.strip().split(',')
        if len(parts) < 3:
            continue
        cmd = parts[1] if len(parts) > 1 else ''
        pid = parts[2].strip()
        if not pid.isdigit():
            continue
        if 'monitor_server' in cmd or 'playback_server' in cmd:
            print(f'[kill] 终止 PID {pid}: {cmd.strip()[:80]}', flush=True)
            try:
                subprocess.run(['taskkill', '/pid', pid, '/f'], capture_output=True, timeout=10)
                killed.append(pid)
            except Exception as e:
                print(f'[kill] 终止 {pid} 失败: {e}', flush=True)
    if not killed:
        print('[kill] 无残留监控进程', flush=True)
    else:
        print(f'[kill] 已终止: {killed}', flush=True)
    return 0

if __name__ == '__main__':
    sys.exit(main())
