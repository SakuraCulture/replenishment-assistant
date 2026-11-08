"""Seal 0.49.3 rc1 with isolated workbook, Qt, regression and preservation evidence."""
import html,json,re,shutil,subprocess,zipfile
from pathlib import Path
from aoxiang_bundle import verify
from seal_payment_reference import read,sha,write,tests

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/analytics_ids_0493'


def main():
    build=read(ROOT/'build_0.49.3_rc1/build_info.json');commit=build['source_commit']
    assert build['version']=='0.49.3' and build['source_mode']=='immutable git archive; no working-tree overlay'
    assert not subprocess.check_output(['git','diff',commit,'--','src','tests','tools/verify_analytics_identifiers.py','tools/seal_analytics_identifiers.py'],cwd=ROOT)
    regression=tests(QA/'regression.log',1335);focused=tests(QA/'analytics-final.log',44)
    source=read(QA/'source1/acceptance.json');exe=read(QA/'exe1/acceptance.json')
    for r in (source,exe):
        assert r['passed'] and r['process_exit']==0 and len(r['checks'])==19 and not r['live_platform'] and not r['live_database_written']
        assert r['version']=='0.49.3' and r['model_version']=='0.3.4-rules' and r['storage_format_version']==2
    assert exe['source_commit']==commit
    previous=read(ROOT/'releases/0.49.2/rc1/frozen.json');old=read(ROOT/'releases/0.49.2/rc1/validation.json')
    protected=dict(previous['preserved_deliveries']);protected['交付_0.49.2_rc1/补货助手/补货助手.exe']=old['exe_sha256'];protected[old['zip']]=old['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in previous['core'].items())
    assert all(sha(ROOT/p)==h for p,h in protected.items())
    assert not subprocess.check_output(['git','diff','v0.49.2-rc1',commit,'--','components','src/payment_matching.py','src/payment_auto_matching.py','src/payment_references.py'],cwd=ROOT)
    delivery=ROOT/'交付_0.49.3_rc1/补货助手';executable_hash=sha(delivery/'补货助手.exe');assert exe['exe_sha256']==executable_hash
    component=verify(delivery/'_internal/components/aoxiang')
    for p in (ROOT/'build_0.49.3_rc1/source/components/aoxiang/src').glob('*.mjs'):
        assert sha(p)==sha(delivery/'_internal/components/aoxiang/src'/p.name)
    release=ROOT/'releases/0.49.3/rc1';manual=delivery/'使用手册/0.49.3经营分析单号关联';target=delivery.parent/'补货助手_0.49.3_rc1_经营分析单号关联.zip'
    assert not release.exists() and not manual.exists() and not target.exists()
    release.mkdir(parents=True);manual.mkdir(parents=True)
    sources={'build_info.json':ROOT/'build_0.49.3_rc1/build_info.json','regression.log':QA/'regression.log','analytics-tests.log':QA/'analytics-final.log',
             'source-report.json':QA/'source1/acceptance.json','exe-report.json':QA/'exe1/acceptance.json',
             '门店经营分析单号.png':QA/'exe1/门店经营分析单号.png','订单三种单号.png':QA/'exe1/订单三种单号.png',
             '经营分析单号_合成示例.xlsx':Path(exe['workbook'])}
    for name,path in sources.items():shutil.copyfile(path,release/name)
    retained=release/'retained-development';retained.mkdir()
    shutil.copyfile(QA/'identifiers1.log',retained/'identifiers1.log')
    write(retained/'README.json',{'not_final_acceptance':True,'identifiers1.log':'16-case first run had one test-fixture error: the mutation callback ran during the initial read transaction, producing SQLite database-is-locked. It was moved to the first completed-sheet callback to inject a true mid-export update; the strict no-publication assertion was retained. Final 44-case group and complete regression pass.'})
    doc=(ROOT/'docs/0.49.3经营分析单号关联.md').read_text('utf-8')
    doc+='\n## 实际界面与样例\n\n以下为新EXE的隔离合成演示，不是真实业务或真实平台数据。\n\n![门店经营分析三种单号](门店经营分析单号.png)\n\n![逐订单三种单号](订单三种单号.png)\n\n打开同目录“经营分析单号_合成示例.xlsx”可查看45列分析表和逐单对应。\n'
    (manual/'使用说明.md').write_text(doc,'utf-8');blocks=[]
    for line in doc.splitlines():
        if line.startswith('!['):
            m=re.fullmatch(r'!\[(.*?)\]\((.*?)\)',line);blocks.append('<figure><img src="'+html.escape(m[2])+'"><figcaption>'+html.escape(m[1])+'</figcaption></figure>')
        elif line.startswith('# '):blocks.append('<h1>'+html.escape(line[2:])+'</h1>')
        elif line.startswith('## '):blocks.append('<h2>'+html.escape(line[3:])+'</h2>')
        elif line:blocks.append('<p>'+html.escape(line)+'</p>')
    (manual/'打开使用说明.html').write_text('<!doctype html><meta charset="utf-8"><title>经营分析单号关联</title><style>body{max-width:1100px;margin:40px auto;padding:0 24px;font:18px/1.8 "Microsoft YaHei",sans-serif;color:#193e43}img{max-width:100%;border:1px solid #d0dfe0}h1,h2{color:#176e65}</style>'+''.join(blocks)+'<p><a href="经营分析单号_合成示例.xlsx">打开合成导出示例</a></p>','utf-8')
    for name in ('门店经营分析单号.png','订单三种单号.png','经营分析单号_合成示例.xlsx'):shutil.copyfile(release/name,manual/name)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist())==len(hashes)
        for name,digest in hashes.items():
            with archive.open('补货助手/'+name) as stream:
                import hashlib
                assert hashlib.file_digest(stream,'sha256').hexdigest()==digest
    write(release/'manifest.json',{'source_commit':commit,'files':hashes});write(release/'frozen.json',{'core':previous['core'],'preserved_deliveries':protected})
    result={'passed':True,'source_commit':commit,'python_regression':regression,'analytics_tests':focused,'exe_checks':19,'core_unchanged':len(previous['core']),
            'preserved_delivery_files':len(protected),'exe_sha256':executable_hash,'zip':target.relative_to(ROOT).as_posix(),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'files':len(hashes),
            'components_unchanged_from':'v0.49.2-rc1','component_files_verified':len(component['files']),'node_tests_rerun':False,'performance_measured':False,'live_platform':False,'live_database_written':False}
    write(release/'validation.json',result)
    (release/'交付报告.md').write_text(f'''# 0.49.3 rc1 交付报告

门店经营分析在同一查询快照、界面和Excel中保留签发单号、聚水潭内部订单号、翱象采购单号。门店/分仓/订单视图都可查看；导出原42列后追加三列，并附“单号关联明细”。聚水潭本机维护值优先，未维护时取当前付款原表；翱象取本地已保存采购关联，按原订单身份、修订、仓及签发快照核对。没有在线查询或修改平台。

## 使用及业务边界

五段经营分析 → 查询 → 门店经营分析（含可售天数） → 导出当前范围。仅导出部分单时在订单漏斗中勾选，再点导出所选订单。

同店跨日、跨仓多单按签发单逐行对应；一张签发单多张采购单在明细中分别留行。未提供内部号、未取得采购号不补0或猜号；已作废保留原号和本机已留存状态。原付款版本变化的已关联采购号仍留存，并标记待复核。补号、关联或已留存状态变化后，旧快照标为需刷新，导出前后复核，避免混入新旧单号。

原五段数量、货值、比率、必采分组、排除规则及可售天数未改。导出前42列保持原位置、数值、类型和格式；新表45列，增加单号关联明细。分析只读本机资料，不迁移存储、不改变付款、采购、收货或模型规则。

## 验证与限制

- 全量回归{regression['tests']}个独立测试通过，用时{regression['seconds']}秒。
- 经营分析专项{focused['tests']}项通过，包括16项新单号测试和2项新界面测试；原有指标、格式及AI/界面导出一致性检查保留。
- 源码和实际EXE各19项界面验收通过：两日同店两仓4张仓单，实际点击查询、全范围与勾选导出，检查45列和单号明细；核对文本类型、前导零、作废状态、旧快照失效、刷新及可售天数视图。
- 隔离查询和导出前后，业务库与翱象组件库哈希不变。读库仅提取关联元数据，未启动平台客户端或读取原生商品大表。
- 15个受保护核心文件、44个旧交付EXE/ZIP哈希不变；平台组件未改，本轮未重跑Node测试。
- 仅隔离合成案例，未接真实平台或改动日常库；未测低配电脑和大历史库资源占用，不宣称性能提升。既有QLabel销毁后回调问题不在本次修改范围。
- 该版同步新增功能图文教程；主新人手册仍保留其原版本及历史截图标注，不冒充全面换版。

## 交付与追溯

- 应用0.49.3；模型0.3.4-rules；参数/作业/存储版本3/1/2。分析快照标识funnel-2。
- 分支feat/analytics-order-identifiers-0.49.3。
- 不可变构建源码提交{commit}。
- EXE：交付_0.49.3_rc1/补货助手/补货助手.exe。
- ZIP：{result['zip']}，{result['zip_bytes']}字节，{len(hashes)}个文件逐文件校验通过。
- EXE SHA256：{executable_hash}。
- ZIP SHA256：{result['zip_sha256']}。
- 旧版保留，未推送、未合并。开发阶段测试夹具锁冲突日志保留在retained-development，不计为最终验收。

![门店经营分析三种单号](门店经营分析单号.png)

![订单三种单号](订单三种单号.png)
''','utf-8')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
