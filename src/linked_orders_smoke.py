"""Real Qt flow for a numbered payment, two platform IDs, and receipt history."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import time
import traceback


def workbook(path,head,stage,qty,number):
    import lifecycle_io as io
    from openpyxl import Workbook
    book=Workbook();sheet=book.active;sheet.title='业务明细';columns=io.fields(stage);sheet.append(columns)
    record={'补货单号':head['number'],'门店编码':head['store'],'供货仓':head['warehouse'],
            '商品编码':next(iter(head['products'])),'数量单位':'库存单位',io.QUANTITIES[stage]:qty,
            io.DOCNUM[stage]:number,io.DOCDATE[stage]:head['day'],'实际金额':7.25,'币种':'CNY'}
    sheet.append([record.get(column) for column in columns]);book.save(path);book.close()
    return path


def seed_purchase(path,head,payment,id,number,**basis_changes):
    file=path.parent/'aoxiang/component-state/state.sqlite';file.parent.mkdir(parents=True,exist_ok=True)
    basis={'oid':head['oid'],'revision':head['revision'],'signedDigest':head['digest'],'number':head['number'],
           'store':head['store'],'warehouse':head['warehouse'],'paymentVersion':payment['version'],'paymentDigest':payment['digest'],**basis_changes}
    value={'id':id,'state':'BOUND','intent':{'basis':basis},'binding':{'orderId':number,'status':'待提交'}}
    with closing(sqlite3.connect(file)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS purchase_creates(id TEXT PRIMARY KEY,state TEXT NOT NULL,platform_order TEXT,payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS purchase_receipts(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO purchase_creates VALUES(?,?,?,?)',(id,'BOUND',number,json.dumps(value,ensure_ascii=False)))
    return file


def start_smoke(app,win,args):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication,QMessageBox
    from PySide6.QtTest import QTest
    from lifecycle_batch_ui import BatchImport
    from lifecycle_ui import Document
    from jst_results_ui import ResultsPage
    from signing_memory_fixture import fixture
    from version_info import diagnostic
    import lifecycle_batch,order_lifecycle,payment_references,result_semantics

    out=Path(args[0]).resolve();spec=json.loads((out/'synthetic.json').read_text('utf-8'))
    path=win.work_repository.path.resolve()
    if spec.get('synthetic') is not True or spec.get('live_platform') is not False or path.parent!=out/'settings':
        raise ValueError('订单跟踪验收须使用隔离资料目录')
    win.auto_check.stop();win.jst_generator.timer.stop();win.aoxiang_workflows.timer.stop()

    def exercise():
        checks=[];report={'passed':False,'live_platform':False,'actual_exe':bool(getattr(sys,'frozen',False)),'checks':checks}
        panel=win.followup
        dialogs=[]
        unexpected=[];warnings=QTimer(win);warnings.setInterval(40)
        def dismiss_warning():
            dialog=QApplication.activeModalWidget()
            if isinstance(dialog,QMessageBox):
                unexpected.append(dialog.windowTitle()+' '+dialog.text()+' '+dialog.informativeText());dialog.reject()
        warnings.timeout.connect(dismiss_warning);warnings.start()
        try:
            payload,codes=fixture(5,2,0);code=codes[0]
            for index,row in enumerate(payload['results'][codes[1]]['rows']):
                row.update(A=payload['results'][code]['store_name'],B=code,E=f'00000000{index}')
                payload['results'][code]['rows'].append(row)
            payload['results'][code]['summary']=result_semantics.summarize(payload['results'][code]['rows'])
            payload['results'].pop(codes[1]);payload['states'].pop(codes[1])
            win.orders.confirm(payload,[code],None,{code:None})
            heads=order_lifecycle.current_targets(path);assert len(heads)==2 and heads[0]['oid']==heads[1]['oid'],heads
            head=next(h for h in heads if h['warehouse']=='河北仓')

            def save(stage,quantity,number):
                name=workbook(out/(stage+'.xlsx'),head,stage,quantity,number)
                d=lifecycle_batch.read([name],stage)['documents'][0]
                d['target']=head;order_lifecycle.commit(path,lifecycle_batch.prepare(path,[d],[head]))
            save('return',6,'RETURN');save('operations',6,'SIGNED')
            paid=workbook(out/'payment.xlsx',head,'payment',5,'local-payment-reference')
            importer=BatchImport(win,win,'payment');importer.add_paths([paid]);dialogs.append(importer)
            assert importer.read_ready and not importer.issues,importer.note.text()
            assert [h['number'] for h in importer.heads]==[head['number']],importer.heads
            assert importer.targets[0].currentData()['number']==head['number']
            importer.preview();assert importer.review is not None and importer.pages.currentWidget() is importer.review,unexpected
            assert not importer.review.save.isEnabled()
            importer.review.complete.setChecked(True);importer.review.save.click()
            assert importer.receipt and importer.receipt[0]['number']==head['number'],unexpected
            checks.append('实际标准付款单导入仅读目标仓单，原核对与确认保存正常')
            snapshot=order_lifecycle.snapshot(path,head)
            payment_references.save(path,payment_references.read(path,head),'00040902')
            file=seed_purchase(path,head,snapshot['documents']['payment'],'first','PO-A000030')
            seed_purchase(path,head,snapshot['documents']['payment'],'second','PO-B000030')
            seed_purchase(path,head,snapshot['documents']['payment'],'retired','PO-RECREATED',oid='recreated-order')
            before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (path,file)}
            win.show_section(3);win.tracking_tabs.setCurrentIndex(0)
            panel.start.setText(head['day']);panel.end.setText(head['day'])
            def search(needle,count):
                panel.search.setText(needle);panel.query();deadline=time.monotonic()+12
                while (not panel.table.isEnabled() or (getattr(panel,'applied_request',None) or {}).get('search')!=needle) and time.monotonic()<deadline:
                    QTest.qWait(20)
                assert panel.table.isEnabled() and (panel.applied_request or {}).get('search')==needle,panel.note.text()
                assert panel.table.rowCount()==count,panel.note.text()
            search('00040902',1);search('B000030',1);search('PO-RECREATED',0);search('local-payment-reference',0)
            search('PO-A000030',1)
            assert [panel.table.horizontalHeaderItem(i).text() for i in range(7,10)]==['聚水潭内部订单号','翱象采购单号','收货状态']
            assert panel.table.item(0,7).text()=='00040902' and panel.table.item(0,8).text()=='PO-A000030、PO-B000030'
            checks.append('两种单号和第二个采购号均可查，原仓身份失效或普通付款单号不串绑')

            jst=ResultsPage(win,heads,win)
            try:
                deadline=time.monotonic()+12
                while jst.data is None and time.monotonic()<deadline:QTest.qWait(20)
                assert jst.data is not None,jst.status.text()
                for number in ('00040902','B000030'):
                    jst.search.setText(number)
                    assert [r['head']['number'] for r in jst.visible_records()]==[head['number']]
            finally:jst.shutdown();jst.deleteLater()
            checks.append('聚水潭结果页用双平台单号均可定位对应仓单')

            pending=[];modal=QTimer(win);modal.setInterval(40)
            def inspect_status():
                dialog=QApplication.activeModalWidget()
                if isinstance(dialog,QMessageBox):
                    pending.append((dialog.text(),dialog.informativeText(),[b.text() for b in dialog.buttons()]))
                    dialog.reject()
            modal.timeout.connect(inspect_status);warnings.stop();modal.start()
            try:panel.table.cellWidget(0,9).click()
            finally:modal.stop();modal.deleteLater();warnings.start()
            assert len(pending)==1 and pending[0][0]=='待提交' and 'PO-A000030' in pending[0][1],pending
            assert '查看 / 同步翱象收货' in pending[0][2],pending
            assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in before.items())
            checks.append('尚无正式实收时先看本地状态，明确选择后才进入收货同步')

            save('receipt',4,'SIGNED-RECEIPT')
            before[path]=hashlib.sha256(path.read_bytes()).hexdigest()
            search('PO-A000030',1)
            status=panel.table.cellWidget(0,9);assert status.text()=='实收已归档'

            viewed=[];modal=QTimer(win);modal.setInterval(40)
            def inspect_document():
                dialog=QApplication.activeModalWidget()
                if isinstance(dialog,Document):
                    doc=dialog.versions.currentData();record=next(row for row in doc['payload']['rows'] if row['document_number']=='SIGNED-RECEIPT')
                    viewed.append((record['base_quantity'],dialog.files.count()));dialog.reject()
                elif isinstance(dialog,QMessageBox):viewed.append(dialog.text());dialog.reject()
            modal.timeout.connect(inspect_document);modal.start()
            try:status.click()
            finally:modal.stop();modal.deleteLater()
            assert viewed==[('4',1)],(viewed,unexpected)
            assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in before.items())
            checks.append('点击收货状态进入正式实收版本，读取数量及原件，业务库与平台归档库字节保持一致')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        finally:
            warnings.stop();warnings.deleteLater()
            for d in dialogs:d.close();d.deleteLater()
            version=diagnostic();report['version']={k:version[k] for k in ('version','build_id','source_commit')}
            (out/'linked_orders.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.job_dirty=False;win._discard_close=True;win.close()
    QTimer.singleShot(300,exercise)
