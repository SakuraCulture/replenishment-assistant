"""Opt-in isolated real-EXE diagnostics export, IO and normal-close acceptance."""
import hashlib,json,sys,time,traceback,zipfile
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton


def wait_task(*,progress,cancel):
    from runtime_support import checkpoint
    from export_progress import RowProgress
    for i in range(60):
        checkpoint(cancel);progress(RowProgress('核对合成记录：'+str(i),i,60));time.sleep(.1)
    return {'unchanged_result':[3,1,2]}


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();phase=args[1];spec=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert spec['synthetic'] is True and spec['live_platform'] is False
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.timer.stop();win.aoxiang_workflows.stopping=True
    import performance_log as perf,performance_log_ui,export_locations
    from application_runtime import current
    monitor=current().performance
    checks=[];shots=[];dialogs=[];state={'export_clicked':False,'error':None}
    target=out/('native-performance-'+phase+'.zip')
    def require(condition,text):
        if not condition:raise AssertionError(text)
        checks.append(text)
    def capture(widget,name):
        if widget.grab().save(str(out/name)):shots.append(name)
    def poll():
        dialog=QApplication.activeModalWidget()
        try:
            if dialog is None:return
            if dialog.objectName()=='performanceLog':
                if not state['export_clicked']:
                    state['export_clicked']=True;capture(dialog,'performance-menu-'+phase+'.png')
                    QTimer.singleShot(0,dialog.findChild(QPushButton,'exportPerformanceLog').click)
                else:dialog.accept()
            elif isinstance(dialog,QMessageBox):
                dialogs.append({'title':dialog.windowTitle(),'text':dialog.text()});capture(dialog,'performance-result-'+phase+'.png')
                require(dialog.windowTitle()=='性能日志已导出','真实导出成功弹窗可见；未替换QMessageBox')
                dialog.accept()
        except Exception:
            state['error']=traceback.format_exc()
            if dialog is not None:dialog.reject()
    timer=QTimer(win);timer.setInterval(100);timer.timeout.connect(poll);timer.start()
    def exercise():
        from version_info import diagnostic
        report={'passed':False,'phase':phase,'actual_exe':bool(getattr(sys,'frozen',False)),'checks':checks,
                'screenshots':shots,'dialogs':dialogs,'live_platform':False,'version':diagnostic(),'messagebox_stubs':False}
        original=export_locations.save_file
        try:
            require(monitor is perf._current and monitor.thread.is_alive(),'默认低频记录已启动')
            require(monitor.folder.resolve().is_relative_to(out),'日志处于隔离资料目录')
            require(win.concurrency.value()==2,'默认计算并发保持2')
            if phase=='full':
                import work_repository as wr,normal_delivery as nd
                from signing_memory_fixture import fixture
                from ui_work_io import Session,request
                payload,codes=fixture(2000,2,20)
                payload['execution_warehouses']={s:list(dict.fromkeys(r['AU'] for r in payload['results'][s]['rows'])) for s in codes}
                with Session(win.work_repository.path) as session:
                    result=request(win,session,'delivery_probe',[nd.probe_payload(payload,codes),codes])
                    require(len(result['plans'])==2,'实际后台核对结果仍为2单')
                    tail={**payload,'results':{s:r for s,r in payload['results'].items() if s not in codes}}
                    request(win,session,'delivery_expand',[tail,list(payload['results'])])
                    (out/'exports').mkdir();result=request(win,session,'delivery_execute',str(out/'exports'))
                    require(len(result['paths'])==2,'日志开启时实际整批签发导出2份')
                require(len(win.orders.headers())==2,'正式归档记录仍完整')
                import openpyxl,normal_exports
                for path,code in zip(result['paths'],codes):
                    book=openpyxl.load_workbook(path,read_only=True)
                    try:actual=[(r[4],r[16],r[15]) for name in normal_exports.SHEETS for r in book[name].iter_rows(min_row=2,values_only=True) if r and r[4] is not None]
                    finally:book.close()
                    require(actual==[(r['E'],r['AS'],r['V']) for r in payload['results'][code]['rows']],code+'数量、价格和商品顺序逐行不变')
            else:require(len(win.orders.headers())==2,'退出重开后原有2单仍存在')
            from business_ui import run
            require(run(win,wait_task,(),'核对合成记录')=={'unchanged_result':[3,1,2]},'通用后台操作结果与顺序不变')
            export_locations.save_file=lambda *a,**k:(str(target),'ZIP文件 (*.zip)')
            performance_log_ui.show(win)
            if state['error']:raise AssertionError(state['error'])
            require(target.is_file(),'真实菜单导出了可发送的ZIP')
            with zipfile.ZipFile(target) as z:
                require(set(z.namelist())=={'说明.txt','环境.json','性能日志.jsonl','摘要.json'},'ZIP仅含轻量诊断白名单文件')
                summary=json.loads(z.read('摘要.json'));records=[json.loads(line) for line in z.read('性能日志.jsonl').splitlines()]
                require(summary['samples']>=2,'正常5秒间隔资源样本已落盘')
                require(any('private_bytes' in p and 'cpu_seconds' in p for r in records if r['kind']=='sample' for p in r['processes']),'实际原生CPU与内存计数可读取')
                require(any(r['kind']=='operation_end' and r['operation']=='performance_log_smoke.wait_task' for r in records),'短操作/通用后台操作耗时可追踪')
                if phase=='full':require(any(r['kind']=='operation_end' and r['operation']=='workio.delivery_execute' for r in records),'实际签发归档导出阶段耗时已记录')
                require(not summary['incomplete_logging_observed'],'本轮无丢弃或日志采样错误')
                raw=z.read('性能日志.jsonl').decode()
                require('合成门店' not in raw and str(out) not in raw and '500000000000' not in raw,'导出日志没有商品、门店与路径内容')
            report.update(passed=True,export_summary=summary,zip_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
            capture(win,'performance-completed-'+phase+'.png')
        except Exception:report['error']=traceback.format_exc()
        finally:
            export_locations.save_file=original;timer.stop()
            (out/('report-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.job_dirty=False;win._discard_close=True;win.close()
    QTimer.singleShot(350,exercise)
