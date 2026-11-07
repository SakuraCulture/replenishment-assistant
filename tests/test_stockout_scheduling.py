"""Empty business queue polls must not look like active allocation work."""
import sqlite3,sys,tempfile,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import jst_batches,jst_tasks
from stockout_entry import Entry
from jst_task_ui import Generator


class PriorityTests(unittest.TestCase):
    def test_empty_poll_does_not_interrupt_analysis(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'isolated.sqlite3';sqlite3.connect(path).close()
            generator=SimpleNamespace(worker=object(),active_work=False)
            host=SimpleNamespace(jst_generator=generator);entry=SimpleNamespace(host=host)
            self.assertEqual(Generator.drain(generator,path,Path(temp),lambda _:None,None),[])
            self.assertFalse(Entry.busy(entry));self.assertFalse(generator.active_work)

    def test_actual_allocation_work_marks_priority_before_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'isolated.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE jst_batches(batch_id TEXT,status TEXT)');db.execute("INSERT INTO jst_batches VALUES('batch','active')")
            generator=SimpleNamespace(worker=object(),active_work=False)
            entry=SimpleNamespace(host=SimpleNamespace(jst_generator=generator));seen=[]
            def preview(*a,**k):
                seen.append(Entry.busy(entry));return {'original':'batch'}
            with patch.object(jst_batches,'preview_existing',preview),patch.object(jst_batches,'execute',return_value={'batch_id':'batch'}):
                result=Generator.drain(generator,path,Path(temp),lambda _:None,None)
            self.assertEqual(seen,[True]);self.assertEqual(result,[{'batch_id':'batch'}])

    def test_other_business_workers_still_take_priority(self):
        generator=SimpleNamespace(worker=object(),active_work=False)
        entry=SimpleNamespace(host=SimpleNamespace(jst_generator=generator,worker=object()))
        self.assertTrue(Entry.busy(entry))
        entry.host.worker=None;generator.active_work=True
        self.assertTrue(Entry.busy(entry))


if __name__=='__main__':unittest.main()
