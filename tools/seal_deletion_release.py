"""Seal validated 0.32.1 candidate without replacing any delivery or user data."""
import hashlib,json,shutil,sqlite3,statistics,zipfile
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1];Q=ROOT/'qa/deletion_0321';OUT=ROOT/'releases/0.32.1/rc3';DELIVERY=ROOT/'交付_0.32.1_rc3/补货助手'
def read(path):return json.loads(Path(path).read_text('utf-8'))
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()
def save(path,obj):Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2),'utf-8')
def main():
    equivalent=read(Q/'final-equivalence.json');exe=read(Q/'exe-rc3/deletion-report.json')
    if not equivalent['passed'] or not exe['passed'] or not exe['frozen_exe']:raise ValueError('Required comparison/EXE evidence missing or failed')
    logs=(Q/'all-tests-final.txt').read_bytes()
    if b'Ran 657 tests' not in logs or b'OK' not in logs or b'FAILED' in logs:raise ValueError('Full regression did not pass')
    baseline=read(Q/'baseline.json');changed=[p for p,h in baseline['protected'].items() if sha(p)!=h]
    if changed:raise ValueError('Protected files changed: '+repr(changed))
    small=read(Q/'small-final/report.json');warm=read(Q/'large-warm-final/report.json');cold=read(Q/'large-cold-final/report.json')
    signatures=[r['result']['signature'] for r in small['records']]
    if any(s!=signatures[0] for s in signatures):raise ValueError('Deletion business tables differ')
    if any(r['exit'] or not r['result'] for r in warm['records']+cold['records']):raise ValueError('Large deletion incomplete')
    if any(r['result']['signature']!=cold['records'][0]['result']['signature'] for r in warm['records']):raise ValueError('Warm/cold business outcomes differ')
    with closing(sqlite3.connect(ROOT/'qa/delete_diagnosis_20261103/frozen/workbench.sqlite3')) as a,closing(sqlite3.connect(Q/'large-cold-final/candidate-1/settings/workbench.sqlite3')) as b:
        old={r[0]:r for r in a.execute('SELECT oid,day,store,number,current FROM normal_orders')};new={r[0]:r for r in b.execute('SELECT oid,day,store,number,current FROM normal_orders')}
        survivors={'deleted_parents':len(set(old)-set(new)),'remaining_orders':len(new),'parent_changes':[o for o,r in new.items() if old.get(o)!=r],'signed_changes':[]}
        for row in b.execute('SELECT * FROM normal_revisions'):
            if row!=a.execute('SELECT * FROM normal_revisions WHERE oid=? AND revision=?',row[:2]).fetchone():survivors['signed_changes'].append(row[:2])
        if survivors['deleted_parents']!=1 or survivors['parent_changes'] or survivors['signed_changes']:raise ValueError('Surviving signed data changed')
    OUT.mkdir(parents=True,exist_ok=False)
    for src,name in [(Q/'final-equivalence.json','equivalence.json'),(Q/'all-tests-final.txt','regression.txt'),(Q/'special-tests-r5.txt','deletion-regression.txt'),(Q/'exe-rc3/deletion-report.json','exe-report.json')]:shutil.copy2(src,OUT/name)
    for name in ('delete-preview.png','after-delete.png'):shutil.copy2(Q/'exe-rc3'/name,OUT/name)
    save(OUT/'performance.json',{'small_AB_BA_AB':small,'large_cold_n1':cold,'large_verified_cache_n3':warm,'legacy_diagnosis_not_completed':read(ROOT/'qa/delete_diagnosis_20261103/run1/stopped.json'),'warmup_note':'Same frozen business DB; only verified reference-cache rows copied from previous successful isolated deletion. OS file caches not flushed. Prior large-warm run excluded: last sample overlapped Qt walkthrough; large-warm-final is sequential. Small/warm timings preceded the final surviving-draft validation guard; final cold, regression and EXE include it.'})
    save(OUT/'deletion-equivalence.json',{'small_business_table_signature_equal':True,'small_records':6,'large_survivors':survivors,'warm_cold_final_signature_equal':True,'exclusions':['jobs.revision: regenerated CAS token','jobs.saved: operation wall clock','snapshot_chunks: representation/content-addressed packing','snapshot_storage_meta: storage metadata','maintenance_chunk_refs and maintenance_legacy_refs: derived reference caches'],'method':'SQL column order, rowid sequence, scalar types and values hashed; snapshot payloads compared by canonical JSON SHA preserving numeric/null/string distinctions and arrays. All surviving large signed payloads compared byte-for-byte. Large old full deletion could not complete; no claimed full old/new large outcome comparison.'})
    zip_path=DELIVERY.parent/'补货助手_0.32.1_rc3_彻底删除修复.zip'
    files={str(p.relative_to(DELIVERY)).replace('\\','/'):sha(p) for p in sorted(DELIVERY.rglob('*')) if p.is_file()}
    with zipfile.ZipFile(zip_path,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for rel in files:z.write(DELIVERY/rel,'补货助手/'+rel)
    with zipfile.ZipFile(zip_path) as z:
        if len(z.namelist())!=len(files):raise ValueError('ZIP file count mismatch')
        for rel,digest in files.items():
            if hashlib.sha256(z.read('补货助手/'+rel)).hexdigest()!=digest:raise ValueError('ZIP file mismatch: '+rel)
    manifest={'source_commit':exe['source_commit'],'baseline_commit':baseline['commit'],'delivery':str(DELIVERY),'exe_sha256':sha(DELIVERY/'补货助手.exe'),'zip':str(zip_path),'zip_sha256':sha(zip_path),'files':files,'file_count':len(files),'protected_changed':changed,'core_sha256':baseline['core']};save(OUT/'delivery-manifest.json',manifest)
    def series(records,variant):return [r for r in records if r['variant']==variant]
    def metrics(rows):
        t=[r['result']['times']['total'] for r in rows];m=[r['rss_peak_sum']/1048576 for r in rows]
        return f"{statistics.median(t):.2f}（{min(t):.2f}–{max(t):.2f}）",f"{statistics.median(m):.2f}（{min(m):.2f}–{max(m):.2f}）"
    b_t,b_m=metrics(series(small['records'],'baseline'));n_t,n_m=metrics(series(small['records'],'candidate'));c_t,c_m=metrics(cold['records']);w_t,w_m=metrics(warm['records'])
    report=f'''# 0.32.1 rc3 彻底删除修复交付

本地分支 `fix/order-deletion-0.32.1`；基线 `{baseline['commit']}`。实际EXE从干净提交 `{exe['source_commit']}` 构建，随后提交本报告与QA工具，不合并、不推送。保留0.32.0及构建检查用rc1/rc2；本次试用使用rc3。

## 已完成

修复完整快照反复展开、事务内重编码、全部历史引用常驻内存、内层循环不能及时取消、删除后整份工作区重建。新增有界分块编辑及旧JSON流式解析、按内容校验的引用缓存、写锁前准备与精确CAS、作业子进程及真实阶段进度。删除范围、外部库存占用及共享文件保留规则不变。

准备/读取/等待写锁/事务中取消均安全退出或回滚，进入最终提交后不中断；已提交而刷新失败会明确区分，避免重复删除。其他门店控件、参数、签发及当前结果保留。

## 验证证据

- 657项全套回归通过；20项新增专项通过。包含新旧格式等价、空值/日期/顺序、损坏块、引用缓存、取消后重试、等待写锁、提交边界、并发新增共享作业拒绝、剩余草稿及日期校验。原删除测试继续覆盖双仓、外部锁单占用和跨库失败回滚。
- 固定0.32.0源码独立运行，14244行真实样本、多个预算/关闭例外、自定义参数、单双仓、冲突及异常等38份有序结果/Excel比较，差异0。业务输入、中间值、建议、执行、金额、来源与原表格结构严格比较；仅既有精确时钟/进程/应用构建白名单。模型、预算、一箱例外、默认配置4文件SHA不变。
- 真实小库AB/BA/AB三轮删除后的所有业务表摘要一致。SQL类型、列顺序、行顺序及逻辑快照摘要参与比较；排除字段详见deletion-equivalence.json，无放宽数值容差。
- 大库成功删除1个门店当天单据后，其余38个父单及存活签发记录逐条原始字节不变；有/无引用缓存最终业务表一致。旧版大库删除未能完成，因此不宣称大库全量旧/新最终结果已对照成功。
- 实际 Windows EXE：两店spawn计算、保存签发、预览取消、处理中关闭安全取消、再次执行成功、旧格式解析C扩展、未选店签发/当前计算及界面刷新、重复删除已不存在目标返回异常。`frozen_exe=true`，{exe['heartbeat_ticks']}次25ms计时器回调，整个脚本走查最大间隔{exe['max_gap_seconds']:.3f}秒；包含截图与同步校验，不等于大库人工体感测试。
- 冻结资料及旧EXE/ZIP校验无变化。测试均在QA副本，不删除真实用户订单，不重启用户应用，不连接翱象/聚水潭平台。

## 性能

同电脑、同解释器及依赖。小库14244行真实计算归档；大库39个正常单、11份共享作业、3份草稿，主库约980MiB，派生索引约721MiB，分块声明解压体积约5.34GiB，另有大旧格式快照。

| 场景 | 删除流程秒：中位数（最小–最大） | 处理进程RSS MiB：中位数（最小–最大） |
|---|---:|---:|
| 小库旧版，3次 | {b_t} | {b_m} |
| 小库新版，3次 | {n_t} | {n_m} |
| 大库新版首次引用检查，1次 | {c_t} | {c_m} |
| 同大库复用已验证引用缓存，3次 | {w_t} | {w_m} |

小库耗时中位缩短约56%，内存峰值中位约减少93%。大库初次仍需数分钟核对共享引用；复用后仍有几十秒开销，不能称秒删。旧版诊断运行293.60秒未完成，当时工作集17.46GiB，已安全停止QA进程；它不是成功耗时或连续采样峰值，不能拿来宣称固定提速倍数。

操作计时包括预览、准备、写锁、事务、引用清理和索引，不含QA复制/校验、人工确认或Qt列表刷新；完整进程耗时另在JSON。每50ms采样处理进程及后代RSS求和，共享页可能重复；不代表已有用户主窗口常驻内存。未清OS文件缓存，不是磁盘冷读测试。复用组只复制经过校验的派生缓存到同一业务冻结库，业务输入没有变化。全部原始值及阶段耗时在performance.json。

## 失败与限制

第一轮大库候选触及3GiB保护阈值，定位并修复了旧JSON全量展开；失败目录保留。原对照首次未按既有工具要求固定PYTHONHASHSEED，出现两处分类映射字典插入顺序差异；按原流程给双方固定0后重新捕获，严格比较0差异，未排序掩盖。新增剩余草稿测试首次调用了不存在的测试API，纠正为现有save_job后通过，未改业务预期。进度节流专项首次把后续独立读回的进度也计入写入阶段，拆开测量阶段后通过；原始失败记录保留。

没有重新做平台真实联调、新电脑重建或业务人员无指导走查；本次不涉及平台或计算功能修改。异常退出可能留下系统临时准备文件。删除不擦除磁盘原Excel、外部订单或旧备份，SQLite文件不保证立刻缩小。首次共享引用检查仍是剩余耗时主项。

## 交付及复跑

程序：`{DELIVERY / '补货助手.exe'}`。

ZIP：`{zip_path}`。共{len(files)}文件，ZIP内逐文件SHA一致。

EXE SHA-256：`{manifest['exe_sha256']}`。

ZIP SHA-256：`{manifest['zip_sha256']}`。

```powershell
.venv\\Scripts\\python.exe -m unittest discover -s tests
.venv\\Scripts\\python.exe tools/verify_deletion_equivalence.py --name 新的对照名 --capture
.venv\\Scripts\\python.exe tools/deletion_qa.py run --fixture small --rounds 3 --name 新的小库测试名
.venv\\Scripts\\python.exe tools/deletion_qa.py run --fixture large --only candidate --rounds 1 --name 新的大库测试名
```

大库与真实对照依赖本机QA冻结样本；便携单元测试自行生成数据。实际EXE命令见使用文档，必须使用独立REPLENISHMENT_DATA_DIR。完整原始证据位于`qa/deletion_0321`；发行目录保存汇总、全部性能原始值、严格对照、回归日志、EXE记录及交付清单。
'''
    (OUT/'交付报告.md').write_text(report,'utf-8')
    print(json.dumps({k:manifest[k] for k in ('source_commit','exe_sha256','zip_sha256','file_count')},ensure_ascii=False))
if __name__=='__main__':main()
