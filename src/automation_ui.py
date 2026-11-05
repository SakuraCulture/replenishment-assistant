"""Business-facing local AI connection and task details, no second workbench."""
import json
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QMessageBox, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QComboBox
from ui_controls import label, button
from automation_protocol import CommandError

STATES = {'preparing': '检查中', 'prepared': '检查完成', 'running': '计算中', 'stopping': '正在停止后续',
          'stopped': '已停止', 'completed': '已完成', 'partial': '部分完成', 'failed': '失败',
          'needs_input': '等待补资料或配置', 'needs_review': '待核对采用', 'interrupted': '原工作台已退出'}


def install(host):
    from automation_gateway import Gateway
    from automation_playbook_ui import show_playbooks
    host.automation = Gateway(host)
    row = QHBoxLayout(); note = label('AI 协助未连接', 'muted'); note.setWordWrap(False)
    row.addWidget(button('AI 协助', lambda: settings(host)))
    row.addWidget(button('AI 常用业务', lambda: show_playbooks(host))); row.addWidget(note, 1)
    details = button('查看进度', lambda: tasks(host)); stop = button('■ 停止后续', lambda: stop_current(host))
    take = button('接手处理', lambda: takeover(host)); row.addWidget(details); row.addWidget(stop); row.addWidget(take)
    host.centralWidget().layout().insertLayout(1, row)
    def refresh():
        g = host.automation
        if g.active:
            p = g.task_record(g.active); note.setText('AI 协助 · ' + p['message'])
        else: note.setText('AI 协助已连接 · 查询与计算' if g.enabled else 'AI 协助未连接')
        details.setVisible(g.enabled); stop.setVisible(bool(g.active)); take.setVisible(bool(g.active))
    host.automation.changed.connect(refresh); refresh()


def stop_current(host):
    g = host.automation
    if g.active: g.stop_remaining({'task_id': g.active})


def takeover(host):
    g=host.automation
    if g.active:
        task=g.task_record(g.active)
        try:g.journal.update(g.active,human_takeover=True)
        except Exception as exc:g.fail_callback(task,exc)
    stop_current(host)
    host.status.setText('已停止启动后续步骤；当前门店安全结束后可人工编辑，已完成结果仍可查看。')


def bridge_command():
    if getattr(sys, 'frozen', False):
        path = Path(sys.executable).parent / 'AI接口/补货助手AI接口.exe'
        if not path.is_file(): raise ValueError('交付包缺少 AI 接口程序，请使用完整交付目录')
        return str(path), []
    return sys.executable, [str(Path(__file__).with_name('automation_bridge.py'))]


def connection_configs(host):
    command, args = bridge_command(); args += ['--connection', str(host.automation.connection_file)]
    entry = {'command': command, 'args': args}
    toml = '[mcp_servers.replenishment]\ncommand = ' + json.dumps(command, ensure_ascii=False) + '\nargs = ' + json.dumps(args, ensure_ascii=False) + '\nstartup_timeout_sec = 20\ntool_timeout_sec = 40\n'
    return {'workbuddy': {'mcpServers': {'replenishment': entry}}, 'codex': toml}


def cli_guide(host):
    command, args = bridge_command()
    def quote(value): return "'" + str(value).replace("'", "''") + "'"
    executable = '& ' + ' '.join(quote(s) for s in [command, *args])
    connected = executable + ' --connection ' + quote(host.automation.connection_file)
    return '\n'.join([
        '补货助手 CLI · 在 PowerShell 执行；与 MCP 共用工作台连接和权限。',
        '先打开本交付包的补货助手，在 AI 协助启用本机连接，并关闭业务弹窗。',
        '版本（不需要连接）：\n' + executable + ' version',
        '连接诊断（只查询，不自动启用）：\n' + connected + ' doctor',
        '开放工具与参数：\n' + executable + ' tools',
        '参数业务字典（只查询）：\n' + connected + ' call parameter_definitions',
        '当前工作台：\n' + connected + ' call workbench_context',
        '复杂参数保存为 UTF-8 JSON 后调用，例如：\n' + connected + " call effective_parameters --arguments-file '.\\参数核对.json'",
        '参数核对.json 内容示例：{"stores":["准确门店编码"],"scope":"current"}；defaults 为已保存默认，不代表本次已采用。',
        '先核对 context_token 和门店编码再试算；新请求使用新 operation_key，状态不明查询原任务。',
        '定时任务只读：find_resources 的 schedules / scheduled_runs，以及 scheduled_run_status。',
        '不读取数据库绕过接口；不开放签发、采购、删除、永久设置或平台取数。',
        '退出码 0=成功（任务已受理仍须查询完成），2=输入/连接/业务拒绝，3=接口未完成；输出 UTF-8 JSON。',
        '更换程序目录或资料环境后，重新生成这份说明。不要发送 connection.json 的连接密钥。'])


def settings(host):
    g = host.automation; d = QDialog(host); d.setWindowTitle('AI 协助 · Codex / WorkBuddy'); d.resize(700, 350)
    v = QVBoxLayout(d); v.addWidget(label('使用当前工作台，查询与计算；正式保存、签发和平台采购仍在原页面完成。'))
    v.addWidget(label('输入原表保留在本机。工具返回的必要摘要可能进入你选择的 AI 服务；浏览器登录态和连接凭据不提供给模型。', 'muted'))
    status = label(''); v.addWidget(status)
    on = button('启用本机连接', lambda: change(True), True); off = button('关闭本机连接', lambda: change(False))
    row = QHBoxLayout(); row.addWidget(on); row.addWidget(off); v.addLayout(row)
    def refresh():
        status.setText('已启用，可在 Codex / WorkBuddy 连接。关闭客户端不会自动取消工作台已受理任务。' if g.enabled else '尚未启用；只允许连接这个资料环境对应的工作台。')
        on.setEnabled(not g.enabled); off.setEnabled(g.enabled)
    def change(enabled):
        try: g.enable() if enabled else g.disable()
        except Exception as exc: QMessageBox.warning(d, '连接未完成', str(exc))
        refresh()
    def export():
        path = QFileDialog.getExistingDirectory(d, '选择客户端配置说明保存位置')
        if not path: return
        try:
            data = connection_configs(host); folder = Path(path)
            targets = {'Codex_补货助手_MCP.toml': data['codex'], 'WorkBuddy_补货助手_MCP.json': json.dumps(data['workbuddy'], ensure_ascii=False, indent=2),
                       '补货助手_CLI使用说明.txt': cli_guide(host)}
            if any((folder / n).exists() for n in targets): raise ValueError('该目录已有同名配置，请选择新目录，避免覆盖已有配置')
            for name, body in targets.items(): (folder / name).write_text(body, encoding='utf-8')
            QMessageBox.information(d, '配置已生成', '已生成客户端配置片段和 CLI 使用说明。请在客户端添加，不要覆盖整份原配置；启用连接后关闭此弹窗再使用。')
        except Exception as exc: QMessageBox.warning(d, '未导出配置', str(exc))
    v.addWidget(button('生成连接配置与 CLI 使用说明', export))
    v.addWidget(button('查看本机 AI 任务', lambda: tasks(host)))
    v.addWidget(button('× 关闭', d.reject)); refresh(); d.exec()


def tasks(host):
    g = host.automation
    if not g.journal: QMessageBox.information(host, 'AI 协助', '请先启用本机连接。'); return
    d = QDialog(host); d.setWindowTitle('AI 协助任务 · 只记录操作进展'); d.resize(950, 650)
    v = QVBoxLayout(d); pick = QComboBox(); v.addWidget(pick)
    message = label(''); v.addWidget(message); progress = QProgressBar(); v.addWidget(progress)
    table = QTableWidget(0, 4); table.setHorizontalHeaderLabels(['门店编码', '门店名称', '状态', '处理说明'])
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); v.addWidget(table, 1)
    row = QHBoxLayout(); row.addWidget(button('查看此任务结果', lambda: results(host,pick.currentData())))
    row.addWidget(button('■ 停止该任务后续', lambda: stop_selected())); row.addWidget(button('× 关闭', d.reject)); v.addLayout(row)
    def stop_selected():
        if pick.currentData(): g.stop_remaining({'task_id': pick.currentData()}); refresh()
    def refresh():
        current = pick.currentData()
        try:records = [g.task_failures.get(r['task_id'], r) for r in g.journal.list(0, 100)]
        except Exception as exc:
            from automation_gateway import database_busy
            if not database_busy(exc):raise
            message.setText('任务记录正在被其他操作使用，稍后自动刷新；不要重复提交。');return
        pick.blockSignals(True); pick.clear()
        for p in records: pick.addItem(p['created'][:19].replace('T', ' ') + ' · ' + STATES.get(p['state'], p['state']), p['task_id'])
        index = pick.findData(current or g.active); pick.setCurrentIndex(max(0, index)); pick.blockSignals(False)
        if not pick.currentData(): message.setText('暂无任务'); return
        p = g.task_record(pick.currentData()); message.setText(p['message'])
        count = p.get('progress', {}); progress.setRange(0, count.get('total', 1)); progress.setValue(count.get('completed', 0))
        rows = p.get('stores') or p.get('preview', {}).get('stores', []); table.setRowCount(len(rows))
        issues = {x['store']: x['message'] for x in p.get('preview', {}).get('issues', [])}
        for i, r in enumerate(rows):
            for j, val in enumerate([r['code'], r.get('name', ''), r.get('state', STATES.get(p['state'], p['state'])), r.get('error') or issues.get(r['code'], '')]): table.setItem(i, j, QTableWidgetItem(str(val)))
    pick.currentIndexChanged.connect(lambda: refresh())
    timer = QTimer(d); timer.setInterval(1000); timer.timeout.connect(refresh); timer.start(); refresh(); d.exec(); timer.stop()


def results(host, task_id):
    """Read the same result reference the external client sees; never silently adopt."""
    g=host.automation
    try:r=g.require_result(task_id)
    except CommandError as exc:QMessageBox.information(host,'暂无可查看结果',str(exc));return
    d=QDialog(host);d.setWindowTitle('AI 任务结果 · 核对后采用');d.resize(1050,640)
    v=QVBoxLayout(d);pick=QComboBox()
    for s in r['codes']:pick.addItem(r['states'][s]['name']+'（'+s+'）',s)
    v.addWidget(pick);note=label('');v.addWidget(note)
    table=QTableWidget(0,7);table.setHorizontalHeaderLabels(['商品编码','商品名称','归属仓','执行数量','执行金额','月销量','说明'])
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents);v.addWidget(table,1)
    offset=[0];next_offset=[None]
    row=QHBoxLayout();previous=button('◀ 上一页',lambda:page(-1));following=button('▶ 下一页',lambda:page(1));row.addWidget(previous);row.addWidget(following);row.addStretch();v.addLayout(row)
    adopt=button('采用到本次作业',lambda:apply(),True);v.addWidget(adopt);v.addWidget(button('× 关闭',d.reject))
    def refresh():
        value=g.calculation_results({'task_id':task_id,'store':pick.currentData(),'offset':offset[0],'limit':50});next_offset[0]=value['next_offset']
        state=value['stores'][0];summary=state.get('summary') or {}
        note.setText(f"{state['state']} · 执行金额 {summary.get('amount','待核')} 元 · 当前 {offset[0]+1 if value['total'] else 0}—{offset[0]+len(value['items'])} / {value['total']} 行；尚未保存或签发。")
        table.setRowCount(len(value['items']))
        for i,x in enumerate(value['items']):
            for j,key in enumerate(('sku','name','warehouse','quantity','amount','monthly_sales','reason')):
                table.setItem(i,j,QTableWidgetItem('未提供' if x.get(key) is None else str(x[key])))
        previous.setEnabled(offset[0]>0);following.setEnabled(next_offset[0] is not None);adopt.setEnabled(not host.worker and bool(r['results']) and not host.readonly)
    def page(direction):
        offset[0]=max(0,offset[0]-50) if direction<0 else next_offset[0];refresh()
    def changed():offset[0]=0;refresh()
    def apply():
        try:
            value=g.adopt_trial({'task_id':task_id,'context_token':g.token()})
            QMessageBox.information(d,'采用进度',value.get('message','已开始采用，请在任务进度查看'));d.accept()
        except Exception as exc:QMessageBox.warning(d,'未采用结果',str(exc))
    pick.currentIndexChanged.connect(changed);refresh();d.exec()
