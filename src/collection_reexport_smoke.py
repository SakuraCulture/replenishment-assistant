"""Actual Qt + bundled component, using only isolated synthetic records and no Edge binding."""
import hashlib,json,os,sqlite3,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    if not fixture.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Requires isolated synthetic settings')
    from aoxiang_fetch_ui import FetchDialog
    import aoxiang_current_login
    original_login=aoxiang_current_login.current_configuration
    # The fixture deliberately has no browser binding. Login behavior is outside this offline acceptance.
    aoxiang_current_login.current_configuration=lambda client,*a,**kw:client.request('configuration')
    def run():
        checks=[];dialog=None;report={'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'passed':False,'checks':checks}
        def wait(check,seconds=15):
            until=time.monotonic()+seconds
            while not check() and time.monotonic()<until:app.processEvents();QTest.qWait(15)
            if not check():raise TimeoutError('Isolated UI condition timed out')
        def business():
            with closing(sqlite3.connect(fixture['business_db'])) as db:return '\n'.join(db.iterdump())
        files=[Path(fixture['manifest']),*[Path(p) for p in fixture['command_files']]]
        before=business();hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        def confirm(button,answer):
            answered=[];timer=QTimer();timer.setInterval(15)
            def respond():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    if box.defaultButton()!=box.button(QMessageBox.StandardButton.No):raise ValueError('Must default to cancel')
                    answered.append(box.text());timer.stop();box.button(QMessageBox.StandardButton.Yes if answer else QMessageBox.StandardButton.No).click()
            timer.timeout.connect(respond);timer.start();button.click()
            wait(lambda:answered and dialog.worker is None and dialog.pending_maintenance is None)
            timer.stop();return answered[0]
        try:
            build=Path(getattr(sys,'_MEIPASS',Path(__file__).parent))/'build_info.json'
            report['source_commit']=json.loads(build.read_text('utf-8'))['source_commit'] if build.exists() else 'source'
            dialog=FetchDialog(win,win.repository.folder,win.job_day);dialog.show();wait(lambda:dialog.worker is None)
            for row,check in enumerate(dialog.checks):check.setChecked(dialog.stores.item(row,2).text()=='W2')
            other=dialog.jobs.get('old-failed')['record']['items'][1]
            dialog.grab().save(str(out/'01-选中门店重导.png'))
            text=confirm(dialog.reexport_btn,False)
            if '重复' not in text or dialog.jobs.get('blocked')['state']!='NEEDS_REVIEW':raise ValueError('Cancelled reexport changed original')
            checks.append('真实重导确认框默认取消，列明所选范围和可能重复文件；取消不退役')
            confirm(dialog.reexport_btn,True)
            wait(lambda:dialog.worker is None and dialog.current['id'].startswith('rep-'))
            ident=dialog.current['id'];wait(lambda:dialog.worker is None and dialog.current['state']=='INTERRUPTED')
            if dialog.current['command']['scope']['warehouse_codes']!=['W2']:raise ValueError('Reexport scope widened')
            if dialog.current['record']['error']['code']!='SETUP_REQUIRED':raise ValueError('Offline fixture tried an unexpected path')
            checks.append('受理未知旧项可明确退役并建立独立新任务；无Edge绑定，在平台动作前停止')
            if dialog.jobs.get('old-failed')['record']['items'][1]!=other:raise ValueError('Unselected store changed')
            checks.append('同批未选门店的身份、位置、状态和错误保持不变')
            dialog.grab().save(str(out/'02-重导新任务与旧记录.png'))
            confirm(dialog.end_btn,True)
            if dialog.current['state']!='ENDED':raise ValueError('Local end did not finish')
            checks.append('新任务可安全结束，不等待平台生命周期或删除旧记录')
            dialog.close();wait(lambda:dialog._can_close and dialog.worker is None);dialog.deleteLater()
            dialog=FetchDialog(win,win.repository.folder,win.job_day);dialog.show();wait(lambda:dialog.worker is None)
            if dialog.jobs.get(ident)['state']!='ENDED' or dialog.jobs.get('old-failed')['record']['items'][0]['state']!='ENDED':raise ValueError('Retirement not durable')
            checks.append('关闭重开保持所选退役与新任务记录，旧成功文件仍可检查采用')
            if business()!=before or hashes!={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}:raise ValueError('Business data or original bytes changed')
            checks.append('草稿/签发逐行逻辑数据、既有采用清单和原件哈希不变')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            if dialog:
                dialog.close()
                try:wait(lambda:dialog._can_close and dialog.worker is None)
                except Exception:report['passed']=False;report['close_error']=traceback.format_exc()
                dialog.deleteLater()
            aoxiang_current_login.current_configuration=original_login
            win.job_dirty=False;win._discard_close=True;win.close()
            try:
                wait(lambda:win.aoxiang_workflows.session.client is None and win.aoxiang_workflows.owner is None)
                report['shared_component_closed']=True
            except Exception:report['passed']=False;report['shutdown_error']=traceback.format_exc()
            (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(200,run)
