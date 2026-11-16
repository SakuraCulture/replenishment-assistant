"""Synthetic inputs for the opt-in native carton allocation acceptance."""
import copy, hashlib, json, sqlite3, sys
from pathlib import Path
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def fixture(out):
    import openpyxl
    import test_archive_orders as seed, normal_orders as no, result_semantics as sem
    import warehouse_catalog as wc, lifecycle_io as io, order_lifecycle as lc
    import operations_flow, export_settings, work_repository as wr
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    work,_,payload=seed.seed(out)
    # Same SKU in both warehouses deliberately has different stock and carton.
    cases=[('河北仓','0001',1090,15,5000,1095,'启用'),
           ('河北仓','0002',800,24,5000,816,'启用'),
           ('河北仓','0003',500,12,5000,504,'启用'),
           ('河北仓','0004',1,24,100,24,'备用'),
           ('河北仓','0005',48,24,23,23,'启用'),
           ('河北仓','0006',48,24,100,48,'启用'),
           ('河北仓','0007',0,24,100,0,'启用'),
           ('河北仓','0008',1090,15,1094,1094,'启用'),
           ('山东仓','0001',48,12,30,30,'启用')]
    original=payload['result']['rows'][0];rows=[]
    for wh,sku,demand,step,stock,expected,status in cases:
        row=copy.deepcopy(original);row.update(E=sku,AU=wh,D='合成商品'+sku)
        rows.append(row)
    payload['result']['rows']=rows;payload['result']['summary']=sem.summarize(rows)
    with closing(work.connect()) as db,db:
        db.execute('UPDATE normal_revisions SET digest=?,payload=? WHERE oid=? AND revision=?',
                   (no.fingerprint(payload['result']),no.encode(payload,db),'oid',1))
    heads=lc.current_targets(work.path)
    for wh in wc.WAREHOUSES:
        file=out/(wh+'-合成仓资料.xlsx');book=openpyxl.Workbook();ws=book.active
        headers=list(dict.fromkeys([*wc.FIELDS,'商品标签']));ws.append(headers)
        for warehouse,sku,demand,step,stock,expected,status in cases:
            if warehouse!=wh:continue
            item={'商品编码':sku,'商品名称':'合成商品'+sku,'分类':'测试','基本售价':2,
                  '出库箱规':step,'商品状态':status,'可用数':stock,'商品标签':'合成验收'}
            ws.append([item.get(k) for k in headers])
        book.save(file);book.close();wc.Repository(out/'settings').save(wc.import_catalog(file,wh))
    file=out/'合成回传.xlsx';book=openpyxl.Workbook();ws=book.active;ws.title='门店回传单';ws.append(io.fields('return'))
    for wh,sku,demand,step,stock,expected,status in cases:
        h=next(h for h in heads if h['warehouse']==wh)
        row={'补货单号':h['number'],'签发修订':h['revision'],'门店编码':h['store'],
             '供货仓':wh,'商品编码':sku,'商品名称':'合成商品'+sku,'门店确认数量':demand,
             '数量单位':'库存单位','回传单号':h['number']}
        ws.append([row.get(k) for k in io.fields('return')])
    book.save(file);book.close();info=io.inspect([file])[0];sheet=info['sheets'][0]
    scan=io.scan('return',[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
    lc.commit(work.path,lc.prepare_numbered(work.path,scan));operations_flow.adopt(work.path,heads)
    (out/'exports').mkdir();export_settings.save(out/'settings/export_settings.json',str(out/'exports'))
    marker={'synthetic':True,'live_platform':False,'heads':heads,'cases':cases,
            'return_digests':{h['warehouse']:lc.snapshot(work.path,h)['documents']['return']['digest'] for h in heads}}
    (out/'synthetic.json').write_text(json.dumps(marker,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'fixture':str(out),'cases':len(cases),'orders':len(heads)},ensure_ascii=False))


if __name__=='__main__':fixture(sys.argv[1])
