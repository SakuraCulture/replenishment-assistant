"""Sample the simultaneous Windows working set of one isolated command tree."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from application_processes import process_snapshot, _open_process
from performance_counters import Counters


def measure(command, output, volume, timeout):
    metric = Counters()
    seen = {}
    ticks = int(time.time() * 1e7) + 116444736000000000
    started = time.perf_counter()
    peak = {'working_bytes': 0}
    minimum_free = shutil.disk_usage(volume).free
    members = set()
    samples = 0
    count = 0
    with output.with_suffix('.log').open('xb') as log, output.with_suffix('.jsonl').open('x', encoding='utf-8') as trace:
        worker = subprocess.Popen(command, stdout=log, stderr=log)
        while worker.poll() is None:
            children = process_snapshot()
            cohort = {worker.pid} | (set(seen) & children.keys())
            while True:
                added = {pid for pid, info in children.items() if info['parent'] in cohort or info['parent'] in seen}
                if added <= cohort:
                    break
                cohort |= added
            processes = []
            for pid in cohort:
                kernel, handle = _open_process(pid)
                if not handle:
                    continue
                try:
                    reading = metric.process(pid, handle)
                finally:
                    kernel.CloseHandle(handle)
                identity = reading.get('identity', 0)
                if identity < ticks or pid in seen and seen[pid] != identity or 'working_bytes' not in reading:
                    continue
                seen[pid] = identity
                processes.append({'pid': pid, 'name': children.get(pid, {}).get('name'), 'working_bytes': reading['working_bytes']})
            current = {'time': time.time(), 'working_bytes': sum(p['working_bytes'] for p in processes),
                       'processes': processes, 'system': metric.system()}
            minimum_free = min(minimum_free, shutil.disk_usage(volume).free)
            if current['working_bytes'] > peak['working_bytes']:
                peak = current
            members.update(p['name'] for p in processes if p['name'])
            count = max(count, len(processes))
            trace.write(json.dumps(current, ensure_ascii=False) + '\n')
            trace.flush()
            samples += 1
            if time.perf_counter() - started > timeout:
                raise TimeoutError(f'Isolated command PID {worker.pid} remains in its original run')
            time.sleep(.15)
    result = {'elapsed_seconds': time.perf_counter() - started, 'peak_tree_MiB': peak['working_bytes'] / 2**20,
              'peak': peak, 'samples': samples, 'names_observed': sorted(members), 'max_simultaneous_processes': count,
              'minimum_disk_free_bytes': minimum_free, 'exit_code': worker.returncode}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--volume', required=True, type=Path)
    parser.add_argument('--timeout', default=480, type=float)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError('A fresh evidence path is required')
    command = args.command[1:] if args.command[0] == '--' else args.command
    result = measure(command, args.out, args.volume, args.timeout)
    print(json.dumps({k:v for k,v in result.items() if k!='peak'}, ensure_ascii=False))
    raise SystemExit(result['exit_code'])
