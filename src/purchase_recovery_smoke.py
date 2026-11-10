"""Frozen Qt acceptance with synthetic accepted tasks; never connects to a platform."""
import copy,json,os,sys,time,traceback,sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget,QMessageBox
from aoxiang_workflow_ui import Manager,QueueDialog


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    if (not json.loads((out/'fixture.json').read_text('utf-8')).get('synthetic')
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings'):
        raise ValueError('Requires isolated synthetic settings')

    def run():
        checks=[];beats=[];dialogs=[];manager=None
        timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':checks}
        records={};starts=[];polls={}
        def wait(predicate,seconds=20):
            until=time.monotonic()+seconds
            while not predicate() and time.monotonic()<until:app.processEvents();QTest.qWait(15)
            if not predicate():raise TimeoutError('Isolated UI condition timed out')
        def box_action(action,expected,name):
            captured=[];answers=QTimer();answers.setInterval(20)
            def answer():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    answers.stop();captured.append(box.text()+'\n'+box.informativeText())
                    box.grab().save(str(out/name));box.accept()
            answers.timeout.connect(answer);answers.start()
            try:action()
            finally:answers.stop()
            if not captured or not all(s in captured[0] for s in expected):raise AssertionError(captured)
        class Client:
            def __init__(self,*_):self.running=False
            def start(self):self.running=True
            def close(self,timeout=.5):self.running=False;return True
            def request(self,action,**kw):
                if action=='configuration':return {}
                id=kw.get('purchase_id');r=records[id]
                if action=='purchase.status':return copy.deepcopy(r)
                if action!='purchase.start':raise AssertionError(action)
                # This test permits only continuation of already accepted tasks.
                if not r['uploadAttempted'] or not r.get('taskId'):raise AssertionError('Unexpected upload dispatch')
                starts.append(id);polls[id]=polls.get(id,0)+1
                if id=='synthetic-0' and polls[id]==1:
                    r.update(state='ACCEPTED',error={'code':'TASK_PENDING','message':'原导入任务尚未完成'})
                else:r.update(state='BOUND',binding={'orderId':'PO-'+id,'status':'待提交'},error=None)
                return copy.deepcopy(r)
        def batch(ids,states):
            heads=[{'oid':'OID-'+str(n),'day':'2026-11-07','store':'TEST-'+str(n),'name':'合成门店'+str(n),
                    'warehouse':'山东仓','number':'BH-QA-'+str(n),'revision':'R1','current_revision':'R1',
                    'digest':'0'*64,'visibility_token':None} for n in ids]
            with closing(sqlite3.connect(host.work_repository.path)) as db,db:
                db.execute('CREATE TABLE IF NOT EXISTS normal_orders(oid TEXT PRIMARY KEY,day TEXT,store TEXT)')
                for h in heads:db.execute('INSERT OR REPLACE INTO normal_orders VALUES(?,?,?)',(h['oid'],h['day'],h['store']))
            p=manager.store.prepare(heads)
            for pos,n in enumerate(ids):
                id='synthetic-'+str(n)
                manager.store.update(p['id'],position=pos,changes={'state':'PREPARED','package':{'id':id},
                    'summary':{'sku_count':100+n,'amount':'100.00'}})
                records[id]={'id':id,'state':'BOUND' if states[pos]=='BOUND' else 'ACCEPTED','taskId':'TASK-'+id,
                    'uploadAttempted':True,'active':False,'binding':{'orderId':'PO-'+id,'status':'待提交'} if states[pos]=='BOUND' else None,
                    'error':None,'additionalTaskIds':[]}
            p=manager.store.update(p['id'],state='READY');j=manager.store.upload(p['id'],list(range(len(ids))),p['sequence'])
            for pos,state in enumerate(states):
                manager.store.update(j['id'],position=pos,changes={'state':state,
                    'error':{'code':'SUBMISSION_UNKNOWN','message':'旧版本提示：没有唯一受理任务号'} if state=='REVIEW' else None,
                    'result':{'binding':copy.deepcopy(records['synthetic-'+str(ids[pos])]['binding'])} if state=='BOUND' else {}})
            return manager.store.update(j['id'],state='COMPLETED_WITH_ERRORS')
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
            win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            host=QWidget(win);host.repository=SimpleNamespace(folder=out/'isolated-queue');host.closing=False
            host.work_repository=SimpleNamespace(path=out/'unused-business.sqlite3')
            manager=Manager(host,client_factory=Client);manager.timer.stop()
            job=batch(range(6),['REVIEW']*5+['BOUND']);successful=copy.deepcopy(job['items'][5])
            dialog=QueueDialog(host,manager,job['id']);dialogs.append(dialog);dialog.resize(1380,780);dialog.show();app.processEvents()
            assert dialog.retry_btn.isEnabled() and dialog.retry_btn.text()=='继续核对未完成采购'
            assert dialog.table.item(0,4).text()=='采购结果需核对'
            assert '没有唯一受理任务号' not in dialog.table.item(0,7).text()
            dialog.grab().save(str(out/'01-旧待核记录可继续.png'));checks.append('旧版五笔误待核可一键沿原采购任务继续，原因显示处理路径')
            dialog.retry_btn.click();wait(lambda:manager.worker is None and manager.store.get(job['id'])['state']=='COMPLETED')
            dialog.refresh();after=manager.store.get(job['id'])
            assert all(i['state']=='BOUND' for i in after['items']) and after['items'][5]==successful
            assert set(starts)=={'synthetic-'+str(n) for n in range(5)} and polls['synthetic-0']==2
            assert all(a['package']==b['package'] for a,b in zip(job['items'],after['items']))
            dialog.grab().save(str(out/'02-原任务全部核验关联.png'));checks.append('已受理任务先等待再关联，原任务身份不变，已成功第六单不重复执行')
            checks.append('采购创建动作零次；组件边界只接收已有任务的结果核验')
            waiting_job=batch([7],['REVIEW'])
            manager.store.update(waiting_job['id'],position=0,changes={'state':'FAILED',
                'error':{'code':'TASK_PENDING','message':'原任务未完成'},'purchase_observation':{'taskId':'TASK-synthetic-7','uploadAttempted':True}})
            dialog.id=waiting_job['id'];dialog.current=None;dialog.refresh()
            assert dialog.table.item(0,4).text()=='已受理，等待核对'
            assert '已受理待核对1单' in dialog.summary.text() and '失败' not in dialog.jobs.currentText()
            dialog.grab().save(str(out/'03-已受理等待核对.png'));checks.append('平台处理中单独计为已受理待核对，批次标题不显示失败')
            unknown=batch([8],['REVIEW']);records['synthetic-8'].update(taskId=None,state='NEEDS_REVIEW')
            dialog.id=unknown['id'];dialog.current=None;dialog.refresh();before=len(starts)
            dialog.retry_btn.click();wait(lambda:manager.worker is None and manager.store.get(unknown['id'])['state']=='COMPLETED_WITH_ERRORS')
            dialog.refresh();assert len(starts)==before
            dialog.table.selectRow(0)
            box_action(dialog.reason_btn.click,['可能已建单','结束所选任务并重建预览','新预览仍需确认上传'],'04-无回执处理方法.png')
            checks.append('真正没有回执时保持保护，弹框说明可能已建单及核对路径')
            dialog.accept();dialogs.remove(dialog);dialog.deleteLater()
            reopened=QueueDialog(host,manager,job['id']);dialogs.append(reopened);reopened.show();app.processEvents()
            assert all(reopened.table.item(n,4).text()=='已关联采购单' for n in range(6))
            assert not reopened.retry_btn.isEnabled();checks.append('关闭重开保留六笔关联状态，完成项不再提供重试')
            reopened.accept();dialogs.remove(reopened);reopened.deleteLater()
            from retry_profile_smoke import install
            from stock_diagnosis_ui import DiagnosisDialog
            install(win,'B');win.job_day='2026-11-06'
            diagnosis=DiagnosisDialog(win);dialogs.append(diagnosis);diagnosis.show()
            with patch('work_repository.today',return_value='2026-11-07'):
                box_action(diagnosis.calculate,['作业已跨天','2026-11-06','2026-11-07','用此配置新建今日草稿'],'05-跨天试算说明.png')
            assert diagnosis.session is None;checks.append('跨天试算在计算前说明两天日期及继续路径，不生成候选')
            from stock_diagnosis import strict_baseline
            old={'S':{'store_name':'合成门店','rows':[{'E':'QA-001','AS':4}]}}
            new=copy.deepcopy(old);new['S']['rows'][0]['AS']=5
            try:strict_baseline(old,new)
            except ValueError as exc:message=str(exc)
            else:raise AssertionError('Mismatch accepted')
            box_action(lambda:QMessageBox.warning(win,'未完成试算',message),['QA-001','模型补货量','当前结果 4，重算结果 5','重新计算'],'06-具体差异说明.png')
            checks.append('一致性检查仍阻止真实数量差异，说明商品、字段、原值与重算值')
            from job_date_messages import export_previous
            message=export_previous('2026-11-06','2026-11-07',False)
            box_action(lambda:QMessageBox.warning(win,'尚不能签发导出',message),['尚未签发','用此配置新建今日草稿'],'07-旧草稿签发说明.png')
            checks.append('旧日期未签发草稿明确引导今日续作，不要求导出不存在的签发修订')
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
