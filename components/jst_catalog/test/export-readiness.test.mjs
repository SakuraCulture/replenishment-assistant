import test from 'node:test';
import assert from 'node:assert/strict';
import {exportRowReady} from '../src/product-export.mjs';
test('native pending row may have no count; completed original row must match',()=>{
  for(const text of ['', '206073780\n商品资料\n0\n生成中', '206073780\n商品资料\n4497\n99%'])
    assert.equal(exportRowReady(text,4497),false);
  assert.equal(exportRowReady('1\n206073780\n商品资料_20261103161051\n4497\n2026-11-03 16:10:51\n商品资料\n100%\n下载完成\n下载删除',4497),true);
});
test('completed mismatched count/type and failed native tasks remain failures',()=>{
  for(const text of ['商品资料\n4496\n下载完成','商品资料\n\n下载完成','订单资料\n4497\n下载完成'])
    assert.throws(()=>exportRowReady(text,4497),{code:'FILE_IDENTITY'});
  for(const text of ['商品资料\n0\n失败','商品资料\n0\n已取消'])
    assert.throws(()=>exportRowReady(text,4497),{code:'PLATFORM_FAILED'});
});
