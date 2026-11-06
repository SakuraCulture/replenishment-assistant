"""Write the final report from sealed measurements, never from projected gains."""
import hashlib,json,statistics,subprocess
from pathlib import Path
from run_deletion_0462 import CASES
ROOT=Path(__file__).resolve().parents[1];R=ROOT/'releases/0.46.2/rc1'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def main():
    build=read(R/'build_info.json');manifest=read(R/'manifest.json');native=read(R/'native-fixture.json');frozen=read(R/'frozen.json')
    labels=['历史派生库，有缓存，1 父单','原生合成大库，有缓存，1 父单','历史派生库，无缓存，1 父单','原生合成大库，无缓存，1 父单',
            '历史派生库，有缓存，3 天 3 父单','原生合成大库，有缓存，3 天 3 父单','小型合成库，无缓存，1 父单']
    rows=[];stages=[];summaries={}
    for label,(name,_,rounds,*_) in zip(labels,CASES):
        report=read(R/(name+'-performance.json'));summaries[name]=report['summary']
        def pair(key,scale=1):return ' → '.join(f"{report['summary'][side][key]['median']/scale:.2f}" for side in ('before','after'))
        rows.append(f"| {label} | {rounds} | {pair('total_seconds')} | {pair('peak_rss',2**20)} | {pair('read_bytes',2**20)} | {pair('write_bytes',2**20)} |")
        for side in ('before','after'):
            runs=[x for x in report['runs'] if x['side']==side]
            stages.append({'case':name,'side':side,'seconds':[x['total_seconds'] for x in runs],
                           'rss_at_start_median_mib':statistics.median(x['rss_at_start']/2**20 for x in runs),
                           'scratch_prepared_median_mib':statistics.median(x['scratch_prepared_bytes']/2**20 for x in runs),
                           'stages':{k:statistics.median(x['stages'][k]['seconds'] for x in runs) for k in runs[0]['stages']}})
    (R/'stage-summary.json').write_text(json.dumps(stages,ensure_ascii=False,indent=2),encoding='utf-8')
    core={}
    for path,digest in frozen['protected'].items():
        p=Path(path)
        if p.is_relative_to(ROOT/'src'):core[str(p.relative_to(ROOT))]={'before':digest,'after':hashlib.sha256(p.read_bytes()).hexdigest()}
    components=subprocess.check_output(['git','diff','--name-only',frozen['commit'],build['source_commit'],'--','components'],cwd=ROOT,text=True).strip()
    audit={'passed':not components and all(v['before']==v['after'] for v in core.values()),'core':core,'component_changes':components}
    assert audit['passed'];(R/'source-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    def improvement(name,key):
        s=summaries[name];return (1-s['after'][key]['median']/s['before'][key]['median'])*100
    text=f'''# 0.46.2 rc1 交付报告

本轮实现引用缓存增量暂存和快照流式重写，减少删除过程中的重复工作。按最终同机对照，历史派生大库有缓存单父单耗时下降 {improvement('derived-warm','total_seconds'):.1f}%，原生合成大库同场景下降 {improvement('native-warm','total_seconds'):.1f}%。具体内存、读写及其他场景见下表，不推广为低配置业务电脑承诺。

## 版本与范围

- 分支：`perf/deletion-stream-0.46.2`；基线：`{frozen['commit']}`（0.46.1 rc2）。
- 最终源码：`{build['source_commit']}`，从固定 Git 归档独立构建。应用 0.46.2，模型 0.3.4-rules，归属 0.5.1，参数/作业/存储格式 3/1/2。
- 改动集中在 `src/data_maintenance.py` 和 `src/maintenance_storage.py`，另更新版本、文档及隔离验收工具。计算、预算、箱规、默认参数与归属核心哈希保持；浏览器组件无差异，见 [源码核验](source-audit.json)。

## 实际瓶颈和处理

原流程把整份维护引用缓存复制到临时库，核验后又逐条比对发布。现在源缓存只读查询，只把缺失或失效条目写入临时库并在删除成功的事务中发布。所有源块的压缩摘要、缓存校验值和失效时的原内容校验仍执行，引用核验范围没有缩小。

原快照先完整校验、再按门店筛选、最后计算输出摘要，多个阶段反复读取和解压同一批内容。现在对当前写入器的有序字典结构，在流式遍历中同时计算原摘要、筛选及输出摘要；删除掉的字段也进入原摘要。宽映射、整体块及特殊容器沿用现有的当前格式处理路径。原始字节缓存仍有 8 MiB 上限，未把整天 JSON 装入内存。

同一天重复快照复用是已有能力，本轮未重复计作新增收益。全量清单校验（含待回收记录）、内容块校验、并发 data_version 失效及写锁内记录复核保留。未改共享父单删除范围、未选订单/门店、共享原件和平台订单保护；取消、失败回滚、重试和一次事务提交保留。没有修改 secure_delete、增加 VACUUM、延迟回收或分批提交。

## 最终新旧对照

下表为 **0.46.1 → 0.46.2**，各组使用相同输入，按 AB/BA 交替串行运行。数值为中位数，次数是每个版本的运行次数。

| 场景 | 次数 | 耗时（秒） | 工作进程峰值 RSS（MiB） | 进程读取（MiB） | 进程写入（MiB） |
| --- | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

阶段耗时、每次原始值、准备完成时暂存库大小及开始计时时的 RSS 见 [阶段汇总](stage-summary.json)。各场景原始记录对应 `*-performance.json`，全部预览 token、删除结果、删除前后逐表有序业务签名零差异；没有增加业务字段忽略项。

耗时含预览、准备、事务执行与提交，不含复制输入和验收签名计算。RSS 为工作进程 30 毫秒采样峰值并包含起始读数，不是整个应用或整机；验收辅助处理可能影响起始常驻内存，因此同时保留起始值。Windows 进程 I/O 包含系统缓存，不是物理磁盘占用率；未强制清空系统缓存。暂存库只记录准备结束时文件大小，不代表所有日志文件的峰值。

## 输入与结果等价性

历史派生库为 0.46.1 已使用的冻结 QA 副本，只含当前格式；历史转换产生的块划分与新建库不同。原冻结样本与派生输入本轮均不修改，不能将此结果直接与含旧快照的 37 秒或 24 秒相除。

原生合成库由固定 0.46.1 正常编码器直接写入，没有旧格式转换。包含 3 天、每天 8 店、每店 12000 行；主库 {native['database_bytes']/2**20:.2f} MiB，{native['chunks']:,} 个内容块。随机追踪字段仅用于放大输入体积，签发行采用固定测试夹具；这是负载验证，不是真实业务还原或模型计算验收。小型合成库沿用固定当前格式 QA 输入。

除既有签名比较外，48 组不同结构的当前格式快照与固定旧实现对照，完整规范化内容、摘要和长度一致：[差分验证](differential.json)。七组最终候选结果各取一轮，使用原 Reader 独立重读所有存活快照，校验真实字节摘要、内容块和精确存活集合，均通过（`*-audit.json`）；不以新算法声明的摘要代替内容验证。

## 回归与实际 EXE

- 完整 Python 回归 **1044 项通过**：[日志](regression.txt)。删除专项 49 项通过，新增 9 项覆盖缓存暂存边界、不同块形态、保存的字段映射、原摘要不符、被删除分支损坏以及缓存命中时的逐引用长度校验。
- 同一最终 EXE：删除 8 项、备份界面/CLI 7 项、采集恢复/自动配置界面 10 项通过：[删除](exe-deletion.json)、[备份](exe-storage.json)、[采集恢复](exe-recovery.json)。包含取消、关闭进度安全取消、重试、未选订单和队列保护。
- EXE 验收全程隔离数据库、离线组件及模拟身份回执，没有调用真实平台。聚水潭两仓浏览器配置和异常任务恢复继续保留；未将此前 Node 或真实本机浏览器验收冒充本轮重新执行。
- 性能与 EXE 验收工具超时不强杀可能正在提交的删除进程；超时会保留运行状态记录供核对。

## 限制与保留证据

内存变动以表中实测为准，本版不宣称所有场景都有内存下降。首次无缓存仍需校验历史内容，删除仍有与全库共享资料有关的开销。未在低配置业务电脑实测，无缓存及多父单各两组，样本数量仍有限。

初版流式处理预先读取共享配置，暴露了内容块缓存命中时未再次核对引用声明长度的问题。新增负面用例在初版失败：[复现](exploratory/cached-length-before-fix.txt)。补上逐引用检查后通过，并在新目录重跑全部 1044 项回归、49 项删除专项及七组对照。初版未打包；测量中的任务自然结束，下一任务在打开数据库之前被停止，没有强杀提交。初版两组已完成对照与单组试验均保留在 [探索记录](exploratory/README.txt) 及 `qa/deletion_0462`，不用于最终结论。

既有 `browser_status_ui` 的 QLabel 销毁后回调问题本轮未修复，不宣称已消除。0.46.1 rc1 清单校验遗漏的失败记录与修复、0.45.1 误用 EXE 样本的失败记录均保留在各自原发行目录。

## 交付

- 程序：`交付_0.46.2_rc1/补货助手/补货助手.exe`。
- ZIP：`交付_0.46.2_rc1/补货助手_0.46.2_rc1_删除流式处理优化.zip`，完整解压后运行，保留全部子目录。
- EXE SHA-256：`{manifest['exe_sha256']}`。
- ZIP SHA-256：`{manifest['zip_sha256']}`。
- [构建记录](build_info.json)、[文件清单](manifest.json)、[源码差异](runtime.patch)。最终 QA 为 `qa/deletion_0462_final`，发行证据为 `releases/0.46.2/rc1`。
- 所有既有交付保留；未推送、未合并，未修改真实业务库或平台数据。
'''
    (R/'交付报告.md').write_text(text,encoding='utf-8');print('Wrote 0.46.2 rc1 report')
if __name__=='__main__':main()
