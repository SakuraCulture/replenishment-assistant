"""Seal new-path-only 0.50.4 evidence after immutable build and isolated EXE acceptance."""
import argparse,hashlib,json,re,shutil,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def sha(p):
    with p.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);a=p.parse_args();qa=a.qa.resolve();qa.relative_to(ROOT/'qa')
    build=ROOT/'build_0.50.4_rc1';delivery=ROOT/'交付_0.50.4_rc1';app=delivery/'补货助手';release=ROOT/'releases/0.50.4/rc1'
    if release.exists():raise FileExistsError('Use a fresh release directory')
    info=json.loads((build/'build_info.json').read_text('utf-8'));source=info['source_commit'];exe=sha(app/'补货助手.exe')
    proofs={}
    for label,file in [('exe-reexport','exe-reexport/process-acceptance.json'),('exe-stockout','exe-stockout/report.json'),
                       ('exe-pipeline','exe-pipeline/process-acceptance.json'),('exe-catalog','exe-catalog/process-acceptance.json')]:
        proof=json.loads((qa/file).read_text('utf-8'));assert proof['passed'] and proof.get('process_exit',proof.get('process_exit_code'))==0,label
        assert proof.get('frozen_exe',proof.get('frozen',False)),label
        assert proof['exe_sha256']==exe,label
        proofs[label]=proof
    assert proofs['exe-reexport']['source_commit']==source
    full=(qa/'python-full-final.log').read_text('utf-8');match=re.search(r'Ran (\d+) tests in ([\d.]+)s\s+OK\s*$',full);assert match,'Final Python regression failed'
    counts={}
    for label,file in [('aoxiang','node-aoxiang-final.log'),('jst','node-jst-sealed.log')]:
        text=(qa/file).read_text('utf-8');assert re.search(r'fail 0\b',text) and re.search(r'cancelled 0\b',text),label
        counts[label]=int(re.search(r'tests (\d+)',text)[1])
    manual=json.loads((qa/'manual-html/html-report.json').read_text('utf-8'));assert manual['passed'] and manual['image_count']==32
    protected=json.loads((qa/'protection.json').read_text('utf-8'))
    for name,digest in protected['model_unchanged'].items():assert sha(ROOT/'src'/name)==digest,name
    for row in protected['old_0503_artifacts']:assert sha(ROOT/row['path'])==row['sha256']
    from catalog_sync_bundle import verify_component
    verify_component(app/'_internal/components/jst_catalog',app/'_internal/components/aoxiang')
    files=sorted(p for p in app.rglob('*') if p.is_file());assert not any(p.suffix in ('.sqlite','.sqlite3','.db') for p in files)
    archive=delivery/'补货助手_0.50.4_rc1.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for f in files:z.write(f,f.relative_to(delivery).as_posix())
    with zipfile.ZipFile(archive) as z:assert z.testzip() is None and len(z.infolist())==len(files)
    release.mkdir(parents=True)
    def write(name,value):(release/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
    for name,proof in proofs.items():write(name+'.json',proof)
    write('protection.json',protected);write('manual-html.json',manual)
    comparison=json.loads((qa/'before-after-recovery.json').read_text('utf-8'));assert comparison['baseline_0503']['blocked'] and not comparison['candidate_0504']['blocked']
    write('before-after-recovery.json',comparison)
    shutil.copyfile(build/'build_info.json',release/'build_info.json')
    for file in ['python-full-final.log','python-final-special-02.log','node-aoxiang-final.log','node-jst-sealed.log','python-full-02.log','python-reexport.log','node-full-03.log']:
        shutil.copyfile(qa/file,release/(file.replace('.log','.txt')))
    screenshots=release/'screenshots';screenshots.mkdir()
    for image in (qa/'exe-reexport').glob('*.png'):shutil.copyfile(image,screenshots/image.name)
    artifacts={'source_commit':source,'files':[{'path':str(p.relative_to(ROOT)),'bytes':p.stat().st_size,'sha256':sha(p)} for p in (app/'补货助手.exe',archive)]}
    write('artifacts.json',artifacts)
    write('delivery-files.json',[{'path':p.relative_to(app).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in files])
    validation={'base':protected['base'],'source_commit':source,'python_full_tests':int(match[1]),'python_full_seconds':float(match[2]),
                'node_aoxiang_tests':counts['aoxiang'],'node_jst_tests':counts['jst'],'exe_checks':sum(len(v['checks']) for v in proofs.values()),
                'model_unchanged':True,'old_0503_preserved':True,'live_platform_tested':False,'business_pc_retested':False,'pushed':False,'merged':False}
    write('validation.json',validation)
    report=f'''# 0.50.4 rc1 补货建议重导与采集恢复交付

## 结果与改动范围

补货建议导出原来同时受主程序未完成任务、组件未决提交、结束门禁和同日文件路径约束。受理回执不明时，新建和结束可能互相拦截；已知原任务可以恢复，但不能明确放弃旧取数并重导。使用已保留0.50.3交付中的组件及本版组件，对同类隔离未知受理记录进行了纯本地结束预览：旧版返回受理未知阻塞，本版无该阻塞，详见before-after-recovery.json。这是流程锁定问题，本版不宣称改变平台本身生成文件的速度。

现在可勾选门店后“重新导出所选门店”，一次范围确认后安全退役所选旧项、独立建立新任务；旧平台任务可能继续完成，原始事实和文件保留。结束本地采集不再等待平台任务完成或核清受理结果。旧批其他项保留稳定身份和顺序；原先运行的其余项恢复，原先暂停的不会自动启动。

run/item/attempt、命令项和文件发布检查本地失效；晚到回执只追加到旧记录。原件按run独立保存，当前缓存不因较旧版本晚到而倒退，旧版保存现场退役后不阻塞新文件。补货与分析登记用途并隔离当前缓存；旧记录按原始上下文登记，不按店名猜归属。

缺货率分析的选中门店重导建立新修订，继承当时设置、冻结仓原件和未选已完成门店快照；旧日报保留，发布时再次检查当前日报没有被其他作业更新。历史日期不能混入今日库存。

本轮未改采购上传、收货业务、模型、预算、箱规、默认参数、仓归属或缺货率公式。七个核心源文件逐字节保持；旧0.50.3 EXE/ZIP哈希保持。商家、门店、查询范围、行数、关键字段、编码精度及文件完整性校验继续执行。

## 验证

| 范围 | 最终结果 |
|---|---|
| Python全量回归 | {match[1]}项通过，{match[2]}秒 |
| Python最终分析/重导专项 | 24项通过 |
| 固定随包Node的翱象组件全量 | {counts['aoxiang']}项通过 |
| 聚水潭组件回归 | {counts['jst']}项通过 |
| 固定源码实际EXE | 共{validation['exe_checks']}项；重导{len(proofs['exe-reexport']['checks'])}、分析{len(proofs['exe-stockout']['checks'])}、采购流水线{len(proofs['exe-pipeline']['checks'])}、资料生命周期{len(proofs['exe-catalog']['checks'])}，全部进程退出0 |
| 教程 | 离线HTML20章32图；图片、搜索、锚点、放大、移动宽度及无外部请求检查通过 |

实际EXE重导验收使用真实Qt和随包Node、受理未知合成记录及未配置Edge的隔离目录；验证取消/确认、所选范围、新旧记录、结束与重开，以及草稿/签发逐行签名、清单和原件哈希不变。分析验收使用实际独立进程、封闭假运输及合成Excel，包含只重导选中S、保留T快照和冻结仓资料。晚到回执、发布失败、事务回滚、过期预览和跨用途旧记录由组件/源码专项验证。

## 失败记录与已知限制

早期旧测试要求禁止结束未知受理，本轮按已批准行为替换为“明确退役但保留证据”的断言；未放宽完整性预期。新增测试曾误用重复日报唯一键、根目录相对文件夹和未关闭的测试连接，修正夹具后重跑。第一轮全量1400项的两个错误分别是新增重复键夹具和未设置子进程PYTHONPATH，最终以正确环境完整复跑。测试总数从早期1400变为1396，是去掉重复导入/继承造成的4次旧UI用例重复执行；原有用例仍由原模块完整运行，未删除业务断言。封装脚本在验收提交中补齐src模块自定位；运行包始终来自b3c1e4c固定源码，EXE未因此改变。原日志及初始运行器、预期变更/超时诊断均保存在 `{qa.relative_to(ROOT).as_posix()}`，没有覆盖。

没有连接真实翱象或聚水潭，没有删除真实业务数据，也没有在低配置业务电脑或异常现场实测；不把合成等待或流程恢复当作现场提速保证。重导可能产生重复平台导出文件；本地结束不会撤销它们。不同版本原件持续保留，反复重导会增加磁盘占用，沿用空间检查且不自动删旧文件。真实商家/门店不符、盘不足、状态库不完整或原件不合格时仍明确停止。分析的仓资料取数沿原机制恢复，不伪造完整原件。既有QLabel销毁回调及Qt弃用警告未在本轮修复；预期锁冲突/失败回滚测试会输出模拟错误日志，最终测试结论见完整日志。

## 交付与复跑

- 源码提交：`{source}`；基线：`{protected['base']}`。
- 分支：`feat/collection-reexport-0.50.4`；本轮仅本地提交、打包和验收，未推送、未合并。
- 构建：`tools/build_from_commit.py --commit {source} --build-id rc1`，独立Git归档，无工作区覆盖。
- EXE：`交付_0.50.4_rc1/补货助手/补货助手.exe`。
- ZIP：`交付_0.50.4_rc1/补货助手_0.50.4_rc1.zip`，完整解压后运行。
- 使用说明：`docs/0.50.4补货建议重导与采集恢复.md`；随包新人手册第20.2节，历史PDF明确保留为0.49.3归档。
- 重导EXE复跑：`tools/verify_collection_reexport.py --exe <新EXE> --out <qa下新目录>`。
- 分析EXE复跑：`tools/verify_stockout.py --exe <新EXE> --output <qa下新目录>`。
- 全量回归需 `PYTHONUTF8=1`、`PYTHONPATH=<项目绝对路径>/src`，再执行 `.venv/Scripts/python.exe -m unittest discover -s tests`。

业务端等当前动作安全结束后正常退出旧版，完整解压新包运行；无需初始化业务资料或重新绑定浏览器。所有旧交付、原始/失败证据和独立删除性能工作区保留。
'''
    (release/'交付报告.md').write_text(report,'utf-8',newline='\n');shutil.copyfile(release/'交付报告.md',delivery/'交付报告.md')
    print(json.dumps({'release':str(release),'artifacts':artifacts,'validation':validation},ensure_ascii=False))


if __name__=='__main__':main()
