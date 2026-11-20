"""Run one same-original synthetic flow through browser, parser, Qt and storage.

Never opens a real account or signs/confirms a real order. The only browser
surface is a private localhost fixture with an empty browser context.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/name) for name in ('src','tests','tools')]
import openpyxl
import jst_auto_order as automatic
import jst_fulfillment as fulfillment
import order_lifecycle as lifecycle
import work_repository as wr


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')


def reopen(case):
    marker=json.loads((case/'synthetic-only.json').read_text('utf-8'))
    if marker.get('synthetic_only') is not True:
        raise ValueError('Refuse non-synthetic data')
    path=case/'workbench.sqlite3'
    plan,_=fulfillment.prepare_review(path,[marker['online']])
    history=lifecycle.history(path,plan['plans'][0]['head'],'payment')
    result={'synthetic_only':True,'payment_versions':len(history),'same_business_content':not plan['plans'][0]['changed'],
            'source_digest':plan['jst_fulfillment'][0]['digest'],'new_payment_commits':0}
    assert result['payment_versions']==1 and result['same_business_content']
    dump(case/'reopened.json',result)


def main(output):
    import test_jst_auto_order as fixtures
    fixture=fixtures.AutoOrders()
    fixture.setUp()
    try:
        keys=fixture.prepared()
        automatic.process(fixture.path,keys,fixture.portal,'preview')
        automatic.process(fixture.path,keys,fixture.portal,'submit')
        order=fixture.read(keys[0])
        output.mkdir(parents=True,exist_ok=False)
        case=output/'same-original-case'
        case.mkdir()
        path=case/'workbench.sqlite3'
        with closing(sqlite3.connect(fixture.path)) as source,closing(sqlite3.connect(path)) as target:
            source.backup(target)
        dump(case/'synthetic-only.json',{'synthetic_only':True,'online':order['online']})
        def stock():
            with closing(wr.read_connection(path)) as db:
                return sha256(wr.dumps({name:db.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                                       for name in ('jst_baselines','jst_warehouses','jst_active','jst_reservations','jst_locks')}).encode()).hexdigest()
        before=stock()
        assert not lifecycle.snapshot(path,order['payload']['head'])['present']['payment']
        native=case/'合成实配原件.xlsx'
        book=openpyxl.Workbook()
        sheet=book.active
        sheet.title='合成实配明细'
        sheet.append(['内部订单号','线上订单号','店铺名称','商品编码','商品名称','实配数量','商品单价','商品金额','数量单位'])
        amount=Decimal(0)
        for sku,qty,_ in order['payload']['lines']:
            actual=min(Decimal(qty),Decimal('12'))
            price=Decimal('2.75')
            amount+=actual*price
            sheet.append([order['internal'],order['online'],order['payload']['shop']['name'],sku,'合成验收商品',str(actual),str(price),str(actual*price),'库存单位'])
        book.save(native)
        book.close()
        binding=order['proof']['binding']
        spec={'warehouse':binding['warehouse'],'shop':binding['shop'],'online':order['online'],'internal':order['internal'],
              'phase':order['stage'],'binding':binding,'contact':{'buyer':order['payload']['profile']['values']['buyer']}}
        config={'synthetic_only':True,'operation_id':uuid4().hex,'state_root':str(case/'jst_catalog_sync'),'native_file':str(native),
                'spec':spec,'account':{'warehouse':binding['warehouse'],'expectedCompany':binding['company'],
                'expectedCoid':binding['coid'],'expectedUid':binding['uid'],'bindingVersion':binding['version']}}
        input_file=case/'native-input.json'
        dump(input_file,config)
        node=ROOT/'components/aoxiang/runtime/node.exe'
        entry=ROOT/'tools/jst_fulfillment_local.mjs'
        def native_run(file,*flags):
            process=subprocess.run([str(node),str(entry),str(input_file),str(file),*flags],capture_output=True,text=True,encoding='utf-8')
            (file.with_suffix('.log')).write_text(process.stdout+process.stderr,'utf-8')
            if process.returncode:
                raise ValueError('Local browser acceptance failed: '+process.stderr[-1200:])
            return json.loads(file.read_text('utf-8'))
        received=native_run(case/'native-received.json')
        assert received['downloads']==1 and received['queries']==1
        class Browser:
            def action(self,action,current,**extra):
                assert action=='allocation' and current['online']==order['online']
                return received['result']
        pulled=fulfillment.pull(path,keys,Browser())
        assert pulled[0]['state']=='READY'
        assert not lifecycle.snapshot(path,order['payload']['head'])['present']['payment']
        plan,overview=fulfillment.prepare_review(path,keys)
        assert Decimal(overview[0]['amount'])==amount
        # Exercise the actual review entry. Checkbox/save input is synthetic.
        from PySide6.QtWidgets import QApplication,QWidget
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFont,QFontDatabase
        import jst_fulfillment_ui
        from lifecycle_ui import Review
        app=QApplication.instance() or QApplication([])
        app.setStyle('Fusion')
        font=Path('C:/Windows/Fonts/msyh.ttc')
        if font.exists():QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont('Microsoft YaHei',9))
        host=QWidget()
        host.work_repository=SimpleNamespace(path=path)
        host.repository=SimpleNamespace(folder=case)
        host.closing=False
        confirmations=[]
        timer=QTimer()
        timer.setInterval(20)
        def approve_synthetic_review():
            dialog=app.activeModalWidget()
            if not isinstance(dialog,Review):return
            timer.stop()
            assert not dialog.save.isEnabled()
            assert not lifecycle.snapshot(path,order['payload']['head'])['present']['payment']
            dialog.setWindowTitle('隔离流程验收 · 合成实配付款核对')
            dialog.grab().save(str(output/'同一原单_待人工确认.png'))
            dialog.complete.setChecked(True)
            assert dialog.save.isEnabled()
            confirmations.append('synthetic_checkbox_and_save')
            dialog.save.click()
        timer.timeout.connect(approve_synthetic_review)
        timer.start()
        receipt=jst_fulfillment_ui.review(host,host,keys)
        assert len(confirmations)==1 and receipt[0]['changed']
        assert stock()==before
        native_resume=native_run(case/'native-resumed.json','--resume')
        assert native_resume['downloads']==0 and native_resume['result']['reused'] is True
        process=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--reopen',str(case)],capture_output=True,text=True,encoding='utf-8')
        if process.returncode:raise ValueError('Separate process reopen failed: '+process.stderr[-1200:])
        reopened=json.loads((case/'reopened.json').read_text('utf-8'))
        assert reopened['source_digest']==pulled[0]['digest']
        result={'synthetic_only':True,'real_platform_requests':0,'real_order_imports':0,'real_payments':0,
                'warehouse':binding['warehouse'],'local_order':order['payload']['head']['number'],
                'synthetic_internal':order['internal'],'online':order['online'],'original_order_quantity':order['payload']['stats']['quantity'],
                'actual_quantity':overview[0]['quantity'],'actual_amount':str(amount),'native_file_sha256':received['result']['sha'],
                'native_exports':1,'native_exports_on_restart':0,'pending_payment_before_manual_confirmation':True,
                'synthetic_manual_confirmations':len(confirmations),'payment_versions_after_reopen':reopened['payment_versions'],
                'original_stock_and_allocation_unchanged':True,'same_original_through_all_stages':True,
                'passed':['local_browser_download','exact_original_binding','partial_allocation_and_changed_price',
                          'original_archived','pending_payment_review','explicit_checkbox_and_save','payment_saved',
                          'component_restart_reuses_original','application_reopen_reuses_payment_version']}
        dump(output/'同一原单流程验收.json',result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        host.deleteLater()
    finally:
        fixture.doCleanups()


if __name__=='__main__':
    if sys.argv[1]=='--reopen':reopen(Path(sys.argv[2]).resolve())
    else:main(Path(sys.argv[1]).resolve())
