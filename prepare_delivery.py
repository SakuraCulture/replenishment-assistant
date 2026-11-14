"""Attach local documentation and installed dependency license texts."""
import sys,shutil,json,argparse,importlib.metadata as md
from pathlib import Path
root=Path(__file__).parent
parser=argparse.ArgumentParser();parser.add_argument('--delivery',type=Path,required=True);args=parser.parse_args()
dest=args.delivery.resolve();assert (dest/'补货助手.exe').is_file()
version=json.loads((root/'src/version.json').read_text(encoding='utf-8'))
build=json.loads((dest/'_internal/build_info.json').read_text(encoding='utf-8-sig'))
assert build['version']==version['version']
if version['version'] in {'0.53.1','0.53.2'}:
    (dest/'docs').mkdir(exist_ok=True)
    for name in ('0.53.0双仓独立执行与分仓删除.md','0.53.1分配采用仓资料导出.md'):
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    if version['version']=='0.53.2':
        shutil.copy2(root/'docs/0.53.2界面模型重置与回收防护.md',dest/'docs/0.53.2界面模型重置与回收防护.md')
shutil.copy2(root/'使用说明.txt',dest/'使用说明.txt')
if version['version'] in {'0.52.0','0.52.1','0.52.2','0.52.3','0.52.4','0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    (dest/'docs').mkdir(exist_ok=True)
    shutil.copy2(root/'docs/0.52.0运营签发与分配规则.md',dest/'docs/0.52.0运营签发与分配规则.md')
if version['version'] in {'0.51.10','0.52.0','0.52.1','0.52.2','0.52.3','0.52.4','0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    (dest/'docs').mkdir(exist_ok=True)
    shutil.copy2(root/'docs/0.51.10迁移退出与实例识别修复.md',dest/'docs/0.51.10迁移退出与实例识别修复.md')
if version['version'] in {'0.52.1','0.52.2','0.52.3','0.52.4','0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.1数据保存位置与整库搬迁.md',dest/'docs/0.52.1数据保存位置与整库搬迁.md')
if version['version'] in {'0.52.2','0.52.3','0.52.4','0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.2资料搬迁可见进度.md',dest/'docs/0.52.2资料搬迁可见进度.md')
if version['version'] in {'0.52.3','0.52.4','0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.3搬迁资源释放与自动连接暂停.md',dest/'docs/0.52.3搬迁资源释放与自动连接暂停.md')
if version['version'] in {'0.52.4','0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.4搬迁SQLite事务日志校验修复.md',dest/'docs/0.52.4搬迁SQLite事务日志校验修复.md')
if version['version'] in {'0.52.5','0.52.6','0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.5签发反馈与原修订导出修复.md',dest/'docs/0.52.5签发反馈与原修订导出修复.md')
if version['version'] in {'0.52.6','0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.6核心链路性能优化.md',dest/'docs/0.52.6核心链路性能优化.md')
if version['version'] in {'0.52.7','0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.7内存释放与采购上传调度.md',dest/'docs/0.52.7内存释放与采购上传调度.md')
if version['version'] in {'0.52.8','0.52.9'}:
    shutil.copy2(root/'docs/0.52.8订单新增合并导入.md',dest/'docs/0.52.8订单新增合并导入.md')
if version['version']=='0.52.9':
    shutil.copy2(root/'docs/0.52.9回传重复商品核对与准确定位.md',dest/'docs/0.52.9回传重复商品核对与准确定位.md')
if version['version'] in {'0.51.6','0.51.7','0.51.8'}:
    (dest/'docs').mkdir(exist_ok=True)
    shutil.copy2(root/'docs/0.51.6经营分析参数迁移修复.md',dest/'docs/0.51.6经营分析参数迁移修复.md')
if version['version'] in {'0.51.7','0.51.8'}:
    shutil.copy2(root/'docs/0.51.7大仓缺货率导出.md',dest/'docs/0.51.7大仓缺货率导出.md')
if version['version']=='0.51.8':
    shutil.copy2(root/'docs/0.51.8付款后回传更正与原批重分配.md',dest/'docs/0.51.8付款后回传更正与原批重分配.md')
    shutil.copy2(root/'docs/0.51.8本月新品自动首铺.md',dest/'docs/0.51.8本月新品自动首铺.md')
    image_folder=dest/'docs/manual/images';image_folder.mkdir(parents=True,exist_ok=True)
    for name in ('paid-return-results-0518.png','paid-return-preview-0518.png'):
        shutil.copy2(root/'docs/manual/images'/name,image_folder/name)
if version['version'] in {'0.50.0','0.50.1','0.50.2','0.50.3'}:
    (dest/'docs').mkdir(exist_ok=True)
    shutil.copy2(root/'docs/0.50.0订单删除与任务失效.md',dest/'docs/0.50.0订单删除与任务失效.md')
if version['version'] in {'0.50.1','0.50.2','0.50.3'}:
    shutil.copy2(root/'docs/0.50.1当前Edge登录与共享连接.md',dest/'docs/0.50.1当前Edge登录与共享连接.md')
    shutil.copy2(root/'docs/0.50.1付款按商品编码认单.md',dest/'docs/0.50.1付款按商品编码认单.md')
if version['version'] in {'0.50.2','0.50.3'}:
    shutil.copy2(root/'docs/0.50.2采购异常恢复与等待流水线.md',dest/'docs/0.50.2采购异常恢复与等待流水线.md')
if version['version']=='0.50.3':
    shutil.copy2(root/'docs/0.50.3聚水潭采集服务退出与占锁恢复.md',dest/'docs/0.50.3聚水潭采集服务退出与占锁恢复.md')
if (root/'docs/manual/index.html').is_file():
    shutil.copytree(root/'docs/manual',dest/'使用手册')
if version['version'] in {'0.9.1','0.9.2','0.10.0','0.11.0','0.12.0','0.13.0','0.14.0','0.15.0','0.15.1'}:
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.7.1导出优化与批量确认单.md','0.7.2历史明细性能.md','0.7.3工作链路性能与过滤.md','0.7.4作业IO第一阶段.md','0.8.0分块存储与兼容.md','0.9.0业务日志与回传.md','0.9.1在途保底与历史查询.md','0.9.1代码疑问核查.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
if version['version'] in {'0.9.2','0.10.0','0.11.0','0.12.0','0.13.0','0.14.0','0.15.0','0.15.1'}:
    shutil.copy2(root/'docs/0.9.2已确认问题修复.md',dest/'docs/0.9.2已确认问题修复.md')
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
if version['version'] in {'0.7.1','0.7.2','0.7.3','0.7.4','0.8.0','0.9.0'}:
    (dest/'docs').mkdir(exist_ok=True);shutil.copy2(root/'docs/0.7.1导出优化与批量确认单.md',dest/'docs/0.7.1导出优化与批量确认单.md')
if version['version'] in {'0.7.2','0.7.3','0.7.4','0.8.0','0.9.0'}:shutil.copy2(root/'docs/0.7.2历史明细性能.md',dest/'docs/0.7.2历史明细性能.md')
if version['version'] in {'0.7.3','0.7.4','0.8.0','0.9.0'}:shutil.copy2(root/'docs/0.7.3工作链路性能与过滤.md',dest/'docs/0.7.3工作链路性能与过滤.md')
if version['version'] in {'0.7.4','0.8.0','0.9.0'}:shutil.copy2(root/'docs/0.7.4作业IO第一阶段.md',dest/'docs/0.7.4作业IO第一阶段.md')
if version['version'] in {'0.8.0','0.9.0'}:shutil.copy2(root/'docs/0.8.0分块存储与兼容.md',dest/'docs/0.8.0分块存储与兼容.md')
if version['version']=='0.9.0':shutil.copy2(root/'docs/0.9.0业务日志与回传.md',dest/'docs/0.9.0业务日志与回传.md')
if version['version'] in {'0.10.0','0.11.0','0.12.0','0.13.0','0.14.0','0.15.0','0.15.1'}:shutil.copy2(root/'docs/0.10.0分仓确认单与门店默认.md',dest/'docs/0.10.0分仓确认单与门店默认.md')
if version['version'] in {'0.11.0','0.12.0','0.13.0','0.14.0','0.15.0','0.15.1'}:shutil.copy2(root/'docs/0.11.0分仓归档与回收站.md',dest/'docs/0.11.0分仓归档与回收站.md')
if version['version'] in {'0.12.0','0.13.0','0.14.0','0.15.0','0.15.1'}:shutil.copy2(root/'docs/0.12.0历史批量独立导出.md',dest/'docs/0.12.0历史批量独立导出.md')
if version['version'] in {'0.13.0','0.14.0','0.15.0','0.15.1'}:
    shutil.copy2(root/'docs/0.13.0交互整改与33项清单.md',dest/'docs/0.13.0交互整改与33项清单.md')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.13.0交互整改与33项清单.md').read_bytes())
if version['version']=='0.14.0':
    shutil.copy2(root/'docs/0.14.0首页待办与实际下单.md',dest/'docs/0.14.0首页待办与实际下单.md')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.14.0首页待办与实际下单.md').read_bytes())
if version['version'] in {'0.15.0','0.15.1'}:
    shutil.copy2(root/'docs/0.15.0订单生命周期.md',dest/'docs/0.15.0订单生命周期.md')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.15.0订单生命周期.md').read_bytes())
if version['version']=='0.15.1':
    shutil.copy2(root/'docs/0.15.1默认导出位置.md',dest/'docs/0.15.1默认导出位置.md')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.15.1默认导出位置.md').read_bytes())
licenses=dest/'licenses';licenses.mkdir(exist_ok=True)
if version['version'] in {'0.16.0','0.16.1'}:
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.15.0订单生命周期.md','0.15.1默认导出位置.md','0.16.0逐环节核对方案.md','0.16.0使用说明.md','商品资料保存链路_0.16.0.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.16.0使用说明.md').read_bytes())
if version['version']=='0.16.1':
    shutil.copy2(root/'docs/0.16.1简化生命周期导入.md',dest/'docs/0.16.1简化生命周期导入.md')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.16.1简化生命周期导入.md').read_bytes())
    sys.path.insert(0,str(root/'src'))
    import lifecycle_io
    templates=dest/'导入模板';templates.mkdir(exist_ok=True)
    for stage in lifecycle_io.STAGES:lifecycle_io.template(stage,templates/(lifecycle_io.TITLES[stage]+'模板.xlsx'))
if (root/'third_party_licenses').exists():shutil.copytree(root/'third_party_licenses',licenses,dirs_exist_ok=True)
if version['version'] in {'0.17.0','0.17.1','0.18.0','0.18.1','0.19.0','0.19.1','0.20.0'}:
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.17.0四场景与批量单据.md','四个主干业务场景与低认知交互方案_V1.md','商品资料保存链路_0.16.0.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.17.0四场景与批量单据.md').read_bytes())
    sys.path.insert(0,str(root/'src'))
    import lifecycle_io
    templates=dest/'导入模板';templates.mkdir(exist_ok=True)
    for stage in lifecycle_io.STAGES:lifecycle_io.template(stage,templates/(lifecycle_io.TITLES[stage]+'模板.xlsx'))
if version['version'] in {'0.18.0','0.18.1','0.19.0','0.19.1','0.20.0'}:
    name='0.18.0签发前最终清单.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes())
    import import_templates
    import_templates.write('stocking',dest/'导入模板/铺货清单最低模板.xlsx')
if version['version'] in {'0.18.1','0.19.0','0.19.1','0.20.0'}:
    name='0.18.1缺从仓资料保留主仓.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    with (dest/'使用说明.txt').open('a',encoding='utf-8') as f:
        f.write('\n\n'+(root/'docs'/name).read_text(encoding='utf-8'))
if version['version'] in {'0.19.0','0.19.1','0.20.0'}:
    name='0.19.0搜索选品与清单补足.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_text((root/'docs'/name).read_text(encoding='utf-8')+'\n\n'+(dest/'使用说明.txt').read_text(encoding='utf-8'),encoding='utf-8')
if version['version']=='0.17.1':
    shutil.copy2(root/'docs/0.17.1签发同名覆盖.md',dest/'docs/0.17.1签发同名覆盖.md')
    with (dest/'使用说明.txt').open('a',encoding='utf-8') as f:
        f.write('\n\n'+(root/'docs/0.17.1签发同名覆盖.md').read_text(encoding='utf-8'))
if version['version'] in {'0.19.1','0.20.0'}:
    name='0.19.1核对签发金额.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes()+b'\n\n'+(dest/'使用说明.txt').read_bytes())
versions={}
for name in ['PySide6-Essentials','shiboken6','openpyxl','et-xmlfile','pillow','PyInstaller']:
    dist=md.distribution(name);versions[name]=dist.version
    (licenses/name).mkdir(exist_ok=True)
    (licenses/name/'package-metadata.txt').write_text(dist.read_text('METADATA') or '',encoding='utf-8')
    for entry in dist.files or []:
        if any(token in str(entry).lower() for token in ['license','licence','copying']) and ('dist-info' in str(entry) or '.dist-info' in str(entry)):
            src=Path(dist.locate_file(entry))
            if src.is_file():
                sub=licenses/name/str(entry).split('.dist-info/',1)[-1]
                sub.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,sub)
python_license=Path(sys.base_prefix)/'LICENSE.txt'
if python_license.exists():shutil.copy2(python_license,licenses/'Python-LICENSE.txt')
(dest/'版本信息.json').write_text(json.dumps({**version,**build,'python':sys.version,'packages':versions},ensure_ascii=False,indent=2),encoding='utf-8')
(licenses/'组件来源.txt').write_text('Python: https://www.python.org/\nQt / PySide6 (unmodified dynamic libraries): https://download.qt.io/official_releases/QtForPython/pyside6/\nQt: https://download.qt.io/official_releases/qt/\nopenpyxl: https://openpyxl.readthedocs.io/\nPillow: https://python-pillow.org/\nPyInstaller: https://pyinstaller.org/\n随包Qt/PySide6以动态库方式使用；可用兼容版本库替换_internal内的对应文件。第三方组件适用随包各自许可。\n',encoding='utf-8')
print(json.dumps(versions))

if version['version']=='0.20.0':
    name='0.20.0最终清单与筛选.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes())

if version['version']=='0.21.0':
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.17.0四场景与批量单据.md','0.18.0签发前最终清单.md','0.20.0最终清单与筛选.md','0.21.0日批次与聚水潭订单.md','商品资料保存链路_0.16.0.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.21.0日批次与聚水潭订单.md').read_bytes())
    sys.path.insert(0,str(root/'src'))
    import lifecycle_io,import_templates,business_excel,warehouse_catalog
    templates=dest/'导入模板';templates.mkdir(exist_ok=True)
    for stage in lifecycle_io.STAGES:lifecycle_io.template(stage,templates/(lifecycle_io.TITLES[stage]+'模板.xlsx'))
    import_templates.write('stocking',templates/'铺货清单最低模板.xlsx')
    business_excel.write(templates/'仓商品资料最低模板.xlsx',[('商品资料',warehouse_catalog.FIELDS,[])])

if version['version'] in {'0.23.0','0.24.0','0.25.0','0.26.0','0.26.1','0.27.0','0.27.1','0.28.0'}:
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.17.0四场景与批量单据.md','0.20.0最终清单与筛选.md','0.22.0付款来源与自动生成.md','0.22.1聚水潭门店汇总.md','0.23.0聚水潭结果与导出.md','商品资料保存链路_0.16.0.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_bytes((root/'docs/0.23.0聚水潭结果与导出.md').read_bytes())
    sys.path.insert(0,str(root/'src'))
    import lifecycle_io,import_templates,business_excel,warehouse_catalog
    templates=dest/'导入模板';templates.mkdir(exist_ok=True)
    for stage in lifecycle_io.STAGES:lifecycle_io.template(stage,templates/(lifecycle_io.TITLES[stage]+'模板.xlsx'))
    import_templates.write('stocking',templates/'铺货清单最低模板.xlsx')
    business_excel.write(templates/'仓商品资料最低模板.xlsx',[('商品资料',warehouse_catalog.FIELDS,[])])

if version['version'] in {'0.24.0','0.25.0','0.26.0','0.26.1','0.27.0','0.27.1','0.28.0'}:
    for name in ['0.24.0翱象本地候选.md','翱象采购上传与关联.md','翱象收货同步与终态恢复.md','翱象批次作业与周期同步.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs/0.24.0翱象本地候选.md').read_bytes())
    import_templates.write('aoxiang_stores',dest/'导入模板/翱象门店目录最低模板.xlsx')

if version['version'] in {'0.25.0','0.26.0','0.26.1','0.27.0','0.27.1','0.28.0'}:
    name='0.25.0签发单隐藏单号与批量调参.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes())
    import_templates.write('store_parameters',dest/'导入模板/门店参数最低模板.xlsx')
if version['version'] in {'0.26.0','0.26.1','0.27.0','0.27.1','0.28.0'}:
    name='0.26.0完整门店参数.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes())
    (dest/'导入模板/门店参数最低模板.xlsx').rename(dest/'导入模板/门店参数完整模板.xlsx')
if version['version'] in {'0.26.1','0.27.0','0.27.1','0.28.0'}:
    name='0.26.1门店默认重新载入.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes())
if version['version'] in {'0.27.0','0.27.1','0.28.0'}:
    name='0.27.0数据与任务维护.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_bytes((root/'docs'/name).read_bytes())

if version['version'] in {'0.27.1','0.28.0'}:
    name='0.27.1后台锁恢复修复.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs'/name,dest/'使用说明.txt')

if version['version']=='0.28.0':
    name='0.28.0业务可读性.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs'/name,dest/'使用说明.txt')

if version['version'] in {'0.32.0','0.32.1','0.32.2','0.33.0','0.33.1','0.34.0','0.35.0','0.35.1'}:
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.32.0使用与结构.md','0.32.0实施记录.md','聚水潭双仓完整验证_20261103.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs/0.32.0使用与结构.md',dest/'使用说明.txt')
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    sys.path.insert(0,str(root/'src'))
    import parameter_tables,import_templates,lifecycle_io
    templates=dest/'导入模板';templates.mkdir(exist_ok=True)
    for kind in ['warehouse_cycles','classified_parameters','store_policy','store_routes','store_budget','store_qualification','store_refusals']:
        import_templates.write(kind,templates/(import_templates.TEMPLATES[kind][0]+'模板.xlsx'))
    for stage in lifecycle_io.STAGES:lifecycle_io.template(stage,templates/(lifecycle_io.TITLES[stage]+'模板.xlsx'))

if version['version'] in {'0.32.1','0.32.2'}:
    shutil.copy2(root/'docs/0.32.1彻底删除性能修复.md',dest/'docs/0.32.1彻底删除性能修复.md')
    shutil.copy2(root/'docs/0.32.1彻底删除性能修复.md',dest/'使用说明.txt')

if version['version']=='0.32.2':
    shutil.copy2(root/'docs/0.32.2任务快照与聚水潭依据修复.md',dest/'docs/0.32.2任务快照与聚水潭依据修复.md')
    shutil.copy2(root/'docs/0.32.2任务快照与聚水潭依据修复.md',dest/'使用说明.txt')

if version['version'] in {'0.33.0','0.33.1','0.34.0','0.35.0','0.35.1'}:
    name='0.33.0批次库存与处理范围.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs'/name,dest/'使用说明.txt')
    import_templates.write('catalog',templates/'仓商品资料最低模板.xlsx')

if version['version'] in {'0.33.1','0.34.0','0.35.0','0.35.1'}:
    name='0.33.1翱象采集结束与记录删除.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs'/name,dest/'使用说明.txt')

if version['version'] in {'0.34.0','0.35.0','0.35.1'}:
    name='0.34.0参数分组与预案.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs'/name,dest/'使用说明.txt')
    for kind in ['common_parameters','global_category_mapping']:
        import_templates.write(kind,templates/(import_templates.TEMPLATES[kind][0]+'模板.xlsx'))

if version['version'] in {'0.35.0','0.35.1'}:
    name='0.35.0AI协助使用与结构.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'docs'/name,dest/'使用说明.txt')
if version['version']=='0.35.1':
    name='0.35.1AI常用业务与提示词.md'
    shutil.copy2(root/'docs'/name,dest/'docs'/name)
    (dest/'使用说明.txt').write_text((root/'docs'/name).read_text('utf-8')+'\n\n'+(dest/'使用说明.txt').read_text('utf-8'),encoding='utf-8')

if version['version']=='0.36.1':
    (dest/'docs').mkdir(exist_ok=True)
    for name in ['0.36.0业务待办与核对.md','0.36.1浏览器连接保留.md']:
        shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_text((root/'docs/0.36.1浏览器连接保留.md').read_text('utf-8')+'\n\n'+(root/'docs/0.36.0业务待办与核对.md').read_text('utf-8'),encoding='utf-8')

if version['version']=='0.36.2':
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.36.2底部连接状态.md','0.36.1浏览器连接保留.md','0.36.0业务待办与核对.md']
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_text('\n\n'.join((root/'docs'/name).read_text('utf-8') for name in names),encoding='utf-8')

if version['version']=='0.37.0':
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.37.0定时补货.md','0.36.2底部连接状态.md','0.36.1浏览器连接保留.md']
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_text('\n\n'.join((root/'docs'/name).read_text('utf-8') for name in names),encoding='utf-8')

if version['version'] in {'0.38.0','0.38.1'}:
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.38.0AI与CLI查询试算.md','0.37.0定时补货.md','0.35.0AI协助使用与结构.md','0.35.1AI常用业务与提示词.md']
    if version['version']=='0.38.1':names.insert(0,'0.38.1参数业务字典.md')
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_text((root/'docs'/names[0]).read_text('utf-8'),encoding='utf-8')

if version['version']=='0.39.0':
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.39.0业务使用指南.md','0.39.0经营分析开发记录.md','0.38.1参数业务字典.md','0.38.0AI与CLI查询试算.md','0.35.0AI协助使用与结构.md']
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    shutil.copy2(root/'docs'/names[0],dest/'使用说明.txt')

if version['version']=='0.44.0':
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.44.0预计可售天数.md','0.39.0业务使用指南.md','0.40.0AI口径与本机连接.md',
           '0.42.0参数批量快捷设置.md','0.43.0准入现状查询.md','0.43.1批量解除拒收.md']
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    shutil.copy2(root/'docs'/names[0],dest/'使用说明.txt')

if version['version'] in {'0.45.0','0.45.1'}:
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.45.0货盘结构分析.md','0.44.0预计可售天数.md','0.39.0业务使用指南.md','0.40.0AI口径与本机连接.md',
           '0.42.0参数批量快捷设置.md','0.43.0准入现状查询.md','0.43.1批量解除拒收.md']
    if version['version']=='0.45.1':names.insert(0,'0.45.1删除负载优化.md')
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    shutil.copy2(root/'docs'/names[0],dest/'使用说明.txt')

if version['version'] in {'0.46.1','0.46.2'}:
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.46.1仅保留当前快照格式.md','0.46.0采集恢复与浏览器自动配置.md','0.45.2删除性能优化.md',
           '0.45.0货盘结构分析.md','0.39.0业务使用指南.md','0.40.0AI口径与本机连接.md']
    if version['version']=='0.46.2':names.insert(0,'0.46.2删除流式处理优化.md')
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    (dest/'使用说明.txt').write_text('\n\n'.join((root/'docs'/name).read_text('utf-8') for name in names[:3 if version['version']=='0.46.2' else 2]),encoding='utf-8')

if version['version'] in {'0.46.4','0.46.5','0.47.0','0.47.1','0.47.2','0.47.3','0.47.4','0.47.5'}:
    (dest/'docs').mkdir(exist_ok=True)
    names=['0.46.4作废采购核对与删除恢复.md','0.46.3采购恢复与异常说明.md','0.46.2删除流式处理优化.md',
           '0.46.1仅保留当前快照格式.md','0.46.0采集恢复与浏览器自动配置.md','0.39.0业务使用指南.md','0.40.0AI口径与本机连接.md']
    if version['version'] in {'0.46.5','0.47.0','0.47.1','0.47.2','0.47.3','0.47.4','0.47.5'}:names.insert(0,'0.46.5单仓商品归属修正.md')
    if version['version'] in {'0.47.0','0.47.1','0.47.2','0.47.3','0.47.4','0.47.5'}:names.insert(0,'0.47.0门店缺货率分析.md')
    if version['version'] in {'0.47.1','0.47.2','0.47.3','0.47.4','0.47.5'}:names.insert(0,'0.47.1缺货率翱象并行导出.md')
    if version['version'] in {'0.47.2','0.47.3','0.47.4','0.47.5'}:names.insert(0,'0.47.2启动自动连接与登录核验.md')
    if version['version'] in {'0.47.3','0.47.4','0.47.5'}:names.insert(0,'0.47.3分析供货设置与名称显示.md')
    if version['version'] in {'0.47.4','0.47.5'}:names.insert(0,'0.47.4缺货率状态剔除与作业管理.md')
    if version['version']=='0.47.5':names.insert(0,'0.47.5全部初始化与勾选采购任务.md')
    for name in names:shutil.copy2(root/'docs'/name,dest/'docs'/name)
    shutil.copy2(root/'requirements-win-amd64.lock',dest/'requirements-win-amd64.lock')
    shutil.copy2(root/'docs'/names[0],dest/'使用说明.txt')
