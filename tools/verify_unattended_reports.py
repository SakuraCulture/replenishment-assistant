"""Run report business buttons in delivered EXEs against the same retained inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

from measure_unattended import measure


def run(exe, source, out, mode):
    out = out.resolve()
    sources = source/'settings'
    marker = json.loads((source/'synthetic.json').read_text('utf-8'))
    if marker.get('live_platform') is not False or not (marker.get('synthetic') or marker.get('isolated')):
        raise ValueError('需要已有的隔离报表验收资料')
    out.mkdir(parents=True, exist_ok=False)
    target = out/'settings'
    inputs = ('workbench.sqlite3','analysis/stockout/stockout.sqlite3',
              'analysis/warehouse_stockout/warehouse_stockout.sqlite3', 'analysis/business_daily', 'warehouse_catalogs')
    fingerprints = {}
    for relative in inputs:
        src,dest = sources/relative,target/relative
        if src.is_dir():
            shutil.copytree(src,dest)
        else:
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(src,dest)
            with dest.open('rb') as stream:
                fingerprints[relative] = hashlib.file_digest(stream,'sha256').hexdigest()
    shutil.copy2(source/'synthetic.json',out/'synthetic.json')
    fixture = json.loads((source/'fixture.json').read_text('utf-8'))
    fixture['browser_harness'] = str(Path(__file__).with_name('verify_unattended_browsers.mjs').resolve())
    (out/'fixture.json').write_text(json.dumps(fixture,ensure_ascii=False,indent=2),'utf-8')
    (out/'inputs.json').write_text(json.dumps(fingerprints,indent=2),'utf-8')
    (out/'export').mkdir()
    (out/'temp').mkdir()
    os.environ.update(REPLENISHMENT_DATA_DIR=str(target), LOCALAPPDATA=str(out/'local'),
                      APPDATA=str(out/'roaming'), TEMP=str(out/'temp'), TMP=str(out/'temp'), PYTHONUTF8='1')
    for name in ('PYTHONHOME','PYTHONPATH','QT_QPA_PLATFORM','REPLENISHMENT_PORTABLE_PATHS'):
        os.environ.pop(name,None)
    command = [str(exe.resolve(strict=True)), '--joint-reports-smoke', str(out), mode]
    measurements = measure(command,out/'timing.json',out,480)
    result = json.loads((out/(mode+'.json')).read_text('utf-8'))
    if result['passed'] and not measurements['exit_code'] and mode=='parallel':
        # Real quit/reopen for the already published but unacknowledged workbook.
        shutil.copy2(out/'parallel.json',out/'exercise.json')
        reopened=measure(command[:-1]+['reopen'],out/'reopen-timing.json',out,150)
        receipt=json.loads((out/'reopen.json').read_text('utf-8'))
        if reopened['exit_code'] or not receipt.get('existing_file_retained'):
            raise ValueError('重开后补回执证据未通过')
    print(json.dumps({'passed':result['passed'],'error':result.get('error'),'peak_MiB':measurements['peak_tree_MiB'],
                      'elapsed_seconds':measurements['elapsed_seconds']},ensure_ascii=False))
    return not measurements['exit_code'] and result['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('exe','fixture','out'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--mode',choices=('exercise','parallel'),required=True)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.exe,args.fixture,args.out,args.mode) else 1)
