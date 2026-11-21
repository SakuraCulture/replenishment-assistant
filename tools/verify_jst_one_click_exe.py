"""Run dual-warehouse one-click acceptance from an extracted standalone package."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, payload):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def seed(case):
    import purchases
    from test_archive_orders import seed as signed_orders
    from test_jst_one_click import OneClick
    from test_lifecycle import LifecycleTests
    from jst_one_click_fixture import SKUS, WAREHOUSES
    import warehouse_catalog
    import work_repository as wr

    case.mkdir()
    sample = LifecycleTests()
    sample.root = case
    sample.work, sample.repo, sample.payload = signed_orders(case)
    sample.path = sample.work.path
    sample.heads = purchases.targets(sample.path)
    sample.h, sample.count = sample.heads[0], 0
    scenario = OneClick()
    scenario.prepare_case(sample)
    sample.save('operations', [{'补货单号': h['number'], '供货仓': h['warehouse'],
                                '商品编码': SKUS[h['warehouse']], '门店确认数量': 40}
                               for h in sample.heads])
    setup = scenario.preflight()
    assert not setup['skipped'] and [r['mode'] for r in setup['ready']] == ['allocate', 'allocate'], setup
    old = warehouse_catalog.Repository(sample.path.parent).catalogs()
    stock = dict(zip(WAREHOUSES, (17, 12)))
    orders = []
    for head in sample.heads:
        wh = head['warehouse']
        assert old[wh]['items'][SKUS[wh]]['可用数'] == 100
        orders.append(dict(warehouse=wh, number=head['number'], sku=SKUS[wh], old_stock=100,
                           old_revision=old[wh]['revision'], stock=stock[wh]))
    write(case/'synthetic.json', dict(case='jst-one-click', synthetic=True, live_platform=False,
                                     day=wr.today(), orders=orders))


def contents(folder):
    files = {}
    for path in sorted(folder.rglob('*')):
        assert not path.is_symlink() and not path.is_junction(), path
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        assert not {'.git', 'settings', 'Cookies', 'Login Data', 'test-appdata'} & set(relative.parts), relative
        assert relative.suffix.lower() not in ('.db', '.sqlite', '.sqlite3', '.log'), relative
        files[relative.as_posix()] = (path.stat().st_size, sha(path))
    return files


def pack(delivery, out):
    app = delivery/'补货助手'
    files = contents(app)
    assert {'补货助手.exe', '_internal/python312.dll', '_internal/build_info.json',
            '_internal/components/jst_catalog/integrity.manifest.json',
            '_internal/components/aoxiang/runtime/node.exe'} <= files.keys()
    pending = out/'candidate.zip'
    with zipfile.ZipFile(pending, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in files:
            archive.write(app/name, '补货助手/'+name)
    with zipfile.ZipFile(pending) as archive:
        assert len(archive.namelist()) == len(files) and archive.testzip() is None
        assert set(archive.namelist()) == {'补货助手/'+name for name in files}
        archive.extractall(out/'unpacked')
    extracted = out/'unpacked/补货助手'
    assert contents(extracted) == files
    return pending, extracted, files


def run_exe(command, case, phase, metadata):
    env = os.environ.copy()
    for key in ('QT_QPA_PLATFORM', 'QT_PLUGIN_PATH', 'PYTHONHOME', 'PYTHONPATH', 'REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(key, None)
    env.update(LOCALAPPDATA=str(case/'test-appdata'), APPDATA=str(case/'test-roaming'),
               REPLENISHMENT_DATA_DIR=str(case/'settings'), PYTHONUTF8='1', PYTHONHASHSEED='0')
    if metadata:
        env['PATH'] = os.pathsep.join((str(Path(env['WINDIR'])/'System32'), env['WINDIR']))
    started = time.monotonic()
    with (case/(phase+'-process.log')).open('xb') as log:
        proc = subprocess.Popen([*command, '--jst-one-click-smoke', str(case), phase],
                                cwd=case, env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code = proc.wait(timeout=150)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f'隔离验收进程 {proc.pid} 尚未退出，请核对原进程及日志') from None
    report_path = case/(phase+'-report.json')
    report = read(report_path) if report_path.exists() else {}
    assert code == 0 and report.get('passed') is True, (code, report.get('error'), str(report_path))
    assert report['frozen_exe'] is bool(metadata)
    assert report['synthetic'] is True and report['live_platform'] is False
    if metadata:
        assert all(report['build'][k] == metadata[k] for k in ('source_commit', 'build_id', 'source_archive_sha256'))
    return report, dict(file=str(report_path), sha256=sha(report_path), exit_code=code,
                        seconds=round(time.monotonic()-started, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--delivery', type=Path)
    parser.add_argument('--commit')
    parser.add_argument('--preserved', type=Path)
    args = parser.parse_args()
    assert bool(args.delivery) == bool(args.commit), '封装验证须指定源码提交及交付目录'
    from application_processes import discover
    active = [r['pid'] for r in discover(guarded=True) if r['main']]
    if active:
        raise RuntimeError(f'仍有补货助手实例，请安全结束后再运行验收：{active}')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    preserved = read(args.preserved) if args.preserved else {}
    assert all(sha(Path(name)) == digest for name, digest in preserved.items())
    metadata = None
    if args.delivery:
        delivery = args.delivery.resolve()
        pending, extracted, files = pack(delivery, out)
        metadata = read(extracted/'_internal/build_info.json')
        assert metadata['source_commit'] == args.commit
        command = [str(extracted/'补货助手.exe')]
    else:
        command = [sys.executable, str(ROOT/'src/app.py')]
    case = out/'actual-exe' if metadata else out/'source-app'
    seed(case)
    exercised, first = run_exe(command, case, 'exercise', metadata)
    reopened, second = run_exe(command, case, 'reopen', metadata)
    assert exercised['packaged_component_started'] and exercised['orders'] == reopened['orders'] and not reopened['calls']
    events = [json.loads(line)['event'] for line in (case/'.settings.diagnostics/application.jsonl').read_text('utf-8').splitlines()]
    assert events.count('STOPPED') == 2
    assert not {'EXCEPTION', 'UNRAISABLE', 'THREAD_EXCEPTION', 'ERROR_DIALOG_FAILED'} & set(events)
    assert all(sha(Path(name)) == digest for name, digest in preserved.items())
    result = dict(passed=True, release=metadata, synthetic=True, live_platform=False, orders=exercised['orders'],
                  exercise=first, reopen=second, old_files_preserved=preserved)
    if metadata:
        assert contents(extracted) == files
        final = delivery/f"补货助手_{metadata['version']}_{metadata['build_id']}_一键分配下单.zip"
        if final.exists():
            raise FileExistsError(final)
        zip_sha = sha(pending)
        pending.rename(final)
        assert sha(final) == zip_sha
        result.update(zip=str(final), zip_sha256=zip_sha, zip_bytes=final.stat().st_size,
                      verified_files=len(files), exe_sha256=files['补货助手.exe'][1])
    write(out/'验收报告.json', result)
    print(out/'验收报告.json')


if __name__ == '__main__':
    main()
