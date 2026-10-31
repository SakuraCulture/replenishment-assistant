import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';
const [specFile,output,preview]=process.argv.slice(2);
await fs.mkdir(preview,{recursive:true});
const specs=JSON.parse(await fs.readFile(specFile,'utf8'));
let index=0;
for(const spec of specs){
 const wb=Workbook.create();
 for(const tab of spec.sheets){
  const s=wb.worksheets.add(tab.name);s.showGridLines=false;
  const data=[tab.headers,...tab.rows];
  const all=s.getRangeByIndexes(0,0,data.length,tab.headers.length);all.values=data;
  all.format.font.name='微软雅黑';all.format.font.size=10;
  all.format.columnWidth=18;all.format.rowHeight=24;all.format.verticalAlignment='center';
  const header=s.getRangeByIndexes(0,0,1,tab.headers.length);header.format.fill='#173F4F';header.format.font.color='#FFFFFF';header.format.font.bold=true;header.format.rowHeight=36;header.format.wrapText=true;
  for(let j=0;j<tab.headers.length;j++){
    const h=tab.headers[j], col=s.getRangeByIndexes(1,j,Math.max(1,tab.rows.length),1);
    if(/编码|条码|单号|账号/.test(h)){col.setNumberFormat('@');col.format.columnWidth=h.includes('补货单号')?42:20;}
    if(/名称|商品名/.test(h))col.format.columnWidth=24;
    if(/金额|售价|单价/.test(h))col.setNumberFormat('#,##0.00;[Red](#,##0.00);0.00');
    if(spec.record){col.format.columnWidth=tab.widths?.[j]||22;col.format.wrapText=true;col.format.rowHeight=92;}
  }
  if(spec.record){const st=tab.headers.indexOf('执行结果');if(st>=0){const range=s.getRangeByIndexes(1,st,tab.rows.length,1);range.format.fill='#FFF4CE';range.dataValidation={rule:{type:'list',values:['未执行','通过','失败','阻塞']}};}}
  s.freezePanes.freezeRows(1);s.freezePanes.freezeColumns(spec.record?2:1);
 }
 wb.recalculate();
 for(const tab of spec.sheets){
  const check=await wb.inspect({kind:'region',sheetId:tab.name,range:'A1:H8',maxChars:900,tableMaxRows:3});
  await fs.writeFile(path.join(preview,`${index}-${tab.name}.inspect.json`),JSON.stringify(check));
  const png=await wb.render({sheetName:tab.name,range:`A1:${spec.record?'F':'H'}${Math.min(tab.rows.length+1,spec.record?4:8)}`,scale:1,format:'png'});
  await fs.writeFile(path.join(preview,`${index}-${tab.name}.png`),new Uint8Array(await png.arrayBuffer()));
 }
 const dest=path.join(output,spec.file);await fs.mkdir(path.dirname(dest),{recursive:true});
 const xlsx=await SpreadsheetFile.exportXlsx(wb);await xlsx.save(dest);
 console.log(JSON.stringify({file:spec.file,sheets:spec.sheets.length,rows:spec.sheets.map(s=>s.rows.length)}));index++;
}
