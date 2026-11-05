"""Actual packaged Qt export in a new synthetic environment; no user settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import openpyxl

ROOT=Path(__file__).resolve().parents[1]


def require(ok,message):
    if not ok:raise AssertionError(message)


def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();exe=a.exe.resolve();out=a.output.resolve()
    require(not out.exists(),'Use a new QA output directory')
    info=json.loads((exe.parent/'_internal/build_info.json').read_text('utf-8'))
    require(info['version']=='0.40.1','Wrong build')
    subprocess.run([sys.executable,str(ROOT/'tools/verify_analytics_0390.py'),'--fixture-only','--output',str(out)],cwd=ROOT,check=True)
    with (out/'process.log').open('w',encoding='utf-8') as log:
        run=subprocess.run([str(exe),'--analytics-smoke',str(out)],cwd=ROOT,
            env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'QT_QPA_PLATFORM':'windows'},
            stdout=log,stderr=log,timeout=300)
    report=json.loads((out/'exe-report.json').read_text('utf-8'))
    require(run.returncode==0 and report['passed'] and report['frozen_exe'],'Packaged UI failed')
    book=openpyxl.load_workbook(out/'actual-exe-report.xlsx')
    try:
        names=['漏斗汇总','必采漏斗','非必采漏斗','属性待核漏斗','门店数量记录','门店货值记录']
        require(book.sheetnames[:6]==names,'New monitor sheets missing in EXE')
        require(book['门店货值记录'].max_row>4,'No per-order records')
        for name in names:
            require(book[name].freeze_panes=='B5','Frozen headers missing')
            require(book[name]['A1'].fill.fgColor.rgb=='00142B40','Wrong template palette')
            for row in book[name]:
                require(all(c.data_type not in ('f','e') for c in row),'Unexpected formulas or errors')
        export={'sheets':book.sheetnames,'order_value_rows':book['门店货值记录'].max_row-4,'template_checks_passed':True}
    finally:book.close()
    evidence={'passed':True,'build':info,'exe_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
              'ui':report,'export':export,'limitation':'实际EXE内自动Qt操作，合成12店/每店500商品；未连真实AI客户端、未跑真实平台或手动Excel编辑重算。'}
    (out/'report.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(evidence,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
