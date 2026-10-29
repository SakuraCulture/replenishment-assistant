"""Re-run export with the frozen caller order; never reorder workbook cells to compare."""
import argparse,sys,json,shutil,hashlib
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--qa',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
root=a.root.resolve();sys.path[:0]=[str(root/'src'),str(root/'tools')]
from structure_oracle import read,write_json,compare,sha,source_hashes
import batch_service
baseline=a.baseline.resolve();candidate=a.candidate.resolve();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
manifest=json.loads((candidate/'capture.json').read_text(encoding='utf-8'));assert manifest['source_hashes']==source_hashes(root)
old=read(baseline/'dual_parallel.pklz');new=read(candidate/'dual_parallel.pklz');assert set(old)==set(new)
order=list(old);results={s:new[s] for s in order}
statuses={s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in results.items()};statuses['FAIL']={'name':'失败店','state':'计算失败','error':'预设失败'}
for f in candidate.iterdir():
 if f.suffix=='.pklz' or f.name in {'real.xlsx','filtered.xlsx','capture.json'}:shutil.copy2(f,out/f.name)
batch_service.export_batch(results,statuses,out/'batch.xlsx')
replay={'reason':'Original test constructed export input dictionaries from unordered process completion; baseline and candidate therefore supplied different row-order inputs. The production exporter is unchanged.','baseline_order':order,'original_candidate_order':list(new),'rendered_candidate_order':list(results),'comparison':'Exact cell coordinates, types, values, styles and worksheet structures; no output sorting or extra field exclusions.','baseline_workbook_sha256':sha(baseline/'batch.xlsx'),'original_candidate_workbook_sha256':sha(candidate/'batch.xlsx'),'controlled_candidate_workbook_sha256':sha(out/'batch.xlsx'),'preserved_original_report':str(candidate/'equivalence.json'),'tool_sha256':sha(Path(__file__))}
write_json(out/'export_replay.json',replay)
passed=compare(a.qa.resolve(),baseline,out,out/'equivalence.json');assert passed
