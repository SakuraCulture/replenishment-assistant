"""Read-only before/after protection manifest for storage-stage acceptance."""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CORE=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json','engine.py','batch_service.py','warehouse_catalog.py','input_adapter.py','table_records.py','parameter_schema.py','new_products.py','result_semantics.py']
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--check',type=Path);a=p.parse_args()
    if a.check:
        manifest=json.loads(a.check.read_text(encoding='utf-8'));diff=[name for name,h in manifest['protected'].items() if not Path(name).exists() or sha(name)!=h]
        report={'passed':not diff,'changed':diff,'baseline':manifest['baseline'],'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
    else:
        paths=[ROOT/'src'/n for n in CORE]+[ROOT/'qa/archive_072/snapshot.sqlite3',Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings/workbench.sqlite3']
        paths += list((ROOT/'交付_0.7.3_rc1').glob('*.zip'))+list((ROOT/'交付_0.7.3_rc1/补货助手').rglob('*'))
        inputs=json.loads((ROOT/'qa/full_workflow_073/inputs_manifest.json').read_text(encoding='utf-8'));paths += [Path(r['path']) for r in inputs]
        report={'baseline':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'protected':{str(f):sha(f) for f in paths if f.is_file()}}
    assert not a.output.exists();a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':report.get('passed'), 'protected':len(report.get('protected',{})),'changed':report.get('changed',[])},ensure_ascii=False));assert report.get('passed',True)
if __name__=='__main__':main()
