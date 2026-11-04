"""Create explicitly synthetic, offline EXE input/payment fixtures in a new QA folder."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]


def prepare(out, component):
    import openpyxl
    from test_archive_orders import seed
    import lifecycle_io as io
    import order_lifecycle as lc
    import purchases
    from aoxiang_acquisition import prepare as prepare_job, manifest
    from aoxiang_jobs import Jobs
    from aoxiang_directory import file_hash, atomic_json

    out, component = Path(out).resolve(), Path(component).resolve()
    work, _, _ = seed(out)
    # Archive fixtures intentionally have incomplete old masters. Purchase preparation
    # rechecks CURRENT admission evidence, so supply an explicit current empty-tag
    # column through the normal importer; do not rewrite archived snapshots or bypass
    # qualification checks. These files contain only synthetic product codes.
    import warehouse_catalog as wc
    for warehouse in ('河北仓','山东仓'):
        current=out/('SYNTHETIC-current-'+warehouse+'.xlsx');wb=openpyxl.Workbook()
        wb.active.append(wc.REQUIRED+['商品标签','商品名称'])
        wb.active.append(['0001','饮品',2,1,'启用',100,'','合成商品'])
        wb.active.append(['0002','饮品',3,6,'启用',100,'','合成箱规'])
        wb.save(current);wb.close()
        wc.Repository(work.path.parent).save(wc.import_catalog(current,warehouse))
    head = purchases.targets(work.path)[0]
    for stage, qty, amount in [('return',5,12), ('payment',7,.99)]:
        path = out/(stage+'.xlsx')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '业务明细'
        headers = io.fields(stage)
        ws.append(headers)
        values = {'补货单号':head['number'], '签发修订':1, '门店编码':'T', '供货仓':'河北仓',
                  '商品编码':'0001', '商品名称':'合成商品', '数量单位':'库存单位', io.QUANTITIES[stage]:qty,
                  io.DOCNUM[stage]:'SYNTHETIC', '实际金额':amount, '币种':'CNY'}
        ws.append([values.get(h) for h in headers])
        wb.save(path)
        wb.close()
        info = io.inspect([path])[0]
        sheet = info['sheets'][0]
        scan = io.scan(stage,[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
        heads = lc.available(work.path, scan)
        bindings = {lc.binding_key(r):lc.matching(r,heads)[0] for r in scan['rows']}
        lc.commit(work.path, lc.prepare(work.path, scan, bindings))
    ax = out/'settings/aoxiang'
    node = component/'runtime/node.exe'
    def run(script, *args):
        return subprocess.check_output([str(node),'--input-type=module','-e',script,*map(str,args)],
                                       cwd=component, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    run("import {StateStore} from './src/state.mjs';import {freshConfig,saveConfig} from './src/config.mjs';"
        "const root=process.argv[1],s=await StateStore.open(root);await saveConfig(root,freshConfig({accountId:'A',merchantCode:'M',displayName:'EXE离线合成商家'},[{buyerWarehouseCode:'T',displayName:'测试店',operatingStores:[],mappingSource:'offline-fixture',mappingVerifiedAt:'2026-11-02T00:00:00Z'}]));s.close();", ax/'component-state')
    template = out/'synthetic-native-template.xlsx'
    shutil.copyfile(ROOT/'components/aoxiang/test/fixtures/purchase-import-template.xlsx', template)
    receipt = json.loads(run("import {inspectPurchaseTemplate,templateContract} from './src/purchase-template.mjs';console.log(JSON.stringify({...await inspectPurchaseTemplate(process.argv[1]),contractVersion:(await templateContract()).version}));",template))
    receipt.update(status='SUPPORTED',file=str(template),identity={'accountId':'A','merchantCode':'M'},
                   recordFile=str(out/'template-record.json'),latestFile=str(out/'template-latest.json'),
                   attemptId='offline-exe-fixture',importSubmissions=0,uiProductLimit=None,
                   source='SYNTHETIC OFFLINE EXE FIXTURE; NOT A PLATFORM RECEIPT')
    atomic_json(receipt['recordFile'],receipt)
    atomic_json(receipt['latestFile'],receipt)
    configuration = {'configured':True,'enabled':True,'display_name':'离线输入测试',
                     'account_binding':{'account_id':'A','merchant_code':'M'},'store_mapping_version':'offline',
                     'targets':[{'buyerWarehouseCode':code,'displayName':code} for code in ('W2','W1')]}
    jobs = Jobs(ax/'jobs.sqlite3')
    job = prepare_job(jobs,configuration,['W2','W1'],'reuse',day='2026-11-02')
    items = []
    for index,code in enumerate(('W2','W1')):
        path = out/(code+'.xlsx')
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '商品补货建议'
        ws.append(['仓库/门店编码','仓库/门店名称','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）'])
        ws.append([code,'合成门店'+code,'0001','合成单件',1,7,30,index,0,0])
        ws.append([code,'合成门店'+code,'0002','合成箱规',2,14,60,0,0,0])
        wb.save(path)
        wb.close()
        digest = file_hash(path)
        target = ax/'command-files'/job['id']/f'{index}-{digest}.xlsx'
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,target)
        result = {'warehouse_code':code,'run_id':'synthetic-'+code,'file':str(target),'sha256':digest,
                  'bytes':target.stat().st_size,'rows':2,'account_binding':configuration['account_binding'],'reused':False}
        items.append({'index':index,'warehouse_code':code,'run_id':'synthetic-'+code,'state':'SUCCEEDED','result':result,'error':None})
    jobs.observe(job['id'],{'id':job['id'],'fingerprint':job['fingerprint'],'request':job['command'],'state':'SUCCEEDED','sequence':2,'items':items})
    value = manifest(jobs.get(job['id']), ax)
    master = out/'合成商品资料.xlsx'
    wb = openpyxl.Workbook()
    wb.active.append(['商品编码','商品名称','分类','基本售价','出库箱规','商品状态','仓归属'])
    wb.active.append(['0001','合成单件','饮料',2,1,'启用','河北仓'])
    wb.active.append(['0002','合成箱规','饮料',3,6,'启用','河北仓'])
    wb.save(master)
    wb.close()
    fixture = dict(synthetic=True,live_platform=False,head=head,receipt=receipt,manifest=value,master=str(master))
    atomic_json(out/'business-fixture.json',fixture)
    return fixture
