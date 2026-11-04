"""Deterministic synthetic history for UI/scope/performance QA, never user data."""
import argparse
import copy
import json
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]


def prepare(out, stores=60, products=200):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    from test_archive_orders import seed
    from test_jst import catalog
    import normal_orders as no
    import work_repository as wr
    import storage_chunks as chunks
    import result_semantics as sem
    import order_lifecycle as lc
    import lifecycle_io as io
    import jst_batches as batches
    import jst_orders as jst
    import openpyxl
    work, _, original = seed(out, chunked=True)
    # All order quantities come from an unchanged model-generated fixture.
    with closing(work.connect()) as db, db:
        db.execute('DELETE FROM normal_orders')
        db.execute('DELETE FROM normal_revisions')
        known = chunks.known(db)
        for i in range(stores):
            p = copy.deepcopy(original)
            rows = []
            for n in range(products):
                d = copy.deepcopy(p['result']['rows'][n % 2])
                d['E'] = f'{n + 1:04d}'; d['D'] = '合成商品' + str(n + 1)
                rows.append(d)
            code = f'S{i:03d}'
            p['result'].update(store=code, store_name='合成门店' + code, rows=rows, summary=sem.summarize(rows))
            day = '2026-11-03' if i % 3 == 0 else '2026-11-04'
            digest = no.fingerprint_owned(p['result'], p.get('new_focus'))
            pack = chunks.prepare(p, known, binding=digest); raw = chunks.put(db, pack); known.update(pack.chunks)
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)', (day,code,'normal_model','oid-'+code,'BH-'+day.replace('-','')+'-'+code+'-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)', ('oid-'+code,1,digest,day+'T10:00:00',raw))
    heads = lc.current_targets(work.path)
    for stage, threshold in [('return',1), ('payment',2), ('receipt',3)]:
        book = openpyxl.Workbook(); sheet = book.active; sheet.title = '业务明细'; sheet.append(io.fields(stage))
        for h in heads:
            if int(h['store'][1:]) % 4 < threshold: continue
            row = {'补货单号':h['number'], '签发修订':1, '门店编码':h['store'], '供货仓':h['warehouse'],
                   '商品编码':'0001' if h['warehouse']=='河北仓' else '0002', '商品名称':'合成商品', '数量单位':'库存单位',
                   io.QUANTITIES[stage]:4, io.DOCNUM[stage]:'QA-'+stage+'-'+h['store'], io.DOCDATE[stage]:'2026-11-04',
                   '实际金额':8, '币种':'CNY'}
            sheet.append([row.get(f) for f in io.fields(stage)])
        path = out / (stage+'.xlsx'); book.save(path); book.close()
        file = io.inspect([path])[0]; page = file['sheets'][0]
        scan = io.scan(stage, [{**file, **page, 'mapping':{h:i for i,h in enumerate(page['headers'])}}])
        targets = lc.available(work.path,scan)
        bindings = {lc.binding_key(r):lc.matching(r,targets)[0] for r in scan['rows']}
        lc.commit(work.path,lc.prepare(work.path,scan,bindings))
    selected = [h for h in heads if h['warehouse']=='河北仓' and int(h['store'][1:]) % 4 == 1][:2]
    if len(selected)==2:
        plan = batches.preview_new(work.path,selected,{'河北仓':catalog(stock=100)},True)
        cancel = threading.Event()
        def progress(v):
            if getattr(v,'business_event',None):cancel.set()
        result = batches.execute(work.path,plan,progress=progress,cancel=cancel)
        if result['errors'] or len(result['generated'])!=1:raise AssertionError(result)
        jst.lock(work.path,result['generated'])
    meta = {'synthetic':True,'stores':stores,'signed_rows_per_store':products,'orders':len(heads),'database':str(work.path),
            'description':'合成店仓订单，日期/仓/四个生命周期状态/锁定/暂停批次；不是实际门店记录'}
    (out/'synthetic.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),'utf-8')
    return meta


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--out',type=Path,required=True)
    p.add_argument('--stores',type=int,default=60); p.add_argument('--products',type=int,default=200)
    a=p.parse_args(); print(json.dumps(prepare(a.out,a.stores,a.products),ensure_ascii=False))
