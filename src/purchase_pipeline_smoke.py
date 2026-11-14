"""Offline Qt acceptance: real local rebuild protocol, synthetic platform pipeline."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'pipeline-fixture.json').read_text('utf-8'))
    if fixture.get('synthetic') is not True or fixture.get('live_platform') is not False or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Requires explicit isolated, offline fixture')

    def run():
        from aoxiang_client import ProcessClient,ComponentError
        from aoxiang_workflow_ui import QueueDialog
        from aoxiang_workflows import Workflows
        from aoxiang_workflow_runner import Runner
        from aoxiang_rebuild import current_heads
        from business_ui import run as background
        checks=[];beats=[];dialog=None;calls=[];manager=win.aoxiang_workflows
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':checks}
        timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        def wait(predicate,seconds=35):
            end=time.monotonic()+seconds
            while not predicate() and time.monotonic()<end:app.processEvents();QTest.qWait(15)
            if not predicate():raise TimeoutError('Isolated UI condition timed out')
        class LocalClient(ProcessClient):
            def request(self,action,**values):
                calls.append(action)
                if action=='setup.check_login':return {'id':'offline-login','state':'VERIFIED'}
                if action=='template.start':return {'id':'offline-template','state':'RUNNING'}
                if action=='template.status':return {'id':'offline-template','state':'READY','receipt':fixture['receipt']}
                if action not in {'configuration','ping','shutdown','session.health','session.release','purchase.list','purchase.status','purchase.reserve','purchase.rebuild','purchase.start'}:
                    raise AssertionError('Offline acceptance forbids platform action: '+action)
                if action=='purchase.start':
                    # Only prove that the superseded original is rejected before
                    # any adapter can execute. No new command may start here.
                    record=super().request('purchase.status',**values)
                    if record['state']!='SUPERSEDED':raise AssertionError('Unexpected browser dispatch')
                return super().request(action,**values)
        try:
            manager.timer.stop();manager.client_factory=LocalClient
            old_package=fixture['package'];head=fixture['head']
            import purchases
            sibling=next(h for h in purchases.targets(win.work_repository.path) if h['warehouse']!=head['warehouse'])
            prep=manager.store.prepare([head,sibling])
            for n,h in enumerate((head,sibling)):
                manager.store.update(prep['id'],position=n,changes={'state':'PREPARED','package':{k:old_package[k] for k in ('id','package_file','package_sha256','plan_file','plan_sha256')},'summary':{'amount':'.98','sku_count':1}})
            prep=manager.store.update(prep['id'],state='READY')
            job=manager.store.upload(prep['id'],[0,1],prep['sequence'])
            manager.store.update(job['id'],position=0,changes={'state':'REVIEW','error':{'code':'SUBMISSION_UNKNOWN','message':'合成无回执'},'purchase_observation':{'uploadAttempted':True,'taskId':None}})
            manager.store.update(job['id'],position=1,changes={'state':'BOUND','result':{'binding':{'orderId':'PO-UNSELECTED'}}})
            job=manager.store.update(job['id'],state='COMPLETED_WITH_ERRORS');untouched=copy.deepcopy(job['items'][1])
            dialog=QueueDialog(win,manager,job['id']);dialog.resize(1400,820);dialog.show();app.processEvents()
            dialog.table.item(0,0).setCheckState(Qt.CheckState.Checked)
            before=manager.store.get(job['id'])
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.No):dialog.rebuild_btn.click()
            assert manager.store.get(job['id'])==before
            checks.append('取消重建确认不改变旧任务、采购记录或未选项')
            answers=QTimer();answers.setInterval(20);confirmed=[]
            def answer():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    answers.stop();confirmed.append(box.text());box.grab().save(str(out/'01-明确重建范围.png'))
                    box.button(QMessageBox.StandardButton.Yes).click()
            answers.timeout.connect(answer);answers.start()
            try:dialog.rebuild_btn.click()
            finally:answers.stop()
            assert confirmed and all(t in confirmed[0] for t in ('旧上传结果可能未核清','重复单','本次只生成预览'))
            new_id=dialog.id;assert new_id!=job['id']
            wait(lambda:manager.worker is None and manager.store.get(new_id)['state']=='READY')
            dialog.refresh();new=manager.store.get(new_id);assert len(new['items'])==1 and new['items'][0]['state']=='PREPARED'
            assert manager.store.get(job['id'])['items'][1]==untouched
            dialog.grab().save(str(out/'02-新预览就绪未上传.png'))
            checks.append('真实Qt按钮重建无回执任务：只处理勾选仓单，未选已成功仓单逐字段保持')
            assert 'purchase.rebuild' in calls and 'purchase.submit' not in calls
            from aoxiang_ui_common import component_root
            client=manager.session.lease()
            try:
                client.start();old=client.request('purchase.status',purchase_id=old_package['id']);fresh=client.request('purchase.status',purchase_id=new['items'][0]['package']['id'])
                assert old['state']=='SUPERSEDED' and old['uploadAttempted'] and fresh['rebuildOf']==old['id'] and not fresh['uploadAttempted']
                try:client.request('purchase.start',purchase_id=old['id'])
                except ComponentError as exc:assert exc.code=='PURCHASE_SUPERSEDED'
                else:raise AssertionError('Old command dispatched again')
                assert client.request('purchase.list',limit=100)['total']==2
            finally:assert client.close(timeout=10)
            again=manager.store.rebuild_selected(job['id'],[head['number']],job['sequence'],current_heads(win.work_repository.path,[job['items'][0]]))
            assert again['id']==new_id
            checks.append('固定Node实际释放旧预约并原子登记后继；旧命令拒绝派发，重试不重复建立新预览')
            assert Path(old_package['package_file']).is_file()
            checks.append('旧冻结文件及上传边界事实保留；新预览仍须单独确认上传')
            # Actual production host scheduler with a closed synthetic platform.
            class Platform:
                def __init__(self):self.rows={};self.events=[];self.inflight=set();self.maximum=0
                def request(self,action,**values):
                    if action=='configuration':return {'purchase_pipeline':True}
                    id=values['purchase_id'];r=self.rows[id]
                    if action=='purchase.status':return copy.deepcopy(r)
                    if action=='purchase.submit':
                        if not r['uploadAttempted']:
                            self.events.append(('submit',id));self.inflight.add(id);self.maximum=max(self.maximum,len(self.inflight));r.update(uploadAttempted=True,taskId='SYNTHETIC-'+id,state='ACCEPTED')
                        return r
                    if action=='purchase.start':
                        time.sleep(.08);self.events.append(('observe',id));r.update(state='BOUND',binding={'orderId':'SYNTHETIC-PO-'+id});self.inflight.remove(id);return r
                    raise AssertionError(action)
            store=Workflows(out/'pipeline-simulation');heads=[]
            for n in range(9):heads.append({**head,'oid':'SYNTHETIC-'+str(n),'store':'SYNTHETIC-'+str(n),'number':'SYNTHETIC-'+str(n)})
            p=store.prepare(heads);platform=Platform()
            for n in range(9):
                pid='SYNTHETIC-'+str(n);store.update(p['id'],position=n,changes={'state':'PREPARED','package':{'id':pid}})
                platform.rows[pid]={'state':'RESERVED','uploadAttempted':False,'active':False,'taskId':None}
            p=store.update(p['id'],state='READY');upload=store.upload(p['id'],list(range(9)),p['sequence'])
            def simulate(progress,cancel):
                return Runner(store,platform,component_root(),out/'unused-synthetic.sqlite3',cancel=cancel,progress=progress,delay=.01).run(upload['id'])
            with patch('aoxiang_upload_pipeline.require_orders'),patch('aoxiang_upload_pipeline.validate_for_upload'),patch('aoxiang_upload_pipeline.Directory'):
                result=background(dialog,simulate,(),'隔离模拟平台等待流水线')
            assert result['state']=='COMPLETED' and platform.maximum==9 and platform.events[:9]==[('submit','SYNTHETIC-'+str(n)) for n in range(9)]
            assert [i['result']['binding']['orderId'] for i in result['items']]==['SYNTHETIC-PO-SYNTHETIC-'+str(n) for n in range(9)]
            assert '无本地数量上限' in dialog.upload_policy.text()
            dialog.grab().save(str(out/'03-无本地并发数量上限.png'))
            checks.append('应用内生产调度：9单全部按选定顺序先提交再核对，无本地在途任务上限，各结果仍归原单')
            assert len(beats)>3;checks.append('重建与等待使用后台Worker，Qt事件循环持续响应')
            report.update(passed=True,component_calls=calls,simulation_events=platform.events,simulation_max_inflight=platform.maximum)
        except Exception:report['error']=traceback.format_exc()
        finally:
            if dialog:dialog.close();dialog.deleteLater()
            manager.prepare_close()
            try:wait(lambda:manager.worker is None and manager.session_closer is None);manager.shutdown()
            except Exception:report['passed']=False;report['close_error']=traceback.format_exc()
            win.job_dirty=False;win._discard_close=True;win.close()
            try:wait(lambda:manager.owner is None)
            except Exception:report['passed']=False;report['window_close_error']=traceback.format_exc()
            timer.stop();report['heartbeat_ticks']=len(beats)
            (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(250,run)
