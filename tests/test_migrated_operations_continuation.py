"""Additional review probes: real synthetic imports followed by continued work."""
import json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import test_operations_lifecycle as fixture
import work_repository,order_migration,migration_status,order_lifecycle as lifecycle
import operations_flow,jst_orders

class ContinuedWork(unittest.TestCase):
    def setUp(self):
        self.source=fixture.Operations();self.source.setUp();self.addCleanup(self.source.tearDown)
        self.source.route();self.source.stock(100)
        self.source.f.save('return',[{'门店确认数量':40}])
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.target=self.root/'settings';work_repository.Repository(self.target)
        self.h=self.source.h
    def migrate(self):
        package=self.root/'orders.zip'
        order_migration.export(self.source.path.parent,package)
        plan=order_migration.prepare(self.target,package)
        with patch('application_reset._check_other_processes'):result=order_migration.apply(plan)
        self.assertEqual(result['state'],'COMPLETED');self.assertTrue(Path(result['backup']).is_dir())
        self.assertEqual(migration_status.latest(self.target)['state'],'COMPLETED')
        return self.target/'workbench.sqlite3'
    def test_return_only_import_can_adopt_operations_allocate_and_save_payment(self):
        path=self.migrate();self.assertEqual(lifecycle.snapshot(path,self.h)['next'],'operations')
        operations_flow.adopt(path,[self.h])
        prepared=jst_orders.prepare(path,[self.h]);self.assertEqual(prepared['payload']['plans'][0]['rows'][0]['demand'],'40')
        done=jst_orders.commit(path,prepared);self.assertFalse(done['errors'])
        lifecycle.commit(path,jst_orders.payment_plan(path,done['generated'][0]))
        self.assertEqual(lifecycle.snapshot(path,self.h)['next'],'receipt')
    def test_paid_import_can_correct_operations_and_reallocate_without_rewriting_payment(self):
        self.source.f.save('operations',[{'门店确认数量':12}])
        done=jst_orders.commit(self.source.path,jst_orders.prepare(self.source.path,[self.h]))
        lifecycle.commit(self.source.path,jst_orders.payment_plan(self.source.path,done['generated'][0]))
        path=self.migrate();previous=lifecycle.snapshot(path,self.h)['documents']['payment']
        scan=self.source.f.scan('operations',[{'门店确认数量':5}])
        lifecycle.commit(path,lifecycle.prepare_numbered(path,scan,self.h))
        prepared=jst_orders.prepare(path,[self.h],correction=True)
        self.assertFalse(prepared['payload']['skipped'])
        self.assertEqual(prepared['payload']['plans'][0]['rows'][0]['demand'],'5')
        changed=jst_orders.commit(path,prepared);self.assertFalse(changed['errors'])
        after=lifecycle.snapshot(path,self.h)
        self.assertEqual(after['documents']['payment'],previous)
        self.assertFalse(after['health']['payment'])

if __name__=='__main__':unittest.main(verbosity=2)
