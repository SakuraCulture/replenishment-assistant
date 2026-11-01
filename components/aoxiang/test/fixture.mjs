import {createArchive} from '../src/zip.mjs';
import {fieldContract} from '../src/config.mjs';
const column=i=>{let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;};
const escape=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
export async function nativeFixture(file,{code='WH',sku='900719925474099312345',numericSku=false,variant='current'}={}){
  const c=await fieldContract(variant),values=Array(c.columns.length).fill('');values[c.keyColumnPositions1Based.buyerWarehouseCode-1]=code;values[c.keyColumnPositions1Based.skuCode-1]=sku;
  const rows=[c.columns.map(c=>c.rawHeader),values].map((r,i)=>`<row r="${i+1}">${r.map((v,k)=>numericSku&&i===1&&k===c.keyColumnPositions1Based.skuCode-1?`<c r="${column(k)}${i+1}"><v>${escape(v)}</v></c>`:`<c r="${column(k)}${i+1}" t="inlineStr"><is><t>${escape(v)}</t></is></c>`).join('')}</row>`).join('');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:`<workbook xmlns:r="urn:r"><sheets><sheet name="${c.nativeSheetName}" r:id="r1"/></sheets></workbook>`},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><dimension ref="A1:A1"/><sheetData>'+rows+'</sheetData></worksheet>'}]);return file;
}
