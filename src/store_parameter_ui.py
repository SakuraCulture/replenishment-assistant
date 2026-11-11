"""Explicit preview and atomic persistence for store-specific Excel parameter patches."""
import copy
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QMessageBox
from ui_controls import label, button
from business_ui import grid, run
from import_ui import ImportDialog
import export_locations
import store_parameter_import as spi
import store_defaults
import work_repository as wr
from runtime_support import Cancelled

SCOPES = [('仅保存门店默认（以后自动带入）', 'defaults'),
          ('仅应用本次作业（变化门店需重算）', 'current'),
          ('应用本次并保存门店默认', 'both')]


def capture(host, data):
    codes = [r['code'] for r in data['rows']]
    saved = host.work_repository.stores()
    current, names = {}, {}
    for code in codes:
        if code in host.store_widgets:
            p = host.preset_for(code)
            p['config'] = copy.deepcopy(host.config_for(code))
            current[code] = spi.base(p, host.store_ui(code))
        names[code] = host.states.get(code, {}).get('name') or saved.get(code, {}).get('name', '')
    return {'saved': {c: saved[c] for c in codes if c in saved}, 'current': current, 'names': names}


def preview(data, snapshot, scope):
    bases = snapshot['current'] if scope != 'defaults' else snapshot['saved']
    result=spi.plan(data, bases, current=scope != 'defaults', names=snapshot['names'])
    if data.get('scoped') and scope=='both':
        saved=spi.plan(data,snapshot['saved'],names=snapshot['names'])
        result['current_changed']=result['changed'][:];result['saved_plan']=saved
        result['changed']=list(dict.fromkeys([*result['changed'],*saved['changed']]))
        result['errors']+=saved['errors']
        result['rows']=[[r[0],r[1],'本次 / '+r[2],*r[3:]] for r in result['rows']]+[[r[0],r[1],'默认 / '+r[2],*r[3:]] for r in saved['rows']]
    return result


def verify_sources(data, progress=lambda _: None, cancel=None):
    for path, checksum in data['sources'].items():
        spi.checkpoint(cancel)
        progress('核对导入文件未变化：' + path)
        if spi.digest(path) != checksum:
            raise ValueError('预览后文件发生变化，请重新导入：' + path)


def save_values(repository, values, expected, progress=lambda _: None, cancel=None):
    spi.checkpoint(cancel)
    progress('正在保存门店默认参数；整批提交…')
    repository.save_stores(values, expected=expected,action='批量导入门店参数')


def commit(host, data, snapshot, scope, prepared, persist=None):
    """Called on UI thread after a modal preview; validate all before any persistence."""
    if prepared['errors']:
        raise ValueError('仍有校验错误，未应用任何门店')
    if scope != 'defaults':
        if host.readonly or host.worker is not None:
            raise ValueError('历史只读或正在计算，不能应用本次参数')
        latest = capture(host, data)['current']
        if latest != snapshot['current']:
            raise wr.Conflict('本次门店设置在预览后变化，请重新导入核对')
    if preview(data, snapshot, scope) != prepared:
        raise ValueError('导入预览已失效，请重新核对')
    codes = prepared['changed']
    values = prepared['values']
    # Persist first, atomically with optimistic concurrency, before touching UI/results.
    saved = {}
    if scope != 'current':
        row_names = {r['code']: r['name'] for r in data['rows']}
        save_plan=prepared.get('saved_plan',prepared)
        for code in save_plan['changed']:
            v = save_plan['values'][code]
            saved[code] = store_defaults.record(v['preset'], v['preset']['config'], v['ui'],
                snapshot['saved'].get(code), include_budget=True, saved=wr.now())
            name = row_names.get(code) or snapshot['names'].get(code)
            if name:
                saved[code]['name'] = name
        expected = {c: snapshot['saved'].get(c) for c in save_plan['changed']}
        if persist:
            persist(saved, expected)
        else:
            host.work_repository.save_stores(saved, expected=expected,action='批量导入门店参数')
        host.saved_store_defaults = {**getattr(host, 'saved_store_defaults', {}), **saved}
    current_codes=prepared.get('current_changed',codes)
    if scope != 'defaults' and current_codes:
        hydrating, initializing = host._hydrating, host.initializing
        host._hydrating = host.initializing = True
        try:
            for code in current_codes:
                v = values[code]
                host.store_presets[code] = copy.deepcopy(v['preset'])
                host.overrides[code] = copy.deepcopy(v['preset']['config']['warehouses'])
                host.set_store_ui(code, v['ui'])
                if any(v['ui'].get(k) != snapshot['current'][code]['ui'].get(k) for k in ('mode', 'amount')):
                    host.store_widgets[code]['saved'] = False
        finally:
            host._hydrating, host.initializing = hydrating, initializing
        host.invalidate(current_codes)
        host.selection_changed()
    for code in codes:
        if code in host.store_widgets:
            host.refresh_parameter_label(code)
    return codes


class Preview(QDialog):
    def __init__(self, parent, host, data, snapshot, scope):
        super().__init__(parent)
        self.setWindowTitle('批量导入门店参数 · 核对后应用')
        self.resize(1120, 700)
        self.host, self.data, self.snapshot = host, data, snapshot
        v = QVBoxLayout(self)
        v.addWidget(label('；'.join(data.get('formats', ['旧模板：空白保持原值']))+'。0按实际值处理；全部校验通过后才可应用。', 'section'))
        self.scope = QComboBox()
        for text, key in SCOPES:
            self.scope.addItem(text, key)
        self.scope.setCurrentIndex(self.scope.findData(scope))
        v.addWidget(self.scope)
        self.note = label('', 'muted')
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        table, self.model = grid(self, ['门店编码', '门店名称', '参数', '原值', '新值', '来源'], [])
        search = QLineEdit()
        search.setPlaceholderText('搜索门店、参数或校验错误')
        search.textChanged.connect(self.model.filter)
        v.addWidget(search)
        v.addWidget(table, 1)
        for i, width in enumerate([145, 180, 270, 160, 160, 250]):
            table.setColumnWidth(i, width)
        self.error = label('', 'muted')
        self.error.setWordWrap(True)
        v.addWidget(self.error)
        row = QHBoxLayout()
        self.go = button('确认应用', self.accept, True)
        self.go.setAutoDefault(False)
        cancel = button('取消，不修改', self.reject)
        cancel.setDefault(True)
        row.addWidget(self.go)
        row.addWidget(cancel)
        v.addLayout(row)
        self.scope.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def refresh(self):
        scope = self.scope.currentData()
        self.prepared = preview(self.data, self.snapshot, scope)
        errors = list(self.prepared['errors'])
        if scope != 'defaults' and (self.host.readonly or self.host.worker is not None):
            errors.append('当前正在计算或为历史只读作业；请改为仅保存默认，或结束计算后再导入。')
        self.model.replace(self.prepared['rows'] + [['校验错误', '', message, '', '', ''] for message in errors])
        target = '保存的默认参数；未保存门店从标准方案开始' if scope == 'defaults' else '本次门店实际生效参数'
        resets=sum(len(r.get('reset_fields',[])) for r in self.data['rows'])
        self.note.setText(f'以{target}为原值。共{len(self.data["rows"])}店，{len(self.prepared["changed"])}店有变更/新增；空白默认/继承项 {resets} 项，请逐项核对。\n'
                          + ('本次作业及已有结果不改变。' if scope == 'defaults' else '仅变化门店的结果失效，重算后生效。') + '历史签发不变。\n'+'\n'.join(self.data.get('notices',[])))
        self.error.setText(f'{len(errors)}项错误，整批不能应用；完整错误在上表中，可搜索“校验错误”。' if errors else '校验通过；请核对生效范围和逐店差异。' if self.prepared['changed'] else '当前范围没有参数变化，无需重复应用；如要修改本次作业，请核对上方生效范围。')
        self.go.setText({'defaults': '确认保存门店默认', 'current': '确认应用本次作业', 'both': '确认应用并保存默认'}[scope])
        self.go.setEnabled(not errors and bool(self.prepared['changed']))


def import_parameters(parent, host, scope='defaults',kind='warehouse_cycles'):
    dialog = ImportDialog(parent, kind, title='导入门店设置（兼容旧模板）', multiple=True)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return
    try:
        data = run(parent, spi.read, (dialog.paths,), '读取并校验门店参数')
        if any(f[0]=='category_profiles' for row in data['rows'] for f in row['values']):
            raise ValueError('旧表含门店分类映射。本版映射全局共用，请删除这些导入列/工作表，并在“全局分类映射”单独核对维护；本次未改任何参数。')
        snapshot = capture(host, data)
        check = Preview(parent, host, data, snapshot, scope)
        if check.exec() != QDialog.DialogCode.Accepted:
            return
        run(parent, verify_sources, (data,), '确认参数文件未变化')
        codes = commit(host, data, snapshot, check.scope.currentData(), check.prepared,
            persist=lambda values, expected: run(parent, save_values,
                (host.work_repository, values, expected), '保存门店默认参数', cancellable=False))
        if hasattr(parent, 'refresh'):
            parent.refresh()
        scope=check.scope.currentData();prepared=check.prepared
        current_count=len(prepared.get('current_changed',prepared['changed'])) if scope!='defaults' else 0
        saved_count=len(prepared.get('saved_plan',prepared)['changed']) if scope!='current' else 0
        QMessageBox.information(parent, '批量参数已应用', f'已处理{len(codes)}家门店：本次作业变化{current_count}家，保存默认{saved_count}家。\n'
            +('请重新计算变化门店后查看结果。\n' if current_count else '本次计算结果不改变。\n')+check.note.text())
    except Cancelled:
        return
    except Exception as exc:
        QMessageBox.warning(parent, '门店参数未应用', str(exc))


def export_parameters(parent, host, codes, *, current=False,kind='classified_parameters'):
    if not codes:
        QMessageBox.information(parent, '请选择门店', '请先勾选要导出的门店。')
        return
    try:
        saved = host.work_repository.stores()
        records = {}
        for code in codes:
            if current:
                p = host.preset_for(code)
                p['config'] = host.config_for(code)
                value = spi.base(p, host.store_ui(code))
            else:
                value = copy.deepcopy(saved.get(code) or spi.base())
            value['name'] = host.states.get(code, {}).get('name') or value.get('name', '')
            records[code] = value
        name = '本次门店参数.xlsx' if current else '门店默认参数.xlsx'
        path, _ = export_locations.save_file(parent, '导出可修改回导的门店参数', name, 'Excel (*.xlsx)')
        if path:
            if not path.lower().endswith('.xlsx'):
                path += '.xlsx'
            run(parent, export_scoped, (path, kind, records), '导出门店参数')
            QMessageBox.information(parent, '门店参数已导出', path + '\n可修改数值后使用“批量导入门店参数”回导；没有保存默认的门店导出标准参考值。')
    except Cancelled:
        return
    except Exception as exc:
        QMessageBox.warning(parent, '门店参数未导出', str(exc))


def export_scoped(path,kind,records,progress=lambda _:None,cancel=None):
    from parameter_tables import write
    return write(path,kind,records,progress,cancel,scoped=True)
