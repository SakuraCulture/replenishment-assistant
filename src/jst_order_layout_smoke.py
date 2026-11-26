"""Visible Windows acceptance of small-window automatic order operations."""
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QPoint, QRect, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QTableView

import catalog_session_ui
from catalog_sync_client import ComponentError
from jst_auto_order_store import read
from jst_auto_order_ui import Orders
from jst_order_store import active
from jst_order_dialogs import ImportConfirmation, JobDetails
from jst_results import key as scope_key
from jst_ui import GenerationDialog
from order_redo_smoke import Exercise, button
from platform_import_manual_fixture import JstManual
import work_repository as wr

LONG_REASON = (
    '聚水潭预览校验未通过。请核对原仓企业、平台店铺和原订单商品明细。\n'
    '第 1 项：原文件的商品编码与店铺有效商品尚未完成一一匹配。\n'
    '第 2 项：核对收货资料、销售计量单位和平台预览里的折合数量。\n'
    '第 3 项：本次查询回查到原线上单号，保留原任务供业务复核。\n'
    '第 4 项：请保留原文件及本次预览提示，逐项核查商品行后再继续。\n'
    '第 5 项：补充校验表的仓库范围、商品来源、数量、金额和参考批次。\n'
    '第 6 项：检查本次所选仓单的数量及商品状态，联系原作业负责人核对。\n'
    '完整原因结尾标记 ORD-LAYOUT-ENDING。'
)


class Portal(JstManual):
    def acquire_wait(self,warehouses,cancel,progress):
        return self.acquire()

    def order(self,action,spec,args):
        if action=='order.preview' and spec['online'] in self.layout_errors:
            raise ComponentError('PREVIEW_CHANGED',LONG_REASON)
        return super().order(action,spec,args)


def rect_values(rect):
    return [rect.x(),rect.y(),rect.width(),rect.height()]


class LayoutExercise(Exercise):
    def __init__(self,app,win,root,fixture):
        self.app,self.win,self.root = app,win,root
        self.path = win.work_repository.path
        self.fixture = fixture
        self.phase = 'layout'
        self.portal = Portal(root/'platform')
        self.portal.layout_errors = set(fixture['errors'])
        self.shots = []

    def click(self,widget):
        assert widget.isVisible() and widget.isEnabled(), widget.text()
        QTest.mouseClick(widget,Qt.MouseButton.LeftButton)

    def small(self,dialog):
        dialog.resize(720,440)
        area=dialog.screen().availableGeometry()
        dialog.move(area.topLeft()+QPoint(30,42))
        QTest.qWait(70)
        assert (dialog.width(),dialog.height())==(720,440), rect_values(dialog.geometry())
        assert area.contains(dialog.frameGeometry()), (rect_values(area),rect_values(dialog.frameGeometry()))
        assert abs(dialog.devicePixelRatioF()-1.25)<0.001, dialog.devicePixelRatioF()

    def footer(self,dialog,controls):
        rects={}
        for control in controls:
            r=QRect(control.mapTo(dialog,QPoint()),control.size())
            assert control.isVisible() and dialog.rect().contains(r), (control.text(),rect_values(r))
            rects[control.text()]=rect_values(r)
        return rects

    def scroll(self,table,count):
        assert table.model().rowCount()==count and table.viewport().height()>=99
        rail=table.verticalScrollBar()
        assert (rail.maximum()>0)==(count>1), (count,rail.maximum())
        if count>1:
            table.scrollToBottom()
            index=table.model().index(count-1,0)
            assert rail.value()==rail.maximum() and table.visualRect(index).intersects(table.viewport().rect())
        return rail.maximum()

    def scroll_right(self,table):
        rail=table.horizontalScrollBar()
        assert rail.maximum()>0
        rail.setValue(rail.maximum())
        model=table.model()
        index=model.index(0,model.columnCount()-1)
        assert table.visualRect(index).right()<=table.viewport().width()
        assert table.visualRect(index).right()>0
        rail.setValue(0)
        return rail.maximum()

    def open_more(self,window):
        activated=[]
        problem=[]
        def verify():
            menu=window.more_menu
            try:
                assert menu.isVisible(), '更多操作菜单未打开'
                probe=window.buttons['probe']
                assert probe.isEnabled() and probe.text()=='核对平台账号和店铺'
                self.shot(menu,'更多操作菜单')
                QTest.mouseClick(menu,Qt.MouseButton.LeftButton,pos=menu.actionGeometry(probe).center())
                activated.append(True)
            except Exception as exc:
                problem.append(str(exc))
            finally:
                menu.close()
        QTimer.singleShot(80,verify)
        self.click(window.more_btn)
        self.wait(lambda:bool(activated or problem),'更多操作菜单')
        assert activated and not problem,problem
        assert any(a=='order.probe' for a,_,_ in self.portal.calls)

    def details(self,window,expected):
        detail={}
        def read_dialog(d):
            self.small(d)
            count=self.fixture['count']
            detail['scroll_max']=self.scroll(d.table,count)
            detail['buttons']=self.footer(d,(d.copy_btn,d.close_btn))
            if count>=60:
                self.shot(d,'作业明细_滚动到底')
            one=expected[2]
            term='ORD-LAYOUT-ENDING' if one in self.portal.layout_errors else one
            d.search.setText(term)
            QTest.qWait(60)
            assert d.model.rowCount()==1, d.model.rowCount()
            assert [d.model.data(d.model.index(0,i)) for i in range(3)]==list(expected[:3])
            assert d.reason.toPlainText()==expected[-1], repr(d.reason.toPlainText())
            if one in self.portal.layout_errors:
                bar=d.reason.verticalScrollBar()
                assert bar.maximum()>0
                bar.setValue(bar.maximum())
            saved=QMimeData()
            clipboard=self.app.clipboard()
            formats=clipboard.mimeData()
            if formats:
                for f in formats.formats():
                    saved.setData(f,formats.data(f))
            try:
                self.click(d.copy_btn)
                copied=clipboard.text()
                assert all(value in copied for value in (*expected[:3],expected[-1]))
            finally:
                clipboard.setMimeData(saved)
            detail.update(search=term,full_reason=expected[-1],searched_key=one,copied=True)
            self.shot(d,'查看与复制完整原因')
            self.click(d.close_btn)
        self.dialog(lambda:self.click(window.details_btn),JobDetails,read_dialog)
        return detail

    def confirmation_grid(self,window,expected,accepted):
        seen={}
        def review(d):
            self.small(d)
            assert [row[2] for row in d.model.rows]==expected
            seen['keys']=[row[2] for row in d.model.rows]
            seen['horizontal_scroll_max']=self.scroll_right(d.table)
            seen['scroll_max']=self.scroll(d.table,len(expected))
            seen['buttons']=self.footer(d,(d.cancel_btn,d.confirm_btn))
            assert d.cancel_btn.isDefault()
            self.shot(d,'确认导入清单_滚动到底' if accepted else '确认导入清单_取消前')
            self.click(d.confirm_btn if accepted else d.cancel_btn)
        self.dialog(lambda:self.click(window.buttons['submit']),ImportConfirmation,review)
        return seen

    def redo_batch(self,page,imported):
        def snapshot():
            with closing(wr.read_connection(self.path)) as db:
                tasks = {k:read(db,k) for k in imported}
                stock = {k:active(db,o['payload']['head']) for k,o in tasks.items()}
            return tasks, stock

        original, assigned = snapshot()
        ready = {next(k for k in imported if original[k]['payload']['head']['warehouse']==wh)
                 for wh in ('河北仓','山东仓')}
        pending, excluded = [k for k in imported if k not in ready][:2]
        selected = ready | {pending}
        for k in ready | {excluded}:
            self.portal.orders[k].update(native_status='已取消',status='review')
        first_call = len(self.portal.calls)

        def run_orders(w):
            self.small(w)
            for index,row in enumerate(w.records):
                w.table.item(index,0).setCheckState(Qt.CheckState.Checked if row['online'] in selected else Qt.CheckState.Unchecked)
            assert w.batch_redo_btn in w.more_menu.actions() and w.batch_redo_btn.isEnabled()
            assert not w.redo_btn.isEnabled()

            def confirm(accept):
                def review(d):
                    self.small(d)
                    model = d.findChild(QTableView).model()
                    assert {r[5] for r in model.rows}==selected
                    assert {r[5] for r in model.rows if r[2]=='可登记'}==ready
                    assert all(r[4]==original[r[5]]['internal'] for r in model.rows)
                    assert '尚未核实' in next(r[3] for r in model.rows if r[5]==pending)
                    cancel = button(d,'取消')
                    go = button(d,'确认登记 2 张')
                    assert cancel.isDefault()
                    self.footer(d,(cancel,go))
                    self.shot(d,'批量登记核对清单' if accept else '批量登记_取消前')
                    self.click(go if accept else cancel)
                self.dialog(w.batch_redo_btn.trigger,'批量登记重做 · 核对结果',review)

            confirm(False)
            tasks, stock = snapshot()
            assert stock==assigned and not any(tasks[k].get('historical') for k in ready)
            assert w.isVisible()

            def details(d):
                assert w.note.text()=='已登记 2 张 · 待处理 1 张'
                assert {r[2] for r in d.model.rows if r[3]=='已登记'}==ready
                assert {r[2] for r in d.model.rows if r[3]=='待处理'}=={pending}
                self.shot(d,'批量登记作业结果')
                self.click(d.close_btn)
            self.dialog(lambda:confirm(True),JobDetails,details)
        self.dialog(lambda:self.click(page.auto_tasks_btn),Orders,run_orders)

        expected = {scope_key(original[k]['payload']['head']) for k in ready}
        self.wait(lambda:not page.reading and {scope_key(r['head']) for r in page.records}==expected,'返回成功仓单')
        assert page.chosen==expected and {scope_key(h) for h in page.scope}==expected
        assert page.prepare_btn.isEnabled() and '已返回 2 张重做仓单' in page.status.text()
        self.shot(page,'两仓同时返回分配页')
        tasks, stock = snapshot()
        assert all(tasks[k]['historical'] and stock[k] is None and tasks[k]['stage']=='CANCELLED' for k in ready)
        assert all(tasks[k]==original[k] and stock[k]==assigned[k] for k in set(imported)-ready)
        lookups = self.portal.calls[first_call:]
        expected_lookups = {('order.lookup',original[k]['payload']['head']['warehouse'],k) for k in selected}
        assert len(lookups)==2*len(selected) and set(lookups)==expected_lookups,lookups
        return {'registered':sorted(ready),'pending':[pending],'unselected_cancelled':excluded,
                'cancel_before_confirm_left_allocations':True,'platform_requests':lookups,
                'returned_warehouses':sorted(h['warehouse'] for h in page.scope)}

    def fresh_stock(self,page,imported,redo):
        from jst_orders import lock

        page.clear_scope()
        self.wait(lambda:not page.reading and page.scope is None,'返回全部仓单')
        with closing(wr.read_connection(self.path)) as db:
            originals={k:read(db,k) for k in imported if k not in redo['registered']}
        wh_keys={wh:[k for k,o in originals.items() if o['payload']['head']['warehouse']==wh]
                 for wh in ('河北仓','山东仓')}
        picked=wh_keys['河北仓'][:2]+wh_keys['山东仓'][:1]
        other=wh_keys['河北仓'][2]
        selected={scope_key(originals[k]['payload']['head']) for k in picked}
        assert len(selected)==3
        lock(self.path,[originals[picked[0]]['gid']],'合成历史下单标记')
        page.chosen=selected
        page.render()
        page.controls()
        self.portal.stock={'河北仓':3,'山东仓':1}
        first_call=len(self.portal.calls)
        evidence={}

        def allocate(d):
            self.click(d.preview_btn)
            assert d.plan is not None,d.status.text()
            p=d.plan['payload']
            assert {scope_key(item['head']) for item in p['plans']}==selected
            assert p['reserved']=={'河北仓':{},'山东仓':{}}
            totals={wh:sum(Decimal(item['summary']['quantity']) for item in p['plans'] if item['head']['warehouse']==wh)
                    for wh in self.portal.stock}
            assert totals==self.portal.stock,totals
            evidence['quantities']={wh:str(q) for wh,q in totals.items()}
            self.shot(d,'重新分配_双仓最新库存预览')
            def confirm_allocation(confirm):
                assert confirm.findChild(QTableView).model().rowCount()==3
                self.click(button(confirm,'确认生成'))
            self.dialog(lambda:self.click(d.generate_btn),'确认本次库存分配',confirm_allocation)
            self.wait(lambda:d.receipt is not None and not d.worker,'保存本次分配')
            assert not d.receipt['errors'] and len(d.receipt['generated'])==3,d.receipt
            self.shot(d,'重新分配_本次已保存')
            self.click(button(d,'关闭'))
        self.dialog(lambda:self.click(page.reallocate_btn),GenerationDialog,allocate)
        self.wait(lambda:not page.reading and all(r['gid'] for r in page.records if scope_key(r['head']) in selected),'读取新分配')
        with closing(wr.read_connection(self.path)) as db:
            for k in picked:
                original=originals[k]
                head=original['payload']['head']
                assert active(db,head) not in (None,original['gid'])
                assert read(db,k)==original
            assert active(db,originals[other]['payload']['head'])==originals[other]['gid']
        calls=self.portal.calls[first_call:]
        assert sorted(calls)==[('sync.start',('山东仓',)),('sync.start',('河北仓',))],calls
        return {**evidence,'selected':picked,'untouched_control':other,'platform_requests':calls,
                'original_orders_unchanged':True}

    def operations_update(self,imported,stock_result):
        import openpyxl
        import order_identity
        import order_lifecycle as lifecycle
        from jst_order_store import locked
        from lifecycle_batch_ui import BatchImport
        from lifecycle_home_ui import StageDialog

        unaffected=[k for k in imported if k not in stock_result['selected']
                    and self.portal.orders[k]['native_status']!='已取消']
        with closing(wr.read_connection(self.path)) as db:
            original, control = [read(db,k) for k in unaffected[:2]]
            head = original['payload']['head']
            control_head = control['payload']['head']
            assert original['stage']=='IMPORTED' and active(db,head)==locked(db,head)==original['gid']
            assert active(db,control_head)==control['gid']
            document=lifecycle.current(db,head,'operations')
        row=next(r for r in document['payload']['rows'] if Decimal(r['base_quantity'])>0)
        new_qty=int(Decimal(row['base_quantity']))+1
        exported=lifecycle.export(self.path,[head],self.root,'operations')
        assert len(exported['files'])==1 and not exported['error'],exported
        source=self.root/'验收运营签发改量.xlsx'
        with closing(openpyxl.load_workbook(exported['files'][0])) as book:
            # The shared fixture renames stores after copying one source store's rows.
            visible=[(sheet,i) for sheet in book if sheet.title!=order_identity.SHEET
                     for i in range(2,sheet.max_row+1) if sheet.cell(i,5).value is not None]
            for sheet,i in visible:
                sheet.cell(i,2).value=head['store']
            (sheet,i),=[(s,n) for s,n in visible if str(s.cell(n,5).value)==row['sku']]
            assert Decimal(str(sheet.cell(i,17).value))==Decimal(row['base_quantity'])
            sheet.cell(i,17).value=new_qty
            book.save(source)

        first_call=len(self.portal.calls)
        def operations(dialog):
            assert dialog.stage=='operations'
            page=dialog.panel
            self.wait(lambda:getattr(page,'applied_request',None) is not None,'运营签发')
            assert len(page.checked)==1
            def import_file(window):
                assert window.stage=='operations'
                with patch.object(QFileDialog,'getOpenFileNames',return_value=([str(source)],'')):
                    self.click(button(window,'选择文件'))
                assert window.read_ready and not window.issues,window.issues
                self.click(window.preview_btn)
                plan,=window.plan['plans']
                assert plan['stage']=='operations' and plan['changed']
                assert next(r['base_quantity'] for r in plan['payload']['rows'] if r['sku']==row['sku'])==str(new_qty)
                QTest.qWait(60)
                self.shot(window,'已下单仓单_运营更新核对')
                window.review.complete.setChecked(True)
                window.review.extra_ack.setChecked(True)
                self.click(window.review.save)
                saved,=window.receipt
                assert saved['changed'] and saved['version']==document['version']+1
            self.dialog(lambda:self.click(button(page,'导入运营签发单')),BatchImport,import_file)
        self.dialog(lambda:self.win.followup.open_stage('operations',[head]),StageDialog,operations)

        with closing(wr.read_connection(self.path)) as db:
            new=lifecycle.current(db,head,'operations')
            assert new['version']==document['version']+1
            assert active(db,head) is None and active(db,control_head)==control['gid']
            assert read(db,original['online'])==original
        exported=lifecycle.export(self.path,[head],self.root,'operations')
        assert len(exported['files'])==1 and not exported['error']
        with closing(openpyxl.load_workbook(exported['files'][0],read_only=True,data_only=True)) as book:
            quantities=[values[16] for sheet in book if sheet.title!=order_identity.SHEET
                        for values in sheet.iter_rows(min_row=2,values_only=True) if str(values[4])==row['sku']]
        assert quantities==[new_qty]
        assert not self.portal.calls[first_call:]
        return {'warehouse':head['warehouse'],'order':head['number'],'old_qty':row['base_quantity'],
                'exported_qty':new_qty,'old_version':document['version'],'saved_version':new['version'],
                'old_allocation_invalidated':True,'unselected_unchanged':True,
                'original_order_unchanged':True,'platform_requests':[]}

    def run(self):
        count=self.fixture['count']
        keys=self.fixture['jst_keys']
        ready=self.fixture['ready']
        result={}
        def on_orders(w):
            self.small(w)
            result['window']={'logical_size':[w.width(),w.height()],
                              'screen':rect_values(w.screen().availableGeometry()),
                              'device_pixel_ratio':w.devicePixelRatioF()}
            footer=tuple(w.buttons[k] for k in ('preview','submit','recover'))+(w.close_btn,)
            before=self.footer(w,footer)
            initial_height=w.table.viewport().height()
            assert w.table.rowCount()==count
            self.click(button(w,'勾选当前列表'))
            assert [r['online'] for r in w.selected()]==keys
            if count==1:
                self.open_more(w)
            self.click(w.buttons['preview'])
            expected_previewed=count-len(self.portal.layout_errors)
            assert w.note.text()==f'已预览 {expected_previewed} 张 · 可确认导入 {len(ready)} 张 · {count-len(ready)} 张待处理',w.note.text()
            assert initial_height==w.table.viewport().height()
            assert len(w.job_rows)==count and not w.note.wordWrap()
            result.update(summary=w.note.text(),footer=before,table_height=initial_height,
                          order_horizontal_max=self.scroll_right(w.table),
                          order_scroll_max=self.scroll(w.table,count))
            assert self.footer(w,footer)==before
            self.shot(w,'主窗口_滚动到底')
            w.table.scrollToTop()
            self.shot(w,'主窗口_预览结果')
            if count==27:
                w.resize(1280,580)
                QTest.qWait(65)
                self.footer(w,footer)
                self.shot(w,'主窗口_常用尺寸')
                self.small(w)
            target=self.fixture['errors'][-1] if self.fixture['errors'] else next(
                (key for key in reversed(keys) if key not in ready),keys[0])
            detail=next(row for row in w.job_rows if row[2]==target)
            if self.portal.layout_errors:
                assert detail[-1].endswith(LONG_REASON)
                assert 'ORD-LAYOUT-ENDING' in detail[-1]
            elif count>1:
                assert '上次发送结果仍待核实' in detail[-1]
            result['details']=self.details(w,detail)
            excluded=None
            if count>=60:
                excluded=ready[0]
                row=next(i for i,r in enumerate(w.records) if r['online']==excluded)
                w.table.item(row,0).setCheckState(Qt.CheckState.Unchecked)
            eligible=[k for k in ready if k!=excluded]
            result['unselected_ready']=excluded
            result['confirmation_cancel']=self.confirmation_grid(w,eligible,False)
            assert not any(a=='order.import' for a,_,_ in self.portal.calls)
            result['confirmation_done']=self.confirmation_grid(w,eligible,True)
            imports=[key for a,_,key in self.portal.calls if a=='order.import']
            assert imports==eligible,(imports,eligible)
            result['imports']=imports
            assert self.footer(w,footer)==before
            self.click(w.buttons['recover'])
            assert [key for a,_,key in self.portal.calls if a=='order.import']==eligible
            assert '原单' in w.note.text()
            self.click(w.close_btn)

        def results_page(page):
            self.dialog(lambda:self.click(page.auto_tasks_btn),Orders,on_orders)
            if count==27:
                result['batch_redo']=self.redo_batch(page,result['imports'])
                result['fresh_stock']=self.fresh_stock(page,result['imports'],result['batch_redo'])
        with patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=self.portal)):
            self.enter('聚水潭导入单','jst',results_page)
            if count==27:
                result['operations_update']=self.operations_update(result['imports'],result['fresh_stock'])
        with closing(wr.read_connection(self.path)) as db:
            after={k:read(db,k) for k in keys}
            assert {k for k,r in after.items() if r['internal']}==set(result['imports'])
            assert all(after[k]['file_sha']==self.fixture['rows'][k]['file_sha'] for k in keys)
        result['count']=count
        result['screenshots']=self.shots
        result['calls']=self.portal.calls
        return result
