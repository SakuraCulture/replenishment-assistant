"""Serial, isolated actual-EXE acceptance suite for a fresh candidate build."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--exe', type=Path, required=True)
    p.add_argument('--qa', type=Path, required=True)
    p.add_argument('--build', required=True)
    a = p.parse_args()
    qa = a.qa.resolve()
    fixture = qa / 'normal'
    common = ['--exe', str(a.exe.resolve())]
    cases = [
        ('business', 'tools/verify_business_exe.py', ['--fixture', str(fixture / 'payload.json')]),
        ('fixes', 'tools/verify_business_exe.py', ['--fixture', str(fixture / 'payload.json'), '--real-result', str(qa / 'equivalence1/new/real_None_True.pklz')]),
        ('normal', 'verify_normal_exe.py', ['--fixtures', str(fixture)]),
        ('delivery', 'verify_export_exe.py', ['--fixtures', str(fixture)]),
        ('history', 'tools/verify_history_exe.py', ['--snapshot', str(ROOT / 'qa/archive_072/snapshot.sqlite3')]),
        ('work-io', 'tools/verify_work_io_exe.py', ['--snapshot', str(ROOT / 'qa/storage_080/real_current/settings/workbench.sqlite3')]),
        ('storage', 'tools/verify_storage_exe.py', ['--fixture', str(fixture / 'payload.json')]),
    ]
    assert all(not (qa / f'exe-{name}-{a.build}').exists() for name, _, _ in cases)
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    for name, script, args in cases:
        target = qa / f'exe-{name}-{a.build}'
        print('start', name, flush=True)
        start = time.monotonic()
        with target.with_suffix('.log').open('x', encoding='utf-8') as log:
            subprocess.run([sys.executable, str(ROOT / script), *common, *args, '--output', str(target)],
                           cwd=ROOT, env=env, stdout=log, stderr=log, check=True)
        print('passed', name, round(time.monotonic() - start, 3), flush=True)


if __name__ == '__main__':
    main()
