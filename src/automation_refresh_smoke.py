"""Actual bridge CLI + MCP read-only schedule/parameter checks, synthetic only."""
import copy
import json
import subprocess
from pathlib import Path
from automation_ui import bridge_command


def exercise(win, client, out, wait, require):
    from scheduled_replenishment import Repository
    from version_info import VERSION, BUILD
    manager = getattr(win, 'scheduled', None)
    if manager: manager.timer.stop()
    repo = Repository(win.work_repository.path)
    schedule = repo.save_schedule(dict(name='隔离查询验收', plan_id='synthetic-only', kind='once', date='2099-01-01', time='06:00', enabled=False))
    run = repo.manual(schedule)
    repo.change(run['id'], state='REVIEW', stores=[dict(code=c, name=c, state='READY' if c=='B' else 'FAILED', message='合成验收') for c in ('B', 'A')], message='合成任务，请勿执行')
    saved = win.current_parameter_record('A'); saved = copy.deepcopy(saved)
    saved['preset']['config']['warehouses']['山东仓'] = [9, 2]
    win.work_repository.save_stores({'A': saved})
    before = win.work_repository.path.read_bytes(); before_ui = copy.deepcopy(win.snapshot())
    command, arg = bridge_command()
    results = []
    def cli(*args, expected=0):
        index = len(results)
        with (out/f'cli-{index}.json').open('wb') as stdout, (out/f'cli-{index}.log').open('wb') as stderr:
            p = subprocess.Popen([command, *arg, '--connection', str(win.automation.connection_file), *args], stdout=stdout, stderr=stderr,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try: wait(lambda: p.poll() is not None, 45)
            finally:
                if p.poll() is None: p.terminate(); p.wait(10)
        result = json.loads((out/f'cli-{index}.json').read_text('utf-8'))
        require(p.returncode == expected, str(result)); results.append(result); return result
    require(cli('version')['result']['version'] == VERSION, 'CLI version mismatch')
    doctor = cli('doctor')['result']
    require(doctor['versions_match'] and doctor['bridge']['source_commit'] == BUILD['source_commit'], 'Build identity mismatch')
    request = out/'cli-parameters.json'; request.write_text(json.dumps({'stores':['A'], 'scope':'defaults'}), 'utf-8')
    defaults = cli('call', 'effective_parameters', '--arguments-file', str(request))['result']
    current = client.call('effective_parameters', {'stores':['A']})
    require(defaults['stores'][0]['parameter_signature'] != current['stores'][0]['parameter_signature'], 'Scopes mixed')
    error = cli('call', 'start_schedule', expected=2)
    require(error['error']['code'] == 'NOT_AVAILABLE', 'Scheduler write exposed')
    schedules = client.call('find_resources', {'kind':'schedules','limit':1})
    runs = client.call('find_resources', {'kind':'scheduled_runs','limit':1})
    require(schedules['items'][0]['id'] == schedule['id'] and runs['items'][0]['id'] == run['id'], 'Wrong run')
    first = client.call('scheduled_run_status', {'run_id':run['id'], 'limit':1})
    second = client.call('scheduled_run_status', {'run_id':run['id'], 'limit':1,'offset':first['stores']['next_offset'],'after':first['next_after']})
    require([first['stores']['items'][0]['code'], second['stores']['items'][0]['code']] == ['B','A'], 'Store order changed')
    require(win.work_repository.path.read_bytes() == before and win.snapshot() == before_ui, 'Read-only queries changed business data')
    report = {'passed':True,'cli':results,'scheduled_first':first,'scheduled_second':second,
              'checks':['CLI version/build and live doctor','CLI defaults vs MCP current parameters','scheduler write rejected',
                        'schedule/run/store pagination and incremental events','queries leave DB bytes and current job unchanged']}
    (out/'refresh-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    return report
