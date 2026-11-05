"""Configure, repair and verify one explicitly selected local client/environment."""
import json
from pathlib import Path
from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QFileDialog, QMessageBox
from ui_controls import label, button
import automation_connection as connection


def create_dialog(host):
    g=host.automation;d=QDialog(host);d.setWindowTitle('AI 协助 · 本机连接');d.resize(780,480)
    layout=QVBoxLayout(d)
    layout.addWidget(label('选择客户端 → 配置本机连接 → 检查 → 到客户端开始使用'))
    layout.addWidget(label('只绑定当前资料环境。新电脑需重新配置；首次信任和客户端重新加载按其提示操作。','muted'))
    row=QHBoxLayout();client=QComboBox();client.addItem('Codex','codex');client.addItem('WorkBuddy','workbuddy');row.addWidget(client)
    target=QLineEdit();target.setReadOnly(True);target.setToolTip('仅修改其中的 replenishment 条目，其他连接和设置保留。');row.addWidget(target,1)
    def choose():
        path,_=QFileDialog.getSaveFileName(d,'选择客户端已有配置（保留其他设置）',target.text(),'配置文件 (*.json *.toml)',options=QFileDialog.Option.DontConfirmOverwrite)
        if path:target.setText(path)
    row.addWidget(button('选择配置文件…',choose));layout.addLayout(row)
    status=label('');layout.addWidget(status)
    result=label('尚未验证客户端。工具能列出，不等于已连接到当前作业。','muted');layout.addWidget(result)
    registered=[False];verified=[False];expired=[False];process=QProcess(d);timeout=QTimer(d);timeout.setSingleShot(True)
    def refresh():
        last=g.last_client
        recent=bool(last and last['name'].startswith('MCP / ') and last['version_match'] and last['instance']==g.instance)
        status.setText('① 客户端配置：'+('已写入，需在客户端加载/信任' if registered[0] else '尚未在本窗口确认')+
            '\n② 工作台连接：'+('已启用' if g.enabled else '未启用')+
            '\n③ 资料环境与版本：'+('链路核对通过' if verified[0] and g.enabled else '待检查')+
            '\n④ 客户端实际接入：'+('最近已核对当前工作台（不代表持续在线）' if recent else '待客户端调用核对'))
        enable.setEnabled(not g.enabled);disable.setEnabled(g.enabled)
    def select():target.setText(str(connection.client_path(client.currentData())));registered[0]=False;verified[0]=False;refresh()
    def change(on):
        try:g.enable() if on else g.disable();verified[0]=False
        except Exception as exc:QMessageBox.warning(d,'连接未完成',str(exc))
        refresh()
    row=QHBoxLayout();enable=button('启用本机连接',lambda:change(True));disable=button('关闭本机连接',lambda:change(False));row.addWidget(enable);row.addWidget(disable);layout.addLayout(row)
    def configure():
        try:
            entry=connection.planned_entry(g);plan=connection.prepare_config(client.currentData(),Path(target.text()),entry)
            prompt='将此客户端的“补货助手”连接绑定到当前资料环境。\n其他连接与设置保留；原配置会先备份。\n如之前连接的是其他资料环境，本次会明确切换到当前环境。\n\n目标：'+str(plan['path'])
            if QMessageBox.question(d,'核对本机连接',prompt,QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Ok:return
            if not g.enabled:g.enable()
            connection.install_entry(g);receipt=connection.apply_config(plan);registered[0]=True;verified[0]=False
            result.setText('配置完成。请在 '+client.currentText()+' 重新加载补货助手连接；首次使用需信任。随后点击“检查本机连接”。')
            result.setToolTip('备份位置：'+str(receipt['backup'] or '首次配置，无旧文件'))
        except Exception as exc:QMessageBox.warning(d,'配置未完成',str(exc))
        refresh()
    configure_button=button('配置/修复本机连接',configure,True);layout.addWidget(configure_button)
    def check():
        if process.state()!=QProcess.ProcessState.NotRunning:return
        verified[0]=False;expired[0]=False
        try:
            if not g.enabled:raise ValueError('请先启用本机连接')
            path=Path(target.text());raw=path.read_bytes() if path.exists() else None
            _,entry=connection.merge_config(client.currentData(),raw,connection.planned_entry(g))
            if not isinstance(entry,dict) or not isinstance(entry.get('command'),str) or not isinstance(entry.get('args'),list) or not all(isinstance(a,str) for a in entry['args']):raise ValueError('该客户端未配置本机接口，请先配置/修复')
            # Never execute an arbitrary command found in a third-party config.
            expected=connection.planned_entry(g)
            if entry['command']!=expected['command'] or entry['args']!=expected['args']:raise ValueError('客户端仍指向旧程序或其他资料环境，请先配置/修复')
            result.setText('正在启动接口并核对当前工作台…');check_button.setEnabled(False);configure_button.setEnabled(False)
            process.start(entry['command'],entry['args']+['doctor']);timeout.start(15000)
        except Exception as exc:result.setText(str(exc));refresh()
    def finished(*_):
        timeout.stop();check_button.setEnabled(True);configure_button.setEnabled(True)
        try:
            if expired[0]:raise ValueError('等待接口超过15秒，检查已停止；请核对程序位置、连接开关后重试')
            output=bytes(process.readAllStandardOutput()).decode('utf-8');reply=json.loads(output)
            if not reply.get('ok'):raise ValueError(reply.get('error',{}).get('message','接口未完成检查'))
            data=reply['result'];ctx=data['workbench']
            if not data['versions_match']:raise ValueError('接口与工作台版本不一致，请重新配置后在客户端重新加载')
            if ctx['instance_id']!=g.instance or ctx['workspace_id']!=g.workspace:raise ValueError('返回了其他工作台，请修复配置；未采用其数据')
            verified[0]=True;registered[0]=True
            result.setText('本机链路验证通过。资料环境和版本一致；客户端仍需加载/信任连接，并重新复制当前业务提示词。')
        except Exception as exc:result.setText('检查未通过：'+str(exc))
        refresh()
    check_button=button('检查本机连接',check);layout.addWidget(check_button)
    process.finished.connect(finished)
    def failed(_):
        if process.error()==QProcess.ProcessError.FailedToStart:
            timeout.stop();check_button.setEnabled(True);configure_button.setEnabled(True);result.setText('接口程序无法启动，可能已移动或缺失，请配置/修复。')
    process.errorOccurred.connect(failed)
    def expire():expired[0]=True;process.kill()
    timeout.timeout.connect(expire)
    layout.addWidget(label('业务原表留在本机；工具返回的业务摘要可能进入所选 AI 服务。连接密钥和浏览器登录态不提供给模型。','muted'))
    def export():
        folder=QFileDialog.getExistingDirectory(d,'选择配置说明保存目录')
        if not folder:return
        try:
            from automation_ui import connection_configs,cli_guide
            data=connection_configs(host);files={'Codex_补货助手_MCP.toml':data['codex'],
                'WorkBuddy_补货助手_MCP.json':json.dumps(data['workbuddy'],ensure_ascii=False,indent=2),'补货助手_CLI使用说明.txt':cli_guide(host)}
            if any((Path(folder)/name).exists() for name in files):raise ValueError('已有同名说明，请选择新目录')
            for name,body in files.items():(Path(folder)/name).write_text(body,'utf-8')
            result.setText('已导出手动配置片段。路径仅适用于本机；换机请从新电脑重新生成。')
        except Exception as exc:QMessageBox.warning(d,'未导出说明',str(exc))
    row=QHBoxLayout();row.addWidget(button('导出配置与 CLI 说明',export))
    from automation_ui import tasks
    row.addWidget(button('查看 AI 任务',lambda:tasks(host)));row.addWidget(button('× 关闭',d.reject));layout.addLayout(row)
    def close():
        if process.state()!=QProcess.ProcessState.NotRunning:process.kill();process.waitForFinished(2000)
        try:g.changed.disconnect(refresh)
        except (RuntimeError,TypeError):pass
    d.finished.connect(close);g.changed.connect(refresh)
    client.currentIndexChanged.connect(select);select()
    # Exposed handles are used by the actual packaged UI acceptance, not by business callers.
    d.client_pick=client;d.config_path=target;d.configure_button=configure_button;d.check_button=check_button;d.probe=process;d.result_label=result
    return d


def show(host):
    d=create_dialog(host);d.exec();d.deleteLater()
