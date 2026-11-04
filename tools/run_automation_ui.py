import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--out', type=Path, required=True); p.add_argument('--exe', type=Path); p.add_argument('--codex-client',action='store_true'); a = p.parse_args()
    out = a.out.resolve()
    if out.exists(): raise FileExistsError('Use a new isolated acceptance directory')
    out.mkdir(parents=True); (out / 'synthetic.json').write_text('{}', encoding='utf-8')
    command = [str(a.exe.resolve())] if a.exe else [sys.executable, str(ROOT / 'src/app.py')]
    with (out / 'process.log').open('w', encoding='utf-8') as log:
        extra={'REPLENISHMENT_CLIENT_RUNNER':str(ROOT/'tools/verify_codex_client.py'),'REPLENISHMENT_CLIENT_PYTHON':sys.executable} if a.codex_client else {}
        result = subprocess.run([*command, '--automation-smoke', str(out)], cwd=ROOT,
            env={**os.environ, **extra, 'REPLENISHMENT_DATA_DIR': str(out / 'settings')}, stdout=log, stderr=log, timeout=300)
    report = json.loads((out / 'automation-report.json').read_text('utf-8'))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.returncode or not report['passed']: raise SystemExit(1)
