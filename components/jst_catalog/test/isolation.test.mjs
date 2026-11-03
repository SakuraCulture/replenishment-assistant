import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {AccountSessions} from '../src/accounts.mjs';
import {receiveKnownExport} from '../src/product-export.mjs';

test('normal browser root and sibling profile cannot become dedicated JST',async()=>{
 const temp=await fs.mkdtemp(path.join(os.tmpdir(),'catalog-boundary-'));const normal=path.join(temp,'normal');await fs.mkdir(path.join(normal,'Profile 1'),{recursive:true});
 const s=new AccountSessions({});s.components={env:{defaultEdgeRoot:()=>normal}};
 try{await assert.rejects(s.binding({profilePath:path.join(normal,'Profile 1')}),{code:'BROWSER_ISOLATION'});}finally{await fs.rm(temp,{recursive:true});}
});
test('two warehouses cannot use sibling profiles in one instance root',async()=>{
 const temp=await fs.mkdtemp(path.join(os.tmpdir(),'catalog-boundary-'));await fs.mkdir(path.join(temp,'Default'));
 const s=new AccountSessions({});s.components={env:{defaultEdgeRoot:()=>temp+'-unrelated'}};
 try{await assert.rejects(s.binding({profilePath:path.join(temp,'Default')},[{binding:{userDataRoot:temp}}]),{code:'ACCOUNT_ISOLATION'});}finally{await fs.rm(temp,{recursive:true});}
});
function mockPage(attributes){
 let downloads=0;const row={count:async()=>1,innerText:async()=>'123 商品资料 2 下载完成',locator:()=>({evaluateAll:async()=>attributes,filter:()=>({count:async()=>0})})};
 const history={url:()=>'/app/common/export/export_log.aspx',getByText:()=>({}),locator:selector=>selector==='#_jt_body_list'?{count:async()=>1,waitFor:async()=>{}}:{filter:()=>row}};
 return {page:{locator:()=>({count:async()=>1,isVisible:async()=>true,evaluate:async()=>false,dispatchEvent:async()=>{}}),frames:()=>[history],context:()=>({request:{get:async()=>{downloads++;throw Error('must never download');}}})},count:()=>downloads};
}
test('exact task without unique bound native link never takes recent downloads',async()=>{
 for(const attributes of [[],['https://jstzbstatic.erp321.com/export/aaa.xlsx','https://jstzbstatic.erp321.com/export/bbb.xlsx']]){
  const p=mockPage(attributes);await assert.rejects(receiveKnownExport(p.page,{recordId:'123',expectedRows:2,output:'unused'}),{code:'FILE_IDENTITY'});assert.equal(p.count(),0);
 }
});
