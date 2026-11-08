"""Seal the isolated 0.49.1 rc1 acceptance; never overwrite evidence or a ZIP."""
import hashlib
import html
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from aoxiang_bundle import verify

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'qa/payment_auto_0491'


def read(path):
    return json.loads(path.read_text('utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def passed_tests(path, expected):
    raw = path.read_text('utf-8-sig')
    match = re.search(r'Ran (\d+) tests in ([\d.]+)s', raw)
    assert match and int(match[1]) == expected and re.search(r'\sOK\s*$', raw), path
    ids = re.findall(r'^(test\w+ \([^\n]+\)) \.\.\.', raw, re.M)
    assert len(ids) == len(set(ids)) == expected, path
    return {'tests': expected, 'unique_test_ids': len(set(ids)), 'seconds': float(match[2])}


def main():
    build = read(ROOT / 'build_0.49.1_rc1/build_info.json')
    commit = build['source_commit']
    assert build['version'] == '0.49.1'
    assert build['source_mode'] == 'immutable git archive; no working-tree overlay'
    assert not subprocess.check_output(['git', 'diff', commit, '--', 'src', 'components', 'tests',
                                       'tools/verify_payment_matching.py', 'tools/verify_payment_auto_samples.py'], cwd=ROOT)
    regression = passed_tests(QA / 'regression-final.log', 1305)
    focused = passed_tests(QA / 'matching4.log', 35)
    exe = read(QA / 'exe1/acceptance.json')
    real = read(QA / 'exe-samples1/acceptance.json')
    source = read(QA / 'source2/report.json')
    samples = read(QA / 'samples-final/report.json')
    for report, count in ((exe, 23), (real, 31)):
        assert report['passed'] and report['process_exit'] == 0 and len(report['checks']) == count
        assert report['source_commit'] == commit and report['version'] == '0.49.1' and not report['live_platform']
        assert report['model_version'] == '0.3.4-rules' and report['storage_format_version'] == 2
    assert real['frozen_order_copy_only'] and not real['live_database_written']
    assert source['passed'] and len(source['checks']) == 23
    assert samples['passed'] and len(samples['checks']) == 9 and all(c['passed'] for c in samples['checks'])
    assert samples['source_files_unchanged'] and samples['snapshot_unchanged']
    assert not samples['source_database_written'] and not samples['platform_called']
    assert not samples['company_selection_required'] and not samples['manual_order_selection_required_for_cases_01_to_05']
    sample_root = ROOT.parent / '付款认单案例_20261108_02/付款文件'
    assert all(sha(sample_root / p) == h for p, h in samples['source_files_sha256'].items())

    previous = read(ROOT / 'releases/0.49.0/rc1/frozen.json')
    old = read(ROOT / 'releases/0.49.0/rc1/validation.json')
    protected = dict(previous['preserved_deliveries'])
    protected['交付_0.49.0_rc1/补货助手/补货助手.exe'] = old['exe_sha256']
    protected[old['zip']] = old['zip_sha256']
    assert all(sha(ROOT / p) == h for p, h in previous['core'].items())
    assert all(sha(ROOT / p) == h for p, h in protected.items())
    assert not subprocess.check_output(['git', 'diff', 'v0.49.0-rc1', commit, '--', 'components'], cwd=ROOT)
    delivery = ROOT / '交付_0.49.1_rc1/补货助手'
    executable_hash = sha(delivery / '补货助手.exe')
    assert exe['exe_sha256'] == real['exe_sha256'] == executable_hash
    component = verify(delivery / '_internal/components/aoxiang')
    for path in (ROOT / 'build_0.49.1_rc1/source/components/aoxiang/src').glob('*.mjs'):
        assert sha(path) == sha(delivery / '_internal/components/aoxiang/src' / path.name)

    release = ROOT / 'releases/0.49.1/rc1'
    target = delivery.parent / '补货助手_0.49.1_rc1_付款自动认单.zip'
    manual = delivery / '使用手册/0.49.1付款自动认单'
    assert not release.exists() and not target.exists() and not manual.exists()
    release.mkdir(parents=True, exist_ok=False)
    sources = {
        'build_info.json': ROOT / 'build_0.49.1_rc1/build_info.json',
        'regression.log': QA / 'regression-final.log',
        'payment-tests.log': QA / 'matching4.log',
        'exe-report.json': QA / 'exe1/acceptance.json',
        'actual-sample-exe-report.json': QA / 'exe-samples1/acceptance.json',
        'source-report.json': QA / 'source2/report.json',
        'actual-samples.json': QA / 'samples-final/report.json',
        'verify_packaged_samples.py': QA / 'run_sample_exe.py',
        '付款自动识别.png': QA / 'exe-samples1/真实案例_03.png',
        '错误单号提示.png': QA / 'exe-samples1/真实案例_03错误签发号.png',
        '缺商品提示.png': QA / 'exe-samples1/真实案例_06.png',
    }
    for name, path in sources.items():
        shutil.copyfile(path, release / name)
    retained = release / 'retained-development'
    retained.mkdir()
    for name in ('matching2.log', 'regression.log', 'samples1.log', 'seal.log'):
        shutil.copyfile(QA / name, retained / name)
    write(retained / 'README.json', {
        'not_final_acceptance': True,
        'matching2.log': 'New verified-company warehouse guard exposed an old fixture binding company 100 to SD while its first generation was HB. Fixture identity was corrected; production guard was retained. Final 35 tests pass.',
        'regression.log': 'Initial 1303-test run had one subprocess import error because the launcher omitted PYTHONPATH for src/tools. No runtime or test code changed for this error; final discovery includes two additional cases and all 1305 pass.',
        'samples1.log': 'Read-only scenario verification first selected an Excel temporary ~$ lock file and failed before case processing. Verifier now selects exactly cases 01 through 07. The original workbooks were not modified.',
        'seal.log': 'First sealing preflight incorrectly expected model_version in build_info instead of the EXE version report. Stopped before creating release evidence or ZIP; sealer now checks the actual EXE report field. Application code and executable unchanged.',
        'other_development_evidence': 'Earlier source smoke and sample runs remain in qa/payment_auto_0491. Final release uses source2, samples-final, exe1 and exe-samples1.',
    })

    manual.mkdir(parents=True, exist_ok=False)
    document = (ROOT / 'build_0.49.1_rc1/source/docs/0.49.1付款自动认单.md').read_text('utf-8')
    document = document.replace('全量回归、实际EXE及案例证据见[交付报告](../releases/0.49.1/rc1/交付报告.md)。',
                                '全量回归1305项通过；付款专项35项通过；实际EXE两组23项和31项验收通过。')
    document += '\n## 实际界面\n\n未选择来源企业或目标订单，别称付款表已自动关联：\n\n![自动认单](付款自动识别.png)\n\n错误签发号明确显示冲突：\n\n![错误单号](错误单号提示.png)\n'
    with (manual / '使用说明.md').open('x', encoding='utf-8') as stream:
        stream.write(document)
    for name in ('付款自动识别.png', '错误单号提示.png'):
        shutil.copyfile(release / name, manual / name)
    blocks = []
    for line in document.splitlines():
        if line.startswith('!['):
            match = re.fullmatch(r'!\[(.*?)\]\((.*?)\)', line)
            blocks.append('<figure><img src="' + html.escape(match[2]) + '"><figcaption>' + html.escape(match[1]) + '</figcaption></figure>')
        elif line.startswith('# '):
            blocks.append('<h1>' + html.escape(line[2:]) + '</h1>')
        elif line.startswith('## '):
            blocks.append('<h2>' + html.escape(line[3:]) + '</h2>')
        elif line:
            blocks.append('<p>' + html.escape(line) + '</p>')
    with (manual / '打开使用说明.html').open('x', encoding='utf-8') as stream:
        stream.write('<!doctype html><meta charset="utf-8"><title>付款自动认单</title><style>body{max-width:1100px;margin:40px auto;padding:0 28px;font:18px/1.8 "Microsoft YaHei",sans-serif;color:#193e43}h1,h2{color:#176e65}h2{margin-top:36px}img{max-width:100%;border:1px solid #d0dfe0}figure{margin:28px 0}p{white-space:pre-wrap}</style>' + ''.join(blocks))

    hashes = {p.relative_to(delivery).as_posix(): sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:
            archive.write(delivery / name, '补货助手/' + name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist()) == len(hashes)
        for name, digest in hashes.items():
            with archive.open('补货助手/' + name) as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == digest
    write(release / 'manifest.json', {'source_commit': commit, 'files': hashes})
    write(release / 'frozen.json', {'core': previous['core'], 'preserved_deliveries': protected})
    report = {
        'passed': True, 'source_commit': commit, 'python_regression': regression, 'payment_tests': focused,
        'exe_synthetic_checks': len(exe['checks']), 'exe_actual_sample_checks': len(real['checks']),
        'actual_sample_checks': len(samples['checks']), 'source_files_unchanged': True,
        'live_database_written': False, 'live_platform': False, 'performance_measured': False,
        'core_unchanged': len(previous['core']), 'preserved_delivery_files': len(protected),
        'components_unchanged_from': 'v0.49.0-rc1', 'component_files_verified': len(component['files']),
        'node_tests_rerun': False, 'exe_sha256': executable_hash,
        'zip': target.relative_to(ROOT).as_posix(), 'zip_sha256': sha(target),
        'zip_bytes': target.stat().st_size, 'files': len(hashes),
    }
    write(release / 'validation.json', report)
    delivery_report = f'''# 0.49.1 rc1 付款自动认单交付

付款文件默认自动寻找对应的签发订单及供货仓。完整明细唯一时，不再要求先选择聚水潭企业、确认门店别称或手动选择订单。实际付款内容仍由用户核对后确认保存。

## 原因与修复

此前有两处已确认问题：自动识别即使找到唯一完整匹配，也被“先选择来源企业／确认别称”挡住；聚水潭付款表适配器没有读取用户追加的“签发单号／补货单号”。本轮修正这两条路径，不改模板原13列及金额数量事实。

- 无单号时，按有效分配单的全部商品编码、逐商品库存单位数量、金额核对全部相关日期；唯一完整匹配自动关联。同店两仓、店名细微不同、新内部号均可识别。
- 有明确签发号时读取并核对当前签发身份，保留实际付款数量／金额差异。错误单号若与完整明细或原关联矛盾，明确报冲突，不忽略错误号后自动改绑。
- 原内部号的金额更正，在原订单及分配仍有效、整单商品数量一致且唯一时自动认回原单，显示金额差异。重复导入相同付款不新增版本。
- 默认模式不猜来源企业，不改企业或门店主资料；自动认单对应单独记在本机。付款和对应同事务保存，取消、并发复核、失败整体回滚及旧身份不可改绑保护保持。
- 界面显示已对应／未对应数量，下方固定展示完整识别原因；高级企业选项默认收起。

## 你提供的案例

| 案例 | 最终结果 |
| --- | --- |
| 01 正常山东仓、02 同店河北仓 | 不选企业或订单，自动认到各自仓单 |
| 03 原始别称、04 同别称新内部号 | 不维护别称，按整单明细自动识别 |
| 05 原内部号增加一分钱 | 自动认回原单，显示 +0.01 元差异 |
| 06 缺一款、07 错商品编码 | 不误称完整匹配，显示缺少／多出商品及编码 |
| 用户后来修改的03，追加东阿店签发号 | 明确报告与恒大御景半岛明细／原关联冲突，不静默改号 |

03正向案例使用此前交付ZIP中的未填号原件；用户修改版另做负向验收，原文件未覆盖。全部使用独立冻结订单副本，不代表正在使用的资料库仍包含这些订单；若现场原订单已删除，不能自动恢复或绑定同号新订单。

## 验证

- 全量Python回归 **{regression['tests']}项** 一次完整通过（{regression['seconds']:.3f}秒，测试ID无重复），[原始日志](regression.log)。付款专项 **35项** 通过（包含在全量中），[专项日志](payment-tests.log)。
- 同一实际EXE：合成业务验收 **23项**、用户实际文件与冻结订单验收 **31项** 全通过，[合成验收](exe-report.json)、[实际案例EXE验收](actual-sample-exe-report.json)。两组均确认固定构建来源及EXE哈希。
- 实际案例服务验证 **9项** 通过，[案例结果及原件哈希](actual-samples.json)。7份原文件及原冻结库保持不变。正常案例在隔离库保存，缺行／错码／错误单号未保存为付款。
- {len(previous['core'])}项计算、预算默认值、归属、库存分配、缺货率、采集与删除核心文件哈希未变；{len(protected)}个旧交付EXE／ZIP哈希未变，含0.49.0。
- 浏览器组件源码与0.49.0一致，包内{len(component['files'])}个翱象组件文件通过清单校验；本轮没有重跑Node专项，不复述旧结果为新验收。
- 开发期企业仓测试夹具不一致、首轮子进程缺PYTHONPATH及Excel临时锁文件误入扫描的失败保存在[retained-development](retained-development/README.json)。最终修正测试启动环境与夹具，没有放宽生产校验。首轮1303项中1项导入错误不能称为首轮全通过。

## 使用与限制

关闭旧版后运行新完整目录中的EXE，或先完整解压ZIP；不要只复制EXE。进入“付款核对 → 导入门店付款单”，读取文件后正常案例自动带出订单，点击“核对并准备保存”检查付款事实，再确认保存。无需先选择企业、维护别称或手填签发号。

错误单号、缺行、错码或跨日多张完整相同订单仍可能无法唯一识别；会给出原因，不把部分相似或最近一单当正确订单。自动识别不代表已经实际付款，也不自动上传采购。未调用真实平台，未修改正在使用的订单库；未测低配置电脑、大历史库全量识别耗时，本轮不宣称性能提升。既有browser_status_ui的QLabel销毁后回调问题不在本轮修复范围。

## 固定交付

- 分支：`fix/payment-auto-recognition-0.49.1`；固定源码提交：`{commit}`。
- 应用0.49.1，模型0.3.4-rules，归属0.5.2，参数／作业／存储格式3／1／2。构建为不可变Git归档，没有工作区覆盖；详见[build_info.json](build_info.json)。
- 本地标签：`v0.49.1-rc1`，封存后创建。验收封存脚本和本报告是后续证据提交，不混称EXE源码。
- 程序：`交付_0.49.1_rc1/补货助手/补货助手.exe`。
- ZIP：`{report['zip']}`，{report['zip_bytes']/1048576:.1f} MiB。
- EXE SHA256：`{executable_hash}`。
- ZIP SHA256：`{report['zip_sha256']}`。
- 完整文件及压缩包逐成员哈希已核验：[manifest.json](manifest.json)、[validation.json](validation.json)。图文说明位于包内“使用手册/0.49.1付款自动认单/打开使用说明.html”。
- 保留全部旧交付；未推送、未合并。
'''
    with (release / '交付报告.md').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(delivery_report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
