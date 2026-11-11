"""Verify fixed-source evidence and create a new local 0.51.0 delivery seal."""
import argparse,hashlib,json,re,shutil,subprocess,sys,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE='a91a8114f8d9daaa408c0a298ca53ccbc8f8c5b1'
SOURCE='4abebb2305529133c045a309e94527688a6c6154'

def sha(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):value.update(block)
    return value.hexdigest()

def read(path):return json.loads(path.read_text('utf-8'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def python_result(path):
    match=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\b',path.read_text('utf-8'))
    assert match,('Python tests not passed',str(path))
    return {'passed':True,'tests':int(match[1]),'seconds':float(match[2]),'file':path.name,'sha256':sha(path)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--qa',type=Path,required=True);args=parser.parse_args();qa=args.qa.resolve()
    build=ROOT/'build_0.51.0_rc1';delivery=ROOT/'交付_0.51.0_rc1';app=delivery/'补货助手';release=ROOT/'releases/0.51.0/rc1'
    archive=delivery/'补货助手_0.51.0_rc1.zip'
    if release.exists() or archive.exists():raise FileExistsError('Seal destination already exists')
    info=read(build/'build_info.json');assert info['source_commit']==SOURCE and info['version']=='0.51.0'
    assert info['source_mode']=='immutable git archive; no working-tree overlay'
    assert sha(build/'source.zip')==info['source_archive_sha256']
    assert not git('diff',SOURCE,'--','src','components','tests').strip(),'Runtime/test sources changed after full test'
    version=read(app/'_internal/version.json');assert version['version']=='0.51.0' and version['model_version']=='0.3.5-rules'
    full=python_result(qa/'python-full-sealed-14.log');special=python_result(qa/'final-target-13.log')
    node={}
    for name,count in [('aoxiang',244),('jst',58)]:
        file=qa/f'node-{name}-sealed-15.log';text=file.read_text('utf-8')
        assert re.search(r'(?:ℹ|#) tests '+str(count)+r'\b',text) and re.search(r'(?:ℹ|#) fail 0\b',text)
        node[name]={'tests':count,'passed':True,'sha256':sha(file)}
    exe=read(qa/'exe-01/report.json');invocation=read(qa/'exe-01/invocation.json')
    assert exe['passed'] and exe['frozen_exe'] and exe['source_commit']==SOURCE and len(exe['checks'])==23
    assert invocation['exit_code']==0 and invocation['exe_sha256_before']==invocation['exe_sha256_after']==sha(app/'补货助手.exe')
    preissue=read(qa/'exe-preissue-01/report.json');assert preissue['passed'] and len(preissue['runs'])==2
    for run in preissue['runs']:
        assert run['passed'] and run['exit_code']==0 and not run['remaining_children'] and run['exe_sha256']==invocation['exe_sha256_before']
        assert run['version']['source_commit']==SOURCE
    manual=read(qa/'manual-html-01/html-report.json');assert manual['passed'] and manual['chapters']==21 and manual['image_count']==36
    edition=read(build/'source/docs/manual/business-edition.json')
    for name,digest in edition['files'].items():assert sha(app/'使用手册'/name)==digest,('manual file',name)
    assert sha(app/'使用手册/补货助手_0.49.3_新人业务上手手册.pdf')=='e2f494ed73817a561a87265811d2474c054e39f87b31ec60092ef1bac44e7326'
    stable={}
    for name in ('model.py','one_box_exception.py','default_config.json','default_must_sell.json','warehouse_routing.py','mandatory_holding.py','admission.py','normal_orders.py','preissue.py','purchases.py','order_lifecycle.py'):
        old=git('show',BASE+':src/'+name);new=git('show',SOURCE+':src/'+name);assert old==new,name;stable[name]=hashlib.sha256(new).hexdigest()
    assert not git('diff',BASE,SOURCE,'--','components/jst_catalog').strip()
    old=read(ROOT/'releases/0.50.5/rc3/artifacts.json')
    for file in old['files']:assert sha(ROOT/file['path'])==file['sha256']
    sys.path.insert(0,str(build/'source/src'))
    from catalog_sync_bundle import verify_component
    verify_component(app/'_internal/components/jst_catalog',app/'_internal/components/aoxiang')
    # Only documentation from the exact immutable source archive is attached.
    (app/'docs').mkdir(exist_ok=True)
    document=build/'source/docs/0.51.0订单迁移与仓店缺货分析.md'
    shutil.copyfile(document,app/'docs'/document.name);shutil.copyfile(document,app/'使用说明.txt')
    files=sorted(file for file in app.rglob('*') if file.is_file())
    assert not any(file.suffix.lower() in ('.db','.sqlite','.sqlite3') for file in files)
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as package:
        for file in files:package.write(file,file.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as package:assert package.testzip() is None and len(package.infolist())==len(files)
    release.mkdir(parents=True)
    validation={'source_commit':SOURCE,'baseline':BASE,'python_full':full,'final_special':special,'node':node,
        'exe_checks':len(exe['checks']),'preissue_phases':len(preissue['runs']),'manual_chapters':21,'manual_images':36,
        'live_platform_tested':False,'business_pc_tested':False,'pushed':False,'merged':False}
    write(release/'validation.json',validation);write(release/'exe-report.json',exe);write(release/'exe-invocation.json',invocation)
    write(release/'exe-preissue.json',preissue);write(release/'manual-html.json',manual)
    write(release/'protection.json',{'unchanged_source':stable,'jst_component_unchanged':True,'old_delivery':old['files']})
    write(release/'artifacts.json',{'source_commit':SOURCE,'files':[{'path':str(file.relative_to(ROOT)),'bytes':file.stat().st_size,'sha256':sha(file)} for file in (app/'补货助手.exe',archive)]})
    write(release/'delivery-files.json',[{'path':file.relative_to(app).as_posix(),'bytes':file.stat().st_size,'sha256':sha(file)} for file in files])
    shutil.copyfile(build/'build_info.json',release/'build_info.json')
    for name in ('python-full-sealed-14.log','final-target-13.log','node-aoxiang-sealed-15.log','node-jst-sealed-15.log'):
        shutil.copyfile(qa/name,release/name.replace('.log','.txt'))
    failure=release/'failure-evidence';failure.mkdir()
    for name in ('source-02','source-03'):shutil.copyfile(qa/name/'report.json',failure/(name+'.json'))
    write(release/'source-acceptance.json',read(qa/'source-04/report.json'))
    shots=release/'screenshots';shots.mkdir()
    for file in (qa/'exe-01').glob('*.png'):shutil.copyfile(file,shots/file.name)
    report=f'''# 0.51.0 rc1 订单迁移与仓店缺货分析交付

五项开发均已实现并在固定源码 EXE 验收。应用0.51.0，模型0.3.5-rules、仓归属0.5.2、参数V3及快照格式2保持。

## 实际变化与使用

1. **订单跟踪 → 完整订单迁移／导入与参数导出**：全部本地订单及全部修订、草稿、回传、付款、实收、三种单号、分配库存、防重和删除屏障、参数、清单、原件一并导出。不是当前筛选子集。导入先校验和预览，确认后安全退出并完整恢复；目标完整目录保留同级备份，失败／中断恢复整组目录。目标连接、AI及独立分析资料保留；源登录状态不迁移。迁入作业与自动计划暂停，原回执不改写。
2. **单主窗口**：同一Windows用户重复启动唤醒原隐藏／最小化窗口，后台工作进程保留。兼容旧版窗口身份。验证中发现Windows虚拟环境启动器被误判第二份应用，已区别自己的启动器和真正业务窗口；独立旧版仍受保护。
3. **决策复查 → 完整计算表**：两张工作表，固定业务输入、当时生效参数、预测／预算／箱规／保底／改量链路和签发结果。门店及商品名称编码、标签、类目、库存等来自当时资料；未留存则空白说明，不用当前资料补写。原件和签发模板保留独立入口。
4. **门店缺货率 → 商品与待核**：预计销售天数=(原可用库存+原在途库存)/月销×30。负值按原值计算；月销≤0或必需输入缺失，留空并说明。现有缺货率不变。
5. **大仓缺货率**：独立懒加载。剔除虚拟分类包含淘汰、仓禁用、商品标签精确日配；分子为剩余可用库存≤0支数，分母为剩余支数，不要求月销。支持供货仓、供应商编码／名称、分类层级、商品、日期等自定义组合、多选筛选、完整Excel、计划、恢复、重采、原件重算、日志和数据删除。采集仅在本板块留存，不更新补货及门店分析资料。

日配只读取聚水潭商品标签，按中英文逗号、分号、换行拆分后完整匹配“日配”；“非日配”“日配专区”不匹配。必卖最低量仍低于日配、禁采、停采、禁用及其他准入排除优先级。

## 验证证据

- 同一固定源码Python全量 **{full['tests']}项通过**，{full['seconds']:.3f}秒；封版前专项 **{special['tests']}项通过**。
- 翱象Node **244项**、聚水潭Node **58项**通过，无失败、跳过。组件包完整性校验通过。翱象只增加迁入原文件的只读查找，不改采购价格、身份或防重语义。
- 实际 EXE **23项验收**通过：真实Qt、子工作进程、Excel、重复启动、隐藏恢复、大仓指标／切面／筛选／导出／重算／删除、门店销售天数、计算表、参数导出、迁移预览及关闭后的实际整组恢复。恢复前后签发与生命周期表有序签名相同，备份和完成回执存在。
- 原人工改量／签发流程 **exercise、reopen两阶段**通过，子进程无残留。证据绑定相同EXE摘要和构建提交。
- 订单迁移隔离测试覆盖WAL已提交内容、库内原件摘要、文件篡改／越界、来源与目标写锁、取消、完整回滚、崩溃恢复及再次迁移的路径关联。业务库、连接配置及独立分析资料保护均有断言。
- 模型、默认参数、必卖默认、箱规例外、仓归属、准入、原签发／采购／生命周期核心11文件与基线逐字节一致。input_adapter仅增加迁移路径读取，不宣称该文件未改变。
- 图文HTML手册21章、36图，离线图片、锚点、搜索、练习、图片放大、移动宽度通过，无外部请求。新增配图来自源码Qt合成案例；PDF仍保留0.49.3历史版且摘要不变。
- 旧0.50.5 EXE与ZIP摘要核对不变；既有交付、全部QA和失败证据保留。未推送、未合并。

过程失败：source-02、source-03均完成22项后被进程保护拦截实际迁入；定位为本次启动器误判后修复，source-04及最终EXE完成23项。报告及堆栈见failure-evidence。其他夹具／断言修正的原始日志保留于`qa/operations_0510_20261111`，不覆盖或冒充通过。

## 边界与限制

仅隔离合成资料；真实Qt、Excel和进程链路实际运行，JST平台回执模拟，未调用真实平台、删除真实业务数据或在低配业务电脑测试。没有提速／物理磁盘占用测量结论。全量日志中的既有Qt回调／弃用警告未在本轮修复。

迁入为全部订单区恢复，不混合两台电脑订单；目标有未保存内容时须按预览处理。导入只在安全退出后生效，忙碌或锁不释放会拒绝替换。原本缺失的外部文件不能补造，清单显示缺失；已留存原件照常携带。迁移备份不自动删除。自动计划仅应用运行时执行；无历史库存的日期不补0。

## 提交、交付与复跑

- 功能分支：`feat/migration-and-warehouse-analysis-0.51.0`
- 构建源码：`{SOURCE}`；基线：`{BASE}`。验收证据另行提交，标签`v0.51.0-rc1`指向证据提交。
- 程序：`交付_0.51.0_rc1/补货助手/补货助手.exe`
- 完整包：`交付_0.51.0_rc1/补货助手_0.51.0_rc1.zip`；摘要见artifacts.json，逐文件见delivery-files.json。
- 源码／实际EXE入口：`tools/verify_operations_0510.py --out <全新隔离目录> [--exe <本版EXE>]`。
- 原人工改量验收：`tools/verify_preissue_exe.py --exe <本版EXE> --output <全新隔离目录>`；封存：`tools/seal_operations_0510.py --qa qa/operations_0510_20261111`。

打包来自git archive，没有工作树覆盖。封存另附同一固定归档内的本版说明，并将其作为包内使用说明；未替换可执行文件。SHA仅证明完整性，功能结论以上述独立验收为准。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n')
    print(json.dumps({'release':str(release),'archive':str(archive),'source_commit':SOURCE,'python_tests':full['tests'],'exe_checks':23},ensure_ascii=False))

if __name__=='__main__':main()
