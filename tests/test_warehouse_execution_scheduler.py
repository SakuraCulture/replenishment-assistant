"""A late scheduled whole-store result cannot restore a deleted warehouse."""
import unittest
import test_scheduled_replenishment as fixtures
import deletion_barrier as barrier
import work_repository as wr


class ScheduledWarehouseFence(unittest.TestCase):
    def test_late_result_rejected_before_any_value_or_ready_state_is_saved(self):
        f=fixtures.ScheduleTests();f.setUp();self.addCleanup(f.tearDown)
        run=f.repo.claim(f.repo.manual(f.s)['id']);rid=run['id']
        f.repo.save_value(rid,'basis',{'warehouse_deletion_generations':{'S':{'河北仓':0,'山东仓':0}}},run['epoch'])
        with f.repo.db(True) as db:
            barrier.schema(db)
            db.execute('INSERT INTO warehouse_deletion_generations VALUES(?,?,?,1)',(run['day'],'S','河北仓'))
        before=f.repo.get(rid)
        result={'store':'S','rows':[{'E':'A','AU':'河北仓'},{'E':'B','AU':'山东仓'}],'summary':{'amount':10}}
        with self.assertRaisesRegex(wr.Conflict,'仓单已删除|代次变化'):
            f.repo.save_value(rid,'result:S',result,run['epoch'],store='S')
        self.assertEqual(f.repo.get(rid),before)
        self.assertIsNone(f.repo.value(rid,'result:S'))
        self.assertEqual(f.repo.value(rid,'basis')['warehouse_deletion_generations']['S']['山东仓'],0)


if __name__=='__main__':unittest.main()
