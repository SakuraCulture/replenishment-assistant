"""Business-facing release report based only on completed QA reports."""
import argparse,hashlib,json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def main():
    a=argparse.ArgumentParser();a.add_argument('--qa',type=Path,required=True);a.add_argument('--delivery',type=Path,required=True);args=a.parse_args();q=args.qa.resolve();delivery=args.delivery.resolve()
    meta=read(delivery/'版本信息.json');perf=read(q/'performance_release/report.json');ui=read(q/'exe-work-io/work_io_report.json');books=read(q/'workbooks.json');real=read(q/'real_current/report.json')
    assert all(x['passed'] for x in [perf,ui,books,real]) and perf['runs']==3
    names={'save':'首次保存','load':'完整恢复','copy_config':'复制门店配置','save_unchanged':'无变化再次保存','overwrite_cancel':'覆盖核对后取消','confirm':'首次确认归档','confirm_unchanged':'无变化再次归档','export_signed':'已归档五店导出'}
    lines=[];memory=[]
    for phase,title in names.items():
        old,new=[perf['summary'][phase][k] for k in ['old','new']];percent=(1-new['median']/old['median'])*100
        lines.append(f"|{title}|{old['median']:.2f}（{old['min']:.2f}–{old['max']:.2f}）|{new['median']:.2f}（{new['min']:.2f}–{new['max']:.2f}）|{'减少' if percent>=0 else '增加'}{abs(percent):.1f}%|")
        memory.append(f"|{title}|{old['peak_rss_mib_median']:.0f}|{new['peak_rss_mib_median']:.0f}|{new['peak_rss_mib_median']-old['peak_rss_mib_median']:+.0f}|")
    hashes={n:sha(ROOT/'src'/n) for n in ['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json','storage_chunks.py','storage_maintenance.py','normal_orders.py','work_repository.py']}
    protected=read(q/'protected_before.json');differences=[p for p,h in protected.items() if not Path(p).is_file() or sha(Path(p))!=h]
    preservation={'passed':not differences,'files':len(protected),'differences':differences,'scope':'original Excel inputs, daily user database, frozen legacy snapshot, old 0.7.4 delivery and ZIP'}
    (q/'protected_after.json').write_text(json.dumps(preservation,ensure_ascii=False,indent=2),encoding='utf-8');assert not differences
    unchanged={n:sha(ROOT/'src'/n)==sha(q/'baseline/src'/n) for n in ['one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']};assert all(unchanged.values())
    audit={'passed':True,'baseline_commit':'39f49442c66bec6885873f75858514406a43cf45','build_commit':meta['source_commit'],'current_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'hashes':hashes,'unchanged_business_files':unchanged,'exe_sha256':sha(delivery/'补货助手.exe')}
    (q/'source_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    uirows='\n'.join(f"|{key}|{v['seconds']:.2f}|{v['max_ui_gap_seconds']:.3f}|" for key,v in ui['timings'].items())
    report=f'''# 补货助手0.8.0 rc1交付与验收

后续三个阶段已实现并验证：分块存储/旧格式兼容；固定修订按需读取；副本迁移回退、性能与实际EXE验收。旧0.7.4和用户资料保留；仅本地独立交付，不合并、不推送。

## 规则修正单独核验

唯一规则变化：原缺货天数N>30且月销J>0时，有效缺货天数按0参与原预测公式，原N保留并注明冲突。日销/周销/月销预测公式、正常缺货恢复、零销量缺货恢复、新品识别/选择、箱规、预算优先级均不改。没有实施13天硬兜底、统一日均或缺货商品转首铺。

真实五店84,693条重新计算：13条命中，其他84,680条逐字段一致；全部商品最终建议、候选和执行量/金额均未变，总金额仍{real['sum_amount']:,.2f}元。13条的中间预测和原因允许按本次授权变化，详情见real_recalculation.json。合成案例旧28→新42证明修正实际生效；这不代表未来所有数据都不会改数量。模型0.3.1-rules，应用0.8.0，需求归属0.5.0。

## 存储实现和边界

SQLite业务表和逻辑schema1保持；新增不可变内容块表和存储元数据表，物理格式2。数组及大字典每256项一组，以规范JSON SHA256复用块。草稿/签发共享块，写入仍在原CAS事务内，完整原始数据和旧修订保留。首次写入前SQLite在线完整备份；不自动全库迁移，不自动删除旧块。

完整恢复校验所有块与整份规范JSON摘要，统一解析；复制配置、无变化核对和已签发导出按需读取。投影校验只覆盖清单及被读取块，未访问块的损坏由全读/维护转换检查，不能宣称投影完成了全库校验。首次保存与改动归档仍需遍历数据，完整恢复仍需传输全部快照。

首页维护只创建新副本，不切换当前数据库。旧EXE不能直接读分块记录；回退须生成旧格式兼容副本，关闭所有版本后放入独立资料目录。自动初始备份不包含后续作业，保留后续作业应使用完整兼容副本。模型不同的历史作业保持只读，继续工作先复制配置、重新计算。见随包0.8.0说明中的步骤。

## 正式三轮耗时

同一Windows电脑/Python依赖、固定旧0.7.4输入与参数，5店84,693行；新旧交替，逐个执行，未同时运行打包/其他重测试。包含spawn、IPC、核对、事务、写文件和子进程回收。数据初始化和事后验证不混入操作计时。OS缓存未清空；运行时间为秒，中位数（最小–最大）。未预设提速比例，不能外推其他电脑。

|操作|0.7.4|0.8.0|耗时变化|
|---|---:|---:|---:|
{chr(10).join(lines)}

三次独立目标数据库大小：旧{perf['database_bytes']['old']}字节，新{perf['database_bytes']['new']}字节。此规模只包含测试作业/修订，不代表长期历史增长上限。

## 内存

外部每50ms采样主进程和全部子进程RSS之和；共享页每进程重复计数，不等于唯一物理内存。以下是每轮峰值的三次中位数，MiB；解释器/GC会造成波动。

|操作|旧|新|差值|
|---|---:|---:|---:|
{chr(10).join(memory)}

## 等价与兼容证据

- 独立旧/新解释器、相同时钟/UUID和同一冻结结果：保存、重复保存、首次/重复签发、单店修订、并发修订冲突六场景。所有业务SQL行、ID/事件/修订、完整解码数据和Excel值/类型/样式/顺序0差异；无业务字段白名单。仅排除两个新增物理表，并把三类payload物理编码解码后比较。
- 六次真实五店流程：整份规范作业摘要、每店完整业务指纹、原表结构、单号/修订及文件名一致。该测试固定原模型身份以隔离存储变化；当前模型另以真实重算和定向规则对照验证。
- {len(books['checks'])}份工作簿对照、{sum(r['cells'] for r in books['checks']):,}个单元格：所有OOXML内容和条目顺序一致，仅排除core.xml的created/modified与ZIP容器元数据。批注、价格/状态格式、字段/表顺序都比较。
- 真实旧库→分块副本→旧格式副本，全原业务SQL行和完整payload摘要相同，源库哈希不变。另验证日期、长编码、空/0/False/0.0、空字串、块损坏拒绝、失败回滚、既有目标拒绝、取消不发布和当前库不变。
- 首次备份、修订冲突、准备取消、提交后取消导出、重试、死进程和64位进度均通过。模型/归档/导出不依赖当前用户目录测试。

## 实际EXE

从干净提交{meta['source_commit']}构建，EXE SHA256 `{audit['exe_sha256']}`。实际运行五组：正常单/新品预算、4槽5店自动补位及批量导出、旧库历史搜索/明细/导出、当前模型真实五店完整作业流程、首页维护及命令行升级/回退。使用私有测试目录，没有测试遗留子进程；未通过源码启动代替冻结程序验收。

以下为一次完整EXE流程观察，不当作三轮EXE对比；Qt每25ms记录响应间隔。

|操作|耗时秒|最长响应间隔秒|
|---|---:|---:|
{uirows}

每个IO阶段显示实际阶段/已用时间/最近进展；有计数时显示阶段内比例，否则活动条，不伪造预计百分比。提交开始后等待事务完成，读取与副本转换在安全边界取消。

## 版本与保留

基线39f4944；定向修正4dee6c8；分块存储0072672；按需读取abcd526；副本/性能修正与候选构建d8b35b6。阶段4包含预跑发现的大字典18万查询退化修正及统一完整解析。预跑仅用于诊断，不替代上面三轮正式数据。

{len(protected)}个受保护文件哈希一致，含原Excel、用户每日数据库、旧包和ZIP。one_box_exception.py、allocation.py、默认配置与必卖清单哈希不变；核心哈希见source_audit.json。本轮不处理外部确认/下单/收货历史关联，不增加并行导出或汇总页，不清理用户历史。

可复跑脚本和输入位置见docs/0.8.0分块存储与兼容.md。完整报告保留每轮耗时、内存与进度；所有日常数据及旧包未覆盖。
'''
    (q/'交付报告.md').write_text(report,encoding='utf-8',newline='\n');print('release report and protected hashes passed')

if __name__=='__main__':main()
