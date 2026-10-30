"""Generate a business-readable release report from completed local evidence."""
import argparse
import json
import statistics
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--qa', type=Path, required=True)
    parser.add_argument('--delivery', type=Path, required=True)
    args = parser.parse_args()
    qa = args.qa.resolve()
    def read(rel):
        return json.loads((qa / rel).read_text(encoding='utf-8-sig'))
    perf = read('performance_final/report.json')
    eq = read('equivalence_final/report.json')
    real = read('real_exports2/report.json')
    legacy = read('legacy_exports/report.json')
    race = read('races1/report.json')
    history = read('exe-history/history_ui_report.json')
    work = read('exe-work-io/work_io_report.json')
    business = read('exe-business/business_ui_report.json')
    fixes = read('exe-fixes/fixes_ui_report.json')
    meta = json.loads((args.delivery / '版本信息.json').read_text(encoding='utf-8-sig'))
    output = qa / '交付报告.md'
    assert not output.exists(), output
    labels = {'prepare_unsigned': '未签发准备（含进程启动/关闭）', 'archive_prepare_inside': '归档内部准备',
              'archive_commit_inside': '归档内部校验/编码/备份/提交', 'archive_total': '首次归档完整流程',
              'preview_changed': '已有单据变更预览', 'export_signed_total': '已签发五店导出完整流程'}
    timing = []
    memory = []
    for key, values in perf['summary'].items():
        a, b = values['old'], values['new']
        timing.append(f"|{labels[key]}|{a['median']:.3f}（{a['min']:.3f}～{a['max']:.3f}）|{b['median']:.3f}（{b['min']:.3f}～{b['max']:.3f}）|{(b['median']/a['median']-1)*100:+.1f}%|")
        if all(v is not None for v in a['peak_mib'] + b['peak_mib']):
            am, bm = statistics.median(a['peak_mib']), statistics.median(b['peak_mib'])
            memory.append(f"|{labels[key]}|{am:.1f}|{bm:.1f}|{bm-am:+.1f}|")
    raw = []
    for key, values in perf['summary'].items():
        raw.append(f"|{labels[key]}|" + ' / '.join(f'{x:.3f}' for x in values['old']['seconds']) + '|' + ' / '.join(f'{x:.3f}' for x in values['new']['seconds']) + '|')
    whitelist = json.dumps(eq['whitelist'], ensure_ascii=False, indent=2)
    text = f"""# 补货助手 0.9.0 rc1 试用交付与验收

本次已完成三步功能开发及0.8.0三个遗留问题修复。固定基线为`e45b9d08cddbced1bf8566281987431534bbf5b7`，本地分支`fix/business-history-0.9.0`，构建提交`{meta['source_commit']}`。模型仍为`{meta['model_version']}`，仓归属0.5.0，参数/作业逻辑schema 1、物理存储格式2不变。没有合并、推送或替换旧包。

试用程序：`{args.delivery.resolve() / '补货助手.exe'}`。完整ZIP、EXE SHA256及发行证据工具提交见同目录`release_report.json`；最终发行证据提交由本地`v0.9.0`标签标记。请解压完整目录运行，不能只复制EXE。

## 已完成的三步

1. **历史业务表单。** 从历史补货单进入单据概况、当时参数、商品建议与销量库存、当时商品资料、完整计算依据；可搜索、导出完整表单Excel、查看或下载当时原文件。实际生效配置来自签发快照，不使用当前参数补填。操作日志可查询和导出Excel，双击定位事件对应的原修订。业务查看入口不再要求阅读JSON。
2. **统一导入。** 补货建议、分仓资料、旧版混合资料、必卖清单、新品关注、加盟商回传均支持拖拽和最低标准Excel模板。编码按文本保留；现有必卖CSV/JSON备份导入兼容。商品资料/销售等原文件在校验后独立按哈希留存，不塞进计算结果，不修改原文件。
3. **独立加盟商回传。** 支持原加盟商确认单Q列或最低模板，按补货单号+确切签发修订+商品编码关联。原表修订标识丢失时必须人工选修订，不按相似名称或相近日期猜测。预览差异，默认取消；确认后单独保存回传版本、来源行和原文件。空白不清除旧回传，0表示明确不补；重复同量导入不新增版本；冲突、非法数量、未知商品阻止提交。箱规或预算异常提示但不擅自修改实际回传量。原签发建议、模型结果和原加盟商导出不被回传覆盖。

## 三个遗留问题与验证

|问题|修复|实际证据|
|---|---|---|
|提交前取消仍生成归档|取消贯穿准备、编码、备份、事务前、等待锁、取得锁和最终提交前；可取消阶段整体回滚。最后检查点之后进入不可中断COMMIT，界面禁用取消；完成后取消仅停止剩余Excel。保留多店原子事务和安全备份。|`fixes.json`覆盖各边界及重试；`races.json`真实14244行定时取消未进入事务；`fixes_ui.json`实际EXE取消、按钮状态、已提交后取消、原修订重试。|
|批量覆盖只有总额|后台复用`revision_diff`，按选店顺序展示修订、金额、商品增删/数量/金额、参数、来源、首铺和关注变化。最多200条且显式截断，完整Excel可导出；默认取消，CAS绑定预览修订。|同为12元的moving 5→0、new_must 0→6、new_anomaly 1→0在实际EXE确认框可见；过期预览拒绝提交、失败多店回滚见`controls.json`、`selected_controls.json`。|
|最低持有归因不准确|复用AR/BI/BP/BQ/BR/BU/AS判断保底是否实际抬高最终建议；不重算模型。预算、首铺、一箱、极端缺货等按生效原因解释；缺少旧中间字段时保守标为补货建议。|AR=78、BQ=6、AS=78改为动销补货；AR=AS=BQ且无抬高不归为最低持有。单件/整箱、实际保底、零销/极端、首铺/一箱、预算部分/暂缓、旧字段缺失均通过，见`fixes.json`。|

取消定时复现本轮记录：请求在{race['timed_cancellation'][0]['seconds']:.3f}秒，安全退出在{race['timed_cancellation'][1]['seconds']:.3f}秒，未BEGIN、未生成草稿/正常单/修订/业务事件/Excel。**可能留下安全备份和物理存储元数据**，不宣称数据库字节完全不变。已提交后导出取消保留原修订和已完成文件，重试不另建修订。

并发读回复验：A提交V001，B在A读回前提交V002；A读回、Excel和导出日志仍为V001，最新头为V002。见`races.json`，没有通过读取“最新单”绕过绑定。

## 等价范围与允许变化

构建提交对固定基线重新独立运行，共{len(eq['cases'])}项有序对照。14244行真实样本覆盖不限预算、0/3000/5000/10000、关闭一箱例外、自定义参数，以及单/双仓、多文件重复/冲突/异常。比较输入、中间值、原建议、候选、执行量与金额、路由、状态、来源、审计，列表/字典顺序和值类型严格一致；不按商品键重排，不放宽浮点容差。另检查门店+商品唯一性和完整性。

保存、重复保存、签发、单店修订、修订冲突的完整业务SQL、解码快照、ID和事件同样对照。**未允许的差异为{eq['unexpected_differences']}条**。模型、最低恢复、一箱例外、预算实现、输入匹配、仓路由和默认配置核心文件与基线逐字节一致，哈希见`source_audit.json`。

五店当前规则冻结快照{real['rows']:,}行以及旧格式历史快照各独立导出比较：工作表/列/行/商品顺序、值、类型、格式、批注、自定义属性均核验。当前快照仅{len(real['allowed_reason_changes']):,}个、旧快照仅{len(legacy['allowed_reason_changes']):,}个V列“决策理由”文本发生授权修正，其余业务差异0。逐单元格的门店、SKU、原/新理由在`real_exports.json`和`legacy_exports.json`，两个集合来自同一五店业务基底的不同存储/规则快照，不能相加当作独立商品数。

允许变化仅为：取消行为、批量签发预览、V列理由文本、新增业务表单/原文件附件/独立回传表及其事件。**没有新增模型归因字段，没有扩大业务指纹忽略字段。** 原加盟商导出22列和中心仓筛选不变。N>30且J>0仅有效N按0、原N保留的0.8.0规则继续保留。

动态字段精确白名单（原验证器定义，逐字段，不整块忽略元数据）：

```json
{whitelist}
```

存储对照时固定相同时间/UUID而非忽略；物理分块解码后比较全部业务内容。内部Excel动态规范化仅针对B列：`运行概览`中A列为“计算时间”“软件版本”的值；A列为“构建信息”时仅其version/source_commit/build_id/built_at子字段；`版本及参数`中A列为“导出时间”或以“ / version”“ / source_commit”“ / build_id”“ / built_at”结尾的值。模型/仓路由/schema版本仍严格比较。V列仅允许文本变化且逐条列出，不忽略该列类型/样式/结构。原输入、旧0.8.0交付文件/ZIP、日常配置及数据库哈希核对见`preservation.json`。

## 性能实测

同机、同Python和依赖、同一当前0.8.0模型五店84,693行快照，旧/新按AB、BA、AB顺序逐个运行三轮，没有并发测试争抢。未清空Windows文件缓存，含预热/缓存影响，不代表冷磁盘或长期历史规模。完整阶段包含spawn、IPC、顺序装配和回收；“内部”两项只计算已建会话内时间，不能相加当成独立全流程。

秒；括号为三次最小～最大。正百分比表示更慢。

|阶段|0.8.0中位数（范围）|0.9.0中位数（范围）|耗时变化|
|---|---:|---:|---:|
{chr(10).join(timing)}

这次以正确取消、可核对和可追溯为目标，**不能宣称保存/归档/导出大幅提速**。新增差异核对需比较商品及来源；完整准备仍遍历快照，已签发导出仍需准备和Excel写出。小幅变化需结合三次范围判断，未验证超大历史、多用户长期使用的速度。

进程树RSS采样峰值的三次中位数，单位MiB。每50ms采样主进程+全部自有子进程；共享页会重复计数，这不是独占物理内存。内部子阶段未单独标记内存，不伪造数值。

|阶段|0.8.0|0.9.0|差值|
|---|---:|---:|---:|
{chr(10).join(memory)}

全部原始时间（按各版本实际第1/2/3次运行）：

|阶段|0.8.0三次秒数|0.9.0三次秒数|
|---|---|---|
{chr(10).join(raw)}

各次原始内存、源码哈希和进度在`performance-old/new-0/1/2-result.json`与对应`progress.json`；完整环境在`performance.json`。三个新版本运行起止源码哈希一致，并与实际构建源码核对。

## 实际EXE验收

使用干净Windows PATH和独立`REPLENISHMENT_DATA_DIR`，实际启动候选EXE，非源码冒充打包验证；以下报告绑定同一EXE SHA256及构建提交。

- `business_ui.json`：原资料导入留存、六类原生拖拽事件和模板下载、表单/完整原表/Excel、回传预览取消/确认/重复导入、独立数量展示、日志查询导出及定位原修订。已查看实际截图，中文正常可读。
- `fixes_ui.json`：编码及等待写锁取消、提交时按钮禁用、提交后写Excel取消、原修订重试、实际批量同金额商品差异弹框。
- `normal_ui.json`、`delivery_ui.json`：原计算与新品选择流程、归档/回读、五店四进程继续排队、批量选择四店导出、未选店不签发、取消与重试、中心仓22列结构。
- `history_ui.json`：五店旧归档冷加载、搜索最新请求生效、23,579行明细反复开关20次、取消和读进程失败恢复、日志Excel和历史修订导出。
- `work_io_ui.json`：当前模型五店84,693行完整恢复/保存、同日覆盖取消、归档、五份确认单导出、再读回逐字段相等、关闭中取消、进度和无孤儿进程。
- `storage_ui.json`：实际旧格式兼容、转换副本/取消、维护入口；新增附件/回传在旧格式转换副本中保留另由`business.json`验证。

实际EXE业务表单测试最大Qt计时器间隔{business['max_tick_gap_seconds']:.3f}秒，取消/提交测试{fixes['max_ui_gap_seconds']:.3f}秒；五店历史首次索引{history['initial_list_seconds']:.3f}秒、明细重复打开范围{min(history['detail_times']):.3f}～{max(history['detail_times']):.3f}秒、历史测试最大间隔{history['max_tick_gap_seconds']:.3f}秒。完整作业所有阶段中的最大间隔为{max(v['max_ui_gap_seconds'] for v in work['timings'].values()):.3f}秒，完整数据接收仍会有短暂停顿；进程树采样峰值最高{max(work['peak_tree_mib'].values()):.1f}MiB，不能宣称内存成本已解决。各阶段响应和耗时原始值见`work_io_ui.json`，UI响应不等于操作已经完成。

## 兼容与未纳入事项

- 旧归档未保存的原Excel明确显示“历史原文件未留存”；已有字段快照继续可读，不编造全量历史原表。从旧配置沿用、尚未在新版重新导入的商品资料也可能没有原文件附件；如需后续单据查看原表全部列，请在新版分别重新导入两个仓的资料。新导入原文件只留存一次，库会随新原文件增加；本版不自动清理历史附件。
- 加盟商回传是事实记录，不自动规整箱规、不重新分配预算；新版签发修订不自动继承旧修订回传。仅备注变化/全空无量变化不建立数量新版本。
- 这是本地正常单确认链路；实际下单数、实际收货数及跨系统历史资料关联尚未接入。无销量统一首铺、8.5天目标是后续规则任务，本版未实施。
- 旧EXE不理解新增业务入口；试用前可复制用户数据作独立备份，不能把新旧模型版本互认当作已验证。原0.8.0包和日常资料本轮未被改写。
- 初始测试中遇到过测试夹具/断言格式问题：jobs-only数据库不能当作已签发库、5.0显示不可用字符串“5 | 0”核对、提交前首次进度不能继续当作“已提交Excel进度”。已纠正测试定位并在新QA目录重跑；失败日志保留，没有通过修改计算预期迎合实现。

## 可复跑

在项目根目录使用`.venv\\Scripts\\python.exe`。每次输出必须是新目录；基线源码来自`git archive e45b9d0`，不得替换当前工作区。完整命令见`docs/0.9.0业务日志与回传.md`。关键命令如下：

```powershell
.venv\\Scripts\\python.exe tools/verify_090_fixes.py --fixture qa/business_090/normal/payload.json --output qa/recheck090/fixes
.venv\\Scripts\\python.exe tools/verify_business_090.py --fixture qa/business_090/normal/payload.json --output qa/recheck090/business
.venv\\Scripts\\python.exe tools/verify_090_equivalence.py --baseline qa/business_090/baseline --fixture qa/business_090/normal/payload.json --output qa/recheck090/equivalence
.venv\\Scripts\\python.exe tools/verify_090_races.py --fixture qa/business_090/normal/payload.json --real-result qa/recheck090/equivalence/new/real_None_True.pklz --output qa/recheck090/races
.venv\\Scripts\\python.exe tools/verify_090_real_exports.py --baseline qa/business_090/baseline --snapshot qa/business_090/performance1/old-0/settings/workbench.sqlite3 --output qa/recheck090/real-exports
.venv\\Scripts\\python.exe tools/benchmark_090.py --baseline qa/business_090/baseline --snapshot qa/storage_080/real_current/settings/workbench.sqlite3 --output qa/recheck090/performance --runs 3
.venv\\Scripts\\python.exe tools/verify_business_exe.py --exe 交付_0.9.0_rc1/补货助手/补货助手.exe --fixture qa/business_090/normal/payload.json --output qa/recheck090/exe-business
.venv\\Scripts\\python.exe tools/verify_business_exe.py --exe 交付_0.9.0_rc1/补货助手/补货助手.exe --fixture qa/business_090/normal/payload.json --real-result qa/recheck090/equivalence/new/real_None_True.pklz --output qa/recheck090/exe-fixes
.venv\\Scripts\\python.exe tools/verify_work_io_exe.py --exe 交付_0.9.0_rc1/补货助手/补货助手.exe --snapshot qa/storage_080/real_current/settings/workbench.sqlite3 --output qa/recheck090/exe-work-io
```

本地冻结输入和完整QA目录为`{qa}`；业务输入没有上传Git。发行目录只纳入报告、哈希、截图及可复跑脚本引用。新机器复跑须先取得同一哈希的授权业务输入，不能把合成样本当真实数据替代。
"""
    output.write_text(text, encoding='utf-8', newline='\n')
    print(output)


if __name__ == '__main__':
    main()
