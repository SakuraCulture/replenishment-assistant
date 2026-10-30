import os
os.environ['OPENPYXL_LXML']='False'
import sys,pickle,zlib,threading,json,subprocess
from pathlib import Path
sys.path.insert(0,'src')
import xlsx_exports as x
from runtime_support import Cancelled
from openpyxl.worksheet._writer import ALL_TEMP_FILES
r=pickle.loads(zlib.decompress(Path('qa/normal_070/rc3-candidate/real_None_True.pklz').read_bytes()));before=set(ALL_TEMP_FILES);stop=threading.Event();created=set();dest=Path('qa/export_071/final/cleanup-cancel.xlsx')
def progress(m):
 if getattr(m,'current',0)>=500:
  created.update(set(ALL_TEMP_FILES)-before);stop.set()
try:x.export_xlsx(r,dest,cancel=stop,progress=progress)
except Cancelled:pass
else:raise AssertionError('did not cancel')
assert created and not dest.exists() and not any(Path(p).exists() for p in created) and set(ALL_TEMP_FILES)==before
result={'passed':True,'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'created_temporary_xml_files':len(created),'remaining_files':0,'final_file_exists':False,'lxml':False}
Path('qa/export_071/final/cancel_cleanup.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(result)
