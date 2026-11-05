"""Minimal pre/post reproduction; --source can point at a fixed Git archive."""
import argparse,json,sys,sqlite3,tempfile,time,os
from pathlib import Path
from types import SimpleNamespace
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);a=p.parse_args()
sys.path.insert(0,str(a.source.resolve()/'src'));os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import QTimer
from shiboken6 import isValid
import readable_ui,scheduled_ui,scheduled_replenishment as sr
app=QApplication([]);errors=[];sys.excepthook=lambda t,v,tb:errors.append(str(v))
box=QMessageBox();box.setText('签发');readable_ui.review_table(box,['商品','数量'],[['A',1]])
layout=box.layout();box.setInformativeText('与签发导出一致：插入表格后再设置说明')
cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);box.setDefaultButton(cancel)
QTimer.singleShot(60,cancel.click);box.exec()
report={'review':{'errors':errors,'old_layout_valid':isValid(layout),'new_layout_is_old':box.layout() is layout,'table_valid':isValid(box.business_table)}}
with tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'work.sqlite3';c=sqlite3.connect(path);c.execute('CREATE TABLE marker(n)');c.commit();c.execute('BEGIN EXCLUSIVE')
    host=SimpleNamespace(_large_io_dialog=SimpleNamespace(_completed=False),aoxiang_workflows=SimpleNamespace(maintenance_depth=1))
    m=SimpleNamespace(reader=None,closing=False,repo=sr.Repository(path),host=host,status=SimpleNamespace(setText=lambda _:None))
    started=time.monotonic()
    try:scheduled_ui.Manager.poll(m);error=None
    except Exception as exc:error=f'{type(exc).__name__}: {exc}'
    report['poll']={'error':error,'seconds':time.monotonic()-started};c.rollback();c.close()
print(json.dumps(report,ensure_ascii=False,indent=2))
