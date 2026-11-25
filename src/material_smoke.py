"""Exercise the Windows application and isolated platform boundaries for material orders."""
import json
import os
import traceback
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QPushButton


def start_smoke(app, host, args):
    out, phase = Path(args[0]).resolve(), args[1]
    spec = json.loads((out/'material-fixture.json').read_text(encoding='utf-8'))
    path = host.work_repository.path
    if (phase not in ('register','resume') or spec.get('synthetic') is not True
            or spec.get('live_platform') is not False or path.resolve()!=out/'settings/workbench.sqlite3'
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings'
            or Path(os.environ.get('LOCALAPPDATA','')).resolve()!=out/'test-appdata'):
        raise ValueError('物料验收需要专用隔离资料目录')

    import aoxiang_purchase_tasks
    import catalog_session_ui
    import jst_auto_order_store
    import jst_fulfillment
    import jst_one_click_fixture
    import lifecycle_batch
    import material_orders
    import order_lifecycle
    import purchases
    import work_repository as wr
    from version_info import diagnostic

    host.jst_generator.stopped = True
    host.aoxiang_workflows.stopping = True
    host.browser_status.paused = True
    host.browser_status.initial.stop()
    for manager in (host.jst_generator,host.aoxiang_workflows,host.browser_status,host.jst_fulfillment_runner,host.scheduled):
        manager.timer.stop()
    platform = jst_one_click_fixture.Component(out/'platform')
    result = {'phase':phase, 'checks':[], 'orders':[]}
    checks = result['checks']

    def button(window, title):
        return next(widget for widget in window.findChildren(QPushButton) if title in (widget.text(),widget.accessibleName()))

    def grab(window, label):
        assert window.grab().save(str(out/(phase+'-'+label+'.png')))

    def wait(callback):
        for _ in range(600):
            if callback():return
            QTest.qWait(25)
        raise TimeoutError('等待软件入口读取超时')

    def drive(action, handlers):
        errors, used = [], set()
        timer = QTimer(host)
        def tick():
            dialog = app.activeModalWidget()
            if dialog is None:
                return
            title = dialog.windowTitle()
            if title not in handlers or title in used:
                return
            used.add(title)
            try:
                handlers[title](dialog)
            except BaseException:
                errors.append(traceback.format_exc())
                dialog.reject()
        timer.timeout.connect(tick)
        timer.start(15)
        try:
            action()
        finally:
            timer.stop()
            timer.deleteLater()
        if errors:
            raise RuntimeError('\n'.join(errors))
        return used

    def accept_preview(dialog):
        grab(dialog,'分配预览')
        button(dialog,'确认分配').click()

    def desk_open(desk):
        desk.wh.setCurrentText(spec['affected'])
        assert {row[0] for row in desk.catalog_rows}=={spec['sku'],'@ZERO'}
        grab(desk,'物料目录')
        desk.tabs.setCurrentIndex(1)
        if phase=='register':
            def accept_import(dialog):
                grab(dialog,'需求核对')
                button(dialog,'确认保存申请').click()
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(out/'物料需求.xlsx'),'')):
                assert '核对物料需求表' in drive(button(desk,'导入需求表').click,{'核对物料需求表':accept_import})
            assert desk.lines.rowCount()==3
            button(desk,'勾选有剩余需求').click()
            def interrupted(dialog):
                assert '受控中断' in dialog.text()
                dialog.accept()
            with patch.object(material_orders,'commit',side_effect=RuntimeError('受控中断：批次已登记')):
                seen = drive(desk.preview_button.click,{'核对物料分配':accept_preview,'物料分配未完成':interrupted})
            assert seen=={'核对物料分配','物料分配未完成'},seen
            pending, = material_orders.pending(path)
            assert [row['quantity'] for row in pending['rows']]==spec['quantities']
            result.update(batch=pending['id'],rows=pending['rows'])
            with closing(wr.read_connection(path)) as db:
                assert db.execute("SELECT count(*) FROM normal_orders WHERE kind='material'").fetchone()[0]==0
            checks.append('正常入口导入并确认，登记后中断，物料仓单和占用均为零')
            desk.reject()
            return
        pending, = material_orders.pending(path)
        option = pending['created']+' · '+pending['id'][:12]
        with patch.object(QInputDialog,'getItem',return_value=(option,True)):
            assert '核对物料分配' in drive(button(desk,'继续原分配').click,{'核对物料分配':accept_preview})
        saved = desk.last_result
        assert saved and saved['batch']==pending['id'] and saved['rows']==pending['rows']
        echo = material_orders.commit(path,saved['batch'])
        assert echo==saved and len(saved['orders'])==3
        result.update(batch=saved['batch'],rows=saved['rows'])
        checks.append('重开并通过继续原分配沿用原快照，重复确认返回同批结果')
        button(desk,'查看物料订单').click()

    def yes(dialog):
        dialog.button(QMessageBox.StandardButton.Yes).click()

    def auto_orders(dialog):
        assert len(dialog.records)==3
        grab(dialog,'聚水潭原仓单')
        button(dialog,'平台预览').click()
        platform.drop_import_response = True
        drive(button(dialog,'确认导入').click,{'确认导入':yes})
        platform.drop_import_response = False
        button(dialog,'核对原单').click()
        assert len(platform.orders)==3
        assert sum(call[0]=='order.import' for call in platform.calls)==3
        grab(dialog,'聚水潭回读')
        dialog.reject()

    def jst_page(dialog):
        page = dialog.panel
        wait(lambda: page.data is not None and not page.reading)
        assert len(page.visible_records())==3 and all(r['state']=='ready' for r in page.visible_records())
        button(page,'勾选筛选结果').click()
        assert '聚水潭自动下单' in drive(page.auto_prepare_btn.click,{'聚水潭自动下单':auto_orders})
        dialog.reject()

    def finish():
        destination = out/(phase+'-report.json')
        try:
            with patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=platform)):
                assert '物料单' in drive(host.material_entry.click,{'物料单':desk_open})
                if phase=='resume':
                    panel = host.followup
                    wait(lambda:len(panel.records)==3 and panel.order_kind.currentData()=='material')
                    assert all(h.get('kind')=='material' for h in panel.records)
                    grab(panel,'物料订单跟踪')
                    assert '聚水潭导入单 · 环节工作页' in drive(lambda:panel.open_stage('jst'),
                                                               {'聚水潭导入单 · 环节工作页':jst_page})
                    heads = {h['number']:h for h in purchases.targets(path,current_only=True) if h.get('kind')=='material'}
                    with closing(wr.read_connection(path)) as db:
                        originals = [(online,jst_auto_order_store.read(db,online)) for online in platform.orders]
                    config = spec['ax_config']
                    suppliers = spec['suppliers']
                    for online, original in originals:
                        h = heads[original['payload']['head']['number']]
                        plan, overview = jst_fulfillment.prepare_review(path,[online])
                        assert len(overview)==1 and order_lifecycle.snapshot(path,h)['documents']['payment'] is None
                        order_lifecycle.commit(path,plan)
                        assert order_lifecycle.snapshot(path,h)['status']=='待付款'
                        platform.orders[online]['native_status']='发货中(打单拣货)'
                        jst_fulfillment.run_order_payment(path,[online],platform)
                        assert order_lifecycle.snapshot(path,h)['status']=='待收货'
                        package = aoxiang_purchase_tasks.prepare(path,h,config,suppliers,spec['template_receipt'],
                            out/'purchase-packages',spec['template_contract'])
                        aoxiang_purchase_tasks.validate_for_upload(path,package,config,suppliers)
                        intent = package['intent']
                        assert intent['supplierCode']==suppliers[h['warehouse']]
                        received = _receipt(path,out,package,config,suppliers,h)
                        result['orders'].append(dict(number=h['number'],warehouse=h['warehouse'],store=h['store'],
                            generation=original['gid'],online=online,internal=original['internal'],
                            account=original['proof']['binding']['coid'],payment_version=1,quantity=intent['totalQuantity'],
                            amount=intent['totalAmount'],supplier=suppliers[h['warehouse']],jst_submissions=1,
                            ax_uploads=0,status=order_lifecycle.snapshot(path,h)['status'],**received))
                    export = out/'原物料单';export.mkdir()
                    files = lifecycle_batch.export_signed(path,list(heads.values()),export)
                    assert len(files)==3 and lifecycle_batch.export_signed(path,list(heads.values()),export)==files
                    result['exports']=files
                    checks.extend(['实际入口导入三张原仓单，受理后断线沿原线上单号核对，实际受控提交各一次',
                                   '付款版本人工确认后仍待付款，核实发货中后直转待收货',
                                   '翱象同仓供应商、付款数量金额及最终上传前校验通过',
                                   '按同仓原采购和受控收货原件落账，重复同步维持一个实收版本',
                                   '按原仓单补导并校验重复导出回执'])
            result['calls']=platform.calls
            result['build']=diagnostic()
            destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            app.exit(0)
        except BaseException:
            destination.write_text(json.dumps({**result,'traceback':traceback.format_exc()},ensure_ascii=False,indent=2),encoding='utf-8')
            app.exit(1)

    QTimer.singleShot(100,finish)


def _receipt(path, out, package, configuration, suppliers, head):
    import openpyxl

    import aoxiang_receipts
    import order_lifecycle
    import work_repository as wr
    from aoxiang_directory import file_hash

    intent = package['intent']
    po = 'MAT-PO-'+intent['id'][-12:]
    directory = out/'受控实收'/po
    directory.mkdir(parents=True)
    scope = dict(orderId=po,store=head['store'],supplier=intent['supplierCode'],status='已完成',
                 quantity=intent['totalQuantity'],amount=intent['totalAmount'],
                 receivedQuantity=intent['totalQuantity'],receivedAmount=intent['totalAmount'])
    main = {**scope,'skuCount':str(len(intent['rows'])),'_sourceRow':5}
    rows = [{**scope,**{key:line[key] for key in ('sku','name','quantity','amount')},'unit':'库存单位',
             'receivedQuantity':line['quantity'],'receivedAmount':line['amount'],'_sourceRow':i+5,
             'createdAt':wr.now(),'updatedAt':wr.now()} for i,line in enumerate(intent['rows'])]
    files = []
    for source,title,records in (('D16','采购主单详情',[main]),('D17','采购子单详情',rows)):
        filename=directory/(source+'.xlsx')
        book=openpyxl.Workbook()
        sheet=book.active
        sheet.title=title
        sheet.append(list(records[0]))
        for _ in range(3):sheet.append([])
        for record in records:sheet.append(list(record.values()))
        book.save(filename)
        book.close()
        files.append(dict(source=source,file=str(filename),sha256=file_hash(filename)))
    observed = dict(schema=1,operation='purchase.receipt.observation',purchaseId=intent['id'],runId='material-controlled',
                    identity=intent['identity'],basis=intent['basis'],orderId=po,supplierCode=intent['supplierCode'],
                    observedAt=wr.now(),platformCompletedAt=None,terminal=True,eligible=True,main=main,rows=rows,
                    issues=[],files=files,quantityBasis='native_actual_inventory_quantity_no_conversion')
    filename=directory/'收货观察.json'
    filename.write_text(json.dumps(observed,ensure_ascii=False),encoding='utf-8')
    receipt=dict(file=str(filename),sha256=file_hash(filename))
    installed=aoxiang_receipts.synchronize(path,package,receipt,configuration,suppliers)
    repeated=aoxiang_receipts.synchronize(path,package,receipt,configuration,suppliers)
    assert installed['version']==repeated['version']==1 and not repeated['changed']
    assert order_lifecycle.snapshot(path,head)['documents']['receipt']['version']==1
    return dict(po=po,receipt_version=1,receipt_writes=1)
