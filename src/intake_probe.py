"""Opt-in acceptance tasks only; never selected by normal application checks."""
import json,os,time,sys
from pathlib import Path

def mutate_reader(target):
    import engine
    original=engine.adapter.read_table
    def read(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        if str(Path(path).resolve())==str(Path(target).resolve()):
            with Path(path).open('ab') as f:f.write(b'QA-read-change')
        return result
    engine.adapter.read_table=read
    return original

def probe_file(index,path,*,folder,mode):
    from file_intake import check_file
    folder=Path(folder);pid=os.getpid()
    def record(state):
        (folder/f'{index}_{state}.json').write_text(json.dumps({'index':index,'pid':pid,'time':time.perf_counter(),'state':state,'qt_instance':bool(sys.modules.get('PySide6.QtWidgets') and sys.modules['PySide6.QtWidgets'].QApplication.instance())}),encoding='utf-8')
    record('start')
    if mode=='crash' and index==0:os._exit(23)
    if mode in {'reverse','cancel','close'}:time.sleep(2 if index==0 else .05)
    original=mutate_reader(path) if mode=='change' and index==0 else None
    try:
        result=check_file(index,path);record('finish');return result
    finally:
        if original:
            import engine
            engine.adapter.read_table=original
