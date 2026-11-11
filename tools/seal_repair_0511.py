"""Seal 0.51.1 only after immutable build, regressions and isolated EXE acceptance."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from datetime import datetime
from pathlib import Path
from aoxiang_bundle import verify
ROOT=Path(__file__).resolve().parents[1]
SOURCE='dc8efe14dabc3ec681114a4977655483ae446c3d'
BASE='8fc7ef8aad8177d66d751728f58e4d393bca52d8'
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def write(p,data):p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);a=p.parse_args();qa=a.qa.resolve();qa.relative_to(ROOT/'qa')
    build=ROOT/'build_0.51.1_rc1';delivery=ROOT/'交付_0.51.1_rc1';app=delivery/'补货助手';release=ROOT/'releases/0.51.1/rc1';archive=delivery/'补货助手_0.51.1_rc1.zip'
    if release.exists() or archive.exists():raise FileExistsError('Use a new immutable release; never overwrite')
    info=read(build/'build_info.json');assert info['source_commit']==SOURCE and info['version']=='0.51.1'
    assert info['source_mode']=='immutable git archive; no working-tree overlay';assert sha(build/'source.zip')==info['source_archive_sha256']
    assert not git('diff',SOURCE,'--','src','components','tests').strip(),'Runtime or tests changed after source freeze'
    assert read(qa/'immutable-build.json')['passed']
    validation={name:read(qa/(name+'.json')) for name in ['full-python','full-aoxiang','full-jst']}
    for result in validation.values():assert result['passed'] and result['source_commit']==SOURCE and result['exit_code']==0
    assert validation['full-python']['tests']==1461
    assert validation['full-aoxiang']['pass']==validation['full-aoxiang']['tests']==250
    assert validation['full-jst']['pass']==validation['full-jst']['tests']==58
    payment=read(qa/'payment-exe-01/acceptance.json');collection=read(qa/'collection-exe-01/process-acceptance.json');reexport=read(qa/'reexport-exe-01/process-acceptance.json')
    exe_sha=sha(app/'补货助手.exe')
    for result in [payment,collection,reexport]:
        assert result['passed'] and result['source_commit']==SOURCE and result['exe_sha256']==exe_sha
        assert result.get('process_exit',result.get('process_exit_code'))==0
    assert len(payment['checks'])==32 and len(collection['checks'])==13
    assert collection['accepted'] and collection['frozen_exe'] and collection['shared_component_closed']
    assert reexport['accepted'] and reexport['shared_component_closed']
    login=read(qa/'packaged-login.json');assert login['passed'] and login['exit_code']==0 and login['all_requests_intercepted']
    native=app/'_internal/components/aoxiang';verify(native)
    assert login['runtime_sha256']==info['aoxiang']['node_sha256']==sha(native/'runtime/node.exe')
    assert login['adapter_sha256']==sha(native/'src/aoxiang-ui.mjs')==hashlib.sha256(git('show',SOURCE+':components/aoxiang/src/aoxiang-ui.mjs')).hexdigest()
    assert re.search(r'(?:ℹ|#) pass 5\b',(qa/'packaged-login.log').read_text('utf-8'))
    manual=read(qa/'manual-html-01/html-report.json');assert manual['passed'] and manual['chapters']==21 and manual['image_count']==38
    edition=read(build/'source/docs/manual/business-edition.json')
    for name,digest in edition['files'].items():assert sha(app/'使用手册'/name)==digest,('manual',name)
    assert sha(app/'使用手册/补货助手_0.49.3_新人业务上手手册.pdf')=='e2f494ed73817a561a87265811d2474c054e39f87b31ec60092ef1bac44e7326'
    stable={}
    for name in ['model.py','one_box_exception.py','default_config.json','default_must_sell.json','warehouse_routing.py','mandatory_holding.py','admission.py','normal_orders.py','preissue.py','purchases.py','work_repository.py','data_maintenance.py','maintenance_storage.py','input_adapter.py']:
        old=git('show',BASE+':src/'+name);new=git('show',SOURCE+':src/'+name);assert old==new,name;stable[name]=hashlib.sha256(new).hexdigest()
    assert not git('diff',BASE,SOURCE,'--','components/jst_catalog').strip()
    old_artifacts=read(ROOT/'releases/0.51.0/rc1/artifacts.json')
    for artifact in old_artifacts['files']:assert sha(ROOT/artifact['path'])==artifact['sha256'],'Old delivery overwritten'
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in sorted(app.rglob('*')):
            if file.is_file():z.write(file,file.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    assert sha(app/'补货助手.exe')==exe_sha
    release.mkdir(parents=True)
    write(release/'build_info.json',info)
    artifacts={'source_commit':SOURCE,'files':[{'path':str(p.relative_to(ROOT)),'bytes':p.stat().st_size,'sha256':sha(p)} for p in [app/'补货助手.exe',archive]]}
    write(release/'artifacts.json',artifacts)
    write(release/'protection.json',{'source_commit':SOURCE,'baseline':BASE,'unchanged_sources':stable,'jst_component_unchanged':True,'old_0510_artifacts_unchanged':old_artifacts,'old_pdf_unchanged':True,'real_business_data_touched':False})
    write(release/'validation.json',{'source_commit':SOURCE,'baseline':BASE,'sealed_at':datetime.now().astimezone().isoformat(),'regressions':validation,
          'exe_checks':{'payment':len(payment['checks']),'legacy_collection':len(collection['checks']),'selected_reexport':len(reexport['checks'])},
          'packaged_login_tests':5,'exe_sha256':exe_sha,'manual_chapters':21,'manual_images':38,'live_platform':False,'business_pc_tested':False,'pushed':False,'merged':False})
    for source,destination in [('payment-exe-01/acceptance.json','payment-exe.json'),('collection-exe-01/process-acceptance.json','collection-exe.json'),('reexport-exe-01/process-acceptance.json','reexport-exe.json'),('packaged-login.json','packaged-login.json'),('manual-html-01/html-report.json','manual-html.json'),('full-python.log','python-full.txt'),('full-aoxiang.log','node-aoxiang-full.txt'),('full-jst.log','node-jst-full.txt'),('packaged-login.log','packaged-login.txt'),('python-targeted-final.log','python-targeted.txt')]:shutil.copy2(qa/source,release/destination)
    screens=release/'screenshots';screens.mkdir()
    for folder in ['payment-exe-01','collection-exe-01','reexport-exe-01']:
        for file in (qa/folder).glob('*.png'):shutil.copy2(file,screens/(folder+'-'+file.name))
    failures=release/'failure-evidence';failures.mkdir()
    for file in ['node-targeted-01.log','python-targeted-02.log','payment-source-01.log','collection-source-01/exe-report.json']:
        shutil.copy2(qa/file,failures/file.replace('/','-'))
    write(failures/'index.json',{'not_deleted':True,'items':[
        {'file':'node-targeted-01.log','reason':'New test fixture deleted a command before its FK events; backup file glob also counted SQLite sidecars. Fixed fixture and exact .sqlite selection; 35 targeted and 250 full component tests then passed.'},
        {'file':'python-targeted-02.log','reason':'Fake client raised KeyError rather than the actual COMMAND_NOT_FOUND contract for an unaccepted local intention. Corrected test transport; final 36 targeted tests passed.'},
        {'file':'payment-source-01.log','reason':'QA generator read products from the generated-order header; canonical lifecycle head contains that field. Corrected fixture; source and EXE then passed 32 checks.'},
        {'file':'collection-source-01-exe-report.json','reason':'QA wait returned before the confirmed delete was dispatched on the next Qt event. Added pending-maintenance settlement; source and EXE then passed 13 checks.'}],
        'earlier_diagnosis':'qa/diagnose_binding_collection_20261111_01, _02, _03 and qa/diagnose_login_20261111_01 are retained; initial failed probes were fixture/transport mistakes and corrected before implementation.'})
    write(release/'delivery-files.json',{'files':[{'path':p.relative_to(app).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(app.rglob('*')) if p.is_file()]})
    report=f'''# 0.51.1 rc1 交付报告

付款、旧采集维护及登录核验四类问题已修复，并独立打包。只使用隔离资料，未连接真实平台，未改业务电脑资料。旧版、QA及失败证据保留；未推送、未合并。

## 修复结果

1. **付款整单绑定**：移除按SKU旧签发仓归属拒绝当前付款的判断。自动认单仍比较完整商品、数量和金额；明确选择整张仓单后可以核对保存实际付款差异，不使用辅助码、条码判仓。明确文件身份冲突、源单失效及并发校验保留。
2. **核对没反应**：核对/保存失败弹出原因及完整详情，同时保留来源文件、目标选择和页面内说明；不再只写底部小字。预览和保存仍分开。
3. **旧记录占用**：升级前备份，给可证明已明确结束的旧采集补齐退役屏障；原平台号、受理事实和文件保留，已结束日志可删，晚到回执不恢复占用。真实未完成任务定位到准确日期、门店、状态与原记录，保留“继续原任务”和“重新导出所选门店”入口；其他门店不变。
4. **已登录仍卡核验**：同源SPA切路由后主动刷新，在刷新前监听本次身份查询；只读读取临时失败最多再核验一次，不创建/重放导出。分别报告身份请求、响应读取及业务页就绪阶段，显示耗时，失败结束不确定进度。

隔离Edge复现了“只切hash不再请求身份”的缺口，与用户手动刷新正常的反馈一致。未取得业务电脑现场诊断，不能把它宣称为现场唯一原因。完整改动范围见源码文档`docs/0.51.1付款绑定与采集登录修复.md`。

## 验证

- Python全量 **1461项通过**，耗时{validation['full-python']['test_seconds']}秒；本轮专项36项通过。
- 翱象组件 **250项通过**；聚水潭组件 **58项通过**。使用各组件`node --test test/*.test.mjs`入口。
- 同一实际EXE：付款 **32项**、旧采集升级及删除 **13项**、所选门店重导 **{len(reexport['checks'])}项**通过，进程正常退出；采集验收确认共享组件正常关闭。付款校验保存实际商品/金额，未选订单和签发快照不变；采集校验业务数据库、已采用清单和原文件不变。
- 用包内Node和JS执行 **5组真实Edge隔离测试**：已登录SPA、首次打开、临时响应失败、持续失败、未登录/身份不符及业务页未就绪；全部网络请求本地拦截，未使用用户浏览器资料。
- 手册已同步，HTML **21章、38图**离线检查通过；0.49.3 PDF仅作历史归档，哈希未变。新增图明确注明源码Qt隔离演练；本目录另存实际EXE验收截图。
- 模型、预算、箱规、默认参数、必采规则、商品归属、业务删除事务及聚水潭组件与基线逐文件相同。付款只移除这条旧仓推断，不放宽原件完整性、源单和事务校验。

## 交付与版本

- 分支：`fix/payment-collection-login-0.51.1`。
- 基线：`{BASE}`；构建源码提交：`{SOURCE}`。
- 应用0.51.1，模型0.3.5-rules，归属0.5.2，参数3，业务存储格式2。
- EXE：`交付_0.51.1_rc1/补货助手/补货助手.exe`；SHA256 `{exe_sha}`。
- ZIP：`交付_0.51.1_rc1/补货助手_0.51.1_rc1.zip`；SHA256 `{sha(archive)}`。
- 构建：`build_0.51.1_rc1`，从Git归档构建，无工作区覆盖。验收证据提交及本地标签记录在Git中，区别于上述构建源码提交。
- 复跑目录：`{qa.relative_to(ROOT).as_posix()}`。实际EXE入口为`tools/verify_payment_matching.py`、`tools/verify_collection_ended.py`，每次指定全新输出目录；所选重导用该QA目录内`verify_reexport_isolated.py`额外加入合成QA单实例标记。包内登录验证入口为该目录内`verify_packaged_login.py`。封存入口：`tools/seal_repair_0511.py --qa {qa.relative_to(ROOT).as_posix()}`。

## 使用与限制

关闭旧版后使用新包，保留原配置资料目录。首次启动组件会对符合条件的旧状态自动备份并修复；可回原记录删除已结束日志。真实未完成记录不会擅自删除：需要新文件时按所选门店重导。付款明确选对整张仓单后按核对页确认实际事实；文件本身的明确身份冲突仍需纠正。

本轮未在业务电脑实测网络、真实页面权限或平台端到端，未提供删除性能提升数据。现有QLabel销毁后回调及弃用警告不属于本轮修复，不宣称已解决。首次失败诊断和新夹具/验收等待条件错误均保留，说明见`failure-evidence/index.json`；不能把最终通过描述为每次运行都通过。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n')
    print(json.dumps({'release':str(release),'exe_sha256':exe_sha,'archive':str(archive),'zip_mib':round(archive.stat().st_size/1024**2,2),'source_commit':SOURCE},ensure_ascii=False))
if __name__=='__main__':main()
