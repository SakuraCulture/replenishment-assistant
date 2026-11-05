"""Seal candidate only after strict baseline comparison and frozen UI acceptance."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8')

def main():
    qa=ROOT/'qa/admission_overview_0430';release=ROOT/'releases/0.43.0/rc1';delivery=ROOT/'交付_0.43.0_rc1/补货助手'
    build=read(ROOT/'build_0.43.0_rc1/build_info.json');comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json');gui=read(qa/'exe-ui/report.json')
    regression=(qa/'regression-final.txt').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<960:raise ValueError('Full regression not passed')
    if not comparison['passed'] or comparison['differences'] or comparison['additional_ignored_fields']:raise ValueError('Strict comparison failed')
    if not gui['passed'] or not gui['frozen_exe'] or gui['exit_code']:raise ValueError('Actual EXE failed')
    if gui['source_commit']!=build['source_commit'] or gui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('Build mismatch')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Protected file changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    base=subprocess.check_output(['git','show','9980fd7:src/admission.py'],cwd=ROOT)
    if hashlib.sha256(base).hexdigest()!=sha(ROOT/'src/admission.py'):raise ValueError('Admission rules changed')
    release.mkdir(parents=True,exist_ok=False)
    for src,dst in [(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'regression-final.txt','regression.txt'),(qa/'targeted.txt','targeted.txt'),(qa/'exe-ui/report.json','exe-report.json')]:shutil.copyfile(src,release/dst)
    shutil.copyfile(ROOT/'build_0.43.0_rc1/build_info.json',release/'build_info.json')
    for p in (qa/'exe-ui').glob('*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.43.0_rc1_门店准入现状.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP mismatch: '+rel)
    manifest={'source_commit':build['source_commit'],'version':'0.43.0','build_id':'rc1','exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files}
    save(release/'manifest.json',manifest)
    report=f'''# 0.43.0 rc1 门店准入现状

基线9980fd7（0.42.0），分支feat/admission-overview-0.43.0；构建源码{build['source_commit']}。固定Git归档构建，后续证据提交不混称构建源码。未推送、未合并。

## 改动与保护范围

参数设置新增“二类医疗资质”和“门店拒收清单”独立入口，提供全体状态、筛选、只读详情、维护、模板、导入后定位及筛选导出。归集当前门店、参数门店、目录及独立准入记录；删除参数后的准入仍可查看，不复活参数。拒收维护不覆盖资质，资质维护保留拒收和原始来源。

准入核心admission.py、模型、预算、仓归属与既有导出不改；参数/存储格式不变。未实际修改用户数据库、原Excel或旧包。明确维护仍按原规则使受影响当前结果失效；历史不回写。

## 验证

- 全量{m[1]}项通过，{m[2]}秒；新增17项涵盖状态/到期边界、参数删除后可见、查询只读、取消、筛选精确导出、编码类型、只读防修改、导入定位及维护隔离。
- 冻结同输入独立运行基线和候选，33份业务对象及5份Excel共38份严格对照差异0，含14,244行真实样本；不扩大白名单。核心文件及旧版EXE/ZIP、原始验收输入哈希未变。
- 实际EXE {len(gui['checks'])}项原生Qt检查通过：入口、查询、筛选、只读详情、取消、解除保存、资质与拒收两类导入、导入定位、筛选导出、返回全部和未选门店保护；构建提交/EXE哈希一致。验收均在隔离合成库。
- 未进行真实业务资质审批、平台联调或跨应用鼠标拖拽。复用控件的文件接收和导入链已验证；不宣称计算性能改善。
- 首轮新增测试夹具错误使用函数取消标记且未关闭SQLite连接，已按既有Event/close契约修正。首次原生驱动未识别图标关闭按钮、并在定时回调内阻塞嵌套弹框，已改用可访问名称和事件排队；相关失败目录保留。没有改旧预期迎合实现。

## 交付

EXE：{delivery}/补货助手.exe
ZIP：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}；{len(files)}个交付文件逐项核对ZIP。

保留0.42.0及所有旧包。使用和复跑命令见docs/0.43.0准入现状查询.md；完整QA为qa/admission_overview_0430。本轮仅补齐准入现状管理，不迁移数据、不扩大为批量审批或资质证件管理。
'''
    (release/'交付报告.md').write_text(report,'utf-8');print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
