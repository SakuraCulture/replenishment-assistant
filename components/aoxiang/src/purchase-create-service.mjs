import path from 'node:path';
import {readJson,fail,now,businessDate,countValue,sameIdentity} from './common.mjs';
import {loadConfig,validateConfig} from './config.mjs';
import {assertHandoff} from './preflight.mjs';
import {checkPurchaseConfiguration,checkPurchaseInput} from './purchase-create-artifacts.mjs';
import {PurchaseCoordinator} from './purchase-coordinator.mjs';
import {requireHostOrder,requireUnretired,captureOwnedFiles} from './order-retirement.mjs';
import {purchaseContract,purchaseScopeHash} from './purchase-contract.mjs';
import {validateWorkbook} from './workbook-validator.mjs';
import {IMPORT_CODE} from './purchase-create-contract.mjs';

export async function nativeCreationEvidence(p,task,candidate,coordinator,run){
  if(run.status!=='SUCCEEDED'||run.orderId!==candidate.supplyOrderId||run.query.orderIds.join(',')!==candidate.supplyOrderId)fail('PURCHASE_PROOF_SCOPE','采购核验文件不属于唯一候选采购单');
  const values={},files=[];
  for(const source of ['D16','D17']){
    const file=await coordinator.sourceFile(run,source),contract=purchaseContract(source);
    contract.captureColumns1Based=source==='D16'?{orderId:1,status:2,store:4,supplier:6,skuCount:7,quantity:8,amount:9,externalOrderId:10}:
      {orderId:1,status:2,store:8,supplier:10,sku:13,quantity:19,amount:27,note:37};
    values[source]=await validateWorkbook(file,{contract,warehouseCode:p.basis.store,expectedRows:run.exports[source].asset.rows,maxCompressedBytes:67108864});
    if(values[source].sha256!==run.exports[source].asset.sha256)fail('PURCHASE_PROOF_CHANGED','采购核验文件发生变化');
    files.push({source,file,sha256:values[source].sha256});
  }
  if(values.D16.capturedRows.length!==1)fail('PURCHASE_PROOF_SCOPE','采购主单不是恰好一张');
  return {identity:p.identity,task,candidates:[candidate.supplyOrderId],main:{...values.D16.capturedRows[0],marker:candidate.buyerMessage},rows:values.D17.capturedRows,files,observedAt:now()};
}

export class PurchaseCreateService{
  constructor({root,state,ledger,browsers,progress=()=>{},coordinatorFactory=options=>new PurchaseCoordinator(options)}){Object.assign(this,{root,state,ledger,browsers,progress,coordinatorFactory});}
  async context(p,signal){
    requireUnretired(this.state.db,p.id,p.basis.oid,p.basis.warehouse);requireHostOrder(p.basis.oid,p.basis.warehouse);
    const config=await loadConfig(this.root);checkPurchaseConfiguration(p,config);await assertHandoff(this.root,config.tenantBinding);
    const binding=await validateConfig('machine.binding',await readJson(path.join(this.root,'config/machine.binding.json')));
    const browser=await this.browsers.acquire({state:this.state,config,binding,signal,onProgress:e=>this.progress(e.stage)});
    browser.adapter.onImportReceipt=r=>this.ledger.accepted(r);
    const download=browser.adapter.retirementDownloadBase||browser.adapter.downloadPurchase;
    browser.adapter.retirementDownloadBase=download;
    browser.adapter.downloadPurchase=async(...args)=>{const result=await download(...args);captureOwnedFiles(this.state,p.basis.oid,result,p.basis.warehouse);return result;};
    return {config,binding,adapter:browser.adapter,signal,hostOrderId:p.basis.oid,hostPurchaseId:p.id,hostWarehouse:p.basis.warehouse};
  }
  async preflight(p,{forBinding=false}={}){requireUnretired(this.state.db,p.id,p.basis.oid,p.basis.warehouse);requireHostOrder(p.basis.oid,p.basis.warehouse);checkPurchaseConfiguration(p,await loadConfig(this.root));return checkPurchaseInput(p,{requireRounded:!forBinding});}
  async prepare(p,{signal}){const {adapter}=await this.context(p,signal);return adapter.preparePurchaseCreate(p);}
  async upload(p,baseline,{signal}){const {adapter}=await this.context(p,signal);return adapter.uploadPurchaseOnce({intent:p,baseline});}
  async observe(p,receipt,{signal}){
    const context=await this.context(p,signal),{adapter}=context,r=this.ledger.get(p.id);
    const task=await adapter.purchaseImportTask({taskId:receipt.taskId,identity:p.identity});
    if(task.status!=='FINISHED')fail(['FAILED','FAILURE','CANCELLED'].includes(task.status)?'IMPORT_TASK_FAILED':'TASK_PENDING','原导入任务尚未成功完成，保留原任务不重传');
    if(task.code!==IMPORT_CODE||countValue(task.failCount)!==0||countValue(task.successCount)!==p.rows.length)fail('IMPORT_TASK_PARTIAL','原导入任务非全部成功，保留失败信息及已生成单，不重传');
    const current=await adapter.queryCreateOrders({identity:p.identity,buyerWarehouseCode:p.basis.store,name:p.target.displayName,from:r.baseline.from,to:businessDate(),dateBasis:'created'});
    const candidates=current.rows.filter(v=>v.buyerMessage===p.note&&!r.baseline.orderIds.includes(v.supplyOrderId));
    if(candidates.length!==1)fail('PURCHASE_NOT_UNIQUE','尚未唯一找到本次新采购单，缺单或意外多单只待核，不重传',{orderIds:candidates.map(v=>v.supplyOrderId)});
    const c=candidates[0];
    if(c.buyerWarehouseCode!==p.basis.store||c.sellerMerchantCode!==p.supplierCode||c.outOrderId||c.executionMainOrderId)fail('PURCHASE_CREATED_MISMATCH','找到的原采购单店仓、供应商或外部订单与本次任务不一致，请核对原单');
    if(typeof c.status!=='string'||!c.status)fail('PURCHASE_STATUS_UNSUPPORTED','原采购单查询状态缺失，暂不能核实是否已作废',{status:c.status});
    if(typeof c.gmtCreate!=='string'||!/^\d{13}$/.test(c.gmtCreate))fail('PURCHASE_CREATED_DATE','采购制单时间口径未支持，不按本地时间猜测');
    const day=businessDate(new Date(Number(c.gmtCreate)));
    const coordinator=this.coordinatorFactory({state:this.state,...context,onProgress:e=>this.progress(e.stage)});
    // One exact PO. The existing coordinator persists D16/D17 intents, task IDs and files;
    // resuming a failed verification follows those tasks, never submits the import again.
    const scope={buyerWarehouseCode:p.basis.store,from:day,to:day,dateBasis:'created',orderId:c.supplyOrderId};
    const prior=this.state.rows('purchase_runs').filter(v=>sameIdentity(v.identity,p.identity)&&v.scopeHash===purchaseScopeHash(scope)).at(-1);
    // A completed old export may predate a platform void. Refresh only completed
    // evidence; unfinished exports must resume their saved tasks, never resubmit.
    const rerun=c.status!=='0'&&prior?.status==='SUCCEEDED';
    const run=await coordinator.run({target:p.target,...scope,rerun});
    return {...await nativeCreationEvidence(p,task,c,coordinator,run),listingStatus:c.status};
  }
}
