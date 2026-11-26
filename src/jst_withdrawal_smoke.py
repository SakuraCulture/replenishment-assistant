"""Exercise withdrawal and reallocation through the packaged Qt workbench."""
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QFileDialog, QLineEdit, QListWidget, QPushButton, QTableView
from types import SimpleNamespace
from unittest.mock import patch

import catalog_session_ui
import jst_auto_order as automatic
import jst_auto_order_store as tasks
import jst_order_store as allocations
import lifecycle_facts
import purchases
import warehouse_catalog
import work_repository as wr
from jst_auto_order_ui import Orders
from jst_ui import GenerationDialog
from order_redo_smoke import Exercise, button
from platform_import_manual_fixture import JstManual


class WithdrawalExercise(Exercise):
    def __init__(self, app, win, root, data):
        self.app, self.win, self.root = app, win, root
        self.path, self.phase = win.work_repository.path, 'withdrawal'
        self.data, self.rows = data, data['rows']
        self.heads = {h['number']:h for h in purchases.targets(self.path)}
        self.portal = JstManual(root/'platform')
        self.shots = []
        for number, row in self.rows.items():
            if row['outcome'] in ('platform','cancelled'):
                with closing(wr.read_connection(self.path)) as db:
                    order = tasks.read(db,row['online'])
                self.portal.accepted(order,'17081' if row['outcome']=='platform' else '17082')
                if row['outcome']=='cancelled':
                    self.portal.orders[row['online']].update(native_status='已取消',status='review')

    def active_reservations(self):
        with closing(wr.read_connection(self.path)) as db:
            result = {}
            for number, head in self.heads.items():
                gid = allocations.active(db,head)
                reserved = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=? ORDER BY sku',(gid,)))
                result[number] = {'gid':gid, 'reserved':reserved}
            return result

    def select_rows(self,page,numbers):
        checked = Qt.CheckState.Checked
        page.clear_selection()
        for i in range(page.table.topLevelItemCount()):
            group = page.table.topLevelItem(i)
            group.setExpanded(True)
            for j in range(group.childCount()):
                item = group.child(j)
                if item.text(0).split(' · ',1)[1] in numbers:
                    item.setCheckState(0,checked)
        assert len(page.chosen)==len(numbers), (page.chosen,numbers)

    def withdrawal_dialog(self,page,numbers,name,accept=False,attest=()):
        self.select_rows(page,numbers)
        evidence = {}
        def tools(dialog):
            def review(d):
                model = d.findChild(QTableView).model()
                if attest:
                    def attest_scope(dialog):
                        options = dialog.findChild(QListWidget)
                        options_found = set()
                        for i in range(options.count()):
                            item = options.item(i)
                            number = item.text().split(' · ',1)[0]
                            options_found.add(number)
                            if number in attest:
                                item.setCheckState(Qt.CheckState.Checked)
                        assert options_found==numbers
                        entry = {field.accessibleName():field for field in dialog.findChildren(QLineEdit)}
                        entry['核对人'].setText('隔离验收经办人')
                        entry['未发送核对依据'].setText('本人停在平台预览，已核对未点最终确认且无其他渠道上传')
                        add = button(dialog,'加入撤销清单')
                        assert not add.isEnabled()
                        dialog.findChild(QCheckBox).setChecked(True)
                        assert add.isEnabled()
                        add.click()
                    self.dialog(button(d,'补记未发送凭据').click,'补记最终导入未发送',attest_scope)
                record = model.rows
                assert set(numbers)=={r[0] for r in record}, record
                evidence.update({r[0]:list(r[1:]) for r in record})
                approved = [row for row in record if row[4]=='可撤销']
                accept_buttons = [b for b in d.findChildren(QPushButton)
                                  if b.isVisible() and b.text().startswith('确认撤销 ')]
                assert len(accept_buttons)==bool(approved)
                QTest.qWait(60)
                self.shot(d,name)
                if accept:
                    assert approved and accept_buttons[0].isEnabled()
                    assert d.findChild(QLineEdit).text()=='按最新库存重新分配'
                    accept_buttons[0].click()
            self.dialog(button(dialog,'撤销未下单生成单').click,'撤销未下单生成单',review)
        self.dialog(page.tools_btn.click,'其他订单操作',tools)
        self.wait(lambda:not page.reading and not page.reviewing,'刷新撤销结果')
        return evidence

    def browser_redo(self,page,number):
        def tasks_dialog(dialog):
            self.task_selection(dialog,self.rows[number]['online'])
            self.confirmation(dialog.redo_btn.trigger,'确认沿原仓单重做','17082')
        self.dialog(page.auto_tasks_btn.click,Orders,tasks_dialog)
        self.wait(lambda:not page.reading and page.scope and len(page.records)==1,'返回重做原仓单')
        page.clear_scope()
        self.wait(lambda:not page.reading and len(page.records)==len(self.rows),'显示两仓')

    def regenerate(self,page,numbers):
        self.select_rows(page,numbers)
        result = {}
        def generation(d):
            self.wait(lambda:bool(d.catalogs),'初始仓库存')
            for wh in self.data['fresh_stock']:
                d.warehouse.setCurrentText(wh)
                fixture = str(self.root/(wh+'-最新库存.xlsx'))
                with patch.object(QFileDialog,'getOpenFileName',return_value=(fixture,'Excel (*.xlsx)')):
                    d.update_btn.click()
                assert Path(d.catalogs[wh]['catalog']['source_path']).name==Path(fixture).name
            d.preview_btn.click()
            value = d.plan['payload']
            result['batch_id'] = value['batch_id']
            result['other_reserved'] = value['reserved']
            result['stock'] = {wh:{'revision':b['catalog']['revision'],'file':b['catalog']['source_path'],
                                  'sha256':b['catalog']['source_sha256']}
                               for wh,b in value['new_batch']['stocks'].items()}
            result['preview'] = {p['head']['number']:{'warehouse':p['head']['warehouse'],
                     'allocated':p['rows'][0]['quantity'],'gid':p['gid']} for p in value['plans']}
            assert set(result['preview'])==numbers and not value['skipped']
            for number, r in result['preview'].items():
                assert r['allocated']==self.data['expected_new'][r['warehouse']][self.rows[number]['outcome']], result
            self.shot(d,'按仓更新后分配预览')
            self.confirmation(d.generate_btn.click,'确认生成聚水潭订单',str(len(numbers))+'单')
            self.wait(lambda:d.worker is None and d.receipt is not None,'确认生成新批')
            assert len(d.receipt['generated'])==len(numbers) and not d.receipt['errors'], d.receipt
            self.shot(d,'确认生成结果')
        self.dialog(page.prepare_btn.click,GenerationDialog,generation)
        return result

    def run(self):
        by_outcome=lambda outcome:{n for n,r in self.rows.items() if r['outcome']==outcome}
        unknown = by_outcome('unknown')
        eligible = by_outcome('unsent') | by_outcome('rejected')
        cancelled, = by_outcome('cancelled')
        native, = by_outcome('platform')
        after_release = eligible | {cancelled}
        operator_scope = {min(n for n in unknown if self.rows[n]['warehouse']==wh)
                          for wh in self.data['fresh_stock']}
        selected = set(self.rows)-by_outcome('control')
        report = {}
        def exercise(page):
            before = self.active_reservations()
            assert all(s['reserved'] for s in before.values())
            report['before'] = before
            report['unknown_only'] = self.withdrawal_dialog(page,unknown,'十张无回执')
            assert all(row[3]=='证据不足或待处理' and '精确查询 0 张' in row[-1] and '终止凭据' in row[-1]
                       for row in report['unknown_only'].values())
            assert self.active_reservations()==before
            report['mixed_cancel'] = self.withdrawal_dialog(page,selected,'两仓混选确认清单')
            assert {n for n,r in report['mixed_cancel'].items() if r[3]=='可撤销'}==eligible
            assert report['mixed_cancel'][native][3]=='需先取消平台原单'
            assert report['mixed_cancel'][cancelled][3]=='已取消，转原单重做'
            assert self.active_reservations()==before
            report['mixed_confirm'] = self.withdrawal_dialog(page,selected,'两仓混选提交前',accept=True)
            current = self.active_reservations()
            assert all(current[n]['gid'] is None for n in eligible)
            assert all(current[n]==before[n] for n in before if n not in eligible)
            report['after_withdraw'] = current
            report['repeat'] = self.withdrawal_dialog(page,eligible,'重复撤销结果')
            assert all('已撤销' in row[-1] for row in report['repeat'].values())
            assert self.active_reservations()==current
            def old_task(d):
                chosen = next(iter(sorted(eligible)))
                self.task_selection(d,self.rows[chosen]['online'])
                assert not d.buttons['submit'].isEnabled() and not d.buttons['preview'].isEnabled()
                assert not d.redo_btn.isEnabled() and d.export.isEnabled()
                self.shot(d,'历史原单导入已失效')
            self.dialog(page.auto_tasks_btn.click,Orders,old_task)
            self.wait(lambda:not page.reading,'返回分配页')
            self.browser_redo(page,cancelled)
            current = self.active_reservations()
            assert all(current[n]['gid'] is None for n in after_release)
            assert all(current[n]==before[n] for n in before if n not in after_release)
            report['after_cancelled_redo'] = current
            report['new_batch'] = self.regenerate(page,after_release)
            current = self.active_reservations()
            assert all(current[n]['gid']==r['gid'] and list(current[n]['reserved'].values())==[r['allocated']]
                       for n,r in report['new_batch']['preview'].items())
            assert all(current[n]==before[n] for n in before if n not in after_release)
            report['after_regenerate'] = current
            report['operator_attestation'] = self.withdrawal_dialog(page,unknown,'原经办人批量补记未发送',
                                                                    accept=True,attest=operator_scope)
            observed = report['operator_attestation']
            assert {n for n,r in observed.items() if r[3]=='可撤销'}==operator_scope
            assert all('业务核实' in observed[n][-1] for n in operator_scope)
            after_operator = self.active_reservations()
            assert all(after_operator[n]['gid'] is None for n in operator_scope)
            assert all(after_operator[n]==current[n] for n in current if n not in operator_scope)
            report['after_operator_withdraw'] = after_operator
        with patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=self.portal)):
            self.enter('聚水潭导入单','jst',exercise)
        report['retained'] = {}
        with closing(wr.read_connection(self.path)) as db:
            for number, row in self.rows.items():
                h = self.heads[number]
                assert lifecycle_facts.current(db,h,'payment') is None
                assert lifecycle_facts.current(db,h,'receipt') is None
                old = tasks.read(db,row['online'],workbook=True)
                assert old['file_sha']==row['file_sha']
                assert allocations.read_generation(db,row['gid'])[0]['gid']==row['gid']
                assert bool(old.get('historical'))==(number in after_release | operator_scope)
                report['retained'][number] = {'stage':old['stage'],'sequence':old['sequence'],'file_sha':old['file_sha']}
                if number in eligible | operator_scope:
                    proof = old['proof']['withdrawal']
                    assert old['stage']=='WITHDRAWN' and proof['reserved']==report['before'][number]['reserved']
                    assert proof['lookup']['exact_absence'] and proof['initial_lookup']['exact_absence']
                    if number in operator_scope:
                        assert proof['ending']['kind']=='业务核实未发送' and proof['ending']['operator']=='隔离验收经办人'
                    report['retained'][number]['proof'] = proof
        ready = automatic.prepare(self.path,[
            {'head':self.heads[n],'gid':report['after_regenerate'][n]['gid']} for n in sorted(after_release)])
        assert len(ready['ready'])==len(after_release) and not ready['failed'], ready
        assert not set(ready['ready']) & {r['online'] for r in self.rows.values()}
        report['new_online'] = ready['ready']
        report['fixture'] = self.rows
        report['calls'] = self.portal.calls
        report['screenshots'] = self.shots
        assert {a for a,_,_ in self.portal.calls}=={'order.lookup'}
        assert sum(a=='order.lookup' for a,_,_ in self.portal.calls)==53, self.portal.calls
        return report
