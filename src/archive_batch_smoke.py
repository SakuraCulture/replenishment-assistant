"""Opt-in source/EXE UI acceptance; uses only the existing isolated smoke DB."""
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QApplication,QDialog,QFileDialog,QComboBox
import openpyxl,hashlib
from contextlib import closing
import archive_batch as ab
from business_ui import Task
from archive_batch_ui import Preview

def exercise(win,out,checks,shot,wait):
    original=QFileDialog.getExistingDirectory;folder=out/'batch-archive';folder.mkdir();QFileDialog.getExistingDirectory=lambda *a,**k:str(folder)
    state={'mode':1,'kind':'signed','cancel':False,'cancelled':False,'preview':False,'missing':False};errors=[]
    def dialogs():
        try:
            for d in QApplication.topLevelWidgets():
                if not isinstance(d,QDialog) or not d.isVisible():continue
                if isinstance(d,Task) and state['cancel'] and not state['cancelled']:
                    state['cancelled']=True;d.close()
                elif isinstance(d,Preview):
                    state['preview']=True;d.mode.setCurrentIndex(state['mode']);shot(d,'batch-preview-'+state['kind']+'.png')
                    if state['missing']:
                        if d.start.isEnabled() or not any(i['error'] for i in d.plan['items']):raise AssertionError('missing sources accepted')
                        d.reject();continue
                    if not d.start.isEnabled():raise AssertionError('preview unexpectedly unavailable')
                    d.accept()
                elif d.windowTitle()=='商品资料：请选择实际产物':
                    d.findChild(QComboBox).setCurrentIndex(1 if state['kind']=='reference' else 0);d.accept()
                elif d.windowTitle()=='导出结果 · 原归档未改变':shot(d,'batch-result.png');d.accept()
        except Exception as exc:errors.append(str(exc))
    timer=QTimer(win);timer.timeout.connect(dialogs);timer.start(25)
    try:
        for i in range(len(win.archive_orders)):win.archive_table.item(i,0).setCheckState(Qt.CheckState.Checked)
        if len(win.archive_checked)!=4:raise AssertionError('checkbox selection failed')
        shot(win,'batch-archive-toolbar.png')
        for kind,mode,expected in [('signed',0,4),('signed',1,2),('calculation',1,2),('original',1,2),('reference',1,2)]:
            state.update(kind=kind,mode=mode,preview=False);before=set(folder.iterdir())
            win.export_archive_batch(kind)
            if errors:raise AssertionError(errors)
            added=list(set(folder.iterdir())-before)
            if len(added)!=1 or not state['preview']:raise AssertionError('export task/preview missing '+kind)
            files=list(added[0].glob('*.xlsx'))
            if len(files)!=expected:raise AssertionError((kind,len(files),expected))
            for f in files:
                b=openpyxl.load_workbook(f)
                if kind!='original' and len(b.sheetnames)!=1:raise AssertionError('contents bundled')
                b.close()
            if kind=='original':
                with closing(win.work_repository.connect()) as db:hashes={r[0] for r in db.execute('SELECT sha FROM source_files')}
                if any(hashlib.sha256(f.read_bytes()).hexdigest() not in hashes for f in files):raise AssertionError('original bytes changed')
            checks.append(f'actual batch UI {kind}/{mode}: {expected} files; exact warehouse revision preview')
        # The preparation dialog is cancellable; it must not launch a file task.
        before=set(folder.iterdir());state.update(cancel=True,cancelled=False,preview=False)
        win.export_archive_batch('calculation')
        if not state['cancelled']:raise AssertionError('no actual cancellation reached')
        if set(folder.iterdir())!=before:raise AssertionError('cancelled preparation exported')
        state.update(cancel=False);win.export_archive_batch('signed')
        if set(folder.iterdir())==before:raise AssertionError('retry did not export')
        checks.append('actual preparation cancellation and retry; no task directory on cancelled preparation')
        # Corrupt/missing only this smoke database, restore its original rows immediately.
        with closing(win.work_repository.connect()) as db:original_rows=db.execute('SELECT * FROM source_files').fetchall()
        try:
            with closing(win.work_repository.connect()) as db,db:db.execute('DELETE FROM source_files')
            state.update(kind='original',missing=True,preview=False);before=set(folder.iterdir());win.export_archive_batch('original')
            if not state['preview'] or set(folder.iterdir())!=before or errors:raise AssertionError('missing-original UI failed '+repr(errors))
            checks.append('actual missing-original preview disables export; no explanatory workbook substituted')
        finally:
            with closing(win.work_repository.connect()) as db,db:db.executemany('INSERT INTO source_files VALUES(?,?,?,?)',original_rows)
            state.update(missing=False)
        win.archive_search.setText('QA_A');wait(lambda:win.history_reader.active is None and not win.archive_timer.isActive())
        if win.archive_checked:raise AssertionError('query retained hidden selection')
        win.archive_search.clear();wait(lambda:win.history_reader.active is None and not win.archive_timer.isActive())
        # Exercise pagination selection with lightweight copied headers only; never persisted.
        old_all,old_matches=win.archive_all,win.archive_matches
        from copy import deepcopy
        many=[]
        for i in range(105):
            h=deepcopy(old_all[0]);h['oid']='synthetic-page-'+str(i);many.append(h)
        win.archive_all=many;win.archive_matches=[True]*105;win.render_archive_page();win.check_archive(win.archive_orders);win.archive_turn(1);win.check_archive(win.archive_orders)
        if len(win.archive_checked)!=105:raise AssertionError('cross-page selection lost')
        win.archive_all=old_all;win.archive_matches=old_matches;win.clear_archive_selection();win.archive_page=0;win.render_archive_page()
        checks.append('filter clears selection; 105 synthetic headers preserve selection across pages')
    finally:timer.stop();timer.deleteLater();QFileDialog.getExistingDirectory=original

def single(win,head,out,kind='calculation'):
    """Drive detail/event independent button through the real preview and result UI."""
    original=QFileDialog.getExistingDirectory;QFileDialog.getExistingDirectory=lambda *a,**k:str(out)
    def tick():
        for d in QApplication.topLevelWidgets():
            if isinstance(d,Preview) and d.isVisible():d.mode.setCurrentIndex(0);d.accept()
            elif isinstance(d,QDialog) and d.isVisible() and d.windowTitle()=='导出结果 · 原归档未改变':d.accept()
    timer=QTimer(win);timer.timeout.connect(tick);timer.start(25)
    try:win.export_archive(head,kind)
    finally:timer.stop();timer.deleteLater();QFileDialog.getExistingDirectory=original
