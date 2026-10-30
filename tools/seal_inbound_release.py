"""Seal immutable 0.9.1 candidate with fresh source + actual EXE evidence."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/inbound_091';DIST=ROOT/'交付_0.9.1_rc1';APP=DIST/'补货助手';REL=ROOT/'releases/0.9.1/rc1'
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
load=lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
info=load(APP/'_internal/build_info.json');head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
if head!=info['source_commit']:raise ValueError('seal must bind current clean build commit')
files={'real':'real/report.json','regression':'regression_comparison.json','exports':'real_exports/report.json','cancellation':'fixes/report.json','business':'business/report.json','races':'races/report.json','exe_history':'exe_inbound/inbound_ui_report.json','exe_business':'exe_business/business_ui_report.json'}
reports={k:load(QA/v) for k,v in files.items()}
for k,r in reports.items():
    if not r['passed']:raise ValueError('failed evidence '+k)
exe_hash=sha(APP/'补货助手.exe')
for key in ('exe_history','exe_business'):
    r=reports[key]
    if r['development'] or r['exit_code'] or r['remaining_children'] or r['exe_sha256']!=exe_hash or r['version']['source_commit']!=head:raise ValueError('EXE evidence mismatch '+key)
for name,h in reports['real']['source_hashes'].items():
    if sha(ROOT/'src'/name)!=h:raise ValueError('core drift '+name)
for name in ['unit_final.log','unit_optimized_final.log']:
    text=(QA/name).read_text(encoding='utf-8')
    if 'Ran 12 tests' not in text or '\nOK' not in text:raise ValueError('unit evidence missing '+name)
protection=load(QA/'protected.json');drift=[p for p,h in {**protection['files'],**protection['inputs']}.items() if not Path(p).is_file() or sha(p)!=h]
if drift:raise ValueError('protected inputs/releases/user data changed '+repr(drift))
if REL.exists() or (DIST/'补货助手_0.9.1_rc1.zip').exists():raise FileExistsError('release already sealed')
report={'passed':True,'source_commit':head,'baseline_commit':protection['baseline'],'version':'0.9.1','model_version':'0.3.2-rules','build_id':info['build_id'],'exe_sha256':exe_hash,'protected_drift':drift,'protected_files':len(protection['files']),'frozen_inputs':protection['inputs'],'core_hashes':reports['real']['source_hashes'],'delivery':str(APP),'actual_exe_reports':{k:reports[k] for k in ['exe_history','exe_business']}}
text=f'''# 0.9.1 交付与验收

构建提交 `{head}`；基线 `{protection['baseline']}`。应用 0.9.1 / 模型 0.3.2-rules；分支 fix/inbound-cap-history-0.9.1。仅本地提交/打包，旧版保留，未合并或推送。

交付：`{APP}`。启动补货助手.exe，完整目录需一起保留；ZIP 解压后运行，不单独移动 EXE。

## 完成

- 已有在途的追加保底受原21天库存额度约束，一箱例外不能重新放行同类额度拦截。双零最低持有及箱规倍数保留；原禁用/停采/预算仍有效。
- 5条风险仅提示。汇总提示栏直达风险筛选，商品详情展示3.5天复核情景与实际仓到货周期，无自动加回。
- 日志按日期、门店、多关键词查询；单据选中商品直接带入，左侧名称搜索/点击选品，右侧表单范围明确。父页面刷新不再取消子表单，关闭各自回收读取进程。
- 表单导出绑定单号/修订/商品/范围；完整字段不受字段筛选影响。操作日志导出匹配操作过程，确认单仍按原中心仓模板范围。

## 验证

- 固定签发批次10店、164722行：85545条有效资料匹配记录新旧核心重放；306条中间变化、64条执行变化，金额240467.53→237962.58，减少2504.95。非目标数量/金额差异0，双零5567条原样保留，箱规检查通过。风险恰好5条。
- 33个读取/冲突/双仓/自定义参数/预算场景及5个工作簿共38项严格对照：业务值/类型/顺序差异0。仅模型版本元数据升级（12个Excel单元格精确列明），不扩大忽略范围。
- 真实10店20份确认单：只移除批准的64条不再执行商品，其他单元格值/类型/样式/商品顺序0差异，表头/工作表顺序保持。
- 12组便携自动测试，正常与 python -O 均通过。显式错误哈希仍拒绝；小数取整失败与z=0旧边界另留复现。
- 重新验证提交前取消、写锁等待、最终边界、回滚与重试、同额商品差异、原因归因、独立回传、旧格式读取/副本转换、A导出V001同时B签发V002绑定。
- 实际EXE两组：真实历史组合查询、选品/参数/销量库存/商品资料、完整表单逐格导出、父页面刷新不干扰、快速切换、未知编码/重试/关闭；实际引擎限额及风险筛选；拖拽/模板、回传取消/确认、原签发不变、事件回溯。进程退出码0、遗留子进程0。
- 保护核对 {len(protection['files'])} 个旧包/日常资料文件与冻结输入，哈希未变。测试使用独立QA库，不写日常库。

## 实际运行范围与限制

实际EXE完整测试计时、事件循环最大间隔、采样进程树内存在 exe_history.json、exe_business.json。它们不是串行AB性能比较，不宣称全面提速或全程零停顿。冷历史索引、完整快照/原文件读取仍需等待；界面后台读取，加载/失败/导出状态可见。

尚未处理：预算候选非倍数导致整店失败、位置式源行映射的长期脆弱性、计算进程共享数据及逐行复制成本、批量错误诊断、极小小数销量z=0预测边界、全新机器可复现构建、批量冲突确认及首次使用教程。详见随包 docs/0.9.1代码疑问核查.md，不把“已核查”当作“全部修复”。

历史签发不重算不覆盖；旧模型作业继续工作需重算。首次运行新包使用现有默认资料位置，建议关闭旧版再运行；验收使用隔离资料。原0.9.0包未替换。

EXE SHA-256：`{exe_hash}`。
复跑命令：docs/0.9.1在途保底与历史查询.md；完整QA：qa/inbound_091。core_hashes、允许字段差异和具体变化在随发行JSON内。
'''
REL.mkdir(parents=True)
for k,v in files.items():shutil.copy2(QA/v,REL/(k+'.json'))
for name in ['review_probes.json','review_probes_optimized.json','unit_final.log','unit_optimized_final.log']:shutil.copy2(QA/name,REL/(name+'.txt' if name.endswith('.log') else name))
(REL/'交付报告.md').write_text(text,encoding='utf-8');(APP/'验收与限制.md').write_text(text,encoding='utf-8')
manifest={str(f.relative_to(APP)):sha(f) for f in APP.rglob('*') if f.is_file()};(REL/'files.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
archive=DIST/'补货助手_0.9.1_rc1.zip'
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
    for f in sorted(APP.rglob('*')):
        if f.is_file():z.write(f,f.relative_to(DIST))
report.update(zip_path=str(archive),zip_sha256=sha(archive),delivery_files=len(manifest));(REL/'release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'passed':True,'source_commit':head,'exe_sha256':exe_hash,'zip_sha256':report['zip_sha256'],'delivery_files':len(manifest)},ensure_ascii=False))
