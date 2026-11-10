"""Seal a verified immutable delivery into NEW evidence/ZIP destinations only."""
import argparse,hashlib,json,re,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text('utf-8'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);a=p.parse_args();qa=a.qa.resolve()
    build=ROOT/'build_0.50.5_rc1';delivery=ROOT/'交付_0.50.5_rc1';app=delivery/'补货助手';release=ROOT/'releases/0.50.5/rc1'
    if release.exists():raise FileExistsError('Release evidence already exists')
    info=read(build/'build_info.json');source=info['source_commit'];base='385444c'
    assert info['version']=='0.50.5' and info['source_mode']=='immutable git archive; no working-tree overlay'
    version=read(app/'_internal/version.json');assert version['version']=='0.50.5' and version['model_version']=='0.3.5-rules'
    python=(qa/'python-full-final.log').read_text('utf-8');match=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\b',python);assert match,'Full regression not passed'
    special=(qa/'final-special-02.log').read_text('utf-8');target=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\b',special);assert target
    proof=read(qa/'exe-mandatory/report.json');assert proof['passed'] and proof['frozen_exe'] and proof['exit_code']==0
    assert proof['exe_sha256']==sha(app/'补货助手.exe') and proof['source_commit']==source and not proof['live_platform']
    preissue=read(qa/'exe-preissue/report.json');assert preissue['passed']
    for run in preissue['runs']:assert run['passed'] and run['exit_code']==0 and not run['remaining_children'] and run['exe_sha256']==proof['exe_sha256']
    compare=read(qa/'comparison/comparison.json');assert compare['passed'] and compare['forecast_zero_differences']
    manual=read(qa/'manual-html/html-report.json');assert manual['passed'] and manual['image_count']==33
    stable={}
    for name in ('model.py','one_box_exception.py','default_config.json','default_must_sell.json','warehouse_routing.py','input_adapter.py'):
        digest=hashlib.sha256(subprocess.check_output(['git','show',base+':src/'+name],cwd=ROOT)).hexdigest()
        assert sha(ROOT/'src'/name)==digest;stable[name]=digest
    assert not subprocess.check_output(['git','diff',base,source,'--','components'],cwd=ROOT).strip(),'Component sources changed'
    old=read(ROOT/'releases/0.50.4/rc1/artifacts.json')
    for f in old['files']:assert sha(ROOT/f['path'])==f['sha256']
    from catalog_sync_bundle import verify_component
    verify_component(app/'_internal/components/jst_catalog',app/'_internal/components/aoxiang')
    edition=read(ROOT/'docs/manual/business-edition.json')
    for name,digest in edition['files'].items():assert sha(ROOT/'docs/manual'/name)==digest,('manual hash',name)
    files=sorted(f for f in app.rglob('*') if f.is_file());assert not any(f.suffix in ('.db','.sqlite','.sqlite3') for f in files)
    archive=delivery/'补货助手_0.50.5_rc1.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for f in files:z.write(f,f.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None and len(z.infolist())==len(files)
    release.mkdir(parents=True)
    validation=dict(base=compare['baseline'],source_commit=source,python_full=int(match[1]),python_full_seconds=float(match[2]),
        final_special=int(target[1]),comparison_cases=compare['cases'],ordinary_equivalence=compare['ordinary_outside_scope_unchanged'],
        forecast_zero_differences=True,exe_mandatory_checks=len(proof['checks']),exe_preissue_phases=len(preissue['runs']),
        node_suites_rerun=False,component_sources_unchanged=True,manual_images=33,old_0504_preserved=True,
        live_platform_tested=False,business_pc_tested=False,pushed=False,merged=False)
    write(release/'validation.json',validation);write(release/'exe-mandatory.json',proof);write(release/'exe-preissue.json',preissue)
    write(release/'comparison.json',compare);write(release/'manual-html.json',manual)
    write(release/'protection.json',dict(unchanged_core=stable,old_0504_artifacts=old['files']))
    write(release/'artifacts.json',dict(source_commit=source,files=[dict(path=str(f.relative_to(ROOT)),bytes=f.stat().st_size,sha256=sha(f)) for f in (app/'补货助手.exe',archive)]))
    write(release/'delivery-files.json',[dict(path=f.relative_to(app).as_posix(),bytes=f.stat().st_size,sha256=sha(f)) for f in files])
    shutil.copyfile(build/'build_info.json',release/'build_info.json')
    for name in ('python-full-final.log','final-special-02.log','target-01.log','target-03.log'):shutil.copyfile(qa/name,release/(name.replace('.log','.txt')))
    shots=release/'screenshots';shots.mkdir()
    for image in (qa/'exe-mandatory').glob('*.png'):shutil.copyfile(image,shots/image.name)
    report=f'''# 0.50.5 rc1 必采最低持有与日配排除交付

补货中的必卖清单以前只提高预算优先级，零销量、零库存且无缺货天数的商品仍可能为零建议。现在用户确认以现有最低可售参考量（默认1个库存单位）作为必采最低持有基数；可用及在途不足时，向上按最小出库单位补足。先分配各必采最低量，再分配追加和普通需求；预算不足保留缺口，预算零与末尾一个出库单位且不超过105%的边界不变。

商品资料精确日配标签默认排除；禁采、停采、禁用、拒收、资质及资料校验优先。实际归属仓和资料来源保持，补货建议同名标签不替代商品资料。新品重分配保留最低量；人工明确例外需核对具体风险且可精确撤销。未出现的SKU不补造，未知资料不改成零。

原预测公式和AS/BV参考保留，执行数量、金额、理由、签发Excel和历史快照贯通。模型/执行规则版本为0.3.5-rules，应用0.50.5；未签发旧结果需重算，已签发历史按原量重导。缺货率统计公式未改。教程第03及18章和参数说明已同步，HTML共20章33图；PDF保留0.49.3历史版。

## 验证

- 最终源码Python全量 {match[1]} 项通过，{match[2]} 秒；{target[1]} 项专项通过。
- 固定0.50.4源码与本版运行相同 {compare['cases']} 个合成场景，所有预测中间值零差异；{compare['ordinary_outside_scope_unchanged']} 个范围外普通商品场景数量、金额一致。{compare['changed_execution']} 个场景按本次规则改变执行量，明细见comparison.json。这里是场景等价性验证，不宣称业务电脑提速。
- 固定源码实际EXE {len(proof['checks'])} 项新规则链路通过，包含真实合成Excel读取、Qt详情、签发导出、归档回读、预算零及人工例外恢复；另完成原人工改量完整流程的exercise/reopen两阶段验收，无残留子进程。证据绑定同一EXE哈希和构建提交。
- 六个未授权变化的核心文件逐字节一致；翱象/聚水潭组件源码未更改，固定随包组件清单校验通过。本轮未重跑Node全量。
- 离线HTML图片、搜索、锚点、练习、图片放大和移动宽度检查通过，无外部请求。新图是源码Qt合成示例，逐图保留来源；不冒充业务现场。
- 旧0.50.4 EXE及ZIP哈希保持，不覆盖已有交付、QA和历史。

过程失败证据仍保留：target-01是单行全部待核的合成夹具触发既有整店拒算，补充有效对照行后专项通过；target-03是专项命令误写模块名，改正入口后通过。这两次未修改或放宽业务校验。全量日志中的故意注入异常不等于测试失败；既有Qt回调/弃用告警不在本次修复范围。

## 使用与限制

正常退出旧版，完整解压新ZIP运行；未签发旧结果重新计算。默认基数1个库存单位不是默认一箱；例如箱规6、无覆盖时补6，已有1件时不因保底多补一箱。实际在途到店时间、中心仓可供量、预算及人工例外仍需业务判断。本轮只用隔离数据，没有调用真实平台或修改用户业务库；尚未在业务电脑现场验证。

构建源码提交：`{source}`。证据在本目录，原始记录在`{qa.relative_to(ROOT)}`；独立包`交付_0.50.5_rc1/补货助手_0.50.5_rc1.zip`，应用`交付_0.50.5_rc1/补货助手/补货助手.exe`。未推送、未合并。各文件大小与SHA256见artifacts.json。
'''
    (release/'交付报告.md').write_text(report,'utf-8',newline='\n')
    print(json.dumps(validation,ensure_ascii=False))


if __name__=='__main__':main()
