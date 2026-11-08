"""Seal a fixed, verified 0.49.0 build; refuse overwrites of prior evidence/ZIP."""
import hashlib,html,json,re,shutil,subprocess,zipfile
from pathlib import Path
from aoxiang_bundle import verify
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/payment_matching_0490'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,value):
    with p.open('x',encoding='utf-8',newline='\n') as f:f.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def main():
    build=read(ROOT/'build_0.49.0_rc1/build_info.json');commit=build['source_commit'];assert build['version']=='0.49.0'
    assert not subprocess.check_output(['git','diff',commit,'--','src','components','tools/verify_payment_matching.py','tools/verify_packaged_payment_matching.py'],cwd=ROOT)
    raw=(QA/'phase1/regression1.log').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s',raw);assert m and int(m[1])>=1287
    split=not bool(re.search(r'\sOK\s*$',raw))
    if split:
        assert raw.rstrip().endswith('FAILED (errors=1)') and raw.count('ERROR: test_')==1
        assert 'ERROR: test_due_schedule_rechecked_after_read_phase (test_queue_lock_recovery.QueueLockRecovery.test_due_schedule_rechecked_after_read_phase)' in raw and 'p1：本机订单数据库不存在' in raw
        changed=subprocess.check_output(['git','diff','--name-only',commit,'--','tests'],cwd=ROOT,text=True).splitlines();assert changed==['tests/test_queue_lock_recovery.py']
        supplement=(QA/'phase1/queue-lock-supplement.log').read_text('utf-8-sig');assert re.search(r'Ran 6 tests .*\s+OK\s*$',supplement)
        assert 'test_due_schedule_rechecked_after_read_phase (test_queue_lock_recovery.QueueLockRecovery.test_due_schedule_rechecked_after_read_phase) ... ok' in supplement
    ids=re.findall(r'^(test\w+ \([^\n]+\)) \.\.\.',raw,re.M);assert len(ids)==len(set(ids))==int(m[1])
    node=(QA/'phase1/node-all2.log').read_text('utf-8-sig');assert re.search(r'(?:#|ℹ) tests 199',node) and re.search(r'(?:#|ℹ) pass 199',node) and re.search(r'(?:#|ℹ) fail 0',node) and re.search(r'(?:#|ℹ) skipped 0',node)
    exe=read(QA/'exe1/acceptance.json');source=read(QA/'source3/report.json');component=read(QA/'component1/report.json');pricing=read(QA/'pricing-component1/report.json');perf=read(QA/'performance5/report.json');sample=read(QA/'sample-read.json')
    assert exe['passed'] and exe['process_exit']==0 and len(exe['checks'])==14 and exe['source_commit']==commit and not exe['live_platform']
    assert source['passed'] and len(source['checks'])==14 and source['version']=='0.49.0'
    assert component['passed'] and component['tests']>=20 and not component['live_platform'] and pricing['passed'] and pricing['tests']==8
    assert perf['passed'] and sample['passed'] and sample['source_unchanged'] and not sample['saved_business']
    assert perf['sampler_sha256']==sha(ROOT/'tools/benchmark_payment_matching.py')
    previous=read(ROOT/'releases/0.48.7/rc1/frozen.json');old=read(ROOT/'releases/0.48.7/rc1/validation.json');protected=dict(previous['preserved_deliveries'])
    protected['交付_0.48.7_rc1/补货助手/补货助手.exe']=old['exe_sha256'];protected[old['zip']]=old['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in previous['core'].items());assert all(sha(ROOT/p)==h for p,h in protected.items())
    assert not subprocess.check_output(['git','diff','v0.48.7-rc1',commit,'--','components/jst_catalog'],cwd=ROOT)
    delivery=ROOT/'交付_0.49.0_rc1/补货助手';manifest=verify(delivery/'_internal/components/aoxiang')
    for p in (ROOT/'build_0.49.0_rc1/source/components/aoxiang/src').glob('*.mjs'):assert sha(p)==sha(delivery/'_internal/components/aoxiang/src'/p.name)
    release=ROOT/'releases/0.49.0/rc1';release.mkdir(parents=True,exist_ok=False)
    sources={'build_info.json':ROOT/'build_0.49.0_rc1/build_info.json','regression.log':QA/'phase1/regression1.log','queue-lock-supplement.log':QA/'phase1/queue-lock-supplement.log','node-tests.log':QA/'phase1/node-all2.log','exe-report.json':QA/'exe1/acceptance.json','source-report.json':QA/'source3/report.json','component-report.json':QA/'component1/report.json','component-tests.log':QA/'component1/tests.log','pricing-component.json':QA/'pricing-component1/report.json','pricing-component-tests.log':QA/'pricing-component1/tests.log','performance.json':QA/'performance4/report.json','sample-read.json':QA/'sample-read.json'}
    sources['performance.json']=QA/'performance5/report.json';sources['benchmark_payment_matching.py']=ROOT/'tools/benchmark_payment_matching.py'
    for name in ('付款识别.png','付款核对.png','同步历史只读.png'):sources[name]=QA/'exe1'/name
    for name,p in sources.items():shutil.copyfile(p,release/name)
    failed=release/'retained-development';failed.mkdir()
    for name in ('matching1.log','python1.log','python2.log','performance1.log','performance4.log'):
        shutil.copyfile((QA/name) if name=='matching1.log' else QA/'phase1'/name,failed/name)
    write(failed/'README.json',{'not_final_acceptance':True,'matching1':'Development test fixture left a SQLite connection open; duplicate imported test class was removed. Final discovery contains unique test IDs.','python1':'Early isolated unittest blocked on UI error dialog after newly enforced local identity guard; owned test process stopped, no business task killed.','python2':'UI-only fake identities did not satisfy new local guard; unrelated queue fake was isolated and real DB tests added. An explicit fixture connection leak was fixed. No production validation relaxed.','performance1':'Sampler dependency missing; exited before measurement. Existing QA-only psutil reused.','performance2_and_3':'Early timing probes, including an incorrect venv-launcher RSS sample and a source change during development. Retained under qa, excluded from performance claims.','performance4':'Parent read the worker PID handoff before the first write completed. QA sampler now tolerates this brief handoff; runtime unchanged. Final performance5 targets actual worker PID and native Windows peak.'})
    manual=delivery/'使用手册/0.49.0付款识别与采购状态';manual.mkdir(parents=True,exist_ok=False)
    for name in ('付款识别.png','付款核对.png','同步历史只读.png'):shutil.copyfile(release/name,manual/name)
    document=(ROOT/'build_0.49.0_rc1/source/docs/0.49.0付款识别与采购状态.md').read_text('utf-8')
    with (manual/'使用说明.md').open('x',encoding='utf-8') as f:f.write(document.replace('../../releases/0.49.0/rc1/',''))
    blocks=[];steps=False
    for line in document.splitlines():
        if line.startswith('!['):
            match=re.match(r'!\[(.*?)\]\((.*?)\)',line);blocks.append('<figure><img src="'+html.escape(Path(match[2]).name)+'"><figcaption>'+html.escape(match[1])+'</figcaption></figure>');continue
        line=re.sub(r'\[([^\]]+)\]\([^)]+\)',r'\1',line)
        if line.startswith('# '):blocks.append('<h1>'+html.escape(line[2:])+'</h1>')
        elif line.startswith('## '):blocks.append('<h2>'+html.escape(line[3:])+'</h2>')
        elif line:blocks.append('<p>'+html.escape(line)+'</p>')
    with (manual/'打开使用说明.html').open('x',encoding='utf-8') as f:f.write('<!doctype html><meta charset="utf-8"><title>付款单识别与采购状态</title><style>body{max-width:1100px;margin:40px auto;padding:0 28px;font:18px/1.8 "Microsoft YaHei",sans-serif;color:#193e43}h1,h2{color:#176e65}h2{margin-top:36px}img{max-width:100%;border:1px solid #d0dfe0}figure{margin:28px 0}figcaption{font-size:14px;color:#667d82}p{white-space:pre-wrap}</style>'+''.join(blocks))
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()};target=delivery.parent/'补货助手_0.49.0_rc1_付款识别与采购状态.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in hashes:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(hashes)
        for name,h in hashes.items():
            with z.open('补货助手/'+name) as f:assert hashlib.file_digest(f,'sha256').hexdigest()==h
    write(release/'manifest.json',{'source_commit':commit,'files':hashes});write(release/'frozen.json',{'core':previous['core'],'preserved_deliveries':protected})
    report={'passed':True,'source_commit':commit,'python_tests':int(m[1]),'unique_python_test_ids':len(set(ids)),'python_seconds':float(m[2]),'python_single_run_passed':not split,'python_fixture_supplement_required':split,'python_fixture_supplement_tests':6 if split else 0,'node_tests':199,'exe_checks':len(exe['checks']),'component_tests':component['tests'],'pricing_component_tests':pricing['tests'],'live_platform':False,'core_unchanged':len(previous['core']),'preserved_delivery_files':len(protected),'component_files_verified':len(manifest['files']),'exe_sha256':sha(delivery/'补货助手.exe'),'zip':str(target.relative_to(ROOT)),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'files':len(hashes)}
    write(release/'validation.json',report)
    modes={'baseline':'原完整候选读取','cold':'首次轻量索引','warm':'已有索引','expanded':'已有索引＋全日期识别','cancel':'冷索引中途取消'}
    rows=[]
    for r in perf['runs']:
        rows.append(f"| {r['stores']} | {modes[r['mode']]} | {r['seconds']:.3f} | {r['native_peak_worker_rss_mib']:.1f} | {r['process_read_mib']:.3f} | {r['process_write_mib']:.3f} | {r.get('decoded_snapshots','—')} |")
    text=f'''# 0.49.0 rc1 交付报告

已实施付款单分层识别、作废采购状态、已知采购号精查和有效收货同步范围。未调用真实平台，未清理真实业务数据，未推送或合并。

## 固定来源与交付

- 分支：`feature/payment-matching-and-purchase-status-0.49.0`。
- EXE固定源码：`{commit}`；不可变Git归档构建，应用0.49.0、模型0.3.4-rules、存储格式2。验收工具及测试夹具修正和本报告在后续证据提交，不能混称为EXE源码。
- 标签：`v0.49.0-rc1`（封存后本地创建）。
- 应用：`交付_0.49.0_rc1/补货助手/补货助手.exe`，需完整目录。
- 压缩包：`{report['zip']}`，{report['zip_bytes']/1048576:.1f} MiB。
- EXE SHA256：`{report['exe_sha256']}`。
- ZIP SHA256：`{report['zip_sha256']}`。
- 完整文件哈希见[manifest.json](manifest.json)。{len(previous['core'])}项模型／参数／归属／分配／删除等核心文件与{len(protected)}个旧交付文件哈希保持。聚水潭采集组件源码未改。
- [图文业务说明](../../../docs/0.49.0付款识别与采购状态.md)；包内“使用手册/0.49.0付款识别与采购状态/打开使用说明.html”可离线打开。

## 实际原因和改动

| 问题 | 已确认代码原因 | 本轮行为 |
| --- | --- | --- |
| 付款表缺签发号、仓；店名有差异 | 原paid_order适配要求完整13列表头，只识别买家账号；缺号只提供人工候选，没有来源企业＋内部号关系和分层明细索引 | 最小四列及买家帐号别名；来源企业来自既有核验coid，先查原内部号，再按生命周期、门店、时间、汇总和生成明细核对。格式归一，业务别名明确确认；多候选不默认第一张 |
| 作废仍待实收、周期反复出现 | 原终态主要按已完成；作废空白实收被当完整性问题，终态与最终实收待办未分开 | VOIDED／VOIDED_REVIEW分开；清洁作废停轮询且不写实收0；冲突保留原事实和证据 |
| 有采购号还要求日期、店名 | 原查询范围强绑定仓店自然日 | 独立order_id范围、核验实际请求无隐藏筛选；仅在无导出意图且有原生制单日证明时降级，受理未知不重提 |
| 同步页出现已删旧订单 | 原关联台账为审计防重而保留，但正常候选与本地有效订单及原付款身份未统一核对 | 本批／勾选范围传递，全局有效池另入口；OID、修订、可见性及付款版本复核；历史只读；排队／周期／派发／轮询重验，安全退役本地调度投影 |

付款与新增内部号／门店对应复用原生命周期服务，同一事务保存／回滚。对应附在独立预览侧元数据，不污染付款内容摘要，所以重复导入相同事实不增加版本。已确认内部号不能改绑同号新订单，门店名称对应可显式解除并先备份。

索引只在进入相应付款导入后加载，不在启动时扫描。首次仅逐份解码当前签发／当前生成，缓存轻量名称、范围、汇总和版本；完整复核读真实候选。确认门店后只读该门店相关全部日期，不能用窄日期排掉同内容候选。缓存损坏保留损坏文件后重建；取消不写业务事实，允许保留已验证缓存进度。

## 验证结果与限制

- Python完整执行{int(m[1])}个唯一测试，首轮{int(m[1])-1}项通过、1项旧调度锁夹具报错：该夹具没有任何本地业务数据库，被新增有效性校验正确阻止。补上真实隔离签发与付款后，原锁竞争断言保持，该模块6项补验通过；生产代码未因此改变。没有把首轮说成全量一次通过，原始[完整日志](regression.log)与[补验日志](queue-lock-supplement.log)均保留。
- Node全套199项通过，0失败、0跳过。[日志](node-tests.log)。
- 实际EXE14项隔离验收通过，确认固定源码提交；含启动不加载、来源选择、唯一候选、日期不误排、完整范围确认、付款／关系保存、重复导入、作废不写0、历史只读、取消禁用旧预览。[EXE报告](exe-report.json)。
- 包内Node及生产模块28项查询／收货／作废／恢复测试通过，另8项原采购计价专项通过；离线原生格式Excel夹具与本机隔离Edge，没有请求真实翱象。[组件报告](component-report.json)。
- 用户原样表只读识别1份来源单、155行商品；表头买家帐号保留，O列额外汇总没有当商品读取。文件SHA256前后一致：`{sample['source_sha256']}`。未保存该付款，未绑定缺失的原订单，未读取或迁移登录凭据。
- 新增真实隔离场景覆盖跨日同内容不自动选、企业同内部号隔离、合计同但SKU不同、缺行／额外行／空白、映射及依据并发变化、提交回滚、旧冻结身份与同号重建、调度和轮询撤销。原业务校验未放宽。
- 开发期夹具连接未关闭、旧UI假身份与新增检查不兼容、采样依赖缺失等失败保留在`retained-development/`；早期测量还在本机QA目录，不用于最终性能结论。

## 资源测量

{perf['rounds']}轮5／25／100店合成样本（SKU很少），同机，系统缓存未清。原路径只读完整候选；首次索引另外生成摘要，工作量不同，不能将此表宣传为完整付款端到端提速比例。新旧候选仓单身份集合零差异，热索引重复解码0份快照；所有测量前后主业务库哈希不变。

| 门店数 | 操作 | 操作秒 | 工作进程峰值RSS MiB | 进程读 MiB | 进程写 MiB | 解码快照数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

RSS用实际工作进程的Windows高水位，并保留20ms采样；不误取venv启动器。I/O为进程计数、含系统缓存，不是物理磁盘负载。扩大检索包含付款文件读取和明细复核；取消在进度第3步触发，可能留下轻量索引，不写业务。原始[性能记录](performance.json)。未测低配业务电脑、大SKU真实历史库或真实平台耗时，不承诺固定响应秒数和整机内存上限。本轮也未重测删除性能，不把新索引指标用于删除提速宣传。

## 仍需现场验证

真实翱象页面是否能清空所有日期并按PO号唯一查询，尚未现场验证；离线Edge验证了清空控件、实际范围拒绝及安全降级。平台若强制日期且原件没有可信制单日，将明确停止，不猜日期、不重复创建任务。

付款原表没有可靠企业身份时需确认来源；业务别名、多个相同历史候选、混仓和生成明细变化仍需人工核对。已删除原订单不会凭文件内容自动重建或补绑。作废不是退款／删除本地事实；原收货冲突需要业务处理。

既有`browser_status_ui`的QLabel销毁后回调警告仍保留，本轮未修。新版本未自动覆盖正在运行的旧版，应关闭旧版后从新的完整目录启动。
'''
    with (release/'交付报告.md').open('x',encoding='utf-8',newline='\n') as f:f.write(text)
    print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
