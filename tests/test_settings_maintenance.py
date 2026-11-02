import sys, tempfile, unittest, copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import work_repository as wr
import parameter_schema as schema
import settings_history as history

class SettingsMaintenance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=wr.Repository(self.tmp.name)
        self.a=wr.loads(wr.dumps({'preset':self.repo.presets()[0],'ui':{'primary':'山东仓'},'saved':'before'}))
        self.b=copy.deepcopy(self.a);self.b['preset']['config']['parameters']['depth']=4
    def test_undo_import_preserves_later_change(self):
        self.repo.save_stores({'A':self.a,'B':self.a})
        self.repo.save_stores({'A':self.b,'B':self.b},action='批量导入门店参数')
        entry=history.history(self.repo)[0]
        c=copy.deepcopy(self.b);c['ui']['primary']='河北仓';self.repo.save_stores({'B':c})
        p=history.plan(self.repo,entry);self.assertEqual(p['conflicts'],['B'])
        history.apply(self.repo,p);self.assertEqual(self.repo.stores(),{'A':self.a,'B':c})
    def test_optimistic_conflict_rolls_back_all(self):
        self.repo.save_stores({'A':self.a,'B':self.a});p={'values':{'A':self.b,'B':self.b},'expected':{'A':self.a,'B':None},'action':'test'}
        with self.assertRaises(wr.Conflict):history.apply(self.repo,p)
        self.assertEqual(self.repo.stores(),{'A':self.a,'B':self.a});self.assertEqual(len(history.history(self.repo)),1)
    def test_first_import_undo_and_explicit_restore(self):
        self.repo.save_stores({'A':self.a});e=history.history(self.repo)[0]
        history.apply(self.repo,history.plan(self.repo,e));self.assertEqual(self.repo.stores(),{})
        history.apply(self.repo,history.plan(self.repo,e,restore=True));self.assertEqual(self.repo.stores(),{'A':self.a})
    def test_restore_preset_does_not_apply(self):
        p=self.repo.save_preset('试用',schema.standard());self.repo.archive_preset(p['id'],p['version'])
        self.assertEqual(len(self.repo.presets()),1)
        self.assertTrue(self.repo.presets(include_archived=True)[1]['archived'])
        self.repo.restore_preset(p['id'],p['version']);self.assertEqual(self.repo.presets()[1],wr.loads(wr.dumps(p)));self.assertEqual(self.repo.stores(),{})
    def test_diff_readable_and_null_distinct(self):
        from settings_maintenance_ui import diffs
        rows=diffs({'values':{'A':{'ui':{'amount':0}}},'expected':{'A':{'ui':{'amount':None}}}})
        self.assertEqual(rows,[['A','ui / amount',None,0]])

if __name__=='__main__':unittest.main()
