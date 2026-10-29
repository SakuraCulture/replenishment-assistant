"""Opt-in native UI acceptance for explicitly labelled synthetic warehouse fixtures."""
import json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton
import batch_service,engine
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,out=map(Path,args[:2]);out.mkdir(parents=True,exist_ok=True)
    state={'step':0,'start':time.monotonic(),'checks':[]};timer=QTimer(win)
    def finish(error=None):
        timer.stop();meta=diagnostic();report={'passed':error is None,'error':error,'version':meta['version'],'source_commit':meta['source_commit'],'checks':state['checks']}
        (out/'warehouse_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(1 if error else 0)
    def failed(message):
        if state.get('expected_failure'):state['expected_failure']=False;state['failure_seen']=message
        else:finish(message)
    win.failed=failed;sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    def snap(name):win.repaint();assert win.grab().save(str(out/(name+'.png')))
    def choose(store,primary,secondary):
        for key,value in [('primary',primary),('secondary',secondary)]:
            c=win.store_widgets[store][key];c.setCurrentIndex(c.findData(value))
    def tick():
        try:
            if time.monotonic()-state['start']>150:raise TimeoutError(str(state))
            step=state['step']
            if step==0:
                assert not win.master_legacy.isChecked();assert not win.catalogs
                win.import_catalog_path(fixture/'河北仓.xlsx','河北仓');state['step']=1
            elif step==1 and win.worker is None:
                assert '河北仓' in win.catalogs;win.import_catalog_path(fixture/'山东仓.xlsx','山东仓');state['step']=2
            elif step==2 and win.worker is None:
                assert len(win.catalogs)==2;win.sales.add_paths([str(fixture/'sales.xlsx')]);snap('01_分仓资料');QTest.mouseClick(win.load_btn,Qt.MouseButton.LeftButton);state['step']=3
            elif step==3 and win.worker is None:
                assert win.loaded and len(win.store_widgets)==2;win.navigate(1)
                for w in win.store_widgets.values():w['mode'].setCurrentIndex(1)
                assert not win.calculate_btn.isEnabled()
                choose('QA_A','河北仓','河北仓');choose('QA_B','山东仓','河北仓');assert not win.calculate_btn.isEnabled()
                choose('QA_A','河北仓','山东仓');assert win.calculate_btn.isEnabled();snap('02_主从仓设置')
                state['checks'].append('separate import workers, explicit warehouse labels, missing/identical primary-secondary blocked')
                QTest.mouseClick(win.calculate_btn,Qt.MouseButton.LeftButton);state['step']=4
            elif step==4 and win.worker is None:
                assert all(s['state']=='已完成' for s in win.states.values()),win.states
                a={d['E']:d for d in win.results['QA_A']['rows']};b={d['E']:d for d in win.results['QA_B']['rows']}
                assert a['both']['AU']=='河北仓' and b['both']['AU']=='山东仓'
                assert a['both_empty']['AU']=='山东仓' and engine.order_quantity(a['both_empty'])>0
                assert engine.order_quantity(a['both_disabled'])==0
                win.navigate(3);snap('03_需求结果');win.open_overview(0,0)
                # Open an actual zero-stock ownership detail using the visible proxy row.
                win.search.setText('both_empty');assert win.proxy.rowCount()==1;win.show_detail(win.proxy.index(0,0))
                assert '需求归属仓' in win.details.toPlainText() and '从仓无货仍保留需求' in win.details.toPlainText()
                state['checks'].append('packaged process computation uses per-store priority, zero-stock secondary demand survives; disabled excluded')
                state['step']=41
            elif step==41:
                snap('04_双仓无货仍有需求')
                win.resize(1080,720);win.navigate(0);state['step']=5
            elif step==5:
                snap('05_小窗口导入');win.navigate(1);state['step']=6
            elif step==6:
                snap('06_小窗口主从仓');win.resize(1380,900)
                state['other']=win.results['QA_B'];choose('QA_A','河北仓',None)
                assert win.states['QA_A']['state']=='待重算' and win.states['QA_B']['state']=='已完成'
                win.store_widgets['QA_B']['check'].setChecked(False);win.start_calculation();state['step']=7
            elif step==7 and win.worker is None:
                assert win.results['QA_B'] is state['other']
                a={d['E']:d for d in win.results['QA_A']['rows']};assert a['both_empty']['AU']=='河北仓' and engine.order_quantity(a['both_empty'])>0
                win.job(batch_service.export_batch,(win.results,win.states,out/'界面分仓导出.xlsx'),lambda p:state.update(exported=p));state['step']=8
            elif step==8 and win.worker is None:
                assert Path(state['exported']).is_file()
                from batch_app import BatchWindow
                restored=BatchWindow();assert len(restored.catalogs)==2;assert restored.catalog_repository.routes()['QA_A']['secondary'] is None;restored.close()
                state['checks'].append('single-store supply change invalidates/recomputes only that store; exported results and persisted catalogs/routes verified')
                state['old']=win.catalogs['河北仓']['revision'];state['expected_failure']=True
                win.import_catalog_path(fixture/'duplicate.xlsx','河北仓');state['step']=9
            elif step==9 and win.worker is None:
                assert state.get('failure_seen') and win.catalogs['河北仓']['revision']==state['old']
                assert win.states['QA_B']['state']=='已完成'
                win.import_catalog_path(fixture/'河北仓.xlsx','河北仓');state['step']=10
            elif step==10 and win.worker is None:
                assert win.input_dirty and all(s['state']=='待重算' for s in win.states.values()) and not win.batch_export.isEnabled()
                state['checks'].append('failed catalog import preserves previous snapshot/results; successful update invalidates input/results and blocks stale export');finish()
        except Exception:finish(traceback.format_exc())
    timer.timeout.connect(tick);timer.start(250)
