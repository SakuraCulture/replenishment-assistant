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


class HubScheduleTests(unittest.TestCase):
    def test_same_time_legacy_plans_form_one_both_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);clock=datetime(2026,11,7,7,59,tzinfo=TZ)
            legacy.save(root,True,'08:00',clock);legacy.save(root,True,'08:00',clock,filename='warehouse-stockout-schedule.json')
            calls=[];entry=SimpleNamespace(host=SimpleNamespace(repository=SimpleNamespace(folder=root)),worker=None,queued=None,closing=False,busy=lambda:False,start=lambda *a,**k:calls.append((a,k)),arm=lambda:None,ensure_watch=lambda:None,status=lambda _:None)
            original=legacy.due
            with patch.object(legacy,'due',side_effect=lambda folder,**kw:original(folder,datetime(2026,11,7,8,0,tzinfo=TZ),**kw)),patch('stockout_common.today',return_value='2026-11-07'):
                schedules.due(entry)
            self.assertEqual(len(calls),1);self.assertEqual(calls[0][0][1]['mode'],'both')
            self.assertTrue(legacy.load(root)['enabled']);self.assertTrue(legacy.load(root,filename='warehouse-stockout-schedule.json')['enabled'])
    def test_replace_preserves_chosen_mode_and_disables_old_plans(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);legacy.save(root,True);legacy.save(root,True,filename='warehouse-stockout-schedule.json')
            schedules.save(root,True,'09:00','warehouse',['山东仓']);rows=schedules.plans(root)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['mode'],'warehouse');self.assertEqual(rows[0]['warehouses'],['山东仓']);self.assertFalse(legacy.load(root)['enabled'])
            schedules.save(root,False,'09:00','warehouse');self.assertEqual(schedules.plans(root),[])


if __name__=='__main__':unittest.main()
