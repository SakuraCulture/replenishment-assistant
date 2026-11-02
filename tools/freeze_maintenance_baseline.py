"""Freeze source and protected inputs without opening user repositories for writing."""
import hashlib, json, subprocess, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'qa/maintenance_0270'

def sha(p):
    return hashlib.file_digest(p.open('rb'), 'sha256').hexdigest()

if __name__ == '__main__':
    QA.mkdir(parents=True, exist_ok=True)
    target = QA / 'baseline.json'
    if target.exists():
        raise SystemExit('Baseline already frozen; do not overwrite')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'archive', '--format=zip', '-o', str(QA/'source.zip'), commit], cwd=ROOT, check=True)
    with zipfile.ZipFile(QA/'source.zip') as z:
        z.extractall(QA/'before_source')
    protected = [ROOT/'src'/s for s in ['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']]
    protected += [ROOT/'交付_0.26.1_rc1/补货助手/补货助手.exe',Path('C:/Users/lenovo/Desktop/参数.xlsx')]
    inputs = json.loads((ROOT/'qa/aoxiang_integration_20261101/inputs/baseline.json').read_text('utf-8')) if (ROOT/'qa/aoxiang_integration_20261101/inputs/baseline.json').exists() else {}
    target.write_text(json.dumps({'baseline':commit,'protected':{str(p):sha(p) for p in protected},'inputs':inputs},ensure_ascii=False,indent=2),encoding='utf-8')
    print(commit)
