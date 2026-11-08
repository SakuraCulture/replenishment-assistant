import fs from 'node:fs/promises';
import path from 'node:path';
import {constants} from 'node:fs';
import {openArchive} from './zip.mjs';
import {canonical,readJson,sha256,fail,sameIdentity} from './common.mjs';
import {fileHash,validateWorkbook} from './workbook-validator.mjs';
import {inspectPurchaseTemplate,templateContract} from './purchase-template.mjs';
import {validatePurchaseIntent,decimal,PRICE_POLICY} from './purchase-create-contract.mjs';
import {mappingVersion} from './machine-commands.mjs';

export async function readPurchasePackage(file,hash){
  if(!path.isAbsolute(file)||file.startsWith('\\\\'))fail('PURCHASE_PACKAGE_PATH','采购任务必须使用本机绝对文件路径');
  const stat=await fs.stat(file);if(!stat.isFile()||stat.size>4194304)fail('PURCHASE_PACKAGE_SIZE','采购任务超过4MiB本机保护值');
  const bytes=await fs.readFile(file);if(sha256(bytes)!==hash)fail('PURCHASE_PACKAGE_CHANGED','采购任务文件已变化');
  return validatePurchaseIntent(JSON.parse(bytes.toString('utf8')));
}
export function checkPurchaseConfiguration(p,config){
  if(!config.enabled||!sameIdentity(p.identity,config.tenantBinding)||p.mappingVersion!==mappingVersion(config))fail('PURCHASE_CONFIGURATION_CHANGED','翱象账号或门店配置变化，请重新核对采购预览');
  const targets=config.targets.filter(t=>t.buyerWarehouseCode===p.basis.store);
  if(targets.length!==1||canonical(targets[0])!==canonical(p.target))fail('PURCHASE_CONFIGURATION_CHANGED','采购目标门店与已核验目录不符');
}
export async function checkPurchaseInput(p,{latest=true,requireRounded=true}={}){
  validatePurchaseIntent(p);
  if(requireRounded&&p.pricePolicy!==PRICE_POLICY)fail('PURCHASE_PRICE_POLICY','旧预览未按两位采购单价生成；未上传任务请取消原预览后重新生成，已上传任务只继续原任务核对');
  const receipt=await readJson(p.template.recordFile),contract=await templateContract();
  if(receipt.status!=='SUPPORTED'||receipt.attemptId!==p.template.attemptId||receipt.sha256!==p.template.sha256||!sameIdentity(receipt.identity,p.identity)||receipt.contractVersion!==contract.version||canonical(receipt.schema)!==canonical(contract.schema)||receipt.importSubmissions!==0)fail('PURCHASE_TEMPLATE_PROOF','本次模板获取记录未通过核验');
  if(latest){const last=await readJson(receipt.latestFile);if(last.attemptId!==receipt.attemptId||last.status!=='SUPPORTED'||last.sha256!==receipt.sha256)fail('PURCHASE_TEMPLATE_STALE','采购模板获取记录已变化，请重新获取并预览');}
  if(receipt.uiProductLimit!==null&&receipt.uiProductLimit!==undefined&&(!Number.isSafeInteger(receipt.uiProductLimit)||p.rows.length>receipt.uiProductLimit||receipt.uiProductLimit<1))fail('PURCHASE_LIMIT','采购商品超过本次平台上限，本期不拆单');
  if(await fileHash(receipt.file)!==p.template.sha256||await fileHash(p.file.path)!==p.file.sha256||(await fs.stat(p.file.path)).size!==p.file.bytes)fail('PURCHASE_FILE_CHANGED','采购原模板或待上传文件发生变化');
  const input=await inspectPurchaseTemplate(p.file.path),visible=contract.schema.sheets.find(s=>s.state==='visible'),actual=input.schema.sheets.find(s=>s.name===visible.name);
  if(!actual||actual.sampleRows.length!==p.rows.length||actual.sampleRows.some((r,i)=>r.row!==visible.inputStartRow+i||r.isExample||r.hasFormula))fail('PURCHASE_INPUT_ROWS','采购文件行号、样例或公式不符合冻结内容');
  const expectedSchema=structuredClone(contract.schema);expectedSchema.sheets.find(s=>s.name===visible.name).sampleRows=actual.sampleRows;
  if(canonical(input.schema)!==canonical(expectedSchema))fail('PURCHASE_INPUT_STRUCTURE','采购文件说明、表头、隐藏规则或数据区之外内容变化');
  const native=await openArchive(receipt.file),generated=await openArchive(p.file.path);
  try{
    if(canonical([...native.entries.keys()])!==canonical([...generated.entries.keys()]))fail('PURCHASE_INPUT_STRUCTURE','采购文件ZIP结构变化');
    const changed=[];for(const member of native.entries.keys())if(!(await native.buffer(member)).equals(await generated.buffer(member)))changed.push(member);
    if(changed.length!==1||!changed[0].startsWith('xl/worksheets/'))fail('PURCHASE_INPUT_STRUCTURE','采购文件修改了数据表之外的原生内容');
  }finally{native.close();generated.close();}
  const parsed=await validateWorkbook(p.file.path,{warehouseCode:p.basis.store,expectedRows:p.rows.length,maxCompressedBytes:10485760,
    contract:{nativeSheetName:visible.name,headerRow:visible.headerRow,columns:visible.headers.map(c=>({rawHeader:c.value})),keyColumnPositions1Based:{buyerWarehouseCode:1,skuCode:3},captureColumns1Based:Object.fromEntries([...'ABCDEFGHIJKL'].map((c,i)=>[c,i+1]))}});
  for(let i=0;i<p.rows.length;i++){
    const r=p.rows[i],a=parsed.capturedRows[i],rounded=p.pricePolicy===PRICE_POLICY,expected={A:p.basis.store,B:p.supplierCode,C:r.sku,D:decimal(r.quantity),E:rounded?decimal(r.unitPrice):'',F:rounded?'':decimal(r.amount),G:'',H:r.name,I:'',J:p.note,K:'否',L:''};
    a.D=decimal(a.D);if(rounded)a.E=decimal(a.E);else a.F=decimal(a.F);
    if(canonical(a)!==canonical(expected))fail('PURCHASE_INPUT_MISMATCH','采购文件逐行内容、顺序或外部订单标记与冻结付款不一致',{index:i});
  }
  if(await fileHash(p.file.path)!==p.file.sha256||await fileHash(receipt.file)!==p.template.sha256)fail('PURCHASE_FILE_CHANGED','核验期间采购文件发生变化');
  return parsed;
}
export async function freezePurchaseInput(root,p){
  const directory=path.join(root,'purchase-create-files',p.id);await fs.mkdir(directory,{recursive:true});
  const file=path.join(directory,p.file.sha256+'.xlsx');
  try{await fs.copyFile(p.file.path,file,constants.COPYFILE_EXCL);const h=await fs.open(file,'r+');try{await h.sync();}finally{await h.close();}}catch(e){if(e.code!=='EEXIST')throw e;}
  if(await fileHash(file)!==p.file.sha256)fail('PURCHASE_FILE_CHANGED','采购任务原件留存不一致，未覆盖');
  return {...p,file:{...p.file,sourcePath:p.file.path,path:file}};
}
