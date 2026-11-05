"""Seal 0.42.0 only with fresh regression, strict comparison and actual EXE reports."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,data):p.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8')
def main():
    qa=ROOT/'qa/parameter_quick_0420';release=ROOT/'releases/0.42.0/rc1';delivery=ROOT/'交付_0.42.0_rc1/补货助手'
    build=read(ROOT/'build_0.42.0_rc1/build_info.json');comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json')
    gui=read(qa/'exe-ui/report.json');existing=read(qa/'existing-exe-ui/report.json')
    regression=(qa/'regression-final.txt').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<943:raise ValueError('Final regression not passed')
    if not comparison['passed'] or comparison['differences'] or comparison['additional_ignored_fields']:raise ValueError('Strict comparison failed')
    for report in (gui,existing):
        if not report['passed'] or not report['frozen_exe'] or report['exit_code']:raise ValueError('Actual EXE acceptance failed')
        if report['source_commit']!=build['source_commit'] or report['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('EXE/source mismatch')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Input or old release changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    release.mkdir(parents=True,exist_ok=False)
    for src,dst in [(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'regression-final.txt','regression.txt'),(qa/'targeted.txt','targeted.txt'),(qa/'exe-ui/report.json','exe-report.json'),(qa/'existing-exe-ui/report.json','existing-exe-report.json')]:shutil.copyfile(src,release/dst)
    shutil.copyfile(ROOT/'build_0.42.0_rc1/build_info.json',release/'build_info.json')
    for p in (qa/'exe-ui').glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.42.0_rc1_参数快捷设置.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP content mismatch: '+rel)
    manifest={'source_commit':build['source_commit'],'version':'0.42.0','build_id':'rc1','exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    save(release/'manifest.json',manifest)
    report=f'''# 0.42.0 rc1 参数快捷设置

基线da3546a（0.41.2），分支feat/parameter-quick-actions-0.42.0。构建源码{build['source_commit']}；固定Git归档构建。后续证据提交和标签不混称构建源码。未推送、未合并。

## 修改

参数设置和计算准备直接批量选择一箱例外、极端缺货恢复、新品铺设、预算与供货关系；未选项各店保留。分组模板增加下拉、必填批注及输入提示，表头和数据行不改，V2/V3/旧混合模板的空白规则保持。限定预算缺有效金额给出明确错误。已有方案显示摘要及逐店差异，应用到默认或本次；本次可明确同时保存未来默认。预算、主从仓与映射分别保留，预览变化及保存失败不采用，映射不兼容整批拒绝。

模型0.3.4-rules、仓归属0.5.1及参数/存储格式不变。全局配置、资质拒收、原签发和历史作业不改。没有实际修改用户门店配置。

## 验证

- 最终全量{m[1]}项回归通过，{m[2]}秒；新增19项定向回归涵盖预算条件、0/空白、非法粘贴、旧模板空白语义、默认/当前隔离、批量差异保留、并发预览、失败不采用与映射兼容。
- 相同冻结输入独立运行修改前后，33份业务对象、5份Excel共38份严格比较差异0；含14,244行真实样本及原多预算、仓和异常场景。值、类型与顺序不放宽，不新增白名单。核心计算与签发导出文件哈希一致。
- 实际EXE快捷流程{len(gui['checks'])}项、原参数预案/删除/恢复流程{len(existing['checks'])}项检查通过，构建提交及EXE哈希分别核对。新增链含取消、输入校验、默认与当前采用、显式双保存、模板生成/解析/提交、草稿恢复。模板填写测试使用openpyxl填入合成行，没有以WPS/Excel鼠标打开下拉作人工验证。
- 原始输入与0.41.2 EXE/ZIP哈希未变。未连接真实平台或AI客户端，不宣称计算性能或真实人员操作耗时改善。
- 全量回归仍有既有browser_status_ui中已销毁QLabel回调告警；本轮两条实际EXE参数流程未捕获异常为0。未夹带全局浏览器状态修复。
- 首轮新增测试的夹具将数据append到模板预格式化行之后，且误写内置山东到货预期4天（实际5天）；改为明确写第2行并按原内置5天校验。原有测试预期及业务默认未改。失败输出保留于本轮作业工具记录。

## 交付

EXE：{delivery}/补货助手.exe
ZIP：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}；{len(files)}个文件逐项核对ZIP内容。

保留0.41.2及旧包。本轮没有重构全部参数页面或新建方案编辑器；旧混合模板仍为兼容入口。详细使用与复跑命令见docs/0.42.0参数批量快捷设置.md，完整本轮QA位于qa/parameter_quick_0420。
'''
    (release/'交付报告.md').write_text(report,'utf-8')
    print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
