"""Opt-in isolated EXE acceptance: legacy ended upgrade, delete, exact blocker UI."""
import hashlib,json,os,sqlite3,sys,time,traceback
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox
from aoxiang_fetch_ui import FetchDialog

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();f=json.loads((out/'fixture.json').read_text('utf-8'))
    if not f.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Explicit synthetic isolation required')
    def execute():
        checks=[];dialog=None;report={'passed':False,'checks':checks,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False}
        def require(ok,text):
            if not ok:raise AssertionError(text)
            checks.append(text)
        def wait(fn):
            until=time.monotonic()+20
            while not fn() and time.monotonic()<until:app.processEvents();QTest.qWait(15)
            if not fn():raise TimeoutError('Isolated GUI did not settle')
        def business():
            with closing(sqlite3.connect(f['business_db'])) as db:return '\n'.join(db.iterdump())
        files=[Path(f['manifest']),*[Path(p) for p in f['command_files']]]
        before=business();hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
        def click_modal(button,response):
            boxes=[];timer=QTimer();timer.setInterval(20)
            def dismiss():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    boxes.append(box.detailedText() or box.text());box.grab().save(str(out/('弹窗-'+str(len(checks))+'.png')))
                    box.done(response);timer.stop()
            timer.timeout.connect(dismiss);timer.start();button.click();wait(lambda:bool(boxes) and dialog.worker is None and dialog.pending_maintenance is None);timer.stop();return boxes
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            dialog=FetchDialog(win,win.repository.folder,win.job_day);dialog.show();wait(lambda:dialog.worker is None)
            require(dialog.current['id']=='blocked' and dialog.current['state']=='ENDED','旧版已结束记录仍可查看，未伪造平台任务成功')
            state=Path(f['state_root']);backups=list((state/'recovery').glob('before-ended-collection-*.sqlite'))
            require(len(backups)==1,'第一次升级前已自动保存可校验的组件库备份')
            with closing(sqlite3.connect(state/'state.sqlite')) as db:
                for a in f['legacy_attempts']:
                    require(db.execute('SELECT 1 FROM retired_collections WHERE run_id=?',(a['runId'],)).fetchone() is not None,'已明确结束的旧采集不再占用门店：'+a['id'])
                    old=json.loads(db.execute('SELECT payload FROM attempts WHERE id=?',(a['id'],)).fetchone()[0])
                    require(old['taskId']==a['taskId'] and old['stage']=='ACCEPTED','原平台任务号及未核实事实完整保留：'+a['id'])
            require(dialog.delete_btn.isEnabled(),'旧已结束记录的删除按钮可用')
            click_modal(dialog.delete_btn,int(QMessageBox.StandardButton.No))
            require(dialog.jobs.get('blocked')['state']=='ENDED','取消删除保留原记录')
            click_modal(dialog.delete_btn,int(QMessageBox.StandardButton.Yes))
            require(all(j['id']!='blocked' for j in dialog.jobs.recent()),'确认后旧采集日志真正删除，不再因平台未完成而拦截')
            from aoxiang_acquisition import prepare
            fresh=prepare(dialog.jobs,dialog.configuration,['W1'],'refresh',day='2026-11-11')
            require(fresh['state']=='PREPARED','释放旧占用后可建立新的本地意向；本验收不派发平台导出')
            for row,check in enumerate(dialog.checks):check.setChecked(dialog.stores.item(row,2).text()=='W1')
            with patch('aoxiang_current_login.current_configuration',return_value=dialog.configuration):
                boxes=click_modal(dialog.start_btn,int(QMessageBox.StandardButton.Ok))
            require(dialog.current['id']==fresh['id'] and dialog.history.currentData()==fresh['id'] and fresh['id'] in boxes[0],'真正未完成任务会定位到准确原记录，并弹出原日期和处理入口')
            require(dialog.selected_codes()==['W1'] and dialog.reexport_btn.isEnabled(),'保留本次门店勾选，可从所选门店重导入口处理真实未完成任务')
            dialog.grab().save(str(out/'定位真实待处理记录.png'))
            require(business()==before and [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]==hashes,'业务订单、草稿、采用清单及未选门店原文件内容不变')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            if dialog:
                dialog.close()
                try:wait(lambda:dialog._can_close and dialog.worker is None)
                except Exception:report['passed']=False;report['close_error']=traceback.format_exc()
                dialog.deleteLater()
            win.job_dirty=False;win._discard_close=True;win.close()
            try:
                wait(lambda:win.aoxiang_workflows.session.client is None and win.aoxiang_workflows.session_closer is None and win.aoxiang_workflows.owner is None)
                report['shared_component_closed']=True
            except Exception:report['passed']=False;report['shutdown_error']=traceback.format_exc()
            (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(300,execute)
