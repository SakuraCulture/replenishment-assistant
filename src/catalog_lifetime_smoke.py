"""Opt-in offline acceptance of real JST ownership, exit and re-open."""
import hashlib,json,os,sys,time,traceback
from pathlib import Path


def fixture(args):
    out=Path(args[0]).resolve();value=json.loads((out/'fixture.json').read_text('utf-8'))
    if value.get('synthetic') is not True or value.get('live_platform') is not False or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Requires explicit isolated offline fixture')
    return out,value


def parent_helper(args):
    out,value=fixture(args)
    from catalog_sync_client import Client
    from priority_list import atomic_json
    client=Client(out/'settings/jst_catalog_sync');client.start()
    if client.request('sync.status')['batches'][0]['id']!=value['original_id']:raise ValueError('Original synthetic task lost')
    atomic_json(out/'parent-started.json',{'parent_pid':os.getpid(),'child_pid':client.process.pid,'frozen':bool(getattr(sys,'frozen',False))})
    # Explicitly simulated desktop crash, while the service is idle. No browser
    # action is started and no business submit is terminated.
    os._exit(23)


def start_smoke(app,win,args):
    out,value=fixture(args);app.setQuitOnLastWindowClosed(False)
    from PySide6.QtCore import QTimer,QEventLoop
    from PySide6.QtWidgets import QWidget,QMessageBox,QApplication
    from types import SimpleNamespace
    from catalog_sync_ui import Dialog,open_sync
    from catalog_sync_client import Client
    from catalog_session_ui import manager
    from version_info import diagnostic
    def execute():
        checks=[];ticks=[];dialog=None;ghost=None;owner=None;component=None
        report={'passed':False,'frozen':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':checks,'version':diagnostic()}
        timer=QTimer();timer.setInterval(25);timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start()
        def wait(test,seconds=25):
            end=time.monotonic()+seconds
            while not test() and time.monotonic()<end:
                loop=QEventLoop();QTimer.singleShot(20,loop.quit);loop.exec()
            if not test():raise TimeoutError('Offline lifetime condition did not finish safely')
        def unchanged():return hashlib.sha256((out/'settings/jst_catalog_sync/jobs.json').read_bytes()).hexdigest()==value['jobs_sha256']
        try:
            win.auto_check.stop();win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop()
            dialog=Dialog(win);dialog.show();owner=manager(win).session;component=owner.client;pid=component.process.pid
            assert dialog.records.count()==1 and dialog.state['batches'][0]['id']==value['original_id'] and unchanged()
            dialog.grab().save(str(out/'01-异常退出后原任务保留.png'))
            checks.append('异常退出的主进程由外部工具确认后，实际资料页重开成功，原采集任务及文件摘要保持')
            dialog.reject();assert owner.lease is None and component.running;dialog.deleteLater();dialog=None
            dialog=Dialog(win);dialog.show();assert owner.client.process.pid==pid and unchanged()
            checks.append('关闭并重开资料窗口复用同一个健康采集进程，不重复启动服务')
            # A second independent owner exercises the production entry's error
            # dialog, exactly as an already-owned state directory would do.
            ghost=QWidget(win);ghost.repository=SimpleNamespace(folder=win.repository.folder);ghost.work_repository=win.work_repository;ghost.closing=False;ghost.worker=None
            capture=[];answer=QTimer();answer.setInterval(20)
            def accept():
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    answer.stop();capture.append(box.text());box.grab().save(str(out/'02-占用者与恢复提示.png'));box.accept()
            answer.timeout.connect(accept);answer.start()
            try:open_sync(ghost)
            finally:answer.stop()
            assert capture and all(s in capture[0] for s in ('聚水潭原采集服务',str(pid),'任务和已下载文件已保留'))
            assert component.running and unchanged() and ghost.catalog_sessions.session.lease is None
            checks.append('真实第二服务占用错误显示中文原因与持锁进程号，失败窗口释放租用，不动原任务')
            assert owner.client.request('sync.status')['batches'][0]['items'][0]['native']['nativeTaskRef']=='SYNTHETIC-EXACT-RECEIPT'
            dialog.reject();dialog.deleteLater();dialog=None;ghost.deleteLater();ghost=None
            win._discard_close=True;win.job_dirty=False;win.close();wait(lambda:owner.client is None and not component.running)
            checks.append('正常退出主软件后真实Node进程退出、本地任务锁释放，无需重启Windows')
            reopened=Client(out/'settings/jst_catalog_sync')
            try:
                reopened.start();assert reopened.request('sync.status')['batches'][0]['id']==value['original_id'];assert unchanged()
            finally:assert reopened.close(timeout=10)
            assert not reopened.running and unchanged();checks.append('退出后新服务立即打开同一目录，原受理号与完整任务文件逐字节保持')
            assert len(ticks)>3;checks.append('启动、占用错误与退出收尾均在后台执行，Qt事件循环持续响应')
            report.update(passed=True,component_exited=True,retained_component_pid=pid,jobs_unchanged=True)
        except Exception:report['error']=traceback.format_exc()
        finally:
            if dialog:dialog.reject();dialog.deleteLater()
            if ghost:ghost.deleteLater()
            win._discard_close=True;win.job_dirty=False;win.close()
            if owner:
                try:wait(lambda:owner.client is None and (component is None or not component.running))
                except Exception:report['passed']=False;report['close_error']=traceback.format_exc()
            timer.stop();report['heartbeat_ticks']=len(ticks)
            (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(250,execute)
