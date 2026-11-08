"""Seal 0.48.6 fixed-source native UI and packaged component recovery evidence."""
import hashlib,json,re,shutil,subprocess,zipfile
from pathlib import Path
from aoxiang_bundle import verify
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/workflow_0486'
def read(p):return json.loads(p.read_text('utf-8-sig'))
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
def main():
    build=read(ROOT/'build_0.48.6_rc1/build_info.json');commit=build['source_commit'];assert build['version']=='0.48.6'
    assert not subprocess.check_output(['git','diff',commit,'--','src','tests','components'],cwd=ROOT)
    raw=(QA/'run1/regression.log').read_text('utf-8-sig');m=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',raw)
    assert m and int(m[1])>=1257
    ids=re.findall(r'^(test\w+ \([^\n]+\)) \.\.\.',raw,re.M);assert len(ids)==len(set(ids))==int(m[1])
    assert re.search(r'Ran 99 tests .*\s+OK\s*$',(QA/'run1/focused2.log').read_text('utf-8-sig'))
    node=(QA/'run1/node-all1.log').read_text('utf-8-sig');assert re.search(r'^# tests 191$',node,re.M) and re.search(r'^# pass 191$',node,re.M) and re.search(r'^# fail 0$',node,re.M) and re.search(r'^# skipped 0$',node,re.M)
    exe=read(QA/'exe1/report.json');payment=read(QA/'payment-exe1/report.json');probe=read(QA/'probe-exe1/report.json')
    for v in (exe,payment):assert v['passed'] and v['source_commit']==commit and v['live_platform'] is False
    assert len(set(exe['checks']))==len(exe['checks'])>=22 and len(set(payment['checks']))==len(payment['checks'])>=12
    assert probe['passed'] and probe['live_platform'] is False and len(probe['checks'])==3
    assert read(QA/'source4/report.json')['passed'] and read(QA/'payment-source1/report.json')['passed']
    previous=read(ROOT/'releases/0.48.5/rc1/frozen.json');old=read(ROOT/'releases/0.48.5/rc1/validation.json')
    protected=dict(previous['preserved_deliveries']);protected['交付_0.48.5_rc1/补货助手/补货助手.exe']=old['exe_sha256'];protected[old['zip']]=old['zip_sha256']
    assert all(sha(ROOT/p)==h for p,h in previous['core'].items());assert all(sha(ROOT/p)==h for p,h in protected.items())
    assert not subprocess.check_output(['git','diff','v0.48.5-rc1',commit,'--','components/jst_catalog'],cwd=ROOT)
    delivery=ROOT/'交付_0.48.6_rc1/补货助手';component=delivery/'_internal/components/aoxiang';manifest=verify(component)
    assert sha(component/'src/purchase-store-selector.mjs')==sha(ROOT/'build_0.48.6_rc1/source/components/aoxiang/src/purchase-store-selector.mjs')
    release=ROOT/'releases/0.48.6/rc1';release.mkdir(parents=True,exist_ok=False)
    mapping={'build_info.json':ROOT/'build_0.48.6_rc1/build_info.json','regression.log':QA/'run1/regression.log','focused-tests.log':QA/'run1/focused2.log',
      'node-tests.log':QA/'run1/node-all1.log','source-ui.json':QA/'source4/report.json','exe-report.json':QA/'exe1/report.json',
      'payment-exe.json':QA/'payment-exe1/report.json','packaged-recovery.json':QA/'probe-exe1/report.json'}
    for name,p in mapping.items():shutil.copyfile(p,release/name)
    (release/'retained-failures').mkdir()
    for name,p in {'node-focused1.log':QA/'run1/node-focused1.log','node-focused2.log':QA/'run1/node-focused2.log','fixture-missing-tags1.json':QA/'source1/report.json','fixture-missing-tags2.json':QA/'source2/report.json'}.items():shutil.copyfile(p,release/'retained-failures'/name)
    (release/'screenshots').mkdir()
    for p in (QA/'exe1').glob('*.png'):shutil.copyfile(p,release/'screenshots'/p.name)
    shutil.copyfile(QA/'payment-exe1/review-all.png',release/'screenshots/payment-review.png')
    manuals=delivery/'使用手册'
    for version,name in [('0.48.3','0.48.3最终清单筛选与批量加减.md'),('0.48.4','0.48.4表格空间与签发引导.md'),('0.48.5','0.48.5生成单直达付款核对.md'),('0.48.6','0.48.6采购恢复与作业交互.md')]:
        with (manuals/(version+'新增功能使用说明.txt')).open('xb') as f:f.write((ROOT/'build_0.48.6_rc1/source/docs'/name).read_bytes())
    images=manuals/'0.48.6图示';images.mkdir()
    for name in ('generation.png','import-1280.png','payment-review.png'):shutil.copyfile(release/'screenshots'/name,images/name)
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>0.48.6 业务操作指引</title><style>body{max-width:1100px;margin:32px auto;padding:0 24px;font:18px/1.7 "Microsoft YaHei",sans-serif;color:#22383c}h1,h2{color:#176c63}img{max-width:100%;border:1px solid #ccdadd}aside{background:#edf6f3;padding:18px}li{margin:10px 0}</style><h1>采购核对、分配与付款：0.48.6操作指引</h1><aside>以下截图为隔离合成样例。软件不代替实际付款，不将生成或导出当成已经付款。</aside><h2>平台已建单，但助手仍待核</h2><ol><li>进入原来的「采购上传与核对」批次，找到待核订单。</li><li>点击「继续核对采购结果」，查询原受理任务并核对原采购单；不要新建或重传。</li><li>核验通过后自动回填采购单号。仍有异常时，打开「查看原因与下一步」，技术详情保留请求类型和路径。网络或登录异常不等同采购失败。</li></ol><h2>先决定分配顺序，再预览生成</h2><ol><li>默认回传确认时间优先；预览后，上方列表就是执行顺序。</li><li>拖动列表自动采用人工顺序，重新预览；恢复默认可点击「恢复回传时间顺序」。</li><li>确认生成后，原批顺序冻结。锁单先选择下方完成列表，可全选；按钮标明所选数量，导出不代表已经下单。</li></ol><img src="0.48.6图示/generation.png" alt="分配顺序和锁单数量"><h2>批量导入：左边选单查看，右边核对商品</h2><p>读取依据和统计放到页签里。左侧筛选一单仅改变查看；底部明确显示本批全部保存份数。核对完整读取范围后，确认保存本批完整单据。</p><img src="0.48.6图示/import-1280.png" alt="批量核对布局"><h2>用生成单直接核对付款</h2><p>在聚水潭导入单结果页点击「按当前筛选核对付款」或「按勾选核对付款」，无需导出再导入。逐单核对门店、仓、数量、金额与实际已付款一致后才确认；不同请取消并使用真实付款单。</p><img src="0.48.6图示/payment-review.png" alt="直接核对付款"></html>'''
    with (manuals/'0.48.6操作指引.html').open('x',encoding='utf-8') as f:f.write(html)
    hashes={p.relative_to(delivery).as_posix():sha(p) for p in sorted(delivery.rglob('*')) if p.is_file()};target=delivery.parent/'补货助手_0.48.6_rc1_采购恢复与作业交互.zip'
    with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
        for name in hashes:z.write(delivery/name,'补货助手/'+name)
    with zipfile.ZipFile(target) as z:
        assert len(z.namelist())==len(hashes)
        for name,h in hashes.items():
            with z.open('补货助手/'+name) as f:assert hashlib.file_digest(f,'sha256').hexdigest()==h
    write(release/'manifest.json',{'source_commit':commit,'files':hashes});write(release/'frozen.json',{'core':previous['core'],'preserved_deliveries':protected})
    result={'passed':True,'source_commit':commit,'python_tests':int(m[1]),'unique_test_ids':len(set(ids)),'python_seconds':float(m[2]),'focused_tests':99,'node_tests':191,
      'exe_checks':len(exe['checks']),'payment_exe_checks':len(payment['checks']),'packaged_recovery_checks':len(probe['checks']),'geometry':exe['geometry'],
      'exe_sha256':sha(delivery/'补货助手.exe'),'zip':str(target.relative_to(ROOT)),'zip_sha256':sha(target),'zip_bytes':target.stat().st_size,'files':len(hashes),
      'core_unchanged':len(previous['core']),'preserved_delivery_files':len(protected),'component_files_verified':len(manifest['files']),'jst_component_unchanged':True,'live_platform':False}
    write(release/'validation.json',result);print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
