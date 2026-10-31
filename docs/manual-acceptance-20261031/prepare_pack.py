"""Build/verify wholly synthetic business acceptance fixtures; never user data."""
import argparse,copy,hashlib,json,sys,shutil,sqlite3
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

DAY='2026-10-31'
STORES={'UAT001':'练习甲店','UAT002':'练习乙店'}
NAMES=['稳定动销饮料','整箱纸巾','主仓禁用转从仓','主仓无货转从仓','仅河北有货','双仓禁用','门店停采','地采商品','缺箱规未知分类','缺价商品','关注必卖新品','往期新品','缺货天数冲突','已有在途纸巾','零销缺货恢复','库存充足']

def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes((json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n').encode())

def spec(file,headers,rows,sheet='数据'):
    return {'file':file,'sheets':[{'name':sheet,'headers':headers,'rows':rows}]}

def inputs(out):
    out.mkdir(parents=True,exist_ok=True)
    masters=[]
    for wh in ['河北仓','山东仓']:
        rows=[]
        for i,name in enumerate(NAMES,1):
            if i==8 or wh=='山东仓' and i==5:continue
            rows.append([f'{i:04d}',name,'未知分类' if i==9 else '日百' if i in (2,14) else '饮品',None if i==10 else (3 if wh=='山东仓' else 2),None if i==9 else 6 if i in (2,3,11) else 24 if i==14 else 1,'禁用' if i==6 or i==3 and wh=='河北仓' else '启用',0 if i==4 and wh=='河北仓' else 500,'练习原表保留字段'])
        masters.append(spec('01_补货输入/'+wh+'商品资料.xlsx',['商品编码','商品名称','分类','基本售价','出库箱规','商品状态','可用数','原表备注'],rows,'Sheet1'))
    hs=['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','在途库存天数','缺货天数（天）','补货状态','基础单位','供应商编码','商品条码']
    rows=[]
    for code,name in STORES.items():
        for i,item in enumerate(NAMES,1):
            h,w,m,k,l,n=1,7,30,0,0,0
            if i in (11,12):h=w=m=0;n=None if i==11 else 0
            if i==13:n=45
            if i==14:h,w,m,l=1.71,14,38,24
            if i==15:h=w=m=0;n=12
            if i==16:k=1000
            rows.append([code,name,f'{i:04d}',item,h,w,m,k,l,0,n,'停采' if i==7 else '正常','个','UAT-SUP',f'690000000{i:04d}'])
    values=masters+[spec('01_补货输入/两店补货建议.xlsx',hs,rows,'商品补货建议'),spec('01_补货输入/必卖清单.xlsx',['商品编码','商品名称'],[['0001',NAMES[0]],['0011',NAMES[10]]]),spec('01_补货输入/新品关注清单.xlsx',['商品编码','商品名称','关注'],[['0011',NAMES[10],'是'],['0012',NAMES[11],'否']])]
    conflict=copy.deepcopy(rows[:1]);conflict[0][6]=60
    values.append(spec('04_异常练习/同店同品冲突.xlsx',hs,conflict,'商品补货建议'))
    dump(out/'inputs-spec.json',values)

def seed(source,out,package):
    sys.path.insert(0,str(source/'src'))
    import batch_service as batch,warehouse_catalog as wc,priority_list as pl,new_product_focus as focus
    import work_repository as wr,normal_orders as no,normal_exports as ne,new_products as np
    import parameter_schema as ps,source_archive,store_defaults,version_info,result_semantics as sem
    folder=package/'seed/settings';work=wr.Repository(folder);repo=no.Repository(work)
    inputs=package/'01_补货输入';catalogs={w:wc.import_catalog(inputs/(w+'商品资料.xlsx'),w) for w in wc.WAREHOUSES}
    for c in catalogs.values():wc.Repository(folder).save(c)
    profile=pl.import_profile(inputs/'必卖清单.xlsx');pl.Repository(folder).save(profile)
    fpath=inputs/'新品关注清单.xlsx';f=focus.import_list({'path':str(fpath),'sha256':hashlib.sha256(fpath.read_bytes()).hexdigest()},'数据','商品编码','关注',DAY[:7]);focus.Repository(folder).save(f)
    sales=inputs/'两店补货建议.xlsx';loaded=batch.load([sales],catalogs)
    c=ps.standard();preset=work.save_preset('练习标准方案',c);results={};settings={};store_ui={}
    for s in STORES:
        route={'primary':'河北仓' if s=='UAT001' else '山东仓','secondary':'山东仓' if s=='UAT001' else '河北仓'}
        settings[s]={'warehouses':copy.deepcopy(c['warehouses']),'exception':True,'budget':None,'parameter_config':copy.deepcopy(c),'supply_route':route,'preset':{k:v for k,v in preset.items() if k!='config'},'parameter_signature':ps.signature(c)}
        result=batch.calculate_store(loaded,s,settings[s],profile)
        results[s]=np.apply(result,{'0011':6})
        store_ui[s]={'mode':1,'amount':'','selected':True,**route}
    payload={'schema':1,'day':DAY,'app_version':'0.17.1','model_version':version_info.MODEL_VERSION,'loaded':loaded,'states':{s:{'state':'已完成','name':n} for s,n in STORES.items()},'results':results,'catalogs':catalogs,'profile':profile,'new_focus':f,'input_dirty':False,'store_presets':{s:copy.deepcopy(preset) for s in STORES},'overrides':{s:copy.deepcopy(c['warehouses']) for s in STORES},'default_by_store':{},'store_ui':store_ui,'sales':[str(sales)],'products':'','legacy':False,'concurrency':2,'exception':True,'export_log':[],'pending_copy':None}
    for p in inputs.glob('*.xlsx'):source_archive.capture(work.path,p,hashlib.sha256(p.read_bytes()).hexdigest())
    work.save_stores({s:store_defaults.record(preset,c,u,include_budget=True,saved=wr.now()) for s,u in store_ui.items()})
    with patch.object(wr,'today',return_value=DAY):orders=repo.confirm(payload,list(STORES),None,{s:None for s in STORES})
    expected=[];specs=[];mixed={s:[] for s in ['return','payment','receipt']};line_checks=[]
    from warehouse_orders import partitions,child_number
    import lifecycle_io as io
    signed=package/'02_原签发参考';signed.mkdir(exist_ok=True)
    for order in orders:
        for wh,groups in partitions(order['payload']['result'],f).items():
            number=child_number(order['number'],wh);ne.export(order,signed/ne.filename(order,wh),warehouse=wh)
            rs=[d for rows in groups.values() for d in rows];positive=[d for d in rs if sem.order_quantity(d)>0]
            ret=[];pay=[];receipt=[];rows_by_stage={}
            for i,d in enumerate(rs):
                q=sem.order_quantity(d);step=d['BD'];r=q if q>0 else 0
                index=next((j for j,x in enumerate(positive) if x['E']==d['E']),-1)
                kind='数值'
                if index==0:r=q+2*step
                elif index==1:r=0;kind='显式0'
                elif index==2:r=None;kind='空白'
                elif index==3:r=0;kind='缺行'
                if kind!='缺行':ret.append([number,d['E'],r,d['D']])
                eff=0 if r is None else r
                p=max(0,eff-step) if index==0 else eff
                got=max(0,p-step) if index==0 else p
                # Synthetic payment supplies its own price for the intentionally unpriced master SKU.
                if eff>0:pay.append([number,d['E'],p,'PAY-'+order['store']+'-'+wh,p*(d['V'] if d['V'] is not None else 2),'CNY',d['D']])
                if p>0:receipt.append([number,d['E'],got,'REC-'+order['store']+'-'+wh,DAY,d['D']])
                line_checks.append([number,wh,d['E'],d['D'],q,kind,eff,eff-q,p,p-eff,got,got-p])
            for stage,rows in [('return',ret),('payment',pay),('receipt',receipt)]:
                headers={'return':['补货单号','商品编码','门店确认数量','商品名称'],'payment':['补货单号','商品编码','付款单商品数量','付款单号','实际金额','币种','商品名称'],'receipt':['补货单号','商品编码','实际收货数量','收货单号','收货日期','商品名称']}[stage]
                file='03_生命周期单据/'+io.TITLES[stage]+'/'+order['store']+'-'+wh+'.xlsx'
                specs.append(spec(file,headers,rows,io.TITLES[stage]));mixed[stage]+=rows;rows_by_stage[stage]=(headers,rows)
            amounts=[sem.order_amount(d) for d in rs]
            expected.append({'number':number,'store':order['store'],'warehouse':wh,'signed_rows':len(rs),'signed_quantity':sum(sem.order_quantity(d) for d in rs),'known_signed_amount':sum(a for a in amounts if isinstance(a,(int,float))),'unpriced_items':sum(not isinstance(a,(int,float)) for a in amounts)})
    hs={s:next(x['sheets'][0]['headers'] for x in specs if '/'+io.TITLES[s]+'/' in x['file']) for s in mixed}
    specs.append({'file':'03_生命周期单据/混合三环节_十二单.xlsx','sheets':[{'name':io.TITLES[s],'headers':hs[s],'rows':mixed[s]} for s in ('receipt','payment','return')]})
    first=copy.deepcopy(mixed['return'][0]);first[2]=-1
    specs.append(spec('04_异常练习/回传负数量.xlsx',hs['return'],[first],'门店回传单'))
    first=copy.deepcopy(mixed['return'][0]);first[0]='BH-NOT-EXISTS-ZC-HB'
    specs.append(spec('04_异常练习/回传未知单号.xlsx',hs['return'],[first],'门店回传单'))
    first=copy.deepcopy(mixed['return'][0]);first[1]='9999';first[2]=2
    specs.append(spec('04_异常练习/回传新增商品.xlsx',hs['return'],[first],'门店回传单'))
    first_rows=[copy.deepcopy(r) for r in mixed['return'] if r[0]==mixed['return'][0][0]];first_rows[0][2]=first_rows[0][2]+1
    specs.append(spec('04_异常练习/同单第二份回传.xlsx',hs['return'],first_rows,'门店回传单'))
    specs.append(spec('04_异常练习/回传列顺序变化.xlsx',list(reversed(hs['return'])),[list(reversed(r)) for r in mixed['return'] if r[0]==mixed['return'][0][0]],'门店回传单'))
    rows=[]
    for r in mixed['payment']:
        if r[0]!=mixed['payment'][0][0]:continue
        rows.append(['UAT-PAY-001','UAT-ONLINE-001','练习甲店','UAT-ACCOUNT',DAY+' 12:00:00','线下店铺',r[1],r[6],1,r[2],(r[4]/r[2] if r[2] else 2),r[4],''])
    specs.append(spec('04_异常练习/原格式付款单_需绑定.xlsx',io.PAYMENT_HEADERS,rows,'原付款明细'))
    specs.append(spec('05_预期结果/逐商品核对答案.xlsx',['补货单号','仓','商品编码','商品名称','签发量','回传输入形式','回传有效量','回传减签发','付款量','付款减回传','收货量','收货减付款'],line_checks,'逐商品答案'))
    baseline=[{'store':s,'sku':d['E'],'warehouse':d.get('AU'),'AS':d.get('AS'),'execution':sem.order_quantity(d),'amount':sem.order_amount(d),'source':d.get('_source'),'N':d.get('N'),'BA':d.get('BA'),'BD':d.get('BD'),'V':d.get('V')} for s,r in results.items() for d in r['rows']]
    # Concrete business facts, not an automated pass mark for a human tester.
    dump(out/'lifecycle-spec.json',specs);dump(package/'05_预期结果/expected.json',{'synthetic':True,'day':DAY,'orders':expected,'items':line_checks,'calculations':baseline})
    dump(out/'seed-payload.json',payload)
    print(json.dumps({'orders':expected,'spec_count':len(specs)},ensure_ascii=False))

def verify(source,package,out):
    out.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(source/'src'))
    import lifecycle_batch as batch,order_lifecycle as lc,purchases,work_repository as wr
    import tempfile
    output=Path(tempfile.mkdtemp(prefix='verify-',dir=out));db=output/'workbench.sqlite3';shutil.copy2(package/'seed/settings/workbench.sqlite3',db)
    hashes={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*') if p.is_file() and '练习记录' not in p.parts}
    def frozen():
        with closing(sqlite3.connect(db)) as c:return {t:c.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ['jobs','normal_orders','normal_revisions','normal_work_revisions','source_files']}
    before=frozen();checks=[]
    files=list((package/'03_生命周期单据').glob('*/*.xlsx'))
    data=batch.read(list(reversed(files)))
    if data['issues'] or len(data['documents'])!=12:raise AssertionError(data['issues'])
    plan=batch.prepare(db,data['documents']);lc.commit(db,plan)
    heads=purchases.targets(db);expected=json.loads((package/'05_预期结果/expected.json').read_text(encoding='utf-8'))
    count=0
    for h in heads:
        view=lc.snapshot(db,h)
        # UI rows: SKU/name/unit/signed/return/payment/receipt/... (checked below).
        if view['status']!='已收齐':raise AssertionError((h['number'],view['status']))
        rows=[r for r in expected['items'] if r[0]==h['number']]
        if [r[0] for r in view['rows']]!=[r[2] for r in rows]:raise AssertionError('SKU order differs')
        for actual,want in zip(view['rows'],rows):
            target=[want[4],want[6],want[8] if want[6]>0 else None,want[10] if want[8]>0 else None]
            if actual[2:6]!=target:raise AssertionError((h['number'],actual,target))
        checks.append({'number':h['number'],'all_sku_quantities_and_order_equal':True,'rows':len(rows)})
        count+=1
    if frozen()!=before:raise AssertionError('signed source tables changed')
    checks.append('12 separate documents, reverse stage order, atomic save, four completed warehouse orders; original five tables unchanged')
    combined=output/'combined.sqlite3';shutil.copy2(package/'seed/settings/workbench.sqlite3',combined)
    data=batch.read([package/'03_生命周期单据/混合三环节_十二单.xlsx']);plan=batch.prepare(combined,data['documents']);lc.commit(combined,plan)
    if len(plan['plans'])!=12:raise AssertionError('mixed workbook not 12 documents')
    for h in heads:
        a=lc.snapshot(db,h);b=lc.snapshot(combined,h)
        if a['rows']!=b['rows'] or a['status']!=b['status']:raise AssertionError('mixed and separate differ')
    checks.append('one workbook with three reversed-stage sheets resolves to same 12 documents')
    def rejected(filename):
        read=batch.read([package/'04_异常练习'/filename])
        if read['issues']:return read['issues'][0]['message']
        try:batch.prepare(db,read['documents'])
        except ValueError as e:return str(e)
        raise AssertionError('unexpected acceptance '+filename)
    checks.append({'negative_quantity_rejected':rejected('回传负数量.xlsx'),'unknown_order_rejected':rejected('回传未知单号.xlsx'),'unnumbered_requires_binding':rejected('原格式付款单_需绑定.xlsx')})
    duplicate=batch.read([package/'03_生命周期单据/门店回传单/UAT001-河北仓.xlsx',package/'04_异常练习/同单第二份回传.xlsx'])
    try:batch.prepare(db,duplicate['documents'])
    except ValueError as e:checks.append({'duplicate_candidates_rejected':str(e)})
    else:raise AssertionError('duplicate accepted')
    extra=batch.prepare(db,batch.read([package/'04_异常练习/回传新增商品.xlsx'])['documents'])
    if extra['plans'][0]['payload']['extra']!=['9999']:raise AssertionError('extra SKU not visible')
    checks.append('extra SKU 9999 explicitly shown; preview not committed')
    reordered=batch.prepare(db,batch.read([package/'04_异常练习/回传列顺序变化.xlsx'])['documents'])
    normal=batch.prepare(db,batch.read([package/'03_生命周期单据/门店回传单/UAT001-河北仓.xlsx'])['documents'])
    columns=lambda p:[(r['sku'],r['base_quantity'],r['quantity_origin'],r['upstream_quantity'],r['difference']) for r in p['plans'][0]['payload']['rows']]
    if columns(reordered)!=columns(normal):raise AssertionError('reordered columns differ')
    checks.append('header-based mapping preserves row order, quantity, origin, basis and difference')
    signed=batch.read(list((package/'02_原签发参考').glob('*.xlsx')))
    if signed['issues'] or len(signed['documents'])!=4:raise AssertionError(('signed layout not recognized',signed['issues']))
    direct=batch.prepare(db,signed['documents'])
    for p in direct['plans']:
        for r in p['payload']['rows']:
            if float(r['base_quantity'])!=float(r['upstream_quantity']):raise AssertionError('signed quantity alias changed')
    checks.append('four original signed-layout workbooks auto-detected as return, quantities equal signed basis')
    paid=batch.read([package/'04_异常练习/原格式付款单_需绑定.xlsx']);paid['documents'][0]['target']=next(h for h in heads if h['number']=='BH-20261031-UAT001-ZC-HB')
    bound=batch.prepare(db,paid['documents'])
    if bound['plans'][0]['payload']['pending']:raise AssertionError('bound payment pending')
    checks.append('13-column payment file resolves after explicit whole-order binding')
    from threading import Event
    from runtime_support import Cancelled
    stop=Event();stop.set()
    try:batch.read(files,cancel=stop)
    except Cancelled:checks.append('cancel before read leaves data unchanged; clean retry already passed')
    else:raise AssertionError('cancel ignored')
    import source_archive,history_read
    with closing(sqlite3.connect(db)) as c:
        for p in (package/'01_补货输入').glob('*仓商品资料.xlsx'):
            sha=hashlib.sha256(p.read_bytes()).hexdigest();raw=source_archive.read(c,sha)
            if raw[1]!=p.read_bytes():raise AssertionError('source bytes differ')
    checks.append('both original master workbooks retained byte-for-byte')
    progress=[];visible=history_read.execute(db,output/'index.sqlite3',{'kind':'list','from':DAY,'to':DAY},Event(),progress.append)
    if len(visible['records'])!=4 or progress[-1]!={'done':2,'total':2}:raise AssertionError('history query mismatch')
    checks.append('four warehouse history rows and two order progress events')
    if frozen()!=before:raise AssertionError('signed source tables changed after negative previews')
    after={str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*') if p.is_file() and '练习记录' not in p.parts}
    if hashes!=after:raise AssertionError('package modified')
    checks.append('all package input and seed hashes unchanged')
    import version_info
    dump(out/('validation-'+version_info.VERSION+'.json'),{'passed':True,'checks':checks,'warehouse_orders':count,'sku_rows':sum(x['signed_rows'] for x in expected['orders']),'manual_execution_status':'未由业务人员执行','app_version':version_info.VERSION,'source_root':str(source),'allowed_comparison_differences':'none in compared UI business rows; source file provenance intentionally differs for mixed vs separate files','original_tables_unchanged':['jobs','normal_orders','normal_revisions','normal_work_revisions','source_files']})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['inputs','seed','verify']);p.add_argument('--source',type=Path);p.add_argument('--out',type=Path,required=True);p.add_argument('--package',type=Path);a=p.parse_args()
    if a.action=='inputs':inputs(a.out)
    elif a.action=='seed':seed(a.source.resolve(),a.out,a.package.resolve())
    else:verify(a.source.resolve(),a.package.resolve(),a.out)
