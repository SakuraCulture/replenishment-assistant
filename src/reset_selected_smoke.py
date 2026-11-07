"""Actual UI/reset acceptance in a caller-provided, marked isolated directory."""
import hashlib
import json
import os
import sqlite3
import sys
import threading
import traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();verify=len(args)>1 and args[1]=='verify'
    root=Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()
    if root!=out/'settings' or not (out/'synthetic.json').is_file():raise ValueError('Reset smoke requires a marked isolated fixture')
    report_file=out/('restart-report.json' if verify else 'report.json')
    checks=[];report={'checks':checks,'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'no_live_platform':True}
    def write():report_file.write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    def require(value,name):
        if not value:raise AssertionError(name)
        checks.append(name)
    def fail(exc):report['error']=str(exc);report['traceback']=traceback.format_exc();write();win._discard_close=True;win.close()
    def run():
        try:
            from aoxiang_workflow_ui import QueueDialog
            from aoxiang_workflow_runner import Runner
            from unittest.mock import patch
            import application_reset as reset
            import application_reset_ui as reset_ui
            from version_info import VERSION
            report['version']=VERSION
            manager=win.aoxiang_workflows;manager.timer.stop()
            # Other installed versions may be open against the user's own data.
            # This marked fixture uses a different root and still exercises the
            # real per-directory instance lock and real directory rename.
            reset._check_other_processes=lambda:None
            report['older_process_probe']='bypassed only in marked isolated UI acceptance; production keeps the probe'
            # No source or frozen smoke is permitted to start a native client.
            manager.client_factory=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Live client forbidden'))
            if verify:
                with closing(sqlite3.connect(win.work_repository.path)) as db:
                    require(db.execute('SELECT count(*) FROM normal_orders').fetchone()[0]==0,'Restart has no old business orders')
                    require(db.execute('SELECT count(*) FROM stores').fetchone()[0]==0,'Restart has no saved store parameters')
                require(manager.store.list()==[],'Restart has no old purchase batch')
                require(not (root/'analysis/test-raw.bin').exists(),'Restart has no old analysis data')
                require(not (root/'aoxiang/test-connection.json').exists(),'Restart has no old application connection configuration')
                require((root/'初始化完成.json').is_file(),'Restart preserves reset receipt and backup location')
                report['passed']=True;write();win._discard_close=True;win.close();return
            root.joinpath('analysis').mkdir(exist_ok=True);(root/'analysis/test-raw.bin').write_bytes(b'isolated-analysis')
            (root/'aoxiang/test-connection.json').write_text('{"test_only":true}','utf-8')
            with closing(sqlite3.connect(win.work_repository.path)) as db:
                for n in (1,2,3):
                    db.execute('INSERT INTO normal_orders(day,store,kind,oid,number,current) VALUES(?,?,?,?,?,?)',('2099-01-01','RESET'+str(n),'center','reset'+str(n),'RESET-'+str(n),1))
                db.execute('INSERT INTO stores(code,data) VALUES(?,?)',('RESET','{"name":"初始化验收门店"}'));db.commit()
            heads=[{'oid':'reset'+str(n),'day':'2099-01-01','store':'RESET'+str(n),'name':'隔离测试门店'+str(n),
                    'warehouse':'山东仓','number':'RESET-'+str(n),'revision':1,'current_revision':1,'digest':'a'*64,'visibility_token':'visible'} for n in (1,2,3)]
            job=manager.store.prepare(heads);job=manager.store.update(job['id'],state='PAUSED')
            dialog=QueueDialog(win,manager,job['id']);dialog.timer.stop();dialog.resize(1200,770);dialog.show()
            for n in range(3):dialog.table.item(n,0).setCheckState(Qt.CheckState.Unchecked)
            dialog.table.item(1,0).setCheckState(Qt.CheckState.Checked)
            with patch.object(manager,'tick'),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):dialog.selected_btn.click()
            require(manager.store.get(job['id'])['dispatch_keys']==['RESET-2'],'UI dispatches exactly the checked order')
            calls=[]
            class Client:
                def request(self,action,**kw):
                    if action=='configuration':return {}
                    raise AssertionError(action)
            def prepared(r,j,item,*args):
                calls.append(item['key']);r.store.update(j['id'],position=item['position'],changes={'package':{'id':'synthetic-purchase'}})
                return 'PREPARED',{'purchase_id':'synthetic-purchase'}
            with patch.object(Runner,'prepare_item',prepared):
                result=Runner(manager.store,Client(),out,win.work_repository.path,cancel=threading.Event()).run(job['id'])
            require(calls==['RESET-2'],'Runner does not prepare unchecked siblings')
            require([i['state'] for i in result['items']]==['PENDING','PREPARED','PENDING'],'Unchecked siblings remain pending')
            require(result['state']=='PAUSED','Selection completion holds remaining items')
            dialog.refresh();require(dialog.upload_btn.isEnabled(),'Selected successful preview can be confirmed without running siblings')
            dialog.grab().save(str(out/'selected-queue.png'))
            for n in range(3):dialog.table.item(n,0).setCheckState(Qt.CheckState.Unchecked)
            dialog.table.item(0,0).setCheckState(Qt.CheckState.Checked)
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):dialog.delete_local_btn.click()
            require([i['key'] for i in manager.store.get(job['id'])['items']]==['RESET-2','RESET-3'],'UI deletes only the checked local queue record')
            with closing(sqlite3.connect(win.work_repository.path)) as db:
                require(db.execute('SELECT count(*) FROM normal_orders').fetchone()[0]==3,'Queue-record deletion leaves business orders intact')
                db.execute("DELETE FROM normal_orders WHERE oid='reset3'");db.commit()
            manager.order_signature=None;dialog.refresh()
            require([i['key'] for i in manager.store.get(job['id'])['items']]==['RESET-2'],'Refresh prunes deleted-order local remnants')
            with closing(sqlite3.connect(win.work_repository.path)) as db:db.execute("DELETE FROM normal_orders WHERE oid='reset2'");db.commit()
            manager.order_signature=None;dialog.refresh()
            remaining=manager.store.get(job['id'])['items'][0]
            require(remaining['source_deleted'] and remaining['result']['purchase_id']=='synthetic-purchase','Platform identity remains available after local order deletion')
            dialog.accept();dialog.deleteLater()
            def click_confirm(confirm):
                def choose():
                    box=app.activeModalWidget()
                    if not isinstance(box,QMessageBox):raise AssertionError('Expected reset confirmation')
                    button=next(b for b in box.buttons() if b.text()==('备份并全部初始化' if confirm else '取消'))
                    if confirm:box.grab().save(str(out/'reset-confirm.png'))
                    button.click()
                QTimer.singleShot(20,choose)
            click_confirm(False);reset_ui.request_reset(win)
            require(win.isVisible() and not getattr(win,'_factory_reset_request',None),'Cancel reset leaves application and data unchanged')
            original=reset.perform
            def perform(value):
                before={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
                result=original(value);backup=Path(result['backup'])
                after={p.relative_to(backup).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.rglob('*') if p.is_file()}
                require(before==after,'Complete backup has identical file bytes including databases and configuration')
                require(sorted(p.name for p in root.iterdir())==['初始化完成.json'],'All original local data moved out before fresh restart')
                report['backup']=str(backup);report['backup_file_count']=len(before);report['passed']=True;write();return result
            reset.perform=perform
            QMessageBox.information=lambda *a,**k:QMessageBox.StandardButton.Ok
            def warning(*a,**k):report['error']=str(a[2] if len(a)>2 else a);report['passed']=False;write();return QMessageBox.StandardButton.Ok
            QMessageBox.warning=warning
            click_confirm(True);reset_ui.request_reset(win)
            require(bool(getattr(win,'_factory_reset_request',None)),'Confirmed reset closes through normal safe-shutdown path')
            write()
        except Exception as exc:fail(exc)
    QTimer.singleShot(300,run)
