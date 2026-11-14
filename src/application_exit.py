"""Post-event-loop handoff: no widget construction after shutdown begins."""
import logging,sys,traceback,os

def drain_gui_objects(runtime):
    collector=getattr(runtime,'gui_gc',None)
    if collector is not None:collector.drain()

def running_threads(runtime):
    from PySide6.QtCore import QThread
    from shiboken6 import isValid
    result=[]
    for parent in (getattr(runtime,'window',None),getattr(runtime,'application',None)):
        if parent is not None and isValid(parent):
            for thread in parent.findChildren(QThread):
                if isValid(thread) and thread.isRunning() and thread not in result:result.append(thread)
    return result

def retained_sessions(runtime):
    from shiboken6 import isValid
    win=getattr(runtime,'window',None)
    if win is None or not isValid(win):return []
    result=[]
    for name in ('catalog_sessions','aoxiang_workflows'):
        owner=getattr(win,name,None);session=getattr(owner,'session',None)
        if session is not None and getattr(session,'client',None) is not None:result.append(session)
    return result

def finish(runtime,code):
    import shiboken6
    win=runtime.window;plan=getattr(win,'_order_import_request',None);runtime.request=plan
    reset=getattr(win,'_factory_reset_request',None)
    storage=getattr(win,'_data_location_request',None);runtime.storage_request=storage
    runtime.storage_restart=bool(getattr(win,'_data_location_restart',False))
    if storage:
        from data_location import control
        runtime.route(control()/'diagnostics')
    runtime.transition('UI_TEARDOWN')
    if plan:
        from migration_status import record
        record(plan,'WAITING_EXIT',phase='UI_TEARDOWN')
    runtime.primary.quiesce()
    if running_threads(runtime) or retained_sessions(runtime):raise RuntimeError('事件循环提前退出，但后台线程或连接尚未结束；不执行迁入，等待原任务安全收尾')
    if (plan or reset or storage) and runtime.shutdown_errors:raise RuntimeError('安全退出阶段已发生异常，资料操作未执行；原始错误：'+str(runtime.first_shutdown_exception)) from runtime.first_shutdown_exception
    before=runtime.shutdown_errors
    drain_gui_objects(runtime)
    if shiboken6.isValid(win):shiboken6.delete(win)
    runtime.window=None
    if runtime.shutdown_errors!=before:raise RuntimeError('退出清理回调发生异常，迁入未执行；原始错误：'+str(runtime.first_shutdown_exception)) from runtime.first_shutdown_exception
    runtime.lease.close();logging.shutdown()
    if storage:
        # The native file chooser / other application-owned objects may retain
        # directory handles after the main window is gone. Destroy Qt before
        # moving data, but retain the exact existing inter-process lock.
        primary=runtime.primary;runtime.primary=primary.take_guard()
        shiboken6.delete(primary)
        app=runtime.application
        drain_gui_objects(runtime)
        if app is not None and shiboken6.isValid(app):shiboken6.delete(app)
        runtime.application=None
        if runtime.shutdown_errors!=before:raise RuntimeError('Qt退出回调发生异常，搬迁未执行；原始错误：'+str(runtime.first_shutdown_exception)) from runtime.first_shutdown_exception
        runtime.emit('STORAGE_QT_RELEASED',instance_guard_retained=True)
    if plan:
        runtime.transition('APPLYING')
        from order_migration import apply
        result=apply(plan);runtime.emit('MIGRATION_COMPLETED',result=result)
    if reset:
        runtime.transition('APPLYING')
        from application_reset import perform
        result=perform(reset);runtime.emit('RESET_COMPLETED',result=result)
    if storage:
        runtime.transition('APPLYING')
        from data_relocation import apply
        runtime.location_result=apply(storage);runtime.emit('DATA_LOCATION_COMPLETED',result=runtime.location_result)
    runtime.transition('COMPLETED');return code

def release(runtime):
    """Drain Qt deletion with the non-UI hook installed, then restore hooks."""
    import shiboken6
    runtime.transition('FINALIZING')
    if running_threads(runtime) or retained_sessions(runtime):
        # Do not kill commits or destroy their QThread parents. Keep all guards
        # until cooperative completion; this loop has no platform/GUI actions.
        from PySide6.QtCore import QTimer
        app=runtime.application;app.setQuitOnLastWindowClosed(False);timer=QTimer(app)
        def drain():
            if running_threads(runtime):return
            try:
                for session in retained_sessions(runtime):
                    if not session.close(timeout=.1):return
            except BaseException:
                if not getattr(runtime,'drain_error_recorded',False):runtime.exception(*sys.exc_info(),event='DRAIN_EXCEPTION');runtime.drain_error_recorded=True
                return
            app.quit()
        timer.setInterval(50);timer.timeout.connect(drain)
        win=getattr(runtime,'window',None)
        if win is not None and shiboken6.isValid(win):win._order_import_request=None;win._data_location_request=None
        runtime.transition('WAITING_WORKERS');timer.start();app.exec();timer.stop();timer.deleteLater()
        runtime.transition('FINALIZING')
    drain_gui_objects(runtime)
    for name in ('window','lease','primary'):
        item=getattr(runtime,name,None)
        if item is None:continue
        try:
            if name=='window':
                if shiboken6.isValid(item):shiboken6.delete(item)
            else:item.close()
        except BaseException:runtime.exception(*sys.exc_info(),event='RELEASE_EXCEPTION')
        setattr(runtime,name,None)
    logging.shutdown()
    app=getattr(runtime,'application',None)
    drain_gui_objects(runtime)
    if app is not None and shiboken6.isValid(app):
        try:shiboken6.delete(app)
        except BaseException:runtime.exception(*sys.exc_info(),event='QT_FINALIZE_EXCEPTION')
    runtime.restore()

def notice(host,root):
    """Only the new, running GUI reports the previous handoff outcome."""
    from application_runtime import ui_allowed
    from migration_status import latest,folder
    from PySide6.QtWidgets import QMessageBox
    if not ui_allowed():return
    from data_relocation import status
    moved=status() if not os.environ.get('REPLENISHMENT_DATA_DIR') else None
    if moved and not moved.get('acknowledged') and moved['state'] in ('COMPLETED','FAILED'):
        from data_location import journal_file
        from stockout_common import atomic_json
        if moved['state']=='COMPLETED':
            message='数据已保存在新位置：'+moved['target']+'\n恢复副本：'+moved['archive']
            if moved['phase']=='CLEANUP_PENDING':message+='\n旧位置副本尚待归档或清理，请在“显示与帮助 → 数据保存位置”处理。'
        else:message='上次数据搬迁未完成，原资料或恢复副本保留；不会自动重放。\n'+moved.get('error',{}).get('message',moved['message'])
        QMessageBox.information(host,'数据保存位置结果',message)
        moved['acknowledged']=True;atomic_json(journal_file(),moved)
    value=latest(root)
    if value is None:return
    from stockout_common import atomic_json
    if value.get('acknowledged'):return
    state=value['state'];title='上次订单迁入结果'
    detail=f"状态：{state} / {value['phase']}\n诊断：{folder(root)}\n"
    if state=='COMPLETED':
        result=value.get('result',{})
        if result.get('mode')=='merge':
            counts=result['counts'];detail+='订单合并已完成：新增 {added} 单，完全重复跳过 {duplicate} 单，冲突保留本机 {conflict} 单，本机已删除不恢复 {deleted} 单。'.format(**counts)+'\n本机原数据保留，完整备份：'+result['backup']+'\n完整结果：'+result['report']+'\n迁入任务未自动执行；请先核验连接和原平台结果。'
        else:detail+='订单迁入已完成；原库完整备份保留。请先核对连接和原平台结果，再逐项继续。'
    elif state=='CANCELLED':return
    else:detail+='未确认迁入完成。原库、迁入材料及已有备份保留；不会自动重放。请查看原始错误，处理后重新校验原迁移包。\n'+value.get('error',{}).get('message','上次在退出或替换完成前中断，请保留现场。')
    QMessageBox.information(host,title,detail)
    value['acknowledged']=True;atomic_json(folder(root)/(value['nonce']+'.status.json'),value)
