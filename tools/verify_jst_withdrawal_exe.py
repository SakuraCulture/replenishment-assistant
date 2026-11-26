"""Seed repeatable cases and exercise the extracted Windows app's normal JST controls."""
import argparse
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import zipfile

from verify_jst_one_click_exe import contents, read, sha, write
from verify_platform_import_failure_exe import run


def seed_orders(root, scenarios):
    import jst_auto_order as automatic
    import jst_auto_order_store as tasks
    import jst_batches
    import jst_order_store as allocations
    from jst_one_click_fixture import SKUS
    from platform_import_manual_fixture import JstManual
    import normal_orders
    import payment_shop_names
    import purchases
    from test_archive_orders import seed as signed
    from test_jst_auto_order import CONTACT
    from test_jst_one_click import OneClick
    from test_lifecycle import LifecycleTests
    import work_repository as wr

    root.mkdir(parents=True)
    sample = LifecycleTests()
    sample.root = root
    sample.work, sample.repo, sample.payload = signed(root)
    sample.path, sample.count = sample.work.path, 0
    case = {}
    with closing(sample.work.connect()) as db, db:
        db.execute("DELETE FROM normal_revisions WHERE oid='oid'")
        db.execute("DELETE FROM normal_orders WHERE oid='oid'")
        for i, (outcome, wh, demand) in enumerate(scenarios,1):
            oid, code, name = f'qa-{i:02}', f'QA{i:02}', f'验收门店{i:02}'
            p = deepcopy(sample.payload)
            p['result'].update(store=code,store_name=name,
                               rows=[r for r in p['result']['rows'] if r['AU'] == wh])
            number = 'BH-QA'+f'{i:02}'+'-ZC'
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',
                       (wr.today(),code,'normal_model',oid,number,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',
                       (oid,1,normal_orders.fingerprint(p['result']),wr.now(),normal_orders.encode(p,db)))
            case[oid] = dict(outcome=outcome,warehouse=wh,demand=demand)
    sample.heads = purchases.targets(sample.path)
    assert len(sample.heads)==len(scenarios), [h['number'] for h in sample.heads]
    sample.h = sample.heads[0]
    def demand_rows(count_key):
        return [{'补货单号':h['number'],'门店编码':h['store'],'供货仓':h['warehouse'],
                 '商品编码':SKUS[h['warehouse']],count_key:case[h['oid']]['demand']} for h in sample.heads]
    sample.save('return',demand_rows('门店确认数量'))
    sample.save('operations',demand_rows('门店确认数量'))
    payment_shop_names.save(sample.path,payment_shop_names.prepare(
        {'names':{h['store']:h['name'] for h in sample.heads},'records':{}},
        [{'store':h['store'],'warehouse':h['warehouse'],'value':h['name']+'聚水潭'} for h in sample.heads]))
    for h in sample.heads:
        automatic.save_profile(sample.path,h['store'],h['warehouse'],CONTACT,None)
    scenario = OneClick()
    scenario.path = sample.path
    scenario.component = JstManual(root/'component')
    with scenario.sync_once(100):
        for wh in ('河北仓','山东仓'):
            scenario.refresh(wh)
    batch = jst_batches.preview_new(sample.path,sample.heads)
    saved = jst_batches.execute(sample.path,batch)
    assert len(saved['generated'])==len(sample.heads) and not saved['errors'], saved
    with closing(wr.read_connection(sample.path)) as db:
        selected = [{'head':h,'gid':allocations.active(db,h)} for h in sample.heads]
    prepared = automatic.prepare(sample.path,selected)
    assert len(prepared['ready'])==len(scenarios) and not prepared['failed'], prepared
    portal = JstManual(root/'component')
    assert all(r['ok'] for r in automatic.run_browser(sample.path,prepared['ready'],portal,'preview'))
    rows = {}
    with closing(tasks.connection(sample.path)) as db,db:
        for online in prepared['ready']:
            order = tasks.read(db,online)
            h = order['payload']['head']
            r = case[h['oid']]
            rows[h['number']] = {**r,'online':online,'gid':order['gid'],'file_sha':order['file_sha']}
            if r['outcome'] == 'control':
                continue
            proof = dict(order['proof'])
            at = '2026-11-25T12:15:17Z'
            if r['outcome']=='unsent':
                proof['import_not_sent'] = dict(online=online,file_sha=order['file_sha'],binding=proof['binding'],
                                                reason='最终导入控件未发送',observed_at=at)
            if r['outcome']=='rejected':
                proof['import_receipt'] = dict(online=online,success_count=0,fail_count=1,
                                               text='成功条数：0，失败条数：1',observed_at=at)
            tasks.update(db,order,'IMPORT_UNKNOWN',proof=proof,reason='已核对本仓企业、店铺和导入入口')
    return rows


def seed(root):
    import openpyxl
    import warehouse_catalog
    import work_repository as wr
    from jst_one_click_fixture import SKUS

    hb, sd = '河北仓', '山东仓'
    scenarios = [('unsent',sd,11),('rejected',hb,13),('cancelled',hb,7),('platform',sd,3)]
    scenarios += [('unknown',wh,2) for wh in (sd,hb) for _ in range(5)]
    scenarios += [('control',sd,3),('control',hb,3)]
    rows = seed_orders(root,scenarios)
    latest = {sd:22,hb:29}
    for wh in (hb,sd):
        with closing(openpyxl.Workbook()) as book:
            book.active.append(warehouse_catalog.REQUIRED+['商品标签','商品名称'])
            book.active.append([SKUS[wh],'测试',2,1,'启用',latest[wh],'','合成商品'])
            book.save(root/(wh+'-最新库存.xlsx'))
    write(root/'import-failure-fixture.json',dict(synthetic=True,live_platform=False,
          rows=rows,day=wr.today(),fresh_stock=latest,expected_new={sd:{'unsent':'11'},hb:{'rejected':'13','cancelled':'7'}}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    exe=None
    if args.zip:
        with zipfile.ZipFile(args.zip) as archive:
            assert archive.testzip() is None
            archive.extractall(out/'unpacked')
        app=out/'unpacked'/'补货助手'
        files=contents(app)
        exe=app/'补货助手.exe'
        assert {'补货助手.exe','_internal/build_info.json','_internal/components/jst_catalog/integrity.manifest.json'} <= files.keys()
    case=out/'样本'
    seed(case)
    report=run(exe,case,'withdrawal')
    if exe:
        assert contents(app)==files and report['build']['build_id']==read(app/'_internal/build_info.json')['build_id']
    write(out/'report.json',dict(report=report,zip_sha256=sha(args.zip) if exe else None,
                                exe_sha256=sha(exe) if exe else None))
    print(out/'report.json')


if __name__=='__main__':
    main()
