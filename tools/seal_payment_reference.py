"""Seal the local 0.49.2 rc1 number editor after isolated validation."""
import hashlib, html, json, re, shutil, subprocess, zipfile
from pathlib import Path
from aoxiang_bundle import verify

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'qa/payment_reference_0492'


def read(path):return json.loads(path.read_text('utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def write(path,value):
    with path.open('x',encoding='utf-8',newline='\n') as stream:stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def tests(path,count):
    raw=path.read_text('utf-8-sig'); match=re.search(r'Ran (\d+) tests in ([\d.]+)s',raw)
    assert match and int(match[1])==count and re.search(r'\sOK\s*$',raw),path
    ids=re.findall(r'^(test\w+ \([^\n]+\)) \.\.\.',raw,re.M)
    assert len(ids)==len(set(ids))==count,path
    return {'tests':count,'unique_test_ids':count,'seconds':float(match[2])}


def main():
    build=read(ROOT/'build_0.49.2_rc1/build_info.json'); commit=build['source_commit']
    assert build['version']=='0.49.2' and build['source_mode']=='immutable git archive; no working-tree overlay'
    assert not subprocess.check_output(['git','diff',commit,'--','src','tests','tools/verify_payment_reference.py','tools/seal_payment_reference.py'],cwd=ROOT)
    regression=tests(QA/'regression.log',1317); focused=tests(QA/'maintenance1.log',12)
    source=read(QA/'source1/acceptance.json'); exe=read(QA/'exe1/acceptance.json')
    for r in (source,exe):
        assert r['passed'] and r['process_exit']==0 and len(r['checks'])==17 and not r['live_platform'] and not r['live_database_written']
        assert r['version']=='0.49.2' and r['model_version']=='0.3.4-rules' and r['storage_format_version']==2
    assert exe['source_commit']==commit
    previous=read(ROOT/'releases/0.49.1/rc1/frozen.json'); old=read(ROOT/'releases/0.49.1/rc1/validation.json')
    protected=dict(previous['preserved_deliveries']); protected['交付_0.49.1_rc1/补货助手/补货助手.exe']=old['exe_sha256']; protected[old['zip']]=old['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in previous['core'].items())
    assert all(sha(ROOT/p)==h for p,h in protected.items())
    assert not subprocess.check_output(['git','diff','v0.49.1-rc1',commit,'--','components','src/payment_matching.py','src/payment_auto_matching.py'],cwd=ROOT)
    delivery=ROOT/'交付_0.49.2_rc1/补货助手'; executable_hash=sha(delivery/'补货助手.exe')
    assert exe['exe_sha256']==executable_hash
    component=verify(delivery/'_internal/components/aoxiang')
    for p in (ROOT/'build_0.49.2_rc1/source/components/aoxiang/src').glob('*.mjs'):
        assert sha(p)==sha(delivery/'_internal/components/aoxiang/src'/p.name)
    release=ROOT/'releases/0.49.2/rc1'; target=delivery.parent/'补货助手_0.49.2_rc1_内部订单号维护.zip'; manual=delivery/'使用手册/0.49.2内部订单号维护'
    assert not release.exists() and not target.exists() and not manual.exists()
    release.mkdir(parents=True); manual.mkdir(parents=True)
    sources={'build_info.json':ROOT/'build_0.49.2_rc1/build_info.json','regression.log':QA/'regression.log','reference-tests.log':QA/'maintenance1.log','source-report.json':QA/'source1/acceptance.json','exe-report.json':QA/'exe1/acceptance.json'}
    for name in ('填写内部订单号.png','已保存内部订单号.png','号码修改记录.png'):sources[name]=QA/'exe1'/name
    for name,path in sources.items():shutil.copyfile(path,release/name)
    doc=(ROOT/'docs/0.49.2聚水潭内部号维护.md').read_text('utf-8')
    doc=doc.replace('全量回归和实际EXE验收见[交付报告](../releases/0.49.2/rc1/交付报告.md)。','全量回归1317项、单号维护专项12项、实际EXE界面17项验收通过。')
    doc+='\n## 填写与修改记录\n\n![填写内部订单号](填写内部订单号.png)\n\n![号码修改记录](号码修改记录.png)\n'
    (manual/'使用说明.md').write_text(doc,'utf-8'); blocks=[]
    for line in doc.splitlines():
        if line.startswith('!['):
            m=re.fullmatch(r'!\[(.*?)\]\((.*?)\)',line); blocks.append('<figure><img src="'+html.escape(m[2])+'"><figcaption>'+html.escape(m[1])+'</figcaption></figure>')
        elif line.startswith('# '):blocks.append('<h1>'+html.escape(line[2:])+'</h1>')
        elif line.startswith('## '):blocks.append('<h2>'+html.escape(line[3:])+'</h2>')
        elif line:blocks.append('<p>'+html.escape(line)+'</p>')
    (manual/'打开使用说明.html').write_text('<!doctype html><meta charset="utf-8"><title>聚水潭内部订单号维护</title><style>body{max-width:1000px;margin:40px auto;padding:0 24px;font:18px/1.8 "Microsoft YaHei",sans-serif;color:#193e43}img{max-width:100%;border:1px solid #d0dfe0}h1,h2{color:#176e65}</style>'+''.join(blocks),'utf-8')
    for name in ('填写内部订单号.png','号码修改记录.png'):shutil.copyfile(release/name,manual/name)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()}
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in hashes:archive.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist())==len(hashes)
        for name,digest in hashes.items():
            with archive.open('补货助手/'+name) as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==digest
    write(release/'manifest.json',{'source_commit':commit,'files':hashes}); write(release/'frozen.json',{'core':previous['core'],'preserved_deliveries':protected})
    report={'passed':True,'source_commit':commit,'python_regression':regression,'reference_tests':focused,'exe_checks':len(exe['checks']),'core_unchanged':len(previous['core']),'preserved_delivery_files':len(protected),'exe_sha256':executable_hash,'zip':str(target.relative_to(ROOT)).replace('\\','/'),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'files':len(hashes),'live_database_written':False,'live_platform':False,'components_unchanged_from':'v0.49.1-rc1','component_files_verified':len(component['files']),'node_tests_rerun':False}
    write(release/'validation.json',report)
    note=f'''# 0.49.2 rc1 交付报告

订单详情新增“维护聚水潭内部订单号”。直接填写或修改后保存；原付款文件有号时带出，修改历史可查看。无文件导入、企业选择或自动认单步骤。

## 使用与范围

订单跟踪 → 查看订单详情 → 单据与依据 → 维护聚水潭内部订单号 → 填写 → 保存。已确认付款的订单可用，已有采购或收货也能维护。

只写当前仓单的本机号码登记与修改事件；沿用已有生命周期存储表。付款版本、原件、数量、金额、库存分配及已有采购收货依据均不改写。付款识别代码、模型、预算、默认参数、主从仓归属及平台组件均未改变。手工维护的号码不自动改写历史付款文件，也不改变付款自动认单规则。

## 验证

- 全量回归 {regression['tests']} 个独立测试通过，用时 {regression['seconds']} 秒。
- 单号专项 {focused['tests']} 项通过：填空、更正、同店两仓隔离、收货后补号、并发修改、删除/失效身份、取消、写入中途失败回滚及重复保存。
- 源码与实际EXE各 {len(exe['checks'])} 项界面验收通过；启动不加载维护模块，打开订单入口后按需读取。实际点击入口、填写、保存、重开、更正、取消、空值提示、重复保存与修改记录均验证。
- 隔离样本逐表验证签发、付款、收货、原文件及库存分配完全相同；未调用真实平台，未写本机日常业务库。
- {len(previous['core'])} 个受保护核心文件与 {len(protected)} 个旧交付EXE/ZIP哈希不变。平台组件无变动，本轮未重跑Node测试；未做性能测量或低配电脑实测。既有QLabel销毁后回调问题不在本次修改范围。

## 交付与追溯

- 应用0.49.2；模型0.3.4-rules；存储格式2。
- 分支：feature/payment-reference-maintenance-0.49.2。
- 不可变构建源码提交：{commit}。
- EXE：交付_0.49.2_rc1/补货助手/补货助手.exe。
- ZIP：{report['zip']}。
- EXE SHA256：{executable_hash}。
- ZIP SHA256：{report['zip_sha256']}。
- ZIP含 {len(hashes)} 个文件，解压内容逐文件校验通过；带图文使用说明。旧版本保留，未推送、未合并。

早期独立自动认单原型的18项测试日志保留在qa/payment_reference_0492/references1.log。该原型已按用户要求撤下，本交付以精简入口的12项专项和17项界面验收为准，不将早期结果计入验收。

![填写内部订单号](填写内部订单号.png)

![号码修改记录](号码修改记录.png)
'''
    (release/'交付报告.md').write_text(note,'utf-8'); print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
