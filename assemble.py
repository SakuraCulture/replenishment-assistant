import json,zipfile,hashlib,shutil
from pathlib import Path
root=Path(__file__).parent
dest=root/'交付_0.3'/'补货助手'
qa=json.loads((root/'qa/v03/backend_report.json').read_text(encoding='utf-8'))
ui=json.loads((root/'qa/v03/packaged_ui/ui_report.json').read_text(encoding='utf-8'))
assert qa['passed'] and ui['passed']
for p,h in qa['source_hashes_unchanged'].items():assert hashlib.sha256(Path(p).read_bytes()).hexdigest()==h
assert not (dest/'_internal/icuuc.dll').exists()
toc=(root/'build_0.3/补货助手/Analysis-00.toc').read_text(encoding='utf-8')
assert 'native\\\\poppler' not in toc and 'native\\\\libheif' not in toc
notes='''补货助手 MVP 0.3.0 验收记录

1. 原模型核心及一箱例外规则不变；14,244行AS/BV与已验收结果一致。
2. 禁用/停采排除仍生效；关闭总预算时430款、12,268.87元，执行数量与0.2一致。
3. 清单从SKU明细准确提取2,142个必卖编码，忽略199个冬季独有编码，重复0；本店命中2,120个，缺22个，全部保留匹配审计。
4. 总预算测试0/1,000/3,000/5,000/10,000/12,268.87/20,000元，逐行检查不超候选量、按箱规倍数、资金不超预算、原计算不变。
5. 必卖先于普通；若必卖仍不足，则分给普通的资金加余额不足以再买一个未满足必卖的最小单位。独立测试覆盖整箱余款、价格待核、负数/布尔/非金额预算、零预算和最低持有缺口。
6. 清单保存、重启读取、备份还原、错误导入不覆盖旧清单、门店预算隔离记忆通过。
7. 3,000元场景程序导出读回：补货清单金额不超预算、无禁用/停采，预算未满足行数匹配，全店必卖匹配2,142行。
8. EXE在独立工作目录、去除Python路径后完成真实UI导入、计算、筛选、预算0及3,000元、缺口展示、预算导出、重新创建窗口恢复设置、导入清单使旧结果失效。
9. Windows界面、1080×720窗口与中文显示已目视检查；未验证其他电脑。
10. 原模型及三份输入工作簿SHA256不变。清单复用配置为独立JSON，不覆盖原始Excel。
'''
(root/'qa/v03/验收记录.txt').write_text(notes,encoding='utf-8-sig')
shutil.copy2(root/'qa/v03/验收记录.txt',dest/'验收记录.txt')
files=sorted(p for p in dest.rglob('*') if p.is_file())
manifest={str(p.relative_to(dest)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(root/'qa/v03/delivery_sha256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
archive=root/'交付_0.3'/'补货助手_MVP_0.3.zip'
with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in files:z.write(p,p.relative_to(dest.parent))
with zipfile.ZipFile(archive) as z:assert z.testzip() is None
report={'archive':str(archive),'zip_bytes':archive.stat().st_size,'uncompressed_bytes':sum(p.stat().st_size for p in files),'file_count':len(files),'exe_sha256':manifest['补货助手.exe'],'zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'source_files_unchanged':True,'packaged_ui_passed':True}
(root/'qa/v03/release_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=True,indent=2))
