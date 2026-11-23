"""Opt-in packaged workflow check on an explicitly isolated settings directory."""
import json
import os
from pathlib import Path
import sys
import time
import traceback

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QLabel,QMessageBox,QPushButton


def start_smoke(app,win,args):
    out=Path(args[0]).resolve()
    data=json.loads((out/'fixture.json').read_text('utf-8'))
    if not data.get('isolated') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Independent acceptance settings required')
    checks=[]
    messages=[]
    def require(condition,text):
        if not condition:raise AssertionError(text)
        checks.append(text)
    def idle():
        limit=time.monotonic()+110
        while win.worker is not None:
            if time.monotonic()>limit:raise TimeoutError('等待实际工作台后台任务')
            app.processEvents()
            time.sleep(.012)
        app.processEvents()
        if messages:raise RuntimeError('；'.join(messages))
    def wait_for(predicate,label):
        limit=time.monotonic()+45
        while not predicate():
            if time.monotonic()>limit:raise TimeoutError(label)
            app.processEvents()
            time.sleep(.012)
    def run():
        from collections import Counter
        from contextlib import closing
        from unittest.mock import patch
        import jst_allocation
        import new_product_focus as focus
        import new_products
        import normal_delivery
        import normal_exports
        from new_product_ui import NewProductDialog
        from openpyxl import load_workbook
        import result_semantics as sem
        import warehouse_newness as recent
        import work_repository as wr
        from version_info import diagnostic
        day=data['day']; hb='河北仓'; sd='山东仓'; sku=data['shared_sku']; store='S1'
        def message(_,title,text,*args,**kwargs):
            messages.append(title+' · '+text)
            return QMessageBox.StandardButton.Ok
        def import_through_button(wh,file):
            win.navigate(0)
            with patch('import_ui.choose',return_value=[file]),patch('interaction_ui.confirm',return_value=True):
                win.catalog_buttons[wh].click()
                idle()
            return win.catalogs[wh]
        def examine(wh):
            returned={};errors=[];timer=QTimer(win)
            def read():
                box=QApplication.activeModalWidget()
                if not box or box.objectName()!='warehouseRecentDialog':return
                try:
                    timer.stop()
                    returned['summary']=box.findChild(QLabel,'warehouseRecentCounts').text()
                    returned['groups']={k:list(v) for k,v in box.groups.items()}
                    box.grab().save(str(out/('新品窗口-'+wh+'-'+str(len(checks))+'.png')))
                except Exception:errors.append(traceback.format_exc())
                finally:box.accept()
            timer.setInterval(30);timer.timeout.connect(read);timer.start()
            win.findChild(QPushButton,'warehouseRecent'+wh).click()
            timer.stop()
            if errors:raise AssertionError(errors[0])
            return returned
        def examine_history(warehouse,basis):
            from history_read import detail_head
            from history_ui import AuditDialog
            head=detail_head(win.work_repository.path,day,store,warehouse,1)
            dialog=AuditDialog(win,win,head,sku=sku)
            try:
                dialog.setModal(False);dialog.show()
                wait_for(lambda:dialog.loaded_key==('决策总览',sku),'等待签发历史详情读回')
                values={key:value for key,value,*_ in dialog.model.rows}
                require((values['仓新品判定日'],values['新品判定采用仓'],values['仓资料版本'],
                         values['首入库日期'],values['仓新品'])==
                        (basis['day'],warehouse,basis['catalog_revision'],basis['first_date'],'是'),
                        '历史决策总览展示原仓新品依据')
                dialog.grab().save(str(out/'历史签发新品依据.png'))
            finally:dialog.reject();dialog.deleteLater()
        def examine_selection(result,profile,expected):
            base=new_products.original(result)
            dialog=NewProductDialog(win,base,base,profile)
            try:
                dialog.group.setCurrentText('本月新品')
                current=[dialog.rows[i]['E'] for i in dialog.model.visible]
                require(current==expected,'新品选品按实际供货仓过滤，旧关注记录不参与全商品档案判定')
                for i in dialog.model.visible:
                    row=dialog.rows[i]
                    require(row['AU'] in dialog.model.default_sources[i],
                            row['E']+' 未勾选时仍按归属仓推荐首铺量')
            finally:dialog.reject();dialog.deleteLater()
        def signed_workbook(path,warehouse,expected_sku,editable):
            with closing(load_workbook(path)) as book:
                require([ws.title for ws in book if ws.protection.sheet]==([] if editable else ['本月新品'])
                        and not book.security.lockStructure,
                        warehouse+(' 补导后本月新品可编辑' if editable else ' 签发时只保护本月新品')+'，可新增工作表')
                current=book['本月新品']
                require(current['E2'].value==expected_sku and current['Q2'].value>0
                        and bool(current.protection.password)==(not editable),warehouse+' 本月新品与签发数量正确')
                return [(ws.title,[[(cell.value,cell.data_type,cell.number_format) for cell in row]
                                    for row in ws.iter_rows()]) for ws in book]
        def allow_editing():
            from export_locations import ExportSettingsDialog
            errors=[]
            def save():
                box=QApplication.activeModalWidget()
                try:
                    require(isinstance(box,ExportSettingsDialog),'顶部导出设置打开实际选项窗口')
                    require(not box.monthly_new_sheet_editable.isChecked(),'首次打开默认只读')
                    box.monthly_new_sheet_editable.click()
                    box.grab().save(str(out/'导出设置.png'))
                    box.save_btn.click()
                except Exception:
                    errors.append(traceback.format_exc())
                    if box:box.reject()
            QTimer.singleShot(0,save)
            next(b for b in win.findChildren(QPushButton) if b.text()=='导出设置').click()
            if errors:raise AssertionError(errors[0])
            reloaded=ExportSettingsDialog(win)
            try:require(reloaded.monthly_new_sheet_editable.isChecked(),'关闭并重开设置后仍允许编辑')
            finally:reloaded.deleteLater()
        def calculate(primary,secondary=None):
            win.navigate(0)
            win.load_btn.click()
            idle()
            require(store in win.store_widgets,'检查文件已识别门店')
            w=win.store_widgets[store];w['check'].setChecked(True);w['mode'].setCurrentIndex(1)
            w['primary'].setCurrentIndex(w['primary'].findData(primary))
            w['secondary'].setCurrentIndex(w['secondary'].findData(secondary))
            win.navigate(1);win.calculate_btn.click();idle()
            require(win.states[store]['state']=='已完成','门店正常计算入口完成')
            return win.results[store]
        def projected(result):
            return [(r['E'],r['AU'],r['AS'],r.get('_new_requested'),sem.order_quantity(r),sem.order_amount(r)) for r in result['rows']]
        def allocated(result,catalogs):
            return {wh:[(x['sku'],x['quantity'],x['amount']) for x in jst_allocation.allocate(
                        [{'sku':r['E'],'base_quantity':sem.order_quantity(r)} for r in result['rows']
                         if r['AU']==wh and sem.orderable(r)],catalog['items'],{})] for wh,catalog in catalogs.items()}
        try:
            with patch.object(wr,'today',return_value=day),patch.object(QMessageBox,'warning',side_effect=message):
                require(win.work_repository.path.resolve()==out/'settings/workbench.sqlite3','实际 EXE 只使用隔离订单库')
                win.job_day=day
                win.show_section(1);win.navigate(0)
                require(not win.focus_import_btn.isVisibleTo(win.import_page),'准备数据默认使用仓45天新品入口')
                win.catalog_maintenance_toggle.setChecked(True)
                require(win.focus_import_btn.isVisibleTo(win.import_page),'旧版关注仍可从兼容入口维护')
                win.catalog_maintenance_toggle.setChecked(False)
                hc=import_through_button(hb,data['hebei'])
                sc=import_through_button(sd,data['shandong_synthetic'])
                require((hc['source_rows'],len(hc['items']),len(hc['quarantined']))==(4525,4494,31),
                        '河北原件 4525 行，采用 4494，另有 31 行待核')
                hv=examine(hb)
                sv=examine(sd)
                expected_hebei=set(data['hebei_expected'])
                require(f'仓新品 {len(expected_hebei):,} 款' in hv['summary'],'实际工作台显示河北仓新品数量')
                require(set(a[0] for a in hv['groups']['新品'])==expected_hebei,'河北仓新品逐 SKU 与预期相同')
                require(Counter(a[4] for a in hv['groups']['新品'])==data.get('hebei_expected_status',{'启用':4,'备用':5}) and
                        sum(a[5]==0 for a in hv['groups']['新品'])==2,'启用/备用/零库存数量与原件一致')
                unknown=next(a for a in hv['groups']['日期待核'] if a[0]=='1717845590862065717')
                require(unknown[5]==10 and unknown[6]==recent.UNKNOWN,'可用 10 的河北空日期留待核')
                require({a[0] for a in sv['groups']['新品']}=={'@SD-new','@SD-boundary','@SD-today'},
                        '山东隔离档案使用自己的三款日期记录')
                for warehouse,view in ((hb,hv),(sd,sv)):
                    exclusions=data.get('excluded',{}).get(warehouse)
                    if exclusions is not None:
                        require({a[0]:a[6] for a in view['groups']['已排除']}==exclusions,
                                warehouse+'仓新品入口逐款显示禁用及淘汰依据')
                dayafter=recent.view(sc,'2026-11-23')
                require('@SD-boundary' not in {a['商品编码'] for a,b in dayafter if b['new']},'翌日同一仓资料第 45 天商品退出')
                win.sales.add_paths([data['sales']])
                win.new_focus=None
                result=calculate(hb,sd)
                by={r['E']:r for r in result['rows']}; row=by[sku]
                require(row['AU']==hb and sem.order_quantity(row)==2 and row['_new_requested']==2,
                        '河北真实 SKU 的仓45天依据进入同店作业，按原箱规自动首铺2件')
                require(by['@SD-new']['AU']==sd and by['@SD-new']['_meta']['warehouse_newness']['new'],
                        '同一批实际归属山东的商品使用山东清单')
                require(len(by)==len(data['sales_codes']) and list(by)==data['sales_codes'],
                        '始终只处理本店已有输入，逐商品顺序一致')
                for code in data.get('screened_sales',[]):
                    excluded=by[code]
                    require(bool(recent.row_screening_reason(excluded)) and not excluded.get('_new_requested')
                            and sem.new_product_group(excluded) is None,code+'不参与新品选择、首铺和签发新品分组')
                if local_sku:=data.get('local_sku'):
                    require(by[local_sku]['_source']=='地采商品','同批保留零销量地采商品，签发校验按中心仓范围执行')
                examine_selection(result,None,['@SD-new',sku])
                frozen=win.snapshot()
                planned=normal_delivery.prepare_batch(win.orders,frozen,[store])
                protected_folder=out/'默认签发';protected_folder.mkdir()
                delivery=normal_delivery.execute_batch(win.orders,planned,protected_folder)
                require(delivery['signed'] and len(delivery['paths'])==2,'成品签发生成两仓文件')
                saved=win.orders.get(day,store)
                basis=next(r['_meta']['warehouse_newness'] for r in saved['payload']['result']['rows'] if r['E']==sku)
                require(basis==row['_meta']['warehouse_newness'] and saved['revision']==1,
                        '无人工关注清单即可签发，保存判定日、仓、资料版本和原首入库日期')
                expected={hb:sku,sd:'@SD-new'}
                originals={wh:signed_workbook(protected_folder/normal_exports.filename(saved,wh),wh,code,False)
                           for wh,code in expected.items()}
                before=projected(result); balances=allocated(result,{hb:hc,sd:sc})
                swapped=calculate(sd,hb)
                other=next(r for r in swapped['rows'] if r['E']==sku)
                require(other['AU']==sd and not other['_meta']['warehouse_newness']['new'] and sem.order_quantity(other)==0,
                        '同店同码改属山东后是老品，未串用河北日期')
                stale_focus=focus.empty('2026-11')
                stale_focus['items']=[{'sku':sku,'name':'旧关注测试','focused':True}]
                stale_focus['revision']=focus.revision(stale_focus)
                examine_selection(swapped,stale_focus,['@SD-new'])
                old=import_through_button(hb,data['hebei_ordinary'])
                legacy=examine(hb)
                require(legacy['summary']==recent.MISSING_COLUMN,'普通商品资料显示旧版未提供日期')
                without=calculate(hb,sd)
                win.new_focus=focus.empty('2026-11')
                win.new_focus['items']=[{'sku':sku,'name':'按相同集合对照','focused':True}]
                win.new_focus['revision']=focus.revision(win.new_focus)
                ordinary=calculate(hb,sd)
                after=projected(ordinary);old_allocation=allocated(ordinary,{hb:old,sd:sc})
                require(before==after and balances==old_allocation,'相同新品集合逐SKU数量、首铺、分配及顺序与旧版一致')
                history=win.orders.get(day,store,1)
                require(projected(history['payload']['result'])==before and
                        next(r['_meta']['warehouse_newness'] for r in history['payload']['result']['rows'] if r['E']==sku)==basis,
                        '换回旧资料后历史签发仍读取原仓日期和数量')
                examine_history(hb,basis)
                allow_editing()
                editable_folder=out/'可编辑补导';editable_folder.mkdir()
                for wh,code in expected.items():
                    path=editable_folder/normal_exports.filename(history,wh)
                    with patch('export_locations.save_file',return_value=(str(path),'Excel')):
                        win.export_loaded_order({**history,'selected_warehouse':wh})
                        wait_for(path.is_file,'等待原签发补导')
                        idle()
                    require(signed_workbook(path,wh,code,True)==originals[wh],
                            wh+' 原签发与可编辑补导的单元格值、类型、格式及顺序一致')
                require(win.orders.head(day,store)['revision']==1,'应用导出设置后补导沿用 V001')
                if legacy:=data.get('legacy_signed'):
                    from business_presenter import warehouse_newness_rows
                    legacy_work=wr.Repository.open_existing(legacy['db'],read_only=True)
                    archived=type(win.orders).from_existing_work(legacy_work).get(day,store,1)
                    old_result=archived['payload']['result']
                    old=next(r for r in old_result['rows'] if r['E']==sku)
                    require(recent.for_row(old)==legacy['basis'] and legacy['basis']['rule']==recent.LEGACY_RULE,
                            '从旧交付数据库读回原 45 天判定依据')
                    require(list(map(list,projected(old_result)))==legacy['rows'] and
                            sem.new_product_group(old)=='本月新品' and warehouse_newness_rows(old)[-1][1]=='是',
                            '旧签发沿用原新品分组、数量、金额及行序')
                changes=[item for item in zip(projected(without),before) if item[0]!=item[1]]
                out_data={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,
                          'hebei':hv,'shandong_isolated':sv,'same_set':before,'allocation':balances,
                          'different_set':changes,'history_evidence':basis,**diagnostic()}
        except Exception:
            out_data={'passed':False,'checks':checks,'error':traceback.format_exc(),**diagnostic()}
        (out/'report.json').write_text(json.dumps(out_data,ensure_ascii=False,indent=2),encoding='utf-8')
        win._discard_close=True;win.job_dirty=False;win.close()
    QTimer.singleShot(160,run)
