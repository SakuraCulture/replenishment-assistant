"""Opt-in frozen local business acceptance with synthetic files. Never connects AX."""
import copy
import json
import time
from pathlib import Path


def exercise(win, output, wait, active, checks):
    import openpyxl
    import batch_service
    import engine
    import parameter_schema
    import order_lifecycle as lc
    import aoxiang_receipts as receipts
    from aoxiang_client import ProcessClient
    from aoxiang_input_ui import adopt
    from aoxiang_purchase_ui import PurchaseDialog
    from aoxiang_directory import atomic_json, file_hash

    fixture = json.loads((output/'business-fixture.json').read_text(encoding='utf-8'))
    if fixture.get('synthetic') is not True or fixture.get('live_platform') is not False:
        raise ValueError('Explicit synthetic local fixture required')
    times = {}
    began = time.perf_counter()
    win.master_legacy.setChecked(True)
    win.products.path = fixture['master']
    win.auto_check.stop()
    win.job_dirty = False
    if not adopt(win,fixture['manifest']):
        raise ValueError('Input adoption refused')
    wait(lambda:win.worker is None)
    if list(win.loaded['stores']) != ['W2','W1']:
        raise ValueError('Input file/store order changed')
    times['input_adoption_seconds'] = time.perf_counter()-began
    checks.append('合成两店文件经原检查Worker与Windows spawn采用，门店顺序W2/W1保持')
    profile = json.loads((Path(__file__).parent/'default_must_sell.json').read_text(encoding='utf-8'))
    from ui_tasks import BatchWorker
    result_map, jobs = {}, {}
    began = time.perf_counter()
    for code, budget in [('W2',30),('W1',None)]:
        jobs[code] = {'warehouses':engine.DEFAULT_WAREHOUSES,'exception':True,'budget':budget,'parameter_config':parameter_schema.standard()}
    worker = BatchWorker(win.loaded,jobs,profile,2,win)
    def changed(code,state,result,error):
        if error:active['error']=error
        if state=='已完成':result_map[code]=result
    worker.changed.connect(changed)
    worker.failed.connect(lambda error:active.update(error=error))
    worker.start()
    try:
        wait(lambda:worker.isFinished())
    finally:
        if worker.isRunning():
            worker.cancel.set()
            # Keep the actual worker alive until running stores finish normally.
            saved_error, active['error'] = active['error'], None
            wait(lambda:worker.isFinished())
            active['error'] = saved_error
        worker.deleteLater()
    if set(result_map)!=set(jobs):
        raise ValueError('Calculation did not complete')
    results = [result_map[code] for code in jobs]
    for code in jobs:
        engine.export_xlsx(result_map[code],output/(code+'-frozen.xlsx'))
    times['calculate_and_export_seconds'] = time.perf_counter()-began
    checks.append('两店分别按30元/不限预算计算并导出；由外部脚本与源码同输入逐字段及Excel对照')
    atomic_json(output/'business-results.json',results)
    allowed = {'configuration','purchase.list','purchase.status','purchase.reserve','purchase.cancel','ping','shutdown'}
    calls = []
    class LocalOnly(ProcessClient):
        def request(self, action, **kwargs):
            if action not in allowed:
                raise RuntimeError('Offline EXE smoke forbids platform action: '+action)
            calls.append(action)
            return super().request(action,**kwargs)
    before = lc.snapshot(win.work_repository.path,fixture['head'])
    package, config, suppliers = None, None, None
    for index in range(2):
        dialog = PurchaseDialog(win,win,fixture['head'],client_factory=LocalOnly)
        active['dialog'] = dialog
        dialog.show()
        wait(lambda:dialog.configuration is not None and dialog.worker is None)
        if index == 0:
            dialog.receipt = fixture['receipt']
            began = time.perf_counter()
            dialog.run(dialog.prepare_package,dialog.package_ready)
            wait(lambda:dialog.worker is None)
            times['purchase_prepare_reserve_seconds'] = time.perf_counter()-began
        package, config, suppliers = dialog.package, dialog.configuration, dialog.suppliers
        if not package or dialog.current['state'] != 'RESERVED' or package['intent']['totalQuantity'] != '7' or package['intent']['totalAmount'] != '0.99':
            raise ValueError('Frozen purchase quantity/amount/recovery differs')
        if dialog.table.rowCount()!=1 or dialog.table.item(0,0).text()!='0001':
            raise ValueError('Business purchase preview missing')
        dialog.grab().save(str(output/f'purchase-local-{index+1}.png'))
        dialog.close()
        wait(lambda:dialog._can_close and dialog.worker is None)
        if dialog.client.running:
            raise ValueError('Purchase component survived close')
        dialog.deleteLater()
        active['dialog'] = None
    if lc.snapshot(win.work_repository.path,fixture['head']) != before:
        raise ValueError('Preparation changed original lifecycle facts')
    checks.append('真实固定Node登记采购任务，付款7件/0.99元保持；窗口重开恢复原预览与唯一任务，未上传')
    # The server's native observation is explicitly synthetic here. Node parsers
    # and terminal controller have separate tests; this exercises the frozen host adapter.
    intent = package['intent']
    scope = dict(orderId='SYNTHETIC-PO',store='T',supplier='2078246',status='已完成',quantity='7',amount='0.99',receivedQuantity='5',receivedAmount='0.70')
    main = {**scope,'skuCount':'1','_sourceRow':5}
    row = {**scope,'sku':'0001','name':'合成商品','unit':'包','_sourceRow':5,'createdAt':'2026-11-01 12:00:00','updatedAt':'2026-11-02 12:00:00'}
    files = []
    for source,values in [('D16',main),('D17',row)]:
        file = output/(source+'-SYNTHETIC.xlsx')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '采购主单详情' if source=='D16' else '采购子单详情'
        ws.append(list(values))
        for _ in range(3):ws.append([])
        ws.append(list(values.values()))
        wb.save(file)
        wb.close()
        files.append(dict(source=source,file=str(file),sha256=file_hash(file)))
    observation = dict(schema=1,operation='purchase.receipt.observation',purchaseId=intent['id'],runId='synthetic',identity=intent['identity'],basis=intent['basis'],orderId='SYNTHETIC-PO',supplierCode='2078246',observedAt='2026-11-02T04:00:00Z',platformCompletedAt=None,terminal=True,eligible=True,main=main,rows=[row],issues=[],files=files,quantityBasis='native_actual_inventory_quantity_no_conversion')
    file = output/'synthetic-observation.json'
    atomic_json(file,observation)
    observed = dict(file=str(file),sha256=file_hash(file))
    first = receipts.synchronize(win.work_repository.path,package,observed,config,suppliers)
    second = receipts.synchronize(win.work_repository.path,package,observed,config,suppliers)
    after = lc.snapshot(win.work_repository.path,fixture['head'])
    if first['version']!=1 or second['changed'] or second['version']!=1 or after['documents']['payment']!=before['documents']['payment'] or after['documents']['return']!=before['documents']['return']:
        raise ValueError('Receipt changed upstream facts or replay duplicated version')
    if after['documents']['receipt']['payload']['rows'][0]['base_quantity']!='5':
        raise ValueError('Receipt quantity changed')
    checks.append('合成完成单实收5件按原生命周期落库，重复同步不新增修订；付款7件及回传5件原事实保持')
    # Explicit QA business edit after successful synchronization: original procurement
    # stays at 7 / 0.99 while current confirmed payment becomes 8 / 1.23.
    import lifecycle_io as io
    payment_file = output/'synthetic-payment-update.xlsx'
    wb = openpyxl.load_workbook(output/'payment.xlsx')
    ws = wb.active
    headers = [c.value for c in ws[1]]
    ws.cell(2,headers.index(io.QUANTITIES['payment'])+1,8)
    ws.cell(2,headers.index('实际金额')+1,1.23)
    wb.save(payment_file)
    wb.close()
    input_file = io.inspect([payment_file])[0]
    sheet = input_file['sheets'][0]
    scan = io.scan('payment',[{**input_file,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
    lc.commit(win.work_repository.path,lc.prepare(win.work_repository.path,scan,{lc.binding_key(r):fixture['head'] for r in scan['rows']}))
    changed = lc.snapshot(win.work_repository.path,fixture['head'])
    began = time.perf_counter()
    dialog = PurchaseDialog(win,win,fixture['head'],client_factory=LocalOnly)
    active['dialog'] = dialog
    dialog.show()
    wait(lambda:dialog.current is not None and dialog.worker is None)
    times['stale_basis_open_seconds'] = time.perf_counter()-began
    if '付款单已更新' not in dialog.basis_note.text() or dialog.upload_btn.isEnabled():
        raise ValueError('Outdated procurement basis not visible or upload still enabled')
    if dialog.table.item(0,2).text()!='7' or dialog.table.item(0,3).text()!='0.99':
        raise ValueError('Outdated procurement was silently repriced or changed')
    dialog.grab().save(str(output/'purchase-stale-basis.png'))
    dialog.close()
    wait(lambda:dialog._can_close and dialog.worker is None)
    if dialog.client.running:
        raise ValueError('Stale-basis component survived close')
    dialog.deleteLater()
    active['dialog'] = None
    if lc.snapshot(win.work_repository.path,fixture['head'])!=changed:
        raise ValueError('Reading stale basis mutated original lifecycle facts')
    checks.append('当前付款更新为8件/1.23元后，旧采购预览明确待核且禁用上传；仍展示原7件/0.99元，查询无业务写入')
    atomic_json(output/'business-local-evidence.json',dict(synthetic=True,live_platform=False,checks=checks,timings=times,protocol_actions=calls,receipt_first=first,receipt_second=second))
