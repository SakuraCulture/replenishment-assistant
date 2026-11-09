"""Opt-in actual EXE acceptance; synthetic fixture, closed transport, no browser."""
import json,os,sys,threading,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QCheckBox,QPushButton,QMessageBox,QDialog
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    if not fixture.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Requires isolated fixture')
    app.setQuitOnLastWindowClosed(False)
    def execute():
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':[]};checks=report['checks']
        release=threading.Event();entered=threading.Event();errors=[];messages=[];thread=None;client=None
        controls=QTimer(win);controls.setInterval(20);choice=['cancel']
        def wait(predicate):
            end=time.monotonic()+30
            while not predicate() and time.monotonic()<end:app.processEvents();QTest.qWait(10)
            if not predicate():raise TimeoutError('UI/worker wait')
        def confirm():
            for d in QApplication.topLevelWidgets():
                if not d.isVisible():continue
                if isinstance(d,QMessageBox):messages.append((d.windowTitle(),d.text()));d.accept()
                elif isinstance(d,QDialog) and d.windowTitle()=='彻底删除订单及关联数据':
                    if choice[0]=='cancel':d.reject();continue
                    boxes=d.findChildren(QCheckBox);assert len(boxes)==1;boxes[0].setChecked(True)
                    next(b for b in d.findChildren(QPushButton) if b.text()=='永久删除上述订单').click()
        try:
            import data_maintenance_ui as ui,normal_orders as no,purchases,deletion_barrier
            from aoxiang_workflow_runner import Runner
            from aoxiang_client import ProcessClient,ComponentError
            from aoxiang_ui_common import component_root
            from aoxiang_workflow_ui import QueueDialog
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
            manager=win.aoxiang_workflows;manager.stopping=True;manager.timer.stop()
            heads=purchases.targets(win.work_repository.path);gone=next(h for h in heads if h['oid']=='oid');kept=next(h for h in heads if h['oid']=='other')
            before=no.Repository(win.work_repository).get(kept['day'],kept['store'])
            job=manager.store.prepare([gone,kept]);calls=[]
            class ClosedClient:
                def request(self,action,**kw):
                    assert action=='configuration';return {}
            runner=Runner(manager.store,ClosedClient(),out,win.work_repository.path,cancel=threading.Event())
            def prepare(job,item,*_):
                oid=item['head']['oid'];calls.append(oid)
                if oid=='oid':entered.set();assert release.wait(45)
                return 'PREPARED',{'proof':oid}
            runner.prepare_item=prepare
            def run():
                try:runner.run(job['id'])
                except Exception:errors.append(traceback.format_exc())
            # Suppliers are a local mapping; no platform transport is provided.
            from aoxiang_directory import Directory
            Directory(manager.store.folder).save_suppliers({'山东仓':'ZXC2025','河北仓':'2078246'})
            thread=threading.Thread(target=run);thread.start();wait(entered.is_set)
            win.followup.checked={('oid',gone['warehouse']):gone,('other',kept['warehouse']):kept}
            controls.timeout.connect(confirm);controls.start()
            ui.remove(win,win,[gone]);assert no.Repository(win.work_repository).get(gone['day'],gone['store']) is not None
            checks.append('取消确认不删除任何订单或任务')
            choice[0]='delete';ui.remove(win,win,[gone])
            assert no.Repository(win.work_repository).get(gone['day'],gone['store']) is None
            assert no.Repository(win.work_repository).get(kept['day'],kept['store'])==before
            assert set(h['oid'] for h in win.followup.checked.values())=={'other'}
            checks+=['运行中订单单次确认即可删除，无平台状态门禁','同店两仓完整删除，未选订单快照和勾选保留']
            release.set();wait(lambda:not thread.is_alive());assert not errors,errors
            current=manager.store.get(job['id']);assert calls==['oid','other'] and len(current['items'])==1
            assert current['items'][0]['result']=={'proof':'other'}
            checks.append('晚到结果不复活或错写，剩余订单继续完成')
            d=QueueDialog(win,manager,job['id']);d.timer.stop();d.show();app.processEvents();d.grab().save(str(out/'剩余批次.png'));d.close();d.deleteLater()
            assert len(deletion_barrier.pending(win.work_repository.path))==1
            client=ProcessClient(component_root(),out/'settings/aoxiang/component-state');client.start()
            assert client.request('purchase.list')['total']==0
            assert deletion_barrier.pending(win.work_repository.path)==[]
            try:client.request('orders.retire',operation_id='f'*32,oids=['other']);raise AssertionError('live order retirement accepted')
            except ComponentError as e:assert e.code=='RETIREMENT_NOT_CONFIRMED'
            assert client.close(timeout=20);client=None
            checks+=['实际随包组件纯本地处理离线清理待办并确认','无已提交删除依据时不能退役未选订单']
            assert not any(title in ('未完成删除','订单已删除，页面未完成刷新') for title,_ in messages),messages
            report['messages']=messages;report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            release.set()
            if thread:thread.join(10)
            controls.stop()
            if client:client.close(timeout=20)
            win.job_dirty=False;win._discard_close=True;win.close()
            try:wait(lambda:win.aoxiang_workflows.owner is None and win.aoxiang_workflows.session.client is None);report['shutdown_complete']=True
            except Exception:report['passed']=False;report['shutdown_error']=traceback.format_exc()
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            if build.exists():report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit']
            (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(100,execute)
