"""Opt-in real-data history/working UI acceptance, private launcher DB only."""
import json,time,traceback,sys,pickle,zlib
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QFileDialog,QApplication
from PySide6.QtTest import QTest
from history_ui import ArchiveDialog,AuditDialog
from version_info import diagnostic
import result_semantics as sem,model,engine

def start_smoke(app,win,args):
    out,real=map(Path,args[:2]);checks=[];times={};beats=[];last=[time.perf_counter()];clock=QTimer(win)
    def beat():now=time.perf_counter();beats.append(now-last[0]);last[0]=now
    clock.timeout.connect(beat);clock.start(25)
    def wait(test,seconds=120):
        started=time.monotonic();loop=QEventLoop();timer=QTimer();timer.timeout.connect(lambda:loop.quit() if test() or time.monotonic()-started>seconds else None);timer.start(20);loop.exec();timer.stop()
        if not test():raise TimeoutError('UI condition not reached')
    def idle(reader):return reader.active is None and reader.pending is None
    def shot(w,name):assert w.grab().save(str(out/name))
    def run():
        error=None;dialogs=[]
        try:
            t=time.perf_counter();wait(lambda:idle(win.history_reader) and len(win.archive_all)>=10);times['initial_history']=time.perf_counter()-t
            win.show_section(2);wait(lambda:idle(win.history_reader))
            win.archive_store.setCurrentIndex(win.archive_store.findData('TBBLTT20260612'));win.archive_day.setCurrentIndex(win.archive_day.findData('2026-10-30'));win.archive_search.setText('卫生巾 1715279001171538009');win.refresh_archive();wait(lambda:idle(win.history_reader));assert len(win.archive_orders)==1
            shot(win,'01-history-query.png');checks.append('real archive date + store + two-keyword product query returns exact one order')
            d=ArchiveDialog(win,win.archive_orders[0]);dialogs.append(d);d.show();wait(lambda:d.selected is not None)
            d.search.setText('1715279001171538009');d.filter();assert d.model.rowCount()==1;d.table.selectRow(0);sku=d.model.rows[d.model.indices[0]][0]
            audit=AuditDialog(d,win,d.selected,products=d.model.rows,sku=sku);dialogs.append(audit);audit.show();t=time.perf_counter();wait(lambda:idle(audit.reader) and audit.loaded_key is not None);times['sales_form']=time.perf_counter()-t
            values={r[0]:r[1] for r in audit.model.rows};assert float(values['签发执行量'])==24 and float(values['在途库存'])==24
            shot(audit,'02-selected-sales.png');checks.append('selected SKU inherited without manual entry; old signed 24 and inbound24 remain readable, never recomputed')
            audit.scope.setCurrentText('当时参数');win.refresh_archive();wait(lambda:idle(audit.reader) and audit.loaded_key is not None and idle(win.history_reader));assert len(audit.model.rows)>50
            shot(audit,'03-parameters.png');checks.append('parent refresh cannot cancel child form; parameters load and retain exact historical values')
            audit.scope.setCurrentText('当时商品资料快照');wait(lambda:idle(audit.reader) and audit.loaded_key is not None);assert any('当时选取字段' in r[0] or '旧版混合资料' in r[0] for r in audit.model.rows)
            shot(audit,'04-catalog.png');checks.append('historical catalog snapshot nonempty; absent original file explicitly distinguished from saved fields')
            rows=list(audit.model.rows);save=QFileDialog.getSaveFileName;QFileDialog.getSaveFileName=lambda *a,**k:(str(out/'historical-form.xlsx'),'')
            try:audit.search.setText('不匹配字段');assert audit.model.rowCount()==0;audit.export();wait(lambda:(out/'historical-form.xlsx').exists() and idle(audit.reader))
            finally:QFileDialog.getSaveFileName=save
            import openpyxl
            b=openpyxl.load_workbook(out/'historical-form.xlsx',read_only=True);actual=list(b.active.values)[1:];assert actual==[tuple(v if v!='' else None for v in r) for r in rows];b.close();checks.append('explicit full-form export ignores field display filter as labelled; every cell compared to displayed full form')
            audit.search.clear();audit.product_search.setText('她研社');assert audit.product_model.rowCount()>0
            QTest.mouseClick(audit.products.viewport(),Qt.MouseButton.LeftButton,pos=audit.products.visualRect(audit.product_model.index(0,0)).center());wait(lambda:idle(audit.reader))
            audit.scope.setCurrentText('商品完整计算依据');audit.scope.setCurrentText('商品建议与销量库存');wait(lambda:idle(audit.reader) and audit.loaded_key and audit.loaded_key[0]=='商品建议与销量库存');assert any(r[0]=='近30天销量' for r in audit.model.rows)
            audit.sku.setText('not-in-order');audit.load();assert not audit.export_btn.isEnabled();audit.sku.setText(sku);audit.load();wait(lambda:idle(audit.reader) and audit.export_btn.isEnabled())
            audit.load();audit.reject();assert audit.reader.proc is None and d.selected;d.reject();checks.append('product name selection, rapid tab switch, unknown SKU disables export, retry, close while reading and owned process cleanup')
            order=pickle.loads(zlib.decompress((real/'07.pklz').read_bytes()));r=order['payload']['result'];riskrows=[d for d in r['rows'] if sem.inbound_risk(d)];assert len(riskrows)==3
            # Exercise actual engine for a frozen changed SKU (not only model()).
            one=riskrows[0];loaded={'normalized':[(one,one['_meta'])],'config':r['config'],'stores':{r['store']:r['store_name']},'hashes':r['sources']}
            computed=engine.run_calculation(loaded,r['store'],r['warehouses'],True);assert computed['rows'][0]['AS']==0 and computed['checks']['failures']==[]
            win.loaded=loaded;win.current_store=r['store'];win.result=r;win.results={r['store']:r};win.states={r['store']:{'name':r['store_name'],'state':'已完成','error':''}};win.set_rows(r['rows'],True);win.mode.setCurrentText('在途衔接风险');win.filter_changed();assert win.proxy.rowCount()==3 and all(sem.order_quantity(x)==0 for x in win.visible_rows())
            win.show_section(1);win.pages.setCurrentIndex(4);win.show_detail(win.proxy.index(0,0));assert '不自动追加' in win.details.toPlainText();shot(win,'05-inbound-risk.png');checks.append('actual engine cap integration, three Hongmen warning rows, zero quantities; filter and detail do not refill')
            win.clear_archive_filters();wait(lambda:idle(win.history_reader));assert len(win.archive_orders)>=10;checks.append('clear query restores full archive list')
        except Exception:error=traceback.format_exc()
        finally:
            for dialog in reversed(dialogs):
                try:dialog.reject();dialog.deleteLater()
                except RuntimeError:pass
            clock.stop();win.job_dirty=False;win._discard_close=True;win.close()
            report={'passed':error is None,'error':error,'checks':checks,'times':times,'ui_ticks':len(beats),'max_tick_gap_seconds':max(beats,default=0),'version':diagnostic()};(out/'inbound_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(0 if error is None else 1)
    QTimer.singleShot(0,run)
