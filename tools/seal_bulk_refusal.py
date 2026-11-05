"""Seal 0.43.1 with fresh business comparison, regression and 7k EXE evidence."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf-8',newline='\n')

def main():
    qa=ROOT/'qa/bulk_refusal_0431';release=ROOT/'releases/0.43.1/rc1';delivery=ROOT/'交付_0.43.1_rc1/补货助手'
    build=read(ROOT/'build_0.43.1_rc1/build_info.json');comparison=read(qa/'comparison/equivalence.json');frozen=read(qa/'comparison/frozen.json');gui=read(qa/'exe-ui/report.json')
    regression=(qa/'regression-final.txt').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',regression)
    if not m or int(m[1])<971:raise ValueError('Full regression not passed')
    if not comparison['passed'] or comparison['differences'] or comparison['additional_ignored_fields']:raise ValueError('Strict comparison failed')
    if not gui['passed'] or not gui['frozen_exe'] or gui['exit_code'] or len(gui['bulk_benchmark']['seconds'])!=3:raise ValueError('Actual EXE failed')
    if gui['source_commit']!=build['source_commit'] or gui['exe_sha256']!=sha(delivery/'补货助手.exe'):raise ValueError('Build mismatch')
    if any(sha(Path(p))!=h for p,h in frozen['protected'].items()):raise ValueError('Protected file changed')
    if subprocess.check_output(['git','diff',build['source_commit'],'--','src'],cwd=ROOT):raise ValueError('Source changed after build')
    base=subprocess.check_output(['git','show','fa43f66:src/admission.py'],cwd=ROOT)
    if hashlib.sha256(base).hexdigest()!=sha(ROOT/'src/admission.py'):raise ValueError('Admission rules changed')
    release.mkdir(parents=True,exist_ok=False)
    for src,dst in [(qa/'comparison/equivalence.json','equivalence.json'),(qa/'comparison/frozen.json','frozen.json'),(qa/'regression-final.txt','regression.txt'),(qa/'targeted.txt','targeted.txt'),(qa/'exe-ui/report.json','exe-report.json')]:shutil.copyfile(src,release/dst)
    shutil.copyfile(ROOT/'build_0.43.1_rc1/build_info.json',release/'build_info.json')
    for p in (qa/'exe-ui').glob('0[789]-*.png'):shutil.copyfile(p,release/p.name)
    files={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    target=delivery.parent/'补货助手_0.43.1_rc1_批量解除拒收.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for rel in files:z.write(delivery/rel,'补货助手/'+rel)
    with zipfile.ZipFile(target) as z:
        for rel,h in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=h:raise ValueError('ZIP mismatch: '+rel)
    manifest={'source_commit':build['source_commit'],'version':'0.43.1','build_id':'rc1','exe_sha256':sha(delivery/'补货助手.exe'),'zip_sha256':sha(target),'files':files};save(release/'manifest.json',manifest)
    table='\n'.join(f'| {name} | {gui["bulk_benchmark"]["summary"][k]["median"]:.4f} | {gui["bulk_benchmark"]["summary"][k]["min"]:.4f} | {gui["bulk_benchmark"]["summary"][k]["max"]:.4f} |' for k,name in [('query','查询'),('prepare','准备核对'),('save','备份及事务保存')])
    report=f'''# 0.43.1 批量解除拒收候选

基线fa43f66（0.43.0），分支feat/bulk-refusal-release-0.43.1。构建源码{build['source_commit']}；干净固定Git归档构建。后续证据提交单独记录。不推送、不合并，0.43.0及旧包保留。

## 交互与边界

店-SKU复选框、勾选当前筛选、清空、完整范围核对和一次批量保存。筛选或刷新清空勾选，已解除不可再选；到期未解除可明确解除。按店合并复制与事务，未选记录、资质、原原因与来源保留。确认前并发变化整批拒绝；取消及失败不部分写入。明确保存后当前受影响门店需重算；历史不回写。未操作用户真实7000条清单。

## 验证

- 全量{m[1]}项通过（{m[2]}秒），新增11项覆盖精确键、重复选择、未选隔离、取消、并发拒绝、事务失败回滚、过期项、选择顺序、筛选清空和只读。
- 原同输入业务计算/Excel共38份严格对照差异0（含14,244行真实样本），不新增忽略白名单；模型/预算/箱规/准入核心文件未改，旧EXE/ZIP和验收输入哈希未变。
- 实际EXE {len(gui['checks'])}项检查通过，包含原资质/拒收查询导入链与7000条新链：实际点击复选框、全选、完整预览、取消、筛选切换、解除并刷新、已解除核对，逐店确认只有所选active改变。EXE哈希及构建提交核对一致。

## 7000条合成性能

70家店×100商品，三轮逐个运行、独立合成数据库，暖文件缓存。秒为单位，全部原始值在exe-report.json。保存包含既有备份、版本复核、事件和事务，不代表用户大历史库的备份耗时；未宣称相较旧版倍数提升（旧版没有一次批量解除功能）。

| 阶段 | 中位数 | 最小 | 最大 |
| --- | ---: | ---: | ---: |
{table}

实际界面单次查询含呈现{gui['bulk_ui_query_seconds']:.4f}秒，全选{gui['bulk_select_seconds']:.4f}秒，打开完整预览、自动确认、保存及刷新合计{gui['bulk_ui_confirm_save_refresh_seconds']:.4f}秒（不含人工阅读等待）。没有进行真实数据解除或平台联调。

## 交付

EXE：{delivery}/补货助手.exe
ZIP：{target}
EXE SHA256：{manifest['exe_sha256']}
ZIP SHA256：{manifest['zip_sha256']}。逐项核对{len(files)}个ZIP文件。

复跑命令和边界：docs/0.43.1批量解除拒收.md；完整QA：qa/bulk_refusal_0431。
'''
    (release/'交付报告.md').write_text(report,'utf-8',newline='\n');print(json.dumps({'zip':str(target),'sha256':manifest['zip_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
