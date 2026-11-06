"""Real AX files + explicitly reconstructed QA upstream. No browser/user DB writes."""
import argparse, copy, hashlib, json, sqlite3, subprocess, sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import aoxiang_receipts as ar, aoxiang_purchase as ap, order_lifecycle as lc, lifecycle_io as io
import normal_orders as no, work_repository as wr, result_semantics as sem
from aoxiang_directory import file_hash
from aoxiang_workflows import Workflows

def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),'utf-8')
def node(script,*args):
    r=subprocess.run([str(ROOT/'components/aoxiang/runtime/node.exe'),'--input-type=module','-e',script,*map(str,args)],cwd=ROOT/'components/aoxiang',capture_output=True,text=True,encoding='utf-8')
    if r.returncode:raise RuntimeError(r.stderr)
    return json.loads(r.stdout)

def main(a):
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);live=a.live.resolve()
    run=json.loads((live/'run.json').read_text('utf-8'));q=json.loads((live/'query.json').read_text('utf-8'))['rows'][0]
    if run['status']!='SUCCEEDED':raise ValueError('Native query/export not complete')
    scope={'orderId':run['orderId'],'store':run['target']['buyerWarehouseCode'],'supplier':q['sellerMerchantCode']}
    sources=node("""import fs from 'node:fs/promises';import path from 'node:path';import {readReceiptSource} from './src/purchase-receipts.mjs';
const root=process.argv[1],scope=JSON.parse(process.argv[2]),r=JSON.parse(await fs.readFile(path.join(root,'run.json'),'utf8'));let out=[];
for(const id of ['D16','D17']){const e=r.exports[id];out.push(await readReceiptSource(path.join(root,'downloads',e.asset.relativePath),id,scope,e.taskSummary.successCount,e.asset.sha256));}console.log(JSON.stringify(out));""",live,json.dumps(scope))
    save(out/'validated-sources.json',sources)
    main,rows=sources[0]['rows'][0],sources[1]['rows']
    if main['status']!='已完成' or any(r['status']!='已完成' for r in rows):raise ValueError('Actual completed state not proven')
    from test_archive_orders import seed
    work,repo,p=seed(out);catalog_sample=copy.deepcopy(p['result']['rows'][1]);signed=[]
    # This is a QA reference constructed from actual PO purchase quantities,
    # not the missing historic application signing/payment records.
    for r in rows:
        d=copy.deepcopy(catalog_sample);qty=float(Decimal(r['quantity']));amount=float(Decimal(r['amount']))
        d.update(E=r['sku'],D=r['name'],AS=qty,BV=amount,V=amount/qty,AU='山东仓',_execution_qty=qty,_execution_amount=amount,
                 _candidate_qty=qty,_candidate_amount=amount)
        signed.append(d)
    p['result'].update(store=scope['store'],store_name=run['target']['displayName'],rows=signed,summary=sem.summarize(signed))
    digest=no.fingerprint(p['result'])
    with closing(work.connect()) as db,db:
        db.execute('UPDATE normal_orders SET store=?',(scope['store'],))
        db.execute('UPDATE normal_revisions SET digest=?,payload=?',(digest,no.encode(p, db)))
    head=lc.current_targets(work.path)[0]
    import openpyxl
    for stage in ('return','payment'):
        book=openpyxl.Workbook();sheet=book.active;sheet.title='合成上游参考';headers=io.fields(stage);sheet.append(headers)
        for r in rows:
            values={'补货单号':head['number'],'签发修订':1,'门店编码':scope['store'],'供货仓':'山东仓','商品编码':r['sku'],'商品名称':r['name'],
                    '数量单位':'库存单位',io.QUANTITIES[stage]:r['quantity'],io.DOCNUM[stage]:'QA-REFERENCE-'+stage,'实际金额':r['amount'],'币种':'CNY'}
            sheet.append([values.get(h) for h in headers])
        path=out/(stage+'-qa-reference.xlsx');book.save(path);book.close()
        info=io.inspect([path])[0];sh=info['sheets'][0]
        scan=io.scan(stage,[{**info,**sh,'mapping':{h:i for i,h in enumerate(sh['headers'])}}])
        lc.commit(work.path,lc.prepare(work.path,scan,{lc.binding_key(r):head for r in scan['rows']}))
    config={'configured':True,'enabled':True,'account_binding':{'account_id':run['identity']['accountId'],'merchant_code':run['identity']['merchantCode']},
            'store_mapping_version':'isolated-source-derived-reference','targets':[run['target']]}
    suppliers={'山东仓':scope['supplier']};plan=ap.freeze_payment(work.path,head,config,suppliers)
    intent={'id':'qa-completed-'+run['orderId'],'identity':run['identity'],'supplierCode':scope['supplier'],
            'basis':{'store':scope['store'],'planFingerprint':plan['fingerprint']},
            'rows':[{k:format(Decimal(r[k]),'f') if k in ('quantity','amount') else r[k] for k in ('sku','name','quantity','amount')} for r in plan['rows']],
            'totalQuantity':format(Decimal(plan['total_quantity']),'f'),'totalAmount':format(Decimal(plan['total_amount']),'f')}
    save(out/'plan.json',plan);save(out/'intent.json',intent)
    observation=node("""import fs from 'node:fs/promises';import path from 'node:path';import {receiptObservation} from './src/purchase-receipts.mjs';
const dir=process.argv[1],p=JSON.parse(await fs.readFile(path.join(dir,'intent.json'),'utf8')),ss=JSON.parse(await fs.readFile(path.join(dir,'validated-sources.json'),'utf8'));
console.log(JSON.stringify(receiptObservation(p,{orderId:ss[0].rows[0].orderId},process.argv[2],ss)));""",out,run['id'])
    save(out/'observation.json',observation);ar.validate_observation(observation,intent)
    package={'id':intent['id'],'package_file':str(out/'intent.json'),'package_sha256':file_hash(out/'intent.json'),
             'plan_file':str(out/'plan.json'),'plan_sha256':file_hash(out/'plan.json')}
    manifest={'file':str(out/'observation.json'),'sha256':file_hash(out/'observation.json')}
    before=lc.snapshot(work.path,head)
    with closing(work.connect()) as db:
        signed_before=db.execute('SELECT * FROM normal_revisions').fetchall()
    first=ar.synchronize(work.path,package,manifest,config,suppliers)
    second=ar.synchronize(work.path,package,manifest,config,suppliers)
    after=lc.snapshot(work.path,head)
    assert first['version']==1 and not second['changed'] and second['version']==1
    assert all(after['documents'][s]==before['documents'][s] for s in ('return','payment'))
    with closing(work.connect()) as db:
        assert db.execute('SELECT * FROM normal_revisions').fetchall()==signed_before
        for f in after['documents']['receipt']['payload']['files']:
            raw=db.execute('SELECT content FROM lifecycle_files WHERE sha=?',(f['sha'],)).fetchone()[0]
            assert hashlib.sha256(raw).hexdigest()==f['sha']
    actual=after['documents']['receipt']['payload']['rows']
    assert [r['sku'] for r in actual]==[r['sku'] for r in rows]
    assert [Decimal(r['base_quantity']) for r in actual]==[Decimal(r['receivedQuantity']) for r in rows]
    assert sum(Decimal(r['amount']) for r in actual)==Decimal(main['receivedAmount'])
    assert all(r['document_date'] is None for r in after['documents']['receipt']['payload']['raw_rows'])
    queue=Workflows(out/'scheduler',clock=lambda:0)
    queue.configure_schedules([{'id':intent['id'],'basis':intent['basis'],'binding':{'orderId':run['orderId']}}],5,True)
    queue.observe_schedule(intent['id'],{'state':'SYNCED','terminal':observation['terminal'],'summary':{'status':main['status']}})
    restarted=Workflows(out/'scheduler',clock=lambda:100000)
    assert restarted.enqueue_due() is None and restarted.schedule(intent['id'])['terminal'] and not restarted.schedule(intent['id'])['enabled']
    report={'passed':True,'real_platform':{'order':run['orderId'],'store':scope['store'],'status':main['status'],'purchase_skus':len(rows),
            'purchase_quantity':main['quantity'],'purchase_amount':main['amount'],'received_quantity':main['receivedQuantity'],'received_amount':main['receivedAmount'],
            'short_skus':sum(Decimal(r['receivedQuantity'])<Decimal(r['quantity']) for r in rows),
            'export_tasks':{k:v['taskId'] for k,v in run['exports'].items()},'source_hashes':{s['source']:s['sha256'] for s in sources}},
            'qa_replay':{'upstream':'Explicitly source-derived synthetic signing/return/payment reference; not actual historic app records',
            'formal_receipt_adapter':True,'repeat_does_not_add_version':True,'raw_files_retained':True,'upstream_unchanged':True,
            'exact_quantity_amount_and_order':True,'completed_stops_polling_after_restart':True,'platform_completed_at':None},
            'limits':['One supplied completed order verified, not all 28.','No platform submit/receipt/change; only two exact-scope native export tasks.',
            'Does not prove an actual historic app signing/payment binding or continuous create-to-receive flow.','No invented receipt completion time.']}
    save(out/'report.json',report);print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--live',type=Path,required=True);p.add_argument('--out',type=Path,required=True);main(p.parse_args())
