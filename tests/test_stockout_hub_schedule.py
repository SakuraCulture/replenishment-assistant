"""Legacy schedule scope, coalescing and explicit replacement."""
import sys,tempfile,unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import stockout_trigger as legacy
import stockout_hub_schedule as schedules
from stockout_common import TZ
from stockout_schedule_queue import Queue


class HubScheduleTests(unittest.TestCase):
    def test_same_time_legacy_plans_form_one_both_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);clock=datetime(2026,11,7,7,59,tzinfo=TZ)
            legacy.save(root,True,'08:00',clock);legacy.save(root,True,'08:00',clock,filename='warehouse-stockout-schedule.json')
            calls=[];entry=SimpleNamespace(host=SimpleNamespace(repository=SimpleNamespace(folder=root)),worker=None,queued=None,closing=False,busy=lambda:False,start=lambda *a,**k:calls.append((a,k)),arm=lambda:None,ensure_watch=lambda:None,status=lambda _:None)
            with patch('stockout_hub_service.preview',return_value={'day':'2026-11-07'}),patch('stockout_common.today',return_value='2026-11-07'):
                schedules.due(entry,datetime(2026,11,7,8,0,tzinfo=TZ))
            self.assertEqual(len(calls),1);self.assertEqual(calls[0][0][1]['mode'],'both')
            self.assertTrue(legacy.load(root)['enabled']);self.assertTrue(legacy.load(root,filename='warehouse-stockout-schedule.json')['enabled'])
            rows=Queue(root).entries();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'CLAIMED')
            self.assertEqual(calls[0][0][1]['scheduled_id'],rows[0]['id']);self.assertEqual(len(rows[0]['contributors']),2)
    def test_replace_preserves_chosen_mode_and_disables_old_plans(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);legacy.save(root,True);legacy.save(root,True,filename='warehouse-stockout-schedule.json')
            schedules.save(root,True,'09:00','warehouse',['山东仓']);rows=schedules.plans(root)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['mode'],'warehouse');self.assertEqual(rows[0]['warehouses'],['山东仓']);self.assertFalse(legacy.load(root)['enabled'])
            schedules.save(root,False,'09:00','warehouse');self.assertEqual(schedules.plans(root),[])

    def test_commit_and_claim_interruptions_reuse_original_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);before=datetime(2026,11,7,7,59,tzinfo=TZ);when=before.replace(minute=0,hour=8)
            legacy.save(root,True,'08:00',before);q=Queue(root)
            prepare=lambda dt,plans:{'mode':'store','day':dt.date().isoformat()}
            with patch.object(legacy,'next_time',side_effect=OSError('interruption before committing trigger and cursor')):
                with self.assertRaises(OSError):q.due(when,prepare)
            self.assertFalse(q.entries());self.assertEqual(legacy.load(root)['next_at'],when.isoformat())
            registered,=Queue(root).due(when,prepare)
            self.assertFalse(Queue(root).due(when,prepare))
            first=q.claim();self.assertEqual(first['id'],registered['id'])
            reopened=Queue(root);reopened.recover();same=reopened.claim()
            self.assertEqual((same['id'],same['request']),(first['id'],first['request']))
            self.assertGreater(same['epoch'],first['epoch'])
            with self.assertRaisesRegex(ValueError,'代次'):reopened.require_claim(first['id'],first['epoch'])
            reopened.finish(first['id'],first['epoch'],'COMPLETE')
            self.assertEqual(reopened.entries()[0]['state'],'CLAIMED')
            reopened.finish(same['id'],same['epoch'],'COMPLETE')
            self.assertFalse(reopened.pending())


if __name__=='__main__':unittest.main()
