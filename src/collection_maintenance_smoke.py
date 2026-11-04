"""Frozen Qt + bundled Node acceptance using explicitly synthetic, isolated data."""
import hashlib,json,os,sqlite3,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox
from aoxiang_fetch_ui import FetchDialog

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    if not fixture.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Smoke requires explicit isolated synthetic settings')
    def run():
        checks=[];dialog=None;beats=[];timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        report={'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'passed':False,'checks':checks}
        def wait(check,timeout=12):
            until=time.monotonic()+timeout
            while not check() and time.monotonic()<until:app.processEvents();QTest.qWait(15)
            if not check():raise TimeoutError('UI condition timed out')
        def business():
            with closing(sqlite3.connect(fixture['business_db'])) as db:return '\n'.join(db.iterdump())
        files=[Path(fixture['manifest']),*[Path(p) for p in fixture['command_files']]]
        before_files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files};before=business()
        def confirm(button,answer):
            responses=[]
            answer_timer=QTimer();answer_timer.setInterval(20)
            def answer_box():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    if box.defaultButton()!=box.button(QMessageBox.StandardButton.No):raise ValueError('Confirmation must default to cancel')
                    responses.append(box.windowTitle());answer_timer.stop()
                    box.button(QMessageBox.StandardButton.Yes if answer else QMessageBox.StandardButton.No).click()
            answer_timer.timeout.connect(answer_box);answer_timer.start();button.click()
            wait(lambda:bool(responses) and dialog.worker is None and dialog.pending_maintenance is None)
            answer_timer.stop()
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            dialog=FetchDialog(win,win.repository.folder,win.job_day);dialog.show();wait(lambda:dialog.worker is None)
            if dialog.current['id']!='blocked':raise ValueError('Fixture not loaded')
            dialog.grab().save(str(out/'01-结束入口.png'))
            confirm(dialog.end_btn,False)
            if dialog.current['state']!='NEEDS_REVIEW':raise ValueError('Cancel changed record')
            checks.append('结束预览取消不修改记录；真实弹框默认取消')
            confirm(dialog.end_btn,True)
            if dialog.current['state']!='ENDED' or dialog.jobs.get('old-failed')['state']!='ENDED':raise ValueError('Related blockers not ended')
            checks.append('结束当前批次及同门店旧失败记录，底层占用释放')
            dialog.grab().save(str(out/'02-结束后可新建.png'))
            confirm(dialog.delete_btn,False)
            if dialog.history.findData('blocked')<0:raise ValueError('Cancel deleted row')
            confirm(dialog.delete_btn,True)
            if dialog.history.findData('blocked')>=0:raise ValueError('Delete not refreshed')
            checks.append('删除预览取消/确认，列表立即刷新')
            dialog.history.setCurrentIndex(dialog.history.findData('adopted'));wait(lambda:dialog.worker is None and dialog.current['id']=='adopted')
            confirm(dialog.delete_btn,True)
            checks.append('成功采集记录可删除，原采用文件仍在')
            dialog.select_visible();dialog.start_btn.click()
            wait(lambda:dialog.worker is None and dialog.current and dialog.current['id'].startswith('rep-'))
            new_id=dialog.current['id']
            wait(lambda:dialog.worker is None and dialog.current['state']=='INTERRUPTED')
            if dialog.current['record']['error']['code']!='SETUP_REQUIRED':raise ValueError('Unexpected new task outcome')
            checks.append('同门店新建成功并进入调度；离线夹具无浏览器绑定，在连接前按预期停止')
            confirm(dialog.end_btn,True)
            dialog.close();wait(lambda:dialog._can_close and dialog.worker is None)
            dialog=FetchDialog(win,win.repository.folder,win.job_day);dialog.show();wait(lambda:dialog.worker is None)
            if dialog.history.findData('blocked')>=0 or dialog.jobs.get(new_id)['state']!='ENDED':raise ValueError('State not durable')
            checks.append('关闭重开，结束和删除持久保存')
            if before!=business():raise ValueError('Draft/signed business rows changed')
            if before_files!={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}:raise ValueError('Adopted bytes changed')
            checks.append('真实草稿/签发库完整逻辑行、采用清单和原文件哈希不变')
            report['passed']=True
        except Exception:
            report['error']=traceback.format_exc()
        finally:
            if dialog:
                dialog.close()
                try:wait(lambda:dialog._can_close and dialog.worker is None)
                except Exception:report['passed']=False;report['close_error']=traceback.format_exc()
            timer.stop();report['heartbeat_ticks']=len(beats);report['max_heartbeat_gap_seconds']=max((b-a for a,b in zip(beats,beats[1:])),default=0)
            (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            win.job_dirty=False;win.close();app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(200,run)
