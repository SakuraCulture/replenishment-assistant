import test from 'node:test';
import assert from 'node:assert/strict';
import {downloadRequestMatches,downloadResponseUrl} from '../src/product-export.mjs';
const history='https://www.erp321.com/app/common/export/export_log.aspx';
function request(args=['123'],method='CheckAndGetExportData',url=history){
  return {method:()=> 'POST',url:()=>url,postData:()=>new URLSearchParams({__CALLBACKID:'JTable1',
    __CALLBACKPARAM:JSON.stringify({Method:method,Args:args,CallControl:'{page}'})}).toString()};
}
test('native download callback must identify exactly the original task and action',()=>{
  assert.equal(downloadRequestMatches(request(),history,'123'),true);
  for(const r of [request(['456']),request(['123','456']),request(['123'],'Update'),request(['123'],'CheckAndGetExportData','https://other.test/app/common/export/export_log.aspx')])
    assert.equal(downloadRequestMatches(r,history,'123'),false);
});
test('callback requires native success and the allowlisted XLSX address',()=>{
  const ok={IsSuccess:true,GotoLogin:false,ReturnValue:'https://jstzbstatic.erp321.com/export/abcdef.xlsx?test=synthetic'};
  assert.equal(downloadResponseUrl('0|'+JSON.stringify(ok)).pathname,'/export/abcdef.xlsx');
  for(const patch of [{IsSuccess:false},{GotoLogin:true},{ExceptionMessage:'denied'},{ReturnValue:'https://other.test/export/abcdef.xlsx'}, {ReturnValue:null}])
    assert.throws(()=>downloadResponseUrl('0|'+JSON.stringify({...ok,...patch})),{code:'FILE_IDENTITY'});
  assert.throws(()=>downloadResponseUrl(JSON.stringify(ok)),{code:'FILE_IDENTITY'});
});
test('native five-minute limit is a recoverable wait, not a new export',()=>{
  assert.throws(()=>downloadResponseUrl('0|'+JSON.stringify({IsSuccess:true,ReturnValue:null,Message:'相同的文件不要频繁导出，请5分钟后再试!'})),{code:'DOWNLOAD_RATE_LIMIT'});
});
