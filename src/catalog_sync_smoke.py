"""Opt-in frozen/source UI acceptance with synthetic downloaded native task (no platform)."""
import json,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtWidgets import QApplication
from version_info import diagnostic

def start_smoke(app,win,args):
 app.setQuitOnLastWindowClosed(False)
 out=Path(args[0]);out.mkdir(parents=True,exist_ok=True);checks=[];screens=[]
 real=json.loads((out/'real_catalogs.json').read_text('utf-8')) if (out/'real_catalogs.json').exists() else None
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
   if not real:
    import hashlib
    d.warehouse.setCurrentText('山东仓')
    before=d.client.request('sync.status');account=d.account_data();pid=d.client.owner.client.process.pid
    native=before['batches'][0]['items'][0]['native'];saved_hash=hashlib.sha256(Path(native['file']).read_bytes()).hexdigest()
    d.manual.show();d.disable_btn.click()
    state=d.client.request('sync.status');disabled=next(a for a in state['accounts'] if a['warehouse']==account['warehouse'])
    require(not disabled['enabled'] and disabled['bindingVersion']==account['bindingVersion'],'有待处理原任务仍可停用，不改变账号绑定版本')
    require(state['batches']==before['batches'],'停用保留原批次、平台任务号和下载证据')
    require([a for a in state['accounts'] if a['warehouse']!=account['warehouse']]==[a for a in before['accounts'] if a['warehouse']!=account['warehouse']],'停用仅影响所选仓，另一仓保持不变')
    require(d.enable_btn.isEnabled() and not d.fetch.isEnabled(),'停用后按钮与手动刷新状态同步')
    require(d.client.owner.client.process.pid==pid,'停用未关闭或重启原连接进程')
    require('原任务' in d.note.text() and '保留' in d.note.text(),'停用结果清楚说明原任务与连接保留')
    d.grab().save(str(out/'disabled.png'));screens.append('disabled.png');d.reject();d.deleteLater();d=None
    d=Dialog(win);d.show();d.warehouse.setCurrentText(account['warehouse']);d.manual.show();require(not d.account_data()['enabled'],'关闭并重开窗口仍保留停用状态')
    d.enable_btn.click();restored=d.client.request('sync.status')
    require(d.account_data()['enabled'] and d.account_data()['bindingVersion']==account['bindingVersion'],'再次启用保留原账号身份，原任务可继续')
    require(restored['batches']==before['batches'] and hashlib.sha256(Path(native['file']).read_bytes()).hexdigest()==saved_hash,'启停往返不改写原任务或下载原件')
   d.grab().save(str(out/'before.png'));screens.append('before.png');d.resume_btn.click();d.stop.click();wait(lambda:d.worker is None);require(not d.adopted,'立即停止保留下载任务，不采用资料');d.resume_btn.click();require(d.worker is not None,'真实按钮启动后台任务');require(not d.fetch.isEnabled(),'运行时禁止重复提交');wait(lambda:d.worker is None)
   if real:
    require(len(d.adopted)==2,'两仓真实下载文件由 EXE 校验及采用')
    from source_archive import read
    from contextlib import closing
    import hashlib
    for wh,expected in real.items():
     d.warehouse.setCurrentText(wh)
     require(d.company.text()==expected['company'],'账号公司随所选仓正确显示：'+wh)
     require(len(win.catalogs[wh]['items'])==expected['rows'],'真实行数及仓标签一致：'+wh)
     with closing(win.work_repository.connect()) as db:stored=read(db,expected['sha256'])
     require(stored is not None and hashlib.sha256(stored[1]).hexdigest()==expected['sha256'],'原件由实际程序完整落库：'+wh)
    require(all(row[2]=='已采用' for row in d.model.rows),'两仓采用状态准确')
   else:
    require(len(d.adopted)==1,'真实 Node → 当前程序子进程校验 → 采用完成');require('山东仓' in win.catalogs and len(win.catalogs['山东仓']['items'])==2,'界面刷新本仓资料')
    require(d.model.rows[0][2]=='已采用','采用结果显示准确');require(not d.model.rows[1][2]=='已采用','失败仓没有冒充成功');require('河北仓' not in win.catalogs,'失败不写另一仓')
   require(win.input_dirty,'成功采用令当前输入需要重查')
   require(d.bar.maximum()==2 and d.bar.value()==2,'最终采用/错误回执刷新实际完成进度为100%')
   d.grab().save(str(out/'after.png'));screens.append('after.png');require(len(ticks)>5,'Qt持续响应')
   owner=win.catalog_sessions.session;component=owner.client;pid=component.process.pid
   d.reject();require(not d.client.running and owner.lease is None,'关闭窗口释放独占操作权');require(component.running,'关闭窗口保留真实 Node 进程');d.deleteLater();d=None
   d=Dialog(win);d.show();require(owner.client.process.pid==pid,'重开默认资料窗口复用原进程');require(d.model.rows[0][2]=='已采用','重开读取已采用记录');d.resume_btn.click();wait(lambda:d.worker is None);require(len(d.adopted)==0,'再次继续不会重放成功仓');d.reject();require(component.running,'停止和重试后仍保留原连接');d.deleteLater();d=None
   d=Dialog(win,purpose='allocation_baseline');d.show();require(owner.client.process.pid==pid,'分配库存入口复用同一进程');require(not d.resume_btn.isEnabled(),'不同用途不能串用原下载任务');d.reject();d.deleteLater();d=None
   require(win.aoxiang_workflows.session.client is None,'聚水潭窗口没有启动或重置翱象连接')
   from catalog_sync_ui import open_sync
   def close_during_work():
    current=win._large_io_dialog;current.resume_btn.click()
    require(current.worker is not None,'窗口内再次继续启动后台任务')
    win._discard_close=True;win.close()
    require(current.close_pending and not current._completed,'主软件关闭先请求停止，等待原作业安全返回')
   QTimer.singleShot(150,close_during_work);open_sync(win)
   wait(lambda:owner.client is None and not component.running)
   require(not component.running,'退出主软件安全结束真实 Node 进程')
   report={'passed':True,'checks':checks,'screenshots':screens,'scope':'real downloaded files replayed through UI; no new browser/platform operation; actual Node and EXE validator' if real else 'synthetic saved platform task; no real browser or platform; real Node and no-Qt validator; frozen flag distinguishes EXE/source','heartbeat_count':len(ticks),'retained_component_pid':pid,'component_exited':not component.running,'version':diagnostic(),'frozen':bool(getattr(sys,'frozen',False))}
  except Exception:
   report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen':bool(getattr(sys,'frozen',False))}
  finally:
   timer.stop();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
   if d:
    if d.worker:d.cancel();wait(lambda:d.worker is None)
    d.reject();d.deleteLater()
   win._discard_close=True;win.close()
   owner=getattr(getattr(win,'catalog_sessions',None),'session',None)
   if owner and owner.client is not None:wait(lambda:owner.client is None)
   app.exit(0 if report.get('passed') else 1)
 QTimer.singleShot(250,execute)
