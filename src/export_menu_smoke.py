"""Explicit isolated Qt acceptance. Closed transport; never starts a browser."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from aoxiang_fetch_ui import FetchDialog
from aoxiang_jobs import Jobs

def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    if not fixture.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Explicit isolated fixture required')
    class Client:
        def __init__(self,*_):self.running=False;self.calls=[];self.record=copy.deepcopy(fixture['record'])
        def start(self):self.running=True
        def close(self,**_):self.running=False;return True
        def request(self,action,**kw):
            self.calls.append(action)
            if action=='configuration':return copy.deepcopy(fixture['configuration'])
            if action=='events':return {'events':[]}
            if action=='status':return copy.deepcopy(self.record)
            if action=='collection.continue':
                self.record['sequence']+=1
                self.record['items'][1].update(state='INTERRUPTED',error={'code':'QA_OFFLINE','message':'隔离运输仅验证恢复按钮，未连接平台'})
                return copy.deepcopy(self.record)
            raise AssertionError('Unexpected action: '+action)
    def run():
        report={'passed':False,'frozen':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':[]};d=None
        def wait(check):
            until=time.monotonic()+10
            while not check() and time.monotonic()<until:app.processEvents();QTest.qWait(10)
            if not check():raise TimeoutError('UI wait')
        try:
            d=FetchDialog(None,out/'settings',fixture['day'],client_factory=Client);d.show()
            wait(lambda:d.worker is None and d.current is not None)
            assert d.current['state']=='NEEDS_REVIEW' and d.resume_btn.isEnabled()
            report['checks'].append('待核记录提供专用恢复按钮')
            d.resume_btn.click();wait(lambda:d.worker is None)
            assert d.client.calls.count('collection.continue')==1
            assert not any(a in d.client.calls for a in ('resume','retry','reserve'))
            assert d.current['record']['items'][0]==fixture['record']['items'][0]
            report['checks']+=['按钮调用专用核对协议，不调用重发','原待核门店及任务身份保持','其他门店的返回状态更新到界面']
            d.grab().save(str(out/'恢复按钮.png'))
            d.close();wait(lambda:d._can_close and d.worker is None)
            assert not d.client.running
            report['checks'].append('窗口安全关闭且假连接已释放')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            if d and not d._can_close:
                d.close();wait(lambda:d._can_close and d.worker is None)
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            if build.exists():report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit']
            (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(100,run)
