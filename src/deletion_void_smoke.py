"""Isolated Qt acceptance for duplicate previews and verified void associations."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget,QMessageBox
from aoxiang_workflow_ui import Manager,QueueDialog
from aoxiang_workflow_state import deletion_issue,counts


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    if (not json.loads((out/'fixture.json').read_text('utf-8')).get('synthetic')
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings'):
        raise ValueError('Requires isolated synthetic settings')
    app.setQuitOnLastWindowClosed(False)

    def run():
        checks=[];dialogs=[];manager=None;starts=[];records={};beats=[]
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':checks}
        timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        def wait(predicate,seconds=20):
            until=time.monotonic()+seconds
            while not predicate() and time.monotonic()<until:app.processEvents();QTest.qWait(15)
            if not predicate():raise TimeoutError('Isolated condition timed out')
        def confirm(action,answer,name):
            captured=[];answers=QTimer();answers.setInterval(20)
            def respond():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    answers.stop();captured.append(box.text());box.grab().save(str(out/name));box.button(answer).click()
            answers.timeout.connect(respond);answers.start()
            try:action()
            finally:answers.stop()
            assert captured and '不操作平台' in captured[0]
        class Client:
            def __init__(self,*_):self.running=False
            def start(self):self.running=True
            def close(self,timeout=.5):self.running=False;return True
            def request(self,action,**kw):
                if action=='configuration':return {}
                id=kw.get('purchase_id');r=records[id]
                if action=='purchase.status':return copy.deepcopy(r)
                if action!='purchase.start':raise AssertionError(action)
                if not r['uploadAttempted'] or not r.get('taskId'):raise AssertionError('Unexpected upload')
                starts.append(id);r.update(state='BOUND',binding={'orderId':'PO-'+id,'status':'已作废',
                    'method':'known_import_task_and_exact_native_rows'},error=None)
                return copy.deepcopy(r)
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
            win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            host=QWidget(win);host.repository=SimpleNamespace(folder=out/'isolated-queue');host.closing=False
            host.work_repository=SimpleNamespace(path=out/'unused-business.sqlite3')
            manager=Manager(host,client_factory=Client);manager.timer.stop()
            heads=[{'oid':'OID-'+str(n),'day':'2026-11-07','store':'QA-'+str(n),'name':'合成门店'+str(n),
                'warehouse':'河北仓' if n==2 else '山东仓','number':'BH-QA-'+str(n),'revision':'R1','current_revision':'R1',
                'digest':'0'*64,'visibility_token':None} for n in range(6)]
            prepared=manager.store.prepare(heads)
            for n in range(6):
                id='original-'+str(n)
                manager.store.update(prepared['id'],position=n,changes={'state':'PREPARED','package':{'id':id},'summary':{'sku_count':n+100,'amount':'100.00'}})
                records[id]={'id':id,'state':'ACCEPTED','taskId':'TASK-'+id,'uploadAttempted':True,'active':False,'additionalTaskIds':[]}
            prepared=manager.store.update(prepared['id'],state='READY')
            original=manager.store.upload(prepared['id'],list(range(6)),prepared['sequence'])
            for n in range(6):
                manager.store.update(original['id'],position=n,changes={'state':'REVIEW' if n<5 else 'BOUND',
                    'error':{'code':'PURCHASE_CREATED_MISMATCH','message':'旧版待提交状态限制'} if n<5 else None,
                    'result':{} if n<5 else {'binding':{'orderId':'PO-original-5','status':'待提交'}}})
            original=manager.store.update(original['id'],state='COMPLETED_WITH_ERRORS');sixth=copy.deepcopy(original['items'][5])
            duplicate=manager.store.prepare(heads)
            for n in range(6):manager.store.update(duplicate['id'],position=n,changes={'state':'REVIEW','package':{'id':'duplicate-'+str(n)},
                'error':{'code':'PURCHASE_ALREADY_RESERVED','message':'原任务存在'}})
            duplicate=manager.store.update(duplicate['id'],state='COMPLETED_WITH_ERRORS')
            dialog=QueueDialog(host,manager,duplicate['id']);dialogs.append(dialog);dialog.resize(1450,780);dialog.show();app.processEvents()
            assert dialog.end_local_btn.isEnabled()
            assert all(dialog.table.item(n,4).text()=='重复本地预览，可结束' for n in range(6))
            assert all(deletion_issue(original,i) for i in original['items'][:5])
            assert all(deletion_issue(duplicate,i) is None for i in duplicate['items'])
            dialog.grab().save(str(out/'01-重复预览可结束.png'));checks.append('复现六份重复预览：不再误当平台任务，五份原上传待核仍阻止删除')
            dialog.table.item(0,0).setCheckState(Qt.CheckState.Checked)
            confirm(dialog.end_local_btn.click,QMessageBox.StandardButton.No,'02-取消结束本地准备.png')
            assert manager.store.get(duplicate['id'])==duplicate;checks.append('取消结束操作时原任务完整保留')
            confirm(dialog.end_local_btn.click,QMessageBox.StandardButton.Yes,'03-结束所选本地准备.png')
            ended=manager.store.get(duplicate['id']);assert ended['items'][0]['ended_locally']
            assert ended['items'][1:]==duplicate['items'][1:] and not starts
            checks.append('只结束勾选的重复预览，未选任务不变，未调用平台')
            dialog.id=original['id'];dialog.current=None;dialog.refresh()
            assert dialog.retry_btn.isEnabled();dialog.retry_btn.click()
            wait(lambda:manager.worker is None and manager.store.get(original['id'])['state']=='COMPLETED')
            dialog.refresh();after=manager.store.get(original['id'])
            assert starts==['original-'+str(n) for n in range(5)] and after['items'][5]==sixth
            assert all(a['package']==b['package'] for a,b in zip(original['items'],after['items']))
            checks.append('仅继续五笔原受理任务，已关联第六单及采购身份不变，没有新上传')
            assert counts(after)['voided']==5 and counts(after)['success']==1
            assert all(dialog.table.item(n,4).text()=='已关联，平台已作废' for n in range(5))
            assert '上次核验' in dialog.table.item(5,7).text()
            dialog.grab().save(str(out/'04-已作废明确显示.png'));checks.append('已核实作废单独计数，旧关联只表示上次核验，不声称实时有效')
            assert all(deletion_issue(after,i) is None for i in after['items'])
            checks.append('全部已核实后允许返回完整删除范围确认；本验收未删除业务订单')
            dialog.accept();dialogs.remove(dialog);dialog.deleteLater()
            reopened=QueueDialog(host,manager,original['id']);dialogs.append(reopened);reopened.show();app.processEvents()
            assert reopened.table.item(0,6).text()=='PO-original-0' and not reopened.retry_btn.isEnabled()
            checks.append('关闭重开保留采购单号及作废状态，完成项不重复执行')
            report.update(passed=True,original_task_checks=starts,synthetic_uploads=0)
        except Exception:report['error']=traceback.format_exc()
        finally:
            for dialog in dialogs:dialog.close();dialog.deleteLater()
            if manager:
                manager.prepare_close()
                try:wait(lambda:manager.worker is None and manager.session_closer is None);manager.shutdown()
                except Exception:report['passed']=False;report['queue_close_error']=traceback.format_exc()
            win.job_dirty=False;win._discard_close=True;win.close()
            try:wait(lambda:win.aoxiang_workflows.owner is None)
            except Exception:report['passed']=False;report['close_error']=traceback.format_exc()
            timer.stop();report['heartbeat_ticks']=len(beats)
            (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(250,run)
