"""Seal locally tested 0.9.0 candidate; never replace old packages or publish."""
import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'e45b9d08cddbced1bf8566281987431534bbf5b7'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')


def git(*args):
    return subprocess.check_output(['git', '-c', 'core.quotepath=false', *args], cwd=ROOT, text=True, encoding='utf-8').strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--delivery', type=Path, required=True)
    parser.add_argument('--qa', type=Path, required=True)
    args = parser.parse_args()
    dest, qa = args.delivery.resolve(), args.qa.resolve()
    meta = load(dest / '版本信息.json')
    assert meta['version'] == '0.9.0' and meta['model_version'] == '0.3.1-rules'
    assert not git('status', '--porcelain'), 'seal from a clean local evidence-tools commit'
    head, build = git('rev-parse', 'HEAD'), meta['source_commit']
    subprocess.run(['git', 'merge-base', '--is-ancestor', build, head], cwd=ROOT, check=True)
    changed = git('diff', '--name-only', build, head).splitlines()
    assert all(p.startswith(('tools/', 'docs/', 'releases/')) or p in ('README.md', '使用说明.txt') for p in changed), changed
    files = {
        'equivalence': 'equivalence_rc2/report.json', 'fixes': 'fixes_complete/report.json',
        'business': 'business_rc2/report.json', 'form_quantities': 'form-quantities-final/report.json', 'races': 'races1/report.json',
        'controls': 'io_final/report.json', 'selected_controls': 'selected_final/report.json',
        'normal': 'normal/normal_report.json', 'chunks': 'chunks/report.json',
        'projections': 'projections/report.json', 'structure': 'structure.json',
        'real_exports': 'real_exports2/report.json', 'legacy_exports': 'legacy_exports/report.json',
        'performance': 'performance_rc2/report.json',
    }
    ui = {
        'business_ui': 'exe-business-rc2/business_ui_report.json', 'fixes_ui': 'exe-fixes-rc2/fixes_ui_report.json',
        'normal_ui': 'exe-normal-rc2/normal_ui_report.json', 'delivery_ui': 'exe-delivery-rc2/export_ui_report.json',
        'history_ui': 'exe-history-rc2/history_ui_report.json', 'work_io_ui': 'exe-work-io-rc2/work_io_report.json',
        'storage_ui': 'exe-storage-rc2/storage_ui_report.json',
    }
    evidence = {}
    exe_hash = sha(dest / '补货助手.exe')
    for name, rel in {**files, **ui}.items():
        data = load(qa / rel)
        assert data['passed'], (name, rel)
        if name in ui:
            assert data['exe_sha256'] == exe_hash, name
            version = data['version']
            commit = version.get('source_commit') if isinstance(version, dict) else data.get('source_commit')
            assert commit == build, (name, commit, build)
            assert not data.get('development', False) and not data.get('remaining_children', []), name
        evidence[name] = data
    eq = evidence['equivalence']
    assert eq['baseline_commit'] == BASELINE and eq['candidate_commit'] == build
    assert eq['unexpected_differences'] == 0 and eq['input_drift'] == [] and eq['strict_order']
    actual = {f.name: sha(f) for f in (ROOT / 'src').glob('*') if f.is_file()}
    assert evidence['performance']['runs'] == 3
    for kind in ('old', 'new'):
        for i in range(3):
            run = load(qa / f'performance_rc2/{kind}-{i}/result.json')
            assert run['passed']
            if kind == 'new':
                assert run['source_hashes'] == actual, 'source drift after performance measurement'
    protected = {**load(qa / 'protected.json'), **eq['frozen_inputs']}
    drift = [p for p, digest in protected.items() if not Path(p).is_file() or sha(p) != digest]
    assert not drift, drift
    cores = eq['core_hashes']
    for name, digests in cores.items():
        assert digests['old'] == digests['new'] == sha(ROOT / 'src' / name), name
    report_dir = ROOT / 'releases' / '0.9.0' / meta['build_id']
    archive = dest.parent / f"补货助手_0.9.0_{meta['build_id']}.zip"
    assert not report_dir.exists() and not archive.exists()
    assert (qa / '交付报告.md').is_file()
    report_dir.mkdir(parents=True)
    for name, rel in {**files, **ui}.items():
        shutil.copy2(qa / rel, report_dir / (name + '.json'))
    # Keep all six raw runs, including measured progress, not only medians.
    for kind in ('old', 'new'):
        for i in range(3):
            for filename in ('result.json', 'progress.json'):
                shutil.copy2(qa / f'performance_rc2/{kind}-{i}/{filename}', report_dir / f'performance-{kind}-{i}-{filename}')
    screenshots = report_dir / 'screenshots'
    screenshots.mkdir()
    for path in (qa / 'exe-business-rc2').glob('*.png'):
        shutil.copy2(path, screenshots / path.name)
    save(report_dir / 'preservation.json', {'passed': True, 'checked_files': len(protected), 'hashes': protected, 'differences': drift})
    save(report_dir / 'source_audit.json', {'passed': True, 'baseline': BASELINE, 'build_commit': build,
        'core_hashes': cores, 'all_source_hashes': actual,
        'changed_files': git('diff', '--name-only', BASELINE, build).splitlines(),
        'allowed_calculation_field_changes': [],
        'allowed_export_changes': 'only individually listed merchant V-column decision reason text; no other values, types, order or styles',
        'allowed_behavior_changes': ['pre-commit cancellation rollback', 'exact revision/SKU review', 'business forms and independent return data / source attachments']})
    shutil.copy2(qa / '交付报告.md', report_dir / '交付报告.md')
    shutil.copy2(qa / '交付报告.md', dest / '性能与验收报告.md')
    receipt = {'passed': True, 'version': '0.9.0', 'build_id': meta['build_id'],
        'baseline_commit': BASELINE, 'source_commit': build, 'evidence_tools_commit': head,
        'branch': git('branch', '--show-current'), 'model_version': meta['model_version'],
        'exe_sha256': exe_hash, 'business_differences': 0,
        'real_reason_changes': len(evidence['real_exports']['allowed_reason_changes']),
        'legacy_reason_changes': len(evidence['legacy_exports']['allowed_reason_changes']),
        'old_package_and_inputs_preserved': True, 'no_remote_push_or_merge': True,
        'delivery': str(dest), 'evidence': str(report_dir),
        'limits': ['old missing source files are explicitly unavailable, never reconstructed from current master',
            'pre-commit cancellation can retain safety backups or physical storage metadata',
            'return quantities are recorded facts, not automatically rounded or reallocated',
            'blank returns preserve previous quantity; notes-only changes do not create a quantity revision',
            'actual ordering and receipt imports are future scope',
            'performance is specific to frozen five-store sample and this Windows machine; no claim of major speedup']}
    save(dest / '验收记录.json', receipt)
    manifest = {f.relative_to(dest).as_posix(): sha(f) for f in dest.rglob('*') if f.is_file()}
    save(report_dir / 'files_sha256.json', manifest)
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as bundle:
        for rel in manifest:
            bundle.write(dest / rel, dest.name + '/' + rel)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
    receipt.update(zip=str(archive), zip_sha256=sha(archive), delivered_files=len(manifest))
    save(report_dir / 'release_report.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
