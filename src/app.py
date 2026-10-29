"""Application entrypoint: Qt setup, logging, exceptions and window creation."""
import sys,os,logging
from pathlib import Path
from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QColor,QIcon,QPalette
from PySide6.QtWidgets import QApplication,QMessageBox
from ui_controls import STYLE

def main():
    app=QApplication(sys.argv);app.setApplicationName('补货助手');app.setOrganizationName('YoutuanTools');app.setStyle('Fusion')
    palette=app.style().standardPalette()
    for role,color in [(QPalette.ColorRole.Window,'#F3F6F7'),(QPalette.ColorRole.WindowText,'#22383C'),(QPalette.ColorRole.Base,'#FFFFFF'),(QPalette.ColorRole.AlternateBase,'#F7FAFA'),(QPalette.ColorRole.Text,'#22383C'),(QPalette.ColorRole.Button,'#FFFFFF'),(QPalette.ColorRole.ButtonText,'#22383C'),(QPalette.ColorRole.Highlight,'#DDEFEA'),(QPalette.ColorRole.HighlightedText,'#153C43')]:palette.setColor(role,QColor(color))
    app.setPalette(palette);app.setStyleSheet(STYLE)
    icon=Path(__file__).parent/'app.ico'
    if icon.exists():app.setWindowIcon(QIcon(str(icon)))
    logs=Path(os.environ.get('REPLENISHMENT_DATA_DIR') or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation));logs.mkdir(parents=True,exist_ok=True);logging.basicConfig(filename=logs/'app.log',level=logging.INFO,encoding='utf-8')
    def unexpected(t,v,tb):
        logging.error('界面异常',exc_info=(t,v,tb));QMessageBox.critical(None,'操作未完成',f'{v}\n日志位置：{logs}')
    sys.excepthook=unexpected
    from workbench import Workbench
    win=Workbench();win.show()
    if '--batch-smoke' in sys.argv:
        from batch_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--batch-smoke')+1:])
    if '--warehouse-smoke' in sys.argv:
        from warehouse_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--warehouse-smoke')+1:])
    if '--work-smoke' in sys.argv:
        from work_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--work-smoke')+1:])
    if '--intake-smoke' in sys.argv:
        from intake_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--intake-smoke')+1:])
    return app.exec()


def __getattr__(name):
    # Historical external source callers only; active views never import app.
    if name=="MainWindow":
        from legacy_window import MainWindow
        return MainWindow
    raise AttributeError(name)

if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
