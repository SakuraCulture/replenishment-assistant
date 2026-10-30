"""Compare fixed pre-0.7 source capture to current unselected-new-product path."""
import argparse,json,subprocess
from pathlib import Path
from parallel_qa import clean,diff,read,sha,js,WHITELIST,excel_snapshot,source_hashes,ROOT


def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True);a=p.parse_args();q=a.qa.resolve();candidate=a.candidate.resolve();baseline=q/'baseline-capture'
    names=json.loads((baseline/'capture.json').read_text(encoding='utf-8'))['cases'];cap=json.loads((candidate/'capture.json').read_text(encoding='utf-8'));assert names==cap['cases'];cases=[]
    def identity(value):
        if isinstance(value,dict):
            if 'rows' in value and 'summary' in value and 'store' in value:
                keys=[(d['B'],d['E']) for d in value['rows']];assert len(keys)==len(set(keys));assert all(k[0]==value['store'] for k in keys)
            for item in value.values():identity(item)
        elif isinstance(value,(list,tuple)):
            for item in value:identity(item)
    for name in names:
        old,new=read(baseline/(name+'.pklz')),read(candidate/(name+'.pklz'));identity(old);identity(new);changes=diff(clean(old),clean(new));cases.append({'case':name,'differences':len(changes),'details':changes})
    for path in baseline.glob('*.xlsx'):
        changes=diff(excel_snapshot(path),excel_snapshot(candidate/path.name));cases.append({'case':path.name,'differences':len(changes),'details':changes})
    manifest=json.loads((q/'baseline_manifest.json').read_text(encoding='utf-8'));changed=[p for p,h in manifest['protected'].items() if not Path(p).exists() or sha(p)!=h]
    core={p:{'before':h,'after':sha(ROOT/p)} for p,h in manifest['core'].items()};count=sum(c['differences'] for c in cases)
    assert cap['source_hashes']==source_hashes(ROOT),'source changed since capture'
    report={'passed':not count and not changed and all(x['before']==x['after'] for x in core.values()),'version':json.loads((ROOT/'src/version.json').read_text(encoding='utf-8'))['version'],'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'baseline_commit':manifest['baseline_commit'],'candidate_sources':source_hashes(ROOT),'differences':count,'strict_order':True,'whitelist':WHITELIST,'cases':cases,'changed_protected':changed,'protected_file_count':len(manifest['protected']),'core':core,'scope':'No new products selected: all original business fields, lists, key ordering and types; 14244 rows, six budget/exception variants, custom settings, warehouse and multi-file scenarios, five Excel structures/styles.'}
    js(candidate/'normal_equivalence.json',report);print(json.dumps({'passed':report['passed'],'differences':count,'cases':len(cases)}));assert report['passed']


if __name__=='__main__':main()
