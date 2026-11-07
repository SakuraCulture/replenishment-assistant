"""Instrument ordinary app startup without entering analytics; isolated settings only."""
import argparse,json,os,sys,time,threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();out=args.output.resolve()
    if not (out/'synthetic.json').is_file():raise ValueError('Marked QA required')
    sys.path[:0]=[str(args.source.resolve()/'src'),str(ROOT/'qa/export_071/qa-deps')]
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import psutil
    proc=psutil.Process();stop=threading.Event();peak=[proc.memory_info().rss];access=[]
    owned=str(out/'settings/analysis/stockout').casefold()
    def audit(event,arguments):
        if event in ('open','sqlite3.connect','os.scandir','os.listdir') and arguments:
            value=str(arguments[0]).replace('/','\\').casefold()
            if owned in value:access.append([event,value])
    sys.addaudithook(audit)
    def sample():
        while not stop.wait(.02):peak[0]=max(peak[0],proc.memory_info().rss)
    thread=threading.Thread(target=sample);thread.start();start=time.perf_counter();io=proc.io_counters()
    import application_window
    original=application_window.ApplicationWindow
    class Window(original):
        def __init__(self):
            super().__init__()
            from PySide6.QtCore import QTimer
            def finish():
                from PySide6.QtWidgets import QApplication
                entry=getattr(self,'stockout',None);t=time.perf_counter()
                for _ in range(100):self.work_repository.stores();self.work_repository.presets();self.work_repository.list_jobs()
                elapsed=time.perf_counter()-t;endio=proc.io_counters()
                report={'passed':not access and (not entry or entry.worker is None and entry.page is None and entry.timer is None),
                    'startup_seconds':t-start,'business_reads_100_seconds':elapsed,'rss':proc.memory_info().rss,'peak_rss':peak[0],
                    'analysis_access':access,'analysis_modules':[k for k in sys.modules if k.startswith('stockout_')],
                    'io':{k:getattr(endio,k)-getattr(io,k) for k in io._fields}}
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
                self._discard_close=True;self.job_dirty=False;self.close();QApplication.instance().exit(0 if report['passed'] else 1)
            QTimer.singleShot(400,finish)
    application_window.ApplicationWindow=Window
    import app
    try:code=app.main()
    finally:stop.set();thread.join()
    return code


if __name__=='__main__':sys.exit(main())
