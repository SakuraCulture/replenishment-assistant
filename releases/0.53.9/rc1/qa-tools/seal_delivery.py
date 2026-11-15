"""Seal the tested independent recovery delivery; never changes old packages."""
import hashlib,json,shutil,subprocess,zipfile
from pathlib import Path

ROOT=Path.cwd().resolve()
QA=Path(__file__).resolve().parent
OUTPUT=QA.parents[1]
REL=ROOT/'releases/0.53.9/rc1'
DIST=OUTPUT/'交付_0.53.9_rc1'
BUILD=OUTPUT/'build_0.53.9_rc1'

def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def md(p,s):p.write_bytes(s.encode('utf-8'))

build=read(QA/'build-report-rc1.json');assert build['passed']
info=read(BUILD/'build_info.json')
reg=read(QA/'targeted-03.json');assert reg['passed'] and not reg['failures'] and not reg['errors']
native={name:read(QA/name/'report.json') for name in ('exe-recovery-01','exe-compatibility-01','exe-followup-01')}
for n,d in native.items():
    assert d['passed'] and d['exe_sha256']==build['exe_sha256'],n
    assert all(r['passed'] and r['exit_code']==0 for r in d['runs']),n
perf=read(QA/'comparison-03/report.json');ret=read(QA/'comparison-return-01/report.json')
assert perf['passed'] and ret['passed'] and all(perf['business_equivalence'].values()) and all(ret['business_equivalence'].values())
assert all(read(QA/'prebuild-boundaries.json')['performance_backend_equal'].values())
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip()==info['base_commit']
assert not subprocess.check_output(['git','diff','--cached'],cwd=ROOT)
exe=DIST/'补货助手/补货助手.exe';assert sha(exe)==build['exe_sha256']
manifest=read(BUILD/'source-manifest.json')
for name,digest in manifest.items():assert sha(BUILD/'source'/name)==digest,name
for name,digest in info['working_tree_files_sha256'].items():
    if name.startswith(('src/','components/','tools/','tests/')):assert sha(ROOT/name)==digest,name
old=read(ROOT/'releases/0.53.8/rc2/artifacts.json')
for key in ('exe','zip'):assert sha(Path(old[key]))==old[key+'_sha256'],'Old delivery changed'
REL.mkdir(parents=True,exist_ok=False)
copies={'build_info.json':BUILD/'build_info.json','source-manifest.json':BUILD/'source-manifest.json',
        'regression.json':QA/'targeted-03.json','performance-payment-receipt-jst.json':QA/'comparison-03/report.json',
        'performance-return-operations.json':QA/'comparison-return-01/report.json','boundaries.json':QA/'prebuild-boundaries.json'}
for name,d in native.items():copies[name+'.json']=QA/name/'report.json'
for name,p in copies.items():shutil.copy2(p,REL/name)
failures=[
 {'evidence':'targeted-01.json / targeted-process-01.log','kind':'验收装配错误','reason':'首版回归入口缺少子进程保护，签发测试启动的子进程重复装配了套件；加主入口保护后重新独立执行，最终330项通过。'},
 {'evidence':'source-ui-01','kind':'已修复的恢复界面问题','reason':'恢复列表函数未接受通用后台任务传入的progress/cancel参数；已补齐，真实控件路径最终通过。'},
 {'evidence':'source-ui-02','kind':'合成样本错误','reason':'测试付款样本漏必填单号，原校验正确拒绝；补齐样本，未放宽业务校验。'},
 {'evidence':'comparison-01','kind':'测量环境缺少依赖','reason':'采样进程未找到已有psutil；显式引用隔离依赖后重跑。'},
 {'evidence':'comparison-02','kind':'非最终性能证据','reason':'首版逐JSON token编码的检查点开销偏高，已改为按单/字段编码；该轮还因读取开发中的可变源码被一致性保护中止，不作为最终对照。'},
 {'evidence':'visible-source-02 / targeted-03 / comparison-03 / comparison-return-01','kind':'最终通过证据','reason':'固定核心源码、独立目录和输入后重新完成；早期失败、日志和输入均未覆盖。'}]
write(REL/'failure-evidence.json',failures)

rows=[]
def add(label,a,b,seconds):
    rows.append(f"| {label} | {a[seconds]:.3f} → {b[seconds]:.3f} | {a['peak_rss_mib']:.1f} → {b['peak_rss_mib']:.1f} | {a['process_read_mib']:.3f} → {b['process_read_mib']:.3f} | {a['process_write_mib']:.3f} → {b['process_write_mib']:.3f} |")
for label,key in [('门店回传首次保存','return_initial'),('运营回传首次保存','operations_initial'),('门店回传无变化保存','return_unchanged'),('运营回传无变化保存','operations_unchanged')]:
    v=ret['summary'][key];add(label,v['old'],v['new'],'save_seconds')
for label,key,phase in [('付款首次保存','payment','save'),('收货首次保存','receipt','save'),('付款无变化保存','payment','unchanged_save'),('收货无变化保存','receipt','unchanged_save'),('聚水潭生成提交','jst','save'),('聚水潭正常锁单','jst','lock_uncontended')]:
    v=perf['summary'][key][phase];add(label,v['old'],v['new'],'seconds')
counts={n:sum(len(r['checks']) for r in d['runs']) for n,d in native.items()}
report=f'''# 0.53.9 rc1 主业务生命周期恢复交付报告

已完成核查方案中四项已复现缺陷修复、旧备用重试入口的范围隔离，以及回传、运营签发、付款、收货的原保存任务恢复。独立包已验证；没有改动真实数据，没有创建提交、推送或合并，没有替换用户正在用的软件。

## 已确认问题及修复

1. 翱象混合批次取消未开始项时，旧代码会同时困住已受理项的结果核对。现在保留受理身份与原任务核对通道；逐行继续只处理该行，不重启已取消兄弟项。
2. 同一已取消预览再次确认，旧代码只返回取消记录而不安排执行。现在衔接同一原执行与冻结采购包，原已受理/未知请求仍先查原状态，不盲目重发。明确点击“恢复并继续原任务”可重新安排被取消的未开始项。
3. 新分配批次在第一张生成单保存前失败，曾使原批次一直暂停。现在同事务记录本次切换，仅在无已提交/已删除分配步骤、无后来人工控制变化时恢复本次造成的原暂停，不全局激活旧批次。
4. 分配单及库存预留已提交，而最后控制状态写入失败，旧代码可能把整次显示为失败并丢失回执。现在返回已保存清单与“批次状态待恢复”；原窗口或结果页“恢复分配状态”只补齐控制状态，不重复分配、预留或生成。
5. 旧备用勾选重试路径可能扩大成整批调度，已停用并明确引导到原批范围操作。当前常用界面原已走明确范围入口；没有证据证明此备用路径是业务现场事故原因。

这些是逻辑恢复缺陷，不等于已证明历史报错均由SQLite物理死锁导致。其他历史业务机事故未在本轮重新复现，不冒充已经全部解决。

## 四个环节如何恢复

回传、运营签发、付款和收货在原保存前留存必要的压缩核对内容及小型身份记录；保存成功后删除大预览，保留回执。无变化保存只留小型记录，不压缩整份明细，不为每次点击复制数据库。恢复材料只在需要时读取，不在启动时加载全部明细。

从本环节、批量导入页或“显示与帮助 → 恢复并继续原操作…”进入。先核对持久事实：整批已保存时直接取回原回执；尚未提交时恢复原核对内容，再走原完整校验。原保存批次仍原子处理，不自动跳过失败单或默默缩小范围。原来源、修订、上游、单据版本或删除代次变化则具体提示重核，不覆盖后来事实。签发与导出继续使用已有确切修订恢复入口，不新建一条签发链路。

真实关闭恢复窗口/主窗口时安全取消，退出阶段不再继续打开新的恢复子窗口；原材料重开可继续。单仓删除会清理其恢复材料，晚到结果不能复活已删仓，未选仓与其他订单保留。恢复界面只读/提交均使用原后台工作线程与取消通道。

## 影响范围与不变量

运行改动涉及翱象队列状态/调度/UI、聚水潭批次控制/恢复UI、生命周期保存包装与恢复服务/UI、删除后的恢复材料清理、入口与版号。业务计算文件src/model.py和src/default_config.json与实际0.53.8 rc2逐字节相同。数量、金额、币种、顺序、箱规、预算、供货归属、默认并发、事务原子保存和原校验不变。没有改业务表结构或存储格式；只使用已有批次事件表记录控制切换，新增数据目录下lifecycle_recovery侧目录。

任意追加冻结批次成员、自动作废外部单、强行回滚已付款/已收货事实、清锁文件及绕过活跃实例均不在本轮。旧版本没有记录的未保存编辑不能凭空恢复，可用原件或已存历史重新核对。完整订单迁移包保持原白名单，不把未完成本机预览搬到另一台机器隐式执行；整体数据位置搬迁会随目录保留。

## 新旧性能与内存

基线是实际0.53.8 rc2冻结源码，444个Python文件与本轮起点逐文件确认一致。6张仓单、12000行合成样本；相同机器、独立进程与数据库副本，新旧交替串行各三次，中位数如下。测量串行只为公平比较，不降低产品默认并发。

| 后端阶段 | 秒（旧→新） | 峰值RSS MiB（旧→新） | 进程读 MiB（旧→新） | 进程写 MiB（旧→新） |
| --- | ---: | ---: | ---: | ---: |
{chr(10).join(rows)}

恢复检查点让四环节首次保存增加约0.23～0.26秒、进程写入增加约0.61～0.83 MiB；保存过程峰值RSS增加约0.1～1.2 MiB。大预览编码在业务写锁外，另有短暂的恢复身份发布事务。回传/运营主要写锁区间为0.378→0.369秒、0.509→0.506秒；付款/收货测得0.638→0.641秒、0.774→0.800秒，不能声称锁耗时严格不变。无变化保存不做大预览持久化，时间接近原版，只增加约8 KiB状态写入。生成提交存在小幅波动，不能宣称全部提速或普遍减少内存。本轮收益是异常可恢复；首版逐token编码增加约0.8秒的问题已优化，再固定源码重测。

两个对照中完整计划、原保存回执、逐表有序业务事实/视图以及相应进度/边界签名均一致，没有金额容差、没有排序后掩盖业务顺序。核心被测后端与本包源码逐字节一致；后续只补退出中的UI保护和验收入口，不把可变开发源码混作最终测量。

RSS为20毫秒采样工作进程峰值，不是整机/整个应用树或理论瞬时峰值；Windows进程I/O含缓存，不等于物理磁盘负载。初次原件解析/副本准备在计时外，数字是后端入口，不包括Qt绘制、列表刷新或平台耗时。未在低配置业务电脑在线实测。

## 验证和保留的失败证据

- 最终{reg['tests']}项去重定向回归全部通过，失败/错误/跳过均为0。覆盖取消与再次确认、已受理/未知请求防重复、原范围、删除晚到回写、分配失败与恢复、部分成功继续、控制状态提交失败、上游冲突、四环节原校验、事务取消/回滚、签发及原修订导出恢复。不是仓库全量回归。
- 最终源码原生Qt验收53项通过，包含恢复中安全关闭。实际同一EXE恢复验收{counts['exe-recovery-01']}项、四环节/分配/锁单兼容{counts['exe-compatibility-01']}项、自然转付款/离线采购准备{counts['exe-followup-01']}项均通过；分套检查存在重复覆盖，不冒充独立场景总数。每套真实关闭重开，所有进程退出码0。
- 使用真实控件、后台线程、真实本地合成数据库和文件，确认弹窗由定时器实际点击，未把QMessageBox替换为空函数。异常在严格离线合成边界注入；采购仅使用标记的离线响应，未连接平台、未在线导出/上传。没有终止其他进程、没有读取真实业务库。
- 失败和最终证据均保留。首轮恢复界面参数不兼容已修复；初期缺失付款单号是样本错误，原校验保持；首次测试装配缺主入口保护已纠正；缺采样依赖和可变源码被一致性保护中止的对照不计为通过。详见failure-evidence.json及QA原目录。

## 版本、来源和交付

应用0.53.9 rc1，模型0.3.6-rules；分支codex/lifecycle-recovery-0.53.9，基线Git提交{info['base_commit']}。源码引用{build['source_reference']}，是未提交工作区的不可变源码快照，不是Git提交。source.zip、逐文件manifest和构建日志保留在{BUILD}。实际EXE SHA256：{build['exe_sha256']}。

独立目录：{DIST}。完整解压本目录ZIP后运行“补货助手/补货助手.exe”，不要只复制单个EXE。图文HTML教程和本版说明随包提供。ZIP与大小/摘要见artifacts.json和SHA256.txt。

完整QA：{QA}。原0.53.8 rc2的EXE及ZIP重新校验摘要一致；全部旧交付和历史证据保留。未提交、未推送、未合并，未自动替换现用软件，未操作真实订单或调用真实平台。
'''
md(REL/'交付报告.md',report)
md(DIST/'交付报告.md',report)
shutil.copy2(ROOT/'docs/0.53.9主业务生命周期恢复.md',DIST/'使用与恢复说明.md')
write(DIST/'验收摘要.json',{'passed':True,'regression_tests':reg['tests'],'native_checks_by_suite':counts,'live_platform':False,'source_reference':build['source_reference'],'exe_sha256':build['exe_sha256']})
archive=DIST/'补货助手_0.53.9_rc1_主业务生命周期恢复.zip'
assert not archive.exists()
members=sorted(p for p in DIST.rglob('*') if p.is_file() and p!=archive)
assert not any(p.relative_to(DIST).parts[0]=='settings' for p in members)
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in members:z.write(p,p.relative_to(DIST).as_posix())
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    for p in members:assert z.getinfo(p.relative_to(DIST).as_posix()).file_size==p.stat().st_size
artifacts={'passed':True,'version':'0.53.9','build_id':'rc1','branch':'codex/lifecycle-recovery-0.53.9','base_commit':info['base_commit'],
           'source_reference':build['source_reference'],'source_snapshot_sha256':build['source_snapshot_sha256'],'exe':str(exe),'exe_sha256':sha(exe),
           'zip':str(archive),'zip_sha256':sha(archive),'zip_bytes':archive.stat().st_size,'zip_members':len(members),'zip_crc_passed':True,
           'regression_unique_tests':reg['tests'],'native_checks_by_suite':counts,'report':str(REL/'交付报告.md'),'qa':str(QA),
           'old_0538_exe_zip_hashes_unchanged':True,'git_committed':False,'git_pushed':False,'git_merged':False}
write(REL/'artifacts.json',artifacts);write(DIST/'artifacts.json',artifacts)
for base in (REL,DIST):md(base/'SHA256.txt',f"{artifacts['exe_sha256']}  补货助手/补货助手.exe\n{artifacts['zip_sha256']}  {archive.name}\n")
write(QA/'final-artifacts.json',artifacts)
print(json.dumps(artifacts,ensure_ascii=False))
