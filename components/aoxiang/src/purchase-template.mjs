import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {SaxesParser} from 'saxes';
import {openArchive} from './zip.mjs';
import {outputPath} from './publisher.mjs';
import {fileHash} from './workbook-validator.mjs';
import {canonical,sha256,now,uid,readJson,writeJson,exists,fail,sameIdentity,safeName} from './common.mjs';

const local=n=>String(n?.name||n).split(':').at(-1);
function xml(text,handlers){
  const p=new SaxesParser({xmlns:false});
  p.on('doctype',()=>fail('PURCHASE_TEMPLATE_XML','模板不允许DTD'));
  for(const [name,fn]of Object.entries(handlers))p.on(name,fn);
  p.write(text).close();
}
function sheetData(text,strings){
  let row=null,cell=null,reading='',validation=null;
  const rows=[],validations=[],seen=new Set();
  xml(text,{
    opentag:n=>{
      const l=local(n);
      if(l==='row')row={row:Number(n.attributes.r),cells:[]};
      if(l==='c'){
        const ref=n.attributes.r;
        if(!/^[A-Z]+[1-9]\d*$/.test(ref)||seen.has(ref))fail('PURCHASE_TEMPLATE_XML','模板单元格引用缺失或重复');
        seen.add(ref);cell={ref,type:n.attributes.t||'n',value:'',formula:''};
      }
      if(cell&&['t','v','f'].includes(l))reading=l;
      if(l==='dataValidation'){validation={...n.attributes,formula1:'',formula2:''};validations.push(validation);}
      if(validation&&['formula1','formula2'].includes(l))reading=l;
    },
    text:s=>{if(cell&&reading)cell[reading==='f'?'formula':'value']+=s;else if(validation&&reading)validation[reading]+=s;},
    closetag:n=>{
      const l=local(n);
      if(['t','v','f','formula1','formula2'].includes(l))reading='';
      if(l==='c'&&cell){
        if(cell.type==='s'){
          if(!/^\d+$/.test(cell.value)||strings[Number(cell.value)]===undefined)fail('PURCHASE_TEMPLATE_XML','模板共享字符串索引无效');
          cell.value=strings[Number(cell.value)];
        }
        if(!row||!Number.isSafeInteger(row.row)||!cell.ref.endsWith(String(row.row))||Number(cell.ref.replace(/^[A-Z]+/,''))!==row.row)fail('PURCHASE_TEMPLATE_XML','模板行号与单元格不一致');
        if(cell.value!==''||cell.formula)row.cells.push({ref:cell.ref,value:cell.value,...(cell.formula?{formula:cell.formula}:{})});
        cell=null;
      }
      if(l==='row'&&row){if(row.cells.length)rows.push(row);row=null;}
      if(l==='dataValidation')validation=null;
    }
  });
  return {rows,validations};
}

// Inspect semantics, never execute formulas or rewrite the native container.
// Formatting/ZIP timestamps may vary without changing the supported mapping.
export async function inspectPurchaseTemplate(file){
  const st=await fs.stat(file);
  if(!st.isFile()||st.size<100||st.size>10485760)fail('PURCHASE_TEMPLATE_SIZE','采购模板大小无效或超过10MiB工程保护值');
  const zip=await openArchive(file,{maxUncompressedBytes:67108864,maxEntries:1024});
  try{
    for(const [name,entry]of zip.entries){
      if(entry.uncompressedSize===0)fail('PURCHASE_TEMPLATE_XML','模板包含空文件成员');
      if(/vbaProject|externalLinks|embeddings|\.bin$/i.test(name))fail('PURCHASE_TEMPLATE_XML','模板包含未支持的宏或外部内容');
    }
    await zip.validateAll();
    for(const member of ['[Content_Types].xml','_rels/.rels'])xml((await zip.buffer(member)).toString('utf8'),{});
    const strings=[],sheets=[],definedNames=[],rels=new Map();let definedName=null;
    if(zip.entries.has('xl/sharedStrings.xml')){
      let value='',active=false;
      xml((await zip.buffer('xl/sharedStrings.xml')).toString('utf8'),{
        opentag:n=>{if(local(n)==='si'){value='';active=true;}},
        text:s=>{if(active)value+=s;},closetag:n=>{if(local(n)==='si'){strings.push(value);active=false;}}
      });
    }
    xml((await zip.buffer('xl/workbook.xml')).toString('utf8'),{
      opentag:n=>{
        if(local(n)==='sheet')sheets.push({name:n.attributes.name,id:n.attributes['r:id'],state:n.attributes.state||'visible'});
        if(local(n)==='definedName'){definedName={...n.attributes,value:''};definedNames.push(definedName);}
      },text:s=>{if(definedName)definedName.value+=s;},closetag:n=>{if(local(n)==='definedName')definedName=null;}
    });
    xml((await zip.buffer('xl/_rels/workbook.xml.rels')).toString('utf8'),{opentag:n=>{
      if(local(n)!=='Relationship')return;
      if(n.attributes.TargetMode==='External'||rels.has(n.attributes.Id))fail('PURCHASE_TEMPLATE_XML','模板工作表关系无效');
      rels.set(n.attributes.Id,n.attributes.Target);
    }});
    if(!sheets.length||new Set(sheets.map(s=>s.name)).size!==sheets.length)fail('PURCHASE_TEMPLATE_XML','模板工作表缺失或重名');
    const schemaSheets=[];
    for(const s of sheets){
      const target=rels.get(s.id);
      if(!target||target.includes('\\')||target.includes(':'))fail('PURCHASE_TEMPLATE_XML','模板工作表关系不可识别');
      const member=path.posix.normalize(target.startsWith('/')?target.slice(1):'xl/'+target);
      if(!member.startsWith('xl/worksheets/'))fail('PURCHASE_TEMPLATE_XML','模板工作表路径不可识别');
      const {rows,validations}=sheetData((await zip.buffer(member)).toString('utf8'),strings);
      if(s.state==='visible'){
        const headers=rows.find(r=>r.row===7)?.cells||[],samples=rows.filter(r=>r.row>7);
        // All non-empty native example rows must be known to the builder. A
        // newly added sample row cannot quietly turn into a business order.
        schemaSheets.push({name:s.name,state:s.state,headerRow:7,inputStartRow:8,headers,instructions:rows.filter(r=>r.row<7),
          sampleRows:samples.map(r=>({row:r.row,isExample:r.cells.some(c=>c.ref==='A'+r.row&&c.value.startsWith('示例')),
            ...(r.cells.some(c=>c.formula)?{hasFormula:true}:{})})),validations});
      }else schemaSheets.push({name:s.name,state:s.state,rows,validations});
    }
    const schema={format:1,definedNames,sheets:schemaSheets};
    return {bytes:st.size,sha256:await fileHash(file),semanticSha256:sha256(canonical(schema)),schema,zipCrc:'PASS'};
  }finally{zip.close();}
}

export async function templateContract(){return readJson(new URL('../assets/purchase-import-template-contract.json',import.meta.url));}
export class PurchaseTemplateService{
  constructor({adapter,outputRoot,identity,signal,contract}={}){Object.assign(this,{adapter,outputRoot,identity,signal,contract});}
  checkSignal(){if(this.signal?.aborted)fail('PAUSED','已停止获取采购模板');}
  async prepareImport(){return this.fetchLatest();}
  async fetchLatest(){
    this.checkSignal();
    if(!this.identity?.accountId||!this.identity?.merchantCode)fail('PURCHASE_TEMPLATE_INPUT','尚未核对获取模板的商家身份');
    const attemptId=uid(),tenant=sha256(canonical({accountId:this.identity.accountId,merchantCode:this.identity.merchantCode})).slice(0,20),
      base='模板/采购单/'+safeName(this.identity.merchantCode)+'_'+tenant,
      latest=await outputPath(this.outputRoot,base+'/latest.json'),recordFile=await outputPath(this.outputRoot,base+'/获取记录/'+attemptId+'.json'),
      staging=await outputPath(this.outputRoot,'.staging/purchase-template/'+attemptId+'/placeholder.xlsx');
    let raw=null,record=null,archiveFile=null;
    try{
      // Always consult the current native button. latest.json is an audit
      // pointer, never an offline or failed-network fallback for an import.
      raw=await this.adapter.downloadPurchaseTemplate({directory:path.dirname(staging),expectedIdentity:this.identity});this.checkSignal();
      if(!sameIdentity(raw.identity,this.identity))fail('WRONG_IDENTITY','模板下载账号或商家与配置不同');
      const actualHash=await fileHash(raw.file);
      if(actualHash!==raw.sha256||!/^[a-f0-9]{64}$/.test(actualHash))fail('PURCHASE_TEMPLATE_HASH','模板下载文件与接收记录不一致');
      archiveFile=await outputPath(this.outputRoot,base+'/原生版本/'+actualHash+'.xlsx');
      if(await exists(archiveFile)){
        if(await fileHash(archiveFile)!==actualHash)fail('TEMPLATE_CACHE_CHANGED','同版本模板被修改；保留现文件与新下载文件，停止导入准备',{file:archiveFile,downloadedFile:raw.file});
      }else{
        await fs.copyFile(raw.file,archiveFile,constants.COPYFILE_EXCL);
        const h=await fs.open(archiveFile,'r+');try{await h.sync();}finally{await h.close();}
      }
      if(await fileHash(archiveFile)!==actualHash)fail('PURCHASE_TEMPLATE_HASH','模板归档后校验失败');
      const inspected=await inspectPurchaseTemplate(archiveFile),contract=this.contract||await templateContract();this.checkSignal();
      const supported=canonical(inspected.schema)===canonical(contract.schema);
      record={format:1,attemptId,status:supported?'SUPPORTED':'NEEDS_REVIEW',identity:raw.identity,checkedAt:raw.checkedAt||now(),
        source:raw.source,transport:raw.transport,uiProductLimit:raw.uiProductLimit??null,importSubmissions:raw.importSubmissions,
        file:archiveFile,sha256:actualHash,bytes:inspected.bytes,semanticSha256:inspected.semanticSha256,contractVersion:contract.version,
        schema:inspected.schema,recordFile,latestFile:latest};
      await writeJson(recordFile,record);await writeJson(latest,record);
      if(!supported)fail('PURCHASE_TEMPLATE_CHANGED','已保存最新原生模板，但字段、填写规则或样例行发生变化，停止导入准备并等待兼容更新',{file:archiveFile,recordFile});
      await fs.unlink(raw.file);
      return record;
    }catch(e){
      if(!this.signal?.aborted&&!record){
        const failed={format:1,attemptId,status:'FETCH_FAILED',identity:this.identity,checkedAt:now(),errorCode:e.code||'PURCHASE_TEMPLATE_FAILED',
          ...(archiveFile?{file:archiveFile}:{}),...(raw?.file?{downloadedFile:raw.file}:{}),recordFile};
        await writeJson(recordFile,failed);await writeJson(latest,failed);
      }
      throw e;
    }
  }
}
