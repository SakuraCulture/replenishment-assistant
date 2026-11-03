import test from 'node:test';
import assert from 'node:assert/strict';
import {exportScopeValid} from '../src/product-export.mjs';
import {CatalogService} from '../src/service.mjs';
const scope={coid:'company-test',uid:'user-test'};
const request=()=>({coid:scope.coid,uid:scope.uid,data:{type:3,filterType:2,searchCondition:{enabled:'1,0,-1',c_id:'',queryFlds:['sku_id','enabled'],sku_type:1,orderBy:''}}});
test('captured native all-status all-data request passes',()=>assert.equal(exportScopeValid(request(),scope),true));
test('enabled-only request is blocked before transmission',()=>{const r=request();r.data.searchCondition.enabled='1';assert.equal(exportScopeValid(r,scope),false);});
test('selected-row range is blocked',()=>{const r=request();r.data.filterType=1;assert.equal(exportScopeValid(r,scope),false);});
test('foreign company is blocked',()=>{const r=request();r.coid='other';assert.equal(exportScopeValid(r,scope),false);});
test('additional supplier restriction is blocked',()=>{const r=request();r.data.searchCondition.supplier_id='supplier';assert.equal(exportScopeValid(r,scope),false);});
test('unknown export options are blocked',()=>{const r=request();r.data.skuIds=['A'];assert.equal(exportScopeValid(r,scope),false);});
test('order mutation is not exposed by the integrated catalog-only service',async()=>{
  const service=new CatalogService({store:{},driver:{}});
  await assert.rejects(service.command({action:'submitOrderImport'}),{code:'UNKNOWN_ACTION'});
});
