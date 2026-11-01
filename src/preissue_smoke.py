"""Opt-in isolated acceptance inside the shipped executable, with real Qt tasks."""
import copy,json,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QMimeData,QUrl,QPointF,Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QDialog,QPushButton


def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];state={};checks=[];ticks=[];last=[time.perf_counter()];timings={}
    def wait(predicate,seconds=90):
        loop=QEventLoop();timer=QTimer();start=time.monotonic();timer.timeout.connect(lambda:loop.quit() if predicate() or time.monotonic()-start>seconds else None);timer.start(15);loop.exec();timer.stop()
        if not predicate():raise AssertionError('UI timeout '+repr(state))
    def drop(widget,path):
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))]);event=QDropEvent(QPointF(20,20),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);widget.dropEvent(event)
    def automate():
        from preissue_ui import FinalListDialog
        from stocking_ui import MappingDialog,ProductPicker
        from import_ui import ImportDialog
        from merchant_selection import MerchantSelection
        from new_product_ui import NewProductDialog
        now=time.perf_counter();ticks.append(now-last[0]);last[0]=now
        d=QApplication.activeModalWidget()
        if d is None:return
        try:
            if isinstance(d,ProductPicker):
                if d.task or d.model is None:return
                d.search.setText(state['pick_sku']);d.model.setData(d.model.index(d.model.positions[state['pick_sku']],0),Qt.CheckState.Checked,Qt.ItemDataRole.CheckStateRole);d.search.setText('不会匹配的关键词')
                if state['pick_sku'] not in d.model.checked or '筛选外1' not in d.count.text():raise AssertionError('picker lost hidden selection')
                d.grab().save(str(out/('picker-'+state['mode']+'.png')))
                if state['mode']=='pick_cancel':d.reject()
                else:
                    d.search.clear();d.grab().save(str(out/'picker-visible-products.png'));d.finish(state['mode']=='pick_preview')
            elif isinstance(d,NewProductDialog):
                choice=next(i for i,(c,q) in enumerate(d.widgets) if c.isEnabled())
                c,q=d.widgets[choice];c.setChecked(True);q.setValue(d.rows[choice]['BD']);d.preview_btn.click()
            elif isinstance(d,ImportDialog) and d.kind=='stocking':drop(d,out/'stocking.xlsx');d.go.click()
            elif isinstance(d,MappingDialog):d.accept()
            elif isinstance(d,FinalListDialog):
                if d.task:return
                mode=state['mode'];i=d.model.positions[(state['store'],state['sku'])]
                if mode=='draft':
                    d.model.reason='暂不补货';d.model.setData(d.model.index(i,9),0);d.grab().save(str(out/'final-pending.png'));d.keep_draft();return
                if mode=='cancel':d.dirty=False;d.reject();return
                if mode=='list' and not any(e.get('kind')=='plan' for e in d.model.edits[state['store']].values()):return
                if not d.prepared:d.preview();return
                if any(p['errors'] for p in d.prepared.values()):raise AssertionError(d.prepared)
                if not d.confirm_all.isChecked():d.confirm_all.setChecked(True)
                d.grab().save(str(out/('final-'+mode+'.png')));d.apply()
            elif isinstance(d,MerchantSelection):d.accept()
            elif isinstance(d,QMessageBox):
                expected={'dailyOverwrite':'覆盖当天记录','deliveryConfirm':'确认签发并导出','newBudgetPreview':'应用到本店草稿'}
                title=d.windowTitle()
                if d.objectName() in expected:next(b for b in d.buttons() if b.text()==expected[d.objectName()]).click()
                elif title=='清单遇到已人工修改商品':next(b for b in d.buttons() if b.text()=='保留人工修改').click()
                elif title in ('导出已签发版本','导出签发单'):d.button(QMessageBox.StandardButton.Yes).click()
                elif title=='签发单已导出':d.accept()
                else:raise AssertionError(title+': '+d.text())
            elif d.windowTitle()=='签发与导出结果':next(b for b in d.findChildren(QPushButton) if b.text()=='继续本次补货').click()
        except Exception:
            state['automation_error']=traceback.format_exc()
            if hasattr(d,'dirty'):d.dirty=False
            d.reject()
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(automate);timer.start()
    def check_amounts(s, name):
        # Independent expected values from final rows; no projection helper.
        from decimal import Decimal
        import normal_exports,result_semantics as sem
        before=copy.deepcopy(win.results);r=win.results[s]
        manual=[d for d in r['rows'] if d.get('_preissue',{}).get('kind')=='manual' and d['_source']=='资料匹配' and sem.work_eligible(d)]
        total=sum((Decimal(str(sem.order_amount(d))) for d in manual),Decimal(0))
        delta=sum((Decimal(str(sem.order_amount(d)))-Decimal(str(d['_preissue']['automatic_amount'])) for d in manual),Decimal(0))
        grouped=normal_exports.groups(r,win.new_focus)
        new=sum((Decimal(str(sem.order_amount(d))) for k in ('本月新品','往期新品') for d in grouped[k] if sem.numeric(sem.order_amount(d))),Decimal(0))
        win.refresh_tables();i=win.overview_codes.index(s)
        if win.overview.item(i,12).text()!=f'{total:,.2f}\n较自动安排 {delta:+,.2f} 元':raise AssertionError('manual money cell differs')
        if win.overview.item(i,13).text()!=f'{new:,.2f}':raise AssertionError('new money cell differs')
        win.navigate(3);win.grab().save(str(out/(name+'-overview.png')))
        win.open_overview(i,12)
        if {d['E'] for d in win.visible_rows()}!={d['E'] for d in manual}:raise AssertionError('manual drilldown loses zero rows')
        if win.detail_review_amounts.values['manual'].text()!=f'{total:,.2f}':raise AssertionError('detail amount differs')
        win.grab().save(str(out/(name+'-detail.png')))
        win.open_overview(i,13)
        if {d['E'] for d in win.visible_rows()}!={d['E'] for k in ('本月新品','往期新品') for d in grouped[k]}:raise AssertionError('new drilldown disagrees with export')
        if win.results!=before:raise AssertionError('money view mutated business result')
        checks.append(name+': amount columns, manual delta, zero-quantity drilldown, new export grouping and nonmutation')
    def finish(error=None):
        timer.stop()
        from version_info import diagnostic
        report={'passed':error is None,'error':error,'phase':phase,'checks':checks,'timings':timings,'max_ui_gap_seconds':max(ticks,default=0),'ui_ticks':len(ticks),'version':diagnostic(),'synthetic':True,'limits':'Automated Qt interaction in actual process; synthetic two-store input, not a human usability study.'}
        (out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(1 if error else 0)
    def exercise():
        try:
            import preissue as rules,result_semantics as sem,work_repository as wr,normal_delivery,stocking_lists
            if phase=='reopen':
                record=win.work_repository.get_job(wr.today());win.restore_job(record)
                expected=json.loads((out/'expected.json').read_text(encoding='utf-8'))
                for s,values in expected.items():
                    r=win.results[s];rules.validate_result(r,win.job_day)
                    if rules.digest(r)!=values['digest']:raise AssertionError('restart changed final result')
                if not stocking_lists.Repository(win.work_repository.path).all():raise AssertionError('lost library')
                from history_ui import AuditDialog
                heads=win.orders.headers();head=next(h for h in heads if h['store'] in expected);head={**head,'warehouse':'河北仓'}
                sku=next(iter(win.results[head['store']]['preissue']['edits']))
                d=AuditDialog(win,win,head,sku=sku);d.show();d.scope.setCurrentText('签发前调整');wait(lambda:bool(d.loaded_key));d.grab().save(str(out/'historical-adjustment.png'))
                if not any('最终签发量' in str(r) for r in d.model.rows):raise AssertionError('adjustment form empty')
                d.reject();d.deleteLater();checks.append('fresh process restored final edits, reusable list and historical adjustment business form')
                selected=next((item for value in win.results[head['store']]['preissue']['lists'].values() for item in value['items'] if item.get('selection_sources')),None)
                if not selected:raise AssertionError('query selection snapshot missing after restart')
                query_wh=next(r['AU'] for r in win.results[head['store']]['rows'] if r['E']==selected['sku'])
                d=AuditDialog(win,win,{**head,'warehouse':query_wh},sku=selected['sku']);d.show();d.scope.setCurrentText('签发前调整');wait(lambda:bool(d.loaded_key))
                if '搜索选商品' not in str(d.model.rows):raise AssertionError('history query provenance is not visible')
                d.grab().save(str(out/'historical-query-source.png'));d.reject();d.deleteLater();checks.append('query-selected SKU, catalog revision and source rows visible after packaged restart');check_amounts(head['store'],'money-restored');finish();return
            win.show_section(1);start=time.perf_counter();drop(win.sales,out/'sales.xlsx');wait(lambda:win.worker is None and win.loaded is not None);timings['check_seconds']=time.perf_counter()-start
            start=time.perf_counter();win.calculate_btn.click();wait(lambda:win.worker is None and len(win.results)==2);timings['calculate_seconds']=time.perf_counter()-start
            if any(st['state']!='已完成' for st in win.states.values()):raise AssertionError(win.states)
            # Existing fixture: Hebei has child_missing with stock zero; Shandong
            # has no entry. Exercise the real packaged spawn calculation path.
            for store,result in win.results.items():
                rows={d['E']:d for d in result['rows']};d=rows['child_missing']
                if d['AU']!='河北仓' or d['_source']!='资料匹配' or d['V']!=2 or d['BD']!=1 or sem.order_quantity(d)<=0:raise AssertionError(('warehouse fallback',store,d))
                if not d['_meta']['product_file'].endswith('河北仓.xlsx'):raise AssertionError('fallback source lost')
                if rows['local']['_source']!='地采商品' or sem.order_quantity(rows['both_disabled'])!=0 or sem.order_quantity(rows['stopped'])!=0:raise AssertionError('fallback changed exclusion/local rules')
            checks.append('actual spawn: secondary missing retains primary catalog, price, carton and demand; disabled/stopped/local boundaries preserved')
            import new_products
            new_store=list(win.results)[-1];win.current_store=new_store
            if win.worker or win.readonly or win.input_dirty:raise AssertionError(('new selection unavailable',bool(win.worker),win.readonly,win.input_dirty))
            win.choose_new_products(True);wait(lambda:win.worker is None)
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if not any(sem.order_amount(d)>0 for d in win.results[new_store]['rows'] if new_products.is_new(d) and sem.numeric(sem.order_amount(d))):raise AssertionError('positive new selection did not apply')
            check_amounts(new_store,'money-new-applied')
            base=copy.deepcopy(win.results);s=next(iter(win.results));r=win.results[s]
            eligible=[d for d in r['rows'] if not rules.blocked(d) and sem.order_quantity(d)>0 and d['K']==0 and d['L']==0]
            if not eligible:raise AssertionError('fixture lacks dual zero')
            sku=eligible[0]['E'];state.update(store=s,sku=sku,mode='cancel');win.current_store=s
            win.open_final_list(True)
            if win.results!=base:raise AssertionError('cancel changed results')
            state['mode']='draft';win.open_final_list(True)
            if s not in win.preissue_drafts or s not in win.work_repository.get_job(wr.today())['payload']['preissue_drafts']:raise AssertionError('draft not persisted')
            try:normal_delivery.prepare_batch(win.orders,win.snapshot(),[s])
            except ValueError as exc:
                if '待核对' not in str(exc):raise
            else:raise AssertionError('pending signed')
            state['mode']='apply';start=time.perf_counter();win.open_final_list(True);timings['manual_preview_apply_seconds']=time.perf_counter()-start
            if sem.order_quantity(next(d for d in win.results[s]['rows'] if d['E']==sku))!=0:raise AssertionError('zero not final')
            if rules.original(win.results[s])!=base[s]:raise AssertionError('model/automatic changed')
            checks.append('actual final-list cancel, pending draft saved, signing pending rejected, zero risk checkbox and atomic apply; original result exactly restorable')
            check_amounts(s,'money-manual-applied')
            other=next(d for d in r['rows'] if d['E']!=sku and not rules.blocked(d))
            import openpyxl
            b=openpyxl.Workbook();ws=b.active;ws.append(['目标持有量','商品名称','商品编码']);ws.append([float(rules.holding(other))+sem.order_quantity(other)+other['BD'],other['D'],other['E']]);b.save(out/'stocking.xlsx');b.close()
            from stocking_ui import StockingDialog
            lib=StockingDialog(win);lib.show();lib.import_files();wait(lambda:lib.task is None and lib.current is not None)
            lib.name.setText('EXE合成计划');lib.kind.setCurrentIndex(lib.kind.findData('plan'));lib.strategy.setCurrentIndex(lib.strategy.findData('target'))
            for c in lib.scopes.values():c.setChecked(True)
            lib.save(False);wait(lambda:lib.task is None)
            if not lib.current.get('version'):raise AssertionError(lib.message.text())
            lib.grab().save(str(out/'stocking-library.png'));lib.use();wait(lambda:lib.task is None)
            if not lib.selected:raise AssertionError(lib.message.text())
            listing=lib.selected;lib.deleteLater();state['mode']='list';win.current_store=s;win.open_final_list(True,listing)
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if other['E'] not in win.results[s]['preissue']['edits']:raise AssertionError('list did not apply')
            for store,r0 in base.items():
                if store!=s and r0!=win.results[store]:raise AssertionError('other store changed')
                if store==s:
                    edits=win.results[store]['preissue']['edits']
                    for old,new in zip(r0['rows'],win.results[store]['rows']):
                        if old['E'] not in edits and old!=new:raise AssertionError('unselected row changed')
            checks.append('reordered-header list drag/drop, explicit mapping, reusable save, target gap fill, manual zero protected, unselected rows/stores unchanged')
            # Query path: cancellation is read-only; direct preview needs no saved
            # list. Then append to a file list and save a new reusable version.
            pick=next(d for d in r['rows'] if d['E'] not in {sku,other['E']} and not rules.blocked(d))
            state.update(pick_sku=pick['E'],mode='pick_cancel');before=copy.deepcopy(win.results)
            lib=StockingDialog(win,codes=[s]);lib.show();lib.search_products()
            if lib.current is not None or win.results!=before:raise AssertionError('cancel selection mutated draft')
            state['mode']='pick_preview';lib.kind.setCurrentIndex(lib.kind.findData('plan'));lib.search_products();wait(lambda:lib.task is None)
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if not lib.selected or lib.selected.get('version') or lib.selected['sources']:raise AssertionError('direct query preview forced save/file: '+lib.message.text())
            checks.append('actual search/select keeps hidden checks, cancels without side effects, direct preview creates temporary list without Excel or mandatory save');lib.deleteLater()
            lib=StockingDialog(win,codes=[s]);lib.show();saved=lib.repository.all()[0];old_items=copy.deepcopy(saved['items']);lib.show_value(saved);lib.target.setText('1')
            state['mode']='pick_append';lib.search_products()
            if lib.current['items'][:len(old_items)]!=old_items:raise AssertionError('query append overwrote existing file targets')
            new_index=next(i for i,x in enumerate(lib.current['items']) if x['sku']==pick['E']);lib.table.item(new_index,2).setText(str(float(rules.holding(pick))+sem.order_quantity(pick)+pick['BD']))
            lib.save(True);wait(lambda:lib.task is None)
            if lib.current.get('version')!=2:raise AssertionError('query append revision save failed: '+lib.message.text())
            lib.grab().save(str(out/'query-merged-library.png'));lib.use();wait(lambda:lib.task is None)
            listing=lib.selected
            if not listing:raise AssertionError('query list did not preview')
            lib.deleteLater();state['mode']='list';win.open_final_list(True,listing)
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if pick['E'] not in win.results[s]['preissue']['edits']:raise AssertionError('query selected product not applied')
            if rules.original(win.results[s])!=base[s]:raise AssertionError('query changed model/automatic data')
            for store,original in base.items():
                if store!=s and win.results[store]!=original:raise AssertionError('query changed unselected store')
            checks.append('query append preserves file targets, saves V2, applies existing carton/budget/risk preview and keeps original model/other store intact')
            start=time.perf_counter();win.export_current();timings['sign_export_seconds']=time.perf_counter()-start
            if state.get('automation_error'):raise AssertionError(state['automation_error'])
            if not list((out/'exports').glob('*.xlsx')):raise AssertionError('no signed files')
            import openpyxl
            exported=0
            for path in (out/'exports').glob('*.xlsx'):
                book=openpyxl.load_workbook(path,read_only=True,data_only=True)
                try:
                    for sheet in book:
                        values=sheet.values;headers=next(values)
                        if '商品编码' not in headers:continue
                        for row in values:
                            if row[headers.index('商品编码')]=='child_missing':
                                if row[headers.index('所属仓')]!='河北仓':raise AssertionError('fallback exported to wrong warehouse')
                                exported+=1
                finally:book.close()
            if not exported:raise AssertionError('fallback missing from signed export')
            checks.append('actual split-warehouse signed Excel includes restored product only under Hebei')
            orders=[h for h in win.orders.headers() if h['store']==s]
            if not orders:raise AssertionError('no signed order')
            order=win.orders.get(win.job_day,s);rules.validate_result(order['payload']['result'],win.job_day)
            if order['payload']['result']!=win.results[s]:raise AssertionError('signed final mismatch')
            checks.append('actual spawn work I/O confirms/signs split-warehouse Excel and stores final quantity plus original audit')
            (out/'expected.json').write_text(json.dumps({s:{'digest':rules.digest(win.results[s])}}),encoding='utf-8')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(600,exercise)
