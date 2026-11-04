"""Opt-in source/frozen Qt checks; simulated connection states, no platform access."""
import json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop
from version_info import diagnostic

def start_smoke(app,win,args):
    app.setQuitOnLastWindowClosed(False)
    out=Path(args[0]);checks=[];ticks=[];report={};bar=win.browser_status
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(fn,seconds=10):
        until=time.monotonic()+seconds
        while not fn() and time.monotonic()<until:
            loop=QEventLoop();QTimer.singleShot(20,loop.quit);loop.exec()
        require(fn(),'界面后台观察在限定时间内返回')
    class Simulated:
        running=True
        def __init__(self,data):self.data=data;self.calls=[]
        def request(self,action,**values):
            self.calls.append((action,values));time.sleep(.15)
            if isinstance(self.data,Exception):raise self.data
            return self.data
    def run():
        nonlocal report
        from catalog_session_ui import manager
        owners=[win.aoxiang_workflows.session,manager(win).session]
        originals=[(o.client,getattr(o,'started',None),getattr(o,'ready',None)) for o in owners]
        timer=QTimer();timer.setInterval(20);timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start()
        try:
            win.jst_generator.timer.stop();win.aoxiang_workflows.timer.stop();win.resize(1080,900)
            wait(lambda:not bar.workers)
            require(all(o.client is None for o in owners),'启动状态灯不创建组件或浏览器连接')
            require(all(s=='NOT_CONNECTED' for s in bar.states.values()),'初始三个连接均为灰色未连接')
            for i in range(win.sections.count()):
                win.sections.setCurrentIndex(i);app.processEvents()
                require(bar.isVisible(),'主业务页面底部始终可见：'+str(i))
            win.sections.setCurrentIndex(0)
            owners[0].client=Simulated({'state':'CONNECTED'});owners[0].started=True
            owners[1].client=Simulated({'connections':[{'warehouse':'山东仓','state':'CONNECTED'},{'warehouse':'河北仓','state':'DISCONNECTED'}]});owners[1].ready=True
            bar.refresh();wait(lambda:not bar.workers)
            require(bar.states=={'翱象':'CONNECTED','山东仓':'CONNECTED','河北仓':'DISCONNECTED'},'三灯分别显示绿绿红，异常仓不影响另一仓')
            require(all(w.geometry().right()<=bar.width() for w in bar.labels.values()),'较窄窗口三个名称与状态完整显示')
            win.grab().save(str(out/'connected-and-disconnected.png'))
            owners[0].client.data={'state':'CONNECTING'};bar.refresh();wait(lambda:not bar.workers)
            require(bar.states['翱象']=='CONNECTING','连接期间显示黄色连接中')
            owners[0].client.data=TimeoutError('QA status timeout');bar.refresh();wait(lambda:not bar.workers)
            require(bar.states['翱象']=='UNKNOWN','观察超时显示待确认，无授权弹窗、无业务重试')
            win.grab().save(str(out/'unknown.png'))
            for o in owners:require(all(x[0]=='session.health' for x in o.client.calls),'状态灯仅发送轻量只读观察')
            require(len(ticks)>5,'后台观察期间Qt持续响应')
            report={'passed':True,'checks':checks,'frozen':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'heartbeat_count':len(ticks),
                    'scope':'actual Qt and frozen flag; simulated transport states; no platform/login actions'}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen':bool(getattr(sys,'frozen',False))}
        finally:
            bar.prepare_close();wait(lambda:not bar.workers);timer.stop()
            for o,(client,started,ready) in zip(owners,originals):
                o.client=client
                if started is not None:o.started=started
                if ready is not None:o.ready=ready
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win._discard_close=True;win.close();QTimer.singleShot(250,lambda:app.exit(0 if report.get('passed') else 1))
    QTimer.singleShot(250,run)
