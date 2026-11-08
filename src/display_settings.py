"""Local reading preferences, independent of business parameters and snapshots."""
import json
import re
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QActionGroup
from ui_controls import STYLE


def apply(settings):
    app=QApplication.instance();size=settings.get('font',10)
    if size not in (10,11,12):size=10
    density=settings.get('density','standard')
    if density not in ('compact','standard','roomy'):density='standard'
    app.setProperty('reading_density',density)
    app.setStyleSheet(re.sub(r'font-size:(\d+)pt',lambda m:f'font-size:{int(m[1])*size/10:g}pt',STYLE))


def save():
    app=QApplication.instance()
    if not getattr(app,'reading_path',None):return
    from priority_list import atomic_json
    atomic_json(app.reading_path,app.reading_settings)


def install(host):
    app=QApplication.instance();app.reading_path=Path(host.repository.folder)/'display_ui.json'
    try:settings=json.loads(app.reading_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):settings={}
    if not isinstance(settings,dict):settings={}
    app.reading_settings=settings;apply(settings)
    menu=host.menuBar().addMenu('显示与帮助')
    def change(key,value):
        settings[key]=value;apply(settings)
        try:save()
        except OSError as exc:host.status.setText('显示已调整，但本机偏好未保存：'+str(exc))
    fonts=menu.addMenu('正文字号');group=QActionGroup(host);group.setExclusive(True)
    for size in (10,11,12):
        action=fonts.addAction(f'{size} 磅'+('（默认）' if size==10 else ''));action.setCheckable(True);action.setChecked(settings.get('font',10)==size);group.addAction(action)
        action.triggered.connect(lambda checked=False,value=size:change('font',value))
    rows=menu.addMenu('表格行距');density=QActionGroup(host);density.setExclusive(True)
    for key,title in (('compact','紧凑'),('standard','标准'),('roomy','宽松')):
        action=rows.addAction(title);action.setCheckable(True);action.setChecked(settings.get('density','standard')==key);density.addAction(action)
        action.triggered.connect(lambda checked=False,value=key:change('density',value))
    def reset():
        settings.clear();app.setProperty('reading_layout_generation',(app.property('reading_layout_generation') or 0)+1);apply(settings)
        for a in group.actions():a.setChecked(a.text().startswith('10 '))
        for a in density.actions():a.setChecked(a.text()=='标准')
        try:save()
        except OSError as exc:host.status.setText('显示已恢复，但偏好未保存：'+str(exc))
    menu.addAction('恢复默认显示',reset)
    def help_page():
        from user_help import show
        show(host)
    def recovery():
        from export_recovery_ui import show
        show(host,host.work_repository.path)
    menu.addSeparator();menu.addAction('图文使用手册',help_page)
    menu.addAction('导出任务与恢复',recovery)
    from table_readability import fit_window
    fit_window(host,1380,900)
