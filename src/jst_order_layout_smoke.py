"""Visible Windows acceptance of small-window automatic order operations."""
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QPoint, QRect, QTimer, Qt
from PySide6.QtTest import QTest

import catalog_session_ui
from catalog_sync_client import ComponentError
from jst_auto_order_store import read
from jst_auto_order_ui import Orders
from jst_order_dialogs import ImportConfirmation, JobDetails
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

        with patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=self.portal)):
            self.enter('聚水潭导入单','jst',
                       lambda page:self.dialog(lambda:self.click(page.auto_tasks_btn),Orders,on_orders))
        with closing(wr.read_connection(self.path)) as db:
            after={k:read(db,k) for k in keys}
            assert {k for k,r in after.items() if r['internal']}==set(result['imports'])
            assert all(after[k]['file_sha']==self.fixture['rows'][k]['file_sha'] for k in keys)
        result['count']=count
        result['screenshots']=self.shots
        result['calls']=self.portal.calls
        return result
