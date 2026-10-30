"""Merchant reply preview and independent revision history, outside calculation work."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QFileDialog,QMessageBox
from ui_controls import label,button
from business_ui import run,grid
from runtime_support import Cancelled
import merchant_feedback as feedback
import business_excel

HEADERS=['商品编码','商品名称','推荐分组','原签发建议','门店确认数量','与签发差额','确认状态','所属仓']

def import_returns(parent,host):
    from import_ui import choose
    paths=choose(parent,'feedback','导入加盟商回传（可多文件）',True)
    if not paths:return
    try:
        path=host.work_repository.path;scanned=run(parent,feedback.scan,(paths,),'读取回传表')
        options=run(parent,feedback.choices,(path,list(scanned['orders'])),'匹配补货单号');bindings={}
        if any(not opts for opts in options.values()):raise ValueError('本机未找到这些单号：'+'、'.join(n for n,opts in options.items() if not opts))
        needed=[n for n,vs in scanned['orders'].items() if None in vs]
        if needed:
            d=QDialog(parent);d.setWindowTitle('明确选择原签发修订');v=QVBoxLayout(d);v.addWidget(label('这些文件缺少修订标识。请选择当时发给门店的修订；不得按日期或名称自动猜测。'));combos={}
            for number in needed:
                v.addWidget(label(number));combo=QComboBox();combo.addItem('请选择签发修订',None)
                for option in options[number]:combo.addItem(f"V{option['revision']:03d} · {option['saved']}"+(' · 当前' if option['revision']==option['current'] else ' · 历史'),option['revision'])
                v.addWidget(combo);combos[number]=combo
            go=button('核对回传差异',d.accept,True);go.setEnabled(False)
            for combo in combos.values():combo.currentIndexChanged.connect(lambda:go.setEnabled(all(c.currentData() is not None for c in combos.values())))
            v.addWidget(go);v.addWidget(button('取消',d.reject))
            if d.exec()!=QDialog.DialogCode.Accepted:return
            bindings={n:c.currentData() for n,c in combos.items()}
        plan=run(parent,feedback.prepare,(path,scanned,bindings),'核对回传差异')
        d=QDialog(parent);d.setWindowTitle('确认保存门店回传 · 不改原签发建议');d.resize(1150,720);v=QVBoxLayout(d);rows=[];summary=[]
        for p in plan['plans']:
            h=p['head'];summary.append(f"{h['number']} V{h['revision']:03d}：{len(p['differences'])}款变化；{p['payload']['status']}；尚待确认{len(p['payload']['pending'])}款"+('；同一内容，无新增回传版本' if not p['changed'] else '')+('；绑定历史修订，不应用到当前单' if h['revision']!=h['current_revision'] else ''))
            summary.extend(wh+'：'+v['status']+'，待确认 '+str(len(v['pending']))+' 款' for wh,v in p['payload']['warehouses'].items())
            for row in p['differences']:rows.append([p['payload']['incoming'][row[0]]['number'],*row,p['payload']['incoming'][row[0]]['warehouse']])
            summary.extend(p['warnings'])
        v.addWidget(label('\n'.join(summary)));table,model=grid(d,['补货单号','商品编码','商品名称','原签发建议','上次回传','本次回传','与签发差额','所属仓'],rows);v.addWidget(table,1)
        v.addWidget(label('空白不清除已有回传，0表示明确不补；回传不会重算预算或自动规整箱规。','muted'));row=QHBoxLayout();cancel=button('取消',d.reject);cancel.setDefault(True);row.addWidget(cancel);save=button('确认保存独立回传',d.accept,True);save.setAutoDefault(False);row.addWidget(save);v.addLayout(row)
        if d.exec()!=QDialog.DialogCode.Accepted:return
        receipts=run(parent,feedback.commit,(path,plan),'保存回传（事务完成前不可关闭）',False)
        QMessageBox.information(parent,'回传已处理','\n'.join(f"{r['number']} V{r['revision']:03d} · 回传第{r['feedback_version']}版 · {r['status']}" for r in receipts))
        return receipts
    except Cancelled:return
    except Exception as exc:QMessageBox.warning(parent,'回传未保存',str(exc))

class FeedbackDialog(QDialog):
    def __init__(self,parent,host,head):
        super().__init__(parent);self.host=host;self.head=head;self.setWindowTitle(head['number']+f" V{head['revision']:03d} · 门店回传");self.resize(1150,700);v=QVBoxLayout(self);self.note=label('');v.addWidget(self.note);self.version=QComboBox();v.addWidget(self.version);self.table,self.model=grid(self,HEADERS);v.addWidget(self.table,1);row=QHBoxLayout();row.addWidget(button('导入回传Excel',self.import_files));row.addWidget(button('下载最低标准模板',self.template));row.addWidget(button('回传来源 / 下载原文件',self.sources));row.addWidget(button('导出当前对照',self.export));row.addWidget(button('关闭',self.accept));v.addLayout(row);self.version.currentIndexChanged.connect(self.show_version);self.refresh()
    def refresh(self):
        self.data=run(self,feedback.view,(self.host.work_repository.path,self.head),'读取门店确认记录');self.version.blockSignals(True);self.version.clear()
        for ver,saved,p in self.data['history']:self.version.addItem(f'回传第{ver}版 · {saved}',p)
        if not self.data['history']:self.version.addItem('尚无回传',None)
        self.version.blockSignals(False);self.show_version()
    def show_version(self):
        p=self.version.currentData();values=p['values'] if p else {};rows=[]
        for original in self.data['rows']:
            r=list(original);r[4]=values.get(r[0]);r[5]=None if r[4] is None else r[4]-r[3];r[6]='未确认' if r[4] is None else '已确认';rows.append(r)
        self.model.replace(rows)
        status=[]
        for wh in dict.fromkeys(r[7] for r in rows):
            group=[r for r in rows if r[7]==wh];pending=sum(r[3]>0 and r[4] is None for r in group);received=sum(r[4] is not None for r in group)
            status.append(str(wh)+'：'+('尚无回传' if not received else '部分确认' if pending else '已确认')+f'（待确认{pending}款）')
        self.note.setText('；'.join(status)+' · 原签发建议不变，回传数量为库存单位。')
    def import_files(self):
        if import_returns(self,self.host):self.refresh()
    def template(self):
        from import_ui import download
        download(self,'feedback')
    def sources(self):
        p=self.version.currentData()
        if not p:QMessageBox.information(self,'尚无回传','尚无回传原文件');return
        d=QDialog(self);d.setWindowTitle('本版回传来源与原文件');d.resize(1150,650);v=QVBoxLayout(d);rows=[]
        for sku,item in p['incoming'].items():
            for source in item['sources']:rows.append([sku,item['quantity'],item.get('note'),source['file'],source['sheet'],source['row'],source['sha']])
        table,model=grid(d,['商品编码','本次回传','备注','原文件','工作表','行号','SHA256'],rows);v.addWidget(table,1);files=QComboBox()
        for f in p['files']:files.addItem(f['name'],f)
        v.addWidget(files)
        def save():
            f=files.currentData()
            if not f:return
            path,_=QFileDialog.getSaveFileName(d,'下载回传原文件',f['name'],'Excel (*.xlsx)')
            if not path:return
            version=self.data['history'][self.version.currentIndex()][0]
            try:run(d,feedback.export_source,(self.host.work_repository.path,self.head,version,f['sha'],path),'下载回传原文件')
            except Exception as exc:QMessageBox.warning(d,'未下载',str(exc))
        v.addWidget(button('下载选中的回传原文件',save));v.addWidget(button('关闭',d.accept));d.exec()
    def export(self):
        path,_=QFileDialog.getSaveFileName(self,'导出数量对照',self.head['number']+'门店确认对照.xlsx','Excel (*.xlsx)')
        if not path:return
        info=[['补货单号',self.head['number']],['签发修订',self.head['revision']],['回传版本',self.version.currentText()],['数量单位','库存单位；签发建议与回传独立保存']]
        try:run(self,business_excel.write,(path,[('数量对照',HEADERS,self.model.rows),('单据信息',['项目','内容'],info)]),'导出数量对照')
        except Exception as exc:QMessageBox.warning(self,'未导出',str(exc))
