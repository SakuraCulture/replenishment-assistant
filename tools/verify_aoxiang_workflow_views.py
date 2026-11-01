"""Synthetic source-Qt views and local queue latency. Never starts a real AX client."""
import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    output=parser.parse_args().output.resolve();output.mkdir(parents=True,exist_ok=False)
    from PySide6.QtGui import QFont, QFontDatabase
    from aoxiang_workflow_ui import QueueDialog
    from test_aoxiang_workflow_ui import WorkflowUiTests
    from test_aoxiang_workflows import head
    from aoxiang_workflows import Workflows
    WorkflowUiTests.setUpClass()
    font=Path('C:/Windows/Fonts/msyh.ttc')
    if font.is_file():QFontDatabase.addApplicationFont(str(font));WorkflowUiTests.app.setFont(QFont('Microsoft YaHei',10))
    f=WorkflowUiTests();f.setUp()
    try:
        job=f.m.store.prepare([head(n) for n in (2,1,3)])
        for n in range(3):
            f.m.store.update(job['id'],position=n,changes={'state':'PREPARED' if n<2 else 'REVIEW',
                'package':{'id':'synthetic'+str(n)} if n<2 else None,
                'summary':{'sku_count':120+n,'amount':'1234.56'},
                'error':{'message':'合成样例：付款已变化，请核对原付款及采购记录'} if n==2 else None})
        f.m.store.update(job['id'],state='COMPLETED_WITH_ERRORS',message='合成演示；2单预览完成，1单待核，均未上传')
        d=QueueDialog(f.host,f.m,job['id']);d.resize(1360,760);d.show();f.app.processEvents()
        if not d.grab().save(str(output/'batch-synthetic.png')):raise RuntimeError('screenshot failed')
        d.accept();d.deleteLater()
        selection=f.selection();selection.resize(1180,700);f.app.processEvents()
        if not selection.grab().save(str(output/'receipt-selection-synthetic.png')):raise RuntimeError('screenshot failed')
    finally:f.tearDown()
    with tempfile.TemporaryDirectory() as folder:
        store=Workflows(Path(folder))
        for n in range(1000):
            j=store.prepare([head(n)]);store.update(j['id'],state='COMPLETED')
        samples=[]
        for _ in range(3):
            started=time.perf_counter()
            for i in range(20):
                rows=store.list();due=store.enqueue_due();queued=store.next_queued()
                if len(rows)!=100 or due is not None or queued is not None:raise AssertionError('queue result')
            samples.append((time.perf_counter()-started)/20)
        report={'synthetic':True,'exe':False,'real_platform':False,'completed_one_order_batches':1000,
                'operation':'recent100 + due scan with no enabled plan + first queued lookup',
                'runs_seconds_per_cycle':samples,'median_seconds':statistics.median(samples),'min_seconds':min(samples),'max_seconds':max(samples),
                'cache':'same local machine, warm file cache; 3 serial groups of 20 cycles',
                'limits':'local database responsiveness only; no baseline speedup claim, network, large-SKU or EXE performance claim',
                'screenshots_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob('*.png')}}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
