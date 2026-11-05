"""Replay native Qt and Windows spawn checks against a built 0.39.0 EXE.

Creates only a new isolated output folder. No user settings or platform requests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    exe, out = args.exe.resolve(), args.output.resolve()
    if out.exists():
        raise FileExistsError('Use a new isolated output directory')
    if not exe.is_file():
        raise FileNotFoundError(exe)
    info = json.loads((exe.parent / '_internal/build_info.json').read_text('utf-8'))
    if info['version'] != '0.39.0':
        raise ValueError('Expected a 0.39.0 candidate')
    before = hashlib.sha256(exe.read_bytes()).hexdigest()
    subprocess.run([sys.executable, '-B', str(ROOT / 'tools/verify_analytics_0390.py'),
                    '--fixture-only', '--output', str(out)], cwd=ROOT, check=True)
    with (out / 'process.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([str(exe), '--analytics-smoke', str(out)], cwd=ROOT,
                                env={**os.environ, 'REPLENISHMENT_DATA_DIR': str(out / 'settings'),
                                     'QT_QPA_PLATFORM': 'windows'},
                                stdout=log, stderr=log, timeout=300)
    report = json.loads((out / 'exe-report.json').read_text('utf-8'))
    evidence = {'build': info, 'exe_sha256': before,
                'exe_unchanged': hashlib.sha256(exe.read_bytes()).hexdigest() == before,
                'process_exit_code': result.returncode}
    (out / 'delivery-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.returncode or not report['passed'] or not report['frozen_exe'] or not evidence['exe_unchanged']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
