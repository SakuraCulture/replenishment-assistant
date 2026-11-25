"""Additional real-window acceptance cases. Faults are scoped to the disposable fixture."""
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import json
import time
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import special_plans as sp


def exercise(ui,case):
    if case=='race':return race(ui)
    if case in ('interrupt','resume-transaction'):return interrupted(ui,case)
    if case=='calendar':return calendar(ui)
    if case=='holding-goals':return holding(ui)
    if case=='revision-kit':return revision(ui)
    if case=='scope-boundary':return scope_boundary(ui)
    if case=='amount':return amount(ui)
    if case=='cancel':return cancellation(ui)
    if case=='source-rows':return source_rows(ui)
    if case=='ambiguous':return ambiguous(ui)
    if case.startswith('migration-'):return migrate(ui,case)
    raise ValueError(case)


def simple(ui,kind,sku,target=1):
    v=ui.inputs('opening' if kind=='opening' else 'fair')
    v.update(stores=['A'],lines=[dict(sku=sku,unit='件',target=str(target))])
    return v


def set_file(ui,name,data):
    f=ui.root/(name+'.json')
    f.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf8')
    ui.file=str(f)


def heartbeat(ui,d):
    ui.wait(lambda:d.claim_worker is None,60)
    return d.status.text()


def race(ui):
    from catalog_write_lock import acquire
    d,o,h=ui.desk('policy'),ui.desk('opening'),ui.desk('policy')
    for w,wh in ((d,'山东仓'),(o,'山东仓'),(h,'河北仓')):
        v=simple(ui,w.kind,'SHARED',6 if wh=='山东仓' else 8)
        v['stores']=['T']
        if wh=='河北仓':v['scope'].update(warehouse=wh,account='1',shop='22',supplier='HB-SUP')
        ui.publish(w,v)
        row=ui.preview(w)['rows'][0]
        assert row['before']==('4' if wh=='山东仓' else '8'),row
    with acquire(Path(d.path).parent,'山东仓'):
        for w in (d,o):ui.press(w,'确认本轮分配')
        assert d.claim_worker and o.claim_worker
        both=[bool(w.claim_worker.isRunning()) for w in (d,o)]
        ui.press(h,'确认本轮分配')
    statuses=[heartbeat(ui,w) for w in (d,o,h)]
    with sp.connection(d.path) as db:
        rows=list(db.execute("""SELECT n.kind,a.warehouse,r.sku,r.quantity,a.gid FROM jst_active a JOIN normal_orders n ON n.oid=a.oid
             JOIN jst_reservations r ON r.gid=a.gid WHERE r.sku='SHARED' ORDER BY a.warehouse,n.kind"""))
    sd=sum(Decimal(r[3]) for r in rows if r[1]=='山东仓')
    hb=sum(Decimal(r[3]) for r in rows if r[1]=='河北仓')
    assert both==[True,True] and sd==10 and hb==8 and sum('已登记' in s for s in statuses)==2,(rows,statuses,both)
    ui.snap(d,'山东并发'),ui.snap(h,'河北独立')
    for w in (d,o,h):ui.press(w,'刷新原单')
    assert all(ui.preview(w)['rows'][0]['quantity']=='0' for w in (d,o,h))
    return dict(workers=both,statuses=statuses,reservations=rows,shandong=str(sd),hebei=str(hb))


def interrupted(ui,case):
    d=ui.desk('opening')
    if case=='interrupt':
        v=simple(ui,'opening','FAIR',3)
        ui.publish(d,v)
        ui.preview(d)
        ui.snap(d,'提交前')
        import os,special_waves
        save=special_waves._save_package
        def stop(*args,**kw):
            result=save(*args,**kw)
            (ui.root/'事务中断点.txt').write_text('首包仓单、分配代次、占用在当前事务已写，尚未提交。','utf8')
            os._exit(86)
        with patch.object(special_waves,'_save_package',stop):ui.claim(d)
    with sp.connection(d.path) as db:
        a=[db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('special_runs','special_packages','special_claims')]
    assert a==[0,0,0],a
    ui.preview(d);ui.claim(d);ui.output(d,'重启后逐单')
    with sp.connection(d.path) as db:
        b=[db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('special_runs','special_packages','special_claims')]
    assert b==[1,1,1] and (ui.root/'事务中断点.txt').is_file(),b
    ui.snap(d,'重启完成')
    return dict(after_crash=a,after_retry=b)


def calendar(ui):
    import special_waves
    d=ui.desk('policy')
    v=simple(ui,'policy','PART',3)
    prefix='2026-11-26T'
    v.update(start=prefix+'09:00:00+08:00',end=prefix+'12:00:00+08:00')
    v['lines'][0].update(start=prefix+'10:00:00+08:00',end=prefix+'11:00:00+08:00')
    moment=['09:59:59']
    clock=lambda:prefix+moment[0]+'+08:00'
    with patch.object(sp,'now',clock),patch.object(special_waves,'now',clock):
        ui.publish(d,v)
        preview=ui.preview(d)
        assert not preview['rows'][0]['window_open']
        ui.press(d,'确认本轮分配')
        assert '时段' in heartbeat(ui,d)
        ui.press(d,'登记定时认领')
        ui.press(d,'运行已到期票据')
        with sp.connection(d.path) as db:assert db.execute('SELECT count(*) FROM special_runs').fetchone()[0]==0
        moment[0]='10:00:00'
        ui.press(d,'确认本轮分配')
        ui.press(d,'运行已到期票据')
        heartbeat(ui,d)
        ui.press(d,'刷新原单')
        assert len(d.packages)==1
        ui.jst_window(d,0)
        moment[0]='11:00:00'
        ui.jst_window(d,0,recover_only=True)
        ui.publish(d,v)
        ui.preview(d)
        ui.press(d,'确认本轮分配')
        assert '时段' in heartbeat(ui,d)
        ui.press(d,'登记定时认领');ui.press(d,'运行已到期票据')
        with sp.connection(d.path) as db:
            states=db.execute('SELECT state,message FROM special_tickets ORDER BY rowid').fetchall()
            runs=db.execute('SELECT count(*) FROM special_runs').fetchone()[0]
        assert runs==1 and [x[0] for x in states]==['已处理','待复核'],(runs,states)
        ui.snap(d,'截止和原票据')
    return dict(times=['09:59:59','10:00:00','11:00:00'],runs=runs,tickets=states,original_queries=len(ui.jst.calls))


def snap(ui,d,rows):
    set_file(ui,'门店观测'+str(len(ui.log)),rows)
    ui.press(d,'登记门店快照')
    assert '已登记' in d.status.text(),d.status.text()


def holding(ui):
    d=ui.desk('policy')
    def observations(skus,values,k):
        return [dict(store='A',sku=s,at=f'2026-11-{20+k:02}T10:00:00+08:00',snapshot=s+str(k),available=q,transit=0,
                     promise=0,included=0,evidence='门店原观测') for s,q in zip(skus,values)]
    v=simple(ui,'policy','K1',4)
    v['mode']='持有下限'
    skus=['K1','K2','HOLD']
    v['lines']=[dict(sku=s,target=4,unit='件') for s in skus]
    ui.publish(d,v);snap(ui,d,observations(skus,[6,6,0],0))
    three=[r['quantity'] for r in ui.preview(d)['rows']]
    assert three==['0','0','4'],three
    timeline={}
    for maintain in (False,True):
        v=simple(ui,'policy','HOLD',4);v.update(mode='持有下限',maintain=maintain)
        ui.publish(d,v)
        stages=[]
        for day,q in enumerate((4,None,2)):
            snap(ui,d,observations(['HOLD'],[q],day))
            row=ui.preview(d)['rows'][0]
            stages.append([row['quantity'],row['attained'],row['reason']])
        timeline[str(maintain)]=stages
    assert timeline['False'][-1][0]=='0' and timeline['True'][-1][0]=='2'
    combined=[]
    for target in (6,10):
        v=simple(ui,'policy','PART',target);v.update(mode='持有下限')
        ui.publish(d,v);snap(ui,d,observations(['PART'],[4],0))
        combined.append(int(ui.preview(d)['rows'][0]['quantity']));ui.claim(d)
    assert sum(combined)==6,combined
    ui.snap(d,'持有目标共享承诺')
    return dict(three=three,history=timeline,additional=combined)


def revision(ui):
    d=ui.desk('opening')
    v=simple(ui,'opening','DISABLED')
    v['lines'] += [dict(sku='K1',unit='件',target=1,kit='齐套一'),dict(sku='ABSENT',unit='件',target=1,kit='齐套一'),dict(sku='PART',unit='件',target=1)]
    key=ui.publish(d,v)
    before=ui.preview(d)['rows']
    assert [r['quantity'] for r in before]==['0','0','0','1'],before
    v['lines'][0]['target']='0'
    ui.publish(d,v,True)
    v['lines'].append(dict(sku='K2',unit='件',target=1,replaces=before[0]['id']))
    ui.publish(d,v,True)
    after=ui.preview(d)['rows']
    assert [r['quantity'] for r in after]==['0','0','0','1','1']
    ui.claim(d);ui.output(d,'免供和替代原行');ui.snap(d,'版本修订')
    with sp.connection(d.path) as db:
        versions=db.execute('SELECT version,reason,sha FROM special_versions WHERE plan=? ORDER BY version',(key,)).fetchall()
    return dict(original=before[0]['id'],versions=versions,approved=[(r['sku'],r['target'],r['quantity'],r['reason']) for r in after])


def scope_boundary(ui):
    d=ui.desk('opening')
    v=simple(ui,'opening','SHARED',1)
    other=dict(warehouse='河北仓',account='1',shop='22',supplier='HB-SUP')
    v['lines'].append(dict(sku='SHARED',unit='件',target=1,**other))
    try:ui.publish(d,v)
    except RuntimeError as exc:assert '供货范围冲突' in str(exc)
    else:raise AssertionError('不兼容供货范围已发布')
    v['single_purchase']=False
    ui.publish(d,v);ui.preview(d);ui.claim(d)
    assert [p['scope']['warehouse'] for p in d.packages]==['山东仓','河北仓']
    evidence=[]
    for i in range(2):
        orig=ui.proof(d,i);evidence.append(orig);ui.pay(d,i)
        d.orders.selectRow(i);ui.press(d,'冻结合并采购')
        assert '2.00' in d.status.text(),d.status.text()
    d.tabs.setCurrentIndex(1);d.orders.selectRow(1)
    set_file(ui,'错仓件',evidence[0]);ui.press(d,'核对聚水潭原件')
    assert '不符' in d.status.text()
    ui.snap(d,'阻止异仓原号')
    ui.output(d,'双仓原号逐行')
    return dict(warehouses=[e['warehouse'] for e in evidence],originals=[e['internal'] for e in evidence],groups=2)


def amount(ui):
    d=ui.desk('opening')
    ui.publish(d,simple(ui,'opening','PART',3));ui.preview(d);ui.claim(d);orig=ui.proof(d,0)
    orig['freight']='5';set_file(ui,'运费原件',orig)
    d.orders.selectRow(0);ui.press(d,'核对聚水潭原件');ui.pay(d,0)
    corr=dict(online=orig['online'],internal=orig['internal'],version=1,amounts={'PART':'10.00'},reason='人工核对付款商品额为10元')
    set_file(ui,'付款更正',corr);d.orders.selectRow(0);ui.press(d,'更正本包付款')
    assert not d.status.text().startswith('待核'),d.status.text()
    d.orders.selectRow(0);ui.press(d,'冻结合并采购')
    v=json.loads(d.detail.toPlainText())
    assert Decimal(v['total_amount'])==Decimal('9.99') and Decimal(v['freight'])==5 and Decimal(v['rounding_delta'])==Decimal('-.01'),v
    ui.snap(d,'价格和运费');ui.output(d,'付款版本对账')
    return v


def cancellation(ui):
    d=ui.desk('policy')
    ui.publish(d,simple(ui,'policy','PART',4));ui.preview(d);ui.claim(d);orig=ui.proof(d,0);ui.pay(d,0)
    p=d.packages[0];d.orders.selectRow(0)
    false={**orig,'status':'已取消','rows':[{**r,'quantity':'0','amount':'0','final_evidence':'尚未取得原单取消回读'} for r in orig['rows']]}
    set_file(ui,'未证实取消',false);ui.press(d,'核对聚水潭原件')
    assert d.status.text().startswith('待核')
    assert ui.preview(d)['rows'][0]['quantity']=='0'
    obj=ui.proof(d,0,0,True,'已取消')
    set_file(ui,'重复回执',obj);d.tabs.setCurrentIndex(1);d.orders.selectRow(0);ui.press(d,'核对聚水潭原件')
    assert ui.preview(d)['rows'][0]['quantity']=='4'
    ui.claim(d)
    assert ui.preview(d)['rows'][0]['quantity']=='0'
    with sp.connection(d.path) as db:
        releases=db.execute('SELECT gid,sku,qty FROM special_releases').fetchall()
        old=sp.latest(db,p['oid'],'付款')
    assert len(releases)==1 and old['version']==1
    ui.output(d,'取消重做对账');ui.snap(d,'旧款与新代次')
    return dict(release=releases,old_paid_version=old['version'],gids=[p['gid'] for p in d.packages])


def source_rows(ui):
    d=ui.desk('opening')
    v=simple(ui,'opening','PART',18)
    v['lines'] += [dict(sku='PART',target=20,unit='件'),dict(sku='000001',target=1,unit='件'),dict(sku='000002',target=0,unit='件')]
    ui.publish(d,v);obj=ui.preview(d)
    items=obj['packets'][0]['items']
    assert len(obj['rows'])==4 and [r['sku'] for r in items]==['PART','000001'] and items[0]['quantity']=='38' and len(items[0]['claims'])==2
    ui.claim(d);ui.output(d,'原序与重复行');ui.snap(d,'单款源行份额')
    return dict(items=[(r['sku'],r['quantity'],r['claims']) for r in items],original_count=4)


def migrate(ui,case):
    from PySide6.QtWidgets import QPushButton
    from pathlib import Path
    import hashlib,zipfile
    if case=='migration-check':
        d=ui.desk('opening');assert len(d.packages)==4
        d.tabs.setCurrentIndex(1);d.orders.selectRow(0)
        d.tabs.setCurrentIndex(2);ui.press(d,'全部经营实绩')
        totals=json.loads(d.detail.toPlainText())
        assert totals['开业']['实收件数']=='1650'
        ui.output(d,'迁入后逐行');ui.snap(d,'迁入后四包')
        return dict(packages=[p['online'] for p in d.packages],actual=totals)
    if case=='migration-export':
        d=ui.desk('policy')
        for sku in ('K1','K2'):
            ui.publish(d,simple(ui,'policy',sku,1));ui.preview(d);ui.claim(d)
        d.close()
    ui.win.show_section(3)
    options=[b for b in ui.win.findChildren(QPushButton) if b.text().startswith('完整订单迁移／导入与参数导出')]
    if not options:raise ValueError('订单跟踪未找到完整迁移入口')
    button=next((b for b in options if b.isVisible()),options[0])
    archive=ui.root.parent/'专项完整迁移.zip'
    entry='导出完整订单迁移包…' if case=='migration-export' else '校验并导入完整订单迁移包…'
    ui.export=ui.file=str(archive)
    def inside(dialog):
        ui.snap(dialog,'完整迁移')
        ui.press(dialog,entry)
        if dialog.isVisible():dialog.close()
    ui.modal(button.click,inside)
    assert archive.is_file()
    with zipfile.ZipFile(archive) as z:
        m=json.loads(z.read('manifest.json'))
    if case=='migration-import':
        assert getattr(ui.win,'_order_import_request',None) or (ui.root/'.settings.order-migrations').is_dir()
    return dict(archive=str(archive),sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),counts=m['counts'])


def ambiguous(ui):
    d=ui.desk('opening')
    v=simple(ui,'opening','PART',2)
    ui.publish(d,v);ui.preview(d);ui.claim(d)
    ui.proof(d,0);ui.pay(d,0)
    v['lines'][0]['target']='4'
    ui.publish(d,v,True);ui.preview(d);ui.claim(d)
    ui.proof(d,1);ui.pay(d,1)
    d.orders.selectRow(0);ui.press(d,'冻结合并采购')
    assert '8.00' in d.status.text(),d.status.text()
    (ui.root/'short-receipt').touch()
    d.tabs.setCurrentIndex(1);d.orders.selectRow(0)
    ui.ax_flow(d,1,8)
    import special_settlement as ss
    g=ss.group_id(d.packages[0])
    with sp.connection(d.path) as db:
        initial=sp.latest(db,g,'正式实收')
        purchase=sp.latest(db,g,'采购预览')['body']['rows'][0]
    assert initial['body']['rows'][0]['pending']=='3'
    assigned=[dict(oid=r['oid'],line=r['line'],quantity=str(q)) for r,q in zip(purchase['sources'],(2,1))]
    a=initial['body']
    correction=dict(original=a['original'],task=a['task'],version=1,reason='依据原包到货件数人工认领',rows=[dict(sku='PART',unit='件',amount='6',quantity='3',shares=assigned)])
    set_file(ui,'组级原实收分摊',correction)
    d.tabs.setCurrentIndex(2);ui.press(d,'更正已入账实收');ui.press(d,'查看组级收货')
    with sp.connection(d.path) as db:
        final=sp.latest(db,g,'正式实收')
        ack=sp.latest(db,g,'原平台回执')
        actual=db.execute('SELECT count(*) FROM special_receipts').fetchone()[0]
    assert actual==1 and final['version']==2 and final['body']['rows'][0]['pending']=='0' and ack['body']['sha']==initial['sha']
    ui.snap(d,'原回执和当期认领');ui.output(d,'实收认领两版本')
    return dict(original=a['original'],received=3,unassigned_before=3,unassigned_after=0,versions=[initial['sha'],final['sha']],linked_ack=ack['body'],formal_records=actual)
