"""Opt-in isolated EXE walkthrough and genuine UI captures; no live platforms."""
import copy
import ctypes
import json
import time
from datetime import date,timedelta
from pathlib import Path
from PySide6.QtCore import QTimer,Qt,QDate
from PySide6.QtWidgets import QMessageBox,QPushButton,QTableWidget,QComboBox,QTabWidget
from PySide6.QtTest import QTest


def exercise(app,win,out,codes,checks):
    def wait(predicate):
        end=time.monotonic()+90
        while not predicate() and time.monotonic()<end:QTest.qWait(20)
        assert predicate(),'walkthrough background timeout'
    def shot(widget,name):
        QTest.qWait(120);assert widget.grab().save(str(out/(name+'.png')))
    before=copy.deepcopy(win.results)
    win.show_section(0);shot(win,'home')
    win.show_section(1);win.navigate(3);shot(win,'signed-overview')
    from preissue_ui import FinalListDialog
    import display_settings
    dialog=FinalListDialog(win,codes);dialog.show();QTest.qWait(150)
    dialog.table.setCurrentIndex(dialog.proxy.index(0,3));shot(dialog,'audit-10pt')
    display_settings.apply({'font':12,'density':'roomy'});QTest.qWait(150)
    assert dialog.table.verticalHeader().defaultSectionSize()>=dialog.table.fontMetrics().height()+8
    dialog.table.horizontalScrollBar().setValue(dialog.table.horizontalScrollBar().maximum())
    assert '当前商品' in dialog.reading_identity.text();shot(dialog,'audit-12pt')
    dialog.reject();dialog.deleteLater();display_settings.apply({'font':10,'density':'standard'})
    assert win.results==before
    checks.append('actual audit dialog: font 10/12, row-height adjustment, horizontal-scroll identity, cancel preserves all calculation results')

    # Deny sharing using a real Windows handle: force the actual UI export path
    # to fail without changing permissions or touching any real business file.
    import normal_orders,normal_exports,export_files,export_receipts
    repo=normal_orders.Repository(win.work_repository);order=repo.get(win.job_day,codes[0])
    files=sorted((out/'exports').glob('*.xlsx'));locked=files[0];sha=export_files.sha256(locked)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    kernel.CreateFileW.restype=ctypes.c_void_p;kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=kernel.CreateFileW(str(locked),0x80000000,0,None,3,0x80,None)
    assert handle not in (None,ctypes.c_void_p(-1).value),'cannot create isolated sharing lock'
    events=[];controller=QTimer(win);controller.setInterval(80)
    def control():
        modal=app.activeModalWidget()
        if isinstance(modal,QMessageBox):
            if modal.windowTitle()=='导出已签发版本':
                assert modal.defaultButton() is modal.button(QMessageBox.StandardButton.Cancel)
                modal.button(QMessageBox.StandardButton.Yes).click()
            elif modal.windowTitle()=='操作未完成':
                events.append(modal.text());shot(modal,'export-locked-error');modal.accept()
    controller.timeout.connect(control);controller.start()
    try:win.export_current(codes)
    finally:controller.stop();kernel.CloseHandle(handle)
    assert events and '导出任务与恢复' in events[-1],events
    assert export_files.sha256(locked)==sha
    assert all(repo.get(win.job_day,s)['revision']==1 for s in codes)
    failed=next(t for t in export_receipts.recent(win.work_repository.path) if t['state']=='导出未全部完成')
    assert any(f.get('error') for f in failed['files'])
    checks.append('actual 核对与签发→确认签发并导出: Windows file lock produces readable receipt; old file and signed revision preserved')

    import export_recovery_ui
    phase={'value':0};timer=QTimer(win);timer.setInterval(150)
    def recover_ui():
        modal=app.activeModalWidget()
        if not modal:return
        if modal.windowTitle()=='导出任务与恢复':
            if phase['value']==0:
                phase['value']=1;combo=modal.findChild(QComboBox);combo.setCurrentIndex(combo.findData(failed['id']))
                target=next(b for b in modal.findChildren(QPushButton) if b.text()=='核对所选任务');QTimer.singleShot(0,target.click)
            elif phase['value']==1:
                table=modal.findChild(QTableWidget)
                if not table.rowCount():return
                phase['value']=2;shot(modal,'export-recovery')
                target=next(b for b in modal.findChildren(QPushButton) if b.text()=='按勾选项恢复');QTimer.singleShot(0,target.click)
            elif phase['value']==3:phase['value']=4;modal.accept()
        elif modal.windowTitle()=='恢复结果 · 原签发不变':
            shot(modal,'recovery-result');phase['value']=3;modal.accept()
    timer.timeout.connect(recover_ui);timer.start()
    try:export_recovery_ui.show(win,win.work_repository.path)
    finally:timer.stop()
    assert phase['value']==4 and all(repo.get(win.job_day,s)['revision']==1 for s in codes)
    checks.append('actual recovery dialog: inspect selected receipt, restore checked missing files to new directory, no re-signing')

    from lifecycle_batch_ui import BatchImport
    d=BatchImport(win,win,'return');d.show();d.add_paths([str(locked)])
    assert d.documents and not d.issues and all(c.currentData() for c in d.targets),d.note.text()
    d.table.selectRow(0);shot(d,'return-binding')
    def capture_details():
        modal=app.activeModalWidget()
        if modal and modal.windowTitle()=='文件关联信息（只读）':shot(modal,'return-identity');modal.accept()
    timer=QTimer(d);timer.setInterval(150);timer.timeout.connect(capture_details);timer.start()
    try:d.file_identity()
    finally:timer.stop()
    d.preview();assert d.review is not None,d.note.text();shot(d,'return-review')
    d.save();assert d.receipt and all(r['stage']=='return' for r in d.receipt);d.deleteLater()
    checks.append('actual signed workbook → batch return: hidden identities matched; readonly metadata shown; preview and atomic save completed')

    # Self-contained analysis fixtures are created only under isolated settings.
    from stockout_repository import Repository
    from stockout_facts import load_catalog,load_store
    import openpyxl
    analysis=Repository(win.repository.folder);analysis.initialize()
    stores=[{'code':s,'name':win.results[s]['store_name']} for s in codes]
    analysis.save_settings({'stores':stores,'routes':{s:{'primary':'山东仓','secondary':None} for s in codes},'profile':None})
    def workbook(name,headers,rows):
        path=out/name;book=openpyxl.Workbook();sheet=book.active;sheet.append(headers)
        for row in rows:sheet.append(row)
        book.save(path);book.close();return path
    catalog=workbook('演示仓资料.xlsx',['商品编码','商品状态','可用数','分类','供应商名称','虚拟分类'],
        [['A','启用',100,'饮品','演示供货商甲',''],['B','启用',50,'食品','演示供货商乙','淘便考核'],['C','禁用',10,'食品','演示供货商乙','']])
    catalog_id=load_catalog(analysis,catalog,'山东仓')
    for ago in (2,1,0):
        day=(date.fromisoformat(win.job_day)-timedelta(days=ago)).isoformat();run=analysis.new_run(day=day);analysis.change(run['id'],catalogs={'山东仓':catalog_id})
        for s in codes:
            path=workbook('演示销量.xlsx',['仓库/门店编码','商品编码','商品名称','月销','可用库存','在途库存'],
                [[s,'A','演示饮料',30,0 if ago!=1 else 2,8],[s,'B','演示食品',20,-2,0],[s,'C','演示禁用品',12,0,0]])
            load_store(analysis,run['id'],s,path,observed=day+'T08:00:00+08:00')
        analysis.publish(run['id'])
    win.stockout.enter();wait(lambda:win.stockout.worker is None and win.stockout.queued is None)
    page=win.stockout.page;page.refresh();wait(lambda:win.stockout.worker is None and win.stockout.queued is None)
    shot(win,'stockout-overview');page.set_dimensions(['store','warehouse','supplier','category']);page.refresh()
    wait(lambda:win.stockout.worker is None and win.stockout.queued is None);page.findChild(QTabWidget).setCurrentIndex(1);shot(win,'stockout-cuts')
    from stockout_ai_ui import show
    timer=QTimer(win);timer.setInterval(150)
    def ai_prompt():
        modal=app.activeModalWidget()
        if modal and modal.windowTitle()=='AI 门店缺货率分析':shot(modal,'ai-stockout');modal.accept()
    timer.timeout.connect(ai_prompt);timer.start()
    try:show(page,page.options(),page.dimensions())
    finally:timer.stop()
    from stockout_ai import analyze,read,export
    report=analyze(win.repository.folder,page.options(),page.dimensions())
    assert report['overview']['total']['base']==12 and report['overview']['total']['zero']==10
    assert read(win.repository.folder,report,'repeated')['total']==4
    export(win.repository.folder,report,out/'AI缺货率同口径.xlsx',include_detail=True)
    checks.append('packaged independent stockout: native UI and AI totals agree; negative stock, disabled exclusion, repeated observations, pinned Excel export exercised')
    import sys,subprocess
    from unittest.mock import patch
    gateway=win.automation
    with patch('automation_connection.refresh_registration',lambda _:None):gateway.enable()
    bridge=[str(Path(sys.executable).parent/'AI接口/补货助手AI接口.exe')] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).parent/'automation_bridge.py')]
    calls=[]
    def call(tool,args):
        request=out/('bridge-'+str(len(calls))+'.json');request.write_text(json.dumps(args,ensure_ascii=False),'utf-8')
        process=subprocess.Popen([*bridge,'--connection',str(gateway.connection_file),'call',tool,'--arguments-file',str(request)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        wait(lambda:process.poll() is not None);stdout,stderr=process.communicate();value=json.loads(stdout)
        assert process.returncode==0 and value['ok'],(tool,value,stderr.decode('utf-8',errors='replace'))
        calls.append({'tool':tool,'ok':value['ok']});return value['result']
    context=call('workbench_context',{});assert call('stockout_context',{})['available']
    task=call('query_stockout_analysis',{'context_token':context['context_token'],'operation_key':'exe-query','query':page.options(),'dimensions':page.dimensions()})
    wait(lambda:gateway.worker is None)
    assert call('task_status',{'task_id':task['task_id']})['state']=='completed'
    summary=call('stockout_analysis_results',{'task_id':task['task_id']});assert summary['overview']==report['overview']
    args={'task_id':task['task_id'],'level':'zero'};pending=call('stockout_analysis_results',args);assert pending['page_pending'];wait(lambda:gateway.worker is None)
    assert call('stockout_analysis_results',args)['total']==10
    output=call('export_stockout_analysis',{'task_id':task['task_id'],'snapshot_id':summary['snapshot_id'],'context_token':context['context_token'],'operation_key':'exe-export','directory':str(out)})
    wait(lambda:gateway.worker is None);assert call('task_status',{'task_id':output['task_id']})['state']=='completed'
    gateway.disable();(out/'bridge-report.json').write_text(json.dumps({'passed':True,'transport':'real bundled CLI and named pipe; same gateway as MCP; no external AI client','calls':calls},ensure_ascii=False,indent=2),'utf-8')
    checks.append('actual AI interface executable: named-pipe context, query, asynchronous page, status and export completed; no external Codex/WorkBuddy client asserted')
    win.show_section(1);QTest.qWait(150);assert win.stockout.page is None
    assert win.results==before
    (out/'screenshot-index.json').write_text(json.dumps({'source':'actual EXE' if getattr(__import__('sys'),'frozen',False) else 'source application',
        'data':'isolated synthetic fixtures; no live platform calls','screenshots':[p.name for p in out.glob('*.png')]},ensure_ascii=False,indent=2),'utf-8')
