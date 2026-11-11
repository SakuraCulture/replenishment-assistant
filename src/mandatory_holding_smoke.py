"""Opt-in frozen-EXE acceptance using isolated, synthetic Excel and local DBs."""
import copy,json,os,sys,traceback
from pathlib import Path
from PySide6.QtCore import QTimer


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Requires isolated synthetic settings')
    def run():
        from openpyxl import Workbook,load_workbook
        import admission,batch_service,engine,new_product_focus,normal_exports,normal_orders,preissue,priority_list,result_semantics as sem
        import work_repository as wr
        from version_info import diagnostic,MODEL_VERSION
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False,'checks':[],**diagnostic()}
        def require(ok,text):
            if not ok:raise AssertionError(text)
            report['checks'].append(text)
        try:
            store='QA_MUST';day=wr.today();sales=out/'合成补货建议.xlsx';master=out/'合成商品资料.xlsx'
            fields=['仓库/门店名称','仓库/门店编码','商品编码','商品名称','日销','周销','月销','可用库存','在途库存','缺货天数（天）','库存单位','补货状态','商品标签']
            products=['商品编码','商品名称','分类','出库箱规','基本售价','商品状态','仓归属','商品标签']
            # sku, sales, available, inbound, pack, master tag, supply, export state, sales tag
            cases=[('01',(0,0,0),0,0,6,'','启用','正常',''),('02',(0,0,0),1,0,1,'','启用','正常',''),
                   ('03',(2,14,60),0,0,1,'必卖；日配','启用','正常',''),('04',(2,14,60),0,0,1,'','禁用','正常',''),
                   ('05',(2,14,60),0,0,1,'','启用','停采',''),('06',(2,14,60),0,0,1,'','启用','禁采',''),
                   ('07',(0,0,0),0,0,1,'','启用','正常',''),('08',(2,14,60),0,0,1,'','启用','正常',''),
                   ('09',(0,0,0),.25,.25,1,'','启用','正常',''),('10',(0,0,0),0,0,1,'','启用','正常','日配')]
            b=Workbook();s=b.active;s.title='商品补货建议';s.append(fields)
            m=Workbook();p=m.active;p.title='Sheet1';p.append(products)
            for sku,sale,k,l,step,tag,status,export,salestag in cases:
                s.append(['合成必采验收店',store,sku,'合成商品'+sku,*sale,k,l,0,'包',export,salestag])
                p.append([sku,'合成商品'+sku,'休闲食品',step,2,status,'河北仓',tag])
            b.save(sales);b.close();m.save(master);m.close()
            loaded=engine.load_inputs(sales,master)
            profile=priority_list.make_profile([{'sku':c[0],'name':'合成'+c[0]} for c in cases if c[0] not in ('07','08')],'合成必采清单')
            settings={'warehouses':{'河北仓':(8,5)},'exception':False,'budget':None,'admission':admission.snapshot(win.work_repository.path,store)}
            r=batch_service.calculate_store(loaded,store,settings,profile);rows={d['E']:d for d in r['rows']}
            require(rows['01']['AS']==0 and sem.order_quantity(rows['01'])==6 and sem.order_amount(rows['01'])==12,'零销零库存必采默认补一箱，原模型零建议保留')
            require(sem.order_quantity(rows['02'])==0 and sem.order_quantity(rows['09'])==1,'已有覆盖不重补，不足基数按出库单位补足')
            require(all(sem.order_quantity(rows[s])==0 for s in ('03','04','05','06')),'日配、禁用、停采、禁采优先排除必采品')
            require(sem.order_quantity(rows['07'])==0 and sem.order_quantity(rows['08'])==rows['08']['AS'],'普通零销及普通动销原规则保留')
            require(sem.order_quantity(rows['10'])==1,'仅采用商品资料日配标签，不误用补货建议同名字段')
            win.loaded=loaded;win.result=r;win.current_store=store;win.results={store:r}
            win.show_section(1);win.set_rows(r['rows'],True);win.pages.setCurrentIndex(win.pages.count()-1)
            win.show_technical_detail(rows['01']);win.detail_panel.show();app.processEvents()
            require('必采最低持有' in win.details.toPlainText(),'实际Qt商品详情可读到最低持有依据')
            win.grab().save(str(out/'必采最低持有详情.png'))
            focus=new_product_focus.empty(day[:7]);payload={'schema':1,'model_version':MODEL_VERSION,'day':day,'input_dirty':False,
                'states':{store:{'state':'已完成','name':'合成必采验收店'}},'results':{store:r},'new_focus':focus,'admission_required':True}
            repo=normal_orders.Repository(win.work_repository);head=repo.confirm(payload,[store],None,{store:None})[0]
            order=repo.get(day,store,head['revision']);normal_exports.export(order,out/'签发确认单.xlsx',warehouse='河北仓')
            with_read=load_workbook(out/'签发确认单.xlsx',read_only=True,data_only=True)
            try:
                exported={str(c[4]):(c[16],c[20]) for ws in with_read if ws.sheet_state=='visible' for c in list(ws.values)[1:]}
                require(exported['01']==(6,'必采最低持有') and all(s not in exported for s in ('03','04','05','06')),'签发Excel保存有效数量及必采理由，排除商品不混入')
            finally:with_read.close()
            require(sem.order_quantity(order['payload']['result']['rows'][0])==6,'签发归档及重新读取保持最低持有执行量')
            zero=batch_service.calculate_store(loaded,store,{**settings,'budget':0},profile)
            require(all(sem.order_quantity(d)==0 for d in zero['rows']) and zero['summary']['holding_gaps']>=3,'预算零不超支，零销必采缺口仍显式保留')
            preview=preissue.preview(r,{'01':{'quantity':0,'reason':'合成验收人工例外','kind':'manual'}},day)
            require(any(x['kind']=='mandatory_holding' for x in preview['risks']),'人工取消必采保底可定位风险，不能默默丢失保护')
            changed=preissue.finalize(preview,[x['id'] for x in preview['risks']],current=r)
            require(preissue.original(changed)==r,'人工例外撤销后精确恢复自动数量和依据')
            old=copy.deepcopy(rows['01']);old.pop('_mandatory_holding');old['_meta']['product_tags']='日配'
            require(sem.order_quantity(old)==0,'旧快照没有新规则依据时不追溯补算或改写')
            report['passed']=True
        except Exception:report['error']=traceback.format_exc()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
        print(json.dumps(report,ensure_ascii=False));win.dirty=False;win.readonly=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(200,run)
