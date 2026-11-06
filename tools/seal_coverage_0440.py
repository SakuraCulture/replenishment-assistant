"""Seal the independently built 0.44.0 candidate and fresh evidence only."""
import hashlib, json, re, shutil, subprocess, zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8',newline='\n')


def main():
    qa=ROOT/'qa/coverage_0440';release=ROOT/'releases/0.44.0/rc2'
    delivery=ROOT/'交付_0.44.0_rc2/补货助手'
    build=read(ROOT/'build_0.44.0_rc2/build_info.json')
    business=read(qa/'business_comparison/equivalence.json');frozen=read(qa/'business_comparison/frozen.json')
    analytics=read(qa/'analytics_comparison_2/report.json');afrozen=read(qa/'analytics_comparison_2/frozen.json')
    gui=read(qa/'exe-ui-rc2/report.json')
    regression=(qa/'regression.txt').read_text('utf-8-sig')
    m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<988:raise ValueError('Full regression missing')
    for name in ('final-targeted-coverage','final-targeted-ai','final-targeted-analytics'):
        if not re.search(r'\sOK\s*$',(qa/(name+'.txt')).read_text('utf-8-sig')):raise ValueError(name)
    if not business['passed'] or business['differences'] or business['additional_ignored_fields']:raise ValueError('Business differs')
    if not analytics['passed'] or analytics['differences']:raise ValueError('Analytics differs')
    if analytics['dynamic_exclusions']!=['report.as_of','report.history.as_of']:raise ValueError('Unexpected exclusions')
    if not gui['passed'] or not gui['frozen_exe'] or gui['exit_code'] or len(gui['checks'])!=20:raise ValueError('Actual EXE failed')
    if gui['source_commit']!=build['source_commit'] or gui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE/build mismatch')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    for name,hashes in business['core'].items():
        if hashes['before']!=hashes['after'] or sha(ROOT/'src'/name)!=hashes['before']:raise ValueError('Core changed: '+name)
    for path,value in {**frozen['protected'],**afrozen['protected'],**afrozen['databases']}.items():
        if sha(Path(path))!=value:raise ValueError('Protected input/package changed: '+path)
    release.mkdir(parents=True,exist_ok=False)
    copies={'business_comparison/equivalence.json':'business-equivalence.json','business_comparison/frozen.json':'business-frozen.json',
            'analytics_comparison_2/report.json':'analytics-report.json','analytics_comparison_2/frozen.json':'analytics-frozen.json',
            'regression.txt':'regression.txt','final-targeted-coverage.txt':'targeted-coverage.txt','final-targeted-ai.txt':'targeted-ai.txt',
            'final-targeted-analytics.txt':'targeted-analytics.txt','exe-ui-rc2/report.json':'exe-report.json','exe-ui-rc2/mcp-calls.json':'mcp-calls.json'}
    for source,target in copies.items():shutil.copyfile(qa/source,release/target)
    shutil.copyfile(ROOT/'build_0.44.0_rc2/build_info.json',release/'build_info.json')
    for p in (qa/'exe-ui-rc2').glob('0*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.44.0_rc2_预计可售天数.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        if len(z.namelist())!=len(files):raise ValueError('ZIP file count mismatch')
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP content mismatch: '+rel)
    manifest={'source_commit':build['source_commit'],'baseline':business['baseline_commit'],'version':'0.44.0','build_id':'rc2',
              'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    save(release/'manifest.json',manifest)
    lines=[]
    for sample,label in [('rich','双仓多轮4行'),('large','12店6000行')]:
        perf=analytics['performance'][sample]
        for phase,title in [('query','查询'),('export','导出')]:
            b,a=perf['before'][phase],perf['after'][phase]
            lines.append(f"| {label} {title} | {b['median']:.4f}（{b['min']:.4f}～{b['max']:.4f}） | {a['median']:.4f}（{a['min']:.4f}～{a['max']:.4f}） | {b['sampled_peak_mb']:.1f} → {a['sampled_peak_mb']:.1f} |")
    table='\n'.join(lines)
    report=f'''# 0.44.0 预计可售天数交付

日期2026-11-06；分支feat/coverage-analytics-0.44.0，本次仅本地提交、构建、验收和标签，不推送、不合并。

固定基线：{business['baseline_commit']}（0.43.1业务源码，与开始时主线339db81一致，另保留前轮方案文档）。固定构建源码：{build['source_commit']}。源码归档哈希见build_info.json；发行证据提交及标签在构建之后，不混称为构建源码。

## 完成内容

五段分析新增预计可售天数视图、单商品中文依据、最新轮次门店加权、门店逐轮天数、共同对比截止环节。Excel旧37列保留、追加AL～AP五段新增天数，并附逐轮/商品及口径表；AI分页查询及导出复用同一coverage-1服务。固定历史AN×AM，不换最新预测，不回写签发及生命周期。无有效预测、未知数量、缺单位、缺价和跨仓依据冲突分别提示。实收新增天数不是门店当前库存。

顺带修复实际联调暴露的两处接口问题：AI分类空字符串与GUI“留空全部”一致；断开连接重复清理已销毁QLocalSocket改为幂等清理。未增加业务写入权限。

模型/仓归属仍0.3.4-rules/0.5.1，参数/作业/存储格式3/1/2。model、allocation、one_box_exception、engine、两份默认业务配置、签发导出和聚水潭订单8个核心文件SHA256前后相同，逐项见business-equivalence.json。

## 独立验证

- 主体实现全量{m[1]}项回归通过（{m[2]}秒，rc1布局）；rc2只调整展示文字/列宽，最后改动后另跑覆盖指标14项、AI6项、分析16项定向检查。
- 固定基线38份原业务计算/Excel对照差异0，含14,244行真实样本和既有组合场景；保持原精确时间/进程/构建信息白名单，没有增加忽略业务字段。输入及0.43.1 EXE/ZIP哈希未变。
- 独立两组冻结合成分析库，旧新AB/BA/AB串行3轮。原报告逐字段值、类型、字典/记录顺序和snapshot_id相同；旧报表{analytics['original_excel_cells_compared']}个单元格的值、类型、数字格式、字体、填色、边框、对齐、保护和原批注相同；原工作表及37列顺序保留。
- 分析对照仅排除查询生成时刻report.as_of与report.history.as_of；只允许新增report.coverage、Excel五列/新表及扩展筛选范围。不是忽略整块历史或元数据。
- 实际打包EXE及包内stdio MCP共{len(gui['checks'])}项通过：查询、指标切换、分店分仓逐层查看、中文解释、历史缺预测、3类Excel、MCP完成状态/分页/逐轮/导出与原生全部单元格一致、停止重试、共同实收范围、过期拦截及刷新恢复。
- EXE查询及呈现{gui['query_seconds']:.4f}秒，采样界面心跳{gui['heartbeat_ticks']}次，最大间隔{gui['max_heartbeat_gap_seconds']:.4f}秒；为小型合成验收环境，不代表大库上限。

## 性能（秒；中位数及最小/最大，内存MiB）

| 样本/操作 | 0.43.1 | 0.44.0 | 主进程采样峰值 |
| --- | ---: | ---: | ---: |
{table}

新增指标及附加工作表有耗时和内存成本，未宣称提速。6,000行查询增加约0.25秒，导出增加约0.95秒。查询含原查询和新增投影，导出含两次原有有效性检查及Excel保存。相同Python/依赖/电脑，逐个独立进程，暖系统文件缓存；每组3次原值全部在analytics-report.json。内存由原采样工具每30毫秒记录主进程RSS，含解释器，可能漏短峰；本段没有计算子进程。真实大历史库、几十万行及新机性能不在本次结论内。

## 失败证据和修正

首次源码窗口发现新增metric属性与Qt原生方法重名，改为metric_choice。MCP验证发现空分类接口拒绝和已销毁socket重复清理异常并修正。测试读回Decimal文本12.0按Decimal值与12核对，不改变业务预期；样式对照按对象副本的实际样式比较。首次新旧分析对照暴露history.as_of运行时间差，明确列为第二个精确动态路径，其他历史字段全比。rc1功能验收通过后，截图复查发现逐轮表头截断及缺失文字不够准确，rc2仅缩短显示表头并将天数待核项明确标为待核；重新运行完整EXE/MCP检查。旧失败目录保留在qa/coverage_0440，不作为通过证据。过期测试主动修改隔离库摘要引发的ValueError是预期拦截，之后恢复并重新查询通过。

## 使用、交付和边界

操作及复跑命令：docs/0.44.0预计可售天数.md；完整QA目录qa/coverage_0440。新纯逻辑与分析/UI脚本可自动生成合成输入；38份旧业务对照需要另带已冻结QA样本，不能假称不依赖真实输入。

EXE：{delivery}/补货助手.exe
ZIP：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}
已逐项核对{len(files)}个ZIP文件；保留0.43.1 rc1，不改变用户业务数据库、配置或平台数据。运行需完整目录。

实际stdio MCP链路通过；本轮没有重新配置Codex/WorkBuddy或验收其自然语言回答，不把工具数值一致当作AI解释必然正确。没有重新进行真实平台、第二台电脑或全新电脑构建验收。旧历史缺少预测依据仍待核，不能用当前资料填补；当前库存周转需要连续库存/销量依据，暂不提供。逐轮趋势首期采用可导出的逐轮表，未新增折线图；异常需结合既有多轮复盘查看，不自动推断缺货原因。
'''
    (release/'交付报告.md').write_text(report,'utf-8',newline='\n')
    print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256'],'files':len(files)},ensure_ascii=False))


if __name__=='__main__':main()
