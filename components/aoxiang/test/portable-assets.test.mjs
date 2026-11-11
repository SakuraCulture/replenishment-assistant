import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {portablePath} from '../src/portable-assets.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {sha256,readJson} from '../src/common.mjs';

test('portable read paths preserve original hashes and existing user files',{concurrency:false},async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'portable-')),prior=process.env.REPLENISHMENT_PORTABLE_PATHS;
  try{
    const original=path.join(root,'old','source.xlsx'),asset=path.join(root,'迁入','source.xlsx'),json=path.join(root,'迁入','proof.json'),marker=path.join(root,'map.json');
    await fs.mkdir(path.dirname(asset));await fs.writeFile(asset,'frozen');await fs.writeFile(json,'{"proof":"kept"}');
    await fs.writeFile(marker,JSON.stringify({version:1,paths:{[original]:'迁入/source.xlsx',[path.join(root,'missing.json')]:'迁入/proof.json'},contents:{[sha256('frozen')]:['迁入/source.xlsx']}}));process.env.REPLENISHMENT_PORTABLE_PATHS=marker;
    assert.equal(await portablePath(original),asset);assert.equal(await fileHash(original),sha256('frozen'));assert.deepEqual(await readJson(path.join(root,'missing.json')),{proof:'kept'});
    assert.equal(await portablePath(path.join(root,'new-output-root','source.xlsx'),{sha256:sha256('frozen')}),asset);
    await fs.mkdir(path.dirname(original));await fs.writeFile(original,'user-new-file');assert.equal(await portablePath(original),original);assert.equal(await fileHash(original),sha256('user-new-file'));
  }finally{if(prior===undefined)delete process.env.REPLENISHMENT_PORTABLE_PATHS;else process.env.REPLENISHMENT_PORTABLE_PATHS=prior;await fs.rm(root,{recursive:true,force:true});}
});

test('portable map rejects escaping files',{concurrency:false},async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'portable-')),prior=process.env.REPLENISHMENT_PORTABLE_PATHS;
  try{
    const original=path.join(root,'missing.xlsx'),marker=path.join(root,'map.json');await fs.writeFile(marker,JSON.stringify({version:1,paths:{[original]:'../secret.xlsx'},contents:{}}));process.env.REPLENISHMENT_PORTABLE_PATHS=marker;
    await assert.rejects(portablePath(original),e=>e.code==='MIGRATION_PATH_INVALID');
  }finally{if(prior===undefined)delete process.env.REPLENISHMENT_PORTABLE_PATHS;else process.env.REPLENISHMENT_PORTABLE_PATHS=prior;await fs.rm(root,{recursive:true,force:true});}
});
