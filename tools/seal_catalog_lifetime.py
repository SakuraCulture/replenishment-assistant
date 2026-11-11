"""Seal the isolated 0.50.3 rc2 ownership recovery evidence; new paths only."""
import argparse,hashlib,json,re,shutil,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);a=p.parse_args()
    qa=a.qa.resolve();qa.relative_to(ROOT/'qa');release=ROOT/'releases/0.50.3/rc2'
    if release.exists():raise FileExistsError(release)
    delivery=ROOT/'交付_0.50.3_rc2';app=delivery/'补货助手';build=ROOT/'build_0.50.3_rc2'
    info=json.loads((build/'build_info.json').read_text('utf-8'));assert info['version']=='0.50.3' and info['build_id']=='rc2'
    assert json.loads((app/'_internal/build_info.json').read_text('utf-8'))==info
    source=info['source_commit'];exe_sha=sha(app/'补货助手.exe')
    proofs={}
    for label,file in [('exe-lifetime','exe-lifetime/process-acceptance.json'),('exe-pipeline','exe-pipeline/process-acceptance.json'),('source-lifetime','source-ui-01/process-acceptance.json')]:
        value=json.loads((qa/file).read_text('utf-8'));assert value['accepted'] and value['passed'] and not value['live_platform']
        if label.startswith('exe-'):assert value['exe_sha256']==exe_sha and value['process_exit']==0 and value.get('frozen',value.get('frozen_exe'))
        proofs[label]=value
    parent=json.loads((qa/'exe-lifetime/parent-crash-recovery.json').read_text('utf-8'));assert parent['passed'] and parent['frozen'] and parent['owned_child_exited'] and parent['jobs_unchanged']
    full=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',(qa/'python-full.log').read_text('utf-8'));assert full
    counts={}
    for label,file in [('jst','node-jst-final.log'),('aoxiang','node-aoxiang-full.log')]:
        text=(qa/file).read_text('utf-8');assert re.search(r'fail 0\b',text);counts[label]=int(re.search(r'tests (\d+)',text)[1])
    assert re.search(r'Ran 26 tests .*\s+OK\s*$',(qa/'python-catalog-sealed.log').read_text('utf-8'))
    protection=json.loads((qa/'protection.json').read_text('utf-8'))
    for row in protection['old_0502_artifacts']:assert sha(ROOT/row['path'])==row['sha256']
    for name,digest in protection['model_unchanged'].items():assert sha(ROOT/'src'/name)==digest
    manual=json.loads((qa/'manual-html/html-report.json').read_text('utf-8'));assert manual['passed'] and manual['image_count']==30
    from catalog_sync_bundle import verify_component
    verify_component(app/'_internal/components/jst_catalog',app/'_internal/components/aoxiang')
    files=sorted(p for p in app.rglob('*') if p.is_file());assert not any(p.suffix in ('.sqlite','.sqlite3','.db') for p in files)
    archive=delivery/'补货助手_0.50.3_rc2.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in files:z.write(file,file.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None and len(z.infolist())==len(files)
    release.mkdir(parents=True)
    def write(name,value):(release/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')
    for label,value in proofs.items():write(label+'.json',value)
    write('parent-crash-recovery.json',parent);write('protection.json',protection);write('manual-html.json',manual)
    shutil.copyfile(build/'build_info.json',release/'build_info.json')
    for src,dest in [('before-fix.log','before-fix.txt'),('python-full.log','python-full.txt'),('python-catalog-sealed.log','python-catalog-final.txt'),('node-jst-final.log','node-jst-final.txt'),('node-aoxiang-full.log','node-aoxiang-full.txt'),('python-catalog-initial.log','early-handle-warning.txt')]:shutil.copyfile(qa/src,release/dest)
    artifacts={'source_commit':source,'files':[{'path':str(p.relative_to(ROOT)),'bytes':p.stat().st_size,'sha256':sha(p)} for p in (app/'补货助手.exe',archive)]};write('artifacts.json',artifacts)
    write('delivery-files.json',[{'path':p.relative_to(app).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in files])
    write('evidence-manifest.json',[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(qa.rglob('*')) if p.is_file() and p.name!='seal.log'])
    validation={'source_commit':source,'base':'51193fa','python_full_tests':int(full[1]),'python_full_seconds':float(full[2]),'final_python_catalog_tests':26,'node_jst_tests':counts['jst'],'node_aoxiang_tests':counts['aoxiang'],'exe_checks':sum(len(p['checks']) for k,p in proofs.items() if k.startswith('exe-')),'parent_crash_recovered':True,'original_jobs_unchanged':True,'old_0502_preserved':True,'live_platform_tested':False,'business_pc_retested':False,'pushed':False,'merged':False}
    write('validation.json',validation)
    report=f'''# 补货助手 0.50.3 rc2：聚水潭采集服务退出与占锁恢复

交付日期：2026-11-10。分支 `fix/catalog-service-lifetime-0.50.3`。本地提交和交付，未推送、未合并。

## 现场现象和定位

业务端商品资料同步提示 `database is locked`，关软件重开无效、重启电脑后恢复。没有取得当次业务电脑的进程或日志，不能把开发机状态当作现场证据，也不能断言现场唯一持锁者。

隔离复现确认了一个相符的实际代码缺口：关闭时先等待浏览器断连，再释放本地任务库锁；断连抛错会跳过释放，断连长期不返回会一直占锁。修复前新增2项用例中1项失败，直接出现相同锁错误；`before-fix.txt`保留。

## 修复范围

- 停止新动作，先等在途任务及状态提交完成；浏览器断连清理错误或超时也走finally释放任务锁。超时仅限制闲置连接清理，不限制提交或状态保存。
- 标准输入关闭、关闭命令及父进程消失共用一次正常收尾。等待保存时保持Node存活，避免管道关闭后只剩Promise而提前结束；安全收尾完成才结束自身传输进程。
- 主程序协议失败后，先等待自己持有的旧子进程退出，再创建新连接；不在旧服务旁反复启动。子进程先退出也关闭主程序残留写管道，每次启动使用独立技术选项文件。
- 原服务仍有效占锁时，保留保护并显示中文原因、已知进程号和下一步。新归属文件仅保存进程身份，不保存凭据；不删除锁文件抢占，也不按node进程名称批量结束服务。
- 原采集任务号、下载文件、账号与采用记录保留；任务JSON业务格式不变。模型、预算、箱规、默认参数和仓归属不变。0.50.2采购重建及等待流水线保留。

## 本轮验证

| 范围 | 结果 |
|---|---|
| Python全量回归 | **{validation['python_full_tests']}项通过**，{validation['python_full_seconds']}秒 |
| 最终Python商品资料专项 | **26项通过**；将ResourceWarning作为错误检查，未再出现句柄泄漏 |
| 聚水潭组件全量 | **{counts['jst']}项通过**，含8项新增退出/持锁保护测试 |
| 翱象组件全量 | **{counts['aoxiang']}项通过** |
| 固定源码实际EXE | **{validation['exe_checks']}项通过**：资料生命周期6项、上一版采购重建/流水线6项，两个验收进程退出0 |
| 实际EXE主进程异常退出 | 闲置状态明确模拟主进程退出23，所属真实Node退出；随后原目录直接重开，原任务文件SHA保持 |
| 图文教程 | HTML20章30图，离线图片、搜索、放大、移动宽度及历史PDF链接通过；PDF保留为0.49.3归档 |
| 保护 | 0.50.2 rc1 EXE/ZIP哈希保持，7个核心源文件逐字节保持 |

全量Python在本轮候选源码上执行；补入最后的Node存活保护后，又执行最终58项JST组件、26项Python专项及最终EXE验收。初始rc1构建保留，未作为最终业务交付；最终源码和构建为rc2。早期专项发现子进程先退出时主程序未关闭写管道的ResourceWarning，修复后严格检查通过，原日志保留。

所有验证使用隔离目录、合成任务和真实本地组件，不调用真实聚水潭或翱象平台，没有初始化或删除真实业务资料。实际EXE验证了正常退出、异常退出、资料窗口复用、真实占锁提示及重开；提交中的安全等待、晚到回执保存和断连卡住由封闭组件用例验证，没有人为终止实际平台提交。

## 现场边界

业务电脑尚未升级后复测，本报告不宣称现场已验证解决。仍有有效服务工作时，本版继续阻止第二个服务接管；来自未修复旧版的未知残留也不自动强杀。业务端此前已重启清理占用，更新本版后重点观察是否再留锁。既有QLabel销毁后回调问题不在本轮修复范围，未宣称已解决。

## 交付与复跑

- 固定源码：`{source}`；初始修复源码：`d708dec6a6edc2324377cc6475636bf0fac9a1ad`。
- 构建：`tools/build_from_commit.py --commit {source} --build-id rc2`，独立Git归档，无工作区覆盖。
- EXE：`交付_0.50.3_rc2/补货助手/补货助手.exe`。
- ZIP：`交付_0.50.3_rc2/补货助手_0.50.3_rc2.zip`，完整解压后使用。
- 使用说明：`docs/0.50.3聚水潭采集服务退出与占锁恢复.md`、随包新人手册第20.1节。
- 复跑：`tools/verify_catalog_lifetime.py --exe <新EXE> --out <qa下全新目录>`；先读脚本，只允许隔离合成资料。该脚本有明确的闲置父进程异常退出模拟，不可用于实际业务进程。
- 原始证据：`qa/catalog_service_lifetime_0503_20261110/`；本目录包含原失败日志、最终日志、实际EXE报告、构建和文件哈希。

业务端等当前任务安全结束后正常退出旧版，再运行本次完整交付；无需初始化数据或重新绑定账号。所有旧版本及独立删除性能工作区保留。
'''
    (release/'交付报告.md').write_text(report,'utf-8');shutil.copyfile(release/'交付报告.md',delivery/'交付报告.md')
    print(json.dumps({'release':str(release),'artifacts':artifacts,'validation':validation},ensure_ascii=False))


if __name__=='__main__':main()
