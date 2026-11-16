"""Offline measurements of actual local business entries; no application edits."""
import argparse,copy,gc,hashlib,json,os,sqlite3,sys,threading,time,traceback
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n','utf-8')


def fixed_serve(connection,path,cancel):
    source=Path(os.environ['FLOW_PERF_SOURCE']);sys.path[:0]=[str(source/'src')]
    import work_repository as wr
    wr.today=lambda:'2026-11-16';wr.now=lambda:'2026-11-16T12:00:00+08:00'
    from work_io_process import serve
    return serve(connection,path,cancel)

def main(a):
    sys.path[:0]=[str(a.source/'src')]
    # Reuse the locally archived CPython-compatible monitor, leaving bundled
    # application dependencies ahead of this optional monitoring dependency.
    sys.path.append(str(Path(__file__).resolve().parents[1]/'export_071/qa-deps'))
    import psutil,openpyxl
    import work_repository as wr,normal_orders as normal,engine,batch_service as calc
    import order_lifecycle as lc,lifecycle_batch as batch,lifecycle_io as io
    import warehouse_catalog as wc,jst_batches as jb,jst_orders as jo,jst_exports as jex
    import jst_payment as jp,jst_results as jr,operations_flow as ops,lifecycle_home
    import aoxiang_purchase as ax,order_analytics as analytics,order_analytics_exports as ae
    import ui_work_io
    ui_work_io.serve=fixed_serve
    os.environ['FLOW_PERF_SOURCE']=str(a.source)
    from ui_work_io import Session
    from datetime import timezone,timedelta
    import datetime as date_module
    class FixedDateTime(date_module.datetime):
        @classmethod
        def now(cls,tz=None):
            value=date_module.datetime(2026,11,16,12,tzinfo=timezone(timedelta(hours=8)))
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    analytics.datetime=FixedDateTime
    wr.today=lambda:'2026-11-16'
    a.output.mkdir(parents=True,exist_ok=False);dbroot=a.output/'settings';dbroot.mkdir()
    os.environ['REPLENISHMENT_DATA_DIR']=str(dbroot)
    (a.output/'synthetic.json').write_text('{"synthetic":true,"live_platform":false}','utf-8')
    # IDs and business clocks are reproducible. Normal calculation's diagnostic
    # task timestamps are excluded from the equality checks, not from execution.
    wr.now=lambda:'2026-11-16T12:00:00+08:00'
    report={'passed':False,'source':str(a.source),'version':json.loads((a.source/'src/version.json').read_text('utf-8'))['version'],
            'stores':a.stores,'input_skus_per_store':a.rows,'default_workers':2,'synthetic':True,'live_platform':False,
            'stages':[],'business_facts':{},'notes':['Process-tree private commit is sampled, not whole-PC physical usage.',
             'Process IO includes OS-cached IO. File generation by a user is fixture setup and excluded.',
             'No OS cache flush, no real platforms, no real settings, no source code writes.']}
    process=psutil.Process();phase='setup';all_started=time.perf_counter();last_progress=0
    def progress(value):
        nonlocal last_progress
        now=time.monotonic()
        if now-last_progress>=.5:
            write(a.output/'progress.json',{'stage':phase,'text':str(value),'at':now});last_progress=now
    def measure(name,fn):
        nonlocal phase
        phase=name;write(a.output/'active.json',{'phase':phase,'pid':os.getpid()})
        records={};initial={};peak=[0];peak_rss=[0];stop=threading.Event()
        def sample(first=False):
            private=rss=0
            for p in [process,*process.children(recursive=True)]:
                try:
                    key=(p.pid,p.create_time());mem=p.memory_info();cpu=p.cpu_times();disk=p.io_counters()
                    private+=getattr(mem,'private',mem.rss);rss+=mem.rss
                    records[key]=(cpu.user+cpu.system,disk.read_bytes,disk.write_bytes)
                    if first:initial[key]=records[key]
                except (psutil.NoSuchProcess,psutil.AccessDenied):pass
            peak[0]=max(peak[0],private);peak_rss[0]=max(peak_rss[0],rss)
        sample(True)
        def watch():
            while not stop.wait(.05):sample()
        thread=threading.Thread(target=watch,daemon=True);thread.start();start=time.perf_counter()
        profiler=None
        if a.profile:
            import cProfile
            profiler=cProfile.Profile();profiler.enable()
        try:value=fn()
        finally:
            elapsed=time.perf_counter()-start
            if profiler:
                profiler.disable();profiler.dump_stats(a.output/(name+'.pstats'))
            stop.set();thread.join();sample()
            totals=[sum(max(v[i]-initial.get(k,(0,0,0))[i],0) for k,v in records.items()) for i in range(3)]
            record={'phase':name,'seconds':elapsed,'process_tree_cpu_seconds':totals[0],
                    'peak_private_commit_mib':peak[0]/2**20,'peak_rss_sum_mib':peak_rss[0]/2**20,
                    'process_read_mib':totals[1]/2**20,'process_write_mib':totals[2]/2**20}
            report['stages'].append(record);write(a.output/'partial.json',report)
            print(json.dumps(record,ensure_ascii=False),flush=True)
        return value
    def signature(rows):
        digest=hashlib.sha256();count=0
        def normalized(value):
            if isinstance(value,Decimal):return str(value.normalize())
            if isinstance(value,dict):return {k:normalized(v) for k,v in value.items()}
            if isinstance(value,(tuple,list)):return [normalized(v) for v in value]
            return value
        for row in rows:digest.update(wr.dumps(normalized(row)).encode());digest.update(b'\n');count+=1
        return {'rows':count,'sha256':digest.hexdigest()}
    def checked_result(result):
        return signature([s,r['E'],r['AU'],str(Decimal(str(r['AS']))),str(Decimal(str(r['BV'])))]
                         for s in sorted(result) for r in result[s]['rows'])
    def book(path,headers,rows):
        b=openpyxl.Workbook(write_only=True);s=b.create_sheet('合成资料');s.append(headers)
        for row in rows:s.append(row)
        b.properties.created=b.properties.modified=datetime(2026,11,16,12)
        b.save(path);b.close()
    def canonical(value):return str(Decimal(str(value or 0)).normalize())
    try:
        inputs=a.output/'inputs';inputs.mkdir();sales=[];master=inputs/'合成商品资料.xlsx'
        cfg=engine.config();sf=list(dict.fromkeys(cfg['sales_fields'].values()));pf=list(dict.fromkeys([*cfg['product_fields'].values(),'仓归属','商品标签','可用数']))
        def items():
            for i in range(a.rows):
                row={'商品编码':f'5{i:012d}','商品名称':'合成商品'+str(i),'分类':'饮料','出库箱规':12,'基本售价':2,
                     '商品状态':'启用','仓归属':'河北仓' if i%2==0 else '山东仓','商品标签':'合成常规','可用数':100000}
                yield [row.get(k) for k in pf]
        book(master,pf,items())
        for n in range(0 if a.signed_path else a.stores):
            path=inputs/(f'合成门店S{n:03d}.xlsx');sales.append(path)
            def sales_rows(n=n):
                for i in range(a.rows):
                    row={'仓库/门店名称':'合成门店'+str(n),'仓库/门店编码':f'S{n:03d}','供应商编码':'SYN-SUP',
                         '商品编码':f'5{i:012d}','商品名称':'合成商品'+str(i),'商品条码':'690'+str(1000000000+i),
                         '单位类型':'库存单位','日销':2.86,'周销':24,'月销':73,'可用库存':0,'在途库存':0,
                         '在途库存天数':0,'缺货天数（天）':0,'商品标签':'','补货状态':'建议补货'}
                    yield [row.get(k) for k in sf]
            book(path,sf,sales_rows())
        require=lambda ok,text:None if ok else (_ for _ in ()).throw(AssertionError(text))
        if a.signed_path:
            require(a.signed_path.is_file(),'missing signed synthetic source')
            require((a.signed_path.parent.parent/'result.json').is_file(),'signed source has no passed receipt')
            require(json.loads((a.signed_path.parent.parent/'result.json').read_text('utf-8'))['passed'],'signed source failed')
            with closing(sqlite3.connect(a.signed_path.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(dbroot/'workbench.sqlite3')) as dst:src.backup(dst)
            work=wr.Repository(dbroot);repo=normal.Repository(work)
            codes=[h['store'] for h in repo.headers()]
            report['signed_seed']=str(a.signed_path)
        else:
            loaded=measure('01_input_read_prepare',lambda:calc.load(sales,master,progress))
            require(len(loaded['stores'])==a.stores,'input stores')
            settings={'warehouses':copy.deepcopy(engine.DEFAULT_WAREHOUSES),'exception':True,'budget':None}
            report['calculation_settings']=settings
            jobs={s:copy.deepcopy(settings) for s in sorted(loaded['stores'])};errors=[]
            def notify(s,state,result,error):
                if error:errors.append([s,error])
                progress(s+' '+state)
            calculated=measure('02_calculate_default_2_workers',lambda:calc.run(loaded,jobs,None,notify=notify))
            require(not errors and all(calculated.values()),'calculation errors: '+str(errors))
            report['business_facts']['calculation']=checked_result(calculated)
            payload={'schema':1,'day':wr.today(),'model_version':engine.MODEL_VERSION,'input_dirty':False,
                     'results':{s:calculated[s] for s in sorted(calculated)},'loaded':loaded,'catalogs':{},
                     'states':{s:{'name':loaded['stores'][s],'state':'已完成'} for s in calculated},'new_focus':None,
                     'store_presets':{s:settings for s in calculated},'export_log':[]}
            work=wr.Repository(dbroot);repo=normal.Repository(work);codes=list(payload['results'])
            def save_draft():
                with Session(work.path) as session:
                    prepared=session.call('prepare_save',payload,progress)
                    if not prepared['same']:return session.call('save',None,progress)
                    return prepared
            measure('03_draft_save_with_process_ipc',save_draft)
            measure('03b_draft_unchanged_save_with_process_ipc',save_draft)
            exports=a.output/'signed';exports.mkdir()
            with Session(work.path) as session:
                measure('04_sign_prepare_with_process_ipc',lambda:session.call('delivery_prepare',(payload,codes),progress))
                signed=measure('05_sign_commit_export_with_process_ipc',lambda:session.call('delivery_execute',str(exports),progress))
            require(len(repo.headers())==a.stores,'signed store count')
        request={'from':wr.today(),'to':wr.today(),'all_records':True}
        for name in ('cold','warm'):
            value=measure('06_list_'+name,lambda:lifecycle_home.read(work.path,a.output/'home-cache.sqlite3',request,progress=progress))
            report['business_facts']['list_'+name]={'displayed_records':len(value['records'])};del value
        heads=measure('07_load_lifecycle_targets',lambda:lc.current_targets(work.path,progress))
        require(len(heads)==a.stores*(1 if a.signed_path else 2),'warehouse target count')
        report['warehouse_orders']=len(heads)
        def lifecycle_file(stage):
            target=inputs/(stage+'.xlsx');headers=io.fields(stage)
            def rows():
                for h in heads:
                    for sku,p in h['products'].items():
                        row={'补货单号':h['number'],'签发修订':h['revision'],'门店编码':h['store'],'供货仓':h['warehouse'],
                             '商品编码':sku,'商品名称':p['name'],'数量单位':'库存单位',io.QUANTITIES[stage]:p['quantity'],
                             io.DOCNUM[stage]:'SYN-'+stage+'-'+h['number'],io.DOCDATE[stage]:wr.today()}
                        if stage=='payment':row.update(实际金额=p['quantity']*p['price'],币种='CNY')
                        yield [row.get(k) for k in headers]
            book(target,headers,rows());return target
        for stage,label in [('return','08_return'),('operations','09_operations')]:
            file=lifecycle_file(stage)
            documents=measure(label+'_file_read',lambda:batch.read([file],stage,progress)['documents'])
            plan=measure(label+'_prepare',lambda:batch.prepare(work.path,documents,heads,progress))
            receipts=measure(label+'_confirmed_save',lambda:lc.commit(work.path,plan,progress))
            require(len(receipts)==len(heads),'save receipts '+stage)
            del plan,documents,receipts
        measure('09b_operations_natural_adopt_recheck',lambda:ops.adopt_plan(work.path,heads,progress))
        measure('09c_operations_export',lambda:lc.export(work.path,heads,a.output/'operations-export','operations',progress=progress))
        catalogs={}
        for wh in wc.WAREHOUSES:
            c=measure('10_catalog_import_'+wh,lambda:wc.import_catalog(master,wh))
            measure('10_catalog_save_'+wh,lambda:wc.Repository(dbroot).save(c));catalogs[wh]=c
        plan=measure('11_stock_allocation_preview',lambda:jb.preview_new(work.path,heads,catalogs,progress=progress))
        report['business_facts']['allocation']=signature([p['head']['store'],p['head']['warehouse'],r['sku'],canonical(r['demand']),canonical(r['quantity']),canonical(r['amount'])] for p in plan['payload']['plans'] for r in p['rows'])
        generated=measure('12_stock_allocation_confirm',lambda:jb.execute(work.path,plan,progress=progress))
        require(not generated['errors'] and len(generated['generated'])==len(heads),'allocation result: '+str(generated))
        gids=generated['generated'];del plan
        measure('13_jst_export',lambda:jex.export_checked(work.path,gids,a.output/'jst-export',progress=progress))
        measure('14_jst_lock',lambda:jo.lock(work.path,gids,progress=progress))
        entries=jr.prepare_export_ids(work.path,gids,progress)['entries']
        natural=measure('15_payment_natural_prepare',lambda:jp.prepare(work.path,entries,progress));del natural
        file=lifecycle_file('payment')
        documents=measure('16_payment_file_read',lambda:batch.read([file],'payment',progress)['documents'])
        plan=measure('16_payment_prepare',lambda:batch.prepare(work.path,documents,heads,progress))
        receipts=measure('16_payment_confirmed_save',lambda:lc.commit(work.path,plan,progress));del plan,documents,receipts
        config={'configured':True,'enabled':True,'account_binding':{'account_id':'SYN-A','merchant_code':'SYN-M'},
                'store_mapping_version':'synthetic','targets':[{'buyerWarehouseCode':s,'displayName':s} for s in codes]}
        suppliers={'河北仓':'SYN-HB','山东仓':'SYN-SD'}
        plans=measure('17_purchase_freeze',lambda:[ax.freeze_payment(work.path,h,config,suppliers) for h in heads])
        measure('18_purchase_final_validation_local',lambda:[ax.validate_basis(work.path,p,config,suppliers) for p in plans])
        report['business_facts']['purchase']=signature([p['head']['store'],p['head']['warehouse'],r['sku'],canonical(r['quantity']),canonical(r['payment_amount'])] for p in plans for r in p['rows']);del plans
        file=lifecycle_file('receipt')
        documents=measure('19_receipt_file_read',lambda:batch.read([file],'receipt',progress)['documents'])
        plan=measure('19_receipt_prepare',lambda:batch.prepare(work.path,documents,heads,progress))
        receipts=measure('19_receipt_confirmed_save',lambda:lc.commit(work.path,plan,progress));del plan,documents,receipts
        spec={'from':wr.today(),'to':wr.today(),'rounds':8}
        result=measure('20_analytics_query',lambda:analytics.collect(work.path,spec,progress))
        report['business_facts']['analysis']=signature([r['store'],r['warehouse'],r['sku'],r['quantities'],r['valid']] for r in result['rows'])
        measure('21_analytics_summary_export',lambda:ae.export(work.path,result,a.output/'订单经营分析.xlsx',level='analysis',progress=progress))
        measure('22_analytics_detail_export',lambda:ae.export(work.path,result,a.output/'订单经营细则分析.xlsx',level='analysis_detail',progress=progress))
        # A repeated read does not get a synthetic application cache injected.
        repeated=measure('23_analytics_query_again',lambda:analytics.collect(work.path,spec,progress));del repeated,result
        report['final_statuses']=[]
        for h in heads:
            view=lc.snapshot(work.path,h,_comparison=False)
            require(view['next'] is None,'incomplete lifecycle '+h['number'])
            report['final_statuses'].append({'store':h['store'],'warehouse':h['warehouse'],'next':view['next'],'health':view['health']})
        with closing(sqlite3.connect(work.path)) as db:require(db.execute('PRAGMA quick_check').fetchone()[0]=='ok','quick_check')
        report['passed']=True
    except BaseException:
        report.update(error=traceback.format_exc(),failed_phase=phase)
    finally:
        report['elapsed_including_fixture_and_verification_seconds']=time.perf_counter()-all_started
        write(a.output/'result.json',report)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stores',type=int,default=4);p.add_argument('--rows',type=int,default=5000);p.add_argument('--profile',action='store_true');p.add_argument('--signed-path',type=Path)
    raise SystemExit(main(p.parse_args()))
