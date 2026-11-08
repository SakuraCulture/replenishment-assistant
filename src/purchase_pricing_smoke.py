"""Opt-in, isolated packaged pricing/preview acceptance; no platform requests."""
import json,time,traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtWidgets import QMessageBox
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]);checks=[];dialog=None
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    class Client:
        running=False
        def __init__(self,*args):self.calls=[]
        def start(self):self.running=True
        def close(self,timeout=10):self.running=False;return True
        def request(self,action,**kwargs):
            self.calls.append(action)
            if action=='configuration':return {'configured':True,'enabled':True}
            if action=='purchase.list':return {'items':[],'total':0}
            raise AssertionError('禁止实际采购动作：'+action)
    def execute():
        nonlocal dialog
        try:
            from aoxiang_purchase_pricing import POLICY,priced_row
            from aoxiang_purchase_excel import build_input_sheet
            from aoxiang_purchase_ui import PurchaseDialog
            rows=[{'sku':str(i),'name':'合成商品'+str(i),'quantity':q,**priced_row(q,a)} for i,(q,a) in enumerate([('3','10'),('2','5.35'),('7','.99')],1)]
            require([r['unit_price'] for r in rows]==['3.33','2.68','0.14'],'两位单价含循环小数及2.675四舍五入')
            require([r['amount'] for r in rows]==['9.99','5.36','0.98'],'采购逐项金额按两位单价计算')
            head={'name':'隔离合成门店','warehouse':'山东仓','store':'T','number':'BH-OFFLINE'}
            plan={'head':head,'supplier_code':'TEST','rows':rows,'price_basis':POLICY}
            raw=b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData><row r="8"><c r="E8"><v>1</v></c></row></sheetData></worksheet>'
            sheet=build_input_sheet(raw,plan,{'inputStartRow':8,'sampleRows':[{'row':8}]})
            ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'};root=ET.fromstring(sheet)
            cells={c.attrib['r']:c for c in root.findall('.//s:c',ns)}
            require([cells['E'+str(i)].find('s:v',ns).text for i in (8,9,10)]==['3.33','2.68','0.14'],'实际Excel数值写两位单价，非仅显示格式')
            require(all('F'+str(i) not in cells for i in (8,9,10)),'金额列留空，防止金额优先导致反算高精度单价')
            (out/'synthetic-sheet.xml').write_bytes(sheet)
            host=SimpleNamespace(repository=SimpleNamespace(folder=out/'settings'),work_repository=SimpleNamespace(path=out/'unused.sqlite'))
            dialog=PurchaseDialog(None,host,head,client_factory=Client);dialog.resize(1280,800);dialog.show()
            deadline=time.monotonic()+20
            while dialog.configuration is None or dialog.worker is not None:
                if time.monotonic()>deadline:raise AssertionError('隔离预览窗口初始化超时')
                loop=QEventLoop();QTimer.singleShot(20,loop.quit);loop.exec()
            intent={'target':{'displayName':head['name']},'supplierCode':'TEST','rows':rows,'totalQuantity':'12','totalAmount':'16.33','paymentTotalAmount':'16.34','roundingDelta':'-0.01'}
            dialog.package={'intent':intent};dialog.render()
            app.processEvents()
            require(dialog.table.columnCount()==7,'原付款、单价、采购金额与差额各自展示')
            require([dialog.table.item(0,i).text() for i in (3,4,5,6)]==['10','3.33','9.99','-0.01'],'原付款和舍入差额准确显示')
            require('16.34' in dialog.summary.text() and '16.33' in dialog.summary.text() and '-0.01' in dialog.summary.text(),'预览汇总保留原付款及采购差额')
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.No) as question:
                dialog.upload()
                text=question.call_args.args[2]
            require(all(x in text for x in ('16.34','16.33','-0.01','四舍五入')),'原上传确认框展示舍入规则与差额')
            require(not any(a in dialog.client.calls for a in ('purchase.reserve','purchase.start')),'取消确认未派发采购，无平台请求')
            dialog.grab().save(str(out/'purchase-preview.png'))
            report={'passed':True,**diagnostic(),'checks':checks,'synthetic':True,'live_platform':False}
        except Exception:report={'passed':False,**diagnostic(),'checks':checks,'error':traceback.format_exc()}
        finally:
            if dialog:dialog.close()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            win._discard_close=True;win.close();QTimer.singleShot(200,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(200,execute)
