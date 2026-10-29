import subprocess,sys,os,json
from pathlib import Path
root=Path(__file__).parent
qa=root/'qa'/'packaged_ui';qa.mkdir(parents=True,exist_ok=True)
source=Path(r'D:\企业微信\WXWork\1688855943717538\Cache\File\2026-10')
env=os.environ.copy()
for k in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(k,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR']
exe=root/'交付'/'补货助手'/'补货助手.exe'
run=subprocess.run([str(exe),'--smoke',str(source/'补货建议导出服务.xlsx'),str(source/'混合仓商品资料.xlsx'),str(qa)],env=env,cwd=qa,timeout=260)
assert run.returncode==0,run.returncode
report=json.loads((qa/'ui_report.json').read_text(encoding='utf-8'))
assert report['passed'],report
print(json.dumps(report,ensure_ascii=True,indent=2))
