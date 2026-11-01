"""Collect local 0.22 rc2 evidence. Refuses incomplete validation; no publishing."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
Q=ROOT/'qa/payment_0220';OUT=ROOT/'releases/0.22.0/rc2'


def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,value):Path(p).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    OUT.mkdir(parents=True,exist_ok=False)
    evidence={'equivalence':Q/'equiv/equivalence.json','original-exports':Q/'original-exports/report.json',
              'performance':Q/'performance/report.json','exe-auto':Q/'exe-auto-rc2/report.json',
              'exe-manual':Q/'exe-manual-rc2/report.json','exe-original':Q/'exe-original-rc2/report.json'}
    for name,path in evidence.items():
        if not read(path)['passed']:raise AssertionError(str(path))
        shutil.copy2(path,OUT/(name+'.json'))
    for name,run in [('normal','20261101-210301-20179795'),('optimized','20261101-210334-4b0ce638')]:
        log=ROOT/'qa/dev/runs'/run/'tests.txt'
        if 'Ran 346 tests' not in log.read_text(encoding='utf-8') or not log.read_text(encoding='utf-8').rstrip().endswith('OK'):raise AssertionError(log)
        shutil.copy2(log,OUT/(name+'-tests.txt'))
        shutil.copy2(log.parent/'environment.json',OUT/(name+'-environment.json'))
    frozen=read(Q/'equiv/baseline.json');changed=[]
    for path,digest in {**frozen['inputs'],**frozen['protected']}.items():
        if sha(path)!=digest:changed.append(path)
    old_manifest=read(ROOT/'releases/0.21.0/rc2/files-sha256.json')
    for relative,digest in old_manifest.items():
        path=ROOT/'交付_0.21.0_rc2/补货助手'/relative
        if sha(path)!=digest:changed.append(str(path))
    old_zip=ROOT/'交付_0.21.0_rc2/补货助手_0.21.0_rc2.zip'
    if sha(old_zip)!='9654db2bfb82997b6a9e94085a12e72d0028deefc18f05a83e530cb84428f48c':changed.append(str(old_zip))
    core={name:sha(ROOT/'src'/name)==digest for name,digest in frozen['core'].items()}
    save(OUT/'protection.json',{'core':core,'changed':changed,'previous_package_files':len(old_manifest),'input_files':len(frozen['inputs']),'protected_paths':len(frozen['protected'])})
    if changed or not all(core.values()):raise AssertionError('Protection difference; inspect report')
    build=read(ROOT/'build_0.22.0_rc2/build_info.json');shutil.copy2(ROOT/'build_0.22.0_rc2/build_info.json',OUT/'build-info.json')
    shutil.copy2(ROOT/'build_0.22.0_rc2/build.log',OUT/'build.log')
    shutil.copy2(ROOT/'build_0.22.0_rc2/command.json',OUT/'build-command.json')
    package=ROOT/'交付_0.22.0_rc2/补货助手';exe=package/'补货助手.exe'
    manifest={str(p.relative_to(package)).replace('\\','/'):sha(p) for p in sorted(package.rglob('*')) if p.is_file()};save(OUT/'files-sha256.json',manifest)
    archive=package.parent/'补货助手_0.22.0_rc2.zip'
    if archive.exists():raise FileExistsError(archive)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(package.rglob('*')):
            if p.is_file():z.write(p,str(p.relative_to(package.parent)))
    source={str(p.relative_to(ROOT)):sha(p) for p in sorted((ROOT/'src').glob('*')) if p.is_file()};save(OUT/'source-hashes.json',source)
    for p in (ROOT/'src').glob('*'):
        if p.is_file() and sha(p)!=sha(ROOT/'build_0.22.0_rc2/source/src'/p.name):raise AssertionError('Built source mismatch '+str(p))
    auto=read(evidence['exe-auto']);manual=read(evidence['exe-manual']);original=read(evidence['exe-original'])
    for r in [auto,*manual['runs'],*original['runs']]:
        if r['exit_code'] or r['remaining_children']:raise AssertionError('EXE failure')
    save(OUT/'release.json',{'passed':True,'build':build,'exe_sha256':sha(exe),'zip_sha256':sha(archive),'zip_bytes':archive.stat().st_size,'actual_exe_starts':6,'tests_per_mode':346,'pushed':False,'merged':False})
    perf=read(evidence['performance']);lines=[]
    for stores in (3,10):
        pair={r['mode']:r['metrics'] for r in perf['summary'] if r['stores']==stores}
        for metric,title in [('stock_prepare','仓资料准备保存'),('allocation_preview','分配预览'),('generation_save','生成保存'),('export','聚水潭导出'),('payment_preview','付款核对预览'),('peak_tree_mib','采样峰值MiB')]:
            cells=[]
            for mode in ('old','new'):
                v=pair[mode][metric];cells.append(f"{v['median']:.4f} [{v['min']:.4f}, {v['max']:.4f}]")
            lines.append(f'|{stores*2}仓单|{title}|'+ '|'.join(cells)+'|')
    report=f'''# 0.22.0 rc2 交付与验收

已实现：四阶段生命周期、回传后可恢复自动生成、四列聚水潭导入单、两种付款来源、缺外部单号提示、逐商品数量金额与外部单号校准。库存和模型规则不变。候选包为`交付_0.22.0_rc2/补货助手/补货助手.exe`，完整ZIP同目录。保留0.21.0；rc1为中途候选，不作为最终试用版本。未合并、未推送、未执行远端下单。

## 版本及实现

基线125ad539e7116e7b46b714bb34e2c7be31d4ec90；分支feat/payment-source-workflow-0.22.0。数据／任务阶段4003634，界面及生成协调阶段5a1d532，队列决胜顺序修正及构建提交{build['source_commit']}。应用0.22.0；模型0.3.3-rules、归属0.5.1、每日存储格式2保持。新jst_tasks表保存自动任务；既有生命周期快照格式及历史原件不迁移。

回传事务→任务记录→后台单消费者→已有仓分配服务→已生成快照／占用；中断后重用已准备的操作ID。队列按回传确认时间、业务日、单号排序，与原分配决胜一致。库存未知／缺价／无效箱规等问题停为待处理；普通库存不足按原规则向下取整。付款来源不操作占用、锁或下游实际收货数。

四列文件和直接保存明细都复用付款核对提交。原13列付款保留。回拉时外部单号为空，明确未提供；补货单号仅用于关联。付款校准显示新增、移除、数量、金额、外部单号差异；已收货时保留原收货事实并提示重新核对。

## 验证结果

- 346项普通及-O测试通过。新增9项专项验证四列／直接回拉值类型一致、调序、空白0、缺价、非法单号、双来源选择、锁后付款校准、收货保留、取消原子性、提交后中断恢复、同秒队列顺序。原27项分配、仓锁及回归继续通过。
- 固定基线30组有序计算／Excel对照零差异；包括真实14244行样本、各预算、单／双仓、原计算中间字段、参数、自定义设置、串行／并发及恢复。12组原归档／签发／原件导出零差异。精确动态白名单随equivalence.json保存，没有扩展。
- 新旧执行分配的全部商品行、汇总和上游依据严格一致；聚水潭前三列的工作表顺序、值、类型、格式逐项一致，第四列为授权新增。每组3次交替串行运行，详细结果见performance.json。
- 最终rc2实际EXE共6次启动，均exit0且无遗留子进程：自动恢复生成、四列回拉付款UI；人工生成／锁后更新仓资料／付款核对／关闭重试／重开；原模型spawn、最终清单编辑、草稿恢复、签发及导出。新自动链UI心跳最大{auto['heartbeat_max']:.3f}秒，进程树采样峰值{auto['peak_tree_mib']:.2f}MiB。属于实际EXE中的Qt自动验收，不冒充人工现场试用或远端下单。
- 核心10文件哈希一致，冻结输入及日常资料保护核对无变化，旧0.21.0包{len(old_manifest)}文件和ZIP一致。完整证据见protection.json、source-hashes.json和files-sha256.json。

## 允许变化的明确范围

聚水潭导出末列订单号；付款回拉新增adapter/price/amount_basis/external_order_number/payment_source审计；直接回拉document_number从错误使用补货号改为空，数值字符串使用已有付款输入标准化格式；临时预览新增calibration；新增jst_tasks；回传后的后台生成行为；总览四类生命周期及辅助状态。原模型业务字段、数量金额、签发和已有导出没有非预期差异。旧测试只更新三列→四列及阶段名称预期，并从原业务表保护中明确单列新增jst_tasks；未扩大原业务字段忽略范围。

## 性能

同一机器Python3.12锁定依赖，各3次、新旧交替逐个运行。以下秒数为中位数[最小,最大]；内存行单位MiB。每单1个合成回传SKU、每仓2个SKU，不是真实大单吞吐。新进程包含解释器启动，未清操作系统缓存；内存50ms采样进程树RSS相加，未扣共享页。原始值全部保留。

|规模|阶段|0.21.0|0.22.0|
|---|---|---|---|
{chr(10).join(lines)}

没有全面提速结论，四列导出新增关联列有开销；自动生成主要减少等待下一步手工确认的操作。上述性能测量基于5a1d532，最终仅修正自动任务同秒排序与测试重复原件方式，测量中的手工分配／导出路径未变。没有把原测试量级外推到多年历史或大量真实订单。

## 发现及修正记录

初次UI检查发现旧首页重组会删除新增进度控件，已改为重组后挂在订单跟踪页并通过实际EXE验证。首次-O失败源于测试重新导出同名文件后ZIP时间变化，变成另一个原件哈希；测试已改为重复导入同一原件，未改变系统原件审计或幂等规则。原失败记录在qa/payment_0220及qa/dev/runs保留。同秒订单优先序差异已修正并补明确反序入队测试。

## 复跑与限制

操作说明及命令见docs/0.22.0付款来源与自动生成.md。新功能便携测试tests/test_payment_source.py；性能／EXE工具tools/payment_source_qa.py，手动链tools/jst_qa.py，原链tools/verify_preissue_exe.py。原计算对照使用tools/verify_picker_regression.py --qa qa/payment_0220/equiv，基线源码及14个冻结输入在该目录；它们不随Git发布。新输出请用全新目录，不能覆盖已有证据。

尚未验证真实聚水潭对第四列的接收、真实大规模订单性能、跨机新包现场体验。只实现本地文件、库存预留和明确确认的生命周期事实，不自动判断外部付款。旧历史缺少信息不补造。回退必须使用保留旧包及升级前备份，不使用旧程序继续写新版资料。安全升级、跨系统自动执行器、共享远端库存不在本轮。

EXE SHA256：{sha(exe)}

ZIP SHA256：{sha(archive)}
'''
    (OUT/'交付报告.md').write_text(report,encoding='utf-8')
    print(json.dumps({'release':str(OUT),'exe':str(exe),'zip':str(archive),'passed':True},ensure_ascii=False))


if __name__=='__main__':main()
