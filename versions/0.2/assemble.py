import json,zipfile,hashlib,shutil
from pathlib import Path
root=Path(__file__).parent
dest=root/'交付_0.2'/'补货助手'
qa=json.loads((root/'qa/v02/backend_report.json').read_text(encoding='utf-8'))
ui=json.loads((root/'qa/v02/packaged_ui/ui_report.json').read_text(encoding='utf-8'))
assert qa['passed'] and ui['passed']
for p,h in qa['source_hashes_unchanged'].items():assert hashlib.sha256(Path(p).read_bytes()).hexdigest()==h
assert not (dest/'_internal/icuuc.dll').exists()
toc=(root/'build_0.2/补货助手/Analysis-00.toc').read_text(encoding='utf-8')
assert 'native\\\\poppler' not in toc and 'native\\\\libheif' not in toc
notes='''补货助手 MVP 0.2.0 验收记录

1. 计算基准：原模型与已批准的一箱例外副本，使用鹿泉店本批两份输入。
2. 逐行对比：14,244行；关闭例外时AS/BV全行等于原模型，开启时全行等于已批准副本。
3. 计算口径：8个一箱例外新增378.80元，其余14,236行计算数量金额不变。原模型和一箱例外代码均未改动。
4. 地采：5,161行；异常1行同时属于地采；资料匹配9,083行。
5. 边界测试：11项通过，覆盖关闭开关、到货边界、在途、停采/禁用、缺数据、无销量、非箱规、已有订单、不修改原对象。
6. 新增执行层过滤：禁用1,973行，停采1,564行，同时命中268行，去重后排除3,269行。所有已排除商品执行量/金额为0；其余10,975行执行量/金额与上一版一致。单独和同时命中的边界检查通过。
7. 全量导出14,244行，补货清单430行，已排除3,269行，地采页5,161行，例外页8行。逐条验证补货清单无禁用/停采，已排除执行量/金额为0，计算参考保留。商品编码为文本，无意外生成的Excel公式。
8. 打包后的EXE：在不含Python/开发工具的PATH、其他工作目录下，完成导入、计算、地采/例外/禁用/停采/已排除筛选、搜索、详情、关闭及恢复例外、筛选导出、参数失效检查。
9. 目视检查Windows界面及1080×720窗口，中文文字、执行金额、已排除和计算参考可见。
10. 沿用已修复的隔离构建PATH，交付包不包含冲突的宿主ICU DLL，EXE完整测试通过。
11. 默认补货430款、12,268.87元；排除部分原计算金额11,564.68元。关闭一箱例外后422款、11,890.07元。
12. 原模型文件和输入工作簿SHA256核验保持不变。未验证其他电脑。
'''
(root/'qa/v02/验收记录.txt').write_text(notes,encoding='utf-8-sig')
shutil.copy2(root/'qa/v02/验收记录.txt',dest/'验收记录.txt')
files=sorted(p for p in dest.rglob('*') if p.is_file())
manifest={str(p.relative_to(dest)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(root/'qa/v02/delivery_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
archive=root/'交付_0.2'/'补货助手_MVP_0.2.zip'
with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in files:z.write(p,p.relative_to(dest.parent))
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
report={'archive':str(archive),'zip_bytes':archive.stat().st_size,'uncompressed_bytes':sum(p.stat().st_size for p in files),'file_count':len(files),'exe_sha256':manifest['补货助手.exe'],'zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'source_files_unchanged':True,'packaged_ui_passed':True}
(root/'qa/v02/release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=True,indent=2))
