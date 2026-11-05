"""Seal 0.41.2 only with current strict comparison and actual EXE evidence."""
import re,shutil,subprocess,zipfile,hashlib,json
from pathlib import Path
from seal_store_defaults import ROOT,read,sha,save

def main():
    qa=ROOT/'qa/store_config_deletion_0412';release=ROOT/'releases/0.41.2/rc1';delivery=ROOT/'交付_0.41.2_rc1/补货助手'
    build_path=ROOT/'build_0.41.2_rc1/build_info.json';build=read(build_path);eq=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json');gui=read(qa/'exe-ui/report.json')
    m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',(qa/'regression.txt').read_text('utf-8-sig'))
    if not m or int(m[1])<924:raise ValueError('Regression not passed')
    if not eq['passed'] or eq['differences'] or eq['additional_ignored_fields']:raise ValueError('Strict comparison failed')
    if not gui['passed'] or not gui['frozen_exe'] or gui['exit_code'] or gui['version']!='0.41.2':raise ValueError('EXE acceptance failed')
    if gui['source_commit']!=build['source_commit'] or gui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE/source mismatch')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Protected files changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    release.mkdir(parents=True,exist_ok=False)
    for src,name in [(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'regression.txt','regression.txt'),(qa/'targeted.txt','targeted.txt'),(qa/'exe-ui/report.json','exe-report.json'),(build_path,'build_info.json')]:
        (release/name).write_text(src.read_text('utf-8-sig'),encoding='utf-8',newline='\n')
    for p in (qa/'exe-ui').glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()};target=delivery.parent/'补货助手_0.41.2_rc1_整条门店配置删除.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP member mismatch: '+rel)
    manifest={'version':'0.41.2','build_id':'rc1','source_commit':build['source_commit'],'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files};save(release/'manifest.json',manifest)
    report=f'''# 0.41.2 rc1 整条门店配置删除

基线522cb62（0.41.1），独立分支fix/delete-store-config-records-0.41.2。构建源码{build['source_commit']}；固定Git归档，无工作区叠加。后续证据提交/标签不混称构建源码。未推送、未合并。

## 修改

删除所选门店整个stores记录，包括参数、策略、绑定、主从仓和预算；0.41.1残留空参数记录也可删。列表默认只看已保存配置，当前临时作业独立查看。整批事务、预览后版本核对、失败回滚及安全备份沿用。利用既有删除审计避免旧JSON仓/预算记忆在新作业中复活；重新保存优先，历史恢复保持原快照。不新增回收站。

未选门店、目录、资质/拒收、公共预案、补货预案名单、当前作业、历史订单、计算及导出规则不改。没有执行真实门店数据删除。模型0.3.4-rules、仓归属0.5.1和格式版本不变。

## 验证

- {m[1]}项全量回归通过，{m[2]}秒；本轮新增5项，删除相关14项通过。覆盖残留配置、重复、并发核对、故障回滚、取消、当前和历史隔离、共享预案/映射不变、旧仓预算不回填及重新保存生效。
- 固定相同输入独立运行基线和候选，33份业务对象+5份Excel严格对照差异0。包含14,244行真实样本及原有多预算/仓路由/异常场景，不新增忽略字段、不重排值。精确白名单和核心文件哈希见equivalence.json。
- 实际EXE完成{len(gui['checks'])}项参数流程检查，见exe-report.json和截图；包括整行从列表消失、当前作业单独查看、旧残留清理、新作业内置参数填充、旧JSON不复活仓预算、历史恢复与参数装配。该链无未捕获Qt异常。
- 冻结输入及0.41.1 EXE/ZIP哈希复核未变。全部测试写独立QA，不触碰日常用户资料。全量日志含其他页面既有QLabel回调告警，未夹带全局Qt修改。

## 交付

EXE：{delivery}/补货助手.exe
ZIP：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}
共{len(files)}个文件，ZIP每项哈希均核对。

0.41.1及更早包保留。旧版不理解本次删除后的兼容处理，回退请使用删除前备份副本。既有审计/备份仍可明确恢复，不在有效配置列表保留占位。无真实平台、AI客户端联调或性能提升声明。

使用与复跑：docs/0.41.2整条门店配置删除.md；本轮QA：qa/store_config_deletion_0412。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n');print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256']},ensure_ascii=False))

if __name__=='__main__':main()
