"""Read-only audit: never regenerate or overwrite received baseline evidence."""
import hashlib,json,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=root,text=True,encoding='utf-8').strip()
def audit():
    evidence=root/'releases/0.3.0';dest=root/'交付_0.3/补货助手'
    expected=json.loads((evidence/'delivery_sha256.json').read_text(encoding='utf-8'))
    actual={str(p.relative_to(dest)):digest(p) for p in dest.rglob('*') if p.is_file()}
    assert expected==actual,'0.3 delivery changed'
    release=json.loads((evidence/'release_report.json').read_text(encoding='utf-8'))
    assert digest(Path(release['archive']))==release['zip_sha256']
    assert git('rev-parse','v0.3.0-baseline^{}')==git('rev-parse','7b8da6b')
    tracked=git('ls-files','-z').split('\0')
    prohibited=[p for p in tracked if p and (p.split('/')[0] in {'.venv','qa','settings'} or p.endswith(('.xlsx','.xls','.csv','.exe','.zip','.log','.dll','.pyd')))]
    assert not prohibited,prohibited
    required=['src/model.py','src/app.py','src/app.ico','src/default_config.json','src/default_must_sell.json','build.ps1','verify_budget.py','releases/0.3.0/baseline_audit.json']
    assert all(p in tracked for p in required)
    immutable=['model.py','input_adapter.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']
    for p in immutable:
        old=subprocess.check_output(['git','show','v0.3.0-baseline:src/'+p],cwd=root)
        assert hashlib.sha256(old).hexdigest()==digest(root/'src'/p),p
    return {'passed':True,'baseline_commit':git('rev-parse','v0.3.0-baseline^{}'),'baseline_tag_object':git('rev-parse','v0.3.0-baseline'),'delivery_files':len(actual),'zip_sha256':release['zip_sha256'],'rules_unchanged':immutable,'remotes':git('remote','-v'),'scope':'file integrity and tracking only; not business/UI or reproducible build verification'}
if __name__=='__main__':print(json.dumps(audit(),ensure_ascii=False,indent=2))
