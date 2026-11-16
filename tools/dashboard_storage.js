// Replaces only storage/import behavior in the existing offline dashboard.
const STORE_BUNDLE='warehouse_stockout_bundle_v2';
const embeddedRecords=DATA.records.slice();
let storageFailure='',bundleRaw=null,bundleCache=null;
function validateBundle(value){
  if(!value||value.schema!==2||!Array.isArray(value.imported)||!Array.isArray(value.actions))throw new Error('备份或保存资料格式不符');
  if(value.imported.some(r=>!Array.isArray(r)||r.length!==8||!r[0]||!r[1]||!r[2]||typeof r[4]!=='number'||!Number.isFinite(r[4])))throw new Error('库存更正记录不完整或无效，请保留原文件核对');
  if(value.actions.some(a=>!a||typeof a!=='object'||Array.isArray(a)))throw new Error('治理记录格式不符');
  return value;
}
function readBundle(strict=false){
  try{
    const raw=localStorage.getItem(STORE_BUNDLE);
    if(raw!==null&&raw===bundleRaw&&bundleCache)return bundleCache;
    const value=validateBundle(raw===null?{schema:2,imported:JSON.parse(localStorage.getItem(STORE_IMPORTED)||'[]'),actions:JSON.parse(localStorage.getItem(STORE_ACTIONS)||'[]')}:JSON.parse(raw));
    if(raw!==null){bundleRaw=raw;bundleCache=value;}
    return value;
  }catch(error){
    storageFailure='无法读取原保存资料；请保留原页面和备份，核对后再导入：'+error.message;
    if(strict)throw new Error(storageFailure);
    return {schema:2,imported:[],actions:[]};
  }
}
function commitBundle(value){
  validateBundle(value);
  const raw=JSON.stringify(value);
  localStorage.setItem(STORE_BUNDLE,raw);
  if(localStorage.getItem(STORE_BUNDLE)!==raw)throw new Error('保存结果未确认，请重新打开核对；当前页面保留原数据');
  bundleRaw=raw;bundleCache=value;storageFailure='';
}
function readActions(){return JSON.parse(JSON.stringify(readBundle().actions));}
function saveActions(actions){
  try{const previous=readBundle(true);commitBundle({...previous,actions});return true;}
  catch(error){alert('治理记录未确认保存：'+error.message+'。请先导出完整备份。');return false;}
}
function readImported(){return readBundle().imported;}
function persistImported(imported){const previous=readBundle(true);commitBundle({...previous,imported});}
function mergedRecords(imported){
  const records=embeddedRecords.slice(),indices=new Map(records.map((r,i)=>[r[0]+'|'+r[1]+'|'+r[2],i]));
  for(const r of imported){const key=r[0]+'|'+r[1]+'|'+r[2];if(indices.has(key))records[indices.get(key)]=r;else{indices.set(key,records.length);records.push(r);}}
  return records;
}
function adoptRecords(records){
  DATA.records=records;DATA.dates=[...new Set(records.map(r=>r[0]))].sort();
  $('dateSelect').innerHTML=DATA.dates.map(d=>`<option value="${d}">${d}</option>`).join('');$('dateSelect').value=DATA.dates[DATA.dates.length-1];
  skuPage=0;renderAll();
}
function handleCSV(e){
  const file=e.target.files[0];if(!file)return;
  const reader=new FileReader();
  reader.onload=()=>{
    try{
      const parsed=parseCSV(reader.result),headers=parsed.shift().map(x=>x.trim()),idx={};
      ['观测日期','供货仓','商品编码','商品名称','可用库存','商品状态','商品分类','供应商名称'].forEach(k=>idx[k]=headers.indexOf(k));
      const missing=Object.keys(idx).filter(k=>idx[k]<0);if(missing.length)throw new Error('CSV缺少字段：'+missing.join('、'));
      const previous=readBundle(true),records=mergedRecords(previous.imported),indices=new Map(records.map((r,i)=>[r[0]+'|'+r[1]+'|'+r[2],i]));
      const updates=new Map(previous.imported.map(r=>[r[0]+'|'+r[1]+'|'+r[2],r]));let added=0,changed=0;
      for(const row of parsed){
        const rec=[normalizeDate(row[idx['观测日期']]),row[idx['供货仓']],row[idx['商品编码']],row[idx['商品名称']],Number(row[idx['可用库存']]||0),row[idx['商品状态']]||'未知',row[idx['商品分类']]||'未分类',row[idx['供应商名称']]||'未填写'];
        if(!rec[0]||!rec[1]||!rec[2])continue;
        const key=rec[0]+'|'+rec[1]+'|'+rec[2];
        if(indices.has(key)){records[indices.get(key)]=rec;changed++;}else{indices.set(key,records.length);records.push(rec);added++;}
        updates.set(key,rec);
      }
      commitBundle({...previous,imported:[...updates.values()]});
      adoptRecords(records);$('importStatus').textContent=`已保存：新增 ${added} 条，更正 ${changed} 条；重开仍保留`;
    }catch(error){$('importStatus').textContent='导入未确认保存，当前页面保留原数据：'+error.message;alert('导入未确认保存：'+error.message+'。请保留CSV并先导出已有完整备份。');}
    e.target.value='';
  };
  reader.onerror=()=>{$('importStatus').textContent='CSV读取失败，原数据保留';e.target.value='';};
  reader.readAsText(file,'UTF-8');
}
function exportFullBackup(){
  try{
    const bundle=readBundle(true),data={kind:'warehouse-stockout-dashboard-backup',schema:2,exportedAt:new Date().toISOString(),bundle};
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download='大仓缺货治理看板完整备份.json';a.click();URL.revokeObjectURL(url);
  }catch(error){alert('未导出完整备份：'+error.message);}
}
function restoreFullBackup(e){
  const file=e.target.files[0];if(!file)return;const reader=new FileReader();
  reader.onload=()=>{
    try{
      const value=JSON.parse(reader.result);
      if(value.kind!=='warehouse-stockout-dashboard-backup'||value.schema!==2)throw new Error('请选择包含库存更正与治理记录的完整备份');
      const bundle=validateBundle(value.bundle);
      readBundle(true);
      if(!confirm(`恢复此备份将替换当前保存的库存更正和治理记录：库存 ${bundle.imported.length} 条，治理 ${bundle.actions.length} 条。请确认已保留当前完整备份。`))return;
      const records=mergedRecords(bundle.imported);commitBundle(bundle);adoptRecords(records);
      $('importStatus').textContent=`已恢复并保存：库存更正 ${bundle.imported.length} 条，治理记录 ${bundle.actions.length} 条`;
    }catch(error){$('importStatus').textContent='完整备份未确认恢复：'+error.message;alert('未确认恢复：'+error.message);}
    finally{e.target.value='';}
  };
  reader.onerror=()=>{$('importStatus').textContent='备份读取失败，原数据保留';e.target.value='';};reader.readAsText(file,'UTF-8');
}
function initStorageControls(){
  const anchor=$('importStatus'),container=anchor.parentElement;
  for(const [id,text,click] of [['exportFullBackup','导出完整备份',exportFullBackup],['restoreFullBackup','恢复完整备份',()=>input.click()]]){
    const b=document.createElement('button');b.id=id;b.type='button';b.className='btn';b.textContent=text;b.addEventListener('click',click);container.insertBefore(b,anchor);
  }
  const input=document.createElement('input');input.id='backupInput';input.type='file';input.accept='.json,application/json';input.style.display='none';input.addEventListener('change',restoreFullBackup);container.appendChild(input);
  anchor.title='资料保存在当前浏览器；移动页面或更换浏览器前，请先导出完整备份。旧版仅导出的治理记录不含库存更正。';
  if(storageFailure)anchor.textContent=storageFailure;
}
