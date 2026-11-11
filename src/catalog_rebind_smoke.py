"""Actual correction UI + real Node protocol, verified identity fixtures only."""
import copy,hashlib,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtWidgets import QApplication,QMessageBox
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    if not marker['synthetic'] or Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()!=out/'user':raise ValueError('Isolated fixture required')
    app.setQuitOnLastWindowClosed(False)
    def execute():
        checks=[];d=None
        def require(ok,text):
            if not ok:raise AssertionError(text)
            checks.append(text)
        def wait(fn):
            deadline=time.monotonic()+30
            while not fn() and time.monotonic()<deadline:
                loop=QEventLoop();QTimer.singleShot(30,loop.quit);loop.exec()
            require(fn(),'后台连接安全结束')
        def choose(yes):
            timer=QTimer();timer.setInterval(30)
            def answer():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    require('山东仓' in box.text() and '河北仓' in box.text() and '文件' in box.text(),'确认对话框明确两仓和保留文件的范围')
                    box.button(QMessageBox.StandardButton.Yes if yes else QMessageBox.StandardButton.Cancel).click();timer.stop()
            timer.timeout.connect(answer);timer.start();d.rebind_both_btn.click();timer.stop()
        try:
            from catalog_sync_ui import Dialog
            from catalog_sync_client import ComponentError
            win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop()
            d=Dialog(win);d.show();d.manual.show();d.warehouse.setCurrentText('山东仓');app.processEvents()
            before=d.client.request('sync.status');original=copy.deepcopy(before['accounts']);file=Path(before['batches'][0]['items'][0]['native']['file']);digest=hashlib.sha256(file.read_bytes()).hexdigest()
            d.probe=before['operations']['original-sd'];d.mapping.setChecked(True);d.bind_btn.click()
            require(d.client.request('sync.status')['batches']==before['batches'],'原样保存通过且没有改变未完成采集')
            require(d.account_data()['bindingVersion']==1,'原样保存不递增绑定版本')
            before=d.client.request('sync.status');choose(False)
            require(d.client.request('sync.status')==before,'取消纠错不改账号、任务或核验记录')
            choose(True);ended=d.client.request('sync.status')
            require(not ended['accounts'] and all(ended['rebind_required'].values()),'两仓绑定一次解除，等待明确重新核验')
            require(all(i.get('retirement') for i in ended['batches'][0]['items']),'两仓未完成项在本地结束，仍保留历史行')
            require([i['native'] for i in ended['batches'][0]['items']]==[i['native'] for i in before['batches'][0]['items']],'原平台任务与下载回执完整保留')
            require(hashlib.sha256(file.read_bytes()).hexdigest()==digest,'原Excel原路径及字节不变')
            require(all(r[2]=='本地已结束' for r in d.model.rows) and not d.resume_btn.isEnabled(),'窗口明确已结束，不能恢复旧项')
            require(win.input_dirty,'已有输入需重新检查，历史签发不回写')
            d.grab().save(str(out/'retired.png'))
            for command in ('sync.resume','sync.acknowledge_adoption'):
                try:d.client.request(command,batch_id='batch',item_id='item-sd',item_ids=['item-sd'],receipt={});raise AssertionError('Late command accepted')
                except ComponentError as exc:require(exc.code=='ITEM_RETIRED','真实组件拒绝旧命令：'+command)
            try:d.client.request('accounts.ensure',warehouse='山东仓',expected_version=0);raise AssertionError('Old binding reused')
            except ComponentError as exc:require(exc.code=='ACCOUNT_REBIND_REQUIRED','自动连接不能把旧错绑重新建回来')
            owner=win.catalog_sessions.session;d.reject();d.deleteLater();d=None
            wait(lambda:owner.close())
            # Simulate completed read-only login probes while the service is
            # safely closed. No browser/login/platform is invoked by QA.
            jobs=out/'user/jst_catalog_sync/jobs.json';data=json.loads(jobs.read_text('utf-8'))
            for wh,source in [('山东仓',original[1]),('河北仓',original[0])]:
                key='correct-'+source['id'];data['operations'][key]={'id':key,'state':'SUCCEEDED','action':'accounts.probe','warehouse':wh,
                    'bindingEpoch':data['bindingEpochs'][wh],'result':{'binding':source['binding'],'scope':{'company':source['expectedCompany'],'coid':source['expectedCoid'],'uid':source['expectedUid'],'expectedRows':2}}}
            jobs.write_text(json.dumps(data,ensure_ascii=False),'utf-8')
            d=Dialog(win);d.show();d.manual.show()
            for wh,key in [('山东仓','correct-hb'),('河北仓','correct-sd')]:
                d.warehouse.setCurrentText(wh);d.probe=d.state['operations'][key];d.mapping.setChecked(True);d.bind_btn.click()
                require(d.account_data()['bindingVersion']==3,'交换绑定后版本递增且成功保存：'+wh)
            final=d.client.request('sync.status');bywh={a['warehouse']:a for a in final['accounts']}
            require(bywh['山东仓']['expectedCoid']=='3' and bywh['河北仓']['expectedCoid']=='1','两仓账号和浏览器可互换，唯一性保护仍成立')
            require(not final['rebind_required'],'明确保存后解除自动连接等待状态')
            require(hashlib.sha256(file.read_bytes()).hexdigest()==digest,'重新绑定仍不改原件')
            d.grab().save(str(out/'rebound.png'));d.reject();d.deleteLater();d=None;wait(lambda:owner.close())
            report={'passed':True,'checks':checks,'synthetic':True,'identity_probes_simulated':True,'live_platform':False,'frozen':bool(getattr(sys,'frozen',False)),'version':diagnostic()}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen':bool(getattr(sys,'frozen',False))}
        finally:
            if d:d.reject();d.deleteLater()
            win._discard_close=True;win.close()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
            app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(300,execute)
