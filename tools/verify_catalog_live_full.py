"""Opt-in live catalog verification in an isolated QA store; never write user data.

Uses the ordinary host Client/run_sync path. Account bindings are copied read-only;
login sessions stay in their existing dedicated browsers. Resume never creates a
replacement export for a previously accepted/unknown task.
"""
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from catalog_sync_client import Client
from catalog_sync import run_sync
from work_repository import Repository


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--accounts', type=Path, required=True)
    p.add_argument('--warehouses', nargs='+', default=['河北仓', '山东仓'])
    p.add_argument('--purpose', choices=['default_catalog', 'allocation_baseline'], default='default_catalog')
    p.add_argument('--resume', action='store_true')
    a = p.parse_args(); out = a.output.resolve()
    if not out.is_relative_to(ROOT / 'qa'): raise ValueError('Independent qa directory required')
    if out.exists() and not a.resume: raise FileExistsError('Use --resume for this original run')
    out.mkdir(parents=True, exist_ok=True)
    client = Client(out / 'service'); work = Repository(out / 'settings')
    if not a.resume:
        client.start(); client.close()
        source = json.loads(a.accounts.read_text('utf-8'))
        accounts = {wh: source['accounts'][wh] for wh in a.warehouses}
        if any(not x['mappingVerified'] or not x['enabled'] for x in accounts.values()):
            raise ValueError('Warehouse mapping must already be explicitly confirmed')
        jobs = out / 'service/jobs.json'; state = json.loads(jobs.read_text('utf-8'))
        state['accounts'] = accounts; jobs.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
        client = Client(out / 'service')
    started = time.perf_counter(); progress_file = out / 'progress.json'
    def progress(value):
        event = getattr(value, 'business_event', {})
        state = event.get('state', {})
        summary = {'message': str(value), 'elapsed': time.perf_counter()-started,
                   'active': state.get('active'),
                   'items': [{'warehouse': i['account']['warehouse'], 'stage': i['stage'],
                              'task': (i.get('native') or {}).get('nativeTaskRef'),
                              'error': i.get('error'), 'adoption_error': i.get('adoption_error')}
                             for b in state.get('batches', []) for i in b['items']]}
        progress_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    try:
        client.start(); state = client.request('sync.status'); resume = None
        if a.resume:
            pending = [b for b in state['batches'] if b['purpose'] == a.purpose and any(not i['adoption'] for i in b['items'])]
            if len(pending) != 1: raise ValueError('Expected exactly one original unfinished batch')
            b = pending[0]; resume = {'batch_id': b['id'], 'item_ids': [i['id'] for i in b['items'] if not i['adoption']]}
        result = run_sync(client, out / 'settings', work.path, a.warehouses, a.purpose, resume, progress)
        result.update(elapsed_seconds=time.perf_counter()-started,
                      passed=all(i.get('adoption') for i in result['batch']['items']),
                      scope='real platform exports; isolated application data; normal production orchestration')
        report = out / ('result-resume-'+str(time.time_ns())+'.json' if a.resume else 'result.json')
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
        print(json.dumps({'passed': result['passed'], 'adopted': result['adopted'], 'report': str(report)}, ensure_ascii=False), flush=True)
        return 0 if result['passed'] else 1
    finally:
        while not client.close(timeout=1): print('Waiting for safe component shutdown', flush=True)


if __name__ == '__main__': raise SystemExit(main())
