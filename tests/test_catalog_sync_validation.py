import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import openpyxl
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import catalog_sync_validation as validation
import warehouse_catalog as wc


class CatalogValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.file=self.root/'source.xlsx';b=openpyxl.Workbook();s=b.active;s.append(wc.REQUIRED+['商品名称'])
        s.append(['001234567890123456','',None,None,'禁用',0,'合成样本']);b.save(self.file);b.close()
        self.request={'schema':1,'file':str(self.file),'warehouse':'山东仓','sha256':validation.digest(self.file),'expected_rows':1,'candidate':str(self.root/'candidate.json')}
    def tearDown(self):self.temp.cleanup()
    def test_original_values_types_order_and_rows_match_manual_import(self):
        from datetime import datetime
        with patch.object(wc,'datetime') as clock:
            clock.now.return_value=datetime(2026,11,3)
            manual=wc.import_catalog(self.file,'山东仓');receipt=validation.validate(self.request)
        got=validation.candidate(receipt);self.assertEqual(list(manual),list(got));self.assertEqual(manual,got)
        self.assertIsNone(got['items']['001234567890123456']['基本售价']);self.assertEqual(got['items']['001234567890123456']['_row'],2)
        self.assertEqual(validation.digest(self.file),self.request['sha256'])
    def test_count_hash_and_candidate_mutations_rejected(self):
        with self.assertRaises(ValueError):validation.validate({**self.request,'expected_rows':2})
        with self.assertRaises(ValueError):validation.validate({**self.request,'sha256':'0'*64})
        receipt=validation.validate(self.request);Path(receipt['candidate']).write_text('{}')
        with self.assertRaises(ValueError):validation.candidate(receipt)
    def test_headless_app_entry_does_not_import_qt_or_write_settings(self):
        request=self.root/'request.json';request.write_text(json.dumps(self.request),encoding='utf-8');result=self.root/'result.json';settings=self.root/'must-not-exist'
        env=os.environ.copy();env['REPLENISHMENT_DATA_DIR']=str(settings)
        p=subprocess.run([sys.executable,str(Path(wc.__file__).parent/'app.py'),'--catalog-validate','--request',str(request),'--result',str(result)],env=env,capture_output=True,timeout=60)
        self.assertEqual(p.returncode,0,p.stderr);self.assertFalse(json.loads(result.read_text('utf-8'))['qt_imported']);self.assertFalse(settings.exists())


if __name__=='__main__':unittest.main()
