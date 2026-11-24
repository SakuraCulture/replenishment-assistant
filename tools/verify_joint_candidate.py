"""Run the packaged Qt acceptance actions and sample the actual process tree."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))

from benchmark_export_exe import marker, working_set_tree
from export_files import sha256
from export_receipts import directory


def set_reuse(case, previous, count):
    origin = previous/'settings/workbench.sqlite3'
    target = case/'settings/workbench.sqlite3'
    selected = sorted(marker(previous/'fixture.json')['heads'], key=lambda h:h['number'])[:count]
    ids = {h['oid'] for h in selected}
    expected = {h['number'] for h in selected}
    reused = []
    with closing(sqlite3.connect(origin.as_uri()+'?mode=ro',uri=True)) as source:
        events = list(source.execute("SELECT at,oid,revision,action,detail FROM normal_events WHERE action='带号导出'"))
    with closing(sqlite3.connect(target)) as db,db:
        for row in events:
            if row[1] not in ids:
                continue
            data = json.loads(row[4]);task = data.get('export_task')
            saved = marker(directory(origin)/(str(task)+'.json'))
            if not saved or not saved.get('reuse_original'):
                continue
            file, = saved['files']
            if (file['state']!='已完成' or file['head']['number'] not in expected
                    or not file.get('output_profile') or sha256(file['path']) != file['sha256']):
                continue
            db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',row)
            receipt = directory(target)/(str(task)+'.json')
            receipt.parent.mkdir(exist_ok=True)
            saved['database'] = str(target.resolve())
            receipt.write_text(json.dumps(saved,ensure_ascii=False,indent=2),'utf-8')
            reused.append(file['head']['number'])
    assert set(reused)==expected, (len(reused),count)
    return reused


def run(exe, case, mode, timeout):
    settings = case/'settings'
    env = {**os.environ, 'REPLENISHMENT_DATA_DIR':str(settings), 'LOCALAPPDATA':str(case/'local'),
           'APPDATA':str(case/'roaming'),'TEMP':str(case/'temp'),'TMP':str(case/'temp'),
           'REPLENISHMENT_IO_TRACE':str(case/'trace'),'PYTHONUTF8':'1'}
    for key in ('PYTHONPATH','PYTHONHOME','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):
        env.pop(key,None)
    (case/'temp').mkdir(exist_ok=True)
    report_case = mode in ('exercise','reopen')
    result_path = case/(mode+'.json' if report_case else 'result.json')
    command = [str(exe), '--joint-reports-smoke' if report_case else '--joint-export-smoke', str(case), mode]
    start_time = time.monotonic()
    peak, at_peak = 0, {}
    with (case/(mode+'-exe.log')).open('wb') as log, (case/(mode+'-working-set.jsonl')).open('w',encoding='utf-8') as samples:
        process = subprocess.Popen(command, cwd=exe.parent, env=env,stdout=log,stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        (case/'pid.txt').write_text(str(process.pid),'ascii')
        start = result = None
        while process.poll() is None:
            start = start or marker(case/'start.json')
            result = result or marker(result_path)
            if start and not result:
                tree = working_set_tree(process.pid)
                sample = sum(tree.values())
                if sample > peak:
                    peak, at_peak = sample, tree
                samples.write(json.dumps({'time':time.time(),'bytes':sample,'processes':tree}) + '\n')
                samples.flush()
            if time.monotonic()-start_time>timeout:
                (case/'still-running.json').write_text(json.dumps({'pid':process.pid,'time':time.time(),'retained':True}), 'utf-8')
                raise TimeoutError('隔离作业尚未安全退出，请查看原任务状态')
            time.sleep(.2)
    result = marker(result_path) or {'time':time.time(),'state':'EXITED','exit_code':process.returncode}
    if report_case:
        print(json.dumps({'mode':mode,'passed':result.get('passed'),'error':result.get('error')},ensure_ascii=False))
        return bool(result.get('passed') and process.returncode==0)
    paths = list((case/'export').rglob('*.xlsx'))
    files = [{'file':str(p.relative_to(case/'export')),'bytes':p.stat().st_size,'sha256':sha256(p)} for p in paths]
    inf = {'mode':mode,'exe':str(exe),'exe_sha256':sha256(exe),'input_sha256':marker(case/'fixture.json')['database_sha256'],
           'build':marker(exe.parent/'版本信息.json'),'start':start,'result':result,
           'seconds':result['time']-start['time'] if start else None,'peak_tree_bytes':peak,'peak_tree_processes':at_peak,
           'files':files,'output_bytes':sum(r['bytes'] for r in files),'exit_code':process.returncode}
    (case/'timing.json').write_text(json.dumps(inf,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({k:inf[k] for k in ('mode','seconds','peak_tree_bytes','output_bytes','result')},ensure_ascii=False))
    return result['state']=='COMPLETE' and process.returncode==0


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--mode',required=True,choices=('exercise','reopen','return','operations','signed'))
    parser.add_argument('--fixture',type=Path)
    parser.add_argument('--reuse-from',type=Path)
    parser.add_argument('--reuse-count',type=int,default=0)
    parser.add_argument('--timeout',type=float,default=1200)
    args=parser.parse_args()
    root=args.out.resolve()
    if args.fixture:
        fixture=args.fixture.resolve();info=marker(fixture/'fixture.json')
        if not info['synthetic'] or info['live_platform']:
            raise ValueError('需要隔离压力样本')
        root.mkdir()
        shutil.copytree(fixture/'settings',root/'settings')
        original=root/'settings/workbench.sqlite3'
        assert sha256(original)==info['database_sha256']
        (root/'export').mkdir()
        (root/'synthetic.json').write_text('{"synthetic":true,"live_platform":false}', 'utf-8')
        (root/'fixture.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),'utf-8')
        (root/'settings/export_settings.json').write_text(json.dumps({'schema':1,'default_export_directory':str(root/'export'),
            'monthly_new_sheet_editable':False},ensure_ascii=False),'utf-8')
        if args.reuse_from:
            reused=set_reuse(root,args.reuse_from.resolve(),args.reuse_count)
            (root/'reuse-input.json').write_text(json.dumps(reused,ensure_ascii=False,indent=2),'utf-8')
    success=run(args.exe.resolve(strict=True),root,args.mode,args.timeout)
    raise SystemExit(0 if success else 1)
