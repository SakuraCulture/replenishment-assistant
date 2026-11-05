"""Seal the 0.41.1 candidate using this run's strict and frozen-EXE evidence."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,value):p.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')

def main():
    qa=ROOT/'qa/store_default_deletion_0411';release=ROOT/'releases/0.41.1/rc1';delivery=ROOT/'交付_0.41.1_rc1/补货助手'
    build=read(ROOT/'build_0.41.1_rc1/build_info.json');comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json');gui=read(qa/'exe-ui/report.json')
    regression=(qa/'regression.txt').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<919:raise ValueError('Regression not passed')
    if not comparison['passed'] or comparison['differences'] or comparison['additional_ignored_fields']:raise ValueError('Strict comparison failed')
    if not gui['passed'] or not gui['frozen_exe'] or gui['exit_code']:raise ValueError('EXE acceptance failed')
    if gui['source_commit']!=build['source_commit'] or gui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE/source mismatch')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Input or old release changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    release.mkdir(parents=True,exist_ok=False)
    for src,dst in [(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'regression.txt','regression.txt'),(qa/'targeted.txt','targeted.txt'),(qa/'exe-ui/report.json','exe-report.json'),(ROOT/'build_0.41.1_rc1/build_info.json','build_info.json')]:
        (release/dst).write_text(src.read_text('utf-8-sig'),encoding='utf-8',newline='\n')
    for p in (qa/'exe-ui').glob('*.png'):shutil.copyfile(p,release/p.name)
    (release/'regression-initial.txt').write_text((qa/'regression-initial.txt').read_text('utf-8-sig'),encoding='utf-8',newline='\n')
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.41.1_rc1_门店默认参数删除.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP content mismatch: '+rel)
    manifest={'source_commit':build['source_commit'],'version':'0.41.1','build_id':'rc1','exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    save(release/'manifest.json',manifest)
    report=f'''# 0.41.1 rc1 门店默认参数删除

基线：f4a6120（0.41.0候选）。独立分支fix/removable-store-defaults-0.41.1。
构建源码：{build['source_commit']}。固定Git归档，无工作区叠加。证据提交和标签是后续提交；未推送、未合并。

交付：{delivery}/补货助手.exe
压缩包：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}。共{len(files)}个文件，ZIP内容逐个哈希验证。

## 完成

门店默认参数可单选/多选删除，独立按钮及默认取消的核对弹窗。只移除参数载荷和默认预案绑定，供货仓、预算及未选门店保留。首次或下次新作业准备自动填入内置参数，未明确保存则不写回门店默认；参数库及选择器不展示系统内置参数。明确指定其他参数预案仍优先，跟随门店默认才使用兜底。当前及历史参数快照不变。全局分类映射保留；旧统一映射必要时等值登记到独立映射存储。原安全备份和变更记录保留。

## 验证

- 全量{m[1]}项通过，{m[2]}秒；新增9项覆盖删除边界、竞争/重复、首次自动填写、参数重新保存、参数预案引用、取消、历史恢复与映射保留。
- 初次全量有1项旧界面测试失败：它依赖已隐藏的内置预案选项。改为创建同值自建预案，仍检查取消保持22、确认载入21，数值预期未改；初次日志随附，最终重新完整执行。
- 基线与候选独立运行同一冻结输入，33份业务对象和5份Excel严格对照，差异0；含14,244行真实样本和已有预算/仓路由/异常场景。值、类型、顺序不放宽，新增忽略字段0，精确白名单及核心文件哈希见equivalence.json。
- 实际EXE通过{len(gui['checks'])}项参数界面链检查，包括新建预案、批选默认、逐店补货预案、删除所选默认、下一次自动填入、不重新持久化、历史草稿恢复和计算参数装配；详见exe-report.json与截图。
- 原始输入及0.41.0 EXE/ZIP哈希复核不变；测试只写隔离QA资料，没有删除真实门店默认。模型0.3.4-rules、仓归属0.5.1及默认业务配置未改变。

## 边界

旧数据由新版兼容读取。旧EXE不理解删除后的仅路由/预算记录，保留旧包不代表可直接用旧程序打开已删除默认的新资料；回退应使用删除前备份副本。历史预案绑定旧内置配置保留数值，不自动转换。
本轮没有真实平台、定时取数或AI客户端自然语言联调，不宣称性能提升或换机可复现构建。全量回归中其他页面既有QLabel销毁回调告警另留日志；本次实际参数EXE链无未捕获Qt异常，未扩大为全局Qt重构。

使用与复跑：docs/0.41.1门店默认参数删除.md。完整本次QA：qa/store_default_deletion_0411。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n')
    print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256']},ensure_ascii=False))

if __name__=='__main__':main()
