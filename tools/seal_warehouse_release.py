"""Seal only the clean 0.10.0 build and its current acceptance evidence."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/warehouse_010';DIST=ROOT/'交付_0.10.0_rc1';APP=DIST/'补货助手';REL=ROOT/'releases/0.10.0/rc1'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
load=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip();info=load(APP/'_internal/build_info.json')
if info['source_commit']!=head or subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip():raise ValueError('requires clean exact build commit')
reports={k:load(Q/v) for k,v in {'regression':'regression_final.json','delivery':'delivery_accepted/report.json','business':'business_final/report.json','cancellation':'fixes/report.json','exe':'exe_rc1/report.json'}.items()}
for k,r in reports.items():
    if not r['passed']:raise ValueError('failed '+k)
for name,h in reports['delivery']['source_hashes'].items():
    if sha(ROOT/name)!=h:raise ValueError('source changed since delivery checks: '+name)
exe_sha=sha(APP/'补货助手.exe')
for r in reports['exe']['runs']:
    if not r['passed'] or r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=exe_sha or r['version']['source_commit']!=head:raise ValueError('EXE not accepted')
for name in ('units_final.log','units_optimized.log'):
    text=(Q/name).read_text(encoding='utf-8')
    if 'Ran 35 tests' not in text or '\nOK' not in text:raise ValueError('unit checks missing')
protected=load(Q/'protected.json');drift=[p for p,h in protected.items() if not Path(p).is_file() or sha(p)!=h]
if drift:raise ValueError('protected files changed '+repr(drift))
if REL.exists():raise FileExistsError(REL)
REL.mkdir(parents=True)
for name,r in reports.items():(REL/(name+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
for src,name in [('baseline.json','baseline.json'),('regression.json','initial_unseeded_comparison.json'),('units_final.log','units.txt'),('units_optimized.log','units_optimized.txt')]:shutil.copy2(Q/src,REL/name)
for f in (Q/'exe_rc1').glob('*.png'):shutil.copy2(f,REL/f.name)
table='\n'.join(f"|{r['case']}|{r['files']}|{r['rows']}|{r['quantity']}|{r['amount']:.2f}|0|" for r in reports['delivery']['comparisons'])
text=f'''# 0.10.0 rc1 交付与验收

分支 feature/warehouse-confirmations-0.10.0；基线6894be792e99f9aa179c1fc5ac8a8e24ffe134b4；干净构建提交 {head}。应用0.10.0，模型0.3.3-rules，仓归属0.5.0，存储格式2不变。保留0.9.2；本轮没有合并或推送。

## 使用

运行 {APP / '补货助手.exe'}；整个文件夹保留，也可完整解压ZIP。文件名为日期门店名称（精简版·河北仓）.xlsx /（精简版·山东仓）.xlsx。工作页选择店仓后签发完整门店正常单，再分文件；历史页可只重导某仓。两仓子单同一父单和修订，回传校验当时归属仓。门店默认保存主从仓与完整有效参数，下次导入自动带入；预算独立勾选，临时修改不保存，恢复历史使用原快照。

## 已验证

- 35组便携测试正常与优化模式通过。38项独立源码回归逐字段、类型、列表/字典顺序、内部Excel对照0差异，覆盖真实14244行不限/0/3000/5000/10000预算、关闭一箱、自定义参数及多文件、主从仓、异常场景。
- 拆分确认单与冻结0.9.2导出函数逐格对照：各仓行合并与原总表无重复无丢失，数量、金额、原字段、样式、表序和仓内商品顺序一致。只允许子单号、仓属性、文件边界与文件名变化；没有删列或放宽浮点容差。真实case取冻结真实计算结果；synthetic两店人为分配仓用于边界验证，不冒充真实路由。

|场景|分仓文件数|导出商品行|总数量|总金额|非预期差异|
|--|--:|--:|--:|--:|--:|
{table}

- 两仓同批回传合并一个父修订回传版本；单仓更新保留另一仓；重复导入无新版本；跨仓商品拒绝；旧父号兼容；预览并发冲突拒绝；历史V001重导不改快照。
- A签发V001、B在A读回前签发V002：A的两份仓文件和两条导出日志仍绑定V001。单仓选择仍归档完整门店。现有原子事务、备份、编码/写锁/提交边界取消、失败回滚回归通过。取消可能留下安全备份/存储元数据，不声称数据库字节不变。
- 实际EXE两次独立启动：两仓资料导入、检查文件、两店不同参数并发计算；门店默认保存弹框、预算未勾选；店仓选择取消/全选、批量归档生成四份文件；两仓回传预览保存、按仓状态；历史仅选山东重导；临时配置不写默认。第二次启动导入相同门店自动恢复默认，随后恢复作业仍取当时参数。两次退出码0、无遗留子进程。EXE截图与耗时/RSS采样见exe.json（不是对照性能测试）。
- 保护 {len(protected)} 个旧发行文件/冻结输入/用户资料，SHA256漂移0。模型、分配、一箱、路由、核心编排、默认配置与归档存储实现哈希不变，见regression.json。

## 允许变化与兼容

新增仓子单的号码/文件/事件来源，回传payload的warehouse/number/warehouses字段，回传表“所属仓”及仓状态；门店默认saved时间与显式预算选项。没有改签发数量、候选数量、执行数量/金额、计算理由、模型指纹白名单或数据库schema。老归档、老回传和已有门店默认兼容，不回写历史快照。历史内部总单导出函数仍保留薄兼容入口；用户加盟商入口全部分仓。

## 限制与证据

没有当前归属仓的历史商品不能猜仓导出；该门店提示待核，其他门店可继续。没有可导出商品的仓不出文件；零数量新品仍属候选，会保留。多仓文件逐个写出，不是并行导出；未承诺提速，也未改历史索引性能。

首次不固定Python哈希种子发现原有分类映射字典键序2处差异（engine原set遍历），初次报告保留为initial_unseeded_comparison.json。固定双方PYTHONHASHSEED=0后重新独立计算、比较为0；没有排序记录、删除字段或修改旧模型。未顺带修复此旧实现。

构建用0.9.2已锁定依赖的独立环境；本机实际运行验证，不代表全新电脑或逐字节可复现构建。完整复跑命令和模块调用链见docs/0.10.0分仓确认单与门店默认.md；QA脚本拒绝复用已存在输出目录，需新目录复跑。

EXE SHA256：{exe_sha}。ZIP及全文件清单见release_report.json / files.json。标签在证据提交后指向发行证据提交；构建提交与标签提交分别记录。
'''
(REL/'交付报告.md').write_text(text,encoding='utf-8',newline='\n');(APP/'交付报告.md').write_text(text,encoding='utf-8',newline='\n')
(APP/'验收记录.json').write_text(json.dumps({'passed':True,'build_commit':head,'baseline':'6894be792e99f9aa179c1fc5ac8a8e24ffe134b4','regression_differences':0,'exe_checks':[r['checks'] for r in reports['exe']['runs']]},ensure_ascii=False,indent=2),encoding='utf-8')
files={str(p.relative_to(APP)):sha(p) for p in sorted(APP.rglob('*')) if p.is_file()};(REL/'files.json').write_text(json.dumps(files,ensure_ascii=False,indent=2),encoding='utf-8')
zip_path=DIST/'补货助手_0.10.0_rc1.zip'
with zipfile.ZipFile(zip_path,'x',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(APP.rglob('*')):
        if p.is_file():z.write(p,str(Path(APP.name)/p.relative_to(APP)))
report={'passed':True,'version':'0.10.0','model_version':'0.3.3-rules','branch':'feature/warehouse-confirmations-0.10.0','source_commit':head,'exe_sha256':exe_sha,'zip_sha256':sha(zip_path),'zip':str(zip_path),'delivery':str(APP),'files':len(files),'protected_files':len(protected),'protected_changes':[]}
(REL/'release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
