"""Optional names travel with full data; additive order merge keeps local names."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests')]
import merge_fixture as f
import payment_shop_names as names,order_migration as full,order_merge_migration as merge

def save(order,value):
    names.save(order.path,names.prepare(names.load(order.path),[{'store':order.code,'warehouse':'河北仓','value':value}]))

class NamesMigration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=f.Order(self.root/'source','SOURCE');self.target=f.Order(self.root/'target','LOCAL')
        save(self.source,'迁入河北店名');save(self.target,'本机河北店名')
    def test_full_archive_carries_names_and_legacy_missing_names_still_imports(self):
        package=self.root/'full.zip';self.source.export(package);expected=names.load(self.source.path)['records'];plan=full.prepare(self.target.root,package)
        with patch('application_reset._check_other_processes'):result=full.apply(plan)
        self.assertEqual(result['state'],'COMPLETED');self.assertEqual(names.load(self.target.path)['records'],expected)
        backup=Path(result['backup'])/'workbench.sqlite3';self.assertEqual(next(iter(names.load(backup)['records'].values()))['name'],'本机河北店名')
        old=f.Order(self.root/'old','OLD');legacy=self.root/'old.zip';old.export(legacy);plan=full.prepare(self.target.root,legacy)
        with patch('application_reset._check_other_processes'):full.apply(plan)
        self.assertEqual(names.load(self.target.path)['records'],{})
    def test_additive_merge_preserves_local_global_names(self):
        package=self.root/'merge.zip';self.source.export(package);expected=names.load(self.target.path)['records'];plan=merge.prepare(self.target.root,package)
        with patch('order_merge_migration._check_other_processes'):result=full.apply(plan)
        self.assertEqual(result['state'],'COMPLETED');actual=names.load(self.target.path)
        self.assertEqual(actual['records'],expected);self.assertIn('SOURCE',actual['names']);self.assertIn('LOCAL',actual['names'])
    def test_whole_location_move_preserves_names(self):
        import data_location as location,data_relocation as move
        from test_archive_orders import seed
        with patch.dict(os.environ,LOCALAPPDATA=str(self.root/'localappdata')):
            previous=os.environ.pop('REPLENISHMENT_DATA_DIR',None)
            try:
                source=location.default_base();source.mkdir(parents=True);work,repo,payload=seed(source)
                data=names.load(work.path);code=next(iter(data['names']));names.save(work.path,names.prepare(data,[{'store':code,'warehouse':'山东仓','value':'搬迁保留店名'}]));expected=names.load(work.path)['records']
                with patch.object(move,'browser_rows',return_value=[]),patch('application_reset._check_other_processes'):
                    plan=move.prepare(source,self.root/'D盘合成资料');result=move.apply(plan)
                self.assertEqual(result['state'],'COMPLETED');self.assertEqual(names.load(location.root()/'workbench.sqlite3')['records'],expected)
            finally:
                if previous is not None:os.environ['REPLENISHMENT_DATA_DIR']=previous

if __name__=='__main__':unittest.main()
