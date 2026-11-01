"""Explicit read-only live setup probe. Only the authorized TSD20251211 store; no export/create/submit."""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from aoxiang_client import ProcessClient
from aoxiang_directory import read_directory, Directory, file_hash, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--expected-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = {str(p.resolve()): file_hash(p) for p in [args.directory, args.expected_config]}
    data = read_directory(args.directory)
    targets = [dict(code=i['code'], name=i['name']) for i in data['items'] if i['code'] == 'TSD20251211']
    if targets != [{'code': 'TSD20251211', 'name': '泰盛店【搬品用】'}]:
        raise ValueError('授权测试店的编码/名称不符，停止探测')
    Directory(output / 'directory').save(data)
    client = ProcessClient(ROOT / 'components/aoxiang', output / 'component-state')
    observations = []
    try:
        ready = client.start()
        # Seed only the portable, non-secret expectation after state DB initialization.
        destination = client.state_root / 'config/portable.config.json'
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(args.expected_config, destination)
        found = client.request('discover')
        if len(found['profiles']) != 1 or len(found['installations']) != 1:
            raise ValueError('发现多个或零个浏览器/用户配置，需明确选择后再探测，不能猜第一个')
        params = {'candidate': {'edge_executable': found['installations'][0]['executable'],
                                'actual_profile_path': found['profiles'][0]['actualProfilePath']},
                  'targets': targets, 'output_root': str(output / 'downloads')}
        job = client.request('setup.start', **params)
        previous = None
        until = time.monotonic() + 240
        while True:
            status = client.request('setup.status', setup_id=job['id'])
            if (status['state'], status['stage']) != previous:
                print(json.dumps({'state': status['state'], 'stage': status['stage'], 'error': status.get('error')}, ensure_ascii=False), flush=True)
                observations.append(status)
                previous = status['state'], status['stage']
                atomic_json(output / 'observations.json', observations)
            if status['state'] != 'RUNNING':
                break
            if time.monotonic() >= until:
                client.request('setup.cancel', setup_id=job['id'])
                raise TimeoutError('探测达到等待上限，已请求停止，保留原观察记录')
            time.sleep(.5)
        if status['state'] != 'READY':
            raise RuntimeError('真实平台核验未通过；见 observations.json')
        saved = client.request('setup.commit', setup_id=job['id'])
        report = {'passed': saved['state'] == 'SAVED', 'scope': 'read-only setup, no export or purchase creation',
                  'targets': targets, 'directory_rows': len(data['items']), 'saved': saved,
                  'before': before, 'after': {p: file_hash(p) for p in before}}
        if report['before'] != report['after']:
            raise RuntimeError('保护文件发生变化')
        atomic_json(output / 'report.json', report)
        print(json.dumps({'passed': report['passed'], 'identity': saved['identity'], 'targets': saved['targets']}, ensure_ascii=False), flush=True)
    finally:
        # Never terminate a live platform operation. Node has bounded native waits.
        while not client.close(timeout=1):
            print('等待当前只读查询安全结束…', flush=True)
            time.sleep(.5)


if __name__ == '__main__':
    main()
