"""Seal this tested local candidate; refuses failed evidence or existing ZIP."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),'utf-8')

def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--delivery',type=Path,required=True);p.add_argument('--release',type=Path,required=True);a=p.parse_args()
    qa=a.qa.resolve();delivery=a.delivery.resolve();release=a.release.resolve()
    comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json');exe=read(qa/'exe/deletion-report.json');build=read(delivery/'_internal/build_info.json')
    log=(qa/'all-tests.txt').read_text('utf-8');matched=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',log)
    if not matched or int(matched[1])<884:raise ValueError('Full regression missing or failed')
    if not comparison['passed'] or len(comparison['cases'])!=38 or comparison['differences']:raise ValueError('Strict comparison failed')
    if not exe['passed'] or not exe['frozen_exe'] or exe['process_exit']:raise ValueError('Actual EXE failed')
    if exe['source_commit']!=build['source_commit'] or build['version']!='0.40.2':raise ValueError('Build evidence mismatch')
    if exe['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE changed')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Protected input/release changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Application source changed after build')
    release.mkdir(parents=True,exist_ok=False)
    for source,target in [(qa/'all-tests.txt','regression.txt'),(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'exe/deletion-report.json','exe-report.json'),(qa/'exe/ui-trace.jsonl','ui-trace.txt')]:shutil.copy2(source,release/target)
    for name in ('local-failure-queue.png','task-blockers.png','delete-preview.png','after-delete.png'):shutil.copy2(qa/'exe'/name,release/name)
    files={str(p.relative_to(delivery)).replace('\\','/'):sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.40.2_rc1_删除与任务修复.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        if len(z.namelist())!=len(files):raise ValueError('ZIP file count changed')
        for rel,digest in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=digest:raise ValueError('ZIP content changed')
    manifest={'build':build,'files':files,'file_count':len(files),'exe_sha256':exe['exe_sha256'],'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'protected_unchanged':True}
    save(release/'封存信息.json',manifest)
    report=f'''# 0.40.2 rc1 删除与任务状态修复交付

- 本地分支：`fix/order-deletion-workflow-0.40.2`。未推送、未合并。
- 基线：`{comparison['baseline_commit']}`（0.40.1）。构建源码：`{build['source_commit']}`，固定Git归档构建，无工作区叠加。
- 程序：`{delivery/'补货助手.exe'}`。
- ZIP：`{target}`；{len(files)}个文件，已逐文件校验ZIP内容。
- EXE SHA256：`{exe['exe_sha256']}`。
- ZIP SHA256：`{manifest['zip_sha256']}`。
- 模型0.3.4-rules、仓归属0.5.1、存储格式2不变。0.40.1及旧发行包保留。

## 修复与使用

本地付款检查失败、尚无采购预览或采购身份的历史任务，不再永久挡住删除。仍在排队的本地准备可在完整阻塞表中明确结束所选项；未选项保持。运行中先安全停止，上传结果不明仍需核对原任务，不能因为浏览器断开而清除受理不明证据。

采购预览和失败原因分开；没有预览时按钮禁用，不再引导到错误的收货页。进度显示已检查数和成功数，100%检查不代表成功。部分删除后剩余状态、计数和位置一致；勾选按订单身份保留。打开原任务先释放删除维护暂停，避免任务无法继续。

仍按原门店当天父单范围删除，包括关联双仓，确认页明确列出。没有扩大到未选门店。任务结束与订单删除分别确认：结束后取消删除不会自动恢复任务，也不会删除订单。具体操作见 `docs/0.40.2删除与任务状态.md`。

## 本轮验证

- 全量{matched[1]}项回归通过，{matched[2]}秒。新增13项聚焦本地失败删除、未知受理保留、全部阻塞显示、精确结束范围、并发预览拒绝、部分批次状态、取消/跨库失败回滚、勾选身份及维护退出。
- 基线与候选分别运行：14,244行真实冻结样本、多预算/关闭例外、自定义参数、单双仓、异常及冲突；33份有序业务记录和5份Excel，共38份严格对照，差异0。不重排、不放宽容差；既有精确时钟/进程/应用构建白名单见equivalence.json，新增忽略字段为空。
- 核心模型、预算、一箱例外、引擎、默认配置、签发导出和聚水潭分配文件SHA一致；原始输入和旧EXE/ZIP保护哈希复核无变化。
- 隔离双店双仓验证未选店签发快照、当前计算结果、未选任务的全部业务字段一致；删除后内部位置仅连续重编号。跨库失败整体回滚，预览后任务启动拒绝删除，取消后重试成功。
- 实际打包EXE（frozen_exe=true）完成Windows spawn两店计算、保存签发；失败原因/预览控件检查；阻塞表取消、确认结束所选本地准备；删除进度关闭取消；再次删除成功；保留未选店签发、当前计算、排队项；列表刷新与重复删除错误返回。完整操作见exe-report.json及截图。测试未连接平台。
- EXE全走查{exe['seconds']:.2f}秒；25ms界面心跳{exe['heartbeat_ticks']}次，最大间隔{exe['max_gap_seconds']:.3f}秒，包含同步断言及截图。这是合成小样本自动Qt操作，不代表真实大库删除耗时或人工验收。

## 限制与保留证据

没有替用户清理真实订单、没有操作或撤销平台采购。外部防重复身份保留；部分删除保留共享批次事件，全部删除清理该本地批次事件。原磁盘Excel、导出件、旧备份保留。历史大库共享引用检查仍需时间，不宣称整体删除提速；没有重新做大库性能、新电脑或真实平台联调。

全量回归仍出现历史QLabel销毁后回调告警（旧0.40.0/0.40.1日志已有），不是本次修复目标；最终EXE本链验收通过，不宣称历史告警已消除。

新增UI测试首次误用了只生成单项的夹具，改为三项夹具后通过；未改变业务预期。增强源码走查第二次因测试控制器没有真正点击确认按钮而循环，已停止该隔离QA进程，改用实际按钮点击后源码与EXE通过；失败证据保留在qa目录，不当作产品通过记录。

## 复跑与证据

使用 `.venv/Scripts/python.exe`，命令见 `docs/0.40.2删除与任务状态.md`。本轮完整本地目录 `qa/deletion_workflows_0402`，可移交证据在本目录。`tools/verify_deletion_workflows.py` 支持固定源码对照与独立EXE验收；`tools/seal_deletion_workflows.py` 校验通过证据、保护哈希、构建提交和ZIP，不覆盖已有交付。

构建命令：`.venv/Scripts/python.exe tools/build_from_commit.py --commit {build['source_commit']} --build-id 新编号`。最终证据提交和标签指向包含报告的后续提交，不能与构建源码提交混称。
'''
    (release/'交付报告.md').write_text(report,'utf-8')
    print(json.dumps({'zip':str(target),'exe_sha256':exe['exe_sha256'],'zip_sha256':manifest['zip_sha256']},ensure_ascii=False))

if __name__=='__main__':main()
