"""Seal the independently built 0.45.1 deletion candidate and measured evidence."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/deletion_0451'
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8')
def main():
    release=ROOT/'releases/0.45.1/rc1';delivery=ROOT/'交付_0.45.1_rc1/补货助手'
    frozen=read(QA/'frozen.json');small=read(QA/'synthetic-release/report.json');real=read(QA/'real-warm-release/report.json')
    ui=read(QA/'exe/deletion-report.json');build=read(ROOT/'build_0.45.1_rc1/build_info.json')
    for r in (small,real):
        if not r['passed'] or r['differences'] or len(r['runs'])!=6:raise ValueError('Comparison incomplete')
    for p,h in frozen['protected'].items():
        if sha(Path(p))!=h:raise ValueError('Protected input/code/package changed: '+p)
    regression=(QA/'regression.txt').read_text('utf-8-sig');check=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not check or int(check[1])<1000:raise ValueError('Full regression not passed')
    for name,count in [('targeted-deletion.txt',40),('targeted-maintenance.txt',10)]:
        if not re.search(r'Ran '+str(count)+r' tests in [\d.]+s\s+OK\s*$',(QA/name).read_text('utf-8-sig')):raise ValueError(name)
    if not ui['passed'] or not ui['frozen_exe'] or ui['process_exit'] or ui['source_commit']!=build['source_commit'] or ui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('Actual EXE not accepted')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source differs from build')
    release.mkdir(parents=True,exist_ok=False)
    for a,b in {'frozen.json':'frozen.json','synthetic-release/report.json':'synthetic-performance.json','real-warm-release/report.json':'historical-performance.json','regression.txt':'regression.txt','targeted-deletion.txt':'deletion-tests.txt','targeted-maintenance.txt':'maintenance-tests.txt','exe/deletion-report.json':'exe-report.json'}.items():shutil.copyfile(QA/a,release/b)
    shutil.copyfile(ROOT/'build_0.45.1_rc1/build_info.json',release/'build_info.json')
    for p in (QA/'exe').glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()};target=delivery.parent/'补货助手_0.45.1_rc1_删除负载优化.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if len(z.namelist())!=len(files):raise ValueError('File count changed')
        for name,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=h:raise ValueError(name)
    manifest={'version':'0.45.1','build_id':'rc1','source_commit':build['source_commit'],'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    save(release/'manifest.json',manifest)
    rows=[]
    for name,r in [('合成首次引用核验',small),('真实冻结历史／已有引用缓存',real)]:
        for side,title in [('before','修改前'),('after','修改后')]:
            s=r['summary'][side];t=s['total_seconds'];mem=s['peak_rss'];read_bytes=s['read_bytes']['median'];write_bytes=s['write_bytes']['median']
            rows.append(f"| {name}／{title} | {t['median']:.2f}（{t['min']:.2f}～{t['max']:.2f}） | {mem['median']/2**20:.1f}（{mem['min']/2**20:.1f}～{mem['max']/2**20:.1f}） | {read_bytes/2**20:.1f} | {write_bytes/2**20:.1f} |")
    text=f'''# 0.45.1 删除负载优化交付

固定基线：{frozen['commit']}。分支perf/deletion-load-0.45.1。构建源码：{build['source_commit']}。本轮本地修改、验证、打包，不推送、不合并；保留0.45.0 rc1及其哈希。

## 改动与边界

关联校验按原JSON格式逐条累计，预览token不变；旧作业逐份读取、用固定大小的SHA256描述原内容；写锁内一次比较源记录集合及内容，原逻辑摘要复用；同次已核验引用只在main.data_version未变时复用，有任何并发提交即重新核对；缓存只写差异；生成任务逐条读取；索引明细分段扫描并完整验证UTF-8。编辑器减少空查临时库，临时引用读取使用只读事务。

仅修改data_maintenance.py和maintenance_storage.py的执行方式；版本/说明及交付脚本随之更新。模型、预算、箱规、签发、归属及默认配置未改，模型0.3.4-rules。删除仍在一笔事务内，取消/异常整体回滚；不延后回收、不降低secure_delete、不做VACUUM、不接触平台或用户业务数据。

## 同机新旧对照

| 场景 | 总耗时中位数及范围（秒） | 工作进程峰值RSS中位数及范围（MiB） | 读取中位数（MiB） | 写入中位数（MiB） |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

各场景新旧交替串行三轮，固定输入、相同Python和依赖。计时包含预览和删除，含临时准备、核验、提交，排除QA副本复制与对照签名生成。RSS每30ms采样单个执行进程，不是整个GUI或整机；Windows进程I/O计数包含系统缓存，并非物理盘读写或磁盘占用率。原始每轮与分阶段数据随报告保存。合成样本为专门构造的多旧修订/任务负载，不冒充真实门店。

真实样本为早期隔离冻结历史（约1GB主库及约0.76GB索引），原件未改。预先带入同一份已核验引用缓存，不能据此推断首次无缓存的大库耗时；本轮未重新跑大库首次无缓存测试。没有在低配置业务电脑上实测，不保证任务管理器峰值降低相同比例。大量旧格式历史首次解析、事务安全日志及安全删除仍有开销。

## 等价及故障验证

- 两组各三轮：预览token、删除结果、逐表有序且带类型的业务签名一致，差异0；检查主库、翱象任务库、归档索引及生命周期索引。
- 原有对照规则仅排除jobs.revision/saved两个新产生的动态字段，分块与维护缓存物理布局独立验证。翱象删除事件使用两端相同固定时钟1793923200.0，事件字段不忽略。第一轮探索对照只有ax_workflows.payload.updated_at和新删除事件payload.at不同，原因是两次实际执行时刻不同；保留探索报告，最终对照冻结时钟，不扩大忽略范围。
- 40项删除专项与10项数据维护测试通过，含同店双仓、未选订单/队列、共享原表、写锁等待取消、准备取消重试、提交边界、跨库回滚、准备后变更、并发破坏内容块拒绝、损坏UTF-8、相同缓存不重写。
- 全量{check[1]}项回归通过（{check[2]}秒）。核心八文件及旧EXE/ZIP哈希相同；没有因性能调整业务预期。
- 回归日志含既有browser_status_ui控件销毁后QLabel回调警告；releases/0.44.0/rc2/regression.txt已有同一堆栈。本轮没有修改该模块，不宣称已修复该独立遗留项。
- 实际打包EXE通过{len(ui['checks'])}项验收：两店spawn计算/保存/签发，取消预览、关闭删除进度安全取消、重试删除、旧格式解析器、结束所选本地准备、不影响未选已排队任务、未选店签发/计算值保持、重复删除拒绝、页面刷新。EXE报告另列心跳，整段含同步验收取样，不能当作仅删除阶段的响应上限。

## 交付与复跑

使用入口不变。方法、边界和命令见docs/0.45.1删除负载优化.md；完整隔离证据qa/deletion_0451，提交证据在本目录。此次不重复运行平台自动化，不把源码测试称为EXE测试，不修改旧验收标签。

EXE：{delivery}/补货助手.exe

ZIP：{target}

EXE SHA256：{manifest['exe_sha256']}

ZIP SHA256：{manifest['zip_sha256']}

ZIP共{len(files)}个文件逐一核验。证据提交在构建之后，构建源码以build_info.json为准。
'''
    (release/'交付报告.md').write_text(text,'utf-8');print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256'],'files':len(files)},ensure_ascii=False))
if __name__=='__main__':main()
