"""Seal 0.50.2 local evidence and a new ZIP, without changing existing bundles."""
import argparse,hashlib,json,re,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools')]


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);a=p.parse_args()
    qa=a.qa.resolve();qa.relative_to(ROOT/'qa')
    build=ROOT/'build_0.50.2_rc1';delivery=ROOT/'交付_0.50.2_rc1';app=delivery/'补货助手'
    release=ROOT/'releases/0.50.2/rc1'
    if release.exists():raise FileExistsError(release)
    info=json.loads((build/'build_info.json').read_text('utf-8'));source=info['source_commit']
    assert info['version']=='0.50.2' and info['build_id']=='rc1'
    assert json.loads((app/'_internal/build_info.json').read_text('utf-8'))==info
    exe_hash=digest(app/'补货助手.exe');proofs={}
    for label,path in [('exe-pipeline','exe-pipeline/process-acceptance.json'),('exe-original-recovery','exe-original-recovery/process-acceptance.json'),('source-pipeline','source-ui-03/process-acceptance.json'),('source-original-recovery','previous-recovery-source-02/process-acceptance.json')]:
        value=json.loads((qa/path).read_text('utf-8'));assert value['passed'] and value['accepted'] and not value['live_platform']
        if label.startswith('exe-'):assert value['frozen_exe'] and value['exe_sha256']==exe_hash and value['process_exit']==0
        proofs[label]=value
    python=(qa/'python-full-final.log').read_text('utf-8');match=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',python);assert match
    node=(qa/'node-full-final.log').read_text('utf-8');count=re.search(r'tests (\d+)',node);assert count and re.search(r'fail 0\b',node)
    manual=json.loads((qa/'manual-html/html-report.json').read_text('utf-8'));assert manual['passed'] and manual['chapters']==20 and not manual['external_requests']
    core=json.loads((qa/'baseline-diagnostic.json').read_text('utf-8'));assert all(v['equals_base'] for v in core['core_source'].values())
    protection=[]
    for old in json.loads((ROOT/'releases/0.50.1/rc3/artifacts.json').read_text('utf-8'))['files']:
        path=ROOT/old['path'];assert digest(path)==old['sha256'];protection.append(old)
    import aoxiang_bundle
    aoxiang_bundle.verify(app/'_internal/components/aoxiang')
    files=[p for p in app.rglob('*') if p.is_file()]
    assert not any(p.suffix in ('.sqlite','.sqlite3','.db') for p in files),'A business/state DB entered the bundle'
    archive=delivery/'补货助手_0.50.2_rc1.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for path in sorted(files):z.write(path,path.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None and len(z.infolist())==len(files)
    release.mkdir(parents=True)
    def write(name,value):(release/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8')
    shutil.copyfile(build/'build_info.json',release/'build_info.json')
    shutil.copyfile(qa/'baseline-diagnostic.json',release/'baseline-diagnostic.json')
    for name,value in proofs.items():write(name+'.json',value)
    write('manual-html.json',manual)
    for src,dest in [('python-full-final.log','python-full-final.txt'),('node-full-final.log','node-full-final.txt'),('python-full-initial.log','python-full-initial.txt'),('python-pipeline-third.log','early-pipeline-fixture-failure.txt'),('python-workflows-final.log','early-workflow-fixture-failure.txt'),('baseline.log','legacy-baseline-failure.txt')]:shutil.copyfile(qa/src,release/dest)
    for src,dest in [('source-ui-02/process-acceptance.json','early-shared-session-fixture-failure.json'),('previous-recovery-source/process-acceptance.json','early-original-recovery-fixture-failure.json')]:shutil.copyfile(qa/src,release/dest)
    artifacts={'source_commit':source,'files':[{'path':str(path.relative_to(ROOT)),'bytes':path.stat().st_size,'sha256':digest(path)} for path in (app/'补货助手.exe',archive)]}
    write('artifacts.json',artifacts)
    write('protection.json',{'old_0501_rc3_preserved':protection,'core_source':core['core_source'],'source_base':core['source_comparison_base'],'legacy_030_audit_passed':False,'legacy_030_reason':'Fixed legacy directory does not exist; not restored or rewritten'})
    write('delivery-files.json',[{'path':p.relative_to(app).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(files)])
    write('evidence-manifest.json',[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(qa.rglob('*')) if p.is_file() and p.name!='seal.log'])
    validation={'source_commit':source,'base_commit':subprocess.check_output(['git','rev-parse','cebb794'],cwd=ROOT,text=True).strip(),'python_tests':int(match[1]),'python_seconds':float(match[2]),'node_tests':int(count[1]),'exe_checks':sum(len(v['checks']) for k,v in proofs.items() if k.startswith('exe-')),'manual_chapters':manual['chapters'],'manual_images':manual['image_count'],'live_platform_tested':False,'real_platform_speed_measured':False,'legacy_030_audit_passed':False,'old_delivery_preserved':True,'model_unchanged':True,'pushed':False,'merged':False}
    write('validation.json',validation)
    report=f'''# 补货助手 0.50.2 rc1：采购异常恢复与等待流水线

交付日期：2026-11-10。分支 `feat/purchase-recovery-pipeline-0.50.2`，本地交付，未推送、未合并。

## 结果与用法

异常任务新增“结束所选任务并重建预览”。在原批次勾选需要重做的仓单，即使原上传无回执、平台处理中、部分失败或已成功，也能明确结束旧占用并准备后继；不再要求先把旧结果核验成功。新预览采用当前有效签发和付款，仍须另行确认上传。原平台单不会撤销，可能的重复部分由业务处理。

上传取得并保存唯一回执后先提交下一单，再核验原任务；默认同时等待2单，可设1–4单。浏览器动作依次进行，平台处理等待重叠，完整主单和商品核验保持。已受理等待一轮未完成即持久排队并释放会话，稍后继续原任务。单项业务异常不拖住其他项，连接及身份异常仍暂停本轮。

旧冻结文件、回执、PO号和重建关系保留；晚到回执只写原记录，不能覆盖后继。所选旧同步计划停止，未选任务和已确认的签发、付款、实收事实保持。源订单已删则不能按同号新单恢复。重建应答丢失可重复请求同一操作，原子事务失败整体回滚。补货模型、预算、箱规、默认参数、仓归属和业务存储格式不变。

## 验证

| 范围 | 本轮结果 |
|---|---|
| Python全量回归 | **{validation['python_tests']}项通过**，{validation['python_seconds']}秒 |
| 翱象组件全量回归 | **{validation['node_tests']}项通过**，含12项新增重建及受理分离测试 |
| 新增Python专项 | 13项调度/范围/当前原单测试；另增真实本地Node的重建应答丢失恢复 |
| 实际固定EXE | **{validation['exe_checks']}项通过**：新增重建/模拟流水线6项，原任务继续核对9项；两进程均退出0 |
| 源码界面 | 同两组15项通过；原生文件及本地Node真实登记，平台替身封闭 |
| 图文手册 | 20章28图，离线加载、搜索、放大、手机宽度和历史PDF链接通过；无外部请求 |
| 保护 | 0.50.1 rc3 EXE与ZIP哈希保持，当前基线7个核心源文件逐字节一致 |

所有验证使用隔离数据库和合成文件，未调用真实平台，未清空正在使用的资料。实际EXE新流程使用真实本地组件重建；平台并行等待顺序使用封闭替身验证。尚未测量真实平台吞吐、限流或低配置电脑效果，不承诺倍速。上限只统计本批本轮有唯一回执的等待项，未知结果不代表平台没有在途请求；不同批次不是全局并行配额。

全量首轮保留2项失败：调用分离后旧用例仍拦截purchase.start，新说明曾遗漏原“单据详情”路径。修正故障注入到purchase.submit并保留原保护预期，说明同时提供原核对与新重建出口后，最终全量通过。新增专项初轮的SQLite夹具未关闭、过短模拟等待，以及早期GUI夹具错误地另开共享组件、旧冒烟缺来源库，也均保留原失败证据，修正夹具后通过。未跳过完整性校验或放宽数量、金额、原单身份预期。

历史 `tools/audit_baseline.py` 未通过：其固定 `交付_0.3/补货助手` 目录不存在（期望191文件，实有0），没有恢复或改写旧目录；不能宣称本轮已重验0.3完整交付。当前基线核心源码及现存0.50.1 rc3另行核验，见 `protection.json`。既有QLabel销毁后回调问题不在本轮修复范围，未宣称已解决。

## 实现与复跑

- 组件：持久预约与历史分离，新增原子rebuild及submit-only动作，保留自动重复请求保护。
- 主程序：稳定item_id和执行令牌回写，当前原仓单读取、所选重建与旧同步退役、有界等待调度。
- 入口与说明：批次恢复按钮、平台等待状态、1–4单上限、随包说明及新人手册第20章。
- 方案：`docs/plans/采购异常恢复与等待流水线_20261110.md`；用法：`docs/0.50.2采购异常恢复与等待流水线.md`。
- 全量：`.venv/Scripts/python.exe -m unittest discover -s tests -v`（PYTHONPATH=src）；组件：在components/aoxiang运行固定Node的`--test test/*.test.mjs`。
- EXE：`tools/verify_purchase_pipeline.py --exe <新EXE> --out <qa下新目录>`；原核对：`tools/verify_purchase_recovery.py --exe <新EXE> --out <qa下另一新目录>`。先读脚本，输出目录必须全新。

## 交付

- 基线：`{validation['base_commit']}`。
- 固定打包源码：`{source}`；构建命令：`tools/build_from_commit.py --commit {source} --build-id rc1`。
- EXE：`交付_0.50.2_rc1/补货助手/补货助手.exe`。
- ZIP：`交付_0.50.2_rc1/补货助手_0.50.2_rc1.zip`。完整解压运行，保留所有子目录。
- 原始验证：`qa/purchase_recovery_pipeline_0502_20261110/`；哈希和构建信息见本目录。旧包与独立0.49.4删除性能工作区保持，本版不包含该独立分支。
'''
    (release/'交付报告.md').write_text(report,'utf-8');shutil.copyfile(release/'交付报告.md',delivery/'交付报告.md')
    print(json.dumps({'release':str(release),'zip':str(archive),'validation':validation,'artifacts':artifacts},ensure_ascii=False))


if __name__=='__main__':main()
