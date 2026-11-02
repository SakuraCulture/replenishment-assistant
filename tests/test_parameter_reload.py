"""New imports adopt durable defaults without discarding intentional job edits."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import engine
import parameter_schema as schema
import store_parameter_import as spi
import work_repository as wr
from PySide6.QtWidgets import QApplication
from workbench import Workbench


def loaded():
    rows = []
    for code in ('A', 'B'):
        d = dict(A=code, B=code, D='测试商品', E='1001', H=2, I=14, J=60,
                 K=3, L=0, N=0, O=1, V=8, S='启用', AU='山东仓', _source='资料匹配')
        m = dict(export_row=2, product_row=2, match='商品编码精确匹配', blocking=[],
                 notes=[], category='休闲食品', export_status='正常', export_updated=None)
        rows.append((d, m))
    return dict(normalized=rows, config=engine.config(), stores={'A':'测试A','B':'测试B'},
                blocked={}, hashes={}, intake={'files':[], 'conflicts':[], 'duplicates':0, 'products':''})


def record(depth=6):
    value = spi.base()
    value['ui'].update(mode=1, primary='山东仓', secondary=None)
    c = value['preset']['config']
    c['warehouses'] = {wh:list(cycle) for wh,cycle in c['warehouses'].items()}
    c['warehouses']['山东仓'] = [depth, 3.5]
    c['profile_parameters'] = {'快销': {'moq_days':7, 'moq_price':10, 'probability':.95},
                               '日百': {'moq_days':12, 'moq_price':6, 'probability':.9}}
    c['weight_profiles']['快销'] = dict(alpha=.5, w1=.7, w2=.2)
    return value


class ParameterReloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, REPLENISHMENT_DATA_DIR=self.tmp.name)
        self.env.start()
        self.win = Workbench()
    def tearDown(self):
        import shiboken6
        self.win._discard_close = True
        self.win.close(); self.app.processEvents(); shiboken6.delete(self.win)
        self.env.stop(); self.tmp.cleanup()
    def inject(self, **records):self.win.work_repository.save_stores(records)
    def load(self):self.win.loaded_ok(loaded())

    def test_fresh_import_and_worker_settings_use_saved_values(self):
        value = record(); self.inject(A=value); self.load()
        self.assertEqual(self.win.config_for('A'), value['preset']['config'])
        self.assertEqual(self.win.selected_settings('A')['parameter_config'], value['preset']['config'])
        self.assertEqual(self.win.selected_settings('A')['warehouses'], value['preset']['config']['warehouses'])
        self.assertNotIn('profile_parameters', self.win.config_for('B'))

    def test_injection_after_initial_check_takes_effect_on_recheck(self):
        self.load(); before = self.win.config_for('A')
        value = record(); self.inject(A=value); self.load()
        self.assertNotEqual(before, value['preset']['config'])
        self.assertEqual(self.win.config_for('A'), value['preset']['config'])
        self.assertEqual(self.win.store_ui('A')['mode'], 1)
        self.assertIn('1 店采用已保存默认', self.win.import_note.text())
        self.load(); self.assertEqual(self.win.config_for('A'), value['preset']['config'])

    def test_updated_default_refreshes_inherited_config_but_keeps_local_budget_selection(self):
        self.inject(A=record(6)); self.load()
        ui = self.win.store_ui('A'); ui.update(mode=2, amount='3000', selected=False)
        self.win.set_store_ui('A', ui)
        self.inject(A=record(9)); self.load()
        self.assertEqual(self.win.config_for('A')['warehouses']['山东仓'], [9, 3.5])
        self.assertEqual(self.win.store_ui('A'), ui)

    def test_explicit_current_parameter_edits_survive(self):
        self.inject(A=record(6)); self.load()
        own = self.win.config_for('A'); own['parameters']['cap_days'] = 18
        self.win.apply_parameters(['A'], own, self.win.preset_for('A'))
        self.inject(A=record(9)); self.load()
        self.assertEqual(self.win.config_for('A'), own)
        self.assertIn('保留本次调整', self.win.import_note.text())

    def test_explicit_route_and_parameter_edits_are_independent(self):
        self.inject(A=record(6)); self.load()
        ui = self.win.store_ui('A'); ui.update(primary='河北仓', secondary='山东仓')
        self.win.set_store_ui('A', ui)
        self.inject(A=record(9)); self.load()
        self.assertEqual(self.win.store_ui('A'), ui)
        self.assertEqual(self.win.config_for('A')['warehouses']['山东仓'], [9, 3.5])

    def test_saving_current_as_default_then_external_update_refreshes(self):
        self.load(); value = record()
        self.win.apply_parameters(['A'], value['preset']['config'], value['preset'])
        self.win.set_store_ui('A', value['ui']); self.win.save_store_defaults(['A'], include_budget=True)
        self.inject(A=record(9)); self.load()
        self.assertEqual(self.win.config_for('A'), record(9)['preset']['config'])

    def test_restored_snapshot_kept_and_new_job_loads_latest(self):
        self.inject(A=record(6)); self.load()
        snapshot = self.win.snapshot()
        self.inject(A=record(9))
        self.win.restore_job({'day':wr.today(), 'saved':wr.now(), 'payload':snapshot})
        self.assertEqual(self.win.config_for('A'), record(6)['preset']['config'])
        self.load(); self.assertEqual(self.win.config_for('A'), record(6)['preset']['config'])
        self.win.reset_job(); self.load()
        self.assertEqual(self.win.config_for('A'), record(9)['preset']['config'])

    def test_explicit_copied_snapshot_survives_repeated_checks(self):
        self.inject(A=record(9))
        old = record(6)
        self.win.pending_copy = {'store_presets':{'A':old['preset']}, 'store_ui':{'A':old['ui']}}
        self.load(); self.load()
        self.assertEqual(self.win.config_for('A'), old['preset']['config'])

    def test_resolving_conflict_restores_selection_but_not_other_selection(self):
        self.inject(A=record()); blocked = loaded(); blocked['blocked']={'A':1}
        self.win.loaded_ok(blocked)
        self.win.store_widgets['B']['check'].setChecked(False)
        self.load(); self.assertEqual(self.win.selected_codes(), ['A'])


if __name__ == '__main__':unittest.main()
