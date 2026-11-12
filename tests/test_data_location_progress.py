import json,tempfile,unittest
from pathlib import Path
from data_location_progress import presentation,read_status

class Progress(unittest.TestCase):
    def test_old_inflight_receipt_has_byte_and_file_progress(self):
        value=presentation({'state':'APPLYING','phase':'COPYING','files':4047,'total_files':6242,'message':'已复制4047/6242份，5455.2/7467.4 MiB'})
        self.assertAlmostEqual(value['percent'],73.05,places=1);self.assertFalse(value['terminal']);self.assertIn('4047 / 6242',value['counts'])
    def test_each_stage_progress_is_separate_from_overall_success(self):
        value=presentation({'state':'APPLYING','phase':'ARCHIVING','bytes_done':100,'bytes_total':100})
        self.assertEqual(value['percent'],100);self.assertFalse(value['success']);self.assertFalse(value['terminal'])
    def test_pending_cleanup_is_not_shown_as_total_completion(self):
        value=presentation({'state':'COMPLETED','phase':'CLEANUP_PENDING'})
        self.assertTrue(value['terminal']);self.assertFalse(value['success']);self.assertIn('尚未完成',value['title'])
    def test_rollback_explains_original_error(self):
        value=presentation({'state':'FAILED','phase':'ROLLED_BACK','error':{'message':'文件范围变化'}})
        self.assertIn('原位置',value['title']);self.assertEqual(value['details'],'文件范围变化');self.assertFalse(value['success'])
    def test_success_requires_final_phase(self):
        self.assertTrue(presentation({'state':'COMPLETED','phase':'COMPLETED'})['success'])
    def test_unquantifiable_integrity_check_has_no_fake_percentage(self):
        self.assertIsNone(presentation({'state':'APPLYING','phase':'CHECKING'})['percent'])
    def test_viewer_is_pinned_to_one_operation_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'move.json';path.write_text(json.dumps({'nonce':'a'*32,'state':'APPLYING'}),'utf-8');before=path.read_bytes()
            self.assertEqual(read_status(folder,'a'*32)['state'],'APPLYING')
            with self.assertRaisesRegex(ValueError,'另一项'):read_status(folder,'b'*32)
            self.assertEqual(path.read_bytes(),before);self.assertEqual([p.name for p in Path(folder).iterdir()],['move.json'])
    def test_finished_operation_can_still_be_read_after_the_next_move(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'operations').mkdir()
            prior=root/'operations'/('a'*32+'.status.json');prior.write_text(json.dumps({'nonce':'a'*32,'state':'FAILED'}),'utf-8')
            (root/'move.json').write_text(json.dumps({'nonce':'b'*32,'state':'APPLYING'}),'utf-8')
            self.assertEqual(read_status(root,'a'*32)['state'],'FAILED')
    def test_read_only_viewer_is_not_a_writer_but_other_workers_remain_protected(self):
        import application_processes as p
        from unittest.mock import patch
        rows=[{'pid':12340,'parent':1,'name':'补货助手.exe','argv':['补货助手.exe','--data-location-progress','--operation','a'*32],'created':'2026-11-12T00:00:00Z'},
              {'pid':12341,'parent':1,'name':'补货助手.exe','argv':['补货助手.exe','--catalog-validate'],'created':'2026-11-12T00:00:00Z'}]
        with patch.object(p,'records',return_value=rows),patch.object(p,'main_windows',return_value={}),patch.object(p,'wrapper_ids',return_value=set()),patch.object(p,'kernel_state',return_value={'state':'RUNNING'}):
            matches=p.discover(guarded=True)
        self.assertEqual([r['pid'] for r in matches],[12341]);self.assertEqual(matches[0]['role'],'worker')
    def test_live_status_readers_allow_atomic_publication_on_windows(self):
        import threading
        from data_location_status import read,save
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'status.json';save(path,{'generation':0});stop=threading.Event();errors=[]
            def observer():
                try:
                    # Poll 250 times faster than a real window, but release the
                    # file between polls. An infinite open/read loop would
                    # deliberately starve Windows MoveFileEx, not model a UI.
                    while not stop.is_set():
                        self.assertIsInstance(read(path)['generation'],int);stop.wait(.002)
                except BaseException as exc:errors.append(exc)
            workers=[threading.Thread(target=observer) for _ in range(2)]
            for worker in workers:worker.start()
            try:
                for number in range(1,101):save(path,{'generation':number})
            finally:
                stop.set()
                for worker in workers:worker.join(timeout=5)
            self.assertFalse(errors,errors);self.assertEqual(read(path)['generation'],100)

if __name__=='__main__':unittest.main()
