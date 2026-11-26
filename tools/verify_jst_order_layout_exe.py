"""Exercise the old and new automatic-order dialogs from extracted packages."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from verify_jst_one_click_exe import ROOT, contents, read, sha, write
from verify_jst_withdrawal_exe import seed_orders
from jst_auto_order_store import list_orders


def seed(case, count):
    whs = ('河北仓', '山东仓')
    outcomes = ['unsent'] * count
    if count > 1:
        outcomes[-2:] = ['unknown', 'unknown']
    if count >= 60:
        outcomes[-3] = 'preview_error'
    rows = seed_orders(case, [(outcome, whs[i % 2], 2) for i, outcome in enumerate(outcomes)])
    ordered = list_orders(case/'settings/workbench.sqlite3')
    assert len(ordered)==count
    keys = [r['online'] for r in ordered]
    lookup = {r['online']:rows[r['number']] for r in ordered}
    errors = [key for key in keys if lookup[key]['outcome']=='preview_error']
    ready = [key for key in keys if lookup[key]['outcome']=='unsent']
    fixture = {'synthetic':True,'live_platform':False,'jst_keys':keys,'count':count,
               'errors':errors,'ready':ready,'rows':lookup}
    write(case/'import-failure-fixture.json',fixture)
    write(case/'pending-preview.json',{'ready':ready,'preview':keys,
                                      'file_sha':{k:lookup[k]['file_sha'] for k in keys}})


def run(exe, case, phase):
    env = os.environ.copy()
    for key in ('QT_QPA_PLATFORM','QT_PLUGIN_PATH','PYTHONHOME','PYTHONPATH',
                'QT_FONT_DPI','QT_DEVICE_PIXEL_RATIO','REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(key,None)
    env.update(REPLENISHMENT_DATA_DIR=str(case/'settings'),LOCALAPPDATA=str(case/'local'),
               APPDATA=str(case/'roaming'),PYTHONUTF8='1',QT_SCALE_FACTOR='1',
               QT_SCREEN_SCALE_FACTORS='1.25',QT_SCALE_FACTOR_ROUNDING_POLICY='PassThrough')
    command = [str(exe)] if exe else [sys.executable,str(ROOT/'src/app.py')]
    with (case/'process.log').open('xb') as log:
        process = subprocess.Popen([*command,'--platform-import-failure-smoke',str(case),phase],
                                   cwd=case,env=env,stdout=log,stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        code = process.wait(timeout=155)
    record = case/(phase+'-report.json')
    details = read(record) if record.is_file() else {}
    assert code==0 and details.get('passed') and details['frozen_exe'] is bool(exe), (code,details)
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--counts',nargs='+',type=int,default=[1,27,64])
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    exe, files = None, None
    if args.zip:
        with zipfile.ZipFile(args.zip) as archive:
            assert archive.testzip() is None
            archive.extractall(out/'unpacked')
        app = out/'unpacked'/'补货助手'
        exe = app/'补货助手.exe'
        files = contents(app)
        assert {'补货助手.exe','_internal/build_info.json',
                '_internal/components/jst_catalog/integrity.manifest.json'} <= files.keys()
    phase = 'preview' if args.baseline else 'layout'
    results = {}
    for count in args.counts:
        case = out/f'{count}张'
        seed(case,count)
        result = run(exe,case,phase)
        if exe:
            assert result['build']['build_id']==read(app/'_internal/build_info.json')['build_id']
        results[str(count)] = result
    if exe:
        assert contents(app)==files
    report = out/'report.json'
    write(report,dict(cases=results,zip_sha256=sha(args.zip) if exe else None,exe_sha256=sha(exe) if exe else None))
    print(report)


if __name__=='__main__':
    main()
