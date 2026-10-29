"""Opt-in UI acceptance for the actual packaged app, isolated by launcher."""
import json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QTableWidget,QPushButton
import batch_service,engine
from version_info import diagnostic

def start_smoke(app,win,args):
    sales,products,output=args[:3];out=Path(output);out.mkdir(parents=True,exist_ok=True)
    state={'step':0,'start':time.monotonic(),'checks':[]};timer=QTimer(win)
    def finish(error=None):
        timer.stop();meta=diagnostic();report={'passed':error is None,'error':error,'version':meta['version'],'source_commit':meta['source_commit'],'checks':state['checks']}
        (out/'ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(1 if error else 0)
    win.failed=lambda message:finish(message)
    sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def snap(name):win.repaint();assert win.grab().save(str(out/(name+'.png')))
    def tick():
        try:
            if time.monotonic()-state['start']>380:raise TimeoutError(str(state))
            step=state['step']
            if step==0:
                win.master_legacy.setChecked(True)
                snap('01_准备数据');fixture=Path(sales).parent
                files=[str(fixture/n) for n in ['part1.xlsx','part2.xlsx','conflict.xlsx']]
                win.sales.add_paths(files if all(Path(p).exists() for p in files) else [sales]);win.products.set_path(products);QTest.mouseClick(win.load_btn,Qt.MouseButton.LeftButton);state['step']=1
            elif step==1 and win.loaded and win.worker is None:
                if win.loaded['intake']['conflicts']:
                    assert win.loaded['blocked'];state['step']=10
                    def choose():
                        try:
                            dialog=QApplication.activeModalWidget();table=dialog.findChild(QTableWidget)
                            assert table.rowCount()>0
                            for i in range(table.rowCount()):table.cellWidget(i,2).setCurrentIndex(1)
                            assert dialog.grab().save(str(out/'02a_重复冲突.png'))
                            accept=next(b for b in dialog.findChildren(QPushButton) if b.text()=='应用选择并重新检查');QTest.mouseClick(accept,Qt.MouseButton.LeftButton)
                        except Exception:finish(traceback.format_exc())
                    QTimer.singleShot(150,choose);win.resolve_conflicts();state['step']=11
                else:state['step']=11
            elif step==11 and win.loaded and win.worker is None:
                assert not win.loaded['blocked'];state['checks'].append('multi-file UI import, duplicate conflict dialog and explicit source choice')
                assert len(win.loaded['stores'])==2;assert not win.calculate_btn.isEnabled();win.navigate(1)
                codes=list(win.store_widgets);state['codes']=codes
                for s in codes:
                    w=win.store_widgets[s];w['mode'].setCurrentIndex(2);w['amount'].setText('3000' if s==codes[0] else '5000')
                assert win.calculate_btn.isEnabled();snap('02_门店预算');QTest.mouseClick(win.calculate_btn,Qt.MouseButton.LeftButton);state['step']=2
            elif step==2:
                if not state.get('progress_snap'):snap('03_并发进度');state['progress_snap']=True
                if win.worker is None:
                    assert all(s['state']=='已完成' for s in win.states.values()),win.states
                    assert len({r['task']['pid'] for r in win.results.values()})==2
                    state['checks'].append('application process workers calculate two stores successfully')
                    win.navigate(3);snap('04_门店汇总');win.open_overview(0,7);assert win.result and not win.dirty
                    win.mode.setCurrentText('必卖未补足');assert win.proxy.rowCount()>0;win.show_detail(win.proxy.index(0,0));snap('05_商品缺口依据')
                    win.resize(1080,720);win.navigate(1);state['step']=3
            elif step==3:
                snap('06_小窗口');win.resize(1380,900);a,b=state['codes'];state['other']=win.results[b]
                win.store_widgets[a]['amount'].setText('0');assert win.states[a]['state']=='待重算' and win.states[b]['state']=='已完成'
                assert not win.export_all.isEnabled() if win.current_store==a else True
                win.store_widgets[b]['check'].setChecked(False);win.start_calculation();state['step']=4
            elif step==4 and win.worker is None:
                a,b=state['codes'];assert win.results[a]['summary']['amount']==0;assert win.results[b] is state['other'];state['checks'].append('changing one budget invalidates only that store; zero budget and recompute preserve other store')
                win.navigate(3);snap('07_单店重算后汇总');path=out/'界面导出.xlsx';win.job(batch_service.export_batch,(win.results,win.states,path),lambda p:state.update(exported=p));state['step']=5
            elif step==5 and win.worker is None:
                assert Path(state['exported']).is_file();state['checks'].append('UI worker exports combined workbook')
                from batch_app import BatchWindow
                restored=BatchWindow();assert restored.repository.budgets()[state['codes'][0]]['amount']==0;assert restored.profile['revision']==win.profile['revision'];restored.close()
                state['checks'].append('isolated settings survive fresh window; master path and must-list reusable')
                win.mark_dirty();assert all(s['state']=='待重算' for s in win.states.values());assert not win.batch_export.isEnabled()
                state['checks'].append('shared configuration invalidates all results and blocks export')
                # Isolate the immediate UI transition before any load worker succeeds/fails.
                for item in win.states.values():item['state']='已完成'
                win.input_dirty=False;win.update_controls();assert win.batch_export.isEnabled()
                original_job=win.job;win.job=lambda *args:None;win.start_load();win.job=original_job
                assert win.input_dirty and all(s['state']=='待重算' for s in win.states.values());assert not win.batch_export.isEnabled()
                state['checks'].append('rechecking existing paths invalidates old results before loading, including failure/cancel cases');finish()
        except Exception:finish(traceback.format_exc())
    timer.timeout.connect(tick);timer.start(200)
