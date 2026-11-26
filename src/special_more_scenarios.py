"""Business variants exercised from the standard Windows workbenches."""
from decimal import Decimal
import hashlib
import json
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QPushButton
from unittest.mock import patch
import special_plans as sp
from special_scenarios import simple,set_file,snap,heartbeat


def extra(ui,case):
    return {'choice-quota':choice_quota,'stock-proof':stock_proof,'policy-receipts':policy_receipts,
        'two-400':two_warehouses,'original-2000':original_2000,'assortment-change':assortment_change,
        'local-release':local_release,'file-interrupt':lambda u:file_recovery(u,False),'file-resume':lambda u:file_recovery(u,True)}[case](ui)


def file_recovery(ui,resume):
    import os,special_native
    d=ui.desk('opening')
    if not resume:
        ui.publish(d,simple(ui,'opening','PART',2));ui.preview(d);ui.claim(d)
    p=d.packages[0]
    f=ui.root/'同一原包.xls'
    ui.export=str(f)
    def within(w):
        if not resume:
            ui.snap(w,'写入原路径前')
            def leave(*args):
                (ui.root/'原文件已落盘.txt').write_text('文件写入完成，回执前退出','utf8')
                os._exit(87)
            with patch.object(special_native,'jst_file_receipt',leave):ui.press(w,'保存核对用 XLS')
        else:
            original=f.read_bytes()
            with sp.connection(d.path) as db:
                assert not db.execute("SELECT 1 FROM special_records WHERE stage='聚水潭文件回执'").fetchone()
            f.write_bytes(b'injected-different-digest')
            try:
                ui.press(w,'保存核对用 XLS')
                assert any('摘要不同' in msg for _,msg in ui.log)
                with sp.connection(d.path) as db:assert not db.execute("SELECT 1 FROM special_records WHERE stage='聚水潭文件回执'").fetchone()
            finally:f.write_bytes(original)
            for _ in range(2):ui.press(w,'保存核对用 XLS')
            assert f.read_bytes()==original
    d.tabs.setCurrentIndex(1);d.orders.selectRow(0)
    ui.platform_dialog(d,'聚水潭原任务',within)
    with sp.connection(d.path) as db:
        rows=db.execute("SELECT stage,version FROM special_records WHERE stage IN ('聚水潭文件意图','聚水潭文件回执') ORDER BY rowid").fetchall()
    assert rows==[('聚水潭文件意图',1),('聚水潭文件回执',1)]
    ui.snap(d,'原路径原摘要恢复')
    return dict(online=p['online'],digest=hashlib.sha256(f.read_bytes()).hexdigest(),records=rows)


def choice_quota(ui):
    d=ui.desk('policy')
    v=simple(ui,'policy','PART',6)
    v.update(stores=[f'门店{i:02}' for i in range(20)],quota=100)
    try:ui.publish(d,v)
    except RuntimeError as exc:assert '20 件' in str(exc)
    else:raise AssertionError('20件超额未拦截')
    v=ui.inputs('scope')
    key=ui.publish(d,v)
    data=ui.preview(d)['rows'];a=[r for r in data if r['store']=='A']
    set_file(ui,'A店选择',dict(plan=key,version=1,store='A',quantities={a[0]['id']:2,a[1]['id']:2}))
    ui.press(d,'门店确认')
    snap(ui,d,[dict(store='A',sku=s,at='2026-11-25T10:00:00+08:00',snapshot='快照-'+s,available=q,transit=0) for s,q in [('K1',2),('K2',0)]])
    out=ui.preview(d)['rows']
    assert [(r['sku'],r['quantity']) for r in out if Decimal(r['quantity'])]==[('K2','2')]
    ui.snap(d,'任选只补B');return dict(over_quota=20,positive=[['K2',2]],stores=sorted({r['store'] for r in out}))


def stock_proof(ui):
    import warehouse_catalog
    d=ui.desk('policy')
    ui.publish(d,simple(ui,'policy','SHARED',4));ui.preview(d);ui.claim(d);ui.proof(d,0)
    p=d.packages[0]
    obj=ui.desk('opening');ui.publish(obj,simple(ui,'opening','SHARED',10))
    before=ui.preview(obj)['rows'][0]
    assert before['quantity']=='6',before
    ui.load_stock(obj);ui.press(obj,'确认本轮分配')
    assert '重新预览' in heartbeat(ui,obj)
    pending=ui.preview(obj)['rows'][0]
    assert pending['quantity']=='0' and '待核' in pending['reason'],pending
    cat=warehouse_catalog.Repository(obj.path.parent).snapshots(('山东仓',))[0]['山东仓']
    evidence=dict(warehouse='山东仓',source_sha=cat['source_sha256'],revision=cat['revision'],reason='仓原快照与原单核对',
        rows=[dict(gid=p['gid'],sku='SHARED',order_number=p['number'],included='4',platform_reference=p['online'])])
    set_file(ui,'当前库存已含原代次',evidence);ui.press(obj,'核对快照已含占用')
    after=ui.preview(obj)['rows'][0]
    assert after['quantity']=='10',after
    ui.snap(obj,'库存已含四件');return dict(previous=before['quantity'],uncertain=pending['reason'],after=after['quantity'],generation=p['gid'])


def local_release(ui):
    d=ui.desk('policy');ui.publish(d,simple(ui,'policy','PART',4));ui.preview(d);ui.claim(d)
    oid=d.packages[0]['oid']
    from PySide6.QtWidgets import QInputDialog
    with patch.object(QInputDialog,'getText',return_value=('已核对本地未发送原包',True)):
        for _ in range(2):
            d.tabs.setCurrentIndex(1);d.orders.selectRow(0);ui.press(d,'撤销未发包')
            assert not d.status.text().startswith('待核'),d.status.text()
    assert ui.preview(d)['rows'][0]['quantity']=='4'
    ui.claim(d);ui.proof(d,1)
    with patch.object(QInputDialog,'getText',return_value=('平台已受理，不应通过',True)):
        d.tabs.setCurrentIndex(1);d.orders.selectRow(1);ui.press(d,'撤销未发包')
        assert '无法证实原包未发送' in d.status.text()
    with sp.connection(d.path) as db:
        count=db.execute('SELECT count(*) FROM special_releases').fetchone()[0]
        assert count==1 and sp.latest(db,oid,'未发送撤销')['version']==1
    ui.snap(d,'已发原单保留占用')
    return dict(releases=count,replacement=4,packages=len(d.packages))


def policy_receipts(ui):
    import openpyxl,lifecycle_io as io,work_repository as wr
    d=ui.desk('policy');ui.publish(d,ui.inputs('fair'));ui.preview(d);ui.claim(d)
    for i,p in enumerate(d.packages):
        orig=ui.proof(d,i);ui.pay(d,i)
        if i==2:continue
        f=ui.root/f'原始收货{i}.xlsx'
        book=openpyxl.Workbook();page=book.active;page.title=io.TITLES['receipt']
        fields=io.fields('receipt');page.append(fields)
        row={'补货单号':p['number'],'签发修订':1,'门店编码':p['scope']['store'],'供货仓':'山东仓','商品编码':'FAIR','商品名称':'样品FAIR','数量单位':'库存单位',
             io.QUANTITIES['receipt']:int(p['items'][0]['quantity']),io.DOCNUM['receipt']:'隔离收货'+str(i),io.DOCDATE['receipt']:wr.today(),'实际金额':int(p['items'][0]['amount']),'币种':'CNY'}
        page.append([row.get(k) for k in fields]);book.save(f)
        d.orders.selectRow(i);d.tabs.setCurrentIndex(2)
        def post(w):
            ui.press(w,'选择文件');ui.press(w,'核对并准备保存')
            assert w.review,w.note.text()
            w.review.complete.setChecked(True);w.review.extra_ack.setChecked(True)
            ui.snap(w,'正式政策收货'+str(i));ui.press(w,'确认保存完整单据')
            assert w.receipt,w.note.text()
        with patch.object(QFileDialog,'getOpenFileNames',return_value=([str(f)],'')):
            ui.modal(lambda:ui.press(d,'登记本仓实收'),post)
    ui.output(d,'已收五与未结二')
    import csv
    with open(ui.export,encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    assert sum(Decimal(r['已归属实收']) for r in rows)==5 and sum(Decimal(r['有效未结']) for r in rows)==2,rows
    ui.proof(d,2,0,True,'已取消')
    ui.output(d,'取消后剩余七')
    with open(ui.export,encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    assert sum(Decimal(r['尚可补']) for r in rows)==7,rows
    return dict(received=5,open_before=2,gap_before=5,gap_after=7)


def two_warehouses(ui):
    d=ui.desk('opening')
    raw=ui.inputs('opening')
    clean=raw['lines'][:400]
    raw.update(lines=clean+[{**v,'source_row':i+402,'warehouse':'河北仓','account':'1','shop':'22','supplier':'HB-SUP'} for i,v in enumerate(clean)],single_purchase=False)
    ui.publish(d,raw);parts=ui.preview(d)['packets'];ui.claim(d)
    assert [len(p['items']) for p in parts]==[400,400] and [p['scope']['warehouse'] for p in parts]==['山东仓','河北仓']
    ui.output(d,'双仓400原行');ui.snap(d,'双仓各四百')
    return dict(sizes=[400,400],orders=[p['number'] for p in d.packages])


def original_2000(ui):
    ui.win.show_section(2)
    options=[b for b in ui.win.findChildren(QPushButton) if b.text()=='聚水潭导入单']
    assert options
    stats={}
    def page(w):
        p=w.panel
        ui.wait(lambda:p.data is not None and not p.reading and not p.worker,60)
        p.warehouse.setCurrentText('山东仓')
        ui.wait(lambda:not p.reading and not p.worker,60)
        ui.press(p,'勾选筛选结果')
        def selected(o):
            assert len(o.selected())==1,o.note.text()
            row=o.selected()[0]
            assert row['sku_count']==2000,row['sku_count']
            ui.export=str(ui.root/'普通2000款整单.xls')
            ui.press(o,'保存核对用 XLS');ui.press(o,'平台预览');ui.press(o,'确认导入');ui.press(o,'核对原单')
            got=ui.jst.orders[row['online']]
            assert len(got['items'])==2000 and len(ui.jst.orders)==1
            stats.update(lines=2000,orders=1,online=row['online'],internal=got['internal'],xlsx=ui.export)
            ui.snap(o,'普通两千款一张原单')
            o.close()
        from jst_auto_order_ui import Orders
        from PySide6.QtCore import QTimer
        errors=[]
        timer=QTimer(w)
        def await_orders():
            modal=ui.app.activeModalWidget()
            if isinstance(modal,Orders):
                timer.stop()
                try:selected(modal)
                except BaseException as exc:errors.append(exc);modal.reject()
        timer.timeout.connect(await_orders);timer.start(20)
        try:ui.press(p,p.auto_prepare_btn.text())
        finally:timer.stop()
        if errors:raise errors[0]
        assert stats,p.status.text()
        w.close()
    ui.modal(options[0].click,page)
    return stats


def assortment_change(ui):
    d=ui.desk('opening')
    raw=ui.inputs('opening');ui.publish(d,raw);ui.preview(d);ui.claim(d)
    import openpyxl
    updated=ui.root/'到货后禁用仍在.xlsx'
    w=openpyxl.load_workbook(ui.root/'inputs/山东仓-第二版.xlsx');ws=w.active
    fields={c.value:c.column for c in ws[1]}
    ws.cell(1651,fields['商品状态'],'禁用');w.save(updated);w.close()
    d.tabs.setCurrentIndex(0);ui.file=str(updated);ui.press(d,'采用仓资料')
    pending=ui.preview(d)['rows'];assert sum(int(r['quantity']) for r in pending)==44
    ui.claim(d);assert len(d.packages)==4
    raw['lines'][-1]['target']='0'
    ui.publish(d,raw,True);ui.output(d,'批准免供一款')
    import csv
    with open(ui.export,encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
    assert len(rows)==1650 and sum(Decimal(r['有效未结']) for r in rows)==1649 and rows[-1]['经批准免供']=='1' and rows[-1]['原目标']=='1'
    ui.snap(d,'原货盘留有一款免供');return dict(original=1650,supplied_commitments=1649,waived=1)
