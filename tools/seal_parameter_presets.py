"""Seal this candidate only after strict regression and actual EXE acceptance."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,data):p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')

def main():
    qa=ROOT/'qa/parameter_presets_0410';release=ROOT/'releases/0.41.0/rc1';delivery=ROOT/'交付_0.41.0_rc1/补货助手'
    build=read(ROOT/'build_0.41.0_rc1/build_info.json');comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json');gui=read(qa/'exe-ui/report.json')
    regression=(qa/'regression-final.txt').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<910:raise ValueError('Final regression not passed')
    if not comparison['passed'] or comparison['differences'] or comparison['additional_ignored_fields']:raise ValueError('Strict comparison failed')
    if not gui['passed'] or not gui['frozen_exe'] or gui['exit_code']:raise ValueError('EXE acceptance failed')
    if gui['source_commit']!=build['source_commit'] or gui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE/source mismatch')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Input or old release changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    release.mkdir(parents=True,exist_ok=False)
    for src,dst in [(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'regression-final.txt','regression.txt'),(qa/'targeted-final2.txt','targeted.txt'),(qa/'exe-ui/report.json','exe-report.json')]:shutil.copyfile(src,release/dst)
    shutil.copyfile(ROOT/'build_0.41.0_rc1/build_info.json',release/'build_info.json')
    for p in (qa/'exe-ui').glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.41.0_rc1_门店参数预案.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    # Verify every member rather than only accepting ZIP construction success.
    with zipfile.ZipFile(target) as z:
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP content mismatch: '+rel)
    manifest={'source_commit':build['source_commit'],'version':'0.41.0','build_id':'rc1','exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    save(release/'manifest.json',manifest)
    report=f'''# 0.41.0 rc1 门店参数预案

基线 main：620351285423e9017af1ee9edd7829d435836eae（0.40.4）。独立分支 feat/store-parameter-presets-0.41.0。
构建源码：{build['source_commit']}。从固定Git归档构建，无工作区叠加。证据提交与标签是后续提交，不混称构建提交。未推送、未合并。

交付：{delivery}/补货助手.exe
压缩包：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}。共{len(files)}个文件，ZIP内容逐一验证。

## 实现

完整参数预案库；系统默认只读不可删除；门店单选/批选默认预案；补货预案逐店跟随默认或指定完整参数，保留门店顺序。参数库编辑更新未来引用，删除列出引用并要求替换、同事务提交。保存或删除不改当前和历史快照。旧补货预案保持旧字段规则，可显式转换。定时、AI查询/试算共用配置装配；默认直接改值自动变为独立门店默认。

模型0.3.4-rules、仓归属0.5.1、参数格式3、作业格式1、存储格式2未变。核心模型、预算、一箱例外、引擎、默认业务配置、签发导出及聚水潭分配哈希与基线一致。

## 验证

- 最终全量{m[1]}项回归通过，{m[2]}秒；新增13项覆盖预案引用、事务竞争、默认解绑、删除后历史恢复、逐店顺序、定时冻结、AI装配、真实计算结果。
- 同一冻结输入独立运行修改前后：33份业务对象、5份Excel，共38份严格对照差异0。包含14,244行真实样本、多预算、自定义参数、单双仓及异常。值、类型和顺序不放宽，未增加忽略字段；精确白名单见equivalence.json。
- 实际EXE执行：参数表编辑并保存、批量选择默认且当前不变、逐店选择不同参数和顺序、采用、草稿保存、删除引用替换、恢复历史参数、恢复后计算参数装配；本链未捕获Qt异常0。截图与exe-report.json为本次实际程序输出。
- 未改日常用户数据库或原Excel，未连接真实平台。旧0.40.4 EXE/ZIP和冻结输入哈希复核不变。

## 边界

旧数据不自动重配参数。跟随命名预案仅影响后续采用；已采用/恢复的本次快照不重新解析。删除只移除可选预案及替换未来引用，历史版本仍可解释；不级联删除门店或订单。

没有重新进行真实定时平台取数和Codex/WorkBuddy自然语言联调；相关装配路径使用隔离数据自动验证。不宣称新机器可复现构建或性能提升。
全量回归中仍可能出现先前已记录的其他页面QLabel销毁回调告警，本轮实际参数链没有该异常，未夹带全局Qt重构。
早期新测试夹具比较内存元组与既有JSON列表、以及两次不同业务日期，修正夹具输入后严格比较通过；首次源码UI脚本未实际点击确认按钮，修正驱动后通过，失败证据保留在QA。

复跑命令与功能边界见 docs/0.41.0门店参数预案.md。完整本轮目录 qa/parameter_presets_0410，选定证据随本报告保存。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n')
    print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
