"""New synthetic files, actual Qt/EXE, owned offline Node and normal reopen."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser();p.add_argument('--out', type=Path, required=True);p.add_argument('--exe', type=Path);a = p.parse_args()
    out = a.out.resolve();out.mkdir(exist_ok=False);(out / 'temp').mkdir();tempfile.tempdir = str(out / 'temp')
    sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
    from test_aoxiang_purchase_tasks import PurchaseTaskTests
    from aoxiang_directory import atomic_json
    f = PurchaseTaskTests();f.setUp()
    try:
        template = out / '合成原生采购模板.xlsx';shutil.copy2(f.receipt['file'], template);f.receipt['file'] = str(template)
        for k in ('recordFile', 'latestFile'):atomic_json(f.receipt[k], f.receipt)
        assert f.client.close(timeout=10)
        data = {'synthetic': True, 'live_platform': False, 'path': str(f.f.path), 'head': f.f.h, 'payload': f.f.payload,
                'config': f.config, 'suppliers': f.suppliers, 'receipt': f.receipt, 'component_template': str(f.root / 'component'),
                'new_payment_plan': f.f.plan(f.f.scan('payment', [{'付款单商品数量': 2, '实际金额': .99}]))}
        atomic_json(out / 'fixture.json', data)
        shutil.copy2(ROOT / 'components/aoxiang/test/host-dispatch-fixture.mjs', out / 'offline-transport.mjs')
        env = os.environ.copy()
        for k in ('PYTHONPATH', 'PYTHONHOME', 'REPLENISHMENT_PORTABLE_PATHS', 'QT_PLUGIN_PATH'):env.pop(k, None)
        env.update(PYTHONUTF8='1', REPLENISHMENT_DATA_DIR=str(f.f.path.parent), LOCALAPPDATA=str(out / 'localappdata'), APPDATA=str(out / 'roaming'), QT_QPA_PLATFORM='windows')
        command = [str(a.exe.resolve())] if a.exe else [sys.executable, str(ROOT / 'src/app.py')]
        reports = []
        for phase in ('exercise', 'reopen'):
            with (out / ('process-' + phase + '.log')).open('wb') as log:
                process = subprocess.Popen([*command, '--five-fixes-smoke', str(out), phase], cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
                code = process.wait(timeout=240)
            file = out / ('report-' + phase + '.json')
            report = json.loads(file.read_text(encoding='utf-8')) if file.is_file() else {'passed': False, 'error': 'No report; inspect instance protection and process log'}
            report['exit_code'] = code;reports.append(report)
            if not report['passed'] or code:break
        result = {'passed': len(reports) == 2 and all(r['passed'] and r['exit_code'] == 0 for r in reports), 'reports': reports, 'live_platform': False,
                  'offline_transport_sha256': hashlib.sha256((out / 'offline-transport.mjs').read_bytes()).hexdigest()}
        if a.exe:result['exe_sha256'] = hashlib.sha256(a.exe.read_bytes()).hexdigest()
        atomic_json(out / 'acceptance.json', result)
        shutil.copytree(f.f.root, out / 'fixture-evidence')
        print(json.dumps({'passed': result['passed'], 'out': str(out), 'checks': sum(len(r.get('checks', [])) for r in reports)}, ensure_ascii=False))
        return 0 if result['passed'] else 1
    finally:f.tearDown()


if __name__ == '__main__':
    sys.exit(main())
