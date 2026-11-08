// Run against the bundled component, with an isolated journal and simulated adapter.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';
const [componentArg,outputArg]=process.argv.slice(2);
if(!componentArg||!outputArg)throw Error('usage: node verify_packaged_purchase_recovery.mjs <component> <new-output>');
const component=path.resolve(componentArg),output=path.resolve(outputArg);await fs.mkdir(output);
const load=name=>import(pathToFileURL(path.join(component,'src',name+'.mjs')).href);
const {StateStore}=await load('state'),{PurchaseCreates,purchasePublic}=await load('purchase-create-ledger'),{MachinePurchaseCreate}=await load('machine-purchase-create');
const {IMPORT_CODE}=await load('purchase-create-contract'),{sha256}=await load('common');
const checks=[],h='a'.repeat(64);
for(const mode of ['valid','bad-amount','no-receipt']){
  const state=await StateStore.open(path.join(output,mode));
  try{
    const ledger=await PurchaseCreates.open(state),id='SYNTHETIC-'+mode;
    const p={schema:1,operation:'purchase.create',id,identity:{accountId:'QA',merchantCode:'QA'},mappingVersion:h,
      basis:{number:'QA-BH',warehouse:'河北仓',store:'QA-STORE',planFingerprint:h,paymentVersion:1,paymentDigest:h},target:{buyerWarehouseCode:'QA-STORE',displayName:'隔离合成店'},
      supplierCode:'QA-SUPPLIER',marker:'AX_CREATE:'+id,note:'AX_CREATE:'+id+' 本地单:QA-BH',createExternalOrder:false,postCreationState:'待提交',
      file:{path:path.join(output,'synthetic.xlsx'),sha256:h,bytes:300},template:{recordFile:path.join(output,'template.json'),sha256:h,attemptId:'QA-TEMPLATE'},
      rows:[{sku:'QA-SKU',name:'隔离合成商品',quantity:'2',amount:'4'}],totalQuantity:'2',totalAmount:'4'};
    const receipt={intentId:id,taskId:'GEI@'+IMPORT_CODE+'@261108000001@999000',code:IMPORT_CODE,fileSha256:h,requestSha256:h,exactSourceBytesPresent:true,importRequests:1};
    ledger.reserve(p,sha256(JSON.stringify(p)));let uploads=0,reads=0;
    const controller=new MachinePurchaseCreate({ledger,adapter:{prepare:async()=>({orderIds:[]}),upload:async()=>{
      uploads++;if(mode!=='no-receipt')ledger.accepted(receipt);
      throw Object.assign(new Error('synthetic blocked extra request'),{code:'IMPORT_SIDE_EFFECT_BLOCKED',details:{blocked:[{method:'POST',host:'saas-retail.ele.me',path:'/synthetic/blocked'}]}});
    },observe:async(_,r)=>{
      reads++;assert.equal(r.taskId,receipt.taskId);
      const orderId='QA-PO';return {identity:p.identity,task:{taskId:r.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:'1',failCount:'0'},candidates:[orderId],
        main:{orderId,store:p.basis.store,supplier:p.supplierCode,status:'待提交',externalOrderId:'',marker:p.note,quantity:'2',amount:'4',skuCount:'1'},
        rows:[{...p.rows[0],amount:mode==='bad-amount'?'9':'4',orderId,store:p.basis.store,supplier:p.supplierCode,status:'待提交',note:p.note}],
        files:['D16','D17'].map(source=>({source,sha256:h,file:path.join(output,source+'.xlsx')})),observedAt:'2026-11-08T00:00:00Z'};
    }}});
    controller.start(id);await controller.pending;const r=ledger.get(id);assert.equal(uploads,1);
    if(mode==='valid'){
      assert.equal(r.state,'BOUND');assert.equal(reads,1);assert.equal(r.binding.orderId,'QA-PO');assert.equal(purchasePublic(r).uploadWarning.code,'IMPORT_SIDE_EFFECT_BLOCKED');controller.start(id);assert.equal(uploads,1);
      checks.push('打包组件保存唯一回执后恢复完整核验、关联成功、警告留存且重复执行不重传');
    }else{
      assert.equal(r.state,'NEEDS_REVIEW');assert.equal(r.binding,null);assert.equal(reads,mode==='no-receipt'?0:1);
      checks.push(mode==='bad-amount'?'打包组件拒绝金额不符的逐商品证据':'打包组件无回执时不猜测、不查询近似订单、不重传');
    }
  }finally{state.close();}
}
const report={passed:true,synthetic:true,live_platform:false,checks};await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
