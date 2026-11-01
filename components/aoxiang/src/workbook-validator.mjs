import fs from 'node:fs/promises';
import path from 'node:path';
import {createReadStream} from 'node:fs';
import crypto from 'node:crypto';
import {StringDecoder} from 'node:string_decoder';
import {SaxesParser} from 'saxes';
import {openArchive} from './zip.mjs';
import {fail,countValue} from './common.mjs';

export async function fileHash(file){const h=crypto.createHash('sha256');for await(const b of createReadStream(file))h.update(b);return h.digest('hex');}
const local = s => s.split(':').at(-1);
async function parseXml(zip,name,handlers){
  const parser=new SaxesParser({xmlns:false});
  parser.on('doctype',()=>fail('XML_DTD','不接受带DTD的工作簿XML'));
  for(const [event,fn] of Object.entries(handlers))parser.on(event,fn);
  const decoder=new StringDecoder('utf8');
  await zip.read(name,b=>parser.write(decoder.write(b)));parser.write(decoder.end()).close();
}
function columnIndex(reference){const letters=/^[A-Z]+/.exec(reference)?.[0];if(!letters)fail('CELL_REFERENCE','工作簿缺少合法单元格坐标');let n=0;for(const x of letters)n=n*26+x.charCodeAt(0)-64;return n-1;}

export async function validateWorkbook(file,{warehouseCode,expectedRows,contract,maxCompressedBytes=1073741824}={}){
  const stat=await fs.stat(file);if(stat.size<100||stat.size>maxCompressedBytes)fail('FILE_SIZE','Excel文件大小不符合保护条件');
  const fh=await fs.open(file,'r');const signature=Buffer.alloc(4);await fh.read(signature,0,4,0);await fh.close();
  if(signature.toString('hex')!=='504b0304')fail('NOT_XLSX','文件不是有效XLSX，可能是登录页或错误响应');
  const zip=await openArchive(file);
  try{
    await zip.validateAll();
    const sheets=[];await parseXml(zip,'xl/workbook.xml',{opentag:n=>{if(local(n.name)==='sheet')sheets.push({name:n.attributes.name,rid:n.attributes['r:id']});}});
    const matches=sheets.filter(s=>s.name===contract.nativeSheetName);if(matches.length!==1)fail('SHEET_MISMATCH','未找到唯一的约定工作表',{expected:contract.nativeSheetName,actual:sheets.map(s=>s.name)});
    let target;
    await parseXml(zip,'xl/_rels/workbook.xml.rels',{opentag:n=>{if(local(n.name)==='Relationship'&&n.attributes.Id===matches[0].rid){if(n.attributes.TargetMode==='External')fail('EXTERNAL_SHEET','工作表不能引用外部文件');target=n.attributes.Target;}}});
    if(!target)fail('SHEET_RELATION','未找到工作表关联');
    const sheetPath=target.startsWith('/')?target.slice(1):path.posix.normalize('xl/'+target);
    if(!sheetPath.startsWith('xl/'))fail('SHEET_RELATION','工作表路径不属于工作簿');
    const strings=[];let inSi=false,inT=false,text='',phonetic=0;
    if(zip.entries.has('xl/sharedStrings.xml'))await parseXml(zip,'xl/sharedStrings.xml',{
      opentag:n=>{const l=local(n.name);if(l==='si'){inSi=true;text='';}if(l==='rPh')phonetic++;if(l==='t'&&inSi&&!phonetic)inT=true;},
      text:v=>{if(inT)text+=v;},
      closetag:n=>{const l=local(n.name||n);if(l==='t')inT=false;if(l==='rPh')phonetic--;if(l==='si'){strings.push(text);inSi=false;if(strings.length>1000000)fail('SST_LIMIT','工作簿共享文本超过保护值');}}
    });
    let rowNumber=0,row=null,cell=null,reading=null,header=null,rows=0,nullKeys=0,duplicates=0;
    const entities=new Map(),keys=new Set(),rowWidths=new Set(),capturedRows=[];
    const headerExpected=contract.columns.map(c=>c.rawHeader);
    await parseXml(zip,sheetPath,{
      opentag:n=>{
        const l=local(n.name);
        if(l==='row'){rowNumber=Number(n.attributes.r);row=[];}
        else if(l==='c'&&row){cell={col:columnIndex(n.attributes.r),type:n.attributes.t||'n',value:'',formula:false};}
        else if(l==='f'&&cell)cell.formula=true;
        else if((l==='v'||l==='t')&&cell)reading=l;
      },
      text:v=>{if(reading&&cell)cell.value+=v;},
      closetag:n=>{
        const l=local(n.name||n);
        if(l==='v'||l==='t')reading=null;
        else if(l==='c'&&cell){
          let value=cell.value;
          if(cell.type==='s'){const i=Number(value);if(!Number.isSafeInteger(i)||strings[i]===undefined)fail('INVALID_SHARED_STRING','共享字符串索引无效');value=strings[i];}
          const keyPositions=Object.values(contract.keyColumnPositions1Based).map(p=>p-1);
          if(rowNumber>contract.headerRow&&keyPositions.includes(cell.col)&&(cell.formula||(cell.type==='n'&&(/e[+-]?\d+/i.test(value)||/^\d{16,}$/.test(value)))))fail('UNSAFE_NATIVE_ID','原生编码使用公式、科学计数或超15位数值，无法保证业务ID精度；原文件保留待核对',{row:rowNumber,column:cell.col+1});
          row[cell.col]=value;cell=null;
        }else if(l==='row'&&row){
          if(rowNumber===contract.headerRow){header=Array.from({length:Math.max(row.length,headerExpected.length)},(_,i)=>row[i]??'');if(JSON.stringify(header)!==JSON.stringify(headerExpected))fail('SCHEMA_DRIFT','原生表头或列位发生变化，文件已保留待复核',{actualHeaders:header});}
          else if(rowNumber>contract.headerRow&&row.some(v=>v!==''&&v!==undefined)){
            if(!header)fail('HEADER_MISSING','工作簿缺少约定表头');rows++;rowWidths.add(row.length);
            const code=row[contract.keyColumnPositions1Based.buyerWarehouseCode-1]??'';
            const sku=row[contract.keyColumnPositions1Based.skuCode-1]??'';
            entities.set(code,(entities.get(code)||0)+1);if(!code||!sku)nullKeys++;
            const key=JSON.stringify([code,sku,...(contract.keyColumnPositions1Based.purchaseOrder?[row[contract.keyColumnPositions1Based.purchaseOrder-1]]:[])]);if(keys.has(key))duplicates++;keys.add(key);
            if(contract.captureColumns1Based){if(capturedRows.length>=500000)fail('CAPTURE_LIMIT','原生明细超过本版本50万行校验保护值');capturedRows.push(Object.fromEntries(Object.entries(contract.captureColumns1Based).map(([k,v])=>[k,row[v-1]??''])));}
          }row=null;
        }
      }
    });
    if(!header)fail('HEADER_MISSING','工作簿未包含约定表头');
    if(nullKeys)fail('NULL_KEYS','原生文件存在空主体编码或业务键',{nullKeys});
    if(warehouseCode&&(entities.size!==1||!entities.has(warehouseCode)))fail('ENTITY_MISMATCH','文件中的实际仓店与指定仓店不一致',{entities:Object.fromEntries(entities)});
    if(expectedRows!==undefined&&rows!==countValue(expectedRows))fail('ROW_COUNT_MISMATCH','文件实际行数与已验证任务行数不同',{actualRows:rows,expectedRows});
    if(!rows)fail('EMPTY_UNVERIFIED','零行导出契约尚未验证，不能自动验收');
    return {status:'VALIDATED',bytes:stat.size,sha256:await fileHash(file),sheet:contract.nativeSheetName,rows,columns:header.length,entityCounts:Object.fromEntries(entities),uniqueWarehouseSkuKeys:keys.size,duplicateWarehouseSkuRows:duplicates,nullKeys,rowWidths:[...rowWidths],nativeBytesPreserved:true,...(contract.captureColumns1Based?{capturedRows}: {})};
  }finally{zip.close();}
}
