"""Independent, synthetic warehouse and order inputs for Windows acceptance."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
import openpyxl

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import warehouse_catalog
import work_repository as wr
import normal_orders

def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')

def scope(wh='山东仓'):
    code='SD' if wh=='山东仓' else 'HB'
    return dict(warehouse=wh,account='2' if code=='SD' else '1',shop='44' if code=='SD' else '22',merchant='TEST-MERCHANT',supplier=code+'-SUP',currency='CNY',settlement='现结')

def definition(title,stores,lines,**k):
    return dict(kind=k.pop('kind','policy'),title=title,stores=stores,lines=lines,scope=scope(),
                start='2026-01-01T00:00:00+08:00',end='2027-01-01T00:00:00+08:00',**k)

def catalog(root,wh,n,second=False):
    folder=root/'inputs'
    xlsx=folder/(wh+('-第二版' if second else '-第一版')+'.xlsx')
    book=openpyxl.Workbook()
    sheet=book.active
    sheet.title='商品资料'
    sheet.append(['商品编码','商品名称','分类','基本售价','出库箱规','商品状态','可用数','商品单位','商品标签'])
    for i in range(1,n+1):
        sku=f'{i:06}'
        qty=1 if wh=='山东仓' else 8
        status='启用'
        if not second and wh=='山东仓' and 1606<=i<=1649:
            qty=0
        if not second and wh=='山东仓' and i==1650:
            status='禁用'
        sheet.append([sku,'样品'+sku,'仓库商品',2,1,status,qty,'件'])
    for sku,qty,step,status,unit in [('SHARED',10 if wh=='山东仓' else 8,1,'启用','件'),('FAIR',7 if not second else 12,1,'启用','件'),
          ('BOX',100,12,'启用','件'),('HOLD',100,1,'启用','件'),('DISABLED',40,1,'禁用','件'),
          ('UNIT',40,1,'启用',None),('PART',40,1,'启用','件'),('K1',15,1,'启用','件'),('K2',15,1,'启用','件')]:
        sheet.append([sku,'样品'+sku,'门店耗材',2,step,status,qty,unit])
    book.save(xlsx)
    return warehouse_catalog.import_catalog(xlsx,wh)

def setup(root,n=2000):
    root.mkdir(parents=True,exist_ok=False)
    (root/'inputs').mkdir()
    settings=root/'settings'
    w=wr.Repository(settings)
    normal_orders.Repository(w)
    for wh in ('山东仓','河北仓'):
        data=catalog(root,wh,n)
        warehouse_catalog.Repository(settings).save(data)
        changed=catalog(root,wh,n,True)
        dump(root/'inputs'/(wh+'-refresh.json'),changed)
    from contextlib import closing
    with closing(w.connect()) as db,db:
        import lifecycle_store, payment_shop_names
        lifecycle_store.schema(db)
        for code in ('A','B','C','D','E'):
            db.execute('INSERT OR IGNORE INTO stores VALUES(?,?)',(code,wr.dumps({'name':'同名测试门店' if code in ('A','D') else '测试店'+code})))
            for wh in ('山东仓','河北仓'):
                entry=dict(store=code,warehouse=wh,name='隔离样本店'+code,version='qa-fixture')
                db.execute("INSERT INTO lifecycle_mappings(stage,name,payload) VALUES('payment',?,?)",(payment_shop_names.key(code,wh),wr.dumps(entry)))
    from jst_delivery_profiles import save_profile
    for wh in ('山东仓','河北仓'):
        for code in ('A','B','C','D','E'):
            save_profile(w.path,code,wh,dict(buyer='隔离买家',recipient='测试收货',phone='13000000000',
                province='测试省',city='测试市',district='测试区',address='隔离样本地址'),None)
    dump(root/'synthetic.json',dict(synthetic=True,live_platform=False))
    lines=[dict(sku=f'{i:06}',unit='件',target='1',source_row=i+1) for i in range(1,1651)]
    dump(root/'inputs/opening.json',definition('开业 1650',['A'],lines,kind='opening'))
    dump(root/'inputs/fair.json',definition('三店覆盖',['A','B','C'],[dict(sku='FAIR',unit='件',target='4')]))
    dump(root/'inputs/priority.json',definition('三店优先',['A','B','C'],[dict(sku='FAIR',unit='件',target='4')],strategy='优先配齐'))
    dump(root/'inputs/hold.json',definition('门店持有',['A'],[dict(sku='HOLD',unit='件',target='10')],mode='持有下限'))
    dump(root/'inputs/scope.json',definition('冻结选店',['A','B','C','D','E'],[dict(sku=s,unit='件',target='2') for s in ('K1','K2','HOLD','BOX')],
         mode='任选款',k=2,require_confirmation=True,store_types={'A':'一类','B':'一类','C':'一类','D':'二类','E':'二类'},store_type='一类',store_count=2))
    dump(root/'inputs/boxes.json',definition('包装与准入',['A'],[dict(sku=s,unit='件',target='10',strict=s=='BOX') for s in ('BOX','PART','DISABLED','ABSENT','UNIT')]))
    return w.path

def contention(root):
    """Freeze real legacy ordinary and material generation rows in this isolated test copy."""
    from contextlib import closing
    sys.path.insert(0,str(ROOT/'tests'))
    from test_archive_orders import seed
    from test_lifecycle import LifecycleTests
    import jst_batches, order_lifecycle, material_orders, material_requests
    work,_,payload=seed(root)
    r=payload['result']
    sd=next(row for row in r['rows'] if row['AU']=='山东仓')
    sd.update(E='SHARED',G='件',D='样品SHARED')
    with closing(work.connect()) as db,db:
        db.execute('INSERT OR IGNORE INTO stores VALUES(?,?)',('T',wr.dumps(dict(name='同一测试店'))))
        db.execute('UPDATE normal_revisions SET digest=?,payload=? WHERE oid=? AND revision=1',
                   (normal_orders.fingerprint(r),normal_orders.encode(payload,db),'oid'))
    sample=LifecycleTests()
    sample.root,sample.count,sample.path=root,0,work.path
    sample.h=next(h for h in order_lifecycle.current_targets(work.path) if h['warehouse']=='山东仓')
    sample.save('return',[{'供货仓':'山东仓','商品编码':'SHARED','门店确认数量':4}])
    cat=warehouse_catalog.Repository(work.path.parent).snapshots(('山东仓',))[0]
    generated=jst_batches.execute(work.path,jst_batches.preview_new(work.path,[sample.h],cat))
    assert not generated['errors'],generated
    with closing(work.connect()) as db,db:
        req,ver=material_requests.save(db,'T','山东仓',[dict(sku='SHARED',quantity=2)])
    v=material_orders.preview(work.path,[dict(request=req,version=ver,sku='SHARED')])
    assert v['payload']['rows'][0]['quantity']=='2',v['payload']['rows']
    material_orders.commit(work.path,material_orders.register(work.path,v))

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('folder',type=Path)
    p.add_argument('--size',type=int,default=2000)
    a=p.parse_args()
    print(setup(a.folder,a.size))
