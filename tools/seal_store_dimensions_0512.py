"""Seal immutable 0.51.2 store dimension exports after isolated EXE acceptance."""
import argparse,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path
from datetime import datetime
from aoxiang_bundle import verify
ROOT=Path(__file__).resolve().parents[1]
SOURCE='90d244defdbeed279a2feb1912a3e93ce868f1c2'
BASE='66f6c4f'
def read(p):return json.loads(p.read_text('utf-8'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def write(p,d):p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')

def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);a=p.parse_args();qa=a.qa.resolve();qa.relative_to(ROOT/'qa')
    build=ROOT/'build_0.51.2_rc1';delivery=ROOT/'交付_0.51.2_rc1';app=delivery/'补货助手';release=ROOT/'releases/0.51.2/rc1';archive=delivery/'补货助手_0.51.2_rc1.zip'
    if release.exists() or archive.exists():raise FileExistsError('Never overwrite a sealed release')
    info=read(build/'build_info.json');assert info['source_commit']==SOURCE and info['version']=='0.51.2'
    assert info['source_mode']=='immutable git archive; no working-tree overlay' and sha(build/'source.zip')==info['source_archive_sha256']
    assert read(qa/'immutable-build.json')['passed'];assert not git('diff',SOURCE,'--','src','tests','components').strip()
    full=read(qa/'full-python.json');assert full['passed'] and full['exit_code']==0 and full['tests']==1469 and full['source_commit']==SOURCE
    exe=read(qa/'exe-01/acceptance.json');exe_sha=sha(app/'补货助手.exe')
    assert exe['passed'] and exe['process_exit']==0 and exe['source_commit']==SOURCE and exe['exe_sha256']==exe_sha and len(exe['checks'])==29
    manual=read(qa/'manual-html-01/html-report.json');assert manual['passed'] and manual['chapters']==21 and manual['image_count']==38
    edition=read(build/'source/docs/manual/business-edition.json')
    for name,digest in edition['files'].items():assert sha(app/'使用手册'/name)==digest,name
    assert sha(app/'使用手册/补货助手_0.49.3_新人业务上手手册.pdf')=='e2f494ed73817a561a87265811d2474c054e39f87b31ec60092ef1bac44e7326'
    verify(app/'_internal/components/aoxiang');assert not git('diff',BASE,SOURCE,'--','components').strip()
    stable={}
    for name in ['model.py','default_config.json','default_must_sell.json','warehouse_routing.py','mandatory_holding.py','admission.py','preissue.py','normal_orders.py','purchases.py','order_lifecycle.py','work_repository.py','data_maintenance.py','maintenance_storage.py','input_adapter.py','warehouse_catalog.py','coverage_metrics.py','funnel_compact_export.py']:
        old=git('show',BASE+':src/'+name);new=git('show',SOURCE+':src/'+name);assert old==new,name;stable[name]=hashlib.sha256(new).hexdigest()
    old=read(ROOT/'releases/0.51.1/rc1/artifacts.json')
    for r in old['files']:assert sha(ROOT/r['path'])==r['sha256']
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in sorted(app.rglob('*')):
            if file.is_file():z.write(file,file.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    release.mkdir(parents=True);write(release/'build_info.json',info)
    write(release/'artifacts.json',{'source_commit':SOURCE,'files':[{'path':str(p.relative_to(ROOT)),'bytes':p.stat().st_size,'sha256':sha(p)} for p in [app/'补货助手.exe',archive]]})
    write(release/'validation.json',{'source_commit':SOURCE,'baseline':git('rev-parse',BASE).decode().strip(),'sealed_at':datetime.now().astimezone().isoformat(),'full_python':full,'targeted_tests':57,'exe_checks':29,'exe_sha256':exe_sha,'html_chapters':21,'html_images':38,'live_platform':False,'business_computer_tested':False,'business_data_modified':False,'pushed':False,'merged':False})
    write(release/'protection.json',{'unchanged_sources':stable,'components_identical_to_0511':True,'component_validation_reused':'releases/0.51.1/rc1/validation.json','old_0511_artifacts_unchanged':old,'old_pdf_unchanged':True})
    for source,destination in [('full-python.log','python-full.txt'),('targeted-final.log','python-targeted.txt'),('exe-01/acceptance.json','exe-report.json'),('source-01/acceptance.json','source-report.json'),('manual-html-01/html-report.json','manual-html.json')]:
        # Keep the original QA bytes; repository text copies use LF consistently.
        (release/destination).write_text((qa/source).read_text('utf-8'),encoding='utf-8',newline='\n')
    example=release/'组合导出_隔离合成示例.xlsx';shutil.copy2(Path(exe['workbook']),example)
    write(release/'example.json',{'synthetic':True,'live_platform':False,'path':example.name,'sha256':sha(example),'business_data':False})
    for file in (qa/'exe-01').glob('*.png'):
        (release/'screenshots').mkdir(exist_ok=True);shutil.copy2(file,release/'screenshots'/file.name)
    write(release/'delivery-files.json',{'files':[{'path':p.relative_to(app).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(app.rglob('*')) if p.is_file()]})
    report=f'''# 0.51.2 rc1 门店经营分析导出补全

已补齐SKU×门店×上游供应商×原商品类目组合导出，独立打包。只读历史归档属性，不修改经营、计算或采购数据；旧交付保留，未推送、未合并。

## 原因与修复

原门店经营分析只有门店、分仓和订单汇总，分析事实也未读取上游供应商。本版在原导出中新增“门店SKU供应商类目”工作表，“当前商品明细”同步增加供应商、原类目、待核原因及来源。

同一门店＋供货仓＋SKU＋上游供应商＋原类目＋计算分类组＋库存单位＋必采属性＋纳入状态一行，跨日期同组合汇总，供应商/类目/仓/单位变化拆行。门店及SKU编码、名称并列，保留五段数量/货值/款数、阶段比率、可售天数、期间、资料覆盖及三种单号对应；表头可筛选、查看口径批注。原三张汇总的指标和列位置不变，单号关联明细保留。只导出勾选订单时新表同步限选。

供应商来自原签发作业本仓冻结资料和已归档原件“供应商名称”，不是补货建议C列中心仓编码。原商品类目来自签发/同轮冻结/归档“分类”，与快销、日百等计算分类组分开；多来源不一致标记待核。不会读取当前目录回填历史、合并供应商别名或跨仓猜归属。源码保留原件SHA、工作表和原行号。

缺列、空值或重复/冲突明确待核，不因新维度缺失减少原有漏斗。原本排除或业务待核的行仍列出，比较指标留空。原件校验失败继续阻断发布。比值按共同有效商品集合计算，天数沿用固定预测和最新业务日；缺资料不补0。

## 验证

- Python全量 **1469项通过**，{full['test_seconds']}秒；专项 **57项通过**，覆盖历史本仓归属、缺列、重复表头、类目冲突、原件缺失/损坏、生命周期追加SKU、分仓/分期/单位拆分、跨日合并、共同范围比率、取消和所选订单。
- 同一实际EXE **29项通过**：两日两仓四张原单，实际点击全范围/勾选导出；核对组合字段、三单号、五段货值与原汇总一致、所选范围、编号文本格式、原数据库及组件库不变。示例“组合导出_隔离合成示例.xlsx”为实际EXE产物，仅供看格式，不是业务资料。
- 手册HTML **21章38图**通过离线检查，新增业务使用步骤；历史PDF哈希不变。
- 计算、预算、箱规、默认参数、必采、归属、生命周期、删除、可售天数和旧汇总计算源码与0.51.1逐文件一致。两个原生组件源码也完全相同，复用0.51.1已封存组件验收，未无意义重复运行；新包仍校验组件清单。
- 没有调用真实平台或改业务电脑数据。旧版既有QLabel回调等警告不属于本轮，不宣称已解决。

## 交付

- 分支：`fix/store-analytics-dimensions-0.51.2`。
- 构建源码：`{SOURCE}`；基线：`{git('rev-parse',BASE).decode().strip()}`。证据提交和本地标签另记录在Git中。
- 应用0.51.2，模型0.3.5-rules，归属0.5.2，参数3，业务存储格式2。分析投影由funnel-2升级funnel-3，防止旧分析缓存与新增属性混用。
- EXE：`交付_0.51.2_rc1/补货助手/补货助手.exe`，SHA256 `{exe_sha}`。
- ZIP：`交付_0.51.2_rc1/补货助手_0.51.2_rc1.zip`，SHA256 `{sha(archive)}`。从固定Git归档构建，无工作区覆盖。
- 本轮QA：`{qa.relative_to(ROOT).as_posix()}`。GUI复跑：`tools/verify_analytics_identifiers.py --out <qa下新目录> --exe <本版EXE>`；封存：`tools/seal_store_dimensions_0512.py --qa {qa.relative_to(ROOT).as_posix()}`。

退出旧版、解压运行新包，回经营分析重新查询后导出，打开“门店SKU供应商类目”。旧Excel不会自动改写。若历史原件和冻结字段本来就缺失，仍显示待核，不能保证所有历史属性都有实值。本轮补的是导出，方案中的页面自定义供应商/类目筛选仍未实施。业务电脑真实资料与平台端到端尚未现场联测。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n')
    print(json.dumps({'release':str(release),'archive':str(archive),'zip_mib':round(archive.stat().st_size/1024**2,2),'exe_sha256':exe_sha,'source_commit':SOURCE},ensure_ascii=False))
if __name__=='__main__':main()
