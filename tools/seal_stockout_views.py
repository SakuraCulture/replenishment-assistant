"""Seal 0.47.7 custom-view acceptance from immutable source; keep all previous deliveries."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/stockout_views_0477'
BASELINE='f8b7f81a9a27df7e095710a9b73d65ff932709e2'


def read(path):return json.loads(path.read_text('utf-8-sig'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def freeze():
    target=QA/'frozen.json'
    if target.exists():raise FileExistsError(target)
    old=read(ROOT/'releases/0.47.6/rc1/frozen.json');manifest=read(ROOT/'releases/0.47.6/rc1/manifest.json')
    protected=dict(old['protected'])
    protected[str(ROOT/'交付_0.47.6_rc1/补货助手/补货助手.exe')]=manifest['exe_sha256']
    protected[str(ROOT/'交付_0.47.6_rc1/补货助手_0.47.6_rc1_负库存计入缺货率.zip')]=manifest['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in old['core'].items())
    assert all(sha(Path(p))==h for p,h in protected.items())
    write(target,{'baseline':BASELINE,'core':old['core'],'protected':protected,
                 'scope':'Independent stockout views, filters and streaming Excel only. Metric, warehouse routing and business data unchanged.'})


def report(release,build,manifest,frozen,regression,focused,source_ui,exe,real):
    text=f'''# 0.47.7 rc1：门店缺货率多维切面、筛选与导出

已实现用户确认的结构：每套自定义维度组合一张工作表，每行是一组完整维度与指标；同次可选择多套组合。支持12维自由组合、保存32套方案，按多个维度搜索、多选和跨页保留筛选。地采商品的供货仓统一显示“地采”。

## 交付与使用

- 应用：`交付_0.47.7_rc1/补货助手/补货助手.exe`，保留完整目录运行。
- 压缩包：`交付_0.47.7_rc1/补货助手_0.47.7_rc1_缺货率多维切面与导出.zip`。
- 入口：“门店缺货率 → 切面分析 → 管理 / 自定义组合”。完成后点“导出本视角Excel”，勾选所需组合。
- 筛选入口：“多维筛选…”，同维度多选为或，跨维度为且；门店、商品支持编码与名称搜索，精确编码参与筛选。
- 旧日报可直接使用新切面及地采显示，无需为本版重新采集或重算。此前负库存旧日报需要重算时，保留原提示。
- [完整操作说明](../../../docs/0.47.7缺货率多维切面与导出.md)。旧交付全部保留。

## 改动与一致性

原界面只能交叉两维，后台最多三维，导出当前切面按200组分页反复汇总。现在开放全部12维，页面仍按200组分批显示；每个导出切面使用一次分组查询，逐行写完全部分组。每张数据表含Excel筛选、冻结表头和数值百分比，原始字符串按文本保存。

组合保存在分析专用`views.json`，按需读取；并发保存有版本检查。多维筛选候选按页查询，每维最多1000个选项。旧地采“不适用”在读取时统一显示，不改历史快照。新的采集快照采用“地采”文字，不改变商品归属规则。

应用0.47.7；模型0.3.4-rules、归属0.5.2、参数3、作业1、存储2、分析库结构1和统计口径store-stockout-2均不变。月销大于0为基数，库存≤0（含负数）计缺货；停采/禁采和实际供货仓禁用默认同时剔除分子分母；淘汰、考核品及完整性待核规则保持。

## 验证

| 验证 | 实际结果 | 证据 |
| --- | --- | --- |
| 全量Python回归 | {regression[1]}项通过，{regression[2]}秒 | [日志](regression.txt) |
| 缺货率专项 | {focused[1]}项通过，{focused[2]}秒 | [日志](focused-tests.txt) |
| 源码实际界面 | {len(source_ui['checks'])}项通过 | [记录](source-ui.json) |
| 固定源码实际EXE | {len(exe['checks'])}项通过，退出码0 | [记录](analysis-exe.json) |
| 真实资料隔离副本只读校验 | {real['raw_rows']}条记录，各组与原始数值独立计算零差异 | [记录](real-sample.json) |
| 原核心与旧交付 | {len(frozen['core'])}项核心、{len(frozen['protected'])}个旧EXE/ZIP哈希一致 | [冻结清单](frozen.json) |
| 新目录与ZIP | {len(manifest['files'])}个文件逐项哈希一致 | [文件清单](manifest.json) |

实际EXE走真实界面按钮，覆盖组合编辑保存、六维同表、编码名称、多个维度勾选、只看已选、两套组合导出、Excel筛选、关闭查询安全取消，以及原有重采、重算、日志删除、数据删除和离开板块释放。平台协议使用明确标记的离线合成输入；未调用真实平台。原补货库逐表签名和原参数文件保持不变。

真实隔离样本共5店、1个观测日，默认视角仍为缺货 {real['total']['zero']} / 有销 {real['total']['base']} = {real['total']['rate']:.2%}。三套组合分别为 {', '.join(str(c['groups']) for c in real['cuts'])} 组，全部导出后核对行数与合计一致。该次导出 {real['export_seconds']} 秒、文件 {real['export_bytes']/1024**2:.2f} MiB；这是本机单次功能核验记录，不是新旧性能对照或低配业务机承诺。复用已有隔离副本只读验证，没有再复制大数据库；来源数据库、原件及分析设置哈希均未改变。实际商品明细工作簿仅保存在本地QA，不入Git。

## 提交与边界

- 基线：`{BASELINE}`。
- 分支：`feat/stockout-custom-views-0.47.7`。
- 固定构建源码：`{build['source_commit']}`。
- 本地验收标签：`v0.47.7-rc1`，指向后续发行证据提交；与源码提交区分。
- EXE SHA256：`{manifest['exe_sha256']}`。
- ZIP SHA256：`{manifest['zip_sha256']}`。
- 本轮未推送、未合并；未停止用户正在运行的旧程序，未修改日常业务数据库。

尚未在低配置业务电脑或长期连续多日规模上实测；组合越细，行数和导出时间越大。超过100万数据行会明确续表，不截断。分页、取消及临时文件清理继续有效，但不承诺整机内存或物理磁盘占用上限。本版没有重新测量订单删除性能，也没有修复此前记录的QLabel销毁回调警告，不能视为已解决。
'''
    (release/'交付报告.md').write_text(text,'utf-8',newline='\n')


def seal(build_id):
    build=read(ROOT/f'build_0.47.7_{build_id}/build_info.json');delivery=ROOT/f'交付_0.47.7_{build_id}/补货助手'
    assert build['version']=='0.47.7'
    assert not subprocess.check_output(['git','diff',build['source_commit'],'--','src','tests','tools','components','prepare_delivery.py'],cwd=ROOT)
    assert not subprocess.check_output(['git','diff',BASELINE,'--','components'],cwd=ROOT)
    frozen=read(QA/'frozen.json')
    assert all(sha(ROOT/p)==h for p,h in frozen['core'].items())
    assert all(sha(Path(p))==h for p,h in frozen['protected'].items())
    regression=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',(QA/'regression-final.txt').read_text('utf-8-sig'))
    focused=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',(QA/'focused-final-02.txt').read_text('utf-8-sig'))
    assert regression and int(regression[1])>=1206
    assert focused and int(focused[1])>=95
    source_ui=read(QA/'source-ui-final/report.json');exe=read(QA/('exe-'+build_id)/'report.json')
    for value in (source_ui,exe):
        assert value['passed'] and value['version']=='0.47.7' and value['process_exit']==0 and len(value['checks'])>=62
    assert exe['frozen_exe'] and exe['source_commit']==build['source_commit'] and exe['exe_sha256']==sha(delivery/'补货助手.exe')
    real=read(QA/'real-views-01/report.json')
    assert real['passed'] and real['source_unchanged'] and real['originals_unchanged'] and real['settings_unchanged']
    assert all(v['differences']==0 for v in real['cuts'])
    release=ROOT/f'releases/0.47.7/{build_id}';release.mkdir(parents=True,exist_ok=False)
    for name,source in {'frozen.json':QA/'frozen.json','regression.txt':QA/'regression-final.txt',
                        'focused-tests.txt':QA/'focused-final-02.txt','source-ui.json':QA/'source-ui-final/report.json',
                        'analysis-exe.json':QA/('exe-'+build_id)/'report.json','real-sample.json':QA/'real-views-01/report.json',
                        'build_info.json':ROOT/f'build_0.47.7_{build_id}/build_info.json'}.items():
        (release/name).write_text(source.read_text('utf-8-sig').rstrip('\n')+'\n','utf-8',newline='\n')
    for path in (QA/('exe-'+build_id)).glob('*.png'):shutil.copyfile(path,release/path.name)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/f'补货助手_0.47.7_{build_id}_缺货率多维切面与导出.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist())==len(hashes)
        for name,digest in hashes.items():assert hashlib.sha256(archive.read('补货助手/'+name)).hexdigest()==digest
    manifest={'version':'0.47.7','source_commit':build['source_commit'],'build_id':build_id,
          'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':hashes,
          'regression_count':int(regression[1]),'focused_count':int(focused[1]),'exe_check_count':len(exe['checks'])}
    write(release/'manifest.json',manifest)
    report(release,build,manifest,frozen,regression,focused,source_ui,exe,real)
    print(json.dumps({'release':str(release),'zip':str(target)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['freeze','seal']);parser.add_argument('--build-id',default='rc1')
    args=parser.parse_args();freeze() if args.action=='freeze' else seal(args.build_id)
