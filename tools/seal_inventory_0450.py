"""Seal 0.45.0 only after fresh business, analytics, performance and EXE evidence."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8',newline='\n')


def main():
    qa=ROOT/'qa/inventory_0450';release=ROOT/'releases/0.45.0/rc1';delivery=ROOT/'交付_0.45.0_rc1/补货助手'
    build=read(ROOT/'build_0.45.0_rc1/build_info.json');business=read(qa/'business_comparison/equivalence.json');frozen=read(qa/'business_comparison/frozen.json')
    parity=read(qa/'analytics_comparison/report.json');million=read(qa/'million-final/report.json');detail=read(qa/'details/report.json');ui=read(qa/'exe-ui/report.json')
    regression=(qa/'regression-full.txt').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<1009:raise ValueError('Missing full regression')
    for name in ('targeted-inventory','targeted-api','targeted-contract'):
        if not re.search(r'\sOK\s*$',(qa/(name+'.txt')).read_text('utf-8-sig')):raise ValueError(name)
    if not business['passed'] or business['differences'] or business['additional_ignored_fields']:raise ValueError('Business differs')
    if not parity['passed'] or parity['differences'] or parity['allowed_business_changes']:raise ValueError('Existing analytics differs')
    if parity['dynamic_exclusions']!=['report.as_of','report.history.as_of']:raise ValueError('Unexpected exclusions')
    if not million['passed'] or not detail['passed'] or len(million['runs'])!=3 or len(detail['runs'])!=3:raise ValueError('Missing performance')
    if not ui['passed'] or not ui['frozen_exe'] or ui['exit_code'] or len(ui['checks'])<26:raise ValueError('Actual EXE not accepted')
    if ui['source_commit']!=build['source_commit'] or ui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE/build differs')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    for name,h in business['core'].items():
        if h['before']!=h['after'] or sha(ROOT/'src'/name)!=h['before']:raise ValueError('Core changed '+name)
    for path,h in frozen['protected'].items():
        if sha(Path(path))!=h:raise ValueError('Protected file changed '+path)
    release.mkdir(parents=True,exist_ok=False)
    copies={'business_comparison/equivalence.json':'business-equivalence.json','business_comparison/frozen.json':'frozen.json','analytics_comparison/report.json':'analytics-parity.json',
        'million-final/report.json':'million-performance.json','details/report.json':'detail-performance.json','regression-full.txt':'regression.txt','targeted-inventory.txt':'targeted-inventory.txt',
        'targeted-api.txt':'targeted-api.txt','targeted-contract.txt':'targeted-contract.txt','exe-ui/report.json':'exe-report.json','real-inventory.json':'real-inventory.json'}
    for a,b in copies.items():shutil.copyfile(qa/a,release/b)
    shutil.copyfile(ROOT/'build_0.45.0_rc1/build_info.json',release/'build_info.json')
    for p in (qa/'exe-ui').glob('0*.png'):shutil.copyfile(p,release/p.name)
    def scrub(value):
        if isinstance(value,dict):return {k:'[isolated context redacted]' if k in ('context_token','instance_id') else scrub(v) for k,v in value.items()}
        if isinstance(value,list):return [scrub(v) for v in value]
        return value
    save(release/'mcp-evidence.json',scrub(read(qa/'exe-ui/mcp-evidence.json')))
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.45.0_rc1_货盘结构分析.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        if len(z.namelist())!=len(files):raise ValueError('ZIP file count')
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP content '+rel)
    manifest={'version':'0.45.0','build_id':'rc1','baseline':business['baseline_commit'],'source_commit':build['source_commit'],
        'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files,
        'new_source_hashes':{p.name:sha(p) for p in (ROOT/'src').glob('*inventory*.py')}}
    save(release/'manifest.json',manifest)
    lines=[]
    for title,r in [('百万行汇总（10店×10轮×1万SKU）',million),('9千行详细导出（3店×3轮×1千SKU）',detail)]:
        for key,label in [('query','查询'),('repeat_query','复查'),('detail','一店一轮明细'),('summary_export','汇总导出'),('detail_export','全查询商品导出')]:
            if key not in r['summary']:continue
            p=r['summary'][key];lines.append(f"| {title} / {label} | {p['median']:.3f} | {p['min']:.3f}～{p['max']:.3f} | {p['peak_mib']:.1f} |")
    table='\n'.join(lines)
    report=f'''# 0.45.0 货盘结构分析交付

分支feat/inventory-structure-0.45.0；本轮只本地提交、验证、构建及标签，不推送、不合并。保留0.44.0 rc2。固定基线{business['baseline_commit']}（0.44.0代码加已批准方案文档）。构建源码{build['source_commit']}；证据提交和标签在构建之后，不混称构建提交。

## 已完成

独立货盘服务、历史门店逐轮看板、现货/在途/总持有、大盘/中心仓/地采/来源待核、中心仓状态/必采及交叉指标；同店上一有效业务日、数量/价格/范围变化与分类迁移；当前计算汇总预览；独立汇总或详细Excel；三个AI工具、分页、明确快照及失效拦截。库存取父级完整结果一次，不因双仓分单重复计价。不使用原漏斗的禁用/停采排除来筛掉持有存量。

源码索引和操作说明：docs/0.45.0货盘结构分析.md。新服务inventory_structure、导出inventory_structure_exports、页面inventory_structure_ui、AI适配automation_inventory。旧计算、预算、箱规、签发、库存分配和存储格式均未修改，模型/归属保持0.3.4-rules/0.5.1。

## 验证

- 主体全量{m[1]}项回归通过，用时{m[2]}秒。随后增加重复编码保留及释放长读事务的核验，最终新指标19项、AI3项、协议回归定向结果另附；未改变旧业务预期，工具数量从23到26是明确新增三个接口。
- 基线逐业务记录及Excel对照{len(business['cases'])}份、差异0，含14,244行真实冻结输入；八个核心文件哈希相同，原文件及旧包哈希未变。使用既有精确动态白名单，不扩大业务忽略范围。
- 原分析报告逐字段、类型、顺序、快照ID一致，Excel共{parity['cells_compared']}单元格及全部工作表/行列/值/类型/格式/样式/批注一致。仅排除report.as_of、report.history.as_of查询时刻；允许业务差异0。
- 实际新EXE及包内stdio MCP通过{len(ui['checks'])}项：查询、日期/视角、门店轮次下钻、禁用筛选、中文公式、详细Excel、工具发现、完成状态、分页、原生/AI {ui['compared_cells']}个单元格一致、取消/重试、删除失效与未选店保留、关闭安全取消。查询时间单元格为两个独立查询的精确动态排除。
- 实际EXE小型合成查询与呈现{ui['query_seconds']:.3f}秒；界面心跳{ui['heartbeat_ticks']}次，最大间隔{ui['max_heartbeat_gap_seconds']:.3f}秒。未将源码窗口当EXE结果。测试MCP连接只用隔离环境，不是重新让Codex/WorkBuddy自然语言解释的验收。
- 真实冻结计算结果的只读货盘核验见real-inventory.json；未读取日常用户数据库或访问平台。当前普通地采仍缺可靠单价，保留未计价而非猜金额。

## 性能（秒；主进程采样峰值MiB）

| 样本/阶段 | 中位数 | 最小～最大 | 峰值内存 |
| --- | ---: | ---: | ---: |
{table}

同机、同Python/依赖、串行每组三次，OS文件缓存温热；查询包含SQLite读取、解码、两轮比较及聚合，导出包含核验与保存，内存每30ms采样工作集、含解释器，可能遗漏短峰，无计算子进程。新功能无旧同功能基线，不宣称提速。百万行测汇总和单轮明细；全量商品Excel用9千行三次，未实测百万行详细Excel。百万行查询期间只保留两轮商品，返回小型摘要；不是全量商品常驻界面。解码/聚合前释放读事务，前后核验固定修订，测试中并发写入不被长分析阻塞且过期结果拒绝发布。

旧分析新旧AB/BA/AB三轮原始耗时在analytics-parity.json，无业务功能变化，也不把噪声当提速。

## 限制和未混入事项

地采实际售价列与基础单位契约未确认，本版没有分析价格补充导入入口，缺价地采只能看持有和未计价款数；不能宣称全店完整货值。库存范围只证明当次输入，未提供数据更新时间不冒充实时。旧历史不补写价格或标签。首期趋势只绘所选店总盘，不含来源堆叠；另一仓原表查看及独立筛选明细导出暂未新增；组合筛选可查看，查询范围完整明细可导出。签发前预览仅汇总，正式历史支持下钻导出。删除仍走既有父单边界，本次不改变删除动作。

回归曾有一次无详细输出的长等待，已停止该隔离测试进程并使用带栈诊断的文件入口完整重跑；stdin诊断无法被Windows spawn重入，改用文件入口，没有改计算代码迎合。全量日志包含既有故障注入及Qt关闭时已销毁标签的信号异常；原browser_status_ui源码未改，不把这些日志称为本次修复。新功能独立EXE验收结果见exe-report.json。

## 交付

EXE：{delivery}/补货助手.exe

ZIP：{target}

EXE SHA256：{manifest['exe_sha256']}

ZIP SHA256：{manifest['zip_sha256']}

逐一校验ZIP内{len(files)}个文件；manifest记录固定源码及源文件哈希。未验证全新电脑重建、逐字节可复现构建或本轮真实平台。复跑命令见操作说明；完整QA在qa/inventory_0450，提交的精简证据在本目录。MCP证据仅脱敏本轮隔离context_token/instance_id，原始工具输出留在QA。
'''
    (release/'交付报告.md').write_text(report,'utf-8',newline='\n');print(json.dumps({'zip':str(target),'files':len(files),'sha256':manifest['zip_sha256']},ensure_ascii=False))


if __name__=='__main__':main()
