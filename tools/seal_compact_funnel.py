"""Seal the tested 0.40.4 candidate without replacing old releases."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    qa=ROOT/'qa/compact_funnel_0404';release=ROOT/'releases/0.40.4/rc1';delivery=ROOT/'交付_0.40.4_rc1/补货助手'
    build=read(ROOT/'build_0.40.4_rc1/build_info.json');comparison=read(qa/'comparison/report.json');exe=read(qa/'exe/report.json')
    tests=(qa/'all-tests.txt').read_text('utf-8-sig');matched=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',tests)
    if not matched or int(matched[1])!=897:raise ValueError('Regression failed')
    if not comparison['passed'] or comparison['differences'] or not exe['passed'] or not exe['frozen_exe'] or exe['process_exit']:raise ValueError('Acceptance failed')
    if build['version']!='0.40.4' or exe['source_commit']!=build['source_commit'] or exe['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('Build mismatch')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    old=read(ROOT/'releases/0.40.3/rc1/封存信息.json')
    for file,key in [(ROOT/'交付_0.40.3_rc1/补货助手/补货助手.exe','exe_sha256'),(ROOT/'交付_0.40.3_rc1/补货助手_0.40.3_rc1_签发弹窗与轮询修复.zip','zip_sha256')]:
        if sha(file)!=old[key]:raise ValueError('Old package changed')
    release.mkdir(parents=True,exist_ok=False)
    for source,target in [('all-tests.txt','regression.txt'),('compact-tests-final.txt','compact-tests.txt'),('analytics-tests.txt','analytics-tests.txt'),('comparison/report.json','comparison.json'),('exe/report.json','exe-report.json')]:shutil.copy2(qa/source,release/target)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.40.4_rc1_经营分析精简.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if len(z.namelist())!=len(files):raise ValueError('ZIP count mismatch')
        for name,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=h:raise ValueError('ZIP hash mismatch')
    manifest={'build':build,'files':files,'file_count':len(files),'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'old_0403_unchanged':True}
    (release/'封存信息.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),'utf-8')
    text=f'''# 0.40.4 rc1 经营分析精简导出

- 分支：`feat/lean-funnel-export-0.40.4`，未推送、未合并。
- 基线：`{comparison['baseline']}`（0.40.3）；构建源码：`{build['source_commit']}`。固定 Git 归档构建；后续证据提交不是构建提交。
- 程序：`{delivery/'补货助手.exe'}`。
- ZIP：`{target}`，{len(files)}文件，已逐文件核验。
- EXE SHA256：`{manifest['exe_sha256']}`。
- ZIP SHA256：`{manifest['zip_sha256']}`。

## 实现

常规导出只留37列：7个基础字段、五阶段×3指标、4组相邻比值和1组首尾比值×3指标。商品编码数量是正数量去重SKU款数；商品数按用户确认相加库存单位数量；货值沿用签发价。未知不当0，分母0留空，必采/非必采/属性待核与原排除规则保留。

门店导出3张表（门店、分仓、订单）；订单导出1张表；不再附带旧监控大盘、周月环比、资料审计、商品证据、多轮复盘和说明页。商品明细仅在原独立选项明确请求时导出。定义放在表头批注。AI和原生共用相同写出实现；AI能力描述已同步，未改变查询原始事实。

业务开始/结束是该行签发业务日期范围，非生命周期完成时刻。款数按观察粒度去重，跨仓/跨日期的相同SKU在门店汇总中不重复计数，因此不能直接把分仓款数相加。比值使用同一有效商品集合，覆盖不同的阶段总量不能直接相除。详见 `docs/0.40.4经营分析精简导出.md`。

## 验证

- {matched[1]}项全量回归通过，{matched[2]}秒；新增5项37列专项测试，23项分析/AI/UI相关测试通过。
- 固定基线导出与新导出保留字段逐项比较：{comparison['shared_cells_compared']}个单元格，差异0；冻结报告和隔离数据库不变。新数量、SKU/数量比值及首尾比值用独立显式预期验证。
- 排除、分组、跨单去重、混合单位、0/未知、缺价、分母0、超100%、筛选订单范围和编码文本/数值/百分比格式均有测试。缺价但数量明确0时仍保留原货值0规则，首次新增测试对此预期错误，纠正为原 amount 合同；未修改业务实现。
- 原商品明细回归改为明确请求 detail，保留全部字段值和类型的既有断言；因用户授权取消常规自动附带明细，而不是放宽结果校验。
- AI接口与手工导出Excel的工作表、单元格值、类型及格式一致；本轮没有重新连接外部Codex/WorkBuddy客户端。
- 实际EXE（frozen=true）用12店/6,000条合成记录完成查询、下钻、导出、停止重试、既有试算采用/撤回；导出确认为3张表、每张37列，无额外页。具体见exe-report.json。
- 模型、预算、一箱例外、引擎、默认配置、签发导出及分析事实服务源码哈希与0.40.3一致。旧0.40.3 EXE/ZIP校验不变。没有改变或导出用户真实数据。

## 证据与限制

`tools/verify_compact_funnel.py` 支持固定基线与新EXE验收，`tests/test_funnel_compact.py` 自建合成记录；完整本地证据在 `qa/compact_funnel_0404`。合成示例：`qa/compact_funnel_0404/comparison/经营分析_37列_合成示例.xlsx`。封存命令 `tools/seal_compact_funnel.py` 只接受通过证据，不覆盖旧包。

本轮是导出精简，没有改软件看板及历史数据事实。没有进行真实门店重新取数、采购、实收写入或新电脑验收。全量回归中的既有QLabel生命周期告警仍保留，不据此宣称全软件没有此类问题；本次导出数值和EXE验收通过。不宣称真实大库性能提升。
'''
    (release/'交付报告.md').write_text(text,'utf-8');print(json.dumps({'zip':str(target),'exe_sha256':manifest['exe_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
