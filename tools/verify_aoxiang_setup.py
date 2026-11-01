"""AX03 isolated checks. Keeps the immutable AX01/AX02 verification expectations intact."""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from verify_aoxiang_protocol import ROOT, sha, git


def template_contents(path):
    with zipfile.ZipFile(path) as archive:
        result = {name: archive.read(name) for name in archive.namelist()}
    core = ET.fromstring(result['docProps/core.xml'])
    for field in ['created', 'modified']:
        for node in core.findall('{http://purl.org/dc/terms/}' + field):
            node.text = '<dynamic-time>'
    result['docProps/core.xml'] = ET.tostring(core)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live-evidence', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.baseline.read_text(encoding='utf-8'))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONHASHSEED='0',
               REPLENISHMENT_DATA_DIR=str(output / 'python-settings'),
               LOCALAPPDATA=str(output / 'component-settings'), QT_QPA_PLATFORM='offscreen')
    protected = {}
    for group, base in [('component_runtime_sha256', ROOT / 'components/aoxiang'),
                        ('input_sha256', args.baseline.parent), ('core_sha256', ROOT),
                        ('protected', Path())]:
        for name, expected in manifest[group].items():
            target = base / name
            if sha(target) != expected:
                raise ValueError('Protected file changed: ' + str(target))
            protected[str(target)] = expected
    archive_path = args.baseline.parent / 'baseline-source.zip'
    if sha(archive_path) != manifest['source_archive_sha256']:
        raise ValueError('Baseline archive changed')
    changes, unchanged = {}, []
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            if not name.startswith('src/') or name.endswith('/'):
                continue
            before, after = hashlib.sha256(archive.read(name)).hexdigest(), sha(ROOT / name)
            if before == after:
                unchanged.append(name)
            else:
                changes[name] = {'before': before, 'after': after}
        original_template = archive.read('src/import_templates.py')
    if set(changes) != {'src/application_window.py', 'src/import_templates.py'}:
        raise ValueError('Unexpected original application changes: ' + repr(changes))
    old = type(sys)('original_templates')
    exec(compile(original_template, 'frozen/import_templates.py', 'exec'), old.__dict__)
    sys.path.insert(0, str(ROOT / 'src'))
    import import_templates as new
    templates = []
    for kind in old.TEMPLATES:
        old.write(kind, output / (kind + '-old.xlsx'))
        new.write(kind, output / (kind + '-new.xlsx'))
        if template_contents(output / (kind + '-old.xlsx')) != template_contents(output / (kind + '-new.xlsx')):
            raise ValueError('Existing template differs: ' + kind)
        templates.append(kind)
    new.write('aoxiang_stores', output / '门店目录最低模板.xlsx')
    live = json.loads(args.live_evidence.read_text(encoding='utf-8'))
    if not live['passed'] or live['saved']['state'] != 'SAVED' or live['before'] != live['after']:
        raise ValueError('Live setup evidence incomplete')
    logs = []
    import time
    commands = [
        ('environment', [sys.executable, 'tools/check_environment.py', '--report', str(output / 'environment.json')], ROOT),
        ('component', [str(ROOT / 'components/aoxiang/runtime/node.exe'), '--test', 'test/*.test.mjs'], ROOT / 'components/aoxiang'),
        ('python', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], ROOT),
    ]
    for name, command, cwd in commands:
        start = time.perf_counter()
        log = output / (name + '.txt')
        with log.open('x', encoding='utf-8', newline='\n') as stream:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        logs.append({'name': name, 'command': command, 'cwd': str(cwd), 'exit_code': result.returncode,
                     'seconds': time.perf_counter() - start, 'sha256': sha(log)})
        print(name + ': ' + str(result.returncode), flush=True)
        if result.returncode:
            raise SystemExit('Failed; inspect ' + str(log))
    py, js = [(output / (name + '.txt')).read_text(encoding='utf-8') for name in ['python', 'component']]
    if not py.rstrip().endswith('OK') or 'fail 0' not in js or 'skipped 0' not in js:
        raise ValueError('Suite incomplete or skipped')
    for name, expected in protected.items():
        if sha(name) != expected:
            raise ValueError('Protected file changed during verification: ' + name)
    source_names = [*changes, 'src/aoxiang_client.py', 'src/aoxiang_directory.py', 'src/aoxiang_setup_ui.py',
                    'components/aoxiang/src/machine-entry.mjs', 'components/aoxiang/src/machine-protocol.mjs',
                    'components/aoxiang/src/machine-setup.mjs', 'tools/probe_aoxiang_setup.py',
                    'tools/verify_aoxiang_setup.py']
    report = {'task': 'AX03', 'passed': True, 'head_before_commit': git('rev-parse', 'HEAD'),
              'branch': git('branch', '--show-current'), 'baseline_commit': manifest['baseline_commit'],
              'changes_to_original_app': changes, 'unchanged_original_app_files': unchanged,
              'template_identical': templates, 'template_dynamic_whitelist': ['docProps/core.xml/dcterms:created', 'docProps/core.xml/dcterms:modified'],
              'python_tests': int(re.search(r'Ran (\d+) tests', py)[1]),
              'component_tests': int(re.search(r'tests (\d+)', js)[1]), 'runs': logs,
              'protected': protected, 'code_sha256': {name: sha(ROOT / name) for name in source_names},
              'live_evidence': {'file': str(args.live_evidence.resolve()), 'sha256': sha(args.live_evidence),
                                'target': live['targets'], 'directory_rows': live['directory_rows'],
                                'query_rows': live['saved']['targets'][0]['rows'],
                                'scope': live['scope'], 'original_files_unchanged': live['before'] == live['after']},
              'limitations': ['Source Qt tests, not EXE acceptance.', 'Live setup query only, no export or purchase creation.',
                              'Automatic download adoption, procurement and receipt integration remain subsequent phases.',
                              'Test-suite times are not product performance measurements.']}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({k: report[k] for k in ['task', 'passed', 'python_tests', 'component_tests']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
