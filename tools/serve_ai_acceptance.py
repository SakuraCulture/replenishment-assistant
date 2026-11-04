"""Own an isolated synthetic workbench for real-client natural-language checks."""
import argparse
import copy
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    sys.path.insert(0,str(ROOT/'src'))
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from application_window import ApplicationWindow
    from parameter_workspace_smoke import fixture
    from automation_ui import connection_configs
    from automation_playbooks import render
    from ui_controls import STYLE
    import batch_service,work_repository as wr,archive_orders
    app=QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    win=ApplicationWindow();win.setWindowTitle('隔离验收 · 合成数据 · 不连接真实平台')
    sample=fixture();rows=[]
    for d,m in sample['normalized']:
        for i in range(4):
            x,y=copy.deepcopy(d),copy.deepcopy(m);x.update(E=str(1001+i),D='合成商品'+str(i+1))
            if i==3:x['S']='禁用'
            y['export_row']=i+2;rows.append((x,y))
    sample['normalized']=rows;win.loaded_ok(sample)
    for code in ('A','B'):win.store_widgets[code]['mode'].setCurrentIndex(1)
    for code in ('A','B'):
        win.results[code]=batch_service.calculate_store(win.loaded,code,win.selected_settings(code),win.profile)
        win.states[code]['state']='已完成'
    order=win.orders.confirm(win.snapshot(),['A'],None,{'A':None})[0]
    order_id=archive_orders.number(order['number'],'山东仓')
    win.results={}
    for code in ('A','B'):win.states[code]['state']='待计算'
    win.refresh_tables();win.automation.enable();win.show()
    config=connection_configs(win)
    (out/'clients.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),'utf-8')
    before=copy.deepcopy(win.snapshot());formal=(win.work_repository.list_jobs(),win.orders.headers(),win.work_repository.stores())
    def save(name,value):
        temp=out/(name+'.tmp');temp.write_text(wr.dumps(value),'utf-8');os.replace(temp,out/name)
    last=[None]
    def tick():
        if (out/'stop').exists():
            if win.automation.worker:return
            save('final.json',{'formal_unchanged':formal==(win.work_repository.list_jobs(),win.orders.headers(),win.work_repository.stores()),'current_unchanged':before==win.snapshot(),'tasks':win.automation.journal.list(0,100)})
            win.job_dirty=False;win._discard_close=True;win.close();app.quit();return
        path=out/'request.json'
        if not path.exists():return
        try:request=json.loads(path.read_text('utf-8'))
        except ValueError:return
        if request['id']==last[0]:return
        last[0]=request['id']
        case=request['case'];values={};g=win.automation
        try:
            if case in ('check','calculate'):values={'stores':['B','A']}
            elif case=='compare':values={'stores':['A'],'warehouse':'山东仓','days_a':5,'days_b':7}
            elif case=='history':values={'order_id':order_id,'warehouse':'山东仓','sku':'1001'}
            else:
                tids=[tid for tid,r in g.calculations.items() if isinstance(r,dict) and not r.get('parent')]
                if not tids:raise ValueError('No real calculated task available')
                tid=tids[-1]
                values={'task_id':tid}
                if case=='explain':values.update(stores=['A'],warehouse='山东仓',sku='1001')
                if case=='adopt':values['reviewed']=True
            prompt=render(case,g.workbench_context({}),values)
            save('response.json',{'id':request['id'],'case':case,'prompt':prompt,'values':values})
        except Exception as exc:save('response.json',{'id':request['id'],'error':str(exc)})
    timer=QTimer(win);timer.setInterval(100);timer.timeout.connect(tick);timer.start()
    save('ready.json',{'synthetic':True,'pid':os.getpid(),'order_id':order_id,'app':'source','started':time.time()})
    app.exec()


if __name__=='__main__':
    multiprocessing.freeze_support();main()
