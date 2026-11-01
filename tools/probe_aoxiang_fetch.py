"""Live export for the authorized test store only; persists the exact command before sending."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from aoxiang_client import ProcessClient
from aoxiang_jobs import Jobs
from aoxiang_acquisition import prepare, manifest, freeze_manifest, ACTIVE
from aoxiang_directory import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    client = ProcessClient(ROOT / 'components/aoxiang', args.state_root)
    jobs = Jobs(output / 'jobs.sqlite3')
    started = time.monotonic()
    try:
        client.start()
        config = client.request('configuration')
        target = [i for i in config.get('targets', []) if i['buyerWarehouseCode'] == 'TSD20251211']
        if len(target) != 1 or target[0]['displayName'] != '泰盛店【搬品用】':
            raise ValueError('授权测试店绑定不符，停止')
        job = prepare(jobs, config, ['TSD20251211'], 'reuse', day=datetime.now().date().isoformat())
        atomic_json(output / 'request.json', job['command'])
        job = jobs.dispatch(client, job['id'])
        after, last = 0, None
        until = time.monotonic() + 420
        while True:
            job = jobs.refresh(client, job['id'])
            events = client.request('events', command_id=job['id'], after=after, limit=100)['events']
            for event in events:
                after = event['sequence']
                with (output / 'events.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                stage = event['detail'].get('stage')
                if stage:
                    print(stage, flush=True)
            if job['state'] != last:
                print('state: ' + job['state'], flush=True)
                last = job['state']
            atomic_json(output / 'latest.json', job)
            if job['state'] not in ACTIVE:
                break
            if time.monotonic() >= until:
                jobs.observe(job['id'], client.request('pause', command_id=job['id']))
                raise TimeoutError('已请求暂停，保存原命令；不得另建命令重试未知提交')
            time.sleep(.8)
        if job['state'] != 'SUCCEEDED':
            raise RuntimeError('采集未完成，原命令已留存：' + job['state'])
        value = manifest(job, args.state_root)
        saved = freeze_manifest(value, output)
        report = {'passed': True, 'scope': 'test store native replenishment export only; no procurement',
                  'seconds': time.monotonic() - started, 'manifest': value, 'manifest_path': saved}
        atomic_json(output / 'report.json', report)
        print(json.dumps({'passed': True, 'rows': [i['rows'] for i in value['files']], 'seconds': report['seconds']}, ensure_ascii=False), flush=True)
    finally:
        while not client.close(timeout=1):
            print('等待当前动作安全结束；保留平台已受理任务…', flush=True)
            time.sleep(.5)


if __name__ == '__main__':
    main()
