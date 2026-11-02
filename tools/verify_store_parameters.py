"""Strict existing capture comparison plus old/new signed workbooks (only U.hidden may change)."""
import argparse
import hashlib
import json
import os
import pickle
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def signed(source, result_path, out):
    sys.path.insert(0, str(source / 'src'))
    import normal_exports as ne
    import new_product_focus as focus
    result = pickle.loads(zlib.decompress(result_path.read_bytes()))
    # Synthetic signed identity; original frozen calculation rows remain untouched.
    order = {'number': 'BH-QA-ZC', 'revision': 1, 'current_revision': 1, 'store': result['store'],
             'day': '2026-11-02', 'payload': {'result': result, 'new_focus': focus.empty('2026-11')}}
    out.mkdir(parents=True, exist_ok=False)
    for warehouse in (None, '河北仓', '山东仓'):
        ne.export(order, out / ((warehouse or '兼容总单') + '.xlsx'), warehouse=warehouse)


def workbook(path, hide_expected):
    files, changes = {}, []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            root = ET.fromstring(z.read(name)) if name.endswith(('.xml', '.rels')) else None
            if name.startswith('xl/worksheets/sheet') and name.endswith('.xml'):
                columns = root.findall('s:cols/s:col', NS)
                cols = [c for c in columns if c.attrib.get('min') == c.attrib.get('max') == '21']
                if len(cols) != 1 or cols[0].attrib.get('hidden') != ('1' if hide_expected else None):
                    raise AssertionError('Unexpected U hidden flag: ' + str(path) + '/' + name)
                if hide_expected:
                    changes.append(name + '/cols/col[min=21,max=21]/@hidden: absent -> 1')
                    del cols[0].attrib['hidden']
            if name == 'docProps/core.xml':
                for tag in ('created', 'modified'):
                    for node in root.findall('{http://purl.org/dc/terms/}' + tag):
                        node.text = '<clock>'
            files[name] = ET.tostring(root) if root is not None else z.read(name)
    return files, changes


def compare(args):
    import parallel_qa as qa
    before, after, baseline = [Path(x).resolve() for x in (args.before, args.after, args.baseline)]
    baseline_meta = json.loads((baseline.parent / 'baseline.json').read_text(encoding='utf-8'))
    names = json.loads((before / 'capture.json').read_text(encoding='utf-8'))['cases']
    if names != json.loads((after / 'capture.json').read_text(encoding='utf-8'))['cases']:
        raise AssertionError('Case order changed')
    cases = []
    for name in names:
        changes = qa.diff(qa.clean(qa.read(before / (name + '.pklz'))), qa.clean(qa.read(after / (name + '.pklz'))))
        cases.append({'case': name, 'differences': changes})
    for p in before.glob('*.xlsx'):
        cases.append({'case': p.name, 'differences': qa.diff(qa.excel_snapshot(p), qa.excel_snapshot(after / p.name))})
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    for source, name in ((baseline, 'signed-before'), (ROOT, 'signed-after')):
        subprocess.run([sys.executable, __file__, 'signed', '--source', str(source), '--result',
                        str(before / 'real_None_True.pklz'), '--output', str(out / name)], check=True)
    signed_changes = []
    for p in (out / 'signed-before').glob('*.xlsx'):
        old, _ = workbook(p, False)
        new, allowed = workbook(out / 'signed-after' / p.name, True)
        differences = [name for name in dict.fromkeys([*old, *new]) if old.get(name) != new.get(name)]
        signed_changes.append({'file': p.name, 'allowed': allowed, 'unexpected_differences': differences})
    core = {name: {'before': h, 'after': qa.sha(ROOT / 'src' / name)} for name, h in baseline_meta['core'].items()}
    result = {'baseline_commit': baseline_meta['commit'], 'strict_order_and_types': True,
              'whitelist': qa.WHITELIST, 'signed_whitelist': ['docProps/core.xml created/modified timestamps',
                  'exact U-column hidden attribute; no values/types/styles/headers/order/linkage excluded'],
              'cases': cases, 'signed': signed_changes, 'core': core,
              'passed': not any(c['differences'] for c in cases) and not any(c['unexpected_differences'] for c in signed_changes)
                  and all(v['before'] == v['after'] for v in core.values())}
    (out / 'equivalence.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': result['passed'], 'business_cases': len(cases), 'signed_files': len(signed_changes)}))
    if not result['passed']:
        raise AssertionError('See equivalence.json for exact differences')


def exe(args):
    executable = Path(args.exe).resolve()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env['REPLENISHMENT_DATA_DIR'] = str(out / 'settings')
    env.pop('QT_QPA_PLATFORM', None)
    env['PYTHONUTF8'] = '1'
    with (out / 'console.txt').open('w', encoding='utf-8') as log:
        process = subprocess.run([str(executable), '--store-parameters-smoke', str(Path(args.fixture).resolve()), str(out)],
                                 env=env, stdout=log, stderr=log, timeout=120)
    result = json.loads((out / 'exe-report.json').read_text(encoding='utf-8'))
    result['exit_code'] = process.returncode
    result['exe_sha256'] = hashlib.sha256(executable.read_bytes()).hexdigest()
    (out / 'exe-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    if process.returncode or not result['passed'] or not result.get('frozen_exe'):
        raise AssertionError('Actual EXE smoke failed')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['compare', 'signed', 'exe'])
    for name in ('baseline', 'before', 'after', 'output', 'source', 'result', 'exe', 'fixture'):
        p.add_argument('--' + name)
    a = p.parse_args()
    if a.mode == 'compare':compare(a)
    elif a.mode == 'signed':signed(Path(a.source), Path(a.result), Path(a.output))
    else:exe(a)
