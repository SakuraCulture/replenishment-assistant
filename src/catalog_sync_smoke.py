"""Opt-in frozen/source UI acceptance with synthetic downloaded native task (no platform)."""
import json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtWidgets import QApplication
from version_info import diagnostic

def start_smoke(app,win,args):
 out=Path(args[0]);out.mkdir(parents=True,exist_ok=True);checks=[];screens=[]
 def require(ok,message):
  if not ok:raise AssertionError(message)
  checks.append(message)
 def wait(fn,seconds=45):
  until=time.monotonic()+seconds
  while not fn() and time.monotonic()<until:
   loop=QEventLoop();QTimer.singleShot(25,loop.quit);loop.exec()
  require(fn(),'后台任务在限制时间内结束')
 def execute():
  d=None;ticks=[];timer=QTimer();timer.setInterval(20);timer.timeout.connect(lambda:ticks.append(time.monotonic()));timer.start()
  report={}
  try:
   from catalog_sync_ui import Dialog
   win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop()
   d=Dialog(win);d.show();require(d.resume_btn.isEnabled(),'原下载任务可恢复');require(not d.bind_btn.isEnabled(),'未核验不能保存账号');require(not d.stop.isEnabled(),'空闲不能停止')
   d.grab().save(str(out/'before.png'));screens.append('before.png');d.resume_btn.click();d.stop.click();wait(lambda:d.worker is None);require(not d.adopted,'立即停止保留下载任务，不采用资料');d.resume_btn.click();require(d.worker is not None,'真实按钮启动后台任务');require(not d.fetch.isEnabled(),'运行时禁止重复提交');wait(lambda:d.worker is None)
   require(len(d.adopted)==1,'真实 Node → 当前程序子进程校验 → 采用完成');require('山东仓' in win.catalogs and len(win.catalogs['山东仓']['items'])==2,'界面刷新本仓资料');require(win.input_dirty,'成功采用令当前输入需要重查')
   require(d.model.rows[0][2]=='已采用','采用结果显示准确');require(not d.model.rows[1][2]=='已采用','失败仓没有冒充成功');require('河北仓' not in win.catalogs,'失败不写另一仓')
   d.grab().save(str(out/'after.png'));screens.append('after.png');require(len(ticks)>5,'Qt持续响应');d.reject();require(not d.client.running,'关闭组件退出，无残留 Node');d.deleteLater();d=None
   d=Dialog(win);d.show();require(d.model.rows[0][2]=='已采用','重开读取已采用记录');d.resume_btn.click();wait(lambda:d.worker is None);require(len(d.adopted)==0,'再次继续不会重放成功仓');d.reject();require(not d.client.running,'重试后安全关闭');d.deleteLater();d=None
   report={'passed':True,'checks':checks,'screenshots':screens,'scope':'synthetic saved platform task; no real browser or platform; real Node and no-Qt validator; frozen flag distinguishes EXE/source','heartbeat_count':len(ticks),'version':diagnostic(),'frozen':bool(getattr(sys,'frozen',False))}
  except Exception:
   report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen':bool(getattr(sys,'frozen',False))}
  finally:
   timer.stop();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
   if d:
    if d.worker:d.cancel();wait(lambda:d.worker is None)
    d.reject();d.deleteLater()
   win._discard_close=True;win.close();app.exit(0 if report.get('passed') else 1)
 QTimer.singleShot(250,execute)
