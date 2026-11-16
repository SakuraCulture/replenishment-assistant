"""Deterministic synthetic signing data for explicit offline acceptance only."""
import hashlib


def fixture(rows=20000,files=17,extra=1000):
    import work_repository as wr,result_semantics as sem
    from version_info import MODEL_VERSION
    # Fields represent model intermediates, source labels, metadata and audits.
    # This is deliberately much wider than a quantity-only SKU fixture.
    fields='O P Q R T U W X Y Z AA AB AC AD AE AF AG AH AI AJ AK AL AO AP AQ AR AT AV AW AX AY AZ BA BB BC BE BF BG BI BJ BK BL BM BO BP BQ BR BS BT BU BW BX BY'.split()
    results={};loaded={};warehouses=['河北仓','山东仓'];catalogs={wh:{'items':{}} for wh in warehouses}
    for n in range(files+bool(extra)):
        store=f'SYN{n+1:03d}';warehouse=warehouses[n%2];data=[];source=[]
        count=rows if n<files else extra
        for i in range(count):
            sku=f'5{i:012d}';quantity=(1+i%5)*12;price=round(.75+(i%113)/20,2)
            row={key:round((i%937+n)/31,6) for key in fields}
            row.update(A='合成门店'+store,B=store,C='SYN-SUP-'+str(i%31),D='合成商品'+str(i)+'/'+str(i%17)+'规格',E=sku,
                F='690'+str(1000000000+i),G='瓶',H=2.86,I=24,J=73,K=i%19,L=i%7,M=2,N=3,BD=12,V=price,S='启用',
                AU=warehouse,AV='A',AS=quantity,BV=quantity*price,AT='正常',AW='动销补货',_source='资料匹配',
                _meta={'blocking':[],'notes':['合成来源-'+hashlib.sha256(f'{n}-{i}'.encode()).hexdigest()[:24]],'catalog_revision':'synthetic-wide'},
                _audit={'applied':False,'source':'合成资料','metrics':{'sales':73,'stock':i%19}},_preview=False,
                _candidate_qty=quantity,_candidate_amount=quantity*price,_must_sell=i%13==0)
            data.append(row)
            source.append([row[k] for k in 'A B C D E F G H I J K L M N BD V S AU'.split()])
            if n<2:
                catalogs[warehouse]['items'][sku]={'商品编码':sku,'商品名称':row['D'],'基本售价':price,'箱规':12,'可用库存':i%1000,
                    '供应商':'合成供应商'+str(i%31),'商品标签':'必采' if i%13==0 else '',
                    '类目名称':'合成类目'+str(i%23),'新店新品首铺数':12,'单位':'瓶','状态':'启用',
                    '原表补充':{key:row[key] for key in fields[:16]}}
        results[store]={'store':store,'store_name':'合成门店'+store,'rows':data,'summary':sem.summarize(data),
            'budget':{'enabled':False},'config':{'parameters':{'cap_days':21}},'sources':{'fixture':'synthetic-wide-v1','warehouse':warehouse},
            'effective_settings':{'备货天数':5,'到货天数':3.5,'默认并发':2}}
        loaded[store]={'headers':'A B C D E F G H I J K L M N BD V S AU'.split(),'rows':source,
            'source':'合成输入-'+store+'.xlsx','hash':hashlib.sha256(store.encode()).hexdigest()}
    # Keep the application's real loaded/intake shape, including the short
    # nested lists that used to become one enormous JSON block.
    selected=[];normalized=[];warehouse_rows={wh:[] for wh in warehouses}
    source_fields='A B C D E F G H I J K L M N BD V S AU'.split()
    for store,result in results.items():
        for i,row in enumerate(result['rows']):
            values=loaded[store]['rows'][i]
            selected.append({'store':store,'sku':row['E'],'values':values,'path':'synthetic-'+store+'.xlsx','sheet':'补货建议','row':i+2})
            original={key:row[key] for key in source_fields}
            meta={'match':'商品编码精确匹配','blocking':[],'export_file':'synthetic-'+store+'.xlsx','export_sheet':'补货建议','export_row':i+2}
            pair=(original,meta);normalized.append(pair)
            for warehouse in warehouses:
                warehouse_rows[warehouse].append(({**original,'AU':warehouse},meta))
    input_data={'stores':{s:r['store_name'] for s,r in results.items()},'normalized':normalized,'warehouse_rows':warehouse_rows,
        'blocked':{},'hashes':{s:v['hash'] for s,v in loaded.items()},'catalogs':{wh:{'count':len(c['items']),'revision':'synthetic-'+wh} for wh,c in catalogs.items()},
        'intake':{'selected':selected,'files':[{'path':v['source'],'error':None} for v in loaded.values()],
                  'headers':source_fields,'conflicts':[],'duplicates':0,'products':None}}
    return {'schema':1,'day':wr.today(),'model_version':MODEL_VERSION,'input_dirty':False,
        'results':results,'states':{s:{'name':r['store_name'],'state':'已完成'} for s,r in results.items()},
        'loaded':input_data,'catalogs':catalogs,'store_presets':{s:{'id':'standard','version':1} for s in results},
        'export_log':[],'synthetic':True},list(results)[:files]

