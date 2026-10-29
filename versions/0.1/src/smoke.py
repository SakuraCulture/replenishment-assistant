"""Opt-in local release self-test. Receives all fixture paths on command line."""
import json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
import engine

def start_smoke(app,win,args):
    sales,products,output=args[:3]
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    state={'stage':0,'started':time.monotonic(),'checks':[]}
    def finish(error=None):
        timer.stop()
        report={'passed':error is None,'error':error,'checks':state['checks'],'summary':win.result['summary'] if win.result else None}
        (out/'ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        app.exit(1 if error else 0)
    def fail(message):finish(message)
    win.failed=fail
    sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def snap(name):
        win.repaint();assert win.grab().save(str(out/(name+'.png')))
    def tick():
        try:
            if time.monotonic()-state['started']>240:raise TimeoutError(str(state))
            stage=state['stage']
            if stage==0:
                snap('01_初始界面');win.sales.set_path(sales);win.products.set_path(products);state['stage']=1
            elif stage==1 and win.loaded and win.worker is None:
                assert len(win.model.rows)==14244
                assert engine.summarize(win.model.rows)['local']==5161
                assert win.calculate_btn.isEnabled() and not win.export_all.isEnabled()
                snap('02_导入检查');state['checks'].append('import preview and local classification')
                QTest.mouseClick(win.calculate_btn,Qt.MouseButton.LeftButton);state['stage']=2
            elif stage==2 and win.result and win.worker is None:
                assert win.result['summary']['amount']==23833.55
                assert win.result['summary']['exception']==8
                assert win.proxy.rowCount()==1046 and win.export_all.isEnabled()
                snap('03_计算结果');win.mode.setCurrentText('地采商品');state['stage']=3
            elif stage==3:
                assert win.proxy.rowCount()==5161
                win.show_detail(win.proxy.index(0,0));assert '地采商品' in win.details.toPlainText()
                snap('04_地采明细');win.detail_panel.hide();win.mode.setCurrentText('一箱例外');state['stage']=4
            elif stage==4:
                assert win.proxy.rowCount()==8
                win.show_detail(win.proxy.index(0,0));assert '放行依据' in win.details.toPlainText()
                snap('05_一箱例外');win.resize(1080,720);state['stage']=5
            elif stage==5:
                snap('06_小窗口');win.resize(1380,900)
                first=win.visible_rows()[0]['E'];win.search.setText(first);assert win.proxy.rowCount()==1
                win.search.setText('no_such_product_0');assert win.proxy.rowCount()==0
                win.search.clear();win.detail_panel.hide();win.mode.setCurrentText('全部商品');assert win.proxy.rowCount()==14244
                state['checks'].append('local / exception / search filters and explanation drawer')
                win.exception.setChecked(False);assert win.dirty and not win.export_all.isEnabled()
                win.start_calculation();state['stage']=6
            elif stage==6 and win.worker is None:
                assert win.result['summary']['amount']==23454.75 and win.result['summary']['exception']==0
                state['checks'].append('toggle off restores exact baseline; stale export disabled')
                win.exception.setChecked(True);win.start_calculation();state['stage']=7
            elif stage==7 and win.worker is None:
                assert win.result['summary']['amount']==23833.55
                win.mode.setCurrentText('一箱例外')
                def exported(path):state['export']=path
                win.job(engine.export_xlsx,(win.result,str(out/'界面导出_一箱例外.xlsx'),win.visible_rows(),'当前筛选：一箱例外'),exported)
                state['stage']=8
            elif stage==8 and win.worker is None:
                assert Path(state['export']).is_file()
                import openpyxl
                book=openpyxl.load_workbook(state['export'],read_only=True)
                assert sum(1 for _ in book['筛选明细'].iter_rows())==9;book.close()
                state['checks'].append('background filtered export reopened with eight rows')
                spin=win.warehouses.cellWidget(0,1);spin.setValue(spin.value()+.5)
                assert win.dirty and not win.export_all.isEnabled()
                state['checks'].append('warehouse parameter change invalidates prior result')
                finish()
        except Exception:finish(traceback.format_exc())
    timer=QTimer(win);timer.setInterval(300);timer.timeout.connect(tick);timer.start()
