"""Opt-in packaged UI acceptance on an explicitly isolated synthetic database."""
import json,time,traceback,sqlite3
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtTest import QTest


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='jst_events'")}


def cells(path):
    import openpyxl
    b=openpyxl.load_workbook(path);r=[(s.title,s.freeze_panes,[[[c.value,c.data_type,c.number_format,str(c._style)] for c in row] for row in s]) for s in b];b.close();return r


def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];checks=[];state={};ticks=[];last=[time.perf_counter()]
    def tick():
        now=time.perf_counter();ticks.append(now-last[0]);last[0]=now
        d=QApplication.activeModalWidget()
        if isinstance(d,QMessageBox):state['error']=d.windowTitle()+': '+d.text();d.reject()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(tick);timer.start()
    def wait(fn):
        loop=QEventLoop();poll=QTimer();start=time.monotonic()
        poll.timeout.connect(lambda:loop.quit() if fn() or state.get('error') or time.monotonic()-start>90 else None);poll.start(15);loop.exec();poll.stop()
        if state.get('error'):raise AssertionError(state['error'])
        if not fn():raise AssertionError('UI timeout')
    def execute():
        dialog=None
        try:
            import jst_results as service,jst_orders as orders,work_repository as wr
            from lifecycle_home_ui import StageDialog
            path=win.work_repository.path
            if path.resolve()!=(out/'settings/workbench.sqlite3').resolve():raise ValueError('Isolation path required')
            win._discard_close=True;win.followup.search.setText('不应继承');win.followup.status.setCurrentText('待回传')
            dialog=StageDialog(win.followup,'jst');dialog.setModal(True);win._large_io_dialog=dialog;dialog.show();p=dialog.panel
            wait(lambda:p.data is not None)
            wait(lambda:p.data['counts']['ready']==4)
            wait(lambda:win.jst_generator.worker is None);win.jst_generator.stopped=True
            if p.search.text() or p.warehouse.currentIndex()!=0:raise AssertionError('inherited hidden filters')
            checks.append('结果页保持打开时自动生成4个仓单；不继承生命周期和隐藏筛选')
            before=facts(path)
            def export_click(btn):
                old=getattr(p,'receipt',None);QTest.mouseClick(btn,Qt.MouseButton.LeftButton)
                wait(lambda:p.worker is None and getattr(p,'receipt',None) is not old);wait(lambda:not p.reading)
                return p.receipt
            r=export_click(p.all_btn)
            if r['remaining'] or len(r['files'])!=4:raise AssertionError(r)
            expected=[(Path(f['path']).name,cells(f['path'])) for f in r['files']]
            QTest.qWait(60);dialog.grab().save(str(out/(phase+'-results.png')))
            if phase=='exercise':
                r=export_click(p.all_btn)
                if [(Path(f['path']).name,cells(f['path'])) for f in r['files']]!=expected:raise AssertionError('repeat values/order/style')
                checks.append('同页两次真实点击导出全部，无确认弹框；4份同名覆盖且值/类型/样式/顺序相同')
                top=p.table.topLevelItem(0);top.setExpanded(True);p.table.setCurrentItem(top.child(0));p.table.setFocus();QTest.keyClick(p.table,Qt.Key.Key_Space)
                if len(p.chosen)!=1 or top.checkState(0)!=Qt.CheckState.PartiallyChecked:raise AssertionError('warehouse checkbox/partial parent')
                r=export_click(p.selected_btn)
                if len(r['files'])!=1:raise AssertionError('single warehouse export')
                checks.append('键盘Space勾选单仓、门店部分选中，导出勾选仅1份')
                p.search.setText('没有这个门店')
                if p.chosen or p.table.topLevelItemCount()!=0:raise AssertionError('query selection clear')
                r=export_click(p.all_btn)
                if len(r['files'])!=4:raise AssertionError('all export incorrectly filtered')
                p.search.clear();checks.append('搜索清空勾选；筛选无行时整批全部仍是4份')
                # Real Windows sharing violation, then retry only the original failed file.
                import ctypes
                from ctypes import wintypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];kernel.CreateFileW.restype=wintypes.HANDLE
                kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
                handle=kernel.CreateFileW(r['files'][0]['path'],0x80000000,0,None,3,0x80,None)
                if handle==wintypes.HANDLE(-1).value:raise OSError(ctypes.get_last_error())
                try:r=export_click(p.all_btn)
                finally:kernel.CloseHandle(handle)
                if len(r['remaining'])!=1 or len(r['files'])!=3:raise AssertionError('occupied file receipt')
                r=export_click(p.retry_btn)
                if r['total']!=1 or r['remaining']:raise AssertionError('failed retry not exact')
                checks.append('真实Windows文件占用失败；关闭占用后只重试原失败1份')
                original=service.export
                def slow(path,plan,folder,progress=lambda _:None,cancel=None):
                    def report(msg):progress(msg);time.sleep(.12)
                    return original(path,plan,folder,report,cancel)
                service.export=slow
                old=p.receipt;QTest.mouseClick(p.all_btn,Qt.MouseButton.LeftButton)
                wait(lambda:p.progress.value()>=1);QTest.mouseClick(p.stop_btn,Qt.MouseButton.LeftButton)
                wait(lambda:p.worker is None and p.receipt is not old);wait(lambda:not p.reading)
                if not p.receipt['cancelled'] or not p.remaining:raise AssertionError('cancel not exercised')
                service.export=original;r=export_click(p.retry_btn)
                if r['remaining']:raise AssertionError('cancel retry')
                checks.append('真实停止按钮取消剩余文件，按原gid重试，不再次计算')
                if facts(path)!=before:raise AssertionError('export changed business tables')
                # Closing during export waits for a safe file boundary.
                service.export=slow;QTest.mouseClick(p.all_btn,Qt.MouseButton.LeftButton);QTimer.singleShot(25,dialog.close)
                wait(lambda:p.worker is None and not dialog.isVisible());service.export=original
                checks.append('导出时关闭窗口，安全退出，无强杀提交')
            else:
                if len(p.data['groups'])!=2 or any(r['last_export']=='未导出' for r in p.records):raise AssertionError('reopen lost results')
                if facts(path)!=before:raise AssertionError('reopen export changed business tables')
                checks.append('重启直接读取2店4仓单，重复导出无需生命周期筛选')
                dialog.resize(1100,720);QTest.qWait(80)
                for widget in (p.all_btn,p.selected_btn,p.open_btn,p.progress):
                    pos=widget.mapTo(dialog,widget.rect().bottomRight())
                    if not dialog.rect().contains(pos):raise AssertionError('small window export controls clipped')
                p.all_btn.setFocus();QTest.keyClick(p.all_btn,Qt.Key.Key_Tab)
                if QApplication.focusWidget() is not p.open_btn:raise AssertionError('keyboard focus skips fixed action')
                dialog.grab().save(str(out/'reopen-1100x720.png'))
                checks.append('1100×720逻辑像素窗口底部按钮/进度可见，Tab跳过禁用勾选导出到打开目录')
                dialog.reject()
            dialog.deleteLater();dialog=None;win._large_io_dialog=None
            result={'passed':True,'phase':phase,'checks':checks,'heartbeat_max':max(ticks,default=0),'source_commit':__import__('version_info').diagnostic().get('source_commit')}
        except Exception:result={'passed':False,'phase':phase,'checks':checks,'error':traceback.format_exc()}
        (out/('results-ui-'+phase+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        timer.stop()
        if dialog:
            dialog.reject()
            if dialog.panel.worker:wait(lambda:dialog.panel.worker is None)
        win._large_io_dialog=None;win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if result['passed'] else 1))
    QTimer.singleShot(100,execute)
