"""Freeze a clean source commit, QA inputs and existing delivery without user writes."""
import argparse, hashlib, json, shutil, subprocess, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def freeze(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'archive', '--format=zip', '-o', str(output/'baseline.zip'), commit], cwd=ROOT, check=True)
    with zipfile.ZipFile(output/'baseline.zip') as z:
        z.extractall(output/'baseline')
    # Copy the existing frozen fixture corpus, then hash the new copies. Do not
    # reuse parallel_qa.freeze: it also scans unrelated deliveries and user state.
    shutil.copytree(ROOT/'qa/lifecycle_comparison_0300/inputs', output/'inputs')
    sample = ROOT/'qa/review_jst_catalog_20261103/已验收样本/临沂账号_商品资料_全部状态_20261103.xlsx'
    if not sample.exists():
        matches = list((ROOT/'qa/review_jst_catalog_20261103').rglob('*.xlsx'))
        if len(matches) != 1:
            raise ValueError('Expected the independently inspected 9207-row sample')
        sample = matches[0]
    shutil.copy2(sample, output/'inputs/catalog-real.xlsx')
    protected = {str(p): sha(p) for p in (output/'inputs').rglob('*') if p.is_file()}
    protected.update({str(p): sha(p) for p in (ROOT/'交付_0.30.0_rc1').rglob('*') if p.is_file()})
    protected[str(sample)] = sha(sample)
    ax = {p.relative_to(ROOT).as_posix(): sha(p) for p in (ROOT/'components/aoxiang/src').rglob('*') if p.is_file()}
    record = {'baseline': commit, 'protected': protected, 'aoxiang_source': ax,
              'source_zip_sha256': sha(output/'baseline.zip'), 'user_data_access': 'none; isolated QA only'}
    (output/'baseline.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
    print('Frozen', commit, 'protected', len(protected))


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--output', type=Path, required=True)
    freeze(p.parse_args().output)
