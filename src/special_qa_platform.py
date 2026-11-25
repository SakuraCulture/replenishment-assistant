"""Disk-backed, offline AX transport for the explicitly isolated Qt acceptance run."""
import json
from pathlib import Path
from decimal import Decimal
import time
import openpyxl
from aoxiang_directory import file_hash,atomic_json
from aoxiang_ui_common import component_root
from jst_one_click_fixture import Component

class JST(Component):
    def __init__(self,folder):
        super().__init__(folder)
        self.record=folder/'controlled-jst.json'
        if self.record.exists():
            data=json.loads(self.record.read_text('utf8'))
            self.orders,self.calls=data['orders'],data['calls']

    def order(self,action,spec,args):
        try:
            if action=='order.import' and (self.folder/'lose-ax-response').exists() and not self.orders:
                self.drop_import_response=True
            result=super().order(action,spec,args)
            if action in ('order.preview','order.stage_import'):
                result.update(order_count=1,import_format=spec.get('import_format','MultiCommodity'))
            return result
        finally:
            self.drop_import_response=False
            atomic_json(self.record,dict(orders=self.orders,calls=self.calls))

class Platform:
    def __init__(self,root):
        if json.loads((root/'synthetic.json').read_text('utf8'))!={'synthetic':True,'live_platform':False}:
            raise ValueError('受控平台只接受隔离样本')
        self.root=root
        self.running=False
        self.statepath=root/'controlled-platform.json'
        self.state=json.loads(self.statepath.read_text('utf8')) if self.statepath.exists() else dict(calls=[],create=0,accepted=0,queries=0)
        self.config={'configured':True,'enabled':True,'account_binding':{'account_id':'TEST-AX','merchant_code':'TEST-MERCHANT'},
                     'store_mapping_version':'QA-1','targets':[{'buyerWarehouseCode':'A','displayName':'测试门店A'}]}
        f=root/'inputs/controlled-template.xlsx'
        contract=json.loads((component_root()/'assets/purchase-import-template-contract.json').read_text('utf8'))
        self.template=dict(status='SUPPORTED',file=str(f),sha256=file_hash(f),contractVersion=contract['version'],schema=contract['schema'],
                           identity={'accountId':'TEST-AX','merchantCode':'TEST-MERCHANT'},uiProductLimit=3000,attemptId='QA-template',
                           recordFile=str(root/'template-receipt.json'))
        atomic_json(Path(self.template['recordFile']),self.template)
    def start(self,*args,**kw):
        self.running=True
    def close(self,*args,**kw):
        self.running=False
        return True
    def save(self):
        atomic_json(self.statepath,self.state)
    def observation(self,intent):
        original='QA-AX-1001'
        common=dict(orderId=original,status='已完成',store=intent['basis']['store'],supplier=intent['supplierCode'])
        rows=[]
        for i,line in enumerate(intent['rows'],2):
            short=self.root.joinpath('short-receipt').exists() and i==2
            qty=str(Decimal(line['quantity'])-(1 if short else 0))
            amount=str(Decimal(qty)*Decimal(line['unitPrice']))
            rows.append({**line,**common,'unit':'件','_sourceRow':i,'receivedQuantity':qty,'receivedAmount':amount})
        main={**common,'skuCount':str(len(rows)),'_sourceRow':2}
        for k in ('quantity','amount','receivedQuantity','receivedAmount'):
            main[k]=str(sum(Decimal(r[k]) for r in rows))
        files=[]
        for source,data in [('D16',[main]),('D17',rows)]:
            f=self.root/(source+'-controlled.xlsx')
            book=openpyxl.Workbook(write_only=True)
            sh=book.create_sheet('采购主单详情' if source=='D16' else '采购子单详情')
            sh.append(list(data[0]))
            for row in data:sh.append(list(row.values()))
            book.save(f)
            files.append(dict(source=source,file=str(f),sha256=file_hash(f)))
        value=dict(schema=1,operation='purchase.receipt.observation',purchaseId=intent['id'],identity=intent['identity'],basis=intent['basis'],
                   supplierCode=intent['supplierCode'],orderId=original,eligible=True,terminal=True,issues=[],main=main,rows=rows,files=files,
                   observedAt='2026-11-26T12:00:00+08:00',quantityBasis='native_actual_inventory_quantity_no_conversion')
        f=self.root/'native-observation.json'
        atomic_json(f,value)
        return dict(file=str(f),sha256=file_hash(f))

    def request(self,action,**kw):
        s=self.state
        s['calls'].append({'action':action,'at':time.time()})
        self.save()
        if action=='configuration':return self.config
        if action=='template.start':return {'id':'QA-template'}
        if action=='template.status':return dict(id='QA-template',stage='原生模板',state='READY',receipt=self.template)
        if action=='purchase.list':return {'items':[s['purchase']] if 'purchase' in s else [],'total':int('purchase' in s)}
        if action=='purchase.reserve':
            f=Path(kw['package_file'])
            if file_hash(f)!=kw['package_sha256']:raise ValueError('原包摘要不符')
            intent=json.loads(f.read_text('utf8'))
            if 'purchase' not in s:
                s['intent']=intent
                s['purchase']=dict(id=intent['id'],basis=intent['basis'],totalQuantity=intent['totalQuantity'],totalAmount=intent['totalAmount'],
                    rows=len(intent['rows']),createdAt='2026-11-26',sequence=1,state='RESERVED',active=False,uploadAttempted=False)
            elif s['purchase']['id']!=intent['id']:raise ValueError('同一组不能再建第二张采购')
            self.save()
            return {'record':s['purchase']}
        if action=='purchase.start':
            p=s['purchase']
            if kw.get('expected_task_id'):
                if p.get('taskId')!=kw['expected_task_id']:raise ValueError('回查任务号错误')
                s['queries']+=1
            else:
                s['create']+=1
                if s['create']>1:
                    self.save()
                    raise ValueError('受控平台收到重复建单请求')
                s['accepted']+=1
                p.update(uploadAttempted=True,taskId='QA-AX-TASK',state='ACCEPTED',sequence=2)
                self.save()
                if (self.root/'lose-ax-response').exists():
                    raise RuntimeError('受控故障：平台已受理，提交应答中断')
            p.update(state='BOUND',sequence=3,binding={'orderId':'QA-AX-1001','status':'已到待入库'})
            self.save()
            return p
        if action=='purchase.status':
            s['queries']+=1
            self.save()
            return s['purchase']
        if action.startswith('receipt.'):
            p=s['purchase']
            record=s.setdefault('receipt',dict(id=p['id'],basis=p['basis'],sequence=1,state='IDLE',terminal=False,active=False,
                scope={'orderId':p['binding']['orderId'],'store':p['basis']['store'],'supplier':s['intent']['supplierCode'],'identity':s['intent']['identity']}))
            if action=='receipt.start':
                s['queries']+=1
                record.update(state='READY',sequence=2,terminal=True,manifest=self.observation(s['intent']),summary={'status':'已完成'})
            elif action=='receipt.synced':
                if (self.root/'lose-receipt-response').exists() and not s.get('dropped'):
                    s['dropped']=True
                    self.save()
                    raise RuntimeError('受控故障：正式入账后回执中断')
                record.update(state='SYNCED',sequence=3,hostReceipt=kw['receipt'])
            elif action!='receipt.status':raise ValueError('未配置的回执动作 '+action)
            self.save()
            return record
        raise ValueError('隔离平台没有此操作 '+action)
