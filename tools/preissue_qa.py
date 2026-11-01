"""Freeze/compare the 0.18 pre-sign editing candidate without touching daily data."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ['model.py', 'one_box_exception.py', 'allocation.py', 'default_config.json',
        'default_must_sell.json', 'quantity_contract.py', 'input_adapter.py',
        'warehouse_catalog.py', 'parameter_schema.py', 'new_products.py']


def sha(p):
    with Path(p).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(p, value):
    Path(p).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def freeze(q):
    q.mkdir(parents=True, exist_ok=False)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    archive = q/'baseline.zip'
    subprocess.run(['git', 'archive', '--format=zip', '-o', str(archive), commit], cwd=ROOT, check=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(q/'baseline')
    shutil.copytree(ROOT/'qa/business_flows_0170/inputs', q/'inputs')
    protected = {}
    for directory in [ROOT/'交付_0.17.1_rc1', Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手']:
        if directory.exists():
            protected.update({str(p):sha(p) for p in directory.rglob('*') if p.is_file()})
    manifest = {'commit':commit, 'core':{n:sha(ROOT/'src'/n) for n in CORE},
                'inputs':{str(p):sha(p) for p in (q/'inputs').rglob('*') if p.is_file()},
                'protected':protected}
    save(q/'baseline.json', manifest)
    print(json.dumps({'baseline':commit, 'inputs':len(manifest['inputs']), 'protected':len(protected)}))


def compare(q,candidate=None,reference=None):
    sys.path.insert(0, str(ROOT/'tools'))
    from parallel_qa import clean, diff, read, WHITELIST
    from structure_oracle import excel_snapshot
    a, b = reference or q/'model_old', candidate or q/'model_new'
    names = json.loads((a/'capture.json').read_text(encoding='utf-8'))['cases']
    if names != json.loads((b/'capture.json').read_text(encoding='utf-8'))['cases']:
        raise ValueError('Capture cases differ')
    cases = []
    def identity(v):
        if isinstance(v, dict):
            if {'rows', 'store', 'summary'} <= v.keys():
                keys = [(r['B'], r['E']) for r in v['rows']]
                if len(keys) != len(set(keys)) or any(s != v['store'] for s, _ in keys):
                    raise ValueError('Non-unique/wrong-store rows')
            for item in v.values():identity(item)
        elif isinstance(v, (list, tuple)):
            for item in v:identity(item)
    for name in names:
        x, y = read(a/(name+'.pklz')), read(b/(name+'.pklz'))
        identity(x); identity(y)
        cases.append({'name':name, 'differences':diff(clean(x), clean(y))})
    for p in a.glob('*.xlsx'):
        cases.append({'name':p.name, 'differences':diff(excel_snapshot(p), excel_snapshot(b/p.name))})
    frozen = json.loads((q/'baseline.json').read_text(encoding='utf-8'))
    changed = [p for p, h in {**frozen['protected'], **frozen['inputs']}.items() if sha(p) != h]
    core = {n:sha(ROOT/'src'/n) == digest for n, digest in frozen['core'].items()}
    report = {'passed':not changed and all(core.values()) and not any(c['differences'] for c in cases),
              'cases':cases, 'whitelist':WHITELIST, 'core_unchanged':core,
              'changed_protected':changed, 'baseline_commit':frozen['commit'],
              'comparison':'Exact values, types, full records, source and dictionary/list order; no SKU reordering.',
              'reference_capture':str(a),'candidate_capture':str(b)}
    save(q/('equivalence-final.json' if candidate else 'equivalence.json'), report)
    print(json.dumps({'passed':report['passed'], 'cases':len(cases), 'differences':sum(len(c['differences']) for c in cases)}))
    if not report['passed']:raise AssertionError('Business equivalence failed; see report')


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('action', choices=['freeze', 'compare']); p.add_argument('--qa', type=Path, required=True);p.add_argument('--candidate',type=Path)
    p.add_argument('--reference',type=Path);a = p.parse_args()
    if a.action=='freeze':freeze(a.qa.resolve())
    else:compare(a.qa.resolve(),a.candidate.resolve() if a.candidate else None,a.reference.resolve() if a.reference else None)
