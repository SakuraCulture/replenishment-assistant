"""Seal 0.40.3 only after independent comparison, regression and frozen UI pass."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8')

def main():
    qa=ROOT/'qa/gui_maintenance_0403';release=ROOT/'releases/0.40.3/rc1';delivery=ROOT/'交付_0.40.3_rc1/补货助手'
    build=read(ROOT/'build_0.40.3_rc1/build_info.json');comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json')
    gui=read(qa/'gui-exe/gui-report.json');deletion=read(qa/'deletion-exe/deletion-report.json')
    regression=(qa/'all-tests-final.txt').read_text('utf-8-sig');matched=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not matched or int(matched[1])!=892:raise ValueError('Final regression not passed')
    if not comparison['passed'] or comparison['differences'] or comparison['changed_protected']:raise ValueError('Business comparison failed')
    for report in (gui,deletion):
        if not report['passed'] or not report['frozen_exe'] or report['process_exit']:raise ValueError('Frozen UI failed')
        if report['source_commit']!=build['source_commit'] or report['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('Frozen evidence mismatch')
    if build['version']!='0.40.3' or gui['errors']:raise ValueError('Wrong version or GUI exception')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Protected source/release changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    release.mkdir(parents=True,exist_ok=False)
    for source,target in [('all-tests-final.txt','regression.txt'),('targeted-final2.txt','targeted.txt'),('comparison/equivalence.json','equivalence.json'),('comparison/frozen.json','frozen.json'),('before-final.json','reproduction-before.json'),('after-final.json','reproduction-after.json'),('gui-exe/gui-report.json','gui-exe-report.json'),('deletion-exe/deletion-report.json','deletion-exe-report.json')]:shutil.copy2(qa/source,release/target)
    for folder,names in [('gui-exe',['cancel-confirmation.png','diff-confirmation.png','confirm-confirmation.png','export-result.png']),('deletion-exe',['delete-preview.png','after-delete.png'])]:
        for name in names:shutil.copy2(qa/folder/name,release/name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.40.3_rc1_签发弹窗与轮询修复.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in files:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        if len(z.namelist())!=len(files):raise ValueError('ZIP count mismatch')
        for name,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+name)).hexdigest()!=h:raise ValueError('ZIP hash mismatch')
    fixtures={p.relative_to(ROOT).as_posix():sha(p) for p in (ROOT/'qa/deletion_0321/inputs').iterdir() if p.is_file()}
    manifest={'build':build,'files':files,'file_count':len(files),'exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'ui_fixture_sha256':fixtures,'protected_unchanged':True}
    save(release/'封存信息.json',manifest)
    report=f'''# 0.40.3 rc1 签发弹窗与删除轮询修复

基线：`{comparison['baseline_commit']}`（0.40.2）；独立分支 `fix/gui-maintenance-callbacks-0.40.3`。
构建源码：`{build['source_commit']}`。由固定 Git 归档构建，无工作区叠加；证据提交和标签为后续提交。未推送、未合并。

程序：`{delivery/'补货助手.exe'}`。
压缩包：`{target}`。共 {len(files)} 个文件，ZIP 已逐文件校验。
EXE SHA256：`{manifest['exe_sha256']}`。
ZIP SHA256：`{manifest['zip_sha256']}`。
模型 0.3.4-rules、仓归属 0.5.1、存储格式 2 均未改变；旧 0.40.2 EXE/ZIP 哈希不变。

## 修复

签发确认页的说明更新会销毁旧 QGridLayout，原回调仍引用它；隔离复现错误与截图一致。现在回调按弹窗生命周期重新获取当前布局，并避免对弹窗/内部布局的反向持有。取消、搜索、默认按钮、差异导出后再次确认均保留。

定时轮询的数据库存在性查询原在 GUI 线程，遇删除独占锁会抛出未捕获异常。现在完整检查在后台执行，保存/签发/维护期间避让，瞬时锁冲突只显示等待并在后续轮询重查。不重新执行签发/删除。即使旧轮询已经开始，其完成回调也会再次检查维护状态，不趁维护启动新作业。反向增加定时执行/资料采用期间的删除保护。

应用日志固定写入带时间的 app.log；隔离程序验证文件日志确实产生。用户当时的日志为空，因此不能从旧日志证明真实签发或删除事务的最终状态，也未擅自再次处理真实订单。

## 本轮验证

- 最终全量 {matched[1]} 项回归通过，{matched[2]} 秒；新增 8 项覆盖说明重建布局、完整差异反复打开、控件销毁、无布局/父弹窗引用循环、数据库独占、维护避让、任务反向互斥及已开始轮询的完成回调。
- 0.40.2 与候选使用同一冻结输入独立运行，33 份有序业务记录、5 份 Excel，共38份对照差异0。包含14,244行真实样本、多预算、关闭一箱例外、自定义参数、单双仓及异常/冲突。全部业务值、类型、顺序及Excel结构/样式按既有严格口径比较，新增忽略字段为空；精确动态字段白名单见equivalence.json。
- 核心模型、预算、一箱例外、引擎、默认配置、签发导出、聚水潭分配源码哈希一致；冻结输入及旧包哈希复核一致。
- 实际 EXE 签发链：两店双仓计算 → 签发核对取消（无签发）→ 再次进入 → 导出完整差异 → 同一弹窗再次确认 → 签发 V001 → 生成4份文件；原当前计算结果完整相等，确认表和按钮可见、默认取消。GUI未捕获异常0。
- 实际 EXE 独占锁：维护期间不发查询；非维护的外部独占由后台返回“等待数据库释放”，界面心跳继续，释放后重新查询成功；不创建定时业务任务。
- 实际 EXE 删除：本地失败任务查看、结束所选准备、取消删除、重试成功、剩余门店计算/签发及未选批次项保持、页面刷新、重复已删除对象拒绝。详见 deletion-exe-report.json。
- 实际签发走查 {gui['seconds']:.2f} 秒，25ms心跳最大间隔 {gui['max_gap_seconds']:.3f} 秒；实际删除走查 {deletion['seconds']:.2f} 秒，最大间隔 {deletion['max_gap_seconds']:.3f} 秒。含断言/截图的小样本记录，不是性能前后对比，不代表真实大库耗时。

## 限制和失败证据

不改变签发或删除事务、计算/预算、平台订单和用户资料。没有替用户删除、签发、重试或连接平台；真实旧弹窗后的业务状态须在订单跟踪核对。其他软件长期持锁仍会阻塞真正的写操作，本次不隐藏这些异常，不强杀持锁进程。

全量回归中仍有既存 QLabel 销毁后回调告警（历史验收已记录），不属于本次 QGridLayout 修复；实际本链 EXE 验收无该异常，不宣称全软件所有 Qt 回调问题已清除。

首次定向测试的临时 SQLite 连接未显式关闭导致夹具清理失败，已修复测试资源清理。首个修复候选在实际签发取消后再进入时出现原生访问异常，未交付；移除观察器对父弹窗及私有布局的持有后，源码和实际EXE通过，并增加生命周期回归。失败输出保留在 qa/gui_maintenance_0403/source-ui 和 source-ui-r2，不用失败候选充当通过证据。

## 复跑

命令与边界见 `docs/0.40.3签发弹窗与删除轮询修复.md`。本轮完整 QA 为 `qa/gui_maintenance_0403`；定向测试自建合成临时数据，真实冻结业务输入不随Git分发。`tools/reproduce_gui_callbacks.py` 可对固定基线/候选分别复现；`tools/verify_gui_maintenance.py` 运行实际页面；`tools/verify_deletion_workflows.py` 运行计算/Excel对照和删除EXE验收。

构建：`.venv/Scripts/python.exe tools/build_from_commit.py --commit {build['source_commit']} --build-id 新编号`。封存：`.venv/Scripts/python.exe tools/seal_gui_maintenance.py`（不得覆盖已有封存目录）。
'''
    (release/'交付报告.md').write_text(report,'utf-8');print(json.dumps({'zip':str(target),'exe_sha256':manifest['exe_sha256'],'zip_sha256':manifest['zip_sha256']},ensure_ascii=False))

if __name__=='__main__':main()
