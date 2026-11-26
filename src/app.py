"""Application entrypoint: Qt setup, logging, exceptions and window creation."""
import sys,os,logging
from pathlib import Path
import app_smoke_dispatch as smokes

def _main():
    from PySide6.QtCore import QStandardPaths
    from PySide6.QtGui import QColor,QIcon,QPalette
    from PySide6.QtWidgets import QApplication,QMessageBox
    from ui_controls import STYLE
    app=QApplication(sys.argv);app.setApplicationName('补货助手');app.setOrganizationName('YoutuanTools');app.setStyle('Fusion')
    palette=app.style().standardPalette()
    for role,color in [(QPalette.ColorRole.Window,'#F3F6F7'),(QPalette.ColorRole.WindowText,'#22383C'),(QPalette.ColorRole.Base,'#FFFFFF'),(QPalette.ColorRole.AlternateBase,'#F7FAFA'),(QPalette.ColorRole.Text,'#22383C'),(QPalette.ColorRole.Button,'#FFFFFF'),(QPalette.ColorRole.ButtonText,'#22383C'),(QPalette.ColorRole.Highlight,'#DDEFEA'),(QPalette.ColorRole.HighlightedText,'#153C43')]:palette.setColor(role,QColor(color))
    app.setPalette(palette);app.setStyleSheet(STYLE)
    icon=Path(__file__).parent/'app.ico'
    if icon.exists():app.setWindowIcon(QIcon(str(icon)))
    from application_runtime import Runtime,settings_root
    from application_recovery import Session,TICKET_ENV
    import data_location
    default_data=data_location.default_base()
    overridden=bool(os.environ.get('REPLENISHMENT_DATA_DIR'))
    supervised=bool(os.environ.get(TICKET_ENV))
    logs=data_location.hint(default_data)
    runtime=Runtime(logs,folder=None if overridden else data_location.control()/'diagnostics');runtime.bind_qt(app)
    if smokes.install_startup(app,sys.argv):
        # Refuse before constructing repositories, not only after the UI opens.
        if logs.absolute()!=data_location.default_base()/'settings' and not data_location.config():
            raise ValueError('Synthetic location acceptance root mismatch')
    app.aboutToQuit.connect(lambda:runtime.transition('EVENT_LOOP_EXIT'))
    from single_instance import SingleInstance,scope
    primary=SingleInstance(scope(sys.argv,logs),parent=app,data_root=logs)
    runtime.primary=primary
    try:
        if not primary.acquire():
            runtime.emit('EXISTING_WINDOW_ACTIVATED')
            return 0
    except Exception as exc:
        if supervised:
            runtime.exception(*sys.exc_info(),event='PROCESS_RESTART_BLOCKED');return 1
        from data_location_progress import show_pending
        code=show_pending(app)
        if code is not None:return code
        QMessageBox.warning(None,'补货助手已在运行',str(exc));return 1
    from application_reset import recover,InstanceLease
    try:
        if not overridden:
            from data_relocation import recover as recover_location
            recover_location()
        logs=settings_root(default_data)
        runtime.route(logs.parent/('.'+logs.name+'.diagnostics'),root=logs)
        try:runtime.process_recovery=Session.prepare(logs,sys.argv)
        except OSError:
            if supervised:raise
            runtime.exception(*sys.exc_info(),event='PROCESS_RECOVERY_SETUP_FAILED')
        recover(logs)
        from order_migration import recover as recover_orders
        recover_orders(logs)
        if (logs/'迁入原文件映射.json').is_file():os.environ['REPLENISHMENT_PORTABLE_PATHS']=str(logs/'迁入原文件映射.json')
        else:os.environ.pop('REPLENISHMENT_PORTABLE_PATHS',None)
        instance_lease=InstanceLease(logs)
        runtime.lease=instance_lease
    except Exception as exc:
        if supervised:
            runtime.exception(*sys.exc_info(),event='PROCESS_RESTART_BLOCKED');return 1
        QMessageBox.warning(None,'资料目录未打开',str(exc));return 1
    app.data_root=logs
    logs.mkdir(parents=True,exist_ok=True);logging.basicConfig(filename=runtime.folder/'app.log',level=logging.INFO,encoding='utf-8',force=True,format='%(asctime)s %(levelname)s %(message)s')
    from performance_log import install as install_performance
    install_performance(runtime,app)
    from workbench import Workbench
    if smokes.use_workbench(sys.argv):win=Workbench()
    else:
        from application_window import ApplicationWindow
        win=ApplicationWindow()
    runtime.window=win
    win.show()
    primary.bind(win)
    recovery=getattr(runtime,'process_recovery',None)
    if recovery:
        try:recovery.start()
        except OSError:runtime.exception(*sys.exc_info(),event='PROCESS_RECOVERY_START_FAILED')
        if recovery.restarted:
            runtime.emit('PROCESS_RESTARTED',attempt=recovery.owner['attempt'])
            win.status.setText('软件已在异常退出后重新启动。原定时任务可在运行记录查看。')
    # GUI acceptance installs isolated transports before explicitly exercising
    # startup; normal launches always attempt connection after the first paint.
    from browser_startup_ui import start_application_connections
    start_application_connections(win, sys.argv)
    from application_exit import notice
    from PySide6.QtCore import QTimer
    QTimer.singleShot(0,win,lambda:notice(win,logs))
    smokes.start_window_smokes(app,win,sys.argv)
    runtime.workbench_ready=True
    runtime.emit('WORKBENCH_READY')
    code=app.exec()
    if code==0:runtime.expect_exit()
    from application_exit import finish
    return finish(runtime,code)


def main():
    from application_runtime import current
    from application_recovery import TICKET_ENV
    interactive_launch = len(sys.argv) == 1 and not os.environ.get(TICKET_ENV)
    startup_error = None
    code=1;runtime=None
    try:code=_main()
    except BaseException as exc:
        runtime=current()
        if runtime:
            runtime.exception(*sys.exc_info())
            if runtime.request:
                from migration_status import record,latest
                try:
                    prior=latest(runtime.root)
                    if not prior or prior['nonce']!=runtime.request['nonce'] or prior['state']!='FAILED':record(runtime.request,'FAILED',phase=runtime.phase,error=exc)
                except BaseException:runtime.exception(*sys.exc_info(),event='HANDOFF_LOG_EXCEPTION')
            if getattr(runtime,'storage_request',None):
                try:
                    import data_relocation
                    prior=data_relocation.status()
                    if prior and prior['state'] not in ('FAILED','COMPLETED'):
                        data_relocation.record(runtime.storage_request,'FAILED','WAITING_EXIT','安全退出或搬迁未完成，原资料与已有副本保留',error={'message':str(exc),'traceback':__import__('traceback').format_exc()})
                    runtime.location_result=data_relocation.status()
                except BaseException:runtime.exception(*sys.exc_info(),event='STORAGE_HANDOFF_LOG_EXCEPTION')
        if interactive_launch and not getattr(runtime, 'workbench_ready', False):
            from startup_failure import save_failure
            startup_error = (exc, save_failure(exc))
        elif runtime is None:sys.__excepthook__(*sys.exc_info())
        code=1
    finally:
        runtime=current()
        try:
            if runtime:
                from application_exit import release
                release(runtime)
        finally:
            if startup_error is not None:
                from startup_failure import show_failure
                show_failure(*startup_error)
    if runtime and getattr(runtime,'location_result',None):
        from data_location_ui import reopen
        reopen(runtime)
    return code


def __getattr__(name):
    # Historical external source callers only; active views never import app.
    if name=="MainWindow":
        from legacy_window import MainWindow
        return MainWindow
    raise AttributeError(name)

if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    from application_recovery import WATCH_FLAG
    if len(sys.argv)>1 and sys.argv[1]==WATCH_FLAG:
        from application_recovery import watch
        sys.exit(watch(sys.argv[2:]))
    if '--data-location-progress' in sys.argv:
        from data_location_progress import main as progress_main
        sys.exit(progress_main(sys.argv[sys.argv.index('--data-location-progress')+1:]))
    if '--order-migration-progress' in sys.argv:
        from order_migration_progress import main as migration_progress_main
        sys.exit(migration_progress_main(sys.argv[sys.argv.index('--order-migration-progress')+1:]))
    smokes.run_catalog_helper(sys.argv)
    if '--catalog-validate' in sys.argv:
        from catalog_sync_validation import cli
        sys.exit(cli(sys.argv[sys.argv.index('--catalog-validate')+1:]))
    if '--storage-copy' in sys.argv:
        from storage_maintenance import cli
        sys.exit(cli(sys.argv[sys.argv.index('--storage-copy')+1:]))
    sys.exit(main())
