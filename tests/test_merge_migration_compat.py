"""Trial integration only: migrate the new operations route and its facts."""
import sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import test_operations_lifecycle as fixture
import order_migration as migration,migration_status,order_lifecycle as lifecycle
import operations_flow,work_repository
import jst_orders

class CombinedMigration(unittest.TestCase):
    def test_operations_route_return_allocation_and_paid_facts_survive_import(self):
        source=fixture.Operations();source.setUp();self.addCleanup(source.tearDown)
        source.route();source.f.save('return',[{'门店确认数量':40}])
        source.f.save('operations',[{'门店确认数量':12}]);source.stock(7)
        done=jst_orders.commit(source.path,jst_orders.prepare(source.path,[source.h]))
        self.assertFalse(done['errors'])
        lifecycle.commit(source.path,jst_orders.payment_plan(source.path,done['generated'][0]))
        view=source.f.view()
        with closing(sqlite3.connect(source.path)) as db:
            routes=db.execute('SELECT * FROM lifecycle_routes ORDER BY oid,revision').fetchall()
            versions=db.execute('SELECT * FROM lifecycle_versions ORDER BY oid,revision,warehouse,stage,version').fetchall()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'settings';work_repository.Repository(target)
            package=root/'operations-orders.zip'
            result=migration.export(source.path.parent,package)
            self.assertEqual(result['manifest']['counts']['lifecycle_routes'],len(routes))
            plan=migration.prepare(target,package)
            with patch('application_reset._check_other_processes'):
                receipt=migration.apply(plan)
            self.assertEqual(receipt['state'],'COMPLETED')
            self.assertEqual(migration_status.latest(target)['state'],'COMPLETED')
            self.assertTrue(Path(receipt['backup']).is_dir())
            path=target/'workbench.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT * FROM lifecycle_routes ORDER BY oid,revision').fetchall(),routes)
                self.assertEqual(db.execute('SELECT * FROM lifecycle_versions ORDER BY oid,revision,warehouse,stage,version').fetchall(),versions)
                self.assertTrue(operations_flow.required(db,source.h))
            after=lifecycle.snapshot(path,source.h)
            for stage in ('return','operations','payment'):
                self.assertEqual(after['documents'][stage],view['documents'][stage])
            self.assertEqual(after['next'],view['next'])
            self.assertEqual(after['health'],view['health'])
            self.assertIsNone(migration.recover(target))
