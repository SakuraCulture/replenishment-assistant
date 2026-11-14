"""Opt-in frozen-EXE comparison against evidence from the prior frozen source."""
import hashlib,json,sqlite3,sys,traceback
from contextlib import closing
from decimal import Decimal
from pathlib import Path


def typed(value):
    if isinstance(value,dict):return ['dict',[[typed(k),typed(v)] for k,v in value.items()]]
    if isinstance(value,(list,tuple)):return [type(value).__name__,[typed(v) for v in value]]
    if isinstance(value,Decimal):return ['Decimal',str(value)]
    return [type(value).__name__,value]


def collect(path,payload,out):
    import work_repository as wr,order_lifecycle as lifecycle,order_analytics as analytics
    import daily_batch,history_read
    from ui_work_io import Session
    repo=history_read.repository(path);codes=list(payload['results']);values={}
    with Session(path) as session:
        values['preview']=session.call('prepare_normal',[payload,codes],lambda _:None)
        for suffix in ('json','xlsx'):session.call('review_export',str(out/('full.'+suffix)),lambda _:None)
    import openpyxl
    book=openpyxl.load_workbook(out/'full.xlsx',read_only=True)
    values['xlsx']={s:list(book[s].values) for s in book.sheetnames};book.close()
    values['json_bytes']=hashlib.sha256((out/'full.json').read_bytes()).hexdigest()
    values['lifecycle']=[];values['facts']=[]
    with closing(sqlite3.connect(path)) as db:
        for h in repo.headers():
            order=repo.get(h['day'],h['store'],h['revision'])
            for wh in ('河北仓','山东仓'):
                head={**h,'warehouse':wh}
                view=lifecycle.snapshot(path,head,db=db,order=order)
                values['lifecycle'].append(view)
                values['facts'].append(analytics.build_facts(order,head,view,analytics.Evidence(db)))
    request={'from':payload['day'],'to':payload['day'],'all_records':True}
    values['home']=__import__('lifecycle_home').read(path,out/'home.sqlite3',request)
    values['daily']=daily_batch.read(path,out/'daily.sqlite3',{**request,'all_matched':True})
    values['history']=[history_read.cached(path,out/'history.sqlite3',h) for h in repo.headers()]
    return typed(values)


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer
    from version_info import diagnostic
    fixture,out=map(Path,args[:2]);out=out.resolve()
    def run():
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'live_platform':False}
        try:
            assert json.loads((out/'synthetic.json').read_text('utf-8'))['synthetic']
            assert win.work_repository.path.resolve()==(out/'settings/workbench.sqlite3').resolve()
            import work_repository as wr
            actual=collect(str(win.work_repository.path),wr.loads(fixture.read_text('utf-8')),out)
            (out/'actual-typed.json').write_text(json.dumps(actual,ensure_ascii=False),'utf-8')
            expected=json.loads((out/'expected-typed.json').read_text('utf-8'))
            assert actual==expected,'Frozen baseline and EXE ordered typed evidence differs'
            report.update(passed=True,checks=['Actual Session full JSON bytes and all XLSX rows equal frozen baseline',
                'Lifecycle and analytics complete ordered typed values equal frozen baseline',
                'History, tracking and daily queries equal frozen baseline'])
            win.grab().save(str(out/'exe-comparison-completed.png'))
        except Exception:report['error']=traceback.format_exc()
        (out/'ui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        win.job_dirty=False;win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(350,run)
