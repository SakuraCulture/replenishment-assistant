"""Opt-in isolated actual-application acceptance; never opens platform browsers."""
import copy,json,os,sys,time,traceback
from pathlib import Path
import openpyxl
from PySide6.QtCore import QTimer,Qt,QMimeData,QUrl,QPoint,QPointF
from PySide6.QtGui import QDragEnterEvent,QDropEvent
from PySide6.QtWidgets import QApplication,QDialog,QPushButton
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    fixture,out=map(lambda p:Path(p).resolve(),args[:2]);checks=[];beats=[];errors=[]
    def require(ok,message):
        if not ok:raise AssertionError(message)
    def click(dialog,text):
        targets=[b for b in dialog.findChildren(QPushButton) if b.text()==text]
        require(len(targets)==1,'button not unique: '+text);require(targets[0].isEnabled(),'button disabled: '+text);QTest.mouseClick(targets[0],Qt.MouseButton.LeftButton)
    def confirm_later(title,text):
        started=time.monotonic()
        def attempt():
            d=app.activeModalWidget()
            if d is not None and d.windowTitle()==title:click(d,text);return
            if time.monotonic()-started>10:
                errors.append('confirmation not found: '+title)
                if d:d.reject()
                return
            QTimer.singleShot(40,attempt)
        QTimer.singleShot(40,attempt)
    def wait(predicate,seconds=45):
        deadline=time.monotonic()+seconds
        while not predicate() and time.monotonic()<deadline:app.processEvents();QTest.qWait(20)
        require(predicate(),'background operation timed out')
    def drop(d,path):
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
        enter=QDragEnterEvent(QPoint(10,10),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);d.dragEnterEvent(enter)
        event=QDropEvent(QPointF(10,10),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);d.dropEvent(event)
        require(enter.isAccepted() and event.isAccepted(),'drag/drop was not accepted')
    def execute():
        heartbeat=QTimer(win);heartbeat.setInterval(25);heartbeat.timeout.connect(lambda:beats.append(time.monotonic()));heartbeat.start()
        try:
            require(Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()==out/'settings','isolated settings required');out.mkdir(parents=True,exist_ok=True)
            win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
            from business_ui import run
            import warehouse_catalog as wc,batch_service as bs,parameter_schema as ps,replenishment_plans as plans,new_product_focus as focus,admission,result_semantics as sem,work_repository as wr
            copied=out/'inputs';copied.mkdir();catalogs={}
            for wh in wc.WAREHOUSES:
                b=openpyxl.load_workbook(fixture/(wh+'.xlsx'));s=b.active;headers=[c.value for c in s[1]];tag=len(headers)+1;s.cell(1,tag,'商品标签')
                for rn in range(2,s.max_row+1):s.cell(rn,tag,'合成验收,常规商品')
                p=copied/(wh+'.xlsx');b.save(p);b.close();catalogs[wh]=wc.import_catalog(p,wh);win.catalog_repository.save(catalogs[wh])
            loaded=run(win,bs.load,([fixture/'sales.xlsx'],catalogs),'验收：检查两店资料')
            require(len(loaded['stores'])>=2,'two-store fixture required');codes=list(loaded['stores']);a,b=codes[:2]
            win.catalogs=catalogs;win.profile=json.loads((fixture/'must.json').read_text('utf-8'));win.loaded_ok(loaded);win.new_focus=focus.empty(wr.today()[:7])
            for s in codes:win.set_store_ui(s,{**win.store_ui(s),'mode':1,'primary':'河北仓','secondary':'山东仓'})
            win.sales.paths=[str(fixture/'sales.xlsx')];win.sales.refresh();win.auto_check.stop()
            # Real Qt template drop, background read and explicit current-only preview.
            import parameter_tables as pt,store_parameter_import as spi,store_parameter_ui as pui
            from import_ui import ImportDialog
            p=out/'周期模板.xlsx';pt.write(p);book=openpyxl.load_workbook(p);book['分仓周期'].append([a,loaded['stores'][a],'山东仓',9,3]);book.save(p);book.close()
            picker=ImportDialog(win,'warehouse_cycles');drop(picker,p);require(picker.paths==[str(p)],'dropped file not selected');picker.reject()
            data=run(win,spi.read,([str(p)],),'验收：参数读取');captured=pui.capture(win,data);before=win.work_repository.stores()
            preview=pui.Preview(win,win,data,captured,'current');preview.show();app.processEvents();preview.grab().save(str(out/'01_参数差异.png'));QTest.keyClick(preview,Qt.Key.Key_Escape)
            require(win.work_repository.stores()==before,'cancel wrote defaults')
            preview=pui.Preview(win,win,data,captured,'current');preview.show();app.processEvents();QTest.mouseClick(preview.go,Qt.MouseButton.LeftButton);pui.commit(win,data,captured,'current',preview.prepared)
            require(win.config_for(a)['warehouses']['山东仓']==[9,3] or tuple(win.config_for(a)['warehouses']['山东仓'])==(9,3),'parameter import failed');require(win.work_repository.stores()==before,'current-only edit changed defaults')
            checks.append('Qt drag/drop, new template, background read, preview cancel and current-only application')
            # Save owned plan through its actual editor, then explicitly adopt it.
            from replenishment_plan_ui import Editor,Library
            editor=Editor(win);editor.name.setText('验收线路');editor.config=copy.deepcopy(win.config_for(a));editor.show();app.processEvents();click(editor,'保存补货计划');require(editor.result()==QDialog.DialogCode.Accepted,'plan save failed');saved=editor.saved
            library=Library(win,True);library.show();app.processEvents();confirm_later('采用计划并核对替换范围','采用到本次作业');click(library,'采用到本次作业')
            require(win.active_plan['id']==saved['id'],'plan adoption failed');require(all(win.config_for(s)==saved['config'] for s in codes),'plan config not active');win.show_section(1);win.navigate(0);app.processEvents();win.grab().save(str(out/'02_计划准备页.png'))
            win.loaded_ok(copy.deepcopy(loaded));require(win.active_plan==saved and win.config_for(a)==saved['config'],'recheck lost plan');checks.append('plan editor save/adoption, explicit scope review, ordered stores and owned preset survive recheck')
            # Actual spawn calculation. Gate facts in this tiny copied fixture are synthetic.
            win.concurrency.setValue(2);win.start_calculation(codes);wait(lambda:win.worker is None)
            require(all(win.states[s]['state']=='已完成' for s in codes),str(win.states));before_rows=copy.deepcopy(win.results)
            target=next(d for d in win.results[a]['rows'] if d.get('_source')=='资料匹配' and sem.orderable(d));sku=target['E']
            from admission_ui import Editor as AdmissionEditor
            e=AdmissionEditor(win,win,a);e.state.setCurrentText('已核验');e.qfields['reviewer'].setText('QA合成核验');e.qfields['verified_on'].setText(wr.today());e.value.setdefault('refusals',{})[sku]={'name':target['D'],'reason':'QA门店拒收','active':True,'expires':None};e.refresh();e.show();app.processEvents();e.grab().save(str(out/'03_门店准入.png'));confirm_later('核对本店准入变化','保存本店准入');click(e,'核对并保存本店准入')
            require(win.states[a]['state']=='待重算' and win.states[b]['state']=='已完成','wrong invalidation scope')
            win.start_calculation([a]);wait(lambda:win.worker is None);require(win.states[a]['state']=='已完成',str(win.states[a]))
            after=next(d for d in win.results[a]['rows'] if d['E']==sku);require(after['AS']==target['AS'] and sem.order_quantity(after)==0,'guard modified forecast or failed to exclude');require(win.results[b]==before_rows[b],'unrelated store changed')
            checks.append('frozen executable spawn, two-store calculation, qualification/refusal form, affected-store-only invalidation/recalculate, AS preserved/execution zero')
            # Save/load/sign via the same spawned work-I/O protocol and real progress UI.
            from ui_work_io import Session,request,read_job
            with Session(win.work_repository.path) as session:
                request(win,session,'prepare_save',win.snapshot());request(win,session,'save',None)
            record=read_job(win,win.work_repository.path,wr.today());win.restore_job(record,owned=True);require(win.active_plan==saved,'saved draft lost plan');require(win.results[b]==before_rows[b],'save/restore changed result')
            with Session(win.work_repository.path) as session:
                request(win,session,'prepare_normal',(win.snapshot(),codes));signed=request(win,session,'confirm_normal',None)
            require(len(signed['orders'])==len(codes),'sign count differs');checks.append('actual spawn progress dialogs: save, restore and atomic signature with plan/config/admission snapshots')
            import lifecycle_batch as lb,order_lifecycle as lc,normal_exports as ne,order_identity
            heads=run(win,lc.current_targets,(win.work_repository.path,),'验收：读取签发单');folder=out/'signed';folder.mkdir();files=run(win,lb.export_signed,(win.work_repository.path,heads,folder),'验收：按仓导出签发单')
            for path in files:
                book=openpyxl.load_workbook(path);require(book[order_identity.SHEET].sheet_state=='veryHidden','identity not hidden');require('补货单号' not in [c.value for c in book.worksheets[0][1]],'merchant detail still contains order-number column');book.close()
            from lifecycle_batch_ui import BatchImport
            dlg=BatchImport(win,win,'return');dlg.show();app.processEvents();drop(dlg,files[0]);require(not dlg.issues and dlg.documents,'returned file recognition failed');click(dlg,'核对并准备保存');require(dlg.review is not None,dlg.note.text());dlg.grab().save(str(out/'04_回传差异.png'));dlg.reject()
            require(not lc.snapshot(win.work_repository.path,heads[0])['present']['return'],'preview cancel wrote lifecycle')
            dlg=BatchImport(win,win,'return');dlg.add_paths(files);require(not dlg.issues,str(dlg.issues));dlg.show();app.processEvents();click(dlg,'核对并准备保存');require(dlg.review is not None,dlg.note.text());app.processEvents();QTest.mouseClick(dlg.review.complete,Qt.MouseButton.LeftButton,pos=QPoint(8,dlg.review.complete.height()//2));require(dlg.review.save.isEnabled(),'full-order acknowledgement required');QTest.mouseClick(dlg.review.save,Qt.MouseButton.LeftButton);require(dlg.receipt,dlg.note.text())
            checks.append('actual per-warehouse signed export; hidden metadata; multi-order return import, full difference preview/cancel and atomic commit')
            # Remove both identity sources from a private copy; bind explicitly in UI.
            unbound=out/'无号回传.xlsx';book=openpyxl.load_workbook(files[0]);del book[order_identity.SHEET]
            for prop in list(book.custom_doc_props.names):del book.custom_doc_props[prop]
            book.save(unbound);book.close();dlg=BatchImport(win,win,'return');dlg.add_paths([unbound]);dlg.show();app.processEvents();require(dlg.targets[0].currentData() is None,'missing identity silently bound')
            target=next(h for h in heads if h['store']==dlg.documents[0]['scan']['rows'][0]['store'] and h['warehouse']==dlg.documents[0]['scan']['rows'][0]['warehouse']);combo=dlg.targets[0];idx=next(i for i in range(1,combo.count()) if combo.itemData(i)['number']==target['number']);combo.setCurrentIndex(idx);click(dlg,'核对并准备保存');require(dlg.review is not None,dlg.note.text());dlg.grab().save(str(out/'05_缺号明确绑定.png'));dlg.reject();checks.append('missing hidden identity: no silent guess; explicit target selected in same import workspace; cancel preserves previous return')
            require(not errors,str(errors));heartbeat.stop();intervals=[b-a for a,b in zip(beats,beats[1:])]
            from version_info import diagnostic
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'heartbeat':{'interval_ms':25,'ticks':len(beats),'max_gap_seconds':max(intervals,default=0),'scope':'entire scripted walkthrough including synchronous fixture creation/screenshots; not an independent human usability result'},**diagnostic()}
        except Exception:
            heartbeat.stop();report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'error':traceback.format_exc(),'dialog_errors':errors}
        (out/'exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8');win._discard_close=True
        app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(200,execute)
