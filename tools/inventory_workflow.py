import sys,json,hashlib,time
from pathlib import Path
from collections import Counter
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from file_intake import check_file
import engine
qa=ROOT/'qa/full_workflow_073'
folder=Path(r'C:\Users\lenovo\Downloads\补货建议导出服务_20251020110049814545021961410193758')
if __name__=='__main__':
    result=[];status_field=engine.config()['sales_fields']['replenishment_status']
    for i,p in enumerate(sorted(folder.glob('*.xlsx'))):
        start=time.perf_counter();v=check_file(i,str(p));row={**v['report'],'sha256':v.get('hash'),'seconds':time.perf_counter()-start,'names':v.get('names'), 'status_values':dict(Counter(str(r['values'].get(status_field)) for _,r in v.get('staged',[])))}
        result.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
        (qa/'inputs_manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    files=['src/model.py','src/one_box_exception.py','src/allocation.py','src/default_config.json','src/work_repository.py','交付_0.7.2_rc1/补货助手/补货助手.exe','交付_0.7.2_rc1/补货助手_0.7.2_rc1.zip']
    manifest={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in files}
    (qa/'protected_hashes.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
