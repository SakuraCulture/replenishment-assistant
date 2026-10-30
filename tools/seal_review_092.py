"""Seal a separate 0.9.2 delivery only with current, actual EXE and frozen evidence."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/verified_092';DIST=ROOT/'交付_0.9.2_rc2';APP=DIST/'补货助手';REL=ROOT/'releases/0.9.2/rc2'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
load=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();info=load(APP/'_internal/build_info.json')
if info['source_commit']!=head or subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():raise ValueError('seal requires clean build commit')
paths={'core':'real_core.json','equivalence':'overrun_equivalence.json','technical_stage':'equivalence_pre_overrun.json','performance':'performance.json','batch':'batch/batch_report.json','cancellation':'fixes_final/report.json','business':'business_final/report.json','races':'races_final/report.json','intake':'intake_controls/controls.json','exe_business':'exe_business_rc2/business_ui_report.json','exe_history':'exe_inbound_rc2/inbound_ui_report.json'}
reports={k:load(Q/v) for k,v in paths.items()}
for k,r in reports.items():
    if not r['passed']:raise ValueError('failed '+k)
eh=sha(APP/'补货助手.exe')
for key in ('exe_business','exe_history'):
    r=reports[key]
    if r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=eh or r['version']['source_commit']!=head or r['version']['model_version']!='0.3.3-rules':raise ValueError('actual EXE mismatch '+key)
protected=load(Q/'protected.json');drift=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
if drift:raise ValueError('protected files changed '+repr(drift))
for name in ('units_final092.log','units_optimized092.log','units_clean092.log'):
    text=(Q/name).read_text(encoding='utf-8')
    if 'Ran 28 tests' not in text or '\nOK' not in text:raise ValueError('unit evidence '+name)
if 'No broken requirements found' not in (Q/'clean_pip_check.log').read_text(encoding='utf-8'):raise ValueError('clean environment dependency check missing')
if REL.exists():raise FileExistsError(REL)
REL.mkdir(parents=True)
for key,path in paths.items():shutil.copy2(Q/path,REL/(key+'.json'))
for name in ('units_final092.log','units_optimized092.log','units_clean092.log','clean_pip_check.log','wheel_manifest.json','generated_step_probe.json'):
    shutil.copy2(Q/name,REL/(name+'.txt' if name.endswith('.log') else name))
performance=reports['performance']['summary'];table='\n'.join(f"|{r['mode']}|{r['variant']}|{r['median_seconds']:.3f}|{r['min_seconds']:.3f}–{r['max_seconds']:.3f}|{r['peak_mib']:.1f}|" for r in performance)
text=f'''# 0.9.2 rc2 验收交付

构建提交 `{head}`，基线 `7dfc00b545b6dac1dd586212fff940e5346e0710`，分支 fix/verified-issues-0.9.2。应用0.9.2/模型0.3.3-rules。保留0.9.1及日常资料，未合并或替换。已推送的是0.9.1分支和标签；0.9.2本地交付。

运行 `{APP}` 下的补货助手.exe，整个目录保留或完整解压ZIP。已从本机新建空venv、离线SHA锁定wheel安装环境构建；不是全新电脑验收或逐字节可复现声明。

## 已完成

统一严格步长尾差核验；来源映射与身份核对；计算输入瘦身和移除重复拷贝/单元素调用；文件检查复用一次工作簿打开；错误诊断号/完整堆栈；零分母明确拒绝；参数生效核对；批量来源冲突提案/默认取消/撤销；停止常驻反馈；快捷键和首次指南。

另外按用户最新确认，先按目标预算分配，末尾最多追加一个最小出库单位，整店最多105%，0元不补，不超过模型候选。5%不是数量容差，不是把正常预算提高5%重新分配。汇总、商品、历史和确认单理由明确显示补足。

## 独立证据

- 技术修复阶段38项严格对照0差异；最终预算规则38项独立参考计算对照0非预期差异，逐商品允许变化见equivalence.json。真实14244样本：3000预算2999.87→3040.87；5000预算4999.98→5005.37；10000预算9980.33→10013.33；自定义3000方案3000→3040.50。每店最多一条增加，原已分配数量不挪动。0元/不限预算不变。
- 10店164722行，85545条可计算核心重放0字段差异；原21天在途约束、一箱与双零规则保持。28组便携测试正常/优化模式/新空venv通过。
- 多文件顺序/强制乱序、取消、进程故障、重试、读取期间真实文件变化；两店串行/并发、预算隔离、停止和导出核对通过。
- 重新验证准备/事务/写锁/提交边界取消与重试、同额商品差异、独立回传、旧历史、A导出V001同时B签发V002绑定。取消可能保留安全备份/存储元数据，不宣称测试库字节不变。
- 实际EXE两组：历史查询、参数/销量库存/商品资料与逐格导出、关闭进程回收、风险筛选；拖拽和最低模板、回传、快捷键、参数生效核对、批量冲突取消/确认/撤销、停止提示；EXE内实际调用末尾补足/0预算/确认单理由。退出码0、遗留子进程0。停止提示用未启动的测试worker验证UI，真实停止语义另由batch验收。
- 保护{len(protected)}个原输入/旧包/日常资料文件，SHA256漂移0。所有写入在独立QA库/交付目录。

## 三轮串行交替性能

|操作|版本|中位秒|最小–最大秒|采样最大MiB|
|--|--|--:|--:|--:|
{table}

同机、同Python依赖与14244行输入；新旧交替逐个运行，不清空系统文件缓存。read含哈希/识别/读取/校验，calculate含模型/预算，batch另含复制/序列化/spawn。内存为进程树RSS每50ms求和，包含子进程解释器及加载，不等于系统物理峰值。新增预算规则的结果差异由独立参考计算验证，不假称所有预算金额不变。完整18次原值见performance.json；不把一次最好值或拆掉哈希说成提速。

## 仍有边界

真正非步长候选仍拒绝，未擅自改为部分商品完成；零分母未编造预测回退，已有阻塞输入遇到模型异常仍可能使门店失败。共享元数据仍可能重复传输，旧混合资料入口仍有额外读取；没有共享内存/全面向量化/一键演示。历史冷索引仍有等待。源文件哈希不删。完整问题状态和复跑入口见docs/0.9.2已确认问题修复.md。

EXE SHA256 `{eh}`。构建源码与后续发行证据提交分开记录；旧签发快照不会因新预算规则改变。
'''
(REL/'交付报告.md').write_text(text,encoding='utf-8');(APP/'验收与限制.md').write_text(text,encoding='utf-8')
files={str(p.relative_to(APP)):sha(p) for p in APP.rglob('*') if p.is_file()};(REL/'files.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
archive=DIST/'补货助手_0.9.2_rc2.zip'
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(APP.rglob('*')):
        if p.is_file():z.write(p,p.relative_to(DIST))
report={'passed':True,'source_commit':head,'version':'0.9.2','model_version':'0.3.3-rules','exe_sha256':eh,'zip_sha256':sha(archive),'delivery':str(APP),'zip':str(archive),'protected_count':len(protected),'protected_drift':drift,'files':len(files),'source_hashes':{p.name:sha(p) for p in (ROOT/'src').iterdir() if p.is_file()}}
(REL/'release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':True,'source_commit':head,'exe_sha256':eh,'zip_sha256':report['zip_sha256']},ensure_ascii=False))
