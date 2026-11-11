"""Seal 0.51.3 warehouse first-fill defaults after isolated acceptance."""
import argparse,hashlib,json,re,shutil,subprocess,zipfile
from datetime import datetime
from pathlib import Path
from aoxiang_bundle import verify

ROOT=Path(__file__).resolve().parents[1]
SOURCE='758ca2b03e651f5b422f0789fc649f7fb01b68d4'
BASE='fb02565'
PDF_SHA='e2f494ed73817a561a87265811d2474c054e39f87b31ec60092ef1bac44e7326'
def read(path):return json.loads(path.read_text('utf-8'))
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
def copy_text(source,target):target.write_text(source.read_text('utf-8'),encoding='utf-8',newline='\n')

def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',required=True,type=Path);a=p.parse_args()
    qa=a.qa.resolve();qa.relative_to(ROOT/'qa')
    build=ROOT/'build_0.51.3_rc1';delivery=ROOT/'交付_0.51.3_rc1';app=delivery/'补货助手'
    release=ROOT/'releases/0.51.3/rc1';archive=delivery/'补货助手_0.51.3_rc1.zip'
    if release.exists() or archive.exists():raise FileExistsError('Never overwrite a sealed release')
    info=read(build/'build_info.json');assert info['source_commit']==SOURCE and info['version']=='0.51.3'
    assert info['source_mode']=='immutable git archive; no working-tree overlay'
    assert sha(build/'source.zip')==info['source_archive_sha256']
    assert read(qa/'immutable-build.json')['passed']
    assert not git('diff',SOURCE,'--','src','tests','components').strip(),'Runtime or tests changed after source freeze'
    full=read(qa/'full-python-recheck.json')
    assert full['passed'] and full['exit_code']==0 and full['tests']==1477 and full['source_commit']==SOURCE
    targeted=(qa/'targeted-sealed.log').read_text('utf-8')
    assert re.search(r'Ran 57 tests in [\d.]+s',targeted) and targeted.rstrip().endswith('OK')
    single=read(qa/'single-instance-recheck.json');assert single['passed'] and single['tests']==2
    pipeline=read(qa/'pipeline-01/report.json');assert pipeline['passed'] and len(pipeline['checks'])==3
    exe_sha=sha(app/'补货助手.exe');exe=read(qa/'exe-01/acceptance.json')
    assert exe['passed'] and exe['process_exit']==0 and exe['source_commit']==SOURCE and exe['exe_sha256']==exe_sha and len(exe['checks'])==23
    preissue=read(qa/'exe-preissue-01/report.json')
    assert preissue['passed'] and len(preissue['runs'])==2
    for run in preissue['runs']:
        assert run['passed'] and run['exit_code']==0 and not run['remaining_children']
        assert run['exe_sha256']==exe_sha and run['version']['source_commit']==SOURCE
    manual=read(qa/'manual-html-01/html-report.json')
    assert manual['passed'] and manual['chapters']==21 and manual['image_count']==39
    edition=read(build/'source/docs/manual/business-edition.json')
    assert edition['application']=='0.51.3' and edition['images']==39
    for name,digest in edition['files'].items():assert sha(app/'使用手册'/name)==digest,name
    assert sha(app/'使用手册/补货助手_0.49.3_新人业务上手手册.pdf')==PDF_SHA
    verify(app/'_internal/components/aoxiang')
    assert not git('diff',BASE,SOURCE,'--','components').strip()
    stable={}
    for name in ['model.py','default_config.json','default_must_sell.json','warehouse_routing.py','mandatory_holding.py','admission.py','new_products.py','preissue.py','normal_orders.py','purchases.py','order_lifecycle.py','work_repository.py','data_maintenance.py','maintenance_storage.py','input_adapter.py','allocation.py']:
        old=git('show',BASE+':src/'+name);new=git('show',SOURCE+':src/'+name)
        assert old==new,name;stable[name]=hashlib.sha256(new).hexdigest()
    old=read(ROOT/'releases/0.51.2/rc1/artifacts.json')
    for item in old['files']:assert sha(ROOT/item['path'])==item['sha256']
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in sorted(app.rglob('*')):
            if file.is_file():z.write(file,file.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None
    release.mkdir(parents=True)
    write(release/'build_info.json',info)
    write(release/'artifacts.json',{'source_commit':SOURCE,'files':[{'path':str(path.relative_to(ROOT)),'bytes':path.stat().st_size,'sha256':sha(path)} for path in [app/'补货助手.exe',archive]]})
    write(release/'validation.json',{'source_commit':SOURCE,'baseline':git('rev-parse',BASE).decode().strip(),'sealed_at':datetime.now().astimezone().isoformat(),'full_python':full,'targeted_tests':57,'exe_checks':23,'preissue_phases':2,'single_instance_recheck':single,'actual_calculation_pipeline':pipeline,'exe_sha256':exe_sha,'html_chapters':21,'html_images':39,'live_platform':False,'business_computer_tested':False,'business_data_modified':False,'pushed':False,'merged':False})
    write(release/'protection.json',{'unchanged_sources':stable,'components_identical_to_0512':True,'component_validation_reused':'releases/0.51.1/rc1/validation.json','old_0512_artifacts_unchanged':old,'old_pdf_sha256':PDF_SHA})
    for source,target in [('full-python-recheck.log','python-full.txt'),('targeted-sealed.log','python-targeted.txt'),('single-instance-recheck.log','python-single-instance-recheck.txt'),('exe-01/acceptance.json','exe-report.json'),('exe-preissue-01/report.json','exe-preissue.json'),('source-02/acceptance.json','source-report.json'),('pipeline-01/report.json','calculation-pipeline.json'),('manual-html-01/html-report.json','manual-html.json')]:copy_text(qa/source,release/target)
    (release/'failed-evidence').mkdir()
    copy_text(qa/'targeted-01.log',release/'failed-evidence/targeted-01.txt')
    copy_text(qa/'full-python.log',release/'failed-evidence/full-python-01.txt')
    write(release/'failed-evidence/full-python-01.json',read(qa/'full-python.json'))
    (release/'failed-evidence/说明.md').write_text('首次专项中1项测试夹具错误：禁用商品已被原准入逻辑筛除，测试仍索引第0项而出现IndexError。修正为验证行与控件均为空，未放宽准入或生产逻辑。其后57项专项通过。\n\n首次全量1477项中1项单实例测试报JSONDecodeError：测试辅助进程每50毫秒直接覆盖状态JSON，读取时遇到空文件。该测试及单实例生产逻辑与基线一致，单独2项复跑通过；随后同一源码完整全量通过，未放宽或修改该测试。原始失败日志均保留。\n',encoding='utf-8',newline='\n')
    (release/'screenshots').mkdir()
    for path in (qa/'exe-01').glob('*.png'):shutil.copy2(path,release/'screenshots'/path.name)
    write(release/'delivery-files.json',{'files':[{'path':path.relative_to(app).as_posix(),'bytes':path.stat().st_size,'sha256':sha(path)} for path in sorted(app.rglob('*')) if path.is_file()]})
    report=f'''# 0.51.3 rc1 本月新品首铺量采用对应仓资料

已实现本月新品入店数量维护使用实际归属仓商品资料“新店新品首铺数”，独立打包，旧版本保留。未推送、未合并。

## 原因和改动

原仓资料导入未保留首铺字段，选择新品窗口统一默认一个最小出库单位。本版作为可选字段保留原值，并将仓、资料修订、原件SHA、工作表、行号随本轮计算冻结。主从仓路由后只读实际归属仓的首铺数，同SKU在山东/河北可不同，不借未选仓资料。

- 本月新品默认数量取对应仓“新店新品首铺数”，按库存单位，不乘箱数，不自动勾选。
- 已有本次人工数量保留，往期/未归期新品默认量沿用原规则。
- 缺列、空白、0、负数、非数字、过大、来源仓/修订不符，明确待维护，不偷偷用1箱。首铺字段重复仅使该可选字段待核，不猜首列/末列，不妨碍正常补货导入。
- 首铺数不符合箱规时保留原数值，预览给出具体SKU和原因；维护后再预览，不自动凑整。
- 字段、来源和手工修改依据可在“资格/提示”及悬停提示查看；分仓模板及新人图文教程已同步。

新品候选识别、禁采/停采/禁用/日配准入、必采最低持有、预算分配、箱规、原模型AS及历史签发不改。默认首铺量是候选需求，仍经原预算预览和确认应用，并非永久库存下限。

## 验证

- Python全量 **1477项通过**，{full['test_seconds']}秒；相关专项 **57项通过**，含8项新增测试，覆盖分仓取量、主从仓、字段缺失/重复/非法、预算和箱规、手工保留、禁用准入及小数库存单位。
- 同一实际EXE **23项通过**：山东18、河北30、山东主河北从取30；默认不选、人工12不覆盖、往期仍取最小单位6、缺列可维护、31与箱规6冲突清晰提示、维护30后预览、原计算/主库不被预览改写。
- 同一EXE预签发 **exercise / reopen两阶段通过**，验证既有计算、调整、签发及重开链路；无残留子进程。完整检查列表见exe-preissue.json。
- 真实原表导入→本仓路由→模型计算三组隔离链路，首铺原值随计算快照保留；原模型AS仍为0，新品候选识别不变。
- 新人HTML手册 **21章39图**离线检查通过；补充对应仓首铺数图文步骤，历史PDF哈希保持不变。
- 原模型、预算、准入、必采、路由、生命周期、删除及新品应用函数逐文件与0.51.2一致。两个原生组件源码不变，复用既有组件验收并检查新包清单。
- 首次专项有1项夹具断言错误：禁用商品正确被筛除，测试误索引不存在的行，修正断言后通过。首次全量有1项单实例测试读临时状态文件遇空文件而报JSONDecodeError，该测试/生产逻辑未变；单独2项复跑与同源码整套重跑通过，未修改或放宽断言。原始失败日志保留。既有QLabel销毁后回调等警告本轮未修，不宣称解决。

全部使用隔离合成资料和数据库，无真实平台调用，未修改业务电脑数据。

## 使用和限制

更新/重新导入对应仓商品资料，保留准确表头“新店新品首铺数”；重新检查/计算未签发门店，再进“选择门店并安排新品 / 安排本店新品”查看本月新品。缺量到“资料待核”维护，勾选后预览预算并确认应用。

旧仓缓存或旧计算快照没有该字段时不会借最新资料回填历史，需上述更新和重算。已有人工量不被默认数覆盖；旧签发不回写。本次未在业务电脑实地或真实平台端到端测试。

## 提交和交付

- 分支：`feat/warehouse-first-fill-0.51.3`。
- 构建源码：`{SOURCE}`；基线：`{git('rev-parse',BASE).decode().strip()}`。证据提交及本地标签另见Git。
- 应用0.51.3；模型0.3.5-rules、归属0.5.2、参数3、存储格式2不变。
- EXE：`交付_0.51.3_rc1/补货助手/补货助手.exe`；SHA256 `{exe_sha}`。
- ZIP：`交付_0.51.3_rc1/补货助手_0.51.3_rc1.zip`；SHA256 `{sha(archive)}`。由固定Git归档构建，无工作区覆盖。
- QA：`{qa.relative_to(ROOT).as_posix()}`。复跑入口：`tools/verify_first_fill.py --out <qa下新目录> --exe <本版EXE>`、`tools/verify_preissue_exe.py --output <qa下新目录> --exe <本版EXE>`。
'''
    (release/'交付报告.md').write_text(report,encoding='utf-8',newline='\n')
    print(json.dumps({'release':str(release),'archive':str(archive),'zip_mib':round(archive.stat().st_size/1024**2,2),'exe_sha256':exe_sha,'source_commit':SOURCE},ensure_ascii=False))

if __name__=='__main__':main()
